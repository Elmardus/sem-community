# #921 — the grid is not always a sink — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give every destination a kWh can take a per-cycle OPEN / HELD / CLOSED verdict decided in one pure place, enforce "grid export CLOSED while the price is negative" as a limit at the meter with per-brand write adapters, and let the battery, the house, the car and the loads absorb energy before anything is clipped — all default-OFF, proven once on .175, merged whole.

**Architecture:** `sink_verdicts()` (new, pure) reads the tariff LEVEL, the forecast, the clock and the permission axis and returns one `SinkVerdict` per sink; it rides `FleetCycleState` like every other fleet input. `ExportGuard` (new, pure, mirrors `peak_guard.py`) turns a CLOSED grid verdict into `LIMIT_EXPORT` / `RELEASE_EXPORT` intents with hysteresis, dispatched through `actuate_battery`'s existing observer seam to two new adapter verbs (Huawei = services, Deye = the #827 select, generic = a writable number). `decide_battery`, `_battery_assist_split`, `charge_pacing` and `today_plan` consume the verdicts; none of them reads a price.

**Tech Stack:** Python 3.13/3.14, Home Assistant custom integration, pytest + pytest-homeassistant-custom-component. Tests run from the CI layout: `rsync -a --delete --exclude=.git --exclude=node_modules ./ /tmp/ha-config-arc/custom_components/solar_energy_management/ && cd /tmp/ha-config-arc && PYTHONPATH=/tmp/ha-config-arc python3.12 -m pytest custom_components/solar_energy_management/tests/<file> -q -p no:warnings` — abbreviated below as `semtest <file>`. Lint: `/tmp/venv-ci/bin/ruff check <files>` (ruff 0.16.3, the CI pin).

**Spec:** `docs/superpowers/specs/2026-09-15-921-grid-not-always-a-sink-design.md`. **Branch:** `feature/921-grid-not-always-a-sink` in the worktree `/home/sem/sem-arc-921` (stages 1–3, Tasks 1–17). **Stage 4 (#956, Tasks 18–24) is its own branch `feature/956-roster-services`, cut from develop after the arc merges** — decision 15.09: adapters first, #956 after — and completes the arc.

---

## Two facts that shape every task

1. **No install has a negative export rate today** (PROD is a fixed 0.075/kWh). Every behaviour here is dormant until a spot feed-in tariff classifies an hour `NEGATIVE`, and every switch ships OFF, so nothing wakes itself. That is release-train gate 4 satisfied by being inert.
2. **"I could not ask" is not "the meter is hostile."** An unreadable export price is `NORMAL`/OPEN everywhere in this plan (#925, class 86). The forecast sell already refuses to sell on an unreadable price (`forecast_sell.py:119-133`); the guard must never *engage* on one.

## File structure

| File | Responsibility |
|---|---|
| `coordinator/energy_calculator.py:643-651`, `coordinator/types.py:438,1171`, `sensor.py:478` | Tasks 1–2 — measure and publish kWh/cost exported while negative (#871 step 0) |
| `coordinator/sink_verdicts.py` (new) | Task 3 — the one pure place: `SinkVerdict`, `sink_verdicts()`, `next_closed_window()` |
| `coordinator/charger_types.py`, `coordinator/build_view.py:193-199`, `coordinator/coordinator.py:_build_fleet_cycle_state` | Task 4 — the verdicts ride the fleet state |
| `coordinator/export_guard.py` (new) | Task 5 — the limit at the meter with hysteresis |
| `coordinator/charger_types.py` (`BatteryIntent`, `BatteryDecision`), `coordinator/actuate_battery.py` | Task 6 — `LIMIT_EXPORT` / `RELEASE_EXPORT` through the observer seam |
| `coordinator/battery_adapters/base.py`, `huawei.py`, `deye.py`, `generic.py` | Task 7 — the two verbs per brand |
| `coordinator/coordinator.py` (guard tick, probe hold, BatteryView) | Task 8 — wiring |
| `coordinator/decide_battery.py:430-460`, `coordinator/decide.py:254-262` | Tasks 9–10 — #879 house sink, #892 morning EV window |
| `coordinator/sink_verdicts.py`, `coordinator/coordinator.py:_today_pacing_ledger` | Task 11 — #926 headroom before the meter closes |
| `coordinator/day_ledger.py:111`, `coordinator/surplus_controller.py` | Task 12 — #871 steps 1–2: unclamp, absorb |
| `cleanup.py`, `__init__.py:3076` | Task 13 — hand-back on unload / disable / removal |
| `switch.py`, `persisted_flags.py`, `number.py`, `sensor.py`, `strings.json`, `translations/*.json` | Task 14 — the surface, all in the GUI |
| `coordinator/today_plan.py`, `dashboard/card/src/cards/sem-today-plan-card.js`, `dashboard/translations.json` | Task 15 — plan rows |
| `tests/test_921_sink_scenario.py` | Task 16 — the scenario rig |
| `docs/USER_GUIDE.md`, `README.md`, `CHANGELOG.md` | Task 17 |

---

### Task 1: Measure the exposure (#871 step 0)

**Files:**
- Modify: `coordinator/energy_calculator.py:643-651`
- Modify: `coordinator/types.py:438` (field), `:1171` (`to_dict`)
- Test: `tests/test_921_measure_negative_export.py`

- [ ] **Step 1: Write the failing test**

```python
"""#871 step 0 (arc #921) — measure what a negative export price costs before acting."""
from types import SimpleNamespace

from custom_components.solar_energy_management.coordinator.energy_calculator import (
    EnergyCalculator,
)


def _calc(export_rate):
    calc = EnergyCalculator.__new__(EnergyCalculator)
    calc._daily = {}; calc._monthly = {}; calc._yearly = {}
    calc._daily_cost = {}; calc._monthly_cost = {}; calc._yearly_cost = {}
    calc._export_rate = export_rate
    return calc


def _power(export_w):
    return SimpleNamespace(
        solar_power=export_w, grid_import_power=0.0, grid_export_power=export_w,
        battery_charge_power=0.0, battery_discharge_power=0.0, ev_power=0.0,
        home_consumption_power=0.0, grid_power=export_w, battery_power=0.0,
    )


class TestNegativeExportIsCounted:
    def test_a_negative_rate_accrues_kwh_and_cost(self):
        calc = _calc(-0.05)
        energy = calc.calculate(_power(2000.0), interval_hours=1.0)
        assert energy.daily_grid_export_negative == 2.0
        assert round(energy.daily_grid_export_negative_cost, 3) == 0.10

    def test_a_positive_rate_accrues_nothing(self):
        energy = _calc(0.075).calculate(_power(2000.0), interval_hours=1.0)
        assert energy.daily_grid_export_negative == 0.0
        assert energy.daily_grid_export_negative_cost == 0.0

    def test_zero_is_worthless_not_costly(self):
        energy = _calc(0.0).calculate(_power(2000.0), interval_hours=1.0)
        assert energy.daily_grid_export_negative == 0.0
```

`EnergyCalculator`'s constructor and the internal store names differ per version: before writing `_calc`, run `grep -n "def __init__\|self\._daily\b\|def _accumulate\|def _get_daily" coordinator/energy_calculator.py | head` and copy the REAL attribute names into the fixture; the shape above is the contract, the names are whatever the file says. If a constructor with a `hass` stub is simpler, use `grep -rn "EnergyCalculator(" tests/ | head -3` and copy one.

- [ ] **Step 2: Run it and watch it fail**

`semtest tests/test_921_measure_negative_export.py` → `AttributeError: 'EnergyTotals' object has no attribute 'daily_grid_export_negative'`.

- [ ] **Step 3: Add the fields**

`coordinator/types.py`, directly after `daily_grid_export: float = 0.0` (`:438`):

```python
    #: (#871, arc #921) kWh exported while the export rate was NEGATIVE — energy
    #: the meter charged for instead of paying for. Zero on every fixed-tariff
    #: install, which is all of them today; the counter exists so the cost of
    #: NOT acting is measurable before anything acts.
    daily_grid_export_negative: float = 0.0
    daily_grid_export_negative_cost: float = 0.0
```

`to_dict` (`:1171`), after `"daily_grid_export_energy": self.energy.daily_grid_export,`:

```python
            "daily_grid_export_negative_kwh": self.energy.daily_grid_export_negative,
            "daily_grid_export_negative_cost": self.energy.daily_grid_export_negative_cost,
```

- [ ] **Step 4: Accrue at the existing seam**

`coordinator/energy_calculator.py`, inside `if power.grid_export_power >= MIN_POWER_THRESHOLD:` (`:643`), after the existing `_accumulate("grid_export", …)` call:

```python
            # (#871) The same kWh counted again when the meter was hostile —
            # a separate key, not a sign on the export total, because that
            # total is what a user reads as "what I sent out".
            if float(getattr(self, "_export_rate", 0.0) or 0.0) < 0:
                self._accumulate("grid_export_negative", today, month_key,
                                 year_key, export_increment)
                self._accumulate_cost("cost_export_negative", today, month_key,
                                      year_key,
                                      export_increment * abs(self._export_rate))
```

and beside `energy.daily_grid_export = self._get_daily("grid_export", today)` (`:651`):

```python
        energy.daily_grid_export_negative = self._get_daily("grid_export_negative", today)
        energy.daily_grid_export_negative_cost = self._get_daily_cost("cost_export_negative", today)
```

If `_accumulate_cost` / `_get_daily_cost` do not exist under those names, `grep -n "cost" coordinator/energy_calculator.py | head -20` and use the file's own cost accumulator; do not invent a second store.

- [ ] **Step 5: Run** `semtest tests/test_921_measure_negative_export.py` → 3 passed. Then `semtest tests/test_energy_calculator*.py tests/test_628*.py` → all pass (the export seam has an identity test; nothing there may move).

- [ ] **Step 6: Commit**

```bash
git add coordinator/energy_calculator.py coordinator/types.py tests/test_921_measure_negative_export.py
git commit -m "feat(#871): count what a negative export price actually costs

Step 0 of arc #921, deliberately first: nothing recorded that export went
negative and SEM kept pushing into it. Zero on every fixed-tariff install."
```

---

### Task 2: Publish the exposure

**Files:**
- Modify: `sensor.py:478-483` (after `daily_grid_export_energy`)
- Modify: `strings.json` (`entity.sensor`), `translations/*.json` ×16
- Test: `tests/test_921_measure_negative_export.py` (append)

- [ ] **Step 1: Write the failing test**

```python
class TestTheExposureIsVisible:
    def test_both_sensors_are_declared(self):
        from custom_components.solar_energy_management import sensor as sensor_mod
        keys = {d.key for d in sensor_mod.SENSOR_TYPES}
        assert {"daily_grid_export_negative_kwh", "daily_grid_export_negative_cost"} <= keys
```

- [ ] **Step 2: Run it and watch it fail** → `AssertionError`.

- [ ] **Step 3: Add the descriptions** after the `daily_grid_export_energy` description in `SENSOR_TYPES` (`sensor.py:478`):

```python
    # (#871, arc #921) What a hostile meter cost today. Both stay at 0.0 on a
    # fixed feed-in tariff, which is every install until someone opts into spot.
    SensorEntityDescription(
        key="daily_grid_export_negative_kwh",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorEntityDescription(
        key="daily_grid_export_negative_cost",
        state_class=SensorStateClass.TOTAL,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
```

- [ ] **Step 4: Names in all 17 files.** `strings.json` under `entity.sensor` and every `translations/<lang>.json` (cs da de en es fi fr hu it nl no pl pt ro sv zh-Hans): keys `daily_grid_export_negative_kwh` → "Exported while price negative", `daily_grid_export_negative_cost` → "Cost of exporting at a negative price" (translate per language; German: "Eingespeist bei negativem Preis" / "Kosten der Einspeisung bei negativem Preis"). Run `semtest tests/test_674_translation_parity.py` — it fails on any file left out.

- [ ] **Step 5: Run** `semtest tests/test_921_measure_negative_export.py tests/test_674_translation_parity.py` → all pass.

- [ ] **Step 6: Commit** `git add sensor.py strings.json translations tests/test_921_measure_negative_export.py && git commit -m "feat(#871): publish the negative-export exposure as two diagnostics"`

---

### Task 3: The one pure place — `sink_verdicts`

**Files:**
- Create: `coordinator/sink_verdicts.py`
- Test: `tests/test_921_sink_verdicts.py`

- [ ] **Step 1: Write the failing test**

```python
"""arc #921 — a sink has a STATE, not a price. One verdict per sink per cycle."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from custom_components.solar_energy_management.coordinator.sink_verdicts import (
    CLOSED, HELD, OPEN, next_closed_window, sink_verdicts,
)

TZ = timezone.utc
NOW = datetime(2026, 9, 15, 12, 0, tzinfo=TZ)


def _pp(hour, level):
    return SimpleNamespace(timestamp=NOW.replace(hour=hour),
                           level=SimpleNamespace(value=level))


def _verdicts(**kw):
    base = dict(now=NOW, tariff_level="normal", upcoming=[], export_rate_known=True,
                export_guard_enabled=False, house_sink_enabled=False,
                morning_window_enabled=False, departure=None, morning_hours=2.0,
                forecast_refills_pack=False, pacing_horizon_end=None)
    base.update(kw)
    return sink_verdicts(**base)


class TestGridExport:
    def test_negative_level_with_the_guard_on_closes_the_grid(self):
        v = _verdicts(tariff_level="negative", export_guard_enabled=True)
        assert v["grid_export"].state == CLOSED

    def test_negative_level_with_the_guard_off_stays_open(self):
        assert _verdicts(tariff_level="negative").state_of("grid_export") == OPEN

    def test_an_unknown_price_is_open_never_closed(self):
        v = _verdicts(tariff_level="negative", export_guard_enabled=True,
                      export_rate_known=False)
        assert v["grid_export"].state == OPEN
        assert "unknown" in v["grid_export"].reason

    def test_zero_is_worthless_not_hostile(self):
        assert _verdicts(tariff_level="cheap", export_guard_enabled=True)["grid_export"].state == OPEN


class TestNextClosedWindow:
    def test_finds_the_first_negative_run_after_now(self):
        ups = [_pp(12, "normal"), _pp(13, "negative"), _pp(14, "negative"), _pp(15, "cheap")]
        assert next_closed_window(NOW, ups) == (NOW.replace(hour=13), NOW.replace(hour=15))

    def test_no_negative_hour_is_none(self):
        assert next_closed_window(NOW, [_pp(12, "normal"), _pp(13, "cheap")]) is None

    def test_a_run_already_open_starts_now(self):
        ups = [_pp(12, "negative"), _pp(13, "negative"), _pp(14, "normal")]
        assert next_closed_window(NOW, ups) == (NOW, NOW.replace(hour=14))

    def test_an_empty_curve_is_none(self):
        assert next_closed_window(NOW, []) is None
        assert next_closed_window(NOW, None) is None


class TestBatteryHeadroom:
    def test_held_when_a_closed_window_starts_inside_the_pacing_horizon(self):
        ups = [_pp(12, "normal"), _pp(15, "negative"), _pp(16, "negative")]
        v = _verdicts(upcoming=ups, export_guard_enabled=True,
                      pacing_horizon_end=NOW.replace(hour=19))
        assert v["battery"].state == HELD
        assert v["battery"].until == NOW.replace(hour=15)

    def test_open_when_the_closed_window_is_past_the_horizon(self):
        ups = [_pp(20, "negative")]
        v = _verdicts(upcoming=ups, export_guard_enabled=True,
                      pacing_horizon_end=NOW.replace(hour=19))
        assert v["battery"].state == OPEN


class TestHouse:
    def test_off_by_default(self):
        assert _verdicts(tariff_level="expensive")["house"].state == OPEN

    def test_held_in_cheap_hours(self):
        assert _verdicts(tariff_level="cheap", house_sink_enabled=True)["house"].state == HELD

    def test_held_in_negative_hours(self):
        assert _verdicts(tariff_level="negative", house_sink_enabled=True)["house"].state == HELD

    def test_open_in_expensive_hours(self):
        assert _verdicts(tariff_level="expensive", house_sink_enabled=True)["house"].state == OPEN


class TestMorningEv:
    def test_open_inside_the_window_when_the_forecast_refills(self):
        v = _verdicts(morning_window_enabled=True, departure=NOW + timedelta(hours=1),
                      forecast_refills_pack=True)
        assert v["ev"].state == OPEN
        assert v["ev"].until == NOW + timedelta(hours=1)

    def test_closed_outside_the_window(self):
        v = _verdicts(morning_window_enabled=True, departure=NOW + timedelta(hours=5),
                      forecast_refills_pack=True)
        assert v["ev"].state == HELD

    def test_held_when_the_forecast_will_not_refill(self):
        v = _verdicts(morning_window_enabled=True, departure=NOW + timedelta(hours=1),
                      forecast_refills_pack=False)
        assert v["ev"].state == HELD

    def test_no_departure_no_window(self):
        v = _verdicts(morning_window_enabled=True, departure=None, forecast_refills_pack=True)
        assert v["ev"].state == HELD

    def test_off_by_default_is_the_legacy_rule(self):
        v = _verdicts(departure=NOW + timedelta(hours=1), forecast_refills_pack=True)
        assert v["ev"].state == OPEN and v["ev"].reason == "legacy assist rule"


class TestTheDictIsComplete:
    def test_every_sink_has_a_verdict(self):
        v = _verdicts()
        assert set(v) == {"grid_export", "battery", "house", "ev"}
        for sv in v.values():
            assert sv.state in (OPEN, HELD, CLOSED) and sv.reason
```

- [ ] **Step 2: Run it and watch it fail** → `ModuleNotFoundError`.

- [ ] **Step 3: Write the module**

```python
"""arc #921 — a sink has a STATE, not a price.

SEM's balance layer reasons about energy. Price already decides WHEN in SEM
(cheap hours, negative-import force charge, the arbitrage floor) through the
LEVEL the tariff provider classifies; it must not start deciding HOW MUCH.
So the arc's one model is a per-cycle verdict per destination a kWh can take:

    OPEN   — energy may go there (the sink's own gates still apply)
    HELD   — it may, but not now: keep the energy where it is
    CLOSED — it may not; for the grid this is ENFORCED by the export guard

Computed ONCE per cycle here, threaded into ``FleetCycleState``, and consumed
by the routers (surplus, decide_battery, pacing, the plan). No consumer
re-derives a verdict from a price. Pure: no hass, no clock of its own.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Tuple

OPEN = "open"
HELD = "held"
CLOSED = "closed"

SINKS = ("grid_export", "battery", "house", "ev")


@dataclass(frozen=True)
class SinkVerdict:
    sink: str
    state: str
    reason: str
    until: Optional[datetime] = None

    def to_dict(self) -> Dict[str, Any]:
        return {"sink": self.sink, "state": self.state, "reason": self.reason,
                "until": self.until.isoformat() if self.until else None}


class _Verdicts(dict):
    def state_of(self, sink: str) -> str:
        return self[sink].state


def _level(p) -> str:
    lv = getattr(p, "level", None)
    return str(getattr(lv, "value", lv) or "").lower()


def next_closed_window(now: datetime, upcoming) -> Optional[Tuple[datetime, datetime]]:
    """The first run of NEGATIVE slots at or after ``now`` as ``(start, end)``.

    ``end`` is the closing boundary — the first non-negative slot's start, or
    the last negative slot plus its cadence when the curve ends negative. A run
    already in progress starts at ``now``. ``None`` when there is none, when the
    curve is empty, and when the curve cannot be read — absence is OPEN.
    """
    pts = [p for p in (upcoming or []) if getattr(p, "timestamp", None) is not None]
    pts.sort(key=lambda p: p.timestamp)
    if not pts:
        return None
    gaps = [b.timestamp - a.timestamp for a, b in zip(pts, pts[1:]) if b.timestamp > a.timestamp]
    slot = min(gaps) if gaps else timedelta(hours=1)
    start = end = None
    for p in pts:
        if p.timestamp + slot <= now:
            continue                                   # already over
        if _level(p) == "negative":
            if start is None:
                start = max(now, p.timestamp)
            end = p.timestamp + slot
        elif start is not None:
            return start, p.timestamp
    return (start, end) if start is not None else None


def sink_verdicts(*, now: datetime, tariff_level: Optional[str], upcoming,
                  export_rate_known: bool, export_guard_enabled: bool,
                  house_sink_enabled: bool, morning_window_enabled: bool,
                  departure: Optional[datetime], morning_hours: float,
                  forecast_refills_pack: bool,
                  pacing_horizon_end: Optional[datetime]) -> Dict[str, SinkVerdict]:
    """One verdict per sink. Unknown is OPEN, never CLOSED (#925, class 86)."""
    level = str(tariff_level or "").lower()
    out: _Verdicts = _Verdicts()

    # grid export — CLOSED only on a READ negative level with the guard on
    if not export_guard_enabled:
        out["grid_export"] = SinkVerdict("grid_export", OPEN, "export guard off")
    elif not export_rate_known:
        out["grid_export"] = SinkVerdict("grid_export", OPEN, "export price unknown — not closing on a guess")
    elif level == "negative":
        win = next_closed_window(now, upcoming)
        out["grid_export"] = SinkVerdict("grid_export", CLOSED, "export price negative — the meter is closed",
                                         until=win[1] if win else None)
    else:
        win = next_closed_window(now, upcoming)
        out["grid_export"] = SinkVerdict("grid_export", OPEN, "export price not negative",
                                         until=win[0] if win else None)

    # battery — HELD headroom when a CLOSED window starts inside the pacing horizon (#926)
    win = next_closed_window(now, upcoming) if (export_guard_enabled and export_rate_known) else None
    if win and pacing_horizon_end is not None and win[0] < pacing_horizon_end:
        out["battery"] = SinkVerdict("battery", HELD, "hold headroom — the meter closes before the day ends",
                                     until=win[0])
    else:
        out["battery"] = SinkVerdict("battery", OPEN, "fill as the day model says")

    # house — the pack is spent on the house in EXPENSIVE hours, kept in CHEAP/NEGATIVE ones (#879)
    if not house_sink_enabled:
        out["house"] = SinkVerdict("house", OPEN, "house sink off — inverter self-consumption rule")
    elif level in ("cheap", "very_cheap", "negative"):
        out["house"] = SinkVerdict("house", HELD, f"{level} hour — let the house import, keep the pack")
    else:
        out["house"] = SinkVerdict("house", OPEN, f"{level or 'unknown'} hour — the pack may cover the house")

    # ev — a morning window before departure, if the sun refills the pack today (#892)
    if not morning_window_enabled:
        out["ev"] = SinkVerdict("ev", OPEN, "legacy assist rule")
    elif departure is None:
        out["ev"] = SinkVerdict("ev", HELD, "no departure time configured")
    elif not forecast_refills_pack:
        out["ev"] = SinkVerdict("ev", HELD, "forecast will not refill the pack today")
    elif now < departure - timedelta(hours=float(morning_hours or 0.0)) or now >= departure:
        out["ev"] = SinkVerdict("ev", HELD, "outside the morning window",
                                until=departure - timedelta(hours=float(morning_hours or 0.0)))
    else:
        out["ev"] = SinkVerdict("ev", OPEN, "morning window — empty the pack into the car", until=departure)
    return out
```

- [ ] **Step 4: Run** `semtest tests/test_921_sink_verdicts.py` → all pass. Lint.

- [ ] **Step 5: Commit** `git add coordinator/sink_verdicts.py tests/test_921_sink_verdicts.py && git commit -m "feat(#921): one verdict per sink per cycle — a sink has a state, not a price"`

---

### Task 4: The verdicts ride the fleet state

**Files:**
- Modify: `coordinator/charger_types.py` (`FleetCycleState` beside `curtailment_grant_w: float = 0.0` at `:970`; `FleetContext` beside `:706`)
- Modify: `coordinator/build_view.py:193-199`
- Modify: `coordinator/coordinator.py` `_build_fleet_cycle_state` (the `FleetCycleState(` call ending at `curtailment_grant_w=self._curtailment_grant_w(power),`)
- Test: `tests/test_921_sink_verdicts.py` (append)

- [ ] **Step 1: Write the failing test**

```python
class TestTheVerdictsRideTheFleetState:
    def test_fleet_state_and_context_carry_them(self):
        from custom_components.solar_energy_management.coordinator.charger_types import (
            FleetContext, FleetCycleState,
        )
        assert FleetCycleState.__dataclass_fields__["sink_verdicts"].default_factory() == {}
        assert FleetContext().sink_verdicts == {}

    def test_build_view_threads_them(self):
        from custom_components.solar_energy_management.coordinator import build_view
        from tests.ast_contracts import call_kwargs
        kwargs = call_kwargs(build_view.build_charger_view, "FleetContext")
        assert kwargs and "sink_verdicts" in kwargs[0]

    def test_the_coordinator_computes_them_in_one_place(self):
        from tests.ast_contracts import call_sites
        sites = call_sites("sink_verdicts")
        assert [s[0] for s in sites] == ["coordinator/coordinator.py"], sites
```

Confirm the import path of `ast_contracts` first: `grep -rn "ast_contracts import" tests/ | head -2` and the builder's name: `grep -n "^def build_charger_view" coordinator/build_view.py`.

- [ ] **Step 2: Run and watch it fail.**

- [ ] **Step 3: Add the fields**

`FleetCycleState` (after `curtailment_grant_w: float = 0.0` at `:970`) and `FleetContext` (after `:706`'s docstring):

```python
    #: (arc #921) the cycle's sink verdicts — {sink: SinkVerdict}. Empty until
    #: computed, and an empty dict reads as "every sink OPEN" everywhere.
    sink_verdicts: Dict[str, Any] = field(default_factory=dict)
```

(`field` and `Dict` are already imported in `charger_types.py`; verify with `grep -n "^from dataclasses\|^from typing" coordinator/charger_types.py`.)

`build_view.py`, beside the `curtailment_grant_w=` copy (`:195`):

```python
        # (arc #921) the sink verdicts ride the same one-place thread.
        sink_verdicts=dict(getattr(fleet_state, "sink_verdicts", None) or {}),
```

- [ ] **Step 4: Compute them in `_build_fleet_cycle_state`**, right before the `return FleetCycleState(` call; read the export price with the SAME tri-state the forecast sell uses (`coordinator.py:7478-7490`):

```python
        # (arc #921) one verdict per sink, computed here and nowhere else.
        from .sink_verdicts import sink_verdicts as _sink_verdicts
        _xr_known = False
        try:
            _prov = getattr(self, "_tariff_provider", None)
            if _prov is not None and hasattr(_prov, "get_current_export_rate"):
                float(_prov.get_current_export_rate()); _xr_known = True
        except Exception:  # noqa: BLE001 — unreadable is a state, not 0
            _xr_known = False
        try:
            _ups = getattr(_prov.get_tariff_data(), "upcoming_prices", None) if _prov else None
        except Exception:  # noqa: BLE001
            _ups = None
        _verdicts = _sink_verdicts(
            now=dt_util.now(), tariff_level=tariff_level, upcoming=_ups,
            export_rate_known=_xr_known,
            export_guard_enabled=bool(self.config.get("export_guard_enabled", False)),
            house_sink_enabled=bool(self.config.get("battery_house_sink_enabled", False)),
            morning_window_enabled=bool(self.config.get("ev_morning_window_enabled", False)),
            departure=self._ev_departure_dt(),
            morning_hours=float(self.config.get("ev_morning_window_hours", 2.0) or 2.0),
            forecast_refills_pack=self._forecast_refills_pack(),
            pacing_horizon_end=self._pacing_horizon_end(),
        )
        self._sink_verdicts = _verdicts
```

and `sink_verdicts=_verdicts,` in the `FleetCycleState(` call. Add three small helpers on the coordinator next to `_today_pacing_ledger` (`:6868`):

```python
    def _ev_departure_dt(self):
        """(#892) The configured departure as a datetime today (tomorrow if past), or None."""
        ent = self.config.get("ev_departure_time_entity", "")
        st = self.hass.states.get(ent) if ent else None
        if not st or st.state in ("unknown", "unavailable", ""):
            return None
        try:
            h, m = (int(x) for x in str(st.state).split(":")[:2])
        except (TypeError, ValueError):
            return None
        now = dt_util.now()
        dep = now.replace(hour=h, minute=m, second=0, microsecond=0)
        return dep if dep > now else dep + timedelta(days=1)

    def _forecast_refills_pack(self) -> bool:
        """(#892) Will today's remaining forecast put back what the morning takes?
        Conservative: unknown forecast → False."""
        _fd = getattr(getattr(self, "_forecast_reader", None), "forecast_data", None)
        remaining = getattr(_fd, "forecast_remaining_today_kwh", None)
        cap = float(getattr(self, "battery_capacity_kwh", 0.0) or 0.0)
        if remaining is None or cap <= 0:
            return False
        floor = float(self.config.get("battery_morning_drain_floor_soc", 50.0) or 50.0)
        return float(remaining) >= cap * (1.0 - floor / 100.0)

    def _pacing_horizon_end(self):
        """(#926) Sunset+10 today as a datetime — the pacer's own horizon."""
        try:
            h, m = (int(x) for x in self.time_manager.get_sunset_plus_10_time().split(":"))
            return dt_util.now().replace(hour=h, minute=m, second=0, microsecond=0)
        except Exception:  # noqa: BLE001
            return None
```

- [ ] **Step 5: Run** `semtest tests/test_921_sink_verdicts.py tests/test_multi_charger_control.py tests/test_589_percharger_astguard.py tests/test_873_cycle_executes.py` → all pass; no decision changes yet.

- [ ] **Step 6: Commit** `git add coordinator/charger_types.py coordinator/build_view.py coordinator/coordinator.py tests/test_921_sink_verdicts.py && git commit -m "feat(#921): the sink verdicts ride the fleet state — computed once, read everywhere"`

---

### Task 5: `ExportGuard` — the limit at the meter

**Files:**
- Create: `coordinator/export_guard.py`
- Test: `tests/test_921_export_guard.py`

- [ ] **Step 1: Write the failing test**

```python
"""#955 — a limit at the meter, mirroring the peak guard. Pure; the clock is fed."""
from custom_components.solar_energy_management.coordinator.export_guard import (
    ENGAGE_HOLD_S, EXPORT_EPS_W, RELEASE_HOLD_S, ExportGuard,
)
from custom_components.solar_energy_management.coordinator.sink_verdicts import CLOSED, OPEN


def _run(guard, seq):
    """seq: [(t, verdict_state, export_w)] → list of intents."""
    return [guard.update(t, state, export_w).intent for t, state, export_w in seq]


class TestHysteresis:
    def test_engages_only_after_the_closed_verdict_holds(self):
        g = ExportGuard()
        out = _run(g, [(0, CLOSED, 500.0), (ENGAGE_HOLD_S - 1, CLOSED, 500.0),
                       (ENGAGE_HOLD_S + 1, CLOSED, 500.0)])
        assert out == [None, None, "limit_export"]
        assert g.state == "engaged"

    def test_a_flapping_verdict_never_engages(self):
        g = ExportGuard()
        out = _run(g, [(0, CLOSED, 500.0), (60, OPEN, 500.0), (120, CLOSED, 500.0), (180, OPEN, 500.0)])
        assert set(out) == {None}

    def test_releases_only_after_open_holds(self):
        g = ExportGuard()
        _run(g, [(0, CLOSED, 500.0), (ENGAGE_HOLD_S + 1, CLOSED, 500.0)])
        out = _run(g, [(1000, OPEN, 0.0), (1000 + RELEASE_HOLD_S - 1, OPEN, 0.0),
                       (1000 + RELEASE_HOLD_S + 1, OPEN, 0.0)])
        assert out == [None, None, "release_export"]
        assert g.state == "idle"


class TestLastNotFirst:
    def test_no_measured_export_no_engagement(self):
        """The sinks absorbed everything this cycle — nothing to clip."""
        g = ExportGuard()
        out = _run(g, [(0, CLOSED, 0.0), (ENGAGE_HOLD_S + 1, CLOSED, EXPORT_EPS_W - 1)])
        assert out == [None, None]
        assert g.state == "holding"

    def test_export_reappearing_while_closed_engages(self):
        g = ExportGuard()
        _run(g, [(0, CLOSED, 0.0), (ENGAGE_HOLD_S + 1, CLOSED, 0.0)])
        assert g.update(ENGAGE_HOLD_S + 2, CLOSED, 900.0).intent == "limit_export"


class TestRefusal:
    def test_refused_is_sticky_until_release(self):
        g = ExportGuard()
        _run(g, [(0, CLOSED, 500.0), (ENGAGE_HOLD_S + 1, CLOSED, 500.0)])
        g.report_refused("no export control on this brand")
        assert g.state == "refused"
        assert g.update(ENGAGE_HOLD_S + 30, CLOSED, 500.0).intent is None
        assert g.update(5000, OPEN, 0.0).intent is None
        assert g.update(5000 + RELEASE_HOLD_S + 1, OPEN, 0.0).intent is None
        assert g.state == "idle"

    def test_three_refusals_ask_for_a_repair(self):
        g = ExportGuard()
        for _ in range(3):
            g.report_refused("adapter raised")
        assert g.repair_wanted is True


class TestUnknownIsNotHostile:
    def test_open_from_the_start_does_nothing(self):
        g = ExportGuard()
        assert _run(g, [(0, OPEN, 5000.0), (600, OPEN, 5000.0)]) == [None, None]
        assert g.state == "idle"
```

- [ ] **Step 2: Run and watch it fail** → `ModuleNotFoundError`.

- [ ] **Step 3: Write the module**

```python
"""#955 — the export guard: a limit at the meter, mirroring ``peak_guard``.

The peak guard caps IMPORT per billing slot and every device below it obeys.
This is the same rule mirrored: while the grid-export sink is CLOSED (the
tariff level is NEGATIVE — read, not guessed), cap export at zero. Nothing
here reasons about money: a verdict says the meter is closed and SEM honours
it the way it honours a reserve SOC.

Three rules, each load-bearing:

* **Hysteresis both ways.** Spot prices cross zero repeatedly; a curtailment
  that engages on every crossing is the flapping this project spent months
  removing and a measurable harvest loss. CLOSED must hold ``ENGAGE_HOLD_S``
  before the inverter is touched, OPEN must hold ``RELEASE_HOLD_S`` before it
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

from dataclasses import dataclass
from typing import Optional

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


class ExportGuard:
    """States: idle → holding → engaged → releasing → idle; refused from
    holding/engaged, cleared on the OPEN side once RELEASE_HOLD_S holds."""

    def __init__(self) -> None:
        self.state: str = "idle"
        self.reason: str = "export guard idle"
        self._closed_since: Optional[float] = None
        self._open_since: Optional[float] = None
        self._refusals: int = 0
        self.repair_wanted: bool = False

    def report_refused(self, why: str) -> None:
        self._refusals += 1
        self.state = "refused"
        self.reason = f"export cut refused: {why}"
        if self._refusals >= REFUSALS_FOR_REPAIR:
            self.repair_wanted = True

    def update(self, now: float, verdict_state: str, export_w: Optional[float]) -> ExportCommand:
        closed = verdict_state == "closed"
        exp = float(export_w or 0.0)
        if closed:
            self._open_since = None
            if self._closed_since is None:
                self._closed_since = now
            held = now - self._closed_since
            if self.state == "refused":
                return ExportCommand(None, 0.0, self.reason)
            if self.state == "engaged":
                return ExportCommand(None, 0.0, "export cut holding — the meter is closed")
            if held < ENGAGE_HOLD_S:
                self.state = "holding"
                self.reason = f"meter closed for {held:.0f}s of {ENGAGE_HOLD_S:.0f}s — waiting it out"
                return ExportCommand(None, 0.0, self.reason)
            if exp < EXPORT_EPS_W:
                self.state = "holding"
                self.reason = "meter closed and the sinks absorb everything — nothing to clip"
                return ExportCommand(None, 0.0, self.reason)
            self.state = "engaged"
            self.reason = f"export {exp:.0f} W into a closed meter — cutting to 0 W"
            return ExportCommand(LIMIT_EXPORT, 0.0, self.reason)
        # OPEN (or HELD, which is not a grid state) — release with hysteresis
        self._closed_since = None
        if self.state in ("idle",):
            return ExportCommand(None, 0.0, "export guard idle")
        if self._open_since is None:
            self._open_since = now
        held = now - self._open_since
        if held < RELEASE_HOLD_S:
            if self.state != "refused":
                self.state = "releasing"
            self.reason = f"meter open for {held:.0f}s of {RELEASE_HOLD_S:.0f}s — holding the cut"
            return ExportCommand(None, 0.0, self.reason)
        was_refused = self.state == "refused"
        self.state = "idle"
        self.reason = "export guard idle"
        self._refusals = 0
        self.repair_wanted = False
        self._open_since = None
        return ExportCommand(None if was_refused else RELEASE_EXPORT, 0.0, "meter open — releasing the cut")
```

- [ ] **Step 4: Run** `semtest tests/test_921_export_guard.py` → all pass. Lint.

- [ ] **Step 5: Commit** `git add coordinator/export_guard.py tests/test_921_export_guard.py && git commit -m "feat(#955): the export guard — a limit at the meter with hysteresis, last not first"`

---

### Task 6: Intents through the observer seam

**Files:**
- Modify: `coordinator/charger_types.py` (`BatteryIntent` after `STOP_FORCE_DISCHARGE = "stop_force_discharge"`; `BatteryDecision` after `floor_soc: float = 0.0`)
- Modify: `coordinator/actuate_battery.py:37-39` (`_observe` watts map), `:245-260` (a new branch after `STOP_FORCE_DISCHARGE`)
- Test: `tests/test_921_export_guard.py` (append)

- [ ] **Step 1: Write the failing test**

```python
import pytest
from unittest.mock import AsyncMock, MagicMock

from custom_components.solar_energy_management.coordinator.actuate_battery import actuate_battery
from custom_components.solar_energy_management.coordinator.charger_types import (
    BatteryDecision, BatteryIntent,
)


def _adapter():
    a = MagicMock()
    a.command_limit_export = AsyncMock()
    a.command_release_export = AsyncMock()
    a.last_intent = None
    return a


@pytest.mark.asyncio
class TestTheIntentsDispatch:
    async def test_limit_export_calls_the_verb_with_the_cap(self):
        a = _adapter()
        await actuate_battery(BatteryDecision("b1", BatteryIntent.LIMIT_EXPORT,
                                              export_limit_w=0.0, reason="closed"), a)
        a.command_limit_export.assert_awaited_once_with(0.0)

    async def test_release_export_calls_the_release_verb(self):
        a = _adapter()
        await actuate_battery(BatteryDecision("b1", BatteryIntent.RELEASE_EXPORT, reason="open"), a)
        a.command_release_export.assert_awaited_once()

    async def test_observer_mode_calls_nothing_and_records_a_would(self):
        a = _adapter(); ctl = MagicMock()
        await actuate_battery(BatteryDecision("b1", BatteryIntent.LIMIT_EXPORT,
                                              export_limit_w=0.0, reason="closed"),
                              a, observer=True, controller=ctl)
        a.command_limit_export.assert_not_awaited()
        assert ctl.publish_observer_decision.call_args.kwargs["action"] == "limit_export"

    async def test_a_brand_without_the_verb_is_a_recorded_refusal_not_a_crash(self):
        a = _adapter()
        a.command_limit_export = AsyncMock(side_effect=NotImplementedError("no export control"))
        await actuate_battery(BatteryDecision("b1", BatteryIntent.LIMIT_EXPORT,
                                              export_limit_w=0.0, reason="closed"), a)
        assert "no export control" in str(a._last_error)
```

- [ ] **Step 2: Run and watch it fail** → `AttributeError: LIMIT_EXPORT`.

- [ ] **Step 3: Add the intents and the field**

`BatteryIntent`, after `STOP_FORCE_DISCHARGE`:

```python
    LIMIT_EXPORT = "limit_export"
    """(#955) Cap the inverter's grid feed-in at ``export_limit_w`` — 0 W for a
    closed meter. Issued by the export guard, last not first."""

    RELEASE_EXPORT = "release_export"
    """(#955) Put the feed-in limit back to what SEM found."""
```

`BatteryDecision`, after `floor_soc: float = 0.0`:

```python
    export_limit_w: float = 0.0
```

`actuate_battery.py` `_observe` watts map (`:37-39`), add `BatteryIntent.LIMIT_EXPORT: decision.export_limit_w,`. Then a branch after the `STOP_FORCE_DISCHARGE` branch:

```python
    if decision.intent in (BatteryIntent.LIMIT_EXPORT, BatteryIntent.RELEASE_EXPORT):
        # (#955) A brand without an export control REFUSES here — recorded on
        # the adapter so the guard can say so — and never raises out of the
        # cycle. The guard reads ``_last_error`` and reports the refusal.
        try:
            if decision.intent is BatteryIntent.LIMIT_EXPORT:
                await adapter.command_limit_export(float(decision.export_limit_w or 0.0))
            else:
                await adapter.command_release_export()
            adapter._last_error = None
        except NotImplementedError as exc:
            adapter._last_error = f"export control not available: {exc}"
        except Exception as exc:  # noqa: BLE001 — a refused cut is a state, not a crash
            adapter._last_error = f"export control failed: {exc}"
        log_on_change(
            _LOGGER, f"actuate:{decision.battery_id}", logging.INFO,
            "actuate_battery(%s): %s %.0f W — %s", decision.battery_id,
            decision.intent.value.upper(), float(decision.export_limit_w or 0.0), decision.reason,
        )
        return
```

- [ ] **Step 4: Run** `semtest tests/test_921_export_guard.py tests/test_actuate_battery*.py tests/test_818*.py` → all pass (`_POWER_DERIVED_INTENTS` is untouched: these intents are decided from a verdict, not a power number).

- [ ] **Step 5: Commit** `git add coordinator/charger_types.py coordinator/actuate_battery.py tests/test_921_export_guard.py && git commit -m "feat(#955): LIMIT_EXPORT / RELEASE_EXPORT through the battery actuation seam — observer cuts here"`

---

### Task 7: The two verbs, per brand

**Files:**
- Modify: `coordinator/battery_adapters/base.py` (after `command_off`, `:443+`)
- Modify: `coordinator/battery_adapters/huawei.py`, `deye.py`, `generic.py`
- Test: `tests/test_921_export_adapters.py`

- [ ] **Step 1: Write the failing test**

```python
"""#955 — each brand's dialect for 'stop feeding the grid' and 'put it back'."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.solar_energy_management.coordinator.battery_adapters.base import (
    BatteryControlAdapter,
)
from custom_components.solar_energy_management.coordinator.battery_adapters.huawei import (
    HuaweiBatteryAdapter,
)
from custom_components.solar_energy_management.coordinator.battery_adapters.generic import (
    GenericBatteryAdapter,
)


def _hass(states=None):
    hass = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.states.get = MagicMock(side_effect=lambda e: (states or {}).get(e))
    return hass


def _calls(hass):
    return [(c.args[0], c.args[1], c.args[2]) for c in hass.services.async_call.await_args_list]


@pytest.mark.asyncio
class TestTheBaseRefuses:
    async def test_base_verbs_raise_not_implemented(self):
        class Bare(BatteryControlAdapter):
            async def command_normal(self): ...
            async def command_limit_discharge(self, watts): ...
            async def command_force_charge(self, *a): ...
            async def command_stop_force_charge(self): ...
        b = Bare(_hass(), {})
        with pytest.raises(NotImplementedError):
            await b.command_limit_export(0.0)
        with pytest.raises(NotImplementedError):
            await b.command_release_export()


@pytest.mark.asyncio
class TestHuawei:
    def _adapter(self, states=None, override=False):
        hass = _hass(states)
        cfg = {"inverter_device_id": "dev-huawei", "export_guard_override_external": override,
               "export_control_readback_entity": "sensor.inverter_active_power_control"}
        return HuaweiBatteryAdapter(hass, cfg), hass

    async def test_zero_export_is_one_service_call_by_device_id(self):
        a, hass = self._adapter({"sensor.inverter_active_power_control": SimpleNamespace(state="Unlimited")})
        await a.command_limit_export(0.0)
        assert ("huawei_solar", "set_zero_power_grid_connection", {"device_id": "dev-huawei"}) in _calls(hass)

    async def test_release_is_the_integrations_own_reset(self):
        a, hass = self._adapter()
        await a.command_release_export()
        assert ("huawei_solar", "reset_maximum_feed_grid_power", {"device_id": "dev-huawei"}) in _calls(hass)

    async def test_refuses_under_external_scheduling(self):
        a, hass = self._adapter({"sensor.inverter_active_power_control": SimpleNamespace(state="DI Active Scheduling")})
        with pytest.raises(NotImplementedError, match="external scheduling"):
            await a.command_limit_export(0.0)
        assert _calls(hass) == []

    async def test_the_override_lets_it_act_under_external_scheduling(self):
        a, hass = self._adapter({"sensor.inverter_active_power_control": SimpleNamespace(state="DI Active Scheduling")}, override=True)
        await a.command_limit_export(0.0)
        assert len(_calls(hass)) == 1

    async def test_a_cap_in_watts_uses_the_watt_service(self):
        a, hass = self._adapter()
        await a.command_limit_export(1500.0)
        assert ("huawei_solar", "set_maximum_feed_grid_power", {"device_id": "dev-huawei", "power": 1500}) in _calls(hass)

    async def test_repeat_is_not_rewritten(self):
        """#538 — an identical command every cycle is a modbus write-storm."""
        a, hass = self._adapter()
        await a.command_limit_export(0.0); await a.command_limit_export(0.0)
        assert len(_calls(hass)) == 1


@pytest.mark.asyncio
class TestGeneric:
    async def test_a_writable_number_is_captured_written_and_restored(self):
        hass = _hass({"number.inv_export_limit": SimpleNamespace(state="11000")})
        a = GenericBatteryAdapter(hass, {"export_limit_entity": "number.inv_export_limit"})
        await a.command_limit_export(0.0)
        await a.command_release_export()
        assert _calls(hass) == [
            ("number", "set_value", {"entity_id": "number.inv_export_limit", "value": 0.0}),
            ("number", "set_value", {"entity_id": "number.inv_export_limit", "value": 11000.0}),
        ]

    async def test_a_sensor_domain_limit_is_observable_only(self):
        a = GenericBatteryAdapter(_hass(), {"export_limit_entity": "sensor.inv_export_limit"})
        with pytest.raises(NotImplementedError, match="read-only"):
            await a.command_limit_export(0.0)

    async def test_no_entity_no_control(self):
        a = GenericBatteryAdapter(_hass(), {})
        with pytest.raises(NotImplementedError):
            await a.command_limit_export(0.0)
```

`GenericBatteryAdapter`'s constructor may need more config keys to build — check `grep -n "def __init__" -A 12 coordinator/battery_adapters/generic.py` and add the minimum the fixture needs.

- [ ] **Step 2: Run and watch it fail** → `AttributeError: command_limit_export`.

- [ ] **Step 3: Base verbs** (`base.py`, after `command_off`):

```python
    # ── (#955) export control: the dialect is per brand ──────────────────
    async def command_limit_export(self, watts: float) -> None:
        """Cap grid feed-in at ``watts`` (0 = zero export). Brands without an
        export control raise ``NotImplementedError`` — the guard records the
        refusal; it is a state, not a crash."""
        raise NotImplementedError("this battery adapter has no export control")

    async def command_release_export(self) -> None:
        """Put the feed-in limit back to what SEM found."""
        raise NotImplementedError("this battery adapter has no export control")

    #: What the adapter last wrote as an export cap; None = released/never.
    _last_export_limit_w: Optional[float] = None
```

- [ ] **Step 4: Huawei** (`huawei.py`, inside the class):

```python
    # ── (#955) export control is SERVICE-shaped on huawei_solar ──────────
    _EXTERNAL_MODES = ("di active scheduling", "remote scheduling")

    def _external_scheduling(self) -> bool:
        ent = self._config.get("export_control_readback_entity", "")
        st = self._hass.states.get(ent) if ent else None
        mode = str(getattr(st, "state", "") or "").lower()
        return any(m in mode for m in self._EXTERNAL_MODES)

    async def command_limit_export(self, watts: float) -> None:
        device_id = self._config.get("inverter_device_id", "")
        if not device_id:
            raise NotImplementedError("no inverter_device_id configured")
        if self._external_scheduling() and not bool(
                self._config.get("export_guard_override_external", False)):
            raise NotImplementedError(
                "inverter is under external scheduling — an operator's mode is not SEM's to replace")
        w = max(0.0, float(watts))
        if self._last_export_limit_w is not None and abs(self._last_export_limit_w - w) < 1.0:
            return                       # #538 — a repeat is pure cost
        if w <= 0.0:
            await self._hass.services.async_call(
                "huawei_solar", "set_zero_power_grid_connection", {"device_id": device_id})
        else:
            await self._hass.services.async_call(
                "huawei_solar", "set_maximum_feed_grid_power",
                {"device_id": device_id, "power": int(round(w))})
        self._last_export_limit_w = w
        self._last_intent = BatteryIntent.LIMIT_EXPORT

    async def command_release_export(self) -> None:
        device_id = self._config.get("inverter_device_id", "")
        if not device_id:
            raise NotImplementedError("no inverter_device_id configured")
        # The integration provides the restore itself: reset IS the prior.
        await self._hass.services.async_call(
            "huawei_solar", "reset_maximum_feed_grid_power", {"device_id": device_id})
        self._last_export_limit_w = None
        self._last_intent = BatteryIntent.RELEASE_EXPORT
```

`self._hass` / `self._config` are the base's names (`base.py __init__`); `BatteryIntent` is already imported in `huawei.py`.

- [ ] **Step 5: Deye** (`deye.py`, inside the class — the #827 select, prior captured exactly as `command_force_discharge` does at `:441`):

```python
    # ── (#955) export control is the System Work Mode select on Deye ─────
    async def command_limit_export(self, watts: float) -> None:
        ent = self._system_work_mode_entity
        target = self._system_work_mode_options.get("zero_export_to_load")
        if not ent or not target:
            raise NotImplementedError("no Deye system work mode select configured")
        current = self._get_state(ent)
        if current == target:
            return
        if current and current in self._system_work_mode_options.values():
            self._export_mode_prior = str(current)
        if not await self._write_and_verify(ent, target, "select"):
            raise RuntimeError("Deye work mode write did not verify")
        self._last_export_limit_w = 0.0
        self._last_intent = BatteryIntent.LIMIT_EXPORT

    async def command_release_export(self) -> None:
        ent = self._system_work_mode_entity
        prior = getattr(self, "_export_mode_prior", None)
        if not ent:
            raise NotImplementedError("no Deye system work mode select configured")
        if prior and self._get_state(ent) != prior:
            if not await self._write_and_verify(ent, prior, "select"):
                raise RuntimeError("Deye work mode restore did not verify")
        self._export_mode_prior = None
        self._last_export_limit_w = None
        self._last_intent = BatteryIntent.RELEASE_EXPORT
```

Deye's zero-export cap is all-or-nothing (a mode, not watts); `watts > 0` selects the same mode — say so in the docstring.

- [ ] **Step 6: Generic** (`generic.py`, inside the class):

```python
    # ── (#955) export control is a writable number, when there is one ────
    async def command_limit_export(self, watts: float) -> None:
        ent = str(self._config.get("export_limit_entity", "") or "")
        if not ent:
            raise NotImplementedError("no export limit entity configured")
        if not ent.startswith("number."):
            raise NotImplementedError(f"{ent} is read-only — an export limit SEM can see but not set")
        if getattr(self, "_export_prior", None) is None:
            st = self._hass.states.get(ent)
            try:
                self._export_prior = float(getattr(st, "state", None))
            except (TypeError, ValueError):
                raise NotImplementedError(f"{ent} is unreadable — nothing to restore to")
        await self._hass.services.async_call(
            "number", "set_value", {"entity_id": ent, "value": max(0.0, float(watts))})
        self._last_export_limit_w = max(0.0, float(watts))
        self._last_intent = BatteryIntent.LIMIT_EXPORT

    async def command_release_export(self) -> None:
        ent = str(self._config.get("export_limit_entity", "") or "")
        prior = getattr(self, "_export_prior", None)
        if ent and prior is not None:
            await self._hass.services.async_call(
                "number", "set_value", {"entity_id": ent, "value": float(prior)})
        self._export_prior = None
        self._last_export_limit_w = None
        self._last_intent = BatteryIntent.RELEASE_EXPORT
```

- [ ] **Step 7: Run** `semtest tests/test_921_export_adapters.py tests/test_inverter_battery_arch.py tests/test_battery_modes_523.py tests/test_827*.py tests/test_709*.py` → all pass.

- [ ] **Step 8: Commit** `git add coordinator/battery_adapters && git commit -m "feat(#955): export control per brand — Huawei services, the Deye #827 select, a writable number; refuse under external scheduling"`

---

### Task 8: Wiring — the guard ticks, the probe holds, the battery view sees the verdicts

**Files:**
- Modify: `coordinator/coordinator.py` — the peak-guard method (locate: `grep -n "_peak_slot_allowed_w = allowed" coordinator/coordinator.py`), `_build_fleet_cycle_state`, `_curtailment_grant_w` (`:10830`), the `BatteryView(` build (`:7684`), the battery actuation loop (locate: `grep -n "actuate_battery(" coordinator/coordinator.py`), `_async_update_data` result publish (beside `result["charge_pacing"]`, `:4792`)
- Modify: `coordinator/charger_types.py` (`BatteryView`: `sink_verdicts: "Any" = None`)
- Test: `tests/test_921_guard_wiring.py`

- [ ] **Step 1: Write the failing test**

```python
"""#955 wiring — the guard ticks after the sinks, the probe holds while it is engaged."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.solar_energy_management.coordinator.coordinator import SEMCoordinator
from custom_components.solar_energy_management.coordinator.export_guard import ExportGuard
from custom_components.solar_energy_management.coordinator.sink_verdicts import CLOSED, OPEN, SinkVerdict


def _fake(engaged=False, verdict=OPEN, export_w=0.0, enabled=True):
    hass = MagicMock(); hass.services.async_call = AsyncMock()
    g = ExportGuard()
    if engaged:
        g.state = "engaged"
    fake = SimpleNamespace(
        hass=hass, config={"export_guard_enabled": enabled, "curtailment_probe_enabled": True},
        _observer_mode=False, _export_guard=g,
        _sink_verdicts={"grid_export": SinkVerdict("grid_export", verdict, "t")},
        _battery_adapters={"b1": MagicMock(command_limit_export=AsyncMock(), command_release_export=AsyncMock(), _last_error=None)},
        _curtailment_last=None,
    )
    power = SimpleNamespace(grid_export_power=export_w, solar_power=0.0, home_consumption_power=0.0,
                            battery_charge_power=0.0, ev_power=0.0, ev_connected=False,
                            grid_power_unavailable=False, grid_import_power=0.0)
    return fake, power


@pytest.mark.asyncio
class TestTheGuardTicks:
    async def test_engaged_guard_holds_the_probe(self):
        fake, power = _fake(engaged=True)
        assert SEMCoordinator._curtailment_grant_w(fake, power) == 0.0
        assert fake._curtailment_last["state"] == "held_by_export_guard"

    async def test_disabled_guard_never_calls_an_adapter(self):
        fake, power = _fake(verdict=CLOSED, export_w=3000.0, enabled=False)
        for t in (0, 200, 400):
            await SEMCoordinator._run_export_guard(fake, power, now=float(t))
        fake._battery_adapters["b1"].command_limit_export.assert_not_awaited()

    async def test_closed_meter_with_export_engages_after_the_hold(self):
        fake, power = _fake(verdict=CLOSED, export_w=3000.0)
        for t in (0, 60, 130):
            await SEMCoordinator._run_export_guard(fake, power, now=float(t))
        fake._battery_adapters["b1"].command_limit_export.assert_awaited_once_with(0.0)
        assert fake._export_guard_state["state"] == "engaged"

    async def test_observer_mode_records_a_would_and_writes_nothing(self):
        fake, power = _fake(verdict=CLOSED, export_w=3000.0)
        fake._observer_mode = True
        fake.publish_observer_decision = MagicMock()
        for t in (0, 60, 130):
            await SEMCoordinator._run_export_guard(fake, power, now=float(t))
        fake._battery_adapters["b1"].command_limit_export.assert_not_awaited()
        assert fake._export_guard_state["state"] == "engaged"
        assert fake._export_guard_state["would"] == "limit_export"
```

The adapter registry attribute name on the coordinator may not be `_battery_adapters`; find it with `grep -n "_battery_adapters\b\|self._battery_adapter\b" coordinator/coordinator.py | head -3` and use that name in both the fixture and the method.

- [ ] **Step 2: Run and watch it fail** → `AttributeError: _run_export_guard`.

- [ ] **Step 3: The probe holds while the guard is engaged** — first lines of `_curtailment_grant_w` (`:10830`), before the `enabled = …` read:

```python
        # (#955) While SEM itself limits export, the #743 probe must not go
        # looking for an inverter "someone else" is limiting: it would harvest
        # the very energy the guard is deliberately clipping.
        _eg = getattr(self, "_export_guard", None)
        if _eg is not None and getattr(_eg, "state", "idle") == "engaged":
            self._curtailment_last = {"state": "held_by_export_guard", "grant_w": 0.0}
            return 0.0
```

- [ ] **Step 4: The guard's cycle step** — a new coordinator method beside the peak-guard one:

```python
    async def _run_export_guard(self, power, *, now: float | None = None) -> None:
        """(#955) The limit at the meter, AFTER the sinks ran this cycle.

        Reads the cycle's grid verdict, feeds the guard the export the meter
        still shows, and dispatches its intent through every battery adapter's
        export verbs — observer mode records a WOULD and writes nothing, exactly
        as ``actuate_battery`` does for every other intent.
        """
        import time as _time
        from .actuate_battery import actuate_battery
        from .charger_types import BatteryDecision, BatteryIntent
        from .export_guard import ExportGuard, LIMIT_EXPORT
        from .sink_verdicts import OPEN
        if getattr(self, "_export_guard", None) is None:
            self._export_guard = ExportGuard()
        guard = self._export_guard
        enabled = bool(self.config.get("export_guard_enabled", False))
        verdict = (getattr(self, "_sink_verdicts", None) or {}).get("grid_export")
        state = getattr(verdict, "state", OPEN) if enabled else OPEN
        export_w = (None if getattr(power, "grid_power_unavailable", False)
                    else float(getattr(power, "grid_export_power", 0.0) or 0.0))
        cmd = guard.update(_time.monotonic() if now is None else now, state, export_w)
        would = None
        if cmd.intent:
            intent = BatteryIntent.LIMIT_EXPORT if cmd.intent == LIMIT_EXPORT else BatteryIntent.RELEASE_EXPORT
            adapters = getattr(self, "_battery_adapters", None) or {}
            refused = []
            for bid, adapter in adapters.items():
                decision = BatteryDecision(battery_id=str(bid), intent=intent,
                                           export_limit_w=cmd.watts, reason=cmd.reason)
                await actuate_battery(decision, adapter, observer=bool(self._observer_mode),
                                      controller=self if hasattr(self, "publish_observer_decision") else None)
                if getattr(adapter, "_last_error", None):
                    refused.append(f"{bid}: {adapter._last_error}")
            if self._observer_mode:
                would = cmd.intent
            elif refused and intent is BatteryIntent.LIMIT_EXPORT:
                guard.report_refused("; ".join(refused))
        self._export_guard_state = {
            "enabled": enabled, "state": guard.state, "reason": guard.reason,
            "would": would, "repair_wanted": guard.repair_wanted,
            "verdict": getattr(verdict, "reason", "no verdict"),
        }
```

Call it from `_async_update_data` **after** the battery actuation loop and the surplus controller have run for the cycle (find the last `actuate_battery(` call site; add `await self._run_export_guard(power)` after that block, guarded with the same broad-exception pattern the peak guard uses so a guard bug never kills a cycle — but LOG it at WARNING, never swallow silently). Publish beside `result["charge_pacing"]` (`:4792`):

```python
            _eg = getattr(self, "_export_guard_state", None)
            if _eg:
                result["export_guard"] = dict(_eg)
                result["export_guard_state"] = _eg.get("state") or "idle"
            _sv = getattr(self, "_sink_verdicts", None) or {}
            result["sink_verdicts"] = {k: v.to_dict() for k, v in _sv.items()}
```

- [ ] **Step 5: The battery view sees the verdicts** — `BatteryView` gets `sink_verdicts: "Any" = None` (after `forecast_sell`), and the `BatteryView(` build (`:7684`) passes `sink_verdicts=getattr(self, "_sink_verdicts", None) or {},`.

- [ ] **Step 6: Run** `semtest tests/test_921_guard_wiring.py tests/test_743*.py tests/test_873_cycle_executes.py tests/test_864*.py` → all pass.

- [ ] **Step 7: Commit** `git add coordinator && git commit -m "feat(#955): the export guard ticks after the sinks; the probe holds while it is engaged"`

---

### Task 9: #879 — the house as a sink

**Files:**
- Modify: `coordinator/decide_battery.py` — insert before the `# ─── LIMIT_DISCHARGE branch (unified solar gate) ───` comment (`:~342`)
- Test: `tests/test_921_house_and_morning.py`

- [ ] **Step 1: Write the failing test**

```python
"""#879 the house as a sink, #892 the car before it leaves — decided in decide_battery."""
from custom_components.solar_energy_management.coordinator.charger_types import (
    BatteryIntent, BatteryRuntime, BatteryView, FleetContext,
)
from custom_components.solar_energy_management.coordinator.decide_battery import decide_battery
from custom_components.solar_energy_management.coordinator.sink_verdicts import HELD, OPEN, SinkVerdict


def _view(*, house=None, ev=None, soc=80.0, ev_connected=False, cfg_extra=None):
    cfg = {"battery_max_discharge_power": 4000, "battery_max_charge_power_w": 5000,
           "battery_mode": "auto", "battery_morning_drain_floor_soc": 50.0}
    cfg.update(cfg_extra or {})
    verdicts = {}
    if house: verdicts["house"] = SinkVerdict("house", house, "t")
    if ev: verdicts["ev"] = SinkVerdict("ev", ev, "t")
    return BatteryView(runtime=BatteryRuntime(battery_id="b1", last_known_soc=soc), config=cfg,
                       fleet=FleetContext(), charging_state="idle", ev_charging=ev_connected,
                       ev_connected=ev_connected, home_consumption_w=800.0,
                       scheduler_decision=None, sink_verdicts=verdicts)


class TestHouse:
    def test_held_clamps_discharge_to_zero(self):
        d = decide_battery(_view(house=HELD))
        assert d.intent is BatteryIntent.LIMIT_DISCHARGE and d.discharge_limit_w == 0.0
        assert "house" in d.reason

    def test_open_is_normal(self):
        assert decide_battery(_view(house=OPEN)).intent is BatteryIntent.NORMAL

    def test_no_verdict_is_todays_behaviour(self):
        assert decide_battery(_view()).intent is BatteryIntent.NORMAL

    def test_held_yields_to_a_force_charge_mode(self):
        d = decide_battery(_view(house=HELD, cfg_extra={"battery_mode": "force_charge"}))
        assert d.intent is BatteryIntent.FORCE_CHARGE


class TestMorningEv:
    def test_open_window_lifts_the_ev_protection_clamp(self):
        d = decide_battery(_view(ev=OPEN, ev_connected=True, soc=80.0))
        assert d.intent is BatteryIntent.NORMAL and "morning window" in d.reason

    def test_the_drain_floor_ends_the_window(self):
        d = decide_battery(_view(ev=OPEN, ev_connected=True, soc=49.0))
        assert d.intent is not BatteryIntent.NORMAL or "morning window" not in d.reason

    def test_held_keeps_todays_clamp(self):
        d = decide_battery(_view(ev=HELD, ev_connected=True, soc=80.0))
        assert "morning window" not in d.reason
```

- [ ] **Step 2: Run and watch it fail** → `TypeError: unexpected keyword 'sink_verdicts'` (Task 8 added the field; if it fails differently, the field is missing — add it) then assertion failures.

- [ ] **Step 3: The branches** — in `decide_battery`, after the scheduler block ends (the `return BatteryDecision(... "ensure not force-charging")` at `:~338`) and BEFORE the `# ─── LIMIT_DISCHARGE branch` comment:

```python
    # ─── arc #921: the sink verdicts (default = every sink OPEN) ───
    _sv = getattr(view, "sink_verdicts", None) or {}
    _ev_v = _sv.get("ev")
    _house_v = _sv.get("house")

    # (#892) A morning window before departure: the pack is SPENT into the
    # car deliberately, down to the drain floor — the EV protection clamp
    # below must not fight a window the user opened. Bounded by the floor.
    if _ev_v is not None and _ev_v.state == "open":
        _floor = float(cfg.get("battery_morning_drain_floor_soc", 50.0) or 50.0)
        soc = rt.last_known_soc
        if rt.available and soc is not None and soc > _floor:
            return BatteryDecision(
                battery_id=rt.battery_id, intent=BatteryIntent.NORMAL,
                reason=f"morning window — the pack feeds the car down to {_floor:.0f}% "
                       f"(SOC {soc:.0f}%)")

    # (#879) The house as a sink: HELD in a cheap/negative hour means "let the
    # house import, keep the pack for the expensive hours" — the WHEN is the
    # tariff level, the HOW MUCH is zero house cover. 0 W is quantised to 0.
    if _house_v is not None and _house_v.state == "held":
        return BatteryDecision(
            battery_id=rt.battery_id, intent=BatteryIntent.LIMIT_DISCHARGE,
            discharge_limit_w=0.0,
            reason=f"house sink held — {_house_v.reason}")
```

- [ ] **Step 4: Run** `semtest tests/test_921_house_and_morning.py tests/test_battery_modes_523.py tests/test_solar_plus_battery_is_the_permission.py tests/test_875_soc_never_read.py tests/test_691*.py` → all pass.

- [ ] **Step 5: Commit** `git add coordinator/decide_battery.py tests/test_921_house_and_morning.py && git commit -m "feat(#879, #892): the house as a sink, and a morning window that empties the pack into the car"`

---

### Task 10: #892 — the charger side of the morning window

**Files:**
- Modify: `coordinator/decide.py:_battery_assist_split` (the `if surplus < f.battery_assist_min_surplus_w:` gate at `:~258`)
- Modify: `coordinator/charger_types.py` (`FleetContext`: `ev_morning_window_open: bool = False`), `coordinator/build_view.py` (derive it from the verdicts)
- Test: `tests/test_921_house_and_morning.py` (append)

- [ ] **Step 1: Write the failing test**

```python
from custom_components.solar_energy_management.coordinator.charger_types import (
    ChargerEnergy, ChargerPower, ChargerView,
)
from custom_components.solar_energy_management.coordinator.decide import battery_assist_budget_w


def _cview(*, morning=False, surplus_w=300.0, soc=80.0):
    return ChargerView(
        power=ChargerPower(charger_id="k", power_w=0.0, connected=True, charging=False),
        energy=ChargerEnergy(charger_id="k"), mode="min_plus_solar",
        config={"ev_min_current": 6, "ev_phases": 3, "ev_voltage": 230, "ev_max_current": 32},
        fleet=FleetContext(solar_w=surplus_w + 500.0, home_w=500.0, battery_soc=soc,
                           battery_soc_known=True, battery_priority=5,
                           battery_assist_max_power_w=4500.0, battery_assist_min_surplus_w=1200.0,
                           auto_start_soc=60.0, buffer_soc=30.0, ev_morning_window_open=morning),
        ev_priority=1)


class TestTheChargerSide:
    def test_below_the_solar_gate_the_window_still_offers_the_pack(self):
        assert battery_assist_budget_w(_cview(morning=True)) > battery_assist_budget_w(_cview())

    def test_without_the_window_the_gate_holds_as_today(self):
        assert battery_assist_budget_w(_cview()) == 300.0
```

If `FleetContext` needs other fields for `fleet_soc_zone`, copy them from `tests/test_875_soc_never_read.py::_view`.

- [ ] **Step 2: Run and watch it fail.**

- [ ] **Step 3: The field and the gate.** `FleetContext`: `ev_morning_window_open: bool = False` with a one-line docstring `"""(#892) the ev sink verdict is OPEN this cycle — the pack may feed the car below the solar gate."""`. `build_view.py`, beside `sink_verdicts=`:

```python
        ev_morning_window_open=(getattr(getattr(fleet_state, "sink_verdicts", None) or {},
                                        "get", lambda k: None)("ev") or SimpleNamespace(state="open")).state == "open"
                               and bool(getattr(fleet_state, "morning_window_enabled", False)),
```

Simpler and clearer — compute it in `_build_fleet_cycle_state` instead and store `morning_window_open: bool` on `FleetCycleState`, then copy it here as `ev_morning_window_open=bool(getattr(fleet_state, "morning_window_open", False))`. Do the simpler form. In `decide.py`, the gate becomes:

```python
    if surplus < f.battery_assist_min_surplus_w:
        _consent = (getattr(view, "mode", None) == "solar_plus_battery"
                    or bool(getattr(f, "forecast_spending_enabled", False))
                    or bool(getattr(f, "ev_morning_window_open", False)))   # (#892)
        _budget_ok = (float(getattr(f, "battery_spendable_kwh", 0.0) or 0.0) > 0.0
                      or bool(getattr(f, "ev_morning_window_open", False)))
        if not (_consent and _budget_ok):
            return surplus, 0.0
```

- [ ] **Step 4: Run** `semtest tests/test_921_house_and_morning.py tests/test_875_soc_never_read.py tests/test_885*.py tests/test_537*.py` → all pass.

- [ ] **Step 5: Commit** `git add coordinator && git commit -m "feat(#892): the charger side of the morning window — the pack may feed the car below the solar gate"`

---

### Task 11: #926 — headroom before the meter closes

**Files:**
- Modify: `coordinator/coordinator.py:_today_pacing_ledger` (`:6868-6895`), `_run_charge_pacing` state dict (`:7172`)
- Test: `tests/test_921_pacing_headroom.py`

- [ ] **Step 1: Write the failing test**

```python
"""#926 — the pacer lands the pack full by the earlier of sunset and the next closed meter."""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

from custom_components.solar_energy_management.coordinator.coordinator import SEMCoordinator
from custom_components.solar_energy_management.coordinator.sink_verdicts import HELD, OPEN, SinkVerdict

TZ = timezone.utc


def _fake(battery_verdict):
    now = datetime(2026, 9, 15, 10, 0, tzinfo=TZ)
    fake = SimpleNamespace(
        config={}, hass=MagicMock(),
        time_manager=SimpleNamespace(get_sunrise_time=lambda: "07:00", get_sunset_plus_10_time=lambda: "19:40"),
        _forecast_reader=SimpleNamespace(forecast_data=SimpleNamespace(forecast_today_kwh=30.0)),
        _tariff_provider=None,
        _sink_verdicts={"battery": battery_verdict},
        _day_home_w_at=lambda now: (lambda t: 500.0),
        _configured_export_rate=lambda: 0.075,
    )
    return fake, now


class TestTheSecondLandingTime:
    def test_a_held_battery_trims_the_ledger_to_the_closing(self, monkeypatch):
        import custom_components.solar_energy_management.coordinator.coordinator as C
        monkeypatch.setattr(C.dt_util, "now", lambda: datetime(2026, 9, 15, 10, 0, tzinfo=TZ))
        held = SinkVerdict("battery", HELD, "t", until=datetime(2026, 9, 15, 15, 0, tzinfo=TZ))
        fake, _ = _fake(held)
        slots = SEMCoordinator._today_pacing_ledger(fake)
        assert slots and max(s.end for s in slots) <= datetime(2026, 9, 15, 15, 0, tzinfo=TZ)

    def test_an_open_battery_keeps_sunset(self, monkeypatch):
        import custom_components.solar_energy_management.coordinator.coordinator as C
        monkeypatch.setattr(C.dt_util, "now", lambda: datetime(2026, 9, 15, 10, 0, tzinfo=TZ))
        fake, _ = _fake(SinkVerdict("battery", OPEN, "t"))
        slots = SEMCoordinator._today_pacing_ledger(fake)
        assert slots and max(s.end for s in slots) > datetime(2026, 9, 15, 15, 0, tzinfo=TZ)
```

`_day_home_w_at`'s real signature: `grep -n "def _day_home_w_at" -A 3 coordinator/coordinator.py` — match it in the fixture.

- [ ] **Step 2: Run and watch the first case fail** (the ledger reaches 19:40).

- [ ] **Step 3: Trim the horizon.** In `_today_pacing_ledger`, after `sunrise, sunset = _at(sr_s), _at(ss_s)`:

```python
            # (#926) Land full by the EARLIER of sunset and the next closed
            # meter: every kWh of headroom the pack still has when the export
            # price turns negative is a kWh the guard does not have to
            # destroy. The verdict carries the closing time; no price here.
            _bv = (getattr(self, "_sink_verdicts", None) or {}).get("battery")
            _until = getattr(_bv, "until", None)
            if getattr(_bv, "state", "open") == "held" and _until and now < _until < sunset:
                sunset = _until
```

and in `_run_charge_pacing`'s state dict add `"lands_by": (getattr(_bv, "until", None).isoformat() if … else None)` — read `_bv` the same way there, or simpler: `"headroom_for_closed_meter": bool(getattr(bv, "state", "open") == "held")`.

- [ ] **Step 4: Run** `semtest tests/test_921_pacing_headroom.py tests/test_820_charge_pacing.py tests/test_820_inert_at_the_wire.py tests/test_820_pacing_reads_the_house_profile.py tests/test_949*.py` → all pass.

- [ ] **Step 5: Commit** `git add coordinator/coordinator.py tests/test_921_pacing_headroom.py && git commit -m "feat(#926): the pacer holds headroom before the meter closes"`

---

### Task 12: #871 steps 1–2 — unclamp, and let the sinks absorb

**Files:**
- Modify: `coordinator/day_ledger.py:111`
- Modify: `coordinator/surplus_controller.py` (the min-surplus / regulation-offset gate — locate with `grep -n "DEFAULT_REGULATION_OFFSET\|regulation_offset" coordinator/surplus_controller.py | head`)
- Test: `tests/test_921_absorb.py`

- [ ] **Step 1: Write the failing test**

```python
"""#871 steps 1-2 — a negative price is a cost the planner can see, and the loads absorb first."""
from datetime import datetime, timezone

from custom_components.solar_energy_management.coordinator.day_ledger import build_day_slots

TZ = timezone.utc
T0 = datetime(2026, 9, 15, 10, 0, tzinfo=TZ)


def _slots(export_rate):
    return build_day_slots(
        start=T0, end=T0.replace(hour=12), day_kwh=20.0,
        sunrise=T0.replace(hour=7), sunset=T0.replace(hour=19),
        home_w_at=lambda t: 300.0, price_at=lambda t: 0.30, level_cheap_at=lambda t: False,
        export_rate=export_rate,
    )


class TestTheLedgerSeesTheCost:
    def test_a_negative_export_rate_reaches_the_slot(self):
        s = _slots(-0.05)
        assert s and s[0].price == -0.05

    def test_a_positive_rate_is_unchanged(self):
        assert _slots(0.075)[0].price == 0.075

    def test_a_missing_rate_is_zero(self):
        assert _slots(None)[0].price == 0.0
```

Plus, for the absorb half, a test on the surplus controller's gate: find the function that decides whether a load may START on the current surplus (`grep -n "def .*can_start\|def _should_activate\|regulation_offset" coordinator/surplus_controller.py | head -5`) and write two cases with a `SurplusController` built the way `tests/test_953_cheap_hours_finish_window.py` builds one: the same surplus that is declined under an OPEN grid verdict is accepted under CLOSED (the regulation offset — the 50 W "always export a little" buffer — is what relaxes: it becomes 0 while the grid is CLOSED). Name them `test_closed_meter_relaxes_the_regulation_offset` and `test_open_meter_keeps_it`.

- [ ] **Step 2: Run and watch the first case fail** (`assert 0.0 == -0.05`).

- [ ] **Step 3: Remove the clamp** (`day_ledger.py:111`):

```python
                # (#871) NOT max(0.0, …). The clamp made a negative export rate
                # — one you PAY — look identical to a free kWh, and a planner
                # cannot prefer another sink over a cost it cannot see. ``or 0.0``
                # still handles an absent rate, the only case the clamp covered.
                price=float(export_rate or 0.0),
```

- [ ] **Step 4: Relax the regulation offset while the grid is CLOSED.** Where the controller reads `regulation_offset` for the cycle, read the fleet verdict too (the controller receives the fleet state / coordinator — find how it reads `curtailment_grant_w` or the peak state: `grep -n "peak\|fleet" coordinator/surplus_controller.py | head -8`) and use `offset = 0.0 if grid_closed else configured_offset`. Safety gates (peak guard, reserve, stop-war) are not touched — pin that with an AST test: `reads_attribute(SurplusController.<method>, "self", "_peak_...")` unchanged before/after is overkill; instead assert in the test that a CLOSED verdict with the peak guard at 0 W allowance still starts nothing.

- [ ] **Step 5: Run** `semtest tests/test_921_absorb.py tests/test_755_*.py tests/test_820_*.py tests/test_778_spendable_budget.py tests/test_953_cheap_hours_finish_window.py tests/test_559*.py` → all pass. Anything failing in the ledger consumers "silently relied on non-negative prices — fix the consumer, never restore the clamp" (the #871 plan's rule).

- [ ] **Step 6: Commit** `git add coordinator/day_ledger.py coordinator/surplus_controller.py tests/test_921_absorb.py && git commit -m "fix(#871): a negative export price is a cost, not a free kWh — and the loads absorb before anything is clipped"`

---

### Task 13: Hand-back — unload, disable, removal

**Files:**
- Modify: `cleanup.py` (`_PER_ENTRY_STORE_FORMATS`), `__init__.py:3076` (before the pacing release)
- Modify: `coordinator/coordinator.py` (persist the engaged state in a `Store` `sem.export_guard.{entry_id}` — copy `ChargePacingWriter`'s `_store` use in `charge_pacing.py:164-215`)
- Test: `tests/test_921_handback.py`

- [ ] **Step 1: Write the failing test**

```python
"""#955 obeys #908/#936/#949: only what SEM commanded is handed back, and it is handed back first."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.solar_energy_management import cleanup
from custom_components.solar_energy_management.coordinator.export_guard import ExportGuard


class TestInventory:
    def test_the_guard_store_is_in_the_per_entry_inventory(self):
        assert "sem.export_guard.{entry_id}" in cleanup._PER_ENTRY_STORE_FORMATS


@pytest.mark.asyncio
class TestRelease:
    async def test_an_engaged_guard_is_released_on_unload(self):
        from custom_components.solar_energy_management.coordinator.coordinator import SEMCoordinator
        g = ExportGuard(); g.state = "engaged"
        adapter = MagicMock(command_release_export=AsyncMock(), _last_error=None)
        fake = SimpleNamespace(_export_guard=g, _battery_adapters={"b1": adapter}, _observer_mode=False)
        said = await SEMCoordinator.async_release_export_guard(fake, reason="unloaded")
        adapter.command_release_export.assert_awaited_once()
        assert g.state == "idle" and "released" in said

    async def test_an_idle_guard_touches_nothing(self):
        from custom_components.solar_energy_management.coordinator.coordinator import SEMCoordinator
        adapter = MagicMock(command_release_export=AsyncMock())
        fake = SimpleNamespace(_export_guard=ExportGuard(), _battery_adapters={"b1": adapter}, _observer_mode=False)
        assert await SEMCoordinator.async_release_export_guard(fake, reason="unloaded") is None
        adapter.command_release_export.assert_not_awaited()
```

- [ ] **Step 2: Run and watch it fail.**

- [ ] **Step 3: Inventory + release method.** `cleanup.py`: add `"sem.export_guard.{entry_id}",` to `_PER_ENTRY_STORE_FORMATS` with the comment `#:   coordinator/coordinator.py (#955) -> f"sem.export_guard.{entry_id}"`. Coordinator:

```python
    async def async_release_export_guard(self, *, reason: str):
        """(#955, the #908 rule) Put the inverter's feed-in back if — and only
        if — SEM is the one holding it. Never raises: a teardown that fails
        half way must still let HA remove the entry."""
        guard = getattr(self, "_export_guard", None)
        if guard is None or guard.state not in ("engaged", "releasing", "refused"):
            return None
        released = []
        for bid, adapter in (getattr(self, "_battery_adapters", None) or {}).items():
            try:
                await adapter.command_release_export()
                released.append(str(bid))
            except Exception as exc:  # noqa: BLE001
                _LOGGER.warning("export guard: could not release %s on %s: %s", bid, reason, exc)
        guard.state = "idle"; guard.reason = f"released on {reason}"
        return f"export guard released on {reason}: {', '.join(released) or 'nothing to release'}"
```

`__init__.py`, at `:3076` — BEFORE `pending_pacing_release` is read and before observer mode flips (the #949 comment explains why order matters):

```python
            # (#955) The export cut FIRST: an inverter left at zero feed-in by
            # a removed SEM would throw away every surplus kWh with nothing
            # left on the system that knows why. Same rule, same order as #949.
            try:
                _said = await coordinator.async_release_export_guard(
                    reason="disabled" if entry.disabled_by is not None else "unloaded")
                if _said:
                    _LOGGER.info("%s", _said)
            except Exception as exc:  # noqa: BLE001 — teardown must finish
                _LOGGER.warning("export guard release failed on unload: %s", exc)
```

Persistence: on engage, `await store.async_save({"engaged": True, "since": iso})`; on release, `async_remove`/`async_save({"engaged": False})`; on a clean start (`async_setup_entry` after the coordinator exists), if the store says engaged → release once and clear (Huawei's reset is idempotent; Deye/generic restore their captured prior). Copy the exact `Store(hass, 1, key)` construction from `deye_snapshot_store.py`.

- [ ] **Step 4: Run** `semtest tests/test_921_handback.py tests/test_935*.py tests/test_949*.py tests/test_936*.py` → all pass.

- [ ] **Step 5: Commit** `git add cleanup.py __init__.py coordinator/coordinator.py tests/test_921_handback.py && git commit -m "feat(#955): hand the inverter's feed-in back — first, and only if SEM held it"`

---

### Task 14: The surface — every setting in the GUI, all default OFF

**Files:**
- Modify: `switch.py` (`SWITCH_TYPES`, after `battery_charge_pacing_enabled`), `persisted_flags.py:43-56`
- Modify: `number.py` (`NUMBER_TYPES`, `CONFIG_KEY_MAP` if keys differ, the defaults table at `:704`), `consts/core.py` (defaults)
- Modify: `sensor.py` (`export_guard_state` diagnostic), `strings.json` (`entity.switch` `:1399`, `entity.number` `:1422`, `entity.sensor`), `translations/*.json` ×16
- Modify: `dashboard/sem_dashboard_template.yaml` (Config tab: the three switches + numbers beside the pacing switch; Control tab: the guard state tile beside the peak tile), `dashboard/card/src/cards/sem-grid-card.js` (read `export_guard_state`), then `cd dashboard/card && npm run build`
- Test: `tests/test_921_surface.py`

- [ ] **Step 1: Write the failing test**

```python
"""arc #921 — the surface: three switches default OFF and persisted, the numbers, the state sensor."""
from custom_components.solar_energy_management import number as number_mod, sensor as sensor_mod, switch as switch_mod
from custom_components.solar_energy_management.persisted_flags import PERSISTED_FLAG_DEFAULTS

SWITCHES = ("export_guard_enabled", "export_guard_override_external",
            "battery_house_sink_enabled", "ev_morning_window_enabled")
NUMBERS = ("export_guard_engage_s", "export_guard_release_s",
           "ev_morning_window_hours", "battery_morning_drain_floor_soc")


class TestSwitches:
    def test_all_four_exist(self):
        keys = {d.key for d in switch_mod.SWITCH_TYPES}
        assert set(SWITCHES) <= keys

    def test_all_four_are_persisted_and_default_off(self):
        for k in SWITCHES:
            assert PERSISTED_FLAG_DEFAULTS[k] is False, k


class TestNumbers:
    def test_all_four_exist_with_sane_bounds(self):
        by_key = {d.key: d for d in number_mod.NUMBER_TYPES}
        for k in NUMBERS:
            assert k in by_key, k
        assert by_key["ev_morning_window_hours"].native_min_value == 0.5
        assert by_key["battery_morning_drain_floor_soc"].native_max_value == 90
        assert by_key["export_guard_engage_s"].native_min_value >= 30


class TestSensor:
    def test_the_guard_state_is_a_diagnostic_sensor(self):
        d = next(d for d in sensor_mod.SENSOR_TYPES if d.key == "export_guard_state")
        assert d.entity_category.value == "diagnostic"
```

- [ ] **Step 2: Run and watch it fail.**

- [ ] **Step 3: Switches** (`switch.py`, after the pacing switch), all `entity_category=EntityCategory.CONFIG`, keys exactly as in the test, icons `mdi:transmission-tower-off`, `mdi:shield-alert-outline`, `mdi:home-lightning-bolt-outline`, `mdi:car-clock`. `persisted_flags.py`: four entries `False` with the comment `# (arc #921) ships asleep like the spending arc — woken deliberately.`

- [ ] **Step 4: Numbers** (`number.py` `NUMBER_TYPES`, `mode=NumberMode.BOX` for the seconds, `SLIDER` for hours/SOC):

```python
    NumberEntityDescription(key="export_guard_engage_s", native_unit_of_measurement="s",
                            native_min_value=30, native_max_value=900, native_step=10,
                            mode=NumberMode.BOX, entity_category=EntityCategory.CONFIG),
    NumberEntityDescription(key="export_guard_release_s", native_unit_of_measurement="s",
                            native_min_value=60, native_max_value=1800, native_step=10,
                            mode=NumberMode.BOX, entity_category=EntityCategory.CONFIG),
    NumberEntityDescription(key="ev_morning_window_hours", native_unit_of_measurement="h",
                            native_min_value=0.5, native_max_value=6, native_step=0.5,
                            mode=NumberMode.SLIDER, entity_category=EntityCategory.CONFIG),
    NumberEntityDescription(key="battery_morning_drain_floor_soc", native_unit_of_measurement="%",
                            native_min_value=10, native_max_value=90, native_step=5,
                            mode=NumberMode.SLIDER, entity_category=EntityCategory.CONFIG),
```

Defaults in the table at `:704`: `120`, `300`, `2.0`, `50`; constants in `consts/core.py` beside `DEFAULT_BATTERY_ASSIST_MIN_SURPLUS`. `ExportGuard` reads the two hold values from config at construction: change Task 5's constants to defaults and give `ExportGuard.__init__(engage_hold_s=ENGAGE_HOLD_S, release_hold_s=RELEASE_HOLD_S)`; the coordinator passes the config values. Keep the module constants (the tests use them).

- [ ] **Step 5: Sensor** (`sensor.py`): `SensorEntityDescription(key="export_guard_state", entity_category=EntityCategory.DIAGNOSTIC, icon="mdi:transmission-tower-off")` — the generic value path reads `result["export_guard_state"]` (Task 8 publishes it).

- [ ] **Step 6: Names** in `strings.json` and all 16 `translations/*.json`: switches "Export guard", "Export guard: override external scheduling", "House as a battery sink", "Morning EV window"; numbers "Export guard engage delay (s)", "Export guard release delay (s)", "Morning EV window (h)", "Morning EV drain floor (%)"; sensor "Export guard". Run `semtest tests/test_674_translation_parity.py`.

- [ ] **Step 7: Dashboard.** Config tab: add the four switches and four numbers directly under the charge-pacing switch in `dashboard/sem_dashboard_template.yaml` (same mushroom entity-card shape; the template is Jinja-translated server-side — use plain strings). Control tab: in `sem-grid-card.js` read `export_guard_state` (add to the entity list at `:28`) and render one line under the peak block: `Export guard · ${state}`; `npm run build`. Pin in `tests/test_card_registry_metadata.py` nothing new (no new card); pin in `tests/test_921_surface.py` that the template contains all eight entity ids.

- [ ] **Step 8: Run** `semtest tests/test_921_surface.py tests/test_674_translation_parity.py tests/test_778_permission_switches.py tests/test_card_template_lint.py tests/test_new_install_observes_first.py` → all pass.

- [ ] **Step 9: Commit** `git add switch.py persisted_flags.py number.py consts/core.py sensor.py strings.json translations dashboard tests/test_921_surface.py && git commit -m "feat(#921): the surface — four switches (all OFF), four numbers, the guard state, on the Config and Control tabs"`

---

### Task 15: Plan rows

**Files:**
- Modify: `coordinator/today_plan.py` (new kinds + two kwargs), `coordinator/coordinator.py:4996` (`_shared_plan_kwargs`), `dashboard/card/src/cards/sem-today-plan-card.js` (`KINDS`), `dashboard/translations.json` (16 languages), then `python3 scripts/regenerate_localize.py` and `npm run build`
- Test: `tests/test_921_plan_rows.py`

- [ ] **Step 1: Write the failing test**

```python
from datetime import datetime, timezone

from custom_components.solar_energy_management.coordinator.today_plan import compose_today_plan

TZ = timezone.utc
NOW = datetime(2026, 9, 15, 12, 0, tzinfo=TZ)


class TestExportRows:
    def test_a_closing_and_a_reopening_are_rows(self):
        plan = compose_today_plan(now=NOW, export_closes_at=NOW.replace(hour=14),
                                  export_reopens_at=NOW.replace(hour=16))
        kinds = [r["kind"] for r in plan]
        assert "export_closed" in kinds and "export_reopens" in kinds

    def test_nothing_when_the_meter_stays_open(self):
        assert all(r["kind"] not in ("export_closed", "export_reopens")
                   for r in compose_today_plan(now=NOW))
```

- [ ] **Step 2: Run and watch it fail** → `TypeError: unexpected keyword`.

- [ ] **Step 3: Kinds, kwargs, rows.** `today_plan.py`: `KIND_EXPORT_CLOSED = "export_closed"`, `KIND_EXPORT_REOPENS = "export_reopens"`; `compose_today_plan(..., export_closes_at: Optional[datetime] = None, export_reopens_at: Optional[datetime] = None, ...)`; rows beside the night window:

```python
    # === arc #921: the meter closes / reopens (the grid stops being a sink) ===
    if export_closes_at and now < export_closes_at < horizon:
        rows.append(PlanRow(when=export_closes_at, kind=KIND_EXPORT_CLOSED, label="plan_export_closed"))
    if export_reopens_at and now < export_reopens_at < horizon:
        rows.append(PlanRow(when=export_reopens_at, kind=KIND_EXPORT_REOPENS, label="plan_export_reopens"))
```

`_shared_plan_kwargs` (`:4996`): from `self._sink_verdicts.get("grid_export")` — `export_closes_at=(v.until if v.state == "open" else None)`, `export_reopens_at=(v.until if v.state == "closed" else None)`. Card `KINDS`: `export_closed: { icon: 'mdi:transmission-tower-off', color: '#f06292' }`, `export_reopens: { icon: 'mdi:transmission-tower', color: '#8353d1' }`. `dashboard/translations.json`: `plan_export_closed` "Export price negative — meter closed" / `plan_export_reopens` "Export price positive again — meter open", in all 16 languages (German: "Einspeisepreis negativ — Zähler geschlossen" / "Einspeisepreis wieder positiv — Zähler offen"). `tests/test_963_plan_events_that_wont_happen.py::TestPlanIsTranslated` reads the composer's keys and will fail on any language left English — that is the guard.

- [ ] **Step 4: Run** `semtest tests/test_921_plan_rows.py tests/test_963_plan_events_that_wont_happen.py tests/test_742_strip_joint_blocks.py` → all pass; `python3 scripts/regenerate_localize.py`; `cd dashboard/card && npm run build`.

- [ ] **Step 5: Commit** `git add coordinator/today_plan.py coordinator/coordinator.py dashboard tests/test_921_plan_rows.py && git commit -m "feat(#921): the plan says when the meter closes and reopens"`

---

### Task 16: The scenario rig — one negative hour through the real cycle

**Files:**
- Create: `tests/test_921_sink_scenario.py` (built on `tests/test_873_cycle_executes.py::run_cycle`)

- [ ] **Step 1: Write the test**

```python
"""arc #921 — a negative hour with a car, a battery and a load, through the REAL cycle.

Built on #873's ``run_cycle``: the assembly, not the parts. Pins the ORDER the
arc promises — sinks absorb first, the guard engages only on export still
measured after that, and it engages only after its hold — and that every
switch OFF leaves today's behaviour byte-for-byte."""
import pytest

from tests.test_873_cycle_executes import _sensors, run_cycle, sealed_nights


def _config(**flags):
    cfg = {"currency": "EUR", "export_guard_enabled": True, "battery_max_discharge_power": 5000}
    cfg.update(flags)
    return cfg


@pytest.mark.asyncio
async def test_everything_off_is_todays_cycle():
    before = await run_cycle(config={"currency": "EUR"}, states=_sensors(6000, -3000, 0, 60), nights=sealed_nights())
    after = await run_cycle(config=_config(export_guard_enabled=False), states=_sensors(6000, -3000, 0, 60), nights=sealed_nights())
    for k in ("home_consumption_power", "grid_power", "charging_state"):
        assert before[k] == after[k], k
    assert after["export_guard_state"] == "idle"


@pytest.mark.asyncio
async def test_a_negative_hour_publishes_the_verdicts_and_holds_before_engaging():
    # A NEGATIVE level needs a tariff provider stub: copy the dynamic-provider stub from
    # tests/test_856_*.py (grep "PriceLevel.NEGATIVE" tests/ | head -3) into `states`/config.
    out = await run_cycle(config=_config(), states=_sensors(6000, 3000, 0, 60), nights=sealed_nights())
    assert out["sink_verdicts"]["grid_export"]["state"] in ("open", "closed")
    assert out["export_guard_state"] in ("idle", "holding")     # never 'engaged' on the first cycle
```

Read `run_cycle`'s signature and `_sensors` (`tests/test_873_cycle_executes.py:87-165`) before adapting; the tariff stub is the one piece to copy from an existing NEGATIVE-level test.

- [ ] **Step 2: Run** `semtest tests/test_921_sink_scenario.py` → pass; then the FULL suite: `semtest tests/ -q -rf > /tmp/921-suite.txt 2>&1; grep -E "passed|failed" /tmp/921-suite.txt | tail -1` → 0 failed.

- [ ] **Step 3: Commit** `git add tests/test_921_sink_scenario.py && git commit -m "test(#921): a negative hour through the real cycle — sinks first, guard last, everything off is today"`

---

### Task 17: Docs and CHANGELOG

**Files:** `docs/USER_GUIDE.md` (a section "When the export price goes negative" after `## Peak Load Management`, `:1245`), `README.md` (one bullet after the peak-load bullet at `:44`), `CHANGELOG.md` `[Unreleased]`.

- [ ] **Step 1: USER_GUIDE** — what a sink verdict is (one table: OPEN/HELD/CLOSED), the export guard (what it writes per brand, the two delays, the external-scheduling refusal and its override, the hand-back), the house sink, the morning EV window, the two #871 diagnostics; every switch OFF by default; SEM curtails only what the battery, the car and the loads could not take.

- [ ] **Step 2: README** bullet: **"The grid is not always a sink (2.1, #921)** — on a spot feed-in tariff the export price goes negative; SEM measures what that costs, keeps the energy in the battery, the car, the house and the loads first, and — when you turn the export guard on — caps feed-in at zero at the inverter for the duration, with hysteresis and a hand-back. Huawei (services), Deye (work mode), any writable export-limit number."

- [ ] **Step 3: CHANGELOG** under `[Unreleased]`, music-assistant style, one bullet per child (✨ #955, ✨ #871, ✨ #926, ✨ #879, ✨ #892) each ending "Off by default."

- [ ] **Step 4: Commit** `git add docs README.md CHANGELOG.md && git commit -m "docs(#921): where else a kWh can go, and how SEM makes sure it does"`

---

---

# Stage 4 — #956: the roster learns service-shaped controls

**Why it is a separate branch and still part of this plan.** The arc's guard ships on hand-written
brand verbs (Task 7). #956 is what makes the *next* brand free: a capability offered as a
SERVICE (Huawei's four `huawei_solar.*` export services, `forcible_charge`; KEBA's `keba.set_current`)
is invisible to the crawler today — `scripts/crawl_integration_roster.py` never reads
`services.yaml`, `ROLE_RULES` keys only on entity platforms, `SEM_CONFIG_KEY_FOR_ROLE` maps a role to
an ENTITY-id key, and the Config card can therefore never propose one. Stage 4 teaches all four
layers the second shape, then turns Task 7's hand-written Huawei table into a crawled row — with a
parity test pinning that the adapter and the roster say the same thing, so neither can drift.

**Branch:** `feature/956-roster-services` from develop (after the arc lands). **Read first:**
`gh issue view 956`, `consts/role_lexicon.py` (`ROLE_RULES`, `SEM_CONFIG_KEY_FOR_ROLE`,
`OBSERVE_ONLY_ROLES`, `PER_CHARGER_ROLES`, `AUTO_RESOLVED_ROLES`), the crawler's
`_strings_urls` / `roles_from_vocabulary` / `_match_role` / `render_module` / `write_roles_baseline`,
`hardware_detection.propose_roles_from_roster` / `_role_action` / `propose_for_installed`, and the
three #915 structural tests (`test_915_roster_is_not_a_claim.py`, `test_915_roster_at_runtime.py`,
`test_915_coverage_ratchet.py`) — every one of them must keep passing with the new shape.

**The shape, in one sentence:** a role rule may match a *service* (`{"service": "<domain>.<name>",
"fields": (...)}`) as well as an entity; a vocabulary row for a service-shaped role carries
`{"platform": "service", "keys": ("set_zero_power_grid_connection", ...), "target": "device_id",
"release": "reset_maximum_feed_grid_power"}`; the proposal for such a role resolves to a **service
plus a target**, accepted into the config keys the adapters already read (`inverter_device_id` for
Huawei, `export_limit_entity` for a number) — and a service is never proposed as writable without
the same "SEM may write this" judgement the entity roles get (#810).

### File structure (stage 4)

| File | Responsibility |
|---|---|
| `scripts/crawl_integration_roster.py` | Tasks 18–19 — mine `services.yaml`; a second rule form; emit service rows |
| `consts/role_lexicon.py` | Task 19 — `SERVICE_ROLE_RULES`, `SEM_SERVICE_TARGET_KEY_FOR_ROLE`, `WRITE_GATED_SERVICE_ROLES` |
| `consts/integration_roster.py` (generated) | Task 20 — regenerated with service rows; `tests/roster_*_baseline.json` moved with the reason |
| `hardware_detection.py` | Task 21 — `propose_roles_from_roster` intersects service roles with the install's *services* (`hass.services.async_services()`), `_role_action` → `set_option` on the target key |
| `dashboard/card/src/cards/sem-config-card.js`, `dashboard/translations.json` | Task 22 — a service proposal renders "service · target" with the same accept button |
| `coordinator/battery_adapters/huawei.py`, `tests/test_956_roster_services.py` | Task 23 — the hand-written Huawei export table becomes a read of the roster row; parity pinned |
| `docs/USER_GUIDE.md`, `docs/ARCHITECTURE.md`, `CHANGELOG.md` | Task 24 |

---

### Task 18: Mine `services.yaml` beside the entity vocabulary

**Files:**
- Modify: `scripts/crawl_integration_roster.py` (beside `_strings_urls`, `:412`)
- Test: `tests/test_956_roster_services.py`

- [ ] **Step 1: Write the failing test**

```python
"""#956 — the roster is blind to services; teach the crawler the second shape."""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _crawler():
    spec = importlib.util.spec_from_file_location("crawler", ROOT / "scripts" / "crawl_integration_roster.py")
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    return mod


HUAWEI_SERVICES_YAML = """
set_zero_power_grid_connection:
  name: Set zero power grid connection
  fields:
    device_id:
      required: true
      selector:
        device:
          integration: huawei_solar
set_maximum_feed_grid_power:
  fields:
    device_id: {required: true}
    power: {required: true, selector: {number: {min: 0, max: 50000, unit_of_measurement: W}}}
reset_maximum_feed_grid_power:
  fields:
    device_id: {required: true}
forcible_charge:
  fields:
    device_id: {required: true}
    power: {required: true}
    duration: {required: true}
"""


class TestServicesAreMined:
    def test_services_yaml_urls_sit_beside_strings(self):
        c = _crawler()
        urls = list(c._services_urls("wlcrs/huawei_solar", "huawei_solar", "hacs"))
        assert any(u.endswith("/custom_components/huawei_solar/services.yaml") for u in urls)
        assert any(u.endswith("/services.yaml") and "custom_components" not in u for u in urls), \
            "content_in_root repos (wlcrs/huawei_solar is one) have no custom_components wrapper"

    def test_core_services_come_from_the_core_tree(self):
        c = _crawler()
        urls = list(c._services_urls("", "keba", "core"))
        assert urls == ["https://raw.githubusercontent.com/home-assistant/core/dev/homeassistant/components/keba/services.yaml"]

    def test_the_parser_yields_name_and_field_set(self):
        c = _crawler()
        svc = c.parse_services_yaml(HUAWEI_SERVICES_YAML)
        assert svc["set_zero_power_grid_connection"]["fields"] == ("device_id",)
        assert svc["set_maximum_feed_grid_power"]["fields"] == ("device_id", "power")
        assert "forcible_charge" in svc

    def test_garbage_yields_nothing_not_an_exception(self):
        c = _crawler()
        assert c.parse_services_yaml("not: [valid") == {}
        assert c.parse_services_yaml("") == {}
```

- [ ] **Step 2: Run it and watch it fail** — `semtest tests/test_956_roster_services.py` → `AttributeError: _services_urls`.

- [ ] **Step 3: The URL builder and the parser**, beside `_strings_urls`:

```python
def _services_urls(repo: str, domain: str, origin: str) -> Iterable[str]:
    """Where an integration declares its SERVICES — the second shape a
    capability arrives in (#956). Same layout rules as ``_strings_urls``:
    core in the core tree, HACS under custom_components/ or content_in_root."""
    if origin == "core":
        yield f"https://raw.githubusercontent.com/home-assistant/core/dev/homeassistant/components/{domain}/services.yaml"
        return
    if not repo:
        return
    for branch in ("main", "master"):
        for path in (f"custom_components/{domain}/services.yaml", "services.yaml"):
            yield f"https://raw.githubusercontent.com/{repo}/{branch}/{path}"


def parse_services_yaml(text: str) -> Dict[str, dict]:
    """``{service_name: {"fields": (field, ...)}}`` — the name and the field
    SET, which is all a rule can key on. Garbage is an empty dict, never an
    exception: one bad file must not cost the whole crawl."""
    try:
        import yaml
        doc = yaml.safe_load(text or "") or {}
    except Exception:  # noqa: BLE001
        return {}
    if not isinstance(doc, dict):
        return {}
    out = {}
    for name, body in doc.items():
        if not isinstance(name, str) or not isinstance(body, dict):
            continue
        fields = body.get("fields") or {}
        out[name] = {"fields": tuple(sorted(str(f) for f in fields)) if isinstance(fields, dict) else ()}
    return out
```

Wire it into the fetch: where `_strings_urls` results are fetched per row (`grep -n "_strings_urls(" scripts/crawl_integration_roster.py`), fetch `_services_urls` the same way (same `_get`, same cache, same `offline` flag) and attach the parsed dict to the row as `row["services"]`. `yaml` is already a HA dependency; the crawler runs on the dev box.

- [ ] **Step 4: Run** → 4 passed. **Step 5: Commit** `git commit -m "feat(#956): the crawler reads services.yaml — the second shape a capability arrives in"`

---

### Task 19: A rule form that matches a service

**Files:**
- Modify: `consts/role_lexicon.py` (after `ROLE_RULES`), `scripts/crawl_integration_roster.py` (`roles_from_vocabulary`, `_match_role`)
- Test: `tests/test_956_roster_services.py` (append)

- [ ] **Step 1: Write the failing test**

```python
class TestServiceRoleRules:
    def test_the_lexicon_has_service_rules_for_the_export_cut(self):
        from custom_components.solar_energy_management.consts import role_lexicon as lex
        r = lex.SERVICE_ROLE_RULES["zero_export"]
        assert r["platform"] == "service"
        assert any(__import__("re").search(p, "set_zero_power_grid_connection", 2) for p in r["any"])
        assert "release_any" in r      # the undo is part of the role, not an afterthought

    def test_a_service_row_is_emitted_for_huawei(self):
        c = _crawler()
        from custom_components.solar_energy_management.consts import role_lexicon as lex
        vocab = {"service": {"set_zero_power_grid_connection": {"fields": ("device_id",)},
                             "reset_maximum_feed_grid_power": {"fields": ("device_id",)},
                             "forcible_charge": {"fields": ("device_id", "duration", "power")}}}
        roles = c.roles_from_vocabulary(vocab, lex, kind="energy")
        row = roles["zero_export"]
        assert row["platform"] == "service"
        assert "set_zero_power_grid_connection" in row["keys"]
        assert row["release"] == "reset_maximum_feed_grid_power"
        assert row["target"] == "device_id"

    def test_a_service_without_its_release_is_not_a_role(self):
        """A cut SEM cannot undo is a cut SEM may not make (#908)."""
        c = _crawler()
        from custom_components.solar_energy_management.consts import role_lexicon as lex
        vocab = {"service": {"set_zero_power_grid_connection": {"fields": ("device_id",)}}}
        assert "zero_export" not in c.roles_from_vocabulary(vocab, lex, kind="energy")

    def test_entity_rules_are_untouched(self):
        from custom_components.solar_energy_management.consts import role_lexicon as lex
        assert all(v["platform"] != "service" for v in lex.ROLE_RULES.values())
```

- [ ] **Step 2: Run and watch it fail.**

- [ ] **Step 3: The rules** (`consts/role_lexicon.py`, after `ROLE_RULES`):

```python
#: (#956) Roles a brand offers as a SERVICE rather than an entity. ``any``
#: matches the service NAME; ``fields_any`` a field it must carry; ``release_any``
#: the service that undoes it — a role without its undo is not proposed
#: (#908: hand back what you found). ``target`` names the field that
#: addresses the device, which is what the config key stores.
SERVICE_ROLE_RULES: Final[Dict[str, Dict[str, Any]]] = {
    "zero_export": {
        "platform": "service",
        "any": (r"zero_power_grid", r"zero_export", r"set_maximum_feed_grid_power$"),
        "fields_any": (r"device_id",),
        "release_any": (r"reset_maximum_feed_grid_power", r"reset_export", r"clear_export_limit"),
        "target": "device_id",
    },
    "battery_force_charge_service": {
        "platform": "service",
        "any": (r"^forcible_charge$", r"force_charge$"),
        "fields_any": (r"device_id",),
        "release_any": (r"stop_forcible_charge", r"stop_force_charge"),
        "target": "device_id",
    },
}

#: (#956) A service-shaped role is accepted into the key its ADAPTER already
#: reads — never a new spelling of an existing setting.
SEM_SERVICE_TARGET_KEY_FOR_ROLE: Final[Dict[str, str]] = {
    "zero_export": "inverter_device_id",
    "battery_force_charge_service": "inverter_device_id",
}

#: (#956) Service roles SEM may CALL only after the same write-safety
#: judgement the entity roles get (#810): a declared service is not consent.
WRITE_GATED_SERVICE_ROLES: Final[frozenset] = frozenset({"zero_export", "battery_force_charge_service"})
```

- [ ] **Step 4: The matcher and the row** (`scripts/crawl_integration_roster.py`): in `roles_from_vocabulary`, after the entity rules produce their rows, walk `vocab.get("service", {})` against `lexicon.SERVICE_ROLE_RULES`:

```python
    svc = vocab.get("service") or {}
    for role, rule in getattr(lexicon, "SERVICE_ROLE_RULES", {}).items():
        hits = [n for n, b in svc.items()
                if any(re.search(p, n, re.I) for p in rule["any"])
                and (not rule.get("fields_any")
                     or any(re.search(p, f, re.I) for p in rule["fields_any"] for f in b.get("fields", ())))]
        release = next((n for n in svc if any(re.search(p, n, re.I) for p in rule.get("release_any", ()))), None)
        if hits and release:
            out[role] = {"platform": "service", "keys": tuple(sorted(hits)), "options": (),
                         "release": release, "target": rule.get("target", "device_id")}
```

`render_module` needs no change: rows are `repr`'d. `test_915_roster_is_not_a_claim.py::test_every_vocabulary_row_says_so` must still pass — read it; if it requires `kind_from == "vocabulary"` semantics per row, a service row satisfies the same rule (it came from the repo's own `services.yaml`).

- [ ] **Step 5: Run** `semtest tests/test_956_roster_services.py tests/test_915_roster_is_not_a_claim.py` → pass. **Step 6: Commit** `git commit -m "feat(#956): a role rule may match a service — and only with its undo"`

---

### Task 20: Regenerate the roster with service rows

- [ ] **Step 1:** `python3 scripts/crawl_integration_roster.py --refresh --write` (network; ~minutes), then `python3 scripts/crawl_integration_roster.py --roles-baseline` — **read the diff of `tests/roster_roles_baseline.json` before committing** (the file's own header: never regenerate it in the same breath as `--write` without reading the diff). Expect gains only (service rows for `huawei_solar`, possibly `keba`, `deye`), no lost keys.
- [ ] **Step 2:** `semtest tests/test_915_coverage_ratchet.py tests/test_915_roster_is_not_a_claim.py tests/test_855_brand_footprint_ratchet.py` → pass; the coverage baseline (`tests/roster_coverage_baseline.json`) moves with the reason in the commit.
- [ ] **Step 3: Commit** `git commit -m "chore(#956): roster regenerated — service-shaped roles for the brands that publish them"`

---

### Task 21: Propose a service role against the install's own services

**Files:**
- Modify: `hardware_detection.py` (`propose_roles_from_roster`, `_role_action`, `propose_for_installed`)
- Test: `tests/test_956_roster_services.py` (append)

- [ ] **Step 1: Write the failing test**

```python
from types import SimpleNamespace
from unittest.mock import patch

from custom_components.solar_energy_management import hardware_detection as hd


def _roster_with(vocab):
    return SimpleNamespace(ROLE_VOCAB={"huawei_solar": vocab}, ROSTER={"huawei_solar": {"kind": "energy"}})


HUAWEI_ROW = {"zero_export": {"platform": "service", "keys": ("set_zero_power_grid_connection", "set_maximum_feed_grid_power"),
                              "options": (), "release": "reset_maximum_feed_grid_power", "target": "device_id"}}


class TestServiceProposals:
    def test_a_service_the_install_registers_is_proposed_with_its_target(self):
        with patch.object(hd, "_roster", return_value=_roster_with(HUAWEI_ROW)):
            props = hd.propose_roles_from_roster(
                [], "huawei_solar",
                services_of=lambda dom: {"set_zero_power_grid_connection", "reset_maximum_feed_grid_power"},
                device_id_of=lambda dom: "dev-123")
        assert props["zero_export"]["service"] == "huawei_solar.set_zero_power_grid_connection"
        assert props["zero_export"]["release"] == "huawei_solar.reset_maximum_feed_grid_power"
        assert props["zero_export"]["target"] == "dev-123"

    def test_a_service_the_install_does_not_register_is_not_proposed(self):
        """An INTERSECTION, like the entity roles: the roster never invents hardware."""
        with patch.object(hd, "_roster", return_value=_roster_with(HUAWEI_ROW)):
            props = hd.propose_roles_from_roster([], "huawei_solar", services_of=lambda dom: set(),
                                                 device_id_of=lambda dom: "dev-123")
        assert "zero_export" not in props

    def test_a_service_present_without_its_release_is_not_proposed(self):
        with patch.object(hd, "_roster", return_value=_roster_with(HUAWEI_ROW)):
            props = hd.propose_roles_from_roster([], "huawei_solar",
                                                 services_of=lambda dom: {"set_zero_power_grid_connection"},
                                                 device_id_of=lambda dom: "dev-123")
        assert "zero_export" not in props

    def test_the_action_writes_the_adapters_own_key_and_is_write_gated(self):
        act = hd._role_action("zero_export")
        assert act == {"config_key": "inverter_device_id", "action": "set_option", "write_gated": True}

    def test_the_default_callables_are_not_required_by_entity_callers(self):
        """Every existing caller passes only (dev_entities, domain) — unchanged."""
        with patch.object(hd, "_roster", return_value=_roster_with({})):
            assert hd.propose_roles_from_roster([], "huawei_solar") == {}
```

- [ ] **Step 2: Run and watch it fail.**

- [ ] **Step 3: The intersection with services.** `propose_roles_from_roster(dev_entities, domain, *, state_of=None, strategy_values=None, services_of=None, device_id_of=None)`: after the entity roles, for each vocabulary row with `platform == "service"`, take `registered = services_of(domain)` (default: `lambda dom: set()`, so existing callers see no change) and propose only when a `keys` member AND the `release` are both registered; `target` comes from `device_id_of(domain)` (default: `lambda dom: ""`). Proposal shape: `{"service": f"{domain}.{name}", "release": f"{domain}.{release}", "target": target, "matched_key": name}`. In `propose_for_installed`, pass `services_of=lambda dom: set(hass.services.async_services().get(dom, {}))` and `device_id_of=` the same resolver `HuaweiBatteryAdapter._autodetect_battery_device` uses (lift it to a module function `resolve_brand_device_id(hass, domain)` so both share it). `_role_action`: consult `SEM_SERVICE_TARGET_KEY_FOR_ROLE` and add `"write_gated": role in WRITE_GATED_SERVICE_ROLES`; existing entity roles return exactly what they return today (pin that with a parametrised test over `SEM_CONFIG_KEY_FOR_ROLE`).

- [ ] **Step 4: Run** `semtest tests/test_956_roster_services.py tests/test_915_roster_at_runtime.py tests/test_915_write_verification.py` → pass. **Step 5: Commit** `git commit -m "feat(#956): a service-shaped role is proposed as an intersection with the install's own services"`

---

### Task 22: The Config card shows a service proposal

**Files:**
- Modify: `dashboard/card/src/cards/sem-config-card.js` (the `roster_proposals` renderer, `:1103-1140`), `dashboard/translations.json` (16 languages), then `npm run build`
- Test: `tests/test_956_roster_services.py` (append: the card's proposal branch names both `service` and `target` — parsed from the renderer's template, not `assert … in src`)

- [ ] A proposal carrying `service` renders `service · target` where an entity proposal renders the entity id, uses the same accept button through `_saveOption(p.config_key, target, fieldKey)`, and — when `write_gated` — the button reads *"Allow SEM to call this"* (`config_proposed_write_gated`, 16 languages) so the consent is explicit (#810). `test_963`'s parity guard covers plan keys only; add the two new keys to the parity check in `test_956_roster_services.py` the same way.

- [ ] **Commit** `git commit -m "feat(#956): the Config tab proposes a service-shaped control with explicit consent"`

---

### Task 23: The hand-written Huawei table becomes a roster read — parity pinned

**Files:**
- Modify: `coordinator/battery_adapters/huawei.py` (Task 7's verbs)
- Test: `tests/test_956_roster_services.py` (append)

- [ ] **Step 1: Write the failing test**

```python
class TestParity:
    def test_the_adapter_and_the_roster_name_the_same_services(self):
        """One source of truth: the recipe the adapter replays on removal IS
        the roster's release; the cut it issues IS the roster's key."""
        from custom_components.solar_energy_management.consts import integration_roster as r
        from custom_components.solar_energy_management.coordinator.battery_adapters.huawei import (
            HuaweiBatteryAdapter,
        )
        row = r.ROLE_VOCAB["huawei_solar"]["zero_export"]
        a = HuaweiBatteryAdapter(MagicMock(), {}); a._inverter_device_id = "dev"
        assert a.export_release_recipe()["service"] == row["release"]
        assert HuaweiBatteryAdapter.EXPORT_CUT_SERVICE in row["keys"]
```

- [ ] **Step 2–3:** `HuaweiBatteryAdapter.EXPORT_CUT_SERVICE = "set_zero_power_grid_connection"` and `EXPORT_RELEASE_SERVICE = "reset_maximum_feed_grid_power"` become class constants read from the roster row at import when present (`_roster_row("huawei_solar", "zero_export")`), falling back to the literals — so a roster regeneration that renames a service fails the parity test instead of silently diverging. Deye's row is entity-shaped (`select`), so its verb stays as built; the parity test for Deye pins `zero_export_to_load` against `ROLE_VOCAB["deye"]["battery_strategy"]["options"]` if the roster carries it, else is skipped with the reason.

- [ ] **Commit** `git commit -m "feat(#956): the Huawei export verbs read the roster row — parity pinned, no second source of truth"`

---

### Task 24: Docs

- [ ] `docs/ARCHITECTURE.md` roster section: the second shape, the intersection with `hass.services`, the consent gate. `docs/USER_GUIDE.md` "Detected hardware": a service proposal looks like *service · device* and asks for consent. `CHANGELOG.md`: ✨ **The roster can see a control that arrives as a service** (#956) — one bullet.
- [ ] **Commit** `git commit -m "docs(#956): a capability's shape is the author's choice — the roster now reads both"`

### Before merge (stage 4)

- Full suite green; ruff clean; CI green on the PR.
- Challenge record `~/claude-jobs/challenge-feature-956-roster-services.md` — ask ruflo to REFUTE: *"no service is ever proposed without its release, without being registered on the install, or without explicit consent; and the entity roles' proposals are byte-for-byte what they were."*
- Live check on .46: the Config tab lists Huawei's zero-export service with its device; accepting writes `inverter_device_id`; the guard (Task 7) then has a device without any manual config.
- Merge on approval with `SEM_FEAT_OK`.

## Before merge

- Full suite green from `/tmp/ha-config-arc`; `/tmp/venv-ci/bin/ruff check .` clean; CI green on the pushed branch (3.13 and 3.14 rungs).
- **Challenge record** at `~/claude-jobs/challenge-feature-921-grid-not-always-a-sink.md` — ask a ruflo reviewer to REFUTE, directly: *"No consumer of a sink verdict reads a price; the export guard can never engage on an unknown price or before its hold; every switch OFF reproduces beta.25's behaviour byte-for-byte; unload releases the cut before observer mode flips."*
- **Live on .175, observer ON, once** (build once, test many): hold the feed-in entity negative through `~/bin/sem-sim-compress.sh` on a compressed day; read `would_decisions` + `withheld_commands` for the WOULD `set_zero_power_grid_connection` on the real Huawei after the engage hold; read `sensor.sem_charging_state` for the `export_closed`/`export_reopens` rows and the pacer's trimmed landing time; confirm `curtailment.state == held_by_export_guard`; release the hold and read the WOULD `reset_maximum_feed_grid_power` after the release hold; toggle the guard off and confirm the guard state returns to `idle` with no write.
- Merge on approval with `SEM_FEAT_OK="#921 whole: verdicts + guard + adapters + sinks, all default-off, .175-proven" git push origin develop`.

## Self-review notes

- **Spec coverage:** §2 model → Task 3; §3 rows → Tasks 5–12; §4.1 → 3; §4.2 → 5; §4.3 → 6–7; §4.4 → 13; §4.5 → 8; §4.6 → 9–12; §4.7 → 14; §5 → 4, 8; §6 → 5, 6, 8; §7 → every task + 16; §8 stages 1–3 → Tasks 1–2, 3–8, 9–15; §9 → Task 8's probe hold and the Amber carve-out left in `get_current_export_rate`.
- **Type consistency:** `SinkVerdict(sink, state, reason, until)` everywhere; `ExportGuard.update(now, verdict_state, export_w) → ExportCommand(intent, watts, reason)`; adapter verbs `command_limit_export(watts)` / `command_release_export()`; `BatteryDecision.export_limit_w`; `FleetCycleState.sink_verdicts` / `FleetContext.sink_verdicts` / `BatteryView.sink_verdicts`; `FleetContext.ev_morning_window_open`.
- **Stage 4 coverage:** #956's four gaps (source, rule form, acceptance path, write gate) → Tasks 18, 19, 21/22, 21; the hand-written Huawei table → Task 23; every #915 structural test named where it must keep passing.
- **Known soft spots, named:** Task 1's calculator fixture and Task 12's surplus-controller gate name must be read from the file before writing (the plan says so at both points); Task 16's NEGATIVE-level tariff stub is copied from an existing test rather than invented; the Deye export cap is a mode, not watts, and says so.
