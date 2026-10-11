"""#1092 — a user's diagnostics download replays as the same install, and
holds nothing personal.

``diag_to_capture`` turns the crawler's evidence in a download into a rig
capture. For that to be a test of THEIR install, the real download must
carry what the crawler reads. Round trip, on every capture that offers a
charger: replay it, start SEM, take the REAL diagnostics export, convert
it, replay the result beside the original — the crawler must offer the
same charger, wired the same way.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict

import pytest

from .integrations_rig.capture_tools.diag_to_capture import convert
from .integrations_rig.rig import capture_names, load_capture, replay
from .test_integrations_lifecycle import (  # noqa: F401 — the fixture rides along
    NO_OFFER, _discovery_without_the_wait, _entry, _report, _seed, _start,
)

def _box_names(offer: Dict[str, Any]):
    """The box names an offer carries: the values of box-naming fields in
    its service data (the crawler fills them from the device identifier)."""
    from custom_components.solar_energy_management.hardware_detection import (
        BOX_VALUE_FIELDS,
    )
    out = []
    for k, v in offer.items():
        if k.endswith("_service_data") and isinstance(v, str):
            try:
                data = json.loads(v or "{}")
            except ValueError:
                continue
            out += [str(x) for f, x in data.items() if f in BOX_VALUE_FIELDS and x]
    return out


def _offers(report: Dict[str, Any], domain: str):
    """Each offered charger's wiring for ``domain``; box names (the
    original's) and their stand-ins (the copy's) both read as ``<box>``."""
    out = []
    for key in ("near_misses", "chargers"):
        for r in report.get(key) or []:
            if str(r.get("platform") or "").split("_rig")[0] != domain:
                continue
            offer = dict(r.get("suggested_charger") or {})
            if not offer and key == "chargers":
                offer = {k: (v.get("entity") or v.get("value"))
                         for k, v in (r.get("mapped") or {}).items()
                         if isinstance(v, dict)}
            offer.pop("id", None)
            offer.pop("name", None)
            if not offer:
                continue
            text = json.dumps(offer, sort_keys=True)
            for b in sorted(_box_names(offer), key=len, reverse=True):
                slug = re.sub(r"[^a-z0-9]+", "_", b.lower()).strip("_")
                text = re.sub(rf"(?<![a-z0-9]){re.escape(slug)}(?![a-z0-9])",
                              "<box>", text, flags=re.I)
            text = re.sub(r"box_\d+", "<box>", text)
            text = re.sub(r"[0-9a-f]{32}", "<device>", text)
            out.append(json.loads(text))
    return sorted(out, key=lambda o: json.dumps(o, sort_keys=True))


@pytest.mark.asyncio
@pytest.mark.parametrize("name", [n for n in capture_names() if n not in NO_OFFER])
async def test_a_download_replays_as_the_same_install(sem_real_hass, name):
    from custom_components.solar_energy_management.diagnostics import (
        async_get_config_entry_diagnostics,
    )
    hass = sem_real_hass
    cap = load_capture(name)
    domain = cap["domain"]
    await replay(hass, cap)
    _seed(hass)
    entry = _entry()
    await _start(hass, entry)
    report = _report(entry)
    live = _offers(report, domain)
    assert live, f"{name}: the crawler offers nothing live"
    names = [b for r in (report.get("near_misses") or []) + (report.get("chargers") or [])
             for b in _box_names(r.get("suggested_charger") or {})]
    await entry.runtime_data.async_refresh()        # the download reads data
    await hass.async_block_till_done()
    download = await async_get_config_entry_diagnostics(hass, entry)
    download = json.loads(json.dumps(download, default=str))  # as a file holds it
    det = download.get("detection") or {}
    assert domain in (det.get("services") or {}), (
        f"{name}: the download does not list the integration's services")
    text = json.dumps(det)
    assert not _personal_values(text), f"{name}: the download names a box"
    for b in names:
        slug = re.sub(r"[^a-z0-9]+", "_", b.lower()).strip("_")
        assert not re.search(rf"(?<![a-z0-9]){re.escape(slug)}(?![a-z0-9])",
                             text, flags=re.I), (
            f"{name}: the download still carries the box name '{b}'")
    converted = convert(download, domain, pin="round trip")

    # the install goes; its download comes back in its place
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er
    ereg, dreg = er.async_get(hass), dr.async_get(hass)
    for other in hass.config_entries.async_entries(domain):
        for e in er.async_entries_for_config_entry(ereg, other.entry_id):
            hass.states.async_remove(e.entity_id)
            ereg.async_remove(e.entity_id)
        for d in dr.async_entries_for_config_entry(dreg, other.entry_id):
            dreg.async_remove_device(d.id)
    await hass.async_block_till_done()
    await replay(hass, converted)
    back = _offers(_report(entry), domain)
    assert back == live, (
        f"{name}: the download did not replay as the same install:\n"
        f" live: {live}\n back: {back}")


#: Where a person's box name sits in a download: the values of these keys.
_PERSONAL = re.compile(r'"(charger_name|charger|device_name|charger_id|serial'
                       r'|serial_number)"\s*:\s*"([^"]+)"')


def _personal_values(text: str):
    vals = set(_PERSONAL.findall(text))
    for blob in re.findall(r'"ev_[a-z_]*service_data"\s*:\s*"((?:[^"\\]|\\.)*)"', text):
        vals |= set(_PERSONAL.findall(blob.replace('\\"', '"')))
    return {v for _, v in vals if not re.fullmatch(r"box_\d+", v)}


def test_no_personal_value_survives_into_a_capture():
    """weindler's own download (#1054, before the redaction) named his box;
    the capture made from it must not — not in service data, not as the
    device, not inside an entity id."""
    src = Path(__file__).resolve().parent / "integrations_rig" / "captures"
    for path in sorted(src.glob("*.json")):
        cap = json.loads(path.read_text())
        if cap["source"]["kind"] != "live-install" or \
                "diagnostics" not in str(cap["source"].get("repo")):
            continue
        text = path.read_text()
        assert not _personal_values(text), (
            f"{path.name} names a box: {_personal_values(text)}")
        for dev in cap.get("devices") or {}:
            assert re.fullmatch(r"box_\d+|[0-9a-f]{32}|device_\d+", dev), (
                f"{path.name}: the device key '{dev}' is not a stand-in")


def test_the_converter_replaces_every_box_name():
    """A download that names its box (an old one) comes out with stand-ins
    only — the name, its slug inside entity ids, the device key."""
    diag = {"data": {"detection": {"near_misses": [{
        "platform": "shape_box", "device_id": "d" * 32,
        "entities": [{"entity": "sensor.shape_box_my_garage_box_power",
                      "domain": "sensor", "device_class": "power",
                      "unit": "W"}],
        "proposed_roles": {"ev_current_control": {
            "service": "shape_box.set_current", "fields": ["charger_name", "current"],
            "data": {"charger_name": "My_Garage_Box"}}},
        "suggested_charger": {
            "ev_charger_service": "shape_box.set_current",
            "ev_charger_service_data": json.dumps({"charger_name": "My_Garage_Box"})},
    }]}}}
    text = json.dumps(convert(diag, "shape_box", pin="unit"))
    assert "garage" not in text.lower(), text
    assert "box_1" in text
