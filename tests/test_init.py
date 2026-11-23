"""Tests for setup, unload, failure handling and device lifecycle."""

from datetime import date, timedelta

from aiohttp import ClientError
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.setup import async_setup_component
import pytest
from pytest_homeassistant.common import MockConfigEntry
from pytest_homeassistant.components.diagnostics import (
    get_diagnostics_for_config_entry,
)
from pytest_homeassistant.test_util.aiohttp import AiohttpClientMocker
from pytest_homeassistant.typing import ClientSessionGenerator

from custom_components.guesty_owners import async_remove_config_entry_device
from custom_components.guesty_owners.const import (
    API_BASE,
    CONF_DAYS_AHEAD,
    DOMAIN,
    STATS_UPDATE_INTERVAL,
    STAYS_UPDATE_INTERVAL,
)

from .conftest import HOST, LISTING_ID, OWNER_ID, FakePortal, advance

SECOND_LISTING = "11st1ng00000000000000002"


async def test_setup_and_unload(
    hass: HomeAssistant, setup_entry: MockConfigEntry, portal: FakePortal
) -> None:
    assert setup_entry.state is ConfigEntryState.LOADED
    assert portal.logins == 1
    assert hass.states.get("binary_sensor.harbor_loft_occupied").state == "on"

    assert await hass.config_entries.async_unload(setup_entry.entry_id)
    await hass.async_block_till_done()

    assert setup_entry.state is ConfigEntryState.NOT_LOADED
    assert hass.states.get("binary_sensor.harbor_loft_occupied").state == (
        STATE_UNAVAILABLE
    )


async def test_devices(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    device_registry: dr.DeviceRegistry,
) -> None:
    listing = device_registry.async_get_device_by_identifier(
        (DOMAIN, LISTING_ID), setup_entry.entry_id
    )
    assert listing is not None
    assert listing.name == "Harbor Loft"
    assert listing.model == "Apartment"
    assert listing.entry_type is dr.DeviceEntryType.SERVICE
    assert listing.configuration_url == (
        f"https://{HOST}/my-properties/{LISTING_ID}/calendar"
    )
    account = device_registry.async_get_device_by_identifier(
        (DOMAIN, f"owner_{OWNER_ID}"), setup_entry.entry_id
    )
    assert account is not None
    assert account.name == "Acme Stays"


async def test_setup_wrong_password_starts_reauth(
    hass: HomeAssistant, config_entry: MockConfigEntry, portal: FakePortal
) -> None:
    portal.password = "changed-in-the-portal"
    config_entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert config_entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [flow["context"]["source"] for flow in flows] == [SOURCE_REAUTH]


async def test_setup_offline_retries(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    aioclient_mock.post(f"{API_BASE}/owners/auth/login", exc=ClientError("boom"))
    config_entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.SETUP_RETRY


@pytest.mark.parametrize("section", ["owner", "listings"])
async def test_setup_retries_when_essentials_fail(
    hass: HomeAssistant, config_entry: MockConfigEntry, portal: FakePortal, section: str
) -> None:
    portal.fail(section, 500)
    config_entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_password_changed_while_running(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    portal: FakePortal,
    freezer,
) -> None:
    """When the session expires and the password no longer works, ask again."""
    portal.session_valid = False
    portal.password = "changed-in-the-portal"

    await advance(hass, freezer, STAYS_UPDATE_INTERVAL)

    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [flow["context"]["source"] for flow in flows] == [SOURCE_REAUTH]
    assert hass.states.get("binary_sensor.harbor_loft_occupied").state == (
        STATE_UNAVAILABLE
    )


async def test_session_expiry_is_invisible(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    portal: FakePortal,
    freezer,
) -> None:
    portal.session_valid = False

    await advance(hass, freezer, STAYS_UPDATE_INTERVAL)

    assert portal.logins == 2
    assert hass.states.get("binary_sensor.harbor_loft_occupied").state == "on"
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)


async def test_hidden_sections_leave_the_rest_working(
    hass: HomeAssistant, config_entry: MockConfigEntry, portal: FakePortal
) -> None:
    """Managers can switch sections off; those sensors are just unknown."""
    hidden = ("analytics", "occupancy", "booked_nights", "reviews", "statements")
    for section in (*hidden, "unread"):
        portal.fail(section, 403)
    config_entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get("binary_sensor.harbor_loft_occupied").state == "on"
    for entity_id in (
        "sensor.harbor_loft_owner_revenue_this_month",
        "sensor.harbor_loft_guest_rating",
        "sensor.acme_stays_unread_messages",
        "sensor.acme_stays_statement_due_to_owner",
    ):
        assert hass.states.get(entity_id).state == STATE_UNKNOWN


async def test_calendar_hidden_falls_back_to_reservation_list(
    hass: HomeAssistant, config_entry: MockConfigEntry, portal: FakePortal
) -> None:
    portal.fail("calendar", 400)
    config_entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    # The guest in the house is only visible in the calendar, so the home looks
    # vacant, but upcoming stays are still known.
    assert hass.states.get("binary_sensor.harbor_loft_occupied").state == "off"
    assert hass.states.get("sensor.harbor_loft_upcoming_stays").state == "3"
    assert hass.states.get("sensor.harbor_loft_nightly_rate").state == STATE_UNKNOWN


async def test_nothing_readable_for_a_listing(
    hass: HomeAssistant, config_entry: MockConfigEntry, portal: FakePortal
) -> None:
    portal.fail("calendar", 400)
    portal.fail("reservations", 400)
    config_entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_update_failure_and_recovery(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    portal: FakePortal,
    freezer,
) -> None:
    portal.fail("listings", 502)
    await advance(hass, freezer, STAYS_UPDATE_INTERVAL)
    assert hass.states.get("binary_sensor.harbor_loft_occupied").state == (
        STATE_UNAVAILABLE
    )

    portal.failures.clear()
    await advance(hass, freezer, STAYS_UPDATE_INTERVAL)
    assert hass.states.get("binary_sensor.harbor_loft_occupied").state == "on"


async def test_rate_limit_marks_unavailable(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    portal: FakePortal,
    freezer,
) -> None:
    portal.fail("unread", 429)
    await advance(hass, freezer, STATS_UPDATE_INTERVAL)
    assert hass.states.get("sensor.acme_stays_unread_messages").state == (
        STATE_UNAVAILABLE
    )


async def test_listing_added_and_removed(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    portal: FakePortal,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
    freezer,
) -> None:
    """Entities follow the listings the owner has in the portal."""
    portal.listings.append(
        {"_id": SECOND_LISTING, "nickname": "Lake House", "timezone": "UTC"}
    )
    await advance(hass, freezer, STAYS_UPDATE_INTERVAL)

    assert hass.states.get("binary_sensor.lake_house_occupied") is not None
    assert hass.states.get("calendar.lake_house_reservations") is not None
    # Its figures are fetched right away rather than at the next hourly refresh.
    assert hass.states.get("sensor.lake_house_guest_reviews").state == "20"
    assert device_registry.async_get_device_by_identifier(
        (DOMAIN, SECOND_LISTING), setup_entry.entry_id
    )

    portal.listings.pop()
    await advance(hass, freezer, STAYS_UPDATE_INTERVAL)

    assert not device_registry.async_get_device_by_identifier(
        (DOMAIN, SECOND_LISTING), setup_entry.entry_id
    )
    assert entity_registry.async_get("binary_sensor.lake_house_occupied") is None
    assert hass.states.get("binary_sensor.harbor_loft_occupied").state == "on"


async def test_only_stale_devices_can_be_removed(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    device_registry: dr.DeviceRegistry,
) -> None:
    listing = device_registry.async_get_device_by_identifier(
        (DOMAIN, LISTING_ID), setup_entry.entry_id
    )
    account = device_registry.async_get_device_by_identifier(
        (DOMAIN, f"owner_{OWNER_ID}"), setup_entry.entry_id
    )
    stale = device_registry.async_get_or_create(
        config_entry_id=setup_entry.entry_id, identifiers={(DOMAIN, "sold-long-ago")}
    )

    assert not await async_remove_config_entry_device(hass, setup_entry, listing)
    assert not await async_remove_config_entry_device(hass, setup_entry, account)
    assert await async_remove_config_entry_device(hass, setup_entry, stale)


async def test_days_ahead_option(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    portal: FakePortal,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(config_entry, options={CONF_DAYS_AHEAD: 30})
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    calendar_calls = [
        url
        for _, url, _, _ in aioclient_mock.mock_calls
        if url.path.endswith("calendar")
    ]
    assert calendar_calls[0].query["from"] == "2026-06-08"
    assert (
        calendar_calls[0].query["to"] == (date(2026, 6, 10) + timedelta(30)).isoformat()
    )


async def test_diagnostics_redact_private_data(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    hass_client: ClientSessionGenerator,
) -> None:
    assert await async_setup_component(hass, "diagnostics", {})
    diagnostics = await get_diagnostics_for_config_entry(hass, hass_client, setup_entry)

    text = str(diagnostics)
    for secret in ("correct-horse", "owner@example.com", "Alex Example", "Example Way"):
        assert secret not in text
    assert diagnostics["entry"]["data"]["password"] == "**REDACTED**"
    assert diagnostics["portal_settings"] == {
        "showInbox": True,
        "netRentalIncome": True,
    }
    stays = diagnostics["stays"][LISTING_ID]["stays"]
    assert len(stays) == 4
    assert stays[0]["guest_name"] == "**REDACTED**"
    assert stays[0]["check_in"] == "2026-06-08T23:00:00+00:00"
    assert diagnostics["stats"]["unread_messages"] == 3


async def test_empty_listing_answer_keeps_devices(
    hass: HomeAssistant,
    setup_entry: MockConfigEntry,
    portal: FakePortal,
    device_registry: dr.DeviceRegistry,
    freezer,
) -> None:
    """A momentarily empty listing answer must not wipe the user's devices."""
    portal.listings, kept = [], portal.listings
    await advance(hass, freezer, STAYS_UPDATE_INTERVAL)

    assert device_registry.async_get_device_by_identifier(
        (DOMAIN, LISTING_ID), setup_entry.entry_id
    )
    assert hass.states.get("binary_sensor.harbor_loft_occupied").state == (
        STATE_UNAVAILABLE
    )

    portal.listings = kept
    await advance(hass, freezer, STAYS_UPDATE_INTERVAL)
    assert hass.states.get("binary_sensor.harbor_loft_occupied").state == "on"


async def test_section_answering_401_is_not_a_password_problem(
    hass: HomeAssistant, config_entry: MockConfigEntry, portal: FakePortal
) -> None:
    """A section that refuses a brand-new session is hidden, not a bad login."""
    portal.fail("reviews", 401)
    config_entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert config_entry.state is ConfigEntryState.LOADED
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert hass.states.get("sensor.harbor_loft_guest_rating").state == STATE_UNKNOWN
    assert hass.states.get("sensor.harbor_loft_guest_reviews").state == STATE_UNKNOWN
    assert hass.states.get("binary_sensor.harbor_loft_occupied").state == "on"
