"""Reservation change events for the Guesty Owners Portal integration."""

from datetime import date, timedelta

from homeassistant.components.event import EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .coordinator import GuestyOwnersConfigEntry, StaysCoordinator
from .entity import ListingEntity, async_add_listing_entities, stay_attributes
from .models import ListingSnapshot, Stay

# Every entity reads from a coordinator; none of them polls on its own.
PARALLEL_UPDATES = 0

EVENT_BOOKED = "booked"
EVENT_CANCELED = "canceled"
EVENT_MODIFIED = "modified"

# A reservation made this recently before the previous refresh still counts as
# new: Guesty's record can show up a little after the booking itself.
RECENTLY_CREATED = timedelta(days=1)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: GuestyOwnersConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the reservation event entities."""
    stays = entry.runtime_data.stays
    async_add_listing_entities(
        entry,
        async_add_entities,
        lambda listing_id: [ReservationEvent(stays, entry, listing_id)],
    )


def _schedule(stay: Stay) -> tuple[object, ...]:
    """Return the parts of a stay whose change is worth an event."""
    return (stay.check_in, stay.check_out, stay.guests, stay.status)


def _within(day: date, end: date | None) -> bool:
    """Return True if a source that reached ``end`` would have shown ``day``."""
    return end is not None and day <= end


def _newly_booked(stay: Stay, previous: ListingSnapshot) -> bool:
    """Decide whether a stay that just came into view is a new booking.

    A stay can also come into view because the calendar window moved forward
    over a booking the reservation list never mentioned, or because a source
    that had failed works again. Neither is news.
    """
    if stay.created_at is not None:
        # The calendar says when the reservation was made, which settles it.
        return stay.created_at >= previous.fetched_at - RECENTLY_CREATED
    # Only the reservation list knows this stay; it is new if the list was
    # already being read that far ahead and did not have it.
    return _within(stay.check_in_date, previous.list_end)


def _canceled(stay: Stay, previous: ListingSnapshot, current: ListingSnapshot) -> bool:
    """Decide whether a stay that dropped out of view was canceled.

    Stays also drop out once they are over, or when the source that showed them
    cannot be read. Only a stay that had not started, and that a working source
    would still be showing, counts.
    """
    if stay.check_in <= dt_util.utcnow():
        return False
    if _within(stay.check_in_date, current.calendar_end):
        return True
    if _within(stay.check_in_date, previous.calendar_end):
        # It may have been known from the calendar alone, which is now
        # unreadable or (after an options change) shorter.
        return False
    return _within(stay.check_in_date, current.list_end)


def changes(
    previous: ListingSnapshot, current: ListingSnapshot
) -> list[tuple[str, Stay]]:
    """Compare two snapshots of a listing and list what happened in between."""
    before = {stay.id: stay for stay in previous.stays}
    after = {stay.id: stay for stay in current.stays}
    found: list[tuple[str, Stay]] = []
    for stay_id, stay in after.items():
        old = before.get(stay_id)
        if old is None:
            if _newly_booked(stay, previous):
                found.append((EVENT_BOOKED, stay))
        elif _schedule(old) != _schedule(stay):
            found.append((EVENT_MODIFIED, stay))
    found.extend(
        (EVENT_CANCELED, stay)
        for stay_id, stay in before.items()
        if stay_id not in after and _canceled(stay, previous, current)
    )
    return found


class ReservationEvent(ListingEntity[StaysCoordinator], EventEntity):
    """Fires when a reservation is booked, canceled or modified."""

    _attr_translation_key = "reservation"

    def __init__(
        self,
        coordinator: StaysCoordinator,
        entry: GuestyOwnersConfigEntry,
        listing_id: str,
    ) -> None:
        """Initialize the event entity."""
        snapshot = coordinator.data[listing_id]
        super().__init__(coordinator, entry, snapshot.listing, "reservation")
        self._attr_event_types = [EVENT_BOOKED, EVENT_CANCELED, EVENT_MODIFIED]
        # The first view is the baseline; nothing is reported for it.
        self._previous: ListingSnapshot = snapshot

    @property
    def available(self) -> bool:
        """Return True while the listing is still in the portal."""
        return super().available and self._listing_id in self.coordinator.data

    @callback
    def _handle_coordinator_update(self) -> None:
        """Report what changed since the previous refresh."""
        current = self.coordinator.data.get(self._listing_id)
        if current is None:
            self.async_write_ha_state()
            return
        for event_type, stay in changes(self._previous, current):
            self._trigger_event(event_type, stay_attributes(stay))
            self.async_write_ha_state()
        self._previous = current
        self.async_write_ha_state()
