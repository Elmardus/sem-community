# #871 — a negative export price is a cost (steps 0-2 of arc #921)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a negative export price visible, measurable, and actionable by every sink SEM already owns — without building curtailment.

**Architecture:** Three layers, cheapest first. **Measure** (a counter for kWh exported while the rate was negative, and what it cost) so the exposure is known before anything acts. **See** (remove the `max(0.0, …)` clamp so the planner meets a negative price as a cost). **Act** (give `FleetContext` a price it has never had, and let the EV, loads and battery accept energy they would normally decline while export is negative — behind a default-off switch, inert until deliberately woken).

**Tech Stack:** Python 3.13/3.14, Home Assistant custom integration, pytest + pytest-homeassistant-custom-component. Tests run from the CI layout via `~/bin/semtest`.

---

## Scope

**In:** steps 0, 1 and 2 of **#871**, the meter dimension of arc **#921**.

**Arc:** #921 — "where may the battery's energy GO". #871 is one of its three children (#879 the house, #892 the EV, #871 the meter). #921's whole point is that solving them one at a time produces three answers that disagree, so whatever is built here has to fit the shared model, not just this child.

**Out:** step 3, curtailment. It needs a role in the #915 roster and per-brand write adapters (Huawei publishes four services, Deye a select), plus a hand-back obeying the #908/#936/#949 rule. That is its own plan and may never be needed — a house with a battery and a car absorbs nearly every negative hour without it.

**Read first:** `gh issue view 921` (the arc and the shared model — the verified ground for this child is in its comments) and `gh issue view 871`, **including the correction comments** — `export_rate` lives on `ArbitrageSignals`, *not* on `FleetContext`, and it is not unread.

## Two facts that shape every task

1. **No install has a negative export rate today.** PROD is `provider: static`, a fixed 0.075/kWh. Every behaviour change here is dormant until someone moves to a spot feed-in tariff. That is a gift: it means these tasks can land without changing what any current user experiences, and it is why step 2 still ships behind a switch — untested-in-anger code should not wake itself.
2. **The stored-energy side is already guarded.** `battery_charge_scheduler.py:583` refuses to sell below `arbitrage_min_export_price` (default 0.20), and #931 closed the unchecked forecast-spend sell. Do not re-solve selling. This arc is about **solar surplus**.

## File structure

| File | Responsibility in this plan |
|---|---|
| `coordinator/energy_calculator.py` | Task 1 — accrue kWh and cost exported while the rate is negative, at the existing export seam (`:643-650`) |
| `coordinator/types.py` | Tasks 1, 4 — the `EnergyTotals` fields and their `to_dict` entries |
| `sensor.py` | Task 2 — two sensors publishing the exposure |
| `coordinator/day_ledger.py` | Task 3 — remove the `max(0.0, export_rate)` clamp at `:111` |
| `coordinator/charger_types.py` | Task 4 — `FleetContext.export_rate` (new field) |
| `coordinator/build_view.py`, `coordinator/coordinator.py` | Task 4 — populate it at both construction sites |
| `coordinator/ev_control.py`, `coordinator/surplus_controller.py` | Task 5 — the negative-export posture, behind the switch |
| `switch.py`, `strings.json`, `translations/*.json` | Task 5 — the default-off switch |
| `docs/USER_GUIDE.md`, `CHANGELOG.md` | Task 6 |

---

### Task 1: Measure the exposure

**Files:**
- Modify: `coordinator/energy_calculator.py:642-653`
- Modify: `coordinator/types.py:412-460` (`EnergyTotals`), `:1171` (`to_dict`)
- Test: `tests/test_871_negative_export_exposure.py`

- [ ] **Step 1: Write the failing test**

```python
"""#871 step 0 — measure what a negative export price costs, before acting.

Nothing today records "export was negative for two hours and SEM pushed
6 kWh into it". Without that number, the decision to build curtailment
(step 3, which DESTROYS energy) rests on a guess.
"""
from custom_components.solar_energy_management.coordinator.energy_calculator import (
    EnergyCalculator,
)


class TestNegativeExportIsCounted:
    def test_a_negative_rate_accrues_kwh_and_cost(self, calc_and_power):
        calc, power = calc_and_power
        calc._export_rate = -0.05          # you PAY 0.05/kWh to export
        power.grid_export_power = 2000.0   # 2 kW for one hour
        energy = calc.calculate(power, interval_hours=1.0)
        assert energy.daily_grid_export_negative == 2.0
        assert round(energy.daily_grid_export_negative_cost, 3) == 0.10

    def test_a_positive_rate_accrues_nothing(self, calc_and_power):
        calc, power = calc_and_power
        calc._export_rate = 0.075
        power.grid_export_power = 2000.0
        energy = calc.calculate(power, interval_hours=1.0)
        assert energy.daily_grid_export_negative == 0.0
        assert energy.daily_grid_export_negative_cost == 0.0

    def test_a_zero_rate_accrues_nothing(self, calc_and_power):
        """Zero is worthless, not costly — only a NEGATIVE rate is a loss."""
        calc, power = calc_and_power
        calc._export_rate = 0.0
        power.grid_export_power = 2000.0
        energy = calc.calculate(power, interval_hours=1.0)
        assert energy.daily_grid_export_negative == 0.0
```

The `calc_and_power` fixture must build a real `EnergyCalculator` plus a `PowerReadings`. Copy the construction from the nearest existing sibling — `grep -rn "EnergyCalculator(" tests/ | head -3` — rather than inventing one.

- [ ] **Step 2: Run it and watch it fail**

```bash
~/bin/semtest tests/test_871_negative_export_exposure.py -q
```

Expected: `AttributeError: 'EnergyTotals' object has no attribute 'daily_grid_export_negative'`.

- [ ] **Step 3: Add the fields**

In `coordinator/types.py`, beside `daily_grid_export` (`:438`):

```python
    #: (#871) kWh exported while the export rate was NEGATIVE — energy the
    #: meter charged for instead of paying for. Zero on every fixed-tariff
    #: install, which is all of them today; the counter exists so the cost
    #: of NOT acting is measurable before anything is built to act.
    daily_grid_export_negative: float = 0.0
    daily_grid_export_negative_cost: float = 0.0
```

and in `to_dict` beside `:1171`:

```python
            "daily_grid_export_negative_kwh": self.energy.daily_grid_export_negative,
            "daily_grid_export_negative_cost": self.energy.daily_grid_export_negative_cost,
```

- [ ] **Step 4: Accrue at the existing seam**

In `coordinator/energy_calculator.py`, inside the `if power.grid_export_power >= MIN_POWER_THRESHOLD:` block (`:643`), after the existing two `_accumulate*` calls:

```python
            # (#871) The same kWh, counted again when the meter was hostile.
            # A separate key rather than a sign on the existing one: the
            # export counter is an ENERGY total a user reads as "what I sent
            # out", and folding a price signal into it would make a bad hour
            # look like less export rather than costlier export.
            if self._export_rate < 0:
                self._accumulate("grid_export_negative", today, month_key,
                                 year_key, export_increment)
                self._accumulate_cost(
                    "cost_export_negative", today, month_key, year_key,
                    export_increment * abs(self._export_rate))
```

and beside the existing `energy.daily_grid_export = …` reads (`:651`):

```python
        energy.daily_grid_export_negative = self._get_daily(
            "grid_export_negative", today)
        energy.daily_grid_export_negative_cost = self._get_daily_cost(
            "cost_export_negative", today)
```

- [ ] **Step 5: Run the test**

```bash
~/bin/semtest tests/test_871_negative_export_exposure.py -q
```

Expected: 3 passed.

- [ ] **Step 6: Commit**

```bash
git add coordinator/energy_calculator.py coordinator/types.py tests/test_871_negative_export_exposure.py
git commit -m "feat(#871): count what a negative export price actually costs

Step 0 of the arc, and deliberately first: nothing today records that
export went negative and SEM kept pushing into it. Curtailment (step 3)
destroys energy, so the decision to build it should rest on a measured
number rather than a guess. Zero on every fixed-tariff install."
```

---

### Task 2: Publish the exposure

**Files:**
- Modify: `sensor.py` (description list beside `daily_grid_export_energy` at `:478`)
- Test: `tests/test_871_negative_export_exposure.py` (append)

- [ ] **Step 1: Write the failing test**

```python
class TestTheExposureIsVisible:
    def test_both_sensors_are_declared(self):
        from custom_components.solar_energy_management import sensor as sensor_mod

        keys = {d.key for d in sensor_mod.SENSOR_TYPES}
        assert "daily_grid_export_negative_kwh" in keys
        assert "daily_grid_export_negative_cost" in keys
```

If `SENSOR_TYPES` is not the list's name, find it: `grep -n "daily_grid_export_energy" -B 30 sensor.py | grep -E "^[0-9]+[:-][A-Z_]+ *[:=]"`.

- [ ] **Step 2: Run it and watch it fail**

```bash
~/bin/semtest tests/test_871_negative_export_exposure.py::TestTheExposureIsVisible -q
```

Expected: `AssertionError`.

- [ ] **Step 3: Add the descriptions**

In `sensor.py`, immediately after the `daily_grid_export_energy` description (`:478-482`):

```python
    # (#871) What a hostile meter cost today. Both stay at 0.0 on a fixed
    # feed-in tariff, which is every install until someone opts into spot.
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

- [ ] **Step 4: Run the whole file, then the translation-parity guard**

```bash
~/bin/semtest tests/test_871_negative_export_exposure.py tests/test_674_translation_parity.py -q
```

Expected: all pass. If parity fails, add the two entity names to `strings.json` **and all 16** `translations/*.json` — that guard exists because a key added to one file only is the commonest regression in this repo.

- [ ] **Step 5: Commit**

```bash
git add sensor.py strings.json translations tests/test_871_negative_export_exposure.py
git commit -m "feat(#871): publish the negative-export exposure as two diagnostics"
```

---

### Task 3: Let the planner see a negative price

**Files:**
- Modify: `coordinator/day_ledger.py:111`
- Test: `tests/test_871_negative_export_exposure.py` (append)

- [ ] **Step 1: Write the failing test**

```python
class TestTheLedgerPricesASurplusSlotHonestly:
    """`price = max(0.0, export_rate)` is the one line that makes a negative
    rate indistinguishable from a free one. A planner cannot prefer another
    sink over a price it cannot see."""

    def _slots(self, export_rate):
        from custom_components.solar_energy_management.coordinator.day_ledger import (
            build_day_slots,
        )
        # Build the smallest bright-day window that yields a surplus slot.
        # Copy the argument shape from tests/test_755_*.py or
        # tests/test_820_pacing_reads_the_house_profile.py — do NOT invent it.
        ...

    def test_a_negative_export_rate_reaches_the_slot(self):
        slots = self._slots(-0.05)
        assert slots, "the fixture must produce at least one surplus slot"
        assert slots[0].price == -0.05

    def test_a_positive_rate_is_unchanged(self):
        assert self._slots(0.075)[0].price == 0.075

    def test_a_missing_rate_is_still_zero(self):
        assert self._slots(None)[0].price == 0.0
```

Fill `_slots` from an existing caller before writing the implementation — `grep -rn "build_day_slots(" tests/ | head -3`.

- [ ] **Step 2: Run it and watch the first case fail**

```bash
~/bin/semtest tests/test_871_negative_export_exposure.py::TestTheLedgerPricesASurplusSlotHonestly -q
```

Expected: `assert 0.0 == -0.05`.

- [ ] **Step 3: Remove the clamp**

`coordinator/day_ledger.py:111`:

```python
                # (#871) NOT max(0.0, …). The clamp made a negative export
                # rate — one you PAY — look identical to a free kWh, and a
                # planner cannot prefer another sink over a cost it cannot
                # see. `or 0.0` still handles an absent rate, which is the
                # only case the clamp was ever really covering.
                price=float(export_rate or 0.0),
```

- [ ] **Step 4: Run the surrounding suites, not just the new test**

```bash
~/bin/semtest tests/test_871_negative_export_exposure.py tests/test_755_learning_layer.py \
  tests/test_820_charge_pacing.py tests/test_820_pacing_reads_the_house_profile.py \
  tests/test_778_spendable.py -q -rf
```

Expected: all pass. Anything that fails here is a consumer that silently relied on prices being non-negative — that is the finding, not a nuisance. Fix the consumer, do not restore the clamp.

- [ ] **Step 5: Commit**

```bash
git add coordinator/day_ledger.py tests/test_871_negative_export_exposure.py
git commit -m "fix(#871): a negative export price is a cost, not a free kWh

day_ledger clamped the signed rate to zero, so the planner met a hostile
meter and a generous one as the same number. #523 kept the sign on
purpose; this is the consumer that lost it again one layer down."
```

---

### Task 4: Give the fleet view a price

**Files:**
- Modify: `coordinator/charger_types.py:680+` (`FleetContext`)
- Modify: `coordinator/build_view.py:186`, `coordinator/coordinator.py:7407`
- Test: `tests/test_871_negative_export_exposure.py` (append)

This task is **pure plumbing and changes no decision.** Keep it that way — a field arriving and a field being acted on are separate commits, so a bisect can tell them apart.

- [ ] **Step 1: Write the failing test**

```python
class TestTheFleetViewCarriesThePrice:
    """FleetContext is what every charger's decide() actually sees, and it has
    never carried a price — only `grid_export_w`, which is a power. (The
    `export_rate` on ArbitrageSignals is a different object and IS consumed,
    by the arbitrage floor.)"""

    def test_the_field_exists_and_defaults_to_zero(self):
        from custom_components.solar_energy_management.coordinator.charger_types import (
            FleetContext,
        )
        assert FleetContext().export_rate == 0.0

    def test_build_view_populates_it_from_the_fleet_state(self):
        from custom_components.solar_energy_management.coordinator.ast_contracts import (
            call_kwargs,
        )
        from custom_components.solar_energy_management.coordinator import build_view

        kwargs = call_kwargs(build_view.build_charger_view, "FleetContext")
        assert kwargs and "export_rate" in kwargs[0]
```

Use `tests/ast_contracts.py` (not `coordinator/`) — check the import path with `grep -rn "from.*ast_contracts import" tests/ | head -2` — and confirm the builder's function name with `grep -n "^def " coordinator/build_view.py | head`.

- [ ] **Step 2: Run it and watch it fail**

```bash
~/bin/semtest tests/test_871_negative_export_exposure.py::TestTheFleetViewCarriesThePrice -q
```

Expected: `AttributeError: 'FleetContext' object has no attribute 'export_rate'`.

- [ ] **Step 3: Add the field**

In `coordinator/charger_types.py`, inside `FleetContext`, beside `grid_export_w`:

```python
    export_rate: float = 0.0
    """(#871) The SIGNED current export price (/kWh) — negative means the
    meter charges you to export. Zero when unknown or on a fixed tariff.

    `ArbitrageSignals` carries the same quantity for the SELL decision; this
    is the same number reaching the surplus decision, which has never had it.
    Both read `provider.get_current_export_rate()`, whose sign #523 restored
    after an `abs()` turned "you pay to export" into a credit."""
```

- [ ] **Step 4: Populate it at both sites**

`coordinator/build_view.py:186`, beside `tariff_level=fleet_state.tariff_level`:

```python
        # (#871) the price rides the same one-place thread as the level.
        export_rate=float(getattr(fleet_state, "export_rate", 0.0) or 0.0),
```

`coordinator/coordinator.py:7407`, in the same dataclass call:

```python
            export_rate=float(getattr(self, "_current_export_rate", 0.0) or 0.0),
```

Confirm the coordinator's own accessor first — `grep -n "export_rate" coordinator/coordinator.py | head` — and use whatever it already computes rather than adding a second source. If `fleet_state` has no `export_rate`, add it there too, populated from the same provider call the arbitrage path uses.

- [ ] **Step 5: Run the charger suites**

```bash
~/bin/semtest tests/test_multi_charger_control.py tests/test_v14_integration.py \
  tests/test_589_percharger_astguard.py tests/test_871_negative_export_exposure.py -q -rf
```

Expected: all pass, and no decision changes — this commit adds a number nobody reads yet.

- [ ] **Step 6: Commit**

```bash
git add coordinator/charger_types.py coordinator/build_view.py coordinator/coordinator.py tests/test_871_negative_export_exposure.py
git commit -m "feat(#871): the fleet view carries the export price

Plumbing only, no decision changes. FleetContext is what every charger's
decide() sees and it has never had a price -- only grid_export_w, a power.
(ArbitrageSignals.export_rate is a different object and is consumed, by
the sell floor; this arc is about solar surplus.)"
```

---

### Task 5: The negative-export posture, asleep

**Files:**
- Modify: `coordinator/ev_control.py`, `coordinator/surplus_controller.py`
- Modify: `switch.py`, `strings.json`, `translations/*.json` (16 files)
- Test: `tests/test_871_negative_export_exposure.py` (append)

**It ships default-off.** Gate 4 of the release train: finished work whose behaviour stays off may merge. No install can exercise this today (every one is on a fixed tariff), so it must not wake itself — and a switch is also how a spot-tariff user opts in once they have read what it does.

- [ ] **Step 1: Write the failing test**

```python
class TestTheSinksPreferAnythingToAHostileMeter:
    """The mirror of the negative-IMPORT path SEM already runs end to end:
    there, a negative price makes SEM charge everything. Here, a negative
    export price should make the sinks accept energy they would decline."""

    def test_the_posture_is_off_by_default(self):
        from custom_components.solar_energy_management.coordinator.export_posture import (
            export_posture,
        )
        assert export_posture(export_rate=-0.05, enabled=False) == "normal"

    def test_a_negative_rate_with_the_switch_on_prefers_other_sinks(self):
        from custom_components.solar_energy_management.coordinator.export_posture import (
            export_posture,
        )
        assert export_posture(export_rate=-0.05, enabled=True) == "avoid_export"

    def test_a_positive_rate_is_normal_even_when_enabled(self):
        from custom_components.solar_energy_management.coordinator.export_posture import (
            export_posture,
        )
        assert export_posture(export_rate=0.075, enabled=True) == "normal"

    def test_zero_is_normal(self):
        from custom_components.solar_energy_management.coordinator.export_posture import (
            export_posture,
        )
        assert export_posture(export_rate=0.0, enabled=True) == "normal"

    def test_an_unknown_rate_is_normal(self):
        """#925: could-not-ask is not 'the meter is hostile'. Acting on a
        missing price would push energy around for no reason."""
        from custom_components.solar_energy_management.coordinator.export_posture import (
            export_posture,
        )
        assert export_posture(export_rate=None, enabled=True) == "normal"
```

- [ ] **Step 2: Run it and watch it fail**

```bash
~/bin/semtest tests/test_871_negative_export_exposure.py::TestTheSinksPreferAnythingToAHostileMeter -q
```

Expected: `ModuleNotFoundError: coordinator.export_posture`.

- [ ] **Step 3: Write the decision, in one pure place**

Create `coordinator/export_posture.py`:

```python
"""#871 — one answer to "is the meter worth feeding right now?".

Three call sites answer versions of this question today with three rules:
the arbitrage floor (`arbitrage_min_export_price`), forecast_sell's own
check (#931), and — for solar surplus — nothing at all. This is that
question having ONE answer, the same instinct as #638's one gate and
#576's one device list.

Pure and tiny on purpose: the posture is a verdict, and every consumer
reads the same verdict rather than re-deriving it from a price.
"""
from __future__ import annotations

from typing import Optional

#: Business as usual: export is worth something, or we cannot tell.
NORMAL = "normal"
#: The meter charges us. Prefer ANY other sink; never curtail here (that is
#: step 3, a separate build, and the only step that destroys the kWh).
AVOID_EXPORT = "avoid_export"


def export_posture(*, export_rate: Optional[float],
                   enabled: bool) -> str:
    """The one verdict. ``enabled`` is the user's opt-in switch.

    An UNKNOWN rate is NORMAL, never AVOID_EXPORT (#925: "I could not ask"
    is not an answer). Zero is normal too — a worthless kWh is not a costly
    one, and the two must not be conflated the way day_ledger's old clamp
    conflated them in the other direction.
    """
    if not enabled:
        return NORMAL
    if export_rate is None:
        return NORMAL
    try:
        rate = float(export_rate)
    except (TypeError, ValueError):
        return NORMAL
    return AVOID_EXPORT if rate < 0.0 else NORMAL
```

- [ ] **Step 4: Run the test**

```bash
~/bin/semtest tests/test_871_negative_export_exposure.py::TestTheSinksPreferAnythingToAHostileMeter -q
```

Expected: 5 passed.

- [ ] **Step 5: Add the switch, default off**

In `switch.py`, beside the other feature switches, add key `export_posture_enabled`. Copy the **exact** pattern of an existing default-off switch — `grep -n "battery_charge_pacing_enabled" switch.py` — including its persisted-flag handling, and add the same key to `PERSISTED_FLAG_DEFAULTS` in `persisted_flags.py` with `False`, which is where #820 and #778 record theirs.

- [ ] **Step 6: Consume the posture in the two sinks**

In `coordinator/ev_control.py` and `coordinator/surplus_controller.py`, read the posture once per cycle from `fleet.export_rate` plus the switch, and where a sink currently declines surplus for a *threshold* reason (a min-surplus gate, a buffer floor), let `AVOID_EXPORT` lower that bar. Do **not** touch safety gates — the peak guard (#864), the battery reserve floor, and any stop-war logic stay exactly as they are. Pin each relaxation with its own test showing the same input declining under `NORMAL` and accepting under `AVOID_EXPORT`.

- [ ] **Step 7: Full suite**

```bash
~/bin/semtest tests/ -q -rf > /tmp/871-suite.txt 2>&1; echo "EXIT=$?"; grep -c FAILED /tmp/871-suite.txt
```

Expected: `EXIT=0`, `0`.

- [ ] **Step 8: Commit**

```bash
git add coordinator/export_posture.py coordinator/ev_control.py coordinator/surplus_controller.py switch.py persisted_flags.py strings.json translations tests/test_871_negative_export_exposure.py
git commit -m "feat(#871): prefer any sink over a hostile meter -- asleep

One verdict, in one pure place, consumed by the EV and the surplus loads:
the mirror of the negative-IMPORT path SEM already runs. Default off. No
install has a negative export rate today (every one is a fixed tariff),
so this must not wake itself; a spot-tariff user opts in deliberately.

Unknown and zero are both NORMAL -- #925, and the inverse of the clamp
this arc removed in day_ledger."
```

---

### Task 6: Docs and changelog

**Files:**
- Modify: `docs/USER_GUIDE.md`, `CHANGELOG.md`

- [ ] **Step 1: Add a USER_GUIDE section**

Under the tariff material, a short "When export pays nothing" section: what the two new diagnostics mean, that they stay at zero on a fixed feed-in tariff, what the switch does when turned on, and that SEM does **not** curtail — it only moves energy to better sinks.

- [ ] **Step 2: CHANGELOG**

```markdown
- ✨ **SEM can tell a hostile meter from a generous one** (#871, #871). On a
  dynamic feed-in tariff the export price can go negative — you pay to
  export. SEM now measures that exposure (two new diagnostics, zero on a
  fixed tariff), prices it honestly in the planner instead of clamping it to
  zero, and — when you turn the new switch on — prefers putting surplus into
  the car, the battery or a load rather than the meter. SEM does not curtail
  your inverter; nothing is thrown away.
```

- [ ] **Step 3: Commit**

```bash
git add docs/USER_GUIDE.md CHANGELOG.md
git commit -m "docs(#871): what a negative export price means and what SEM does"
```

---

## Before merge

- `~/bin/semtest tests/ -q -rf` green (expect ~10,300+).
- `/tmp/venv-ci/bin/ruff check .` clean.
- A **challenge record** at `~/claude-jobs/challenge-feature-871-negative-export.md` — `sem-ready.sh` gate 2b refuses a feature branch without one. Ask a ruflo reviewer to REFUTE: *"no sink relaxation in this change can move energy in a way the user did not ask for, and no safety gate is weakened by AVOID_EXPORT."*
- **A live simulation on .175**, because the posture cannot be exercised by any real tariff: hold a negative export rate, confirm the posture flips, the sinks relax, and that flipping the switch off restores the previous behaviour exactly.
- Merge on approval with `SEM_FEAT_OK` — it is an enhancement.

## Self-review notes

- **Spec coverage:** step 0 → Tasks 1-2; step 1 → Task 3; step 2 → Tasks 4-5; docs → Task 6. Step 3 (curtailment) is deliberately out of scope and named as such.
- **Known soft spots, flagged rather than hidden:** Task 3's test fixture and Task 4's `fleet_state.export_rate` source must be read from existing callers before writing — the plan says so at each point rather than inventing a shape. Task 5 Step 6 is the only step that names a behaviour change without giving the exact diff, because which thresholds may relax is a judgement that needs the surrounding code in view; it carries an explicit invariant (safety gates untouched) and a per-relaxation test requirement instead.
