"""thegrove — in-house Home Assistant integration bridging Bambuddy.

One in-house integration is the SOLE bridge to the Bambu printers + AMS,
replacing ha-bambulab and the ad-hoc Bambuddy REST glue. A thin MAPPER over
Bambuddy's already-decoded `printer_status` (WS read + REST write), structured
as a multi-printer hub: one Bambuddy connection, one HA device per printer.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.device_registry import DeviceEntry

from .const import DOMAIN
from .hub import TheGroveHub
from .services import async_setup_services, async_unload_services

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.IMAGE,
    Platform.LIGHT,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]

type TheGroveConfigEntry = ConfigEntry[TheGroveHub]


async def async_setup_entry(hass: HomeAssistant, entry: TheGroveConfigEntry) -> bool:
    """Set up the Bambuddy hub for one config entry."""
    hub = TheGroveHub(hass, entry)
    try:
        await hub.async_setup()
    except Exception as err:  # noqa: BLE001 - normalize to a retryable error
        raise ConfigEntryNotReady(
            f"Bambuddy not reachable at {entry.data.get('host')}: {err}"
        ) from err

    entry.runtime_data = hub
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    async_setup_services(hass)  # idempotent; integration-level write services
    entry.async_on_unload(entry.add_update_listener(_async_reload_on_update))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: TheGroveConfigEntry) -> bool:
    """Tear down: unload platforms, then cancel the WS task + timers cleanly."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        await entry.runtime_data.async_shutdown()
        # Remove the global services once the last thegrove entry is gone.
        remaining = [
            e for e in hass.config_entries.async_entries(DOMAIN)
            if e.entry_id != entry.entry_id
        ]
        if not remaining:
            async_unload_services(hass)
    return unload_ok


async def _async_reload_on_update(
    hass: HomeAssistant, entry: TheGroveConfigEntry
) -> None:
    """Reload on options change — exercises a full clean teardown + re-setup."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: TheGroveConfigEntry, device: DeviceEntry
) -> bool:
    """Allow removing a device whose printer Bambuddy no longer reports."""
    hub = entry.runtime_data
    live_serials = {c.identity.serial for c in hub.coordinators.values()}
    return not any(
        ident[1] in live_serials for ident in device.identifiers if ident[0] == DOMAIN
    )
