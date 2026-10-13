"""#1102 — a Solar-only boiler kept running while the house imported.

@lostcontrol (Fronius, no battery, 2.2.0-beta.15): a 3 kW boiler stayed on
for more than 20 minutes after the house started buying from the grid. The
solar stayed at about 5.2 kW, the house rose to 5.5–8.5 kW.

The loads' pool is "the surplus that would exist if the controller's own
loads were off": the grid figure plus the loads' own draw (#508 W7). The
coordinator handed it the CLAMPED half of the grid — ``grid_export_power``,
``max(0, grid)`` — so any import read as 0 W and vanished before the add-back.
With 2 kW imported and the 3 kW boiler on, the pool read 3 kW instead of
1 kW; after the boiler's own debit the walk sat at −50 W, inside the −100 W
LIFO band, for as long as the sun stayed above 3 kW. Nothing stopped it.

The fix gives the pool the import back. The battery charge is the first
thing an import can be explained by — #938 already takes the import off the
reclaim (``charge − import``) — so only the import the charge does not
explain comes off the export side. Together the two addends are the full
balance: ``draw + export + charge − import``, still bounded by the sun.

A second count lived in the same walk: a running variable load (a watt
setpoint, a current knob) was offered ``remaining + its own draw``, but the
pool already holds its draw (the add-back). With the import seen, that double
count would ramp the load to its maximum, import, and get it stopped — a
cycle. It is now offered ``remaining`` alone.
"""
from __future__ import annotations

import ast
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.solar_energy_management.coordinator.energy_reclaim import (
    reclaimable_battery_w,
)
from custom_components.solar_energy_management.coordinator.surplus_controller import (
    IMPORT_DEADBAND_W,
    SurplusController,
    solar_bounded_reclaim,
    solar_bounded_surplus,
)
from custom_components.solar_energy_management.devices.base import DeviceControlMode
from custom_components.solar_energy_management.devices.power_setpoint import (
    PowerSetpointDevice,
)

_ROOT = Path(__file__).resolve().parent.parent
_COORD = _ROOT / "coordinator" / "coordinator.py"
_SC = _ROOT / "coordinator" / "surplus_controller.py"

SOLAR_W = 5200.0
BOILER_W = 3000.0


# ── 1. the pool keeps the import ─────────────────────────────────────────


@pytest.mark.unit
class TestThePoolKeepsTheImport:
    def test_reporters_numbers(self):
        # 2 kW imported with the 3 kW boiler on → about 1 kW would be left
        # if it were off (less the 100 W deadband). The old pool said 3 kW.
        assert solar_bounded_surplus(
            grid_export_w=0.0, active_draw_w=BOILER_W, solar_w=SOLAR_W,
            grid_import_w=2000.0,
        ) == pytest.approx(BOILER_W - 2000.0 + IMPORT_DEADBAND_W)

    def test_the_old_call_lost_it(self):
        # The pre-fix call shape — no import — is the 3 kW the boiler rode on.
        assert solar_bounded_surplus(
            grid_export_w=0.0, active_draw_w=BOILER_W, solar_w=SOLAR_W,
        ) == pytest.approx(BOILER_W)

    def test_import_larger_than_the_draw_floors_at_zero(self):
        assert solar_bounded_surplus(
            grid_export_w=0.0, active_draw_w=BOILER_W, solar_w=SOLAR_W,
            grid_import_w=3500.0,
        ) == 0.0

    def test_a_zero_export_setpoint_is_not_a_deficit(self):
        # Victron ESS holds +50 W of import by default: the pool must not
        # read that as the loads running on the grid.
        assert solar_bounded_surplus(
            grid_export_w=0.0, active_draw_w=BOILER_W, solar_w=SOLAR_W,
            grid_import_w=50.0,
        ) == pytest.approx(BOILER_W)
        assert IMPORT_DEADBAND_W == 100.0

    def test_the_chargers_own_grid_draw_is_not_the_loads(self):
        # A Min+Solar car at its 6 A floor (4.14 kW) imports 2.1 kW; a
        # running boiler ranked above the charger keeps its sun (#885).
        assert solar_bounded_surplus(
            grid_export_w=0.0, active_draw_w=BOILER_W, solar_w=6000.0,
            grid_import_w=2140.0, ev_draw_w=4140.0,
        ) == pytest.approx(BOILER_W)

    def test_import_past_the_chargers_draw_is_the_houses(self):
        assert solar_bounded_surplus(
            grid_export_w=0.0, active_draw_w=BOILER_W, solar_w=SOLAR_W,
            grid_import_w=3000.0, ev_draw_w=1000.0,
        ) == pytest.approx(BOILER_W - 2000.0 + IMPORT_DEADBAND_W)

    def test_a_grid_charging_battery_does_not_excuse_the_import(self):
        # An inverter's own daytime grid charge (5 kW): sun 4 kW, house
        # 0.5 kW, boiler 3 kW → 4.5 kW imported. The pack does not yield,
        # so the boiler is on the grid: the pool must not carry it.
        assert solar_bounded_surplus(
            grid_export_w=0.0, active_draw_w=BOILER_W, solar_w=4000.0,
            grid_import_w=4500.0,
        ) == 0.0

    def test_meter_never_read_takes_nothing_off(self):
        # None = no readable meter yet this lifetime: the running loads keep
        # what they hold (the export side reads 0 too), nothing new starts.
        assert solar_bounded_surplus(
            grid_export_w=0.0, active_draw_w=BOILER_W, solar_w=SOLAR_W,
            grid_import_w=None,
        ) == pytest.approx(BOILER_W)

    def test_export_and_sun_cap_unchanged(self):
        assert solar_bounded_surplus(
            grid_export_w=500.0, active_draw_w=1000.0, solar_w=5000.0,
            grid_import_w=0.0,
        ) == pytest.approx(1500.0)
        assert solar_bounded_surplus(
            grid_export_w=0.0, active_draw_w=1600.0, solar_w=800.0,
            grid_import_w=0.0,
        ) == pytest.approx(800.0)

    def test_the_two_addends_are_the_whole_balance(self):
        """Over a grid of meter / pack / load states: export surplus plus
        the reclaim is ``draw + export + charge − import`` (past the
        deadband), bounded by 0 and the sun — exactly, whenever the pack
        charges from the sun (no import beside it); never more when it
        charges from the grid. Before the fix the sum lost the import
        whenever no charge stood beside it."""
        exact = overlap = 0
        for solar in (0.0, 800.0, 5200.0):
            for draw in (0.0, 1000.0, 3000.0):
                for charge in (0.0, 1000.0, 3000.0):
                    for grid in (-4000.0, -2000.0, -500.0, -60.0, 0.0, 700.0, 2500.0):
                        export, imp = max(0.0, grid), max(0.0, -grid)
                        s = solar_bounded_surplus(
                            grid_export_w=export, active_draw_w=draw,
                            solar_w=solar, grid_import_w=imp)
                        r = solar_bounded_reclaim(
                            reclaimable_battery_w(
                                battery_charge_power=charge, soc=60.0,
                                priority_soc=30.0, battery_commanded=False,
                                grid_import_w=imp),
                            surplus_w=s, solar_w=solar)
                        counted = max(0.0, imp - IMPORT_DEADBAND_W)
                        want = min(solar, max(0.0, draw + export + charge - counted))
                        if charge > 0.0 and imp > 0.0:
                            assert s + r <= want + 1e-6, (solar, draw, charge, grid)
                            overlap += 1
                        else:
                            assert s + r == pytest.approx(want), (
                                solar, draw, charge, grid)
                            exact += 1
        assert exact == 3 * 3 * 1 * 7 + 3 * 3 * 2 * 3
        assert overlap == 3 * 3 * 2 * 4


# ── 2. the reporter's afternoon, closed loop ─────────────────────────────


def _boiler():
    """A 3 kW switch, past its minimum run (the reporter: > 20 min)."""
    d = MagicMock()
    d.device_id = "boiler"
    d.name = "Boiler"
    d.priority = 5
    d.min_power_threshold = BOILER_W
    d.rated_power = BOILER_W
    d.is_enabled = True
    d.managed_externally = False
    d.is_active = True
    d.device_type = MagicMock(value="switch")
    d.activate = AsyncMock(return_value=BOILER_W)
    d.adjust_power = AsyncMock(return_value=BOILER_W)
    d.get_current_consumption = MagicMock(
        side_effect=lambda: BOILER_W if d.is_active else 0.0)
    d.can_activate = MagicMock(return_value=True)
    d.can_deactivate = MagicMock(return_value=True)
    d.record_activated = MagicMock()
    d.record_deactivated = MagicMock()
    d.reset_surplus_timer = MagicMock()
    d.status = MagicMock()
    d.control_mode = DeviceControlMode.SURPLUS
    d._sem_owned = True
    d.is_deadline_approaching = False
    d._offpeak_forced = False
    d._offpeak_forced_date = None
    d._batt_overnight_forced = False
    d._batt_overnight_forced_date = None
    d._daily_runtime_meter_day = None
    d.needs_offpeak_activation = False
    d.remaining_daily_runtime_sec = 0
    d.daily_min_runtime_sec = 0
    d.daily_targets_met = False
    d.daily_max_runtime_reached = False
    d.stop_condition_met = False
    d.top_up_policy = "solar_only"
    d.battery_assist_enabled = False
    d.battery_eligible_overnight = False
    d.stop_entity = ""
    d.stop_at = 0

    async def _off():
        d.is_active = False
    d.deactivate = AsyncMock(side_effect=_off)
    return d


async def _afternoon(sc, *, house_w, cycles, with_import, solar_w=SOLAR_W):
    """No battery. Each cycle the meter reads what the house, the loads and
    the sun leave; the pool is built the way the coordinator builds it."""
    imports = []
    for _ in range(cycles):
        draw = sc.active_surplus_draw_w()
        grid = solar_w - house_w - draw
        export, imp = max(0.0, grid), max(0.0, -grid)
        imports.append(imp)
        kw = {"grid_import_w": imp} if with_import else {}
        pool = solar_bounded_surplus(
            grid_export_w=export, active_draw_w=draw, solar_w=solar_w, **kw)
        await sc.update(pool, price_level="normal", grid_import_w=imp)
    return imports


@pytest.mark.asyncio
class TestTheBoilerStopsWhenTheHouseImports:
    async def test_the_reporters_afternoon_stops_it(self):
        # House 4.2 kW + boiler 3 kW against 5.2 kW of sun: 2 kW imported.
        sc = SurplusController(MagicMock())
        boiler = _boiler()
        sc.register_device(boiler)
        await _afternoon(sc, house_w=4200.0, cycles=12, with_import=True)
        boiler.deactivate.assert_awaited()

    async def test_without_the_import_it_never_stops(self):
        # Non-vacuous: the same afternoon through the pre-fix call shape
        # keeps the boiler on every cycle — the reported symptom.
        sc = SurplusController(MagicMock())
        boiler = _boiler()
        sc.register_device(boiler)
        imports = await _afternoon(sc, house_w=4200.0, cycles=12, with_import=False)
        boiler.deactivate.assert_not_awaited()
        assert min(imports) == pytest.approx(2000.0)

    async def test_a_funded_boiler_keeps_running(self):
        # House 2 kW: the sun covers both, 200 W exported — nothing stops.
        sc = SurplusController(MagicMock())
        boiler = _boiler()
        sc.register_device(boiler)
        imports = await _afternoon(sc, house_w=2000.0, cycles=12, with_import=True)
        boiler.deactivate.assert_not_awaited()
        assert max(imports) == 0.0

    async def test_once_stopped_it_does_not_restart_into_the_import(self):
        sc = SurplusController(MagicMock())
        boiler = _boiler()
        sc.register_device(boiler)
        await _afternoon(sc, house_w=4200.0, cycles=12, with_import=True)
        assert boiler.is_active is False
        await _afternoon(sc, house_w=4200.0, cycles=12, with_import=True)
        boiler.activate.assert_not_awaited()


def _load(device_id, draw, priority, *, paid=False):
    """A running on/off load. ``paid`` = on the cheap-hours grid top-up."""
    d = _boiler()
    d.device_id = device_id
    d.name = device_id
    d.priority = priority
    d.min_power_threshold = draw
    d.rated_power = draw
    d.activate = AsyncMock(return_value=draw)
    d.adjust_power = AsyncMock(return_value=draw)
    d.get_current_consumption = MagicMock(
        side_effect=lambda: draw if d.is_active else 0.0)
    if paid:
        day = date(2026, 10, 12)
        d._daily_runtime_meter_day = day
        d._offpeak_forced = True
        d._offpeak_forced_date = day
        d.top_up_policy = "cheap_hours"
        d.remaining_daily_runtime_sec = 3600
    return d


async def _cycles(sc, *, solar_w, house_w, n, price="cheap"):
    for _ in range(n):
        # What the meter sees: every load that is on, credited or not.
        on_w = sum(d.get_current_consumption()
                   for d in sc._devices.values() if d.is_active)
        draw = sc.active_surplus_draw_w()
        grid = solar_w - house_w - on_w
        export, imp = max(0.0, grid), max(0.0, -grid)
        pool = solar_bounded_surplus(
            grid_export_w=export, active_draw_w=draw, solar_w=solar_w,
            grid_import_w=imp)
        await sc.update(pool, price_level=price, grid_import_w=imp)


@pytest.mark.asyncio
class TestAPaidLoadDoesNotStopASolarOne:
    """The import a load on a PAID source causes (a cheap-hours grid top-up)
    is that load's own. Sun 4 kW, house 0.5 kW: 3.5 kW spare. A 1 kW pump on
    solar and a 3 kW boiler buying its daily minimum from the grid."""

    async def test_a_funded_pump_ahead_of_the_top_up_keeps_running(self):
        sc = SurplusController(MagicMock())
        pump = _load("pump", 1000.0, 2)
        boiler = _load("boiler", 3000.0, 5, paid=True)
        sc.register_device(pump)
        sc.register_device(boiler)
        await _cycles(sc, solar_w=4000.0, house_w=500.0, n=12)
        pump.deactivate.assert_not_awaited()
        boiler.deactivate.assert_not_awaited()

    async def test_a_pump_behind_the_top_up_gets_only_what_is_left(self):
        # The boiler is first in line for the sun: 3.5 kW spare minus its
        # 3 kW leaves 0.5 kW — not enough for the 1 kW pump.
        sc = SurplusController(MagicMock())
        boiler = _load("boiler", 3000.0, 2, paid=True)
        pump = _load("pump", 1000.0, 5)
        sc.register_device(boiler)
        sc.register_device(pump)
        await _cycles(sc, solar_w=4000.0, house_w=500.0, n=12)
        pump.deactivate.assert_awaited()
        boiler.deactivate.assert_not_awaited()

    async def test_at_night_the_solar_load_still_stops(self):
        sc = SurplusController(MagicMock())
        pump = _load("pump", 1000.0, 2)
        boiler = _load("boiler", 3000.0, 5, paid=True)
        sc.register_device(pump)
        sc.register_device(boiler)
        await _cycles(sc, solar_w=0.0, house_w=500.0, n=6)
        pump.deactivate.assert_awaited()
        boiler.deactivate.assert_not_awaited()


@pytest.mark.asyncio
class TestOnlyLoadsTheWalkDebitsAreCredited:
    """The add-back is for loads the walk debits. A load in mode Off is the
    user's — the belief sync sees it running, the walk skips it — and one
    behind the operator's relay is cut by the relay. Crediting either hands
    its draw, and the import it causes, to the Solar-only loads."""

    def _washer(self):
        washer = _load("washer", 2000.0, 3)
        washer.control_mode = DeviceControlMode.OFF
        washer._sem_owned = False   # the user started it
        return washer

    def test_mode_off_and_relay_locked_loads_are_not_credited(self):
        sc = SurplusController(MagicMock())
        locked = _load("heat_pump", 2500.0, 4)
        locked.locked_by_operator = True
        running = _load("boiler", 3000.0, 1)
        for d in (self._washer(), locked, running):
            sc.register_device(d)
        assert sc.active_surplus_draw_w() == pytest.approx(3000.0)

    async def test_a_users_washer_is_not_spare_sun(self):
        # Sun 3 kW, house 1 kW, the user's 2 kW washer on: nothing spare.
        sc = SurplusController(MagicMock())
        pump = _load("pump", 1500.0, 2)
        pump.is_active = False
        sc.register_device(self._washer())
        sc.register_device(pump)
        await _cycles(sc, solar_w=3000.0, house_w=1000.0, n=12, price="normal")
        pump.activate.assert_not_awaited()
        assert sc._devices["washer"].deactivate.await_count == 0

    async def test_crediting_the_washer_started_the_pump(self):
        # Non-vacuous: the same meter with the washer's 2 kW credited.
        sc = SurplusController(MagicMock())
        pump = _load("pump", 1500.0, 2)
        pump.is_active = False
        sc.register_device(self._washer())
        sc.register_device(pump)
        for _ in range(12):
            pool = solar_bounded_surplus(
                grid_export_w=0.0, active_draw_w=2000.0, solar_w=3000.0,
                grid_import_w=0.0)
            await sc.update(pool, price_level="normal", grid_import_w=0.0)
        pump.activate.assert_awaited()


# ── 3. a variable load is offered its pool once, not twice ───────────────


ENTITY = "number.heater_power"


class _Hass:
    """A number entity whose state follows what SEM writes to it."""

    def __init__(self, value: float):
        self._attrs = {"min": 0.0, "max": 9000.0, "step": 1.0}
        self._state = SimpleNamespace(state=str(value), attributes=self._attrs)
        self.states = SimpleNamespace(get=self._get)
        self.services = SimpleNamespace(async_call=self._call)

    def _get(self, entity_id):
        return self._state if entity_id == ENTITY else None

    async def _call(self, domain, service, data=None, **_kw):
        assert (domain, service) == ("number", "set_value")
        self._state = SimpleNamespace(
            state=str(data["value"]), attributes=self._attrs)

    @property
    def value(self) -> float:
        return float(self._state.state)


@pytest.mark.asyncio
class TestAVariableLoadFollowsTheSun:
    async def test_it_settles_on_the_spare_power_not_its_maximum(self):
        # Sun 5.2 kW, house 2 kW: 3.2 kW spare. The heater runs at 1 kW.
        hass = _Hass(1000.0)
        heater = PowerSetpointDevice(
            hass=hass, device_id="heater", name="Heater",
            rated_power=9000.0, min_power_threshold=500.0, entity_id=ENTITY)
        heater.adopt_if_running()
        assert heater.is_active
        sc = SurplusController(hass)
        sc.register_device(heater)
        imports = await _afternoon(sc, house_w=2000.0, cycles=30, with_import=True)
        # It follows the spare power less the 50 W export margin — it does
        # not ramp to 9 kW on the grid, and nothing has to stop it.
        assert hass.value == pytest.approx(3150.0, abs=60.0)
        assert heater.is_active
        assert max(imports[-10:]) == 0.0

    def test_the_walk_offers_a_running_load_the_pool_once(self):
        # Source pin: the active branch hands ``adjust_power`` the remaining
        # pool, which already holds the load's own draw (the add-back).
        tree = ast.parse(_SC.read_text(encoding="utf-8"))
        calls = [
            n for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr == "adjust_power"
            and isinstance(n.func.value, ast.Name) and n.func.value.id == "device"
        ]
        args = [ast.unparse(c.args[0]) for c in calls if c.args]
        assert "remaining_surplus" in args, args
        assert not any("old_consumption" in a for a in args), args


# ── 4. the guard: the coordinator hands the pool the import ──────────────


def _callee(node: ast.Call) -> str:
    f = node.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return ast.unparse(f)
    return ""


def _kw(node: ast.Call, name: str):
    for k in node.keywords:
        if k.arg == name:
            return k.value
    return None


def _enclosing(tree: ast.AST, target: ast.AST) -> ast.AST:
    best = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.lineno <= target.lineno <= (node.end_lineno or node.lineno):
                if best is None or node.lineno > best.lineno:
                    best = node
    assert best is not None
    return best


def _bindings(name: str, scope: ast.AST):
    out = []
    for node in ast.walk(scope):
        if isinstance(node, ast.Assign):
            if any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
                out.append(node.value)
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign, ast.NamedExpr)):
            t = node.target
            if isinstance(t, ast.Name) and t.id == name:
                out.append(node if isinstance(node, ast.AugAssign) else node.value)
    return out


@pytest.mark.unit
class TestCoordinatorWiringGuard:
    """Every ``solar_bounded_surplus`` call in the coordinator carries the
    held meter import (a refactor that drops it puts the reporter's boiler
    back on the grid) and the chargers' draw (one that drops it stops a
    load ranked above a charger whenever the car imports)."""

    def test_every_pool_call_carries_the_import(self):
        tree = ast.parse(_COORD.read_text(encoding="utf-8"))
        calls = [n for n in ast.walk(tree)
                 if isinstance(n, ast.Call) and _callee(n) == "solar_bounded_surplus"]
        assert calls, "solar_bounded_surplus() is no longer called from the coordinator"
        for c in calls:
            scope = _enclosing(tree, c)
            imp = _kw(c, "grid_import_w")
            assert imp is not None, "solar_bounded_surplus() called without grid_import_w"
            srcs = [imp] if isinstance(imp, ast.Call) else _bindings(
                getattr(imp, "id", ""), scope)
            assert srcs, f"grid_import_w={ast.unparse(imp)} is never assigned"
            for s in srcs:
                assert isinstance(s, ast.Call) and _callee(s) == "held_grid_import", (
                    f"grid_import_w must come from held_grid_import(), got {ast.unparse(s)}")
            ev = _kw(c, "ev_draw_w")
            assert ev is not None and "ev_power" in ast.unparse(ev), (
                "solar_bounded_surplus() must be handed the chargers' draw")
            assert "grid_export_power" in ast.unparse(_kw(c, "grid_export_w"))
