"""#1022 — the PV health rule, with no Home Assistant in it.

Seven settled days of yield against forecast give one colour. Fewer than
three settled days give nothing. Snow needs freezing weather, a forecast
that says the plant should produce, and a plant that does not — for two
hours, not one cloud.
"""
from custom_components.solar_energy_management.analytics.pv_health import (
    FULL_YIELD_RATIO, MIN_SETTLED_DAYS, THRESHOLDS, PvHealth, pv_health,
)


def _days(*ratios, forecast=20.0):
    """(forecast_kwh, actual_kwh) pairs, oldest first."""
    return [(forecast, forecast * r) for r in ratios]


class TestColour:
    def test_thresholds_are_the_agreed_ones(self):
        assert THRESHOLDS == (("green", 0.85), ("yellow", 0.65), ("orange", 0.40))
        assert FULL_YIELD_RATIO == 0.90
        assert MIN_SETTLED_DAYS == 3

    def test_green_from_85_percent(self):
        h = pv_health(_days(0.9, 0.8, 0.85), 0, None, 0, 0, None)
        assert h.state == "green"
        assert h.ratio_7d == 0.85

    def test_yellow_orange_red(self):
        assert pv_health(_days(0.7, 0.7, 0.7), 0, None, 0, 0, None).state == "yellow"
        assert pv_health(_days(0.5, 0.5, 0.5), 0, None, 0, 0, None).state == "orange"
        assert pv_health(_days(0.1, 0.2, 0.3), 0, None, 0, 0, None).state == "red"

    def test_the_ratio_is_energy_weighted_not_a_mean_of_ratios(self):
        # a big day at 100 % and a tiny day at 0 % is nearly 100 %, not 50 %
        h = pv_health([(30.0, 30.0), (1.0, 0.0), (30.0, 30.0)], 0, None, 0, 0, None)
        assert h.ratio_7d > 0.95

    def test_fewer_than_three_settled_days_is_no_verdict(self):
        h = pv_health(_days(0.9, 0.9), 0, None, 0, 0, None)
        assert h.state is None
        assert h.settled_days == 2
        assert h.ratio_7d is None

    def test_only_the_last_seven_days_count(self):
        ratios = [0.0] * 5 + [1.0] * 7
        h = pv_health(_days(*ratios), 0, None, 0, 0, None)
        assert h.state == "green"
        assert h.settled_days == 7

    def test_a_day_without_a_forecast_or_actual_is_skipped(self):
        h = pv_health([(20.0, 18.0), (None, 5.0), (20.0, None), (20.0, 18.0), (20.0, 18.0)],
                      0, None, 0, 0, None)
        assert h.settled_days == 3
        assert h.state == "green"


class TestDaysSinceFullYield:
    def test_counts_back_to_the_last_full_day(self):
        h = pv_health(_days(0.95, 0.5, 0.6, 0.7), 0, None, 0, 0, None)
        assert h.days_since_full_yield == 3

    def test_zero_when_the_newest_day_was_full(self):
        h = pv_health(_days(0.5, 0.5, 0.95), 0, None, 0, 0, None)
        assert h.days_since_full_yield == 0

    def test_none_when_no_day_was_full(self):
        h = pv_health(_days(0.5, 0.5, 0.5), 0, None, 0, 0, None)
        assert h.days_since_full_yield is None


class TestSnow:
    def test_freezing_and_dark_under_a_bright_forecast_for_two_hours(self):
        h = pv_health(_days(0.9, 0.9, 0.9), 0, True, 1500.0, 20.0, 7200.0)
        assert h.snow is True

    def test_not_after_one_hour(self):
        h = pv_health(_days(0.9, 0.9, 0.9), 0, True, 1500.0, 20.0, 3600.0)
        assert h.snow is False

    def test_not_when_the_plant_produces(self):
        h = pv_health(_days(0.9, 0.9, 0.9), 0, True, 1500.0, 400.0, 7200.0)
        assert h.snow is False

    def test_not_when_the_forecast_is_dim(self):
        h = pv_health(_days(0.9, 0.9, 0.9), 0, True, 300.0, 0.0, 7200.0)
        assert h.snow is False

    def test_never_without_a_temperature_source(self):
        h = pv_health(_days(0.9, 0.9, 0.9), 0, None, 1500.0, 0.0, 7200.0)
        assert h.snow is False

    def test_not_above_freezing(self):
        h = pv_health(_days(0.9, 0.9, 0.9), 0, False, 1500.0, 0.0, 7200.0)
        assert h.snow is False


class TestShape:
    def test_downtime_rides_through_rounded(self):
        h = pv_health(_days(0.9, 0.9, 0.9), 12.49, None, 0, 0, None)
        assert h.downtime_min_today == 12
        assert isinstance(h, PvHealth)
        assert h.as_attributes() == {
            "ratio_7d": 0.9, "settled_days": 3, "downtime_min_today": 12,
            "days_since_full_yield": 0, "snow": False,
        }
