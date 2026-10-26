"""#1194 — a battery sensor marked "Inverted" in HA is read as it is.

HA's battery dialog offers Standard, Inverted and Two sensors. HA's own sign
for a battery is + while DISCHARGING (``BatterySourceType.stat_rate``:
"positive when discharging, negative when charging"); "Inverted" says the
sensor has the opposite sign, so it reads + while CHARGING. HA then makes its
own ``<sensor>_inverted`` sensor, the original × −1, for its graphs. SEM reads
the original, and SEM's sign is + while charging — so an Inverted sensor is
already in SEM's sign.

The one-battery path flipped it anyway (#551 took HA's sign for + while
charging), so a one-battery home read charge as discharge until the sign
check learned a lock on the flipped value. Homes with two or more batteries
read each sensor as it is, and so does the grid path. The issue was filed the
other way round (found while fixing #1171): the per-battery path was right.

Now every combined battery sensor is read as it is. A fleet lock saved while
the flip was still there judged the flipped value, so the first read after a
restore moves it once: the battery reads the same before and after the update.
"""
from __future__ import annotations

import ast
import logging
import pathlib
from unittest.mock import MagicMock

import pytest

from custom_components.solar_energy_management.coordinator.sensor_reader import (
    SensorReader,
)
from custom_components.solar_energy_management.ha_energy_reader import _parse_prefs

ROOT = pathlib.Path(__file__).resolve().parents[1]
FLEET = "__fleet__"

SOLAR = {"type": "solar", "stat_energy_from": "sensor.pv_energy",
         "stat_rate": "sensor.pv_power"}
GRID = {"type": "grid", "flow_from": [{"stat_energy_from": "sensor.grid_import"}],
        "flow_to": [{"stat_energy_to": "sensor.grid_export"}],
        "stat_rate": "sensor.grid_power"}


def _inverted(n):
    """HA's stored shape: ``stat_rate`` is HA's own ``_inverted`` sensor, the
    user's sensor sits in ``power_config.stat_rate_inverted``."""
    return {"type": "battery",
            "stat_energy_from": f"sensor.batt{n}_discharge_energy",
            "stat_energy_to": f"sensor.batt{n}_charge_energy",
            "stat_rate": f"sensor.batt{n}_power_inverted",
            "power_config": {"stat_rate_inverted": f"sensor.batt{n}_power"}}


def _standard(n):
    return {"type": "battery",
            "stat_energy_from": f"sensor.batt{n}_discharge_energy",
            "stat_energy_to": f"sensor.batt{n}_charge_energy",
            "stat_rate": f"sensor.batt{n}_power",
            "power_config": {"stat_rate": f"sensor.batt{n}_power"}}


def _ed(batteries):
    prefs = {"energy_sources": [SOLAR, GRID, *batteries], "device_consumption": []}
    return _parse_prefs(MagicMock(), prefs, quiet=True)


def _reader(batteries, watts, options=None):
    """The real reader on HA's real prefs layout. ``watts`` per battery
    number; the energy counters have no state, so no sign is learned."""
    states = {"sensor.pv_power": "3000", "sensor.grid_power": "0"}
    for n, w in watts.items():
        states[f"sensor.batt{n}_power"] = str(w)
        # HA's own sensor reads the other way; SEM must not read it
        states[f"sensor.batt{n}_power_inverted"] = str(-w)
    hass = MagicMock()
    hass.states.get = lambda eid: (
        MagicMock(state=states[eid], attributes={"unit_of_measurement": "W"})
        if eid in states else None)
    reader = SensorReader(hass, dict(options or {}))
    reader._sign_vote_warmup = 0
    reader.set_energy_dashboard_config(_ed(batteries))
    return reader


# ── Read as is ─────────────────────────────────────────────────────────────

def test_the_parser_keeps_the_users_sensor_not_has_inverted_copy():
    ed = _ed([_inverted(1)])
    assert ed.battery_power == "sensor.batt1_power"
    assert ed.battery_power_inverted is True


def test_one_inverted_battery_that_charges_reads_charging():
    """+1200 W on an Inverted sensor is 1200 W of charge (the old flip read
    it as 1200 W of discharge)."""
    readings = _reader([_inverted(1)], {1: 1200}).read_power()
    assert readings.battery_power == pytest.approx(1200.0)
    assert readings.battery_charge_power == pytest.approx(1200.0)
    assert readings.battery_discharge_power == pytest.approx(0.0)


@pytest.mark.parametrize("w", [1200, -800])
def test_one_battery_and_two_batteries_read_a_sensor_the_same(w):
    """The two paths used to disagree on the same sensor. b1 of a two-battery
    home and the one battery of a one-battery home read the same."""
    alone = _reader([_inverted(1)], {1: w}).read_power()
    fleet = _reader([_inverted(1), _standard(2)], {1: w, 2: 500}).read_power()
    assert alone.battery_power == pytest.approx(float(w))
    assert fleet.batteries["b1"].power_w == pytest.approx(float(w))
    assert fleet.batteries["b2"].power_w == pytest.approx(500.0)
    assert fleet.battery_power == pytest.approx(float(w) + 500.0)


def test_a_standard_battery_is_read_as_is_too():
    """The twin: nothing changes for a Standard sensor."""
    assert _reader([_standard(1)], {1: 1200}).read_power().battery_power == \
        pytest.approx(1200.0)


# ── A lock saved while the flip was there ──────────────────────────────────

def _old_value(raw, lock):
    """What the old code showed: the flip, then the saved lock."""
    v = -raw
    return -v if lock else v


@pytest.mark.parametrize("lock", [True, False])
@pytest.mark.parametrize("raw", [1200, -800])
def test_a_saved_lock_reads_the_same_after_the_update(lock, raw, caplog):
    reader = _reader([_inverted(1)], {1: raw})
    reader.restore_sign_state({"battery_detected": {FLEET: True},
                               "battery_inverted": {FLEET: lock}})
    with caplog.at_level(logging.INFO):
        first = reader.read_power().battery_power
    assert first == pytest.approx(float(_old_value(raw, lock)))
    assert reader._battery_sign_inverted[FLEET] is (not lock)
    assert reader._battery_sign_detected[FLEET] is True
    assert "#1194" in caplog.text
    # once: the next read keeps it
    assert reader.read_power().battery_power == pytest.approx(first)
    assert reader._battery_sign_inverted[FLEET] is (not lock)


def test_the_moved_lock_is_saved_and_not_moved_again():
    reader = _reader([_inverted(1)], {1: 1200})
    reader.restore_sign_state({"battery_detected": {FLEET: True},
                               "battery_inverted": {FLEET: True}})
    reader.read_power()
    saved = reader.export_sign_state()
    assert saved["battery_basis"] == "sensor"
    assert saved["battery_inverted"][FLEET] is False

    again = _reader([_inverted(1)], {1: 1200})
    again.restore_sign_state(saved)
    assert again.read_power().battery_power == pytest.approx(1200.0)
    assert again._battery_sign_inverted[FLEET] is False


def test_a_save_before_the_first_read_keeps_the_old_shape():
    """A restart before the first read must still move the lock next time."""
    reader = _reader([_inverted(1)], {1: 1200})
    reader.restore_sign_state({"battery_detected": {FLEET: True},
                               "battery_inverted": {FLEET: True}})
    early = reader.export_sign_state()
    assert "battery_basis" not in early

    after_restart = _reader([_inverted(1)], {1: 1200})
    after_restart.restore_sign_state(early)
    assert after_restart.read_power().battery_power == pytest.approx(1200.0)
    assert after_restart._battery_sign_inverted[FLEET] is False
    assert after_restart.export_sign_state()["battery_basis"] == "sensor"


def test_a_fresh_reader_saves_the_new_shape():
    assert SensorReader(MagicMock(), {}).export_sign_state()["battery_basis"] == "sensor"


@pytest.mark.parametrize("case", ["new_shape", "other_marker", "standard",
                                  "override", "per_battery"])
def test_locks_the_flip_never_touched_are_kept(case):
    """Only the one-battery fleet lock on an Inverted sensor judged a flipped
    value. Every other lock judged the sensor as it is and stays."""
    options, batteries, watts = {}, [_inverted(1)], {1: 1200}
    state = {"battery_detected": {FLEET: True}, "battery_inverted": {FLEET: True}}
    if case == "new_shape":
        state["battery_basis"] = "sensor"
    elif case == "other_marker":
        # a later store shape is not an old one
        state["battery_basis"] = "v2"
    elif case == "standard":
        batteries = [_standard(1)]
    elif case == "override":
        # SEM's own battery power pick was never flipped
        options = {"battery_power_sensor": "sensor.batt9_power"}
        watts = {1: 1200, 9: 1200}
    elif case == "per_battery":
        # each battery's lock judged its sensor as it is; the fleet lock from
        # the one-battery days is not used while the home reads per battery
        batteries, watts = [_inverted(1), _standard(2)], {1: 1200, 2: 500}
        state = {"battery_detected": {"b1": True, "b2": True, FLEET: True},
                 "battery_inverted": {"b1": True, "b2": False, FLEET: True}}
    reader = _reader(batteries, watts, options)
    reader.restore_sign_state(state)
    before = dict(reader._battery_sign_inverted)
    assert FLEET in before
    reader.read_power()
    reader.read_power()
    assert reader._battery_sign_inverted == before
    assert all(reader._battery_sign_detected[bid] for bid in before)


def test_the_fleet_lock_waits_while_the_home_reads_per_battery():
    """A home that went from one battery to two keeps its old fleet lock for
    the day it goes back to one; the move happens then."""
    reader = _reader([_inverted(1), _standard(2)], {1: 1200, 2: 500})
    reader.restore_sign_state({"battery_detected": {FLEET: True},
                               "battery_inverted": {FLEET: True}})
    reader.read_power()
    assert reader._battery_sign_inverted[FLEET] is True
    assert "battery_basis" not in reader.export_sign_state()

    reader.set_energy_dashboard_config(_ed([_inverted(1)]))
    assert reader.read_power().battery_power == pytest.approx(
        float(_old_value(1200, True)))
    assert reader._battery_sign_inverted[FLEET] is False
    assert reader.export_sign_state()["battery_basis"] == "sensor"


@pytest.mark.parametrize("flip", [False, True])
def test_a_reset_leaves_nothing_to_move(flip):
    reader = _reader([_inverted(1)], {1: 1200}, {"battery_sign_user_flip": flip})
    reader.restore_sign_state({"battery_detected": {FLEET: True},
                               "battery_inverted": {FLEET: True}})
    reader.reset_sign_state()
    assert reader.read_power().battery_power == pytest.approx(-1200.0 if flip else 1200.0)
    assert not reader._battery_sign_detected.get(FLEET)
    assert not reader._battery_sign_inverted.get(FLEET)
    assert reader.export_sign_state()["battery_basis"] == "sensor"


# ── The one-tap flip, with no lock saved ───────────────────────────────────

OLD_STORE_NO_LOCK = {"grid_detected": False, "grid_inverted": False,
                     "battery_detected": {}, "battery_inverted": {}}


def _inverted_no_counters(n):
    return {"type": "battery",
            "power_config": {"stat_rate_inverted": f"sensor.batt{n}_power"}}


@pytest.mark.parametrize("counters", [True, False])
@pytest.mark.parametrize("brand", [None, "goodwe", "huawei_solar"])
@pytest.mark.parametrize("raw", [1200, -800])
def test_a_flip_set_against_the_old_read_reads_the_same(brand, raw, counters):
    """The user's "Fix battery sign" corrected the flipped read. With no lock
    to turn over, SEM locks the old read's sign on the sensor as it is: the
    brand seed, which the old sign check asked only with both energy
    counters set, else none. The user sees what they saw, also after a
    restart."""
    seeded = brand == "goodwe" and counters     # the table negates goodwe
    battery = _inverted(1) if counters else _inverted_no_counters(1)

    def make():
        r = _reader([battery], {1: raw}, {"battery_sign_user_flip": True})
        r._entity_owner = lambda eid: (brand, None)
        return r

    old = -(raw if seeded else -raw)    # flip, brand seed, then the user flip
    reader = make()
    reader.restore_sign_state(dict(OLD_STORE_NO_LOCK))
    assert reader.read_power().battery_power == pytest.approx(float(old))
    assert reader._battery_sign_detected[FLEET] is True
    saved = reader.export_sign_state()
    assert saved["battery_basis"] == "sensor"

    again = make()
    again.restore_sign_state(saved)
    assert again.read_power().battery_power == pytest.approx(float(old))


@pytest.mark.parametrize("case", ["no_flip", "no_store", "new_store",
                                  "per_battery_first"])
def test_no_lock_is_made_without_an_old_flip(case):
    """No flip set against the old read: nothing is kept, and the battery
    reads right from the start. ``{}`` is no store (or a reset). A flip on a
    home that read per battery at the update judged each sensor as it is."""
    options = {"battery_sign_user_flip": case != "no_flip"}
    state = {"no_flip": dict(OLD_STORE_NO_LOCK), "no_store": {},
             "new_store": {**OLD_STORE_NO_LOCK, "battery_basis": "sensor"},
             "per_battery_first": dict(OLD_STORE_NO_LOCK)}[case]
    if case == "per_battery_first":
        reader = _reader([_inverted(1), _standard(2)], {1: 1200, 2: 500}, options)
        reader.restore_sign_state(state)
        reader.read_power()
        reader.set_energy_dashboard_config(_ed([_inverted(1)]))
    else:
        reader = _reader([_inverted(1)], {1: 1200}, options)
        reader.restore_sign_state(state)
    reader.read_power()
    assert not reader._battery_sign_detected.get(FLEET)
    assert not reader._battery_sign_inverted.get(FLEET)


# ── Guard ──────────────────────────────────────────────────────────────────

def _readers_of_the_flag(source: str) -> set:
    found = set()
    for fn in ast.walk(ast.parse(source)):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(fn):
            if isinstance(node, ast.Attribute) and node.attr == "battery_power_inverted":
                found.add(fn.name)
            # getattr(ed, "battery_power_inverted", …) too
            if isinstance(node, ast.Constant) and node.value == "battery_power_inverted":
                found.add(fn.name)
    return found


def test_only_the_lock_move_reads_the_inverted_flag():
    """A sensor marked Inverted is already in SEM's sign. No read may flip on
    the flag again; its one reader moves a lock saved before #1194."""
    found = set()
    for path in ROOT.rglob("*.py"):
        if "tests" in path.parts or "__pycache__" in path.parts:
            continue
        found |= {f"{path.relative_to(ROOT)}::{f}"
                  for f in _readers_of_the_flag(path.read_text())}
    assert found == {"ha_energy_reader.py::_extract_battery_config",
                     "coordinator/sensor_reader.py::_move_lock_learned_on_flip"}


def test_the_guard_finds_the_old_flip():
    old = ("def _read_from_energy_dashboard(self):\n"
           "    raw = self._read_sensor(ed.battery_power, 'battery')\n"
           "    if ed.battery_power_inverted:\n"
           "        raw = -raw\n"
           "def _read_battery(self):\n"
           "    if getattr(ed, 'battery_power_inverted', False):\n"
           "        raw = -raw\n")
    assert _readers_of_the_flag(old) == {"_read_from_energy_dashboard",
                                         "_read_battery"}
