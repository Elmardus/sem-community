"""#1027 — a SEM that only watches neither parks a charger nor hands one back.

A test install ran with the observer switch ON, watching a KEBA that a
second SEM really controls. Removing it sent ``keba.enable`` and put the box's
failsafe back on its charging fallback. No car was plugged in, so nothing
happened that day.

Three steps had to line up, and each of them read one layer's answer as
another's:

1. ``send`` is the single hardware seam (#855) and it WITHHOLDS while SEM
   watches, returning False. ``park_off`` read "the call did not raise" as
   "the box was parked" and wrote the park debt to disk — on every
   disconnect, for a box it had never touched.
2. The next setup adopted that record, so a lifetime that commands nothing
   believed it had parked a wallbox.
3. Removal hands back every box SEM parked. That gate saw the adopted flag
   and said yes — the first real command of the lifetime, on somebody else's
   hardware.

The rule is #936's, written for batteries and extended to the export cut by
#955: watching takes no debt and pays none. The record belongs to the
lifetime that really parked the box, so it is left exactly as it is — and the
first cycle that can command again adopts it (#949's rule for the pacer).
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.solar_energy_management.devices.base import (
    CurrentControlDevice,
)


def _run(coro):
    return asyncio.run(coro)


class FakeStore:
    """A park record with a visible payload."""

    def __init__(self, data=None):
        self.data = data

    async def async_load(self):
        return self.data

    async def async_save(self, data):
        self.data = dict(data)

    async def async_remove(self):
        self.data = None


def _device(*, watching: bool, store=None, parked=False,
            services=("disable", "enable", "set_failsafe")):
    """A KEBA whose ``send`` is the REAL seam.

    The whole bug is what the layer above the seam records when the seam
    withholds, so a mocked ``send`` would paper over exactly the line under
    test. ``calls`` holds what really reached Home Assistant.
    """
    calls: list = []

    async def _async_call(domain, service, data, blocking=True):
        calls.append((domain, service, dict(data or {})))

    dev = SimpleNamespace(
        name="KEBA P30", device_id="ev_charger", charger_id="ev_charger",
        observer_mode=watching, withheld_commands=[],
        _sem_parked=parked, _park_store=store,
        charger_service="keba.set_current",
        start_service=None, start_service_data=None, service_device_id=None,
        charge_mode_entity=None, charge_mode_start=None, start_stop_entity=None,
        arm_failsafe_enabled=True, steady_failsafe=True, min_current=6,
        _session_active=True, _current_setpoint=6.0, _last_write_at=1.0,
        _status=SimpleNamespace(state=None, current_consumption_w=100.0),
        _set_current=AsyncMock(), arm_failsafe_off=AsyncMock(),
        hass=SimpleNamespace(services=SimpleNamespace(
            has_service=lambda d, s: s in services,
            async_call=_async_call)),
    )
    for m in ("send", "park_off", "_remember_parked", "_write_park_record",
              "adopt_park_state",
              "release_to_user", "session_start_mechanism", "arm_failsafe"):
        setattr(dev, m, getattr(CurrentControlDevice, m).__get__(dev))
    dev.calls = calls
    return dev


@pytest.mark.unit
class TestWatchingTakesNoDebt:
    """Step 1 — the seam withheld the disable, so there is no park to record."""

    def test_a_withheld_disable_writes_no_record(self):
        store = FakeStore()
        dev = _device(watching=True, store=store)
        _run(dev.park_off())
        assert dev.calls == [], "watching commands nothing"
        assert store.data is None, "no park happened, so no park is recorded"
        assert dev._sem_parked is False

    def test_a_real_disable_still_writes_one(self):
        """The floor: the same path, commanding. Without this the test above
        passes on any device that simply cannot park."""
        store = FakeStore()
        dev = _device(watching=False, store=store)
        _run(dev.park_off())
        assert ("keba", "disable", {}) in dev.calls
        assert store.data == {"parked": ["ev_charger"]}
        assert dev._sem_parked is True

    def test_a_switch_controlled_box_reads_the_same_answer(self):
        """The other park mechanism, same rule: the claim follows the send."""
        store = FakeStore()
        dev = _device(watching=True, store=store, services=())
        dev.start_stop_entity = "switch.wallbox_charging"
        _run(dev.park_off())
        assert dev.calls == []
        assert store.data is None and dev._sem_parked is False

    def test_the_claim_comes_from_the_send_and_not_from_silence(self):
        """(review) The gate one layer down hides this one: with
        ``_remember_parked`` gated, ``park_off`` could still read "the call
        did not raise" as "the box was parked" and no test would notice. So
        ask the question the other way round — a send that goes nowhere while
        the device believes it commands. That is the line under test, alone.
        """
        store = FakeStore()
        dev = _device(watching=False, store=store)
        dev.send = AsyncMock(return_value=False)     # withheld, did not raise
        _run(dev.park_off())
        assert store.data is None, "no send left, so no park to record"
        assert dev._sem_parked is False

    def test_a_send_that_lands_still_books_the_debt(self):
        store = FakeStore()
        dev = _device(watching=False, store=store)
        dev.send = AsyncMock(return_value=True)
        _run(dev.park_off())
        assert store.data == {"parked": ["ev_charger"]}

    def test_an_existing_record_survives_a_watching_lifetime(self):
        """The record is the commanding lifetime's. A watcher must not pay
        off a debt it did not take — the box would then hold its no with
        nothing left on the system that knows why (#935)."""
        store = FakeStore({"parked": ["ev_charger"]})
        dev = _device(watching=True, store=store)
        _run(dev._remember_parked(False))
        assert store.data == {"parked": ["ev_charger"]}


@pytest.mark.unit
class TestWatchingAdoptsNoPark:
    """Step 2 — taking over a park is taking on the duty to hand the box
    back, and a lifetime that commands nothing cannot owe that."""

    def test_a_watching_device_does_not_adopt(self):
        dev = _device(watching=True)
        dev.adopt_park_state(["ev_charger"])
        assert dev._sem_parked is False

    def test_a_commanding_device_still_adopts(self):
        dev = _device(watching=False)
        dev.adopt_park_state(["ev_charger"])
        assert dev._sem_parked is True

    def test_the_setup_adopter_asks_the_coordinator_not_the_device(self):
        """It runs before the first cycle, and a device learns its mode ON a
        cycle — so a device asked here answers with the default, which is
        "I act". The store is not even read."""
        from custom_components.solar_energy_management import (
            _async_adopt_parked_chargers,
        )

        store = FakeStore({"parked": ["ev_charger"]})
        dev = _device(watching=False)          # nobody has told it yet
        coord = SimpleNamespace(_observer_mode=True, _ev_devices={"c": dev})
        hass = MagicMock()
        entry = SimpleNamespace(entry_id="01ABCDEFGHIJKLMNOPQRSTUVWX")
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "custom_components.solar_energy_management._park_store",
                lambda h, e: store)
            _run(_async_adopt_parked_chargers(hass, entry, coord))
        assert dev._sem_parked is False
        assert store.data == {"parked": ["ev_charger"]}, "left for a commander"

    def test_a_commanding_setup_adopts(self):
        from custom_components.solar_energy_management import (
            _async_adopt_parked_chargers,
        )

        store = FakeStore({"parked": ["ev_charger"]})
        dev = _device(watching=False)
        coord = SimpleNamespace(_observer_mode=False, _ev_devices={"c": dev})
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "custom_components.solar_energy_management._park_store",
                lambda h, e: store)
            _run(_async_adopt_parked_chargers(
                MagicMock(), SimpleNamespace(entry_id="01A"), coord))
        assert dev._sem_parked is True


@pytest.mark.unit
class TestWatchingHandsNothingBack:
    """Step 3 — the reporter's own gesture: remove a watching SEM."""

    def test_no_command_and_no_claim_on_removal(self):
        store = FakeStore({"parked": ["ev_charger"]})
        dev = _device(watching=True, store=store, parked=True)
        said = _run(dev.release_to_user(reason="integration removed"))
        assert said is None
        assert dev.calls == [], "keba.enable must not leave the process"
        assert store.data == {"parked": ["ev_charger"]}

    def test_a_commanding_removal_still_hands_the_box_back(self):
        store = FakeStore({"parked": ["ev_charger"]})
        dev = _device(watching=False, store=store, parked=True)
        said = _run(dev.release_to_user(reason="integration removed"))
        assert said and "keba.enable" in said
        assert [c[:2] for c in dev.calls] == [("keba", "enable"),
                                              ("keba", "set_failsafe")]
        assert store.data == {"parked": []}

    def test_a_park_left_behind_is_named_in_the_log(self, caplog):
        """(review) The box may be another SEM's, so SEM leaves it alone —
        but the record can still name a box an earlier COMMANDING lifetime of
        this entry really parked, and a removal deletes SEM's own files. This
        is the last moment anything can say so."""
        from custom_components.solar_energy_management import (
            _async_say_the_park_is_unpaid,
        )

        store = FakeStore({"parked": ["ev_charger"]})
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "custom_components.solar_energy_management._park_store",
                lambda h, e: store)
            _run(_async_say_the_park_is_unpaid(
                MagicMock(), SimpleNamespace(entry_id="01A")))
        assert "ev_charger" in caplog.text
        assert "refuses to charge" in caplog.text

    def test_nothing_is_said_when_no_box_is_parked(self, caplog):
        from custom_components.solar_energy_management import (
            _async_say_the_park_is_unpaid,
        )

        store = FakeStore({"parked": []})
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "custom_components.solar_energy_management._park_store",
                lambda h, e: store)
            _run(_async_say_the_park_is_unpaid(
                MagicMock(), SimpleNamespace(entry_id="01A")))
        assert "#1027" not in caplog.text

    def test_the_gate_reads_the_lifetime_captured_before_the_flip(self):
        """The order in ``async_unload_entry`` is load-bearing and invisible:
        ``async_release_batteries_on_unload`` sets observer mode ON part way
        down (#936). A gate reading the flag LIVE, moved below that line,
        would answer "watching" for every install and quietly stop handing
        chargers back. So the gate reads a variable captured at the top, and
        the flag is read exactly once."""
        import ast
        import inspect
        import textwrap

        from custom_components import solar_energy_management as sem

        tree = ast.parse(textwrap.dedent(
            inspect.getsource(sem.async_unload_entry)))
        live = [n for n in ast.walk(tree)
                if isinstance(n, ast.Call)
                and getattr(n.func, "id", "") == "getattr"
                and len(n.args) > 1
                and isinstance(n.args[1], ast.Constant)
                and n.args[1].value == "_observer_mode"]
        assert len(live) == 1, (
            "the lifetime's mode is captured once, at the top; every gate "
            "below reads that variable")

    def test_unload_asks_the_coordinator_before_stashing_a_hand_back(self):
        """The lifetime knows its mode from setup; a device learns it on a
        cycle. So the gate that decides whether a removal hands hardware
        back reads the coordinator, and a removal in the window between
        setup and the first cycle is covered too."""
        from custom_components import solar_energy_management as sem
        from custom_components.solar_energy_management.tests.ast_contracts import (
            reads_flag,
        )

        assert reads_flag(sem.async_unload_entry,
                          "coordinator", "_observer_mode")


@pytest.mark.unit
class TestTheRecordWaitsForACommander:
    """#949's rule, one layer over: leaving observer mode still adopts. A rig
    watched for an hour and then took control of a box it did not know was
    parked would be #935's hole again, through the switch the user is invited
    to use."""

    def _coordinator(self):
        from custom_components.solar_energy_management.coordinator.coordinator import (
            SEMCoordinator,
        )

        fired = []
        coord = SimpleNamespace(
            _observer_mode=True, _ev_devices={}, _ev_device=None,
            _surplus_controller=None,
            _readopt_parked_chargers=lambda: fired.append(1))
        coord._push_observer_mode_to_devices = (
            SEMCoordinator._push_observer_mode_to_devices.__get__(coord))
        coord._readopt_on_leaving_observer = (
            SEMCoordinator._readopt_on_leaving_observer.__get__(coord))
        return coord, fired

    def test_the_first_commanding_cycle_takes_the_park_back(self):
        coord, fired = self._coordinator()
        coord._push_observer_mode_to_devices()          # watching
        assert fired == []
        coord._observer_mode = False
        coord._push_observer_mode_to_devices()          # the switch went off
        assert fired == [1]

    def test_a_steady_lifetime_never_re_reads_the_record(self):
        coord, fired = self._coordinator()
        coord._observer_mode = False
        for _ in range(5):
            coord._push_observer_mode_to_devices()
        assert fired == [], "only the transition adopts, not every cycle"

    def test_watching_on_and_on_adopts_nothing(self):
        coord, fired = self._coordinator()
        for _ in range(5):
            coord._push_observer_mode_to_devices()
        assert fired == []


@pytest.mark.unit
class TestTheGateCannotBeRefactoredAway:
    """The debt has exactly three doors — take it, adopt it, pay it — and
    each one has to ask whether this SEM commands at all. Pinned by the AST
    so a rename or a rewrite fails here instead of on somebody's charger."""

    @pytest.mark.parametrize("name", ["_remember_parked", "adopt_park_state",
                                      "release_to_user"])
    def test_every_door_asks(self, name):
        from custom_components.solar_energy_management.tests.ast_contracts import (
            reads_flag,
        )

        fn = getattr(CurrentControlDevice, name)
        assert reads_flag(fn, "self", "observer_mode"), (
            f"{name} must ask whether SEM commands before it claims, keeps "
            f"or pays a park debt")

    def test_the_hook_is_actually_wired_in_production(self):
        """(review) The behaviour tests below inject their own hook, so the
        one production line that sets it could be deleted with a green
        suite — and "the record is not stranded" would be false."""
        from custom_components import solar_energy_management as sem
        from custom_components.solar_energy_management.tests.ast_contracts import (
            assigns_attribute,
        )

        assert assigns_attribute(sem.async_setup_entry, "coordinator",
                                 "_readopt_parked_chargers")
        assert assigns_attribute(sem.async_unload_entry, "coordinator",
                                 "_readopt_parked_chargers"), (
            "and it is dropped on unload — it holds the entry and the "
            "coordinator alive")

    def test_setup_tells_every_device_its_mode_before_anything_claims(self):
        """"A device nobody told is a device that acts" is the documented
        default of the seam, and until #1027 the only teller was the
        per-cycle push."""
        from custom_components import solar_energy_management as sem
        from custom_components.solar_energy_management.tests.ast_contracts import (
            calls,
        )

        assert calls(sem.async_setup_entry, "_push_observer_mode_to_devices")
