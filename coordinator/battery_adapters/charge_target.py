"""(#1180) The pack's charge-target number, put back after a forced charge.

GoodWe and the generic switch setup write SEM's charge target into the
``battery_target_soc_entity`` number when a grid charge starts. On many
inverters that number is the pack's standing upper limit
(``soc_upper_limit``, ``battery_max_charge_soc`` …). The stop turned the
switch or the mode back and never wrote the old value back, so a night
charge to 60 % left the pack stopping at 60 % on every sunny day after.

The rules are charge pacing's (#949, #820), for the same reasons:

* the value is SAVED before SEM's first write and PUT BACK on the stop;
* the saved value is PERSISTED, so the next lifetime adopts it instead of
  saving SEM's own 60 % as the user's value (an HA restart never unloads
  the entry, so the number keeps SEM's value across it);
* a number whose value cannot be read is never written: the saved value is
  the only way back;
* a number that still reads SEM's own last value is SEM's write not yet
  read back, not the user's setting (class 95): the value saved before it
  stays the one to put back;
* the put-back goes out even when the number already reads the saved
  value — that read may be the one from before SEM's write;
* a put-back that cannot go out (the number unreadable, the call raised)
  is OWED, and tried again at most every ``RETRY_S`` on a cycle with no
  forced charge — never by re-sending the whole stop (class 108).

ONE hold per config entry, keyed by the ENTITY and shared by every
battery: there is no per-battery target list, so two packs behind one
inverter write the same number. A hold per battery would let the second
pack save the first one's 60 % as its own value to put back.

The store is injected at runtime and duck-typed (``async_load`` /
``async_save``), like ``ChargePacingWriter``'s, so this module stays free
of Home Assistant imports.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from ...utils.log_gate import log_on_change

_LOGGER = logging.getLogger(__name__)


@dataclass
class _Target:
    #: the value to put back (as SEM found it)
    prior: Optional[float]
    #: SEM's last value written to the number
    sent: Optional[float] = None
    #: SEM's value is on the number now
    held: bool = False
    #: a put-back that could not go out yet
    owed: bool = False
    #: when the last put-back was tried (monotonic)
    tried_at: Optional[float] = None


def _as_number(raw) -> Optional[float]:
    if isinstance(raw, bool) or not isinstance(raw, (str, int, float)):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if value == value and value not in (float("inf"), float("-inf")) else None


def _wire(value: float):
    """Whole numbers go out as int, as the charge target always did."""
    return int(value) if float(value).is_integer() else float(value)


class ChargeTargetHold:
    """What SEM put on each charge-target number, and what goes back."""

    #: an owed put-back is tried again no faster than this
    RETRY_S: float = 60.0

    def __init__(self, hass, store: Any = None,
                 configured: Iterable[str] = ()) -> None:
        self._hass = hass
        self._store = store
        #: the target entities SEM's config names now; a held one that is
        #: not among them is put back (the user moved the setting)
        self._configured = {str(e) for e in configured if e}
        self._targets: dict[str, _Target] = {}
        self._adopted = store is None
        self._lock = asyncio.Lock()
        self._clock = time.monotonic

    # ─── the commands ────────────────────────────────────────────────

    async def write(self, entity_id: str, value: float) -> bool:
        """Save the number's value (first write only), then write ``value``.

        False when nothing was written: the number cannot be read, so its
        value could never be put back. The charge goes on without it — the
        scheduler stops the charge at its own target anyway."""
        entity_id = str(entity_id)
        async with self._lock:
            await self._adopt()
            t = self._targets.get(entity_id)
            changed = False
            if t is None or not t.held:
                read = self._read(entity_id)
                if read is None:
                    log_on_change(
                        _LOGGER, f"charge_target:{entity_id}", logging.WARNING,
                        "Battery: %s cannot be read — SEM does not write its "
                        "charge target there, as the value could not be put "
                        "back (#1180)", entity_id)
                    return False
                prior = read
                if (t is not None and t.prior is not None and t.sent is not None
                        and abs(read - t.sent) < 0.5):
                    # SEM's own last value, not yet read back (class 95)
                    prior = t.prior
                t = self._targets[entity_id] = _Target(
                    prior=prior, sent=t.sent if t is not None else None,
                    held=True)
                changed = True
                log_on_change(
                    _LOGGER, f"charge_target:{entity_id}", logging.INFO,
                    "Battery: %s saved at %s before SEM's charge target "
                    "(#1180)", entity_id, prior)
            t.owed = False
            if t.sent is None or abs(t.sent - float(value)) >= 0.5:
                t.sent = float(value)
                changed = True
            if changed:
                # saved BEFORE the write: the record must describe a number
                # that may already carry SEM's value (#949)
                await self._save()
            await self._hass.services.async_call(
                "number", "set_value",
                {"entity_id": entity_id, "value": _wire(value)},
            )
            return True

    async def put_back(self, entity_id: str) -> None:
        """The stop: write the saved value back, or leave it owed."""
        async with self._lock:
            await self._adopt()
            t = self._targets.get(str(entity_id or ""))
            if t is not None and t.held:
                await self._put_back(str(entity_id), t)

    async def put_back_owed(self) -> None:
        """A cycle with no forced charge: try the owed put-backs again."""
        async with self._lock:
            await self._adopt()
            now = self._clock()
            for entity_id, t in list(self._targets.items()):
                if not (t.held and t.owed):
                    continue
                if t.tried_at is not None and now - t.tried_at < self.RETRY_S:
                    continue
                await self._put_back(entity_id, t)

    def snapshot(self) -> dict:
        """For diagnostics: each number SEM holds or owes."""
        return {e: {"saved": t.prior, "sent": t.sent, "owed": t.owed}
                for e, t in self._targets.items() if t.held}

    # ─── inside ──────────────────────────────────────────────────────

    async def _put_back(self, entity_id: str, t: _Target) -> None:
        t.tried_at = self._clock()
        if t.prior is None:
            # a record with no value to put back (damaged): nothing to do
            t.held = t.owed = False
            await self._save()
            return
        if self._read(entity_id) is None:
            # Home Assistant drops a write to an unavailable entity without
            # a word — the record would be gone and the value with it
            t.owed = True
            log_on_change(
                _LOGGER, f"charge_target:{entity_id}", logging.WARNING,
                "Battery: %s cannot be read — SEM puts %s back once it can "
                "(#1180)", entity_id, t.prior)
            return
        try:
            await self._hass.services.async_call(
                "number", "set_value",
                {"entity_id": entity_id, "value": _wire(t.prior)},
            )
        except Exception as exc:  # noqa: BLE001 — retried, never fatal
            t.owed = True
            log_on_change(
                _LOGGER, f"charge_target:{entity_id}", logging.WARNING,
                "Battery: putting %s back to %s failed (%s) — SEM tries "
                "again (#1180)", entity_id, t.prior, exc)
            return
        t.held = t.owed = False
        await self._save()
        log_on_change(
            _LOGGER, f"charge_target:{entity_id}", logging.INFO,
            "Battery: %s put back to %s after the forced charge (#1180)",
            entity_id, t.prior)

    def _read(self, entity_id: str) -> Optional[float]:
        try:
            state = self._hass.states.get(entity_id)
        except Exception:  # noqa: BLE001
            return None
        return _as_number(getattr(state, "state", None)) if state is not None else None

    async def _adopt(self) -> None:
        """Take over what the last lifetime left on the numbers. Once."""
        if self._adopted:
            return
        self._adopted = True
        try:
            record = await self._store.async_load()
        except Exception:  # noqa: BLE001 — a lost record is not a lost cycle
            _LOGGER.debug("charge target: record unreadable", exc_info=True)
            return
        entities = record.get("entities") if isinstance(record, dict) else None
        if not isinstance(entities, dict):
            return
        for entity_id, raw in entities.items():
            if not isinstance(raw, dict) or not entity_id:
                continue
            t = _Target(prior=_as_number(raw.get("prior")),
                        sent=_as_number(raw.get("sent")),
                        held=raw.get("held") is True)
            if t.held and str(entity_id) not in self._configured:
                # no longer SEM's target setting: hand it back
                t.owed = True
            self._targets[str(entity_id)] = t
            if t.held:
                _LOGGER.info(
                    "Battery: %s still carries SEM's charge target %s — "
                    "%s goes back on the stop (#1180)",
                    entity_id, t.sent, t.prior)

    async def _save(self) -> None:
        if self._store is None:
            return
        # a number SEM let go of is kept only while it is still the target:
        # its last value is what tells SEM's own echo from the user's value
        data = {"entities": {
            e: {"prior": t.prior, "sent": t.sent, "held": t.held}
            for e, t in self._targets.items()
            if t.held or e in self._configured}}
        try:
            await self._store.async_save(data)
        except Exception:  # noqa: BLE001 — persistence never costs a cycle
            _LOGGER.debug("charge target: could not save the record",
                          exc_info=True)
