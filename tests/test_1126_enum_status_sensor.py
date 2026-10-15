"""#1126 — a status sensor of ``device_class: enum`` is still a status sensor.

Found by running SEM's detection on Home Assistant's own Peblar test data
(HA 2026.8.2, ``tests/integrations_rig/captures/peblar.json``). The Peblar's
state sensor (``cp_state``: ``suspended``, ``charging``, ``no_ev_connected``
…) carries ``device_class: enum``. ``_discover_peblar`` took a state sensor
only with NO device class, so a Peblar found through the device list got no
plug and no charging sensor: SEM could not tell a car was plugged in.

Root shape: the rule meant "a text sensor" and said "no device class". HA's
``enum`` class is the class of a text sensor with a fixed list of states —
what a status sensor is. ``_discover_easee`` had the same test.

The pins below:

* HA's own Peblar, in either order, through the brand rule, the whole
  registry discovery and the detection report;
* a German, Dutch, Spanish or Swedish Peblar — the id is built in the
  install's language, the translation key is not;
* the bound sensor read as the coordinator reads it: plugged at
  ``suspended``, charging at ``charging``, empty at ``no_ev_connected``;
* Easee, with and without the class;
* a code check that no brand function asks for a missing device class.
"""
from __future__ import annotations

import ast
import inspect
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from custom_components.solar_energy_management import hardware_detection as hd
from custom_components.solar_energy_management.coordinator.charger_adapters.status_enum import (
    classify_charger_status,
    is_cable_present,
)
from custom_components.solar_energy_management.hardware_detection import (
    _discover_easee,
    _discover_peblar,
    build_detection_report,
    discover_all_ev_chargers_from_registry,
)

_CAPTURE = Path(__file__).parent / "integrations_rig" / "captures" / "peblar.json"
_STATE = "sensor.peblar_ev_charger_state"


def _entry(entity_id, platform, device_id="dev-1", device_class=None,
           translation_key=None, options=None):
    return SimpleNamespace(
        entity_id=entity_id, platform=platform, device_id=device_id,
        original_device_class=device_class, disabled_by=None,
        original_unit_of_measurement=None, unit_of_measurement=None,
        translation_key=translation_key,
        unique_id=entity_id.split(".", 1)[1],
        capabilities={"options": options} if options else None,
        config_entry_id="entry-1",
    )


def _capture():
    return json.loads(_CAPTURE.read_text())["entities"]


def _peblar(reverse=False, rename=None):
    rename = rename or {}
    rows = list(reversed(_capture())) if reverse else _capture()
    return [_entry(rename.get(e["entity_id"], e["entity_id"]), "peblar",
                   "peblar-1", e.get("original_device_class"),
                   e.get("translation_key"),
                   (e.get("attributes") or {}).get("options"))
            for e in rows]


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


class TestThePeblarFromHomeAssistantsOwnTestData:
    def test_the_data_really_holds_the_trap(self):
        """No vacuous pass: the state sensor IS ``enum`` in core's data, so
        the rule that was there — no device class — binds nothing."""
        state = next(e for e in _capture() if e["entity_id"] == _STATE)
        assert state["original_device_class"] == "enum"
        old_rule = [e["entity_id"] for e in _capture()
                    if e["entity_id"].startswith("sensor.")
                    and "state" in e["entity_id"]
                    and e.get("original_device_class") is None]
        assert old_rule == []

    def test_the_brand_rule_binds_the_state_sensor(self):
        result = _discover_peblar(_peblar())
        assert result["ev_connected_sensor"] == _STATE
        assert result["ev_charging_sensor"] == _STATE

    def test_registry_discovery_binds_it_in_either_order(self):
        for reverse in (False, True):
            charger = _discover(_peblar(reverse=reverse))[0]
            assert charger["ev_connected_sensor"] == _STATE, reverse
            assert charger["ev_charging_sensor"] == _STATE, reverse

    def test_the_limit_source_is_never_the_state(self):
        """The other ``enum`` sensor on the box says why the current is
        limited; it is never read as the car's state."""
        charger = _discover(_peblar())[0]
        assert "sensor.peblar_ev_charger_limit_source" not in charger.values()

    def test_the_diagnostics_report_shows_the_same(self):
        report = build_detection_report(MagicMock(), registry=_registry(_peblar()))
        peblar = [c for c in report["chargers"] if c.get("platform") == "peblar"]
        assert peblar
        mapped = peblar[0]["mapped"]
        assert mapped["ev_connected_sensor"]["entity"] == _STATE
        assert mapped["ev_charging_sensor"]["entity"] == _STATE

    @pytest.mark.parametrize("word", ["zustand", "status", "estado", "tillstand"])
    def test_a_peblar_in_another_language_binds_it_too(self, word):
        """HA builds the id in the install's language (de ``Zustand``, nl
        ``Status``, es ``Estado``, sv ``Tillstånd``); the translation key
        ``cp_state`` is the same in every one."""
        renamed = f"sensor.peblar_ev_charger_{word}"
        charger = _discover(_peblar(rename={_STATE: renamed}))[0]
        assert charger["ev_connected_sensor"] == renamed
        assert charger["ev_charging_sensor"] == renamed

    def test_the_bound_sensor_reads_the_car(self):
        """What the binding is for: every state core lists reads as the
        coordinator's reader reads it. A car plugged in and paused
        (``suspended``) is plugged in and not charging."""
        state = next(e for e in _capture() if e["entity_id"] == _STATE)
        options = set(state["attributes"]["options"])
        assert {"suspended", "charging", "no_ev_connected"} <= options
        assert is_cable_present("suspended") is True
        assert is_cable_present("charging") is True
        assert is_cable_present("no_ev_connected") is False
        assert classify_charger_status("charging") == "charging"
        assert classify_charger_status("suspended") != "charging"
        assert classify_charger_status("no_ev_connected") != "charging"


class TestEasee:
    def _easee(self, status_class):
        return [
            _entry("sensor.easee_home_eh123456_status", "easee", "easee-1",
                   status_class, "easee_status"),
            _entry("sensor.easee_home_eh123456_power", "easee", "easee-1",
                   "power", "power"),
            _entry("sensor.easee_home_eh123456_temp_max", "easee", "easee-1",
                   "temperature", "internal_temperature"),
        ]

    @pytest.mark.parametrize("status_class", [None, "enum"])
    def test_the_status_sensor_binds_with_or_without_the_class(self, status_class):
        result = _discover_easee(self._easee(status_class))
        assert result["ev_connected_sensor"] == "sensor.easee_home_eh123456_status"
        assert result["ev_charging_sensor"] == "sensor.easee_home_eh123456_status"

    def test_a_status_with_a_number_class_is_not_the_state(self):
        """A reading is not a state: a sensor named "status" that HA says
        is a temperature stays out."""
        rows = self._easee(None)
        rows[0] = _entry("sensor.easee_home_eh123456_status", "easee",
                         "easee-1", "temperature", "easee_status")
        assert "ev_connected_sensor" not in _discover_easee(rows)


# ── the code check ───────────────────────────────────────────────────────

def _asks_for_no_class(node) -> bool:
    """Does this expression ask that a device class be missing?
    ``dc is None``, ``dc == None``, ``not dc``, ``dc in (None, …)`` on a
    name ``dc`` or an ``original_device_class`` read."""
    def is_class(n):
        if isinstance(n, ast.Name) and n.id in ("dc", "device_class"):
            return True
        if isinstance(n, ast.Attribute) and n.attr == "original_device_class":
            return True
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                and n.func.id == "getattr" and len(n.args) >= 2
                and isinstance(n.args[1], ast.Constant)
                and n.args[1].value == "original_device_class"):
            return True
        return False

    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return is_class(node.operand)
    if isinstance(node, ast.Compare) and is_class(node.left):
        for op, right in zip(node.ops, node.comparators, strict=True):
            if isinstance(op, (ast.Is, ast.Eq)) and isinstance(right, ast.Constant) \
                    and right.value is None:
                return True
            if isinstance(op, ast.In) and isinstance(right, (ast.Tuple, ast.List, ast.Set)) \
                    and any(isinstance(x, ast.Constant) and x.value is None
                            for x in right.elts):
                return True
    return False


def _offenders(tree) -> list:
    found = []
    for fn in ast.walk(tree):
        if not (isinstance(fn, ast.FunctionDef) and fn.name.startswith("_discover_")):
            continue
        for node in ast.walk(fn):
            if _asks_for_no_class(node):
                found.append(f"{fn.name}:{node.lineno}")
    return found


class TestNoBrandRuleAsksForAMissingDeviceClass:
    def test_no_brand_function_does(self):
        """A status sensor is read with ``_is_state_text`` — none or
        ``enum`` — never with a test for a missing class."""
        tree = ast.parse(inspect.getsource(hd))
        assert _offenders(tree) == []

    @pytest.mark.parametrize("shape", [
        "dc is None",
        "dc == None",
        "not dc",
        "dc in (None, 'x')",
        "entry.original_device_class is None",
        "getattr(entry, 'original_device_class', None) is None",
    ])
    def test_the_check_catches_each_shape(self, shape):
        src = (
            "def _discover_x(entities):\n"
            "    for entry in entities:\n"
            "        dc = entry.original_device_class\n"
            f"        if {shape}:\n"
            "            pass\n"
        )
        assert _offenders(ast.parse(src)) == ["_discover_x:4"]

    def test_the_fixed_form_passes_it(self):
        src = (
            "def _discover_x(entities):\n"
            "    for entry in entities:\n"
            "        dc = entry.original_device_class\n"
            "        if _is_state_text(entry) and dc == 'power':\n"
            "            pass\n"
        )
        assert _offenders(ast.parse(src)) == []


# ── a Peblar setup saved before the fix ──────────────────────────────────

def _saved_before_the_fix():
    """What zero-config setup saved before #1126: detection's answer with
    no plug and no charging sensor, through setup's own builder."""
    from custom_components.solar_energy_management import (
        build_discovered_charger_storage,
    )
    found = dict(_discover(_peblar())[0])
    found.pop("ev_connected_sensor")
    found.pop("ev_charging_sensor")
    return build_discovered_charger_storage({}, {}, found)[2]


def _heal(chargers, entries):
    from custom_components.solar_energy_management import (
        _fill_state_sensors_of_own_save,
    )
    with patch.object(hd.entity_registry, "async_get",
                      return_value=_registry(entries)):
        return _fill_state_sensors_of_own_save(MagicMock(), chargers)


class TestASavedPeblarGetsItsStateSensor:
    """Detection runs again only while no charger is saved, so a Peblar
    setup saved without a state sensor would never get one."""

    def test_the_save_really_has_none(self):
        saved = _saved_before_the_fix()
        assert saved[0]["ev_current_control_entity"] == \
            "number.peblar_ev_charger_charge_limit"
        assert not saved[0].get("ev_connected_sensor")
        assert not saved[0].get("ev_charging_sensor")

    def test_the_heal_fills_both(self):
        saved = _saved_before_the_fix()
        healed = _heal(saved, _peblar())
        assert healed[0]["ev_connected_sensor"] == _STATE
        assert healed[0]["ev_charging_sensor"] == _STATE
        # nothing else moves
        rest = {k: v for k, v in healed[0].items()
                if k not in ("ev_connected_sensor", "ev_charging_sensor")}
        assert rest == saved[0]

    def test_it_runs_once(self):
        healed = _heal(_saved_before_the_fix(), _peblar())
        assert _heal(healed, _peblar()) is None

    def test_a_charger_the_user_set_up_is_left_alone(self):
        mine = dict(_saved_before_the_fix()[0], id="ev_charger_1")
        assert _heal([mine], _peblar()) is None

    def test_one_sensor_set_is_the_user_choice(self):
        saved = _saved_before_the_fix()
        saved[0]["ev_charging_sensor"] = "binary_sensor.my_car_charging"
        assert _heal(saved, _peblar()) is None

    def test_a_second_charger_is_kept_as_it_is(self):
        other = {"id": "ev_charger_1", "name": "Garage",
                 "ev_connected_sensor": "binary_sensor.keba_plug"}
        healed = _heal(_saved_before_the_fix() + [other], _peblar())
        assert healed[0]["ev_connected_sensor"] == _STATE
        assert healed[1] is other

    def test_a_normal_install_never_walks_the_registry(self):
        from custom_components.solar_energy_management import (
            _fill_state_sensors_of_own_save,
        )
        mine = [{"id": "ev_charger_0",
                 "ev_connected_sensor": "binary_sensor.keba_plug"}]
        with patch.object(hd.entity_registry, "async_get",
                          side_effect=AssertionError("walked")):
            assert _fill_state_sensors_of_own_save(MagicMock(), mine) is None

    def test_setup_heals_before_it_reads_the_charger_list(self):
        import custom_components.solar_energy_management as pkg
        tree = ast.parse(Path(pkg.__file__).read_text(encoding="utf-8"))
        setup = next(n for n in ast.walk(tree)
                     if isinstance(n, ast.AsyncFunctionDef)
                     and n.name == "async_setup_entry")
        heal = [n.lineno for n in ast.walk(setup)
                if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Name)
                and n.func.id == "_fill_state_sensors_of_own_save"]
        read = [n.lineno for n in ast.walk(setup)
                if isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "full_config"
                        for t in n.targets)]
        assert len(heal) == 2 and read, (heal, read)
        assert max(heal) < min(read), (heal, read)
