"""#996 — the running-time verdict survives a restart, so rows do not flap.

Without it an install with no forecast got its forecast rows built on every
restart (nothing asked yet, UNKNOWN), removed on the next options reload
(asked while running, ABSENT), built again on the next restart — registry
rows deleted and recreated over and over. SEM stores the last verdict each
runtime capability had from a read taken while HA was running and falls
back to it at setup. The coordinator's per-cycle check still corrects
it; a turn to ABSENT waits for the next setup."""
from __future__ import annotations

import importlib.util

import pytest

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("pytest_homeassistant_custom_component") is None,
    reason="pytest-homeassistant-custom-component not installed; CI runs these",
)

from homeassistant.const import EVENT_HOMEASSISTANT_STARTED  # noqa: E402
from homeassistant.core import CoreState  # noqa: E402
from homeassistant.helpers import entity_registry as er  # noqa: E402

from custom_components.solar_energy_management.coordinator.forecast_reader import (  # noqa: E402
    SOLCAST_ENTITIES,
)
from custom_components.solar_energy_management.coordinator.install_modules import (  # noqa: E402
    Module, Presence,
)

from .test_923_real_installs import _dashboard, _minimal_entry  # noqa: E402
from .test_996_restart_race import FORECAST_ROW, _install_solcast, _row  # noqa: E402


@pytest.fixture(autouse=True)
def _misses_confirm_at_once(monkeypatch):
    """These tests are about what a remembered verdict does, not about how
    long a miss must hold before it is remembered (test_996_runtime_facts)."""
    from custom_components.solar_energy_management.coordinator import coordinator as mod
    monkeypatch.setattr(mod, "CAPABILITY_MISS_CONFIRM_READS", 1)
    monkeypatch.setattr(mod, "CAPABILITY_MISS_HOLD_S", 0.0)


async def _start(hass, entry, *, starting):
    """Set the entry up as a boot (HA still starting) or a live setup."""
    hass.set_state(CoreState.starting if starting else CoreState.running)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry.runtime_data


async def _finish_boot(hass, entry):
    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()
    await _cycle(hass, entry)


async def _cycle(hass, entry):
    """One coordinator cycle — where the live runtime answers are read."""
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()


async def _stop(hass, entry):
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


def _uninstall_solcast(hass):
    """Solcast not loaded yet on this boot: no entities, no states."""
    reg = er.async_get(hass)
    for entity_id in SOLCAST_ENTITIES.values():
        if reg.async_get(entity_id):
            reg.async_remove(entity_id)
        hass.states.async_remove(entity_id)


def _verdict(coordinator):
    return coordinator.setup_presence[Module.SOLAR_FORECAST]


@pytest.mark.asyncio
async def test_a_restart_after_a_stored_absent_builds_no_rows(
        hass, enable_custom_integrations, monkeypatch):
    _dashboard(monkeypatch)
    entry = _minimal_entry()
    entry.add_to_hass(hass)
    assert _verdict(await _start(hass, entry, starting=False)) is Presence.ABSENT
    await _stop(hass, entry)

    removed = []
    hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED,
                          lambda e: removed.append(e.data) if e.data.get("action") == "remove" else None)
    assert _verdict(await _start(hass, entry, starting=True)) is Presence.ABSENT
    await _finish_boot(hass, entry)
    assert _row(hass) is None
    assert not [r for r in removed if FORECAST_ROW in r.get("entity_id", "")
                or "forecast" in r.get("entity_id", "")]


@pytest.mark.asyncio
async def test_a_restart_after_a_stored_present_keeps_the_rows_through_the_race(
        hass, enable_custom_integrations, monkeypatch):
    _dashboard(monkeypatch)
    _install_solcast(hass)
    entry = _minimal_entry()
    entry.add_to_hass(hass)
    assert _verdict(await _start(hass, entry, starting=False)) is Presence.PRESENT
    reg = er.async_get(hass)
    reg.async_update_entity(_row(hass), name="My forecast")
    await _stop(hass, entry)

    _uninstall_solcast(hass)                       # Solcast loads after SEM
    coordinator = await _start(hass, entry, starting=True)
    assert _verdict(coordinator) is Presence.PRESENT
    assert reg.async_get(_row(hass)).name == "My forecast"

    _install_solcast(hass)
    await _finish_boot(hass, entry)
    assert entry.runtime_data.install_presence()[Module.SOLAR_FORECAST] is Presence.PRESENT
    assert reg.async_get(_row(hass)).name == "My forecast"


@pytest.mark.asyncio
async def test_a_new_forecast_after_a_stored_absent_comes_back_in_the_next_cycle(
        hass, enable_custom_integrations, monkeypatch):
    _dashboard(monkeypatch)
    entry = _minimal_entry()
    entry.add_to_hass(hass)
    await _start(hass, entry, starting=False)
    await _stop(hass, entry)
    assert _verdict(await _start(hass, entry, starting=True)) is Presence.ABSENT
    await _finish_boot(hass, entry)
    assert _row(hass) is None

    _install_solcast(hass)                         # the user installs Solcast
    await _cycle(hass, entry)
    assert _verdict(entry.runtime_data) is Presence.PRESENT
    assert _row(hass) is not None


@pytest.mark.asyncio
async def test_no_flip_flop_across_restart_reload_restart(
        hass, enable_custom_integrations, monkeypatch):
    _dashboard(monkeypatch)
    entry = _minimal_entry()
    entry.add_to_hass(hass)
    created = []
    hass.bus.async_listen(
        er.EVENT_ENTITY_REGISTRY_UPDATED,
        lambda e: created.append(e.data["entity_id"])
        if e.data.get("action") == "create" and "forecast_today_kwh" in e.data.get("entity_id", "")
        else None)

    await _start(hass, entry, starting=False)             # first run, asked while running
    await _stop(hass, entry)
    seen = [_verdict(await _start(hass, entry, starting=True))]   # restart
    await _finish_boot(hass, entry)
    assert await hass.config_entries.async_reload(entry.entry_id)  # options reload
    await hass.async_block_till_done()
    seen.append(_verdict(entry.runtime_data))
    await _stop(hass, entry)
    seen.append(_verdict(await _start(hass, entry, starting=True)))  # restart again
    await _finish_boot(hass, entry)

    assert seen == [Presence.ABSENT] * 3
    assert created == []
    assert _row(hass) is None


@pytest.mark.asyncio
async def test_a_restart_right_after_a_one_cycle_blip_keeps_the_rows(
        hass, enable_custom_integrations, monkeypatch):
    from custom_components.solar_energy_management.coordinator import coordinator as mod
    monkeypatch.setattr(mod, "CAPABILITY_MISS_CONFIRM_READS", 6)   # the real rule
    monkeypatch.setattr(mod, "CAPABILITY_MISS_HOLD_S", 600.0)
    _dashboard(monkeypatch)
    _install_solcast(hass)
    entry = _minimal_entry()
    entry.add_to_hass(hass)
    assert _verdict(await _start(hass, entry, starting=False)) is Presence.PRESENT

    # Solcast's forecast is unavailable for one cycle: the reader cannot
    # read it, but the integration is still installed — no miss at all.
    for entity_id in SOLCAST_ENTITIES.values():
        hass.states.async_set(entity_id, "unavailable")
    await _cycle(hass, entry)
    assert not entry.runtime_data._capability_miss_streak.get("solar_forecast")
    await _stop(hass, entry)

    _uninstall_solcast(hass)                       # and it loads late on the boot
    coordinator = await _start(hass, entry, starting=True)
    assert _verdict(coordinator) is Presence.PRESENT
    assert _row(hass) is not None
