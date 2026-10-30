"""#996 — the runtime capabilities against a real Home Assistant start.

The review found the race: SEM is not ordered after the forecast
integrations, and the module verdict is frozen right after the first
refresh. On a restart where Solcast has not loaded yet, a "none found"
read would make the forecast rows ABSENT and the stale sweep would delete
their registry rows — and the user's names and settings with them.

Required: a miss read before HA is running is never ABSENT; the coordinator
asks again every cycle and reloads once when a capability turns up; a read
while running that finds nothing is still ABSENT."""
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

from custom_components.solar_energy_management.const import DOMAIN  # noqa: E402
from custom_components.solar_energy_management.coordinator.forecast_reader import (  # noqa: E402
    SOLCAST_ENTITIES, SOLCAST_PLATFORM,
)
from custom_components.solar_energy_management.coordinator.install_modules import (  # noqa: E402
    Module, Presence,
)

from .test_923_real_installs import _dashboard, _minimal_entry  # noqa: E402

FORECAST_ROW = "sem_forecast_today_kwh"


def _row(hass):
    return er.async_get(hass).async_get_entity_id("sensor", DOMAIN, FORECAST_ROW)


def _install_solcast(hass):
    """What the Solcast integration leaves behind once it has loaded."""
    reg = er.async_get(hass)
    for role, entity_id in SOLCAST_ENTITIES.items():
        reg.async_get_or_create(
            "sensor", SOLCAST_PLATFORM, f"solcast_{role}",
            suggested_object_id=entity_id.split(".", 1)[1])
        hass.states.async_set(entity_id, "12.5", {"unit_of_measurement": "kWh"})


@pytest.mark.asyncio
async def test_a_restart_before_solcast_loaded_keeps_every_row(
        hass, enable_custom_integrations, monkeypatch):
    _dashboard(monkeypatch)
    entry = _minimal_entry()
    entry.add_to_hass(hass)
    # The row a previous run created — with the user's own name on it.
    reg = er.async_get(hass)
    reg.async_get_or_create("sensor", DOMAIN, FORECAST_ROW, config_entry=entry,
                            suggested_object_id="sem_forecast_today_kwh")
    reg.async_update_entity("sensor.sem_forecast_today_kwh", name="My forecast")

    hass.set_state(CoreState.starting)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    coordinator = entry.runtime_data
    assert coordinator.setup_presence[Module.SOLAR_FORECAST] is Presence.UNKNOWN
    assert coordinator.setup_presence[Module.EXPORT_LIMIT] is Presence.UNKNOWN
    assert _row(hass) == "sensor.sem_forecast_today_kwh"
    assert reg.async_get("sensor.sem_forecast_today_kwh").name == "My forecast"

    # Solcast finishes loading after SEM, then HA says it is up.
    _install_solcast(hass)
    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()
    await coordinator.async_refresh()          # the next cycle asks again
    await hass.async_block_till_done()

    assert coordinator.install_presence()[Module.SOLAR_FORECAST] is Presence.PRESENT
    assert _row(hass) == "sensor.sem_forecast_today_kwh"
    assert reg.async_get("sensor.sem_forecast_today_kwh").name == "My forecast"


@pytest.mark.asyncio
async def test_nothing_while_running_is_absent_and_a_late_install_brings_it_back(
        hass, enable_custom_integrations, monkeypatch):
    _dashboard(monkeypatch)
    entry = _minimal_entry()
    entry.add_to_hass(hass)
    assert hass.state is CoreState.running
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    # Asked while running, nothing there: ABSENT, and the row is not built.
    assert entry.runtime_data.setup_presence[Module.SOLAR_FORECAST] is Presence.ABSENT
    assert _row(hass) is None

    # The user installs Solcast; no reload by hand — the next cycle sees it.
    _install_solcast(hass)
    await hass.async_block_till_done()
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    assert entry.runtime_data.setup_presence[Module.SOLAR_FORECAST] is Presence.PRESENT
    assert _row(hass) == "sensor.sem_forecast_today_kwh"
