"""#1032 hardware wave — the roster learns charger ROLES, proven on the real
output of the integrations that expose them (tests/integrations_rig).

One crawler: the roles feed the roster's existing slots — a charger SEM has
no row for is a near miss with a ``suggested_charger``; a car that charges
is a ``vehicles`` row with its ``charge_control``. No brand is named in the
code under test; the brands are only the data.
"""
from __future__ import annotations

import json

from .integrations_rig.rig import crawl, load_capture, replay


async def _report(hass, *names):
    for n in names:
        await replay(hass, load_capture(n))
    return crawl(hass)


async def _zaptec_mode_off(hass, name):
    """(#1142) A Zaptec capture with its "Charger mode" sensor switched off.
    The brand rule maps a Go by that sensor; without it the Go is a near
    miss, and the near-miss offer is what the pins below are about."""
    cap = json.loads(json.dumps(load_capture(name)))
    off = [e for e in cap["entities"] if e["entity_id"].startswith("sensor.")
           and e["unique_id"].endswith("_charger_operation_mode")]
    assert len(off) == 1
    off[0]["disabled_by"] = "user"
    await replay(hass, cap)
    return crawl(hass)


def _near(rep, platform):
    return [n for n in rep["near_misses"] if n["platform"] == platform]


def _cars(rep, platform):
    return [v for v in rep["vehicles"] if v["platform"] == platform
            and v.get("charge_control")]


# ── R3 charge control on the car ─────────────────────────────────────────

import pytest  # noqa: E402


@pytest.mark.parametrize("name", ["tesla_fleet", "teslemetry", "tessie"])
async def test_r3_the_car_integration_offers_its_charge_control(hass, name):
    rep = await _report(hass, name)
    (car,) = _cars(rep, name)
    cc = car["charge_control"]
    assert cc["ev_current_control_entity"] == "number.test_charge_current"
    assert cc["ev_charging_power_sensor"] == "sensor.test_charger_power"
    assert cc["ev_start_stop_entity"] == "switch.test_charge"


@pytest.mark.parametrize("name", ["tesla_fleet", "teslemetry", "tessie"])
async def test_r3_the_energy_site_is_not_a_car(hass, name):
    rep = await _report(hass, name)
    for row in _cars(rep, name) + _near(rep, name):
        assert "energy_site" not in json.dumps(row), row


# ── R4 read-only charger + the car that drives it ────────────────────────

async def test_r4_a_wall_connector_alone_only_reports(hass):
    rep = await _report(hass, "tesla_wall_connector")
    (wc,) = _near(rep, "tesla_wall_connector")
    assert wc["missing"] == ["control"]
    assert wc["suggested_charger"] == {}
    assert {"plug", "power"} <= set(wc["charger_roles"])


async def test_r4_a_wall_connector_is_driven_through_the_one_car(hass):
    rep = await _report(hass, "tesla_wall_connector", "tesla_fleet")
    (wc,) = _near(rep, "tesla_wall_connector")
    sc = wc["suggested_charger"]
    assert sc["ev_current_control_entity"] == "number.test_charge_current"
    assert sc["ev_start_stop_entity"] == "switch.test_charge"
    # power and plug stay the wall connector's own readings
    assert sc["ev_charging_power_sensor"].startswith("sensor.tesla_wall_connector")
    assert wc["paired_vehicle"]


async def test_r4_two_cars_are_a_question_not_a_guess(hass):
    await replay(hass, load_capture("tessie"), suffix="_car2")
    rep = await _report(hass, "tesla_wall_connector", "tesla_fleet")
    (wc,) = _near(rep, "tesla_wall_connector")
    assert wc["suggested_charger"] == {}
    assert len(wc["choose_vehicle"]) == 2


# ── R5 a select read by its options ──────────────────────────────────────

async def test_r5_the_zappi_charge_mode_is_found_by_its_options(hass):
    rep = await _report(hass, "myenergi")
    offers = [n["suggested_charger"] for n in _near(rep, "myenergi")
              if n["suggested_charger"]]
    # The fixture has two Zappis. Only the first has an internal-load CT —
    # the clamp that measures the car; the second's CTs measure an AC
    # battery and a monitor point. A charger SEM cannot measure is not
    # offered (the near-miss rule, kept).
    assert len(offers) == 1, rep["near_misses"]
    off = offers[0]
    assert "zappi_1" in off["ev_charge_mode_entity"]
    assert off["ev_charge_mode_start"] == "Fast"
    assert off["ev_charge_mode_stop"] == "Stopped"
    assert off["_suggested_phase_switch"]["entity"].endswith("_phase_setting")


async def test_r5_a_water_heater_diverter_is_not_a_charger(hass):
    rep = await _report(hass, "myenergi")
    for n in rep["near_misses"]:
        assert "eddi" not in json.dumps(n.get("suggested_charger"))
        assert "harvi" not in json.dumps(n.get("suggested_charger"))


# ── R1 companion + R2 start/stop pair ────────────────────────────────────

async def test_r2_the_start_and_stop_buttons_are_the_control(hass):
    rep = await _zaptec_mode_off(hass, "zaptec_no_limit")
    offers = [n["suggested_charger"] for n in _near(rep, "zaptec")
              if n["suggested_charger"]]
    (off,) = offers
    assert off["ev_start_service"] == "button.press"
    assert json.loads(off["ev_start_service_data"])["entity_id"].endswith(
        "_resume_charging")
    assert json.loads(off["ev_stop_service_data"])["entity_id"].endswith(
        "_stop_charging_final")
    # the charger's own max current is a stored setting: not rewritten
    assert "ev_current_control_entity" not in off


async def test_r2_a_stop_button_alone_is_not_a_control(hass):
    rep = await _report(hass, "blue_current")
    for n in rep["near_misses"]:
        assert "ev_start_service" not in (n.get("suggested_charger") or {})


async def test_r1_the_installation_is_the_chargers_companion(hass):
    rep = await _zaptec_mode_off(hass, "zaptec")
    (chg,) = _near(rep, "zaptec")
    assert len(chg["companions"]) == 1
    # the live current control comes from the installation, not the setting
    # (#1141 asks whether the offer may pick the site limit at all)
    assert chg["suggested_charger"]["ev_current_control_entity"].endswith(
        "_available_current")


# ── R6 services read by their fields ─────────────────────────────────────

def test_r6_a_per_phase_service_is_named():
    from custom_components.solar_energy_management.hardware_detection import (
        _service_field_roles,
    )
    roles = _service_field_roles("easee", load_capture("easee")["services"])
    assert roles["phase_service"]["service"] == "easee.set_circuit_dynamic_limit"


async def test_r6_a_site_current_service_is_report_data_only(hass):
    rep = await _zaptec_mode_off(hass, "zaptec_no_limit")
    (chg,) = _near(rep, "zaptec")
    assert "site_service" in chg["charger_roles"]
    assert "ev_charger_service" not in chg["suggested_charger"]


# ── a charger driven by services, or already set up, is not "read-only" ──

def _keba_without_its_brand_path():
    """The KEBA capture under a domain no brand path knows — what any
    service-driven integration looks like to the role reader alone."""
    cap = json.loads(json.dumps(load_capture("keba")))
    cap["domain"] = "rig_svc_charger"
    for e in cap["entities"]:
        e["entity_id"] = e["entity_id"].replace("keba_p30", "svc_box")
    return cap


async def test_a_service_controlled_charger_is_not_read_only(hass):
    """.175, 02.10: a real KEBA, controlled through keba.set_current, came
    back as a read-only charger with 'missing control'."""
    await replay(hass, _keba_without_its_brand_path())
    rep = crawl(hass)
    (n,) = _near(rep, "rig_svc_charger")
    assert n["missing"] == []
    assert n["suggested_charger"]["ev_charger_service"] == "rig_svc_charger.set_current"
    assert n["suggested_charger"]["ev_service_param_name"] == "current"


async def test_a_charger_sem_already_drives_is_no_near_miss(hass):
    """The real KEBA on .175: the brand path binds it."""
    rep = await _report(hass, "keba")
    assert _near(rep, "keba") == []
    assert [c for c in rep["chargers"] if c["platform"] == "keba"]


async def test_a_configured_charger_is_no_near_miss(hass):
    from custom_components.solar_energy_management.hardware_detection import (
        build_detection_report,
    )
    await replay(hass, _keba_without_its_brand_path())
    rep = build_detection_report(
        hass, configured_entities={"sensor.svc_box_charging_power"})
    assert _near(rep, "rig_svc_charger") == []


# ── nothing becomes a charger that is not one ────────────────────────────

async def test_no_role_finds_a_charger_on_a_heat_pump(hass):
    rep = await _report(hass, "vicare")
    assert _near(rep, "vicare") == []
    assert _cars(rep, "vicare") == []


# ── a meter alone is no role charger (#1036's rule on the role path) ─────

async def test_a_bare_meter_is_no_role_charger(hass):
    cap = {"domain": "rig_meter", "source": {"kind": "declared", "repo": "x",
                                             "commit": "x"},
           "devices": {"rig_meter:m": {"name": "meter", "model": None,
                                       "manufacturer": None}},
           "entities": [
               {"entity_id": f"sensor.meter_{k}", "unique_id": f"m_{k}",
                "translation_key": k, "original_device_class": dc,
                "unit_of_measurement": u, "capabilities": {},
                "entity_category": None, "disabled_by": None,
                "device": "rig_meter:m", "state": "1", "attributes": {}}
               for k, dc, u in (("power", "power", "W"),
                                ("total_energy", "energy", "kWh"),
                                ("current", "current", "A"))],
           "services": {}}
    await replay(hass, cap)
    rep = crawl(hass)
    assert not [n for n in rep["near_misses"]
                if n["platform"] == "rig_meter" and n["suggested_charger"]]
