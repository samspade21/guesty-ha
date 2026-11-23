"""Fixtures: a fake owners portal that answers like the real API.

Every ID, name and amount here is invented. The shapes mirror what the portal
returns, including its quirks: guest stays leave the reservation list once
they start, and the calendar labels local times as UTC.
"""

from collections.abc import Generator
from datetime import date, timedelta
import re
from typing import Any

from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant.common import MockConfigEntry, async_fire_time_changed
from pytest_homeassistant.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)
from yarl import URL

from custom_components.guesty_owners.const import API_BASE, DOMAIN

# Noon in Phoenix on a day with a guest in the house.
NOW = "2026-06-10T19:00:00+00:00"
TODAY = date(2026, 6, 10)

HOST = "acme.guestyowners.com"
USERNAME = "owner@example.com"
PASSWORD = "correct-horse"
OWNER_ID = "0wner000000000000000001"
ACCOUNT_ID = "acc0unt00000000000000001"
LISTING_ID = "11st1ng00000000000000001"
TIMEZONE = "America/Phoenix"
TOKEN = "aGVhZGVy.cGF5bG9hZA.c2lnbmF0dXJl"

STAY_CURRENT = "stay00000000000000000001"
STAY_NEXT = "stay00000000000000000002"
STAY_OWNER = "stay00000000000000000003"
STAY_FAR = "stay00000000000000000004"


def reservation(
    stay_id: str,
    check_in: date,
    check_out: date,
    *,
    source: str = "airbnb2",
    status: str = "confirmed",
    guest: str = "Alex Example",
    guests: int = 4,
    owner: bool = False,
    created: str = "2026-04-01T12:00:00.000Z",
) -> dict[str, Any]:
    """Build a reservation the way the calendar's blockRefs carry it."""
    return {
        "_id": stay_id,
        "listingId": LISTING_ID,
        "checkInDateLocalized": check_in.isoformat(),
        "checkOutDateLocalized": check_out.isoformat(),
        "status": status,
        "confirmationCode": f"CODE-{stay_id[-2:]}",
        "source": source,
        "nightsCount": (check_out - check_in).days,
        # Owner reservations come with empty planned times.
        "plannedArrival": "" if owner else "16:00",
        "plannedDeparture": "" if owner else "10:00",
        "guest": {"_id": f"guest-{stay_id[-2:]}", "fullName": guest},
        "guestsCount": guests,
        "numberOfGuests": {
            "numberOfAdults": 2,
            "numberOfChildren": guests - 2,
            "numberOfInfants": 0,
        },
        "money": {"currency": "USD", "hostPayout": 500.0},
        "createdAt": created,
        # Local wall time labelled as UTC; the integration must not trust it.
        "checkIn": f"{check_in.isoformat()}T16:00:00+00:00",
        "checkOut": f"{check_out.isoformat()}T10:00:00+00:00",
        **({"ownerId": OWNER_ID} if owner else {}),
    }


def listed(raw: dict[str, Any]) -> dict[str, Any]:
    """Reshape a reservation the way ``/owners/me/reservations`` lists it."""
    item = {
        key: value
        for key, value in raw.items()
        if key
        not in (
            "money",
            "confirmationCode",
            "checkIn",
            "checkOut",
            "ownerId",
            "createdAt",
        )
    }
    item["numberOfGuests"] = {**raw["numberOfGuests"], "numberOfPets": 1}
    return item


class FakePortal:
    """State of the fake portal; tests change it between refreshes."""

    def __init__(self) -> None:
        """Start with one listing, a guest in the house and bookings ahead."""
        self.logins = 0
        self.session_valid = False
        self.password = PASSWORD
        self.login_body: Any = TOKEN
        self.failures: dict[str, int] = {}
        # Reservations the list endpoint leaves out, as the real one has been
        # seen to do; the calendar still shows them.
        self.unlisted: set[str] = set()
        # Canceled reservations the list endpoint still returns for a while.
        self.stale_listed: dict[str, dict[str, Any]] = {}
        # Some portals may not put reservation details on calendar nights.
        self.calendar_names_bookings = True
        self.owner: dict[str, Any] = {
            "_id": OWNER_ID,
            "accountId": ACCOUNT_ID,
            "email": USERNAME,
            "fullName": "Olive Owner",
            "ownersPortalSettings": {"showInbox": True, "netRentalIncome": True},
        }
        self.listings: list[dict[str, Any]] = [
            {
                "_id": LISTING_ID,
                "nickname": "Harbor Loft",
                "title": "Harbor Loft / 1BR by the marina",
                "address": {"full": "1 Example Way, Testville, AZ 85000"},
                "timezone": TIMEZONE,
                "defaultCheckInTime": "16:00",
                "defaultCheckOutTime": "10:00",
                "bedrooms": 1,
                "propertyType": "Apartment",
                "active": True,
            }
        ]
        self.reservations: dict[str, dict[str, Any]] = {
            STAY_CURRENT: reservation(
                STAY_CURRENT, date(2026, 6, 8), date(2026, 6, 12)
            ),
            STAY_NEXT: reservation(
                STAY_NEXT,
                date(2026, 6, 12),
                date(2026, 6, 15),
                source="VRBO",
                guest="Blake Sample",
                guests=2,
            ),
            STAY_OWNER: reservation(
                STAY_OWNER,
                date(2026, 6, 20),
                date(2026, 6, 22),
                source="owner-guest",
                guest="Olive Owner",
                owner=True,
            ),
            STAY_FAR: reservation(STAY_FAR, date(2026, 11, 1), date(2026, 11, 5)),
        }
        self.analytics: dict[str, Any] = {
            "infoBox": [
                {"title": "OWNER_REVENUE", "amount": 1250.5, "type": "USD"},
                {"title": "OCCUPANCY", "amount": 40.0, "type": ""},
                {"title": "AVG_NIGHTLY_RATE", "amount": 185.25, "type": "USD"},
                {"title": "BOOKED_NIGHTS", "amount": 12, "type": None},
                {"title": "NET_RENTAL_INCOME", "amount": "N/A", "type": "USD"},
            ],
            "currency": "USD",
        }
        self.reviews: dict[str, Any] = {
            "ratingDistribution": {"1": 0, "2": 0, "3": 1, "4": 5, "5": 14},
            "reviews": {
                "results": [{"rating": 5, "createdAt": "2026-05-20T09:30:00.000Z"}],
                "totalCount": 20,
            },
        }
        self.statements: list[dict[str, Any]] = [
            {
                "_id": "statement000000000000001",
                "periodStartDate": "2026-05-01T00:00:00.000Z",
                "periodEndDate": "2026-05-31T00:00:00.000Z",
                "dueToOwner": 1500.25,
                "endingOwnerBalance": 300.0,
                "paidToOwner": 0,
                "payoutStatus": "NOT_PAID",
                "currency": "USD",
            }
        ]
        # The service behind the Analytics page; occupancy is a fraction here.
        self.occupancy: dict[str, Any] = {"data": [{"occupancy": 0.35}]}
        self.booked_nights: dict[str, Any] = {"data": [{"bookedNights": 64}]}
        self.unread = 3

    def fail(self, section: str, status: int) -> None:
        """Make one section of the API answer with an error status."""
        self.failures[section] = status

    def _calendar(self, start: date, end: date) -> list[dict[str, Any]]:
        days = []
        day = start
        while day <= end:
            refs = []
            status = "available"
            blocks = {"b": False, "o": False, "pt": False, "m": False}
            for raw in self.reservations.values():
                first = date.fromisoformat(raw["checkInDateLocalized"])
                last = date.fromisoformat(raw["checkOutDateLocalized"])
                if not first <= day < last:
                    continue
                owner = "ownerId" in raw
                kind = "o" if owner else "b"
                blocks[kind] = True
                status = "unavailable" if owner else "booked"
                ref = {"type": kind, "reservationId": raw["_id"]}
                if self.calendar_names_bookings:
                    ref["reservation"] = raw
                refs.append(ref)
            days.append(
                {
                    "date": day.isoformat(),
                    "listingId": LISTING_ID,
                    "currency": "USD",
                    "price": 150 + day.day,
                    "minNights": 3,
                    "status": status,
                    "blocks": blocks,
                    "blockRefs": refs,
                }
            )
            day += timedelta(days=1)
        return days

    def _listed(self, query: Any, *, today: date) -> dict[str, Any]:
        owner = query["reservationType"] == "owner"
        items = [
            listed(raw)
            for raw in (*self.reservations.values(), *self.stale_listed.values())
            if ("ownerId" in raw) is owner
            and raw["_id"] not in self.unlisted
            # Guest stays vanish from this list once they have started.
            and (owner or date.fromisoformat(raw["checkInDateLocalized"]) > today)
            and raw["checkOutDateLocalized"] > query["startDate"]
            and raw["checkInDateLocalized"] <= query["endDate"]
        ]
        skip = int(query.get("skip", 0))
        return {
            "reservations": items[skip:],
            "count": len(items),
            "totalCount": len(items),
        }

    def respond(self, method: str, url: URL, body: Any, today: date) -> tuple[int, Any]:
        """Answer one request with (status, body)."""
        path = url.path.removeprefix(URL(API_BASE).path)
        query = url.query
        if path.startswith("/owners/auth/portals/meta/"):
            if path.rsplit("/", 1)[-1] != HOST:
                return 404, 'message: Portal not found data: "Portal not found"'
            return 200, {"accountId": ACCOUNT_ID, "account": {"name": "Acme Stays"}}
        if path == "/owners/auth/login":
            self.logins += 1
            if body["password"] != self.password or body["username"] != USERNAME:
                return 401, 'message: Unauthorized data: "Wrong credentials!"'
            self.session_valid = True
            return 200, self.login_body
        if not self.session_valid:
            return 401, {"message": "Not Authorized"}

        sections = {
            "/owners/me/listings": "listings",
            "/owners/me/reservations": "reservations",
            "/owners/me/reviews": "reviews",
            "/owners/me": "owner",
            "/revenue-analytics/analytics/owner": "analytics",
            "/revenue-analytics-service/occupancy": "occupancy",
            "/revenue-analytics-service/booked-nights": "booked_nights",
            "/statements-v2/owner-portal": "statements",
            "/inbox/owners-portal/conversations/count/unread": "unread",
        }
        section = "calendar" if path.endswith("/calendar") else sections[path]
        if section in self.failures:
            return self.failures[section], {"error": {"message": "nope", "status": 0}}

        if section == "calendar":
            return 200, self._calendar(
                date.fromisoformat(query["from"]), date.fromisoformat(query["to"])
            )
        if section == "reservations":
            return 200, self._listed(query, today=today)
        if section == "listings":
            # Like the real endpoint, this one ignores ``skip``.
            return 200, {"results": self.listings, "totalCount": len(self.listings)}
        if section == "statements":
            return 200, {"data": self.statements[:1], "total": len(self.statements)}
        if section == "unread":
            return 200, {"countUnread": self.unread, "type": "owner"}
        return 200, getattr(self, section)


async def advance(hass: HomeAssistant, freezer, interval: timedelta) -> None:
    """Move the clock forward and let everything scheduled until then run."""
    freezer.tick(interval)
    async_fire_time_changed(hass)
    # Coordinator refreshes run as background tasks.
    await hass.async_block_till_done(wait_background_tasks=True)


@pytest.fixture(autouse=True)
def frozen_time(freezer) -> None:
    """Pin the clock so stays are in a known relation to now."""
    freezer.move_to(NOW)


@pytest.fixture
def portal(aioclient_mock: AiohttpClientMocker, freezer) -> FakePortal:
    """Route every API request to a fake portal."""
    fake = FakePortal()

    async def handle(method: str, url: URL, data: Any) -> AiohttpClientMockResponse:
        # "Today" for the fake follows the frozen clock in the listing's zone.
        from homeassistant.util import dt as dt_util  # noqa: PLC0415

        today = dt_util.utcnow().astimezone(dt_util.get_time_zone(TIMEZONE)).date()
        status, payload = fake.respond(method, url, data, today)
        if isinstance(payload, str) and not payload.startswith("message:"):
            return AiohttpClientMockResponse(method, url, status=status, json=payload)
        if isinstance(payload, str):
            return AiohttpClientMockResponse(method, url, status=status, text=payload)
        return AiohttpClientMockResponse(method, url, status=status, json=payload)

    pattern = re.compile(rf"^{re.escape(API_BASE)}/")
    aioclient_mock.get(pattern, side_effect=handle)
    aioclient_mock.post(pattern, side_effect=handle)
    return fake


@pytest.fixture
def config_entry() -> MockConfigEntry:
    """Return a config entry for the fake portal's owner."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="Acme Stays",
        unique_id=OWNER_ID,
        data={CONF_HOST: HOST, CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD},
    )


@pytest.fixture
async def setup_entry(
    hass: HomeAssistant, config_entry: MockConfigEntry, portal: FakePortal
) -> MockConfigEntry:
    """Set up the integration against the fake portal."""
    await hass.config.async_set_time_zone("UTC")
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    return config_entry


@pytest.fixture(autouse=True)
def enable_all_entities(entity_registry_enabled_by_default: None) -> Generator[None]:
    """Enable entities that are disabled by default so tests can read them."""
    return
