"""Find the archive for the CURRENT print when `current_archive_id` is absent.

Pure, HA-free, Bambuddy-free — unit-testable in the fast loop.

Why this exists: BB only links `current_archive_id` for prints IT dispatched
(matched via `subtask_id`). A **printer-initiated** print is never dispatched, so
the live status carries no link — yet BB still creates the archive (with
`started_at` + a weight estimate). So we recover it by querying the archive list
and picking the active row. Proven live (2026-06-24): printer-initiated reprint
had `current_archive_id=None` but archive 89 (status="printing", newest) held the
real start time + weight.

Orphan-row guard (P0-FINDINGS §5): a crashed print can leave a stale
`status="printing"` archive forever. We mitigate with THREE filters — the caller
only matches while actually printing (state RUNNING/PAUSE), we cross-check the
live print NAME, and we pick the NEWEST started_at (the current print's archive
is always newest). Residual risk: a same-name orphan newer than the real row,
which is effectively impossible in practice.
"""

from __future__ import annotations

from typing import Any


def _norm_name(s: Any) -> str:
    """Normalize a print name for comparison. The live `subtask_name` is
    underscore-joined ("Grocery_Bag_Holder") while the archive `print_name` is
    spaced ("Grocery Bag Holder"), so fold both."""
    return str(s or "").replace("_", " ").strip().lower()


def pick_active_archive(
    rows: list[dict], *, subtask_name: str | None = None
) -> dict | None:
    """From an archive-list response, return the row for the running print.

    Newest `status=="printing"` row, preferring a name match to the live
    `subtask_name` when one is available. Returns None if nothing plausible.
    """
    printing = [a for a in rows if a.get("status") == "printing"]
    if not printing:
        return None
    if subtask_name:
        norm = _norm_name(subtask_name)
        named = [a for a in printing if _norm_name(a.get("print_name")) == norm]
        if named:  # only narrow if the name actually matched something
            printing = named
    # Newest started_at wins (the current print's archive is always the newest).
    return max(printing, key=lambda a: a.get("started_at") or "")
