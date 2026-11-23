"""Tests for the response models."""

from datetime import UTC, date, datetime, time
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
    source_label,
    stays_from_calendar,
)

from .conftest import LISTING_ID, STAY_CURRENT, STAY_OWNER, listed, reservation

TZ = ZoneInfo("America/Phoenix")
LISTING = Listing.from_api(
    {
        "_id": LISTING_ID,
        "nickname": "Harbor Loft",
        "timezone": "America/Phoenix",
        "defaultCheckInTime": "15:00",
        "defaultCheckOutTime": "11:00",
    },
    TZ,
)
assert LISTING is not None


def test_listing_defaults() -> None:
    listing = Listing.from_api({"_id": "x", "title": "Only a title"}, UTC)
    assert listing is not None
    assert listing.name == "Only a title"
    assert listing.check_in_time == time(16, 0)
    assert listing.active is True
    assert Listing.from_api({"nickname": "No id"}, UTC) is None


def test_stay_times_come_from_local_date_and_planned_time() -> None:
    """The calendar's ``checkIn`` is local time labelled UTC and must be ignored."""
    raw = reservation("s1", date(2026, 6, 8), date(2026, 6, 12))
    assert raw["checkIn"] == "2026-06-08T16:00:00+00:00"

    stay = Stay.from_api(raw, LISTING, owner=False)

    assert stay is not None
    # 16:00 at the listing (UTC-7) is 23:00 UTC, not 16:00 UTC.
    assert stay.check_in == datetime(2026, 6, 8, 23, 0, tzinfo=UTC)
    assert stay.check_out == datetime(2026, 6, 12, 17, 0, tzinfo=UTC)
    assert stay.nights == 4
    assert stay.guest_name == "Alex Example"
    assert stay.host_payout == 500.0
    assert stay.created_at == datetime(2026, 4, 1, 12, 0, tzinfo=UTC)
    assert stay.owner is False
    assert stay.confirmed


def test_owner_stay_uses_listing_default_times() -> None:
    raw = reservation(
        "s2", date(2026, 6, 20), date(2026, 6, 22), source="owner-guest", owner=True
    )
    stay = Stay.from_api(raw, LISTING, owner=False)

    assert stay is not None
    assert stay.owner is True  # recognised from the source alone
    assert stay.check_in == datetime(2026, 6, 20, 22, 0, tzinfo=UTC)  # 15:00 local
    assert stay.check_out == datetime(2026, 6, 22, 18, 0, tzinfo=UTC)  # 11:00 local


def test_stay_falls_back_to_real_instants() -> None:
    stay = Stay.from_api(
        {
            "_id": "s3",
            "status": "confirmed",
            "checkIn": "2026-03-10T23:00:00.000Z",
            "checkOut": "2026-03-12T17:00:00.000Z",
        },
        LISTING,
        owner=False,
    )
    assert stay is not None
    assert stay.check_in_date == date(2026, 3, 10)
    assert stay.check_out_date == date(2026, 3, 12)


def test_stay_rejects_what_is_not_a_stay() -> None:
    good = reservation("s4", date(2026, 6, 8), date(2026, 6, 12))
    assert Stay.from_api({**good, "status": "canceled"}, LISTING, owner=False) is None
    assert Stay.from_api({**good, "status": "inquiry"}, LISTING, owner=False) is None
    assert Stay.from_api({**good, "_id": None}, LISTING, owner=False) is None
    assert (
        Stay.from_api({"_id": "s", "status": "confirmed"}, LISTING, owner=False) is None
    )
    backwards = reservation("s5", date(2026, 6, 12), date(2026, 6, 8))
    assert Stay.from_api(backwards, LISTING, owner=False) is None


def test_reserved_is_a_stay_but_not_confirmed() -> None:
    raw = reservation("s6", date(2026, 6, 8), date(2026, 6, 12), status="reserved")
    stay = Stay.from_api(raw, LISTING, owner=False)
    assert stay is not None
    assert not stay.confirmed


def test_calendar_and_list_views_merge() -> None:
    guest = reservation(STAY_CURRENT, date(2026, 6, 8), date(2026, 6, 10))
    owner = reservation(STAY_OWNER, date(2026, 6, 20), date(2026, 6, 22), owner=True)
    days = [
        {"blockRefs": [{"type": "b", "reservation": guest}]},
        {"blockRefs": [{"type": "b", "reservation": guest}]},
        # The preparation day after checkout points at the same reservation.
        {"blockRefs": [{"type": "pt", "reservation": guest}]},
        {"blockRefs": [{"type": "o", "reservation": owner}]},
        {"blockRefs": "garbage"},
        {"blockRefs": [None, {"type": "m"}]},
    ]

    from_calendar = stays_from_calendar(days, LISTING)
    assert set(from_calendar) == {STAY_CURRENT, STAY_OWNER}
    assert from_calendar[STAY_OWNER].owner
    assert from_calendar[STAY_CURRENT].pets is None

    from_list = Stay.from_api(listed(guest), LISTING, owner=False)
    assert from_list is not None
    merged = merge_stays(from_calendar, [from_list])

    assert [stay.id for stay in merged] == [STAY_CURRENT, STAY_OWNER]
    # The list knows about pets, the calendar about money; merging keeps both.
    assert merged[0].pets == 1
    assert merged[0].host_payout == 500.0


def test_merge_drops_listed_stays_the_calendar_does_not_have() -> None:
    def stay(stay_id: str, first: date, last: date) -> Stay:
        made = Stay.from_api(reservation(stay_id, first, last), LISTING, owner=False)
        assert made is not None
        return made

    on_calendar = stay("real", date(2026, 6, 8), date(2026, 6, 10))
    canceled = stay("canceled", date(2026, 6, 9), date(2026, 6, 11))
    far = stay("far", date(2026, 12, 1), date(2026, 12, 4))
    listed_stays = [on_calendar, canceled, far]

    trusting = merge_stays({"real": on_calendar}, listed_stays)
    assert [item.id for item in trusting] == ["real", "canceled", "far"]

    checked = merge_stays(
        {"real": on_calendar}, listed_stays, authoritative_until=date(2026, 9, 8)
    )
    assert [item.id for item in checked] == ["real", "far"]


def test_calendar_names_every_booking() -> None:
    guest = reservation(STAY_CURRENT, date(2026, 6, 8), date(2026, 6, 10))
    named = {"blocks": {"b": True}, "blockRefs": [{"type": "b", "reservation": guest}]}
    free = {"blocks": {"b": False, "o": False}, "blockRefs": []}
    manual = {"blocks": {"m": True}, "blockRefs": [{"type": "m"}]}
    anonymous = {"blocks": {"o": True}, "blockRefs": [{"type": "o"}]}

    assert calendar_names_every_booking([named, free, manual])
    assert calendar_names_every_booking([])
    assert not calendar_names_every_booking([named, anonymous])
    assert not calendar_names_every_booking([{"blocks": {"b": True}}])


def _snapshot(*stays: Stay, days: tuple[CalendarDay, ...] = ()) -> ListingSnapshot:
    return ListingSnapshot(
        listing=LISTING,
        stays=merge_stays({}, stays),
        days=days,
        calendar_end=date(2026, 9, 8),
        list_end=date(2027, 6, 10),
        fetched_at=datetime(2026, 6, 10, 19, 0, tzinfo=UTC),
    )


def _stay(stay_id: str, first: date, last: date, **kwargs) -> Stay:
    stay = Stay.from_api(
        reservation(stay_id, first, last, **kwargs), LISTING, owner=False
    )
    assert stay is not None
    return stay


def test_snapshot_views() -> None:
    current = _stay("a", date(2026, 6, 8), date(2026, 6, 12))
    following = _stay("b", date(2026, 6, 12), date(2026, 6, 15))
    hold = _stay("c", date(2026, 6, 16), date(2026, 6, 18), status="reserved")
    snapshot = _snapshot(following, hold, current)
    noon = datetime(2026, 6, 10, 19, 0, tzinfo=UTC)

    assert snapshot.current_stay(noon) == current
    assert snapshot.next_stay(noon) == following
    assert snapshot.upcoming_stays(noon) == (following,)
    assert snapshot.arrival_today(noon) is None
    assert snapshot.departure_today(noon) is None
    # Next thing to happen is local midnight, before Friday's checkout.
    assert snapshot.next_transition(noon) == datetime(2026, 6, 11, 7, 0, tzinfo=UTC)

    turnover = datetime(2026, 6, 12, 19, 0, tzinfo=UTC)  # between the two stays
    assert snapshot.current_stay(turnover) is None
    assert snapshot.departure_today(turnover) == current
    assert snapshot.arrival_today(turnover) == following
    assert snapshot.next_transition(turnover) == following.check_in


def test_today_is_the_listing_date_not_utc() -> None:
    snapshot = _snapshot(_stay("a", date(2026, 6, 12), date(2026, 6, 15)))
    # 03:00 UTC on the 13th is still the evening of the 12th at the listing.
    evening = datetime(2026, 6, 13, 3, 0, tzinfo=UTC)
    assert snapshot.local_date(evening) == date(2026, 6, 12)
    assert snapshot.arrival_today(evening) is not None


def test_calendar_day_and_booked_nights() -> None:
    days = tuple(
        day
        for raw in (
            {
                "date": "2026-06-10",
                "status": "booked",
                "price": 160,
                "currency": "USD",
                "minNights": 2,
                "blocks": {"b": True, "o": False},
            },
            {"date": "2026-06-11", "status": "booked"},
            {"date": "2026-06-12", "status": "unavailable", "blocks": {"o": True}},
            {"date": "2026-07-20", "status": "booked"},
            {"date": "nonsense"},
        )
        if (day := CalendarDay.from_api(raw)) is not None
    )
    snapshot = _snapshot(days=days)
    noon = datetime(2026, 6, 10, 19, 0, tzinfo=UTC)

    tonight = snapshot.tonight(noon)
    assert tonight is not None
    assert (tonight.price, tonight.min_nights, tonight.blocks) == (160.0, 2, ("b",))
    assert snapshot.booked_nights(noon, 30) == 2
    assert _snapshot().booked_nights(noon, 30) is None
    assert _snapshot().tonight(noon) is None


def test_period_metrics_tolerate_hidden_tiles() -> None:
    metrics = PeriodMetrics.from_api(
        {
            "infoBox": [
                {"title": "OWNER_REVENUE", "amount": 1250.5},
                {"title": "NET_RENTAL_INCOME", "amount": "N/A"},
                {"title": "BOOKED_NIGHTS", "amount": 12},
                "junk",
            ],
            "currency": "USD",
        }
    )
    assert metrics.owner_revenue == 1250.5
    assert metrics.net_rental_income is None
    assert metrics.booked_nights == 12
    assert metrics.occupancy is None
    assert PeriodMetrics.from_api({}).owner_revenue is None

    # Figures from the Analytics page's own service win over the tiles.
    preferred = PeriodMetrics.from_api(
        {"infoBox": [{"title": "OCCUPANCY", "amount": 31.5}]},
        occupancy=0.35,
        booked_nights=64.0,
    )
    assert preferred.occupancy == 35.0
    assert preferred.booked_nights == 64


def test_review_summary() -> None:
    summary = ReviewSummary.from_api(
        {
            "ratingDistribution": {"3": 1, "4": 5, "5": 14, "x": 9},
            "reviews": {"results": [{"createdAt": "2026-05-20T09:30:00.250Z"}]},
        }
    )
    assert summary is not None
    assert summary.count == 20
    assert summary.average == 4.65
    assert summary.latest == datetime(2026, 5, 20, 9, 30, 0, 250000, tzinfo=UTC)

    empty = ReviewSummary.from_api({"ratingDistribution": {}})
    assert empty is not None
    assert (empty.count, empty.average, empty.latest) == (0, None, None)
    assert ReviewSummary.from_api({}) is None


def test_statement() -> None:
    statement = Statement.from_api(
        {
            "_id": "s",
            "periodEndDate": "2026-05-31T00:00:00.000Z",
            "dueToOwner": 1500.25,
            "payoutStatus": "NOT_PAID",
        }
    )
    assert statement is not None
    assert statement.period_end == date(2026, 5, 31)
    assert statement.payout_status == "not_paid"
    assert statement.ending_balance is None
    assert Statement.from_api({}) is None


def test_source_label() -> None:
    assert source_label("airbnb2") == "Airbnb"
    assert source_label("VRBO") == "Vrbo"
    assert source_label("Some Channel") == "Some Channel"
    assert source_label(None) is None
