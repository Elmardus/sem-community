"""#812 — a tariff per device (a heat pump on its own meter and price).

A German "Messkonzept 8" house buys the heat pump's kWh on a second, cheaper
contract with its own meter. SEM assumed one import price for the house. A
device may now name its own tariff: a fixed rate or a price entity, and
optionally its own meter.

* none set = the house tariff, exactly as before;
* a fixed rate prices the device's grid kWh and reads ``normal`` — a flat
  price has nothing to compare against, so it is never cheap (#994);
* a price entity is classified by the house classifier, so the device's
  cheap hours follow its own curve;
* the cost sensor exists only when a tariff is set;
* the settings are in the options flow AND the cards.
"""
from __future__ import annotations

import json
import re
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from homeassistant.util import dt as dt_util

from custom_components.solar_energy_management.coordinator.device_tariff import (
    LEVEL_FIXED, DeviceTariffs, apply_tariff, device_price_level, grid_share,
    heat_pump_tariff, tariff_of,
)
from custom_components.solar_energy_management.coordinator.decision_log import (
    DecisionLog, record_load,
)
from custom_components.solar_energy_management.coordinator.energy_calculator import (
    EnergyCalculator,
)
from custom_components.solar_energy_management.coordinator.price_signal import (
    is_cheap_name,
)
from custom_components.solar_energy_management.coordinator.surplus_controller import (
    SurplusController,
)
from custom_components.solar_energy_management.devices.base import DeviceControlMode

ROOT = Path(__file__).resolve().parents[1]
H = 3600.0

#: 24 hourly prices starting NOW — now is the cheapest hour.
CURVE_CHEAP_NOW = [0.05] + [0.30 + 0.01 * i for i in range(23)]
#: …and one where now is the dearest hour.
CURVE_DEAR_NOW = [0.60] + [0.10 + 0.01 * i for i in range(23)]


def _load(device_id="pump", min_power=731, *, rate=None, entity=None,
          meter=None, deficit_s=4 * H):
    """A cheap-hours load (the #953 shape), optionally on its own tariff."""
    d = MagicMock()
    d.device_id = device_id
    d.name = device_id
    d.priority = 1
    d.min_power_threshold = min_power
    d.rated_power = min_power
    d.is_enabled = True
    d.managed_externally = False
    d.is_active = False
    d.device_type = MagicMock(value="switch")
    d.control_mode = DeviceControlMode.SURPLUS
    d._sem_owned = False
    d.battery_assist_enabled = False
    d.battery_eligible_overnight = False
    d.top_up_policy = "cheap_hours"
    d.remaining_daily_runtime_sec = deficit_s
    d.has_runtime_deficit = deficit_s > 0
    d.needs_offpeak_activation = deficit_s > 0
    d.activate = AsyncMock(return_value=min_power)
    d.adjust_power = AsyncMock(return_value=min_power)
    d.get_current_consumption = MagicMock(return_value=0.0)
    d.can_activate = MagicMock(return_value=True)
    d.can_deactivate = MagicMock(return_value=True)
    d.status = MagicMock()
    d.status.allocated_power_w = 0.0
    d._offpeak_forced = False
    d._offpeak_forced_date = None
    d._batt_overnight_forced = False
    d._batt_overnight_forced_date = None
    d._daily_runtime_meter_day = None
    d.daily_targets_met = False
    d.daily_max_runtime_reached = False
    d.stop_condition_met = False
    d.is_deadline_approaching = False
    d.start_reserve_w = 0.0
    d.comfort_state = ""
    d.__class__ = MagicMock

    async def _deact():
        d.is_active = False
    d.deactivate = AsyncMock(side_effect=_deact)
    # the tariff fields as a real device carries them (class defaults)
    d.tariff_rate, d.tariff_entity, d.tariff_meter = 0.0, "", ""
    d._tariff_level, d.tariff_inputs = None, None
    apply_tariff(d, {k: v for k, v in (("tariff_rate", rate),
                                       ("tariff_entity", entity),
                                       ("tariff_meter", meter)) if v is not None})
    return d


def _price_state(curve, state=None):
    now = dt_util.now().replace(minute=0, second=0, microsecond=0)
    raw = [{"start": (now + timedelta(hours=i)).isoformat(), "value": v}
           for i, v in enumerate(curve)]
    return SimpleNamespace(state=str(curve[0] if state is None else state),
                           attributes={"raw_today": raw})


def _hass_with(states: dict):
    hass = MagicMock()
    hass.states.get = lambda eid: states.get(eid)
    return hass


def _imported(calc, kwh, rate):
    """Today's house import, booked the way ``calculate_energy`` books it."""
    calc._import_rate = rate
    day = dt_util.now().date()
    mk, yk = calc._month_key(day), str(day.year)
    calc._accumulate("grid_import", day, mk, yk, kwh)
    calc._accumulate_cost("cost_import", day, mk, yk, kwh * rate)
    return calc, day


async def _night_cycle(sc, price_level):
    await sc.update(0.0, price_level=price_level, reclaim_w=0.0,
                    battery_priority=100, battery_soc=80.0,
                    battery_reserve_soc=10.0, is_night=True,
                    daylight_remaining_s=0.0)


# ── 1. default = the house tariff, exactly ───────────────────────────────


@pytest.mark.unit
class TestNoTariffIsTheHouseTariff:
    def test_a_device_without_one_reads_the_house_level(self):
        d = _load()
        DeviceTariffs(None).stamp([d])
        assert d._tariff_level is None and d.tariff_inputs is None
        for house in ("cheap", "normal", "expensive", "flat", None):
            assert device_price_level(d, house) == house

    async def test_the_cheap_hour_still_starts_the_load(self, mock_hass):
        sc = SurplusController(mock_hass, regulation_offset=0)
        pump = _load()
        sc.register_device(pump)
        await _night_cycle(sc, "cheap")
        pump.activate.assert_awaited_once()

    async def test_a_normal_hour_still_does_not(self, mock_hass):
        sc = SurplusController(mock_hass, regulation_offset=0)
        pump = _load()
        sc.register_device(pump)
        await _night_cycle(sc, "normal")
        assert not pump.activate.called

    def test_nothing_is_booked_and_no_inputs_change(self):
        tariffs = DeviceTariffs(None)
        d = _load()
        assert tariffs.grid_kwh(d, 1.0, 1.0) is None
        assert tariffs.price_now(d) is None
        log = DecisionLog(None)
        log.set_inputs({"import_price": 0.30, "price_level": "cheap"})
        rec = record_load(log, d, on=True)
        assert rec.inputs == {"import_price": 0.30, "price_level": "cheap"}

    def test_a_mock_attribute_is_not_a_tariff(self):
        """A device that never set the fields must not read as configured
        because a test double answers every attribute."""
        assert not tariff_of(MagicMock()).configured


# ── 2. a fixed rate: priced, and "normal" ────────────────────────────────


@pytest.mark.unit
class TestAFixedRate:
    def test_it_reads_normal_never_cheap(self):
        d = _load(rate=0.21)
        tariffs = DeviceTariffs(None)
        assert tariffs.level(d) == LEVEL_FIXED == "normal"
        tariffs.stamp([d])
        assert device_price_level(d, "cheap") == "normal"
        assert not is_cheap_name(device_price_level(d, "very_cheap"))

    async def test_a_cheap_house_hour_does_not_start_it(self, mock_hass):
        sc = SurplusController(mock_hass, regulation_offset=0)
        pump = _load(rate=0.21)
        sc.register_device(pump)
        await _night_cycle(sc, "cheap")
        assert not pump.activate.called

    async def test_a_running_top_up_ends_with_its_own_reason(self, mock_hass):
        sc = SurplusController(mock_hass, regulation_offset=0)
        pump = _load(rate=0.21, deficit_s=3 * H)
        pump.is_active = True
        pump._sem_owned = True
        pump._offpeak_forced = True
        pump._daily_runtime_meter_day = dt_util.now().date()
        pump._offpeak_forced_date = pump._daily_runtime_meter_day
        pump.needs_offpeak_activation = False
        pump.get_current_consumption = MagicMock(return_value=731.0)
        sc.register_device(pump)
        await _night_cycle(sc, "cheap")
        assert pump.deactivate.called

    def test_its_grid_kwh_are_priced_at_the_rate(self):
        d = _load(rate=0.20)
        tariffs = DeviceTariffs(None)
        assert tariffs.price_now(d) == pytest.approx(0.20)
        # 0.5 kWh this cycle, the grid carried 40 % of the house
        assert tariffs.grid_kwh(d, 0.5, grid_share(800.0, 2000.0)) == pytest.approx(0.2)

    def test_the_log_shows_the_device_price(self):
        d = _load(rate=0.20)
        DeviceTariffs(None).stamp([d])
        log = DecisionLog(None)
        log.set_inputs({"import_price": 0.30, "price_level": "cheap", "soc": 50})
        rec = record_load(log, d, on=True)
        assert rec.inputs["import_price"] == pytest.approx(0.20)
        assert rec.inputs["price_level"] == "normal"
        assert rec.inputs["soc"] == 50       # the house context stays


# ── 3. a price entity: its own curve gates it ────────────────────────────


@pytest.mark.unit
class TestAPriceEntity:
    async def test_its_cheap_hour_starts_it_when_the_house_is_normal(self, mock_hass):
        mock_hass.states.get = lambda eid: (
            _price_state(CURVE_CHEAP_NOW) if eid == "sensor.hp_price" else None)
        sc = SurplusController(mock_hass, regulation_offset=0)
        pump = _load(entity="sensor.hp_price")
        sc.register_device(pump)
        await _night_cycle(sc, "normal")
        assert is_cheap_name(pump._tariff_level), pump._tariff_level
        pump.activate.assert_awaited_once()

    async def test_its_dear_hour_holds_it_when_the_house_is_cheap(self, mock_hass):
        mock_hass.states.get = lambda eid: (
            _price_state(CURVE_DEAR_NOW) if eid == "sensor.hp_price" else None)
        sc = SurplusController(mock_hass, regulation_offset=0)
        pump = _load(entity="sensor.hp_price")
        sc.register_device(pump)
        await _night_cycle(sc, "cheap")
        assert not is_cheap_name(pump._tariff_level)
        assert not pump.activate.called

    def test_the_house_classifier_is_reused_not_copied(self):
        from custom_components.solar_energy_management.tests.ast_contracts import calls
        assert calls(DeviceTariffs.provider_for, "DynamicTariffProvider")
        assert calls(DeviceTariffs.level, "comparative_level")
        assert calls(DeviceTariffs.level, "absence_word")

    def test_an_unreadable_entity_is_not_cheap_and_falls_back_to_the_rate(self):
        d = _load(rate=0.19, entity="sensor.hp_price")
        tariffs = DeviceTariffs(_hass_with({
            "sensor.hp_price": SimpleNamespace(state="unavailable", attributes={})}))
        assert not is_cheap_name(tariffs.level(d))
        assert tariffs.price_now(d) == pytest.approx(0.19)

    def test_the_price_is_the_entity_state(self):
        d = _load(entity="sensor.hp_price")
        tariffs = DeviceTariffs(_hass_with({"sensor.hp_price": _price_state(CURVE_CHEAP_NOW)}))
        assert tariffs.price_now(d) == pytest.approx(0.05)


# ── 4. the own meter ─────────────────────────────────────────────────────


@pytest.mark.unit
class TestTheOwnMeter:
    def test_its_steps_are_the_grid_kwh(self):
        states = {"sensor.hp_meter": SimpleNamespace(state="100.0", attributes={})}
        tariffs = DeviceTariffs(_hass_with(states))
        d = _load(rate=0.20, meter="sensor.hp_meter")
        assert tariffs.grid_kwh(d, 9.9, 1.0) is None          # baseline
        states["sensor.hp_meter"] = SimpleNamespace(state="100.4", attributes={})
        assert tariffs.grid_kwh(d, 9.9, 1.0) == pytest.approx(0.4)

    def test_a_reset_or_a_jump_is_not_consumption(self):
        states = {"sensor.hp_meter": SimpleNamespace(state="100.0", attributes={})}
        tariffs = DeviceTariffs(_hass_with(states))
        d = _load(rate=0.20, meter="sensor.hp_meter")
        tariffs.grid_kwh(d, 0, 0)
        states["sensor.hp_meter"] = SimpleNamespace(state="0.2", attributes={})
        assert tariffs.grid_kwh(d, 0, 0) is None
        states["sensor.hp_meter"] = SimpleNamespace(state="900.0", attributes={})
        assert tariffs.grid_kwh(d, 0, 0) is None


# ── 5. the books, through the coordinator seam ───────────────────────────


@pytest.mark.unit
class TestTheBooks:
    def _seam(self, devices):
        from custom_components.solar_energy_management.coordinator.coordinator import (
            SEMCoordinator,
        )
        sc = SimpleNamespace(_devices={d.device_id: d for d in devices},
                             device_tariffs=DeviceTariffs(None))
        calc, _ = _imported(EnergyCalculator({}, MagicMock()), 5.0, 0.30)
        fake = SimpleNamespace(_energy_calculator=calc, _surplus_controller=sc)
        return lambda power, flows: SEMCoordinator._book_device_tariffs(
            fake, power, flows)

    def test_a_device_on_its_own_rate_is_costed(self):
        pump = _load("heat_pump", rate=0.20)
        pump.last_cycle_energy_kwh = 0.5
        book = self._seam([pump])
        power = SimpleNamespace(home_consumption_power=2000.0)
        flows = SimpleNamespace(grid_to_home=1000.0)
        book(power, flows)
        out = book(power, flows)
        assert out["daily_tariff_grid_kwh_heat_pump"] == pytest.approx(0.5)
        assert out["daily_tariff_cost_heat_pump"] == pytest.approx(0.1)
        assert out["tariff_price_heat_pump"] == pytest.approx(0.20)

    def test_a_device_without_one_adds_nothing(self):
        pump = _load("heat_pump")
        pump.last_cycle_energy_kwh = 0.5
        out = self._seam([pump])(SimpleNamespace(home_consumption_power=2000.0),
                                 SimpleNamespace(grid_to_home=2000.0))
        assert out == {}

    def test_sun_only_costs_nothing(self):
        pump = _load("heat_pump", rate=0.20)
        pump.last_cycle_energy_kwh = 0.5
        out = self._seam([pump])(SimpleNamespace(home_consumption_power=2000.0),
                                 SimpleNamespace(grid_to_home=0.0))
        assert out["daily_tariff_cost_heat_pump"] == 0.0


# ── 6. heat pump config → device, and a goal on any load ─────────────────


@pytest.mark.unit
class TestWhereTheTariffComesFrom:
    def test_a_heat_pump_row_carries_it(self):
        from custom_components.solar_energy_management import _heat_pump_rows
        rows = _heat_pump_rows({
            "heat_pump_relay1_entity": "switch.a",
            "heat_pump_relay2_entity": "switch.b",
            "heat_pump_tariff_rate": 0.22,
            "heat_pump_tariff_meter": "sensor.hp_meter",
            "heat_pumps": [{"heat_pump_climate_entity": "climate.x",
                            "heat_pump_tariff_entity": "sensor.hp2_price"}],
        })
        d1, d2 = SimpleNamespace(), SimpleNamespace()
        apply_tariff(d1, heat_pump_tariff(rows[0]))
        apply_tariff(d2, heat_pump_tariff(rows[1]))
        assert tariff_of(d1).rate == pytest.approx(0.22)
        assert tariff_of(d1).meter == "sensor.hp_meter"
        assert tariff_of(d2).entity == "sensor.hp2_price"

    def test_a_goal_sets_it_on_any_load(self):
        from custom_components.solar_energy_management.features.device_registry import (
            UnifiedDeviceRegistry,
        )
        assert {"tariff_rate", "tariff_entity", "tariff_meter"} <= set(
            UnifiedDeviceRegistry.GOAL_PROPERTIES)
        reg = UnifiedDeviceRegistry.__new__(UnifiedDeviceRegistry)
        reg._device_goals = {"pool": {"tariff_rate": "0.18"}}
        dev = SimpleNamespace(device_id="pool", tariff_rate=0.0, tariff_entity="",
                              tariff_meter="")
        reg._apply_goals(dev)
        assert tariff_of(dev).rate == pytest.approx(0.18)

    def test_a_bad_rate_is_none(self):
        for bad in (-1, "x", float("nan"), None, True):
            d = SimpleNamespace()
            apply_tariff(d, {"tariff_rate": bad})
            assert not tariff_of(d).configured


# ── 7. the cost sensor exists only when set ──────────────────────────────


@pytest.mark.unit
class TestTheSensorExistsOnlyWhenSet:
    def test_one_sensor_per_device_with_a_tariff(self):
        from custom_components.solar_energy_management.sensor import (
            _device_tariff_descriptions,
        )
        coord = SimpleNamespace(_surplus_controller=SimpleNamespace(_devices={
            "heat_pump": _load("heat_pump", rate=0.2),
            "heat_pump_2": _load("heat_pump_2"),
            "pool": _load("pool", entity="sensor.pool_price"),
        }))
        hass = SimpleNamespace(config=SimpleNamespace(currency="EUR"))
        keys = [d.key for d in _device_tariff_descriptions(hass, coord)]
        assert keys == ["daily_tariff_cost_heat_pump", "daily_tariff_cost_pool"]

    def test_none_set_means_none(self):
        from custom_components.solar_energy_management.sensor import (
            _device_tariff_descriptions,
        )
        coord = SimpleNamespace(_surplus_controller=SimpleNamespace(
            _devices={"heat_pump": _load("heat_pump")}))
        hass = SimpleNamespace(config=SimpleNamespace(currency="EUR"))
        assert _device_tariff_descriptions(hass, coord) == []


# ── 8. the settings: options flow AND cards ──────────────────────────────

FLOW_KEYS = ("heat_pump_tariff_rate", "heat_pump_tariff_entity",
             "heat_pump_tariff_meter")


@pytest.mark.unit
class TestTheSettingsAreEverywhere:
    def test_both_heat_pump_steps_show_them(self):
        from custom_components.solar_energy_management.config_flow import (
            OPTIONS_FLOW_OWNED_KEYS, OptionsFlowHandler,
        )
        fields = OptionsFlowHandler._heat_pump_tariff_fields(
            SimpleNamespace(hass=None), lambda k: None)
        assert {str(k) for k in fields} == set(FLOW_KEYS)
        src = (ROOT / "config_flow.py").read_text("utf-8")
        assert src.count("**self._heat_pump_tariff_fields(") == 2
        assert set(FLOW_KEYS) <= OPTIONS_FLOW_OWNED_KEYS

    def test_they_reload_like_the_rest_of_the_pump(self):
        from custom_components.solar_energy_management import (
            _SET_OPTION_STRUCTURAL_KEYS,
        )
        assert set(FLOW_KEYS) <= _SET_OPTION_STRUCTURAL_KEYS
        card = (ROOT / "dashboard/card/src/cards/sem-config-card.js").read_text("utf-8")
        for k in FLOW_KEYS:
            assert f"'{k}'" in card

    def test_the_config_card_shows_them(self):
        card = (ROOT / "dashboard/card/src/cards/sem-config-card.js").read_text("utf-8")
        for k in FLOW_KEYS:
            assert re.search(rf"_render\w+\('{k}'", card), k

    def test_the_load_card_shows_them_per_device(self):
        card = (ROOT / "dashboard/card/src/cards/sem-load-priority-card.js").read_text("utf-8")
        assert "_renderOwnTariff(device)" in card
        for k in ("tariff_rate", "tariff_entity", "tariff_meter"):
            assert f"'{k}'" in card

    def test_every_language_has_the_words(self):
        dash = json.loads((ROOT / "dashboard/translations.json").read_text("utf-8"))
        words = ("device_tariff_section", "device_tariff_rate",
                 "device_tariff_entity", "device_tariff_meter",
                 "device_tariff_hint", "device_tariff_heat_pump",
                 "config_help_hp_tariff_rate", "config_help_hp_tariff_entity",
                 "config_help_hp_tariff_meter")
        assert [(lang, w) for lang, t in dash.items() for w in words
                if not t.get(w)] == []
        for path in [ROOT / "strings.json", *sorted((ROOT / "translations").glob("*.json"))]:
            data = json.loads(path.read_text("utf-8"))
            steps = data["options"]["step"]
            for k in FLOW_KEYS:
                assert steps["heat_pump"]["data"].get(k), (path.name, k)
                assert steps["heat_pump_unit"]["data"].get(k), (path.name, k)
            assert "{device}" in data["entity"]["sensor"]["per_device_tariff_cost"]["name"]


# ── 9. pipeline: a real setup with a heat pump on its own rate ───────────


@pytest.mark.asyncio
async def test_a_real_setup_gives_the_pump_its_tariff_and_sensor(
        sem_real_hass, sem_config_entry):
    """Config row → controller → stamp → gate, log and books → the sensor."""
    from custom_components.solar_energy_management.const import DOMAIN
    from .test_services_real import _seed_sem_input_sensors

    _seed_sem_input_sensors(sem_real_hass)
    for eid in ("switch.hp_relay", "switch.hp_relay_b",
                "switch.hp2_relay", "switch.hp2_relay_b"):
        sem_real_hass.states.async_set(eid, "off")
    sem_config_entry.add_to_hass(sem_real_hass)
    sem_real_hass.config_entries.async_update_entry(
        sem_config_entry, options={
            **sem_config_entry.options,
            "heat_pump_relay1_entity": "switch.hp_relay",
            "heat_pump_relay2_entity": "switch.hp_relay_b",
            "heat_pump_tariff_rate": 0.21,
            "heat_pumps": [{"heat_pump_relay1_entity": "switch.hp2_relay",
                            "heat_pump_relay2_entity": "switch.hp2_relay_b"}],
        })
    assert await sem_real_hass.config_entries.async_setup(sem_config_entry.entry_id)
    await sem_real_hass.async_block_till_done()

    coord = sem_real_hass.data[DOMAIN][sem_config_entry.entry_id]
    coord = getattr(coord, "coordinator", coord)
    sc = coord._surplus_controller
    hp, hp2 = sc.get_device("heat_pump"), sc.get_device("heat_pump_2")
    assert tariff_of(hp).rate == pytest.approx(0.21)
    assert not tariff_of(hp2).configured

    # the gate: own tariff reads normal, the second pump the house level
    sc.device_tariffs.stamp([hp, hp2])
    assert device_price_level(hp, "cheap") == "normal"
    assert device_price_level(hp2, "cheap") == "cheap"

    # the books: the pump's kWh, the grid's share, the pump's rate
    hp._last_cycle_energy_kwh = 1.0
    hp2._last_cycle_energy_kwh = 1.0
    _imported(coord._energy_calculator, 5.0, 0.30)
    out = coord._book_device_tariffs(
        SimpleNamespace(home_consumption_power=1000.0),
        SimpleNamespace(grid_to_home=500.0))
    assert out["daily_tariff_cost_heat_pump"] == pytest.approx(0.105, abs=0.01)
    assert not any(k.endswith("heat_pump_2") for k in out)

    # the sensor: there for the pump on its own rate, absent for the other
    assert sem_real_hass.states.get("sensor.sem_daily_tariff_cost_heat_pump") is not None
    assert sem_real_hass.states.get("sensor.sem_daily_tariff_cost_heat_pump_2") is None


# ── 10. the house cost: one total, no double count ───────────────────────


@pytest.mark.unit
class TestTheHouseCostIsOneTotal:
    @staticmethod
    def _calc_with_import(kwh, rate):
        return _imported(EnergyCalculator({}, MagicMock()), kwh, rate)

    def test_no_tariff_is_todays_number(self):
        calc, day = self._calc_with_import(2.0, 0.30)
        pump = _load("heat_pump")
        pump.last_cycle_energy_kwh = 1.0
        SEMCoordinatorSeam(calc, [pump]).book(grid_to_home=2000.0, home=2000.0)
        assert calc._get_daily_cost("cost_import", day) == pytest.approx(0.60)

    def test_the_device_kwh_move_to_its_own_rate(self):
        # 2 kWh imported at 0.30; the pump took 1 kWh of it on its 0.20 rate
        calc, day = self._calc_with_import(2.0, 0.30)
        pump = _load("heat_pump", rate=0.20)
        pump.last_cycle_energy_kwh = 1.0
        out = SEMCoordinatorSeam(calc, [pump]).book(grid_to_home=2000.0, home=2000.0)
        assert calc._get_daily_cost("cost_import", day) == pytest.approx(0.50)
        assert out["daily_tariff_cost_heat_pump"] == pytest.approx(0.20)

    def test_kwh_leave_at_the_rate_they_were_booked_at(self):
        # The reviewer's case: 1 kWh imported at 0.10; later the house rate
        # is 0.40 and the pump bills 1 kWh at 0.05. Moving at 0.40 would
        # leave the house at 0.10 − 0.40 + 0.05 = −0.25.
        calc, day = self._calc_with_import(1.0, 0.10)
        calc._import_rate = 0.40
        calc.accumulate_device_tariff("heat_pump", 1.0, 0.05, day)
        assert calc._get_daily_cost("cost_import", day) == pytest.approx(0.05)
        assert calc._get_daily_cost("cost_import", day) >= 0

    def test_mixed_rates_move_at_their_average_and_never_below_zero(self):
        calc, day = self._calc_with_import(1.0, 0.10)
        mk, yk = calc._month_key(day), str(day.year)
        calc._accumulate("grid_import", day, mk, yk, 1.0)
        calc._accumulate_cost("cost_import", day, mk, yk, 0.50)   # 1 kWh at 0.50
        for _ in range(4):                                         # 4 × 0.5 kWh
            calc.accumulate_device_tariff("heat_pump", 0.5, 0.0, day)
        # everything moved at a free rate: the house money is exactly gone
        assert calc._get_daily_cost("cost_import", day) == pytest.approx(0.0, abs=1e-9)

    def test_a_meter_running_ahead_books_the_same_capped_kwh_on_both_sides(self):
        calc, day = self._calc_with_import(0.5, 0.30)
        moved = calc.accumulate_device_tariff("heat_pump", 2.0, 0.20, day)
        assert moved == pytest.approx(0.5)
        # house: 0.5 × 0.20; the pump: the same 0.5 kWh, nothing more
        assert calc._get_daily_cost("cost_import", day) == pytest.approx(0.10)
        books = calc.get_device_tariff("heat_pump", day)
        assert books["daily_grid_kwh"] == pytest.approx(0.5)
        assert books["daily_cost"] == pytest.approx(0.10)

    def test_an_unreadable_price_keeps_the_house_rate_and_says_so(self, caplog):
        calc, day = self._calc_with_import(2.0, 0.30)
        pump = _load("heat_pump", entity="sensor.hp_price")
        pump.last_cycle_energy_kwh = 1.0
        seam = SEMCoordinatorSeam(calc, [pump])
        seam.tariffs = DeviceTariffs(_hass_with({
            "sensor.hp_price": SimpleNamespace(state="unavailable", attributes={})}))
        with caplog.at_level("INFO"):
            out = seam.book(grid_to_home=2000.0, home=2000.0)
        assert out["daily_tariff_grid_kwh_heat_pump"] == pytest.approx(1.0)
        assert out["daily_tariff_cost_heat_pump"] == pytest.approx(0.30)
        assert out["tariff_price_source_heat_pump"] == "house"
        assert calc._get_daily_cost("cost_import", day) == pytest.approx(0.60)
        assert "house rate" in caplog.text

    def test_the_own_meter_survives_a_restart(self):
        states = {"sensor.hp_meter": SimpleNamespace(state="100.0", attributes={})}
        calc = EnergyCalculator({}, MagicMock())
        d = _load("heat_pump", rate=0.20, meter="sensor.hp_meter")
        DeviceTariffs(_hass_with(states)).grid_kwh(
            d, 0, 0, store=calc.device_meter_last)
        saved = calc.get_state()
        fresh = EnergyCalculator({}, MagicMock())
        fresh.restore_state(saved)
        states["sensor.hp_meter"] = SimpleNamespace(state="100.7", attributes={})
        step = DeviceTariffs(_hass_with(states)).grid_kwh(
            d, 0, 0, store=fresh.device_meter_last)
        assert step == pytest.approx(0.7)


class SEMCoordinatorSeam:
    """The coordinator's books seam over a real calculator."""

    def __init__(self, calc, devices):
        self.calc, self.devices = calc, devices
        self.tariffs = DeviceTariffs(None)

    def book(self, *, grid_to_home, home):
        from custom_components.solar_energy_management.coordinator.coordinator import (
            SEMCoordinator,
        )
        sc = SimpleNamespace(_devices={d.device_id: d for d in self.devices},
                             device_tariffs=self.tariffs)
        fake = SimpleNamespace(_energy_calculator=self.calc, _surplus_controller=sc)
        return SEMCoordinator._book_device_tariffs(
            fake, SimpleNamespace(home_consumption_power=home),
            SimpleNamespace(grid_to_home=grid_to_home))


# ── 11. the plan reads the device's own tariff ───────────────────────────


@pytest.mark.unit
class TestThePlanReadsTheOwnTariff:
    HOUSE = [0.40, 0.10, 0.40, 0.40, 0.40, 0.40]   # house cheap at slot 1

    def _ledger(self):
        from custom_components.solar_energy_management.coordinator.energy_planner import (
            LedgerSlot, build_night_ledger,
        )
        t0 = dt_util.now().replace(minute=0, second=0, microsecond=0)
        slots = [LedgerSlot(start=t0 + timedelta(hours=n),
                            end=t0 + timedelta(hours=n + 1), price=p,
                            level_cheap=p < 0.15, home_w=300.0)
                 for n, p in enumerate(self.HOUSE)]
        return t0, build_night_ledger(slots, soc_kwh=0.0, floor_kwh=0.0,
                                      max_discharge_w=5000.0, peak_limit_w=0.0)

    def _pack(self, tariff):
        from custom_components.solar_energy_management.coordinator.energy_planner import (
            Demand, pack_night,
        )
        t0, ledger = self._ledger()
        d = Demand(id="load:heat_pump", kind="load", energy_kwh=1.0,
                   max_power_w=1000.0, min_power_w=1000.0,
                   deadline=t0 + timedelta(hours=6), needs_cheap_level=True,
                   tariff=tariff)
        plan = pack_night([d], ledger)
        return t0, [a for a in plan.allocations if a.demand_id == d.id], plan

    def test_no_tariff_packs_the_house_cheap_hour(self):
        t0, allocs, _ = self._pack(None)
        assert [a.start for a in allocs] == [t0 + timedelta(hours=1)]

    def test_a_fixed_rate_has_no_cheaper_hour(self):
        d = _load("heat_pump", rate=0.20)
        tariff = DeviceTariffs(None).plan_tariff(d)
        _, allocs, plan = self._pack(tariff)
        assert allocs == []
        assert "no cheap window" in plan.results[0].note

    def test_a_price_entity_packs_its_own_cheap_hour(self):
        # the device curve is cheapest at slot 4, dear at the house's slot 1
        curve = [0.30, 0.50, 0.32, 0.31, 0.05, 0.33] + [0.30] * 18
        d = _load("heat_pump", entity="sensor.hp_price")
        tariffs = DeviceTariffs(_hass_with({
            "sensor.hp_price": _price_state(curve)}))
        t0, allocs, _ = self._pack(tariffs.plan_tariff(d))
        assert [a.start for a in allocs] == [t0 + timedelta(hours=4)]
        assert allocs[0].price == pytest.approx(0.05)

    def test_the_coordinator_builds_load_demands_through_the_one_reader(self):
        from custom_components.solar_energy_management.tests.ast_contracts import (
            call_kwargs,
        )
        from custom_components.solar_energy_management.coordinator.coordinator import (
            SEMCoordinator,
        )
        # every load and comfort demand names its tariff …
        plan_fns = [getattr(SEMCoordinator, n) for n in dir(SEMCoordinator)
                    if callable(getattr(SEMCoordinator, n, None))
                    and any("_plan_tariff" in str(c) for c in
                            getattr(getattr(SEMCoordinator, n), "__code__",
                                    SimpleNamespace(co_varnames=()))
                            .co_varnames)]
        assert len(plan_fns) == 1, plan_fns
        demands = call_kwargs(plan_fns[0], "Demand")
        named = [kw for kw in demands if "needs_cheap_level" in kw]
        assert named and all("tariff" in kw for kw in named)
        # … and that reader is the live gate's DeviceTariffs
        from custom_components.solar_energy_management.tests.ast_contracts import calls
        assert calls(plan_fns[0], "plan_tariff")
