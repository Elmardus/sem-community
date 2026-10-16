"""#1141 — setup offered a Zaptec installation's "Available current" as the
charger's current control.

That number limits every charger on the site, and the owner sets it as a
grid guard (#804: 3x25 A on the reporting install). SEM never writes it.
The brand rule (`_discover_zaptec`) always refused it; the role reader did
not. When the brand rule could not map a Go (its "Charger mode" sensor
turned off), the near-miss offer took the installation's number in two ways:

* R1 — the installation is the Go's companion (same config entry, no power
  reading), and its "live" current number replaced the Go's stored setting;
* the role word list matched ``available_current`` as a charger control, so
  the roster proposed it first, on any device that held it.

The fix closes both: a companion's numbers are never a charger's control,
and ``available_current`` is not a charger-control word. These pins run the
real integration's entities through the real crawler and discovery.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from custom_components.solar_energy_management import hardware_detection as hd

from .integrations_rig.rig import capture_names, crawl, load_capture, replay

SITE_LIMIT = "number.abbastova_available_current"


def _mode_off(name):
    """The capture with the Go's "Charger mode" sensor turned off — the case
    the brand rule cannot map, so the role reader makes the offer."""
    cap = json.loads(json.dumps(load_capture(name)))
    off = [e for e in cap["entities"] if e["entity_id"].startswith("sensor.")
           and e["unique_id"].endswith("_charger_operation_mode")]
    assert len(off) == 1
    off[0]["disabled_by"] = "user"
    return cap


def _values(d):
    return json.dumps(d or {}, sort_keys=True)


# ── the reporter's shape, through the real report and the real discovery ──

async def test_the_offer_never_names_the_site_limit(hass):
    await replay(hass, _mode_off("zaptec"))
    assert hass.states.get(SITE_LIMIT) is not None, "premise: the site limit exists"
    rep = crawl(hass)
    near = [n for n in rep["near_misses"] if n["platform"] == "zaptec"]
    assert len(near) == 1, "premise: the Go is a near miss, its site a companion"
    assert near[0]["companions"], "premise: the installation is the companion"
    offer = near[0]["suggested_charger"]
    assert offer, "the Go is still offered"
    assert SITE_LIMIT not in _values(offer)
    # it starts and stops through its own buttons
    assert offer["ev_start_service"] == "button.press"
    assert "ev_current_control_entity" not in offer


async def test_discovery_never_prefills_the_site_limit(hass):
    """Setup and the add-charger step read discovery, not the report."""
    from custom_components.solar_energy_management.hardware_detection import (
        discover_all_ev_chargers_from_registry,
        discover_ev_charger_from_registry,
    )
    await replay(hass, _mode_off("zaptec"))
    every = discover_all_ev_chargers_from_registry(hass)
    assert every, "premise: the Go is offered"
    for c in every:
        assert SITE_LIMIT not in _values(c), c
    assert SITE_LIMIT not in _values(
        discover_ev_charger_from_registry(hass, include_roles=True))


@pytest.mark.parametrize("name", ["zaptec", "zaptec_no_limit"])
async def test_the_site_limit_changes_nothing_in_the_offer(hass, name):
    """An install that may set the site limit and one that may not get the
    same offer: the number is not part of what SEM drives."""
    await replay(hass, _mode_off(name))
    (n,) = [n for n in crawl(hass)["near_misses"] if n["platform"] == "zaptec"]
    offer = n["suggested_charger"]
    controls = {k: v for k, v in offer.items()
                if k.startswith(("ev_current", "ev_charger_service",
                                 "ev_service", "ev_start", "ev_stop"))}
    assert controls == {
        "ev_start_service": "button.press",
        "ev_start_service_data": json.dumps(
            {"entity_id": "button.zaptec_go2_zap012345_resume_charging"}),
        "ev_stop_service": "button.press",
        "ev_stop_service_data": json.dumps(
            {"entity_id": "button.zaptec_go2_zap012345_stop_charging_final"}),
    }
    assert offer["ev_charging_power_sensor"] == (
        "sensor.zaptec_go2_zap012345_total_charge_power")


async def test_the_roster_proposes_the_chargers_own_number(hass):
    """The report's own proposal for the integration (report data the card
    shows) names the Go's max current, never the site limit."""
    await replay(hass, load_capture("zaptec"))
    rep = crawl(hass)
    assert rep["roster_proposals"], "premise: the report proposes roles"
    assert SITE_LIMIT not in _values(rep["roster_proposals"])
    vocab = hd._roster().ROLE_VOCAB["zaptec"]["ev_current_control"]["keys"]
    assert "available_current" not in vocab
    assert "charger_max_current" in vocab
    # the word list the roster is mined with, so a refresh cannot bring it back
    from custom_components.solar_energy_management.consts import role_lexicon as lex
    for word in ("available_current", "inst1_available_current",
                 "ev_available_current"):
        assert lex.role_for("number", word) is None, word
    assert lex.role_for("number", "charger_max_current") == "ev_current_control"


# ── R1 for any brand: a companion's current is never the charger's ───────

def _site_with_a_shared_current():
    """The Zaptec capture under a domain no brand path knows, with the
    installation's number renamed to a plain charger-control word
    (``maximum_current``). Only R1 can still reach it: the word list matches
    it, the companion holds it, the Go's own number is a stored setting."""
    cap = _mode_off("zaptec")
    cap["domain"] = "rig_site_charger"
    for e in cap["entities"]:
        if e["unique_id"] == "inst1_available_current":
            e["entity_id"] = "number.abbastova_maximum_current"
            e["unique_id"] = "inst1_maximum_current"
            e["translation_key"] = "maximum_current"
    return cap


async def test_a_companions_current_is_never_adopted(hass):
    from custom_components.solar_energy_management.consts import role_lexicon as lex
    assert lex.role_for("number", "maximum_current") == "ev_current_control", (
        "premise: the word is a charger control on a charger")
    await replay(hass, _site_with_a_shared_current())
    rep = crawl(hass)
    (n,) = [n for n in rep["near_misses"] if n["platform"] == "rig_site_charger"]
    assert n["companions"], "premise: the installation is the companion"
    assert n["suggested_charger"], "the Go is still offered"
    assert "number.abbastova_maximum_current" not in _values(n["suggested_charger"])
    from custom_components.solar_energy_management.hardware_detection import (
        discover_all_ev_chargers_from_registry,
    )
    every = discover_all_ev_chargers_from_registry(hass)
    assert every, "premise: discovery offers the Go"
    for c in every:
        assert "number.abbastova_maximum_current" not in _values(c), c


def _companion_leaks(hass, rep):
    """Entities a near-miss offer names that live on its own companion,
    and how many companions were seen. Entity ids inside JSON values (a
    button press's ``entity_id``) count too."""
    from homeassistant.helpers import entity_registry as er
    reg = er.async_get(hass)
    leaks, seen = [], 0
    for n in rep["near_misses"]:
        side = {c["device_id"] for c in n.get("companions") or ()}
        seen += len(side)
        for v in (n.get("suggested_charger") or {}).values():
            if not isinstance(v, str):
                continue
            try:
                inner = json.loads(v)
            except ValueError:
                inner = None
            ids = [v] + ([str(x) for x in inner.values()]
                         if isinstance(inner, dict) else [])
            for eid in ids:
                e = reg.async_get(eid) if "." in eid else None
                if e is not None and e.device_id in side:
                    leaks.append(eid)
    return leaks, seen


@pytest.mark.parametrize("shape", ["zaptec", "zaptec_no_limit", "shared"])
async def test_no_offer_takes_a_control_from_its_companion(hass, shape):
    """The shapes that have a companion: no offer names an entity on it."""
    cap = _site_with_a_shared_current() if shape == "shared" else _mode_off(shape)
    await replay(hass, cap)
    leaks, seen = _companion_leaks(hass, crawl(hass))
    assert seen, "premise: a companion was seen"
    assert leaks == []


@pytest.mark.parametrize("name", capture_names())
async def test_no_capture_offers_a_control_from_a_companion(hass, name):
    """Every rig integration as captured. None has a companion today (the
    Zaptec Go maps through its brand rule); a new capture that has one is
    checked here without a new test."""
    await replay(hass, load_capture(name))
    leaks, _ = _companion_leaks(hass, crawl(hass))
    assert leaks == []


# ── the shapes the simulator publishes, before and after ─────────────────

def _ent(entity_id, *, device_id, unique_id="", device_class=None):
    return SimpleNamespace(
        entity_id=entity_id, platform="zaptec", device_id=device_id,
        translation_key="", unique_id=unique_id,
        original_device_class=device_class, disabled_by=None,
        config_entry_id="entry1")


def _registry(entities):
    return SimpleNamespace(entities={e.entity_id: e for e in entities},
                           async_get=lambda eid: {
                               e.entity_id: e for e in entities}.get(eid))


@pytest.mark.unit
def test_a_site_device_with_a_power_reading_gets_no_offer():
    """The old simulator fixture: the installation alone, with a power
    reading and the site limit. It was offered as a charger driven through
    the site limit; it has nothing a charger is driven by."""
    ents = [
        _ent("number.site_available_current", device_id="site",
             unique_id="sim-installation-1_available_current",
             device_class="current"),
        _ent("sensor.site_total_charge_power", device_id="site",
             unique_id="sim-installation-1_total_charge_power",
             device_class="power"),
    ]
    rep = hd.build_detection_report(registry=_registry(ents))
    for n in rep["near_misses"]:
        assert "available_current" not in _values(n.get("suggested_charger"))
        assert "available_current" not in _values(n.get("proposed_roles"))
    proposed = hd.propose_roles_from_roster(ents, "zaptec")
    assert "ev_current_control" not in proposed
    assert hd.charger_from_near_miss(ents, "zaptec", proposed) == {}


@pytest.mark.unit
def test_the_simulator_fixture_offers_the_chargers_number():
    """The new simulator fixture: the charger with its power and max
    current, the installation with its two numbers. One offer, wired to the
    charger's number; the installation is its companion, not a near miss."""
    ents = [
        _ent("number.lader_maximale_laadstroom", device_id="charger",
             unique_id="sim-charger-1_charger_max_current",
             device_class="current"),
        _ent("sensor.lader_laadvermogen", device_id="charger",
             unique_id="sim-charger-1_charge_power", device_class="power"),
        _ent("number.site_beschikbare_stroom", device_id="site",
             unique_id="sim-installation-1_available_current",
             device_class="current"),
        _ent("number.site_terugschakelen", device_id="site",
             unique_id="sim-installation-1_three_to_one_phase_switch_current",
             device_class="current"),
    ]
    rep = hd.build_detection_report(registry=_registry(ents))
    offered = [n for n in rep["near_misses"] if n.get("suggested_charger")]
    assert len(offered) == 1, rep["near_misses"]
    assert [c["device_id"] for c in offered[0]["companions"]] == ["site"]
    assert [n["device_id"] for n in rep["near_misses"]] == ["charger"]
    offer = offered[0]["suggested_charger"]
    assert offer["ev_current_control_entity"] == "number.lader_maximale_laadstroom"
    assert offer["ev_charging_power_sensor"] == "sensor.lader_laadvermogen"
    assert "beschikbare_stroom" not in _values(offer)
