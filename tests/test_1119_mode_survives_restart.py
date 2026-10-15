"""#1119 — a boiler or heat pump set to Off or Peak-only stays so after a
restart.

The controllers are built in Solar (surplus) mode. The mode the user picked
on the card was applied only on a live change; after a restart
``refresh_direct_device_overrides`` put back the priority and the goals, not
the mode, so the device ran on sun again while the card showed the saved
mode.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from custom_components.solar_energy_management.coordinator.surplus_controller import (
    SurplusController,
)
from custom_components.solar_energy_management.devices.base import DeviceControlMode
from custom_components.solar_energy_management.devices.heat_pump_controller import (
    HeatPumpController,
)
from custom_components.solar_energy_management.devices.hot_water_controller import (
    HotWaterController,
)
from custom_components.solar_energy_management.features.device_registry import (
    UnifiedDeviceRegistry,
)
from custom_components.solar_energy_management.tests.test_1062_hot_water_fields import (
    _Hass,
)


def _restarted(hass, modes: dict):
    """A fresh controller as the boot builds it, and the registry with the
    modes it loaded from storage."""
    dev = HotWaterController(
        hass=hass, entity_id="switch.boiler",
        temperature_entity_id="sensor.boiler_temp",
        solar_target_temp=55.0, rated_power=2000.0)
    dev.record_legionella_cycle()
    sc = SurplusController(hass)
    sc.register_device(dev)
    reg = UnifiedDeviceRegistry.__new__(UnifiedDeviceRegistry)
    reg._device_goals = {}
    reg._priority_overrides = {}
    reg._service_registrations = {}
    reg._control_mode_overrides = dict(modes)
    reg._user_control_modes = set(modes)
    reg._surplus_controller = sc
    reg.priority_for = lambda did, seed=5: seed
    return reg, sc, dev


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["off", "peak_only"])
async def test_a_saved_mode_holds_after_restart(mode):
    hass = _Hass()
    hass.set("switch.boiler", "off")
    hass.set("sensor.boiler_temp", "40.0")
    reg, sc, dev = _restarted(hass, {"hot_water": mode})
    reg.refresh_direct_device_overrides()
    assert dev.control_mode is DeviceControlMode(mode)
    await sc.update(5000.0)
    assert not dev.is_active, f"a boiler set to {mode} ran on sun after a restart"
    assert not hass.calls("turn_on")


@pytest.mark.asyncio
async def test_no_saved_mode_keeps_solar():
    hass = _Hass()
    hass.set("switch.boiler", "off")
    hass.set("sensor.boiler_temp", "40.0")
    reg, sc, dev = _restarted(hass, {})
    reg.refresh_direct_device_overrides()
    assert dev.control_mode is DeviceControlMode.SURPLUS
    await sc.update(5000.0)
    assert dev.is_active


def test_the_heat_pump_too():
    hass = _Hass()
    hp = HeatPumpController(hass, relay1_entity_id="switch.sg1")
    sc = SurplusController(hass)
    sc.register_device(hp)
    reg = UnifiedDeviceRegistry.__new__(UnifiedDeviceRegistry)
    reg._device_goals = {}
    reg._priority_overrides = {}
    reg._service_registrations = {}
    reg._control_mode_overrides = {"heat_pump": "off"}
    reg._user_control_modes = set()      # an Off set before the fix counts too
    reg._surplus_controller = sc
    reg.priority_for = lambda did, seed=5: seed
    reg.refresh_direct_device_overrides()
    assert hp.control_mode is DeviceControlMode.OFF


def test_a_bad_stored_mode_is_ignored():
    reg, _sc, dev = _restarted(_Hass(), {"hot_water": "nonsense"})
    reg.refresh_direct_device_overrides()
    assert dev.control_mode is DeviceControlMode.SURPLUS


def test_service_rows_keep_their_spec_mode():
    hass = _Hass()
    reg, sc, _dev = _restarted(hass, {})
    svc = SimpleNamespace(device_id="svc", priority=5, is_ev=False,
                          control_mode=DeviceControlMode.PEAK_ONLY)
    sc._devices["svc"] = svc
    reg._service_registrations = {"svc": {"control_mode": "peak_only"}}
    reg._apply_goals = lambda d: None
    reg.refresh_direct_device_overrides()
    assert svc.control_mode is DeviceControlMode.PEAK_ONLY


def test_a_peak_only_the_upgrade_wrote_is_not_the_users():
    """The #805 freeze pins "peak_only" for every id it knows — a dragged or
    goal-carrying boiler included. That is not a choice; Solar stays."""
    reg, _sc, dev = _restarted(_Hass(), {"hot_water": "peak_only"})
    reg._user_control_modes = set()
    reg.refresh_direct_device_overrides()
    assert dev.control_mode is DeviceControlMode.SURPLUS


@pytest.mark.asyncio
async def test_a_live_pick_is_marked_as_the_users():
    hass = _Hass()
    reg, _sc, dev = _restarted(hass, {})
    reg._user_control_modes = set()
    reg._load_manager = None
    async def _save():
        return None
    reg._save_storage = _save
    await reg.update_device_control_mode("hot_water", "peak_only")
    assert "hot_water" in reg._user_control_modes
    dev.control_mode = DeviceControlMode.SURPLUS      # a restart builds Solar
    reg.refresh_direct_device_overrides()
    assert dev.control_mode is DeviceControlMode.PEAK_ONLY
