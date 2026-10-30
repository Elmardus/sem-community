"""#996 — a control is shown only where this house can use it.

The install-modules oracle (#923) grows five capabilities next to the four
hardware modules. A price knob needs a dynamic tariff; the export guard
needs an export-limit entity; the forecast rows need a forecast; the
per-kWp rows need a plant size; the ROI rows need an investment figure.
Same three states, same consumers: nothing is created for an ABSENT
capability, UNKNOWN keeps everything.

Two capabilities are answered partly at runtime (an integration SEM finds
in the entity registry): a forecast integration and the inverter's
export-limit entity. Their answer is fed in as ``runtime``; "not asked" is
UNKNOWN, never ABSENT (#925)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from custom_components.solar_energy_management.coordinator.install_modules import (
    CAPABILITY_KEYS,
    ENTITY_MODULES,
    MODULE_EVIDENCE_KEYS,
    Module,
    Presence,
    absent_entity_ids,
    all_unknown,
    entity_kept,
    module_verdict,
    presence_summary,
)

ED_EMPTY = SimpleNamespace(has_battery=False, has_ev=False)

CAPABILITIES = (
    Module.DYNAMIC_TARIFF, Module.EXPORT_LIMIT, Module.SOLAR_FORECAST,
    Module.PV_SIZE, Module.INVESTMENT,
)


def _v(config, runtime=None):
    return module_verdict(config, ED_EMPTY, True, runtime=runtime)


class TestDynamicTariff:

    def test_dynamic_mode_is_present(self):
        assert _v({"tariff_mode": "dynamic"})[Module.DYNAMIC_TARIFF] is Presence.PRESENT

    @pytest.mark.parametrize("mode", ["static", "", None])
    def test_static_or_unset_is_absent(self, mode):
        assert _v({"tariff_mode": mode})[Module.DYNAMIC_TARIFF] is Presence.ABSENT

    def test_a_price_entity_without_the_mode_is_not_a_dynamic_tariff(self):
        # The dynamic provider is built only when the mode says so; a price
        # entity alone drives nothing.
        v = _v({"price_entity": "sensor.price", "tariff_mode": "static"})
        assert v[Module.DYNAMIC_TARIFF] is Presence.ABSENT

    def test_never_unknown(self):
        assert module_verdict({}, None, False)[Module.DYNAMIC_TARIFF] is Presence.ABSENT


class TestExportLimit:

    def test_a_named_entity_is_present_without_asking(self):
        v = _v({"export_limit_entity": "number.inverter_export_limit"})
        assert v[Module.EXPORT_LIMIT] is Presence.PRESENT

    def test_found_at_runtime_is_present(self):
        assert _v({}, {"export_limit": True})[Module.EXPORT_LIMIT] is Presence.PRESENT

    def test_asked_and_none_is_absent(self):
        assert _v({}, {"export_limit": False})[Module.EXPORT_LIMIT] is Presence.ABSENT

    def test_not_asked_is_unknown(self):
        assert _v({})[Module.EXPORT_LIMIT] is Presence.UNKNOWN
        assert _v({}, {"export_limit": None})[Module.EXPORT_LIMIT] is Presence.UNKNOWN

    def test_a_non_bool_answer_is_not_an_answer(self):
        assert _v({}, {"export_limit": "yes"})[Module.EXPORT_LIMIT] is Presence.UNKNOWN


class TestSolarForecast:

    def test_a_forecast_entity_is_present(self):
        v = _v({"dynamic_forecast_entity": "sensor.pv_forecast"})
        assert v[Module.SOLAR_FORECAST] is Presence.PRESENT

    def test_a_detected_integration_is_present(self):
        assert _v({}, {"solar_forecast": True})[Module.SOLAR_FORECAST] is Presence.PRESENT

    def test_detection_found_nothing_is_absent(self):
        assert _v({}, {"solar_forecast": False})[Module.SOLAR_FORECAST] is Presence.ABSENT

    def test_a_named_source_still_needs_the_integration(self):
        # "solcast" chosen but never installed: the rows would show nothing.
        v = _v({"solar_forecast_source": "solcast"}, {"solar_forecast": False})
        assert v[Module.SOLAR_FORECAST] is Presence.ABSENT

    def test_not_detected_yet_is_unknown(self):
        assert _v({"solar_forecast_source": "auto"})[Module.SOLAR_FORECAST] is Presence.UNKNOWN


class TestPlantSizeAndInvestment:

    @pytest.mark.parametrize("value", [10.0, 3, "7.5"])
    def test_a_positive_size_is_present(self, value):
        assert _v({"system_size_kwp": value})[Module.PV_SIZE] is Presence.PRESENT

    @pytest.mark.parametrize("value", [None, 0, 0.0, "", "abc", -1])
    def test_no_size_is_absent(self, value):
        assert _v({"system_size_kwp": value})[Module.PV_SIZE] is Presence.ABSENT

    def test_investment_follows_the_same_rule(self):
        assert _v({"system_investment_cost": 38000})[Module.INVESTMENT] is Presence.PRESENT
        assert _v({})[Module.INVESTMENT] is Presence.ABSENT


class TestTheFourModulesAreUntouched:

    def test_hardware_verdicts_ignore_the_runtime_facts(self):
        v = module_verdict({"battery_soc_sensor": "sensor.soc"}, ED_EMPTY, True,
                           runtime={"solar_forecast": False, "export_limit": False})
        assert v[Module.BATTERY] is Presence.PRESENT
        assert v[Module.EV] is Presence.ABSENT

    def test_the_three_argument_call_still_works(self):
        v = module_verdict({}, None, False)
        assert v[Module.BATTERY] is Presence.UNKNOWN
        assert v[Module.SOLAR_FORECAST] is Presence.UNKNOWN


class TestRows:
    PLAIN = {**all_unknown(), **{c: Presence.ABSENT for c in CAPABILITIES}}

    @pytest.mark.parametrize("pk", [
        ("number", "cheap_price_threshold"), ("number", "expensive_price_threshold"),
        ("sensor", "tariff_next_cheap_start"),
    ])
    def test_price_rows_need_a_dynamic_tariff(self, pk):
        assert ENTITY_MODULES[pk] == {Module.DYNAMIC_TARIFF}
        assert not entity_kept(*pk, self.PLAIN)

    @pytest.mark.parametrize("pk", [
        ("switch", "export_guard_enabled"), ("switch", "export_guard_override_external"),
        ("number", "export_guard_engage_s"), ("number", "export_guard_release_s"),
        ("sensor", "export_guard_state"),
    ])
    def test_export_guard_rows_need_an_export_limit(self, pk):
        assert ENTITY_MODULES[pk] == {Module.EXPORT_LIMIT}

    @pytest.mark.parametrize("key", [
        "forecast_today_kwh", "forecast_tomorrow_kwh", "forecast_remaining_today_kwh",
        "forecast_power_now_w", "forecast_peak_power_today_w", "forecast_peak_time_today",
        "forecast_surplus_kwh", "forecast_trust_d1", "forecast_trust_d2",
        "forecast_dampening_factor", "forecast_correction_factor",
        "forecast_corrected_today", "forecast_history_days", "best_surplus_window",
        "pv_performance_vs_forecast",
    ])
    def test_forecast_rows_need_a_forecast(self, key):
        assert ENTITY_MODULES[("sensor", key)] == {Module.SOLAR_FORECAST}

    @pytest.mark.parametrize("pk", [
        ("switch", "forecast_spending_enabled"), ("switch", "battery_charge_pacing_enabled"),
        ("sensor", "battery_spendable_kwh"), ("sensor", "battery_dynamic_floor_pct"),
        ("sensor", "battery_charge_pacing"),
    ])
    def test_spending_and_pacing_need_the_battery_and_a_forecast(self, pk):
        assert ENTITY_MODULES[pk] == {Module.BATTERY, Module.SOLAR_FORECAST}

    @pytest.mark.parametrize("key", [
        "pv_daily_specific_yield", "pv_estimated_annual_degradation", "pv_degradation_trend",
    ])
    def test_per_kwp_rows_need_a_plant_size(self, key):
        assert ENTITY_MODULES[("sensor", key)] == {Module.PV_SIZE}

    @pytest.mark.parametrize("key", ["roi_percentage", "roi_payback_years", "roi_annual_savings"])
    def test_roi_rows_need_an_investment(self, key):
        assert ENTITY_MODULES[("sensor", key)] == {Module.INVESTMENT}

    def test_the_tell_tales_stay_on_every_install(self):
        # These are how "you have no dynamic tariff / no forecast" stays
        # visible — the #923 diag_charger_count precedent.
        for pk in (("sensor", "forecast_source"), ("binary_sensor", "forecast_available"),
                   ("sensor", "tariff_provider"), ("binary_sensor", "tariff_is_dynamic"),
                   ("sensor", "tariff_price_level"), ("number", "system_size_kwp"),
                   ("number", "system_investment_cost")):
            assert entity_kept(*pk, self.PLAIN), pk

    def test_the_plain_house_loses_exactly_these_controls(self):
        gone = absent_entity_ids(self.PLAIN)
        controls = sorted(e for e in gone if not e.startswith(("sensor.", "binary_sensor.")))
        assert controls == [
            "number.sem_cheap_price_threshold", "number.sem_expensive_price_threshold",
            "number.sem_export_guard_engage_s", "number.sem_export_guard_release_s",
            "switch.sem_battery_charge_pacing_enabled", "switch.sem_export_guard_enabled",
            "switch.sem_export_guard_override_external", "switch.sem_forecast_spending_enabled",
        ]

    def test_unknown_keeps_every_capability_row(self):
        assert absent_entity_ids(all_unknown()) == frozenset()


class TestWiring:

    def test_capability_keys_are_not_reload_keys(self):
        # #462: a slider (plant size, investment) must not reload per tweak.
        assert not set(CAPABILITY_KEYS) & MODULE_EVIDENCE_KEYS

    def test_summary_carries_the_capabilities(self):
        s = presence_summary({Module.DYNAMIC_TARIFF: Presence.ABSENT})
        assert s["dynamic_tariff"] == "absent"
        assert set(s) == {m.value for m in Module}
