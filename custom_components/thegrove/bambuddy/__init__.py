"""Talking-to-Bambuddy — the only code that knows Bambuddy exists.

Session-injected (an aiohttp ClientSession is passed in), so these clients are
HA-free and testable standalone. In HA the session is
`homeassistant.helpers.aiohttp_client.async_get_clientsession(hass)`.
"""
