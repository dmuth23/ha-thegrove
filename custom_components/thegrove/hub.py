"""The hub — one Bambuddy connection fanning out to N printers.

Owns the single WS client, the per-printer coordinators, the SEPARATE 30 s REST
backstop timer, and the watchdog. Built for clean teardown: every task/timer is
tracked and cancelled in `async_shutdown` so an options-change reload leaves no
orphaned socket or reconnect loop.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import datetime, timedelta, timezone

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval

from .bambuddy.rest_client import BambuddyRestClient
from .bambuddy.ws_client import BambuddyWSClient
from .brain.archive_match import pick_active_archive
from .const import (
    CONF_API_TOKEN,
    CONF_BACKSTOP_INTERVAL,
    CONF_HOST,
    CONF_WATCHDOG_TIMEOUT,
    DEFAULT_BACKSTOP_INTERVAL,
    DEFAULT_WATCHDOG_TIMEOUT,
)
from .coordinator import PrinterCoordinator, PrinterIdentity, VpCoordinator

_LOGGER = logging.getLogger(__name__)

SIGNAL_NEW_PRINTER = "thegrove_new_printer_{}"


def _identity_from_printer(p: dict) -> PrinterIdentity:
    return PrinterIdentity(
        printer_id=int(p["id"]),
        serial=str(p.get("serial_number") or f"unknown-{p['id']}"),
        model=str(p.get("model") or "printer"),
        name=str(p.get("name") or f"Printer {p['id']}"),
    )


def _identity_from_frame(printer_id: int, data: dict) -> PrinterIdentity:
    """Fallback identity when /printers/ doesn't list a printer_id (e.g. a
    lifecycle test feeding a synthetic frame). The WS data carries name+model
    but not the serial, so synthesize a stable-enough serial from the id."""
    return PrinterIdentity(
        printer_id=printer_id,
        serial=f"unknown-{printer_id}",
        model=str(data.get("model") or "printer"),
        name=str(data.get("name") or f"Printer {printer_id}"),
    )


def parse_bb_utc(value: str | None) -> datetime | None:
    """Parse a Bambuddy timestamp as UTC.

    LANDMINE (P0): the /archives endpoint returns timestamps NAIVE (no Z), but
    they ARE UTC. Attach UTC tzinfo so HA renders correct local time and the
    delayed-print math isn't off by the offset.
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _parse_slot_presets(data: dict) -> tuple[tuple[int, int, str, str], ...]:
    """{key: {ams_id, tray_id, preset_id, preset_name}} -> (ams,tray,id,name) tuples."""
    out: list[tuple[int, int, str, str]] = []
    for v in (data or {}).values():
        if isinstance(v, dict):
            out.append((
                int(v.get("ams_id") or 0),
                int(v.get("tray_id") or 0),
                str(v.get("preset_id") or ""),
                str(v.get("preset_name") or ""),
            ))
    return tuple(sorted(out))


def _parse_print_objects(data: dict) -> tuple[tuple[int, str], ...]:
    """{objects: [{id, name, ...}]} -> (id, name) tuples."""
    out: list[tuple[int, str]] = []
    for o in (data or {}).get("objects") or []:
        if isinstance(o, dict) and o.get("id") is not None:
            out.append((int(o["id"]), str(o.get("name") or f"Object {o['id']}")))
    return tuple(out)


class TheGroveHub:
    """Holds the WS client, coordinators, and lifecycle for one config entry."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        host = entry.data[CONF_HOST]
        token = entry.data.get(CONF_API_TOKEN)
        session = async_get_clientsession(hass)
        self.rest = BambuddyRestClient(session, host, api_key=token or None)
        self.ws = BambuddyWSClient(
            session,
            host,
            on_frame=self._on_ws_frame,
            token_provider=self.rest.get_ws_token if token else None,
        )
        self.coordinators: dict[int, PrinterCoordinator] = {}
        self._backstop_interval = entry.options.get(
            CONF_BACKSTOP_INTERVAL, DEFAULT_BACKSTOP_INTERVAL
        )
        self._watchdog_timeout = entry.options.get(
            CONF_WATCHDOG_TIMEOUT, DEFAULT_WATCHDOG_TIMEOUT
        )
        # Virtual Printers — a SEPARATE polling coordinator (REST-only, no WS),
        # kept out of `coordinators` so VP ids can't collide with printer ids.
        self.vp_coordinator = VpCoordinator(
            hass, entry, self.rest, update_interval_seconds=self._backstop_interval
        )
        self._ws_task: asyncio.Task | None = None
        self._unsubs: list = []

    def printer_serial(self, printer_id: int | None) -> str | None:
        """The serial of a real printer by its BB id, for VP `via_device` links.
        None when the id is absent or that printer isn't loaded (graceful)."""
        if printer_id is None:
            return None
        coord = self.coordinators.get(printer_id)
        return coord.identity.serial if coord else None

    # ---- setup ----------------------------------------------------------
    async def async_setup(self) -> None:
        """Enumerate printers, prime via REST, then start WS + timers."""
        printers = await self.rest.list_printers()  # raises -> ConfigEntryNotReady
        for p in printers:
            self._ensure_coordinator(_identity_from_printer(p))

        # Startup prime: one REST /status per printer populates sticky + a first
        # model immediately (the first WS frame arrives ~1.5 s later).
        for printer_id in list(self.coordinators):
            await self._poll_one(printer_id)

        # Prime the VP surface (best-effort — VP failure must NOT block the
        # integration). Platforms read `vp_coordinator.data` to build VP entities;
        # a VP that appears later needs a reload to surface (like a new AMS).
        await self.vp_coordinator.async_refresh()

        # The live WS feed, as an HA-tracked background task (auto-cancelled on
        # unload; we also cancel+await explicitly in async_shutdown).
        self._ws_task = self.entry.async_create_background_task(
            self.hass, self.ws.run(), name="thegrove-ws"
        )

        # SEPARATE 30 s REST backstop (NOT the coordinator's native interval).
        self._unsubs.append(
            async_track_time_interval(
                self.hass,
                self._async_backstop_tick,
                timedelta(seconds=self._backstop_interval),
            )
        )
        # Watchdog: re-evaluate availability so a dead WS feed flips entities
        # `unavailable` even though no frame arrives to trigger a write.
        self._unsubs.append(
            async_track_time_interval(
                self.hass,
                self._watchdog_tick,
                timedelta(seconds=max(2, self._watchdog_timeout // 2)),
            )
        )

    def _ensure_coordinator(self, identity: PrinterIdentity) -> PrinterCoordinator:
        coord = self.coordinators.get(identity.printer_id)
        if coord is not None:
            return coord
        coord = PrinterCoordinator(
            self.hass,
            self.entry,
            identity,
            # One shared socket -> hub-wide connection state. Per-printer drop
            # (socket up, one printer gone) is a P2 refinement via the frame's
            # own `connected` field.
            ws_is_connected=lambda: self.ws.is_connected,
        )
        self.coordinators[identity.printer_id] = coord
        return coord

    # ---- WS frame routing ----------------------------------------------
    @callback
    def _on_ws_frame(self, printer_id: int, data: dict) -> None:
        coord = self.coordinators.get(printer_id)
        if coord is None:
            # First-ever printer_id -> dynamic add (no reload).
            self.hass.async_create_task(self._async_dynamic_add(printer_id, data))
            return
        coord.handle_ws_frame(data)

    async def _async_dynamic_add(self, printer_id: int, data: dict) -> None:
        """A printer simply appears: create its coordinator + device, then tell
        the platforms to add its entities — no reload."""
        identity = await self._resolve_identity(printer_id, data)
        coord = self._ensure_coordinator(identity)
        coord.handle_ws_frame(data)  # seed it immediately
        _LOGGER.info("thegrove discovered printer %s (%s)", printer_id, identity.serial)
        async_dispatcher_send(
            self.hass, SIGNAL_NEW_PRINTER.format(self.entry.entry_id), printer_id
        )

    async def _resolve_identity(self, printer_id: int, data: dict) -> PrinterIdentity:
        try:
            for p in await self.rest.list_printers():
                if int(p["id"]) == printer_id:
                    return _identity_from_printer(p)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("identity lookup failed for %s: %s", printer_id, err)
        return _identity_from_frame(printer_id, data)

    # ---- REST backstop --------------------------------------------------
    async def _async_backstop_tick(self, _now) -> None:
        for printer_id in list(self.coordinators):
            await self._poll_one(printer_id)

    async def _poll_one(self, printer_id: int) -> None:
        try:
            status = await self.rest.get_status(printer_id)
        except Exception as err:  # noqa: BLE001 - backstop is best-effort
            _LOGGER.debug("backstop /status failed for %s: %s", printer_id, err)
            return
        archive_id = status.get("current_archive_id")
        weight = start = None
        if archive_id:
            # Clean path: BB-dispatched print, the subtask_id link is present.
            try:
                arc = await self.rest.get_archive(archive_id)
                weight = arc.get("filament_used_grams")
                start = parse_bb_utc(arc.get("started_at"))
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("archive %s fetch failed: %s", archive_id, err)
        elif status.get("state") in ("RUNNING", "PAUSE"):
            # Printer-initiated print: no subtask_id, so BB never links it. Recover
            # the archive from the list (newest 'printing' row, name-checked) so
            # start-time + weight still populate. Gated on an active print to dodge
            # the orphan-row landmine (P0-FINDINGS §5 / brain.archive_match).
            try:
                rows = await self.rest.list_archives(printer_id)
                arc = pick_active_archive(rows, subtask_name=status.get("subtask_name"))
                if arc:
                    archive_id = arc.get("id")
                    weight = arc.get("filament_used_grams")
                    start = parse_bb_utc(arc.get("started_at"))
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("archive match failed for %s: %s", printer_id, err)
        # Saved slot presets (cheap DB read; always) + printable objects (only
        # meaningful during a print). Both best-effort: a failure leaves them empty.
        presets: tuple[tuple[int, int, str, str], ...] = ()
        try:
            presets = _parse_slot_presets(await self.rest.list_slot_presets(printer_id))
        except Exception as err:  # noqa: BLE001 - best-effort
            _LOGGER.debug("slot-presets fetch failed for %s: %s", printer_id, err)
        objects: tuple[tuple[int, str], ...] = ()
        if status.get("state") in ("RUNNING", "PAUSE"):
            try:
                objects = _parse_print_objects(await self.rest.get_print_objects(printer_id))
            except Exception as err:  # noqa: BLE001 - best-effort
                _LOGGER.debug("print-objects fetch failed for %s: %s", printer_id, err)
        coord = self.coordinators.get(printer_id)
        if coord is not None:
            coord.apply_rest_status(
                status, archive_id, weight, start,
                printable_objects=objects, slot_presets=presets,
            )

    # ---- watchdog -------------------------------------------------------
    @callback
    def _watchdog_tick(self, _now) -> None:
        for coord in self.coordinators.values():
            coord.async_update_listeners()

    # ---- teardown -------------------------------------------------------
    async def async_shutdown(self) -> None:
        """Cancel every timer + the WS task and close the single socket."""
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        await self.ws.stop()
        if self._ws_task is not None:
            self._ws_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._ws_task
            self._ws_task = None
