"""#1115 — a Solar-only car never draws the home battery, forecast
spending on or off.

docs/EV_CHARGING_LOGIC.md: Solar only "Never discharges for the car". The
forecast-spending switch has one job, selling forecast surplus to the grid
(c49c4006). The battery side read it bare: with the switch on and a budget
left, ``decide_battery`` lifted the discharge clamp for any plugged car, and
the pack covered a Solar-only car.

Now the switch counts only for a charger whose mode takes the pack under
the gate (Min + Solar), the same rule the charger side uses.
"""
from __future__ import annotations

import pytest

from custom_components.solar_energy_management.coordinator.coordinator import (
    SEMCoordinator,
)
from custom_components.solar_energy_management.coordinator.decide_battery import (
    BatteryIntent,
    decide_battery,
)

from .test_solar_plus_battery_is_the_permission import _bview


def _coord(mode, *, switch):
    c = SEMCoordinator.__new__(SEMCoordinator)
    c.config = {"ev_chargers": [{"id": "a", "charge_mode": mode}],
                "forecast_spending_enabled": switch}
    c._effective_charge_mode_for = lambda cfg: cfg.get("charge_mode", "off")
    return c


class _Power:
    ev_connected_per_charger = {"a": True}
    ev_connected = True


@pytest.mark.unit
class TestASolarOnlyCarKeepsTheClamp:
    @pytest.mark.parametrize("mode", ["solar_only", "solar_plus_cheap"])
    def test_the_switch_does_not_open_the_pack(self, mode):
        """Switch on, 1.7 kWh spendable, sun below the gate, a Solar-only
        car plugged in: the pack covers the house only."""
        wants = _coord(mode, switch=True)._ev_wants_pack(_Power())
        d = decide_battery(_bview(switch=True, ev_wants_pack=wants))
        assert d.intent is BatteryIntent.LIMIT_DISCHARGE, d.reason
        assert d.discharge_limit_w == pytest.approx(1000.0)

    def test_min_plus_solar_still_spends_with_the_switch(self):
        wants = _coord("min_plus_solar", switch=True)._ev_wants_pack(_Power())
        assert wants is True
        d = decide_battery(_bview(switch=True, ev_wants_pack=wants))
        assert d.intent is not BatteryIntent.LIMIT_DISCHARGE, d.reason

    def test_min_plus_solar_without_the_switch_keeps_the_clamp(self):
        assert _coord("min_plus_solar", switch=False)._ev_wants_pack(
            _Power()) is False

    def test_solar_plus_battery_needs_no_switch(self):
        assert _coord("solar_plus_battery", switch=False)._ev_wants_pack(
            _Power()) is True
