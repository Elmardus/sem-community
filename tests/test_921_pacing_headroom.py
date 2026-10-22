"""#926 — the pacer lands the pack full by the EARLIER of sunset and the next closed meter.

Every kWh of headroom the pack still has when the export price turns
negative is a kWh the export guard does not have to destroy. The battery
verdict carries the closing time (``until``); no price enters the pacer.
"""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

from custom_components.solar_energy_management.coordinator.coordinator import SEMCoordinator
from custom_components.solar_energy_management.coordinator.sink_verdicts import (
    HELD, OPEN, SinkVerdict,
)

TZ = timezone.utc
T10 = datetime(2026, 9, 15, 10, 0, tzinfo=TZ)


def _fake(battery_verdict):
    return SimpleNamespace(
        config={}, hass=MagicMock(),
        time_manager=SimpleNamespace(get_sunrise_time=lambda: "07:00",
                                     get_sunset_plus_10_time=lambda: "19:40"),
        _forecast_reader=SimpleNamespace(
            forecast_data=SimpleNamespace(forecast_today_kwh=30.0,
                                          forecast_remaining_today_kwh=20.0)),
        _tariff_provider=None,
        _sink_verdicts={"battery": battery_verdict},
        _day_home_w_at=lambda now: (lambda t: 500.0),
        _configured_export_rate=lambda: 0.075,
        _export_rate_at=lambda: (lambda ts: 0.075),   # (#1162)
    )


def _freeze(monkeypatch):
    import custom_components.solar_energy_management.coordinator.coordinator as C
    monkeypatch.setattr(C.dt_util, "now", lambda: T10)


class TestTheSecondLandingTime:
    def test_a_held_battery_trims_the_ledger_to_the_closing(self, monkeypatch):
        _freeze(monkeypatch)
        held = SinkVerdict("battery", HELD, "t", until=T10.replace(hour=15))
        slots = SEMCoordinator._today_pacing_ledger(_fake(held))
        assert slots, "the fixture must yield daylight slots"
        assert max(s.end for s in slots) <= T10.replace(hour=15)

    def test_an_open_battery_keeps_sunset(self, monkeypatch):
        _freeze(monkeypatch)
        slots = SEMCoordinator._today_pacing_ledger(_fake(SinkVerdict("battery", OPEN, "t")))
        assert slots and max(s.end for s in slots) > T10.replace(hour=15)

    def test_a_closing_after_sunset_changes_nothing(self, monkeypatch):
        _freeze(monkeypatch)
        held = SinkVerdict("battery", HELD, "t", until=T10.replace(hour=22))
        a = SEMCoordinator._today_pacing_ledger(_fake(held))
        b = SEMCoordinator._today_pacing_ledger(_fake(SinkVerdict("battery", OPEN, "t")))
        assert max(s.end for s in a) == max(s.end for s in b)

    def test_a_closing_already_past_changes_nothing(self, monkeypatch):
        _freeze(monkeypatch)
        held = SinkVerdict("battery", HELD, "t", until=T10.replace(hour=9))
        a = SEMCoordinator._today_pacing_ledger(_fake(held))
        b = SEMCoordinator._today_pacing_ledger(_fake(SinkVerdict("battery", OPEN, "t")))
        assert max(s.end for s in a) == max(s.end for s in b)

    def test_no_verdict_is_todays_ledger(self, monkeypatch):
        _freeze(monkeypatch)
        f = _fake(SinkVerdict("battery", OPEN, "t")); f._sink_verdicts = {}
        assert SEMCoordinator._today_pacing_ledger(f)
