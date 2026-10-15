"""#1136 — a charger found after start-up was built by a second builder.

The coordinator's late retry (``_retry_ev_device_setup``) built the device
itself, with less than setup's builder in ``__init__``:

1. it built only when discovery found a charger SERVICE, so a charger run
   by a current entity, a start/stop switch or a charge-mode select (the
   ``charger_is_complete`` gate setup uses) was never built;
2. it never registered the charger with load management, and the charger
   priority rows read ``_ev_devices`` only, which it never filled;
3. the charger was not in SEM's config, so its energy counter never reached
   ``_collect_ev_counter_entities``.

The fix keeps ONE builder: the retry saves the find exactly as setup saves
its own find (``build_discovered_charger_storage``); the options update
reloads SEM and setup builds it like any saved charger.

The finds below are shapes, not brands: what the crawler hands back for a
charger with each kind of control.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from custom_components.solar_energy_management import (
    build_discovered_charger_storage,
)
from custom_components.solar_energy_management.coordinator.coordinator import (
    SEMCoordinator,
)
from custom_components.solar_energy_management.tests import ast_contracts

_DISCOVER = ("custom_components.solar_energy_management.hardware_detection"
             ".discover_ev_charger_from_registry")

POWER = "sensor.box_power"
TOTAL = "sensor.box_total_energy"

#: One find per kind of control the crawler can hand back.
FINDS = {
    "current entity": {"ev_charging_power_sensor": POWER,
                       "ev_current_control_entity": "number.box_max_current",
                       "ev_total_energy_sensor": TOTAL},
    "start/stop switch": {"ev_charging_power_sensor": POWER,
                          "ev_start_stop_entity": "switch.box_charging",
                          "ev_total_energy_sensor": TOTAL},
    "charge-mode select": {"ev_charging_power_sensor": POWER,
                           "ev_charge_mode_entity": "select.box_mode",
                           "ev_charge_mode_start": "fast",
                           "ev_charge_mode_stop": "stopped"},
    "charger service": {"ev_charging_power_sensor": POWER,
                        "ev_charger_service": "box.set_current"},
}


async def _retry(found, *, config=None, entry=None):
    hass = MagicMock()
    entry = entry if entry is not None else SimpleNamespace(
        data={"solar_power_sensor": "sensor.pv"}, options={"observer_mode": True})
    me = SimpleNamespace(hass=hass, config=dict(config or {}),
                         config_entry=entry, _ev_retry_count=1)
    with patch(_DISCOVER, return_value=dict(found)):
        await SEMCoordinator._retry_ev_device_setup(me)
    return me, hass.config_entries.async_update_entry


@pytest.mark.unit
class TestTheFindIsSavedLikeSetupsOwn:

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind", sorted(FINDS))
    async def test_every_kind_of_control_is_saved(self, kind):
        """Point 1: the retry used to want a charger service; setup takes a
        power reading and any way to act."""
        found = FINDS[kind]
        me, update = await _retry(found)
        assert update.called, f"a late charger with a {kind} was dropped"
        saved = update.call_args.kwargs
        _, _, want = build_discovered_charger_storage({}, {}, found)
        assert saved["options"]["ev_chargers"] == want
        assert saved["data"]["ev_chargers"] == want
        # what was there stays
        assert saved["data"]["solar_power_sensor"] == "sensor.pv"
        assert saved["options"]["observer_mode"] is True
        assert me._ev_retry_count == 999

    @pytest.mark.asyncio
    async def test_a_half_find_waits_for_the_next_retry(self):
        """No power reading: not a charger SEM can run. Nothing saved, the
        retry keeps going (its integration may still be loading)."""
        me, update = await _retry({"ev_start_stop_entity": "switch.box"})
        assert not update.called
        assert me._ev_retry_count == 1

    @pytest.mark.asyncio
    async def test_a_saved_charger_is_never_overwritten(self):
        me, update = await _retry(FINDS["current entity"],
                                  config={"ev_chargers": [{"id": "mine"}]})
        assert not update.called
        assert me._ev_retry_count == 999

    @pytest.mark.asyncio
    async def test_nothing_before_setup_has_an_entry(self):
        me = SimpleNamespace(hass=MagicMock(), config={}, config_entry=None,
                             _ev_retry_count=1)
        with patch(_DISCOVER, return_value=dict(FINDS["current entity"])):
            await SEMCoordinator._retry_ev_device_setup(me)
        assert not me.hass.config_entries.async_update_entry.called

    @pytest.mark.asyncio
    async def test_the_saved_charger_counts_its_energy(self):
        """Point 3: the counter list reads SEM's config, which now holds
        the saved card."""
        _, update = await _retry(FINDS["current entity"])
        saved = update.call_args.kwargs["options"]
        coord = SEMCoordinator.__new__(SEMCoordinator)
        coord.config = saved
        coord._energy_dashboard_config = None
        assert SEMCoordinator._collect_ev_counter_entities(coord) == [TOTAL]


@pytest.mark.unit
class TestOneBuilder:

    def test_only_setup_builds_a_charger_device(self):
        """Points 1-2: one production site builds a charger device — setup's
        builder, which gates on ``charger_is_complete`` and registers load
        management. A second site is where the parts drift apart again."""
        sites = [rel for rel, _, _ in ast_contracts.call_sites("CurrentControlDevice")
                 if rel.split("/")[0] in ("__init__.py", "coordinator")]
        # (features/device_registry.py builds current-controlled LOADS, not
        # chargers.)
        assert sites == ["__init__.py"], sites

    def test_the_late_retry_builds_nothing_itself(self):
        assert not ast_contracts.calls(SEMCoordinator._retry_ev_device_setup,
                                       "register_device")
