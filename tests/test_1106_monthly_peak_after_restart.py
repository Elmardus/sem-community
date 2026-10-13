"""#1106 — the monthly peak read 0 kW for ~10 s after each restart.

PROD 12.10 18:32, 13.10 10:10 and 14:15: right after Home Assistant
started, ``sensor.sem_monthly_consecutive_peak`` showed 0.0 kW, then its
real value (2.629 kW) one cycle later. ``async_setup_entry`` ran the first
refresh BEFORE load management was set up, so the first data set had no
load manager and the peak defaulted to 0.0 — a billed peak nobody
measured. Pinned: load management, with its saved peak, exists when the
first refresh runs, and the first published data carries the peak.
"""
from __future__ import annotations

import importlib.util

import pytest

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("pytest_homeassistant_custom_component") is None,
    reason="pytest-homeassistant-custom-component not installed; CI runs these",
)

from homeassistant.util import dt as dt_util  # noqa: E402

from custom_components.solar_energy_management.const import DOMAIN  # noqa: E402
from custom_components.solar_energy_management.coordinator.coordinator import (  # noqa: E402
    SEMCoordinator,
)


@pytest.mark.asyncio
async def test_first_data_carries_the_saved_monthly_peak(
    hass, hass_storage, sem_config_entry, enable_custom_integrations,
) -> None:
    hass_storage[f"{DOMAIN}_load_management_devices"] = {
        "version": 1, "minor_version": 1, "key": f"{DOMAIN}_load_management_devices",
        "data": {"devices": {}, "monthly_consecutive_peak": 2.629,
                 "monthly_peak_month": dt_util.now().month},
    }
    sem_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        sem_config_entry,
        options={**sem_config_entry.options, "load_management_enabled": True})

    seen = {}
    orig = SEMCoordinator.async_config_entry_first_refresh

    async def spy(self):
        lm = getattr(self, "_load_manager", None)
        seen["peak_at_first_refresh"] = (
            None if lm is None else lm._monthly_consecutive_peak)
        await orig(self)
        seen["first_data_peak"] = (self.data or {}).get("monthly_consecutive_peak")

    SEMCoordinator.async_config_entry_first_refresh = spy
    try:
        assert await hass.config_entries.async_setup(sem_config_entry.entry_id)
        await hass.async_block_till_done()
    finally:
        SEMCoordinator.async_config_entry_first_refresh = orig

    assert seen["peak_at_first_refresh"] == pytest.approx(2.629)
    assert seen["first_data_peak"] == pytest.approx(2.629)
