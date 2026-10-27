"""#1187 — a restart after dark lost the solar made while SEM was down.

PROD, 12.10.2026: HA went down at 02:30 and came back at 20:00, after
sunset. The inverter's yield counter (``sensor.inverter_gesamtenergieertrag``)
rose 30.38 kWh in that one step. The meter rows took in their whole steps
(20.42 kWh export, 8.79 kWh into the battery, 5.44 kWh out of it), but the
sun gate (#681) judges a step by the sun at the moment it is read: after dark
the whole step was rolled into the baseline. Solar stayed at 0 kWh, and home,
the balance of the rows, fell from 1.62 to 0 kWh.

The other way round, a step read in daylight after a night start was booked in
full, the night's battery output with it — the #681 fault, once per outage.

The fix judges a step by its span: when a sunrise or sunset passed since the
last read, and the last read is older than the integrator's gap limit, the
step is credited less what the batteries put out over the same span (in the
dark a hybrid's counter moves only by that).
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from custom_components.solar_energy_management.coordinator import (
    energy_calculator as ec,
)
from custom_components.solar_energy_management.coordinator.energy_calculator import (
    EnergyCalculator,
)

DAY = date(2026, 10, 12)
MONTH, YEAR = "2026_10", "2026"
SOLAR = "sensor.inverter_gesamtenergieertrag"
SOLAR_2 = "sensor.inverter_2_total_yield"
IMPORT = "sensor.power_meter_verbrauch"
EXPORT = "sensor.power_meter_exportierte_energie"
BATT_IN = "sensor.battery_1_gesamtladung"
BATT_OUT = "sensor.battery_1_gesamtentladung"
BATT_OUT_2 = "sensor.battery_2_total_discharge"

SUNRISE = datetime(2026, 10, 12, 5, 40, tzinfo=timezone.utc)   # 07:40 CEST
SUNSET = datetime(2026, 10, 12, 16, 40, tzinfo=timezone.utc)   # 18:40 CEST
NEXT_SUNRISE = SUNRISE + timedelta(days=1)
NEXT_SUNSET = SUNSET + timedelta(days=1)


def _at(hh, mm=0, ss=0):
    """A UTC time on the PROD day."""
    return datetime(2026, 10, 12, hh, mm, ss, tzinfo=timezone.utc)


def _kwh(value):
    return SimpleNamespace(state=str(value),
                           attributes={"unit_of_measurement": "kWh"})


def _sun(states, when):
    """``sun.sun`` as HA shows it at ``when``: below the horizon outside
    sunrise..sunset, and the next rising and setting as ISO strings."""
    up = SUNRISE <= when < SUNSET
    rising = SUNRISE if when < SUNRISE else NEXT_SUNRISE
    setting = SUNSET if when < SUNSET else NEXT_SUNSET
    states["sun.sun"] = SimpleNamespace(
        state="above_horizon" if up else "below_horizon",
        attributes={"next_rising": rising.isoformat(),
                    "next_setting": setting.isoformat()})


def _calc(states, solar=(SOLAR,), outs=(BATT_OUT,), meters=True):
    """Meters read flat unless a test moves them."""
    from homeassistant.core import CoreState
    for entity_id, value in ((IMPORT, 8423.80), (EXPORT, 10453.45),
                             (BATT_IN, 7005.23)):
        states.setdefault(entity_id, _kwh(value))
    hass = MagicMock()
    hass.state = CoreState.running
    hass.states.get = lambda eid: states.get(eid)
    calc = EnergyCalculator(config={"system_size_kwp": 10},
                            time_manager=MagicMock())
    calc.configure_solar_counters(hass, list(solar), True)
    mapping = {}
    if meters:
        mapping = {"grid_import": [IMPORT], "grid_export": [EXPORT],
                   "battery_charge": [BATT_IN]}
    if outs:
        mapping["battery_discharge"] = list(outs)
    calc.configure_meter_counters(hass, mapping, True)
    return calc


def _solar(calc):
    return calc._daily_accumulators.get(f"solar_{DAY}", 0.0)


def _go(calc, states, when, **counters):
    for entity_id, value in counters.items():
        states[entity_id] = _kwh(value) if value is not None else None
    _sun(states, when)
    calc._counter_backed = set()
    calc._reconcile_solar_energy(DAY, MONTH, YEAR, when)


def _prod_night(calc, states):
    """02:20–02:30 (00:20Z–00:30Z): SEM watches the night, then HA stops."""
    for minute, solar, out in ((20, 23832.80, 7104.63), (25, 23832.83, 7104.66),
                               (30, 23832.86, 7104.69)):
        _go(calc, states, _at(0, minute), **{SOLAR: solar, BATT_OUT: out})


@pytest.mark.unit
class TestTheProdDay:
    """THE regression: 12.10.2026 on PROD, replayed."""

    def test_the_day_made_while_ha_was_down_is_counted(self):
        states = {}
        calc = _calc(states)
        _prod_night(calc, states)
        assert _solar(calc) == 0.0
        # 20:00 CEST: HA is back, the sun has set
        _go(calc, states, _at(18, 0),
            **{SOLAR: 23863.24, BATT_OUT: 7110.13, IMPORT: 8424.03})
        # the 30.38 kWh step less the battery's 5.44 kWh and the grid's
        # 0.23 kWh over it (0 before)
        assert _solar(calc) == pytest.approx(30.38 - 5.44 - 0.23)
        assert calc._monthly_accumulators[f"solar_{MONTH}"] == pytest.approx(24.71)
        assert calc._yearly_accumulators[f"solar_{YEAR}"] == pytest.approx(24.71)
        assert calc._lifetime_accumulators["lifetime_solar"] == pytest.approx(24.71)
        assert "solar" in calc._counter_backed

    def test_the_night_after_the_restart_is_still_not_solar(self):
        states = {}
        calc = _calc(states)
        _prod_night(calc, states)
        _go(calc, states, _at(18, 0),
            **{SOLAR: 23863.24, BATT_OUT: 7110.13, IMPORT: 8424.03})
        # the counter after 20:00: +0.5 kWh an hour. The battery counter is
        # held flat, so only the sun's turn keeps these hours out (review).
        for n, (solar, out) in enumerate(((23863.74, 7110.13),
                                          (23864.21, 7110.13),
                                          (23864.65, 7110.13)), start=1):
            _go(calc, states, _at(18 + n), **{SOLAR: solar, BATT_OUT: out})
            # one cycle after each hour: SEM watches the night again
            _go(calc, states, _at(18 + n, 0, 10), **{SOLAR: solar, BATT_OUT: out})
        assert _solar(calc) == pytest.approx(24.71)

    def test_home_no_longer_falls(self):
        """The rows as PROD had them at 02:30; at 20:00 every meter row takes
        its step. Home, their balance, fell to 0 before (solar 0)."""
        states = {}
        calc = _calc(states)
        calc._midnight_ev_since = DAY - timedelta(days=1)
        rows = {"solar": 0.0, "grid_import": 0.03, "grid_export": 0.05,
                "battery_discharge": 1.64, "battery_charge": 0.0, "home": 1.62}
        for category, kwh in rows.items():
            calc._daily_accumulators[f"{category}_{DAY}"] = kwh

        def cycle(when, solar, imp, exp, b_in, b_out):
            for entity_id, value in ((IMPORT, imp), (EXPORT, exp),
                                     (BATT_IN, b_in)):
                states[entity_id] = _kwh(value)
            _go(calc, states, when, **{SOLAR: solar, BATT_OUT: b_out})
            for category in ("grid_import", "grid_export", "battery_charge",
                             "battery_discharge"):
                calc._reconcile_metered_energy(category, DAY, MONTH, YEAR)
            calc._reconcile_home_energy(DAY, MONTH, YEAR)

        cycle(_at(0, 30), 23832.86, 8423.80, 10453.45, 7005.23, 7104.69)
        assert calc._daily_accumulators[f"home_{DAY}"] == pytest.approx(1.62)
        cycle(_at(18, 0), 23863.24, 8424.03, 10473.87, 7014.02, 7110.13)
        home = calc._daily_accumulators[f"home_{DAY}"]
        # 24.71 + 0.26 − 20.47 + 7.08 − 8.79 (0.0 before the fix)
        assert home == pytest.approx(2.79, abs=0.01)
        assert home >= 1.62


@pytest.mark.unit
class TestTheSpanDecides:

    def test_a_read_in_daylight_after_a_night_start_keeps_the_battery_out(self):
        """The sibling: down 02:00–09:00 CEST, read in daylight. 3 kWh of the
        step was the battery at night; it was booked as solar before."""
        states = {}
        calc = _calc(states)
        _go(calc, states, _at(0, 0), **{SOLAR: 5000.0, BATT_OUT: 100.0})
        _go(calc, states, _at(7, 0), **{SOLAR: 5004.0, BATT_OUT: 103.0})
        assert _solar(calc) == pytest.approx(1.0)
        # the day goes on from there
        _go(calc, states, _at(10, 0), **{SOLAR: 5014.0, BATT_OUT: 103.0})
        assert _solar(calc) == pytest.approx(11.0)

    def test_a_watched_morning_then_down_into_the_night(self):
        """SEM saw the morning, HA was down 16:00–23:00 CEST."""
        states = {}
        calc = _calc(states)
        _go(calc, states, _at(5, 45), **{SOLAR: 5000.0, BATT_OUT: 100.0})
        _go(calc, states, _at(12, 0), **{SOLAR: 5010.0, BATT_OUT: 100.0})
        assert _solar(calc) == pytest.approx(10.0)
        _go(calc, states, _at(21, 0), **{SOLAR: 5016.0, BATT_OUT: 101.0})
        assert _solar(calc) == pytest.approx(15.0)   # 10.0 before

    def test_a_gap_inside_one_night_is_still_absorbed(self):
        states = {}
        calc = _calc(states)
        _go(calc, states, _at(19, 0), **{SOLAR: 5000.0, BATT_OUT: 100.0})
        _go(calc, states, _at(23, 0), **{SOLAR: 5002.0, BATT_OUT: 100.5})
        assert _solar(calc) == 0.0

    def test_a_gap_before_sunrise_is_still_absorbed(self):
        states = {}
        calc = _calc(states)
        _go(calc, states, _at(0, 30), **{SOLAR: 5000.0, BATT_OUT: 100.0})
        _go(calc, states, _at(5, 30), **{SOLAR: 5002.5, BATT_OUT: 100.5})
        assert _solar(calc) == 0.0

    def test_a_gap_inside_one_day_is_unchanged(self):
        """Both reads in daylight: the step is credited as before, the
        battery's output not taken off."""
        states = {}
        calc = _calc(states)
        _go(calc, states, _at(8, 0), **{SOLAR: 5000.0, BATT_OUT: 100.0})
        _go(calc, states, _at(13, 0), **{SOLAR: 5012.0, BATT_OUT: 102.0})
        assert _solar(calc) == pytest.approx(12.0)

    def test_one_cycle_across_sunset_is_judged_as_before(self):
        """A step no longer than the integrator's gap limit is one cycle."""
        states = {}
        calc = _calc(states)
        _go(calc, states, _at(10, 0), **{SOLAR: 5000.0, BATT_OUT: 100.0})
        _go(calc, states, SUNSET - timedelta(seconds=30),
            **{SOLAR: 5010.0, BATT_OUT: 100.0})
        assert _solar(calc) == pytest.approx(10.0)
        _go(calc, states, SUNSET - timedelta(seconds=30)
            + timedelta(seconds=ec.MAX_INTEGRATION_GAP_SECONDS),
            **{SOLAR: 5011.0, BATT_OUT: 100.0})
        assert _solar(calc) == pytest.approx(10.0)

    def test_a_gap_just_past_the_limit_across_sunset_counts(self):
        states = {}
        calc = _calc(states)
        _go(calc, states, _at(10, 0), **{SOLAR: 5000.0, BATT_OUT: 100.0})
        _go(calc, states, SUNSET - timedelta(seconds=30),
            **{SOLAR: 5010.0, BATT_OUT: 100.0})
        _go(calc, states, SUNSET - timedelta(seconds=30)
            + timedelta(seconds=ec.MAX_INTEGRATION_GAP_SECONDS + 1),
            **{SOLAR: 5011.0, BATT_OUT: 100.0})
        assert _solar(calc) == pytest.approx(11.0)


@pytest.mark.unit
class TestTheBatteryBound:

    def test_no_battery_holds_nothing_back(self):
        """No battery: a counter cannot move in the dark — the whole step."""
        states = {}
        calc = _calc(states, outs=(), meters=False)
        calc.configure_meter_counters(
            calc._hass, {"grid_import": [IMPORT], "grid_export": [EXPORT]}, True)
        _go(calc, states, _at(0, 30), **{SOLAR: 5000.0})
        _go(calc, states, _at(18, 0), **{SOLAR: 5030.0})
        assert _solar(calc) == pytest.approx(30.0)

    @pytest.mark.parametrize("known_by", ["charge counter", "booked energy"])
    def test_a_battery_without_a_discharge_counter_holds_all_back(self, known_by):
        """(review) A battery with no discharge counter (a manual setup
        reads its power only): nothing bounds its output, so the step is
        kept out, as the sun gate did before."""
        states = {}
        calc = _calc(states, outs=(), meters=known_by == "charge counter")
        if known_by == "booked energy":
            calc._lifetime_accumulators["lifetime_battery_discharge"] = 812.4
        _go(calc, states, _at(0, 30), **{SOLAR: 5000.0})
        _go(calc, states, _at(18, 0), **{SOLAR: 5030.0})
        assert _solar(calc) == 0.0

    def test_one_battery_going_back_is_not_hidden_by_the_other(self):
        """(review) The sum rose by 1; one counter went back."""
        states = {}
        calc = _calc(states, outs=(BATT_OUT, BATT_OUT_2))
        _go(calc, states, _at(0, 30),
            **{SOLAR: 5000.0, BATT_OUT: 100.0, BATT_OUT_2: 8.0})
        _go(calc, states, _at(18, 0),
            **{SOLAR: 5030.0, BATT_OUT: 106.0, BATT_OUT_2: 3.0})
        assert _solar(calc) == 0.0

    def test_a_riemann_sum_cannot_bound(self, monkeypatch):
        """(review) HA's ``integration`` helper does not count while HA is
        down: flat over the outage, it says nothing of the night."""
        from homeassistant.helpers import entity_registry as er
        registry = Mock()
        registry.async_get = lambda eid: SimpleNamespace(
            platform="integration" if eid == BATT_OUT else "huawei_solar")
        monkeypatch.setattr(er, "async_get", lambda hass: registry)
        states = {}
        calc = _calc(states)
        _go(calc, states, _at(0, 30), **{SOLAR: 5000.0, BATT_OUT: 100.0})
        _go(calc, states, _at(18, 0), **{SOLAR: 5030.0, BATT_OUT: 100.0})
        assert _solar(calc) == 0.0
        registry.async_get = lambda eid: SimpleNamespace(platform="huawei_solar")
        calc = _calc(states)
        _go(calc, states, _at(0, 30), **{SOLAR: 5000.0, BATT_OUT: 100.0})
        _go(calc, states, _at(18, 0), **{SOLAR: 5030.0, BATT_OUT: 105.0})
        assert _solar(calc) == pytest.approx(25.0)

    @pytest.mark.parametrize("how", [
        "sem's own", "yaml without an id", "powercalc", "no registry"])
    def test_any_counter_ha_computes_cannot_bound(self, monkeypatch, how):
        """(review round 2) A YAML Riemann sum has no registry entry but a
        ``source``; powercalc names ``source_entity``; SEM's own lifetime
        sensor in the Energy Dashboard counts only while SEM runs; no
        registry at all is no proof."""
        from homeassistant.helpers import entity_registry as er

        from custom_components.solar_energy_management.const import DOMAIN
        registry = Mock()
        registry.async_get = lambda eid: (
            SimpleNamespace(platform=DOMAIN)
            if how == "sem's own" and eid == BATT_OUT else None)

        def _registry(hass):
            if how == "no registry":
                raise KeyError("entity_registry")
            return registry
        monkeypatch.setattr(er, "async_get", _registry)
        states = {}
        calc = _calc(states)
        _go(calc, states, _at(0, 30), **{SOLAR: 5000.0, BATT_OUT: 100.0})
        _go(calc, states, _at(18, 0), **{SOLAR: 5030.0, BATT_OUT: 105.0})
        key = {"yaml without an id": "source", "powercalc": "source_entity"}.get(how)
        if key:
            states[BATT_OUT].attributes[key] = "sensor.battery_power"
            calc = _calc(states)
            _go(calc, states, _at(0, 30), **{SOLAR: 5000.0, BATT_OUT: 100.0})
            states[BATT_OUT].attributes[key] = "sensor.battery_power"
            _go(calc, states, _at(18, 0), **{SOLAR: 5030.0})
        assert _solar(calc) == 0.0

    def test_two_batteries_add_up(self):
        states = {}
        calc = _calc(states, outs=(BATT_OUT, BATT_OUT_2))
        _go(calc, states, _at(0, 30),
            **{SOLAR: 5000.0, BATT_OUT: 100.0, BATT_OUT_2: 50.0})
        _go(calc, states, _at(18, 0),
            **{SOLAR: 5030.0, BATT_OUT: 103.0, BATT_OUT_2: 52.0})
        assert _solar(calc) == pytest.approx(25.0)

    def test_two_inverters_share_what_is_held_back(self):
        states = {}
        calc = _calc(states, solar=(SOLAR, SOLAR_2))
        _go(calc, states, _at(0, 30),
            **{SOLAR: 5000.0, SOLAR_2: 2000.0, BATT_OUT: 100.0})
        _go(calc, states, _at(18, 0),
            **{SOLAR: 5030.0, SOLAR_2: 2010.0, BATT_OUT: 108.0})
        assert _solar(calc) == pytest.approx(32.0)
        bl = calc._solar_counter_baselines
        assert bl["base"][SOLAR] == pytest.approx(5006.0)
        assert bl["base"][SOLAR_2] == pytest.approx(2002.0)

    def test_more_out_than_the_step_credits_nothing(self):
        states = {}
        calc = _calc(states)
        _go(calc, states, _at(0, 30), **{SOLAR: 5000.0, BATT_OUT: 100.0})
        _go(calc, states, _at(18, 0), **{SOLAR: 5003.0, BATT_OUT: 110.0})
        assert _solar(calc) == 0.0

    def test_a_discharge_counter_not_read_yet_waits(self):
        """A restart brings the battery up after the inverter: wait, and do
        not let home balance against a solar row without the gap."""
        states = {}
        calc = _calc(states)
        _go(calc, states, _at(0, 30), **{SOLAR: 5000.0, BATT_OUT: 100.0})
        _go(calc, states, _at(18, 0), **{SOLAR: 5030.0, BATT_OUT: None})
        assert _solar(calc) == 0.0
        assert "solar" not in calc._counter_backed
        _go(calc, states, _at(18, 0, 10), **{SOLAR: 5030.0, BATT_OUT: 105.0})
        assert _solar(calc) == pytest.approx(25.0)
        assert "solar" in calc._counter_backed
        # credited once: the next dark cycles add nothing
        _go(calc, states, _at(18, 0, 20), **{SOLAR: 5030.01, BATT_OUT: 105.01})
        assert _solar(calc) == pytest.approx(25.0)

    def test_what_the_integrator_adds_while_waiting_is_not_counted_twice(self):
        """(review) Restart at 09:00 CEST, the battery counter loads at 09:30;
        the power integral adds 2.0 kWh meanwhile. The step from 02:30 holds
        those 2.0 kWh too: 5.0 was booked, the truth is 3.0."""
        states = {}
        calc = _calc(states)
        _go(calc, states, _at(0, 30), **{SOLAR: 5000.0, BATT_OUT: 100.0})
        _go(calc, states, _at(7, 0), **{SOLAR: 5004.0, BATT_OUT: None})
        assert _solar(calc) == 0.0
        calc._daily_accumulators[f"solar_{DAY}"] = 2.0   # the integral
        _go(calc, states, _at(7, 2), **{SOLAR: 5006.0, BATT_OUT: 103.0})
        assert _solar(calc) == pytest.approx(3.0)

    @pytest.mark.parametrize("first", ["0", "error", ""])
    def test_a_counter_still_loading_waits(self, first):
        """(review round 2) A counter loading reads no number above 0
        (#1043). Taken as a reading, it went back: the day was held back."""
        states = {}
        calc = _calc(states)
        _go(calc, states, _at(0, 30), **{SOLAR: 5000.0, BATT_OUT: 100.0})
        states[BATT_OUT] = SimpleNamespace(
            state=first, attributes={"unit_of_measurement": "kWh"})
        _go(calc, states, _at(18, 0), **{SOLAR: 5030.0})
        assert "solar" not in calc._counter_backed
        _go(calc, states, _at(18, 1), **{SOLAR: 5030.0, BATT_OUT: 105.0})
        assert _solar(calc) == pytest.approx(25.0)

    def test_a_night_charge_from_the_grid_is_held_back(self):
        """(review round 2) #681 says a hybrid's counter passes grid power to
        a car charging at night: 20 kWh of it in the step, the import shows
        it."""
        states = {}
        calc = _calc(states)
        _go(calc, states, _at(0, 30), **{SOLAR: 5000.0, BATT_OUT: 100.0})
        _go(calc, states, _at(18, 0),
            **{SOLAR: 5048.0, BATT_OUT: 103.0, IMPORT: 8443.80})
        assert _solar(calc) == pytest.approx(25.0)

    def test_nothing_credited_keeps_the_anchor_now(self):
        """(review round 2) The wait ran out and all was held back: the
        anchor is the row now, as before, so the rest of the day counts."""
        states = {}
        calc = _calc(states)
        _go(calc, states, _at(0, 30), **{SOLAR: 5000.0, BATT_OUT: 100.0})
        start = _at(7, 0)
        _go(calc, states, start, **{SOLAR: 5004.0, BATT_OUT: None})
        calc._daily_accumulators[f"solar_{DAY}"] = 2.0   # the integral
        _go(calc, states, start + timedelta(seconds=ec.SOLAR_GAP_WAIT_S),
            **{SOLAR: 5006.0})
        assert _solar(calc) == pytest.approx(2.0)
        calc._daily_accumulators[f"solar_{DAY}"] = 3.0   # +1 integrated
        _go(calc, states, _at(8, 30), **{SOLAR: 5009.0, BATT_OUT: 103.0})
        assert _solar(calc) == pytest.approx(5.0)   # 2.0 + the counter's 3

    def test_the_wait_ends(self):
        """(review) A discharge counter that never comes back holds the day
        only for the Repair wait; then the step is kept out."""
        states = {}
        calc = _calc(states)
        _go(calc, states, _at(0, 30), **{SOLAR: 5000.0, BATT_OUT: 100.0})
        start = _at(18, 0)
        _go(calc, states, start, **{SOLAR: 5030.0, BATT_OUT: None})
        _go(calc, states, start + timedelta(
            seconds=ec.SOLAR_GAP_WAIT_S - 10), **{SOLAR: 5030.0})
        assert "solar" not in calc._counter_backed
        _go(calc, states, start + timedelta(
            seconds=ec.SOLAR_GAP_WAIT_S), **{SOLAR: 5030.0})
        assert "solar" in calc._counter_backed
        assert _solar(calc) == 0.0
        assert calc._solar_gap_wait_since is None
        # from there on SEM watches the night again
        _go(calc, states, start + timedelta(
            seconds=ec.SOLAR_GAP_WAIT_S + 10), **{SOLAR: 5030.2})
        assert _solar(calc) == 0.0

    def test_the_wait_has_no_clock_before_ha_runs(self):
        states = {}
        from homeassistant.core import CoreState
        calc = _calc(states)
        calc._hass.state = CoreState.starting   # is_running is true here too
        _go(calc, states, _at(0, 30), **{SOLAR: 5000.0, BATT_OUT: 100.0})
        _go(calc, states, _at(18, 0), **{SOLAR: 5030.0, BATT_OUT: None})
        _go(calc, states, _at(19, 0), **{SOLAR: 5030.0})
        assert "solar" not in calc._counter_backed
        assert calc._solar_gap_wait_since is None
        calc._hass.state = CoreState.running
        _go(calc, states, _at(19, 0, 10), **{SOLAR: 5030.0, BATT_OUT: 105.0})
        assert _solar(calc) == pytest.approx(25.0)

    def test_a_discharge_counter_dark_before_the_outage_bounds_from_further_back(self):
        """The last read before the outage could not read the battery: the
        older output is kept, a larger bound, never a smaller one."""
        states = {}
        calc = _calc(states)
        _go(calc, states, _at(0, 20), **{SOLAR: 5000.0, BATT_OUT: 99.0})
        _go(calc, states, _at(0, 30), **{SOLAR: 5000.0, BATT_OUT: None})
        _go(calc, states, _at(18, 0), **{SOLAR: 5030.0, BATT_OUT: 104.0})
        assert _solar(calc) == pytest.approx(25.0)

    @pytest.mark.parametrize("case", ["went back", "set changed", "never read"])
    def test_an_unknown_bound_holds_all_back(self, case):
        """Nothing proves how much was the battery: as the sun gate did."""
        states = {}
        calc = _calc(states)
        _go(calc, states, _at(0, 30), **{SOLAR: 5000.0,
                                         BATT_OUT: None if case == "never read" else 100.0})
        if case == "never read":
            calc._solar_counter_baselines["span"]["outs"] = None
        if case == "set changed":
            calc.configure_meter_counters(
                calc._hass, {"battery_discharge": [BATT_OUT_2]}, True)
            states[BATT_OUT_2] = _kwh(103.0)
        out = 90.0 if case == "went back" else 103.0
        _go(calc, states, _at(18, 0), **{SOLAR: 5030.0, BATT_OUT: out})
        assert _solar(calc) == 0.0

    def test_a_solar_counter_reset_across_the_gap_credits_nothing(self):
        states = {}
        calc = _calc(states)
        _go(calc, states, _at(0, 30), **{SOLAR: 5000.0, BATT_OUT: 100.0})
        _go(calc, states, _at(18, 0), **{SOLAR: 20.0, BATT_OUT: 105.0})
        assert _solar(calc) == 0.0


@pytest.mark.unit
class TestNoRecordJudgesAsBefore:

    def test_a_store_from_before(self):
        states = {}
        calc = _calc(states)
        _go(calc, states, _at(0, 30), **{SOLAR: 5000.0, BATT_OUT: 100.0})
        calc._solar_counter_baselines.pop("span")
        _go(calc, states, _at(18, 0), **{SOLAR: 5030.0, BATT_OUT: 105.0})
        assert _solar(calc) == 0.0

    @pytest.mark.parametrize("field,value", [
        ("at", "not a time"), ("at", "2026-10-12T00:30:00"),   # no zone
        ("turn", None), ("read", "sensor.x"), ("at", 17),
        ("at", "2026-13-12T00:30:00+00:00"), ("turn", "2026-10-12T05:40:00+99:00"),
    ])
    def test_a_damaged_record(self, field, value):
        states = {}
        calc = _calc(states)
        _go(calc, states, _at(0, 30), **{SOLAR: 5000.0, BATT_OUT: 100.0})
        calc._solar_counter_baselines["span"][field] = value
        _go(calc, states, _at(18, 0), **{SOLAR: 5030.0, BATT_OUT: 105.0})
        assert _solar(calc) == 0.0

    def test_a_sun_without_times_judges_by_the_read(self):
        """The last read found no next sunrise or sunset: no turn to pass."""
        states = {SOLAR: _kwh(5000.0), BATT_OUT: _kwh(100.0),
                  "sun.sun": SimpleNamespace(state="below_horizon", attributes={})}
        calc = _calc(states)
        calc._reconcile_solar_energy(DAY, MONTH, YEAR, _at(0, 30))
        assert calc._solar_counter_baselines["span"]["turn"] is None
        _go(calc, states, _at(18, 0), **{SOLAR: 5030.0, BATT_OUT: 105.0})
        assert _solar(calc) == 0.0

    def test_no_sun_entity_still_fails_open(self):
        """#681's rule: no ``sun.sun`` → the counter is credited."""
        states = {SOLAR: _kwh(5000.0), BATT_OUT: _kwh(100.0)}
        calc = _calc(states)
        calc._reconcile_solar_energy(DAY, MONTH, YEAR, _at(0, 30))
        states[SOLAR], states[BATT_OUT] = _kwh(5030.0), _kwh(105.0)
        calc._reconcile_solar_energy(DAY, MONTH, YEAR, _at(18, 0))
        assert _solar(calc) == pytest.approx(30.0)

    def test_a_counter_dark_at_the_last_cycle_is_judged_as_before(self):
        """Only counters read at the last cycle start the span there."""
        states = {}
        calc = _calc(states, solar=(SOLAR, SOLAR_2))
        _go(calc, states, _at(0, 30),
            **{SOLAR: 5000.0, SOLAR_2: None, BATT_OUT: 100.0})
        _go(calc, states, _at(18, 0),
            **{SOLAR: 5030.0, SOLAR_2: 2010.0, BATT_OUT: 105.0})
        assert _solar(calc) == pytest.approx(25.0)
        assert calc._solar_counter_baselines["base"][SOLAR_2] == 2010.0

    def test_a_new_day_starts_without_a_span(self):
        states = {}
        calc = _calc(states)
        _go(calc, states, _at(0, 30), **{SOLAR: 5000.0, BATT_OUT: 100.0})
        _sun(states, _at(18, 0))
        states[SOLAR], states[BATT_OUT] = _kwh(5030.0), _kwh(105.0)
        tomorrow = DAY + timedelta(days=1)
        calc._reconcile_solar_energy(tomorrow, MONTH, YEAR,
                                     _at(18, 0) + timedelta(days=1))
        assert calc._daily_accumulators.get(f"solar_{tomorrow}", 0.0) == 0.0


@pytest.mark.unit
class TestTheRecord:

    def test_the_span_survives_the_store(self):
        """The restart is the whole point: the record goes through JSON."""
        states = {}
        calc = _calc(states)
        _prod_night(calc, states)
        stored = json.loads(json.dumps(calc.get_state()))
        fresh = _calc(states)
        fresh.restore_state(stored)
        _go(fresh, states, _at(18, 0),
            **{SOLAR: 23863.24, BATT_OUT: 7110.13, IMPORT: 8424.03})
        assert _solar(fresh) == pytest.approx(24.71)

    def test_the_record_holds_the_read(self):
        states = {}
        calc = _calc(states)
        _go(calc, states, _at(0, 30), **{SOLAR: 5000.0, BATT_OUT: 100.0})
        span = calc._solar_counter_baselines["span"]
        assert span == {
            "at": _at(0, 30).isoformat(), "turn": SUNRISE.isoformat(),
            "outs": {BATT_OUT: 100.0, IMPORT: 8423.80}, "row": 0.0,
            "read": [SOLAR],
        }
        _go(calc, states, _at(12, 0), **{SOLAR: 5001.0, BATT_OUT: 100.0})
        assert calc._solar_counter_baselines["span"]["turn"] == SUNSET.isoformat()

    def test_the_turn_reads_a_published_time(self):
        """HA publishes the times as ISO strings; a datetime works too."""
        states = {"sun.sun": SimpleNamespace(state="below_horizon", attributes={
            "next_rising": SUNRISE, "next_setting": SUNSET.isoformat()})}
        calc = _calc(states)
        assert calc._sun_turn() == SUNRISE
        states["sun.sun"].attributes["next_rising"] = "garbage"
        assert calc._sun_turn() == SUNSET
        states["sun.sun"] = SimpleNamespace(state="below_horizon", attributes=None)
        assert calc._sun_turn() is None
        states.pop("sun.sun")
        assert calc._sun_turn() is None


@pytest.mark.unit
class TestThroughTheCycle:
    """``calculate_energy`` hands the cycle's time to the solar check."""

    def test_the_cycle_passes_its_time(self):
        from homeassistant.util import dt as dt_util

        from custom_components.solar_energy_management.coordinator.types import (
            PowerReadings,
        )
        from custom_components.solar_energy_management.utils.time_manager import (
            TimeManager,
        )
        from freezegun import freeze_time

        from homeassistant.helpers import entity_registry as er

        states = {}
        hass = Mock()
        hass.data = {}
        hass.states.get = lambda eid: states.get(eid)
        registry = Mock()
        registry.async_get = lambda eid: None
        er_get, er.async_get = er.async_get, lambda h: registry
        before = dt_util.DEFAULT_TIME_ZONE
        dt_util.set_default_time_zone(timezone.utc)
        try:
            calc = EnergyCalculator({"update_interval": 10, "system_size_kwp": 10},
                                    TimeManager(hass))
            calc.configure_solar_counters(hass, [SOLAR], True)
            calc.configure_meter_counters(hass, {"battery_discharge": [BATT_OUT]}, True)

            def cycle(when, solar, out):
                states[SOLAR], states[BATT_OUT] = _kwh(solar), _kwh(out)
                _sun(states, when)
                with freeze_time(when):
                    calc._last_update = None
                    p = PowerReadings()
                    p.calculate_derived()
                    calc.calculate_energy(p)

            cycle(_at(0, 30), 23832.86, 7104.69)
            cycle(_at(18, 0), 23863.24, 7110.13)
        finally:
            dt_util.set_default_time_zone(before)
            er.async_get = er_get
        assert _solar(calc) == pytest.approx(30.38 - 5.44)
