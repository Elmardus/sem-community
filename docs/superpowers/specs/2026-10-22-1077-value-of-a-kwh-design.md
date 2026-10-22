# What a kWh is worth — design (#1077, #1090, #812, #1084)

Branch `feature/value-of-a-kwh`. One idea: SEM decides on what a kWh is
**worth**, not on one price. Bought, sold, and per device. And it says
**why** it acted.

## Today (22.10.2026)

- Import price: tariff provider, `get_current_import_rate`
  (`tariff/tariff_provider.py:1870`); dynamic adds `grid_import_surcharge`.
  No VAT, no markup, no energy tax.
- Export value: `get_current_export_rate` (:1929) reads the feed-in entity.
  But the planner and day ledger use `_configured_export_rate()`
  (`coordinator.py:8133`), the fixed number only — the feed-in entity never
  reaches a plan. That is a bug on its own: file it, fix it in stage B.
- Negative price: `battery_charge_scheduler.py:371` compares the IMPORT
  price only.
- Per device: energy per device exists, cost per device does not. Every
  load gates on the house price level.
- Why: `observer_decisions` (swept each cycle), `TraceCollector(30)` in
  memory, free-text reasons. No logbook. Strategy sensor = primary charger.

## Stages (build all, test once, every stage off by default)

### A. Decision log (#1084) — recording only, may ship on

- One `DecisionRecord`: time, device, old → new, `reason_code`, short text,
  inputs (import value, export value, price level, SOC, surplus, target).
- Written at ONE place: where a command leaves for a device (charger,
  battery, load). Not at every decision — only when the command changes.
- Kept: ring buffer of 200 in memory, in diagnostics; restored never.
- Shown: HA logbook (event `solar_energy_management_decision` + a
  `logbook.py` describer), and `last_decision` as an attribute on the
  existing per-charger / per-device state sensor. No new sensor per device.
- Strategy per charger: attribute on `charger_<id>_charging_state`.

### B. Import and export value (#1077)

- A value layer in the tariff provider:
  `import_value = (spot + markup + energy_tax) × (1 + VAT) + surcharge`,
  `export_value = spot − feed_in_fee`.
- Feed-in fee: `per_kwh` | `tiered` (kWh exported this year → €/kWh) |
  `fixed_monthly` (marginal 0). Tiered tracks exported kWh year to date.
- A preset "NL from 2027" fills the fields; other countries leave them 0,
  and the values equal today's prices exactly (replay must show no change).
- Every decision that reads an export rate reads `export_value_at(t)`:
  planner, day ledger, arbitrage, forecast sell, sink verdicts, costs.
- Sensors (daily + yearly): self-consumption value, export value, feed-in
  fees. Near a tier edge, storing beats exporting.

### C. Negative export value (#1090)

- When `export_value < 0`, exporting costs money. The right answer is to
  **stop exporting** (battery and loads take the sun), not to buy: on a
  Dutch tariff the import price is still positive.
- So: the grid sink closes on `export_value < threshold` (default 0) —
  `sink_verdicts` already does this for a negative feed-in. Grid charging
  still needs `import_value < 0`.
- This differs from the issue's wording; say so on the issue.

### D. A tariff per device (#812, Messkonzept 8)

- A device may name its own tariff: a fixed rate or a price entity, and its
  own meter. Its grid kWh are priced at that tariff; its cheap/expensive
  gate reads that tariff.
- First user: the heat pump. Same shape for any load.
- Per-device cost appears in the decision log inputs and a device cost
  sensor.

### E. "Why?" card — mockup first

- Timeline per device from the log. Mockup to Guido before code
  (`docs/UI_PATTERNS.md`, EV card as reference).

## Settings

All new fields in the GUI config card AND the options flow. The option
ratchet (`tests/option_surface_baseline.json`, 200) grows by about 8:
energy tax, VAT, markup, fee model, fee value or tiers, export threshold,
device tariff. That needs a decision; the preset keeps it to one choice
for most users.

## Proof

- Unit tests per stage; pipeline test per tariff mode.
- Replay against PROD days: with no value fields set, zero change.
- A Dutch day built from Rien's numbers (€0.15–0.59 import, negative
  export): battery takes the sun, nothing bought, nothing exported at a
  loss.
- .175: settings, logbook entries, sensors, the card.

## Decided (22.10)

1. #1090: stop exporting when the export value is below the threshold;
   grid charging still needs `import_value < 0`. Said so on the issue.
2. Logbook: changes only — start, stop, mode, current step ≥ 1 A.
3. Option count grows by about 8; baseline updated with the reason.
