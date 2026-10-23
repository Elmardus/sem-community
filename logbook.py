"""(#1084) Logbook lines for SEM's decision log.

Each ``solar_energy_management_decision`` event becomes one line:
``<device>: <old> → <new> — <reason>``. (#1174) The reason is the plain
sentence for the record's code, in Home Assistant's language, from the
same catalogue the Why? card reads (``plain_reasons``). A code without a
sentence falls back to the decision's own internal text.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Mapping, Optional

from homeassistant.core import Event, HomeAssistant, callback

from .consts.core import DOMAIN
from .coordinator.decision_log import EVENT_DECISION
from .plain_reasons import DATA_KEY, kind_word, plain_sentence

# The logbook's own keys. Spelled out rather than imported so this platform
# does not pull the logbook integration in as a dependency.
_NAME = "name"
_MESSAGE = "message"


def describe_decision(data: Dict[str, Any],
                      catalogue: Optional[Mapping[str, Mapping[str, str]]] = None,
                      lang: str = "en") -> Dict[str, str]:
    """One decision record → the logbook's name + message."""
    name = str(data.get("name") or data.get("device_id") or "SEM")
    if name == data.get("device_id"):
        # (#1174) known only by its id (the grid export): the kind's word
        name = kind_word(catalogue or {}, str(data.get("device_kind") or ""),
                         lang) or name
    old = data.get("old") or "none"
    new = data.get("new") or "?"
    message = f"{old} → {new}"
    params = data.get("params")
    text = (plain_sentence(catalogue or {}, str(data.get("reason_code") or ""),
                           params if isinstance(params, Mapping) else None, lang)
            or str(data.get("text") or "").strip())
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
        return describe_decision(
            dict(event.data or {}), hass.data.get(DATA_KEY),
            getattr(hass.config, "language", None) or "en")

    async_describe_event(DOMAIN, EVENT_DECISION, _describe)
