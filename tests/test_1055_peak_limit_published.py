"""#1055 — the Control tab showed 5 kW while the saved limit was "unlimited".

lostcontrol (Fronius, no charger, load management OFF) set the grid ceiling
to unlimited — and to any other number — and after a page refresh the Control
tab always read 5.0 kW.

Root cause: ``_build_load_management_data`` filled ``target_peak_limit`` and
``peak_limit_unlimited`` from the LoadManager only. With load management off
there is no LoadManager (#897), so the ``LoadManagementData`` defaults — 5.0
kW, limited — went out on ``sensor.sem_target_peak_limit`` as the user's
setting. The EV sizing meanwhile read the saved option (#716/#913), so what
SEM showed and what SEM did were two different ceilings.

Closure: one reader, ``_target_peak_limit_kw`` (+ ``_peak_limit_unlimited``),
used by both the EV sizing and the published sensor. The contradiction test
at the bottom pins that the published ceiling is the one control sizes
against, with and without a LoadManager.
"""
import ast
import inspect
import math
import textwrap
from unittest.mock import MagicMock, patch

import pytest

from custom_components.solar_energy_management.const import (
    DEFAULT_PEAK_LIMIT_UNLIMITED,
    DEFAULT_TARGET_PEAK_LIMIT,
)
from custom_components.solar_energy_management.coordinator.coordinator import (
    SEMCoordinator,
)
from custom_components.solar_energy_management.coordinator.types import (
    PowerReadings,
    SEMData,
)


def _coord(config, lm=None):
    with patch.object(SEMCoordinator, "__init__", return_value=None):
        coord = SEMCoordinator.__new__(SEMCoordinator)
    coord.config = dict(config)
    coord.hass = MagicMock()
    coord._load_manager = lm
    return coord


def _lm(target, unlimited=False):
    lm = MagicMock()
    lm.get_load_management_data = MagicMock(return_value={
        "target_peak_limit": target,
        "peak_limit_unlimited": unlimited,
        "state": "normal",
    })
    return lm


def _power(import_w=0.0):
    p = PowerReadings()
    p.grid_import_power = import_w
    return p


@pytest.mark.unit
class TestPublishedWithoutLoadManager:
    def test_the_reporters_unlimited_is_published(self):
        """The reporter's case: load management off, unlimited saved."""
        coord = _coord({"load_management_enabled": False,
                        "target_peak_limit": 5.0,
                        "peak_limit_unlimited": True})
        data = SEMData(
            load_management=coord._build_load_management_data(_power()),
        ).to_dict()
        assert data["peak_limit_unlimited"] is True

    def test_a_saved_number_is_published_not_the_default(self):
        """"I can set the target to whatever value, it is always 5 kW"."""
        coord = _coord({"load_management_enabled": False,
                        "target_peak_limit": 8.5})
        lm_data = coord._build_load_management_data(_power(2000.0))
        assert lm_data.target_peak_limit == 8.5
        assert lm_data.peak_limit_unlimited is False
        # Margin and percentage follow the real ceiling, not the 0.5 kW /
        # 0 % placeholders.
        assert lm_data.peak_margin == pytest.approx(6.5)
        assert lm_data.current_vs_peak_percentage == pytest.approx(2.0 / 8.5 * 100)

    def test_a_slider_write_shows_on_the_next_cycle(self):
        """The no-LoadManager service path (#913) writes through
        ``persist_global_option``; the next build must publish it."""
        from custom_components.solar_energy_management import (
            persist_global_option,
        )
        coord = _coord({"load_management_enabled": False,
                        "target_peak_limit": 5.0})
        entry = MagicMock()
        entry.data = {"target_peak_limit": 5.0}
        entry.options = {}
        hass = MagicMock()

        def _update_entry(e, options):
            e.options = options  # what HA's async_update_entry does

        hass.config_entries.async_update_entry.side_effect = _update_entry
        with patch(
            "custom_components.solar_energy_management._refresh_runtime_config"
        ):
            persist_global_option(hass, entry, coord,
                                  "target_peak_limit", 11.0)
            persist_global_option(hass, entry, coord,
                                  "peak_limit_unlimited", False)
        assert coord._build_load_management_data(_power()).target_peak_limit == 11.0

    def test_zero_config_publishes_the_defaults(self):
        coord = _coord({})
        lm_data = coord._build_load_management_data(_power())
        assert lm_data.target_peak_limit == DEFAULT_TARGET_PEAK_LIMIT
        assert lm_data.peak_limit_unlimited is DEFAULT_PEAK_LIMIT_UNLIMITED

    def test_an_unreadable_meter_does_not_break_the_publish(self):
        coord = _coord({"target_peak_limit": 6.0})
        p = PowerReadings()
        p.grid_import_power = None
        assert coord._build_load_management_data(p).target_peak_limit == 6.0

    @pytest.mark.parametrize("bad", [None, "abc"])
    def test_a_saved_value_that_is_not_a_number_keeps_a_limit(self, bad):
        """The publish now runs every cycle on every install, so a bad
        saved value must not stop the cycle — and must not read as "no
        limit" either."""
        coord = _coord({"target_peak_limit": bad})
        lm_data = coord._build_load_management_data(_power())
        assert lm_data.target_peak_limit == DEFAULT_TARGET_PEAK_LIMIT
        assert coord._get_peak_limit_w() == DEFAULT_TARGET_PEAK_LIMIT * 1000

    @pytest.mark.parametrize("with_lm", [False, True])
    def test_no_limit_is_zero_percent_not_percent_of_the_saved_number(
            self, with_lm):
        """Review finding: with no ceiling the saved 5.0 is not in force, so
        4.6 kW of import must not read 92 % (orange/red on three cards)."""
        cfg = {"target_peak_limit": 5.0, "peak_limit_unlimited": True}
        coord = _coord(cfg, lm=_lm(5.0, True) if with_lm else None)
        lm_data = coord._build_load_management_data(_power(4600.0))
        assert lm_data.current_vs_peak_percentage == 0.0
        # ...while a real 5 kW limit still reads 92 %.
        coord = _coord({"target_peak_limit": 5.0},
                       lm=_lm(5.0) if with_lm else None)
        assert coord._build_load_management_data(
            _power(4600.0)).current_vs_peak_percentage == pytest.approx(92.0)


@pytest.mark.unit
class TestLoadManagerStillWins:
    def test_the_live_manager_value_is_published(self):
        """The slider writes through a live LoadManager without a reload,
        so its value can be newer than ``coordinator.config``."""
        coord = _coord({"target_peak_limit": 6.0}, lm=_lm(9.0))
        lm_data = coord._build_load_management_data(_power(3000.0))
        assert lm_data.target_peak_limit == 9.0
        assert lm_data.peak_margin == pytest.approx(6.0)

    def test_the_live_unlimited_flag_is_published(self):
        coord = _coord({"peak_limit_unlimited": False}, lm=_lm(5.0, True))
        assert coord._build_load_management_data(_power()).peak_limit_unlimited is True

    def test_a_failing_manager_falls_back_to_the_saved_option(self):
        lm = MagicMock()
        lm.get_load_management_data = MagicMock(side_effect=RuntimeError)
        coord = _coord({"target_peak_limit": 7.0}, lm=lm)
        assert coord._build_load_management_data(_power()).target_peak_limit == 7.0


@pytest.mark.unit
class TestShownCeilingIsTheSizedCeiling:
    """The guard for the shape: what the sensor says the ceiling is and what
    the EV sizing uses must never disagree, whoever holds the value."""

    @pytest.mark.parametrize("with_lm", [False, True])
    @pytest.mark.parametrize("target,unlimited", [
        (3.0, False), (8.5, False), (25.0, False), (5.0, True), (80.0, True),
    ])
    def test_published_matches_sized(self, with_lm, target, unlimited):
        cfg = {"target_peak_limit": target, "peak_limit_unlimited": unlimited,
               "load_management_enabled": with_lm}
        coord = _coord(cfg, lm=_lm(target, unlimited) if with_lm else None)
        lm_data = coord._build_load_management_data(_power())
        sized_w = coord._get_peak_limit_w()
        assert lm_data.peak_limit_unlimited is unlimited
        if unlimited:
            assert math.isinf(sized_w)
        else:
            assert lm_data.target_peak_limit * 1000.0 == sized_w

    def test_the_builder_does_not_read_the_ceiling_from_the_manager_dict(self):
        """Structural: the ceiling must come from the shared reader, not from
        ``lm_info`` inside the LoadManager-only branch — that read is the
        one that disappears when load management is off."""
        src = textwrap.dedent(
            inspect.getsource(SEMCoordinator._build_load_management_data))
        tree = ast.parse(src)
        reads = {
            node.args[0].value
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "lm_info"
            and node.args and isinstance(node.args[0], ast.Constant)
        }
        assert "target_peak_limit" not in reads
        assert "peak_limit_unlimited" not in reads
        calls = {
            node.func.attr for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "self"
        }
        assert {"_target_peak_limit_kw", "_peak_limit_unlimited"} <= calls
