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

from datetime import date, datetime, timedelta, timezone
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
from custom_components.solar_energy_management.coordinator.energy_calculator import (
    EnergyCalculator,
)
from custom_components.solar_energy_management.coordinator.simulation_job import (
    SimulationJob,
)
from custom_components.solar_energy_management.coordinator.types import PowerReadings
from custom_components.solar_energy_management.sensor import (
    CAR_DAY_SENSOR_KEYS,
    SEMSolarSensor,
)
from custom_components.solar_energy_management.utils.time_manager import TimeManager

CHARGE_BY = "07:00"


# ── the sensor's last_reset ─────────────────────────────────────────


def _calc(charge_by: str = CHARGE_BY, *, upgraded=True) -> EnergyCalculator:
    """``upgraded``: the install is past the car day this code first ran on."""
    hass = Mock()
    hass.data = {}
    hass.states.get = lambda _eid: None
    calc = EnergyCalculator(
        {"update_interval": 10, "ev_target_time": charge_by,
         "ev_chargers": [{"id": "box"}]},
        TimeManager(hass),
    )
    if upgraded:
        calc._ev_reset_since = date(2026, 10, 1)
    return calc


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


def test_the_car_day_sensor_is_the_house_wide_daily_ev_energy():
    assert CAR_DAY_SENSOR_KEYS == {"daily_ev_energy"}


def test_last_reset_is_the_car_day_start_and_midnight_for_the_rest():
    with freeze_time(_local(2026, 10, 23, 6, 0)):
        calc = _calc()
        _cycle(calc)
        assert _sensor("daily_ev_energy", calc).last_reset == _local(
            2026, 10, 22, 7)
        for key in ("daily_solar_energy", "daily_home_energy",
                    "daily_grid_import_energy", "flow_solar_to_ev_energy"):
            assert _sensor(key, calc).last_reset == _local(2026, 10, 23, 0)
        # the per-charger row counts TOTAL_INCREASING: no last_reset
        assert _sensor("charger_box_daily_energy", calc,
                       SensorStateClass.TOTAL_INCREASING).last_reset is None


def test_last_reset_moves_once_at_the_rollover():
    seen = []
    with freeze_time(_local(2026, 10, 22, 23, 0)) as clock:
        calc = _calc()
        sensor = _sensor("daily_ev_energy", calc)
        for _ in range(10 * 6):          # 23:00 → 09:00, every 10 minutes
            _cycle(calc, 3_000.0)
            seen.append((dt_util.now(), sensor.last_reset))
            clock.tick(timedelta(minutes=10))
    resets = sorted({r for _, r in seen})
    assert resets == [_local(2026, 10, 22, 7), _local(2026, 10, 23, 7)]
    moved = [t for (t, r), (_, prev) in zip(seen[1:], seen[:-1], strict=True) if r != prev]
    assert moved == [_local(2026, 10, 23, 7)]
    # midnight leaves it where it is
    assert all(r == _local(2026, 10, 22, 7) for t, r in seen
               if t < _local(2026, 10, 23, 7))


def test_a_restart_keeps_the_car_day_start():
    with freeze_time(_local(2026, 10, 23, 6, 0)):
        before = _calc()
        _cycle(before, 3_000.0)
        expected = _sensor("daily_ev_energy", before).last_reset
        saved = before.get_state()
        # restart with the Charge-by moved: the running day keeps its start
        after = _calc("09:00")
        after.restore_state(saved)
        assert _sensor("daily_ev_energy", after).last_reset == expected
        _cycle(after)
        assert _sensor("daily_ev_energy", after).last_reset == expected
        assert expected == _local(2026, 10, 22, 7)


def test_the_upgrade_day_keeps_midnight_until_the_next_car_day():
    """New code first running at 12:00: moving last_reset to 07:00 now
    would make the recorder count the morning's kWh again."""
    seen = []
    with freeze_time(_local(2026, 10, 23, 12, 0)) as clock:
        calc = _calc(upgraded=False)
        sensor = _sensor("daily_ev_energy", calc)
        for _ in range(20 * 6):          # 12:00 → 08:00 next day
            _cycle(calc, 3_000.0)
            seen.append((dt_util.now(), sensor.last_reset))
            clock.tick(timedelta(minutes=10))
    first_day = [r for t, r in seen if t < _local(2026, 10, 24, 0)]
    assert set(first_day) == {_local(2026, 10, 23, 0)}
    assert seen[-1][1] == _local(2026, 10, 24, 7)
    moved = [t for (t, r), (_, prev) in zip(seen[1:], seen[:-1], strict=True)
             if r != prev]
    # midnight as before, then the car day's start from its first rollover
    assert moved == [_local(2026, 10, 24, 0), _local(2026, 10, 24, 7)]


def test_the_upgrade_mark_survives_a_restart():
    with freeze_time(_local(2026, 10, 23, 12, 0)):
        before = _calc(upgraded=False)
        _cycle(before)
        assert _sensor("daily_ev_energy", before).last_reset == _local(
            2026, 10, 23, 0)
        saved = before.get_state()
    with freeze_time(_local(2026, 10, 24, 8, 0)):
        after = _calc(upgraded=False)
        after.restore_state(saved)
        _cycle(after)
        assert _sensor("daily_ev_energy", after).last_reset == _local(
            2026, 10, 24, 7)


def test_the_upgrade_mark_is_stored():
    from custom_components.solar_energy_management.coordinator import storage
    assert "ev_reset_since" in storage.CALCULATOR_STATE_KEYS


def test_a_fleet_on_different_deadlines_resets_at_midnight():
    with freeze_time(_local(2026, 10, 23, 6, 0)):
        calc = _calc()
        calc.config["ev_chargers"] = [{"id": "a", "ev_target_time": "06:00"},
                                      {"id": "b", "ev_target_time": "08:00"}]
        calc._ev_day_offset = calc._ev_day_key = None
        assert calc.ev_day_start() == _local(2026, 10, 23, 0)


def test_the_simulation_reads_the_reset_hour_from_the_same_rule():
    with freeze_time(_local(2026, 10, 23, 12, 0)):
        calc = _calc("07:30")
        job = SimulationJob(Mock(), SimpleNamespace(_energy_calculator=calc,
                                                    config={}))
        assert job._ev_day_start_hour() == 7
        assert SimulationJob(Mock(), SimpleNamespace(config={})
                             )._ev_day_start_hour() is None


# ── days already recorded ───────────────────────────────────────────

TZ = timezone(timedelta(hours=2))
DAY = "2026-10-23"
CARRIED = 10.95          # what the car counter held at midnight
NIGHT = [0.62] * 6       # 00:00–06:00: 10.95 → 14.67


def _rows(date, values, key="change", states=None):
    day = datetime.fromisoformat(date).replace(tzinfo=TZ)
    rows = []
    for h, v in enumerate(values):
        row = {"start": (day + timedelta(hours=h)).isoformat(), key: v}
        if states is not None:
            row["state"] = states[h]
        rows.append(row)
    return rows


def _flows(ev_rows, ev_true):
    base = {role: _rows(DAY, [0.0] * 24) for role in FLOW_ROLES}
    base["home"] = _rows(DAY, [0.5] * 24)
    base["grid_import"] = _rows(DAY, [0.5 + e for e in ev_true])
    base["ev"] = ev_rows
    base["soc"] = _rows(DAY, [50.0] * 24, key="mean")
    return base


def _prod_ev_rows(with_carried=True, after_reset=0.0):
    """PROD 23.10: the recorder restarted the cycle at midnight, so hour 0
    holds the carried 10.95 kWh plus the hour's 0.62; 14.67 at 06:00; 0 at
    07:00 under an unchanged last_reset."""
    states, value = [], CARRIED
    for kwh in NIGHT:
        value += kwh
        states.append(round(value, 2))
    states += [states[-1], after_reset] + [after_reset] * 16
    changes = [states[0]] + [round(b - a, 2) for a, b in
                             zip(states[:6], states[1:7], strict=True)]
    changes += [round(after_reset - states[6], 2)] + [0.0] * 16
    rows = _rows(DAY, changes, states=states)
    if with_carried:
        rows.insert(0, {"start": "2026-10-22T23:00:00+02:00",
                        "change": 0.0, "state": CARRIED})
    return rows


def test_the_prod_rollover_drop_is_a_reset_not_a_step_back():
    ev_true = NIGHT + [0.0] * 18
    day = HourlyDay.from_statistics(_flows(_prod_ev_rows(), ev_true),
                                    date=DAY, tz=TZ, ev_day_start_hour=7)
    assert day.ev[:6] == pytest.approx(NIGHT)
    assert day.ev[6] == day.ev[7] == 0.0
    assert day.total("ev") == pytest.approx(3.72)
    assert not any("stepped back" in n for n in day.notes)
    assert not day.counter_jumped


def test_the_rollover_hour_books_its_own_charge():
    ev_true = NIGHT + [0.0, 1.2] + [0.0] * 16
    day = HourlyDay.from_statistics(
        _flows(_prod_ev_rows(after_reset=1.2), ev_true),
        date=DAY, tz=TZ, ev_day_start_hour=7)
    assert day.ev[7] == pytest.approx(1.2)
    assert day.total("ev") == pytest.approx(3.72 + 1.2)
    assert not day.counter_jumped


def test_without_the_carried_value_the_day_is_marked():
    """The night cannot be told from the carried kWh: the day says so and
    the self-check leaves it out."""
    ev_true = [0.0] * 24
    day = HourlyDay.from_statistics(
        _flows(_prod_ev_rows(with_carried=False), ev_true),
        date=DAY, tz=TZ, ev_day_start_hour=7)
    assert day.ev[:8] == pytest.approx([0.0] * 8)
    assert any("ev counter stepped back 14.67 kWh at 07:00" in n
               and "could not be read" in n for n in day.notes)
    assert day.counter_jumped


def test_without_the_reset_hour_the_drop_is_still_a_step_back():
    ev_true = NIGHT + [0.0] * 18
    day = HourlyDay.from_statistics(_flows(_prod_ev_rows(), ev_true),
                                    date=DAY, tz=TZ)
    assert any("ev counter stepped back 14.67 kWh at 07:00" in n
               for n in day.notes)
    assert day.counter_jumped


def test_a_real_step_back_at_another_hour_is_still_noted():
    changes = [0.0] * 12 + [3.0, 0.0, -2.0] + [0.0] * 9
    ev_true = [0.0] * 12 + [1.0] + [0.0] * 11
    day = HourlyDay.from_statistics(_flows(_rows(DAY, changes), ev_true),
                                    date=DAY, tz=TZ, ev_day_start_hour=7)
    assert any("ev counter stepped back 2.00 kWh at 14:00" in n
               for n in day.notes)
    assert day.counter_jumped


def test_only_the_car_counter_is_read_this_way():
    flows = _flows(_rows(DAY, [0.0] * 24), [0.0] * 24)
    flows["solar"] = _rows(DAY, [0.0] * 7 + [-2.0] + [0.0] * 16)
    flows["solar"][0]["change"] = 2.0
    day = HourlyDay.from_statistics(flows, date=DAY, tz=TZ,
                                    ev_day_start_hour=7)
    assert any("solar counter stepped back 2.00 kWh at 07:00" in n
               for n in day.notes)
