"""(#1084) The Why? card's read: the decision log, newest first.

Read-only, any user may ask: the rows are the same ones the logbook shows.
"""
from __future__ import annotations

from typing import Any, Optional

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN
from .coordinator.decision_log import DECISION_LOG_SIZE, DecisionLog

WS_DECISION_LOG = f"{DOMAIN}/decision_log"


def _log_for(hass: HomeAssistant, entry_id: Optional[str]) -> Optional[DecisionLog]:
    """The decision log of one SEM entry, or None when there is none."""
    entries = hass.config_entries.async_entries(DOMAIN)
    if entry_id:
        entries = [e for e in entries if e.entry_id == entry_id]
    entry = entries[0] if entries else None
    coordinator = getattr(entry, "runtime_data", None) if entry else None
    log = getattr(coordinator, "decision_log", None)
    return log if isinstance(log, DecisionLog) else None


@websocket_api.websocket_command({
    vol.Required("type"): WS_DECISION_LOG,
    vol.Optional("entry_id"): str,
})
@callback
def ws_decision_log(hass: HomeAssistant, connection, msg: dict[str, Any]) -> None:
    """Every kept record, newest first."""
    log = _log_for(hass, msg.get("entry_id"))
    if log is None:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND,
                              "no SEM entry")
        return
    try:
        rows = list(reversed(log.records()))
    except Exception:  # noqa: BLE001 — a half-built log answers "nothing"
        rows = []
    connection.send_result(msg["id"], {"records": rows,
                                       "size": DECISION_LOG_SIZE})


@callback
def async_register_websocket(hass: HomeAssistant) -> None:
    """Register the command. Idempotent: a reload overwrites the same slot."""
    websocket_api.async_register_command(hass, ws_decision_log)
