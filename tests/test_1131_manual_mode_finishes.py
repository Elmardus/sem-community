"""#1131 — Force charge stayed on when the battery was full.

The mode was a place to stay: at 100 % the house took 1 % and SEM bought it
back from the grid at once, for as long as nobody changed the mode. Force
discharge did the same at the reserve — the next day's sun was sold down to
it again. Each is one job: when it is done the battery goes back to Auto.

Three links, each driven for real:

* ``manual_mode_done`` — the pure rule (full for a charge, at the reserve for
  a sale, never on a level SEM cannot read);
* ``SEMCoordinator._finish_manual_battery_mode`` — writes Auto into the store
  the battery's control on screen writes (real config entry, real select
  platform), and the select shows it;
* ``_run_battery_pipeline`` — calls it for every battery with the level and
  whether it is a reading, BEFORE the cycle decides, and before the VPP
  override is laid over the user's mode.
"""
from __future__ import annotations

import importlib.util
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from custom_components.solar_energy_management.coordinator.battery_night import (
    FULL_SOC,
)
from custom_components.solar_energy_management.coordinator.charger_types import (
    BatteryPower,
    BatteryRuntime,
)
from custom_components.solar_energy_management.coordinator.coordinator import (
    SEMCoordinator,
)
from custom_components.solar_energy_management.coordinator.decide_battery import (
    manual_mode_done,
)
from custom_components.solar_energy_management.coordinator.install_modules import (
    Module, Presence,
)

_HAS_PHACC = importlib.util.find_spec("pytest_homeassistant_custom_component") is not None


# ── 1. The rule ────────────────────────────────────────────────────────────

class TestTheRule:
    @pytest.mark.parametrize("soc,done", [
        (40.0, False), (98.9, False), (FULL_SOC, True), (100.0, True)])
    def test_a_charge_is_done_when_the_battery_is_full(self, soc, done):
        assert manual_mode_done("force_charge", soc, soc_read=True,
                                reserve=20.0) is done

    @pytest.mark.parametrize("soc,done", [
        (80.0, False), (30.5, False), (30.0, True), (12.0, True)])
    def test_a_sale_is_done_at_its_reserve(self, soc, done):
        assert manual_mode_done("force_discharge", soc, soc_read=True,
                                reserve=30.0) is done

    @pytest.mark.parametrize("mode,soc", [
        ("force_charge", 100.0), ("force_discharge", 0.0)])
    def test_a_level_sem_cannot_read_finishes_nothing(self, mode, soc):
        # 0.0 is what a unit that never answered carries; 100 % may be held
        # from before a dropout. Neither is the battery's level.
        assert manual_mode_done(mode, soc, soc_read=False, reserve=20.0) is False
        assert manual_mode_done(mode, None, soc_read=True, reserve=20.0) is False

    @pytest.mark.parametrize("mode", [
        "auto", "self_consumption", "off", "allow_arbitrage", None, ""])
    def test_the_standing_modes_never_finish(self, mode):
        for soc in (0.0, 20.0, 100.0):
            assert manual_mode_done(mode, soc, soc_read=True, reserve=20.0) is False

    def test_the_sale_ends_exactly_where_decide_battery_stops_selling(self):
        # decide_battery sells while ``soc > reserve``; done is the opposite,
        # so a readable level is always either selling or finished.
        from custom_components.solar_energy_management.coordinator.charger_types import (
            BatteryIntent, BatteryView, FleetContext,
        )
        from custom_components.solar_energy_management.coordinator.decide_battery import (
            decide_battery,
        )
        for soc in (19.0, 19.9, 20.0, 20.1, 21.0, 60.0):
            view = BatteryView(
                runtime=BatteryRuntime(battery_id="b1", last_known_soc=soc),
                config={"battery_mode": "force_discharge",
                        "battery_reserve_soc": 20.0},
                fleet=FleetContext(), charging_state="idle",
                ev_charging=False, home_consumption_w=500.0)
            sells = decide_battery(view).intent is BatteryIntent.FORCE_DISCHARGE
            done = manual_mode_done("force_discharge", soc, soc_read=True,
                                    reserve=20.0)
            assert sells != done, soc


# ── 2. The write, and the select that shows it ─────────────────────────────

def _entry(hass, options):
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from custom_components.solar_energy_management.const import DOMAIN
    entry = MockConfigEntry(domain=DOMAIN, data={}, options=dict(options),
                            title="SEM #1131")
    entry.add_to_hass(hass)
    return entry


def _coordinator(hass, entry, slugs):
    """A coordinator double with the captured battery controls and the REAL
    ``_per_battery_config`` / ``_finish_manual_battery_mode``."""
    from custom_components.solar_energy_management.const import DOMAIN
    coordinator = MagicMock()
    coordinator.last_update_success = True
    coordinator.config_entry = entry
    coordinator.hass = hass
    coordinator.setup_presence = {m: Presence.PRESENT for m in Module}
    coordinator.config = {**entry.data, **entry.options}
    coordinator.battery_control_slugs = slugs
    coordinator._sensor_reader._energy_dashboard_config.battery_power_list = []
    coordinator._per_battery_config = (
        lambda i, c=1: SEMCoordinator._per_battery_config(coordinator, i, c))
    entry.runtime_data = coordinator
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    return coordinator


async def _selects(hass, entry):
    from custom_components.solar_energy_management import select
    added: list = []
    await select.async_setup_entry(hass, entry, added.extend)
    out = {}
    for ent in added:
        key = ent.entity_description.key
        if key.startswith("battery_") and key.endswith("_mode"):
            ent.hass = hass
            ent.async_write_ha_state = lambda: None
            out[key] = ent
    return out


def _finish(coordinator, idx, count, soc, soc_read=True, bid="primary"):
    SEMCoordinator._finish_manual_battery_mode(
        coordinator, idx, count,
        BatteryRuntime(battery_id=bid, last_known_soc=soc), soc_read)


def _runs(coordinator, idx, count):
    cfg = SEMCoordinator._per_battery_config(coordinator, idx, count)
    return str(cfg.get("battery_mode", "auto") or "auto").lower()


def _shows(select_entity):
    select_entity._handle_coordinator_update()
    return select_entity.current_option


@pytest.mark.skipif(not _HAS_PHACC, reason="pytest-homeassistant-custom-component not installed")
class TestTheModeGoesBackToAuto:
    @pytest.mark.asyncio
    async def test_one_battery_full_goes_back_to_auto(self, hass):
        entry = _entry(hass, {"battery_mode": "auto"})
        coordinator = _coordinator(hass, entry, ())
        selects = await _selects(hass, entry)
        await selects["battery_mode"].async_select_option("force_charge")
        assert _runs(coordinator, 0, 1) == "force_charge"

        _finish(coordinator, 0, 1, soc=97.0)
        assert entry.options["battery_mode"] == "force_charge", (
            "not full yet: the charge goes on")

        _finish(coordinator, 0, 1, soc=100.0)
        assert entry.options["battery_mode"] == "auto"
        assert _runs(coordinator, 0, 1) == "auto"
        assert _shows(selects["battery_mode"]) == "auto", (
            "the select must show the mode SEM runs")

    @pytest.mark.asyncio
    async def test_a_level_sem_cannot_read_keeps_the_mode(self, hass):
        entry = _entry(hass, {"battery_mode": "force_charge"})
        coordinator = _coordinator(hass, entry, ())
        _finish(coordinator, 0, 1, soc=100.0, soc_read=False)
        assert entry.options["battery_mode"] == "force_charge"

    @pytest.mark.asyncio
    async def test_a_sale_goes_back_to_auto_at_the_reserve(self, hass):
        entry = _entry(hass, {"battery_mode": "force_discharge",
                              "battery_reserve_soc": 30})
        coordinator = _coordinator(hass, entry, ())
        selects = await _selects(hass, entry)
        _finish(coordinator, 0, 1, soc=31.0)
        assert entry.options["battery_mode"] == "force_discharge"
        _finish(coordinator, 0, 1, soc=30.0)
        assert entry.options["battery_mode"] == "auto"
        assert _shows(selects["battery_mode"]) == "auto"

    @pytest.mark.asyncio
    async def test_two_batteries_only_the_full_one_moves(self, hass):
        entry = _entry(hass, {"battery_modes": ["force_discharge", "force_charge"],
                              "battery_reserve_socs": [20, 20],
                              # a stale scalar the per-battery controls never read
                              "battery_mode": "force_charge"})
        coordinator = _coordinator(hass, entry, ("b1", "b2"))
        selects = await _selects(hass, entry)
        assert set(selects) == {"battery_b1_mode", "battery_b2_mode"}

        _finish(coordinator, 0, 2, soc=100.0, bid="b1")   # b1 sells: not done
        _finish(coordinator, 1, 2, soc=100.0, bid="b2")   # b2 charges: done
        assert entry.options["battery_modes"] == ["force_discharge", "auto"]
        assert entry.options["battery_mode"] == "force_charge", (
            "the per-battery controls own the mode; the scalar is not theirs")
        assert _runs(coordinator, 0, 2) == "force_discharge"
        assert _runs(coordinator, 1, 2) == "auto"
        assert _shows(selects["battery_b1_mode"]) == "force_discharge"
        assert _shows(selects["battery_b2_mode"]) == "auto"

    @pytest.mark.asyncio
    async def test_a_grid_event_force_charge_never_moves_the_select(self, hass):
        # The VPP override lays force_charge over an Auto battery for the
        # event. Full or not, the user's mode is Auto and is not written.
        entry = _entry(hass, {"battery_mode": "auto"})
        coordinator = _coordinator(hass, entry, ())
        coordinator._vpp_battery_override = {"battery_mode": "force_charge"}
        writes = []
        hass.config_entries.async_update_entry = (
            lambda *a, **kw: writes.append(kw))
        _finish(coordinator, 0, 1, soc=100.0)
        assert writes == []

    @pytest.mark.asyncio
    async def test_the_select_follows_a_mode_written_anywhere(self, hass):
        # The set_option service writes the scalar the same way.
        from custom_components.solar_energy_management import persist_global_option
        entry = _entry(hass, {"battery_mode": "auto"})
        coordinator = _coordinator(hass, entry, ())
        selects = await _selects(hass, entry)
        persist_global_option(hass, entry, coordinator, "battery_mode",
                              "self_consumption")
        assert _shows(selects["battery_mode"]) == "self_consumption"


# ── 3. The pipeline asks, for every battery, before it decides ─────────────

class _Decided(Exception):
    """Raised by the patched decide_battery: the cycle reached the decision."""


def _pipeline_stub():
    stub = MagicMock()
    stub.setup_presence = {m: Presence.PRESENT for m in Module}
    stub._observer_mode = False
    stub._battery_charge_scheduler.enabled = False
    stub.config = {}
    return stub


async def _run_pipeline(monkeypatch, power, stop_at="primary"):
    from custom_components.solar_energy_management.coordinator import (
        actuate_battery as ab,
        decide_battery as db,
    )
    from custom_components.solar_energy_management.coordinator.charger_types import (
        BatteryDecision, BatteryIntent,
    )
    order: list = []
    stub = _pipeline_stub()
    stub._finish_manual_battery_mode = (
        lambda idx, count, runtime, soc_read:
        order.append(("finish", idx, count, runtime.battery_id,
                      runtime.last_known_soc, soc_read)))
    stub._vpp_apply_battery_override = (
        lambda pbc: order.append(("vpp",)) or {})

    def _decide(view):
        order.append(("decide", view.runtime.battery_id))
        if view.runtime.battery_id == stop_at:
            raise _Decided
        return BatteryDecision(battery_id=view.runtime.battery_id,
                               intent=BatteryIntent.OFF)

    async def _actuate(*a, **kw):
        return None

    monkeypatch.setattr(db, "decide_battery", _decide)
    monkeypatch.setattr(ab, "actuate_battery", _actuate)
    with pytest.raises(_Decided):
        await SEMCoordinator._run_battery_pipeline(
            stub, power, SimpleNamespace(), "idle")
    return order


def _single(**kw):
    base = dict(batteries={}, battery_soc=100.0, battery_power=0.0,
                battery_soc_unavailable=False, battery_soc_known=True)
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.mark.asyncio
class TestThePipelineAsksFirst:
    async def test_a_full_battery_is_asked_before_the_cycle_decides(self, monkeypatch):
        order = await _run_pipeline(monkeypatch, _single())
        assert order[0] == ("finish", 0, 1, "primary", 100.0, True)
        assert [o[0] for o in order] == ["finish", "vpp", "decide"], (
            "the mode must be finished before the VPP override is laid over "
            "it and before the cycle decides")

    async def test_a_held_level_is_not_a_reading(self, monkeypatch):
        order = await _run_pipeline(
            monkeypatch, _single(battery_soc_unavailable=True))
        assert order[0][-1] is False

    async def test_a_level_never_read_is_not_a_reading(self, monkeypatch):
        order = await _run_pipeline(
            monkeypatch, _single(battery_soc=0.0, battery_soc_known=False))
        assert order[0][-1] is False

    async def test_each_unit_brings_its_own_level(self, monkeypatch):
        power = _single(batteries={
            "b1": BatteryPower(battery_id="b1", power_w=0.0, soc_pct=None),
            "b2": BatteryPower(battery_id="b2", power_w=0.0, soc_pct=100.0),
        })
        order = await _run_pipeline(monkeypatch, power, stop_at="b2")
        finishes = [o for o in order if o[0] == "finish"]
        # b1's sensor has not answered: its 0.0 is no reading. b2 is full.
        assert finishes == [("finish", 0, 2, "b1", 0.0, False),
                            ("finish", 1, 2, "b2", 100.0, True)]
