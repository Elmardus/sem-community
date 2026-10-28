"""#1213 — setup against homes from past issues.

Each case replays a real integration's output (the crawler rig, see
``integrations_rig/PINS.md``) into a real Home Assistant, gives it the
Energy Dashboard the reporter described, and walks the real config flow.
The Energy Dashboard and the sensor values are the reporter's words, not a
capture: the rig has the entities, the issue has the rest.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

from custom_components.solar_energy_management import config_flow as cf
from custom_components.solar_energy_management.ha_energy_reader import (
    EnergyDashboardConfig,
)
from custom_components.solar_energy_management.hardware_detection import (
    propose_energy_sources,
)
from custom_components.solar_energy_management.setup_check import ASK_MISSING

from .integrations_rig.rig import load_capture, replay


async def _home(hass, *names):
    for n in names:
        await replay(hass, load_capture(n))


async def _flow(hass, cfg, first=None, then=None):
    """The real flow: user step, then the step it asked for."""
    flow = cf.SolarEnergyManagementConfigFlow()
    flow.hass = hass
    flow.context = {}
    with patch.object(cf, "read_energy_dashboard_config",
                      AsyncMock(return_value=cfg)), \
            patch.object(cf, "discover_inverter_from_registry",
                         return_value=None):
        result = await flow.async_step_user(first)
        if then is not None:
            result = await getattr(
                flow, f"async_step_{result['step_id']}")(then)
    return flow, result


def _counters_only_dashboard(**kw):
    """An Energy Dashboard with energy counters and no grid power sensor
    (#1103, #1188)."""
    cfg = EnergyDashboardConfig()
    cfg.has_solar = cfg.has_grid = True
    for k, v in kw.items():
        setattr(cfg, k, v)
    return cfg


# ── #1103: SolarEdge + Envoy, grid counters but no grid power ──────────────

async def test_1103_solaredge_and_envoy_setup_asks_and_offers_the_real_meter(hass):
    from homeassistant.helpers import entity_registry as er
    await _home(hass, "solaredge", "enphase_envoy")
    # the reporter's Repair named SEM's own result as the grid meter
    er.async_get(hass).async_get_or_create(
        "sensor", "solar_energy_management", "grid_import_power",
        suggested_object_id="sem_grid_import_power",
        original_device_class="power")
    hass.states.async_set("sensor.sem_grid_import_power", "0",
                          {"unit_of_measurement": "W"})
    cfg = _counters_only_dashboard(solar_power="sensor.solaredge_solar_power",
                                   solar_power_list=["sensor.solaredge_solar_power"])

    flow, shown = await _flow(hass, cfg)

    assert shown["step_id"] == "pick_grid"
    assert flow._check().ask == ASK_MISSING
    # the offer is the SolarEdge meter, never SEM's own sensor
    assert flow._check().other_grid == "sensor.solaredge_grid_power"
    assert shown["description_placeholders"]["solar"] == "1\u202f500 W"

    flow, done = await _flow(
        hass, cfg, then={"grid_power_sensor": "sensor.solaredge_grid_power"})
    assert done["step_id"] == "hardware"
    assert flow._data["grid_power_sensor"] == "sensor.solaredge_grid_power"


async def test_1103_leaving_the_picker_empty_still_installs(hass):
    await _home(hass, "solaredge", "enphase_envoy")
    flow, done = await _flow(hass, _counters_only_dashboard(), then={})
    assert done["step_id"] == "hardware"
    assert not flow._data.get("grid_power_sensor")


# ── the crawler on homes it used to get wrong ──────────────────────────────

async def test_an_envoy_alone_proposes_no_battery_it_cannot_tell(hass):
    """Two AC battery modules and an Encharge match "battery". Picking the
    first one would show a single module as the whole battery."""
    await _home(hass, "enphase_envoy")
    props = propose_energy_sources(hass)
    assert "battery_power_sensor" not in props


async def test_a_phase_of_the_battery_is_never_the_battery(hass):
    """SMA publishes the battery charge per phase (_a, _b, _c) and as a
    total; the first match was phase A."""
    await _home(hass, "sma")
    props = propose_energy_sources(hass)
    assert "battery_power_sensor" not in props
    assert props["solar_power_sensor"]["entity"] == "sensor.sma_device_name_pv_power"
    assert props["grid_import_power_sensor"]["entity"] == "sensor.sma_device_name_grid_power"


async def test_solaredge_is_found_by_its_own_declared_keys(hass):
    await _home(hass, "solaredge")
    props = propose_energy_sources(hass)
    assert {k: v["entity"] for k, v in props.items()} == {
        "solar_power_sensor": "sensor.solaredge_solar_power",
        "grid_import_power_sensor": "sensor.solaredge_grid_power",
        "battery_power_sensor": "sensor.battery_bat001_power",
        "battery_soc_sensor": "sensor.battery_bat001_state_of_charge",
    }


# ── #1051: Solis, the old inverters picked as solar ────────────────────────

async def test_1051_a_wrong_solar_sensor_is_fixed_in_setup(hass):
    """The reporter's Energy Dashboard named the old inverters, and he "did
    not know how to fix it". The picker is on the first screen now, and a
    changed pick is the only thing that is stored."""
    hass.states.async_set("sensor.solis_s6_eh3p_smartport_old_invertes_power",
                          "820", {"unit_of_measurement": "W"})
    hass.states.async_set("sensor.solis_s6_eh3p_total_dc_output_power",
                          "3410", {"unit_of_measurement": "W"})
    hass.states.async_set("sensor.solis_s6_eh3p_meter_active_power",
                          "-640", {"unit_of_measurement": "W"})
    cfg = _counters_only_dashboard(
        solar_power="sensor.solis_s6_eh3p_smartport_old_invertes_power",
        solar_power_list=["sensor.solis_s6_eh3p_smartport_old_invertes_power"],
        grid_import_power="sensor.solis_s6_eh3p_meter_active_power")

    flow, shown = await _flow(hass, cfg)
    assert shown["step_id"] == "user"
    assert shown["description_placeholders"]["solar"] == "820 W"

    flow, done = await _flow(hass, cfg, first={"own_sensors": {
        "solar_production_sensor": "sensor.solis_s6_eh3p_total_dc_output_power"}})
    assert done["step_id"] == "hardware"
    assert flow._data["solar_production_sensor"] == \
        "sensor.solis_s6_eh3p_total_dc_output_power"
    assert "grid_power_sensor" not in flow._data


# ── review findings (round 3) ──────────────────────────────────────────────

async def test_pressing_submit_on_the_grid_picker_pins_nothing(hass):
    """The crawler's guess was prefilled and "Submit to skip" stored it."""
    await _home(hass, "solaredge")
    cfg = _counters_only_dashboard()
    flow, shown = await _flow(hass, cfg)
    assert shown["step_id"] == "pick_grid"
    key = next(k for k in shown["data_schema"].schema
               if str(k) == "grid_power_sensor")
    assert key.description is None or "suggested_value" not in key.description
    flow, done = await _flow(hass, cfg, then={})
    assert not flow._data.get("grid_power_sensor")


async def test_the_other_meter_is_named_with_its_watts(hass):
    from custom_components.solar_energy_management.setup_check import ASK_GUESSED
    await _home(hass, "solaredge")
    cfg = _counters_only_dashboard(grid_import_power="sensor.solaredge_current_power")
    cfg.derived_power = {"grid": "sensor.solaredge_current_power"}
    flow, shown = await _flow(hass, cfg)
    assert shown["step_id"] == "check_grid"
    assert flow._check().ask == ASK_GUESSED
    assert "sensor.solaredge_grid_power, 250 W" in \
        shown["description_placeholders"]["why"]


async def test_a_found_sensor_the_picker_cannot_offer_never_blocks_setup(hass):
    """A dashboard sensor with no power class or unit is not in the picker's
    list; the form sends it back from the closed section."""
    hass.states.async_set("sensor.odd_pv", "900", {"unit_of_measurement": "W"})
    cfg = _counters_only_dashboard(solar_power="sensor.odd_pv",
                                   solar_power_list=["sensor.odd_pv"],
                                   grid_import_power="sensor.odd_meter")
    flow, shown = await _flow(hass, cfg)
    schema = shown["data_schema"].schema
    section_key = next(k for k in schema if str(k) == "own_sensors")
    inner = schema[section_key].schema.schema
    solar = next(k for k in inner if str(k) == "solar_production_sensor")
    allowed = set(inner[solar].config["include_entities"]) \
        if "include_entities" in inner[solar].config else None
    suggested = (solar.description or {}).get("suggested_value")
    assert suggested is None or allowed is None or suggested in allowed
