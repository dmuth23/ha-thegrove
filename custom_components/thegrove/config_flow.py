"""Config flow for thegrove — connect to the Bambuddy hub."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .bambuddy.rest_client import BambuddyRestClient
from .const import CONF_API_TOKEN, CONF_HOST, DEFAULT_HOST, DOMAIN


class TheGroveConfigFlow(ConfigFlow, domain=DOMAIN):
    """One config entry = the Bambuddy hub (all its printers)."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input[CONF_HOST].rstrip("/")
            token = user_input.get(CONF_API_TOKEN) or None

            session = async_get_clientsession(self.hass)
            rest = BambuddyRestClient(session, host, api_key=token)
            try:
                printers = await rest.list_printers()
            except Exception:  # noqa: BLE001 - any failure = can't connect
                errors["base"] = "cannot_connect"
            else:
                if not printers:
                    errors["base"] = "no_printers"
                else:
                    await self.async_set_unique_id(host)
                    self._abort_if_unique_id_configured()
                    names = ", ".join(p.get("name", "?") for p in printers)
                    return self.async_create_entry(
                        title=f"The Grove ({names})",
                        data={CONF_HOST: host, CONF_API_TOKEN: token},
                    )

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_HOST,
                    default=(user_input or {}).get(CONF_HOST, DEFAULT_HOST),
                ): str,
                vol.Optional(CONF_API_TOKEN): str,
            }
        )
        return self.async_show_form(
            step_id="user", data_schema=schema, errors=errors
        )
