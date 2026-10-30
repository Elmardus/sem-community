"""Turn Home Assistant core's own entity snapshots into a rig capture.

Core tests every integration against mocked API data and freezes what the
integration created in ``tests/components/<domain>/snapshots/*.ambr``
(syrupy). That IS the real integration's output at a Home Assistant tag —
no SEM-written fixture in between. This tool downloads those files at the
pinned tag and keeps what the crawler reads.

    python3 snapshot_to_capture.py <domain> [--tag 2026.8.2] [--device-split -]

The snapshot does not record the device. ``--device-split`` names the
character the integration puts between its device serial and the key in
``unique_id`` (Tesla: ``VIN-key``); without it, every entity is one device.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from dump import write_capture  # noqa: E402

PLATFORMS = ("number", "switch", "sensor", "binary_sensor", "select",
             "button", "time")

_STR = r"'((?:[^'\\]|\\.)*)'"


def _field(block: str, name: str) -> Optional[str]:
    m = re.search(rf"^\s*'{name}': (?:{_STR}|None|<[^:>]+: {_STR}>)", block, re.M)
    if not m:
        return None
    return m.group(1) if m.group(1) is not None else m.group(2)


def _capabilities(block: str) -> Dict[str, Any]:
    m = re.search(r"'capabilities': dict\(\{(.*?)^\s*\}\),", block, re.M | re.S)
    if not m:
        return {}
    body = m.group(1)
    caps: Dict[str, Any] = {}
    for key in ("max", "min", "step"):
        km = re.search(rf"'{key}'>?: ([-0-9.e]+)", body)
        if km:
            caps[key] = float(km.group(1))
    om = re.search(r"'options'>?: list\(\[(.*?)\]\)", body, re.S)
    if om:
        caps["options"] = re.findall(_STR, om.group(1))
    return caps


def parse_ambr(text: str) -> List[Dict[str, Any]]:
    """Entity rows from one ``.ambr`` file: ``-entry`` blocks joined with
    their ``-state`` block."""
    rows: Dict[str, Dict[str, Any]] = {}
    for m in re.finditer(r"^# name: [^\n]*\[([^\[\]]+)-(entry|state)\]\n(.*?)^# ---",
                         text, re.M | re.S):
        eid, kind, block = m.group(1), m.group(2), m.group(3)
        row = rows.setdefault(eid, {"entity_id": eid})
        if kind == "entry":
            row.update({
                "platform": _field(block, "platform"),
                "unique_id": _field(block, "unique_id"),
                "translation_key": _field(block, "translation_key"),
                "original_device_class": _field(block, "original_device_class"),
                "unit_of_measurement": _field(block, "unit_of_measurement"),
                "entity_category": _field(block, "entity_category"),
                "capabilities": _capabilities(block),
                "disabled_by": None,
            })
        else:
            st = re.search(rf"^\s*'state': {_STR}", block, re.M)
            row["state"] = st.group(1) if st else None
    return [r for r in rows.values() if r.get("platform")]


def _fetch(domain: str, platform: str, tag: str) -> Optional[str]:
    path = f"tests/components/{domain}/snapshots/test_{platform}.ambr"
    proc = subprocess.run(
        ["gh", "api", f"repos/home-assistant/core/contents/{path}?ref={tag}",
         "-H", "Accept: application/vnd.github.raw"],
        capture_output=True, text=True)
    if proc.returncode != 0 or proc.stdout.startswith('{"message"'):
        return None
    return proc.stdout


def _core_services(domain: str) -> Dict[str, list]:
    """The integration's services and their fields, from the
    ``services.yaml`` of the installed Home Assistant (run this tool with the
    test venv's python, so the version is the pinned one)."""
    import importlib.util
    spec = importlib.util.find_spec("homeassistant")
    if spec is None or not spec.submodule_search_locations:
        return {}
    path = Path(list(spec.submodule_search_locations)[0]) / "components" / domain / "services.yaml"
    if not path.exists():
        return {}
    import yaml
    doc = yaml.safe_load(path.read_text()) or {}
    return {name: sorted(((body or {}).get("fields") or {}))
            for name, body in sorted(doc.items())}


def build(domain: str, tag: str, split: Optional[str]) -> Dict[str, Any]:
    entities: List[Dict[str, Any]] = []
    files = []
    for platform in PLATFORMS:
        text = _fetch(domain, platform, tag)
        if text is None:
            continue
        files.append(f"test_{platform}.ambr")
        entities.extend(parse_ambr(text))
    devices: Dict[str, Dict[str, Any]] = {}
    for e in entities:
        uid = str(e.get("unique_id") or "")
        key = uid.split(split, 1)[0] if (split and split in uid) else domain
        e["device"] = f"{domain}:{key}"
        devices.setdefault(e["device"], {"name": key, "model": None,
                                         "manufacturer": None})
        attrs: Dict[str, Any] = {}
        if e.get("unit_of_measurement"):
            attrs["unit_of_measurement"] = e["unit_of_measurement"]
        if e.get("original_device_class"):
            attrs["device_class"] = e["original_device_class"]
        attrs.update({k: v for k, v in e["capabilities"].items()})
        e["attributes"] = attrs
    entities.sort(key=lambda r: r["entity_id"])
    return {
        "domain": domain,
        "source": {
            "kind": "core-snapshot",
            "repo": "home-assistant/core",
            "tag": tag,
            "files": [f"tests/components/{domain}/snapshots/{f}" for f in files],
            "note": "Home Assistant's own test output for this integration "
                    "at the tag; the device grouping comes from unique_id",
        },
        "devices": devices,
        "entities": entities,
        "services": _core_services(domain),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("domain")
    ap.add_argument("--tag", default="2026.8.2")
    ap.add_argument("--device-split", default=None)
    args = ap.parse_args()
    cap = build(args.domain, args.tag, args.device_split)
    out = HERE.parent / "captures" / f"{args.domain}.json"
    write_capture(cap, out)
    print(f"{args.domain}: {len(cap['entities'])} entities, "
          f"{len(cap['devices'])} devices -> {out}")


if __name__ == "__main__":
    main()
