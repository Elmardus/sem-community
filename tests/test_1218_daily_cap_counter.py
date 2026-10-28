"""#1218 — a counter step above the battery's daily cap grew the month.

The battery rows have a daily cap: 3 × the battery size. The counter
correction (``_reconcile_metered_energy``) checked only the 1,000 kWh unit
trap. A counter step above the cap was adopted: the day and the month, year
and lifetime moved by it. The cap then wrote the day back alone. On the next
cycle the step was there again, and it was added again — 30 cycles of a
100 kWh step on a 10 kWh battery left the lifetime at 2,130 kWh. The day's
savings grew without end too: the correction prices its step at the day's own
rate, which the step itself had raised.

The cure: one ceiling per row (``_daily_ceiling``), read by the cap and by
every counter correction. A correction never adopts what the cap would cut.
"""
from __future__ import annotations

import ast
import logging
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from freezegun import freeze_time

from custom_components.solar_energy_management.coordinator.energy_calculator import (
    MAX_PLAUSIBLE_DAILY_KWH,
    EnergyCalculator,
)
from custom_components.solar_energy_management.coordinator.flow_calculator import (
    FlowCalculator,
)
from custom_components.solar_energy_management.coordinator.types import (
    PowerReadings,
)
from custom_components.solar_energy_management.utils.time_manager import TimeManager

REPO = Path(__file__).resolve().parents[1]
DAY = date(2026, 10, 28)
NOON = "2026-10-28 12:00:00"
MONTH = f"{DAY.year}_{DAY.month}"
YEAR = str(DAY.year)
STEP_S = 60
CAPACITY = 10.0                 # kWh → a 30 kWh daily cap
KWP = 10.0                      # kWp → a 160 kWh daily cap
COUNTER = {
    "battery_charge": "sensor.bms_charged",
    "battery_discharge": "sensor.bms_discharged",
    "grid_import": "sensor.p1_imported",
    "grid_export": "sensor.p1_exported",
    "solar": "sensor.inverter_yield",
}


def _calc(categories, counter: dict) -> EnergyCalculator:
    """A calculator whose ``categories`` rows follow their counters."""
    def _get(entity_id):
        if entity_id not in counter:
            return None
        state = MagicMock()
        state.state = str(counter[entity_id])
        state.attributes = {"unit_of_measurement": "kWh"}
        return state
    hass = MagicMock()
    hass.states.get = _get
    calc = EnergyCalculator(
        {"update_interval": STEP_S, "battery_capacity_kwh": CAPACITY,
         "system_size_kwp": KWP},
        TimeManager(hass))
    calc._import_rate = 0.30
    meters = {c: [COUNTER[c]] for c in categories if c != "solar"}
    if meters:
        calc.configure_meter_counters(hass, meters, True)
    if "solar" in categories:
        calc.configure_solar_counters(hass, [COUNTER["solar"]], True)
    return calc


def _read(*, solar=0.0, home=0.0, battery=0.0, grid=0.0) -> PowerReadings:
    """Signs as SEM holds them: grid + is export, battery − is discharge."""
    p = PowerReadings(solar_power=solar, battery_power=battery, grid_power=grid)
    p.calculate_derived()
    p.home_consumption_power = home
    return p


# Every row busy at once: sun, a charging pack and an export by day…
DAYTIME = _read(solar=4000.0, home=500.0, battery=1500.0, grid=2000.0)
# …and a discharging pack beside an import.
EVENING = _read(solar=0.0, home=2500.0, battery=-1500.0, grid=-1000.0)


def _cycle(calc: EnergyCalculator, power: PowerReadings):
    calc._last_update = None
    return calc.calculate_energy(
        power, FlowCalculator().calculate_power_flows(power))


def _kwh(watts: float, cycles: int = 1) -> float:
    return watts * STEP_S * cycles / 3_600_000


def _rows(calc: EnergyCalculator, category: str) -> tuple:
    lifetime = calc._lifetime_accumulators.get(f"lifetime_{category}", 0.0)
    return (calc._daily_accumulators.get(f"{category}_{DAY}", 0.0),
            calc._monthly_accumulators.get(f"{category}_{MONTH}", 0.0),
            calc._yearly_accumulators.get(f"{category}_{YEAR}", 0.0),
            lifetime)


@pytest.fixture
def noon():
    with freeze_time(NOON) as frozen:
        yield frozen


def _power_for(category: str) -> PowerReadings:
    return EVENING if category == "battery_discharge" else DAYTIME


@pytest.mark.unit
@pytest.mark.usefixtures("noon")
class TestTheReportedShape:
    """A 100 kWh counter step on a 10 kWh battery, 30 cycles."""

    def _run(self, category: str, step: float = 100.0, cycles: int = 30):
        counter = {COUNTER[category]: 1000.0}
        calc = _calc([category], counter)
        power = _power_for(category)
        _cycle(calc, power)                     # the counter's baseline
        counter[COUNTER[category]] += step
        for _ in range(cycles):
            _cycle(calc, power)
        return calc

    @pytest.mark.parametrize("category", ["battery_charge", "battery_discharge"])
    def test_the_longer_rows_hold_the_day(self, category):
        calc = self._run(category)
        day, month, year, lifetime = _rows(calc, category)
        assert month == pytest.approx(day)
        assert year == pytest.approx(day)
        assert lifetime == pytest.approx(day)

    @pytest.mark.parametrize("category", ["battery_charge", "battery_discharge"])
    def test_the_day_keeps_what_the_power_read(self, category):
        """The step is refused: the row runs on the power sensor."""
        calc = self._run(category)
        assert _rows(calc, category)[0] == pytest.approx(_kwh(1500.0, 31))

    def test_the_savings_stay_the_powers(self):
        """The old way the day's battery savings reached 4.9e16."""
        calc = self._run("battery_discharge")
        savings = calc._daily_cost_accumulators.get(
            f"cost_batt_savings_{DAY}", 0.0)
        assert 0.0 < savings <= _kwh(1500.0, 31) * 0.30 + 1e-9

    @pytest.mark.parametrize("category", ["battery_charge", "battery_discharge"])
    def test_a_step_inside_the_ceiling_is_still_followed(self, category):
        """The twin: the cap must not switch the counter off."""
        calc = self._run(category, step=5.0)
        day, month, year, lifetime = _rows(calc, category)
        assert day == pytest.approx(5.0 + _kwh(1500.0), abs=0.06)
        assert month == year == lifetime == pytest.approx(day)

    def test_a_step_that_goes_away_is_followed_again(self):
        """A spike: the counter comes back, re-baselines and is followed."""
        counter = {COUNTER["battery_charge"]: 1000.0}
        calc = _calc(["battery_charge"], counter)
        _cycle(calc, DAYTIME)
        counter[COUNTER["battery_charge"]] += 100.0
        for _ in range(5):
            _cycle(calc, DAYTIME)
        counter[COUNTER["battery_charge"]] -= 100.0
        _cycle(calc, DAYTIME)                   # the reset: a new baseline
        before = _rows(calc, "battery_charge")[0]
        counter[COUNTER["battery_charge"]] += 4.0
        _cycle(calc, DAYTIME)
        day, month, year, lifetime = _rows(calc, "battery_charge")
        assert day == pytest.approx(before + 4.0, abs=0.06)
        assert month == year == lifetime == pytest.approx(day)

    def test_one_warning_a_day(self, caplog):
        with caplog.at_level(logging.WARNING):
            self._run("battery_charge")
        refused = [r for r in caplog.records
                   if "sanity ceiling" in r.getMessage()]
        assert len(refused) == 1
        assert "battery_charge" in refused[0].getMessage()
        assert not [r for r in caplog.records if "capping" in r.getMessage()]


@pytest.mark.unit
@pytest.mark.usefixtures("noon")
class TestTheRowsMoveTogether:
    """The guard for the class: whatever a counter does, a cycle moves the
    day, month, year and lifetime by the same amount — for every row a
    counter corrects, with every counter at once."""

    def _drive(self, step: float, cycles: int = 20):
        counter = {COUNTER[c]: 1000.0 for c in COUNTER}
        calc = _calc(list(COUNTER), counter)
        _cycle(calc, DAYTIME)
        for n in range(cycles):
            for entity in counter:
                counter[entity] += step if n == 0 else 0.01
            _cycle(calc, DAYTIME if n % 2 else EVENING)
        return calc

    @pytest.mark.parametrize("step", [
        2.0,                        # inside every ceiling
        100.0,                      # above the battery's, inside solar's
        500.0,                      # above solar's too
        2 * MAX_PLAUSIBLE_DAILY_KWH,  # above the grid's
    ])
    def test_month_year_and_lifetime_hold_the_day(self, step):
        calc = self._drive(step)
        for category in COUNTER:
            day, month, year, lifetime = _rows(calc, category)
            assert (month, year, lifetime) == pytest.approx(
                (day, day, day)), category
        for category in ("battery_charge", "battery_discharge", "solar"):
            # the rows with a cap: the day is held to it
            assert _rows(calc, category)[0] <= calc._daily_ceiling(category)


@pytest.mark.unit
class TestOneCeiling:

    def test_the_figures(self):
        calc = EnergyCalculator(
            {"battery_capacity_kwh": CAPACITY, "system_size_kwp": KWP},
            MagicMock())
        assert calc._daily_ceiling("battery_charge") == 30.0
        assert calc._daily_ceiling("battery_discharge") == 30.0
        assert calc._daily_ceiling("solar") == 160.0
        assert calc._daily_ceiling("grid_import") == MAX_PLAUSIBLE_DAILY_KWH
        assert calc._daily_ceiling("grid_export") == MAX_PLAUSIBLE_DAILY_KWH

    def test_the_defaults_are_the_caps_old_ones(self):
        calc = EnergyCalculator({}, MagicMock())
        assert calc._daily_ceiling("battery_charge") == 15 * 3
        assert calc._daily_ceiling("solar") == 10 * 16

    def test_every_correction_and_the_cap_read_it(self):
        """A correction that checks a looser figure than the cap is how the
        month ran away: each must ask ``_daily_ceiling``."""
        tree = ast.parse((REPO / "coordinator/energy_calculator.py")
                         .read_text(encoding="utf-8"))
        methods = {n.name: n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef)}
        for name in ("_reconcile_metered_energy", "_reconcile_solar_energy",
                     "calculate_energy"):
            calls = [n for n in ast.walk(methods[name])
                     if isinstance(n, ast.Call)
                     and getattr(n.func, "attr", None) == "_daily_ceiling"]
            assert calls, f"{name} does not read _daily_ceiling"
