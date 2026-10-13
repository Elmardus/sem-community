"""#1105 — home power jumped once on a battery dropout (PROD 13.10.2026).

HA-PROD (Huawei), times UTC:

    19:29:40  grid    -2 (import    2)  battery -1535 (discharging)  home 1537
    19:29:48  meter -1614: the battery has STOPPED (reserve reached) …
    19:29:46  … but its register still says -1450 and polls slower
    19:30:25  SEM cycle reads grid -1614 and battery -1450 → home 3064
    19:30:25  the battery register goes unavailable
    19:30:55  battery 9 W, grid -1649 → home 1649

The #1047 hold covers a DARK input. Here every input was live: the
battery's last reading was simply older than the meter's, so its stop
showed up twice (as grid import AND as discharge). The spike guard did
not fire (1527 W < its 2 kW). Pinned: a grid step that matches the
battery's still-reported discharge, while that discharge did not fall,
holds the SHOWN home until the battery reports again — briefly, and
never the steering figure.
"""
from __future__ import annotations

import pytest

from custom_components.solar_energy_management.coordinator.coordinator import (
    SEMCoordinator,
)
from custom_components.solar_energy_management.coordinator.types import (
    PowerReadings,
    SEMData,
)


def _coord(clock):
    c = SEMCoordinator.__new__(SEMCoordinator)
    c._shown_home_clock = lambda: clock[0]
    return c


def _p(home, imp, dis, **flags):
    p = PowerReadings()
    p.home_consumption_power = float(home)
    p.grid_import_power = float(imp)
    p.battery_discharge_power = float(dis)
    for k, v in flags.items():
        setattr(p, k, v)
    return p


def _shown(p):
    return SEMData(power=p).to_dict()["home_consumption_power"]


def test_prod_13_10_battery_stop_does_not_jump_home():
    clock = [1000.0]
    c = _coord(clock)
    c._hold_shown_home(_p(1537, 2, 1535))                 # 19:29:40
    clock[0] = 1045.0
    p = _p(3064, 1614, 1450)                               # 19:30:25 stale battery
    c._hold_shown_home(p)
    assert _shown(p) == pytest.approx(1537)
    assert p.home_consumption_power == pytest.approx(3064)  # steering untouched
    clock[0] = 1075.0
    p = _p(1649, 1649, 0)                                  # 19:30:55 battery reports 0
    c._hold_shown_home(p)
    assert _shown(p) == pytest.approx(1649)


def test_a_real_load_is_shown_after_a_short_hold():
    """An appliance the same size as the discharge, and the battery truly
    still discharging: the hold lets go within its window."""
    clock = [1000.0]
    c = _coord(clock)
    c._hold_shown_home(_p(1537, 2, 1535))
    for t in (1010.0, 1020.0, 1030.0, 1040.0, 1050.0, 1060.0, 1070.0,
              1080.0, 1090.0, 1100.0, 1110.0):
        clock[0] = t
        p = _p(3064, 1614, 1450)
        c._hold_shown_home(p)
    assert _shown(p) == pytest.approx(3064)


def test_a_step_unlike_the_discharge_is_shown_at_once():
    clock = [1000.0]
    c = _coord(clock)
    c._hold_shown_home(_p(1537, 2, 1535))
    clock[0] = 1010.0
    p = _p(4537, 3002, 1535)          # a 3 kW kettle+oven, battery unchanged
    c._hold_shown_home(p)
    assert _shown(p) == pytest.approx(4537)


def test_no_battery_discharge_no_hold():
    clock = [1000.0]
    c = _coord(clock)
    c._hold_shown_home(_p(500, 500, 0))
    clock[0] = 1010.0
    p = _p(2100, 2100, 0)
    c._hold_shown_home(p)
    assert _shown(p) == pytest.approx(2100)
