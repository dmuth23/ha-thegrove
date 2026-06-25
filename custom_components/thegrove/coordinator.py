"""Per-printer push coordinator.

One DataUpdateCoordinator per printer, run in PURE PUSH mode
(`update_interval=None`): WS frames call `handle_ws_frame`, the 30 s REST
backstop calls `apply_rest_status`. Both paths sticky-merge the REST-only
fields (current_archive_id + derived weight/start-time) so they never blank
between polls.

The coordinator does NOT own a poll timer — the backstop runs on the hub's own
`async_track_time_interval` task (HA's `async_set_updated_data` resets the
coordinator's native interval, which a ~1.5 s WS frame would perpetually
starve).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .bambuddy.rest_client import BambuddyRestClient
from .brain.mapper import map_printer_status, merge_rest_only
from .brain.model import PrinterModel
from .brain.vp import VirtualPrinterModel, map_virtual_printers

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PrinterIdentity:
    """Stable identity for one printer (from GET /printers/)."""

    printer_id: int  # Bambuddy DB id — WS-demux routing key
    serial: str  # the truly-stable key
    model: str
    name: str


class PrinterCoordinator(DataUpdateCoordinator[PrinterModel]):
    """Push-fed coordinator holding the latest PrinterModel for one printer."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        identity: PrinterIdentity,
        *,
        ws_is_connected: Callable[[], bool],
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"thegrove {identity.serial}",
            update_interval=None,  # pure push
        )
        self.identity = identity
        self._ws_is_connected = ws_is_connected
        # REST-only sticky state — only the REST poll mutates it. These keys are
        # in /status but NOT the WS frame (verified 2026-06-24), so without the
        # sticky merge they blank/flap on every ~1.5 s WS push.
        self._archive_id: int | None = None
        self._weight: float | None = None
        self._start: datetime | None = None
        self._firmware: str | None = None
        self._nozzle_type: str | None = None
        self._nozzle_diameter: str | None = None
        self._store_to_sdcard: bool | None = None
        self._print_options: tuple[tuple[str, bool | str], ...] = ()
        self._printable_objects: tuple[tuple[int, str], ...] = ()
        self._slot_presets: tuple[tuple[int, int, str, str], ...] = ()

    @property
    def feed_alive(self) -> bool:
        """True while the shared WS socket is connected.

        Availability tracks the CONNECTION, not frame arrival: Bambuddy pushes
        sparsely at idle (snapshot-on-connect, then on-change), so a frame-age
        check would wrongly mark an idle printer unavailable. A dead socket is
        caught by the aiohttp heartbeat -> reconnect, which flips this False.
        (Per-printer drop while the socket stays up is signalled by the frame's
        own `connected` field -> a P2 binary_sensor.)
        """
        return self._ws_is_connected()

    @callback
    def handle_ws_frame(self, data: dict) -> None:
        """Map one WS frame and push it, carrying the held REST-only fields."""
        model = map_printer_status(
            data,
            printer_id=self.identity.printer_id,
            serial=self.identity.serial,
            model=self.identity.model,
            name=self.identity.name,
        )
        self.async_set_updated_data(self._with_sticky(model))

    @callback
    def apply_rest_status(
        self,
        status: dict,
        archive_id: int | None,
        weight: float | None,
        start: datetime | None,
        *,
        printable_objects: tuple[tuple[int, str], ...] = (),
        slot_presets: tuple[tuple[int, int, str, str], ...] = (),
    ) -> None:
        """Apply a REST /status snapshot: update sticky fields AND push.

        /status is a superset of the WS data, so it doubles as a WS-miss
        backstop. The sticky fields are mutated ONLY here.
        """
        self._archive_id = archive_id
        self._weight = weight
        self._start = start
        self._printable_objects = printable_objects
        self._slot_presets = slot_presets
        model = map_printer_status(
            status,
            printer_id=self.identity.printer_id,
            serial=self.identity.serial,
            model=self.identity.model,
            name=self.identity.name,
        )
        # Capture the REST-only body fields from the freshly-mapped /status model
        # so WS frames (which lack them) inherit the last-known value.
        self._firmware = model.firmware_version
        self._nozzle_type = model.nozzle_type
        self._nozzle_diameter = model.nozzle_diameter
        self._store_to_sdcard = model.store_to_sdcard
        self._print_options = model.print_options
        self.async_set_updated_data(self._with_sticky(model))

    def _with_sticky(self, model: PrinterModel) -> PrinterModel:
        return merge_rest_only(
            model,
            current_archive_id=self._archive_id,
            print_weight_grams=self._weight,
            print_start_time=self._start,
            firmware_version=self._firmware,
            nozzle_type=self._nozzle_type,
            nozzle_diameter=self._nozzle_diameter,
            store_to_sdcard=self._store_to_sdcard,
            print_options=self._print_options,
            printable_objects=self._printable_objects,
            slot_presets=self._slot_presets,
        )


class VpCoordinator(DataUpdateCoordinator[dict[int, VirtualPrinterModel]]):
    """Standard POLLING coordinator for the Virtual-Printer surface.

    VPs are REST-only — there is no `virtual_printer_status` WS frame — so this
    is HA's native polling coordinator (interval = the REST backstop), NOT the
    pure-push PrinterCoordinator. One coordinator holds ALL VPs as
    `{vp_id: VirtualPrinterModel}` (one `GET /virtual-printers` returns the lot),
    keeping them out of the hub's `coordinators: dict[int, …]` where VP ids would
    collide with real-printer ids.

    `async_request_refresh()` (called right after a PUT write) is the post-write
    ~1s confirm — there's no WS frame to clear an entity's optimistic value.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        rest: BambuddyRestClient,
        *,
        update_interval_seconds: int,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name="thegrove virtual-printers",
            update_interval=timedelta(seconds=update_interval_seconds),
        )
        self._rest = rest

    async def _async_update_data(self) -> dict[int, VirtualPrinterModel]:
        try:
            payload = await self._rest.list_virtual_printers()
        except Exception as err:  # noqa: BLE001 -> UpdateFailed (entities unavailable)
            raise UpdateFailed(f"virtual-printers poll failed: {err}") from err
        return map_virtual_printers(payload)
