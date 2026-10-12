"""The pipeline of one run (docs/SPEC.md, «Шаги конвейера»): parse -> spec -> check + review
(red -> rework, at most 2 rounds) -> prototype -> prototype check (one retry) -> acceptance by
code and by the reviewer.

Pure orchestration: the gateway, the store, the settings and the clock come in ``Deps``, so tests
run the whole thing on fakes. After every step the state (docs/SPEC.md, «Состояние прогона») is
saved and an event goes to the store; the page reads them by SSE.

Money: the run holds the worst case of its main pass (parse, spec, review, prototype,
acceptance review) from the start; app.main holds it at admission. Every call reserves its own
worst case before it starts and settles the real cost after, also on errors and timeouts
(``Run.call``); a main call takes its step's share out of the hold. So extras and other runs
spend only what is left beyond the held pass. A main reservation refused fails the run with
"budget_exhausted". Extras — rework rounds and the prototype retry — are skipped instead, with
a "budget_skip" event (a gateway error there is skipped too). A format retry belongs to its
step: in the main pass it may use the rest of the hold (without it the run fails anyway), in a
rework round it is an extra like the round.

Time: the whole run gets ``run_timeout_s``; each call's timeout is the step's timeout or what is
left of the run, whichever is less. Past the deadline the run fails with "timeout". An extra
starts only if, after half the timeouts of the main steps still ahead, it gets at least half of
its own; its timeout ends before that reserve, otherwise it is skipped ("time_skip").
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Callable

from app import checks, prompts, schema
from app.checks import _items, _label, _str  # the same ids and texts accept_by_code reports
from app.config import Settings
from app.gateway import MODEL_PARAMS, Chat, ChatClient, GatewayError
from app.pii import mask_pii
from app.pricing import messages_worst_rub, step_worst_rub
from app.store import Store

log = logging.getLogger("demo_brief")

MAX_REWORK_ROUNDS = 2
PROTO_ATTEMPTS = 2
TRUNCATED = ("Ответ обрезан на лимите токенов, прототип не поместился. Сделай короче: один общий CSS, "
             "без повторов разметки, до 10 КБ.")
CODE_CHECKS = ("screen", "field")
MAIN_STEPS = ("parse", "spec", "review", "prototype", "accept_review")  # one pass, app.pricing.run_worst_rub
TIME_SHARE = 0.5
NO_ANSWER = "ревьюер не ответил по этому пункту"

MESSAGES = {
    "model_format": "Модель дважды ответила не по формату. Попробуйте ещё раз или посмотрите записанный пример.",
    "model_error": "Модель не ответила. Попробуйте ещё раз через минуту или посмотрите записанный пример.",
    "timeout": "Прогон не уложился в {s} с и остановлен. Попробуйте ещё раз или посмотрите записанный пример.",
    "internal": "Внутренняя ошибка прогона. Попробуйте ещё раз.",
}


@dataclass
class Deps:
    gateway: ChatClient
    store: Store
    settings: Settings
    clock: Callable[[], float] = field(default=time.monotonic)


class RunFailed(Exception):
    def __init__(self, code: str, message: str | None = None):
        super().__init__(code)
        self.code = code
        self.message = message


class _Skip(Exception):
    """An extra call was not made ("budget", "time") or failed ("model_error"); the run goes on."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def initial_state(run_id: str, brief: str, created: float) -> dict:
    return {"id": run_id, "status": "running", "brief": brief, "created": created,
            "parse": None, "specs": [], "spec": None, "spec_md": None, "spec_has_remarks": False,
            "prototype": None, "acceptance": [], "steps": [], "totals": None, "error": None}


_JSON_FENCE = re.compile(r"\A\s*```(?:json)?[ \t]*\r?\n?(.*?)\r?\n?```\s*\Z", re.DOTALL | re.IGNORECASE)


def parse_answer(content: str, answer_schema: dict) -> dict | None:
    """The model's JSON answer, or None when it is off format: not JSON, not an object, a required
    field missing or a list/object field of another type. Deeper types are left to app.checks."""
    text = content.strip() if isinstance(content, str) else ""
    fenced = _JSON_FENCE.match(text)
    if fenced:
        text = fenced[1]
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        return None
    if not isinstance(data, dict):
        return None
    body = answer_schema["schema"]
    for key in body["required"]:
        if key not in data:
            return None
        kind = body["properties"][key].get("type")
        value = data[key]
        if (kind == "array" and not isinstance(value, list)) or (kind == "object" and not isinstance(value, dict)):
            return None
    return data


def verdicts(items) -> list[dict]:
    """Reviewer items as ``{"id", "ok", "note"}``; anything but ``ok: true`` is a «no»."""
    return [{"id": _str(i.get("id")), "ok": i.get("ok") is True, "note": _str(i.get("note"))}
            for i in (items if isinstance(items, list) else []) if isinstance(i, dict)]


def checklist_verdicts(items) -> list[dict]:
    """The spec reviewer's verdicts with every checklist item present: an item it skipped is a
    «no», so an empty or short answer cannot make the spec green. A bare number is read as Ч-id."""
    got = verdicts(items)
    for v in got:
        if v["id"].isdigit():
            v["id"] = "Ч" + v["id"]
    seen = {v["id"] for v in got}
    return got + [{"id": cid, "ok": False, "note": NO_ANSWER} for cid, _ in prompts.CHECKLIST if cid not in seen]


def remarks_for(issues: list[dict], review: list[dict]) -> list[str]:
    """What goes back to the spec's author: code issues (K) and the reviewer's «no» (Ч); an item
    the reviewer skipped goes back as the checklist item itself."""
    names = dict(prompts.CHECKLIST)
    out = [f"{i['id']}: {i['text']}" for i in issues]
    for r in review:
        if not r["ok"]:
            note = r["note"] if r["note"] != NO_ANSWER else ""
            out.append(f"{r['id']}: {note or names.get(r['id'], 'пункт чек-листа не выполнен')}")
    return out


class Run:
    def __init__(self, run_id: str, brief: str, deps: Deps, hold_id: int | None = None):
        self.id = run_id
        self.brief = mask_pii(brief)[0]  # app.main masks already; again here for scripts and evals
        self.deps = deps
        self.s = deps.settings
        self.store = deps.store
        self.t0 = deps.clock()
        self.deadline = self.t0 + self.s.run_timeout_s
        self.hold_id = hold_id
        # main steps not started yet -> their share of the held pass
        self.ahead = {step: step_worst_rub(self.s, step) for step in MAIN_STEPS}
        state = self.store.load_run(run_id)
        if state is None:
            state = initial_state(run_id, self.brief, self.store.clock())
            self.store.create_run(run_id, state)
        self.state = state

    # ---------- plumbing ----------

    def t_ms(self) -> int:
        return int((self.deps.clock() - self.t0) * 1000)

    def left_s(self) -> float:
        return self.deadline - self.deps.clock()

    def check_time(self) -> None:
        if self.left_s() <= 0:
            raise RunFailed("timeout")

    def emit(self, type_: str, **data) -> None:
        self.store.add_event(self.id, self.t_ms(), type_, data)

    def save(self) -> None:
        self.store.save_run(self.id, self.state)

    def hold_pass(self) -> None:
        """Hold the main pass's worst case unless app.main already did at admission."""
        if self.hold_id is not None:
            return
        decision = self.store.hold(sum(self.ahead.values()))
        if not decision.allowed:
            raise RunFailed("budget_exhausted", decision.message)
        self.hold_id = decision.reservation_id

    def release_hold(self) -> None:
        """What is left of the held pass goes back to the day's budget. Idempotent, never raises."""
        hold, self.hold_id = self.hold_id, None
        if hold is None:
            return
        try:
            self.store.settle(hold, 0.0)
        except Exception:
            log.exception("run=%s could not release its hold", self.id[:6])

    def extra_timeout(self, conf) -> float | None:
        """An extra call's timeout: what is left after half the timeouts of the main steps still
        ahead; None when that is less than half of its own step's timeout."""
        reserve = TIME_SHARE * sum(self.s.step(step).timeout_s for step in self.ahead)
        timeout = min(conf.timeout_s, self.left_s() - reserve)
        return timeout if timeout >= TIME_SHARE * conf.timeout_s else None

    def call(self, step: str, messages: list[dict], answer_schema: dict | None, round_: int = 1,
             attempt: int = 1, extra: bool = False) -> tuple[Chat, float]:
        """One model call with its reservation. Returns the answer and its cost (the reserve when
        the gateway gave no usage). ``extra`` (a rework round, the prototype retry): skipped
        instead of failing the run — no money beyond the held pass, no time, a gateway error."""
        conf = self.s.step(step)
        self.check_time()
        timeout = min(conf.timeout_s, self.left_s())
        if extra:
            timeout = self.extra_timeout(conf)
            if timeout is None:
                self.emit("time_skip", step=step, round=round_, attempt=attempt)
                raise _Skip("time")
        worst = messages_worst_rub(conf.model, messages, conf.max_tokens)
        # a main step's first call takes its share of the hold; its format retry may take the rest
        share = 0.0 if extra else self.ahead.pop(step, worst)
        hold = (self.hold_id, share) if self.hold_id is not None and share else None
        decision = self.store.reserve(worst, hold=hold)
        if not decision.allowed:
            if extra:
                self.emit("budget_skip", step=step, round=round_, attempt=attempt)
                raise _Skip("budget")
            raise RunFailed("budget_exhausted", decision.message)
        cost = 0.0  # settled at 0 if even the step_start event fails: no call was made
        try:
            self.emit("step_start", step=step, round=round_, attempt=attempt)
            started, cost = self.deps.clock(), worst
            chat = self.deps.gateway.chat(conf.model, messages, answer_schema, conf.max_tokens,
                                          MODEL_PARAMS.get(conf.model), timeout)
            if chat.has_usage:
                cost = chat.cost_rub
        except GatewayError as e:
            log.warning("run=%s step=%s round=%s attempt=%s gateway error: %s", self.id[:6], step, round_,
                        attempt, e.detail)
            failed = Chat("", 0, 0, int((self.deps.clock() - started) * 1000), worst)
            self.record(step, round_, attempt, conf.model, failed, worst, ok=False, error="model_error",
                        check_time=False)
            self.check_time()
            if extra:
                raise _Skip("model_error") from None
            raise RunFailed("model_error") from None
        finally:
            self.store.settle(decision.reservation_id, cost)
        return chat, cost

    def record(self, step: str, round_: int, attempt: int, model: str, chat: Chat, cost: float, ok: bool,
               error: str | None = None, check_time: bool = True) -> None:
        row = {"step": step, "round": round_, "attempt": attempt, "ms": chat.ms, "tokens_in": chat.tokens_in,
               "tokens_out": chat.tokens_out, "rub": round(cost, 4), "ok": ok, "model": model}
        if error:
            row["error"] = error
        self.state["steps"].append(row)
        self.save()
        self.emit("step_end", **row)
        if check_time:
            self.check_time()

    def json_step(self, step: str, messages: list[dict], answer_schema: dict, round_: int = 1,
                  extra: bool = False) -> dict:
        """A step with a JSON answer: one retry when it is off format, then the run fails
        ("model_format"). The retry is what its step is: in an extra step a retry that cannot be
        made skips the round (_Skip), in a main step it fails the run (budget, gateway)."""
        model = self.s.step(step).model
        for attempt in (1, 2):
            msgs = messages if attempt == 1 else prompts.with_format_retry(messages)
            chat, cost = self.call(step, msgs, answer_schema, round_, attempt, extra=extra)
            data = parse_answer(chat.content, answer_schema)
            self.record(step, round_, attempt, model, chat, cost, ok=data is not None,
                        error=None if data is not None else "model_format")
            if data is not None:
                return data
        raise RunFailed("model_format")

    # ---------- steps ----------

    def parse(self) -> dict:
        data = self.json_step("parse", prompts.parse_messages(self.brief), schema.PARSE)
        parse, _ = checks.truncate_parse(data)
        self.state["parse"] = parse
        self.save()
        return parse

    def round_fits(self, messages: list[dict]) -> bool:
        conf = self.s.step("spec")
        need = messages_worst_rub(conf.model, messages, conf.max_tokens) + step_worst_rub(self.s, "review")
        return self.store.left_today() >= need

    def specs(self, parse: dict) -> dict:
        previous, remarks = None, None
        for round_ in range(1, MAX_REWORK_ROUNDS + 2):
            rework = round_ > 1
            messages = prompts.spec_messages(self.brief, parse, previous, remarks)
            if rework and not self.round_fits(messages):
                self.emit("budget_skip", step="spec", round=round_, attempt=1)
                self.state["spec_has_remarks"] = True
                break
            try:
                raw = self.json_step("spec", messages, schema.SPEC, round_, extra=rework)
            except _Skip:
                self.state["spec_has_remarks"] = True
                break
            spec, issues = checks.check_spec(raw)
            entry = {"round": round_, "spec": spec, "issues": issues, "review": [],
                     "red": checks.is_red_by_code(issues)}
            self.state["specs"].append(entry)
            self.state.update(spec=spec, spec_md=checks.render_spec_md(spec))
            self.save()
            self.emit("check", step="check", round=round_, issues=issues)
            try:
                answer = self.json_step("review", prompts.review_messages(self.brief, self.state["spec_md"]),
                                        schema.SPEC_REVIEW, round_, extra=rework)
            except _Skip:
                self.state["spec_has_remarks"] = True
                self.save()
                break
            review = checklist_verdicts(checks.truncate_review(answer)[0].get("items"))
            entry.update(review=review, red=entry["red"] or any(not r["ok"] for r in review))
            self.save()
            self.emit("review", step="review", round=round_, items=review, red=entry["red"])
            if not entry["red"]:
                break
            if round_ == MAX_REWORK_ROUNDS + 1:
                self.state["spec_has_remarks"] = True
                self.save()
                break
            previous, remarks = spec, remarks_for(issues, review)
        return self.state["spec"]

    def prototype(self, spec: dict) -> str | None:
        """Up to two attempts; keeps the better one: a usable HTML over an unusable one, then
        fewer P errors, then the later one. Usable: found, within 80 KB and no network by P3. An
        unusable HTML is neither stored nor served, acceptance then says «прототипа нет»."""
        model = self.s.step("prototype").model
        best, remarks = None, None
        for attempt in range(1, PROTO_ATTEMPTS + 1):
            try:
                chat, cost = self.call("prototype", prompts.prototype_messages(spec, remarks), None, 1, attempt,
                                       extra=attempt > 1)
            except _Skip:
                break
            html = checks.extract_html(chat.content)
            issues = checks.check_prototype(html or "", spec)
            if chat.finish_reason == "length":
                # Cut at max_tokens: the HTML may still parse, but its script is likely broken.
                issues.insert(0, checks.issue("P1", "error", TRUNCATED))
            errors = [i for i in issues if i["level"] == "error"]
            usable = html is not None and not any(i["id"] in ("P2", "P3") for i in errors)
            self.record("prototype", 1, attempt, model, chat, cost, ok=html is not None, check_time=False)
            self.emit("proto_check", step="proto_check", attempt=attempt, issues=issues)
            score = (not usable, len(errors))
            if best is None or score <= best[0]:
                best = (score, html if usable else None)
                self.keep_prototype(html, issues, attempt, usable)
            self.check_time()
            if not errors:
                break
            remarks = [f"{i['id']}: {i['text']}" for i in errors]
        return best[1] if best else None

    def keep_prototype(self, html: str | None, issues: list[dict], attempt: int, stored: bool) -> None:
        size = len(html.encode("utf-8", "replace")) if html else 0
        if stored:
            self.store.save_prototype(self.id, html)
        self.state["prototype"] = {"attempts": attempt, "issues": issues, "bytes": size,
                                   "url": f"/api/brief/runs/{self.id}/prototype" if stored else None}
        self.save()

    def acceptance(self, spec: dict, html: str | None) -> None:
        by_code = checks.accept_by_code(spec, html)
        self.emit("accept", step="accept_code", items=by_code)
        criteria = [(i, a) for i, a in _items(spec, "acceptance") if a.get("check") not in CODE_CHECKS]
        rows = [{"id": _label(a, i), "text": _str(a.get("text")), "by": "review"} for i, a in criteria]
        if rows and html is None:
            by_review = [{**r, "ok": False, "note": "прототипа нет"} for r in rows]
        elif rows:
            asked = [{**a, "id": r["id"]} for (_, a), r in zip(criteria, rows)]
            answer = self.json_step("accept_review", prompts.accept_messages(spec, asked, checks.screens_text(html)),
                                    schema.ACCEPT_REVIEW)
            got = {v["id"]: v for v in verdicts(answer.get("items"))}
            by_review = [{**r, "ok": got[r["id"]]["ok"], "note": got[r["id"]]["note"]} if r["id"] in got
                         else {**r, "ok": False, "note": "ревьюер не ответил по этому критерию"} for r in rows]
        else:
            by_review = []
        if by_review:
            self.emit("accept", step="accept_review", items=by_review)
        code_rows, review_rows = iter(by_code), iter(by_review)
        self.state["acceptance"] = [next(code_rows) if a.get("check") in CODE_CHECKS else next(review_rows)
                                    for _, a in _items(spec, "acceptance")]
        self.save()

    # ---------- the run ----------

    def totals(self) -> dict:
        steps = self.state["steps"]
        acceptance = self.state["acceptance"]
        return {"ms": self.t_ms(), "rub": round(sum(s["rub"] for s in steps), 4),
                "tokens_in": sum(s["tokens_in"] for s in steps), "tokens_out": sum(s["tokens_out"] for s in steps),
                "accepted": sum(1 for a in acceptance if a["ok"]), "total": len(acceptance),
                "failed_ids": [a["id"] for a in acceptance if not a["ok"]]}

    def execute(self) -> None:
        self.hold_pass()
        parse = self.parse()
        spec = self.specs(parse)
        self.check_time()
        html = self.prototype(spec)
        self.acceptance(spec, html)
        self.check_time()
        totals = self.totals()
        self.state.update(status="done", totals=totals)
        self.release_hold()  # before the final event: the page asks for the budget right after it
        self.store.finish_run(self.id, self.state, totals["ms"], "done", {
            "status": "done", "accepted": totals["accepted"], "total": totals["total"],
            "failed_ids": totals["failed_ids"], "spec_has_remarks": self.state["spec_has_remarks"],
            "ms": totals["ms"], "rub": totals["rub"], "tokens_in": totals["tokens_in"],
            "tokens_out": totals["tokens_out"]})

    def fail(self, code: str, message: str | None) -> None:
        message = message or MESSAGES.get(code, MESSAGES["internal"]).format(s=int(self.s.run_timeout_s))
        error = {"code": code, "message": message}
        self.state.update(status="failed", error=error, totals=self.totals())
        self.release_hold()
        self.store.finish_run(self.id, self.state, self.t_ms(), "failed", error)


def run(run_id: str, brief: str, deps: Deps, hold_id: int | None = None) -> dict:
    """Run the pipeline to the end; never raises (an unexpected error fails the run "internal").
    ``hold_id``: the main pass already held by the caller (app.main), released at the end.
    Returns the final state."""
    error = {"code": "internal", "message": MESSAGES["internal"]}
    try:
        r = Run(run_id, brief, deps, hold_id)
    except Exception:  # the store is broken: try to give back the hold and end the run's row
        log.exception("run=%s could not start", run_id[:6])
        _abandon(deps.store, run_id, hold_id, error)
        return {"id": run_id, "status": "failed", "error": error}
    try:
        r.execute()
    except RunFailed as e:
        log.info("run=%s failed: %s", run_id[:6], e.code)
        _fail_quietly(r, e.code, e.message)
    except Exception:
        log.exception("run=%s internal error", run_id[:6])
        _fail_quietly(r, "internal", None)
    finally:
        r.release_hold()
    log.info("run=%s status=%s rub=%s", run_id[:6], r.state["status"], (r.state.get("totals") or {}).get("rub"))
    return r.state


def _abandon(store: Store, run_id: str, hold_id: int | None, error: dict) -> None:
    try:
        if hold_id is not None:
            store.settle(hold_id, 0.0)
        state = store.load_run(run_id)
        if state is not None and state.get("status") == "running":
            state.update(status="failed", error=error)
            store.finish_run(run_id, state, 0, "failed", error)
    except Exception:
        log.exception("run=%s could not be marked failed", run_id[:6])


def _fail_quietly(r: Run, code: str, message: str | None) -> None:
    try:
        r.fail(code, message)
    except Exception:
        log.exception("run=%s could not save the failure", r.id[:6])
