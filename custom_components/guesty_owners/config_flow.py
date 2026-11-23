"""Config flow for the Guesty Owners Portal integration."""

from collections.abc import Mapping
import logging
from typing import Any, override

from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_create_clientsession
import probatio

from .api import (
    GuestyOwnersAuthError,
    GuestyOwnersClient,
    GuestyOwnersConnectionError,
    GuestyOwnersError,
    GuestyOwnersPortalNotFoundError,
    normalize_hostname,
)
from .const import (
    CONF_DAYS_AHEAD,
    DEFAULT_DAYS_AHEAD,
    DOMAIN,
    MAX_DAYS_AHEAD,
    MIN_DAYS_AHEAD,
)
from .coordinator import GuestyOwnersConfigEntry

_LOGGER = logging.getLogger(__name__)

_EMAIL_SELECTOR = selector.TextSelector(
    selector.TextSelectorConfig(
        type=selector.TextSelectorType.EMAIL, autocomplete="username"
    )
)
_PASSWORD_SELECTOR = selector.TextSelector(
    selector.TextSelectorConfig(
        type=selector.TextSelectorType.PASSWORD, autocomplete="current-password"
    )
)

STEP_USER_SCHEMA = probatio.Schema(
    {
        probatio.Required(CONF_HOST): selector.TextSelector(
            selector.TextSelectorConfig(autocomplete="off")
        ),
        probatio.Required(CONF_USERNAME): _EMAIL_SELECTOR,
        probatio.Required(probatio.Secret(CONF_PASSWORD)): _PASSWORD_SELECTOR,
    }
)

STEP_REAUTH_SCHEMA = probatio.Schema(
    {probatio.Required(probatio.Secret(CONF_PASSWORD)): _PASSWORD_SELECTOR}
)


def _without_password(values: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return the values worth showing again in a form."""
    return {
        key: value
        for key, value in (values or {}).items()
        if key in (CONF_HOST, CONF_USERNAME)
    }


class GuestyOwnersConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for a Guesty Owners Portal login."""

    VERSION = 1

    async def _async_validate(
        self, host: str, username: str, password: str
    ) -> tuple[dict[str, str], dict[str, Any]]:
        """Log in and return (errors, details of the account that logged in)."""
        try:
            hostname = normalize_hostname(host)
        except ValueError:
            return {CONF_HOST: "invalid_host"}, {}

        # A throwaway session, so one account's cookies never reach another's login.
        session = async_create_clientsession(self.hass)
        client = GuestyOwnersClient(session, hostname, username, password)
        try:
            meta = await client.async_get_portal_meta()
            await client.async_login()
            owner = await client.async_get_owner()
        except GuestyOwnersPortalNotFoundError:
            return {CONF_HOST: "portal_not_found"}, {}
        except GuestyOwnersAuthError:
            return {"base": "invalid_auth"}, {}
        except GuestyOwnersConnectionError:
            return {"base": "cannot_connect"}, {}
        except GuestyOwnersError:
            _LOGGER.exception("Unexpected response from the Guesty Owners Portal")
            return {"base": "unknown"}, {}
        finally:
            session.detach()

        owner_id = owner.get("_id")
        if not isinstance(owner_id, str) or not owner_id:
            _LOGGER.error("The Guesty Owners Portal did not return an owner ID")
            return {"base": "unknown"}, {}

        account = meta.get("account")
        portal_name = account.get("name") if isinstance(account, dict) else None
        return {}, {
            "owner_id": owner_id,
            "title": portal_name if isinstance(portal_name, str) else hostname,
            "data": {
                CONF_HOST: hostname,
                CONF_USERNAME: username,
                CONF_PASSWORD: password,
            },
        }

    @override
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the portal address and the owner's login."""
        errors: dict[str, str] = {}
        if user_input is not None:
            errors, account = await self._async_validate(
                user_input[CONF_HOST],
                user_input[CONF_USERNAME],
                user_input[CONF_PASSWORD],
            )
            if not errors:
                await self.async_set_unique_id(account["owner_id"])
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=account["title"], data=account["data"]
                )

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                STEP_USER_SCHEMA, _without_password(user_input)
            ),
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start again after the portal rejected the stored password."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the new password."""
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        if user_input is not None:
            errors, account = await self._async_validate(
                entry.data[CONF_HOST],
                entry.data[CONF_USERNAME],
                user_input[CONF_PASSWORD],
            )
            if not errors:
                await self.async_set_unique_id(account["owner_id"])
                self._abort_if_unique_id_mismatch(reason="wrong_account")
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_PASSWORD: user_input[CONF_PASSWORD]}
                )
            # The portal field is not shown here, so report its errors generally.
            if CONF_HOST in errors:
                errors = {"base": errors[CONF_HOST]}

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=STEP_REAUTH_SCHEMA,
            errors=errors,
            description_placeholders={
                "username": entry.data[CONF_USERNAME],
                "portal": entry.data[CONF_HOST],
            },
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the portal address, email or password of an existing entry."""
        errors: dict[str, str] = {}
        entry = self._get_reconfigure_entry()
        if user_input is not None:
            errors, account = await self._async_validate(
                user_input[CONF_HOST],
                user_input[CONF_USERNAME],
                user_input[CONF_PASSWORD],
            )
            if not errors:
                await self.async_set_unique_id(account["owner_id"])
                self._abort_if_unique_id_mismatch(reason="wrong_account")
                return self.async_update_reload_and_abort(
                    entry, data_updates=account["data"]
                )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                STEP_USER_SCHEMA, _without_password(user_input or entry.data)
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    @override
    def async_get_options_flow(
        config_entry: GuestyOwnersConfigEntry,
    ) -> GuestyOwnersOptionsFlow:
        """Return the options flow."""
        return GuestyOwnersOptionsFlow()


class GuestyOwnersOptionsFlow(OptionsFlowWithReload):
    """Let the user choose how far ahead the calendar is read."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(
                data={CONF_DAYS_AHEAD: int(user_input[CONF_DAYS_AHEAD])}
            )

        schema = probatio.Schema(
            {
                probatio.Required(CONF_DAYS_AHEAD): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=MIN_DAYS_AHEAD,
                        max=MAX_DAYS_AHEAD,
                        step=1,
                        mode=selector.NumberSelectorMode.BOX,
                        unit_of_measurement="days",
                    )
                )
            }
        )
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                schema,
                {
                    CONF_DAYS_AHEAD: self.config_entry.options.get(
                        CONF_DAYS_AHEAD, DEFAULT_DAYS_AHEAD
                    )
                },
            ),
        )
