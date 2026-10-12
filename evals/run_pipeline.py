"""The briefs of evals/briefs.jsonl through the whole pipeline (app.pipeline.run), one at a time.
One JSON line per run to evals/runs/<name>.jsonl:
``{"id", "niche", "brief", "state", "events", "prototype_html", "error"}`` — ``state`` is the
run's state as GET /api/brief/runs/{id} returns it (docs/SPEC.md), ``events`` its SSE events.
Summary: evals/summarize.py; a form for a human analyst: evals/calibration.py.

Needs LLM_API_KEY and LLM_BASE_URL; models and limits come from the same env as the service.
Money, twice over: a run starts only if the rubles spent so far plus its worst case without
rework (app.pricing.run_worst_rub) fit --max-rub, and the runs share a fresh store whose budget
is --max-rub for the whole eval, not per day (app.store.CapStore), so every call inside reserves
against it as in the service. Rework rounds that no longer fit are skipped by the pipeline itself
(event budget_skip).

  .venv/bin/python evals/run_pipeline.py --dry-run
  .venv/bin/python evals/run_pipeline.py --max-rub 30 --only autoservice,dental --name pipeline-flash
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evals.common import RUNS, RowWriter, load_briefs, parse_ids, run_name, shown  # noqa: E402


class LiveRunner:
    """app.pipeline.run on the real gateway with a throwaway store in ``var_dir`` (tests pass
    settings and a fake gateway). Imports the service lazily."""

    def __init__(self, max_rub: float, var_dir: str | Path, settings=None, gateway=None):
        from app import pipeline
        from app.config import Settings
        from app.gateway import Gateway
        from app.store import CapStore

        self.settings = settings or Settings.from_env()
        if not self.settings.live:
            raise SystemExit("LLM_API_KEY and LLM_BASE_URL are not set")
        self._pipeline = pipeline
        self.store = CapStore(Path(var_dir) / "brief.sqlite3", daily_budget_rub=max_rub, runs_per_hour=10**6,
                              retention_days=self.settings.retention_days)
        gateway = gateway or Gateway(self.settings.base_url, self.settings.api_key)
        self._deps = pipeline.Deps(gateway, self.store, self.settings)

    def __call__(self, brief: str) -> dict:
        run_id = uuid.uuid4().hex
        state = self._pipeline.run(run_id, brief, self._deps)
        return {"state": state, "events": self.store.events_after(run_id, 0, limit=10_000),
                "prototype_html": self.store.load_prototype(run_id)}


def worst_run_rub() -> float:
    from app.config import Settings
    from app.pricing import run_worst_rub

    return run_worst_rub(Settings.from_env())


def select(briefs: list[dict], only: set[str]) -> list[dict]:
    unknown = only - {b["id"] for b in briefs}
    if unknown:
        raise SystemExit(f"unknown brief ids: {', '.join(sorted(unknown))}")
    return [b for b in briefs if not only or b["id"] in only]


def run(briefs: list[dict], runner, max_rub: float, worst_rub: float, writer=None) -> tuple[list[dict], dict]:
    """Each brief through ``runner(brief) -> {"state", "events", "prototype_html"}``; stops before
    a run whose worst case does not fit. A runner that raises gives a row with ``error``."""
    budget = {"max": max_rub, "spent": 0.0, "stopped": False}
    rows = []
    for b in briefs:
        if budget["spent"] + worst_rub > max_rub:
            budget["stopped"] = True
            break
        row = {"id": b["id"], "niche": b.get("niche"), "brief": b["brief"], "state": None, "events": [],
               "prototype_html": None, "error": None}
        try:
            row.update(runner(b["brief"]))
        except Exception as e:  # noqa: BLE001 — one broken run must not lose the others
            row["error"] = f"{type(e).__name__}: {e}"
        totals = (row["state"] or {}).get("totals") or {}
        # a run without totals (crashed) may still have spent up to its worst case
        budget["spent"] += float(totals["rub"]) if "rub" in totals else worst_rub
        rows.append(row)
        if writer is not None:
            writer.write(row)
        print(progress(row), file=sys.stderr, flush=True)
    return rows, budget


def progress(row: dict) -> str:
    """One line per run for the console: status, acceptance, rubles, seconds, error code."""
    state = row["state"] or {}
    t = state.get("totals") or {}
    error = (state.get("error") or {}).get("code") or row["error"] or ""
    return (f"{row['id']}: {state.get('status') or 'error'} {t.get('accepted', '?')}/{t.get('total', '?')} "
            f"{t.get('rub', 0):.2f} ₽ {t.get('ms', 0) / 1000:.0f} s {error}").rstrip()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--max-rub", type=float, help="spend at most this many rubles (required unless --dry-run)")
    ap.add_argument("--only", default="", help="brief ids, comma-separated")
    ap.add_argument("--name", default="", help="output file evals/runs/<name>.jsonl")
    ap.add_argument("--out-dir", default=str(RUNS))
    ap.add_argument("--dry-run", action="store_true", help="print the worst case and exit")
    args = ap.parse_args(argv)

    briefs = select(load_briefs(), parse_ids(args.only))
    worst = worst_run_rub()
    print(f"briefs: {len(briefs)}, worst case without rework {worst:.2f} ₽ per run, "
          f"{worst * len(briefs):.2f} ₽ in all; stop at {args.max_rub if args.max_rub is not None else '—'} ₽",
          file=sys.stderr)
    if args.dry_run:
        return 0
    if args.max_rub is None:
        ap.error("--max-rub is required")
    path = Path(args.out_dir) / f"{args.name or run_name('pipeline')}.jsonl"
    # the store holds briefs and prototypes only while the runs go: the rows keep what is needed
    with tempfile.TemporaryDirectory(prefix="demo-brief-evals-") as var:
        runner = LiveRunner(args.max_rub, var)  # no key: exits before an output file is opened
        with RowWriter(path) as writer:
            rows, budget = run(briefs, runner, args.max_rub, worst, writer)
    print(f"spent {budget['spent']:.2f} ₽{' (stopped at the limit)' if budget['stopped'] else ''}; "
          f"wrote {len(rows)} rows to {shown(path)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
