"""evals/: the measurement set (briefs, valid and broken specs), ops, run_checks, run_pipeline,
summarize and calibration. No network: the reviewer and the pipeline run on the fake gateway."""

import copy
import json
import re
from dataclasses import replace

import pytest

from app.checks import check_spec
from app.config import Settings
from app.pii import mask_pii
from app.pricing import prompt_chars
from app.schema import SPEC
from evals import calibration, run_checks, run_pipeline, summarize
from evals.common import RowWriter, load_briefs, load_broken, load_valid, read_jsonl
from evals.ops import OpError, apply_ops, parse_pointer
from tests.fakes import FakeGateway, Reply, happy, review_reply, PINNED_ENV

BRIEFS = load_briefs()
VALID = load_valid()
BROKEN = load_broken(valid=VALID)
CODE_CASES = [c for c in BROKEN if c["expect"]["by"] == "code"]
REVIEW_CASES = [c for c in BROKEN if c["expect"]["by"] == "review"]
CHECKLIST_IDS = [i for i, _ in calibration.checklist()]


def errors_of(spec):
    _, issues = check_spec(spec)
    return [i for i in issues if i["level"] == "error"]


def conforms(value, schema, path="$"):
    """Problems of ``value`` against a strict JSON schema of app.schema (types, required, enum)."""
    kinds = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
    is_kind = {"string": lambda v: isinstance(v, str), "boolean": lambda v: isinstance(v, bool),
               "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
               "null": lambda v: v is None, "array": lambda v: isinstance(v, list),
               "object": lambda v: isinstance(v, dict)}
    if not any(is_kind[k](value) for k in kinds):
        return [f"{path}: {type(value).__name__}, want {kinds}"]
    if "enum" in schema and value not in schema["enum"]:
        return [f"{path}: {value!r} not in {schema['enum']}"]
    out = []
    if isinstance(value, dict):
        if set(value) != set(schema["required"]):
            out.append(f"{path}: keys {sorted(set(value) ^ set(schema['required']))} differ")
        for key, sub in schema["properties"].items():
            if key in value:
                out += conforms(value[key], sub, f"{path}.{key}")
    elif isinstance(value, list):
        for i, item in enumerate(value):
            out += conforms(item, schema["items"], f"{path}[{i}]")
    return out


# --- briefs -----------------------------------------------------------------------------------

def test_briefs_set():
    ids = [b["id"] for b in BRIEFS]
    assert len(BRIEFS) == 20 and len(set(ids)) == 20
    assert {"autoservice", "dental", "building"} <= set(ids)
    assert len({b["niche"] for b in BRIEFS}) == 20


@pytest.mark.parametrize("b", BRIEFS, ids=lambda b: b["id"])
def test_brief_shape(b):
    assert set(b) == {"id", "niche", "brief"}
    assert re.fullmatch(r"[a-z]+", b["id"])
    assert 0 < len(b["brief"]) <= 500
    assert 1 <= len(re.findall(r"[.!?…]+(?=\s|$)", b["brief"])) <= 3
    assert mask_pii(b["brief"])[0] == b["brief"]  # nothing the service would hide


def test_some_briefs_are_vague():
    assert sum(1 for b in BRIEFS if len(b["brief"]) < 80) >= 3


# --- valid specs ------------------------------------------------------------------------------

def test_five_valid_specs_for_briefs():
    assert len(VALID) == 5
    assert set(VALID) <= {b["id"] for b in BRIEFS}


@pytest.mark.parametrize("name", sorted(VALID))
def test_valid_spec_has_no_code_issues(name):
    spec = VALID[name]
    out, issues = check_spec(copy.deepcopy(spec))
    assert issues == []
    assert out == spec  # nothing truncated


@pytest.mark.parametrize("name", sorted(VALID))
def test_valid_spec_shape(name):
    spec = VALID[name]
    assert conforms(spec, SPEC["schema"]) == []
    assert 3 <= len(spec["scenarios"]) <= 5
    assert 2 <= len(spec["roles"]) <= 3
    assert 5 <= len(spec["acceptance"]) <= 9
    assert {a["check"] for a in spec["acceptance"]} == {"screen", "field", "review"}


# --- broken specs -----------------------------------------------------------------------------

def test_broken_set():
    ids = [c["id"] for c in BROKEN]
    assert len(BROKEN) == 10 and len(set(ids)) == 10
    assert len(CODE_CASES) == 5 and len(REVIEW_CASES) == 5
    assert len({c["expect"]["check"] for c in CODE_CASES}) == 5
    assert len({c["expect"]["check"] for c in REVIEW_CASES}) == 5
    assert all(re.fullmatch(r"K([1-9]|1[01])", c["expect"]["check"]) for c in CODE_CASES)
    assert all(c["expect"]["check"] in CHECKLIST_IDS for c in REVIEW_CASES)


@pytest.mark.parametrize("case", BROKEN, ids=lambda c: c["id"])
def test_broken_case_shape(case):
    assert set(case) == {"id", "base", "defect", "ops", "expect", "spec"}
    assert re.search(r"[а-яё]", case["defect"]) and case["ops"]
    assert all(op["path"].startswith("/") for op in case["ops"])
    assert case["spec"] != VALID[case["base"]]
    assert conforms(case["spec"], SPEC["schema"]) == []  # the reviewer gets a well-formed spec


@pytest.mark.parametrize("case", CODE_CASES, ids=lambda c: c["id"])
def test_code_case_caught_by_exactly_its_check(case):
    assert {i["id"] for i in errors_of(case["spec"])} == {case["expect"]["check"]}


@pytest.mark.parametrize("case", REVIEW_CASES, ids=lambda c: c["id"])
def test_review_case_passes_code(case):
    assert errors_of(case["spec"]) == []


def test_load_broken_does_not_touch_valid():
    before = copy.deepcopy(VALID)
    load_broken(valid=VALID)
    assert VALID == before


# --- ops --------------------------------------------------------------------------------------

DOC = {"a": [1, 2, 3], "b": {"c": "x", "d/e": 1, "f~g": 2}}


@pytest.mark.parametrize("ops, want", [
    ([{"op": "replace", "path": "/a/1", "value": 9}], {"a": [1, 9, 3]}),
    ([{"op": "remove", "path": "/a/0"}], {"a": [2, 3]}),
    ([{"op": "add", "path": "/a/0", "value": 0}], {"a": [0, 1, 2, 3]}),
    ([{"op": "add", "path": "/a/3", "value": 4}], {"a": [1, 2, 3, 4]}),
    ([{"op": "add", "path": "/a/-", "value": 4}], {"a": [1, 2, 3, 4]}),
    ([{"op": "add", "path": "/b/new", "value": [1]}], {"b": {"c": "x", "d/e": 1, "f~g": 2, "new": [1]}}),
    ([{"op": "replace", "path": "/b/d~1e", "value": 5}], {"b": {"c": "x", "d/e": 5, "f~g": 2}}),
    ([{"op": "remove", "path": "/b/f~0g"}], {"b": {"c": "x", "d/e": 1}}),
    ([{"op": "remove", "path": "/a/2"}, {"op": "remove", "path": "/a/1"}], {"a": [1]}),
    ([{"op": "replace", "path": "", "value": {"z": 1}}], None),
])
def test_ops_apply(ops, want):
    doc = copy.deepcopy(DOC)
    out = apply_ops(doc, ops)
    assert doc == DOC  # the base is never changed
    if want is None:
        assert out == {"z": 1}
    else:
        assert out == {**copy.deepcopy(DOC), **want}


def test_ops_value_is_copied():
    value = {"k": [1]}
    out = apply_ops(DOC, [{"op": "add", "path": "/v", "value": value}])
    value["k"].append(2)
    assert out["v"] == {"k": [1]}


@pytest.mark.parametrize("op", [
    {"op": "replace", "path": "/missing", "value": 1},
    {"op": "remove", "path": "/b/missing"},
    {"op": "replace", "path": "/a/3", "value": 1},
    {"op": "remove", "path": "/a/-"},
    {"op": "add", "path": "/a/5", "value": 1},
    {"op": "replace", "path": "/a/01", "value": 1},
    {"op": "replace", "path": "/a/x", "value": 1},
    {"op": "replace", "path": "/b/c/d", "value": 1},
    {"op": "add", "path": "/x/y", "value": 1},
    {"op": "replace", "path": "/a/0"},
    {"op": "move", "path": "/a/0", "from": "/a/1"},
    {"op": "remove", "path": ""},
    {"op": "replace", "path": "a", "value": 1},
    "not an op",
])
def test_ops_errors(op):
    with pytest.raises(OpError):
        apply_ops(DOC, [op])


def test_parse_pointer():
    assert parse_pointer("") == []
    assert parse_pointer("/scenarios/2/role") == ["scenarios", "2", "role"]
    assert parse_pointer("/a~1b/c~0d/~01") == ["a/b", "c~d", "~1"]


# --- run_checks -------------------------------------------------------------------------------

def test_run_checks_code_only(tmp_path, capsys):
    assert run_checks.main(["--code-only", "--out-dir", str(tmp_path), "--name", "x"]) == 0
    rows = read_jsonl(tmp_path / "x.jsonl")
    assert len(rows) == 15 and not list(tmp_path.glob("*.tmp"))
    t = run_checks.tally(rows)
    assert t["code"]["caught_code"] == (5, 5)
    assert t["review"]["caught_code"] == (0, 5)
    assert t["valid"]["false_code"] == (0, 5)
    assert t["review"]["caught_review"] == (0, 0) and all(r["review"] is None for r in rows)
    assert all(r["review_skipped"] is None for r in rows)
    out = capsys.readouterr().out
    assert "caught by code 5/5" in out and "false alarms code 0/5" in out


def test_run_checks_needs_max_rub_for_reviewer():
    with pytest.raises(SystemExit):
        run_checks.main([])


@pytest.fixture
def live_settings(tmp_path):
    return replace(Settings.from_env(PINNED_ENV), api_key="k", base_url="http://gw.example", var_dir=tmp_path / "var")


def test_run_checks_reviewer_on_fake_gateway(live_settings):
    cases = run_checks.load_cases()
    replies = []
    for c in cases:
        if c["kind"] == "broken" and c["expect"]["by"] == "review":
            replies.append(review_reply({c["expect"]["check"]: "нарушено"}))
        elif c["case"] == "dental":
            replies += ["не JSON", review_reply()]  # one format retry, as in the pipeline
        else:
            replies.append(review_reply())
    gw = FakeGateway(replies)
    reviewer = run_checks.LiveReviewer(live_settings, gw)
    rows, budget = run_checks.run(cases, reviewer, max_rub=5.0)
    assert not gw.replies and not budget["stopped"]
    t = run_checks.tally(rows)
    assert t["review"]["caught_review"] == (5, 5) and t["review"]["red_review"] == (5, 5)
    assert t["valid"]["false_review"] == (0, 5) and t["code"]["red_review"] == (0, 5)
    dental = next(r for r in rows if r["case"] == "dental")
    assert dental["review"]["attempts"] == 2 and dental["review"]["error"] is None
    assert 0 < budget["spent"] == pytest.approx(sum(r["review"]["rub"] for r in rows))
    call = gw.calls[0]
    assert call["model"] == live_settings.review_model and call["schema"]["name"] == "spec_review"
    assert "<бриф>" in call["messages"][-1]["content"] and VALID["bakery"]["title"] in call["messages"][-1]["content"]


def test_run_checks_reviewer_stops_at_max_rub(live_settings):
    cases = run_checks.load_cases()[:3]
    gw = FakeGateway()
    reviewer = run_checks.LiveReviewer(live_settings, gw)
    one = reviewer.worst(cases[0]["brief"], cases[0]["spec"])
    chars = prompt_chars(reviewer.messages(cases[0]["brief"], cases[0]["spec"], 1))
    gw.replies.append(Reply(review_reply(), tokens_in=chars // 2 - 10, tokens_out=1500))  # close to the worst
    rows, budget = run_checks.run(cases, reviewer, max_rub=one * 1.5)
    assert budget["stopped"] and len(gw.calls) == 1
    assert rows[0]["review"] is not None and rows[0]["review_skipped"] is None
    assert [r["review_skipped"] for r in rows[1:]] == ["max_rub", "max_rub"]
    assert all(r["code_errors"] == [] for r in rows)  # code checks still ran
    assert budget["spent"] <= one * 1.5


def test_run_checks_reviewer_format_twice(live_settings):
    gw = FakeGateway(["нет", "снова нет"])
    reviewer = run_checks.LiveReviewer(live_settings, gw)
    rows, _ = run_checks.run(run_checks.load_cases()[:1], reviewer, max_rub=5.0)
    review = rows[0]["review"]
    assert review["error"] == "model_format" and review["items"] is None and review["attempts"] == 2
    assert rows[0]["false_review"] is None  # not counted either way


def test_live_reviewer_needs_key():
    with pytest.raises(SystemExit):
        run_checks.LiveReviewer(Settings.from_env(PINNED_ENV), FakeGateway())


# --- run_pipeline -----------------------------------------------------------------------------

def fake_state(rub=1.2, status="done", accepted=7, total=8, versions=1, attempts=1, steps=None, remarks=False):
    specs = [{"round": i + 1, "spec": {"title": f"v{i + 1}"}, "issues": [], "red": i + 1 < versions,
              "review": [{"id": "Ч1", "ok": True, "note": ""}]} for i in range(versions)]
    return {"id": "r", "status": status, "brief": "b", "specs": specs, "spec_has_remarks": remarks,
            "prototype": {"attempts": attempts}, "steps": steps or [],
            "acceptance": [{"id": f"A{i + 1}", "by": "code" if i % 2 else "review", "ok": i < accepted}
                           for i in range(total)],
            "totals": {"rub": rub, "ms": 40000, "accepted": accepted, "total": total},
            "error": None if status == "done" else {"code": "timeout", "message": "…"}}


def test_run_pipeline_stops_before_max_rub():
    seen = []

    def runner(brief):
        seen.append(brief)
        return {"state": fake_state(rub=1.2), "events": [], "prototype_html": "<html></html>"}

    rows, budget = run_pipeline.run(BRIEFS, runner, max_rub=2.5, worst_rub=1.0)
    assert len(rows) == 2 and budget["stopped"]
    assert budget["spent"] == pytest.approx(2.4)
    assert seen == [b["brief"] for b in BRIEFS[:2]]


def test_run_pipeline_runner_error_counts_worst():
    def runner(brief):
        raise RuntimeError("boom")

    rows, budget = run_pipeline.run(BRIEFS[:2], runner, max_rub=10, worst_rub=3.0)
    assert [r["error"] for r in rows] == ["RuntimeError: boom"] * 2
    assert budget["spent"] == 6.0


def test_run_pipeline_select():
    assert [b["id"] for b in run_pipeline.select(BRIEFS, {"dental", "building"})] == ["dental", "building"]
    assert len(run_pipeline.select(BRIEFS, set())) == 20
    with pytest.raises(SystemExit):
        run_pipeline.select(BRIEFS, {"nope"})


def test_run_pipeline_dry_run_and_max_rub(capsys):
    assert run_pipeline.main(["--dry-run", "--only", "dental"]) == 0
    assert "briefs: 1, worst case" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        run_pipeline.main([])


def test_live_scripts_without_key_exit_before_writing(tmp_path, monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    with pytest.raises(SystemExit):
        run_pipeline.main(["--max-rub", "1", "--out-dir", str(tmp_path)])
    with pytest.raises(SystemExit):
        run_checks.main(["--max-rub", "1", "--out-dir", str(tmp_path)])
    assert list(tmp_path.iterdir()) == []


def test_live_runner_on_fake_gateway(tmp_path, live_settings):
    runner = run_pipeline.LiveRunner(50.0, tmp_path / "var", live_settings, FakeGateway(happy()))
    with RowWriter(tmp_path / "p.jsonl") as w:
        rows, budget = run_pipeline.run(BRIEFS[:1], runner, max_rub=50.0, worst_rub=5.0, writer=w)
    row = read_jsonl(tmp_path / "p.jsonl")[0]
    assert row == json.loads(json.dumps(rows[0], ensure_ascii=False))
    assert row["error"] is None and row["state"]["status"] == "done"
    assert row["events"][-1]["type"] == "done" and row["prototype_html"].startswith("<!doctype html>")
    assert budget["spent"] == pytest.approx(row["state"]["totals"]["rub"])
    s = summarize.summarize([row])
    assert s["done"]["k"] == 1 and s["acceptance"]["n"] == row["state"]["totals"]["total"]
    assert list(s["steps"]) == ["parse", "spec", "review", "prototype", "accept_review"]


# --- summarize --------------------------------------------------------------------------------

def test_wilson():
    assert summarize.wilson(0, 0) == (0.0, 0.0)
    lo, hi = summarize.wilson(5, 10)
    assert lo == pytest.approx(0.2366, abs=1e-4) and hi == pytest.approx(0.7634, abs=1e-4)
    lo, hi = summarize.wilson(0, 10)
    assert lo == 0.0 and hi == pytest.approx(0.2775, abs=1e-4)
    lo, hi = summarize.wilson(10, 10)
    assert lo == pytest.approx(0.7225, abs=1e-4) and hi == 1.0
    lo, hi = summarize.wilson(5, 5)
    assert lo == pytest.approx(0.5655, abs=1e-4)


def test_pct():
    values = list(range(1, 11))
    assert summarize.pct(values, 0.5) == 5 and summarize.pct(values, 0.95) == 10
    assert summarize.pct(values, 0.0) == 1
    assert summarize.pct([7], 0.95) == 7 and summarize.pct([], 0.5) == 0.0
    assert summarize.pct([3, 1, 2], 0.5) == 2


def step(name, ms, rub, ok=True):
    return {"step": name, "round": 1, "attempt": 1, "ms": ms, "tokens_in": 10, "tokens_out": 10, "rub": rub,
            "ok": ok, "model": "m"}


def test_summarize_pipeline():
    rows = [
        {"id": "a", "state": fake_state(rub=2.0, accepted=8, total=8, versions=1,
                                        steps=[step("spec", 4000, 0.5), step("review", 2000, 0.1)])},
        {"id": "b", "state": fake_state(rub=4.0, accepted=6, total=8, versions=3, attempts=2, remarks=True,
                                        steps=[step("spec", 6000, 0.7), step("spec", 8000, 0.9, ok=False)]),
         "events": [{"type": "budget_skip"}]},
        {"id": "c", "state": fake_state(rub=0.5, status="failed", accepted=0, total=0)},
        {"id": "d", "state": None, "error": "RuntimeError: boom"},
    ]
    s = summarize.summarize_pipeline(rows)
    assert s["runs"] == 4 and s["done"]["k"] == 2 and s["failed_codes"] == {"timeout": 1, "runner": 1}
    assert s["acceptance"] == summarize.share(14, 16)
    assert s["all_accepted"]["k"] == 1 and s["all_accepted"]["n"] == 2
    assert s["spec_versions"] == {"1": 2, "3": 1}
    assert s["prototype_attempts"] == {"1": 2, "2": 1}
    assert s["first_red"]["k"] == 1 and s["spec_has_remarks"]["k"] == 1 and s["budget_skips"] == 1
    assert s["steps"]["spec"] == {"calls": 3, "failed": 1, "p50_ms": 6000, "p95_ms": 8000, "rub_per_call": 0.7}
    assert list(s["steps"]) == ["spec", "review"]
    assert s["rub_per_run"]["sum"] == 6.5 and s["rub_per_run"]["p50"] == 0.5 and s["rub_per_run"]["max"] == 4.0
    by = s["acceptance_by"]
    assert by["code"]["n"] + by["review"]["n"] == 16
    text = summarize.render_pipeline(s)
    assert "14 из 16" in text and "| spec | 3 | 1 | 6.0 | 8.0 | 0.700 |" in text


def test_summarize_checks_and_main(tmp_path, capsys):
    run_checks.main(["--code-only", "--out-dir", str(tmp_path), "--name", "c"])
    capsys.readouterr()
    rows = read_jsonl(tmp_path / "c.jsonl")
    s = summarize.summarize(rows)
    assert s["kind"] == "checks"
    assert s["by_code"]["caught_code"]["k"] == 5 and s["by_review"]["caught_code"]["k"] == 0
    assert s["valid"]["false_code"] == summarize.share(0, 5) and s["missed"] == []
    assert s["by_review"]["caught_review"]["n"] == 0
    assert summarize.main([str(tmp_path / "c.jsonl")]) == 0
    assert "5 из 5" in capsys.readouterr().out
    assert summarize.main(["--json", str(tmp_path / "c.jsonl")]) == 0
    assert json.loads(capsys.readouterr().out)["rows"] == 15


def test_summarize_rejects_mixed():
    with pytest.raises(ValueError):
        summarize.summarize([{"state": {}}, {"kind": "valid", "code_errors": []}])


# --- calibration ------------------------------------------------------------------------------

def test_checklist_items():
    assert CHECKLIST_IDS == [f"Ч{i}" for i in range(1, 9)]


def review(bad=()):
    return [{"id": i, "ok": i not in bad, "note": f"замечание {i}" if i in bad else ""} for i in CHECKLIST_IDS]


def calib_rows():
    a = {"id": "run-a", "brief": "Бриф А", "specs": [
        {"round": 1, "spec": VALID["dental"], "review": review(["Ч5", "Ч6"]), "red": True},
        {"round": 2, "spec": VALID["dental"], "review": review(["Ч5"]), "red": True}]}
    b = {"id": "run-b", "brief": "Бриф Б", "specs": [
        {"round": 1, "spec": VALID["bakery"], "review": review(), "red": False},
        {"round": 2, "spec": {"title": "не проверена"}, "review": [], "red": False}]}
    no_review = {"id": "run-c", "brief": "Бриф В", "specs": [{"round": 1, "spec": VALID["fitness"], "review": []}]}
    return [{"id": "dental", "niche": "стоматология", "state": a}, {"id": "fitness", "state": no_review},
            {"id": "bakery", "niche": "пекарня", "state": b}]


def fill(form, answers):
    """Put ``answers[(spec_n, item)]`` into the form's «да / нет» cells, spec by spec."""
    out, n = [], 0
    for line in form.splitlines():
        if line.startswith("<!-- calibration"):
            n += 1
        m = re.match(r"^\| (Ч\d) \| (.+?) \|  \|  \|$", line)
        if m and (n, m[1]) in answers:
            line = f"| {m[1]} | {m[2]} | {answers[(n, m[1])]} |  |"
        out.append(line)
    return "\n".join(out)


def test_calibration_form():
    picked = calibration.pick(calib_rows(), 5)
    assert [(p["id"], p["run"], p["round"]) for p in picked] == [("dental", "run-a", 2), ("bakery", "run-b", 1)]
    form = calibration.make_form(picked, calibration.checklist())
    assert form.count("<!-- calibration id=") == 2
    assert "<!-- calibration id=dental run=run-a -->" in form
    assert VALID["dental"]["title"] in form and "> Бриф А" in form and "не проверена" not in form
    assert "замечание" not in form  # the reviewer's verdicts are not shown
    assert len(re.findall(r"^\| Ч\d \| .+ \|  \|  \|$", form, re.M)) == 16
    assert not re.search(r"^# (?!Бланк)", form, re.M)  # the spec's headings are nested
    empty, bad = calibration.parse_form(form)
    assert bad == [] and all(v is None for answers in empty.values() for v in answers.values())
    assert calibration.pick(calib_rows(), 1) == picked[:1]
    assert [p["id"] for p in calibration.pick(calib_rows(), 5, {"bakery"})] == ["bakery"]


def test_calibration_agreement():
    rows = calib_rows()
    form = calibration.make_form(calibration.pick(rows, 5), calibration.checklist())
    answers = {(1, i): "да" for i in CHECKLIST_IDS} | {(2, i): "Да" for i in CHECKLIST_IDS}
    answers.update({(1, "Ч5"): "нет", (1, "Ч6"): "нет", (1, "Ч8"): "", (2, "Ч2"): "может"})
    human, bad = calibration.parse_form(fill(form, answers))
    assert bad == ["bakery Ч2: «может» — не «да» и не «нет»"]
    a = calibration.agreement(human, calibration.reviewer_verdicts(rows), CHECKLIST_IDS)
    # spec 1: 7 cells (Ч8 empty), Ч6 differs; spec 2: 7 cells (Ч2 not counted), all agree
    assert a["cells"] == summarize.share(13, 14)
    assert a["table"] == {"both_yes": 12, "both_no": 1, "reviewer_missed": 1, "reviewer_false_alarm": 0}
    assert a["kappa"] == pytest.approx(0.6316, abs=1e-4)
    assert a["per_item"]["Ч6"] == summarize.share(1, 2) and a["per_item"]["Ч5"] == summarize.share(2, 2)
    assert a["per_item"]["Ч8"]["n"] == 1 and a["per_item"]["Ч2"]["n"] == 1
    assert a["disagreements"] == [{"id": "dental", "item": "Ч6", "human": False, "reviewer": True, "note": ""}]
    assert a["empty_cells"] == 2 and a["missing_runs"] == [] and a["specs"] == 2
    text = calibration.render_agreement(a, bad)
    assert "13 из 14" in text and "| dental | Ч6 | нет | да |" in text and "может" in text


def test_calibration_missing_run_and_all_agree():
    rows = calib_rows()
    form = calibration.make_form(calibration.pick(rows, 5), calibration.checklist())
    human, _ = calibration.parse_form(fill(form, {(2, i): "+" for i in CHECKLIST_IDS}))
    a = calibration.agreement(human, calibration.reviewer_verdicts(rows[1:]), CHECKLIST_IDS)
    assert a["missing_runs"] == ["dental"] and a["cells"] == summarize.share(8, 8)
    assert a["kappa"] is None  # all «да» on both sides: kappa is undefined


def test_calibration_main(tmp_path, capsys):
    runs = tmp_path / "p.jsonl"
    runs.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in calib_rows()), encoding="utf-8")
    form = tmp_path / "form.md"
    assert calibration.main(["form", str(runs), "--n", "2", "--out", str(form)]) == 0
    filled = fill(form.read_text(encoding="utf-8"), {(1, i): "да" for i in CHECKLIST_IDS})
    form.write_text(filled, encoding="utf-8")
    assert calibration.main(["agree", str(form), str(runs)]) == 0
    out = capsys.readouterr().out
    assert "7 из 8" in out and "Ч5" in out
    empty = tmp_path / "empty.jsonl"
    empty.write_text(json.dumps({"id": "x", "state": {"specs": []}}), encoding="utf-8")
    assert calibration.main(["form", str(empty)]) == 1
