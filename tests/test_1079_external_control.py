"""#1079 — a device another party steers: SEM yields, or says so.

Pinned here: the default changes nothing; a yielded device gets no write at
the seam, in the charger actuator or in the battery actuator; a restart with
the indicator still loading never reads as "steered"; a flickering indicator
holds; observer mode still wins; a device set back to none is released.
"""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.solar_energy_management.coordinator.external_control import (
    HOLD_S,
    MODE_NONE,
    MODE_OBSERVE,
    MODE_YIELD,
    ExternalControlBook,
    Spec,
    normalise_mode,
    read_indicator,
)

NOW = datetime(2026, 10, 27, 20, 14, tzinfo=timezone.utc)


def _hass(**states):
    hass = MagicMock()
    hass.states.get = lambda eid: (
        SimpleNamespace(state=states[eid], attributes={"friendly_name": "Grid Rewards"})
        if eid in states else None)
    return hass


def _spec(mode=MODE_YIELD, entity="binary_sensor.steer"):
    return {"charger:c1": Spec(mode, entity, "Grid Rewards")}


@pytest.mark.unit
class TestReadIndicator:
    @pytest.mark.parametrize("state,want", [
        ("on", True), ("off", False), ("active", True), ("idle", False),
        ("steering", True), ("none", False), ("Normal", False),
    ])
    def test_a_state_reads_on_or_off(self, state, want):
        assert read_indicator(_hass(**{"x.y": state}), "x.y") is want

    @pytest.mark.parametrize("state", ["unknown", "unavailable", ""])
    def test_an_unreadable_state_is_none_never_off(self, state):
        assert read_indicator(_hass(**{"x.y": state}), "x.y") is None

    def test_a_missing_entity_is_none(self):
        assert read_indicator(_hass(), "x.y") is None
        assert read_indicator(_hass(), "") is None

    def test_a_bad_mode_is_none(self):
        assert normalise_mode("YIELD") == MODE_YIELD
        assert normalise_mode("steer") == MODE_NONE
        assert normalise_mode(None) == MODE_NONE


@pytest.mark.unit
class TestBook:
    def test_the_default_changes_nothing(self):
        book = ExternalControlBook()
        book.update(_hass(), _spec(MODE_NONE), 0.0, NOW)
        assert not book.yielding("charger:c1")
        assert book.rows() == []

    def test_an_active_indicator_yields(self):
        book = ExternalControlBook()
        book.update(_hass(**{"binary_sensor.steer": "on"}), _spec(), 0.0, NOW)
        assert book.yielding("charger:c1")
        assert "controlled by Grid Rewards since 20:14 — SEM yielding" in \
            book.line("charger:c1", "Wallbox")

    def test_restart_with_the_indicator_still_loading_is_not_steered(self):
        book = ExternalControlBook()
        for t in (0.0, 10.0, 20.0):
            book.update(_hass(**{"binary_sensor.steer": "unavailable"}),
                        _spec(), t, NOW)
            assert not book.yielding("charger:c1")
        book.update(_hass(), _spec(), 30.0, NOW)        # entity not there yet
        assert not book.yielding("charger:c1")

    def test_a_gap_in_the_reading_keeps_what_was_known(self):
        book = ExternalControlBook()
        book.update(_hass(**{"binary_sensor.steer": "on"}), _spec(), 0.0, NOW)
        book.update(_hass(**{"binary_sensor.steer": "unavailable"}),
                    _spec(), 600.0, NOW)
        assert book.yielding("charger:c1")

    def test_a_flickering_indicator_holds_then_releases(self):
        book = ExternalControlBook()
        on = _hass(**{"binary_sensor.steer": "on"})
        off = _hass(**{"binary_sensor.steer": "off"})
        book.update(on, _spec(), 0.0, NOW)
        book.update(off, _spec(), 10.0, NOW)            # a blink
        assert book.yielding("charger:c1")
        book.update(off, _spec(), HOLD_S - 1.0, NOW)
        assert book.yielding("charger:c1")
        book.update(off, _spec(), HOLD_S + 1.0, NOW)    # off for the hold
        assert not book.yielding("charger:c1")

    def test_a_blink_that_comes_back_on_restarts_the_hold(self):
        book = ExternalControlBook()
        on = _hass(**{"binary_sensor.steer": "on"})
        off = _hass(**{"binary_sensor.steer": "off"})
        book.update(on, _spec(), 0.0, NOW)
        book.update(off, _spec(), 200.0, NOW)
        book.update(on, _spec(), 250.0, NOW)
        book.update(off, _spec(), 250.0 + HOLD_S - 1.0, NOW)
        assert book.yielding("charger:c1")

    def test_observe_never_yields_but_raises_on_an_unmade_write(self):
        book = ExternalControlBook()
        book.update(_hass(**{"binary_sensor.steer": "on"}),
                    _spec(MODE_OBSERVE), 0.0, NOW, {"charger:c1": True})
        assert not book.yielding("charger:c1")
        assert book.raising("charger:c1")
        book.update(_hass(**{"binary_sensor.steer": "on"}),
                    _spec(MODE_OBSERVE), 10.0, NOW, {"charger:c1": False})
        assert not book.raising("charger:c1")

    def test_yield_does_not_raise_a_repair(self):
        book = ExternalControlBook()
        book.update(_hass(**{"binary_sensor.steer": "on"}), _spec(),
                    0.0, NOW, {"charger:c1": True})
        assert not book.raising("charger:c1")

    def test_a_device_set_back_to_none_is_released(self):
        book = ExternalControlBook()
        book.update(_hass(**{"binary_sensor.steer": "on"}), _spec(), 0.0, NOW)
        book.update(_hass(**{"binary_sensor.steer": "on"}),
                    _spec(MODE_NONE), 1.0, NOW)
        assert not book.yielding("charger:c1")
        book.update(_hass(**{"binary_sensor.steer": "on"}), _spec(), 2.0, NOW)
        assert book.yielding("charger:c1")              # fresh, not stale


@pytest.mark.unit
class TestChargerSeam:
    @staticmethod
    def _device(yielded):
        from custom_components.solar_energy_management.devices.base import (
            ControllableDevice,
        )
        class _Dev(ControllableDevice):
            async def activate(self, available_watts):
                return 0.0

            async def deactivate(self):
                return None

            async def adjust_power(self, available_watts):
                return 0.0

        dev = _Dev.__new__(_Dev)
        dev.hass = MagicMock()
        dev.hass.services.async_call = AsyncMock()
        dev.device_id = "c1"
        dev.name = "Wallbox"
        dev.observer_mode = False
        dev.external_yield = yielded
        return dev

    @pytest.mark.asyncio
    async def test_a_yielded_device_sends_nothing(self):
        dev = self._device(True)
        sent = await dev.send("number", "set_value", {"value": 10}, why="x")
        assert sent is False
        dev.hass.services.async_call.assert_not_called()
        assert dev.yielded_commands[-1]["service"] == "number.set_value"

    @pytest.mark.asyncio
    async def test_the_failsafe_arming_still_goes_out(self):
        dev = self._device(True)
        assert await dev.send("keba", "set_failsafe",
                              {"failsafe_timeout": 600}) is True
        dev.hass.services.async_call.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_yielded_charger_gets_no_zero_amp_dead_man(self):
        """A persisted 0 A dead-man ends the other party's session ~10 s
        into any masterless window (#1163). Yield arms none (review)."""
        from custom_components.solar_energy_management.devices.base import (
            CurrentControlDevice,
        )
        dev = self._device(True)
        dev.charger_service = "keba.set_current"
        dev.hass.services.has_service = MagicMock(return_value=True)
        await CurrentControlDevice.arm_failsafe_off(dev)
        dev.hass.services.async_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_default_device_sends(self):
        dev = self._device(False)
        assert await dev.send("number", "set_value", {"value": 10}) is True
        dev.hass.services.async_call.assert_awaited_once()


@pytest.mark.unit
class TestChargerActuator:
    @pytest.mark.asyncio
    async def test_yielded_skips_the_reconciler_and_stamps_the_device(self):
        from custom_components.solar_energy_management.coordinator.actuate import (
            actuate,
        )
        dev = SimpleNamespace(external_yield=False, observer_mode=False)
        adapter = SimpleNamespace(_device=dev)
        decision = SimpleNamespace(
            charger_id="c1", intent=SimpleNamespace(value="charge"),
            reason="surplus", mode="pv", commanded_amps=10)
        rec = SimpleNamespace(reconcile_and_apply=AsyncMock())
        await actuate(decision, adapter, SimpleNamespace(), rec, yielded=True)
        rec.reconcile_and_apply.assert_not_called()
        assert dev.external_yield is True

    @pytest.mark.asyncio
    async def test_not_yielded_runs_the_reconciler_and_clears_the_stamp(self):
        from custom_components.solar_energy_management.coordinator.actuate import (
            actuate,
        )
        dev = SimpleNamespace(external_yield=True, observer_mode=False)
        adapter = SimpleNamespace(_device=dev)
        decision = SimpleNamespace(
            charger_id="c1", intent=SimpleNamespace(value="charge"),
            reason="surplus", mode="pv", commanded_amps=10)
        rec = SimpleNamespace(reconcile_and_apply=AsyncMock())
        await actuate(decision, adapter, SimpleNamespace(), rec)
        rec.reconcile_and_apply.assert_awaited_once()
        assert dev.external_yield is False


@pytest.mark.unit
class TestBatteryActuator:
    @staticmethod
    def _decision(intent):
        return SimpleNamespace(
            battery_id="b1", intent=intent, reason="r", charge_power_w=0.0,
            discharge_power_w=0.0, discharge_limit_w=500.0)

    @pytest.mark.asyncio
    async def test_yielded_calls_no_adapter_method(self):
        from custom_components.solar_energy_management.coordinator.actuate_battery import (
            actuate_battery,
        )
        from custom_components.solar_energy_management.coordinator.charger_types import (
            BatteryIntent,
        )
        adapter = MagicMock()
        for name in ("command_normal", "command_off", "command_limit_discharge",
                     "command_force_charge", "command_force_discharge"):
            setattr(adapter, name, AsyncMock())
        for intent in (BatteryIntent.NORMAL, BatteryIntent.OFF,
                       BatteryIntent.LIMIT_DISCHARGE,
                       BatteryIntent.FORCE_CHARGE):
            await actuate_battery(self._decision(intent), adapter, yielded=True)
        for name in ("command_normal", "command_off", "command_limit_discharge",
                     "command_force_charge", "command_force_discharge"):
            getattr(adapter, name).assert_not_called()

    @pytest.mark.asyncio
    async def test_not_yielded_still_acts(self):
        from custom_components.solar_energy_management.coordinator.actuate_battery import (
            actuate_battery,
        )
        from custom_components.solar_energy_management.coordinator.charger_types import (
            BatteryIntent,
        )
        adapter = MagicMock()
        adapter.command_normal = AsyncMock()
        adapter.last_intent = None
        await actuate_battery(self._decision(BatteryIntent.NORMAL), adapter)
        adapter.command_normal.assert_awaited_once()


@pytest.mark.unit
class TestCoordinatorSpecs:
    @staticmethod
    def _coord(config):
        from custom_components.solar_energy_management.coordinator.coordinator import (
            SEMCoordinator,
        )
        c = MagicMock()
        c.config = config
        c.hass = _hass(**{"binary_sensor.steer": "on"})
        c._external_control = ExternalControlBook()
        c._observer_mode = False
        c._ev_devices = {}
        c._charger_reconcilers = {}
        c._live_battery_adapters = lambda: []
        c._external_control_specs = lambda: SEMCoordinator._external_control_specs(c)
        c.external_yielding = lambda k: SEMCoordinator.external_yielding(c, k)
        return c, SEMCoordinator

    def test_no_setting_means_no_specs(self):
        c, C = self._coord({"ev_chargers": [{"id": "c1"}]})
        assert C._external_control_specs(c) == {}

    def test_charger_and_battery_settings_become_specs(self):
        c, C = self._coord({
            "ev_chargers": [
                {"id": "c1", "external_control_mode": "yield",
                 "external_control_indicator": "binary_sensor.steer"},
                {"external_control_mode": "observe",
                 "external_control_indicator": "switch.x"}],
            "battery_external_control_mode": "yield",
            "battery_external_control_indicator": "binary_sensor.steer"})
        specs = C._external_control_specs(c)
        assert set(specs) == {"charger:c1", "charger:ev_charger_1", "battery"}
        assert specs["charger:c1"].mode == MODE_YIELD
        assert specs["battery"].label == "Grid Rewards"

    def test_refresh_stamps_the_device_and_yields(self):
        c, C = self._coord({
            "ev_chargers": [{"id": "c1", "external_control_mode": "yield",
                             "external_control_indicator": "binary_sensor.steer"}]})
        dev = SimpleNamespace(external_yield=False)
        c._ev_devices = {"c1": dev}
        C._refresh_external_control(c)
        assert C.external_yielding(c, "charger:c1") is True
        assert dev.external_yield is True

    def test_observer_mode_never_stamps_a_yield(self):
        c, C = self._coord({
            "ev_chargers": [{"id": "c1", "external_control_mode": "yield",
                             "external_control_indicator": "binary_sensor.steer"}]})
        c._observer_mode = True
        dev = SimpleNamespace(external_yield=False)
        c._ev_devices = {"c1": dev}
        C._refresh_external_control(c)
        assert dev.external_yield is False

    def test_a_charger_set_back_to_none_is_released(self):
        c, C = self._coord({
            "ev_chargers": [{"id": "c1", "external_control_mode": "yield",
                             "external_control_indicator": "binary_sensor.steer"}]})
        dev = SimpleNamespace(external_yield=False)
        c._ev_devices = {"c1": dev}
        C._refresh_external_control(c)
        c.config = {"ev_chargers": [{"id": "c1"}]}
        C._refresh_external_control(c)
        assert dev.external_yield is False
        assert C.external_yielding(c, "charger:c1") is False
