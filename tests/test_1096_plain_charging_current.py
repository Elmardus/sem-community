"""#1096 — a plain ``charging_current`` number is a charger's current
control, on a charger and nowhere else.

The lifecycle suite's ``shape_number_plain_name`` proves the charger side
through the real crawler and setup. This file pins the other side: on an
inverter or battery the same word is the pack's charge limit (the #809
trap), so a device that is not a charger — no charger kind, no plug, no
charging reading — never has it read as an EV current control. The
charger-only names that were there before (``amp``, ``charge_rate``) now
follow the same rule; the lexicon always said they should.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from custom_components.solar_energy_management.hardware_detection import (
    read_charger_roles,
)


def _e(eid, *, key=None, dc=None, unit=None):
    return SimpleNamespace(
        entity_id=eid, platform="shape_inverter", translation_key=key,
        unique_id=f"uid_{key or eid.split('.', 1)[1]}",
        original_device_class=dc, unit_of_measurement=unit,
        device_id="dev1", entity_category=None, capabilities={})


POWER = _e("sensor.box_power", key="power", dc="power", unit="W")
PLUG = _e("binary_sensor.box_plug", key="plug", dc="plug")


@pytest.mark.parametrize("key", ["charging_current", "amp", "charge_rate"])
def test_on_a_charger_the_plain_name_is_the_current_control(key):
    num = _e(f"number.box_{key}", key=key, dc="current", unit="A")
    roles = read_charger_roles([POWER, PLUG, num], "shape_inverter")
    assert roles.get("current_number") == num.entity_id


@pytest.mark.parametrize("key", ["charging_current", "amp", "charge_rate"])
def test_on_an_inverter_it_is_not(key):
    """No plug, no charging reading, no charger kind: a battery's limit."""
    num = _e(f"number.inv_{key}", key=key, dc="current", unit="A")
    soc = _e("sensor.inv_battery_soc", key="battery_soc", dc="battery", unit="%")
    roles = read_charger_roles([POWER, soc, num], "shape_inverter")
    assert "current_number" not in roles
