"""#1143 — a Zaptec saved through the setup wizard still read "online" as
"charging".

Before #1142 the setup wizard prefilled a Zaptec Go's "Charger" sensor as
the charging sensor. That sensor is ``device_class: connectivity``: on while
the box is online, so SEM saw a charge almost all the time. #1142 fixed
detection, and a startup heal for setup's own save (no plug sensor). The
wizard's save has a plug sensor — the form asks for both — so it counted as
the user's and kept the online sensor:

* as the user's own value in the charger's device reference (``overrides``:
  it differs from what the crawler finds today, "Charger mode");
* in the crawler's last answer (``found``) of a reference made before #1142,
  when "Charger mode" is turned off and the crawler gives no state now;
* in the wizard's flat keys, which the sensor reader and the charger builder
  read when the card has none.

The rule now: a link sensor never says a car charges, whoever saved it —
the same rule #1144 made for a site limit. Every start takes it out of the
references and the flat keys; what the crawler finds today shows through.
These pins run the Go's v0.8.7 layout (@OddmarDam's) through the real setup
step and the real setup.
"""
from __future__ import annotations

import json
import logging

import pytest

from custom_components.solar_energy_management import charger_refs as cr
from custom_components.solar_energy_management import hardware_detection as hd

from .test_1139_orphan_charger_repairs import GO, _zaptec
from .test_1144_saved_site_limit import _CORE, _seed_core, _set_up, _unload

LINK = f"binary_sensor.{GO}_charger"          # "Charger": the box is online
ONLINE = f"binary_sensor.{GO}_online"         # also ``connectivity``
MODE = f"sensor.{GO}_charger_mode"            # the charger's real state
POWER = f"sensor.{GO}_charge_power"
CURRENT = f"number.{GO}_charger_max_current"
SWITCH = f"switch.{GO}_charging"

#: What the setup wizard saved before #1142: the online sensor prefilled as
#: the charge, the plug the user picked ("Charger mode", as @OddmarDam did).
WIZARD = {
    "ev_connected_sensor": MODE,
    "ev_charging_sensor": LINK,
    "ev_charging_power_sensor": POWER,
    "ev_current_control_entity": CURRENT,
    "ev_start_stop_entity": SWITCH,
}
CARD = {"id": "ev_charger", "name": "EV Charger", **WIZARD}


def _registry(hass):
    from homeassistant.helpers import entity_registry as er
    return er.async_get(hass)


def _device_of(hass):
    reg = _registry(hass)
    return lambda eid: getattr(reg.async_get(eid), "device_id", None)


def _wizard_entry(hass, data=None, options=None):
    """A 2.1 store: the wizard writes the flat keys and the list to
    ``entry.data``."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from custom_components.solar_energy_management.const import DOMAIN
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**(data or {}), **WIZARD, cr.LEGACY_KEY: [dict(CARD)]},
        options=dict(options or {}))
    entry.add_to_hass(hass)
    return entry


def _values(d):
    return json.dumps(d or {}, sort_keys=True, default=dict)


def _mode_off(hass):
    """Turn "Charger mode" off: the crawler then gives the Go no state."""
    from homeassistant.helpers.entity_registry import RegistryEntryDisabler
    _registry(hass).async_update_entity(
        MODE, disabled_by=RegistryEntryDisabler.USER)


# ── premise ──────────────────────────────────────────────────────────────

async def test_premise_the_wizard_save_reads_the_link_every_start(hass):
    """Non-vacuous: the sensor is a link sensor, today's crawler finds
    "Charger mode", and without the rule the user's value still wins."""
    await _zaptec(hass, with_limit=False)
    assert hd.says_a_link(_registry(hass).async_get(LINK))
    live = cr.live_charger_roles(hass)
    dev = _device_of(hass)(POWER)
    assert live[dev]["ev_charging_sensor"] == MODE
    _refs, resolved = cr.reconcile(
        data_list=[dict(CARD)], legacy=None, refs=None, sig=None,
        device_of=_device_of(hass), live_by_device=live,
        refuse=lambda c: hd.site_limit_controls(c, _registry(hass).async_get))
    assert resolved[0]["ev_charging_sensor"] == LINK


# ── the reporter's shape, through the real setup step ────────────────────

async def test_the_wizard_save_reads_charger_mode_at_the_next_start(
        hass, caplog):
    await _zaptec(hass, with_limit=False)
    entry = _wizard_entry(hass, data={"solar_power_sensor": "sensor.pv"})
    with caplog.at_level(logging.WARNING):
        out = cr.async_reconcile(hass, entry)
    (card,) = out
    assert card["ev_charging_sensor"] == MODE
    # nothing else the user set moved
    assert {k: v for k, v in card.items() if k != "ev_charging_sensor"} == {
        k: v for k, v in CARD.items() if k != "ev_charging_sensor"}
    # the store says so too: the refs, the list the forms read, the flat keys
    assert LINK not in _values(entry.options)
    assert LINK not in _values(entry.data)
    assert entry.data["ev_connected_sensor"] == MODE
    assert entry.data["solar_power_sensor"] == "sensor.pv"
    assert "#1143" in caplog.text and LINK in caplog.text
    assert "#1144" not in caplog.text
    # once is enough
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        again = cr.async_reconcile(hass, entry)
    assert again == out
    assert "#1143" not in caplog.text


async def test_a_save_already_stored_as_a_reference_loses_it_too(hass):
    """A 2.2 store: the ref holds the link as the user's own value."""
    await _zaptec(hass, with_limit=False)
    first = _wizard_entry(hass)
    refs = cr.reconcile(
        data_list=[dict(CARD)], legacy=None, refs=None, sig=None,
        device_of=_device_of(hass), live_by_device=cr.live_charger_roles(hass))
    assert refs[0][0]["overrides"]["ev_charging_sensor"] == LINK, "premise"
    await hass.config_entries.async_remove(first.entry_id)
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from custom_components.solar_energy_management.const import DOMAIN
    ref_list, resolved = refs
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={
        cr.REFS_KEY: ref_list, cr.LEGACY_KEY: resolved,
        cr.SIG_KEY: cr.signature(resolved)})
    entry.add_to_hass(hass)
    (card,) = cr.async_reconcile(hass, entry)
    assert card["ev_charging_sensor"] == MODE
    (ref,) = entry.options[cr.REFS_KEY]
    assert "ev_charging_sensor" not in ref["overrides"]
    assert LINK not in _values(ref)


async def test_with_charger_mode_off_the_link_goes_and_nothing_takes_its_place(
        hass):
    """A ref made before #1142 keeps the old crawler's link in ``found``;
    today's crawler gives no state. SEM then reads the charge from the
    power, as for any charger without a charging sensor."""
    await _zaptec(hass, with_limit=False)
    dev_of = _device_of(hass)
    old_live = {dev_of(POWER): {"ev_charging_sensor": LINK,
                                "ev_charging_power_sensor": POWER,
                                "ev_current_control_entity": CURRENT,
                                "ev_start_stop_entity": SWITCH}}
    refs, resolved = cr.reconcile(
        data_list=[dict(CARD)], legacy=None, refs=None, sig=None,
        device_of=dev_of, live_by_device=old_live)
    assert refs[0]["found"]["ev_charging_sensor"] == LINK, "premise"
    assert "ev_charging_sensor" not in refs[0]["overrides"], "premise"
    _mode_off(hass)
    assert "ev_charging_sensor" not in (
        cr.live_charger_roles(hass).get(dev_of(POWER)) or {}), "premise"
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from custom_components.solar_energy_management.const import DOMAIN
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={
        cr.REFS_KEY: refs, cr.LEGACY_KEY: resolved,
        cr.SIG_KEY: cr.signature(resolved)})
    entry.add_to_hass(hass)
    (card,) = cr.async_reconcile(hass, entry)
    assert "ev_charging_sensor" not in card
    assert card["ev_connected_sensor"] == MODE, "the user's plug stays"
    assert card["ev_current_control_entity"] == CURRENT
    assert LINK not in _values(entry.options)


async def test_setups_own_save_still_gets_both_state_sensors(hass):
    """The #1142 heal fills setup's own save (no plug, the link as the
    charge). Made a reference at the same start, that save's plug role is
    ``cleared`` (the crawler offers one, the card had none); dropping the
    link un-clears it, so the plug shows through as the heal gave it."""
    from custom_components.solar_energy_management import (
        _fill_state_sensors_of_own_save,
        build_discovered_charger_storage,
    )
    await _zaptec(hass, with_limit=False)
    found = [c for c in hd.discover_all_ev_chargers_from_registry(hass)
             if c.get("_platform") == "zaptec"][0]
    old = dict(found)
    old.pop("ev_connected_sensor")
    old["ev_charging_sensor"] = LINK
    own = build_discovered_charger_storage({}, {}, old)[2]
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from custom_components.solar_energy_management.const import DOMAIN
    entry = MockConfigEntry(domain=DOMAIN, data={cr.LEGACY_KEY: own},
                            options={cr.LEGACY_KEY: own})
    entry.add_to_hass(hass)
    (card,) = cr.async_reconcile(hass, entry)
    assert card["ev_connected_sensor"] == MODE
    assert card["ev_charging_sensor"] == MODE
    assert _fill_state_sensors_of_own_save(hass, [card]) is None
    # the next start keeps it
    (again,) = cr.async_reconcile(hass, entry)
    assert again == card


async def test_a_real_state_sensor_and_a_link_as_the_plug_are_left_alone(
        hass, caplog):
    """Only the charge is refused: a car's "charge cable" sensor (Tesla) is
    ``connectivity`` too, so a link as the plug stays."""
    await _zaptec(hass, with_limit=False)
    mine = {**CARD, "ev_connected_sensor": ONLINE, "ev_charging_sensor": MODE}
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from custom_components.solar_energy_management.const import DOMAIN
    entry = MockConfigEntry(domain=DOMAIN, data={cr.LEGACY_KEY: [mine]})
    entry.add_to_hass(hass)
    with caplog.at_level(logging.WARNING):
        (card,) = cr.async_reconcile(hass, entry)
    assert card == mine
    assert "#1143" not in caplog.text


# ── the real setup: what the charger and the reader read ─────────────────

@pytest.mark.asyncio
async def test_setup_reads_the_car_from_charger_mode(sem_real_hass):
    """Down to what SEM decides: with the box online and a car plugged in
    but not charging, the charger's status says not charging."""
    from custom_components.solar_energy_management.coordinator.charger_adapters import (
        adapter_for,
    )
    hass = sem_real_hass
    _seed_core(hass)
    await _zaptec(hass, with_limit=False)
    entry = _wizard_entry(hass, data=dict(_CORE))
    devs = await _set_up(hass, entry)
    try:
        dev = devs["ev_charger"]
        assert dev.charging_status_entity == MODE
        coordinator = entry.runtime_data
        assert LINK not in _values(coordinator.config)
        hass.states.async_set(LINK, "on")
        hass.states.async_set(MODE, "connected_requesting")
        assert adapter_for(dev)._status_class() == "not_charging"
        # the reader's flat key (the fleet's blink hold) no longer reads it
        reader = coordinator._sensor_reader
        assert reader.config.ev_charging_sensor in ("", MODE)
        assert reader._fleet_charging_status() is not True
        hass.states.async_set(MODE, "connected_charging")
        assert adapter_for(dev)._status_class() == "charging"
    finally:
        await _unload(hass, entry)


# ── the pure step ────────────────────────────────────────────────────────

def _link_only(card):
    return ("ev_charging_sensor",) if card.get("ev_charging_sensor") == LINK else ()


def test_a_link_drop_un_clears_the_state_roles_only():
    """The current roles stay cleared: the card's own control is fine."""
    ref = {"id": "z", "device_id": "go", "settings": {},
           "cleared": ["ev_connected_sensor", "ev_current_control_entity"],
           "overrides": {"ev_charging_sensor": LINK,
                         "ev_start_stop_entity": SWITCH},
           "found": {"ev_charging_power_sensor": POWER,
                     "ev_connected_sensor": MODE, "ev_charging_sensor": MODE,
                     "ev_current_control_entity": CURRENT}}
    (out,) = cr.drop_refused([ref], {}, _link_only)
    assert out["cleared"] == ["ev_current_control_entity"]
    assert out["overrides"] == {"ev_start_stop_entity": SWITCH}
    card = cr.resolve(out)
    assert card["ev_connected_sensor"] == MODE
    assert card["ev_charging_sensor"] == MODE
    assert "ev_current_control_entity" not in card


def test_a_site_limit_drop_leaves_the_state_roles_cleared():
    site = "number.abbastova_available_current"
    ref = {"id": "z", "device_id": "go", "settings": {},
           "cleared": ["ev_connected_sensor"],
           "overrides": {"ev_current_control_entity": site},
           "found": {"ev_charging_power_sensor": POWER,
                     "ev_connected_sensor": MODE}}
    (out,) = cr.drop_refused(
        [ref], {}, lambda c: hd.site_limit_controls(c))
    assert out["cleared"] == ["ev_connected_sensor"]


def test_what_is_a_link_as_the_charge():
    from types import SimpleNamespace

    def e(eid, dc):
        return SimpleNamespace(entity_id=eid, original_device_class=dc)
    entries = {LINK: e(LINK, "connectivity"),
               f"binary_sensor.{GO}_charging": e(
                   f"binary_sensor.{GO}_charging", "battery_charging"),
               MODE: e(MODE, "enum"),
               "sensor.box_link": e("sensor.box_link", "connectivity")}
    of = entries.get
    assert hd.link_as_charging({"ev_charging_sensor": LINK}, of) == (
        "ev_charging_sensor",)
    for eid in (f"binary_sensor.{GO}_charging", MODE, "sensor.box_link",
                "binary_sensor.not_registered"):
        assert hd.link_as_charging({"ev_charging_sensor": eid}, of) == (), eid
    # the plug is another question
    assert hd.link_as_charging({"ev_connected_sensor": LINK}, of) == ()
    # without the registry, nothing is said
    assert hd.link_as_charging({"ev_charging_sensor": LINK}) == ()
    assert hd.link_as_charging(None) == ()
