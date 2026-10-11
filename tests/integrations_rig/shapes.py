"""#1092 — the shapes integrations come in, as captures with no brand.

The crawler reads roles, not brands, so the lifecycle suite is parametrized
over how integrations DIFFER, not over who made them: current via a number
or via a service (with data such as a charger name), start/stop via a
switch, a button or a service, power in W or kW, sensors with no device
class, an enum status or a binary plug, a device-less service-driven box,
a cloud integration. Each shape is a capture dict in the same format as
``captures/<name>.json`` — ``rig.replay`` takes either.

The domains are invented (``shape_*``): nothing here may match by name.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional


def _ent(domain: str, obj: str, *, state: str, device: Optional[str] = "box",
         device_class: Optional[str] = None, unit: Optional[str] = None,
         options: Optional[List[str]] = None, key: Optional[str] = None,
         caps: Optional[Dict[str, Any]] = None,
         category: Optional[str] = None) -> Dict[str, Any]:
    platform = obj.split(".", 1)[0]
    name = obj.split(".", 1)[1]
    attrs: Dict[str, Any] = {"friendly_name": name.replace("_", " ")}
    if unit:
        attrs["unit_of_measurement"] = unit
    if device_class:
        attrs["device_class"] = device_class
    if options:
        attrs["options"] = options
    capabilities = dict(caps or {})
    if options:
        capabilities["options"] = options
    return {
        "entity_id": f"{platform}.{domain}_{name}",
        "unique_id": f"{domain}_{name}",
        "device": device,
        "state": state,
        "attributes": attrs,
        "original_device_class": device_class,
        "unit_of_measurement": unit,
        "translation_key": key or name,
        "capabilities": capabilities,
        "disabled_by": None,
        "entity_category": category,
    }


def _shape(domain: str, entities: List[Dict[str, Any]], *,
           services: Optional[Dict[str, List[str]]] = None,
           device: bool = True, note: str) -> Dict[str, Any]:
    return {
        "domain": domain,
        "devices": ({"box": {"name": f"{domain} box", "manufacturer": "Shape",
                             "model": "generic"}} if device else {}),
        "entities": entities,
        "services": services or {},
        "source": {"kind": "declared", "repo": "sem-community/tests",
                   "commit": "synthetic", "note": note, "dropped": []},
    }


def _number_current(d: str, name: str = "charger_current") -> Dict[str, Any]:
    return _ent(d, f"number.{name}", state="16", unit="A",
                device_class="current",
                caps={"min": 6, "max": 32, "step": 1, "mode": "box"})


def _power_w(d: str, *, cls: bool = True) -> Dict[str, Any]:
    return _ent(d, "sensor.charging_power", state="0", unit="W",
                device_class="power" if cls else None,
                caps={"state_class": "measurement"})


def _power_kw(d: str, *, cls: bool = True) -> Dict[str, Any]:
    return _ent(d, "sensor.charging_power", state="0.0", unit="kW",
                device_class="power" if cls else None,
                caps={"state_class": "measurement"})


def _energy(d: str, *, cls: bool = True) -> Dict[str, Any]:
    return _ent(d, "sensor.energy_total", state="1234.5", unit="kWh",
                device_class="energy" if cls else None,
                caps={"state_class": "total_increasing"})


def _plug_binary(d: str) -> Dict[str, Any]:
    return _ent(d, "binary_sensor.plugged_in", state="off", device_class="plug")


def _charging_binary(d: str) -> Dict[str, Any]:
    return _ent(d, "binary_sensor.charging", state="off",
                device_class="battery_charging")


def _status_enum(d: str) -> Dict[str, Any]:
    return _ent(d, "sensor.status", state="disconnected", device_class="enum",
                options=["disconnected", "awaiting_start", "charging",
                         "completed", "error"])


def _switch(d: str) -> Dict[str, Any]:
    return _ent(d, "switch.charging_enabled", state="on")


def _buttons(d: str) -> List[Dict[str, Any]]:
    return [_ent(d, "button.start_charging", state="unknown"),
            _ent(d, "button.stop_charging", state="unknown")]


def shapes() -> Dict[str, Dict[str, Any]]:
    """Every shape, by name. A new way an integration can look is a new
    entry here — never a brand."""
    out: Dict[str, Dict[str, Any]] = {}

    d = "shape_number_w"
    out[d] = _shape(d, [_power_w(d), _energy(d), _plug_binary(d),
                        _charging_binary(d), _number_current(d), _switch(d)],
                    note="local box: current via a number, start/stop via a "
                         "switch, power in W with device classes")

    d = "shape_number_kw"
    out[d] = _shape(d, [_power_kw(d), _energy(d), _status_enum(d),
                        _number_current(d)],
                    note="current via a number, power in kW, status as an enum, "
                         "no start/stop entity")

    d = "shape_service_name"
    out[d] = _shape(d, [_power_kw(d, cls=False), _energy(d, cls=False),
                        _ent(d, "sensor.car_status", state="idle"),
                        _switch(d)],
                    services={"set_max_current": ["max_current", "charger_name"]},
                    note="go-e-like: no device classes, kW, current via a "
                         "service that names the box (charger_name)")

    d = "shape_service_entity"
    out[d] = _shape(d, [_power_w(d), _energy(d), _plug_binary(d),
                        _charging_binary(d)],
                    services={"set_charging_current": ["device_id", "current"],
                              "start_charging": ["device_id"],
                              "stop_charging": ["device_id"]},
                    note="cloud-like: current and start/stop via services "
                         "keyed by device id, no number entity")

    d = "shape_buttons"
    out[d] = _shape(d, [_power_w(d), _energy(d), _status_enum(d),
                        _number_current(d), *_buttons(d)],
                    note="start/stop via two buttons, current via a number")

    d = "shape_deviceless"
    out[d] = _shape(d, [
        {**_power_w(d), "device": None},
        {**_energy(d), "device": None},
        {**_plug_binary(d), "device": None},
        {**_charging_binary(d), "device": None},
    ], services={"set_current": ["current"], "enable": [], "disable": []},
        device=False,
        note="KEBA-like: no device in the registry, current and start/stop "
             "via domain services")

    # ── shapes the crawler cannot complete today (#1092 findings) ──
    d = "shape_number_plain_name"
    out[d] = _shape(d, [_power_w(d), _energy(d), _plug_binary(d),
                        _charging_binary(d),
                        _number_current(d, "charging_current")],
                    note="current via a number named plainly "
                         "'charging_current' (unit A, class current) on a "
                         "charger-shaped device")

    return out


#: Shapes the crawler cannot complete today: the gap each one shows. The
#: lifecycle suite marks them xfail(strict=True) so a crawler fix flips them.
SHAPE_GAPS: Dict[str, str] = {
    "shape_number_plain_name": "#1092 crawler gap: a number named "
        "'charging_current' (A, class current) on a charger is not read as "
        "the current control",
    "shape_service_entity": "#1092 crawler gap: a service keyed by device_id "
        "is offered without the device_id in its service data",
}


def shape_names() -> List[str]:
    return sorted(shapes())
