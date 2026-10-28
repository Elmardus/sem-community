"""#801 — SG-Ready via a service call, for heat pumps whose control surface
is a command (Buderus over EMS-ESP), beside the relay path. The user was
about to build a template helper to translate SEM's switch-shaped
assumption; the assumption moved instead.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest


class _Services:
    def __init__(self):
        self.calls = []

    def has_service(self, *_):
        return True

    async def async_call(self, domain, service, data, blocking=False):
        self.calls.append((f"{domain}.{service}", dict(data)))


def _controller(**over):
    from custom_components.solar_energy_management.devices.heat_pump_controller import (
        HeatPumpController,
    )
    hass = SimpleNamespace(services=_Services(),
                           states=SimpleNamespace(get=lambda e: over.get("_state_map", {}).get(e)))
    kw = dict(hass=hass, sg_ready_service="ems_esp.send_command",
              sg_ready_service_data={"command": "sgready", "value": "{state}"})
    kw.update({k: v for k, v in over.items() if not k.startswith("_")})
    return HeatPumpController(**kw), hass


@pytest.mark.asyncio
async def test_the_service_is_the_actuation_and_relays_stay_untouched():
    from custom_components.solar_energy_management.devices.heat_pump_controller import (
        SGReadyState,
    )
    c, hass = _controller(relay1_entity_id="switch.r1", relay2_entity_id="switch.r2")
    ok = await c._set_sg_ready_state(SGReadyState.BOOST)
    assert ok is True
    assert hass.services.calls == [("ems_esp.send_command",
                                    {"command": "sgready", "value": "3"})]
    assert c._last_relay_path == "service"


@pytest.mark.asyncio
async def test_placeholders_render_the_truth_table():
    from custom_components.solar_energy_management.devices.heat_pump_controller import (
        SGReadyState,
    )
    c, hass = _controller(sg_ready_service_data={
        "r1": "{relay1}", "r2": "{relay2}", "s": "{state}", "n": 7})
    await c._set_sg_ready_state(SGReadyState.FORCE_ON)      # 1:1
    _, data = hass.services.calls[0]
    assert data == {"r1": "true", "r2": "true", "s": "4", "n": 7}


@pytest.mark.asyncio
async def test_a_failing_service_reports_false_and_names_the_path():
    from custom_components.solar_energy_management.devices.heat_pump_controller import (
        SGReadyState,
    )
    c, hass = _controller()
    async def boom(*a, **k):
        raise RuntimeError("ems down")
    hass.services.async_call = boom
    assert await c._set_sg_ready_state(SGReadyState.NORMAL) is False
    assert c._last_relay_path == "service_failed"


@pytest.mark.asyncio
async def test_read_back_mismatch_is_named_not_trusted():
    from custom_components.solar_energy_management.devices.heat_pump_controller import (
        SGReadyState,
    )
    c, hass = _controller(sg_ready_state_entity="sensor.sg_state",
                          _state_map={"sensor.sg_state": SimpleNamespace(state="2")})
    ok = await c._set_sg_ready_state(SGReadyState.BOOST)     # commanded 3, reads 2
    assert ok is True
    assert c._last_relay_path == "service_unverified"


@pytest.mark.asyncio
async def test_without_a_service_the_relay_path_is_unchanged():
    from custom_components.solar_energy_management.devices.heat_pump_controller import (
        SGReadyState,
    )
    c, hass = _controller(sg_ready_service=None,
                          relay1_entity_id="switch.r1",
                          relay2_entity_id="switch.r2")
    await c._set_sg_ready_state(SGReadyState.BOOST)
    assert [c0 for c0, _ in hass.services.calls] == [
        "homeassistant.turn_off", "homeassistant.turn_on"]   # 0:1 for BOOST


@pytest.mark.asyncio
@pytest.mark.parametrize("readback", [None, "matching", "lagging", "unknown"])
async def test_successful_service_updates_commanded_and_published_sg_state(readback):
    """A renewal consuming SEM's SG sensor must repeat the successful command."""
    from custom_components.solar_energy_management.coordinator.coordinator import (
        SEMCoordinator,
    )
    from custom_components.solar_energy_management.devices.heat_pump_controller import (
        SGReadyState,
    )
    states = {}
    c, hass = _controller(
        sg_ready_state_entity="sensor.sg_state" if readback else None,
        _state_map=states,
    )
    previous = SGReadyState.NORMAL
    for requested in (SGReadyState.BOOST, SGReadyState.FORCE_ON, SGReadyState.NORMAL):
        if readback:
            value = (str(int(requested)) if readback == "matching" else
                     str(int(previous)) if readback == "lagging" else "unknown")
            states["sensor.sg_state"] = SimpleNamespace(state=value)
        assert await c._set_sg_ready_state(requested) is True
        assert hass.services.calls[-1][1]["value"] == str(int(requested))
        assert c.sg_ready_state is requested
        mode, published, _ = SEMCoordinator._heat_pump_sensor_state(c)
        assert (mode, published) == (requested.name.lower(), int(requested))
        # A delayed/unreadable observation must still be surfaced separately.
        assert c._last_relay_path == (
            "service_unverified" if readback in ("lagging", "unknown") else "service")
        previous = requested


@pytest.mark.asyncio
async def test_service_activation_and_deactivation_publish_the_boost_lifecycle():
    from custom_components.solar_energy_management.coordinator.coordinator import (
        SEMCoordinator,
    )
    c, _ = _controller()
    await c.activate(c.force_on_threshold - 1)
    assert SEMCoordinator._heat_pump_sensor_state(c) == ("boost", 3, True)
    await c.deactivate()
    assert SEMCoordinator._heat_pump_sensor_state(c) == ("normal", 2, False)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["service_error", "invalid_service"])
async def test_failed_service_keeps_last_sg_state(failure):
    from custom_components.solar_energy_management.coordinator.coordinator import (
        SEMCoordinator,
    )
    from custom_components.solar_energy_management.devices.heat_pump_controller import (
        SGReadyState,
    )
    c, hass = _controller()
    c._hp_status.sg_ready_state = SGReadyState.BOOST
    if failure == "service_error":
        async def boom(*a, **k):
            raise RuntimeError("service offline")
        hass.services.async_call = boom
    else:
        c.sg_ready_service = "invalid"
    assert await c._set_sg_ready_state(SGReadyState.NORMAL) is False
    assert c.sg_ready_state is SGReadyState.BOOST
    assert SEMCoordinator._heat_pump_sensor_state(c)[1] == 3
