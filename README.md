# The Grove (Bambuddy)

A Home Assistant custom integration (`domain: thegrove`) for Bambu Lab printers,
driven entirely through a **[Bambuddy](https://bambuddy.org)** backend. It is a
thin **mapper** over Bambuddy's already-decoded printer status: one Bambuddy
connection, one Home Assistant device per printer, with stable serial-based
entity IDs.

> Install Bambuddy once; The Grove turns it into native Home Assistant devices,
> sensors, and controls — no per-printer MQTT setup, no cloud.

## Features

- **Multi-printer hub** — one Bambuddy connection, demultiplexed into one HA
  device per printer.
- **Live status** via WebSocket push (~1.5 s), with a 30 s REST poll as backstop.
- **Full read set** — print state/progress/stage, temperatures, fans, per-AMS
  units and per-tray filament, HMS errors, filament-runout and fault detection,
  cover image, and more.
- **Controls** — chamber light, pause/resume/stop, clear plate, AMS load/unload
  and per-slot configuration, AI-detector toggles and sensitivities, AMS dryer,
  print speed, and services for slot config / object skipping / calibration /
  drying.
- **Virtual Printers** — Bambuddy's inbound job-routing surfaced as HA entities.

## Requirements

- A running **Bambuddy** instance — the `bambuddy-daily` Home Assistant add-on is
  the common case, but any reachable Bambuddy host works.
- Home Assistant.

## Supported hardware

Single-extruder Bambu Lab printers (P-, X-, and A-series) with standard AMS.
Dual-extruder (H2D) is not yet supported.

## Installation (HACS)

1. In **HACS → ⋮ → Custom repositories**, add this repository's URL as an
   **Integration**.
2. Find **The Grove (Bambuddy)** in HACS and click **Download**.
3. **Restart** Home Assistant.
4. Go to **Settings → Devices & Services → + Add Integration**, search for
   **The Grove**, and enter your Bambuddy host.

## Configuration

| Field | Notes |
|---|---|
| **Host** | Your Bambuddy base URL. Defaults to the `bambuddy-daily` add-on host (`http://33558673-bambuddy-daily:8000`). |
| **API token** | Optional — only needed if your Bambuddy requires authentication. |

Each printer Bambuddy knows about becomes its own Home Assistant device, with
entity IDs of the form `sensor.{model}_{serial}_*`.
