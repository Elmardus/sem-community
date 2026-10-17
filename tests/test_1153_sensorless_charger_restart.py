"""#1153 — a charger with no plug sensor is started again.

A charger with no plug sensor of its own shows a car only while it draws
more than 500 W (the physics defence, #285+1). When the draw stopped, the
plug confirmation read "no car" three cycles later, the reconciler parked
the box off two cycles after that, and every mode answered "EV
disconnected" from then on: SEM never offered it current again, so the car
waiting on the cable never charged again.

The rule: unknown is not gone. Such a charger's car is unknown unless it
draws (``ChargerPower.plug_known``). Modes run their normal logic, the
reconciler does not park the box, the session is not ended, and the
ChargeStability give-up and backoff bound the offers to a charger that may
be empty.

These tests replay the production chain — confirm the plug, build the
view, decide, the stability layer, the reconciler — cycle by cycle.
"""
from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from custom_components.solar_energy_management.coordinator.build_view import (
    build_charger_view,
)
from custom_components.solar_energy_management.coordinator.charge_stability import (
    FULL_CAR_BACKOFF_S,
    ChargeStability,
)
from custom_components.solar_energy_management.coordinator.charger_reconciler import (
    ActionKind,
    ChargerReconciler,
    desired_from_decision,
    observe,
)
from custom_components.solar_energy_management.coordinator.charger_types import (
    ChargerIntent,
    FleetCycleState,
)
from custom_components.solar_energy_management.coordinator.coordinator import (
    SEMCoordinator,
)
from custom_components.solar_energy_management.coordinator.decide import decide
from custom_components.solar_energy_management.coordinator.ev_availability import (
    has_plug_sensor,
    plan_connectivity,
)
from custom_components.solar_energy_management.coordinator.ev_control import (
    EVControlMixin,
)
from custom_components.solar_energy_management.coordinator.types import (
    PowerFlows,
    SessionData,
)

CID = "wb"
STEP = 30.0


class _Adapter:
    """The adapter surface the stability layer and ``observe`` read."""

    min_current_a = 6
    max_current_a = 16
    handshake_power_w = 500.0

    def __init__(self):
        self.last_intent = None
        self._device = SimpleNamespace(_current_setpoint=0)

    def actual_charging(self, power):
        return float(power.power_w) > 500.0

    def is_self_charging(self, power):
        return False


def _readings(draw_w: float):
    """What the sensor reader publishes for a charger with no plug sensor:
    no per-charger map, and a fleet flag only the physics defence sets."""
    return SimpleNamespace(
        solar_power=9000.0, home_consumption_power=800.0,
        battery_power=0.0, battery_charge_power=0.0,
        battery_discharge_power=0.0, battery_soc=95.0,
        grid_import_power=0.0, grid_export_power=max(0.0, 8200.0 - draw_w),
        ev_power=draw_w, ev_power_per_charger=None,
        ev_connected=draw_w > 500.0, ev_charging=draw_w > 500.0,
        ev_connected_per_charger=None, ev_charging_per_charger=None,
    )


class _Loop:
    """One charger, driven through the production chain."""

    def __init__(self, *, charger_cfg=None, config=None, mode="solar_only"):
        self.charger_cfg = charger_cfg or {"id": CID}
        self.config = {"battery_capacity_kwh": 15, "ev_chargers": [self.charger_cfg],
                       "minimum_solar_power": 1000, **(config or {})}
        self.mode = mode
        self.coord = SEMCoordinator.__new__(SEMCoordinator)
        self.coord.config = self.config
        self.coord._ev_conn_confirmed = {}
        self.coord._ev_conn_streak = {}
        self.adapter = _Adapter()
        self.stability = ChargeStability()
        self.rec = ChargerReconciler(charger_id=CID, heartbeat_s=5.0,
                                     idle_disable_threshold=4)
        self.t = 0.0

    def tick(self, draw_w: float):
        power = _readings(draw_w)
        self.coord._confirm_ev_connection(power)
        view = build_charger_view(
            FleetCycleState(power=power, config=self.config, is_night=False),
            charger_id=CID, charger_cfg=self.charger_cfg, mode=self.mode,
            daily_ev_kwh=0.0, hardware_max_a=16,
        )
        raw = decide(view)
        decision = self.stability.filter(
            raw, view, self.adapter, enable_delay_s=60, disable_delay_s=180,
            now_ts=self.t,
        )
        desired, amps = desired_from_decision(decision)
        actions = self.rec.reconcile(desired, amps,
                                     observe(self.adapter, view.power), now=self.t)
        # The live loop: last_intent follows a charge command and STAYS
        # across a debounced idle (the #610 PROD lesson).
        if decision.intent in (ChargerIntent.CHARGE_AT_AMPS, ChargerIntent.CHARGE_MAX):
            self.adapter.last_intent = decision.intent
            self.adapter._device._current_setpoint = int(decision.commanded_amps or 16)
        self.t += STEP
        return view, raw, decision, [a.kind for a in actions]


def _charge_then_stop(loop: _Loop, *, charging_cycles=10):
    for _ in range(charging_cycles):
        loop.tick(4140.0)


@pytest.mark.unit
class TestTheIssueReplay:
    def test_a_stopped_charger_is_offered_current_again(self):
        loop = _Loop()
        _charge_then_stop(loop)
        out = [loop.tick(0.0) for _ in range(20)]
        views = [o[0] for o in out]
        # The plug confirmation still reads "no car" after three cycles…
        assert views[-1].power.connected is False
        # …but that is not an answer for this charger.
        assert views[-1].power.plug_known is False
        assert views[-1].power.car_absent is False
        # No mode answers "EV disconnected"…
        for _, raw, _, _ in out:
            assert "disconnected" not in raw.reason
        # …and SEM offers a start.
        assert any(d.intent is ChargerIntent.CHARGE_AT_AMPS
                   for _, _, d, _ in out[5:])

    def test_the_box_is_never_parked_off(self):
        loop = _Loop()
        _charge_then_stop(loop)
        kinds = [k for _ in range(60) for k in loop.tick(0.0)[3]]
        assert ActionKind.PARK_OFF not in kinds

    @pytest.mark.parametrize("mode", ["solar_only", "min_plus_solar",
                                      "solar_plus_battery", "solar_plus_cheap"])
    def test_every_surplus_mode_proceeds(self, mode):
        loop = _Loop(mode=mode)
        _charge_then_stop(loop)
        out = [loop.tick(0.0) for _ in range(10)]
        assert all("disconnected" not in raw.reason for _, raw, _, _ in out)
        assert any(d.intent is ChargerIntent.CHARGE_AT_AMPS for _, _, d, _ in out)

    def test_always_max_proceeds(self):
        loop = _Loop(mode="always_max")
        _charge_then_stop(loop)
        _, raw, _, kinds = loop.tick(0.0)
        for _ in range(10):
            _, raw, _, kinds = loop.tick(0.0)
        assert raw.intent is ChargerIntent.CHARGE_MAX
        assert ActionKind.PARK_OFF not in kinds

    def test_offers_to_an_empty_charger_are_bounded(self):
        """Eight hours of surplus and no car: a few short ladders, then at
        most one per backoff period — not a ladder every few minutes."""
        loop = _Loop()
        _charge_then_stop(loop)
        hours = 8
        giveups = 0
        backoff_seen = False
        for _ in range(int(hours * 3600 / STEP)):
            _, _, d, _ = loop.tick(0.0)
            if "did not accept the start" in d.reason:
                giveups += 1
            if "start backoff" in d.reason:
                backoff_seen = True
        assert backoff_seen
        # 3 ladders, then one per backoff (20, 40, 80, 80 … minutes).
        periods = 0
        left = hours * 3600 - 3 * 300
        k = 0
        while left > 0:
            left -= FULL_CAR_BACKOFF_S * min(2 ** k, 4) + 300
            periods += 1
            k += 1
        assert 3 <= giveups <= 3 + periods

    def test_a_car_that_draws_again_ends_the_backoff(self):
        loop = _Loop()
        _charge_then_stop(loop)
        for _ in range(int(2 * 3600 / STEP)):
            loop.tick(0.0)
        assert CID in loop.stability._giveup_streak
        loop.tick(4140.0)
        assert CID not in loop.stability._giveup_streak
        assert CID not in loop.stability._giveup_backoff_until


@pytest.mark.unit
class TestAChargerThatCanSayIsUnchanged:
    def test_an_unplug_with_a_plug_sensor_still_idles_and_parks(self):
        cfg = {"id": CID, "ev_connected_sensor": "binary_sensor.wb_plug"}
        loop = _Loop(charger_cfg=cfg)
        _charge_then_stop(loop)
        out = [loop.tick(0.0) for _ in range(10)]
        assert out[-1][0].power.plug_known is True
        assert out[-1][0].power.car_absent is True
        assert "disconnected" in out[-1][1].reason
        assert ActionKind.PARK_OFF in [k for o in out for k in o[3]]

    def test_the_legacy_flat_plug_sensor_counts(self):
        assert has_plug_sensor({"id": CID}, {"ev_plug_sensor": "binary_sensor.p"})
        assert has_plug_sensor({"id": CID}, {"ev_connected_sensor": "binary_sensor.p"})
        assert not has_plug_sensor({"id": CID}, {})

    def test_the_plan_and_the_view_use_one_test(self):
        """``plan_connectivity`` answers None exactly where the view says
        the plug is not known."""
        power = _readings(0.0)
        for cfg, full in (({"id": CID}, {}),
                          ({"id": CID, "ev_connected_sensor": "b.x"}, {}),
                          ({"id": CID}, {"ev_plug_sensor": "b.x"})):
            view = build_charger_view(
                FleetCycleState(power=power, config=full, is_night=False),
                charger_id=CID, charger_cfg=cfg, mode="solar_only",
                daily_ev_kwh=0.0,
            )
            assert (plan_connectivity(CID, cfg, full, power) is None) is (
                not view.power.plug_known)


def _session_host():
    h = SimpleNamespace()
    h.config = {"update_interval": 30}
    h._boot_monotonic = time.monotonic() - 9999.0
    h._last_ev_connected = True
    h._ev_conn_confirmed = {"": True}
    h._ev_conn_streak = {}
    h._session_data = SessionData(active=True, start_time="2026-10-17T10:00:00+02:00",
                                  energy_kwh=5.0, solar_energy_kwh=5.0)
    h._storage = MagicMock()
    h._ev_device = None
    h._this_charger_power = lambda ev, p: float(getattr(p, "ev_power", 0.0))
    h._energy_calculator = SimpleNamespace(
        _import_rate=0.3, ev_battery_cost_rate=lambda: 0.0)
    return h


@pytest.mark.unit
class TestTheSessionIsNotEndedOnAnUnknown:
    def _run(self, plug_known):
        h = _session_host()
        for _ in range(6):
            p = SimpleNamespace(ev_connected=False, ev_power=0.0,
                                ev_connected_per_charger=None)
            EVControlMixin._confirm_ev_connection(h, p)
            EVControlMixin._update_session_tracking(h, p, PowerFlows(),
                                                    plug_known=plug_known)
        return h

    def test_a_sensorless_stop_keeps_the_session(self):
        h = self._run(plug_known=False)
        assert h._session_data.active is True
        assert not h._storage.add_session_to_history.called
        assert not h._storage.update_lifetime_ev_stats.called

    def test_an_unplug_a_sensor_reports_still_ends_it(self):
        h = self._run(plug_known=True)
        assert h._session_data.active is False
        assert h._storage.update_lifetime_ev_stats.called

    def test_the_coordinator_passes_the_plug_test(self):
        import inspect
        src = inspect.getsource(SEMCoordinator._async_update_data)
        assert "has_plug_sensor(charger_cfg, self.config)" in src
