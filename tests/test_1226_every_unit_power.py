"""#1226 — a second inverter or battery with no power sensor read 0 W.

A home with two inverters (or two batteries) in the Energy Dashboard, where
one has kWh counters and no power sensor: SEM looked for a power sensor on
the counter's device only when NO unit had one (``_derive_missing_power_
sensors`` asked the scalar ``solar_power`` / ``battery_power``, which hold
the first unit). So solar (or battery) power was the other unit alone, and
no Repair said so (#1188 reads the same scalar).

Now each source is asked on its own (``_derive_unit_power``). A power sensor
found for it joins the unit list the reader sums; a source with none is kept
in ``unsourced_counters`` and named in the ``power_sensor_not_found`` Repair.
A source whose counter sits on a device SEM already reads power from is left
alone: that device's sensor may hold it too, and two would count it twice.
"""
from __future__ import annotations

import itertools
import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.solar_energy_management.const import DOMAIN
from custom_components.solar_energy_management.coordinator import (
    repair_issues as ri,
)
from custom_components.solar_energy_management.coordinator.sensor_reader import (
    SensorReader,
)
from custom_components.solar_energy_management.coordinator.battery_controls import (
    BATTERY_SLOT_UNITS_KEY,
    realign_battery_slots,
)
from custom_components.solar_energy_management.ha_energy_reader import (
    _parse_prefs,
    battery_units,
    battery_units_before_derive,
    unit_energy_counters,
    unsourced_counters,
    yield_found_units_to_picks,
)

MONO = "time.monotonic"
WAIT = ri.UNAVAILABLE_REPAIR_THRESHOLD_S

_W = {"device_class": "power", "unit_of_measurement": "W",
      "state_class": "measurement"}
_KWH = {"device_class": "energy", "unit_of_measurement": "kWh",
        "state_class": "total_increasing"}


def _device(hass, name):
    entry = MockConfigEntry(domain="test_inverter", unique_id=name)
    entry.add_to_hass(hass)
    dev = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("test_inverter", name)}, name=name)
    return entry, dev


def _entity(hass, entry, dev, entity_id, dc, value):
    """Registered on ``dev``; ``value`` None leaves it with no state yet
    (its integration has not loaded: a cold start)."""
    domain, object_id = entity_id.split(".", 1)
    er.async_get(hass).async_get_or_create(
        domain, "test_inverter", f"{entry.unique_id}_{object_id}",
        suggested_object_id=object_id, config_entry=entry, device_id=dev.id,
        original_device_class=dc)
    if value is not None:
        hass.states.async_set(entity_id, str(value),
                              _W if dc == "power" else _KWH)


def _inverter(hass, name, watts=None, *, extra=()):
    """An inverter device: its yield counter and, when ``watts`` is given,
    its ``<name>_pv_power`` sensor. Returns (counter, power)."""
    entry, dev = _device(hass, name)
    counter, power = f"sensor.{name}_total_yield", f"sensor.{name}_pv_power"
    _entity(hass, entry, dev, counter, "energy", 1000)
    if watts is not None:
        _entity(hass, entry, dev, power, "power", watts)
    for eid, dc, value in extra:
        _entity(hass, entry, dev, eid, dc, value)
    return counter, power


def _battery(hass, name, watts=None, *, loaded=True):
    """A battery device: charge/discharge counters and, when ``watts`` is
    given, its combined ``<name>_battery_power`` sensor (+ = charging).
    ``loaded`` False: registered, no states yet (a cold start)."""
    entry, dev = _device(hass, name)
    charge, discharge = f"sensor.{name}_charged", f"sensor.{name}_discharged"
    power = f"sensor.{name}_battery_power"
    _entity(hass, entry, dev, charge, "energy", 50 if loaded else None)
    _entity(hass, entry, dev, discharge, "energy", 40 if loaded else None)
    if watts is not None:
        _entity(hass, entry, dev, power, "power", watts if loaded else None)
    return (charge, discharge), power


GRID = {"type": "grid",
        "flow_from": [{"stat_energy_from": "sensor.grid_import"}],
        "flow_to": [{"stat_energy_to": "sensor.grid_export"}],
        "power": [{"stat_rate": "sensor.grid_power"}]}


def _prefs(*sources):
    return {"energy_sources": [GRID, *sources], "device_consumption": []}


def _solar(counter, power=None):
    src = {"type": "solar", "stat_energy_from": counter}
    if power:
        src["stat_rate"] = power
    return src


def _batt(counters, power=None, pair=None):
    src = {"type": "battery", "stat_energy_to": counters[0],
           "stat_energy_from": counters[1]}
    if power:
        src["stat_rate"] = power
    if pair:
        src["power_config"] = {"stat_rate_from": pair[0], "stat_rate_to": pair[1]}
    return src


def _reader(hass, ed, **config) -> SensorReader:
    hass.states.async_set("sensor.grid_power", "0", _W)
    r = SensorReader(hass, {"update_interval": 10, **config})
    r._sign_vote_warmup = 0
    r.set_energy_dashboard_config(ed)
    return r


def _cycles(r, t0, t1, *, step=10.0):
    t, readings = t0, None
    while t <= t1:
        with patch(MONO, return_value=t):
            readings = r.read_power()
            r.report_unsourced_power(waiting=False)
        t += step
    return readings


def _issue(hass, source):
    return ir.async_get(hass).async_get_issue(
        DOMAIN, f"power_sensor_not_found_{source}")


# ── 1. a second inverter's power is found and read ──────────────────────


async def test_the_second_inverter_is_found_and_summed(hass):
    """The issue: inverter A has a power link, B only its counter. B's
    power sensor sits on its own device and is now found and read."""
    a_counter, a_power = _inverter(hass, "inv_a", 3000)
    b_counter, b_power = _inverter(hass, "inv_b", 2000)
    ed = _parse_prefs(hass, _prefs(_solar(a_counter, a_power),
                                   _solar(b_counter)), quiet=True)

    assert ed.solar_power_list == [a_power, b_power]
    assert ed.solar_power == a_power
    assert ed.derived_units == {b_power: b_counter}
    # the scalar is A's own link, so "solar" power is not called derived
    assert "solar" not in ed.derived_power
    assert unsourced_counters(ed, "solar") == []
    assert ed.power_resolution_incomplete() is False

    readings = _cycles(_reader(hass, ed), 0.0, WAIT + 20)
    assert readings.solar_power == pytest.approx(5000)
    assert set(readings.inverters) == {a_power, b_power}
    assert _issue(hass, "solar") is None


async def test_the_first_inverter_is_found_when_the_second_has_the_link(hass):
    a_counter, a_power = _inverter(hass, "inv_a", 3000)
    b_counter, b_power = _inverter(hass, "inv_b", 2000)
    ed = _parse_prefs(hass, _prefs(_solar(a_counter), _solar(b_counter, b_power)),
                      quiet=True)
    assert ed.solar_power_list == [b_power, a_power]
    assert ed.solar_power == b_power
    readings = _cycles(_reader(hass, ed), 0.0, 0.0)
    assert readings.solar_power == pytest.approx(5000)


async def test_with_no_link_at_all_every_inverter_is_found(hass):
    """Before, only the first source was searched even here."""
    a_counter, a_power = _inverter(hass, "inv_a", 3000)
    b_counter, b_power = _inverter(hass, "inv_b", 2000)
    ed = _parse_prefs(hass, _prefs(_solar(a_counter), _solar(b_counter)),
                      quiet=True)
    assert ed.solar_power_list == [a_power, b_power]
    assert ed.solar_power == a_power
    assert ed.derived_power["solar"] == a_power      # the scalar IS derived
    assert ed.derived_units == {a_power: a_counter, b_power: b_counter}


# ── 2. a second battery the same way ────────────────────────────────────


async def test_the_second_battery_is_found_with_its_own_counters(hass):
    a_counters, a_power = _battery(hass, "pack_a", 1500)
    b_counters, b_power = _battery(hass, "pack_b", 700)
    ed = _parse_prefs(hass, _prefs(_batt(a_counters, a_power), _batt(b_counters)),
                      quiet=True)

    assert battery_units(ed) == [a_power, b_power]
    assert ed.battery_power == a_power
    assert "battery" not in ed.derived_power
    assert unit_energy_counters(ed, b_power) == b_counters
    assert unit_energy_counters(ed, a_power) == a_counters

    readings = _cycles(_reader(hass, ed), 0.0, 0.0)
    assert set(readings.batteries) == {"b1", "b2"}
    assert readings.batteries["b2"].name == b_power
    assert readings.batteries["b2"].power_read is True
    assert abs(readings.battery_power) == pytest.approx(2200)


async def test_a_battery_beside_a_two_sensor_battery_is_found(hass):
    """Before, any pair stopped the derive for every battery (#761's guard
    was asked of the house, not of the source)."""
    entry, dev = _device(hass, "verto")
    pair = ("sensor.verto_discharge_power", "sensor.verto_charge_power")
    for eid in pair:
        _entity(hass, entry, dev, eid, "power", 0)
    _entity(hass, entry, dev, "sensor.verto_battery_power", "power", 0)
    _entity(hass, entry, dev, "sensor.verto_charged", "energy", 5)
    _entity(hass, entry, dev, "sensor.verto_discharged", "energy", 4)
    b_counters, b_power = _battery(hass, "pack_b", 700)
    ed = _parse_prefs(hass, _prefs(
        _batt(("sensor.verto_charged", "sensor.verto_discharged"), pair=pair),
        _batt(b_counters)), quiet=True)
    assert battery_units(ed) == [b_power, pair[1]]
    # the pair's own device was not asked: no combined sensor beside it (#761)
    assert "sensor.verto_battery_power" not in battery_units(ed)


# ── 3. nothing found: the reader names it ───────────────────────────────


async def test_a_second_inverter_with_no_power_sensor_gets_the_repair(hass):
    a_counter, a_power = _inverter(hass, "inv_a", 3000)
    b_counter, _ = _inverter(hass, "inv_b")              # counter only
    ed = _parse_prefs(hass, _prefs(_solar(a_counter, a_power),
                                   _solar(b_counter)), quiet=True)
    assert unsourced_counters(ed, "solar") == [b_counter]
    assert ed.power_resolution_incomplete() is True      # the #274 retry

    r = _reader(hass, ed)
    _cycles(r, 0.0, WAIT - 10)
    assert _issue(hass, "solar") is None, "raised before the wait was over"
    readings = _cycles(r, WAIT, WAIT + 20)
    assert readings.solar_power == pytest.approx(3000)
    issue = _issue(hass, "solar")
    assert issue is not None
    assert issue.translation_placeholders == {"energy_entity": b_counter}
    # its own text: the Config card pick would drop inverter A
    assert issue.translation_key == "power_sensor_not_found_unit"


async def test_a_second_battery_with_no_power_sensor_gets_the_repair(hass):
    a_counters, a_power = _battery(hass, "pack_a", 1500)
    b_counters, _ = _battery(hass, "pack_b")
    ed = _parse_prefs(hass, _prefs(_batt(a_counters, a_power), _batt(b_counters)),
                      quiet=True)
    assert battery_units(ed) == [a_power]
    assert unsourced_counters(ed, "battery") == [b_counters[0]]
    _cycles(_reader(hass, ed), 0.0, WAIT + 20)
    assert _issue(hass, "battery").translation_placeholders == {
        "energy_entity": b_counters[0]}


async def test_two_inverters_with_no_power_sensor_are_both_named(hass):
    """A counter with no device (a Riemann sum, a template) cannot be
    searched: it is named, beside the inverter that reads."""
    a_counter, a_power = _inverter(hass, "inv_a", 3000)
    for eid in ("sensor.balcony_energy", "sensor.shed_energy"):
        hass.states.async_set(eid, "10", _KWH)
    ed = _parse_prefs(hass, _prefs(_solar(a_counter, a_power),
                                   _solar("sensor.balcony_energy"),
                                   _solar("sensor.shed_energy")), quiet=True)
    _cycles(_reader(hass, ed), 0.0, WAIT + 20)
    assert _issue(hass, "solar").translation_placeholders == {
        "energy_entity": "sensor.balcony_energy, sensor.shed_energy"}


async def test_the_repair_clears_once_the_inverter_is_found(hass):
    """The cold start: B's integration registers after SEM's first read.
    The #274 retry reads the dashboard again and finds it."""
    a_counter, a_power = _inverter(hass, "inv_a", 3000)
    b_counter, _ = _inverter(hass, "inv_b")
    prefs = _prefs(_solar(a_counter, a_power), _solar(b_counter))
    r = _reader(hass, _parse_prefs(hass, prefs, quiet=True))
    _cycles(r, 0.0, WAIT + 20)
    assert _issue(hass, "solar") is not None

    entry = hass.config_entries.async_get_entry(
        next(e.entry_id for e in hass.config_entries.async_entries("test_inverter")
             if e.unique_id == "inv_b"))
    dev = dr.async_get(hass).async_get_device(identifiers={("test_inverter", "inv_b")})
    _entity(hass, entry, dev, "sensor.inv_b_pv_power", "power", 2000)
    ed = _parse_prefs(hass, prefs, quiet=True)
    assert ed.power_resolution_incomplete() is False
    r.set_energy_dashboard_config(ed)
    readings = _cycles(r, WAIT + 30, WAIT + 30)
    assert readings.solar_power == pytest.approx(5000)
    assert _issue(hass, "solar") is None


# ── 4. never twice: a device SEM already reads ──────────────────────────


async def test_a_string_counter_on_a_read_inverter_is_not_read_twice(hass):
    """One inverter, two dashboard rows: string 1 with its own power link,
    string 2 with its counter only. The device's total power sensor holds
    string 1 too, so taking it would count string 1 twice."""
    entry, dev = _device(hass, "hybrid")
    rows = [("sensor.hybrid_pv1_energy", "energy", 500),
            ("sensor.hybrid_pv2_energy", "energy", 400),
            ("sensor.hybrid_pv1_power", "power", 1800),
            ("sensor.hybrid_pv_power_total", "power", 3000)]
    for eid, dc, value in rows:
        _entity(hass, entry, dev, eid, dc, value)
    ed = _parse_prefs(hass, _prefs(
        _solar("sensor.hybrid_pv1_energy", "sensor.hybrid_pv1_power"),
        _solar("sensor.hybrid_pv2_energy")), quiet=True)
    assert ed.solar_power_list == ["sensor.hybrid_pv1_power"]
    assert ed.derived_units == {}
    # not called missing either: its device is read
    assert unsourced_counters(ed, "solar") == []
    assert ed.power_resolution_incomplete() is False


async def test_a_counter_on_the_device_of_a_read_counter_is_left_alone(hass):
    """The link is a template with no device; the counter beside it on the
    inverter tells which device that template measures."""
    entry, dev = _device(hass, "inv")
    for eid, dc, value in (("sensor.inv_yield_1", "energy", 5),
                           ("sensor.inv_yield_2", "energy", 5),
                           ("sensor.inv_pv_power", "power", 3000)):
        _entity(hass, entry, dev, eid, dc, value)
    hass.states.async_set("sensor.pv_template_power", "3000", _W)
    ed = _parse_prefs(hass, _prefs(
        _solar("sensor.inv_yield_1", "sensor.pv_template_power"),
        _solar("sensor.inv_yield_2")), quiet=True)
    assert ed.solar_power_list == ["sensor.pv_template_power"]
    assert unsourced_counters(ed, "solar") == []


async def test_two_counters_of_one_device_with_no_link_take_its_sensor_once(hass):
    """The SolaX shape (#250): one device, two counter rows, no link."""
    _c, power = _inverter(hass, "solax", 3000, extra=(
        ("sensor.solax_pv2_yield", "energy", 7),))
    ed = _parse_prefs(hass, _prefs(_solar("sensor.solax_total_yield"),
                                   _solar("sensor.solax_pv2_yield")), quiet=True)
    assert ed.solar_power_list == [power]
    assert unsourced_counters(ed, "solar") == []


async def test_a_battery_on_a_device_another_battery_reads_is_left_alone(hass):
    """A fleet sensor beside a pack's own: taking it would add the pack."""
    entry, dev = _device(hass, "huawei")
    for eid, dc, value in (
            ("sensor.huawei_battery_1_charged", "energy", 5),
            ("sensor.huawei_battery_1_discharged", "energy", 4),
            ("sensor.huawei_battery_2_charged", "energy", 5),
            ("sensor.huawei_battery_2_discharged", "energy", 4),
            ("sensor.huawei_battery_1_power", "power", 800),
            ("sensor.huawei_batteries_battery_power", "power", 1600)):
        _entity(hass, entry, dev, eid, dc, value)
    ed = _parse_prefs(hass, _prefs(
        _batt(("sensor.huawei_battery_1_charged",
               "sensor.huawei_battery_1_discharged"),
              "sensor.huawei_battery_1_power"),
        _batt(("sensor.huawei_battery_2_charged",
               "sensor.huawei_battery_2_discharged"))), quiet=True)
    assert battery_units(ed) == ["sensor.huawei_battery_1_power"]
    assert unsourced_counters(ed, "battery") == []


# ── 5. the Config card pick ─────────────────────────────────────────────


async def test_a_solar_pick_stands_for_all_solar_when_one_inverter_reads(hass):
    a_counter, a_power = _inverter(hass, "inv_a", 3000)
    b_counter, _ = _inverter(hass, "inv_b")
    hass.states.async_set("sensor.all_pv", "5000", _W)
    ed = _parse_prefs(hass, _prefs(_solar(a_counter, a_power), _solar(b_counter)),
                      quiet=True)
    r = _reader(hass, ed, solar_production_sensor="sensor.all_pv")
    readings = _cycles(r, 0.0, WAIT + 20)
    assert readings.solar_power == pytest.approx(5000)
    assert _issue(hass, "solar") is None


async def test_a_solar_pick_does_not_hide_a_third_inverter_beside_a_sum(hass):
    """Two inverters read are summed, and the pick is not read (#623): the
    third one's 0 W is in the sum, so it is still named."""
    a_counter, a_power = _inverter(hass, "inv_a", 3000)
    b_counter, b_power = _inverter(hass, "inv_b", 2000)
    c_counter, _ = _inverter(hass, "inv_c")
    hass.states.async_set("sensor.all_pv", "6000", _W)
    ed = _parse_prefs(hass, _prefs(_solar(a_counter, a_power), _solar(b_counter),
                                   _solar(c_counter)), quiet=True)
    r = _reader(hass, ed, solar_production_sensor="sensor.all_pv")
    readings = _cycles(r, 0.0, WAIT + 20)
    assert readings.solar_power == pytest.approx(5000)
    assert _issue(hass, "solar").translation_placeholders == {
        "energy_entity": c_counter}


async def test_a_battery_pick_stands_for_the_battery_when_one_reads(hass):
    a_counters, a_power = _battery(hass, "pack_a", 1500)
    b_counters, _ = _battery(hass, "pack_b")
    hass.states.async_set("sensor.all_batt", "2000", _W)
    ed = _parse_prefs(hass, _prefs(_batt(a_counters, a_power), _batt(b_counters)),
                      quiet=True)
    _cycles(_reader(hass, ed, battery_power_sensor="sensor.all_batt"), 0.0, WAIT + 20)
    assert _issue(hass, "battery") is None


# ── 6. the rule for every home: each source is read or named ────────────


_SHAPES = ("linked", "found", "none")


async def test_every_counted_source_is_read_or_named(hass):
    """For one to three solar sources and one to three batteries, each
    linked, found on its own device, or with nothing on its device: every
    source's power is in the unit list or its counter is named. Before the
    fix, every home with a linked first source and an unlinked one after
    it broke this."""
    homes = 0
    for n in (1, 2, 3):
        for shapes in itertools.product(_SHAPES, repeat=n):
            for kind in ("solar", "battery"):
                homes += 1
                sources, expect_read, expect_named = [], [], []
                for i, shape in enumerate(shapes):
                    name = f"h{homes}_{kind}_{i}"
                    watts = None if shape == "none" else 100 * (i + 1)
                    if kind == "solar":
                        counter, power = _inverter(hass, name, watts)
                        sources.append(_solar(counter, power if shape == "linked" else None))
                        named = counter
                    else:
                        counters, power = _battery(hass, name, watts)
                        sources.append(_batt(counters, power if shape == "linked" else None))
                        named = counters[0]
                    (expect_named if shape == "none" else expect_read).append(
                        named if shape == "none" else power)
                if kind == "battery":       # solar + grid: SEM uses the dashboard
                    sources.append(_solar("sensor.roof_yield", "sensor.roof_power"))
                ed = _parse_prefs(hass, _prefs(*sources), quiet=True)
                units = (ed.solar_power_list if kind == "solar"
                         else battery_units(ed))
                assert sorted(units) == sorted(expect_read), (kind, shapes)
                assert unsourced_counters(ed, kind) == expect_named, (kind, shapes)
                assert ed.power_resolution_incomplete() is bool(expect_named), (
                    kind, shapes)
    assert homes == 2 * (3 + 9 + 27)


# ── 7. the diagnostics show what was found and what was not ─────────────


async def test_the_diagnostics_list_found_and_named_units(hass, tmp_path):
    from datetime import timedelta
    from unittest.mock import MagicMock

    from custom_components.solar_energy_management.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    a_counter, a_power = _inverter(hass, "inv_a", 3000)
    b_counter, b_power = _inverter(hass, "inv_b", 2000)
    c_counter, _ = _inverter(hass, "inv_c")
    ed = _parse_prefs(hass, _prefs(_solar(a_counter, a_power), _solar(b_counter),
                                   _solar(c_counter)), quiet=True)
    # the shapes tests/test_diagnostics_per_source_lists.py runs on
    mock_hass = MagicMock()
    mock_hass.config.config_dir = str(tmp_path)

    async def _executor(func, *args, **kwargs):
        return func(*args, **kwargs)
    mock_hass.async_add_executor_job = _executor
    mock_hass.states.get = MagicMock(return_value=None)
    entry = MagicMock()
    entry.entry_id, entry.version, entry.domain = "e1226", 7, DOMAIN
    entry.title, entry.data, entry.options = "SEM", {}, {}
    coordinator = MagicMock()
    coordinator.last_update_success = True
    coordinator.update_interval = timedelta(seconds=10)
    coordinator._observer_mode = False
    coordinator._load_manager = None
    coordinator.data = {}
    coordinator._energy_dashboard_config = ed
    entry.runtime_data = coordinator
    mock_hass.data = {DOMAIN: {entry.entry_id: coordinator}}

    result = await async_get_config_entry_diagnostics(mock_hass, entry)
    lists = result["energy_dashboard"]["per_source_lists"]
    assert lists["derived_units"] == {b_power: b_counter}
    assert lists["unsourced_counters"] == {"solar": [c_counter], "battery": []}


# ── 8. review: a slow start, a first record, the saved pick, the text ───


async def test_a_cold_start_finds_the_same_batteries(hass):
    """SEM often sets up before the battery's integration has loaded. The
    sensor is in the registry, with no state yet: it is found all the same,
    so the battery list, and each battery's saved settings, do not move
    between a slow start and a warm one."""
    a_counters, a_power = _battery(hass, "pack_a", 1500)
    b_counters, b_power = _battery(hass, "pack_b", 700, loaded=False)
    prefs = _prefs(_batt(a_counters, a_power), _batt(b_counters))
    cold = _parse_prefs(hass, prefs, quiet=True)
    assert battery_units(cold) == [a_power, b_power]
    assert unsourced_counters(cold, "battery") == []

    saved = {BATTERY_SLOT_UNITS_KEY: [a_power, b_power],
             "battery_modes": ["auto", "force_charge"],
             "battery_reserve_socs": [20, 40]}
    assert realign_battery_slots(saved, battery_units(cold),
                                 battery_units_before_derive(cold)) is None

    # loaded: the same list, and the battery reads
    hass.states.async_set(b_power, "700", _W)
    warm = _parse_prefs(hass, prefs, quiet=True)
    assert battery_units(warm) == battery_units(cold)


async def test_a_registered_sensor_that_is_not_power_is_not_taken(hass):
    """The control: with no state, the registry has to say power."""
    entry, dev = _device(hass, "pack_x")
    _entity(hass, entry, dev, "sensor.pack_x_charged", "energy", None)
    _entity(hass, entry, dev, "sensor.pack_x_battery_power", "energy", None)
    a_counters, a_power = _battery(hass, "pack_a", 1500)
    ed = _parse_prefs(hass, _prefs(_batt(a_counters, a_power),
                                   _batt(("sensor.pack_x_charged", None))), quiet=True)
    assert battery_units(ed) == [a_power]
    assert unsourced_counters(ed, "battery") == ["sensor.pack_x_charged"]


async def test_a_first_record_keeps_the_pairs_slot_with_the_pair(hass):
    """An install with no slot record yet (from before #1195): its slots
    were written when the pair was b1. The battery found now is b1 and the
    pair b2, so slot 1 must follow the pair; the found one starts unset."""
    entry, dev = _device(hass, "verto")
    pair = ("sensor.verto_discharge_power", "sensor.verto_charge_power")
    for eid in pair:
        _entity(hass, entry, dev, eid, "power", 0)
    for eid in ("sensor.verto_charged", "sensor.verto_discharged"):
        _entity(hass, entry, dev, eid, "energy", 5)
    b_counters, b_power = _battery(hass, "pack_b", 700)
    ed = _parse_prefs(hass, _prefs(
        _batt(("sensor.verto_charged", "sensor.verto_discharged"), pair=pair),
        _batt(b_counters)), quiet=True)
    assert battery_units(ed) == [b_power, pair[1]]
    assert battery_units_before_derive(ed) == [pair[1]]

    saved = {"battery_discharge_control_entities": ["number.verto_limit"],
             "battery_modes": ["force_discharge"],
             "battery_reserve_socs": [30]}
    changes, moved = realign_battery_slots(
        saved, battery_units(ed), battery_units_before_derive(ed))
    assert moved is False                    # a first record: no reload
    assert changes[BATTERY_SLOT_UNITS_KEY] == [b_power, pair[1]]
    assert changes["battery_discharge_control_entities"] == [None, "number.verto_limit"]
    assert changes["battery_modes"] == [None, "force_discharge"]
    assert changes["battery_reserve_socs"] == [None, 30]
    # the control: without the old order, the found battery took the pair's slot
    plain, _ = realign_battery_slots(saved, battery_units(ed))
    assert "battery_discharge_control_entities" not in plain


def test_the_old_order_is_what_a_build_before_numbered():
    ed = SimpleNamespace(
        battery_power_list=["sensor.a", "sensor.b", "sensor.c"],
        battery_power_pairs=[("sensor.d_from", "sensor.d_to")],
        derived_units={"sensor.b": "sensor.b_charged",
                       "sensor.pv": "sensor.pv_yield"},
        battery_charge_energy="sensor.a_charged", battery_discharge_energy=None)
    # beside batteries of their own, a found one was not a battery
    assert battery_units_before_derive(ed) == ["sensor.a", "sensor.c", "sensor.d_to"]
    # none of their own: only the first battery's was found
    ed = SimpleNamespace(
        battery_power_list=["sensor.a", "sensor.b"], battery_power_pairs=[],
        derived_units={"sensor.a": "sensor.a_charged", "sensor.b": "sensor.b_charged"},
        battery_charge_energy="sensor.a_charged", battery_discharge_energy=None)
    assert battery_units_before_derive(ed) == ["sensor.a"]
    # the first battery's sensor was not found: that build read none
    ed.derived_units = {"sensor.b": "sensor.b_charged"}
    ed.battery_power_list = ["sensor.b"]
    assert battery_units_before_derive(ed) == []
    # nothing found: the list as it is
    ed = SimpleNamespace(battery_power_list=["sensor.a"], battery_power_pairs=[],
                         derived_units={})
    assert battery_units_before_derive(ed) == ["sensor.a"]


def test_the_setup_aligns_with_the_old_order():
    """Deleting ``before=`` at the one call site leaves the rule unused."""
    from custom_components.solar_energy_management.coordinator.coordinator import (
        SEMCoordinator,
    )
    from custom_components.solar_energy_management.coordinator import (
        coordinator as coord_mod,
    )

    ed = SimpleNamespace(
        battery_power_list=["sensor.found"],
        battery_power_pairs=[("sensor.p_from", "sensor.p_to")],
        derived_units={"sensor.found": "sensor.found_charged"},
        battery_charge_energy="sensor.p_charged", battery_discharge_energy=None)
    entry = SimpleNamespace(entry_id="e", data={}, state=None, options={
        "battery_modes": ["force_discharge"]})
    hass = MagicMock()
    hass.config_entries.async_update_entry = lambda e, options: setattr(
        e, "options", options) or True
    fake = SimpleNamespace(config_entry=entry, hass=hass, config={})
    assert coord_mod.battery_units_before_derive(ed) == ["sensor.p_to"]
    SEMCoordinator._align_battery_slots(fake, ed)
    assert entry.options["battery_modes"] == [None, "force_discharge"]


async def test_a_saved_pick_of_one_batterys_own_sensor_hides_nothing(hass):
    """Setup saves the dashboard's first battery sensor as the battery pick
    (``to_dict``). That is one battery's sensor, not all of them."""
    a_counters, a_power = _battery(hass, "pack_a", 1500)
    b_counters, _ = _battery(hass, "pack_b")
    ed = _parse_prefs(hass, _prefs(_batt(a_counters, a_power), _batt(b_counters)),
                      quiet=True)
    _cycles(_reader(hass, ed, battery_power_sensor=a_power), 0.0, WAIT + 20)
    issue = _issue(hass, "battery")
    assert issue is not None
    assert issue.translation_key == "power_sensor_not_found_unit"


async def test_a_saved_pick_of_one_inverters_own_sensor_hides_nothing(hass):
    a_counter, a_power = _inverter(hass, "inv_a", 3000)
    b_counter, _ = _inverter(hass, "inv_b")
    ed = _parse_prefs(hass, _prefs(_solar(a_counter, a_power), _solar(b_counter)),
                      quiet=True)
    _cycles(_reader(hass, ed, solar_production_sensor=a_power), 0.0, WAIT + 20)
    assert _issue(hass, "solar").translation_key == "power_sensor_not_found_unit"


async def test_with_no_unit_read_the_text_offers_the_pick_too(hass):
    """Nothing of the source reads: the #1188 text, with its Config card
    remedy, stays."""
    a_counter, _ = _inverter(hass, "inv_a")
    b_counter, _ = _inverter(hass, "inv_b")
    ed = _parse_prefs(hass, _prefs(_solar(a_counter), _solar(b_counter)), quiet=True)
    assert ed.solar_power_list == []
    _cycles(_reader(hass, ed), 0.0, WAIT + 20)
    issue = _issue(hass, "solar")
    assert issue.translation_key == "power_sensor_not_found"
    assert issue.translation_placeholders == {
        "energy_entity": f"{a_counter}, {b_counter}"}


async def test_the_text_follows_when_the_other_unit_starts_reading(hass):
    """Same counter, other story: the Repair is filed again with its text."""
    a_counter, a_power = _inverter(hass, "inv_a", 3000)
    b_counter, _ = _inverter(hass, "inv_b")
    prefs = _prefs(_solar(a_counter, a_power), _solar(b_counter))
    ed = _parse_prefs(hass, prefs, quiet=True)
    r = _reader(hass, ed)
    _cycles(r, 0.0, WAIT + 20)
    assert _issue(hass, "solar").translation_key == "power_sensor_not_found_unit"
    ed.solar_power_list.clear()             # A's link removed: none reads
    ed.solar_power = None
    _cycles(r, WAIT + 30, WAIT + 30)
    assert _issue(hass, "solar").translation_key == "power_sensor_not_found"


_ROOT = Path(__file__).resolve().parents[1]
_FILES = [_ROOT / "strings.json"] + sorted((_ROOT / "translations").glob("*.json"))


@pytest.mark.parametrize("path", _FILES, ids=lambda p: p.name)
def test_every_language_has_the_per_unit_text(path):
    issues = json.loads(path.read_text(encoding="utf-8"))["issues"]
    text, whole = issues["power_sensor_not_found_unit"], issues["power_sensor_not_found"]
    for part in ("title", "description"):
        assert set(re.findall(r"\{(\w+)\}", text[part])) == {"energy_entity"}, (
            f"{path.name} {part}")
    # it sends the user to the Energy Dashboard only: shorter than the
    # #1188 text, which also offers the Config card pick
    assert len(text["description"]) < len(whole["description"]), path.name
    assert "⚙" not in text["description"], path.name


# ── 9. second review: a total on one row, and the pick ──────────────────


async def test_a_site_total_on_one_row_is_not_added_to(hass):
    """Row A links a site meter's total (another device); row B has its
    counter only, and its inverter has its own power sensor. HA adds up
    the linked rows, so the total is all of solar. Adding B's sensor read
    7000 W for 5000 W, and that surplus starts loads on grid power."""
    a_counter, _ = _inverter(hass, "inv_a")
    b_counter, b_power = _inverter(hass, "inv_b", 2000)
    entry, dev = _device(hass, "home_manager")
    _entity(hass, entry, dev, "sensor.home_manager_pv_power", "power", 5000)
    ed = _parse_prefs(hass, _prefs(_solar(a_counter, "sensor.home_manager_pv_power"),
                                   _solar(b_counter)), quiet=True)
    assert ed.solar_power_list == ["sensor.home_manager_pv_power"]
    # not called missing either: the total may hold it
    assert unsourced_counters(ed, "solar") == []
    readings = _cycles(_reader(hass, ed), 0.0, WAIT + 20)
    assert readings.solar_power == pytest.approx(5000)
    assert _issue(hass, "solar") is None


async def test_a_template_total_on_one_battery_row_is_not_added_to(hass):
    a_counters, _ = _battery(hass, "pack_a")
    b_counters, _ = _battery(hass, "pack_b", 700)
    hass.states.async_set("sensor.all_batteries_power", "1500", _W)
    ed = _parse_prefs(hass, _prefs(_batt(a_counters, "sensor.all_batteries_power"),
                                   _batt(b_counters)), quiet=True)
    assert battery_units(ed) == ["sensor.all_batteries_power"]
    assert unsourced_counters(ed, "battery") == []


async def test_a_pick_for_all_solar_beats_a_found_inverter(hass):
    """A user who set a total as the Config card pick (the #1226 work
    around) keeps it: the found sensor may be one string only."""
    a_counter, a_power = _inverter(hass, "inv_a", 3000)
    b_counter, b_power = _inverter(hass, "inv_b", 900)
    hass.states.async_set("sensor.all_pv", "5000", _W)
    ed = _parse_prefs(hass, _prefs(_solar(a_counter, a_power), _solar(b_counter)),
                      quiet=True)
    assert ed.solar_power_list == [a_power, b_power]
    yield_found_units_to_picks(ed, solar_pick="sensor.all_pv")
    assert ed.solar_power_list == [a_power]
    readings = _cycles(_reader(hass, ed, solar_production_sensor="sensor.all_pv"),
                       0.0, WAIT + 20)
    assert readings.solar_power == pytest.approx(5000)
    assert _issue(hass, "solar") is None


async def test_a_pick_beside_two_linked_inverters_changes_nothing(hass):
    """Two linked inverters are summed and the pick is not read (#623): a
    found third one is summed with them."""
    a_counter, a_power = _inverter(hass, "inv_a", 3000)
    b_counter, b_power = _inverter(hass, "inv_b", 2000)
    c_counter, c_power = _inverter(hass, "inv_c", 1000)
    ed = _parse_prefs(hass, _prefs(_solar(a_counter, a_power),
                                   _solar(b_counter, b_power), _solar(c_counter)),
                      quiet=True)
    yield_found_units_to_picks(ed, solar_pick="sensor.all_pv")
    assert ed.solar_power_list == [a_power, b_power, c_power]
    # and a pick that is one inverter's own sensor is no pick for all
    yield_found_units_to_picks(ed, solar_pick=a_power)
    assert ed.solar_power_list == [a_power, b_power, c_power]


async def test_a_pick_for_the_battery_beats_a_found_battery(hass):
    """The battery the same way, before the reader and the per-battery
    entities read the list, so both see one battery."""
    a_counters, a_power = _battery(hass, "pack_a", 1500)
    b_counters, b_power = _battery(hass, "pack_b", 700)
    ed = _parse_prefs(hass, _prefs(_batt(a_counters, a_power), _batt(b_counters)),
                      quiet=True)
    assert battery_units(ed) == [a_power, b_power]
    yield_found_units_to_picks(ed, battery_pick=a_power)      # setup's save
    assert battery_units(ed) == [a_power, b_power]
    yield_found_units_to_picks(ed, battery_pick="sensor.all_batt")
    assert battery_units(ed) == [a_power]
    assert ed.battery_power == a_power
    assert unit_energy_counters(ed, b_power) is None


async def test_a_pick_keeps_what_the_build_before_found(hass):
    """No battery linked: the build before found the first one and read
    the pick beside it (SOC from that sensor's device). That stays; only
    the second, found since, goes."""
    a_counters, a_power = _battery(hass, "pack_a", 1500)
    b_counters, b_power = _battery(hass, "pack_b", 700)
    ed = _parse_prefs(hass, _prefs(_batt(a_counters), _batt(b_counters)), quiet=True)
    assert battery_units(ed) == [a_power, b_power]
    yield_found_units_to_picks(ed, battery_pick="sensor.all_batt")
    assert battery_units(ed) == [a_power]
    assert ed.battery_power == a_power
    assert ed.derived_power["battery"] == a_power
    assert ed.derived_units == {a_power: a_counters[0]}
    assert ed.power_resolution_incomplete() is False


async def test_one_found_battery_and_a_pick_read_as_before(hass):
    a_counters, a_power = _battery(hass, "pack_a", 1500)
    ed = _parse_prefs(hass, _prefs(_batt(a_counters)), quiet=True)
    yield_found_units_to_picks(ed, battery_pick="sensor.all_batt")
    assert battery_units(ed) == [a_power]
    assert ed.battery_power == a_power


async def test_one_found_inverter_and_a_pick_read_as_before(hass):
    a_counter, a_power = _inverter(hass, "inv_a", 3000)
    b_counter, b_power = _inverter(hass, "inv_b", 2000)
    ed = _parse_prefs(hass, _prefs(_solar(a_counter), _solar(b_counter)), quiet=True)
    yield_found_units_to_picks(ed, solar_pick="sensor.all_pv")
    assert ed.solar_power_list == [a_power]
    assert ed.solar_power == a_power
    assert ed.derived_power["solar"] == a_power


async def test_a_found_battery_beside_a_pair_is_not_the_first_batterys_field(hass):
    """Setup saves ``battery_power`` as the battery pick: a found second
    battery there would later be read in place of the pair."""
    entry, dev = _device(hass, "verto")
    pair = ("sensor.verto_discharge_power", "sensor.verto_charge_power")
    for eid in pair:
        _entity(hass, entry, dev, eid, "power", 0)
    for eid in ("sensor.verto_charged", "sensor.verto_discharged"):
        _entity(hass, entry, dev, eid, "energy", 5)
    b_counters, b_power = _battery(hass, "pack_b", 700)
    ed = _parse_prefs(hass, _prefs(
        _batt(("sensor.verto_charged", "sensor.verto_discharged"), pair=pair),
        _batt(b_counters)), quiet=True)
    assert battery_units(ed) == [b_power, pair[1]]
    assert ed.battery_power is None
    assert ed.to_dict()["battery_power_sensor"] is None


def test_the_dashboard_read_applies_the_picks_before_handing_it_out():
    """Deleting the call, or moving it after the hand-out, leaves the
    reader and the platforms with the found units."""
    import ast
    import inspect
    import textwrap

    from custom_components.solar_energy_management.coordinator.coordinator import (
        SEMCoordinator,
    )
    src = textwrap.dedent(inspect.getsource(
        SEMCoordinator.async_initialize_energy_dashboard))
    calls = {}
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            calls.setdefault(name, []).append(node.lineno)
    assert len(calls["yield_found_units_to_picks"]) == 1
    first = calls["yield_found_units_to_picks"][0]
    assert first < min(calls["set_energy_dashboard_config"])
    assert first < min(calls["_align_battery_slots"])
