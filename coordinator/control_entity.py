"""#824 — is the entity SEM is about to command actually there?

A dead SENSOR makes SEM's numbers look wrong, which people notice and
report. A dead CONTROL entity makes SEM look like it is working — a
commanded current on the dashboard, a strategy that explains itself, a
clean log — while the car does whatever it likes.

@onkelfu lost days to exactly that: one unsupported ``mode: slider`` line
in a template number meant HA never loaded the entity properly (it existed
only as ``restored: true``), so every write SEM made landed nowhere. He
found it by instrumenting the Modbus proxy himself.

SEM already had ``raise_charger_actuation_failed``, and it did not fire —
that Repair is driven by three consecutive writes that RAISED, and his
writes never raised. **The failure produces no error**, which is why this
check is a pre-flight look at the entity rather than error handling around
the write.

Pure and injectable, deliberately: no HA imports, ``state_of`` supplied by
the caller. Same shape as #814's ``validate_phase_switch_entity``, which
this generalises — that one only needed to know whether an entity existed,
and existence is precisely what is NOT the question here.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Optional, Tuple

# Domains SEM can actually command. The ``input_*`` twins are first class:
# they are what people build when their charger exposes no native control,
# and #804 had to learn that lesson separately for phase switching.
CONTROL_ENTITY_DOMAINS = (
    "number", "input_number",
    "switch", "input_boolean",
    "select", "input_select",
)

#: (#1032, bug class 49) What SEM can start and stop a charge with: it turns
#: a switch or an input_boolean on and off, and presses a button (#804:
#: Wattpilot's "Laden erzwingen"). The config flow and the card offer switch
#: and button; refusing the button filed this Repair on a healthy charger.
START_STOP_DOMAINS = ("switch", "input_boolean", "button")

# States that mean "this entity is not currently commandable". ``restored``
# entities — HA loaded a name from the registry but the platform never
# produced the entity — report exactly this.
_DEAD_STATES = ("unavailable", "unknown", "none", "")

#: (#1032) The reason for a control entity its own live device has made
#: ``unavailable`` because the command does not apply right now. Valid, not
#: broken: the entity is there and its integration is speaking.
NOT_OFFERED_NOW = "not_offered_now"


@dataclass(frozen=True)
class ControlEntityVerdict:
    """What SEM believes about one configured control entity."""

    configured: Optional[str]
    """The entity_id the user named, or None when the capability is simply
    not configured."""

    valid: Optional[bool]
    """True when commandable, False when declared-but-broken, None when
    nothing was configured — absence of a capability is not a fault."""

    reason: Optional[str] = None
    """``wrong_domain`` / ``missing`` / ``unavailable`` when broken;
    ``NOT_OFFERED_NOW`` on a valid verdict (#1032). Stable strings, so the
    Repair and the card can key off them rather than parse prose."""


def device_says_not_now(
    own_attributes: Optional[Mapping[str, Any]],
    siblings: Iterable[Tuple[Optional[str], Optional[Mapping[str, Any]]]],
) -> bool:
    """(#1032) Is an ``unavailable`` control entity its live device's answer?

    Some integrations publish a control as ``unavailable`` while its command
    does not apply: Zaptec's "Charging" switch whenever resume/stop is not
    valid (no car, car full), HA core's Wallbox pause switch while no car is
    connected. The entity is fine; the device is telling us "not now".

    ``siblings`` are ``(state, attributes)`` of the OTHER entities on the
    same device from the same config entry — only the control's own
    integration may answer for it. The answer is yes only when:

    * the entity itself is not ``restored`` — HA restored a name its platform
      never produced (#824's template number, or an integration still
      loading). No device can speak for an entity that does not exist;
    * at least one sibling holds a real reading — the device is live;
    * no connectivity sibling reads ``off`` — the device says it is offline.

    No siblings at all (a YAML template with no device) answers no, so the
    entity is judged on its own state as before.
    """
    if (own_attributes or {}).get("restored"):
        return False
    live = False
    for state, attributes in siblings:
        attributes = attributes or {}
        if attributes.get("restored"):
            continue
        value = str(state).strip().lower() if state is not None else ""
        if attributes.get("device_class") == "connectivity" and value == "off":
            return False
        if value not in _DEAD_STATES:
            live = True
    return live


def validate_control_entity(
    entity_id: Optional[str],
    state_of: Callable[[str], Optional[str]],
    *,
    not_now: Optional[Callable[[str], bool]] = None,
    domains: Tuple[str, ...] = CONTROL_ENTITY_DOMAINS,
) -> ControlEntityVerdict:
    """Judge one configured control entity.

    ``state_of(entity_id)`` returns the entity's state string, or None when
    the entity does not exist. The state — not mere existence — is the
    question: a restored entity exists and cannot be commanded.

    ``not_now(entity_id)`` (#1032) answers whether an ``unavailable`` entity
    is its live device saying the command does not apply right now. When it
    says yes the entity is valid with reason ``NOT_OFFERED_NOW``. Without it,
    ``unavailable`` is broken, as before.

    ``domains`` are the domains this field may hold (``START_STOP_DOMAINS``
    for start/stop). A button that was never pressed reads ``unknown``; that
    is its normal state, not a fault.
    """
    if not entity_id:
        return ControlEntityVerdict(None, None, None)

    domain = str(entity_id).split(".", 1)[0]
    if domain not in domains:
        return ControlEntityVerdict(entity_id, False, "wrong_domain")

    state = state_of(entity_id)
    if state is None:
        return ControlEntityVerdict(entity_id, False, "missing")
    value = str(state).strip().lower()
    if domain == "button" and value == "unknown":
        return ControlEntityVerdict(entity_id, True, None)
    if value in _DEAD_STATES:
        if value == "unavailable" and not_now is not None and not_now(entity_id):
            return ControlEntityVerdict(entity_id, True, NOT_OFFERED_NOW)
        return ControlEntityVerdict(entity_id, False, "unavailable")
    return ControlEntityVerdict(entity_id, True, None)
