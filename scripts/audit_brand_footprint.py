#!/usr/bin/env python3
"""#855 — count brand knowledge sitting in the generic device layer.

``devices/base.py`` is supposed to know about DEVICES; the adapters in
``coordinator/charger_adapters/`` exist to hold brand quirks. Today the
generic layer carries the actual mechanics for a dozen brands, which is
why a one-idea fix (#854, "a stop must not enable the charger") had to be
made in two layers, and why observer mode — which cuts above the adapter —
cannot see the commands SEM would really send.

This is the measuring stick for the move. It counts, it does not judge:
the ratchet test decides whether a number is allowed to have changed.

    python3 scripts/audit_brand_footprint.py             # report
    python3 scripts/audit_brand_footprint.py --baseline  # rewrite the baseline
"""
from __future__ import annotations

import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
TARGET = ROOT / "devices" / "base.py"
BASELINE = ROOT / "tests" / "brand_footprint_baseline.json"

# Every brand SEM speaks to. Kept explicit rather than inferred: a new
# brand must be ADDED here consciously, which is itself the moment to ask
# whether its code belongs in the generic layer at all.
BRANDS = (
    "keba", "wallbox", "zaptec", "easee", "go-e", "wattpilot", "heidelberg",
    "openwb", "ocpp", "ohme", "peblar", "v2c", "alfen", "openevse",
    "chargepoint", "juicebox", "garo", "tesla", "deye", "huawei", "fronius",
    "solaredge", "sma", "solax", "goodwe", "sungrow", "growatt", "enphase",
    "powerwall", "kostal", "sonnen", "victron", "marstek", "nibe", "buderus",
    # tariff and forecast sources: found by role, not by name (#855 layers)
    "tibber", "amber", "octopus", "nordpool", "entsoe", "energyzero",
    "awattar", "zonneplan", "solcast",
)

# (#855, layers) Where brand knowledge MAY live: the per-brand charger and
# battery adapters and the crawler (its roster and its vocabulary). Everything else is a generic
# layer and may only lose brand mentions. ``tests``, ``scripts`` and
# ``tools`` hold fixtures, not runtime code.
ALLOWED = ("coordinator/charger_adapters/", "coordinator/battery_adapters/",
           "hardware_detection.py", "consts/", "tests/", "scripts/", "tools/")
LAYERS_BASELINE = ROOT / "tests" / "brand_footprint_layers_baseline.json"


def count_layers(root: pathlib.Path = ROOT) -> dict[str, int]:
    """Brand mentions per generic-layer file (files with none left out)."""
    out: dict[str, int] = {}
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        if rel.startswith(ALLOWED) or "/node_modules/" in rel or rel.startswith("."):
            continue
        n = sum(count_brands(path.read_text(encoding="utf-8")).values())
        if n:
            out[rel] = n
    return out


def count_brands(text: str) -> dict[str, int]:
    """Brand mentions, comments and docstrings included — deliberately.

    A comment explaining KEBA's firmware in the generic layer is brand
    knowledge living in the wrong file just as much as the code it
    explains; when the mechanics move, the explanation moves with them.
    """
    out: dict[str, int] = {}
    for brand in BRANDS:
        hits = re.findall(
            rf"(?<![a-z0-9_]){re.escape(brand)}(?![a-z0-9])", text, re.I)
        if hits:
            out[brand] = len(hits)
    return dict(sorted(out.items()))


def main() -> int:
    if "--layers-baseline" in sys.argv:
        layers = count_layers()
        LAYERS_BASELINE.write_text(json.dumps(
            {"_comment": "#855 layers: brand mentions per generic file; may "
                         "only shrink. Regenerate: scripts/audit_brand_"
                         "footprint.py --layers-baseline",
             "total": sum(layers.values()), "per_file": layers},
            indent=2) + "\n")
        print(f"layers baseline: {sum(layers.values())} mentions in "
              f"{len(layers)} files")
        return 0
    counts = count_brands(TARGET.read_text())
    total = sum(counts.values())
    if "--baseline" in sys.argv:
        old = json.loads(BASELINE.read_text())
        old["total"] = total
        old["per_brand"] = counts
        BASELINE.write_text(json.dumps(old, indent=2) + "\n")
        print(f"baseline rewritten: {total} brand mentions in {TARGET.name}")
        return 0
    print(f"{total} brand mentions in {TARGET.relative_to(ROOT)}")
    for brand, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {brand:14} {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
