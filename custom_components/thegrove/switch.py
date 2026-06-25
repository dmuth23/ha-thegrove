"""Switch platform for thegrove — the AMS dryer (P3-4b).

First user of TheGroveAmsCommandEntity (a write entity on the AMS CHILD device).
The dryer is read+write: `is_on` reflects the live `dry_status`, turning on/off
starts/stops a dry cycle. Drying is the "cheap/safe" AMS write (no filament
burned), so it's the natural first AMS-child control.

Named **Dryer** (the control) to stay distinct from the existing **Drying**
read-only binary_sensor (the status) on the same AMS device — they coexist: the
binary_sensor remains the canonical status read (P2), the switch adds control.

Dryer switch defaults: 45 °C / 8 h (Doug, 2026-06-25). Full temp/duration/filament
control is the `start_drying` SERVICE.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import TheGroveConfigEntry
from .bambuddy.rest_client import BambuddyRestClient
from .brain.model import AmsUnit
from .brain.vp import VirtualPrinterModel
from .coordinator import PrinterCoordinator, VpCoordinator
from .detectors import DETECTORS, Detector, detector_enabled, detector_sensitivity
from .entity import (
    TheGroveAmsCommandEntity,
    TheGroveCommandEntity,
    TheGroveVpCommandEntity,
    ams_side,
)
from .hub import SIGNAL_NEW_PRINTER

SWITCH = "switch"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TheGroveConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """One dryer switch per AMS unit, with dynamic-add for printers/AMS that appear."""
    hub = entry.runtime_data

    @callback
    def _entities_for(coord: PrinterCoordinator) -> list[SwitchEntity]:
        # AI-detector switches are printer-level (built always; they read
        # print_options, which the 30 s REST backstop fills in).
        ents: list[SwitchEntity] = [
            PrintOptionSwitch(coord, hub.rest, det) for det in DETECTORS
        ]
        model = coord.data
        if model is not None:
            ents += [AmsDryerSwitch(coord, hub.rest, ams.id) for ams in model.ams]
        return ents

    initial: list[SwitchEntity] = []
    for coord in hub.coordinators.values():
        initial += _entities_for(coord)
    # Virtual-Printer switches (separate coordinator). New VPs need a reload.
    for vp in (hub.vp_coordinator.data or {}).values():
        target_serial = hub.printer_serial(vp.target_printer_id)
        initial += [
            VpSwitch(hub.vp_coordinator, hub.rest, vp.vp_id, desc, target_serial=target_serial)
            for desc in VP_SWITCHES
        ]
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


#: Dryer switch start defaults (Doug 2026-06-25). The start_drying SERVICE
#: overrides these per-call.
DRYER_TEMP_C = 45
DRYER_HOURS = 8


class AmsDryerSwitch(TheGroveAmsCommandEntity, SwitchEntity):
    """The AMS dryer — reads `dry_status`, writes start/stop (defaults 45 °C/8 h)."""

    _attr_name = "Dryer"
    _attr_icon = "mdi:water-boiler"

    def __init__(
        self, coordinator: PrinterCoordinator, rest: BambuddyRestClient, ams_id: int
    ) -> None:
        key = f"ams_{ams_side(ams_id)}_dryer"
        super().__init__(
            coordinator, rest, ams_id=ams_id, key=key, platform_domain=SWITCH
        )

    def _ams(self) -> AmsUnit | None:
        model = self.coordinator.data
        if model is None:
            return None
        return next((a for a in model.ams if a.id == self._ams_id), None)

    def _confirming_value(self) -> bool | None:
        ams = self._ams()
        if ams is None or ams.dry_status is None:
            return None
        return ams.dry_status != 0

    @property
    def is_on(self) -> bool | None:
        if self._optimistic is not None:
            return self._optimistic
        return self._confirming_value()

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._run(
            self._rest.start_drying(
                self._printer_id, self._ams_id, temp=DRYER_TEMP_C, duration=DRYER_HOURS
            ),
            action="Start dryer",
        )
        self._apply_optimistic(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._run(
            self._rest.stop_drying(self._printer_id, self._ams_id),
            action="Stop dryer",
        )
        self._apply_optimistic(False)


class PrintOptionSwitch(TheGroveCommandEntity, SwitchEntity):
    """One AI-detection (xcam) module on/off — reads `print_options`, writes via
    set_print_option. Passes the module's CURRENT sensitivity through on a toggle
    so flipping on/off never resets its sensitivity. Config category (hidden)."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:cctv"

    def __init__(
        self, coordinator: PrinterCoordinator, rest: BambuddyRestClient, det: Detector
    ) -> None:
        super().__init__(
            coordinator, rest, key=f"detect_{det.key}", platform_domain=SWITCH
        )
        self._det = det
        self._attr_name = det.name

    def _confirming_value(self) -> bool | None:
        model = self.coordinator.data
        return None if model is None else detector_enabled(model, self._det)

    @property
    def is_on(self) -> bool | None:
        if self._optimistic is not None:
            return self._optimistic
        return self._confirming_value()

    async def _set(self, enabled: bool) -> None:
        model = self.coordinator.data
        # Pass the module's current sensitivity through (else the route default
        # "medium" would overwrite it). "medium" only when none is known.
        sens = (detector_sensitivity(model, self._det) if model else None) or "medium"
        await self._run(
            self._rest.set_print_option(
                self._printer_id, self._det.write_module, enabled, sensitivity=sens
            ),
            action=f"{'Enable' if enabled else 'Disable'} {self._det.name}",
        )
        self._apply_optimistic(enabled)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)


# --- Virtual-Printer switches (P2.5) ----------------------------------------


@dataclass(frozen=True, kw_only=True)
class VpSwitchDescription(SwitchEntityDescription):
    """A boolean VP routing field, written as a single-key PUT patch.
    `field` is BOTH the PUT body key and the VirtualPrinterModel attribute."""

    field: str
    value_fn: Callable[[VirtualPrinterModel], bool]


VP_SWITCHES: tuple[VpSwitchDescription, ...] = (
    VpSwitchDescription(
        key="enabled",
        name="Enabled",
        icon="mdi:power",
        field="enabled",
        value_fn=lambda vp: vp.enabled,
    ),
    VpSwitchDescription(
        key="auto_dispatch",
        name="Auto dispatch",
        icon="mdi:send-clock",
        field="auto_dispatch",
        value_fn=lambda vp: vp.auto_dispatch,
    ),
    VpSwitchDescription(
        key="queue_force_color_match",
        name="Force color match",
        icon="mdi:palette-swatch",
        field="queue_force_color_match",
        value_fn=lambda vp: vp.queue_force_color_match,
    ),
)


class VpSwitch(TheGroveVpCommandEntity, SwitchEntity):
    """A VP boolean control (enabled / auto_dispatch / queue_force_color_match).
    One-field PUT patch + optimistic show + post-write poll-to-confirm."""

    entity_description: VpSwitchDescription

    def __init__(
        self,
        coordinator: VpCoordinator,
        rest: BambuddyRestClient,
        vp_id: int,
        description: VpSwitchDescription,
        *,
        target_serial: str | None = None,
    ) -> None:
        super().__init__(
            coordinator,
            rest,
            vp_id=vp_id,
            key=description.key,
            platform_domain=SWITCH,
            target_serial=target_serial,
        )
        self.entity_description = description
        self._attr_name = description.name

    def _confirming_value(self) -> bool | None:
        vp = self.vp
        return None if vp is None else self.entity_description.value_fn(vp)

    @property
    def is_on(self) -> bool | None:
        if self._optimistic is not None:
            return self._optimistic
        return self._confirming_value()

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._write_vp(
            True,
            action=f"Enable {self.entity_description.name}",
            **{self.entity_description.field: True},
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._write_vp(
            False,
            action=f"Disable {self.entity_description.name}",
            **{self.entity_description.field: False},
        )
