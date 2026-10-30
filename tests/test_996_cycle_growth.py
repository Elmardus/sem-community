"""#996 — the coordinator's own cycle brings a capability back.

The review reproduced it: a registry listener that only hears "create"
misses an enabled Solcast entity and a renamed export-limit entity (both
are "update"). The coordinator's reader then sees Solcast and pacing runs
off it, yet the rows the platforms were built without never come back.

The live runtime answer is read every cycle anyway, so the growth check
runs there: a capability PRESENT live that the platforms were built
without reloads once, rate-limited per entry. And a live HIT counts at any
time — a forecast installed while HA was down is PRESENT at setup even
when the stored verdict says no."""
from __future__ import annotations

import importlib.util
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from custom_components.solar_energy_management.coordinator.coordinator import SEMCoordinator
from custom_components.solar_energy_management.coordinator.install_modules import (
    Module, Presence, all_unknown,
)

_PHACC = importlib.util.find_spec("pytest_homeassistant_custom_component") is not None


@pytest.fixture(autouse=True)
def _misses_confirm_at_once(monkeypatch):
    """These tests are about what a remembered verdict does, not about how
    long a miss must hold before it is remembered (test_996_runtime_facts)."""
    from custom_components.solar_energy_management.coordinator import coordinator as mod
    monkeypatch.setattr(mod, "CAPABILITY_MISS_CONFIRM_READS", 1)
    monkeypatch.setattr(mod, "CAPABILITY_MISS_HOLD_S", 0.0)


class TestTheRateLimitIsPerEntry:

    def _stub(self, entry_id, hass):
        stub = MagicMock()
        stub.setup_presence = {**all_unknown(), Module.SOLAR_FORECAST: Presence.ABSENT}
        stub.install_presence.return_value = {
            **all_unknown(), Module.SOLAR_FORECAST: Presence.PRESENT}
        stub.config_entry = SimpleNamespace(entry_id=entry_id)
        stub.hass = hass
        return stub

    def test_two_entries_do_not_starve_each_other(self):
        hass = MagicMock()
        hass.data = {}
        a, b = self._stub("entry_a", hass), self._stub("entry_b", hass)
        SEMCoordinator._check_module_growth(a)
        SEMCoordinator._check_module_growth(b)
        calls = [c.args[0] for c in hass.config_entries.async_schedule_reload.call_args_list]
        assert calls == ["entry_a", "entry_b"]

    def test_one_entry_reloads_once_per_interval(self):
        hass = MagicMock()
        hass.data = {}
        a = self._stub("entry_a", hass)
        for _ in range(5):
            SEMCoordinator._check_module_growth(a)
        assert hass.config_entries.async_schedule_reload.call_count == 1


if _PHACC:
    from homeassistant.core import CoreState
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er

    from custom_components.solar_energy_management.const import DOMAIN
    from custom_components.solar_energy_management.coordinator import sensor_reader
    from custom_components.solar_energy_management.coordinator.forecast_reader import (
        SOLCAST_ENTITIES, SOLCAST_PLATFORM,
    )

    from .test_923_real_installs import _dashboard, _minimal_entry
    from .test_996_restart_race import _install_solcast, _row


async def _setup(hass, entry, *, starting=False):
    hass.set_state(CoreState.starting if starting else CoreState.running)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry.runtime_data


async def _cycle(hass, entry):
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()


def _exists(hass, platform, unique_id):
    return er.async_get(hass).async_get_entity_id(platform, DOMAIN, unique_id) is not None


@pytest.mark.skipif(not _PHACC, reason="pytest-homeassistant-custom-component not installed")
@pytest.mark.asyncio
async def test_enabling_a_disabled_forecast_entity_brings_the_rows_back(
        hass, enable_custom_integrations, monkeypatch):
    _dashboard(monkeypatch)
    reg = er.async_get(hass)
    for role, entity_id in SOLCAST_ENTITIES.items():
        reg.async_get_or_create(
            "sensor", SOLCAST_PLATFORM, f"solcast_{role}",
            suggested_object_id=entity_id.split(".", 1)[1],
            disabled_by=er.RegistryEntryDisabler.USER)
    entry = _minimal_entry()
    entry.add_to_hass(hass)
    first = await _setup(hass, entry)
    assert first.setup_presence[Module.SOLAR_FORECAST] is Presence.ABSENT
    assert _row(hass) is None

    for entity_id in SOLCAST_ENTITIES.values():      # an "update", not a "create"
        reg.async_update_entity(entity_id, disabled_by=None)
        hass.states.async_set(entity_id, "12.5", {"unit_of_measurement": "kWh"})
    await _cycle(hass, entry)

    assert entry.runtime_data is not first               # reloaded once
    assert entry.runtime_data.setup_presence[Module.SOLAR_FORECAST] is Presence.PRESENT
    assert _row(hass) is not None

    # No reload loop: later cycles leave the entry alone.
    settled = entry.runtime_data
    for _ in range(3):
        await _cycle(hass, entry)
    assert entry.runtime_data is settled


@pytest.mark.skipif(not _PHACC, reason="pytest-homeassistant-custom-component not installed")
@pytest.mark.asyncio
async def test_renaming_to_an_export_limit_word_brings_the_guard_back(
        hass, enable_custom_integrations, monkeypatch):
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    _dashboard(monkeypatch)
    monkeypatch.setattr(sensor_reader, "EXPORT_LIMIT_RESCAN_S", 0.0)
    inverter_entry = MockConfigEntry(domain="huawei_solar", data={})
    inverter_entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=inverter_entry.entry_id, identifiers={("huawei_solar", "inv1")})
    reg = er.async_get(hass)
    reg.async_get_or_create("sensor", "huawei_solar", "solar_power", device_id=device.id,
                            suggested_object_id="test_solar_power")
    reg.async_get_or_create("number", "huawei_solar", "power_cap", device_id=device.id,
                            suggested_object_id="inverter_power_cap")
    entry = _minimal_entry()
    entry.add_to_hass(hass)
    first = await _setup(hass, entry)
    assert first.setup_presence[Module.EXPORT_LIMIT] is Presence.ABSENT
    assert not _exists(hass, "switch", "sem_export_guard_enabled")

    reg.async_update_entity("number.inverter_power_cap",
                            new_entity_id="number.inverter_export_limit")
    await _cycle(hass, entry)

    assert entry.runtime_data is not first
    assert entry.runtime_data.setup_presence[Module.EXPORT_LIMIT] is Presence.PRESENT
    assert _exists(hass, "switch", "sem_export_guard_enabled")


@pytest.mark.skipif(not _PHACC, reason="pytest-homeassistant-custom-component not installed")
@pytest.mark.asyncio
async def test_installed_while_down_is_present_at_setup_despite_a_stored_no(
        hass, enable_custom_integrations, monkeypatch):
    _dashboard(monkeypatch)
    entry = _minimal_entry()
    entry.add_to_hass(hass)
    assert (await _setup(hass, entry)).setup_presence[Module.SOLAR_FORECAST] is Presence.ABSENT
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    _install_solcast(hass)                               # while SEM was down
    coordinator = await _setup(hass, entry, starting=True)
    assert coordinator.setup_presence[Module.SOLAR_FORECAST] is Presence.PRESENT
    assert _row(hass) is not None
