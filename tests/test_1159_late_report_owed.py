"""#1159 — a late counter report is booked once, on the day it belongs to.

A car charges before the day starts and the charger's counter reports it
later. SEM keeps that energy as ``owed`` (#1049): the counter's next moves
pay it first, so the new day does not book it again. Two cases still went
wrong (found while fixing #1157):

1. ``owed`` never ran out. A counter that showed part of the session, then
   the rest in one jump after the late-report wait, had that jump judged as
   energy from outside the window (#1101) — not booked, and not paid. What
   was owed then took the next real charge that day.
2. A counter of several chargers got nothing owed, so its late report was
   booked on both days and home read that much too low.

The rules now: what a row owes a counter is never more than what SEM saw
drawn that the counter has not shown yet (``pending`` or ``unreported``).
A counter is owed whenever SEM knows the boxes it counts and reads each of
them: a counter two chargers name, or a fallback (Energy Dashboard, legacy
daily sensor) SEM can tie to its one charger. A fallback it cannot tie is
given the whole fleet as a guess, and owes nothing.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from freezegun import freeze_time

from custom_components.solar_energy_management.coordinator.coordinator import (
    SEMCoordinator,
)
from custom_components.solar_energy_management.coordinator.energy_calculator import (
    EnergyCalculator,
)

from . import test_1157_shared_ev_counter as shared
from .ast_contracts import call_sites
from .test_1101_ev_counter_catch_up import (
    LINKS,
    LINKS_W,
    RECHTS_W,
    _charge,
    _cycle,
    _idle,
    _night,
    _rows,
)

NEW_DAY = "2026-10-12"
SHARED = "sensor.shared_total"
ED_EV = shared.ED_EV
DAILY = shared.DAILY


def _late_session(calc, clock, values):
    """1.8 kWh drawn just before the day starts; the counter shows none."""
    _cycle(calc, clock, values)
    _charge(calc, clock, values, 10, meter=0.0)
    _idle(calc, clock, values, 12)               # past the day's start


@pytest.mark.unit
class TestOwedRunsOut:
    def test_a_jump_judged_foreign_does_not_leave_owed_to_eat_a_charge(self):
        """THE issue's case 2. The counter shows 0.6 at 00:10 and the rest
        (1.2) in one jump at 03:00, long after the wait: judged foreign, not
        booked. The 08:00 session (100 min at 10.8 kW, counter in step) is
        the new day's in full. Before: 16.8 — owed took 1.2 of it."""
        with freeze_time("2026-10-11 23:48:00") as clock:
            values, calc = _night()
            _late_session(calc, clock, values)
            assert calc._midnight_ev_baselines["owed"], "vacuous: none owed"
            values[LINKS] += 0.6
            _cycle(calc, clock, values)
            _idle(calc, clock, values, 170)
            values[LINKS] += 1.2
            _cycle(calc, clock, values)
            assert calc._ev_counter_foreign, "vacuous: the jump was booked"
            _idle(calc, clock, values, 300)
            _charge(calc, clock, values, 100)
            _idle(calc, clock, values, 2)
            assert _rows(calc, NEW_DAY)[1] == pytest.approx(18.0, abs=0.05)

    def test_a_rest_never_reported_does_not_eat_a_charge(self):
        """A power read above the meter: the counter shows 1.2 of 1.8 and
        never the rest. After the wait nothing is owed any more."""
        with freeze_time("2026-10-11 23:48:00") as clock:
            values, calc = _night()
            _late_session(calc, clock, values)
            values[LINKS] += 1.2
            _cycle(calc, clock, values)
            assert calc._midnight_ev_baselines["owed"], "vacuous: all paid"
            _idle(calc, clock, values, 300)
            assert not calc._midnight_ev_baselines["owed"]
            _charge(calc, clock, values, 100)
            _idle(calc, clock, values, 2)
            assert _rows(calc, NEW_DAY)[1] == pytest.approx(18.0, abs=0.05)

    def test_a_report_hours_late_is_still_paid(self):
        """The counter has not moved since the session, so a report three
        hours later is still booked by the step check — and still owed. A
        rule that let owed run out with the wait alone would book it twice."""
        with freeze_time("2026-10-11 23:48:00") as clock:
            values, calc = _night()
            _late_session(calc, clock, values)
            _idle(calc, clock, values, 180)
            assert LINKS not in calc._ev_counter_unreported, "vacuous: no wait"
            values[LINKS] += 1.8
            _cycle(calc, clock, values)
            assert not calc._ev_counter_foreign
            assert _rows(calc, NEW_DAY)[1] == pytest.approx(0.0, abs=0.01)
            assert not calc._midnight_ev_baselines["owed"]  # paid

    def test_a_report_in_two_steps_inside_the_wait_is_paid(self):
        with freeze_time("2026-10-11 23:48:00") as clock:
            values, calc = _night()
            _late_session(calc, clock, values)
            values[LINKS] += 0.6
            _cycle(calc, clock, values)
            _idle(calc, clock, values, 30)
            values[LINKS] += 1.2
            _cycle(calc, clock, values)
            assert _rows(calc, NEW_DAY)[1] == pytest.approx(0.0, abs=0.01)

    def test_every_row_lets_go_at_the_charge_by_time_too(self):
        """The EV-day row and the charger's own row roll at Charge-by
        (11:00), not at midnight: the same rule holds for them."""
        with freeze_time("2026-10-12 10:48:00") as clock:
            values, calc = _night()
            _late_session(calc, clock, values)
            charger_bl = calc._charger_counter_baselines["links"]
            assert calc._ev_counter_baselines["owed"], "vacuous: none owed"
            assert charger_bl["owed"], "vacuous: the charger row owes none"
            values[LINKS] += 0.6
            _cycle(calc, clock, values)
            _idle(calc, clock, values, 300)
            assert not calc._ev_counter_baselines["owed"]
            assert not charger_bl["owed"]
            charger_before = calc.charger_rows["links"]
            row_before = _rows(calc)[0]
            _charge(calc, clock, values, 100)
            _idle(calc, clock, values, 2)
            assert _rows(calc)[0] - row_before == pytest.approx(18.0, abs=0.05)
            assert calc.charger_rows["links"] - charger_before == (
                pytest.approx(18.0, abs=0.05))

    def test_without_the_trim_the_charge_is_eaten(self, monkeypatch):
        """Not vacuous: the first test with the trim switched off."""
        monkeypatch.setattr(EnergyCalculator, "_trim_ev_owed",
                            lambda *a, **k: None)
        with freeze_time("2026-10-11 23:48:00") as clock:
            values, calc = _night()
            _late_session(calc, clock, values)
            values[LINKS] += 0.6
            _cycle(calc, clock, values)
            _idle(calc, clock, values, 170)
            values[LINKS] += 1.2
            _cycle(calc, clock, values)
            _idle(calc, clock, values, 300)
            _charge(calc, clock, values, 100)
            _idle(calc, clock, values, 2)
            assert _rows(calc, NEW_DAY)[1] == pytest.approx(16.8, abs=0.05)

    def test_the_trim_holds_owed_to_what_is_left(self):
        calc = EnergyCalculator({"update_interval": 60}, None)
        calc._midnight_ev_baselines["owed"] = {LINKS: 1.8}
        calc._ev_counter_baselines["owed"] = {LINKS: 0.3}
        calc._trim_ev_owed(LINKS, 0.5)
        assert calc._midnight_ev_baselines["owed"] == {LINKS: 0.5}
        assert calc._ev_counter_baselines["owed"] == {LINKS: 0.3}
        calc._trim_ev_owed(LINKS, 0.0)
        assert calc._midnight_ev_baselines["owed"] == {}
        assert calc._ev_counter_baselines["owed"] == {}

    def test_a_counter_that_has_not_moved_owes_it_all(self):
        """No watch record, and the wait has dropped ``unreported``: what the
        box drew since the counter last moved is still owed in full."""
        with freeze_time("2026-10-11 23:48:00") as clock:
            values, calc = _night()
            _late_session(calc, clock, values)
            calc._ev_counter_room.pop(LINKS)
            calc._ev_counter_unreported.pop(LINKS, None)
            owed = dict(calc._midnight_ev_baselines["owed"])
            assert owed, "vacuous: none owed"
            _idle(calc, clock, values, 200)
            assert calc._midnight_ev_baselines["owed"] == owed

    @pytest.mark.parametrize("bad", ["x", None, float("nan"), True])
    def test_a_damaged_owed_value_does_not_raise(self, bad):
        with freeze_time("2026-10-11 23:48:00") as clock:
            values, calc = _night()
            _late_session(calc, clock, values)
            calc._midnight_ev_baselines["owed"][LINKS] = bad
            _cycle(calc, clock, values)  # the anchor check drops it


def _shared_night(counter, boxes, *, report=11.0, rechts_w=0.0, **kw):
    """Links (and Rechts, if asked) charge an hour before midnight; the
    counter reports ``report`` kWh at 00:30. Returns the calculator and the
    new day's rows."""
    sensors = kw.get("sensors", ("links", "rechts"))
    values = {counter: 4321.0}
    with freeze_time("2026-10-11 22:58:30") as clock:
        calc = shared._watched(values, counter, boxes, **kw)
        for _ in range(60):
            shared._cycle(calc, clock, values, 11_000.0, rechts_w,
                          sensors=sensors)
        for _ in range(32):
            shared._cycle(calc, clock, values, sensors=sensors)
        values[counter] += report
        shared._cycle(calc, clock, values, sensors=sensors)
        shared._cycle(calc, clock, values, sensors=sensors)
        return calc, shared._rows(calc, NEW_DAY)


@pytest.mark.unit
class TestASharedCounterIsOwed:
    def test_a_counter_two_chargers_name_is_owed_its_late_report(self):
        """THE issue's case 1. Both chargers name one counter; it reports
        Links's evening session at 00:30. The old day holds it from the power
        read, the new day books none of it. Before: 11.0."""
        _, rows = _shared_night(SHARED, ("links", "rechts"))
        assert rows[1] == pytest.approx(0.0, abs=0.2)

    def test_both_boxes_late_are_owed(self):
        _, rows = _shared_night(SHARED, ("links", "rechts"), report=22.0,
                                rechts_w=11_000.0)
        assert rows[1] == pytest.approx(0.0, abs=0.4)

    @pytest.mark.parametrize("counter", [ED_EV, DAILY])
    def test_a_fallback_tied_to_links_is_owed(self, counter):
        """The coordinator tied the fallback to Links: one box."""
        _, rows = _shared_night(counter, ("links",))
        assert rows[1] == pytest.approx(0.0, abs=0.2)

    def test_a_tied_fallback_owes_only_its_own_box(self):
        """Rechts charges too, but the counter is Links's: only Links's
        11 kWh were owed, and its report paid them."""
        calc, rows = _shared_night(ED_EV, ("links",), rechts_w=11_000.0)
        assert calc._ev_counter_pending[ED_EV] == 0.0
        assert not calc._midnight_ev_baselines["owed"]
        assert rows[1] == pytest.approx(0.0, abs=0.2)

    @pytest.mark.parametrize("counter", [ED_EV, DAILY])
    def test_a_guessed_fallback_owes_nothing(self, counter):
        """SEM could not tie it: the late report books as before #1159."""
        _, rows = _shared_night(counter, ("links", "rechts"),
                                guessed=[counter])
        assert rows[1] == pytest.approx(11.0, abs=0.2)

    def test_without_the_fix_it_is_booked_twice(self, monkeypatch):
        """Not vacuous: owed only for one box, as before."""
        monkeypatch.setattr(
            EnergyCalculator, "_ev_counter_owes",
            lambda self, e: len(self._ev_counter_boxes.get(e, ())) == 1)
        _, rows = _shared_night(SHARED, ("links", "rechts"))
        assert rows[1] == pytest.approx(11.0, abs=0.2)

    def test_several_boxes_must_each_read_their_own_power(self):
        """Rechts has no power sensor of its own: ``pending`` then holds the
        fleet's draw, which is not this counter's, so none of it is owed."""
        calc = shared._calc({SHARED: 1.0}, SHARED, ("links", "rechts"),
                            sensors=("links",))
        assert not calc._ev_counter_owes(SHARED)
        calc = shared._calc({SHARED: 1.0}, SHARED, ("links", "rechts"))
        assert calc._ev_counter_owes(SHARED)
        calc = shared._calc({SHARED: 1.0}, SHARED, ("links",),
                            chargers=("links",), sensors=())
        assert calc._ev_counter_owes(SHARED)  # one box: the fleet read
        calc = shared._calc({SHARED: 1.0}, SHARED, ("links", "rechts"),
                            guessed=[SHARED])
        assert not calc._ev_counter_owes(SHARED)
        assert not calc._ev_counter_owes("sensor.not_tracked")

    def test_guessed_outside_the_counter_set_is_dropped(self):
        calc = shared._calc({SHARED: 1.0}, SHARED, ("links", "rechts"),
                            guessed=[SHARED, "sensor.gone"])
        assert calc._ev_counter_guessed == {SHARED}


class _Registry:
    def __init__(self, devices):
        self._devices = devices

    def async_get(self, entity_id):
        device = self._devices.get(entity_id)
        return SimpleNamespace(device_id=device) if device else None


def _ns(config, *, ed_ev=None, ed_power=None, devices=None, monkeypatch=None):
    """A coordinator double, as #1157's test builds it, with an entity
    registry that knows ``devices`` (entity → device id)."""
    ns = SimpleNamespace(
        config=config, hass=object() if devices is not None else None,
        _energy_dashboard_config=SimpleNamespace(ev_energy=ed_ev,
                                                 ev_power=ed_power))
    ns._collect_ev_counter_entities = (
        lambda: SEMCoordinator._collect_ev_counter_entities(ns))
    ns._own_ev_counters = lambda: SEMCoordinator._own_ev_counters(ns)
    if devices is not None:
        from homeassistant.helpers import entity_registry as er
        monkeypatch.setattr(er, "async_get", lambda hass: _Registry(devices))
    return ns


def _two(links=None, rechts=None):
    return {"ev_chargers": [
        {"id": "links", "ev_charging_power_sensor": LINKS_W, **(links or {})},
        {"id": "rechts", "ev_charging_power_sensor": RECHTS_W,
         **(rechts or {})}]}


@pytest.mark.unit
class TestTheCoordinatorTiesAFallback:
    def test_by_the_power_sensor_beside_it(self):
        ns = _ns(_two(), ed_ev=ED_EV, ed_power=LINKS_W)
        assert SEMCoordinator._ev_counter_scope(ns) == ({ED_EV: ["links"]}, [])

    def test_by_its_device(self, monkeypatch):
        ns = _ns({**_two(), "ev_daily_energy_sensor": DAILY},
                 devices={DAILY: "dev_r", LINKS_W: "dev_l", RECHTS_W: "dev_r"},
                 monkeypatch=monkeypatch)
        assert SEMCoordinator._ev_counter_scope(ns) == ({DAILY: ["rechts"]}, [])

    def test_by_the_service_device(self, monkeypatch):
        ns = _ns(_two(rechts={"ev_service_device_id": "dev_r"}), ed_ev=ED_EV,
                 devices={ED_EV: "dev_r"}, monkeypatch=monkeypatch)
        assert SEMCoordinator._ev_counter_scope(ns) == ({ED_EV: ["rechts"]}, [])

    @pytest.mark.parametrize("name, config, ed_power, devices", [
        ("a power that is no charger's", _two(), "sensor.sum", {}),
        ("two chargers on one device", _two(), None,
         {ED_EV: "dev", LINKS_W: "dev", RECHTS_W: "dev"}),
        ("power and device disagree", _two(), LINKS_W,
         {ED_EV: "dev_r", RECHTS_W: "dev_r"}),
        ("the charger reads no power of its own",
         {"ev_chargers": [
             {"id": "links", "ev_charging_power_sensor": LINKS_W},
             {"id": "rechts", "ev_current_control_entity": "number.r"}]},
         None, {ED_EV: "dev_r", "number.r": "dev_r"}),
        ("no answer", _two(), None, {}),
    ])
    def test_not_tied_is_a_guess(self, name, config, ed_power, devices,
                                 monkeypatch):
        ns = _ns(config, ed_ev=ED_EV, ed_power=ed_power, devices=devices,
                 monkeypatch=monkeypatch)
        boxes, guessed = SEMCoordinator._ev_counter_scope(ns)
        assert boxes == {ED_EV: ["links", "rechts"]}, name
        assert guessed == [ED_EV], name

    @pytest.mark.parametrize("config, ed_ev, expected", [
        ({"ev_chargers": [{"id": "links"}]}, ED_EV, {ED_EV: ["links"]}),
        ({}, ED_EV, {ED_EV: ["ev_charger"]}),
        (_two({"ev_total_energy_sensor": SHARED},
              {"ev_total_energy_sensor": SHARED}), None,
         {SHARED: ["links", "rechts"]}),
        (_two({"ev_total_energy_sensor": "sensor.l"}), ED_EV,
         {"sensor.l": ["links"]}),
    ])
    def test_a_known_counter_is_no_guess(self, config, ed_ev, expected):
        """One box, a counter chargers name, a charger's own counter."""
        ns = _ns(config, ed_ev=ed_ev)
        assert SEMCoordinator._ev_counter_scope(ns) == (expected, [])

    def test_the_two_methods_read_one_answer(self):
        ns = _ns(_two(), ed_ev=ED_EV, ed_power="sensor.sum")
        boxes, guessed = SEMCoordinator._ev_counter_scope(ns)
        assert SEMCoordinator._ev_counter_boxes(ns) == boxes
        assert SEMCoordinator._ev_counters_guessed(ns) == guessed

    def test_no_registry_is_no_answer(self):
        from custom_components.solar_energy_management.coordinator.coordinator import (
            _entity_device_lookup,
        )
        assert _entity_device_lookup(None)("sensor.x") is None
        assert _entity_device_lookup(object())("sensor.x") is None

    def test_every_counter_setup_hands_over_the_guess(self):
        sites = call_sites("configure_ev_counters")
        assert sites, "nothing configures the EV counters"
        for _, _, kw in sites:
            assert "guessed" in kw, sites


@pytest.mark.unit
class TestABoxSEMDoesNotReadIsOwedNothing:
    def test_another_box_s_draw_is_not_owed(self):
        """Links has a counter but no power sensor; Rechts has a sensor.
        ``pending`` of Links's counter then grows with the fleet read —
        Rechts's draw. The old day never held Links's energy (no power
        read), so Links's report after midnight is the new day's."""
        values = {ED_EV: 4321.0}
        with freeze_time("2026-10-11 22:58:30") as clock:
            calc = shared._watched(values, ED_EV, ("links",),
                                   sensors=("rechts",))
            assert not calc._ev_counter_owes(ED_EV)
            for _ in range(60):
                shared._cycle(calc, clock, values, 0.0, 11_000.0,
                              sensors=("rechts",))
            for _ in range(32):
                shared._cycle(calc, clock, values, sensors=("rechts",))
            assert calc._ev_counter_pending[ED_EV] > 10.0, "vacuous"
            assert not calc._midnight_ev_baselines["owed"]
            values[ED_EV] += 5.0
            shared._cycle(calc, clock, values, sensors=("rechts",))
            shared._cycle(calc, clock, values, sensors=("rechts",))
            assert shared._rows(calc, NEW_DAY)[1] == pytest.approx(5.0, abs=0.01)

    def test_one_box_with_no_power_sensor_anywhere_keeps_the_stand_in(self):
        calc = shared._calc({ED_EV: 1.0}, ED_EV, ("links",),
                            sensors=())
        assert calc._ev_counter_owes(ED_EV)

    def test_the_tie_needs_an_id(self):
        ns = _ns({"ev_chargers": [
            {"id": "links", "ev_charging_power_sensor": LINKS_W},
            {"ev_charging_power_sensor": RECHTS_W}]},
            ed_ev=ED_EV, ed_power=RECHTS_W)
        boxes, guessed = SEMCoordinator._ev_counter_scope(ns)
        assert boxes == {ED_EV: ["links", "ev_charger_1"]}
        assert guessed == [ED_EV]
