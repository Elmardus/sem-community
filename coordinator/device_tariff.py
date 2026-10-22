"""(#812) A tariff per device — a heat pump on its own meter and price.

Some houses buy a device's kWh on a second contract: in Germany a heat pump
behind its own meter ("Messkonzept 8", about a third cheaper), in the US a
geothermal meter at a discount. SEM assumed one import price for the house.
Here a device may name its OWN tariff:

* a fixed rate (per kWh), or a price entity (a spot or time-of-use sensor),
  and optionally
* its own energy meter — a kWh counter of what that tariff bills.

None set (the default) = the house tariff, exactly as before: the device's
price level is the house level, nothing is booked, no sensor exists.

What reads it:

* the device's cheap/expensive gate. A fixed rate has nothing to compare
  against, so its level is ``normal`` — never cheap (#994: a level needs a
  reference; a flat tariff is not cheap). A price entity is classified by
  the SAME classifier the house price uses: a ``DynamicTariffProvider`` over
  that entity, read through ``price_signal.comparative_level``.
* the decision log: the inputs of a load's record carry its own price.
* a daily cost per device: its grid kWh × its price. Grid kWh are the own
  meter's deltas when one is named, else the device's kWh × the share of
  the house load the grid supplied this cycle (SEM's own flow split).

Pure apart from reading entity states through ``hass``.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional

from ..tariff.tariff_provider import (
    DynamicTariffProvider, PriceLevel, StaticTariffProvider,
)
from .price_signal import absence_word, comparative_level, is_cheap

#: The three per-device fields — device attributes and device-goal keys.
TARIFF_KEYS = ("tariff_rate", "tariff_entity", "tariff_meter")

#: The heat pump's config keys (flat keys = the primary unit, the same
#: names in each ``heat_pumps`` row) → the device attribute they set.
HEAT_PUMP_TARIFF_KEYS = {
    "heat_pump_tariff_rate": "tariff_rate",
    "heat_pump_tariff_entity": "tariff_entity",
    "heat_pump_tariff_meter": "tariff_meter",
}

#: A fixed rate compares with nothing: neither cheap nor expensive.
LEVEL_FIXED = PriceLevel.NORMAL.value

#: A meter that jumps more than this in one cycle was replaced or reset;
#: the step is not a cycle's consumption.
MAX_METER_STEP_KWH = 5.0

_UNREADABLE = ("unavailable", "unknown", "none", "")


def parse_rate(value: Any) -> float:
    """A stored rate → a finite, positive float; anything else 0 (none)."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return 0.0
    try:
        rate = float(value)
    except (TypeError, ValueError):
        return 0.0
    return rate if math.isfinite(rate) and rate > 0 else 0.0


@dataclass(frozen=True)
class DeviceTariff:
    """One device's own tariff. All empty = the house tariff."""
    rate: float = 0.0
    entity: str = ""
    meter: str = ""

    @property
    def configured(self) -> bool:
        return self.rate > 0 or bool(self.entity)


def tariff_of(device: Any) -> DeviceTariff:
    """The tariff a device carries (attributes set by ``apply_tariff``)."""
    return DeviceTariff(
        rate=parse_rate(getattr(device, "tariff_rate", 0.0)),
        entity=_text(getattr(device, "tariff_entity", "")),
        meter=_text(getattr(device, "tariff_meter", "")),
    )


def _text(value: Any) -> str:
    """An entity id as stored; anything not a string is none."""
    return value.strip() if isinstance(value, str) else ""


def apply_tariff(device: Any, values: Dict[str, Any]) -> None:
    """Set the tariff fields PRESENT in ``values`` on a device.

    Key-present only (the #688 pattern): a heat pump's config row seeds its
    tariff at construction, and a device goal stored later overrides only
    the fields it names.
    """
    if "tariff_rate" in values:
        device.tariff_rate = parse_rate(values.get("tariff_rate"))
    for key in ("tariff_entity", "tariff_meter"):
        if key in values:
            setattr(device, key, str(values.get(key) or "").strip())


def heat_pump_tariff(row: Dict[str, Any]) -> Dict[str, Any]:
    """A heat pump config row → the tariff fields it sets."""
    return {attr: row.get(key) for key, attr in HEAT_PUMP_TARIFF_KEYS.items()
            if key in row}


def device_price_level(device: Any, house_level: Optional[str]) -> Optional[str]:
    """The level this device's gate reads: its own when it has a tariff,
    else the house level. ``DeviceTariffs.stamp`` sets ``_tariff_level``."""
    own = getattr(device, "_tariff_level", None)
    return own if isinstance(own, str) else house_level


def grid_share(grid_to_home_w: Any, home_w: Any) -> float:
    """The share of the house load the grid supplied, 0…1. Unknown = 0."""
    try:
        grid, home = float(grid_to_home_w or 0.0), float(home_w or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if not (math.isfinite(grid) and math.isfinite(home)) or home <= 0:
        return 0.0
    return max(0.0, min(1.0, grid / home))


class PlanTariff:
    """(#812) A device's own tariff as the planner reads it, slot by slot.

    The same provider and the same rule as the live gate: ``cheap_at`` is
    ``price_signal.is_cheap`` at that hour (a fixed rate is never cheap),
    ``price_at`` the provider's price there, the fixed rate when it cannot
    say, else None (the planner then reads the house price).
    """

    def __init__(self, provider: Any, rate: float = 0.0) -> None:
        self.provider = provider
        self.rate = rate

    def price_at(self, when) -> Optional[float]:
        from .day_ledger import tariff_price_at
        price = tariff_price_at(self.provider, when)
        if price is not None:
            return price
        return self.rate or None

    def cheap_at(self, when) -> bool:
        return is_cheap(self.provider, when)


class DeviceTariffs:
    """The per-device tariffs of one install: providers, prices, levels."""

    def __init__(self, hass: Any = None) -> None:
        self.hass = hass
        #: The house import rate, the noise floor of a device curve's
        #: classifier (see ``DynamicTariffProvider.fallback_price``).
        self.house_rate: Optional[float] = None
        self._providers: Dict[str, tuple] = {}
        self._meter_last: Dict[str, float] = {}

    # ── price and level ───────────────────────────────────────────────

    def provider_for(self, device: Any):
        """The tariff provider over a device's own tariff, or None."""
        tariff = tariff_of(device)
        did = str(getattr(device, "device_id", "") or "")
        if not tariff.configured:
            self._providers.pop(did, None)
            return None
        key = (tariff.rate, tariff.entity)
        cached = self._providers.get(did)
        if cached is not None and cached[0] == key:
            provider = cached[1]
        elif tariff.entity:
            provider = DynamicTariffProvider(
                self.hass, price_entity=tariff.entity, export_rate=0.0,
                classification_mode="percentile")
            self._providers[did] = (key, provider)
        else:
            provider = StaticTariffProvider(
                peak_rate=tariff.rate, off_peak_rate=tariff.rate,
                export_rate=0.0)
            self._providers[did] = (key, provider)
        if tariff.entity:
            floor = tariff.rate or self.house_rate
            if floor:
                provider.fallback_price = float(floor)
        return provider

    def _entity_price(self, entity: str) -> Optional[float]:
        states = getattr(self.hass, "states", None)
        if states is None:
            return None
        state = states.get(entity)
        raw = str(getattr(state, "state", "") or "").strip().lower()
        if state is None or raw in _UNREADABLE:
            return None
        try:
            value = float(raw)
        except ValueError:
            return None
        return value if math.isfinite(value) else None

    def price_now(self, device: Any) -> Optional[float]:
        """The device's price per kWh now; None = no own tariff, or its
        entity cannot be read and there is no fixed rate to fall back on."""
        tariff = tariff_of(device)
        if not tariff.configured:
            return None
        if tariff.entity:
            if self._entity_price(tariff.entity) is None:
                return tariff.rate or None
            try:
                return float(self.provider_for(device).get_current_import_rate())
            except Exception:  # noqa: BLE001 — a provider that cannot answer
                return tariff.rate or None
        return tariff.rate

    def level(self, device: Any) -> Optional[str]:
        """The device's own price level, or None = it reads the house level."""
        provider = self.provider_for(device)
        if provider is None:
            return None
        # The house rule, unchanged: a level only where a comparison stands
        # behind it. A fixed rate is one flat price, so this is None for it.
        lvl = comparative_level(provider)
        if lvl is not None:
            return lvl.value
        if not tariff_of(device).entity:
            return LEVEL_FIXED
        return absence_word(provider)

    def plan_tariff(self, device: Any) -> Optional[PlanTariff]:
        """(#812) What the energy plan reads for this device; None = the
        house tariff (no own tariff set)."""
        provider = self.provider_for(device)
        if provider is None:
            return None
        return PlanTariff(provider, tariff_of(device).rate)

    def stamp(self, devices: Iterable[Any]) -> None:
        """Once per cycle: each device's gate level and log inputs.

        A device without its own tariff gets ``None`` for both — it reads
        the house level and the house inputs, exactly as before.
        """
        for device in devices:
            try:
                lvl = self.level(device)
            except Exception:  # noqa: BLE001 — a bad tariff never costs a cycle
                lvl = None
            device._tariff_level = lvl
            if lvl is None:
                device.tariff_inputs = None
                continue
            inputs: Dict[str, Any] = {"price_level": lvl}
            price = self.price_now(device)
            if price is not None:
                inputs["import_price"] = price
            device.tariff_inputs = inputs

    # ── grid kWh ──────────────────────────────────────────────────────

    def grid_kwh(self, device: Any, increment_kwh: float,
                 share: float) -> Optional[float]:
        """This cycle's grid kWh on the device's tariff.

        Own meter named: its delta since the last read (the first read only
        sets the baseline; a fall or an implausible jump is a meter reset,
        not consumption). Else the device's kWh × the grid's share.
        None = nothing to book this cycle.
        """
        tariff = tariff_of(device)
        if not tariff.configured:
            return None
        if tariff.meter:
            did = str(getattr(device, "device_id", "") or "")
            now = self._entity_price(tariff.meter)   # a number, like a price
            if now is None:
                return None
            last = self._meter_last.get(did)
            self._meter_last[did] = now
            if last is None:
                return None
            step = now - last
            if step <= 0 or step > MAX_METER_STEP_KWH:
                return None
            return step
        try:
            kwh = float(increment_kwh or 0.0) * float(share)
        except (TypeError, ValueError):
            return None
        return kwh if kwh > 0 else None
