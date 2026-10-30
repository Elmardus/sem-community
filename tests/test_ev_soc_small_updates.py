"""Small real SOC changes must correct the energy-based stopping estimate."""
from datetime import datetime, timedelta

import pytest

from custom_components.solar_energy_management.coordinator.ev_soc_need import (
    soc_remaining_need,
)
from custom_components.solar_energy_management.coordinator.ev_taper_detector import (
    EVTaperDetector,
)


def _deliver_energy(detector, start):
    """Deliver 0.9 kWh at 5.4 kW, sampled every ten seconds."""
    for step in range(61):
        detector.update(5400, 28, True, start + timedelta(seconds=step * 10))
    return start + timedelta(minutes=10)


@pytest.mark.parametrize("fresh_soc", [69.0, 68.6, 68.4, 68.0])
def test_small_sensor_change_corrects_estimate_and_virtual_fallback(fresh_soc):
    detector = EVTaperDetector({"ev_battery_capacity_kwh": 64})
    detector.get_virtual_soc(68.5)
    _deliver_energy(detector, datetime(2026, 10, 24, 6))
    assert detector.energy_accounted_soc() > 69.0

    detector.get_virtual_soc(fresh_soc)

    assert detector.energy_accounted_soc() == pytest.approx(fresh_soc)
    assert detector.get_virtual_soc(None) == pytest.approx(fresh_soc)


def test_successive_half_point_changes_each_correct_the_estimate():
    detector = EVTaperDetector({"ev_battery_capacity_kwh": 64})
    detector.get_virtual_soc(68.5)
    timestamp = datetime(2026, 10, 24, 6)

    for fresh_soc in [69.0, 69.5, 70.0, 70.5, 71.0, 71.5]:
        timestamp = _deliver_energy(detector, timestamp)
        detector.get_virtual_soc(fresh_soc)
        assert detector.energy_accounted_soc() == pytest.approx(fresh_soc)


def test_identical_sensor_poll_keeps_energy_delivered_since_anchor():
    detector = EVTaperDetector({"ev_battery_capacity_kwh": 64})
    detector.get_virtual_soc(68.5)
    _deliver_energy(detector, datetime(2026, 10, 24, 6))
    before_poll = detector.energy_accounted_soc()

    detector.get_virtual_soc(68.5)

    assert before_poll > 69.0
    assert detector.energy_accounted_soc() == before_poll


def test_half_point_correction_reopens_need_after_estimate_stop():
    detector = EVTaperDetector({"ev_battery_capacity_kwh": 64})
    detector.get_virtual_soc(79.5)
    _deliver_energy(detector, datetime(2026, 10, 24, 6))
    assert soc_remaining_need(
        80, 79.5, detector.energy_accounted_soc(), 64
    ).effective_kwh == 0

    # The fresh car reading contradicts the estimate that ended the charge.
    detector.get_virtual_soc(79.0)

    assert soc_remaining_need(
        80, 79.0, detector.energy_accounted_soc(), 64
    ).effective_kwh == pytest.approx(0.64)
