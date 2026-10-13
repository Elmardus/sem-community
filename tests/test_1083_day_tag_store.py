"""#1083 — day tags stored nightly, asleep until the soak flag is on.

Nothing here guesses: a recorder that cannot answer stores NOTHING (a
"could not ask" is not a grey day, #925), and the flag off stores nothing.
"""
import asyncio
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

from custom_components.solar_energy_management.coordinator.day_tag_store import (
    KEY_COUNTER,
    KEY_PRICE,
    KEY_SOLAR,
    DayTagger,
    longest_dark_min,
    sample_series,
)
from custom_components.solar_energy_management.coordinator.storage import (
    DAY_TAGS_MAX,
    SEMStorage,
)

DAY = date(2026, 10, 12)
START = datetime(2026, 10, 12, tzinfo=timezone.utc)


def _storage():
    st = SEMStorage.__new__(SEMStorage)
    st._daily_data = {}
    st._energy_data = {}

    async def _save():
        st.saved = True
    st.async_save_daily = _save
    return st


def _state(t, v):
    return SimpleNamespace(last_changed=t, state=str(v))


def _tagger(states_by_key, flag=True, storage=None, history=()):
    coord = SimpleNamespace(
        config={"day_tags": flag, "cheap_price_threshold": 0.10},
        _forecast_tracker=SimpleNamespace(_history=list(history)),
    )
    st = storage or _storage()
    st.get_session_history = lambda: []

    async def _read(_hass, eid, _days):
        return states_by_key.get(eid.removeprefix("sensor.sem_"))

    tg = DayTagger(SimpleNamespace(), coord, storage=st, read_states=_read)
    tg._entity_id = lambda key: f"sensor.sem_{key}"
    return tg, st


def test_storage_round_trip_bound_and_same_day_replaces():
    st = _storage()
    st.put_day_tag({"date": "2026-10-08", "tags": ["grey"]})
    st.put_day_tag({"date": "2026-10-08", "tags": ["mixed"]})
    assert st.get_day_tags() == [{"date": "2026-10-08", "tags": ["mixed"]}]
    for i in range(DAY_TAGS_MAX + 20):
        st.put_day_tag({"date": (date(2025, 1, 1) + timedelta(days=i)).isoformat()})
    tags = st.get_day_tags()
    assert len(tags) == DAY_TAGS_MAX
    assert tags == sorted(tags, key=lambda t: t["date"])


def test_starts_are_remembered_per_day():
    st = _storage()
    st.note_start("2026-10-12")
    assert st.started_on("2026-10-12")
    assert not st.started_on("2026-10-11")


def test_sampling_keeps_dark_readings_dark():
    s = sample_series([(START + timedelta(hours=9), "800"),
                       (START + timedelta(hours=11), "unavailable"),
                       (START + timedelta(hours=11, minutes=30), "900")], START)
    by_h = dict(s)
    assert by_h[8.0] is None            # nothing recorded yet
    assert by_h[10.0] == 800.0          # sticky between changes
    assert by_h[11.25] is None          # dark, not 0 W
    assert by_h[12.0] == 900.0
    assert longest_dark_min(s, (8.0, 17.0)) >= 30


def test_flag_off_stores_nothing():
    tg, st = _tagger({}, flag=False)
    assert asyncio.run(tg.async_tag_day(DAY)) is None
    assert st.get_day_tags() == []


def test_recorder_that_cannot_answer_stores_nothing():
    tg, st = _tagger({KEY_SOLAR: None})
    assert asyncio.run(tg.async_tag_day(DAY)) is None
    assert st.get_day_tags() == []


def test_prod_12_10_frozen_counter_is_stored_as_a_gap():
    rec = SimpleNamespace(date="2026-10-12", actual_kwh=0.0, forecast_kwh=15.0)
    prior = [SimpleNamespace(date=f"2026-10-0{d}", actual_kwh=20.0 + d,
                             forecast_kwh=20.0) for d in range(1, 9)]
    states = {
        KEY_SOLAR: [_state(START, "0")],
        "grid_power": [_state(START, "-600")],
        "battery_power": [_state(START, "0")],
        KEY_COUNTER: [_state(START, "23832.86")],
        KEY_PRICE: [_state(START, "0.36")],
    }
    tg, st = _tagger(states, history=prior + [rec])
    out = asyncio.run(tg.async_tag_day(DAY))
    assert out is not None and "gap" in out["tags"]
    assert st.get_day_tags()[-1]["date"] == "2026-10-12"
    assert getattr(st, "saved", False)
