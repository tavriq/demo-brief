"""Paths, loaders and the run writer shared by the eval scripts. Stdlib only."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EVALS = ROOT / "evals"
RUNS = EVALS / "runs"
BRIEFS = EVALS / "briefs.jsonl"
VALID_DIR = EVALS / "specs" / "valid"
BROKEN = EVALS / "broken.jsonl"
MSK = timezone(timedelta(hours=3))

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evals.ops import apply_ops  # noqa: E402


def read_jsonl(path: str | Path) -> list[dict]:
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def read_rows(paths: list[str | Path]) -> list[dict]:
    return [row for p in paths for row in read_jsonl(p)]


def load_briefs(path: str | Path = BRIEFS) -> list[dict]:
    return read_jsonl(path)


def load_valid(directory: str | Path = VALID_DIR) -> dict[str, dict]:
    """File stem -> spec, sorted by stem. The stem is the id of the brief the spec was written for."""
    return {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in sorted(Path(directory).glob("*.json"))}


def load_broken(path: str | Path = BROKEN, valid: dict[str, dict] | None = None) -> list[dict]:
    """Broken cases with ``spec`` = the base spec after the case's ops."""
    valid = load_valid() if valid is None else valid
    cases = []
    for case in read_jsonl(path):
        if case["base"] not in valid:
            raise ValueError(f"{case['id']}: no valid spec {case['base']!r}")
        cases.append({**case, "spec": apply_ops(valid[case["base"]], case["ops"])})
    return cases


def run_name(prefix: str) -> str:
    return f"{prefix}-{datetime.now(MSK):%Y-%m-%d-%H%M}"


def parse_ids(raw: str) -> set[str]:
    return {x.strip() for x in raw.split(",") if x.strip()}


class RowWriter:
    """JSON lines to ``<path>.tmp`` as they come (a crash keeps them there), renamed to ``path``
    on a clean close."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.tmp = self.path.with_name(self.path.name + ".tmp")
        self._f = self.tmp.open("w", encoding="utf-8")
        self.count = 0

    def write(self, row: dict) -> None:
        self._f.write(json.dumps(row, ensure_ascii=False) + "\n")
        self._f.flush()
        self.count += 1

    def close(self) -> None:
        self._f.close()
        self.tmp.replace(self.path)

    def __enter__(self) -> "RowWriter":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is None:
            self.close()
        else:
            self._f.close()


def shown(path: str | Path) -> str:
    """The path relative to the repo when it is inside it."""
    path = Path(path).resolve()
    return str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path)
