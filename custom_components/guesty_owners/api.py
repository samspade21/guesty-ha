"""Read-only client for the Guesty Owners Portal API.

The portal is a single-page app served from ``<name>.guestyowners.com`` (or a
property manager's own domain) that talks to ``https://app.guesty.com/api``.
This client reproduces only the GET requests the portal makes, plus the login.
It is not the official Guesty Open API and needs no API keys from the manager.
"""

import asyncio
from datetime import date
import json
import logging
import math
import re
from typing import Any, Final

import aiohttp

from .const import API_BASE, PORTAL_API_KEY, PORTAL_DOMAIN_SUFFIX

_LOGGER = logging.getLogger(__name__)

REQUEST_TIMEOUT: Final = aiohttp.ClientTimeout(total=45)
PAGE_SIZE: Final = 100
MAX_PAGES: Final = 20

# The portal asks for exactly these calendar fields; without the filter a day is
# roughly seven times larger.
CALENDAR_FIELDS: Final = (
    "date accountId listingId guest status note price currency "
    "listing.prices.currency reservationId reservation blocks "
    "ownerReservation ownerReservationId"
)

_HOSTNAME_RE: Final = re.compile(r"[a-z0-9-]+(\.[a-z0-9-]+)+")
_TOKEN_RE: Final = re.compile(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")


class GuestyOwnersError(Exception):
    """Base error for the Guesty Owners Portal client."""


class GuestyOwnersConnectionError(GuestyOwnersError):
    """The API could not be reached."""


class GuestyOwnersAuthError(GuestyOwnersError):
    """The credentials were rejected."""


class GuestyOwnersPortalNotFoundError(GuestyOwnersError):
    """No owners portal exists at the given hostname."""


class GuestyOwnersApiError(GuestyOwnersError):
    """The API answered with an error or an unexpected body."""

    def __init__(self, message: str, status: int | None = None) -> None:
        """Initialize the error."""
        super().__init__(message)
        self.status = status


class GuestyOwnersRateLimitError(GuestyOwnersApiError):
    """The API asked us to slow down."""

    def __init__(self, retry_after: float | None) -> None:
        """Initialize the error."""
        super().__init__("Rate limited by the Guesty API", 429)
        self.retry_after = retry_after


def normalize_hostname(value: str) -> str:
    """Turn what a user typed into a portal hostname.

    Accepts a bare portal name (``acme``), a hostname, or a pasted URL. A bare
    name becomes ``acme.guestyowners.com``.
    """
    host = value.strip().lower()
    if "://" in host:
        host = host.split("://", 1)[1]
    host = re.split(r"[/?#]", host, maxsplit=1)[0]
    host = host.rsplit("@", 1)[-1].split(":", 1)[0].strip(".")
    if host and "." not in host:
        host += PORTAL_DOMAIN_SUFFIX
    if not _HOSTNAME_RE.fullmatch(host):
        raise ValueError(f"Not a valid portal hostname: {value!r}")
    return host


def _decode(text: str) -> Any:
    """Decode a response body, which is JSON for data and plain text for errors."""
    try:
        return json.loads(text)
    except ValueError:
        return text


def _error_message(payload: Any) -> str:
    """Pull a short human-readable message out of an error body."""
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            payload = error
        for key in ("message", "data"):
            if isinstance(payload.get(key), str):
                return str(payload[key])[:200]
        return "no error message"
    return str(payload)[:200]


class GuestyOwnersClient:
    """Minimal async client for one owners portal login."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        hostname: str,
        username: str,
        password: str,
    ) -> None:
        """Initialize the client."""
        self._session = session
        self._hostname = hostname
        self._username = username
        self._password = password
        self._token: str | None = None
        self._login_lock = asyncio.Lock()

    @property
    def hostname(self) -> str:
        """Return the portal hostname this client logs in to."""
        return self._hostname

    async def _raw(
        self,
        method: str,
        path: str,
        *,
        params: list[tuple[str, str]] | None = None,
        body: dict[str, Any] | None = None,
        token: str | None = None,
    ) -> tuple[int, Any, float | None]:
        """Send one request and return status, decoded body and Retry-After."""
        headers = {"Accept": "application/json"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        try:
            async with self._session.request(
                method,
                f"{API_BASE}{path}",
                params=params,
                json=body,
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            ) as response:
                text = await response.text()
                retry_after: float | None = None
                if (raw := response.headers.get("Retry-After")) is not None:
                    try:
                        retry_after = float(raw)
                    except ValueError:
                        retry_after = None
                return response.status, _decode(text), retry_after
        except (aiohttp.ClientError, TimeoutError) as err:
            raise GuestyOwnersConnectionError(
                f"Error talking to the Guesty API: {err!r}"
            ) from err

    async def async_get_portal_meta(self) -> dict[str, Any]:
        """Return the public description of the portal (no login needed)."""
        status, payload, _ = await self._raw(
            "GET", f"/owners/auth/portals/meta/{self._hostname}"
        )
        if status == 404:
            raise GuestyOwnersPortalNotFoundError(self._hostname)
        if status >= 400 or not isinstance(payload, dict):
            raise GuestyOwnersApiError(
                f"Portal lookup failed: {_error_message(payload)}", status
            )
        return payload

    async def async_login(self) -> str:
        """Log in, keep the session token and return it.

        The token is a JWT that is valid for about three weeks. There is no
        refresh endpoint; the portal simply logs in again.
        """
        status, payload, retry_after = await self._raw(
            "POST",
            "/owners/auth/login",
            body={
                "username": self._username,
                "password": self._password,
                "hostname": self._hostname,
                "apiKey": PORTAL_API_KEY,
            },
        )
        if status in (401, 403):
            raise GuestyOwnersAuthError("Wrong email or password")
        if status == 429:
            raise GuestyOwnersRateLimitError(retry_after)
        if status >= 400:
            raise GuestyOwnersApiError(
                f"Login failed: {_error_message(payload)}", status
            )
        # The portal accepts either a bare token or an object carrying one.
        token = payload.get("token") if isinstance(payload, dict) else payload
        if isinstance(token, str):
            token = token.strip().strip('"')
        if not isinstance(token, str) or not _TOKEN_RE.fullmatch(token):
            # Say what did come back, without ever echoing a value from it.
            shape = (
                f"an object with keys {sorted(payload)}"
                if isinstance(payload, dict)
                else f"a {type(payload).__name__}"
            )
            raise GuestyOwnersApiError(
                f"Login succeeded but returned no token; got {shape}", status
            )
        self._token = token
        return token

    async def _async_ensure_token(self, stale: str | None) -> str:
        """Return a token, logging in if there is none or ``stale`` was rejected."""
        async with self._login_lock:
            if self._token is None or self._token == stale:
                self._token = None
                self._token = await self.async_login()
            return self._token

    async def _get(self, path: str, params: list[tuple[str, str]] | None = None) -> Any:
        """Send an authenticated GET, logging in again once if the token expired."""
        token = self._token or await self._async_ensure_token(None)
        status, payload, retry_after = await self._raw(
            "GET", path, params=params, token=token
        )
        if status == 401:
            token = await self._async_ensure_token(token)
            status, payload, retry_after = await self._raw(
                "GET", path, params=params, token=token
            )
            # Still 401 with a token minted a moment ago: the credentials are
            # fine and this is the API refusing the section, handled below.
        if status == 429:
            raise GuestyOwnersRateLimitError(retry_after)
        if status >= 400:
            raise GuestyOwnersApiError(
                f"GET {path} failed ({status}): {_error_message(payload)}", status
            )
        return payload

    async def _get_dict(
        self, path: str, params: list[tuple[str, str]] | None = None
    ) -> dict[str, Any]:
        """Send an authenticated GET that must return a JSON object."""
        payload = await self._get(path, params)
        if not isinstance(payload, dict):
            raise GuestyOwnersApiError(f"GET {path} returned an unexpected body")
        return payload

    async def _get_paged(
        self, path: str, params: list[tuple[str, str]], items_key: str
    ) -> list[dict[str, Any]]:
        """Collect every page of a ``skip``/``limit`` list endpoint."""
        items: list[dict[str, Any]] = []
        seen: set[Any] = set()
        for _ in range(MAX_PAGES):
            page = await self._get_dict(
                path,
                [*params, ("skip", str(len(items))), ("limit", str(PAGE_SIZE))],
            )
            batch = page.get(items_key)
            if not isinstance(batch, list):
                break
            # Not every list endpoint honours ``skip``; one that ignores it
            # answers with the same page again, which must end the loop.
            fresh = [
                item
                for item in batch
                if isinstance(item, dict) and item.get("_id") not in seen
            ]
            if not fresh:
                break
            seen.update(item.get("_id") for item in fresh)
            items.extend(fresh)
            total = page.get("totalCount")
            if not isinstance(total, int) or len(items) >= total:
                break
        return items

    async def async_get_owner(self) -> dict[str, Any]:
        """Return the logged-in owner, including the portal feature flags."""
        return await self._get_dict("/owners/me")

    async def async_get_listings(self) -> list[dict[str, Any]]:
        """Return every listing (property) the owner can see."""
        return await self._get_paged("/owners/me/listings", [], "results")

    async def async_get_reservations(
        self, listing_id: str, start: date, end: date, *, owner: bool
    ) -> list[dict[str, Any]]:
        """Return upcoming reservations with at least one night in the range.

        Guest and owner reservations are separate queries, and guest stays stop
        being listed once they have started; the calendar covers those.
        """
        return await self._get_paged(
            "/owners/me/reservations",
            [
                ("reservationType", "owner" if owner else "guest"),
                ("reservationDateMatchType", "anyNightInRange"),
                ("startDate", start.isoformat()),
                ("endDate", end.isoformat()),
                ("listingIds[]", listing_id),
            ],
            "reservations",
        )

    async def async_get_calendar(
        self, listing_id: str, owner_id: str, start: date, end: date
    ) -> list[dict[str, Any]]:
        """Return one entry per night with price, availability and bookings."""
        payload = await self._get(
            f"/revenue-analytics/analytics/listings/{listing_id}/calendar",
            [
                ("from", start.isoformat()),
                ("to", end.isoformat()),
                ("ownerId", owner_id),
                ("fields", CALENDAR_FIELDS),
                ("applyOwnerRatePlan", "true"),
            ],
        )
        if not isinstance(payload, list):
            raise GuestyOwnersApiError("Calendar returned an unexpected body")
        return [day for day in payload if isinstance(day, dict)]

    async def async_get_analytics(
        self, listing_id: str, owner_id: str, start: date, end: date
    ) -> dict[str, Any]:
        """Return revenue and occupancy figures for one listing and period."""
        return await self._get_dict(
            "/revenue-analytics/analytics/owner",
            [
                ("from", start.isoformat()),
                ("to", end.isoformat()),
                ("ownerId", owner_id),
                ("listingIds", listing_id),
                ("confirmedOnly", "true"),
            ],
        )

    async def _async_get_period_figure(
        self,
        name: str,
        key: str,
        listing_id: str,
        owner_id: str,
        start: date,
        end: date,
    ) -> float | None:
        """Return one figure from the service behind the portal's Analytics page."""
        payload = await self._get_dict(
            f"/revenue-analytics-service/{name}",
            [
                ("owner_id", owner_id),
                ("from", start.isoformat()),
                ("to", end.isoformat()),
                ("listing_id", listing_id),
            ],
        )
        rows = payload.get("data")
        if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
            return None
        value = rows[0].get(key)
        if isinstance(value, bool) or not isinstance(value, int | float):
            return None
        try:
            number = float(value)
        except OverflowError:
            return None
        return number if math.isfinite(number) else None

    async def async_get_occupancy(
        self, listing_id: str, owner_id: str, start: date, end: date
    ) -> float | None:
        """Return the occupancy the portal shows, as a fraction between 0 and 1."""
        return await self._async_get_period_figure(
            "occupancy", "occupancy", listing_id, owner_id, start, end
        )

    async def async_get_booked_nights(
        self, listing_id: str, owner_id: str, start: date, end: date
    ) -> float | None:
        """Return the booked nights the portal shows."""
        return await self._async_get_period_figure(
            "booked-nights", "bookedNights", listing_id, owner_id, start, end
        )

    async def async_get_reviews(self, listing_id: str) -> dict[str, Any]:
        """Return the rating distribution and the most recent review."""
        return await self._get_dict(
            "/owners/me/reviews",
            [("listingIds[]", listing_id), ("skip", "0"), ("limit", "1")],
        )

    async def async_get_latest_statement(self) -> dict[str, Any] | None:
        """Return the most recent owner statement, if any has been shared."""
        payload = await self._get_dict(
            "/statements-v2/owner-portal",
            [("skip", "0"), ("limit", "1"), ("sort", "-periodEndDate")],
        )
        data = payload.get("data")
        if isinstance(data, list) and data and isinstance(data[0], dict):
            return data[0]
        return None

    async def async_get_unread_count(self) -> int | None:
        """Return the number of unread conversations in the portal inbox."""
        payload = await self._get_dict(
            "/inbox/owners-portal/conversations/count/unread", [("type", "owner")]
        )
        count = payload.get("countUnread")
        return count if isinstance(count, int) else None
