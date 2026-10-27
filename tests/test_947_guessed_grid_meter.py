"""#947 — a name-matched grid meter must earn its place.

The reporter's `sensor.sem_grid_import_power` was a square wave: ~852 W for
one coordinator cycle, then 0, while every real meter in the house read ~0.
The Energy Dashboard gave SEM grid COUNTERS and no grid power entity, so SEM
matched meters by a SUBSTRING of the entity id — and `power_production`, the
DSMR/P1 feed-in meter's name, matched `sensor.power_production_now`, a SOLAR
FORECAST entity, which SEM then read as the grid export meter.

#911 answered that instance by excluding forecasts. A blacklist over every
sensor in a house cannot be completed: the import patterns reach
`power_consumption`, which this repo's own startup-race fixture uses as a
HEAT PUMP. The root cause is that a name-matched pick was stored
indistinguishably from a configured one and trusted forever.

So the pick is a CANDIDATE until it agrees with the grid energy counters —
the thing SEM already has and did not guess at.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from custom_components.solar_energy_management.coordinator import sensor_reader as sr_mod
from custom_components.solar_energy_management.coordinator.sensor_reader import SensorReader


class _Clock:
    """A monotonic clock the test drives, in place of the module's ``time``."""

    def __init__(self):
        self.t = 1000.0

    def monotonic(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


def _state(value, unit="W", device_class=None, state_class="measurement"):
    s = MagicMock()
    s.state = str(value)
    s.attributes = {"unit_of_measurement": unit}
    if device_class:
        s.attributes["device_class"] = device_class
    if state_class:
        s.attributes["state_class"] = state_class
    return s


def _ed(import_energy="sensor.grid_import_total", export_energy="sensor.grid_export_total"):
    ed = MagicMock()
    ed.solar_power = None
    ed.solar_power_list = []
    ed.grid_import_power = None
    ed.grid_power_list = []
    ed.grid_power_from = None
    ed.grid_power_to = None
    ed.grid_import_energy = import_energy
    ed.grid_export_energy = export_energy
    ed.grid_import_energy_list = [import_energy] if import_energy else []
    ed.grid_export_energy_list = [export_energy] if export_energy else []
    ed.battery_power = None
    ed.battery_power_list = []
    ed.battery_power_from = None
    ed.battery_power_to = None
    ed.battery_power_inverted = False
    ed.battery_power_pairs = []
    ed.battery_soc = None
    ed.battery_soc_list = []
    ed.battery_charge_energy = None
    ed.battery_discharge_energy = None
    ed.ev_power = None
    ed.has_solar = False
    ed.has_grid = True
    ed.has_battery = False
    ed.has_ev = False
    return ed


class _Rig:
    """A reader over a mutable state table, with the clock in the test's hand."""

    def __init__(self, monkeypatch, states, ed=None, device_of=None):
        self.states = states
        self.clock = _Clock()
        monkeypatch.setattr(sr_mod, "time", self.clock)
        hass = MagicMock()
        hass.states.get = lambda eid: self.states.get(eid)

        def _all(domain=None):
            out = []
            for eid, st in self.states.items():
                s = MagicMock()
                s.entity_id = eid
                s.state = st.state
                s.attributes = st.attributes
                if domain is None or eid.startswith(f"{domain}."):
                    out.append(s)
            return out

        hass.states.async_all = _all
        self.reader = SensorReader(hass, {"update_interval": 10})
        self.reader._sign_vote_warmup = 0
        self.reader._energy_dashboard_config = ed or _ed()
        if device_of is not None:
            self.reader._get_device_for_entity = lambda eid: device_of.get(eid)
        else:
            self.reader._get_device_for_entity = lambda eid: None

    def set(self, eid, value, **kw):
        self.states[eid] = _state(value, **kw)

    def read(self):
        with patch.object(sr_mod, "_ri", MagicMock()):
            return self.reader.read_power()

    @property
    def repairs(self):
        return self._repairs

    def read_watching_repairs(self):
        self._repairs = MagicMock()
        with patch.object(sr_mod, "_ri", self._repairs):
            return self.reader.read_power()


# The reporter's shape: a guessed import meter and a solar-forecast entity
# matched as the export meter, against counters that never move.
def _guessed_states(import_w=852.0, export_w=0.0, import_kwh=100.0, export_kwh=50.0):
    return {
        "sensor.heat_pump_power_consumption": _state(import_w, device_class="power"),
        "sensor.power_production": _state(export_w, device_class="power"),
        "sensor.grid_import_total": _state(import_kwh, "kWh"),
        "sensor.grid_export_total": _state(export_kwh, "kWh"),
    }


class TestAGuessDoesNotSteer:
    def test_the_first_read_reports_no_grid_power(self, monkeypatch):
        """852 W of claimed import, and SEM reports 0 — because nothing has
        corroborated the sensor it just matched by name."""
        rig = _Rig(monkeypatch, _guessed_states())
        power = rig.read()
        assert rig.reader._split_grid_discovery["import"] == "sensor.heat_pump_power_consumption"
        assert rig.reader._split_grid_discovery["confidence"] == "any-device"
        assert power.grid_power == 0.0
        power.calculate_derived()
        assert power.grid_import_power == 0.0

    def test_the_reporters_phantom_is_rejected_by_the_counters(self, monkeypatch):
        """15 minutes of 852 W against counters that never move. That is not
        a meter, and SEM says so instead of reporting the number."""
        rig = _Rig(monkeypatch, _guessed_states())
        rig.read()
        for _ in range(15):
            rig.clock.advance(60)
            power = rig.read()
        assert rig.reader._split_grid_proof["verdict"] is False
        assert power.grid_power == 0.0

    def test_the_rejection_raises_a_repair_naming_both_sensors(self, monkeypatch):
        rig = _Rig(monkeypatch, _guessed_states())
        rig.read()
        for _ in range(14):
            rig.clock.advance(60)
            rig.read()
        rig.clock.advance(60)
        rig.read_watching_repairs()
        call = rig.repairs.raise_split_grid_rejected.call_args
        assert call is not None, "a disproved guess must surface in the UI"
        assert call.kwargs["import_entity"] == "sensor.heat_pump_power_consumption"


class TestAGuessThatTracksTheCountersIsPromoted:
    def test_it_steers_once_the_energy_agrees(self, monkeypatch):
        """A real meter: 2 kW of import for 15 min moves the import counter
        by 0.5 kWh. SEM adopts it and reads the grid again."""
        rig = _Rig(monkeypatch, _guessed_states(import_w=2000.0))
        rig.read()
        kwh = 100.0
        for _ in range(15):
            rig.clock.advance(60)
            kwh += 2000.0 / 1000.0 / 60.0          # 2 kW for one minute
            rig.set("sensor.grid_import_total", round(kwh, 6), unit="kWh")
            power = rig.read()
        assert rig.reader._split_grid_proof["verdict"] is True
        assert power.grid_power == -2000.0
        power.calculate_derived()
        assert power.grid_import_power == 2000.0

    def test_the_verdict_survives_into_the_next_window(self, monkeypatch):
        """A proven meter must not blink off for a cycle when the window
        rolls over — the window is bookkeeping, not the verdict."""
        rig = _Rig(monkeypatch, _guessed_states(import_w=2000.0))
        rig.read()
        kwh = 100.0
        for _ in range(16):
            rig.clock.advance(60)
            kwh += 2000.0 / 1000.0 / 60.0
            rig.set("sensor.grid_import_total", round(kwh, 6), unit="kWh")
            power = rig.read()
        assert power.grid_power == -2000.0, "the fresh window un-steered a proven meter"


class TestEvidenceBeatsNames:
    def test_a_same_device_pair_never_waits(self, monkeypatch):
        """Growatt, DSMR, E3DC, Senec — the brands these patterns were
        written for — have their power sensors on the counter's own device.
        That is evidence, not a guess, and it steers on the first read."""
        states = _guessed_states(import_w=852.0)
        device_of = {
            "sensor.grid_import_total": "meter",
            "sensor.heat_pump_power_consumption": "meter",
            "sensor.power_production": "meter",
        }
        rig = _Rig(monkeypatch, states, device_of=device_of)
        power = rig.read()
        assert rig.reader._split_grid_discovery["confidence"] == "same-device"
        assert power.grid_power == -852.0


class TestSilenceIsNotAVerdict:
    def test_a_quiet_window_convicts_nobody(self, monkeypatch):
        """No import, no export, counters still: the candidate is unproven,
        NOT disproved. "I could not tell" is its own value (#925)."""
        rig = _Rig(monkeypatch, _guessed_states(import_w=0.0))
        rig.read()
        for _ in range(20):
            rig.clock.advance(60)
            rig.read()
        assert rig.reader._split_grid_proof["verdict"] is None

    def test_a_quiet_window_is_restarted_rather_than_left_open(self, monkeypatch):
        """After two hours of nothing the window re-baselines, so a house
        that wakes up at noon is judged on the afternoon, not on the night."""
        rig = _Rig(monkeypatch, _guessed_states(import_w=0.0))
        rig.read()
        rig.clock.advance(3600)
        rig.read()
        started_before = rig.reader._split_grid_proof["started"]
        rig.clock.advance(3600)
        rig.read()
        assert rig.reader._split_grid_proof["started"] != started_before

    def test_a_counter_reset_does_not_decide(self, monkeypatch):
        """Growatt resets its daily counters at midnight. A negative delta
        means the comparison is garbage, not that the meter is wrong."""
        rig = _Rig(monkeypatch, _guessed_states(import_w=2000.0))
        rig.read()
        for _ in range(14):
            rig.clock.advance(60)
            rig.read()
        rig.set("sensor.grid_import_total", 0.0, unit="kWh")   # midnight reset
        rig.clock.advance(60)
        rig.read()
        assert rig.reader._split_grid_proof["verdict"] is None


class TestTheHalfPairIsNamed:
    def test_an_export_only_discovery_says_which_silence_it_is(self, monkeypatch, caplog):
        """Export-only used to fall through to "no grid power sensor found",
        which is a different fact. SEM still reads 0 — export-minus-zero
        would make a house that imports read as one that never does — but it
        says which of the two silences this is."""
        states = {
            "sensor.power_production": _state(1500, device_class="power"),
            "sensor.grid_import_total": _state(100, "kWh"),
            "sensor.grid_export_total": _state(50, "kWh"),
        }
        rig = _Rig(monkeypatch, states)
        with caplog.at_level("WARNING"):
            power = rig.read()
        assert power.grid_power == 0.0
        assert "export" in caplog.text.lower()


# ── tier 1: what the integrations DECLARE (#947, folded from the roster audit)

class TestDeclaredBeatsGuessed:
    """The #915 roster reads each integration's own repository. A declared
    role is the integration author's semantic label; a substring of an
    entity_id is a guess about a house. The audit that produced this tier
    found 98 grid-meter-shaped declared keys across 46 domains that the
    lexicon did not match — word order being the recurring miss, so Fronius
    (``power_grid``) and Tibber (``power_flow_from_grid``) contributed no
    grid role at all despite ~10k installs each."""

    def _registry(self, entries):
        reg = MagicMock()
        reg.entities = {e.entity_id: e for e in entries}
        return reg

    def _entry(self, entity_id, platform, translation_key=None, unique_id=None,
               device_id=None):
        e = MagicMock()
        e.entity_id = entity_id
        e.platform = platform
        e.translation_key = translation_key
        e.unique_id = unique_id or ""
        e.device_id = device_id
        e.disabled_by = None     # a real registry row's default
        return e

    def test_a_declared_pair_steers_on_the_first_read(self, monkeypatch):
        """Fronius declares power_grid_import / power_grid_export. That is
        evidence, so it needs no corroboration window."""
        states = {
            "sensor.fronius_import": _state(1200, device_class="power"),
            "sensor.fronius_export": _state(0, device_class="power"),
            "sensor.grid_import_total": _state(100, "kWh"),
            "sensor.grid_export_total": _state(50, "kWh"),
        }
        rig = _Rig(monkeypatch, states,
                   device_of={"sensor.grid_import_total": "fronius_inverter",
                              "sensor.fronius_import": "fronius_inverter",
                              "sensor.fronius_export": "fronius_inverter"})
        reg = self._registry([
            self._entry("sensor.fronius_import", "fronius",
                        translation_key="power_grid_import",
                        device_id="fronius_inverter"),
            self._entry("sensor.fronius_export", "fronius",
                        translation_key="power_grid_export",
                        device_id="fronius_inverter"),
        ])
        with patch.object(sr_mod.er, "async_get", return_value=reg):
            power = rig.read()
        assert rig.reader._split_grid_discovery["confidence"] == "declared"
        assert power.grid_power == -1200.0

    def test_a_forecast_can_never_win_the_declared_tier(self, monkeypatch):
        """#947's exact collision. `forecast_solar` declares
        `power_production_next_12hours` and no grid role whatsoever, so on
        this tier the forecast is not a candidate at all — structurally,
        not by a blacklist that has to be kept complete."""
        states = {
            "sensor.power_production_now": _state(3000, device_class="power"),
            "sensor.grid_import_total": _state(100, "kWh"),
            "sensor.grid_export_total": _state(50, "kWh"),
        }
        rig = _Rig(monkeypatch, states)
        reg = self._registry([
            self._entry("sensor.power_production_now", "forecast_solar",
                        translation_key="power_production_now"),
        ])
        with patch.object(sr_mod.er, "async_get", return_value=reg):
            imp, exp, affinity = rig.reader._declared_split_grid_power(
                rig.reader._energy_dashboard_config)
        assert (imp, exp, affinity) == (None, None, False)

    def test_the_lexicon_knows_the_brands_sems_own_patterns_were_written_for(self):
        """The runtime IMPORT/EXPORT patterns name Growatt, Senec, GivEnergy,
        E3DC and DSMR. The roster is what turns those names into evidence, so
        the split-grid brands it CAN answer for must not silently shrink."""
        from custom_components.solar_energy_management.consts import (
            integration_roster as roster,
        )
        vocab = getattr(roster, "ROLE_VOCAB", {})
        pairs = {d for d, v in vocab.items()
                 if "grid_import_power" in v and "grid_export_power" in v}
        assert {"growatt_modbus", "senec", "fronius", "tibber"} <= pairs, (
            f"declared split-grid pairs shrank: {sorted(pairs)}")


# ── what the ruflo review refuted ─────────────────────────────────────

class TestTheReviewFindings:
    def _reg(self, entries):
        reg = MagicMock()
        reg.entities = {e.entity_id: e for e in entries}
        return reg

    def _entry(self, entity_id, platform, translation_key=None, device_id=None):
        e = MagicMock()
        e.entity_id = entity_id
        e.platform = platform
        e.translation_key = translation_key
        e.unique_id = ""
        e.device_id = device_id
        e.disabled_by = None
        return e

    def test_a_declared_meter_on_another_device_still_has_to_prove_itself(self, monkeypatch):
        """REFUTED (a): device affinity was a tie-break, not a requirement, so
        a lone declared candidate was trusted wherever it lived. A Senec
        battery retrofitted behind an existing DSMR meter declares both grid
        halves and measures a DIFFERENT point from the Energy Dashboard's
        counters — and was believed unconditionally."""
        states = {
            "sensor.senec_import": _state(900, device_class="power"),
            "sensor.senec_export": _state(0, device_class="power"),
            "sensor.grid_import_total": _state(100, "kWh"),
            "sensor.grid_export_total": _state(50, "kWh"),
        }
        rig = _Rig(monkeypatch, states,
                   device_of={"sensor.grid_import_total": "dsmr_meter",
                              "sensor.senec_import": "senec_box",
                              "sensor.senec_export": "senec_box"})
        reg = self._reg([
            self._entry("sensor.senec_import", "senec",
                        translation_key="grid_imported_power", device_id="senec_box"),
            self._entry("sensor.senec_export", "senec",
                        translation_key="grid_exported_power", device_id="senec_box"),
        ])
        with patch.object(sr_mod.er, "async_get", return_value=reg):
            power = rig.read()
        assert rig.reader._split_grid_discovery["confidence"] == "declared-elsewhere"
        assert power.grid_power == 0.0, "a declared pick off the grid device must prove itself"

    def test_a_declared_meter_on_the_grid_device_needs_no_window(self, monkeypatch):
        states = {
            "sensor.senec_import": _state(900, device_class="power"),
            "sensor.senec_export": _state(0, device_class="power"),
            "sensor.grid_import_total": _state(100, "kWh"),
            "sensor.grid_export_total": _state(50, "kWh"),
        }
        rig = _Rig(monkeypatch, states,
                   device_of={"sensor.grid_import_total": "senec_box",
                              "sensor.senec_import": "senec_box",
                              "sensor.senec_export": "senec_box"})
        reg = self._reg([
            self._entry("sensor.senec_import", "senec",
                        translation_key="grid_imported_power", device_id="senec_box"),
            self._entry("sensor.senec_export", "senec",
                        translation_key="grid_exported_power", device_id="senec_box"),
        ])
        with patch.object(sr_mod.er, "async_get", return_value=reg):
            power = rig.read()
        assert rig.reader._split_grid_discovery["confidence"] == "declared"
        assert power.grid_power == -900.0

    def test_a_sub_load_that_correlates_is_still_caught(self, monkeypatch):
        """REFUTED (a): a heat pump drawing 2.0 kWh of a 2.3 kWh house import
        passes ANY tolerance loose enough for a real meter's sampling error.
        It is caught the first time it switches OFF while the house keeps
        importing — a real meter cannot read nothing while its own counter
        advances."""
        rig = _Rig(monkeypatch, _guessed_states(import_w=2000.0))
        rig.read()
        kwh = 100.0
        # 10 min of the heat pump running, tracking import closely
        for _ in range(10):
            rig.clock.advance(60)
            kwh += 2000.0 / 1000.0 / 60.0
            rig.set("sensor.grid_import_total", round(kwh, 6), unit="kWh")
            rig.read()
        # the pump stops; the house keeps importing at 1.8 kW
        rig.set("sensor.heat_pump_power_consumption", 0.0, device_class="power")
        for _ in range(6):
            rig.clock.advance(60)
            kwh += 1800.0 / 1000.0 / 60.0
            rig.set("sensor.grid_import_total", round(kwh, 6), unit="kWh")
            power = rig.read()
        assert rig.reader._split_grid_proof["verdict"] is False
        assert power.grid_power == 0.0

    def test_a_wh_counter_is_not_a_thousandfold_disagreement(self, monkeypatch):
        """REFUTED (b): the counters were summed RAW while the power side was
        normalised to watts, so a Wh counter (real hardware, #551) put the two
        sides 1000x apart and failed every window forever — on an install that
        worked before this change."""
        states = _guessed_states(import_w=2000.0)
        states["sensor.grid_import_total"] = _state(100000.0, "Wh")
        states["sensor.grid_export_total"] = _state(50000.0, "Wh")
        rig = _Rig(monkeypatch, states)
        rig.read()
        wh = 100000.0
        for _ in range(15):
            rig.clock.advance(60)
            wh += 2000.0 / 60.0                     # 2 kW for one minute, in Wh
            rig.set("sensor.grid_import_total", round(wh, 4), unit="Wh")
            power = rig.read()
        assert rig.reader._split_grid_proof["verdict"] is True
        assert power.grid_power == -2000.0

    @pytest.mark.parametrize("confidence,expected", [
        ("declared", "split-declared"),
        ("declared-elsewhere", "split-declared-unverified"),
        ("same-device", "split"),
        ("any-device", "split-lowconf"),
    ])
    def test_each_tier_reports_as_itself(self, confidence, expected):
        """REFUTED (c): publish_diag collapsed every non-same-device pick into
        "split-lowconf", so the strongest tier read as the weakest.

        Asserted on what the function RETURNS, not on how it is spelled — a
        source-string check is coupled to the text and not the behaviour
        (#925 / bug class 76), and the ledger of those only shrinks.
        """
        from custom_components.solar_energy_management.coordinator.publish_diag import (
            build_diagnostics,
        )
        reader = MagicMock()
        reader._split_grid_discovery = {"import": "sensor.i", "export": "sensor.e",
                                        "confidence": confidence}
        reader._grid_sign_inverted = False
        reader._manual_grid_mismatch = False
        reader._raw_config = {}
        coord = MagicMock()
        coord._sensor_reader = reader
        # No manual override — that branch short-circuits before the tiers.
        coord.config = {}
        out = build_diagnostics(coord)
        assert out["diag_grid_mode"] == expected


def test_recorder_replay_cannot_convict_a_meter(monkeypatch):
    """(#947 review, open item) While HA replays the recorder, counter states
    arrive in bursts that are not elapsed time. The sign voter already sits
    those cycles out; the corroborator compares MAGNITUDES, so a replayed jump
    against a real-time integral would convict a good meter. Same gate."""
    rig = _Rig(monkeypatch, _guessed_states(import_w=2000.0))
    rig.reader._sign_vote_warmup = 12
    kwh = 100.0
    for _ in range(12):                       # the replay burst
        rig.clock.advance(10)
        kwh += 5.0                            # a whole afternoon per cycle
        rig.set("sensor.grid_import_total", kwh, unit="kWh")
        rig.read()          # read_power decrements the warm-up itself
    assert rig.reader._split_grid_proof["verdict"] is None
    assert rig.reader._split_grid_proof["contradictions"] == 0


def test_a_repair_a_previous_lifetime_left_is_cleared_by_a_proven_read(monkeypatch):
    """(#933) The clear must not depend on the corroborator RUNNING. A pick
    that improves to `declared` across a restart — because the roster learned
    the brand — never corroborates again, so a memo-gated clear would leave
    the old Repair standing forever over a problem that is gone."""
    states = {
        "sensor.growatt_import_from_grid": _state(900, device_class="power"),
        "sensor.growatt_export_to_grid": _state(0, device_class="power"),
        "sensor.grid_import_total": _state(100, "kWh"),
        "sensor.grid_export_total": _state(50, "kWh"),
    }
    rig = _Rig(monkeypatch, states,
               device_of={"sensor.grid_import_total": "growatt",
                          "sensor.growatt_import_from_grid": "growatt",
                          "sensor.growatt_export_to_grid": "growatt"})
    repairs = MagicMock()
    with patch.object(sr_mod, "_ri", repairs):
        rig.reader.read_power()
    assert rig.reader._split_grid_discovery["confidence"] == "same-device"
    assert repairs.clear_split_grid_guessed.called, (
        "a proven read must retire a guess Repair a previous lifetime left")
    assert rig.reader._split_proof_reconciled is True

    # …and only once per lifetime.
    repairs.reset_mock()
    with patch.object(sr_mod, "_ri", repairs):
        rig.reader.read_power()
    assert not repairs.clear_split_grid_guessed.called
