import time

import pytest

from harness_android.adb import poll_until, _INPUT_TEXT_ESCAPE


def test_poll_until_returns_first_truthy():
    calls = []

    def fn():
        calls.append(1)
        return "ok" if len(calls) >= 3 else None

    t0 = time.monotonic()
    out = poll_until(fn, timeout=2, interval=0.01)
    assert out == "ok"
    assert len(calls) == 3
    assert time.monotonic() - t0 < 1.0


def test_poll_until_swallows_exceptions_then_succeeds():
    state = {"n": 0}

    def fn():
        state["n"] += 1
        if state["n"] < 3:
            raise OSError("not yet")
        return state["n"]

    assert poll_until(fn, timeout=2, interval=0.01) == 3


def test_poll_until_timeout_includes_last_error():
    def fn():
        raise ValueError("connection refused")

    with pytest.raises(TimeoutError) as exc:
        poll_until(fn, timeout=0.1, interval=0.02, desc="devtools")
    msg = str(exc.value)
    assert "devtools" in msg
    assert "connection refused" in msg


def test_poll_until_timeout_on_falsy():
    with pytest.raises(TimeoutError):
        poll_until(lambda: None, timeout=0.1, interval=0.02, desc="never")


@pytest.mark.parametrize(
    "raw,expect",
    [
        ("hello", "hello"),
        ("a b", r"a\ b"),
        ("a;b", r"a\;b"),
        ("$(rm -rf)", r"\$\(rm\ -rf\)"),
        ("a&b|c", r"a\&b\|c"),
        ("'\"", "\\'\\\""),
        ("`id`", r"\`id\`"),
    ],
)
def test_input_text_escape_regex(raw, expect):
    assert _INPUT_TEXT_ESCAPE.sub(r"\\\1", raw) == expect
