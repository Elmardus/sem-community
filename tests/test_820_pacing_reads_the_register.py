"""#820 (01.10.2026) — the pacer must trust the register, not its memory.

@ArneGollin1987, pacing on: ``number.battery_max_charge_power_inv_1`` sat at
1560 W for days. It did not move even below 30 % SOC, where SEM should let
the battery charge at full power. SEM's own surface said ``cap_w 2443``.

The chain found in the code:

* the cap went out UNCLAMPED with ``blocking=False``. Home Assistant refuses
  a ``number.set_value`` outside the entity's min/max, and a non-blocking
  call never hears about it;
* the repeat check compared the new cap with ``last_written_w`` — SEM's
  memory of what it sent — so one refused write read as "held" for days;
* below the buffer the release wrote back the value captured at engage. A
  register already at 1560 W was "restored" to 1560 W, so "full power"
  never came.

The register is the truth: clamp to what it can take, compare with what it
holds, say so when it does not take a write, and on release give the pack
its real maximum.
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
from custom_components.solar_energy_management.utils.log_gate import (
    reset_log_gate,
)

ENTITY = "number.battery_max_charge_power_inv_1"


class Register:
    """A number entity that behaves like Home Assistant's: a value inside
    min/max lands on the state, one outside is refused and the state stays.
    ``refuse_all`` stands for a register that takes nothing at all."""

    def __init__(self, value, *, lo=0.0, hi=5000.0, step=1.0, unit="W",
                 refuse_all=False):
        self.state = SimpleNamespace(
            state=str(value),
            attributes={"min": lo, "max": hi, "step": step,
                        "unit_of_measurement": unit})
        self.refuse_all = refuse_all
        self.writes: list[float] = []

    async def call(self, domain, service, data, blocking=False):
        value = float(data["value"])
        self.writes.append(value)
        attrs = self.state.attributes
        if self.refuse_all or not attrs["min"] <= value <= attrs["max"]:
            return  # refused — and with blocking=False nobody hears it
        self.state.state = str(value)

    def hass(self):
        return SimpleNamespace(
            states=SimpleNamespace(get=lambda _eid: self.state),
            services=SimpleNamespace(async_call=AsyncMock(side_effect=self.call)),
        )


def _run(coro):
    return asyncio.run(coro)


class _Clock:
    """(#820, 02.10) The writer's clock: a refusal now needs time as well
    as cycles, and a rewrite waits out the minimum interval."""

    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _clocked():
    w = ChargePacingWriter()
    w._clock = clock = _Clock()
    return w, clock


@pytest.fixture(autouse=True)
def _fresh_log_gate():
    reset_log_gate()
    yield
    reset_log_gate()


class TestTheCapFitsTheRegister:
    def test_arnes_cap_above_the_entity_max_is_clamped_not_lost(self):
        reg = Register(1560, hi=1560)
        w = ChargePacingWriter()
        assert _run(w.apply(reg.hass(), ENTITY, 2443.0, observer=False)) in (
            "wrote", "held")
        assert all(v <= 1560 for v in reg.writes), (
            f"SEM sent {reg.writes} to a register whose max is 1560 W — "
            "Home Assistant refuses that and the cap never lands")

    def test_a_kw_register_gets_kilowatts(self):
        reg = Register(5.0, hi=5.0, step=0.1, unit="kW")
        w = ChargePacingWriter()
        assert _run(w.apply(reg.hass(), ENTITY, 2443.0, observer=False)) == "wrote"
        assert reg.writes == [pytest.approx(2.4)], (
            "2443 sent to a kW register is 2443 kW — refused, or worse")
        assert float(reg.state.state) == pytest.approx(2.4)

    def test_the_cap_rounds_down_to_the_step(self):
        reg = Register(5000, step=100.0)
        w = ChargePacingWriter()
        _run(w.apply(reg.hass(), ENTITY, 2443.0, observer=False))
        assert reg.writes == [2400.0], "a cap is a ceiling: round it down"


class TestTheRegisterIsTheTruth:
    def test_a_write_the_register_never_takes_is_reported(self):
        reg = Register(1560, refuse_all=True)
        w, clock = _clocked()
        h = reg.hass()
        actions = []
        for _ in range(12):
            actions.append(_run(w.apply(h, ENTITY, 2443.0, observer=False)))
            clock.t += 10
        assert actions[0] == "wrote"
        assert actions[-1] == "write_refused", (
            f"{actions}: the register stayed at 1560 W and SEM said 'held' — "
            "that is the #820 report, for days")

    def test_a_refused_write_is_not_repeated_every_cycle(self):
        reg = Register(1560, refuse_all=True)
        w = ChargePacingWriter()
        h = reg.hass()
        for _ in range(10):
            _run(w.apply(h, ENTITY, 2443.0, observer=False))
        assert len(reg.writes) == 1, (
            "re-sending the same refused value every cycle hammers the "
            "inverter's register (#538) and still changes nothing")

    def test_one_slow_cycle_is_not_a_refusal(self):
        """A non-blocking write may land a cycle late on a slow bus."""
        reg = Register(5000)
        w = ChargePacingWriter()
        h = reg.hass()
        real = reg.call

        async def late(domain, service, data, blocking=False):
            reg.writes.append(float(data["value"]))  # not applied yet
        h.services.async_call.side_effect = late
        assert _run(w.apply(h, ENTITY, 2000.0, observer=False)) == "wrote"
        assert _run(w.apply(h, ENTITY, 2000.0, observer=False)) == "held"
        reg.state.state = "2000.0"  # it landed
        h.services.async_call.side_effect = real
        assert _run(w.apply(h, ENTITY, 2000.0, observer=False)) == "held"

    def test_a_cap_the_inverter_dropped_is_written_again(self):
        """The cap landed; later the inverter put its own value back. The
        old dedupe compared with SEM's memory and never noticed."""
        reg = Register(5000)
        w, clock = _clocked()
        h = reg.hass()
        assert _run(w.apply(h, ENTITY, 2000.0, observer=False)) == "wrote"
        assert _run(w.apply(h, ENTITY, 2000.0, observer=False)) == "held"
        reg.state.state = "1560"
        # (#820, 02.10) not at once: at most one write per five minutes
        assert _run(w.apply(h, ENTITY, 2000.0, observer=False)) == "held"
        clock.t += 300
        assert _run(w.apply(h, ENTITY, 2000.0, observer=False)) == "wrote"
        assert reg.writes == [2000.0, 2000.0]

    def test_a_refusal_is_logged_once(self, caplog):
        reg = Register(1560, refuse_all=True)
        w, clock = _clocked()
        h = reg.hass()
        with caplog.at_level(logging.WARNING):
            for _ in range(30):
                _run(w.apply(h, ENTITY, 2443.0, observer=False))
                clock.t += 10
        lines = [r for r in caplog.records
                 if "refused" in r.getMessage() and ENTITY in r.getMessage()]
        assert len(lines) == 1, [r.getMessage() for r in lines]


class TestBelowTheBufferIsFullPower:
    def test_release_gives_the_pack_its_maximum_not_the_old_1560(self):
        """Arne: the register read 1560 W when pacing engaged, so SEM
        captured 1560 as 'the value to put back' — and below 30 % SOC it
        put back 1560."""
        reg = Register(1560)
        w = ChargePacingWriter()
        h = reg.hass()
        _run(w.apply(h, ENTITY, 1200.0, observer=False, hw_max_w=5000.0))
        out = _run(w.apply(h, ENTITY, None, observer=False, hw_max_w=5000.0))
        assert out == "restored"
        assert reg.writes[-1] == 5000.0
        assert float(reg.state.state) == 5000.0

    def test_release_keeps_a_higher_user_value(self):
        reg = Register(9000, hi=10000)
        w = ChargePacingWriter()
        h = reg.hass()
        _run(w.apply(h, ENTITY, 2000.0, observer=False, hw_max_w=5000.0))
        _run(w.apply(h, ENTITY, None, observer=False, hw_max_w=5000.0))
        assert reg.writes[-1] == 9000.0

    def test_release_never_exceeds_what_the_register_takes(self):
        reg = Register(1000, hi=3000)
        w = ChargePacingWriter()
        h = reg.hass()
        _run(w.apply(h, ENTITY, 800.0, observer=False, hw_max_w=5000.0))
        _run(w.apply(h, ENTITY, None, observer=False, hw_max_w=5000.0))
        assert reg.writes[-1] == 3000.0

    def test_a_release_during_a_blip_is_tried_again(self):
        """Review: the register read unavailable at the moment of release.
        SEM must stay engaged, write nothing, and release on the next cycle
        when the register is back — not forget it holds a cap."""
        reg = Register(1560)
        w = ChargePacingWriter()
        h = reg.hass()
        _run(w.apply(h, ENTITY, 1200.0, observer=False, hw_max_w=5000.0))
        good = reg.state
        reg.state = SimpleNamespace(state="unavailable", attributes={})
        out = _run(w.apply(h, ENTITY, None, observer=False, hw_max_w=5000.0))
        assert out == "limit_unreadable"
        assert w.engaged is True
        reg.state = good
        out = _run(w.apply(h, ENTITY, None, observer=False, hw_max_w=5000.0))
        assert out == "restored"
        assert reg.writes[-1] == 5000.0
        assert w.engaged is False

    def test_a_cap_above_the_max_stays_on_the_step_grid(self):
        """A cap is a ceiling: clamped to max 2450 on a 200-step register it
        lands on 2400, a value the step allows. (A release writes the
        entity's own max, which is always allowed.)"""
        reg = Register(1000, hi=2450, step=200)
        w = ChargePacingWriter()
        h = reg.hass()
        _run(w.apply(h, ENTITY, 3000.0, observer=False, hw_max_w=5000.0))
        assert reg.writes[-1] == 2400.0

    def test_sems_own_cap_is_never_captured_as_the_value_to_restore(self):
        """A release the register refused leaves SEM's cap on it. The next
        engagement must not take that cap for the user's setting."""
        reg = Register(5000)
        w = ChargePacingWriter()
        h = reg.hass()
        _run(w.apply(h, ENTITY, 1560.0, observer=False, hw_max_w=5000.0))
        reg.refuse_all = True
        _run(w.apply(h, ENTITY, None, observer=False, hw_max_w=5000.0))
        assert float(reg.state.state) == 1560.0  # the release did not land
        reg.refuse_all = False
        _run(w.apply(h, ENTITY, 1200.0, observer=False, hw_max_w=5000.0))
        assert w.restore_value != 1560.0, (
            "SEM captured its own cap as the user's maximum")
        _run(w.apply(h, ENTITY, None, observer=False, hw_max_w=5000.0))
        assert reg.writes[-1] == 5000.0


# ─── end to end through the coordinator's pacing step ─────────────────────

def _day():
    from datetime import datetime, timedelta
    t0 = datetime(2026, 9, 30, 8, 0)
    return [SimpleNamespace(
        start=t0 + timedelta(hours=i), end=t0 + timedelta(hours=i + 1),
        hours=1.0, soc_kwh=6.0, home_batt_kwh=0.0, solar_w=6800.0,
        cap_override_w=6000.0, grid_committed_w=0.0,
    ) for i in range(8)]


def _coordinator(reg: Register):
    cfg = {
        "battery_charge_pacing_enabled": True,
        "battery_charge_power_limit_entity": ENTITY,
        "battery_max_target_soc": 100.0,
        "inverter_ac_limit_w": 20000.0,
        "battery_max_charge_power_w": 5000.0,
    }
    return SimpleNamespace(
        hass=reg.hass(), config=cfg, data={"battery_soc": 40.0},
        battery_capacity_kwh=19.0,
        _planning_evidence={"forecast_trust_d1": 0.9},
        _observer_mode=False, _charge_pacing_writer=None,
        external_yielding=lambda key: False,
        _today_pacing_ledger=_day,
    )


def _soc(pct):
    from custom_components.solar_energy_management.coordinator.types import (
        PowerReadings,
    )
    return PowerReadings(battery_soc=pct, battery_soc_unavailable=False,
                         battery_soc_known=True, battery_soc_stale_s=None)


@pytest.mark.asyncio
async def test_arnes_day_below_the_buffer_the_pack_gets_full_power():
    """The register sat at 1560 W when pacing engaged. Below the buffer
    SEM must hand the pack its 5000 W, not 'restore' 1560 W."""
    from custom_components.solar_energy_management.coordinator.coordinator import (
        SEMCoordinator,
    )
    reg = Register(1560)
    fake = _coordinator(reg)
    await SEMCoordinator._run_charge_pacing(fake, _soc(70.0))
    assert fake._charge_pacing_state["action"] == "wrote", fake._charge_pacing_state
    assert float(reg.state.state) < 1460.0, "pacing put a real cap on"
    await SEMCoordinator._run_charge_pacing(fake, _soc(25.0))
    assert fake._charge_pacing_state["action"] == "restored"
    assert float(reg.state.state) == 5000.0, (
        f"the register reads {reg.state.state} W below the buffer")


@pytest.mark.asyncio
async def test_a_refused_cap_is_on_the_surface():
    from custom_components.solar_energy_management.coordinator.coordinator import (
        SEMCoordinator,
    )
    reg = Register(1560, refuse_all=True)
    fake = _coordinator(reg)
    await SEMCoordinator._run_charge_pacing(fake, _soc(70.0))
    import time
    clock = _Clock()
    clock.t = time.monotonic()
    fake._charge_pacing_writer._clock = clock
    for _ in range(12):
        clock.t += 10
        await SEMCoordinator._run_charge_pacing(fake, _soc(70.0))
    assert fake._charge_pacing_state["action"] == "write_refused"


def test_an_unload_release_on_an_unreadable_register_keeps_the_record():
    """Review: at unload the register read unavailable. Writing the raw
    watt value could land 1000x off on a kW register. Write nothing, keep
    the record so the next start releases it."""
    from custom_components.solar_energy_management.coordinator.charge_pacing import (
        async_release_pacing,
    )
    reg = Register(1.56, hi=10.0, unit="kW")
    reg.state = SimpleNamespace(state="unavailable", attributes={})
    h = reg.hass()
    store = SimpleNamespace(async_remove=AsyncMock())
    out = _run(async_release_pacing(h, (ENTITY, 1560.0, store), "unload"))
    assert out is None
    assert reg.writes == []
    store.async_remove.assert_not_awaited()
