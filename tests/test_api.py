"""Tests for the API client."""

from datetime import date

from aiohttp import ClientError
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_create_clientsession
import pytest
from pytest_homeassistant.test_util.aiohttp import AiohttpClientMocker

from custom_components.guesty_owners.api import (
    GuestyOwnersApiError,
    GuestyOwnersAuthError,
    GuestyOwnersClient,
    GuestyOwnersConnectionError,
    GuestyOwnersPortalNotFoundError,
    GuestyOwnersRateLimitError,
    normalize_hostname,
)
from custom_components.guesty_owners.const import API_BASE, PORTAL_API_KEY

from .conftest import (
    HOST,
    LISTING_ID,
    OWNER_ID,
    PASSWORD,
    STAY_NEXT,
    TOKEN,
    USERNAME,
    FakePortal,
)


def make_client(
    hass: HomeAssistant, host: str = HOST, password: str = PASSWORD
) -> GuestyOwnersClient:
    return GuestyOwnersClient(
        async_create_clientsession(hass), host, USERNAME, password
    )


@pytest.mark.parametrize(
    ("typed", "hostname"),
    [
        ("acme", "acme.guestyowners.com"),
        ("  Acme.GuestyOwners.com ", "acme.guestyowners.com"),
        ("https://acme.guestyowners.com/login", "acme.guestyowners.com"),
        ("http://owners.example.com:443/dashboard?x=1", "owners.example.com"),
        ("owners.example.co.uk/", "owners.example.co.uk"),
    ],
)
def test_normalize_hostname(typed: str, hostname: str) -> None:
    assert normalize_hostname(typed) == hostname


@pytest.mark.parametrize("typed", ["", "   ", "https://", "bad host.com", "a_b.com"])
def test_normalize_hostname_rejects(typed: str) -> None:
    with pytest.raises(ValueError, match="Not a valid portal hostname"):
        normalize_hostname(typed)


@pytest.mark.parametrize("body", [TOKEN, {"token": TOKEN}, f"  {TOKEN}\n"])
async def test_login_accepts_token_shapes(
    hass: HomeAssistant,
    portal: FakePortal,
    aioclient_mock: AiohttpClientMocker,
    body: object,
) -> None:
    """The token may come back bare or wrapped in an object."""
    portal.login_body = body
    client = make_client(hass)

    assert await client.async_login() == TOKEN
    owner = await client.async_get_owner()

    assert owner["_id"] == OWNER_ID
    _, _, sent, _ = aioclient_mock.mock_calls[0]
    assert sent == {
        "username": USERNAME,
        "password": PASSWORD,
        "hostname": HOST,
        "apiKey": PORTAL_API_KEY,
    }
    _, _, _, headers = aioclient_mock.mock_calls[1]
    assert headers["Authorization"] == f"Bearer {TOKEN}"


@pytest.mark.parametrize("body", ["", "not a token", {"token": None}, {}, 42])
async def test_login_without_token(
    hass: HomeAssistant, portal: FakePortal, body: object
) -> None:
    portal.login_body = body
    with pytest.raises(GuestyOwnersApiError, match="no token"):
        await make_client(hass).async_login()


async def test_login_wrong_password(hass: HomeAssistant, portal: FakePortal) -> None:
    with pytest.raises(GuestyOwnersAuthError):
        await make_client(hass, password="wrong").async_login()


async def test_first_request_logs_in(hass: HomeAssistant, portal: FakePortal) -> None:
    client = make_client(hass)
    await client.async_get_owner()
    await client.async_get_owner()
    assert portal.logins == 1


async def test_expired_session_logs_in_again(
    hass: HomeAssistant, portal: FakePortal
) -> None:
    """A 401 on a data request means the three-week token ran out."""
    client = make_client(hass)
    await client.async_get_owner()
    portal.session_valid = False

    assert (await client.async_get_owner())["_id"] == OWNER_ID
    assert portal.logins == 2


async def test_expired_session_and_changed_password(
    hass: HomeAssistant, portal: FakePortal
) -> None:
    client = make_client(hass)
    await client.async_get_owner()
    portal.session_valid = False
    portal.password = "changed"

    with pytest.raises(GuestyOwnersAuthError):
        await client.async_get_owner()


async def test_401_after_fresh_login_is_an_api_error(
    hass: HomeAssistant, portal: FakePortal
) -> None:
    portal.fail("reviews", 401)
    with pytest.raises(GuestyOwnersApiError) as raised:
        await make_client(hass).async_get_reviews("l")
    assert not isinstance(raised.value, GuestyOwnersAuthError)
    assert raised.value.status == 401


async def test_login_error_describes_shape_not_values(
    hass: HomeAssistant, portal: FakePortal
) -> None:
    portal.login_body = {"accessToken": "secret-value", "user": {}}
    with pytest.raises(GuestyOwnersApiError) as raised:
        await make_client(hass).async_login()
    assert "['accessToken', 'user']" in str(raised.value)
    assert "secret-value" not in str(raised.value)


async def test_period_figures(hass: HomeAssistant, portal: FakePortal) -> None:
    client = make_client(hass)
    period = ("l", "o", date(2026, 1, 1), date(2026, 12, 31))
    assert await client.async_get_occupancy(*period) == 0.35
    assert await client.async_get_booked_nights(*period) == 64.0

    portal.occupancy = {"data": []}
    portal.booked_nights = {"data": [{"bookedNights": None}]}
    assert await client.async_get_occupancy(*period) is None
    assert await client.async_get_booked_nights(*period) is None


async def test_portal_meta(hass: HomeAssistant, portal: FakePortal) -> None:
    meta = await make_client(hass).async_get_portal_meta()
    assert meta["account"]["name"] == "Acme Stays"
    assert portal.logins == 0


async def test_portal_not_found(hass: HomeAssistant, portal: FakePortal) -> None:
    with pytest.raises(GuestyOwnersPortalNotFoundError):
        await make_client(hass, host="nope.guestyowners.com").async_get_portal_meta()


async def test_listings_stop_when_skip_is_ignored(
    hass: HomeAssistant, portal: FakePortal
) -> None:
    """An endpoint that repeats its first page must not loop or duplicate."""
    portal.listings = [{"_id": f"listing-{number}"} for number in range(3)]
    listings = await make_client(hass).async_get_listings()

    assert [item["_id"] for item in listings] == [
        "listing-0",
        "listing-1",
        "listing-2",
    ]


async def test_reservations_query(
    hass: HomeAssistant, portal: FakePortal, aioclient_mock: AiohttpClientMocker
) -> None:
    client = make_client(hass)
    stays = await client.async_get_reservations(
        LISTING_ID, date(2026, 6, 8), date(2027, 6, 10), owner=False
    )

    # The guest in the house is not listed; the calendar is what shows them.
    assert STAY_NEXT in [stay["_id"] for stay in stays]
    assert len(stays) == 2
    _, url, _, _ = aioclient_mock.mock_calls[-1]
    assert url.query["reservationType"] == "guest"
    assert url.query["reservationDateMatchType"] == "anyNightInRange"
    assert url.query["listingIds[]"] == LISTING_ID
    assert url.query["startDate"] == "2026-06-08"


async def test_calendar_query(
    hass: HomeAssistant, portal: FakePortal, aioclient_mock: AiohttpClientMocker
) -> None:
    days = await make_client(hass).async_get_calendar(
        LISTING_ID, OWNER_ID, date(2026, 6, 10), date(2026, 6, 12)
    )

    assert [day["date"] for day in days] == ["2026-06-10", "2026-06-11", "2026-06-12"]
    _, url, _, _ = aioclient_mock.mock_calls[-1]
    assert url.path.endswith(f"/listings/{LISTING_ID}/calendar")
    assert url.query["ownerId"] == OWNER_ID
    assert "blocks" in url.query["fields"]


@pytest.mark.parametrize(
    ("section", "call"),
    [
        ("analytics", lambda c: c.async_get_analytics("l", "o", date.min, date.max)),
        ("reviews", lambda c: c.async_get_reviews("l")),
        ("statements", lambda c: c.async_get_latest_statement()),
        ("unread", lambda c: c.async_get_unread_count()),
    ],
)
async def test_section_error(
    hass: HomeAssistant, portal: FakePortal, section: str, call
) -> None:
    portal.fail(section, 404)
    with pytest.raises(GuestyOwnersApiError) as raised:
        await call(make_client(hass))
    assert raised.value.status == 404


async def test_no_statement_yet(hass: HomeAssistant, portal: FakePortal) -> None:
    portal.statements = []
    assert await make_client(hass).async_get_latest_statement() is None


async def test_rate_limit(hass: HomeAssistant, portal: FakePortal) -> None:
    portal.fail("owner", 429)
    with pytest.raises(GuestyOwnersRateLimitError):
        await make_client(hass).async_get_owner()


async def test_unexpected_body(hass: HomeAssistant, portal: FakePortal) -> None:
    portal.owner = ["not", "an", "object"]  # type: ignore[assignment]
    with pytest.raises(GuestyOwnersApiError, match="unexpected body"):
        await make_client(hass).async_get_owner()


async def test_connection_error(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(
        f"{API_BASE}/owners/auth/portals/meta/{HOST}", exc=ClientError("boom")
    )
    with pytest.raises(GuestyOwnersConnectionError):
        await make_client(hass).async_get_portal_meta()
