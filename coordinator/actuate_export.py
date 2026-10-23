"""Pure-dispatch ``actuate_export(decision, adapter)`` (#955).

The seam for the house's meter limit, mirroring :func:`actuate_battery`: one
intent, one adapter method, no branch on brand. Observer mode cuts the trigger
here and records what SEM WOULD command.

**The key belongs to this seam.** The first build borrowed the battery's
(``battery:<id>``), so the export cut and the battery's own discharge decision
overwrote each other every cycle and the cut was invisible on the rig — found
live on .175 with the compressed sun sim, for a feature whose whole promise is
that it is holding the meter shut (#855: a case is judged on what would hit
the wire, so the wire must be readable).

Returns the refusal text when the write could not happen (no adapter, a brand
with no export control, an adapter that raised), else ``None``. The caller
hands that to the guard; the seam knows nothing about hysteresis. Observer mode
never refuses: nothing was attempted, so nothing was declined.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

from .charger_types import ExportIntent
from .decision_log import log_of, record_export
from ..utils.log_gate import log_on_change

if TYPE_CHECKING:  # pragma: no cover
    from .battery_adapters.base import BatteryControlAdapter
    from .charger_types import ExportDecision

_LOGGER = logging.getLogger(__name__)

#: The one key this axis publishes under. A house has one meter.
OBSERVER_KEY = "export_guard"


#: Guard states that mean "SEM is holding the meter shut RIGHT NOW". A guard
#: merely counting up to its engage hold has written nothing, so it says
#: nothing — the surface answers "what would hit the wire", not "what am I
#: thinking about".
_STANDING = ("engaged", "releasing", "refused")


async def actuate_export(decision: "ExportDecision",
                         adapter: "Optional[BatteryControlAdapter]", *,
                         observer: bool = False,
                         controller=None,
                         standing: Optional[str] = None,
                         withheld: Optional[list] = None) -> Optional[str]:
    """Apply this cycle's export decision. See the module docstring.

    ``withheld`` (observer only): a list the caller owns for THIS cycle; the
    seam appends the adapter's dry-run row — the exact service + payload the
    write would have been, or the refusal in the verb's own words. #855's
    contract, extended to the meter: an observer rig is judged on what would
    hit the wire, and until this the export axis showed only its decision.
    """
    if decision.intent is ExportIntent.NONE:
        if observer and standing in _STANDING:
            _publish_standing(controller, standing)
            if withheld is not None:
                # (live on .175, 17.09) SAY that this is the roster's
                # re-publish, not a command: the standing row and a command
                # row were indistinguishable, and a dispatch that never saw
                # a command still produced a perfect-looking withheld row.
                row = _dry_run(
                    adapter, ExportIntent.RELEASE if standing == "releasing"
                    else ExportIntent.LIMIT, 0.0)
                row["standing"] = True
                withheld.append(row)
        return None

    watts = float(decision.watts or 0.0)
    if observer:
        # (#764) the decision still ran for real; this seam only records the
        # command it WOULD send, under its own key.
        if controller is not None:
            try:
                controller.publish_observer_decision(
                    key=OBSERVER_KEY, name="grid export",
                    action=decision.intent.value, power_w=watts,
                    reason=decision.reason, kind="battery")
            except Exception:  # noqa: BLE001 — the surface never breaks the seam
                pass
        if withheld is not None:
            row = _dry_run(adapter, decision.intent, watts)
            row["standing"] = False          # the command itself, this cycle
            withheld.append(row)
        # (#1174) the decision log's export row, flagged as observed
        record_export(log_of(controller), decision, observer=True)
        log_on_change(   # (#762) transition-gated
            _LOGGER, OBSERVER_KEY, logging.INFO,
            "OBSERVER · WOULD %s at %.0f W — %s",
            decision.intent.value.upper(), watts, decision.reason)
        return None

    if adapter is None:
        return "no adapter can write the export limit on this install"
    try:
        if decision.intent is ExportIntent.LIMIT:
            await adapter.command_limit_export(watts)
        else:
            await adapter.command_release_export()
    except NotImplementedError as exc:
        return f"export control not available: {exc}"
    except Exception as exc:  # noqa: BLE001 — a refused cut is a state, not a crash
        return f"export control failed: {exc}"
    # (#1174) the write left: the decision log's export row
    record_export(log_of(controller), decision)
    log_on_change(   # (#762) transition-gated
        _LOGGER, OBSERVER_KEY, logging.INFO,
        "export %s at %.0f W — %s", decision.intent.value, watts, decision.reason)
    return None


def _publish_standing(controller, standing: str) -> None:
    """(#764) Keep the cut on the observer ROSTER while it is being held.

    ``observer_decisions`` is swept every cycle by
    ``retire_unpublished_observer_decisions``: *whoever published this cycle
    stays; everyone else is dropped.* The seam speaks when a COMMAND fires —
    once, at the transition — so without this the cut appears for one cycle
    and vanishes while SEM is still holding the meter shut.

    Found live on .175 (16.09) for the second time, from the opposite
    mistake: the first build published the standing row and I removed it,
    reading the map's "always carries the CURRENT would-state" as persistence.
    It is a ROSTER, and that sentence is true only because everyone
    re-publishes. Exactly one publisher per cycle either way — the seam's
    command when there is one, this when there is not.
    """
    if controller is None:
        return
    action = "release_export" if standing == "releasing" else "limit_export"
    # (#992, class 99) One sentence for three states was one too few:
    # ``refused`` means the adapter could not or may not apply the cut, so
    # the meter is NOT shut — and this row is the surface someone reads to
    # verify the #955 guarantee. Say what each state actually is.
    said = {
        "engaged": "export cut engaged — holding the meter shut",
        "releasing": "export cut releasing — handing the meter back",
        "refused": "export cut refused — the adapter declined the write, "
                   "the meter is NOT held",
    }.get(standing, f"export cut {standing}")
    try:
        controller.publish_observer_decision(
            key=OBSERVER_KEY, name="grid export", action=action, power_w=0.0,
            reason=said, kind="battery")
    except Exception:  # noqa: BLE001 — the surface never breaks the seam
        pass


def _dry_run(adapter, intent, watts: float) -> dict:
    """The adapter's dry-run row, or a row that says why there is none. Never
    raises: the surface never breaks the seam."""
    if adapter is None:
        return {"service": None, "data": None,
                "why": "no adapter can write the export limit on this install"}
    try:
        row = adapter.export_dry_run(intent, watts)
        if not isinstance(row, dict):
            raise TypeError(f"dry-run returned {type(row).__name__}")
        return {"service": row.get("service"), "data": row.get("data"),
                "why": row.get("why"), "intent": intent.value}
    except Exception as exc:  # noqa: BLE001 — the surface never breaks the seam
        return {"service": None, "data": None, "why": f"dry-run failed: {exc}",
                "intent": intent.value}
