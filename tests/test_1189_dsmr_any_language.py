"""#1189 — a DSMR smart meter's power is found in every language.

The Dutch/Belgian smart meter (HA core ``dsmr``) publishes grid import and
export power as two sensors. When the Energy Dashboard's grid entry has
counters and no power sensor, SEM looks for that pair. It matched the
ENGLISH entity_id words only (``power_consumption`` / ``power_production``),
and DSMR's names follow the user's language: ``sensor.electricity_meter_
energieverbruik`` in Dutch, ``…_stromverbrauch`` in German. Nothing matched,
and the grid read 0 W.

DSMR gives the pair the keys ``current_electricity_usage`` and
``current_electricity_delivery`` — as translation_key and as the unique_id's
last segment — and those do not change with the language. The lexicon had no
rule for them, so the roster carried no DSMR vocabulary and the declared tier
(asked first) could not answer. Bug class 10, the split-pair half.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
from unittest.mock import MagicMock, patch

import pytest

from custom_components.solar_energy_management.consts import (
    integration_roster as roster_mod,
)
from custom_components.solar_energy_management.coordinator import sensor_reader as sr_mod
from custom_components.solar_energy_management.coordinator.sensor_reader import SensorReader

ROOT = pathlib.Path(__file__).resolve().parent.parent

_SERIAL = "4530303331303033343236"   # DSMR: unique_id = f"{serial}_{key}"
_METER = "dsmr_electricity_meter"     # the device of power AND counters

# The names HA gives DSMR's two power sensors, per language (HA 2025.1).
# ``test_the_names_are_has_own`` checks the premise against HA's own files.
_NAMES = {
    "en": ("Power consumption", "Power production"),
    "nl": ("Energieverbruik", "Energieproductie"),
    "de": ("Stromverbrauch", "Stromerzeugung"),
}


def _slug(name: str) -> str:
    return name.lower().replace(" ", "_")


def _state(value, unit="W", device_class=None, state_class="measurement"):
    s = MagicMock()
    s.state = str(value)
    s.attributes = {"unit_of_measurement": unit}
    if device_class:
        s.attributes["device_class"] = device_class
    if state_class:
        s.attributes["state_class"] = state_class
    return s


def _ed(import_energy, export_energy):
    ed = MagicMock()
    ed.solar_power = None
    ed.solar_power_list = []
    ed.grid_import_power = None
    ed.grid_power_list = []
    ed.grid_power_from = None
    ed.grid_power_to = None
    ed.grid_import_energy = import_energy
    ed.grid_export_energy = export_energy
    ed.grid_import_energy_list = [import_energy]
    ed.grid_export_energy_list = [export_energy]
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


def _entry(entity_id, translation_key, key=None):
    e = MagicMock()
    e.entity_id = entity_id
    e.platform = "dsmr"
    e.translation_key = translation_key
    e.unique_id = f"{_SERIAL}_{key or translation_key}"
    e.device_id = _METER
    e.disabled_by = None
    return e


class _Meter:
    """A DSMR meter as HA sets it up in one language: two power sensors in
    kW (DSMR's own unit) and the tariff-1 counters, all on one device."""

    def __init__(self, monkeypatch, lang, import_kw, export_kw):
        use, prod = (f"sensor.electricity_meter_{_slug(n)}" for n in _NAMES[lang])
        self.imp, self.exp = use, prod
        counter_in = f"{use}_tariff_1"
        counter_out = f"{prod}_tariff_1"
        self.states = {
            use: _state(import_kw, "kW", device_class="power"),
            prod: _state(export_kw, "kW", device_class="power"),
            counter_in: _state(1234.5, "kWh", device_class="energy",
                               state_class="total_increasing"),
            counter_out: _state(678.9, "kWh", device_class="energy",
                                state_class="total_increasing"),
        }
        self.registry = MagicMock()
        self.registry.entities = {e.entity_id: e for e in (
            _entry(use, "current_electricity_usage"),
            _entry(prod, "current_electricity_delivery"),
            _entry(counter_in, "electricity_used_tariff",
                   key="electricity_used_tariff_1"),
            _entry(counter_out, "electricity_delivered_tariff",
                   key="electricity_delivered_tariff_1"),
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
        self.reader._energy_dashboard_config = _ed(counter_in, counter_out)
        self.reader._get_device_for_entity = lambda eid: (
            _METER if eid in self.states else None)

    def read(self):
        with patch.object(sr_mod.er, "async_get", return_value=self.registry), \
                patch.object(sr_mod, "_ri", MagicMock()), \
                patch.object(sr_mod, "is_sem_entity", lambda *a, **k: False):
            return self.reader.read_power()

    @property
    def picks(self):
        d = self.reader._split_grid_discovery
        return d["import"], d["export"], d["confidence"]


@pytest.mark.parametrize("lang", sorted(_NAMES))
class TestTheMeterIsFoundInEveryLanguage:
    def test_import_reads_as_import(self, monkeypatch, lang):
        meter = _Meter(monkeypatch, lang, import_kw=1.2, export_kw=0.0)
        power = meter.read()
        assert meter.picks == (meter.imp, meter.exp, "declared")
        assert power.grid_power == pytest.approx(-1200.0)

    def test_export_reads_as_export(self, monkeypatch, lang):
        meter = _Meter(monkeypatch, lang, import_kw=0.0, export_kw=2.5)
        power = meter.read()
        assert meter.picks == (meter.imp, meter.exp, "declared")
        assert power.grid_power == pytest.approx(2500.0)


class TestTheKeyIsWhatFindsIt:
    """Without DSMR's vocabulary in the roster, the Dutch and German meter
    is not found at all — the control that makes the tests above mean
    something. English is still found by its words, as before."""

    def _without_dsmr(self, monkeypatch):
        vocab = {d: v for d, v in roster_mod.ROLE_VOCAB.items() if d != "dsmr"}
        monkeypatch.setattr(roster_mod, "ROLE_VOCAB", vocab)

    @pytest.mark.parametrize("lang", ["nl", "de"])
    def test_a_translated_meter_is_lost_without_the_key(self, monkeypatch, lang):
        self._without_dsmr(monkeypatch)
        meter = _Meter(monkeypatch, lang, import_kw=1.2, export_kw=0.0)
        meter.read()
        assert meter.picks[:2] == (None, None)

    def test_english_was_found_by_its_words(self, monkeypatch):
        self._without_dsmr(monkeypatch)
        meter = _Meter(monkeypatch, "en", import_kw=1.2, export_kw=0.0)
        meter.read()
        assert meter.picks == (meter.imp, meter.exp, "same-device")


def _crawler():
    spec = importlib.util.spec_from_file_location(
        "crawler_1189", ROOT / "scripts" / "crawl_integration_roster.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestTheRosterCarriesThePair:
    def test_the_shipped_roster_declares_both_halves(self):
        vocab = roster_mod.ROLE_VOCAB["dsmr"]
        assert vocab["grid_import_power"]["keys"] == ("current_electricity_usage",)
        assert vocab["grid_export_power"]["keys"] == ("current_electricity_delivery",)
        assert set(vocab) == {"grid_import_power", "grid_export_power"}

    def test_the_lexicon_takes_the_pair_and_nothing_beside_it(self):
        """DSMR's sensor keys as HA core declares them. The per-phase powers,
        the tariff counters and the Belgian average demand must not join the
        pair: a phase is a third of the meter, a counter is kWh."""
        crawler = _crawler()
        keys = (
            "current_electricity_usage", "current_electricity_delivery",
            "instantaneous_active_power_l1_positive",
            "instantaneous_active_power_l1_negative",
            "electricity_used_tariff_1", "electricity_delivered_tariff_1",
            "electricity_imported_total", "electricity_exported_total",
            "current_average_demand", "maximum_demand_current_month",
            "electricity_active_tariff", "gas_meter_reading",
        )
        vocab = {"sensor": {k: {"options": ()} for k in keys}}
        roles = crawler.roles_from_vocabulary(vocab, crawler._lexicon(), "energy")
        assert {r: v["keys"] for r, v in roles.items()} == {
            "grid_import_power": ("current_electricity_usage",),
            "grid_export_power": ("current_electricity_delivery",),
        }

    def test_the_names_are_has_own(self):
        """The premise, read from the installed core integration: the keys
        are DSMR's own, and its Dutch and German names hold none of the
        English words the name search looks for. Not the exact wording — a
        translator may change that, and SEM does not care."""
        import homeassistant
        base = pathlib.Path(homeassistant.__file__).parent / "components" / "dsmr"
        strings = json.loads((base / "strings.json").read_text())["entity"]["sensor"]
        assert {"current_electricity_usage",
                "current_electricity_delivery"} <= set(strings)
        words = sr_mod.IMPORT_PATTERNS + sr_mod.EXPORT_PATTERNS
        for lang in ("nl", "de"):
            tr = json.loads((base / "translations" / f"{lang}.json").read_text())
            sensors = tr["entity"]["sensor"]
            for key in ("current_electricity_usage", "current_electricity_delivery"):
                slug = _slug(sensors[key]["name"])
                assert not any(w in slug for w in words), (lang, slug)
        for use, prod in (_NAMES["nl"], _NAMES["de"]):
            assert not any(w in _slug(n) for w in words for n in (use, prod))
        assert any(w in _slug(_NAMES["en"][0]) for w in sr_mod.IMPORT_PATTERNS)

class TestADeadRowIsNotAMeter:
    """(review) The declared tier walked every registry row. A DSMR entry
    that is disabled, or that HA only restored because its integration did
    not set it up, ended the search — and the meter that works was never
    looked for."""

    def _with_working_meter(self, monkeypatch):
        # The Dutch DSMR rows are dead; an English-named pair on the
        # counters' own device is the meter that works.
        meter = _Meter(monkeypatch, "nl", import_kw=1.2, export_kw=0.0)
        meter.states["sensor.p1_power_consumption"] = _state(
            800, device_class="power")
        meter.states["sensor.p1_power_production"] = _state(
            0, device_class="power")
        return meter

    def test_a_disabled_dsmr_row_is_skipped(self, monkeypatch):
        meter = self._with_working_meter(monkeypatch)
        for eid in (meter.imp, meter.exp):
            meter.registry.entities[eid].disabled_by = "config_entry"
            del meter.states[eid]          # a disabled entity has no state
        power = meter.read()
        assert meter.picks == ("sensor.p1_power_consumption",
                               "sensor.p1_power_production", "same-device")
        assert power.grid_power == pytest.approx(-800.0)

    def test_a_restored_dsmr_row_is_skipped(self, monkeypatch):
        meter = self._with_working_meter(monkeypatch)
        for eid in (meter.imp, meter.exp):
            meter.states[eid] = _state("unavailable", "kW", device_class="power")
            meter.states[eid].attributes["restored"] = True
        power = meter.read()
        assert meter.picks[:2] == ("sensor.p1_power_consumption",
                                   "sensor.p1_power_production")
        assert power.grid_power == pytest.approx(-800.0)

    def test_a_row_with_no_state_yet_still_counts(self, monkeypatch):
        """Cold start: the meter has not loaded, the registry answers."""
        meter = _Meter(monkeypatch, "de", import_kw=0.0, export_kw=0.0)
        for eid in (meter.imp, meter.exp):
            del meter.states[eid]
        with patch.object(sr_mod.er, "async_get", return_value=meter.registry):
            imp, exp, on_device = meter.reader._declared_split_grid_power(
                meter.reader._energy_dashboard_config)
        assert (imp, exp, on_device) == (meter.imp, meter.exp, True)


class TestADeclaredPairReplacesAGuess:
    """(review) A name guess held before the meter appeared was kept: the
    adoption gate upgraded only on a same-device name match, and a Dutch
    meter never gives one."""

    def test_the_gate_takes_a_declared_pair(self, monkeypatch):
        meter = _Meter(monkeypatch, "nl", import_kw=0.0, export_kw=0.0)
        meter.states["sensor.heat_pump_power_consumption"] = _state(
            400, device_class="power")
        disc = {"import": "sensor.heat_pump_power_consumption",
                "export": None, "confidence": "any-device"}
        assert meter.reader._should_adopt_split_grid_picks(disc, "declared")
        assert not meter.reader._should_adopt_split_grid_picks(
            disc, "declared-elsewhere")


class TestTheCardKnowsTheGridSensorsInUse:
    """(review) The Config card hides a proposal for an entity SEM already
    reads. The grid's two sensors were not on that list, so a DSMR meter
    SEM reads got two "Use" buttons — and one click on half the pair sets
    export to 0 W."""

    def _used(self, ed, disc):
        from types import SimpleNamespace

        from custom_components.solar_energy_management.coordinator.coordinator import (
            SEMCoordinator,
        )
        coord = SEMCoordinator.__new__(SEMCoordinator)
        coord.config = {}
        coord._sensor_reader = SimpleNamespace(
            config=SimpleNamespace(), _energy_dashboard_config=ed,
            _split_grid_discovery=disc)
        return coord._configured_entity_ids()

    def test_the_pair_sem_found_and_trusts(self):
        from types import SimpleNamespace
        disc = {"import": "sensor.electricity_meter_energieverbruik",
                "export": "sensor.electricity_meter_energieproductie",
                "confidence": "declared"}
        used = self._used(SimpleNamespace(), disc)
        assert {disc["import"], disc["export"]} <= used

    def test_an_unproven_guess_is_not_in_use(self):
        from types import SimpleNamespace
        disc = {"import": "sensor.heat_pump_power_consumption",
                "export": None, "confidence": "any-device"}
        assert disc["import"] not in self._used(SimpleNamespace(), disc)

    def test_the_dashboards_two_sensor_pair_and_list(self):
        from types import SimpleNamespace
        ed = SimpleNamespace(grid_power_from="sensor.grid_in",
                             grid_power_to="sensor.grid_out",
                             grid_power_list=["sensor.meter_a", "sensor.meter_b"])
        used = self._used(ed, None)
        assert {"sensor.grid_in", "sensor.grid_out",
                "sensor.meter_a", "sensor.meter_b"} <= used
