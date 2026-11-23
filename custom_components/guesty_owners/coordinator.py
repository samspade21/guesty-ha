"""Data update coordinators for the Guesty Owners Portal integration."""

import asyncio
from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import date, timedelta, tzinfo
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    GuestyOwnersApiError,
    GuestyOwnersAuthError,
    GuestyOwnersClient,
    GuestyOwnersError,
    GuestyOwnersRateLimitError,
)
from .const import (
    CONF_DAYS_AHEAD,
    DAYS_BEHIND,
    DEFAULT_DAYS_AHEAD,
    DOMAIN,
    RESERVATION_LIST_DAYS,
    STATS_UPDATE_INTERVAL,
    STAYS_UPDATE_INTERVAL,
)
from .models import (
    CalendarDay,
    Listing,
    ListingSnapshot,
    ListingStats,
    PeriodMetrics,
    ReviewSummary,
    Statement,
    StatsData,
    Stay,
    calendar_names_every_booking,
    merge_stays,
    stays_from_calendar,
)

_LOGGER = logging.getLogger(__name__)

# Listings are refreshed side by side, but not so many at once that a large
# portfolio looks like a burst of traffic.
MAX_PARALLEL_LISTINGS = 3


@dataclass(slots=True)
class GuestyOwnersRuntimeData:
    """Objects shared by the platforms of one config entry."""

    client: GuestyOwnersClient
    stays: StaysCoordinator
    stats: StatsCoordinator


type GuestyOwnersConfigEntry = ConfigEntry[GuestyOwnersRuntimeData]


def _update_failed(err: GuestyOwnersError) -> Exception:
    """Translate a client error into what the coordinator should raise."""
    if isinstance(err, GuestyOwnersAuthError):
        return ConfigEntryAuthFailed(
            translation_domain=DOMAIN, translation_key="auth_failed"
        )
    return UpdateFailed(
        translation_domain=DOMAIN,
        translation_key="update_failed",
        translation_placeholders={"error": str(err)},
        retry_after=getattr(err, "retry_after", None),
    )


async def _optional[T](request: Awaitable[T], what: str) -> T | None:
    """Await a request whose data the manager may have hidden from owners.

    Portals differ in what they expose, and the web app itself gets 400s and
    404s for sections that are switched off. Those answer with None; anything
    that means the whole update is in trouble still propagates.
    """
    try:
        return await request
    except GuestyOwnersAuthError, GuestyOwnersRateLimitError:
        raise
    except GuestyOwnersApiError as err:
        _LOGGER.debug("Guesty did not return %s: %s", what, err)
        return None


class StaysCoordinator(DataUpdateCoordinator[dict[str, ListingSnapshot]]):
    """Fetch listings with their reservations and calendar."""

    config_entry: GuestyOwnersConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: GuestyOwnersConfigEntry,
        client: GuestyOwnersClient,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=f"{DOMAIN} stays",
            update_interval=STAYS_UPDATE_INTERVAL,
            always_update=False,
        )
        self.client = client
        self.owner: dict[str, Any] = {}
        self._days_ahead: int = config_entry.options.get(
            CONF_DAYS_AHEAD, DEFAULT_DAYS_AHEAD
        )

    @property
    def owner_id(self) -> str:
        """Return the owner ID, which is also the config entry's unique ID."""
        return str(self.owner.get("_id") or self.config_entry.unique_id)

    async def _async_update_data(self) -> dict[str, ListingSnapshot]:
        """Fetch every listing's stays."""
        try:
            if not self.owner:
                self.owner = await self.client.async_get_owner()
            raw_listings = await self.client.async_get_listings()

            limiter = asyncio.Semaphore(MAX_PARALLEL_LISTINGS)

            async def fetch(raw: dict[str, Any]) -> ListingSnapshot | None:
                async with limiter:
                    return await self._async_fetch_listing(raw)

            snapshots = await asyncio.gather(*(fetch(raw) for raw in raw_listings))
        except GuestyOwnersError as err:
            raise _update_failed(err) from err
        return {
            snapshot.listing.id: snapshot
            for snapshot in snapshots
            if snapshot is not None
        }

    async def _async_resolve_timezone(self, name: Any) -> tzinfo:
        """Return a listing's timezone, falling back to Home Assistant's."""
        if isinstance(name, str) and (tz := await dt_util.async_get_time_zone(name)):
            return tz
        return dt_util.get_default_time_zone()

    async def _async_fetch_listing(self, raw: dict[str, Any]) -> ListingSnapshot | None:
        """Fetch the calendar and reservations of one listing."""
        tz = await self._async_resolve_timezone(raw.get("timezone"))
        if (listing := Listing.from_api(raw, tz)) is None:
            return None

        now = dt_util.utcnow()
        today = now.astimezone(tz).date()
        start = today - timedelta(days=DAYS_BEHIND)
        calendar_end = today + timedelta(days=self._days_ahead)
        list_end = today + timedelta(days=RESERVATION_LIST_DAYS)

        raw_days = await _optional(
            self.client.async_get_calendar(
                listing.id, self.owner_id, start, calendar_end
            ),
            "the calendar",
        )
        guest_list = await _optional(
            self.client.async_get_reservations(
                listing.id, start, list_end, owner=False
            ),
            "guest reservations",
        )
        owner_list = await _optional(
            self.client.async_get_reservations(listing.id, start, list_end, owner=True),
            "owner reservations",
        )
        if raw_days is None and guest_list is None and owner_list is None:
            raise GuestyOwnersApiError(
                f"Neither the calendar nor the reservations of {listing.name} "
                "could be read"
            )

        listed = [
            stay
            for reservations, owner in ((guest_list, False), (owner_list, True))
            for reservation in reservations or []
            if (stay := Stay.from_api(reservation, listing, owner=owner)) is not None
        ]

        days = tuple(
            day
            for raw_day in raw_days or []
            if (day := CalendarDay.from_api(raw_day)) is not None
        )
        return ListingSnapshot(
            listing=listing,
            stays=merge_stays(
                stays_from_calendar(raw_days or [], listing),
                listed,
                authoritative_until=(
                    calendar_end
                    if raw_days is not None and calendar_names_every_booking(raw_days)
                    else None
                ),
            ),
            days=days,
            calendar_end=calendar_end if raw_days is not None else None,
            list_end=list_end if None not in (guest_list, owner_list) else None,
            fetched_at=now,
        )


class StatsCoordinator(DataUpdateCoordinator[StatsData]):
    """Fetch revenue figures, reviews, statements and the inbox count."""

    config_entry: GuestyOwnersConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: GuestyOwnersConfigEntry,
        client: GuestyOwnersClient,
        stays: StaysCoordinator,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=f"{DOMAIN} statistics",
            update_interval=STATS_UPDATE_INTERVAL,
            always_update=False,
        )
        self.client = client
        self._stays = stays

    async def _async_update_data(self) -> StatsData:
        """Fetch the slow-changing figures."""
        listings = [snapshot.listing for snapshot in (self._stays.data or {}).values()]
        try:
            per_listing = {
                listing.id: await self._async_fetch_listing(listing)
                for listing in listings
            }
            raw_statement = await _optional(
                self.client.async_get_latest_statement(), "the latest statement"
            )
            unread = await _optional(
                self.client.async_get_unread_count(), "the inbox count"
            )
        except GuestyOwnersError as err:
            raise _update_failed(err) from err
        return StatsData(
            listings=per_listing,
            statement=Statement.from_api(raw_statement) if raw_statement else None,
            unread_messages=unread,
        )

    async def _async_fetch_metrics(
        self, listing: Listing, start: date, end: date, what: str
    ) -> PeriodMetrics | None:
        """Fetch the revenue tiles for one period."""
        period = (listing.id, self._stays.owner_id, start, end)
        raw = await _optional(self.client.async_get_analytics(*period), what)
        occupancy = await _optional(
            self.client.async_get_occupancy(*period), f"occupancy for {what}"
        )
        booked_nights = await _optional(
            self.client.async_get_booked_nights(*period), f"booked nights for {what}"
        )
        if raw is None and occupancy is None and booked_nights is None:
            return None
        return PeriodMetrics.from_api(
            raw or {}, occupancy=occupancy, booked_nights=booked_nights
        )

    async def _async_fetch_listing(self, listing: Listing) -> ListingStats:
        """Fetch the figures of one listing."""
        today = dt_util.utcnow().astimezone(listing.tz).date()
        month_start = today.replace(day=1)
        next_month = (month_start + timedelta(days=32)).replace(day=1)
        reviews = await _optional(
            self.client.async_get_reviews(listing.id), "guest reviews"
        )
        return ListingStats(
            month=await self._async_fetch_metrics(
                listing,
                month_start,
                next_month - timedelta(days=1),
                "this month's figures",
            ),
            year=await self._async_fetch_metrics(
                listing,
                today.replace(month=1, day=1),
                today.replace(month=12, day=31),
                "this year's figures",
            ),
            reviews=ReviewSummary.from_api(reviews) if reviews is not None else None,
        )
