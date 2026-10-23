"""#955 — the export guard: a limit at the meter, mirroring ``peak_guard``.

The peak guard caps IMPORT per billing slot and every device below it obeys.
This is the same rule mirrored: while the grid-export sink is CLOSED (the
tariff level is NEGATIVE — read, not guessed), cap export at zero. Nothing
here reasons about money: a verdict says the meter is closed and SEM honours
it the way it honours a reserve SOC.

Three rules, each load-bearing:

* **Hysteresis both ways.** Spot prices cross zero repeatedly; a curtailment
  that engages on every crossing is the flapping this project spent months
  removing and a measurable harvest loss. CLOSED must hold ``engage_hold_s``
  before the inverter is touched, OPEN must hold ``release_hold_s`` before it
  is released.
* **Last, not first.** The battery, the car and the loads ran THIS cycle on
  the same verdict. Only export that is still MEASURED at the meter after
  that is clipped — a kWh kept beats a kWh destroyed.
* **Refusal is a state.** A brand with no export control, an adapter that
  raises, a read-back that disagrees: the guard says so, holds, and asks for
  a Repair after three — it never idles silently (class 86: silence reads
  as health).

Pure: the coordinator feeds a monotonic ``now`` and the measured export.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from . import reason_codes as RC

ENGAGE_HOLD_S: float = 120.0
RELEASE_HOLD_S: float = 300.0
EXPORT_EPS_W: float = 50.0        # "export pinned at ~0" — same as the probe's
REFUSALS_FOR_REPAIR: int = 3

LIMIT_EXPORT = "limit_export"
RELEASE_EXPORT = "release_export"


@dataclass(frozen=True)
class ExportCommand:
    intent: Optional[str]          # LIMIT_EXPORT | RELEASE_EXPORT | None
    watts: float                   # the cap: 0.0 for a zero-export window
    reason: str
    #: (#1174) why, as a stable code and its numbers — recorded only
    reason_code: str = field(default="", compare=False)
    reason_params: Dict[str, Any] = field(default_factory=dict, compare=False)


class ExportGuard:
    """States: idle → holding → engaged → releasing → idle; ``refused`` from
    holding/engaged, cleared on the OPEN side once ``release_hold_s`` holds."""

    def __init__(self, engage_hold_s: float = ENGAGE_HOLD_S,
                 release_hold_s: float = RELEASE_HOLD_S) -> None:
        self.engage_hold_s = float(engage_hold_s)
        self.release_hold_s = float(release_hold_s)
        self.state: str = "idle"
        self.reason: str = "export guard idle"
        self._closed_since: Optional[float] = None
        self._open_since: Optional[float] = None
        self._refusals: int = 0
        self.repair_wanted: bool = False
        #: (live on .175) Did this guard ever actually APPLY a cut? A guard
        #: that only ever HELD has nothing to undo, and releasing anyway
        #: would call the inverter's reset over a limit SEM never set — or
        #: over one somebody ELSE set. #908: hand back only what you took.
        self._applied: bool = False

    def report_refused(self, why: str) -> None:
        """The adapter could not (or may not) apply the cut. Sticky until the
        meter has been OPEN for the release hold; three in a row want a Repair."""
        self._refusals += 1
        self.state = "refused"
        self.reason = f"export cut refused: {why}"
        if self._refusals >= REFUSALS_FOR_REPAIR:
            self.repair_wanted = True

    def update(self, now: float, verdict_state: str,
               export_w: Optional[float], *, reason_code: str = "",
               reason_params: Optional[Dict[str, Any]] = None) -> ExportCommand:
        """``reason_code`` / ``reason_params``: (#1174) why the verdict closed
        the meter, carried onto the cut so the decision log can say it."""
        closed = verdict_state == "closed"
        exp = float(export_w or 0.0)          # a BLIND meter is not export
        if closed:
            self._open_since = None
            if self._closed_since is None:
                self._closed_since = now
            held = now - self._closed_since
            if self.state == "refused":
                return ExportCommand(None, 0.0, self.reason)
            if self.state == "engaged":
                self.reason = "export cut holding — the meter is closed"
                return ExportCommand(None, 0.0, self.reason)
            if held < self.engage_hold_s:
                self.state = "holding"
                self.reason = (f"meter closed for {held:.0f}s of "
                               f"{self.engage_hold_s:.0f}s — waiting it out")
                return ExportCommand(None, 0.0, self.reason)
            if export_w is None:
                self.state = "holding"
                self.reason = "meter closed but unreadable — not cutting on a guess"
                return ExportCommand(None, 0.0, self.reason)
            if exp < EXPORT_EPS_W:
                self.state = "holding"
                self.reason = "meter closed and the sinks absorb everything — nothing to clip"
                return ExportCommand(None, 0.0, self.reason)
            self.state = "engaged"
            self._applied = True
            self.reason = f"export {exp:.0f} W into a closed meter — cutting to 0 W"
            return ExportCommand(LIMIT_EXPORT, 0.0, self.reason,
                                 reason_code=reason_code or RC.EXPORT_NEGATIVE_PRICE,
                                 reason_params=dict(reason_params or {}))
        # OPEN (HELD is not a grid state) — release with hysteresis
        self._closed_since = None
        if self.state == "idle":
            return ExportCommand(None, 0.0, "export guard idle")
        if not self._applied and self.state != "refused":
            # Only ever HELD — nothing was written, so there is nothing to put
            # back. Straight to idle, silently (live on .175: the guard emitted
            # a release after a hold that never cut, which on Huawei is a real
            # reset service call over a limit SEM never set).
            self.state = "idle"
            self.reason = "meter open — nothing was cut, nothing to release"
            self._open_since = None
            return ExportCommand(None, 0.0, self.reason)
        if self._open_since is None:
            self._open_since = now
        held = now - self._open_since
        if held < self.release_hold_s:
            if self.state != "refused":
                self.state = "releasing"
            self.reason = (f"meter open for {held:.0f}s of "
                           f"{self.release_hold_s:.0f}s — holding the cut")
            return ExportCommand(None, 0.0, self.reason)
        was_refused = self.state == "refused"
        applied = self._applied
        self.state = "idle"
        self.reason = "export guard idle"
        self._refusals = 0
        self.repair_wanted = False
        self._open_since = None
        self._applied = False
        return ExportCommand(None if (was_refused or not applied) else RELEASE_EXPORT,
                             0.0, "meter open — releasing the cut",
                             reason_code=RC.EXPORT_RELEASED)
