"""#1092 — every integration, from the crawler to a working charger.

``test_integrations_rig`` asks what the crawler FINDS. This file asks what
happens NEXT, through SEM's real setup and its real services:

1. the crawler offers a charger, and the offer is complete — the create
   step never guesses (a service's other fields are filled, a control
   exists);
2. the user creates it ("Create this charger entry");
3. SEM runs exactly one charger, wired the way the crawler said;
4. a restart keeps it;
5. pressing "Create" again makes no second one;
6. an install that carries an old, incomplete card for the same box (the
   shape weindler's go-e upgraded with, #1054) ends with the working
   charger, and deleting the old card keeps it deleted.

Assertions are about OUTCOMES — what runs, what the detection page says,
what is left behind — never about how the config entry stores a charger,
so a change of storage (#1054) is judged by the same tests.

Parametrized over every capture in ``integrations_rig/captures/`` (real
integrations' output, fixtures only) and every shape in
``integrations_rig/shapes.py`` (how integrations differ, no brand). A new
capture or shape is new cases, no code. Known faults are
``xfail(strict=True)`` with the issue that owns them, so a fix flips them.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import pytest

from .integrations_rig.rig import capture_names, load_capture, replay
from .integrations_rig.shapes import SHAPE_GAPS, shape_names, shapes

#: Known faults, by case: the issue that owns each.
KNOWN_FAULTS: Dict[str, str] = {
    **SHAPE_GAPS,
}

#: Captures where the crawler offers no charger to create — each with the
#: reason. Every capture is a charger case or named here; a new capture that
#: is neither fails ``test_every_capture_is_classified``.
NO_OFFER: Dict[str, str] = {
    "goecharger_live_weindler": "a download from before 2.2.0-beta.16 "
        "carries no units or states, so the crawler cannot see the power "
        "reading it saw live; a new download does (the go-e round trip in "
        "test_integrations_diag_capture proves it)",
    "entsoe": "a price feed",
    "entsoe_named_15m": "a price feed",
    "helios_forecast": "a solar forecast",
    "vicare": "a heat pump",
    "tesla_fleet": "a car, paired to a charger, not one",
    "teslemetry": "a car, paired to a charger, not one",
    "tessie": "a car, paired to a charger, not one",
    "tesla_wall_connector": "read-only: the integration offers no control "
                            "(an upstream limit)",
    "blue_current": "the capture holds only its buttons (core snapshots "
                    "nothing else)",
}

#: The keys that make a charger able to act.
_ACTS = ("ev_charger_service", "ev_current_control_entity",
         "ev_start_stop_entity", "ev_start_service", "ev_charge_mode_entity")


def _case(name: str) -> Dict[str, Any]:
    return shapes()[name] if name.startswith("shape_") else load_capture(name)


def _cases() -> List[Any]:
    out = []
    for name in [*capture_names(), *shape_names()]:
        if name in NO_OFFER:
            continue
        marks = []
        if name in KNOWN_FAULTS:
            marks.append(pytest.mark.xfail(strict=True, reason=KNOWN_FAULTS[name]))
        out.append(pytest.param(name, marks=marks, id=name))
    return out


@pytest.fixture(autouse=True)
def _discovery_without_the_wait(monkeypatch):
    """Load management waits 30 s before its first device discovery; every
    reload here would wait for it. The discovery itself still runs."""
    from custom_components.solar_energy_management.features import load_management

    async def _no_wait(_seconds, *a, **k):
        return None
    monkeypatch.setattr(load_management.asyncio, "sleep", _no_wait)


def _entry(chargers: Optional[List[Dict[str, Any]]] = None,
           data_chargers: Optional[List[Dict[str, Any]]] = None):
    """SEM before a charger is added — or, given ``chargers``, an install
    that already carries them."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from custom_components.solar_energy_management.const import DOMAIN
    data: Dict[str, Any] = {
        "grid_power_sensor": "sensor.test_grid_power",
        "battery_power_sensor": "sensor.test_battery_power",
        "battery_soc_sensor": "sensor.test_battery_soc",
        "solar_power_sensor": "sensor.test_solar_power",
        "battery_capacity_kwh": 10.0,
        "update_interval": 30,
        "electricity_import_rate": 0.30,
        "electricity_export_rate": 0.08,
    }
    options: Dict[str, Any] = {}
    if data_chargers is not None:
        data["ev_chargers"] = data_chargers
        data["has_ev"] = False
    if chargers is not None:
        options["ev_chargers"] = chargers
    return MockConfigEntry(domain=DOMAIN, version=12, minor_version=1,
                           data=data, options=options,
                           title="SEM lifecycle rig")


def _seed(hass) -> None:
    for eid, val, unit in (
            ("sensor.test_grid_power", "0", "W"),
            ("sensor.test_battery_power", "0", "W"),
            ("sensor.test_battery_soc", "50", "%"),
            ("sensor.test_solar_power", "0", "W")):
        hass.states.async_set(eid, val, {"unit_of_measurement": unit})


async def _start(hass, entry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def _report(entry) -> Dict[str, Any]:
    coord = entry.runtime_data
    coord.refresh_detection_report()
    return coord._detection_report or {}


def _mine(rows, domain: str):
    return [r for r in rows or []
            if str(r.get("platform") or "").split("_rig")[0] == domain]


def _offer(hass, report, domain: str) -> Optional[Dict[str, Any]]:
    """What "Create this charger entry" saves: the complete offer on a near
    miss, else the charger the setup walk finds for this integration."""
    for n in _mine(report.get("near_misses"), domain):
        sc = n.get("suggested_charger") or {}
        if sc.get("id") and not n.get("missing"):
            return dict(sc)
    from custom_components.solar_energy_management.hardware_detection import (
        discover_all_ev_chargers_from_registry,
    )
    found = [c for c in discover_all_ev_chargers_from_registry(hass)
             if str(c.get("_platform") or domain).split("_rig")[0] == domain]
    if found:
        return {"id": found[0].get("id") or f"{domain}_charger", **found[0]}
    return None


def _service_fields(hass, service: str) -> List[str]:
    from custom_components.solar_energy_management.hardware_detection import (
        _described_fields, _services_of,
    )
    dom, _, svc = service.partition(".")
    of = _services_of(hass)
    fields = list(((of(dom).fields if of else {}) or {}).get(svc) or [])
    return fields or list(_described_fields(hass, dom, svc) or [])


#: Service fields that pick WHICH box — the create step cannot guess them.
_BOX_FIELDS = ("device_id", "charger_name", "charger_id", "serial", "charger")


def _offer_gaps(hass, offer: Dict[str, Any]) -> List[str]:
    """What the create step would have to guess. Empty = it guesses nothing."""
    gaps = []
    if not offer.get("ev_charging_power_sensor"):
        gaps.append("no power reading")
    if not any(offer.get(k) for k in _ACTS):
        gaps.append("no way to act")
    svc = offer.get("ev_charger_service")
    if svc and "." in svc and not svc.startswith(("number.", "button.", "switch.")):
        data = json.loads(offer.get("ev_charger_service_data") or "{}")
        if offer.get("ev_service_device_id"):
            data["device_id"] = offer["ev_service_device_id"]
        for f in _service_fields(hass, svc):
            if f in _BOX_FIELDS and f not in data:
                gaps.append(f"service {svc} needs '{f}', the offer leaves it out")
    return gaps


def _running(entry) -> List[str]:
    return list(entry.runtime_data._ev_devices.keys())


def _assert_one_working(entry, offer: Dict[str, Any], domain: str) -> str:
    """ONE charger runs, wired to the crawler's power reading and control;
    the detection page calls it configured and offers no second; nothing is
    left for an id SEM does not run. Returns its id."""
    running = _running(entry)
    assert len(running) == 1, f"SEM runs {running}, want exactly one charger"
    cid = running[0]
    rows = [r for r in (_report(entry).get("chargers") or []) if r.get("configured")]
    assert [r.get("charger_id") for r in rows] == [cid], (
        f"detection page calls {[r.get('charger_id') for r in rows]} "
        f"configured, SEM runs {cid}")
    mapped = rows[0].get("mapped") or {}

    def _wired(key):
        m = mapped.get(key) or {}
        return m.get("entity") or m.get("value")
    assert _wired("ev_charging_power_sensor") == offer["ev_charging_power_sensor"], (
        f"power reads {_wired('ev_charging_power_sensor')}, the crawler said "
        f"{offer['ev_charging_power_sensor']}")
    acts = [k for k in _ACTS if offer.get(k)]
    assert any(_wired(k) == offer[k] for k in acts), (
        f"the running charger is not wired to the crawler's control {acts}: {mapped}")
    left = [n for n in _mine(_report(entry).get("near_misses"), domain)
            if (n.get("suggested_charger") or {}).get("id")]
    assert not left, "the detection page still offers to create the charger SEM runs"
    coord = entry.runtime_data
    stale = [k for k in (getattr(coord, "_daily_ev_per_charger", {}) or {}) if k != cid]
    intel = getattr(getattr(coord, "data", None), "per_charger_intelligence", None) or {}
    stale += [k for k in intel if k != cid]
    assert not stale, f"per-charger state left for ids SEM does not run: {stale[:4]}"
    return cid


async def _create(hass, charger: Dict[str, Any]) -> None:
    """The card's click (``chargerAddPlan`` + ``set_option``)."""
    from custom_components.solar_energy_management.const import DOMAIN
    await hass.services.async_call(
        DOMAIN, "set_option",
        {"options": {"ev_chargers": [{**charger, "ev_min_current": 6,
                                      "ev_surplus_priority": 3}]}},
        blocking=True)
    await hass.async_block_till_done()


async def _reload(hass, entry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()


async def _replayed(hass, name: str) -> str:
    cap = _case(name)
    await replay(hass, cap)
    _seed(hass)
    return cap["domain"]


@pytest.mark.asyncio
@pytest.mark.parametrize("name", _cases())
async def test_from_the_crawler_to_one_working_charger(sem_real_hass, name):
    hass = sem_real_hass
    domain = await _replayed(hass, name)
    entry = _entry()
    await _start(hass, entry)

    offer = _offer(hass, _report(entry), domain)
    assert offer is not None, (
        f"{name}: the crawler offers no charger — if that is right, name it "
        "in NO_OFFER with the reason")
    gaps = _offer_gaps(hass, offer)
    assert not gaps, f"{name}: the create step would guess: {gaps}"

    await _create(hass, offer)
    cid = _assert_one_working(entry, offer, domain)

    await _reload(hass, entry)
    assert _assert_one_working(entry, offer, domain) == cid, "a restart changed the charger"

    await _create(hass, _offer(hass, _report(entry), domain) or offer)
    await _reload(hass, entry)
    assert _assert_one_working(entry, offer, domain) == cid, "a second click changed the charger"


@pytest.mark.asyncio
@pytest.mark.parametrize("name", _cases())
async def test_an_old_incomplete_card_never_wins(sem_real_hass, name):
    """weindler's upgrade (#1054): an incomplete card for the same box was
    saved first; the working charger came later. After start-up and a
    delete of the old card, the working charger runs — and keeps running."""
    hass = sem_real_hass
    domain = await _replayed(hass, name)
    probe = _entry()
    await _start(hass, probe)
    offer = _offer(hass, _report(probe), domain)
    await hass.config_entries.async_remove(probe.entry_id)
    await hass.async_block_till_done()
    assert offer is not None, f"{name}: the crawler offers no charger"
    assert not _offer_gaps(hass, offer), f"{name}: the offer is incomplete"
    # weindler's old card pointed at an energy reading of the same box the
    # crawler did NOT pick (go-e's ``energy_total_corrected``); use one if
    # the integration has it, else the offer's own
    picked = {v for v in offer.values() if isinstance(v, str)}
    other = sorted(e["entity_id"] for e in _case(name)["entities"]
                   if str(e.get("unit_of_measurement") or "").lower() in ("kwh", "wh")
                   and e["entity_id"] not in picked)
    energy = (other[0] if other else
              offer.get("ev_total_energy_sensor")
              or offer.get("ev_session_energy_sensor")
              or offer["ev_charging_power_sensor"])
    # the id an older SEM minted: the platform and ten hex digits
    old = {"id": f"{domain}_28eb748ac3", "name": "Old card",
           "ev_total_energy_sensor": energy}
    working = {**offer, "ev_min_current": 6, "ev_surplus_priority": 3}

    entry = _entry(chargers=[dict(old), dict(working)], data_chargers=[dict(old)])
    await _start(hass, entry)

    from custom_components.solar_energy_management.const import DOMAIN
    if old["id"] in _running(entry) or any(
            r.get("charger_id") == old["id"]
            for r in (_report(entry).get("chargers") or [])):
        await hass.services.async_call(DOMAIN, "remove_charger",
                                       {"charger_id": old["id"]}, blocking=True)
        await hass.async_block_till_done()
    await _reload(hass, entry)
    cid = _assert_one_working(entry, offer, domain)
    await _reload(hass, entry)
    assert _assert_one_working(entry, offer, domain) == cid


def test_every_capture_is_classified():
    """A capture is a charger case or a named non-charger — never silently
    neither. What the crawler concluded decides which: a charger row or a
    near miss with an offer is a charger."""
    import json as _json
    from pathlib import Path
    golden = Path(__file__).resolve().parent / "integrations_rig" / "crawler"
    for name in capture_names():
        view = _json.loads((golden / f"{name}.json").read_text())
        offers = bool(view.get("chargers")) or any(
            n.get("offer") for n in view.get("near_misses") or [])
        if name in NO_OFFER:
            assert not offers, (
                f"{name} is listed as offering no charger, but the crawler "
                "offers one now — take it off the list")
        else:
            assert offers, (
                f"{name}: the crawler offers no charger; name it in "
                "NO_OFFER with the reason, or fix the crawler")
    assert set(NO_OFFER) <= set(capture_names()), "a stale NO_OFFER row"


@pytest.mark.parametrize("name", [n for n in capture_names() if n not in NO_OFFER])
def test_every_crawler_offer_is_a_complete_charger(name):
    """(#1092) What the crawler offers, SEM must call complete — one answer
    to "is this a charger SEM can run". When the two disagreed (#1054 round
    3 asked for plug and charging sensors), a running Peblar read as a half
    card and its box was offered again."""
    import json as _json
    from pathlib import Path
    from custom_components.solar_energy_management.hardware_detection import (
        charger_is_complete,
    )
    view = _json.loads((Path(__file__).resolve().parent / "integrations_rig"
                        / "crawler" / f"{name}.json").read_text())
    offers = [n["offer"] for n in view.get("near_misses") or [] if n.get("offer")]
    offers += [{k: v for k, v in c["mapped"].items()} for c in view.get("chargers") or []]
    assert offers
    for offer in offers:
        assert charger_is_complete(offer), f"{name}: the crawler's offer reads as half: {offer}"
    assert not charger_is_complete({"id": "x", "ev_total_energy_sensor": "sensor.e"})
