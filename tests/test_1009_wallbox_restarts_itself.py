"""#1009 — SEM stays silent when the wallbox restarts itself.

PROD, 26.09.2026, a real KEBA P30, twice: the user pressed Pause (or chose
Off), SEM sent ONE stop and went hands-off, and 600-630 s later the box
switched itself back on at 8 A — its failsafe fallback. SEM said nothing.

Two things were wrong, neither in the pause itself:

* the hands-off row (#898) skipped the #823 recogniser on purpose ("no
  failsafe naming"), written believing the #740 dead-man OFF holds the box.
  On the official KEBA integration it cannot: the fallback floor is 6 A, the
  0 A is refused without an error, and the charging failsafe SEM armed at
  start (#546, 600 s / floor) is what brings the box back;
* ``arm_failsafe_off`` logged "re-armed as dead-man's OFF, fallback 0 A" as
  success — the instrument reporting a result it never checked (#925).

So: the hands-off row observes (never commands) and names the constant
interval on the second episode; the arm reads the box back and says what
it did, once; a withheld observer write reads nothing back, because the
rigs share the real box.
"""
from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.solar_energy_management.coordinator.charger_reconciler import (
    Action,
    ActionKind,
    ChargerReconciler,
    DesiredState,
    ObservedState,
)
from custom_components.solar_energy_management.devices.base import (
    DEADMAN_MAX_TRIES,
    FAILSAFE_READBACK_DELAY_S,
    CurrentControlDevice,
)

HEARTBEAT_S = 5.0


def _rec() -> ChargerReconciler:
    return ChargerReconciler(charger_id="ev_charger", heartbeat_s=HEARTBEAT_S,
                             idle_disable_threshold=4)


def _obs(charging=False, power=0.0) -> ObservedState:
    return ObservedState(charging=charging, setpoint_a=0,
                         self_charging=False, power_w=power)


def _off_episode(rec, t0: float, back_after: float) -> list:
    """Off (hands-off): SEM's one stop lands, the box settles, then it comes
    back by itself ``back_after`` seconds after the stop.

    The row's stop latch starts SET (a reconciler built while the mode is
    already Off must not stop a session it never started), so every episode
    is entered from a charging cycle — as on a live install."""
    rec.reconcile(DesiredState.CHARGE, 10, _obs(charging=True, power=4100.0), now=t0 - 60.0)
    a = rec.reconcile(DesiredState.RELEASED, 0, _obs(charging=True, power=4100.0), now=t0)
    assert a == [Action(ActionKind.DISABLE)], f"the one stop did not land: {a}"
    rec.reconcile(DesiredState.RELEASED, 0, _obs(charging=False), now=t0 + 30.0)
    return rec.reconcile(DesiredState.RELEASED, 0,
                         _obs(charging=True, power=3200.0), now=t0 + back_after)


def _leave_off(rec, now: float) -> None:
    rec.reconcile(DesiredState.CHARGE, 10, _obs(charging=True, power=4100.0), now=now)


def _reports(acts):
    return [x for x in acts if x.kind is ActionKind.REPORT_FAILSAFE_SUSPECTED]


class TestHandsOffStillNamesTheBox:
    def test_the_second_episode_names_the_failsafe(self):
        rec = _rec()
        a1 = _off_episode(rec, 1000.0, 620.0)
        assert Action(ActionKind.DISABLE) not in a1, "hands-off: no second stop"
        assert not _reports(a1), "one gap could be anything (#611)"
        _leave_off(rec, 1000.0 + 700.0)
        a2 = _off_episode(rec, 3000.0, 630.0)          # within 2 % of 620
        rep = _reports(a2)
        assert len(rep) == 1
        assert abs(rep[0].interval_s - 625.0) < 1.0
        assert Action(ActionKind.DISABLE) not in a2

    def test_jittery_returns_are_not_a_failsafe(self):
        rec = _rec()
        _off_episode(rec, 1000.0, 620.0)
        _leave_off(rec, 1700.0)
        a2 = _off_episode(rec, 3000.0, 700.0)          # 80 s apart: a car, a human
        assert not _reports(a2)

    def test_hands_off_holds_after_the_box_comes_back(self):
        rec = _rec()
        _off_episode(rec, 1000.0, 620.0)
        for i in range(1, 30):
            acts = rec.reconcile(DesiredState.RELEASED, 0,
                                 _obs(charging=True, power=3200.0),
                                 now=1620.0 + 10.0 * i)
            assert acts == [Action(ActionKind.NONE)], f"cycle {i}: {acts}"

    def test_sub_minute_returns_are_not_counted(self):
        rec = _rec()
        _off_episode(rec, 1000.0, 45.0)
        _leave_off(rec, 1100.0)
        a2 = _off_episode(rec, 3000.0, 45.0)
        assert not _reports(a2)

    def test_an_off_with_nothing_charging_measures_nothing(self):
        """The anchor is this row's own stop, not some earlier row's."""
        rec = _rec()
        _off_episode(rec, 1000.0, 620.0)               # gap 1 = 620
        # leave Off without SEM ever wanting to charge (IDLE, nothing drawing),
        # then Off again with the car still not charging: no intent, no draw,
        # so the row issues no stop
        rec.reconcile(DesiredState.IDLE, 0, _obs(charging=False), now=1700.0)
        rec.reconcile(DesiredState.IDLE, 0, _obs(charging=False), now=2940.0)
        a = rec.reconcile(DesiredState.RELEASED, 0, _obs(charging=False), now=3000.0)
        assert Action(ActionKind.DISABLE) not in a
        rec.reconcile(DesiredState.RELEASED, 0, _obs(charging=False), now=3030.0)
        # something starts the car 620 s later — with no stop of ours behind it
        a = rec.reconcile(DesiredState.RELEASED, 0, _obs(charging=True, power=3200.0), now=3620.0)
        assert not _reports(a), "620 after a stop we never issued must not pair with gap 1"

    def test_an_off_row_gap_does_not_pair_with_the_first_hands_off_gap(self):
        """A VPP export pause or the phase guard stops through the OFF row
        (a DISABLE, not the user's Off). Its stop-war gap must not complete
        the user's very first Off episode — and a hands-off gap must not
        complete the OFF row's pair either. Own lists, one shared Repair."""
        rec = _rec()
        # OFF row: SEM's DISABLE lands, the box comes back 620 s later (#823 accounting)
        rec.reconcile(DesiredState.CHARGE, 10, _obs(charging=True, power=4100.0), now=900.0)
        a = rec.reconcile(DesiredState.OFF, 0, _obs(charging=True, power=4100.0), now=1000.0)
        assert Action(ActionKind.DISABLE) in a
        rec.reconcile(DesiredState.OFF, 0, _obs(charging=False), now=1030.0)
        a = rec.reconcile(DesiredState.OFF, 0, _obs(charging=True, power=4100.0), now=1620.0)
        assert not _reports(a)
        # the user's FIRST Off: same 620 s return — must not speak yet
        a2 = _off_episode(rec, 3000.0, 620.0)
        assert not _reports(a2), "one hands-off gap must not pair with an OFF-row gap"
        # the user's second Off: now it speaks, from its own two gaps
        _leave_off(rec, 3700.0)
        a3 = _off_episode(rec, 5000.0, 620.0)
        assert len(_reports(a3)) == 1

    def test_it_reports_once_then_a_held_stop_clears_it(self):
        rec = _rec()
        _off_episode(rec, 1000.0, 620.0)
        _leave_off(rec, 1700.0)
        assert _reports(_off_episode(rec, 3000.0, 630.0))
        _leave_off(rec, 3700.0)
        # third episode: the user fixed the box — the stop holds
        rec.reconcile(DesiredState.CHARGE, 10, _obs(charging=True, power=4100.0), now=4940.0)
        a = rec.reconcile(DesiredState.RELEASED, 0, _obs(charging=True, power=4100.0), now=5000.0)
        assert a == [Action(ActionKind.DISABLE)]
        quiet = []
        for t in (5030.0, 5600.0, 6200.0, 6300.0):
            quiet += rec.reconcile(DesiredState.RELEASED, 0, _obs(charging=False), now=t)
        clears = [x for x in quiet if x.kind is ActionKind.CLEAR_FAILSAFE_SUSPECTED]
        assert len(clears) == 1
        assert not _reports(quiet)


# ---- the read-back ---------------------------------------------------------

def _keba(hass=None):
    hass = hass or MagicMock()
    hass.services.async_call = AsyncMock()
    hass.services.has_service = MagicMock(return_value=True)
    hass.pending = []
    hass.async_create_task = hass.pending.append
    dev = CurrentControlDevice(
        hass, "keba1", "Keba",
        charger_service="keba.set_current",
        min_current=10, max_current=16,
    )
    return dev, hass


def _fs_state(fallback, timeout=600, eid="binary_sensor.keba_p30_failsafe_mode"):
    st = MagicMock()
    st.entity_id = eid
    st.attributes = {"fallback_current": fallback, "failsafe_timeout": timeout}
    return st


def _run_now(hass, delay, action):
    action(None)


async def _arm(dev, hass):
    """One stop's dead-man's OFF, every resend (#1163) included."""
    await dev.arm_failsafe_off()
    while hass.pending:
        await hass.pending.pop(0)


@pytest.fixture(autouse=True)
def _no_settle(monkeypatch):
    monkeypatch.setattr(
        "custom_components.solar_energy_management.devices.base.DEADMAN_SETTLE_S", 0)


@pytest.mark.asyncio
class TestTheReadBackSaysWhatTheBoxDid:
    async def test_a_refused_zero_is_a_warning_once(self, caplog):
        dev, hass = _keba()
        hass.states.async_all = MagicMock(return_value=[_fs_state(8.0)])
        with patch("homeassistant.helpers.event.async_call_later", side_effect=_run_now) as later, \
             caplog.at_level(logging.INFO):
            await _arm(dev, hass)
            await _arm(dev, hass)
        assert later.call_count == 2 * DEADMAN_MAX_TRIES, "each stop asks every try (#1163)"
        assert later.call_args.args[1] == FAILSAFE_READBACK_DELAY_S
        warns = [r for r in caplog.records
                 if r.levelno == logging.WARNING and "refuses a 0 A fallback" in r.getMessage()]
        assert len(warns) == 1, "once per box, not once per stop"
        assert "600 s / 8 A" in warns[0].getMessage()
        assert not [r for r in caplog.records if "re-armed as dead-man's OFF" in r.getMessage()], \
            "the old success line is gone"
        assert [r for r in caplog.records if "asked the box for a dead-man's OFF" in r.getMessage()]

    async def test_a_zero_fallback_is_confirmed_not_warned(self, caplog):
        dev, hass = _keba()
        hass.states.async_all = MagicMock(return_value=[_fs_state(0.0, timeout=10)])
        with patch("homeassistant.helpers.event.async_call_later", side_effect=_run_now), \
             caplog.at_level(logging.INFO):
            await _arm(dev, hass)
        assert not [r for r in caplog.records if r.levelno == logging.WARNING]
        assert [r for r in caplog.records if "confirmed on read-back" in r.getMessage()]

    async def test_no_readback_entity_is_not_a_confirmation(self, caplog):
        dev, hass = _keba()
        hass.states.async_all = MagicMock(return_value=[])
        with patch("homeassistant.helpers.event.async_call_later", side_effect=_run_now), \
             caplog.at_level(logging.DEBUG):
            await _arm(dev, hass)
        msgs = [r.getMessage() for r in caplog.records]
        assert not [m for m in msgs if "confirmed on read-back" in m]
        assert not [r for r in caplog.records if r.levelno == logging.WARNING]
        assert [m for m in msgs if "cannot be verified" in m]

    async def test_another_brands_failsafe_entity_is_ignored(self, caplog):
        dev, hass = _keba()
        hass.states.async_all = MagicMock(return_value=[
            _fs_state(6.0, eid="binary_sensor.other_box_failsafe_mode")])
        with patch("homeassistant.helpers.event.async_call_later", side_effect=_run_now), \
             caplog.at_level(logging.DEBUG):
            await _arm(dev, hass)
        assert not [r for r in caplog.records if r.levelno == logging.WARNING]

    async def test_a_fixed_box_makes_a_later_relapse_worth_saying_again(self, caplog):
        dev, hass = _keba()
        hass.states.async_all = MagicMock(return_value=[_fs_state(8.0)])
        with patch("homeassistant.helpers.event.async_call_later", side_effect=_run_now), \
             caplog.at_level(logging.INFO):
            await _arm(dev, hass)                          # refused → warning
            hass.states.async_all.return_value = [_fs_state(0.0, timeout=10)]
            await _arm(dev, hass)                          # fixed → confirmed, flag cleared
            hass.states.async_all.return_value = [_fs_state(8.0)]
            await _arm(dev, hass)                          # relapse → warning again
        warns = [r for r in caplog.records if r.levelno == logging.WARNING and "refuses a 0 A fallback" in r.getMessage()]
        assert len(warns) == 2

    async def test_no_brand_token_reads_nothing(self, caplog):
        """Without a token to match, any failsafe sensor could be another
        charger's — reading it would be a guess (#925)."""
        hass = MagicMock(); hass.services.async_call = AsyncMock(); hass.services.has_service = MagicMock(return_value=True)
        dev = CurrentControlDevice(hass, "c1", "Box", current_entity_id="number.box_current",
                                  min_current=6, max_current=16)
        hass.states.async_all = MagicMock(return_value=[_fs_state(8.0, eid="binary_sensor.somebox_failsafe_mode")])
        assert dev._brand_key() == ""
        assert dev._failsafe_readback_state() is None

    async def test_a_withheld_observer_write_reads_nothing_back(self, caplog):
        dev, hass = _keba()
        dev.observer_mode = True
        hass.states.async_all = MagicMock(return_value=[_fs_state(8.0)])
        with patch("homeassistant.helpers.event.async_call_later", side_effect=_run_now) as later, \
             caplog.at_level(logging.INFO):
            await _arm(dev, hass)
        assert later.call_count == 0, "the rigs share the real box: never judge a write SEM did not make"
        assert not [r for r in caplog.records if r.levelno == logging.WARNING]
