"""#955 — the feed-in verbs go to the INVERTER, not the battery.

`huawei_solar` splits its services by device TYPE. `forcible_charge` resolves
a BATTERY device; `set_zero_power_grid_connection` and
`reset_maximum_feed_grid_power` go through `get_inverter_data`, which raises
`wrong_device_type` for anything that is not the inverter.

`_inverter_device_id` is — despite the name — the battery device:
`_autodetect_battery_device` looks for `connected_energy_storage` or
`/battery` on purpose (#523). Handing that to the feed-in verbs refuses on
EVERY zero-config Huawei install, which is the common one.

Nothing caught it: the other tests stub `_inverter_device_id = "dev"`, and the
rig runs in observer mode where the call is never made. It was found by
reading PROD's device registry — `Inverter` is `('huawei_solar','BT2470369058')`
with no `via_device_id`, `Battery 1` is `('huawei_solar','BT2470369058/battery_1')`
with `via_device_id` pointing at the inverter. This file is that registry.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from custom_components.solar_energy_management.coordinator.battery_adapters.huawei import (
    HuaweiBatteryAdapter,
)

INV, BATT, OPT = "inv-dev-id", "batt-dev-id", "opt-dev-id"


class _State:
    def __init__(self, state, watt=None, percent=None):
        self.state = state
        self.attributes = {"maximum_power_watt": watt, "maximum_power_percent": percent}


#: (#1026) The config entry the fake huawei_solar devices hang off. HA's
#: device registry is no longer readable as a mapping, so the fake answers the
#: two supported lookups instead: by id, and per config entry.
HUAWEI_ENTRY = "huawei-config-entry"


class _FakeRegistry:
    def __init__(self, devices):
        self._by_id = dict(devices)

    def async_get(self, device_id):
        return self._by_id.get(device_id)

    def for_config_entry(self, entry_id):
        return list(self._by_id.values()) if entry_id == HUAWEI_ENTRY else []


def _registry(devices):
    reg = _FakeRegistry(devices)
    return patch.multiple(
        "homeassistant.helpers.device_registry",
        async_get=MagicMock(return_value=reg),
        async_entries_for_config_entry=MagicMock(
            side_effect=lambda r, entry_id: r.for_config_entry(entry_id)))


def _prod_shaped():
    """PROD's actual shape: one inverter, one battery under it, an optimizer."""
    return {
        INV: SimpleNamespace(id=INV, identifiers={("huawei_solar", "BT2470369058")},
                             via_device_id=None),
        BATT: SimpleNamespace(id=BATT,
                              identifiers={("huawei_solar", "BT2470369058/battery_1")},
                              via_device_id=INV),
        OPT: SimpleNamespace(id=OPT, identifiers={("huawei_solar", "JV2339221485")},
                             via_device_id=INV),
    }


def _adapter(battery_device=BATT, config=None, mode="Unlimited"):
    a = HuaweiBatteryAdapter.__new__(HuaweiBatteryAdapter)
    a._hass = MagicMock()
    a._hass.config_entries.async_entries.return_value = [
        SimpleNamespace(entry_id=HUAWEI_ENTRY)]
    a._config = {"export_control_readback_entity": "sensor.apc", **(config or {})}
    a._inverter_device_id = battery_device
    # A READABLE mode by default: since the readback became three-state, an
    # unreadable one REFUSES the cut, so it is a subject of its own tests
    # rather than an accident of a fixture that never set it.
    a._hass.states.get = MagicMock(
        return_value=None if mode is None else _State(mode))
    return a


class TestTheExportVerbsTargetTheInverter:
    def test_the_battery_id_is_not_what_the_feed_in_verbs_get(self):
        """The bug, stated as the thing that must never come back."""
        a = _adapter()
        with _registry(_prod_shaped()):
            assert a._export_device_id() == INV
            assert a._export_device_id() != a._inverter_device_id

    def test_it_follows_the_battery_up_to_its_inverter(self):
        a = _adapter()
        with _registry(_prod_shaped()):
            assert a._export_device_id() == INV

    def test_a_solar_only_install_finds_the_root_device(self):
        """No battery at all — the inverter is still the only root huawei
        device whose identifier carries no sub-part."""
        devs = _prod_shaped(); devs.pop(BATT)
        a = _adapter(battery_device="")
        with _registry(devs):
            assert a._export_device_id() == INV

    def test_an_optimizer_is_never_the_target(self):
        """Optimizers are huawei_solar devices too — 27 of them on PROD."""
        devs = {OPT: _prod_shaped()[OPT]}
        a = _adapter(battery_device="")
        with _registry(devs):
            assert a._export_device_id() == ""   # a sub-device is not the inverter

    def test_an_explicit_config_wins(self):
        a = _adapter(config={"export_device_id": "chosen-by-hand"})
        with _registry(_prod_shaped()):
            assert a._export_device_id() == "chosen-by-hand"

    def test_no_huawei_devices_at_all_is_a_refusal_not_a_crash(self):
        a = _adapter(battery_device="")
        with _registry({}):
            assert a._export_device_id() == ""

    def test_a_registry_that_raises_is_a_refusal_not_a_crash(self):
        a = _adapter()
        with patch("homeassistant.helpers.device_registry.async_get",
                   side_effect=RuntimeError("no registry")):
            assert a._export_device_id() == ""


@pytest.mark.asyncio
class TestTheWriteUsesIt:
    async def test_the_cut_is_addressed_to_the_inverter(self):
        a = _adapter()
        a._hass.services.async_call = MagicMock()

        async def _call(*args, **kw):
            return None
        a._hass.services.async_call = MagicMock(side_effect=_call)
        with _registry(_prod_shaped()):
            await a.command_limit_export(0.0)
        sent = a._hass.services.async_call.call_args.args
        assert sent[0] == "huawei_solar"
        assert sent[2]["device_id"] == INV, "the cut went to the battery again"

    async def test_the_release_is_addressed_to_the_inverter(self):
        a = _adapter()

        async def _call(*args, **kw):
            return None
        a._hass.services.async_call = MagicMock(side_effect=_call)
        with _registry(_prod_shaped()):
            await a.command_release_export()
        assert a._hass.services.async_call.call_args.args[2]["device_id"] == INV

    async def test_no_inverter_found_refuses_and_says_so(self):
        a = _adapter(battery_device="")
        with _registry({}):
            with pytest.raises(NotImplementedError):
                await a.command_limit_export(0.0)


# ── (#908) the release must restore the mode SEM FOUND, not "Unlimited" ──

def _with_mode(mode_state):
    a = _adapter(config={"export_device_id": INV,
                         "export_control_readback_entity": "sensor.apc",
                         "export_guard_override_external": True})
    a._hass.states.get = MagicMock(return_value=mode_state)
    calls = []

    async def _call(domain, service, data, *args, **kw):
        calls.append((domain, service, dict(data)))
    a._hass.services.async_call = MagicMock(side_effect=_call)
    return a, calls


@pytest.mark.asyncio
class TestTheReleaseRestoresWhatWasFound:
    """PROD's SUN2000 has sat in DI Active Scheduling — the grid operator's
    ripple-control receiver on the dry contacts — for its whole history.
    ACTIVE_POWER_CONTROL_MODE is ONE register with five mutually exclusive
    values, and `reset_maximum_feed_grid_power` is documented by the
    integration as *"Set Active Power Control to 'Unlimited'"*. So the old
    release would have taken the inverter OUT of the operator's scheme and
    left it there, with nothing in SEM aware of it."""

    async def test_di_scheduling_is_put_back_not_unlimited(self):
        a, calls = _with_mode(_State("DI Active Scheduling", watt=0, percent=0.0))
        await a.command_limit_export(0.0)
        await a.command_release_export()
        assert calls[-1][1] == "set_di_active_power_scheduling", calls
        assert calls[-1][1] != "reset_maximum_feed_grid_power"

    async def test_an_unlimited_inverter_still_gets_the_plain_reset(self):
        a, calls = _with_mode(_State("Unlimited"))
        await a.command_limit_export(0.0)
        await a.command_release_export()
        assert calls[-1][1] == "reset_maximum_feed_grid_power"

    async def test_a_watt_cap_is_put_back_at_its_own_number(self):
        a, calls = _with_mode(_State("Limited to 7000W", watt=7000, percent=70.0))
        await a.command_limit_export(0.0)
        await a.command_release_export()
        assert calls[-1][1] == "set_maximum_feed_grid_power"
        assert calls[-1][2]["power"] == 7000

    async def test_a_percent_cap_is_put_back_at_its_own_number(self):
        a, calls = _with_mode(_State("Limited to 60.0%", watt=0, percent=60.0))
        await a.command_limit_export(0.0)
        await a.command_release_export()
        assert calls[-1][1] == "set_maximum_feed_grid_power_percent"
        assert calls[-1][2]["power_percentage"] == 60.0

    async def test_an_unreadable_mode_takes_the_cap_off_in_a_dialect_that_lands(self):
        """Unknown is not a licence to invent — but it is also no excuse to
        ask for something the hardware refuses. Measured on the reference
        SUN2000 (17.09.2026): `reset_maximum_feed_grid_power` (mode 0,
        Unlimited) returns cleanly and the register then reads DI Active
        Scheduling — a value that call never wrote. Mode 7 at 100 % is the
        same intent — 100 % of nominal IS the inverter's maximum — and it
        lands."""
        a, calls = _with_mode(None)
        with _registry(_prod_shaped()):
            await a.command_limit_export(0.0)
            await a.command_release_export()
        assert calls[-1][1] == "set_maximum_feed_grid_power_percent"
        assert calls[-1][2]["power_percentage"] == 100.0
        assert calls[-1][1] != "reset_maximum_feed_grid_power"

    async def test_the_prior_is_captured_before_the_cut_not_after(self):
        """Capture after the write would record SEM's own 'Zero Power'."""
        a, calls = _with_mode(_State("DI Active Scheduling"))
        await a.command_limit_export(0.0)
        assert a._export_prior_mode[0] == "DI Active Scheduling"
        assert calls[0][1] == "set_zero_power_grid_connection"

    async def test_the_recipe_persisted_for_a_restart_carries_the_same_mode(self):
        """A restart replays the RECIPE, so it must name the prior mode too —
        otherwise the cut outlives SEM and the hand-back is still wrong."""
        a, _ = _with_mode(_State("DI Active Scheduling"))
        await a.command_limit_export(0.0)
        assert a.export_release_recipe()["service"] == "set_di_active_power_scheduling"


@pytest.mark.asyncio
class TestTheRestoreSurvivesARestart:
    """The gap the first version of the capture left open, caught on PROD
    asking whether the claim was even right.

    `_export_prior_mode` lives in memory. After a restart, `adopt_export_prior`
    takes over the cut from the store — and the base dropped the recipe on the
    floor, so `export_release_recipe()` had nothing to go on and fell back to
    `reset_maximum_feed_grid_power`, i.e. Unlimited. Re-deriving it is not an
    option either: by then the inverter reports SEM's own "Zero Power".
    The previous lifetime's capture is the only record of what was found."""

    def _restarted(self, stored_service):
        a = _adapter(config={"export_device_id": INV})
        a.adopt_export_prior({"domain": "huawei_solar", "service": stored_service,
                              "data": {"device_id": INV}})
        return a

    def test_an_adopted_di_cut_is_released_back_into_di(self):
        a = self._restarted("set_di_active_power_scheduling")
        assert a.export_release_recipe()["service"] == "set_di_active_power_scheduling"

    def test_it_does_not_fall_back_to_unlimited(self):
        """The defect in one line."""
        a = self._restarted("set_di_active_power_scheduling")
        assert a.export_release_recipe()["service"] != "reset_maximum_feed_grid_power"

    def test_an_adopted_watt_cap_keeps_its_number(self):
        a = _adapter(config={"export_device_id": INV})
        a.adopt_export_prior({"domain": "huawei_solar",
                              "service": "set_maximum_feed_grid_power",
                              "data": {"device_id": INV, "power": 7000}})
        r = a.export_release_recipe()
        assert r["service"] == "set_maximum_feed_grid_power" and r["data"]["power"] == 7000

    def test_an_adopted_cut_still_counts_as_held(self):
        a = self._restarted("set_di_active_power_scheduling")
        assert a.holds_export_cut() is True

    async def test_the_release_writes_the_adopted_mode(self):
        a = self._restarted("set_di_active_power_scheduling")
        calls = []

        async def _call(domain, service, data, *args, **kw):
            calls.append((domain, service, dict(data)))
        a._hass.services.async_call = MagicMock(side_effect=_call)
        await a.command_release_export()
        assert calls[-1][1] == "set_di_active_power_scheduling", calls

    async def test_a_released_adapter_forgets_the_adopted_recipe(self):
        """Otherwise the NEXT cut would restore a mode from two lifetimes ago."""
        a = self._restarted("set_di_active_power_scheduling")

        async def _call(*args, **kw):
            return None
        a._hass.services.async_call = MagicMock(side_effect=_call)
        await a.command_release_export()
        assert a._adopted_recipe is None
        assert a.holds_export_cut() is False


# ── (#955) "I could not ask" is its own answer, not "no" ──

@pytest.mark.asyncio
class TestAnUnreadableModeIsNotPermission:
    """`.175`'s `sensor.inverter_active_power_control` sat `unavailable` from
    05:37 to 07:49 on 17.09 while the inverter was under DI Active Scheduling
    the whole time. The old `_external_scheduling` lower-cased that string,
    matched none of `_EXTERNAL_MODES`, and returned False — so the guard
    would have cut, believing the inverter free, and replaced the operator's
    mode. Three states, and the cut refuses on two of them."""

    def _no_override(self, mode_state):
        a = _adapter(config={"export_device_id": INV,
                             "export_control_readback_entity": "sensor.apc"})
        a._hass.states.get = MagicMock(return_value=mode_state)

        async def _call(*args, **kw):
            return None
        a._hass.services.async_call = MagicMock(side_effect=_call)
        return a

    async def test_an_unavailable_readback_refuses_the_cut(self):
        a = self._no_override(_State("unavailable"))
        with _registry(_prod_shaped()):
            with pytest.raises(NotImplementedError, match="cannot read"):
                await a.command_limit_export(0.0)
        a._hass.services.async_call.assert_not_called()

    async def test_a_readable_free_inverter_is_still_cut(self):
        a = self._no_override(_State("Unlimited"))
        with _registry(_prod_shaped()):
            await a.command_limit_export(0.0)
        assert a._hass.services.async_call.call_args.args[1] == (
            "set_zero_power_grid_connection")

    async def test_a_repeat_cut_after_sem_own_write_blinds_the_readback_is_still_a_no_op(self):
        """Live, 17.09: every write to the SUN2000 knocked the writing host's
        modbus poll out for ~60 s, readback included. The guard issues LIMIT
        once per engagement, so this cannot latch through the guard — but the
        adapter's own repeat check must sit ABOVE the mode check, or a repeat
        of SEM's own cut refuses itself with 'cannot read'."""
        a = self._no_override(_State("Unlimited"))
        with _registry(_prod_shaped()):
            await a.command_limit_export(0.0)
            a._hass.states.get = MagicMock(return_value=_State("unavailable"))
            await a.command_limit_export(0.0)          # must not raise
        assert a._hass.services.async_call.call_count == 1

    async def test_the_override_still_lets_a_deliberate_cut_through(self):
        a = self._no_override(_State("unavailable"))
        a._config["export_guard_override_external"] = True
        with _registry(_prod_shaped()):
            await a.command_limit_export(0.0)
        assert a._hass.services.async_call.called

class TestTheModeReadIsThreeState:
    def _a(self, mode_state):
        a = _adapter(config={"export_device_id": INV,
                             "export_control_readback_entity": "sensor.apc"})
        a._hass.states.get = MagicMock(return_value=mode_state)
        return a

    def test_the_three_states_are_distinguishable(self):
        for state, expected in ((_State("DI Active Scheduling"), True),
                                (_State("Unlimited"), False),
                                (_State("unavailable"), None),
                                (_State("unknown"), None),
                                (_State(""), None),
                                (None, None)):
            assert self._a(state)._external_scheduling() is expected, state

    def test_an_unread_prior_is_recorded_as_unread_not_as_a_mode(self):
        a = self._a(_State("unavailable"))
        a._capture_export_prior()
        assert a._export_prior_mode[0] == ""


# ── (#955) the readback LAGS, so a baseline is read once and kept ──

@pytest.mark.asyncio
class TestASecondCutDoesNotAdoptTheFirstOne:
    """`huawei_solar` polls the configuration registers on its own slow
    schedule: PROD's view of this mode lagged a real write by 7m55s, then by
    14m21s (measured 17.09.2026). The guard's engage/release timers are
    minutes, so a cut → release → cut lands well inside that window. If the
    second cut re-read the sensor it would see SEM's OWN `Zero Power` and
    record it as the inverter's baseline — and the release would then put the
    meter back to shut, and keep doing so. A latch, with SEM believing it had
    let go."""

    async def test_the_second_cut_keeps_the_first_captured_prior(self):
        a = _adapter(config={"export_device_id": INV,
                             "export_control_readback_entity": "sensor.apc",
                             "export_guard_override_external": True})
        calls = []

        async def _call(domain, service, data, *args, **kw):
            calls.append((domain, service, dict(data)))
        a._hass.services.async_call = MagicMock(side_effect=_call)

        a._hass.states.get = MagicMock(return_value=_State("DI Active Scheduling"))
        await a.command_limit_export(0.0)
        await a.command_release_export()
        assert calls[-1][1] == "set_di_active_power_scheduling"

        # the register now echoes SEM's own cut back — the lagging read
        a._hass.states.get = MagicMock(return_value=_State("Zero Power"))
        await a.command_limit_export(0.0)
        await a.command_release_export()
        assert calls[-1][1] == "set_di_active_power_scheduling", (
            "the second release adopted SEM's own cut as the baseline")
        assert a._export_prior_mode[0] == "DI Active Scheduling"

    async def test_an_adopted_recipe_still_wins_over_the_kept_prior(self):
        """A restart's adopted recipe is the more specific answer and keeps
        precedence — the kept prior is the within-session half of the same
        rule, not a replacement for it."""
        a = _adapter(config={"export_device_id": INV,
                             "export_control_readback_entity": "sensor.apc",
                             "export_guard_override_external": True})
        a._hass.states.get = MagicMock(return_value=_State("Unlimited"))
        a._capture_export_prior()
        a._adopted_recipe = {"domain": "huawei_solar",
                             "service": "set_di_active_power_scheduling",
                             "data": {"device_id": INV}}
        assert a.export_release_recipe()["service"] == "set_di_active_power_scheduling"

