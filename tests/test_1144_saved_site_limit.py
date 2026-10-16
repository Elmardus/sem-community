"""#1144 — a Zaptec saved from the old setup offer still drove the site limit.

Before #1141 the setup offer gave a Zaptec Go its installation's "Available
current" as the current control. That number limits every charger on the
site, and the owner sets it to protect the main fuse (#804). #1141 fixed the
offer; a save made before it was not changed:

* a charger is stored as a device reference, and the reference keeps the
  crawler's last answer (``found``) when a start cannot ask. A role the
  crawler stopped giving is never forgotten, so every start resolved the
  site limit again and SEM wrote it every cycle;
* the same holds for a save that holds it as the user's own value (a card
  stored as a reference after #1141, a pick by hand), for the old Zaptec
  rule's ``zaptec.limit_current`` service (before 25.08), and for the
  primary charger's old flat keys, which setup reads when the card has none.

The rule now: SEM never writes a whole site's current limit, whoever saved
it. The references drop it at every start (what the crawler finds today shows
through), the charger builder refuses it, and so does the wiring every device
builder calls. These pins run the real Zaptec capture through the real setup.
"""
from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from custom_components.solar_energy_management import charger_refs as cr
from custom_components.solar_energy_management import hardware_detection as hd

from .integrations_rig.rig import load_capture, replay
from .test_1141_site_limit_never_control import SITE_LIMIT, _mode_off

GO = "zaptec_go2_zap012345"
POWER = f"sensor.{GO}_total_charge_power"
SESSION = f"sensor.{GO}_completed_session_energy"
OWN_CURRENT = f"number.{GO}_charger_max_current"
RESUME = f"button.{GO}_resume_charging"
STOP = f"button.{GO}_stop_charging_final"

#: What the old offer handed setup for a Go the brand rule could not map
#: (captured by running the crawler at 3989b7eb, the commit before #1141).
OLD_OFFER = {
    "ev_charging_power_sensor": POWER,
    "ev_current_control_entity": SITE_LIMIT,
    "ev_session_energy_sensor": SESSION,
    "ev_start_service": "button.press",
    "ev_start_service_data": json.dumps({"entity_id": RESUME}),
    "ev_stop_service": "button.press",
    "ev_stop_service_data": json.dumps({"entity_id": STOP}),
}
#: …and what the crawler answered for the Go's device then.
OLD_LIVE = {k: OLD_OFFER[k] for k in ("ev_charging_power_sensor",
                                      "ev_current_control_entity",
                                      "ev_session_energy_sensor")}
CARD = {"id": "zaptec_go", "name": "Zaptec Go", **OLD_OFFER}


def _registry(hass):
    from homeassistant.helpers import entity_registry as er
    return er.async_get(hass)


def _device_of(hass):
    reg = _registry(hass)
    return lambda eid: getattr(reg.async_get(eid), "device_id", None)


def _saved_while_the_old_offer_was_live(hass, card=CARD):
    """The refs a start wrote while the old offer was live: the site limit
    is the crawler's answer (``found``), not the user's."""
    dev_of = _device_of(hass)
    refs, resolved = cr.reconcile(
        data_list=None, legacy=[dict(card)], refs=None, sig=None,
        device_of=dev_of, live_by_device={dev_of(POWER): OLD_LIVE})
    return refs, resolved


def _entry(hass, options, data=None):
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from custom_components.solar_energy_management.const import DOMAIN
    entry = MockConfigEntry(domain=DOMAIN, data=dict(data or {}),
                            options=dict(options))
    entry.add_to_hass(hass)
    return entry


def _values(d):
    return json.dumps(d or {}, sort_keys=True, default=dict)


# ── premise: the bug, on the real capture ────────────────────────────────

async def test_premise_the_old_save_kept_the_site_limit_every_start(hass):
    """Without the rule, today's crawler gives the Go no current control,
    yet the ref still resolves the site limit: ``found`` never forgets."""
    await replay(hass, _mode_off("zaptec"))
    refs, resolved = _saved_while_the_old_offer_was_live(hass)
    assert refs[0]["found"]["ev_current_control_entity"] == SITE_LIMIT
    assert not refs[0]["overrides"], "premise: the site limit was the crawler's"
    live = cr.live_charger_roles(hass)
    assert "ev_current_control_entity" not in live[refs[0]["device_id"]], (
        "premise: since #1141 the crawler offers no current control")
    _r, today = cr.reconcile(
        data_list=None, legacy=resolved, refs=refs,
        sig=cr.signature(resolved), device_of=_device_of(hass),
        live_by_device=live)
    assert today[0]["ev_current_control_entity"] == SITE_LIMIT


# ── the reporter's shape, through the real setup step ────────────────────

async def test_the_old_save_loses_the_site_limit_at_the_next_start(hass, caplog):
    await replay(hass, _mode_off("zaptec"))
    refs, resolved = _saved_while_the_old_offer_was_live(hass)
    entry = _entry(hass, {cr.REFS_KEY: refs, cr.LEGACY_KEY: resolved,
                          cr.SIG_KEY: cr.signature(resolved)})
    with caplog.at_level(logging.WARNING):
        out = cr.async_reconcile(hass, entry)
    (card,) = out
    assert SITE_LIMIT not in _values(card)
    # it starts and stops through its own buttons, as the offer does today
    for k in ("ev_start_service", "ev_start_service_data", "ev_stop_service",
              "ev_stop_service_data", "ev_charging_power_sensor"):
        assert card[k] == OLD_OFFER[k], k
    assert hd.charger_is_complete(card)
    # the store says so too: the refs, the list the forms read
    assert SITE_LIMIT not in _values(entry.options[cr.REFS_KEY])
    assert SITE_LIMIT not in _values(entry.options[cr.LEGACY_KEY])
    assert "#1144" in caplog.text and SITE_LIMIT in caplog.text
    # once is enough: the next start finds nothing to take out
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        again = cr.async_reconcile(hass, entry)
    assert again == out
    assert "#1144" not in caplog.text


async def test_a_users_own_pick_of_the_site_limit_goes_too(hass):
    """A card stored as a reference after #1141 holds the site limit as the
    user's own value (it differs from the crawler). #804 says never, so it
    goes too, and the Go's own control — what the brand rule finds today
    — shows through."""
    await replay(hass, load_capture("zaptec"))
    live = cr.live_charger_roles(hass)
    dev = _device_of(hass)(POWER)
    assert live[dev]["ev_current_control_entity"] == OWN_CURRENT, (
        "premise: the brand rule maps this Go")
    entry = _entry(hass, {cr.LEGACY_KEY: [dict(CARD)]})
    (card,) = cr.async_reconcile(hass, entry)
    assert card["ev_current_control_entity"] == OWN_CURRENT
    (ref,) = entry.options[cr.REFS_KEY]
    assert "ev_current_control_entity" not in ref["overrides"]
    assert SITE_LIMIT not in _values(ref)


async def test_the_old_zaptec_service_goes(hass):
    """Before 25.08 the Zaptec rule fell back to ``zaptec.limit_current``,
    which sets the installation's ``available_current``. That save drove
    the site limit every cycle, through a service."""
    await replay(hass, load_capture("zaptec"))
    dev = _device_of(hass)(POWER)
    old = {"id": "zaptec_old", "name": "Zaptec",
           "ev_charging_power_sensor": POWER,
           "ev_charger_service": "zaptec.limit_current",
           "ev_service_param_name": "available_current",
           "ev_service_device_id": dev}
    entry = _entry(hass, {cr.LEGACY_KEY: [old]})
    (card,) = cr.async_reconcile(hass, entry)
    for k in ("ev_charger_service", "ev_service_param_name",
              "ev_service_device_id"):
        assert k not in card, k
    assert "limit_current" not in _values(entry.options)
    assert card["ev_current_control_entity"] == OWN_CURRENT


async def test_the_primarys_old_flat_keys_go(hass):
    """Setup reads the primary's old flat keys when its card has none, so a
    site limit there would come back the moment the card's is gone."""
    await replay(hass, _mode_off("zaptec"))
    refs, resolved = _saved_while_the_old_offer_was_live(hass)
    entry = _entry(hass, {cr.REFS_KEY: refs, cr.LEGACY_KEY: resolved,
                          cr.SIG_KEY: cr.signature(resolved),
                          "ev_charger_service": "zaptec.limit_current",
                          "ev_service_param_name": "available_current"},
                   data={"ev_current_control_entity": SITE_LIMIT,
                         "ev_charging_power_sensor": POWER,
                         "solar_power_sensor": "sensor.pv"})
    cr.async_reconcile(hass, entry)
    assert SITE_LIMIT not in _values(entry.data)
    assert "limit_current" not in _values(entry.options)
    # nothing else is touched
    assert entry.data["ev_charging_power_sensor"] == POWER
    assert entry.data["solar_power_sensor"] == "sensor.pv"


async def test_a_charger_with_its_own_control_is_left_alone(hass, caplog):
    """The same box saved with its own number keeps it, with no warning."""
    await replay(hass, load_capture("zaptec"))
    own = {**CARD, "ev_current_control_entity": OWN_CURRENT}
    entry = _entry(hass, {cr.LEGACY_KEY: [own]})
    with caplog.at_level(logging.WARNING):
        (card,) = cr.async_reconcile(hass, entry)
    assert card == own
    assert "#1144" not in caplog.text


# ── the builder: SEM never writes it, whatever reaches setup ─────────────

def _seed_core(hass):
    for eid, val, unit in (("sensor.test_grid_power", "0", "W"),
                           ("sensor.test_battery_power", "0", "W"),
                           ("sensor.test_battery_soc", "50", "%"),
                           ("sensor.test_solar_power", "0", "W")):
        hass.states.async_set(eid, val, {"unit_of_measurement": unit})


_CORE = {"grid_power_sensor": "sensor.test_grid_power",
         "battery_power_sensor": "sensor.test_battery_power",
         "battery_soc_sensor": "sensor.test_battery_soc",
         "solar_power_sensor": "sensor.test_solar_power",
         "battery_capacity_kwh": 10.0, "update_interval": 30}


async def _set_up(hass, entry):
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry.runtime_data._ev_devices


async def _unload(hass, entry):
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.asyncio
async def test_setup_builds_the_old_save_without_the_site_limit(sem_real_hass):
    hass = sem_real_hass
    _seed_core(hass)
    await replay(hass, _mode_off("zaptec"))
    refs, resolved = _saved_while_the_old_offer_was_live(hass)
    entry = _entry(hass, {cr.REFS_KEY: refs, cr.LEGACY_KEY: resolved,
                          cr.SIG_KEY: cr.signature(resolved)},
                   data=dict(_CORE))
    devs = await _set_up(hass, entry)
    try:
        dev = devs["zaptec_go"]
        assert dev.current_entity_id is None
        assert dev.start_service == "button.press"
        assert dev.stop_service == "button.press"
        # what a start and a stop send: the Go's buttons, never the site
        from custom_components.solar_energy_management.coordinator.charger_adapters import (
            adapter_for,
        )
        sent = []

        async def _record(domain, service, data=None, *, why=""):
            sent.append((domain, service, dict(data or {})))
            return True

        with patch.object(dev, "send", side_effect=_record):
            adapter = adapter_for(dev)
            await adapter.command_current(16)
            await adapter.command_idle()
        assert ("button", "press", {"entity_id": RESUME}) in sent, sent
        assert ("button", "press", {"entity_id": STOP}) in sent, sent
        assert SITE_LIMIT not in _values(sent)
        assert not [c for c in sent if c[0] in ("number", "zaptec")], sent
    finally:
        await _unload(hass, entry)


@pytest.mark.asyncio
@pytest.mark.parametrize("where", ["card", "flat key", "service"])
async def test_the_builder_refuses_it_when_the_refs_did_not_run(
        sem_real_hass, where, caplog):
    """The refs step can fail ("chargers unchanged"), and the builder also
    reads the primary's flat keys: it asks the same question itself."""
    hass = sem_real_hass
    _seed_core(hass)
    await replay(hass, _mode_off("zaptec"))
    card = dict(CARD)
    data = dict(_CORE)
    if where == "flat key":
        card.pop("ev_current_control_entity")
        data["ev_current_control_entity"] = SITE_LIMIT
    elif where == "service":
        card.pop("ev_current_control_entity")
        card.update({"ev_charger_service": "zaptec.limit_current",
                     "ev_service_param_name": "available_current"})
    entry = _entry(hass, {cr.LEGACY_KEY: [card]}, data=data)
    with patch.object(cr, "async_reconcile",
                      side_effect=RuntimeError("refs down")), \
            caplog.at_level(logging.WARNING):
        devs = await _set_up(hass, entry)
    try:
        dev = devs["zaptec_go"]
        assert dev.current_entity_id is None
        assert not dev.charger_service
        assert dev.start_service == "button.press", (
            "the charger is still built and starts through its button")
        assert "#1144" in caplog.text
    finally:
        await _unload(hass, entry)


@pytest.mark.asyncio
async def test_a_charger_whose_only_control_was_the_site_limit_is_not_built(
        sem_real_hass):
    """Nothing left to act with: setup's own gate skips it (a charger SEM
    cannot steer is one it must not pretend to), it does not keep a device
    that writes nothing."""
    hass = sem_real_hass
    _seed_core(hass)
    await replay(hass, _mode_off("zaptec"))
    card = {"id": "zaptec_go", "ev_charging_power_sensor": POWER,
            "ev_current_control_entity": SITE_LIMIT}
    entry = _entry(hass, {cr.LEGACY_KEY: [card]}, data=dict(_CORE))
    with patch.object(cr, "async_reconcile",
                      side_effect=RuntimeError("refs down")):
        devs = await _set_up(hass, entry)
    try:
        assert "zaptec_go" not in devs
    finally:
        await _unload(hass, entry)


async def test_the_shared_wiring_drops_it_from_any_device(hass):
    """``wire_current_entity`` is what every device builder calls — a load
    with a current control too."""
    await replay(hass, load_capture("zaptec"))
    for eid, kept in ((SITE_LIMIT, None), (OWN_CURRENT, OWN_CURRENT)):
        dev = SimpleNamespace(current_entity_id=eid, start_stop_entity=None)
        hd.wire_current_entity(hass, dev, "load_1", eid)
        assert dev.current_entity_id == kept, eid


# ── what a site limit is ─────────────────────────────────────────────────

def _e(eid, uid=None, key=None):
    return SimpleNamespace(entity_id=eid, unique_id=uid, translation_key=key)


@pytest.mark.parametrize("eid,uid,key,site", [
    # the reporter's English id, and a Dutch one whose key is still English
    (SITE_LIMIT, "inst1_available_current", "available_current", True),
    ("number.jan_jansen_beschikbare_stroom", "inst1_available_current",
     "available_current", True),
    ("number.site_phase", "inst1_available_current_phase1", None, True),
    ("number.zaptec_garage_available_current", None, None, True),
    # a charger's own control, by every brand's word
    (OWN_CURRENT, "chg1_charger_max_current", "charger_max_current", False),
    ("number.wallbox_maximum_charging_current", None,
     "maximum_charging_current", False),
    ("number.goe_amp", "goe_amp", "amp", False),
    # a reading is never a control
    ("sensor.wallbox_max_available_current", None, None, False),
])
def test_what_says_a_site_limit(eid, uid, key, site):
    assert hd.says_a_site_limit(eid, _e(eid, uid, key)) is site


def test_which_keys_go():
    of = {SITE_LIMIT: _e(SITE_LIMIT, "inst1_available_current",
                         "available_current")}.get
    assert hd.site_limit_controls(
        {"ev_current_control_entity": SITE_LIMIT}, of) == (
            "ev_current_control_entity",)
    # a service is the site's when its current field is
    assert set(hd.site_limit_controls(
        {"ev_charger_service": "zaptec.limit_current",
         "ev_service_param_name": "available_current",
         "ev_service_device_id": "dev"}, of)) == {
            "ev_charger_service", "ev_service_param_name",
            "ev_service_device_id"}
    # …or when the number it sets is
    assert set(hd.site_limit_controls(
        {"ev_charger_service": "number.set_value",
         "ev_charger_service_entity_id": SITE_LIMIT}, of)) == {
            "ev_charger_service", "ev_charger_service_entity_id"}
    # a charger's own service and number stay
    for own in ({"ev_charger_service": "easee.set_charger_dynamic_limit",
                 "ev_service_param_name": "current"},
                {"ev_charger_service": "keba.set_current"},
                {"ev_current_control_entity": OWN_CURRENT}):
        assert hd.site_limit_controls(own, of) == (), own
    assert hd.site_limit_controls(None) == ()


def test_discovery_drops_a_site_limit_at_the_one_place_every_path_meets():
    """The Zaptec rule refused the site limit by unique id only; an offer of
    any brand that names one loses it at the discovery choke point."""
    ents = [_e(SITE_LIMIT, "inst1_available_current", "available_current")]
    for offer in ({"ev_current_control_entity": SITE_LIMIT},
                  {"ev_charger_service": "zaptec.limit_current",
                   "ev_service_param_name": "available_current"}):
        out = {"ev_charging_power_sensor": POWER, **offer}
        hd.apply_charger_discovery_guards(out, ents)
        assert out == {"ev_charging_power_sensor": POWER}, offer


def test_the_pure_step_leaves_a_clean_ref_as_it_is():
    ref = {"id": "a", "device_id": "d", "found": {"x": 1}, "overrides": {},
           "cleared": [], "settings": {}}
    assert cr.drop_refused([ref], {}, lambda card: ())[0] is ref
    # and a reconcile without the rule behaves as before
    refs, resolved = cr.reconcile(
        data_list=None, legacy=[dict(CARD)], refs=None, sig=None,
        device_of=lambda eid: "go", live_by_device={"go": OLD_LIVE})
    assert resolved[0]["ev_current_control_entity"] == SITE_LIMIT


def test_the_crawlers_answer_is_refused_before_it_resolves():
    """A crawler that still named a site limit (an old build, a brand not
    known yet) does not get it into the card through ``found``."""
    def refuse(card):
        return hd.site_limit_controls(card)

    refs, resolved = cr.reconcile(
        data_list=None, legacy=[{"id": "a", "ev_charging_power_sensor": POWER,
                                 "ev_start_service": "button.press"}],
        refs=None, sig=None, device_of=lambda eid: "go",
        live_by_device={"go": OLD_LIVE}, refuse=refuse)
    assert SITE_LIMIT not in _values(refs)
    assert SITE_LIMIT not in _values(resolved)


def test_the_real_saved_option_sets_are_untouched():
    """PROD's and HA-TEST's real charger lists hold no site limit."""
    from pathlib import Path
    fixtures = Path(__file__).resolve().parent / "fixtures"
    for name in ("hw_996_prod_options.json", "hw_996_hatest_options.json"):
        opts = json.loads((fixtures / name).read_text())
        assert opts["ev_chargers"], name
        for c in opts["ev_chargers"]:
            assert hd.site_limit_controls(c, MagicMock(return_value=None)) == (), c


# ── review, 16.10: the refs step, every shape ────────────────────────────

def _id_only(card):
    return hd.site_limit_controls(card)


def test_a_stale_value_behind_a_clear_never_shows_through():
    """A pre-25.08 service save that became a ref while the old offer was
    live: its card was complete, so the crawler's site limit went to
    ``found`` and the control role to ``cleared``. Dropping the service
    must not un-clear that stale value."""
    old = {"id": "z", "ev_charging_power_sensor": POWER,
           "ev_charger_service": "zaptec.limit_current",
           "ev_service_param_name": "available_current"}
    refs, resolved = cr.reconcile(
        data_list=None, legacy=[old], refs=None, sig=None,
        device_of=lambda eid: "go", live_by_device={"go": OLD_LIVE})
    assert refs[0]["found"]["ev_current_control_entity"] == SITE_LIMIT
    assert "ev_current_control_entity" in refs[0]["cleared"], "premise"
    for live in ({"go": {"ev_charging_power_sensor": POWER}},
                 {"go": {"ev_charging_power_sensor": POWER,
                         "ev_current_control_entity": OWN_CURRENT}}):
        r1, out = cr.reconcile(
            data_list=None, legacy=resolved, refs=refs,
            sig=cr.signature(resolved), device_of=lambda eid: "go",
            live_by_device=live, refuse=_id_only)
        assert SITE_LIMIT not in _values(r1) and SITE_LIMIT not in _values(out)
        assert "limit_current" not in _values(out)
        # what the crawler finds today shows through
        assert out[0].get("ev_current_control_entity") == live["go"].get(
            "ev_current_control_entity")
        # and the next start has nothing left to take out
        r2, again = cr.reconcile(
            data_list=None, legacy=out, refs=r1, sig=cr.signature(out),
            device_of=lambda eid: "go", live_by_device=live, refuse=_id_only)
        assert (r2, again) == (r1, out)


def test_a_part_keeps_a_value_the_card_does_not_use():
    """The user's own number wins over a stale site limit in ``found``:
    the stale value goes, the user's stays."""
    ref = {"id": "z", "device_id": "go", "cleared": [], "settings": {},
           "found": {"ev_charging_power_sensor": POWER,
                     "ev_current_control_entity": SITE_LIMIT},
           "overrides": {"ev_current_control_entity": OWN_CURRENT}}
    (out,) = cr.drop_refused([ref], {}, _id_only)
    assert out["overrides"] == {"ev_current_control_entity": OWN_CURRENT}
    assert out["found"] == {"ev_charging_power_sensor": POWER}


def test_a_ref_the_drop_left_half_can_be_replaced():
    """Power and the site limit only: after the drop it is a half card, so
    a new complete card for the same box replaces it (#1054), and is not
    turned away as a copy."""
    half = {"id": "z", "ev_charging_power_sensor": POWER,
            "ev_current_control_entity": SITE_LIMIT}
    refs, resolved = cr.reconcile(
        data_list=None, legacy=[half], refs=None, sig=None,
        device_of=lambda eid: "go", live_by_device={}, refuse=_id_only)
    assert refs[0]["stored_complete"] is False
    assert not hd.charger_is_complete(resolved[0])
    fresh = {"id": "z2", "ev_charging_power_sensor": POWER,
             "ev_start_service": "button.press",
             "ev_start_service_data": json.dumps({"entity_id": RESUME})}
    _r, out = cr.reconcile(
        data_list=None, legacy=resolved + [fresh], refs=refs,
        sig=cr.signature(resolved), device_of=lambda eid: "go",
        live_by_device={}, refuse=_id_only)
    assert [c["id"] for c in out] == ["z2"]
    assert out[0]["ev_start_service"] == "button.press"


def test_a_site_limit_does_not_say_which_device_a_card_is():
    """The site limit sits on the site's device. With one vote each, the
    site could win the tie, and the Go's crawler answer never show."""
    card = {"id": "z", "ev_charging_power_sensor": POWER,
            "ev_current_control_entity": SITE_LIMIT,
            "ev_start_service": "button.press"}
    dev_of = {POWER: "b_go", SITE_LIMIT: "a_site"}.get
    refs, out = cr.reconcile(
        data_list=None, legacy=[card], refs=None, sig=None,
        device_of=dev_of,
        live_by_device={"b_go": {"ev_charging_power_sensor": POWER,
                                 "ev_current_control_entity": OWN_CURRENT}},
        refuse=_id_only)
    assert refs[0]["device_id"] == "b_go"
    assert out[0]["ev_current_control_entity"] == OWN_CURRENT


def test_the_zaptec_rule_skips_the_site_limit_and_takes_the_next():
    """The brand rule alone, without the choke point: an id fallback that
    took the site limit first never reached the charger's own number."""
    def ent(eid, dc=None, uid=""):
        return SimpleNamespace(entity_id=eid, platform="zaptec",
                               device_id="go", unique_id=uid,
                               original_device_class=dc,
                               translation_key=None, original_name=None,
                               has_entity_name=False, disabled_by=None,
                               unit_of_measurement=None,
                               capabilities=None, entity_category=None)
    found = hd._discover_zaptec([
        ent("binary_sensor.zaptec_garage_cable_connected"),
        ent("number.zaptec_garage_available_current", "current"),
        ent("number.zaptec_garage_charging_current", "current"),
        ent("sensor.zaptec_garage_total_charge_power", "power"),
    ])
    assert found["ev_current_control_entity"] == (
        "number.zaptec_garage_charging_current")


def test_start_and_stop_keep_the_device_id_they_share():
    """The start and stop services name the box by the same device id; only
    the current service goes."""
    keys = hd.site_limit_controls({
        "ev_charger_service": "zaptec.limit_current",
        "ev_service_param_name": "available_current",
        "ev_service_device_id": "dev",
        "ev_start_service": "zaptec.resume_charging"})
    assert set(keys) == {"ev_charger_service", "ev_service_param_name"}


def test_an_entity_service_is_judged_by_its_target_not_a_field_name():
    """``number.set_value`` writes its target and reads no field name, so a
    stray ``available_current`` field (an old flat key) says nothing."""
    assert hd.site_limit_controls({
        "ev_charger_service": "number.set_value",
        "ev_charger_service_entity_id": OWN_CURRENT,
        "ev_service_param_name": "available_current"}) == ()
