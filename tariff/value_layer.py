"""What a kWh is worth — import and export value (#1077).

A price sensor says what the market pays. What a kWh is WORTH to the house
is different on each side once net metering ends (the Netherlands, 1 Jan
2027):

* bought:  ``(spot + markup + energy_tax) × (1 + VAT) + surcharge``
* sold:    ``spot_export − feed_in_fee``

The feed-in fee comes in three shapes: ``per_kwh`` (a price per kWh),
``tiered`` (the price per kWh steps up with the kWh exported this year)
and ``fixed_monthly`` (a fixed sum per month — it costs the same whatever
SEM does, so for decisions its marginal price is 0; it is only counted).

Every field defaults to 0 / off, and with every field at 0 both values are
the price SEM used before, to the last bit — the short-circuits below
return the very expression the providers computed before.

Pure stdlib on purpose, like ``install_modules``: tested without Home
Assistant.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Tuple

# ── Option keys (#1077). One place, read by the providers, the options
# flow, the card, set_option and the diagnose dump.
CONF_VALUE_PRESET = "tariff_value_preset"
CONF_IMPORT_PRICE_BASIS = "import_price_basis"
CONF_IMPORT_MARKUP = "import_markup"
CONF_IMPORT_ENERGY_TAX = "import_energy_tax"
CONF_IMPORT_VAT_PERCENT = "import_vat_percent"
CONF_FEED_IN_FEE_MODEL = "feed_in_fee_model"
CONF_FEED_IN_FEE = "feed_in_fee"
CONF_FEED_IN_FEE_TIERS = "feed_in_fee_tiers"

VALUE_OPTION_KEYS: Tuple[str, ...] = (
    CONF_VALUE_PRESET,
    CONF_IMPORT_PRICE_BASIS,
    CONF_IMPORT_MARKUP,
    CONF_IMPORT_ENERGY_TAX,
    CONF_IMPORT_VAT_PERCENT,
    CONF_FEED_IN_FEE_MODEL,
    CONF_FEED_IN_FEE,
    CONF_FEED_IN_FEE_TIERS,
)

# What the price sensor already holds — the rule that keeps tax from being
# added twice. ``all_in`` (the default) adds nothing: Tibber's price, a
# static or calendar rate you typed off your bill, any all-in template.
# ``spot_incl_vat``: the bare market price with VAT already on it (some
# Dutch price sensors), so markup and tax get VAT, the spot does not.
# ``spot``: the bare market price (Nord Pool raw, ENTSO-E), so all three
# are added and VAT goes on the whole sum.
BASIS_ALL_IN = "all_in"
BASIS_SPOT_INCL_VAT = "spot_incl_vat"
BASIS_SPOT = "spot"
PRICE_BASES: Tuple[str, ...] = (BASIS_ALL_IN, BASIS_SPOT_INCL_VAT, BASIS_SPOT)

FEE_NONE = "none"
FEE_PER_KWH = "per_kwh"
FEE_TIERED = "tiered"
FEE_FIXED_MONTHLY = "fixed_monthly"
FEE_MODELS: Tuple[str, ...] = (FEE_NONE, FEE_PER_KWH, FEE_TIERED, FEE_FIXED_MONTHLY)

PRESET_NONE = "none"
PRESET_NL_2027 = "nl_2027"
PRESET_CUSTOM = "custom"
PRESETS: Tuple[str, ...] = (PRESET_NONE, PRESET_NL_2027, PRESET_CUSTOM)

VALUE_DEFAULTS: Mapping[str, Any] = {
    CONF_VALUE_PRESET: PRESET_NONE,
    CONF_IMPORT_PRICE_BASIS: BASIS_ALL_IN,
    CONF_IMPORT_MARKUP: 0.0,
    CONF_IMPORT_ENERGY_TAX: 0.0,
    CONF_IMPORT_VAT_PERCENT: 0.0,
    CONF_FEED_IN_FEE_MODEL: FEE_NONE,
    CONF_FEED_IN_FEE: 0.0,
    CONF_FEED_IN_FEE_TIERS: "",
}

# What each preset writes. ``none`` is every other country: nothing added,
# nothing taken — the prices SEM used before. ``nl_2027`` fills the Dutch
# energy tax (2026 rate per kWh, excl. VAT — check your bill for 2027) and
# 21 % VAT, and turns the per-kWh feed-in fee on; the fee itself and the
# supplier markup differ per supplier, so they stay what the user typed.
# Tax and VAT only mean something on a spot price, so the preset also says
# the price sensor reports spot (ENTSO-E, Nord Pool). A price that is
# already all-in (Tibber's total) needs no preset.
PRESET_FIELDS: Mapping[str, Mapping[str, Any]] = {
    PRESET_NONE: {
        CONF_IMPORT_MARKUP: 0.0,
        CONF_IMPORT_ENERGY_TAX: 0.0,
        CONF_IMPORT_VAT_PERCENT: 0.0,
        CONF_FEED_IN_FEE_MODEL: FEE_NONE,
        CONF_FEED_IN_FEE: 0.0,
        CONF_FEED_IN_FEE_TIERS: "",
    },
    PRESET_NL_2027: {
        CONF_IMPORT_PRICE_BASIS: BASIS_SPOT,
        CONF_IMPORT_ENERGY_TAX: 0.0916,
        CONF_IMPORT_VAT_PERCENT: 21.0,
        CONF_FEED_IN_FEE_MODEL: FEE_PER_KWH,
    },
    PRESET_CUSTOM: {},
}


def _amount(value: Any) -> float:
    """A non-negative finite number, else 0 — a hand-edited NaN or a
    negative tax must never turn into a credit (same guard as the
    surcharge, #710)."""
    if isinstance(value, bool):
        return 0.0
    try:
        value = float(value)
    except (TypeError, ValueError):
        return 0.0
    return value if math.isfinite(value) and value >= 0.0 else 0.0


def parse_tiers(value: Any) -> Tuple[Tuple[float, float], ...]:
    """``"0:0, 1000:0.02, 3000:0.05"`` → ((0, 0), (1000, 0.02), (3000, 0.05)).

    Each pair is "from this many kWh exported this year: this fee per kWh".
    Also takes a list of pairs or of ``{"from_kwh", "fee"}`` dicts. A pair
    that does not read is skipped, not guessed at; the result is sorted.
    """
    pairs = []
    if isinstance(value, str):
        # A new pair starts only where "<kWh>:" follows a separator, so a
        # decimal comma inside a fee ("1000:0,02") stays one number.
        items = [p for p in re.split(r"[,;]\s*(?=\d+(?:[.,]\d+)?\s*:)", value)
                 if p.strip()]
        for item in items:
            m = re.match(r"\s*(\d+(?:[.,]\d+)?)\s*:\s*(-?\d+(?:[.,]\d+)?)", item)
            if m:
                pairs.append((m.group(1).replace(",", "."),
                              m.group(2).replace(",", ".")))
    elif isinstance(value, (list, tuple)):
        for item in value:
            if isinstance(item, Mapping):
                pairs.append((item.get("from_kwh"), item.get("fee")))
            elif isinstance(item, (list, tuple)) and len(item) == 2:
                pairs.append((item[0], item[1]))
    out = {}
    for start, fee in pairs:
        try:
            start_f, fee_f = float(str(start).strip()), float(str(fee).strip())
        except (TypeError, ValueError):
            continue
        if not (math.isfinite(start_f) and math.isfinite(fee_f)):
            continue
        if start_f < 0 or fee_f < 0:
            continue
        out[start_f] = fee_f
    return tuple(sorted(out.items()))


@dataclass(frozen=True)
class ValueSettings:
    """The value fields of one install. All zero = today's prices."""

    basis: str = BASIS_ALL_IN
    markup: float = 0.0
    energy_tax: float = 0.0
    vat_percent: float = 0.0
    fee_model: str = FEE_NONE
    fee: float = 0.0
    tiers: Tuple[Tuple[float, float], ...] = field(default_factory=tuple)

    @property
    def adds_to_import(self) -> bool:
        """Does the import side change anything at all?"""
        if self.basis == BASIS_ALL_IN:
            return False
        return bool(self.markup or self.energy_tax
                    or (self.basis == BASIS_SPOT and self.vat_percent))

    @property
    def configured(self) -> bool:
        """Any value field set — the capability that makes the value
        sensors exist (#1077)."""
        return bool(self.markup or self.energy_tax or self.vat_percent
                    or self.fee_model != FEE_NONE)

    def import_value(self, spot: float, surcharge: float = 0.0) -> float:
        """What a bought kWh costs, from the price the sensor reports."""
        if not self.adds_to_import:
            # The exact expression the providers used before (#1077: all
            # zero must equal today's price to the bit).
            return spot + surcharge
        vat = 1.0 + self.vat_percent / 100.0
        if self.basis == BASIS_SPOT_INCL_VAT:
            return spot + (self.markup + self.energy_tax) * vat + surcharge
        return (spot + self.markup + self.energy_tax) * vat + surcharge

    def fee_rate(self, exported_this_year_kwh: float = 0.0) -> float:
        """The feed-in fee on the NEXT exported kWh (the marginal fee).

        ``fixed_monthly`` is 0 here: the month costs the same whatever SEM
        does, so it must not tip a single decision.
        """
        if self.fee_model == FEE_PER_KWH:
            return self.fee
        if self.fee_model == FEE_TIERED:
            rate = 0.0
            for start, fee in self.tiers:
                if exported_this_year_kwh >= start:
                    rate = fee
                else:
                    break
            return rate
        return 0.0

    def export_value(self, spot_export: float,
                     exported_this_year_kwh: float = 0.0) -> float:
        """What a sold kWh earns, from the export price SEM read."""
        fee = self.fee_rate(exported_this_year_kwh)
        if not fee:
            return spot_export
        return spot_export - fee

    def fixed_monthly_fee(self) -> float:
        return self.fee if self.fee_model == FEE_FIXED_MONTHLY else 0.0

    def next_tier_kwh(self, exported_this_year_kwh: float) -> Optional[float]:
        """Where the next, higher fee starts, in kWh exported this year —
        None when there is no next tier (or no tiered fee)."""
        if self.fee_model != FEE_TIERED:
            return None
        for start, _fee in self.tiers:
            if start > exported_this_year_kwh:
                return start
        return None


NO_VALUE = ValueSettings()


def settings_from_config(config: Mapping[str, Any] | None) -> ValueSettings:
    """Build the settings from the options; anything unreadable is off."""
    cfg = config or {}
    basis = str(cfg.get(CONF_IMPORT_PRICE_BASIS) or BASIS_ALL_IN)
    if basis not in PRICE_BASES:
        basis = BASIS_ALL_IN
    model = str(cfg.get(CONF_FEED_IN_FEE_MODEL) or FEE_NONE)
    if model not in FEE_MODELS:
        model = FEE_NONE
    vat = _amount(cfg.get(CONF_IMPORT_VAT_PERCENT))
    return ValueSettings(
        basis=basis,
        markup=_amount(cfg.get(CONF_IMPORT_MARKUP)),
        energy_tax=_amount(cfg.get(CONF_IMPORT_ENERGY_TAX)),
        # A VAT above 100 % is a typo, not a tax.
        vat_percent=vat if vat <= 100.0 else 0.0,
        fee_model=model,
        fee=_amount(cfg.get(CONF_FEED_IN_FEE)),
        tiers=parse_tiers(cfg.get(CONF_FEED_IN_FEE_TIERS)),
    )


# The import amounts that only mean something on a spot price (#1077).
IMPORT_ADD_KEYS: Tuple[str, ...] = (
    CONF_IMPORT_MARKUP, CONF_IMPORT_ENERGY_TAX, CONF_IMPORT_VAT_PERCENT,
)


def basis_contradiction(config: Mapping[str, Any] | None,
                        changed: Mapping[str, Any] | None = None) -> bool:
    """Does an all-in price carry a markup, tax or VAT (#1077)?

    An all-in price adds nothing, so those amounts would be ignored
    without a word. ``config`` is the whole result (stored + new). With
    ``changed`` (a partial write, the card's set_option), only a write
    that causes the clash is refused: picking all-in, or a positive
    amount. Setting an amount to 0 is always allowed. One check for the
    options flow and set_option.
    """
    s = settings_from_config(config)
    if s.basis != BASIS_ALL_IN or not (
            s.markup or s.energy_tax or s.vat_percent):
        return False
    if changed is None:
        return True
    if CONF_IMPORT_PRICE_BASIS in changed:
        return True
    return any(_amount(changed[k]) > 0 for k in IMPORT_ADD_KEYS if k in changed)


def apply_value_preset(submitted: Mapping[str, Any],
                       stored: Mapping[str, Any]) -> dict:
    """Fill the fields a preset owns, on the way into the options.

    * The preset was CHANGED → its fields are written (overriding what the
      form sent for those fields, which was only the old values shown).
    * A field the current preset owns was changed by hand → the preset
      becomes ``custom``, so the form never claims "NL" over numbers that
      are not NL's.
    Shared by the options flow and ``set_option`` (the card), so both
    paths agree.
    """
    out = dict(submitted)
    stored_preset = stored.get(CONF_VALUE_PRESET, PRESET_NONE)
    preset = out.get(CONF_VALUE_PRESET)
    if preset in PRESET_FIELDS and preset != stored_preset:
        out.update(PRESET_FIELDS[preset])
        return out
    current = preset if preset in PRESET_FIELDS else stored_preset
    owned = PRESET_FIELDS.get(current, {})
    for key, value in owned.items():
        if key in out and out[key] != stored.get(key, VALUE_DEFAULTS[key]) \
                and out[key] != value:
            out[CONF_VALUE_PRESET] = PRESET_CUSTOM
            break
    return out
