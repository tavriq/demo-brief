import io
import json
import urllib.error

import pytest

from app import gateway
from app.gateway import Gateway, GatewayError
from app.schema import PARSE


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def answer(content="{}", usage=None, finish="stop"):
    body = {"choices": [{"message": {"content": content}, "finish_reason": finish}]}
    if usage is not None:
        body["usage"] = usage
    return FakeResponse(json.dumps(body).encode())


@pytest.fixture
def sent(monkeypatch):
    calls, replies = [], []

    def urlopen(req, timeout):
        calls.append({"url": req.full_url, "body": json.loads(req.data), "timeout": timeout,
                      "auth": req.get_header("Authorization")})
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(gateway.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(gateway.time, "sleep", lambda s: None)
    return calls, replies


def test_json_schema_and_usage(sent):
    calls, replies = sent
    replies.append(answer('{"niche": "x"}', {"prompt_tokens": 1000, "completion_tokens": 100}))
    chat = Gateway("http://gw.example/v1/", "secret").chat("gemini/gemini-3.1-flash-lite",
                                                          [{"role": "user", "content": "б"}], PARSE, 1200,
                                                          timeout_s=40)
    body = calls[0]["body"]
    assert calls[0]["url"] == "http://gw.example/v1/chat/completions" and calls[0]["timeout"] == 40
    assert body["response_format"] == {"type": "json_schema", "json_schema": PARSE}
    assert body["max_completion_tokens"] == 1200 and body["temperature"] == 0
    assert chat.content == '{"niche": "x"}' and chat.finish_reason == "stop" and chat.has_usage
    assert chat.cost_rub == pytest.approx((1000 * 34 + 100 * 203) / 1e6)


def test_plain_text_has_no_response_format_and_no_usage(sent):
    calls, replies = sent
    replies.append(answer("<html></html>", finish="length"))
    chat = Gateway("http://gw.example", "k", timeout_s=7).chat("m", [], None, 14000)
    assert "response_format" not in calls[0]["body"] and calls[0]["timeout"] == 7
    assert chat.finish_reason == "length" and not chat.has_usage and chat.cost_rub == 0


def test_one_wait_after_429_then_errors_without_secrets(sent):
    calls, replies = sent
    busy = urllib.error.HTTPError("http://gw.example", 429, "busy", {}, io.BytesIO(b"slow down"))
    replies += [busy, answer()]
    Gateway("http://gw.example", "k").chat("m", [], None, 10)
    assert len(calls) == 2
    failed = len(calls)
    replies += [urllib.error.HTTPError("http://gw.example", 500, "x", {}, io.BytesIO(b"secret detail"))] * 2
    with pytest.raises(GatewayError) as e:
        Gateway("http://gw.example", "secret").chat("m", [], None, 10)
    assert e.value.detail == "chat HTTP 500" and "secret" not in str(e.value) and len(calls) == failed + 2
    replies.append(TimeoutError())
    with pytest.raises(GatewayError, match="TimeoutError"):
        Gateway("http://gw.example", "k").chat("m", [], None, 10)
    replies.append(FakeResponse(b"not json"))
    with pytest.raises(GatewayError, match="unexpected"):
        Gateway("http://gw.example", "k").chat("m", [], None, 10)


def test_transient_400_is_repeated_once(sent):
    # 08.10: Haiku got HTTP 400 on 2 of 4 identical calls, the same request passed a minute later
    calls, replies = sent
    replies += [urllib.error.HTTPError("http://gw.example", 400, "bad", {}, io.BytesIO(b"upstream")), answer()]
    Gateway("http://gw.example", "k").chat("m", [], None, 10)
    assert len(calls) == 2
    replies += [urllib.error.HTTPError("http://gw.example", 401, "no", {}, io.BytesIO(b"key"))]
    with pytest.raises(GatewayError, match="HTTP 401"):
        Gateway("http://gw.example", "k").chat("m", [], None, 10)
    assert len(calls) == 3
