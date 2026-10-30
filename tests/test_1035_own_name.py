"""#1035 — a word in the DEVICE name is not a word about the entity.

Found by loading Home Assistant's own Peblar test data (HA 2026.8.2) and
running SEM's detection on it. SEM picked:

* start/stop: ``switch.peblar_ev_charger_force_single_phase``, not
  ``switch.peblar_ev_charger_charge`` — so SEM would switch the box to one
  phase to "stop" a charge;
* charging power: ``sensor.peblar_ev_charger_power_phase_3``, one phase,
  not ``sensor.peblar_ev_charger_power`` — so SEM saw a third of the power.

Root cause, switch: Home Assistant builds an entity id from the device name
and the entity's own name. The Peblar's default device name is "Peblar EV
Charger", so "charge" is in EVERY id of the device, and the brand rule
``"charge" in entity_id`` matched both switches. The loop kept the last one.
The same shape is in every brand rule that tests a word against the whole
id: a charger whose owner named it "Charger" or "Carport" fed every entity
to the rule.

Root cause, power: the brand rule binds on ``device_class: power`` and keeps
the last sensor; the per-phase sensors come after the total. Bug class 89
already swapped a CAPABILITY bound this way, but not one phase.

The pins below assert the class, not the brand:

* the Peblar from HA's own test data, in either order;
* every platform in ``_EV_CHARGER_PLATFORMS``, on a device named "EV
  Charger" and one named "Carport Charger", with role-free entities before
  and after the real ones — no role takes one;
* a code check that no brand rule tests a word against the whole id.
"""
from __future__ import annotations

import ast
import inspect
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from custom_components.solar_energy_management import hardware_detection as hd
from custom_components.solar_energy_management.hardware_detection import (
    _EV_CHARGER_PLATFORMS,
    _discover_ocpp,
    _discover_openwb,
    _discover_unit,
    _is_phase_leg,
    _own_names,
    _reject_capability_sensor,
    build_detection_report,
    discover_all_ev_chargers_from_registry,
    ocpp_charge_control_switch,
)


def _entry(entity_id, platform, device_id="dev-1", device_class=None,
           unit=None, translation_key=None, unique_id=None):
    return SimpleNamespace(
        entity_id=entity_id, platform=platform, device_id=device_id,
        original_device_class=device_class, disabled_by=None,
        original_unit_of_measurement=unit, unit_of_measurement=unit,
        translation_key=translation_key,
        unique_id=unique_id or entity_id.split(".", 1)[1],
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


# ── Home Assistant's own Peblar (tests/components/peblar, HA 2026.8.2) ──
# entity id, device class, unit, translation key — as core's snapshots
# register them, in the snapshots' order.
_PEBLAR = [
    ("binary_sensor.peblar_ev_charger_active_errors", "problem", None, "active_error_codes"),
    ("binary_sensor.peblar_ev_charger_active_warnings", "problem", None, "active_warning_codes"),
    ("button.peblar_ev_charger_identify", "identify", None, None),
    ("button.peblar_ev_charger_restart", "restart", None, None),
    ("number.peblar_ev_charger_charge_limit", "current", "A", "charge_current_limit"),
    ("select.peblar_ev_charger_smart_charging", None, None, "smart_charging"),
    ("sensor.peblar_ev_charger_current", "current", "A", None),
    ("sensor.peblar_ev_charger_current_phase_1", "current", "A", "current_phase_1"),
    ("sensor.peblar_ev_charger_current_phase_2", "current", "A", "current_phase_2"),
    ("sensor.peblar_ev_charger_current_phase_3", "current", "A", "current_phase_3"),
    ("sensor.peblar_ev_charger_lifetime_energy", "energy", "kWh", "energy_total"),
    ("sensor.peblar_ev_charger_limit_source", "enum", None, "charge_current_limit_source"),
    ("sensor.peblar_ev_charger_power", "power", "W", None),
    ("sensor.peblar_ev_charger_power_phase_1", "power", "W", "power_phase_1"),
    ("sensor.peblar_ev_charger_power_phase_2", "power", "W", "power_phase_2"),
    ("sensor.peblar_ev_charger_power_phase_3", "power", "W", "power_phase_3"),
    ("sensor.peblar_ev_charger_session_energy", "energy", "kWh", "energy_session"),
    ("sensor.peblar_ev_charger_state", "enum", None, "cp_state"),
    ("sensor.peblar_ev_charger_uptime", "timestamp", None, "uptime"),
    ("sensor.peblar_ev_charger_voltage_phase_1", "voltage", "V", "voltage_phase_1"),
    ("sensor.peblar_ev_charger_voltage_phase_2", "voltage", "V", "voltage_phase_2"),
    ("sensor.peblar_ev_charger_voltage_phase_3", "voltage", "V", "voltage_phase_3"),
    ("switch.peblar_ev_charger_charge", None, None, "charge"),
    ("switch.peblar_ev_charger_force_single_phase", None, None, "force_single_phase"),
]


def _peblar(reverse=False):
    rows = list(reversed(_PEBLAR)) if reverse else _PEBLAR
    return [_entry(eid, "peblar", "peblar-1", dc, unit, key)
            for eid, dc, unit, key in rows]


class TestThePeblarFromHomeAssistantsOwnTestData:
    def test_start_stop_is_the_charge_switch(self):
        charger = _discover(_peblar())[0]
        assert charger["ev_start_stop_entity"] == "switch.peblar_ev_charger_charge"

    def test_charging_power_is_the_total_not_one_phase(self):
        charger = _discover(_peblar())[0]
        assert charger["ev_charging_power_sensor"] == "sensor.peblar_ev_charger_power"

    def test_the_rest_is_unchanged(self):
        charger = _discover(_peblar())[0]
        assert charger["ev_current_control_entity"] == \
            "number.peblar_ev_charger_charge_limit"
        assert charger["ev_session_energy_sensor"] == \
            "sensor.peblar_ev_charger_session_energy"
        assert charger["ev_total_energy_sensor"] == \
            "sensor.peblar_ev_charger_lifetime_energy"

    def test_registry_order_does_not_decide(self):
        forward = _discover(_peblar())[0]
        backward = _discover(_peblar(reverse=True))[0]
        assert forward == backward

    def test_the_diagnostics_report_shows_the_same(self):
        report = build_detection_report(MagicMock(), registry=_registry(_peblar()))
        peblar = [c for c in report["chargers"] if c.get("platform") == "peblar"]
        assert peblar
        mapped = peblar[0]["mapped"]
        assert mapped["ev_start_stop_entity"]["entity"] == \
            "switch.peblar_ev_charger_charge"
        assert mapped["ev_charging_power_sensor"]["entity"] == \
            "sensor.peblar_ev_charger_power"

    def test_one_renamed_entity_does_not_bring_the_bug_back(self):
        """Owners rename entity ids. One renamed id must not switch the own
        names off for the whole device (the review of this fix)."""
        for renamed in ("sensor.wallbox_uptime", "sensor.ev_power"):
            at = {"sensor.wallbox_uptime": 18, "sensor.ev_power": 12}[renamed]
            rows = list(_PEBLAR)
            rows[at] = (renamed,) + rows[at][1:]
            entities = [_entry(e, "peblar", "peblar-1", dc, u, k)
                        for e, dc, u, k in rows]
            charger = _discover(entities)[0]
            assert charger["ev_start_stop_entity"] == \
                "switch.peblar_ev_charger_charge", renamed

    def test_a_german_or_dutch_peblar_keeps_its_current_control(self):
        """HA builds ids in the install's language: the limit is
        ``…_ladestrombegrenzung`` (de) or ``…_laadlimiet`` (nl), with no
        English word but the device name. It is the unit's only current
        number, so it stays the control (the review of this fix: without
        it SEM set up no charger at all). The switch names are examples:
        whatever they are, the single-phase switch is never start/stop."""
        for limit, charge, single in (
                ("ladestrombegrenzung", "laden", "einphasig_erzwingen"),
                ("laadlimiet", "laden", "eenfasig_forceren")):
            rename = {
                "number.peblar_ev_charger_charge_limit":
                    f"number.peblar_ev_charger_{limit}",
                "switch.peblar_ev_charger_charge":
                    f"switch.peblar_ev_charger_{charge}",
                "switch.peblar_ev_charger_force_single_phase":
                    f"switch.peblar_ev_charger_{single}",
            }
            entities = [_entry(rename.get(e, e), "peblar", "peblar-1", dc, u, k)
                        for e, dc, u, k in _PEBLAR]
            charger = _discover(entities)[0]
            assert charger["ev_current_control_entity"] == \
                f"number.peblar_ev_charger_{limit}"
            assert charger.get("ev_start_stop_entity") != \
                f"switch.peblar_ev_charger_{single}"

    def test_the_data_really_holds_the_trap(self):
        """No vacuous pass: the rule that was there, spelled out — a word
        tested against the whole id, the last match kept — hands back
        exactly the two entities #1035 names, on this data, in this order."""
        switches = [e for e, *_ in _PEBLAR if e.startswith("switch.")]
        powers = [e for e, dc, *_ in _PEBLAR
                  if e.startswith("sensor.") and dc == "power"]
        assert [s for s in switches if "charge" in s][-1] == \
            "switch.peblar_ev_charger_force_single_phase"
        assert powers[-1] == "sensor.peblar_ev_charger_power_phase_3"


# ── The entity's own name ────────────────────────────────────────────
def _garo(device, suffix=""):
    return [
        _entry(f"switch.{device}{suffix}", "garo_wallbox", "garo-1"),
        _entry(f"sensor.{device}_power{suffix}", "garo_wallbox", "garo-1", "power", "W"),
        _entry(f"number.{device}_current_limit{suffix}", "garo_wallbox", "garo-1",
               "current", "A"),
    ]


class TestGaroKeepsItsStartStop:
    """The row's "laddbox" hint finds GARO's switch, which is the device
    itself — also for a second box and for a box its owner named."""

    def test_the_reporters_box(self):
        charger = _discover(_garo("garo_laddbox"))[0]
        assert charger["ev_start_stop_entity"] == "switch.garo_laddbox"

    def test_a_second_box_of_the_same_name(self):
        charger = _discover(_garo("garo_laddbox", "_2"))[0]
        assert charger["ev_start_stop_entity"] == "switch.garo_laddbox_2"

    def test_a_box_named_laddbox_garage(self):
        charger = _discover(_garo("laddbox_garage"))[0]
        assert charger["ev_start_stop_entity"] == "switch.laddbox_garage"


class TestOwnNames:
    def test_the_device_name_is_taken_off(self):
        own = _own_names(_peblar())
        assert own["switch.peblar_ev_charger_force_single_phase"] == \
            "_force_single_phase"
        assert own["switch.peblar_ev_charger_charge"] == "_charge"

    def test_a_main_entity_keeps_the_device_name(self):
        """GARO's start/stop switch IS the device: its id is the device name
        alone, so the device name is its own name."""
        own = _own_names(_garo("garo_laddbox"))
        assert own["switch.garo_laddbox"] == "_garo_laddbox"
        assert own["sensor.garo_laddbox_power"] == "_power"

    def test_a_second_box_of_the_same_name_too(self):
        """Home Assistant numbers a second box ``_2``: the main entity is
        still the device name, not the number."""
        own = _own_names(_garo("garo_laddbox", "_2"))
        assert own["switch.garo_laddbox_2"] == "_garo_laddbox_2"
        assert own["sensor.garo_laddbox_power_2"] == "_power_2"

    def test_a_word_only_some_ids_start_with_is_their_own(self):
        """A small unit must agree in full: "charge" starts two of three
        ids here, and is theirs."""
        own = _own_names([
            _entry("switch.box_charge_a", "x"),
            _entry("switch.box_charge_b", "x"),
            _entry("sensor.box_power", "x"),
        ])
        assert own["switch.box_charge_a"] == "_charge_a"
        # three of four is not "all" on a unit this small
        own = _own_names([
            _entry("sensor.wb_charging_power", "x"),
            _entry("number.wb_charging_current", "x"),
            _entry("switch.wb_charging_enable", "x"),
            _entry("binary_sensor.wb_plug", "x"),
        ])
        assert own["number.wb_charging_current"] == "_charging_current"

    def test_one_renamed_id_does_not_hide_the_device_name(self):
        """A real charger publishes many entities. One its owner renamed
        keeps its whole name; the rest still lose the device name."""
        rows = [(e, dc, u, k) for e, dc, u, k in _PEBLAR]
        rows[18] = ("sensor.wallbox_uptime",) + rows[18][1:]
        entities = [_entry(e, "peblar", "peblar-1", dc, u, k) for e, dc, u, k in rows]
        own = _own_names(entities)
        assert own["switch.peblar_ev_charger_force_single_phase"] == \
            "_force_single_phase"
        assert own["sensor.wallbox_uptime"] == "_wallbox_uptime"

    def test_three_quarters_of_eight_is_enough(self):
        ids = [f"sensor.peblar_ev_charger_x{i}" for i in range(6)] + [
            "sensor.my_a", "sensor.my_b"]
        own = _own_names([_entry(e, "peblar") for e in ids])
        assert own["sensor.peblar_ev_charger_x0"] == "_x0"

    def test_five_of_eight_is_not(self):
        ids = [f"sensor.peblar_ev_charger_x{i}" for i in range(5)] + [
            "sensor.my_a", "sensor.my_b", "sensor.other_c"]
        own = _own_names([_entry(e, "peblar") for e in ids])
        assert own["sensor.peblar_ev_charger_x0"] == "_peblar_ev_charger_x0"

    def test_a_small_unit_with_a_renamed_id_keeps_whole_names(self):
        own = _own_names([
            _entry("switch.ev_charger_charge", "peblar"),
            _entry("switch.ev_charger_child_lock", "peblar"),
            _entry("sensor.my_power", "peblar"),
        ])
        assert own["switch.ev_charger_child_lock"] == "_ev_charger_child_lock"

    def test_one_entity_alone_keeps_its_whole_name(self):
        own = _own_names([_entry("switch.ev_charger_charge", "peblar")])
        assert own == {"switch.ev_charger_charge": "_ev_charger_charge"}

    def test_a_transport_keeps_the_whole_name(self):
        """On mqtt the device name is the only mark of the brand: the
        JuiceBox and Wallbox rows ask for it."""
        own = _own_names([
            _entry("sensor.juicebox_power", "mqtt"),
            _entry("sensor.juicebox_lifetime_energy", "mqtt"),
        ])
        assert own["sensor.juicebox_power"] == "_juicebox_power"

    def test_names_that_share_nothing_lose_nothing(self):
        own = _own_names([
            _entry("sensor.garage_power", "keba"),
            _entry("binary_sensor.keba_plug", "keba"),
        ])
        assert own["sensor.garage_power"] == "_garage_power"


# ── One phase is not the reading ─────────────────────────────────────
class TestOnePhaseIsSwappedForTheTotal:
    def test_a_bound_phase_is_swapped_for_the_total(self):
        entities = _peblar()
        result = {"ev_charging_power_sensor": "sensor.peblar_ev_charger_power_phase_3"}
        _reject_capability_sensor(result, entities)
        assert result["ev_charging_power_sensor"] == "sensor.peblar_ev_charger_power"

    def test_l1_l2_l3_are_phases_too(self):
        entities = [
            _entry("sensor.wb_power", "wallbox", device_class="power", unit="W"),
            _entry("sensor.wb_power_l3", "wallbox", device_class="power", unit="W"),
        ]
        result = {"ev_charging_power_sensor": "sensor.wb_power_l3"}
        _reject_capability_sensor(result, entities)
        assert result["ev_charging_power_sensor"] == "sensor.wb_power"

    def test_swap_only_a_phase_with_no_total_stays(self):
        """Class 89: a charger with no power entity is worse than one that
        reads a third of it."""
        entities = [
            _entry("sensor.wb_power_phase_1", "wallbox", device_class="power", unit="W"),
            _entry("sensor.wb_status", "wallbox"),
        ]
        result = {"ev_charging_power_sensor": "sensor.wb_power_phase_1"}
        _reject_capability_sensor(result, entities)
        assert result["ev_charging_power_sensor"] == "sensor.wb_power_phase_1"

    def test_three_phase_power_is_the_sum_not_a_leg(self):
        assert _is_phase_leg("sensor.box_power_phase_3")
        assert _is_phase_leg("sensor.box_phase_2_power")
        assert _is_phase_leg("sensor.box_power_l1")
        assert not _is_phase_leg("sensor.box_3_phase_power")
        assert not _is_phase_leg("sensor.box_power")

    def _phase_1_box(self):
        return [
            _entry("sensor.phase_1_box_power", "wallbox", device_class="power", unit="W"),
            _entry("sensor.phase_1_box_import_power", "wallbox", device_class="power",
                   unit="W"),
            _entry("sensor.phase_1_box_power_l2", "wallbox", device_class="power", unit="W"),
            _entry("sensor.phase_1_box_total_power", "wallbox", device_class="power",
                   unit="W"),
        ]

    def test_a_device_named_phase_1_is_not_one_phase(self):
        """Whether a sensor is one phase is read from its own name: the
        total stays, though the whole id says "phase_1"."""
        result = {"ev_charging_power_sensor": "sensor.phase_1_box_power"}
        _reject_capability_sensor(result, self._phase_1_box())
        assert result["ev_charging_power_sensor"] == "sensor.phase_1_box_power"

    def test_on_a_device_named_phase_1_a_real_leg_finds_its_sum(self):
        """…and the twin search reads the own name too, or it would throw
        the total away as "one phase"."""
        result = {"ev_charging_power_sensor": "sensor.phase_1_box_power_l2"}
        _reject_capability_sensor(result, self._phase_1_box())
        assert result["ev_charging_power_sensor"] == "sensor.phase_1_box_power"

    def test_a_phase_is_never_swapped_for_another_quantity(self):
        """A device can publish its grid, solar or battery power beside the
        charger's. One phase of the charge is closer than any of those."""
        entities = [
            _entry("sensor.box_power_l1", "x", device_class="power", unit="W"),
            _entry("sensor.box_power_l3", "x", device_class="power", unit="W"),
            _entry("sensor.box_grid_power", "x", device_class="power", unit="W"),
            _entry("sensor.box_battery_power", "x", device_class="power", unit="W"),
        ]
        result = {"ev_charging_power_sensor": "sensor.box_power_l3"}
        _reject_capability_sensor(result, entities)
        assert result["ev_charging_power_sensor"] == "sensor.box_power_l3"

    def test_a_total_named_sum_is_the_sum(self):
        entities = [
            _entry("sensor.box_power_l1", "x", device_class="power", unit="W"),
            _entry("sensor.box_grid_power", "x", device_class="power", unit="W"),
            _entry("sensor.box_power_sum", "x", device_class="power", unit="W"),
        ]
        result = {"ev_charging_power_sensor": "sensor.box_power_l1"}
        _reject_capability_sensor(result, entities)
        assert result["ev_charging_power_sensor"] == "sensor.box_power_sum"

    def test_a_total_named_total_is_the_sum(self):
        entities = [
            _entry("sensor.box_power_l3", "x", device_class="power", unit="W"),
            _entry("sensor.box_grid_power", "x", device_class="power", unit="W"),
            _entry("sensor.box_total_power", "x", device_class="power", unit="W"),
        ]
        result = {"ev_charging_power_sensor": "sensor.box_power_l3"}
        _reject_capability_sensor(result, entities)
        assert result["ev_charging_power_sensor"] == "sensor.box_total_power"


# ── A read role is never lost to the own names (class 89) ─────────────
class TestAReadRoleKeepsTheWholeIdAnswer:
    """openWB's power rule asks for "charg". A loadpoint whose device name
    says "Chargepoint" and whose power sensor is named in German matched
    only through the device name. Losing the reading costs more than the
    guess (class 89), so a measurand role the own names leave empty keeps
    the whole-id answer."""

    def _loadpoint(self):
        return [
            _entry("sensor.chargepoint_3_ladeleistung", "openwb2mqtt", "lp3", "power", "W"),
            _entry("binary_sensor.chargepoint_3_plug", "openwb2mqtt", "lp3", "plug"),
            _entry("number.chargepoint_3_current", "openwb2mqtt", "lp3", "current", "A"),
        ]

    def test_the_own_names_alone_find_no_power(self):
        """Non-vacuous: without the fallback the role is gone."""
        assert "ev_charging_power_sensor" not in _discover_openwb(self._loadpoint())

    def test_the_power_reading_stays(self):
        charger = _discover(self._loadpoint())[0]
        assert charger["ev_charging_power_sensor"] == \
            "sensor.chargepoint_3_ladeleistung"

    def test_a_control_is_not_filled_from_the_device_name(self):
        """A switch that only the device name calls "charge" is a guess that
        acts — it stays unbound."""
        entities = [
            _entry("sensor.charger_power_active_import", "ocpp", "cp", "power", "W"),
            _entry("sensor.charger_status_connector", "ocpp", "cp"),
            _entry("number.charger_maximum_current", "ocpp", "cp", "current", "A"),
            _entry("switch.charger_child_lock", "ocpp", "cp"),
        ]
        assert "ev_start_stop_entity" not in _discover_unit(_discover_ocpp, entities)

    def test_a_number_ha_does_not_call_a_current_is_not_filled(self):
        """The lone-number fallback trusts HA's ``device_class: current``,
        never the device name alone."""
        entities = [
            _entry("sensor.charger_power_active_import", "ocpp", "cp", "power", "W"),
            _entry("sensor.charger_status_connector", "ocpp", "cp"),
            _entry("number.charger_led_brightness", "ocpp", "cp"),
        ]
        assert "ev_current_control_entity" not in _discover_unit(
            _discover_ocpp, entities)

    def test_two_current_numbers_are_a_choice_it_does_not_make(self):
        """A rule that keeps the FIRST match, on a device whose name holds
        its word: the whole-id answer is one of two current numbers."""
        def _first_wins(entities):
            own = _own_names(entities)
            out = {"ev_charging_power_sensor": "sensor.ev_charger_power"}
            for e in entities:
                eid = str(e.entity_id)
                if eid.startswith("number.") and "charg" in own[eid]:
                    out.setdefault("ev_current_control_entity", eid)
            return out
        entities = [
            _entry("sensor.ev_charger_power", "x", "d", "power", "W"),
            _entry("number.ev_charger_stromgrenze", "x", "d", "current", "A"),
            _entry("number.ev_charger_mindeststrom", "x", "d", "current", "A"),
        ]
        # non-vacuous: the whole id does answer, with the first number
        assert _first_wins(hd._WholeIds(entities))["ev_current_control_entity"] \
            == "number.ev_charger_stromgrenze"
        assert "ev_current_control_entity" not in _discover_unit(
            _first_wins, entities)

    def test_the_device_name_alone_never_makes_a_charger(self):
        """The fallback fills a charger the own names found; it never makes
        one. A Zaptec installation its owner called "Carport Charger" has
        only "charg" in its device name to look like a charger."""
        entities = [
            _entry("sensor.carport_charger_total_power", "zaptec", "inst",
                   "power", "W"),
            # no class: a ``connectivity`` sensor is never the charge (#1142)
            _entry("binary_sensor.carport_charger_led", "zaptec", "inst"),
            _entry("number.carport_charger_available_current", "zaptec", "inst",
                   "current", "A",
                   unique_id="inst_available_current"),
        ]
        # non-vacuous: the whole ids alone would admit it
        assert hd._discover_zaptec(hd._WholeIds(entities))
        assert _discover(entities) == []

    def test_the_fallback_never_takes_an_entity_another_role_holds(self):
        def _fn(entities):
            own = _own_names(entities)
            out = {"ev_session_energy_sensor": "sensor.total_box_session"}
            for e in entities:
                if "total" in own[str(e.entity_id)]:
                    out["ev_total_energy_sensor"] = str(e.entity_id)
            return out
        # the whole-id pass keeps the LAST "total": the session counter
        entities = [_entry("sensor.total_box_power", "x"),
                    _entry("sensor.total_box_session", "x")]
        assert _fn(hd._WholeIds(entities))["ev_total_energy_sensor"] == \
            "sensor.total_box_session"
        result = _discover_unit(_fn, entities)
        assert "ev_total_energy_sensor" not in result


# ── The class, across every brand SEM discovers ──────────────────────
# Each platform gets one device whose NAME carries the words the rules look
# for. Beside the real entities sit entities whose own names name no role:
# placed first AND last, so neither a last-wins nor a first-wins loop can
# hide one.
_ROLE_ROWS = [
    ("binary_sensor", "plug_connected", "plug", None),
    ("binary_sensor", "charging", "power", None),
    ("sensor", "status_connector", None, None),
    ("sensor", "charging_power", "power", "W"),
    ("sensor", "total_energy", "energy", "kWh"),
    ("sensor", "session_energy", "energy", "kWh"),
    ("number", "charging_current", "current", "A"),
    ("switch", "charge", None, None),
]
_ROLE_FREE = [
    ("switch", "force_single_phase", None, None),
    ("switch", "child_lock", None, None),
    ("number", "led_brightness", None, None),
    ("select", "display_language", None, None),
    ("binary_sensor", "update_available", None, None),
    ("binary_sensor", "online", "connectivity", None),
    ("sensor", "wifi_signal", "signal_strength", "dBm"),
    ("button", "identify", "identify", None),
]
_DEVICE_NAMES = ("ev_charger", "carport_charger")


def _every_platform(device_name, decoys_last=True):
    entries = []
    for platform, _fn in _EV_CHARGER_PLATFORMS:
        dev = f"{platform}-{device_name}"
        real = [_entry(f"{dom}.{device_name}_{own}", platform, dev, dc, unit)
                for dom, own, dc, unit in _ROLE_ROWS]
        free = [_entry(f"{dom}.{device_name}_{own}", platform, dev, dc, unit)
                for dom, own, dc, unit in _ROLE_FREE]
        entries += real + free if decoys_last else free + real
    return entries


def _role_free_ids(device_name):
    return {f"{dom}.{device_name}_{own}" for dom, own, _dc, _u in _ROLE_FREE}


class TestNoBrandReadsTheDeviceName:
    def test_most_brands_actually_bite(self):
        """Non-vacuous: the registry must really produce chargers."""
        for device_name in _DEVICE_NAMES:
            assert len(_discover(_every_platform(device_name))) >= 10

    def test_the_device_names_do_carry_the_rule_words(self):
        """Non-vacuous: every role-free entity carries "charg" through the
        device name — the word half the brand rules ask for."""
        for device_name in _DEVICE_NAMES:
            for eid in _role_free_ids(device_name):
                assert "charg" in eid

    def test_no_role_takes_an_entity_whose_own_name_names_none(self):
        for device_name in _DEVICE_NAMES:
            free = _role_free_ids(device_name)
            for decoys_last in (True, False):
                for charger in _discover(_every_platform(device_name, decoys_last)):
                    for role, eid in charger.items():
                        if role.startswith("_"):
                            continue
                        assert eid not in free, (
                            f"{charger.get('_platform')} bound {eid} as {role} "
                            f"(device {device_name})")


# ── The manual OCPP path (#976) ──────────────────────────────────────
def _patched_registry(entries):
    class _Reg:
        def __init__(self):
            self.entities = {e.entity_id: e for e in entries}

        def async_get(self, eid):
            return self.entities.get(eid)
    return patch("homeassistant.helpers.entity_registry.async_get",
                 return_value=_Reg())


class TestOcppNeverStopsWithAvailability:
    def test_auto_detection_skips_the_availability_switch(self):
        """Where the own names cannot help (no device name shared), the
        auto-detection still never takes the availability switch — the rule
        the manual path already had."""
        entities = [
            _entry("sensor.cp_power_active_import", "ocpp", "cp", "power", "W"),
            _entry("sensor.cp_status_connector", "ocpp", "cp"),
            _entry("switch.charger_charge_control", "ocpp", "cp"),
            _entry("switch.charger_availability", "ocpp", "cp"),
        ]
        assert _own_names(entities)["switch.charger_availability"] == \
            "_charger_availability"
        charger = _discover(entities)[0]
        assert charger["ev_start_stop_entity"] == "switch.charger_charge_control"


class TestTheOcppManualPath:
    def test_a_charge_point_named_charger_finds_its_charge_control(self):
        """OCPP names a charge point "charger" unless told otherwise, so
        "charge" is in every switch it has."""
        entries = [
            _entry("number.charger_maximum_current", "ocpp", "cp"),
            _entry("switch.charger_reset_lock", "ocpp", "cp"),
            _entry("switch.charger_charge_control", "ocpp", "cp"),
            _entry("switch.charger_availability", "ocpp", "cp"),
        ]
        with _patched_registry(entries):
            assert ocpp_charge_control_switch(
                MagicMock(), "number.charger_maximum_current") == \
                "switch.charger_charge_control"


# ── The code check ───────────────────────────────────────────────────
_WHOLE_ID_NAMES = {"eid", "eid_lower", "entity_id"}


def _brand_functions():
    names = {fn.__name__ for _p, fn in _EV_CHARGER_PLATFORMS
             if getattr(fn, "__name__", "<lambda>") != "<lambda>"}
    names |= {n for n in dir(hd) if n.startswith("_discover_")}
    names |= {"charger_from_near_miss", "ocpp_charge_control_switch"}
    return sorted(n for n in names if callable(getattr(hd, n, None)))


def _is_whole_id(node) -> bool:
    """``eid``, ``eid_lower``, ``entity_id``, ``x.entity_id`` — and any of
    them through ``str()``, ``.lower()`` or ``.casefold()``."""
    if isinstance(node, ast.Name):
        return node.id in _WHOLE_ID_NAMES
    if isinstance(node, ast.Attribute):
        return node.attr == "entity_id"
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in ("lower", "casefold"):
            return _is_whole_id(func.value)
        if isinstance(func, ast.Name) and func.id == "str" and node.args:
            return _is_whole_id(node.args[0])
    return False


def _whole_id_word_tests(fn) -> list:
    """Every word test on a whole id: ``<x> in eid`` (any ``x``), a regex
    over it, or ``_name_hit(eid, …)``. Suffix tests (``endswith``) are fine:
    the device name is in front."""
    tree = ast.parse(inspect.getsource(fn).lstrip())
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            for op, right in zip(node.ops, node.comparators, strict=True):
                if isinstance(op, (ast.In, ast.NotIn)) and _is_whole_id(right):
                    hits.append(f"{ast.unparse(node.left)} in {ast.unparse(right)}")
        if isinstance(node, ast.Call) and node.args:
            func = node.func
            name = func.id if isinstance(func, ast.Name) else (
                func.attr if isinstance(func, ast.Attribute) else "")
            if name == "_name_hit" and _is_whole_id(node.args[0]):
                hits.append(f"_name_hit({ast.unparse(node.args[0])}, …)")
            if name in ("search", "match", "fullmatch", "findall") and any(
                    _is_whole_id(a) for a in node.args):
                hits.append(f"{name}(…, {ast.unparse(node.args[-1])})")
    return hits


class TestNoBrandRuleTestsAWordAgainstTheWholeId:
    def test_the_check_sees_the_brand_functions(self):
        """Non-vacuous: the walk covers the hand-written brands and the
        hint matcher."""
        names = _brand_functions()
        for must in ("_discover_peblar", "_discover_ocpp", "_discover_zaptec",
                     "_discover_from_hints", "charger_from_near_miss"):
            assert must in names

    def test_the_check_catches_the_old_rule(self):
        def _old(entities):
            for entry in entities:
                eid = entry.entity_id
                if eid.startswith("switch.") and "charge" in eid:
                    return eid
        assert _whole_id_word_tests(_old) == ["'charge' in eid"]

    def test_the_check_catches_the_other_shapes(self):
        import re

        def _old(entities, words):
            for entry in entities:
                eid = str(entry.entity_id)
                if any(w in eid for w in words):
                    return eid
                if "x" in eid.lower() or "y" in entry.entity_id:
                    return eid
                if re.search("z", eid) or _name_hit(eid, "w"):  # noqa: F821
                    return eid
                if eid.endswith("_amp"):          # a suffix: fine
                    return eid
        assert len(_whole_id_word_tests(_old)) == 5

    def test_no_brand_function_does(self):
        offenders = {n: _whole_id_word_tests(getattr(hd, n))
                     for n in _brand_functions()}
        assert not {n: h for n, h in offenders.items() if h}
