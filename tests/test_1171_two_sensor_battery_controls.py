"""#1171 — a battery with two power sensors gets its own sensors and controls.

HA's battery dialog has a "Two sensors" option: one charge and one discharge
power sensor, no combined one. The reader keeps those batteries in
``battery_power_pairs`` and reads each on its own (#553), but the sensor
platform and the control discovery counted ``battery_power_list`` only. A home
of two such batteries got no ``sensor.sem_battery_b<N>_*`` and no per-battery
Mode select: one shared select drove both, so Force charge on one waited for
the other (found while fixing #1167). A home with one of each was hit too.

Now one list, ``ha_energy_reader.battery_units``, is every battery count and
slug list. The real-HA tests run SEM's real setup on a real registry with the
Energy Dashboard config parsed from HA's own prefs layout.
"""
from __future__ import annotations

import ast
import importlib.util
import pathlib
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.solar_energy_management.ha_energy_reader import (
    _parse_prefs, battery_units,
)

ROOT = pathlib.Path(__file__).resolve().parents[1]
COORDINATOR_MODULE = "custom_components.solar_energy_management.coordinator.coordinator"

_HAS_PHACC = importlib.util.find_spec("pytest_homeassistant_custom_component") is not None
real_ha = pytest.mark.skipif(
    not _HAS_PHACC,
    reason="pytest-homeassistant-custom-component not installed; CI runs these",
)

SOLAR = {"type": "solar", "stat_energy_from": "sensor.pv_energy",
         "stat_rate": "sensor.pv_power"}
GRID = {"type": "grid", "flow_from": [{"stat_energy_from": "sensor.grid_import"}],
        "flow_to": [{"stat_energy_to": "sensor.grid_export"}],
        "stat_rate": "sensor.grid_power"}


def _pair(n):
    return {"type": "battery",
            "stat_energy_from": f"sensor.verto{n}_discharge_energy",
            "stat_energy_to": f"sensor.verto{n}_charge_energy",
            "power_config": {"stat_rate_from": f"sensor.verto{n}_discharge_power",
                             "stat_rate_to": f"sensor.verto{n}_charge_power"}}


def _combined(n):
    return {"type": "battery",
            "stat_energy_from": f"sensor.sessy{n}_discharge_energy",
            "stat_energy_to": f"sensor.sessy{n}_charge_energy",
            "stat_rate": f"sensor.sessy{n}_power"}


# The pair is listed FIRST in the mixed home: the reader still numbers the
# combined battery b1, so the order is part of what is checked.
HOMES = {
    "two_pairs": [_pair(1), _pair(2)],
    "pair_and_combined": [_pair(1), _combined(1)],
}
UNITS = {
    "two_pairs": ["sensor.verto1_charge_power", "sensor.verto2_charge_power"],
    "pair_and_combined": ["sensor.sessy1_power", "sensor.verto1_charge_power"],
}
# Watts per battery unit, in slug order: b1 charges 1200 W, b2 discharges 700 W.
STATES = {
    "sensor.verto1_charge_power": "1200", "sensor.verto1_discharge_power": "0",
    "sensor.verto2_charge_power": "0", "sensor.verto2_discharge_power": "700",
    "sensor.sessy1_power": "1200",
}
WATTS = {
    "two_pairs": {"b1": 1200.0, "b2": -700.0},
    # the pair is b2 here, and charges
    "pair_and_combined": {"b1": 1200.0, "b2": 1200.0},
}


def _prefs(batteries):
    return {"energy_sources": [SOLAR, GRID, *batteries], "device_consumption": []}


def _config(batteries):
    return _parse_prefs(MagicMock(), _prefs(batteries), quiet=True)


# ── The list ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("home", list(HOMES))
def test_every_battery_is_a_unit_in_the_readers_order(home):
    assert battery_units(_config(HOMES[home])) == UNITS[home]


def test_one_battery_of_either_kind_is_one_unit():
    assert battery_units(_config([_pair(1)])) == ["sensor.verto1_charge_power"]
    assert battery_units(_config([_combined(1)])) == ["sensor.sessy1_power"]
    assert battery_units(_config([])) == []
    assert battery_units(None) == []


@pytest.mark.parametrize("home", list(HOMES))
def test_the_list_names_the_batteries_the_reader_reads(home):
    """The slugs the platforms build from must be the reader's own: b<N> of
    the list is the battery the reader published as b<N>."""
    from custom_components.solar_energy_management.coordinator.sensor_reader import (
        SensorReader,
    )
    hass = MagicMock()
    hass.states.get = lambda eid: (
        MagicMock(state=STATES[eid], attributes={"unit_of_measurement": "W"})
        if eid in STATES else None)
    ed = _config(HOMES[home])
    reader = SensorReader(hass, {})
    reader._sign_vote_warmup = 0
    reader.set_energy_dashboard_config(ed)
    readings = reader.read_power()

    units = battery_units(ed)
    assert list(readings.batteries) == [f"b{i + 1}" for i in range(len(units))]
    assert [bp.name for bp in readings.batteries.values()] == units
    assert {b: bp.power_w for b, bp in readings.batteries.items()} == WATTS[home]


# ── Real setup ─────────────────────────────────────────────────────────────

MINIMAL_DATA = {
    "grid_power_sensor": "sensor.grid_power",
    "solar_power_sensor": "sensor.pv_power",
    "min_solar_power": 1000,
    "peak_load": 6000,
    "update_interval": 30,
    "electricity_import_rate": 0.30,
    "electricity_export_rate": 0.08,
}
PER_BATTERY_SENSORS = ("power", "soc", "status", "capacity_kwh")


async def _setup(hass, monkeypatch, batteries):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.solar_energy_management.const import DOMAIN
    for eid, state in {**STATES, "sensor.pv_power": "3000",
                       "sensor.grid_power": "0"}.items():
        hass.states.async_set(eid, state, {"unit_of_measurement": "W"})
    ed = _parse_prefs(hass, _prefs(batteries), quiet=True)
    monkeypatch.setattr(f"{COORDINATOR_MODULE}.read_energy_dashboard_config_outcome",
                        AsyncMock(return_value=(ed, True)))
    entry = MockConfigEntry(domain=DOMAIN, version=12, minor_version=1,
                            data=dict(MINIMAL_DATA), options={},
                            title="SEM #1171")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _entity(hass, platform, unique_id):
    from homeassistant.helpers import entity_registry as er

    from custom_components.solar_energy_management.const import DOMAIN
    return er.async_get(hass).async_get_entity_id(platform, DOMAIN, unique_id)


@real_ha
@pytest.mark.asyncio
@pytest.mark.parametrize("home", list(HOMES))
async def test_each_battery_gets_its_own_sensors_and_mode(
        hass, enable_custom_integrations, monkeypatch, home):
    entry = await _setup(hass, monkeypatch, HOMES[home])
    coordinator = entry.runtime_data
    assert coordinator.battery_control_slugs == ("b1", "b2")

    for bid in ("b1", "b2"):
        for key in PER_BATTERY_SENSORS:
            assert _entity(hass, "sensor", f"sem_battery_{bid}_{key}"), (bid, key)
        assert _entity(hass, "select", f"{entry.entry_id}_battery_{bid}_mode"), bid
        assert _entity(hass, "number", f"{entry.entry_id}_battery_{bid}_reserve_soc"), bid
    # One select per battery replaces the shared one.
    assert _entity(hass, "select", f"{entry.entry_id}_battery_mode") is None

    # Each battery's own power, the pair's charge minus its discharge.
    for bid, watts in WATTS[home].items():
        state = hass.states.get(_entity(hass, "sensor", f"sem_battery_{bid}_power"))
        assert float(state.state) == pytest.approx(watts), bid

    # Force charge on b2 drives b2 only — the #1171 symptom was one shared
    # mode that drove (and waited on) both.
    await hass.services.async_call(
        "select", "select_option",
        {"entity_id": _entity(hass, "select", f"{entry.entry_id}_battery_b2_mode"),
         "option": "force_charge"},
        blocking=True)
    assert coordinator._per_battery_config(1, 2)["battery_mode"] == "force_charge"
    assert coordinator._per_battery_config(0, 2).get("battery_mode", "auto") == "auto"
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


@real_ha
@pytest.mark.asyncio
async def test_one_two_sensor_battery_keeps_the_one_shared_mode(
        hass, enable_custom_integrations, monkeypatch):
    """The twin: one battery is still one battery — no b1 surface, the one
    global select (on the old code the two-pair home looked like this)."""
    entry = await _setup(hass, monkeypatch, [_pair(1)])
    assert entry.runtime_data.battery_control_slugs == ()
    assert _entity(hass, "select", f"{entry.entry_id}_battery_mode")
    assert _entity(hass, "select", f"{entry.entry_id}_battery_b1_mode") is None
    assert _entity(hass, "sensor", "sem_battery_b1_power") is None
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


# ── Guard: who may read the combined list on its own ───────────────────────

# Every function in the package that reads ``battery_power_list`` itself, and
# why the combined sensors alone are right there. A count of the batteries or
# a slug list is ``battery_units``; a new reader fails until it is sorted.
DECLARED_READERS = {
    ("ha_energy_reader.py", "battery_units"):
        "the list itself",
    ("ha_energy_reader.py", "_extract_battery_config"):
        "the parser that fills it",
    ("ha_energy_reader.py", "_derive_battery_power_if_unrepresented"):
        "the derive adds to it",
    ("coordinator/sensor_reader.py", "set_energy_dashboard_config"):
        "sign audits exist for combined units only (pairs are never sign-checked)",
    ("coordinator/sensor_reader.py", "read_power"):
        "sign detection runs on combined units only; pairs follow as b<N+k>",
    ("coordinator/sensor_reader.py", "battery_sign_diagnostics"):
        "the sign payload names a combined sensor",
    ("coordinator/sensor_reader.py", "_read_from_energy_dashboard"):
        "the read loop that numbers combined units first",
    ("coordinator/sensor_reader.py", "_audit_battery_sign_lock"):
        "the #647 audit checks combined units only",
    ("config_flow.py", "async_step_user"):
        "the setup summary lists each combined sensor beside the pairs",
    ("diagnostics.py", "async_get_config_entry_diagnostics"):
        "the raw list, dumped beside the pairs",
    ("coordinator/coordinator.py", "_configured_entity_ids"):
        "entities in use, read beside the pairs",
}


def _readers_in(rel, source):
    found = set()
    for func in ast.walk(ast.parse(source)):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(func):
            named = (
                (isinstance(node, ast.Attribute) and node.attr == "battery_power_list")
                or (isinstance(node, ast.Constant) and node.value == "battery_power_list"))
            if named:
                found.add((rel, func.name))
    return found


def _readers():
    found = set()
    for path in ROOT.rglob("*.py"):
        rel = path.relative_to(ROOT).as_posix()
        if not rel.startswith("tests/"):
            found |= _readers_in(rel, path.read_text(encoding="utf-8"))
    return found


def test_every_reader_of_the_combined_list_is_declared():
    readers = _readers()
    new = readers - set(DECLARED_READERS)
    assert not new, f"count batteries with battery_units, or declare why not: {sorted(new)}"
    stale = set(DECLARED_READERS) - readers
    assert not stale, f"declared but no longer a reader: {sorted(stale)}"


# The two #1171 counts, as they were.
OLD_SENSOR = (
    "async def async_setup_entry(hass, entry, add):\n"
    "    _ed = getattr(_sr, '_energy_dashboard_config', None)\n"
    "    batt_list = list(getattr(_ed, 'battery_power_list', []) or [])\n")
OLD_CONTROLS = (
    "def discover_battery_control_slugs(coordinator):\n"
    "    batt_list = list(getattr(ed, 'battery_power_list', []) or [])\n")


def test_the_guard_bites_on_the_old_counts():
    old = (_readers_in("sensor.py", OLD_SENSOR)
           | _readers_in("coordinator/battery_controls.py", OLD_CONTROLS))
    assert old == {("sensor.py", "async_setup_entry"),
                   ("coordinator/battery_controls.py", "discover_battery_control_slugs")}
    assert not old & set(DECLARED_READERS)


# ── Siblings: what SEM says it reads ───────────────────────────────────────

def test_both_sensors_of_a_pair_count_as_in_use():
    """The Config card proposes no entity SEM already reads (#915); a
    two-sensor battery is read on both of its sensors."""
    from types import SimpleNamespace

    from custom_components.solar_energy_management.coordinator.coordinator import (
        SEMCoordinator,
    )
    coord = SEMCoordinator.__new__(SEMCoordinator)
    coord.config = {}
    coord._sensor_reader = SimpleNamespace(
        config=SimpleNamespace(), _energy_dashboard_config=_config(HOMES["two_pairs"]))
    used = coord._configured_entity_ids()
    for n in (1, 2):
        assert {f"sensor.verto{n}_charge_power",
                f"sensor.verto{n}_discharge_power"} <= used


@pytest.mark.asyncio
async def test_the_diagnostics_list_and_read_each_pair():
    from custom_components.solar_energy_management.diagnostics import (
        async_get_config_entry_diagnostics,
    )
    ed = _config(HOMES["pair_and_combined"])
    coordinator = MagicMock()
    coordinator._energy_dashboard_config = ed
    entry = MagicMock()
    entry.entry_id = "e1171"
    entry.data, entry.options = {}, {}
    entry.runtime_data = coordinator
    hass = MagicMock()
    hass.data = {"solar_energy_management": {"e1171": coordinator}}
    hass.states.get = lambda eid: (
        MagicMock(state=STATES[eid], attributes={"unit_of_measurement": "W"})
        if eid in STATES else None)

    result = await async_get_config_entry_diagnostics(hass, entry)

    block = result["energy_dashboard"]
    assert block["per_source_lists"]["battery_power_pairs"] == [
        ["sensor.verto1_discharge_power", "sensor.verto1_charge_power"]]
    battery = block["per_source_readings"]["battery"]
    assert battery["sensor.verto1_charge_power"]["state"] == "1200"
    assert battery["sensor.verto1_discharge_power"]["state"] == "0"
    assert battery["sensor.sessy1_power"]["state"] == "1200"
