"""#1199 — an idle plugged-in car does not hold the house battery.

PROD 24.–25.10: the car stayed plugged in overnight, charging had stopped
and night charging was off, yet SEM capped the battery's discharge at the
house load all night (a write every few minutes). The cap now holds only
while a car draws, or drew in the last 15 minutes (a car that pulses on and
off still drained the pack between bursts on 24.06).
"""
from types import SimpleNamespace

import pytest

from custom_components.solar_energy_management.coordinator import coordinator as coord_mod
from custom_components.solar_energy_management.coordinator.charger_types import (
    BatteryIntent,
    BatteryRuntime,
    BatteryView,
    FleetContext,
)
from custom_components.solar_energy_management.coordinator.decide_battery import (
    decide_battery,
)


def _view(*, charging, connected=True, drew_recently=True):
    return BatteryView(
        runtime=BatteryRuntime(battery_id="b1", last_known_soc=40.0),
        config={"battery_max_discharge_power": 5000,
                "battery_max_charge_power_w": 5000,
                "battery_mode": "auto",
                "battery_discharge_protection_enabled": True},
        fleet=FleetContext(solar_w=0.0, home_w=700.0, battery_soc=40.0,
                           battery_soc_known=True, buffer_soc=70.0,
                           battery_assist_min_surplus_w=1200.0),
        charging_state="idle",
        ev_charging=charging,
        ev_connected=connected,
        ev_drew_recently=drew_recently,
        home_consumption_w=700.0,
    )


@pytest.mark.unit
class TestTheCapFollowsACarThatMayDraw:
    def test_an_idle_car_leaves_the_battery_alone(self):
        d = decide_battery(_view(charging=False, drew_recently=False))
        assert d.intent is not BatteryIntent.LIMIT_DISCHARGE, d.reason

    def test_a_car_that_drew_minutes_ago_is_still_covered(self):
        d = decide_battery(_view(charging=False, drew_recently=True))
        assert d.intent is BatteryIntent.LIMIT_DISCHARGE, d.reason

    def test_a_drawing_car_is_covered(self):
        d = decide_battery(_view(charging=True, drew_recently=False))
        assert d.intent is BatteryIntent.LIMIT_DISCHARGE, d.reason

    def test_no_car_no_cap(self):
        d = decide_battery(_view(charging=False, connected=False, drew_recently=True))
        assert d.intent is not BatteryIntent.LIMIT_DISCHARGE, d.reason


@pytest.mark.unit
class TestTheDrawWindow:
    def _coord(self):
        return SimpleNamespace()

    def test_window_holds_then_releases(self, monkeypatch):
        now = [1000.0]
        monkeypatch.setattr(coord_mod, "_ev_draw_clock", lambda: now[0])
        c = self._coord()
        f = coord_mod.SEMCoordinator._ev_drew_recently
        assert f(c, SimpleNamespace(ev_charging=True))
        now[0] += coord_mod.EV_CLAMP_HOLD_S - 1
        assert f(c, SimpleNamespace(ev_charging=False)), "inside the window"
        now[0] += 2
        assert not f(c, SimpleNamespace(ev_charging=False)), "idle past the window"
        assert f(c, SimpleNamespace(ev_charging=True)), "a new draw opens it again"

    def test_a_restart_counts_as_a_draw(self, monkeypatch):
        now = [5.0]
        monkeypatch.setattr(coord_mod, "_ev_draw_clock", lambda: now[0])
        c = self._coord()
        f = coord_mod.SEMCoordinator._ev_drew_recently
        assert f(c, SimpleNamespace(ev_charging=False)), "unknown history holds the cap first"
        now[0] += coord_mod.EV_CLAMP_HOLD_S + 1
        assert not f(c, SimpleNamespace(ev_charging=False))
