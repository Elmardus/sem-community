"""#996 — the generated dashboard on the plain house.

The template names no capability entity explicitly (the cards pick their
entities themselves and render nothing for a missing one), so the prune
has nothing to remove — and must remove nothing else. A full house is
untouched; UNKNOWN prunes nothing."""
from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import MagicMock

import yaml

from custom_components.solar_energy_management.coordinator.install_modules import (
    Module, Presence, absent_entity_ids, all_unknown,
)
from custom_components.solar_energy_management.features.dashboard_generator import (
    DashboardGenerator,
)

TEMPLATE = Path(__file__).resolve().parents[1] / "dashboard" / "sem_dashboard_template.yaml"
CAPABILITIES = (Module.DYNAMIC_TARIFF, Module.EXPORT_LIMIT, Module.SOLAR_FORECAST,
                Module.PV_SIZE, Module.INVESTMENT)
PLAIN = {**all_unknown(), **{c: Presence.ABSENT for c in CAPABILITIES}}


def _generator(presence):
    hass = MagicMock()
    entry = MagicMock()
    entry.data = {"ev_chargers": [{"id": "ev_charger"}]}
    entry.options = {}
    entry.runtime_data.setup_presence = presence
    hass.config_entries.async_entries.return_value = [entry]
    hass.data = {}
    return DashboardGenerator(hass)


def _template():
    return yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))


def _dump(tpl):
    return json.dumps(tpl, sort_keys=True)


def test_the_plain_house_references_no_capability_entity():
    """After the generator's prune, the plain house's dashboard names no
    entity it does not have. (#1022 put sensor.sem_pv_health in the
    template explicitly — a forecast entity — so the prune must drop it.)"""
    from custom_components.solar_energy_management.features.dashboard_generator import (
        DashboardGenerator,
    )
    gone = absent_entity_ids(PLAIN)
    assert gone, "the plain house must lose something, or this test is vacuous"
    tpl = _template()
    assert "sensor.sem_pv_health" in _dump(tpl)   # the prune has work to do
    DashboardGenerator._drop_entity_refs(object.__new__(DashboardGenerator),
                                         tpl.get("views", []), gone)
    dumped = _dump(tpl)
    leftovers = sorted(
        eid for eid in gone
        if re.search(rf"(?<![a-z0-9_.]){re.escape(eid)}(?![a-z0-9_])", dumped))
    assert not leftovers, leftovers


def test_the_plain_house_keeps_every_view():
    tpl = _template()
    before = [v.get("path") for v in tpl["views"]]
    _generator(PLAIN)._prune_absent_modules(tpl)
    assert [v.get("path") for v in tpl["views"]] == before


def test_a_full_house_loses_nothing():
    tpl = _template()
    before = _dump(tpl)
    _generator({m: Presence.PRESENT for m in Module})._prune_absent_modules(tpl)
    assert _dump(tpl) == before
