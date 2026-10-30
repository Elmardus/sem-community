"""#1130 — with two chargers, only the cars whose mode allows it use the
home battery.

Car A in Solar only, car B in Solar + battery, no sun. B's mode opened the
battery's discharge clamp, and one battery serves the whole house, so car A
charged from it too.

Now, while a connected car may not use the battery, the clamp stays: the
battery covers the house plus the assist the other cars were offered
(``ChargerDecision.assist_w``, summed in device order), and nothing more.
"""
from __future__ import annotations

from dataclasses import replace

import pytest

from custom_components.solar_energy_management.coordinator.charger_types import (
    BatteryView,
)
from custom_components.solar_energy_management.coordinator.coordinator import (
    SEMCoordinator,
)
from custom_components.solar_energy_management.coordinator.decide_battery import (
    BatteryIntent,
    decide_battery,
)

from .test_solar_plus_battery_is_the_permission import _bview

HOUSE_W = 1000.0
B_ASSIST_W = 2500.0


def _coord(modes, *, switch=False, offered=B_ASSIST_W):
    c = SEMCoordinator.__new__(SEMCoordinator)
    c.config = {"ev_chargers": [{"id": cid, "charge_mode": m}
                                for cid, m in modes.items()],
                "forecast_spending_enabled": switch}
    c._effective_charge_mode_for = lambda cfg: cfg.get("charge_mode", "off")
    c._assist_committed_w_per_cycle = offered
    return c


class _Power:
    ev_connected = True

    def __init__(self, connected):
        self.ev_connected_per_charger = connected


def _decide(coord, power):
    view = _bview(switch=False, ev_wants_pack=coord._ev_wants_pack(power))
    return decide_battery(
        replace(view, ev_pack_shared_w=coord._ev_pack_shared_w(power)))


@pytest.mark.unit
class TestTheBatteryFeedsOnlyTheCarsThatMayUseIt:
    def test_a_solar_only_car_beside_a_solar_plus_battery_one(self):
        """No sun, both plugged in: the battery covers the house and B's
        offered assist. A gets nothing from it."""
        c = _coord({"a": "solar_only", "b": "solar_plus_battery"})
        d = _decide(c, _Power({"a": True, "b": True}))
        assert d.intent is BatteryIntent.LIMIT_DISCHARGE, d.reason
        assert d.discharge_limit_w == pytest.approx(HOUSE_W + B_ASSIST_W)

    def test_nothing_offered_means_the_house_only(self):
        c = _coord({"a": "solar_only", "b": "solar_plus_battery"}, offered=0.0)
        d = _decide(c, _Power({"a": True, "b": True}))
        assert d.discharge_limit_w == pytest.approx(HOUSE_W)

    def test_the_solar_only_car_unplugged_opens_it_as_before(self):
        c = _coord({"a": "solar_only", "b": "solar_plus_battery"})
        d = _decide(c, _Power({"a": False, "b": True}))
        assert d.intent is not BatteryIntent.LIMIT_DISCHARGE, d.reason

    def test_two_cars_that_may_both_use_it(self):
        """Both may: the battery stays open, as before. They split the
        assist in device order on the charger side."""
        c = _coord({"a": "solar_plus_battery", "b": "solar_plus_battery"})
        power = _Power({"a": True, "b": True})
        assert c._ev_pack_shared_w(power) is None
        assert _decide(c, power).intent is not BatteryIntent.LIMIT_DISCHARGE

    def test_min_plus_solar_with_the_switch_counts_as_may(self):
        c = _coord({"a": "min_plus_solar", "b": "solar_plus_battery"},
                   switch=True)
        assert c._ev_pack_shared_w(_Power({"a": True, "b": True})) is None

    def test_below_the_floor_the_house_only(self):
        c = _coord({"a": "solar_only", "b": "solar_plus_battery"})
        power = _Power({"a": True, "b": True})
        view = _bview(switch=False, ev_wants_pack=True, soc=70.0)
        d = decide_battery(
            replace(view, ev_pack_shared_w=c._ev_pack_shared_w(power)))
        assert d.discharge_limit_w == pytest.approx(HOUSE_W)

    def test_the_view_defaults_to_no_such_car(self):
        assert BatteryView.__dataclass_fields__["ev_pack_shared_w"].default is None


@pytest.mark.unit
class TestTheCoordinatorWiresIt:
    def test_the_battery_view_carries_it(self):
        import inspect
        from custom_components.solar_energy_management.coordinator import (
            coordinator as cm,
        )
        src = inspect.getsource(cm)
        assert "ev_pack_shared_w=self._ev_pack_shared_w(power)" in src
