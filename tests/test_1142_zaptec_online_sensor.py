"""#1142 — setup read a Zaptec's "Charger" sensor as "a car is charging".

That sensor is the charger's main binary sensor, ``device_class:
connectivity``: it is on while the box is online, so almost all the time.
The Zaptec rule bound it because "charg" is in its own name, "Charger". The
real state is "Charger mode" (``charger_operation_mode``: disconnected,
connected_requesting, connected_charging, connected_finished), which setup
left empty. Found while fixing #1139.

On the integration's newer layout the main sensor is called "active", so
nothing bound a state at all, the brand rule refused the Go, and setup fell
through to the near-miss offer with the installation's site limit as its
control (#1141). Binding "Charger mode" maps the Go in both layouts.

Bug class 42: HA's device class says what the sensor is; a word in its name
was asked instead. The guard sits at the choke point every registry path
funnels through, so no brand reads a link sensor as the charge.

The pins below:

* the v0.8.7 layout @OddmarDam runs, and the pinned upstream layout;
* the bound sensor read as the coordinator reads it;
* every platform in ``_EV_CHARGER_PLATFORMS`` with a link sensor named
  "Charger", with a twin that shows the rules do bite on the name;
* the guard alone;
* a Zaptec setup saved before the fix gets "Charger mode" at startup.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from custom_components.solar_energy_management import hardware_detection as hd
from custom_components.solar_energy_management.coordinator.sensor_reader import (
    SensorReader,
)
from custom_components.solar_energy_management.hardware_detection import (
    _EV_CHARGER_PLATFORMS,
    _own_names,
    _reject_link_as_charging,
    discover_all_ev_chargers_from_registry,
)

from .integrations_rig.rig import load_capture, replay
from .test_1139_orphan_charger_repairs import GO, _zaptec

MODE = f"sensor.{GO}_charger_mode"
ONLINE_CHARGER = f"binary_sensor.{GO}_charger"


def _go(found):
    (charger,) = [c for c in found if c.get("_platform") == "zaptec"]
    return charger


# ── his layout (custom-components/zaptec v0.8.7) ────────────────────────

async def test_the_layout_really_holds_the_trap(hass):
    """Non-vacuous: the main sensor is a link sensor, and its own name
    holds the word the old rule asked for."""
    from homeassistant.helpers import entity_registry as er
    await _zaptec(hass, with_limit=True)
    entry = er.async_get(hass).async_get(ONLINE_CHARGER)
    assert entry.original_device_class == "connectivity"
    siblings = [e for e in er.async_get(hass).entities.values()
                if e.device_id == entry.device_id]
    assert "charg" in _own_names(siblings)[ONLINE_CHARGER]


@pytest.mark.parametrize("with_limit", [False, True])
async def test_charger_mode_is_the_state_never_the_online_sensor(
        hass, with_limit):
    await _zaptec(hass, with_limit=with_limit)
    charger = _go(discover_all_ev_chargers_from_registry(hass))
    assert charger["ev_connected_sensor"] == MODE
    assert charger["ev_charging_sensor"] == MODE
    assert ONLINE_CHARGER not in charger.values()
    assert f"binary_sensor.{GO}_online" not in charger.values()
    # nothing else moved
    assert charger["ev_current_control_entity"] == (
        f"number.{GO}_charger_max_current")
    assert charger["ev_charging_power_sensor"] == f"sensor.{GO}_charge_power"


# ── the pinned upstream layout ──────────────────────────────────────────

@pytest.mark.parametrize("name", ["zaptec", "zaptec_no_limit"])
async def test_the_upstream_layout_is_mapped_by_its_brand_rule(hass, name):
    """Its main sensor is "active", so no state was bound and the Go went
    to the near-miss offer. Now the brand rule maps it."""
    await replay(hass, load_capture(name))
    charger = _go(discover_all_ev_chargers_from_registry(hass))
    assert charger.get("_found_by") != "roles"
    mode = "sensor.zaptec_go2_zap012345_charger_operation_mode"
    assert charger["ev_connected_sensor"] == mode
    assert charger["ev_charging_sensor"] == mode
    assert charger["ev_current_control_entity"] == (
        "number.zaptec_go2_zap012345_charger_max_current")
    assert "abbastova" not in str(
        {k: v for k, v in charger.items() if not k.startswith("_")})


async def test_the_session_energy_is_not_the_power(hass):
    """The rule's name fallback took ``total_charge_power_session`` — kWh,
    keyed with "power" — as the power, once the brand rule ran on this
    layout."""
    await replay(hass, load_capture("zaptec"))
    charger = _go(discover_all_ev_chargers_from_registry(hass))
    assert charger["ev_charging_power_sensor"] == (
        "sensor.zaptec_go2_zap012345_total_charge_power")
    assert charger["ev_session_energy_sensor"] == (
        "sensor.zaptec_go2_zap012345_total_charge_power_session")


def test_a_real_charging_binary_wins_over_charger_mode():
    """Builds with their own plug and charging binaries (the simulator's
    layout) keep them; "Charger mode" fills only what is empty."""
    dev = "zaptec-go"
    def e(eid, dc=None, key=""):
        return SimpleNamespace(
            entity_id=eid, platform="zaptec", device_id=dev,
            original_device_class=dc, disabled_by=None,
            original_unit_of_measurement=None, unit_of_measurement=None,
            translation_key=None, unique_id=f"chg-1_{key}",
            config_entry_id="entry-1")
    entities = [
        e("sensor.go_charger_mode", "enum", "charger_operation_mode"),
        e("binary_sensor.go_cable_connected", "plug", "cable_connected"),
        e("binary_sensor.go_charging", "battery_charging", "charging"),
        e("binary_sensor.go_charger", "connectivity", ""),
        e("sensor.go_charge_power", "power", "charge_power"),
        e("number.go_charger_max_current", "current", "charger_max_current"),
    ]
    result = hd._discover_zaptec(entities)
    assert result["ev_connected_sensor"] == "binary_sensor.go_cable_connected"
    assert result["ev_charging_sensor"] == "binary_sensor.go_charging"
    # and with the binaries gone, the mode fills both
    alone = hd._discover_zaptec([x for x in entities if x.entity_id not in (
        "binary_sensor.go_cable_connected", "binary_sensor.go_charging")])
    assert alone["ev_connected_sensor"] == "sensor.go_charger_mode"
    assert alone["ev_charging_sensor"] == "sensor.go_charger_mode"


def test_a_session_counter_without_a_class_is_not_the_power():
    """Builds that set no device class: the unit says kWh is energy."""
    def e(eid, unit, key):
        return SimpleNamespace(
            entity_id=eid, platform="zaptec", device_id="d",
            original_device_class=None, disabled_by=None,
            original_unit_of_measurement=unit, unit_of_measurement=unit,
            translation_key=None, unique_id=f"chg-1_{key}",
            config_entry_id="entry-1")
    entities = [
        e("sensor.go_total_charge_power", "W", "total_charge_power"),
        e("sensor.go_total_charge_power_session", "kWh",
          "total_charge_power_session"),
        e("sensor.go_charger_operation_mode", None, "charger_operation_mode"),
    ]
    result = hd._discover_zaptec(entities)
    assert result["ev_charging_power_sensor"] == "sensor.go_total_charge_power"


# ── what the binding is for ──────────────────────────────────────────────

@pytest.mark.parametrize("mode, plugged, charging", [
    ("disconnected", False, False),
    ("connected_requesting", True, False),
    ("connected_charging", True, True),
    ("connected_finished", True, False),
])
async def test_the_bound_sensor_reads_the_car(hass, mode, plugged, charging):
    reader = SimpleNamespace(hass=hass)
    hass.states.async_set(MODE, mode)
    hass.states.async_set(ONLINE_CHARGER, "on")
    assert SensorReader._read_binary_sensor(reader, MODE, "ev_plug") is plugged
    assert SensorReader._read_binary_sensor(
        reader, MODE, "ev_charging") is charging
    # the old binding: a charge whenever the box is online
    assert SensorReader._read_binary_sensor(
        reader, ONLINE_CHARGER, "ev_charging") is True


# ── the class, across every brand SEM discovers ─────────────────────────

def _entry(entity_id, platform, device_id, device_class=None, unit=None):
    return SimpleNamespace(
        entity_id=entity_id, platform=platform, device_id=device_id,
        original_device_class=device_class, disabled_by=None,
        original_unit_of_measurement=unit, unit_of_measurement=unit,
        translation_key=None, unique_id=entity_id.split(".", 1)[1],
        config_entry_id="entry-1",
    )


#: A charger with no charging sensor of its own, beside a sensor whose own
#: name says "charger" — the box's main entity, as Zaptec builds it.
_ROWS = [
    ("binary_sensor", "plug_connected", "plug", None),
    ("sensor", "charging_power", "power", "W"),
    ("sensor", "total_energy", "energy", "kWh"),
    ("number", "charging_current", "current", "A"),
    ("switch", "charge", None, None),
]


def _every_platform(link_class, link_first):
    entries = []
    for platform, _fn in _EV_CHARGER_PLATFORMS:
        dev = f"{platform}-box"
        rows = [_entry(f"{dom}.garage_box_{own}", platform, dev, dc, unit)
                for dom, own, dc, unit in _ROWS]
        link = _entry("binary_sensor.garage_box_charger", platform, dev,
                      link_class)
        entries += [link, *rows] if link_first else [*rows, link]
    return entries


def _discover(entries):
    registry = MagicMock()
    registry.entities.values.return_value = entries
    with patch.object(hd.entity_registry, "async_get", return_value=registry):
        return discover_all_ev_chargers_from_registry(MagicMock())


def _bound_as_charging(found):
    return {c.get("_platform") for c in found
            if c.get("ev_charging_sensor") == "binary_sensor.garage_box_charger"}


@pytest.mark.parametrize("link_first", [True, False])
def test_the_rules_do_bite_on_the_name(link_first):
    """Non-vacuous: with no device class the same sensor IS bound as the
    charge by the name rules of several brands."""
    bitten = _bound_as_charging(_discover(_every_platform(None, link_first)))
    assert {"zaptec", "goecharger", "wallbox"} <= bitten
    assert len(bitten) >= 5


@pytest.mark.parametrize("link_first", [True, False])
def test_no_brand_reads_a_link_sensor_as_the_charge(link_first):
    found = _discover(_every_platform("connectivity", link_first))
    assert len(found) >= 10       # the chargers are still found
    assert _bound_as_charging(found) == set()


@pytest.mark.parametrize("link_first", [True, False])
def test_a_real_charging_sensor_beside_it_is_kept(link_first):
    """The name rules keep the last match. Skipping the link sensor in the
    rule, not only dropping it after, keeps the real one in either order."""
    entries = _every_platform("connectivity", link_first)
    for platform, _fn in _EV_CHARGER_PLATFORMS:
        entries.insert(0, _entry("binary_sensor.garage_box_charging", platform,
                                 f"{platform}-box", "battery_charging"))
    by_platform = {c.get("_platform"): c.get("ev_charging_sensor")
                   for c in _discover(entries)}
    bitten = _bound_as_charging(_discover(_every_platform(None, link_first)))
    for platform in bitten:
        assert by_platform.get(platform) == "binary_sensor.garage_box_charging", (
            platform)


class TestTheGuard:
    def _run(self, device_class, eid="binary_sensor.box_charger"):
        result = {"ev_charging_sensor": eid, "ev_connected_sensor": eid}
        _reject_link_as_charging(result, [_entry(eid, "x", "d", device_class)])
        return result

    def test_a_link_sensor_is_dropped(self):
        result = self._run("connectivity")
        assert "ev_charging_sensor" not in result
        # a plug is another question: a car's charge cable uses this class
        assert result["ev_connected_sensor"] == "binary_sensor.box_charger"

    @pytest.mark.parametrize("dc", [None, "power", "battery_charging",
                                    "running", "plug"])
    def test_any_other_binary_is_kept(self, dc):
        assert self._run(dc)["ev_charging_sensor"] == "binary_sensor.box_charger"

    def test_a_status_sensor_is_kept(self):
        eid = "sensor.box_status"
        assert self._run("enum", eid)["ev_charging_sensor"] == eid

    def test_an_entity_outside_the_unit_is_kept(self):
        result = {"ev_charging_sensor": "binary_sensor.elsewhere"}
        _reject_link_as_charging(result, [])
        assert result == {"ev_charging_sensor": "binary_sensor.elsewhere"}


# ── a Zaptec setup saved before the fix ──────────────────────────────────

def _old_save(found):
    """What zero-config setup saved before #1142, through setup's own
    builder: no plug sensor, the online sensor as the charge."""
    from custom_components.solar_energy_management import (
        build_discovered_charger_storage,
    )
    old = dict(found)
    old.pop("ev_connected_sensor")
    old["ev_charging_sensor"] = ONLINE_CHARGER
    return build_discovered_charger_storage({}, {}, old)[2]


def _heal(hass, chargers):
    from custom_components.solar_energy_management import (
        _fill_state_sensors_of_own_save,
    )
    return _fill_state_sensors_of_own_save(hass, chargers)


async def _saved(hass):
    await _zaptec(hass, with_limit=True)
    return _old_save(_go(discover_all_ev_chargers_from_registry(hass)))


async def test_the_old_save_gets_charger_mode(hass):
    saved = await _saved(hass)
    assert saved[0]["ev_charging_sensor"] == ONLINE_CHARGER
    assert not saved[0].get("ev_connected_sensor")
    healed = _heal(hass, saved)
    assert healed[0]["ev_connected_sensor"] == MODE
    assert healed[0]["ev_charging_sensor"] == MODE
    rest = {k: v for k, v in healed[0].items()
            if k not in ("ev_connected_sensor", "ev_charging_sensor")}
    assert rest == {k: v for k, v in saved[0].items()
                    if k != "ev_charging_sensor"}


async def test_it_runs_once(hass):
    healed = _heal(hass, await _saved(hass))
    assert _heal(hass, healed) is None


async def test_a_save_through_the_form_is_left_alone(hass):
    """The forms ask for both sensors: a save with a plug sensor is the
    user's, even with the online sensor as the charge."""
    saved = await _saved(hass)
    saved[0]["ev_connected_sensor"] = f"sensor.{GO}_charger_mode"
    assert _heal(hass, saved) is None


async def test_another_charging_sensor_is_left_alone(hass):
    saved = await _saved(hass)
    saved[0]["ev_charging_sensor"] = f"binary_sensor.{GO}_permanent_cable_lock"
    assert _heal(hass, saved) is None


async def test_a_charger_the_user_set_up_is_left_alone(hass):
    saved = await _saved(hass)
    mine = dict(saved[0], id="ev_charger_1")
    assert _heal(hass, [mine]) is None


async def test_the_link_goes_when_detection_finds_no_state(hass):
    """A brand that maps a charger with no state sensor: the online sensor
    is still not the charge; SEM reads the charge from the power."""
    saved = await _saved(hass)
    found = _go(discover_all_ev_chargers_from_registry(hass))
    bare = {k: v for k, v in found.items()
            if k not in ("ev_connected_sensor", "ev_charging_sensor")}
    with patch.object(hd, "discover_all_ev_chargers_from_registry",
                      return_value=[bare]):
        healed = _heal(hass, saved)
    assert "ev_charging_sensor" not in healed[0]
    assert "ev_connected_sensor" not in healed[0]
    assert healed[0]["ev_current_control_entity"] == (
        saved[0]["ev_current_control_entity"])


async def test_the_link_goes_even_when_a_control_does_not_match(hass):
    """A control that differs from detection keeps the rest of the heal
    away; the link sensor still goes."""
    saved = await _saved(hass)
    saved[0]["ev_start_stop_entity"] = ""
    healed = _heal(hass, saved)
    assert "ev_charging_sensor" not in healed[0]
    assert not healed[0].get("ev_connected_sensor")
    assert healed[0]["ev_current_control_entity"] == (
        saved[0]["ev_current_control_entity"])


async def test_a_link_save_gets_the_control_detection_finds(hass):
    """(#1134) An own save's control that differs from detection is
    replaced in the same step."""
    saved = await _saved(hass)
    right = saved[0]["ev_start_stop_entity"]
    saved[0]["ev_start_stop_entity"] = f"switch.{GO}_permanent_cable_lock"
    healed = _heal(hass, saved)
    assert healed[0]["ev_start_stop_entity"] == right
    assert healed[0]["ev_charging_sensor"] == MODE
