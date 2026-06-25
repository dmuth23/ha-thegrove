"""Select platform for thegrove — print-speed mode (P3).

The printer's speed mode is both READ (`speed_level` 1-4) and WRITABLE, so it's
a select rather than a write-only control: `current_option` reflects the live
speed_level, selecting writes via `set_print_speed`. This is the first select
primitive — P2.5's Virtual-Printer mode select reuses this exact shape.
Optimistic + watchdog-safe confirm-clear are inherited from the command base.
"""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import TheGroveConfigEntry
from .bambuddy.rest_client import BambuddyRestClient
from .brain.vp import VP_MODES
from .coordinator import PrinterCoordinator, VpCoordinator
from .detectors import (
    DETECTORS,
    SENSITIVITY_OPTIONS,
    Detector,
    detector_enabled,
    detector_sensitivity,
)
from .entity import TheGroveCommandEntity, TheGroveVpCommandEntity
from .hub import SIGNAL_NEW_PRINTER

SELECT = "select"

# BB print-speed modes (printers.py set_print_speed): 1=silent … 4=ludicrous.
SPEED_OPTION_TO_MODE = {"Silent": 1, "Standard": 2, "Sport": 3, "Ludicrous": 4}
SPEED_MODE_TO_OPTION = {mode: name for name, mode in SPEED_OPTION_TO_MODE.items()}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TheGroveConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """One print-speed select per printer, with dynamic-add."""
    hub = entry.runtime_data

    @callback
    def _entities_for(coord: PrinterCoordinator) -> list[SelectEntity]:
        ents: list[SelectEntity] = [PrinterPrintSpeed(coord, hub.rest)]
        # A sensitivity select per detector that HAS a sensitivity dial.
        ents += [
            DetectorSensitivitySelect(coord, hub.rest, det)
            for det in DETECTORS
            if det.sensitivity_key is not None
        ]
        return ents

    initial: list[SelectEntity] = []
    for coord in hub.coordinators.values():
        initial += _entities_for(coord)
    # Virtual-Printer mode select (separate coordinator). New VPs need a reload.
    for vp in (hub.vp_coordinator.data or {}).values():
        target_serial = hub.printer_serial(vp.target_printer_id)
        initial.append(
            VpModeSelect(hub.vp_coordinator, hub.rest, vp.vp_id, target_serial=target_serial)
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


class PrinterPrintSpeed(TheGroveCommandEntity, SelectEntity):
    """The print-speed mode — reads `speed_level`, writes via set_print_speed."""

    _attr_name = "Print speed"
    _attr_icon = "mdi:speedometer"
    _attr_options = list(SPEED_OPTION_TO_MODE)

    def __init__(self, coordinator: PrinterCoordinator, rest: BambuddyRestClient) -> None:
        super().__init__(coordinator, rest, key="print_speed", platform_domain=SELECT)

    def _option_from_model(self) -> str | None:
        model = self.coordinator.data
        if model is None:
            return None
        return SPEED_MODE_TO_OPTION.get(model.speed_level)

    def _confirming_value(self) -> str | None:
        return self._option_from_model()

    @property
    def current_option(self) -> str | None:
        if self._optimistic is not None:
            return self._optimistic
        return self._option_from_model()

    async def async_select_option(self, option: str) -> None:
        # HA validates `option` against _attr_options before calling us.
        mode = SPEED_OPTION_TO_MODE[option]
        await self._run(
            self._rest.set_print_speed(self._printer_id, mode),
            action=f"Set print speed to {option}",
        )
        self._apply_optimistic(option)


class DetectorSensitivitySelect(TheGroveCommandEntity, SelectEntity):
    """An AI-detector's sensitivity (low/medium/high/never_halt). Reads
    `print_options`, writes via set_print_option — passing the detector's CURRENT
    enabled state through so changing sensitivity doesn't toggle the detector.
    Config category (hidden by default)."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:tune-variant"
    _attr_options = SENSITIVITY_OPTIONS

    def __init__(
        self, coordinator: PrinterCoordinator, rest: BambuddyRestClient, det: Detector
    ) -> None:
        super().__init__(
            coordinator, rest, key=f"detect_{det.key}_sensitivity", platform_domain=SELECT
        )
        self._det = det
        self._attr_name = f"{det.name} sensitivity"

    def _confirming_value(self) -> str | None:
        model = self.coordinator.data
        return None if model is None else detector_sensitivity(model, self._det)

    @property
    def current_option(self) -> str | None:
        if self._optimistic is not None:
            return self._optimistic
        return self._confirming_value()

    async def async_select_option(self, option: str) -> None:
        model = self.coordinator.data
        enabled = detector_enabled(model, self._det) if model else None
        if enabled is None:
            enabled = True  # keep it on when the current state isn't known yet
        await self._run(
            self._rest.set_print_option(
                self._printer_id, self._det.write_module, enabled, sensitivity=option
            ),
            action=f"Set {self._det.name} sensitivity",
        )
        self._apply_optimistic(option)


class VpModeSelect(TheGroveVpCommandEntity, SelectEntity):
    """A Virtual Printer's routing mode (archive/review/queue/proxy). Reads `mode`,
    writes a single-field PUT patch. Same optimistic + confirm shape as the
    print-speed select, but on the VP coordinator (poll-confirmed, no WS)."""

    _attr_name = "Mode"
    _attr_icon = "mdi:call-split"
    _attr_options = list(VP_MODES)

    def __init__(
        self,
        coordinator: VpCoordinator,
        rest: BambuddyRestClient,
        vp_id: int,
        *,
        target_serial: str | None = None,
    ) -> None:
        super().__init__(
            coordinator, rest, vp_id=vp_id, key="mode", platform_domain=SELECT,
            target_serial=target_serial,
        )

    def _confirming_value(self) -> str | None:
        vp = self.vp
        return None if vp is None else vp.mode

    @property
    def current_option(self) -> str | None:
        if self._optimistic is not None:
            return self._optimistic
        return self._confirming_value()

    async def async_select_option(self, option: str) -> None:
        # HA validates `option` against _attr_options (the canonical VP_MODES).
        await self._write_vp(option, action=f"Set VP mode to {option}", mode=option)
