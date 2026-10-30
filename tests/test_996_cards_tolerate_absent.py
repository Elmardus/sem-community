"""#996 — the config and control cards survive a capability's controls
not existing, and reference no entity SEM does not build.

The cards pick their entities themselves (the dashboard template names
none of them), so the generator's prune cannot help them. Two things
keep them honest: every ``<platform>.sem_<key>`` literal in the two cards
is a real static entity (a stale id would render nothing forever), and
every row of a capability goes through a renderer that returns
``nothing`` for a missing entity — no empty row, no orphan label."""
from __future__ import annotations

import re
from pathlib import Path

from custom_components.solar_energy_management.coordinator.install_modules import (
    CORE_BY_DECISION, ENTITY_MODULES, Module,
)

CARDS = Path(__file__).resolve().parents[1] / "dashboard" / "card" / "src" / "cards"
CONFIG_CARD = CARDS / "sem-config-card.js"
CONTROL_CARD = CARDS / "sem-control-card.js"
ENTITY_LITERAL = re.compile(r"'(number|switch|select|button|sensor|binary_sensor)\.sem_([a-z0-9_]+)'")
CAPABILITIES = frozenset({
    Module.DYNAMIC_TARIFF, Module.EXPORT_LIMIT, Module.SOLAR_FORECAST,
    Module.PV_SIZE, Module.INVESTMENT,
})


def _static_keys():
    from custom_components.solar_energy_management.binary_sensor import BINARY_SENSOR_TYPES
    from custom_components.solar_energy_management.button import BUTTONS
    from custom_components.solar_energy_management.number import NUMBER_TYPES
    from custom_components.solar_energy_management.select import SELECT_TYPES
    from custom_components.solar_energy_management.sensor import SENSOR_TYPES
    from custom_components.solar_energy_management.switch import SWITCH_TYPES
    lists = {"sensor": SENSOR_TYPES, "number": NUMBER_TYPES, "switch": SWITCH_TYPES,
             "binary_sensor": BINARY_SENSOR_TYPES, "button": BUTTONS, "select": SELECT_TYPES}
    return {(p, d.key) for p, ds in lists.items() for d in ds}


def _references(path):
    return {(m.group(1), m.group(2)) for m in ENTITY_LITERAL.finditer(path.read_text(encoding="utf-8"))}


def test_the_two_cards_reference_only_entities_sem_builds():
    real = _static_keys()
    stale = sorted((_references(CONFIG_CARD) | _references(CONTROL_CARD)) - real)
    assert not stale, f"card references to entities no platform creates: {stale}"


def test_every_capability_row_on_the_config_card_is_guarded():
    src = CONFIG_CARD.read_text(encoding="utf-8")
    capability_rows = {
        pk for pk, needs in ENTITY_MODULES.items() if needs & CAPABILITIES
    } & _references(CONFIG_CARD)
    assert capability_rows, "the config card must show at least one capability control"
    unguarded = []
    for platform, key in sorted(capability_rows):
        entity_id = f"{platform}.sem_{key}"
        for m in re.finditer(rf"(_render[A-Za-z]+)\('{re.escape(entity_id)}'", src):
            if m.group(1) not in ("_renderToggle", "_renderStepper"):
                unguarded.append((entity_id, m.group(1)))
    assert not unguarded, unguarded


def test_the_guarded_renderers_return_nothing_for_a_missing_entity():
    src = CONFIG_CARD.read_text(encoding="utf-8")
    for renderer in ("_renderToggle", "_renderStepper"):
        body = src[src.index(f"    {renderer}(entityId"):]
        head = body[:600]
        assert "if (!entity) return nothing;" in head, renderer


def test_core_rows_the_cards_use_are_on_every_install():
    # A control the cards always show must be core, or a house without the
    # capability shows a gap where nothing explains itself.
    always = {("switch", "observer_mode"), ("number", "update_interval"),
              ("number", "minimum_solar_power"), ("number", "regulation_offset")}
    assert always <= _references(CONFIG_CARD)
    for pk in always:
        assert pk in CORE_BY_DECISION and pk not in ENTITY_MODULES, pk
