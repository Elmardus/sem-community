"""#996 — the two reference installs, from their real options.

``tests/fixtures/996_*_options.json`` are the merged data+options of the
SEM entry on HA-PROD and HA-TEST (30.09.2026; the notification service
removed). Both are on a flat tariff, both let SEM find the forecast and
the export-limit entity in the registry, and both HAVE them (Forecast.Solar
/ Solcast; Huawei ``active_power_control``).

The dropped list is the proof: exactly the rows that can do nothing on
that house, nothing else."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "audit_control_needs.py"


def _audit():
    spec = importlib.util.spec_from_file_location("audit_control_needs", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.audit


def _options(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


HARDWARE_ROWS = ("heat_pump", "hot_water", "legionella")


def _new_drops(result):
    """The capability drops — the #923 hardware drops are not new."""
    return [e for e in result["dropped"] if not any(h in e for h in HARDWARE_ROWS)]


def test_prod_loses_only_what_a_flat_tariff_without_a_plant_size_cannot_use():
    result = _audit()(_options("996_prod_options.json"),
                      solar_forecast=True, export_limit=True, ed_battery=True, ed_ev=True)
    assert result["verdict"]["dynamic_tariff"] == "absent"
    assert result["verdict"]["export_limit"] == "present"
    assert result["verdict"]["solar_forecast"] == "present"
    assert result["verdict"]["investment"] == "present"
    assert _new_drops(result) == [
        # static tariff: the thresholds feed the dynamic provider only
        "number.sem_cheap_price_threshold",
        "number.sem_expensive_price_threshold",
        # (#1077) no value field set: the value books stay off
        "sensor.sem_daily_export_value",
        "sensor.sem_daily_feed_in_fees",
        "sensor.sem_daily_self_consumption_value",
        # no plant size set: kWh/kWp was divided by the analyzer's 10 kWp default
        "sensor.sem_pv_daily_specific_yield",
        "sensor.sem_pv_degradation_trend",
        "sensor.sem_pv_estimated_annual_degradation",
        # a flat tariff has no next cheap window
        "sensor.sem_tariff_next_cheap_start",
        "sensor.sem_yearly_export_value",
        "sensor.sem_yearly_feed_in_fees",
        "sensor.sem_yearly_self_consumption_value",
    ]


def test_prod_keeps_its_export_guard_forecast_pacing_and_roi():
    result = _audit()(_options("996_prod_options.json"),
                      solar_forecast=True, export_limit=True, ed_battery=True, ed_ev=True)
    gone = set(result["dropped"])
    for kept in ("switch.sem_export_guard_enabled", "sensor.sem_forecast_today_kwh",
                 "switch.sem_battery_charge_pacing_enabled", "switch.sem_forecast_spending_enabled",
                 "sensor.sem_roi_percentage", "number.sem_battery_max_discharge_power",
                 "number.sem_system_size_kwp", "number.sem_electricity_import_rate"):
        assert kept not in gone, kept


def test_hatest_loses_the_price_knobs_and_the_roi_rows():
    result = _audit()(_options("996_hatest_options.json"),
                      solar_forecast=True, export_limit=True, ed_battery=True, ed_ev=True)
    assert result["verdict"]["pv_size"] == "present"     # 10 kWp is set there
    assert result["verdict"]["investment"] == "absent"
    assert _new_drops(result) == [
        "number.sem_cheap_price_threshold",
        "number.sem_expensive_price_threshold",
        # (#1077) no value field set: the value books stay off
        "sensor.sem_daily_export_value",
        "sensor.sem_daily_feed_in_fees",
        "sensor.sem_daily_self_consumption_value",
        "sensor.sem_roi_annual_savings",
        "sensor.sem_roi_payback_years",
        "sensor.sem_roi_percentage",
        "sensor.sem_tariff_next_cheap_start",
        "sensor.sem_yearly_export_value",
        "sensor.sem_yearly_feed_in_fees",
        "sensor.sem_yearly_self_consumption_value",
    ]


def test_not_asked_drops_nothing_that_needs_an_answer():
    # The runtime facts left unknown: forecast and export-limit rows stay.
    result = _audit()(_options("996_prod_options.json"))
    gone = set(result["dropped"])
    assert "sensor.sem_forecast_today_kwh" not in gone
    assert "switch.sem_export_guard_enabled" not in gone
    assert "number.sem_cheap_price_threshold" in gone
