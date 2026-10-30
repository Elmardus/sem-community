"""#1117 — a Deye goes back to its own work mode after SEM lifts the export cut.

The export verbs compared the whole HA State object with an option name, so
"already at Zero Export" and "save the old mode" were never true: the old mode
was never saved, the release had nothing to put back, and every cut cycle
rewrote the select. Run through the real export seam (``actuate_export``) with
a select that follows its writes, as the hardware does.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.solar_energy_management.coordinator.actuate_export import (
    actuate_export,
)
from custom_components.solar_energy_management.coordinator.battery_adapters.deye import (
    DeyeBatteryAdapter,
)
from custom_components.solar_energy_management.coordinator.charger_types import (
    ExportDecision, ExportIntent,
)

ENT = "select.deye_system_work_mode"
OPTIONS = ["Selling First", "Zero Export To Load", "Zero Export To CT"]


def _rig(start):
    """A select whose state follows select_option, like the real entity."""
    states = {ENT: SimpleNamespace(state=start, attributes={"options": OPTIONS})}
    hass = MagicMock()
    hass.states.get = MagicMock(side_effect=lambda e: states.get(e))

    async def _call(domain, service, data, blocking=False):
        if service == "select_option":
            states[data["entity_id"]] = SimpleNamespace(
                state=data["option"], attributes={"options": OPTIONS})
    hass.services.async_call = AsyncMock(side_effect=_call)
    a = DeyeBatteryAdapter(hass, {
        "battery_platform": "deye",
        "deye_system_work_mode_control": True,
        "deye_system_work_mode_entity": ENT,
        "deye_system_work_mode_selling_option": "Selling First",
        "deye_system_work_mode_zero_load_option": "Zero Export To Load",
        "deye_system_work_mode_zero_ct_option": "Zero Export To CT",
    })
    a._observer_mode = False
    a._actuation_enabled = True
    return hass, a, states


def _writes(hass):
    return [c.args[2]["option"] for c in hass.services.async_call.await_args_list
            if c.args[1] == "select_option"]


LIMIT = ExportDecision(intent=ExportIntent.LIMIT, watts=0.0, reason="cut")
RELEASE = ExportDecision(intent=ExportIntent.RELEASE, watts=0.0, reason="lift")


@pytest.mark.asyncio
async def test_cut_then_release_puts_the_old_mode_back():
    hass, a, states = _rig("Selling First")
    assert await actuate_export(LIMIT, a) is None
    assert states[ENT].state == "Zero Export To Load"
    assert await actuate_export(RELEASE, a) is None
    assert states[ENT].state == "Selling First"
    assert _writes(hass) == ["Zero Export To Load", "Selling First"]


@pytest.mark.asyncio
async def test_a_held_cut_writes_once_not_every_cycle():
    hass, a, _ = _rig("Zero Export To CT")
    for _ in range(5):
        assert await actuate_export(LIMIT, a) is None
    assert _writes(hass) == ["Zero Export To Load"]
    await actuate_export(RELEASE, a)
    assert _writes(hass) == ["Zero Export To Load", "Zero Export To CT"]


@pytest.mark.asyncio
async def test_the_recipe_names_the_saved_mode():
    _, a, _ = _rig("Selling First")
    await actuate_export(LIMIT, a)
    recipe = a.export_release_recipe()
    assert recipe["data"] == {"entity_id": ENT, "option": "Selling First"}
    row = a.export_dry_run(ExportIntent.RELEASE, 0.0)
    assert row["data"]["option"] == "Selling First"


def test_dry_run_on_an_uncut_rig_names_the_option_not_the_state_object():
    _, a, _ = _rig("Zero Export To CT")
    row = a.export_dry_run(ExportIntent.RELEASE, 0.0)
    assert row["data"]["option"] == "Zero Export To CT"


@pytest.mark.asyncio
async def test_an_unreadable_select_saves_nothing():
    hass, a, states = _rig("unavailable")
    await actuate_export(LIMIT, a)
    assert a.export_release_recipe() is None
