"""(#1051) rdehuyss's first install, 2.2.0-beta.15.

A bed-heating smart plug in the Energy Dashboard's individual devices
(``sensor.bedverwarming_device_power``) became the EV: the old rule matched
the letters "ev" anywhere in the entity id, and "device" has them. A home
with no car got an EV charger.

The rule now: an Energy Dashboard device is the EV only when the crawler
knows its integration as a charger (``known_charger_domains``). Names say
nothing.
"""
from __future__ import annotations

import pytest
from homeassistant.helpers import entity_registry as er

from custom_components.solar_energy_management.ha_energy_reader import (
    EnergyDashboardConfig,
    _extract_ev_from_devices,
    _parse_prefs,
)

# His Energy Dashboard, as far as the diagnostics show it.
PREFS = {
    "energy_sources": [
        {"type": "solar",
         "stat_energy_from": "sensor.solis_s6_eh3p_pv_total_energy_generation",
         "stat_rate": "sensor.solis_s6_eh3p_smartport_old_invertes_power"},
        {"type": "grid", "flow_from": [
            {"stat_energy_from": "sensor.bluetooth_proxy_garage_energy_consumed_tariff_1"},
            {"stat_energy_from": "sensor.bluetooth_proxy_garage_energy_consumed_tariff_2"}],
         "flow_to": [
            {"stat_energy_to": "sensor.bluetooth_proxy_garage_energy_produced_tariff_1"},
            {"stat_energy_to": "sensor.bluetooth_proxy_garage_energy_produced_tariff_2"}]},
        {"type": "battery",
         "stat_energy_to": "sensor.solis_s6_eh3p_today_battery_charge_energy",
         "stat_energy_from": "sensor.solis_s6_eh3p_today_battery_discharge_energy"},
    ],
    "device_consumption": [
        {"stat_consumption": "sensor.bedverwarming_device_energy",
         "stat_rate": "sensor.bedverwarming_device_power"},
    ],
}


def _register(hass, entity_id, platform):
    reg = er.async_get(hass)
    domain, object_id = entity_id.split(".", 1)
    reg.async_get_or_create(domain, platform, object_id,
                            suggested_object_id=object_id)


async def test_a_smart_plug_named_device_is_not_the_ev(hass):
    """His exact case: a ZHA plug whose ids contain "device"."""
    for eid in ("sensor.bedverwarming_device_energy",
                "sensor.bedverwarming_device_power"):
        _register(hass, eid, "zha")
    config = _parse_prefs(hass, PREFS, quiet=True)
    assert config.has_ev is False
    assert config.ev_power is None and config.ev_energy is None


async def test_a_charger_integration_device_is_the_ev(hass):
    """Not vacuous: a device the crawler knows as a charger still counts."""
    _register(hass, "sensor.keba_p30_total_energy", "keba")
    _register(hass, "sensor.keba_p30_charging_power", "keba")
    prefs = dict(PREFS, device_consumption=[
        {"stat_consumption": "sensor.keba_p30_total_energy",
         "stat_rate": "sensor.keba_p30_charging_power"}])
    config = _parse_prefs(hass, prefs, quiet=True)
    assert config.has_ev is True
    assert config.ev_energy == "sensor.keba_p30_total_energy"
    assert config.ev_power == "sensor.keba_p30_charging_power"


@pytest.mark.parametrize("name,is_ev", [
    ("sensor.bedverwarming_device_energy", False),   # "ev" inside "device"
    ("sensor.dryer_device_energy", False),
    ("sensor.ev_charger_energy", True),               # whole words still count
    ("sensor.keba_total_energy", True),
    ("sensor.tesla_wall_connector_energy", True),
])
def test_without_the_registry_only_whole_words_count(name, is_ev):
    """No registry to ask: the entity id's WHOLE words decide, never letters
    inside a word."""
    config = EnergyDashboardConfig()
    _extract_ev_from_devices([{"stat_consumption": name}], config)
    assert config.has_ev is is_ev


async def test_a_roster_charger_counts_whatever_its_name(hass):
    """A charger integration the crawler knows (here OCPP, a roster
    charger row) is the EV even when its ids hold no charger word."""
    _register(hass, "sensor.garage_home_energy", "ocpp")
    prefs = dict(PREFS, device_consumption=[
        {"stat_consumption": "sensor.garage_home_energy"}])
    assert _parse_prefs(hass, prefs, quiet=True).has_ev is True


def test_the_load_list_uses_the_same_rule():
    """The individual-device list (load control, dashboard) asks the same
    question: his plug is a load, not a car."""
    from custom_components.solar_energy_management.ha_energy_reader import (
        get_all_individual_devices,
    )
    config = EnergyDashboardConfig()
    config.device_consumption = PREFS["device_consumption"]
    rows = get_all_individual_devices(config)
    assert rows and not any(r.get("is_ev") for r in rows)


# ── the tip that wrote an Activity row every cycle ───────────────────────

def _assistant(monkeypatch, clock):
    from unittest.mock import MagicMock
    import custom_components.solar_energy_management.analytics.energy_assistant as ea_mod
    monkeypatch.setattr(ea_mod.time, "monotonic", lambda: clock["t"])
    hass = MagicMock()
    hass.config.currency = "EUR"
    return ea_mod, ea_mod.EnergyAssistant(hass)


def test_the_shown_tip_keeps_its_words_between_rotations(monkeypatch):
    """His Activity: "114% … exported" then "111% …" every ~30 s. The tip's
    numbers move every cycle; the text a person sees (a sensor STATE, so a
    logbook row per change) must hold until the next rotation."""
    clock = {"t": 1000.0}
    ea_mod, ea = _assistant(monkeypatch, clock)
    ea.analyze(daily_solar_kwh=10.0, daily_grid_export_kwh=7.0,
               self_consumption_rate=90.0)
    first = ea.assistant_data.current_tip
    assert first and "exported" in first
    clock["t"] += 30
    ea.analyze(daily_solar_kwh=10.2, daily_grid_export_kwh=7.3,
               self_consumption_rate=90.0)
    assert ea.assistant_data.current_tip == first
    clock["t"] += ea_mod.TIP_ROTATION_INTERVAL_S + 1
    ea.analyze(daily_solar_kwh=10.2, daily_grid_export_kwh=7.3,
               self_consumption_rate=90.0)
    shown = [t.description for t in ea._tips]
    assert ea.assistant_data.current_tip in shown   # fresh words on rotation


def test_more_export_than_solar_gives_no_export_tip(monkeypatch):
    """114 % of solar exported is not a fact about the house: the inputs
    disagree (his solar power read only the old inverters). No tip on it."""
    clock = {"t": 1000.0}
    _, ea = _assistant(monkeypatch, clock)
    ea.analyze(daily_solar_kwh=5.0, daily_grid_export_kwh=5.7)
    tips = " ".join(t.description for t in ea._tips)
    assert "exported" not in tips


# ── the BE price sensor: today/tomorrow slots priced as all_in ───────────

def test_a_slot_priced_all_in_is_read_at_its_all_in_price(mock_hass):
    """His BE prices sensor: today/tomorrow lists of {start, energy,
    network, taxes, all_in}. The price a home pays is ``all_in``; the
    energy part alone is a quarter of it."""
    from datetime import datetime, timedelta
    from unittest.mock import MagicMock
    from custom_components.solar_energy_management.tariff.tariff_provider import (
        DynamicTariffProvider,
    )
    start = datetime.fromisoformat("2026-10-11T12:00:00+02:00")
    slots = [{"start": (start + timedelta(hours=i)).isoformat(),
              "energy": 0.053534, "network": 0.0587, "taxes": 0.06436,
              "all_in": 0.176594 + i / 100} for i in range(3)]
    state = MagicMock()
    state.state = "0.176594"
    state.attributes = {"today": slots, "tomorrow": [],
                        "unit_of_measurement": "EUR/kWh"}
    mock_hass.states.get = MagicMock(return_value=state)
    prices = DynamicTariffProvider(
        mock_hass, price_entity="sensor.be_electricity_price")._read_prices_list()
    assert [round(p.price, 6) for p in prices] == [0.176594, 0.186594, 0.196594]
