"""#1036 — a meter device next to a charger is not a charger.

Easee's integration ships the Equalizer as its own device beside the
charger. The Equalizer measures the house's grid import and export, and
``_discover_easee`` — like most brand functions — admits a device on a
power reading alone. So SEM offered the Equalizer as a second charger, with
the grid import as its charging power. Its entities come first in the
registry, so it was also the PRIMARY charger: the one
``discover_ev_charger_from_registry`` hands the config flow's first step and
the coordinator's late setup, which then drives it.

The entities below are the real ones from the Easee integration's own
entity descriptions (nordicopen/easee_hass), as the hardware-wave crawler
loaded them. The fix is generic, in the walk every brand shares — so the
last class of pins runs the same meter beside every brand SEM knows.
"""
import ast
import pathlib
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from custom_components.solar_energy_management import hardware_detection as hd
from custom_components.solar_energy_management.hardware_detection import (
    _CHARGER_ONLY_ROLES,
    _EV_CHARGER_PLATFORMS,
    _discover_easee,
    apply_charger_discovery_guards,
    build_detection_report,
    discover_all_ev_chargers_from_registry,
    discover_ev_charger_from_registry,
    meters_beside_chargers,
)

_PKG = pathlib.Path(__file__).resolve().parent.parent


def _ent(entity_id, device_id, device_class=None, platform="easee",
         unit=None, disabled=False, key=None):
    if disabled is True:
        disabled = "user"
    return SimpleNamespace(
        entity_id=entity_id, platform=platform, device_id=device_id,
        original_device_class=device_class,
        disabled_by=(disabled or None),
        config_entry_id="entry-1",
        unique_id=entity_id.split(".", 1)[1],
        translation_key=key, entity_category=None,
        original_unit_of_measurement=unit, unit_of_measurement=unit,
    )


#: Off until the user turns them on, as the integration ships them.
_OFF = "integration"


def _equalizer(dev="eq-QP123456", name="easee_equalizer_qp123456"):
    p = f"sensor.{name}"
    return [
        _ent(f"binary_sensor.{name}_online", dev, "connectivity",
             key="online"),
        _ent(f"{p}_current", dev, "current", unit="A", disabled=_OFF,
             key="current"),
        _ent(f"{p}_export_energy", dev, "energy", unit="kWh",
             key="export_energy"),
        _ent(f"{p}_export_power", dev, "power", unit="kW",
             key="export_power"),
        _ent(f"{p}_export_reactive_energy", dev, "energy", unit="kWh",
             key="export_reactive_energy"),
        _ent(f"{p}_export_reactive_power", dev, "power", unit="kW",
             key="export_reactive_power"),
        _ent(f"{p}_import_energy", dev, "energy", unit="kWh",
             key="import_energy"),
        _ent(f"{p}_import_power", dev, "power", unit="kW",
             key="import_power"),
        _ent(f"{p}_import_reactive_energy", dev, "energy", unit="kWh",
             key="import_reactive_energy"),
        _ent(f"{p}_import_reactive_power", dev, "power", unit="kW",
             key="import_reactive_power"),
        _ent(f"{p}_temp_max", dev, "temperature", unit="°C",
             key="internal_temperature"),
        _ent(f"{p}_voltage", dev, "voltage", unit="V", disabled=_OFF,
             key="voltage"),
        _ent(f"switch.{name}_surplus", dev, "current", key="surplus"),
    ]


def _charger(dev="box-EH123456", name="easee_home_eh123456",
             status="live", session="live"):
    """``status`` / ``session``: "live", "disabled", "renamed" or None."""
    p = f"sensor.{name}"
    ents = [
        _ent(f"binary_sensor.{name}_cable_locked", dev, "lock",
             key="cable_locked"),
        _ent(f"binary_sensor.{name}_online", dev, "connectivity",
             key="online"),
        _ent(f"{p}_circuit_current", dev, "current", unit="A",
             disabled=_OFF, key="circuit_current"),
        _ent(f"{p}_current", dev, "current", unit="A", disabled=_OFF,
             key="current"),
        _ent(f"{p}_dynamic_charger_limit", dev, "current", unit="A",
             disabled=_OFF, key="dynamic_charger_limit"),
        _ent(f"{p}_energy_last_hour", dev, "energy", unit="kWh",
             disabled=_OFF, key="energy_last_hour"),
        _ent(f"{p}_lifetime_energy", dev, "energy", unit="kWh",
             key="lifetime_energy"),
        _ent(f"{p}_max_charger_limit", dev, "current", unit="A",
             disabled=_OFF, key="max_charger_limit"),
        _ent(f"{p}_power", dev, "power", unit="kW", key="power"),
        _ent(f"{p}_voltage", dev, "voltage", unit="V", disabled=_OFF,
             key="voltage"),
        _ent(f"switch.{name}_is_enabled", dev, key="is_enabled"),
        _ent(f"switch.{name}_smart_charging", dev, key="smart_charging"),
    ]
    if session:
        ents.append(_ent(f"{p}_session_energy", dev, "energy", unit="kWh",
                         disabled=(session == "disabled"),
                         key="session_energy"))
    if status:
        eid = ("sensor.carport_toestand" if status == "renamed"
               else f"{p}_status")
        ents.append(_ent(eid, dev, disabled=(status == "disabled"),
                         key="easee_status"))
    return ents


def _registry(entries):
    reg = SimpleNamespace()
    reg.entities = {e.entity_id: e for e in entries}
    return reg


def _config_walk(entries):
    with patch.object(hd.entity_registry, "async_get",
                      return_value=_registry(entries)):
        return discover_all_ev_chargers_from_registry(MagicMock())


def _primary(entries):
    with patch.object(hd.entity_registry, "async_get",
                      return_value=_registry(entries)):
        return discover_ev_charger_from_registry(MagicMock())


class TestTheEaseeEqualizer:
    """The reporter's shape: the Equalizer first, the charger second."""

    def test_the_brand_function_alone_admits_the_equalizer(self):
        # The pre-fix rule, spelled out, so the pins below cannot pass
        # because the Equalizer was never admitted in the first place.
        eq = _equalizer()
        mapping = _discover_easee(eq)
        apply_charger_discovery_guards(mapping, eq)
        assert mapping["ev_charging_power_sensor"] == (
            "sensor.easee_equalizer_qp123456_import_power")
        assert mapping["ev_service_device_id"] == "eq-QP123456"
        assert not any(mapping.get(r) for r in _CHARGER_ONLY_ROLES)

    def test_the_config_path_offers_only_the_charger(self):
        found = _config_walk(_equalizer() + _charger())
        assert [c["_device_id"] for c in found] == ["box-EH123456"]
        assert found[0]["ev_charging_power_sensor"] == (
            "sensor.easee_home_eh123456_power")

    def test_the_primary_charger_is_the_charger_not_the_grid_meter(self):
        # The late setup builds THE charger from this one — it read the
        # house's grid import as EV power and steered the Equalizer.
        primary = _primary(_equalizer() + _charger())
        assert primary["ev_charging_power_sensor"] == (
            "sensor.easee_home_eh123456_power")
        assert primary["ev_service_device_id"] == "box-EH123456"

    def test_the_order_of_the_registry_does_not_matter(self):
        found = _config_walk(_charger() + _equalizer())
        assert [c["_device_id"] for c in found] == ["box-EH123456"]

    def test_the_report_names_the_equalizer_as_a_meter(self):
        rep = build_detection_report(
            registry=_registry(_equalizer() + _charger()))
        assert [c["device_id"] for c in rep["chargers"]] == ["box-EH123456"]
        assert [m["device_id"] for m in rep["meters"]] == ["eq-QP123456"]
        assert {e["entity"] for e in rep["meters"][0]["entities"]} == {
            e.entity_id for e in _equalizer() if not e.disabled_by}
        assert rep["near_misses"] == []

    def test_the_report_says_nothing_on_a_plain_install(self):
        rep = build_detection_report(registry=_registry(_charger()))
        assert rep["meters"] == []
        assert len(rep["chargers"]) == 1


class TestANearMissWaitsForThePlatform:
    """The report's near miss asked "has this brand a charger?" in registry
    order, so a site device listed BEFORE its charger still read as "almost
    supported — please report". The same order that made the Equalizer the
    primary charger."""

    def _site(self):
        return [_ent("binary_sensor.easee_site_online", "site",
                     "connectivity")]

    def test_the_site_device_alone_is_a_near_miss(self):
        # The pre-fix rule, so the next pin cannot pass vacuously.
        rep = build_detection_report(registry=_registry(self._site()))
        assert [n["device_id"] for n in rep["near_misses"]] == ["site"]

    def test_listed_before_its_charger_it_is_not_one(self):
        rep = build_detection_report(
            registry=_registry(self._site() + _charger()))
        assert rep["near_misses"] == []
        assert [c["device_id"] for c in rep["chargers"]] == ["box-EH123456"]

    def test_listed_after_its_charger_it_is_not_one_either(self):
        rep = build_detection_report(
            registry=_registry(_charger() + self._site()))
        assert rep["near_misses"] == []


class TestOnlyNextToACharger:
    """The sibling is the evidence. Without one, nothing changes."""

    def test_an_equalizer_alone_is_still_offered(self):
        # No charger beside it: the pre-fix answer stands. Dropping a unit
        # on missing roles alone would cost a box whose status sensor the
        # user disabled its only charger.
        found = _config_walk(_equalizer())
        assert [c["_device_id"] for c in found] == ["eq-QP123456"]

    def test_a_charger_with_its_status_disabled_is_still_offered_alone(self):
        ents = _charger(status="disabled", session="disabled")
        found = _config_walk(ents)
        assert [c["_device_id"] for c in found] == ["box-EH123456"]
        assert not any(found[0].get(r) for r in _CHARGER_ONLY_ROLES)

    def test_two_chargers_are_two_chargers(self):
        found = _config_walk(
            _equalizer()
            + _charger()
            + _charger(dev="box-EH654321", name="easee_home_eh654321"))
        assert [c["_device_id"] for c in found] == [
            "box-EH123456", "box-EH654321"]

    def test_a_second_charger_with_only_its_session_left_is_kept(self):
        found = _config_walk(
            _charger()
            + _charger(dev="box-EH654321", name="easee_home_eh654321",
                       status=None))
        assert len(found) == 2

    def test_a_second_charger_with_its_roles_disabled_is_kept(self):
        # The review of this fix: box 2 maps only its power, exactly like
        # the Equalizer — but its status and session are on the device,
        # only switched off. The whole device is the evidence.
        box2 = _charger(dev="box-EH654321", name="easee_home_eh654321",
                        status="disabled", session="disabled")
        mapping = _discover_easee([e for e in box2 if not e.disabled_by])
        assert not any(mapping.get(r) for r in _CHARGER_ONLY_ROLES)
        found = _config_walk(_equalizer() + _charger() + box2)
        assert [c["_device_id"] for c in found] == [
            "box-EH123456", "box-EH654321"]
        rep = build_detection_report(
            registry=_registry(_equalizer() + _charger() + box2))
        assert [m["device_id"] for m in rep["meters"]] == ["eq-QP123456"]

    def test_a_second_charger_with_its_status_renamed_is_kept(self):
        # A renamed id hides the status from the brand function, which
        # reads names. The integration's own key for it does not change.
        box2 = _charger(dev="box-EH654321", name="easee_home_eh654321",
                        status="renamed", session="disabled")
        mapping = _discover_easee([e for e in box2 if not e.disabled_by])
        assert not any(mapping.get(r) for r in _CHARGER_ONLY_ROLES)
        found = _config_walk(_charger() + box2)
        assert [c["_device_id"] for c in found] == [
            "box-EH123456", "box-EH654321"]

    def test_a_unit_with_a_charger_mark_is_never_called_a_meter(self):
        # A current number is a mark only a charger has (#814). Easee binds
        # no number, so the mapping shows no charger role — the mark alone
        # must keep the unit.
        marked = _equalizer() + [
            _ent("number.easee_equalizer_qp123456_charging_current",
                 "eq-QP123456", "current", unit="A")]
        found = _config_walk(marked + _charger())
        assert sorted(c["_device_id"] for c in found) == [
            "box-EH123456", "eq-QP123456"]

    def test_without_keys_to_compare_nothing_is_dropped(self):
        # The review of this fix: with no translation keys SEM cannot tell
        # the Equalizer from a second box whose roles are switched off — and
        # the heal at setup would act on that guess. So it does not guess.
        def bare(ents):
            for e in ents:
                e.translation_key = None
            return ents
        found = _config_walk(bare(_equalizer()) + bare(_charger()))
        assert sorted(c["_device_id"] for c in found) == [
            "box-EH123456", "eq-QP123456"]

    def test_a_disabled_mark_on_the_device_keeps_it_too(self):
        marked = _equalizer() + [
            _ent("number.easee_equalizer_qp123456_charging_current",
                 "eq-QP123456", "current", unit="A", disabled=True)]
        found = _config_walk(marked + _charger())
        assert sorted(c["_device_id"] for c in found) == [
            "box-EH123456", "eq-QP123456"]

    def test_a_mark_alone_is_not_the_evidence(self):
        # Only a role the brand function bound makes a unit the evidence.
        # A site limit Easee does not bind keeps its own unit, and does not
        # turn the unit beside it into a meter.
        site = [_ent("number.easee_site_max_current", "site", "current",
                     unit="A"),
                _ent("sensor.easee_site_power", "site", "power", unit="kW")]
        units = {}
        for dev, ents in (("site", site), ("eq-QP123456", _equalizer())):
            mapping = _discover_easee(ents)
            apply_charger_discovery_guards(mapping, ents)
            units[dev] = (mapping, ents)
        assert meters_beside_chargers("easee", units) == set()

    def test_a_transport_platform_has_no_neighbours(self):
        plug = _ent("binary_sensor.jb_plug", "jb", key="plug")
        units = {
            "juicebox": ({"ev_connected_sensor": "binary_sensor.jb_plug",
                          "ev_charging_power_sensor": "sensor.jb_power"},
                         [plug]),
            "heat_pump": ({"ev_charging_power_sensor": "sensor.hp_power"},
                          [_ent("sensor.hp_power", "hp", "power",
                                key="power")]),
        }
        assert meters_beside_chargers("mqtt", units) == set()
        # …and the same two units on an integration's own platform:
        assert meters_beside_chargers("easee", units) == {"heat_pump"}


def _meter_for(platform):
    p = f"sensor.{platform}_meter"
    return [
        _ent(f"{p}_{k}", "meter", dc, platform, unit=u, key=k)
        for k, dc, u in (("import_power", "power", "W"),
                         ("export_power", "power", "W"),
                         ("import_energy", "energy", "kWh"),
                         ("export_energy", "energy", "kWh"),
                         ("total_energy", "energy", "kWh"),
                         ("current", "current", "A"))
    ]


def _box_for(platform):
    n = f"{platform}_box"
    return [
        _ent(f"binary_sensor.{n}_plug", "box", "plug", platform, key="plug"),
        _ent(f"binary_sensor.{n}_charging", "box", "battery_charging",
             platform, key="charging"),
        _ent(f"sensor.{n}_status", "box", None, platform, key="status"),
        _ent(f"sensor.{n}_charging_power", "box", "power", platform,
             unit="W", key="charging_power"),
        _ent(f"sensor.{n}_session_energy", "box", "energy", platform,
             unit="kWh", key="session_energy"),
        _ent(f"number.{n}_charging_current", "box", "current", platform,
             unit="A", key="charging_current"),
        _ent(f"switch.{n}_charging", "box", None, platform,
             key="charging_switch"),
        _ent(f"select.{n}_charge_mode", "box", None, platform,
             key="charge_mode"),
    ]


#: The brands where a bare meter was a charger before #1036, and where the
#: test box is a charger: the oracle below must cover every one of them.
_ADMITTED_AND_EXERCISED = (
    "keba", "easee", "goecharger", "goecharger_mqtt", "goecharger_api2",
    "wallbox", "chargepoint", "heidelberg_energy_control", "openwb2mqtt",
    "openwbmqtt", "ocpp", "ohme", "peblar", "v2c", "wattpilot",
)
# (#1032) openevse left the brand list: the roster's roles find it, and a
# meter alone is no role charger (power without a plug or a control) —
# pinned in tests/test_hw_wave_roles.py.


class TestEveryBrandSharesTheRule:
    """The class lives in every brand function that admits a device on a
    power reading. The rule sits in the walk they all share — so one meter
    beside one box, on every platform SEM knows."""

    def _platforms(self):
        return [p for p, _fn in _EV_CHARGER_PLATFORMS
                if p not in hd._TRANSPORT_PLATFORMS]

    def test_no_brand_offers_the_meter_beside_its_box(self):
        exercised, admitted_alone = [], []
        fns = dict(_EV_CHARGER_PLATFORMS)
        for platform in self._platforms():
            meter = _meter_for(platform)
            if fns[platform](list(meter)):
                admitted_alone.append(platform)
            found = _config_walk(meter + _box_for(platform))
            boxes = [c for c in found if c.get("_device_id") == "box"]
            if not any(any(c.get(r) for r in _CHARGER_ONLY_ROLES)
                       for c in boxes):
                continue   # this brand's names do not match the test box
            exercised.append(platform)
            assert not [c for c in found if c.get("_device_id") == "meter"], (
                f"{platform}: the meter beside the box is offered as a "
                "charger")
            rep = build_detection_report(
                registry=_registry(meter + _box_for(platform)))
            assert "meter" not in {c["device_id"] for c in rep["chargers"]}, (
                platform)
        # Not vacuous: the pre-fix walk really did admit the meter on these
        # brands, and the box really was a charger on them. A literal list,
        # so a brand dropping out of the check fails here instead.
        assert set(_ADMITTED_AND_EXERCISED) <= (
            set(admitted_alone) & set(exercised)), (admitted_alone, exercised)

    def test_both_binding_walks_ask_the_question(self):
        # The class recurs by a third walk that maps unit by unit and
        # forgets to look at the units beside it.
        tree = ast.parse(
            (_PKG / "hardware_detection.py").read_text(encoding="utf-8"))
        wanted = {"discover_all_ev_chargers_from_registry",
                  "build_detection_report"}
        seen = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name in wanted:
                calls = {n.func.id for n in ast.walk(node)
                         if isinstance(n, ast.Call)
                         and isinstance(n.func, ast.Name)}
                if "meters_beside_chargers" in calls:
                    seen.add(node.name)
        assert seen == wanted, f"not asking: {sorted(wanted - seen)}"

    def test_a_unit_the_guards_emptied_is_not_a_charger(self):
        # Both walks map every unit before deciding, so the config path now
        # reads the guarded mapping like the report always did. A unit whose
        # only role was an offline register used to come back as a charger
        # with no roles at all — and could be the one setup saves.
        unit = [_ent("number.goe_box_max_current_offline", "goe", "current",
                     "goecharger", unit="A")]
        assert hd._discover_goecharger(list(unit))   # pre-fix: admitted
        assert _config_walk(unit) == []



class TestTheDiagnosticsDownload:
    """The download takes a fixed list of report keys. A meter the report
    names but the download drops is a meter the user never sees."""

    @pytest.mark.asyncio
    async def test_the_download_carries_the_meters(self, tmp_path):
        from custom_components.solar_energy_management.diagnostics import (
            async_get_config_entry_diagnostics,
        )
        rep = build_detection_report(
            registry=_registry(_equalizer() + _charger()))
        assert rep["meters"]   # not vacuous: there is a meter to carry

        hass = MagicMock()
        hass.config.config_dir = str(tmp_path)

        async def _executor(func, *args, **kwargs):
            return func(*args, **kwargs)

        hass.async_add_executor_job = _executor
        coord = MagicMock()
        coord.last_update_success = True
        coord.update_interval = timedelta(seconds=10)
        coord._observer_mode = False
        coord._load_manager = None
        coord._energy_dashboard_config = None
        coord.data = {"detection_report": rep}
        entry = MagicMock()
        entry.entry_id = "entry_1036"
        entry.version = 7
        entry.domain = "solar_energy_management"
        entry.data = {}
        entry.options = {}
        entry.runtime_data = coord
        hass.data = {"solar_energy_management": {entry.entry_id: coord}}

        result = await async_get_config_entry_diagnostics(hass, entry)
        assert result["detection"]["meters"] == rep["meters"]


def _saved_by_setup(units):
    """What zero-config setup saved before #1036: the FIRST unit the walk
    returned, built here from the brand function itself so these pins do
    not lean on the code they test."""
    from custom_components.solar_energy_management import (
        build_discovered_charger_storage,
    )
    ents = [e for e in units if not e.disabled_by]
    found = _discover_easee(ents)
    apply_charger_discovery_guards(found, ents)
    found["_platform"] = "easee"
    found["_device_id"] = ents[0].device_id
    return build_discovered_charger_storage({}, {}, found)[2]


def _heal(chargers, entries):
    from custom_components.solar_energy_management import (
        _drop_meters_saved_as_chargers,
    )
    with patch.object(hd.entity_registry, "async_get",
                      return_value=_registry(entries)):
        return _drop_meters_saved_as_chargers(MagicMock(), chargers)


class TestTheSavedEqualizerIsRemoved:
    """Setup saves the first charger it finds and never looks again while
    one is saved — so an install that saved the Equalizer keeps it until
    something removes it. Only SEM's own save is touched."""

    def test_setup_really_saved_the_equalizer(self):
        saved = _saved_by_setup(_equalizer())
        assert saved[0]["ev_charging_power_sensor"] == (
            "sensor.easee_equalizer_qp123456_import_power")

    def test_the_saved_equalizer_is_removed(self):
        saved = _saved_by_setup(_equalizer())
        assert _heal(saved, _equalizer() + _charger()) == []

    def test_a_charger_the_user_added_beside_it_stays(self):
        mine = {"id": "ev_charger_1", "name": "Garage",
                "ev_connected_sensor": "sensor.easee_home_eh123456_status",
                "ev_charging_power_sensor": "sensor.easee_home_eh123456_power"}
        saved = _saved_by_setup(_equalizer()) + [mine]
        assert _heal(saved, _equalizer() + _charger()) == [mine]

    def test_a_saved_equalizer_the_user_edited_stays(self):
        saved = _saved_by_setup(_equalizer())
        saved[0]["ev_connected_sensor"] = "sensor.easee_home_eh123456_status"
        assert _heal(saved, _equalizer() + _charger()) is None

    def test_a_save_from_before_v1_7_5_is_removed_too(self):
        # Setup then kept ``_platform`` / ``_device_id`` and used one id.
        old = [{"id": "ev_charger", "name": "EV Charger",
                "_platform": "easee", "_device_id": "eq-QP123456",
                "ev_charging_power_sensor":
                    "sensor.easee_equalizer_qp123456_import_power",
                "ev_charger_service": "easee.set_charger_dynamic_limit"}]
        assert _heal(old, _equalizer() + _charger()) == []

    def test_a_saved_equalizer_with_another_power_sensor_stays(self):
        saved = _saved_by_setup(_equalizer())
        saved[0]["ev_charging_power_sensor"] = (
            "sensor.easee_home_eh123456_power")
        assert _heal(saved, _equalizer() + _charger()) is None

    def test_a_saved_real_charger_stays(self):
        saved = _saved_by_setup(_charger(status="disabled",
                                         session="disabled"))
        assert not any(saved[0].get(r) for r in _CHARGER_ONLY_ROLES)
        assert _heal(saved, _equalizer() + _charger()) is None

    def test_with_no_charger_beside_it_nothing_is_removed(self):
        saved = _saved_by_setup(_equalizer())
        assert _heal(saved, _equalizer()) is None

    def test_a_normal_install_never_walks_the_registry(self):
        from custom_components.solar_energy_management import (
            _drop_meters_saved_as_chargers,
        )
        mine = [{"id": "ev_charger_0",
                 "ev_connected_sensor": "binary_sensor.keba_plug"}]
        with patch.object(hd.entity_registry, "async_get",
                          side_effect=AssertionError("walked")):
            assert _drop_meters_saved_as_chargers(MagicMock(), mine) is None

    def test_setup_heals_before_it_reads_the_charger_list(self):
        # An emptied list must send THIS setup back to discovery, so the
        # heal has to run before ``full_config`` is built from the entry.
        import custom_components.solar_energy_management as pkg
        tree = ast.parse(pathlib.Path(pkg.__file__).read_text(
            encoding="utf-8"))
        setup = next(n for n in ast.walk(tree)
                     if isinstance(n, ast.AsyncFunctionDef)
                     and n.name == "async_setup_entry")
        heal = [n.lineno for n in ast.walk(setup)
                if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Name)
                and n.func.id == "_drop_meters_saved_as_chargers"]
        read = [n.lineno for n in ast.walk(setup)
                if isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "full_config"
                        for t in n.targets)]
        assert len(heal) == 2 and read, (heal, read)
        assert max(heal) < min(read), (heal, read)
