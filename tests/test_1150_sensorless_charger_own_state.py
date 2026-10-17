"""#1150 — a charger with no state sensor no longer reads the other chargers.

In a multi-charger home a charger with no plug or charging sensor of its own
fell back, in ``build_charger_view``, to the fleet value — true when ANY
charger has a car or charges. So charger 2 counted as charging whenever
charger 1 charged: the redirect veto struck charger 2 for a draw it never
made (#899), the phase planner and sequencer were told it charged
(#804/#1008), and it read connected with no car of its own.

The rule: with more than one charger, a charger without its own sensor does
not take the fleet value. Its car is unknown (``plug_known``, #1153) and its
charging state comes from its own draw only. With a single charger the
fleet value IS its own and nothing changes.
"""
from __future__ import annotations

import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.solar_energy_management.coordinator.build_view import (
    build_charger_view,
)
from custom_components.solar_energy_management.coordinator.charger_types import (
    FleetCycleState,
)
from custom_components.solar_energy_management.coordinator.coordinator import (
    SEMCoordinator,
)
from custom_components.solar_energy_management.coordinator.energy_reclaim import (
    REDIRECT_IMPORT_TOLERANCE_W,
    redirect_strikes,
)
from custom_components.solar_energy_management.coordinator.ev_availability import (
    charger_state,
    fleet_flag_is_own,
    has_plug_sensor,
    plan_connectivity,
)

LEFT = {"id": "left", "ev_connected_sensor": "binary_sensor.left_plug",
        "ev_charging_sensor": "binary_sensor.left_charging",
        "ev_charging_power_sensor": "sensor.left_power"}
RIGHT = {"id": "right", "ev_charging_power_sensor": "sensor.right_power"}
FLEET = {"battery_capacity_kwh": 15, "ev_chargers": [LEFT, RIGHT]}


def _power(*, left_w=7000.0, right_w=0.0):
    """What the reader publishes: the left charger's own sensors in the
    maps, the fleet flags as their OR, each charger's own power."""
    return SimpleNamespace(
        solar_power=9000.0, home_consumption_power=800.0,
        battery_power=0.0, battery_charge_power=0.0,
        battery_discharge_power=0.0, battery_soc=80.0,
        grid_import_power=0.0, grid_export_power=0.0,
        ev_power=left_w + right_w,
        ev_power_per_charger={"left": left_w, "right": right_w},
        ev_connected=True, ev_charging=left_w > 500.0,
        ev_connected_per_charger={"left": True},
        ev_charging_per_charger={"left": left_w > 500.0},
    )


def _view(power, cfg, config=FLEET):
    return build_charger_view(
        FleetCycleState(power=power, config=config, is_night=False),
        charger_id=cfg["id"], charger_cfg=cfg, mode="solar_only",
        daily_ev_kwh=0.0,
    ).power


@pytest.mark.unit
class TestTheViewInAFleet:
    def test_a_sibling_charging_is_not_this_chargers_charge(self):
        """THE bug: the left charger charges, the right one has no sensor
        and draws nothing — it read connected and charging."""
        right = _view(_power(left_w=7000.0, right_w=0.0), RIGHT)
        assert right.charging is False
        assert right.connected is False
        # …and its car is unknown, not gone (#1153): it is still offered.
        assert right.plug_known is False
        assert right.car_absent is False

    def test_its_own_draw_makes_it_charging_and_connected(self):
        right = _view(_power(left_w=0.0, right_w=4140.0), RIGHT)
        assert right.charging is True
        assert right.connected is True
        assert right.plug_known is True

    def test_a_draw_under_the_floor_is_standby_not_a_charge(self):
        right = _view(_power(left_w=7000.0, right_w=140.0), RIGHT)
        assert right.charging is False
        assert right.connected is False

    def test_the_charger_with_its_own_sensors_is_unchanged(self):
        left = _view(_power(left_w=7000.0), LEFT)
        assert left.connected is True
        assert left.charging is True
        assert left.plug_known is True

    def test_no_own_power_reading_is_not_a_charge(self):
        p = _power(left_w=7000.0)
        p.ev_power_per_charger = {"left": 7000.0}
        assert charger_state("right", p, FLEET, "charging") is False
        assert charger_state("right", p, FLEET, "connected") is False


@pytest.mark.unit
class TestASingleChargerKeepsTheFleetValue:
    def test_the_fleet_flag_is_its_own(self):
        single = {"battery_capacity_kwh": 15, "ev_chargers": [RIGHT]}
        p = _power(left_w=0.0, right_w=0.0)
        p.ev_connected_per_charger = {}
        p.ev_charging_per_charger = {}
        p.ev_connected = True
        p.ev_charging = True
        view = _view(p, RIGHT, config=single)
        assert view.connected is True
        assert view.charging is True

    def test_a_config_without_a_charger_list_is_a_single_charger(self):
        assert fleet_flag_is_own({})
        assert fleet_flag_is_own({"ev_chargers": [RIGHT]})
        assert not fleet_flag_is_own(FLEET)


@pytest.mark.unit
class TestTheConsumersTheIssueNames:
    def test_the_redirect_veto_gets_no_strike_from_a_siblings_charge(self):
        """(#899) A strike needs ``charging``; the coordinator passes the
        view's. Before the fix the right charger struck while the left
        charged, and three strikes vetoed its redirect for the plug-in."""
        right = _view(_power(left_w=7000.0, right_w=0.0), RIGHT)
        strikes = 0
        for _ in range(5):
            strikes = redirect_strikes(
                strikes, redirect_w=1500.0,
                grid_import_w=REDIRECT_IMPORT_TOLERANCE_W + 500.0,
                charging=right.charging)
        assert strikes == 0
        src = inspect.getsource(SEMCoordinator._async_update_data)
        assert "charging=bool(view.power.charging)" in src

    def test_the_phase_sequencer_reads_the_views_charging(self):
        from custom_components.solar_energy_management.coordinator.ev_control import (
            EVControlMixin,
        )
        src = inspect.getsource(EVControlMixin)
        assert "charging=cp.charging" in src


@pytest.mark.unit
class TestTheSiblingReadsAreSwept:
    def test_the_flat_plug_keys_answer_only_for_one_charger(self):
        flat = {"ev_plug_sensor": "binary_sensor.plug"}
        assert has_plug_sensor({"id": "a"}, flat)
        assert not has_plug_sensor({"id": "a"}, {**flat, "ev_chargers": [LEFT, RIGHT]})
        assert has_plug_sensor(LEFT, FLEET)

    def test_the_plan_asks_no_fleet_flag_for_a_sensorless_charger(self):
        power = SimpleNamespace(ev_connected=True)
        cfg = {**FLEET, "ev_plug_sensor": "binary_sensor.plug"}
        assert plan_connectivity("right", RIGHT, cfg, power) is None

    async def test_no_notification_for_the_sensorless_charger(self):
        """(#584) The gate's fallback was the fleet flag — "solar charging
        started" on the charger with no car whenever the other charged."""
        coord = MagicMock()
        coord._notification_manager = AsyncMock()
        coord._effective_states_per_charger = {
            "left": ("solar_charging_active", "Left"),
            "right": ("solar_charging_active", "Right"),
        }
        coord._build_per_charger_intelligence = MagicMock(return_value={})
        coord.time_manager.is_night_mode = MagicMock(return_value=False)
        coord.config = {"update_interval": 30, **FLEET}
        coord._ev_devices = {}
        coord._last_ev_connected_per_charger = {}
        power = _power(left_w=7000.0, right_w=0.0)
        power.battery_soc = 80.0
        await SEMCoordinator._send_notifications(
            coord, "idle", power,
            MagicMock(daily_ev=0.0, daily_solar=0.0, daily_home=0.0),
            MagicMock(), MagicMock(current_vs_peak_percentage=0.0, autarky_rate=0.0),
            MagicMock(), MagicMock(forecast_tomorrow_kwh=10.0), 0.0, 6, 4000.0,
        )
        notified = [c.kwargs.get("charger_id") for c in
                    coord._notification_manager.notify_state_change.await_args_list]
        assert notified == ["left"]

    def test_the_session_swap_and_the_night_gate_use_the_same_rule(self):
        from custom_components.solar_energy_management.coordinator.ev_control import (
            EVControlMixin,
        )
        loop = inspect.getsource(SEMCoordinator._async_update_data)
        assert 'charger_state(\n                        cid, power, self.config, "connected")' in loop
        gate = inspect.getsource(EVControlMixin._police_opted_out_charger)
        assert "charger_state(cid, power, _config, \"connected\")" in gate
        assert "plug_known=" in gate
        packs = inspect.getsource(SEMCoordinator._ev_pack_modes)
        assert "charger_state(" in packs

    def test_the_pack_modes_ask_each_chargers_own_car(self):
        """(#1130) A sensorless charger in solar_plus_battery does not
        consent for the pack because its sibling has a car."""
        coord = SEMCoordinator.__new__(SEMCoordinator)
        coord.config = {**FLEET, "ev_chargers": [
            {**LEFT, "charge_mode": "solar_only"},
            {**RIGHT, "charge_mode": "solar_plus_battery"},
        ]}
        coord._effective_charge_mode_for = lambda cfg: cfg["charge_mode"]
        # Only the left car is connected, and its mode does not take the pack.
        assert SEMCoordinator._ev_pack_modes(coord, _power(left_w=7000.0)) == [False]
        # The right charger draws: now its own car consents.
        assert SEMCoordinator._ev_pack_modes(
            coord, _power(left_w=7000.0, right_w=4140.0)) == [False, True]
