# The Grove (Bambuddy)

[![Open your Home Assistant instance and open this repository inside HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=dmuth23&repository=ha-thegrove&category=integration)
[![GitHub release](https://img.shields.io/github/v/release/dmuth23/ha-thegrove)](https://github.com/dmuth23/ha-thegrove/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

**Bambu Lab printers in Home Assistant, driven entirely through [Bambuddy](https://github.com/maziggy/bambuddy) — no per-printer MQTT setup, no cloud.**

Install Bambuddy once, point The Grove at it, and every printer Bambuddy knows about becomes
a native Home Assistant device: live sensors, controls, AMS trays, dryer switches, AI-detector
toggles, and Bambuddy's Virtual Printers — a hundred-plus entities on a typical setup. The
Grove is a thin mapper over Bambuddy's already-decoded printer status, not another MQTT
decoder.

> **Status: Beta.** Built and live-verified against a real printer through a full print
> lifecycle (runout, finish, plate-clear, cancels) plus supervised live-write tests of the
> controls. It works, but it's young — expect rough edges and please
> [open issues](https://github.com/dmuth23/ha-thegrove/issues).

## Features

- **Multi-printer hub** — one Bambuddy connection, demultiplexed into one Home Assistant
  device per printer.
- **Live status** via WebSocket push (~1.5 s), with a periodic REST poll as backstop.
- **Full read set** — print state / progress / stage, task name, temperatures, fans, per-AMS
  units and per-tray filament, active material, external spool, HMS errors, filament-runout
  and print-fault detection, awaiting-plate-clear, print weight and start time, and the model
  cover image.
- **Controls** — chamber light, pause / resume / stop, clear plate, clear HMS, homing, print
  speed, AMS load / unload / refresh per tray, AMS dryer switches, and AI-detector toggles
  with per-detector sensitivity selects.
- **Services** — `thegrove.configure_slot`, `thegrove.skip_objects`,
  `thegrove.start_calibration`, and `thegrove.start_drying`, all device-targeted, so
  everything is scriptable from your own automations.
- **Virtual Printers** — Bambuddy's inbound job-routing surfaced as HA entities: mode select
  (archive / review / queue / proxy), enable and auto-dispatch switches, queue color-match,
  running state, and pending-file count.
- **Stable serial-based entity IDs** — `sensor.{model}_{serial}_*`, so entity IDs survive
  renames while friendly display names are preserved as labels.

## Screenshots

*(coming soon)*

## Requirements

- A running **[Bambuddy](https://github.com/maziggy/bambuddy)** instance reachable from your
  Home Assistant host. The `bambuddy-daily` Home Assistant add-on is the common case, but any
  reachable Bambuddy host works.
- Home Assistant **2025.1.0+**.
- A single-extruder Bambu Lab printer (P-, X-, or A-series) with standard AMS, already added
  to Bambuddy. Dual-extruder (H2D) is **not yet supported**.

## Install

### 1. Get the integration (HACS)

Click the badge at the top of this page, **or** manually: HACS → ⋮ → **Custom repositories** →
add `https://github.com/dmuth23/ha-thegrove`, category **Integration**. (The Grove isn't in
the HACS default store yet, so the custom-repository step is required for now.)

Download **The Grove (Bambuddy)**, then **restart Home Assistant**.

### 2. Connect it to Bambuddy

**Settings → Devices & Services → Add Integration → The Grove** → enter your Bambuddy host:

| Field | Notes |
|---|---|
| **Bambuddy host (URL)** | Base URL of your Bambuddy instance. Defaults to the `bambuddy-daily` add-on's internal host (`http://33558673-bambuddy-daily:8000`), so add-on users can usually just accept the default. |
| **API token (optional)** | Only needed if your Bambuddy has authentication enabled. |

The Grove verifies the connection and that Bambuddy reports at least one printer, then
creates a device for each printer (and each Virtual Printer). Done.

## How it works

Bambuddy is the single source of truth. The Grove holds one WebSocket to Bambuddy for pushed
printer status (~1.5 s updates), demultiplexes it by printer, and maps Bambuddy's
already-decoded fields straight onto Home Assistant entities — a slower REST poll runs behind
the WebSocket as a backstop. All writes (controls and services) go through Bambuddy's REST
API, which handles the actual printer communication. Zero Bambuddy code modifications — stock
API only, and no direct MQTT connection to the printers.

## License

MIT — see [LICENSE](LICENSE).
