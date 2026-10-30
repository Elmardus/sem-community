"""#1034 — the V2C Trydan: SEM took the minimum current and the solar power.

Found by loading Home Assistant's own V2C test data (HA 2026.8.2) and running
SEM's detection on it. SEM picked:

* current control: ``number.…_min_intensity`` (the minimum), not
  ``number.…_intensity`` — so every SEM write would move the floor of the
  range and leave the charge where it was;
* charging power: ``sensor.…_photovoltaic_power`` (the solar array), not
  ``sensor.…_charge_power`` — so SEM would read solar output as the car.

Root cause: the brand rule keeps the LAST number named "intensity" and the
LAST ``device_class: power`` sensor. The box publishes three current numbers
(the set-point and both ends of its range) and four power sensors (the car,
the house, the solar array, the home battery), so registry order decided.

The fix is generic, at the choke point every discovery path runs through:

* bug class 56, second instance — a current number named as one END of a
  range (``min``/``max``) is swapped for its set-point twin; a floor with no
  twin is dropped, a ceiling with no twin stays (Alfen, Wallbox, Zaptec and
  OCPP drive a "max current" number and have no other);
* bug class 89, third instance — a read role bound to another circuit
  (house, solar, battery, grid) is swapped for the car's reading; swap only.
"""
from __future__ import annotations

import itertools
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from custom_components.solar_energy_management.hardware_detection import (
    _EV_CHARGER_PLATFORMS,
    _MEASURAND_ROLES,
    _discover_v2c,
    _own_names,
    _reject_capability_sensor,
    _reject_range_end_current_control,
    apply_charger_discovery_guards,
    build_detection_report,
    discover_all_ev_chargers_from_registry,
    probe_charger_candidates,
)


def _entry(entity_id, platform, device_id="dev-1", device_class=None,
           unit=None, translation_key=None):
    return SimpleNamespace(
        entity_id=entity_id, platform=platform, device_id=device_id,
        original_device_class=device_class, disabled_by=None,
        original_unit_of_measurement=unit, unit_of_measurement=unit,
        translation_key=translation_key,
        unique_id=f"{device_id}_{translation_key or entity_id}",
        config_entry_id="entry-1",
    )


def _registry(entries):
    registry = MagicMock()
    registry.entities.values.return_value = entries
    return registry


def _discover(entries):
    with patch(
        "custom_components.solar_energy_management.hardware_detection."
        "entity_registry.async_get",
        return_value=_registry(entries),
    ):
        return discover_all_ev_chargers_from_registry(MagicMock())


# ── Home Assistant's own V2C (tests/components/v2c, HA 2026.8.2) ──────
# entity id, device class, unit, translation key — as core's snapshots
# register them, in the snapshots' order.
_V2C = [
    ("number.evse_1_1_1_1_installation_voltage", "voltage", "V", "voltage_installation"),
    ("number.evse_1_1_1_1_intensity", "current", "A", "intensity"),
    ("number.evse_1_1_1_1_max_intensity", "current", "A", "max_intensity"),
    ("number.evse_1_1_1_1_min_intensity", "current", "A", "min_intensity"),
    ("select.evse_1_1_1_1_charge_mode", None, None, "charge_mode"),
    ("sensor.evse_1_1_1_1_battery_power", "power", "W", "battery_power"),
    ("sensor.evse_1_1_1_1_charge_energy", "energy", "kWh", "charge_energy"),
    ("sensor.evse_1_1_1_1_charge_power", "power", "W", "charge_power"),
    ("sensor.evse_1_1_1_1_charge_time", "duration", "s", "charge_time"),
    ("sensor.evse_1_1_1_1_house_power", "power", "W", "house_power"),
    ("sensor.evse_1_1_1_1_ip_address", None, None, "ip_address"),
    ("sensor.evse_1_1_1_1_meter_error", "enum", None, "meter_error"),
    ("sensor.evse_1_1_1_1_photovoltaic_power", "power", "W", "fv_power"),
    ("sensor.evse_1_1_1_1_signal_status", None, None, "signal_status"),
    ("sensor.evse_1_1_1_1_ssid", None, None, "ssid"),
]
_SET_POINT = "number.evse_1_1_1_1_intensity"
_CHARGE_POWER = "sensor.evse_1_1_1_1_charge_power"


def _v2c(rows=None):
    return [_entry(eid, "v2c", "v2c-1", dc, unit, key)
            for eid, dc, unit, key in (rows or _V2C)]


class TestTheV2cFromHomeAssistantsOwnTestData:
    def test_the_control_is_the_set_point(self):
        charger = _discover(_v2c())[0]
        assert charger["ev_current_control_entity"] == _SET_POINT

    def test_the_power_is_the_car(self):
        charger = _discover(_v2c())[0]
        assert charger["ev_charging_power_sensor"] == _CHARGE_POWER

    def test_the_energy_is_unchanged(self):
        charger = _discover(_v2c())[0]
        assert charger["ev_total_energy_sensor"] == \
            "sensor.evse_1_1_1_1_charge_energy"

    def test_no_order_decides(self):
        """Every order of the three numbers and the four power sensors —
        the bug WAS the order."""
        numbers = [r for r in _V2C if r[0].startswith("number.")]
        powers = [r for r in _V2C if r[1] == "power"]
        rest = [r for r in _V2C if r not in numbers and r not in powers]
        for n in itertools.permutations(numbers):
            for p in itertools.permutations(powers):
                charger = _discover(_v2c(list(n) + rest + list(p)))[0]
                assert charger["ev_current_control_entity"] == _SET_POINT, n
                assert charger["ev_charging_power_sensor"] == _CHARGE_POWER, p

    def test_the_diagnostics_report_shows_the_same(self):
        report = build_detection_report(MagicMock(), registry=_registry(_v2c()))
        v2c = [c for c in report["chargers"] if c.get("platform") == "v2c"]
        assert v2c
        mapped = v2c[0]["mapped"]
        assert mapped["ev_current_control_entity"]["entity"] == _SET_POINT
        assert mapped["ev_charging_power_sensor"]["entity"] == _CHARGE_POWER

    def test_a_german_v2c_is_read_by_its_keys(self):
        """HA builds ids in the install's language. The translation keys
        stay English, so the guards still see the floor and the solar."""
        rename = {
            "number.evse_1_1_1_1_intensity": "number.evse_1_1_1_1_stromstarke",
            "number.evse_1_1_1_1_min_intensity":
                "number.evse_1_1_1_1_minimale_stromstarke",
            "number.evse_1_1_1_1_max_intensity":
                "number.evse_1_1_1_1_maximale_stromstarke",
            "sensor.evse_1_1_1_1_charge_power": "sensor.evse_1_1_1_1_ladeleistung",
            "sensor.evse_1_1_1_1_house_power": "sensor.evse_1_1_1_1_hausleistung",
            "sensor.evse_1_1_1_1_battery_power":
                "sensor.evse_1_1_1_1_batterieleistung",
            "sensor.evse_1_1_1_1_photovoltaic_power":
                "sensor.evse_1_1_1_1_photovoltaik_leistung",
        }
        rows = [(rename.get(e, e), dc, u, k) for e, dc, u, k in _V2C]
        result = {
            "ev_current_control_entity": "number.evse_1_1_1_1_minimale_stromstarke",
            "ev_charging_power_sensor": "sensor.evse_1_1_1_1_photovoltaik_leistung",
        }
        apply_charger_discovery_guards(result, _v2c(rows))
        assert result["ev_current_control_entity"] == \
            "number.evse_1_1_1_1_stromstarke"
        assert result["ev_charging_power_sensor"] == \
            "sensor.evse_1_1_1_1_ladeleistung"

    def test_the_data_really_holds_the_trap(self):
        """No vacuous pass: the brand rule alone, before the guards, hands
        back exactly the two entities #1034 names, on this data, in this
        order."""
        raw = _discover_v2c(_v2c())
        assert raw["ev_current_control_entity"] == \
            "number.evse_1_1_1_1_min_intensity"
        assert raw["ev_charging_power_sensor"] == \
            "sensor.evse_1_1_1_1_photovoltaic_power"


# ── The control: one end of a range is not the set-point ────────────
def _numbers(device, *own_and_keys):
    return [_entry(f"number.{device}_{own}", "x", "d", "current", "A", key)
            for own, key in own_and_keys]


class TestARangeEndIsNotTheSetPoint:
    def test_a_ceiling_with_a_set_point_twin_swaps(self):
        ents = _numbers("box", ("intensity", None), ("max_intensity", None))
        result = {"ev_current_control_entity": "number.box_max_intensity"}
        _reject_range_end_current_control(result, ents)
        assert result["ev_current_control_entity"] == "number.box_intensity"

    def test_a_floor_with_a_set_point_twin_swaps(self):
        ents = _numbers("box", ("charging_current", None),
                        ("min_charging_current", None))
        result = {"ev_current_control_entity": "number.box_min_charging_current"}
        _reject_range_end_current_control(result, ents)
        assert result["ev_current_control_entity"] == "number.box_charging_current"

    def test_a_floor_alone_is_dropped(self):
        """Monitor-only beats writing every cycle to the minimum."""
        ents = _numbers("box", ("min_current", None), ("charge_limit", None),
                        ("led_level", None))
        result = {"ev_current_control_entity": "number.box_min_current"}
        _reject_range_end_current_control(result, ents)
        assert "ev_current_control_entity" not in result

    def test_a_floor_goes_to_its_ceiling_when_there_is_no_set_point(self):
        """A ceiling alone stays, so a floor beside it must go to it: else
        the order of the two would decide whether SEM can steer the box."""
        ents = _numbers("box", ("min_current", None), ("max_current", None))
        result = {"ev_current_control_entity": "number.box_min_current"}
        _reject_range_end_current_control(result, ents)
        assert result["ev_current_control_entity"] == "number.box_max_current"

    def test_a_min_max_pair_answers_the_same_in_either_order(self):
        rows = [
            ("sensor", "charging_power", "power", "W"),
            ("binary_sensor", "plug_connected", "plug", None),
            ("number", "min_charging_current", "current", "A"),
            ("number", "max_charging_current", "current", "A"),
        ]
        answers = set()
        for order in (rows, list(reversed(rows))):
            ents = [_entry(f"{dom}.wb_{own}", "wallbox", "wb-1", dc, unit)
                    for dom, own, dc, unit in order]
            answers.add(_discover(ents)[0].get("ev_current_control_entity"))
        assert answers == {"number.wb_max_charging_current"}

    def test_two_set_points_leave_a_floor_dropped(self):
        ents = _numbers("box", ("intensity", "other"), ("current", "intensity"),
                        ("min_intensity", "min_intensity"))
        result = {"ev_current_control_entity": "number.box_min_intensity"}
        _reject_range_end_current_control(result, ents)
        assert "ev_current_control_entity" not in result

    def test_a_floor_with_two_ceilings_is_dropped(self):
        ents = _numbers("box", ("min_current", "min_current"),
                        ("max_current", "other"),
                        ("led", "max_current"))
        result = {"ev_current_control_entity": "number.box_min_current"}
        _reject_range_end_current_control(result, ents)
        assert "ev_current_control_entity" not in result

    def test_two_set_points_beat_a_ceiling_and_drop_the_floor(self):
        ents = _numbers("box", ("intensity", "other"), ("current", "intensity"),
                        ("max_intensity", "max_intensity"),
                        ("min_intensity", "min_intensity"))
        result = {"ev_current_control_entity": "number.box_min_intensity"}
        _reject_range_end_current_control(result, ents)
        assert "ev_current_control_entity" not in result

    def test_a_minimum_is_not_a_ceiling(self):
        ents = _numbers("box", ("min_current", None),
                        ("minimum_current", None), ("led", None))
        result = {"ev_current_control_entity": "number.box_min_current"}
        _reject_range_end_current_control(result, ents)
        assert "ev_current_control_entity" not in result

    def test_on_a_transport_the_device_name_comes_off_in_front_only(self):
        """mqtt keeps the device name in every id. "Min Laddbox" still has
        a floor of its own, and "Max Box" a floor beside its ceiling."""
        def _mqtt(device, owns):
            return [_entry(f"number.{device}_{own}", "mqtt", "m-1", "current",
                           "A") for own in owns] + [
                _entry(f"sensor.{device}_power", "mqtt", "m-1", "power", "W")]
        ents = _mqtt("min_laddbox", ("min_current", "current"))
        result = {"ev_current_control_entity": "number.min_laddbox_min_current"}
        _reject_range_end_current_control(result, ents)
        assert result["ev_current_control_entity"] == \
            "number.min_laddbox_current"
        ents = _mqtt("max_box", ("min_current", "max_current"))
        result = {"ev_current_control_entity": "number.max_box_min_current"}
        _reject_range_end_current_control(result, ents)
        assert result["ev_current_control_entity"] == "number.max_box_max_current"

    def test_a_word_in_front_of_half_the_ids_is_not_the_device(self):
        ents = [
            _entry("number.min_current", "mqtt", "m-1", "current", "A"),
            _entry("number.min_voltage", "mqtt", "m-1", "voltage", "V"),
            _entry("number.current", "mqtt", "m-1", "current", "A"),
            _entry("sensor.power", "mqtt", "m-1", "power", "W"),
        ]
        result = {"ev_current_control_entity": "number.min_current"}
        _reject_range_end_current_control(result, ents)
        assert result["ev_current_control_entity"] == "number.current"

    def test_only_the_side_with_the_word_is_compared(self):
        """The id says "max", the key does not: the key is not a twin
        test, or a sibling sharing the key would be taken (review)."""
        ents = _numbers("box", ("max_current", "connector_current"),
                        ("led_current", "connector_current"))
        result = {"ev_current_control_entity": "number.box_max_current"}
        _reject_range_end_current_control(result, ents)
        assert result["ev_current_control_entity"] == "number.box_max_current"

    def test_a_device_word_on_a_transport_is_not_a_floor(self):
        """On mqtt the device name stays in every id. "min" is Swedish
        for "my": "Min JuiceBox" keeps its live control."""
        ents = [_entry(f"{dom}.min_juicebox_{own}", "mqtt", "jb-1", dc, unit)
                for dom, own, dc, unit in (
                    ("sensor", "power", "power", "W"),
                    ("sensor", "energy_lifetime", "energy", "kWh"),
                    ("sensor", "status", None, None),
                    ("number", "max_current_online_wanted", "current", "A"),
                )]
        result = {"ev_current_control_entity":
                  "number.min_juicebox_max_current_online_wanted"}
        apply_charger_discovery_guards(result, ents)
        assert result["ev_current_control_entity"] == \
            "number.min_juicebox_max_current_online_wanted"

    def test_a_ceiling_alone_stays(self):
        """Alfen, Wallbox, Zaptec and OCPP drive a "max current" number and
        have no other: it IS the control."""
        for own in ("max_current", "maximum_charging_current",
                    "charger_max_current", "maximum_current"):
            ents = _numbers("box", (own, None), ("min_current", None))
            result = {"ev_current_control_entity": f"number.box_{own}"}
            _reject_range_end_current_control(result, ents)
            assert result["ev_current_control_entity"] == f"number.box_{own}"

    def test_a_device_named_max_is_not_a_ceiling(self):
        ents = _numbers("max", ("charging_current", None),
                        ("led_brightness", None))
        result = {"ev_current_control_entity": "number.max_charging_current"}
        _reject_range_end_current_control(result, ents)
        assert result["ev_current_control_entity"] == "number.max_charging_current"

    def test_a_device_named_min_garage_keeps_its_control(self):
        ents = _numbers("min_garage", ("charging_current", None),
                        ("led_brightness", None))
        result = {"ev_current_control_entity":
                  "number.min_garage_charging_current"}
        _reject_range_end_current_control(result, ents)
        assert result["ev_current_control_entity"] == \
            "number.min_garage_charging_current"

    def test_the_twin_is_found_by_key_when_the_id_was_renamed(self):
        ents = _numbers("box", ("amps", "intensity"),
                        ("min_intensity", "min_intensity"),
                        ("led_brightness", None))
        result = {"ev_current_control_entity": "number.box_min_intensity"}
        _reject_range_end_current_control(result, ents)
        assert result["ev_current_control_entity"] == "number.box_amps"

    def test_the_twin_must_be_a_current_number(self):
        ents = [
            _entry("number.box_min_intensity", "x", "d", "current", "A"),
            _entry("number.box_intensity", "x", "d", None, "%"),
            _entry("sensor.box_intensity", "x", "d", "current", "A"),
        ]
        result = {"ev_current_control_entity": "number.box_min_intensity"}
        _reject_range_end_current_control(result, ents)
        assert "ev_current_control_entity" not in result

    def test_two_twins_are_a_choice_it_does_not_make(self):
        ents = _numbers("box", ("intensity", "other"),
                        ("current", "intensity"),
                        ("max_intensity", "max_intensity"))
        result = {"ev_current_control_entity": "number.box_max_intensity"}
        _reject_range_end_current_control(result, ents)
        assert result["ev_current_control_entity"] == "number.box_max_intensity"

    def test_a_set_point_is_left_alone(self):
        ents = _numbers("box", ("intensity", None), ("min_intensity", None))
        result = {"ev_current_control_entity": "number.box_intensity"}
        _reject_range_end_current_control(result, ents)
        assert result["ev_current_control_entity"] == "number.box_intensity"

    def test_the_offline_guard_still_runs_first(self):
        """JuiceBox (#886): the offline register goes to its online twin,
        which is a ceiling with no set-point twin and so stays."""
        ents = _numbers("juicebox", ("max_current_online_wanted", None),
                        ("max_current_offline_wanted", None))
        result = {"ev_current_control_entity":
                  "number.juicebox_max_current_offline_wanted"}
        apply_charger_discovery_guards(result, ents)
        assert result["ev_current_control_entity"] == \
            "number.juicebox_max_current_online_wanted"


# ── The reading: another circuit is not the car ──────────────────────
def _powers(device, *own_and_keys, dc="power", unit="W"):
    return [_entry(f"sensor.{device}_{own}", "x", "d", dc, unit, key)
            for own, key in own_and_keys]


class TestAnotherCircuitIsNotTheCar:
    def test_each_circuit_is_swapped_for_the_car(self):
        for circuit in ("house_power", "home_power", "photovoltaic_power",
                        "pv_power", "solar_power", "battery_power",
                        "grid_power", "inverter_power", "household_power",
                        "mains_power", "evu_power", "akku_power",
                        "shaper_live_power"):
            ents = _powers("box", ("charge_power", None), (circuit, None),
                       ("led", None))
            result = {"ev_charging_power_sensor": f"sensor.box_{circuit}"}
            _reject_capability_sensor(result, ents)
            assert result["ev_charging_power_sensor"] == \
                "sensor.box_charge_power", circuit

    def test_the_replacement_is_never_another_circuit(self):
        ents = _powers("box", ("battery_power", None), ("house_power", None),
                       ("photovoltaic_power", None), ("charge_power", None))
        for bound in ("battery_power", "house_power", "photovoltaic_power"):
            result = {"ev_charging_power_sensor": f"sensor.box_{bound}"}
            _reject_capability_sensor(result, ents)
            assert result["ev_charging_power_sensor"] == "sensor.box_charge_power"

    def test_swap_only_a_circuit_with_no_car_reading_stays(self):
        """Class 89's rule: a missing power reading is not a safe state."""
        ents = _powers("box", ("house_power", None), ("battery_power", None))
        result = {"ev_charging_power_sensor": "sensor.box_house_power"}
        _reject_capability_sensor(result, ents)
        assert result["ev_charging_power_sensor"] == "sensor.box_house_power"

    def test_energy_too(self):
        ents = _powers("box", ("house_energy", None), ("charge_energy", None),
                       dc="energy", unit="kWh")
        result = {"ev_total_energy_sensor": "sensor.box_house_energy"}
        _reject_capability_sensor(result, ents)
        assert result["ev_total_energy_sensor"] == "sensor.box_charge_energy"

    def test_a_device_named_solar_carport_is_not_a_circuit(self):
        ents = _powers("solar_carport", ("charge_power", None),
                       ("pv_power", None), ("led", None))
        own = _own_names(ents)
        assert own["sensor.solar_carport_charge_power"] == "_charge_power"
        result = {"ev_charging_power_sensor": "sensor.solar_carport_charge_power"}
        _reject_capability_sensor(result, ents)
        assert result["ev_charging_power_sensor"] == \
            "sensor.solar_carport_charge_power"
        result = {"ev_charging_power_sensor": "sensor.solar_carport_pv_power"}
        _reject_capability_sensor(result, ents)
        assert result["ev_charging_power_sensor"] == \
            "sensor.solar_carport_charge_power"

    def test_the_key_names_the_circuit_in_any_language(self):
        ents = _powers("box", ("ladeleistung", "charge_power"),
                       ("photovoltaik_leistung", "fv_power"))
        result = {"ev_charging_power_sensor": "sensor.box_photovoltaik_leistung"}
        _reject_capability_sensor(result, ents)
        assert result["ev_charging_power_sensor"] == "sensor.box_ladeleistung"

    def test_a_device_word_on_a_transport_is_not_a_circuit(self):
        """#1035's phase swap must survive a JuiceBox its owner called
        "Home JuiceBox": on mqtt "home" stays in every id (review)."""
        ents = [_entry(f"sensor.home_juicebox_{own}", "mqtt", "jb-1", dc, unit)
                for own, dc, unit in (
                    ("power", "power", "W"), ("power_l1", "power", "W"),
                    ("energy_lifetime", "energy", "kWh"),
                    ("status", None, None))]
        result = {"ev_charging_power_sensor": "sensor.home_juicebox_power_l1"}
        _reject_capability_sensor(result, ents)
        assert result["ev_charging_power_sensor"] == \
            "sensor.home_juicebox_power"

    def test_on_a_transport_a_circuit_beside_the_device_name_is_seen(self):
        ents = [_entry(f"sensor.home_evse_{own}", "mqtt", "m-1", dc, unit)
                for own, dc, unit in (
                    ("power", "power", "W"), ("home_power", "power", "W"),
                    ("energy", "energy", "kWh"), ("status", None, None))]
        result = {"ev_charging_power_sensor": "sensor.home_evse_home_power"}
        _reject_capability_sensor(result, ents)
        assert result["ev_charging_power_sensor"] == "sensor.home_evse_power"

    def test_a_small_unit_with_a_renamed_id_keeps_its_reading(self):
        """Under eight ids one rename hides the device name; "home" in it
        is still not a circuit (review)."""
        ents = [
            _entry("sensor.home_charger_power", "x", "d", "power", "W"),
            _entry("sensor.home_charger_energy", "x", "d", "energy", "kWh"),
            _entry("number.home_charger_intensity", "x", "d", "current", "A"),
            _entry("sensor.car_power_peak", "x", "d", "power", "W"),
        ]
        result = {"ev_charging_power_sensor": "sensor.home_charger_power"}
        _reject_capability_sensor(result, ents)
        assert result["ev_charging_power_sensor"] == "sensor.home_charger_power"

    def test_among_equals_the_car_wins_over_the_alphabet(self):
        ents = _powers("box", ("house_power", None), ("apparent_power", None),
                       ("ev_charging_power", None))
        result = {"ev_charging_power_sensor": "sensor.box_house_power"}
        _reject_capability_sensor(result, ents)
        assert result["ev_charging_power_sensor"] == \
            "sensor.box_ev_charging_power"

    def test_a_circuit_never_takes_a_reading_another_role_holds(self):
        ents = (_powers("box", ("house_energy", None), dc="energy", unit="kWh")
                + _powers("box", ("session_energy", None), dc="energy",
                          unit="kWh"))
        result = {"ev_total_energy_sensor": "sensor.box_house_energy",
                  "ev_session_energy_sensor": "sensor.box_session_energy"}
        _reject_capability_sensor(result, ents)
        assert result["ev_total_energy_sensor"] == "sensor.box_house_energy"


# ── The class, across every brand SEM discovers ──────────────────────
# Each platform gets one device that publishes the car's reading and the
# set-point beside readings of other circuits and both ends of the current
# range — first AND last, so neither a first-wins nor a last-wins loop can
# hide one.
_REAL = [
    ("binary_sensor", "plug_connected", "plug", None),
    ("binary_sensor", "charging", "power", None),
    ("sensor", "status_connector", None, None),
    ("sensor", "charging_power", "power", "W"),
    ("sensor", "total_energy", "energy", "kWh"),
    ("number", "charging_current", "current", "A"),
    ("switch", "charge", None, None),
]
_TRAPS = [
    ("sensor", "house_power", "power", "W"),
    ("sensor", "photovoltaic_power", "power", "W"),
    ("sensor", "battery_power", "power", "W"),
    ("sensor", "grid_power", "power", "W"),
    ("sensor", "house_total_energy", "energy", "kWh"),
    ("number", "min_charging_current", "current", "A"),
    ("number", "max_charging_current", "current", "A"),
]
_TRAP_OWN = {own for _d, own, _dc, _u in _TRAPS}


def _every_platform(traps_last=True):
    entries = []
    for platform, _fn in _EV_CHARGER_PLATFORMS:
        dev = f"{platform}-1"
        real = [_entry(f"{dom}.wb_{platform}_{own}", platform, dev, dc, unit)
                for dom, own, dc, unit in _REAL]
        traps = [_entry(f"{dom}.wb_{platform}_{own}", platform, dev, dc, unit)
                 for dom, own, dc, unit in _TRAPS]
        entries += real + traps if traps_last else traps + real
    return entries


def _own_of(eid, platform):
    return eid.split(".", 1)[1].removeprefix(f"wb_{platform}_")


class TestNoBrandBindsARangeEndOrAnotherCircuit:
    def test_most_brands_actually_bite(self):
        """Non-vacuous: the registry must really produce chargers that bind
        a control and a power reading."""
        for traps_last in (True, False):
            chargers = _discover(_every_platform(traps_last))
            assert len([c for c in chargers
                        if c.get("ev_current_control_entity")]) >= 8
            assert len([c for c in chargers
                        if c.get("ev_charging_power_sensor")]) >= 10

    def test_no_role_takes_a_trap(self):
        for traps_last in (True, False):
            for charger in _discover(_every_platform(traps_last)):
                platform = charger["_platform"]
                for role in (*_MEASURAND_ROLES, "ev_current_control_entity"):
                    eid = charger.get(role)
                    if eid:
                        assert _own_of(eid, platform) not in _TRAP_OWN, (
                            f"{platform} bound {eid} as {role} "
                            f"(traps last: {traps_last})")

    def test_the_answer_is_the_same_in_either_order(self):
        def _roles(chargers):
            return {c["_platform"]: {
                r: c.get(r)
                for r in (*_MEASURAND_ROLES, "ev_current_control_entity")}
                for c in chargers}
        assert _roles(_discover(_every_platform(True))) == \
            _roles(_discover(_every_platform(False)))

    def test_the_prober_does_not_suggest_one_either(self):
        rows = [(f"{dom}.probe_{own}", dc, unit)
                for dom, own, dc, unit in _TRAPS + _REAL]
        entries = [_entry(eid, "somebrand", "probe-1", dc, unit)
                   for eid, dc, unit in rows]
        candidates = probe_charger_candidates(registry=_registry(entries))
        assert candidates
        for cand in candidates:
            for role in ("ev_charging_power_sensor",
                         "ev_current_control_entity"):
                eid = cand["roles"].get(role)
                assert eid is None or \
                    eid.split(".", 1)[1].removeprefix("probe_") not in _TRAP_OWN, eid
