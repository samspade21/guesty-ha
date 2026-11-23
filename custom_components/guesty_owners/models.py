"""Typed views of the Guesty Owners Portal API responses.

Everything here is plain Python with no Home Assistant imports. The dataclasses
are frozen so the coordinators can compare old and new data cheaply.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, fields, replace
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from typing import Any, Final

# Reservation statuses that mean somebody is, or will be, staying.
CONFIRMED_STATUSES: Final = frozenset({"confirmed", "checked_in", "checked_out"})
# A hold that has not been paid for or accepted yet.
TENTATIVE_STATUSES: Final = frozenset({"reserved", "awaiting_payment"})

AVAILABILITY_STATES: Final = ("available", "booked", "unavailable")

DEFAULT_CHECK_IN: Final = time(16, 0)
DEFAULT_CHECK_OUT: Final = time(10, 0)

_SOURCE_LABELS: Final = {
    "airbnb": "Airbnb",
    "airbnb2": "Airbnb",
    "bookingcom": "Booking.com",
    "booking.com": "Booking.com",
    "homeaway": "Vrbo",
    "homeaway2": "Vrbo",
    "vrbo": "Vrbo",
    "manual": "Direct",
    "owner": "Owner",
    "owner-guest": "Owner guest",
}


def _as_str(value: Any) -> str | None:
    """Return a non-empty string, or None."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _as_float(value: Any) -> float | None:
    """Return a number as float; the API uses ``"N/A"`` and null for hidden values."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _as_int(value: Any) -> int | None:
    """Return a whole number, or None."""
    number = _as_float(value)
    return None if number is None else int(number)


def _as_date(value: Any) -> date | None:
    """Parse the date part of an ISO date or datetime string."""
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _as_instant(value: Any) -> datetime | None:
    """Parse an ISO datetime into an aware UTC datetime."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _as_time(value: Any, default: time) -> time:
    """Parse ``HH:MM``; owner reservations often carry an empty string."""
    if isinstance(value, str):
        try:
            return time.fromisoformat(value.strip())
        except ValueError:
            pass
    return default


def _as_dict(value: Any) -> Mapping[str, Any]:
    """Return a mapping, or an empty one."""
    return value if isinstance(value, Mapping) else {}


def source_label(source: str | None) -> str | None:
    """Return a display name for a booking source."""
    if source is None:
        return None
    return _SOURCE_LABELS.get(source.lower(), source)


@dataclass(frozen=True, slots=True, kw_only=True)
class Listing:
    """A property the owner can see."""

    id: str
    name: str
    title: str | None
    address: str | None
    timezone: str | None
    check_in_time: time
    check_out_time: time
    bedrooms: int | None
    property_type: str | None
    active: bool
    # Resolved by the coordinator; not part of equality so data stays comparable.
    tz: tzinfo = field(default=UTC, compare=False, repr=False)

    @classmethod
    def from_api(cls, raw: Mapping[str, Any], tz: tzinfo) -> Listing | None:
        """Build a listing from ``/owners/me/listings``."""
        if (listing_id := _as_str(raw.get("_id"))) is None:
            return None
        title = _as_str(raw.get("title"))
        return cls(
            id=listing_id,
            name=_as_str(raw.get("nickname")) or title or listing_id,
            title=title,
            address=_as_str(_as_dict(raw.get("address")).get("full")),
            timezone=_as_str(raw.get("timezone")),
            check_in_time=_as_time(raw.get("defaultCheckInTime"), DEFAULT_CHECK_IN),
            check_out_time=_as_time(raw.get("defaultCheckOutTime"), DEFAULT_CHECK_OUT),
            bedrooms=_as_int(raw.get("bedrooms")),
            property_type=_as_str(raw.get("propertyType")),
            active=raw.get("active") is not False,
            tz=tz,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class Stay:
    """One guest or owner reservation."""

    id: str
    listing_id: str
    owner: bool
    status: str
    source: str | None
    confirmation_code: str | None
    check_in: datetime
    check_out: datetime
    check_in_date: date
    check_out_date: date
    nights: int | None
    guests: int | None
    adults: int | None
    children: int | None
    infants: int | None
    pets: int | None
    guest_name: str | None
    host_payout: float | None
    currency: str | None
    # When Guesty recorded the reservation. Only the calendar reports it.
    created_at: datetime | None

    @property
    def confirmed(self) -> bool:
        """Return True for a firm booking rather than an unconfirmed hold."""
        return self.status in CONFIRMED_STATUSES

    @property
    def source_label(self) -> str | None:
        """Return a display name for the booking source."""
        return source_label(self.source)

    def active_at(self, now: datetime) -> bool:
        """Return True between check-in and check-out."""
        return self.check_in <= now < self.check_out

    @classmethod
    def from_api(
        cls, raw: Mapping[str, Any], listing: Listing, *, owner: bool
    ) -> Stay | None:
        """Build a stay from a reservation object.

        Both the reservation list and the calendar carry the local check-in and
        check-out dates plus planned arrival and departure times, so the instants
        are always built from those in the listing's timezone. The calendar's own
        ``checkIn``/``checkOut`` are local wall time labelled as UTC and are
        never used; the list's are real instants and serve as a fallback.
        """
        stay_id = _as_str(raw.get("_id"))
        status = _as_str(raw.get("status"))
        if stay_id is None or status is None:
            return None
        if status not in CONFIRMED_STATUSES and status not in TENTATIVE_STATUSES:
            return None

        tz = listing.tz
        check_in_date = _as_date(raw.get("checkInDateLocalized"))
        check_out_date = _as_date(raw.get("checkOutDateLocalized"))
        if check_in_date is not None and check_out_date is not None:
            arrival = _as_time(raw.get("plannedArrival"), listing.check_in_time)
            departure = _as_time(raw.get("plannedDeparture"), listing.check_out_time)
            check_in = datetime.combine(check_in_date, arrival, tz).astimezone(UTC)
            check_out = datetime.combine(check_out_date, departure, tz).astimezone(UTC)
        else:
            instant_in = _as_instant(raw.get("checkIn"))
            instant_out = _as_instant(raw.get("checkOut"))
            if instant_in is None or instant_out is None:
                return None
            check_in, check_out = instant_in, instant_out
            check_in_date = check_in.astimezone(tz).date()
            check_out_date = check_out.astimezone(tz).date()
        if check_out <= check_in:
            return None

        people = _as_dict(raw.get("numberOfGuests"))
        money = _as_dict(raw.get("money"))
        source = _as_str(raw.get("source"))
        return cls(
            id=stay_id,
            listing_id=listing.id,
            owner=owner or (source or "").lower().startswith("owner"),
            status=status,
            source=source,
            confirmation_code=_as_str(raw.get("confirmationCode")),
            check_in=check_in,
            check_out=check_out,
            check_in_date=check_in_date,
            check_out_date=check_out_date,
            nights=_as_int(raw.get("nightsCount")),
            guests=_as_int(raw.get("guestsCount")),
            adults=_as_int(people.get("numberOfAdults")),
            children=_as_int(people.get("numberOfChildren")),
            infants=_as_int(people.get("numberOfInfants")),
            pets=_as_int(people.get("numberOfPets")),
            guest_name=_as_str(_as_dict(raw.get("guest")).get("fullName")),
            host_payout=_as_float(money.get("hostPayout")),
            currency=_as_str(money.get("currency")),
            created_at=_as_instant(raw.get("createdAt")),
        )

    def merged_with(self, other: Stay) -> Stay:
        """Fill fields this stay is missing from another view of the same stay."""
        updates: dict[str, Any] = {
            item.name: getattr(other, item.name)
            for item in fields(self)
            if getattr(self, item.name) is None
            and getattr(other, item.name) is not None
        }
        if other.owner and not self.owner:
            updates["owner"] = True
        return replace(self, **updates) if updates else self


@dataclass(frozen=True, slots=True, kw_only=True)
class CalendarDay:
    """Price and availability for one night."""

    day: date
    status: str | None
    price: float | None
    currency: str | None
    min_nights: int | None
    blocks: tuple[str, ...]

    @classmethod
    def from_api(cls, raw: Mapping[str, Any]) -> CalendarDay | None:
        """Build a day from the listing calendar."""
        if (day := _as_date(raw.get("date"))) is None:
            return None
        blocks = _as_dict(raw.get("blocks"))
        return cls(
            day=day,
            status=_as_str(raw.get("status")),
            price=_as_float(raw.get("price")),
            currency=_as_str(raw.get("currency")),
            min_nights=_as_int(raw.get("minNights")),
            blocks=tuple(sorted(key for key, value in blocks.items() if value is True)),
        )


def stays_from_calendar(
    raw_days: Iterable[Mapping[str, Any]], listing: Listing
) -> dict[str, Stay]:
    """Collect the reservations referenced by calendar days.

    Each blocked night carries ``blockRefs`` with the reservation behind the
    block. Block type ``o`` marks an owner stay; ``pt`` is the preparation day
    after a checkout and points at the same reservation.
    """
    stays: dict[str, Stay] = {}
    for raw_day in raw_days:
        refs = raw_day.get("blockRefs")
        if not isinstance(refs, list):
            continue
        for raw_ref in refs:
            ref = _as_dict(raw_ref)
            reservation = _as_dict(ref.get("reservation"))
            owner = ref.get("type") == "o" or bool(reservation.get("ownerId"))
            known = stays.get(str(reservation.get("_id")))
            if known is not None:
                # The same reservation is repeated on every night it blocks.
                if owner and not known.owner:
                    stays[known.id] = replace(known, owner=True)
                continue
            stay = Stay.from_api(reservation, listing, owner=owner)
            if stay is not None:
                stays[stay.id] = stay
    return stays


def merge_stays(
    primary: Mapping[str, Stay], secondary: Iterable[Stay]
) -> tuple[Stay, ...]:
    """Combine calendar stays with listed reservations, sorted by check-in."""
    merged = dict(primary)
    for stay in secondary:
        known = merged.get(stay.id)
        merged[stay.id] = stay if known is None else known.merged_with(stay)
    return tuple(sorted(merged.values(), key=lambda item: (item.check_in, item.id)))


@dataclass(frozen=True, slots=True, kw_only=True)
class ListingSnapshot:
    """Everything known about one listing's stays and calendar at a point in time."""

    listing: Listing
    stays: tuple[Stay, ...]
    days: tuple[CalendarDay, ...]
    # Last local date each source covered, or None if it could not be read.
    # The calendar shows every stay in its range. The reservation list reaches
    # further ahead but has been seen to leave some bookings out.
    calendar_end: date | None
    list_end: date | None
    # When this view was fetched. Not part of equality, so unchanged data still
    # compares equal from one poll to the next.
    fetched_at: datetime = field(compare=False)

    def local_date(self, now: datetime) -> date:
        """Return today's date where the listing is."""
        return now.astimezone(self.listing.tz).date()

    def current_stay(self, now: datetime) -> Stay | None:
        """Return the confirmed stay in progress, if any."""
        for stay in self.stays:
            if stay.confirmed and stay.active_at(now):
                return stay
        return None

    def upcoming_stays(self, now: datetime) -> tuple[Stay, ...]:
        """Return confirmed stays that have not started yet."""
        return tuple(
            stay for stay in self.stays if stay.confirmed and stay.check_in > now
        )

    def next_stay(self, now: datetime) -> Stay | None:
        """Return the next confirmed stay that has not started yet."""
        upcoming = self.upcoming_stays(now)
        return upcoming[0] if upcoming else None

    def arrival_today(self, now: datetime) -> Stay | None:
        """Return the confirmed stay whose check-in falls on today's local date."""
        today = self.local_date(now)
        for stay in self.stays:
            if stay.confirmed and stay.check_in_date == today:
                return stay
        return None

    def departure_today(self, now: datetime) -> Stay | None:
        """Return the confirmed stay whose check-out falls on today's local date."""
        today = self.local_date(now)
        for stay in self.stays:
            if stay.confirmed and stay.check_out_date == today:
                return stay
        return None

    def tonight(self, now: datetime) -> CalendarDay | None:
        """Return the calendar entry for today's local date."""
        today = self.local_date(now)
        for entry in self.days:
            if entry.day == today:
                return entry
        return None

    def booked_nights(self, now: datetime, window: int) -> int | None:
        """Count guest-booked nights in the next ``window`` days."""
        if not self.days:
            return None
        today = self.local_date(now)
        end = today + timedelta(days=window)
        return sum(
            1
            for entry in self.days
            if today <= entry.day < end and entry.status == "booked"
        )

    def next_transition(self, now: datetime) -> datetime:
        """Return the next instant at which time alone changes this listing's state.

        That is the next check-in or check-out, or the next local midnight (when
        "today" rolls over), whichever comes first.
        """
        tz = self.listing.tz
        tomorrow = now.astimezone(tz).date() + timedelta(days=1)
        candidates = [datetime.combine(tomorrow, time.min, tz).astimezone(UTC)]
        for stay in self.stays:
            candidates.extend(
                instant for instant in (stay.check_in, stay.check_out) if instant > now
            )
        return min(candidates)


@dataclass(frozen=True, slots=True, kw_only=True)
class PeriodMetrics:
    """Revenue and occupancy figures for one listing over one period."""

    owner_revenue: float | None
    net_rental_income: float | None
    occupancy: float | None
    booked_nights: int | None
    average_nightly_rate: float | None
    currency: str | None

    @classmethod
    def from_api(
        cls,
        raw: Mapping[str, Any],
        *,
        occupancy: float | None = None,
        booked_nights: float | None = None,
    ) -> PeriodMetrics:
        """Build metrics from ``/revenue-analytics/analytics/owner``.

        Each tile is ``{"title": "OWNER_REVENUE", "amount": 1250.5}``. Managers
        choose which tiles owners may see, so any of them can be missing.

        ``occupancy`` (a fraction) and ``booked_nights`` come from the service
        the portal's own Analytics page reads. Over long periods its numbers
        differ from the tiles, and since they are what the owner sees in the
        portal they win; the tiles are the fallback.
        """
        tiles: dict[str, float | None] = {}
        boxes = raw.get("infoBox")
        for box in boxes if isinstance(boxes, list) else []:
            if isinstance(box, Mapping) and isinstance(box.get("title"), str):
                tiles[box["title"]] = _as_float(box.get("amount"))
        return cls(
            owner_revenue=tiles.get("OWNER_REVENUE"),
            net_rental_income=tiles.get("NET_RENTAL_INCOME"),
            occupancy=(
                round(occupancy * 100, 2)
                if occupancy is not None
                else tiles.get("OCCUPANCY")
            ),
            booked_nights=_as_int(
                booked_nights
                if booked_nights is not None
                else tiles.get("BOOKED_NIGHTS")
            ),
            average_nightly_rate=tiles.get("AVG_NIGHTLY_RATE"),
            currency=_as_str(raw.get("currency")),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ReviewSummary:
    """Guest review totals for one listing."""

    count: int
    average: float | None
    latest: datetime | None

    @classmethod
    def from_api(cls, raw: Mapping[str, Any]) -> ReviewSummary | None:
        """Build a summary from ``/owners/me/reviews``."""
        distribution = raw.get("ratingDistribution")
        if not isinstance(distribution, Mapping):
            return None
        count = 0
        total = 0.0
        for stars, amount in distribution.items():
            try:
                rating = float(stars)
            except TypeError, ValueError:
                continue
            number = _as_int(amount) or 0
            count += number
            total += rating * number
        results = _as_dict(raw.get("reviews")).get("results")
        latest = None
        if isinstance(results, list) and results:
            latest = _as_instant(_as_dict(results[0]).get("createdAt"))
        return cls(
            count=count,
            average=round(total / count, 2) if count else None,
            latest=latest,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class Statement:
    """The most recent owner statement."""

    id: str
    period_start: date | None
    period_end: date | None
    due_to_owner: float | None
    ending_balance: float | None
    paid_to_owner: float | None
    payout_status: str | None
    currency: str | None

    @classmethod
    def from_api(cls, raw: Mapping[str, Any]) -> Statement | None:
        """Build a statement from ``/statements-v2/owner-portal``."""
        if (statement_id := _as_str(raw.get("_id"))) is None:
            return None
        payout_status = _as_str(raw.get("payoutStatus"))
        return cls(
            id=statement_id,
            period_start=_as_date(raw.get("periodStartDate")),
            period_end=_as_date(raw.get("periodEndDate")),
            due_to_owner=_as_float(raw.get("dueToOwner")),
            ending_balance=_as_float(raw.get("endingOwnerBalance")),
            paid_to_owner=_as_float(raw.get("paidToOwner")),
            payout_status=payout_status.lower() if payout_status else None,
            currency=_as_str(raw.get("currency")),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ListingStats:
    """Slow-changing figures for one listing; a section is None when hidden."""

    month: PeriodMetrics | None
    year: PeriodMetrics | None
    reviews: ReviewSummary | None


@dataclass(frozen=True, slots=True, kw_only=True)
class StatsData:
    """Slow-changing figures for the whole account."""

    listings: Mapping[str, ListingStats]
    statement: Statement | None
    unread_messages: int | None
