"""#1224 — the Config card saved half a grid power pair, and export read 0 W.

The Detected-hardware rows offered each half of a split grid pair (import,
export) with its own "Use this" button, and one click saved one half. On a
home whose dashboard declares the pair, that half then outranked the
dashboard's own pair: export read 0 W, and only a log line said so.

Pinned here:

* each half's proposal carries the whole pair (``pair``), so one click
  saves both keys in one write — the card side is
  ``dashboard/card/test/pair-write.test.js``;
* a half whose partner is here but cannot take a button (not loaded, wrong
  unit) gets no button either (``pair_blocked``);
* a half the user already saved keeps its partner's offer whole;
* the reader sets half a saved pair aside when the dashboard measures both
  directions (its own pair, its meter sum), not only beside a combined sensor.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from custom_components.solar_energy_management import hardware_detection as hd

from .test_split_grid_integration import (
    _make_energy_dashboard_config,
    _make_reader_with_states,
    _state,
)

IMP_KEY = "grid_import_power_entity"
EXP_KEY = "grid_export_power_entity"


def _ent(entity_id, platform, translation_key, device_id="dev1"):
    return SimpleNamespace(
        entity_id=entity_id, platform=platform, device_id=device_id,
        translation_key=translation_key, unique_id="",
        original_device_class="power", disabled_by=None,
        config_entry_id="entry1")


def _power_state(unit="W"):
    return SimpleNamespace(state="100", attributes={
        "unit_of_measurement": unit, "device_class": "power"})


def _dsmr_pair():
    return [_ent("sensor.dsmr_usage", "dsmr", "current_electricity_usage"),
            _ent("sensor.dsmr_delivery", "dsmr", "current_electricity_delivery")]


@pytest.mark.unit
class TestOneClickSavesTheWholePair:
    def test_each_half_carries_the_whole_pair(self):
        out = hd.propose_roles_from_roster(
            _dsmr_pair(), "dsmr", state_of=lambda e: _power_state())
        imp, exp = out["grid_import_power"], out["grid_export_power"]
        assert imp["action"] == exp["action"] == "set_option"
        assert imp["pair"] == {"sensor.dsmr_usage": {EXP_KEY: ["sensor.dsmr_delivery"]}}
        assert exp["pair"] == {"sensor.dsmr_delivery": {IMP_KEY: ["sensor.dsmr_usage"]}}

    def test_the_pair_lists_every_choice_of_each_half(self):
        """Senec declares two import keys: the runner-up is a choice of the
        import half, and the export half's button may pair with it."""
        ents = [_ent("sensor.senec_imp", "senec", "grid_imported_power"),
                _ent("sensor.senec_imp2", "senec", "gridconsumedpower"),
                _ent("sensor.senec_exp", "senec", "grid_exported_power")]
        out = hd.propose_roles_from_roster(ents, "senec")
        assert out["grid_export_power"]["pair"] == {
            "sensor.senec_exp": {IMP_KEY: ["sensor.senec_imp", "sensor.senec_imp2"]}}
        assert out["grid_import_power"]["pair"] == {
            "sensor.senec_imp": {EXP_KEY: ["sensor.senec_exp"]},
            "sensor.senec_imp2": {EXP_KEY: ["sensor.senec_exp"]}}

    def test_a_role_outside_any_pair_carries_no_pair(self):
        dom = "anker_solix_official"
        ents = [_ent("sensor.a_imp", dom, "grid_import_power"),
                _ent("sensor.a_exp", dom, "grid_export_power"),
                _ent("sensor.a_soc", dom, "battery_soc"),
                _ent("sensor.a_pv", dom, "pv_power")]
        out = hd.propose_roles_from_roster(ents, dom)
        assert {r for r, p in out.items() if "pair" in p} == {
            "grid_import_power", "grid_export_power"}
        assert out["battery_soc"]["action"] == "set_option"


@pytest.mark.unit
class TestAPairIsOneDevice:
    """(review) The Config card asks once per DOMAIN: two meters of one
    integration (two Tibber homes) arrive in one call. Half of each is no
    meter: export of one home minus import of the other."""

    def _two_meters(self):
        return [_ent("sensor.a_usage", "dsmr", "current_electricity_usage", "devA"),
                _ent("sensor.a_delivery", "dsmr", "current_electricity_delivery", "devA"),
                _ent("sensor.b_usage", "dsmr", "current_electricity_usage", "devB"),
                _ent("sensor.b_delivery", "dsmr", "current_electricity_delivery", "devB")]

    def test_each_choice_pairs_only_on_its_own_device(self):
        out = hd.propose_roles_from_roster(self._two_meters(), "dsmr")
        assert out["grid_import_power"]["pair"] == {
            "sensor.a_usage": {EXP_KEY: ["sensor.a_delivery"]},
            "sensor.b_usage": {EXP_KEY: ["sensor.b_delivery"]}}
        assert out["grid_export_power"]["pair"] == {
            "sensor.a_delivery": {IMP_KEY: ["sensor.a_usage"]},
            "sensor.b_delivery": {IMP_KEY: ["sensor.b_usage"]}}

    def test_halves_on_two_devices_are_two_halves(self):
        ents = [_ent("sensor.a_usage", "dsmr", "current_electricity_usage", "devA"),
                _ent("sensor.b_delivery", "dsmr", "current_electricity_delivery", "devB")]
        out = hd.propose_roles_from_roster(ents, "dsmr")
        for role in ("grid_import_power", "grid_export_power"):
            assert out[role]["action"] == "pair_incomplete", role
            assert "pair" not in out[role]

    def test_a_runner_up_that_makes_a_pair_leads_the_row(self):
        """(review) Neither first choice has a partner on its device, but
        home B's two sensors do: they lead the rows, so the button shows."""
        ents = [_ent("sensor.a_usage", "dsmr", "current_electricity_usage", "devA"),
                _ent("sensor.b_usage", "dsmr", "current_electricity_usage", "devB"),
                _ent("sensor.a0_delivery", "dsmr", "current_electricity_delivery", "devC"),
                _ent("sensor.b_delivery", "dsmr", "current_electricity_delivery", "devB")]
        out = hd.propose_roles_from_roster(ents, "dsmr")
        imp, exp = out["grid_import_power"], out["grid_export_power"]
        assert imp["action"] == exp["action"] == "set_option"
        assert (imp["entity"], exp["entity"]) == ("sensor.b_usage", "sensor.b_delivery")
        assert [a["entity"] for a in imp["alternatives"]] == ["sensor.a_usage"]
        assert [a["entity"] for a in exp["alternatives"]] == ["sensor.a0_delivery"]
        assert imp["pair"] == {"sensor.b_usage": {EXP_KEY: ["sensor.b_delivery"]}}

    def test_the_install_form_fills_one_meter(self):
        """(review) The install form took each half's own first choice:
        home A's import beside the export that sits on home B."""
        ents = [_ent("sensor.a_usage", "dsmr", "current_electricity_usage", "devA"),
                _ent("sensor.b_usage", "dsmr", "current_electricity_usage", "devB"),
                _ent("sensor.a_delivery", "dsmr", "current_electricity_delivery", "devB"),
                _ent("sensor.b_delivery", "dsmr", "current_electricity_delivery", "devA")]
        reg = SimpleNamespace(entities={e.entity_id: e for e in ents})
        out = hd.propose_energy_sources(registry=reg)
        device = {e.entity_id: e.device_id for e in ents}
        assert IMP_KEY in out and EXP_KEY in out
        assert device[out[IMP_KEY]["entity"]] == device[out[EXP_KEY]["entity"]]

    def test_the_install_form_fills_no_half_alone(self):
        ents = [_ent("sensor.a_usage", "dsmr", "current_electricity_usage", "devA"),
                _ent("sensor.b_delivery", "dsmr", "current_electricity_delivery", "devB")]
        reg = SimpleNamespace(entities={e.entity_id: e for e in ents})
        out = hd.propose_energy_sources(registry=reg)
        assert IMP_KEY not in out and EXP_KEY not in out

    def test_no_device_proves_no_pair(self):
        ents = [_ent("sensor.dsmr_usage", "dsmr", "current_electricity_usage", None),
                _ent("sensor.dsmr_delivery", "dsmr", "current_electricity_delivery", None)]
        out = hd.propose_roles_from_roster(ents, "dsmr")
        assert out["grid_import_power"]["action"] == "pair_incomplete"
        assert out["grid_export_power"]["action"] == "pair_incomplete"


@pytest.mark.unit
class TestAPartnerThatCannotBeUsedBlocksBothHalves:
    @pytest.mark.parametrize("state_of, partner_action", [
        # registry knows it, HA never produced it
        (lambda e: None if e == "sensor.dsmr_delivery" else _power_state(),
         "not_loaded"),
        # measured in amps
        (lambda e: _power_state("A") if e == "sensor.dsmr_delivery"
         else _power_state(), "unit_mismatch"),
    ])
    def test_no_button_saves_one_half(self, state_of, partner_action):
        out = hd.propose_roles_from_roster(_dsmr_pair(), "dsmr",
                                           state_of=state_of)
        imp, exp = out["grid_import_power"], out["grid_export_power"]
        assert exp["action"] == partner_action
        assert imp["action"] == "pair_blocked"
        assert imp["missing_role"] == ["grid_export_power"]
        assert "pair" not in imp and "pair" not in exp

    def test_a_runner_up_takes_the_same_gate(self):
        """Its button writes it too: one not loaded is no choice, and the
        row says why."""
        ents = [_ent("sensor.senec_imp", "senec", "grid_imported_power"),
                _ent("sensor.senec_imp2", "senec", "gridconsumedpower"),
                _ent("sensor.senec_exp", "senec", "grid_exported_power")]
        out = hd.propose_roles_from_roster(
            ents, "senec",
            state_of=lambda e: None if e == "sensor.senec_imp2" else _power_state())
        alt = out["grid_import_power"]["alternatives"][0]
        assert alt["entity"] == "sensor.senec_imp2"
        assert alt["action"] == "not_loaded"
        assert out["grid_import_power"]["pair"] == {
            "sensor.senec_imp": {EXP_KEY: ["sensor.senec_exp"]}}
        assert out["grid_export_power"]["pair"] == {
            "sensor.senec_exp": {IMP_KEY: ["sensor.senec_imp"]}}

    def test_a_runner_up_outside_any_pair_is_gated_too(self):
        ents = [_ent("sensor.senec_imp", "senec", "grid_imported_power"),
                _ent("sensor.senec_imp2", "senec", "gridconsumedpower"),
                _ent("sensor.senec_exp", "senec", "grid_exported_power")]
        out = hd.propose_roles_from_roster(
            ents, "senec",
            state_of=lambda e: _power_state("A") if e == "sensor.senec_imp2"
            else _power_state())
        alt = out["grid_import_power"]["alternatives"][0]
        assert alt["action"] == "unit_mismatch" and alt["unit_seen"] == "A"

    def test_the_install_form_does_not_prefill_a_blocked_half(self):
        """The install form takes only halves that still offer the button."""
        prop = {"grid_import_power": {"entity": "sensor.dsmr_usage",
                                      "matched_key": "x",
                                      "action": "pair_blocked"}}
        reg = SimpleNamespace(entities={e.entity_id: e for e in _dsmr_pair()})
        orig = hd.propose_roles_from_roster
        try:
            hd.propose_roles_from_roster = lambda *a, **k: prop
            out = hd.propose_energy_sources(registry=reg)
        finally:
            hd.propose_roles_from_roster = orig
        assert IMP_KEY not in out


@pytest.mark.unit
class TestASavedHalfKeepsItsPartnerWhole:
    def test_the_other_half_still_saves_both(self):
        """The import half is saved (an older card, one click): its row is
        gone as "in use", and the export row's button completes the pair."""
        reg = SimpleNamespace(entities={e.entity_id: e for e in _dsmr_pair()})
        out = hd.propose_for_installed(
            reg, configured_entities={"sensor.dsmr_usage"},
            state_of=lambda e: _power_state())
        roles = next(r for r in out if r["domain"] == "dsmr")["proposed_roles"]
        assert "grid_import_power" not in roles
        assert roles["grid_export_power"]["action"] == "set_option"
        assert roles["grid_export_power"]["pair"] == {
            "sensor.dsmr_delivery": {IMP_KEY: ["sensor.dsmr_usage"]}}


def _ed_with_declared_pair():
    ed = _make_energy_dashboard_config(
        solar_power="sensor.pv", grid_import_power=None, battery_power=None)
    ed.grid_power_from = "sensor.ed_import_w"
    ed.grid_power_to = "sensor.ed_export_w"
    return ed


@pytest.mark.unit
class TestTheReaderSetsAHalfPairAside:
    STATES = {
        "sensor.pv": _state(4000),
        "sensor.ed_import_w": _state(0),
        "sensor.ed_export_w": _state(2500),     # exporting 2.5 kW
        "sensor.dsmr_usage": _state(0, device_class="power"),
    }

    def test_the_dashboard_pair_beats_a_saved_half(self):
        reader = _make_reader_with_states(
            MagicMock(), dict(self.STATES), _ed_with_declared_pair(),
            extra_config={IMP_KEY: "sensor.dsmr_usage"})
        power = reader.read_power()
        power.calculate_derived()
        assert power.grid_power == 2500, "export read, not 0 − 0"
        assert power.grid_export_power == 2500
        assert reader._grid_source_key == (
            "ed_pair:sensor.ed_import_w|sensor.ed_export_w")

    def test_an_export_half_alone_also_yields(self):
        reader = _make_reader_with_states(
            MagicMock(), dict(self.STATES), _ed_with_declared_pair(),
            extra_config={EXP_KEY: "sensor.ed_export_w"})
        assert reader.read_power().grid_power == 2500
        assert reader._grid_source_key.startswith("ed_pair:")

    def test_the_dashboard_meter_sum_beats_a_saved_half(self):
        ed = _make_energy_dashboard_config(
            solar_power="sensor.pv", grid_import_power="sensor.meter_a",
            grid_power_list=["sensor.meter_a", "sensor.meter_b"],
            battery_power=None)
        states = {"sensor.pv": _state(0),
                  "sensor.meter_a": _state(-300, device_class="power"),
                  "sensor.meter_b": _state(-200, device_class="power"),
                  "sensor.my_imp": _state(900, device_class="power")}
        reader = _make_reader_with_states(
            MagicMock(), states, ed, extra_config={IMP_KEY: "sensor.my_imp"})
        reader.read_power()
        assert reader._grid_source_key == "sum:2", "both meters, not one"

    def test_a_whole_saved_pair_still_outranks_the_dashboard(self):
        states = dict(self.STATES)
        states["sensor.my_exp"] = _state(1000, device_class="power")
        reader = _make_reader_with_states(
            MagicMock(), states, _ed_with_declared_pair(),
            extra_config={IMP_KEY: "sensor.dsmr_usage",
                          EXP_KEY: "sensor.my_exp"})
        assert reader.read_power().grid_power == 1000
        assert reader._grid_source_key == "manual:sensor.dsmr_usage|sensor.my_exp"

    def test_the_dashboard_sign_check_runs_beside_a_set_aside_half(self):
        """(review) The #589 check asked the saved keys; with the half set
        aside the dashboard pair is read, and its sign is the one to check."""
        reader = _make_reader_with_states(
            MagicMock(), dict(self.STATES), _ed_with_declared_pair(),
            extra_config={IMP_KEY: "sensor.dsmr_usage"})
        reader._audit_autodetect_grid_sign = MagicMock()
        reader.read_power()
        reader._audit_autodetect_grid_sign.assert_called_once()

    def test_a_read_manual_pair_skips_the_dashboard_sign_check(self):
        states = dict(self.STATES)
        states["sensor.my_exp"] = _state(1000, device_class="power")
        reader = _make_reader_with_states(
            MagicMock(), states, _ed_with_declared_pair(),
            extra_config={IMP_KEY: "sensor.dsmr_usage", EXP_KEY: "sensor.my_exp"})
        reader._audit_autodetect_grid_sign = MagicMock()
        reader.read_power()
        reader._audit_autodetect_grid_sign.assert_not_called()

    def test_with_no_whole_answer_the_half_is_still_read(self):
        """A zero-export home: the import half IS the whole story
        (test_split_grid_integration.py::test_manual_import_only)."""
        ed = _make_energy_dashboard_config(
            solar_power="sensor.pv", grid_import_power=None, battery_power=None)
        states = {"sensor.pv": _state(0),
                  "sensor.my_imp": _state(800, device_class="power")}
        reader = _make_reader_with_states(
            MagicMock(), states, ed, extra_config={IMP_KEY: "sensor.my_imp"})
        assert reader.read_power().grid_power == -800


@pytest.mark.unit
class TestTheDiagnosticsSayWhatWasRead:
    def _mode(self, source):
        from .test_620_device_goal_model import TestPublishDiag625
        from custom_components.solar_energy_management.coordinator.publish_diag import (
            build_diagnostics)
        coord = TestPublishDiag625()._coord(config={IMP_KEY: "sensor.dsmr_usage"})
        coord._sensor_reader._grid_source_key = source
        return build_diagnostics(coord)["diag_grid_mode"]

    def test_a_set_aside_half_is_not_the_mode(self):
        assert self._mode("ed_pair:sensor.ed_import_w|sensor.ed_export_w") != "manual"

    def test_a_read_half_is_manual(self):
        assert self._mode("manual:sensor.dsmr_usage|None") == "manual"

    def test_before_the_first_read_the_saved_keys_say_it(self):
        assert self._mode(None) == "manual"
