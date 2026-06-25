"""Image platform for thegrove — the print cover still.

Per OD-G the LIVE camera is BB's long-lived-token `camera:` path (documented,
NOT built inside the integration). This entity is only the on-demand **cover
still** from `cover_url` (REST PNG), re-fetched when the plate changes.
"""

from __future__ import annotations

from homeassistant.components.image import ImageEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from . import TheGroveConfigEntry
from .bambuddy.rest_client import BambuddyRestClient
from .coordinator import PrinterCoordinator
from .entity import TheGroveEntity
from .hub import SIGNAL_NEW_PRINTER

IMAGE = "image"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TheGroveConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """One cover image per printer, with dynamic-add."""
    hub = entry.runtime_data

    @callback
    def _entities_for(coord: PrinterCoordinator) -> list[ImageEntity]:
        return [PrinterCoverImage(coord, hub.rest)]

    initial: list[ImageEntity] = []
    for coord in hub.coordinators.values():
        initial += _entities_for(coord)
    async_add_entities(initial)

    @callback
    def _add_new_printer(printer_id: int) -> None:
        coord = hub.coordinators.get(printer_id)
        if coord is not None:
            async_add_entities(_entities_for(coord))

    entry.async_on_unload(
        async_dispatcher_connect(
            hass, SIGNAL_NEW_PRINTER.format(entry.entry_id), _add_new_printer
        )
    )


class PrinterCoverImage(TheGroveEntity, ImageEntity):
    """The current print's cover still (REST, on-demand)."""

    _attr_name = "Cover"
    _attr_content_type = "image/png"

    def __init__(
        self, coordinator: PrinterCoordinator, rest: BambuddyRestClient
    ) -> None:
        TheGroveEntity.__init__(self, coordinator, key="cover", platform_domain=IMAGE)
        ImageEntity.__init__(self, coordinator.hass)
        self._rest = rest
        self._cover_path: str | None = None
        self._last_plate: int | None = None
        self._refresh_marker()

    @callback
    def _handle_coordinator_update(self) -> None:
        self._refresh_marker()
        super()._handle_coordinator_update()

    def _refresh_marker(self) -> None:
        """Track the cover path; stamp image_last_updated on a new plate so HA
        re-fetches (cover content changes per plate, the URL does not)."""
        model = self.coordinator.data
        if model is None:
            return
        self._cover_path = model.cover_url
        if model.current_plate_id != self._last_plate:
            self._last_plate = model.current_plate_id
            self._attr_image_last_updated = dt_util.utcnow()

    async def async_image(self) -> bytes | None:
        if not self._cover_path:
            return None
        return await self._rest.get_bytes(self._cover_path)
