"""Pure ``decide_export(fleet) → ExportDecision`` (#955).

The house's meter limit, decided the way SEM's other two control axes are
decided: a pure function of the cycle's inputs. :class:`ExportGuard` (the
tracker) owns hysteresis and "last, not first" — it has already answered
*whether* a write is due this cycle and left its command on the fleet. This
turns that into the intent the seam will write, and nothing else. No hass, no
adapter, no clock.

It exists because the first build of #955 decided IN the coordinator and
dispatched from there: two producers of ``BatteryDecision``, two
``actuate_battery`` call sites, and an observer surface where the two
clobbered each other under one key (found live on .175).

It takes the **fleet**, not a ``BatteryView``. Net house export is owned by no
per-device decider, and reaching for the battery loop's last-assigned view
would make this depend on a loop that only ever runs because a synthetic
``"primary"`` runtime is appended when a house has no configured batteries.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .charger_types import ExportDecision, ExportIntent
from .export_guard import LIMIT_EXPORT, RELEASE_EXPORT

if TYPE_CHECKING:  # pragma: no cover
    from .charger_types import FleetContext

#: The tracker's command vocabulary → this axis's intents. A command the
#: tracker does not issue (``None`` — idle or merely holding) is NONE.
_BY_COMMAND = {LIMIT_EXPORT: ExportIntent.LIMIT, RELEASE_EXPORT: ExportIntent.RELEASE}


def decide_export(fleet: "FleetContext | Any") -> ExportDecision:
    """This cycle's export intent.

    ``NONE`` whenever the guard is off, has issued no command, or is merely
    holding — which is the overwhelmingly common cycle, and on every install
    that has never turned the guard on, the only one.
    """
    if not bool(getattr(fleet, "export_guard_enabled", False)):
        return ExportDecision(reason="export guard off")
    cmd = getattr(fleet, "export_command", None)
    intent = _BY_COMMAND.get(getattr(cmd, "intent", None), ExportIntent.NONE)
    if intent is ExportIntent.NONE:
        return ExportDecision(reason=str(getattr(cmd, "reason", "") or "no export command"))
    return ExportDecision(
        intent=intent,
        watts=float(getattr(cmd, "watts", 0.0) or 0.0),
        reason=str(getattr(cmd, "reason", "")),
        # (#1174) the why the guard carried from the verdict
        reason_code=str(getattr(cmd, "reason_code", "") or ""),
        reason_params=dict(getattr(cmd, "reason_params", None) or {}),
    )
