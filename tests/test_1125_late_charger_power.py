"""#1125 — a charger found after start-up: its draw counted as house load.

A charger whose integration loads after SEM is built by the coordinator's
late retry (``_retry_ev_device_setup``), not by setup. The retry handed its
sensors to the reader through a key list typed by hand:

    ev_connected_sensor    → ev_plug_sensor
    ev_charging_sensor     → ev_charging_sensor
    ev_total_energy_sensor → ev_total_energy_sensor   (no such reader field)

No power sensor. With no Energy Dashboard EV sensor the reader had nothing
to read, ``ev_power`` was 0 W, and ``home = solar + import − ev`` kept the
car inside the house. The charger's own view saw 0 W too (``build_view``
takes the fleet figure when no charger has a sensor of its own). Found as
bug class 120's first open item (#1044).

Pinned here: the found keys go through the reader's own start-up parse
(``SensorReader.adopt_late_charger``), so every EV sensor the reader knows
is handed over — a field added later is handed over too (bug class 24).
"""
from __future__ import annotations

import ast
import inspect
import pathlib
import textwrap
from dataclasses import fields
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import homeassistant.util.dt as dt_util
import pytest

from custom_components.solar_energy_management.coordinator.coordinator import (
    SEMCoordinator,
)
from custom_components.solar_energy_management.coordinator.health_check import (
    chargers_outside_home,
    sem_ev_chargers,
)
from custom_components.solar_energy_management.coordinator.sensor_reader import (
    SensorConfig,
    SensorReader,
)
from custom_components.solar_energy_management.ha_energy_reader import (
    EnergyDashboardConfig,
)

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_DISCOVER = ("custom_components.solar_energy_management.hardware_detection"
             ".discover_ev_charger_from_registry")

PV = "sensor.pv_power"
GRID = "sensor.grid_power"
PLUG = "binary_sensor.keba_plug"
CHARGING = "binary_sensor.keba_charging"
POWER = "sensor.keba_charging_power"
TOTAL = "sensor.keba_total_energy"

#: What ``_discover_keba`` returns for a KEBA P30 (hardware_detection).
KEBA = {
    "ev_connected_sensor": PLUG,
    "ev_charging_sensor": CHARGING,
    "ev_charging_power_sensor": POWER,
    "ev_total_energy_sensor": TOTAL,
    "ev_charger_service": "keba.set_current",
    "ev_service_param_name": "current",
    "ev_charger_service_entity_id": PLUG,
}

#: Noon, the car on 11 A × 3: 9 kW of sun, nothing to or from the grid.
SOLAR_W, EV_W = 9000.0, 7000.0


def _state(value, unit="W"):
    s = Mock()
    s.state = str(value)
    s.attributes = {"unit_of_measurement": unit} if unit else {}
    s.last_updated = s.last_reported = dt_util.utcnow()
    return s


def _hass(**extra):
    table = {PV: _state(SOLAR_W), GRID: _state(0.0),
             POWER: _state(EV_W), PLUG: _state("on", None),
             CHARGING: _state("on", None), **extra}
    hass = MagicMock()
    hass.states.get = MagicMock(side_effect=lambda eid: table.get(eid))
    hass.states.async_all = MagicMock(return_value=[])
    return hass


def _reader(config=None, *, dashboard=None):
    """A real reader. ``config`` None is the legacy path with a solar and a
    grid sensor; ``dashboard`` puts it on the Energy Dashboard path."""
    reader = SensorReader(_hass(), dict(config if config is not None else {
        "solar_production_sensor": PV, "grid_power_sensor": GRID}))
    reader._sign_vote_warmup = 0
    if dashboard is not None:
        reader.set_energy_dashboard_config(dashboard)
    return reader


async def _late_retry(reader, found=KEBA):
    """The coordinator's REAL late retry, on a stub that carries what it
    reads, with the charger found by discovery."""
    me = SimpleNamespace(
        hass=MagicMock(), config={"ev_surplus_priority": 3, "ev_phases": 3},
        _surplus_controller=MagicMock(), _sensor_reader=reader,
        _ev_device=None, _ev_devices={},
        refresh_detection_report=lambda: None,
    )
    with patch(_DISCOVER, return_value=dict(found)):
        await SEMCoordinator._retry_ev_device_setup(me)
    return me


def _home(readings):
    readings.calculate_derived()
    return readings.home_consumption_power


# ───────────────────────────────────────────────────────────────────────
# 1. the late charger's draw is read, and is not house load
# ───────────────────────────────────────────────────────────────────────

@pytest.mark.unit
class TestTheLateChargerIsRead:

    @pytest.mark.asyncio
    async def test_its_power_sensor_reaches_the_reader(self):
        reader = _reader()
        me = await _late_retry(reader)
        assert reader.config.ev_power_sensor == POWER
        assert reader.config.ev_plug_sensor == PLUG
        assert reader.config.ev_charging_sensor == CHARGING
        assert me._ev_device.power_entity_id == POWER

    @pytest.mark.asyncio
    async def test_the_car_is_not_house_load(self):
        reader = _reader()
        me = await _late_retry(reader)
        readings = reader.read_power()
        assert readings.ev_power == EV_W
        assert readings.ev_power_fleet_entity == POWER
        assert _home(readings) == SOLAR_W - EV_W
        # bug class 120: the charger this read covers is out of ``home``,
        # so the baseload does not take it away a second time.
        chargers = sem_ev_chargers(me)
        assert chargers == [me._ev_device]
        assert chargers_outside_home(chargers, readings) == chargers

    @pytest.mark.asyncio
    async def test_the_old_hand_over_kept_the_car_in_the_house(self):
        """The twin: the same charger, the reader given what the old key
        list gave (plug and charging, no power). The car's 7 kW is house
        load, and the charger is still inside ``home`` — the number this
        fix moves."""
        reader = _reader()
        old = {k: v for k, v in KEBA.items() if k != "ev_charging_power_sensor"}
        me = await _late_retry(reader, old)
        readings = reader.read_power()
        assert reader.config.ev_power_sensor is None
        assert readings.ev_power == 0.0
        assert _home(readings) == SOLAR_W
        assert chargers_outside_home(sem_ev_chargers(me), readings) == []

    @pytest.mark.asyncio
    async def test_on_the_energy_dashboard_path_too(self):
        """An Energy Dashboard with solar and grid but no EV sensor — the
        shape the issue names."""
        ed = EnergyDashboardConfig(solar_power=PV, solar_power_list=[PV],
                                   has_solar=True)
        reader = _reader({}, dashboard=ed)
        await _late_retry(reader)
        readings = reader.read_power()
        assert readings.ev_power == EV_W
        assert readings.ev_power_fleet_entity == POWER
        assert _home(readings) == SOLAR_W - EV_W

    @pytest.mark.asyncio
    async def test_an_energy_dashboard_ev_sensor_still_wins(self):
        """The Energy Dashboard's EV sensor is read first, as before; the
        late sensor is only the fallback."""
        ed_ev = "sensor.ed_ev_power"
        ed = EnergyDashboardConfig(solar_power=PV, solar_power_list=[PV],
                                   has_solar=True, ev_power=ed_ev)
        reader = SensorReader(_hass(**{ed_ev: _state(6500.0)}), {})
        reader._sign_vote_warmup = 0
        reader.set_energy_dashboard_config(ed)
        await _late_retry(reader)
        readings = reader.read_power()
        assert readings.ev_power_fleet_entity == ed_ev
        assert readings.ev_power == 6500.0

    @pytest.mark.asyncio
    async def test_a_sensor_the_user_set_is_kept(self):
        mine = "sensor.my_ev_meter"
        reader = _reader({"solar_production_sensor": PV,
                          "grid_power_sensor": GRID,
                          "ev_power_sensor": mine})
        await _late_retry(reader)
        assert reader.config.ev_power_sensor == mine
        assert reader.config.ev_plug_sensor == PLUG


# ───────────────────────────────────────────────────────────────────────
# 2. one parse — the hand-over cannot leave a field out again
# ───────────────────────────────────────────────────────────────────────

def _keys_the_parse_reads() -> set:
    """Every config key ``_parse_config`` reads, from its own source."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(SensorReader._parse_config)))
    keys = set()
    for n in ast.walk(tree):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "get" and n.args
                and isinstance(n.args[0], ast.Constant)
                and isinstance(n.args[0].value, str)):
            keys.add(n.args[0].value)
    return keys


@pytest.mark.unit
class TestOneParse:

    def test_every_ev_field_the_parse_knows_is_handed_over(self):
        """Derived, not listed: a found dict with every key the start-up
        parse reads fills every EV field — a field added to the parse later
        is handed over without anyone touching the late path."""
        keys = _keys_the_parse_reads()
        assert "ev_charging_power_sensor" in keys  # the scan can see
        reader = SensorReader(MagicMock(), {})
        taken = reader.adopt_late_charger({k: f"sensor.{k}" for k in keys})
        ev_fields = {f.name for f in fields(SensorConfig)
                     if f.name.startswith("ev_")}
        assert set(taken) == ev_fields
        for name in ev_fields:
            assert getattr(reader.config, name), name

    def test_a_charger_brings_charger_sensors_only(self):
        reader = SensorReader(MagicMock(), {})
        reader.adopt_late_charger({"solar_production_sensor": "sensor.x",
                                   "battery_soc_sensor": "sensor.y"})
        assert reader.config.solar_power_sensor is None
        assert reader.config.battery_soc_sensor is None

    def test_an_empty_find_changes_nothing(self):
        reader = SensorReader(MagicMock(), {"ev_power_sensor": "sensor.m"})
        assert reader.adopt_late_charger({}) == []
        assert reader.adopt_late_charger(None) == []
        assert reader.config.ev_power_sensor == "sensor.m"

    def test_the_late_path_hands_over_through_the_reader(self):
        from custom_components.solar_energy_management.tests.ast_contracts import (
            calls,
        )
        assert calls(SEMCoordinator._retry_ev_device_setup, "adopt_late_charger")

    def test_no_one_else_writes_the_readers_fields(self):
        """Structural: outside ``sensor_reader.py`` no production code sets
        a field of the reader's config — not by ``setattr`` and not by
        assignment. A second writer is where a key list grows again."""
        offenders = []
        for path in sorted(_ROOT.rglob("*.py")):
            rel = path.relative_to(_ROOT).as_posix()
            if rel.startswith(("tests/", "scripts/")) or rel == (
                    "coordinator/sensor_reader.py"):
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            offenders += [f"{rel}:{line}" for line in _reader_config_writes(tree)]
        assert not offenders, offenders

    def test_the_lint_can_fail(self):
        """Bug class 8: the scan above sees the old shape, both spellings."""
        old = ast.parse(textwrap.dedent("""
            def f(self, value):
                setattr(self._sensor_reader.config, "ev_plug_sensor", value)
                self._sensor_reader.config.ev_power_sensor = value
                self._sensor_reader.other = value
        """))
        assert _reader_config_writes(old) == [3, 4]


def _reader_config_writes(tree) -> list:
    """Lines that set a field on ``<x>._sensor_reader.config``."""
    def _is_reader_config(node):
        return (isinstance(node, ast.Attribute) and node.attr == "config"
                and isinstance(node.value, ast.Attribute)
                and node.value.attr == "_sensor_reader")

    lines = []
    for n in ast.walk(tree):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                and n.func.id == "setattr" and n.args
                and _is_reader_config(n.args[0])):
            lines.append(n.lineno)
        elif isinstance(n, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            tgts = n.targets if isinstance(n, ast.Assign) else [n.target]
            if any(isinstance(t, ast.Attribute) and _is_reader_config(t.value)
                   for t in tgts):
                lines.append(n.lineno)
    return sorted(lines)
