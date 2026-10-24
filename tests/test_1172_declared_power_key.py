"""#1172 — a P1 meter's power is found in every language.

@mrietdijk80-jpg (SolarEdge + AlphaESS, a HomeWizard P1 meter, Dutch HA): the
Energy Flow Sankey drew no line in or out of the Grid node — no Grid → House,
no Grid → Battery, no Solar → Grid — while the Grid total (31.3 kWh) was right.

The totals come from the meter's kWh counters. The lines come from SEM's live
power flows, and SEM read 0 W from the grid all day. The Energy Dashboard's
grid entry had the counters but no power sensor, so SEM looked for one on the
counter's device by entity_id WORDS (``_POWER_DERIVE_RULES``, bug class 10).
HomeWizard names the meter's net power by its device class alone:
``sensor.p1_meter_power`` in English, ``sensor.p1_meter_vermogen`` in Dutch.
No word in the list fits either, and the split-grid search finds no pair on a
meter that has one signed sensor. Grid power stayed 0, so every grid flow did.

The fix asks what the integration CALLS the sensor — its entity key in the
registry's unique_id (``<serial>_active_power_w``), which is the same in every
language — before it asks the entity_id. Solar and battery get the same tier
(HomeWizard sockets and kWh meters count those too).
"""
from __future__ import annotations

import pytest
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.solar_energy_management.coordinator.flow_calculator import (
    FlowCalculator,
)
from custom_components.solar_energy_management.coordinator.sensor_reader import (
    SensorReader,
)
from custom_components.solar_energy_management.ha_energy_reader import (
    _POWER_DERIVE_RULES,
    _find_power_sensor_on_device,
    _parse_prefs,
)

SERIAL = "5c2fafabcdef"
IMPORT = "sensor.p1_meter_energy_import"      # the reporter's own ids
EXPORT = "sensor.p1_meter_energie_export"
POWER = "sensor.p1_meter_vermogen"

_W = {"device_class": "power", "unit_of_measurement": "W",
      "state_class": "measurement"}
_KWH = {"device_class": "energy", "unit_of_measurement": "kWh",
        "state_class": "total_increasing"}


def _device(hass, platform="homewizard", serial=SERIAL, name="P1 meter"):
    entry = MockConfigEntry(domain=platform, unique_id=f"HWE-P1_{serial}")
    entry.add_to_hass(hass)
    dev = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(platform, serial)}, name=name)
    return entry, dev


def _entity(hass, entry, dev, entity_id, key, *, platform="homewizard",
            device_class=None, translation_key=None, disabled=False):
    domain, object_id = entity_id.split(".", 1)
    return er.async_get(hass).async_get_or_create(
        domain, platform, f"{entry.unique_id}_{key}",
        suggested_object_id=object_id, config_entry=entry, device_id=dev.id,
        original_device_class=device_class, translation_key=translation_key,
        disabled_by=(er.RegistryEntryDisabler.INTEGRATION if disabled else None),
    )


def _p1_meter(hass, power=POWER, *, power_state=True, platform="homewizard"):
    """A three-phase HomeWizard P1 meter, as its integration registers it."""
    entry, dev = _device(hass, platform=platform)
    base = power.rsplit("_", 1)[0]
    rows = [
        (IMPORT, "total_power_import_kwh", "energy", "total_energy_import_kwh"),
        (EXPORT, "total_power_export_kwh", "energy", "total_energy_export_kwh"),
        (power, "active_power_w", "power", None),
        # the near-misses on the same device: each is power, none is the net
        (f"{power}_fase_1", "active_power_l1_w", "power", "active_power_phase_w"),
        (f"{power}_fase_2", "active_power_l2_w", "power", "active_power_phase_w"),
        (f"{power}_fase_3", "active_power_l3_w", "power", "active_power_phase_w"),
        (f"{base}_gemiddeld_vermogen", "active_power_average_w", "power",
         "active_power_average_w"),
        (f"{base}_piekvermogen", "monthly_power_peak_w", "power",
         "monthly_power_peak_w"),
    ]
    for eid, key, dc, tk in rows:
        _entity(hass, entry, dev, eid, key, platform=platform,
                device_class=dc, translation_key=tk)
        if eid == power and not power_state:
            continue
        hass.states.async_set(eid, "12.0" if dc == "energy" else "800",
                              _KWH if dc == "energy" else _W)
    return entry, dev


def _rule(kind, *, declared=True):
    rule = dict(_POWER_DERIVE_RULES[kind])
    if not declared:
        rule.pop("declared", None)
    return rule


# ── the meter's own word, in every language ──────────────────────────────

@pytest.mark.parametrize("slug", [
    "power",        # English
    "vermogen",     # Dutch — the reporter
    "leistung",     # German
    "effekt",       # Swedish / Danish / Norwegian (#628's install)
    "puissance",    # French
    "potenza",      # Italian
])
async def test_the_meters_power_is_found_in_every_language(hass, slug):
    power = f"sensor.p1_meter_{slug}"
    _p1_meter(hass, power)
    assert _find_power_sensor_on_device(hass, IMPORT, _rule("grid")) == power


async def test_no_word_in_the_list_finds_it(hass):
    """The bug itself: the name rules alone answer nothing for this meter —
    which is why the reporter's grid read 0 W."""
    _p1_meter(hass)
    assert _find_power_sensor_on_device(
        hass, IMPORT, _rule("grid", declared=False)) is None


async def test_before_the_meter_has_loaded_the_registry_answers(hass):
    """Cold start: SEM often sets up before the meter's integration. The
    registry is loaded already; the grid has no retry for a missed pick."""
    _p1_meter(hass, power_state=False)
    assert hass.states.get(POWER) is None
    assert _find_power_sensor_on_device(hass, IMPORT, _rule("grid")) == POWER


async def test_a_registry_record_that_is_not_power_is_refused(hass):
    entry, dev = _device(hass)
    _entity(hass, entry, dev, IMPORT, "total_power_import_kwh", device_class="energy")
    _entity(hass, entry, dev, POWER, "active_power_w", device_class="energy")
    hass.states.async_set(IMPORT, "12.0", _KWH)
    assert _find_power_sensor_on_device(hass, IMPORT, _rule("grid")) is None


async def test_a_live_state_that_is_not_power_is_refused(hass):
    _p1_meter(hass)
    hass.states.async_set(POWER, "12.0", _KWH)
    assert _find_power_sensor_on_device(hass, IMPORT, _rule("grid")) is None


async def test_the_key_counts_only_for_the_integration_that_declares_it(hass):
    """``active_power_w`` is HomeWizard's word. Another integration's entity
    with the same unique_id tail is not a declaration."""
    _p1_meter(hass, platform="some_other_meter")
    assert _find_power_sensor_on_device(hass, IMPORT, _rule("grid")) is None


async def test_a_disabled_declared_sensor_is_not_taken(hass):
    entry, dev = _device(hass)
    _entity(hass, entry, dev, IMPORT, "total_power_import_kwh", device_class="energy")
    _entity(hass, entry, dev, POWER, "active_power_w", device_class="power",
            disabled=True)
    hass.states.async_set(IMPORT, "12.0", _KWH)
    assert _find_power_sensor_on_device(hass, IMPORT, _rule("grid")) is None


# ── the siblings: a HomeWizard socket or kWh meter on solar or a battery ──

async def test_each_source_takes_its_own_key_from_a_kwh_meter(hass):
    """A kWh meter gives its net power AND that power × −1 (production,
    positive while producing). Grid and battery want the net power; solar
    wants production, which is the sign SEM reads solar in."""
    entry, dev = _device(hass, serial="kwh3abc", name="kWh meter")
    _entity(hass, entry, dev, "sensor.kwh_meter_energie_import",
            "total_power_import_kwh", device_class="energy")
    _entity(hass, entry, dev, "sensor.kwh_meter_vermogen", "active_power_w",
            device_class="power")
    _entity(hass, entry, dev, "sensor.kwh_meter_productievermogen",
            "active_production_power_w", device_class="power",
            translation_key="active_production_power_w")
    hass.states.async_set("sensor.kwh_meter_energie_import", "3.0", _KWH)
    hass.states.async_set("sensor.kwh_meter_vermogen", "-420", _W)
    hass.states.async_set("sensor.kwh_meter_productievermogen", "420", _W)
    counter = "sensor.kwh_meter_energie_import"
    assert (_find_power_sensor_on_device(hass, counter, _rule("grid"))
            == "sensor.kwh_meter_vermogen")
    assert (_find_power_sensor_on_device(hass, counter, _rule("battery"))
            == "sensor.kwh_meter_vermogen")
    assert (_find_power_sensor_on_device(hass, counter, _rule("solar"))
            == "sensor.kwh_meter_productievermogen")


async def test_a_name_match_still_works_where_no_key_is_declared(hass):
    """The new tier adds; it takes nothing away from a brand found by name."""
    entry, dev = _device(hass, platform="solax", serial="sx1", name="SolaX")
    _entity(hass, entry, dev, "sensor.solax_grid_import_total", "gi",
            platform="solax", device_class="energy")
    _entity(hass, entry, dev, "sensor.solax_measured_power", "mp",
            platform="solax", device_class="power")
    hass.states.async_set("sensor.solax_grid_import_total", "3.0", _KWH)
    hass.states.async_set("sensor.solax_measured_power", "-300", _W)
    assert _find_power_sensor_on_device(
        hass, "sensor.solax_grid_import_total", _rule("grid")
    ) == "sensor.solax_measured_power"


# ── the reporter's dashboard, end to end ─────────────────────────────────

#: HA 2026.x keeps one entry per grid connection. The reporter's has the two
#: counters and no power sensor — the case the Energy Dashboard allows.
PREFS = {
    "energy_sources": [
        {"type": "grid", "stat_energy_from": IMPORT, "stat_energy_to": EXPORT,
         "stat_cost": None, "entity_energy_price": None,
         "number_energy_price": None, "stat_compensation": None,
         "entity_energy_price_export": None, "number_energy_price_export": None,
         "cost_adjustment_day": 0.0},
        {"type": "solar", "stat_energy_from": "sensor.solaredge_lifetime_energy",
         "stat_rate": "sensor.solaredge_current_power"},
        {"type": "battery",
         "stat_energy_from": "sensor.alphaess_total_energy_discharge_battery",
         "stat_energy_to": "sensor.alphaess_total_energy_charge_battery",
         "stat_rate": "sensor.alphaess_battery_power"},
    ],
    "device_consumption": [],
}


async def test_the_dashboard_without_a_power_link_now_has_grid_power(hass):
    _p1_meter(hass)
    config = _parse_prefs(hass, PREFS, quiet=True)
    assert config.grid_import_power == POWER
    assert config.derived_power.get("grid") == POWER


def _night(hass, declared: bool, monkeypatch):
    """The reporter's night: the P1 meter imports 800 W (HomeWizard says
    +800, HA's convention), 700 W of it charges the AlphaESS, the house
    takes 100 W, no sun. Five cycles, the import counter moving."""
    if not declared:
        monkeypatch.setitem(_POWER_DERIVE_RULES, "grid", _rule("grid", declared=False))
    _p1_meter(hass)
    hass.states.async_set("sensor.solaredge_current_power", "0", _W)
    hass.states.async_set("sensor.solaredge_lifetime_energy", "9000", _KWH)
    hass.states.async_set("sensor.alphaess_battery_power", "700", _W)
    config = _parse_prefs(hass, PREFS, quiet=True)
    reader = SensorReader(hass, {"update_interval": 10})
    reader._sign_vote_warmup = 0
    reader.set_energy_dashboard_config(config)
    readings = None
    for i in range(5):
        hass.states.async_set(IMPORT, f"{12.0 + 0.01 * i:.2f}", _KWH)
        hass.states.async_set(EXPORT, "12.0", _KWH)
        hass.states.async_set("sensor.alphaess_total_energy_charge_battery",
                              f"{5.0 + 0.01 * i:.2f}", _KWH)
        hass.states.async_set("sensor.alphaess_total_energy_discharge_battery",
                              "4.0", _KWH)
        readings = reader.read_power()
    calc = FlowCalculator()
    flows = calc.calculate_power_flows(readings)
    energy = calc.integrate_energy_flows(flows, 3600)
    return readings, flows, energy


async def test_the_reporters_night_draws_the_grid_lines(hass, monkeypatch):
    readings, flows, energy = _night(hass, True, monkeypatch)
    assert readings.grid_import_power == pytest.approx(800)
    assert readings.grid_export_power == 0
    assert flows.grid_to_battery == pytest.approx(700)
    assert flows.grid_to_home == pytest.approx(100)
    # the Sankey's two Grid lines for that hour
    assert energy.grid_to_battery == pytest.approx(0.7)
    assert energy.grid_to_home == pytest.approx(0.1)


async def test_without_the_key_the_same_night_has_no_grid_lines(hass, monkeypatch):
    """The control: same home, the name rules only — the reporter's 2.1.0
    picture, so the test above passes because of the fix."""
    readings, flows, energy = _night(hass, False, monkeypatch)
    assert readings.grid_import_power == 0
    assert energy.grid_to_battery == 0
    assert energy.grid_to_home == 0
