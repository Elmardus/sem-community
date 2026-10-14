"""#1032 — SEM connects to integrations; it does not cover hardware.

01.10.2026: "this is the whole idea of SEM — we do not cover hardware
integration, we connect to integrations with the crawler if possible."

A new device is a ROLE the crawler learns (consts/role_lexicon.py,
charger_roles.py), proven on the real integration in tests/integrations_rig.
These guards keep it that way:

* ``hardware_detection.py`` may not grow a new brand-named ``_wire_<x>`` /
  ``_discover_<x>`` function. The ones listed below exist today and are
  cleanup candidates (each folds into a role on its own branch, proven on
  the rig first) — the list may only SHRINK.
* the roster's role reader (the functions below, in hardware_detection.py)
  names no integration at all.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: 01.10.2026. Shrinks only: remove a name when its path folds into a role.
BRAND_PATHS_TODAY = frozenset({
    "_discover_abl_emh1", "_discover_alfen", "_discover_blue_current",
    "_discover_chargepoint", "_discover_easee", "_discover_garo",
    "_discover_goecharger", "_discover_goecharger_mqtt",
    "_discover_heidelberg", "_discover_juicebox", "_discover_keba",
    "_discover_mqtt_brands", "_discover_ocpp",
    "_discover_ohme", "_discover_openwb",
    "_discover_peblar", "_discover_v2c", "_discover_wallbox",
    "_discover_wallbox_mqtt", "_discover_wattpilot", "_discover_zaptec",
    "_wire_wattpilot",
})
#: generic, not a brand: the shared hint walker every brand row feeds, and
#: #1035's wrapper that runs every brand function on one unit
GENERIC = frozenset({"_discover_from_hints", "_discover_unit"})


def _functions(path: Path) -> set:
    tree = ast.parse(path.read_text())
    return {n.name for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def test_no_new_brand_path_in_hardware_detection():
    names = {n for n in _functions(ROOT / "hardware_detection.py")
             if re.match(r"^_(wire|discover)_", n)} - GENERIC
    new = sorted(names - BRAND_PATHS_TODAY)
    assert not new, (
        f"new brand path(s) {new}: teach the crawler the ROLE instead "
        "(consts/role_lexicon.py + read_charger_roles in hardware_detection.py) "
        "and prove it on the real integration in tests/integrations_rig")


def test_the_cleanup_list_only_shrinks():
    names = _functions(ROOT / "hardware_detection.py")
    gone = sorted(BRAND_PATHS_TODAY - names)
    assert not gone, (
        f"{gone} folded into a role — remove them from BRAND_PATHS_TODAY")


def _integration_domains() -> set:
    from custom_components.solar_energy_management.consts import (
        integration_roster as roster,
    )
    from custom_components.solar_energy_management.hardware_detection import (
        _EV_CHARGER_PLATFORMS,
    )
    domains = set(getattr(roster, "ROSTER", {}) or {})
    domains |= {p for p, _ in _EV_CHARGER_PLATFORMS}
    domains |= {"tesla_fleet", "teslemetry", "tessie", "tesla_wall_connector",
                "myenergi", "zaptec", "easee", "eg4_web_monitor", "lxp_modbus"}
    return domains


ROLE_READER = ("_role_words", "_rule_hits", "_first_hit", "_speaks_vehicle",
               "_select_options", "_pick_option", "_charging_power",
               "_plugged", "_charging_now", "_current_number_role",
               "_is_stored_setting", "_service_current_role",
               "_service_field_roles", "read_charger_roles", "_roles_offer",
               "_offer_missing", "_roles_pass")


def test_the_role_reader_names_no_integration():
    tree = ast.parse((ROOT / "hardware_detection.py").read_text())
    fns = {n.name: n for n in ast.walk(tree)
           if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    missing = [f for f in ROLE_READER if f not in fns]
    assert not missing, missing
    literals = {n.value for f in ROLE_READER for n in ast.walk(fns[f])
                if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    named = sorted(literals & _integration_domains())
    assert not named, f"the role reader names integrations: {named}"


#: (#1032 review) Every place the crawler compares a platform with an
#: integration's name, today. Shrinks only: a pair leaves when its brand
#: path folds into a role. The role reader itself is not on it.
PLATFORM_NAME_COMPARES_TODAY = frozenset({
    ("_matches_platform", "zaptec"), ("_matches_platform", "zaptec_"),
    ("discover_all_ev_chargers_from_registry", "zaptec"),
    ("build_detection_report", "zaptec"),
    ("ocpp_charge_control_switch", "ocpp"),
    ("wattpilot_force_buttons", "wattpilot"),
    ("wire_current_entity", "ocpp"), ("wire_current_entity", "wattpilot"),
})


def _platform_name_compares() -> set:
    domains = _integration_domains()
    tree = ast.parse((ROOT / "hardware_detection.py").read_text())
    found = set()

    class V(ast.NodeVisitor):
        def __init__(self):
            self.fn = []

        def visit_FunctionDef(self, n):
            self.fn.append(n.name)
            self.generic_visit(n)
            self.fn.pop()

        def _name(self):
            return self.fn[-1] if self.fn else "<module>"

        def visit_Compare(self, n):
            parts = [n.left, *n.comparators]
            lits = [p.value for p in parts if isinstance(p, ast.Constant)
                    and isinstance(p.value, str) and p.value in domains]
            lits += [e.value for p in parts
                     if isinstance(p, (ast.Tuple, ast.Set, ast.List))
                     for e in p.elts if isinstance(e, ast.Constant)
                     and e.value in domains]
            if lits and any("platform" in ast.unparse(p) for p in parts):
                found.update((self._name(), x) for x in lits)
            self.generic_visit(n)

        def visit_Call(self, n):
            f = n.func
            if (isinstance(f, ast.Attribute) and f.attr in ("startswith", "endswith")
                    and "platform" in ast.unparse(f.value)):
                for a in n.args:
                    if (isinstance(a, ast.Constant) and isinstance(a.value, str)
                            and a.value.rstrip("_") in domains):
                        found.add((self._name(), a.value))
            self.generic_visit(n)

    V().visit(tree)
    return found


def test_no_new_platform_name_compare():
    new = sorted(_platform_name_compares() - PLATFORM_NAME_COMPARES_TODAY)
    assert not new, (
        f"new platform == '<integration>' compare(s) {new}: teach the "
        "crawler the role instead")


def test_the_platform_compare_list_only_shrinks():
    gone = sorted(PLATFORM_NAME_COMPARES_TODAY - _platform_name_compares())
    assert not gone, f"{gone} are gone — remove them from the list"


#: (#1032 review) Named constants that hold integration names. The role
#: reader may read the brand list only to step AROUND brands (the role pass
#: leaves a device a brand path owns to that path) — never to treat a
#: brand differently.
BRAND_CONSTANTS = re.compile(r"BRAND|PROVEN|_EV_CHARGER_PLATFORMS|ROSTER$|"
                             r"_PATTERNS$")
ROLE_READER_MAY_STEP_AROUND = {("_roles_pass", "_EV_CHARGER_PLATFORMS")}


def test_the_role_reader_reads_no_brand_constant():
    tree = ast.parse((ROOT / "hardware_detection.py").read_text())
    fns = {n.name: n for n in ast.walk(tree)
           if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    bad = []
    for f in ROLE_READER:
        for n in ast.walk(fns[f]):
            name = (n.id if isinstance(n, ast.Name)
                    else n.attr if isinstance(n, ast.Attribute) else None)
            if name and BRAND_CONSTANTS.search(name) and \
                    (f, name) not in ROLE_READER_MAY_STEP_AROUND:
                bad.append((f, name))
            # a platform compared with something BUILT (f-string, +, getattr)
            if isinstance(n, ast.Compare):
                parts = [n.left, *n.comparators]
                if any("platform" in ast.unparse(p) for p in parts) and any(
                        isinstance(p, (ast.JoinedStr, ast.BinOp))
                        or (isinstance(p, ast.Call)
                            and getattr(p.func, "id", "") == "getattr"
                            and "platform" not in ast.unparse(p))
                        for p in parts):
                    bad.append((f, ast.unparse(n)))
    assert not bad, f"the role reader treats brands by name: {bad}"
