import asyncio
import json
import sqlite3
import threading
import time
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app import main
from app.config import Settings
from app.main import Streams, create_app, event_stream, load_examples
from app.pipeline import initial_state
from app.pricing import run_worst_rub
from app.store import Store
from tests.fakes import FakeGateway, happy, PINNED_ENV
from tests.fixtures_spec import VALID_PROTOTYPE

# Copied from docs/SPEC.md on purpose, not imported: the test must catch any drift.
CSP = ("sandbox allow-scripts; default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
       "img-src data:; font-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'self'")
BRIEF = "Нужна онлайн-запись в автосервис. Пишите boss@corp.example или 8 912 345 67 89"


@pytest.fixture
def settings(tmp_path):
    recorded = tmp_path / "recorded"
    recorded.mkdir()
    return replace(Settings.from_env(PINNED_ENV), api_key="k", base_url="http://gw.example", var_dir=tmp_path / "var",
                   recorded_dir=recorded)


@pytest.fixture(autouse=True)
def fast_sse(monkeypatch):
    monkeypatch.setattr(main, "SSE_POLL_S", 0.02)


def make(settings, replies=(), **overrides):
    gw = FakeGateway(replies)
    return TestClient(create_app(replace(settings, **overrides), client=gw)), gw


def start(client, brief=BRIEF):
    r = client.post("/api/brief/runs", json={"brief": brief})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def wait_done(client, run_id, timeout_s=5.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        state = client.get(f"/api/brief/runs/{run_id}").json()
        if state["status"] != "running":
            return state
        time.sleep(0.01)
    raise AssertionError("the run did not finish")


def frames(text):
    """SSE body -> list of (id, data dict) and comment lines."""
    out = []
    for block in text.strip().split("\n\n"):
        lines = block.split("\n")
        if lines[0].startswith(":"):
            out.append(("comment", lines[0]))
            continue
        assert lines[0].startswith("id: ") and lines[1].startswith("data: "), block
        out.append((int(lines[0][4:]), json.loads(lines[1][6:])))
    return out


def blocking(release: threading.Event, reply):
    def wait():
        assert release.wait(5)
        return reply
    return wait


def test_health_and_status(settings):
    client, _ = make(settings)
    assert client.get("/api/brief/health").json() == {"ok": True}
    r = client.get("/api/brief/status")
    data = r.json()
    assert data["live"] is True and data["budget_rub"] == 15 and data["spent_rub"] == 0
    assert 4.5 < data["run_worst_rub"] < 6.5 and data["runs_running"] == 0
    assert data["models"] == {"parse": "gemini/gemini-3.1-flash-lite", "spec": "gemini/gemini-3.1-flash-lite",
                              "review": "anthropic/claude-haiku-5-5", "prototype": "gemini/gemini-3.1-flash-lite",
                              "accept_review": "anthropic/claude-haiku-5-5"}
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["content-security-policy"] == "default-src 'none'; frame-ancestors 'none'"
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["referrer-policy"] == "no-referrer"


def test_run_end_to_end_with_contacts_masked(settings):
    client, gw = make(settings, happy())
    run_id = start(client)
    state = wait_done(client, run_id)
    assert state["status"] == "done" and state["totals"]["accepted"] == 8 and state["id"] == run_id
    assert "boss@corp.example" not in state["brief"] and "[email скрыт]" in state["brief"]
    assert "345 67 89" not in state["brief"] and "[телефон скрыт]" in state["brief"]
    sent = json.dumps([c["messages"] for c in gw.calls], ensure_ascii=False)
    assert "boss@corp.example" not in sent and "345 67 89" not in sent
    db = sqlite3.connect(settings.var_dir / "brief.sqlite3")
    dump = "\n".join(str(row) for table in ("runs", "events", "prototypes")
                     for row in db.execute(f"SELECT * FROM {table}"))
    assert "boss@corp.example" not in dump and "345 67 89" not in dump
    for key in ("parse", "specs", "spec", "spec_md", "spec_has_remarks", "prototype", "acceptance", "steps",
                "totals", "error", "created"):
        assert key in state
    status = client.get("/api/brief/status").json()
    assert status["spent_rub"] > 0 and status["counters"]["done_24h"] == 1 and status["runs_running"] == 0


def test_sse_after_the_run(settings):
    client, _ = make(settings, happy())
    run_id = start(client)
    wait_done(client, run_id)
    r = client.get(f"/api/brief/runs/{run_id}/events")
    assert r.status_code == 200
    assert r.headers["content-type"] == "text/event-stream; charset=utf-8"
    assert r.headers["x-accel-buffering"] == "no" and r.headers["cache-control"] == "no-store"
    got = frames(r.text)
    assert [i for i, _ in got] == list(range(1, 17))
    assert all(e["seq"] == i and "t_ms" in e for i, e in got)
    assert got[0][1] == {"seq": 1, "t_ms": got[0][1]["t_ms"], "type": "step_start", "step": "parse", "round": 1,
                         "attempt": 1}
    assert got[-1][1]["type"] == "done" and got[-1][1]["accepted"] == 8
    end = got[1][1]
    assert {"ms", "tokens_in", "tokens_out", "rub", "ok", "model"} <= set(end)
    assert not any("spec" in e and isinstance(e.get("spec"), dict) for _, e in got)  # no artifacts in events


def test_sse_last_event_id(settings):
    client, _ = make(settings, happy())
    run_id = start(client)
    wait_done(client, run_id)
    tail = frames(client.get(f"/api/brief/runs/{run_id}/events", headers={"Last-Event-ID": "14"}).text)
    assert [i for i, _ in tail] == [15, 16]
    started = time.monotonic()
    r = client.get(f"/api/brief/runs/{run_id}/events", headers={"Last-Event-ID": "16"})
    assert r.text == "" and time.monotonic() - started < 1  # the client has everything: closed at once
    junk = frames(client.get(f"/api/brief/runs/{run_id}/events", headers={"Last-Event-ID": "abc"}).text)
    assert junk[0][0] == 1


def test_sse_follows_a_running_run_with_heartbeat(settings, monkeypatch):
    monkeypatch.setattr(main, "SSE_HEARTBEAT_S", 0.05)
    release = threading.Event()
    replies = happy()
    replies[1] = blocking(release, replies[1])
    client, _ = make(settings, replies)
    run_id = start(client)
    threading.Timer(0.4, release.set).start()
    got = frames(client.get(f"/api/brief/runs/{run_id}/events").text)
    assert ("comment", ": ping") in got
    events = [e for i, e in got if i != "comment"]
    assert events[-1]["type"] == "done" and [e["seq"] for e in events] == list(range(1, 17))


def test_sse_stops_when_the_client_disconnects(settings):
    store = Store(settings.var_dir / "s.sqlite3", 15, 3)
    store.create_run("r-0123456789abcdef", initial_state("r-0123456789abcdef", "б", 1.0))
    calls = []

    async def disconnected():
        calls.append(1)
        return len(calls) > 2

    async def collect():
        return [chunk async for chunk in event_stream(store, "r-0123456789abcdef", 0, disconnected)]

    assert asyncio.run(asyncio.wait_for(collect(), 2)) == [] and len(calls) == 3


def test_unknown_runs(settings):
    client, _ = make(settings)
    for path in ("/api/brief/runs/{}", "/api/brief/runs/{}/events", "/api/brief/runs/{}/prototype"):
        for run_id in ("nope", "x" * 22, "a" * 200):
            r = client.get(path.format(run_id))
            assert r.status_code == 404 and r.json()["code"] == "not_found"


def test_prototype_headers_byte_exact(settings):
    client, _ = make(settings, happy())
    run_id = start(client)
    state = wait_done(client, run_id)
    r = client.get(state["prototype"]["url"])
    assert r.status_code == 200 and r.text == VALID_PROTOTYPE.strip()
    raw = [(k.decode(), v.decode()) for k, v in r.headers.raw]
    assert [v for k, v in raw if k.lower() == "content-security-policy"] == [CSP]
    assert r.headers["content-type"] == "text/html; charset=utf-8"
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["referrer-policy"] == "no-referrer"
    assert r.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("body", [{"brief": ""}, {"brief": "   "}, {"brief": "x" * 501}, {"brief": 5}, {},
                                  {"brief": "ок", "model": "openai/gpt-5.6-terra"}, [1]])
def test_bad_briefs(settings, body):
    client, gw = make(settings)
    r = client.post("/api/brief/runs", json=body)
    assert r.status_code == 422 and r.json()["code"] == "bad_request" and "500" in r.json()["message"]
    assert gw.calls == []


def test_not_json_and_too_large(settings):
    client, _ = make(settings)
    r = client.post("/api/brief/runs", content=b"{brief", headers={"content-type": "application/json"})
    assert r.status_code == 422
    r = client.post("/api/brief/runs", content=b"x" * 20000, headers={"content-type": "application/json"})
    assert r.status_code == 413 and r.json()["code"] == "too_long" and r.headers["cache-control"] == "no-store"


def test_brief_of_500_chars_is_fine(settings):
    client, _ = make(settings, happy())
    assert wait_done(client, start(client, "а" * 500))["status"] == "done"


def test_hourly_limit(settings):
    client, gw = make(settings, happy(), runs_per_hour=1)
    wait_done(client, start(client))
    r = client.post("/api/brief/runs", json={"brief": "ещё"})
    assert r.status_code == 429 and r.json()["code"] == "rate_limited" and r.json()["retry_after_s"] > 0
    assert len(gw.calls) == 5


def test_busy_does_not_use_up_the_hourly_limit(settings):
    release = threading.Event()
    replies = happy()
    replies[0] = blocking(release, replies[0])
    client, _ = make(settings, [*replies, *happy()], max_concurrent_runs=1, runs_per_hour=2)
    first = start(client)
    assert client.get("/api/brief/status").json()["runs_running"] == 1
    r = client.post("/api/brief/runs", json={"brief": "ещё"})
    assert r.status_code == 429 and r.json()["code"] == "busy" and r.json()["retry_after_s"] == 30
    release.set()
    wait_done(client, first)
    deadline = time.monotonic() + 2
    while client.get("/api/brief/status").json()["runs_running"] and time.monotonic() < deadline:
        time.sleep(0.01)
    assert wait_done(client, start(client))["status"] == "done"


def test_budget_gate_before_any_call(settings):
    client, gw = make(settings, happy(), daily_budget_rub=4)
    r = client.post("/api/brief/runs", json={"brief": BRIEF})
    assert r.status_code == 503 and r.json()["code"] == "budget_exhausted" and gw.calls == []
    assert client.get("/api/brief/status").json()["live"] is False


def test_offline_without_key(settings):
    (settings.recorded_dir / "demo.json").write_text(json.dumps(recorded()), encoding="utf-8")
    client = TestClient(create_app(replace(settings, api_key="")))
    r = client.post("/api/brief/runs", json={"brief": BRIEF})
    assert r.status_code == 503 and r.json()["code"] == "offline"
    assert client.get("/api/brief/status").json()["live"] is False
    assert client.get("/api/brief/examples").json()[0]["name"] == "demo"


def recorded(name="demo"):
    state = {**initial_state("x" * 22, "бриф", 1.0), "status": "done",
             "prototype": {"attempts": 1, "issues": [], "bytes": 10, "url": "/api/brief/runs/x/prototype"}}
    return {"name": name, "title": "Пример", "brief": "бриф", "state": state,
            "events": [{"seq": 1, "t_ms": 0, "type": "step_start", "step": "parse", "round": 1}],
            "prototype_html": "<!doctype html><html><body><section data-screen=\"S1\">Экран</section></body></html>"}


def test_examples(settings):
    (settings.recorded_dir / "demo.json").write_text(json.dumps(recorded(), ensure_ascii=False), encoding="utf-8")
    (settings.recorded_dir / "broken.json").write_text("{", encoding="utf-8")
    (settings.recorded_dir / "Bad Name.json").write_text(json.dumps(recorded()), encoding="utf-8")
    client, _ = make(settings)
    r = client.get("/api/brief/examples")
    assert r.json() == [{"name": "demo", "title": "Пример", "brief": "бриф"}]
    assert r.headers["cache-control"] == "public, max-age=300"
    one = client.get("/api/brief/examples/demo")
    data = one.json()
    assert set(data) == {"name", "title", "brief", "state", "events"} and data["events"][0]["t_ms"] == 0
    assert data["state"]["prototype"]["url"] == "/api/brief/examples/demo/prototype"
    assert one.headers["cache-control"] == "public, max-age=300"
    proto = client.get("/api/brief/examples/demo/prototype")
    assert proto.status_code == 200 and "data-screen" in proto.text
    assert proto.headers["content-security-policy"] == CSP and proto.headers["cache-control"] == "public, max-age=300"
    assert proto.headers["content-type"] == "text/html; charset=utf-8"


@pytest.mark.parametrize("name", ["..", "demo.json", "DEMO", "a" * 41, "-demo", "demo%00", "..%2Fdemo",
                                  "%2E%2E%2Fsecret", "broken", "Bad Name"])
def test_example_names_cannot_escape(settings, name):
    (settings.recorded_dir / "demo.json").write_text(json.dumps(recorded()), encoding="utf-8")
    (settings.recorded_dir.parent / "secret.json").write_text(json.dumps(recorded("secret")), encoding="utf-8")
    client, _ = make(settings)
    for path in (f"/api/brief/examples/{name}", f"/api/brief/examples/{name}/prototype"):
        assert client.get(path).status_code == 404, path


def test_no_examples(settings, tmp_path):
    client, _ = make(settings)
    assert client.get("/api/brief/examples").json() == []
    assert load_examples(tmp_path / "missing") == {}


def test_run_left_running_by_a_restart(settings):
    store = Store(settings.var_dir / "brief.sqlite3", 15, 3)
    store.create_run("r-0123456789abcdef", initial_state("r-0123456789abcdef", "б", store.clock()))
    client, _ = make(settings)
    state = client.get("/api/brief/runs/r-0123456789abcdef").json()
    assert state["status"] == "failed" and state["error"]["code"] == "restart"
    got = frames(client.get("/api/brief/runs/r-0123456789abcdef/events").text)
    assert [e["type"] for _, e in got] == ["failed"]


def wait_for(condition, timeout_s=3.0):
    deadline = time.monotonic() + timeout_s
    while not condition():
        assert time.monotonic() < deadline, "condition not reached"
        time.sleep(0.01)


@pytest.mark.parametrize("dest, code", [(None, 200), ("iframe", 200), ("document", 404), ("frame", 404),
                                        ("empty", 404)])
def test_prototype_opens_only_inside_a_frame(settings, dest, code):
    (settings.recorded_dir / "demo.json").write_text(json.dumps(recorded()), encoding="utf-8")
    client, _ = make(settings, happy())
    url = wait_done(client, start(client))["prototype"]["url"]
    headers = {"Sec-Fetch-Dest": dest} if dest else {}
    for path in (url, "/api/brief/examples/demo/prototype"):
        r = client.get(path, headers=headers)
        assert r.status_code == code, path
        if code == 200:
            assert r.headers["vary"] == "Sec-Fetch-Dest" and r.headers["content-security-policy"] == CSP
        else:
            assert r.json()["code"] == "not_found" and "<section" not in r.text


def test_a_second_run_does_not_start_on_the_money_of_one_pass(settings):
    budget = run_worst_rub(settings) * 1.5
    release = threading.Event()
    replies = happy()
    replies[0] = blocking(release, replies[0])
    client, gw = make(settings, [*replies, *happy()], daily_budget_rub=budget)
    first = start(client)
    r = client.post("/api/brief/runs", json={"brief": "ещё один"})
    assert r.status_code == 503 and r.json()["code"] == "budget_exhausted"
    assert client.get("/api/brief/status").json()["live"] is False
    release.set()
    assert wait_done(client, first)["status"] == "done"
    assert client.get("/api/brief/status").json()["live"] is True  # the held pass is given back
    assert wait_done(client, start(client))["status"] == "done"


def test_hold_is_given_back_when_the_start_is_refused(settings):
    client, _ = make(settings, happy(), runs_per_hour=1)
    wait_done(client, start(client))
    spent = client.get("/api/brief/status").json()["spent_rub"]
    assert client.post("/api/brief/runs", json={"brief": "ещё"}).status_code == 429
    assert client.get("/api/brief/status").json()["spent_rub"] == spent


def test_sse_streams_are_capped(settings, monkeypatch):
    monkeypatch.setattr(main, "SSE_PER_RUN", 1)
    release = threading.Event()
    replies = happy()
    replies[0] = blocking(release, replies[0])
    client, _ = make(settings, replies)
    streams = client.app.state.streams
    run_id = start(client)
    first = {}
    held = threading.Thread(target=lambda: first.update(r=client.get(f"/api/brief/runs/{run_id}/events")))
    held.start()
    wait_for(lambda: streams.open.get(run_id) == 1)
    r = client.get(f"/api/brief/runs/{run_id}/events")
    assert r.status_code == 429 and r.json()["code"] == "busy" and r.json()["retry_after_s"] == 30
    assert r.headers["connection"] == "close"
    release.set()
    held.join(5)
    assert first["r"].status_code == 200 and frames(first["r"].text)[-1][1]["type"] == "done"
    assert streams.open == {}
    assert client.get(f"/api/brief/runs/{run_id}/events").status_code == 200 and streams.open == {}


def test_streams_cap_per_run_and_in_total():
    s = Streams(per_run=2, total=3)
    assert s.acquire("a") and s.acquire("a") and not s.acquire("a")
    assert s.acquire("b") and not s.acquire("c")
    s.release("a")
    assert s.acquire("c") and s.open == {"a": 1, "b": 1, "c": 1}
    for k in ("a", "b", "c", "c"):
        s.release(k)
    assert s.open == {}


def test_sse_ends_at_the_runs_deadline_whatever_its_status(settings, monkeypatch):
    monkeypatch.setattr(main, "SSE_GRACE_S", 0.0)
    release = threading.Event()
    replies = happy()
    replies[0] = blocking(release, replies[0])
    client, _ = make(settings, replies, run_timeout_s=0.3)
    run_id = start(client)
    started = time.monotonic()
    got = frames(client.get(f"/api/brief/runs/{run_id}/events").text)
    assert time.monotonic() - started < 2 and [e["type"] for _, e in got] == ["step_start"]
    release.set()
    assert wait_done(client, run_id)["error"]["code"] == "timeout"


def test_health_purges_old_runs(settings):
    client, _ = make(settings)
    store = client.app.state.store
    old = store.clock() - 31 * 86400
    db = sqlite3.connect(store.db_path, isolation_level=None)
    db.execute("INSERT INTO runs (id, ts, updated, status, state) VALUES ('old-0123456789abcdef', ?, ?, 'done', '{}')",
               (old, old))
    store._purged_at = 0.0
    assert client.get("/api/brief/health").json() == {"ok": True}
    assert db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


def test_restart_frees_the_holds_of_dead_runs(settings):
    store = Store(settings.var_dir / "brief.sqlite3", 15, 3)
    store.hold(run_worst_rub(settings))
    store.reserve(2.0)  # a call that was in flight: it may have been charged, stays at its worst case
    client, _ = make(settings)
    assert client.get("/api/brief/status").json()["spent_rub"] == 2.0
