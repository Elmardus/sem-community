"""#1047 — home power jumps when the battery reading drops out.

HA-PROD, 04.10.2026 (Huawei), 10 s cycles:

    15:25:24  solar 4596   battery +3916 (charging)   grid +6    home  674
    15:25:49  solar unavailable
    15:26:01  battery unavailable
    15:26:03  grid unavailable
    15:26:24  solar 4676   battery dark               grid dark  home 4676  <- 20 s
    15:26:44  solar 4676   battery +4094              grid +38   home  544

``sensor.sem_battery_power`` kept 3916 W through the gap (the entity's
dark-read grace). Home was shown straight from the reader's 0 W fallback,
so the whole solar output landed on the house. The #237/#444 hold catches
a dip only; its 2-cycle spike guard had been spent by the dip hold on the
solar-dark cycles before.

Pinned here: the SHOWN home (entity, ``coordinator.data``, the cards'
snapshot) keeps its last value while any input of it reads dark, for the
same grace as the input entities. The STEERING home is untouched: it
steers beside the raw battery/grid fields, and a held home next to a 0 W
battery charge is a surplus that is not there (the first cut of this fix
held the battery inside the sum and the review measured a 3.9 kW phantom
EV surplus from exactly that).
"""
from __future__ import annotations

import ast
import inspect
import textwrap
from unittest.mock import MagicMock, Mock

import pytest

from custom_components.solar_energy_management.consts.core import (
    SENSOR_DARK_READ_GRACE_S,
)
from custom_components.solar_energy_management.coordinator.coordinator import (
    SEMCoordinator,
)
from custom_components.solar_energy_management.coordinator.sensor_reader import (
    SensorReader,
)
from custom_components.solar_energy_management.coordinator.types import (
    PowerReadings,
    SEMData,
)
from custom_components.solar_energy_management.tests.ast_contracts import (
    _callee_name,
    reads_flag,
)

GRACE = SENSOR_DARK_READ_GRACE_S


def _state(value):
    import homeassistant.util.dt as dt_util
    s = Mock()
    s.state = str(value)
    s.attributes = {"unit_of_measurement": "W"}
    s.last_updated = s.last_reported = dt_util.utcnow()
    return s


class _House:
    """A real reader over three sensors the test can move, and the
    coordinator steps that turn its reading into what SEM shows."""

    def __init__(self):
        self.values = {"sensor.pv": "0", "sensor.grid": "0", "sensor.bat": "0"}
        self.clock = [1000.0]
        hass = MagicMock()
        hass.states = MagicMock()
        hass.states.get = lambda eid: (
            _state(self.values[eid]) if eid in self.values else None)
        self.reader = SensorReader(hass, {
            "solar_production_sensor": "sensor.pv",
            "grid_power_sensor": "sensor.grid",
            "battery_power_sensor": "sensor.bat",
        })
        self.reader._energy_dashboard_config = None
        self.coord = SEMCoordinator.__new__(SEMCoordinator)
        self.coord._shown_home_clock = lambda: self.clock[0]

    def cycle(self, at, *, pv, grid, bat):
        self.clock[0] = at
        self.values.update(
            {"sensor.pv": pv, "sensor.grid": grid, "sensor.bat": bat})
        p = self.reader.read_power()
        self.coord._smooth_home_consumption(p)
        self.coord._hold_shown_home(p)
        return p, SEMData(power=p).to_dict(), self.coord._build_power_snapshot(p)


def _replay():
    h = _House()
    out = {}
    out["15:25:24"] = h.cycle(1000, pv=4596, grid=6, bat=3916)
    out["15:25:54"] = h.cycle(1030, pv="unavailable", grid=6, bat=3916)
    out["15:26:04"] = h.cycle(1040, pv="unavailable", grid="unavailable",
                              bat="unavailable")
    out["15:26:14"] = h.cycle(1050, pv="unavailable", grid="unavailable",
                              bat="unavailable")
    out["15:26:24"] = h.cycle(1060, pv=4676, grid="unavailable",
                              bat="unavailable")
    out["15:26:34"] = h.cycle(1070, pv=4676, grid="unavailable",
                              bat="unavailable")
    out["15:26:44"] = h.cycle(1080, pv=4676, grid=38, bat=4094)
    return out


@pytest.mark.unit
class TestTheProdAfternoon:
    def test_the_shown_home_never_jumps_to_the_solar_output(self):
        out = _replay()
        for t in ("15:25:54", "15:26:04", "15:26:14", "15:26:24", "15:26:34"):
            _p, data, _snap = out[t]
            assert data["home_consumption_power"] == 674, (
                f"{t}: shown home {data['home_consumption_power']} W — a dark "
                f"input's 0 W was shown as house load")
        assert out["15:26:44"][1]["home_consumption_power"] == 544

    def test_the_bug_is_reproduced_without_the_hold(self):
        """Vacuity: the same replay, shown straight from the steering
        figure, is the PROD jump."""
        p, _data, _snap = _replay()["15:26:24"]
        assert p.home_consumption_power == pytest.approx(4676)

    def test_the_cards_keep_the_last_set_that_added_up(self):
        out = _replay()
        _p, _d, snap = out["15:26:24"]
        assert snap["held"] is True
        assert snap["home_w"] == pytest.approx(674)
        assert snap["battery_w"] == pytest.approx(3916), (
            "the cards showed a 0 W battery beside a 4.7 kW house")
        assert out["15:26:44"][2]["held"] is False

    def test_the_input_entities_and_their_halves_go_quiet(self):
        _p, data, _snap = _replay()["15:26:24"]
        for k in ("battery_power", "battery_charge_power",
                  "battery_discharge_power", "grid_power",
                  "grid_import_power", "grid_export_power"):
            assert data[k] is None, k
        assert data["solar_power"] == 4676


@pytest.mark.unit
class TestSteeringIsNotTouched:
    def test_the_hold_never_writes_the_steering_figure(self):
        c = SEMCoordinator.__new__(SEMCoordinator)
        c._shown_home_clock = lambda: 1000.0
        p = PowerReadings()
        p.home_consumption_power = 674.0
        c._hold_shown_home(p)
        p.home_consumption_power = 4676.0
        p.battery_power_all_unavailable = True
        c._hold_shown_home(p)
        assert p.home_shown_w == pytest.approx(674)
        assert p.home_consumption_power == pytest.approx(4676)

    def test_no_phantom_ev_surplus_from_a_dark_battery(self):
        """solar − home − battery charge, the EV budget's own sum, on the
        dark cycle: the raw set says the meter's answer (≈ 0), never the
        3.9 kW a held home beside a 0 W battery charge would invent."""
        p, _d, _s = _replay()["15:26:24"]
        surplus = max(0.0, p.solar_power - p.home_consumption_power
                      - p.battery_charge_power)
        assert surplus < 100.0, f"phantom surplus {surplus:.0f} W"
        assert p.inputs_degraded is True


@pytest.mark.unit
class TestTheHoldEndsWhereTheEntityLetsGo:
    def _coord(self, clock):
        c = SEMCoordinator.__new__(SEMCoordinator)
        c._shown_home_clock = lambda: clock[0]
        return c

    def _p(self, home, **flags):
        p = PowerReadings()
        p.home_consumption_power = float(home)
        for k, v in flags.items():
            setattr(p, k, v)
        return p

    @pytest.mark.parametrize("flag", [
        "solar_power_unavailable", "grid_power_unavailable",
        "battery_power_all_unavailable"])
    def test_each_dark_input_holds_the_shown_home(self, flag):
        clock = [1000.0]
        c = self._coord(clock)
        c._hold_shown_home(self._p(674))
        clock[0] = 1010.0
        p = self._p(4676, **{flag: True})
        c._hold_shown_home(p)
        assert SEMData(power=p).to_dict()["home_consumption_power"] == 674

    def test_the_grace_boundary_is_the_entity_boundary(self):
        """Same ``age <= grace`` as ``SEMSolarSensor``: held at the edge,
        released one second past it — the input entities blank there too,
        and home shows the computed value again (never unknown)."""
        clock = [1000.0]
        c = self._coord(clock)
        c._hold_shown_home(self._p(674))
        clock[0] = 1000.0 + GRACE
        p = self._p(4676, battery_power_all_unavailable=True)
        c._hold_shown_home(p)
        assert p.home_shown_w == pytest.approx(674)
        clock[0] = 1000.0 + GRACE + 1
        p = self._p(4676, battery_power_all_unavailable=True)
        c._hold_shown_home(p)
        assert p.home_shown_w is None
        assert SEMData(power=p).to_dict()["home_consumption_power"] == 4676

    def test_nothing_shown_yet_is_never_held(self):
        """A restart into a dropout: there is no last value to keep."""
        c = self._coord([1000.0])
        p = self._p(4676, battery_power_all_unavailable=True)
        c._hold_shown_home(p)
        assert p.home_shown_w is None
        assert SEMData(power=p).to_dict()["home_consumption_power"] == 4676

    def test_the_held_value_is_the_last_one_shown(self):
        clock = [1000.0]
        c = self._coord(clock)
        c._hold_shown_home(self._p(674))
        clock[0] = 1010.0
        c._hold_shown_home(self._p(710))
        clock[0] = 1020.0
        p = self._p(4676, grid_power_unavailable=True)
        c._hold_shown_home(p)
        assert p.home_shown_w == pytest.approx(710)

    def test_a_live_cycle_shows_its_own_figure(self):
        clock = [1000.0]
        c = self._coord(clock)
        c._hold_shown_home(self._p(674))
        p = self._p(2500)
        c._hold_shown_home(p)
        assert p.home_shown_w is None
        assert SEMData(power=p).to_dict()["home_consumption_power"] == 2500


@pytest.mark.unit
class TestEachInputOnItsOwnClock:
    """Each input entity keeps its last value from ITS OWN last good read,
    so home holds exactly while one of them is still holding."""

    def _run(self, timeline):
        h = _House()
        shown = {}
        for at, pv, grid, bat in timeline:
            _p, data, _s = h.cycle(at, pv=pv, grid=grid, bat=bat)
            shown[at] = data["home_consumption_power"]
        return shown

    def test_blinks_that_overlap_in_turn_never_show_a_jump(self):
        """Solar dark 10–100 s, battery 90–200 s, grid 190–300 s: no cycle
        is fully live, but each input's own grace is fresh — releasing at
        180 s showed the solar output as house load again."""
        U = "unavailable"
        tl = [(1000, 4596, 6, 3916)]
        for t in range(1010, 1310, 10):
            o = t - 1000
            tl.append((t, U if 10 <= o < 100 else 4596,
                       U if 190 <= o < 300 else 6,
                       U if 90 <= o < 200 else 3916))
        tl.append((1310, 4596, 6, 3916))
        shown = self._run(tl)
        for t in range(1010, 1300, 10):
            assert shown[t] == 674, f"t+{t - 1000}s showed {shown[t]} W"
        assert shown[1310] == 674

    def test_a_chain_of_blinks_lets_go_after_two_graces(self):
        """Solar and battery blink in turn, overlapping, for ten minutes:
        each input's own grace stays fresh, so only the age of the held
        value can end the freeze — at two graces, and not before."""
        U = "unavailable"
        tl = [(1000, 4596, 6, 3916)]
        for t in range(1010, 1600, 10):
            phase = ((t - 1010) // 50) % 2          # 50 s solar, 50 s battery
            edge = (t - 1010) % 50 >= 40             # 10 s overlap
            pv_dark = phase == 0 or edge
            bat_dark = phase == 1 or edge
            tl.append((t, U if pv_dark else 4596, 6, U if bat_dark else 3916))
        shown = self._run(tl)
        assert all(shown[t] == 674 for t in range(1010, 1000 + 2 * GRACE + 1, 10)), (
            "released inside two graces")
        late = [shown[t] for t in range(1000 + 2 * GRACE + 10, 1600, 10)]
        assert any(v != 674 for v in late), "the held value never expired"

    def test_an_input_dark_all_night_does_not_stop_the_hold(self):
        """Solar unavailable for hours (an inverter asleep), battery grid-
        charging: a battery blink must still hold home. Its entity is blank
        and it is 0 W in home anyway, so it neither holds nor blocks."""
        U = "unavailable"
        tl = [(1000, 0, -4600, 3900)]                    # last solar read
        tl += [(t, U, -4600, 3900) for t in range(1010, 1200, 10)]
        # the kettle: house 700 → 1200 W, while solar is still dark
        tl += [(t, U, -5100, 3900) for t in range(1200, 1400, 10)]
        tl += [(1400, U, -5100, U), (1410, U, -5100, U), (1420, U, -5100, U)]
        shown = self._run(tl)
        assert shown[1390] == 1200, "solar dark past its grace: home is live"
        for t in (1400, 1410, 1420):
            assert shown[t] == 1200, f"{t}: battery blink showed {shown[t]} W"

    def test_an_input_never_read_does_not_hold(self):
        """Configured but dark since the restart: no entity value, no hold."""
        h = _House()
        _p, data, _s = h.cycle(1000, pv="unavailable", grid=-500, bat=0)
        assert data["home_consumption_power"] == 500


@pytest.mark.unit
class TestTheSplitFiguresFollowTheirInput:
    def _data(self, **flags):
        p = PowerReadings()
        p.solar_power, p.grid_power, p.battery_power = 4676.0, 6.0, 3916.0
        p.calculate_derived()
        for k, v in flags.items():
            setattr(p, k, v)
        return SEMData(power=p).to_dict()

    def test_healthy_cycle_publishes_numbers(self):
        d = self._data()
        assert d["battery_charge_power"] == 3916
        assert d["battery_discharge_power"] == 0
        assert d["grid_export_power"] == 6
        assert d["grid_import_power"] == 0

    def test_a_dark_battery_blanks_its_halves(self):
        d = self._data(battery_power_all_unavailable=True)
        assert d["battery_charge_power"] is None
        assert d["battery_discharge_power"] is None
        assert d["grid_export_power"] == 6, "only the dark input goes quiet"

    def test_a_dark_grid_blanks_its_halves(self):
        d = self._data(grid_power_unavailable=True)
        assert d["grid_import_power"] is None
        assert d["grid_export_power"] is None
        assert d["battery_charge_power"] == 3916


#: ``to_dict`` blanks these too, but they are not terms of the balance.
_NOT_A_BALANCE_INPUT = {"battery_soc_unavailable"}


def _blanking_flags(fn) -> set:
    """Every ``*_unavailable`` flag a function reads — through ``getattr``
    with a string or as a plain attribute."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    flags = set()
    for n in ast.walk(tree):
        if (isinstance(n, ast.Call) and getattr(n.func, "id", None) == "getattr"
                and len(n.args) >= 2 and isinstance(n.args[1], ast.Constant)
                and isinstance(n.args[1].value, str)):
            name = n.args[1].value
        elif isinstance(n, ast.Attribute):
            name = n.attr
        else:
            continue
        if name.endswith("_unavailable"):
            flags.add(name)
    return flags


@pytest.mark.unit
class TestTheHoldListensToEveryFlagThatBlanksAnInput:
    def test_every_input_the_entity_blanks_holds_the_shown_home(self):
        """Home is built from every power input ``to_dict`` can blank. A
        flag added there and not to the hold is this bug again for that
        input."""
        blanks = _blanking_flags(SEMData.to_dict) - _NOT_A_BALANCE_INPUT
        held_on = {flag for _n, flag in SEMCoordinator._SHOWN_HOME_INPUTS}
        assert len(blanks) >= 3, f"the parse is blind: {blanks}"
        assert blanks <= held_on, sorted(blanks - held_on)

    def test_the_hold_runs_after_smoothing_and_before_everything_shown(self):
        """Deleting the call leaves every unit test above green — so the
        cycle order is pinned here, on the parsed CALLS: after the last
        write of home, before anything that publishes it or the gap."""
        first = {}
        tree = ast.parse(textwrap.dedent(
            inspect.getsource(SEMCoordinator._async_update_data)))
        for n in ast.walk(tree):
            if isinstance(n, ast.Call):
                name = _callee_name(n)
                first[name] = min(first.get(name, n.lineno), n.lineno)
        order = ("_smooth_home_consumption", "_hold_shown_home",
                 "_build_charging_context", "SEMData", "_build_power_snapshot")
        missing = [c for c in order if c not in first]
        assert not missing, missing
        assert first["_smooth_home_consumption"] < first["_hold_shown_home"]
        for later in order[2:]:
            assert first["_hold_shown_home"] < first[later], later

    def test_the_house_meter_gap_compares_the_shown_figure(self):
        """(#891) The gap explains two dashboards that disagree; measured
        against the steering figure it read +4 kW while the entity said
        674 W."""
        assert reads_flag(SEMCoordinator._build_fleet_cycle_state,
                          "power", "home_shown_w")
