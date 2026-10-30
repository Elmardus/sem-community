"""#1042 (02.10) — the V2C Trydan's pause switch, read backwards.

Home Assistant's V2C integration (core, ``v2c/switch.py``) publishes the
session pause as a switch: key ``paused``, name "Pause session", and it is
ON while the box is paused — ``turn_on`` calls ``evse.pause()``. SEM adopted
it as the charger's start/stop switch and drove it like every other one:
``turn_on`` to start, ``turn_off`` to stop, "on" read as charging. So every
start paused the box and every stop resumed it.

The same V2C also publishes "Pause dynamic control modulation" (key
``pause_dynamic``), which pauses the box's own solar modulation, not the
charge. HA registers it after the session pause, and the V2C rule took the
LAST switch with "pause" in its name.

Bug class 118: a switch read by its state, not by what its name says "on"
means. The integration says that in the name it gives the switch — HA's
translation key first, the entity's own name when it has none — and every
write to and read of a start/stop switch now goes through
``utils/switch_sense``. Only a pause of the charge and nothing else is on
while stopped: Wallbox's "Pause/resume" is on while it charges, and a switch
the owner made keeps "on = start".

A V2C set up before the fix most likely saved the modulation pause; the
builder swaps it for the device's session pause (``charge_pause_twin``).
"""
from __future__ import annotations

import ast
import itertools
import pathlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.solar_energy_management.consts.devices import (
    names_a_charge_pause,
    names_a_pause,
    names_another_pause,
)
from custom_components.solar_energy_management.coordinator.charger_adapters import (
    GenericAdapter,
    adapter_for,
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
    _discover_v2c,
    _own_names,
    discover_all_ev_chargers_from_registry,
    wire_current_entity,
)
from custom_components.solar_energy_management.utils.switch_sense import (
    charge_pause_twin,
    on_means_paused,
    reads_running,
    switch_service,
)

PKG = pathlib.Path(__file__).resolve().parents[1]

# ── HA core's V2C (2026.8): ids as an English install builds them ──────
PAUSE = "switch.evse_1_1_1_1_pause_session"
PAUSE_DYNAMIC = "switch.evse_1_1_1_1_pause_dynamic_control_modulation"
NUM = "number.evse_1_1_1_1_intensity"
POWER = "sensor.evse_1_1_1_1_charge_power"

# The switch platform, in the order ``TRYDAN_SWITCHES`` registers it:
# entity id, translation key.
_SWITCHES = [
    (PAUSE, "paused"),
    ("switch.evse_1_1_1_1_lock_evse", "locked"),
    ("switch.evse_1_1_1_1_charge_point_timer", "timer"),
    ("switch.evse_1_1_1_1_dynamic_intensity_modulation", "dynamic"),
    (PAUSE_DYNAMIC, "pause_dynamic"),
]
_REST = [
    ("binary_sensor.evse_1_1_1_1_connected", None, None, "connected"),
    ("binary_sensor.evse_1_1_1_1_charging", None, None, "charging"),
    (NUM, "current", "A", "intensity"),
    ("number.evse_1_1_1_1_max_intensity", "current", "A", "max_intensity"),
    ("number.evse_1_1_1_1_min_intensity", "current", "A", "min_intensity"),
    (POWER, "power", "W", "charge_power"),
    ("sensor.evse_1_1_1_1_charge_energy", "energy", "kWh", "charge_energy"),
]


def _row(eid, dc=None, unit=None, key=None, platform="v2c", device="v2c-1",
         original_name=None, has_entity_name=True):
    return SimpleNamespace(
        entity_id=eid, platform=platform, device_id=device,
        original_device_class=dc, device_class=None,
        unit_of_measurement=unit, translation_key=key,
        disabled_by=None, config_entry_id="e-v2c",
        original_name=original_name, has_entity_name=has_entity_name,
        name=None)


def _v2c(switches=None, rest=None):
    switches = _SWITCHES if switches is None else switches
    rest = _REST if rest is None else rest
    return ([_row(e, key=k) for e, k in switches]
            + [_row(e, dc, u, k) for e, dc, u, k in rest])


class _Registry:
    def __init__(self, entries):
        self.entities = {e.entity_id: e for e in entries}

    def async_get(self, eid):
        return self.entities.get(eid)


def _registry_patch(entries):
    return patch("homeassistant.helpers.entity_registry.async_get",
                 return_value=_Registry(entries))


def _discover(entries):
    with patch(
        "custom_components.solar_energy_management.hardware_detection."
        "entity_registry.async_get",
        return_value=_Registry(entries),
    ):
        found = discover_all_ev_chargers_from_registry(MagicMock())
    assert len(found) == 1, found
    return found[0]


# ═══════════════════════════════════════════════════════════════════════
# The words
# ═══════════════════════════════════════════════════════════════════════

class TestTheWords:
    @pytest.mark.parametrize("name", [
        "paused", "pause_session", "Pause session", "pause_charge",
        "pause_charging", "Pause EV charging"])
    def test_a_pause_of_the_charge(self, name):
        assert names_a_charge_pause(name)

    @pytest.mark.parametrize("name", [
        "pause_resume",                       # Wallbox: on while it charges
        "pause_dynamic",                      # V2C: the solar modulation
        "not_paused", "no_pause", "pause_inverted", "allow_pause",
        "auto_pause_disabled",                # the opposite, or not a stop
        "evse_1_1_1_1_pause_session",         # a device name is no own name
        "charging_enabled", "charge_control", "garo_laddbox",
        "pausenraum_charge",                  # a word that only starts so
        "", None])
    def test_anything_else(self, name):
        assert not names_a_charge_pause(name)

    def test_a_pause_of_anything(self):
        assert names_a_pause("pause_dynamic")
        assert not names_a_pause("pausenraum")

    def test_a_pause_of_something_else(self):
        assert names_another_pause("pause_dynamic")
        for name in ("paused", "pause_session", "pause_resume", "charging"):
            assert not names_another_pause(name), name


# ═══════════════════════════════════════════════════════════════════════
# The sense: the key first, the id when there is no key
# ═══════════════════════════════════════════════════════════════════════

class TestTheSense:
    def test_the_v2c_session_pause(self):
        with _registry_patch(_v2c()):
            assert on_means_paused(MagicMock(), PAUSE)

    def test_a_german_v2c_is_read_by_its_key(self):
        """HA builds the id in the install's language ("Vorgang
        pausieren"); the key stays ``paused``."""
        sw = "switch.evse_1_1_1_1_vorgang_pausieren"
        with _registry_patch([_row(sw, key="paused")]):
            assert on_means_paused(MagicMock(), sw)

    def test_the_modulation_pause_keeps_its_old_sense(self):
        """Not a stop control at all — SEM's writes to it stay as they were
        (the builder swaps it, below)."""
        with _registry_patch(_v2c()):
            assert not on_means_paused(MagicMock(), PAUSE_DYNAMIC)

    def test_the_key_beats_a_renamed_id(self):
        """An owner who renamed NRGkick's "Charging enabled" to ``…_pause``
        renamed the label, not what on does."""
        sw = "switch.nrgkick_pause"
        with _registry_patch([_row(sw, key="charging_enabled",
                                   platform="nrgkick")]):
            assert not on_means_paused(MagicMock(), sw)

    def test_wallbox_pause_resume_is_on_while_it_charges(self):
        sw = "switch.wallbox_pulsar_plus_pause_resume"
        with _registry_patch([_row(sw, key="pause_resume",
                                   platform="wallbox")]):
            assert not on_means_paused(MagicMock(), sw)
            assert switch_service(MagicMock(), sw, run=True) == "turn_on"

    def test_an_integration_without_keys_is_read_by_its_own_name(self):
        sw = "switch.ohme_home_pause_charge"
        with _registry_patch([_row(sw, platform="ohme",
                                   original_name="Pause charge")]):
            assert on_means_paused(MagicMock(), sw)

    def test_a_full_name_is_not_an_own_name(self):
        """Without ``has_entity_name`` the name carries the device's."""
        sw = "switch.ohme_home_pause_charge"
        with _registry_patch([_row(sw, platform="ohme",
                                   original_name="Ohme Home Pause charge",
                                   has_entity_name=False)]):
            assert not on_means_paused(MagicMock(), sw)

    @pytest.mark.parametrize("sw,platform", [
        ("switch.v2c_pause_inverted", "template"),   # an owner's workaround
        ("input_boolean.ev_pause", "input_boolean"),
    ])
    def test_a_switch_the_owner_made_keeps_on_as_start(self, sw, platform):
        with _registry_patch([_row(sw, platform=platform,
                                   original_name=sw.split(".")[1],
                                   has_entity_name=False)]):
            assert not on_means_paused(MagicMock(), sw)

    def test_no_registry_entry_keeps_on_as_start(self):
        with _registry_patch([]):
            assert not on_means_paused(MagicMock(), "switch.ev_pause")
        assert not on_means_paused(None, "switch.ohme_pause_charge")

    def test_a_button_has_no_sense(self):
        with _registry_patch([_row("button.v2c_pause", key="paused")]):
            assert not on_means_paused(MagicMock(), "button.v2c_pause")

    def test_writes_and_reads(self):
        with _registry_patch(_v2c()):
            hass = MagicMock()
            assert switch_service(hass, PAUSE, run=True) == "turn_off"
            assert switch_service(hass, PAUSE, run=False) == "turn_on"
            assert reads_running(hass, PAUSE, "off") is True
            assert reads_running(hass, PAUSE, "on") is False
            assert reads_running(hass, PAUSE, "unavailable") is None
            assert reads_running(hass, PAUSE, None) is None


# ═══════════════════════════════════════════════════════════════════════
# The box: a V2C that pauses on turn_on, as core's switch does
# ═══════════════════════════════════════════════════════════════════════

class _States:
    def __init__(self):
        self._m = {}

    def set(self, eid, state, **attrs):
        self._m[eid] = SimpleNamespace(state=state, attributes=attrs)

    def get(self, eid):
        return self._m.get(eid)


class _Trydan:
    """Core's ``V2CSwitchEntity`` for key ``paused``: on = paused."""

    def __init__(self, paused: bool):
        self.paused = paused
        self.calls: list = []
        self.hass = MagicMock()
        self.hass.states = _States()
        self.hass.states.set(NUM, "16", min=6, max=32, step=1)
        self._show()
        self.hass.services.async_call = AsyncMock(side_effect=self._call)
        self.hass.services.has_service = MagicMock(return_value=False)

    def _show(self):
        self.hass.states.set(PAUSE, "on" if self.paused else "off")

    async def _call(self, domain, service, data=None, **_kw):
        self.calls.append((domain, service, dict(data or {})))
        if domain == "switch" and (data or {}).get("entity_id") == PAUSE:
            self.paused = service == "turn_on"      # evse.pause() / resume()
            self._show()

    def switch_writes(self):
        return [s for d, s, data in self.calls
                if d == "switch" and data.get("entity_id") == PAUSE]


def _charger(box):
    d = CurrentControlDevice(
        hass=box.hass, device_id="v2c_trydan", name="V2C Trydan",
        priority=3, min_current=6.0, max_current=32.0, phases=3,
        voltage=230.0, power_entity_id=POWER, charger_service=None,
        charger_service_entity_id=None, current_entity_id=NUM,
    )
    d.start_stop_entity = PAUSE
    return d


@pytest.mark.asyncio
class TestTheV2cRuns:
    async def test_a_start_resumes_a_paused_box(self):
        box = _Trydan(paused=True)
        with _registry_patch(_v2c()):
            await _charger(box).start_session()
        assert box.switch_writes() == ["turn_off"]
        assert box.paused is False

    async def test_a_stop_pauses_a_running_box(self):
        box = _Trydan(paused=False)
        with _registry_patch(_v2c()):
            await _charger(box).stop_session()
        assert box.switch_writes() == ["turn_on"]
        assert box.paused is True

    async def test_a_start_into_a_running_box_sends_nothing(self):
        """Class 117's rule, read the right way round: "off" is running."""
        box = _Trydan(paused=False)
        with _registry_patch(_v2c()):
            dev = _charger(box)
            await dev.start_session()
        assert box.switch_writes() == []
        assert dev._session_active is True

    async def test_park_off_pauses(self):
        box = _Trydan(paused=False)
        with _registry_patch(_v2c()):
            dev = _charger(box)
            dev._remember_parked = AsyncMock()
            await dev.park_off()
        assert box.switch_writes() == ["turn_on"]
        assert box.paused is True

    async def test_the_hand_back_resumes_a_box_sem_paused(self):
        box = _Trydan(paused=True)
        with _registry_patch(_v2c()):
            dev = _charger(box)
            dev._sem_parked = True
            said = await dev.release_to_user(reason="removal")
        assert box.switch_writes() == ["turn_off"]
        assert box.paused is False
        assert said and PAUSE in said

    async def test_the_hand_back_leaves_a_running_box_alone(self):
        box = _Trydan(paused=False)
        with _registry_patch(_v2c()):
            dev = _charger(box)
            dev._sem_parked = True
            await dev.release_to_user(reason="removal")
        assert box.switch_writes() == []

    async def test_the_adapter_reads_the_pause(self):
        box = _Trydan(paused=True)
        with _registry_patch(_v2c()):
            adapter = adapter_for(_charger(box))
            assert type(adapter) is GenericAdapter
            assert adapter.enable_state() == (False, True)
            await adapter.ensure_enabled()
            assert box.switch_writes() == ["turn_off"]
            assert adapter.enable_state() == (True, True)
            box.hass.states.set(PAUSE, "unavailable")
            assert adapter.enable_state() == (None, False)

    async def test_through_the_reconciler(self):
        """SEM wants 16 A from a paused box, then wants it off. The box ends
        charging, then paused — and SEM never sent the start as a pause."""
        box = _Trydan(paused=True)
        with _registry_patch(_v2c()):
            dev = _charger(box)
            adapter = adapter_for(dev)
            rec = ChargerReconciler("v2c_trydan", heartbeat_s=60.0)
            now = 1000.0
            for _ in range(3):
                await rec.reconcile_and_apply(
                    ChargerDecision(
                        charger_id="v2c_trydan", mode="always_max",
                        intent=ChargerIntent.CHARGE_AT_AMPS,
                        commanded_amps=16, reason="test", budget_w=0.0),
                    adapter,
                    ChargerPower(charger_id="v2c_trydan",
                                 power_w=0.0 if box.paused else 11000.0,
                                 connected=True, charging=not box.paused),
                    now)
                now += 70.0
            assert box.paused is False, box.calls
            assert "turn_on" not in box.switch_writes(), box.calls
            sets = [d["value"] for dom, s, d in box.calls
                    if (dom, s) == ("number", "set_value")]
            assert 16 in sets, "non-vacuous: the current write must land"
            for _ in range(3):
                await rec.reconcile_and_apply(
                    ChargerDecision(
                        charger_id="v2c_trydan", mode="off",
                        intent=ChargerIntent.DISABLE,
                        commanded_amps=0, reason="test", budget_w=0.0),
                    adapter,
                    ChargerPower(charger_id="v2c_trydan",
                                 power_w=0.0 if box.paused else 11000.0,
                                 connected=True, charging=not box.paused),
                    now)
                now += 70.0
        assert box.paused is True, box.calls


@pytest.mark.asyncio
class TestASwitchNamedForTheChargeIsUnchanged:
    """Non-vacuous for the sense: the same box with a switch whose on is
    the charge still gets turn_on to start and turn_off to stop."""

    @pytest.mark.parametrize("sw,key", [
        ("switch.wallbox_pulsar_plus_pause_resume", "pause_resume"),
        ("switch.nrgkick_charging_enabled", "charging_enabled"),
        ("switch.wallbox_charge_control", None),
    ])
    async def test_start_and_stop(self, sw, key):
        hass = MagicMock()
        hass.states = _States()
        hass.states.set(sw, "off")
        hass.services.async_call = AsyncMock(return_value=None)
        hass.services.has_service = MagicMock(return_value=False)
        with _registry_patch([_row(sw, key=key, platform="nrgkick")]):
            dev = CurrentControlDevice(
                hass=hass, device_id="c", name="c", priority=3,
                min_current=6.0, max_current=32.0, phases=3, voltage=230.0,
                power_entity_id=POWER, charger_service=None,
                charger_service_entity_id=None, current_entity_id=NUM)
            dev.start_stop_entity = sw
            await dev.start_session()
            hass.states.set(sw, "on")
            await dev.stop_session()
        sent = [c.args[1] for c in hass.services.async_call.await_args_list
                if c.args[0] == "switch"]
        assert sent == ["turn_on", "turn_off"]


# ═══════════════════════════════════════════════════════════════════════
# Detection: the session pause, never the modulation pause
# ═══════════════════════════════════════════════════════════════════════

class TestV2cDetection:
    def test_every_order_binds_the_session_pause(self):
        for order in itertools.permutations(_SWITCHES):
            charger = _discover(_v2c(list(order)))
            assert charger["ev_start_stop_entity"] == PAUSE, order

    def test_the_trap_is_real(self):
        """Non-vacuous: the old rule, run — "pause" in the own name, last
        wins — on HA's registration order binds the modulation pause."""
        rows = _v2c()
        own = _own_names(rows)
        old = None
        for e in rows:
            if e.entity_id.startswith("switch.") and \
                    "pause" in own.get(e.entity_id, ""):
                old = e.entity_id
        assert old == PAUSE_DYNAMIC

    def test_a_german_v2c_binds_its_session_pause(self):
        rename = {
            PAUSE: "switch.evse_1_1_1_1_vorgang_pausieren",
            PAUSE_DYNAMIC: ("switch.evse_1_1_1_1_modulation_der_"
                            "dynamischen_steuerung_pausieren"),
        }
        switches = [(rename.get(e, e), k) for e, k in _SWITCHES]
        charger = _discover(_v2c(switches))
        assert charger["ev_start_stop_entity"] == rename[PAUSE]

    def test_without_keys_the_own_name_decides(self):
        """A row with no key but the integration's own name: "Pause session"
        is the charge, "Pause dynamic control modulation" is not — and the
        run time reads the one it binds the same way."""
        names = {PAUSE: "Pause session",
                 PAUSE_DYNAMIC: "Pause dynamic control modulation"}
        rows = [_row(e, original_name=names.get(e))
                for e, _k in reversed(_SWITCHES)] + _v2c(switches=[])
        raw = _discover_v2c(rows)
        assert raw["ev_start_stop_entity"] == PAUSE
        with _registry_patch(rows):
            assert switch_service(MagicMock(), PAUSE, run=True) == "turn_off"

    def test_a_name_the_run_time_cannot_read_is_not_bound(self):
        """No key and no own name: SEM could not tell which way it is on,
        so it does not take it as the start/stop (the #627 Repair says
        so) rather than drive it as on-while-charging."""
        rows = [_row(e, has_entity_name=False) for e, _k in _SWITCHES]
        raw = _discover_v2c(rows + _v2c(switches=[]))
        assert "ev_start_stop_entity" not in raw


# ═══════════════════════════════════════════════════════════════════════
# The heal: a V2C saved with the modulation pause
# ═══════════════════════════════════════════════════════════════════════

def _built(saved, rows):
    """A charger as the builders leave it: ``wire_current_entity`` runs
    after the saved start/stop entity is set."""
    box = _Trydan(paused=True)
    with _registry_patch(rows):
        dev = _charger(box)
        dev.start_stop_entity = saved
        wire_current_entity(box.hass, dev, "v2c_trydan", NUM)
    return box, dev


class TestTheSavedModulationPause:
    def test_it_is_swapped_for_the_session_pause(self):
        _box, dev = _built(PAUSE_DYNAMIC, _v2c())
        assert dev.start_stop_entity == PAUSE

    @pytest.mark.asyncio
    async def test_and_then_starts_the_charge(self):
        box, dev = _built(PAUSE_DYNAMIC, _v2c())
        with _registry_patch(_v2c()):
            await dev.start_session()
        assert box.switch_writes() == ["turn_off"]
        assert box.paused is False

    @pytest.mark.parametrize("saved", [
        PAUSE, "switch.evse_1_1_1_1_lock_evse", None])
    def test_anything_else_is_kept(self, saved):
        _box, dev = _built(saved, _v2c())
        assert dev.start_stop_entity == saved

    def test_no_twin_no_swap_and_says_so(self, caplog):
        """Configured, so the #627 Repair stays quiet: the log speaks."""
        rows = [r for r in _v2c() if r.entity_id != PAUSE]
        _box, dev = _built(PAUSE_DYNAMIC, rows)
        assert dev.start_stop_entity == PAUSE_DYNAMIC
        assert any("#1042" in r.getMessage() and r.levelname == "WARNING"
                   for r in caplog.records)

    def test_two_twins_no_guess(self):
        rows = _v2c() + [_row("switch.evse_1_1_1_1_pause_charge",
                              key="pause_charge")]
        _box, dev = _built(PAUSE_DYNAMIC, rows)
        assert dev.start_stop_entity == PAUSE_DYNAMIC

    def test_another_device_is_not_a_twin(self):
        rows = [r for r in _v2c() if r.entity_id != PAUSE] + [
            _row("switch.evse_2_pause_session", key="paused", device="v2c-2")]
        _box, dev = _built(PAUSE_DYNAMIC, rows)
        assert dev.start_stop_entity == PAUSE_DYNAMIC

    def test_wallbox_is_never_swapped(self):
        sw = "switch.wallbox_pulsar_plus_pause_resume"
        rows = [_row(sw, key="pause_resume", platform="wallbox",
                     device="wb"),
                _row("switch.wallbox_pulsar_plus_x", key="paused",
                     platform="wallbox", device="wb")]
        with _registry_patch(rows):
            assert charge_pause_twin(MagicMock(), sw) is None


# ═══════════════════════════════════════════════════════════════════════
# The guard: no start/stop write or read spells on or off itself
# ═══════════════════════════════════════════════════════════════════════

_NAMES_THE_SWITCH = ("start_stop_entity", "_start_stop_entity",
                     "_pause_switch_entity", "_discover_pause_switch")
# The config key names the switch only where its value is fetched — a list
# of keys (a migration) does not.
_CONFIG_KEY = "ev_start_stop_entity"
_SPELLED = ("turn_on", "turn_off", "on", "off")
_SPELLED_NAMES = ("STATE_ON", "STATE_OFF", "SERVICE_TURN_ON",
                  "SERVICE_TURN_OFF")


def _own_nodes(fn):
    """The nodes of ``fn`` without those of a function inside it."""
    nested = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
    stack = [n for n in fn.body if not isinstance(n, nested)]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(c for c in ast.iter_child_nodes(node)
                     if not isinstance(c, nested))


def _offenders(source: str, where: str = "<src>"):
    out = []
    for fn in ast.walk(ast.parse(source)):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        nodes = list(_own_nodes(fn))
        params = {a.arg for a in ast.walk(fn.args)
                  if isinstance(a, ast.arg)}
        fetched = [n.slice for n in nodes if isinstance(n, ast.Subscript)] + [
            n.args[0] for n in nodes
            if isinstance(n, ast.Call) and n.args
            and isinstance(n.func, ast.Attribute) and n.func.attr == "get"]
        names = bool(params & set(_NAMES_THE_SWITCH)) or any(
            isinstance(k, ast.Constant) and k.value == _CONFIG_KEY
            for k in fetched) or any(
            (isinstance(n, ast.Attribute) and n.attr in _NAMES_THE_SWITCH)
            or (isinstance(n, ast.Name) and n.id in _NAMES_THE_SWITCH)
            or (isinstance(n, ast.Constant) and n.value in _NAMES_THE_SWITCH)
            for n in nodes)
        if not names:
            continue
        spelled = sorted(
            {n.value for n in nodes if isinstance(n, ast.Constant)
             and n.value in _SPELLED}
            | {n.id for n in nodes if isinstance(n, ast.Name)
               and n.id in _SPELLED_NAMES}
            | {n.attr for n in nodes if isinstance(n, ast.Attribute)
               and n.attr in _SPELLED_NAMES})
        if spelled:
            out.append(f"{where}:{fn.lineno} {fn.name} {spelled}")
    return out


class TestTheGuard:
    def test_no_start_stop_switch_is_written_or_read_by_hand(self):
        found = []
        for path in sorted(PKG.rglob("*.py")):
            rel = path.relative_to(PKG).as_posix()
            if rel.startswith(("tests/", "tools/", "scripts/")):
                continue
            found += _offenders(path.read_text(encoding="utf-8"), rel)
        assert found == [], (
            "a function that names the start/stop switch spells on/off "
            "itself — use utils.switch_sense (#1042): " + "; ".join(found))

    @pytest.mark.parametrize("src", [
        # the pre-fix start_session / stop_session / ensure_enabled shapes
        "async def f(self):\n"
        "    await self.send('switch', 'turn_on', "
        "{'entity_id': self.start_stop_entity})\n",
        "def f(dev):\n"
        "    ent = getattr(dev, 'start_stop_entity', None)\n"
        "    return dev.hass.states.get(ent).state == 'on'\n",
        "async def f(self):\n"
        "    eid = self._discover_pause_switch()\n"
        "    service = 'turn_on' if x else 'turn_off'\n",
        "def f(self):\n"
        "    return self.hass.states.get(self._start_stop_entity).state "
        "== STATE_ON\n",
        "async def f(hass, start_stop_entity):\n"
        "    await hass.services.async_call('switch', SERVICE_TURN_OFF, "
        "{'entity_id': start_stop_entity})\n",
        "def f(cfg, hass):\n"
        "    return hass.states.get(cfg['ev_start_stop_entity']).state "
        "== 'on'\n",
    ])
    def test_the_guard_catches_the_old_shapes(self, src):
        assert _offenders(src)

    def test_a_nested_function_is_judged_alone(self):
        src = ("def outer(self):\n"
               "    x = self.start_stop_entity\n"
               "    def inner():\n"
               "        return 'turn_on'\n")
        assert _offenders(src) == []
