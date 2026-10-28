"""#820 follow-up (02.10.2026) — the pacer must be calm on a real inverter.

@ArneGollin1987 ran 2.2.0-beta.6 with the register read-back fix. His
Sungrow, through the mkaiser Modbus package, exposes the charge limit as a
TEMPLATE number (min 10, step 100). Its set_value writes register 33046 with
value/10, and its state is read back from a Modbus sensor a scan later. The
inverter also applies its own value: SEM wanted 1550 W, it reads 1449 W.

Three faults, each a test class below:

1. **Step grid.** The clamp anchored the grid at min=10, so with step 100
   SEM wrote 1410 W and with step 10 it wrote 1151 W. Steps of 100 W mean
   1400 W.
2. **Confirmation.** "Not exactly the cap" was judged refused. A register
   that moved toward the cap, or sits within a step or 10 % of it, TOOK the
   write. Refused means it did not move at all.
3. **Hammering.** The cap was rewritten every few seconds, 1150↔1158. A
   rewrite needs a real difference (more than one step, 100 W or 5 %) and at
   most one every 5 minutes. The release to full power below the buffer
   still goes at once.
"""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.solar_energy_management.coordinator.charge_pacing import (
    ChargePacingWriter,
)
from custom_components.solar_energy_management.coordinator.power_control import (
    clamp_to_entity_range,
)
from custom_components.solar_energy_management.utils.log_gate import (
    reset_log_gate,
)

ENTITY = "number.battery_max_charge_power_inv_1"


class Clock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


class TemplateNumber:
    """Arne's template number: min 10, step 100 (or 10), a write lands on
    the register only on the NEXT read (the Modbus scan), and the inverter
    may apply its own value instead of the one sent."""

    def __init__(self, value, *, lo=10.0, hi=5000.0, step=100.0,
                 applies=None, refuse_all=False):
        self.state = SimpleNamespace(
            state=str(value),
            attributes={"min": lo, "max": hi, "step": step,
                        "unit_of_measurement": "W"})
        self.applies = applies  # value -> what the inverter really takes
        self.refuse_all = refuse_all
        self.pending: float | None = None
        self.writes: list[float] = []

    async def call(self, domain, service, data, blocking=False):
        value = float(data["value"])
        self.writes.append(value)
        if self.refuse_all:
            return
        self.pending = self.applies(value) if self.applies else value

    def scan(self) -> None:
        """The Modbus sensor's next read."""
        if self.pending is not None:
            self.state.state = str(self.pending)
            self.pending = None

    def hass(self):
        return SimpleNamespace(
            states=SimpleNamespace(get=lambda _eid: self.state),
            services=SimpleNamespace(async_call=AsyncMock(side_effect=self.call)),
        )


def _run(coro):
    return asyncio.run(coro)


def _writer(clock):
    w = ChargePacingWriter()
    w._clock = clock
    return w


@pytest.fixture(autouse=True)
def _fresh_log_gate():
    reset_log_gate()
    yield
    reset_log_gate()


class TestTheStepGridStartsAtZero:
    @pytest.mark.parametrize("cap, step, want", [
        (1458.0, 100.0, 1400.0),   # Arne with step 100: SEM wrote 1410
        (1158.0, 10.0, 1150.0),    # Arne with step 10: SEM wrote 1151
        (1550.0, 100.0, 1500.0),
    ])
    def test_a_min_off_the_grid_does_not_move_the_grid(self, cap, step, want):
        attrs = {"min": 10, "max": 5000, "step": step}
        got = clamp_to_entity_range(attrs, cap, 1.0, round_down_to_step=True)
        assert got == pytest.approx(want)

    def test_a_min_on_the_grid_keeps_its_own_grid(self):
        """min 50, step 25: 50 is on the grid of 25, so the grid is the
        same either way — and never a value off the user's step."""
        attrs = {"min": 50, "max": 5000, "step": 25}
        assert clamp_to_entity_range(attrs, 1160.0, 1.0,
                                     round_down_to_step=True) == 1150.0

    def test_a_grid_anchored_at_its_min_when_the_min_is_on_it(self):
        """min 5, step 10 in a kW-scaled entity is not Arne's case; a
        range that STARTS on its own grid (min 100, step 100) is the same
        grid as zero."""
        attrs = {"min": 100, "max": 5000, "step": 100}
        assert clamp_to_entity_range(attrs, 1458.0, 1.0,
                                     round_down_to_step=True) == 1400.0

    def test_a_cap_below_the_first_step_writes_the_entitys_minimum(self):
        """min 10, step 100: nothing on the grid is below 100 but the entity
        takes its min, so a 60 W cap becomes 10 W — the smallest cap it has."""
        attrs = {"min": 10, "max": 5000, "step": 100}
        assert clamp_to_entity_range(attrs, 60.0, 1.0,
                                     round_down_to_step=True) == 10.0

    def test_the_max_is_floored_on_the_zero_grid(self):
        attrs = {"min": 10, "max": 4555, "step": 100}
        assert clamp_to_entity_range(attrs, 9000.0, 1.0,
                                     round_down_to_step=True) == 4500.0

    def test_the_writer_sends_arnes_register_a_value_on_its_step(self):
        clock = Clock()
        reg = TemplateNumber(1560.0)
        w = _writer(clock)
        _run(w.apply(reg.hass(), ENTITY, 1458.0, observer=False))
        assert reg.writes == [1400.0]


class TestATakenWriteIsNotARefusal:
    def test_the_inverter_applies_its_own_value(self):
        """SEM wanted 1550 (sent 1500), the inverter reads 1449."""
        clock = Clock()
        reg = TemplateNumber(2500.0, applies=lambda v: 1449.0)
        w = _writer(clock)
        h = reg.hass()
        assert _run(w.apply(h, ENTITY, 1550.0, observer=False)) == "wrote"
        actions = []
        for _ in range(10):
            reg.scan()
            clock.advance(10)
            actions.append(_run(w.apply(h, ENTITY, 1550.0, observer=False)))
        assert "write_refused" not in actions, actions
        assert len(reg.writes) == 1

    def test_a_drift_of_the_devices_own_is_not_a_taken_write(self):
        """Review: SEM writes 1500, the device drops it, the register drifts
        2000 → 1800 for its own reasons. Moved toward, never in the band:
        pending through the window, then refused — and no fabricated "took
        the write as 1800 W"."""
        clock = Clock()
        reg = TemplateNumber(2000.0, refuse_all=True)
        w = _writer(clock)
        h = reg.hass()
        assert _run(w.apply(h, ENTITY, 1500.0, observer=False)) == "wrote"
        reg.state.state = "1800.0"
        actions = []
        for _ in range(12):
            clock.advance(10)
            actions.append(_run(w.apply(h, ENTITY, 1500.0, observer=False)))
        assert actions[0] == "held" and actions[-1] == "write_refused", actions
        assert w.applied_differs is None
        assert len(reg.writes) == 1

    def test_a_late_entry_into_the_band_is_taken(self):
        clock = Clock()
        reg = TemplateNumber(5000.0, refuse_all=True)
        w = _writer(clock)
        h = reg.hass()
        _run(w.apply(h, ENTITY, 1500.0, observer=False))
        for _ in range(12):
            clock.advance(10)
            out = _run(w.apply(h, ENTITY, 1500.0, observer=False))
        assert out == "write_refused"
        reg.state.state = "1500.0"
        clock.advance(10)
        assert _run(w.apply(h, ENTITY, 1500.0, observer=False)) == "held"

    def test_a_read_back_one_scan_late_is_not_a_refusal(self):
        clock = Clock()
        reg = TemplateNumber(5000.0)
        w = _writer(clock)
        h = reg.hass()
        _run(w.apply(h, ENTITY, 1500.0, observer=False))
        clock.advance(10)
        assert _run(w.apply(h, ENTITY, 1500.0, observer=False)) == "held"
        reg.scan()
        clock.advance(10)
        assert _run(w.apply(h, ENTITY, 1500.0, observer=False)) == "held"

    def test_a_register_that_never_moves_is_refused(self):
        clock = Clock()
        reg = TemplateNumber(1560.0, refuse_all=True)
        w = _writer(clock)
        h = reg.hass()
        _run(w.apply(h, ENTITY, 1000.0, observer=False))
        out = None
        for _ in range(20):
            reg.scan()
            clock.advance(10)
            out = _run(w.apply(h, ENTITY, 1000.0, observer=False))
        assert out == "write_refused"


class TestNoHammering:
    def test_jitter_1150_1158_over_a_minute_is_one_write(self):
        clock = Clock()
        reg = TemplateNumber(5000.0, step=10.0)
        w = _writer(clock)
        h = reg.hass()
        for i in range(12):
            _run(w.apply(h, ENTITY, 1150.0 if i % 2 else 1158.0,
                         observer=False))
            reg.scan()
            clock.advance(5)
        assert len(reg.writes) == 1, reg.writes

    def test_a_real_change_waits_for_the_interval(self):
        clock = Clock()
        reg = TemplateNumber(5000.0)
        w = _writer(clock)
        h = reg.hass()
        _run(w.apply(h, ENTITY, 1500.0, observer=False))
        reg.scan()
        clock.advance(60)
        _run(w.apply(h, ENTITY, 2500.0, observer=False))
        assert reg.writes == [1500.0], "a second write after one minute"
        clock.advance(300)
        assert _run(w.apply(h, ENTITY, 2500.0, observer=False)) == "wrote"
        assert reg.writes == [1500.0, 2500.0]

    def test_a_small_change_is_never_written(self):
        """Inside the deadband: max(step 100, 100 W, 5 %)."""
        clock = Clock()
        reg = TemplateNumber(5000.0)
        w = _writer(clock)
        h = reg.hass()
        _run(w.apply(h, ENTITY, 2000.0, observer=False))
        reg.scan()
        clock.advance(3600)
        _run(w.apply(h, ENTITY, 2090.0, observer=False))
        assert reg.writes == [2000.0]

    def test_a_refused_cap_is_not_retried_with_a_slightly_different_value(self):
        clock = Clock()
        reg = TemplateNumber(1560.0, refuse_all=True)
        w = _writer(clock)
        h = reg.hass()
        for i in range(60):
            _run(w.apply(h, ENTITY, 1000.0 + (i % 3) * 10, observer=False))
            reg.scan()
            clock.advance(10)
        assert len(reg.writes) == 1, reg.writes

    def test_the_release_below_the_buffer_goes_at_once(self):
        clock = Clock()
        reg = TemplateNumber(1560.0)
        w = _writer(clock)
        h = reg.hass()
        _run(w.apply(h, ENTITY, 1200.0, observer=False, hw_max_w=5000.0))
        reg.scan()
        clock.advance(5)
        assert _run(w.apply(h, ENTITY, None, observer=False,
                            hw_max_w=5000.0)) == "restored"
        assert reg.writes == [1200.0, 5000.0]


class TestTheRegisterTookADifferentValue:
    """Arne: SEM wrote 1550 W, the register reads 1449 W. Register 33047 is
    in 0.01 kW and the package divides by 10, and the inverter has its own
    limit — so a value can land as a different number than written. The
    write is TAKEN, and the difference is said, once."""

    def _taken_at(self, wrote, reads):
        clock = Clock()
        reg = TemplateNumber(2500.0, step=10.0, applies=lambda v: reads)
        w = _writer(clock)
        h = reg.hass()
        assert _run(w.apply(h, ENTITY, wrote, observer=False)) == "wrote"
        reg.scan()
        for _ in range(2):  # settled = the same value on two reads
            clock.advance(10)
            assert _run(w.apply(h, ENTITY, wrote, observer=False)) == "held"
        return w, h, reg, clock

    def test_the_difference_is_in_the_writer_state(self):
        w, *_ = self._taken_at(1550.0, 1449.0)
        assert w.applied_differs == (1449.0, 1550.0)

    def test_one_in_band_read_is_not_yet_a_settled_difference(self):
        clock = Clock()
        reg = TemplateNumber(2500.0, step=10.0, applies=lambda v: 1449.0)
        w = _writer(clock)
        h = reg.hass()
        _run(w.apply(h, ENTITY, 1550.0, observer=False))
        reg.scan()
        clock.advance(10)
        assert _run(w.apply(h, ENTITY, 1550.0, observer=False)) == "held"
        assert w.applied_differs is None, "one read is not settled"

    def test_a_value_within_a_step_is_not_a_difference(self):
        w, *_ = self._taken_at(1550.0, 1545.0)
        assert w.applied_differs is None

    def test_the_difference_is_said_once(self, caplog):
        with caplog.at_level(logging.WARNING):
            w, h, reg, clock = self._taken_at(1550.0, 1449.0)
            for _ in range(10):
                clock.advance(10)
                _run(w.apply(h, ENTITY, 1550.0, observer=False))
        lines = [r.getMessage() for r in caplog.records
                 if "took the write as" in r.getMessage()]
        assert len(lines) == 1, lines
        assert "1449 W, not 1550 W" in lines[0]
        assert "unit" in lines[0] and "own limit" in lines[0]

    def test_the_diagnose_button_shows_it(self):
        from custom_components.solar_energy_management.coordinator.battery_diag import (
            pacing_actuation_diag,
        )
        w, h, reg, clock = self._taken_at(1550.0, 1449.0)
        coordinator = SimpleNamespace(
            config={"battery_charge_power_limit_entity": ENTITY},
            _charge_pacing_writer=w, _charge_pacing_state={})
        out = _run(pacing_actuation_diag(h, coordinator))
        assert out["writer"]["applied_differs"] == {
            "register_w": 1449.0, "written_w": 1550.0}

    def test_a_new_write_clears_it(self):
        w, h, reg, clock = self._taken_at(1550.0, 1449.0)
        reg.applies = None
        clock.advance(400)
        assert _run(w.apply(h, ENTITY, 2500.0, observer=False)) == "wrote"
        assert w.applied_differs is None


class TestAStaleReadingIsNoChangeYet:
    """Review pass 2: the Modbus sensor has not scanned since the write, so
    the register still reads the PRE-WRITE value. That is not the hardware
    answering. It only ages the pending window."""

    def test_the_stale_4600_is_pending_until_the_scan_lands(self):
        clock = Clock()
        reg = TemplateNumber(4600.0, hi=10000.0)   # scan() never called
        w = _writer(clock)
        h = reg.hass()
        assert _run(w.apply(h, ENTITY, 5000.0, observer=False)) == "wrote"
        for _ in range(3):
            clock.advance(10)
            assert _run(w.apply(h, ENTITY, 5000.0, observer=False)) == "held"
            assert w._taken is None, "a stale reading is not taken"
            assert w.applied_differs is None, (
                "fabricated: 'took the write as 4600 W' before any scan")
        reg.scan()
        clock.advance(10)
        assert _run(w.apply(h, ENTITY, 5000.0, observer=False)) == "held"
        assert w._taken is True
        assert len(reg.writes) == 1

    def test_stale_through_the_window_is_refused_without_a_diagnosis(self):
        clock = Clock()
        reg = TemplateNumber(4600.0, hi=10000.0, refuse_all=True)
        w = _writer(clock)
        h = reg.hass()
        _run(w.apply(h, ENTITY, 5000.0, observer=False))
        out = None
        for _ in range(12):
            clock.advance(10)
            out = _run(w.apply(h, ENTITY, 5000.0, observer=False))
        assert out == "write_refused"
        assert w.applied_differs is None
        assert len(reg.writes) == 1

    def test_a_stale_reading_inside_the_band_is_still_not_taken(self):
        """4950 → write 5000 (step 100, deadband 250): the pre-write value
        is in the band. Unscanned, it must still not count."""
        clock = Clock()
        reg = TemplateNumber(4700.0, hi=10000.0)
        w = _writer(clock)
        h = reg.hass()
        assert _run(w.apply(h, ENTITY, 5000.0, observer=False)) == "wrote"
        reg.state.state = "4700.0"  # unchanged: no scan
        for _ in range(3):
            clock.advance(10)
            _run(w.apply(h, ENTITY, 5000.0, observer=False))
        assert w._taken is None

    def test_a_value_the_inverter_applied_is_taken_and_not_rewritten(self):
        """sent 5000, the inverter settles at 4600 (changed since the
        pre-write 3000, within 10 %): taken, said once, and NOT rewritten
        every interval."""
        clock = Clock()
        reg = TemplateNumber(3000.0, hi=10000.0, applies=lambda v: 4600.0)
        w = _writer(clock)
        h = reg.hass()
        _run(w.apply(h, ENTITY, 5000.0, observer=False))
        reg.scan()
        for _ in range(12):
            clock.advance(300)
            assert _run(w.apply(h, ENTITY, 5000.0, observer=False)) == "held"
        assert w.applied_differs == (4600.0, 5000.0)
        assert len(reg.writes) == 1


class TestArnesSequenceEndToEnd:
    """Decision (02.10): write 1550 → reads 1449 is TAKEN. Then the
    register is left alone until the cap really moves or someone else
    moves the register."""

    def test_a_register_already_within_the_deadband_is_left_alone(self):
        """1560 on the register, cap 1550: ten watts apart, inside the
        deadband — nothing to write. (The sequence below starts from 1700
        so that the first write is a real one.)"""
        clock = Clock()
        reg = TemplateNumber(1560.0, step=10.0)
        w = _writer(clock)
        assert _run(w.apply(reg.hass(), ENTITY, 1550.0, observer=False)) == "held"
        assert reg.writes == []

    def test_the_sequence(self):
        clock = Clock()
        reg = TemplateNumber(1700.0, step=10.0, applies=lambda v: 1449.0)
        w = _writer(clock)
        h = reg.hass()
        assert _run(w.apply(h, ENTITY, 1550.0, observer=False)) == "wrote"
        reg.scan()
        for _ in range(2):
            clock.advance(10)
            assert _run(w.apply(h, ENTITY, 1550.0, observer=False)) == "held"
        assert w._taken is True
        assert w.applied_differs == (1449.0, 1550.0)
        # 10 more cycles over 30 minutes, cap unchanged: no further write
        for _ in range(10):
            clock.advance(180)
            assert _run(w.apply(h, ENTITY, 1550.0, observer=False)) == "held"
        assert reg.writes == [1550.0]
        # the cap moves to 1700: one write
        reg.applies = None
        clock.advance(10)
        assert _run(w.apply(h, ENTITY, 1700.0, observer=False)) == "wrote"
        reg.scan()
        for _ in range(3):
            clock.advance(120)
            assert _run(w.apply(h, ENTITY, 1700.0, observer=False)) == "held"
        assert reg.writes == [1550.0, 1700.0]
        # someone sets the register to 2000 by hand: one write
        reg.state.state = "2000.0"
        clock.advance(10)
        assert _run(w.apply(h, ENTITY, 1700.0, observer=False)) == "wrote"
        reg.scan()
        for _ in range(3):
            clock.advance(120)
            assert _run(w.apply(h, ENTITY, 1700.0, observer=False)) == "held"
        assert reg.writes == [1550.0, 1700.0, 1700.0]


class FakeStore:
    def __init__(self, data=None):
        self.data = data

    async def async_load(self):
        return self.data

    async def async_save(self, data):
        self.data = dict(data)

    async def async_remove(self):
        self.data = None


class TestAdoptionProvesTheRegister:
    """Review 3: a cap from disk is not a cap on the wire. The register may
    have been moved while SEM was down, or while it was observing."""

    def _lifetime_one(self):
        """Write 1550, the inverter settles at 1449, taken + settled,
        persisted."""
        clock = Clock()
        store = FakeStore()
        reg = TemplateNumber(1700.0, step=10.0, applies=lambda v: 1449.0)
        w = ChargePacingWriter(store=store)
        w._clock = clock
        h = reg.hass()
        assert _run(w.apply(h, ENTITY, 1550.0, observer=False)) == "wrote"
        reg.scan()
        for _ in range(2):
            clock.advance(10)
            assert _run(w.apply(h, ENTITY, 1550.0, observer=False)) == "held"
        assert store.data["cap_w"] == 1550.0
        assert store.data["accepted_w"] == 1449.0
        assert store.data["applied_differs"] == [1449.0, 1550.0]
        return store, reg

    def test_the_record_carries_the_accepted_value(self):
        self._lifetime_one()

    def test_a_register_moved_while_sem_was_down_is_rewritten_at_once(self):
        store, reg = self._lifetime_one()
        reg.applies = None
        reg.state.state = "500.0"           # moved while SEM was down
        clock = Clock(5000.0)
        w = ChargePacingWriter(store=store)
        w._clock = clock
        h = reg.hass()
        assert _run(w.apply(h, ENTITY, 1550.0, observer=False)) == "wrote"
        assert reg.writes[-1] == 1550.0
        assert w._taken is None, "a write on the wire is not yet proven"
        reg.scan()
        clock.advance(10)
        assert _run(w.apply(h, ENTITY, 1550.0, observer=False)) == "held"
        assert w._taken is True
        assert len(reg.writes) == 2

    def test_a_register_at_the_persisted_accepted_value_is_taken_without_a_write(self):
        store, reg = self._lifetime_one()
        n = len(reg.writes)
        clock = Clock(5000.0)
        w = ChargePacingWriter(store=store)
        w._clock = clock
        h = reg.hass()
        assert _run(w.apply(h, ENTITY, 1550.0, observer=False)) == "held"
        assert w._taken is True
        assert w.applied_differs == (1449.0, 1550.0)
        assert len(reg.writes) == n

    def test_a_persisted_difference_is_not_said_again(self, caplog):
        store, reg = self._lifetime_one()
        caplog.clear()                       # lifetime one said it once
        clock = Clock(5000.0)
        w = ChargePacingWriter(store=store)
        w._clock = clock
        h = reg.hass()
        with caplog.at_level(logging.WARNING):
            for _ in range(3):
                clock.advance(10)
                _run(w.apply(h, ENTITY, 1550.0, observer=False))
        assert not [r for r in caplog.records
                    if "took the write as" in r.getMessage()]

    def test_observer_on_then_off_with_the_register_moved_meanwhile(self):
        store, reg = self._lifetime_one()
        clock = Clock(5000.0)
        w = ChargePacingWriter(store=store)
        w._clock = clock
        h = reg.hass()
        assert _run(w.apply(h, ENTITY, 1550.0, observer=False)) == "held"
        n = len(reg.writes)
        # observer on: SEM stands down without forgetting (#949)
        assert _run(w.apply(h, ENTITY, None, observer=True)) == "observer"
        reg.applies = None
        reg.state.state = "500.0"           # moved while SEM observed
        clock.advance(60)
        assert _run(w.apply(h, ENTITY, 1550.0, observer=False)) == "wrote"
        assert len(reg.writes) == n + 1

    def test_an_old_record_without_an_accepted_value_still_proves(self):
        """A record from before this change: no accepted value. Out of band
        on the first reading means moved by someone — one write."""
        store = FakeStore({"entity_id": ENTITY, "restore_value": 5000.0,
                           "cap_w": 1550.0})
        reg = TemplateNumber(500.0, step=10.0)
        w = ChargePacingWriter(store=store)
        w._clock = Clock()
        h = reg.hass()
        assert _run(w.apply(h, ENTITY, 1550.0, observer=False)) == "wrote"
        assert reg.writes == [1550.0]


class TestYieldedWritesNothing:
    """(#1079 review) ruflo found this call had no yield parameter at all —
    a battery yielded to another party kept being paced regardless. Checked
    first, before adopt, so pacing's own state is untouched either way."""

    def test_yielded_makes_no_write_while_engaged(self):
        store, reg = TestAdoptionProvesTheRegister()._lifetime_one()
        n = len(reg.writes)
        clock = Clock(5000.0)
        w = ChargePacingWriter(store=store)
        w._clock = clock
        h = reg.hass()
        assert _run(w.apply(h, ENTITY, 1550.0, observer=False,
                            yielded=True)) == "yielded"
        assert len(reg.writes) == n

    def test_yielded_makes_no_write_on_a_fresh_writer(self):
        reg = TemplateNumber(500.0, step=10.0)
        w = ChargePacingWriter(store=FakeStore())
        w._clock = Clock()
        h = reg.hass()
        assert _run(w.apply(h, ENTITY, 1550.0, observer=False,
                            yielded=True)) == "yielded"
        assert reg.writes == []
        assert w.engaged is False, "yield must not touch pacing's own state"


class TestTheUnloadReleaseReadsTheRecord:
    """Review 4: a writer that saw only observer cycles never adopts the
    record. The unload must still hand the register back — from the
    record, not from the writer's memory."""

    def _prior_engagement(self):
        store, reg = TestAdoptionProvesTheRegister()._lifetime_one()
        assert store.data["restore_value"] == 1700.0
        return store, reg

    def _observer_only_lifetime(self, store, reg, cycles=3):
        w = ChargePacingWriter(store=store)
        w._clock = Clock(5000.0)
        h = reg.hass()
        n = len(reg.writes)
        for _ in range(cycles):
            assert _run(w.apply(h, ENTITY, 1550.0, observer=True)) == "observer"
        assert len(reg.writes) == n, "an observer cycle wrote"
        assert w._unconfirmed_cycles == 0 and w._taken is None
        assert w.engaged is False
        return w, h

    def test_an_observer_only_lifetime_still_releases_from_the_record(self):
        from custom_components.solar_energy_management.coordinator.charge_pacing import (
            async_pending_pacing_release, async_release_pacing,
        )
        store, reg = self._prior_engagement()
        w, h = self._observer_only_lifetime(store, reg)
        coordinator = SimpleNamespace(
            _charge_pacing_writer=w,
            config={"battery_max_charge_power_w": 5000.0})
        held = _run(async_pending_pacing_release(coordinator))
        assert held == (ENTITY, 5000.0, store), "max(restore 1700, hw max 5000)"
        said = _run(async_release_pacing(h, held, "disabled"))
        assert said and "5000" in said
        assert reg.writes[-1] == 5000.0
        assert store.data is None, "the record is dropped with the release"

    def test_no_record_means_nothing_to_release(self):
        from custom_components.solar_energy_management.coordinator.charge_pacing import (
            async_pending_pacing_release,
        )
        reg = TemplateNumber(1700.0, step=10.0)
        w, h = self._observer_only_lifetime(FakeStore(), reg)
        assert _run(async_pending_pacing_release(SimpleNamespace(
            _charge_pacing_writer=w, config={}))) is None

    def test_a_record_with_nothing_to_put_back_is_kept(self):
        from custom_components.solar_energy_management.coordinator.charge_pacing import (
            async_pending_pacing_release,
        )
        store = FakeStore({"entity_id": ENTITY, "restore_value": None,
                           "cap_w": 1550.0})
        w = ChargePacingWriter(store=store)
        assert _run(async_pending_pacing_release(SimpleNamespace(
            _charge_pacing_writer=w, config={}))) is None
        assert store.data is not None

    def test_no_cycle_at_all_reads_the_record_through_the_store_factory(self):
        from custom_components.solar_energy_management.coordinator.charge_pacing import (
            async_pending_pacing_release,
        )
        store, reg = self._prior_engagement()
        held = _run(async_pending_pacing_release(SimpleNamespace(
            _charge_pacing_writer=None, _charge_pacing_store=lambda: store,
            config={"battery_max_charge_power_w": 5000.0})))
        assert held == (ENTITY, 5000.0, store)

    def test_the_memory_path_still_wins_when_engaged(self):
        from custom_components.solar_energy_management.coordinator.charge_pacing import (
            async_pending_pacing_release,
        )
        store, reg = self._prior_engagement()
        w = ChargePacingWriter(store=store)
        w._clock = Clock(5000.0)
        h = reg.hass()
        assert _run(w.apply(h, ENTITY, 1550.0, observer=False)) == "held"
        assert w.engaged
        held = _run(async_pending_pacing_release(SimpleNamespace(
            _charge_pacing_writer=w, config={"battery_max_charge_power_w": 5000.0})))
        assert held == (ENTITY, 5000.0, store)


class TestAReleaseNeverHandsBackSemsOwnCap:
    """Review 5: a record from before #949 can carry SEM's own cap as the
    value to restore. Adoption corrects that in memory; the correction must
    reach the disk, and a release from the disk must apply the same rule."""

    def _own_cap_record(self):
        return FakeStore({"entity_id": ENTITY, "restore_value": 1550.0,
                          "cap_w": 1550.0, "accepted_w": 800.0,
                          "applied_differs": None})

    def test_the_repro_releases_nothing_without_a_hardware_max(self):
        from custom_components.solar_energy_management.coordinator.charge_pacing import (
            async_pending_pacing_release,
        )
        store = self._own_cap_record()
        reg = TemplateNumber(800.0, step=10.0)
        w = ChargePacingWriter(store=store)      # observer-only, never adopted
        _run(w.apply(reg.hass(), ENTITY, 1550.0, observer=True))
        assert _run(async_pending_pacing_release(SimpleNamespace(
            _charge_pacing_writer=w, config={}))) is None

    def test_the_repro_releases_the_hardware_max_when_there_is_one(self):
        from custom_components.solar_energy_management.coordinator.charge_pacing import (
            async_pending_pacing_release,
        )
        store = self._own_cap_record()
        w = ChargePacingWriter(store=store)
        assert _run(async_pending_pacing_release(SimpleNamespace(
            _charge_pacing_writer=w,
            config={"battery_max_charge_power_w": 5000.0}))) == (
            ENTITY, 5000.0, store)

    def test_the_adopt_correction_is_on_disk_after_the_first_held_cycle(self):
        store = self._own_cap_record()
        reg = TemplateNumber(1550.0, step=10.0)
        w = ChargePacingWriter(store=store)
        w._clock = Clock()
        assert _run(w.apply(reg.hass(), ENTITY, 1550.0, observer=False)) == "held"
        assert w.restore_value is None
        assert store.data["restore_value"] is None
        assert store.data["cap_w"] == 1550.0

    def test_engaged_with_nothing_to_restore_does_not_read_the_disk(self):
        """The sentinel path: memory says "engaged, nothing to restore"; a
        stale disk copy must not be consulted instead."""
        from custom_components.solar_energy_management.coordinator.charge_pacing import (
            async_pending_pacing_release, pending_pacing_release,
        )
        store = self._own_cap_record()
        reg = TemplateNumber(1550.0, step=10.0)
        w = ChargePacingWriter(store=store)
        w._clock = Clock()
        _run(w.apply(reg.hass(), ENTITY, 1550.0, observer=False))
        assert w.engaged and w.restore_value is None
        store.data = {"entity_id": ENTITY, "restore_value": 1550.0,
                      "cap_w": 1550.0}             # a stale disk copy
        assert pending_pacing_release(SimpleNamespace(
            _charge_pacing_writer=w)) == (ENTITY, None, store)
        assert _run(async_pending_pacing_release(SimpleNamespace(
            _charge_pacing_writer=w, config={}))) is None
        assert _run(async_pending_pacing_release(SimpleNamespace(
            _charge_pacing_writer=w,
            config={"battery_max_charge_power_w": 5000.0}))) == (
            ENTITY, 5000.0, store)

    def test_a_sentinel_handed_to_the_release_puts_nothing_back(self):
        from custom_components.solar_energy_management.coordinator.charge_pacing import (
            async_release_pacing,
        )
        store = self._own_cap_record()
        reg = TemplateNumber(1550.0, step=10.0)
        assert _run(async_release_pacing(reg.hass(), (ENTITY, None, store),
                                         "disabled")) is None
        assert reg.writes == [] and store.data is not None
