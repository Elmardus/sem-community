"""#1092 — a diagnostics download replays as the same install.

``diag_to_capture`` turns the crawler's evidence in a user's download into a
rig capture. For that to be a test of THEIR install, the download must
carry what the crawler reads. Round trip, on every capture that offers a
charger: replay it, take the detection report the download would carry,
convert it, replay the result beside the original — the crawler must offer
the same charger, wired the same way.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict

import pytest

from .integrations_rig.capture_tools.diag_to_capture import convert
from .integrations_rig.rig import capture_names, crawl, load_capture, replay
from .test_integrations_lifecycle import NO_OFFER

#: Marks the replayed copy. It goes in the MIDDLE of every id — the crawler
#: reads a role from an id's last words, so a suffix would hide it.
MARK = "zzrt_"


def _marked(cap: Dict[str, Any]) -> Dict[str, Any]:
    """The converted capture, renamed so it lives beside the original."""
    text = json.dumps(cap)
    for e in cap["entities"]:
        plat, obj = e["entity_id"].split(".", 1)
        text = text.replace(f'"{e["entity_id"]}"', f'"{plat}.{MARK}{obj}"')
        text = text.replace(f'"{e["unique_id"]}"', f'"{MARK}{e["unique_id"]}"')
    out = json.loads(text)
    out["devices"] = {f"{MARK}{k}": v for k, v in out["devices"].items()}
    for e in out["entities"]:
        if e.get("device"):
            e["device"] = f"{MARK}{e['device']}"
    return out


def _offers(report: Dict[str, Any], domain: str) -> Dict[str, Dict[str, Any]]:
    """Each offered charger's wiring, keyed by whether it came back."""
    out = {}
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
            back = MARK in text
            text = text.replace(MARK, "")
            text = re.sub(r"[0-9a-f]{32}", "<device>", text)
            out["back" if back else "original"] = json.loads(text)
    return out


@pytest.mark.parametrize("name", [n for n in capture_names()
                                  if n not in NO_OFFER])
async def test_a_download_replays_as_the_same_install(hass, name):
    cap = load_capture(name)
    domain = cap["domain"]
    await replay(hass, cap)
    report = crawl(hass)
    converted = convert({"data": {"detection": report}}, domain,
                        pin="round trip")
    await replay(hass, _marked(converted))
    offers = _offers(crawl(hass), domain)
    assert "original" in offers, f"{name}: the original offers nothing"
    assert offers.get("back") == offers["original"], (
        f"{name}: the download did not replay as the same install:\n"
        f" live:     {offers['original']}\n back:     {offers.get('back')}")
