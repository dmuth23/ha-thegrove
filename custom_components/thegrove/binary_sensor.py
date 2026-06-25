"""Binary-sensor platform for thegrove.

P2: the fault-free boolean read set — door, per-printer link (`online`), and
per-AMS `drying` + `active`. All PUSH (CoordinatorEntity), serial-based
entity_ids + friendly names via the §4.3 mechanism (entity.py).

NOT here yet (gated on a live fault reproduction — see derive.py / P2-FINDINGS):
- `print_error` — exact `gcode_state` string + (hard-fail vs also-pause) semantics.
- the OD-E `missing_spool` / `low_filament` sensors — BB almost certainly surfaces
  these as HMS codes during a fault, not as steady `/status` fields (`fila_switch`
  reads None at idle), so their source is revealed by the reproduction.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import TheGroveConfigEntry
from .brain.derive import filament_runout, is_ams_active, print_error
from .brain.model import AmsUnit, PrinterModel
from .coordinator import PrinterCoordinator, VpCoordinator
from .entity import TheGroveAmsEntity, TheGroveEntity, TheGroveVpEntity, ams_side
from .hub import SIGNAL_NEW_PRINTER

BINARY_SENSOR = "binary_sensor"


@dataclass(frozen=True, kw_only=True)
class PrinterBinaryDescription(BinarySensorEntityDescription):
    """A printer-body binary sensor: value_fn reads the whole PrinterModel."""

    value_fn: Callable[[PrinterModel], bool | None]


@dataclass(frozen=True, kw_only=True)
class AmsBinaryDescription(BinarySensorEntityDescription):
    """A per-AMS binary sensor. value_fn takes (model, ams_id) so derivations
    that need printer-level inputs (active <- tray_now) fit the same shape as
    per-unit reads (drying <- the AmsUnit)."""

    value_fn: Callable[[PrinterModel, int], bool | None]


def _ams_drying(model: PrinterModel, ams_id: int) -> bool | None:
    """True while this AMS is running a dry cycle. dry_status==0 is idle;
    any non-zero status is an active/transitioning cycle. (Enum still wants a
    live-dry confirmation — drying is free to reproduce, no filament burned.)"""
    ams = next((a for a in model.ams if a.id == ams_id), None)
    if ams is None or ams.dry_status is None:
        return None
    return ams.dry_status != 0


PRINTER_BINARY_SENSORS: tuple[PrinterBinaryDescription, ...] = (
    PrinterBinaryDescription(
        key="print_error",
        name="Print error",
        device_class=BinarySensorDeviceClass.PROBLEM,  # on = fault active
        # (b): state==FAILED OR an allowlisted fault code (runout family) present.
        # Frame-local; keys off the precise HMS code, not the coarse gcode_state.
        value_fn=print_error,
    ),
    PrinterBinaryDescription(
        key="filament_runout",
        name="Filament runout",
        device_class=BinarySensorDeviceClass.PROBLEM,
        icon="mdi:printer-3d-nozzle-alert",
        # OD-E: BB's runout event (HMS _8011 family), surfaced standalone.
        value_fn=filament_runout,
    ),
    PrinterBinaryDescription(
        key="awaiting_plate_clear",
        name="Awaiting plate clear",
        icon="mdi:tray-alert",
        # on = a finished print is still ON THE BED, not yet cleared/acknowledged.
        # PHYSICAL plate state, NOT a queue flag: proven (Session 2) by pulling the
        # queued job — this stayed True. Persists from FINISH until the plate is
        # cleared; the computer's print queue respects it, but a printer-initiated
        # print bypasses it (runs with this still True).
        value_fn=lambda m: m.awaiting_plate_clear,
    ),
    PrinterBinaryDescription(
        key="door",
        name="Door",
        device_class=BinarySensorDeviceClass.DOOR,  # on = open
        value_fn=lambda m: m.door_open,
    ),
    PrinterBinaryDescription(
        key="store_to_sd",
        name="Store to SD",
        icon="mdi:micro-sd",
        entity_category=EntityCategory.DIAGNOSTIC,
        # on = "Store sent files on external storage" enabled. READ-ONLY (no remote
        # write in BB or the Bambu MQTT protocol — touchscreen-only). Surfaced so
        # HA flags the archiving prerequisite at a glance. REST-only -> sticky.
        value_fn=lambda m: m.store_to_sdcard,
    ),
    PrinterBinaryDescription(
        key="online",
        name="Online",
        # The frame's OWN per-printer link flag — distinct from feed_alive (the
        # shared WS socket, which gates availability). on = printer reachable.
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda m: m.connected,
    ),
)

AMS_BINARY_SENSORS: tuple[AmsBinaryDescription, ...] = (
    AmsBinaryDescription(
        key="drying",
        name="Drying",
        icon="mdi:water-boiler",
        value_fn=_ams_drying,
    ),
    AmsBinaryDescription(
        key="active",
        name="Active",
        icon="mdi:printer-3d-nozzle",
        # Is this AMS the one feeding the nozzle right now (derive._active).
        value_fn=is_ams_active,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TheGroveConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up binary sensors per printer (+ its AMS units), with dynamic-add."""
    hub = entry.runtime_data

    @callback
    def _entities_for(coord: PrinterCoordinator) -> list[BinarySensorEntity]:
        ents: list[BinarySensorEntity] = [
            PrinterBinarySensor(coord, desc) for desc in PRINTER_BINARY_SENSORS
        ]
        model = coord.data
        if model is not None:
            for ams in model.ams:
                ents += [
                    AmsBinarySensor(coord, ams.id, desc) for desc in AMS_BINARY_SENSORS
                ]
        return ents

    initial: list[BinarySensorEntity] = []
    for coord in hub.coordinators.values():
        initial += _entities_for(coord)
    # Virtual-Printer `running` sensor (separate coordinator). New VPs → reload.
    for vp in (hub.vp_coordinator.data or {}).values():
        target_serial = hub.printer_serial(vp.target_printer_id)
        initial.append(
            VpRunningBinarySensor(hub.vp_coordinator, vp.vp_id, target_serial=target_serial)
        )
    async_add_entities(initial)

    @callback
    def _add_new_printer(printer_id: int) -> None:
        coord = hub.coordinators.get(printer_id)
        if coord is not None:
            async_add_entities(_entities_for(coord))

    entry.async_on_unload(
        async_dispatcher_connect(
            hass, SIGNAL_NEW_PRINTER.format(entry.entry_id), _add_new_printer
        )
    )


class PrinterBinarySensor(TheGroveEntity, BinarySensorEntity):
    """A printer-body boolean driven by a PrinterBinaryDescription."""

    entity_description: PrinterBinaryDescription

    def __init__(
        self, coordinator: PrinterCoordinator, description: PrinterBinaryDescription
    ) -> None:
        super().__init__(
            coordinator, key=description.key, platform_domain=BINARY_SENSOR
        )
        self.entity_description = description
        self._attr_name = description.name

    @property
    def is_on(self) -> bool | None:
        model = self.coordinator.data
        if model is None:
            return None
        return self.entity_description.value_fn(model)


class AmsBinarySensor(TheGroveAmsEntity, BinarySensorEntity):
    """A per-AMS boolean on the AMS child device."""

    entity_description: AmsBinaryDescription

    def __init__(
        self,
        coordinator: PrinterCoordinator,
        ams_id: int,
        description: AmsBinaryDescription,
    ) -> None:
        key = f"ams_{ams_side(ams_id)}_{description.key}"
        super().__init__(coordinator, ams_id=ams_id, key=key, platform_domain=BINARY_SENSOR)
        self.entity_description = description
        self._attr_name = description.name

    @property
    def is_on(self) -> bool | None:
        model = self.coordinator.data
        if model is None:
            return None
        return self.entity_description.value_fn(model, self._ams_id)


class VpRunningBinarySensor(TheGroveVpEntity, BinarySensorEntity):
    """A Virtual Printer's `running` flag — on = its advertise/bind server is up
    and the slicer can reach it. Read-only, on the VP device."""

    _attr_name = "Running"
    _attr_device_class = BinarySensorDeviceClass.RUNNING

    def __init__(
        self,
        coordinator: VpCoordinator,
        vp_id: int,
        *,
        target_serial: str | None = None,
    ) -> None:
        super().__init__(
            coordinator, vp_id=vp_id, key="running", platform_domain=BINARY_SENSOR,
            target_serial=target_serial,
        )

    @property
    def is_on(self) -> bool | None:
        vp = self.vp
        return None if vp is None else vp.running
