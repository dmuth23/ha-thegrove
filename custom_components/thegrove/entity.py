"""Base entities for thegrove — serial-based entity_ids + friendly names.

THE KEYSTONE (verified against HA 2026.5.1 entity_registry):
We want entity_id `sensor.p2s_<serial>_ams_left_tray_1` (a stable serial-based
handle) WITH friendly name "Sharkie AMS Left Tray 1" (dashboard). Default
`has_entity_name=True` would slug the *device* name into the object_id →
`sensor.sharkie_*`. Instead each entity sets `self.entity_id` itself: HA splits
that into `internal_integration_suggested_object_id`, which `_async_derive_
object_ids` routes into the registry's `suggested_object_id` slot — and per the
registry precedence (`name > suggested_object_id > object_id_base`),
`suggested_object_id` is **NOT** prefixed with the device name. So the serial
object_id is honored verbatim while `has_entity_name`+`_attr_name` still produce
the human friendly name. (A *valid* entity_id triggers no deprecation warning;
only invalid ones do — `entity_platform.py`.)

`suggested_object_id` is honored only at FIRST registration; on later reloads
the registry returns the stored entity_id. So renaming the scheme after entities
exist requires clearing the registry entry (remove+re-add the integration).
"""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Any

from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import slugify

from .bambuddy.rest_client import BambuddyApiError, BambuddyRestClient
from .brain.vp import VirtualPrinterModel
from .const import DOMAIN
from .coordinator import PrinterCoordinator, PrinterIdentity, VpCoordinator


def object_id_prefix(identity: PrinterIdentity) -> str:
    """The serial-based object_id stem: `{model}_{serial}` (e.g. `p2s_<serial>`).

    Slugified so it is always a valid entity_id object_id and lowercase.
    """
    return slugify(f"{identity.model}_{identity.serial}")


def ams_side(ams_id: int) -> str:
    """Map an AMS unit id to its physical side label (the §4.3 artifact).

    Observed on the live dual-AMS P2S (2026-06-24): id 0 = left, id 1 = right.
    Higher ids / AMS-HT fall back to a stable `ams_<id>` form.
    """
    return {0: "left", 1: "right"}.get(ams_id, f"ams_{ams_id}")


class TheGroveEntity(CoordinatorEntity[PrinterCoordinator]):
    """Base for printer-level entities (device = the printer, keyed on serial)."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: PrinterCoordinator, *, key: str, platform_domain: str
    ) -> None:
        super().__init__(coordinator)
        self._key = key
        identity = coordinator.identity
        self._identity = identity
        self._attr_unique_id = f"{identity.serial}_{key}"
        # Force the serial-based object_id (see module docstring).
        self.entity_id = f"{platform_domain}.{object_id_prefix(identity)}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, identity.serial)},
            name=identity.name,
            manufacturer="Bambu Lab (via Bambuddy)",
            model=identity.model,
            serial_number=identity.serial,
        )

    @property
    def available(self) -> bool:
        """Available while we have data AND the WS socket is connected."""
        return self.coordinator.last_update_success and self.coordinator.feed_alive


class TheGroveCommandMixin:
    """Write-command behavior shared by printer-device AND AMS-device entities.

    Carries the REST client accessor, the printer-id accessor, a single fail-loud
    `_run` helper (any failure → HomeAssistantError, a clean toast with BB's own
    message, never a half-write), and the optimistic-write machinery.

    NO `__init__`: the concrete combiner (`TheGroveCommandEntity` /
    `TheGroveAmsCommandEntity`) owns construction and sets `self._rest` right after
    `super().__init__`. This mixin only adds methods + state.

    MRO CONTRACT (load-bearing): the mixin MUST be listed FIRST in the bases
    (before `TheGroveEntity`/`TheGroveAmsEntity`), so its `_handle_coordinator_
    update` wins in the linearization and its `super()` then chains into
    `CoordinatorEntity._handle_coordinator_update`. Reverse the base order and
    `CoordinatorEntity`'s version would win — the optimism-clear would never run.
    """

    coordinator: PrinterCoordinator  # provided by CoordinatorEntity
    _rest: BambuddyRestClient  # annotation only — each combiner sets it in __init__
    _optimistic: Any = None

    @property
    def _printer_id(self) -> int:
        """The Bambuddy DB id this command targets (the REST path key)."""
        return self.coordinator.identity.printer_id

    async def _run(self, command: Awaitable, *, action: str) -> None:
        """Await a REST command, translating any failure into HomeAssistantError.

        Fail-loud, never half-write: the wrapper raises on a non-2xx, so a failed
        command surfaces as an error toast and HA does NOT optimistically flip the
        entity state (the caller only updates state after this returns cleanly).
        """
        try:
            await command
        except BambuddyApiError as err:
            raise HomeAssistantError(f"{action} failed — {err.detail}") from err
        except Exception as err:  # noqa: BLE001 - network/timeout -> user-facing
            raise HomeAssistantError(f"{action} failed — {err}") from err

    # ---- optimistic write state (shared by light + select + AMS controls) ----
    # A write returns before the next ~1.5s WS frame, so we hold an optimistic
    # value for instant UI feedback. It clears ONLY when a frame CONFIRMS it —
    # never on the hub's dataless ~2s watchdog tick (which would flicker the
    # value back to the stale read). Subclasses that want optimism call
    # `_apply_optimistic()` and implement `_confirming_value()`.

    def _confirming_value(self) -> Any:
        """The current value from coordinator.data that, when equal to the
        optimistic value, means the write landed. Default None = no optimism."""
        return None

    def _apply_optimistic(self, value: Any) -> None:
        """Show `value` immediately, pending frame confirmation."""
        self._optimistic = value
        self.async_write_ha_state()

    @callback
    def _handle_coordinator_update(self) -> None:
        if self._optimistic is not None and self._confirming_value() == self._optimistic:
            self._optimistic = None
        super()._handle_coordinator_update()


class TheGroveCommandEntity(TheGroveCommandMixin, TheGroveEntity):
    """Printer-device command base (light, button, select). Thin combiner: the
    behavior lives in TheGroveCommandMixin; this binds it to the printer-device
    identity (TheGroveEntity) and stores the REST client."""

    def __init__(
        self,
        coordinator: PrinterCoordinator,
        rest: BambuddyRestClient,
        *,
        key: str,
        platform_domain: str,
    ) -> None:
        super().__init__(coordinator, key=key, platform_domain=platform_domain)
        self._rest = rest


class TheGroveAmsEntity(TheGroveEntity):
    """Base for AMS-unit / tray entities — a CHILD device per AMS, linked
    `via_device` to the printer. entity_id stays serial-rooted
    (`sensor.p2s_<serial>_ams_left_*`); friendly name reads "Sharkie AMS Left …".
    """

    def __init__(
        self,
        coordinator: PrinterCoordinator,
        *,
        ams_id: int,
        key: str,
        platform_domain: str,
    ) -> None:
        # key already includes the `ams_left_`/`ams_right_` segment so the
        # serial object_id + unique_id come out right via the parent.
        super().__init__(coordinator, key=key, platform_domain=platform_domain)
        self._ams_id = ams_id
        identity = coordinator.identity
        side = ams_side(ams_id).replace("_", " ").title()  # "Left"/"Right"/"Ams 2"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{identity.serial}_ams_{ams_id}")},
            name=f"{identity.name} AMS {side}",
            manufacturer="Bambu Lab (via Bambuddy)",
            model=f"{identity.model} AMS",
            via_device=(DOMAIN, identity.serial),
        )


class TheGroveAmsCommandEntity(TheGroveCommandMixin, TheGroveAmsEntity):
    """AMS-child-device command base (per-AMS / per-tray controls: drying, tray
    reset/refresh/load). Reuses TheGroveAmsEntity's child-device `device_info`
    (via_device → printer) and the same command + optimism contract as the
    printer-level base. Mixin FIRST in the bases — see TheGroveCommandMixin."""

    def __init__(
        self,
        coordinator: PrinterCoordinator,
        rest: BambuddyRestClient,
        *,
        ams_id: int,
        key: str,
        platform_domain: str,
    ) -> None:
        super().__init__(
            coordinator, ams_id=ams_id, key=key, platform_domain=platform_domain
        )
        self._rest = rest


# --- Virtual Printers (P2.5) — a SEPARATE device class on its OWN coordinator ---


class TheGroveVpEntity(CoordinatorEntity[VpCoordinator]):
    """Base for Virtual-Printer entities.

    A VP is its own HA device keyed `(DOMAIN, "vp-{id}")` — the `vp-` prefix
    dodges the VP/real-printer int-id overlap (separate BB tables, overlapping
    PKs). Entity_id is `{domain}.vp_{id}_{key}` (no serial scheme: a VP's serial
    is a secret we never surface). `via_device` links to the TARGET printer's
    serial-keyed device when that printer is present, so a VP nests under the
    machine it routes to; it degrades gracefully (no link) when the target isn't
    loaded. Reads its model from `coordinator.data[vp_id]`.
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: VpCoordinator,
        *,
        vp_id: int,
        key: str,
        platform_domain: str,
        target_serial: str | None = None,
    ) -> None:
        super().__init__(coordinator)
        self._vp_id = vp_id
        self._key = key
        self._attr_unique_id = f"vp-{vp_id}_{key}"
        self.entity_id = f"{platform_domain}.vp_{vp_id}_{key}"
        vp = (coordinator.data or {}).get(vp_id)
        info = DeviceInfo(
            identifiers={(DOMAIN, f"vp-{vp_id}")},
            name=vp.name if vp else f"VP {vp_id}",
            manufacturer="Bambuddy",
            model="Virtual Printer",
        )
        if target_serial:
            info["via_device"] = (DOMAIN, target_serial)
        self._attr_device_info = info

    @property
    def vp(self) -> VirtualPrinterModel | None:
        """This VP's current model, or None if it's gone from the poll (deleted)."""
        return (self.coordinator.data or {}).get(self._vp_id)

    @property
    def available(self) -> bool:
        """Available while the poll succeeds AND this VP still exists."""
        return self.coordinator.last_update_success and self.vp is not None


class TheGroveVpCommandEntity(TheGroveCommandMixin, TheGroveVpEntity):
    """VP command base (switch/select). Reuses TheGroveCommandMixin's `_run` +
    optimism, but targets `_vp_id` (the mixin's `_printer_id` is printer-only and
    unused here). Mixin FIRST in the bases — same MRO contract as the printer
    command base. After a PUT, the caller triggers a coordinator refresh (no WS
    to clear optimism)."""

    def __init__(
        self,
        coordinator: VpCoordinator,
        rest: BambuddyRestClient,
        *,
        vp_id: int,
        key: str,
        platform_domain: str,
        target_serial: str | None = None,
    ) -> None:
        super().__init__(
            coordinator,
            vp_id=vp_id,
            key=key,
            platform_domain=platform_domain,
            target_serial=target_serial,
        )
        self._rest = rest

    async def _write_vp(self, optimistic: Any, *, action: str, **changes: Any) -> None:
        """PUT the changed VP field(s), then — only on success — show `optimistic`
        and trigger an immediate poll to confirm it. The write goes FIRST so a
        failed PUT raises before any optimistic value is shown (no false UI); the
        explicit refresh clears the optimistic value in ~1s (there's no WS frame
        to do it, so without this it would linger to the 30s backstop)."""
        await self._run(
            self._rest.update_virtual_printer(self._vp_id, **changes), action=action
        )
        self._apply_optimistic(optimistic)
        await self.coordinator.async_request_refresh()
