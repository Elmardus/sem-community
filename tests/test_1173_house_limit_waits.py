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
RISES only to a level the house has held — over the last
``DISCHARGE_LIMIT_FOLLOW_DWELL_CYCLES`` when that is two steps or more,
over the last ``DISCHARGE_LIMIT_ONE_STEP_DWELL_CYCLES`` when it is one.
While it waits the grid covers the rest of the house; the pack never gets
more room than before. Lowering keeps #900's rule: it is the side that
keeps the pack out of the car (review: a long wait there let the pack feed
the car for minutes). A target — a peak cover owed, the hold, the blind
release, the cars' share, a meter at its peak limit — still rises at once,
and so does the first clamp out of one.
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
    """A number entity that takes what is written; every write is kept.
    ``fail`` writes raise first, the way a refused service call does."""

    def __init__(self, fail: int = 0):
        self.writes: list = []
        self.fail = fail
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
            if self.fail:
                self.fail -= 1
                raise RuntimeError("write refused")
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


# ─── 1. a rise waits for the house to hold it ──────────────────────────────

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


class TestABigRiseIsFollowedAfterTheShortWait:
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
    async def test_a_small_first_cycle_does_not_hold_up_a_big_rise(self):
        """(review) 790 W for one cycle, then 3 kW: the house held 3 kW for
        the short wait, so the limit follows then, not five minutes later."""
        hass, a = await _in_force_at(700.0)
        await _run(a, [790.0] + [3000.0] * BIG)
        assert hass.writes == [3000.0]

    @pytest.mark.asyncio
    async def test_a_failed_write_is_tried_again_the_next_cycle(self):
        """(review) The wait is not started again after a write that did
        not land: the house still held the level."""
        hass, a = await _in_force_at(700.0)
        hass.fail = 1
        await _run(a, [2600.0] * BIG)
        assert hass.writes == []
        await _run(a, [2600.0])
        assert hass.writes == [2750.0]


class TestAOneStepRiseWaitsTheLongWait:
    """The hover: a house at 690–800 W crosses the 750 W step all night."""

    @pytest.mark.asyncio
    async def test_one_step_up_waits_the_long_wait(self):
        hass, a = await _in_force_at(700.0)
        await _run(a, [790.0] * (SMALL - 1))
        assert hass.writes == []
        await _run(a, [790.0])
        assert hass.writes == [1000.0]

    @pytest.mark.asyncio
    async def test_a_big_start_does_not_make_a_one_step_rise_big(self):
        """1240 W for less than the short wait, then 990 W: the house held
        one step, so the long wait applies and the limit rises one step."""
        hass, a = await _in_force_at(700.0)
        await _run(a, [1240.0] * (BIG - 1) + [990.0] * (SMALL - BIG))
        assert hass.writes == []
        await _run(a, [990.0])
        assert hass.writes == [1000.0]

    @pytest.mark.asyncio
    async def test_a_second_rise_counts_the_cycles_already_above_it(self):
        """2.6 kW then 3.1 kW: the first rise lands on the short wait; the
        house had already held 3.1 kW that long, so the next is at once."""
        hass, a = await _in_force_at(700.0)
        await _run(a, [2600.0] + [3100.0] * (BIG - 1))
        assert hass.writes == [2750.0]
        await _run(a, [3100.0])
        assert hass.writes == [2750.0, 3250.0]

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

    def test_the_long_wait_is_longer_than_the_short_one(self):
        assert SMALL > BIG >= DISCHARGE_LIMIT_LOWER_DWELL_CYCLES


# ─── 2. lowering keeps #900's rule — it keeps the pack out of the car ──────

class TestLoweringIsUnchanged:
    @pytest.mark.parametrize("follows", [True, False])
    @pytest.mark.asyncio
    async def test_a_fall_lands_after_the_lowering_wait(self, follows):
        hass, a = await _in_force_at(2600.0, follows=follows)
        await _run(a, [700.0] * (DISCHARGE_LIMIT_LOWER_DWELL_CYCLES - 1), follows=follows)
        assert hass.writes == []
        await _run(a, [700.0], follows=follows)
        assert hass.writes == [750.0]

    @pytest.mark.asyncio
    async def test_a_fall_that_starts_one_step_down_is_not_held_longer(self):
        """(review) 1000 W limit, the house at 700 once and then 100 W: the
        first cut lowered to 750 W after five minutes and the pack could
        give 900 W more than the house meanwhile. Now: six cycles, 250 W."""
        hass, a = await _in_force_at(900.0)
        await _run(a, [700.0] + [100.0] * (DISCHARGE_LIMIT_LOWER_DWELL_CYCLES - 1))
        assert hass.writes == [250.0]

    @pytest.mark.asyncio
    async def test_a_rise_that_waits_does_not_block_a_fall(self):
        hass, a = await _in_force_at(1600.0)
        await _run(a, [2600.0] * (BIG - 1) + [300.0] * DISCHARGE_LIMIT_LOWER_DWELL_CYCLES)
        assert hass.writes == [500.0]


# ─── 3. what rises at once ─────────────────────────────────────────────────

class TestTargetsStillRiseAtOnce:
    @pytest.mark.asyncio
    async def test_a_target_rises_on_the_first_cycle(self):
        """A peak cover or the blind release: the meter is at stake now."""
        hass, a = await _in_force_at(700.0, follows=False)
        await _run(a, [2600.0], follows=False)
        assert hass.writes == [2750.0]

    @pytest.mark.asyncio
    async def test_a_peak_cover_after_a_waiting_clamp_lands_at_once(self):
        """The clamp is waiting out a rise; then the meter needs the pack."""
        hass, a = await _in_force_at(700.0)
        await _run(a, [2600.0] * (BIG - 2))
        await _run(a, [2600.0], follows=False)
        assert hass.writes == [2750.0]


class TestOutOfATargetTheClampIsAtOnce:
    @pytest.mark.asyncio
    async def test_from_the_0_w_hold_the_clamp_lands_on_the_first_cycle(self):
        """(review 2) The cheap hour's hold ends with a car plugged in: the
        pack covers the house again now, not after the wait."""
        hass, a = await _in_force_at(0.0, follows=False)
        await _run(a, [600.0])
        assert hass.writes == [750.0]

    @pytest.mark.asyncio
    async def test_from_a_cover_the_clamp_lands_on_the_first_cycle(self):
        hass, a = await _in_force_at(700.0, follows=False)
        await _run(a, [2600.0])
        assert hass.writes == [2750.0]

    @pytest.mark.asyncio
    async def test_once_the_clamp_is_in_force_its_rises_wait(self):
        hass, a = await _in_force_at(0.0, follows=False)
        await _run(a, [600.0] + [2600.0])
        assert hass.writes == [750.0]

    @pytest.mark.asyncio
    async def test_a_clamp_write_that_failed_is_not_waited_on(self):
        hass, a = await _in_force_at(0.0, follows=False)
        hass.fail = 1
        await _run(a, [600.0])
        assert hass.writes == []
        await _run(a, [600.0])
        assert hass.writes == [750.0]


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


class TestAZeroLimitIsALimit:
    @pytest.mark.asyncio
    async def test_a_burst_over_a_0_w_limit_waits_too(self):
        """(review, class 105) ``or -1.0`` read a 0 W limit as none, so a
        one-cycle burst over it was a write pair again."""
        hass, a = await _in_force_at(0.0)
        assert a.last_discharge_limit_w == 0.0
        await _run(a, [600.0] + [0.0] * 20)
        assert hass.writes == []

    @pytest.mark.asyncio
    async def test_a_target_from_0_w_still_rises_at_once(self):
        hass, a = await _in_force_at(0.0, follows=False)
        await _run(a, [600.0], follows=False)
        assert hass.writes == [750.0]


class TestObserverStartsNoWait:
    @pytest.mark.asyncio
    async def test_observer_cycles_do_not_count_toward_a_rise(self):
        hass, a = await _in_force_at(700.0)
        await _run(a, [2600.0] * (BIG - 1))
        await actuate_battery(_clamp(2600.0), a, observer=True)
        await _run(a, [2600.0] * (BIG - 1))
        assert hass.writes == []


# ─── 4. the decider sets the flag where the watts are the house ────────────

def _view(*, allowed_w=None, home_w=800.0, held=False, ev=True, soc=40.0,
          grid_funded_w=0.0, degraded=False, shared_w=None, cars_w=0.0,
          clamped_w=0.0):
    cfg = {"battery_max_discharge_power": MAX_W, "battery_mode": "auto"}
    fleet = FleetContext(
        solar_w=0.0, home_w=home_w, battery_soc=soc, battery_soc_known=True,
        battery_count=1, peak_slot_allowed_w=allowed_w,
        inputs_degraded=degraded, dark_inputs=(("grid",) if degraded else ()),
        peak_committed_w=cars_w, home_residual_clamped_w=clamped_w,
    )
    extra = {}
    if shared_w is not None:
        extra = {"ev_wants_pack": True, "battery_spendable_kwh": 5.0,
                 "ev_pack_shared_w": shared_w}
    return BatteryView(
        runtime=BatteryRuntime(battery_id="b1", last_known_soc=soc), config=cfg,
        fleet=fleet, charging_state="idle", ev_charging=ev, ev_connected=ev,
        home_consumption_w=home_w, scheduler_decision=None,
        grid_funded_load_w=grid_funded_w,
        sink_verdicts=({"house": SinkVerdict("house", HELD, "cheap hour")}
                       if held else {}),
        **extra,
    )


class TestTheDeciderMarksTheHouseClamps:
    def test_the_ev_clamp_follows_the_house(self):
        d = decide_battery(_view())
        assert d.intent is BatteryIntent.LIMIT_DISCHARGE
        assert "ev plugged in" in d.reason
        assert d.follows_load is True

    def test_under_a_limit_with_room_the_ev_clamp_still_follows(self):
        d = decide_battery(_view(allowed_w=6000.0, home_w=3800.0))
        assert d.follows_load is True

    @pytest.mark.parametrize("allowed_w", [0.0, 500.0, 3000.0])
    def test_a_house_over_the_meter_limit_makes_the_ev_clamp_a_target(self, allowed_w):
        """(review BLOCKER) No grid-funded load: the house figure already
        equals the cover, so nothing read as "raised" — but the meter is
        over its limit, and the rise must not wait."""
        d = decide_battery(_view(allowed_w=allowed_w, home_w=3800.0))
        assert d.discharge_limit_w == pytest.approx(3800.0)
        assert d.follows_load is False

    def test_the_cars_offer_counts_toward_the_meter(self):
        """(review 2) 4 kW allowance, 3.5 kW offered to the car, a kettle
        takes the house to 1.5 kW: the meter is over, the rise is at once."""
        d = decide_battery(_view(allowed_w=4000.0, cars_w=3500.0, home_w=1500.0))
        assert d.follows_load is False
        d = decide_battery(_view(allowed_w=4000.0, cars_w=2000.0, home_w=1500.0))
        assert d.follows_load is True

    def test_a_house_figure_that_is_no_measurement_does_not_wait(self):
        """(review 2) A balance that did not close cannot show the meter
        is under its limit."""
        d = decide_battery(_view(allowed_w=6000.0, home_w=800.0, clamped_w=4000.0))
        assert d.intent is BatteryIntent.LIMIT_DISCHARGE
        assert d.follows_load is False

    def test_a_pack_at_its_reserve_still_rises_in_time_for_the_meter(self):
        """(review 2) The cover is not paid at the reserve, but the clamp
        still rises with the house — and must not wait at a full meter."""
        d = decide_battery(_view(allowed_w=0.0, home_w=3800.0, soc=15.0))
        assert d.intent is BatteryIntent.LIMIT_DISCHARGE
        assert d.follows_load is False

    def test_the_ev_clamp_raised_by_the_peak_lands_at_once(self):
        d = decide_battery(_view(allowed_w=0.0, home_w=2000.0, grid_funded_w=1500.0))
        assert "may buy only" in d.reason
        assert d.follows_load is False

    def test_the_cars_share_is_a_target(self):
        """(review, #1130) house + what the cars were offered: the offer is
        what the charger plans with, so it lands at once."""
        d = decide_battery(_view(soc=90.0, shared_w=2000.0))
        assert "offered to the cars" in d.reason, d.reason
        assert d.discharge_limit_w == pytest.approx(2800.0)
        assert d.follows_load is False

    def test_the_grid_funded_clamp_follows_the_house(self):
        d = decide_battery(_view(ev=False, grid_funded_w=400.0))
        assert "grid-funded" in d.reason
        assert d.follows_load is True

    def test_the_grid_funded_clamp_over_the_meter_limit_is_a_target(self):
        d = decide_battery(_view(ev=False, allowed_w=1000.0, home_w=4000.0,
                                 grid_funded_w=3000.0))
        assert "may buy only" in d.reason
        assert d.follows_load is False

    def test_the_grid_funded_clamp_with_the_meter_over_but_not_raised(self):
        """(review 2) House 4 kW, 0.5 kW grid-funded, 1 kW allowance: the
        floor equals the house share, nothing reads as raised — the meter is
        still over."""
        d = decide_battery(_view(ev=False, allowed_w=1000.0, home_w=4000.0,
                                 grid_funded_w=500.0))
        assert "grid-funded" in d.reason
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

    @pytest.mark.asyncio
    async def test_a_house_over_the_meter_limit_is_covered_on_the_first_cycle(self):
        """(review BLOCKER) end to end: a 1000 W clamp in force, the house
        jumps to 3.8 kW under a 0 W allowance — the pack covers it now."""
        hass, a = await _in_force_at(800.0)
        await actuate_battery(decide_battery(_view(allowed_w=0.0, home_w=3800.0)), a)
        assert hass.writes == [4000.0]


# ─── 5. a night shaped like PROD's, end to end ─────────────────────────────

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


def _late_moves(houses, limits):
    """Cycles where the limit should already have moved and had not.

    ``limits[i]`` is the limit in force after cycle ``i``. A rise is owed
    once the last short wait of houses held two steps or more above it, or
    the last long wait held above it at all; a fall once the last lowering
    wait sat a step and the leak below it."""
    late = []
    step = DISCHARGE_LIMIT_STEP_W
    for i, lim in enumerate(limits):
        short = houses[max(0, i - BIG + 1): i + 1]
        long_ = houses[max(0, i - SMALL + 1): i + 1]
        low = houses[max(0, i - DISCHARGE_LIMIT_LOWER_DWELL_CYCLES + 1): i + 1]
        if len(short) == BIG and math.ceil(min(short) / step) * step - lim >= 2 * step:
            late.append((i, "big rise"))
        elif len(long_) == SMALL and min(long_) > lim:
            late.append((i, "rise"))
        elif (len(low) == DISCHARGE_LIMIT_LOWER_DWELL_CYCLES
              and all(lim - h >= step + DISCHARGE_LIMIT_LEAK_W for h in low)):
            late.append((i, "fall"))
    return late


class TestARecordedShapedNight:
    @pytest.mark.asyncio
    async def test_the_night_is_far_fewer_writes(self):
        houses = _night()
        old_hass, old = _adapter()
        await _run(old, houses, follows=False)
        new_hass, new = _adapter()
        await _run(new, houses)
        # the trace exercises the bug: the old rule writes dozens of times
        assert len(old_hass.writes) >= 150, len(old_hass.writes)
        assert len(new_hass.writes) <= len(old_hass.writes) // 3, (
            len(new_hass.writes), len(old_hass.writes))

    @pytest.mark.asyncio
    async def test_no_move_is_later_than_its_wait(self):
        """The cost side, on every cycle of the night: the limit follows a
        held rise within its wait and a fall within the lowering wait."""
        houses = _night()
        hass, a = _adapter()
        limits = []
        for h in houses:
            await _run(a, [h])
            limits.append(a.last_discharge_limit_w)
        assert _late_moves(houses, limits) == []
        assert len(hass.writes) > 10          # the night did move it

    def test_the_oracle_can_fail(self):
        """A limit that never moves is late all night (class 8)."""
        houses = _night()
        assert len(_late_moves(houses, [1000.0] * len(houses))) > 20
