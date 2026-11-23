"""Tests for inputs the live API has not sent yet but one day might."""

from datetime import UTC, date, datetime, time
import json
import math
import random
from typing import Any
from zoneinfo import ZoneInfo

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

from .conftest import reservation

# A zone with daylight saving: clocks go back on 25 October 2026 and forward
# on 29 March 2026.
BERLIN = ZoneInfo("Europe/Berlin")


def _listing(tz: ZoneInfo = BERLIN) -> Listing:
    listing = Listing.from_api({"_id": "l", "nickname": "DST House"}, tz)
    assert listing is not None
    return listing


def _stay(first: date, last: date, **changes: Any) -> Stay:
    raw = {**reservation("s", first, last), **changes}
    stay = Stay.from_api(raw, _listing(), owner=False)
    assert stay is not None
    return stay


def _snapshot(*stays: Stay) -> ListingSnapshot:
    return ListingSnapshot(
        listing=_listing(),
        stays=merge_stays({}, stays),
        days=(),
        calendar_end=None,
        list_end=None,
        fetched_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def test_stay_across_the_end_of_daylight_saving() -> None:
    """Check-in is in summer time and check-out in winter time."""
    stay = _stay(date(2026, 10, 24), date(2026, 10, 26))

    assert stay.check_in == datetime(2026, 10, 24, 14, 0, tzinfo=UTC)  # 16:00 +02:00
    assert stay.check_out == datetime(2026, 10, 26, 9, 0, tzinfo=UTC)  # 10:00 +01:00


def test_midnight_transition_on_the_25_hour_day() -> None:
    snapshot = _snapshot(_stay(date(2026, 10, 24), date(2026, 10, 30)))

    late_saturday = datetime(2026, 10, 24, 21, 30, tzinfo=UTC)  # 23:30 local
    assert snapshot.next_transition(late_saturday) == datetime(
        2026, 10, 24, 22, 0, tzinfo=UTC
    )
    sunday_noon = datetime(2026, 10, 25, 11, 0, tzinfo=UTC)  # 12:00 local, +01:00
    assert snapshot.local_date(sunday_noon) == date(2026, 10, 25)
    # Sunday is 25 hours long; Monday starts at 23:00 UTC.
    assert snapshot.next_transition(sunday_noon) == datetime(
        2026, 10, 25, 23, 0, tzinfo=UTC
    )


def test_check_in_at_an_hour_that_does_not_exist() -> None:
    """02:30 on the night clocks go forward is skipped; it must still resolve."""
    stay = _stay(date(2026, 3, 29), date(2026, 3, 31), plannedArrival="02:30")

    assert stay.check_in.tzinfo is UTC
    assert stay.check_in < stay.check_out
    assert stay.check_in_date == date(2026, 3, 29)


def test_today_flags_use_the_listing_date_across_the_date_line() -> None:
    listing = Listing.from_api({"_id": "l"}, ZoneInfo("Pacific/Auckland"))
    assert listing is not None
    raw = reservation("s", date(2026, 6, 12), date(2026, 6, 14))
    stay = Stay.from_api(raw, listing, owner=False)
    assert stay is not None
    snapshot = ListingSnapshot(
        listing=listing,
        stays=(stay,),
        days=(),
        calendar_end=None,
        list_end=None,
        fetched_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    # 20:00 UTC on the 11th is already the morning of the 12th in Auckland.
    assert snapshot.arrival_today(datetime(2026, 6, 11, 20, 0, tzinfo=UTC)) == stay


def test_numbers_that_are_not_numbers() -> None:
    """Python's JSON parser accepts NaN and Infinity; sensors must not get them."""
    raw = json.loads(
        '{"infoBox": [{"title": "OWNER_REVENUE", "amount": NaN},'
        ' {"title": "BOOKED_NIGHTS", "amount": Infinity},'
        ' {"title": "OCCUPANCY", "amount": -Infinity}]}'
    )
    metrics = PeriodMetrics.from_api(raw, occupancy=math.nan, booked_nights=math.inf)

    assert metrics.owner_revenue is None
    assert metrics.booked_nights is None
    assert metrics.occupancy is None

    day = CalendarDay.from_api({"date": "2026-06-10", "price": math.nan})
    assert day is not None
    assert day.price is None

    summary = ReviewSummary.from_api({"ratingDistribution": {"nan": 3, "5": math.inf}})
    assert summary is not None
    assert summary.average is None or math.isfinite(summary.average)


def test_dates_at_the_edge_of_the_calendar() -> None:
    """A year-9999 or year-1 date cannot be shifted to UTC; skip, don't crash."""
    far = {
        **reservation("s", date(9999, 12, 30), date(9999, 12, 31)),
        "plannedDeparture": "23:00",  # past the last representable day in UTC
    }
    near = {
        **reservation("s", date(1, 1, 1), date(1, 1, 2)),
        "plannedArrival": "01:00",  # before the first representable day in UTC
    }
    west = Listing.from_api({"_id": "l"}, ZoneInfo("America/Phoenix"))
    east = Listing.from_api({"_id": "l"}, ZoneInfo("Asia/Tokyo"))
    assert west is not None
    assert east is not None

    assert Stay.from_api(far, west, owner=False) is None
    assert Stay.from_api(near, east, owner=False) is None


_JUNK: tuple[Any, ...] = (
    None,
    True,
    False,
    0,
    -1,
    3.5,
    10**30,
    math.nan,
    math.inf,
    "",
    " ",
    "x",
    "confirmed",
    "reserved",
    "2026-06-10",
    "2026-13-45",
    "9999-12-31",
    "0001-01-01",
    "2026-06-10T25:61:00Z",
    "2026-06-10T16:00:00+00:00",
    "16:00",
    "25:99",
    "16:00+02:00",
    [],
    {},
    [None],
    ["x", 1],
    {"_id": 5},
    {"fullName": 7, "full": []},
    {"b": True, "o": "yes"},
    {"reservation": {"_id": "r", "status": "confirmed"}},
    {"results": [None, {"createdAt": 4}]},
    {"1": "many", "5": 2, "x": None},
)

_KEYS = (
    "_id",
    "status",
    "source",
    "checkIn",
    "checkOut",
    "checkInDateLocalized",
    "checkOutDateLocalized",
    "plannedArrival",
    "plannedDeparture",
    "nightsCount",
    "guestsCount",
    "numberOfGuests",
    "guest",
    "money",
    "createdAt",
    "ownerId",
    "date",
    "price",
    "currency",
    "minNights",
    "blocks",
    "blockRefs",
    "nickname",
    "title",
    "address",
    "timezone",
    "defaultCheckInTime",
    "defaultCheckOutTime",
    "bedrooms",
    "propertyType",
    "active",
    "infoBox",
    "ratingDistribution",
    "reviews",
    "periodStartDate",
    "periodEndDate",
    "dueToOwner",
    "endingOwnerBalance",
    "paidToOwner",
    "payoutStatus",
    "type",
    "reservation",
    "amount",
)


def _junk_object(rng: random.Random, depth: int = 0) -> dict[str, Any]:
    made: dict[str, Any] = {}
    for key in rng.sample(_KEYS, rng.randint(0, len(_KEYS))):
        roll = rng.random()
        if roll < 0.15 and depth < 2:
            made[key] = _junk_object(rng, depth + 1)
        elif roll < 0.25 and depth < 2:
            made[key] = [_junk_object(rng, depth + 1) for _ in range(rng.randint(0, 3))]
        else:
            made[key] = rng.choice(_JUNK)
    return made


def test_no_parser_crashes_on_malformed_data() -> None:
    """Whatever shape comes back, parsing returns a value or None."""
    rng = random.Random(20260610)
    listing = _listing()
    now = datetime(2026, 6, 10, 19, 0, tzinfo=UTC)

    for _ in range(300):
        raw = _junk_object(rng)
        days = [_junk_object(rng) for _ in range(rng.randint(0, 4))]

        Listing.from_api(raw, BERLIN)
        Statement.from_api(raw)
        PeriodMetrics.from_api(raw, occupancy=rng.choice((None, 0.5, math.nan)))
        ReviewSummary.from_api(raw)
        calendar_names_every_booking(days)
        parsed_days = tuple(
            day for item in days if (day := CalendarDay.from_api(item)) is not None
        )
        stay = Stay.from_api(raw, listing, owner=rng.random() < 0.5)
        from_calendar = stays_from_calendar(days, listing)

        snapshot = ListingSnapshot(
            listing=listing,
            stays=merge_stays(
                from_calendar,
                [stay] if stay else [],
                authoritative_until=rng.choice((None, date(2026, 9, 8))),
            ),
            days=parsed_days,
            calendar_end=None,
            list_end=None,
            fetched_at=now,
        )
        # Everything the entities ask of a snapshot must work on it too.
        snapshot.current_stay(now)
        snapshot.next_stay(now)
        snapshot.arrival_today(now)
        snapshot.departure_today(now)
        snapshot.tonight(now)
        snapshot.booked_nights(now, 30)
        assert snapshot.next_transition(now) > now
        for item in snapshot.stays:
            assert item.check_in < item.check_out
            assert item.check_in.tzinfo is UTC
            assert isinstance(item.check_in_date, date)
        for day in parsed_days:
            assert day.price is None or math.isfinite(day.price)


def test_default_times_survive_junk() -> None:
    listing = Listing.from_api(
        {"_id": "l", "defaultCheckInTime": "25:99", "defaultCheckOutTime": 7}, UTC
    )
    assert listing is not None
    assert listing.check_in_time == time(16, 0)
    assert listing.check_out_time == time(10, 0)
