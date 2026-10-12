"""Scripted gateway, a fake clock and ready answers for every step of the pipeline."""

from __future__ import annotations

import copy
import json
import threading
from dataclasses import dataclass

from app.gateway import Chat, GatewayError
from app.pricing import call_cost_rub
from tests.fixtures_spec import VALID_PROTOTYPE, VALID_SPEC

REVIEW_IDS = [f"Ч{i}" for i in range(1, 9)]
ACCEPT_REVIEW_IDS = ["A3", "A8"]  # criteria with check "review" in VALID_SPEC

PARSE = {"niche": "автосервис", "goal": "Клиенты записываются сами, приёмщик меньше сидит на телефоне.",
         "roles": [{"name": "Клиент", "need": "записаться"}, {"name": "Мастер-приёмщик", "need": "подтвердить"},
                   {"name": "Управляющий", "need": "видеть загрузку"}],
         "assumptions": ["Один сервис"], "questions": [{"q": "Сколько постов?", "why": "меняет расписание"}],
         "out_of_scope": ["Оплата онлайн"]}


@dataclass
class Reply:
    content: str
    tokens_in: int = 1000
    tokens_out: int = 500
    ms: int = 20
    usage: bool = True
    finish: str = "stop"
    advance_s: float = 0.0  # moves the fake clock: a slow model


class FakeClock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class FakeGateway:
    def __init__(self, replies=(), clock: FakeClock | None = None):
        """replies: strings, Reply, exceptions, or callables returning one of those (called at
        the moment of the call, e.g. to drain the budget or to wait on an event)."""
        self.replies = list(replies)
        self.clock = clock
        self.calls: list[dict] = []
        self.lock = threading.Lock()

    def chat(self, model, messages, schema, max_tokens, params=None, timeout_s=None):
        with self.lock:
            self.calls.append({"model": model, "messages": [dict(m) for m in messages], "schema": schema,
                               "max_tokens": max_tokens, "timeout_s": timeout_s})
            if not self.replies:
                raise AssertionError("no more scripted replies")
            reply = self.replies.pop(0)
        if callable(reply) and not isinstance(reply, BaseException):
            reply = reply()
        if isinstance(reply, BaseException):
            raise reply
        if isinstance(reply, str):
            reply = Reply(reply)
        if self.clock is not None and reply.advance_s:
            self.clock.advance(reply.advance_s)
        tin, tout = (reply.tokens_in, reply.tokens_out) if reply.usage else (0, 0)
        return Chat(reply.content, tin, tout, reply.ms, call_cost_rub(model, tin, tout), reply.finish, reply.usage)

    def steps(self) -> list[str]:
        """Which step each call was, by its schema: parse, spec, review, accept_review, prototype."""
        names = {"brief_parse": "parse", "spec": "spec", "spec_review": "review", "accept_review": "accept_review"}
        return [names[c["schema"]["name"]] if c["schema"] else "prototype" for c in self.calls]

    def user(self, i: int) -> str:
        return self.calls[i]["messages"][-1]["content"]


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def parse_reply(**changes) -> str:
    return dumps({**PARSE, **changes})


def spec(**changes) -> dict:
    out = copy.deepcopy(VALID_SPEC)
    out.update(changes)
    return out


def vague_spec() -> dict:
    """Red by code: K7, a vague criterion."""
    out = spec()
    out["acceptance"][0]["text"] = "Клиент быстро создаёт запись"
    return out


def spec_reply(value: dict | None = None) -> str:
    return dumps(VALID_SPEC if value is None else value)


def review_reply(bad: dict | None = None) -> str:
    """All of Ч1–Ч8 ok except ``bad`` ({id: note})."""
    bad = bad or {}
    return dumps({"items": [{"id": i, "ok": i not in bad, "note": bad.get(i, "")} for i in REVIEW_IDS]})


def proto_reply(html: str = VALID_PROTOTYPE) -> str:
    return "Вот прототип:\n```html\n" + html + "\n```"


def accept_reply(bad: dict | None = None, ids=ACCEPT_REVIEW_IDS) -> str:
    bad = bad or {}
    return dumps({"items": [{"id": i, "ok": i not in bad, "note": bad.get(i, "видно на экране")} for i in ids]})


def happy() -> list:
    return [parse_reply(), spec_reply(), review_reply(), proto_reply(), accept_reply()]


__all__ = ["FakeClock", "FakeGateway", "GatewayError", "Reply", "accept_reply", "happy", "parse_reply",
           "proto_reply", "review_reply", "spec", "spec_reply", "vague_spec"]


# The models the money arithmetic in the tests was written for. Defaults change after probes
# (app/config.py); tests pin these so their ruble bounds stay meaningful.
PINNED_ENV = {"PARSE_MODEL": "gemini/gemini-3.1-flash-lite", "SPEC_MODEL": "gemini/gemini-3.1-flash-lite",
              "REVIEW_MODEL": "anthropic/claude-haiku-5-5", "PROTO_MODEL": "gemini/gemini-3.1-flash-lite",
              "PROTO_MAX_TOKENS": "14000", "SPEC_MAX_TOKENS": "5000"}
