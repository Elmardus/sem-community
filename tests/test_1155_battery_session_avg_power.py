"""#1155 — the battery session average spiked to hundreds of MW at every start.

A GoodWe at ~5.5 kW read 548,700,000 W on the first update of a session,
then 11000, 8250, 7340 … — exactly P × (n+1)/n. The session started at
now() while the same cycle booked a whole interval of energy, so the first
average divided an interval of kWh by microseconds, and every later one ran
one interval of energy ahead of the duration. The session now starts one
interval back, so duration and energy span the same time.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from custom_components.solar_energy_management.coordinator.types import (
    BatterySessionData, PowerFlows, PowerReadings,
)

_MOD = "custom_components.solar_energy_management.coordinator.coordinator"
_T0 = datetime(2026, 10, 17, 19, 0, 36, tzinfo=timezone.utc)


def _coordinator(interval_s=10):
    from custom_components.solar_energy_management.coordinator.coordinator import SEMCoordinator

    with patch.object(SEMCoordinator, "__init__", return_value=None):
        coord = SEMCoordinator.__new__(SEMCoordinator)
    coord.config = {"update_interval": interval_s}
    coord.update_interval = timedelta(seconds=interval_s)
    coord._battery_session = BatterySessionData()
    coord._battery_session_idle_count = 0
    coord._battery_session_opposite_count = 0
    coord._energy_calculator = type("Calc", (), {"_import_rate": 0.30})()
    return coord


def _run(coord, cycles, *, discharge_w=0.0, charge_w=0.0, interval_s=10, jitter_s=0.003):
    """One update per interval; the clock reads a few ms after each tick."""
    power = PowerReadings(battery_charge_power=charge_w, battery_discharge_power=discharge_w)
    flows = PowerFlows(solar_to_battery=charge_w)
    seen = []
    for n in range(cycles):
        now = _T0 + timedelta(seconds=n * interval_s + jitter_s)
        with patch(f"{_MOD}.dt_util.now", return_value=now):
            coord._update_battery_session_tracking(power, flows)
        seen.append(coord._battery_session.avg_power_w)
    return seen


class TestTheFirstUpdateOfASession:
    def test_reads_the_battery_power_not_megawatts(self):
        coord = _coordinator()
        first = _run(coord, 1, discharge_w=5500)[0]
        assert abs(first - 5500) < 5500 * 0.01

    def test_a_charge_session_too(self):
        coord = _coordinator()
        first = _run(coord, 1, charge_w=3000)[0]
        assert abs(first - 3000) < 3000 * 0.01


class TestEveryLaterUpdate:
    def test_steady_power_reads_steady_from_the_start(self):
        """The reporter's sequence was 2×, 1.5×, 1.33×, 1.25× of the power."""
        coord = _coordinator()
        seen = _run(coord, 30, discharge_w=5500)
        for avg in seen:
            assert abs(avg - 5500) < 5500 * 0.01, seen

    def test_duration_covers_the_booked_energy(self):
        coord = _coordinator()
        _run(coord, 6, discharge_w=6000)
        s = coord._battery_session
        # 6 cycles × 10 s of 6 kW = 0.1 kWh over one minute
        assert abs(s.energy_kwh - 0.1) < 1e-9
        assert abs(s.duration_minutes - 1.0) < 0.01

    def test_a_slower_update_interval_holds_too(self):
        coord = _coordinator(interval_s=30)
        seen = _run(coord, 10, discharge_w=4000, interval_s=30)
        for avg in seen:
            assert abs(avg - 4000) < 4000 * 0.01, seen


class TestTheStartTime:
    def test_starts_one_interval_before_the_first_update(self):
        coord = _coordinator()
        _run(coord, 1, discharge_w=5500, jitter_s=0)
        start = datetime.fromisoformat(coord._battery_session.start_time)
        assert start == _T0 - timedelta(seconds=10)

    def test_a_running_session_keeps_its_start(self):
        coord = _coordinator()
        _run(coord, 3, discharge_w=5500, jitter_s=0)
        start = datetime.fromisoformat(coord._battery_session.start_time)
        assert start == _T0 - timedelta(seconds=10)
