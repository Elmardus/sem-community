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

Pinned here: since #1136 the late retry saves the find as setup saves its
own, and setup's one builder and the reader's own start-up parse take it
from the saved card. So the late charger's power is read like any saved
charger's, and no second hand-over list can leave a sensor out (bug class
24).
"""
from __future__ import annotations

import ast
import pathlib
import textwrap
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import homeassistant.util.dt as dt_util
import pytest

from custom_components.solar_energy_management.coordinator.coordinator import (
    SEMCoordinator,
)
from custom_components.solar_energy_management.coordinator.health_check import (
    chargers_outside_home,
)
from custom_components.solar_energy_management.coordinator.sensor_reader import (
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


BASE = {"solar_production_sensor": PV, "grid_power_sensor": GRID}


def _reader(config, *, dashboard=None, states=None):
    """A real reader. ``dashboard`` puts it on the Energy Dashboard path;
    ``states`` overrides sensor states."""
    reader = SensorReader(_hass(states), dict(config))
    reader._sign_vote_warmup = 0
    if dashboard is not None:
        reader.set_energy_dashboard_config(dashboard)
    return reader


async def _late_retry(found=KEBA):
    """The coordinator's REAL late retry, on a stub that carries what it
    reads, with the charger found by discovery. Returns the saved card."""
    entry = SimpleNamespace(data={}, options={})
    hass = MagicMock()
    me = SimpleNamespace(hass=hass, config={}, config_entry=entry,
                         _ev_retry_count=1)
    with patch(_DISCOVER, return_value=dict(found)):
        await SEMCoordinator._retry_ev_device_setup(me)
    if not hass.config_entries.async_update_entry.called:
        return None
    return hass.config_entries.async_update_entry.call_args.kwargs[
        "options"]["ev_chargers"][0]


async def _saved(config=None, found=KEBA, **kw):
    """What setup reads after the reload: SEM's config with the saved
    card, and the charger device built from it."""
    card = await _late_retry(found)
    assert card is not None
    reader = _reader({**(BASE if config is None else config),
                      "ev_chargers": [card]}, **kw)
    device = SimpleNamespace(power_entity_id=card.get("ev_charging_power_sensor"))
    return reader, device


def _home(readings):
    readings.calculate_derived()
    return readings.home_consumption_power


# ───────────────────────────────────────────────────────────────────────
# 1. the late charger's draw is read, and is not house load
# ───────────────────────────────────────────────────────────────────────

@pytest.mark.unit
class TestTheLateChargerIsRead:

    @pytest.mark.asyncio
    async def test_its_power_sensor_is_saved(self):
        card = await _late_retry()
        assert card["ev_charging_power_sensor"] == POWER
        assert card["ev_connected_sensor"] == PLUG
        assert card["ev_charging_sensor"] == CHARGING

    @pytest.mark.asyncio
    async def test_the_car_is_not_house_load(self):
        reader, device = await _saved()
        readings = reader.read_power()
        assert readings.ev_power == EV_W
        assert _home(readings) == SOLAR_W - EV_W
        # bug class 120: the charger this read covers is out of ``home``,
        # so the baseload does not take it away a second time.
        assert chargers_outside_home([device], readings) == [device]

    @pytest.mark.asyncio
    async def test_the_charger_sees_its_own_draw(self):
        """``build_view`` gives the only charger the fleet figure; before
        #1125 that was 0 W, so the charger never saw the car drawing."""
        reader, _ = await _saved()
        view = build_charger_view(
            FleetCycleState(power=reader.read_power(), config={},
                            is_night=False, tariff_level=None,
                            forecast_remaining_kwh=0.0),
            charger_id="ev_charger", charger_cfg={}, mode="solar_only",
            daily_ev_kwh=0.0)
        assert view.power.power_w == EV_W

    @pytest.mark.asyncio
    async def test_a_charging_flag_with_no_draw_is_not_a_charge(self):
        """The 500 W floor (#739) gates the charging flag on the power."""
        reader, _ = await _saved(states={POWER: _state(100.0)})
        assert reader.read_power().ev_charging is False

    @pytest.mark.asyncio
    async def test_a_find_with_no_power_sensor_is_not_saved(self):
        """The twin: what the old hand-over gave (plug and charging, no
        power) is not a charger SEM can run, so nothing is saved and the
        retry tries again; the car is never quietly left in the house."""
        old = {k: v for k, v in KEBA.items() if k != "ev_charging_power_sensor"}
        assert await _late_retry(old) is None

    @pytest.mark.asyncio
    async def test_on_the_energy_dashboard_path_too(self):
        """An Energy Dashboard with solar and grid but no EV sensor — the
        shape the issue names."""
        ed = EnergyDashboardConfig(solar_power=PV, solar_power_list=[PV],
                                   has_solar=True)
        reader, _ = await _saved({}, dashboard=ed)
        readings = reader.read_power()
        assert readings.ev_power == EV_W
        assert _home(readings) == SOLAR_W - EV_W


# ───────────────────────────────────────────────────────────────────────
# 2. no second writer of the reader config
# ───────────────────────────────────────────────────────────────────────

@pytest.mark.unit
class TestOneWriter:

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
