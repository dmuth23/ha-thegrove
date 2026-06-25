"""Sensor platform for thegrove — the read "gauges".

P2: the printer-body read set + per-AMS + per-tray sensors, all PUSH
(`should_poll=False` via CoordinatorEntity), with serial-based entity_ids
(`sensor.p2s_<serial>_*`) and human friendly names via the §4.3 mechanism
(see entity.py). Entity set is driven off the OBSERVED live frame, not the wiki.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    EntityCategory,
    UnitOfMass,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import StateType

from . import TheGroveConfigEntry
from .brain.derive import active_material, is_tray_active, real_hms
from .brain.model import AmsUnit, PrinterModel, Tray
from .coordinator import PrinterCoordinator, VpCoordinator
from .entity import TheGroveAmsEntity, TheGroveEntity, TheGroveVpEntity, ams_side
from .hub import SIGNAL_NEW_PRINTER

# --- platform value type ----------------------------------------------------
SENSOR = "sensor"


@dataclass(frozen=True, kw_only=True)
class PrinterSensorDescription(SensorEntityDescription):
    """A printer-body sensor: value_fn reads the whole PrinterModel."""

    value_fn: Callable[[PrinterModel], StateType]


@dataclass(frozen=True, kw_only=True)
class AmsSensorDescription(SensorEntityDescription):
    """A per-AMS sensor: value_fn reads one AmsUnit."""

    value_fn: Callable[[AmsUnit], StateType]


# --- printer-body sensors ---------------------------------------------------
PRINTER_SENSORS: tuple[PrinterSensorDescription, ...] = (
    PrinterSensorDescription(
        key="print_status",
        name="Print status",
        icon="mdi:printer-3d",
        value_fn=lambda m: m.state,
    ),
    # Entity-contract gaps the SpoolTap/AMS-Dry rewrite consumes (P4 handoff):
    PrinterSensorDescription(
        key="task_name",
        name="Task name",
        icon="mdi:format-title",
        # The current print job's name (= ha-bambulab's task_name).
        value_fn=lambda m: m.subtask_name,
    ),
    PrinterSensorDescription(
        key="active_material",
        name="Active material",
        icon="mdi:printer-3d-nozzle",
        # Material loaded to the nozzle now (AMS tray OR external spool) — the
        # door/notification consumers' `active_tray | upper` read.
        value_fn=active_material,
    ),
    PrinterSensorDescription(
        key="stage",
        name="Stage",
        icon="mdi:printer-3d-nozzle",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda m: m.stg_cur_name,
    ),
    PrinterSensorDescription(
        key="progress",
        name="Progress",
        icon="mdi:percent",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda m: m.progress,
    ),
    PrinterSensorDescription(
        key="remaining_time",
        name="Remaining time",
        icon="mdi:timer-sand",
        native_unit_of_measurement=UnitOfTime.MINUTES,
        device_class=SensorDeviceClass.DURATION,
        value_fn=lambda m: m.remaining_time,
    ),
    PrinterSensorDescription(
        key="current_layer",
        name="Current layer",
        icon="mdi:layers",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda m: m.layer_num,
    ),
    PrinterSensorDescription(
        key="total_layers",
        name="Total layers",
        icon="mdi:layers-triple",
        value_fn=lambda m: m.total_layers,
    ),
    PrinterSensorDescription(
        key="nozzle_temp",
        name="Nozzle temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda m: m.nozzle_temp,
    ),
    PrinterSensorDescription(
        key="nozzle_target_temp",
        name="Nozzle target temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda m: m.nozzle_target_temp,
    ),
    PrinterSensorDescription(
        key="bed_temp",
        name="Bed temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda m: m.bed_temp,
    ),
    PrinterSensorDescription(
        key="bed_target_temp",
        name="Bed target temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda m: m.bed_target_temp,
    ),
    PrinterSensorDescription(
        key="chamber_temp",
        name="Chamber temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda m: m.chamber_temp,
    ),
    PrinterSensorDescription(
        key="cooling_fan",
        name="Cooling fan",
        icon="mdi:fan",
        native_unit_of_measurement=PERCENTAGE,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda m: m.cooling_fan_speed,
    ),
    PrinterSensorDescription(
        key="aux_fan",
        name="Aux fan",
        icon="mdi:fan",
        native_unit_of_measurement=PERCENTAGE,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda m: m.big_fan1_speed,
    ),
    PrinterSensorDescription(
        key="chamber_fan",
        name="Chamber fan",
        icon="mdi:fan",
        native_unit_of_measurement=PERCENTAGE,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda m: m.big_fan2_speed,
    ),
    PrinterSensorDescription(
        key="wifi_signal",
        name="Wi-Fi signal",
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda m: m.wifi_signal,
    ),
    PrinterSensorDescription(
        key="speed_level",
        name="Speed level",
        icon="mdi:speedometer",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda m: m.speed_level,
    ),
    PrinterSensorDescription(
        key="firmware_version",
        name="Firmware version",
        icon="mdi:chip",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda m: m.firmware_version,
    ),
    # Archive-sourced (REST-only, sticky-merged). Populate once the print has a
    # current_archive_id; the no-archive_id matcher fallback (touchscreen/SD
    # prints) is the remaining P2 archive item.
    PrinterSensorDescription(
        key="print_weight",
        name="Print weight",
        icon="mdi:weight-gram",
        native_unit_of_measurement=UnitOfMass.GRAMS,
        device_class=SensorDeviceClass.WEIGHT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda m: m.print_weight_grams,  # filament used this print (g)
    ),
    PrinterSensorDescription(
        key="print_start_time",
        name="Print start time",
        icon="mdi:clock-start",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda m: m.print_start_time,  # tz-aware UTC
    ),
)

# --- per-AMS sensors --------------------------------------------------------
AMS_SENSORS: tuple[AmsSensorDescription, ...] = (
    AmsSensorDescription(
        key="humidity",
        name="Humidity",
        native_unit_of_measurement=PERCENTAGE,
        device_class=SensorDeviceClass.HUMIDITY,
        state_class=SensorStateClass.MEASUREMENT,
        # raw % pass-through; NO ≤5 hard-blank (OD-A locked). See §5 caveat.
        value_fn=lambda a: a.humidity,
    ),
    AmsSensorDescription(
        key="temperature",
        name="Temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda a: a.temp,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TheGroveConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up sensors for every known printer (+ its AMS/trays), plus
    dynamically-added printers via the new-printer dispatcher."""
    hub = entry.runtime_data

    @callback
    def _entities_for(coord: PrinterCoordinator) -> list[SensorEntity]:
        ents: list[SensorEntity] = [
            PrinterSensor(coord, desc) for desc in PRINTER_SENSORS
        ]
        ents.append(HmsSensor(coord))
        ents.append(ExternalSpoolSensor(coord))
        ents.append(PrintableObjectsSensor(coord))
        ents.append(SlotPresetsSensor(coord))
        model = coord.data
        if model is not None:
            for ams in model.ams:
                ents += [AmsSensor(coord, ams.id, desc) for desc in AMS_SENSORS]
                ents += [TraySensor(coord, ams.id, t.id) for t in ams.trays]
        return ents

    initial: list[SensorEntity] = []
    for coord in hub.coordinators.values():
        initial += _entities_for(coord)
    # Virtual-Printer queue-depth sensor (separate coordinator). New VPs → reload.
    for vp in (hub.vp_coordinator.data or {}).values():
        target_serial = hub.printer_serial(vp.target_printer_id)
        initial.append(
            VpPendingFilesSensor(hub.vp_coordinator, vp.vp_id, target_serial=target_serial)
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


class PrinterSensor(TheGroveEntity, SensorEntity):
    """A printer-body sensor driven by a PrinterSensorDescription."""

    entity_description: PrinterSensorDescription

    def __init__(
        self, coordinator: PrinterCoordinator, description: PrinterSensorDescription
    ) -> None:
        super().__init__(coordinator, key=description.key, platform_domain=SENSOR)
        self.entity_description = description
        self._attr_name = description.name

    @property
    def native_value(self) -> StateType:
        model = self.coordinator.data
        if model is None:
            return None
        return self.entity_description.value_fn(model)


class AmsSensor(TheGroveAmsEntity, SensorEntity):
    """A per-AMS sensor (humidity/temp) on the AMS child device."""

    entity_description: AmsSensorDescription

    def __init__(
        self,
        coordinator: PrinterCoordinator,
        ams_id: int,
        description: AmsSensorDescription,
    ) -> None:
        key = f"ams_{ams_side(ams_id)}_{description.key}"
        super().__init__(
            coordinator, ams_id=ams_id, key=key, platform_domain=SENSOR
        )
        self.entity_description = description
        self._attr_name = description.name

    def _ams(self) -> AmsUnit | None:
        model = self.coordinator.data
        if model is None:
            return None
        return next((a for a in model.ams if a.id == self._ams_id), None)

    @property
    def native_value(self) -> StateType:
        ams = self._ams()
        if ams is None:
            return None
        return self.entity_description.value_fn(ams)


class TraySensor(TheGroveAmsEntity, SensorEntity):
    """One AMS tray slot. STATE = material string (§H/H2); slot details as
    attributes (filament_id, color, temp range, remain, tag) for SpoolTap."""

    _attr_icon = "mdi:printer-3d-nozzle"

    def __init__(
        self, coordinator: PrinterCoordinator, ams_id: int, tray_id: int
    ) -> None:
        # tray numbering is 1-based for humans ("Tray 1"); BB tray.id is 0-based.
        human = tray_id + 1
        key = f"ams_{ams_side(ams_id)}_tray_{human}"
        super().__init__(
            coordinator, ams_id=ams_id, key=key, platform_domain=SENSOR
        )
        self._tray_id = tray_id
        self._attr_name = f"Tray {human}"

    def _tray(self) -> Tray | None:
        model = self.coordinator.data
        if model is None:
            return None
        ams = next((a for a in model.ams if a.id == self._ams_id), None)
        if ams is None:
            return None
        return next((t for t in ams.trays if t.id == self._tray_id), None)

    @property
    def native_value(self) -> StateType:
        tray = self._tray()
        if tray is None:
            return None
        # Empty sentinel: BB sends "" -> mapper None. Surface "Empty" so the
        # SpoolTap empty-gate + AMS-Dry material table read meaningfully. Exact
        # BB-native empty sentinel is a P4 OD-F enumeration item.
        return tray.tray_type or "Empty"

    @property
    def extra_state_attributes(self) -> dict[str, StateType]:
        tray = self._tray()
        if tray is None:
            return {}
        model = self.coordinator.data
        active = (
            is_tray_active(model, self._ams_id, self._tray_id)
            if model is not None
            else None
        )
        preset = None
        if model is not None:
            preset = next(
                (name for a, t, _pid, name in model.slot_presets
                 if a == self._ams_id and t == self._tray_id),
                None,
            )
        return {
            "filament_id": tray.tray_info_idx,  # =ha-bambulab filament_id
            "color": tray.tray_color,
            "nozzle_temp_min": tray.nozzle_temp_min,
            "nozzle_temp_max": tray.nozzle_temp_max,
            "remain": tray.remain,
            "tag_uid": tray.tag_uid,
            "state": tray.state,
            "active": active,  # this tray loaded to the nozzle (derive._active)
            "cali_idx": tray.cali_idx,  # the slot's K-profile index (k-profiles)
            "preset": preset,  # saved slot preset name (slot-presets), if any
        }


class ExternalSpoolSensor(TheGroveEntity, SensorEntity):
    """The external spool (BB `vt_tray`, id 254) — a Tray-shaped slot that hangs
    off the PRINTER (not an AMS). STATE = material string ("Empty" when none);
    slot details as attributes, mirroring TraySensor for SpoolTap parity."""

    _attr_icon = "mdi:printer-3d-nozzle-outline"

    def __init__(self, coordinator: PrinterCoordinator) -> None:
        super().__init__(coordinator, key="external_spool", platform_domain=SENSOR)
        self._attr_name = "External spool"

    def _vt(self) -> Tray | None:
        model = self.coordinator.data
        return None if model is None else model.vt_tray

    @property
    def native_value(self) -> StateType:
        vt = self._vt()
        if vt is None:
            return None
        return vt.tray_type or "Empty"

    @property
    def extra_state_attributes(self) -> dict[str, StateType]:
        vt = self._vt()
        if vt is None:
            return {}
        return {
            "filament_id": vt.tray_info_idx,
            "color": vt.tray_color,
            "nozzle_temp_min": vt.nozzle_temp_min,
            "nozzle_temp_max": vt.nozzle_temp_max,
            "remain": vt.remain,
            "tag_uid": vt.tag_uid,
            "cali_idx": vt.cali_idx,
        }


class PrintableObjectsSensor(TheGroveEntity, SensorEntity):
    """The active print's skippable objects. STATE = count; the id+name list is in
    the `objects` attribute — use those ids with the `thegrove.skip_objects`
    service (HA has no on-plate object picker)."""

    _attr_icon = "mdi:format-list-numbered"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: PrinterCoordinator) -> None:
        super().__init__(coordinator, key="printable_objects", platform_domain=SENSOR)
        self._attr_name = "Printable objects"

    @property
    def native_value(self) -> StateType:
        model = self.coordinator.data
        return None if model is None else len(model.printable_objects)

    @property
    def extra_state_attributes(self) -> dict[str, StateType]:
        model = self.coordinator.data
        if model is None:
            return {}
        return {"objects": [{"id": i, "name": n} for i, n in model.printable_objects]}


class SlotPresetsSensor(TheGroveEntity, SensorEntity):
    """Saved AMS slot -> filament-preset mappings (BB's auto-reassign presets).
    STATE = count; the per-slot mappings (incl. preset_name) are in attributes."""

    _attr_icon = "mdi:content-save-cog-outline"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: PrinterCoordinator) -> None:
        super().__init__(coordinator, key="slot_presets", platform_domain=SENSOR)
        self._attr_name = "Slot presets"

    @property
    def native_value(self) -> StateType:
        model = self.coordinator.data
        return None if model is None else len(model.slot_presets)

    @property
    def extra_state_attributes(self) -> dict[str, StateType]:
        model = self.coordinator.data
        if model is None:
            return {}
        return {
            "presets": [
                {"ams_id": a, "tray_id": t, "preset_id": pid, "preset_name": name}
                for a, t, pid, name in model.slot_presets
            ]
        }


class HmsSensor(TheGroveEntity, SensorEntity):
    """Per-printer HMS fault count. STATE = number of ACTIVE codes EXCLUDING the
    persistent phantom (so it reads 0 during normal printing, not 1 — R20). The
    codes themselves (short code, severity, module) are attributes."""

    _attr_icon = "mdi:alert-circle-outline"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: PrinterCoordinator) -> None:
        super().__init__(coordinator, key="hms", platform_domain=SENSOR)
        self._attr_name = "HMS errors"

    @property
    def native_value(self) -> StateType:
        model = self.coordinator.data
        if model is None:
            return None
        return len(real_hms(model))

    @property
    def extra_state_attributes(self) -> dict[str, StateType]:
        model = self.coordinator.data
        if model is None:
            return {}
        codes = [
            {"code": short, "severity": e.severity, "module": e.module}
            for short, e in real_hms(model)
        ]
        return {
            "codes": codes,  # non-phantom active faults (short code MMMM_EEEE)
            "suppressed": len(model.hms_errors) - len(codes),  # phantom/benign
        }


class VpPendingFilesSensor(TheGroveVpEntity, SensorEntity):
    """A Virtual Printer's queue depth — `status.pending_files`, the count of jobs
    waiting (e.g. a job held because auto_dispatch is off / manual release).
    `mode` and `target_printer` ride along as attributes for context."""

    _attr_name = "Pending files"
    _attr_icon = "mdi:file-clock"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        coordinator: VpCoordinator,
        vp_id: int,
        *,
        target_serial: str | None = None,
    ) -> None:
        super().__init__(
            coordinator, vp_id=vp_id, key="pending_files", platform_domain=SENSOR,
            target_serial=target_serial,
        )

    @property
    def native_value(self) -> StateType:
        vp = self.vp
        return None if vp is None else vp.pending_files

    @property
    def extra_state_attributes(self) -> dict[str, StateType]:
        vp = self.vp
        if vp is None:
            return {}
        return {
            "mode": vp.mode,
            "auto_dispatch": vp.auto_dispatch,
            "target_printer_id": vp.target_printer_id,
        }
