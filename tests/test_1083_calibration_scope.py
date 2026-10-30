"""#1083 — the settings check judges days it can trust, says how many it
used and how the car days fare; the headline counts undelivered car energy."""
from types import SimpleNamespace

import pytest

from custom_components.solar_energy_management.analytics.simulation.hourly_day import (
    HourlyDay,
)
from custom_components.solar_energy_management.analytics.simulation.summary import (
    summarise,
)
from custom_components.solar_energy_management.coordinator.simulation_job import (
    Calibration,
)


def _own(actual, simulated, ev=0.0):
    return SimpleNamespace(
        actual_cost=actual, simulated_cost=simulated,
        actual={"import_cost": 2.0, "export_revenue": 0.0, "ev": ev})


def test_car_days_get_their_own_error_and_count():
    c = Calibration()
    c.add(_own(1.0, 1.0))
    c.add(_own(1.0, 1.4, ev=8.0))
    c.skip()
    out = c.as_dict()
    assert out["days"] == 2 and out["left_out"] == 1
    assert out["car_days"] == 1
    assert out["car_error_pct"] == pytest.approx(20.0)
    assert out["error_pct"] == pytest.approx(10.0)


def test_no_car_days_means_no_car_error():
    c = Calibration()
    c.add(_own(1.0, 1.0))
    assert c.as_dict()["car_error_pct"] is None


@pytest.mark.parametrize("note,jumped", [
    ("ev counter stepped back 15.39 kWh at 07:00 — netted", True),
    ("the counters disagree at 03:00 by 9.0 kWh — a counter jumped", True),
    ("the house and solar counters moved 1.2 kWh to close", False),
])
def test_a_counter_jump_leaves_the_day_out(note, jumped):
    day = HourlyDay.__new__(HourlyDay)
    day.notes = [note]
    assert HourlyDay.counter_jumped.fget(day) is jumped


def test_the_net_total_counts_car_energy_not_delivered():
    day = SimpleNamespace(
        actual_cost=3.0, simulated_cost=2.0, delta=-1.0, kwh_over_limit=0.0,
        indicative=False, pack_change_value=0.0, ev_not_delivered_value=0.6,
        tags=[], date="2026-10-01", indicative_reasons=[])
    tot = summarise([day])["overall"]
    assert tot["delta"] == pytest.approx(-1.0)
    assert tot["net_delta"] == pytest.approx(-0.4)
