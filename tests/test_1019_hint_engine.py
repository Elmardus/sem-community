"""#1019 — the hint engine, with no Home Assistant in it.

Time is SEM's own: ``facts.night`` is ``time_manager.is_night_mode()``,
and the engine acts on its EDGES — night start, morning — never on a
wall-clock hour. That is what the compressed-sun simulation on .175 moves.
Each hint fires once per event key; a restart with the stored state fires
nothing again.
"""
from datetime import datetime, timedelta, timezone

from custom_components.solar_energy_management.coordinator.hints import (
    GRID_RISE_FACTOR, HINT_CATEGORIES, NIGHT_LOAD_FACTOR, NIGHT_LOAD_MIN_W,
    SILENT_INPUT_S, HintEngine, HintFacts,
)

TZ = timezone(timedelta(hours=2))
MONDAY = datetime(2026, 9, 28, 12, 0, tzinfo=TZ)   # a Monday


def _facts(now, night, enabled=None, **kw):
    base = dict(
        now=now, night=night,
        enabled={c: True for c in HINT_CATEGORIES} if enabled is None else enabled,
        home_w=200.0, daily_solar_kwh=20.0, daily_import_kwh=4.0, daily_home_kwh=12.0,
        daily_ev_kwh=0.0, daily_cost=1.0, self_use_pct=70.0, currency="CHF",
        dark_inputs={}, price_cheap=False, dynamic_tariff=False, idle_plugged_cars=(),
    )
    base.update(kw)
    return HintFacts(**base)


def _run_days(engine, days, *, night_w=200.0, day_import=4.0, day_solar=20.0,
              start=MONDAY, fire_on=None):
    """Drive whole SEM days: day (12:00) → night start (21:00, three
    samples) → morning (07:00 next day). Returns every hint fired."""
    fired = []
    now = start
    for i in range(days):
        kw = fire_on(i) if fire_on else {}
        fired += engine.evaluate(_facts(now, False, **kw))
        night = now.replace(hour=21)
        for k in range(3):
            fired += engine.evaluate(_facts(night + timedelta(minutes=10 * k), True,
                                            home_w=night_w, **kw))
        morning = (now + timedelta(days=1)).replace(hour=7)
        fired += engine.evaluate(_facts(morning, False, **kw))
        now = now + timedelta(days=1)
    return fired


class TestConstants:
    def test_the_agreed_numbers(self):
        assert SILENT_INPUT_S == 900
        assert NIGHT_LOAD_FACTOR == 1.5
        assert NIGHT_LOAD_MIN_W == 300
        assert GRID_RISE_FACTOR == 1.5


class TestNightLoad:
    def test_fires_at_the_morning_edge_when_the_night_was_heavy(self):
        e = HintEngine()
        quiet = _run_days(e, 4)
        assert [h.category for h in quiet] == []
        heavy = _run_days(e, 1, night_w=800.0, start=MONDAY + timedelta(days=4))
        assert [h.category for h in heavy] == ["night_load"]
        h = heavy[0]
        assert h.params["watts"] == 800
        assert h.params["usual"] == 200
        assert h.text_key == "hint_msg_night_load"

    def test_needs_three_past_nights(self):
        e = HintEngine()
        assert _run_days(e, 2, night_w=900.0) == []

    def test_a_small_house_never_hints(self):
        e = HintEngine()
        _run_days(e, 4, night_w=100.0)
        assert _run_days(e, 1, night_w=250.0, start=MONDAY + timedelta(days=4)) == []

    def test_nothing_when_the_switch_is_off(self):
        e = HintEngine()
        off = {c: c != "night_load" for c in HINT_CATEGORIES}
        _run_days(e, 4)
        fired = []
        now = MONDAY + timedelta(days=4)
        fired += e.evaluate(_facts(now, False, enabled=off))
        for k in range(3):
            fired += e.evaluate(_facts(now.replace(hour=21) + timedelta(minutes=k), True,
                                       enabled=off, home_w=900.0))
        fired += e.evaluate(_facts((now + timedelta(days=1)).replace(hour=7), False, enabled=off))
        assert fired == []


class TestGridRise:
    def test_fires_at_night_start_when_import_jumped_under_the_same_sun(self):
        e = HintEngine()
        _run_days(e, 4)
        fired = _run_days(e, 1, day_import=9.0, start=MONDAY + timedelta(days=4),
                          fire_on=lambda i: {"daily_import_kwh": 9.0})
        assert [h.category for h in fired] == ["grid_rise"]
        assert fired[0].params["kwh"] == 9.0
        assert fired[0].params["usual"] == 4.0

    def test_silent_when_the_sun_was_different(self):
        e = HintEngine()
        _run_days(e, 4)
        fired = _run_days(e, 1, start=MONDAY + timedelta(days=4),
                          fire_on=lambda i: {"daily_import_kwh": 9.0, "daily_solar_kwh": 5.0})
        assert fired == []


class TestWeekly:
    def test_fires_at_the_first_night_start_on_a_sunday(self):
        e = HintEngine()
        fired = _run_days(e, 7, fire_on=lambda i: {"daily_ev_kwh": 2.0})
        weekly = [h for h in fired if h.category == "weekly_summary"]
        assert len(weekly) == 1
        p = weekly[0].params
        assert p["solar"] == 140          # 7 × 20
        assert p["grid"] == 28            # 7 × 4
        assert p["ev"] == 14
        assert p["self_use"] == 70
        assert p["currency"] == "CHF"
        assert weekly[0].key == "2026-W40"

    def test_only_once_per_week_even_across_a_restart(self):
        e = HintEngine()
        fired = _run_days(e, 7)
        assert len([h for h in fired if h.category == "weekly_summary"]) == 1
        e2 = HintEngine(e.to_dict())
        sunday = MONDAY + timedelta(days=6)
        again = e2.evaluate(_facts(sunday.replace(hour=21, minute=30), True))
        assert again == []


class TestSilentInput:
    def test_one_message_after_fifteen_minutes_and_one_when_back(self):
        e = HintEngine()
        now = MONDAY
        dark = {"sensor.solar": ("Solar power", 600.0)}
        assert e.evaluate(_facts(now, False, dark_inputs=dark)) == []
        dark = {"sensor.solar": ("Solar power", 1020.0)}
        fired = e.evaluate(_facts(now + timedelta(minutes=7), False, dark_inputs=dark))
        assert [h.text_key for h in fired] == ["hint_msg_silent_input"]
        assert fired[0].params == {"name": "Solar power", "minutes": 17}
        dark = {"sensor.solar": ("Solar power", 1080.0)}
        assert e.evaluate(_facts(now + timedelta(minutes=8), False, dark_inputs=dark)) == []
        back = e.evaluate(_facts(now + timedelta(minutes=9), False, dark_inputs={}))
        assert [h.text_key for h in back] == ["hint_msg_silent_input_back"]
        assert back[0].params == {"name": "Solar power"}

    def test_a_restart_does_not_repeat_the_open_outage(self):
        e = HintEngine()
        dark = {"sensor.solar": ("Solar power", 1000.0)}
        e.evaluate(_facts(MONDAY, False, dark_inputs=dark))
        e2 = HintEngine(e.to_dict())
        dark = {"sensor.solar": ("Solar power", 1030.0)}
        assert e2.evaluate(_facts(MONDAY + timedelta(seconds=30), False, dark_inputs=dark)) == []


class TestCheapNow:
    def test_once_per_cheap_window_with_a_car_waiting(self):
        e = HintEngine()
        now = MONDAY
        kw = dict(dynamic_tariff=True, price_cheap=True, idle_plugged_cars=("Garage",))
        fired = e.evaluate(_facts(now, False, **kw))
        assert [h.category for h in fired] == ["cheap_now"]
        assert e.evaluate(_facts(now + timedelta(minutes=5), False, **kw)) == []
        e.evaluate(_facts(now + timedelta(hours=1), False, dynamic_tariff=True))
        again = e.evaluate(_facts(now + timedelta(hours=2), False, **kw))
        assert [h.category for h in again] == ["cheap_now"]

    def test_never_on_a_flat_tariff(self):
        e = HintEngine()
        assert e.evaluate(_facts(MONDAY, False, dynamic_tariff=False, price_cheap=True,
                                 idle_plugged_cars=("Garage",))) == []

    def test_nothing_without_a_waiting_car(self):
        e = HintEngine()
        assert e.evaluate(_facts(MONDAY, False, dynamic_tariff=True, price_cheap=True)) == []


class TestState:
    def test_round_trips_and_bounds(self):
        e = HintEngine()
        _run_days(e, 20)
        d = e.to_dict()
        assert len(d["night_means"]) <= 14
        assert len(d["daily_totals"]) <= 14
        assert HintEngine(d).to_dict() == d

    def test_a_broken_stored_state_is_a_fresh_engine(self):
        assert HintEngine({"night_means": "junk", "sent": 3}).to_dict() == HintEngine().to_dict()


class TestStorage:
    def test_the_state_round_trips_through_the_energy_store(self):
        from unittest.mock import AsyncMock, MagicMock, patch
        from custom_components.solar_energy_management.coordinator.storage import SEMStorage
        hass = MagicMock()
        hass.config.config_dir = "/config"
        energy_store = MagicMock()
        energy_store.async_load = AsyncMock(return_value=None)
        with patch(
            "custom_components.solar_energy_management.coordinator.storage.Store"
        ) as mock_store:
            mock_store.side_effect = [energy_store, MagicMock()]
            s = SEMStorage(hass, "entry")
        assert s.get_hints_state() == {}
        e = HintEngine()
        _run_days(e, 3)
        s.set_hints_state(e.to_dict())
        assert HintEngine(s.get_hints_state()).to_dict() == e.to_dict()
        s._energy_data["hints"] = "junk"
        assert s.get_hints_state() == {}
