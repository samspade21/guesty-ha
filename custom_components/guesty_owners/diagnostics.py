"""Diagnostics for the Guesty Owners Portal integration."""

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant

from .coordinator import GuestyOwnersConfigEntry
from .models import ListingSnapshot

TO_REDACT = {
    CONF_PASSWORD,
    CONF_USERNAME,
    "address",
    "confirmation_code",
    "guest_name",
    "title",
    "name",
}


def _plain(value: Any) -> Any:
    """Turn tuples into lists, which is what redaction and JSON both expect."""
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(item) for item in value]
    return value


def _snapshot(snapshot: ListingSnapshot) -> dict[str, Any]:
    data = asdict(snapshot)
    # The timezone object is not JSON; its name is already in ``timezone``.
    data["listing"].pop("tz", None)
    return data


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: GuestyOwnersConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    stays = entry.runtime_data.stays
    stats = entry.runtime_data.stats
    return async_redact_data(
        _plain(
            {
                "entry": {"data": dict(entry.data), "options": dict(entry.options)},
                # Which sections the property manager lets this owner see.
                "portal_settings": stays.owner.get("ownersPortalSettings"),
                "stays": {
                    listing_id: _snapshot(snapshot)
                    for listing_id, snapshot in stays.data.items()
                },
                "stats": asdict(stats.data),
            }
        ),
        TO_REDACT,
    )
