"""#1129: the "or:" label between a proposed value and the field stayed English."""
import json
import os

_KEY = "config_proposed_or"
_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "dashboard", "translations.json")


def test_or_label_is_translated_in_every_language():
    with open(_PATH, encoding="utf-8") as f:
        data = json.load(f)
    same = [lang for lang, strings in data.items()
            if lang != "en" and strings[_KEY] == data["en"][_KEY]]
    assert not same, f"{_KEY} is English in {same}"
