"""The current reviewer on specs already written: in every row of a pipeline run (evals/run_pipeline.py)
the reviewed spec version is reviewed again and the new verdicts replace the old ones; the rest of the
row is kept. The output is a pipeline run again, so evals/calibration.py agree compares it with a form
filled on the same specs — a reviewer change is calibrated without paying for new specs.

The call is the pipeline's review step (evals/run_checks.py, LiveReviewer): masked brief, rendered spec,
current prompt, model and schema, one retry when off format; an answer off format twice counts as «no» to
every item, as in the pipeline. Never starts a call whose worst case could pass --max-rub; rows left
after the limit are not written.

  .venv/bin/python evals/rereview.py evals/runs/pipeline-cal-2026-10-12.jsonl --max-rub 5 --name rereview-x
"""

from __future__ import annotations

import argparse
import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evals.calibration import reviewed_version  # noqa: E402
from evals.common import RUNS, RowWriter, read_rows, run_name, shown  # noqa: E402
from evals.run_checks import review_case  # noqa: E402


def rereview(rows: list[dict], reviewer, max_rub: float, writer=None) -> tuple[list[dict], dict]:
    """Rows with the reviewed version's verdicts replaced; ``budget`` as in run_checks.review_case."""
    from app.pipeline import checklist_verdicts

    budget, out = {"max": max_rub, "spent": 0.0, "stopped": False}, []
    for row in rows:
        row = copy.deepcopy(row)
        state = row.get("state") or {}
        entry = reviewed_version(state)
        if entry is None:
            continue
        got = review_case(reviewer, {"brief": state.get("brief") or row.get("brief") or "", "spec": entry["spec"]},
                          budget)
        if got is None:
            break
        entry["review_before"] = entry["review"]
        entry["review"] = checklist_verdicts(got["items"] or [])
        entry["rereview"] = {k: got[k] for k in ("model", "attempts", "rub", "ms", "tokens_in", "tokens_out", "error")}
        out.append(row)
        if writer is not None:
            writer.write(row)
    return out, budget


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("runs", nargs="+", help="pipeline runs, evals/runs/pipeline-*.jsonl")
    ap.add_argument("--max-rub", type=float, required=True, help="stop before spending more")
    ap.add_argument("--name", default="", help="output file evals/runs/<name>.jsonl")
    ap.add_argument("--out-dir", default=str(RUNS))
    args = ap.parse_args(argv)

    from evals.run_checks import LiveReviewer

    reviewer = LiveReviewer()
    path = Path(args.out_dir) / f"{args.name or run_name('rereview')}.jsonl"
    with RowWriter(path) as writer:
        rows, budget = rereview(read_rows(args.runs), reviewer, args.max_rub, writer)
    said_no = sum(not v["ok"] for r in rows for v in reviewed_version(r["state"])["review"])
    print(f"reviewer {reviewer.model}: {len(rows)} specs, «no» {said_no} of {8 * len(rows)}; "
          f"spent {budget['spent']:.4f} ₽ of {args.max_rub:.2f}{' (stopped at the limit)' if budget['stopped'] else ''}")
    print(f"wrote {len(rows)} rows to {shown(path)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
