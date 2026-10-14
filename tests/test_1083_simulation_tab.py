"""#1083 — the Simulation tab: a 9th view that shows only while the
Simulation tab switch is on. The other 8 views never change."""
from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import yaml

from custom_components.solar_energy_management.consts.core import CONF_SIMULATION_TAB
from custom_components.solar_energy_management.features.dashboard_generator import (
    DashboardGenerator,
)

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "dashboard" / "sem_dashboard_template.yaml"
TRANSLATIONS = ROOT / "dashboard" / "translations.json"
TAB_HEADER = ROOT / "dashboard" / "card" / "src" / "cards" / "sem-tab-header.js"

SIM_PATH = "simulation"
SIM_CARD = "custom:sem-simulation-card"
SIM_KEYS = ("simulation", "simulation_sub", "simulation_days_stored",
            "simulation_selected", "simulation_gaps")
OTHER_PATHS = ["home", "energy", "battery", "ev", "control", "config",
               "costs", "system"]


def _generator(options=None, data=None):
    hass = MagicMock()
    # A plain object, not a MagicMock: the flag resolver reads attributes.
    entry = SimpleNamespace(data=dict(data or {}), options=dict(options or {}))
    hass.config_entries.async_entries.return_value = [entry]
    hass.data = {}
    return DashboardGenerator(hass)


def _template():
    return yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))


def _paths(tpl):
    return [v.get("path") for v in tpl["views"]]


def _sim_view(tpl):
    return next(v for v in tpl["views"] if v.get("path") == SIM_PATH)


def test_the_template_has_the_simulation_view_last():
    tpl = _template()
    assert _paths(tpl) == OTHER_PATHS + [SIM_PATH]
    view = _sim_view(tpl)
    assert view["title"] == "Simulation"
    assert view["icon"] == "mdi:flask-outline"
    assert view["type"] == "panel"


def test_the_view_holds_the_header_and_one_card():
    stack = _sim_view(_template())["cards"]
    assert len(stack) == 1 and stack[0]["type"] == "vertical-stack"
    cards = stack[0]["cards"]
    assert [c["type"] for c in cards] == ["custom:sem-tab-header", SIM_CARD]
    assert cards[0]["tab"] == SIM_PATH
    assert cards[1]["entity_prefix"] == "sensor.sem_"


def test_switch_off_drops_the_tab():
    tpl = _template()
    _generator()._prune_sleeping_tabs(tpl)
    assert _paths(tpl) == OTHER_PATHS


def test_switch_off_in_options_beats_on_in_data():
    tpl = _template()
    _generator({CONF_SIMULATION_TAB: False},
               {CONF_SIMULATION_TAB: True})._prune_sleeping_tabs(tpl)
    assert SIM_PATH not in _paths(tpl)


def test_switch_on_keeps_the_tab():
    tpl = _template()
    _generator({CONF_SIMULATION_TAB: True})._prune_sleeping_tabs(tpl)
    assert _paths(tpl) == OTHER_PATHS + [SIM_PATH]


def test_no_entry_drops_the_tab():
    hass = MagicMock()
    hass.config_entries.async_entries.return_value = []
    hass.data = {}
    tpl = _template()
    DashboardGenerator(hass)._prune_sleeping_tabs(tpl)
    assert SIM_PATH not in _paths(tpl)


def test_the_other_eight_views_never_change():
    before = [v for v in _template()["views"] if v.get("path") != SIM_PATH]
    for opts in ({}, {CONF_SIMULATION_TAB: True}):
        tpl = _template()
        _generator(opts)._prune_sleeping_tabs(tpl)
        after = [v for v in tpl["views"] if v.get("path") != SIM_PATH]
        assert json.dumps(after, sort_keys=True) == json.dumps(before, sort_keys=True)


def test_switch_on_changes_nothing():
    tpl = _template()
    keep = copy.deepcopy(tpl)
    _generator({CONF_SIMULATION_TAB: True})._prune_sleeping_tabs(tpl)
    assert tpl == keep


def test_generate_dashboard_calls_the_prune():
    src = (ROOT / "features" / "dashboard_generator.py").read_text(encoding="utf-8")
    body = src.split("async def generate_dashboard", 1)[1].split("\n    def ", 1)[0]
    assert "self._prune_sleeping_tabs(template)" in body


def test_translation_keys_in_every_language():
    data = json.loads(TRANSLATIONS.read_text(encoding="utf-8"))
    assert len(data) == 16
    missing = [(lang, k) for lang, table in data.items()
               for k in SIM_KEYS if not str(table.get(k, "")).strip()]
    assert not missing, missing
    assert data["en"]["simulation"] == "Simulation"


def test_the_tab_header_knows_the_tab():
    src = TAB_HEADER.read_text(encoding="utf-8")
    assert "titleKey: 'simulation', subtitleKey: 'simulation_sub'" in src
    assert "tab === 'simulation'" in src
    assert "#8353d1" in src
