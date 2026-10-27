"""#1188 — SEM finds no power sensor and says so only in the log.

The Energy Dashboard's grid entry had kWh counters and no power sensor, and
SEM found none on the meter. Grid read 0 W: every grid flow was 0, the Sankey
had no Grid lines and the surplus was wrong. One log line said why
("No grid power sensor found"); nothing a user looks at did.

The same holds for solar and battery: a kWh counter with no power sensor
reads 0 W, and the dashboard's start-up retry (#274) gives up with one log
line. Each source now gets a Repair (``power_sensor_not_found_<source>``),
once HA runs, the retry is over and five minutes have passed — a meter that
loads late is not missing (class 86). It clears the cycle a sensor is found.
"""
from __future__ import annotations

import ast
import inspect
import json
import re
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest
from homeassistant.core import CoreState
from homeassistant.helpers import issue_registry as ir

from custom_components.solar_energy_management.const import DOMAIN
from custom_components.solar_energy_management.coordinator import (
    repair_issues as ri,
)
from custom_components.solar_energy_management.coordinator.coordinator import (
    SEMCoordinator,
)
from custom_components.solar_energy_management.coordinator.sensor_reader import (
    SensorReader,
)
from custom_components.solar_energy_management.ha_energy_reader import (
    EnergyDashboardConfig,
)

_ROOT = Path(__file__).resolve().parents[1]
MONO = "time.monotonic"
WAIT = ri.UNAVAILABLE_REPAIR_THRESHOLD_S

COUNTER = {
    "grid": "sensor.p1_meter_energy_import",
    "solar": "sensor.inverter_total_yield",
    "battery": "sensor.battery_charge_total",
}
POWER = {
    "grid": "sensor.p1_meter_power",
    "solar": "sensor.inverter_power",
    "battery": "sensor.battery_power",
}


def _issue(hass, source):
    return ir.async_get(hass).async_get_issue(
        DOMAIN, f"power_sensor_not_found_{source}")


def _ed(missing=(), **extra) -> EnergyDashboardConfig:
    """Every source counted in kWh; the ``missing`` ones have no power."""
    ed = EnergyDashboardConfig(
        solar_energy=COUNTER["solar"], solar_energy_list=[COUNTER["solar"]],
        grid_import_energy=COUNTER["grid"],
        grid_import_energy_list=[COUNTER["grid"]],
        grid_export_energy="sensor.p1_meter_energy_export",
        battery_charge_energy=COUNTER["battery"],
        battery_discharge_energy="sensor.battery_discharge_total",
        has_solar=True, has_grid=True, has_battery=True, **extra)
    if "solar" not in missing:
        ed.solar_power = POWER["solar"]
        ed.solar_power_list = [POWER["solar"]]
    if "grid" not in missing:
        ed.grid_import_power = POWER["grid"]
        ed.grid_power_list = [POWER["grid"]]
    if "battery" not in missing:
        ed.battery_power = POWER["battery"]
        ed.battery_power_list = [POWER["battery"]]
    return ed


def _set_states(hass, missing=()):
    for eid in COUNTER.values():
        hass.states.async_set(eid, "100", {"unit_of_measurement": "kWh"})
    for source, eid in POWER.items():
        if source not in missing:
            hass.states.async_set(eid, "-400", {"unit_of_measurement": "W"})


def _reader(hass, ed, **config) -> SensorReader:
    r = SensorReader(hass, config)
    r._sign_vote_warmup = 0
    r.set_energy_dashboard_config(ed)
    return r


def _cycles(r, t0, t1, *, step=10.0, waiting=False):
    """One cycle the way the coordinator runs it: read, then report."""
    t = t0
    while t <= t1:
        with patch(MONO, return_value=t):
            r.read_power()
            r.report_unsourced_power(waiting=waiting)
        t += step


# ── each source is told ─────────────────────────────────────────────────


@pytest.mark.parametrize("source", ri.POWER_SOURCES)
async def test_a_counted_source_with_no_power_sensor_gets_a_repair(hass, source):
    _set_states(hass, missing=(source,))
    r = _reader(hass, _ed(missing=(source,)))

    _cycles(r, 0.0, WAIT - 10)
    assert _issue(hass, source) is None, "raised before the wait was over"

    _cycles(r, WAIT, WAIT + 20)
    issue = _issue(hass, source)
    assert issue is not None
    assert issue.translation_key == "power_sensor_not_found"
    assert issue.translation_placeholders == {"energy_entity": COUNTER[source]}
    assert issue.learn_more_url and "#sem-found-no-power-sensor" in issue.learn_more_url
    # The other sources have a sensor, so nothing about them.
    for other in set(ri.POWER_SOURCES) - {source}:
        assert _issue(hass, other) is None


async def test_the_reporters_home_end_to_end(hass, caplog):
    """A P1 meter with counters and no power: grid reads 0 W, and now a
    Repair names the counter, beside the old log line."""
    _set_states(hass, missing=("grid",))
    r = _reader(hass, _ed(missing=("grid",)))
    with patch(MONO, return_value=0.0):
        readings = r.read_power()
    assert readings.grid_power == 0.0
    assert "No grid power sensor found" in caplog.text
    _cycles(r, 10.0, WAIT + 20)
    assert _issue(hass, "grid").translation_placeholders == {
        "energy_entity": COUNTER["grid"]}
    assert "No grid power sensor: the Energy Dashboard counts" in caplog.text


async def test_a_home_missing_two_sources_sees_two(hass):
    _set_states(hass, missing=("grid", "battery"))
    r = _reader(hass, _ed(missing=("grid", "battery")))
    _cycles(r, 0.0, WAIT + 20)
    assert _issue(hass, "grid") and _issue(hass, "battery")
    assert _issue(hass, "solar") is None


# ── not raised: a late meter, start-up, the retry, other Repairs ────────


async def test_a_meter_that_loads_late_is_not_missing(hass):
    _set_states(hass, missing=("grid",))
    ed = _ed(missing=("grid",))
    r = _reader(hass, ed)
    _cycles(r, 0.0, WAIT - 60)
    # The dashboard retry found it (#274): the next reads use it.
    ed.grid_import_power = POWER["grid"]
    ed.grid_power_list = [POWER["grid"]]
    hass.states.async_set(POWER["grid"], "-400", {"unit_of_measurement": "W"})
    _cycles(r, WAIT - 50, WAIT * 3)
    assert _issue(hass, "grid") is None


async def test_nothing_while_home_assistant_starts(hass):
    _set_states(hass, missing=("solar",))
    r = _reader(hass, _ed(missing=("solar",)))
    hass.set_state(CoreState.starting)
    try:
        _cycles(r, 0.0, WAIT * 3)
        assert _issue(hass, "solar") is None
    finally:
        hass.set_state(CoreState.running)
    # The wait starts once HA runs, not at the first read.
    _cycles(r, WAIT * 3 + 10, WAIT * 4)
    assert _issue(hass, "solar") is None
    _cycles(r, WAIT * 4 + 10, WAIT * 4 + 30)
    assert _issue(hass, "solar") is not None


async def test_nothing_while_the_dashboard_retry_still_looks(hass):
    _set_states(hass, missing=("battery",))
    r = _reader(hass, _ed(missing=("battery",)))
    _cycles(r, 0.0, WAIT * 3, waiting=True)
    assert _issue(hass, "battery") is None
    _cycles(r, WAIT * 3 + 10, WAIT * 4 + 30)
    assert _issue(hass, "battery") is not None


async def test_a_grid_pick_on_the_config_card_counts_as_found(hass):
    _set_states(hass, missing=("grid",))
    hass.states.async_set("sensor.picked_grid", "-250", {"unit_of_measurement": "W"})
    r = _reader(hass, _ed(missing=("grid",)), grid_power_sensor="sensor.picked_grid")
    _cycles(r, 0.0, WAIT + 20)
    assert _issue(hass, "grid") is None


@pytest.mark.parametrize("source,key", [
    ("solar", "solar_production_sensor"), ("battery", "battery_power_sensor")])
async def test_a_solar_or_battery_pick_counts_as_found(hass, source, key):
    _set_states(hass, missing=(source,))
    hass.states.async_set("sensor.picked", "300", {"unit_of_measurement": "W"})
    r = _reader(hass, _ed(missing=(source,)), **{key: "sensor.picked"})
    _cycles(r, 0.0, WAIT + 20)
    assert _issue(hass, source) is None


async def test_half_a_grid_pair_keeps_its_own_repair(hass):
    """An export meter found by name, no import: split_grid_rejected says
    that. A second notice for the same grid would say it twice."""
    _set_states(hass, missing=("grid",))
    hass.states.async_set("sensor.grid_power_production", "0", {
        "unit_of_measurement": "W", "device_class": "power",
        "state_class": "measurement"})
    r = _reader(hass, _ed(missing=("grid",)))
    _cycles(r, 0.0, WAIT + 20)
    assert r._split_grid_discovery["export"] == "sensor.grid_power_production"
    assert ir.async_get(hass).async_get_issue(DOMAIN, "split_grid_rejected")
    assert _issue(hass, "grid") is None


async def test_a_source_not_counted_is_not_missing(hass):
    """No battery in the dashboard: nothing says there is one."""
    _set_states(hass)
    ed = _ed()
    ed.battery_power = None
    ed.battery_power_list = []
    ed.battery_charge_energy = ed.battery_discharge_energy = None
    ed.has_battery = False
    r = _reader(hass, ed)
    _cycles(r, 0.0, WAIT + 20)
    assert _issue(hass, "battery") is None


@pytest.mark.parametrize("shape", [
    "grid_declared_pair", "grid_sum", "grid_manual_pair", "two_inverters",
    "two_batteries", "battery_pair"])
async def test_every_other_way_to_read_a_source_counts_as_found(hass, shape):
    """Each source is recorded only in the last branch of its read: every
    branch that reads something must leave it alone."""
    _set_states(hass, missing=("grid", "solar", "battery"))
    for eid in ("sensor.a", "sensor.b"):
        hass.states.async_set(eid, "200", {"unit_of_measurement": "W"})
    config = {}
    ed = _ed(missing=("grid", "solar", "battery"))
    source = {"grid_declared_pair": "grid", "grid_sum": "grid",
              "grid_manual_pair": "grid", "two_inverters": "solar",
              "two_batteries": "battery", "battery_pair": "battery"}[shape]
    if shape == "grid_declared_pair":
        ed.grid_power_from, ed.grid_power_to = "sensor.a", "sensor.b"
    elif shape == "grid_sum":
        ed.grid_power_list = ["sensor.a", "sensor.b"]
    elif shape == "grid_manual_pair":
        config = {"grid_import_power_entity": "sensor.a",
                  "grid_export_power_entity": "sensor.b"}
    elif shape == "two_inverters":
        ed.solar_power = "sensor.a"
        ed.solar_power_list = ["sensor.a", "sensor.b"]
    elif shape == "two_batteries":
        ed.battery_power = "sensor.a"
        ed.battery_power_list = ["sensor.a", "sensor.b"]
    else:
        ed.battery_power_from, ed.battery_power_to = "sensor.a", "sensor.b"
        ed.battery_power_pairs = [("sensor.a", "sensor.b")]
    r = _reader(hass, ed, **config)
    _cycles(r, 0.0, WAIT + 20)
    assert _issue(hass, source) is None
    assert source not in r._unsourced_power
    # The control: the sources left without a sensor are still told.
    for other in {"grid", "solar", "battery"} - {source}:
        assert _issue(hass, other) is not None


async def test_an_export_counter_alone_is_told_too(hass):
    """No import counter: no grid branch reads anything, so the export
    counter is the one named."""
    _set_states(hass, missing=("grid",))
    ed = _ed(missing=("grid",))
    ed.grid_import_energy = None
    ed.grid_import_energy_list = []
    r = _reader(hass, ed)
    _cycles(r, 0.0, WAIT + 20)
    assert _issue(hass, "grid").translation_placeholders == {
        "energy_entity": "sensor.p1_meter_energy_export"}


async def test_a_failed_raise_is_tried_again(hass):
    _set_states(hass, missing=("grid",))
    r = _reader(hass, _ed(missing=("grid",)))
    real = ir.async_create_issue
    calls = []

    def flaky(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("registry busy")
        return real(*args, **kwargs)
    with patch.object(ri.ir, "async_create_issue", side_effect=flaky):
        _cycles(r, 0.0, WAIT + 20)
    assert len(calls) == 2
    assert _issue(hass, "grid") is not None


# ── cleared: found, gone, a fresh reader; untouched without an answer ───


async def test_it_clears_the_cycle_a_sensor_is_found(hass):
    _set_states(hass, missing=("grid",))
    ed = _ed(missing=("grid",))
    r = _reader(hass, ed)
    _cycles(r, 0.0, WAIT + 20)
    assert _issue(hass, "grid") is not None

    r.config.grid_power_sensor = "sensor.picked_grid"
    hass.states.async_set("sensor.picked_grid", "-250", {"unit_of_measurement": "W"})
    _cycles(r, WAIT + 30, WAIT + 30)
    assert _issue(hass, "grid") is None


async def test_it_clears_when_the_dashboard_stops_counting_the_source(hass):
    _set_states(hass, missing=("solar",))
    ed = _ed(missing=("solar",))
    r = _reader(hass, ed)
    _cycles(r, 0.0, WAIT + 20)
    assert _issue(hass, "solar") is not None
    ed.solar_energy = None
    ed.solar_energy_list = []
    _cycles(r, WAIT + 30, WAIT + 30)
    assert _issue(hass, "solar") is None


async def test_a_fresh_reader_clears_what_a_reload_left(hass):
    """Class 84: the Repair outlives the reader. An options reload builds a
    new one, whose first answer must clear a leftover once."""
    _set_states(hass)
    ri.raise_power_sensor_not_found(hass, "grid", energy_entity=COUNTER["grid"])
    r = _reader(hass, _ed())
    _cycles(r, 0.0, 0.0)
    assert _issue(hass, "grid") is None


async def test_a_reload_leftover_clears_when_the_source_is_found_later(hass):
    """(review) The meter is still missing on the new reader's first cycle
    and found a minute later: that first "found" clears the leftover."""
    _set_states(hass, missing=("grid",))
    ri.raise_power_sensor_not_found(hass, "grid", energy_entity=COUNTER["grid"])
    r = _reader(hass, _ed(missing=("grid",)))
    _cycles(r, 0.0, 60.0)
    assert _issue(hass, "grid") is not None        # still missing: it stays
    r.config.grid_power_sensor = "sensor.picked_grid"
    hass.states.async_set("sensor.picked_grid", "-250", {"unit_of_measurement": "W"})
    _cycles(r, 70.0, 70.0)
    assert _issue(hass, "grid") is None


async def test_a_cycle_without_the_dashboard_changes_nothing(hass):
    """The legacy path read the config keys: no answer about the dashboard,
    so a standing Repair is not taken for "found"."""
    hass.states.async_set("sensor.legacy_grid", "-100", {"unit_of_measurement": "W"})
    ri.raise_power_sensor_not_found(hass, "grid", energy_entity=COUNTER["grid"])
    r = SensorReader(hass, {"grid_power_sensor": "sensor.legacy_grid"})
    _cycles(r, 0.0, WAIT + 20)
    assert _issue(hass, "grid") is not None
    assert r._unsourced_power is None


async def test_it_is_raised_once_not_every_cycle(hass):
    _set_states(hass, missing=("grid",))
    r = _reader(hass, _ed(missing=("grid",)))
    with patch.object(ri, "raise_power_sensor_not_found",
                      wraps=ri.raise_power_sensor_not_found) as spy:
        _cycles(r, 0.0, WAIT * 3)
    assert spy.call_count == 1


# ── the cycle asks, and the text fits what is passed ────────────────────


def test_the_cycle_reports_after_its_first_read():
    """Deleting the one call site would leave every test above green."""
    src = textwrap.dedent(inspect.getsource(SEMCoordinator._async_update_data))
    tree = ast.parse(src)
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and getattr(n.func, "attr", None) == "report_unsourced_power"]
    assert len(calls) == 1, "the cycle must ask once"
    kw = {k.arg: k.value for k in calls[0].keywords}
    assert isinstance(kw.get("waiting"), ast.Attribute)
    assert kw["waiting"].attr == "_ed_resolve_pending"
    reads = [n.lineno for n in ast.walk(tree) if isinstance(n, ast.Call)
             and getattr(n.func, "attr", None) == "read_power"]
    assert reads and calls[0].lineno > min(reads)


_FILES = [_ROOT / "strings.json"] + sorted((_ROOT / "translations").glob("*.json"))


@pytest.mark.parametrize("path", _FILES, ids=lambda p: p.name)
def test_every_language_names_the_counter_it_is_given(path):
    text = json.loads(path.read_text(encoding="utf-8"))["issues"][
        "power_sensor_not_found"]
    for part in ("title", "description"):
        assert set(re.findall(r"\{(\w+)\}", text[part])) == {"energy_entity"}, (
            f"{path.name} {part}")


def test_every_source_is_recorded_by_the_dashboard_read():
    """A source added to POWER_SOURCES must be recorded by the read too, or
    its Repair can never be raised."""
    src = inspect.getsource(SensorReader._read_from_energy_dashboard)
    recorded = set(re.findall(r'unsourced\["(\w+)"\]\s*=', src))
    assert recorded == set(ri.POWER_SOURCES)


def test_the_repair_is_not_persistent():
    """The reader asks again after every start, so a restart may drop it."""
    src = inspect.getsource(ri.raise_power_sensor_not_found)
    assert "is_persistent=False" in src
