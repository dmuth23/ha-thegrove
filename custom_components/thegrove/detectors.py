"""AI-detection (xcam) module table — Bambuddy's `print_options` detectors.

Shared by the print_options switches (switch.py) and the sensitivity selects
(select.py).

Two BB quirks this table pins (read from BB source `set_xcam_option`, the live
`print_options` frame, and the route's `valid_modules`):
- **WRITE name != READ name for the clump detector:** Bambu accepts
  `clump_detector` on `set_print_option` but REPORTS it back as
  `nozzle_clumping_detector` in `print_options`. (Verify on the live write test.)
- **Per-module command, no cross-module reset:** `xcam_control_set` targets ONE
  module; toggling one detector does NOT touch the others. Each detector's
  sensitivity reads under its own `*_sensitivity` key but writes via the single
  `halt_print_sensitivity` field (interpreted per `module_name`).

`filament_tangle_detect` appears in `print_options` (read-only) but is NOT in the
route's writable `valid_modules`, so it gets no switch.

KNOWN LIMITATION — cross-entity staleness within the 30 s backstop window
(advisor-caught, deliberately NOT fixed unsupervised):
  A detector's enabled-bool and its sensitivity are TWO entities (a switch + a
  select) that BOTH read from `print_options`, which only refreshes on the 30 s
  REST backstop (it's REST-only sticky — absent from the ~1.5 s WS frame). On a
  WRITE, each passes the OTHER dimension THROUGH from its last-known
  `print_options` (switch sends the current sensitivity; select sends the current
  enabled). So if you toggle the switch and then change the sensitivity within
  the SAME 30 s window — before a backstop refresh lands the first write's result
  — the second write carries a stale value for the first dimension and can clobber
  it. It is SELF-HEALING (the next backstop reconciles to the printer's truth) and
  harmless, but it can look like a brief flicker. The real fix is a post-write
  `/status` refresh (re-pull `print_options` immediately after a detector write) —
  a supervised daylight task, on the live-test watch list. Until then: nudge one
  detector dimension at a time and let a backstop tick land between changes.
"""

from __future__ import annotations

from dataclasses import dataclass

from .brain.model import PrinterModel


@dataclass(frozen=True)
class Detector:
    key: str  # thegrove entity key
    name: str  # friendly name
    write_module: str  # set_print_option module_name (the WRITE name)
    enabled_key: str  # print_options READ key for the on/off bool
    sensitivity_key: str | None  # print_options READ key for sensitivity, or None


DETECTORS: tuple[Detector, ...] = (
    Detector("spaghetti", "Spaghetti detection", "spaghetti_detector",
             "spaghetti_detector", "halt_print_sensitivity"),
    Detector("first_layer", "First-layer inspection", "first_layer_inspector",
             "first_layer_inspector", None),
    Detector("ai_monitoring", "AI print monitoring", "printing_monitor",
             "printing_monitor", None),
    Detector("buildplate_marker", "Build-plate marker detection",
             "buildplate_marker_detector", "buildplate_marker_detector", None),
    Detector("skip_parts", "Allow skipping failed parts", "allow_skip_parts",
             "allow_skip_parts", None),
    Detector("pileup", "Pile-up detection", "pileup_detector",
             "pileup_detector", "pileup_sensitivity"),
    Detector("clump", "Nozzle-clump detection", "clump_detector",
             "nozzle_clumping_detector", "nozzle_clumping_sensitivity"),
    Detector("airprint", "Air-printing detection", "airprint_detector",
             "airprint_detector", "airprint_sensitivity"),
    Detector("auto_recovery", "Auto-recovery (step loss)", "auto_recovery_step_loss",
             "auto_recovery_step_loss", None),
)

#: Valid sensitivity levels (BB route `valid_sensitivities`).
SENSITIVITY_OPTIONS = ["low", "medium", "high", "never_halt"]


def po_dict(model: PrinterModel) -> dict:
    """print_options as a dict (stored on the model as sorted (key,value) pairs)."""
    return dict(model.print_options)


def detector_enabled(model: PrinterModel, det: Detector) -> bool | None:
    """The detector's on/off state, or None if the key isn't present yet."""
    val = po_dict(model).get(det.enabled_key)
    return None if val is None else bool(val)


def detector_sensitivity(model: PrinterModel, det: Detector) -> str | None:
    """The detector's current sensitivity, or None (no sensitivity / not present)."""
    if det.sensitivity_key is None:
        return None
    val = po_dict(model).get(det.sensitivity_key)
    return str(val) if val is not None else None
