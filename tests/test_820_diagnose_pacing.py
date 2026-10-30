"""#820 (01.10.2026) — the Diagnose button answers a charge-pacing report.

@ArneGollin1987's ``number.battery_max_charge_power_inv_1`` sat at 1560 W for
days. Two causes were possible, and the download could tell them apart for
neither: it carried the pacing DECISION (cap 2443 W) but nothing about the
register — what it reads, the range and step it accepts, what SEM believed it
held, what it would put back — and the refusal itself is logged by the
inverter's integration, not by SEM, so it never reached SEM's log buffer.
Arne did not know where his Home Assistant log was. The button has to carry
it for him.
"""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

from custom_components.solar_energy_management.coordinator.battery_diag import (
    pacing_actuation_diag,
)
from custom_components.solar_energy_management.coordinator.charge_pacing import (
    ChargePacingWriter,
)
from custom_components.solar_energy_management.utils.log_buffer import (
    SEMLogBuffer,
    attach_foreign_tap,
    written_entities,
)

ENTITY = "number.battery_max_charge_power_inv_1"


def _hass(states=None):
    hass = MagicMock()
    st = dict(states or {})
    hass.states.get = MagicMock(side_effect=lambda eid: st.get(eid))
    return hass


def _register(value="1560", **attrs):
    base = {"min": 10, "max": 9720, "step": 100, "unit_of_measurement": "W"}
    base.update(attrs)
    return SimpleNamespace(state=value, attributes=base)


class _Store:
    def __init__(self, record=None):
        self.record = record

    async def async_load(self):
        return self.record


def _coordinator(writer=None, state=None, config=None):
    return SimpleNamespace(
        _charge_pacing_writer=writer,
        _charge_pacing_state=state,
        config=dict({"battery_charge_power_limit_entity": ENTITY}
                    if config is None else config),
    )


def _run(coro):
    return asyncio.run(coro)


class TestThePayloadAnswersArnesQuestion:
    def test_the_register_and_what_sem_believes_side_by_side(self):
        w = ChargePacingWriter(store=_Store(
            {"entity_id": ENTITY, "restore_value": 1560.0, "cap_w": 2443.0}))
        w.engaged, w.engaged_entity = True, ENTITY
        w.restore_value, w.last_written_w = 1560.0, 2443.0
        w._unconfirmed_cycles = 3
        coord = _coordinator(w, {
            "enabled": True, "cap_w": 2443.0, "action": "write_refused",
            "reason": "paced to land full at day's end", "reason_code": "paced",
            "entity": ENTITY})
        out = _run(pacing_actuation_diag(_hass({ENTITY: _register()}), coord))

        reg = out["register"]
        assert reg["entity"] == ENTITY
        assert reg["state"] == "1560"
        assert (reg["min"], reg["max"], reg["step"]) == (10, 9720, 100)
        assert reg["unit"] == "W"

        writer = out["writer"]
        assert writer["engaged"] is True
        assert writer["restore_value"] == 1560.0
        assert writer["last_written_w"] == 2443.0
        assert writer["confirmed"] is False
        assert writer["unconfirmed_cycles"] == 3

        assert out["decision"]["cap_w"] == 2443.0
        assert out["decision"]["action"] == "write_refused"
        assert out["record"] == {
            "entity_id": ENTITY, "restore_value": 1560.0, "cap_w": 2443.0}

    def test_no_writer_yet_is_said_not_raised(self):
        out = _run(pacing_actuation_diag(
            _hass({ENTITY: _register()}), _coordinator(None, None)))
        assert out["writer"] == {"note": "pacing has not run yet"}
        assert out["register"]["state"] == "1560"
        assert out["record"] is None

    def test_a_missing_register_is_a_value(self):
        out = _run(pacing_actuation_diag(
            _hass({}), _coordinator(ChargePacingWriter(), None)))
        assert out["register"]["state"] == "<missing>"

    def test_no_limit_entity_is_said(self):
        out = _run(pacing_actuation_diag(
            _hass({}), _coordinator(None, None, config={})))
        assert out["register"] == {"entity": None,
                                   "note": "no charge-limit entity is set"}

    def test_it_never_raises(self):
        class _Boom:
            def __getattr__(self, name):
                raise RuntimeError("boom")

        out = _run(pacing_actuation_diag(_hass({}), _Boom()))
        assert "error" in out


class TestForeignWarningsReachTheBuffer:
    def _setup(self, watched):
        buffer = SEMLogBuffer()
        buffer.watch(watched)
        root = logging.getLogger("test_820_root")
        root.setLevel(logging.DEBUG)
        root.propagate = False
        tap = attach_foreign_tap(buffer, logger=root)
        return buffer, root, tap

    def test_a_refusal_naming_the_limit_entity_is_kept(self):
        buffer, root, tap = self._setup({ENTITY})
        try:
            logging.getLogger("test_820_root.homeassistant.core").error(
                "Error executing service: <ServiceCall number.set_value: "
                "entity_id=['%s'], value=2443.0>", ENTITY)
            lines = buffer.get_foreign_lines()
            assert len(lines) == 1
            assert ENTITY in lines[0]
            assert "FOREIGN" in lines[0]
        finally:
            root.removeHandler(tap)

    def test_a_script_warning_carrying_the_entity_in_its_logger_name_is_kept(self):
        """(02.10, mkaiser #654) A template number's set_value is a script
        in mode single; Home Assistant DROPS a second call while one runs and
        logs "Already running" under a logger NAMED after the entity — the
        message never carries the entity id. Arne's foreign log was empty
        while his writes were being dropped."""
        buffer, root, tap = self._setup({"number.battery_max_charge_power_inv_1"})
        try:
            logging.getLogger(
                "test_820_root.homeassistant.helpers.script."
                "battery_max_charge_power_inv_1_set_value").warning(
                "Battery max charge power set_value: Already running")
            lines = buffer.get_foreign_lines()
            assert len(lines) == 1
            assert "FOREIGN" in lines[0] and "Already running" in lines[0]
        finally:
            root.removeHandler(tap)

    def test_another_scripts_already_running_is_not_kept(self):
        buffer, root, tap = self._setup({"number.battery_max_charge_power_inv_1"})
        try:
            logging.getLogger(
                "test_820_root.homeassistant.helpers.script.other_set_value"
            ).warning("Other set_value: Already running")
            assert buffer.get_foreign_lines() == []
        finally:
            root.removeHandler(tap)

    def test_the_logger_match_is_anchored_on_a_segment(self):
        """A watched ``number.inv_1`` must not keep ``other_inv_10_set_value``;
        a segment that IS the object id, or starts with it and ``_``, does."""
        buffer, root, tap = self._setup({"number.inv_1"})
        try:
            logging.getLogger(
                "test_820_root.homeassistant.helpers.script.other_inv_10_set_value"
            ).warning("Other set_value: Already running")
            assert buffer.get_foreign_lines() == []
            logging.getLogger(
                "test_820_root.homeassistant.helpers.script.inv_1_set_value"
            ).warning("Inv 1 set_value: Already running")
            logging.getLogger(
                "test_820_root.homeassistant.helpers.script.inv_1"
            ).warning("Inv 1: Already running")
            assert len(buffer.get_foreign_lines()) == 2
        finally:
            root.removeHandler(tap)

    def test_an_unrelated_warning_is_not_kept(self):
        buffer, root, tap = self._setup({ENTITY})
        try:
            logging.getLogger("test_820_root.other").warning(
                "sensor.kitchen_temp is unavailable")
            assert buffer.get_foreign_lines() == []
        finally:
            root.removeHandler(tap)

    def test_info_is_not_kept(self):
        buffer, root, tap = self._setup({ENTITY})
        try:
            logging.getLogger("test_820_root.other").info(
                "set %s to 2443", ENTITY)
            assert buffer.get_foreign_lines() == []
        finally:
            root.removeHandler(tap)

    def test_sems_own_lines_are_not_doubled(self):
        buffer, root, tap = self._setup({ENTITY})
        try:
            logging.getLogger(
                "test_820_root.custom_components.solar_energy_management.x"
            ).warning("charge pacing: %s refused", ENTITY)
            assert buffer.get_foreign_lines() == []
        finally:
            root.removeHandler(tap)

    def test_the_foreign_lines_are_bounded(self):
        buffer, root, tap = self._setup({ENTITY})
        try:
            log = logging.getLogger("test_820_root.flood")
            for i in range(500):
                log.warning("%s refused %d", ENTITY, i)
            assert len(buffer.get_foreign_lines(1000)) <= 50
        finally:
            root.removeHandler(tap)


class TestWrittenEntities:
    def test_the_writable_entities_come_from_the_config(self):
        config = {
            "battery_charge_power_limit_entity": ENTITY,
            "battery_discharge_control_entity": "number.batteries_max_discharge",
            "grid_power_sensor": "sensor.grid",
            "ev_chargers": [{"current_entity": "number.keba_current",
                             "power_sensor": "sensor.keba_power",
                             "start_stop_entity": "switch.keba_enable"}],
            "batteries": [{"strategy_entity": "select.sessy_strategy"}],
            "empty_entity": "",
        }
        assert written_entities(config) == frozenset({
            ENTITY, "number.batteries_max_discharge", "number.keba_current",
            "switch.keba_enable", "select.sessy_strategy"})

    def test_a_bad_config_is_empty_not_an_error(self):
        assert written_entities(None) == frozenset()
