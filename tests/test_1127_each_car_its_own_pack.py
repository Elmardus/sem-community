"""(#1127) Each charger's estimator uses its own car's battery size and target.

Two chargers, 40 kWh on one and 80 kWh on the other: the second car's
estimated level moved as if it had the first car's pack. Every per-charger
``EVTaperDetector`` was built with the flat config and read the flat
``ev_battery_capacity_kwh`` / ``ev_target_soc`` — the setup value or the
primary's copy — while the card writes each car's value into its own
``ev_chargers`` entry.

The estimate is not only shown: ``energy_accounted_soc`` caps the sensor in
the SOC stop decision, so a pack read at half its size stops the car early.
"""
from __future__ import annotations

import ast
import copy
import inspect
import pathlib
import textwrap
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from custom_components.solar_energy_management import persist_per_charger_option
from custom_components.solar_energy_management.coordinator.coordinator import (
    SEMCoordinator,
)
from custom_components.solar_energy_management.coordinator.ev_taper_detector import (
    CHARGE_EFFICIENCY,
    EVTaperDetector,
)

PKG = pathlib.Path(__file__).resolve().parents[1]

# Flat keys = the first car (setup value / primary's copy). Car 2 differs.
_CONFIG = {
    "ev_battery_capacity_kwh": 40,
    "ev_target_soc": 80,
    "daily_ev_target": 10,
    "ev_chargers": [
        {"id": "keba", "ev_battery_capacity_kwh": 40, "ev_target_soc": 80,
         "daily_ev_target": 10},
        {"id": "wallbox", "ev_battery_capacity_kwh": 80, "ev_target_soc": 50,
         "daily_ev_target": 4},
    ],
}


def _cfg():
    return copy.deepcopy(_CONFIG)


def _det(cid, cfg=None):
    return EVTaperDetector(cfg if cfg is not None else _cfg(), charger_id=cid)


class TestEachCarReadsItsOwnPack:
    def test_estimated_level_uses_the_second_car_s_pack(self):
        """20 kWh used from full: 75 % of an 80 kWh pack, not 50 % of 40."""
        det = _det("wallbox")
        det._soc_anchored = True
        det._energy_since_full = 20.0
        assert det.get_virtual_soc(None) == pytest.approx(75.0)

    def test_the_first_car_keeps_its_own_pack(self):
        det = _det("keba")
        det._soc_anchored = True
        det._energy_since_full = 20.0
        assert det.get_virtual_soc(None) == pytest.approx(50.0)

    def test_the_stop_guard_counts_kwh_into_the_right_pack(self):
        """The #708 cap the SOC stop decision reads: 8 kWh into 80 kWh."""
        det = _det("wallbox")
        det.get_virtual_soc(40.0)                 # sensor anchors at 40 %
        det._current_session_energy_kwh += 8.0
        assert det.energy_accounted_soc() == pytest.approx(
            40.0 + 8.0 * CHARGE_EFFICIENCY / 80.0 * 100.0)

    def test_the_daily_drop_divides_by_the_right_pack(self):
        det = _det("wallbox")
        det.apply_daily_decay(20.0, 4.0)
        assert det.estimated_soc == pytest.approx(75.0)

    def test_a_sensorless_first_session_starts_from_its_own_target(self):
        """The bootstrap assumes the car arrived at ITS target minus what it took."""
        det = _det("wallbox")
        det._session_peak_w = 7000.0
        det.on_session_end(10.0)
        assert det.estimated_soc == pytest.approx(50.0)   # car 2's target, not 80

    def test_the_full_charge_floor_scales_with_its_own_pack(self):
        cfg = _cfg()
        cfg["ev_chargers"][1]["ev_battery_capacity_kwh"] = 24
        assert _det("wallbox", cfg)._session_energy_floor_kwh() == pytest.approx(0.6)
        assert _det("keba", cfg)._session_energy_floor_kwh() == pytest.approx(1.0)

    def test_battery_health_is_measured_against_its_own_pack(self):
        det = _det("wallbox")
        det._battery_health_samples = [{"capacity_estimate_kwh": 72.0}] * 3
        det._calculate_battery_health()
        assert det.battery_health_pct == pytest.approx(90.0)


class TestWhereTheValueComesFrom:
    def test_none_is_the_primary_charger(self):
        assert _det(None).capacity_kwh == 40.0
        cfg = _cfg()
        cfg["ev_chargers"][0]["ev_battery_capacity_kwh"] = 64
        assert _det(None, cfg).capacity_kwh == 64.0

    def test_no_charger_list_reads_the_flat_key(self):
        assert EVTaperDetector({"ev_battery_capacity_kwh": 55}).capacity_kwh == 55.0

    def test_a_null_or_missing_own_value_falls_back_to_flat(self):
        cfg = _cfg()
        cfg["ev_chargers"][1]["ev_battery_capacity_kwh"] = None
        del cfg["ev_chargers"][1]["ev_target_soc"]
        det = _det("wallbox", cfg)
        assert det.capacity_kwh == 40.0
        assert det.car_setting("ev_target_soc", 99) == 80

    def test_null_everywhere_is_the_default(self):
        det = EVTaperDetector({"ev_battery_capacity_kwh": None})
        assert det.capacity_kwh == 40.0

    def test_an_unknown_charger_reads_the_flat_key(self):
        assert _det("gone").capacity_kwh == 40.0

    def test_a_card_write_reaches_the_running_detector(self):
        """The real write path swaps fresh dicts into ``ev_chargers``; the
        detector looks its car up on every read, so no reload is needed."""
        cfg = _cfg()
        det = _det("wallbox", cfg)
        coordinator = SimpleNamespace(config=cfg)
        entry = SimpleNamespace(data=_cfg(), options={})
        hass = MagicMock()
        persist_per_charger_option(
            hass, entry, coordinator, "wallbox", "ev_battery_capacity_kwh", 60.0)
        assert det.capacity_kwh == 60.0
        assert _det("keba", cfg).capacity_kwh == 40.0


class TestTheCoordinatorHandsEachDetectorItsCar:
    def test_restored_detectors_read_their_own_car(self):
        c = SEMCoordinator.__new__(SEMCoordinator)
        c.config = _cfg()
        c._ev_taper_detectors = {}
        SEMCoordinator._restore_per_charger_detectors(c, {"chargers": {
            "keba": {"soc_anchored": True, "energy_since_full": 20.0},
            "wallbox": {"soc_anchored": True, "energy_since_full": 20.0},
        }})
        assert c._ev_taper_detectors["keba"].capacity_kwh == 40.0
        assert c._ev_taper_detectors["wallbox"].capacity_kwh == 80.0
        assert c._ev_taper_detectors["wallbox"].get_virtual_soc(None) == \
            pytest.approx(75.0)

    def test_the_daily_drop_falls_back_to_each_car_s_own_target(self):
        """No prediction yet: each car drops by its own daily target."""
        c = SEMCoordinator.__new__(SEMCoordinator)
        c.config = _cfg()
        keba, wallbox = _det("keba", c.config), _det("wallbox", c.config)
        for d in (keba, wallbox):
            d._last_full_timestamp = "2026-10-14T07:00:00"
        c._ev_taper_detectors = {"keba": keba, "wallbox": wallbox}
        c._last_ev_connected_per_charger = {}
        c._predictor = MagicMock()
        c._predictor.predict_ev_consumption_tomorrow = MagicMock(return_value=0.0)
        c._predictor.predict_car_ev_consumption_tomorrow = MagicMock(return_value=0.0)
        c._read_outdoor_temperature = MagicMock(return_value=15.0)
        c._apply_daily_taper_decay(MagicMock(), SimpleNamespace(ev_connected=False))
        assert keba._energy_since_full == pytest.approx(10.0)
        assert wallbox._energy_since_full == pytest.approx(4.0)

    def test_every_detector_built_in_the_package_names_its_charger(self):
        """A detector built without ``charger_id`` falls back to the
        primary's car. Every construction must say whose car it tracks;
        only the primary default in ``__init__`` may say None."""
        sites = _detector_constructions()
        assert [s for s, _fn, kind in sites if kind == "missing"] == []
        assert [s for s, fn, kind in sites
                if kind == "none" and fn != "__init__"] == []

    def test_the_guard_finds_the_constructions(self):
        """Vacuity twin: the walk sees the live loop and the restore, each
        passing a real charger id."""
        named = {fn for _s, fn, kind in _detector_constructions() if kind == "named"}
        assert {"_update_ev_intelligence", "_restore_per_charger_detectors"} <= named

    def test_the_self_heal_reads_the_car_s_own_pack(self):
        """The self-heal in ``_update_ev_intelligence`` sizes the healed
        estimate with the detector's pack, not the flat key."""
        tree = ast.parse(textwrap.dedent(
            inspect.getsource(SEMCoordinator._update_ev_intelligence)))
        flat = [n.lineno for n in ast.walk(tree)
                if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute) and n.func.attr == "get"
                and n.args and isinstance(n.args[0], ast.Constant)
                and n.args[0].value == "ev_battery_capacity_kwh"]
        assert flat == []
        assert any(isinstance(n, ast.Attribute) and n.attr == "capacity_kwh"
                   for n in ast.walk(tree))

    def test_a_charger_without_an_id_is_matched_by_its_place(self):
        """Setup names an id-less entry ``ev_charger_<idx>``; so does the
        detector, so the second car still reads its own pack."""
        cfg = _cfg()
        del cfg["ev_chargers"][1]["id"]
        assert _det("ev_charger_1", cfg).capacity_kwh == 80.0


def _detector_constructions():
    """Every ``EVTaperDetector(...)`` call in the package (tests excluded):
    ``(file:line, enclosing function, kind)`` — kind is ``missing`` (no
    ``charger_id``), ``none`` (``charger_id=None``) or ``named``."""
    found = {}
    for path in PKG.rglob("*.py"):
        if "tests" in path.relative_to(PKG).parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        scopes = [tree] + [n for n in ast.walk(tree) if isinstance(
            n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        for scope in scopes:   # outer first, so the innermost function wins
            for node in ast.walk(scope):
                if not (isinstance(node, ast.Call)
                        and getattr(node.func, "id", None) == "EVTaperDetector"):
                    continue
                kw = next((k.value for k in node.keywords
                           if k.arg == "charger_id"), None)
                kind = ("missing" if kw is None else
                        "none" if isinstance(kw, ast.Constant) and kw.value is None
                        else "named")
                found[f"{path.relative_to(PKG)}:{node.lineno}"] = (
                    getattr(scope, "name", None), kind)
    return [(site, fn, kind) for site, (fn, kind) in sorted(found.items())]


def _detector_config_reads():
    """Every ``self._config.get(<key>)`` in the detector: ``(method, key)``."""
    tree = ast.parse(inspect.getsource(EVTaperDetector))
    reads = []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef):
            continue
        for node in ast.walk(fn):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "get"
                    and isinstance(node.func.value, ast.Attribute)
                    and node.func.value.attr == "_config"):
                key = (node.args[0].value if node.args
                       and isinstance(node.args[0], ast.Constant) else None)
                reads.append((fn.name, key))
    return reads


class TestTheDetectorReadsCarSettingsOneWay:
    """Inside the detector, ``self._config`` is read only by ``car_setting``
    — except the keys that are one value for the whole home."""

    _HOME_WIDE = {"ev_charger_efficiency"}

    def test_no_direct_config_read_outside_the_resolver(self):
        bad = [(fn, key) for fn, key in _detector_config_reads()
               if fn != "car_setting" and key not in self._HOME_WIDE]
        assert bad == []

    def test_the_guard_sees_the_one_home_wide_read(self):
        """Vacuity twin: the efficiency read is found and allowed."""
        assert ("_charge_efficiency", "ev_charger_efficiency") in \
            _detector_config_reads()
