# #955 — the export axis stops duplicating the battery axis

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The export cut decides in a pure decider beside `decide_battery`, dispatches through its own seam beside `actuate_battery`, and is issued ONCE per cycle to the adapter that can actually cut — so there is no second producer of `BatteryDecision` and no second `actuate_battery` call site.

**What this does NOT claim (corrected by the 16.09 review).** It does not put SEM "on one track". SEM's decide/adapter/seam model holds for exactly TWO device axes — the charger offer (`decide.py`) and the battery intent (`decide_battery.py`). Three other paths decide and write outside it today and are **out of scope**: `coordinator/surplus_controller.py:2098` calls `peak_guard.clamp_import_command` directly inside its 750-line `update()`; `coordinator/charge_pacing.py:248/276/308` writes `number.set_value` raw with its own `if observer` branch; `features/load_management.py` has 9 raw `hass.services.async_call` sites with the shed decision in the same method as the write. Those are pre-existing, larger than this arc, and untouched here. Claiming otherwise is what the first draft of this plan did.

**Architecture:** A stateful tracker ticks in the coordinator and puts a value on the fleet state; a PURE function turns that value into an intent; one seam writes it. Today the tracker, the decision and the dispatch all live in one 76-line coordinator method.

**Not "the peak guard's shape" — a cousin, and the difference matters.** `clamp_to_peak_slot` NARROWS a command the same decider is already computing for the same device. Net house export is owned by no per-device decider — it is solar, battery, EV and loads together. So this produces an independent decision through an independent seam to a handle `actuate_battery` may have written moments earlier in the same cycle. That divergence is justified (there is no existing decider to fold a house-level limit into) and is defended on its own terms, not by claiming parity.

**Tech Stack:** Python 3.13/3.14, HA custom integration, pytest. Tests from the CI layout — abbreviated below as `semtest <file>`:
`rsync -a --delete --exclude=.git --exclude=node_modules /home/sem/sem-arc-921/ /tmp/ha-config-arc/custom_components/solar_energy_management/ && cd /tmp/ha-config-arc && PYTHONPATH=/tmp/ha-config-arc python3.12 -m pytest custom_components/solar_energy_management/tests/<file> -q -p no:warnings`
Lint: `/tmp/venv-ci/bin/ruff check <files>` (0.16.3, the CI pin).

**Branch:** continues on `feature/921-grid-not-always-a-sink` (worktree `/home/sem/sem-arc-921`). **No behaviour change** — this is a re-layering of code that is already live-proven, so every existing test and the .175 evidence carry over unchanged. Any test that has to be *rewritten* rather than *moved* is a signal the refactor changed behaviour; stop and look.

---

## Why (the evidence, so the reader does not have to re-derive it)

| | today | the peak guard (#864), same claim |
|---|---|---|
| tracker | `ExportGuard` in `coordinator._run_export_guard` | `PeakSlotTracker` in `coordinator._compute_peak_slot_allowance` |
| value on the fleet state | — (never leaves the method) | `peak_slot_allowed_w` → `FleetCycleState` → `FleetContext` |
| decision | **in the coordinator** — builds `BatteryDecision`s itself | `decide.py:1150` `clamp_to_peak_slot(result, view)` — **in the decide layer** |
| dispatch | **its own** `await actuate_battery(...)` at `coordinator.py:10871` | the charger's existing seam |

⚠️ The right-hand column is the peak guard at its BEST: `surplus_controller.py:2098` calls its clamp directly, so even this precedent is not uniformly clean — see the scope note above.

Consequences, all present in the tree right now:

- **Two producers of `BatteryDecision`**: `decide_battery.py` and `coordinator.py`. Two `actuate_battery` call sites: `:7836` (the battery loop) and `:10871` (the guard).
- **The observer-key collision was a symptom, not a bug.** Both producers published under `battery:<id>` and clobbered each other; the fix in `1bfcc1e0` patched the key instead of removing the second producer.
- **Category error:** the export cut is HOUSE-level (one inverter) but rides the PER-BATTERY path — the guard loops every adapter, so a two-battery single-inverter install issues the same service call twice. Huawei's #538 de-dup hides it.
- **But `_primary_battery_adapter()` is NOT the fix** (review, 16.09). It is positional — `adapters.get("primary") or next(iter(adapters.values()))` (`coordinator.py:10418`) — and its docstring only ever fixed a singular-vs-plural attribute bug; it makes no claim about which adapter owns the grid tie. On a #531 mixed fleet (a Sessy AC battery beside a Huawei inverter) it can hand the cut to the adapter that *cannot* cut while the inverter that can is never asked, and the guard then reports `refused` forever — the #874 shape, for batteries. Task 4 introduces a capability-based selector instead.

**Why it happened, so it is not repeated:** `decide_battery` returns ONE intent per battery per cycle, and a battery can need `LIMIT_DISCHARGE` *and* an export cut in the same cycle. That is a real constraint. The answer is a separate axis with its own decider — not a coordinator that decides for itself.

**On "last, not first":** the tracker moves to the top of the cycle (beside the peak guard) instead of after the battery loop. That is not a weakening. The rule means the guard clips only export the sinks did not absorb; over a 10 s cycle with minute-scale holds, the meter reading at the top of cycle *N* already reflects what the sinks did in cycle *N−1*. The review accepted the same one-cycle lag for the probe (finding 8, "immaterial under multi-minute hold timers"). Task 4 pins it.

## File structure

| File | Responsibility after this plan |
|---|---|
| `coordinator/export_guard.py` | unchanged — the pure hysteresis tracker + `ExportCommand` |
| `coordinator/decide_export.py` (new) | `decide_export(fleet) → ExportDecision` — pure, brand-blind; takes the FLEET, never the battery loop's last-assigned `view` |
| `coordinator/actuate_export.py` (new) | `actuate_export(decision, adapter, *, observer, controller)` — one write, observer cuts here, publishes the observer decision |
| `coordinator/charger_types.py` | `ExportDecision`; `export_command` on `FleetCycleState` / `FleetContext`; `BatteryIntent.LIMIT_EXPORT/RELEASE_EXPORT` **removed** |
| `coordinator/actuate_battery.py` | the export branch and the `export:<id>` key **removed** — batteries go back to one axis |
| `coordinator/coordinator.py` | `_compute_export_command(power)` (tick only), the fleet-state field, one `actuate_export` call after the battery loop; `_run_export_guard` **deleted** |
| `tests/test_921_export_decide.py` (new) | the pure decider |
| `tests/test_921_guard_wiring.py`, `test_921_handback.py`, `test_921_export_guard.py` | moved to the new seam; the observer-key tests now assert the seam's own key |
| `tests/test_921_one_track.py` (new) | the structural pins that make the second track unrepresentable |

---

### Task 1: `ExportDecision` + a pure `decide_export`

> **Review correction:** the decider takes the **fleet context**, not a `BatteryView`. The first draft wrote `decide_export(view)` and relied on `view` being whatever the per-battery loop last assigned — which survives only because `battery_items` always gets a synthetic `"primary"` entry. `fleet` is a plain local (`coordinator.py:7605`) shared by every view built this cycle; take it directly.

**Files:** Create `coordinator/decide_export.py`; modify `coordinator/charger_types.py`; test `tests/test_921_export_decide.py`.

- [x] **Step 1: Write the failing test**

```python
"""#955 — the export cut decides in the DECIDE layer, like everything else.

Pure: same inputs, same decision, no hass, no adapter, no clock of its own.
The tracker (``ExportGuard``) still owns hysteresis; this turns its command
into the intent the seam will write.
"""
from types import SimpleNamespace

import pytest

from custom_components.solar_energy_management.coordinator.charger_types import (
    ExportDecision, ExportIntent,
)
from custom_components.solar_energy_management.coordinator.decide_export import decide_export
from custom_components.solar_energy_management.coordinator.export_guard import (
    LIMIT_EXPORT, RELEASE_EXPORT, ExportCommand,
)


def _fleet(cmd=None, enabled=True):
    return SimpleNamespace(export_command=cmd, export_guard_enabled=enabled)


class TestTheDecision:
    def test_a_limit_command_becomes_a_limit_intent(self):
        d = decide_export(_fleet(ExportCommand(LIMIT_EXPORT, 0.0, "closed + 3 kW export")))
        assert d.intent is ExportIntent.LIMIT and d.watts == 0.0
        assert "closed" in d.reason

    def test_a_release_command_becomes_a_release_intent(self):
        d = decide_export(_fleet(ExportCommand(RELEASE_EXPORT, 0.0, "meter open")))
        assert d.intent is ExportIntent.RELEASE

    def test_no_command_is_no_intent(self):
        assert decide_export(_fleet(ExportCommand(None, 0.0, "holding"))).intent is ExportIntent.NONE

    def test_an_absent_command_is_no_intent(self):
        """Every install before the first tick, and every rig-shaped stub."""
        assert decide_export(_fleet(None)).intent is ExportIntent.NONE

    def test_the_switch_off_is_no_intent_whatever_the_command_says(self):
        d = decide_export(_fleet(ExportCommand(LIMIT_EXPORT, 0.0, "closed"), enabled=False))
        assert d.intent is ExportIntent.NONE

    def test_it_is_pure(self):
        """Called twice on the same view, it answers the same — no state."""
        f = _fleet(ExportCommand(LIMIT_EXPORT, 0.0, "closed"))
        assert decide_export(f) == decide_export(f)
```

- [x] **Step 2: Run it and watch it fail** — `semtest tests/test_921_export_decide.py` → `ModuleNotFoundError: decide_export`.

- [x] **Step 3: The types** (`coordinator/charger_types.py`, beside `BatteryDecision`):

```python
class ExportIntent(Enum):
    """(#955) What ``actuate_export`` should ask the inverter to do. A HOUSE
    axis, not a per-battery one: the meter is one meter."""

    NONE = "none"
    LIMIT = "limit_export"
    RELEASE = "release_export"


@dataclass(frozen=True)
class ExportDecision:
    intent: ExportIntent = ExportIntent.NONE
    watts: float = 0.0          # the cap; 0.0 for a closed meter
    reason: str = ""
```

and `export_command: "Any" = None` + `export_guard_enabled: bool = False` on **`FleetCycleState`** and **`FleetContext`**, beside `peak_slot_allowed_w` (which is the field this one is modelled on).

- [x] **Step 4: The decider** (`coordinator/decide_export.py`):

```python
"""Pure ``decide_export(view) → ExportDecision`` (#955).

The house's meter limit, decided the way every other SEM control is decided:
a pure function of the view. ``ExportGuard`` (the tracker) owns hysteresis and
"last, not first"; it has already answered *whether* a write is due this cycle
and put its command on the fleet state. This turns that into the intent the
seam will write — and nothing else. No hass, no adapter, no clock.

It exists because the first build decided IN the coordinator and dispatched
from there: two producers of a battery decision, two seams, and an observer
surface where the two clobbered each other under one key.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from .charger_types import ExportDecision, ExportIntent
from .export_guard import LIMIT_EXPORT, RELEASE_EXPORT

if TYPE_CHECKING:  # pragma: no cover
    from .charger_types import FleetContext

_BY_COMMAND = {LIMIT_EXPORT: ExportIntent.LIMIT, RELEASE_EXPORT: ExportIntent.RELEASE}


def decide_export(fleet: "FleetContext") -> ExportDecision:
    """This cycle's export intent. ``NONE`` whenever the guard is off, has no
    command, or is merely holding — the overwhelmingly common case.

    Takes the FLEET, not a per-battery view: the meter is a house quantity and
    no battery owns it (review, 16.09 — the first draft leaned on the battery
    loop's last-assigned ``view``)."""
    if not bool(getattr(fleet, "export_guard_enabled", False)):
        return ExportDecision(reason="export guard off")
    cmd = getattr(fleet, "export_command", None)
    intent = _BY_COMMAND.get(getattr(cmd, "intent", None), ExportIntent.NONE)
    if intent is ExportIntent.NONE:
        return ExportDecision(reason=getattr(cmd, "reason", "") or "no export command")
    return ExportDecision(intent=intent,
                          watts=float(getattr(cmd, "watts", 0.0) or 0.0),
                          reason=str(getattr(cmd, "reason", "")))
```

- [x] **Step 5: Run** → 6 passed. **Step 6: Commit** `git commit -m "feat(#955): decide_export — the meter limit decides in the decide layer"`

---

### Task 2: `actuate_export` — one seam, and the observer key falls out of it

**Files:** Create `coordinator/actuate_export.py`; test `tests/test_921_export_seam.py`.

- [x] **Step 1: Write the failing test**

```python
"""#955 — one write, observer cuts here, and the key belongs to the seam.

The first build published the cut under ``battery:<id>`` — the battery's own
key — so the two decisions clobbered each other every cycle and the cut was
invisible on the rig (found live on .175 with the compressed sim). A seam that
owns its key cannot have that bug.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.solar_energy_management.coordinator.actuate_export import (
    actuate_export,
)
from custom_components.solar_energy_management.coordinator.charger_types import (
    ExportDecision, ExportIntent,
)


def _adapter():
    a = MagicMock()
    a.command_limit_export = AsyncMock()
    a.command_release_export = AsyncMock()
    a._last_error = None
    return a


@pytest.mark.asyncio
class TestTheSeam:
    async def test_limit_writes_the_cap(self):
        a = _adapter()
        await actuate_export(ExportDecision(ExportIntent.LIMIT, 0.0, "closed"), a)
        a.command_limit_export.assert_awaited_once_with(0.0)

    async def test_release_writes_the_release(self):
        a = _adapter()
        await actuate_export(ExportDecision(ExportIntent.RELEASE, 0.0, "open"), a)
        a.command_release_export.assert_awaited_once()

    async def test_none_writes_nothing(self):
        a = _adapter()
        await actuate_export(ExportDecision(reason="holding"), a)
        a.command_limit_export.assert_not_awaited()
        a.command_release_export.assert_not_awaited()

    async def test_observer_cuts_the_trigger_and_records_under_the_seams_key(self):
        a = _adapter(); ctl = MagicMock()
        await actuate_export(ExportDecision(ExportIntent.LIMIT, 0.0, "closed"),
                             a, observer=True, controller=ctl)
        a.command_limit_export.assert_not_awaited()
        kw = ctl.publish_observer_decision.call_args.kwargs
        assert kw["key"] == "export_guard"          # never battery:<id>
        assert kw["action"] == "limit_export"
        assert kw["kind"] == "battery"              # what the .175 sim filters on

    async def test_a_brand_without_the_verb_is_a_refusal_not_a_crash(self):
        a = _adapter()
        a.command_limit_export = AsyncMock(side_effect=NotImplementedError("no export control"))
        said = await actuate_export(ExportDecision(ExportIntent.LIMIT, 0.0, "closed"), a)
        assert said and "no export control" in said

    async def test_an_error_is_a_refusal_too(self):
        a = _adapter()
        a.command_limit_export = AsyncMock(side_effect=RuntimeError("modbus timeout"))
        assert "modbus timeout" in (await actuate_export(
            ExportDecision(ExportIntent.LIMIT, 0.0, "closed"), a) or "")

    async def test_a_successful_write_refuses_nothing(self):
        assert await actuate_export(ExportDecision(ExportIntent.LIMIT, 0.0, "c"), _adapter()) is None

    async def test_no_adapter_is_a_refusal(self):
        assert "no adapter" in (await actuate_export(
            ExportDecision(ExportIntent.LIMIT, 0.0, "c"), None) or "")
```

- [x] **Step 2: Run and watch it fail.**

- [x] **Step 3: The seam** (`coordinator/actuate_export.py`) — returns the refusal string (or `None`), so the caller can feed `ExportGuard.report_refused` without the seam knowing about the tracker:

```python
"""Pure-dispatch ``actuate_export(decision, adapter)`` (#955).

Mirrors :func:`actuate_battery` for the house's meter limit: one intent, one
adapter method, no branch on brand. Observer mode cuts the trigger here and
records what SEM WOULD command, under **this seam's own key** — the first
build borrowed the battery's key and the two overwrote each other every cycle.

Returns the refusal text when the write could not happen (no adapter, a brand
with no export control, an adapter that raised), else ``None``. The caller
hands that to the guard; the seam itself knows nothing about hysteresis.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

from .charger_types import ExportIntent
from ..utils.log_gate import log_on_change

if TYPE_CHECKING:  # pragma: no cover
    from .battery_adapters.base import BatteryControlAdapter
    from .charger_types import ExportDecision

_LOGGER = logging.getLogger(__name__)

#: The one key this axis publishes under. A house has one meter.
OBSERVER_KEY = "export_guard"


async def actuate_export(decision: "ExportDecision",
                         adapter: "Optional[BatteryControlAdapter]", *,
                         observer: bool = False,
                         controller=None) -> Optional[str]:
    if decision.intent is ExportIntent.NONE:
        return None
    if observer:
        if controller is not None:
            try:
                controller.publish_observer_decision(
                    key=OBSERVER_KEY, name="grid export",
                    action=decision.intent.value, power_w=float(decision.watts or 0.0),
                    reason=decision.reason, kind="battery")
            except Exception:  # noqa: BLE001 — the surface never breaks the seam
                pass
        log_on_change(_LOGGER, OBSERVER_KEY, logging.INFO,
                      "OBSERVER · WOULD %s at %.0f W — %s",
                      decision.intent.value.upper(), float(decision.watts or 0.0),
                      decision.reason)
        return None
    if adapter is None:
        return "no adapter to write the export limit"
    try:
        if decision.intent is ExportIntent.LIMIT:
            await adapter.command_limit_export(float(decision.watts or 0.0))
        else:
            await adapter.command_release_export()
    except NotImplementedError as exc:
        return f"export control not available: {exc}"
    except Exception as exc:  # noqa: BLE001 — a refused cut is a state, not a crash
        return f"export control failed: {exc}"
    log_on_change(_LOGGER, OBSERVER_KEY, logging.INFO,
                  "export %s at %.0f W — %s", decision.intent.value,
                  float(decision.watts or 0.0), decision.reason)
    return None
```

- [x] **Step 4: Run** → 8 passed. **Step 5: Commit** `git commit -m "feat(#955): actuate_export — one write for the house's meter limit, and the seam owns its observer key"`

---

### Task 3: Batteries go back to one axis

**Files:** `coordinator/actuate_battery.py`, `coordinator/charger_types.py`; tests `tests/test_921_export_guard.py`, `tests/test_921_guard_wiring.py`.

- [x] **Step 1:** Delete from `actuate_battery.py` the `LIMIT_EXPORT/RELEASE_EXPORT` branch, the `_export` key selection added in `1bfcc1e0`, and the `BatteryIntent.LIMIT_EXPORT: decision.export_limit_w` entry in `_observe`'s watts map. Delete `BatteryIntent.LIMIT_EXPORT` / `RELEASE_EXPORT` and `BatteryDecision.export_limit_w` from `charger_types.py`.

- [x] **Step 2:** Move the affected tests rather than rewriting them: the `TestTheIntentsDispatch` class in `test_921_export_guard.py` and `TestTheCutIsVisibleInObserverMode` in `test_921_guard_wiring.py` are now Task 2's seam tests — delete the duplicates, keeping `test_921_export_seam.py` as the one home. **If a moved test needs its ASSERTION changed (not just its import), stop:** the refactor changed behaviour and that is not the goal.

- [x] **Step 3: Run** `semtest tests/test_921_export_guard.py tests/test_921_export_seam.py tests/test_actuate_battery*.py tests/test_818*.py tests/test_battery_modes_523.py` → all pass.

- [x] **Step 4: Commit** `git commit -m "refactor(#955): the battery decision carries one axis again — the export cut has its own"`

---

### Task 4: The coordinator ticks, and nothing more

> **Review corrections (16.09), all load-bearing:**
> 1. **The wobble is DROPPED.** The first draft hedged: *"if that reads as two mechanisms, have the tracker re-issue its command while engaged."* That would be a #538 write storm — `battery_adapters/generic.py:command_limit_export` calls `hass.services.async_call("number","set_value",…)` **unconditionally** and only then records the value, so any brand on a plain number entity would get a write every ~10 s for the whole time the meter stays closed. The shipped design (one-shot dispatch + a separately maintained `_export_guard_state` dict for the card) is correct; keep it.
> 2. **Persistence stays IDENTITY-keyed.** Only the DISPATCH narrows to one adapter. `export_release_recipes()` keeps looping `_battery_adapters` and keying by real `battery_id`, because the reader — `_export_guard_adopt()` — looks recipes up by real id. Collapsing the store to a single `"primary"` key would make every adoption lookup miss on a multi-battery install and silently lose the captured prior, re-introducing exactly what `92e8b7d0` was built to prevent.
> 3. **A capability selector, not `_primary_battery_adapter()`** — see Step 4a.

**Files:** `coordinator/coordinator.py`, `coordinator/build_view.py`; test `tests/test_921_guard_wiring.py` (rewritten around the tick).

- [x] **Step 1: Write the failing test**

```python
class TestTheCoordinatorOnlyTicks:
    """After the re-layering the coordinator computes a COMMAND and dispatches
    one decision through one seam. It does not decide, and it does not build a
    BatteryDecision."""

    def test_the_tick_puts_a_command_on_the_cycle(self):
        fake, power, _, _ = _fake(verdict=CLOSED, export_w=3000.0)
        for t in (0, 130):
            SEMCoordinator._compute_export_command(fake, power, now=float(t))
        assert fake._export_command.intent == "limit_export"

    def test_the_guard_off_means_no_command(self):
        fake, power, _, _ = _fake(verdict=CLOSED, export_w=3000.0, enabled=False)
        SEMCoordinator._compute_export_command(fake, power, now=0.0)
        assert fake._export_command.intent is None

    def test_a_blind_meter_never_commands(self):
        fake, power, _, _ = _fake(verdict=CLOSED, export_w=3000.0)
        power.grid_power_unavailable = True
        for t in (0, 130):
            SEMCoordinator._compute_export_command(fake, power, now=float(t))
        assert fake._export_command.intent is None

    def test_the_command_rides_the_view(self):
        from .ast_contracts import call_kwargs
        from custom_components.solar_energy_management.coordinator import build_view
        kwargs = call_kwargs(build_view.build_charger_view, "FleetContext")
        assert kwargs and "export_command" in kwargs[0]
```

- [x] **Step 2: Run and watch it fail.**

- [x] **Step 3: The tick**, beside `_compute_peak_slot_allowance` (`coordinator.py:10675`), called from the same place in the cycle. Body as in the first draft: lazily build `ExportGuard` from the two hold numbers, adopt a stored cut if the hook exists, read the verdict, pass `None` for a blind meter, and store `self._export_command = self._export_guard.update(...)`. **No decision, no write.**

  On "last, not first": `power` is a fixed per-cycle snapshot (`grid_export_power` is never mutated between the top of the cycle and the battery loop — grepped clean), and the dispatch still runs after the battery loop, so moving the tick changes no input. The 120 s / 300 s holds dwarf the distinction either way.

- [x] **Step 4: Thread it.** `_build_fleet_cycle_state` passes `export_command=getattr(self, "_export_command", None)` and `export_guard_enabled=bool(self.config.get("export_guard_enabled", False))`; `build_view.py` copies both onto `FleetContext` beside `sink_verdicts`.

- [x] **Step 4a: The capability selector** — new, replacing the plan's earlier use of `_primary_battery_adapter()`:

```python
    def _export_control_adapter(self):
        """(#955) The adapter that can actually cut this house's export.

        NOT ``_primary_battery_adapter()``: that one is positional
        (``next(iter(adapters.values()))``) and says nothing about who owns the
        grid tie. On a #531 mixed fleet — a Sessy AC battery beside a Huawei
        inverter — the first-inserted adapter may have no export control at
        all, and offering it the cut would leave the guard `refused` forever
        while the inverter that CAN cut is never asked.

        Capability first: prefer an adapter that produces a release recipe
        (i.e. it knows how to undo its own cut), then any that implements the
        verb, then the primary as a last resort so a single-battery install
        behaves exactly as before.
        """
        adapters = getattr(self, "_battery_adapters", None) or {}
        if not adapters:
            return None
        for adapter in adapters.values():
            try:
                if adapter.export_release_recipe() is not None:
                    return adapter
            except Exception:  # noqa: BLE001 — a brand that cannot answer is not the one
                continue
        base = type(self).__mro__ and None          # readability: the base's verb is the sentinel
        for adapter in adapters.values():
            fn = getattr(type(adapter), "command_limit_export", None)
            if fn is not None and getattr(fn, "__qualname__", "").split(".")[0] != "BatteryControlAdapter":
                return adapter                       # overrides the base = implements the verb
        return self._primary_battery_adapter()
```

  (Drop the `base = …` line when writing it — it is noise; the qualname test is the check. Confirm the base class name with `grep -n "^class BatteryControlAdapter" coordinator/battery_adapters/base.py`.)

- [x] **Step 5: The one dispatch**, after the per-battery loop, replacing `_run_export_guard` (delete all 76 lines):

```python
        # (#955) The house's meter limit: ONE decision, ONE seam, ONE adapter
        # — the one that can actually cut, not whichever was inserted first.
        from .actuate_export import actuate_export
        from .charger_types import ExportIntent
        from .decide_export import decide_export
        _xd = decide_export(fleet)                   # the cycle's fleet, not a per-battery view
        _refused = await actuate_export(
            _xd, self._export_control_adapter(),
            observer=self._observer_mode, controller=self._surplus_controller)
        if _refused:
            self._export_guard.report_refused(_refused)
        elif _xd.intent is not ExportIntent.NONE and not self._observer_mode:
            _persist = getattr(self, "_export_guard_persist", None)
            if callable(_persist):
                await _persist(_xd.intent is ExportIntent.LIMIT)
        self._export_guard_state = { ... }           # unchanged from today
```

- [x] **Step 6: Persistence and hand-back keep identity.** `export_release_recipes()` is UNCHANGED (loops `_battery_adapters`, keys by real `battery_id`) so `_export_guard_adopt()` keeps finding its recipe. `async_release_export_guard()` is UNCHANGED for the same reason: a previous lifetime may have engaged through a different adapter, and releasing every adapter that has a recipe is both correct and idempotent (#538 de-dup on Huawei, a live-state check on Deye, and generic only writes when it captured a prior).

- [x] **Step 7: Run** `semtest tests/test_921_guard_wiring.py tests/test_921_handback.py tests/test_921_sink_scenario.py tests/test_873_cycle_executes.py tests/test_864*.py tests/test_743*.py` → all pass.

- [x] **Step 8: Commit** `git commit -m "refactor(#955): the coordinator ticks; decide_export decides; actuate_export writes once, to the adapter that can cut"`

---

### Task 5: Make the second track unrepresentable

**Files:** `tests/test_921_one_track.py` (new).

- [x] **Step 1: Write the test** (it is the whole task)

```python
"""#955 — the structural pins that keep this on one track.

16.09, reading the first build: "is this implemented where all the
decisions are taking place, or are we on a second track? I remember having 3
layers in SEM." That was right — the export cut decided in the coordinator and
dispatched itself. These are the guards that make that unrepresentable.
"""
from pathlib import Path

from .ast_contracts import call_sites, symbol_reference_files

ROOT = Path(__file__).resolve().parent.parent


class TestOneProducerPerDecision:
    def test_only_decide_battery_builds_a_battery_decision(self):
        sites = {p for p, _, _ in call_sites("BatteryDecision")}
        assert sites == {"coordinator/decide_battery.py"}, sites

    def test_only_decide_export_builds_an_export_decision(self):
        sites = {p for p, _, _ in call_sites("ExportDecision")}
        assert sites == {"coordinator/decide_export.py"}, sites


class TestOneSeamPerAxis:
    def test_actuate_battery_has_one_production_call_site(self):
        assert {p for p, _, _ in call_sites("actuate_battery")} == {"coordinator/coordinator.py"}

    def test_actuate_export_has_one_production_call_site(self):
        sites = [s for s in call_sites("actuate_export")]
        assert len(sites) == 1 and sites[0][0] == "coordinator/coordinator.py", sites

    def test_the_observer_key_is_named_once(self):
        """The key belongs to the seam; nothing else may spell it."""
        assert symbol_reference_files("OBSERVER_KEY") <= {"coordinator/actuate_export.py"}


class TestTheDecidersArePure:
    def test_neither_decider_touches_hass_or_an_adapter(self):
        for mod in ("decide_battery.py", "decide_export.py"):
            src = (ROOT / "coordinator" / mod).read_text(encoding="utf-8")
            for forbidden in ("hass.", "async_call(", "await ", "_battery_adapters"):
                assert forbidden not in src, f"{mod} reaches outside the decide layer: {forbidden}"


class TestTheHouseAxisIsHouseLevel:
    """The DISPATCH narrows to one adapter; PERSISTENCE keeps identity.
    (Review 16.09: the first draft narrowed both, which would have made every
    `_export_guard_adopt` lookup miss on a multi-battery install.)"""

    def test_the_cut_is_dispatched_to_the_capable_adapter_not_every_battery(self):
        import inspect
        from custom_components.solar_energy_management.coordinator.coordinator import (
            SEMCoordinator,
        )
        src = inspect.getsource(SEMCoordinator._async_update_data)
        assert "_export_control_adapter" in src
        assert "actuate_export" in src

    def test_the_selector_prefers_an_adapter_that_can_actually_cut(self):
        """#531 shape: a Sessy that cannot cut inserted before the Huawei that can."""
        from types import SimpleNamespace
        from unittest.mock import MagicMock
        from custom_components.solar_energy_management.coordinator.coordinator import (
            SEMCoordinator,
        )
        cannot = MagicMock(); cannot.export_release_recipe = MagicMock(return_value=None)
        can = MagicMock(); can.export_release_recipe = MagicMock(
            return_value={"domain": "huawei_solar", "service": "reset_maximum_feed_grid_power",
                          "data": {"device_id": "dev"}})
        fake = SimpleNamespace(_battery_adapters={"sessy": cannot, "huawei": can})
        assert SEMCoordinator._export_control_adapter(fake) is can

    def test_persistence_still_loops_every_adapter_by_real_id(self):
        """The reader looks recipes up by battery_id; a single "primary" key
        would make every adoption miss (92e8b7d0's failure, reintroduced)."""
        import inspect
        from custom_components.solar_energy_management.coordinator.coordinator import (
            SEMCoordinator,
        )
        for fn in (SEMCoordinator.export_release_recipes,
                   SEMCoordinator.async_release_export_guard,
                   SEMCoordinator._export_guard_adopt):
            src = inspect.getsource(fn)
            assert "_battery_adapters" in src, f"{fn.__name__} lost identity-keyed recipes"

    def test_the_recipe_keys_round_trip(self):
        """What export_release_recipes writes, _export_guard_adopt must find."""
        import inspect
        from custom_components.solar_energy_management.coordinator.coordinator import (
            SEMCoordinator,
        )
        writer = inspect.getsource(SEMCoordinator.export_release_recipes)
        reader = inspect.getsource(SEMCoordinator._export_guard_adopt)
        assert "str(bid)" in writer and "str(bid)" in reader
```

`call_sites` returns `(relative_path, lineno, [kwargs])` and skips `tests/`; confirm the tuple shape before relying on it (`grep -n "def call_sites" -A 12 tests/ast_contracts.py`). If `symbol_reference_files` takes different arguments, read its signature — do not guess.

- [x] **Step 2: Run** — expect real failures first (they are the point), then fix what they name until green.

- [x] **Step 3: Commit** `git commit -m "test(#955): the second track is now unrepresentable — one producer, one seam, pure deciders, one adapter"`

---

### Task 6: Re-prove on .175, and write it down

- [x] **Step 1: Suite + lint.** `semtest tests/ -q -rf` green; `/tmp/venv-ci/bin/ruff check .` clean; push and wait for CI on PR #965.

- [x] **Step 2: The same live episode, on the new layering.** Deploy (`SRC=/home/sem/sem-arc-921 ~/bin/sem-deploy-175.sh`), stage export through the split-pair override (`grid_import_power_entity` → the 6000 W entity, `grid_export_power_entity` → the 0 W entity, **both**, then reload the entry — see `reference_175_harness.md`), hold the feed-in at −0.05, `export_guard_enabled: true`, `export_guard_engage_s: 60`. Read the rig's own surfaces with `~/bin/sem-sim-compress.sh 10.10.20.175 18.0 20`. Expect exactly what the first build produced, from the new seam:

```
WOULD export_guard    limit_export     | export cut holding — the meter is closed
WOULD battery:primary limit_discharge  | …
```

then flip the feed-in positive and watch `releasing → idle` with the entry absent once idle. **Restore:** clear the four override keys, delete the synthetic entities, reload.

- [x] **Step 3: Update the record.** Append the outcome to `~/claude-jobs/challenge-feature-921-grid-not-always-a-sink.md`; add one line to `docs/BUG_CLASSES.md` if the second-track shape deserves a class of its own (*"a new control that decides in the orchestrator instead of the decide layer — two producers of one decision, discovered as an observer-surface collision"*); note in `docs/ARCHITECTURE.md` that the export axis has its own decider and seam beside the battery's.

- [x] **Step 4: Commit** `git commit -m "docs(#955): the export axis has its own decider and seam — the three layers hold"`

---

## Before merge

- Full suite green on the merge result; ruff clean; CI green (3.13 + 3.14).
- The challenge record carries both the ruflo round and both live rounds.
- **The judgement that matters:** open `coordinator/coordinator.py` and ask of the export axis the question asked then — *is the decision here, or in the decide layer?* The answer must be visible without running anything.
- Merge on approval with `SEM_FEAT_OK`.

## Cost and risk

**Cost:** 6 tasks, ~4 files touched plus test moves. No behaviour change — which is also the risk: a refactor with no behaviour change is one where a mistake is silent. Task 5's structural pins and Task 6's live re-proof are what make it visible.

**What does NOT change:** the verdicts, the tracker's hysteresis and "last, not first", the adapters, the persistence and hand-back semantics, the surface, the plan rows, the 16 languages. All of it is already live-proven and carries over.

**If the refactor is not taken:** the arc still works — it is proven on real hardware — but SEM gains a second place where a decision is made, and the next feature that needs the same shape will copy it. That is the cost to weigh; it is an open decision.


---

## What execution changed (16.09) — read this before trusting the steps above

Five behaviour changes came out of the build, four of them defects the plan
introduced. The steps are left as written so the diff between plan and outcome
stays legible; where they disagree, the code is right.

1. **Task 4 Step 3 is wrong about placement.** "Beside `_compute_peak_slot_allowance`"
   is 60 lines BEFORE `self._sink_verdicts` is assigned, so the guard read the
   previous cycle's verdict. The tick goes AFTER the verdicts it reads.
2. **Task 4 Step 3 is wrong about adoption.** `_build_fleet_cycle_state` is sync,
   so "adopt a stored cut if the hook exists" became a scheduled task that lands
   a cycle late — an idle first tick then overwrote the restored cut. Adoption
   moved to `_ensure_export_guard()`, awaited by the cycle.
3. **Task 4 Step 6 is wrong about the hand-back being idempotent.** Huawei's
   release needs no prior, so "releasing every adapter that has a recipe" resets
   a feed-in limit the OWNER set on a #531 mixed fleet. Adapters answer
   `holds_export_cut()` from their own `_last_export_intent`; every hand-back
   path asks that instead of `export_release_recipe()`.
4. **Task 3 missed the shared de-dup marker.** The export verbs stamped
   `_last_intent` — the battery axis's #538 marker. The export axis owns
   `_last_export_intent` now.
5. **The dispatch is a method, not inline.** `_apply_export_decision(fleet)`, so
   it can be exercised without a whole coordinator.

And one pin in Task 5 was vacuous as written: `symbol_reference_files` does not
see an import, so the borrowed-key mutation passed. Replaced with a walker that
counts imports. See the Round 4 section of the challenge record.


## The live re-proof (16.09) — and the sixth change

Task 6 Step 2 ran on .175 against the re-layered axis, observer ON, real
Huawei, nothing written. The full episode held:

```
holding    0s/22s of 60s          roster ABSENT          would=None
ENGAGED    four reads over minutes roster limit_export    would=engaged
                                   beside battery:primary limit_discharge
releasing  0/30/60/80s of 90s     roster release_export  would=releasing
idle                              roster ABSENT          would=None
```

**6. The removal in "What execution changed" item 5 was WRONG, and the rig
said so.** `observer_decisions` does not persist: it is a ROSTER, swept every
cycle by `retire_unpublished_observer_decisions`. One publish at the
transition is retired on the next cycle, so the ENGAGED cut was invisible on
the surface a case is judged on (#855) while the card read it correctly. The
standing publish was never a second mechanism — it was the contract. Restored
inside the seam (`_publish_standing`), which is where Task 5's OBSERVER_KEY
pin forced it.

Rig lessons worth keeping:
- `ha core restart` issued right after a deploy's own restart is silently
  REFUSED ("Another job is running for job group home_assistant_core"), and
  SEM then runs the pre-patch config. Verify the RUNNING config, not the file.
- .175 has no `dynamic_feedin_entity` (static export_rate 0.075), so a negative
  export price has to be staged as a synthetic feed-in entity as well as the
  split pair.
