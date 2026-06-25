"""Pure mapper: a Bambuddy `printer_status` dict -> PrinterModel.

No HA imports, no Bambuddy imports — a pure function, unit-testable in the fast
loop. Type coercion happens HERE, once, at the boundary: Bambuddy passes some
fields straight through from raw MQTT with no coercion, so they arrive as
whatever the firmware sent (`"190"` or `190`). The mapper tolerates both.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Any

from .model import AmsUnit, HmsError, PrinterModel, Tray


def _as_int(value: Any) -> int | None:
    """Coerce str/int/float -> int, tolerant of None/empty/garbage."""
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_bool(value: Any) -> bool | None:
    """Coerce to bool, preserving None (so 'unknown' != 'off')."""
    if value is None:
        return None
    return bool(value)


def _clean_str(value: Any) -> str | None:
    """Non-empty string, else None."""
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _clean_tag(value: Any) -> str | None:
    """Tag UID, dropping the all-zero / empty sentinels BB and firmware emit."""
    s = _clean_str(value)
    if s is None:
        return None
    if set(s) <= {"0"}:  # "0000000000000000" -> no tag present
        return None
    return s


def _map_tray(tray: dict) -> Tray:
    return Tray(
        id=_as_int(tray.get("id")) or 0,
        tray_type=_clean_str(tray.get("tray_type")),
        tray_color=_clean_str(tray.get("tray_color")),
        tray_info_idx=_clean_str(tray.get("tray_info_idx")),  # alphanumeric — keep str
        tray_sub_brands=_clean_str(tray.get("tray_sub_brands")),
        remain=_as_int(tray.get("remain")),
        state=_as_int(tray.get("state")),
        nozzle_temp_min=_as_int(tray.get("nozzle_temp_min")),
        nozzle_temp_max=_as_int(tray.get("nozzle_temp_max")),
        tag_uid=_clean_tag(tray.get("tag_uid")),
        cali_idx=_as_int(tray.get("cali_idx")),
        drying_temp=_as_int(tray.get("drying_temp")),
        drying_time=_as_int(tray.get("drying_time")),
    )


def _map_vt_tray(value: Any) -> Tray | None:
    """The external spool: BB sends `vt_tray` as a 1-element list of a Tray-shaped
    dict (id=254). Reuse _map_tray. Empty list / absent / empty material -> the
    Tray still maps (tray_type None = no external spool loaded); only a non-list
    or missing value yields None."""
    if isinstance(value, list):
        return _map_tray(value[0]) if value else None
    if isinstance(value, dict):  # tolerate a non-list shape
        return _map_tray(value)
    return None


def _map_ams(ams: dict) -> AmsUnit:
    trays = tuple(_map_tray(t) for t in (ams.get("tray") or []))
    return AmsUnit(
        id=_as_int(ams.get("id")) or 0,
        humidity=_as_int(ams.get("humidity")),
        temp=_as_float(ams.get("temp")),
        is_ams_ht=bool(ams.get("is_ams_ht")),
        dry_status=_as_int(ams.get("dry_status")),
        dry_sub_status=_as_int(ams.get("dry_sub_status")),
        dry_time=_as_int(ams.get("dry_time")),
        sw_ver=_clean_str(ams.get("sw_ver")),
        trays=trays,
    )


def _map_hms(err: dict) -> HmsError:
    return HmsError(
        code=_clean_str(err.get("code")),
        attr=_as_int(err.get("attr")),
        module=_as_int(err.get("module")),
        severity=_as_int(err.get("severity")),
    )


def _map_extruder_map(raw: Any) -> tuple[tuple[int, int], ...]:
    """BB sends ams_extruder_map as {"0": 0, "1": 0} (str keys). Coerce to a
    sorted tuple of (ams_id, extruder) int pairs for the `_active` derivation."""
    if not isinstance(raw, dict):
        return ()
    pairs = []
    for k, v in raw.items():
        ams_id = _as_int(k)
        extruder = _as_int(v)
        if ams_id is not None and extruder is not None:
            pairs.append((ams_id, extruder))
    return tuple(sorted(pairs))


def _as_print_options(value: Any) -> tuple[tuple[str, bool | str], ...]:
    """Flatten BB's `print_options` dict (AI-detector toggles + per-module
    sensitivity strings) to sorted (key, value) pairs — frozen-friendly for the
    model. REST-only; absent from the WS frame -> () there, sticky-merged back."""
    if not isinstance(value, dict):
        return ()
    return tuple(sorted(((str(k), v) for k, v in value.items()), key=lambda kv: kv[0]))


def map_printer_status(
    data: dict,
    *,
    printer_id: int,
    serial: str,
    model: str,
    name: str,
) -> PrinterModel:
    """Map ONE printer's `printer_status.data` dict to a PrinterModel.

    Identity (printer_id/serial/model/name) is supplied by the caller — the WS
    frame does not carry the serial. REST-only fields default to None and are
    filled later by `merge_rest_only`.
    """
    ams = tuple(_map_ams(a) for a in (data.get("ams") or []))
    hms = tuple(_map_hms(e) for e in (data.get("hms_errors") or []))
    temps = data.get("temperatures") or {}
    nozzles = data.get("nozzles") or []
    nozzle0 = nozzles[0] if nozzles else {}
    return PrinterModel(
        printer_id=printer_id,
        serial=serial,
        model=model,
        name=name,
        state=_clean_str(data.get("state")),
        progress=_as_float(data.get("progress")),
        remaining_time=_as_int(data.get("remaining_time")),
        layer_num=_as_int(data.get("layer_num")),
        total_layers=_as_int(data.get("total_layers")),
        subtask_name=_clean_str(data.get("subtask_name")),
        stg_cur=_as_int(data.get("stg_cur")),
        stg_cur_name=_clean_str(data.get("stg_cur_name")),
        nozzle_temp=_as_float(temps.get("nozzle")),
        nozzle_target_temp=_as_float(temps.get("nozzle_target")),
        bed_temp=_as_float(temps.get("bed")),
        bed_target_temp=_as_float(temps.get("bed_target")),
        chamber_temp=_as_float(temps.get("chamber")),
        chamber_target_temp=_as_float(temps.get("chamber_target")),
        nozzle_heating=_as_bool(temps.get("nozzle_heating")),
        bed_heating=_as_bool(temps.get("bed_heating")),
        chamber_heating=_as_bool(temps.get("chamber_heating")),
        cooling_fan_speed=_as_int(data.get("cooling_fan_speed")),
        big_fan1_speed=_as_int(data.get("big_fan1_speed")),
        big_fan2_speed=_as_int(data.get("big_fan2_speed")),
        heatbreak_fan_speed=_as_int(data.get("heatbreak_fan_speed")),
        door_open=_as_bool(data.get("door_open")),
        wifi_signal=_as_int(data.get("wifi_signal")),
        connected=_as_bool(data.get("connected")),
        chamber_light=_as_bool(data.get("chamber_light")),
        speed_level=_as_int(data.get("speed_level")),
        firmware_version=_clean_str(data.get("firmware_version")),
        store_to_sdcard=_as_bool(data.get("store_to_sdcard")),
        print_options=_as_print_options(data.get("print_options")),
        supports_drying=_as_bool(data.get("supports_drying")),
        cover_url=_clean_str(data.get("cover_url")),
        current_plate_id=_as_int(data.get("current_plate_id")),
        awaiting_plate_clear=_as_bool(data.get("awaiting_plate_clear")),
        nozzle_type=_clean_str(nozzle0.get("nozzle_type")),
        nozzle_diameter=_clean_str(nozzle0.get("nozzle_diameter")),
        tray_now=_as_int(data.get("tray_now")),
        active_extruder=_as_int(data.get("active_extruder")),
        ams_extruder_map=_map_extruder_map(data.get("ams_extruder_map")),
        hms_errors=hms,
        ams=ams,
        vt_tray=_map_vt_tray(data.get("vt_tray")),
    )


def merge_rest_only(
    model: PrinterModel,
    *,
    current_archive_id: int | None = None,
    print_weight_grams: float | None = None,
    print_start_time: datetime | None = None,
    firmware_version: str | None = None,
    nozzle_type: str | None = None,
    nozzle_diameter: str | None = None,
    store_to_sdcard: bool | None = None,
    print_options: tuple[tuple[str, bool | str], ...] = (),
    printable_objects: tuple[tuple[int, str], ...] = (),
    slot_presets: tuple[tuple[int, int, str, str], ...] = (),
) -> PrinterModel:
    """Pure sticky-merge: carry the REST-only fields onto a WS-built model.

    A raw WS frame carries only 37 keys; REST `/status` is a 53-key superset
    (verified 2026-06-24). The fields here live ONLY in `/status`, so a naively
    WS-mapped model blanks them every ~1.5 s — `current_archive_id` flaps the
    delayed-print math; `firmware_version`/`nozzle_*` flap a static value to
    "unknown" between the 30 s polls. The coordinator holds the last-known REST
    values and merges them in via this function. Returns a NEW frozen instance —
    the input is never mutated. (Other REST-only keys — airduct_mode, sdcard,
    timelapse, … — are not surfaced as entities, so they need no sticky merge.)
    """
    return replace(
        model,
        current_archive_id=current_archive_id,
        print_weight_grams=print_weight_grams,
        print_start_time=print_start_time,
        firmware_version=firmware_version,
        nozzle_type=nozzle_type,
        nozzle_diameter=nozzle_diameter,
        store_to_sdcard=store_to_sdcard,
        print_options=print_options,
        printable_objects=printable_objects,
        slot_presets=slot_presets,
    )
