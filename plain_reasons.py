"""(#1174) A decision's reason code → a plain sentence, server side.

The sentences live in ONE place: the ``why_reason_<code>`` keys of
``dashboard/translations.json``, which the Why? card reads in the browser.
The logbook reads the same keys here, in Home Assistant's language. The
table is read once in the executor (``async_load_plain_reasons``) and kept
in ``hass.data``; until then, or for a code without a sentence, callers
fall back to the internal reason text.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, Mapping, Optional

from .coordinator.reason_codes import ALL_CODES, catalogue_key

_LOGGER = logging.getLogger(__name__)

DATA_KEY = "solar_energy_management_plain_reasons"

_TRANSLATIONS = os.path.join(
    os.path.dirname(__file__), "dashboard", "translations.json")
_PLACEHOLDER = re.compile(r"\{(\w+)\}")
#: catalogue entries that are a device kind's word, not a reason
KIND_PREFIX = "kind:"


def load_plain_reasons(path: str = _TRANSLATIONS) -> Dict[str, Dict[str, str]]:
    """Language → {code: sentence}, only the reason keys. Blocking I/O."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as err:
        _LOGGER.debug("plain reasons: catalogue not read: %s", err)
        return {}
    out: Dict[str, Dict[str, str]] = {}
    for lang, table in data.items():
        if not isinstance(table, dict):
            continue
        out[lang] = {code: table[catalogue_key(code)] for code in ALL_CODES
                     if isinstance(table.get(catalogue_key(code)), str)}
        # the kind words, for a device the log knows only by its id
        out[lang].update({f"{KIND_PREFIX}{k[len('why_kind_'):]}": v
                          for k, v in table.items()
                          if k.startswith("why_kind_") and isinstance(v, str)})
    return out


async def async_load_plain_reasons(hass) -> None:
    """Read the catalogue once per Home Assistant run."""
    if hass.data.get(DATA_KEY):
        return
    hass.data[DATA_KEY] = await hass.async_add_executor_job(load_plain_reasons)


def fill(sentence: str, params: Optional[Mapping[str, Any]]) -> Optional[str]:
    """Put the named numbers in. ``None`` when one the sentence needs is missing."""
    values = params or {}
    missing = False

    def _sub(m: "re.Match[str]") -> str:
        nonlocal missing
        val = values.get(m.group(1))
        if val is None:
            missing = True
            return m.group(0)
        if isinstance(val, float) and val.is_integer():
            val = int(val)
        return str(val)

    out = _PLACEHOLDER.sub(_sub, sentence)
    return None if missing else out


def kind_word(catalogue: Mapping[str, Mapping[str, str]], kind: str,
              lang: str = "en") -> Optional[str]:
    """The word for a device kind (``Grid export``) in ``lang``, or ``None``."""
    return plain_sentence(catalogue, f"{KIND_PREFIX}{kind}", None, lang)


def plain_sentence(catalogue: Mapping[str, Mapping[str, str]], code: str,
                   params: Optional[Mapping[str, Any]] = None,
                   lang: str = "en") -> Optional[str]:
    """The sentence for ``code`` in ``lang`` (then its base, then English),
    or ``None`` when there is none."""
    if not code or not catalogue:
        return None
    lang = str(lang or "en")
    for name in (lang, lang.split("-")[0].split("_")[0].lower(), "en"):
        sentence = (catalogue.get(name) or {}).get(code)
        if sentence:
            return fill(sentence, params)
    return None
