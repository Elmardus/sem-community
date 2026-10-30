"""#1114 — loads and chargers read the same Solar Gate.

A charger ranked above the battery counts the pack's charge power in its
Solar Gate (``decide.self_consumption_surplus_w``, the #576 reclaim). The
loads' gate left it out, so a "Solar + battery" load above the battery got
no help while the pack charged: the sun went into the pack, little reached
the grid, and the gate stayed shut.

Now a load above the battery gets the budget with the reclaim counted. A
load below the battery still waits for the pack: that is its place in the
list. Driven through the real ``SurplusController.update`` walk.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from custom_components.solar_energy_management.coordinator.energy_reclaim import (
    reclaimable_battery_w,
    sustained_import,
)
from custom_components.solar_energy_management.coordinator.surplus_controller import (
    SurplusController,
    build_battery_tier_context,
    import_absorbed_by_loads_w,
    solar_bounded_reclaim,
    solar_bounded_surplus,
)

from .test_1112_solar_only_load_never_uses_the_pack import (
    BATTERY_CFG,
    BATTERY_MAX_W,
    SWITCH,
    _boiler,
    _Hass,
)

BATTERY_SLOT = 5


class _House:
    """Sun, a base load, a pack charging from spare sun, the real walk with
    the battery at ``BATTERY_SLOT`` in the list."""

    def __init__(self, device, *, soc):
        self.sc = SurplusController(MagicMock())
        self.sc.register_device(device)
        self.soc = soc
        self.t = 0.0
        self._imp: dict = {}
        self._dis: dict = {}
        self.last: dict = {}

    def _flows(self, solar_w, house_w):
        loads_w = sum(d.get_current_consumption()
                      for d in self.sc._devices.values() if d.is_active)
        net = solar_w - house_w - loads_w
        charge = discharge = 0.0
        if net > 0 and self.soc < 100.0:
            charge = min(net, BATTERY_MAX_W)
        elif net < 0:
            discharge = min(-net, BATTERY_MAX_W)
        grid = net - charge + discharge
        return max(0.0, grid), max(0.0, -grid), charge, discharge

    async def cycle(self, *, solar_w, house_w=500.0, n=1):
        for _ in range(n):
            self.t += 10.0
            export, imp, charge, discharge = self._flows(solar_w, house_w)
            held = sustained_import(self._imp, self.t, grid_import_w=imp,
                                    grid_import_known=True, car_active=False)
            held_dis = sustained_import(self._dis, self.t,
                                        grid_import_w=discharge,
                                        grid_import_known=True,
                                        car_active=False)
            draw = self.sc.active_surplus_draw_w()
            pool = solar_bounded_surplus(
                grid_export_w=export, active_draw_w=draw, solar_w=solar_w,
                grid_import_w=held, battery_discharge_w=held_dis)
            absorbed = import_absorbed_by_loads_w(
                grid_export_w=export, active_draw_w=draw, grid_import_w=held)
            reclaim = solar_bounded_reclaim(
                reclaimable_battery_w(
                    battery_charge_power=charge, soc=self.soc,
                    priority_soc=30, battery_commanded=False,
                    grid_import_w=imp, absorbed_w=absorbed),
                surplus_w=pool, solar_w=solar_w)
            btc = build_battery_tier_context(
                BATTERY_CFG, self.soc, pool, reclaim_w=reclaim)
            await self.sc.update(
                pool, price_level="normal", reclaim_w=reclaim,
                battery_priority=BATTERY_SLOT,
                battery_soc=btc.soc, battery_buffer_soc=btc.effective_floor_soc,
                battery_reserve_soc=btc.reserve_soc,
                battery_assist_budget_w=btc.assist_budget_w,
                battery_assist_budget_above_w=btc.assist_budget_above_battery_w,
                is_night=False, grid_import_w=imp)
            self.last = {"export": export, "import": imp,
                         "charge": charge, "discharge": discharge}


def _assist_boiler(hass, *, priority):
    d = _boiler(hass, battery_assist=True)
    d.priority = priority
    return d


@pytest.mark.asyncio
class TestALoadAboveTheBatteryGetsHelpWhileItCharges:
    async def test_it_starts_on_sun_plus_the_pack(self):
        """Sun 2 kW, house 0.5 kW, the pack at 95 % takes the 1.5 kW spare.
        A 3 kW "Solar + battery" boiler above the battery starts: its gate
        counts the 1.5 kW, as a charger in the same slot would."""
        hass = _Hass()
        house = _House(_assist_boiler(hass, priority=2), soc=95.0)
        await house.cycle(solar_w=2000.0, n=1)
        assert house.last["charge"] == pytest.approx(1500.0)
        await house.cycle(solar_w=2000.0, n=30)
        assert hass.on[SWITCH] is True
        assert house.last["import"] == 0.0

    async def test_a_load_below_the_battery_still_waits(self):
        """Same afternoon, the boiler ranked below the battery: the pack
        charges first, the boiler stays off."""
        hass = _Hass()
        house = _House(_assist_boiler(hass, priority=8), soc=95.0)
        await house.cycle(solar_w=2000.0, n=30)
        assert hass.on[SWITCH] is False
        assert house.last["charge"] == pytest.approx(1500.0)


@pytest.mark.unit
class TestOneGateTwoBudgets:
    def test_the_reclaim_opens_only_the_above_budget(self):
        btc = build_battery_tier_context(
            BATTERY_CFG, 95.0, 0.0, reclaim_w=1500.0)
        assert btc.assist_budget_w == 0.0
        assert btc.assist_budget_above_battery_w == pytest.approx(4500.0)

    def test_no_reclaim_no_difference(self):
        btc = build_battery_tier_context(BATTERY_CFG, 95.0, 2000.0)
        assert btc.assist_budget_above_battery_w == btc.assist_budget_w

    def test_below_the_floor_neither_opens(self):
        btc = build_battery_tier_context(
            BATTERY_CFG, 60.0, 0.0, reclaim_w=3000.0)
        assert btc.assist_budget_above_battery_w == 0.0
