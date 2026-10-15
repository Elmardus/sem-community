"""#1112 — a Solar-only load must never run on the home battery.

docs/LOAD_PRIORITY.md: "Solar only — runs on PV surplus; never force-imports
and never touches the battery." Since #1102 the loads' pool takes the grid
import off, but not the pack's discharge. In a battery home the pack covers
the load the sun no longer funds, the meter reads 0 W, and the load ran on.

The fix: the pack's discharge comes off the pool like the import does
(``solar_bounded_surplus(..., battery_discharge_w=...)``). A "Solar + battery"
load keeps its daytime help: the shortfall now shows, and the Tier-1 headroom
pays it.

The house below is a small physics model driven through the real
``SurplusController.update`` walk, with the pool built the way the
coordinator builds it.
"""
from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from custom_components.solar_energy_management.coordinator.energy_reclaim import (
    reclaimable_battery_w,
    sustained_import,
)
from custom_components.solar_energy_management.coordinator.surplus_controller import (
    IMPORT_DEADBAND_W,
    SurplusController,
    build_battery_tier_context,
    import_absorbed_by_loads_w,
    loads_import_deficit_w,
    solar_bounded_reclaim,
    solar_bounded_surplus,
)
from custom_components.solar_energy_management.devices.base import (
    DeviceControlMode,
    SwitchDevice,
)

_ROOT = Path(__file__).resolve().parent.parent
BOILER_W = 3000.0
SWITCH = "switch.boiler"
BATTERY_CFG = {
    "battery_buffer_soc": 70,
    "battery_priority_soc": 30,
    "battery_assist_min_surplus": 1200,
    "battery_assist_max_power": 4500,
}
BATTERY_MAX_W = 5000.0


class _Hass:
    def __init__(self):
        self.on: dict = {}
        self.calls: list = []
        self.states = SimpleNamespace(get=self._get)
        self.services = SimpleNamespace(async_call=self._call)

    def _get(self, entity_id):
        if entity_id not in self.on:
            return None
        return SimpleNamespace(
            state="on" if self.on[entity_id] else "off", attributes={})

    async def _call(self, domain, service, data=None, **_kw):
        eid = (data or {}).get("entity_id")
        self.calls.append((service, eid))
        if service == "turn_on":
            self.on[eid] = True
        elif service == "turn_off":
            self.on[eid] = False


def _boiler(hass, *, battery_assist=False):
    d = SwitchDevice(
        hass, device_id="boiler", name="Boiler", rated_power=BOILER_W,
        entity_id=SWITCH, min_on_time=0, min_off_time=0,
        daily_min_runtime_sec=4 * 3600,
        battery_assist_enabled=battery_assist,
    )
    d.control_mode = DeviceControlMode.SURPLUS
    d.top_up_policy = "solar_only"
    hass.on[SWITCH] = False
    return d


class _House:
    """Sun, a base load, a self-consumption pack, the real controller."""

    def __init__(self, hass, device, *, soc):
        self.hass = hass
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
        elif net < 0 and self.soc > 5.0:
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
            btc = build_battery_tier_context(BATTERY_CFG, self.soc, pool)
            await self.sc.update(
                pool, price_level="normal", reclaim_w=reclaim,
                battery_soc=btc.soc, battery_buffer_soc=btc.effective_floor_soc,
                battery_reserve_soc=btc.reserve_soc,
                battery_assist_budget_w=btc.assist_budget_w,
                is_night=False, grid_import_w=imp)
            self.last = {"export": export, "import": imp,
                         "charge": charge, "discharge": discharge}


@pytest.mark.asyncio
class TestASolarOnlyLoadStopsWhenThePackCarriesIt:
    async def test_fading_sun_stops_it_instead_of_the_pack(self):
        """The issue's own numbers: sun 3.2 kW, house 1.5 kW, boiler 3 kW.
        The pack would give 1.3 kW, the grid reads 0 W. The boiler stops."""
        hass = _Hass()
        house = _House(hass, _boiler(hass), soc=100.0)
        await house.cycle(solar_w=6000.0, n=30)
        assert hass.on[SWITCH] is True
        await house.cycle(solar_w=3200.0, house_w=1500.0, n=30)
        assert hass.on[SWITCH] is False
        assert house.last["discharge"] == 0.0

    async def test_enough_sun_keeps_it_running(self):
        """The sun still covers house and boiler: nothing comes from the
        pack, so nothing changes."""
        hass = _Hass()
        house = _House(hass, _boiler(hass), soc=100.0)
        await house.cycle(solar_w=6000.0, n=30)
        await house.cycle(solar_w=4000.0, house_w=800.0, n=30)
        assert hass.on[SWITCH] is True


@pytest.mark.asyncio
class TestASolarPlusBatteryLoadKeepsItsHelp:
    async def test_the_pack_still_tops_it_up_by_day(self):
        """Sun 3.2 kW, house 0.5 kW, a full pack: the Solar + battery boiler
        runs on, the pack covers the 0.3 kW gap."""
        hass = _Hass()
        house = _House(hass, _boiler(hass, battery_assist=True), soc=100.0)
        await house.cycle(solar_w=6000.0, n=30)
        await house.cycle(solar_w=3200.0, house_w=500.0, n=30)
        assert hass.on[SWITCH] is True
        assert house.last["discharge"] == pytest.approx(300.0)


@pytest.mark.unit
class TestTheDeficitCountsThePack:
    def test_discharge_counts_like_import(self):
        assert loads_import_deficit_w(0.0, battery_discharge_w=1300.0) == \
            pytest.approx(1300.0 - IMPORT_DEADBAND_W)

    def test_the_cars_draw_explains_it(self):
        assert loads_import_deficit_w(
            0.0, ev_draw_w=2000.0, battery_discharge_w=1300.0) == 0.0

    def test_no_discharge_is_the_old_answer(self):
        assert loads_import_deficit_w(500.0) == loads_import_deficit_w(
            500.0, battery_discharge_w=0.0)


@pytest.mark.unit
class TestTheCoordinatorPassesTheDischarge:
    def test_the_pool_call_carries_the_sustained_discharge(self):
        tree = ast.parse((_ROOT / "coordinator" / "coordinator.py").read_text())
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                 and getattr(n.func, "id", None) == "solar_bounded_surplus"]
        assert calls, "the coordinator no longer builds the pool"
        for c in calls:
            kw = {k.arg for k in c.keywords}
            assert "battery_discharge_w" in kw, (
                "the loads' pool must take the pack's discharge off (#1112)")
