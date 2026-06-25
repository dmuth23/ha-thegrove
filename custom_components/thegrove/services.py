"""Integration-level services — write commands that target the printer DEVICE
rather than a single entity: the filament PROFILE PUSH (configure_slot, the
SpoolTap-relevant write), skip-objects, calibration, and full-parameter drying.

Each service takes a `device` (the printer); the handler resolves it to the
hub's REST client + printer_id and calls the mock-proven wrapper, fail-loud.
Registered once per HA instance (idempotent), removed when the last entry unloads.
"""

from __future__ import annotations

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr

from .bambuddy.rest_client import BambuddyApiError, BambuddyRestClient
from .const import DOMAIN

SERVICE_CONFIGURE_SLOT = "configure_slot"
SERVICE_SKIP_OBJECTS = "skip_objects"
SERVICE_START_CALIBRATION = "start_calibration"
SERVICE_START_DRYING = "start_drying"

_SERVICES = (
    SERVICE_CONFIGURE_SLOT,
    SERVICE_SKIP_OBJECTS,
    SERVICE_START_CALIBRATION,
    SERVICE_START_DRYING,
)

ATTR_DEVICE = "device_id"
_DEVICE = {vol.Required(ATTR_DEVICE): vol.Any(cv.string, [cv.string])}

CONFIGURE_SCHEMA = vol.Schema({
    **_DEVICE,
    vol.Required("ams_id"): vol.Coerce(int),
    vol.Required("tray_id"): vol.Coerce(int),
    vol.Required("tray_info_idx"): cv.string,
    vol.Required("tray_type"): cv.string,
    vol.Required("tray_sub_brands"): cv.string,
    vol.Required("tray_color"): cv.string,
    vol.Required("nozzle_temp_min"): vol.Coerce(int),
    vol.Required("nozzle_temp_max"): vol.Coerce(int),
    vol.Optional("cali_idx", default=-1): vol.Coerce(int),
    vol.Optional("nozzle_diameter", default="0.4"): cv.string,
    vol.Optional("setting_id", default=""): cv.string,
    vol.Optional("k_value", default=0.0): vol.Coerce(float),
})

SKIP_SCHEMA = vol.Schema({
    **_DEVICE,
    vol.Required("object_ids"): vol.All(cv.ensure_list, [vol.Coerce(int)], vol.Length(min=1)),
})

CALIBRATION_SCHEMA = vol.Schema({
    **_DEVICE,
    vol.Optional("bed_leveling", default=False): cv.boolean,
    vol.Optional("vibration", default=False): cv.boolean,
    vol.Optional("motor_noise", default=False): cv.boolean,
    vol.Optional("nozzle_offset", default=False): cv.boolean,
    vol.Optional("high_temp_heatbed", default=False): cv.boolean,
})

DRYING_SCHEMA = vol.Schema({
    **_DEVICE,
    vol.Required("ams_id"): vol.Coerce(int),
    vol.Optional("temp", default=45): vol.All(vol.Coerce(int), vol.Range(min=45, max=85)),
    vol.Optional("duration", default=8): vol.All(vol.Coerce(int), vol.Range(min=1, max=24)),
    vol.Optional("filament", default=""): cv.string,
    vol.Optional("rotate_tray", default=False): cv.boolean,
})


def _resolve(hass: HomeAssistant, device_id: str) -> tuple[BambuddyRestClient, int]:
    """device_id -> (rest client, printer_id). Accepts the printer device OR one
    of its AMS child devices (identifier `{serial}_ams_{id}`). Raises
    HomeAssistantError if it isn't a known thegrove printer."""
    dev = dr.async_get(hass).async_get(device_id)
    if dev is None:
        raise HomeAssistantError(f"Unknown device {device_id}")
    idents = {i[1] for i in dev.identifiers if i[0] == DOMAIN}
    for entry_id in dev.config_entries:
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is None or entry.domain != DOMAIN:
            continue
        hub = entry.runtime_data
        for pid, coord in hub.coordinators.items():
            serial = coord.identity.serial
            if any(x == serial or x.startswith(f"{serial}_") for x in idents):
                return hub.rest, pid
    raise HomeAssistantError(f"Device {device_id} is not a thegrove printer")


def _devices(call: ServiceCall) -> list[str]:
    d = call.data[ATTR_DEVICE]
    return d if isinstance(d, list) else [d]


async def _run(hass: HomeAssistant, call: ServiceCall, action: str, fn) -> None:
    """For each targeted device: resolve -> invoke fn(rest, printer_id), fail-loud."""
    for device_id in _devices(call):
        rest, pid = _resolve(hass, device_id)
        try:
            await fn(rest, pid)
        except BambuddyApiError as err:
            raise HomeAssistantError(f"{action} failed — {err.detail}") from err
        except HomeAssistantError:
            raise
        except Exception as err:  # noqa: BLE001 - network/timeout -> user-facing
            raise HomeAssistantError(f"{action} failed — {err}") from err


def async_setup_services(hass: HomeAssistant) -> None:
    """Register the services once per HA instance (idempotent)."""
    if hass.services.has_service(DOMAIN, SERVICE_CONFIGURE_SLOT):
        return

    async def configure_slot(call: ServiceCall) -> None:
        d = call.data
        await _run(hass, call, "Configure slot", lambda rest, pid: rest.configure_slot(
            pid, d["ams_id"], d["tray_id"],
            tray_info_idx=d["tray_info_idx"], tray_type=d["tray_type"],
            tray_sub_brands=d["tray_sub_brands"], tray_color=d["tray_color"],
            nozzle_temp_min=d["nozzle_temp_min"], nozzle_temp_max=d["nozzle_temp_max"],
            cali_idx=d["cali_idx"], nozzle_diameter=d["nozzle_diameter"],
            setting_id=d["setting_id"], k_value=d["k_value"],
        ))

    async def skip_objects(call: ServiceCall) -> None:
        ids = call.data["object_ids"]
        await _run(hass, call, "Skip objects",
                   lambda rest, pid: rest.skip_objects(pid, ids))

    async def start_calibration(call: ServiceCall) -> None:
        d = call.data
        await _run(hass, call, "Start calibration", lambda rest, pid: rest.start_calibration(
            pid, bed_leveling=d["bed_leveling"], vibration=d["vibration"],
            motor_noise=d["motor_noise"], nozzle_offset=d["nozzle_offset"],
            high_temp_heatbed=d["high_temp_heatbed"],
        ))

    async def start_drying(call: ServiceCall) -> None:
        d = call.data
        await _run(hass, call, "Start drying", lambda rest, pid: rest.start_drying(
            pid, d["ams_id"], temp=d["temp"], duration=d["duration"],
            filament=d["filament"], rotate_tray=d["rotate_tray"],
        ))

    hass.services.async_register(DOMAIN, SERVICE_CONFIGURE_SLOT, configure_slot, schema=CONFIGURE_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_SKIP_OBJECTS, skip_objects, schema=SKIP_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_START_CALIBRATION, start_calibration, schema=CALIBRATION_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_START_DRYING, start_drying, schema=DRYING_SCHEMA)


def async_unload_services(hass: HomeAssistant) -> None:
    """Remove the services (called when the last entry unloads)."""
    for name in _SERVICES:
        hass.services.async_remove(DOMAIN, name)
