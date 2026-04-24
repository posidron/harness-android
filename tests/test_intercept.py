"""Tests for harness_android.intercept.

These tests exercise the request/response dispatch logic against a fake
Browser so we don't need a live emulator.  The critical regression they
lock down is that response-stage fall-through must emit
``Fetch.continueResponse`` \u2014 not ``Fetch.continueRequest``, which CDP
rejects with "Invalid InterceptionId" and hangs the response.
"""

from __future__ import annotations

import pytest

from harness_android.intercept import Interceptor, InterceptedRequest


class _FakeBrowser:
    def __init__(self):
        self.sent: list[tuple[str, dict]] = []

    def send(self, method, params=None):
        self.sent.append((method, params or {}))
        # getResponseBody is the only call that expects a payload back.
        if method == "Fetch.getResponseBody":
            return {"body": "aGVsbG8=", "base64Encoded": True}  # b"hello"
        return {}


def _response_params(request_id="R1", url="https://example.test/", status=200):
    return {
        "requestId": request_id,
        "request": {"url": url, "method": "GET", "headers": {}},
        "responseStatusCode": status,
        "responseHeaders": [{"name": "content-type", "value": "text/plain"}],
        "resourceType": "Document",
    }


# ----------------------------------------------------------------------
# Response-stage fall-through
# ----------------------------------------------------------------------

def test_response_stage_without_handler_uses_continueResponse():
    """No matching response handler must release via Fetch.continueResponse.

    Regression: the fall-through previously sent Fetch.continueRequest, which
    is only valid at Request stage.  At Response stage CDP returns
    "Invalid InterceptionId" and the page hangs.
    """
    b = _FakeBrowser()
    it = Interceptor(b)
    it._handle_request_paused(_response_params())

    methods = [m for m, _ in b.sent]
    # No response handler registered \u2192 we should NOT have fetched the body.
    assert "Fetch.getResponseBody" not in methods
    assert methods[-1] == "Fetch.continueResponse"
    assert "Fetch.continueRequest" not in methods


def test_response_stage_body_fetched_only_when_handler_registered():
    """getResponseBody is expensive; skip it if no one will read it."""
    b = _FakeBrowser()
    it = Interceptor(b)

    @it.on_response("*")
    def spy(req):
        return None  # log-only

    it._handle_request_paused(_response_params())
    methods = [m for m, _ in b.sent]
    assert methods[0] == "Fetch.getResponseBody"
    assert methods[-1] == "Fetch.continueResponse"


def test_response_handler_can_fulfill_with_modified_body():
    b = _FakeBrowser()
    it = Interceptor(b)

    @it.on_response("*")
    def patch(req):
        return {"status": 200, "body": b"patched"}

    it._handle_request_paused(_response_params())
    methods = [m for m, _ in b.sent]
    assert "Fetch.fulfillRequest" in methods
    # Fulfilling short-circuits the fall-through continueResponse.
    assert methods.count("Fetch.continueResponse") == 0


# ----------------------------------------------------------------------
# Background-mode is refused until the browser has a dispatcher
# ----------------------------------------------------------------------

def test_background_start_is_refused_loudly():
    """Running a listener thread while main-thread browser.send() is in flight
    races ws.recv() between threads.  Until the Browser grows a dispatcher
    / lock, background=True must fail fast instead of silently corrupting."""
    b = _FakeBrowser()
    it = Interceptor(b)  # type: ignore[arg-type]
    with pytest.raises(NotImplementedError, match="background=True"):
        it.start(background=True)
