"""Internal typed model for one printer — pure, HA-free, Bambuddy-free.

Frozen dataclasses: a PrinterModel is an immutable snapshot pushed to entities.
The coordinator builds a fresh one per WS frame, then sticky-merges the
REST-only fields onto it via `mapper.merge_rest_only` (which returns a new
instance — frozen, so the original is never mutated).

Field set is derived from the OBSERVED live `printer_status` frame (P2,
2026-06-24), not the wiki — per the plan's drift rule (pin to the box, the doc
leads the artifact).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class HmsError:
    """One structured HMS (Health Management System) fault entry.

    BB pre-decodes these — `code` is a hex string (e.g. "0x20070"), and BB
    supplies its OWN `severity` int (observed scale differs from ha-bambulab's
    code>>16 map; the print_error derivation pins BB's scale in its own pass).
    """

    code: str | None = None
    attr: int | None = None
    module: int | None = None
    severity: int | None = None


@dataclass(frozen=True, slots=True)
class Tray:
    """One AMS tray slot."""

    id: int
    tray_type: str | None = None  # material string ("PLA"/"PLA+"/…); "" -> None
    tray_color: str | None = None  # RGBA hex
    # tray_info_idx is an ALPHANUMERIC Bambu profile code (e.g. "P6342d81") —
    # genuinely a string, not a number to coerce. (=ha-bambulab `filament_id`.)
    tray_info_idx: str | None = None
    tray_sub_brands: str | None = None
    remain: int | None = None  # -1 = unknown (Bambu convention)
    state: int | None = None
    nozzle_temp_min: int | None = None
    nozzle_temp_max: int | None = None
    tag_uid: str | None = None  # RFID chip id; junk/zeros -> None (SpoolTap key)
    cali_idx: int | None = None
    drying_temp: int | None = None
    drying_time: int | None = None


@dataclass(frozen=True, slots=True)
class AmsUnit:
    """One AMS unit (4-tray AMS, or 1-tray AMS-HT)."""

    id: int
    humidity: int | None = None  # raw % when humidity_raw present; see §5 caveat
    temp: float | None = None
    is_ams_ht: bool = False
    dry_status: int | None = None
    dry_sub_status: int | None = None
    dry_time: int | None = None
    sw_ver: str | None = None
    trays: tuple[Tray, ...] = ()


@dataclass(frozen=True, slots=True)
class PrinterModel:
    """Immutable snapshot of one printer's state."""

    # --- identity (from GET /printers/ at config-flow time, NOT the WS frame) ---
    printer_id: int  # Bambuddy DB id — WS-demux routing key only
    serial: str  # the truly-stable identity key
    model: str
    name: str

    # --- live print body (from the WS printer_status frame) ---
    state: str | None = None  # decoded gcode_state: IDLE/RUNNING/PAUSE/FAILED/FINISH
    progress: float | None = None
    remaining_time: int | None = None  # minutes (live, authoritative)
    layer_num: int | None = None
    total_layers: int | None = None
    subtask_name: str | None = None
    stg_cur: int | None = None
    stg_cur_name: str | None = None  # human stage string ("Changing filament")

    # --- temperatures (°C) + heating flags ---
    nozzle_temp: float | None = None
    nozzle_target_temp: float | None = None
    bed_temp: float | None = None
    bed_target_temp: float | None = None
    chamber_temp: float | None = None
    chamber_target_temp: float | None = None
    nozzle_heating: bool | None = None
    bed_heating: bool | None = None
    chamber_heating: bool | None = None

    # --- fans (% / 0-100ish, raw from BB) ---
    cooling_fan_speed: int | None = None
    big_fan1_speed: int | None = None
    big_fan2_speed: int | None = None
    heatbreak_fan_speed: int | None = None

    # --- environment / misc body ---
    door_open: bool | None = None
    wifi_signal: int | None = None  # dBm (negative)
    connected: bool | None = None  # per-printer link state inside the frame
    chamber_light: bool | None = None
    speed_level: int | None = None
    firmware_version: str | None = None
    # store_to_sdcard: the "Store sent files on external storage" toggle (home_flag
    # bit 11). REST-only -> sticky-merged. READ-ONLY (no remote write exists in BB
    # or the Bambu MQTT protocol; touchscreen-only on P2S fw 01.02).
    store_to_sdcard: bool | None = None
    supports_drying: bool | None = None
    cover_url: str | None = None
    current_plate_id: int | None = None
    awaiting_plate_clear: bool | None = None
    nozzle_type: str | None = None
    nozzle_diameter: str | None = None
    # print_options: BB's AI-detection toggles + per-module sensitivity strings,
    # a flat dict flattened to sorted (key, value) pairs (frozen-friendly).
    # REST-only -> sticky-merged (absent from the WS frame).
    print_options: tuple[tuple[str, bool | str], ...] = ()

    # --- active-tray inputs (feed the `_active` per-AMS derivation) ---
    tray_now: int | None = None
    active_extruder: int | None = None
    # ams_extruder_map: ams_id -> extruder index (BB sends str keys; coerced)
    ams_extruder_map: tuple[tuple[int, int], ...] = ()

    hms_errors: tuple[HmsError, ...] = ()
    ams: tuple[AmsUnit, ...] = ()
    # The external spool (BB's `vt_tray`, id=254) — a Tray-shaped slot that hangs
    # off the printer, NOT inside an AMS. None when absent/empty. On the WS frame
    # (not REST-only), so no sticky merge.
    vt_tray: Tray | None = None

    # --- REST-only sticky fields (NEVER on the WS frame) ---
    # The WS dict lacks these, so a naive per-frame map blanks them every ~1.5 s.
    # The coordinator holds the last-known values and merges them in.
    current_archive_id: int | None = None
    print_weight_grams: float | None = None
    print_start_time: datetime | None = None  # tz-aware UTC (BB stamps UTC)
    # Fetched on the 30 s backstop via their own GETs (not in /status) -> sticky.
    printable_objects: tuple[tuple[int, str], ...] = ()  # (id, name) of the active print
    slot_presets: tuple[tuple[int, int, str, str], ...] = ()  # (ams_id, tray_id, preset_id, preset_name)
