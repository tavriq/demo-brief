"""OpenAI-compatible gateway (Timeweb AI Gateway): chat completions, with a strict JSON schema or
plain text. Stdlib only.

No retries after a timeout: it could be billed twice. 429 is not billed: wait once and repeat
(the key is shared with other demos). 400 and 5xx are repeated once too: on 08.10 two of four
identical Haiku calls got HTTP 400 and the same request passed a minute later. The error body goes
to the log, never to the visitor. Errors never carry the key or the URL.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Protocol

from app.pricing import call_cost_rub

# Per-model request settings, checked by probes in demo-arena and demo-voice (07–08.10).
MODEL_PARAMS: dict[str, dict] = {
    "timeweb/gpt-oss-120b": {"reasoning_effort": "low"},
    "timeweb/gemma4:31b": {"reasoning_effort": "none"},
}


class GatewayError(Exception):
    """The gateway failed. ``detail`` is safe to log. Whether a failed call was billed is
    unknown (a timeout may be), so callers settle the reserved worst case."""

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


log = logging.getLogger("demo_brief")
RETRY_CODES = {400, 429, 500, 502, 503, 504, 529}

@dataclass
class Chat:
    content: str
    tokens_in: int
    tokens_out: int
    ms: int
    cost_rub: float
    finish_reason: str | None = None
    # False when the gateway returned no usage: the cost is unknown, callers keep the reserve.
    has_usage: bool = True


class ChatClient(Protocol):
    def chat(self, model: str, messages: list[dict], schema: dict | None, max_tokens: int,
             params: dict | None = None, timeout_s: float | None = None) -> Chat: ...


def _text(content) -> str:
    """Message content: a string, or a list of parts (some providers) joined."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p.get("text") or "" for p in content if isinstance(p, dict))
    return ""


@dataclass
class Gateway:
    base_url: str
    api_key: str
    timeout_s: float = 60.0
    wait_429_s: float = 2.0
    wait_retry_s: float = 1.0

    def _post(self, path: str, data: bytes, timeout_s: float) -> tuple[int, bytes, int]:
        def once() -> tuple[int, bytes]:
            req = urllib.request.Request(self.base_url.rstrip("/") + "/" + path, data=data,
                                         headers={"Authorization": "Bearer " + self.api_key,
                                                  "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=timeout_s) as r:
                    return 200, r.read()
            except urllib.error.HTTPError as e:
                return e.code, e.read()[:300]
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                raise GatewayError(type(e).__name__) from None

        started = time.perf_counter()
        code, body = once()
        if code in RETRY_CODES:
            log.warning("gateway HTTP %s, one retry: %s", code, body[:200].decode("utf-8", "replace"))
            time.sleep(self.wait_429_s if code == 429 else self.wait_retry_s)
            started = time.perf_counter()  # the wait is the gateway's queue, not the model's time
            code, body = once()
        if code != 200:
            log.warning("gateway HTTP %s: %s", code, body[:200].decode("utf-8", "replace"))
        return code, body, int((time.perf_counter() - started) * 1000)

    def chat(self, model: str, messages: list[dict], schema: dict | None, max_tokens: int,
             params: dict | None = None, timeout_s: float | None = None) -> Chat:
        """``schema`` None: a plain text answer (no response_format)."""
        payload = {"model": model, "messages": messages, "max_completion_tokens": max_tokens, "temperature": 0,
                   **(params or {})}
        if schema is not None:
            payload["response_format"] = {"type": "json_schema", "json_schema": schema}
        code, body, ms = self._post("chat/completions", json.dumps(payload).encode(),
                                    self.timeout_s if timeout_s is None else timeout_s)
        if code != 200:
            raise GatewayError(f"chat HTTP {code}")
        try:
            data = json.loads(body)
            choice = data["choices"][0]
            content = _text(choice["message"].get("content"))
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise GatewayError("chat: unexpected response") from None
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        tin, tout = int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)
        return Chat(content, tin, tout, ms, call_cost_rub(model, tin, tout), choice.get("finish_reason"),
                    bool(tin or tout))
