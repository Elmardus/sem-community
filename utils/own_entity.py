"""An entity SEM publishes is never an input SEM finds by itself (#1103).

SEM's sensors are SEM's own results: ``sensor.sem_grid_import_power`` is the
grid power SEM worked out from the meter. A scan that looks for "a sensor
whose name says grid import" finds that one too, and SEM would then read its
own result back as the meter it was worked out from (@LightBridge16: the
Repair offered ``sensor.sem_grid_import_power`` as the grid import meter).

Who owns an entity is in the entity registry (``platform``), not in its id —
a user can rename either. So the answer comes from the registry. An entity
the registry does not know is not SEM's: SEM registers every entity it makes.

Every scan that turns "some sensor in the house" into an input goes through
this module. ``tests/test_1103_own_sensor_is_no_input.py`` holds the scans
to that rule.
"""
from __future__ import annotations

from typing import Any

from ..consts.core import DOMAIN


def is_sem_entity(hass: Any, entity_id: Any, registry: Any = None) -> bool:
    """True when SEM itself publishes ``entity_id``."""
    if not entity_id:
        return False
    try:
        if registry is None:
            from homeassistant.helpers import entity_registry as er
            registry = er.async_get(hass)
        entry = registry.async_get(str(entity_id))
    except Exception:  # noqa: BLE001 — no registry, no answer
        return False
    return getattr(entry, "platform", None) == DOMAIN
