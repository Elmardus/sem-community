"""#1104 — a sunny morning pushed today's corrected solar far too high.

PROD 13.10.2026 10:00 (intraday flag on): sun 07:37–18:40, 3.64 kWh made,
forecast 20.2 kWh. The sine curve expects only ~11 % by 10:00, so the ratio
read 1.64, the 1.5 ceiling applied, and the plan saw 19.02 × 1.5 = 28.5 kWh
still to come. The day made 14.2 kWh in total. 09.10 the same: 22.4 → 29.1,
real 20.9. A clear morning does not promise a clear afternoon; the plan must
not book sun the provider never forecast."""
from datetime import datetime, timedelta
from unittest.mock import patch

from custom_components.solar_energy_management.coordinator.forecast_tracker import (
    ForecastTracker,
)

_NOW = ("custom_components.solar_energy_management.coordinator."
        "forecast_tracker.dt_util.now")


def _tracker(forecast, actual, now, sun=(7.62, 18.67), learned=0.98):
    t = ForecastTracker()
    t._today_forecast = forecast
    t._today_actual = actual
    t._correction_factor = learned
    t._get_sun_hours = lambda: sun
    for k in range(36):
        with patch(_NOW, return_value=now - timedelta(minutes=5 * (36 - k))):
            t._calculate_dampening_factor()
    return t


def test_prod_13_10_sunny_morning_does_not_raise_the_plan():
    now = datetime(2026, 10, 13, 10, 0)
    t = _tracker(20.2, 3.64, now)
    with patch(_NOW, return_value=now):
        assert t.corrected_remaining_kwh(19.02) <= 19.02


def test_prod_09_10_sunny_morning_does_not_raise_the_plan():
    now = datetime(2026, 10, 9, 10, 0)
    t = _tracker(23.7, 3.67, now)
    with patch(_NOW, return_value=now):
        assert t.corrected_remaining_kwh(22.4) <= 22.4


def test_grey_day_still_lowers():
    # PROD 10.10 10:00: raw 20.3, corrected 10.2, real 10.7 — keep this.
    now = datetime(2026, 10, 10, 12, 0)
    t = _tracker(20.3, 1.5, now)
    with patch(_NOW, return_value=now):
        assert t.corrected_remaining_kwh(15.0) < 15.0 * 0.6


def test_the_published_dampening_factor_still_raises():
    # The existing sensor keeps its behaviour; only the plan's path is capped.
    now = datetime(2026, 10, 13, 10, 0)
    t = _tracker(20.2, 3.64, now)
    with patch(_NOW, return_value=now):
        assert t.dampening_factor > 1.0


def test_a_provider_that_forecasts_low_keeps_its_learned_boost():
    """(Review) History says this provider forecasts 30 % low: the plan
    keeps that boost — only the morning's live misread is capped."""
    now = datetime(2026, 10, 13, 10, 0)
    t = _tracker(20.2, 3.64, now, learned=1.3)
    with patch(_NOW, return_value=now):
        r = t.corrected_remaining_kwh(10.0)
    assert 12.0 <= r <= 13.0
