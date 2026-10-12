"""Summary of evals/run_pipeline.py or evals/run_checks.py output: shares with 95% Wilson
intervals, p50/p95 per step, rubles per run, acceptance N of M, how many rework rounds.
The kind of each file is told by its rows; several files of one kind are pooled.

  .venv/bin/python evals/summarize.py evals/runs/pipeline-*.jsonl          (markdown)
  .venv/bin/python evals/summarize.py --json evals/runs/checks-*.jsonl     (JSON)
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evals.common import read_rows  # noqa: E402

STEP_ORDER = ("parse", "spec", "review", "prototype", "accept_review")


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval of k successes out of n; (0, 0) when n is 0."""
    if n == 0:
        return 0.0, 0.0
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, c - h), min(1.0, c + h)


def pct(values: list[float], q: float) -> float:
    """Nearest-rank percentile (q in 0..1); 0 for no values."""
    if not values:
        return 0.0
    s = sorted(values)
    return s[min(len(s) - 1, max(0, math.ceil(q * len(s)) - 1))]


def share(k: int, n: int) -> dict:
    lo, hi = wilson(k, n)
    return {"k": k, "n": n, "p": round(k / n, 4) if n else None, "ci": [round(lo, 4), round(hi, 4)]}


def kind_of(row: dict) -> str:
    if "state" in row:
        return "pipeline"
    if "kind" in row and "code_errors" in row:
        return "checks"
    raise ValueError(f"unknown row: keys {sorted(row)[:8]}")


# --- pipeline ---------------------------------------------------------------------------------

def _dist(values) -> dict:
    return {str(k): v for k, v in sorted(Counter(values).items())}


def _spread(values: list[float], digits: int = 4) -> dict:
    if not values:
        return {"n": 0}
    return {"n": len(values), "mean": round(sum(values) / len(values), digits), "p50": pct(values, 0.5),
            "p95": pct(values, 0.95), "max": max(values), "sum": round(sum(values), digits)}


def summarize_pipeline(rows: list[dict]) -> dict:
    states = [r.get("state") or {} for r in rows]
    done = [s for s in states if s.get("status") == "done"]
    errors = Counter(((s.get("error") or {}).get("code") or "runner") for s in states if s.get("status") != "done")

    steps: dict[str, dict[str, list]] = defaultdict(lambda: {"ms": [], "rub": [], "fail": []})
    for s in states:
        for st in s.get("steps") or []:
            g = steps[st.get("step", "?")]
            g["ms"].append(st.get("ms", 0))
            g["rub"].append(st.get("rub", 0.0))
            g["fail"].append(not st.get("ok", False))
    order = [k for k in STEP_ORDER if k in steps] + sorted(k for k in steps if k not in STEP_ORDER)
    per_step = {k: {"calls": len(steps[k]["ms"]), "failed": sum(steps[k]["fail"]),
                    "p50_ms": pct(steps[k]["ms"], 0.5), "p95_ms": pct(steps[k]["ms"], 0.95),
                    "rub_per_call": round(sum(steps[k]["rub"]) / len(steps[k]["rub"]), 4)} for k in order}

    totals = [s.get("totals") or {} for s in states]
    done_totals = [s.get("totals") or {} for s in done]
    accepted = sum(t.get("accepted", 0) for t in done_totals)
    total = sum(t.get("total", 0) for t in done_totals)
    criteria = [a for s in done for a in s.get("acceptance") or []]
    by_check = {by: share(sum(1 for a in criteria if a.get("by") == by and a.get("ok")),
                          sum(1 for a in criteria if a.get("by") == by)) for by in ("code", "review")}
    first = [s["specs"][0] for s in states if s.get("specs")]
    first_k = Counter(i["id"] for f in first for i in f.get("issues") or [] if i.get("level") == "error")
    first_ch = Counter(v["id"] for f in first for v in f.get("review") or [] if not v.get("ok"))
    budget_skips = sum(1 for r in rows for e in r.get("events") or [] if e.get("type") == "budget_skip")

    return {
        "kind": "pipeline", "runs": len(rows), "done": share(len(done), len(rows)), "failed_codes": dict(errors),
        "rub_per_run": _spread([t.get("rub", 0.0) for t in totals]),
        "ms_per_run": _spread([t.get("ms", 0) for t in totals], 0),
        "steps": per_step,
        "acceptance": share(accepted, total),
        "acceptance_by": by_check,
        "all_accepted": share(sum(1 for t in done_totals if t.get("accepted") == t.get("total")), len(done)),
        "spec_versions": _dist(len(s.get("specs") or []) for s in states if s.get("specs")),
        "first_red": share(sum(1 for f in first if f.get("red")), len(first)),
        "spec_has_remarks": share(sum(1 for s in done if s.get("spec_has_remarks")), len(done)),
        "prototype_attempts": _dist(s["prototype"].get("attempts", 0) for s in states if s.get("prototype")),
        "first_version_code_errors": dict(sorted(first_k.items(), key=lambda x: int(x[0][1:]))),
        "first_version_review_no": dict(sorted(first_ch.items(), key=lambda x: int(x[0][1:]))),
        "budget_skips": budget_skips,
    }


def _pc(s: dict) -> str:
    if not s["n"]:
        return "—"
    return f"{s['k']} из {s['n']} ({s['p']:.0%}, 95%: {s['ci'][0]:.0%}–{s['ci'][1]:.0%})"


def _rub(x: float) -> str:
    return f"{x:.2f}".replace(".", ",")


def render_pipeline(s: dict) -> str:
    out = [f"## Конвейер, прогонов: {s['runs']}", "",
           f"- дошли до конца: {_pc(s['done'])}" + (f"; упали: {s['failed_codes']}" if s["failed_codes"] else "")]
    r, t = s["rub_per_run"], s["ms_per_run"]
    if r["n"]:
        out.append(f"- рублей за прогон: в среднем {_rub(r['mean'])}, p50 {_rub(r['p50'])}, p95 {_rub(r['p95'])}, "
                   f"всего {_rub(r['sum'])}")
        out.append(f"- время прогона: p50 {t['p50'] / 1000:.0f} с, p95 {t['p95'] / 1000:.0f} с")
    out.append(f"- приёмка: {_pc(s['acceptance'])}; кодом {_pc(s['acceptance_by']['code'])}, "
               f"ревьюером {_pc(s['acceptance_by']['review'])}")
    out.append(f"- все критерии прошли: {_pc(s['all_accepted'])}")
    out.append(f"- версий ТЗ (1 = без доработки): {s['spec_versions']}; первая версия красная: {_pc(s['first_red'])}; "
               f"ТЗ с замечаниями: {_pc(s['spec_has_remarks'])}; пропуск по бюджету: {s['budget_skips']}")
    out.append(f"- попыток прототипа: {s['prototype_attempts']}")
    out.append(f"- первая версия, ошибки кода: {s['first_version_code_errors'] or 'нет'}; "
               f"«нет» ревьюера: {s['first_version_review_no'] or 'нет'}")
    out += ["", "| шаг | вызовов | не прошли | p50, с | p95, с | ₽ за вызов |", "|---|---|---|---|---|---|"]
    for name, st in s["steps"].items():
        out.append(f"| {name} | {st['calls']} | {st['failed']} | {st['p50_ms'] / 1000:.1f} | "
                   f"{st['p95_ms'] / 1000:.1f} | {st['rub_per_call']:.3f} |")
    return "\n".join(out)


# --- checks -----------------------------------------------------------------------------------

def summarize_checks(rows: list[dict]) -> dict:
    def flag(items, key):
        done = [r for r in items if r.get(key) is not None]
        return share(sum(1 for r in done if r[key]), len(done))

    code = [r for r in rows if r["kind"] == "broken" and r["expect"]["by"] == "code"]
    review = [r for r in rows if r["kind"] == "broken" and r["expect"]["by"] == "review"]
    valid = [r for r in rows if r["kind"] == "valid"]
    reviews = [r["review"] for r in rows if r.get("review")]
    return {
        "kind": "checks", "rows": len(rows),
        "models": sorted({rv["model"] for rv in reviews}),
        "by_code": {"caught_code": flag(code, "caught_code"), "red_review": flag(code, "red_review")},
        "by_review": {"caught_code": flag(review, "caught_code"), "caught_review": flag(review, "caught_review"),
                      "red_review": flag(review, "red_review")},
        "valid": {"false_code": flag(valid, "false_code"), "false_review": flag(valid, "false_review"),
                  "review_no": dict(Counter(i for r in valid if r.get("review") for i in r["review"]["failed"] or []))},
        "missed": [r["case"] for r in code if r.get("caught_code") is False]
                  + [r["case"] for r in review if r.get("caught_review") is False],
        "review_rub": _spread([rv["rub"] for rv in reviews]),
        "review_ms": _spread([rv["ms"] for rv in reviews], 0),
        "review_errors": sum(1 for rv in reviews if rv.get("error")),
    }


def render_checks(s: dict) -> str:
    out = [f"## Проверка ТЗ, строк: {s['rows']}" + (f", ревьюер {', '.join(s['models'])}" if s["models"] else ""), "",
           "| что | кодом | ревьюером |", "|---|---|---|",
           f"| испорченные «для кода»: поймано | {_pc(s['by_code']['caught_code'])} | "
           f"красное: {_pc(s['by_code']['red_review'])} |",
           f"| испорченные «для ревьюера»: поймано | {_pc(s['by_review']['caught_code'])} (должно быть 0) | "
           f"{_pc(s['by_review']['caught_review'])}; красное: {_pc(s['by_review']['red_review'])} |",
           f"| правильные: ложная тревога | {_pc(s['valid']['false_code'])} | {_pc(s['valid']['false_review'])} |", ""]
    if s["valid"]["review_no"]:
        out.append(f"- «нет» ревьюера на правильных: {s['valid']['review_no']}")
    if s["missed"]:
        out.append(f"- пропущены: {', '.join(s['missed'])}")
    if s["review_rub"]["n"]:
        out.append(f"- ревьюер: {_rub(s['review_rub']['mean'])} ₽ за ТЗ, p50 {s['review_ms']['p50'] / 1000:.1f} с, "
                   f"p95 {s['review_ms']['p95'] / 1000:.1f} с, ошибок {s['review_errors']}")
    return "\n".join(out).rstrip()


def summarize(rows: list[dict]) -> dict:
    kinds = {kind_of(r) for r in rows}
    if len(kinds) != 1:
        raise ValueError(f"mixed outputs: {sorted(kinds)}; summarize one kind at a time")
    return summarize_pipeline(rows) if kinds == {"pipeline"} else summarize_checks(rows)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--json", action="store_true", help="print the summary as JSON")
    args = ap.parse_args(argv)
    rows = read_rows(args.paths)
    if not rows:
        print("no rows", file=sys.stderr)
        return 1
    s = summarize(rows)
    if args.json:
        print(json.dumps(s, ensure_ascii=False, indent=1))
    else:
        print(render_pipeline(s) if s["kind"] == "pipeline" else render_checks(s))
    return 0


if __name__ == "__main__":
    sys.exit(main())
