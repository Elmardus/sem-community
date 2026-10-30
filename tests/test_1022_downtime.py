"""#1022 — minutes the solar input was dark today.

The reader already knows, per cycle, that every solar read came back
dark. This adds the seconds up over the day, resets at midnight, and
publishes minutes on the readings.
"""
from unittest.mock import MagicMock

from custom_components.solar_energy_management.coordinator.sensor_reader import (
    SensorReader,
)
from custom_components.solar_energy_management.coordinator.types import (
    PowerReadings,
)


def _reader():
    r = SensorReader.__new__(SensorReader)
    r._last_cycle_mono = None
    r._last_solar_dark = False
    r._solar_dark_s_today = 0.0
    return r


class TestAccumulator:
    def test_the_first_cycle_counts_nothing(self):
        r = _reader()
        r._last_solar_dark = True
        r._accrue_solar_downtime(1000.0)
        assert r._solar_dark_s_today == 0.0

    def test_a_dark_cycle_adds_the_time_since_the_last_cycle(self):
        r = _reader()
        r._accrue_solar_downtime(1000.0)
        r._last_solar_dark = True
        r._accrue_solar_downtime(1030.0)
        r._accrue_solar_downtime(1060.0)
        assert r._solar_dark_s_today == 60.0

    def test_a_live_cycle_adds_nothing(self):
        r = _reader()
        r._accrue_solar_downtime(1000.0)
        r._last_solar_dark = False
        r._accrue_solar_downtime(1030.0)
        assert r._solar_dark_s_today == 0.0

    def test_midnight_resets(self):
        r = _reader()
        r._accrue_solar_downtime(1000.0)
        r._last_solar_dark = True
        r._accrue_solar_downtime(1030.0)
        r.reset_solar_downtime()
        assert r._solar_dark_s_today == 0.0

    def test_a_short_flap_still_counts_its_cycle(self):
        r = _reader()
        r._accrue_solar_downtime(1000.0)
        r._last_solar_dark = True
        r._accrue_solar_downtime(1010.0)
        r._last_solar_dark = False
        r._accrue_solar_downtime(1020.0)
        assert r._solar_dark_s_today == 10.0

    def test_a_reader_built_without_init_does_not_crash(self):
        r = SensorReader.__new__(SensorReader)
        r._accrue_solar_downtime(1000.0)
        assert r._solar_dark_s_today == 0.0


class TestReadings:
    def test_the_field_defaults_to_zero(self):
        assert PowerReadings().solar_downtime_min_today == 0.0

    def test_read_power_publishes_minutes(self):
        hass = MagicMock()
        hass.states.get.return_value = None          # every sensor dark
        reader = SensorReader(hass, {
            "solar_production_sensor": "sensor.solar",
            "grid_power_sensor": "sensor.grid",
        })
        reader._sign_vote_warmup = 0
        first = reader.read_power()
        assert first.solar_power_unavailable is True
        assert first.solar_downtime_min_today == 0.0
        reader._last_cycle_mono -= 120.0               # two minutes ago
        second = reader.read_power()
        assert 2.0 <= second.solar_downtime_min_today < 2.1
