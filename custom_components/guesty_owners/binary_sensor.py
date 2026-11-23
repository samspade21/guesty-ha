"""Binary sensors for the Guesty Owners Portal integration."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .coordinator import GuestyOwnersConfigEntry, StaysCoordinator
from .entity import StayEntity, async_add_listing_entities, stay_attributes
from .models import ListingSnapshot, Stay

# Every entity reads from a coordinator; none of them polls on its own.
PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class StayBinarySensorDescription(BinarySensorEntityDescription):
    """Describes a binary sensor derived from a listing's stays."""

    is_on_fn: Callable[[ListingSnapshot, datetime], bool]
    stay_fn: Callable[[ListingSnapshot, datetime], Stay | None] | None = None


def _owner_stay(snapshot: ListingSnapshot, now: datetime) -> Stay | None:
    stay = snapshot.current_stay(now)
    return stay if stay is not None and stay.owner else None


BINARY_SENSORS: tuple[StayBinarySensorDescription, ...] = (
    StayBinarySensorDescription(
        key="occupied",
        translation_key="occupied",
        device_class=BinarySensorDeviceClass.OCCUPANCY,
        is_on_fn=lambda snapshot, now: snapshot.current_stay(now) is not None,
        stay_fn=lambda snapshot, now: snapshot.current_stay(now),
    ),
    StayBinarySensorDescription(
        key="owner_stay",
        translation_key="owner_stay",
        is_on_fn=lambda snapshot, now: _owner_stay(snapshot, now) is not None,
        stay_fn=_owner_stay,
    ),
    StayBinarySensorDescription(
        key="check_in_today",
        translation_key="check_in_today",
        is_on_fn=lambda snapshot, now: snapshot.arrival_today(now) is not None,
        stay_fn=lambda snapshot, now: snapshot.arrival_today(now),
    ),
    StayBinarySensorDescription(
        key="check_out_today",
        translation_key="check_out_today",
        is_on_fn=lambda snapshot, now: snapshot.departure_today(now) is not None,
        stay_fn=lambda snapshot, now: snapshot.departure_today(now),
    ),
    StayBinarySensorDescription(
        key="turnover_today",
        translation_key="turnover_today",
        is_on_fn=lambda snapshot, now: (
            snapshot.arrival_today(now) is not None
            and snapshot.departure_today(now) is not None
        ),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: GuestyOwnersConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the binary sensors."""
    stays = entry.runtime_data.stays
    async_add_listing_entities(
        entry,
        async_add_entities,
        lambda listing_id: [
            StayBinarySensor(stays, entry, listing_id, description)
            for description in BINARY_SENSORS
        ],
    )


class StayBinarySensor(StayEntity, BinarySensorEntity):
    """A binary sensor derived from a listing's stays."""

    entity_description: StayBinarySensorDescription

    def __init__(
        self,
        coordinator: StaysCoordinator,
        entry: GuestyOwnersConfigEntry,
        listing_id: str,
        description: StayBinarySensorDescription,
    ) -> None:
        """Initialize the binary sensor."""
        listing = coordinator.data[listing_id].listing
        super().__init__(coordinator, entry, listing, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool | None:
        """Return the state."""
        if (snapshot := self.snapshot) is None:
            return None
        return self.entity_description.is_on_fn(snapshot, dt_util.utcnow())

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Describe the stay this sensor is about."""
        if (stay_fn := self.entity_description.stay_fn) is None:
            return None
        if (snapshot := self.snapshot) is None:
            return None
        return stay_attributes(stay_fn(snapshot, dt_util.utcnow()))
