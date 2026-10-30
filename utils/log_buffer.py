"""In-memory ring buffer of SEM log records for the diagnose surface.

Supervisor installs route Home Assistant's log to journald — there is no
flat ``home-assistant.log`` to tail, so the diagnose payload's
``recent_logs`` was a "please run `ha core logs`" placeholder on exactly
the installs that report bugs most (the entire #461/#462 triage ran
without log visibility). A ``logging.Handler`` attached to the
integration's root logger captures every SEM record regardless of where
HA routes its output: child loggers (``…solar_energy_management.coordinator
.sensor_reader`` etc.) propagate to the ancestor logger, whose handlers
see the records.

INFO and above only — DEBUG would balloon the dump and the diagnose
surface targets "what was SEM doing around the incident", not tracing.

(#820) A write SEM sends with ``blocking=False`` is refused by the OTHER
integration, and Home Assistant logs that refusal under its own logger —
never SEM's. @ArneGollin1987's charge limit sat at 1560 W for days and the
one line that would have said why was in a log he could not find. So a tap
on the root logger keeps WARNING-and-above records from other loggers that
name an entity SEM writes to, in a small list of their own, tagged FOREIGN.
"""
from __future__ import annotations

import logging
from collections import deque

SEM_LOGGER_NAME = "custom_components.solar_energy_management"

_FORMAT = "%(asctime)s %(levelname)s (%(name)s) %(message)s"


#: (#820) foreign lines are few and precious; a flood must not grow memory
FOREIGN_CAPACITY = 50

#: domains SEM writes to — an entity of one of these named in a config
#: ``*_entity`` key is a control SEM may set
_WRITABLE_DOMAINS = ("number.", "select.", "switch.", "button.",
                     "input_number.", "input_boolean.", "input_select.",
                     "climate.", "water_heater.")


class SEMLogBuffer(logging.Handler):
    """Ring buffer handler — keeps the last ``capacity`` formatted lines."""

    def __init__(self, capacity: int = 300) -> None:
        super().__init__(level=logging.INFO)
        self._lines: deque[str] = deque(maxlen=capacity)
        self._foreign: deque[str] = deque(maxlen=FOREIGN_CAPACITY)
        self._watched: frozenset[str] = frozenset()
        self._watched_ids: frozenset[str] = frozenset()
        self.setFormatter(logging.Formatter(_FORMAT))

    def watch(self, entity_ids) -> None:
        """(#820) The entities whose name in another logger's warning makes
        that warning worth keeping."""
        self._watched = frozenset(e for e in (entity_ids or ()) if e)
        # (#820, 02.10 — mkaiser #654) A template number's set_value runs
        # as a script; Home Assistant logs its "Already running" under a
        # logger NAMED after the entity ("...script.<object_id>_set_value")
        # and the message never carries the entity id. Match the logger
        # name on the object id too.
        self._watched_ids = frozenset(
            e.split(".", 1)[1].lower() for e in self._watched if "." in e)

    def _logger_names_entity(self, logger_name: str) -> bool:
        """A dotted segment of the logger name that IS a watched object id,
        or starts with it and an underscore ("<object_id>_set_value").
        Anchored: a watched ``inv_1`` must not match ``other_inv_10_set_value``."""
        ids = self._watched_ids
        if not ids:
            return False
        for seg in logger_name.lower().split("."):
            for oid in ids:
                if seg == oid or seg.startswith(oid + "_"):
                    return True
        return False

    def offer_foreign(self, record: logging.LogRecord) -> None:
        """Keep a WARNING+ record from another logger when it names a
        watched entity. Never raises."""
        try:
            watched = self._watched
            if not watched or record.levelno < logging.WARNING:
                return
            if record.name.startswith(SEM_LOGGER_NAME) or (
                    "." + SEM_LOGGER_NAME) in record.name:
                return  # SEM's own line is already in the main buffer
            message = record.getMessage()
            if not (any(e in message for e in watched)
                    or self._logger_names_entity(record.name)):
                return
            line = (f"{self.formatter.formatTime(record)} FOREIGN "
                    f"{record.levelname} ({record.name}) {message}")
            if record.exc_info and record.exc_info[1] is not None:
                line += f" | {type(record.exc_info[1]).__name__}: {record.exc_info[1]}"
            self._foreign.append(line[:600])
        except Exception:  # noqa: BLE001 — a log handler must never raise
            pass

    def get_foreign_lines(self, count: int = FOREIGN_CAPACITY) -> list[str]:
        """Up to the last ``count`` foreign lines, oldest first."""
        return list(self._foreign)[-count:]

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D102
        try:
            self._lines.append(self.format(record))
        except Exception:  # noqa: BLE001 — a log handler must never raise
            pass

    def get_lines(self, count: int = 80) -> list[str]:
        """Return up to the last ``count`` captured lines, oldest first."""
        return list(self._lines)[-count:]


class _ForeignTap(logging.Handler):
    """(#820) Sits on the root logger and hands WARNING+ records to the
    buffer's foreign list. Cheap: a level check, then a substring check
    against a handful of entity ids."""

    def __init__(self, buffer: SEMLogBuffer) -> None:
        super().__init__(level=logging.WARNING)
        self.buffer = buffer

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D102
        self.buffer.offer_foreign(record)


def attach_foreign_tap(buffer: SEMLogBuffer, logger: logging.Logger | None = None):
    """Attach (or reuse) the foreign tap for ``buffer`` on ``logger`` (the
    root logger by default). Idempotent across reloads."""
    target = logger if logger is not None else logging.getLogger()
    for handler in target.handlers:
        if isinstance(handler, _ForeignTap):
            handler.buffer = buffer
            return handler
    tap = _ForeignTap(buffer)
    target.addHandler(tap)
    return tap


def written_entities(config) -> frozenset[str]:
    """(#820) Every entity SEM may write to, read from the config in ONE
    place: any ``*_entity`` value (at any depth — chargers and batteries are
    lists of dicts) in a domain SEM sets."""
    found: set[str] = set()

    def _walk(node, key=""):
        if isinstance(node, dict):
            for k, v in node.items():
                _walk(v, str(k))
        elif isinstance(node, (list, tuple)):
            for v in node:
                _walk(v, key)
        elif (isinstance(node, str) and key.endswith("_entity")
              and node.startswith(_WRITABLE_DOMAINS)):
            found.add(node)

    try:
        _walk(config)
    except Exception:  # noqa: BLE001
        return frozenset()
    return frozenset(found)


def ensure_attached() -> SEMLogBuffer:
    """Attach (or reuse) the buffer on the integration's root logger.

    Idempotent across reloads — the logger object is module-global and
    survives config-entry teardown, so a second setup finds and reuses
    the existing handler instead of stacking duplicates.
    """
    logger = logging.getLogger(SEM_LOGGER_NAME)
    for handler in logger.handlers:
        if isinstance(handler, SEMLogBuffer):
            attach_foreign_tap(handler)
            return handler
    buffer = SEMLogBuffer()
    logger.addHandler(buffer)
    attach_foreign_tap(buffer)
    return buffer
