"""#1129: text copied from English passes every key-parity test.

A new key is added in English and copied into the other 15 languages so the
parity tests stay green. Nothing then sees that the user reads English: 210
Configuration-tab labels per language were English this way.

The rule: a value that has three or more plain English words and is the same
as English is not translated. A name made of hyphenated or dotted parts
(``Open-Meteo``, ``Forecast.Solar``) or a code is not a plain word, so names
and units stay as they are. No list of exceptions.
"""
import json
import os
import re

_COMPONENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_WORD = re.compile(r"(?<![\w.{>_-])[A-Za-z][a-z]+(?![\w.}>_-])")


def _flat(node, path=""):
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _flat(value, f"{path}.{key}" if path else key)
    elif isinstance(node, str):
        yield path, node


def _copied_prose(en: dict, other: dict) -> list[str]:
    return [
        key for key, value in other.items()
        if en.get(key) == value and len(_WORD.findall(value)) >= 3
    ]


def test_rule_sees_copied_prose_and_skips_names():
    en = {"a": "Boiler control entity", "b": "Open-Meteo Solar Forecast",
          "c": "SOC", "d": "Hot water rated power (W)"}
    fr = {"a": "Boiler control entity", "b": "Open-Meteo Solar Forecast",
          "c": "SOC", "d": "Puissance nominale eau chaude (W)"}
    assert _copied_prose(en, fr) == ["a"]


def test_dashboard_has_no_copied_english_prose():
    with open(os.path.join(_COMPONENT_DIR, "dashboard", "translations.json"),
              encoding="utf-8") as f:
        data = json.load(f)
    bad = {lang: _copied_prose(data["en"], strings)
           for lang, strings in data.items() if lang != "en"}
    bad = {lang: keys for lang, keys in bad.items() if keys}
    assert not bad, f"English text in other languages: {bad}"


def test_integration_strings_have_no_copied_english_prose():
    folder = os.path.join(_COMPONENT_DIR, "translations")
    with open(os.path.join(folder, "en.json"), encoding="utf-8") as f:
        en = dict(_flat(json.load(f)))
    bad = {}
    for name in sorted(os.listdir(folder)):
        if name == "en.json" or not name.endswith(".json"):
            continue
        with open(os.path.join(folder, name), encoding="utf-8") as f:
            keys = _copied_prose(en, dict(_flat(json.load(f))))
        if keys:
            bad[name] = keys
    assert not bad, f"English text in other languages: {bad}"
