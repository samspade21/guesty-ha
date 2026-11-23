"""Base entities for the Guesty Owners Portal integration."""

from collections.abc import Callable
from datetime import datetime
from typing import Any

from homeassistant.core import CALLBACK_TYPE, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)
from homeassistant.util import dt as dt_util

from .const import DOMAIN, MANUFACTURER
from .coordinator import GuestyOwnersConfigEntry, StatsCoordinator, StaysCoordinator
from .models import Listing, ListingSnapshot, ListingStats, Stay


def account_identifier(owner_id: str) -> str:
    """Return the device identifier of the owner account."""
    return f"owner_{owner_id}"


def stay_attributes(stay: Stay | None) -> dict[str, Any]:
    """Describe a stay for use as state or event attributes."""
    if stay is None:
        return {}
    return {
        "reservation_id": stay.id,
        "stay_type": "owner" if stay.owner else "guest",
        "status": stay.status,
        "source": stay.source_label,
        "guest_name": stay.guest_name,
        "check_in": stay.check_in.isoformat(),
        "check_out": stay.check_out.isoformat(),
        "nights": stay.nights,
        "guests": stay.guests,
        "adults": stay.adults,
        "children": stay.children,
        "infants": stay.infants,
        "pets": stay.pets,
    }


@callback
def async_add_listing_entities(
    entry: GuestyOwnersConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
    factory: Callable[[str], list[Any]],
) -> None:
    """Create entities for every listing, now and whenever a new one appears."""
    stays = entry.runtime_data.stays
    known: set[str] = set()

    @callback
    def add_new_listings() -> None:
        current = set(stays.data)
        if current:
            # Forget listings whose device was just removed as stale, so they
            # are set up again if they ever come back.
            known.intersection_update(current)
        if new := current - known:
            known.update(new)
            async_add_entities(
                entity for listing_id in sorted(new) for entity in factory(listing_id)
            )

    add_new_listings()
    entry.async_on_unload(stays.async_add_listener(add_new_listings))


class ListingEntity[CoordinatorT: DataUpdateCoordinator[Any]](
    CoordinatorEntity[CoordinatorT]
):
    """An entity that belongs to one listing."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: CoordinatorT,
        entry: GuestyOwnersConfigEntry,
        listing: Listing,
        key: str,
    ) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self._listing_id = listing.id
        self._attr_unique_id = f"{listing.id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, listing.id)},
            name=listing.name,
            manufacturer=MANUFACTURER,
            model=listing.property_type,
            entry_type=DeviceEntryType.SERVICE,
            configuration_url=(
                f"https://{entry.runtime_data.client.hostname}"
                f"/my-properties/{listing.id}/calendar"
            ),
        )


class StayEntity(ListingEntity[StaysCoordinator]):
    """An entity whose state depends on a listing's stays and on the clock.

    Whether a home is occupied changes at check-in time, not when the next poll
    happens, so the state is also rewritten at every check-in, check-out and
    local midnight.
    """

    _unsub_timer: CALLBACK_TYPE | None = None

    @property
    def snapshot(self) -> ListingSnapshot | None:
        """Return the latest data for this listing."""
        return self.coordinator.data.get(self._listing_id)

    @property
    def available(self) -> bool:
        """Return True while the listing is still in the portal."""
        return super().available and self.snapshot is not None

    async def async_added_to_hass(self) -> None:
        """Start following the clock."""
        await super().async_added_to_hass()
        self._schedule_transition()

    async def async_will_remove_from_hass(self) -> None:
        """Stop following the clock."""
        self._cancel_transition()
        await super().async_will_remove_from_hass()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle new data."""
        self._schedule_transition()
        super()._handle_coordinator_update()

    @callback
    def _cancel_transition(self) -> None:
        if self._unsub_timer is not None:
            self._unsub_timer()
            self._unsub_timer = None

    @callback
    def _schedule_transition(self) -> None:
        self._cancel_transition()
        if (snapshot := self.snapshot) is None:
            return
        self._unsub_timer = async_track_point_in_utc_time(
            self.hass,
            self._handle_transition,
            snapshot.next_transition(dt_util.utcnow()),
        )

    @callback
    def _handle_transition(self, _now: datetime) -> None:
        self._unsub_timer = None
        self.async_write_ha_state()
        self._schedule_transition()


class ListingStatsEntity(ListingEntity[StatsCoordinator]):
    """An entity showing a listing's slow-changing figures."""

    @property
    def stats(self) -> ListingStats | None:
        """Return the latest figures for this listing."""
        return self.coordinator.data.listings.get(self._listing_id)

    @property
    def available(self) -> bool:
        """Return True while the listing is still in the portal."""
        return super().available and self.stats is not None


class AccountEntity(CoordinatorEntity[StatsCoordinator]):
    """An entity that belongs to the owner account rather than one listing."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: StatsCoordinator, entry: GuestyOwnersConfigEntry, key: str
    ) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        owner_id = entry.runtime_data.stays.owner_id
        self._attr_unique_id = f"{owner_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, account_identifier(owner_id))},
            name=entry.title,
            manufacturer=MANUFACTURER,
            entry_type=DeviceEntryType.SERVICE,
            configuration_url=f"https://{entry.runtime_data.client.hostname}",
        )
