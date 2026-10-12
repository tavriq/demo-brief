"""The spec checks on the measurement set: 5 valid specs (evals/specs/valid) and 10 broken ones
(evals/broken.jsonl). One JSON line per spec to evals/runs/<name>.jsonl, a short summary to
stdout; intervals and tables: evals/summarize.py.

Code checks (app.checks.check_spec) always run and are free. The reviewer runs unless
--code-only, exactly as the pipeline's review step does it: the brief masked, the spec rendered
to text, the same prompt, model and schema, one retry when the answer is off format. It needs
LLM_API_KEY and LLM_BASE_URL and never starts a call whose worst case could push the spent
rubles past --max-rub; the rest of the specs then get code checks only.

A broken case is caught by code when its expected K is among the code errors, by the reviewer
when its expected Ч is among the items the reviewer said «no» to. A false alarm on a valid spec
is any code error, or any reviewer «no».

  .venv/bin/python evals/run_checks.py --code-only
  .venv/bin/python evals/run_checks.py --max-rub 1 --name checks-haiku
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.checks import check_spec  # noqa: E402
from evals.common import RUNS, RowWriter, load_briefs, load_broken, load_valid, run_name, shown  # noqa: E402


def load_cases() -> list[dict]:
    """Valid specs first, then the broken ones; each with the brief it was written for."""
    briefs = {b["id"]: b["brief"] for b in load_briefs()}
    valid = load_valid()
    cases = [{"case": name, "kind": "valid", "base": name, "brief": briefs[name], "spec": spec,
              "defect": None, "expect": None} for name, spec in valid.items()]
    cases += [{"case": c["id"], "kind": "broken", "base": c["base"], "brief": briefs[c["base"]], "spec": c["spec"],
               "defect": c["defect"], "expect": c["expect"]} for c in load_broken(valid=valid)]
    return cases


def code_errors(spec: dict) -> tuple[list[str], list[dict]]:
    """Sorted unique ids of the error issues, and all issues."""
    _, issues = check_spec(spec)
    return sorted({i["id"] for i in issues if i["level"] == "error"}, key=lambda x: int(x[1:])), issues


class LiveReviewer:
    """The pipeline's review step on the real gateway (tests pass settings and a fake gateway).
    Imports the service lazily, so --code-only needs none of it."""

    def __init__(self, settings=None, gateway=None):
        from app import checks, pipeline, prompts, schema
        from app.config import Settings
        from app.gateway import MODEL_PARAMS, Gateway, GatewayError
        from app.pii import mask_pii
        from app.pricing import messages_worst_rub

        settings = settings or Settings.from_env()
        if not settings.live:
            raise SystemExit("LLM_API_KEY and LLM_BASE_URL are not set: use --code-only")
        self.conf = settings.step("review")
        self.model = self.conf.model
        self._gw = gateway or Gateway(settings.base_url, settings.api_key, timeout_s=self.conf.timeout_s)
        self._params = MODEL_PARAMS.get(self.model)
        self._checks, self._pipeline, self._prompts, self._schema = checks, pipeline, prompts, schema
        self._gateway_error, self._mask, self._worst = GatewayError, mask_pii, messages_worst_rub

    def messages(self, brief: str, spec: dict, attempt: int) -> list[dict]:
        messages = self._prompts.review_messages(self._mask(brief)[0], self._checks.render_spec_md(spec))
        return messages if attempt == 1 else self._prompts.with_format_retry(messages)

    def worst(self, brief: str, spec: dict, attempt: int = 1) -> float:
        return self._worst(self.model, self.messages(brief, spec, attempt), self.conf.max_tokens)

    def review(self, brief: str, spec: dict, attempt: int) -> dict:
        """One call: ``{"items", "rub", "ms", "tokens_in", "tokens_out", "error"}``; items None
        when the answer is off format or the call failed (then the reserve counts as spent)."""
        worst = self.worst(brief, spec, attempt)
        try:
            chat = self._gw.chat(self.model, self.messages(brief, spec, attempt), self._schema.SPEC_REVIEW,
                                 self.conf.max_tokens, self._params, self.conf.timeout_s)
        except self._gateway_error as e:
            return {"items": None, "rub": worst, "ms": 0, "tokens_in": 0, "tokens_out": 0, "error": e.detail}
        data = self._pipeline.parse_answer(chat.content, self._schema.SPEC_REVIEW)
        items = None
        if data is not None:
            items = self._pipeline.verdicts(self._checks.truncate_review(data)[0].get("items"))
        return {"items": items, "rub": chat.cost_rub if chat.has_usage else worst, "ms": chat.ms,
                "tokens_in": chat.tokens_in, "tokens_out": chat.tokens_out,
                "error": None if items is not None else "model_format"}


def review_case(reviewer, case: dict, budget: dict) -> dict | None:
    """The reviewer's verdict with one format retry, or None when the next call does not fit
    ``budget`` ({"max": rubles, "spent": rubles, "stopped": bool}, updated in place)."""
    calls = []
    for attempt in (1, 2):
        if budget["spent"] + reviewer.worst(case["brief"], case["spec"], attempt) > budget["max"]:
            budget["stopped"] = True
            break
        call = reviewer.review(case["brief"], case["spec"], attempt)
        budget["spent"] += call["rub"]
        calls.append(call)
        if call["error"] != "model_format":
            break
    if not calls:
        return None
    last = calls[-1]
    items = last["items"]
    failed = [i["id"] for i in items if not i["ok"]] if items is not None else None
    return {"model": reviewer.model, "items": items, "failed": failed,
            "red": bool(failed) if failed is not None else None, "attempts": len(calls),
            "rub": round(sum(c["rub"] for c in calls), 4), "ms": sum(c["ms"] for c in calls),
            "tokens_in": sum(c["tokens_in"] for c in calls), "tokens_out": sum(c["tokens_out"] for c in calls),
            "error": last["error"]}


def judge(case: dict, errors: list[str], review: dict | None) -> dict:
    """caught_code / caught_review for a broken case, false alarms for a valid one (None = not run)."""
    reviewed = review is not None and review["items"] is not None
    if case["kind"] == "valid":
        return {"false_code": bool(errors), "false_review": review["red"] if reviewed else None}
    check = case["expect"]["check"]
    return {"caught_code": check in errors,
            "caught_review": check in review["failed"] if reviewed else None,
            "red_review": review["red"] if reviewed else None}


def run(cases: list[dict], reviewer=None, max_rub: float = 0.0, writer=None) -> tuple[list[dict], dict]:
    budget = {"max": max_rub, "spent": 0.0, "stopped": False}
    rows = []
    for case in cases:
        errors, issues = code_errors(case["spec"])
        review = None
        if reviewer is not None and not budget["stopped"]:
            review = review_case(reviewer, case, budget)
        row = {"case": case["case"], "kind": case["kind"], "base": case["base"], "defect": case["defect"],
               "expect": case["expect"], "code_errors": errors, "code_issues": issues, "review": review,
               "review_skipped": "max_rub" if reviewer is not None and review is None else None,
               **judge(case, errors, review)}
        rows.append(row)
        if writer is not None:
            writer.write(row)
    return rows, budget


def tally(rows: list[dict]) -> dict:
    """Counts by group: broken by=code, broken by=review, valid."""
    def count(items, key):
        done = [r for r in items if r.get(key) is not None]
        return sum(1 for r in done if r[key]), len(done)

    code = [r for r in rows if r["kind"] == "broken" and r["expect"]["by"] == "code"]
    review = [r for r in rows if r["kind"] == "broken" and r["expect"]["by"] == "review"]
    valid = [r for r in rows if r["kind"] == "valid"]
    return {
        "code": {"n": len(code), "caught_code": count(code, "caught_code"), "red_review": count(code, "red_review")},
        "review": {"n": len(review), "caught_code": count(review, "caught_code"),
                   "caught_review": count(review, "caught_review"), "red_review": count(review, "red_review")},
        "valid": {"n": len(valid), "false_code": count(valid, "false_code"),
                  "false_review": count(valid, "false_review")},
    }


def _frac(pair: tuple[int, int]) -> str:
    k, n = pair
    return f"{k}/{n}" if n else "—"


def summary_line(t: dict) -> str:
    return (f"by=code: caught by code {_frac(t['code']['caught_code'])}, reviewer red {_frac(t['code']['red_review'])}"
            f" | by=review: caught by code {_frac(t['review']['caught_code'])} (must be 0), "
            f"by reviewer {_frac(t['review']['caught_review'])} (exact Ч), red {_frac(t['review']['red_review'])}"
            f" | valid: false alarms code {_frac(t['valid']['false_code'])}, "
            f"reviewer {_frac(t['valid']['false_review'])}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--code-only", action="store_true", help="code checks only, free, no key needed")
    ap.add_argument("--max-rub", type=float,
                    help="stop the reviewer before spending more (required without --code-only)")
    ap.add_argument("--name", default="", help="output file evals/runs/<name>.jsonl")
    ap.add_argument("--out-dir", default=str(RUNS))
    args = ap.parse_args(argv)
    if not args.code_only and args.max_rub is None:
        ap.error("--max-rub is required unless --code-only")

    cases = load_cases()
    reviewer = None if args.code_only else LiveReviewer()
    name = args.name or run_name("checks-code" if args.code_only else "checks")
    path = Path(args.out_dir) / f"{name}.jsonl"
    with RowWriter(path) as writer:
        rows, budget = run(cases, reviewer, args.max_rub or 0.0, writer)
    print(summary_line(tally(rows)))
    if reviewer is not None:
        print(f"reviewer {reviewer.model}: spent {budget['spent']:.4f} ₽ of {args.max_rub:.2f}"
              f"{' (stopped at the limit)' if budget['stopped'] else ''}")
    print(f"wrote {len(rows)} rows to {shown(path)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
