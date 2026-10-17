"""#1152 — a charger given a plug on 2.2.0-beta.20 was not checked again.

On beta.20 every start merged the crawler's answer into a charger SEM saved
by itself, with no check of its controls, so a Zaptec saved before #1142
got "Charger mode" as its plug. #1151 checks a charger that read no car
before the start; this one reads one now, so it was never asked. A Zaptec
saved before 2.2.0-beta.5 can hold a switch that does not start a charge
(the cable lock), and SEM drives it once it sees the car.

The store does not say where the plug came from. The check is asked once,
of setup's own save only (the id setup gives the box — the startup heal's
rule), when every state sensor its card reads came from the crawler and is
what it finds today. A card the user set up is never asked. Each ref is
marked ``plug_checked`` so it is asked only once.
"""
from __future__ import annotations

import json
from unittest.mock import patch

from homeassistant.config_entries import ConfigEntryState

from custom_components.solar_energy_management import charger_refs as cr

from .test_1151_ref_plug_control_check import (
    LOCK, MODE, PEBLAR_LIVE, PEBLAR_SAVE, PHASE, CHARGE, PLUG, RESUME,
    _peblar_device, _ref_made_before_1142, _registry, _start, _values,
    _without_state,
)


def _beta20_start(refs, resolved, live, device_of):
    """A start of 2.2.0-beta.20: the crawler's answer merged, nothing held,
    no check."""
    with patch.object(cr, "hold_new_state", lambda refs, *a, **k: refs):
        return cr.reconcile(
            data_list=None, legacy=resolved, refs=refs,
            sig=cr.signature(resolved), device_of=device_of,
            live_by_device=live)


async def _plugged_on_beta20(hass, **change):
    """Setup's own Zaptec save, stored as a ref before #1142, then one
    beta.20 start: the save got "Charger mode" with no check."""
    entry, found = await _ref_made_before_1142(hass, **change)
    reg = _registry(hass)
    device_of = (lambda e: getattr(reg.async_get(e), "device_id", None))
    refs, resolved = _beta20_start(
        entry.options[cr.REFS_KEY], entry.options[cr.LEGACY_KEY],
        cr.live_charger_roles(hass), device_of)
    hass.config_entries.async_update_entry(entry, options={
        cr.REFS_KEY: refs, cr.LEGACY_KEY: resolved,
        cr.SIG_KEY: cr.signature(resolved)})
    return entry, found


# ── premise ──────────────────────────────────────────────────────────────

async def test_premise_beta20_gave_the_lock_a_plug_and_1151_does_not_ask(hass):
    entry, _found = await _plugged_on_beta20(hass, ev_start_stop_entity=LOCK)
    (card,) = entry.options[cr.LEGACY_KEY]
    assert card["ev_connected_sensor"] == MODE
    assert card["ev_start_stop_entity"] == LOCK
    (ref,) = entry.options[cr.REFS_KEY]
    assert cr.PLUG_CHECKED not in ref
    # the #1151 step alone: the card read a car, so nothing is held
    with patch.object(cr, "check_saved_plugs",
                      lambda refs, ran, *a, **k: (refs, ran)):
        (card,) = cr.async_reconcile(hass, entry)
    assert card["ev_connected_sensor"] == MODE
    assert card["ev_start_stop_entity"] == LOCK


# ── the issue's shape, through the real setup step and the real heal ─────

async def test_the_lock_is_replaced_before_the_car_is_seen(hass):
    entry, _found = await _plugged_on_beta20(hass, ev_start_stop_entity=LOCK)
    (card,) = _start(hass, entry)
    assert card["ev_start_stop_entity"] == RESUME
    assert card["ev_connected_sensor"] == MODE
    assert card["ev_charging_sensor"] == MODE
    (ref,) = entry.options[cr.REFS_KEY]
    assert ref[cr.PLUG_CHECKED] is True
    # the next starts fold what the heal gave and keep it
    (again,) = _start(hass, entry)
    assert again == card
    (ref,) = entry.options[cr.REFS_KEY]
    assert LOCK not in _values(ref)
    assert "held" not in ref
    (third,) = _start(hass, entry)
    assert third == card


async def test_a_control_detection_does_not_give_keeps_the_plug_away(hass):
    """The heal cannot match the lock: the card reads no car, and the
    #1151 check keeps asking every start."""
    entry, _found = await _plugged_on_beta20(hass, ev_start_stop_entity=LOCK)
    _registry(hass).async_remove(RESUME)
    hass.states.async_remove(RESUME)
    for _ in range(3):
        (card,) = _start(hass, entry)
        assert card["ev_start_stop_entity"] == LOCK
        assert not card.get("ev_connected_sensor")
        assert not card.get("ev_charging_sensor")


async def test_controls_detection_gives_keep_their_plug(hass):
    entry, _found = await _plugged_on_beta20(hass)
    (card,) = cr.async_reconcile(hass, entry)
    assert card["ev_connected_sensor"] == MODE
    assert card["ev_start_stop_entity"] == RESUME
    (ref,) = entry.options[cr.REFS_KEY]
    assert ref[cr.PLUG_CHECKED] is True
    assert "held" not in ref


async def test_a_user_card_is_never_asked(hass):
    """The same card under the id the forms give (``ev_charger``): the
    user may have chosen the lock; their card stays as it is."""
    entry, _found = await _plugged_on_beta20(hass, ev_start_stop_entity=LOCK)
    (ref,) = entry.options[cr.REFS_KEY]
    (card,) = entry.options[cr.LEGACY_KEY]
    mine_ref = {**ref, "id": "ev_charger"}
    mine = {**card, "id": "ev_charger"}
    hass.config_entries.async_update_entry(entry, options={
        cr.REFS_KEY: [mine_ref], cr.LEGACY_KEY: [mine],
        cr.SIG_KEY: cr.signature([mine])})
    for _ in range(2):
        (card,) = _start(hass, entry)
        assert card["ev_start_stop_entity"] == LOCK
        assert card["ev_connected_sensor"] == MODE


async def test_a_user_card_is_marked_once_the_device_is_seen_whole(hass):
    entry, _found = await _plugged_on_beta20(hass, ev_start_stop_entity=LOCK)
    (ref,) = entry.options[cr.REFS_KEY]
    (card,) = entry.options[cr.LEGACY_KEY]
    hass.config_entries.async_update_entry(entry, options={
        cr.REFS_KEY: [{**ref, "id": "ev_charger"}],
        cr.LEGACY_KEY: [{**card, "id": "ev_charger"}],
        cr.SIG_KEY: cr.signature([{**card, "id": "ev_charger"}])})
    cr.async_reconcile(hass, entry)
    assert cr.PLUG_CHECKED not in entry.options[cr.REFS_KEY][0], (
        "Zaptec not loaded: not seen whole, asked again later")
    (zaptec,) = hass.config_entries.async_entries("zaptec")
    zaptec.mock_state(hass, ConfigEntryState.LOADED)
    cr.async_reconcile(hass, entry)
    assert entry.options[cr.REFS_KEY][0][cr.PLUG_CHECKED] is True


async def test_asked_once_only(hass):
    """Once marked, a ref that reads a car is not asked again — as any
    ref the #1151 check sees."""
    entry, _found = await _plugged_on_beta20(hass, ev_start_stop_entity=LOCK)
    (ref,) = entry.options[cr.REFS_KEY]
    (card,) = entry.options[cr.LEGACY_KEY]
    hass.config_entries.async_update_entry(entry, options={
        **entry.options, cr.REFS_KEY: [{**ref, cr.PLUG_CHECKED: True}]})
    (card,) = _start(hass, entry)
    assert card["ev_start_stop_entity"] == LOCK
    assert card["ev_connected_sensor"] == MODE


async def test_without_an_answer_for_own_saves_nothing_is_marked(hass):
    entry, _found = await _plugged_on_beta20(hass, ev_start_stop_entity=LOCK)
    (zaptec,) = hass.config_entries.async_entries("zaptec")
    zaptec.mock_state(hass, ConfigEntryState.LOADED)
    with patch.object(cr, "own_save_ids", lambda hass: None):
        (card,) = cr.async_reconcile(hass, entry)
    assert card["ev_start_stop_entity"] == LOCK
    assert cr.PLUG_CHECKED not in entry.options[cr.REFS_KEY][0]


# ── the pure step ────────────────────────────────────────────────────────

OWN = PEBLAR_SAVE["id"]


def _old_plugged(save=PEBLAR_SAVE):
    """A Peblar save stored as a ref before #1126, then a beta.20 start
    that gave it the plug."""
    refs, resolved = cr.reconcile(
        data_list=[dict(save)], legacy=None, refs=None, sig=None,
        device_of=_peblar_device,
        live_by_device={"peblar-1": _without_state(PEBLAR_LIVE)})
    return _beta20_start(refs, resolved, {"peblar-1": PEBLAR_LIVE},
                         _peblar_device)


def _now(refs, resolved, *, own=(OWN,), sure=(), live=PEBLAR_LIVE, **kw):
    return cr.reconcile(
        data_list=None, legacy=resolved, refs=refs,
        sig=cr.signature(resolved), device_of=_peblar_device,
        live_by_device={"peblar-1": live} if live else {},
        sure=set(sure), own=set(own) if own is not None else None, **kw)


class TestTheRule:

    def test_the_single_phase_switch_is_held(self):
        refs, resolved = _old_plugged()
        assert resolved[0]["ev_connected_sensor"] == PLUG, "premise"
        logged = []
        refs, (card,) = _now(refs, resolved, log=logged.append)
        assert card["ev_start_stop_entity"] == PHASE
        assert not card.get("ev_connected_sensor")
        assert refs[0]["held"] == ["ev_charging_sensor", "ev_connected_sensor"]
        assert refs[0][cr.PLUG_CHECKED] is True
        assert refs[0]["cleared"] == [], "nothing the user chose"
        assert any("#1152" in m for m in logged)
        json.loads(json.dumps(refs))

    def test_the_hold_lasts_through_the_1151_check(self):
        refs, resolved = _old_plugged()
        refs, resolved = _now(refs, resolved)
        refs, (card,) = _now(refs, resolved)
        assert not card.get("ev_connected_sensor")
        assert refs[0]["held"]

    def test_a_save_that_matches_keeps_its_plug(self):
        refs, resolved = _old_plugged(
            {**PEBLAR_SAVE, "ev_start_stop_entity": CHARGE})
        refs, (card,) = _now(refs, resolved)
        assert card["ev_connected_sensor"] == PLUG
        assert "held" not in refs[0]
        assert refs[0][cr.PLUG_CHECKED] is True

    def test_not_setup_s_own_save_is_not_asked(self):
        refs, resolved = _old_plugged()
        refs, (card,) = _now(refs, resolved, own=())
        assert card["ev_connected_sensor"] == PLUG
        assert cr.PLUG_CHECKED not in refs[0], "device not seen whole"
        refs, (card,) = _now(refs, [card], own=(), sure=("peblar-1",))
        assert card["ev_connected_sensor"] == PLUG
        assert refs[0][cr.PLUG_CHECKED] is True

    def test_a_plug_the_user_set_is_not_asked(self):
        """A plug of their own (an override) says the card is theirs."""
        refs, resolved = _old_plugged()
        mine = {**resolved[0], "ev_connected_sensor": "binary_sensor.my_plug"}
        refs, resolved = cr.reconcile(
            data_list=None, legacy=[mine], refs=refs,
            sig=cr.signature(resolved), device_of=_peblar_device,
            live_by_device={"peblar-1": PEBLAR_LIVE})
        assert refs[0]["overrides"]["ev_connected_sensor"] == mine[
            "ev_connected_sensor"], "premise"
        refs, (card,) = _now(refs, resolved)
        assert card["ev_connected_sensor"] == "binary_sensor.my_plug"
        assert "held" not in refs[0]

    def test_a_plug_the_user_set_that_detection_now_gives_is_not_asked(self):
        """The user's plug is what the crawler finds today (it learned it
        later): still theirs."""
        refs, resolved = _old_plugged()
        ref = {**refs[0], "overrides": {**refs[0]["overrides"],
                                        "ev_connected_sensor": PLUG}}
        out, ran = cr.check_saved_plugs(
            [ref], {OWN: resolved[0]}, {"peblar-1": PEBLAR_LIVE},
            sure=set(), own={OWN}, fresh=())
        assert ran == {OWN: resolved[0]}
        assert out[0][cr.PLUG_CHECKED] is True

    def test_a_plug_detection_no_longer_gives_is_not_asked_here(self):
        """The check is for the plug the crawler gives today."""
        refs, resolved = _old_plugged()
        today = {**PEBLAR_LIVE, "ev_connected_sensor": PLUG + "_2",
                 "ev_charging_sensor": PLUG + "_2"}
        refs, (card,) = _now(refs, resolved, live=today)
        assert "held" not in refs[0]

    def test_a_blind_start_marks_nothing(self):
        refs, resolved = _old_plugged()
        refs, (card,) = _now(refs, resolved, live=None)
        assert cr.PLUG_CHECKED not in refs[0]
        assert card["ev_connected_sensor"] == PLUG

    def test_without_own_the_step_does_not_run(self):
        refs, resolved = _old_plugged()
        refs, (card,) = _now(refs, resolved, own=None, sure=("peblar-1",))
        assert cr.PLUG_CHECKED not in refs[0]
        assert card["ev_connected_sensor"] == PLUG

    def test_a_ref_made_at_this_start_is_marked(self):
        """The #1151 check covers a new ref: it is marked at once."""
        refs, resolved = cr.reconcile(
            data_list=[dict(PEBLAR_SAVE)], legacy=None, refs=None, sig=None,
            device_of=_peblar_device,
            live_by_device={"peblar-1": PEBLAR_LIVE}, sure=set(), own=set())
        assert refs[0][cr.PLUG_CHECKED] is True

    def test_a_device_less_charger_is_never_marked(self):
        keba = {"id": "keba", "name": "KEBA",
                "ev_charging_power_sensor": "sensor.keba_power",
                "ev_charger_service": "keba.set_current"}
        refs, resolved = cr.reconcile(
            data_list=[keba], legacy=None, refs=None, sig=None,
            device_of=lambda e: None, live_by_device={})
        refs, (card,) = cr.reconcile(
            data_list=None, legacy=resolved, refs=refs,
            sig=cr.signature(resolved), device_of=lambda e: None,
            live_by_device={}, sure=set(), own={"keba"})
        assert card == keba
        assert cr.PLUG_CHECKED not in refs[0]

    def test_a_link_as_the_charge_is_not_a_plug(self):
        """(#1143) A card that read only an online sensor as the charge read
        no car: the #1151 check already asks it, this step leaves it."""
        link = "binary_sensor.peblar_ev_charger_online"
        refs, resolved = _old_plugged()
        ran = {OWN: {**_without_state(resolved[0]), "ev_charging_sensor": link}}

        def refuse(card):
            return (("ev_charging_sensor",)
                    if card.get("ev_charging_sensor") == link else ())
        out, ran_after = cr.check_saved_plugs(
            refs, ran, {"peblar-1": PEBLAR_LIVE}, sure=set(), own={OWN},
            fresh=(), refuse=refuse)
        assert ran_after == ran
        assert out[0][cr.PLUG_CHECKED] is True


def test_the_check_comes_before_the_hold():
    import inspect
    src = inspect.getsource(cr.reconcile)
    assert src.index("check_saved_plugs(") < src.index("hold_new_state(")


def test_the_heal_and_the_check_read_one_own_rule():
    import inspect
    import custom_components.solar_energy_management as pkg
    assert "_own_saves(hass)" in inspect.getsource(
        pkg._fill_state_sensors_of_own_save)
    assert "_own_saves" in inspect.getsource(cr.own_save_ids)
