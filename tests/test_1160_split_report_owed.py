"""#1160 — a counter report split by the day's start is booked once.

A car charges before the day starts. The charger's counter shows part of
that session before the start and the rest after it. At the new day's
anchor SEM owed the counter only ``pending``: what the box drew since the
counter last moved. Here that is nothing, so the rest was booked on the new
day too, while the old day's row still held the session's power read: on a
row that moves up only (not every charger has a counter of its own).

The rule now (#1161, which replaced the #1160 rules on ``late``): the
counter decides the total. At the day's start each counter owes what SEM
saw its boxes draw that day above what it showed, no more in all than the
closing row held above its counters plus the new row's anchor. Its next
moves pay it, whatever the box does. A power read above the meter is then
taken off the next day: the two days add up to the counter. See
``test_1161_counter_decides``.
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
    OWNERS,
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

    @pytest.mark.parametrize("restart", [False, True])
    def test_a_counter_flat_for_a_whole_day(self, restart):
        """Friday 22:00–23:00 the box draws 10.8 kWh, its power on the
        meter; the counter shows it only on Sunday at 00:30. Saturday's row
        still owed it: Sunday owes it too and books none, as before #1160
        (sixth fix: 10.8)."""
        with freeze_time("2026-10-10 22:00:00") as clock:
            values, calc = _night()
            _only_links(calc)
            for _ in range(60):
                _cycle(calc, clock, values, CHARGE_W)
            _idle(calc, clock, values, 13 * 60)
            if restart:
                calc = _restart(calc, clock, values, timedelta(seconds=1),
                                partial=True)
            _idle(calc, clock, values, 12 * 60 + 30)
            values[LINKS] += 10.8
            _idle(calc, clock, values, 5)
            assert _rows(calc, NEW_DAY)[1] == pytest.approx(0.0, abs=0.1)

    def test_each_counter_owes_its_own(self):
        """Two counters on a row that moves up only. Rechts's power read
        0.6 above its meter before 23:00, Links's 0.6 below until 23:30;
        then Links drew 5.04 with its counter flat until 00:20. Links owes
        what its day drew above what it showed (4.44), Rechts its 0.6: the
        new day books Links's 0.6, so Links's days add up to its counter
        (#1161)."""
        with freeze_time("2026-10-11 21:58:00") as clock:
            values, calc = _night()
            calc.configure_ev_counters(calc._hass, [LINKS, RECHTS], True,
                                       complete=False, owners=OWNERS)
            _idle(calc, clock, values, 2)
            for _ in range(60):
                values[RECHTS] += 0.17
                _cycle(calc, clock, values, 0.0, CHARGE_W)
            for _ in range(30):
                values[LINKS] += 0.12
                _cycle(calc, clock, values, 6_000.0)
            _cycle(calc, clock, values)
            for _ in range(28):
                _cycle(calc, clock, values, CHARGE_W)
            _idle(calc, clock, values, 21)
            assert calc._midnight_ev_baselines["owed"] == pytest.approx(
                {LINKS: 4.44, RECHTS: 0.6}, abs=0.02)
            values[LINKS] += 5.04
            _idle(calc, clock, values, 180)
            assert _rows(calc, NEW_DAY)[1] == pytest.approx(0.6, abs=0.05)

    def test_a_rest_after_a_restart_is_booked_once(self):
        """Was left open: after time SEM did not watch, the rest that came
        then was booked again (1.2). The counter decides (#1161)."""
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

    def test_a_charge_across_the_start(self):
        """Only Links has a counter, the car charges 23:50–00:10 and the
        counter shows each minute four minutes late. Was 2.34, not 1.8: the
        late part was booked again (#1161)."""
        with freeze_time("2026-10-11 23:49:30") as clock:
            values, calc = _night()
            _only_links(calc)
            old_row, _ = _late_charge(calc, clock, values)
            assert old_row == pytest.approx(1.8, abs=0.01)
            assert _rows(calc, NEW_DAY)[1] == pytest.approx(1.8, abs=0.02)

    def test_a_rest_shown_while_the_next_session_runs(self):
        """The rest comes in a move while the box charges again: it pays
        what is owed all the same. Was 3.0, not 1.8 (#1161)."""
        with freeze_time("2026-10-11 23:40:00") as clock:
            values, calc = _night()
            _only_links(calc)
            _cycle(calc, clock, values)
            _charge(calc, clock, values, 10, meter=0.0)
            values[LINKS] += 0.6
            _cycle(calc, clock, values)
            _idle(calc, clock, values, 14)
            assert calc._midnight_ev_baselines["owed"], "vacuous: none owed"
            values[LINKS] += 1.2
            _charge(calc, clock, values, 10)
            _idle(calc, clock, values, 2)
            assert _rows(calc, NEW_DAY)[1] == pytest.approx(1.8, abs=0.02)

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
    """A row the counters own cannot move down while a car charges, so at
    the day's start it can stand above its counters too (#1161)."""

    @pytest.mark.parametrize("start", ["2026-10-11 22:59:30",
                                       "2026-10-12 09:59:30"])
    def test_a_high_power_read_is_taken_off_the_new_day(self, start):
        """The box draws 6 kW, its power sensor reads 10.8. The car charges
        an hour before the day's start and two after. The old day keeps the
        10.8 it read, the new day books 7.2: together the counter's 18, on
        every row it rolls. The decision of #1161 (before: 12)."""
        with freeze_time(start) as clock:
            values, calc = _night()
            old = str(calc._ev_reset_day(None))
            charge_by = start.endswith("09:59:30")
            _late_charge(calc, clock, values, lag=0, meter=0.1, before=60,
                         after=120, charge_by=charge_by)
            _idle(calc, clock, values, 150)
            if not charge_by:
                assert _rows(calc, NEW_DAY)[1] == pytest.approx(7.2, abs=0.1)
            else:
                new = str(calc._ev_reset_day(None))
                assert new != old, "vacuous: the EV day did not roll"
                assert _ev_row(calc, new) == pytest.approx(7.2, abs=0.1)
                assert calc.charger_rows["links"] == pytest.approx(
                    7.2, abs=0.1)

    def test_its_late_counter_across_the_start(self):
        """Every charger has its own counter and the car charges across the
        start, so the old row keeps its 1.8. Was 2.52: the new day booked
        the counter's late 0.72 again (#1161)."""
        with freeze_time("2026-10-11 23:49:30") as clock:
            values, calc = _night()
            old_row, _ = _late_charge(calc, clock, values)
            assert old_row == pytest.approx(1.8, abs=0.01)
            assert _rows(calc, NEW_DAY)[1] == pytest.approx(1.8, abs=0.02)


def _high_read(calc, clock, values, minutes):
    """The box draws 6 kW; its power sensor reads 10.8."""
    for _ in range(minutes):
        values[LINKS] += 0.1
        _cycle(calc, clock, values, CHARGE_W)


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


def _missed_later(idle):
    def run(calc, clock, values):
        _idle(calc, clock, values, idle)
        return _restart_while_charging(calc, clock, values)
    return run


def _dark_later(calc, clock, values):
    _idle(calc, clock, values, 150)
    return _dark_while_charging(calc, clock, values)


def _seen_session(shown_after):
    def run(calc, clock, values):
        _idle(calc, clock, values, 30)
        _charge(calc, clock, values, 60,
                meter=0.0 if shown_after else CHARGE_KWH)
        if shown_after:
            values[LINKS] += 10.8
        _idle(calc, clock, values, 30)
        return _restart_while_charging(calc, clock, values)
    return run


def _counter_dark_through_the_wait(calc, clock, values):
    real, values[LINKS] = values[LINKS], "unavailable"
    _idle(calc, clock, values, 150)
    values[LINKS] = real
    return _restart_while_charging(calc, clock, values)


def _counter_dark_after_a_restart(calc, clock, values):
    _idle(calc, clock, values, 30)
    real, values[LINKS] = values[LINKS] + 12.0, "unavailable"
    calc = _restart(calc, clock, values, timedelta(hours=2), partial=True)
    _cycle(calc, clock, values)
    values[LINKS] = real
    _idle(calc, clock, values, 5)
    return calc


def _shown_cycles_later(calc, clock, values):
    _idle(calc, clock, values, 30)
    calc = _restart(calc, clock, values, timedelta(hours=2), partial=True)
    _idle(calc, clock, values, 2)
    values[LINKS] += 12.0
    _idle(calc, clock, values, 5)
    return calc


def _cloud_dark(calc, clock, values):
    _idle(calc, clock, values, 30)
    real = values[LINKS]
    values[LINKS] = values[LINKS_W] = "unavailable"
    _idle(calc, clock, values, 120)
    values[LINKS], values[LINKS_W] = real + 12.0, 0.0
    _idle(calc, clock, values, 5)
    return calc


def _shown_hours_later(calc, clock, values):
    _idle(calc, clock, values, 30)
    calc = _restart(calc, clock, values, timedelta(hours=1), partial=True)
    _idle(calc, clock, values, 60)
    for _ in range(10):
        _cycle(calc, clock, values, CHARGE_W)
    _idle(calc, clock, values, 55)
    values[LINKS] += 6.0 + 1.8
    _idle(calc, clock, values, 3)
    return calc


def _counter_dark_through_a_charge(calc, clock, values):
    _idle(calc, clock, values, 30)
    real, values[LINKS] = values[LINKS], "unavailable"
    for _ in range(60):
        _cycle(calc, clock, values, 6_000.0)
    _idle(calc, clock, values, 10)
    values[LINKS] = real + 10.8
    _idle(calc, clock, values, 5)
    return calc


@pytest.mark.unit
class TestAHighPowerReadIsTakenOffTheNextDay:
    """The #1160 reviews found the power read above the meter, carried to
    the new day, taking kWh from it, and seven rounds of rules kept it off.
    The decision of #1161: the counter decides the total. 23:00–24:00 the
    box draws 6 kW, its power sensor reads 10.8: the old day keeps 10.8,
    and the new day books what its counter shows less the 4.8. Whatever
    the new day brings, the two days add up to the counter."""

    @pytest.mark.parametrize("then", [
        _missed_later(150), _dark_later, _missed_later(30),
        _seen_session(False), _seen_session(True),
        _counter_dark_through_the_wait, _counter_dark_after_a_restart,
        _shown_cycles_later, _cloud_dark, _shown_hours_later,
        _counter_dark_through_a_charge,
    ])
    def test_the_two_days_add_up_to_the_counter(self, then):
        with freeze_time("2026-10-11 22:59:30") as clock:
            values, calc = _night()
            _only_links(calc)
            start = values[LINKS]
            _high_read(calc, clock, values, 60)
            old = _rows(calc, OLD_DAY)[1]
            _cycle(calc, clock, values)  # the new day's anchor
            assert calc._midnight_ev_baselines["owed"], "vacuous: none owed"
            calc = then(calc, clock, values)
            new = _rows(calc, NEW_DAY)[1]
            assert old == pytest.approx(10.8, abs=0.01)
            # 0.08: the old day's anchor cycle read 0.18, its counter 0.1.
            # A row that moves up only takes its counters in steps of 0.5
            # (``RECONCILIATION_THRESHOLD``): up to that much stays out.
            assert old + new == pytest.approx(
                values[LINKS] - start + 0.08, abs=0.25)

    @pytest.mark.parametrize("past", [1, 30, 60])
    def test_a_session_past_the_start(self, past):
        with freeze_time("2026-10-11 22:59:30") as clock:
            values, calc = _night()
            _only_links(calc)
            start = values[LINKS]
            _high_read(calc, clock, values, 60)
            old = _rows(calc, OLD_DAY)[1]
            _high_read(calc, clock, values, past)
            _idle(calc, clock, values, 150)
            calc = _restart_while_charging(calc, clock, values)
            new = _rows(calc, NEW_DAY)[1]
            assert old + new == pytest.approx(
                values[LINKS] - start + 0.08, abs=0.1)

    def test_an_hourly_counter(self):
        """The counter reports hourly: +6 at 23:00, +3 at 00:10, while the
        box drew 9 and its power read 16.2. A charge SEM missed later pays
        the rest: the two days add up to the counter's 21."""
        with freeze_time("2026-10-11 21:59:30") as clock:
            values, calc = _night()
            _only_links(calc)
            start = shown = values[LINKS]
            for i in range(90):
                _cycle(calc, clock, values, CHARGE_W)
                if i == 59:
                    values[LINKS] = shown + 6.0
            old = _rows(calc, OLD_DAY)[1]
            _idle(calc, clock, values, 40)
            values[LINKS] = shown + 9.0
            _idle(calc, clock, values, 150)
            values[LINKS] += 12.0
            calc = _restart(calc, clock, values, timedelta(hours=2),
                            partial=True)
            _idle(calc, clock, values, 5)
            new = _rows(calc, NEW_DAY)[1]
            assert old == pytest.approx(16.2, abs=0.01)
            assert old + new == pytest.approx(values[LINKS] - start, abs=0.2)

    def test_a_late_report_in_steps_pays_it_in_steps(self):
        """0.6 shown at 23:52, then 0.36 more drawn with the counter flat:
        1.56 owed. Each step pays it, after the wait too, and the store
        keeps what is left."""
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
            values[LINKS] += 0.6
            _cycle(calc, clock, values)
            assert bl["owed"] == pytest.approx({LINKS: 0.96}, abs=0.01)
            fresh = _calc(values)
            _only_links(fresh)
            fresh.restore_state(_store(calc))
            assert fresh._midnight_ev_baselines["owed"] == bl["owed"]
            _idle(calc, clock, values, 150)
            values[LINKS] += 0.96
            _cycle(calc, clock, values)
            assert not bl["owed"]
            assert _rows(calc, NEW_DAY)[1] == pytest.approx(0.0, abs=0.01)

    @pytest.mark.parametrize("bad", ["x", None, float("nan"), [1.0]])
    def test_a_damaged_carried_value_does_not_raise(self, bad):
        with freeze_time("2026-10-11 23:40:00") as clock:
            values, calc = _night()
            _only_links(calc)
            calc, _ = _split(calc, clock, values)
            calc._midnight_ev_baselines["owed"] = {LINKS: 1.0}
            calc._midnight_ev_baselines["carried"] = (
                [1.0] if bad == [1.0] else {LINKS: bad})
            values[LINKS] += 0.5
            _cycle(calc, clock, values)  # the shape check starts afresh
            assert calc._midnight_ev_baselines["carried"] == {}
            assert calc._midnight_ev_baselines["owed"] == {}


@pytest.mark.unit
class TestWhatIsOwed:
    def _anchor(self, held, *, anchor=0.0, old_anchor=0.0, owed=None,
                carried=None, date=OLD_DAY, row=None, pending=None):
        """The closing day: Links drew 1.7 and showed 0.5, Rechts drew 0.6
        and showed none. ``held``: how far its row stood above them."""
        values, calc = _night()
        calc._ev_counter_pending = dict(pending or {})
        calc._ev_counter_drawn_total = {LINKS: 101.7, RECHTS: 50.6}
        bl = {"date": date, "anchor": old_anchor,
              "base": {LINKS: values[LINKS] - 0.5, RECHTS: values[RECHTS]},
              "last": {LINKS: values[LINKS], RECHTS: values[RECHTS]},
              "drawn_at": {LINKS: 100.0, RECHTS: 50.0},
              "owed": dict(owed or {}), "carried": dict(carried or {}),
              "row": old_anchor + 0.5 + held if row is None else row}
        calc._ev_counter_target(bl, NEW_DAY, [LINKS, RECHTS], anchor, False)
        return bl

    def test_each_counter_owes_what_it_drew_above_what_it_showed(self):
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

    def test_what_the_closing_day_owed_rolls_once(self):
        bl = self._anchor(0.0, owed={LINKS: 1.0})
        assert bl["owed"] == pytest.approx({LINKS: 1.0})
        assert bl["carried"] == pytest.approx({LINKS: 1.0})

    def test_what_rolled_once_goes(self):
        bl = self._anchor(5.0, owed={LINKS: 1.0}, carried={LINKS: 1.0})
        assert bl["owed"] == pytest.approx({LINKS: 1.2, RECHTS: 0.6})
        assert bl["carried"] == {}

    @pytest.mark.parametrize("row", ["x", None, float("nan"), True, -1.0])
    def test_no_or_a_damaged_row_owes_pending_as_before(self, row):
        """A store from before #1160, or a damaged one: only ``pending``."""
        values, calc = _night()
        calc._ev_counter_pending = {LINKS: 0.4}
        bl = {"date": OLD_DAY, "anchor": 0.0, "base": {}, "last": {},
              "drawn_at": {}}
        if row is not None:
            bl["row"] = row
        calc._ev_counter_target(bl, NEW_DAY, [LINKS, RECHTS], 0.0, False)
        assert bl["owed"] == pytest.approx({LINKS: 0.4})

    def test_a_closing_day_that_is_not_the_day_before_owes_pending(self):
        """SEM was down a whole day: the stored row is not the one that just
        closed."""
        bl = self._anchor(5.0, date="2026-10-10", pending={LINKS: 0.4})
        assert bl["owed"] == pytest.approx({LINKS: 0.4})
        assert not bl["carried"]

    def test_a_damaged_old_baseline_owes_pending_as_before(self):
        values, calc = _night()
        calc._ev_counter_pending = {LINKS: 0.4}
        bl = {"date": OLD_DAY, "anchor": 0.0, "base": {LINKS: "x"},
              "last": {LINKS: 1.0}, "row": 9.0, "drawn_at": {}}
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

    def test_a_counter_reset_at_the_start_shows_what_it_showed(self):
        """A daily counter goes back to 0 at midnight: that is no move, so
        it owes what its day drew above what it showed, no more."""
        values, calc = _night()
        calc._ev_counter_drawn_total = {LINKS: 101.7}
        bl = {"date": OLD_DAY, "anchor": 0.0,
              "base": {LINKS: values[LINKS] - 0.5},
              "last": {LINKS: values[LINKS]}, "drawn_at": {LINKS: 100.0},
              "row": 5.0}
        values[LINKS] = 0.0
        calc._ev_counter_target(bl, NEW_DAY, [LINKS], 0.0, False)
        assert bl["owed"] == pytest.approx({LINKS: 1.2})

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
        assert calls(EnergyCalculator._ev_counter_target, "_ev_carry_at_start")
