"""(#1112) The repo does not name its maintainer.

Docs, comments, tests and the changelog say "PROD", "a decision" or "on
approval" instead. This test reads every tracked text file and fails on the
name, so it cannot creep back in.

The name is built from parts, so this file does not trip its own check.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

NAME = "gui" + "do"

#: Text that holds the same letters but is someone else's: a third-party
#: GitHub owner in the integration roster.
OTHERS = (NAME.capitalize() + "Jeuken-6512",)

#: Local or generated files: CLAUDE.md is gitignored notes; dist is the
#: built card bundle, rebuilt from the sources this test does read.
SKIP_NAMES = {"CLAUDE.md"}
SKIP_DIRS = {".git", "node_modules", "__pycache__", "dist", ".venv",
             ".pytest_cache"}


def _files() -> list[Path]:
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True,
            check=True, text=True).stdout
        return [ROOT / p for p in out.split("\0") if p]
    except (OSError, subprocess.CalledProcessError):
        # The local test layout copies the tree without .git.
        found = []
        for dirpath, dirnames, filenames in os.walk(ROOT):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            found += [Path(dirpath) / f for f in filenames]
        return found


def _skipped(path: Path) -> bool:
    rel = path.relative_to(ROOT)
    return path.name in SKIP_NAMES or any(p in SKIP_DIRS for p in rel.parts)


def test_no_file_names_the_maintainer():
    pattern = re.compile(NAME, re.IGNORECASE)
    hits = []
    for path in _files():
        if _skipped(path) or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # binary
        for n, line in enumerate(text.splitlines(), 1):
            for other in OTHERS:
                line = line.replace(other, "")
            if pattern.search(line):
                hits.append(f"{path.relative_to(ROOT)}:{n}: {line.strip()[:120]}")
    assert not hits, "the maintainer is named:\n" + "\n".join(hits)
