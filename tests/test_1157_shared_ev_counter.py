"""#1157 — a counter no single charger owns is judged too.

With no charger counter of its own, SEM takes the day's car energy from the
legacy daily EV sensor or the Energy Dashboard EV counter. That counter
belongs to no one box, and neither does a counter two chargers name. The
#1101 step check asked "could THE box have drawn this?" and only knew boxes
of their own, so a jump in such a counter was booked as charging while every
charger read 0 W, and home dropped by the same amount.

The rule now: a counter is judged against every box it counts — the sum of
their draws, and as much room as that many boxes could fill.
"""
from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from freezegun import freeze_time

from custom_components.solar_energy_management.coordinator.coordinator import (
    SEMCoordinator,
)
from custom_components.solar_energy_management.coordinator.energy_calculator import (
    EV_CATEGORY,
    MIDNIGHT_EV_CATEGORY,
    EnergyCalculator,
)
from custom_components.solar_energy_management.coordinator.types import PowerReadings
from custom_components.solar_energy_management.utils.time_manager import TimeManager

from .ast_contracts import call_sites

ED_EV = "sensor.wallbox_energy"         # the Energy Dashboard's EV row
DAILY = "sensor.ev_daily_energy"         # the legacy daily EV sensor
LINKS_W = "sensor.links_power"
RECHTS_W = "sensor.rechts_power"
FLEET_W = "sensor.ev_power"
POWER = {"links": LINKS_W, "rechts": RECHTS_W}

NIGHT = "2026-10-12 00:00:30"
TODAY = "2026-10-12"
CATCH_UP = 11.18
CHARGE_W = 10_800.0
CHARGE_KWH = 0.18


def _hass(values: dict) -> Mock:
    hass = Mock()
    hass.data = {}
    hass.config = Mock()
    hass.config.config_dir = "/config"

    def _get(entity_id):
        if entity_id not in values:
            return None
        state = Mock()
        state.state = str(values[entity_id])
        state.attributes = {"unit_of_measurement": "kWh"}
        return state

    hass.states.get = _get
    return hass


def _calc(values: dict, counter: str, boxes, *, chargers=("links", "rechts"),
          sensors=("links", "rechts"), guessed=()) -> EnergyCalculator:
    """A calculator set up the way the coordinator sets up a counter no box
    owns: not complete, no owner, and the boxes it counts. ``sensors``: the
    chargers with a power sensor of their own. ``guessed`` (#1159): the
    counters given the whole fleet because SEM cannot tell their boxes."""
    hass = _hass(values)
    config = {
        "update_interval": 60,
        "ev_target_time": "11:00",
        "ev_max_current": 32, "ev_phases": 3, "ev_voltage": 230,
    }
    if chargers:
        config["ev_chargers"] = [
            {"id": cid, **({"ev_charging_power_sensor": POWER[cid]}
                           if cid in sensors else {})}
            for cid in chargers]
    calc = EnergyCalculator(config, TimeManager(hass))
    calc.configure_ev_counters(hass, [counter], True, complete=False,
                               owners={}, boxes={counter: list(boxes)},
                               guessed=list(guessed))
    return calc


def _read(calc, values, links_w=0.0, rechts_w=0.0, *,
          sensors=("links", "rechts")):
    """One energy cycle, as the sensor reader hands it over: each box with a
    power sensor of its own in ``ev_power_per_charger``; none → the fleet
    sensor (Energy Dashboard or top level). ``_last_update`` is cleared so
    the configured interval is used."""
    calc._last_update = None
    readings = PowerReadings(ev_power=links_w + rechts_w)
    if sensors:
        for cid, watts in (("links", links_w), ("rechts", rechts_w)):
            if cid in sensors and values.get(POWER[cid]) != "unavailable":
                values[POWER[cid]] = watts
        readings.ev_power_per_charger = {
            cid: w for cid, w in (("links", links_w), ("rechts", rechts_w))
            if cid in sensors}
    else:
        values[FLEET_W] = links_w + rechts_w
        readings.ev_power_fleet_entity = FLEET_W
    readings.calculate_derived()
    calc.calculate_energy(readings)


def _cycle(calc, clock, values, links_w=0.0, rechts_w=0.0, **kw):
    clock.tick(timedelta(seconds=60))
    _read(calc, values, links_w, rechts_w, **kw)


def _watched(values, counter, boxes, **kw):
    """A calculator that has seen the counter move once: the watch starts."""
    sensors = kw.get("sensors", ("links", "rechts"))
    calc = _calc(values, counter, boxes, **kw)
    for bump in (0.0, 0.002):
        values[counter] += bump
        _read(calc, values, sensors=sensors)
    assert counter in calc._ev_counter_room, "vacuous: no watch started"
    return calc


def _rows(calc, day: str = TODAY) -> tuple:
    ev_day = calc._ev_reset_day(None)
    return (
        round(calc._daily_accumulators.get(f"{EV_CATEGORY}_{ev_day}", 0.0), 2),
        round(calc._daily_accumulators.get(
            f"{MIDNIGHT_EV_CATEGORY}_{day}", 0.0), 2),
    )


def _night(counter, boxes, *, values=None, **kw):
    """Three hours watched idle, then 11.18 kWh in one report while every
    box still reads 0 W. Returns the calculator and its rows, read on that
    night (the EV day is read off the clock)."""
    sensors = kw.get("sensors", ("links", "rechts"))
    values = {counter: 4321.0, **(values or {})}
    with freeze_time(NIGHT) as clock:
        calc = _watched(values, counter, boxes, **kw)
        for _ in range(180):
            _cycle(calc, clock, values, sensors=sensors)
        values[counter] += CATCH_UP
        _cycle(calc, clock, values, sensors=sensors)
        return calc, _rows(calc)


@pytest.mark.unit
class TestTheJumpIsNotBooked:
    @pytest.mark.parametrize("counter", [ED_EV, DAILY])
    def test_a_fleet_counter_over_two_boxes(self, counter):
        """THE issue: no box has a counter of its own, both read 0 W."""
        calc, rows = _night(counter, ("links", "rechts"))
        assert rows == (0.0, 0.0)
        assert calc.ev_counter_step_not_drawn(counter)[1] == pytest.approx(
            CATCH_UP, abs=0.01)

    @pytest.mark.parametrize("counter", [ED_EV, DAILY])
    def test_without_the_guard_it_is_booked(self, counter, monkeypatch):
        """Not vacuous: the same night with the judge off books the jump."""
        monkeypatch.setattr(EnergyCalculator, "_judge_ev_step",
                            lambda *a, **k: 0.0)
        _, rows = _night(counter, ("links", "rechts"))
        assert rows == (CATCH_UP, CATCH_UP)

    def test_a_counter_two_chargers_name(self):
        """Neither box owns it (#1049), so nothing judged it before."""
        _, rows = _night("sensor.shared_total", ("links", "rechts"))
        assert rows == (0.0, 0.0)

    def test_one_box_read_through_the_fleet_sensor(self):
        """Flat keys: no charger list, the Energy Dashboard's power row."""
        _, rows = _night(ED_EV, ("ev_charger",), chargers=(), sensors=())
        assert rows == (0.0, 0.0)

    def test_two_boxes_in_standby_are_not_charging(self):
        """Each box draws 300 W: under the 500 W floor box by box, though
        the two add up above it. Still idle, so the jump books nothing: the
        rows hold only the 600 W the power read saw (1.81 kWh)."""
        values = {ED_EV: 4321.0}
        with freeze_time(NIGHT) as clock:
            calc = _watched(values, ED_EV, ("links", "rechts"))
            for _ in range(180):
                _cycle(calc, clock, values, 300.0, 300.0)
            values[ED_EV] += CATCH_UP
            _cycle(calc, clock, values, 300.0, 300.0)
            assert _rows(calc) == pytest.approx((1.81, 1.81), abs=0.01)
            assert calc.ev_counter_step_not_drawn(ED_EV)[1] > 11.0

    def test_a_rise_not_checked_yet_is_held(self):
        """A reader of the counter before this cycle's check (a session's
        total) sees the last checked reading, as #1101 does for own ones."""
        values = {ED_EV: 4321.0}
        with freeze_time(NIGHT):
            calc = _watched(values, ED_EV, ("links", "rechts"))
            seen = values[ED_EV]
            assert calc.ev_counter_judged_kwh(ED_EV, seen + 11.0) == seen
            assert calc.ev_counter_judged_kwh(ED_EV, seen - 1.0) == seen - 1.0


@pytest.mark.unit
class TestChargingIsBooked:
    @pytest.mark.parametrize("box", ["links", "rechts"])
    def test_either_box_charging_is_booked(self, box):
        """100 minutes at 10.8 kW on one box, the counter in step with it."""
        values = {ED_EV: 4321.0}
        with freeze_time(NIGHT) as clock:
            calc = _watched(values, ED_EV, ("links", "rechts"))
            for _ in range(30):
                _cycle(calc, clock, values)
            for _ in range(100):
                values[ED_EV] += CHARGE_KWH
                _cycle(calc, clock, values, **{f"{box}_w": CHARGE_W})
            _cycle(calc, clock, values)
            assert _rows(calc) == pytest.approx((18.0, 18.0), abs=0.05)
            assert not calc._ev_counter_foreign

    def test_both_boxes_charging_at_once_are_booked(self):
        """Both at 10.8 kW, a cloud counter that reports every 10 minutes."""
        values = {ED_EV: 4321.0}
        with freeze_time(NIGHT) as clock:
            calc = _watched(values, ED_EV, ("links", "rechts"))
            for minute in range(1, 61):
                if minute % 10 == 0:
                    values[ED_EV] += 20 * CHARGE_KWH
                _cycle(calc, clock, values, CHARGE_W, CHARGE_W)
            _cycle(calc, clock, values)
            assert _rows(calc) == pytest.approx((21.6, 21.6), abs=0.05)

    def test_two_dark_boxes_leave_twice_the_room(self):
        """Ten minutes with both power reads dark: two boxes could have
        drawn 14.7 kWh in that time, one alone only half of it."""
        values = {ED_EV: 4321.0}
        with freeze_time(NIGHT) as clock:
            calc = _watched(values, ED_EV, ("links", "rechts"))
            for _ in range(130):
                _cycle(calc, clock, values)
            values[LINKS_W] = values[RECHTS_W] = "unavailable"
            for _ in range(10):
                _cycle(calc, clock, values)
            values[ED_EV] += 14.7
            _cycle(calc, clock, values)
            assert _rows(calc) == pytest.approx((14.7, 14.7), abs=0.01)

    def test_one_dark_box_leaves_one_box_of_room(self):
        """Not vacuous for the test above: one box's room refuses it."""
        values = {ED_EV: 4321.0}
        with freeze_time(NIGHT) as clock:
            calc = _calc(values, ED_EV, ("links",), chargers=("links",),
                         sensors=("links",))
            for bump in (0.0, 0.002):
                values[ED_EV] += bump
                _read(calc, values, sensors=("links",))
            for _ in range(130):
                _cycle(calc, clock, values, sensors=("links",))
            values[LINKS_W] = "unavailable"
            for _ in range(10):
                _cycle(calc, clock, values, sensors=("links",))
            values[ED_EV] += 14.7
            _cycle(calc, clock, values, sensors=("links",))
            assert _rows(calc) == (0.0, 0.0)

    def test_a_box_without_its_own_power_read_is_not_watched(self):
        """Rechts has no power sensor: SEM cannot see it idle, so the jump
        may be its charging and is booked as it stands."""
        _, rows = _night(ED_EV, ("links", "rechts"), sensors=("links",))
        assert rows == (CATCH_UP, CATCH_UP)

    def test_a_dark_box_is_not_watched(self):
        """Rechts's power sensor is unavailable all night: booked."""
        _, rows = _night(ED_EV, ("links", "rechts"),
                      values={RECHTS_W: "unavailable"})
        assert rows == (CATCH_UP, CATCH_UP)

    def test_two_boxes_on_one_fleet_sensor_are_not_watched(self):
        """No box has a power sensor of its own. The fleet sensor may read
        one box only (the Energy Dashboard's first EV device): it cannot
        say the other is idle, so the jump is booked as it stands."""
        _, rows = _night(ED_EV, ("links", "rechts"), sensors=())
        assert rows == (CATCH_UP, CATCH_UP)

    def test_the_fleet_sensor_stands_in_only_for_one_box(self):
        """…and only when that counter is the one SEM tracks — as before
        #1157."""
        values = {FLEET_W: 0.0}
        fleet = SimpleNamespace(ev_power_fleet_entity=FLEET_W)
        calc = _calc(values, ED_EV, ("links",), chargers=("links",),
                     sensors=())
        assert calc._ev_box_power_live(("links",), fleet, {})
        calc = _calc(values, ED_EV, ("links", "rechts"), sensors=())
        assert not calc._ev_box_power_live(("links", "rechts"), fleet, {})
        calc.configure_ev_counters(
            calc._hass, [ED_EV, "sensor.other"], True, complete=False,
            owners={}, boxes={ED_EV: ["links"], "sensor.other": ["rechts"]})
        assert not calc._ev_box_power_live(("links",), fleet, {})

    def test_a_box_reading_below_zero_does_not_cancel_another(self):
        """Links reads 400 W, under the floor: one phase of three may be
        drawing, so three hours leave 3.6 kWh of room. Rechts's sensor
        reads −400 W (an offset). It does not take that room away."""
        values = {ED_EV: 4321.0}
        with freeze_time(NIGHT) as clock:
            calc = _watched(values, ED_EV, ("links", "rechts"))
            for _ in range(180):
                _cycle(calc, clock, values, 400.0, -400.0)
            values[ED_EV] += 3.0
            _cycle(calc, clock, values, 400.0, -400.0)
            assert not calc._ev_counter_foreign
            assert _rows(calc)[1] == pytest.approx(3.0, abs=0.01)
    def test_the_fleet_sensor_still_stands_in_for_the_one_own_counter(self):
        """#1101's rule holds beside a counter two chargers name: the one box
        with a counter of its own is read through the fleet sensor."""
        values = {FLEET_W: 0.0}
        calc = _calc(values, "sensor.a", ("a",), sensors=())
        calc.configure_ev_counters(
            calc._hass, ["sensor.a", "sensor.bc"], True, complete=False,
            owners={"sensor.a": "a"}, boxes={"sensor.bc": ["b", "c"]})
        fleet = SimpleNamespace(ev_power_fleet_entity=FLEET_W)
        assert calc._ev_box_power_live(("a",), fleet, {})
        assert not calc._ev_box_power_live(("b", "c"), fleet, {})


@pytest.mark.unit
class TestWhatIsOwed:
    def test_another_box_s_draw_does_not_eat_the_next_move(self):
        """The Energy Dashboard counter counts its first EV device, Links.
        Rechts charges before midnight, so the counter does not move. That
        draw is not owed by the counter: when it reports what Links drew
        while SEM was down, the new day books it. (#1159) SEM could not tie
        the counter to Links, so it gave it the whole fleet as a guess."""
        values = {ED_EV: 4321.0}
        with freeze_time("2026-10-11 21:00:30") as clock:
            calc = _watched(values, ED_EV, ("links", "rechts"),
                            guessed=[ED_EV])
            for _ in range(60):
                _cycle(calc, clock, values)
            for _ in range(119):
                _cycle(calc, clock, values, 0.0, 11_000.0)
            for _ in range(3):
                _cycle(calc, clock, values)
            assert calc._ev_counter_pending[ED_EV] > 20.0, "vacuous"
            assert not calc._midnight_ev_baselines.get("owed")
            clock.tick(timedelta(hours=2))
            values[ED_EV] += 22.0
            _cycle(calc, clock, values)
            _cycle(calc, clock, values)
            assert _rows(calc, "2026-10-12")[1] == pytest.approx(22.0, abs=0.01)


    def test_one_box_s_late_report_is_owed_not_doubled(self):
        """One box, the Energy Dashboard counter: it is that box's. Links
        charges an hour before midnight and the counter reports it at 00:30.
        The old day holds it from the power read; the new day books none."""
        values = {ED_EV: 4321.0}
        with freeze_time("2026-10-11 22:00:30") as clock:
            calc = _calc(values, ED_EV, ("links",), chargers=("links",),
                         sensors=("links",))
            for bump in (0.0, 0.002):
                values[ED_EV] += bump
                _read(calc, values, sensors=("links",))
            for _ in range(59):
                _cycle(calc, clock, values, sensors=("links",))
            for _ in range(60):
                _cycle(calc, clock, values, 11_000.0, sensors=("links",))
            for _ in range(30):
                _cycle(calc, clock, values, sensors=("links",))
            values[ED_EV] += 11.0
            _cycle(calc, clock, values, sensors=("links",))
            _cycle(calc, clock, values, sensors=("links",))
            assert calc._midnight_ev_baselines["owed"] == {}  # paid
            assert _rows(calc, "2026-10-12")[1] == pytest.approx(0.0, abs=0.2)


@pytest.mark.unit
class TestOwnCountersAreUnchanged:
    def test_owners_alone_still_give_one_box_each(self):
        """A caller that names only owners (the tests of #1049/#1101) gets
        one box per counter, and a counter with neither is not judged."""
        calc = EnergyCalculator({"update_interval": 60}, TimeManager(Mock()))
        calc.configure_ev_counters(_hass({}), ["a", "b"], True,
                                   complete=False, owners={"a": "links"})
        assert calc._ev_counter_boxes == {"a": ("links",)}

    def test_boxes_outside_the_counter_set_are_dropped(self):
        calc = EnergyCalculator({"update_interval": 60}, TimeManager(Mock()))
        calc.configure_ev_counters(
            _hass({}), ["a"], True, complete=False, owners={},
            boxes={"a": ["x", "", None], "gone": ["y"]})
        assert calc._ev_counter_boxes == {"a": ("x",)}


@pytest.mark.unit
class TestEveryCounterGetsItsBoxes:
    """The coordinator names the boxes of every counter it hands over."""

    @staticmethod
    def _ns(config, ed_ev=None):
        ns = SimpleNamespace(
            config=config,
            _energy_dashboard_config=SimpleNamespace(ev_energy=ed_ev))
        ns._collect_ev_counter_entities = (
            lambda: SEMCoordinator._collect_ev_counter_entities(ns))
        ns._own_ev_counters = lambda: SEMCoordinator._own_ev_counters(ns)
        return ns

    CONFIGS = {
        "own": ({"ev_chargers": [
            {"id": "links", "ev_total_energy_sensor": "sensor.l"},
            {"id": "rechts", "ev_total_energy_sensor": "sensor.r"}]}, None,
            {"sensor.l": ["links"], "sensor.r": ["rechts"]}),
        "partial": ({"ev_chargers": [
            {"id": "links", "ev_total_energy_sensor": "sensor.l"},
            {"id": "rechts"}]}, ED_EV, {"sensor.l": ["links"]}),
        "shared": ({"ev_chargers": [
            {"id": "links", "ev_total_energy_sensor": "sensor.s"},
            {"id": "rechts", "ev_total_energy_sensor": "sensor.s"}]}, None,
            {"sensor.s": ["links", "rechts"]}),
        "top level for the primary": ({
            "ev_total_energy_sensor": "sensor.t",
            "ev_chargers": [{"id": "links"}, {"id": "rechts"}]}, None,
            {"sensor.t": ["links"]}),
        "flat keys": ({"ev_total_energy_sensor": "sensor.t"}, None,
                      {"sensor.t": ["ev_charger"]}),
        "legacy daily": ({"ev_daily_energy_sensor": DAILY,
                          "ev_chargers": [{"id": "links"}, {"id": "rechts"}]},
                         ED_EV, {DAILY: ["links", "rechts"]}),
        "energy dashboard": ({"ev_chargers": [{"id": "links"}, {}]}, ED_EV,
                             {ED_EV: ["links", "ev_charger_1"]}),
        "energy dashboard, flat keys": ({}, ED_EV, {ED_EV: ["ev_charger"]}),
        "nothing": ({"ev_chargers": [{"id": "links"}]}, None, {}),
    }

    @pytest.mark.parametrize("name", list(CONFIGS))
    def test_the_boxes(self, name):
        config, ed_ev, expected = self.CONFIGS[name]
        ns = self._ns(config, ed_ev)
        boxes = SEMCoordinator._ev_counter_boxes(ns)
        assert boxes == expected
        # Every counter handed over has its boxes.
        assert set(boxes) == set(SEMCoordinator._collect_ev_counter_entities(ns))
        # A counter of its own counts its owner's box alone.
        for counter, owner in SEMCoordinator._ev_counter_owners(ns).items():
            assert boxes[counter] == [owner]

    def test_every_counter_setup_hands_over_the_boxes(self):
        sites = call_sites("configure_ev_counters")
        assert sites, "nothing configures the EV counters"
        for _, _, kw in sites:
            assert "boxes" in kw, sites
