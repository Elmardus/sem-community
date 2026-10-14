# #996 — show only controls that can act here

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A house on a flat tariff gets no price knobs; a house without an
export limit gets no export guard; a house without a forecast gets no
forecast rows. Not a hide switch: SEM asks, per control, whether this
house can use it.

**Architecture:** Extend the one oracle that already does this for four
modules (`coordinator/install_modules.py`, #923): the `Module` enum grows
six capabilities, the `ENTITY_MODULES` table gets their rows, and every
existing consumer follows — `kept_descriptions` skips the entities,
`absent_entity_ids` lets the dashboard generator prune their references,
the config card renders nothing for a missing entity
(`sem-config-card.js:675`). One table, one verdict, no new switch.

**Tech stack:** Python only, plus the dashboard template. No card change
expected.

**Branch:** `feature/996-controls-that-can-act`, worktree
`/home/sem/sem-996`, off develop `4bc96443`. Status: PLAN. Build starts
after the mockup is approved (`docs/UI_PATTERNS.md`, mockup first).

---

## Where this sits in 2.2

Cluster 6 of the 30.09.2026 clustering (see
`2026-09-30-2.2-visibility.md` for the table). Own branch because it
removes entities on real installs and must be proven against PROD's
entity list before it ships.

## Open decisions (the plan assumes the first option)

1. **Remove or disable.** Not create the entity (like #923), or create it
   disabled in the entity registry (a user can re-enable). Not creating
   is what #923 does and what the generator prunes on. Disabled entities
   still show as "unavailable" rows on a hand-made dashboard.
2. **EV modes on a flat tariff.** The per-charger mode select still offers
   "cheap" and "solar + cheap". Dropping them is a select-options change,
   not an entity — a follow-up issue, not this branch.
3. **The advanced tier stays.** The config card's essential/advanced list
   (`_showsControl`) is about complexity, not ability. Both gates apply.

## The capabilities

Every one is read from SEM's own options, so the verdict is never
UNKNOWN (the #925 rule only matters for the Energy Dashboard reads).

| capability | present when | controls that go when absent | sensors that go |
|---|---|---|---|
| DYNAMIC_TARIFF | `tariff_mode == "dynamic"` or `price_entity` wired | `cheap_price_threshold`, `expensive_price_threshold` | price level, next cheap window, price-based spending |
| EXPORT_LIMIT | `export_limit_entity` wired | `export_guard_enabled`, `export_guard_override_external`, `export_guard_engage_s`, `export_guard_release_s` | export guard state rows |
| SOLAR_FORECAST | `dynamic_forecast_entity` wired or a forecast source configured (`forecast_source`) | `forecast_spending_enabled` (also needs BATTERY_CONTROL) | the `forecast_*` rows (14), `sem_pv_health` (#1022) |
| BATTERY_CONTROL | `battery_discharge_control_entity` or `battery_charge_power_limit_entity` wired | `battery_max_discharge_power`, `battery_charge_pacing_enabled`, `battery_may_export`, `battery_house_sink_enabled`, `battery_may_assist_ev` | pacing, scheduler rows that need a writable battery |
| PEAK_LIMIT | `target_peak_limit > 0` | `demand_charge_rate` | peak rows |
| PV_SIZE | `system_size_kwp > 0` | — | `pv_daily_specific_yield`, `pv_performance_vs_forecast`, `pv_estimated_annual_degradation`, `pv_degradation_trend`; `system_investment_cost > 0` gates the ROI rows the same way |

The exact rows are produced by Task 2's script, not by hand. A control
that is useful everywhere (`update_interval`, `observer_mode`,
`electricity_import_rate` — the flat price itself) is listed as
ALWAYS_USEFUL with a one-line reason, so the table is complete.

## Tasks

### Task 1: mockup (gate)

**Files:** scratchpad only; HA-TEST.

- [ ] Configure HA-TEST as the plain house: static tariff, no export
      limit entity, no forecast, battery read-only, no peak limit.
- [ ] Headless Playwright: screenshots of the Configuration and Control
      views before. List every row that will go, per capability.
- [ ] Send the two PNGs and the list for review. Build only after an OK.

### Task 2: the table is complete by construction

**Files:** `coordinator/install_modules.py`, new
`scripts/audit_control_needs.py`, test
`tests/test_996_every_control_has_a_need.py`.

- [ ] Test: every key in `number.py`, `switch.py`, `select.py`,
      `button.py` is either in `ENTITY_MODULES` or in `ALWAYS_USEFUL`
      with a reason; a key in both fails; an unknown key fails. Sensors:
      the `forecast_*`, `pv_*`, `export_guard_*`, price and peak rows
      must be in the table.
- [ ] Add the six `Module` members; `module_verdict` returns them via
      `_config_only`; `tests/test_923_module_growth.py` is extended, not
      bypassed.
- [ ] The script prints the verdict and the kept/dropped entity list for
      a given options JSON (used for the proof on PROD).
- [ ] Commit `feat(#996): capabilities in the install-modules table`.

### Task 3: platforms and generator follow

**Files:** none new — `kept_descriptions`, `absent_entity_ids`,
`_prune_absent_modules` already read the table. Tests
`tests/test_996_platform_gating.py`, `tests/test_996_dashboard_prune.py`.

- [ ] Test: plain house → the price numbers, export switches, forecast
      switch do not exist; the dashboard template has no reference to
      them; the Energy tab keeps its layout.
- [ ] Test: PROD-shaped options (Huawei writable battery, KEBA, export
      limit wired, forecast on) → nothing is dropped that exists today
      (`tests/test_923_real_installs.py` pattern, entity list from the
      PROD diagnostics).
- [ ] Commit `feat(#996): platforms and dashboard follow the table`.

### Task 4: the house changes

**Files:** `__init__.py` (the options update listener already reloads),
test `tests/test_996_options_change.py`.

- [ ] Test: wiring a `price_entity` in the options → after reload the
      price numbers exist; unwiring → they go and their registry rows are
      removed the way #923 removes a module's (verify the #923 cleanup
      path at build; if #923 leaves registry rows, add the removal here).
- [ ] Commit `feat(#996): controls appear and go with the wiring`.

### Task 5: cards tolerate absence

**Files:** `dashboard/card/src/cards/sem-config-card.js`,
`sem-control-card.js`, test `tests/test_996_cards_tolerate_absent.py`
(pattern `tests/test_923_cards_tolerate_absent.py`).

- [ ] Test: every entity id the two cards reference is either in the
      table or always useful; a missing entity renders nothing, not an
      empty row or a header with no rows (a section whose rows all went
      goes with them).
- [ ] Fix the sections that would leave an empty header. `npm run build`.
- [ ] Commit `feat(#996): config and control cards drop empty sections`.

### Task 6: docs and changelog

- [ ] `docs/DASHBOARD_GUIDE.md` and `docs/USER_GUIDE.md`: "SEM shows a
      control only when your house can use it; wire the entity and it
      appears."
- [ ] CHANGELOG `[Unreleased]`, one line ≤ 25 words.
- [ ] Commit `docs(#996): controls that can act`.

## Proof

1. Full suite + ruff; CI both rungs; ruflo reviewer told to REFUTE:
   "no install loses an entity it can use" (feed it PROD's and
   HA-TEST's options).
2. .175 (branch testing, observer ON): `SRC=/home/sem/sem-996 ~/bin/sem-deploy-175.sh`,
   entity count before/after, then `~/bin/sem-sim-compress.sh 10.10.20.175 18.0 30`
   to show nothing breaks across a simulated day.
3. PROD only on a "go": `scripts/audit_control_needs.py` on the
   PROD options first — the dropped list must be empty or approved line
   by line. Then deploy, compare the entity list.
4. #996 has an external reporter? No — ours. Close with proof when PROD
   shows it. Merge with `SEM_FEAT_OK` on approval.
