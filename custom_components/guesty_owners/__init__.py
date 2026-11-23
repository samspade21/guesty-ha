"""The Guesty Owners Portal integration."""

from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .api import GuestyOwnersClient
from .const import DOMAIN
from .coordinator import (
    GuestyOwnersConfigEntry,
    GuestyOwnersRuntimeData,
    StatsCoordinator,
    StaysCoordinator,
)
from .entity import account_identifier

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.CALENDAR,
    Platform.EVENT,
    Platform.SENSOR,
]


async def async_setup_entry(
    hass: HomeAssistant, entry: GuestyOwnersConfigEntry
) -> None:
    """Set up a Guesty Owners Portal login."""
    client = GuestyOwnersClient(
        # A dedicated session keeps the portal's cookies out of the shared jar.
        async_create_clientsession(hass),
        entry.data[CONF_HOST],
        entry.data[CONF_USERNAME],
        entry.data[CONF_PASSWORD],
    )
    stays = StaysCoordinator(hass, entry, client)
    await stays.async_config_entry_first_refresh()
    stats = StatsCoordinator(hass, entry, client, stays)
    await stats.async_config_entry_first_refresh()
    entry.runtime_data = GuestyOwnersRuntimeData(
        client=client, stays=stays, stats=stats
    )

    @callback
    def remove_stale_devices() -> None:
        """Drop the devices of listings the owner no longer has."""
        if not stays.data:
            # An empty answer is far more likely a hiccup than a sold portfolio,
            # and removing a device also discards the user's customizations.
            return
        keep = {account_identifier(stays.owner_id), *stays.data}
        registry = dr.async_get(hass)
        for device in dr.async_entries_for_config_entry(registry, entry.entry_id):
            ours = {ident for domain, ident in device.identifiers if domain == DOMAIN}
            if ours and not ours & keep:
                registry.async_update_device(
                    device.id, remove_config_entry_id=entry.entry_id
                )

    @callback
    def refresh_stats_for_new_listings() -> None:
        """Fetch figures for a listing that just appeared, not an hour later."""
        if set(stays.data) - set(stats.data.listings):
            entry.async_create_background_task(
                hass, stats.async_request_refresh(), f"{DOMAIN} new listing figures"
            )

    remove_stale_devices()
    entry.async_on_unload(stays.async_add_listener(remove_stale_devices))
    entry.async_on_unload(stays.async_add_listener(refresh_stats_for_new_listings))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)


async def async_unload_entry(
    hass: HomeAssistant, entry: GuestyOwnersConfigEntry
) -> None:
    """Unload a config entry."""
    await hass.config_entries.async_unload_platforms_or_raise(entry, PLATFORMS)


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: GuestyOwnersConfigEntry, device: dr.DeviceEntry
) -> bool:
    """Allow removing a device only once its listing is gone from the portal."""
    stays = entry.runtime_data.stays
    keep = {account_identifier(stays.owner_id), *stays.data}
    return not any(
        domain == DOMAIN and ident in keep for domain, ident in device.identifiers
    )
