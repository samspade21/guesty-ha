"""Sensors for the Guesty Owners Portal integration."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.typing import StateType
from homeassistant.util import dt as dt_util

from .const import BOOKED_NIGHTS_WINDOW_DAYS
from .coordinator import GuestyOwnersConfigEntry, StatsCoordinator, StaysCoordinator
from .entity import (
    AccountEntity,
    ListingStatsEntity,
    StayEntity,
    async_add_listing_entities,
    stay_attributes,
)
from .models import (
    AVAILABILITY_STATES,
    ListingSnapshot,
    ListingStats,
    PeriodMetrics,
    StatsData,
    Stay,
)

# Every entity reads from a coordinator; none of them polls on its own.
PARALLEL_UPDATES = 0

type SensorValue = StateType | date | datetime


@dataclass(frozen=True, kw_only=True)
class StaySensorDescription(SensorEntityDescription):
    """Describes a sensor derived from a listing's stays and calendar."""

    value_fn: Callable[[ListingSnapshot, datetime], SensorValue]
    stay_fn: Callable[[ListingSnapshot, datetime], Stay | None] | None = None
    currency_fn: Callable[[ListingSnapshot, datetime], str | None] | None = None


@dataclass(frozen=True, kw_only=True)
class StatsSensorDescription(SensorEntityDescription):
    """Describes a sensor derived from a listing's slow-changing figures."""

    value_fn: Callable[[ListingStats], SensorValue]
    currency_fn: Callable[[ListingStats], str | None] | None = None


@dataclass(frozen=True, kw_only=True)
class AccountSensorDescription(SensorEntityDescription):
    """Describes a sensor that belongs to the owner account."""

    value_fn: Callable[[StatsData], SensorValue]
    currency_fn: Callable[[StatsData], str | None] | None = None


def _occupancy(snapshot: ListingSnapshot, now: datetime) -> str:
    stay = snapshot.current_stay(now)
    if stay is None:
        return "vacant"
    return "owner" if stay.owner else "guest"


def _next_check_out(snapshot: ListingSnapshot, now: datetime) -> Stay | None:
    """Return whoever leaves next: the current occupant, else the next arrival."""
    return snapshot.current_stay(now) or snapshot.next_stay(now)


def _availability(snapshot: ListingSnapshot, now: datetime) -> str | None:
    day = snapshot.tonight(now)
    if day is None or day.status not in AVAILABILITY_STATES:
        return None
    return day.status


STAY_SENSORS: tuple[StaySensorDescription, ...] = (
    StaySensorDescription(
        key="occupancy",
        translation_key="occupancy",
        device_class=SensorDeviceClass.ENUM,
        options=["vacant", "guest", "owner"],
        value_fn=_occupancy,
        stay_fn=lambda snapshot, now: snapshot.current_stay(now),
    ),
    StaySensorDescription(
        key="next_check_in",
        translation_key="next_check_in",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda snapshot, now: (
            stay.check_in if (stay := snapshot.next_stay(now)) else None
        ),
        stay_fn=lambda snapshot, now: snapshot.next_stay(now),
    ),
    StaySensorDescription(
        key="next_check_out",
        translation_key="next_check_out",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda snapshot, now: (
            stay.check_out if (stay := _next_check_out(snapshot, now)) else None
        ),
        stay_fn=_next_check_out,
    ),
    StaySensorDescription(
        key="upcoming_stays",
        translation_key="upcoming_stays",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda snapshot, now: len(snapshot.upcoming_stays(now)),
    ),
    StaySensorDescription(
        key="booked_nights_next_30_days",
        translation_key="booked_nights_next_30_days",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda snapshot, now: snapshot.booked_nights(
            now, BOOKED_NIGHTS_WINDOW_DAYS
        ),
    ),
    StaySensorDescription(
        key="availability",
        translation_key="availability",
        device_class=SensorDeviceClass.ENUM,
        options=list(AVAILABILITY_STATES),
        value_fn=_availability,
    ),
    StaySensorDescription(
        key="nightly_rate",
        translation_key="nightly_rate",
        device_class=SensorDeviceClass.MONETARY,
        suggested_display_precision=0,
        value_fn=lambda snapshot, now: (
            day.price if (day := snapshot.tonight(now)) else None
        ),
        currency_fn=lambda snapshot, now: (
            day.currency if (day := snapshot.tonight(now)) else None
        ),
    ),
    StaySensorDescription(
        key="minimum_nights",
        translation_key="minimum_nights",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda snapshot, now: (
            day.min_nights if (day := snapshot.tonight(now)) else None
        ),
    ),
)


def _metric_sensors(
    period: str, getter: Callable[[ListingStats], PeriodMetrics | None]
) -> tuple[StatsSensorDescription, ...]:
    """Describe the revenue sensors of one reporting period."""

    def value(attribute: str) -> Callable[[ListingStats], SensorValue]:
        return lambda stats: (
            getattr(metrics, attribute) if (metrics := getter(stats)) else None
        )

    def currency(stats: ListingStats) -> str | None:
        return metrics.currency if (metrics := getter(stats)) else None

    return (
        StatsSensorDescription(
            key=f"owner_revenue_{period}",
            translation_key=f"owner_revenue_{period}",
            device_class=SensorDeviceClass.MONETARY,
            suggested_display_precision=2,
            value_fn=value("owner_revenue"),
            currency_fn=currency,
        ),
        StatsSensorDescription(
            key=f"net_rental_income_{period}",
            translation_key=f"net_rental_income_{period}",
            device_class=SensorDeviceClass.MONETARY,
            suggested_display_precision=2,
            value_fn=value("net_rental_income"),
            currency_fn=currency,
        ),
        StatsSensorDescription(
            key=f"occupancy_rate_{period}",
            translation_key=f"occupancy_rate_{period}",
            native_unit_of_measurement=PERCENTAGE,
            state_class=SensorStateClass.MEASUREMENT,
            suggested_display_precision=0,
            value_fn=value("occupancy"),
        ),
        StatsSensorDescription(
            key=f"booked_nights_{period}",
            translation_key=f"booked_nights_{period}",
            state_class=SensorStateClass.MEASUREMENT,
            value_fn=value("booked_nights"),
        ),
        StatsSensorDescription(
            key=f"average_nightly_rate_{period}",
            translation_key=f"average_nightly_rate_{period}",
            device_class=SensorDeviceClass.MONETARY,
            suggested_display_precision=2,
            entity_registry_enabled_default=False,
            value_fn=value("average_nightly_rate"),
            currency_fn=currency,
        ),
    )


STATS_SENSORS: tuple[StatsSensorDescription, ...] = (
    *_metric_sensors("this_month", lambda stats: stats.month),
    *_metric_sensors("this_year", lambda stats: stats.year),
    StatsSensorDescription(
        key="rating",
        translation_key="rating",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda stats: stats.reviews.average if stats.reviews else None,
    ),
    StatsSensorDescription(
        key="reviews",
        translation_key="reviews",
        state_class=SensorStateClass.TOTAL,
        value_fn=lambda stats: stats.reviews.count if stats.reviews else None,
    ),
    StatsSensorDescription(
        key="latest_review",
        translation_key="latest_review",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda stats: stats.reviews.latest if stats.reviews else None,
    ),
)


def _statement_currency(data: StatsData) -> str | None:
    return data.statement.currency if data.statement else None


ACCOUNT_SENSORS: tuple[AccountSensorDescription, ...] = (
    AccountSensorDescription(
        key="unread_messages",
        translation_key="unread_messages",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.unread_messages,
    ),
    AccountSensorDescription(
        key="statement_due_to_owner",
        translation_key="statement_due_to_owner",
        device_class=SensorDeviceClass.MONETARY,
        suggested_display_precision=2,
        value_fn=lambda data: data.statement.due_to_owner if data.statement else None,
        currency_fn=_statement_currency,
    ),
    AccountSensorDescription(
        key="statement_ending_balance",
        translation_key="statement_ending_balance",
        device_class=SensorDeviceClass.MONETARY,
        suggested_display_precision=2,
        value_fn=lambda data: data.statement.ending_balance if data.statement else None,
        currency_fn=_statement_currency,
    ),
    AccountSensorDescription(
        key="statement_period_end",
        translation_key="statement_period_end",
        device_class=SensorDeviceClass.DATE,
        value_fn=lambda data: data.statement.period_end if data.statement else None,
    ),
    AccountSensorDescription(
        key="statement_payout_status",
        translation_key="statement_payout_status",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.statement.payout_status if data.statement else None,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: GuestyOwnersConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the sensors."""
    stays = entry.runtime_data.stays
    stats = entry.runtime_data.stats

    def listing_sensors(listing_id: str) -> list[SensorEntity]:
        return [
            *(StaySensor(stays, entry, listing_id, item) for item in STAY_SENSORS),
            *(StatsSensor(stats, entry, listing_id, item) for item in STATS_SENSORS),
        ]

    async_add_listing_entities(entry, async_add_entities, listing_sensors)
    async_add_entities(AccountSensor(stats, entry, item) for item in ACCOUNT_SENSORS)


class StaySensor(StayEntity, SensorEntity):
    """A sensor derived from a listing's stays and calendar."""

    entity_description: StaySensorDescription

    def __init__(
        self,
        coordinator: StaysCoordinator,
        entry: GuestyOwnersConfigEntry,
        listing_id: str,
        description: StaySensorDescription,
    ) -> None:
        """Initialize the sensor."""
        listing = coordinator.data[listing_id].listing
        super().__init__(coordinator, entry, listing, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> SensorValue:
        """Return the state."""
        if (snapshot := self.snapshot) is None:
            return None
        return self.entity_description.value_fn(snapshot, dt_util.utcnow())

    @property
    def native_unit_of_measurement(self) -> str | None:
        """Return the unit, which for prices is the listing's currency."""
        if (currency_fn := self.entity_description.currency_fn) is None:
            return self.entity_description.native_unit_of_measurement
        if (snapshot := self.snapshot) is None:
            return None
        return currency_fn(snapshot, dt_util.utcnow())

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Describe the stay this sensor is about."""
        if (stay_fn := self.entity_description.stay_fn) is None:
            return None
        if (snapshot := self.snapshot) is None:
            return None
        return stay_attributes(stay_fn(snapshot, dt_util.utcnow()))


class StatsSensor(ListingStatsEntity, SensorEntity):
    """A sensor derived from a listing's slow-changing figures."""

    entity_description: StatsSensorDescription

    def __init__(
        self,
        coordinator: StatsCoordinator,
        entry: GuestyOwnersConfigEntry,
        listing_id: str,
        description: StatsSensorDescription,
    ) -> None:
        """Initialize the sensor."""
        listing = entry.runtime_data.stays.data[listing_id].listing
        super().__init__(coordinator, entry, listing, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> SensorValue:
        """Return the state."""
        if (stats := self.stats) is None:
            return None
        return self.entity_description.value_fn(stats)

    @property
    def native_unit_of_measurement(self) -> str | None:
        """Return the unit, which for money is the account's currency."""
        if (currency_fn := self.entity_description.currency_fn) is None:
            return self.entity_description.native_unit_of_measurement
        if (stats := self.stats) is None:
            return None
        return currency_fn(stats)


class AccountSensor(AccountEntity, SensorEntity):
    """A sensor that belongs to the owner account."""

    entity_description: AccountSensorDescription

    def __init__(
        self,
        coordinator: StatsCoordinator,
        entry: GuestyOwnersConfigEntry,
        description: AccountSensorDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, entry, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> SensorValue:
        """Return the state."""
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def native_unit_of_measurement(self) -> str | None:
        """Return the unit, which for money is the statement's currency."""
        if (currency_fn := self.entity_description.currency_fn) is None:
            return self.entity_description.native_unit_of_measurement
        return currency_fn(self.coordinator.data)
