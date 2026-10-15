"""#1139 — a charger's Repair outlives the charger.

@OddmarDam (Zaptec Go, 2.2.0-beta.18) still had two ERROR Repairs "EV charger
control entity is not working" for a "New charger 1" he had already removed:
``switch.abbastova_charging`` and ``number.abbastova_charger_max_current``,
reason ``missing``. A third named SEM's own pause button, which his charger no
longer used.

Those ids were real once. They are the Zaptec Go's own "Charging" switch and
"Charger max current" number under the device name "Abbastova"; the box was
renamed later (his diagnostics show a third name, ``garage_zaptec_box_1``),
HA renamed the ids, and the stored ones went missing. SEM builds no id from a
device name, and setup offers the Go, not its installation (pinned below on
his real layout). What was wrong: the Repair is persistent and only the
charger's own control check clears it — for an entity that charger still
names. A removed charger, or a replaced entity, had nobody left to clear it.

Bug class 84 (left open (2)): a Repair keyed on what the config no longer
holds. The sweep runs once per setup, after the chargers are registered —
every config change reloads SEM.
"""
from __future__ import annotations

import ast
import inspect
from types import SimpleNamespace

from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

import custom_components.solar_energy_management as sem_init
from custom_components.solar_energy_management import (
    _sweep_orphan_charger_repairs,
)
from custom_components.solar_energy_management.const import DOMAIN
from custom_components.solar_energy_management.coordinator import (
    repair_issues as ri,
)
from custom_components.solar_energy_management.coordinator.coordinator import (
    SEMCoordinator,
)

from .test_708_soc_cap_percharger import PER_CHARGER_REPAIRS

GO = "garage_zaptec_ev_charger"
OLD = "abbastova"

#: The charger he runs now (his own config, posted on #1032).
ZEPTO_GO = {
    "id": "ev_charger",
    "name": "Zepto Go",
    "ev_connected_sensor": f"sensor.{GO}_charger_mode",
    "ev_start_stop_entity": f"switch.{GO}_charging",
    "ev_current_control_entity": f"number.{GO}_charger_max_current",
    "ev_charging_power_sensor": f"sensor.{GO}_charge_power",
}

#: How each per-charger Repair is raised — the REAL raisers, so the id the
#: sweep must match is the one production files.
RAISE = {
    "raise_charger_failsafe_suspected": lambda h, cid: (
        ri.raise_charger_failsafe_suspected(h, cid, name="x", interval_s=60.0)),
    "raise_charger_stop_unenforceable": lambda h, cid: (
        ri.raise_charger_stop_unenforceable(
            h, cid, name="x", power_w=1.0, entity="number.x")),
    "raise_charger_stop_war_stand_down": lambda h, cid: (
        ri.raise_charger_stop_war_stand_down(
            h, cid, name="x", power_w=1.0, minutes=1.0)),
    "raise_charger_phase_count_mismatch": lambda h, cid: (
        ri.raise_charger_phase_count_mismatch(
            h, cid, name="x", believed=3, measured=1, watts_per_amp=230.0,
            nominal_wpa=690.0, samples=20)),
    "raise_soc_cap_unenforceable": lambda h, cid: (
        ri.raise_soc_cap_unenforceable(h, cid, name="x", target_soc=80.0)),
    "raise_charger_actuation_failed": lambda h, cid: (
        ri.raise_charger_actuation_failed(h, cid, name="x", error="e")),
    "raise_charger_control_entity_broken": lambda h, cid: (
        ri.raise_charger_control_entity_broken(
            h, cid, name="x", entity_id="switch.x",
            capability="start and stop charging", reason="missing")),
}

#: Raised by a device that is not always a charger: a load driven by current
#: files ``charger_actuation_failed`` under its own id, and the sweep knows no
#: load ids. Kept out on purpose (decision record, #1139).
NOT_SWEPT = {"raise_charger_actuation_failed"}


def _coordinator(*ids):
    return SimpleNamespace(
        _CONTROL_CAPABILITIES=SEMCoordinator._CONTROL_CAPABILITIES,
        _ev_devices={cid: object() for cid in ids})


def _ids(hass):
    return {i for (d, i) in ir.async_get(hass).issues if d == DOMAIN}


def _broken(hass, cid, eid, reason="missing"):
    ri.raise_charger_control_entity_broken(
        hass, cid, name="x", entity_id=eid,
        capability="start and stop charging", reason=reason)
    return ri._control_entity_issue_id(cid, eid)


# ── his Repairs ──────────────────────────────────────────────────────────

async def test_the_removed_chargers_repairs_go(hass):
    gone = [_broken(hass, "ev_charger_1", f"switch.{OLD}_charging"),
            _broken(hass, "ev_charger_1", f"number.{OLD}_charger_max_current")]
    replaced = _broken(hass, "ev_charger",
                       "button.sem_charger_ev_charger_pause_charging",
                       reason="wrong_domain")
    live = _broken(hass, "ev_charger", f"switch.{GO}_charging",
                   reason="unavailable")

    cleared = _sweep_orphan_charger_repairs(
        hass, _coordinator("ev_charger"), [ZEPTO_GO])

    assert cleared == 3
    assert not ({*gone, replaced} & _ids(hass))
    # the entity Zepto Go still names keeps its Repair: its own check owns it
    assert live in _ids(hass)


async def test_nothing_goes_while_every_owner_is_there(hass):
    """Vacuity twin: same Repairs, every charger and entity still listed."""
    second = {"id": "ev_charger_1",
              "ev_start_stop_entity": f"switch.{OLD}_charging",
              "ev_current_control_entity": f"number.{OLD}_charger_max_current"}
    raised = {_broken(hass, "ev_charger_1", f"switch.{OLD}_charging"),
              _broken(hass, "ev_charger_1", f"number.{OLD}_charger_max_current"),
              _broken(hass, "ev_charger", f"switch.{GO}_charging")}
    for cid in ("ev_charger", "ev_charger_1"):
        RAISE["raise_soc_cap_unenforceable"](hass, cid)

    cleared = _sweep_orphan_charger_repairs(
        hass, _coordinator("ev_charger", "ev_charger_1"), [ZEPTO_GO, second])

    assert cleared == 0
    assert raised <= _ids(hass)


async def test_the_same_entity_on_another_charger_is_not_a_licence(hass):
    """The id carries (charger, entity): a helper the kept charger also names
    does not keep the removed charger's Repair."""
    shared = f"switch.{GO}_charging"
    gone = _broken(hass, "ev_charger_1", shared)
    kept = _broken(hass, "ev_charger", shared)
    _sweep_orphan_charger_repairs(hass, _coordinator("ev_charger"), [ZEPTO_GO])
    assert gone not in _ids(hass)
    assert kept in _ids(hass)


async def test_no_charger_left_clears_them_all(hass):
    gone = _broken(hass, "ev_charger", f"switch.{GO}_charging")
    RAISE["raise_soc_cap_unenforceable"](hass, "ev_charger")
    _sweep_orphan_charger_repairs(hass, _coordinator(), [])
    assert not {gone, ri._soc_cap_issue_id("ev_charger")} & _ids(hass)


async def test_a_registered_fallback_charger_keeps_its_repairs(hass):
    """A charger registered under a positional or flat-key id is in
    ``_ev_devices`` but not listed by id: its per-charger Repairs stay."""
    RAISE["raise_charger_stop_unenforceable"](hass, "ev_charger_0")
    _sweep_orphan_charger_repairs(hass, _coordinator("ev_charger_0"),
                                  [{"name": "no id"}])
    assert ri._stop_unenforceable_issue_id("ev_charger_0") in _ids(hass)


async def test_other_repairs_are_not_touched(hass):
    ri.raise_sensor_unavailable(hass, "sensor.grid", minutes_unavailable=10)
    ri.raise_charger_actuation_failed(hass, "pool_pump", name="Pool", error="e")
    before = _ids(hass)
    _sweep_orphan_charger_repairs(hass, _coordinator(), [])
    assert _ids(hass) == before


# ── the class: every per-charger Repair has a decision ──────────────────

def test_every_per_charger_repair_is_swept_or_says_why():
    """A new per-charger Repair must say what clears it once its charger is
    gone — the #1139 shape is exactly a Repair with no answer to that."""
    assert set(RAISE) == set(PER_CHARGER_REPAIRS), (
        "PER_CHARGER_REPAIRS changed: teach RAISE how to file the new one, "
        "then decide here whether the #1139 sweep clears it.")


async def test_each_swept_repair_of_a_removed_charger_goes(hass):
    filed = {}
    for name, raise_it in RAISE.items():
        before = _ids(hass)
        raise_it(hass, "ev_charger_9")
        (filed[name],) = _ids(hass) - before   # the id production files

    _sweep_orphan_charger_repairs(hass, _coordinator("ev_charger"), [ZEPTO_GO])

    left = _ids(hass)
    assert {name for name, iid in filed.items() if iid in left} == NOT_SWEPT


# ── the call site ───────────────────────────────────────────────────────

def test_setup_sweeps_after_the_chargers_are_registered():
    """Deleting the call leaves every test above green — so pin it: setup
    calls the sweep with the list it registered, after registering it."""
    tree = ast.parse(inspect.getsource(sem_init.async_setup_entry))
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and getattr(n.func, "id", None) == "_sweep_orphan_charger_repairs"]
    assert len(calls) == 1
    (call,) = calls
    assert [getattr(a, "id", None) for a in call.args] == [
        "hass", "coordinator", "ev_chargers_config"]
    registered = [n.lineno for n in ast.walk(tree)
                  if isinstance(n, ast.Assign)
                  and any(getattr(t, "attr", None) == "_ev_device"
                          for t in n.targets)]
    assert registered and call.lineno > max(registered)


# ── setup picks the Go, never its installation (his real layout) ────────

#: custom-components/zaptec v0.8.7: (domain, own id, unique-id key,
#: translation key, device class, unit). The installation's "active" sensor
#: is named "Installation", the charger's "Charger"; both keep the bare id as
#: unique id. Ids are built from the English names, as HA builds them.
_INSTALLATION = [
    ("binary_sensor", "installation", "", None, "connectivity", None),
    ("binary_sensor", "authorization_required", "is_required_authentication",
     "authorization_required", None, None),
    ("number", "3_to_1_phase_switch_current", "three_to_one_phase_switch_current",
     "three_to_one_phase_switch_current", "current", "A"),
    ("sensor", "max_current", "max_current", "max_current", "current", "A"),
    ("sensor", "authentication_type", "authentication_type",
     "authentication_type", "enum", None),
    ("sensor", "installation_type", "installation_type", "installation_type",
     "enum", None),
    ("sensor", "network_type", "network_type", "network_type", "enum", None),
]
_AVAILABLE_CURRENT = ("number", "available_current", "available_current",
                      "available_current", "current", "A")
_CHARGER = [
    ("binary_sensor", "charger", "", None, "connectivity", None),
    ("binary_sensor", "online", "is_online", "online", "connectivity", None),
    ("button", "authorize_charging", "authorize_charge", "authorize_charge", None, None),
    ("button", "resume_charging", "resume_charging", "resume_charging", None, None),
    ("button", "stop_charging", "stop_charging_final", "stop_charging", None, None),
    ("number", "charger_min_current", "charger_min_current",
     "charger_min_current", "current", "A"),
    ("number", "charger_max_current", "charger_max_current",
     "charger_max_current", "current", "A"),
    ("sensor", "charger_mode", "charger_operation_mode",
     "charger_operation_mode", "enum", None),
    ("sensor", "completed_session_energy", "completed_session.Energy",
     "completed_session_energy", "energy", "kWh"),
    ("sensor", "energy_meter", "signed_meter_value_kwh", "signed_meter_value",
     "energy", "kWh"),
    ("sensor", "session_total_charge", "total_charge_power_session",
     "total_charge_power_session", "energy", "kWh"),
    ("sensor", "charge_power", "total_charge_power", "total_charge_power",
     "power", "W"),
    ("switch", "charging", "charger_operation_mode", "charger_operation_mode",
     "switch", None),
    ("switch", "permanent_cable_lock", "permanent_cable_lock",
     "permanent_cable_lock", None, None),
]


async def _zaptec(hass, *, with_limit: bool):
    entry = MockConfigEntry(domain="zaptec", title="zaptec")
    entry.add_to_hass(hass)
    dreg, ereg = dr.async_get(hass), er.async_get(hass)
    inst = dreg.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("zaptec", "inst-1")},
        name="Abbastova", model="Installation", manufacturer="Zaptec")
    go = dreg.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("zaptec", "chg-1")},
        name="Zaptec EV charger", model="Zaptec Go", manufacturer="Zaptec",
        via_device=("zaptec", "inst-1"))
    rows = [(OLD, inst, "inst-1", e) for e in
            _INSTALLATION + ([_AVAILABLE_CURRENT] if with_limit else [])]
    rows += [(GO, go, "chg-1", e) for e in _CHARGER]
    for slug, dev, uid, (dom, own, key, tk, dc, unit) in rows:
        eid = f"{dom}.{slug}_{own}"
        row = ereg.async_get_or_create(
            dom, "zaptec", f"{uid}_{key}" if key else uid,
            suggested_object_id=f"{slug}_{own}", config_entry=entry,
            device_id=dev.id, translation_key=tk, original_device_class=dc,
            unit_of_measurement=unit)
        if row.entity_id != eid:
            ereg.async_update_entity(row.entity_id, new_entity_id=eid)
        hass.states.async_set(eid, "disconnected" if own == "charger_mode"
                              else "16", {"device_class": dc} if dc else {})
    return inst.id, go.id


import pytest  # noqa: E402


@pytest.mark.parametrize("with_limit", [False, True],
                         ids=["no-limit-right", "limit-right"])
async def test_setup_offers_the_go_and_never_its_installation(hass, with_limit):
    from custom_components.solar_energy_management.hardware_detection import (
        CHARGER_ENTITY_KEYS, discover_all_ev_chargers_from_registry)
    inst_id, go_id = await _zaptec(hass, with_limit=with_limit)

    found = discover_all_ev_chargers_from_registry(hass)

    (charger,) = found
    assert charger["_device_id"] == go_id
    roles = {k: v for k, v in charger.items() if k in CHARGER_ENTITY_KEYS}
    assert roles and not [v for v in roles.values() if f".{OLD}_" in str(v)]
    assert charger["ev_current_control_entity"] == (
        f"number.{GO}_charger_max_current")
    # the one installation entity offered is the phase threshold, as a
    # suggestion the user confirms (#804 B4c) — never a role
    assert charger["_suggested_phase_switch"]["entity"] == (
        f"number.{OLD}_3_to_1_phase_switch_current")
