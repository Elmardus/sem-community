"""(#1162) Plans price exported solar at the feed-in price that is paid.

Every day-slot builder priced its surplus slots with the configured
``electricity_export_rate``. With a live feed-in entity (dynamic tariff)
the sensors and the export cost read that entity through the tariff
provider, so the plan valued export at a number nobody is paid.

The plan now asks the provider per slot: the feed-in curve's slot when
the entity carries a forecast, else its value now, else the static rate.
One sign rule serves the live rate and every slot.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

from custom_components.solar_energy_management.coordinator.coordinator import (
    SEMCoordinator,
)
from custom_components.solar_energy_management.coordinator.day_ledger import (
    build_day_slots,
    tariff_export_rate_at,
)
from custom_components.solar_energy_management.tariff.tariff_provider import (
    DynamicTariffProvider,
    StaticTariffProvider,
)

TZ = timezone.utc
DAY = datetime(2026, 7, 1, 0, 0, tzinfo=TZ)
FEEDIN = "sensor.feedin_price"
STATIC = 0.075


def _state(value, attributes=None):
    st = MagicMock()
    st.state = str(value)
    st.attributes = attributes or {}
    st.last_updated = DAY
    return st


def _hass(states):
    hass = MagicMock()
    hass.states.get = MagicMock(side_effect=lambda eid: states.get(eid))
    return hass


def _hourly_curve(price_of_hour):
    return [{"start": (DAY + timedelta(hours=h)).isoformat(),
             "value": price_of_hour(h)} for h in range(24)]


def _provider(states, feedin=FEEDIN):
    return DynamicTariffProvider(_hass(states), feedin_entity=feedin,
                                 export_rate=STATIC)


def _at(hour, minute=0):
    return DAY + timedelta(hours=hour, minutes=minute)


class TestTheProviderPricesEachSlot:

    def test_a_feedin_forecast_prices_each_slot_at_its_own_hour(self):
        prov = _provider({FEEDIN: _state(0.05, {
            "raw_today": _hourly_curve(lambda h: round(0.01 * h, 2))})})
        assert prov.get_export_rate_at(_at(10)) == 0.10
        assert prov.get_export_rate_at(_at(14, 30)) == 0.14

    def test_a_feedin_without_forecast_uses_its_value_now(self):
        prov = _provider({FEEDIN: _state(0.042)})
        assert prov.get_export_rate_at(_at(9)) == 0.042
        assert prov.get_export_rate_at(_at(15)) == 0.042

    def test_an_hour_past_the_curve_uses_the_value_now(self):
        """Tomorrow unpublished: the value now, not the static rate."""
        prov = _provider({FEEDIN: _state(0.042, {
            "raw_today": _hourly_curve(lambda h: 0.10)})})
        assert prov.get_export_rate_at(_at(30)) == 0.042

    def test_no_feedin_entity_is_the_static_rate(self):
        assert _provider({}, feedin=None).get_export_rate_at(_at(12)) == STATIC

    def test_an_unreadable_feedin_entity_is_the_static_rate(self):
        prov = _provider({FEEDIN: _state("unavailable", {
            "raw_today": _hourly_curve(lambda h: 0.30)})})
        assert prov.get_export_rate_at(_at(12)) == STATIC

    def test_static_and_calendar_tariffs_pay_one_rate_all_day(self):
        prov = StaticTariffProvider(export_rate=0.081)
        assert {prov.get_export_rate_at(_at(h)) for h in range(24)} == {0.081}


class TestOneSignRule:

    def test_a_negative_slot_stays_a_cost(self):
        prov = _provider({FEEDIN: _state(-0.02, {
            "raw_today": _hourly_curve(lambda h: -0.03 if h == 13 else 0.05)})})
        assert prov.get_export_rate_at(_at(13)) == -0.03
        assert prov.get_current_export_rate() == -0.02

    def test_amber_flips_both_the_live_rate_and_its_forecast(self):
        """Amber's feed-in channel is sign-inverted; the live rate and the
        forecast sibling go through the same rule."""
        price = "sensor.amber_general_price"
        feed = "sensor.amber_feed_in_price"
        fc = "sensor.amber_feed_in_forecast"
        states = {
            price: _state(0.30),
            feed: _state(-0.08),
            fc: _state(-0.08, {"forecasts": [
                {"start_time": _at(h).isoformat(), "per_kwh": -0.01 * h}
                for h in range(24)]}),
        }
        hass = _hass(states)
        hass.states.async_all = MagicMock(
            return_value=[SimpleNamespace(entity_id=price)])
        prov = DynamicTariffProvider(hass, export_rate=STATIC)
        assert prov.detect_provider() == "amber"
        assert prov.get_current_export_rate() == 0.08
        assert prov.get_export_rate_at(_at(12)) == 0.12

    def test_without_a_curve_the_plan_rate_is_the_live_rate(self):
        for val in (0.04, -0.04):
            prov = _provider({FEEDIN: _state(val)})
            assert prov.get_export_rate_at(_at(11)) == \
                prov.get_current_export_rate()


def _slots(export_rate):
    return build_day_slots(
        start=_at(9), end=_at(16), day_kwh=40.0,
        sunrise=_at(6), sunset=_at(20),
        home_w_at=lambda t: 300.0, price_at=lambda t: 0.30,
        level_cheap_at=lambda t: False, export_rate=export_rate)


def _surplus_prices(slots):
    return {s.start.hour: s.price for s in slots
            if s.cap_override_w is not None}


class TestTheDayLedgerSeesTheFeedinPrice:

    def test_surplus_slots_carry_the_feedin_curve(self):
        prov = _provider({FEEDIN: _state(0.05, {
            "raw_today": _hourly_curve(lambda h: round(0.01 * h, 2))})})
        prices = _surplus_prices(_slots(
            lambda ts: tariff_export_rate_at(prov, ts, STATIC)))
        assert prices, "the fixture must produce surplus slots"
        assert prices == {h: round(0.01 * h, 2) for h in prices}

    def test_the_coordinator_reader_reaches_the_ledger(self):
        prov = _provider({FEEDIN: _state(0.05, {
            "raw_today": _hourly_curve(lambda h: 0.02 if h < 12 else 0.11)})})
        fake = SimpleNamespace(
            config={"electricity_export_rate": STATIC},
            _tariff_provider=prov)
        fake._configured_export_rate = (
            lambda: SEMCoordinator._configured_export_rate(fake))
        prices = _surplus_prices(_slots(SEMCoordinator._export_rate_at(fake)))
        assert prices[10] == 0.02 and prices[14] == 0.11

    def test_a_static_install_prices_exactly_as_before(self):
        for prov in (StaticTariffProvider(export_rate=STATIC), None,
                     _provider({}, feedin=None)):
            fake = SimpleNamespace(
                config={"electricity_export_rate": STATIC},
                _tariff_provider=prov)
            fake._configured_export_rate = (
                lambda f=fake: SEMCoordinator._configured_export_rate(f))
            assert _slots(SEMCoordinator._export_rate_at(fake)) == \
                _slots(STATIC)


class TestASilentEntityAcrossMidnight:
    """A "today" array is anchored to the current day. An entity that does
    not update across midnight keeps its State object; the feed-in curve must
    re-anchor like the import curve does, not fall back to the live value."""

    def test_export_follows_the_new_days_curve_like_import(self, monkeypatch):
        import custom_components.solar_energy_management.tariff \
            .tariff_provider as tp
        today = [round(0.10 + 0.01 * h, 2) for h in range(24)]
        state = _state(0.20, {"today": today})
        hass = _hass({FEEDIN: state, "sensor.price": state})
        prov = DynamicTariffProvider(hass, price_entity="sensor.price",
                                     feedin_entity=FEEDIN, export_rate=STATIC)
        clock = {"now": DAY.replace(hour=23, minute=50)}
        monkeypatch.setattr(tp.dt_util, "now", lambda: clock["now"])

        assert prov.get_export_rate_at(clock["now"]) == 0.33
        assert prov.get_price_at(clock["now"]) == 0.33

        clock["now"] = DAY + timedelta(days=1, minutes=10)
        assert prov.get_price_at(clock["now"]) == 0.10
        assert prov.get_export_rate_at(clock["now"]) == 0.10


def test_a_plan_slot_takes_the_feed_in_fee_off_like_the_live_rate():
    """(#1077) A plan weighs the export VALUE: with a per-kWh fee, every
    forecast slot and the live rate lose the same fee."""
    from custom_components.solar_energy_management.tariff import value_layer as vl
    prov = _provider({FEEDIN: _state(0.05, {
        "today": _hourly_curve(lambda h: 0.10 + 0.01 * h)})})
    prov.value_settings = vl.ValueSettings(fee_model=vl.FEE_PER_KWH, fee=0.03)
    assert abs(prov.get_export_rate_at(_at(10)) - (0.20 - 0.03)) < 1e-9
    assert abs(prov.get_current_export_rate()
               - (prov.get_current_spot_export_rate() - 0.03)) < 1e-9
