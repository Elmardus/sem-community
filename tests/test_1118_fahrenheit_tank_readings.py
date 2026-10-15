"""#1118 — the boiler and the heat pump read a °F tank as °F, not °C.

``get_current_temperature`` on both controllers did ``float(state)``: 120 °F
read as 120 °C, above every target. A switch boiler never started on sun,
and the Legionella check always found its target reached. Every other
temperature read already went through ``temperature_state_to_celsius`` (a
sensor's own unit) or ``_attribute_temp_c`` (an attribute, in the install's
unit).
"""
from __future__ import annotations

import pytest

from custom_components.solar_energy_management.coordinator.surplus_controller import (
    SurplusController,
)
from custom_components.solar_energy_management.devices.heat_pump_controller import (
    HeatPumpController,
)
from custom_components.solar_energy_management.devices.hot_water_controller import (
    HotWaterController,
)
from custom_components.solar_energy_management.tests.test_1062_hot_water_fields import (
    _Hass,
)

F120_C = (120.0 - 32.0) * 5.0 / 9.0     # 48.9 °C


def _boiler(hass, entity="switch.boiler", sensor="sensor.boiler_temp"):
    return HotWaterController(
        hass=hass, entity_id=entity, temperature_entity_id=sensor,
        solar_target_temp=55.0, rated_power=2000.0)


class TestTheBoilerReadsCelsius:

    def test_a_fahrenheit_sensor(self):
        hass = _Hass(unit="°F")
        dev = _boiler(hass)
        hass.set("sensor.boiler_temp", "120", unit_of_measurement="°F")
        assert dev.get_current_temperature() == pytest.approx(F120_C)

    def test_a_celsius_sensor_on_a_us_install_is_left_alone(self):
        hass = _Hass(unit="°F")
        dev = _boiler(hass)
        hass.set("sensor.boiler_temp", "48.9", unit_of_measurement="°C")
        assert dev.get_current_temperature() == pytest.approx(48.9)

    def test_a_water_heater_attribute_on_a_us_install(self):
        hass = _Hass(unit="°F")
        dev = _boiler(hass, entity="water_heater.tank", sensor=None)
        hass.set("water_heater.tank", "eco", current_temperature=120)
        assert dev.get_current_temperature() == pytest.approx(F120_C)

    def test_metric_installs_read_as_before(self):
        hass = _Hass()
        dev = _boiler(hass, entity="water_heater.tank", sensor=None)
        hass.set("water_heater.tank", "eco", current_temperature=48.0)
        assert dev.get_current_temperature() == pytest.approx(48.0)


@pytest.mark.asyncio
async def test_a_fahrenheit_boiler_starts_on_sun():
    hass = _Hass(unit="°F")
    dev = _boiler(hass)
    dev.record_legionella_cycle()
    sc = SurplusController(hass)
    sc.register_device(dev)
    hass.set("switch.boiler", "off")
    hass.set("sensor.boiler_temp", "120", unit_of_measurement="°F")
    await sc.update(5000.0)
    assert dev.is_active, "120 °F was read as 120 °C, above the 55 °C target"


@pytest.mark.asyncio
async def test_legionella_is_not_marked_done_by_a_fahrenheit_tank():
    hass = _Hass(unit="°F")
    dev = _boiler(hass)
    hass.set("switch.boiler", "off")
    hass.set("sensor.boiler_temp", "120", unit_of_measurement="°F")
    assert await dev.check_legionella_cycle() == "legionella_started"


def test_the_heat_pump_reads_celsius():
    hass = _Hass(unit="°F")
    hp = HeatPumpController(hass, relay1_entity_id="switch.sg1",
                            temperature_entity_id="sensor.hp_temp")
    hass.set("sensor.hp_temp", "120", unit_of_measurement="°F")
    assert hp.get_current_temperature() == pytest.approx(F120_C)
    hass.set("sensor.hp_temp", "45", unit_of_measurement="°C")
    assert hp.get_current_temperature() == pytest.approx(45.0)
