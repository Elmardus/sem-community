"""#1019 — hints are one select, ``select.sem_hints``: off / weekly / all,
default off. A pick writes the entry option and the running config."""
import json
import os
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.solar_energy_management.coordinator.hints import (
    DEFAULT_HINT_MODE, HINT_CATEGORIES, HINT_MODES, enabled_categories,
)
from custom_components.solar_energy_management.select import (
    SELECT_TYPES, SEMSelectEntity,
)
from custom_components.solar_energy_management.switch import SWITCH_TYPES

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _description():
    return next(d for d in SELECT_TYPES if d.key == "hints")


class TestModes:
    def test_the_three_modes_default_off(self):
        assert HINT_MODES == ("off", "weekly", "all")
        assert DEFAULT_HINT_MODE == "off"
        assert list(_description().options) == list(HINT_MODES)

    def test_what_each_mode_turns_on(self):
        assert not any(enabled_categories("off").values())
        assert enabled_categories("weekly") == {c: c == "weekly_summary" for c in HINT_CATEGORIES}
        assert all(enabled_categories("all").values())
        assert not any(enabled_categories(None).values())
        assert not any(enabled_categories("junk").values())

    def test_no_hint_switches_remain(self):
        assert not [d.key for d in SWITCH_TYPES if d.key.startswith("hint_")]

    def test_every_language_names_the_select_and_its_states(self):
        files = [os.path.join(_HERE, "strings.json")] + [
            os.path.join(_HERE, "translations", n)
            for n in os.listdir(os.path.join(_HERE, "translations"))]
        for path in files:
            with open(path, encoding="utf-8") as f:
                entry = json.load(f)["entity"]["select"]["hints"]
            assert entry["name"], path
            assert set(entry["state"]) == set(HINT_MODES), path

    def test_the_card_label_exists_in_every_language(self):
        with open(os.path.join(_HERE, "dashboard", "translations.json"), encoding="utf-8") as f:
            data = json.load(f)
        for lang, strings in data.items():
            for key in ("hints", "config_help_hints", "off", "weekly", "all"):
                assert strings.get(key), (lang, key)


class TestPick:
    def _entity(self, config):
        coordinator = MagicMock()
        coordinator.config = config
        coordinator.async_update_config = AsyncMock()
        entry = MagicMock()
        entry.entry_id = "e"
        entry.options = {}
        ent = SEMSelectEntity(coordinator, entry, _description())
        ent.hass = MagicMock()
        ent.async_write_ha_state = MagicMock()
        return ent, coordinator, entry

    def test_default_is_off(self):
        ent, _, _ = self._entity({})
        assert ent.current_option == "off"

    @pytest.mark.asyncio
    async def test_a_pick_writes_the_option_and_the_running_config(self):
        ent, coordinator, entry = self._entity({})
        await ent.async_select_option("weekly")
        coordinator.async_update_config.assert_awaited_once_with({"hints": "weekly"})
        _, kwargs = ent.hass.config_entries.async_update_entry.call_args
        assert kwargs["options"]["hints"] == "weekly"

    @pytest.mark.asyncio
    async def test_an_unknown_value_is_ignored(self):
        ent, coordinator, _ = self._entity({})
        await ent.async_select_option("loud")
        coordinator.async_update_config.assert_not_awaited()
