"""#1173 — the battery discharge limit was rewritten every few minutes.

PROD, 23.10.2026: ``number.batteries_maximale_entladeleistung`` changed 143
times between 00:00 and 11:00, mostly 750 ↔ 1000 W, often one or two minutes
apart. Nights 15.–22.10 had 48–97 changes each.

Under the EV clamp ("SoC below tonight's floor → discharge limit = house
load") the limit IS the house load. #900 quantised it to 250 W steps and let
a FALL through only after six cycles, but a RISE went out at once. So every
burst cost two writes: up on the first cycle above, down a minute later.
The bursts in the recorded nights:

* a kettle or a hob element, 2–3 kW for 10–60 s;
* a house that hovers 690–800 W across the 750 W step (the band there is
  50 W wide: up at 751, down at 700);
* the house figure's own jump for one cycle after SEM moved the pack — the
  grid meter sees the change at once, the pack's power sensor a poll later.

The fix: a limit that follows the house (``BatteryDecision.follows_load``)
moves only once the house has held the new level — up as well as down — for
``DISCHARGE_LIMIT_FOLLOW_DWELL_CYCLES`` when the move is two steps or more,
``DISCHARGE_LIMIT_ONE_STEP_DWELL_CYCLES`` when it is one step. A target
(the hold, a peak cover, the blind release) still rises at once.
"""
from __future__ import annotations

import math
import random
from types import SimpleNamespace

import pytest

from custom_components.solar_energy_management.coordinator.actuate_battery import (
    DISCHARGE_LIMIT_FOLLOW_DWELL_CYCLES,
    DISCHARGE_LIMIT_LEAK_W,
    DISCHARGE_LIMIT_LOWER_DWELL_CYCLES,
    DISCHARGE_LIMIT_ONE_STEP_DWELL_CYCLES,
    DISCHARGE_LIMIT_STEP_W,
    actuate_battery,
)
from custom_components.solar_energy_management.coordinator.battery_adapters.generic import (
    GenericBatteryAdapter,
)
from custom_components.solar_energy_management.coordinator.charger_types import (
    BatteryDecision,
    BatteryIntent,
    BatteryRuntime,
    BatteryView,
    FleetContext,
)
from custom_components.solar_energy_management.coordinator.decide_battery import (
    _stop_what_sem_started,
    decide_battery,
)
from custom_components.solar_energy_management.coordinator.sink_verdicts import (
    HELD,
    SinkVerdict,
)

LIMIT = "number.battery_max_discharge"
MAX_W = 5000.0
BIG = DISCHARGE_LIMIT_FOLLOW_DWELL_CYCLES
SMALL = DISCHARGE_LIMIT_ONE_STEP_DWELL_CYCLES


class _Hass:
    """A number entity that takes what is written; every write is kept."""

    def __init__(self):
        self.writes: list = []
        self._state = self._num(MAX_W)
        self.states = SimpleNamespace(get=lambda e: self._state if e == LIMIT else None,
                                      async_all=lambda *a, **k: [])
        self.services = SimpleNamespace(async_call=self._call)

    @staticmethod
    def _num(w):
        return SimpleNamespace(state=str(w), attributes={
            "unit_of_measurement": "W", "min": 0, "max": MAX_W})

    async def _call(self, domain, service, data=None, **kw):
        if service == "set_value" and (data or {}).get("entity_id") == LIMIT:
            self.writes.append(float(data["value"]))
            self._state = self._num(float(data["value"]))


def _adapter():
    hass = _Hass()
    a = GenericBatteryAdapter(hass, {"battery_discharge_control_entity": LIMIT,
                                     "battery_max_discharge_power": MAX_W})
    return hass, a


def _clamp(house_w, *, follows=True):
    return BatteryDecision(
        battery_id="b1", intent=BatteryIntent.LIMIT_DISCHARGE,
        discharge_limit_w=house_w, reason="ev plugged in → house",
        follows_load=follows)


async def _run(adapter, houses, *, follows=True):
    for h in houses:
        await actuate_battery(_clamp(h, follows=follows), adapter)


async def _in_force_at(house_w, *, follows=True):
    """A clamp already written at ``house_w``'s step, nothing pending."""
    hass, a = _adapter()
    await _run(a, [house_w], follows=follows)
    assert a.last_intent is BatteryIntent.LIMIT_DISCHARGE
    hass.writes.clear()
    return hass, a


# ─── 1. the rule, one move at a time ───────────────────────────────────────

class TestABurstIsNotATrend:
    @pytest.mark.asyncio
    async def test_one_cycle_kettle_writes_nothing(self):
        """The reported shape: 700 W, one cycle at 2.9 kW, 700 W again."""
        hass, a = await _in_force_at(700.0)
        await _run(a, [2900.0] + [700.0] * 40)
        assert hass.writes == []

    @pytest.mark.asyncio
    async def test_before_the_fix_the_same_kettle_was_two_writes(self):
        """The same cycles as a target (no ``follows_load``): up at once,
        down after the lowering wait. Pins that the test above is the bug."""
        hass, a = await _in_force_at(700.0, follows=False)
        await _run(a, [2900.0] + [700.0] * 40, follows=False)
        assert hass.writes == [3000.0, 750.0]

    @pytest.mark.asyncio
    async def test_a_burst_shorter_than_the_wait_writes_nothing(self):
        hass, a = await _in_force_at(700.0)
        await _run(a, [2600.0] * (BIG - 1) + [700.0] * 40)
        assert hass.writes == []

    @pytest.mark.asyncio
    async def test_a_cycle_back_inside_the_band_starts_the_wait_again(self):
        hass, a = await _in_force_at(700.0)
        await _run(a, ([2600.0] * (BIG - 1) + [700.0]) * 20)
        assert hass.writes == []


class TestABigMoveIsFollowedAfterAMinute:
    @pytest.mark.asyncio
    async def test_a_big_rise_that_holds_is_written_once(self):
        hass, a = await _in_force_at(700.0)
        await _run(a, [2600.0] * (BIG - 1))
        assert hass.writes == []
        await _run(a, [2600.0])
        assert hass.writes == [2750.0]
        await _run(a, [2600.0] * 50)
        assert hass.writes == [2750.0]

    @pytest.mark.asyncio
    async def test_the_rise_goes_to_the_level_the_house_held_not_a_late_spike(self):
        """1.6 kW for the whole wait, a 3 kW spike on its last cycle: the
        house held 1.6 kW, so the limit is 1750, not 3000."""
        hass, a = await _in_force_at(700.0)
        await _run(a, [1600.0] * (BIG - 1) + [3000.0])
        assert hass.writes == [1750.0]

    @pytest.mark.asyncio
    async def test_a_big_fall_that_holds_is_written_once(self):
        hass, a = await _in_force_at(2600.0)
        await _run(a, [700.0] * (BIG - 1))
        assert hass.writes == []
        await _run(a, [700.0])
        assert hass.writes == [750.0]

    @pytest.mark.asyncio
    async def test_the_fall_keeps_the_highest_house_of_the_wait(self):
        """600 W, then 1.4 kW on the last cycle (still far below 2750): the
        limit covers the 1.4 kW the house reached, not the 600 W."""
        hass, a = await _in_force_at(2600.0)
        await _run(a, [600.0] * (BIG - 1) + [1400.0])
        assert hass.writes == [1500.0]


class TestAOneStepMoveWaitsLonger:
    """The hover: a house at 690–800 W crosses the 750 W step all night."""

    @pytest.mark.asyncio
    async def test_one_step_up_waits_the_long_wait(self):
        hass, a = await _in_force_at(700.0)
        await _run(a, [790.0] * (SMALL - 1))
        assert hass.writes == []
        await _run(a, [790.0])
        assert hass.writes == [1000.0]

    @pytest.mark.asyncio
    async def test_one_step_down_waits_the_long_wait(self):
        hass, a = await _in_force_at(900.0)
        await _run(a, [690.0] * (SMALL - 1))
        assert hass.writes == []
        await _run(a, [690.0])
        assert hass.writes == [750.0]

    @pytest.mark.asyncio
    async def test_a_hover_across_the_step_writes_nothing(self):
        """690 ↔ 790 W, a few minutes each way. Before: a write pair for
        every crossing."""
        hover = ([790.0] * (SMALL - 2) + [690.0] * (SMALL - 2)) * 30
        hass, a = await _in_force_at(700.0)
        await _run(a, hover)
        assert hass.writes == []
        hass2, b = await _in_force_at(700.0, follows=False)
        await _run(b, hover, follows=False)
        assert hass2.writes == [1000.0, 750.0] * 30

    @pytest.mark.asyncio
    async def test_a_run_that_grows_to_two_steps_needs_only_the_short_wait(self):
        """One step for a while, then two: the size is read each cycle."""
        hass, a = await _in_force_at(700.0)
        await _run(a, [790.0] * 3 + [1300.0] * (BIG - 3))
        assert hass.writes == []        # the run's lowest is still 790
        hass, a = await _in_force_at(700.0)
        await _run(a, [1300.0] * BIG)
        assert hass.writes == [1500.0]

    def test_the_long_wait_is_longer_than_the_short_one(self):
        assert SMALL > BIG >= DISCHARGE_LIMIT_LOWER_DWELL_CYCLES


# ─── 2. what keeps the old rule ────────────────────────────────────────────

class TestTargetsStillRiseAtOnce:
    @pytest.mark.asyncio
    async def test_a_target_rises_on_the_first_cycle(self):
        """A peak cover or the blind release: the meter is at stake now."""
        hass, a = await _in_force_at(700.0, follows=False)
        await _run(a, [2600.0], follows=False)
        assert hass.writes == [2750.0]

    @pytest.mark.asyncio
    async def test_a_target_falls_after_the_old_wait(self):
        hass, a = await _in_force_at(2600.0, follows=False)
        await _run(a, [700.0] * (DISCHARGE_LIMIT_LOWER_DWELL_CYCLES - 1), follows=False)
        assert hass.writes == []
        await _run(a, [700.0], follows=False)
        assert hass.writes == [750.0]

    @pytest.mark.asyncio
    async def test_a_peak_cover_after_a_waiting_clamp_lands_at_once(self):
        """The clamp is waiting out a rise; then the meter needs the pack."""
        hass, a = await _in_force_at(700.0)
        await _run(a, [2600.0] * (BIG - 2))
        await _run(a, [2600.0], follows=False)
        assert hass.writes == [2750.0]
        # the clamp's half-done wait did not survive the cover
        await _run(a, [700.0] * (BIG - 1))
        assert hass.writes == [2750.0]


class TestTheFirstClampIsAtOnce:
    @pytest.mark.asyncio
    async def test_from_normal_the_clamp_lands_on_the_first_cycle(self):
        """(#1066) A car plugged in while the pack runs free: the clamp goes
        out at once, whatever its flag says."""
        hass, a = _adapter()
        await a.command_normal()
        hass.writes.clear()
        await _run(a, [800.0])
        assert hass.writes == [1000.0]

    @pytest.mark.asyncio
    async def test_a_fresh_adapter_writes_on_the_first_cycle(self):
        hass, a = _adapter()
        await _run(a, [800.0])
        assert hass.writes == [1000.0]


# ─── 3. the decider sets the flag where the watts are the house ────────────

def _view(*, allowed_w=None, home_w=800.0, held=False, ev=True, soc=40.0,
          grid_funded_w=0.0, degraded=False):
    cfg = {"battery_max_discharge_power": MAX_W, "battery_mode": "auto"}
    fleet = FleetContext(
        solar_w=0.0, home_w=home_w, battery_soc=soc, battery_soc_known=True,
        battery_count=1, peak_slot_allowed_w=allowed_w,
        inputs_degraded=degraded, dark_inputs=(("grid",) if degraded else ()),
    )
    return BatteryView(
        runtime=BatteryRuntime(battery_id="b1", last_known_soc=soc), config=cfg,
        fleet=fleet, charging_state="idle", ev_charging=ev, ev_connected=ev,
        home_consumption_w=home_w, scheduler_decision=None,
        grid_funded_load_w=grid_funded_w,
        sink_verdicts=({"house": SinkVerdict("house", HELD, "cheap hour")}
                       if held else {}),
    )


class TestTheDeciderMarksTheHouseClamps:
    def test_the_ev_clamp_follows_the_house(self):
        d = decide_battery(_view())
        assert d.intent is BatteryIntent.LIMIT_DISCHARGE
        assert "ev plugged in" in d.reason
        assert d.follows_load is True

    def test_the_ev_clamp_raised_by_the_peak_lands_at_once(self):
        d = decide_battery(_view(allowed_w=0.0, home_w=2000.0, grid_funded_w=1500.0))
        assert "may buy only" in d.reason
        assert d.follows_load is False

    def test_the_grid_funded_clamp_follows_the_house(self):
        d = decide_battery(_view(ev=False, grid_funded_w=400.0))
        assert "grid-funded" in d.reason
        assert d.follows_load is True

    def test_the_grid_funded_clamp_raised_by_the_peak_lands_at_once(self):
        d = decide_battery(_view(ev=False, allowed_w=1000.0, home_w=4000.0,
                                 grid_funded_w=3000.0))
        assert "may buy only" in d.reason
        assert d.follows_load is False

    def test_the_hold_is_a_target(self):
        d = decide_battery(_view(ev=False, held=True))
        assert d.intent is BatteryIntent.LIMIT_DISCHARGE
        assert d.follows_load is False

    def test_the_hold_s_peak_cover_is_a_target(self):
        d = decide_battery(_view(ev=False, held=True, allowed_w=0.0, home_w=2000.0))
        assert d.discharge_limit_w > 0
        assert d.follows_load is False

    def test_the_blind_release_is_a_target(self):
        d = decide_battery(_view(ev=False, held=True, allowed_w=6000.0,
                                 home_w=0.0, degraded=True))
        assert d.discharge_limit_w == pytest.approx(MAX_W)
        assert d.follows_load is False

    def test_a_retried_stop_keeps_the_flag(self):
        """(#1066) Every other cycle the protection is sent again around a
        stop that did not land; that copy must not drop the flag."""
        view = SimpleNamespace(sem_forced_charge=True, sem_forced_discharge=False,
                               sem_stop_misses=3)
        out = _stop_what_sem_started(view, _clamp(800.0))
        assert out.intent is BatteryIntent.LIMIT_DISCHARGE
        assert out.follows_load is True


# ─── 4. a night shaped like PROD's, end to end ─────────────────────────────

def _night(seed: int = 1173, cycles: int = 3600):
    """Ten hours at 10 s: a base load hovering 690–800 W with a fridge
    step, 10–40 s kettle bursts, and three cooking spells where a hob
    cycles 2.4 kW on and off. Synthetic — shaped like the recorded nights,
    no recorded value in it."""
    rnd = random.Random(seed)
    out = []
    for i in range(cycles):
        base = 745.0 + 45.0 * math.sin(i / 97.0) + rnd.uniform(-30.0, 30.0)
        if (i // 90) % 3 == 0:
            base += 100.0                       # fridge, 15 min of every 45
        out.append(base)
    for start in range(40, cycles, 42):         # a burst every ~7 min
        for k in range(rnd.randint(1, 4)):
            if start + k < cycles:
                out[start + k] += rnd.uniform(1800.0, 2600.0)
    for spell in (900, 2100, 3000):             # hob cycling, ~15 min each
        for k in range(90):
            if (k % 18) < rnd.randint(6, 12):
                out[spell + k] += 2400.0
    return out


class TestARecordedShapedNight:
    @pytest.mark.asyncio
    async def test_the_night_is_a_few_writes_not_dozens(self):
        houses = _night()
        old_hass, old = _adapter()
        await _run(old, houses, follows=False)
        new_hass, new = _adapter()
        await _run(new, houses)
        # the trace exercises the bug: the old rule writes dozens of times
        assert len(old_hass.writes) >= 80, len(old_hass.writes)
        assert len(new_hass.writes) <= len(old_hass.writes) // 4, (
            len(new_hass.writes), len(old_hass.writes))

    @pytest.mark.asyncio
    async def test_no_wait_outlasts_its_bound(self):
        """The cost side, on every cycle of the night: the house never sits
        above the limit, or a step and the leak below it, for longer than
        the wait for that move — the limit always follows a held move."""
        houses = _night()
        hass, a = _adapter()
        up = down = 0
        for h in houses:
            before = a.last_discharge_limit_w
            await _run(a, [h])
            after = a.last_discharge_limit_w
            if after != before:
                up = down = 0
                continue
            if h > before:
                up, down = up + 1, 0
            elif before - h >= DISCHARGE_LIMIT_STEP_W + DISCHARGE_LIMIT_LEAK_W:
                up, down = 0, down + 1
            else:
                up = down = 0
            assert up < SMALL and down < SMALL, (h, before, up, down)
        assert len(hass.writes) > 1          # the oracle saw moves at all
