"""#1083 — what a bigger pack may and may not do in the Simulation.

A real run (339 days, own pack 15 kWh, what-if 115 kWh) put one sunny day
at +25.94: 2026-04-24, actual −0.80, simulated 25.14 — about 94 kWh bought
from the grid. The cause, pinned first below: that day had a missing hour,
and a day with a missing hour was neither checked nor closed on the meters,
so a counter jump (144 kWh of solar, 239 kWh of car at 14:00) went through
unclosed and the model bought the 94 kWh between them (bug class 158).

The night plan had three faults of its own on the way, each pinned here:
a negative evening price charged the whole night at positive prices; a
backfilled day with no stored forecast planned the whole house from the
grid; and the deficit grew with the pack. Then the invariants, over
synthetic days: with the same settings a bigger pack never makes the
period dearer (stored energy included) beyond the losses it explains, and
a sunny day with surplus never buys more with a bigger pack unless a
price-justified night charge says so.
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from custom_components.solar_energy_management.analytics.simulation.battery_sim import (
    BRIDGE_MAX_DAYS,
    FORECAST_PROXY_NOTE,
    PeriodChain,
    SimParams,
    night_end,
    simulate_day,
    simulate_period,
)
from custom_components.solar_energy_management.analytics.simulation.hourly_day import (
    FLOW_ROLES,
    HourlyDay,
)

TZ = timezone(timedelta(hours=2))
FIXTURES = Path(__file__).resolve().parent / "fixtures"
#: the 996 PROD options: own pack 15 kWh, 6 kW peak, self-consumption only
PROD = json.loads((FIXTURES / "996_prod_options.json").read_text())
#: PROD with night charging on and a cheap night (the run's install)
NIGHT = {**PROD, "battery_charge_scheduler_enabled": True}
NIGHT_RATE, DAY_RATE, EXPORT = 0.20, 0.33, 0.075
RATES = [NIGHT_RATE] * 6 + [DAY_RATE] * 15 + [NIGHT_RATE] * 3
SIZES = (15.0, 30.0, 60.0, 115.0)

SUN = [0.0] * 24
for _h, _v in zip(range(6, 21), (0.2, 1, 3, 5, 7, 8.5, 9, 9, 8.5, 7, 5, 3,
                                  1.5, 0.6, 0.1), strict=True):
    SUN[_h] = _v
GREY = [0.0] * 24
for _h, _v in zip(range(8, 18), (0.1, 0.3, 0.6, 0.9, 1.0, 1.0, 0.8, 0.5,
                                  0.3, 0.1), strict=True):
    GREY[_h] = _v


def _day(date, solar, *, tags, soc=40.0, ev=None, rates=None,
         forecast="solar", home=0.6):
    """A recorded day as the real 15 kWh pack would have run it is not
    needed here: the actual side only prices the meters. The meters close
    with no pack (import = what the sun missed, export = the rest)."""
    ev = list(ev or [0.0] * 24)
    homes = [home] * 24
    imp = [max(0.0, homes[h] + ev[h] - solar[h]) for h in range(24)]
    exp = [max(0.0, solar[h] - homes[h] - ev[h]) for h in range(24)]
    return HourlyDay(
        date=date, tags=tags, solar=list(solar), home=homes, ev=ev,
        grid_import=imp, grid_export=exp, soc_pct=[soc] * 24,
        import_rate=list(rates or RATES), export_rate=[EXPORT] * 24,
        forecast_kwh=(sum(solar) if forecast == "solar" else forecast))


def _period_cost(run):
    """The period's simulated cost with the stored energy counted."""
    return sum(d.simulated_cost for d in run.days) - run.stored_energy_value


def _run(days, cap, cfg=NIGHT, **over):
    return simulate_period(days, SimParams.from_config(cfg, capacity_kwh=cap,
                                                       **over), cfg)


# ── the 24.04 day ───────────────────────────────────────────────────


def _rows(date, values, key="change", skip=()):
    day = datetime.fromisoformat(date).replace(tzinfo=TZ)
    return [{"start": (day + timedelta(hours=h)).isoformat(), key: v}
            for h, v in enumerate(values) if h not in skip]


def _the_24_04(skip=(3,)):
    """PROD 24.04.2026 as the recorder had it: a sunny day with the car at
    noon, one hour the recorder never wrote (03:00), and at 14:00 the solar
    counter +144.27 kWh, the car counter +238.51 kWh."""
    ev = [0.0] * 24
    for h in range(11, 14):
        ev[h] = 5.0
    home = [0.6] * 24
    imp = [max(0.0, home[h] + ev[h] - SUN[h]) for h in range(24)]
    exp = [max(0.0, SUN[h] - home[h] - ev[h]) for h in range(24)]
    solar = list(SUN)
    solar[14] += 144.27
    car = list(ev)
    car[14] += 238.51
    flows = dict(solar=solar, ev=car, home=home, grid_import=imp,
                 grid_export=exp, battery_charge=[0.0] * 24,
                 battery_discharge=[0.0] * 24)
    rows = {role: _rows("2026-04-24", flows[role], skip=skip)
            for role in FLOW_ROLES}
    rows["soc"] = _rows("2026-04-24", [40.0] * 24, key="mean")
    rows["import_rate"] = _rows("2026-04-24", [0.27] * 24, key="mean")
    rows["export_rate"] = _rows("2026-04-24", [EXPORT] * 24, key="mean")
    return HourlyDay.from_statistics(rows, date="2026-04-24", tz=TZ)


def test_the_24_04_day_is_closed_on_the_meters_with_a_missing_hour():
    day = _the_24_04()
    assert day.missing_hours == [3]
    assert day.counters_disagree          # the jump is seen, not hidden
    # 14:00 closes: the sun covers the car as the meters say
    assert day.solar[14] - day.home[14] - day.ev[14] == pytest.approx(
        day.grid_export[14] + day.battery_charge[14]
        - day.grid_import[14] - day.battery_discharge[14])


@pytest.mark.parametrize("cap", [15.0, 115.0])
def test_the_24_04_day_buys_no_phantom_car_energy(cap):
    # Before the fix: +23.3 (15 kWh) and +26.2 (115 kWh), ~90 kWh bought —
    # the 94.24 kWh of car the jumped solar did not cover, at 0.27.
    res = simulate_day(_the_24_04(), SimParams.from_config(PROD, capacity_kwh=cap),
                       PROD)
    assert res.simulated["grid_import"] < 10.0
    assert res.delta < 5.0
    assert res.actual_cost < 0.0          # the meters' own day: paid back


# ── the night plan ──────────────────────────────────────────────────


def test_a_negative_evening_price_charges_only_its_own_hours():
    # One negative price at 21:00 used to charge the whole next night to
    # 95 % of the pack at positive prices: 35 kWh into a 115 kWh pack on a
    # sunny day (+10.6 on its own).
    rates = list(RATES)
    rates[21] = -0.01
    day = _day("2026-04-20", SUN, tags=["sunny"], rates=rates)
    res = simulate_day(day, SimParams.from_config(NIGHT, capacity_kwh=115.0),
                       NIGHT)
    assert res.grid_charge_kwh == pytest.approx(0.0, abs=0.01)


def test_a_negative_night_hour_charges_in_that_hour_only():
    rates = list(RATES)
    rates[2] = -0.05
    day = _day("2026-04-20", GREY, tags=["grey"], rates=rates, soc=30.0)
    res = simulate_day(day, SimParams.from_config(NIGHT, capacity_kwh=115.0),
                       NIGHT)
    by_hour = {r["hour"]: r for r in res.hours}
    assert by_hour[2]["grid_charge"] > 4.0
    # never more than the pack takes in that hour
    assert by_hour[2]["grid_charge"] <= 5.0 + 1e-9
    assert any("negative price" in s for s in res.story)


def test_a_day_without_a_forecast_plans_with_its_own_solar():
    # A backfilled day has no stored forecast. The scheduler's no-forecast
    # tier planned the whole house (14 kWh) from the grid on a sunny day.
    day = _day("2026-04-21", SUN, tags=["sunny"], forecast=None, soc=20.0)
    res = simulate_day(day, SimParams.from_config(NIGHT, capacity_kwh=115.0),
                       NIGHT)
    assert res.grid_charge_kwh == pytest.approx(0.0, abs=0.01)
    assert FORECAST_PROXY_NOTE in res.story


def test_the_night_charge_does_not_grow_with_the_pack():
    # A grey day: the house needs ~10 kWh more than the sun gives. Charged
    # from an empty pack, a 115 kWh pack buys no more than a 30 kWh one.
    day = _day("2026-04-22", GREY, tags=["grey"], soc=20.0, forecast=2.0)
    charge = {}
    for cap in (30.0, 115.0):
        res = simulate_day(day, SimParams.from_config(NIGHT, capacity_kwh=cap),
                           NIGHT)
        charge[cap] = res.grid_charge_kwh
    assert charge[30.0] > 2.0                     # the plan does charge
    assert charge[115.0] <= charge[30.0] + 0.01
    need = sum(max(0.0, 0.6 - s) for s in GREY)
    assert charge[115.0] <= need / 0.92 + 0.5


def test_the_night_ends_by_seven_without_morning_sun():
    # A solar counter that read nothing until noon must not make the night
    # (and a forced night charge) run until noon.
    solar = [0.0] * 12 + SUN[12:]
    day = _day("2026-04-23", solar, tags=["sunny"])
    assert night_end(day, NIGHT) == 7
    assert night_end(day, {**NIGHT, "night_latest_end": 6.0}) == 6


# ── the invariants ──────────────────────────────────────────────────


def _dates(start, n):
    d0 = datetime.fromisoformat(start)
    return [(d0 + timedelta(days=i)).date().isoformat() for i in range(n)]


def _scenario(name):
    if name == "sunny":
        return [_day(d, SUN, tags=["sunny"]) for d in _dates("2026-05-01", 4)]
    if name == "grey":
        return [_day(d, GREY, tags=["grey"]) for d in _dates("2026-11-01", 4)]
    if name == "sunny_then_grey":
        d = _dates("2026-04-10", 4)
        return ([_day(d[0], SUN, tags=["sunny"])]
                + [_day(x, GREY, tags=["grey"]) for x in d[1:]])
    if name == "negative":
        rates = list(RATES)
        rates[1] = rates[2] = -0.04
        rates[21] = -0.01
        return [_day(d, GREY if i % 2 else SUN, tags=["cheap"], rates=rates)
                for i, d in enumerate(_dates("2026-06-01", 4))]
    if name == "no_forecast":
        return [_day(d, SUN if i % 2 == 0 else GREY, tags=["mixed"],
                     forecast=None) for i, d in enumerate(_dates("2026-04-01", 4))]
    if name == "gaps":
        d = _dates("2026-03-01", 9)
        keep = [d[0], d[1], d[3], d[4], d[8]]       # 1 missing, then 3
        return [_day(x, SUN if i % 2 == 0 else GREY, tags=["mixed"],
                     soc=30.0 + 10 * i) for i, x in enumerate(keep)]
    raise KeyError(name)


SCENARIOS = ("sunny", "grey", "sunny_then_grey", "negative", "no_forecast",
             "gaps")


@pytest.mark.parametrize("cfg", [PROD, NIGHT], ids=["own_rules", "night_charge"])
@pytest.mark.parametrize("name", SCENARIOS)
def test_a_bigger_pack_never_makes_the_period_dearer(name, cfg):
    days = _scenario(name)
    costs = [_period_cost(_run(days, cap, cfg)) for cap in SIZES]
    # the losses a bigger pack can explain: its round trip on what it
    # shifts — a cent a day is far below a 1 kWh round-trip loss
    slack = 0.01 * len(days)
    for small, big in zip(costs, costs[1:], strict=False):
        assert big <= small + slack, (name, costs)


@pytest.mark.parametrize("cfg", [PROD, NIGHT], ids=["own_rules", "night_charge"])
def test_a_sunny_day_never_buys_more_with_a_bigger_pack(cfg):
    days = _scenario("sunny_then_grey") + _scenario("sunny")
    runs = {cap: _run(days, cap, cfg) for cap in SIZES}
    for i, day in enumerate(days):
        if "sunny" not in day.tags:
            continue
        for small, big in zip(SIZES, SIZES[1:], strict=False):
            a, b = runs[small].days[i], runs[big].days[i]
            # a price-justified night charge may buy more — nothing else
            extra = max(0.0, b.grid_charge_kwh - a.grid_charge_kwh)
            assert b.simulated["grid_import"] <= (
                a.simulated["grid_import"] + extra + 0.01), (day.date, small, big)


def test_a_bigger_reserve_starts_full_and_costs_nothing():
    # The real 15 kWh pack starts at 40 % (6 kWh) over a 20 % reserve. A
    # 115 kWh pack with the same 20 % reserve keeps 23 kWh it never spends:
    # it starts with that reserve filled, and the run values no gain from it.
    day = _day("2026-07-11", SUN, tags=["sunny"])
    small = _run([day], 15.0, PROD)
    big = _run([day], 115.0, PROD)
    assert big.days[0].start_kwh == pytest.approx(6.0 + 23.0 - 3.0)
    assert big.days[0].simulated["grid_import"] <= (
        small.days[0].simulated["grid_import"] + 0.01)
    assert _period_cost(big) <= _period_cost(small) + 0.01


# ── gaps ────────────────────────────────────────────────────────────


def test_a_gap_of_up_to_two_days_bridges_a_longer_one_restarts():
    params = SimParams.from_config(PROD, capacity_kwh=115.0)
    chain = PeriodChain(params, PROD)
    days = _scenario("gaps")
    res = [chain.add(d) for d in days]
    chain.finish()
    assert BRIDGE_MAX_DAYS == 2
    assert chain.stretches == 2                 # only the 3-day gap restarts
    assert "bridged" in res[2].story[0]
    assert res[4].story[0].startswith("starts from the recorded level")


def test_the_own_pack_over_a_bridge_follows_the_real_pack():
    # With its own size the simulated pack moves over the missing day as the
    # real one did: no stored-energy gain is made up by the bridge.
    a = _day("2026-03-01", GREY, tags=["grey"], soc=50.0)
    b = _day("2026-03-03", GREY, tags=["grey"], soc=70.0)
    run = _run([a, b], 15.0, PROD)
    assert run.days[1].start_kwh == pytest.approx(
        run.days[0].end_kwh + (70.0 - 50.0) / 100.0 * 15.0)
    assert run.stretches == 1


# ── the what-if levers ──────────────────────────────────────────────


def test_more_pv_buys_less_on_sunny_days():
    days = _scenario("sunny")
    base = _run(days, 15.0, PROD)
    more = _run(days, 15.0, PROD, pv_factor=1.5)
    for a, b in zip(base.days, more.days, strict=True):
        assert b.simulated["grid_import"] <= a.simulated["grid_import"] + 1e-6
        assert b.simulated["solar"] == pytest.approx(1.5 * a.simulated["solar"])
    assert _period_cost(more) < _period_cost(base)


def test_more_pv_is_clipped_at_the_inverter():
    day = _day("2026-05-02", SUN, tags=["sunny"])
    cfg = {**PROD, "inverter_ac_limit_w": 10000}
    res = simulate_day(day, SimParams.from_config(cfg, pv_factor=2.0), cfg)
    assert max(r["solar"] for r in res.hours) == pytest.approx(10.0)
    assert any("inverter limit" in s for s in res.story)


def test_battery_to_the_car_off_never_feeds_the_car_from_the_pack():
    # The car charges every evening on a sunny day; the full pack would
    # cover it in Solar + battery.
    ev = [0.0] * 24
    ev[19] = ev[20] = 4.0
    cfg = {**PROD, "ev_chargers": [{"id": "c", "charge_mode": "solar_plus_battery"}]}
    day = _day("2026-05-03", SUN, tags=["sunny", "car"], ev=ev, soc=95.0)
    on = simulate_day(day, SimParams.from_config(cfg), cfg, uncovered=0.0)
    off = simulate_day(day, SimParams.from_config(cfg, battery_to_ev=False), cfg,
                       uncovered=0.0)
    for h in (19, 20):
        house_gap = max(0.0, day.home[h] - day.solar[h])
        assert off.hours[h]["battery_discharge"] <= house_gap + 1e-9
        assert off.hours[h]["grid_import"] >= 4.0 - 1e-9
    assert on.hours[19]["battery_discharge"] > off.hours[19]["battery_discharge"]


@pytest.mark.parametrize("name", ("sunny_then_grey", "grey", "negative"))
def test_more_battery_power_never_makes_the_period_dearer(name):
    days = _scenario(name)
    slow = _run(days, 30.0, NIGHT, max_charge_w=2500.0, max_discharge_w=2500.0)
    fast = _run(days, 30.0, NIGHT, max_charge_w=10000.0, max_discharge_w=10000.0)

    def shifted(run):
        return sum(d.simulated["battery_discharge"] for d in run.days)

    assert shifted(fast) >= shifted(slow) - 0.01
    # The hourly model reaches the night target in fewer hours with more
    # power, so the packs end a few tenths of a kWh apart; that energy was
    # bought at the night price and is valued at the stretch's mean import
    # rate. Two cents a day covers it; a real fault is ten times that.
    assert _period_cost(fast) <= _period_cost(slow) + 0.02 * len(days)
