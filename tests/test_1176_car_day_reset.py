"""#1176 — the car's daily energy says when it resets.

``sensor.sem_daily_ev_energy`` counts by the car day: it falls to 0 at the
Charge-by time (07:00 on PROD), not at midnight. Its ``last_reset`` said
midnight, so the recorder booked the night's kWh again at 00:00 and a drop
of 14.67 kWh at 07:00 (PROD 23.10.2026). The Simulation tab read that drop
as a counter stepping back and left 62 of 85 days out.

Pinned here: ``last_reset`` is the car day's start for the car-day sensor
and midnight for the rest; it moves once, at the rollover; a restart keeps
it. Days already recorded: the drop in the rollover hour is read as the
reset it is; a step back at any other hour is still noted.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from freezegun import freeze_time
from homeassistant.components.sensor import (
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.util import dt as dt_util

from custom_components.solar_energy_management.analytics.simulation.hourly_day import (
    FLOW_ROLES,
    HourlyDay,
)
from custom_components.solar_energy_management.coordinator import (
    day_tag_store,
    simulation_job,
)
from custom_components.solar_energy_management.analytics.simulation.hourly_day import (
    CAR_DOUBT,
)
from custom_components.solar_energy_management.coordinator.day_tag_store import (
    car_daily_entities,
    car_daily_entity_ids,
    car_day_notes,
    car_rows,
    car_spans,
)
from custom_components.solar_energy_management.coordinator.energy_calculator import (
    EnergyCalculator,
)
from custom_components.solar_energy_management.coordinator.types import PowerReadings
from custom_components.solar_energy_management.sensor import (
    CAR_DAY_SENSOR_KEYS,
    SEMSolarSensor,
)
from custom_components.solar_energy_management.utils.time_manager import TimeManager

CHARGE_BY = "07:00"


# ── the sensor's last_reset ─────────────────────────────────────────


def _calc(charge_by: str = CHARGE_BY) -> EnergyCalculator:
    hass = Mock()
    hass.data = {}
    hass.states.get = lambda _eid: None
    return EnergyCalculator(
        {"update_interval": 10, "ev_target_time": charge_by,
         "ev_chargers": [{"id": "box"}]},
        TimeManager(hass),
    )


def _cycle(calc: EnergyCalculator, ev_w: float = 0.0) -> None:
    calc._last_update = None
    readings = PowerReadings(ev_power=ev_w)
    readings.ev_power_per_charger = {"box": ev_w}
    readings.calculate_derived()
    calc.calculate_energy(readings)


def _sensor(key: str, calc, state_class=SensorStateClass.TOTAL):
    sensor = SEMSolarSensor.__new__(SEMSolarSensor)
    sensor.entity_description = SensorEntityDescription(
        key=key, state_class=state_class)
    sensor.coordinator = SimpleNamespace(_energy_calculator=calc)
    return sensor


def _local(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=dt_util.get_default_time_zone())


def _run(calc, clock, until, step=timedelta(minutes=10), ev_w=3_000.0):
    """Cycle until ``until``; (time, last_reset) per cycle."""
    sensor = _sensor("daily_ev_energy", calc)
    seen = []
    while dt_util.now() <= until:
        _cycle(calc, ev_w)
        seen.append((dt_util.now(), sensor.last_reset))
        clock.tick(step)
    return seen


def _moves(seen):
    return [t for (t, r), (_, prev) in zip(seen[1:], seen[:-1], strict=True)
            if r != prev]


def test_the_car_day_sensor_is_the_house_wide_daily_ev_energy():
    assert CAR_DAY_SENSOR_KEYS == {"daily_ev_energy"}


def test_last_reset_is_the_rollover_and_midnight_for_the_rest():
    with freeze_time(_local(2026, 10, 22, 23, 0)) as clock:
        calc = _calc()
        _run(calc, clock, _local(2026, 10, 23, 8))
        assert _sensor("daily_ev_energy", calc).last_reset == _local(
            2026, 10, 23, 7)
        for key in ("daily_solar_energy", "daily_home_energy",
                    "daily_grid_import_energy", "flow_solar_to_ev_energy"):
            assert _sensor(key, calc).last_reset == _local(2026, 10, 23, 0)
        # the per-charger row counts TOTAL_INCREASING: no last_reset
        assert _sensor("charger_box_daily_energy", calc,
                       SensorStateClass.TOTAL_INCREASING).last_reset is None


def test_last_reset_moves_once_per_rollover_and_not_at_midnight():
    with freeze_time(_local(2026, 10, 22, 8, 0)) as clock:
        calc = _calc()
        seen = _run(calc, clock, _local(2026, 10, 25, 8))
    # the first car day this code saw keeps midnight, as before
    assert _moves(seen) == [_local(2026, 10, 23, 0), _local(2026, 10, 23, 7),
                            _local(2026, 10, 24, 7), _local(2026, 10, 25, 7)]


def test_the_upgrade_day_keeps_midnight_until_the_next_car_day():
    """New code first running at 12:00: moving last_reset to 07:00 now
    would make the recorder count the morning's kWh again."""
    with freeze_time(_local(2026, 10, 23, 12, 0)) as clock:
        seen = _run(_calc(), clock, _local(2026, 10, 24, 8))
    assert {r for t, r in seen if t < _local(2026, 10, 24, 0)} == {
        _local(2026, 10, 23, 0)}
    assert _moves(seen) == [_local(2026, 10, 24, 0), _local(2026, 10, 24, 7)]


def test_a_restart_keeps_the_rollover_moment():
    with freeze_time(_local(2026, 10, 23, 6, 0)) as clock:
        before = _calc()
        _run(before, clock, _local(2026, 10, 23, 8))
        expected = _sensor("daily_ev_energy", before).last_reset
        assert expected == _local(2026, 10, 23, 7)
        saved = before.get_state()
        # restart with the Charge-by moved: the running day keeps its start
        after = _calc("09:00")
        after.restore_state(saved)
        assert _sensor("daily_ev_energy", after).last_reset == expected
        _cycle(after)
        assert _sensor("daily_ev_energy", after).last_reset == expected


def test_the_upgrade_mark_survives_a_restart():
    with freeze_time(_local(2026, 10, 23, 12, 0)) as clock:
        before = _calc()
        _cycle(before)
        saved = before.get_state()
        after = _calc()
        after.restore_state(saved)
        _cycle(after)
        assert _sensor("daily_ev_energy", after).last_reset == _local(
            2026, 10, 23, 0)
        _run(after, clock, _local(2026, 10, 24, 8))
        assert _sensor("daily_ev_energy", after).last_reset == _local(
            2026, 10, 24, 7)


def test_the_rollover_moment_is_stored():
    from custom_components.solar_energy_management.coordinator import storage
    assert {"ev_seen_day", "ev_rolled_at"} <= set(
        storage.CALCULATOR_STATE_KEYS)


def test_a_sunrise_rollover_is_recorded_too():
    """No Charge-by rule: the car day rolls at sunrise. The sensor reports
    the moment it rolled, not a rule re-derived."""
    sunrise = _local(2026, 10, 24, 7, 43)
    with freeze_time(_local(2026, 10, 23, 12, 0)) as clock:
        calc = _calc()
        calc._ev_deadlines = lambda: []
        calc._time_manager.get_current_meter_day_sunrise_based = lambda: (
            dt_util.now().date() if dt_util.now() >= dt_util.now().replace(
                hour=7, minute=43, second=0, microsecond=0)
            else dt_util.now().date() - timedelta(days=1))
        _run(calc, clock, _local(2026, 10, 24, 9), step=timedelta(minutes=1))
    assert _sensor("daily_ev_energy", calc).last_reset == sunrise


# ── days already recorded: the car from its chargers' rows ──────────

TZ = timezone(timedelta(hours=2))
DAY = "2026-10-23"
BOX_A = "sensor.sem_charger_a_daily_energy"
BOX_B = "sensor.sem_charger_b_daily_energy"
FLEET = "sensor.sem_daily_ev_energy"


def _total_increasing(samples):
    """Hourly ``change`` rows as Home Assistant's recorder compiles a
    TOTAL_INCREASING sensor: ``samples`` is ``{hour: [states...]}``; a
    state below 90 % of the one before starts a new cycle
    (``reset_detected``), anything else adds its difference."""
    from homeassistant.components.sensor.recorder import reset_detected

    hass = Mock()
    hass.data = {}
    day = datetime.fromisoformat(DAY).replace(tzinfo=TZ)
    rows, prev = [], None
    for hour in range(24):
        change = 0.0
        for value in samples.get(hour, []):
            if prev is not None and reset_detected(
                    hass, BOX_A, value, prev, Mock()):
                change += value
            elif prev is not None:
                change += value - prev
            prev = value
        rows.append({"start": (day + timedelta(hours=hour)).isoformat(),
                     "change": round(change, 4)})
    return rows


def _flat(value):
    day = datetime.fromisoformat(DAY).replace(tzinfo=TZ)
    return [{"start": (day + timedelta(hours=h)).isoformat(), "change": value}
            for h in range(24)]


def _day(ev_rows):
    rows = {role: _flat(0.0) for role in FLOW_ROLES}
    rows["home"] = _flat(0.5)
    # the meters saw the car: import = house + the car's hour
    car = [0.0] * 24
    for row in ev_rows:
        hour = datetime.fromisoformat(row["start"]).astimezone(TZ).hour
        car[hour] += max(0.0, row["change"])
    rows["grid_import"] = [dict(r, change=0.5 + car[h])
                           for h, r in enumerate(_flat(0.5))]
    rows["ev"] = ev_rows
    return HourlyDay.from_statistics(rows, date=DAY, tz=TZ)


def test_case_a_a_reset_then_a_big_charge_is_counted():
    """8 kWh at 06:50, 0 at 07:00, 5 kWh by 08:00: a fall followed by more
    charge than the drop. The shape of one hour cannot tell it; the
    TOTAL_INCREASING row can."""
    samples = {0: [0.0], 6: [8.0], 7: [0.0, 2.0, 5.0]}
    day = _day(_total_increasing(samples))
    assert day.ev[7] == pytest.approx(5.0)
    assert day.ev[6] == pytest.approx(8.0)
    assert not day.counter_jumped


def test_case_b_a_fall_to_a_third_is_a_new_cycle():
    """10 → 3: the recorder starts a new cycle; the hour never goes
    negative and nothing is netted against the night."""
    samples = {0: [0.0], 3: [10.0], 9: [3.0]}
    day = _day(_total_increasing(samples))
    assert min(day.ev) >= 0.0
    assert day.ev[3] == pytest.approx(10.0)
    assert day.ev[9] == pytest.approx(3.0)
    assert not day.counter_jumped


def test_a_small_dip_stays_a_step_back():
    """10 → 9.5 is within the recorder's 10 %: a negative hour, netted."""
    samples = {0: [0.0], 3: [10.0], 9: [9.5]}
    rows = _total_increasing(samples)
    assert rows[9]["change"] == pytest.approx(-0.5)
    assert _day(rows).total("ev") == pytest.approx(9.5)


def test_two_chargers_add_up():
    a = _total_increasing({0: [0.0], 2: [3.0], 7: [0.0, 1.0]})
    b = _total_increasing({0: [0.0], 2: [4.0], 7: [4.0, 6.0]})
    stats = {BOX_A: a, BOX_B: b, FLEET: _flat(0.0)}
    day = _day(car_rows(stats, [BOX_A, BOX_B], FLEET))
    assert day.ev[2] == pytest.approx(7.0)
    assert day.ev[7] == pytest.approx(1.0 + 2.0)
    assert day.total("ev") == pytest.approx(10.0)


def test_without_charger_rows_the_fleet_row_is_read_as_before():
    """The fallback keeps today's rule: a fall is a step back, noted."""
    fleet = _flat(0.0)
    fleet[0]["change"] = 11.57
    for h in range(1, 6):
        fleet[h]["change"] = 0.62
    fleet[7]["change"] = -14.67
    rows = car_rows({FLEET: fleet, BOX_A: []}, [BOX_A], FLEET)
    assert rows == fleet
    day = _day(rows)
    assert any("ev counter stepped back 14.67 kWh at 07:00" in n
               for n in day.notes)
    assert day.counter_jumped


def test_the_charger_sensors_are_found_removed_ones_too(monkeypatch):
    from homeassistant.helpers import entity_registry as er

    def entry(eid, uid, platform="solar_energy_management"):
        return SimpleNamespace(entity_id=eid, unique_id=uid, platform=platform)

    reg = SimpleNamespace(
        entities={
            1: entry(BOX_A, "sem_charger_a_daily_energy"),
            2: entry("sensor.sem_pv_string_pv1_daily_energy",
                     "sem_pv_string_pv1_daily_energy"),
            3: entry(FLEET, "sem_daily_ev_energy"),
            4: entry("sensor.other_charger_x_daily_energy",
                     "sem_charger_x_daily_energy", platform="other"),
        },
        deleted_entities={
            5: entry(BOX_B, "sem_charger_b_daily_energy"),
        })
    monkeypatch.setattr(er, "async_get", lambda _hass: reg)
    assert car_daily_entity_ids(object()) == [BOX_A, BOX_B]


def test_no_registry_no_charger_rows(monkeypatch):
    from homeassistant.helpers import entity_registry as er

    def boom(_hass):
        raise RuntimeError("no registry")

    monkeypatch.setattr(er, "async_get", boom)
    assert car_daily_entity_ids(object()) == []


@pytest.mark.asyncio
async def test_the_simulation_reads_the_car_from_the_chargers(monkeypatch):
    a = _total_increasing({0: [0.0], 7: [0.0, 2.0]})
    fleet = _flat(0.0)
    fleet[7]["change"] = -9.0
    asked = []

    async def read(_hass, _start, _end, entity_ids, _types):
        asked.append(set(entity_ids))
        core = {eid: _flat(0.5) for eid in ("sensor.s", "sensor.i",
                                            "sensor.e", "sensor.h")}
        return {**core, BOX_A: a, FLEET: fleet}

    monkeypatch.setattr(simulation_job, "car_daily_entities",
                        lambda _hass: {BOX_A: True})
    seen = {}

    def capture(rows, **kw):
        seen.update(rows)
        seen["_notes"] = list(kw.get("notes") or [])
        raise RuntimeError("stop here")

    monkeypatch.setattr(simulation_job.HourlyDay, "from_statistics", capture)
    job = simulation_job.SimulationJob(
        Mock(), SimpleNamespace(config={}), read_stats=read)
    ids = {"solar": "sensor.s", "grid_import": "sensor.i",
           "grid_export": "sensor.e", "home": "sensor.h", "ev": FLEET}
    with pytest.raises(RuntimeError, match="stop here"):
        await job._one_day({}, DAY, [], None, {}, ids, TZ)
    assert BOX_A in asked[0]
    assert seen["ev"] == a
    assert seen["_notes"] == []


@pytest.mark.asyncio
async def test_the_day_tags_read_the_car_from_the_chargers(monkeypatch):
    from homeassistant.util import dt as dt_util

    day = datetime(2026, 10, 23).date()
    start = dt_util.start_of_local_day(day)
    rows_a = [{"start": start + timedelta(hours=7), "change": 2.0}]
    rows_b = [{"start": start + timedelta(hours=7), "change": 3.0}]

    async def read(_hass, _start, _end, entity_ids, _types):
        return {"sensor.s": [{"start": start + timedelta(hours=12),
                              "change": 1.0}],
                BOX_A: rows_a, BOX_B: rows_b,
                FLEET: [{"start": start + timedelta(hours=7),
                         "change": -9.0}]}

    monkeypatch.setattr(day_tag_store, "car_daily_entity_ids",
                        lambda _hass: [BOX_A, BOX_B])
    store = day_tag_store.DayTagger.__new__(day_tag_store.DayTagger)
    store.hass = Mock()
    store._read_stats = read
    got = await store._read_day_stats(day, {"solar": "sensor.s", "ev": FLEET})
    assert got["ev"] == {7: 5.0}


# ── a doubtful sum is never silent ──────────────────────────────────

D0 = datetime.fromisoformat(DAY).replace(tzinfo=TZ)
D1 = D0 + timedelta(days=1)


def _hours(first, count, change=0.1):
    return [{"start": (first + timedelta(hours=h)).timestamp(),
             "change": change} for h in range(count)]


def _period(**rows):
    """Rows over 22.–24.10 per charger; the spans come from them."""
    return dict(rows)


def _doubt(stats, live, spans_from=None):
    spans = car_spans(spans_from or stats, list(live))
    return car_day_notes(stats, live, D0, D1, spans)


def test_a_charger_missing_hours_marks_the_day():
    period = _period(**{BOX_A: _hours(D0 - timedelta(days=1), 72),
                        BOX_B: _hours(D0 - timedelta(days=1), 72)})
    day = {BOX_A: _hours(D0, 24), BOX_B: _hours(D0, 12)}
    notes = _doubt(day, {BOX_A: True, BOX_B: True}, period)
    assert notes == [f"{CAR_DOUBT}: a charger's car energy is missing for "
                     f"12 hours of this day ({BOX_B})"]
    hd = HourlyDay.from_statistics(
        {"solar": _flat(0.0), "grid_import": _flat(0.5),
         "grid_export": _flat(0.0), "home": _flat(0.5)},
        date=DAY, tz=TZ, notes=notes)
    assert hd.counter_jumped


def test_a_charger_missing_the_whole_day_marks_it():
    period = _period(**{BOX_A: _hours(D0 - timedelta(days=1), 72),
                        BOX_B: _hours(D0 - timedelta(days=1), 24)
                        + _hours(D1, 24)})
    day = {BOX_A: _hours(D0, 24), BOX_B: []}
    notes = _doubt(day, {BOX_A: True, BOX_B: True}, period)
    assert len(notes) == 1 and "missing for 24 hours" in notes[0]


def test_a_charger_added_later_is_not_missing():
    period = _period(**{BOX_A: _hours(D0 - timedelta(days=1), 72),
                        BOX_B: _hours(D1, 24)})
    day = {BOX_A: _hours(D0, 24), BOX_B: []}
    assert _doubt(day, {BOX_A: True, BOX_B: True}, period) == []


def test_a_removed_charger_overlapping_a_live_one_marks_the_day():
    day = {BOX_A: _hours(D0, 24), BOX_B: _hours(D0, 24)}
    notes = _doubt(day, {BOX_A: True, BOX_B: False})
    assert notes == [f"{CAR_DOUBT}: a removed charger's car energy overlaps "
                     f"a live one for 24 hours — it may count twice ({BOX_B})"]


def test_a_replaced_charger_does_not_overlap():
    # the old sensor until 10:00, the new one from 10:00
    day = {BOX_B: _hours(D0, 10), BOX_A: _hours(D0 + timedelta(hours=10), 14)}
    spans = {BOX_B: (D0 - timedelta(days=5), D0 + timedelta(hours=9)),
             BOX_A: (D0 + timedelta(hours=10), D1 + timedelta(days=5))}
    assert car_day_notes(day, {BOX_A: True, BOX_B: False}, D0, D1,
                         spans) == []


def test_no_charger_rows_no_doubt():
    assert _doubt({BOX_A: []}, {BOX_A: True}) == []


@pytest.mark.asyncio
async def test_the_simulation_passes_the_doubt_on(monkeypatch):
    async def read(_hass, start, end, entity_ids, _types):
        core = {eid: _flat(0.5) for eid in ("sensor.s", "sensor.i",
                                            "sensor.e", "sensor.h")}
        if end - start > timedelta(days=1, hours=1):        # the span read
            return {BOX_A: _hours(D0 - timedelta(days=1), 72),
                    BOX_B: _hours(D0 - timedelta(days=1), 72)}
        return {**core, BOX_A: _hours(D0, 24), BOX_B: _hours(D0, 20)}

    monkeypatch.setattr(simulation_job, "car_daily_entities",
                        lambda _hass: {BOX_A: True, BOX_B: True})
    seen = {}

    def capture(rows, **kw):
        seen["notes"] = list(kw.get("notes") or [])
        raise RuntimeError("stop here")

    monkeypatch.setattr(simulation_job.HourlyDay, "from_statistics", capture)
    from homeassistant.util import dt as dt_util
    monkeypatch.setattr(dt_util, "start_of_local_day",
                        lambda d: datetime(d.year, d.month, d.day, tzinfo=TZ))
    job = simulation_job.SimulationJob(
        Mock(), SimpleNamespace(config={}), read_stats=read)
    ids = {"solar": "sensor.s", "grid_import": "sensor.i",
           "grid_export": "sensor.e", "home": "sensor.h"}
    run = {"period": {"from": "2026-10-22", "to": "2026-10-24"}}
    with pytest.raises(RuntimeError, match="stop here"):
        await job._one_day(run, DAY, [], None, {}, ids, TZ)
    assert seen["notes"] == [f"{CAR_DOUBT}: a charger's car energy is "
                             f"missing for 4 hours of this day ({BOX_B})"]


def test_live_and_removed_are_told_apart(monkeypatch):
    from homeassistant.helpers import entity_registry as er

    def entry(eid, uid):
        return SimpleNamespace(entity_id=eid, unique_id=uid,
                               platform="solar_energy_management")

    reg = SimpleNamespace(
        entities={1: entry(BOX_A, "sem_charger_a_daily_energy")},
        deleted_entities={2: entry(BOX_B, "sem_charger_b_daily_energy")})
    monkeypatch.setattr(er, "async_get", lambda _hass: reg)
    assert car_daily_entities(object()) == {BOX_A: True, BOX_B: False}
