"""#1032 — the role crawler changes DISCOVERY only. An install that already
has a charger keeps its saved mapping, and a charger found only by roles is
never saved or driven without the user accepting it (setup's silent reseed
and the coordinator's late retry keep to the brand paths).

Proven on the real option sets of PROD and HA-TEST (read-only copies) and
the real KEBA's registry from .175."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from .integrations_rig.rig import load_capture, replay

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.mark.parametrize("fixture", ["hw_996_prod_options.json",
                                     "hw_996_hatest_options.json"])
async def test_a_saved_charger_keeps_its_mapping(hass, fixture):
    from custom_components.solar_energy_management import (
        _drop_meters_saved_as_chargers,
        _fill_state_sensors_of_own_save,
        _heal_offline_current_control_in_list,
    )
    from custom_components.solar_energy_management.hardware_detection import (
        discover_all_ev_chargers_from_registry,
    )
    opts = json.loads((FIXTURES / fixture).read_text())
    saved = copy.deepcopy(opts["ev_chargers"])
    for name in ("keba", "zaptec", "myenergi", "tesla_wall_connector",
                 "tesla_fleet"):
        await replay(hass, load_capture(name))
    discover_all_ev_chargers_from_registry(hass)
    assert _drop_meters_saved_as_chargers(hass, opts["ev_chargers"]) is None
    assert _heal_offline_current_control_in_list(hass, opts["ev_chargers"]) is None
    assert _fill_state_sensors_of_own_save(hass, opts["ev_chargers"]) is None
    assert opts["ev_chargers"] == saved


async def test_a_role_found_charger_is_offered_never_saved_silently(hass):
    from custom_components.solar_energy_management.hardware_detection import (
        discover_all_ev_chargers_from_registry,
        discover_ev_charger_from_registry,
    )
    await replay(hass, load_capture("myenergi"))
    every = discover_all_ev_chargers_from_registry(hass)
    found = [c for c in every if c.get("_found_by") == "roles"]
    assert found and found[0]["ev_charge_mode_entity"].endswith("_charge_mode")
    # setup's reseed and the late retry: nothing
    assert discover_ev_charger_from_registry(hass) == {}
    # the form the user confirms: offered
    assert discover_ev_charger_from_registry(hass, include_roles=True) == found[0]


async def test_the_state_heal_never_saves_a_role_found_charger(hass):
    """(#1126) The heal fills SEM's own save; setup never saved a charger
    only the roles found, so a save with that id is not one to fill."""
    from custom_components.solar_energy_management import (
        _fill_state_sensors_of_own_save,
        stable_discovered_charger_id,
    )
    from custom_components.solar_energy_management.hardware_detection import (
        discover_all_ev_chargers_from_registry,
    )
    await replay(hass, load_capture("goecharger"))
    roles = [c for c in discover_all_ev_chargers_from_registry(hass)
             if c.get("_found_by") == "roles"]
    assert roles and (roles[0].get("ev_connected_sensor")
                      or roles[0].get("ev_charging_sensor"))
    saved = {k: v for k, v in roles[0].items()
             if not k.startswith("_")
             and k not in ("ev_connected_sensor", "ev_charging_sensor")}
    saved["id"] = stable_discovered_charger_id(roles[0])
    assert _fill_state_sensors_of_own_save(hass, [saved]) is None


async def test_a_brand_charger_still_comes_first(hass):
    from custom_components.solar_energy_management.hardware_detection import (
        discover_ev_charger_from_registry,
    )
    await replay(hass, load_capture("myenergi"))
    await replay(hass, load_capture("keba"))
    got = discover_ev_charger_from_registry(hass, include_roles=True)
    assert got["ev_charger_service"] == "keba.set_current"


#: What the RETIRED brand paths saved, on the integration's real entities:
#: OpenEVSE's old path took the box's maximum power as the charging power and
#: no control; NRGkick's old row read @aleho's ``current_set`` shape.
OLD_OPENEVSE_SAVE = {
    "id": "openevse_deadbeeffeed", "name": "OpenEVSE",
    "ev_connected_sensor": "binary_sensor.openevse_mock_config_vehicle_connected",
    "ev_charging_power_sensor": "sensor.openevse_mock_config_shaper_maximum_power",
    "ev_charging_sensor": "sensor.openevse_mock_config_charging_status",
    "ev_total_energy_sensor": "sensor.openevse_mock_config_total_energy_usage",
    "ev_session_energy_sensor": "sensor.openevse_mock_config_usage_this_session",
}
OLD_NRGKICK_SAVE = {
    "id": "nrgkick_nrg", "name": "NRGkick",
    "ev_current_control_entity": "number.nrgkick_test_charging_current",
    "ev_start_stop_entity": "switch.nrgkick_test_charging_enabled",
    "ev_charging_sensor": "sensor.nrgkick_test_status",
    "ev_charging_power_sensor": "sensor.nrgkick_test_total_active_power",
}


@pytest.mark.parametrize("name,saved", [("openevse", OLD_OPENEVSE_SAVE),
                                        ("nrgkick", OLD_NRGKICK_SAVE)])
async def test_a_charger_the_old_path_saved_keeps_its_mapping(hass, name, saved):
    from custom_components.solar_energy_management import (
        _drop_meters_saved_as_chargers,
        _heal_offline_current_control_in_list,
    )
    from custom_components.solar_energy_management.config_flow import (
        _charger_already_installed,
    )
    from custom_components.solar_energy_management.hardware_detection import (
        discover_all_ev_chargers_from_registry,
        discover_ev_charger_from_registry,
    )
    await replay(hass, load_capture(name))
    chargers = [copy.deepcopy(saved)]
    found = discover_all_ev_chargers_from_registry(hass)
    assert _drop_meters_saved_as_chargers(hass, chargers) is None
    assert _heal_offline_current_control_in_list(hass, chargers) is None
    assert chargers == [saved]
    # the add-charger step does not offer the same box again
    assert all(_charger_already_installed(c, chargers) for c in found)
    # and setup's silent reseed runs only with NO charger saved — and even
    # then never with a role-found one
    assert discover_ev_charger_from_registry(hass) == {}
