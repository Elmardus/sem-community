"""#1101 — a charger counter that catches up is not today's charging.

@RienduPre, 2× Wallbox Pulsar behind the MQTT bridge, 2.2.0-beta.16, 12 Oct
2026: ``sensor.wallbox_links_charging_energy`` sat at 5170.12 kWh from 21:05,
the box idle, and rose to 5181.30 in one report at 03:00:40, as a session
began (its own session counter: 0.7 Wh). SEM booked the 11.18 kWh as the
night's charging: the car row read 24.14 kWh where the box's session said
11.68, home read 0.54 kWh for ten hours of a heat-pump house, and true
baseload went to −8.5 kWh.

SEM had watched that box the whole night: its own power sensor read 0 W
every cycle. 11.18 kWh in one 60 s cycle is ~670 kW. The rule now: a counter
step is booked only up to what its box could have drawn since the counter
last moved, in the time SEM did NOT see the box idle — while it charged,
while its power read was dark, while SEM was down. Above that, only what SEM
saw the box draw is booked from the step.
"""
from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from freezegun import freeze_time

from homeassistant.util import dt as dt_util

from custom_components.solar_energy_management.coordinator.energy_calculator import (
    EV_CATEGORY,
    EV_COUNTER_LATE_REPORT_H,
    MIDNIGHT_EV_CATEGORY,
    EnergyCalculator,
)
from custom_components.solar_energy_management.coordinator.types import PowerReadings
from custom_components.solar_energy_management.utils.time_manager import TimeManager

from .ast_contracts import calls, reads_attribute

LINKS = "sensor.wallbox_links_charging_energy"
RECHTS = "sensor.wallbox_rechts_charging_energy"
LINKS_W = "sensor.wallbox_links_charging_power"
RECHTS_W = "sensor.wallbox_rechts_charging_power"
OWNERS = {LINKS: "links", RECHTS: "rechts"}
COUNTER = {"links": LINKS, "rechts": RECHTS}
POWER = {"links": LINKS_W, "rechts": RECHTS_W}

NIGHT = "2026-10-12 00:00:30"
TODAY = "2026-10-12"
CATCH_UP = 11.18
# 10.8 kW read and on the meter: 0.18 kWh per 60 s cycle.
CHARGE_W = 10_800.0
CHARGE_KWH = 0.18


def _hass(values: dict) -> Mock:
    hass = Mock()
    hass.data = {}
    hass.config = Mock()
    hass.config.config_dir = "/config"

    def _get(entity_id):
        if entity_id not in values:
            return None
        state = Mock()
        state.state = str(values[entity_id])
        state.attributes = {"unit_of_measurement": "kWh"}
        return state

    hass.states.get = _get
    return hass


def _calc(values: dict, *, power_sensors: bool = True,
          interval: int = 60) -> EnergyCalculator:
    hass = _hass(values)
    chargers = [{"id": "links"}, {"id": "rechts"}]
    if power_sensors:
        for charger in chargers:
            charger["ev_charging_power_sensor"] = POWER[charger["id"]]
    calc = EnergyCalculator(
        {
            "update_interval": interval,
            "ev_target_time": "11:00",
            "ev_chargers": chargers,
            "ev_max_current": 32, "ev_phases": 3, "ev_voltage": 230,
        },
        TimeManager(hass),
    )
    calc.configure_ev_counters(
        hass, [LINKS, RECHTS], True, complete=True, owners=OWNERS)
    calc.charger_rows = {"links": 0.0, "rechts": 0.0}
    return calc


def _cycle(calc: EnergyCalculator, clock, values: dict,
           links_w: float = 0.0, rechts_w: float = 0.0, *,
           tick: timedelta = timedelta(seconds=60)):
    """One 60 s cycle, as the coordinator runs it: the energy calculator,
    then each charger's own row held to its counter. Each box's power sensor
    reads what its box draws, unless a test made it dark. ``_last_update`` is
    cleared so the configured interval is used."""
    clock.tick(tick)
    for cid, watts in (("links", links_w), ("rechts", rechts_w)):
        if values.get(POWER[cid]) != "unavailable":
            values[POWER[cid]] = watts
    calc._last_update = None
    readings = PowerReadings(ev_power=links_w + rechts_w)
    readings.ev_power_per_charger = {"links": links_w, "rechts": rechts_w}
    readings.calculate_derived()
    calc.calculate_energy(readings)
    day = str(calc._ev_reset_day(None))
    for cid, watts in (("links", links_w), ("rechts", rechts_w)):
        calc.charger_rows[cid] = calc.follow_charger_counter(
            cid, day, calc.charger_rows[cid] + watts / 60_000.0, COUNTER[cid])


def _idle(calc, clock, values, cycles: int):
    for _ in range(cycles):
        _cycle(calc, clock, values)


def _idle_s(calc, clock, values, cycles: int, seconds: int):
    for _ in range(cycles):
        _cycle(calc, clock, values, tick=timedelta(seconds=seconds))


def _charge(calc, clock, values, cycles: int, meter: float = CHARGE_KWH,
            watts: float = CHARGE_W):
    for _ in range(cycles):
        values[LINKS] += meter
        _cycle(calc, clock, values, watts)


def _rows(calc, day: str = TODAY) -> tuple:
    ev_day = calc._ev_reset_day(None)
    return (
        round(calc._daily_accumulators.get(f"{EV_CATEGORY}_{ev_day}", 0.0), 2),
        round(calc._daily_accumulators.get(
            f"{MIDNIGHT_EV_CATEGORY}_{day}", 0.0), 2),
    )


def _night(values=None, **kw):
    """A calculator that has watched both counters before tonight. A watch
    starts at a counter's first move SEM sees (on the reporter's box, the
    evening session's last report); a first sight alone starts none. So:
    one read, then each counter moves 2 Wh — too little to show in a row."""
    values = values or {LINKS: 5170.12, RECHTS: 1094.83}
    calc = _calc(values, **kw)
    for bump in (0.0, 0.002):
        for counter in (LINKS, RECHTS):
            values[counter] += bump
        calc._last_update = None
        readings = PowerReadings(ev_power=0.0)
        readings.ev_power_per_charger = {"links": 0.0, "rechts": 0.0}
        readings.calculate_derived()
        calc.calculate_energy(readings)
    assert set(calc._ev_counter_room) == {LINKS, RECHTS}
    return values, calc


def _store(calc):
    from custom_components.solar_energy_management.coordinator import storage
    store = SimpleNamespace(_daily_data={}, _energy_data={})
    storage.SEMStorage.import_energy_calculator_state(store, calc.get_state())
    return storage.SEMStorage.export_energy_calculator_state(store)


@pytest.mark.unit
class TestTheReportersNight:
    def test_the_catch_up_is_not_booked(self):
        """THE issue. Three hours watched idle, then 11.18 kWh in one report
        while the box still reads 0 W: every EV row books none of it."""
        with freeze_time(NIGHT) as clock:
            values, calc = _night()
            _idle(calc, clock, values, 180)
            values[LINKS] += CATCH_UP
            _cycle(calc, clock, values)
            assert _rows(calc) == (0.0, 0.0)
            assert calc.charger_rows["links"] == pytest.approx(0.0, abs=0.01)

    def test_without_the_guard_it_is_booked(self, monkeypatch):
        """Not vacuous: the same night with the step judge switched off
        books the catch-up, as 2.2.0-beta.16 did."""
        monkeypatch.setattr(EnergyCalculator, "_judge_ev_step",
                            lambda *a, **k: 0.0)
        with freeze_time(NIGHT) as clock:
            values, calc = _night()
            _idle(calc, clock, values, 180)
            values[LINKS] += CATCH_UP
            _cycle(calc, clock, values)
            assert _rows(calc) == (CATCH_UP, CATCH_UP)

    def test_a_row_takes_the_step_out_once(self):
        """The judged step is this cycle's. A charger row that reads again
        before the next judgement (an energy cycle skipped after a stall)
        must not take it out of the next real kWh."""
        with freeze_time(NIGHT) as clock:
            values, calc = _night()
            _idle(calc, clock, values, 30)
            values[LINKS] += CATCH_UP
            _cycle(calc, clock, values)
            assert calc._ev_counter_foreign, "vacuous: the step was not judged"
            values[LINKS] += 0.2
            day = str(calc._ev_reset_day(None))
            row = calc.follow_charger_counter(
                "links", day, calc.charger_rows["links"], LINKS)
            # Not judged yet: the rise waits for the next cycle.
            assert row == pytest.approx(0.0, abs=0.01)
            calc.charger_rows["links"] = row
            _cycle(calc, clock, values)
            assert calc.charger_rows["links"] == pytest.approx(0.2, abs=0.01)
            assert _rows(calc) == pytest.approx((0.2, 0.2), abs=0.01)

    def test_the_session_after_it_is_booked_in_full(self):
        """The session that follows moves the counter in small steps, and
        every one of them is the car's: 100 minutes at 10.8 kW."""
        with freeze_time(NIGHT) as clock:
            values, calc = _night()
            _idle(calc, clock, values, 180)
            values[LINKS] += CATCH_UP
            _cycle(calc, clock, values)
            _charge(calc, clock, values, 100)
            _idle(calc, clock, values, 2)
            assert _rows(calc) == pytest.approx((18.0, 18.0), abs=0.05)
            assert calc.charger_rows["links"] == pytest.approx(18.0, abs=0.05)
            assert calc._monthly_accumulators[f"{EV_CATEGORY}_2026_10"] == (
                pytest.approx(18.0, abs=0.05))
            assert calc._lifetime_accumulators[f"lifetime_{EV_CATEGORY}"] == (
                pytest.approx(18.0, abs=0.05))

    def test_the_restart_the_evening_before_does_not_hide_it(self):
        """The reporter restarted at 22:00. Two minutes down is two minutes
        a box could have charged unseen — not 11 kWh."""
        with freeze_time("2026-10-11 21:50:00") as clock:
            values, calc = _night()
            _idle(calc, clock, values, 10)
            stored = _store(calc)
            clock.tick(timedelta(minutes=2))
            fresh = _calc(values)
            fresh.restore_state(stored)
            fresh.charger_rows = dict(calc.charger_rows)
            _idle(fresh, clock, values, 20)
            values[LINKS] += CATCH_UP
            _cycle(fresh, clock, values)
            assert _rows(fresh, "2026-10-11")[1] == 0.0

    def test_kwh_owed_from_before_midnight_are_paid_not_doubled(self):
        """The box drew before midnight and its counter had not reported
        (#1049 ``owed``). The catch-up arrives with that report inside it:
        the closed day's draw is not booked on the new day, and neither is
        the rest."""
        with freeze_time("2026-10-11 23:48:00") as clock:
            values, calc = _night()
            _cycle(calc, clock, values)
            _charge(calc, clock, values, 10, meter=0.0)  # 1.8 kWh, unreported
            _idle(calc, clock, values, 180)
            values[LINKS] += 1.8 + CATCH_UP
            _cycle(calc, clock, values)
            assert _rows(calc)[1] == 0.0


@pytest.mark.unit
class TestTheReviewFindings:
    def test_ten_second_cycles_too(self):
        """SEM's default cycle is 10 s: one cycle's reach is smaller, the
        catch-up is still not booked."""
        with freeze_time(NIGHT) as clock:
            values, calc = _night(interval=10)
            _idle_s(calc, clock, values, 600, 10)
            values[LINKS] += CATCH_UP
            _cycle(calc, clock, values, tick=timedelta(seconds=10))
            assert _rows(calc) == (0.0, 0.0)

    def test_a_skipped_energy_cycle_does_not_let_a_row_take_it(self):
        """The step lands in a cycle ``calculate_energy`` skips (a stall
        over 120 s; the first cycle after most restarts). The charger row
        still reads the counter in that cycle. It waits for the judge."""
        with freeze_time(NIGHT) as clock:
            values, calc = _night()
            _idle(calc, clock, values, 30)
            values[LINKS] += CATCH_UP
            calc._last_update = dt_util.now()
            clock.tick(timedelta(minutes=3))
            readings = PowerReadings(ev_power=0.0)
            readings.ev_power_per_charger = {"links": 0.0, "rechts": 0.0}
            readings.calculate_derived()
            calc.calculate_energy(readings)  # skipped: the gap is 180 s
            assert calc._ev_counter_seen[LINKS] == pytest.approx(5170.122), (
                "vacuous: the energy cycle ran")
            day = str(calc._ev_reset_day(None))
            calc.charger_rows["links"] = calc.follow_charger_counter(
                "links", day, calc.charger_rows["links"], LINKS)
            assert calc.charger_rows["links"] == pytest.approx(0.0, abs=0.01)
            _idle(calc, clock, values, 2)
            assert _rows(calc) == (0.0, 0.0)
            assert calc.charger_rows["links"] == pytest.approx(0.0, abs=0.01)

    def test_a_late_report_in_two_steps_after_the_stop(self):
        """The box drew 3.06 kWh with the counter silent, then stopped.
        The counter reports it in two steps. Both are the car's."""
        with freeze_time(NIGHT) as clock:
            values, calc = _night()
            _charge(calc, clock, values, 17, meter=0.0)
            _idle(calc, clock, values, 5)
            values[LINKS] += 1.5
            _cycle(calc, clock, values)
            _idle(calc, clock, values, 5)
            values[LINKS] += 1.5
            _cycle(calc, clock, values)
            _idle(calc, clock, values, 2)
            assert _rows(calc) == pytest.approx((3.0, 3.0), abs=0.01)
            assert calc.charger_rows["links"] == pytest.approx(3.0, abs=0.01)

    def test_a_late_step_a_little_above_the_power_read(self):
        """The power read 1.0 kWh, the meter later says 1.5: a read can be
        low. The second late step (1.0) is more than the 0.5 seen and not
        shown, but within it plus one cycle's reach: booked."""
        with freeze_time(NIGHT) as clock:
            values, calc = _night()
            _charge(calc, clock, values, 10, meter=0.0, watts=6_000.0)
            _idle(calc, clock, values, 5)
            values[LINKS] += 0.5
            _cycle(calc, clock, values)
            _idle(calc, clock, values, 5)
            values[LINKS] += 1.0
            _cycle(calc, clock, values)
            _idle(calc, clock, values, 2)
            assert _rows(calc) == pytest.approx((1.5, 1.5), abs=0.01)

    def test_a_counter_ten_minutes_late_every_five(self):
        """A cloud counter that publishes every 5 min what it read 10 min
        before: an hour at 11 kW is 11 kWh, all of it booked."""
        with freeze_time(NIGHT) as clock:
            values, calc = _night()
            base = values[LINKS]
            drawn = []
            for minute in range(90):
                charging = minute < 60
                drawn.append(0.0 if not drawn else drawn[-1])
                if charging:
                    drawn[-1] += 11.0 / 60
                if minute % 5 == 0 and minute >= 10:
                    values[LINKS] = base + drawn[minute - 10]
                _cycle(calc, clock, values, 11_000.0 if charging else 0.0)
            assert values[LINKS] == pytest.approx(base + 11.0), "vacuous"
            assert _rows(calc) == pytest.approx((11.0, 11.0), abs=0.05)

    def test_what_a_high_power_read_leaves_does_not_wait_for_ever(self):
        """The power read 18 kW, the meter 10.8 (#1049's bridge): 2.4 kWh
        seen but never shown. Two hours seen idle clear it; a catch-up
        after that is not booked."""
        with freeze_time(NIGHT) as clock:
            values, calc = _night()
            _charge(calc, clock, values, 20, watts=18_000.0)
            assert calc._ev_counter_unreported[LINKS] == pytest.approx(2.4, abs=0.05)
            _idle(calc, clock, values, int(EV_COUNTER_LATE_REPORT_H * 60) + 5)
            assert LINKS not in calc._ev_counter_unreported
            before = _rows(calc)
            values[LINKS] += 2.0
            _cycle(calc, clock, values)
            assert _rows(calc) == before

    def test_a_dark_read_now_and_then_pauses_the_wait(self):
        """An MQTT bridge that drops for a cycle every 90 min: the idle count
        pauses, it does not start over, so the rest is still dropped."""
        with freeze_time(NIGHT) as clock:
            values, calc = _night()
            _charge(calc, clock, values, 20, watts=18_000.0)
            for minute in range(int(EV_COUNTER_LATE_REPORT_H * 60) + 30):
                values[LINKS_W] = "unavailable" if minute % 90 == 45 else 0.0
                _cycle(calc, clock, values)
            assert LINKS not in calc._ev_counter_unreported
            before = _rows(calc)
            # Each dark minute is a minute unseen: room for 0.73 kWh. The
            # catch-up is far above it.
            values[LINKS] += CATCH_UP
            _cycle(calc, clock, values)
            assert _rows(calc) == before

    def test_counters_switched_off_are_not_held(self):
        with freeze_time(NIGHT) as clock:
            values, calc = _night()
            _idle(calc, clock, values, 2)
            seen = calc._ev_counter_seen[LINKS]
            calc._ev_counter_enabled = False
            assert calc.ev_counter_judged_kwh(LINKS, seen + 5.0) == seen + 5.0

    def test_within_the_wait_it_is_booked(self):
        """The same rest, reported an hour after the stop: booked."""
        with freeze_time(NIGHT) as clock:
            values, calc = _night()
            _charge(calc, clock, values, 20, watts=18_000.0)
            _idle(calc, clock, values, 60)
            before = _rows(calc)
            values[LINKS] += 2.0
            _cycle(calc, clock, values)
            assert _rows(calc)[1] == pytest.approx(before[1] + 2.0, abs=0.01)

    def test_unreported_round_trips(self):
        with freeze_time(NIGHT) as clock:
            values, calc = _night()
            _charge(calc, clock, values, 5, meter=0.0)
            fresh = _calc(values)
            fresh.restore_state(_store(calc))
            assert fresh._ev_counter_unreported == pytest.approx(
                calc._ev_counter_unreported)
            assert fresh._ev_counter_unreported[LINKS] == pytest.approx(0.9)
            _idle(calc, clock, values, 3)
            fresh = _calc(values)
            fresh.restore_state(_store(calc))
            assert fresh._ev_counter_idle_h[LINKS] == pytest.approx(3 / 60)

    def test_a_store_from_before_1049_books_the_first_move(self):
        """No ``seen`` at all: the first read after a restart may still be
        stale. The move after it is booked as it stands."""
        with freeze_time(NIGHT) as clock:
            values, calc = _night()
            stored = _store(calc)
            del stored["ev_counter_pending"]
            clock.tick(timedelta(hours=2))
            fresh = _calc(values)
            fresh.restore_state(stored)
            fresh.charger_rows = dict(calc.charger_rows)
            _idle(fresh, clock, values, 1)
            values[LINKS] += 7.0
            _cycle(fresh, clock, values)
            assert _rows(fresh)[1] == pytest.approx(7.0, abs=0.01)


@pytest.mark.unit
class TestRealLateCountersAreStillBooked:
    """Every honest reason for a counter to step: the box charged, SEM could
    not see it, or SEM was not running."""

    def test_a_cloud_counter_that_reports_late_while_charging(self):
        with freeze_time(NIGHT) as clock:
            values, calc = _night()
            _cycle(calc, clock, values)
            _charge(calc, clock, values, 30, meter=0.0, watts=6_000.0)  # 3.0 read
            values[LINKS] += 5.5
            _cycle(calc, clock, values, 6_000.0)
            assert _rows(calc)[0] == pytest.approx(5.5, abs=0.15)

    def test_a_report_that_lands_after_the_box_stopped(self):
        with freeze_time(NIGHT) as clock:
            values, calc = _night()
            _cycle(calc, clock, values)
            _charge(calc, clock, values, 30, meter=0.0, watts=6_000.0)
            _idle(calc, clock, values, 20)
            values[LINKS] += 5.5
            _cycle(calc, clock, values)
            assert _rows(calc) == pytest.approx((5.5, 5.5), abs=0.01)

    def test_a_dark_power_read_cannot_say_the_box_was_idle(self):
        """A dark sensor reads 0 W. An hour of it is an hour SEM did not see."""
        with freeze_time(NIGHT) as clock:
            values, calc = _night()
            _cycle(calc, clock, values)
            values[LINKS_W] = "unavailable"
            _idle(calc, clock, values, 60)
            values[LINKS] += 5.0
            _cycle(calc, clock, values)
            assert _rows(calc) == pytest.approx((5.0, 5.0), abs=0.01)

    def test_a_box_without_its_own_power_sensor_is_not_watched(self):
        with freeze_time(NIGHT) as clock:
            values, calc = _night(power_sensors=False)
            _idle(calc, clock, values, 60)
            values[LINKS] += 5.0
            _cycle(calc, clock, values)
            assert _rows(calc) == pytest.approx((5.0, 5.0), abs=0.01)

    def test_charging_while_sem_was_down(self):
        """#658's case: a restart two hours long."""
        with freeze_time(NIGHT) as clock:
            values, calc = _night()
            _idle(calc, clock, values, 10)
            stored = _store(calc)
            clock.tick(timedelta(hours=2))
            values[LINKS] += 7.0
            fresh = _calc(values)
            fresh.restore_state(stored)
            fresh.charger_rows = dict(calc.charger_rows)
            _cycle(fresh, clock, values)
            assert _rows(fresh) == pytest.approx((7.0, 7.0), abs=0.01)
            assert fresh.charger_rows["links"] == pytest.approx(7.0, abs=0.01)

    def test_a_skipped_cycle_counts_as_unseen(self):
        """``calculate_energy`` skips a cycle after a long stall; the time
        is still time SEM did not see."""
        with freeze_time(NIGHT) as clock:
            values, calc = _night()
            _idle(calc, clock, values, 10)
            values[LINKS] += 7.0
            _cycle(calc, clock, values, tick=timedelta(hours=1))
            assert _rows(calc) == pytest.approx((7.0, 7.0), abs=0.01)

    def test_a_first_step_before_the_power_read_rises(self):
        """A session start: the counter moves a cycle before the smoothed
        power read does. One cycle's draw is within one box's reach."""
        with freeze_time(NIGHT) as clock:
            values, calc = _night()
            _idle(calc, clock, values, 30)
            values[LINKS] += 0.3
            _cycle(calc, clock, values)
            assert _rows(calc) == pytest.approx((0.3, 0.3), abs=0.01)

    def test_a_trickle_under_the_floor_still_leaves_room(self):
        """300 W read for six hours may be one phase of three: up to 5.4 kWh."""
        with freeze_time(NIGHT) as clock:
            values, calc = _night()
            _cycle(calc, clock, values)
            for _ in range(360):
                _cycle(calc, clock, values, 300.0)
            values[LINKS] += 1.8
            _cycle(calc, clock, values, 300.0)
            assert _rows(calc)[1] == pytest.approx(1.8, abs=0.15)

    def test_after_an_upgrade_the_first_move_is_booked(self):
        """A store from before #1101 has no record of the time since: the
        next move is booked as it stands, and the watch starts there."""
        with freeze_time(NIGHT) as clock:
            values, calc = _night()
            _idle(calc, clock, values, 10)
            stored = _store(calc)
            del stored["ev_counter_pending"]["room"]
            del stored["ev_counter_pending"]["watched_at"]
            fresh = _calc(values)
            fresh.restore_state(stored)
            _idle(fresh, clock, values, 10)
            values[LINKS] += 5.0
            _cycle(fresh, clock, values)
            assert _rows(fresh)[1] == pytest.approx(5.0, abs=0.01)
            _idle(fresh, clock, values, 10)
            values[LINKS] += 5.0
            _cycle(fresh, clock, values)
            assert _rows(fresh)[1] == pytest.approx(5.0, abs=0.01)


EVENING = "2026-10-17 18:00:00"
EVENING_DAY = "2026-10-17"
# 18 Oct 2026, 16:44: the rise 2 s after a plug-in, on 2.2.0-beta.21.
PLUG_IN_STEP = 19.85


def _only_links(calc):
    """The reporter's set: only Links has a counter of its own in SEM, so
    the set is partial and each charger's row follows its power (#1049)."""
    calc.configure_ev_counters(calc._hass, [LINKS], True, complete=False,
                               owners={LINKS: "links"})


def _restart(calc, clock, values, down: timedelta, partial: bool = False):
    stored = _store(calc)
    clock.tick(down)
    fresh = _calc(values)
    if partial:
        _only_links(fresh)
    fresh.restore_state(stored)
    fresh.charger_rows = dict(calc.charger_rows)
    return fresh


@pytest.mark.unit
class TestUnseenTimeIsSpent:
    """#1101, reopened 19 Oct 2026 on 2.2.0-beta.21. The Links counter sat
    at 5291.49 kWh from 17 Oct, the box idle and read 0 W, then rose
    19.85 kWh in one report 2 s after a plug-in on 18 Oct. No car charged;
    the fleet row booked all of it and logged nothing.

    ``room`` held every restart and dark read since the counter last moved,
    at 44 kW, and never let go while the counter stayed flat: 27 minutes
    unseen in a day is 19.85 kWh. ``unreported`` already had a wait — a
    counter that has not shown a draw after two hours seen idle never will.
    The same wait now spends the unseen part of ``room``; unseen time starts
    it again. What SEM saw the box draw stays."""

    @pytest.mark.parametrize("partial", [False, True])
    def test_the_reporters_day(self, partial):
        """Thirty minutes down at the evening restart, hours seen idle,
        then the plug-in step: no row books it."""
        with freeze_time(EVENING) as clock:
            values, calc = _night()
            if partial:
                _only_links(calc)
            _idle(calc, clock, values, 10)
            calc = _restart(calc, clock, values, timedelta(minutes=30), partial)
            _idle(calc, clock, values, 180)
            assert calc._ev_counter_room[LINKS] < 0.01
            values[LINKS] += PLUG_IN_STEP
            _cycle(calc, clock, values)
            assert _rows(calc, EVENING_DAY) == (0.0, 0.0)
            assert calc.charger_rows["links"] == pytest.approx(0.0, abs=0.01)

    def test_within_the_wait_the_unseen_time_still_counts(self):
        """Not vacuous, and #658's case: an hour after the restart a counter
        may still report what the box drew while SEM was down. The wait that
        ran before the restart does not count: it starts again."""
        with freeze_time(EVENING) as clock:
            values, calc = _night()
            _idle(calc, clock, values, 100)
            calc = _restart(calc, clock, values, timedelta(minutes=30))
            _idle(calc, clock, values, 60)
            values[LINKS] += PLUG_IN_STEP
            _cycle(calc, clock, values)
            assert _rows(calc, EVENING_DAY) == pytest.approx(
                (PLUG_IN_STEP, PLUG_IN_STEP), abs=0.01)

    def test_a_dark_read_starts_the_wait_again(self):
        """Thirty minutes of a dark power read late in a wait: that is new
        unseen time, with a wait of its own."""
        with freeze_time(EVENING) as clock:
            values, calc = _night()
            _idle(calc, clock, values, 100)
            values[LINKS_W] = "unavailable"
            _idle(calc, clock, values, 30)
            values[LINKS_W] = 0.0
            _idle(calc, clock, values, 60)
            values[LINKS] += PLUG_IN_STEP
            _cycle(calc, clock, values)
            assert _rows(calc, EVENING_DAY) == pytest.approx(
                (PLUG_IN_STEP, PLUG_IN_STEP), abs=0.01)

    def test_after_a_dark_read_the_wait_spends_it_too(self):
        with freeze_time(EVENING) as clock:
            values, calc = _night()
            values[LINKS_W] = "unavailable"
            _idle(calc, clock, values, 30)
            values[LINKS_W] = 0.0
            _idle(calc, clock, values, 150)
            values[LINKS] += PLUG_IN_STEP
            _cycle(calc, clock, values)
            assert _rows(calc, EVENING_DAY) == (0.0, 0.0)

    def test_a_trickle_read_since_stays(self):
        """300 W read for three hours after the restart may be one phase of
        three: up to 2.7 kWh the counter can still show."""
        with freeze_time(EVENING) as clock:
            values, calc = _night()
            _idle(calc, clock, values, 10)
            calc = _restart(calc, clock, values, timedelta(minutes=30))
            for _ in range(180):
                _cycle(calc, clock, values, 300.0)
            assert 2.6 < calc._ev_counter_room[LINKS] < 2.8
            values[LINKS] += 2.5
            _cycle(calc, clock, values, 300.0)
            assert _rows(calc, EVENING_DAY)[1] == pytest.approx(2.5, abs=0.05)

    def test_with_no_record_the_watch_starts_after_the_wait(self):
        """A store from before #1101 has no record of the time since. Seen
        idle for the wait, the counter's next step is judged like any."""
        with freeze_time(EVENING) as clock:
            values, calc = _night()
            _idle(calc, clock, values, 10)
            stored = _store(calc)
            for key in ("room", "watched_at", "unseen_kwh", "quiet_h"):
                del stored["ev_counter_pending"][key]
            fresh = _calc(values)
            fresh.restore_state(stored)
            _idle(fresh, clock, values, 150)
            values[LINKS] += 5.0
            _cycle(fresh, clock, values)
            assert _rows(fresh, EVENING_DAY) == (0.0, 0.0)

    def test_a_session_seen_and_reported_late_is_booked(self):
        """A cloud counter flat through an hour SEM watched at 11 kW, and
        reporting it after more than the wait: SEM saw that charge, so the
        wait does not spend it."""
        with freeze_time(EVENING) as clock:
            values, calc = _night()
            _charge(calc, clock, values, 60, meter=0.0, watts=11_000.0)
            _idle(calc, clock, values, 125)
            values[LINKS] += 11.0
            _cycle(calc, clock, values)
            assert _rows(calc, EVENING_DAY) == pytest.approx((11.0, 11.0), abs=0.01)
            assert calc.charger_rows["links"] == pytest.approx(11.0, abs=0.01)

    def test_watched_charging_counts_toward_the_wait(self):
        """Dark for 30 minutes, then ten minutes SEM saw at 11 kW, then idle:
        two hours after the dark read its part is spent; the seen charge is
        all the step may book."""
        with freeze_time(EVENING) as clock:
            values, calc = _night()
            values[LINKS_W] = "unavailable"
            _idle(calc, clock, values, 30)
            values[LINKS_W] = 0.0
            _charge(calc, clock, values, 10, meter=0.0, watts=11_000.0)
            _idle(calc, clock, values, 115)
            values[LINKS] += PLUG_IN_STEP
            _cycle(calc, clock, values)
            assert _rows(calc, EVENING_DAY) == pytest.approx((1.83, 1.83), abs=0.05)

    def test_a_move_clears_the_unseen_part(self):
        """The counter moves after a dark read: that move holds the dark
        time, so the wait has nothing left to spend from it. A charge SEM
        sees next keeps its full reach."""
        with freeze_time(EVENING) as clock:
            values, calc = _night()
            values[LINKS_W] = "unavailable"
            _idle(calc, clock, values, 30)
            values[LINKS_W] = 0.0
            values[LINKS] += 0.2
            _cycle(calc, clock, values)
            _charge(calc, clock, values, 30, meter=0.0, watts=11_000.0)
            _idle(calc, clock, values, 95)
            values[LINKS] += 15.5
            _cycle(calc, clock, values)
            assert _rows(calc, EVENING_DAY) == pytest.approx((15.7, 15.7), abs=0.05)

    def test_no_record_starts_the_wait_again(self):
        """A wait nearly done before a store whose watch time is damaged:
        the time since is unknown, so the wait starts again and the next
        move is booked as it stands."""
        with freeze_time(EVENING) as clock:
            values, calc = _night()
            _idle(calc, clock, values, 110)
            stored = _store(calc)
            stored["ev_counter_pending"]["watched_at"] = "junk"
            fresh = _calc(values)
            fresh.restore_state(stored)
            _idle(fresh, clock, values, 15)
            values[LINKS] += 5.0
            _cycle(fresh, clock, values)
            assert _rows(fresh, EVENING_DAY) == pytest.approx((5.0, 5.0), abs=0.01)


@pytest.mark.unit
class TestTheWatchSurvivesTheStore:
    def test_the_wait_round_trips(self):
        """A restart inside one cycle is no unseen time: the wait and the
        unseen part it spends go on across it."""
        with freeze_time(EVENING) as clock:
            values, calc = _night()
            values[LINKS_W] = "unavailable"
            _idle(calc, clock, values, 30)
            values[LINKS_W] = 0.0
            _idle(calc, clock, values, 100)
            stored = _store(calc)
            assert stored["ev_counter_pending"]["unseen_kwh"][LINKS] > 20.0
            fresh = _calc(values)
            fresh.restore_state(stored)
            fresh.charger_rows = dict(calc.charger_rows)
            _idle(fresh, clock, values, 25)
            values[LINKS] += PLUG_IN_STEP
            _cycle(fresh, clock, values)
            assert _rows(fresh, EVENING_DAY) == (0.0, 0.0)

    def test_a_store_from_before_the_wait_spends_all_of_room(self):
        """It does not tell what ``room`` holds, so all of it waits."""
        calc = _calc({})
        calc.restore_state({"ev_counter_pending": {"room": {LINKS: 22.0}}})
        assert calc._ev_counter_unseen_kwh == {LINKS: 22.0}

    def test_a_damaged_wait_is_dropped(self):
        calc = _calc({})
        calc.restore_state({"ev_counter_pending": {
            "quiet_h": {LINKS: "x", RECHTS: 1.5, "b": True},
            "unseen_kwh": {LINKS: float("nan"), RECHTS: 0.2, "c": -3.0}}})
        assert calc._ev_counter_quiet_h == {RECHTS: 1.5}
        assert calc._ev_counter_unseen_kwh == {RECHTS: 0.2}

    def test_room_and_watch_round_trip(self):
        with freeze_time(NIGHT) as clock:
            values, calc = _night()
            _cycle(calc, clock, values)
            _charge(calc, clock, values, 5, meter=0.0)
            stored = _store(calc)
            fresh = _calc(values)
            fresh.restore_state(stored)
            assert fresh._ev_counter_room == pytest.approx(calc._ev_counter_room)
            assert fresh._ev_counter_room[LINKS] > 3.0
            assert fresh._ev_watched_at == calc._ev_watched_at
            assert isinstance(stored["ev_counter_pending"]["watched_at"], str)

    @pytest.mark.parametrize("bad", ["junk", "2026-10-12T00:00:00", 5, None])
    def test_a_damaged_watch_time_is_no_record(self, bad):
        calc = _calc({})
        calc.restore_state({"ev_counter_pending": {
            "room": {LINKS: "x", RECHTS: 1.5, "b": True}, "watched_at": bad}})
        assert calc._ev_counter_room == {RECHTS: 1.5}
        assert calc._ev_watched_at is None


@pytest.mark.unit
class TestTheWiring:
    def test_every_ev_row_reads_the_judged_steps(self):
        """The fleet row, the calendar copy and each charger's row all take
        their counter deltas from ``_ev_counter_target``."""
        assert reads_attribute(EnergyCalculator._ev_counter_target,
                               "self", "_ev_counter_foreign")
        assert calls(EnergyCalculator._reconcile_ev_bucket, "_ev_counter_target")
        assert calls(EnergyCalculator.follow_charger_counter, "_ev_counter_target")

    def test_steps_are_judged_where_the_watch_is_kept(self):
        assert calls(EnergyCalculator._track_ev_pending, "_judge_ev_step")


@pytest.mark.unit
class TestTheSessionTotalLeavesItOutToo:
    """A session with no session counter of its own takes its total from the
    lifetime counter's rise (#1024). The same step is not the session's."""

    KW = dict(solar_kwh=0.0, grid_kwh=0.1, battery_kwh=0.0, cost=0.0,
              power_w=6_000.0, hours=1 / 60, session_meter_kwh=None,
              import_rate=0.3)

    def _session(self, skip):
        from custom_components.solar_energy_management.coordinator.session_energy import step
        meter: dict = {}
        step(meter, lifetime_meter_kwh=5170.12, **self.KW)
        step(meter, lifetime_meter_kwh=5170.22, **self.KW)
        res = step(meter, lifetime_meter_kwh=5170.22 + CATCH_UP,
                   lifetime_skip=skip, **self.KW)
        return meter, res

    def test_the_step_is_left_out(self):
        _, res = self._session((5170.22, CATCH_UP))
        assert res.energy_kwh == pytest.approx(0.2, abs=0.01)

    def test_without_it_the_step_is_counted(self):
        _, res = self._session(None)
        assert res.energy_kwh == pytest.approx(0.2 + CATCH_UP, abs=0.01)

    def test_it_is_left_out_once(self):
        from custom_components.solar_energy_management.coordinator.session_energy import step
        meter, _ = self._session((5170.22, CATCH_UP))
        res = step(meter, lifetime_meter_kwh=5170.32 + CATCH_UP,
                   lifetime_skip=(5170.22, CATCH_UP), **self.KW)
        assert res.energy_kwh == pytest.approx(0.3, abs=0.01)

    def test_a_rise_not_judged_yet_is_held(self):
        with freeze_time(NIGHT) as clock:
            values, calc = _night()
            _idle(calc, clock, values, 2)
            seen = calc._ev_counter_seen[LINKS]
            assert calc.ev_counter_judged_kwh(LINKS, seen + 11.0) == seen
            assert calc.ev_counter_judged_kwh(LINKS, seen - 1.0) == seen - 1.0
            assert calc.ev_counter_judged_kwh("sensor.other", 9.0) == 9.0
            assert calc.ev_counter_judged_kwh(LINKS, None) is None

    def test_the_calculator_hands_over_the_judged_step(self):
        with freeze_time(NIGHT) as clock:
            values, calc = _night()
            _idle(calc, clock, values, 30)
            values[LINKS] += CATCH_UP
            _cycle(calc, clock, values)
            before, kwh = calc.ev_counter_step_not_drawn(LINKS)
            assert before == pytest.approx(5170.12)
            assert kwh == pytest.approx(CATCH_UP, abs=0.01)
            assert calc.ev_counter_step_not_drawn(RECHTS) is None
            assert calc.ev_counter_step_not_drawn(None) is None

    def test_the_session_reads_it(self):
        from custom_components.solar_energy_management.coordinator.ev_control import (
            EVControlMixin,
        )
        import ast
        import inspect
        import textwrap

        from .ast_contracts import call_kwargs
        tree = ast.parse(textwrap.dedent(
            inspect.getsource(EVControlMixin._update_session_tracking)))
        for name in ("ev_counter_step_not_drawn", "ev_counter_judged_kwh"):
            assert any(
                isinstance(n, ast.Call) and getattr(n.func, "id", None) == "getattr"
                and any(isinstance(a, ast.Constant) and a.value == name
                        for a in n.args)
                for n in ast.walk(tree)), name
        sites = call_kwargs(EVControlMixin._update_session_tracking, "_session_step")
        assert sites and all("lifetime_skip" in kw for kw in sites)
