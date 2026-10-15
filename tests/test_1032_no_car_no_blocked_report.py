"""#1032 — Zaptec's charging switch reads unavailable while no car is
attached. With no car there is nothing to start, so no blocked report."""
from custom_components.solar_energy_management.coordinator.charger_reconciler import (
    ActionKind, ChargerReconciler, DesiredState, ObservedState,
)


def _obs(connected, controllable=False, charging=False):
    return ObservedState(charging=charging, setpoint_a=0, self_charging=False,
                         power_w=0.0, enabled=None,
                         enable_controllable=controllable, connected=connected)


def _kinds(desired, observed, amps=0):
    rec = ChargerReconciler(charger_id="c", heartbeat_s=5.0,
                            idle_disable_threshold=4)
    return {a.kind for a in rec.reconcile(desired, amps, observed, 1000.0)}


def test_no_car_and_unavailable_switch_reports_nothing():
    for desired, amps in ((DesiredState.CHARGE, 16), (DesiredState.OFF, 0)):
        kinds = _kinds(desired, _obs(False, charging=desired is DesiredState.OFF), amps)
        assert ActionKind.REPORT_ENABLE_BLOCKED not in kinds


def test_car_plugged_in_and_unavailable_switch_still_reports():
    for desired, amps, charging in ((DesiredState.CHARGE, 16, False),
                                    (DesiredState.OFF, 0, True)):
        kinds = _kinds(desired, _obs(True, charging=charging), amps)
        assert ActionKind.REPORT_ENABLE_BLOCKED in kinds
