"""Tests for the config flow."""

from aiohttp import ClientError
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
import pytest
from pytest_homeassistant.common import MockConfigEntry
from pytest_homeassistant.test_util.aiohttp import AiohttpClientMocker

from custom_components.guesty_owners.const import (
    API_BASE,
    CONF_DAYS_AHEAD,
    DEFAULT_DAYS_AHEAD,
    DOMAIN,
)

from .conftest import HOST, OWNER_ID, PASSWORD, USERNAME, FakePortal

USER_INPUT = {CONF_HOST: "Acme", CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD}
ENTRY_DATA = {CONF_HOST: HOST, CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD}


async def test_user_flow(hass: HomeAssistant, portal: FakePortal) -> None:
    """A bare portal name plus a working login creates the entry."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert not result["errors"]

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Acme Stays"
    assert result["data"] == ENTRY_DATA
    assert result["result"].unique_id == OWNER_ID


@pytest.mark.parametrize(
    ("user_input", "errors"),
    [
        ({**USER_INPUT, CONF_HOST: "not a host"}, {CONF_HOST: "invalid_host"}),
        ({**USER_INPUT, CONF_HOST: "other"}, {CONF_HOST: "portal_not_found"}),
        ({**USER_INPUT, CONF_PASSWORD: "wrong"}, {"base": "invalid_auth"}),
    ],
)
async def test_user_flow_errors_then_recovers(
    hass: HomeAssistant,
    portal: FakePortal,
    user_input: dict[str, str],
    errors: dict[str, str],
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == errors

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_user_flow_cannot_connect(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(
        f"{API_BASE}/owners/auth/portals/meta/{HOST}", exc=ClientError("boom")
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}, data=USER_INPUT
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


@pytest.mark.parametrize(
    ("section", "owner"), [("owner", None), (None, {"_id": None}), (None, {})]
)
async def test_user_flow_unexpected_response(
    hass: HomeAssistant,
    portal: FakePortal,
    section: str | None,
    owner: dict[str, object] | None,
) -> None:
    if section:
        portal.fail(section, 500)
    if owner is not None:
        portal.owner = owner
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}, data=USER_INPUT
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "unknown"}


async def test_user_flow_already_configured(
    hass: HomeAssistant, portal: FakePortal, config_entry: MockConfigEntry
) -> None:
    config_entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}, data=USER_INPUT
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth_flow(
    hass: HomeAssistant, portal: FakePortal, config_entry: MockConfigEntry
) -> None:
    """A new password replaces the stored one without creating a second entry."""
    config_entry.add_to_hass(hass)
    portal.password = "new-password"

    result = await config_entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    assert result["description_placeholders"]["username"] == USERNAME
    assert result["description_placeholders"]["portal"] == HOST

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: PASSWORD}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: "new-password"}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert config_entry.data == {**ENTRY_DATA, CONF_PASSWORD: "new-password"}
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


async def test_reauth_flow_portal_gone(hass: HomeAssistant, portal: FakePortal) -> None:
    """An error about the hidden portal field still reaches the user."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=OWNER_ID,
        data={**ENTRY_DATA, CONF_HOST: "gone.guestyowners.com"},
    )
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: PASSWORD}
    )
    assert result["errors"] == {"base": "portal_not_found"}


async def test_reauth_flow_wrong_account(
    hass: HomeAssistant, portal: FakePortal, config_entry: MockConfigEntry
) -> None:
    config_entry.add_to_hass(hass)
    portal.owner = {"_id": "somebody-else"}

    result = await config_entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: PASSWORD}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_account"
    assert config_entry.data == ENTRY_DATA


async def test_reconfigure_flow(
    hass: HomeAssistant, portal: FakePortal, config_entry: MockConfigEntry
) -> None:
    config_entry.add_to_hass(hass)
    portal.password = "new-password"

    result = await config_entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["errors"] == {"base": "invalid_auth"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**USER_INPUT, CONF_PASSWORD: "new-password"}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert config_entry.data[CONF_PASSWORD] == "new-password"


async def test_reconfigure_flow_wrong_account(
    hass: HomeAssistant, portal: FakePortal, config_entry: MockConfigEntry
) -> None:
    config_entry.add_to_hass(hass)
    portal.owner = {"_id": "somebody-else"}

    result = await config_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_account"


async def test_options_flow(hass: HomeAssistant, setup_entry: MockConfigEntry) -> None:
    result = await hass.config_entries.options.async_init(setup_entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"
    assert DEFAULT_DAYS_AHEAD == 90

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_DAYS_AHEAD: 180}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert setup_entry.options == {CONF_DAYS_AHEAD: 180}
