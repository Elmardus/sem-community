"""Which way a charger's start/stop switch reads (#1042).

A switch says in its name what "on" means. Most charger switches are named
for the charge — NRGkick's "Charging enabled", OCPP's "Charge control",
Wallbox's "Pause/resume" — and are on while the box may charge. V2C's
"Pause session" (core key ``paused``) is named for the pause: on is the
stop, and ``turn_on`` pauses the box. SEM drove it like the others, so every
start paused the charge and every stop resumed it.

Every write SEM makes to a charger's start/stop switch, and every read of
one, goes through this module. ``tests/test_1042_pause_switch.py`` holds the
package to that rule.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from ..consts.devices import names_a_charge_pause, names_another_pause

_LOGGER = logging.getLogger(__name__)


def _registry(hass: Any):
    from homeassistant.helpers import entity_registry as er
    return er.async_get(hass)


def integration_name(entry: Any) -> Optional[str]:
    """The name the INTEGRATION gave the entity, without the device name.

    The translation key first: it is the same in every language (a German
    V2C's id ends in ``vorgang_pausieren``, its key is ``paused``), and an
    owner who renamed the id renamed a label, not what on does. Then the
    entity's own name, when the integration says it is one
    (``has_entity_name``). Nothing else: a template switch or a helper the
    owner made is theirs, built for "on = start", and is left as it is."""
    key = getattr(entry, "translation_key", None)
    if isinstance(key, str) and key:
        return key
    if getattr(entry, "has_entity_name", None) is True:
        name = getattr(entry, "original_name", None)
        if isinstance(name, str) and name:
            return name
    return None


def on_means_paused(hass: Any, entity_id: Any) -> bool:
    """True when this switch is a pause of the charge: on is the stop."""
    eid = str(entity_id or "")
    if not eid.startswith(("switch.", "input_boolean.")):
        return False
    try:
        entry = _registry(hass).async_get(eid)
    except Exception as e:  # noqa: BLE001 — no registry: no name to read
        _LOGGER.debug("on_means_paused(%s): no registry entry: %s", eid, e)
        return False
    if entry is None:
        return False
    return names_a_charge_pause(integration_name(entry))


def switch_service(hass: Any, entity_id: Any, *, run: bool) -> str:
    """The service that lets the charge run (``run=True``) or stops it."""
    on = run != on_means_paused(hass, entity_id)
    return "turn_on" if on else "turn_off"


def reads_running(hass: Any, entity_id: Any, state: Any) -> Optional[bool]:
    """Does this switch state let the charge run?

    ``None`` for anything but ``on``/``off``: ``unavailable``, ``unknown``
    or no state at all is not an answer either way (#945)."""
    if state not in ("on", "off"):
        return None
    return (state == "on") != on_means_paused(hass, entity_id)


def charge_pause_twin(hass: Any, entity_id: Any) -> Optional[str]:
    """A saved start/stop switch that pauses something ELSE → the one switch
    on the same device that pauses the charge; ``None`` otherwise.

    The V2C rule before #1042 took the last switch with "pause" in its
    name, and Home Assistant registers V2C's "Pause dynamic control
    modulation" (key ``pause_dynamic``) after "Pause session" (``paused``).
    Detection runs only while no charger is configured, so a V2C set up
    before the fix keeps the modulation pause, which never stopped the
    charge. Read on keys only, on one device, and only when exactly one
    switch there is a pause of the charge."""
    eid = str(entity_id or "")
    if not eid.startswith("switch."):
        return None
    try:
        reg = _registry(hass)
        entry = reg.async_get(eid)
        if entry is None:
            return None
        key = getattr(entry, "translation_key", None)
        device = getattr(entry, "device_id", None)
        if not (isinstance(key, str) and names_another_pause(key) and device):
            return None
        twins = [
            str(e.entity_id) for e in reg.entities.values()
            if getattr(e, "device_id", None) == device
            and str(e.entity_id).startswith("switch.")
            and str(e.entity_id) != eid
            and not getattr(e, "disabled_by", None)
            and isinstance(getattr(e, "translation_key", None), str)
            and names_a_charge_pause(e.translation_key)]
    except Exception as e:  # noqa: BLE001 — a heal never costs a setup
        _LOGGER.debug("charge_pause_twin(%s) failed: %s", eid, e)
        return None
    if len(twins) == 1:
        return twins[0]
    # Configured, so the #627 Repair stays quiet: say it here.
    _LOGGER.warning(
        "The start/stop switch %s pauses something other than the charge, "
        "and its device has %d switches that pause the charge, so SEM "
        "cannot pick one. SEM cannot stop this charger with %s. Name the "
        "switch that pauses the charge under Configuration → EV chargers "
        "(#1042)", eid, len(twins), eid)
    return None
