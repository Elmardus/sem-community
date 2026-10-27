"""(#1080) The closed-meter grant: the car starts below its floor when the
sun it absorbs costs more to export than the grid costs to fill the gap."""
from types import SimpleNamespace

from custom_components.solar_energy_management.coordinator.coordinator import (
    SEMCoordinator,
)
from custom_components.solar_energy_management.coordinator.curtailment import (
    absorb_grant_w,
)

BASE = dict(closed=True, ev_connected=True, wants_solar=True,
            solar_w=3000.0, home_w=500.0, battery_charge_w=0.0,
            floor_w=4140.0, export_value=-0.08, import_price=0.25)


def grant(**kw):
    return absorb_grant_w(**{**BASE, **kw})


class TestAbsorbGrant:
    def test_fills_the_gap_when_cheaper_than_the_loss(self):
        # sun 2500 W, gap 1640 W: 1640 x 0.25 = 410 > 2500 x 0.08 = 200
        assert grant() == 0.0
        # a deeper loss tips it: 2500 x 0.20 = 500 > 410
        assert grant(export_value=-0.20) == 4140.0 - 2500.0

    def test_open_meter_grants_nothing(self):
        assert grant(closed=False, export_value=-0.5) == 0.0

    def test_positive_value_grants_nothing(self):
        assert grant(export_value=0.03) == 0.0

    def test_sun_already_reaches_the_floor(self):
        assert grant(solar_w=5000.0, export_value=-0.5) == 0.0

    def test_pack_takes_its_share_first(self):
        # the pack charging 2000 W leaves 500 W of sun: gap 3640 x 0.25
        # = 910 > 500 x 0.5 = 250 -> no grant
        assert grant(battery_charge_w=2000.0, export_value=-0.5) == 0.0

    def test_no_sun_no_grant(self):
        assert grant(solar_w=400.0, export_value=-1.0) == 0.0

    def test_unknown_price_is_no(self):
        assert grant(import_price=None, export_value=-0.5) == 0.0
        assert grant(export_value=None) == 0.0
        assert grant(export_value="x", import_price="y") == 0.0

    def test_no_car_or_no_solar_mode(self):
        assert grant(ev_connected=False, export_value=-0.5) == 0.0
        assert grant(wants_solar=False, export_value=-0.5) == 0.0

    def test_grant_holds_while_the_car_charges(self):
        # measured on the solar side: the car's own draw never enters
        a = grant(export_value=-0.5)
        assert a > 0 and grant(export_value=-0.5) == a


class _Prov:
    def get_current_export_rate(self):
        return -0.20

    def get_current_import_rate(self):
        return 0.25


class _Host:
    _absorb_grant_w = SEMCoordinator._absorb_grant_w

    def __init__(self, threshold, state="closed", switch=True):
        self.hass = None
        self.config = {"ev_chargers": [], "ev_min_current": 6,
                       "ev_absorb_grid_fill": switch}
        self._tariff_provider = _Prov()
        self._sink_verdicts = {"grid_export": SimpleNamespace(state=state)}
        self._threshold = threshold

    def _active_export_threshold(self):
        return self._threshold


POWER = SimpleNamespace(ev_connected=True, solar_power=3000.0,
                        home_consumption_power=500.0, battery_charge_power=0.0)


class TestCoordinatorGate:
    def test_off_without_the_value_rules(self, monkeypatch):
        """No value rules, no grant: the meter closes on a negative price
        and everything stays as it was."""
        h = _Host(threshold=None)
        assert h._absorb_grant_w(POWER) == 0.0

    def test_switch_off_buys_nothing_even_when_everything_else_fits(
            self, monkeypatch):
        """The opt-in: off is identical to before (#1080)."""
        from custom_components.solar_energy_management.coordinator import (
            curtailment,
        )
        monkeypatch.setattr(curtailment, "solar_mode_floor",
                            lambda hass, cfg: (True, 4140.0))
        h = _Host(0.0, switch=False)
        assert h._absorb_grant_w(POWER) == 0.0
        assert h._absorb_last == {"grant_w": 0.0}

    def test_prices_still_loading_after_a_restart_grant_nothing(
            self, monkeypatch):
        from custom_components.solar_energy_management.coordinator import (
            curtailment,
        )
        monkeypatch.setattr(curtailment, "solar_mode_floor",
                            lambda hass, cfg: (True, 4140.0))
        h = _Host(0.0)
        h._tariff_provider = SimpleNamespace(
            get_current_export_rate=lambda: None,
            get_current_import_rate=lambda: None)
        assert h._absorb_grant_w(POWER) == 0.0

    def test_no_verdict_yet_grants_nothing(self):
        h = _Host(0.0)
        h._sink_verdicts = {}
        assert h._absorb_grant_w(POWER) == 0.0

    def test_a_charging_car_does_not_change_the_grant(self, monkeypatch):
        """Measured on the solar side: the car's own draw is not an input."""
        from custom_components.solar_energy_management.coordinator import (
            curtailment,
        )
        monkeypatch.setattr(curtailment, "solar_mode_floor",
                            lambda hass, cfg: (True, 4140.0))
        h = _Host(0.0)
        a = h._absorb_grant_w(POWER)
        drawing = SimpleNamespace(**{**vars(POWER), "ev_power": 4000.0})
        assert a > 0 and h._absorb_grant_w(drawing) == a

    def test_open_meter_grants_nothing(self):
        assert _Host(0.0, state="open")._absorb_grant_w(POWER) == 0.0

    def test_closed_meter_with_rules_grants_the_gap(self, monkeypatch):
        from custom_components.solar_energy_management.coordinator import (
            curtailment,
        )
        monkeypatch.setattr(curtailment, "solar_mode_floor",
                            lambda hass, cfg: (True, 4140.0))
        h = _Host(0.0)
        assert h._absorb_grant_w(POWER) == 4140.0 - 2500.0
        assert h._absorb_last["grant_w"] == 1640
