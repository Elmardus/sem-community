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


def _decide(fill_kwh, need_kwh, latched):
    """A ledger whose fill at the hardware maximum is ``fill_kwh``."""
    cap = 21.0
    soc = 100.0 - need_kwh / cap * 100.0
    return paced_charge_cap_w(
        ledger=_ledger(fill_kwh / 6.0 * 1000.0), capacity_kwh=cap, soc_pct=soc,
        target_soc_pct=100.0, forecast_trusted=True, hw_max_charge_w=5000.0,
        weak_latched=latched)


def test_a_day_just_under_the_need_stays_paced():
    d = _decide(5.9, 6.0, latched=False)       # 98 % of the need
    assert d.code == "paced" and d.cap_w is not None, d


def test_a_clearly_short_day_is_weak():
    d = _decide(5.0, 6.0, latched=False)       # 83 %
    assert d.code == "weak_day" and d.cap_w is None, d


def test_once_weak_it_stays_weak_until_the_day_fills_the_need():
    assert _decide(5.9, 6.0, latched=True).code == "weak_day"
    assert _decide(6.3, 6.0, latched=True).code == "paced"


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
