"""Reservation calendar for the Guesty Owners Portal integration."""

from datetime import datetime

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .coordinator import GuestyOwnersConfigEntry, StaysCoordinator
from .entity import StayEntity, async_add_listing_entities
from .models import Listing, Stay

# Every entity reads from a coordinator; none of them polls on its own.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: GuestyOwnersConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the reservation calendars."""
    stays = entry.runtime_data.stays
    async_add_listing_entities(
        entry,
        async_add_entities,
        lambda listing_id: [ReservationCalendar(stays, entry, listing_id)],
    )


def _event(stay: Stay, listing: Listing) -> CalendarEvent:
    """Turn a stay into a calendar event that spans check-in to check-out."""
    who = stay.guest_name or ("Owner stay" if stay.owner else "Guest stay")
    if stay.owner and stay.guest_name:
        who = f"Owner stay: {stay.guest_name}"
    if not stay.confirmed:
        who = f"{who} (unconfirmed)"
    details = [
        f"Source: {stay.source_label}" if stay.source_label else None,
        f"Nights: {stay.nights}" if stay.nights is not None else None,
        f"Guests: {stay.guests}" if stay.guests is not None else None,
        f"Pets: {stay.pets}" if stay.pets else None,
        f"Status: {stay.status}",
    ]
    return CalendarEvent(
        start=stay.check_in.astimezone(listing.tz),
        end=stay.check_out.astimezone(listing.tz),
        summary=who,
        description="\n".join(line for line in details if line),
        location=listing.address,
        uid=stay.id,
    )


class ReservationCalendar(StayEntity, CalendarEntity):
    """Guest and owner stays of one listing."""

    _attr_translation_key = "reservations"

    def __init__(
        self,
        coordinator: StaysCoordinator,
        entry: GuestyOwnersConfigEntry,
        listing_id: str,
    ) -> None:
        """Initialize the calendar."""
        listing = coordinator.data[listing_id].listing
        super().__init__(coordinator, entry, listing, "reservations")

    @property
    def event(self) -> CalendarEvent | None:
        """Return the stay in progress, or else the next one."""
        if (snapshot := self.snapshot) is None:
            return None
        now = dt_util.utcnow()
        for stay in snapshot.stays:
            if stay.check_out > now:
                return _event(stay, snapshot.listing)
        return None

    async def async_get_events(
        self, hass: HomeAssistant, start_date: datetime, end_date: datetime
    ) -> list[CalendarEvent]:
        """Return the stays that overlap a time range."""
        if (snapshot := self.snapshot) is None:
            return []
        return [
            _event(stay, snapshot.listing)
            for stay in snapshot.stays
            if stay.check_in < end_date and stay.check_out > start_date
        ]
