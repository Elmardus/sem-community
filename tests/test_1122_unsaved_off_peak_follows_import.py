"""#1122 — changing the import rate must not save the old one as off-peak.

With no saved off-peak rate the Tariff page shows the import rate in that
field (one price, as SEM reads it — #1040). HA sends back every shown field,
so a user who changed only the import rate from 0.30 to 0.35 saved 0.35 as
import and 0.30 as off-peak: a cheap night nobody entered.

The page is driven the way the frontend drives it: show the form, let the
schema fill every field the user did not touch, submit, then build the tariff
from what was saved.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from custom_components.solar_energy_management.config_flow import (
    OptionsFlowHandler,
)


def _flow(mock_hass, config_entry, options):
    config_entry.options = options
    flow = OptionsFlowHandler(config_entry)
    flow.hass = mock_hass
    flow.async_step_load_management = AsyncMock(return_value={"type": "form"})
    return flow


async def _show_then_submit(flow, config_entry, edits):
    with patch.object(type(flow), "config_entry",
                      new_callable=lambda: property(lambda self: config_entry)):
        shown = await flow.async_step_settings_tariff(None)
        flow.cur_step = shown
        sent = {**_as_shown(shown["data_schema"]), **edits}
        await flow.async_step_settings_tariff(dict(sent))
    return sent


def _as_shown(schema):
    """Every field the page shows with a value, as the frontend sends it."""
    out = {}
    for marker in schema.schema:
        default = getattr(marker, "default", None)
        if callable(default):
            value = default()
            if value is not None:
                out[str(marker.schema)] = value
    return out


def _saved(config_entry, flow):
    return {**config_entry.data, **config_entry.options, **flow._data}


def _tariff(mock_hass, saved):
    from custom_components.solar_energy_management.coordinator import SEMCoordinator
    return SEMCoordinator(mock_hass, {**saved, "tariff_mode": "static",
                                      "update_interval": 30})._tariff_provider


@pytest.fixture
def unsaved_night(config_entry):
    for key in ("electricity_off_peak_rate", "electricity_nt_rate"):
        config_entry.data.pop(key, None)
    return config_entry


@pytest.mark.asyncio
async def test_a_new_import_rate_opens_no_spread(mock_hass, unsaved_night):
    flow = _flow(mock_hass, unsaved_night,
                 {"tariff_mode": "static", "electricity_import_rate": 0.30})
    sent = await _show_then_submit(flow, unsaved_night,
                                   {"electricity_import_rate": 0.35})
    assert sent["electricity_off_peak_rate"] == 0.30      # what HA sends back
    saved = _saved(unsaved_night, flow)
    assert saved.get("electricity_off_peak_rate") is None
    p = _tariff(mock_hass, saved)
    assert (p.peak_rate, p.off_peak_rate) == (0.35, 0.35)


@pytest.mark.asyncio
async def test_an_entered_off_peak_rate_is_saved(mock_hass, unsaved_night):
    flow = _flow(mock_hass, unsaved_night,
                 {"tariff_mode": "static", "electricity_import_rate": 0.30})
    await _show_then_submit(flow, unsaved_night,
                            {"electricity_import_rate": 0.35,
                             "electricity_off_peak_rate": 0.20})
    assert _saved(unsaved_night, flow)["electricity_off_peak_rate"] == 0.20


@pytest.mark.asyncio
async def test_a_saved_off_peak_rate_stays_when_import_moves(mock_hass, config_entry):
    flow = _flow(mock_hass, config_entry,
                 {"tariff_mode": "static", "electricity_import_rate": 0.30,
                  "electricity_off_peak_rate": 0.30})
    await _show_then_submit(flow, config_entry, {"electricity_import_rate": 0.35})
    assert _saved(config_entry, flow)["electricity_off_peak_rate"] == 0.30


@pytest.mark.asyncio
async def test_a_saved_free_night_stays(mock_hass, config_entry):
    flow = _flow(mock_hass, config_entry,
                 {"tariff_mode": "static", "electricity_import_rate": 0.30,
                  "electricity_off_peak_rate": 0.0})
    await _show_then_submit(flow, config_entry, {})
    assert _saved(config_entry, flow)["electricity_off_peak_rate"] == 0.0
