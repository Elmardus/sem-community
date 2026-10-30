"""Live capture: Home Assistant's REAL tesla_wall_connector component, set up
the way core's own tests do it (``tests/components/tesla_wall_connector/
conftest.py`` at 2026.8.2: the client's four calls patched with its own
``Version`` / ``Vitals`` / ``Lifetime`` / ``WifiStatus`` objects).

Run by hand with the client library on the path (it is not a SEM test
dependency):

    pip install --target /tmp/capdeps tesla-wall-connector==1.2.0 --no-deps
    PYTHONPATH=/tmp/capdeps:<CI layout> python -m pytest -p no:cacheprovider \\
        -o python_files='live_*.py' <this file>
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from dump import capture_from_hass, write_capture  # noqa: E402

pytest.importorskip("tesla_wall_connector")


def _vitals():
    from tesla_wall_connector.wall_connector import Vitals
    m = MagicMock(auto_spec=Vitals)
    # core conftest values, a car plugged in and charging
    m.evse_state = 11
    m.handle_temp_c = 25.51
    m.pcba_temp_c = 30.5
    m.mcu_temp_c = 42.0
    m.grid_v = 230.15
    m.grid_hz = 50.021
    m.voltageA_v = 230.1
    m.voltageB_v = 231
    m.voltageC_v = 232.1
    m.currentA_a = 10
    m.currentB_a = 11.1
    m.currentC_a = 12
    m.vehicle_current_a = 32
    m.total_power_w = 7650.3
    m.session_energy_wh = 1234.56
    m.contactor_closed = True
    m.vehicle_connected = True
    return m


def _lifetime():
    from tesla_wall_connector.wall_connector import Lifetime
    m = MagicMock(auto_spec=Lifetime)
    m.energy_wh = 988022
    return m


def _wifi():
    from tesla_wall_connector.wall_connector import WifiStatus
    m = MagicMock(auto_spec=WifiStatus)
    m.signal_strength = 100
    m.wifi_rssi = -50
    m.rssi = 30
    m.snr = 25
    m.wifi_snr = 25
    return m


async def test_capture(hass):
    from homeassistant.const import CONF_HOST
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from tesla_wall_connector.wall_connector import Version

    entry = MockConfigEntry(domain="tesla_wall_connector",
                            data={CONF_HOST: "1.2.3.4"})
    entry.add_to_hass(hass)
    version = Version({"serial_number": "abc123", "part_number": "part_123",
                       "firmware_version": "1.2.3"})
    with patch("tesla_wall_connector.WallConnector.async_get_version",
               return_value=version), \
         patch("tesla_wall_connector.WallConnector.async_get_vitals",
               return_value=_vitals()), \
         patch("tesla_wall_connector.WallConnector.async_get_lifetime",
               return_value=_lifetime()), \
         patch("tesla_wall_connector.WallConnector.async_get_wifi_status",
               return_value=_wifi()):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    import homeassistant
    cap = capture_from_hass(hass, "tesla_wall_connector", source={
        "kind": "live-load",
        "repo": "home-assistant/core",
        "tag": homeassistant.const.__version__,
        "client": "tesla-wall-connector==1.2.0",
        "data": "core tests/components/tesla_wall_connector/conftest.py "
                "values; evse_state 11 (charging)",
    })
    assert cap["entities"], "the integration created nothing"
    write_capture(cap, HERE.parent / "captures" / "tesla_wall_connector.json")
