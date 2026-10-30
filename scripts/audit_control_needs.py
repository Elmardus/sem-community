#!/usr/bin/env python3
"""(#996) Which SEM entities would an install NOT get — from its options.

    python3 scripts/audit_control_needs.py <options.json> \
        [--solar-forecast yes|no|unknown] [--export-limit yes|no|unknown] \
        [--ed-battery yes|no|unknown] [--ed-ev yes|no|unknown]

The JSON is a config entry's ``data`` and ``options`` merged (the shape
``tests/fixtures/996_*_options.json`` holds). The four flags are the
answers SEM finds at runtime — a forecast integration and an export-limit
entity in the entity registry, a battery and an EV in the Energy
Dashboard; "unknown" (the default) keeps every row, as SEM does.

Pure stdlib: loads ``coordinator/install_modules.py`` by path, so it runs
on a machine without Home Assistant (the validate-sem.sh pattern).
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path


def _load_oracle():
    path = Path(__file__).resolve().parents[1] / "coordinator" / "install_modules.py"
    spec = importlib.util.spec_from_file_location("install_modules", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _tri(value: str):
    return {"yes": True, "no": False}.get(value)


def audit(options: dict, *, solar_forecast=None, export_limit=None,
          ed_battery=None, ed_ev=None) -> dict:
    oracle = _load_oracle()
    ed = None
    answered = False
    if ed_battery is not None or ed_ev is not None:
        ed = {"has_battery": bool(ed_battery), "has_ev": bool(ed_ev)}
        answered = True
    presence = oracle.module_verdict(
        options, ed, answered,
        runtime={"solar_forecast": solar_forecast, "export_limit": export_limit})
    gone = sorted(oracle.absent_entity_ids(presence))
    return {
        "verdict": oracle.presence_summary(presence),
        "dropped": gone,
        "dropped_controls": [e for e in gone if not e.startswith(("sensor.", "binary_sensor."))],
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("options", type=Path)
    for flag in ("solar-forecast", "export-limit", "ed-battery", "ed-ev"):
        parser.add_argument(f"--{flag}", choices=("yes", "no", "unknown"), default="unknown")
    args = parser.parse_args(argv)
    options = json.loads(args.options.read_text(encoding="utf-8"))
    result = audit(
        options,
        solar_forecast=_tri(args.solar_forecast), export_limit=_tri(args.export_limit),
        ed_battery=_tri(args.ed_battery), ed_ev=_tri(args.ed_ev))
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
