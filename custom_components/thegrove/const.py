"""Constants for the thegrove integration."""

DOMAIN = "thegrove"

# Config-entry / options keys
CONF_HOST = "host"
CONF_API_TOKEN = "api_token"
CONF_BACKSTOP_INTERVAL = "backstop_interval"
CONF_WATCHDOG_TIMEOUT = "watchdog_timeout"

# Default Bambuddy add-on host (prod, internal Docker hostname). Overridable in
# the config flow — e.g. the LAN IP for the sandbox: http://192.168.250.20:8000.
DEFAULT_HOST = "http://33558673-bambuddy-daily:8000"

# The 30 s REST /status backstop runs on its OWN async_track_time_interval task
# (NOT the coordinator's native update_interval, which WS frames would starve).
DEFAULT_BACKSTOP_INTERVAL = 30  # seconds

# Per-printer staleness window: no WS frame for this long -> that printer's
# entities go `unavailable`. Tuned generously (NOT ~1.5 s x few) so a brief WS
# flap doesn't trigger a reconcile burst downstream — see ARCHITECTURE-PLAN §4.1.
DEFAULT_WATCHDOG_TIMEOUT = 20  # seconds

# Bambuddy WS broadcast endpoint (NOT in the OpenAPI spec — internal broadcast).
WS_PATH = "/api/v1/ws"
