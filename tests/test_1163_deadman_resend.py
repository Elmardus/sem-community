"""#1163 — the dead-man's OFF is lost when it leaves with the box's own poll.

PROD, 24.10.2026, KEBA P30 with debug logging: a stop starts the
integration's fast poll at once, and a ``set_failsafe`` in the same
millisecond got no answer — the box kept 600 s / 8 A and switched itself
back on ~10 min later (14 of 16 stops that day). SEM now waits the burst
out, and resends while the read-back still shows a non-zero fallback —
never once a start has armed the box again.
"""
from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.solar_energy_management.devices import base as base_mod
from custom_components.solar_energy_management.devices.base import (
    DEADMAN_MAX_TRIES,
    CurrentControlDevice,
)


@pytest.fixture(autouse=True)
def _no_settle(monkeypatch):
    monkeypatch.setattr(base_mod, "DEADMAN_SETTLE_S", 0)


def _box(fallback_seq):
    hass = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.services.has_service = MagicMock(return_value=True)
    hass.pending = []
    hass.async_create_task = hass.pending.append
    seq = list(fallback_seq)

    def states(_domain):
        st = MagicMock()
        st.entity_id = "binary_sensor.keba_p30_failsafe_mode"
        fb = seq.pop(0) if len(seq) > 1 else seq[0]
        st.attributes = {"fallback_current": fb, "failsafe_timeout": 600 if fb else 10}
        return [st]
    hass.states.async_all = MagicMock(side_effect=states)
    dev = CurrentControlDevice(hass, "keba1", "Keba", charger_service="keba.set_current",
                               min_current=8, max_current=16)
    return dev, hass


def _zero_asks(hass):
    return [c for c in hass.services.async_call.call_args_list
            if c.args[1] == "set_failsafe" and c.args[2]["failsafe_fallback"] == 0]


async def _drain(hass):
    while hass.pending:
        await hass.pending.pop(0)


def _run_now(hass, delay, action):
    action(None)


@pytest.mark.asyncio
class TestTheDeadManIsAskedAgain:
    async def test_a_dropped_ask_is_resent_until_it_lands(self, caplog):
        dev, hass = _box([8.0, 0.0])
        with patch("homeassistant.helpers.event.async_call_later", side_effect=_run_now), \
             caplog.at_level(logging.INFO):
            await dev.arm_failsafe_off()
            await _drain(hass)
        assert len(_zero_asks(hass)) == 2
        assert [r for r in caplog.records if "confirmed on read-back" in r.getMessage()]
        assert not [r for r in caplog.records if r.levelno == logging.WARNING]

    async def test_a_box_that_never_takes_it_is_warned_after_the_last_try(self, caplog):
        dev, hass = _box([8.0])
        with patch("homeassistant.helpers.event.async_call_later", side_effect=_run_now), \
             caplog.at_level(logging.INFO):
            await dev.arm_failsafe_off()
            await _drain(hass)
        assert len(_zero_asks(hass)) == DEADMAN_MAX_TRIES
        assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1

    async def test_a_start_voids_a_pending_resend(self):
        """A resend after a start would point a charging box at 0 A."""
        dev, hass = _box([8.0])
        later = []
        with patch("homeassistant.helpers.event.async_call_later",
                   side_effect=lambda h, d, a: later.append(a)):
            await dev.arm_failsafe_off()
            await dev.arm_failsafe()            # the next start arms 600 s / floor
            for a in later:
                a(None)
            await _drain(hass)
        assert len(_zero_asks(hass)) == 1, "the stale read-back must not ask again"

    async def test_no_resend_while_a_session_runs(self):
        dev, hass = _box([8.0])
        with patch("homeassistant.helpers.event.async_call_later", side_effect=_run_now):
            dev._session_active = True
            await dev.arm_failsafe_off()
            await _drain(hass)
        assert len(_zero_asks(hass)) == 1

    async def test_the_ask_waits_for_the_poll_burst(self, monkeypatch):
        monkeypatch.setattr(base_mod, "DEADMAN_SETTLE_S", 0.5)
        dev, hass = _box([0.0])
        sleeps = []

        async def fake_sleep(s):
            sleeps.append(s)
        with patch.object(base_mod.asyncio, "sleep", fake_sleep), \
             patch("homeassistant.helpers.event.async_call_later"):
            await dev.arm_failsafe_off()
        assert sleeps and sleeps[0] >= 0.5
        assert len(_zero_asks(hass)) == 1

    async def test_any_start_during_the_wait_cancels_the_ask(self):
        """A start by switch, service or the per-cycle re-enable marks the
        session active without arming the failsafe; it must still win."""
        dev, hass = _box([8.0])

        async def start_meanwhile(_s):
            dev._session_active = True
        with patch.object(base_mod.asyncio, "sleep", start_meanwhile), \
             patch("homeassistant.helpers.event.async_call_later"):
            await dev.arm_failsafe_off()
        assert _zero_asks(hass) == []

    async def test_a_start_after_the_ask_voids_its_resend(self):
        dev, hass = _box([8.0])
        later = []
        with patch("homeassistant.helpers.event.async_call_later",
                   side_effect=lambda h, d, a: later.append(a)):
            await dev.arm_failsafe_off()
            dev._session_active = True
            dev._session_active = False     # stopped again, by a path that sends no ask
            for a in later:
                a(None)
            await _drain(hass)
        assert len(_zero_asks(hass)) == 1
