"""(#1046) The car's daily solar share is the split of its flows.

PROD, 04.10.2026: the car took 6.3 kWh, the session record said 89 % solar
and the EV card's "Solar Share" (``energy_ev_solar_percentage``) said 49 %.
The day's share divided ``solar_to_ev`` (a flow) by ``daily_ev`` (the
charger's metered kWh). The flows miss every cycle where the inverter reads
dark — the allocator leaves that draw unassigned — so the top counted part
of what the bottom counted (class 119). The session record was already
fixed for this (#1024): it scales the flow split to the meter, so its share
is solar ÷ (solar + grid + battery). The day's share now is too.

The share is printed beside ``daily_ev`` ("Today 5 kWh · Solar Share"),
whose day rolls at the Charge-by time, while the flow layer's totals roll at
midnight. So the car's flows are also kept on the EV day
(``EnergyCalculator.ev_day_flows``): a night charge before 07:00 belongs to
the day before, in both numbers (review of the first cut).

The same ratio fed the "mostly from grid" tip and the optimization score,
and the battery charge session divided its solar flow by the measured
charge. All of them go through ``solar_share_pct`` now.
"""
from __future__ import annotations

import ast
import pathlib
import re
import textwrap
from datetime import date, timedelta
from unittest.mock import Mock, patch

import pytest
from freezegun import freeze_time

from custom_components.solar_energy_management.analytics.energy_assistant import (
    EnergyAssistant,
)
from custom_components.solar_energy_management.coordinator import session_energy
from custom_components.solar_energy_management.coordinator.energy_calculator import (
    EnergyCalculator,
)
from custom_components.solar_energy_management.coordinator.flow_calculator import (
    FlowCalculator,
)
from custom_components.solar_energy_management.coordinator.types import (
    BatterySessionData,
    PowerReadings,
)
from custom_components.solar_energy_management.utils.helpers import solar_share_pct
from custom_components.solar_energy_management.utils.time_manager import TimeManager

PKG = pathlib.Path(__file__).resolve().parent.parent

CYCLE_S = 60.0          # one minute per cycle: 6 kW → 0.1 kWh
EV_W = 6000.0

# PROD's shape: the sun feeds the car, the inverter reads dark for a while
# (solar and battery both 0 W; the charger keeps reading its own 6 kW), and a
# few cycles of grid at the end.
SUN = PowerReadings(solar_power=8000.0, home_consumption_power=500.0,
                    ev_power=EV_W, grid_export_power=1500.0)
DARK = PowerReadings(solar_power=0.0, home_consumption_power=500.0,
                     ev_power=EV_W)
GRID = PowerReadings(home_consumption_power=500.0, ev_power=EV_W,
                     grid_import_power=6500.0)
DAY = [SUN] * 30 + [DARK] * 29 + [GRID] * 4


def _run_day(cycles):
    """The real allocator and the real integrator over one day. Returns the
    day's EnergyFlows, the meter's kWh, and what the session record says."""
    fc = FlowCalculator()
    meter: dict = {}
    metered_kwh = 0.0
    totals = None
    energy_flows = None
    hours = CYCLE_S / 3600.0
    for power in cycles:
        pf = fc.calculate_power_flows(power)
        energy_flows = fc.integrate_energy_flows(pf, CYCLE_S)
        metered_kwh += power.ev_power * hours / 1000.0
        totals = session_energy.step(
            meter,
            solar_kwh=pf.solar_to_ev * hours / 1000.0,
            grid_kwh=pf.grid_to_ev * hours / 1000.0,
            battery_kwh=pf.battery_to_ev * hours / 1000.0,
            cost=0.0, power_w=power.ev_power, hours=hours,
            session_meter_kwh=None, lifetime_meter_kwh=None,
            import_rate=0.30,
        )
    return energy_flows, metered_kwh, totals


def _analyze(ea, flows, daily_ev_kwh, **kw):
    return ea.analyze(
        daily_ev_kwh=daily_ev_kwh,
        solar_to_ev_kwh=flows.solar_to_ev,
        grid_to_ev_kwh=flows.grid_to_ev,
        battery_to_ev_kwh=flows.battery_to_ev,
        **kw,
    )


class TestTheCardMatchesTheSession:
    def test_prod_day_reads_the_session_share_not_49(self, mock_hass):
        flows, metered, totals = _run_day(DAY)
        # the shape is real: dark cycles left the draw unassigned
        assert metered == pytest.approx(6.3)
        assert flows.solar_to_ev == pytest.approx(3.0, abs=0.01)
        assert flows.grid_to_ev == pytest.approx(0.4, abs=0.01)
        old = flows.solar_to_ev / metered * 100
        assert old == pytest.approx(47.6, abs=0.1), "the bug's number"

        data = _analyze(EnergyAssistant(mock_hass), flows, metered)
        session_pct = totals.solar_kwh / totals.energy_kwh * 100
        assert totals.energy_kwh == pytest.approx(6.3)
        assert data.ev_solar_percentage == pytest.approx(88.2, abs=0.1)
        assert data.ev_solar_percentage == pytest.approx(session_pct, abs=0.01)
        assert data.to_dict()["energy_ev_solar_percentage"] == 88.2

    def test_the_meter_size_does_not_move_the_share(self, mock_hass):
        """The bottom is the flows: a meter that counted more (dark cycles,
        a wallbox counter that caught up) changes nothing."""
        flows, metered, _ = _run_day(DAY)
        ea = EnergyAssistant(mock_hass)
        a = _analyze(ea, flows, metered).ev_solar_percentage
        b = _analyze(ea, flows, metered * 3).ev_solar_percentage
        assert a == b

    def test_battery_to_ev_is_in_the_bottom(self, mock_hass):
        data = EnergyAssistant(mock_hass).analyze(
            daily_ev_kwh=4.0, solar_to_ev_kwh=3.0, grid_to_ev_kwh=0.0,
            battery_to_ev_kwh=1.0,
        )
        assert data.ev_solar_percentage == pytest.approx(75.0)

    def test_no_flows_reads_zero_as_before(self, mock_hass):
        data = EnergyAssistant(mock_hass).analyze(daily_ev_kwh=5.0)
        assert data.ev_solar_percentage == 0.0

    def test_flows_without_a_meter_still_say_the_split(self, mock_hass):
        """The share does not wait for the meter: the flows alone say it."""
        data = EnergyAssistant(mock_hass).analyze(
            daily_ev_kwh=0.0, solar_to_ev_kwh=0.0, grid_to_ev_kwh=5.0,
        )
        assert data.ev_solar_percentage == 0.0
        data = EnergyAssistant(mock_hass).analyze(
            daily_ev_kwh=0.0, solar_to_ev_kwh=1.0, battery_to_ev_kwh=1.0,
        )
        assert data.ev_solar_percentage == pytest.approx(50.0)


class TestTheTipAndTheScoreUseTheSameSplit:
    def _ev_tips(self, ea):
        return [t for t in ea.get_all_tips() if t["category"] == "ev"
                and "from solar" in t["description"]]

    def test_prod_day_gets_no_mostly_grid_tip(self, mock_hass):
        flows, metered, _ = _run_day(DAY)
        ea = EnergyAssistant(mock_hass)
        _analyze(ea, flows, metered)
        assert self._ev_tips(ea) == [], "an 88 % solar day is not mostly grid"

    def test_tip_quotes_the_flow_split(self, mock_hass):
        ea = EnergyAssistant(mock_hass)
        ea.analyze(daily_ev_kwh=20.0, solar_to_ev_kwh=2.0, grid_to_ev_kwh=8.0)
        tips = self._ev_tips(ea)
        assert len(tips) == 1
        assert "Only 20% of EV charging" in tips[0]["description"]

    def test_no_split_known_gives_no_tip(self, mock_hass):
        """The flows hold nothing: SEM does not know the car was 0 % solar."""
        ea = EnergyAssistant(mock_hass)
        ea.analyze(daily_ev_kwh=10.0)
        assert self._ev_tips(ea) == []

    def test_score_uses_the_flow_split(self, mock_hass):
        flows, metered, _ = _run_day(DAY)
        ea = EnergyAssistant(mock_hass)
        score = _analyze(ea, flows, metered).optimization_score
        # 0 self-consumption, 0 autarky, no solar: only the EV part counts
        assert score == int(min(20, 88.235 * 0.2))
        assert score == 17

    def test_score_without_a_split_gives_no_ev_points(self, mock_hass):
        ea = EnergyAssistant(mock_hass)
        assert ea.analyze(daily_ev_kwh=10.0).optimization_score == 0


def _battery_coordinator():
    from custom_components.solar_energy_management.coordinator.coordinator import (
        SEMCoordinator,
    )
    with patch.object(SEMCoordinator, "__init__", return_value=None):
        coord = SEMCoordinator.__new__(SEMCoordinator)
    coord.config = {"update_interval": 10, "electricity_import_rate": 0.30}
    coord.update_interval = timedelta(seconds=60)
    coord._battery_session = BatterySessionData()
    coord._battery_session_idle_count = 0
    coord._battery_session_opposite_count = 0
    coord._energy_calculator = type("C", (), {"_import_rate": 0.30})()
    return coord


class TestBatterySessionShare:
    def test_dark_cycles_do_not_read_as_not_solar(self):
        sun = PowerReadings(solar_power=3000.0, home_consumption_power=500.0,
                            battery_charge_power=2500.0)
        dark = PowerReadings(home_consumption_power=500.0,
                             battery_charge_power=2500.0)
        grid = PowerReadings(home_consumption_power=500.0, grid_import_power=3000.0,
                             battery_charge_power=2500.0)
        fc = FlowCalculator()
        coord = _battery_coordinator()
        for power in [sun] * 6 + [dark] * 6 + [grid] * 2:
            coord._update_battery_session_tracking(
                power, fc.calculate_power_flows(power))
        s = coord._battery_session
        assert s.session_type == "charge"
        # the measured charge still counts the dark cycles
        assert s.energy_kwh == pytest.approx(2500 * 14 / 60 / 1000)
        old = s.solar_energy_kwh / s.energy_kwh * 100
        assert old == pytest.approx(42.9, abs=0.1), "the bug's number"
        assert s.solar_share_pct == pytest.approx(75.0)

    def test_a_dark_cycle_keeps_the_share_it_had(self):
        sun = PowerReadings(solar_power=3000.0, home_consumption_power=500.0,
                            battery_charge_power=2500.0)
        dark = PowerReadings(battery_charge_power=2500.0)
        fc = FlowCalculator()
        coord = _battery_coordinator()
        coord._update_battery_session_tracking(sun, fc.calculate_power_flows(sun))
        assert coord._battery_session.solar_share_pct == pytest.approx(100.0)
        coord._update_battery_session_tracking(dark, fc.calculate_power_flows(dark))
        assert coord._battery_session.solar_share_pct == pytest.approx(100.0), (
            "a cycle no flow saw is not a cycle of grid"
        )

    def test_a_dark_first_cycle_reads_zero_as_before(self):
        coord = _battery_coordinator()
        dark = PowerReadings(battery_charge_power=2500.0)
        coord._update_battery_session_tracking(
            dark, FlowCalculator().calculate_power_flows(dark))
        assert coord._battery_session.solar_share_pct == 0


class TestHelper:
    @pytest.mark.parametrize("args,want", [
        ((3.0, 0.4, 0.0), 3.0 / 3.4 * 100),
        ((1.0,), 100.0),
        ((0.0, 5.0), 0.0),
        ((0.0, 0.0, 0.0), None),
        ((0.0,), None),
        ((-1.0, 2.0), 0.0),       # a negative flow never counts
        ((2.0, -1.0), 100.0),
        ((None, 1.0), 0.0),
    ])
    def test_split(self, args, want):
        got = solar_share_pct(*args)
        if want is None:
            assert got is None
        else:
            assert got == pytest.approx(want)


def _ev_day_calc(since=date(2026, 10, 1)):
    """A real calculator on a 07:00 Charge-by time. ``since``: the EV day the
    flow rows were first booked on — days before, unless a test is about the
    upgrade day itself."""
    hass = Mock()
    hass.data = {}
    hass.config = Mock()
    hass.config.config_dir = "/config"
    hass.states.get = lambda _eid: None
    calc = EnergyCalculator(
        {"update_interval": 3600, "ev_target_time": "07:00"}, TimeManager(hass))
    calc._ev_flow_since = since
    return calc


def _hour(calc, fc, power):
    """One cycle of one hour through the real calculator, with this cycle's
    real flows. ``_last_update`` cleared so the cycle is the configured hour
    (an apparent gap over the spike limit would skip it)."""
    calc._last_update = None
    pf = fc.calculate_power_flows(power)
    flows = fc.integrate_energy_flows(pf, 3600.0)
    return calc.calculate_energy(power, pf), flows


BATT_TO_EV = PowerReadings(home_consumption_power=500.0, ev_power=2000.0,
                           battery_discharge_power=2500.0)
NIGHT_GRID = PowerReadings(home_consumption_power=500.0, ev_power=6000.0,
                           grid_import_power=6500.0)
DAY_SUN = PowerReadings(solar_power=8000.0, home_consumption_power=500.0,
                        ev_power=5000.0, grid_export_power=2500.0)
DAY_DARK = PowerReadings(home_consumption_power=500.0, ev_power=5000.0)


class TestTheShareCoversTheEvDay:
    """The review's case: night on grid, day on sun. "Today" (daily_ev) has
    rolled at 07:00; a share over the calendar flows would still hold the
    night's grid kWh until midnight."""

    def _night_then_day(self):
        calc, fc = _ev_day_calc(), FlowCalculator()
        with freeze_time("2026-10-03 23:30:00") as clock:
            _hour(calc, fc, BATT_TO_EV)
            clock.move_to("2026-10-04 02:00:00")      # past midnight
            energy, _ = _hour(calc, fc, NIGHT_GRID)
            night = (energy.daily_ev, calc.ev_day_flows())
            clock.move_to("2026-10-04 11:00:00")      # past the Charge-by time
            _hour(calc, fc, DAY_SUN)
            clock.move_to("2026-10-04 12:00:00")
            energy, flows = _hour(calc, fc, DAY_DARK)
            day = (energy.daily_ev, calc.ev_day_flows(), flows)
        return calc, night, day

    def test_the_night_keeps_its_rows_through_midnight(self):
        _, (daily_ev, ev_flows), _ = self._night_then_day()
        assert daily_ev == pytest.approx(8.0)
        assert ev_flows == pytest.approx((0.0, 6.0, 2.0)), (
            "the midnight sweep dropped the EV day's battery hour"
        )

    def test_the_day_share_is_the_day_alone(self, mock_hass):
        _, _, (daily_ev, ev_flows, calendar) = self._night_then_day()
        assert daily_ev == pytest.approx(10.0)       # the dark hour is metered
        assert ev_flows == pytest.approx((5.0, 0.0, 0.0))
        # the calendar flows still hold the night's grid
        assert calendar.solar_to_ev == pytest.approx(5.0)
        assert calendar.grid_to_ev == pytest.approx(6.0)
        cal_pct = solar_share_pct(calendar.solar_to_ev, calendar.grid_to_ev,
                                  calendar.battery_to_ev)
        assert cal_pct == pytest.approx(45.45, abs=0.01), "the review's number"
        assert calendar.solar_to_ev / daily_ev * 100 == pytest.approx(50.0), (
            "the bug's number"
        )
        data = EnergyAssistant(mock_hass).analyze(
            daily_ev_kwh=daily_ev, solar_to_ev_kwh=ev_flows[0],
            grid_to_ev_kwh=ev_flows[1], battery_to_ev_kwh=ev_flows[2])
        assert data.ev_solar_percentage == pytest.approx(100.0)

    def test_the_rows_survive_a_restart(self):
        calc, _, (_, ev_flows, _) = self._night_then_day()
        fresh = _ev_day_calc()
        fresh.restore_state(calc.get_state())
        with freeze_time("2026-10-04 12:00:00"):
            assert fresh.ev_day_flows() == pytest.approx(ev_flows)

    def test_no_flows_handed_over_adds_nothing(self):
        calc = _ev_day_calc()
        with freeze_time("2026-10-04 11:00:00"):
            calc._last_update = None
            calc.calculate_energy(DAY_SUN)
            assert calc.ev_day_flows() == (0.0, 0.0, 0.0)

    def test_the_read_keeps_the_day_daily_ev_was_read_on(self):
        """The energy step ran at 06:59:59, the read comes after 07:00: it
        must still read the day "Today" shows, not the new empty one."""
        calc, fc = _ev_day_calc(), FlowCalculator()
        with freeze_time("2026-10-04 06:59:59") as clock:
            _hour(calc, fc, DAY_SUN)
            clock.move_to("2026-10-04 07:00:01")
            assert calc.ev_day_flows() == pytest.approx((5.0, 0.0, 0.0))


class TestTheUpgradeDay:
    """The EV day the rows are first booked on began before them. Its share
    reads the calendar flows, not 0 % beside a morning's charge (review 2)."""

    def test_the_first_ev_day_says_short(self):
        calc, fc = _ev_day_calc(since=None), FlowCalculator()
        with freeze_time("2026-10-04 15:00:00") as clock:
            _hour(calc, fc, DAY_SUN)
            assert calc._ev_flow_since == date(2026, 10, 4)
            assert calc.ev_day_flows() is None
            clock.move_to("2026-10-05 08:00:00")    # the next EV day
            _hour(calc, fc, DAY_SUN)
            assert calc.ev_day_flows() == pytest.approx((5.0, 0.0, 0.0))

    def test_the_marker_survives_a_restart(self):
        from custom_components.solar_energy_management.coordinator import storage
        assert "ev_flow_since" in storage.CALCULATOR_STATE_KEYS
        calc, fc = _ev_day_calc(since=None), FlowCalculator()
        with freeze_time("2026-10-04 15:00:00"):
            _hour(calc, fc, DAY_SUN)
        fresh = _ev_day_calc(since=None)
        fresh.restore_state(calc.get_state())
        assert fresh._ev_flow_since == date(2026, 10, 4)
        with freeze_time("2026-10-05 08:00:00"):
            _hour(fresh, fc, DAY_SUN)
            assert fresh.ev_day_flows() == pytest.approx((5.0, 0.0, 0.0)), (
                "a restart read as the upgrade day again"
            )

    def test_a_junk_marker_is_dropped(self):
        fresh = _ev_day_calc(since=None)
        fresh.restore_state({"ev_flow_since": "not-a-date"})
        assert fresh._ev_flow_since is None


def _cycle_coordinator():
    from custom_components.solar_energy_management.coordinator.coordinator import (
        SEMCoordinator,
    )
    from .test_873_cycle_executes import WIRED, _hass, _sensors

    coord = SEMCoordinator(_hass(_sensors(0, 0, 0, 50)), dict(WIRED))
    coord.config_entry = None
    return coord


@pytest.mark.asyncio
async def test_the_cycle_publishes_the_ev_day_split():
    """Through the real cycle: the published share is the EV day's split,
    not the calendar flows (which hold nothing on this one idle cycle)."""
    from homeassistant.util import dt as dt_util

    coord = _cycle_coordinator()
    calc = coord._energy_calculator
    day = calc._ev_reset_day(dt_util.now())
    calc._ev_flow_since = day - timedelta(days=3)
    for d in (day - timedelta(days=1), day, day + timedelta(days=1)):
        # all three: the cycle may cross a boundary after ``day`` was taken
        calc._daily_accumulators[f"ev_flow_solar_{d}"] = 3.0
        calc._daily_accumulators[f"ev_flow_battery_{d}"] = 1.0
    data = await coord._async_update_data()
    assert data["energy_ev_solar_percentage"] == pytest.approx(75.0)


@pytest.mark.asyncio
async def test_the_cycle_on_the_upgrade_day_reads_the_calendar_flows():
    """No marker yet (a store from before the fix): the cycle stamps it, and
    the share reads the flow layer's day, not the short EV-day rows."""
    from homeassistant.util import dt as dt_util

    coord = _cycle_coordinator()
    fc = coord._flow_calculator
    fc._current_date = dt_util.now().date()
    fc._flow_accumulators.update({"solar_to_ev": 1.0, "grid_to_ev": 3.0})
    data = await coord._async_update_data()
    assert coord._energy_calculator._ev_flow_since is not None
    assert data["energy_ev_solar_percentage"] == pytest.approx(25.0)


# --- the lint: no solar share divides by a meter total ------------------------
#
# What it can see: a division (``/`` or ``/=``) with "solar" in the top and a
# metered total in the bottom. What it cannot see: the same values copied to
# other names first, or a multiply by a reciprocal. The tests above pin the
# real numbers for those paths; this keeps the plain shape from coming back.

_SOLAR_TOP = re.compile(r"solar", re.I)
_METER_BOTTOM = re.compile(
    r"daily_ev|ev_kwh|energy_kwh|\btotal\b|total_|charge|metered", re.I)

# Each of these divides solar by a total that is ITSELF the flow split scaled
# to the meter, so solar is part of the same total. Keyed by function, with
# how many such divisions it holds: a new one in the same function fails too.
_ALLOWED = {
    ("utils/helpers.py", "solar_share_pct"): (
        1, "the helper: total is the sum of the flows"),
    ("coordinator/ev_control.py", "_update_session_tracking"): (
        1, "session totals come from session_energy.step, which scales every "
           "flow by the same factor (#1024)"),
    ("coordinator/coordinator.py", "_lifetime_ev_shares"): (
        1, "lifetime totals are sums of those scaled session records"),
}


def _is_solar_over_meter(top, bottom) -> bool:
    return bool(_SOLAR_TOP.search(ast.unparse(top))
                and _METER_BOTTOM.search(ast.unparse(bottom)))


def _solar_over_meter(tree):
    """(function, line, text) for every solar ÷ meter-total division."""
    found = []

    def visit(node, func):
        for child in ast.iter_child_nodes(node):
            f = child.name if isinstance(
                child, (ast.FunctionDef, ast.AsyncFunctionDef)) else func
            if (isinstance(child, ast.BinOp) and isinstance(child.op, ast.Div)
                    and _is_solar_over_meter(child.left, child.right)):
                found.append((f, child.lineno, ast.unparse(child)))
            if (isinstance(child, ast.AugAssign) and isinstance(child.op, ast.Div)
                    and _is_solar_over_meter(child.target, child.value)):
                found.append((f, child.lineno, ast.unparse(child)))
            visit(child, f)

    visit(tree, None)
    return found


def _package_modules():
    for p in sorted(PKG.rglob("*.py")):
        rel = p.relative_to(PKG)
        if rel.parts[0] in {"tests", "tools", "scripts", "dashboard"}:
            continue
        if "__pycache__" in rel.parts or "node_modules" in rel.parts:
            continue
        yield rel.as_posix(), p


class TestNoSolarShareOverAMeter:
    def test_the_lint_catches_the_old_code(self):
        old = textwrap.dedent("""
            def analyze(daily_ev_kwh, solar_to_ev_kwh):
                ev_solar_pct = (solar_to_ev_kwh / daily_ev_kwh) * 100
            def _calculate_score(ev_kwh, solar_to_ev_kwh):
                return solar_to_ev_kwh / ev_kwh * 100
            def _update_battery_session_tracking(session):
                session.solar_share_pct = (session.solar_energy_kwh / session.energy_kwh) * 100
            def battery_card(solar_to_batt, daily_battery_charge):
                return solar_to_batt / daily_battery_charge
            def in_place(solar_pct, metered_kwh):
                solar_pct /= metered_kwh
        """)
        hits = _solar_over_meter(ast.parse(old))
        assert [h[0] for h in hits] == [
            "analyze", "_calculate_score", "_update_battery_session_tracking",
            "battery_card", "in_place",
        ]

    def test_every_solar_share_divides_flows_by_flows(self):
        bad, seen = [], {}
        for rel, path in _package_modules():
            for func, line, text in _solar_over_meter(ast.parse(path.read_text())):
                if (rel, func) in _ALLOWED:
                    seen[(rel, func)] = seen.get((rel, func), 0) + 1
                    continue
                bad.append(f"{rel}:{line} in {func}: {text}")
        assert not bad, (
            "a solar share divided by a meter total reads solar low whenever "
            "the flows miss a cycle — use utils.helpers.solar_share_pct:\n"
            + "\n".join(bad)
        )
        # an entry whose code moved away must not keep a free pass, and an
        # allowed function must not grow a second division under its cover
        assert seen == {k: n for k, (n, _why) in _ALLOWED.items()}


# --- the same rule in the dashboard card --------------------------------------

_JS_SOLAR_OVER_METER = re.compile(
    r"\b(\w*solar\w*)\s*/\s*(\w*(?:daily|charge|total|metered|ev_kwh|energy_kwh)\w*)",
    re.I)
CARD_SRC = PKG / "dashboard" / "card" / "src"


class TestTheCardNeverDividesAFlowByAMeter:
    def test_the_lint_catches_the_old_battery_card_line(self):
        old = "const solarPct = dailyCharge > 0 ? Math.round(solarToBatt / dailyCharge * 100) : 0;"
        assert _JS_SOLAR_OVER_METER.search(old)

    def test_no_card_divides_solar_by_a_meter(self):
        bad = []
        for path in sorted(CARD_SRC.rglob("*.js")):
            for n, line in enumerate(path.read_text().splitlines(), 1):
                if line.lstrip().startswith(("//", "*")):
                    continue
                if _JS_SOLAR_OVER_METER.search(line):
                    bad.append(f"{path.relative_to(PKG)}:{n}: {line.strip()}")
        assert not bad, (
            "use solarSharePct (util/solar-share.js) — a flow over a meter "
            "reads solar low:\n" + "\n".join(bad)
        )
