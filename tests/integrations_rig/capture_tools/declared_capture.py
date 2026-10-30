"""A DECLARED capture: what an integration creates, read from its own entity
description tables at a pinned commit, for integrations that cannot be run
offline (Zaptec: every test of its own needs a cloud account).

This is weaker than a live load and the capture says so
(``source.kind = "declared"``): keys, translation keys, device classes and
units are the integration's own words, but which entities a given account
gets, and the entity ids, are built the way the integration builds them
(``has_entity_name``: ``<platform>.<device name>_<key>``), not observed.

    python declared_capture.py <checkout>/custom_components/<domain> \\
        --domain zaptec --out zaptec.json \\
        --device INSTALLATION_ENTITIES=installation:Zaptec Installation:inst1 \\
        --device CHARGER_ENTITIES=charger:Zaptec Go2:chg1 \\
        [--drop available_current]
"""
from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from dump import write_capture  # noqa: E402

PLATFORMS = ("number", "switch", "sensor", "binary_sensor", "select",
             "button", "time")


def _resolve(node: ast.AST) -> Any:
    """A literal, or a Home Assistant constant like
    ``UnitOfElectricCurrent.AMPERE`` / ``NumberDeviceClass.CURRENT``."""
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Attribute):
        owner = node.value
        while isinstance(owner, ast.Attribute) and owner.attr == "const":
            owner = owner.value
        owner_name = owner.attr if isinstance(owner, ast.Attribute) else getattr(owner, "id", "")
        try:
            import homeassistant.const as hc
            from homeassistant.components import (binary_sensor, button,
                                                  number, sensor, switch)
            for mod in (hc, number, sensor, binary_sensor, switch, button):
                cls = getattr(mod, owner_name, None)
                if cls is not None and hasattr(cls, node.attr):
                    val = getattr(cls, node.attr)
                    return getattr(val, "value", val)
            val = getattr(hc, node.attr, None)
            if isinstance(val, str):
                return val
        except Exception:  # noqa: BLE001
            pass
        return node.attr.lower()
    return None


def descriptions(path: Path) -> Dict[str, List[Dict[str, Any]]]:
    """``{LIST_NAME: [description kwargs, …]}`` for one platform file."""
    tree = ast.parse(path.read_text())
    out: Dict[str, List[Dict[str, Any]]] = {}
    for node in tree.body:
        target = None
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target, value = node.target.id, node.value
        elif isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            target, value = node.targets[0].id, node.value
        if not target or not isinstance(value, (ast.List, ast.Tuple)):
            continue
        rows = []
        for call in value.elts:
            if not isinstance(call, ast.Call):
                continue
            kw = {k.arg: _resolve(k.value) for k in call.keywords if k.arg}
            if "key" in kw:
                rows.append(kw)
        if rows:
            out[target] = rows
    return out


def dict_tables(path: Path, names: List[str]) -> List[Dict[str, Any]]:
    """Entities declared as ``{name: {"type": …, "key": …, …}}`` tables
    (Easee's ``const.py`` shape). Rows without a ``type`` are sensors."""
    tree = ast.parse(path.read_text())
    rows: List[Dict[str, Any]] = []
    for node in tree.body:
        if not (isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id in names and isinstance(node.value, ast.Dict)):
            continue
        for k, v in zip(node.value.keys, node.value.values, strict=True):
            if not isinstance(k, ast.Constant) or not isinstance(v, ast.Dict):
                continue
            body = {kk.value: _resolve(vv) for kk, vv in zip(v.keys, v.values, strict=True)
                    if isinstance(kk, ast.Constant)}
            rows.append({
                # Easee names its equalizer platforms ``eq_<platform>``
                "platform": str(body.get("type") or "sensor").removeprefix("eq_"),
                "key": k.value,
                "translation_key": body.get("translation_key"),
                "device_class": body.get("device_class"),
                "native_unit_of_measurement": body.get("units"),
                "entity_registry_enabled_default": body.get("enabled_default"),
                "entity_category": body.get("entity_category"),
            })
    return rows


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def build(src: Path, domain: str, devices: List[str], drop: List[str],
          repo: str, table_specs: List[str] = ()) -> Dict[str, Any]:
    dev_specs = {}
    for spec in devices:
        lst, rest = spec.split("=", 1)
        kind, name, obj_id = rest.split(":")
        dev_specs[lst] = (kind, name, obj_id)
    entities = []
    devs: Dict[str, Dict[str, Any]] = {}
    for platform in PLATFORMS:
        f = src / f"{platform}.py"
        if not f.exists():
            continue
        for lst, rows in descriptions(f).items():
            if lst not in dev_specs:
                continue
            kind, name, obj_id = dev_specs[lst]
            dkey = f"{domain}:{obj_id}"
            devs.setdefault(dkey, {"name": name, "model": kind,
                                   "manufacturer": None})
            for kw in rows:
                if kw["key"] in drop:
                    continue
                unit = kw.get("native_unit_of_measurement")
                dc = kw.get("device_class")
                attrs = {}
                if unit:
                    attrs["unit_of_measurement"] = unit
                if dc:
                    attrs["device_class"] = dc
                caps = {}
                for a, b in (("native_min_value", "min"), ("native_max_value", "max"),
                             ("native_step", "step")):
                    if kw.get(a) is not None:
                        caps[b] = kw[a]
                if kw.get("options"):
                    caps["options"] = kw["options"]
                attrs.update(caps)
                entities.append({
                    "entity_id": f"{platform}.{_slug(name)}_{_slug(kw['key'])}",
                    "unique_id": f"{obj_id}_{kw['key']}",
                    "translation_key": kw.get("translation_key"),
                    "original_device_class": dc,
                    "unit_of_measurement": unit,
                    "capabilities": caps,
                    "entity_category": kw.get("entity_category"),
                    "disabled_by": ("integration"
                                    if kw.get("entity_registry_enabled_default") is False
                                    else None),
                    "device": dkey,
                    "state": None,
                    "attributes": attrs,
                })
    for spec in table_specs:
        file_name, names, kind, name, obj_id = spec.split(":")
        dkey = f"{domain}:{obj_id}"
        devs.setdefault(dkey, {"name": name, "model": kind, "manufacturer": None})
        for kw in dict_tables(src / file_name, names.split(",")):
            if kw["key"] in drop:
                continue
            unit, dc = kw.get("native_unit_of_measurement"), kw.get("device_class")
            attrs = {}
            if unit:
                attrs["unit_of_measurement"] = unit
            if dc:
                attrs["device_class"] = dc
            entities.append({
                "entity_id": f"{kw['platform']}.{_slug(name)}_{_slug(kw['key'])}",
                "unique_id": f"{obj_id}_{kw['key']}",
                "translation_key": kw.get("translation_key"),
                "original_device_class": dc, "unit_of_measurement": unit,
                "capabilities": {}, "entity_category": kw.get("entity_category"),
                "disabled_by": ("integration"
                                if kw.get("entity_registry_enabled_default") is False
                                else None),
                "device": dkey, "state": None, "attributes": attrs,
            })
    services = {}
    sy = src / "services.yaml"
    if sy.exists():
        import yaml
        doc = yaml.safe_load(sy.read_text()) or {}
        services = {n: sorted(((b or {}).get("fields") or {}))
                    for n, b in sorted(doc.items())}
    commit = subprocess.run(["git", "-C", str(src), "rev-parse", "HEAD"],
                            capture_output=True, text=True).stdout.strip()
    entities.sort(key=lambda r: r["entity_id"])
    return {"domain": domain,
            "source": {"kind": "declared", "repo": repo, "commit": commit,
                       "dropped": drop,
                       "note": "read from the integration's entity "
                               "descriptions; entity ids built the way it "
                               "builds them; no live load (cloud-only)"},
            "devices": devs, "entities": entities, "services": services}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("src", type=Path)
    ap.add_argument("--domain", required=True)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--device", action="append", default=[])
    ap.add_argument("--drop", action="append", default=[])
    ap.add_argument("--tables", action="append", default=[],
                    help="file.py:TABLE1,TABLE2:kind:device name:object id")
    a = ap.parse_args()
    cap = build(a.src, a.domain, a.device, a.drop, a.repo, a.tables)
    out = HERE.parent / "captures" / a.out
    write_capture(cap, out)
    print(f"{a.out}: {len(cap['entities'])} entities, {len(cap['devices'])} devices")


if __name__ == "__main__":
    main()
