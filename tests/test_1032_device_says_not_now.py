"""#1032 — a control the device made ``unavailable`` on purpose is not broken.

@OddmarDam (Zaptec Go, 2.2.0-beta.9) got the ERROR Repair "EV charger control
entity is not working" every time the car was unplugged. The start/stop entity
was ``switch.garage_zaptec_ev_charger_charging``, the Zaptec integration's
"Charging" switch, and its ``available`` is not about the box being reachable::

    @property
    def available(self) -> bool:
        command = "stop_charging_final" if self._attr_is_on else "resume_charging"
        return super().available and self.zaptec_obj.is_command_valid(command)

(custom-components/zaptec ``switch.py``). Resume is valid only while paused,
stop only while charging, so the switch reads ``unavailable`` with no car, a
full car, or a car waiting for current. HA core's Wallbox pause switch does
the same: ``unavailable`` while DISCONNECTED / READY / LOCKED.

#824's pre-flight read every ``unavailable`` as "renamed, removed or never
loaded" and filed the Repair after five minutes. The fix asks the device
beside the entity: a live device that made one control unavailable is saying
"not now". Everything #824 exists for still files:

* ``restored`` — HA restored a name the platform never produced (#824's own
  template number, and every entity during a restart's warm-up);
* missing / wrong domain;
* a device that is offline — no live sibling, or a connectivity sensor off;
* any control that is unavailable while the charger DRAWS — the stop and the
  current must be there while a charge runs, on every brand.
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from custom_components.solar_energy_management.coordinator import (
    repair_issues as ri,
)
from custom_components.solar_energy_management.coordinator.control_entity import (
    NOT_OFFERED_NOW,
    device_says_not_now,
    validate_control_entity,
)
from custom_components.solar_energy_management.coordinator.coordinator import (
    SEMCoordinator,
)
from custom_components.solar_energy_management.coordinator.sensor_reader import (
    EV_ACTIVE_CHARGE_FLOOR_W,
)
from custom_components.solar_energy_management.const import DOMAIN

ROOT = Path(__file__).resolve().parents[1]
HOLD = ri.UNAVAILABLE_REPAIR_THRESHOLD_S
SWITCH = "switch.garage_zaptec_ev_charger_charging"


def _issue_id(cid, eid):
    return f"charger_control_entity_broken_{cid}_{eid}"


# ── the rule, pure ────────────────────────────────────────────────────────


@pytest.mark.unit
class TestTheRule:

    def test_a_live_sibling_speaks_for_the_device(self):
        assert device_says_not_now({}, [("Charge done", {})]) is True

    def test_a_restored_entity_has_no_device_to_speak_for_it(self):
        """#824's case: HA restored a name its platform never produced."""
        assert device_says_not_now({"restored": True},
                                   [("Charge done", {})]) is False

    def test_no_siblings_is_no_answer(self):
        """A YAML template number has no device — judged alone, as before."""
        assert device_says_not_now({}, []) is False

    def test_dead_or_restored_siblings_do_not_speak(self):
        assert device_says_not_now({}, [
            ("unavailable", {}), ("unknown", {}), ("", {}), (None, {}),
            ("12", {"restored": True}),
        ]) is False

    def test_a_connectivity_sensor_off_says_the_device_is_offline(self):
        """A cloud box offline keeps stale readings beside an ``online`` off."""
        assert device_says_not_now({}, [
            ("Charge done", {}),
            ("off", {"device_class": "connectivity"}),
        ]) is False
        assert device_says_not_now({}, [
            ("Charge done", {}),
            ("on", {"device_class": "connectivity"}),
        ]) is True


@pytest.mark.unit
class TestTheVerdict:

    def _yes(self, _eid):
        return True

    def test_unavailable_with_the_device_saying_not_now_is_valid(self):
        v = validate_control_entity(
            SWITCH, lambda e: "unavailable", not_now=self._yes)
        assert v.valid is True and v.reason == NOT_OFFERED_NOW

    def test_without_the_question_unavailable_is_still_broken(self):
        v = validate_control_entity(SWITCH, lambda e: "unavailable")
        assert v.valid is False and v.reason == "unavailable"

    def test_only_unavailable_is_answered(self):
        """``unknown`` is a live entity with no value, and missing / wrong
        domain are config faults: the device's answer covers none of them."""
        assert validate_control_entity(
            SWITCH, lambda e: "unknown", not_now=self._yes).valid is False
        assert validate_control_entity(
            SWITCH, lambda e: None, not_now=self._yes).reason == "missing"
        assert validate_control_entity(
            "sensor.x", lambda e: "1", not_now=self._yes).reason == "wrong_domain"

    def test_the_question_is_not_asked_of_a_live_entity(self):
        asked = []
        v = validate_control_entity(
            SWITCH, lambda e: "on", not_now=lambda e: asked.append(e) or False)
        assert v.valid is True and v.reason is None and not asked


# ── the check, on a real Home Assistant ───────────────────────────────────


def _zaptec_available(mode: str, final_stop: bool, is_on: bool) -> bool:
    """custom-components/zaptec ``ZaptecChargeSwitch.available`` and
    ``Charger.is_command_valid``, transcribed."""
    paused = mode == "Connected_Finished" and final_stop
    if is_on:
        return not (paused or mode == "Disconnected")
    return paused


def _box(hass, *, domain, name, entities):
    """One charger device in the real registries; returns entity ids."""
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(domain=domain)
    entry.add_to_hass(hass)
    dev = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(domain, name)})
    reg = er.async_get(hass)
    for platform, obj in entities:
        reg.async_get_or_create(platform, domain, obj, suggested_object_id=obj,
                                config_entry=entry, device_id=dev.id)
    return dev


def _coord(hass):
    return SimpleNamespace(
        hass=hass,
        _CONTROL_CAPABILITIES=SEMCoordinator._CONTROL_CAPABILITIES,
        _control_broken_since={}, _control_repair_raised=set())


def _run(coord, cid, cfg, *, power_w=0.0, minutes=6):
    """Cycles across more than the hold; returns the last cycle's result."""
    clock = [1000.0]
    result = {}
    with patch("time.monotonic", lambda: clock[0]):
        for _ in range(minutes * 6 + 1):
            result = {f"charger_{cid}_power": power_w}
            SEMCoordinator._check_charger_control_entities(coord, cid, cfg, result)
            clock[0] += 10.0
    assert clock[0] - 1000.0 > HOLD, "the run must outlast the hold"
    return result


def _repair(hass, cid, eid):
    from homeassistant.helpers import issue_registry as ir
    return ir.async_get(hass).async_get_issue(DOMAIN, _issue_id(cid, eid))


ZAPTEC_CFG = {"name": "Zepto Go", "ev_start_stop_entity": SWITCH}


def _zepto(hass, *, mode="Disconnected", power="0", online=None):
    ents = [("switch", "garage_zaptec_ev_charger_charging"),
            ("sensor", "garage_zaptec_ev_charger_charger_mode"),
            ("sensor", "garage_zaptec_ev_charger_charge_power")]
    if online is not None:
        ents.append(("binary_sensor", "garage_zaptec_ev_charger_online"))
    _box(hass, domain="zaptec", name="zepto", entities=ents)
    hass.states.async_set("sensor.garage_zaptec_ev_charger_charger_mode", mode)
    hass.states.async_set("sensor.garage_zaptec_ev_charger_charge_power", power,
                          {"unit_of_measurement": "W"})
    if online is not None:
        hass.states.async_set("binary_sensor.garage_zaptec_ev_charger_online",
                              online, {"device_class": "connectivity"})


@pytest.mark.asyncio
class TestTheReportersCharger:

    async def test_unplugged_files_nothing(self, hass):
        """THE report: the car leaves, the switch goes unavailable, the box
        is fine. Before the fix this filed the ERROR Repair at five minutes."""
        _zepto(hass)
        hass.states.async_set(SWITCH, "unavailable")
        coord = _coord(hass)
        result = _run(coord, "zap", ZAPTEC_CFG)
        assert _repair(hass, "zap", SWITCH) is None
        assert result["charger_zap_ev_start_stop_entity_valid"] is True
        assert result["charger_zap_ev_start_stop_entity_reason"] == NOT_OFFERED_NOW
        assert result["charger_zap_control_valid"] is True
        assert not coord._control_broken_since

    async def test_the_pre_fix_rule_would_have_filed_it(self, hass):
        """Vacuity twin: the same home, the device's answer withheld, files —
        so the pin above is the device's answer, not a quiet clock."""
        _zepto(hass)
        hass.states.async_set(SWITCH, "unavailable")
        coord = _coord(hass)
        with patch(
            "custom_components.solar_energy_management.coordinator."
            "coordinator._control_device_says_not_now", lambda h, e: False):
            _run(coord, "zap", ZAPTEC_CFG)
        assert _repair(hass, "zap", SWITCH) is not None

    async def test_a_repair_left_from_before_is_cleared(self, hass):
        """The reporter has one standing now; the first cycle on the fix
        retires it (#933: first valid verdict of a lifetime)."""
        _zepto(hass)
        hass.states.async_set(SWITCH, "unavailable")
        ri.raise_charger_control_entity_broken(
            hass, "zap", name="Zepto Go", entity_id=SWITCH,
            capability="start and stop charging", reason="unavailable")
        assert _repair(hass, "zap", SWITCH) is not None
        coord = _coord(hass)
        SEMCoordinator._check_charger_control_entities(
            coord, "zap", ZAPTEC_CFG, {"charger_zap_power": 0.0})
        assert _repair(hass, "zap", SWITCH) is None

    @pytest.mark.parametrize("mode, final_stop, is_on, drawing", [
        ("Disconnected", False, False, False),
        ("Connected_Requesting", False, False, False),
        ("Connected_Finished", False, False, False),   # car full
        ("Connected_Finished", True, False, False),    # paused by a stop
        ("Connected_Charging", False, True, True),
    ])
    async def test_every_zaptec_mode_is_quiet(self, hass, mode, final_stop,
                                              is_on, drawing):
        """The integration's own table, mode by mode: no state of a healthy
        Zaptec files the Repair."""
        _zepto(hass, mode=mode)
        avail = _zaptec_available(mode, final_stop, is_on)
        hass.states.async_set(SWITCH, ("on" if is_on else "off")
                              if avail else "unavailable")
        coord = _coord(hass)
        _run(coord, "zap", ZAPTEC_CFG,
             power_w=7000.0 if drawing else 0.0)
        assert _repair(hass, "zap", SWITCH) is None, mode
        # the oracle is not trivially "always available"
        if mode == "Disconnected":
            assert not avail


@pytest.mark.asyncio
class TestWhatStillFiles:

    async def test_unavailable_while_drawing(self, hass):
        """A charge runs and SEM cannot stop it: broken on every brand."""
        _zepto(hass, mode="Connected_Charging", power="7000")
        hass.states.async_set(SWITCH, "unavailable")
        _run(_coord(hass), "zap", ZAPTEC_CFG, power_w=7000.0)
        assert _repair(hass, "zap", SWITCH) is not None

    async def test_just_below_and_at_the_floor(self, hass):
        _zepto(hass)
        hass.states.async_set(SWITCH, "unavailable")
        _run(_coord(hass), "zap", ZAPTEC_CFG,
             power_w=EV_ACTIVE_CHARGE_FLOOR_W - 1)
        assert _repair(hass, "zap", SWITCH) is None
        _run(_coord(hass), "zap", ZAPTEC_CFG, power_w=EV_ACTIVE_CHARGE_FLOOR_W)
        assert _repair(hass, "zap", SWITCH) is not None

    async def test_a_restored_switch(self, hass):
        """#824's own case, and a Zaptec that failed to load: HA restored the
        names, nothing on the device is live."""
        _zepto(hass)
        for eid in ("sensor.garage_zaptec_ev_charger_charger_mode",
                    "sensor.garage_zaptec_ev_charger_charge_power"):
            hass.states.async_set(eid, "unavailable", {"restored": True})
        hass.states.async_set(SWITCH, "unavailable", {"restored": True})
        _run(_coord(hass), "zap", ZAPTEC_CFG)
        assert _repair(hass, "zap", SWITCH) is not None

    async def test_a_restored_switch_beside_live_sensors(self, hass):
        """The switch platform never produced it, the sensor platform did:
        the device cannot speak for an entity that does not exist."""
        _zepto(hass)
        hass.states.async_set(SWITCH, "unavailable", {"restored": True})
        _run(_coord(hass), "zap", ZAPTEC_CFG)
        assert _repair(hass, "zap", SWITCH) is not None

    async def test_a_device_with_nothing_live(self, hass):
        _zepto(hass)
        hass.states.async_set("sensor.garage_zaptec_ev_charger_charger_mode",
                              "unavailable")
        hass.states.async_set("sensor.garage_zaptec_ev_charger_charge_power",
                              "unavailable")
        hass.states.async_set(SWITCH, "unavailable")
        _run(_coord(hass), "zap", ZAPTEC_CFG)
        assert _repair(hass, "zap", SWITCH) is not None

    async def test_a_device_that_says_it_is_offline(self, hass):
        _zepto(hass, online="off")
        hass.states.async_set(SWITCH, "unavailable")
        _run(_coord(hass), "zap", ZAPTEC_CFG)
        assert _repair(hass, "zap", SWITCH) is not None

    async def test_an_entity_with_no_device(self, hass):
        """A YAML template number: judged on its own state, as before."""
        cfg = {"name": "Garage", "ev_current_control_entity": "number.wb_ladestrom"}
        hass.states.async_set("number.wb_ladestrom", "unavailable")
        _run(_coord(hass), "wb", cfg)
        assert _repair(hass, "wb", "number.wb_ladestrom") is not None

    async def test_sem_own_entities_do_not_speak_for_the_box(self, hass):
        """Only the charger's own integration can say "not now"."""
        from homeassistant.helpers import entity_registry as er
        dev = _box(hass, domain="zaptec", name="zepto",
                   entities=[("switch", "garage_zaptec_ev_charger_charging")])
        er.async_get(hass).async_get_or_create(
            "sensor", DOMAIN, "sem_zap_power",
            suggested_object_id="sem_zap_power", device_id=dev.id)
        hass.states.async_set("sensor.sem_zap_power", "0")
        hass.states.async_set(SWITCH, "unavailable")
        _run(_coord(hass), "zap", ZAPTEC_CFG)
        assert _repair(hass, "zap", SWITCH) is not None


@pytest.mark.asyncio
async def test_wallbox_pause_switch_with_no_car(hass):
    """HA core's Wallbox: the pause switch is unavailable while READY (no
    car). Same answer, another brand."""
    eid = "switch.wallbox_pulsar_pause_resume"
    _box(hass, domain="wallbox", name="pulsar",
         entities=[("switch", "wallbox_pulsar_pause_resume"),
                   ("sensor", "wallbox_pulsar_status_description"),
                   ("number", "wallbox_pulsar_maximum_charging_current")])
    hass.states.async_set("sensor.wallbox_pulsar_status_description", "Ready")
    hass.states.async_set("number.wallbox_pulsar_maximum_charging_current", "16")
    hass.states.async_set(eid, "unavailable")
    cfg = {"name": "Pulsar", "ev_start_stop_entity": eid,
           "ev_current_control_entity": "number.wallbox_pulsar_maximum_charging_current"}
    result = _run(_coord(hass), "wb", cfg)
    assert _repair(hass, "wb", eid) is None
    assert result["charger_wb_control_valid"] is True


# ── the whole path: SEM set up, the reporter's switch, real cycles ────────


@pytest.mark.asyncio
async def test_the_reporters_home_through_a_real_setup(
    sem_real_hass, sem_config_entry,
):
    """The check reads ``charger_<id>_power`` from the cycle's own result:
    driven through SEM's real refresh, the key is there and the unplugged
    Zaptec files nothing past the hold."""
    from homeassistant.helpers import issue_registry as ir

    from .test_services_real import _seed_sem_input_sensors, _setup_sem

    h = sem_real_hass
    _seed_sem_input_sensors(h)
    _zepto(h)
    h.states.async_set(SWITCH, "unavailable")
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    base = sem_config_entry
    charger = {**base.data["ev_chargers"][0], "ev_start_stop_entity": SWITCH}
    entry = MockConfigEntry(domain=DOMAIN, version=base.version,
                            minor_version=base.minor_version,
                            data={**base.data, "ev_chargers": [charger]})
    await _setup_sem(h, entry)
    coord = entry.runtime_data
    assert isinstance(coord, SEMCoordinator)
    cid = charger["id"]

    for _ in range(2):
        for key in list(coord._control_broken_since):
            coord._control_broken_since[key] -= HOLD + 1
        await coord.async_refresh()
        await h.async_block_till_done()

    assert f"charger_{cid}_power" in coord.data, (
        "the key the check reads is not in the cycle's result")
    assert coord.data[f"charger_{cid}_ev_start_stop_entity_reason"] == NOT_OFFERED_NOW
    assert ir.async_get(h).async_get_issue(DOMAIN, _issue_id(cid, SWITCH)) is None


# ── structure ─────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_every_control_check_asks_the_device():
    """Every production call of the validator passes ``not_now=`` — a new
    control pre-flight that leaves it out brings #1032 back for its entity."""
    calls = []
    for path in ROOT.rglob("*.py"):
        if "tests" in path.parts or "tools" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and getattr(node.func, "id", getattr(node.func, "attr", None))
                    == "validate_control_entity"):
                calls.append((path.name, node))
    assert calls, "the validator is called nowhere — the guard is vacuous"
    for name, node in calls:
        assert any(k.arg == "not_now" for k in node.keywords), (
            f"{name}:{node.lineno} validates a control entity without "
            "asking its device (#1032)")


@pytest.mark.unit
def test_the_rule_stays_pure():
    from custom_components.solar_energy_management.coordinator import (
        control_entity,
    )
    assert "homeassistant" not in inspect.getsource(control_entity)
