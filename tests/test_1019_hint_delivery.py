"""#1019 — a hint reaches the event bus always and the phone when mobile
notifications are on; the coordinator builds the facts from what it has."""
from datetime import timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import time

import pytest

from custom_components.solar_energy_management.const import DOMAIN
from custom_components.solar_energy_management.coordinator.coordinator import (
    SEMCoordinator,
)
from custom_components.solar_energy_management.coordinator.hints import (
    Hint, HintEngine,
)
from custom_components.solar_energy_management.coordinator.notifications import (
    _CHANNEL_HINTS, NotificationManager,
)

TZ = timezone(timedelta(hours=2))


def _notifier(mobile_on):
    hass = MagicMock()
    hass.config.language = "en"
    hass.services.async_call = AsyncMock()
    hass.services.has_service = MagicMock(return_value=True)
    hass.bus.async_fire = MagicMock()
    hass.data = {}
    nm = NotificationManager(hass, {
        "mobile_notification_service": "notify.mobile_app_phone",
        "enable_mobile_notifications": mobile_on,
    })
    nm._mobile_service_checked = True
    nm._mobile_service_available = True
    nm._mobile_service_name = "mobile_app_phone"
    nm._mobile_service_domain = "notify"
    nm._mobile_service_is_companion = True
    return nm


HINT = Hint("night_load", "2026-09-30", "hint_msg_night_load", {"watts": 620, "usual": 210})


class TestDelivery:
    def test_the_channel(self):
        assert _CHANNEL_HINTS == "sem_hints"

    @pytest.mark.asyncio
    async def test_bus_event_always_phone_only_when_mobile_is_on(self):
        nm = _notifier(mobile_on=False)
        await nm.notify_hint(HINT)
        nm.hass.bus.async_fire.assert_called_once()
        event, payload = nm.hass.bus.async_fire.call_args.args
        assert event == f"{DOMAIN}_notification"
        assert payload["category"] == "hint"
        assert payload["hint"] == "night_load"
        assert payload["event"] == "2026-09-30"
        assert payload["message"] == "The house used 620 W all night, usually 210 W."
        nm.hass.services.async_call.assert_not_called()

        nm = _notifier(mobile_on=True)
        await nm.notify_hint(HINT)
        nm.hass.services.async_call.assert_called_once()
        domain, service, call = nm.hass.services.async_call.call_args.args
        assert (domain, service) == ("notify", "mobile_app_phone")
        assert call["message"] == "The house used 620 W all night, usually 210 W."
        assert call["data"]["channel"] == _CHANNEL_HINTS

    @pytest.mark.asyncio
    async def test_every_text_key_renders_in_english(self):
        nm = _notifier(mobile_on=False)
        cases = [
            Hint("silent_input", "k", "hint_msg_silent_input", {"name": "Solar power", "minutes": 17}),
            Hint("silent_input", "k", "hint_msg_silent_input_back", {"name": "Solar power"}),
            Hint("grid_rise", "k", "hint_msg_grid_rise", {"kwh": 9.1, "usual": 4.2}),
            Hint("cheap_now", "k", "hint_msg_cheap_now", {"cars": "Garage"}),
            Hint("weekly_summary", "k", "hint_msg_weekly_summary", {
                "solar": 142, "self_use": 71, "grid": 38, "ev": 24, "cost": 12.4, "currency": "CHF"}),
        ]
        texts = []
        for h in cases:
            nm.hass.bus.async_fire.reset_mock()
            await nm.notify_hint(h)
            texts.append(nm.hass.bus.async_fire.call_args.args[1]["message"])
        assert texts == [
            "Solar power stopped sending 17 min ago.",
            "Solar power is sending again.",
            "Grid use rose to 9.1 kWh (usually 4.2) with the same sun.",
            "Power is cheap now. Garage: plugged in and not charging.",
            "This week: 142 kWh solar · 71 % self-use · 38 kWh from the grid · 24 kWh into the car · 12.40 CHF.",
        ]


def _coordinator():
    c = SEMCoordinator.__new__(SEMCoordinator)
    c.hass = MagicMock()
    c.hass.config.currency = "CHF"
    c.hass.config.language = "en"
    c.config = {
        "tariff_mode": "dynamic",
        "hints": "all",
        "ev_chargers": [{"id": "keba", "name": "Garage"}, {"id": "wall2", "name": "Carport"}],
    }
    c.time_manager = MagicMock()
    c.time_manager.is_night_mode.return_value = False
    c._sensor_reader = MagicMock()
    c._sensor_reader._sensor_unavailable_since = {"sensor.solar": 0.0}
    c._storage = MagicMock()
    c._storage.get_hints_state.return_value = {}
    c._notification_manager = MagicMock()
    c._notification_manager.notify_hint = AsyncMock()
    c._hint_engine = None
    st = MagicMock()
    st.attributes = {"friendly_name": "Solar power"}
    c.hass.states.get.return_value = st
    return c


def _inputs():
    power = SimpleNamespace(
        home_consumption_power=310.0,
        ev_connected_per_charger={"keba": True, "wall2": True},
        ev_charging_per_charger={"keba": False, "wall2": True},
        ev_connected=True, ev_charging=False,
    )
    energy = SimpleNamespace(daily_solar=20.0, daily_grid_import=4.0, daily_home=12.0, daily_ev=1.0)
    costs = SimpleNamespace(daily_net_cost=1.5)
    performance = SimpleNamespace(self_consumption_rate=70.0)
    tariff = SimpleNamespace(tariff_price_level="cheap")
    return power, energy, costs, performance, tariff


class TestCoordinatorFacts:
    def test_the_facts_come_from_what_the_coordinator_has(self):
        c = _coordinator()
        facts = c._hint_facts(*_inputs(), now_mono=1200.0)
        assert facts.night is False
        assert all(facts.enabled.values())
        assert facts.home_w == 310.0
        assert facts.daily_import_kwh == 4.0
        assert facts.daily_cost == 1.5
        assert facts.self_use_pct == 70.0
        assert facts.currency == "CHF"
        assert facts.dark_inputs == {"sensor.solar": ("Solar power", 1200.0)}
        assert facts.price_cheap is True
        assert facts.dynamic_tariff is True
        assert facts.idle_plugged_cars == ("Garage",)

    def test_a_legacy_single_charger_is_named_ev(self):
        c = _coordinator()
        c.config["ev_chargers"] = []
        power, *rest = _inputs()
        power.ev_connected_per_charger = {}
        power.ev_charging_per_charger = {}
        facts = c._hint_facts(power, *rest, now_mono=0.0)
        assert facts.idle_plugged_cars == ("EV",)

    @pytest.mark.asyncio
    async def test_evaluate_sends_and_persists(self):
        c = _coordinator()
        # The evaluator reads the real monotonic clock. A CI machine can be
        # up for less than the 15-minute dark threshold, so "dark since 0"
        # would not be dark long enough there; date the outage an hour back.
        c._sensor_reader._sensor_unavailable_since = {
            "sensor.solar": time.monotonic() - 3600.0}
        await c._evaluate_hints(*_inputs())
        c._notification_manager.notify_hint.assert_awaited()
        sent = [call.args[0] for call in c._notification_manager.notify_hint.await_args_list]
        assert {h.category for h in sent} == {"cheap_now", "silent_input"}
        c._storage.set_hints_state.assert_called()
        state = c._storage.set_hints_state.call_args.args[0]
        assert HintEngine(state).sent["cheap_now:"]

    @pytest.mark.asyncio
    async def test_a_restart_restores_the_engine_from_storage(self):
        c = _coordinator()
        await c._evaluate_hints(*_inputs())
        stored = c._storage.set_hints_state.call_args.args[0]
        c2 = _coordinator()
        c2._storage.get_hints_state.return_value = stored
        await c2._evaluate_hints(*_inputs())
        c2._notification_manager.notify_hint.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_weekly_mode_sends_only_the_weekly_note(self):
        c = _coordinator()
        c.config["hints"] = "weekly"
        await c._evaluate_hints(*_inputs())
        c._notification_manager.notify_hint.assert_not_awaited()   # only the weekly note may fire, and this is no Sunday night start

    @pytest.mark.asyncio
    async def test_nothing_is_evaluated_while_hints_are_off(self):
        c = _coordinator()
        c.config = {"ev_chargers": [], "hints": "off"}
        await c._evaluate_hints(*_inputs())
        c._notification_manager.notify_hint.assert_not_awaited()
        assert c._hint_engine is None
        c._storage.set_hints_state.assert_not_called()
