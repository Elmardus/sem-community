"""#1223 — DSMR Reader's grid power pair is found.

DSMR Reader (HA core ``dsmr_reader``, 1380 installs) publishes grid import
and export power as two sensors in kW, on no device:
``sensor.dsmr_reading_electricity_currently_delivered`` and ``…_returned``.
When the Energy Dashboard's grid entry has counters and no power sensor, SEM
looks for that pair. The entity ids hold none of the English words the name
search knows, so only the declared tier could find it — and the roster had no
DSMR Reader row at all.

Two things in the crawler kept it out, and fixing either alone changes
nothing:

* ``classify_kind`` counted the appliance marker ``ram_`` (computer memory)
  as a SUBSTRING, and DSMR Reader's ``telegram_timestamp`` holds those four
  letters. One appliance hit against no house hit made the meter an
  appliance, and an appliance contributes no roles. Bug class 67: a marker
  ending in ``_`` names a whole segment, so it must begin one.
* DSMR Reader declares no word on the energy-anchor list, so even as a
  non-appliance it read as "other". Its own pair
  (``current_power_usage`` / ``current_power_return``) — both halves of a
  split grid meter, named by the lexicon's own read rules — is now an anchor.
"""
from __future__ import annotations

import ast
import importlib.util
import json
import pathlib
from unittest.mock import MagicMock, patch

import pytest

from custom_components.solar_energy_management.consts import (
    integration_roster as roster_mod,
)
from custom_components.solar_energy_management.consts import role_lexicon as lex
from custom_components.solar_energy_management.coordinator import sensor_reader as sr_mod
from custom_components.solar_energy_management.coordinator.sensor_reader import SensorReader

ROOT = pathlib.Path(__file__).resolve().parent.parent

_ENTRY_ID = "01JDSMRREADER0000000000000"
_IMP = "sensor.dsmr_reading_electricity_currently_delivered"
_EXP = "sensor.dsmr_reading_electricity_currently_returned"
_IMP_L1 = "sensor.dsmr_reading_phase_currently_delivered_l1"
_EXP_L1 = "sensor.dsmr_reading_phase_currently_returned_l1"
_IN = "sensor.dsmr_reading_electricity_delivered_1"
_OUT = "sensor.dsmr_reading_electricity_returned_1"


def _crawler():
    spec = importlib.util.spec_from_file_location(
        "crawler_1223", ROOT / "scripts" / "crawl_integration_roster.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _dsmr_reader_keys() -> list:
    """Every sensor key DSMR Reader declares, read from the installed core
    integration — the vocabulary the crawler mines from its repository."""
    import homeassistant
    base = pathlib.Path(homeassistant.__file__).parent / "components" / "dsmr_reader"
    return sorted(json.loads((base / "strings.json").read_text())["entity"]["sensor"])


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def monotonic(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


def _state(value, unit="kW", device_class="power", state_class="measurement"):
    s = MagicMock()
    s.state = str(value)
    s.attributes = {"unit_of_measurement": unit}
    if device_class:
        s.attributes["device_class"] = device_class
    if state_class:
        s.attributes["state_class"] = state_class
    return s


def _ed():
    ed = MagicMock()
    ed.solar_power = None
    ed.solar_power_list = []
    ed.grid_import_power = None
    ed.grid_power_list = []
    ed.grid_power_from = None
    ed.grid_power_to = None
    ed.grid_import_energy = _IN
    ed.grid_export_energy = _OUT
    ed.grid_import_energy_list = [_IN]
    ed.grid_export_energy_list = [_OUT]
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


def _entry(entity_id, translation_key):
    """A registry row as DSMR Reader writes it: unique_id is the config
    entry id and the MQTT topic slug, and there is no device."""
    e = MagicMock()
    e.entity_id = entity_id
    e.platform = "dsmr_reader"
    e.translation_key = translation_key
    e.unique_id = f"{_ENTRY_ID}-{entity_id.split('.', 1)[1]}"
    e.device_id = None
    e.disabled_by = None
    return e


class _Reader:
    """DSMR Reader as HA sets it up: the two whole-meter powers and the
    phase-L1 powers in kW, the tariff-1 counters in kWh."""

    def __init__(self, monkeypatch, import_kw, export_kw):
        self.clock = _Clock()
        monkeypatch.setattr(sr_mod, "time", self.clock)
        self.states = {
            _IMP: _state(import_kw),
            _EXP: _state(export_kw),
            _IMP_L1: _state(import_kw / 3),
            _EXP_L1: _state(export_kw / 3),
            _IN: _state(1234.5, "kWh", "energy", "total_increasing"),
            _OUT: _state(678.9, "kWh", "energy", "total_increasing"),
        }
        self.registry = MagicMock()
        self.registry.entities = {e.entity_id: e for e in (
            _entry(_IMP, "current_power_usage"),
            _entry(_EXP, "current_power_return"),
            _entry(_IMP_L1, "current_power_usage_l1"),
            _entry(_EXP_L1, "current_power_return_l1"),
            _entry(_IN, "low_tariff_usage"),
            _entry(_OUT, "low_tariff_returned"),
        )}
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
        self.reader._energy_dashboard_config = _ed()
        self.reader._get_device_for_entity = lambda eid: None

    def set(self, eid, value, unit="kWh"):
        dc = "energy" if unit == "kWh" else "power"
        sc = "total_increasing" if unit == "kWh" else "measurement"
        self.states[eid] = _state(value, unit, dc, sc)

    def read(self):
        with patch.object(sr_mod.er, "async_get", return_value=self.registry), \
                patch.object(sr_mod, "_ri", MagicMock()), \
                patch.object(sr_mod, "is_sem_entity", lambda *a, **k: False):
            return self.reader.read_power()

    @property
    def picks(self):
        d = self.reader._split_grid_discovery
        return d["import"], d["export"], d["confidence"]

    def run_minutes(self, minutes, import_kw=0.0, export_kw=0.0):
        """The meter's counters move with its own power, minute by minute."""
        kin = float(self.states[_IN].state)
        kout = float(self.states[_OUT].state)
        power = None
        for _ in range(minutes):
            self.clock.advance(60)
            kin += import_kw / 60.0
            kout += export_kw / 60.0
            self.set(_IN, round(kin, 6))
            self.set(_OUT, round(kout, 6))
            power = self.read()
        return power


class TestTheReaderIsFound:
    def test_the_pair_is_declared(self, monkeypatch):
        meter = _Reader(monkeypatch, import_kw=1.2, export_kw=0.0)
        meter.read()
        # No device on either side: the declared pair proves itself against
        # the counters, like any pick SEM cannot tie to the meter's device.
        assert meter.picks == (_IMP, _EXP, "declared-elsewhere")

    def test_a_phase_row_is_not_the_meter(self, monkeypatch):
        """With only the phase rows left, nothing is declared: a phase is a
        third of the meter, and its key is not the pair's."""
        meter = _Reader(monkeypatch, import_kw=1.2, export_kw=0.0)
        for eid in (_IMP, _EXP):
            del meter.registry.entities[eid]
            del meter.states[eid]
        meter.read()
        assert meter.picks[:2] == (None, None)

    def test_import_reads_as_import_once_the_counters_agree(self, monkeypatch):
        meter = _Reader(monkeypatch, import_kw=1.2, export_kw=0.0)
        meter.read()
        power = meter.run_minutes(16, import_kw=1.2)
        assert meter.reader._split_grid_proof["verdict"] is True
        assert power.grid_power == pytest.approx(-1200.0)

    def test_export_reads_as_export_once_the_counters_agree(self, monkeypatch):
        meter = _Reader(monkeypatch, import_kw=0.0, export_kw=2.5)
        meter.read()
        power = meter.run_minutes(16, export_kw=2.5)
        assert meter.reader._split_grid_proof["verdict"] is True
        assert power.grid_power == pytest.approx(2500.0)


class TestTheRosterRowIsWhatFindsIt:
    """The control: without DSMR Reader's row the same install finds
    nothing, and the grid reads 0 W. That is the reported bug."""

    def test_the_meter_is_lost_without_the_row(self, monkeypatch):
        vocab = {d: v for d, v in roster_mod.ROLE_VOCAB.items()
                 if d != "dsmr_reader"}
        monkeypatch.setattr(roster_mod, "ROLE_VOCAB", vocab)
        meter = _Reader(monkeypatch, import_kw=1.2, export_kw=0.0)
        power = meter.run_minutes(16, import_kw=1.2)
        assert meter.picks[:2] == (None, None)
        assert power.grid_power == 0.0

    def test_the_entity_ids_hold_no_word_the_name_search_knows(self):
        """The premise: only the declared tier can answer for this meter."""
        words = sr_mod.IMPORT_PATTERNS + sr_mod.EXPORT_PATTERNS
        for eid in (_IMP, _EXP):
            assert not any(w in eid for w in words), eid

    def test_the_shipped_roster_declares_both_halves(self):
        vocab = roster_mod.ROLE_VOCAB["dsmr_reader"]
        assert {r: v["keys"] for r, v in vocab.items()} == {
            "grid_import_power": ("current_power_usage",),
            "grid_export_power": ("current_power_return",),
        }
        assert roster_mod.ROSTER["dsmr_reader"]["kind"] == "energy"


class TestTheCrawlerReadsAMeter:
    """What the crawler concludes from DSMR Reader's own vocabulary, read
    from the installed HA core integration."""

    def _vocab(self):
        return {"sensor": {k: {"options": ()} for k in _dsmr_reader_keys()}}

    def test_the_premise_telegram_holds_ram(self):
        keys = _dsmr_reader_keys()
        assert "telegram_timestamp" in keys
        assert {"current_power_usage", "current_power_return"} <= set(keys)

    def test_it_is_energy_and_proposes_the_pair(self):
        crawler = _crawler()
        lexicon = crawler._lexicon()
        vocab = self._vocab()
        kind = crawler.classify_kind(vocab, lexicon, domain="dsmr_reader")
        assert kind == "energy"
        roles = crawler.roles_from_vocabulary(vocab, lexicon, kind)
        assert {r: v["keys"] for r, v in roles.items()} == {
            "grid_import_power": ("current_power_usage",),
            "grid_export_power": ("current_power_return",),
        }

    def test_each_half_of_the_fix_is_needed(self):
        """Without the word rule it is an appliance; without the pair
        anchor it is "other". Both give no roles."""
        crawler = _crawler()
        lexicon = crawler._lexicon()
        vocab = self._vocab()
        blob = " ".join(_dsmr_reader_keys())
        assert "ram_" in blob and not lexicon.says(blob, "ram_")
        assert not lexicon.says_any(blob, lexicon.ENERGY_ANCHORS)
        assert crawler._declares_a_whole_pair(vocab, lexicon)

    def test_half_a_pair_anchors_nothing(self):
        crawler = _crawler()
        lexicon = crawler._lexicon()
        half = {"sensor": {"current_power_usage": {}, "telegram_timestamp": {}}}
        assert not crawler._declares_a_whole_pair(half, lexicon)
        assert crawler.classify_kind(half, lexicon, domain="x") == "other"

    def test_computer_memory_is_still_an_appliance(self):
        crawler = _crawler()
        lexicon = crawler._lexicon()
        vocab = {"sensor": {"ram_usage": {}, "cpu_temperature": {},
                            "battery_level": {}}}
        assert crawler.classify_kind(vocab, lexicon, domain="x") == "appliance"


class TestTheCardOffersTheWholePair:
    """(review) The card's pair needs both halves on one unit. DSMR Reader's
    rows have no device, so each half was offered as "half of a split pair"
    with no button. A row with no device belongs to its config entry."""

    def _rows(self, entry_in=_ENTRY_ID, entry_out=_ENTRY_ID):
        imp, exp = _entry(_IMP, "current_power_usage"), _entry(_EXP, "current_power_return")
        imp.config_entry_id, exp.config_entry_id = entry_in, entry_out
        return [imp, exp]

    def _propose(self, rows):
        from custom_components.solar_energy_management import hardware_detection as hd
        states = {_IMP: _state(1.2), _EXP: _state(0.0)}
        return hd.propose_roles_from_roster(rows, "dsmr_reader",
                                            state_of=states.get)

    def test_one_click_saves_both_halves(self):
        out = self._propose(self._rows())
        assert out["grid_import_power"]["action"] == "set_option"
        assert out["grid_import_power"]["pair"] == {
            _IMP: {"grid_export_power_entity": [_EXP]}}
        assert out["grid_export_power"]["pair"] == {
            _EXP: {"grid_import_power_entity": [_IMP]}}

    def test_two_entries_are_two_meters(self):
        out = self._propose(self._rows(entry_out="01JOTHERENTRY"))
        assert out["grid_import_power"]["action"] == "pair_incomplete"
        assert out["grid_export_power"]["action"] == "pair_incomplete"

    def test_no_device_and_no_entry_is_no_pair(self):
        out = self._propose(self._rows(entry_in=None, entry_out=None))
        assert out["grid_import_power"]["action"] == "pair_incomplete"

    def test_the_install_form_fills_both(self):
        from custom_components.solar_energy_management import hardware_detection as hd
        reg = MagicMock()
        reg.entities = {e.entity_id: e for e in self._rows()}
        out = hd.propose_energy_sources(registry=reg)
        assert out["grid_import_power_entity"]["entity"] == _IMP
        assert out["grid_export_power_entity"]["entity"] == _EXP


class TestAMarkerEndingInAnUnderscoreIsAWord:
    """Class 67 in the kind markers. Real keys from the mined corpus."""

    @pytest.mark.parametrize("words,marker", [
        ("telegram_timestamp", "ram_"),              # DSMR Reader
        ("switching_program_enabled", "ram_"),       # Stiebel Eltron ISG
        ("configuration_program_version_number", "ram_"),  # EDL21 meter
        ("v2c_cloud.program_timer", "ram_"),         # V2C charger service
        ("gp1_pump_speed", "p1_"),                   # Qvantum heat pump
        ("wireless_clients", "ess_"),
    ])
    def test_letters_inside_a_word_are_not_the_word(self, words, marker):
        assert marker in words
        assert not lex.says(words, marker)

    @pytest.mark.parametrize("words,marker", [
        ("ram_usage", "ram_"),
        ("ps_ram_free", "ram_"),
        ("unraid.ram_used", "ram_"),
        ("p1_power", "p1_"),
        ("system_ess_mode", "ess_"),
        ("vin_number", "vin_"),
        ("grid_power", "grid_"),
    ])
    def test_the_word_itself_still_counts(self, words, marker):
        assert lex.says(words, marker)

    @pytest.mark.parametrize("words,marker", [
        ("airpurifier_mode", "purifier"),
        ("vebus_pvinverter_disable", "inverter"),
        ("current_job_mode_dehumidifier", "humidifier"),
        ("battery_max_soc_limit", "_soc"),
    ])
    def test_a_stem_still_finds_a_compound(self, words, marker):
        """Only a marker that ENDS in ``_`` names a whole segment. A stem is
        written to reach compounds, and a leading ``_`` already anchors."""
        assert lex.says(words, marker)

    def test_every_segment_marker_obeys_the_rule(self):
        """An oracle over every marker list the lexicon ships: glued to a
        word before it, a ``word_`` marker never hits; at a key's start, or
        after ``_`` or ``.``, it always does."""
        checked = 0
        for name in _marker_lists():
            for m in getattr(lex, name):
                if not (m.endswith("_") and m[:1].isalnum()):
                    continue
                checked += 1
                assert not lex.says(f"tele{m}x", m), (name, m)
                assert lex.says(f"{m}x", m), (name, m)
                assert lex.says(f"a_{m}x", m), (name, m)
                assert lex.says(f"dom.{m}x", m), (name, m)
        assert checked >= 15


def _marker_lists() -> list:
    names = [n for n in dir(lex)
             if n.isupper() and (n.endswith("_MARKERS") or n.endswith("ANCHORS")
                                 or n.endswith("_CONTEXT"))
             and isinstance(getattr(lex, n), tuple)]
    assert {"APPLIANCE_MARKERS", "HOUSE_MARKERS", "ENERGY_ANCHORS",
            "BATTERY_CONTEXT"} <= set(names)
    return names


_HELPERS = {"says", "says_any", "count_said"}


def _bare_marker_uses(source: str, lists: set) -> list:
    """Every use of a marker list that is not an argument of a lexicon
    helper. A loop, an alias, ``sorted(...)`` or ``A + B`` each hand the
    list to a bare ``m in words`` somewhere; ``says`` is the only test."""
    tree = ast.parse(source)
    parent = {c: p for p in ast.walk(tree) for c in ast.iter_child_nodes(p)}
    found = []
    for node in ast.walk(tree):
        name = None
        if isinstance(node, ast.Attribute) and node.attr in lists:
            name = node.attr
        elif isinstance(node, ast.Name) and node.id in lists:
            name = node.id
        elif (isinstance(node, ast.Call) and getattr(node.func, "id", "") == "getattr"
                and len(node.args) >= 2 and isinstance(node.args[1], ast.Constant)
                and node.args[1].value in lists):
            name = node.args[1].value
        if name is None:
            continue
        up = parent.get(node)
        func = getattr(up, "func", None) if isinstance(up, ast.Call) else None
        helper = getattr(func, "attr", None) or getattr(func, "id", None)
        if helper in _HELPERS and node in up.args:
            continue
        found.append((name, node.lineno))
    return found


class TestNoMarkerIsTestedBySubstring:
    """The guard: outside the lexicon, a marker list is only ever handed to
    ``says_any`` / ``count_said``. The bug was a bare substring count in
    ``classify_kind`` (``sum(1 for m in markers if m in blob)``)."""

    def test_the_lint_sees_the_old_shapes(self):
        lists = set(_marker_lists())
        bad = ("any(m in blob for m in lexicon.APPLIANCE_MARKERS)\n"
               "any(m in w for m in getattr(lexicon, 'HOUSE_MARKERS', ()))\n"
               "_hits(lexicon.VEHICLE_MARKERS)\n"
               "words = lex.CHARGER_MARKERS + lex.GENERATOR_MARKERS\n"
               "for m in sorted(ENERGY_ANCHORS):\n    pass\n"
               "lex.says_any(w, lex.BATTERY_CONTEXT)\n"
               "lex.count_said(w, lex.STRONG_ENERGY_ANCHORS)\n")
        assert sorted(n for n, _ in _bare_marker_uses(bad, lists)) == [
            "APPLIANCE_MARKERS", "CHARGER_MARKERS", "ENERGY_ANCHORS",
            "GENERATOR_MARKERS", "HOUSE_MARKERS", "VEHICLE_MARKERS"]

    def test_no_file_uses_a_marker_list_bare(self):
        lists = set(_marker_lists())
        offenders, seen = [], 0
        for path in sorted(ROOT.rglob("*.py")):
            rel = path.relative_to(ROOT)
            if rel.parts[0] in ("tests", ".git") or rel == pathlib.Path(
                    "consts/role_lexicon.py"):
                continue
            src = path.read_text()
            seen += sum(src.count(n) for n in lists)
            for name, line in _bare_marker_uses(src, lists):
                offenders.append(f"{rel}:{line} uses {name} bare")
        assert not offenders, offenders
        assert seen >= 10      # the crawler and hardware_detection do use them
