"""Check the API client against a real owners portal, from the command line.

Run it from the repository root, inside the development virtualenv:

    python -m scripts.smoke_test acme.guestyowners.com you@example.com

The password is asked for and never printed or stored. Only GET requests are
made after the login. Guest names and amounts are left out of the output.
"""

import argparse
import asyncio
from datetime import UTC, datetime, timedelta
from getpass import getpass
import sys
from zoneinfo import ZoneInfo

import aiohttp

from custom_components.guesty_owners.api import (
    GuestyOwnersApiError,
    GuestyOwnersClient,
    GuestyOwnersError,
    normalize_hostname,
)
from custom_components.guesty_owners.models import (
    CalendarDay,
    Listing,
    ListingSnapshot,
    PeriodMetrics,
    ReviewSummary,
    Statement,
    Stay,
    calendar_names_every_booking,
    merge_stays,
    stays_from_calendar,
)


async def optional(request, what: str):
    """Await a request that a portal may not offer."""
    try:
        return await request
    except GuestyOwnersApiError as err:
        print(f"    {what}: not available ({err.status})")
        return None


async def check_listing(client: GuestyOwnersClient, owner_id: str, raw: dict) -> None:
    """Print what the integration would know about one listing."""
    tz = ZoneInfo(raw.get("timezone") or "UTC")
    listing = Listing.from_api(raw, tz)
    if listing is None:
        print("  a listing without an ID was skipped")
        return
    now = datetime.now(UTC)
    today = now.astimezone(tz).date()
    start, end = today - timedelta(days=2), today + timedelta(days=90)
    print(f"  {listing.name} ({listing.timezone})")

    raw_days = await optional(
        client.async_get_calendar(listing.id, owner_id, start, end), "calendar"
    )
    listed = []
    for owner in (False, True):
        kind = "owner" if owner else "guest"
        reservations = await optional(
            client.async_get_reservations(
                listing.id, start, today + timedelta(days=365), owner=owner
            ),
            f"{kind} reservations",
        )
        print(f"    {kind} reservations listed: {len(reservations or [])}")
        listed += [
            stay
            for item in reservations or []
            if (stay := Stay.from_api(item, listing, owner=owner))
        ]
    from_calendar = stays_from_calendar(raw_days or [], listing)
    listed_ids = {stay.id for stay in listed}
    trusted = raw_days is not None and calendar_names_every_booking(raw_days)
    stays = merge_stays(
        from_calendar, listed, authoritative_until=end if trusted else None
    )
    kept = {stay.id for stay in stays}
    for stay in listed:
        if stay.id not in kept:
            print(
                f"    dropped as canceled (listed but not on the calendar): "
                f"{stay.check_in_date} to {stay.check_out_date}"
            )
    snapshot = ListingSnapshot(
        listing=listing,
        stays=stays,
        days=tuple(
            day for item in raw_days or [] if (day := CalendarDay.from_api(item))
        ),
        calendar_end=end if raw_days is not None else None,
        list_end=today + timedelta(days=365),
        fetched_at=now,
    )
    print(f"    calendar days: {len(snapshot.days)}, stays: {len(snapshot.stays)}")
    for stay in snapshot.stays:
        seen = [
            name
            for name, known in (
                ("calendar", stay.id in from_calendar),
                ("list", stay.id in listed_ids),
            )
            if known
        ]
        created = f"{stay.created_at:%Y-%m-%d}" if stay.created_at else "unknown"
        print(
            f"      {stay.check_in_date} to {stay.check_out_date}  "
            f"{'owner' if stay.owner else 'guest':5}  {stay.status:10}  "
            f"{stay.source_label or '-':12}  created {created}  "
            f"seen in: {' + '.join(seen)}"
        )
    current, upcoming = snapshot.current_stay(now), snapshot.next_stay(now)
    print(f"    occupied now: {current is not None}")
    if upcoming:
        print(
            f"    next check-in: {upcoming.check_in.astimezone(tz):%Y-%m-%d %H:%M} "
            f"for {upcoming.nights} nights via {upcoming.source_label}"
        )
    if tonight := snapshot.tonight(now):
        print(f"    tonight: {tonight.status}, minimum {tonight.min_nights} nights")

    # The same two periods the integration asks for: this month and this year.
    month_start = today.replace(day=1)
    month_end = (month_start + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    periods = {
        "this month": (month_start, month_end),
        "this year": (today.replace(month=1, day=1), today.replace(month=12, day=31)),
    }
    for label, (first, last) in periods.items():
        period = (listing.id, owner_id, first, last)
        tiles = await optional(client.async_get_analytics(*period), "revenue tiles")
        occupancy = await optional(client.async_get_occupancy(*period), "occupancy")
        nights = await optional(
            client.async_get_booked_nights(*period), "booked nights"
        )
        metrics = PeriodMetrics.from_api(
            tiles or {}, occupancy=occupancy, booked_nights=nights
        )
        readable = [
            name
            for name in ("owner_revenue", "net_rental_income")
            if getattr(metrics, name) is not None
        ]
        print(
            f"    {label}: occupancy {metrics.occupancy}%, "
            f"{metrics.booked_nights} booked nights, "
            f"money figures readable: {', '.join(readable) or 'none'}"
        )
    reviews = await optional(client.async_get_reviews(listing.id), "reviews")
    if reviews is not None and (summary := ReviewSummary.from_api(reviews)):
        print(f"    reviews: {summary.count}, average {summary.average}")


async def main() -> int:
    """Log in and walk through every endpoint the integration uses."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("portal", help="portal address, e.g. acme.guestyowners.com")
    parser.add_argument("email", help="the email you sign in to the portal with")
    args = parser.parse_args()

    hostname = normalize_hostname(args.portal)
    password = getpass(f"Password for {args.email} at {hostname}: ")

    async with aiohttp.ClientSession() as session:
        client = GuestyOwnersClient(session, hostname, args.email, password)
        try:
            meta = await client.async_get_portal_meta()
            print(f"Portal found: {meta.get('account', {}).get('name')}")
            await client.async_login()
            print("Login: OK")
            owner = await client.async_get_owner()
            owner_id = owner["_id"]
            listings = await client.async_get_listings()
            print(f"Listings: {len(listings)}")
            for raw in listings:
                await check_listing(client, owner_id, raw)

            statement = await optional(
                client.async_get_latest_statement(), "statements"
            )
            if statement and (parsed := Statement.from_api(statement)):
                print(f"Latest statement: period ending {parsed.period_end}")
            unread = await optional(client.async_get_unread_count(), "inbox")
            if unread is not None:
                print(f"Unread messages: {unread}")
        except GuestyOwnersError as err:
            print(f"FAILED: {type(err).__name__}: {err}")
            return 1
    print("All done.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
