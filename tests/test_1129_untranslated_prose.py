"""#1129: text copied from English passes every key-parity test.

A new key is added in English and copied into the other 15 languages so the
parity tests stay green. Nothing then sees that the user reads English: 210
Configuration-tab labels per language were English this way.

The rule: a value that has three or more plain English words and is the same
as English is not translated. A name made of hyphenated or dotted parts
(``Open-Meteo``, ``Forecast.Solar``) or a code is not a plain word, so names
and units stay as they are. No list of exceptions.

#1227: two Repairs were English in 14 languages but named the settings in
the user's language (``**Grid power**`` → ``**Netzleistung**``), so they were
not the same as English. Others kept an English paragraph after a translated
one, or an older English text that English has since changed. So the rule
compares with names, codes and placeholders taken out, and also fails:
- a value that holds a sentence of its English text (four or more words);
- a sentence of three or more words with a word only English uses
  (``the``, ``your``, ``when`` …). ``are`` (Romanian), ``have`` (Danish)
  and ``been`` (Dutch) are words of other languages, so not on the list.
"""
import json
import os
import re

_COMPONENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_WORD = re.compile(r"(?<![\w.{>_-])[A-Za-z][a-z]+(?![\w.}>_-])")
# What a text names rather than says: a bold setting or card label, a code,
# a placeholder, a quoted entity name. Each language may keep or translate it.
_NAME = re.compile(
    r"\*\*[^*]*\*\*|`[^`]*`|\{[^}]*\}"
    r"|(?<!\w)'[^'\n]+'(?!\w)|\"[^\"\n]+\"|„[^“\n]+“"
)
_SENTENCE = re.compile(r"(?<=[.!?:])\s+|\n+")
# English words no other SEM language uses as a word of its own.
_ENGLISH = frozenset(
    "the and your you with this that when from should there their which "
    "does only into than they because before after until while about must "
    "each every other some what were these those".split()
)


def _flat(node, path=""):
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _flat(value, f"{path}.{key}" if path else key)
    elif isinstance(node, str):
        yield path, node


def _said(value: str) -> str:
    return _NAME.sub("\x00", value)


def _copies(english: str, value: str) -> bool:
    english, value = _said(english), _said(value)
    if english == value:
        return len(_WORD.findall(value)) >= 3
    return any(
        len(_WORD.findall(sentence)) >= 4 and sentence.strip() in value
        for sentence in _SENTENCE.split(english)
    )


def _copied_prose(en: dict, other: dict) -> list[str]:
    return [
        key for key, value in other.items()
        if isinstance(en.get(key), str) and _copies(en[key], value)
    ]


def _english_sentences(other: dict) -> list[str]:
    bad = []
    for key, value in other.items():
        for sentence in _SENTENCE.split(_said(value)):
            words = {w.lower() for w in _WORD.findall(sentence)}
            if len(words) >= 3 and words & _ENGLISH:
                bad.append(key)
                break
    return bad


def _languages(folder: str) -> dict[str, dict]:
    out = {}
    for name in sorted(os.listdir(folder)):
        if name.endswith(".json"):
            with open(os.path.join(folder, name), encoding="utf-8") as f:
                out[name[:-5]] = dict(_flat(json.load(f)))
    return out


def _dashboard() -> dict[str, dict]:
    with open(os.path.join(_COMPONENT_DIR, "dashboard", "translations.json"),
              encoding="utf-8") as f:
        return {lang: {k: v for k, v in strings.items() if isinstance(v, str)}
                for lang, strings in json.load(f).items()}


def _integration() -> dict[str, dict]:
    return _languages(os.path.join(_COMPONENT_DIR, "translations"))


def test_rule_sees_copied_prose_and_skips_names():
    en = {"a": "Boiler control entity", "b": "Open-Meteo Solar Forecast",
          "c": "SOC", "d": "Hot water rated power (W)"}
    fr = {"a": "Boiler control entity", "b": "Open-Meteo Solar Forecast",
          "c": "SOC", "d": "Puissance nominale eau chaude (W)"}
    assert _copied_prose(en, fr) == ["a"]


def test_rule_sees_english_with_translated_names():
    """#1227: the Repair named the setting in French, the rest was English."""
    en = {"r": "Then set **Grid power** in section **Sensor sources**.",
          "s": "Set **Grid power** first."}
    fr = {"r": "Then set **Puissance réseau** in section **Sources des capteurs**.",
          "s": "Réglez d'abord **Puissance réseau**."}
    assert _copied_prose(en, fr) == ["r"]


def test_rule_sees_an_english_sentence_of_the_same_key():
    """#1227: Dutch kept the English second paragraph, with no word only
    English uses in it."""
    en = {"p": "Review the entries below. Pick ONE control path: either a "
               "Number Entity (Wallbox, Ohme) OR a Service Call (KEBA).",
          "q": "Optional fields can be left blank. Pick a sensor."}
    nl = {"p": "Controleer de velden hieronder. Pick ONE control path: either "
               "a Number Entity (Wallbox, Ohme) OR a Service Call (KEBA).",
          "q": "Optionele velden mogen leeg blijven. Pick a sensor."}
    assert _copied_prose(en, nl) == ["p"]


def test_rule_sees_an_english_sentence_in_a_translated_text():
    other = {
        # #1227: a translated Repair with the English closing paragraph.
        "tail": "Le chargeur n'est PAS sous contrôle.\n\nIf you prefer, copy "
                "the context below into an email instead.",
        # An older English text English has since changed.
        "stale": "Night tariff (CHF/kWh). Set equal to import rate if you "
                 "don't have a dual tariff.",
        "translated": "Tarif de nuit (par kWh). Égal au tarif d'achat si vous "
                      "n'avez pas de double tarif.",
        "quoted_name": "Le nombre de l'onduleur (par exemple 'Maximum charging "
                       "power from the battery' chez Huawei).",
        "bold_name": "Réglez **Use the battery when the sun is down** ici.",
        "brands": "Wallbox, go-eCharger, Heidelberg, OpenWB, Ohme, V2C, KEBA.",
    }
    assert _english_sentences(other) == ["tail", "stale"]


def test_word_list_reads_english_as_english():
    """The rule is not empty: it finds most English texts as English."""
    for strings in (_integration(), _dashboard()):
        assert len(_english_sentences(strings["en"])) > 100


def test_dashboard_has_no_copied_english_prose():
    data = _dashboard()
    bad = {lang: _copied_prose(data["en"], strings)
           for lang, strings in data.items() if lang != "en"}
    bad = {lang: keys for lang, keys in bad.items() if keys}
    assert not bad, f"English text in other languages: {bad}"


def test_integration_strings_have_no_copied_english_prose():
    data = _integration()
    bad = {lang: _copied_prose(data["en"], strings)
           for lang, strings in data.items() if lang != "en"}
    bad = {lang: keys for lang, keys in bad.items() if keys}
    assert not bad, f"English text in other languages: {bad}"


def test_no_english_sentence_in_another_language():
    bad = {}
    for source, data in (("translations", _integration()),
                         ("dashboard", _dashboard())):
        for lang, strings in data.items():
            if lang != "en" and (keys := _english_sentences(strings)):
                bad[f"{source}/{lang}"] = keys
    assert not bad, f"English sentences in other languages: {bad}"
