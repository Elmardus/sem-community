"""#1215 — the balance self-heal flipped a declared grid pair.

When the energy balance stays negative for 3 minutes, the coordinator flips
the grid sign once (``_check_sign_flip``). It flipped the reader's lock on
every home, and ``read_power`` applied that lock to every grid source. A pair
(manual, the dashboard's two sensors, a discovered pair) reads export minus
import, so its sign is set by the pair and there is nothing to flip. Flipped,
2000 W of export read as 2000 W of import. When the flip happened to clear the
balance (the real fault was elsewhere: a solar inverter SEM does not see), it
was kept and saved.

Closed at the read: a pair never takes a learned lock, whoever wrote it — the
self-heal, a saved store, the solar override, the brand seed before a
discovered pair is proven. The self-heal stands down on a pair. The lock is
not written either (review): it is about the source it was learned on, and a
home that goes back to that source needs it.
"""
from __future__ import annotations

import ast
import inspect
import logging
import re
import textwrap
from unittest.mock import MagicMock

import pytest

from custom_components.solar_energy_management.coordinator import sensor_reader as sr
from custom_components.solar_energy_management.coordinator.coordinator import (
    SEMCoordinator,
)
from custom_components.solar_energy_management.coordinator.sensor_reader import (
    SensorReader,
)

from .test_split_grid_integration import (
    _make_energy_dashboard_config, _make_reader_with_states, _state,
)

METER = "sensor.power_meter"
IMP, EXP = "sensor.grid_import_w", "sensor.grid_export_w"
SOLAR = "sensor.solar"

#: The house: a second inverter SEM does not see, so solar reads 1000 W
#: while the meter exports 2000 W. The balance is −1000 W, and the grid is
#: NOT what is wrong.
SOLAR_W, EXPORT_W = 1000, 2000


def _states(solar=SOLAR_W, export=EXPORT_W):
    return {
        SOLAR: _state(solar),
        METER: _state(export, device_class="power"),
        IMP: _state(0, device_class="power"),
        EXP: _state(export, device_class="power"),
        "sensor.grid_import_total": _state(10, "kWh"),
        "sensor.grid_export_total": _state(20, "kWh"),
    }


def _ed():
    return _make_energy_dashboard_config(
        solar_power=SOLAR, grid_import_power=None, battery_power=None)


def _ed_pair_reader(**cfg):
    """HA's grid dialog with two sensors (stat_rate_from / stat_rate_to)."""
    ed = _ed()
    ed.grid_power_from, ed.grid_power_to = IMP, EXP
    return _make_reader_with_states(MagicMock(), _states(), ed, extra_config=cfg)


def _manual_pair_reader(**cfg):
    """The Config card's import / export pair beside a dashboard."""
    return _make_reader_with_states(
        MagicMock(), _states(), _ed(),
        extra_config={"grid_import_power_entity": IMP,
                      "grid_export_power_entity": EXP, **cfg})


def _discovered_pair_reader(**cfg):
    """Counters only; SEM found the meter's two sensors on its device."""
    r = _make_reader_with_states(MagicMock(), _states(), _ed(), extra_config=cfg)
    r._split_grid_discovery.update(
        {"import": IMP, "export": EXP, "confidence": "same-device"})
    return r


def _found_pair_reader(**cfg):
    """A name-only pick the counters proved (#947)."""
    r = _make_reader_with_states(MagicMock(), _states(), _ed(), extra_config=cfg)
    r._split_grid_discovery.update(
        {"import": IMP, "export": EXP, "confidence": "any-device"})
    r._split_grid_scan_due = lambda disc: False
    r._corroborate_split_grid = lambda *a: True
    return r


def _legacy_pair_reader(**cfg):
    """No Energy Dashboard: the Config card's import / export pair."""
    return _make_reader_with_states(
        MagicMock(), _states(), None,
        extra_config={"solar_production_sensor": SOLAR,
                      "grid_import_power_entity": IMP,
                      "grid_export_power_entity": EXP, **cfg})


def _combined_reader(**cfg):
    """A one-sensor meter in SEM's sign: the lock has said no negation."""
    ed = _make_energy_dashboard_config(
        solar_power=SOLAR, grid_import_power=METER, battery_power=None)
    r = _make_reader_with_states(MagicMock(), _states(), ed, extra_config=cfg)
    r._grid_sign_detected = True
    r._grid_sign_inverted = False
    return r


PAIRS = {
    "ed_pair": _ed_pair_reader,
    "manual": _manual_pair_reader,
    "discovered": _discovered_pair_reader,
    "discovered_proven": _found_pair_reader,
    "legacy_manual": _legacy_pair_reader,
}


def _coord(reader):
    c = SEMCoordinator.__new__(SEMCoordinator)
    c._sensor_reader = reader
    c._negative_balance_count = 0
    c._sign_flip_suppression_count = 0
    return c


def _run(coord, cycles):
    """What the coordinator's cycle does: read, the self-heal behind the
    cycle's own gate, and the read again after a flip."""
    reader = coord._sensor_reader
    power = None
    for _ in range(cycles):
        power = reader.read_power()
        if power.grid_power != 0 and power.solar_power > 0:
            if coord._check_sign_flip(power):
                power = reader.read_power()
    return power


# ═══════════════════════════════════════════════════════════════════════
# The issue
# ═══════════════════════════════════════════════════════════════════════

class TestTheSelfHealLeavesAPairAlone:
    @pytest.mark.parametrize("kind", sorted(PAIRS))
    def test_export_stays_export_through_the_negative_balance(self, kind):
        """The reported case: 12 minutes of a −1000 W balance."""
        reader = PAIRS[kind]()
        power = _run(_coord(reader), 18 * 4)
        # the self-heal's own gate was open: solar read, balance −1000 W
        assert power.solar_power == SOLAR_W
        assert reader.grid_read_is_pair() is True
        assert power.grid_power == EXPORT_W            # was −2000: import
        assert power.grid_export_power == EXPORT_W
        assert power.grid_import_power == 0
        assert reader._grid_sign_inverted is False

    @pytest.mark.parametrize("kind", sorted(PAIRS))
    def test_nothing_flipped_is_saved(self, kind):
        reader = PAIRS[kind]()
        _run(_coord(reader), 18 * 4)
        assert reader.export_sign_state()["grid_inverted"] is False

    def test_the_self_heal_does_not_count_or_say_it(self, caplog):
        reader = _ed_pair_reader()
        coord = _coord(reader)
        with caplog.at_level(logging.WARNING):
            _run(coord, 18 * 4)
        assert coord._negative_balance_count == 0
        assert not getattr(coord, "_sign_flip_attempted", False)
        assert not getattr(coord, "_sign_flip_latched", False)
        assert "auto-correcting grid sign" not in caplog.text

    def test_a_combined_meter_is_still_healed(self):
        """Unchanged, and proof the loop above reaches the self-heal: the
        same balance on a one-sensor meter flips its lock."""
        reader = _combined_reader()
        power = _run(_coord(reader), 18 + 1)
        assert reader.grid_read_is_pair() is False
        assert reader._grid_sign_inverted is True
        assert power.grid_power == -EXPORT_W


# ═══════════════════════════════════════════════════════════════════════
# Every writer of the lock — the rule is kept at the read
# ═══════════════════════════════════════════════════════════════════════

class TestNoLockNegatesAPair:
    @pytest.mark.parametrize("kind", sorted(PAIRS))
    def test_a_lock_the_bug_saved_does_not_apply(self, kind):
        """Homes on the old build saved the flip with the pair's own source,
        so the restore check (#1103) kept it."""
        reader = PAIRS[kind]()
        reader.read_power()
        source = reader._grid_source_key
        r = PAIRS[kind]()
        r.restore_sign_state({"grid_detected": True, "grid_inverted": True,
                              "grid_source": None if kind == "legacy_manual"
                              else source})
        assert r.read_power().grid_power == EXPORT_W
        assert r.grid_sign_diagnostics()["grid_read_is_pair"] is True

    def test_the_card_says_the_pair_is_not_negated(self):
        """sensor.sem_diag_grid_sign says what the lock does to the read."""
        from custom_components.solar_energy_management.coordinator.publish_diag import (
            build_diagnostics,
        )
        from .test_620_device_goal_model import TestPublishDiag625
        coord = TestPublishDiag625()._coord()
        for make, lock, said in ((_ed_pair_reader, True, "normal"),
                                 (_combined_reader, True, "negated"),
                                 (_combined_reader, False, "normal")):
            r = make()
            r.read_power()
            r._grid_sign_inverted = lock
            coord._sensor_reader = r
            assert build_diagnostics(coord)["diag_grid_sign"] == said, make

    def test_it_is_said_once(self, caplog):
        r = _ed_pair_reader()
        r.read_power()
        r.restore_sign_state({"grid_detected": True, "grid_inverted": True,
                              "grid_source": r._grid_source_key})
        with caplog.at_level(logging.INFO):
            r.read_power()
            r.read_power()
        said = [m for m in caplog.messages if "#1215" in m]
        assert len(said) == 1, said

    def test_back_on_the_meter_the_lock_still_applies(self):
        """Review: the lock is about the one-sensor meter it was learned
        on. A home that reads a pair for a while and then that meter again
        (the dashboard changed while SEM runs) needs it as it was. The meter
        reads in HA's sign (+ = import), so the lock negates it."""
        ed = _make_energy_dashboard_config(
            solar_power=SOLAR, grid_import_power=METER, battery_power=None)
        states = _states()
        states[METER].state = str(-EXPORT_W)
        r = _make_reader_with_states(MagicMock(), states, ed)
        r._grid_sign_detected = True
        r._grid_sign_inverted = True
        assert r.read_power().grid_power == EXPORT_W
        ed.grid_power_from, ed.grid_power_to = IMP, EXP
        assert r.read_power().grid_power == EXPORT_W
        assert r.grid_read_is_pair() is True
        ed.grid_power_from = ed.grid_power_to = None
        assert r.read_power().grid_power == EXPORT_W
        assert r.export_sign_state()["grid_inverted"] is True

    @pytest.mark.parametrize("kind", sorted(PAIRS))
    def test_a_lock_set_between_reads_is_not_applied(self, kind):
        reader = PAIRS[kind]()
        reader.read_power()
        reader._grid_sign_detected = True
        reader._grid_sign_inverted = True
        assert reader.read_power().grid_power == EXPORT_W

    @pytest.mark.parametrize("kind", ["ed_pair", "manual", "discovered",
                                      "discovered_proven"])
    def test_the_solar_override_does_not_flip_a_pair(self, kind):
        """The solar voter's override branch writes the lock too. Evidence
        that says "negate" is held; a solar step then reaches the branch."""
        reader = PAIRS[kind]()
        reader.read_power()
        reader._grid_sign_solar_evidence = -1e6
        reader._grid_sign_solar_mag = 1e6
        reader._grid_sign_solar_samples = 1000
        states = reader.hass.states
        states.get(SOLAR).state = "4000"
        states.get(EXP).state = "5000"
        assert reader.read_power().grid_power == 5000
        assert reader._grid_sign_inverted is False

    def test_the_brand_seed_before_the_proof_does_not_reach_the_pair(self):
        """A name-matched pair is read once the counters prove it (#947).
        Before that the grid reads nothing, the brand seed runs on the
        import pick and can lock "negate" — which the proven pair took."""
        reader = _make_reader_with_states(MagicMock(), _states(), _ed())
        reader._split_grid_discovery.update(
            {"import": IMP, "export": EXP, "confidence": "any-device"})
        reader._split_grid_scan_due = lambda disc: False
        proven = {"now": None}
        reader._corroborate_split_grid = lambda *a: proven["now"]
        reader._grid_brand_seed = lambda entity_id: ("goodwe", True)
        assert reader.read_power().grid_power == 0           # not proven yet
        assert reader._grid_sign_inverted is True            # the seed's lock
        proven["now"] = True
        assert reader.read_power().grid_power == EXPORT_W

    def test_the_user_flip_still_applies_to_a_pair(self):
        """A tap on the pair is the user's choice (#971) and sits on top."""
        reader = _ed_pair_reader()
        reader.read_power()
        opts = reader.user_flip_options({}, True)
        r = _ed_pair_reader(**opts)
        assert r.read_power().grid_power == -EXPORT_W

    def test_the_grid_sign_invert_option_still_applies_to_a_pair(self):
        assert _ed_pair_reader(grid_sign_invert=True).read_power().grid_power \
            == -EXPORT_W


# ═══════════════════════════════════════════════════════════════════════
# Swept: one battery read as a two-sensor pair — the same read, the same lock
# ═══════════════════════════════════════════════════════════════════════

FLEET = "__fleet__"
_SOLAR_SRC = {"type": "solar", "stat_energy_from": "sensor.pv_energy",
              "stat_rate": "sensor.pv_power"}
_GRID_SRC = {"type": "grid",
             "flow_from": [{"stat_energy_from": "sensor.grid_import"}],
             "flow_to": [{"stat_energy_to": "sensor.grid_export"}],
             "stat_rate": "sensor.grid_power"}
_COUNTERS = {"stat_energy_from": "sensor.batt_discharge_energy",
             "stat_energy_to": "sensor.batt_charge_energy"}
BATTERY_PAIR = {"type": "battery", **_COUNTERS, "power_config": {
    "stat_rate_from": "sensor.batt_discharge_w",
    "stat_rate_to": "sensor.batt_charge_w"}}
BATTERY_ONE_SENSOR = {"type": "battery", **_COUNTERS,
                      "stat_rate": "sensor.batt_power",
                      "power_config": {"stat_rate": "sensor.batt_power"}}
#: a fleet lock saved by this build's store (no #1194 move, no #1193 check)
SAVED_NEGATE = {"battery_detected": {FLEET: True},
                "battery_inverted": {FLEET: True},
                "battery_basis": "sensor", "battery_counters": "own"}


def _battery_reader(source, options=None, one_sensor_w="1200"):
    """The real parser on HA's stored prefs; the battery charges 1200 W.
    The counters have no state, so no sign is learned from them."""
    from custom_components.solar_energy_management.ha_energy_reader import (
        _parse_prefs,
    )
    prefs = {"energy_sources": [_SOLAR_SRC, _GRID_SRC, source],
             "device_consumption": []}
    states = {"sensor.pv_power": "3000", "sensor.grid_power": "0",
              "sensor.batt_charge_w": "1200", "sensor.batt_discharge_w": "0",
              "sensor.batt_power": one_sensor_w}
    hass = MagicMock()
    hass.states.get = lambda eid: (
        MagicMock(state=states[eid], attributes={"unit_of_measurement": "W"})
        if eid in states else None)
    r = SensorReader(hass, dict(options or {}))
    r._sign_vote_warmup = 0
    r.set_energy_dashboard_config(_parse_prefs(MagicMock(), prefs, quiet=True))
    return r


class TestNoLockNegatesABatteryPair:
    def test_the_parser_gives_one_pair(self):
        ed = _battery_reader(BATTERY_PAIR)._energy_dashboard_config
        assert ed.battery_power is None
        assert ed.battery_power_pairs == [
            ("sensor.batt_discharge_w", "sensor.batt_charge_w")]

    def test_a_saved_lock_does_not_negate_the_pair(self):
        """A lock learned on a one-sensor battery, then the dialog moved to
        two sensors: charge read as discharge for good (the counter vote
        waits for no lock)."""
        r = _battery_reader(BATTERY_PAIR)
        r.restore_sign_state(SAVED_NEGATE)
        p = r.read_power()
        assert p.battery_power == pytest.approx(1200.0)
        assert p.battery_charge_power == pytest.approx(1200.0)
        # not written: it is about the one-sensor battery (see below)
        assert r.export_sign_state()["battery_inverted"][FLEET] is True

    def test_the_brand_seed_does_not_negate_the_pair(self):
        """The seed asks the CHARGE COUNTER's integration (a pair has no
        power sensor of its own) and sets "negate" for GoodWe."""
        r = _battery_reader(BATTERY_PAIR)
        r._battery_brand = lambda bid, entity: "goodwe"
        assert r.read_power().battery_power == pytest.approx(1200.0)

    def test_a_one_sensor_battery_keeps_its_lock(self):
        """Unchanged, and proof the saved lock is read: it negates a
        one-sensor battery."""
        r = _battery_reader(BATTERY_ONE_SENSOR)
        r.restore_sign_state(SAVED_NEGATE)
        assert r.read_power().battery_power == pytest.approx(-1200.0)

    def test_back_on_one_sensor_the_lock_still_applies(self):
        """Review: the dialog moved to two sensors and back while SEM ran.
        The one sensor reads in the other sign, so its lock is needed."""
        from custom_components.solar_energy_management.ha_energy_reader import (
            _parse_prefs,
        )
        r = _battery_reader(BATTERY_ONE_SENSOR, one_sensor_w="-1200")
        r.restore_sign_state(SAVED_NEGATE)
        assert r.read_power().battery_power == pytest.approx(1200.0)
        for source in (BATTERY_PAIR, BATTERY_ONE_SENSOR):
            r.set_energy_dashboard_config(_parse_prefs(MagicMock(), {
                "energy_sources": [_SOLAR_SRC, _GRID_SRC, source],
                "device_consumption": []}, quiet=True))
            assert r.read_power().battery_power == pytest.approx(1200.0), source

    def test_the_card_says_the_pair_is_not_negated(self):
        from custom_components.solar_energy_management.coordinator.publish_diag import (
            build_diagnostics,
        )
        from .test_620_device_goal_model import TestPublishDiag625
        coord = TestPublishDiag625()._coord()
        for source, said in ((BATTERY_PAIR, "normal"),
                             (BATTERY_ONE_SENSOR, "negated")):
            r = _battery_reader(source)
            r.restore_sign_state(SAVED_NEGATE)
            r.read_power()
            coord._sensor_reader = r
            assert build_diagnostics(coord)["diag_battery_sign"] == said, source

    def test_the_user_flip_still_applies_to_the_pair(self):
        r = _battery_reader(BATTERY_PAIR, {"battery_sign_user_flip": True})
        assert r.read_power().battery_power == pytest.approx(-1200.0)


# ═══════════════════════════════════════════════════════════════════════
# Guard: every grid source the reads can name is a pair or a learned sign
# ═══════════════════════════════════════════════════════════════════════

def test_every_grid_source_kind_is_classed():
    """A new grid branch must say whether a lock applies to it. The kinds
    are read from the f-strings each branch tags its source with."""
    src = textwrap.dedent(inspect.getsource(sr))
    kinds = set()
    for node in ast.walk(ast.parse(src)):
        if not (isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Attribute) and t.attr == "_grid_source_key"
                for t in node.targets)):
            continue
        if isinstance(node.value, ast.JoinedStr):
            head = node.value.values[0]
            assert isinstance(head, ast.Constant), ast.dump(node.value)
            m = re.match(r"([a-z_]+):", head.value)
            assert m, head.value
            kinds.add(m.group(1))
    pair = set(SensorReader._PAIR_GRID_SOURCES)
    learned = {"combined", "sum"}            # a lock applies to these
    assert not pair & learned
    assert kinds == pair | learned, (
        f"classify {kinds - (pair | learned)} as a pair (_PAIR_GRID_SOURCES) "
        f"or a source whose sign is learned; gone: {(pair | learned) - kinds}")


def test_the_self_heal_asks_the_reader():
    """The stand-down sits in the self-heal itself, before it counts."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(
        SEMCoordinator._check_sign_flip)))
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute)
             and n.func.attr == "grid_read_is_pair"]
    assert calls, "_check_sign_flip no longer asks whether the grid is a pair"
