"""#1032 review — what the crawler SHOULD pick on each rig integration,
stated from what the integration's entities mean, not from what the code
happened to produce. The golden snapshots in tests/integrations_rig/crawler
are regenerated only after these pass. A known wrong pick that belongs to
another fix is an ``xfail(strict=True)`` naming that issue: it fails loudly
the day the fix lands, so the snapshot is never the place a bug hides.
"""
from __future__ import annotations

import pytest

from .integrations_rig.rig import crawl, load_capture, replay


async def _rep(hass, *names):
    for n in names:
        await replay(hass, load_capture(n))
    return crawl(hass)


def _charger(rep, platform):
    rows = [c for c in rep["chargers"] if c["platform"] == platform]
    assert len(rows) == 1, rows
    return {k: (v.get("entity") or v.get("value")) for k, v in rows[0]["mapped"].items()}


def _offer(rep, platform):
    offers = [n["suggested_charger"] for n in rep["near_misses"]
              if n["platform"] == platform and n["suggested_charger"]]
    assert len(offers) == 1, rep["near_misses"]
    return offers[0]


async def test_ohme(hass):
    m = _charger(await _rep(hass, "ohme"), "ohme")
    assert m["ev_charge_mode_entity"] == "select.ohme_home_pro_charge_mode"
    # #1039 (v2.2.0-beta.7): the option the select LISTS, not its label
    assert m["ev_charge_mode_start"] == "max_charge"
    assert m["ev_charge_mode_stop"] == "paused"
    assert m["ev_charging_power_sensor"] == "sensor.ohme_home_pro_power"
    assert m["ev_connected_sensor"] == "sensor.ohme_home_pro_status"
    assert m["ev_charging_sensor"] == "sensor.ohme_home_pro_status"


async def test_keba(hass):
    m = _charger(await _rep(hass, "keba"), "keba")
    assert m["ev_charger_service"] == "keba.set_current"
    assert m["ev_charging_power_sensor"] == "sensor.keba_p30_charging_power"
    assert m["ev_connected_sensor"] == "binary_sensor.keba_p30_plug"


async def test_easee_the_equalizer_is_a_meter(hass):
    rep = await _rep(hass, "easee")
    m = _charger(rep, "easee")
    assert m["ev_charging_power_sensor"] == "sensor.easee_home_eh123456_power"
    assert m["ev_charger_service"] == "easee.set_charger_dynamic_limit"
    assert "equalizer" not in str(m)
    assert [x for x in rep["meters"] if x["platform"] == "easee"]


async def test_nrgkick(hass):
    o = _offer(await _rep(hass, "nrgkick"), "nrgkick")
    assert o["ev_current_control_entity"] == "number.nrgkick_test_charging_current"
    assert o["ev_charging_power_sensor"] == "sensor.nrgkick_test_total_active_power"
    assert o["ev_start_stop_entity"] == "switch.nrgkick_test_charging_enabled"
    assert o["ev_session_energy_sensor"] == "sensor.nrgkick_test_charged_energy"
    assert o["ev_total_energy_sensor"] == "sensor.nrgkick_test_total_charged_energy"
    assert o["_suggested_phase_switch"]["entity"] == "number.nrgkick_test_phase_count"


async def test_openevse(hass):
    o = _offer(await _rep(hass, "openevse"), "openevse")
    assert o["ev_current_control_entity"] == "number.openevse_mock_config_charge_rate"
    assert o["ev_charging_power_sensor"] == "sensor.openevse_mock_config_charging_power"
    assert o["ev_connected_sensor"] == "binary_sensor.openevse_mock_config_vehicle_connected"


@pytest.mark.parametrize("name", ["zaptec", "zaptec_no_limit"])
async def test_zaptec(hass, name):
    """#1142: "Charger mode" is the state, never the online sensor; the
    brand rule maps the Go, so the site limit is never its control."""
    rep = await _rep(hass, name)
    m = _charger(rep, "zaptec")
    mode = "sensor.zaptec_go2_zap012345_charger_operation_mode"
    assert m["ev_connected_sensor"] == mode
    assert m["ev_charging_sensor"] == mode
    assert m["ev_current_control_entity"] == (
        "number.zaptec_go2_zap012345_charger_max_current")
    assert m["ev_charging_power_sensor"] == "sensor.zaptec_go2_zap012345_total_charge_power"
    assert "abbastova" not in str(m)
    assert not [n for n in rep["near_misses"] if n["platform"] == "zaptec"]


async def test_myenergi(hass):
    o = _offer(await _rep(hass, "myenergi"), "myenergi")
    assert o["ev_charge_mode_entity"] == "select.test_zappi_1_myenergi_test_zappi_1_charge_mode"
    assert o["ev_charging_power_sensor"] == (
        "sensor.test_zappi_1_myenergi_test_zappi_1_power_ct_internal_load")


async def test_peblar(hass):
    """#1035 (autopilot, v2.2.0-beta.5): the brand rules read the entity's
    own name."""
    m = _charger(await _rep(hass, "peblar"), "peblar")
    assert m["ev_current_control_entity"] == "number.peblar_ev_charger_charge_limit"
    assert m["ev_start_stop_entity"] == "switch.peblar_ev_charger_charge"
    assert m["ev_charging_power_sensor"] == "sensor.peblar_ev_charger_power"
    # #1126: the state sensor is ``device_class: enum``
    assert m["ev_connected_sensor"] == "sensor.peblar_ev_charger_state"
    assert m["ev_charging_sensor"] == "sensor.peblar_ev_charger_state"


async def test_v2c(hass):
    """#1034 (autopilot, v2.2.0-beta.6): the min/max current is not the
    set-point, and the PV power is not the car's."""
    m = _charger(await _rep(hass, "v2c"), "v2c")
    assert m["ev_current_control_entity"] == "number.evse_1_1_1_1_intensity"
    assert m["ev_charging_power_sensor"] == "sensor.evse_1_1_1_1_charge_power"


@pytest.mark.parametrize("name", ["vicare", "blue_current"])
async def test_no_charger_offered(hass, name):
    rep = await _rep(hass, name)
    domain = load_capture(name)["domain"]
    assert not [c for c in rep["chargers"] if c["platform"] == domain]
    assert not [n for n in rep["near_misses"]
                if n["platform"] == domain and n["suggested_charger"]]


def _role_proven():
    from custom_components.solar_energy_management.hardware_detection import (
        ROLE_PROVEN_PLATFORMS,
    )
    return list(ROLE_PROVEN_PLATFORMS)


@pytest.mark.parametrize("platform", _role_proven())
async def test_every_role_proven_platform_is_found_on_its_rig_capture(hass, platform):
    """A brand that left the brand list must still be FOUND, on its real
    output: one complete offer with a power reading and a control."""
    from .integrations_rig.rig import capture_names
    assert platform in capture_names(), f"{platform}: no rig capture"
    o = _offer(await _rep(hass, platform), platform)
    assert o.get("ev_charging_power_sensor")
    assert any(o.get(k) for k in ("ev_current_control_entity",
                                  "ev_start_stop_entity",
                                  "ev_charge_mode_entity",
                                  "ev_start_service", "ev_charger_service"))
