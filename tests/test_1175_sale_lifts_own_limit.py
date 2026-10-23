"""#1175 — a sale hands back the discharge limit SEM wrote before it.

SEM caps a pack's discharge (LIMIT_DISCHARGE) to keep it for the house: the
0 W cheap-hour hold, the EV clamp at night. That limit stays in the register
until a command writes it again, and only NORMAL did. A sale that came next
(Force discharge, a planned sale) wrote only the sale power, so on a brand
where the limit also caps a forced discharge the sale sent nothing.

Pinned here, per brand: the sale puts the limit back to the pack's max (once,
not every cycle); Huawei does it as the cycle's only command and sells on the
next; a limit SEM never wrote is not touched; a failed hand-back does not
block the sale; Off on an AC-coupled pack hands it back too. A completeness
check makes every new adapter class answer the same question.
"""
from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

from custom_components.solar_energy_management.coordinator import battery_adapters
from custom_components.solar_energy_management.coordinator.actuate_battery import (
    actuate_battery,
)
from custom_components.solar_energy_management.coordinator.battery_adapters.base import (
    BatteryControlAdapter,
)
from custom_components.solar_energy_management.coordinator.battery_adapters.deye import (
    DeyeBatteryAdapter,
)
from custom_components.solar_energy_management.coordinator.battery_adapters.generic import (
    GenericBatteryAdapter,
)
from custom_components.solar_energy_management.coordinator.battery_adapters.goodwe import (
    GoodWeBatteryAdapter,
)
from custom_components.solar_energy_management.coordinator.battery_adapters.huawei import (
    HuaweiBatteryAdapter,
)
from custom_components.solar_energy_management.coordinator.charger_types import (
    BatteryDecision,
    BatteryIntent,
)

LIMIT = "number.battery_max_discharge"
SETPOINT = "number.battery_sale_power"
STRATEGY = "select.battery_strategy"
MAX_W = 5000.0


class _Hass:
    """States that take what is written to them; every call is recorded."""

    def __init__(self, *, limit_w: float = MAX_W, fail: tuple = ()):
        self.calls: list = []
        self.fail = set(fail)
        self._states = {
            LIMIT: self._num(limit_w),
            SETPOINT: self._num(0.0),
            STRATEGY: SimpleNamespace(
                state="nom", attributes={"options": ["nom", "api", "idle", "eco"]}),
        }
        self.states = SimpleNamespace(get=self._states.get,
                                      async_all=lambda *a, **k: [])
        self.services = SimpleNamespace(async_call=self._call)

    @staticmethod
    def _num(w):
        return SimpleNamespace(state=str(w), attributes={
            "unit_of_measurement": "W", "min": 0, "max": MAX_W})

    async def _call(self, domain, service, data=None, **kw):
        data = dict(data or {})
        self.calls.append((domain, service, data))
        if (domain, service, data.get("entity_id")) in self.fail:
            raise RuntimeError("write refused")
        ent = data.get("entity_id")
        if service == "set_value" and ent in self._states:
            self._states[ent] = self._num(float(data["value"]))
        if service == "select_option" and ent in self._states:
            self._states[ent] = SimpleNamespace(
                state=data["option"], attributes=self._states[ent].attributes)

    def value(self, ent) -> float:
        return float(self._states[ent].state)

    def writes_to(self, ent) -> list:
        return [c[2]["value"] for c in self.calls
                if c[1] == "set_value" and c[2].get("entity_id") == ent]


def _generic(hass, **over):
    cfg = {"battery_discharge_control_entity": LIMIT,
           "battery_force_discharge_control_entity": SETPOINT,
           "battery_max_discharge_power": MAX_W}
    cfg.update(over)
    return GenericBatteryAdapter(hass, cfg)


def _ac_coupled(hass):
    return _generic(hass, battery_strategy_control_entity=STRATEGY)


def _goodwe(hass):
    return GoodWeBatteryAdapter(hass, {
        "battery_discharge_control_entity": LIMIT,
        "battery_force_discharge_control_entity": SETPOINT,
        "battery_max_discharge_power": MAX_W})


def _huawei(hass, *, service=True):
    cfg = {"battery_discharge_control_entity": LIMIT,
           "battery_max_discharge_power": MAX_W}
    if service:
        cfg["inverter_device_id"] = "luna"
    else:
        cfg["battery_force_discharge_control_entity"] = SETPOINT
    a = HuaweiBatteryAdapter(hass, cfg)
    a._inverter_device_id = "luna" if service else ""
    return a


def _sales(hass) -> list:
    return [c for c in hass.calls
            if (c[0], c[1]) == ("huawei_solar", "forcible_discharge_soc")
            or (c[1] == "set_value" and c[2].get("entity_id") == SETPOINT
                and c[2]["value"] > 0)]


async def _hold_at_zero(adapter, hass):
    """The night hold: SEM caps the pack at 0 W, as the actuator would."""
    await adapter.command_limit_discharge(0.0)
    assert hass.value(LIMIT) == 0.0
    assert adapter.last_discharge_limit_w == 0.0
    hass.calls.clear()


# ── one-cycle brands: the limit goes back to the max, then the sale ──

@pytest.mark.asyncio
@pytest.mark.parametrize("build", [_generic, _ac_coupled, _goodwe],
                         ids=["generic", "ac_coupled", "goodwe"])
async def test_sale_after_a_zero_hold_lifts_the_limit_first(build):
    hass = _Hass()
    a = build(hass)
    await _hold_at_zero(a, hass)

    await a.command_force_discharge(3000.0, 20.0)

    assert hass.writes_to(LIMIT) == [MAX_W]
    assert hass.value(LIMIT) == MAX_W
    assert a.last_discharge_limit_w == MAX_W
    assert hass.writes_to(SETPOINT)[-1] == 3000.0
    # the limit is back before the sale power goes out
    order = [c[2].get("entity_id") for c in hass.calls if c[1] == "set_value"]
    assert order.index(LIMIT) < len(order) - 1 - order[::-1].index(SETPOINT)
    assert a.last_intent is BatteryIntent.FORCE_DISCHARGE


@pytest.mark.asyncio
@pytest.mark.parametrize("build", [_generic, _ac_coupled, _goodwe],
                         ids=["generic", "ac_coupled", "goodwe"])
async def test_a_standing_sale_writes_the_limit_once(build):
    hass = _Hass()
    a = build(hass)
    await _hold_at_zero(a, hass)
    for _ in range(5):
        await a.command_force_discharge(3000.0, 20.0)
    assert hass.writes_to(LIMIT) == [MAX_W]


@pytest.mark.asyncio
@pytest.mark.parametrize("build", [_generic, _ac_coupled, _goodwe],
                         ids=["generic", "ac_coupled", "goodwe"])
async def test_a_limit_sem_never_wrote_is_left_alone(build):
    """The user's own 1000 W: SEM wrote no limit this lifetime (−1)."""
    hass = _Hass(limit_w=1000.0)
    a = build(hass)
    assert a.last_discharge_limit_w == -1.0
    await a.command_force_discharge(3000.0, 20.0)
    assert hass.writes_to(LIMIT) == []
    assert hass.value(LIMIT) == 1000.0
    assert hass.writes_to(SETPOINT)[-1] == 3000.0


@pytest.mark.asyncio
async def test_a_failed_hand_back_does_not_block_the_sale():
    hass = _Hass(limit_w=0.0, fail={("number", "set_value", LIMIT)})
    a = _generic(hass)
    a._last_discharge_limit_w = 0.0          # the hold landed before
    await a.command_force_discharge(3000.0, 20.0)
    assert hass.writes_to(SETPOINT)[-1] == 3000.0
    assert a.last_intent is BatteryIntent.FORCE_DISCHARGE
    # not recorded as handed back: the next cycle asks again
    assert a.last_discharge_limit_w == 0.0


# ── Huawei: one command per cycle (the LUNA2000 blocks on two) ──

@pytest.mark.asyncio
@pytest.mark.parametrize("service", [True, False], ids=["service", "number"])
async def test_huawei_lifts_the_limit_alone_then_sells_next_cycle(service):
    hass = _Hass()
    a = _huawei(hass, service=service)
    await _hold_at_zero(a, hass)

    await a.command_force_discharge(3000.0, 20.0)       # cycle 1
    assert hass.calls == [("number", "set_value",
                           {"entity_id": LIMIT, "value": MAX_W})]
    assert _sales(hass) == []
    assert a.last_intent is not BatteryIntent.FORCE_DISCHARGE

    hass.calls.clear()
    await a.command_force_discharge(3000.0, 20.0)       # cycle 2
    assert len(_sales(hass)) == 1
    assert hass.writes_to(LIMIT) == []
    assert len(hass.calls) == 1
    assert a.last_intent is BatteryIntent.FORCE_DISCHARGE
    if service:
        assert hass.calls[0][2]["power"] == 3000

    hass.calls.clear()
    await a.command_force_discharge(3000.0, 20.0)       # cycle 3: it stands
    assert hass.writes_to(LIMIT) == []
    if service:
        assert hass.calls == []


@pytest.mark.asyncio
async def test_huawei_sells_at_once_with_no_limit_of_sems():
    hass = _Hass()
    a = _huawei(hass)
    await a.command_normal()                 # the max is SEM's last limit
    hass.calls.clear()
    await a.command_force_discharge(3000.0, 20.0)
    assert len(_sales(hass)) == 1 and len(hass.calls) == 1


@pytest.mark.asyncio
async def test_huawei_failed_hand_back_does_not_block_the_sale():
    hass = _Hass(limit_w=0.0, fail={("number", "set_value", LIMIT)})
    a = _huawei(hass)
    a._last_discharge_limit_w = 0.0
    for _ in range(3):
        await a.command_force_discharge(3000.0, 20.0)
    assert len(_sales(hass)) == 1
    assert a.last_intent is BatteryIntent.FORCE_DISCHARGE


@pytest.mark.asyncio
async def test_huawei_late_hand_back_keeps_the_standing_sale():
    """The hand-back failed on the sale's first cycle and lands later: that
    cycle sends only the limit, and the sale SEM started still stands."""
    hass = _Hass(limit_w=0.0, fail={("number", "set_value", LIMIT)})
    a = _huawei(hass)
    a._last_discharge_limit_w = 0.0
    await a.command_force_discharge(3000.0, 20.0)
    assert len(_sales(hass)) == 1
    hass.fail.clear()
    hass.calls.clear()
    a._last_error = "an earlier error"
    await a.command_force_discharge(3000.0, 20.0)
    assert hass.calls == [("number", "set_value",
                           {"entity_id": LIMIT, "value": MAX_W})]
    assert a.last_intent is BatteryIntent.FORCE_DISCHARGE
    assert a.last_error is None


# ── Off on an AC-coupled pack: hands off, SEM's limit included ──

@pytest.mark.asyncio
async def test_ac_coupled_off_hands_back_sems_limit():
    hass = _Hass()
    a = _ac_coupled(hass)
    await _hold_at_zero(a, hass)
    await a.command_off()
    assert hass.value(LIMIT) == MAX_W
    assert hass.states.get(STRATEGY).state == "idle"
    assert a.last_intent is BatteryIntent.OFF


@pytest.mark.asyncio
async def test_ac_coupled_off_leaves_a_users_limit():
    hass = _Hass(limit_w=1000.0)
    a = _ac_coupled(hass)
    await a.command_off()
    assert hass.writes_to(LIMIT) == []


# ── through the actuator: the decisions as SEM sends them ──

@pytest.mark.asyncio
async def test_actuator_hold_then_planned_sale_lifts_the_limit():
    hass = _Hass()
    a = _generic(hass)
    hold = BatteryDecision(battery_id="b1", intent=BatteryIntent.LIMIT_DISCHARGE,
                           discharge_limit_w=0.0, reason="house sink held")
    sale = BatteryDecision(battery_id="b1", intent=BatteryIntent.FORCE_DISCHARGE,
                           discharge_power_w=2500.0, floor_soc=20.0,
                           reason="plan sell block open")
    await actuate_battery(hold, a)
    assert hass.value(LIMIT) == 0.0
    await actuate_battery(sale, a)
    assert hass.value(LIMIT) == MAX_W
    assert hass.value(SETPOINT) == 2500.0
    assert a._sem_forced_discharge is True


@pytest.mark.asyncio
async def test_actuator_observer_writes_nothing():
    hass = _Hass()
    a = _generic(hass)
    a._last_discharge_limit_w = 0.0
    sale = BatteryDecision(battery_id="b1", intent=BatteryIntent.FORCE_DISCHARGE,
                           discharge_power_w=2500.0, floor_soc=20.0, reason="x")
    await actuate_battery(sale, a, observer=True)
    assert hass.calls == []


# ── Deye writes no discharge limit, so a sale has none to hand back ──

def test_deye_holds_no_limit_to_leak():
    import asyncio
    a = DeyeBatteryAdapter(_Hass(), {"battery_max_discharge_power": MAX_W})
    asyncio.run(a.command_limit_discharge(0.0))
    assert a.last_discharge_limit_w == -1.0
    assert asyncio.run(a._hand_back_own_limit()) is False


# ── every adapter class answers the question ──

#: Each concrete adapter and the test above that pins its sale after a hold.
#: A new brand fails here until it is added, with its own test.
COVERED = {
    GenericBatteryAdapter: test_sale_after_a_zero_hold_lifts_the_limit_first,
    GoodWeBatteryAdapter: test_sale_after_a_zero_hold_lifts_the_limit_first,
    HuaweiBatteryAdapter: test_huawei_lifts_the_limit_alone_then_sells_next_cycle,
    DeyeBatteryAdapter: test_deye_holds_no_limit_to_leak,
}


def _concrete_adapters():
    seen, todo = set(), list(BatteryControlAdapter.__subclasses__())
    while todo:
        cls = todo.pop()
        if cls in seen:
            continue
        seen.add(cls)
        todo.extend(cls.__subclasses__())
    return {c for c in seen
            if not inspect.isabstract(c)
            and c.__module__.startswith(battery_adapters.__name__)}


def test_every_adapter_is_covered():
    assert _concrete_adapters() == set(COVERED)


def test_every_sale_override_hands_back_or_writes_no_limit():
    """An override of the sale must reach the hand-back, or belong to a
    brand that writes no discharge limit at all."""
    for cls in _concrete_adapters():
        own = cls.__dict__.get("command_force_discharge")
        if own is None:
            continue
        src = inspect.getsource(own)
        writes_limit = "_apply_discharge_limit" in inspect.getsource(cls)
        assert ("_hand_back_own_limit" in src
                or "super().command_force_discharge" in src
                or not writes_limit), cls.__name__
