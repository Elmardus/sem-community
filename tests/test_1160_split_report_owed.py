"""#1160 — a counter report split by the day's start is booked once.

A car charges before the day starts. The charger's counter shows part of
that session before the start and the rest after it. At the new day's
anchor SEM owed the counter only ``pending``: what the box drew since the
counter last moved. Here that is nothing, so the rest was booked on the new
day too.

That is wrong whenever the old day's row still holds the power read of the
session: on a row that moves up only (not every charger has a counter of
its own), and on a row the counters own while a car charges across the
start (the row may not move down then). Where the old row followed its
counters down, the new day must book the rest.

The rule now: at the day's start, a counter owes what SEM saw its box draw
and the counter has not shown (``unreported``, or ``pending`` if more), but
all counters together no more than the closing row held above its counters
(``_ev_row_held``) plus the new row's anchor.
"""
from __future__ import annotations

import pytest
from freezegun import freeze_time

from custom_components.solar_energy_management.coordinator.energy_calculator import (
    EV_CATEGORY,
    EnergyCalculator,
)

from .ast_contracts import calls
from .test_1101_ev_counter_catch_up import (
    CHARGE_KWH,
    CHARGE_W,
    LINKS,
    RECHTS,
    _calc,
    _charge,
    _cycle,
    _idle,
    _night,
    _only_links,
    _rows,
    _store,
)

OLD_DAY = "2026-10-11"
NEW_DAY = "2026-10-12"


def _ev_row(calc, ev_day: str) -> float:
    return round(calc._daily_accumulators.get(f"{EV_CATEGORY}_{ev_day}", 0.0), 2)


def _split(calc, clock, values, *, before=0.6, after=1.2, restart=None):
    """1.8 kWh drawn in ten minutes, the counter flat. It shows ``before``
    one cycle after the stop, the rest 15 cycles later — after the day's
    start when the clock starts at xx:40. ``restart``: SEM is stored and
    comes back that much later, just before the rest. Returns the
    calculator (a fresh one after a restart) and the old day's calendar
    row at its end."""
    _cycle(calc, clock, values)
    _charge(calc, clock, values, 10, meter=0.0)
    values[LINKS] += before
    _cycle(calc, clock, values)
    _idle(calc, clock, values, 4)
    old_row = _rows(calc, OLD_DAY)[1]
    if restart is not None:
        partial = calc._ev_counter_entities == [LINKS]
        stored = _store(calc)
        clock.tick(restart)
        fresh = _calc(values)
        if partial:
            _only_links(fresh)
        fresh.restore_state(stored)
        fresh.charger_rows = dict(calc.charger_rows)
        calc = fresh
    _idle(calc, clock, values, 10)
    values[LINKS] += after
    _cycle(calc, clock, values)
    _idle(calc, clock, values, 2)
    return calc, old_row


def _new_ev_day(calc):
    """The coordinator zeroes each charger's own row when its EV day rolls,
    before it holds the row to the counter (``_cycle`` does not)."""
    calc.charger_rows = {cid: 0.0 for cid in calc.charger_rows}


def _late_charge(calc, clock, values, *, charge_by=False):
    """20 minutes at 10.8 kW from ten minutes before the day's start; the
    counter shows each minute three minutes late. Returns the old day's
    calendar row and Links's own row at the old day's end."""
    late = [0.0, 0.0, 0.0]
    before = None
    for i, watts in enumerate([CHARGE_W] * 20 + [0.0] * 6):
        late.append(CHARGE_KWH if watts else 0.0)
        values[LINKS] += late.pop(0)
        _cycle(calc, clock, values, watts)
        if i == 9:
            before = (_rows(calc, OLD_DAY)[1], calc.charger_rows["links"])
            if charge_by:
                _new_ev_day(calc)
    return before


@pytest.mark.unit
class TestTheRestIsBookedOnce:
    def test_a_row_that_moves_up_only(self):
        """THE issue. Only Links has a counter in SEM, so the rows move up
        only and the old day keeps the 1.8 the power read saw. The counter
        shows 0.6 at 23:52 and 1.2 at 00:07. Before: the new day booked
        the 1.2 again."""
        with freeze_time("2026-10-11 23:40:00") as clock:
            values, calc = _night()
            _only_links(calc)
            calc, old_row = _split(calc, clock, values)
            assert old_row == pytest.approx(1.8, abs=0.01)
            assert _rows(calc, NEW_DAY)[1] == pytest.approx(0.0, abs=0.01)
            assert not calc._midnight_ev_baselines["owed"]  # paid

    def test_a_row_that_moves_up_only_at_the_charge_by_time(self):
        """The EV-day row rolls at Charge-by (11:00): the same rule."""
        with freeze_time("2026-10-12 10:40:00") as clock:
            values, calc = _night()
            _only_links(calc)
            old = str(calc._ev_reset_day(None))
            calc, _ = _split(calc, clock, values)
            new = str(calc._ev_reset_day(None))
            assert new != old, "vacuous: the EV day did not roll"
            assert _ev_row(calc, old) == pytest.approx(1.8, abs=0.01)
            assert _ev_row(calc, new) == pytest.approx(0.0, abs=0.01)

    def test_across_a_restart_over_the_days_start(self):
        """SEM is down from 23:56 to 00:03: the closing row's figure comes
        back from the store."""
        with freeze_time("2026-10-11 23:40:00") as clock:
            values, calc = _night()
            _only_links(calc)
            calc, _ = _split(calc, clock, values, restart=420)
            assert _rows(calc, NEW_DAY)[1] == pytest.approx(0.0, abs=0.01)

    def test_a_row_the_counters_own_that_followed_them_down(self):
        """Every charger has its own counter. The box stopped and the
        counter reported before the day's start, so the old row dropped to
        0.6: the new day books the rest. Kept as before #1160."""
        with freeze_time("2026-10-11 23:40:00") as clock:
            values, calc = _night()
            calc, old_row = _split(calc, clock, values)
            assert old_row == pytest.approx(0.6, abs=0.01)
            assert _rows(calc, NEW_DAY)[1] == pytest.approx(1.2, abs=0.01)

    @pytest.mark.parametrize("start", ["2026-10-11 23:47:30",
                                       "2026-10-12 10:47:30"])
    def test_a_row_that_drops_in_its_last_cycle(self, start):
        """The counter shows 0.6 in the old day's last minute, and every
        row the counters own drops to it then: the calendar row at
        midnight, the EV-day row and the charger's own row at Charge-by.
        The new day books the rest."""
        with freeze_time(start) as clock:
            values, calc = _night()
            old = str(calc._ev_reset_day(None))
            _cycle(calc, clock, values)
            _charge(calc, clock, values, 10, meter=0.0)
            values[LINKS] += 0.6
            _cycle(calc, clock, values)
            calendar_before = _rows(calc, OLD_DAY)[1]
            charger_before = calc.charger_rows["links"]
            if not start.endswith("23:47:30"):
                _new_ev_day(calc)
            _idle(calc, clock, values, 5)
            values[LINKS] += 1.2
            _cycle(calc, clock, values)
            _idle(calc, clock, values, 2)
            if start.endswith("23:47:30"):
                assert calendar_before == pytest.approx(0.6, abs=0.01)
                assert _rows(calc, NEW_DAY)[1] == pytest.approx(1.2, abs=0.01)
            else:
                new = str(calc._ev_reset_day(None))
                assert new != old, "vacuous: the EV day did not roll"
                assert _ev_row(calc, old) == pytest.approx(0.6, abs=0.01)
                assert _ev_row(calc, new) == pytest.approx(1.2, abs=0.01)
                assert charger_before == pytest.approx(0.6, abs=0.01)
                assert calc.charger_rows["links"] == pytest.approx(1.2, abs=0.01)

    def test_a_charge_across_the_start_with_a_late_counter(self):
        """Every charger has its own counter, the car charges 23:50–00:10
        and the counter shows each minute three minutes late. The old row
        may not move down while the car charges, so it keeps its 1.8. The
        new day is 1.8, not 1.8 plus the late 0.54."""
        with freeze_time("2026-10-11 23:49:30") as clock:
            values, calc = _night()
            old_row, _ = _late_charge(calc, clock, values)
            assert old_row == pytest.approx(1.8, abs=0.01)
            assert _rows(calc, NEW_DAY)[1] == pytest.approx(1.8, abs=0.02)
            assert not calc._midnight_ev_baselines["owed"]  # paid

    def test_a_charge_across_the_charge_by_time_with_a_late_counter(self):
        """The EV-day row and the charger's own row, at 11:00."""
        with freeze_time("2026-10-12 10:49:30") as clock:
            values, calc = _night()
            old = str(calc._ev_reset_day(None))
            _, charger_before = _late_charge(calc, clock, values,
                                             charge_by=True)
            new = str(calc._ev_reset_day(None))
            assert new != old, "vacuous: the EV day did not roll"
            assert _ev_row(calc, old) == pytest.approx(1.8, abs=0.01)
            assert _ev_row(calc, new) == pytest.approx(1.8, abs=0.02)
            assert charger_before == pytest.approx(1.8, abs=0.01)
            assert calc.charger_rows["links"] == pytest.approx(1.8, abs=0.02)

    def test_a_row_that_followed_its_counters_up_owes_nothing(self):
        """Rechts's counter reads above its power, so the old row rose to
        the counters (3.6) and does not hold Links's 1.2 still to come: the
        new day books it."""
        with freeze_time("2026-10-11 23:40:00") as clock:
            values, calc = _night()
            _cycle(calc, clock, values)
            for _ in range(10):
                values[RECHTS] += 0.3
                _cycle(calc, clock, values, CHARGE_W, 5_000.0)
            values[LINKS] += 0.6
            _cycle(calc, clock, values)
            _idle(calc, clock, values, 4)
            assert _rows(calc, OLD_DAY)[1] == pytest.approx(3.6, abs=0.01)
            _idle(calc, clock, values, 10)
            assert not calc._midnight_ev_baselines["owed"]
            values[LINKS] += 1.2
            _cycle(calc, clock, values)
            assert _rows(calc, NEW_DAY)[1] == pytest.approx(1.2, abs=0.01)


@pytest.mark.unit
class TestWhatIsOwed:
    def _anchor(self, held, *, row=None, anchor=0.0):
        """A fresh day anchors with Links 1.2 and Rechts 0.6 not shown yet."""
        values, calc = _night()
        calc._ev_counter_pending = {}
        calc._ev_counter_unreported = {LINKS: 1.2, RECHTS: 0.6}
        bl = {"date": OLD_DAY, "anchor": 0.0,
              "base": {LINKS: 10.0, RECHTS: 20.0},
              "last": {LINKS: 10.5, RECHTS: 20.0}}
        if row is not None:
            bl["row"] = row
        elif held is not None:
            bl["row"] = 0.5 + held
        calc._ev_counter_target(bl, NEW_DAY, [LINKS, RECHTS], anchor, True)
        return bl

    def test_each_counter_owes_what_it_has_not_shown(self):
        assert self._anchor(5.0)["owed"] == pytest.approx(
            {LINKS: 1.2, RECHTS: 0.6})

    def test_no_more_than_the_row_held_shared_by_size(self):
        assert self._anchor(0.9)["owed"] == pytest.approx(
            {LINKS: 0.6, RECHTS: 0.3})

    def test_the_new_rows_anchor_is_held_too(self):
        assert self._anchor(0.6, anchor=0.3)["owed"] == pytest.approx(
            {LINKS: 0.6, RECHTS: 0.3})

    def test_a_row_at_its_counters_owes_nothing(self):
        assert self._anchor(0.0)["owed"] == {}

    def test_pending_counts_when_it_is_more(self):
        values, calc = _night()
        calc._ev_counter_pending = {LINKS: 2.0}
        calc._ev_counter_unreported = {LINKS: 1.2}
        bl = {"date": OLD_DAY, "anchor": 0.0, "base": {}, "last": {},
              "row": 9.0}
        calc._ev_counter_target(bl, NEW_DAY, [LINKS, RECHTS], 0.0, True)
        assert bl["owed"] == pytest.approx({LINKS: 2.0})

    @pytest.mark.parametrize("row", ["x", None, float("nan"), True, -1.0])
    def test_no_or_a_damaged_row_owes_pending_as_before(self, row):
        """A store from before #1160, or a damaged one: only ``pending``."""
        values, calc = _night()
        calc._ev_counter_pending = {LINKS: 0.4}
        calc._ev_counter_unreported = {LINKS: 1.2}
        bl = {"date": OLD_DAY, "anchor": 0.0, "base": {}, "last": {}}
        if row is not None:
            bl["row"] = row
        calc._ev_counter_target(bl, NEW_DAY, [LINKS, RECHTS], 0.0, True)
        assert bl["owed"] == pytest.approx({LINKS: 0.4})

    def test_a_damaged_old_baseline_owes_pending_as_before(self):
        values, calc = _night()
        calc._ev_counter_pending = {LINKS: 0.4}
        calc._ev_counter_unreported = {LINKS: 1.2}
        bl = {"date": OLD_DAY, "anchor": 0.0, "base": {LINKS: "x"},
              "last": {LINKS: 1.0}, "row": 9.0}
        calc._ev_counter_target(bl, NEW_DAY, [LINKS, RECHTS], 0.0, True)
        assert bl["owed"] == pytest.approx({LINKS: 0.4})

    def test_a_counter_that_goes_back_mid_day_owes_pending_as_before(self):
        """Only the day's start compares rows: a reset later that day
        anchors as before."""
        with freeze_time("2026-10-11 23:40:00") as clock:
            values, calc = _night()
            _only_links(calc)
            calc, _ = _split(calc, clock, values)
            _charge(calc, clock, values, 10, meter=0.0)
            assert calc._midnight_ev_baselines["row"] == pytest.approx(
                1.8, abs=0.01), "vacuous: the row holds nothing"
            values[LINKS] = 1.0  # the box was replaced
            _cycle(calc, clock, values)
            assert not calc._midnight_ev_baselines["owed"]

    def test_the_rows_figure_survives_the_store(self):
        with freeze_time("2026-10-11 23:40:00") as clock:
            values, calc = _night()
            _cycle(calc, clock, values, CHARGE_W)
            row = calc._midnight_ev_baselines["row"]
            assert row == pytest.approx(CHARGE_KWH)
            fresh = _calc(values)
            fresh.restore_state(_store(calc))
            assert fresh._midnight_ev_baselines["row"] == row
            assert fresh._charger_counter_baselines["links"]["row"] == (
                calc._charger_counter_baselines["links"]["row"])


@pytest.mark.unit
class TestTheWiring:
    def test_only_the_days_start_compares_rows(self):
        assert calls(EnergyCalculator._ev_counter_target, "_ev_row_held")
