"""Unit tests for the CDP session demultiplexer (no real WebSocket)."""

import json

import pytest

from harness_android.browser import _CDPSession, TargetCrashed


class FakeWS:
    """Stand-in for ``websocket.WebSocket`` driven by a script of frames."""

    def __init__(self, frames):
        self._frames = list(frames)
        self.sent = []
        self.timeout = None

    def send(self, data):
        self.sent.append(json.loads(data))

    def recv(self):
        if not self._frames:
            # _CDPSession treats empty/None recv as nothing to do
            return ""
        return json.dumps(self._frames.pop(0))

    def settimeout(self, t):
        self.timeout = t

    def close(self):
        pass


def make_session(frames):
    s = _CDPSession(url="ws://fake")
    s._ws = FakeWS(frames)
    return s


def test_send_buffers_interleaved_events():
    s = make_session([
        {"method": "Page.frameStartedLoading", "params": {}},
        {"method": "Page.loadEventFired", "params": {"timestamp": 1.0}},
        {"id": 1, "result": {"value": 42}},
    ])
    out = s.send("Runtime.evaluate", {"expression": "1"})
    assert out == {"value": 42}
    assert s._ws.sent[0]["method"] == "Runtime.evaluate"
    assert s._ws.sent[0]["id"] == 1
    # Both events that arrived before the response are buffered, not lost.
    buffered = s.drain_events()
    assert [e["method"] for e in buffered] == [
        "Page.frameStartedLoading",
        "Page.loadEventFired",
    ]


def test_send_raises_on_cdp_error():
    s = make_session([{"id": 1, "error": {"code": -32000, "message": "nope"}}])
    with pytest.raises(RuntimeError, match="nope"):
        s.send("Page.enable")


def test_send_raises_target_crashed():
    s = make_session([
        {"method": "Inspector.targetCrashed", "params": {}},
    ])
    with pytest.raises(TargetCrashed):
        s.send("Runtime.evaluate", timeout=1)
    assert s._crashed is True


def test_wait_event_consumes_from_buffer_first():
    s = make_session([])
    s._events.append({"method": "Page.loadEventFired", "params": {"t": 1}})
    s._events.append({"method": "Other.thing", "params": {}})
    ev = s.wait_event("Page.loadEventFired")
    assert ev["params"]["t"] == 1
    # Unmatched event still in buffer.
    assert len(s._events) == 1
    assert s._events[0]["method"] == "Other.thing"


def test_wait_event_reads_socket_when_buffer_empty():
    s = make_session([
        {"method": "Network.requestWillBeSent", "params": {}},
        {"method": "Page.loadEventFired", "params": {"t": 2}},
    ])
    ev = s.wait_event("Page.loadEventFired", timeout=2)
    assert ev["params"]["t"] == 2
    # The non-matching event was buffered while waiting.
    assert [e["method"] for e in s._events] == ["Network.requestWillBeSent"]


def test_wait_event_with_predicate():
    s = make_session([])
    s._events.append({"method": "Tracing.dataCollected", "params": {"value": [1]}})
    s._events.append({"method": "Tracing.dataCollected", "params": {"value": [2]}})
    ev = s.wait_event(lambda m: m.get("method") == "Tracing.dataCollected"
                      and m["params"]["value"] == [2])
    assert ev["params"]["value"] == [2]
    assert len(s._events) == 1


def test_drain_events_filters_by_method():
    s = make_session([])
    s._events.extend([
        {"method": "A", "params": {}},
        {"method": "B", "params": {}},
        {"method": "A", "params": {}},
    ])
    out = s.drain_events("A")
    assert len(out) == 2
    assert all(e["method"] == "A" for e in out)
    # B remains
    assert [e["method"] for e in s._events] == ["B"]


def test_send_ignores_stale_response_id():
    s = make_session([
        {"id": 99, "result": {"stale": True}},
        {"id": 1, "result": {"ok": True}},
    ])
    out = s.send("Page.enable")
    assert out == {"ok": True}
