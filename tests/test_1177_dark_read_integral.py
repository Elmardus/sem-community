"""#1177 — a dark power read is not 0 W of energy.

PROD (Huawei SUN2000 + LUNA2000 over modbus): the inverter's entities go
``unavailable`` ~27 times an hour, 26 % of the time at noon on 23.10.2026.
The reader puts 0 W in a dark input (#818) — right for steering, wrong for an
integral (bug class 58). The daily solar row booked every dark cycle as no
production: 9.86 kWh against 14.78 kWh of held input power that day, and on
most days exactly the "dark = 0" integral.

The battery, grid and export rows follow their meters (#628), so they kept the
energy solar lost. Home is the residual of those rows, so it went DOWN in every
hour where solar's loss outran the house: 23 hours on 15 days, 0.5–2.4 kWh. In
22 of the 23 hours the balance is positive again with the inverter's hourly
mean power in place of the solar row.

The cure is the one class 58 names for an integral: book the last live value
while the input is dark, for the same ``SENSOR_DARK_READ_GRACE_S`` its entity
shows it. Then the daily row is the integral of the power entity SEM shows.
"""
from __future__ import annotations

import ast
import inspect
import textwrap
from datetime import date, timedelta
from unittest.mock import Mock

import pytest
from freezegun import freeze_time

from custom_components.solar_energy_management.consts.core import (
    SENSOR_DARK_READ_GRACE_S,
)
from custom_components.solar_energy_management.coordinator import (
    energy_calculator as ec,
)
from custom_components.solar_energy_management.coordinator.energy_calculator import (
    EnergyCalculator,
)
from custom_components.solar_energy_management.coordinator.types import PowerReadings
from custom_components.solar_energy_management.utils.time_manager import TimeManager

DAY = date(2026, 10, 23)
NOON = "2026-10-23 12:00:00"
STEP_S = 10

SOLAR = "sensor.inverter_gesamtenergieertrag"
IMPORT = "sensor.power_meter_verbrauch"
EXPORT = "sensor.power_meter_exportierte_energie"
BATT_IN = "sensor.battery_1_gesamtladung"
BATT_OUT = "sensor.battery_1_gesamtentladung"
METERS = {
    "solar": SOLAR,
    "grid_import": IMPORT,
    "grid_export": EXPORT,
    "battery_charge": BATT_IN,
    "battery_discharge": BATT_OUT,
}


def _hass(values: dict) -> Mock:
    hass = Mock()
    hass.data = {}
    hass.config = Mock()
    hass.config.config_dir = "/config"

    def _get(entity_id):
        if entity_id not in values:
            return None
        state = Mock()
        state.state = str(values[entity_id])
        state.attributes = {"unit_of_measurement": "kWh"}
        return state

    hass.states.get = _get
    return hass


def _calc(rows=None, *, meters: bool = False):
    """A calculator at noon. With ``meters`` every row is counter-backed,
    the PROD shape: the home row is then the balance of the others."""
    values = {m: 0.0 for m in METERS.values()}
    hass = _hass(values)
    calc = EnergyCalculator({"update_interval": STEP_S}, TimeManager(hass))
    calc.configure_solar_counters(hass, [SOLAR] if meters else [], meters)
    calc.configure_meter_counters(
        hass,
        {c: [m] for c, m in METERS.items() if c != "solar"} if meters else {},
        meters,
    )
    for category, kwh in (rows or {}).items():
        calc._daily_accumulators[f"{category}_{DAY}"] = kwh
    calc._midnight_ev_since = DAY - timedelta(days=1)
    return calc, values


def _cycle(calc, **power):
    """One cycle of ``STEP_S`` seconds; ``_last_update`` cleared so the
    configured interval is used whatever the frozen clock says."""
    calc._last_update = None
    readings = PowerReadings(**{k: v for k, v in power.items()
                                if k != "home_shown_w"})
    readings.calculate_derived()
    if "home_shown_w" in power:
        readings.home_shown_w = power["home_shown_w"]
    return calc.calculate_energy(readings)


def _kwh(watts: float, cycles: int = 1) -> float:
    return watts * STEP_S * cycles / 3_600_000


def _row(calc, category: str) -> float:
    """The day's raw accumulator — the published rows are rounded to 0.01."""
    return calc._daily_accumulators.get(f"{category}_{DAY}", 0.0)


def _dark_solar(**extra):
    return dict(solar_power=0.0, solar_power_unavailable=True, **extra)


@pytest.fixture
def clock(request):
    """The frozen noon, moved by ``clock.tick``."""
    with freeze_time(NOON) as frozen:
        request.instance.clock = frozen
        yield frozen


@pytest.mark.unit
@pytest.mark.usefixtures("clock")
class TestADarkReadKeepsTheLastLiveValue:
    def test_dark_solar_books_the_last_live_value(self):
        calc, _ = _calc()
        _cycle(calc, solar_power=3000.0)
        _cycle(calc, **_dark_solar())
        assert _row(calc, "solar") == pytest.approx(_kwh(3000.0, 2))

    def test_a_live_zero_stays_zero(self):
        """Only a dark read is held. A live 0 W is a measurement — and the
        value held after it."""
        calc, _ = _calc()
        _cycle(calc, solar_power=3000.0)
        _cycle(calc, solar_power=0.0)
        _cycle(calc, **_dark_solar())
        assert _row(calc, "solar") == pytest.approx(_kwh(3000.0))

    def test_never_read_holds_nothing(self):
        calc, _ = _calc()
        _cycle(calc, **_dark_solar())
        assert _row(calc, "solar") == 0.0

    def test_the_hold_ends_with_the_dark_read_grace(self):
        calc, _ = _calc()
        _cycle(calc, solar_power=3000.0)
        self.clock.tick(timedelta(seconds=SENSOR_DARK_READ_GRACE_S))
        _cycle(calc, **_dark_solar())
        at_edge = _row(calc, "solar")
        assert at_edge == pytest.approx(_kwh(3000.0, 2)), "held at the edge"
        self.clock.tick(timedelta(seconds=1))
        _cycle(calc, **_dark_solar())
        assert _row(calc, "solar") == pytest.approx(at_edge), "held past the grace"

    def test_the_age_counts_from_the_last_live_read(self):
        """A long blank is not renewed by its own dark cycles."""
        calc, _ = _calc()
        _cycle(calc, solar_power=3000.0)
        for _ in range(SENSOR_DARK_READ_GRACE_S // STEP_S + 3):
            self.clock.tick(timedelta(seconds=STEP_S))
            _cycle(calc, **_dark_solar())
        held = SENSOR_DARK_READ_GRACE_S // STEP_S
        assert _row(calc, "solar") == pytest.approx(_kwh(3000.0, 1 + held))

    def test_dark_grid_holds_import_and_export(self):
        calc, _ = _calc()
        _cycle(calc, grid_power=-2000.0)            # import
        _cycle(calc, grid_power=0.0, grid_power_unavailable=True)
        assert _row(calc, "grid_import") == pytest.approx(_kwh(2000.0, 2))
        calc, _ = _calc()
        _cycle(calc, grid_power=1500.0)             # export
        _cycle(calc, grid_power=0.0, grid_power_unavailable=True)
        assert _row(calc, "grid_export") == pytest.approx(_kwh(1500.0, 2))

    def test_dark_battery_holds_charge_and_discharge(self):
        calc, _ = _calc()
        _cycle(calc, battery_power=2500.0)          # charge
        _cycle(calc, battery_power=0.0, battery_power_all_unavailable=True)
        assert _row(calc, "battery_charge") == pytest.approx(_kwh(2500.0, 2))
        calc, _ = _calc()
        _cycle(calc, battery_power=-1200.0)         # discharge
        _cycle(calc, battery_power=0.0, battery_power_all_unavailable=True)
        assert _row(calc, "battery_discharge") == pytest.approx(_kwh(1200.0, 2))

    def test_each_input_on_its_own(self):
        """A dark solar does not hold the live grid."""
        calc, _ = _calc()
        _cycle(calc, solar_power=3000.0, grid_power=-500.0)
        _cycle(calc, solar_power=0.0, solar_power_unavailable=True,
               grid_power=-900.0)
        assert _row(calc, "solar") == pytest.approx(_kwh(3000.0, 2))
        assert _row(calc, "grid_import") == pytest.approx(
            _kwh(500.0) + _kwh(900.0))

    def test_the_home_row_books_the_home_sem_shows(self):
        """(#1047) While an input is dark the coordinator shows the last
        house value; the raw home is built from the dark input's 0 W."""
        calc, _ = _calc()
        _cycle(calc, solar_power=3000.0, battery_power=2400.0)    # 600 W
        _cycle(calc, solar_power=0.0, solar_power_unavailable=True,
               battery_power=2400.0, home_shown_w=600.0)
        assert _row(calc, "home") == pytest.approx(_kwh(600.0, 2))

    def test_the_cost_follows_the_booked_energy(self):
        calc, _ = _calc()
        calc._import_rate = 0.30
        _cycle(calc, grid_power=-2000.0)
        _cycle(calc, grid_power=0.0, grid_power_unavailable=True)
        assert calc._daily_cost_accumulators[f"cost_import_{DAY}"] == (
            pytest.approx(_kwh(2000.0, 2) * 0.30))


@pytest.mark.unit
@pytest.mark.usefixtures("clock")
class TestTheHouseDoesNotGoDown:
    """The PROD hour: PV 4 kW, the pack takes 3.5 kW, the house 0.5 kW, and
    the inverter blinks dark one cycle in four. The pack's meter counts every
    kWh; before the fix solar lost a quarter of its, and home — the balance —
    fell 0.5 kWh an hour."""

    PV_W, CHARGE_W, HOUSE_W = 4000.0, 3500.0, 500.0
    ROWS = {"solar": 12.0, "grid_import": 3.0, "grid_export": 0.0,
            "battery_charge": 5.0, "battery_discharge": 0.0, "home": 10.0}

    def _hour(self):
        calc, values = _calc(dict(self.ROWS), meters=True)
        homes = []
        for i in range(360):
            self.clock.tick(timedelta(seconds=STEP_S))
            # the hybrid's AC yield counter trails PV while the pack charges
            values[SOLAR] += _kwh(self.HOUSE_W)
            values[BATT_IN] += _kwh(self.CHARGE_W)
            dark = i % 4 == 3
            energy = _cycle(
                calc,
                solar_power=0.0 if dark else self.PV_W,
                solar_power_unavailable=dark,
                battery_power=self.CHARGE_W,
            )
            assert energy.daily_home == round(_row(calc, "home"), 2)
            homes.append(_row(calc, "home"))
        return homes, _row(calc, "solar")

    def test_the_home_row_never_goes_down(self):
        homes, _ = self._hour()
        drops = [b - a for a, b in zip(homes, homes[1:], strict=False) if b < a - 1e-9]
        assert not drops, f"home went down {len(drops)} times, by {min(drops):.3f}"
        assert homes[-1] - homes[0] == pytest.approx(
            _kwh(self.HOUSE_W, 359), abs=0.06)

    def test_the_solar_row_is_the_pv(self):
        _, solar = self._hour()
        assert solar - self.ROWS["solar"] == pytest.approx(
            _kwh(self.PV_W, 360), abs=0.01)


def _w_state(value):
    import homeassistant.util.dt as dt_util
    s = Mock()
    s.state = str(value)
    s.attributes = {"unit_of_measurement": "W"}
    s.last_updated = s.last_reported = dt_util.utcnow()
    return s


@pytest.mark.unit
@pytest.mark.usefixtures("clock")
class TestThroughTheRealReader:
    """The flags are the reader's own, not ones the test hands in: a dark
    inverter through ``SensorReader.read_power`` and the coordinator's home
    steps (#1047 PROD 04.10 15:25) into the integral."""

    def test_a_dark_inverter_keeps_its_energy(self):
        from custom_components.solar_energy_management.coordinator.coordinator import (
            SEMCoordinator,
        )
        from custom_components.solar_energy_management.coordinator.sensor_reader import (
            SensorReader,
        )
        values = {"sensor.pv": "4596", "sensor.grid": "6", "sensor.bat": "3916"}
        hass = Mock()
        hass.states.get = lambda eid: (
            _w_state(values[eid]) if eid in values else None)
        reader = SensorReader(hass, {
            "solar_production_sensor": "sensor.pv",
            "grid_power_sensor": "sensor.grid",
            "battery_power_sensor": "sensor.bat",
        })
        reader._energy_dashboard_config = None
        coord = SEMCoordinator.__new__(SEMCoordinator)
        clock = [1000.0]
        coord._shown_home_clock = lambda: clock[0]
        calc, _ = _calc()

        def cycle(**v):
            values.update(v)
            clock[0] += STEP_S
            self.clock.tick(timedelta(seconds=STEP_S))
            p = reader.read_power()
            coord._smooth_home_consumption(p)
            coord._hold_shown_home(p)
            calc._last_update = None
            calc.calculate_energy(p)
            return p

        cycle()
        dark = cycle(**{"sensor.pv": "unavailable"})
        assert dark.solar_power_unavailable and dark.solar_power == 0.0
        assert _row(calc, "solar") == pytest.approx(_kwh(4596.0, 2))
        assert _row(calc, "battery_charge") == pytest.approx(_kwh(3916.0, 2))
        assert _row(calc, "home") == pytest.approx(_kwh(674.0, 2), abs=1e-6)


@pytest.mark.unit
class TestEveryIntegralTakesTheHeldWatts:
    """Structural: ``calculate_energy`` books solar, grid, battery and home
    only from ``_integral_watts`` — a new line reading ``power.solar_power``
    for an integral would bring the 0 W back."""

    HELD = {"solar_power", "grid_import_power", "grid_export_power",
            "battery_charge_power", "battery_discharge_power",
            "home_consumption_power"}

    def test_no_held_field_is_read_off_power(self):
        src = textwrap.dedent(inspect.getsource(EnergyCalculator.calculate_energy))
        reads = sorted(
            node.attr for node in ast.walk(ast.parse(src))
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name) and node.value.id == "power"
            and node.attr in self.HELD
        )
        assert reads == [], f"read off power, not held: {reads}"

    def test_the_held_fields_are_the_integral_inputs(self):
        fields = {f for _, _, names in ec._INTEGRAL_INPUTS for f in names}
        assert fields | {"home_consumption_power"} == self.HELD

    def test_every_flag_is_one_the_reader_sets(self):
        flags = {flag for _, flag, _ in ec._INTEGRAL_INPUTS}
        assert flags <= set(PowerReadings.__dataclass_fields__)

    def test_the_walker_sees_a_read(self):
        """The pin above would pass on a walker that sees nothing."""
        probe = "def f(self, power):\n    x = power.solar_power\n"
        hits = [n.attr for n in ast.walk(ast.parse(probe))
                if isinstance(n, ast.Attribute)
                and isinstance(n.value, ast.Name) and n.value.id == "power"]
        assert hits == ["solar_power"]
