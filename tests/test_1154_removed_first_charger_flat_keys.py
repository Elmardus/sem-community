"""#1154 — removing the first charger left its keys to the next one.

The setup wizard copied the first charger's power sensor, current control,
the service that sets its current and its start/stop switch to the flat
top-level keys. The charger builder lets the FIRST charger take a key its
own card lacks from there (#1149 closed it for every other charger). When
the first charger was removed — the config card's ✕ (``remove_charger``),
the Configure dialog's remove, or a repair at start that drops a charger —
the next one became first, and read the removed charger's power and set
its current.

Now every removal path clears the flat wiring
(``hardware_detection.CHARGER_WIRING_KEYS``) when the first charger goes,
setup clears what a save made before the fix still holds, and the builder
lets the first charger take a flat key only while the flat keys are its
own (``charger_refs.flat_wiring_is_of``: one of its entities, or its HA
device). These pins run the real setup.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.solar_energy_management import charger_refs as cr
from custom_components.solar_energy_management import hardware_detection as hd

from .test_1144_saved_site_limit import _unload
from .test_1149_second_charger_own_wiring import (
    GO,
    GO_POWER,
    GO_SWITCH,
    NEW_FROM_CARD,
    PRIMARY,
    _primary_in,
)

#: What setup says when it clears the flat keys.
CLEARED = "#1154"

_CORE = {
    "grid_power_sensor": "sensor.test_grid_power",
    "battery_power_sensor": "sensor.test_battery_power",
    "battery_soc_sensor": "sensor.test_battery_soc",
    "solar_power_sensor": "sensor.test_solar_power",
    "battery_capacity_kwh": 15.0,
    "update_interval": 30,
}


def _entry(hass, chargers, **flat):
    """The wizard's layout: the FIRST charger's wiring in the flat keys,
    beside the charger list given (the removed one may be gone already)."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.solar_energy_management.const import DOMAIN
    from .test_services_real import _seed_sem_input_sensors
    _seed_sem_input_sensors(hass)
    data = {**_CORE, **PRIMARY, **flat,
            "ev_chargers": [dict(c) for c in chargers]}
    return MockConfigEntry(domain=DOMAIN, version=12, minor_version=1,
                           data=data, options={}, title="SEM chargers")


FIRST = {"id": "ev_charger", "name": "First", **PRIMARY}


async def _set_up(hass, entry):
    from .test_services_real import _setup_sem
    await _setup_sem(hass, entry)
    return entry.runtime_data._ev_devices


def _flat_left(entry):
    """The flat wiring the entry still holds, in either store."""
    return {k: v for k, v in {**entry.data, **entry.options}.items()
            if k in hd.CHARGER_WIRING_KEYS and v not in (None, "")}


# ── which flat keys belong to the first charger (pure) ───────────────────

class TestFlatWiringIsOf:

    def test_a_card_holding_the_mirror_owns_it(self):
        assert cr.flat_wiring_is_of(FIRST, PRIMARY)

    def test_a_legacy_card_sharing_one_entity_owns_it(self):
        card = {"id": "a", "ev_charging_power_sensor": PRIMARY["ev_charging_power_sensor"]}
        assert cr.flat_wiring_is_of(card, PRIMARY)

    def test_the_next_charger_does_not_own_the_removed_ones(self):
        assert not cr.flat_wiring_is_of(GO, PRIMARY)

    def test_the_config_cards_new_charger_owns_nothing(self):
        assert not cr.flat_wiring_is_of(NEW_FROM_CARD, PRIMARY)

    def test_a_shared_service_name_is_not_an_identity(self):
        """Two boxes of one brand share a service and its field name."""
        card = {"id": "b", "ev_charger_service": PRIMARY["ev_charger_service"],
                "ev_service_param_name": PRIMARY["ev_service_param_name"],
                "ev_charging_power_sensor": "sensor.other_power"}
        assert not cr.flat_wiring_is_of(card, PRIMARY)

    def test_the_service_device_id_is_an_identity(self):
        card = {"id": "b", "ev_service_device_id": PRIMARY["ev_service_device_id"]}
        assert cr.flat_wiring_is_of(card, PRIMARY)

    def test_one_device_is_one_box_whatever_entities_each_names(self):
        card = {"id": "c", "ev_charging_power_sensor": "sensor.renamed_power"}
        same = {card["ev_charging_power_sensor"]: "dev1",
                PRIMARY["ev_charging_power_sensor"]: "dev1"}
        other = {**same, card["ev_charging_power_sensor"]: "dev2"}
        assert cr.flat_wiring_is_of(card, PRIMARY, same.get)
        assert not cr.flat_wiring_is_of(card, PRIMARY, other.get)
        # devices unknown (a KEBA, a template sensor): the entities decide
        assert cr.flat_wiring_is_of(FIRST, PRIMARY, lambda e: None)
        assert not cr.flat_wiring_is_of(GO, PRIMARY, lambda e: None)

    def test_no_flat_wiring_is_nothing_to_borrow(self):
        assert cr.flat_wiring_is_of(GO, {"ev_min_current": 8})
        assert cr.flat_wiring_is_of(None, {})

    def test_stale_needs_a_charger_list_or_refs(self):
        """A pre-migration save is its flat keys: setup builds the charger
        from them, so they stay. Once refs exist, no charger owns them."""
        assert cr.stale_flat_wiring(PRIMARY, {}, None) == {}
        assert cr.stale_flat_wiring(PRIMARY, {"ev_chargers": []}, []) == {}
        gone = cr.stale_flat_wiring(PRIMARY, {cr.REFS_KEY: []}, [])
        assert set(gone) == set(PRIMARY)
        assert set(cr.stale_flat_wiring(PRIMARY, {}, [GO])) == set(PRIMARY)
        assert cr.stale_flat_wiring(PRIMARY, {}, [FIRST, GO]) == {}

    def test_options_cover_data_as_the_merge_does(self):
        assert cr.stale_flat_wiring(
            PRIMARY, {k: GO.get(k) for k in GO if k.startswith("ev_")}, [GO]) \
            == {}

    def test_without_flat_wiring_keeps_the_settings(self):
        store = {**PRIMARY, "ev_min_current": 8, "ev_phases": 1, "x": 1}
        assert cr.without_flat_wiring(store) == {"ev_min_current": 8,
                                                 "ev_phases": 1, "x": 1}
        assert cr.without_flat_wiring({"ev_min_current": 8}) is None

    def test_every_key_the_wizard_mirrors_is_cleared(self):
        """The wizard's flat list (config_flow's wrap of the flat keys) less
        the settings is all wiring."""
        mirrored = {
            "ev_connected_sensor", "ev_charging_sensor",
            "ev_charging_power_sensor", "ev_charger_service",
            "ev_charger_service_entity_id", "ev_current_sensor",
            "ev_total_energy_sensor", "ev_session_energy_sensor",
            "ev_service_param_name", "ev_service_device_id",
            "ev_start_stop_entity", "ev_charge_mode_entity",
            "ev_charge_mode_start", "ev_charge_mode_stop",
            "ev_start_service", "ev_start_service_data",
            "ev_stop_service", "ev_stop_service_data",
            "ev_charger_service_data", "ev_current_control_entity",
            "ev_phase_switch_entity",
        }
        assert mirrored <= hd.CHARGER_WIRING_KEYS


# ── the config card's ✕ (remove_charger) ─────────────────────────────────

async def _remove(hass, entry, charger_id, monkeypatch):
    """The service, with the reload it starts held back: what the service
    itself wrote is what is asserted. Then the real reload."""
    from custom_components.solar_energy_management.const import DOMAIN
    reload = AsyncMock(return_value=True)
    with monkeypatch.context() as m:
        m.setattr(hass.config_entries, "async_reload", reload)
        await hass.services.async_call(DOMAIN, "remove_charger",
                                       {"charger_id": charger_id}, blocking=True)
        await hass.async_block_till_done()
    assert reload.await_count == 1
    written = dict(entry.data), dict(entry.options)
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    return written


@pytest.mark.asyncio
async def test_the_card_remove_of_the_first_charger_clears_its_keys(
        sem_real_hass, monkeypatch):
    hass = sem_real_hass
    entry = _entry(hass, [FIRST, GO], ev_min_current=8, ev_phases=1)
    devs = await _set_up(hass, entry)
    try:
        assert list(devs) == ["ev_charger", "ev_charger_1"]
        data, options = await _remove(hass, entry, "ev_charger", monkeypatch)
        # the service's own write, before any setup ran
        assert not set(data) & hd.CHARGER_WIRING_KEYS
        assert not set(options) & hd.CHARGER_WIRING_KEYS
        # settings stay
        assert data["ev_min_current"] == 8 and data["ev_phases"] == 1
        devs = entry.runtime_data._ev_devices
        assert list(devs) == ["ev_charger_1"]
        go = devs["ev_charger_1"]
        assert _primary_in(go) == []
        assert go.power_entity_id == GO_POWER
        assert go.start_stop_entity == GO_SWITCH
        assert not go.current_entity_id and not go.charger_service
        assert _flat_left(entry) == {}
    finally:
        await _unload(hass, entry)


@pytest.mark.asyncio
async def test_the_card_remove_of_a_second_charger_keeps_the_keys(
        sem_real_hass, monkeypatch):
    hass = sem_real_hass
    entry = _entry(hass, [FIRST, GO])
    await _set_up(hass, entry)
    try:
        data, _options = await _remove(hass, entry, "ev_charger_1", monkeypatch)
        assert {k: data[k] for k in PRIMARY} == PRIMARY
        assert _flat_left(entry) == PRIMARY
        dev = entry.runtime_data._ev_devices["ev_charger"]
        assert dev.current_entity_id == PRIMARY["ev_current_control_entity"]
    finally:
        await _unload(hass, entry)


@pytest.mark.asyncio
async def test_the_card_remove_of_the_last_charger_clears_its_keys(
        sem_real_hass, monkeypatch):
    """Nothing else may read them: the sensor reader read the flat power."""
    hass = sem_real_hass
    entry = _entry(hass, [FIRST])
    await _set_up(hass, entry)
    try:
        data, options = await _remove(hass, entry, "ev_charger", monkeypatch)
        assert not set(data) & hd.CHARGER_WIRING_KEYS
        assert not set(options) & hd.CHARGER_WIRING_KEYS
        assert entry.runtime_data._ev_devices == {}
    finally:
        await _unload(hass, entry)


# ── the Configure dialog's remove ────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_dialog_save_clears_the_keys_when_the_first_charger_went(
        sem_real_hass, monkeypatch):
    """The dialog removes a first charger only when its card is half
    (#1054 round 3), but the flat keys can still be that card's — the
    wizard's mirror. Its save clears them in options and in data."""
    from custom_components.solar_energy_management.config_flow import (
        OptionsFlowHandler,
    )
    half = {"id": "ev_charger", "name": "First",
            "ev_total_energy_sensor": PRIMARY["ev_total_energy_sensor"]}
    flow = OptionsFlowHandler.__new__(OptionsFlowHandler)
    flow.hass = sem_real_hass
    update = MagicMock()
    monkeypatch.setattr(flow.hass.config_entries, "async_update_entry", update)
    entry = MagicMock(data={**_CORE, **PRIMARY, "ev_min_current": 8},
                      options={cr.LEGACY_KEY: [half, dict(GO)],
                               cr.REFS_KEY: [], **PRIMARY})
    type(flow).config_entry = property(lambda self: entry)
    try:
        saved = flow._without_flat_wiring_of_removed_first(
            {**entry.options, cr.LEGACY_KEY: [dict(GO)], "ev_phases": 1})
        assert not set(saved) & hd.CHARGER_WIRING_KEYS
        assert saved["ev_phases"] == 1 and saved[cr.LEGACY_KEY] == [GO]
        update.assert_called_once()
        data = update.call_args.kwargs["data"]
        assert not set(data) & hd.CHARGER_WIRING_KEYS
        assert data["ev_min_current"] == 8

        # the first charger stays → nothing changes
        update.reset_mock()
        same = {**entry.options, "ev_phases": 1}
        assert flow._without_flat_wiring_of_removed_first(same) is same
        update.assert_not_called()
    finally:
        del type(flow).config_entry


@pytest.mark.asyncio
async def test_the_dialog_remove_of_a_half_first_charger_end_to_end(
        sem_real_hass, monkeypatch):
    from .test_1054_refs_options_flow import _walk
    hass = sem_real_hass
    half = {"id": "ev_charger", "name": "First",
            "ev_total_energy_sensor": PRIMARY["ev_total_energy_sensor"]}
    entry = _entry(hass, [half, GO])
    devs = await _set_up(hass, entry)
    try:
        # the half card ran on its flat mirror before
        assert devs["ev_charger"].current_entity_id == PRIMARY["ev_current_control_entity"]
        reload = AsyncMock(return_value=True)
        with monkeypatch.context() as m:
            m.setattr(hass.config_entries, "async_reload", reload)
            await _walk(hass, entry, {
                "ev_charger_menu": [{"action": "remove_charger"}],
                "ev_charger_remove": [{"charger_to_remove": "ev_charger"}],
            })
        # the dialog's own save, before any setup ran
        assert [c["id"] for c in entry.options[cr.LEGACY_KEY]] == ["ev_charger_1"]
        assert _flat_left(entry) == {}
        await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        devs = entry.runtime_data._ev_devices
        assert list(devs) == ["ev_charger_1"]
        assert _primary_in(devs["ev_charger_1"]) == []
    finally:
        await _unload(hass, entry)


# ── the repairs at start, and a save made before the fix ─────────────────

@pytest.mark.asyncio
async def test_setup_clears_the_keys_a_removed_first_charger_left(
        sem_real_hass, caplog):
    """A save the old remove left: the next charger first, the removed
    one's wiring still flat."""
    hass = sem_real_hass
    entry = _entry(hass, [GO], ev_min_current=8)
    devs = await _set_up(hass, entry)
    try:
        go = devs["ev_charger_1"]
        assert _primary_in(go) == []
        assert go.power_entity_id == GO_POWER
        assert not go.current_entity_id and not go.charger_service
        assert _flat_left(entry) == {}
        assert entry.data["ev_min_current"] == 8
        assert any(CLEARED in r.getMessage() and r.levelname == "WARNING"
                   for r in caplog.records)
        # the sensor reader reads the charger's own sensors, not the box gone
        assert entry.runtime_data.config.get("ev_charging_power_sensor") is None
    finally:
        await _unload(hass, entry)


@pytest.mark.asyncio
async def test_setup_keeps_the_keys_of_the_first_charger(sem_real_hass, caplog):
    hass = sem_real_hass
    entry = _entry(hass, [FIRST, GO])
    await _set_up(hass, entry)
    try:
        assert _flat_left(entry) == PRIMARY
        assert not any(CLEARED in r.getMessage() for r in caplog.records)
    finally:
        await _unload(hass, entry)


@pytest.mark.asyncio
async def test_a_start_repair_that_drops_the_first_charger_clears_its_keys(
        sem_real_hass, monkeypatch):
    """The repairs at start run before the check: #1036 drops a meter setup
    saved as the charger, and it can be the first."""
    import custom_components.solar_energy_management as sem
    hass = sem_real_hass

    def _drop_first(_hass, chargers):
        if any(isinstance(c, dict) and c.get("id") == "ev_charger"
               for c in chargers or ()):
            return [c for c in chargers if c.get("id") != "ev_charger"]
        return None

    monkeypatch.setattr(sem, "_drop_meters_saved_as_chargers", _drop_first)
    entry = _entry(hass, [FIRST, GO])
    devs = await _set_up(hass, entry)
    try:
        assert list(devs) == ["ev_charger_1"]
        assert _primary_in(devs["ev_charger_1"]) == []
        assert _flat_left(entry) == {}
    finally:
        await _unload(hass, entry)


# ── the builder's own check ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_builder_takes_no_flat_key_that_is_not_the_first_chargers(
        sem_real_hass, monkeypatch):
    """With the setup check out of the way the flat keys are still there;
    the first charger still takes none of them."""
    import custom_components.solar_energy_management as sem
    hass = sem_real_hass
    monkeypatch.setattr(sem, "_drop_flat_wiring_of_a_removed_first",
                        lambda *a, **k: None)
    entry = _entry(hass, [GO, NEW_FROM_CARD | {"id": "ev_charger_2"}])
    devs = await _set_up(hass, entry)
    try:
        assert _flat_left(entry) == PRIMARY
        assert list(devs) == ["ev_charger_1"]
        go = devs["ev_charger_1"]
        assert _primary_in(go) == []
        assert not go.current_entity_id and not go.charger_service
        assert not go.service_device_id
    finally:
        await _unload(hass, entry)


@pytest.mark.asyncio
async def test_the_config_cards_new_charger_made_first_is_not_built(
        sem_real_hass, monkeypatch):
    """The card's new charger has no wiring; first, it took the removed
    charger's whole box from the flat keys."""
    import custom_components.solar_energy_management as sem
    hass = sem_real_hass
    monkeypatch.setattr(sem, "_drop_flat_wiring_of_a_removed_first",
                        lambda *a, **k: None)
    entry = _entry(hass, [NEW_FROM_CARD])
    devs = await _set_up(hass, entry)
    try:
        assert devs == {}
    finally:
        await _unload(hass, entry)
