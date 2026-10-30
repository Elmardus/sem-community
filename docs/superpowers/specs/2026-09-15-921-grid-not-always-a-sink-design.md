# The grid is not always a sink — where else a kWh can go, and making sure it does

**Arc:** #921 · **Children:** #955 #871 #926 #879 #892 #956 · **Branch:** `feature/921-grid-not-always-a-sink`
**Status:** design approved in conversation 15.09.2026 (adapters first, #956 after). Milestone 2.1 — all six, deliberately (#921, 13.09).

## 1. Problem

SEM's stored and harvested energy has two destinations today: the grid (#778's forecast sell) and the
EV (the assist). The house is not one of them, "hold it" is not one of them, and nothing says when a
destination has *stopped being one*. On a spot feed-in tariff the export price goes negative and SEM
keeps pushing surplus into the meter and pays for it (#871); it cannot stop, because it has no export
write path on any brand (#955); it fills the battery to the sunset target while the cheapest hours of
the day sell for nothing (#926); a house with no car has no way to spend its pack on itself (#879);
a car that leaves at 07:00 cannot take the pack's remainder before it goes (#892).

Six issues, one model. Fixing them one at a time produces six rankings that disagree at the edges.

## 2. The model — a sink has a state, not a price

The framing (13.09): *"it seems more natural about the energy and avoid costs with negative grid
export"* and *"SEM has to make sure we do not export."* Two consequences fix the design:

1. **The balance layer reasons about energy, never money.** Price already decides WHEN in SEM (cheap
   hours, negative-import force charge, the arbitrage floor) through the tariff **level** the provider
   classifies (`NEGATIVE / CHEAP / NORMAL / EXPENSIVE`). It must not start deciding HOW MUCH.
2. **A negative export price is not a re-ranking. It closes a sink.** And "closed" must be a guarantee,
   which only a limit at the meter can deliver — the same shape as #864's peak guard, which caps import
   and has every device below it obey.

So the one model is a **per-cycle verdict per sink**:

```
SinkVerdict(sink, state ∈ {OPEN, HELD, CLOSED}, reason, until)
```

- **OPEN** — energy may go there (subject to the sink's own constraints: floors, gates, deadlines).
- **HELD** — it may, but not now; keep the energy where it is (the battery holding through cheap hours;
  headroom held before the meter closes).
- **CLOSED** — it may not; and for the grid this is *enforced*, not preferred.

Three axes stay separate, as they already are:

| axis | answers | lives |
|---|---|---|
| permission | *may* this battery feed that sink at all | `consts/battery_permissions.py` (`may_export`, `may_assist_ev`) |
| order | *who goes first* when several are open | the #576 device list / priority axis |
| **verdict (new)** | *is the sink open right now* | `coordinator/sink_verdicts.py` — one pure function per cycle |

The verdict is computed once per cycle from the tariff level, the forecast, the clock and the
permission axis, threaded into `FleetCycleState` like every other fleet input, and CONSUMED by the
existing routers: `surplus_controller` for harvested surplus, `decide_battery` for stored energy,
`charge_pacing` for the fill, the plan composer for the card. No consumer re-derives it from a price.

## 3. The children as rows

| child | verdict | who enforces it | constraints it keeps |
|---|---|---|---|
| #955 | grid export **CLOSED** while level is `NEGATIVE` | `ExportGuard` — a meter limit beside `peak_guard.py`; intents `LIMIT_EXPORT(w)` / `RELEASE_EXPORT` through `actuate_battery`'s observer seam; per-brand adapter verbs | hysteresis; refuse under external scheduling; hand back on release, unload, disable, removal |
| #871 | (the price read + the sinks absorb first) | steps 0–2 already planned: measure, unclamp, `export_posture` → folded into the verdict | absorb before clip — the guard takes only what is left |
| #926 | battery **HELD** headroom | `charge_pacing` gains a second "land full by": the next CLOSED window's start | the #820 floor, weak-day and trust refusals unchanged |
| #879 | house **OPEN** in `EXPENSIVE`, **HELD** in `CHEAP` | `LIMIT_DISCHARGE` reused (already brand-agnostic) | #878 dynamic floor, reserve; the spread test stays in the scheduler (WHEN layer) |
| #892 | EV **OPEN** in `[departure − N h, departure]` | the #537 assist gate, extended with a morning window | forecast must refill the pack today; a drain floor; departure from `ev_departure_time_entity` |
| #956 | — | the roster learns service-shaped roles; the brand table from #955 becomes a crawled role | after the guard ships — its value is the NEXT brand |

**Sequencing (decision 15.09): adapters first, #956 after.** The adapter registry is built in the ROLE
shape now (`zero_export` capability, per-brand dialect), so #956 fills it rather than reshaping it.

## 4. Components

### 4.1 `coordinator/sink_verdicts.py` — the one pure place (new)

```python
@dataclass(frozen=True)
class SinkVerdict:
    sink: str            # "grid_export" | "battery" | "house" | "ev" | "loads"
    state: str           # OPEN | HELD | CLOSED
    reason: str          # token-bearing sentence; cards render the token
    until: datetime|None # when this state is expected to change (plan/card)

def sink_verdicts(*, now, tariff_level, upcoming_levels, export_rate_known,
                  permissions, forecast, departure, enabled) -> dict[str, SinkVerdict]
```

Rules, in this order — and nothing else:
- **Unknown is OPEN, never CLOSED.** A missing or unreadable export price is "could not ask" (#925,
  class 86): the guard never engages on it, the sinks never relax on it.
- `grid_export` CLOSED ⇔ level `NEGATIVE` and the export guard switch is on. Amber's inverted sign is
  handled where it already is (`get_current_export_rate`); the verdict reads the classified level.
- `battery` HELD when a CLOSED window starts within the pacing horizon (#926), else OPEN.
- `house` OPEN in `EXPENSIVE` (spend stored energy on the house), HELD in `CHEAP`/`NEGATIVE` (let the
  house import; keep the pack), OPEN otherwise — only when the #879 switch is on.
- `ev` OPEN in the morning window when `departure` is set, the #892 switch is on, and the forecast
  refills the pack; otherwise it inherits today's assist rule.

### 4.2 `coordinator/export_guard.py` — the limit at the meter (new, mirrors `peak_guard.py`)

Pure. `ExportGuard.update(now, verdict, grid_export_w)` → `ExportCommand(intent, watts, reason)`:
- **Engage** after the CLOSED verdict has held `ENGAGE_HOLD_S` (hysteresis on the way in).
- **Release** after OPEN has held `RELEASE_HOLD_S` (hysteresis on the way out); a spot curve crossing
  zero every slot must not flap the inverter.
- **Last, not first:** engagement is a cap at *zero export*, issued only when export is actually
  measured (> `EXPORT_EPS_W`) after the sinks had their cycle — the surplus controller and the battery
  ran first this cycle with the same verdict.
- Publishes `state ∈ {idle, holding, engaged, releasing, refused}` and the refusal reason.

### 4.3 Intents, adapters, seam (existing shape)

- `BatteryIntent.LIMIT_EXPORT` / `RELEASE_EXPORT` added beside `LIMIT_DISCHARGE` — `actuate_battery`
  dispatches them like every other intent; **observer mode records a WOULD and calls nothing**.
- `BatteryControlAdapter.command_limit_export(watts)` / `command_release_export()` — base raises
  `NotImplementedError` → the guard reports `refused: no export control on this brand`.
- **Huawei**: `huawei_solar.set_zero_power_grid_connection(device_id)` to engage,
  `huawei_solar.reset_maximum_feed_grid_power(device_id)` to release — the integration's own restore,
  so there is no prior to capture. Read-back `sensor.*_active_power_control`.
  **Refuse** while that sensor reports `DI Active Scheduling` unless `export_guard_override_external`
  is on: an operator's mode is not SEM's to replace.
- **Deye**: the #827 work-mode select — capture prior option, select the zero-export option, restore
  the prior on release, through the same snapshot store #827 built.
- **Generic**: a writable `number` export limit (`export_limit_entity` when its domain is `number`):
  capture prior, write 0, restore prior. A `sensor`-domain match is observable only → `refused`.
- The write goes through `_write_and_verify`-style read-back where a read-back exists.

### 4.4 Hand-back — the #908 / #936 / #949 rule

An engaged guard is inventoried in `cleanup.py` like the pacer's hold (`sem.export_guard.{entry_id}`).
On unload, disable and removal the release runs **first**, before observer mode flips, exactly where
`pending_pacing_release` is read today. Restart adoption: a clean start with a held export limit and
no record releases it (Huawei `reset_…` is idempotent; Deye/generic restore the snapshot).

### 4.5 The probe must not fight the guard

#743's curtailment probe detects an inverter *someone else* is limiting. While the guard is `engaged`,
SEM is that someone: `_curtailment_grant_w` returns 0.0 and the probe is held in `idle`, so it never
"harvests" energy the guard is deliberately clipping. Pinned by test.

### 4.6 Consumers

- `surplus_controller`: `grid_export == CLOSED` lowers the threshold bars (min-surplus gates, buffer
  floors) exactly as the #871 plan's `AVOID_EXPORT` did — safety gates untouched: peak guard, reserve
  SOC, stop-war logic.
- `decide_battery`: `house` verdict → `LIMIT_DISCHARGE` in HELD (clamp to zero house-cover),
  NORMAL in OPEN; `ev` verdict → the morning assist. Precedence keeps FORCE_CHARGE first.
- `charge_pacing`: second landing time = next CLOSED window start (#926); the smaller of the two caps
  wins; refusals unchanged.
- `today_plan`: rows `export_closed` / `export_reopens` / `house_hold` / `ev_morning_window` from the
  verdicts' `until`, translated in all 16 languages (#963's lesson: the parity test runs on plan keys).

### 4.7 Surface (all settings in the GUI — rule, 20.08)

Config tab, beside the peak limit: **Export guard** switch (default OFF), **Override external
scheduling** switch (default OFF, Huawei), **Hold-in / hold-out** numbers (defaults 120 s / 300 s).
Battery intelligence: **House as a sink** switch (#879, default OFF), **Morning EV window** switch +
hours number (#892, default OFF / 2 h). Every switch is in `PERSISTED_FLAG_DEFAULTS`. The Control tab
tile shows the export guard state (`sem-grid-card.js`, beside the peak tile) and the two #871
diagnostics (kWh and cost exported while negative).

## 5. Data flow (one cycle)

```
tariff level, forecast, clock, permissions
        │
        ▼
sink_verdicts()  ──────────────► FleetCycleState.sink_verdicts (one seam, AST-linted)
        │                               │
        ├─► surplus_controller (loads, EV)      absorb first
        ├─► decide_battery (house, EV morning)   spend/hold stored energy
        ├─► charge_pacing (headroom)             fill side
        ├─► today_plan (rows)                    what the user sees
        └─► ExportGuard.update(measured export) ─► LIMIT_EXPORT / RELEASE_EXPORT
                                                    │
                                              actuate_battery (observer cuts here)
                                                    │
                                              brand adapter (Huawei / Deye / generic)
```

## 6. Error handling — "I could not ask" is not "no"

- Unreadable export price → verdict OPEN, guard idle, sinks normal; the plan row says `price unknown`.
- Adapter raises / read-back disagrees → guard state `refused` with the reason, a Repair after
  three consecutive refusals (class 86 rules: only bad time buys a verdict), never a silent idle.
- Guard exception → loud `warning` naming that the export cap is NOT applied, like the peak guard's.
- Observer mode → every write becomes a WOULD; `.175` proof reads `would_decisions` + `withheld_commands`.

## 7. Testing

- Pure units: `sink_verdicts` (every level × switch × unknown), `ExportGuard` hysteresis (engage /
  release holds, flap sequence, refused, last-not-first ordering), each adapter verb (service payloads,
  snapshot/restore, refusal under external scheduling).
- Structural: AST guard that `sink_verdicts` has one call site (the fleet state builder); that no
  consumer reads `export_rate` for a decision; that every new switch is persisted; plan-key parity in
  16 languages; the probe idle while engaged.
- Scenario rig (`test_873_cycle_executes` shape): a negative hour with a car, a battery and a load —
  the sinks absorb, export stays above zero only until the guard engages, the guard releases with
  hysteresis when the level turns, unload hands the inverter back.
- Mutation: the guard's `blind`/unknown paths and the hold timers.
- **Live on .175, observer ON, once**: hold the feed-in entity negative for a compressed day
  (`sem-sim-compress.sh`), confirm WOULD `set_zero_power_grid_connection` on the real Huawei, the
  pacer's second landing time, the plan rows, the probe idle, and release on the way out.

## 8. Stages — build whole, test once

1. **Measure** — #871 step 0 (two diagnostics).
2. **Verdicts + guard + adapters** — `sink_verdicts`, `ExportGuard`, intents, Huawei / Deye / generic
   verbs, hand-back, probe hold, surface. The guarantee.
3. **Sinks** — #871 steps 1–2 (unclamp, absorb), #926 headroom, #879 house, #892 morning EV, plan rows.
4. **#956** — the roster learns service-shaped roles; the brand table becomes a crawled role.

Stages 1–3 ship together on this branch, default-OFF (gate 4: complete and inert), proven once on
.175, merged on approval with `SEM_FEAT_OK`. Stage 4 (#956) is its own branch on the same role
shape — planned in full as Tasks 18–24 of the same plan, so the arc's plan is the complete arc.

## 9. Not this arc

- #743's read side must keep working for an inverter someone else limits.
- Amber's sign carve-out (#523) survives untouched.
- #880 variable loads, #812 Messkonzept 8 — adjacent, judged out (#921, 13.09).
- #899's accounting is settled first if it changes what "the EV took it" means.

## Post-implementation findings (17.09.2026, added after the live proofs)

Recorded here so the design and what the hardware said stay on one page; the
evidence is in `~/claude-jobs/challenge-feature-921-grid-not-always-a-sink.md`.

1. **The export dispatch never wrote** until 17.09 21:00. The battery
   pipeline builds its own `FleetContext` and hands that to the dispatch; the
   arc had threaded `export_command` / `export_guard_enabled` through
   `build_view`'s context only. The observer surface could not show the
   difference (the standing re-publish names the same service). Fixed:
   both producers read `_cycle_fleet_state`; rows carry `standing`; a
   cycle-level pin and an AST sibling pin (bug classes 93, 94).
2. **Huawei, measured on the reference SUN2000:** `Unlimited` (mode 0) is
   refused — "no cap" is `Limited to 100 %` (`set_maximum_feed_grid_power_percent
   100`); the mode readback is three-state and the cut refuses on
   `unavailable`; the prior is captured once and kept across a release
   (the readback echoes a write for 8–15 min); the repeat check sits above
   the mode check (bug classes 95, 96).
3. **Every write blinds the writing host's modbus session for ~60 s**, and
   the config readback can sit `unavailable` for minutes after a restart;
   `homeassistant.update_entity` on the mode sensor forces a read.
4. **Proven twice, end to end, on .175 with observer off** (synthetic
   grid + price, real inverter): cut → register `Zero Power`, release →
   `Limited to 100 %`. Car and battery untouched.
5. **Still open:** the manual split-grid reader inverts the sign under the
   Huawei platform (the sim used the import slot); .175's readback stall after
   restarts; PROD runs the 16.09 tip without these fixes (guard off, inert).
