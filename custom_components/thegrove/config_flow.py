"""Config flow for thegrove — connect to the Bambuddy hub."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .bambuddy.rest_client import BambuddyRestClient
from .const import CONF_API_TOKEN, CONF_HOST, DEFAULT_HOST, DOMAIN


def _schema(host_default: str, token_default: str | None = None) -> vol.Schema:
    token_field = (
        vol.Optional(CONF_API_TOKEN, default=token_default)
        if token_default
        else vol.Optional(CONF_API_TOKEN)
    )
    return vol.Schema(
        {
            vol.Required(CONF_HOST, default=host_default): str,
            token_field: str,
        }
    )


class TheGroveConfigFlow(ConfigFlow, domain=DOMAIN):
    """One config entry = the Bambuddy hub (all its printers)."""

    VERSION = 1

    async def _async_probe(
        self, host: str, token: str | None
    ) -> tuple[str | None, list[dict[str, Any]]]:
        """Connect and list printers. Returns (error_key, printers)."""
        session = async_get_clientsession(self.hass)
        rest = BambuddyRestClient(session, host, api_key=token)
        try:
            printers = await rest.list_printers()
        except Exception:  # noqa: BLE001 - any failure = can't connect
            return "cannot_connect", []
        if not printers:
            return "no_printers", []
        return None, printers

    @staticmethod
    def _title(printers: list[dict[str, Any]]) -> str:
        return "The Grove (" + ", ".join(p.get("name", "?") for p in printers) + ")"

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input[CONF_HOST].rstrip("/")
            token = user_input.get(CONF_API_TOKEN) or None
            error, printers = await self._async_probe(host, token)
            if error:
                errors["base"] = error
            else:
                await self.async_set_unique_id(host)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=self._title(printers),
                    data={CONF_HOST: host, CONF_API_TOKEN: token},
                )

        return self.async_show_form(
            step_id="user",
            data_schema=_schema((user_input or {}).get(CONF_HOST, DEFAULT_HOST)),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the Bambuddy host/token in place (e.g. Bambuddy moved hosts).

        Entities are keyed on printer serial, not on the host, so they survive.
        """
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input[CONF_HOST].rstrip("/")
            token = user_input.get(CONF_API_TOKEN) or None
            error, printers = await self._async_probe(host, token)
            if error:
                errors["base"] = error
            elif any(
                other.entry_id != entry.entry_id and other.unique_id == host
                for other in self._async_current_entries()
            ):
                return self.async_abort(reason="already_configured")
            else:
                return self.async_update_reload_and_abort(
                    entry,
                    unique_id=host,
                    title=self._title(printers),
                    data={CONF_HOST: host, CONF_API_TOKEN: token},
                )

        current = user_input or entry.data
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_schema(
                current.get(CONF_HOST, DEFAULT_HOST), current.get(CONF_API_TOKEN)
            ),
            errors=errors,
        )
