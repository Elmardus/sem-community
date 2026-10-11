"""(#1051) rdehuyss's first install, 2.2.0-beta.15.

A bed-heating smart plug in the Energy Dashboard's individual devices
(``sensor.bedverwarming_device_power``) became the EV: the old rule matched
the letters "ev" anywhere in the entity id, and "device" has them. A home
with no car got an EV charger.

The rule now: an Energy Dashboard device is the EV only when the crawler
knows its integration as a charger (``known_charger_domains``). Names say
nothing.
"""
from __future__ import annotations

import pytest
from homeassistant.helpers import entity_registry as er

from custom_components.solar_energy_management.ha_energy_reader import (
    EnergyDashboardConfig,
    _extract_ev_from_devices,
    _parse_prefs,
)

# His Energy Dashboard, as far as the diagnostics show it.
PREFS = {
    "energy_sources": [
        {"type": "solar",
         "stat_energy_from": "sensor.solis_s6_eh3p_pv_total_energy_generation",
         "stat_rate": "sensor.solis_s6_eh3p_smartport_old_invertes_power"},
        {"type": "grid", "flow_from": [
            {"stat_energy_from": "sensor.bluetooth_proxy_garage_energy_consumed_tariff_1"},
            {"stat_energy_from": "sensor.bluetooth_proxy_garage_energy_consumed_tariff_2"}],
         "flow_to": [
            {"stat_energy_to": "sensor.bluetooth_proxy_garage_energy_produced_tariff_1"},
            {"stat_energy_to": "sensor.bluetooth_proxy_garage_energy_produced_tariff_2"}]},
        {"type": "battery",
         "stat_energy_to": "sensor.solis_s6_eh3p_today_battery_charge_energy",
         "stat_energy_from": "sensor.solis_s6_eh3p_today_battery_discharge_energy"},
    ],
    "device_consumption": [
        {"stat_consumption": "sensor.bedverwarming_device_energy",
         "stat_rate": "sensor.bedverwarming_device_power"},
    ],
}


def _register(hass, entity_id, platform):
    reg = er.async_get(hass)
    domain, object_id = entity_id.split(".", 1)
    reg.async_get_or_create(domain, platform, object_id,
                            suggested_object_id=object_id)


async def test_a_smart_plug_named_device_is_not_the_ev(hass):
    """His exact case: a ZHA plug whose ids contain "device"."""
    for eid in ("sensor.bedverwarming_device_energy",
                "sensor.bedverwarming_device_power"):
        _register(hass, eid, "zha")
    config = _parse_prefs(hass, PREFS, quiet=True)
    assert config.has_ev is False
    assert config.ev_power is None and config.ev_energy is None


async def test_a_charger_integration_device_is_the_ev(hass):
    """Not vacuous: a device the crawler knows as a charger still counts."""
    _register(hass, "sensor.keba_p30_total_energy", "keba")
    _register(hass, "sensor.keba_p30_charging_power", "keba")
    prefs = dict(PREFS, device_consumption=[
        {"stat_consumption": "sensor.keba_p30_total_energy",
         "stat_rate": "sensor.keba_p30_charging_power"}])
    config = _parse_prefs(hass, prefs, quiet=True)
    assert config.has_ev is True
    assert config.ev_energy == "sensor.keba_p30_total_energy"
    assert config.ev_power == "sensor.keba_p30_charging_power"


@pytest.mark.parametrize("name", [
    "sensor.ev_charger_energy",       # a name alone proves nothing …
    "sensor.wallbox_energy",
    "sensor.dryer_device_energy",
])
def test_without_the_registry_a_name_proves_nothing(name):
    """No registry to ask → no EV. A wrong EV drives a plug as a car."""
    config = EnergyDashboardConfig()
    _extract_ev_from_devices([{"stat_consumption": name}], config)
    assert config.has_ev is False
