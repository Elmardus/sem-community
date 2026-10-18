"""#820 — the headroom must hold on every re-plan, not only the first.

@ArneGollin1987 (18.10): a clear day, sun until 18:15, and SEM predicted
the pack full at 18:10 — with a 10 % headroom it should be well earlier.
The owner confirmed the mechanism: every few seconds SEM plans again for a
full battery in the last sunny hour. In the reporter's words, it "will
always try to finish 10 % fast in the last hour slot of the day and not
10 % faster over the whole loading of the day."

The cause: ``paced_charge_cap_w`` solved the smallest cap that lands the
REMAINING need in the LAST remaining slot and opened it by 10 %. Each
re-solve applies the 10 % to what is left — a few hundred watts by the
afternoon — so the margin shrinks with the need and the plan slides back
into the last slot. The fix plans the pack full by a time fixed for the
day: the end of sun brought forward by 10 % of the whole sunny window
(``pacing_finish_by``). These tests replay a day with a re-plan every two
minutes, the way the coordinator runs it.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from custom_components.solar_energy_management.coordinator.charge_pacing import (
    PACING_HEADROOM_PCT, paced_charge_cap_w, pacing_finish_by,
    today_remaining_slots,
)
from custom_components.solar_energy_management.coordinator.day_ledger import (
    build_day_slots,
)

SUNRISE = datetime(2026, 10, 18, 8, 0)
SUNSET = datetime(2026, 10, 18, 18, 15)
WINDOW = SUNSET - SUNRISE
SHARE_EARLY = WINDOW * (PACING_HEADROOM_PCT / 100.0)    # 61.5 min
FINISH_BY = SUNSET - SHARE_EARLY                         # 17:13:30
STEP = timedelta(minutes=2)

CAPACITY = 20.0
TARGET = 90.0
HOME_W = 500.0
HW_MAX = 5000.0


def _builder(**kw):
    return build_day_slots(price_at=lambda t: None,
                           level_cheap_at=lambda t: False, **kw)


def _ledger(now, day_kwh):
    return today_remaining_slots(
        now=now, sunrise=SUNRISE, sunset=SUNSET, day_kwh=day_kwh,
        home_w_at=lambda t: HOME_W, builder=_builder)


def _decide(now, soc, day_kwh=40.0):
    return paced_charge_cap_w(
        ledger=_ledger(now, day_kwh), capacity_kwh=CAPACITY, soc_pct=soc,
        target_soc_pct=TARGET, floor_soc_pct=35.0, forecast_trusted=True,
        inverter_ac_limit_w=0.0, hw_max_charge_w=HW_MAX, day_start=SUNRISE)


def _replay(start_soc, *, forecast_kwh=40.0, actual=lambda t, w: w):
    """Re-plan every STEP from sunrise; between plans the pack takes
    ``min(cap, actual surplus)`` (no cap = the surplus, up to HW_MAX) or
    covers the house in a deficit. ``actual(t, w)`` turns the modelled
    solar into what the roof really gives. Returns (decisions, full time)."""
    soc, now, plans = start_soc, SUNRISE, []
    while now < SUNSET:
        led = _ledger(now, forecast_kwh)
        d = _decide(now, soc, forecast_kwh)
        plans.append((now, soc, d))
        solar = actual(now, led[0].solar_w)
        surplus = solar - HOME_W
        h = STEP.total_seconds() / 3600.0
        if surplus > 0:
            limit = d.cap_w if d.cap_w is not None else HW_MAX
            soc += min(surplus, limit, HW_MAX) * h / 1000.0 / CAPACITY * 100.0
        else:
            soc += surplus * h / 1000.0 / CAPACITY * 100.0
        now += STEP
        if soc >= TARGET:
            return plans, now
    return plans, None


class TestTheFinishTimeIsFixedForTheDay:
    def test_finish_by_is_the_headroom_share_of_the_whole_window(self):
        assert pacing_finish_by(SUNRISE, SUNSET) == FINISH_BY
        assert FINISH_BY == datetime(2026, 10, 18, 17, 13, 30)

    def test_no_window_no_finish_time(self):
        assert pacing_finish_by(None, SUNSET) is None
        assert pacing_finish_by(SUNSET, SUNRISE) is None


class TestAClearDayWithFrequentReplans:
    """10 kWh to charge (40 → 90 % of 20 kWh), sun 08:00–18:15, 40 kWh."""

    def test_every_replan_predicts_full_by_the_fixed_time(self):
        plans, _ = _replay(40.0)
        paced = [(t, d) for t, _soc, d in plans if d.code in ("paced", "clip")]
        assert len(paced) > 100, "the day was barely paced at all"
        for t, d in paced:
            assert d.finish_by == FINISH_BY.isoformat(), (t, d)
            assert d.full_at is not None, (t, d)
            full = datetime.fromisoformat(d.full_at)
            # the reporter's line: never the last sunny slot. One re-plan
            # step of slack: the model averages each hour, so the last
            # partial slot reads a little dimmer than the plan assumed.
            assert full <= SUNSET - SHARE_EARLY + STEP, (
                f"re-plan at {t:%H:%M} predicts full at {full:%H:%M} — the "
                f"headroom collapsed (sun ends {SUNSET:%H:%M})")

    def test_the_pack_is_full_before_the_fixed_time(self):
        _plans, full = _replay(40.0)
        assert full is not None, "the clear day never filled the pack"
        assert full <= FINISH_BY + STEP, f"full at {full:%H:%M}"

    def test_the_pace_still_spreads_the_charge(self):
        """The headroom is not greed: the pack is not full by noon."""
        _plans, full = _replay(40.0)
        assert full >= datetime(2026, 10, 18, 15, 0), f"full at {full:%H:%M}"

    def test_the_late_afternoon_plan_still_aims_early(self):
        """The collapse in one plan: at 16:30 with 0.6 kWh still to go the
        old pace aimed at the last slot; now it aims at 17:13."""
        d = _decide(datetime(2026, 10, 18, 16, 30), TARGET - 3.0)
        assert d.code == "paced"
        assert datetime.fromisoformat(d.full_at) <= FINISH_BY + timedelta(seconds=1)


class TestAShortDayStillChargesFully:
    def test_clouds_after_noon_still_fill_the_pack(self):
        """The forecast says 40 kWh, the afternoon gives 70 % of it. The
        re-plans keep reading the bright forecast; the headroom and the
        release past finish_by still land the pack full before sunset."""
        def cloudy(t, w):
            return w * (0.7 if t.hour >= 12 else 1.0)
        plans, full = _replay(40.0, actual=cloudy)
        assert full is not None and full <= SUNSET, full
        for t, soc, d in plans:
            if t >= FINISH_BY and soc < TARGET and d.cap_w is not None:
                assert d.cap_w == HW_MAX, (t, d)

    def test_past_finish_by_and_short_the_cap_is_full_power(self):
        d = _decide(datetime(2026, 10, 18, 17, 20), TARGET - 1.0)
        assert d.cap_w == HW_MAX, d

    def test_a_forecast_that_cannot_fill_the_pack_caps_nothing(self):
        d = _decide(datetime(2026, 10, 18, 9, 0), 40.0, day_kwh=10.0)
        assert d.cap_w is None and d.code == "weak_day", d

    def test_without_a_day_window_the_old_margin_still_applies(self):
        """Callers that pass no sunrise keep the 10 % cap margin."""
        now = datetime(2026, 10, 18, 9, 0)
        kw = dict(ledger=_ledger(now, 40.0), capacity_kwh=CAPACITY,
                  soc_pct=40.0, target_soc_pct=TARGET, floor_soc_pct=35.0,
                  forecast_trusted=True, hw_max_charge_w=HW_MAX)
        bare = paced_charge_cap_w(**kw)
        fixed = paced_charge_cap_w(**kw, day_start=SUNRISE)
        assert bare.finish_by is None and fixed.finish_by is not None
        assert fixed.cap_w >= bare.cap_w
        assert bare.cap_w == pytest.approx(fixed.cap_w, rel=0.25)
