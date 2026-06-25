"""Virtual Printer model + mapper — pure, HA-free, Bambuddy-free.

A Bambuddy "Virtual Printer" (VP) is NOT a machine: it's a fake printer BB
advertises so a slicer can "print to Bambuddy", and BB then routes the job by
the VP's `mode` (archive/review/queue/proxy). It is a REST-only surface — there
is NO `virtual_printer_status` WS frame (verified: `routes/websocket.py` only
broadcasts `printer_status`/`print_*`/`archive_*`/…) — so unlike PrinterModel
this is fed by a plain polling coordinator, not the push path.

LANDMINE (the reason VPs are their own device class): `VirtualPrinter` and
`Printer` are SEPARATE DB tables with SEPARATE PK sequences, so a VP `id` and a
real-printer `id` overlap (both have an `id=1`). VPs therefore stay OUT of the
hub's `coordinators: dict[int, …]` and key their HA device on `vp-{id}`.

SECRETS: the live `GET /virtual-printers` row also carries `serial` and
`access_code_set`/`bind_ip`. Those are deliberately NOT lifted into this model —
nothing secret should reach an entity attribute or a committed artifact. We
surface only the routing config + light status.

`mode` canonical values pinned from BB source (`models/virtual_printer.py`
`VP_MODE_VALUES`): archive · review · queue · proxy.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Canonical VP modes (BB `VP_MODE_VALUES`). archive=just file the 3MF,
#: review=hold for a look, queue=enqueue+dispatch to a real printer,
#: proxy=pass transparently to one specific printer.
VP_MODES = ("archive", "review", "queue", "proxy")


@dataclass(frozen=True, slots=True)
class VirtualPrinterModel:
    """Immutable snapshot of one VP's routing config + light status."""

    vp_id: int
    name: str
    enabled: bool
    mode: str
    auto_dispatch: bool
    queue_force_color_match: bool
    running: bool  # status.running — the bind/advertise server is up
    pending_files: int  # status.pending_files — queue depth
    target_printer_id: int | None = None
    model_name: str | None = None  # what the VP advertises as (e.g. "P2S")


def _as_bool(value: object) -> bool:
    return bool(value)


def map_virtual_printer(data: dict) -> VirtualPrinterModel:
    """Map one element of `GET /api/v1/virtual-printers` -> VirtualPrinterModel.

    Status lives in a nested `status: {running, pending_files}` object; a missing
    status (e.g. a stopped VP) reads as not-running / 0 pending.
    """
    status = data.get("status") or {}
    target = data.get("target_printer_id")
    return VirtualPrinterModel(
        vp_id=int(data["id"]),
        name=str(data.get("name") or f"VP {data['id']}"),
        enabled=_as_bool(data.get("enabled")),
        mode=str(data.get("mode") or "archive"),
        auto_dispatch=_as_bool(data.get("auto_dispatch")),
        queue_force_color_match=_as_bool(data.get("queue_force_color_match")),
        running=_as_bool(status.get("running")),
        pending_files=int(status.get("pending_files") or 0),
        target_printer_id=int(target) if target is not None else None,
        model_name=(str(data["model_name"]) if data.get("model_name") else None),
    )


def map_virtual_printers(payload: dict) -> dict[int, VirtualPrinterModel]:
    """Map the full `{printers: [...], models: {...}}` list payload to
    `{vp_id: VirtualPrinterModel}`. A malformed row is skipped, not fatal."""
    out: dict[int, VirtualPrinterModel] = {}
    for row in (payload or {}).get("printers") or []:
        if isinstance(row, dict) and row.get("id") is not None:
            vp = map_virtual_printer(row)
            out[vp.vp_id] = vp
    return out
