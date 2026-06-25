"""Button platform for thegrove — fire-and-forget printer commands (P3).

The kept printer controls that have no state of their own: pause / resume / stop,
clear-plate, hms-clear, refresh-status, home-axes. Each is a thin press over a
rest_client wrapper, routed through the shared command base so any failure
becomes a clean HomeAssistantError toast (never a half-write).

Gating policy: buttons stay pressable and rely on Bambuddy to fail-loud (e.g.
"resume" when not paused returns a 4xx -> toast). We do NOT pre-gate on printer
state — simpler, and it surfaces BB's own reason rather than guessing it.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import TheGroveConfigEntry
from .bambuddy.rest_client import BambuddyRestClient
from .brain.derive import global_tray_id
from .coordinator import PrinterCoordinator
from .entity import TheGroveAmsCommandEntity, TheGroveCommandEntity, ams_side
from .hub import SIGNAL_NEW_PRINTER

BUTTON = "button"


@dataclass(frozen=True, kw_only=True)
class PrinterButtonDescription(ButtonEntityDescription):
    """A printer-body command button. press_fn issues one rest_client call;
    action is the human verb used in an error toast."""

    press_fn: Callable[[BambuddyRestClient, int], Awaitable]
    action: str


PRINTER_BUTTONS: tuple[PrinterButtonDescription, ...] = (
    PrinterButtonDescription(
        key="pause",
        name="Pause",
        icon="mdi:pause",
        press_fn=lambda rest, pid: rest.pause_print(pid),
        action="Pause print",
    ),
    PrinterButtonDescription(
        key="resume",
        name="Resume",
        icon="mdi:play",
        press_fn=lambda rest, pid: rest.resume_print(pid),
        action="Resume print",
    ),
    PrinterButtonDescription(
        key="stop",
        name="Stop",
        icon="mdi:stop",
        press_fn=lambda rest, pid: rest.stop_print(pid),
        action="Stop print",
    ),
    PrinterButtonDescription(
        key="clear_plate",
        name="Clear plate",
        icon="mdi:tray-remove",
        press_fn=lambda rest, pid: rest.clear_plate(pid),
        action="Clear plate",
    ),
    PrinterButtonDescription(
        key="clear_hms",
        name="Clear errors",
        icon="mdi:alert-circle-check",
        entity_category=EntityCategory.DIAGNOSTIC,
        press_fn=lambda rest, pid: rest.clear_hms(pid),
        action="Clear HMS errors",
    ),
    PrinterButtonDescription(
        key="refresh",
        name="Refresh status",
        icon="mdi:refresh",
        entity_category=EntityCategory.DIAGNOSTIC,
        press_fn=lambda rest, pid: rest.refresh_status(pid),
        action="Refresh status",
    ),
    PrinterButtonDescription(
        key="home",
        name="Home axes",
        icon="mdi:home-import-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
        press_fn=lambda rest, pid: rest.home_axes(pid),
        action="Home axes",
    ),
    PrinterButtonDescription(
        key="ams_unload",
        name="Unload filament",
        icon="mdi:tray-arrow-down",
        # No slot argument (unloads whatever is at the nozzle) -> printer-level.
        press_fn=lambda rest, pid: rest.ams_unload(pid),
        action="Unload filament",
    ),
)


@dataclass(frozen=True, kw_only=True)
class AmsTrayButtonDescription(ButtonEntityDescription):
    """A per-tray AMS command button (on the AMS child device). press_fn takes
    (rest, printer_id, ams_id, tray_id) — tray_id is the per-AMS 0-based slot."""

    press_fn: Callable[[BambuddyRestClient, int, int, int], Awaitable]
    action: str


# Per-tray AMS buttons — diagnostic/config category (tucked under each AMS device,
# hidden by default), so the per-slot granularity doesn't clutter the dashboard.
AMS_TRAY_BUTTONS: tuple[AmsTrayButtonDescription, ...] = (
    AmsTrayButtonDescription(
        key="load",
        name="Load",
        icon="mdi:tray-arrow-up",
        entity_category=EntityCategory.CONFIG,
        # ams_load takes the GLOBAL tray id = ams_id*4 + slot — computed via the
        # tested `global_tray_id` (inverse of the active-tray decode) so the
        # overloaded encoding never reaches the user.
        press_fn=lambda rest, pid, ams, tray: rest.ams_load(pid, global_tray_id(ams, tray)),
        action="Load filament",
    ),
    AmsTrayButtonDescription(
        key="refresh",
        name="Refresh RFID",
        icon="mdi:nfc-search-variant",
        entity_category=EntityCategory.DIAGNOSTIC,
        press_fn=lambda rest, pid, ams, tray: rest.ams_slot_refresh(pid, ams, tray),
        action="Refresh slot RFID",
    ),
    AmsTrayButtonDescription(
        key="reset",
        name="Reset slot",
        icon="mdi:tray-remove",
        entity_category=EntityCategory.CONFIG,
        press_fn=lambda rest, pid, ams, tray: rest.ams_tray_reset(pid, ams, tray),
        action="Reset slot",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TheGroveConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """One set of command buttons per printer, with dynamic-add."""
    hub = entry.runtime_data

    @callback
    def _entities_for(coord: PrinterCoordinator) -> list[ButtonEntity]:
        ents: list[ButtonEntity] = [
            PrinterButton(coord, hub.rest, desc) for desc in PRINTER_BUTTONS
        ]
        model = coord.data
        if model is not None:
            for ams in model.ams:
                for tray in ams.trays:
                    ents += [
                        AmsTrayButton(coord, hub.rest, ams.id, tray.id, desc)
                        for desc in AMS_TRAY_BUTTONS
                    ]
        return ents

    initial: list[ButtonEntity] = []
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


class PrinterButton(TheGroveCommandEntity, ButtonEntity):
    """A fire-and-forget printer command driven by a PrinterButtonDescription."""

    entity_description: PrinterButtonDescription

    def __init__(
        self,
        coordinator: PrinterCoordinator,
        rest: BambuddyRestClient,
        description: PrinterButtonDescription,
    ) -> None:
        super().__init__(coordinator, rest, key=description.key, platform_domain=BUTTON)
        self.entity_description = description
        self._attr_name = description.name

    async def async_press(self) -> None:
        await self._run(
            self.entity_description.press_fn(self._rest, self._printer_id),
            action=self.entity_description.action,
        )


class AmsTrayButton(TheGroveAmsCommandEntity, ButtonEntity):
    """A per-tray AMS command button on the AMS child device (load/refresh/reset)."""

    entity_description: AmsTrayButtonDescription

    def __init__(
        self,
        coordinator: PrinterCoordinator,
        rest: BambuddyRestClient,
        ams_id: int,
        tray_id: int,
        description: AmsTrayButtonDescription,
    ) -> None:
        human = tray_id + 1  # 1-based for humans, matching the tray sensors
        key = f"ams_{ams_side(ams_id)}_tray_{human}_{description.key}"
        super().__init__(
            coordinator, rest, ams_id=ams_id, key=key, platform_domain=BUTTON
        )
        self._tray_id = tray_id
        self.entity_description = description
        self._attr_name = f"Tray {human} {description.name}"

    async def async_press(self) -> None:
        await self._run(
            self.entity_description.press_fn(
                self._rest, self._printer_id, self._ams_id, self._tray_id
            ),
            action=self.entity_description.action,
        )
