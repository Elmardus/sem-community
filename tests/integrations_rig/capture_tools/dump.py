"""Dump what an integration created in a running ``hass`` into a capture.

A capture is the integration's real output at a pin: every entity it
registered (with the metadata the crawler reads — translation key, unique
id, device class, unit, capabilities, its device), the state Home Assistant
holds for it, and the services the integration registered. The rig
(``tests/integrations_rig/rig.py``) replays it into a fresh test instance.
"""
from __future__ import annotations

import json
from enum import Enum
from pathlib import Path
from typing import Any, Dict


def _plain(value: Any) -> Any:
    """JSON-safe copy: enums become their value, sets become sorted lists."""
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(_plain(k)): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return sorted(_plain(v) for v in value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def capture_from_hass(hass, domain: str, *, source: Dict[str, Any],
                      services_yaml: Path | None = None) -> Dict[str, Any]:
    """Everything ``domain`` created in ``hass``, as a capture dict."""
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er

    ereg = er.async_get(hass)
    dreg = dr.async_get(hass)
    devices: Dict[str, Dict[str, Any]] = {}
    entities = []
    for entry in sorted(ereg.entities.values(), key=lambda e: e.entity_id):
        if entry.platform != domain:
            continue
        dev_key = None
        if entry.device_id:
            dev = dreg.async_get(entry.device_id)
            if dev is not None:
                ident = sorted(f"{a}:{b}" for a, b in dev.identifiers)
                dev_key = ident[0] if ident else entry.device_id
                devices.setdefault(dev_key, {
                    "name": dev.name, "model": dev.model,
                    "manufacturer": dev.manufacturer,
                })
        state = hass.states.get(entry.entity_id)
        entities.append({
            "entity_id": entry.entity_id,
            "unique_id": entry.unique_id,
            "translation_key": entry.translation_key,
            "original_device_class": _plain(entry.original_device_class),
            "unit_of_measurement": entry.unit_of_measurement,
            "capabilities": _plain(entry.capabilities or {}),
            "entity_category": _plain(entry.entity_category),
            "disabled_by": _plain(entry.disabled_by),
            "device": dev_key,
            "state": state.state if state else None,
            "attributes": _plain(dict(state.attributes)) if state else {},
        })
    services: Dict[str, list] = {}
    fields_by_service: Dict[str, list] = {}
    if services_yaml and services_yaml.exists():
        import yaml
        doc = yaml.safe_load(services_yaml.read_text()) or {}
        for name, body in doc.items():
            fields_by_service[name] = sorted((body or {}).get("fields", {}) or {})
    for name in sorted((hass.services.async_services() or {}).get(domain, {})):
        services[name] = fields_by_service.get(name, [])
    return {"domain": domain, "source": source, "devices": devices,
            "entities": entities, "services": services}


def write_capture(capture: Dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(capture, indent=1, sort_keys=True) + "\n")
