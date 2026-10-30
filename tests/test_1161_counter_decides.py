"""#1161 — the counter decides the total.

While a car charges across the car day's start (midnight or the Charge-by
time), the old day's row may stand above its counter: it shows the power
read, and a row that moves up only, or one the counters own while a car
charges, cannot move down. Its counter then shows the late minutes on the
new day, and they were booked on both days.

SEM cannot tell a late report from a power read above the meter: the two
twins below hand it the same numbers at the same times. So the counter
decides. What the old day booked above its counter is carried, per
counter, and taken off that counter's next moves, never below 0 for the
day. Twin A comes out exact; twin B keeps the right total, with the error
on the old day, where the power read caused it. A rest rolls over one more
day start, then goes.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from freezegun import freeze_time

from custom_components.solar_energy_management.coordinator.energy_calculator import (
    EV_CATEGORY,
    EnergyCalculator,
)

from . import test_1157_shared_ev_counter as shared
from .ast_contracts import calls
from .test_1101_ev_counter_catch_up import (
    CHARGE_W,
    LINKS,
    RECHTS,
    _charge,
    _cycle,
    _idle,
    _night,
    _only_links,
    _restart,
    _rows,
    _store,
    _calc,
)

OLD_DAY = "2026-10-11"
NEW_DAY = "2026-10-12"


def _twins(calc, clock, values):
    """Ten minutes at 10.8 kW before the day's start, the counter shows
    0.6 at 23:52; ten more after it, then the counter shows 3.0 in one step.
    Twin A: 1.8 and 1.8 drawn, the power read right. Twin B: 0.6 and 3.0
    drawn, the power read 1.8 both times. SEM sees the same. Returns the
    old day's calendar row at its end."""
    _cycle(calc, clock, values)
    _charge(calc, clock, values, 10, meter=0.0)
    values[LINKS] += 0.6
    _cycle(calc, clock, values)
    _idle(calc, clock, values, 7)
    old = _rows(calc, OLD_DAY)[1]
    _idle(calc, clock, values, 5)            # past midnight
    _charge(calc, clock, values, 10, meter=0.0)
    values[LINKS] += 3.0
    _cycle(calc, clock, values)
    _idle(calc, clock, values, 3)
    return old


def _across(calc, clock, values, *, lag=4, before=10, after=10, tail=6,
            charge_by=False):
    """A car charges across the day's start at 10.8 kW, ``before`` minutes
    up to it; the counter shows each minute ``lag`` minutes late.
    ``charge_by``: each charger's own row rolls, as the coordinator rolls
    it. Returns the old day's calendar row and Links's own row at its end."""
    late = [0.0] * lag
    old = None
    for i, w in enumerate([CHARGE_W] * (before + after) + [0.0] * (lag + tail)):
        late.append(0.18 if w else 0.0)
        values[LINKS] += late.pop(0)
        _cycle(calc, clock, values, w)
        if i == before - 1:
            old = (_rows(calc, OLD_DAY)[1], calc.charger_rows["links"])
            if charge_by:
                calc.charger_rows = {c: 0.0 for c in calc.charger_rows}
    return old


@pytest.mark.unit
class TestTheTwins:
    def test_twin_a_is_exact_on_each_day(self):
        with freeze_time("2026-10-11 23:40:00") as clock:
            values, calc = _night()
            _only_links(calc)
            old = _twins(calc, clock, values)
            assert old == pytest.approx(1.8, abs=0.01)
            assert _rows(calc, NEW_DAY)[1] == pytest.approx(1.8, abs=0.01)
            assert not calc._midnight_ev_baselines["owed"]  # paid

    def test_twin_b_keeps_the_total(self):
        """Truth 0.6 and 3.0: the old day shows 1.8 (high), the new day
        1.8 (low), together the counter's 3.6."""
        with freeze_time("2026-10-11 23:40:00") as clock:
            values, calc = _night()
            _only_links(calc)
            start = values[LINKS]
            old = _twins(calc, clock, values)
            new = _rows(calc, NEW_DAY)[1]
            assert old > 0.6 and new < 3.0
            assert old + new == pytest.approx(values[LINKS] - start, abs=0.02)

    @pytest.mark.parametrize("partial", [True, False])
    def test_a_charge_across_the_start(self, partial):
        """Was 2.34 (moves up only) and 2.52 (owned) for 1.8 drawn."""
        with freeze_time("2026-10-11 23:49:30") as clock:
            values, calc = _night()
            if partial:
                _only_links(calc)
            old, _ = _across(calc, clock, values)
            assert old == pytest.approx(1.8, abs=0.01)
            assert _rows(calc, NEW_DAY)[1] == pytest.approx(1.8, abs=0.02)

    def test_a_charge_across_the_charge_by_time(self):
        """The EV-day row and the charger's own row roll at Charge-by."""
        with freeze_time("2026-10-12 10:49:30") as clock:
            values, calc = _night()
            old = str(calc._ev_reset_day(None))
            _, charger_old = _across(calc, clock, values, charge_by=True)
            new = str(calc._ev_reset_day(None))
            assert new != old, "vacuous: the EV day did not roll"
            ev = calc._daily_accumulators
            assert ev[f"{EV_CATEGORY}_{old}"] == pytest.approx(1.8, abs=0.02)
            assert ev[f"{EV_CATEGORY}_{new}"] == pytest.approx(1.8, abs=0.02)
            assert charger_old == pytest.approx(1.8, abs=0.02)
            assert calc.charger_rows["links"] == pytest.approx(1.8, abs=0.02)


@pytest.mark.unit
class TestTheCarry:
    def test_a_day_with_no_car(self):
        with freeze_time("2026-10-11 23:40:00") as clock:
            values, calc = _night()
            _idle(calc, clock, values, 19)
            assert _rows(calc, OLD_DAY)[1] == 0.0
            _idle(calc, clock, values, 20)
            assert _rows(calc, NEW_DAY)[1] == 0.0
            assert not calc._midnight_ev_baselines["owed"]

    def test_a_carry_larger_than_the_new_day(self):
        """The old day read 4.8 above its counter. The new day's counter
        shows 1.8: the day books 0, never less, and 3.0 carries on."""
        with freeze_time("2026-10-11 22:59:30") as clock:
            values, calc = _night()
            _only_links(calc)
            for _ in range(60):
                values[LINKS] += 0.1
                _cycle(calc, clock, values, CHARGE_W)
            _idle(calc, clock, values, 30)
            bl = calc._midnight_ev_baselines
            assert bl["owed"][LINKS] == pytest.approx(4.8, abs=0.1)
            values[LINKS] += 1.8
            _idle(calc, clock, values, 3)
            assert _rows(calc, NEW_DAY)[1] == 0.0
            assert bl["owed"][LINKS] == pytest.approx(3.0, abs=0.1)

    def test_a_rest_rolls_once_then_goes(self):
        """Unpaid at the next start it rolls over once more; at the start
        after that it is gone, so it cannot take a later day's charge."""
        with freeze_time("2026-10-10 22:59:30") as clock:
            values, calc = _night()
            _only_links(calc)
            for _ in range(60):
                values[LINKS] += 0.1
                _cycle(calc, clock, values, CHARGE_W)
            _idle(calc, clock, values, 24 * 60 + 5)
            bl = calc._midnight_ev_baselines
            assert bl["date"] == "2026-10-12"
            assert bl["owed"][LINKS] == pytest.approx(4.8, abs=0.1)
            assert bl["carried"][LINKS] == pytest.approx(4.8, abs=0.1)
            _idle(calc, clock, values, 24 * 60)
            assert not bl["owed"]
            _charge(calc, clock, values, 10)
            assert _rows(calc, "2026-10-13")[1] == pytest.approx(1.8, abs=0.01)

    def test_a_restart_mid_carry(self):
        """The carry lives in the store: half paid before the restart, the
        rest after it."""
        with freeze_time("2026-10-11 23:49:30") as clock:
            values, calc = _night()
            _only_links(calc)
            _across(calc, clock, values, lag=6, after=0, tail=-3)
            bl = calc._midnight_ev_baselines
            assert bl["owed"], "vacuous: nothing carried"
            calc = _restart(calc, clock, values, timedelta(seconds=30),
                            partial=True)
            assert calc._midnight_ev_baselines["owed"] == bl["owed"]
            values[LINKS] += 0.54  # the last three late minutes
            _idle(calc, clock, values, 10)
            assert _rows(calc, NEW_DAY)[1] == pytest.approx(0.0, abs=0.02)
            assert not calc._midnight_ev_baselines["owed"]

    def test_the_drawn_total_survives_the_store(self):
        with freeze_time("2026-10-11 23:40:00") as clock:
            values, calc = _night()
            _charge(calc, clock, values, 3, meter=0.0)
            fresh = _calc(values)
            fresh.restore_state(_store(calc))
            assert fresh._ev_counter_drawn_total == pytest.approx(
                {LINKS: 0.54})

    @pytest.mark.parametrize("bad", ["x", None, float("nan"), [1.0]])
    def test_a_damaged_store_owes_pending_as_before(self, bad):
        values, calc = _night()
        calc._ev_counter_pending = {LINKS: 0.4}
        bl = {"date": OLD_DAY, "anchor": 0.0, "base": {}, "last": {},
              "row": 9.0, "drawn_at": {LINKS: bad}}
        calc._ev_counter_target(bl, NEW_DAY, [LINKS, RECHTS], 0.0, False)
        assert bl["owed"] == pytest.approx({LINKS: 0.4})

    def test_a_store_from_before_1161_owes_pending_as_before(self):
        values, calc = _night()
        calc._ev_counter_pending = {LINKS: 0.4}
        bl = {"date": OLD_DAY, "anchor": 0.0, "base": {}, "last": {},
              "row": 9.0, "late": {LINKS: 1.0}}
        calc._ev_counter_target(bl, NEW_DAY, [LINKS, RECHTS], 0.0, False)
        assert bl["owed"] == pytest.approx({LINKS: 0.4})
        assert "late" not in bl


@pytest.mark.unit
class TestEachCounterItsOwn:
    def test_two_chargers_each_carry_their_own(self):
        """Both cars charge across the Charge-by time; Links's counter is 4
        minutes late, Rechts's in step. Each charger's own row adds up to
        its counter, the fleet row to both."""
        with freeze_time("2026-10-12 10:49:30") as clock:
            values, calc = _night()
            old_day = str(calc._ev_reset_day(None))
            start = dict(values)
            late = [0.0] * 4
            old_rows = old_fleet = None
            for i, w in enumerate([CHARGE_W] * 20 + [0.0] * 10):
                late.append(0.18 if w else 0.0)
                values[LINKS] += late.pop(0)
                values[RECHTS] += 0.1 if w else 0.0
                _cycle(calc, clock, values, w, 6_000.0 if w else 0.0)
                if i == 9:
                    old_rows = dict(calc.charger_rows)
                    old_fleet = _rows(calc)[0]
                    calc.charger_rows = {c: 0.0 for c in calc.charger_rows}
            links = values[LINKS] - start[LINKS]
            rechts = values[RECHTS] - start[RECHTS]
            assert old_rows["links"] + calc.charger_rows["links"] == (
                pytest.approx(links, abs=0.03))
            assert old_rows["rechts"] + calc.charger_rows["rechts"] == (
                pytest.approx(rechts, abs=0.03))
            assert str(calc._ev_reset_day(None)) != old_day, (
                "vacuous: the EV day did not roll")
            assert old_fleet + _rows(calc)[0] == pytest.approx(
                links + rechts, abs=0.05)

    def test_one_counters_carry_is_not_paid_by_another(self):
        """Rechts read 0.6 above its meter before midnight; only Links's
        counter moves after it. Links's move books in full."""
        with freeze_time("2026-10-11 23:49:30") as clock:
            values, calc = _night()
            calc.configure_ev_counters(calc._hass, [LINKS, RECHTS], True,
                                       complete=False,
                                       owners={LINKS: "links",
                                               RECHTS: "rechts"})
            for _ in range(10):
                values[RECHTS] += 0.12
                _cycle(calc, clock, values, 0.0, CHARGE_W)
            _idle(calc, clock, values, 5)
            assert calc._midnight_ev_baselines["owed"] == pytest.approx(
                {RECHTS: 0.6}, abs=0.01)
            _charge(calc, clock, values, 10)
            _idle(calc, clock, values, 2)
            assert _rows(calc, NEW_DAY)[1] == pytest.approx(1.8, abs=0.02)

    def test_a_shared_counter(self):
        """One counter counts both boxes (#1157): it carries what both
        drew above it, and its moves pay it."""
        values = {shared.DAILY: 100.0}
        with freeze_time("2026-10-11 23:49:00") as clock:
            calc = shared._calc(values, shared.DAILY, ("links", "rechts"))
            shared._read(calc, values)
            values[shared.DAILY] += 0.002
            shared._read(calc, values)
            start = values[shared.DAILY]
            late = [0.0] * 4
            for i, w in enumerate([CHARGE_W] * 20 + [0.0] * 10):
                late.append(0.18 + 0.1 if w else 0.0)
                values[shared.DAILY] += late.pop(0)
                shared._cycle(calc, clock, values, w, 6_000.0 if w else 0.0)
                if i == 9:
                    old = _rows(calc, OLD_DAY)[1]
            assert calc._midnight_ev_baselines["date"] == NEW_DAY
            new = _rows(calc, NEW_DAY)[1]
            assert old + new == pytest.approx(
                values[shared.DAILY] - start, abs=0.05)


@pytest.mark.unit
class TestTheWiring:
    def test_only_the_days_start_carries(self):
        assert calls(EnergyCalculator._ev_counter_target, "_ev_carry_at_start")

