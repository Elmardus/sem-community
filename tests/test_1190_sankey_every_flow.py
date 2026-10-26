"""#1190 — the Energy tab Sankey draws every energy flow SEM publishes.

SEM booked a sold battery's energy in ``sensor.sem_flow_battery_to_grid_energy``
(#776), but the Sankey's Battery node listed only Home and EV. The Export
total held that energy and no line led to it from Battery.

The guard reads the flow keys from ``SENSOR_TYPES``, so the next flow SEM
adds fails here until the Sankey draws it: a child of its source total in the
first column, and a node in the middle column that leads to its target total.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import MagicMock

import yaml

from custom_components.solar_energy_management.coordinator.install_modules import (
    Module, Presence,
)
from custom_components.solar_energy_management.features.dashboard_generator import (
    DashboardGenerator,
)
from custom_components.solar_energy_management.sensor import SENSOR_TYPES

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "dashboard" / "sem_dashboard_template.yaml"
TRANSLATIONS = ROOT / "dashboard" / "translations.json"

# Each end of a flow, as the Sankey's total node for it.
SOURCE_TOTAL = {
    "solar": "sensor.sem_daily_solar_energy",
    "grid": "sensor.sem_daily_grid_import_energy",
    "battery": "sensor.sem_daily_battery_discharge_energy",
}
TARGET_TOTAL = {
    "home": "sensor.sem_daily_home_energy",
    "ev": "sensor.sem_daily_ev_energy",
    "battery": "sensor.sem_daily_battery_charge_energy",
    "grid": "sensor.sem_daily_grid_export_energy",
}
FLOW_KEY = re.compile(r"^flow_([a-z]+)_to_([a-z]+)_energy$")


def _template():
    return yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))


def _sankey(tpl):
    """The one ``custom:sankey-chart`` card config in the template."""
    found = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "custom:sankey-chart":
                found.append(node)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(tpl)
    assert len(found) == 1, f"expected one sankey-chart, found {len(found)}"
    return found[0]


def _columns(sankey):
    return [{e["entity_id"]: e for e in section["entities"]}
            for section in sankey["sections"]]


def _flow_keys():
    keys = [d.key for d in SENSOR_TYPES if FLOW_KEY.match(d.key)]
    assert len(keys) >= 10, keys  # never pass on an empty list
    return keys


def test_every_published_flow_has_its_sankey_line():
    sources, middle, targets = _columns(_sankey(_template()))
    missing = []
    for key in _flow_keys():
        src, dst = FLOW_KEY.match(key).groups()
        flow = f"sensor.sem_{key}"
        source, target = SOURCE_TOTAL[src], TARGET_TOTAL[dst]
        if flow not in sources[source].get("children", []):
            missing.append(f"{source} has no child {flow}")
        if flow not in middle:
            missing.append(f"no middle node {flow}")
        elif middle[flow].get("children") != [target]:
            missing.append(f"{flow} leads to {middle[flow].get('children')}, not {target}")
        if target not in targets:
            missing.append(f"no target node {target}")
    assert not missing, missing


def test_the_battery_sells_into_export():
    sources, middle, _ = _columns(_sankey(_template()))
    flow = "sensor.sem_flow_battery_to_grid_energy"
    assert flow in sources["sensor.sem_daily_battery_discharge_energy"]["children"]
    assert middle[flow]["children"] == ["sensor.sem_daily_grid_export_energy"]
    assert middle[flow]["name"] == "Bat>Grid"


def test_the_new_label_is_translated_in_every_language():
    tables = json.loads(TRANSLATIONS.read_text(encoding="utf-8"))
    assert len(tables) == 16
    for lang, table in tables.items():
        assert table.get("Bat>Grid"), lang
        if lang != "en":
            assert table["Bat>Grid"] != "Bat>Grid", lang


def _generator(presence, language="en"):
    hass = MagicMock()
    hass.config.language = language
    entry = MagicMock()
    entry.data = {"ev_chargers": [{"id": "ev_charger"}]}
    entry.options = {}
    entry.runtime_data.setup_presence = presence
    hass.config_entries.async_entries.return_value = [entry]
    hass.data = {}
    gen = DashboardGenerator(hass)
    gen._cached_translations = json.loads(TRANSLATIONS.read_text(encoding="utf-8"))
    return gen


def test_a_home_without_a_battery_has_no_battery_line():
    tpl = _template()
    presence = {m: Presence.PRESENT for m in Module}
    presence[Module.BATTERY] = Presence.ABSENT
    _generator(presence)._prune_absent_modules(tpl)
    sources, middle, _ = _columns(_sankey(tpl))
    assert "sensor.sem_daily_battery_discharge_energy" not in sources
    assert "sensor.sem_flow_battery_to_grid_energy" not in middle
    # Solar still leads into Export.
    assert middle["sensor.sem_flow_solar_to_grid_energy"]["children"] == [
        "sensor.sem_daily_grid_export_energy"]


def test_the_battery_line_is_named_in_the_home_language():
    gen = _generator({m: Presence.PRESENT for m in Module}, language="de")
    tpl = gen._translate_dashboard(_template())
    _, middle, _ = _columns(_sankey(tpl))
    assert middle["sensor.sem_flow_battery_to_grid_energy"]["name"] == "Bat>Netz"
