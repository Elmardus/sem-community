"""#1132 — each away car's estimate dropped by all cars' EV use.

At each day rollover SEM lowers an away car's estimated charge level by its
predicted daily use (``_apply_daily_taper_decay``). The prediction learned
``energy.daily_ev``: the TOTAL of every charger. With two cars, 12 and 3 kWh
a day, both estimates dropped by ~15 kWh. When a car's own level sensor is
offline the night charge uses this estimate.

The fix learns each car's own closed EV day (the per-charger bucket as it
rolls over) and predicts per car. The fleet total stays the one car's use
on a one-charger install.
"""
from __future__ import annotations

import copy
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from custom_components.solar_energy_management.analytics.consumption_predictor import (
    ConsumptionPredictor,
)
from custom_components.solar_energy_management.coordinator.coordinator import (
    SEMCoordinator,
)
from custom_components.solar_energy_management.coordinator.ev_taper_detector import (
    EVTaperDetector,
)

_CONFIG = {
    "ev_battery_capacity_kwh": 60, "daily_ev_target": 10,
    "ev_chargers": [
        {"id": "car1", "ev_battery_capacity_kwh": 60, "daily_ev_target": 10},
        {"id": "car2", "ev_battery_capacity_kwh": 60, "daily_ev_target": 10},
    ],
}
MONDAY = datetime(2026, 10, 12, 7, 5)


def _learned(predictor, days=7):
    """A week of real days: car 1 uses 12 kWh, car 2 uses 3; the fleet
    total (what ``observe_ev`` sees) is 15."""
    for i in range(days):
        day = MONDAY + timedelta(days=i)
        predictor.observe_ev(day, 15.0)
        predictor.observe_car_ev(day, "car1", 12.0)
        predictor.observe_car_ev(day, "car2", 3.0)


def _away_det(cid):
    det = EVTaperDetector(copy.deepcopy(_CONFIG), charger_id=cid)
    det._last_full_timestamp = MONDAY.isoformat()
    det._soc_anchored = True
    return det


def _coord(predictor, cids):
    c = SimpleNamespace()
    c._ev_taper_detectors = {cid: _away_det(cid) for cid in cids}
    c._last_ev_connected_per_charger = {cid: False for cid in cids}
    c._predictor = predictor
    c._read_outdoor_temperature = lambda: 20.0   # factor 1.0
    return c


def _decay(c):
    SEMCoordinator._apply_daily_taper_decay(
        c, MONDAY + timedelta(days=7), SimpleNamespace(ev_connected=False))
    return {cid: det.energy_since_full for cid, det in c._ev_taper_detectors.items()}


@pytest.mark.unit
class TestEachCarDropsByItsOwnUse:

    def test_two_cars_drop_by_their_own_use(self):
        p = ConsumptionPredictor()
        _learned(p)
        assert _decay(_coord(p, ["car1", "car2"])) == {"car1": 12.0, "car2": 3.0}

    def test_one_car_still_uses_the_fleet_total(self):
        """One charger: the fleet total IS that car's use — no change."""
        p = ConsumptionPredictor()
        for i in range(7):
            p.observe_ev(MONDAY + timedelta(days=i), 9.0)
        assert _decay(_coord(p, ["car1"])) == {"car1": 9.0}

    def test_a_car_with_no_days_yet_uses_its_own_target(self):
        """Not the fleet total: a new car falls back to its daily target."""
        p = ConsumptionPredictor()
        for i in range(7):
            p.observe_ev(MONDAY + timedelta(days=i), 15.0)
        assert _decay(_coord(p, ["car1", "car2"])) == {"car1": 10.0, "car2": 10.0}


@pytest.mark.unit
class TestThePredictorLearnsPerCar:

    def test_one_observation_per_car_per_day(self):
        p = ConsumptionPredictor()
        p.observe_car_ev(MONDAY, "car1", 12.0)
        p.observe_car_ev(MONDAY, "car1", 40.0)          # same day: ignored
        assert p.predict_car_ev_consumption_tomorrow(MONDAY, "car1") == 12.0

    def test_an_empty_day_teaches_nothing(self):
        p = ConsumptionPredictor()
        p.observe_car_ev(MONDAY, "car1", 0.0)
        assert p.predict_car_ev_consumption_tomorrow(MONDAY, "car1") == 0.0

    def test_cars_do_not_share_days(self):
        p = ConsumptionPredictor()
        _learned(p)
        assert p.predict_car_ev_consumption_tomorrow(MONDAY, "car1") == 12.0
        assert p.predict_car_ev_consumption_tomorrow(MONDAY, "car2") == 3.0
        assert p.predict_ev_consumption_tomorrow(MONDAY) == 15.0

    def test_a_car_s_days_survive_a_restart(self):
        p = ConsumptionPredictor()
        _learned(p)
        q = ConsumptionPredictor()
        q.restore_state(p.get_state())
        assert q.predict_car_ev_consumption_tomorrow(MONDAY, "car2") == 3.0

    def test_a_store_saved_before_the_fix_loads(self):
        p = ConsumptionPredictor()
        for i in range(7):
            p.observe_ev(MONDAY + timedelta(days=i), 15.0)
        old = p.get_state()
        old.pop("ev_per_car")
        q = ConsumptionPredictor()
        q.restore_state(old)
        assert q.predict_ev_consumption_tomorrow(MONDAY) == 15.0
        assert q.predict_car_ev_consumption_tomorrow(MONDAY, "car1") == 0.0


@pytest.mark.unit
def test_the_per_charger_rollover_feeds_the_car_s_day():
    """The closing bucket — this car's own EV day — is what is learned.
    Source pin: the rollover branch calls ``observe_car_ev`` before it
    zeroes the bucket."""
    import inspect
    import textwrap
    src = textwrap.dedent(inspect.getsource(SEMCoordinator))
    branch = src[src.index("if self._daily_ev_per_charger_date.get(cid) != ev_day:"):]
    observe = branch.index("observe_car_ev(")
    reset = branch.index("self._daily_ev_per_charger[cid] = 0.0")
    assert observe < reset


@pytest.mark.unit
def test_the_fleet_figure_is_not_handed_to_a_second_car():
    """Twin of the bug: with the fix reverted (every car given the fleet
    figure), car 2 would drop by 15."""
    p = ConsumptionPredictor()
    _learned(p)
    c = _coord(p, ["car1", "car2"])
    c._predictor = MagicMock(wraps=p)
    _decay(c)
    asked = {call.args[1] for call in
             c._predictor.predict_car_ev_consumption_tomorrow.call_args_list}
    assert asked == {"car1", "car2"}
