"""#1095 — a charger the crawler offers with ONLY a charge-mode select or
ONLY start/stop is set up, and SEM drives it.

The crawler offers myenergi and Ohme with a charge-mode select as their only
control, and a Zaptec without current rights with start/stop only. Setup kept
a charger only when it had a current service or a current entity, so these
were dropped ("missing power sensor or control method") and SEM ran nothing.
Setup now asks the crawler's own rule (``charger_is_complete``): a power
reading and any way to act. The lifecycle suite proves the setup side for
the real captures; this file proves the runtime side — such a charger starts
and stops through its one control and no current write is sent anywhere.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from custom_components.solar_energy_management.coordinator.charger_adapters import (
    GenericAdapter,
)
from custom_components.solar_energy_management.devices.base import (
    CurrentControlDevice,
)
from custom_components.solar_energy_management.hardware_detection import (
    charger_is_complete,
)

MODE = "select.box_charge_mode"
SWITCH = "switch.box_charging"


def _device(**kw):
    """A real ``CurrentControlDevice`` with no current control at all,
    recording every service call."""
    sent: list = []
    hass = MagicMock()

    async def _call(domain, service, data=None, blocking=True):
        sent.append((domain, service, dict(data or {})))

    hass.services.async_call = _call
    hass.services.has_service = MagicMock(return_value=False)
    hass.states.get = MagicMock(side_effect=lambda eid: (
        SimpleNamespace(state="off", attributes={}) if eid == SWITCH else None))
    dev = CurrentControlDevice(
        hass=hass, device_id="box", name="Box", priority=5,
        min_current=6.0, max_current=32.0, phases=3, voltage=230.0,
        power_entity_id="sensor.box_power", current_entity_id=None,
        charger_service=None)
    for k, v in kw.items():
        setattr(dev, k, v)
    return dev, sent


def test_setup_accepts_what_the_crawler_offers():
    for control in ({"ev_charge_mode_entity": MODE},
                    {"ev_start_stop_entity": SWITCH},
                    {"ev_start_service": "box.start_charging"}):
        assert charger_is_complete(
            {"ev_charging_power_sensor": "sensor.box_power", **control}), control


@pytest.mark.asyncio
async def test_a_mode_only_charger_starts_and_stops_through_its_select():
    dev, sent = _device(charge_mode_entity=MODE, charge_mode_start="Fast",
                        charge_mode_stop="Stopped")
    adapter = GenericAdapter(dev)
    await adapter.command_current(10)
    assert ("select", "select_option",
            {"entity_id": MODE, "option": "Fast"}) in sent
    sent.clear()
    await adapter.command_idle()
    assert sent == [("select", "select_option",
                     {"entity_id": MODE, "option": "Stopped"})]


@pytest.mark.asyncio
async def test_a_switch_only_charger_starts_and_stops_through_its_switch():
    dev, sent = _device(start_stop_entity=SWITCH)
    adapter = GenericAdapter(dev)
    await adapter.command_current(10)
    assert ("switch", "turn_on", {"entity_id": SWITCH}) in sent
    sent.clear()
    await adapter.command_idle()
    assert ("switch", "turn_off", {"entity_id": SWITCH}) in sent


@pytest.mark.asyncio
async def test_no_current_write_is_invented():
    """With no current control, a current command reaches nothing: no
    ``number.set_value`` and no service with a current field."""
    for kw in ({"charge_mode_entity": MODE, "charge_mode_start": "Fast",
                "charge_mode_stop": "Stopped"},
               {"start_stop_entity": SWITCH}):
        dev, sent = _device(**kw)
        await GenericAdapter(dev).command_current(16)
        assert not [s for s in sent if s[0] == "number"
                    or any("current" in k for k in s[2])], sent
