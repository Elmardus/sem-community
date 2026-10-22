"""(#1084) Logbook lines for SEM's decision log.

Each ``solar_energy_management_decision`` event becomes one line:
``<device>: <old> → <new> — <short text>``. The text is the decision's own
reason, which SEM writes in English everywhere else too (the observer
surface, the strategy attribute), so the line is not translated.
"""
from __future__ import annotations

from typing import Any, Callable, Dict

from homeassistant.core import Event, HomeAssistant, callback

from .consts.core import DOMAIN
from .coordinator.decision_log import EVENT_DECISION

# The logbook's own keys. Spelled out rather than imported so this platform
# does not pull the logbook integration in as a dependency.
_NAME = "name"
_MESSAGE = "message"


def describe_decision(data: Dict[str, Any]) -> Dict[str, str]:
    """One decision record → the logbook's name + message."""
    name = str(data.get("name") or data.get("device_id") or "SEM")
    old = data.get("old") or "none"
    new = data.get("new") or "?"
    message = f"{old} → {new}"
    text = str(data.get("text") or "").strip()
    if text:
        message += f" — {text}"
    if data.get("observer"):
        message += " (observer: not sent)"
    return {_NAME: name, _MESSAGE: message}


@callback
def async_describe_events(
    hass: HomeAssistant,
    async_describe_event: Callable[[str, str, Callable[[Event], Dict[str, str]]], None],
) -> None:
    """Register the decision event with the logbook."""

    @callback
    def _describe(event: Event) -> Dict[str, str]:
        return describe_decision(dict(event.data or {}))

    async_describe_event(DOMAIN, EVENT_DECISION, _describe)
