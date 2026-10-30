"""#1024 — a session's energy is the charger's own meter.

PROD, last ten days to 02.10.2026, KEBA's session meter vs what SEM
recorded from its flow allocations: 10.73 vs 7.36, 11.13 vs 9.55, 4.07 vs
3.55, 2.35 vs 1.98, 1.59 vs 1.43, 2.13 vs 1.62 kWh. The flows give the
split; the meter gives the total.
"""
import pytest

from custom_components.solar_energy_management.coordinator.session_energy import (
    SOURCE_CHARGER_METER, SOURCE_ESTIMATE, SOURCE_LIFETIME_DELTA, seed_from_legacy, step,
)

PROD = [(10.73, 7.36), (11.13, 9.55), (4.07, 3.55), (2.35, 1.98), (1.59, 1.43), (2.13, 1.62)]
CYCLES = 100
H = 10 / 3600


def _run(meter_total, flow_total, *, session_meter=True, lifetime_start=None,
         solar_frac=0.6, power_w=None, gaps=()):
    """A session of CYCLES cycles; the flows cover ``flow_total`` and the
    meter rises to ``meter_total``."""
    meter = {}
    res = None
    for i in range(1, CYCLES + 1):
        f = flow_total / CYCLES
        reading = meter_total * i / CYCLES
        sm = reading if session_meter and i not in gaps else None
        lt = (lifetime_start + reading) if lifetime_start is not None and i not in gaps else None
        res = step(meter, solar_kwh=f * solar_frac, grid_kwh=f * (1 - solar_frac),
                   battery_kwh=0.0, cost=f * (1 - solar_frac) * 0.30,
                   power_w=power_w if power_w is not None else meter_total * 1000 / (CYCLES * H),
                   hours=H, session_meter_kwh=sm, lifetime_meter_kwh=lt, import_rate=0.30)
    return res, meter


class TestProdSessions:
    @pytest.mark.parametrize("keba,sem", PROD)
    def test_the_meter_total_is_recorded_not_the_flow_sum(self, keba, sem):
        res, _ = _run(keba, sem)
        assert res.energy_kwh == pytest.approx(keba, abs=0.005)
        assert res.source == SOURCE_CHARGER_METER

    def test_flows_covering_69_percent_of_the_meter(self):
        res, _ = _run(10.73, 10.73 * 0.69)
        assert res.energy_kwh == pytest.approx(10.73, abs=0.005)
        # the split keeps the flows' proportions, scaled up to the meter
        assert res.solar_kwh == pytest.approx(10.73 * 0.6, abs=0.01)
        assert res.grid_kwh == pytest.approx(10.73 * 0.4, abs=0.01)
        # cost follows the scaled split
        assert res.cost == pytest.approx(10.73 * 0.4 * 0.30, abs=0.01)

    def test_the_ten_days_add_up_to_the_lifetime_counter(self):
        total = sum(_run(k, s)[0].energy_kwh for k, s in PROD)
        assert total == pytest.approx(32.0, abs=0.05)


class TestLifetimeDelta:
    def test_without_a_session_meter_the_lifetime_rise_is_the_total(self):
        res, _ = _run(4.07, 3.55, session_meter=False, lifetime_start=5123.4)
        assert res.energy_kwh == pytest.approx(4.07, abs=0.005)
        assert res.source == SOURCE_LIFETIME_DELTA

    def test_a_lifetime_reset_mid_session_keeps_what_was_counted(self):
        meter = {}
        kw = dict(solar_kwh=0.1, grid_kwh=0.0, battery_kwh=0.0, cost=0.0, power_w=0.0,
                  hours=H, session_meter_kwh=None, import_rate=0.3)
        step(meter, lifetime_meter_kwh=100.0, **kw)
        step(meter, lifetime_meter_kwh=103.0, **kw)    # +3
        step(meter, lifetime_meter_kwh=0.2, **kw)      # reset
        res = step(meter, lifetime_meter_kwh=1.2, **kw)   # +1
        assert res.energy_kwh == pytest.approx(4.0)


class TestSessionMeter:
    def test_an_unavailable_reading_keeps_the_last_good_value(self):
        res, _ = _run(2.13, 1.62, gaps=set(range(60, 101)))
        # the meter last read at cycle 59
        assert res.energy_kwh == pytest.approx(2.13 * 59 / CYCLES, abs=0.005)
        assert res.source == SOURCE_CHARGER_METER

    def test_a_session_meter_reset_mid_session_adds_the_segments(self):
        meter = {}
        kw = dict(solar_kwh=0.1, grid_kwh=0.0, battery_kwh=0.0, cost=0.0, power_w=0.0,
                  hours=H, lifetime_meter_kwh=None, import_rate=0.3)
        for v in (0.0, 1.0, 2.5, 0.1, 0.9):
            res = step(meter, session_meter_kwh=v, **kw)
        assert res.energy_kwh == pytest.approx(3.4)

    def test_last_sessions_stale_value_is_not_this_session(self):
        meter = {}
        kw = dict(solar_kwh=0.1, grid_kwh=0.0, battery_kwh=0.0, cost=0.0, power_w=3600.0,
                  hours=1 / 3600, lifetime_meter_kwh=None, import_rate=0.3)
        res = step(meter, session_meter_kwh=10.73, **kw)    # still last session's
        assert res.source == SOURCE_ESTIMATE
        res = step(meter, session_meter_kwh=0.0, **kw)      # the charger resets
        res = step(meter, session_meter_kwh=0.4, **kw)
        assert res.source == SOURCE_CHARGER_METER
        assert res.energy_kwh == pytest.approx(0.4)


class TestEstimate:
    def test_no_meter_integrates_this_chargers_power_not_the_flows(self):
        res, _ = _run(2.35, 1.98, session_meter=False, power_w=2.35 * 1000 / (CYCLES * H))
        assert res.energy_kwh == pytest.approx(2.35, abs=0.005)
        assert res.source == SOURCE_ESTIMATE

    def test_no_flows_at_all_is_booked_as_grid(self):
        meter = {}
        for v in (0.0, 2.0):
            res = step(meter, solar_kwh=0, grid_kwh=0, battery_kwh=0, cost=0, power_w=0, hours=H,
                       session_meter_kwh=v, lifetime_meter_kwh=None, import_rate=0.25)
        assert (res.grid_kwh, res.solar_kwh, res.cost) == (2.0, 0.0, 0.5)


class TestLegacySeed:
    def test_a_restored_session_without_meter_state_never_shrinks(self):
        meter = {}
        seed_from_legacy(meter, 5.0, 3.0, 2.0, 0.0, 0.6)
        res = step(meter, solar_kwh=0.0, grid_kwh=0.1, battery_kwh=0.0, cost=0.03, power_w=0,
                   hours=H, session_meter_kwh=None, lifetime_meter_kwh=800.0, import_rate=0.3)
        assert res.energy_kwh == pytest.approx(5.0)
        res = step(meter, solar_kwh=0.0, grid_kwh=0.1, battery_kwh=0.0, cost=0.03, power_w=0,
                   hours=H, session_meter_kwh=None, lifetime_meter_kwh=801.0, import_rate=0.3)
        assert res.energy_kwh == pytest.approx(6.0)
        assert res.source == SOURCE_LIFETIME_DELTA
