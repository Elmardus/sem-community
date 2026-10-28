"""#1225 — a SlimmeLezer P1 reader's grid power is found.

The SlimmeLezer runs ESPHome's ``dsmr`` component. Its firmware names the
meter's two power sensors "Power Consumed" and "Power Produced", so the
entity_ids end in ``power_consumed`` / ``power_produced`` (kW). When the
Energy Dashboard's grid entry has counters and no power sensor, SEM looks
for the pair by entity_id words, and it knew ``power_consumption`` /
``power_production`` only. Nothing matched, and the grid read 0 W.

The names come from the device's firmware, not from an integration, so the
roster has no key for them (``esphome`` is an opaque platform). The same
words name other things: Sinapsi Alfa's "Power Produced" is its SOLAR
output, Silla Prism has "Power produced by PV", P1 Monitor has "Power
consumed phase L1". So the words are asked on ``esphome`` only, and only as
the whole name the firmware gave the sensor (``original_name``, or the
object id at the end of ESPHome's unique_id).

Swept beside it (bug class 10): on the meter's own device the name search
kept the LAST match, so a phase row (``grid_import_power_l2``) beat the
whole meter it is part of, and was trusted. A whole-meter row now wins, and
a phase row still picked is checked against the counters.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from custom_components.solar_energy_management.coordinator import sensor_reader as sr_mod
from custom_components.solar_energy_management.coordinator.sensor_reader import SensorReader

_READER = "slimmelezer_device"      # power sensors AND counters live here

# The default SlimmeLezer firmware (zuidwijk/slimmelezer-wifi-p1-reader):
# every power row is in kW, every counter in kWh, all on one device.
_SLIMMELEZER_POWER = (
    "power_consumed", "power_produced",
    "power_consumed_phase_1", "power_consumed_phase_2", "power_consumed_phase_3",
    "power_produced_phase_1", "power_produced_phase_2", "power_produced_phase_3",
)


def _state(value, unit="W", device_class="power", state_class="measurement"):
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


class _House:
    """A house whose Energy Dashboard grid entry holds counters only."""

    def __init__(self, counter_in, counter_out, device=_READER,
                 platform="esphome"):
        self.states = {
            counter_in: _state(1234.5, "kWh", "energy", "total_increasing"),
            counter_out: _state(678.9, "kWh", "energy", "total_increasing"),
        }
        self.devices = {counter_in: device, counter_out: device}
        self.platforms = {counter_in: platform, counter_out: platform}
        self.names = {counter_in: None, counter_out: None}
        self.objs = {counter_in: None, counter_out: None}
        self.ed = _ed(counter_in, counter_out)

    def add(self, entity_id, value, unit="W", device=_READER,
            platform="esphome", name=None, obj=None, **kw):
        """``name`` is the firmware's own name (the registry's
        ``original_name``); ESPHome builds the unique_id's end from it.
        ``obj`` gives that end alone, for a row with no ``original_name``."""
        self.states[entity_id] = _state(value, unit, **kw)
        self.devices[entity_id] = device
        self.platforms[entity_id] = platform
        self.names[entity_id] = name
        self.objs[entity_id] = obj

    def _registry(self):
        entries = {}
        for eid in self.states:
            e = MagicMock()
            e.entity_id = eid
            e.platform = self.platforms[eid]
            e.translation_key = None
            name = self.names[eid]
            e.original_name = name
            obj = (self.objs[eid] or name
                   or eid.split(".", 1)[1]).lower().replace(" ", "_")
            e.unique_id = (f"aa:bb:cc:dd:ee:ff-sensor-{obj}"
                           if e.platform == "esphome" else f"serial_{obj}")
            e.device_id = self.devices[eid]
            e.disabled_by = None
            entries[eid] = e
        reg = MagicMock()
        reg.entities = entries
        reg.async_get = lambda eid: entries.get(eid)
        return reg

    def read(self):
        """One read cycle. The reader is kept, so a second read sees what
        the first one adopted."""
        if getattr(self, "reader", None) is None:
            self.reader = self._reader()
        with patch.object(sr_mod.er, "async_get", return_value=self._registry()), \
                patch.object(sr_mod, "_ri", self.ri), \
                patch.object(sr_mod, "is_sem_entity", lambda *a, **k: False):
            power = self.reader.read_power()
        d = self.reader._split_grid_discovery
        return power, (d["import"], d["export"], d["confidence"])

    def _reader(self):
        self.ri = MagicMock()
        hass = MagicMock()
        hass.states.get = lambda eid: self.states.get(eid)

        def _all(domain=None):
            out = []
            for eid, st in self.states.items():
                if domain is None or eid.startswith(f"{domain}."):
                    s = MagicMock()
                    s.entity_id = eid
                    s.state = st.state
                    s.attributes = st.attributes
                    out.append(s)
            return out

        hass.states.async_all = _all
        reader = SensorReader(hass, {"update_interval": 10})
        reader._sign_vote_warmup = 0
        reader._energy_dashboard_config = self.ed
        reader._get_device_for_entity = lambda eid: self.devices.get(eid)
        return reader


def _slimmelezer(import_kw, export_kw, phase_kw=0.4):
    house = _House("sensor.slimmelezer_energy_consumed_tariff_1",
                   "sensor.slimmelezer_energy_produced_tariff_1")
    for name in ("energy_consumed_tariff_2", "energy_produced_tariff_2"):
        house.add(f"sensor.slimmelezer_{name}", 99.0, "kWh",
                  device_class="energy", state_class="total_increasing")
    for obj in _SLIMMELEZER_POWER:
        value = {"power_consumed": import_kw,
                 "power_produced": export_kw}.get(obj, phase_kw)
        house.add(f"sensor.slimmelezer_{obj}", value, "kW",
                  name=obj.replace("_", " ").title())
    return house


def _counters_only():
    return _House("sensor.slimmelezer_energy_consumed_tariff_1",
                  "sensor.slimmelezer_energy_produced_tariff_1")


IMP = "sensor.slimmelezer_power_consumed"
EXP = "sensor.slimmelezer_power_produced"


class TestTheSlimmeLezerIsFound:
    def test_import_reads_as_import(self):
        power, picks = _slimmelezer(import_kw=1.2, export_kw=0.0).read()
        assert picks == (IMP, EXP, "same-device")
        assert power.grid_power == pytest.approx(-1200.0)

    def test_export_reads_as_export(self):
        power, picks = _slimmelezer(import_kw=0.0, export_kw=2.5).read()
        assert picks == (IMP, EXP, "same-device")
        assert power.grid_power == pytest.approx(2500.0)

    def test_a_renamed_twin_is_still_the_meter(self):
        """HA adds ``_2`` when the device is set up again beside an old row."""
        house = _counters_only()
        house.add("sensor.slimmelezer_power_consumed_2", 0.8, "kW",
                  name="Power Consumed")
        house.add("sensor.slimmelezer_power_produced_2", 0.0, "kW",
                  name="Power Produced")
        power, picks = house.read()
        assert picks[:2] == ("sensor.slimmelezer_power_consumed_2",
                             "sensor.slimmelezer_power_produced_2")
        assert power.grid_power == pytest.approx(-800.0)

    def test_a_renamed_entity_id_is_still_the_meter(self):
        """The firmware's name stays when the user renames the entity_id."""
        house = _counters_only()
        house.add("sensor.p1_in", 0.8, "kW", name="Power Consumed")
        house.add("sensor.p1_out", 0.0, "kW", name="Power Produced")
        power, picks = house.read()
        assert picks == ("sensor.p1_in", "sensor.p1_out", "same-device")
        assert power.grid_power == pytest.approx(-800.0)

    def test_the_unique_id_alone_is_enough(self):
        """A registry row with no ``original_name`` still carries the
        firmware's object id at the end of its unique_id."""
        house = _counters_only()
        house.add(IMP, 0.8, "kW", obj="power_consumed")
        house.add(EXP, 0.0, "kW", obj="power_produced")
        _, picks = house.read()
        assert picks == (IMP, EXP, "same-device")

    def test_without_the_esphome_words_it_is_lost(self, monkeypatch):
        """The control: the same house with the new words taken out reads
        no meter at all, so the tests above pin the new words."""
        monkeypatch.setattr(sr_mod, "PLATFORM_SPLIT_GRID_NAMES", {})
        power, picks = _slimmelezer(import_kw=1.2, export_kw=0.0).read()
        assert picks[:2] == (None, None)
        assert power.grid_power == pytest.approx(0.0)

    def test_the_new_sensor_trigger_knows_the_words(self):
        """A SlimmeLezer that loads after SEM must start a new search."""
        assert any(h in IMP for h in sr_mod.GRID_TRIGGER_HINTS)
        assert any(h in EXP for h in sr_mod.GRID_TRIGGER_HINTS)


class TestTheWordsStayOnESPHome:
    """The same words name solar and phases on other brands."""

    def test_sinapsi_alfa_solar_is_not_the_export(self):
        """Sinapsi Alfa's "Power Produced" is the PV output, on the very
        device that holds the grid counters."""
        house = _House("sensor.alfa_energy_drawn", "sensor.alfa_energy_fed_in",
                       device="alfa", platform="sinapsi_alfa")
        house.add("sensor.alfa_power_produced", 3200, device="alfa",
                  platform="sinapsi_alfa")
        _, picks = house.read()
        assert picks[:2] == (None, None)

    def test_silla_prism_solar_is_not_the_export(self):
        house = _House("sensor.slimmelezer_energy_consumed_tariff_1",
                       "sensor.slimmelezer_energy_produced_tariff_1")
        house.add("sensor.prism_power_produced_by_pv", 3200, device="prism",
                  platform="silla_prism")
        house.add("sensor.prism_power_produced", 3200, device="prism",
                  platform="silla_prism")
        _, picks = house.read()
        assert picks[:2] == (None, None)

    def test_sinapsi_alfa_named_power_produced(self):
        """Its own firmware name, but not on ESPHome."""
        house = _House("sensor.alfa_energy_drawn", "sensor.alfa_energy_fed_in",
                       device="alfa", platform="sinapsi_alfa")
        house.add("sensor.alfa_potenza_prodotta", 3200, device="alfa",
                  platform="sinapsi_alfa", name="Power Produced")
        _, picks = house.read()
        assert picks[:2] == (None, None)

    @pytest.mark.parametrize("name", [
        "Power Consumed Phase 1",     # the SlimmeLezer's own phase row
        "Power Consumed 3",           # a phase in other firmware
        "Solar Power Consumed",       # a CT clamp on the same device
        "Heat Pump Power Consumed",
        "PV-Power Consumed",          # ESPHome keeps the "-" in the object id
    ])
    def test_only_the_whole_name_is_the_meter(self, name):
        house = _counters_only()
        obj = name.lower().replace(" ", "_")
        house.add(f"sensor.slimmelezer_{obj}", 0.4, "kW", name=name)
        house.add(f"sensor.slimmelezer_{obj.replace('consumed', 'produced')}",
                  3.0, "kW", name=name.replace("Consumed", "Produced"))
        _, picks = house.read()
        assert picks[:2] == (None, None)

    def test_solar_beside_the_meter_is_left_out(self):
        """An ESPHome CT monitor that holds the grid counters, the meter's
        pair AND a solar clamp: the solar clamp is not the export."""
        house = _slimmelezer(import_kw=0.5, export_kw=0.0)
        house.add("sensor.slimmelezer_solar_power_produced", 3.0, "kW",
                  name="Solar Power Produced")
        power, picks = house.read()
        assert picks == (IMP, EXP, "same-device")
        assert power.grid_power == pytest.approx(-500.0)


class TestAPhaseRowNeverBeatsTheWholeMeter:
    """(sweep) On the counters' own device the search kept the last match,
    and a phase row sorts after the meter it is part of."""

    def _eg4(self, with_whole=True):
        house = _House("sensor.eg4_grid_import_energy",
                       "sensor.eg4_grid_export_energy",
                       device="eg4", platform="eg4_web_monitor")
        if with_whole:
            house.add("sensor.eg4_grid_import_power", 3000, device="eg4",
                      platform="eg4_web_monitor")
            house.add("sensor.eg4_grid_export_power", 0, device="eg4",
                      platform="eg4_web_monitor")
        for leg in ("l1", "l2"):
            house.add(f"sensor.eg4_grid_import_power_{leg}", 1500, device="eg4",
                      platform="eg4_web_monitor")
            house.add(f"sensor.eg4_grid_export_power_{leg}", 0, device="eg4",
                      platform="eg4_web_monitor")
        return house

    def test_the_whole_meter_is_read(self):
        power, picks = self._eg4().read()
        assert picks == ("sensor.eg4_grid_import_power",
                         "sensor.eg4_grid_export_power", "same-device")
        assert power.grid_power == pytest.approx(-3000.0)

    def test_phase_rows_alone_are_checked_not_trusted(self):
        """No whole-meter row: a phase row is still picked, as before, but
        it is a third of the meter. It is checked against the counters
        like a guess, and steers nothing until they agree."""
        power, picks = self._eg4(with_whole=False).read()
        assert picks == ("sensor.eg4_grid_import_power_l2",
                         "sensor.eg4_grid_export_power_l2", "any-device")
        assert power.grid_power == pytest.approx(0.0)

    def test_a_late_phase_export_is_checked_not_trusted(self):
        """The import was found alone on the counters' device and trusted.
        A phase row that loads later as the export half is a third of the
        meter: the pair is checked against the counters, not trusted."""
        house = _House("sensor.eg4_grid_import_energy",
                       "sensor.eg4_grid_export_energy",
                       device="eg4", platform="eg4_web_monitor")
        house.add("sensor.eg4_grid_import_power", 3000, device="eg4",
                  platform="eg4_web_monitor")
        power, picks = house.read()
        assert picks == ("sensor.eg4_grid_import_power", None, "same-device")
        assert power.grid_power == pytest.approx(-3000.0)
        house.add("sensor.eg4_grid_export_power_l1", 0, device="eg4",
                  platform="eg4_web_monitor")
        house.reader._split_grid_discovery["one_sided_scan_countdown"] = 0
        power, picks = house.read()
        assert picks == ("sensor.eg4_grid_import_power",
                         "sensor.eg4_grid_export_power_l1", "any-device")
        assert power.grid_power == pytest.approx(0.0)
        house.ri.raise_split_grid_guessed.assert_called_once()

    def test_a_late_whole_export_on_the_device_is_trusted(self):
        """The control: the meter's own whole export row completes the
        pair and keeps the device evidence."""
        house = _House("sensor.eg4_grid_import_energy",
                       "sensor.eg4_grid_export_energy",
                       device="eg4", platform="eg4_web_monitor")
        house.add("sensor.eg4_grid_import_power", 3000, device="eg4",
                  platform="eg4_web_monitor")
        house.read()
        house.add("sensor.eg4_grid_export_power", 0, device="eg4",
                  platform="eg4_web_monitor")
        house.reader._split_grid_discovery["one_sided_scan_countdown"] = 0
        power, picks = house.read()
        assert picks == ("sensor.eg4_grid_import_power",
                         "sensor.eg4_grid_export_power", "same-device")
        assert power.grid_power == pytest.approx(-3000.0)

    def test_english_dsmr_phase_rows_by_name(self):
        """Core dsmr's English names, found by words (its key is asked
        first on a real install; here only the name tier answers)."""
        house = _House("sensor.electricity_meter_energy_consumption_tariff_1",
                       "sensor.electricity_meter_energy_production_tariff_1",
                       device="meter", platform="p1_meter")
        house.add("sensor.electricity_meter_power_consumption", 2.4, "kW",
                  device="meter", platform="p1_meter")
        house.add("sensor.electricity_meter_power_production", 0.0, "kW",
                  device="meter", platform="p1_meter")
        for leg in ("l1", "l2", "l3"):
            house.add(f"sensor.electricity_meter_power_consumption_phase_{leg}",
                      0.8, "kW", device="meter", platform="p1_meter")
            house.add(f"sensor.electricity_meter_power_production_phase_{leg}",
                      0.0, "kW", device="meter", platform="p1_meter")
        power, picks = house.read()
        assert picks[:2] == ("sensor.electricity_meter_power_consumption",
                             "sensor.electricity_meter_power_production")
        assert power.grid_power == pytest.approx(-2400.0)

    @pytest.mark.parametrize("object_id, phase", [
        ("power_consumption_phase_l1", True),
        ("grid_import_power_l2", True),
        ("power_consumed_phase_3", True),
        ("inverter_l1_grid_import", True),
        ("grid_import_phase_a", True),
        ("power_consumption", False),
        ("grid_import_power", False),
        ("house_l10_import_power", False),
        ("current_power_consumption_phase", False),
    ])
    def test_what_a_phase_row_is(self, object_id, phase):
        assert bool(sr_mod._PHASE_ROW.search(object_id)) is phase
