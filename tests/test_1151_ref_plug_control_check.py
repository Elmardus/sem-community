"""#1151 — a charger got a plug sensor without the control check.

A charger SEM saved by itself is stored as a device reference, and every
start merges the crawler's answer into it (``refresh_found``). So when the
crawler learns to read a plug — a Peblar's ``enum`` state (#1126), a
Zaptec's "Charger mode" (#1142) — a save that had none gets one.

The startup heal (``_fill_state_sensors_of_own_save``) gives setup's own
save its sensors only when its power and controls are what detection finds
today (#1134): seeing the car puts them to work, and a save made by an older
detection can hold the wrong ones (a Peblar's single-phase switch as
start/stop, #1035). A save that already has a plug skips the heal, so the
reference handed one around the check, and the stale control was driven.

The rule now: a reference adds a plug or charging sensor the charger did not
read only when each control its card drives is what the crawler finds
today. Otherwise the sensors are held back and the heal decides; every
start asks again.
"""
from __future__ import annotations

import json
import random
from unittest.mock import patch

from custom_components.solar_energy_management import charger_refs as cr
from custom_components.solar_energy_management import hardware_detection as hd

from .test_1139_orphan_charger_repairs import GO, _zaptec

LINK = f"binary_sensor.{GO}_charger"          # "Charger": the box is online
MODE = f"sensor.{GO}_charger_mode"            # the charger's real state
POWER = f"sensor.{GO}_charge_power"
CURRENT = f"number.{GO}_charger_max_current"
RESUME = f"button.{GO}_resume_charging"   # what the crawler gives as start/stop
LOCK = f"switch.{GO}_permanent_cable_lock"


def _registry(hass):
    from homeassistant.helpers import entity_registry as er
    return er.async_get(hass)


def _device_of(hass):
    reg = _registry(hass)
    return lambda eid: getattr(reg.async_get(eid), "device_id", None)


def _values(d):
    return json.dumps(d or {}, sort_keys=True, default=dict)


def _start(hass, entry):
    """What setup does, in its order: the refs step, then the state heal on
    the list it wrote (``__init__``), saved back."""
    from custom_components.solar_energy_management import (
        _fill_state_sensors_of_own_save,
    )
    cr.async_reconcile(hass, entry)
    healed = _fill_state_sensors_of_own_save(
        hass, entry.options.get(cr.LEGACY_KEY))
    if healed is not None:
        hass.config_entries.async_update_entry(
            entry, options={**entry.options, cr.LEGACY_KEY: healed})
    return entry.options[cr.LEGACY_KEY]


async def _ref_made_before_1142(hass, **change):
    """Setup's own Zaptec save (no plug, the link as the charge), stored as
    a reference at a start whose crawler did not read "Charger mode" yet
    (2.2.0-beta.19): that crawler gave the link as the charge and no plug.
    ``change`` is what the save held that differs from detection."""
    from custom_components.solar_energy_management import (
        build_discovered_charger_storage,
    )
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from custom_components.solar_energy_management.const import DOMAIN
    await _zaptec(hass, with_limit=False)
    found = [c for c in hd.discover_all_ev_chargers_from_registry(hass)
             if c.get("_platform") == "zaptec"][0]
    old = {k: v for k, v in found.items() if k != "ev_connected_sensor"}
    old["ev_charging_sensor"] = LINK
    (own,) = build_discovered_charger_storage({}, {}, {**old, **change})[2]
    dev_of = _device_of(hass)
    today = cr.live_charger_roles(hass)[dev_of(POWER)]
    old_live = {dev_of(POWER): {
        **{k: v for k, v in today.items() if k not in cr.STATE_ROLES},
        "ev_charging_sensor": LINK}}
    refs, resolved = cr.reconcile(
        data_list=[own], legacy=None, refs=None, sig=None,
        device_of=dev_of, live_by_device=old_live)
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={
        cr.REFS_KEY: refs, cr.LEGACY_KEY: resolved,
        cr.SIG_KEY: cr.signature(resolved)})
    entry.add_to_hass(hass)
    return entry, found


# ── premise ──────────────────────────────────────────────────────────────

async def test_premise_the_crawler_hands_the_plug_to_a_stale_control(hass):
    """Non-vacuous: the save holds the cable lock as start/stop (an
    override, since the old crawler gave the charging switch), and merging
    today's answer gives it the plug next to the lock. The heal then sees a
    plug and skips the save."""
    from custom_components.solar_energy_management import (
        _fill_state_sensors_of_own_save,
    )
    entry, found = await _ref_made_before_1142(hass, ev_start_stop_entity=LOCK)
    (ref,) = entry.options[cr.REFS_KEY]
    assert ref["overrides"] == {"ev_start_stop_entity": LOCK}
    assert "ev_connected_sensor" not in ref["cleared"]
    assert found["ev_start_stop_entity"] == RESUME
    live = cr.live_charger_roles(hass)
    (merged,) = cr.refresh_found([ref], live)
    card = cr.resolve(merged, live.get(ref["device_id"]))
    assert card["ev_connected_sensor"] == MODE
    assert card["ev_start_stop_entity"] == LOCK
    assert _fill_state_sensors_of_own_save(hass, [card]) is None


# ── the issue's shape, through the real setup step and the real heal ─────

async def test_a_stale_control_is_replaced_before_the_car_is_seen(hass):
    entry, found = await _ref_made_before_1142(hass, ev_start_stop_entity=LOCK)
    (card,) = _start(hass, entry)
    assert card["ev_start_stop_entity"] == RESUME
    assert card["ev_connected_sensor"] == MODE
    assert card["ev_charging_sensor"] == MODE
    assert LINK not in _values(entry.options)
    # the next start folds what the heal gave, and keeps it
    (again,) = _start(hass, entry)
    assert again == card
    (ref,) = entry.options[cr.REFS_KEY]
    assert LOCK not in _values(ref)
    assert "held" not in ref
    assert not {"ev_connected_sensor", "ev_charging_sensor"} & set(
        ref["cleared"])
    (third,) = _start(hass, entry)
    assert third == card


async def test_without_the_heal_the_car_is_never_seen_through_the_lock(hass):
    """The refs step alone: the new sensors wait for the heal."""
    entry, _found = await _ref_made_before_1142(
        hass, ev_start_stop_entity=LOCK)
    (card,) = cr.async_reconcile(hass, entry)
    assert card["ev_start_stop_entity"] == LOCK
    assert not card.get("ev_connected_sensor")
    assert not card.get("ev_charging_sensor")


async def test_a_control_detection_does_not_give_keeps_the_sensors_away(hass):
    """(#1126) A start/stop the crawler no longer finds: the heal leaves
    the save, so it never sees the car through that switch."""
    entry, _found = await _ref_made_before_1142(
        hass, ev_start_stop_entity=LOCK)
    _registry(hass).async_remove(RESUME)
    hass.states.async_remove(RESUME)
    assert "ev_start_stop_entity" not in cr.live_charger_roles(hass).get(
        _device_of(hass)(POWER), {}), "premise"
    for _ in range(2):
        (card,) = _start(hass, entry)
        assert card["ev_start_stop_entity"] == LOCK
        assert not card.get("ev_connected_sensor")
        assert not card.get("ev_charging_sensor")


async def test_controls_detection_gives_get_the_plug_at_once(hass):
    """The save drives what the crawler finds: the reference adds the plug
    itself, with no heal."""
    entry, _found = await _ref_made_before_1142(hass)
    (card,) = cr.async_reconcile(hass, entry)
    assert card["ev_connected_sensor"] == MODE
    assert card["ev_charging_sensor"] == MODE
    assert card["ev_start_stop_entity"] == RESUME
    (again,) = _start(hass, entry)
    assert again == card


# ── the pure step ────────────────────────────────────────────────────────

PLUG = "sensor.peblar_ev_charger_state"
PHASE = "switch.peblar_ev_charger_force_single_phase"
CHARGE = "switch.peblar_ev_charger_charge"
P_POWER = "sensor.peblar_ev_charger_power"
LIMIT = "number.peblar_ev_charger_charge_limit"
#: today's crawler for core's Peblar (#1035, #1126)
PEBLAR_LIVE = {"ev_charging_power_sensor": P_POWER,
               "ev_current_control_entity": LIMIT,
               "ev_start_stop_entity": CHARGE,
               "ev_connected_sensor": PLUG, "ev_charging_sensor": PLUG}
PEBLAR_SAVE = {"id": "peblar_1", "name": "Peblar Charger",
               "ev_charging_power_sensor": P_POWER,
               "ev_current_control_entity": LIMIT,
               "ev_start_stop_entity": PHASE}


def _peblar_device(eid):
    return "peblar-1" if "peblar" in eid else None


def _without_state(live):
    return {k: v for k, v in live.items() if k not in cr.STATE_ROLES}


def _ref_then_today(save, then, today=None):
    """``save`` stored as a ref under the crawler ``then``; the next start
    under ``today`` (the legacy copy is what the first start wrote)."""
    today = PEBLAR_LIVE if today is None else today
    refs, resolved = cr.reconcile(
        data_list=[dict(save)], legacy=None, refs=None, sig=None,
        device_of=_peblar_device, live_by_device={"peblar-1": then})
    return cr.reconcile(
        data_list=None, legacy=resolved, refs=refs,
        sig=cr.signature(resolved), device_of=_peblar_device,
        live_by_device={"peblar-1": today} if today else {})


class TestTheRule:

    def test_the_single_phase_switch_never_sees_the_car(self):
        """The issue's example: a Peblar saved before 2.2.0-beta.5, stored
        as a ref before the crawler read its state."""
        refs, (card,) = _ref_then_today(
            PEBLAR_SAVE, _without_state(PEBLAR_LIVE))
        assert card["ev_start_stop_entity"] == PHASE
        assert not card.get("ev_connected_sensor")
        assert not card.get("ev_charging_sensor")
        assert refs[0]["held"] == ["ev_charging_sensor", "ev_connected_sensor"]
        assert refs[0]["cleared"] == [], "nothing the user chose"
        json.loads(json.dumps(refs))  # what the store keeps

    def test_a_save_that_matches_gets_the_plug(self):
        save = {**PEBLAR_SAVE, "ev_start_stop_entity": CHARGE}
        _refs, (card,) = _ref_then_today(save, _without_state(PEBLAR_LIVE))
        assert card["ev_connected_sensor"] == PLUG
        assert card["ev_charging_sensor"] == PLUG

    def test_a_control_only_the_old_crawler_gave_is_stale(self):
        """A German Peblar: today's crawler finds no start/stop, so the old
        answer (the single-phase switch) stays in ``found``. Today's answer
        decides, not the remembered one."""
        then = {**_without_state(PEBLAR_LIVE), "ev_start_stop_entity": PHASE}
        today = {k: v for k, v in PEBLAR_LIVE.items()
                 if k != "ev_start_stop_entity"}
        refs, (card,) = _ref_then_today(PEBLAR_SAVE, then, today)
        assert refs[0]["found"]["ev_start_stop_entity"] == PHASE, "premise"
        assert card["ev_start_stop_entity"] == PHASE
        assert not card.get("ev_connected_sensor")

    def test_a_start_service_the_crawler_cannot_answer_for_holds_it(self):
        save = {**PEBLAR_SAVE, "ev_start_stop_entity": CHARGE,
                "ev_start_service": "peblar.start"}
        _refs, (card,) = _ref_then_today(save, _without_state(PEBLAR_LIVE))
        assert not card.get("ev_connected_sensor")

    def test_a_blind_start_adds_nothing_and_holds_nothing(self):
        refs, (card,) = _ref_then_today(
            PEBLAR_SAVE, _without_state(PEBLAR_LIVE), today={})
        assert not card.get("ev_connected_sensor")
        assert "held" not in refs[0]

    def test_a_user_card_with_its_own_power_keeps_its_plug(self):
        """The forms ask for both sensors: a card that read a plug keeps it,
        whatever else differs from the crawler."""
        mine = {**PEBLAR_SAVE, "id": "ev_charger_1",
                "ev_charging_power_sensor": "sensor.shelly_wallbox_power",
                "ev_start_stop_entity": CHARGE,
                "ev_connected_sensor": PLUG, "ev_charging_sensor": PLUG}
        refs, (card,) = _ref_then_today(mine, PEBLAR_LIVE)
        assert card == mine
        assert refs[0]["cleared"] == []
        assert "held" not in refs[0]

    def test_a_charger_that_read_the_car_gets_the_other_sensor(self):
        """(#1143) The wizard's save read its plug: the charge the crawler
        finds today is added, whatever its controls."""
        save = {**PEBLAR_SAVE, "ev_connected_sensor": PLUG}
        _refs, (card,) = _ref_then_today(save, _without_state(PEBLAR_LIVE))
        assert card["ev_start_stop_entity"] == PHASE, "premise"
        assert card["ev_charging_sensor"] == PLUG

    def test_a_plug_the_card_read_may_change(self):
        """Not new: the crawler follows a rename of the plug it gave."""
        save = {**PEBLAR_SAVE, "ev_connected_sensor": PLUG}
        renamed = {**PEBLAR_LIVE, "ev_connected_sensor": PLUG + "_2",
                   "ev_charging_sensor": PLUG + "_2"}
        _refs, (card,) = _ref_then_today(save, PEBLAR_LIVE, renamed)
        assert card["ev_connected_sensor"] == PLUG + "_2"

    def test_a_link_as_the_charge_counts_as_no_sensor(self):
        """(#1143) The save's charging sensor said only "online": the plug
        and the real charge it gets are new, so they are checked."""
        link = "binary_sensor.peblar_ev_charger_online"
        save = {**PEBLAR_SAVE, "ev_charging_sensor": link}
        then = {**_without_state(PEBLAR_LIVE), "ev_charging_sensor": link}
        refs, resolved = cr.reconcile(
            data_list=[save], legacy=None, refs=None, sig=None,
            device_of=_peblar_device, live_by_device={"peblar-1": then})

        def refuse(card):
            return (("ev_charging_sensor",)
                    if card.get("ev_charging_sensor") == link else ())
        _refs, (card,) = cr.reconcile(
            data_list=None, legacy=resolved, refs=refs,
            sig=cr.signature(resolved), device_of=_peblar_device,
            live_by_device={"peblar-1": PEBLAR_LIVE}, refuse=refuse)
        assert not card.get("ev_connected_sensor")
        assert not card.get("ev_charging_sensor")

    def test_a_half_card_is_still_completed_when_it_matches(self):
        """#1054: the crawler completes a half card."""
        half = {"id": "peblar_1", "name": "Peblar Charger",
                "ev_total_energy_sensor": "sensor.my_peblar_total"}
        refs, (card,) = cr.reconcile(
            data_list=[half], legacy=None, refs=None, sig=None,
            device_of=_peblar_device, live_by_device={"peblar-1": PEBLAR_LIVE})
        assert card["ev_connected_sensor"] == PLUG
        assert card["ev_start_stop_entity"] == CHARGE

    def test_a_half_card_with_a_stale_power_waits_for_the_heal(self):
        half = {"id": "peblar_1", "name": "Peblar Charger",
                "ev_charging_power_sensor": "sensor.peblar_ev_charger_power_phase_3"}
        refs, (card,) = cr.reconcile(
            data_list=[half], legacy=None, refs=None, sig=None,
            device_of=_peblar_device, live_by_device={"peblar-1": PEBLAR_LIVE})
        assert not card.get("ev_connected_sensor")

    def test_held_once_is_quiet(self):
        refs, resolved = _ref_then_today(
            PEBLAR_SAVE, _without_state(PEBLAR_LIVE))
        logged = []
        again = cr.reconcile(
            data_list=None, legacy=resolved, refs=refs,
            sig=cr.signature(resolved), device_of=_peblar_device,
            live_by_device={"peblar-1": PEBLAR_LIVE}, log=logged.append)
        assert again == (refs, resolved)
        assert logged == []


class TestHeldIsAskedAgain:
    """A hold is not a choice: every start asks again."""

    def _held(self):
        refs, resolved = _ref_then_today(
            PEBLAR_SAVE, _without_state(PEBLAR_LIVE))
        assert refs[0]["held"], "premise"
        return refs, resolved

    def _next(self, refs, legacy, live, sig=None):
        return cr.reconcile(
            data_list=None, legacy=legacy, refs=refs,
            sig=cr.signature(legacy) if sig is None else sig,
            device_of=_peblar_device, live_by_device={"peblar-1": live})

    def test_a_start_that_saw_part_of_the_device_lets_go_later(self):
        """A crawler that reads the plug but not yet the start/stop holds a
        save whose controls are right; the next full start shows it."""
        save = {**PEBLAR_SAVE, "ev_start_stop_entity": CHARGE}
        part = {k: v for k, v in PEBLAR_LIVE.items()
                if k != "ev_start_stop_entity"}
        refs, (card,) = _ref_then_today(save, _without_state(PEBLAR_LIVE),
                                        part)
        assert refs[0]["held"] and not card.get("ev_connected_sensor")
        refs, (card,) = self._next(refs, [card], PEBLAR_LIVE)
        assert card["ev_connected_sensor"] == PLUG
        assert "held" not in refs[0]

    def test_a_control_the_user_sets_right_lets_go(self):
        """The user picks the charge switch (a role write, not the form):
        the next start shows the plug."""
        refs, (card,) = self._held()
        mine = {**card, "ev_start_stop_entity": CHARGE}
        refs, (card,) = self._next(refs, [mine], PEBLAR_LIVE,
                                   sig=cr.signature([card]))
        assert card["ev_start_stop_entity"] == CHARGE
        assert card["ev_connected_sensor"] == PLUG
        assert "held" not in refs[0]

    def test_a_slider_change_keeps_it_held(self):
        refs, (card,) = self._held()
        moved = {**card, "ev_min_current": 8}
        refs, (card,) = self._next(refs, [moved], PEBLAR_LIVE,
                                   sig=cr.signature([card]))
        assert card["ev_min_current"] == 8
        assert not card.get("ev_connected_sensor")
        assert refs[0]["held"]

    def test_a_writer_that_names_only_the_plug_lets_both_go(self):
        """The card read the car through the plug the writer set: the charge
        the crawler finds is not kept back either."""
        refs, (card,) = self._held()
        assert refs[0]["held"] == ["ev_charging_sensor", "ev_connected_sensor"]
        mine = {**card, "ev_connected_sensor": PLUG}
        refs, (card,) = self._next(refs, [mine], PEBLAR_LIVE,
                                   sig=cr.signature([card]))
        assert card["ev_connected_sensor"] == PLUG
        assert card["ev_charging_sensor"] == PLUG
        assert "held" not in refs[0]

    def test_a_link_set_by_hand_does_not_make_the_hold_last(self):
        """(#1143) A role write sets the online sensor as the charge, with
        no plug. The link rule must not clear the charge for good because
        the hold hides the plug: once the control is right, both show."""
        refs, (card,) = self._held()
        link = "binary_sensor.peblar_ev_charger_online"

        def refuse(c):
            return (("ev_charging_sensor",)
                    if c.get("ev_charging_sensor") == link else ())
        mine = {**card, "ev_charging_sensor": link}
        refs, (card,) = cr.reconcile(
            data_list=None, legacy=[mine], refs=refs, sig=cr.signature([card]),
            device_of=_peblar_device, live_by_device={"peblar-1": PEBLAR_LIVE},
            refuse=refuse)
        assert not card.get("ev_connected_sensor")
        assert not card.get("ev_charging_sensor")
        assert refs[0]["cleared"] == []
        assert refs[0]["held"] == ["ev_charging_sensor", "ev_connected_sensor"]
        right = {**card, "ev_start_stop_entity": CHARGE}
        refs, (card,) = cr.reconcile(
            data_list=None, legacy=[right], refs=refs, sig=cr.signature([card]),
            device_of=_peblar_device, live_by_device={"peblar-1": PEBLAR_LIVE},
            refuse=refuse)
        assert card["ev_connected_sensor"] == PLUG
        assert card["ev_charging_sensor"] == PLUG

    def test_the_heal_s_fill_is_folded_and_not_held(self):
        refs, (card,) = self._held()
        filled = {**card, "ev_start_stop_entity": CHARGE,
                  "ev_connected_sensor": PLUG, "ev_charging_sensor": PLUG}
        refs, (card,) = self._next(refs, [filled], PEBLAR_LIVE,
                                   sig=cr.signature([card]))
        assert card == filled
        assert "held" not in refs[0]
        assert refs[0]["overrides"] == {}


def test_the_heal_gives_the_held_save_the_right_switch():
    """The issue's Peblar through the real heal (core's test data): the
    held save has neither sensor, so the heal replaces the switch and fills
    both; the next start folds it and the plug stays."""
    from custom_components.solar_energy_management import (
        build_discovered_charger_storage,
    )
    from .test_1126_enum_status_sensor import _discover, _heal, _peblar
    found = dict(_discover(_peblar())[0])
    today = {k: v for k, v in found.items() if k in cr.ROLE_KEYS and v}
    assert today["ev_connected_sensor"], "premise: the crawler reads a plug"
    old = {k: v for k, v in found.items() if k not in cr.STATE_ROLES}
    (save,) = build_discovered_charger_storage(
        {}, {}, {**old, "ev_start_stop_entity": PHASE})[2]
    refs, (card,) = _ref_then_today(save, _without_state(today), today)
    assert card["ev_start_stop_entity"] == PHASE
    assert not card.get("ev_connected_sensor")
    healed = _heal([card], _peblar())
    assert healed[0]["ev_start_stop_entity"] == found["ev_start_stop_entity"]
    assert healed[0]["ev_connected_sensor"] == today["ev_connected_sensor"]
    refs, (folded,) = cr.reconcile(
        data_list=None, legacy=healed, refs=refs, sig=cr.signature([card]),
        device_of=_peblar_device, live_by_device={"peblar-1": today})
    assert folded == healed[0]
    assert PHASE not in _values(refs)
    _refs, (again,) = cr.reconcile(
        data_list=None, legacy=[folded], refs=refs,
        sig=cr.signature([folded]), device_of=_peblar_device,
        live_by_device={"peblar-1": today})
    assert again == folded


# ── the oracle: no path through the setup step skips the check ───────────

_ROLE_VALUES = {
    "ev_charging_power_sensor": ("sensor.p", "sensor.p_phase_3"),
    "ev_current_control_entity": ("number.limit", "number.old"),
    "ev_start_stop_entity": ("switch.charge", "switch.single_phase"),
    "ev_charge_mode_entity": ("select.mode", "select.led"),
    "ev_connected_sensor": ("sensor.state", "binary_sensor.plug"),
    "ev_charging_sensor": ("sensor.state", "binary_sensor.online"),
}


def _random_roles(rng, dev):
    return {k: f"{v}_{dev}" for k, v in (
        (k, rng.choice(v)) for k, v in _ROLE_VALUES.items())
            if rng.random() < 0.6}


def _drives_what_today_finds(card, live):
    return all((card.get(k) or None) == (live.get(k) or None)
               for k in cr.STATE_MUST_MATCH)


def _refuse(card):
    """The online sensor is refused as the charge (#1143)."""
    return (("ev_charging_sensor",)
            if str(card.get("ev_charging_sensor")).startswith(
                "binary_sensor.online") else ())


def _sees_a_car(card):
    return any(card.get(k) and k not in _refuse(card) for k in cr.STATE_ROLES)


def test_a_charger_sees_its_first_car_only_with_todays_controls():
    """Every shape the setup step meets — first start, writers' edits, three
    later starts, a store without the legacy copy; two chargers; any old and
    new crawler — holds the rule."""
    rng = random.Random(1151)
    devs = ("d1", "d2")

    def device_of(eid):
        return eid.rsplit("_", 1)[-1] if eid.endswith(devs) else None
    asked = added = 0
    for _ in range(1500):
        saves = []
        for dev in devs:
            save = {"id": f"c_{dev}", "name": dev, **_random_roles(rng, dev)}
            if not save.get("ev_charging_power_sensor"):
                save["ev_charging_power_sensor"] = f"sensor.p_{dev}"
            if rng.random() < 0.6:
                # setup's own save of an older detection: it read no car
                save = {k: v for k, v in save.items()
                        if k not in cr.STATE_ROLES}
            saves.append(save)
        live = {dev: _random_roles(rng, dev) for dev in devs}
        refs, resolved = cr.reconcile(
            data_list=saves, legacy=None, refs=None, sig=None,
            device_of=device_of, live_by_device=live, refuse=_refuse)
        for _start in range(3):
            legacy, sig = resolved, cr.signature(resolved)
            if rng.random() < 0.3:
                # a writer's edit between two starts
                legacy = [dict(c, **_random_roles(rng, c["name"]))
                          if rng.random() < 0.5 else c for c in resolved]
            if rng.random() < 0.1:
                # no legacy copy: what the charger ran on is the last card
                legacy = sig = None
            ran = {c["id"]: c for c in legacy or resolved}
            live = {dev: _random_roles(rng, dev) for dev in devs}
            refs, resolved = cr.reconcile(
                data_list=None, legacy=legacy, refs=refs, sig=sig,
                device_of=device_of, live_by_device=live, refuse=_refuse)
            json.loads(json.dumps(refs))
            for card in resolved:
                today = {k: v for k, v in live[card["name"]].items()
                         if k not in _refuse(live[card["name"]])}
                if _sees_a_car(ran[card["id"]]):
                    continue
                if any(today.get(k) for k in cr.STATE_ROLES):
                    asked += 1
                if any(card.get(k) for k in cr.STATE_ROLES):
                    added += 1
                    assert _drives_what_today_finds(card, today), (
                        ran[card["id"]], today, card)
    # both answers are seen often enough to mean something
    assert asked > 1000 and 50 < added < asked, (asked, added)


def test_the_heal_and_the_step_ask_the_same_keys():
    """One list: the heal's control check is the step's."""
    import custom_components.solar_energy_management as pkg
    assert pkg._STATE_HEAL_MUST_MATCH is cr.STATE_MUST_MATCH


def test_the_check_runs_after_every_step_that_changes_a_ref():
    """A step added after it would hand a plug around it again: the last
    word on ``refs`` before anything resolves is the check."""
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(cr.reconcile).lstrip())
    sets = sorted((n for n in ast.walk(tree) if isinstance(n, ast.Assign)
                   and any(isinstance(t, ast.Name) and t.id == "refs"
                           for t in n.targets)),
                  key=lambda n: n.lineno)
    last = sets[-1].value
    assert isinstance(last, ast.Call) and isinstance(last.func, ast.Name) \
        and last.func.id == "hold_new_state", ast.dump(last)
    reads = [n.lineno for n in ast.walk(tree) if isinstance(n, ast.Assign)
             and any(isinstance(t, ast.Name) and t.id == "resolved"
                     for t in n.targets)]
    assert reads and min(reads) > sets[-1].lineno
    with patch.object(cr, "hold_new_state", lambda refs, *a, **k: refs):
        _refs, (card,) = _ref_then_today(
            PEBLAR_SAVE, _without_state(PEBLAR_LIVE))
    assert card["ev_connected_sensor"] == PLUG, "the step is what holds it"


def test_without_a_stored_list_the_ref_says_what_it_ran_on():
    """No legacy copy (a store a writer emptied): what the ref resolved to
    before this start is what the charger ran on."""
    refs, _resolved = cr.reconcile(
        data_list=[dict(PEBLAR_SAVE)], legacy=None, refs=None, sig=None,
        device_of=_peblar_device,
        live_by_device={"peblar-1": _without_state(PEBLAR_LIVE)})
    _refs, (card,) = cr.reconcile(
        data_list=None, legacy=None, refs=refs, sig=None,
        device_of=_peblar_device, live_by_device={"peblar-1": PEBLAR_LIVE})
    assert card["ev_start_stop_entity"] == PHASE
    assert not card.get("ev_connected_sensor")


def test_a_wizard_save_over_a_held_charger_reads_the_charge():
    """(#1143) The user saves the held charger again through the wizard,
    which prefilled the online sensor as the charge. Their plug is set, so
    nothing is held any more — before the link rule asks whether the card
    has a plug, or it would clear the charge for good."""
    plug_only = {k: v for k, v in PEBLAR_LIVE.items()
                 if k != "ev_charging_sensor"}
    refs, (card,) = _ref_then_today(
        {**PEBLAR_SAVE, "id": "z"}, _without_state(PEBLAR_LIVE), plug_only)
    assert refs[0]["held"] == ["ev_connected_sensor"], "premise"
    link = "binary_sensor.peblar_ev_charger_online"
    wizard = {**card, "ev_start_stop_entity": CHARGE,
              "ev_connected_sensor": PLUG, "ev_charging_sensor": link}

    def refuse(c):
        return (("ev_charging_sensor",)
                if c.get("ev_charging_sensor") == link else ())
    refs, (card,) = cr.reconcile(
        data_list=None, legacy=[wizard], refs=refs, sig=cr.signature([card]),
        device_of=_peblar_device, live_by_device={"peblar-1": PEBLAR_LIVE},
        refuse=refuse)
    assert card["ev_connected_sensor"] == PLUG
    assert card["ev_charging_sensor"] == PLUG
    assert "held" not in refs[0]
    assert "ev_charging_sensor" not in refs[0]["cleared"]
