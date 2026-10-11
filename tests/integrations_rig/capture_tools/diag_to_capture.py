"""#1092 — a user's SEM diagnostics download, as a rig capture.

The crawler runs on every install; its evidence lands in the diagnostics
download (``detection``: chargers, near misses, meters, with each entity as
the crawler read it, and — from 2.2.0-beta.16 — each integration's services
with their fields). This tool turns one integration's part of that download
into ``captures/<name>.json``, so the user's own install becomes a test
input: real integrations reach the tests from real installs, never by hand.

    python3 -m tests.integrations_rig.capture_tools.diag_to_capture \\
        <diagnostics.json> <platform> <capture name> [--pin "SEM 2.2.0-beta.15 diagnostics DD.MM.YYYY"]

What a download does not hold, the capture does not invent: only short
states (a status word, a number — never a place), no unique ids (they
often hold a serial — the object id stands in). A download from before 2.2.0-beta.16 also has
no units, capabilities or service list; the capture says so in its note,
and the crawler's golden file shows what it can find from that alone.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

CAPTURES = Path(__file__).resolve().parent.parent / "captures"

#: Service fields that name WHICH box — their value is the device's name in
#: the integration's own words, so it becomes the device key the replay
#: registers (the crawler reads it back from the device identifier).
_BOX_FIELDS = ("charger_name", "charger_id", "serial", "charger", "device_name")


def _detection(diag: Dict[str, Any]) -> Dict[str, Any]:
    data = diag.get("data", diag)
    det = data.get("detection") or data.get("detection_report") or {}
    if not det:
        raise SystemExit("no 'detection' section in this download")
    return det


def _rows(det: Dict[str, Any], platform: str) -> List[Dict[str, Any]]:
    out = []
    for key in ("chargers", "near_misses", "meters"):
        for r in det.get(key) or []:
            if str(r.get("platform") or "").split("_rig")[0] == platform:
                out.append(r)
    return out


def _box_name(rows: List[Dict[str, Any]]) -> Optional[str]:
    for r in rows:
        for prop in (r.get("proposed_roles") or {}).values():
            for f, v in (prop.get("data") or {}).items():
                if f in _BOX_FIELDS and v:
                    return str(v)
        sc = r.get("suggested_charger") or {}
        try:
            data = json.loads(sc.get("ev_charger_service_data") or "{}")
        except ValueError:
            data = {}
        for f, v in data.items():
            if f in _BOX_FIELDS and v:
                return str(v)
    return None


def _services(det: Dict[str, Any], rows: List[Dict[str, Any]],
              diag: Dict[str, Any], platform: str) -> Dict[str, List[str]]:
    listed = (det.get("services") or {}).get(platform)
    if listed:
        return {k: sorted(v or []) for k, v in listed.items()}
    # an older download: what the rows and the charger adapters name
    out: Dict[str, List[str]] = {}
    for r in rows:
        for prop in (r.get("proposed_roles") or {}).values():
            svc = str(prop.get("service") or "")
            if svc.startswith(f"{platform}."):
                out[svc.split(".", 1)[1]] = sorted(prop.get("fields") or [])
        sc = r.get("suggested_charger") or {}
        svc = str(sc.get("ev_charger_service") or "")
        if svc.startswith(f"{platform}.") and svc.split(".", 1)[1] not in out:
            fields = {sc.get("ev_service_param_name")}
            try:
                fields |= set(json.loads(sc.get("ev_charger_service_data") or "{}"))
            except ValueError:
                pass
            out[svc.split(".", 1)[1]] = sorted(f for f in fields if f)
    data = diag.get("data", diag)
    for adapter in (data.get("charger_adapters") or {}).values():
        svc = str(adapter.get("charger_service") or "")
        if svc.startswith(f"{platform}.") and svc.split(".", 1)[1] not in out:
            out[svc.split(".", 1)[1]] = []
    return out


def convert(diag: Dict[str, Any], platform: str, *, pin: str) -> Dict[str, Any]:
    det = _detection(diag)
    rows = _rows(det, platform)
    if not rows:
        raise SystemExit(f"the download has no crawler row for '{platform}'")
    box = _box_name(rows)
    devices: Dict[str, Dict[str, Any]] = {}
    entities: List[Dict[str, Any]] = []
    seen = set()
    rich = False
    # a charger's companions (the site device it takes its current from)
    # are devices of their own, listed under the charger row
    units = []
    for i, r in enumerate(rows):
        units.append((box or str(r.get("device_id") or f"device_{i}"), r))
        for c in r.get("companions") or []:
            if isinstance(c, dict) and c.get("evidence"):
                units.append((str(c.get("device_id")),
                              {"device_id": c.get("device_id"),
                               "entities": c["evidence"]}))
    for key, r in units:
        if r.get("device_id") or box:
            devices.setdefault(key, {"name": key, "manufacturer": None,
                                     "model": None})
        ents = r.get("entities") or []
        if isinstance(ents, (int, set)):
            ents = []
        # a charger row lists its entities as the mapped roles + the rest
        ents = [*ents,
                *[v for v in (r.get("mapped") or {}).values()
                  if isinstance(v, dict) and v.get("entity")],
                *(r.get("unmapped") or [])]
        for e in ents:
            eid = str(e.get("entity") or "")
            if not eid or eid in seen:
                continue
            seen.add(eid)
            rich = rich or any(k in e for k in ("unit", "translation_key",
                                                "capabilities"))
            caps = dict(e.get("capabilities") or {})
            attrs: Dict[str, Any] = {}
            if e.get("unit"):
                attrs["unit_of_measurement"] = e["unit"]
            if e.get("device_class"):
                attrs["device_class"] = e["device_class"]
            if caps.get("options"):
                attrs["options"] = caps["options"]
            object_id = eid.split(".", 1)[1]
            entities.append({
                "entity_id": eid,
                "unique_id": object_id,
                "device": key if key in devices else None,
                "state": e.get("state") or "unknown",
                "attributes": attrs,
                "original_device_class": e.get("device_class") or None,
                "unit_of_measurement": e.get("unit"),
                "translation_key": e.get("translation_key"),
                "capabilities": caps,
                "disabled_by": "user" if e.get("disabled") else None,
                "entity_category": e.get("entity_category"),
            })
    services = _services(det, rows, diag, platform)
    note = ("from a user's SEM diagnostics download: the crawler's own "
            "evidence. Short states only, no unique ids (the object id "
            "stands in).")
    if not rich:
        note += (" This download predates 2.2.0-beta.16: no units, "
                 "capabilities or service list — the crawler sees less here "
                 "than it saw live.")
    return {
        "domain": platform,
        "devices": devices,
        "entities": sorted(entities, key=lambda x: x["entity_id"]),
        "services": services,
        # the fields came from the download, not from a registered schema
        "services_without_schema": True,
        "source": {"kind": "live-install", "repo": "user diagnostics (SEM)",
                   "tag": pin, "note": note, "dropped": []},
    }


def main(argv: Optional[List[str]] = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("diagnostics")
    ap.add_argument("platform")
    ap.add_argument("name")
    ap.add_argument("--pin", required=True,
                    help='e.g. "SEM 2.2.0-beta.13 diagnostics 08.10.2026"')
    a = ap.parse_args(argv)
    diag = json.loads(Path(a.diagnostics).read_text())
    cap = convert(diag, a.platform, pin=a.pin)
    out = CAPTURES / f"{a.name}.json"
    out.write_text(json.dumps(cap, indent=1, sort_keys=True) + "\n")
    print(f"wrote {out} ({len(cap['entities'])} entities, "
          f"{len(cap['services'])} services)")


if __name__ == "__main__":
    main()
