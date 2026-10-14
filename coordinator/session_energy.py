"""(#1024) How much energy a charging session delivered, and where it came from.

The TOTAL is the charger's own meter whenever one is known; SEM's power
flows only say how that total SPLITS into solar, grid and battery.

PROD, 10 days to 02.10.2026: summing the flow allocations (solar_to_ev +
grid_to_ev + battery_to_ev) recorded 10–31 % less than KEBA's own session
meter — 25.5 kWh where the meter, and its lifetime counter, said 32.0. The
flows do not cover the whole draw: dark or degraded cycles, clamps and the
allocation order all leave watts unassigned. So the flows were the wrong
place to take a total from.

Order of trust, per session:
  1. ``charger_meter``  — the charger's session counter, once it has shown
     it belongs to THIS session (it rose, or it started near zero);
  2. ``lifetime_delta`` — the rise of the charger's lifetime counter;
  3. ``sem_estimate``   — THIS charger's own measured power, integrated.
     Never the flow sum.

A meter that reads unavailable keeps its last good value. A counter that
drops mid-session (a reset) starts a new segment; what the old segment had
counted is kept. A lifetime step the energy calculator judged as kWh no box
drew (#1101) is left out.

Pure: no Home Assistant. ``meter`` is a plain dict the caller persists with
the session, so a restart mid-charge loses nothing.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

#: a drop larger than this is a counter reset, not jitter
RESET_DROP_KWH = 0.01
#: a session counter that first reads below this belongs to a new session
FRESH_SESSION_KWH = 0.05

SOURCE_CHARGER_METER = "charger_meter"
SOURCE_LIFETIME_DELTA = "lifetime_delta"
SOURCE_ESTIMATE = "sem_estimate"


@dataclass(frozen=True)
class SessionTotals:
    energy_kwh: float
    solar_kwh: float
    grid_kwh: float
    battery_kwh: float
    cost: float
    source: str


def seed_from_legacy(meter: dict, energy_kwh: float, solar_kwh: float,
                     grid_kwh: float, battery_kwh: float, cost: float) -> None:
    """A session stored before this module (or restored without its meter
    state) carries totals but no meter history. Treat what it has as
    already counted, so the session never shrinks."""
    if meter.get("seeded"):
        return
    meter["seeded"] = True
    meter.setdefault("flow_solar", float(solar_kwh or 0.0))
    meter.setdefault("flow_grid", float(grid_kwh or 0.0))
    meter.setdefault("flow_battery", float(battery_kwh or 0.0))
    meter.setdefault("flow_cost", float(cost or 0.0))
    meter.setdefault("measured", float(energy_kwh or 0.0))


def _session_meter(meter: dict, value: Optional[float]) -> Optional[float]:
    if value is not None:
        first, last = meter.get("sm_first"), meter.get("sm_last")
        if last is None:
            meter["sm_first"] = meter["sm_last"] = value
        elif value < last - RESET_DROP_KWH:
            # A reset. Keep what the old segment counted — unless it never
            # moved, which means it was the PREVIOUS session's stale value.
            if last > first or first < FRESH_SESSION_KWH:
                meter["sm_offset"] = meter.get("sm_offset", 0.0) + last
            meter["sm_first"] = meter["sm_last"] = value
        else:
            meter["sm_last"] = value
        first, last = meter["sm_first"], meter["sm_last"]
        if last > first or first < FRESH_SESSION_KWH or meter.get("sm_offset", 0.0) > 0:
            meter["sm_trusted"] = True
    if not meter.get("sm_trusted") or meter.get("sm_last") is None:
        return None
    return meter.get("sm_offset", 0.0) + meter["sm_last"]


def _lifetime_meter(meter: dict, value: Optional[float],
                    skip: Optional[Tuple[float, float]] = None) -> Optional[float]:
    if value is not None:
        last = meter.get("lt_last")
        if last is None:
            # The counter already holds what this charger drew before its
            # first readable cycle — SEM measured that much itself.
            meter["lt_last"] = value
            meter["lt_first"] = value - meter.get("measured", 0.0)
        elif value < last - RESET_DROP_KWH:
            meter["lt_offset"] = meter.get("lt_offset", 0.0) + last - meter["lt_first"]
            meter["lt_first"] = meter["lt_last"] = value
        else:
            if (isinstance(skip, tuple) and len(skip) == 2
                    and last <= skip[0] + 0.001):
                # (#1101) A step that stood for kWh no box drew: the session
                # did not get them either.
                meter["lt_first"] += min(skip[1], max(0.0, value - last))
            meter["lt_last"] = value
    if meter.get("lt_last") is None:
        return None
    return meter.get("lt_offset", 0.0) + meter["lt_last"] - meter["lt_first"]


def step(
    meter: dict,
    *,
    solar_kwh: float,
    grid_kwh: float,
    battery_kwh: float,
    cost: float,
    power_w: float,
    hours: float,
    session_meter_kwh: Optional[float],
    lifetime_meter_kwh: Optional[float],
    import_rate: float,
    lifetime_skip: Optional[Tuple[float, float]] = None,
) -> SessionTotals:
    """One cycle. ``*_kwh``/``cost``: this cycle's flow increments and their
    price. ``power_w``: THIS charger's measured draw. Meter readings in kWh,
    None when unreadable this cycle. ``lifetime_skip``: this cycle's step of
    the lifetime counter that no box drew — (reading before it, kWh)."""
    meter["flow_solar"] = meter.get("flow_solar", 0.0) + max(0.0, solar_kwh)
    meter["flow_grid"] = meter.get("flow_grid", 0.0) + max(0.0, grid_kwh)
    meter["flow_battery"] = meter.get("flow_battery", 0.0) + max(0.0, battery_kwh)
    meter["flow_cost"] = meter.get("flow_cost", 0.0) + max(0.0, cost)
    meter["measured"] = meter.get("measured", 0.0) + max(0.0, power_w) * hours / 1000.0

    sm = _session_meter(meter, session_meter_kwh)
    lt = _lifetime_meter(meter, lifetime_meter_kwh, lifetime_skip)
    if sm is not None:
        total, source = sm, SOURCE_CHARGER_METER
    elif lt is not None:
        total, source = lt, SOURCE_LIFETIME_DELTA
    else:
        total, source = meter["measured"], SOURCE_ESTIMATE
    total = max(0.0, total)
    meter["source"] = source

    flows = meter["flow_solar"] + meter["flow_grid"] + meter["flow_battery"]
    if flows > 0:
        scale = total / flows
        return SessionTotals(
            energy_kwh=total,
            solar_kwh=meter["flow_solar"] * scale,
            grid_kwh=meter["flow_grid"] * scale,
            battery_kwh=meter["flow_battery"] * scale,
            cost=meter["flow_cost"] * scale,
            source=source,
        )
    # No flow said where any of it came from: book it as grid, priced as such.
    return SessionTotals(total, 0.0, total, 0.0, total * import_rate, source)
