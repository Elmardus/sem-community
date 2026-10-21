"""#1160 — a counter report split by the day's start is booked once.

A car charges before the day starts. The charger's counter shows part of
that session before the start and the rest after it. At the new day's
anchor SEM owed the counter only ``pending``: what the box drew since the
counter last moved. Here that is nothing, so the rest was booked on the new
day too, while the old day's row still held the session's power read: on a
row that moves up only (not every charger has a counter of its own).

The rule now: at the day's start a row that moves up only owes each counter
what SEM saw its box draw and the counter has not shown (``unreported``, or
``pending`` if more). All counters together owe no more than the closing
row held above its counters (``_ev_row_held``) plus the new row's anchor.
``unreported`` holds a power read above the meter too, so two reviews
narrowed it: a row the counters own owes ``pending`` only (owing more took
real kWh from the new day; its lag across the start is #1161), and the
part above ``pending`` goes after the late-report wait, as ``unreported``
does (kept, it took kWh from a charge SEM missed later).
"""
from __future__ import annotations

from datetime import timedelta

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
    LINKS_W,
    RECHTS,
    _calc,
    _charge,
    _cycle,
    _idle,
    _night,
    _only_links,
    _restart,
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


def _late_charge(calc, clock, values, *, lag=4, charge_by=False,
                 watts=CHARGE_W, meter=CHARGE_KWH, before=10, after=10):
    """``before`` minutes at ``watts`` up to the day's start and ``after``
    minutes past it; the counter rises ``meter`` a minute, ``lag`` minutes
    late. ``charge_by``: the charger's own row rolls, as the coordinator
    rolls it. Returns the old day's calendar row and Links's own row at the
    old day's end."""
    late = [0.0] * lag
    old = None
    for i, w in enumerate([watts] * (before + after) + [0.0] * (lag + 6)):
        late.append(meter if w else 0.0)
        values[LINKS] += late.pop(0)
        _cycle(calc, clock, values, w)
        if i == before - 1:
            old = (_rows(calc, OLD_DAY)[1], calc.charger_rows["links"])
            if charge_by:
                _new_ev_day(calc)
    return old


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

    def test_a_row_that_drops_in_its_last_cycle_then_moves_up_only(self):
        """The old row drops to 0.6 in its last minute. Rechts's counter is
        dark at the new day's first minute, so the new row moves up only
        and owes ``unreported`` (1.2) — but the old row does not hold it:
        the new day books the rest."""
        with freeze_time("2026-10-11 23:47:30") as clock:
            values, calc = _night()
            _cycle(calc, clock, values)
            _charge(calc, clock, values, 10, meter=0.0)
            values[LINKS] += 0.6
            _cycle(calc, clock, values)
            assert _rows(calc, OLD_DAY)[1] == pytest.approx(0.6, abs=0.01)
            rechts, values[RECHTS] = values[RECHTS], "unavailable"
            _cycle(calc, clock, values)
            assert calc._midnight_ev_baselines["whole"] is None, (
                "vacuous: the new row is owned")
            values[RECHTS] = rechts
            _idle(calc, clock, values, 5)
            values[LINKS] += 1.2
            _cycle(calc, clock, values)
            assert _rows(calc, NEW_DAY)[1] == pytest.approx(1.2, abs=0.01)

    def test_a_charge_across_the_start_with_a_late_counter(self):
        """Only Links has a counter in SEM. The car charges 23:50–00:10 and
        the counter shows each minute four minutes late. The old row keeps
        its 1.8; the new day is 1.8, not 1.8 plus the late 0.72."""
        with freeze_time("2026-10-11 23:49:30") as clock:
            values, calc = _night()
            _only_links(calc)
            old_row, _ = _late_charge(calc, clock, values)
            assert old_row == pytest.approx(1.8, abs=0.01)
            assert _rows(calc, NEW_DAY)[1] == pytest.approx(1.8, abs=0.02)
            assert not calc._midnight_ev_baselines["owed"]  # paid

    def test_a_charge_across_the_charge_by_time_with_a_late_counter(self):
        with freeze_time("2026-10-12 10:49:30") as clock:
            values, calc = _night()
            _only_links(calc)
            old = str(calc._ev_reset_day(None))
            _late_charge(calc, clock, values)
            new = str(calc._ev_reset_day(None))
            assert new != old, "vacuous: the EV day did not roll"
            assert _ev_row(calc, old) == pytest.approx(1.8, abs=0.01)
            assert _ev_row(calc, new) == pytest.approx(1.8, abs=0.02)

    def test_a_row_that_followed_its_counters_up_owes_nothing(self):
        """Rechts's counter reads above its power, so the old row rose to
        the counters (3.0). That row does not hold Links's 1.8: its counter
        had not moved. The new day books it when the counter shows it.
        Before: owed as ``pending``, it was booked on neither day."""
        with freeze_time("2026-10-11 23:40:00") as clock:
            values, calc = _night()
            _cycle(calc, clock, values)
            for _ in range(10):
                values[RECHTS] += 0.3
                _cycle(calc, clock, values, CHARGE_W, 5_000.0)
            _idle(calc, clock, values, 5)
            assert _rows(calc, OLD_DAY)[1] == pytest.approx(3.0, abs=0.06)
            assert calc._ev_counter_pending[LINKS] == pytest.approx(1.8)
            _idle(calc, clock, values, 10)
            assert not calc._midnight_ev_baselines["owed"]
            values[LINKS] += 1.8
            _cycle(calc, clock, values)
            assert _rows(calc, NEW_DAY)[1] == pytest.approx(1.8, abs=0.01)


@pytest.mark.unit
class TestARowTheCountersOwn:
    """The review of a first #1160 fix: a row the counters own owed
    ``unreported`` too, and that holds a power read above the meter."""

    @pytest.mark.parametrize("start", ["2026-10-11 22:59:30",
                                       "2026-10-12 09:59:30"])
    def test_a_high_power_read_takes_nothing_from_the_new_day(self, start):
        """The box draws 6 kW, its power sensor reads 10.8. The car charges
        an hour before the day's start and two after. The new day keeps its
        12 kWh on every row it rolls (first fix: 7.2)."""
        with freeze_time(start) as clock:
            values, calc = _night()
            old = str(calc._ev_reset_day(None))
            charge_by = start.endswith("09:59:30")
            _late_charge(calc, clock, values, lag=0, meter=0.1, before=60,
                         after=120, charge_by=charge_by)
            _idle(calc, clock, values, 150)
            if not charge_by:
                assert _rows(calc, NEW_DAY)[1] == pytest.approx(12.0, abs=0.1)
            else:
                new = str(calc._ev_reset_day(None))
                assert new != old, "vacuous: the EV day did not roll"
                assert _ev_row(calc, new) == pytest.approx(12.0, abs=0.1)
                assert calc.charger_rows["links"] == pytest.approx(
                    12.0, abs=0.1)

    def test_its_late_counter_across_the_start_is_left_open(self):
        """Pinned, left open (#1161): every charger has its own counter and
        the car charges across the start, so the old row keeps its 1.8. The
        new day books the counter's late 0.72 again."""
        with freeze_time("2026-10-11 23:49:30") as clock:
            values, calc = _night()
            old_row, _ = _late_charge(calc, clock, values)
            assert old_row == pytest.approx(1.8, abs=0.01)
            assert _rows(calc, NEW_DAY)[1] == pytest.approx(2.52, abs=0.02)


def _restart_while_charging(calc, clock, values):
    """SEM is down two hours while the car charges 12 kWh."""
    values[LINKS] += 12.0
    calc = _restart(calc, clock, values, timedelta(hours=2), partial=True)
    _idle(calc, clock, values, 5)
    return calc


def _dark_while_charging(calc, clock, values):
    """The power sensor is dark two hours while the car charges 12 kWh."""
    values[LINKS_W] = "unavailable"
    for _ in range(120):
        values[LINKS] += 0.1
        _cycle(calc, clock, values)
    values[LINKS_W] = 0.0
    _idle(calc, clock, values, 5)
    return calc


@pytest.mark.unit
class TestAHighPowerReadIsNotOwedAllDay:
    """The review of a second #1160 fix: on a row that moves up only, the
    part owed above ``pending`` also holds a power read above the meter.
    Kept all day, it took kWh the counter showed later from charging SEM
    did not see."""

    @pytest.mark.parametrize("missed", [_restart_while_charging,
                                        _dark_while_charging])
    def test_a_later_charge_sem_missed_is_booked_in_full(self, missed):
        """Only Links has a counter. 23:00–24:00 the box draws 6 kW and its
        power sensor reads 10.8: 4.8 kWh owed, none of it real. Two and a
        half hours idle drop it; the counter's 12 kWh later are booked
        (second fix: 7.28 and 7.08)."""
        with freeze_time("2026-10-11 22:59:30") as clock:
            values, calc = _night()
            _only_links(calc)
            for _ in range(60):
                values[LINKS] += 0.1
                _cycle(calc, clock, values, CHARGE_W)
            _cycle(calc, clock, values)  # the new day's anchor
            assert calc._midnight_ev_baselines["late"], "vacuous: none late"
            _idle(calc, clock, values, 150)
            assert not calc._midnight_ev_baselines["owed"]
            assert not calc._midnight_ev_baselines["late"]
            calc = missed(calc, clock, values)
            assert _rows(calc, NEW_DAY)[1] == pytest.approx(12.0, abs=0.1)

    def test_the_part_owed_as_pending_stays_after_the_wait(self):
        """0.6 shown at 23:52, then 0.36 more drawn with the counter flat:
        1.56 owed, 0.36 of it ``pending``. A step of 0.6 pays the older
        part first; after the wait the rest of that part goes, and the
        0.36 stays owed (#1159: a rest that never comes is left to the next
        charge)."""
        with freeze_time("2026-10-11 23:40:00") as clock:
            values, calc = _night()
            _only_links(calc)
            _cycle(calc, clock, values)
            _charge(calc, clock, values, 10, meter=0.0)
            values[LINKS] += 0.6
            _cycle(calc, clock, values)
            _charge(calc, clock, values, 2, meter=0.0)
            _idle(calc, clock, values, 7)  # past the day's start
            bl = calc._midnight_ev_baselines
            assert bl["owed"] == pytest.approx({LINKS: 1.56}, abs=0.01)
            assert bl["late"] == pytest.approx({LINKS: 1.2}, abs=0.01)
            values[LINKS] += 0.6
            _cycle(calc, clock, values)
            assert bl["late"] == pytest.approx({LINKS: 0.6}, abs=0.01)
            fresh = _calc(values)
            _only_links(fresh)
            fresh.restore_state(_store(calc))
            assert fresh._midnight_ev_baselines["late"] == bl["late"]
            _idle(calc, clock, values, 150)
            assert bl["owed"] == pytest.approx({LINKS: 0.36}, abs=0.01)
            assert not bl["late"]

    @pytest.mark.parametrize("bad", ["x", None, float("nan"), [1.0]])
    def test_a_damaged_late_value_does_not_raise(self, bad):
        with freeze_time("2026-10-11 23:40:00") as clock:
            values, calc = _night()
            _only_links(calc)
            calc, _ = _split(calc, clock, values)
            calc._midnight_ev_baselines["owed"] = {LINKS: 1.0}
            calc._midnight_ev_baselines["late"] = (
                [1.0] if bad == [1.0] else {LINKS: bad})
            values[LINKS] += 0.5
            _cycle(calc, clock, values)  # the shape check starts afresh


@pytest.mark.unit
class TestWhatIsOwed:
    def _anchor(self, held, *, row=None, anchor=0.0, complete=False,
                pending=None, old_anchor=0.0):
        """A fresh day anchors with Links 1.2 and Rechts 0.6 not shown yet.
        ``complete``: the counters own the new row; else it moves up only."""
        values, calc = _night()
        calc._ev_counter_pending = dict(pending or {})
        calc._ev_counter_unreported = {LINKS: 1.2, RECHTS: 0.6}
        bl = {"date": OLD_DAY, "anchor": old_anchor,
              "base": {LINKS: 10.0, RECHTS: 20.0},
              "last": {LINKS: 10.5, RECHTS: 20.0}}
        if row is not None:
            bl["row"] = row
        elif held is not None:
            bl["row"] = old_anchor + 0.5 + held
        calc._ev_counter_target(bl, NEW_DAY, [LINKS, RECHTS], anchor,
                                complete)
        return bl

    def test_each_counter_owes_what_it_has_not_shown(self):
        assert self._anchor(5.0)["owed"] == pytest.approx(
            {LINKS: 1.2, RECHTS: 0.6})

    def test_no_more_than_the_row_held_shared_by_size(self):
        assert self._anchor(0.9)["owed"] == pytest.approx(
            {LINKS: 0.6, RECHTS: 0.3})

    def test_the_old_rows_anchor_was_shown(self):
        """The old row anchored at 2.0: its counters showed that too."""
        assert self._anchor(0.9, old_anchor=2.0)["owed"] == pytest.approx(
            {LINKS: 0.6, RECHTS: 0.3})

    def test_the_new_rows_anchor_is_held_too(self):
        assert self._anchor(0.6, anchor=0.3)["owed"] == pytest.approx(
            {LINKS: 0.6, RECHTS: 0.3})

    def test_a_row_at_its_counters_owes_nothing(self):
        assert self._anchor(0.0)["owed"] == {}

    def test_a_row_the_counters_own_owes_pending_only(self):
        """``unreported`` may be a power read above the meter: a row the
        counters own would lose that much of the new day."""
        assert self._anchor(5.0, complete=True, pending={LINKS: 0.4})[
            "owed"] == pytest.approx({LINKS: 0.4})
        assert self._anchor(0.1, complete=True, pending={LINKS: 0.4})[
            "owed"] == pytest.approx({LINKS: 0.1})

    def test_pending_counts_when_it_is_more(self):
        values, calc = _night()
        calc._ev_counter_pending = {LINKS: 2.0}
        calc._ev_counter_unreported = {LINKS: 1.2}
        bl = {"date": OLD_DAY, "anchor": 0.0, "base": {}, "last": {},
              "row": 9.0}
        calc._ev_counter_target(bl, NEW_DAY, [LINKS, RECHTS], 0.0, False)
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
        calc._ev_counter_target(bl, NEW_DAY, [LINKS, RECHTS], 0.0, False)
        assert bl["owed"] == pytest.approx({LINKS: 0.4})

    def test_a_closing_day_that_is_not_the_day_before_owes_pending(self):
        """SEM was down a whole day: the stored row is not the one that just
        closed."""
        values, calc = _night()
        calc._ev_counter_pending = {LINKS: 0.4}
        calc._ev_counter_unreported = {LINKS: 1.2}
        bl = {"date": "2026-10-10", "anchor": 0.0, "base": {}, "last": {},
              "row": 9.0}
        calc._ev_counter_target(bl, NEW_DAY, [LINKS, RECHTS], 0.0, False)
        assert bl["owed"] == pytest.approx({LINKS: 0.4})
        assert not bl["late"]

    def test_a_damaged_old_baseline_owes_pending_as_before(self):
        values, calc = _night()
        calc._ev_counter_pending = {LINKS: 0.4}
        calc._ev_counter_unreported = {LINKS: 1.2}
        bl = {"date": OLD_DAY, "anchor": 0.0, "base": {LINKS: "x"},
              "last": {LINKS: 1.0}, "row": 9.0}
        calc._ev_counter_target(bl, NEW_DAY, [LINKS, RECHTS], 0.0, False)
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
