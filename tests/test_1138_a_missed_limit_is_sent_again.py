"""#1138 — a discharge limit the battery did not keep is sent once more.

The 100 W same-limit skip keeps SEM from writing every cycle (#538, #900).
It also held the old register value after a miss, until the wanted limit
moved. After a judged miss, exactly one more send goes out per wanted limit.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.solar_energy_management.coordinator.battery_adapters import (
    generic as generic_mod,
)
from custom_components.solar_energy_management.coordinator.battery_adapters.generic import (
    GenericBatteryAdapter,
)

ENTITY = "number.limit"


def _hass(seen):
    hass = MagicMock()
    hass.states = MagicMock()
    hass.states.get = MagicMock(return_value=SimpleNamespace(
        state=str(seen), attributes={"unit_of_measurement": "W"}))
    return hass


def _adapter(seen):
    ad = GenericBatteryAdapter(_hass(seen), {
        "battery_discharge_control_entity": ENTITY})
    ad._max_discharge_w = 10000.0
    return ad


async def _cycle(ad, wanted, writes, judge=True):
    with patch.object(generic_mod, "async_write_power_setpoint_verbose",
                      new=AsyncMock(side_effect=lambda *a, **k: (
                          writes.append(a[2]) or (True, True)))):
        await ad.command_limit_discharge(wanted)
    if judge:
        with patch("time.monotonic", return_value=1e12):
            return ad.verify_pending_write()


@pytest.mark.unit
@pytest.mark.asyncio
class TestAMissedLimitIsSentAgain:

    async def test_a_kept_limit_is_not_sent_again(self):
        writes = []
        ad = _adapter(seen=1500)
        assert await _cycle(ad, 1500.0, writes) is True
        await _cycle(ad, 1520.0, writes, judge=False)
        assert writes == [1500.0]

    async def test_a_missed_limit_is_sent_once_more(self):
        writes = []
        ad = _adapter(seen=9000)             # the register kept the old value
        assert await _cycle(ad, 1500.0, writes) is False
        await _cycle(ad, 1500.0, writes, judge=False)
        assert writes == [1500.0, 1500.0]

    async def test_the_extra_send_happens_once_not_every_cycle(self):
        writes = []
        ad = _adapter(seen=9000)
        await _cycle(ad, 1500.0, writes)             # miss
        await _cycle(ad, 1500.0, writes)             # extra send, missed again
        await _cycle(ad, 1500.0, writes, judge=False)
        await _cycle(ad, 1500.0, writes, judge=False)
        assert writes == [1500.0, 1500.0]

    async def test_a_new_limit_gets_its_own_extra_send(self):
        writes = []
        ad = _adapter(seen=9000)
        await _cycle(ad, 1500.0, writes)
        await _cycle(ad, 1500.0, writes)
        await _cycle(ad, 2500.0, writes)             # moved 1000 W: normal write
        await _cycle(ad, 2500.0, writes, judge=False)  # missed: one more
        assert writes == [1500.0, 1500.0, 2500.0, 2500.0]
