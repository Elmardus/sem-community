"""#1146 — the per-charger daily energy did not reset at midnight.

Two Wallbox Pulsars, ``ev_charger`` (Links, Charge by 11:00) and
``ev_charger_1`` (Rechts, Charge by 08:00). ``sensor.sem_daily_ev_energy``
reset at 00:00; the per-charger ``charger_<id>_daily_energy`` rows carried
15-10's 0.75 / 9.72 kWh into 16-10, kept counting on top of them through the
night sessions, and fell to 0 at 08:00 and 11:00 — each charger's Charge-by
time. From then on they read 0.0 for the rest of the day although both cars
had charged since midnight (fleet total right at 50.8 kWh).

Root cause: the rows published the per-charger EV-day bucket, which rolls
at the charger's Charge-by time on purpose (#280: the night charge must not
see its target again between hitting it and the deadline). The fix keeps
that bucket for the decisions and publishes a calendar-day view of it:
it starts at local midnight and carries today's kWh across the bucket's
deadline reset.

The replay drives the real ``_update_ev_intelligence`` per-charger loop
cycle by cycle over the reporter's night.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest
from freezegun import freeze_time
from homeassistant.util import dt as dt_util

from custom_components.solar_energy_management.coordinator.coordinator import (
    SEMCoordinator,
)
from custom_components.solar_energy_management.coordinator.departure import (
    departure_hhmm,
)
from custom_components.solar_energy_management.utils.time_manager import (
    TimeManager,
)

TZ = ZoneInfo("Europe/Amsterdam")
LINKS, RECHTS = "ev_charger", "ev_charger_1"
CONFIG = {
    "ev_chargers": [
        {"id": LINKS, "ev_target_time": "11:00"},
        {"id": RECHTS, "ev_target_time": "08:00"},
    ],
}
CYCLE = timedelta(minutes=5)


@pytest.fixture(autouse=True)
def _local_time():
    """The reporter's midnight is local midnight."""
    previous = dt_util.DEFAULT_TIME_ZONE
    dt_util.set_default_time_zone(TZ)
    yield
    dt_util.set_default_time_zone(previous)


def _at(day, hh, mm=0):
    return datetime(2026, 10, day, hh, mm, tzinfo=TZ)


def _bind(c, *names):
    for name in names:
        setattr(c, name, getattr(SEMCoordinator, name).__get__(c))


def _coord():
    """A coordinator with the real per-charger daily path and inert rest."""
    c = MagicMock()
    c.config = CONFIG
    c.update_interval = CYCLE
    c.time_manager = TimeManager(MagicMock(), CONFIG)
    c._charger_target_time = lambda cfg: departure_hhmm(cfg, CONFIG)
    c._ev_devices = {
        cid: SimpleNamespace(_current_setpoint=16, _session_active=True,
                             power_entity_id=None)
        for cid in (LINKS, RECHTS)
    }
    c._ev_taper_detectors = {LINKS: MagicMock(), RECHTS: MagicMock()}
    c._ev_device = None
    c._own_ev_counters = lambda: {}
    # No counters: the row is what the integrator holds.
    c._energy_calculator.follow_charger_counter.side_effect = (
        lambda cid, day, value, counter: value)
    c._last_commanded_amps_per_charger = {}
    c._decline_stall_count = {}
    c._ev_taper_detector.get_virtual_soc.return_value = 50.0
    c._ev_taper_detector.energy_since_full = 0.0
    c._session_data = SimpleNamespace(energy_kwh=0.0)
    c._cycle_vehicle_soc = None
    c._daily_ev_per_charger = {}
    c._daily_ev_per_charger_date = {}
    c._daily_ev_per_charger_cal = {}
    _bind(c, "_charger_power_w", "_charger_calendar_slots",
          "_charger_calendar_day_roll", "_charger_calendar_day_carry",
          "_per_charger_daily_calendar", "_per_charger_daily_report",
          "_charger_daily_kwh", "_per_charger_daily_blob",
          "_restore_per_charger_daily")
    return c


def _watts(t):
    """The reporter's night, cycle by cycle (W per charger)."""
    links = 7000.0 if _at(16, 1) <= t < _at(16, 6) else 0.0
    rechts = 0.0
    if _at(15, 23, 30) <= t < _at(16, 0, 30):     # crosses midnight
        rechts = 3600.0
    elif _at(16, 1, 30) <= t < _at(16, 5, 30):
        rechts = 3600.0
    return {LINKS: links, RECHTS: rechts}


class _Fleet:
    """The fleet ``daily_ev`` as the EnergyCalculator books it: the same
    per-cycle increments, rolled at local midnight."""

    def __init__(self):
        self.day, self.kwh = None, 0.0

    def step(self, t, watts):
        if self.day != t.date():
            self.day, self.kwh = t.date(), 0.0
        self.kwh += sum(watts.values()) * CYCLE.total_seconds() / 3600 / 1000


def _run(c, start, end, fleet=None, record=None):
    t = start
    while t < end:
        watts = _watts(t)
        with freeze_time(t):
            SEMCoordinator._update_ev_intelligence(
                c, SimpleNamespace(ev_power_per_charger=watts, ev_power=0.0,
                                   ev_connected=True, ev_charging=True),
                SimpleNamespace(daily_ev=0.0))
        if fleet is not None:
            fleet.step(t, watts)
        if record is not None:
            record[t] = (c._per_charger_daily_calendar(),
                         fleet.kwh if fleet else None)
        t += CYCLE


def _seed_15_10(c):
    """15-10 22:40: the rows the reporter saw, both buckets on 15-10's EV
    day (opened at that morning's Charge-by), calendar day 15-10."""
    c._daily_ev_per_charger = {LINKS: 0.75, RECHTS: 9.72}
    c._daily_ev_per_charger_date = {LINKS: "2026-10-15", RECHTS: "2026-10-15"}
    c._daily_ev_per_charger_cal = {
        LINKS: {"date": "2026-10-15", "offset": 0.0},
        RECHTS: {"date": "2026-10-15", "offset": 0.0},
    }


@pytest.mark.unit
class TestReportersNight:

    def _replay(self):
        c = _coord()
        _seed_15_10(c)
        fleet = _Fleet()
        fleet.day, fleet.kwh = _at(15, 22, 40).date(), 10.47
        seen = {}
        _run(c, _at(15, 22, 40), _at(16, 13), fleet, seen)
        return c, fleet, seen

    def test_before_midnight_the_rows_carry_on(self):
        _, _, seen = self._replay()
        rows, _ = seen[_at(15, 23, 55)]
        # Rechts 23:30–00:00 = 0.5 h at 3.6 kW on top of 9.72
        assert rows[RECHTS] == pytest.approx(9.72 + 1.8, abs=0.01)
        assert rows[LINKS] == pytest.approx(0.75, abs=0.01)

    def test_both_rows_reset_at_midnight(self):
        _, _, seen = self._replay()
        rows, fleet = seen[_at(16, 0, 0)]
        # The first cycle of 16-10 holds only its own 5 min of charging.
        assert rows[LINKS] == pytest.approx(0.0, abs=0.001)
        assert rows[RECHTS] == pytest.approx(0.3, abs=0.001)
        assert fleet == pytest.approx(0.3, abs=0.001)

    def test_a_session_across_midnight_is_split_at_00_00(self):
        _, _, seen = self._replay()
        before, _ = seen[_at(15, 23, 55)]
        after, _ = seen[_at(16, 0, 25)]
        assert before[RECHTS] - 9.72 == pytest.approx(1.8, abs=0.01)
        assert after[RECHTS] == pytest.approx(1.8, abs=0.01)

    def test_no_drop_at_either_charge_by_time(self):
        """08:00 (Rechts) and 11:00 (Links): the EV-day bucket rolls, the
        day's row keeps what was charged since midnight."""
        _, _, seen = self._replay()
        for t, cid in ((_at(16, 8, 0), RECHTS), (_at(16, 11, 0), LINKS)):
            before, _ = seen[t - CYCLE]
            after, _ = seen[t]
            assert after[cid] == pytest.approx(before[cid], abs=0.001), cid
            assert after[cid] > 10.0, cid

    def test_the_new_day_is_counted_and_sums_to_the_fleet(self):
        c, fleet, seen = self._replay()
        rows, total = seen[_at(16, 12, 55)]
        assert rows[LINKS] == pytest.approx(35.0, abs=0.01)
        assert rows[RECHTS] == pytest.approx(1.8 + 14.4, abs=0.01)
        assert sum(rows.values()) == pytest.approx(total, abs=0.01)
        # every cycle of 16-10, not only the last
        for t, (r, f) in seen.items():
            if t >= _at(16, 0):
                assert sum(r.values()) == pytest.approx(f, abs=0.01), t
        # what the sensors publish is this view
        assert c._per_charger_daily_report(SimpleNamespace(daily_ev=total)) \
            == pytest.approx(rows, abs=0.001)

    def test_decisions_keep_the_ev_day(self):
        """The night charge still measures against the EV-day bucket: at
        05:30 Rechts' bucket holds 15-10 evening's kWh too (#280)."""
        c = _coord()
        _seed_15_10(c)
        _run(c, _at(15, 22, 40), _at(16, 5, 30))
        assert c._charger_daily_kwh(RECHTS, SimpleNamespace(daily_ev=0.0)) \
            == pytest.approx(9.72 + 3.6 + 14.4, abs=0.01)
        assert c._per_charger_daily_calendar()[RECHTS] \
            == pytest.approx(1.8 + 14.4, abs=0.01)


@pytest.mark.unit
class TestRestart:

    def test_a_restart_mid_day_keeps_the_day(self):
        c = _coord()
        _seed_15_10(c)
        _run(c, _at(15, 22, 40), _at(16, 9))
        before = c._per_charger_daily_calendar()
        blob = c._per_charger_daily_blob()
        d = _coord()
        d._restore_per_charger_daily(blob)
        assert d._per_charger_daily_calendar() == pytest.approx(before)
        _run(d, _at(16, 9), _at(16, 13))
        assert d._per_charger_daily_calendar()[LINKS] \
            == pytest.approx(35.0, abs=0.01)

    def test_a_restart_across_midnight_starts_the_day_at_zero(self):
        """Saved 23:55 on 15-10, up again 00:20 on 16-10: yesterday's kWh
        are not today's."""
        c = _coord()
        _seed_15_10(c)
        _run(c, _at(15, 22, 40), _at(15, 23, 55))
        d = _coord()
        d._restore_per_charger_daily(c._per_charger_daily_blob())
        _run(d, _at(16, 0, 20), _at(16, 0, 25))
        assert d._per_charger_daily_calendar()[RECHTS] \
            == pytest.approx(0.3, abs=0.001)
        assert d._per_charger_daily_calendar()[LINKS] == 0.0

    def test_a_store_from_before_the_fix_keeps_its_reading(self):
        """No calendar slots in the store: the bucket is today's until the
        next midnight — the reading the row showed before the upgrade."""
        d = _coord()
        d._restore_per_charger_daily({
            "date": {LINKS: "2026-10-16", RECHTS: "2026-10-16"},
            "values": {LINKS: 4.0, RECHTS: 2.0},
        })
        _run(d, _at(16, 12), _at(16, 12, 5))
        assert d._per_charger_daily_calendar() == {LINKS: 4.0, RECHTS: 2.0}
        _run(d, _at(17, 0), _at(17, 0, 5))
        assert d._per_charger_daily_calendar() == {LINKS: 0.0, RECHTS: 0.0}


@pytest.mark.unit
def test_a_removed_charger_drops_its_calendar_slot():
    c = _coord()
    _seed_15_10(c)
    c.config = {"ev_chargers": [{"id": LINKS}]}
    c._ev_devices = {LINKS: object()}
    c._ev_taper_detectors = {}
    SEMCoordinator._sweep_unconfigured_charger_state(c)
    assert set(c._daily_ev_per_charger_cal) == {LINKS}
