"""#820 (20.10) — pacing must not flip on a day that just fills the pack.

PROD, 13.10: the decision went paced -> weak_day -> paced within minutes when
the need sat at what the day could fill (6.02 / 6.02 kWh at 12:39), and a
one-minute gap in the forecast read as "no day". Each flip gave the cap back
and wrote it again: 13 register writes in a day, 6 of them releases.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

from custom_components.solar_energy_management.coordinator.charge_pacing import (
    PACING_FORECAST_GAP_S, PACING_WEAK_ENTER, bridge_forecast,
    paced_charge_cap_w,
)

SUNRISE = datetime(2026, 10, 13, 7, 40)


def _ledger(surplus_w, hours=6):
    t0 = datetime(2026, 10, 13, 12, 0)
    return [SimpleNamespace(
        start=t0 + timedelta(hours=i), end=t0 + timedelta(hours=i + 1),
        hours=1.0, soc_kwh=0.0, home_batt_kwh=0.0, solar_w=surplus_w + 500.0,
        cap_override_w=surplus_w, grid_committed_w=0.0) for i in range(hours)]


def _decide(fill_kwh, need_kwh, latched, full=False):
    """A ledger whose fill at the hardware maximum is ``fill_kwh``."""
    cap = 21.0
    soc = 100.0 - need_kwh / cap * 100.0
    return paced_charge_cap_w(
        ledger=_ledger(fill_kwh / 6.0 * 1000.0), capacity_kwh=cap, soc_pct=soc,
        target_soc_pct=100.0, forecast_trusted=True, hw_max_charge_w=5000.0,
        weak_latched=latched, full_latched=full)


def test_a_day_just_under_the_need_stays_paced():
    d = _decide(5.9, 6.0, latched=False)       # 98 % of the need
    assert d.code == "paced" and d.cap_w is not None, d


def test_a_clearly_short_day_is_weak():
    d = _decide(5.0, 6.0, latched=False)       # 83 %
    assert d.code == "weak_day" and d.cap_w is None, d


def test_once_weak_it_stays_weak_until_the_day_clearly_fills_the_need():
    assert _decide(6.1, 6.0, latched=True).code == "weak_day"
    assert _decide(6.5, 6.0, latched=True).code == "paced"


def test_a_gap_under_one_battery_step_is_not_weak():
    # 21 kWh pack: one SOC step is 0.21 kWh. 0.4 of 0.5 kWh is noise.
    d = _decide(0.4, 0.5, latched=False)
    assert d.code != "weak_day", d


def test_the_two_edges_leave_no_flip_on_a_wobbling_forecast():
    codes, latched = [], False
    for fill in (6.0, 5.9, 6.05, 5.8, 6.1, 5.95, 6.0):
        d = _decide(fill, 6.0, latched)
        latched = d.code == "weak_day"
        codes.append(d.code)
    assert set(codes) == {"paced"}, codes


def test_the_enter_edge_is_below_one():
    assert 0.5 < PACING_WEAK_ENTER < 1.0


def test_a_short_forecast_gap_is_bridged():
    day, last = bridge_forecast(40.0, None, 1000.0)
    assert (day, last) == (40.0, (40.0, 1000.0))
    day, last2 = bridge_forecast(None, last, 1060.0)
    assert day == 40.0 and last2 == last


def test_a_long_forecast_gap_is_not():
    _, last = bridge_forecast(40.0, None, 1000.0)
    day, _ = bridge_forecast(None, last, 1000.0 + PACING_FORECAST_GAP_S + 1)
    assert day is None


def test_no_forecast_ever_stays_none():
    assert bridge_forecast(None, None, 5.0) == (None, None)
    assert bridge_forecast(0.0, None, 5.0) == (0.0, None)


def _jumps(caps):
    return sum(1 for a, b in zip(caps, caps[1:]) if abs(b - a) > 0.1 * max(a, b))


def test_the_cap_does_not_jump_back_when_the_forecast_wobbles_around_the_need():
    # .175, 21.10: fill/need 1.01 -> 1.00 -> 0.97 wrote 2503 -> 5000 -> 2503
    # -> 5000 W. Once at full power it stays there until the day clearly
    # fills the need again: one change, not one per wobble.
    caps, weak, full = [], False, False
    for fill in (6.05, 6.0, 5.95, 6.02, 5.9, 6.1, 5.95, 6.05):
        d = _decide(fill, 6.0, weak, full)
        weak, full = d.code == "weak_day", d.cap_w == 5000.0
        caps.append(d.cap_w)
    assert None not in caps, caps
    assert _jumps(caps) <= 1, caps


def test_full_power_lets_go_once_the_day_clearly_fills_the_need():
    assert _decide(6.1, 6.0, False, full=True).cap_w == 5000.0
    assert _decide(6.6, 6.0, False, full=True).cap_w < 5000.0


def _bell_ledger():
    # 08:00-18:00, a sine surplus peaking near 1500 W, every hour a surplus
    import math
    t0 = datetime(2026, 10, 13, 8, 0)
    return [SimpleNamespace(
        start=t0 + timedelta(hours=i), end=t0 + timedelta(hours=i + 1),
        hours=1.0, soc_kwh=0.0, home_batt_kwh=0.0,
        solar_w=800.0 + 1500.0 * math.sin(math.pi * (i + 0.5) / 10),
        cap_override_w=1500.0 * math.sin(math.pi * (i + 0.5) / 10),
        home_w=0.0, grid_committed_w=0.0) for i in range(10)]


def test_a_real_day_shape_with_a_finish_time_does_not_jump_back():
    # review, 21.10: with finish_by set, 54.3 % paced at 1630 W, 54.4 %
    # bisected to 5000 W (not full by finish_by), 55.5 % back to 1496 W.
    ledger = _bell_ledger()
    caps, weak, full = [], False, False
    for soc in (54.0, 54.4, 54.3, 54.5, 54.3, 55.5, 54.4, 55.5):
        d = paced_charge_cap_w(
            ledger=ledger, capacity_kwh=21.0, soc_pct=soc,
            target_soc_pct=100.0, forecast_trusted=True,
            hw_max_charge_w=5000.0, day_start=datetime(2026, 10, 13, 8, 0),
            weak_latched=weak, full_latched=full)
        weak, full = d.code == "weak_day", d.cap_w == 5000.0
        caps.append(d.cap_w)
    assert _jumps([c for c in caps if c is not None]) <= 1, caps
