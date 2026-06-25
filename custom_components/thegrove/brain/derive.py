"""Pure derivations over a PrinterModel — values BB does NOT hand us directly.

HA-free, Bambuddy-free: same fast-loop testability as `mapper.py`, kept SEPARATE
from it so the mapper stays a straight pass-through of BB's frame while the
derived logic (which encodes Bambu firmware conventions) is unit-testable in
isolation.

What's here:
- `active_ams_tray` / `is_ams_active` / `is_tray_active` — which AMS+tray is
  loaded to the nozzle, decoded from `tray_now`. PROVEN vs the live Sharkie frame
  (`tray_now=5` -> AMS 1 / tray 1) and cross-checked against ha-bambulab's
  pybambu decode.

- `print_error` / `filament_runout` / HMS helpers — gate semantics PINNED from a
  live runout reproduction (Session 2, 2026-06-24): BB emits `state="PAUSE"`
  (UPPERCASE) on runout, NOT "FAILED", and surfaces runout as HMS code `0701_8011`
  (the `_8011` "filament ran out" family) — top-level `print_error` stayed None.
  Per OD (user picked option (b)): `print_error` = hard-fail OR an allowlisted
  fault code present (stateless, frame-local). See `print_error` below.
"""

from __future__ import annotations

from .model import HmsError, PrinterModel

# --- HMS short codes + fault classification ---------------------------------
# Runout was a PAUSE, so a bare-state gate is wrong; the HMS code is the precise
# signal (it outlives the PAUSE->RUNNING flip by ~2 s — observed in the recovery
# timeline, because state and HMS come from DIFFERENT subsystems). `gcode_state`
# values are UPPERCASE (OD-F casing confirmed).
_STATE_FAILED = "FAILED"
# A code is a REAL error only if its error portion (code & 0xFFFF) >= 0x4000.
# This is BB's OWN rule (bambu_mqtt.py): 0x4xxx fatal / 0x8xxx warning /
# 0xCxxx prompt; lower values are status/phase indicators firmware emits during
# normal operation. Live PHANTOMS caught this way (all error-portion < 0x4000):
# 0500_0070 (mod5, persistent), 0701_0025 (mod7, persistent post-recovery),
# 0701_0001 (transient runout precursor). Beats a hand-maintained denylist —
# the second phantom (0701_0025) appeared only after the runout recovery.
_REAL_HMS_MIN = 0x4000
# Non-AMS "filament ran out" short codes (BB hms_errors.py: 0300_8004 spool,
# 0300_8015 external). The AMS family (07xx/12xx/18xx _8011) is matched by rule.
_NONAMS_RUNOUT = frozenset({"0300_8004", "0300_8015"})


def hms_short_code(err: HmsError) -> str | None:
    """Reconstruct BB's community short code `MMMM_EEEE` from an HmsError —
    `MMMM = (attr>>16)&0xFFFF` (module<<8 | unit), `EEEE = code&0xFFFF`."""
    if err.code is None or err.attr is None:
        return None
    try:
        code = int(err.code, 16)
    except (TypeError, ValueError):
        return None
    return f"{(err.attr >> 16) & 0xFFFF:04X}_{code & 0xFFFF:04X}"


def is_real_hms(err: HmsError) -> bool:
    """A genuine HMS error vs a status/phase 'phantom'. BB's rule: error portion
    `(code & 0xFFFF) >= 0x4000`. Excludes the undocumented status codes firmware
    leaves in the list during normal printing (which BB itself never notifies on,
    having no description-DB entry for them)."""
    if err.code is None:
        return False
    try:
        return (int(err.code, 16) & 0xFFFF) >= _REAL_HMS_MIN
    except (TypeError, ValueError):
        return False


def is_runout_code(short: str) -> bool:
    """The 'filament ran out' HMS family (verified vs BB hms_errors.py).

    `_8011` under an AMS-class module (07=AMS, 12=AMS gen2, 18=AMS-HT) = runout
    in that slot/unit — covers 0700..0707 / 07FE / 07FF / 12xx / 18xx. The two
    non-AMS spool-runout codes are listed explicitly. NB `0300_8011` is *not*
    runout (it's a wrong-plate error), so we can't match the `_8011` suffix alone.
    """
    return (short.endswith("_8011") and short[:2] in {"07", "12", "18"}) or short in _NONAMS_RUNOUT


def real_hms(model: PrinterModel) -> list[tuple[str, HmsError]]:
    """(short_code, err) for every CURRENT genuine HMS error — status/phase
    'phantom' codes (error portion < 0x4000) excluded."""
    out: list[tuple[str, HmsError]] = []
    for e in model.hms_errors:
        if not is_real_hms(e):
            continue
        short = hms_short_code(e)
        if short:
            out.append((short, e))
    return out


def filament_runout(model: PrinterModel) -> bool:
    """OD-E: BB's runout event, surfaced standalone. Per-printer for v1 (the
    `MMMM` low byte encodes the AMS unit — 0701 = unit 1 — but one sample isn't
    enough to publish per-AMS attribution; revisit). Same source as the runout
    arm of `print_error`."""
    return any(is_runout_code(short) for short, _ in real_hms(model))


def print_error(model: PrinterModel) -> bool:
    """Option (b): a real print-stopping/-interrupting fault is active.

    UNION of two independent arms (neither subsumes the other):
    - `state == "FAILED"` **AND a real fault code is present** — a genuine crash.
      The `AND` is load-bearing: a user CANCEL also lands on `FAILED`, but with
      NO fault code (proven live 2026-06-24 — cancel-from-slicer → FAILED,
      faultcodes empty). So `FAILED` alone must NOT fire, or every "stop" reads as
      an error (false alarm). A real failure carries its fault code; a cancel
      doesn't — that's the distinguisher.
    - an ALLOWLISTED fault code (runout family) is present — pause-faults like
      runout that never set FAILED (runout is a PAUSE).

    Frame-local + stateless. `real_hms` already drops the persistent phantoms;
    the runout allowlist keeps benign `0xCxxx` prompts (planned filament change)
    from firing arm 2. v1 fault allowlist = the runout family; it grows as more
    fault classes are captured (accepted false-negative on unseen ones).
    """
    real = real_hms(model)
    if model.state == _STATE_FAILED and real:
        return True
    return any(is_runout_code(short) for short, _ in real)

# `tray_now` sentinels — Bambu firmware convention. Cross-checked against
# ha-bambulab pybambu/models.py (the active-tray decode) AND the live Sharkie
# frame (tray_now=5 -> AMS 1 / tray 1). The decode + thresholds below are pinned
# to Bambuddy's OWN producer-side decode, `usage_tracker._global_to_ams_key`
# (usage_tracker.py:530-535) — the authority, since BB emits both `tray_now` and
# the `ams[].id` we index into. BB: `>=254 -> VT`, `>=128 -> AMS-HT (id==global)`,
# else `(global//4, global%4)`. We index the same `ams[].id`, so our decode must
# match BB's, not diverge from it.
_TRAY_NOW_NONE = 255  # nothing loaded to the nozzle (idle)
_TRAY_NOW_EXTERNAL = 254  # external spool feeds the nozzle (NOT an AMS tray)
_TRAY_NOW_AMS_HT = 128  # >= this -> AMS-HT single-tray unit; raw index = ams id
# (BB's threshold — was wrongly 80; latent on Sharkie, which never reaches it)


def active_ams_tray(model: PrinterModel) -> tuple[int | None, int | None]:
    """Decode (active_ams_id, active_tray_id) from `tray_now`.

    Single-extruder decode (P/X/A-series, incl. Sharkie): `tray_now` is a GLOBAL
    tray index across all 4-tray AMS units, so `ams = tray_now >> 2` and
    `tray = tray_now & 3`. Sentinels 254/255 -> no AMS tray active; values
    >= 128 -> an AMS-HT single-tray unit (raw index, tray 0). Returns
    `(None, None)` when nothing (or only the external spool) is loaded.

    H2D (dual-extruder) is intentionally NOT handled — it folds per-nozzle
    indices differently and needs an H2D on the bench. Sharkie is single-extruder
    (`ams_extruder_map={0:0, 1:0}`, `active_extruder=0`), so the global decode is
    correct for every printer we target today.
    """
    tn = model.tray_now
    if tn is None or tn in (_TRAY_NOW_NONE, _TRAY_NOW_EXTERNAL):
        return (None, None)
    if tn >= _TRAY_NOW_AMS_HT:
        return (tn, 0)
    return (tn >> 2, tn & 0x3)


def global_tray_id(ams_id: int, tray_id: int) -> int:
    """The GLOBAL tray index BB's `ams/load` wants — `ams_id*4 + tray_id`.

    SCOPE: inverts ONLY the regular 4-tray branch of `active_ams_tray` (the
    `tray_now >> 2 / & 3` case), which is every printer we target today
    (single-extruder, standard AMS — proven on the live frame). It is NOT the
    inverse of the AMS-HT branch (`ams.id >= 80`, single-tray, raw index) or the
    H2D dual-extruder fold — those need their own encoding before `ams/load`
    is trusted on that hardware."""
    return ams_id * 4 + tray_id


def is_ams_active(model: PrinterModel, ams_id: int) -> bool:
    """True when this AMS is the one currently feeding the nozzle."""
    active_ams, _ = active_ams_tray(model)
    return active_ams is not None and active_ams == ams_id


def is_tray_active(model: PrinterModel, ams_id: int, tray_id: int) -> bool:
    """True when this specific tray is the one loaded to the nozzle."""
    active_ams, active_tray = active_ams_tray(model)
    return active_ams == ams_id and active_tray == tray_id and active_ams is not None


def active_material(model: PrinterModel) -> str | None:
    """The material loaded to the nozzle right now, as a string (e.g. "PLA").

    Resolves the active AMS tray's `tray_type`, or — when the EXTERNAL spool feeds
    the nozzle (`tray_now == 254`) — the external spool's material. None when idle
    (255), when the active tray/spool is empty, or when the decode finds nothing.
    This is the printer-level "active material" the door/notification consumers
    read off ha-bambulab's `active_tray`.
    """
    ams_id, tray_id = active_ams_tray(model)
    if ams_id is not None and tray_id is not None:
        ams = next((a for a in model.ams if a.id == ams_id), None)
        if ams is None:
            return None
        tray = next((t for t in ams.trays if t.id == tray_id), None)
        return tray.tray_type if tray is not None else None
    if model.tray_now == _TRAY_NOW_EXTERNAL and model.vt_tray is not None:
        return model.vt_tray.tray_type
    return None
