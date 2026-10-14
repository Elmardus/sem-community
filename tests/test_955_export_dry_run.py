"""#955 — the export axis answers "what would hit the wire" (#855, for the meter).

An observer rig never runs the export verb: the seam publishes the DECISION
(``would: limit_export``) and stops. So on 17.09 every one of the day's four
adapter fixes — the inverter device, the three-state mode read, the kept
prior, the ``percent 100`` hand-back — could be exercised only by unit tests,
never on .175's real readback. Chargers got their answer in #855:
``withheld_commands`` names the exact service + payload. This gives the meter
the same: ``adapter.export_dry_run(intent, watts)`` walks the verb's own
checks in the verb's own order and returns the row the write would have been,
or the refusal in the verb's own words — pure, no write, no capture. The seam
appends it to the cycle's withheld list under its own key.
"""
from unittest.mock import AsyncMock, MagicMock, patch
from types import SimpleNamespace

import pytest

from custom_components.solar_energy_management.coordinator.actuate_export import (
    actuate_export,
)
from custom_components.solar_energy_management.coordinator.battery_adapters.base import (
    BatteryControlAdapter,
)
from custom_components.solar_energy_management.coordinator.battery_adapters.deye import (
    DeyeBatteryAdapter,
)
from custom_components.solar_energy_management.coordinator.battery_adapters.generic import (
    GenericBatteryAdapter,
)
from custom_components.solar_energy_management.coordinator.battery_adapters.huawei import (
    HuaweiBatteryAdapter,
)
from custom_components.solar_energy_management.coordinator.charger_types import (
    ExportDecision, ExportIntent,
)

INV, BATT = "inv-dev-id", "batt-dev-id"


class _State:
    def __init__(self, state, watt=None, percent=None):
        self.state = state
        self.attributes = {"maximum_power_watt": watt, "maximum_power_percent": percent}


class _Reg:
    """(#1026) HA's device registry answers `async_get`; its `devices` mapping
    is no longer ours to read."""

    def __init__(self, devices):
        self._by_id = dict(devices)

    def async_get(self, device_id):
        return self._by_id.get(device_id)


def _registry():
    reg = _Reg({
        INV: SimpleNamespace(id=INV, identifiers={("huawei_solar", "BT1")}, via_device_id=None),
        BATT: SimpleNamespace(id=BATT, identifiers={("huawei_solar", "BT1/battery_1")},
                              via_device_id=INV),
    })
    return patch("homeassistant.helpers.device_registry.async_get", return_value=reg)


def _huawei(mode="Limited to 100.0%", percent=100.0, override=False):
    a = HuaweiBatteryAdapter.__new__(HuaweiBatteryAdapter)
    a._hass = MagicMock()
    a._config = {"export_control_readback_entity": "sensor.apc",
                 "export_guard_override_external": override}
    a._inverter_device_id = BATT
    a._hass.states.get = MagicMock(
        return_value=None if mode is None else _State(mode, percent=percent))
    a._hass.services.async_call = AsyncMock()
    return a


# ═══════════════════════════════════════════════════════════════════════
# Huawei — the four 17.09 fixes, readable on an observer rig
# ═══════════════════════════════════════════════════════════════════════

class TestHuaweiDryRun:
    def test_a_zero_cut_is_zero_power_addressed_to_the_INVERTER(self):
        with _registry():
            row = _huawei().export_dry_run(ExportIntent.LIMIT, 0.0)
        assert row == {"service": "huawei_solar.set_zero_power_grid_connection",
                       "data": {"device_id": INV}, "why": None}
        assert row["data"]["device_id"] != BATT, "the #955 bug: the battery id"

    def test_a_watt_cap_is_the_watt_service(self):
        with _registry():
            row = _huawei().export_dry_run(ExportIntent.LIMIT, 1500.0)
        assert row["service"] == "huawei_solar.set_maximum_feed_grid_power"
        assert row["data"] == {"device_id": INV, "power": 1500}

    def test_an_unreadable_mode_is_the_refusal_in_the_verbs_own_words(self):
        with _registry():
            row = _huawei(mode="unavailable").export_dry_run(ExportIntent.LIMIT, 0.0)
        assert row["service"] is None
        assert "cannot read the inverter's active-power mode" in row["why"]

    def test_di_scheduling_refuses_without_the_override_and_cuts_with_it(self):
        with _registry():
            no = _huawei(mode="DI Active Scheduling").export_dry_run(ExportIntent.LIMIT, 0.0)
            yes = _huawei(mode="DI Active Scheduling", override=True).export_dry_run(
                ExportIntent.LIMIT, 0.0)
        assert no["service"] is None and "external scheduling" in no["why"]
        assert yes["service"] == "huawei_solar.set_zero_power_grid_connection"

    def test_a_release_on_a_rig_that_never_cut_is_derived_from_the_readback(self):
        """The standing mode since 17.09: the hand-back the real release
        would capture and restore is `percent 100` — the verb proven to land."""
        with _registry():
            row = _huawei().export_dry_run(ExportIntent.RELEASE, 0.0)
        assert row == {"service": "huawei_solar.set_maximum_feed_grid_power_percent",
                       "data": {"device_id": INV, "power_percentage": 100.0}, "why": None}

    def test_a_release_after_a_real_cut_restores_the_CAPTURED_prior(self):
        """The readback echoes SEM's own cut for up to 15 min (measured); the
        recipe must come from the prior captured BEFORE the write."""
        a = _huawei(mode="DI Active Scheduling", override=True)
        with _registry():
            a._capture_export_prior()
            a._hass.states.get = MagicMock(return_value=_State("Zero Power"))
            row = a.export_dry_run(ExportIntent.RELEASE, 0.0)
        assert row["service"] == "huawei_solar.set_di_active_power_scheduling"

    def test_no_inverter_device_is_a_row_that_says_so(self):
        a = _huawei(); a._inverter_device_id = ""
        with patch("homeassistant.helpers.device_registry.async_get",
                   return_value=_Reg({})):
            row = a.export_dry_run(ExportIntent.LIMIT, 0.0)
        assert row["service"] is None and "inverter_device_id" in row["why"]

    def test_the_dry_run_writes_nothing_and_captures_nothing(self):
        a = _huawei()
        with _registry():
            a.export_dry_run(ExportIntent.LIMIT, 0.0)
            a.export_dry_run(ExportIntent.RELEASE, 0.0)
        a._hass.services.async_call.assert_not_awaited()
        assert getattr(a, "_export_prior_mode", None) is None
        assert a._last_export_limit_w is None and a._last_export_intent is None

    def test_the_real_verb_and_the_dry_run_agree(self):
        """Same checks, same order: whatever the verb sends, the row names."""
        import asyncio
        a = _huawei()
        with _registry():
            row = a.export_dry_run(ExportIntent.LIMIT, 0.0)
            asyncio.get_event_loop().run_until_complete(a.command_limit_export(0.0))
        sent = a._hass.services.async_call.await_args.args
        assert f"{sent[0]}.{sent[1]}" == row["service"] and sent[2] == row["data"]


# ═══════════════════════════════════════════════════════════════════════
# Generic and Deye — the same question, their own dialects
# ═══════════════════════════════════════════════════════════════════════

class TestGenericDryRun:
    def _a(self, ent="number.export_limit", state="6000"):
        a = GenericBatteryAdapter.__new__(GenericBatteryAdapter)
        a._hass = MagicMock(); a._config = {"export_limit_entity": ent}
        a._hass.states.get = MagicMock(return_value=SimpleNamespace(state=state))
        return a

    def test_cut_and_release_name_the_number_write(self):
        a = self._a()
        assert a.export_dry_run(ExportIntent.LIMIT, 0.0) == {
            "service": "number.set_value",
            "data": {"entity_id": "number.export_limit", "value": 0.0}, "why": None}
        assert a.export_dry_run(ExportIntent.RELEASE, 0.0)["data"]["value"] == 6000.0

    def test_no_entity_and_a_read_only_entity_refuse_in_the_verbs_words(self):
        assert "no export limit entity" in self._a(ent="").export_dry_run(ExportIntent.LIMIT, 0)["why"]
        assert "read-only" in self._a(ent="sensor.x").export_dry_run(ExportIntent.LIMIT, 0)["why"]

    def test_an_unreadable_prior_refuses(self):
        assert "unreadable" in self._a(state="unknown").export_dry_run(ExportIntent.LIMIT, 0)["why"]


class TestDeyeDryRun:
    def _a(self, control=True, current="Selling First"):
        a = DeyeBatteryAdapter.__new__(DeyeBatteryAdapter)
        a._hass = MagicMock()
        a._system_work_mode_control = control
        a._system_work_mode_entity = "select.deye_work_mode"
        a._system_work_mode_options = {"zero_export_to_load": "Zero Export To Load",
                                       "selling_first": "Selling First"}
        a._get_state = MagicMock(return_value=current)
        return a

    def test_a_cut_selects_zero_export_and_a_release_the_mode_it_found(self):
        a = self._a()
        assert a.export_dry_run(ExportIntent.LIMIT, 0.0) == {
            "service": "select.select_option",
            "data": {"entity_id": "select.deye_work_mode", "option": "Zero Export To Load"},
            "why": None}
        assert a.export_dry_run(ExportIntent.RELEASE, 0.0)["data"]["option"] == "Selling First"

    def test_without_consent_it_refuses_in_the_verbs_words(self):
        row = self._a(control=False).export_dry_run(ExportIntent.LIMIT, 0.0)
        assert row["service"] is None and "SEM may not drive it" in row["why"]


class TestTheBaseContract:
    def test_a_brand_without_export_control_answers_the_base_refusal(self):
        class Bare(BatteryControlAdapter):
            async def command_normal(self): ...
            async def command_limit_discharge(self, watts): ...
            async def command_force_charge(self, *a): ...
            async def command_stop_force_charge(self): ...
            @property
            def max_charge_power_w(self): return 5000.0
            @property
            def max_discharge_power_w(self): return 5000.0
            @property
            def supports_forced_charge(self): return False
        row = Bare(MagicMock(), {}).export_dry_run(ExportIntent.LIMIT, 0.0)
        assert row == {"service": None, "data": None,
                       "why": "this battery adapter has no export control"}


# ═══════════════════════════════════════════════════════════════════════
# The seam publishes it — observer only, never breaking
# ═══════════════════════════════════════════════════════════════════════

def _adapter(row=None, raises=False):
    a = MagicMock()
    a.command_limit_export = AsyncMock(); a.command_release_export = AsyncMock()
    if raises:
        a.export_dry_run = MagicMock(side_effect=RuntimeError("boom"))
    else:
        a.export_dry_run = MagicMock(return_value=row or {
            "service": "huawei_solar.set_zero_power_grid_connection",
            "data": {"device_id": INV}, "why": None})
    return a


@pytest.mark.asyncio
class TestTheSeamPublishesTheRow:
    async def test_an_observer_cut_appends_the_exact_call(self):
        rows = []
        await actuate_export(ExportDecision(ExportIntent.LIMIT, 0.0, "closed"), _adapter(),
                             observer=True, controller=MagicMock(), withheld=rows)
        assert rows == [{"service": "huawei_solar.set_zero_power_grid_connection",
                         "data": {"device_id": INV}, "why": None, "intent": "limit_export",
                         "standing": False}]

    async def test_a_held_cut_keeps_the_row_on_every_quiet_cycle(self):
        """#764 for the payload: the withheld log is rebuilt each cycle, so a
        standing cut has to keep saying what it is holding."""
        rows = []
        await actuate_export(ExportDecision(reason="holding"), _adapter(),
                             observer=True, controller=MagicMock(), standing="engaged",
                             withheld=rows)
        assert rows and rows[0]["intent"] == "limit_export" and rows[0]["standing"] is True
        rows = []
        await actuate_export(ExportDecision(reason="open"), _adapter(),
                             observer=True, controller=MagicMock(), standing="releasing",
                             withheld=rows)
        assert rows and rows[0]["intent"] == "release_export" and rows[0]["standing"] is True

    async def test_a_guard_that_wrote_nothing_appends_nothing(self):
        rows = []
        await actuate_export(ExportDecision(reason="waiting"), _adapter(),
                             observer=True, controller=MagicMock(), standing="holding",
                             withheld=rows)
        assert rows == []

    async def test_a_live_install_appends_nothing(self):
        rows = []
        await actuate_export(ExportDecision(ExportIntent.LIMIT, 0.0, "closed"), _adapter(),
                             observer=False, withheld=rows)
        assert rows == []

    async def test_a_dry_run_that_raises_becomes_a_row_and_never_breaks_the_seam(self):
        rows = []
        assert await actuate_export(ExportDecision(ExportIntent.LIMIT, 0.0, "closed"),
                                    _adapter(raises=True), observer=True,
                                    controller=MagicMock(), withheld=rows) is None
        assert rows[0]["service"] is None and "dry-run failed: boom" in rows[0]["why"]

    async def test_no_adapter_is_a_row_that_says_so(self):
        rows = []
        await actuate_export(ExportDecision(ExportIntent.LIMIT, 0.0, "closed"), None,
                             observer=True, controller=MagicMock(), withheld=rows)
        assert "no adapter can write the export limit" in rows[0]["why"]

    async def test_no_list_means_the_old_seam_exactly(self):
        a = _adapter()
        await actuate_export(ExportDecision(ExportIntent.LIMIT, 0.0, "closed"), a,
                             observer=True, controller=MagicMock())
        a.export_dry_run.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════
# The coordinator merges it under the seam's own key
# ═══════════════════════════════════════════════════════════════════════

class TestTheCoordinatorMergesTheRow:
    def _fake(self, rows):
        from types import SimpleNamespace as NS
        return NS(_ev_devices={"keba_1": NS(device_id="keba_1", withheld_commands=[
            {"service": "keba.set_current", "data": {"current": 9}, "why": "-"}])},
            _ev_device=None, _export_withheld=rows)

    def test_the_meter_row_sits_beside_the_chargers(self):
        from custom_components.solar_energy_management.coordinator.coordinator import (
            SEMCoordinator,
        )
        row = {"service": "huawei_solar.set_zero_power_grid_connection",
               "data": {"device_id": INV}, "why": None, "intent": "limit_export"}
        out = SEMCoordinator.observer_withheld_commands(self._fake([row]))
        assert out["export_guard"] == [row] and "keba_1" in out

    def test_no_row_means_no_key(self):
        from custom_components.solar_energy_management.coordinator.coordinator import (
            SEMCoordinator,
        )
        assert "export_guard" not in SEMCoordinator.observer_withheld_commands(self._fake([]))
        assert "export_guard" not in SEMCoordinator.observer_withheld_commands(self._fake(None))
