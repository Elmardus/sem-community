"""Which battery mode / reserve controls an install has (#523, #1071).

A battery's mode and reserve are stored twice: the scalar ``battery_mode`` /
``battery_reserve_soc`` keys (written by the ONE global
``select.sem_battery_mode`` / ``number.sem_battery_reserve_soc``) and the
idx-aligned ``battery_modes`` / ``battery_reserve_socs`` lists (written by the
per-battery ``select.sem_battery_b<N>_mode`` / ``number…_reserve_soc``).

Only one of the two has a control on screen, so only one may drive the
battery. #1071: the select/number platforms picked the store by the battery
slugs found at setup, while ``_per_battery_config`` picked it by "is a list
slot set?". A one-battery install that kept a ``battery_modes`` list (.175:
``['auto', 'auto']``) showed the global select, and every write to it was
shadowed by the list — the select read ``force_charge``, SEM ran ``auto``.

The fix is one decision with one owner: ``discover_battery_control_slugs``
runs once in ``async_setup_entry``, before the first refresh and before any
platform builds an entity, and its answer is held on the coordinator. The
platforms build their controls from it and the runtime reads the store of the
controls that were built.

An UNSET reserve is the value its number shows, ``DEFAULT_BATTERY_RESERVE_SOC``
— the runtime used 0 % while the number showed 20 % (``reserve_soc_of``).
"""
from __future__ import annotations

import logging
import re
from typing import Any

from homeassistant.helpers import entity_registry as er

from ..ha_energy_reader import battery_units

_LOGGER = logging.getLogger(__name__)

_BATTERY_SLUG_RE = re.compile(r"^sensor\.sem_battery_(b\d+)_power$")
# (#1171) the same sensor by its unique id (a renamed entity keeps it), and
# the per-battery Mode select (``<entry_id>_battery_<bid>_mode``)
_BATTERY_SENSOR_UID_RE = re.compile(r"^sem_battery_(b\d+)_power$")
_BATTERY_SELECT_UID_RE = re.compile(r"_battery_(b\d+)_mode$")


def discover_battery_control_slugs(coordinator: Any) -> tuple[str, ...]:
    """Short per-battery slugs (``b1`` …) for multi-battery installs.

    Primary source is the Energy Dashboard's batteries, combined sensors and
    two-sensor pairs (``battery_units``, #1171; the same list as the
    per-battery sensors in ``sensor.py``). But that depends
    on ``_energy_dashboard_config`` being populated at capture time, which
    a cold start (#274) can miss — so we FALL BACK to the
    persisted per-battery power sensors in the entity registry
    (``sensor.sem_battery_b<N>_power``). The registry survives restarts,
    so once a multi-battery install has its sensors the control entities
    are created deterministically on every boot. Empty on single-battery
    installs (no per-battery control entities created).

    (#1171) The per-battery Mode selects count too, and a sensor is known
    by its unique id as well. A cold start builds the per-battery sensors
    from the Energy Dashboard, so one that misses it sweeps them; the
    selects stay. Without them a second such start fell back to the one
    shared select, and the next normal start's handover
    (``adopt_shared_battery_settings``) wrote the shared mode over every
    battery's own.
    """
    sr = getattr(coordinator, "_sensor_reader", None)
    ed = getattr(sr, "_energy_dashboard_config", None) if sr is not None else None
    batt_list = battery_units(ed)
    if len(batt_list) > 1:
        return tuple(f"b{i + 1}" for i in range(len(batt_list)))

    # Fallback: discover from the persisted per-battery power sensors —
    # this entry's own (#1071 review: a second SEM entry's batteries are not
    # this install's).
    entry_id = getattr(getattr(coordinator, "config_entry", None), "entry_id", None)
    from ..const import DOMAIN

    def _slug(ent) -> str | None:
        m = _BATTERY_SLUG_RE.match(ent.entity_id)
        if m:
            return m.group(1)
        if ent.platform != DOMAIN or not isinstance(ent.unique_id, str):
            return None
        if ent.domain == "sensor":
            m = _BATTERY_SENSOR_UID_RE.match(ent.unique_id)
        elif (ent.domain == "select" and isinstance(entry_id, str)
              and ent.unique_id.startswith(f"{entry_id}_")):
            m = _BATTERY_SELECT_UID_RE.search(ent.unique_id)
        return m.group(1) if m else None

    try:
        reg = er.async_get(coordinator.hass)
        slugs = sorted({
            slug
            for ent in reg.entities.values()
            if (not isinstance(entry_id, str)
                or ent.config_entry_id == entry_id)
            and (slug := _slug(ent))
        })
        _LOGGER.debug("battery slugs (registry fallback): %s", slugs)
        return tuple(slugs) if len(slugs) > 1 else ()
    except Exception as exc:  # noqa: BLE001 — discovery must never break setup
        _LOGGER.debug("battery slug discovery failed: %s", exc)
        return ()


def captured_battery_control_slugs(coordinator: Any) -> tuple[str, ...] | None:
    """The slugs ``async_setup_entry`` captured, or None before the capture.

    Asks for a tuple on purpose: a test double that answers every attribute
    (``MagicMock``) has not captured anything."""
    slugs = getattr(coordinator, "battery_control_slugs", None)
    return slugs if isinstance(slugs, tuple) else None


def has_per_battery_controls(coordinator: Any, count: int) -> bool:
    """True → the per-battery lists drive each battery; False → the scalar
    keys drive every battery.

    ``async_setup_entry`` captures before the first refresh; the live battery
    count stands in only where nothing was captured (test doubles)."""
    slugs = captured_battery_control_slugs(coordinator)
    if slugs is not None:
        return len(slugs) > 1
    return count > 1


def adopt_shared_battery_settings(hass: Any, entry: Any, coordinator: Any,
                                  slugs: tuple[str, ...]) -> bool:
    """(#1171) The first start with one Mode select per battery, after the one
    shared select: each battery starts on what the shared select drove.

    A home moves to per-battery controls when it gains a second battery, or
    when an update first counts its two-sensor batteries (#1171). Under the
    shared select the ``battery_modes`` / ``battery_reserve_socs`` slots were
    not read. Empty, they run Auto at the default reserve, so a shared Off
    (SEM hands off) would become Auto with no one asking; a list left from an
    older layout would drive again (#531). So the shared mode and reserve are
    copied into every slot, once. The move is read from the registry, which
    still holds the shared select and no per-battery one: the platforms have
    not built or swept anything yet. True when the slots were written.
    """
    if len(slugs) < 2:
        return False
    from ..const import DOMAIN
    entry_id = entry.entry_id
    try:
        reg = er.async_get(hass)
        shared = reg.async_get_entity_id("select", DOMAIN, f"{entry_id}_battery_mode")
        own = any(
            reg.async_get_entity_id("select", DOMAIN, f"{entry_id}_battery_{bid}_mode")
            for bid in slugs)
    except Exception as exc:  # noqa: BLE001 — never break setup
        _LOGGER.debug("battery control handover check failed: %s", exc)
        return False
    if not shared or own:
        return False
    merged = {**(entry.data or {}), **(entry.options or {})}
    mode = merged.get("battery_mode") or None
    reserve = merged.get("battery_reserve_soc")
    new_options = {
        **(entry.options or {}),
        "battery_modes": [mode] * len(slugs),
        "battery_reserve_socs": [reserve] * len(slugs),
    }
    if isinstance(getattr(coordinator, "config", None), dict):
        # only these two: the coordinator's copy of the rest is its own
        coordinator.config.update({k: new_options[k] for k in (
            "battery_modes", "battery_reserve_socs")})
    hass.config_entries.async_update_entry(entry, options=new_options)
    _LOGGER.warning(
        "Battery controls: one Mode select and one reserve per battery now "
        "(%s). Each battery starts on the shared mode %s and reserve %s. %s "
        "and the shared reserve number go away; automations that set them "
        "need the per-battery ones.",
        ", ".join(slugs), mode or "auto",
        "default" if reserve is None else reserve, shared)
    return True
