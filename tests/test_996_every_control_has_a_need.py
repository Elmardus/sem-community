"""#996 — the table is complete by construction.

Every control (number, switch, select, button) either names the modules
and capabilities it needs in ``ENTITY_MODULES`` or is in
``CORE_BY_DECISION`` with a one-line reason. Both is a contradiction,
neither is an undecided control — a knob somebody will see on a house
where it does nothing. Sensors of the families a capability owns
(forecast, tariff, price, export guard, per-kWp, ROI) must choose too."""
from __future__ import annotations

import re

from custom_components.solar_energy_management.coordinator.install_modules import (
    CORE_BY_DECISION, ENTITY_MODULES,
)

CONTROL_PLATFORMS = ("number", "switch", "select", "button")
CAPABILITY_FAMILY = re.compile(
    r"forecast|tariff|price|export_guard|negative|(^|_)pv_|(^|_)roi_|degradation|specific_yield")


def _static_lists():
    from custom_components.solar_energy_management.binary_sensor import BINARY_SENSOR_TYPES
    from custom_components.solar_energy_management.button import BUTTONS
    from custom_components.solar_energy_management.number import NUMBER_TYPES
    from custom_components.solar_energy_management.select import SELECT_TYPES
    from custom_components.solar_energy_management.sensor import SENSOR_TYPES
    from custom_components.solar_energy_management.switch import SWITCH_TYPES
    return {"sensor": SENSOR_TYPES, "number": NUMBER_TYPES, "switch": SWITCH_TYPES,
            "binary_sensor": BINARY_SENSOR_TYPES, "button": BUTTONS,
            "select": SELECT_TYPES}


def _static_keys():
    return {(p, d.key) for p, ds in _static_lists().items() for d in ds}


def test_every_control_has_chosen_a_side():
    undecided = sorted(
        pk for pk in _static_keys()
        if pk[0] in CONTROL_PLATFORMS
        and pk not in ENTITY_MODULES and pk not in CORE_BY_DECISION)
    assert not undecided, (
        "a control must name what it needs in ENTITY_MODULES or, with a "
        f"reason, in CORE_BY_DECISION: {undecided}")


def test_no_control_is_on_both_sides():
    assert not set(ENTITY_MODULES) & set(CORE_BY_DECISION)


def test_every_reason_is_a_sentence():
    for pk, reason in CORE_BY_DECISION.items():
        assert isinstance(reason, str) and len(reason.split()) >= 3, pk


def test_every_sensor_of_a_capability_family_has_chosen():
    undecided = sorted(
        pk for pk in _static_keys()
        if pk[0] in ("sensor", "binary_sensor") and CAPABILITY_FAMILY.search(pk[1])
        and pk not in ENTITY_MODULES and pk not in CORE_BY_DECISION)
    assert not undecided, (
        "a sensor in a capability's family must be in ENTITY_MODULES or, with "
        f"a reason, in CORE_BY_DECISION: {undecided}")


def test_every_row_names_a_real_entity():
    stale = sorted((set(ENTITY_MODULES) | set(CORE_BY_DECISION)) - _static_keys())
    assert not stale, f"rows for keys no platform creates: {stale}"
