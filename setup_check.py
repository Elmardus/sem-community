"""(#1213) The setup check: what SEM found, as plain numbers, and the one
question it asks when it is not sure about the grid meter.

A new user decides here whether SEM is for them. They know whether the house
is buying power or sending it out; they do not know entity IDs. So the
check shows watts, and asks about the grid only when SEM guessed it or found
none.

No direction is claimed here. A meter's sign is the brand's choice, not
HA's: some read + while buying, others + while sending, and
SEM learns which from the energy counters only after install. So setup shows
plain watts, and the question takes its sign from the user's answer.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from .coordinator.units import power_state_to_watts

# Below this a meter's sign says nothing: a house at rest reads a few watts
# either way.
QUIET_W = 50.0
ASK_GUESSED = "guessed"
ASK_MISSING = "missing"


@dataclass
class SetupCheck:
    solar_w: Optional[float] = None
    grid_entity: Optional[str] = None
    grid_w: Optional[float] = None        # as the sensor reads it; sign unknown
    grid_from: str = "missing"            # dashboard | guessed | missing
    battery_w: Optional[float] = None     # sum as read; sign unknown
    soc: Optional[float] = None
    other_grid: Optional[str] = None
    other_grid_w: Optional[float] = None
    ask: Optional[str] = None

    @property
    def grid_is_quiet(self) -> bool:
        return self.grid_w is None or abs(self.grid_w) < QUIET_W

    def answers(self) -> Tuple[str, ...]:
        """The answers that mean something for this meter right now."""
        out = ["keep"] if self.grid_is_quiet else ["buying", "sending"]
        if self.other_grid:
            out.append("other")
        out.append("unknown")
        return tuple(out)


def _watts(hass, entity_id: Optional[str]) -> Optional[float]:
    if not entity_id or hass is None:
        return None
    return power_state_to_watts(hass.states.get(entity_id))


def _sum(values) -> Optional[float]:
    vals = [v for v in values if v is not None]
    return sum(vals) if vals else None


def _percent(hass, entity_id: Optional[str]) -> Optional[float]:
    if not entity_id or hass is None:
        return None
    state = hass.states.get(entity_id)
    try:
        return float(state.state) if state is not None else None
    except (TypeError, ValueError):
        return None


def build_setup_check(hass, cfg: Any,
                      proposals: Optional[Dict[str, Any]] = None) -> SetupCheck:
    """Read what the Energy Dashboard (plus SEM's own guesses) gave."""
    check = SetupCheck()
    derived = getattr(cfg, "derived_power", None) or {}

    solar = list(getattr(cfg, "solar_power_list", None) or [])
    if not solar and getattr(cfg, "solar_power", None):
        solar = [cfg.solar_power]
    check.solar_w = _sum(_watts(hass, e) for e in solar)

    # Grid as the sensor reads it.
    if getattr(cfg, "grid_import_power", None):
        check.grid_entity = cfg.grid_import_power
        check.grid_w = _watts(hass, cfg.grid_import_power)
        check.grid_from = "guessed" if "grid" in derived else "dashboard"
    elif getattr(cfg, "grid_power_from", None) and getattr(cfg, "grid_power_to", None):
        bought = _watts(hass, cfg.grid_power_from)
        sent = _watts(hass, cfg.grid_power_to)
        if bought is not None and sent is not None:
            check.grid_w = bought - sent
        check.grid_from = "dashboard"

    # Battery as read. Only its size is shown, so the sign does not matter.
    parts = []
    for e in (getattr(cfg, "battery_power_list", None) or
              ([cfg.battery_power] if getattr(cfg, "battery_power", None) else [])):
        parts.append(_watts(hass, e))
    for p_from, p_to in (getattr(cfg, "battery_power_pairs", None) or []):
        out_w, in_w = _watts(hass, p_from), _watts(hass, p_to)
        if out_w is not None and in_w is not None:
            parts.append(out_w - in_w)
    check.battery_w = _sum(parts)
    check.soc = _percent(hass, getattr(cfg, "battery_soc", None))

    hit = (proposals or {}).get("grid_import_power_sensor") or {}
    other = hit.get("entity")
    if other and other != check.grid_entity:
        check.other_grid = other
        check.other_grid_w = _watts(hass, other)

    if check.grid_from == "missing":
        check.ask = ASK_MISSING
    elif check.grid_from == "guessed":
        check.ask = ASK_GUESSED
    return check


def answer_updates(check: SetupCheck, answer: Optional[str]) -> Dict[str, Any]:
    """Config keys that carry the user's answer. "unknown" changes nothing:
    SEM goes on learning the sign from the energy counters."""
    if answer == "other" and check.other_grid:
        return {"grid_power_sensor": check.other_grid}
    if answer not in ("buying", "sending", "keep") or not check.grid_entity:
        return {}
    out: Dict[str, Any] = {}
    if check.grid_from == "guessed":
        out["grid_power_sensor"] = check.grid_entity
    if answer != "keep" and not check.grid_is_quiet:
        # SEM's sign is − while buying. A meter that reads + while the user
        # says "buying" must be turned around; the user's answer is the
        # truth, whatever convention the brand chose.
        if (answer == "buying") == (check.grid_w > 0):
            out["grid_sign_invert"] = True
    return out


def fmt_w(w: Optional[float]) -> str:
    if w is None:
        return "?"
    return f"{abs(round(w)):,}".replace(",", " ") + " W"
