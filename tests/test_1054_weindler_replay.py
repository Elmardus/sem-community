"""#1054 round 3 — weindler's install, replayed on a real Home Assistant.

Two inputs, both his own:

* the go-eCharger (HTTP, ``goecharger``) entity list from his diagnostics
  (08.10.2026, device ``118c19168da8cdf95739aaf95b2598a4``): the rig's
  ``goecharger`` capture holds exactly those 50 entities and the
  ``set_max_current(charger_name)`` service — this file pins that;
* his SEM charger stores on beta.13 (``data.ev_chargers`` = the old
  half-filled card, ``options.ev_chargers`` = that card plus the working
  go-e) and on beta.15 (the old card only).

The REAL crawler reads the replayed integration, and its offer is the
charger the card's "Create this charger entry" button sends — the source of
truth for what a complete go-e looks like. No hand-written charger here.
"""
from __future__ import annotations

import json

import pytest

from custom_components.solar_energy_management.const import DOMAIN
from custom_components.solar_energy_management.hardware_detection import (
    charger_is_complete,
)

from .integrations_rig.rig import crawl, load_capture, replay

#: weindler's diagnostics, ``detection.near_misses[0].entities`` (08.10.2026).
WEINDLER_ENTITIES = [f"sensor.goecharger_wallbox_go_e_{s}" for s in (
    "car_status", "charger_max_current", "charger_absolute_max_current",
    "charger_err", "charger_access", "stop_mode", "cable_lock_mode",
    "cable_max_current", "pre_contactor_l1", "pre_contactor_l2",
    "pre_contactor_l3", "post_contactor_l1", "post_contactor_l2",
    "post_contactor_l3", "charger_temp", "charger_temp0", "charger_temp1",
    "charger_temp2", "charger_temp3", "current_session_charged_energy",
    "current_session_charged_energy_corrected", "charge_limit", "adapter",
    "unlocked_by_card", "energy_total", "energy_total_corrected", "wifi",
    "u_l1", "u_l2", "u_l3", "u_n", "i_l1", "i_l2", "i_l3", "p_l1", "p_l2",
    "p_l3", "p_n", "p_all", "lf_l1", "lf_l2", "lf_l3", "lf_n", "firmware",
    "serial_number", "wifi_ssid", "wifi_enabled", "timezone_offset",
    "timezone_dst_offset")] + ["switch.goecharger_wallbox_go_e_allow_charging"]

P = "goecharger_wallbox_go_e"
#: the card an earlier detection half-filled — his ``data.ev_chargers``
OLD = {"id": "goecharger_28eb748ac3", "name": "Goecharger Charger",
       "ev_total_energy_sensor": f"sensor.{P}_energy_total_corrected"}


def test_the_capture_is_his_box():
    cap = load_capture("goecharger")
    assert sorted(e["entity_id"] for e in cap["entities"]) == sorted(WEINDLER_ENTITIES)
    assert cap["services"]["set_max_current"] == ["charger_name", "max_current"]
    # his diagnostics: only the four energy counters carry a device class
    assert {e["entity_id"] for e in cap["entities"]
            if e["original_device_class"]} == {
        f"sensor.{P}_{s}" for s in (
            "current_session_charged_energy",
            "current_session_charged_energy_corrected",
            "energy_total", "energy_total_corrected")}


async def _his_box(hass) -> tuple[str, dict]:
    """Replay the go-e, crawl it, and return (device id, the crawler's offer
    as the card would send it)."""
    devices = await replay(hass, load_capture("goecharger"))
    report = crawl(hass)
    offers = [n for n in report["near_misses"] if n.get("platform") == "goecharger"]
    assert len(offers) == 1, report["near_misses"]
    offer = offers[0]["suggested_charger"]
    # what the card's create button adds (chargerAddPlan in detect-rows.js)
    return devices["wallbox_go_e"], {**offer, "ev_min_current": 6,
                                     "ev_surplus_priority": 3}


def _seed(hass):
    from .test_services_real import _seed_sem_input_sensors
    _seed_sem_input_sensors(hass)


def _sem_entry(base, data_chargers, options_chargers):
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from custom_components.solar_energy_management.config_flow import (
        SolarEnergyManagementConfigFlow as Flow,
    )
    data = {k: v for k, v in base.data.items() if k != "ev_chargers"}
    data.update({"ev_chargers": data_chargers, "has_ev": False})
    return MockConfigEntry(domain=DOMAIN, version=Flow.VERSION,
                           minor_version=Flow.MINOR_VERSION, data=data,
                           options={"ev_chargers": options_chargers})


def _ids(lst):
    return [c["id"] for c in lst or []]


@pytest.mark.asyncio
async def test_the_crawler_reads_his_box(sem_real_hass):
    """The roles, from the real crawler on his entities."""
    _dev, offer = await _his_box(sem_real_hass)
    assert offer["ev_charging_power_sensor"] == f"sensor.{P}_p_all"
    assert offer["ev_connected_sensor"] == f"sensor.{P}_car_status"
    assert offer["ev_charging_sensor"] == f"sensor.{P}_car_status"
    assert offer["ev_start_stop_entity"] == f"switch.{P}_allow_charging"
    assert offer["ev_charger_service"] == "goecharger.set_max_current"
    assert json.loads(offer["ev_charger_service_data"]) == {"charger_name": "wallbox_go_e"}
    assert charger_is_complete(offer)


@pytest.mark.asyncio
async def test_beta13_stores_then_delete_the_old_card(
    sem_real_hass, sem_multi_wallbox_config_entry,
):
    """beta.13 stores → start → X on the old card. One charger left, the
    working one, in both stores; the runtime drives it."""
    from .test_services_real import _setup_sem
    h = sem_real_hass
    _seed(h)
    dev, offer = await _his_box(h)
    new = {**offer, "id": f"goecharger_{dev}", "name": "go-eCharger"}
    entry = _sem_entry(sem_multi_wallbox_config_entry, [dict(OLD)],
                       [dict(OLD), dict(new)])
    await _setup_sem(h, entry)
    # the start-up heal changes nothing here
    assert _ids(entry.options["ev_chargers"]) == [OLD["id"], new["id"]]

    await h.services.async_call(DOMAIN, "remove_charger",
                                {"charger_id": OLD["id"]}, blocking=True)
    await h.async_block_till_done()
    assert _ids(entry.options["ev_chargers"]) == [new["id"]]
    assert _ids(entry.data["ev_chargers"]) == []

    await h.config_entries.async_reload(entry.entry_id)
    await h.async_block_till_done()
    assert _ids(entry.options["ev_chargers"]) == [new["id"]]
    coord = entry.runtime_data
    assert list(coord._ev_devices) == [new["id"]]


@pytest.mark.asyncio
async def test_beta15_lost_state_then_create_from_detection(
    sem_real_hass, sem_multi_wallbox_config_entry,
):
    """His beta.15 state: only the old card, nothing running, a stale 9.45
    kWh bucket for the lost charger. The page offers the box again; the
    create button with the crawler's offer replaces the old card in both
    stores; the runtime drives the go-e; no stale rows remain."""
    from .test_services_real import _setup_sem
    from custom_components.solar_energy_management.hardware_detection import (
        build_detection_report,
    )
    h = sem_real_hass
    _seed(h)
    dev, offer = await _his_box(h)
    lost_id = f"goecharger_{dev}"
    entry = _sem_entry(sem_multi_wallbox_config_entry, [dict(OLD)], [dict(OLD)])
    await _setup_sem(h, entry)
    coord = entry.runtime_data
    assert list(coord._ev_devices) == []

    # the stale state the health check kept warning about
    coord._daily_ev_per_charger[lost_id] = 9.45
    coord._daily_ev_per_charger_date[lost_id] = "2026-10-11"
    await coord.async_refresh()
    await h.async_block_till_done()
    assert lost_id not in coord._daily_ev_per_charger

    # the old card no longer claims the box: it is offered again
    report = build_detection_report(
        h, configured_chargers=entry.options["ev_chargers"])
    assert not [c for c in report["chargers"] if c.get("configured")]
    assert [i["charger_id"] for i in report["incomplete_chargers"]] == [OLD["id"]]
    again = [n for n in report["near_misses"] if n.get("platform") == "goecharger"]
    assert again and again[0]["suggested_charger"]["id"] == lost_id

    # the card's create button
    await h.services.async_call(
        DOMAIN, "set_option", {"options": {"ev_chargers": [offer]}}, blocking=True)
    await h.async_block_till_done()
    assert _ids(entry.options["ev_chargers"]) == [lost_id]
    assert _ids(entry.data["ev_chargers"]) == [lost_id]

    coord = entry.runtime_data
    assert list(coord._ev_devices) == [lost_id]
    assert set(coord._daily_ev_per_charger) <= {lost_id}

    # and it stays that way through the next start
    await h.config_entries.async_reload(entry.entry_id)
    await h.async_block_till_done()
    assert _ids(entry.options["ev_chargers"]) == [lost_id]
    assert _ids(entry.data["ev_chargers"]) == [lost_id]


@pytest.mark.asyncio
async def test_the_configure_dialog_offers_the_old_card(
    sem_real_hass, sem_multi_wallbox_config_entry,
):
    """beta.13 stores, Configure → Remove a charger: the old card is in the
    list (before, the only choice was the working go-e)."""
    from .test_services_real import _setup_sem
    h = sem_real_hass
    _seed(h)
    dev, offer = await _his_box(h)
    new = {**offer, "id": f"goecharger_{dev}", "name": "go-eCharger"}
    entry = _sem_entry(sem_multi_wallbox_config_entry, [dict(OLD)],
                       [dict(OLD), dict(new)])
    await _setup_sem(h, entry)

    flow = await h.config_entries.options.async_init(entry.entry_id)
    flow_obj = h.config_entries.options._progress[flow["flow_id"]]
    # the draft the menu works on: the stored list, as async_step_init seeds it
    flow_obj._data["ev_chargers"] = [dict(c) for c in entry.options["ev_chargers"]]
    res = await flow_obj.async_step_ev_charger_remove()
    field = next(iter(res["data_schema"].schema.values()))
    assert [o["value"] for o in field.config["options"]] == [OLD["id"], new["id"]]
    h.config_entries.options.async_abort(flow["flow_id"])
