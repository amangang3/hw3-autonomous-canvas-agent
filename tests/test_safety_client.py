from __future__ import annotations

import io
import json
import urllib.error

import pytest

from canvas_tool.client import CanvasClient, CanvasError
from canvas_tool.safety import filter_message, normalize_text, redact, strip_html


def test_html_is_stripped_and_normalized():
    assert strip_html("<p>Hello&nbsp;<b>world</b></p><p>next</p>") == "Hello world\nnext"
    assert normalize_text("  café\r\n\r\n next  ") == "café\nnext"


@pytest.mark.parametrize("text", [
    "Short", "x" * 2001,
    "See https://example.com for details " + "x" * 30,
    "Write me at person@example.com " + "x" * 30,
    "Call (617) 555-1212 today " + "x" * 30,
    "The password is definitely secret " + "x" * 30,
    "My API key is abc " + "x" * 30,
    "Here is token 12345678901234567890 " + "x" * 30,
    "Your grade is private " + "x" * 30,
    "123~" + "A" * 26 + " " + "x" * 30,
])
def test_filter_rejects_unsafe_content(text):
    ok, _ = filter_message(text)
    assert not ok


def test_filter_accepts_safe_content():
    ok, reason = filter_message("Intent journals prevent blind retries after an uncertain network result.")
    assert ok and reason is None


def test_redaction_covers_actual_and_token_shaped_values():
    token_shaped = "12~" + "A" * 26
    assert "secret" not in redact(f"Bearer secret and {token_shaped}", "secret")
    assert "[REDACTED]" in redact(f"Bearer secret and {token_shaped}", "secret")


class Response:
    def __init__(self, body, status=200, headers=None):
        self.body = body
        self.status = status
        self.headers = headers or {}
    def read(self): return self.body
    def __enter__(self): return self
    def __exit__(self, *args): pass


def test_get_retries_transient_failure_and_uses_timeout():
    calls = []
    def opener(req, timeout):
        calls.append((req.full_url, timeout))
        if len(calls) == 1:
            raise urllib.error.URLError("temporary")
        return Response(json.dumps({"id": 7}).encode())
    sleeps = []
    client = CanvasClient("secret", opener=opener, sleep=sleeps.append, jitter=lambda: 0)
    assert client.get_json("/api/v1/users/self") == {"id": 7}
    assert len(calls) == 2 and all(c[1] == 20 for c in calls)
    assert sleeps == [1]


def test_post_is_never_retried():
    calls = []
    def opener(req, timeout):
        calls.append(req.method)
        raise urllib.error.URLError("lost ack")
    client = CanvasClient("secret", opener=opener, sleep=lambda _: None)
    with pytest.raises(CanvasError):
        client.post_json("/api/v1/courses/1/discussion_topics/2/entries", {"message": "x"})
    assert calls == ["POST"]


def test_off_origin_pagination_is_rejected():
    def opener(req, timeout):
        return Response(b"[]", headers={"Link": '<https://evil.test/page=2>; rel="next"'})
    client = CanvasClient("secret", opener=opener)
    with pytest.raises(CanvasError, match="pagination"):
        client.get_paginated("/api/v1/courses")


def test_paginated_get_does_not_retry_404():
    calls = []
    def opener(req, timeout):
        calls.append(req.full_url)
        raise urllib.error.HTTPError(req.full_url, 404, "missing", {}, None)
    client = CanvasClient("secret", opener=opener, sleep=lambda _: None)
    with pytest.raises(CanvasError):
        client.get_paginated("/api/v1/courses")
    assert len(calls) == 1


def test_paginated_get_honors_retry_after():
    calls = []
    sleeps = []
    def opener(req, timeout):
        calls.append(req.full_url)
        if len(calls) == 1:
            raise urllib.error.HTTPError(req.full_url, 429, "slow", {"Retry-After": "19"}, None)
        return Response(b"[]")
    client = CanvasClient("secret", opener=opener, sleep=sleeps.append)
    assert client.get_paginated("/api/v1/courses") == []
    assert sleeps == [19]
