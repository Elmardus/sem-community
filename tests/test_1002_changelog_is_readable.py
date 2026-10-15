"""(#1002) A release note nobody reads is not a release note.

RienduPre, 22.09.2026, a contributor who speaks Dutch and works in English
every day:

    "the amount of text and the difficult wording of issues is not what I am
    used to in github issues in general. Often I really have no clue what
    you're trying to tell or ask for. A couple of months ago this was better
    but now it's getting so worse that I don't even read the issues and
    changelog any more."

He stopped reading, so he stopped helping. That is the cost.

There has been a rule since 04.06.2026 — release notes are "one-liner
bullets", "no multi-paragraph prose" — and nothing measured it, so the
median entry grew to about 100 words and the longest reached 228. A rule
with no mechanism is a preference; this file is the mechanism.

It guards ``[Unreleased]`` only. Shipped sections are history and history is
not rewritten — but nothing ships over the cap again.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

CHANGELOG = Path(__file__).resolve().parent.parent / "CHANGELOG.md"

#: Rule, 22.09.2026. A changelog line says what changed, in common words.
#: Anything longer is the reasoning, and the reasoning belongs in the issue.
MAX_WORDS = 25

#: Words only this project knows. A reader meeting them for the first time
#: has to go and look them up, which is the moment they stop reading.
OURS = (
    "ratchet", "the rig", "class 99", "soaking", "seam", "tri-state",
    "fails closed", "one producer", "the ledger", "vacuity", "verdict",
)


def _unreleased() -> str:
    text = CHANGELOG.read_text(encoding="utf-8")
    start = text.index("# [Unreleased]")
    nxt = text.find("\n# [", start + 1)
    return text[start:nxt if nxt > 0 else len(text)]


def _entries() -> list[str]:
    """Top-level bullets of the Unreleased section, joined across wraps."""
    body = _unreleased()
    return [b.strip() for b in re.findall(r"^- .*?(?=^- |\Z)", body,
                                          flags=re.M | re.S)]


def _words(entry: str) -> int:
    e = re.sub(r"`[^`]*`", " x ", entry)          # code spans are one word
    e = re.sub(r"\(#\d+[^)]*\)", " ", e)          # issue refs are not prose
    e = re.sub(r"[*_]", "", e)
    # A decimal is ONE word to a reader. The first cut split "3.2" in two
    # and failed a line that was inside the cap — an instrument inventing a
    # fault is worse than no instrument.
    return len(re.findall(r"[\w'’-]+(?:\.\d+)?", e))


@pytest.mark.unit
class TestEveryEntryCanBeRead:

    def test_the_section_is_found(self):
        """Bug class 8: an empty parse would make every check below pass."""
        assert "# [Unreleased]" in CHANGELOG.read_text(encoding="utf-8")

    def test_no_entry_is_longer_than_a_sentence(self):
        long = [(e.splitlines()[0][:70], _words(e))
                for e in _entries() if _words(e) > MAX_WORDS]
        assert not long, (
            f"{len(long)} changelog entr(ies) over {MAX_WORDS} words:"
            + "".join(f"\n  {n:>4}w  {t}…" for t, n in long)
            + "\n\nSay what changed. The reason goes in the issue, and the "
              "reader can open it if they want it."
        )

    def test_no_words_only_we_know(self):
        # Whole words: the first cut matched "the rig" inside "the right",
        # which is the guard inventing a fault — the thing it exists to stop.
        body = _unreleased().lower()
        found = sorted({w for w in OURS
                        if re.search(rf"\b{re.escape(w)}\b", body)})
        assert not found, (
            f"words only this project knows: {found}. A reader meeting one "
            "for the first time has to go and look it up, which is the "
            "moment they stop reading. Say the plain thing instead."
        )
