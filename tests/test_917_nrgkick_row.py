"""#917 — NRGkick, from the integration's own keys. (#1032) Found by the
roster's roles now, not a brand row: the same entities, offered in the form
the user confirms (``_found_by: roles``).

@aleho named the control surface (number ``current_set``, switch
``charging_enabled``, number ``phase_count``); core's strings.json declares
the same keys (read 24.09.2026). The row binds by key, never by the user's
device name — and the box publishes a dozen power-class sensors (per phase,
apparent, peak), so the power rule must NAME the one that is the charger's
draw.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from custom_components.solar_energy_management.hardware_detection import (
    discover_all_ev_chargers_from_registry,
)


def _entry(entity_id, platform, device_id, device_class=None):
    obj = entity_id.split(".", 1)[1]
    return SimpleNamespace(
        entity_id=entity_id, platform=platform, device_id=device_id,
        original_device_class=device_class, disabled_by=None,
        unique_id=obj, translation_key=obj.removeprefix("nrgkick_"),
        capabilities={}, entity_category=None, config_entry_id=None,
    )


def _discover(entries):
    registry = MagicMock()
    registry.entities.values.return_value = entries
    with patch(
        "custom_components.solar_energy_management.hardware_detection."
        "entity_registry.async_get", return_value=registry,
    ):
        return discover_all_ev_chargers_from_registry(MagicMock())


def _aleho():
    d = "nrg-1"
    p = "nrgkick"
    return [
        _entry("number.nrgkick_current_set", p, d, "current"),
        _entry("number.nrgkick_energy_limit", p, d, "energy"),
        _entry("number.nrgkick_phase_count", p, d),
        _entry("switch.nrgkick_charging_enabled", p, d),
        _entry("binary_sensor.nrgkick_charge_permitted", p, d),
        _entry("sensor.nrgkick_status", p, d),
        _entry("sensor.nrgkick_charging_rate", p, d, "power"),
        _entry("sensor.nrgkick_l1_active_power", p, d, "power"),
        _entry("sensor.nrgkick_total_active_power", p, d, "power"),
        _entry("sensor.nrgkick_peak_power", p, d, "power"),
        _entry("sensor.nrgkick_charging_current", p, d, "current"),
        _entry("sensor.nrgkick_charged_energy", p, d, "energy"),
        _entry("sensor.nrgkick_total_charged_energy", p, d, "energy"),
    ]


def test_nrgkick_is_a_charger_with_the_controls_aleho_named():
    chargers = _discover(_aleho())
    assert len(chargers) == 1
    c = chargers[0]
    assert c["_platform"] == "nrgkick"
    assert c["ev_current_control_entity"] == "number.nrgkick_current_set"
    assert c["ev_start_stop_entity"] == "switch.nrgkick_charging_enabled"
    assert c["_found_by"] == "roles"


def test_the_power_is_the_total_not_a_phase_or_the_peak():
    c = _discover(_aleho())[0]
    assert c["ev_charging_power_sensor"] == "sensor.nrgkick_total_active_power"


def test_session_and_total_energy_are_told_apart():
    """``charged_energy`` is a substring of ``total_charged_energy``."""
    c = _discover(_aleho())[0]
    assert c["ev_session_energy_sensor"] == "sensor.nrgkick_charged_energy"
    assert c["ev_total_energy_sensor"] == "sensor.nrgkick_total_charged_energy"


def test_energy_limit_is_not_the_current_control():
    """Two numbers on the box; only the current one may drive the car."""
    c = _discover(_aleho())[0]
    assert c["ev_current_control_entity"] != "number.nrgkick_energy_limit"


def test_phase_count_is_offered_as_the_phase_switch():
    """#804's surface: the number takes 1 or 3, so the values are the counts."""
    c = _discover(_aleho())[0]
    assert c["_suggested_phase_switch"] == {
        "entity": "number.nrgkick_phase_count",
        "value_1p": "1", "value_3p": "3",
    }


def test_a_lone_nrgkick_sensor_is_not_a_charger():
    only = [_entry("sensor.nrgkick_total_active_power", "nrgkick", "nrg-2", "power")]
    assert _discover(only) == []
