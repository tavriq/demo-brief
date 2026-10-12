"""The repo is public: no private names, local paths, employer words or off-limits niches in any
file (docs/SPEC.md, «Страница»). The word list is private and lives outside the repo: one regex per line
in $TAVRIQ_STOPWORDS or ~/.config/tavriq/stopwords, «#» starts a comment; the local-path check is added
here. Without that file the tests are skipped."""

import os
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".git", ".venv", "__pycache__", ".pytest_cache", "var"}
CONTRACT = {"docs/SPEC.md", "docs/template.md", "docs/checklist.md"}
STOPWORDS = Path(os.environ.get("TAVRIQ_STOPWORDS") or Path.home() / ".config/tavriq/stopwords")
LOCAL_PATH = "/Us" + "ers/"  # split so that this file does not match itself


def _forbidden() -> re.Pattern:
    if not STOPWORDS.is_file():
        pytest.skip(f"no stopword list at {STOPWORDS} (set TAVRIQ_STOPWORDS)")
    lines = STOPWORDS.read_text(encoding="utf-8").splitlines()
    words = [w for w in (s.strip() for s in lines) if w and not w.startswith("#")]
    return re.compile("|".join([*words, LOCAL_PATH]), re.IGNORECASE)


def _text_files():
    for path in sorted(ROOT.rglob("*")):
        rel = path.relative_to(ROOT)
        if path.is_file() and not SKIP_DIRS.intersection(rel.parts):
            try:
                yield rel.as_posix(), path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue


def _hits(only_contract: bool) -> list[str]:
    forbidden, out = _forbidden(), []
    for rel, text in _text_files():
        if (rel in CONTRACT) != only_contract:
            continue
        for n, line in enumerate(text.splitlines(), 1):
            out += [f"{rel}:{n}: {m.group(0)}" for m in forbidden.finditer(line)]
    return out


def test_no_private_words_outside_the_contract():
    assert _hits(only_contract=False) == []


def test_no_private_words_in_the_contract():
    assert _hits(only_contract=True) == []
