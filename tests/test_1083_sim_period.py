"""#1083 — a Simulation run is one continuous period.

A battery carries energy from one day to the next, so a run takes a period
(``from`` … ``to``), never a scatter of days: every stored day in it runs
as one chain, a gap day is left out and bridged. Day types filter the
result afterwards. The result carries what a person needs to see why the
what-if differs: per-day energy and a short note, energy totals actual vs
simulated, the difference per month, and the headline with the stored
energy counted. The what-if levers (battery power, PV size, battery→car)
arrive over the same command.
"""
import base64

import pytest

from custom_components.solar_energy_management import simulation_ws as ws
from custom_components.solar_energy_management.coordinator.simulation_job import (
    MAX_DAYS,
    SimulationError,
    energy_totals,
    month_rows,
)

from .test_1083_simulation_job import (
    CFG,
    DATES,
    _call,
    _error,
    _finish,
    _job,
    _read_xlsx,
    _result,
    _tag,
    _ws_hass,
)

PARAMS = {"capacity_kwh": 20, "reserve_pct": 10}


def _start(hass, date_from, date_to, **params):
    return _call(ws.ws_start, hass, **{"from": date_from, "to": date_to,
                                       "params": {**PARAMS, **params}})


@pytest.mark.asyncio
async def test_a_period_runs_every_stored_day_in_it_as_one_chain():
    job, storage, _ = _job()
    hass = _ws_hass(job)
    _result(_start(hass, "2026-10-01", "2026-10-31"))
    await _finish(job)
    res = _result(_call(ws.ws_result, hass))
    assert [d["date"] for d in res["days"]] == DATES
    assert res["period"] == {"from": "2026-10-01", "to": "2026-10-31"}
    assert res["stored_energy"]["stretches"] == 1


@pytest.mark.asyncio
async def test_a_gap_day_in_the_period_is_left_out_and_bridged():
    tags = [_tag(DATES[0]), _tag(DATES[1], gap=True), _tag(DATES[2])]
    job, storage, _ = _job(tags=tags)
    hass = _ws_hass(job)
    _result(_start(hass, DATES[0], DATES[-1]))
    await _finish(job)
    res = _result(_call(ws.ws_result, hass))
    assert [d["date"] for d in res["days"]] == [DATES[0], DATES[2]]
    assert res["stored_energy"]["stretches"] == 1
    assert "bridged" in res["days"][1]["story"][0]


@pytest.mark.asyncio
async def test_a_period_is_checked():
    job, _, _ = _job()
    hass = _ws_hass(job)
    assert _error(_start(hass, "2026-10-12", "2026-10-10")) == "invalid"
    assert _error(_start(hass, "2025-01-01", "2026-10-12")) == "invalid"
    assert _error(_start(hass, "2020-01-01", "2020-01-31")) == "invalid"
    assert _error(_call(ws.ws_start, hass, **{"from": DATES[0],
                                              "params": PARAMS})) == "invalid"
    assert _error(_call(ws.ws_start, hass, params=PARAMS)) == "invalid"
    with pytest.raises(SimulationError):
        job.period_days("2024-01-01", "2026-01-01")
    assert MAX_DAYS == 365


@pytest.mark.asyncio
async def test_a_period_is_refused_while_the_switch_is_off_or_busy():
    job, _, _ = _job(cfg={**CFG, "simulation_tab": False})
    assert _error(_start(_ws_hass(job), DATES[0], DATES[-1])) == "off"
    job, _, _ = _job()
    hass = _ws_hass(job)
    _result(_start(hass, DATES[0], DATES[-1]))
    assert _error(_start(hass, DATES[0], DATES[-1])) == "busy"
    await _finish(job)


@pytest.mark.asyncio
async def test_the_result_says_why_each_day_differs():
    job, _, _ = _job()
    hass = _ws_hass(job)
    _result(_start(hass, DATES[0], DATES[-1]))
    await _finish(job)
    res = _result(_call(ws.ws_result, hass))
    row = res["days"][0]
    for side in ("actual", "simulated"):
        for key in ("grid_import", "grid_export", "battery_charge",
                    "battery_discharge"):
            assert f"{side}_{key}_kwh" in row
    assert row["actual_end_kwh"] == pytest.approx(5.0)     # 50 % of 10 kWh
    assert "simulated_end_kwh" in row and isinstance(row["story"], list)
    assert row["story"][0].startswith("starts from the recorded level")
    assert row["net_delta"] == pytest.approx(
        row["delta"] - row["pack_change_value"], abs=0.011)
    # the headline: the plain difference with the stored energy counted
    tot = res["total"]
    assert tot["net_delta"] == pytest.approx(
        tot["delta"] - res["stored_energy"]["value"], abs=0.011)
    e = res["energy"]
    assert set(e) == {"actual", "simulated"}
    for side in e.values():
        assert set(side) >= {"grid_import_kwh", "grid_export_kwh", "cost",
                             "self_consumption_pct", "cycles"}
    assert e["actual"]["cost"] == pytest.approx(tot["actual"], abs=0.02)
    assert [m["month"] for m in res["months"]] == ["2026-10"]
    assert res["months"][0]["days"] == 3


@pytest.mark.asyncio
async def test_the_xlsx_carries_the_per_day_energy_months_and_totals():
    job, _, _ = _job()
    hass = _ws_hass(job)
    _result(_start(hass, DATES[0], DATES[-1]))
    await _finish(job)
    sheets = _read_xlsx(base64.b64decode(_result(_call(ws.ws_xlsx, hass))["base64"]))
    head = sheets["Days"][0]
    for want in ("Grid import kWh, actual", "Grid export kWh, actual",
                 "Battery charge kWh, actual", "Battery discharge kWh, actual",
                 "Pack at day end kWh, actual", "Note"):
        assert want in head
    assert any("simulated" in h and "Grid import" in h for h in head)
    assert sheets["Months"][1][0] == ("s", "2026-10")
    labels = [r[0][1] for r in sheets["Totals"] if r]
    assert any(lb.startswith("Net difference incl. stored energy") for lb in labels)
    for want in ("Grid import kWh", "Self-consumption %", "Battery cycles"):
        assert want in labels
    notes = {r[0][1]: r[1] for r in sheets["Notes"][1:] if len(r) > 1}
    assert any(k.startswith("Extra energy left in the pack at stretch ends, summed")
               for k in notes)
    assert not any(k.startswith("Energy in the battery at the end") for k in notes)
    assert ("s", "How the days run") in [r[0] for r in sheets["Notes"][1:]]


# ── the levers ───────────────────────────────────────────────────────


def _params(job, **raw):
    return ws._params(job, {**PARAMS, **raw})


@pytest.mark.asyncio
async def test_battery_power_reaches_the_simulation():
    job, _, _ = _job()
    p = _params(job, max_charge_w=10000, max_discharge_w=8000)
    assert (p.max_charge_w, p.max_discharge_w) == (10000.0, 8000.0)
    assert _params(job).max_charge_w == 5000.0            # as configured
    with pytest.raises(SimulationError):
        _params(job, max_charge_w=-1)


@pytest.mark.asyncio
async def test_pv_as_a_factor_or_extra_kwp():
    job, _, _ = _job(cfg={**CFG, "system_size_kwp": 10.0})
    assert _params(job, pv_extra_kwp=5).pv_factor == pytest.approx(1.5)
    assert _params(job, pv_factor=1.2).pv_factor == pytest.approx(1.2)
    assert _params(job).pv_factor == 1.0
    unknown, _, _ = _job()
    with pytest.raises(SimulationError):                 # no size: ask a factor
        _params(unknown, pv_extra_kwp=5)
    with pytest.raises(SimulationError):
        _params(job, pv_factor=9)


@pytest.mark.asyncio
async def test_battery_to_the_car_off_is_the_one_change():
    job, _, _ = _job()
    assert _params(job, battery_to_ev=False).battery_to_ev is False
    assert _params(job, battery_to_ev=True).battery_to_ev is None
    assert _params(job).battery_to_ev is None


def test_the_schema_takes_the_period_and_the_levers():
    schema = ws.ws_start._ws_schema
    out = schema({"type": ws.WS_START, "id": 1, "from": DATES[0], "to": DATES[-1],
                  "params": {**PARAMS, "max_charge_w": 8000, "pv_factor": 1.5,
                             "pv_extra_kwp": None, "battery_to_ev": False}})
    assert out["from"] == DATES[0] and out["params"]["battery_to_ev"] is False


def test_totals_and_months_are_plain_sums():
    class R:
        def __init__(self, date, delta, change):
            self.date = date
            self.actual = {"grid_import": 10.0, "grid_export": 5.0, "solar": 20.0,
                           "battery_charge": 4.0, "battery_discharge": 4.0}
            self.simulated = {"grid_import": 6.0, "grid_export": 2.0, "solar": 20.0,
                              "battery_charge": 10.0, "battery_discharge": 10.0}
            self.actual_cost, self.simulated_cost = 3.0, 3.0 + delta
            self.delta, self.pack_change_value = delta, change

    rows = [R("2026-09-30", -1.0, 0.5), R("2026-10-01", 1.0, 2.0)]
    e = energy_totals(rows, sim_cap=20.0, real_cap=10.0)
    assert e["actual"]["self_consumption_pct"] == pytest.approx(75.0)
    assert e["simulated"]["self_consumption_pct"] == pytest.approx(90.0)
    assert e["actual"]["cycles"] == pytest.approx(0.8)     # 8 kWh / 10 kWh
    assert e["simulated"]["cycles"] == pytest.approx(1.0)  # 20 kWh / 20 kWh
    m = month_rows(rows)
    assert [x["month"] for x in m] == ["2026-09", "2026-10"]
    assert m[1]["delta"] == 1.0 and m[1]["net_delta"] == -1.0
