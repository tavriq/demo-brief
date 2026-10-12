"""FastAPI app. Run: uvicorn --factory app.main:create_app --no-proxy-headers

``--no-proxy-headers`` matters: the client address (and X-Forwarded-For trust) is resolved by
app.netutil according to TRUSTED_PROXY. Contract: docs/api.md, docs/SPEC.md.

A run goes to a thread pool of MAX_CONCURRENT_RUNS (app.pipeline) with the worst case of its
main pass held in the budget from admission; the page follows it by SSE, which reads the run's
events from SQLite every SSE_POLL_S. Open streams are capped (Streams) and end at the run's
deadline whatever its status: each one holds a connection, and uvicorn answers 503 to
everything past --limit-concurrency. Prototypes are served with a CSP sandbox without network
and only into a frame (Sec-Fetch-Dest); the page shows them in ``<iframe sandbox="allow-scripts">``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import AsyncIterator, Awaitable, Callable

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import MutableHeaders

from app.config import Settings
from app.gateway import ChatClient, Gateway
from app.netutil import client_ip, rate_limit_key
from app.pii import mask_pii
from app.pipeline import Deps, initial_state, run
from app.pricing import run_worst_rub
from app.store import FINAL_EVENTS, Store

log = logging.getLogger("demo_brief")
MAX_BODY_BYTES = 16 * 1024
SSE_POLL_S = 0.3
SSE_HEARTBEAT_S = 15.0
SSE_PER_RUN = 3     # a page needs one; a reload or a second tab may hold another for a moment
SSE_TOTAL = 12      # well below uvicorn's --limit-concurrency 32 (Dockerfile)
SSE_GRACE_S = 30.0  # a stream ends this long after the run's own time limit, whatever its status
BUSY_RETRY_S = 30

SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}
# The prototype is model-written HTML: scripts run, but in a sandbox without same-origin and network.
PROTOTYPE_CSP = ("sandbox allow-scripts; default-src 'none'; script-src 'unsafe-inline'; "
                 "style-src 'unsafe-inline'; img-src data:; font-src data:; base-uri 'none'; "
                 "form-action 'none'; frame-ancestors 'self'")
PROTOTYPE_HEADERS = {
    "Content-Security-Policy": PROTOTYPE_CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Vary": "Sec-Fetch-Dest",  # a cached copy must not open as a page either
}
EXAMPLES_CACHE = "public, max-age=300"

RUN_ID = re.compile(r"[A-Za-z0-9_-]{16,64}")
EXAMPLE_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}")


def _error(status: int, code: str, message: str, **extra) -> JSONResponse:
    return JSONResponse({"code": code, "message": message, **extra}, status_code=status)


class Guard:
    """Plain ASGI middleware (keeps SSE streaming intact): a body size limit and the security
    headers on every response; headers a route sets itself win."""

    def __init__(self, app, max_body: int = MAX_BODY_BYTES):
        self.app = app
        self.max_body = max_body

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for k, v in SECURITY_HEADERS.items():
                    headers.setdefault(k, v)
            await send(message)

        length = dict(scope.get("headers") or []).get(b"content-length", b"")
        if length.isdigit() and int(length) > self.max_body:
            await _error(413, "too_long", "Слишком большой запрос.")(scope, receive, send_with_headers)
            return
        await self.app(scope, receive, send_with_headers)


class Slots:
    """At most ``limit`` runs at once for the whole service (one worker process)."""

    def __init__(self, limit: int):
        self.limit = limit
        self.busy = 0
        self.lock = threading.Lock()

    def acquire(self) -> bool:
        with self.lock:
            if self.busy >= self.limit:
                return False
            self.busy += 1
            return True

    def release(self) -> None:
        with self.lock:
            self.busy = max(0, self.busy - 1)


class Streams:
    """Open SSE streams: at most ``per_run`` per run and ``total`` in all (one worker process)."""

    def __init__(self, per_run: int, total: int):
        self.per_run = per_run
        self.total = total
        self.open: dict[str, int] = {}
        self.lock = threading.Lock()

    def acquire(self, run_id: str) -> bool:
        with self.lock:
            if sum(self.open.values()) >= self.total or self.open.get(run_id, 0) >= self.per_run:
                return False
            self.open[run_id] = self.open.get(run_id, 0) + 1
            return True

    def release(self, run_id: str) -> None:
        with self.lock:
            left = self.open.get(run_id, 0) - 1
            if left > 0:
                self.open[run_id] = left
            else:
                self.open.pop(run_id, None)


class HeldStream(StreamingResponse):
    """A streaming response that calls ``on_close`` once it is over, however it ends."""

    def __init__(self, content, on_close: Callable[[], None], **kwargs):
        super().__init__(content, **kwargs)
        self.on_close = on_close

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.on_close()


def load_examples(directory: Path) -> dict[str, dict]:
    """Recorded runs ``<name>.json`` (scripts/record.py). Bad names and broken files are skipped."""
    out = {}
    for path in sorted(directory.glob("*.json")) if directory.is_dir() else []:
        name = path.stem
        if not EXAMPLE_NAME.fullmatch(name):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.warning("recorded example %s is unreadable", path.name)
            continue
        if not isinstance(data, dict) or not isinstance(data.get("state"), dict):
            continue
        state = dict(data["state"])
        if data.get("prototype_html") and isinstance(state.get("prototype"), dict):
            state["prototype"] = {**state["prototype"], "url": f"/api/brief/examples/{name}/prototype"}
        out[name] = {"name": name, "title": str(data.get("title") or name), "brief": str(data.get("brief") or ""),
                     "state": state, "events": data.get("events") if isinstance(data.get("events"), list) else [],
                     "prototype_html": data.get("prototype_html") if isinstance(data.get("prototype_html"), str)
                     else None}
    return out


def sse(event: dict) -> str:
    return f"id: {event['seq']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"


async def event_stream(store: Store, run_id: str, after: int, disconnected: Callable[[], Awaitable[bool]],
                       max_s: float = float("inf")) -> AsyncIterator[str]:
    """Events with seq > ``after`` as SSE, polled every SSE_POLL_S, a comment every SSE_HEARTBEAT_S
    while nothing happens. Ends after "done"/"failed" (also when the client already has it), when
    the run is gone, the client disconnects or ``max_s`` have passed (the browser reconnects
    with Last-Event-ID if it still wants more)."""
    last, quiet_since = after, time.monotonic()
    deadline = quiet_since + max_s
    while time.monotonic() < deadline and not await disconnected():
        events = await run_in_threadpool(store.events_after, run_id, last)
        for event in events:
            last = event["seq"]
            yield sse(event)
            if event["type"] in FINAL_EVENTS:
                return
        if events:
            quiet_since = time.monotonic()
        else:
            final = await run_in_threadpool(store.final_seq, run_id)
            if final is not None and final <= last:
                return
            if final is None and await run_in_threadpool(store.run_status, run_id) is None:
                return
            if time.monotonic() - quiet_since >= SSE_HEARTBEAT_S:
                quiet_since = time.monotonic()
                yield ": ping\n\n"
        await asyncio.sleep(SSE_POLL_S)


def _last_event_id(raw: str | None) -> int:
    raw = (raw or "").strip()
    return int(raw) if raw.isdigit() and len(raw) < 10 else 0


def _prototype(html: str, cache: str) -> Response:
    return Response(html, media_type="text/html", headers={**PROTOTYPE_HEADERS, "Cache-Control": cache})


def _into_frame(request: Request) -> bool:
    """A prototype is model-written HTML with scripts: it opens only inside a frame. A browser that
    names the destination must say "iframe"; as a top-level page on the site's address it could
    pose as the site and send what the visitor types elsewhere by navigating itself."""
    dest = request.headers.get("sec-fetch-dest")
    return dest is None or dest == "iframe"


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    brief: str


def create_app(settings: Settings | None = None, client: ChatClient | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    store = Store(settings.var_dir / "brief.sqlite3", settings.daily_budget_rub, settings.runs_per_hour,
                  settings.retention_days)
    stale = store.fail_stale_runs()
    if stale:
        log.warning("marked %s runs left from a previous process as failed", stale)
    store.release_holds()  # one worker (Dockerfile): no live run holds anything yet
    if client is None and settings.live:
        client = Gateway(settings.base_url, settings.api_key)
    deps = Deps(client, store, settings)
    runner = ThreadPoolExecutor(max_workers=max(1, settings.max_concurrent_runs), thread_name_prefix="run")
    slots = Slots(settings.max_concurrent_runs)
    streams = Streams(SSE_PER_RUN, SSE_TOTAL)
    worst = run_worst_rub(settings)
    examples = load_examples(settings.recorded_dir)
    bad_brief = f"Нужен бриф от 1 до {settings.max_brief_chars} знаков."

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(Guard)
    app.state.store, app.state.streams = store, streams  # for tests and scripts

    @app.exception_handler(RequestValidationError)
    async def bad_request(_request: Request, _exc: RequestValidationError):
        return _error(422, "bad_request", bad_brief)

    def visitor_ip(request: Request) -> str:
        return client_ip(request.client.host if request.client else None, request.headers.get("x-forwarded-for"),
                         settings.trusted_proxies)

    def not_found() -> JSONResponse:
        return _error(404, "not_found", "Прогон не найден: он старше 30 суток или ссылка неверная.")

    def no_prototype() -> JSONResponse:
        return _error(404, "not_found", "Прототипа нет.")

    @app.get("/api/brief/health")
    def health():
        try:
            store.maybe_purge()  # hourly at most; health is what calls it when nobody visits
        except sqlite3.Error:
            log.exception("purge failed")
        return {"ok": True}

    @app.get("/api/brief/status")
    def status():
        store.maybe_purge()
        spent = store.spent_today()
        return {"live": client is not None and spent + worst <= settings.daily_budget_rub,
                "budget_rub": settings.daily_budget_rub, "spent_rub": round(spent, 2),
                "run_worst_rub": round(worst, 2), "runs_running": slots.busy, "models": settings.models(),
                "counters": store.counters()}

    @app.post("/api/brief/runs")
    def create_run(req: RunRequest, request: Request):
        brief = req.brief.strip()
        if not brief or len(brief) > settings.max_brief_chars:
            return _error(422, "bad_request", bad_brief)
        if client is None:
            return _error(503, "offline", "Живой режим выключен. Записанные примеры работают.")
        if not slots.acquire():
            return _error(429, "busy", f"Сервис занят: одновременно идёт не больше {settings.max_concurrent_runs} "
                                       "прогонов. Попробуйте через минуту или посмотрите записанный пример.",
                          retry_after_s=BUSY_RETRY_S)
        submitted, hold = False, None
        try:
            # the run's main pass is held now, atomically: two runs never start on money for one
            hold = store.hold(worst)
            if not hold.allowed:
                return _error(503, "budget_exhausted", store.budget_message())
            decision = store.start(rate_limit_key(visitor_ip(request)))
            if not decision.allowed:
                return _error(429, decision.code, decision.message, retry_after_s=decision.retry_after_s)
            run_id = secrets.token_urlsafe(16)
            masked = mask_pii(brief)[0]
            store.create_run(run_id, initial_state(run_id, masked, store.clock()))
            future = runner.submit(run, run_id, masked, deps, hold.reservation_id)
            submitted = True
            future.add_done_callback(lambda _f: slots.release())
        finally:
            if not submitted:
                slots.release()
                if hold is not None and hold.allowed:
                    store.settle(hold.reservation_id, 0.0)
        log.info("run=%s started chars=%s", run_id[:6], len(masked))
        return {"id": run_id}

    @app.get("/api/brief/runs/{run_id}")
    def get_run(run_id: str):
        state = store.load_run(run_id) if RUN_ID.fullmatch(run_id) else None
        return state if state is not None else not_found()

    @app.get("/api/brief/runs/{run_id}/events")
    async def events(run_id: str, request: Request):
        if not RUN_ID.fullmatch(run_id) or await run_in_threadpool(store.run_status, run_id) is None:
            return not_found()
        if not streams.acquire(run_id):
            refused = _error(429, "busy", "Открыто слишком много потоков событий. Ход прогона виден и без них, "
                                          "страница переходит на опрос.", retry_after_s=BUSY_RETRY_S)
            refused.headers["Connection"] = "close"  # a kept-alive socket would still count toward the limit
            return refused
        after = _last_event_id(request.headers.get("last-event-id"))
        stream = event_stream(store, run_id, after, request.is_disconnected, settings.run_timeout_s + SSE_GRACE_S)
        return HeldStream(stream, lambda: streams.release(run_id), media_type="text/event-stream",
                          headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    @app.get("/api/brief/runs/{run_id}/prototype")
    def run_prototype(run_id: str, request: Request):
        html = store.load_prototype(run_id) if RUN_ID.fullmatch(run_id) and _into_frame(request) else None
        return _prototype(html, "no-store") if html is not None else no_prototype()

    @app.get("/api/brief/examples")
    def list_examples():
        rows = [{"name": e["name"], "title": e["title"], "brief": e["brief"]} for e in examples.values()]
        return JSONResponse(rows, headers={"Cache-Control": EXAMPLES_CACHE})

    def example(name: str) -> dict | None:
        return examples.get(name) if EXAMPLE_NAME.fullmatch(name) else None

    @app.get("/api/brief/examples/{name}")
    def get_example(name: str):
        e = example(name)
        if e is None:
            return _error(404, "not_found", "Нет такого примера.")
        return JSONResponse({k: e[k] for k in ("name", "title", "brief", "state", "events")},
                            headers={"Cache-Control": EXAMPLES_CACHE})

    @app.get("/api/brief/examples/{name}/prototype")
    def example_prototype(name: str, request: Request):
        e = example(name)
        if e is None or not e["prototype_html"] or not _into_frame(request):
            return no_prototype()
        return _prototype(e["prototype_html"], EXAMPLES_CACHE)

    return app
