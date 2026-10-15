"""#976 — an OCPP charger is never stopped by parking a 0 A profile on it.

@bgthb (Huawei SCharger 22-KT through the OCPP integration, 18.09): after
SEM's first stop, every start — the Huawei app, the card swipe, HA's OCPP
switch — was ``RemoteStartTransaction: Accepted`` followed by
``StopTransaction`` a second later, reason ``Other``. The charge point was
locked, and a factory reset looked inevitable.

The mechanism: the charger was configured by hand with its maximum-current
number alone, so ``stop_session`` found no brand mechanism and fell through
to the generic stop — **write 0 A**. On the OCPP integration that number is
a *charging profile*, and the charge point KEEPS it: the 0 A limit outlives
the session and ends every later one. The KEBA ``set_current(0)`` lesson in
another dialect: a "stop" that is really a persisted limit.

Three things close it, each pinned here:

* ``_set_current`` — the ONE emit seam — never writes 0 A to an OCPP
  current number (a nonzero current still goes; other brands are untouched);
* ``can_stop_charging`` is False for an OCPP number without a switch, so the
  existing #627 Repair says "cannot be stopped — configure the switch";
* the builder adopts the charge point's ``charge_control`` switch when the
  user configured the number alone, so the stop is a RemoteStop.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.solar_energy_management.devices.base import (
    CurrentControlDevice,
)
from custom_components.solar_energy_management.hardware_detection import (
    entity_platform, ocpp_charge_control_switch,
)

NUM, SW, AVAIL = ("number.ocpp_cp_maximum_current",
                  "switch.ocpp_cp_charge_control", "switch.ocpp_cp_availability")


def _hass(min_a=0.0, max_a=32.0):
    hass = MagicMock()
    st = SimpleNamespace(state="16", attributes={"min": min_a, "max": max_a, "step": 1})
    hass.states.get = MagicMock(return_value=st)
    hass.services.async_call = AsyncMock(return_value=None)
    hass.services.has_service = MagicMock(return_value=False)
    return hass


def _device(platform="ocpp", switch=None, min_a=0.0, hass=None):
    hass = hass or _hass(min_a=min_a)
    d = CurrentControlDevice(
        hass=hass, device_id="cp", name="Charge point", priority=3,
        min_current=6.0, max_current=32.0, phases=3, voltage=230.0,
        power_entity_id="sensor.ocpp_cp_power_active_import",
        charger_service=None, charger_service_entity_id=None, current_entity_id=NUM,
    )
    d.zero_amps_parks_a_limit = (platform == "ocpp")
    if switch:
        d.start_stop_entity = switch
    return d, hass


def _calls(hass):
    return [(c.args[0], c.args[1], dict(c.args[2]) if len(c.args) > 2 else {})
            for c in hass.services.async_call.await_args_list]


# ═══════════════════════════════════════════════════════════════════════
# The emit seam
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
class TestZeroAmpsNeverReachesAnOcppNumber:
    async def test_a_zero_write_is_refused(self, caplog):
        d, hass = _device()
        with caplog.at_level(logging.WARNING):
            await d._set_current(0)
        assert not [c for c in _calls(hass) if c[0] == "number"], _calls(hass)
        assert any("refusing to write 0 A" in m for m in caplog.messages)

    async def test_it_says_so_once(self, caplog):
        d, hass = _device()
        with caplog.at_level(logging.WARNING):
            await d._set_current(0); await d._set_current(0); await d._set_current(0)
        assert len([m for m in caplog.messages if "refusing to write 0 A" in m]) == 1

    async def test_a_nonzero_current_still_goes_out(self):
        d, hass = _device()
        await d._set_current(10)
        assert any(c[0] == "number" and c[1] == "set_value" and c[2].get("value") == 10.0
                   for c in _calls(hass)), _calls(hass)

    async def test_another_brand_with_a_zero_capable_number_is_untouched(self):
        """Today's behaviour for every non-OCPP number: 0 A is its pause."""
        d, hass = _device(platform="wallbox", min_a=0.0)
        await d._set_current(0)
        assert any(c[0] == "number" and c[2].get("value") == 0.0 for c in _calls(hass)), _calls(hass)

    async def test_unknown_platform_is_not_treated_as_ocpp(self):
        d, hass = _device(platform=None, min_a=0.0)
        await d._set_current(0)
        assert any(c[0] == "number" and c[2].get("value") == 0.0 for c in _calls(hass))


# ═══════════════════════════════════════════════════════════════════════
# The capability probe (#627) and the stop
# ═══════════════════════════════════════════════════════════════════════

class TestCanStopCharging:
    def test_ocpp_number_alone_cannot_stop(self):
        d, _ = _device()
        assert d.can_stop_charging() is False

    def test_ocpp_with_the_charge_control_switch_can(self):
        d, _ = _device(switch=SW)
        assert d.can_stop_charging() is True

    def test_another_brand_zero_capable_number_can(self):
        d, _ = _device(platform="wallbox", min_a=0.0)
        assert d.can_stop_charging() is True


@pytest.mark.asyncio
class TestStopSessionOnOcpp:
    async def test_with_the_switch_the_stop_is_a_remote_stop_and_no_zero_write(self):
        d, hass = _device(switch=SW)
        d._session_active = True
        await d.stop_session()
        calls = _calls(hass)
        assert ("switch", "turn_off", {"entity_id": SW}) in calls, calls
        assert not any(c[0] == "number" and c[2].get("value") == 0.0 for c in calls), calls

    async def test_without_the_switch_nothing_locks_the_charge_point(self, caplog):
        d, hass = _device()
        d._session_active = True
        with caplog.at_level(logging.WARNING):
            await d.stop_session()
        assert not any(c[0] == "number" and c[2].get("value") == 0.0 for c in _calls(hass))


# ═══════════════════════════════════════════════════════════════════════
# The builder's adoption of the sibling switch
# ═══════════════════════════════════════════════════════════════════════

def _registry(entries):
    class _Reg:
        def __init__(self):
            self.entities = {e.entity_id: e for e in entries}
        def async_get(self, eid):
            return self.entities.get(eid)
    return patch("homeassistant.helpers.entity_registry.async_get", return_value=_Reg())


def _e(eid, platform="ocpp", device_id="dev-cp"):
    return SimpleNamespace(entity_id=eid, platform=platform, device_id=device_id)


class TestSiblingSwitchAdoption:
    def test_the_charge_control_switch_of_the_same_charge_point_is_found(self):
        with _registry([_e(NUM), _e(AVAIL), _e(SW)]):
            assert ocpp_charge_control_switch(MagicMock(), NUM) == SW

    def test_the_availability_switch_is_never_mistaken_for_it(self):
        with _registry([_e(NUM), _e(AVAIL)]):
            assert ocpp_charge_control_switch(MagicMock(), NUM) is None

    def test_another_charge_points_switch_is_not_adopted(self):
        with _registry([_e(NUM), _e("switch.ocpp_other_charge_control", device_id="dev-other")]):
            assert ocpp_charge_control_switch(MagicMock(), NUM) is None

    def test_a_non_ocpp_number_adopts_nothing(self):
        with _registry([_e(NUM, platform="wallbox"), _e(SW)]):
            assert ocpp_charge_control_switch(MagicMock(), NUM) is None

    def test_a_registry_that_raises_is_none_not_a_crash(self):
        with patch("homeassistant.helpers.entity_registry.async_get", side_effect=RuntimeError("boom")):
            assert ocpp_charge_control_switch(MagicMock(), NUM) is None
            assert entity_platform(MagicMock(), NUM) is None

    def test_entity_platform_reads_the_registry(self):
        with _registry([_e(NUM)]):
            assert entity_platform(MagicMock(), NUM) == "ocpp"
        with _registry([]):
            assert entity_platform(MagicMock(), NUM) is None


# ═══════════════════════════════════════════════════════════════════════
# ONE producer — every builder wires the current entity the same way
# ═══════════════════════════════════════════════════════════════════════

class TestOneProducer:
    """The first cut set the flag in the setup-time builder only; the
    coordinator's late retry (``_retry_ev_device_setup``) built a device
    without it — a second producer missing the field, the shape that hid
    the export guard's silent no-op (bug class 93). The facts now have one
    producer, ``hardware_detection.wire_current_entity``, and every
    construction site is pinned to call it."""

    def test_wire_sets_the_flag_and_adopts_the_switch(self, caplog):
        from custom_components.solar_energy_management.hardware_detection import wire_current_entity
        d, _ = _device(platform=None)
        with _registry([_e(NUM), _e(AVAIL), _e(SW)]), caplog.at_level(logging.INFO):
            wire_current_entity(MagicMock(), d, "cp", NUM)
        assert d.zero_amps_parks_a_limit is True and d.start_stop_entity == SW
        assert any("adopted switch.ocpp_cp_charge_control" in m for m in caplog.messages)

    def test_wire_without_a_switch_warns_and_the_device_cannot_stop(self, caplog):
        from custom_components.solar_energy_management.hardware_detection import wire_current_entity
        d, _ = _device(platform=None)
        with _registry([_e(NUM), _e(AVAIL)]), caplog.at_level(logging.WARNING):
            wire_current_entity(MagicMock(), d, "cp", NUM)
        assert d.zero_amps_parks_a_limit is True and d.start_stop_entity is None
        assert any("cannot stop this charger" in m for m in caplog.messages)
        assert d.can_stop_charging() is False

    def test_wire_keeps_a_switch_the_user_configured(self):
        from custom_components.solar_energy_management.hardware_detection import wire_current_entity
        d, _ = _device(platform=None, switch="switch.my_own_stop")
        with _registry([_e(NUM), _e(SW)]):
            wire_current_entity(MagicMock(), d, "cp", NUM)
        assert d.start_stop_entity == "switch.my_own_stop"

    def test_wire_on_another_platform_changes_nothing(self):
        from custom_components.solar_energy_management.hardware_detection import wire_current_entity
        d, _ = _device(platform=None)
        with _registry([_e(NUM, platform="wallbox"), _e(SW)]):
            wire_current_entity(MagicMock(), d, "cp", NUM)
        assert d.zero_amps_parks_a_limit is False and d.start_stop_entity is None

    def test_every_construction_site_calls_the_producer(self):
        """Structural: each production ``CurrentControlDevice(...)`` — the
        setup-time builder AND the coordinator's late retry — sits in a
        function that calls ``wire_current_entity``."""
        import ast as _ast
        from pathlib import Path
        from custom_components.solar_energy_management.tests import ast_contracts
        sites = ast_contracts.call_sites("CurrentControlDevice")
        assert {"__init__.py", "coordinator/coordinator.py"} <= {rel for rel, _, _ in sites}, sites
        root = Path(ast_contracts.__file__).resolve().parent.parent
        for rel, lineno, _ in sites:
            tree = _ast.parse((root / rel).read_text(encoding="utf-8"))
            fns = [n for n in _ast.walk(tree)
                   if isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef))
                   and n.lineno <= lineno <= (n.end_lineno or n.lineno)]
            fn = max(fns, key=lambda n: n.lineno)
            assert any(isinstance(c, _ast.Call)
                       and ast_contracts._callee_name(c) == "wire_current_entity"
                       for c in _ast.walk(fn)), (
                f"{rel}:{lineno} builds a CurrentControlDevice in {fn.name}() "
                f"without wire_current_entity()")

    @pytest.mark.asyncio
    async def test_the_late_retry_builds_a_wired_device(self):
        """A charge point discovered AFTER startup carries the same facts as
        one built at setup: the flag, and the adopted switch."""
        from custom_components.solar_energy_management.coordinator.coordinator import SEMCoordinator
        hass = _hass()
        from custom_components.solar_energy_management.coordinator.sensor_reader import SensorReader
        me = SimpleNamespace(hass=hass, config={"ev_surplus_priority": 3, "ev_phases": 3},
                             _surplus_controller=MagicMock(),
                             _sensor_reader=SensorReader(MagicMock(), {}),   # (#1125)
                             refresh_detection_report=lambda: None)
        auto = {"ev_charger_service": "number.set_value",
                "ev_current_control_entity": NUM,
                "ev_charging_power_sensor": "sensor.ocpp_cp_power_active_import"}
        with patch("custom_components.solar_energy_management.hardware_detection"
                   ".discover_ev_charger_from_registry", return_value=auto), \
                _registry([_e(NUM), _e(AVAIL), _e(SW)]):
            await SEMCoordinator._retry_ev_device_setup(me)
        dev = me._surplus_controller.register_device.call_args.args[0]
        assert dev.zero_amps_parks_a_limit is True and dev.start_stop_entity == SW
