"""#820 (20.10) — a release for a soft reason waits, so the cap does not flap.

@ArneGollin1987: the battery charge switched on and off about every ten
minutes on a clear day. PROD's recorded decisions of 13.10 show the cause:
when the pack's need sat at what the day could fill, the decision went
paced -> weak_day -> paced within minutes, and a one-minute gap in the
forecast read as "no day". Each flip released the cap (a write) and set it
again (another write): 13 register writes in one day, 6 of them releases.
A release for a soft reason (weak_day, trust, a daytime gap) now waits 15
minutes; every other reason (buffer, target, switch off, night, observer)
still gives the cap back at once.
"""
from __future__ import annotations

import asyncio

from custom_components.solar_energy_management.coordinator.charge_pacing import (
    ChargePacingWriter, PACING_SOFT_CODES, PACING_SOFT_HOLD_S,
)

from .test_820_pacing_calm import ENTITY, Clock, TemplateNumber


def _rig():
    clk = Clock(1000.0)
    w = ChargePacingWriter()
    w._clock = clk
    reg = TemplateNumber(5000.0, lo=10, hi=5000, step=100)
    return clk, w, reg, reg.hass()


def _apply(w, hass, reg, cap, **kw):
    act = asyncio.run(w.apply(hass, ENTITY, cap, observer=False,
                              hw_max_w=5000.0, **kw))
    reg.scan()
    return act


def _engage(clk, w, reg, hass):
    assert _apply(w, hass, reg, 1700.0) == "wrote"
    clk.advance(10)


def test_soft_codes_are_the_two_that_flip():
    assert PACING_SOFT_CODES == {"weak_day", "trust"}


def test_a_soft_no_cap_keeps_the_cap():
    clk, w, reg, hass = _rig()
    _engage(clk, w, reg, hass)
    for _ in range(30):                      # five minutes of weak_day
        assert _apply(w, hass, reg, None, soft=True) == "held"
        clk.advance(10)
    assert reg.writes == [1700.0]


def test_a_flip_and_back_writes_nothing():
    """12:39 -> 12:46 on PROD: weak_day, paced, weak_day. One write total."""
    clk, w, reg, hass = _rig()
    _engage(clk, w, reg, hass)
    _apply(w, hass, reg, None, soft=True)
    clk.advance(80)
    assert _apply(w, hass, reg, 1700.0) in ("held", "write_refused")
    clk.advance(300)
    _apply(w, hass, reg, None, soft=True)
    assert reg.writes == [1700.0]


def test_a_soft_no_cap_that_lasts_gives_the_cap_back():
    clk, w, reg, hass = _rig()
    _engage(clk, w, reg, hass)
    clk.advance(PACING_SOFT_HOLD_S + 1)
    assert _apply(w, hass, reg, None, soft=True) == "restored"
    assert reg.writes[-1] == 5000.0


def test_a_hard_no_cap_gives_the_cap_back_at_once():
    """Below the buffer, pacing off, night: not soft."""
    clk, w, reg, hass = _rig()
    _engage(clk, w, reg, hass)
    assert _apply(w, hass, reg, None) == "restored"
    assert reg.writes[-1] == 5000.0


def test_observer_stands_down_at_once_even_when_soft():
    clk, w, reg, hass = _rig()
    _engage(clk, w, reg, hass)
    act = asyncio.run(w.apply(hass, ENTITY, None, observer=True,
                              hw_max_w=5000.0, soft=True))
    assert act == "observer" and not w.engaged


def test_the_wait_counts_from_the_last_wanted_cap():
    clk, w, reg, hass = _rig()
    _engage(clk, w, reg, hass)
    clk.advance(600)
    assert _apply(w, hass, reg, 1700.0) in ("held", "write_refused")
    clk.advance(600)                         # 20 min since the write, 10 since wanted
    assert _apply(w, hass, reg, None, soft=True) == "held"


from custom_components.solar_energy_management.coordinator.charge_pacing import (
    PacingDecision, release_is_soft,
)


def _d(code, need=None, fill=None):
    return PacingDecision(None, "x", code=code, need_kwh=need, fill_kwh=fill)


def test_a_borderline_day_is_soft():
    assert release_is_soft(_d("weak_day", 6.17, 6.06), True, daylight=True)
    assert release_is_soft(_d("weak_day", 9.31, 8.71), True, daylight=True)


def test_a_clearly_short_day_is_not_soft():
    assert not release_is_soft(_d("weak_day", 9.54, 2.19), True, daylight=True)


def test_trust_is_soft_and_buffer_target_night_are_not():
    assert release_is_soft(_d("trust"), True, daylight=True)
    for code in ("buffer", "target"):
        assert not release_is_soft(_d(code), True, daylight=True)


def test_a_gap_is_soft_only_in_daylight():
    assert release_is_soft(_d("none"), True, daylight=True)
    assert not release_is_soft(_d("none"), True, daylight=False)


def test_pacing_off_is_never_soft():
    assert not release_is_soft(_d("weak_day", 6.1, 6.0), False, daylight=True)
    assert not release_is_soft(None, True, daylight=True)
