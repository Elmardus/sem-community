"""(#1053) The names a plan demand is shown under.

A plan demand id is SEM's key: ``ev:<charger id>``, ``load:<device id>``,
``comfort:<device id>``, ``arbitrage:battery``. The energy plan card printed
the part after the colon as the name, so a Dutch dashboard read
"ev_charger_1" and "battery" where the user's charger has a name. The load
rows have carried the device's name since #744; the charger rows and last
night's review did not.

Pure functions, no coordinator: the plan builders and the publisher pass in
what they hold. None means "no name" — the card then shows the demand's kind
in the user's language, never the id.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Iterable, List, Optional


def demand_label(config: Optional[Dict[str, Any]], controller: Any,
                 demand_id: Any) -> Optional[str]:
    """The name a person gave the thing behind ``demand_id``, or None."""
    kind, _, key = str(demand_id or "").partition(":")
    if not key:
        return None
    try:
        if kind == "ev":
            for cfg in (config or {}).get("ev_chargers") or []:
                if isinstance(cfg, dict) and str(cfg.get("id")) == key:
                    return str(cfg.get("name") or "").strip() or None
            return None
        if kind in ("load", "comfort") and controller is not None:
            get_one = getattr(controller, "get_device", None)
            if callable(get_one):
                dev = get_one(key)
            else:
                dev = next((d for d in controller.get_devices_sorted()
                            if str(getattr(d, "device_id", "")) == key), None)
            return str(getattr(dev, "name", "") or "").strip() or None
    except Exception:  # noqa: BLE001 — a name never costs a cycle
        return None
    return None


def ev_not_scheduled(config: Optional[Dict[str, Any]],
                     mode_opted_out: Iterable[str],
                     disconnected: Iterable[str],
                     car_full: Iterable[str]) -> List[Dict[str, Any]]:
    """(#638 C7) Each charger left out of the night, with a MACHINE why the
    card translates, and its name."""
    return [
        {"id": f"ev:{c}", "why": why,
         "label": demand_label(config, None, f"ev:{c}")}
        for why, ids in (("mode", mode_opted_out),
                         ("disconnected", disconnected),
                         ("car_full", car_full))
        for c in ids
    ]


def labelled_review(review: Any,
                    label_of: Callable[[Any], Optional[str]]) -> Any:
    """Last night's review with each demand row's name.

    The review is built when a night closes, from records that hold only
    the demand id. The name is looked up when it is published, so a device
    found after the boot still gets its name. The stored review is not
    changed.
    """
    if not isinstance(review, dict):
        return review
    rows = review.get("demands")
    if not isinstance(rows, list):
        return review
    out = dict(review)
    out["demands"] = [
        {**r, "label": label_of(r.get("demand_id"))}
        if isinstance(r, dict) else r
        for r in rows
    ]
    return out
