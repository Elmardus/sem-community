"""Live capture: the REAL myenergi integration (CJNE/ha-myenergi), set up by
its OWN test helper (``tests/__init__.py: setup_mock_myenergi_config_entry``)
with its OWN fixtures (``tests/fixtures/client.json``, history files).

Run by hand from a checkout of CJNE/ha-myenergi at the pinned commit:

    git clone https://github.com/CJNE/ha-myenergi && cd ha-myenergi
    git checkout <PIN in PINS.md>
    pip install --target /tmp/capdeps pymyenergi==0.2.3 --no-deps
    cp <this file> tests/test_zz_sem_capture.py
    SEM_CAPTURE_OUT=<sem>/tests/integrations_rig/captures/myenergi.json \\
      PYTHONPATH=/tmp/capdeps:. python -m pytest -p no:cacheprovider \\
      -o asyncio_mode=auto tests/test_zz_sem_capture.py
"""
from __future__ import annotations

import json
import os
import subprocess
from enum import Enum
from pathlib import Path

from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from . import setup_mock_myenergi_config_entry


def _plain(v):
    if isinstance(v, Enum):
        return v.value
    if isinstance(v, dict):
        return {str(_plain(k)): _plain(x) for k, x in v.items()}
    if isinstance(v, (list, tuple, set, frozenset)):
        return [_plain(x) for x in v]
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    return str(v)


async def test_capture(hass):
    await setup_mock_myenergi_config_entry(hass)
    ereg, dreg = er.async_get(hass), dr.async_get(hass)
    devices, entities = {}, []
    for e in sorted(ereg.entities.values(), key=lambda x: x.entity_id):
        if e.platform != "myenergi":
            continue
        key = None
        if e.device_id:
            d = dreg.async_get(e.device_id)
            ident = sorted(f"{a}:{b}" for a, b in d.identifiers)
            key = ident[0]
            devices.setdefault(key, {"name": d.name, "model": d.model,
                                     "manufacturer": d.manufacturer})
        st = hass.states.get(e.entity_id)
        entities.append({
            "entity_id": e.entity_id, "unique_id": e.unique_id,
            "translation_key": e.translation_key,
            "original_device_class": _plain(e.original_device_class),
            "unit_of_measurement": e.unit_of_measurement,
            "capabilities": _plain(e.capabilities or {}),
            "entity_category": _plain(e.entity_category),
            "disabled_by": _plain(e.disabled_by),
            "device": key,
            "state": st.state if st else None,
            "attributes": _plain(dict(st.attributes)) if st else {},
        })
    import yaml
    sy = yaml.safe_load(Path("custom_components/myenergi/services.yaml").read_text()) or {}
    fields = {n: sorted(((b or {}).get("fields") or {})) for n, b in sy.items()}
    services = {n: fields.get(n, [])
                for n in sorted(hass.services.async_services().get("myenergi", {}))}
    commit = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                            text=True).stdout.strip()
    cap = {"domain": "myenergi",
           "source": {"kind": "live-load", "repo": "CJNE/ha-myenergi",
                      "commit": commit, "client": "pymyenergi==0.2.3",
                      "data": "the integration's own tests/fixtures "
                              "(client.json, history_*.json) via its own "
                              "setup helper"},
           "devices": devices, "entities": entities, "services": services}
    assert entities
    Path(os.environ["SEM_CAPTURE_OUT"]).write_text(
        json.dumps(cap, indent=1, sort_keys=True) + "\n")
