"""Calibration of the reviewer against a human analyst on the checklist Ч1–Ч8.

``form`` takes generated specs from evals/run_pipeline.py output and writes a markdown form: per
spec the brief, the spec as text (app.checks.render_spec_md) and a table Ч1–Ч8 with empty
«да / нет» cells. It shows the version the reviewer judged (the last one with a review) and not
the reviewer's verdicts, so they do not prompt the analyst. ``agree`` reads the filled form and
the same run file and prints how often the analyst and the reviewer agree: all cells with a
95% Wilson interval, per item, the 2x2 table, Cohen's kappa and the cells they disagree on.
A spec is found again by the hidden marker ``<!-- calibration id=… run=… -->`` above it.

  .venv/bin/python evals/calibration.py form evals/runs/pipeline-x.jsonl --n 5 --out evals/runs/calibration-x.md
  .venv/bin/python evals/calibration.py agree evals/runs/calibration-x.md evals/runs/pipeline-x.jsonl
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.checks import render_spec_md  # noqa: E402
from evals.common import ROOT, parse_ids, read_rows  # noqa: E402
from evals.summarize import share  # noqa: E402

CHECKLIST_MD = ROOT / "docs" / "checklist.md"
_ITEM_ROW = re.compile(r"^\|\s*(Ч\d+)\s*\|\s*(.+?)\s*\|\s*$")
_MARKER = re.compile(r"<!--\s*calibration\s+id=(\S+)\s+run=(\S*)\s*-->")
_YES = {"да", "д", "+", "yes", "y", "ok", "✓", "v"}
_NO = {"нет", "н", "-", "−", "–", "—", "no", "n", "x", "✗"}


def checklist(path: str | Path = CHECKLIST_MD) -> list[tuple[str, str]]:
    """(id, text) of the reviewer's checklist, from the table in docs/checklist.md."""
    return [(m[1], m[2]) for line in Path(path).read_text(encoding="utf-8").splitlines()
            if (m := _ITEM_ROW.match(line.strip()))]


def reviewed_version(state: dict) -> dict | None:
    """The last spec version that has the reviewer's verdicts."""
    for entry in reversed((state or {}).get("specs") or []):
        if entry.get("review") and isinstance(entry.get("spec"), dict):
            return entry
    return None


def pick(rows: list[dict], n: int, only: set[str] | None = None) -> list[dict]:
    """Up to ``n`` reviewed specs, in file order: ``{"id", "run", "niche", "brief", "round", "spec", "review"}``."""
    out = []
    for row in rows:
        if len(out) >= n:
            break
        state = row.get("state") or {}
        entry = reviewed_version(state)
        if entry is None or (only and row.get("id") not in only):
            continue
        out.append({"id": row.get("id") or state.get("id"), "run": state.get("id") or "", "niche": row.get("niche"),
                    "brief": state.get("brief") or row.get("brief") or "", "round": entry.get("round"),
                    "spec": entry["spec"], "review": entry["review"]})
    return out


def _demote(md: str, levels: int = 2) -> str:
    """Headings one or more levels down, so the spec nests under the form's own headings."""
    return re.sub(r"(?m)^(#{1,6}) ", lambda m: "#" * min(6, len(m[1]) + levels) + " ", md)


def make_form(picked: list[dict], items: list[tuple[str, str]]) -> str:
    out = ["# Бланк калибровки ревьюера", "",
           "По каждому ТЗ прочитайте бриф и ТЗ и в колонке «да / нет» поставьте «да», если пункт выполнен, "
           "или «нет», если нет. Пустая клетка — «не знаю», в подсчёт не идёт. Комментарий — по желанию. "
           "Ответов ревьюера здесь нет, чтобы они не подсказывали. Скрытые строки `<!-- calibration … -->` "
           "не трогайте: по ним бланк сверяется с прогоном.", ""]
    for n, p in enumerate(picked, 1):
        out += ["---", "", f"## ТЗ {n} из {len(picked)}: {p['id']}" + (f" ({p['niche']})" if p.get("niche") else ""),
                "", f"<!-- calibration id={p['id']} run={p['run']} -->", "",
                "**Бриф клиента:**", "", *[f"> {line}" for line in p["brief"].splitlines() or [""]], "",
                f"*Версия ТЗ: {p['round']}*" if p.get("round") else "", "",
                _demote(render_spec_md(p["spec"])).rstrip(), "",
                f"### Оценка ТЗ {n}", "", "| Пункт | Что проверить | да / нет | Комментарий |", "|---|---|---|---|"]
        out += [f"| {id_} | {text} |  |  |" for id_, text in items]
        out.append("")
    return "\n".join(out)


def _answer(cell: str) -> bool | None | str:
    """True/False for a recognised answer, None for an empty cell, the raw text otherwise."""
    v = cell.strip().strip("*_ ").lower().rstrip(".")
    if not v:
        return None
    if v in _YES:
        return True
    if v in _NO:
        return False
    return cell.strip()


def parse_form(text: str) -> tuple[dict[tuple[str, str], dict[str, bool | None]], list[str]]:
    """(id, run) -> {Ч: True/False/None} from a filled form, and complaints about cells that are
    neither «да» nor «нет» (they count as empty)."""
    out: dict[tuple[str, str], dict[str, bool | None]] = {}
    bad: list[str] = []
    key = None
    for line in text.splitlines():
        m = _MARKER.search(line)
        if m:
            key = (m[1], m[2])
            out[key] = {}
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")] if line.strip().startswith("|") else []
        if key is None or len(cells) < 3 or not re.fullmatch(r"Ч\d+", cells[0]):
            continue
        answer = _answer(cells[2])
        if isinstance(answer, str):
            bad.append(f"{key[0]} {cells[0]}: «{answer}» — не «да» и не «нет»")
            answer = None
        out[key][cells[0]] = answer
    return out, bad


def reviewer_verdicts(rows: list[dict]) -> dict[tuple[str, str], dict[str, dict]]:
    """(id, run) -> {Ч: {"ok", "note"}} of the version each form shows."""
    out = {}
    for row in rows:
        state = row.get("state") or {}
        entry = reviewed_version(state)
        if entry is not None:
            key = (row.get("id") or state.get("id"), state.get("id") or "")
            out[key] = {v["id"]: {"ok": bool(v.get("ok")), "note": v.get("note") or ""} for v in entry["review"]}
    return out


def _find(key: tuple[str, str], verdicts: dict) -> dict | None:
    if key in verdicts:
        return verdicts[key]
    same_id = [v for (id_, _), v in verdicts.items() if id_ == key[0]]
    return same_id[0] if len(same_id) == 1 and not key[1] else None


def agreement(human: dict, verdicts: dict, items: list[str]) -> dict:
    """Agreement of the analyst's cells with the reviewer's verdicts on the same cells."""
    pairs, missing, empty = [], [], 0
    for key, answers in human.items():
        review = _find(key, verdicts)
        if review is None:
            missing.append(key[0])
            continue
        for item in items:
            h = answers.get(item)
            if h is None:
                empty += 1
                continue
            if item not in review:
                continue
            pairs.append((key[0], item, h, review[item]["ok"], review[item]["note"]))
    n = len(pairs)
    match = sum(1 for _, _, h, r, _ in pairs if h == r)
    hy = sum(1 for p in pairs if p[2])
    ry = sum(1 for p in pairs if p[3])
    pe = (hy * ry + (n - hy) * (n - ry)) / (n * n) if n else 0.0
    kappa = round((match / n - pe) / (1 - pe), 4) if n and pe < 1 else None
    return {
        "cells": share(match, n),
        "per_item": {item: share(sum(1 for p in pairs if p[1] == item and p[2] == p[3]),
                                 sum(1 for p in pairs if p[1] == item)) for item in items},
        "table": {"both_yes": sum(1 for p in pairs if p[2] and p[3]),
                  "both_no": sum(1 for p in pairs if not p[2] and not p[3]),
                  "reviewer_missed": sum(1 for p in pairs if not p[2] and p[3]),
                  "reviewer_false_alarm": sum(1 for p in pairs if p[2] and not p[3])},
        "kappa": kappa,
        "disagreements": [{"id": s, "item": i, "human": h, "reviewer": r, "note": note}
                          for s, i, h, r, note in pairs if h != r],
        "empty_cells": empty, "missing_runs": missing, "specs": len(human) - len(missing),
    }


def _pc(s: dict) -> str:
    return f"{s['k']} из {s['n']} ({s['p']:.0%}, 95%: {s['ci'][0]:.0%}–{s['ci'][1]:.0%})" if s["n"] else "—"


def render_agreement(a: dict, bad: list[str]) -> str:
    t = a["table"]
    out = [f"## Ревьюер и аналитик: {a['specs']} ТЗ", "",
           f"- совпало клеток: {_pc(a['cells'])}; каппа Коэна: {a['kappa'] if a['kappa'] is not None else '—'}",
           f"- оба «да» {t['both_yes']}, оба «нет» {t['both_no']}, ревьюер пропустил (аналитик «нет», ревьюер «да») "
           f"{t['reviewer_missed']}, ложная тревога ревьюера (аналитик «да», ревьюер «нет») "
           f"{t['reviewer_false_alarm']}",
           f"- пустых клеток: {a['empty_cells']}" + (f"; нет в прогоне: {', '.join(a['missing_runs'])}"
                                                      if a["missing_runs"] else ""),
           "", "| пункт | совпало |", "|---|---|"]
    out += [f"| {item} | {_pc(s)} |" for item, s in a["per_item"].items()]
    if a["disagreements"]:
        out += ["", "| ТЗ | пункт | аналитик | ревьюер | заметка ревьюера |", "|---|---|---|---|---|"]
        out += [f"| {d['id']} | {d['item']} | {'да' if d['human'] else 'нет'} | {'да' if d['reviewer'] else 'нет'} | "
                f"{d['note'].replace('|', '/')} |" for d in a["disagreements"]]
    if bad:
        out += ["", "Непонятные ответы (не посчитаны):", *[f"- {b}" for b in bad]]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("form", help="write the form from run_pipeline output")
    f.add_argument("runs", nargs="+")
    f.add_argument("--n", type=int, default=5)
    f.add_argument("--only", default="", help="brief ids, comma-separated")
    f.add_argument("--out", default="", help="write here instead of stdout")
    g = sub.add_parser("agree", help="agreement of a filled form with the reviewer")
    g.add_argument("form")
    g.add_argument("runs", nargs="+")
    args = ap.parse_args(argv)

    items = checklist()
    rows = read_rows(args.runs)
    if args.cmd == "form":
        picked = pick(rows, args.n, parse_ids(args.only))
        if not picked:
            print("no reviewed specs in these runs", file=sys.stderr)
            return 1
        text = make_form(picked, items)
        if args.out:
            Path(args.out).write_text(text, encoding="utf-8")
            print(f"{len(picked)} specs -> {args.out}", file=sys.stderr)
        else:
            print(text)
        return 0
    human, bad = parse_form(Path(args.form).read_text(encoding="utf-8"))
    print(render_agreement(agreement(human, reviewer_verdicts(rows), [i for i, _ in items]), bad))
    return 0


if __name__ == "__main__":
    sys.exit(main())
