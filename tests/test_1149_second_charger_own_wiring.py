"""#1149 — a second charger used the first charger's control.

The charger builder in ``async_setup_entry`` reads each key from the
charger's own card and, when the card has none, from the flat top-level
keys. The setup wizard mirrors the FIRST charger's card there. So a second
charger with no current control of its own got the first charger's current
number — and its current service, the service's device id and fields, its
power sensor and its current sensor — and SEM set the first charger's
current when it drove the second.

The config card's "Add charger" saves exactly such a card (an id, a name, a
minimum current, a priority) and says it stays inert until it is wired. A
Zaptec Go offered with start/stop only (#1141), or a card whose site limit
was taken out (#1144), has no current control either.

The rule now: what names a box (``hardware_detection.CHARGER_WIRING_KEYS``)
a second charger reads from its own card only. The first charger still
reads the flat keys, and settings (limits, phases, priority) still fall
back to them. These pins run the real setup.
"""
from __future__ import annotations

import ast
import inspect
import json

import pytest

from custom_components.solar_energy_management import hardware_detection as hd

from .test_1144_saved_site_limit import _unload

#: The first charger's wiring, every value marked so a leak is easy to see.
PRIMARY = {
    "ev_connected_sensor": "binary_sensor.primary_plug",
    "ev_charging_sensor": "sensor.primary_status",
    "ev_charging_power_sensor": "sensor.primary_power",
    "ev_current_control_entity": "number.primary_max_current",
    "ev_charger_service": "primary_brand.set_current",
    "ev_charger_service_entity_id": "sensor.primary_charger",
    "ev_service_param_name": "primary_amps",
    "ev_service_device_id": "primary-device-id",
    "ev_charger_service_data": json.dumps({"charger_name": "primary-box"}),
    "ev_current_sensor": "sensor.primary_offered_current",
    "ev_total_energy_sensor": "sensor.primary_energy",
    "ev_session_energy_sensor": "sensor.primary_session",
    "ev_start_stop_entity": "switch.primary_charging",
    "ev_charge_mode_entity": "select.primary_mode",
    "ev_charge_mode_start": "primary-start",
    "ev_charge_mode_stop": "primary-stop",
    "ev_start_service": "primary_brand.start",
    "ev_start_service_data": json.dumps({"charger": "primary-box"}),
    "ev_stop_service": "primary_brand.stop",
    "ev_stop_service_data": json.dumps({"charger": "primary-box"}),
    "ev_phase_switch_entity": "select.primary_phases",
}

#: A Zaptec Go as setup offers it: a power reading and start/stop only.
GO_POWER = "sensor.go_charge_power"
GO_SWITCH = "switch.go_charging"
GO = {"id": "ev_charger_1", "name": "Go",
      "ev_charging_power_sensor": GO_POWER,
      "ev_start_stop_entity": GO_SWITCH}

#: What the config card's "Add charger" saves (sem-config-card.js).
NEW_FROM_CARD = {"id": "ev_charger_1", "name": "New charger 2",
                 "ev_min_current": 6, "ev_surplus_priority": 4}


def _entry(hass, second, first=None, **flat):
    """The wizard's layout: the first card mirrored into the flat keys."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.solar_energy_management.const import DOMAIN
    from .test_services_real import _seed_sem_input_sensors
    _seed_sem_input_sensors(hass)
    first = dict(PRIMARY if first is None else first)
    data = {
        "grid_power_sensor": "sensor.test_grid_power",
        "battery_power_sensor": "sensor.test_battery_power",
        "battery_soc_sensor": "sensor.test_battery_soc",
        "solar_power_sensor": "sensor.test_solar_power",
        "battery_capacity_kwh": 15.0,
        "update_interval": 30,
        **PRIMARY, **flat,
        "ev_chargers": [{"id": "ev_charger", "name": "First", **first},
                        dict(second)],
    }
    return MockConfigEntry(domain=DOMAIN, version=12, minor_version=1,
                           data=data, options={}, title="SEM #1149")


async def _set_up(hass, entry):
    from .test_services_real import _setup_sem
    await _setup_sem(hass, entry)
    return entry.runtime_data._ev_devices


def _strings(value, depth=0):
    """Every string a device holds, walked through its plain containers."""
    if isinstance(value, str):
        yield value
    elif depth < 4 and isinstance(value, dict):
        for k, v in value.items():
            yield from _strings(k, depth + 1)
            yield from _strings(v, depth + 1)
    elif depth < 4 and isinstance(value, (list, tuple, set, frozenset)):
        for v in value:
            yield from _strings(v, depth + 1)


def _primary_in(dev):
    return sorted({s for s in _strings(vars(dev)) if "primary" in s})


def _recording(dev):
    sent: list = []

    async def _send(domain, service, data=None, *, why=""):
        sent.append((domain, service, dict(data or {})))
        return True

    dev.send = _send
    return sent


# ── a second charger takes nothing that names the first charger's box ──

@pytest.mark.asyncio
async def test_a_second_charger_holds_none_of_the_first_chargers_wiring(
        sem_real_hass):
    hass = sem_real_hass
    entry = _entry(hass, GO)
    devs = await _set_up(hass, entry)
    try:
        go = devs["ev_charger_1"]
        assert _primary_in(go) == []
        assert go.power_entity_id == GO_POWER
        assert go.start_stop_entity == GO_SWITCH
        assert go.current_entity_id is None
        assert go.charger_service is None
        assert go.charger_service_entity_id is None
        assert go.service_device_id is None
        assert go.service_param_name == "current"
        assert go.service_extra_data == {}
        assert go.current_sensor_entity_id == ""
        assert go.charging_status_entity == ""
        # the first charger keeps its own
        first = devs["ev_charger"]
        assert first.current_entity_id == PRIMARY["ev_current_control_entity"]
        assert first.service_device_id == PRIMARY["ev_service_device_id"]
    finally:
        await _unload(hass, entry)


@pytest.mark.asyncio
async def test_driving_the_second_charger_sends_nothing_to_the_first(
        sem_real_hass):
    """Down to the one seam every charger command goes through."""
    from custom_components.solar_energy_management.coordinator.charger_adapters import (
        adapter_for,
    )
    hass = sem_real_hass
    entry = _entry(hass, GO)
    devs = await _set_up(hass, entry)
    try:
        go = devs["ev_charger_1"]
        sent = _recording(go)
        await go._set_current(10)
        adapter = adapter_for(go)
        await adapter.command_current(10)
        await adapter.command_idle()
        assert sent, "the Go was never driven — the pin would pass vacuously"
        assert not [c for c in sent if "primary" in json.dumps(c)], sent
        assert {c[2].get("entity_id") for c in sent} <= {GO_SWITCH, None}
    finally:
        await _unload(hass, entry)


@pytest.mark.asyncio
async def test_the_config_cards_new_charger_stays_inert(sem_real_hass):
    """It has no power sensor and no control of its own, so SEM does not
    run it — before, it was built on the first charger's and wrote it."""
    hass = sem_real_hass
    entry = _entry(hass, NEW_FROM_CARD)
    devs = await _set_up(hass, entry)
    try:
        assert set(devs) == {"ev_charger"}
    finally:
        await _unload(hass, entry)


@pytest.mark.asyncio
async def test_a_second_charger_with_a_current_number_of_its_own_uses_it(
        sem_real_hass):
    """Its own number, and no service of the first charger on top of it —
    a service would have sent the current to the first charger's device."""
    hass = sem_real_hass
    own = {**GO, "ev_current_control_entity": "number.go_max_current"}
    entry = _entry(hass, own)
    devs = await _set_up(hass, entry)
    try:
        go = devs["ev_charger_1"]
        assert _primary_in(go) == []
        assert go.current_entity_id == "number.go_max_current"
        sent = _recording(go)
        await go._set_current(10)
        assert sent == [("number", "set_value",
                         {"entity_id": "number.go_max_current", "value": 10})]
    finally:
        await _unload(hass, entry)


# ── what is left as it was ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_first_charger_still_reads_the_flat_keys(sem_real_hass):
    """A legacy first card can lack keys the flat mirror holds."""
    hass = sem_real_hass
    first = {k: PRIMARY[k] for k in ("ev_charging_power_sensor",
                                     "ev_charger_service")}
    entry = _entry(hass, GO, first=first)
    devs = await _set_up(hass, entry)
    try:
        dev = devs["ev_charger"]
        assert dev.current_entity_id == PRIMARY["ev_current_control_entity"]
        assert dev.service_device_id == PRIMARY["ev_service_device_id"]
        assert dev.service_param_name == PRIMARY["ev_service_param_name"]
        assert dev.current_sensor_entity_id == PRIMARY["ev_current_sensor"]
        assert dev.start_stop_entity == PRIMARY["ev_start_stop_entity"]
        assert dev.charging_status_entity == PRIMARY["ev_charging_sensor"]
    finally:
        await _unload(hass, entry)


@pytest.mark.asyncio
async def test_a_second_charger_still_takes_the_flat_settings(sem_real_hass):
    """Limits and phases are settings, not wiring."""
    hass = sem_real_hass
    entry = _entry(hass, GO, ev_min_current=8, ev_phases=1)
    devs = await _set_up(hass, entry)
    try:
        go = devs["ev_charger_1"]
        assert go.min_current == 8
        assert go.phases == 1
    finally:
        await _unload(hass, entry)


# ── the rule covers every key the builder reads ─────────────────────────

#: Keys the builder may take from the flat keys for any charger.
SETTINGS = frozenset({
    "ev_surplus_priority", "ev_min_current", "ev_phases",
    "ev_charger_needs_cycle", "keba_arm_failsafe", "keba_steady_failsafe",
})


def _builder_keys():
    """Every key literal the charger builder reads through ``_cfg``."""
    import custom_components.solar_energy_management as sem
    tree = ast.parse(inspect.getsource(sem.async_setup_entry))
    keys = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "_cfg" and node.args
                and isinstance(node.args[0], ast.Constant)):
            keys.add(node.args[0].value)
        # ``{k: _cfg(k) for k in (...)}`` — the keys sit in the iterable
        if isinstance(node, ast.DictComp) and isinstance(node.value, ast.Call) \
                and getattr(node.value.func, "id", None) == "_cfg":
            for gen in node.generators:
                for elt in getattr(gen.iter, "elts", ()):
                    if isinstance(elt, ast.Constant):
                        keys.add(elt.value)
    return keys


def test_every_key_the_builder_reads_is_wiring_or_a_setting():
    """A new key read through ``_cfg`` must be sorted: wiring (card only on a
    second charger) or a setting (flat fallback). Unsorted fails here."""
    keys = _builder_keys()
    assert "ev_current_control_entity" in keys     # the walk sees the reads
    assert keys - hd.CHARGER_WIRING_KEYS - SETTINGS == set()
    assert SETTINGS <= keys, SETTINGS - keys       # no stale setting
    assert not SETTINGS & hd.CHARGER_WIRING_KEYS


def test_the_wiring_covers_every_entity_and_service_key():
    import custom_components.solar_energy_management as sem
    assert hd.CHARGER_ENTITY_KEYS <= hd.CHARGER_WIRING_KEYS
    assert set(hd.SERVICE_CONTROL_KEYS) <= hd.CHARGER_WIRING_KEYS
    assert set(sem._OWN_CARD_CONTROL_KEYS) <= hd.CHARGER_WIRING_KEYS
    assert set(PRIMARY) <= hd.CHARGER_WIRING_KEYS
