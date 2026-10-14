"""#1112 — the battery mode table and the two battery permissions, as tests.

Each test is one sentence the user-facing docs promise, driven through the
real ``decide_battery`` (and, for the car permission, the real charger
``decide``). The docstring quotes the sentence and names where it lives.
A promise the code does not keep today is marked ``xfail(strict=True)``:
the doc and the code disagree, and one of them has to move.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from custom_components.solar_energy_management.coordinator.actuate_battery import (
    actuate_battery,
)
from custom_components.solar_energy_management.coordinator.battery_adapters.generic import (
    GenericBatteryAdapter,
)
from custom_components.solar_energy_management.coordinator.charger_types import (
    BatteryIntent,
    BatteryRuntime,
    BatteryView,
    ChargerEnergy,
    ChargerPower,
    ChargerView,
    FleetContext,
)
from custom_components.solar_energy_management.coordinator.decide import decide
from custom_components.solar_energy_management.coordinator.decide_battery import (
    decide_battery,
)

USER_GUIDE = "docs/USER_GUIDE.md"
MODES = "Per-Battery Modes (Multi-Battery Systems, v1.7.3)"
SPEND = "Forecast-Led Spending — What the budget may be spent on"

MAX_DISCHARGE_W = 4000.0
MAX_CHARGE_W = 5000.0
SELL = (True, MAX_DISCHARGE_W)   # the plan's sell block is open


def _view(*, mode="auto", soc=80.0, reserve=None, sched=None, perms=None,
          sell=None, forecast_sell=None, forecast_spending=False,
          spendable_kwh=0.0, arbitrage_on=True, ev_connected=False,
          fleet=None, available=True):
    cfg = {
        "battery_mode": mode,
        "battery_max_discharge_power": MAX_DISCHARGE_W,
        "battery_max_charge_power_w": MAX_CHARGE_W,
        "battery_grid_arbitrage_enabled": arbitrage_on,
    }
    if reserve is not None:
        cfg["battery_reserve_soc"] = reserve
    return BatteryView(
        runtime=BatteryRuntime(battery_id="b1", last_known_soc=soc,
                               available=available),
        config=cfg,
        fleet=fleet or FleetContext(),
        charging_state="idle",
        ev_charging=False,
        ev_connected=ev_connected,
        home_consumption_w=500.0,
        scheduler_decision=sched,
        arbitrage_sell=sell,
        forecast_sell=forecast_sell,
        forecast_spending_enabled=forecast_spending,
        battery_spendable_kwh=spendable_kwh,
        battery_permissions=perms,
    )


def _sell_verdict(*, spend=False, floor=10.0):
    """The scheduler's 'sell now' verdict — arbitrage, or the forecast spend."""
    return SimpleNamespace(
        state=SimpleNamespace(value="discharging_arbitrage"),
        discharge_power_w=MAX_DISCHARGE_W, floor_soc=floor,
        reason="sell", should_charge=False,
        from_forecast_spend=spend, from_arbitrage=not spend,
    )


def _sells(d) -> bool:
    return d.intent is BatteryIntent.FORCE_DISCHARGE


# ── the mode table ──────────────────────────────────────────────────────


class TestBatteryModeTable:
    def test_auto_is_sem_normal_management(self):
        """USER_GUIDE.md, "Per-Battery Modes": "Auto | Normal SOC zone strategy
        (Zone 1–4) | Default; SEM manages the battery like usual".

        Untouched, an Auto battery gets SEM's normal command — no forced
        charge, no sale, no hands-off."""
        d = decide_battery(_view(mode="auto"))
        assert d.intent is BatteryIntent.NORMAL

    def test_self_consumption_never_sells_arbitrage(self):
        """USER_GUIDE.md, "Per-Battery Modes": "Self-Consumption | Discharge to
        home only, never export to grid".

        Arbitrage is on, the plan's sell block is open, the price verdict
        says sell — a Self-Consumption battery still does not."""
        sells_in_auto = decide_battery(_view(
            mode="auto", sched=_sell_verdict(), sell=SELL))
        assert _sells(sells_in_auto)   # the same cycle sells in Auto
        d = decide_battery(_view(
            mode="self_consumption", sched=_sell_verdict(), sell=SELL))
        assert not _sells(d)

    def test_self_consumption_never_sells_the_forecast_budget(self):
        """USER_GUIDE.md, "Per-Battery Modes": "Self-Consumption | … never
        export to grid" — the forecast spend is the second road to the grid,
        and it is closed too."""
        kw = dict(sched=_sell_verdict(spend=True), forecast_sell=SELL,
                  forecast_spending=True, spendable_kwh=5.0)
        assert _sells(decide_battery(_view(mode="auto", **kw)))
        d = decide_battery(_view(mode="self_consumption", **kw))
        assert not _sells(d)

    def test_force_charge_charges_to_full(self):
        """USER_GUIDE.md, "Per-Battery Modes": "Force Charge | Charge from grid
        … or solar | Deliberate battery top-up"; consts/battery_modes.py:
        "force_charge — manual FORCE_CHARGE to full now"."""
        d = decide_battery(_view(mode="force_charge", soc=40.0))
        assert d.intent is BatteryIntent.FORCE_CHARGE
        assert d.target_soc == 100.0
        assert d.charge_power_w == MAX_CHARGE_W

    @pytest.mark.xfail(strict=True, reason=(
        "Force Charge charges from the grid whatever the price; the table "
        "says 'from grid (if tariff is cheap)'"))
    def test_force_charge_buys_from_the_grid_only_when_cheap(self):
        """USER_GUIDE.md, "Per-Battery Modes": "Force Charge | Charge from grid
        (if tariff is cheap) or solar".

        Night, no sun, the tariff is EXPENSIVE: the grid is the only source,
        so a promise of "grid only if cheap" means no forced charge now."""
        fleet = FleetContext(solar_w=0.0, home_w=500.0, is_night=True,
                             tariff_level="expensive")
        d = decide_battery(_view(mode="force_charge", soc=40.0, fleet=fleet))
        assert d.intent is not BatteryIntent.FORCE_CHARGE

    def test_force_discharge_runs_at_maximum_rate(self):
        """USER_GUIDE.md, "Per-Battery Modes": "Force Discharge | Discharge at
        maximum rate"."""
        d = decide_battery(_view(mode="force_discharge", soc=80.0, reserve=30))
        assert d.intent is BatteryIntent.FORCE_DISCHARGE
        assert d.discharge_power_w == MAX_DISCHARGE_W

    def test_force_discharge_stops_at_the_reserve(self):
        """USER_GUIDE.md, "Per-Battery Modes": "number.sem_battery_<id>_reserve_soc
        — the SOC this battery will not discharge below"; consts/battery_modes.py:
        "force_discharge — … down to the battery's reserve SOC now"."""
        above = decide_battery(_view(mode="force_discharge", soc=31.0, reserve=30))
        assert above.intent is BatteryIntent.FORCE_DISCHARGE
        assert above.floor_soc == 30
        at = decide_battery(_view(mode="force_discharge", soc=30.0, reserve=30))
        assert at.intent is not BatteryIntent.FORCE_DISCHARGE

    def test_a_reserve_never_set_is_20_percent(self):
        """USER_GUIDE.md, "Per-Battery Modes": "A reserve you never set is
        20 %."  A forced sale stops at 20 % when no reserve was chosen."""
        above = decide_battery(_view(mode="force_discharge", soc=21.0))
        assert above.intent is BatteryIntent.FORCE_DISCHARGE
        assert above.floor_soc == 20.0
        at = decide_battery(_view(mode="force_discharge", soc=20.0))
        assert at.intent is not BatteryIntent.FORCE_DISCHARGE

    def test_force_discharge_never_sells_blind(self):
        """USER_GUIDE.md, "Per-Battery Modes": the reserve is "the SOC this
        battery will not discharge below" — a SOC SEM cannot read cannot be
        shown to sit above it, so no sale."""
        d = decide_battery(_view(mode="force_discharge", soc=80.0, reserve=30,
                                 available=False))
        assert d.intent is not BatteryIntent.FORCE_DISCHARGE

    def test_off_beats_every_other_branch(self):
        """USER_GUIDE.md, "Per-Battery Modes": "Off | SEM ignores this battery".

        A sell verdict, an open sell block and a plugged car — none of them
        moves an Off battery out of hands-off."""
        d = decide_battery(_view(mode="off", sched=_sell_verdict(), sell=SELL,
                                 ev_connected=True, soc=95.0))
        assert d.intent is BatteryIntent.OFF

    @pytest.mark.asyncio
    async def test_off_hands_over_once_then_sends_nothing(self):
        """USER_GUIDE.md, "Per-Battery Modes": "Off | SEM ignores this battery";
        consts/battery_modes.py: "One-time clean handoff on entry …, then SEM
        issues NOTHING — no protection, no scheduler, no arbitrage."

        A battery SEM was selling from is switched to Off: one handoff, then
        ten cycles with nothing on the wire."""
        hass = MagicMock()
        state = Mock()
        state.state = "4000"
        state.attributes = {"min": 0, "max": 10000}
        hass.states.get = Mock(return_value=state)
        hass.services.async_call = AsyncMock(return_value=None)
        adapter = GenericBatteryAdapter(hass, {
            "battery_discharge_control_entity": "number.batt_limit",
            "battery_max_discharge_power": MAX_DISCHARGE_W,
        })
        adapter._last_intent = BatteryIntent.FORCE_DISCHARGE

        view = _view(mode="off", sched=_sell_verdict(), sell=SELL,
                     ev_connected=True)
        await actuate_battery(decide_battery(view), adapter)
        assert adapter.last_intent is BatteryIntent.OFF
        handoff_calls = hass.services.async_call.await_count

        for _ in range(10):
            await actuate_battery(decide_battery(view), adapter)
        assert hass.services.async_call.await_count == handoff_calls


# ── the two permissions ─────────────────────────────────────────────────


class TestBatteryPermissions:
    def test_may_not_sell_never_sells(self):
        """USER_GUIDE.md, "What the budget may be spent on": "Battery may sell
        to the grid — export the budget when the price is high."

        Turned off, the battery does not sell — not on the arbitrage verdict,
        not on the forecast spend, in Auto with arbitrage on."""
        no = {"may_export": False}
        arb = decide_battery(_view(mode="auto", sched=_sell_verdict(),
                                   sell=SELL, perms=no))
        spend = decide_battery(_view(
            mode="auto", sched=_sell_verdict(spend=True), forecast_sell=SELL,
            forecast_spending=True, spendable_kwh=5.0, perms=no))
        assert not _sells(arb)
        assert not _sells(spend)

    def test_unset_permissions_keep_the_old_behaviour(self):
        """USER_GUIDE.md, "What the budget may be spent on": "Both default to
        your current behaviour, so turning forecast spending on does not
        silently grant a permission you never gave."

        Unset sells exactly where the mode alone sold before: Auto follows the
        arbitrage switch, Self-Consumption never sells."""
        def sells(mode, arbitrage_on):
            return _sells(decide_battery(_view(
                mode=mode, sched=_sell_verdict(), sell=SELL, perms=None,
                arbitrage_on=arbitrage_on)))
        assert sells("auto", True) is True
        assert sells("auto", False) is False
        assert sells("self_consumption", True) is False

    def test_may_not_charge_the_car_gives_the_charger_no_assist(self):
        """USER_GUIDE.md, "What the budget may be spent on": "Battery may charge
        the car — use the budget on the EV in the evening."

        Off, the charger is offered no battery watts, in the one mode built to
        spend the pack on the car."""
        def charger(may):
            return decide(ChargerView(
                power=ChargerPower(charger_id="keba", connected=True),
                energy=ChargerEnergy(charger_id="keba"),
                mode="solar_plus_battery",
                config={"ev_min_current": 6, "ev_phases": 3,
                        "ev_voltage": 230, "ev_max_current": 32},
                fleet=FleetContext(
                    solar_w=3500.0, home_w=500.0, battery_soc=95.0,
                    battery_assist_max_power_w=4500.0,
                    battery_assist_min_surplus_w=1200.0,
                    battery_may_assist_ev=may),
            ))
        allowed = charger(True)
        assert allowed.assist_w > 0          # the pack is offered when allowed
        denied = charger(False)
        assert denied.assist_w == 0
        assert denied.budget_w < allowed.budget_w

    def test_may_not_charge_the_car_clamps_the_pack(self):
        """USER_GUIDE.md, "What the budget may be spent on": "Battery may charge
        the car" — off, the battery side holds the pack to the house while a
        car is plugged in, even full and in the sun."""
        fleet = FleetContext(solar_w=5000.0, home_w=500.0, battery_soc=95.0)
        allowed = decide_battery(_view(mode="auto", soc=95.0, ev_connected=True,
                                       fleet=fleet))
        assert allowed.intent is BatteryIntent.NORMAL
        denied = decide_battery(_view(mode="auto", soc=95.0, ev_connected=True,
                                      fleet=fleet,
                                      perms={"may_assist_ev": False}))
        assert denied.intent is BatteryIntent.LIMIT_DISCHARGE
