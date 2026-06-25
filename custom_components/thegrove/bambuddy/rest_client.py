"""Bambuddy REST client — on-demand reads + printer commands (P3 write path).

Thin typed wrappers, all printer_id-scoped where relevant. Session injected, so
HA-free. Fail-loud: callers see exceptions (no silent half-success).

The write surface (P3) is the REUSABLE SUBSTRATE the rest of the integration
rides on: every command goes through one generic `_request(method, path, *,
params, json)`. Bambuddy's printer controls are all query-param POSTs
(`chamber-light?on=true`, `print-speed?mode=2`); the `json=` body path exists
for forward-compat (the P2.5 Virtual-Printer `PUT`s). On any non-2xx the client
raises `BambuddyApiError` carrying BB's own `detail` string, which the entity
layer turns into a user-facing `HomeAssistantError` toast.
"""

from __future__ import annotations

import logging
from typing import Any

from aiohttp import ClientSession, ClientTimeout

_LOGGER = logging.getLogger(__name__)
_TIMEOUT = ClientTimeout(total=10)


class BambuddyApiError(RuntimeError):
    """A Bambuddy REST call returned a non-2xx. Carries BB's `detail` message so
    the entity layer can surface a meaningful toast instead of a raw traceback."""

    def __init__(self, status: int, detail: str) -> None:
        self.status = status
        self.detail = detail
        super().__init__(f"Bambuddy {status}: {detail}")


def _norm_params(params: dict[str, Any]) -> dict[str, str]:
    """Render query values the way Bambuddy/FastAPI expect: bools as lowercase
    `true`/`false` (NOT Python's `True`/`False`), everything else `str()`-ed,
    `None` dropped. The chamber-light bool is the first thing this catches."""
    out: dict[str, str] = {}
    for key, value in params.items():
        if value is None:
            continue
        out[key] = ("true" if value else "false") if isinstance(value, bool) else str(value)
    return out


async def _read_detail(resp: Any) -> str:
    """Pull Bambuddy's error message out of a failed response. FastAPI puts it in
    `{"detail": ...}`; fall back to raw text, then the bare status reason."""
    try:
        body = await resp.json()
        if isinstance(body, dict) and "detail" in body:
            return str(body["detail"])
        return str(body)
    except Exception:  # noqa: BLE001 - error body may not be JSON
        try:
            text = (await resp.text()).strip()
            return text or f"HTTP {resp.status}"
        except Exception:  # noqa: BLE001
            return f"HTTP {resp.status}"


class BambuddyRestClient:
    """REST surface of Bambuddy, scoped to one base URL."""

    def __init__(
        self,
        session: ClientSession,
        base_url: str,
        *,
        api_key: str | None = None,
    ) -> None:
        self._session = session
        self._base = base_url.rstrip("/")
        self._api_key = api_key

    @property
    def base_url(self) -> str:
        return self._base

    def _headers(self) -> dict[str, str]:
        return {"X-API-Key": self._api_key} if self._api_key else {}

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any | None = None,
    ) -> Any:
        """The one read/write entry point. Fail-loud: a non-2xx raises
        BambuddyApiError(status, detail). Returns parsed JSON (or text fallback)."""
        async with self._session.request(
            method,
            f"{self._base}{path}",
            headers=self._headers(),
            params=_norm_params(params) if params else None,
            json=json,
            timeout=_TIMEOUT,
        ) as resp:
            if resp.status >= 400:
                detail = await _read_detail(resp)
                raise BambuddyApiError(resp.status, detail)
            try:
                return await resp.json()
            except Exception:  # noqa: BLE001 - some commands return empty/non-JSON
                return await resp.text()

    async def _get(self, path: str) -> Any:
        return await self._request("GET", path)

    async def list_printers(self) -> list[dict]:
        """GET /api/v1/printers/ — identity (id, serial_number, model, name)."""
        return await self._get("/api/v1/printers/")

    async def get_status(self, printer_id: int) -> dict:
        """GET /api/v1/printers/{id}/status — the REST SUPERSET of the WS data.

        Adds REST-only fields the WS feed omits, notably `current_archive_id`.
        """
        return await self._get(f"/api/v1/printers/{printer_id}/status")

    async def get_archive(self, archive_id: int) -> dict:
        """GET /api/v1/archives/{id} — print weight + started_at live here."""
        return await self._get(f"/api/v1/archives/{archive_id}")

    async def list_archives(self, printer_id: int, limit: int = 5) -> list[dict]:
        """GET /api/v1/archives/?printer_id=… — recent archives, newest first.

        Used to recover the active archive for printer-initiated prints, which
        carry no `current_archive_id` link (no `subtask_id`). Tolerant of the
        response being a bare list or wrapped in a container key.
        """
        data = await self._get(
            f"/api/v1/archives/?printer_id={printer_id}&limit={limit}"
        )
        if isinstance(data, list):
            return data
        for key in ("archives", "items", "results"):
            if isinstance(data.get(key), list):
                return data[key]
        return []

    async def list_slot_presets(self, printer_id: int) -> dict:
        """GET /printers/{id}/slot-presets — saved slot->preset mappings, keyed by
        slot index: {key: {ams_id, tray_id, preset_id, preset_name}}."""
        data = await self._get(f"/api/v1/printers/{printer_id}/slot-presets")
        return data if isinstance(data, dict) else {}

    async def get_print_objects(self, printer_id: int) -> dict:
        """GET /printers/{id}/print/objects — the active print's skippable objects:
        {objects: [{id, name, skipped, ...}], total, ...}."""
        data = await self._get(f"/api/v1/printers/{printer_id}/print/objects")
        return data if isinstance(data, dict) else {}

    # ---- commands (P3 write path) --------------------------------------
    # The kept printer-control surface (ARCH-PLAN §5 / printers.py routes).
    # All are query-param POSTs; each raises BambuddyApiError on a non-2xx.

    async def pause_print(self, printer_id: int) -> Any:
        """POST /printers/{id}/print/pause."""
        return await self._request("POST", f"/api/v1/printers/{printer_id}/print/pause")

    async def resume_print(self, printer_id: int) -> Any:
        """POST /printers/{id}/print/resume."""
        return await self._request("POST", f"/api/v1/printers/{printer_id}/print/resume")

    async def stop_print(self, printer_id: int) -> Any:
        """POST /printers/{id}/print/stop — cancel the running job."""
        return await self._request("POST", f"/api/v1/printers/{printer_id}/print/stop")

    async def clear_plate(self, printer_id: int) -> Any:
        """POST /printers/{id}/clear-plate — acknowledge the bed is cleared."""
        return await self._request("POST", f"/api/v1/printers/{printer_id}/clear-plate")

    async def clear_hms(self, printer_id: int) -> Any:
        """POST /printers/{id}/hms/clear — clear HMS/print errors."""
        return await self._request("POST", f"/api/v1/printers/{printer_id}/hms/clear")

    async def refresh_status(self, printer_id: int) -> Any:
        """POST /printers/{id}/refresh-status — ask BB to re-pull from the printer."""
        return await self._request("POST", f"/api/v1/printers/{printer_id}/refresh-status")

    async def home_axes(self, printer_id: int) -> Any:
        """POST /printers/{id}/home-axes — full auto-home (bare G28; `axes` ignored
        server-side, kept default)."""
        return await self._request("POST", f"/api/v1/printers/{printer_id}/home-axes")

    async def set_chamber_light(self, printer_id: int, on: bool) -> Any:
        """POST /printers/{id}/chamber-light?on=true|false."""
        return await self._request(
            "POST", f"/api/v1/printers/{printer_id}/chamber-light", params={"on": on}
        )

    async def set_print_speed(self, printer_id: int, mode: int) -> Any:
        """POST /printers/{id}/print-speed?mode=N (1=silent…4=ludicrous)."""
        return await self._request(
            "POST", f"/api/v1/printers/{printer_id}/print-speed", params={"mode": mode}
        )

    # ---- AMS / filament suite (the GAINED controls ha-bambulab lacks) ----
    # NOTE: `tray_id` is overloaded across these routes —
    #   ams_load:        GLOBAL id = ams_id*4 + slot_id  (+ 254/255 = external)
    #   configure/reset: PER-AMS 0-3 slot index, alongside a separate ams_id
    #   slot refresh:    the route names it `slot_id`
    # Wrappers name the arg to match the route to keep call sites honest.

    async def ams_load(self, printer_id: int, tray_id: int) -> Any:
        """POST /printers/{id}/ams/load?tray_id=N — load from a slot. `tray_id`
        is the GLOBAL encoding (ams_id*4+slot_id, or 254/255 for external)."""
        return await self._request(
            "POST", f"/api/v1/printers/{printer_id}/ams/load", params={"tray_id": tray_id}
        )

    async def ams_unload(self, printer_id: int) -> Any:
        """POST /printers/{id}/ams/unload — unload the currently loaded filament."""
        return await self._request("POST", f"/api/v1/printers/{printer_id}/ams/unload")

    async def ams_slot_refresh(self, printer_id: int, ams_id: int, slot_id: int) -> Any:
        """POST /printers/{id}/ams/{ams_id}/slot/{slot_id}/refresh — re-read RFID."""
        return await self._request(
            "POST",
            f"/api/v1/printers/{printer_id}/ams/{ams_id}/slot/{slot_id}/refresh",
        )

    async def ams_tray_reset(self, printer_id: int, ams_id: int, tray_id: int) -> Any:
        """POST /printers/{id}/ams/{ams_id}/tray/{tray_id}/reset — clear a slot's
        filament config (per-AMS tray_id 0-3)."""
        return await self._request(
            "POST",
            f"/api/v1/printers/{printer_id}/ams/{ams_id}/tray/{tray_id}/reset",
        )

    async def start_drying(
        self,
        printer_id: int,
        ams_id: int,
        *,
        temp: int = 45,
        duration: int = 4,
        filament: str = "",
        rotate_tray: bool = False,
    ) -> Any:
        """POST /printers/{id}/drying/start — temp 45-85°C, duration 1-24h. Empty
        `filament` lets BB backfill from the loaded tray (default PLA)."""
        params: dict[str, Any] = {
            "ams_id": ams_id,
            "temp": temp,
            "duration": duration,
            "rotate_tray": rotate_tray,
        }
        if filament:
            params["filament"] = filament
        return await self._request(
            "POST", f"/api/v1/printers/{printer_id}/drying/start", params=params
        )

    async def stop_drying(self, printer_id: int, ams_id: int) -> Any:
        """POST /printers/{id}/drying/stop?ams_id=N."""
        return await self._request(
            "POST", f"/api/v1/printers/{printer_id}/drying/stop", params={"ams_id": ams_id}
        )

    async def configure_slot(
        self,
        printer_id: int,
        ams_id: int,
        tray_id: int,
        *,
        tray_info_idx: str,
        tray_type: str,
        tray_sub_brands: str,
        tray_color: str,
        nozzle_temp_min: int,
        nozzle_temp_max: int,
        cali_idx: int = -1,
        nozzle_diameter: str = "0.4",
        setting_id: str = "",
        kprofile_filament_id: str = "",
        kprofile_setting_id: str = "",
        k_value: float = 0.0,
    ) -> Any:
        """POST /printers/{id}/slots/{ams_id}/{tray_id}/configure — the filament
        PROFILE PUSH (the SpoolTap-relevant write). `tray_id` is the per-AMS 0-3
        index. All config travels as query params (no JSON body)."""
        params: dict[str, Any] = {
            "tray_info_idx": tray_info_idx,
            "tray_type": tray_type,
            "tray_sub_brands": tray_sub_brands,
            "tray_color": tray_color,
            "nozzle_temp_min": nozzle_temp_min,
            "nozzle_temp_max": nozzle_temp_max,
            "cali_idx": cali_idx,
            "nozzle_diameter": nozzle_diameter,
            "setting_id": setting_id,
            "kprofile_filament_id": kprofile_filament_id,
            "kprofile_setting_id": kprofile_setting_id,
            "k_value": k_value,
        }
        return await self._request(
            "POST",
            f"/api/v1/printers/{printer_id}/slots/{ams_id}/{tray_id}/configure",
            params=params,
        )

    async def set_print_option(
        self,
        printer_id: int,
        module_name: str,
        enabled: bool,
        *,
        print_halt: bool = True,
        sensitivity: str = "medium",
    ) -> Any:
        """POST /printers/{id}/print-options — toggle an AI-detection module."""
        return await self._request(
            "POST",
            f"/api/v1/printers/{printer_id}/print-options",
            params={
                "module_name": module_name,
                "enabled": enabled,
                "print_halt": print_halt,
                "sensitivity": sensitivity,
            },
        )

    async def skip_objects(self, printer_id: int, object_ids: list[int]) -> Any:
        """POST /printers/{id}/print/skip-objects — the ONE JSON-body route: the
        body is a bare array of object ids, e.g. [12, 34]."""
        return await self._request(
            "POST", f"/api/v1/printers/{printer_id}/print/skip-objects", json=object_ids
        )

    async def save_slot_preset(
        self,
        printer_id: int,
        ams_id: int,
        tray_id: int,
        *,
        preset_id: str,
        preset_name: str,
        preset_source: str = "cloud",
    ) -> Any:
        """PUT /printers/{id}/slot-presets/{ams_id}/{tray_id} — DB-only mapping."""
        return await self._request(
            "PUT",
            f"/api/v1/printers/{printer_id}/slot-presets/{ams_id}/{tray_id}",
            params={
                "preset_id": preset_id,
                "preset_name": preset_name,
                "preset_source": preset_source,
            },
        )

    async def delete_slot_preset(self, printer_id: int, ams_id: int, tray_id: int) -> Any:
        """DELETE /printers/{id}/slot-presets/{ams_id}/{tray_id}."""
        return await self._request(
            "DELETE",
            f"/api/v1/printers/{printer_id}/slot-presets/{ams_id}/{tray_id}",
        )

    async def start_calibration(
        self,
        printer_id: int,
        *,
        bed_leveling: bool = False,
        vibration: bool = False,
        motor_noise: bool = False,
        nozzle_offset: bool = False,
        high_temp_heatbed: bool = False,
    ) -> Any:
        """POST /printers/{id}/calibration — at least one routine must be true."""
        return await self._request(
            "POST",
            f"/api/v1/printers/{printer_id}/calibration",
            params={
                "bed_leveling": bed_leveling,
                "vibration": vibration,
                "motor_noise": motor_noise,
                "nozzle_offset": nozzle_offset,
                "high_temp_heatbed": high_temp_heatbed,
            },
        )

    # ---- Virtual Printers (P2.5 — a SEPARATE REST-only surface) ---------
    # No WS frame exists for VPs, so these feed a plain polling coordinator.
    # VP `id` overlaps real-printer `id` (separate DB tables) — callers key the
    # HA device on `vp-{id}`, never mix these into the printer_id space.

    async def list_virtual_printers(self) -> dict:
        """GET /api/v1/virtual-printers — {printers:[...], models:{code:name}}.
        Polled on the backstop timer (no WS). Tolerant of a non-dict body."""
        data = await self._get("/api/v1/virtual-printers")
        return data if isinstance(data, dict) else {"printers": []}

    async def get_virtual_printer(self, vp_id: int) -> dict:
        """GET /api/v1/virtual-printers/{vp_id} — one VP (post-write confirm read)."""
        data = await self._get(f"/api/v1/virtual-printers/{vp_id}")
        return data if isinstance(data, dict) else {}

    async def update_virtual_printer(self, vp_id: int, **changes: Any) -> Any:
        """PUT /api/v1/virtual-printers/{vp_id} — PATCH semantics: BB only assigns
        body fields that are present (`if body.X is not None`, verified in
        `routes/virtual_printers.py`), so we send ONLY the changed field(s) as a
        JSON body (bools native — no query normalization). We never send
        `access_code`: BB force-inherits it from the target printer for non-proxy
        VPs, so there is nothing secret to push or read back."""
        return await self._request(
            "PUT", f"/api/v1/virtual-printers/{vp_id}", json=changes
        )

    async def get_bytes(self, path: str) -> bytes | None:
        """GET a raw binary body (e.g. the cover PNG). None on non-200 so a
        missing/idle cover degrades to 'no image' rather than raising."""
        async with self._session.get(
            f"{self._base}{path}", headers=self._headers(), timeout=_TIMEOUT
        ) as resp:
            if resp.status != 200:
                return None
            return await resp.read()

    async def get_ws_token(self) -> str | None:
        """POST /api/v1/auth/ws-token — mint an ephemeral WS token.

        Only meaningful when Bambuddy auth is enabled (it is OFF on the LAN
        today). Returns None on any non-200 so the caller falls back to a
        token-less connection.
        """
        try:
            async with self._session.post(
                f"{self._base}/api/v1/auth/ws-token",
                headers=self._headers(),
                timeout=_TIMEOUT,
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                return data.get("token")
        except Exception as err:  # noqa: BLE001 - token is best-effort
            _LOGGER.debug("ws-token mint failed (auth likely off): %s", err)
            return None
