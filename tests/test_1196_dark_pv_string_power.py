"""#1196 — a PV string's own power entity showed 0 W while its sensor was dark.

With two or more strings, ``sensor.sem_pv_string_pv2_power`` read 0 W while
string 2's own sensor was unavailable. A string at night also reads 0 W, so
the two looked the same. The total solar power already says unavailable
when every solar sensor is dark (#818); each string had no such rule: the
reader returned its 0.0 fallback and ``to_dict`` published it as a reading.

Each string now carries its own read flag
(``PowerReadings.solar_power_per_string_read``) and publishes None while it
is not set — the rule a dark battery unit keeps (#1170, class 163). Links,
each driven for real:

* the real reader (Energy-Dashboard and legacy paths) → ``SEMData.to_dict``
  for a dark, unknown and not-a-number string, and a V+I pair with one half
  dark; the watts for the sums stay 0.0;
* the twins: a live 0 W and a string asleep at night (#1065) still read
  0 W; a string never marked read publishes none;
* the real ``SEMSolarSensor`` holds the string's last value for the
  dark-read grace and then shows unavailable — never 0 W.
"""
from __future__ import annotations

import pytest
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntityDescription,
)

from custom_components.solar_energy_management.consts.core import (
    SENSOR_DARK_READ_GRACE_S,
)
from custom_components.solar_energy_management.coordinator.types import (
    SEMData,
    StringEnergy,
)
from custom_components.solar_energy_management.ha_energy_reader import (
    EnergyDashboardConfig,
)
from custom_components.solar_energy_management.sensor import SEMSolarSensor
from custom_components.solar_energy_management.tests.test_1065_asleep_solar_reads_zero import (  # noqa: E501
    FRESH_S,
    STALE_S,
    _reader,
    _state,
)

PV1 = "sensor.inv_pv1_power"
PV2 = "sensor.inv_pv2_power"
INV = "sensor.inv_ac_power"


def _ed_reader(states, sun="above_horizon"):
    """The strings beside one inverter total (the usual shape)."""
    reader, table = _reader({INV: _state(3000, FRESH_S), **states}, sun=sun)
    reader.set_energy_dashboard_config(EnergyDashboardConfig(
        solar_power=INV, solar_power_list=[INV], has_solar=True))
    reader.set_pv_strings({"pv1_power": PV1, "pv2_power": PV2})
    return reader, table


def _published(reader):
    data = SEMData()
    data.power = reader.read_power()
    return data.power, data.to_dict()


# ── 1. The reader → to_dict ────────────────────────────────────────────────

class TestADarkStringPublishesNoPower:
    def test_the_issue_case_pv2_dark_pv1_live(self):
        power, out = _published(_ed_reader({
            PV1: _state(1800, FRESH_S), PV2: _state("unavailable", FRESH_S),
        })[0])
        assert out["pv_string_pv2_power"] is None, "not the reader's 0.0"
        assert out["pv_string_pv1_power"] == 1800.0
        assert power.solar_power_per_string == {"pv1": 1800.0, "pv2": 0.0}, (
            "the sums still take 0.0 for the dark string")
        assert power.solar_power_per_string_read == {"pv1": True, "pv2": False}
        # The total comes from the inverter, which still answers.
        assert out["solar_power"] == 3000.0

    @pytest.mark.parametrize("dark", ["unavailable", "unknown", "none"],
                             ids=["unavailable", "unknown", "not-a-number"])
    def test_every_shape_of_no_reading(self, dark):
        _, out = _published(_ed_reader({
            PV1: _state(900, FRESH_S), PV2: _state(dark, FRESH_S),
        })[0])
        assert out["pv_string_pv2_power"] is None
        assert out["pv_string_pv1_power"] == 900.0

    def test_a_missing_entity_is_no_reading(self):
        _, out = _published(_ed_reader({PV1: _state(900, FRESH_S)})[0])
        assert out["pv_string_pv2_power"] is None

    def test_a_live_0_w_still_reads_0_w(self):
        """The twin: a string giving 0 W is a reading."""
        _, out = _published(_ed_reader({
            PV1: _state(900, FRESH_S), PV2: _state(0, FRESH_S),
        })[0])
        assert out["pv_string_pv2_power"] == 0.0

    def test_a_string_asleep_at_night_reads_0_w(self):
        """The #1065 twin: a quiet string at night is a real 0 W, not dark."""
        power, out = _published(_ed_reader({
            PV1: _state(8.1, STALE_S), PV2: _state(0.0, STALE_S),
        }, sun="below_horizon")[0])
        assert out["pv_string_pv1_power"] == 0.0
        assert out["pv_string_pv2_power"] == 0.0
        assert power.solar_power_per_string == {"pv1": 0.0, "pv2": 0.0}

    def test_both_strings_dark(self):
        _, out = _published(_ed_reader({
            PV1: _state("unavailable", FRESH_S),
            PV2: _state("unavailable", FRESH_S),
        })[0])
        assert out["pv_string_pv1_power"] is None
        assert out["pv_string_pv2_power"] is None

    def test_the_legacy_path_keeps_the_same_rule(self):
        reader, _ = _reader({
            INV: _state(3000, FRESH_S), PV1: _state(1800, FRESH_S),
            PV2: _state("unavailable", FRESH_S),
        }, sun="above_horizon", config={"solar_production_sensor": INV})
        reader.set_pv_strings({"pv1_power": PV1, "pv2_power": PV2})
        assert not reader._energy_dashboard_config, "the legacy path ran"
        _, out = _published(reader)
        assert out["pv_string_pv2_power"] is None
        assert out["pv_string_pv1_power"] == 1800.0
        assert out["solar_power"] == 3000.0, "the total read its own sensor"

    def test_a_single_string_publishes_nothing(self):
        """The gate is unchanged: one string has no per-string surface."""
        reader, _ = _ed_reader({PV1: _state(900, FRESH_S)})
        reader.set_pv_strings({"pv1_power": PV1})
        power, out = _published(reader)
        assert power.solar_power_per_string == {}
        assert not getattr(power, "solar_power_per_string_read", {})
        assert not [k for k in out if k.startswith("pv_string_")]


class TestAVoltageCurrentPairNeedsBothHalves:
    V1, I1 = "sensor.inv_pv_1_voltage", "sensor.inv_pv_1_current"
    V2, I2 = "sensor.inv_pv_2_voltage", "sensor.inv_pv_2_current"

    def _pair_reader(self, v2, i2):
        reader, _ = _reader({
            INV: _state(3000, FRESH_S),
            self.V1: _state(400, FRESH_S, unit="V"),
            self.I1: _state(5, FRESH_S, unit="A"),
            self.V2: _state(v2, FRESH_S, unit="V"),
            self.I2: _state(i2, FRESH_S, unit="A"),
        }, sun="above_horizon")
        reader.set_energy_dashboard_config(EnergyDashboardConfig(
            solar_power=INV, solar_power_list=[INV], has_solar=True))
        reader.set_pv_strings({}, {"pv1": (self.V1, self.I1),
                                   "pv2": (self.V2, self.I2)})
        return reader

    @pytest.mark.parametrize("v2,i2,expected", [
        (300, 2, 600.0),
        ("unavailable", 2, None),     # half a pair is no number
        (300, "unavailable", None),
        (300, 0, 0.0),                # a live 0 A is a reading
    ])
    def test_pair(self, v2, i2, expected):
        power, out = _published(self._pair_reader(v2, i2))
        assert out["pv_string_pv2_power"] == expected
        assert out["pv_string_pv1_power"] == 2000.0
        assert power.solar_power_per_string["pv2"] == (expected or 0.0)


class TestToDict:
    def test_a_string_never_marked_read_publishes_no_power(self):
        """The flag is opt-in: a reading nobody said was read is none."""
        data = SEMData()
        data.power.solar_power_per_string = {"pv1": 0.0, "pv2": 1200.0}
        data.power.solar_power_per_string_read = {"pv2": True}
        out = data.to_dict()
        assert out["pv_string_pv1_power"] is None
        assert out["pv_string_pv2_power"] == 1200.0

    def test_the_daily_energy_of_a_dark_string_stays_a_number(self):
        """The day's energy is an integral and keeps its count."""
        data = SEMData()
        data.power.solar_power_per_string = {"pv1": 900.0, "pv2": 0.0}
        data.power.solar_power_per_string_read = {"pv1": True, "pv2": False}
        data.energy_flows.per_string = {"pv1": StringEnergy(1.0),
                                        "pv2": StringEnergy(2.5)}
        out = data.to_dict()
        assert out["pv_string_pv2_power"] is None
        assert out["pv_string_pv2_daily_energy"] == 2.5


# ── 2. The entity: the fleet's grace, never a 0 W ──────────────────────────

def _entity(mock_coordinator):
    mock_coordinator.data = {"last_update": "x"}
    mock_coordinator.last_update_success = True
    desc = SensorEntityDescription(key="pv_string_pv2_power", name="x",
                                   device_class=SensorDeviceClass.POWER)
    return SEMSolarSensor(coordinator=mock_coordinator, description=desc,
                          entry_id="e")


def _cycle(entity, pv2_state, t):
    _, out = _published(_ed_reader({
        PV1: _state(1800, FRESH_S), PV2: _state(pv2_state, FRESH_S),
    })[0])
    entity.coordinator.data = out
    entity._now_monotonic = lambda: t
    entity._update_from_coordinator()


class TestTheEntityHoldsThenShowsUnavailable:
    def test_a_blink_holds_the_last_value_then_an_outage_blanks(
            self, mock_coordinator):
        power = _entity(mock_coordinator)
        _cycle(power, 1200, 1000.0)
        assert power.native_value == 1200.0 and power._attr_available

        _cycle(power, "unavailable", 1030.0)
        assert power._attr_available is True
        assert power.native_value == 1200.0, "held, not 0 W"
        assert power.extra_state_attributes.get("stale_s") == 30

        _cycle(power, "unavailable", 1000.0 + SENSOR_DARK_READ_GRACE_S + 1)
        assert power._attr_available is False
        assert power.native_value is None, "unavailable, not 0 W"

        _cycle(power, 0, 1000.0 + SENSOR_DARK_READ_GRACE_S + 30)
        assert power._attr_available is True
        assert power.native_value == 0.0, "a live 0 W comes back as 0 W"

    def test_never_read_is_unavailable_not_0_w(self, mock_coordinator):
        power = _entity(mock_coordinator)
        _cycle(power, "unavailable", 1000.0)
        assert power._attr_available is False
        assert power.native_value is None
