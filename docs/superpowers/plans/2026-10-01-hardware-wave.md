# Next hardware wave — teach the crawler the roles, not the brands

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Zaptec, the Tesla car integrations, myenergi Zappi, Easee phase
switching and EG4/Luxpower are found and wired by SEM's generic crawler,
because the crawler learns the ROLES these integrations expose. No brand
gets its own code path.

**The rule (01.10.2026):** "this is the whole idea of SEM — we do
not cover hardware integration, we connect to integrations with the crawler
if possible." SEM drives a device only through the entities and services
its Home Assistant integration exposes, and finds them by role. A brand is
proven by loading its **real integration** in the crawler rig (Task 0). It
is never a code path: no `_wire_<brand>`, no `_discover_<brand>`, no
`if platform == "<brand>"`.

**Architecture:** Everything lands in the generic layer that already
exists:

- `consts/role_lexicon.py`: `ROLE_RULES` (entity keys by platform),
  `SERVICE_ROLE_RULES` (#956, services as capabilities), `CHARGER_MARKERS`,
  `VEHICLE_ROLE_RULES`, `role_for()`.
- `hardware_detection.py`: the roster and near-miss path
  (`charger_from_near_miss`, `_discover_from_hints`), and
  `wire_current_entity` as the one wiring producer.
- `consts/integration_roster.py` + `scripts/crawl_integration_roster.py`:
  the census of each integration's declared keys and services.

A new role is one rule entry plus the behaviour that reads it. Every rule
is tested against every brand fixture, so a rule that wins one brand and
breaks another fails the suite.

**Tech stack:** Python 3.14 / HA 2026.8 (CI floor 3.13 / HA 2026.2),
`~/bin/semtest` with its own `SEM_ROOT`, worktree `/home/sem/sem-hw`,
branch `feature/hardware-wave` off develop.

**Ship path:** build every task, suite green, ruflo told to refute, one
deploy to .175 with the compressed sun sim (observer ON), a soak on
PROD, merge to develop only on approval.

---

## Why these integrations (Home Assistant analytics, 01.10.2026)

| integration | installs | today | report |
|---|---|---|---|
| `tesla_wall_connector` + `tesla_fleet` / `tessie` / `teslemetry` / BLE | 6 730 + 5 076 / 1 855 / 1 112 | not wired | none, needs an owner |
| `myenergi` (Zappi) | 3 810 | not found | none, needs an owner |
| `easee` | 3 656 | found, no phase switching | #1015 |
| `zaptec` | 2 112 | some installs report "no role matched" | #1032 |
| `eg4_web_monitor` / `lxp_modbus` | 257 / 370 | requested | #810 |

Left out: #812 is a tariff, not hardware. #849 is upstream. #917 and #808
wait for their reporters. #886 Juicebox is closed.

---

## What each integration exposes today, and the role it needs

Read from each upstream source on 01.10.2026; the rig (Task 0) re-proves
it from the integration's real output.

**Zaptec** ([custom-components/zaptec](https://github.com/custom-components/zaptec), master)
- Installation device: number `available_current` (built only when the
  account may set it, `manager.py create_entities_from_descriptions`),
  number `three_to_one_phase_switch_current`.
- Charger device: buttons `resume_charging` / `stop_charging_final`,
  switch `charger_operation_mode`, numbers `charger_min_current` /
  `charger_max_current` (settings).
- Services: `limit_current`, `stop_charging`, `resume_charging`.
- Today: `available_current` already matches `ev_current_control`, but it
  sits on the **installation** device while plug and power sit on the
  **charger** device, so no single device carries a whole charger. #1032's
  installation device had no `available_current` at all and was reported
  as a near miss.
- Needs: **R1** companion devices, **R2** start/stop buttons,
  **R6** service `limit_current`.

**Tesla car integrations** ([tesla_fleet](https://github.com/home-assistant/core/tree/dev/homeassistant/components/tesla_fleet); Teslemetry and Tessie publish the same keys)
- Number `charge_state_charge_current_request` (A, max from
  `charge_state_charge_current_request_max`); switch with unique id
  `charge_state_user_charge_enable_request`.
- Wall Connector ([tesla_wall_connector](https://github.com/home-assistant/core/tree/dev/homeassistant/components/tesla_wall_connector)):
  read-only, `vehicle_connected`, `contactor_closed`, power.
- Today: no `ev_current_control` rule matches
  `charge_state_charge_current_request` (it has no `ev`/`charger` word),
  and nothing pairs a read-only charger with a car that has the controls.
- Needs: **R3** car-side charging amps + charge switch, **R4** read-only
  charger paired with a controlling car.

**myenergi Zappi** ([CJNE/ha-myenergi](https://github.com/CJNE/ha-myenergi), main)
- Charge-mode select with pymyenergi `CHARGE_MODES` (Fast, Eco, Eco+,
  Stopped), phase select with options `1` / `3` / `auto`, boost services.
- Today: **not findable by key** — upstream gives BOTH selects
  `translation_key="phase_setting"` (an upstream naming slip in
  `select.py`), and SEM's `ev_charge_mode` rule reads keys only.
- Needs: **R5** a select read by its **options**, not only its key.

**Easee** ([nordicopen/easee_hass](https://github.com/nordicopen/easee_hass), master)
- Service `set_circuit_dynamic_limit` with `current_p1`, `current_p2`,
  `current_p3`, `time_to_live`; service `set_charger_phase_mode`.
- Today: the current is wired; nothing names a phase control.
- Needs: **R6** per-phase limit service as a phase control.

**EG4 / Luxpower** (`joyfulhouse/eg4_web_monitor`, `ant0nkr/luxpower-ha-integration`)
- The settled reading from #810: `ac_charge_soc_limit` is the charge
  target; `system_charge_soc_limit`, `soc_cutoff`, `ac_couple_*_soc`,
  `quick_charge` are excluded. Read both sources at build time for the
  AC-charge switch and the charge-rate number.
- Needs: **R7** an AC-charge enable switch as a force-charge control, with
  the #810 exclusions as `not` words. Build only if the #810 box is
  commissioned.

---

## The roles (generic, each covers several integrations)

| role | rule | brands covered | also covers |
|---|---|---|---|
| **R1 companion device** | a device with no charger role whose sibling device (same config entry) carries the charger's plug/power is merged into that charger, never reported as a near miss | Zaptec installation + charger | Easee circuit/equalizer, openWB site, Wallbox installation, any "site + box" integration |
| **R2 start/stop pair** | a `button` (or `switch`) pair with start/resume and stop words on a charger device becomes `ev_start_stop_entity` | Zaptec `resume_charging`/`stop_charging_final` | Wattpilot `frc` buttons (#804), Ohme, Peblar, any box with buttons only |
| **R3 car-side charge control** | on a vehicle device (`VEHICLE_MARKERS`): a number in A with `charge` + `current`/`amps` and a request/limit word is a current control; a switch with `charge` + enable/request is its start/stop | Tesla Fleet, Teslemetry, Tessie, Tesla BLE | Polestar, MG SAIC, VW/Skoda, Kia where they expose amps |
| **R4 read-only charger + controlling car** | a charger device with plug/power roles but no control, on an install with exactly one vehicle device that has R3 controls, is wired to that car's controls; two cars → ask | Tesla Wall Connector + Tesla car | any dumb charger with a smart car |
| **R5 select by options** | a select whose options contain a stop value (`stopped`, `stop`, `off`, `disabled`) and a charge value (`fast`, `eco`, `pv`, `solar`, `now`, `boost`) on a charger device is `ev_charge_mode`; options `1`/`3`(/`auto`) on a charger device are a phase control | Zappi charge mode and phase | go-e, openWB, Ohme, any select with a mistyped key |
| **R6 service roles** | `SERVICE_ROLE_RULES` gains: a service with `current_p1`/`p2`/`p3` fields → phase-capable current control (1-phase = p1 only); `limit_current` / `set_*_limit` with an amps field → current control on a charger device | Easee `set_circuit_dynamic_limit`, Zaptec `limit_current` | Easee equalizer, any per-phase circuit limit |
| **R7 AC-charge force-charge** | a switch with `ac_charge` + enable words on a battery/inverter device is `battery_force_charge`, paired with a `charge_soc_limit` number; the #810 exclusions are `not` words | EG4, Luxpower | Growatt, Deye/Solarman, Sofar where they use AC-charge words |

The control a role names is driven through Home Assistant's normal
service for that domain (`number.set_value`, `button.press`,
`switch.turn_on`, `select.select_option`, the integration's service).
Nothing else.

---

## Task 0: The crawler rig — the real integrations, not hand-written fixtures

Rule, 01.10.2026: "we easily can test these integrations, even install
them and test and use the crawler." Each target integration is loaded for
real in a Home Assistant test instance, and SEM's crawler runs on the
entities and services it actually creates. A pin bump that renames a key
fails the rig.

**How each integration loads (checked 01.10.2026):**

| integration | source | how the rig loads it | data |
|---|---|---|---|
| `tesla_fleet`, `teslemetry`, `tessie` | HA core (in our pinned `homeassistant` 2026.8.2 wheel) | set up the core component with its API client patched | HA core's own fixtures at tag `2026.8.2`: `tests/components/<x>/fixtures/*.json` (`vehicle_data.json`, `products.json`, `live_status.json`, `site_info.json`; Tessie `vehicles.json`, `online.json`, `asleep.json`) and `snapshots/*.ambr` |
| `tesla_wall_connector` | HA core | set up with `tesla_wall_connector` patched the way core's `conftest.py` does (`get_default_version_data`, `Vitals`, `Lifetime`, `WifiStatus`) | built from that conftest; core ships no JSON fixture for it |
| `easee` | HACS `nordicopen/easee_hass` | vendored at a pinned commit; its client `pyeasee` patched | `nordicopen/pyeasee` `tests/fixtures/` (`chargers.json`, `charger-state.json`, `site.json`, `site-state.json`, `sites.json`) |
| `myenergi` | HACS `CJNE/ha-myenergi` | vendored at a pinned commit | its own `tests/fixtures/` (`client.json`, `history_zappi.json`, …) and its `conftest.py` |
| `lxp_modbus` (Luxpower) | HACS `ant0nkr/luxpower-ha-integration` | vendored at a pinned commit | its own `tests/` (`test_data.py`, coordinator tests) |
| `eg4_web_monitor` | HACS `joyfulhouse/eg4_web_monitor` | vendored at a pinned commit | its own `tests/fixtures/` (`dongle_emulation`) and `conftest.py` |
| `zaptec` | HACS `custom-components/zaptec` | vendored at a pinned commit | **no offline data**: its `tests/zaptec/` call the real cloud with a user account. The rig gets a minimal mock built from the payload keys its `zaptec/` API module reads (installation with and without `AvailableCurrent`, one charger). Smallest honest mock, named as such. |

**Not feasible, and why:**

- **No live crawler run on .175 for any of these.** Tesla Fleet,
  Teslemetry, Tessie, Easee, Zaptec and myenergi are cloud integrations
  that need an owner's account. Tesla Wall Connector and Luxpower Modbus
  are local but need the box on the LAN. The live check is the owner's,
  after the beta.
- **phacc 0.13.356 ships no component test fixtures** (only `common.py`,
  `diagnostics`, `recorder`), so the core fixture files are vendored at the
  HA tag with the source link.

**Files:**
- Create: `tests/integrations_rig/` (test-only; `tests/` is already
  excluded from the release zip by `scripts/build_release_zip.sh`
  `EXCLUDE_DIRS` and checked by `scripts/verify_release_zip.py`
  `MUST_NOT_SHIP`)
  - `vendor/<domain>/` — each HACS integration at its pinned commit, with
    `PIN` (repo, commit, date, licence)
  - `core_fixtures/<domain>/` — HA core fixture files at tag `2026.8.2`,
    with `SOURCE` (URL + tag)
  - `rig.py` — loads one integration into a `hass` test instance
    (custom_components path for vendored ones), patches its client with the
    data above, runs setup, then runs SEM's crawler
    (`hardware_detection` detection + roster near-miss + service census)
    and returns the roles it found
  - `PINS.md` — one table of every pin, and how to bump one
- Create: `tests/test_integrations_rig.py` — every integration loads and
  creates entities; the crawler's output per integration is recorded as a
  snapshot (`syrupy`, the format HA core uses)
- Modify: `tests/requirements_test.txt` only if a client library is needed
  (`pyeasee`, `pymyenergi`, `zaptec` deps) — pinned, test-only

- [ ] **Step 1: baseline** — suite and ruff green on `/tmp/ha-hwfull`
  (`~/bin/semtest tests -rf > ~/claude-jobs/suite-hw-baseline.txt`).
- [ ] **Step 2:** load the three core Tesla car integrations and the Wall
  Connector; assert entities exist with the upstream keys
  (`charge_state_charge_current_request`,
  `charge_state_user_charge_enable_request`, `vehicle_connected`,
  `contactor_closed`).
- [ ] **Step 3:** vendor and load Easee, myenergi, Luxpower, EG4; same
  assertion on their keys and services.
- [ ] **Step 4:** Zaptec with the minimal mock, two shapes (with and
  without `available_current`).
- [ ] **Step 5:** record the crawler snapshot per integration; confirm the
  vendored code is absent from `scripts/build_release_zip.sh` output.
- [ ] **Step 6:** commit `test(#1032): the crawler rig loads the real integrations`.

If an integration does not load on our pinned HA (an import that needs a
newer core), record that in `PINS.md`, pick the newest commit that does,
and say which keys may differ from the latest upstream.

## Task 1: The roles, one rule each, test first (on the rig)

For each role R1–R7, in that order:

**Files:**
- Modify: `consts/role_lexicon.py` (the rule) and the generic reader in `hardware_detection.py` (R1 companion merge, R4 pairing, R5 options read)
- Create: `tests/test_hw_wave_roles.py`

- [ ] **Step 1:** failing test on the rig — the role is found in the
  REAL output of its integrations (Task 0) AND not found in any other
  rig integration or the existing real-install fixtures
  (`tests/test_923_real_installs.py`).
- [ ] **Step 2:** run, see it fail.
- [ ] **Step 3:** add the rule; no brand name in any code.
- [ ] **Step 4:** run the role tests, `tests/test_814_hardware_matrix.py`,
  `tests/test_915_*`, `tests/test_956_*`; all pass.
- [ ] **Step 5:** commit `feat(#1032): role <name>`.

A lint test guards the rule itself: `tests/test_hw_wave_no_brand_branches.py`
fails if `hardware_detection.py` gains a new `_wire_<x>` / `_discover_<x>`
or a string compare on a platform name.

## Task 2: Pipeline tests through the roles (on the rig)

**Files:** Modify `tests/test_split_grid_integration.py`.

- [ ] One pipeline test per rig integration: detection → config proposal →
  coordinator writes the control the role named, observer OFF in the test.
- [ ] Commit `test(#1032): pipeline tests for the role-found chargers`.

## Task 3: Matrix and docs

- [ ] `consts/hardware_matrix.py`: rows for Tesla (Wall Connector + car),
  Zappi; Zaptec, Easee and EG4 evidence; status `implemented` until an owner
  confirms. `control` names the ROLE, not a brand path.
- [ ] README brand list; `docs/` page "How SEM finds your hardware" gains
  the seven roles in plain words.
- [ ] CHANGELOG `[Unreleased]`, one line (≤ 25 words).

## Task 4: Prove it, then soak

- [ ] Full suite on `/tmp/ha-hwfull`; ruff; CI on a draft PR.
- [ ] ruflo reviewer, told to refute: "no role finds a control on a device
  that is not that kind of device, on any fixture or real install."
- [ ] .175: `SRC=/home/sem/sem-hw ~/bin/sem-deploy-175.sh`, then
  `timeout 1200 ~/bin/sem-sim-compress.sh 10.10.20.175 18.0 30`. Expect no
  SEM errors, no new entity, no new proposal for .175's own devices.
- [ ] PROD soak on approval (soak marker `BRANCH:` + `LADDER: hands-off`).
- [ ] Short posts on #1032, #1015, #810, and one Discussions call for a
  Tesla owner and a Zappi owner.

---

## Cleanup candidates (brand paths that could fold into roles — do NOT remove in this wave)

| existing path | role that could replace it |
|---|---|
| `_wire_wattpilot` + `_discover_wattpilot` (#804, `frc` buttons) | R2 start/stop pair |
| `_discover_zaptec` + the `zaptec_*` patterns in the Zaptec profile (`hardware_detection.py:180`) — upstream sets `has_entity_name`, so ids follow the device name and these patterns never match a current install | R1 + `ev_current_control` + R6 |
| `_discover_easee` | R1 + R6 |
| `_discover_goecharger`, `_discover_goecharger_mqtt`, `_discover_openwb` (charge-mode selects) | R5 |
| `_discover_ohme`, `_discover_peblar`, `_discover_blue_current`, `_discover_openevse`, `_discover_v2c`, `_discover_alfen`, `_discover_heidelberg`, `_discover_chargepoint`, `_discover_nrgkick` | `ev_current_control` + R2 (check each against its fixture first) |
| `_discover_keba` (service-driven) | `SERVICE_ROLE_RULES` (#956) — KEBA's quirks (failsafe, quota stop) stay as data rows, not a path |
| `_discover_wallbox`, `_discover_wallbox_mqtt`, `_discover_mqtt_brands`, `_discover_abl_emh1`, `_discover_garo`, `_discover_juicebox` | `ev_current_control` + R1, transport platforms stay excluded |
| `_discover_ocpp` | its own protocol words; keep until a fixture proves R2/R6 cover it |

Each removal is its own later branch: load the integration in the rig,
prove the role finds the same entities, then delete the path.

---

## Open decision

**Order:** Tesla and Zappi have the most installs but no owner to prove
them yet; Zaptec, Easee and EG4 have reporters waiting. Build the roles in
the order R1–R7 (reporters' brands land first: R1/R2/R6 cover Zaptec and
Easee), or start with R3/R4 for Tesla?
