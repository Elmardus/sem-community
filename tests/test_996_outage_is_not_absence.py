"""#996 — "this house has a forecast" is a registry fact, not a reading.

The reader's live detection wants a usable ``forecast_today`` state; a
cloud outage makes it miss. The capability asks the registry instead —
an enabled entity of a forecast integration — so an outage of any length
cannot make the forecast rows ABSENT. Only removing or disabling the
integration can, and then only after the confirm streak."""
from __future__ import annotations

import importlib.util

import pytest

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("pytest_homeassistant_custom_component") is None,
    reason="pytest-homeassistant-custom-component not installed; CI runs these",
)

from homeassistant.core import CoreState  # noqa: E402
from homeassistant.helpers import entity_registry as er  # noqa: E402

from custom_components.solar_energy_management.coordinator import coordinator as mod  # noqa: E402
from custom_components.solar_energy_management.coordinator.forecast_reader import (  # noqa: E402
    SOLCAST_ENTITIES, ForecastReader,
)
from custom_components.solar_energy_management.coordinator.install_modules import (  # noqa: E402
    Module, Presence,
)

from .test_923_real_installs import _dashboard, _minimal_entry  # noqa: E402
from .test_996_restart_race import _install_solcast, _row  # noqa: E402
from .test_996_stored_verdict import _cycle, _start, _stop, _uninstall_solcast  # noqa: E402


def _stored(entry):
    return entry.runtime_data._storage.get_capability_verdicts().get("solar_forecast")


def _clock(monkeypatch):
    now = [10_000.0]
    monkeypatch.setattr(mod, "_capability_clock", lambda: now[0])
    return now


class TestTheRegistryRead:

    @pytest.mark.asyncio
    async def test_an_installed_integration_is_present_even_while_unavailable(self, hass):
        _install_solcast(hass)
        for entity_id in SOLCAST_ENTITIES.values():
            hass.states.async_set(entity_id, "unavailable")
        reader = ForecastReader(hass)
        assert reader.installed_answer() is True
        # The live read — what control decisions use — cannot read it.
        assert reader.read_forecast().available is False

    @pytest.mark.asyncio
    async def test_a_disabled_integration_is_absent_while_running(self, hass):
        reg = er.async_get(hass)
        for role, entity_id in SOLCAST_ENTITIES.items():
            reg.async_get_or_create("sensor", "solcast_solar", f"solcast_{role}",
                                    suggested_object_id=entity_id.split(".", 1)[1],
                                    disabled_by=er.RegistryEntryDisabler.USER)
        assert ForecastReader(hass).installed_answer() is False

    @pytest.mark.asyncio
    async def test_nothing_before_running_is_not_asked(self, hass):
        hass.set_state(CoreState.starting)
        assert ForecastReader(hass).installed_answer() is None


@pytest.mark.asyncio
async def test_a_30_minute_outage_never_stores_absent_and_a_restart_keeps_the_rows(
        hass, enable_custom_integrations, monkeypatch):
    now = _clock(monkeypatch)
    _dashboard(monkeypatch)
    _install_solcast(hass)
    entry = _minimal_entry()
    entry.add_to_hass(hass)
    await _start(hass, entry, starting=False)
    assert _stored(entry) is True

    for entity_id in SOLCAST_ENTITIES.values():      # the cloud is down
        hass.states.async_set(entity_id, "unavailable")
    for _ in range(10):                              # 10 cycles over 30 minutes
        now[0] += 180.0
        await _cycle(hass, entry)
    assert _stored(entry) is True
    assert not entry.runtime_data._capability_miss_streak.get("solar_forecast")
    await _stop(hass, entry)

    _uninstall_solcast(hass)                         # and it loads late on the boot
    coordinator = await _start(hass, entry, starting=True)
    assert coordinator.setup_presence[Module.SOLAR_FORECAST] is Presence.PRESENT
    assert _row(hass) is not None


@pytest.mark.asyncio
async def test_removing_the_integration_stores_absent_after_the_streak(
        hass, enable_custom_integrations, monkeypatch):
    now = _clock(monkeypatch)
    _dashboard(monkeypatch)
    _install_solcast(hass)
    entry = _minimal_entry()
    entry.add_to_hass(hass)
    await _start(hass, entry, starting=False)
    assert _stored(entry) is True

    _uninstall_solcast(hass)                         # the user removes Solcast
    reader = entry.runtime_data._forecast_reader
    for i in range(7):
        now[0] += 120.0
        reader._installed_cache = None               # past the 60 s cache
        await _cycle(hass, entry)
        if i < 4:
            assert _stored(entry) is True, f"remembered too early, cycle {i}"
    assert _stored(entry) is False
