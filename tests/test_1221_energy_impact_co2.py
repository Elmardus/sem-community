"""(#1221) The CO2 sensor publishes the two numbers behind its figure.

The Energy Impact card worked "CO2 today" out again on its own:
(solar → home + solar → EV) × 128 g/kWh. ``sensor.sem_daily_co2_avoided``
counts all solar used on site — also what went into the battery — and adds
the live grid value when that is on. On a day the battery charged from the
sun, the card showed less than the sensor.

The card now shows the sensor's state. The line under it needs the kWh and
the kg per kWh the sensor used, so the sensor publishes them:
``solar_used_kwh`` and ``co2_kg_per_kwh_today`` (the day's average once the
live value counts; with no kWh yet, the rate a kWh counts at now).
"""
from __future__ import annotations

from unittest.mock import MagicMock, Mock

import pytest
from freezegun import freeze_time

from custom_components.solar_energy_management.coordinator.energy_calculator import (
    CO2_CORRECTION_CATEGORY,
    GRID_CO2_KG_PER_KWH,
    EnergyCalculator,
)
from custom_components.solar_energy_management.coordinator.types import (
    CostData,
    PowerReadings,
    SEMData,
)
from custom_components.solar_energy_management.utils.time_manager import TimeManager


def _calc() -> EnergyCalculator:
    hass = Mock()
    hass.states.get = lambda _eid: None
    return EnergyCalculator({"update_interval": 3600}, TimeManager(hass))


def _hour(calc, *, solar=0.0, home=0.0, battery=0.0, grid=0.0):
    """One cycle of one hour. Signs as SEM holds them: grid + is export,
    battery + is charge."""
    calc._last_update = None
    r = PowerReadings(solar_power=solar, battery_power=battery, grid_power=grid)
    r.calculate_derived()
    r.home_consumption_power = home
    return calc.calculate_costs(calc.calculate_energy(r))


# A sunny hour: 5 kW of sun, 1 kW to the house, 3 kW into the battery,
# 1 kW out to the grid. Solar used on site: 4 kWh, 3 of them in the battery.
SUNNY = dict(solar=5000.0, home=1000.0, battery=3000.0, grid=1000.0)


@pytest.mark.unit
@freeze_time("2026-10-28 12:00:00")
class TestTheNumbersBehindTheFigure:

    def test_solar_into_the_battery_is_in_the_kwh(self):
        costs = _hour(_calc(), **SUNNY)
        assert costs.daily_co2_solar_used_kwh == pytest.approx(4.0)
        assert costs.daily_co2_kg_per_kwh == pytest.approx(GRID_CO2_KG_PER_KWH)
        assert costs.daily_co2_avoided_kg == pytest.approx(round(4.0 * GRID_CO2_KG_PER_KWH, 2))

    def test_the_two_numbers_give_the_figure(self):
        """What the card prints under the figure must multiply to it."""
        costs = _hour(_calc(), **SUNNY)
        assert costs.daily_co2_solar_used_kwh * costs.daily_co2_kg_per_kwh == pytest.approx(
            costs.daily_co2_avoided_kg, abs=0.01)

    def test_live_value_on_gives_the_days_average(self):
        calc = _calc()
        calc.set_grid_co2_intensity(0.096, live=True)
        costs = _hour(calc, **SUNNY)
        assert costs.daily_co2_avoided_kg == pytest.approx(round(4.0 * 0.096, 2))
        assert costs.daily_co2_kg_per_kwh == pytest.approx(0.096)

    def test_a_day_at_two_rates_shows_its_average(self):
        """One hour at the fixed 128 g, one at a live 64 g: 96 g a kWh."""
        calc = _calc()
        _hour(calc, **SUNNY)
        calc.set_grid_co2_intensity(0.064, live=True)
        costs = _hour(calc, **SUNNY)
        assert costs.daily_co2_solar_used_kwh == pytest.approx(8.0)
        assert costs.daily_co2_kg_per_kwh == pytest.approx(0.096)
        assert costs.daily_co2_solar_used_kwh * costs.daily_co2_kg_per_kwh == pytest.approx(
            costs.daily_co2_avoided_kg, abs=0.01)

    @pytest.mark.parametrize("live,reading,rate", [
        (False, 0.096, GRID_CO2_KG_PER_KWH),
        (True, 0.096, 0.096),
        (True, None, GRID_CO2_KG_PER_KWH),
    ])
    def test_no_solar_yet_shows_the_rate_a_kwh_counts_at_now(self, live, reading, rate):
        calc = _calc()
        calc.set_grid_co2_intensity(reading, live=live)
        costs = _hour(calc, home=500.0, grid=-500.0)
        assert costs.daily_co2_solar_used_kwh == 0.0
        assert costs.daily_co2_kg_per_kwh == pytest.approx(rate)

    def test_a_kwh_that_shows_0_0_shows_the_rate_now(self):
        """40 Wh of sun after a live correction booked earlier today: the
        day's average would read 1378 g/kWh beside "0.0 kWh"."""
        from datetime import date
        calc = _calc()
        today = date(2026, 10, 28)
        calc._accumulate(CO2_CORRECTION_CATEGORY, today, "2026_10", "2026", 0.05)
        costs = _hour(calc, solar=40.0, home=40.0)
        assert costs.daily_co2_solar_used_kwh == 0.0
        assert costs.daily_co2_avoided_kg > 0
        assert costs.daily_co2_kg_per_kwh == pytest.approx(GRID_CO2_KG_PER_KWH)

    def test_the_rate_is_published_at_what_the_card_shows(self):
        """1 g/kWh: a finer rate moves each cycle with the live value on
        and writes a state row the card never shows."""
        calc = _calc()
        calc.set_grid_co2_intensity(0.09617, live=True)
        costs = _hour(calc, **SUNNY)
        assert costs.daily_co2_kg_per_kwh == round(costs.daily_co2_kg_per_kwh, 3)

    def test_a_clean_grid_floors_at_zero(self):
        """0 g live: the figure is 0, and so is its rate — never negative."""
        calc = _calc()
        calc.set_grid_co2_intensity(0.0, live=True)
        costs = _hour(calc, **SUNNY)
        assert costs.daily_co2_avoided_kg == 0.0
        assert costs.daily_co2_kg_per_kwh == 0.0


@pytest.mark.unit
class TestPublished:

    def test_unknown_before_the_first_cost_pass(self):
        costs = CostData()
        assert costs.daily_co2_solar_used_kwh is None
        assert costs.daily_co2_kg_per_kwh is None

    def test_the_data_set_carries_them(self):
        data = SEMData()
        data.costs.daily_co2_solar_used_kwh = 17.7
        data.costs.daily_co2_kg_per_kwh = 0.128
        d = data.to_dict()
        assert d["daily_co2_solar_used_kwh"] == 17.7
        assert d["daily_co2_kg_per_kwh"] == 0.128

    def test_the_sensor_shows_them(self):
        from custom_components.solar_energy_management.sensor import SEMSolarSensor
        me = MagicMock()
        me.entity_description.key = "daily_co2_avoided"
        me.coordinator._grid_co2_status = {"source": "none", "kg_per_kwh": None}
        me.coordinator.config = {}
        me.coordinator.data = {"daily_co2_avoided": 2.27,
                               "daily_co2_solar_used_kwh": 17.7,
                               "daily_co2_kg_per_kwh": 0.128}
        attrs = SEMSolarSensor._extra_state_attributes_base(me)
        assert attrs["solar_used_kwh"] == 17.7
        assert attrs["co2_kg_per_kwh_today"] == 0.128
        assert attrs["co2_fallback_kg_per_kwh"] == GRID_CO2_KG_PER_KWH

    def test_they_stay_out_of_the_recorder(self):
        from custom_components.solar_energy_management.sensor import SEMSolarSensor
        for name in ("solar_used_kwh", "co2_kg_per_kwh_today"):
            assert name in SEMSolarSensor._unrecorded_attributes, name
