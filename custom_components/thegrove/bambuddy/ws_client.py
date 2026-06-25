"""Bambuddy WebSocket live-feed — ONE socket, demuxed by printer_id.

Bambuddy broadcasts every printer's frame as
`{"type":"printer_status","printer_id":N,"data":{...}}` (~1.5 s cadence). This
client holds one connection, filters to `printer_status`, stamps a per-printer
last-frame timestamp (for the watchdog), and hands `data` to the registered
callback routed by `printer_id`.

Designed for clean cancellation: `run()` is meant to be launched as an
HA-tracked background task; cancelling it (or calling `stop()`) tears the
socket down without leaking a reconnect loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable

from aiohttp import ClientSession, WSMsgType

_LOGGER = logging.getLogger(__name__)

FrameCallback = Callable[[int, dict], None]
TokenProvider = Callable[[], Awaitable[str | None]]

_MAX_BACKOFF = 30  # seconds
_WS_PATH = "/api/v1/ws"
_HEARTBEAT = 25  # aiohttp ping interval — detects a dead socket


def ws_url(base_url: str, token: str | None = None) -> str:
    """Build the ws(s):// URL. WS handshakes can't carry custom headers, so an
    auth token must ride as a query param (validated before accept())."""
    base = base_url.rstrip("/")
    if base.startswith("https://"):
        url = "wss://" + base[len("https://") :]
    elif base.startswith("http://"):
        url = "ws://" + base[len("http://") :]
    else:
        url = base
    url += _WS_PATH
    if token:
        url += f"?token={token}"
    return url


def _redact(url: str) -> str:
    return url.split("token=")[0] + "token=<redacted>" if "token=" in url else url


class BambuddyWSClient:
    """One persistent WS client multiplexing all printers."""

    def __init__(
        self,
        session: ClientSession,
        base_url: str,
        *,
        on_frame: FrameCallback,
        token_provider: TokenProvider | None = None,
        logger: logging.Logger = _LOGGER,
    ) -> None:
        self._session = session
        self._base = base_url
        self._on_frame = on_frame
        self._token_provider = token_provider
        self._log = logger
        self._stop = False
        self._ws = None
        self._last_frame: dict[int, float] = {}

    @property
    def is_connected(self) -> bool:
        """True while the single shared socket is open.

        Availability keys on THIS, not frame arrival: Bambuddy pushes frames
        ~1.5 s during a print but only a snapshot-on-connect (then on-change)
        when the printer is IDLE — so a frame-age check would falsely mark an
        idle printer unavailable. The aiohttp heartbeat (25 s ping) detects a
        genuinely dead socket and ends the read loop -> reconnect.
        """
        return self._ws is not None and not self._ws.closed

    @property
    def last_frame(self) -> dict[int, float]:
        """Per-printer monotonic timestamp of the last received frame."""
        return dict(self._last_frame)

    def seconds_since_frame(
        self, printer_id: int, *, now: float | None = None
    ) -> float | None:
        """Seconds since this printer's last frame, or None if never seen.

        Pure given `now` — the coordinator's watchdog calls this to decide
        staleness without this client owning a timer.
        """
        ts = self._last_frame.get(printer_id)
        if ts is None:
            return None
        return (time.monotonic() if now is None else now) - ts

    async def run(self) -> None:
        """Connect / read / reconnect forever until stop() or cancellation."""
        backoff = 1
        while not self._stop:
            try:
                token = (
                    await self._token_provider() if self._token_provider else None
                )
                url = ws_url(self._base, token)
                self._log.debug("thegrove WS connecting: %s", _redact(url))
                async with self._session.ws_connect(
                    url, heartbeat=_HEARTBEAT
                ) as ws:
                    self._ws = ws
                    backoff = 1
                    self._log.info("thegrove WS connected")
                    async for msg in ws:
                        if msg.type == WSMsgType.TEXT:
                            self._handle(msg.data)
                        elif msg.type in (
                            WSMsgType.CLOSED,
                            WSMsgType.CLOSING,
                            WSMsgType.ERROR,
                        ):
                            break
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 - keep the loop alive
                self._log.warning(
                    "thegrove WS error (%s); reconnecting in %ss", err, backoff
                )
            finally:
                self._ws = None
            if self._stop:
                break
            await asyncio.sleep(backoff)  # CancelledError propagates cleanly
            backoff = min(backoff * 2, _MAX_BACKOFF)
        self._log.debug("thegrove WS loop exited")

    def _handle(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except (ValueError, TypeError):
            return
        if msg.get("type") != "printer_status":
            return
        pid = msg.get("printer_id")
        if pid is None:
            return
        self._last_frame[pid] = time.monotonic()
        try:
            self._on_frame(pid, msg.get("data") or {})
        except Exception:  # noqa: BLE001 - one bad frame must not kill the socket
            self._log.exception("thegrove on_frame raised for printer %s", pid)

    async def stop(self) -> None:
        """Request shutdown and close the socket. Idempotent."""
        self._stop = True
        ws = self._ws
        if ws is not None and not ws.closed:
            await ws.close()
