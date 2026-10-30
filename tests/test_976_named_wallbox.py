"""#976 (02.10) — an OCPP charge point named "wallbox" is not a Wallbox.

@bgthb's Huawei SCharger runs on the OCPP integration as a charge point
named "wallbox" — the word German owners use for any wall charger. Its
start/stop is ``switch.wallbox_charge_control``. SEM picked the charger's
adapter by testing ``"wallbox" in entity_id``, so it chose the Wallbox
adapter, which takes the start/stop switch as its pause/resume switch and
turns it ON before every current write. On OCPP that turn_on is a
RemoteStartTransaction. The reporter's log, 02.10 19:23:18:

    EV-OFFER-PROBE(...): desired=16A believed=16A ... charging=True
        actions=['WRITE_CURRENT@16']
    [ocpp] send RemoteStartTransaction ...
    [ocpp] receive {"status":"Rejected"}

A plain current write sent a start to a charger that was already charging,
and the OCPP integration posted "Start transaction failed with response
Rejected" for each one.

Bug class 115: a word in the device's name read as a word about the entity.
The brand is asked of the integration (the registry platform), never of the
id. Pinned here: the reporter's charger end to end through the reconciler,
every other charger platform under a device named "wallbox", a real Wallbox
its owner renamed, and a code check over the adapter package.
"""
from __future__ import annotations

import ast
import inspect
import pathlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.solar_energy_management.coordinator import (
    charger_adapters,
)
from custom_components.solar_energy_management.coordinator.charger_adapters import (
    GenericAdapter,
    WallboxAdapter,
    adapter_for,
)
from custom_components.solar_energy_management.coordinator.charger_adapters import (
    wallbox as wallbox_mod,
)
from custom_components.solar_energy_management.coordinator.charger_reconciler import (
    ChargerReconciler,
)
from custom_components.solar_energy_management.coordinator.charger_types import (
    ChargerDecision,
    ChargerIntent,
    ChargerPower,
)
from custom_components.solar_energy_management.devices.base import (
    CurrentControlDevice,
)
from custom_components.solar_energy_management.hardware_detection import (
    _EV_CHARGER_PLATFORMS,
)

# The reporter's entities (#962 diagnostics), all on the OCPP integration.
NUM = "number.wallbox_maximum_current"
SW = "switch.wallbox_charge_control"
AVAIL = "switch.wallbox_availability"
STATUS = "sensor.wallbox_status_connector"
POWER = "sensor.wallbox_power_active_import"


class _Registry:
    def __init__(self, entries):
        self.entities = {e.entity_id: e for e in entries}

    def async_get(self, eid):
        return self.entities.get(eid)


def _entry(entity_id, platform, device_id="cp-1"):
    return SimpleNamespace(entity_id=entity_id, platform=platform,
                           device_id=device_id)


def _registry_patch(entries):
    return patch("homeassistant.helpers.entity_registry.async_get",
                 return_value=_Registry(entries))


def _ocpp_entries():
    return [_entry(e, "ocpp") for e in (NUM, SW, AVAIL, STATUS, POWER)]


class _States:
    def __init__(self, mapping):
        self._m = dict(mapping)

    def set(self, eid, state, **attrs):
        self._m[eid] = SimpleNamespace(state=state, attributes=attrs)

    def get(self, eid):
        return self._m.get(eid)


def _hass(switch="on", status="Charging"):
    hass = MagicMock()
    hass.states = _States({})
    hass.states.set(NUM, "16", min=0, max=32, step=1)
    hass.states.set(SW, switch)
    hass.states.set(STATUS, status)
    hass.services.async_call = AsyncMock(return_value=None)
    hass.services.has_service = MagicMock(return_value=False)
    return hass


def _reporters_charger(hass):
    """Built as the setup builder builds it: the current number, the
    auto-detected charge-control switch and the status sensor, plus the
    flag ``wire_current_entity`` sets for an OCPP number."""
    d = CurrentControlDevice(
        hass=hass, device_id="ocpp_7a5f6c9f24", name="Ocpp Charger",
        priority=3, min_current=6.0, max_current=32.0, phases=3,
        voltage=230.0, power_entity_id=POWER, charger_service=None,
        charger_service_entity_id=None, current_entity_id=NUM,
    )
    d.start_stop_entity = SW
    d.charging_status_entity = STATUS
    d.zero_amps_parks_a_limit = True
    return d


def _charge(amps):
    return ChargerDecision(
        charger_id="ocpp_7a5f6c9f24", mode="always_max",
        intent=ChargerIntent.CHARGE_AT_AMPS, commanded_amps=amps,
        reason="test", budget_w=0.0,
    )


def _power(watts):
    return ChargerPower(charger_id="ocpp_7a5f6c9f24", power_w=watts,
                        connected=True, charging=watts > 0)


def _starts(hass):
    """Every RemoteStartTransaction SEM sent: a turn_on of the switch."""
    return [c for c in hass.services.async_call.await_args_list
            if c.args[:2] == ("switch", "turn_on")
            and c.args[2].get("entity_id") == SW]


def _writes(hass):
    return [c.args[2]["value"] for c in hass.services.async_call.await_args_list
            if c.args[:2] == ("number", "set_value")]


# ═══════════════════════════════════════════════════════════════════════
# The reporter's charger, end to end
# ═══════════════════════════════════════════════════════════════════════

class TestTheReportersCharger:
    def test_it_gets_the_generic_adapter(self):
        hass = _hass()
        with _registry_patch(_ocpp_entries()):
            adapter = adapter_for(_reporters_charger(hass))
        assert type(adapter) is GenericAdapter

    def test_the_trap_is_real(self):
        """Non-vacuous: every one of its ids carries the word the old rule
        asked for."""
        for eid in (NUM, SW, AVAIL, STATUS, POWER):
            assert "wallbox" in eid

    @pytest.mark.asyncio
    async def test_a_current_write_sends_no_start_while_it_charges(self):
        """The log line itself: a running session, a current write, and no
        RemoteStart with it."""
        hass = _hass(switch="on", status="Charging")
        with _registry_patch(_ocpp_entries()):
            dev = _reporters_charger(hass)
            adapter = adapter_for(dev)
            rec = ChargerReconciler("ocpp_7a5f6c9f24", heartbeat_s=60.0)
            now = 1000.0
            for amps in (16, 16, 12, 14, 16):
                await rec.reconcile_and_apply(_charge(amps), adapter,
                                              _power(11000.0), now)
                now += 70.0     # past the heartbeat: every cycle writes
        assert _writes(hass), "non-vacuous: the current writes must land"
        assert 12 in _writes(hass) and 14 in _writes(hass)
        assert _starts(hass) == [], (
            f"a current write sent RemoteStart: {_starts(hass)}")

    @pytest.mark.asyncio
    async def test_one_start_per_start(self):
        """From a plugged car with no session: exactly one RemoteStart —
        the enable — and none from the current write after it."""
        hass = _hass(switch="off", status="Preparing")
        with _registry_patch(_ocpp_entries()):
            dev = _reporters_charger(hass)
            adapter = adapter_for(dev)
            rec = ChargerReconciler("ocpp_7a5f6c9f24", heartbeat_s=60.0)
            await rec.reconcile_and_apply(_charge(16), adapter, _power(0.0), 1000.0)
            assert len(_starts(hass)) == 1
            # The charger took it: a transaction is running now.
            hass.states.set(SW, "on")
            hass.states.set(STATUS, "Charging")
            await rec.reconcile_and_apply(_charge(10), adapter,
                                          _power(6900.0), 1070.0)
        assert len(_starts(hass)) == 1, _starts(hass)
        assert _writes(hass)[-1] == 10


# ═══════════════════════════════════════════════════════════════════════
# The start itself: a session the switch already shows is not started again
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
class TestNoStartIntoARunningSession:
    """The second way the same refused start went out. The charger (or the
    Huawei app) started the transaction; SEM then starts its own session,
    and ``start_session`` turned the switch on regardless — a RemoteStart
    into a running transaction."""

    async def test_a_switch_that_reads_on_gets_no_turn_on(self):
        hass = _hass(switch="on")
        dev = _reporters_charger(hass)
        await dev.start_session()
        assert _starts(hass) == []
        assert dev._session_active is True     # the session is SEM's now

    async def test_a_switch_that_reads_off_is_turned_on(self):
        hass = _hass(switch="off", status="Preparing")
        dev = _reporters_charger(hass)
        await dev.start_session()
        assert len(_starts(hass)) == 1

    @pytest.mark.parametrize("state", ["unavailable", "unknown"])
    async def test_an_unreadable_switch_still_gets_its_start(self, state):
        hass = _hass(switch=state)
        dev = _reporters_charger(hass)
        await dev.start_session()
        assert len(_starts(hass)) == 1

    async def test_an_optimistic_switch_still_gets_its_start(self):
        """A switch with ``assumed_state`` (template, REST) may read on only
        because SEM said so last; its on is no proof the box runs."""
        hass = _hass()
        hass.states.set(SW, "on", assumed_state=True)
        dev = _reporters_charger(hass)
        await dev.start_session()
        assert len(_starts(hass)) == 1

    async def test_a_button_is_still_pressed(self):
        """A button has no state to read; its press stays as it was."""
        hass = _hass()
        hass.states.set("button.cp_start", "2026-10-02T19:00:00")
        dev = _reporters_charger(hass)
        dev.start_stop_entity = "button.cp_start"
        await dev.start_session()
        presses = [c for c in hass.services.async_call.await_args_list
                   if c.args[:2] == ("button", "press")]
        assert len(presses) == 1

    async def test_the_hand_back_sends_no_start_to_a_running_session(self):
        hass = _hass(switch="on")
        dev = _reporters_charger(hass)
        dev._sem_parked = True
        await dev.release_to_user(reason="removal")
        assert _starts(hass) == []

    async def test_the_hand_back_still_turns_on_a_switch_sem_left_off(self):
        hass = _hass(switch="off", status="Preparing")
        dev = _reporters_charger(hass)
        dev._sem_parked = True
        await dev.release_to_user(reason="removal")
        assert len(_starts(hass)) == 1


# ═══════════════════════════════════════════════════════════════════════
# The brand is the integration
# ═══════════════════════════════════════════════════════════════════════

def _device(**attrs):
    hass = MagicMock()
    base = dict(charger_service="", charger_service_entity_id="",
                charger_current_entity="", start_stop_entity="", hass=hass)
    base.update(attrs)
    return SimpleNamespace(**base)


class TestTheBrandIsTheIntegration:
    @pytest.mark.parametrize("platform", sorted(
        {p for p, _fn in _EV_CHARGER_PLATFORMS} - {"wallbox"}))
    def test_no_other_charger_named_wallbox_is_a_wallbox(self, platform):
        entries = [_entry(SW, platform), _entry(NUM, platform)]
        with _registry_patch(entries):
            assert not wallbox_mod._looks_like_wallbox(
                _device(start_stop_entity=SW, charger_service_entity_id=NUM))

    def test_two_platforms_with_the_word_in_their_own_name(self):
        """Non-vacuous for the list above: Alfen and GARO both carry
        "wallbox" in their DOMAIN, and neither is the Wallbox integration."""
        platforms = {p for p, _fn in _EV_CHARGER_PLATFORMS}
        assert {"alfen_wallbox", "garo_wallbox", "ocpp"} <= platforms

    def test_a_wallbox_its_owner_renamed_is_a_wallbox(self):
        """The other half of the old rule's mistake: a Wallbox named
        "Garage" carries no "wallbox" in any id, and is one."""
        sw = "switch.garage_pause_resume"
        with _registry_patch([_entry(sw, "wallbox")]):
            assert wallbox_mod._looks_like_wallbox(_device(start_stop_entity=sw))
            assert isinstance(adapter_for(_device(start_stop_entity=sw)),
                              WallboxAdapter)

    def test_a_wallbox_found_the_usual_way_still_is_one(self):
        sw = "switch.wallbox_pulsar_plus_pause_resume"
        with _registry_patch([_entry(sw, "wallbox")]):
            assert wallbox_mod._looks_like_wallbox(_device(start_stop_entity=sw))

    def test_the_wallbox_service_still_says_wallbox(self):
        assert wallbox_mod._looks_like_wallbox(
            _device(charger_service="wallbox.set_charging_current"))

    def test_an_entity_the_registry_does_not_know_says_nothing(self):
        """A YAML template named after the box is no evidence of a brand."""
        with _registry_patch([]):
            assert not wallbox_mod._looks_like_wallbox(
                _device(start_stop_entity="switch.wallbox_template"))

    def test_no_registry_at_all_says_nothing(self):
        def _boom(_hass):
            raise RuntimeError("no registry yet")
        with patch("homeassistant.helpers.entity_registry.async_get",
                   side_effect=_boom):
            assert not wallbox_mod._looks_like_wallbox(
                _device(start_stop_entity="switch.wallbox_pause_resume"))


# ═══════════════════════════════════════════════════════════════════════
# The code check
# ═══════════════════════════════════════════════════════════════════════

_BRAND_WORDS = (
    {p for p, _fn in _EV_CHARGER_PLATFORMS} - {"mqtt"}
) | {"wallbox", "keba", "easee", "zaptec", "ocpp", "goe", "go_e", "go-e"}


def _brand_word_tests(source: str) -> list:
    """Every ``"<brand>" in <x>`` in ``source``: a brand read off a string
    the owner may have written. ``x.startswith("keba.")`` on a SERVICE is
    the integration's own domain and is not flagged."""
    hits = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Compare):
            continue
        if not (isinstance(node.left, ast.Constant)
                and isinstance(node.left.value, str)):
            continue
        word = node.left.value.lower()
        if word in _BRAND_WORDS and any(
                isinstance(op, (ast.In, ast.NotIn)) for op in node.ops):
            hits.append(ast.unparse(node))
    return hits


def _adapter_sources():
    pkg = pathlib.Path(inspect.getfile(charger_adapters)).parent
    return {p.name: p.read_text() for p in sorted(pkg.glob("*.py"))}


class TestNoAdapterReadsABrandOffAnId:
    def test_the_check_reads_the_package(self):
        names = set(_adapter_sources())
        assert {"__init__.py", "wallbox.py", "generic.py", "keba.py"} <= names

    def test_the_check_catches_the_old_rule(self):
        old = (
            "def _looks_like_wallbox(device):\n"
            "    for attr in ('start_stop_entity',):\n"
            "        value = (getattr(device, attr, '') or '').lower()\n"
            "        if \"wallbox\" in value:\n"
            "            return True\n"
            "    return False\n"
        )
        assert _brand_word_tests(old) == ["'wallbox' in value"]

    def test_no_adapter_tests_a_brand_word_against_a_string(self):
        found = {name: _brand_word_tests(src)
                 for name, src in _adapter_sources().items()}
        assert not any(found.values()), found
