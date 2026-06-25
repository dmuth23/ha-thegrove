"""Light platform for thegrove — the chamber light (P3 first write slice).

The chamber light is the safest, most reversible control to prove the whole
write loop on: its on/off state is ALREADY in the read frame
(`model.chamber_light`), so the entity reads real state AND writes it — a closed
loop in one entity. Modelled as a `light` (ColorMode.ONOFF), matching
ha-bambulab, so the P4/P5 parallel-run can compare like-for-like.

Post-write behaviour on a PURE-PUSH coordinator: the write returns before the
next WS frame (~1.5 s), so we set an OPTIMISTIC value for instant UI feedback
and drop it only once a frame CONFIRMS it (`_handle_coordinator_update`). The
confirm-based clear matters because the hub's watchdog calls
`async_update_listeners()` every ~2 s carrying NO new data — a clear-on-any-tick
would wipe optimism back to the stale value ~2 s after a toggle (a visible
flicker). Holding optimism until the data agrees rides through dataless ticks
and clears seamlessly on the real on-change frame. A failed write raises
HomeAssistantError (via the command base) and never flips the optimistic state.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.light import ColorMode, LightEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import TheGroveConfigEntry
from .coordinator import PrinterCoordinator
from .entity import TheGroveCommandEntity
from .hub import SIGNAL_NEW_PRINTER

LIGHT = "light"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TheGroveConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """One chamber light per printer, with dynamic-add for printers that appear."""
    hub = entry.runtime_data

    @callback
    def _entities_for(coord: PrinterCoordinator) -> list[LightEntity]:
        return [PrinterChamberLight(coord, hub.rest)]

    initial: list[LightEntity] = []
    for coord in hub.coordinators.values():
        initial += _entities_for(coord)
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


class PrinterChamberLight(TheGroveCommandEntity, LightEntity):
    """The printer's chamber light — read `chamber_light`, write via REST."""

    _attr_name = "Chamber light"
    _attr_color_mode = ColorMode.ONOFF
    _attr_supported_color_modes = {ColorMode.ONOFF}

    def __init__(self, coordinator: PrinterCoordinator, rest) -> None:
        super().__init__(coordinator, rest, key="chamber_light", platform_domain=LIGHT)

    def _confirming_value(self) -> bool | None:
        model = self.coordinator.data
        return None if model is None else model.chamber_light

    @property
    def is_on(self) -> bool | None:
        if self._optimistic is not None:
            return self._optimistic
        return self._confirming_value()

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._run(
            self._rest.set_chamber_light(self._printer_id, True),
            action="Turn on chamber light",
        )
        self._apply_optimistic(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._run(
            self._rest.set_chamber_light(self._printer_id, False),
            action="Turn off chamber light",
        )
        self._apply_optimistic(False)
