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
from custom_components.solar_energy_management.coordinator.build_view import (
    build_charger_view,
)
from custom_components.solar_energy_management.coordinator.charger_types import (
    FleetCycleState,
)
from custom_components.solar_energy_management.ha_energy_reader import (
    EnergyDashboardConfig,
)
from custom_components.solar_energy_management.hardware_detection import (
    _discover_keba,
)
from custom_components.solar_energy_management.tests.ast_contracts import (
    _SKIP_DIRS,
    _production_files,
    calls,
)

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_DISCOVER = ("custom_components.solar_energy_management.hardware_detection"
             ".discover_ev_charger_from_registry")

PV = "sensor.pv_power"
GRID = "sensor.grid_power"
PLUG = "binary_sensor.keba_p30_plug"
CHARGING = "binary_sensor.keba_p30_charging_state"
POWER = "sensor.keba_p30_charging_power"
TOTAL = "sensor.keba_p30_total_energy"


def _row(entity_id, device_class):
    return SimpleNamespace(entity_id=entity_id, original_device_class=device_class,
                           device_id="keba_p30", platform="keba")


#: What the REAL KEBA discovery returns for a KEBA P30's registry rows —
#: not a copy of its keys, so a renamed key cannot leave this test green.
KEBA = _discover_keba([
    _row(PLUG, "plug"), _row(CHARGING, "power"),
    _row(POWER, "power"), _row(TOTAL, "energy"),
])

#: Noon, the car on 11 A × 3: 9 kW of sun, nothing to or from the grid.
SOLAR_W, EV_W = 9000.0, 7000.0


def _state(value, unit="W"):
    s = Mock()
    s.state = str(value)
    s.attributes = {"unit_of_measurement": unit} if unit else {}
    s.last_updated = s.last_reported = dt_util.utcnow()
    return s


def _hass(extra=None):
    table = {PV: _state(SOLAR_W), GRID: _state(0.0),
             POWER: _state(EV_W), PLUG: _state("on", None),
             CHARGING: _state("on", None), **(extra or {})}
    hass = MagicMock()
    hass.states.get = MagicMock(side_effect=lambda eid: table.get(eid))
    hass.states.async_all = MagicMock(return_value=[])
    return hass


def _reader(config=None, *, dashboard=None, states=None):
    """A real reader. ``config`` None is the legacy path with a solar and a
    grid sensor; ``dashboard`` puts it on the Energy Dashboard path;
    ``states`` overrides sensor states."""
    reader = SensorReader(_hass(states), dict(config if config is not None else {
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
    async def test_the_charger_sees_its_own_draw(self):
        """``build_view`` gives the only charger the fleet figure; before the
        fix that was 0 W, so the charger never saw the car drawing (#536)."""
        reader = _reader()
        await _late_retry(reader)
        view = build_charger_view(
            FleetCycleState(power=reader.read_power(), config={},
                            is_night=False, tariff_level=None,
                            forecast_remaining_kwh=0.0),
            charger_id="ev_charger", charger_cfg={}, mode="solar_only",
            daily_ev_kwh=0.0)
        assert view.power.power_w == EV_W

    @pytest.mark.asyncio
    async def test_a_charging_flag_with_no_draw_is_not_a_charge(self):
        """The 500 W floor (#739) gates the charging flag on the power the
        reader now has, as it does for a charger built at setup."""
        reader = _reader(states={POWER: _state(100.0)})
        await _late_retry(reader)
        assert reader.read_power().ev_charging is False

    @pytest.mark.asyncio
    async def test_a_car_drawing_is_connected_whatever_the_plug_says(self):
        """The physics check (#285) needs the draw to see the car: plug and
        charging flag both read off, only the 7 kW says a car is there."""
        reader = _reader(states={PLUG: _state("off", None),
                                 CHARGING: _state("off", None)})
        await _late_retry(reader)
        assert reader.read_power().ev_connected is True

    @pytest.mark.asyncio
    async def test_the_old_hand_over_kept_the_car_in_the_house(self):
        """The twin: the new code given a find with no power sensor — what
        the old key list handed over (plug and charging). The car's 7 kW is
        house load, and the charger is still inside ``home`` — the number
        this fix moves."""
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
        reader = _reader({}, dashboard=ed, states={ed_ev: _state(6500.0)})
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


def _keys_discovery_writes() -> set:
    """Every key the charger discovery writes into a find (a subscript store
    or a ``setdefault``), from ``hardware_detection.py``'s own source."""
    tree = ast.parse((_ROOT / "hardware_detection.py").read_text(encoding="utf-8"))
    keys = set()
    for n in ast.walk(tree):
        if (isinstance(n, ast.Subscript) and isinstance(n.ctx, ast.Store)
                and isinstance(n.slice, ast.Constant)
                and isinstance(n.slice.value, str)):
            keys.add(n.slice.value)
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "setdefault" and n.args
                and isinstance(n.args[0], ast.Constant)):
            keys.add(n.args[0].value)
    return keys


@pytest.mark.unit
class TestOneParse:

    def test_every_key_discovery_finds_and_the_parse_reads_is_taken(self):
        """Independent of the field names: each key the discovery writes
        and the start-up parse reads is taken on its own — a reader field
        not named ``ev_*`` would fail here."""
        shared = _keys_discovery_writes() & _keys_the_parse_reads()
        assert {"ev_charging_power_sensor", "ev_connected_sensor",
                "ev_charging_sensor"} <= shared
        for key in sorted(shared):
            reader = SensorReader(MagicMock(), {})
            assert reader.adopt_late_charger({key: "sensor.x"}), key

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
        assert calls(SEMCoordinator._retry_ev_device_setup, "adopt_late_charger")

    def test_no_one_else_writes_the_readers_fields(self):
        """Structural: outside ``sensor_reader.py`` no production code sets
        a field of the reader's config — not by ``setattr`` and not by
        assignment. A second writer is where a key list grows again."""
        offenders = []
        for rel, tree in _production_files(None, _SKIP_DIRS):
            if rel.as_posix() == "coordinator/sensor_reader.py":
                continue
            offenders += [f"{rel}:{line}" for line in _reader_config_writes(tree)]
        assert not offenders, offenders

    def test_the_lint_can_fail(self):
        """Bug class 8: the scan above sees the old shape, every spelling."""
        old = ast.parse(textwrap.dedent("""
            def f(self, value):
                setattr(self._sensor_reader.config, "ev_plug_sensor", value)
                self._sensor_reader.config.ev_power_sensor = value
                self._sensor_reader.config = replace(value)
                self._sensor_reader.other = value
        """))
        assert _reader_config_writes(old) == [3, 4, 5]


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
            if any(_is_reader_config(t) or (isinstance(t, ast.Attribute)
                                            and _is_reader_config(t.value))
                   for t in tgts):
                lines.append(n.lineno)
    return sorted(lines)
