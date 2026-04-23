import time

import pytest

from harness_android.adb import ADB, poll_until, _INPUT_TEXT_ESCAPE, _SAFE_REMOTE_PATH_RE


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


# ---------------------------------------------------------------------
# _SAFE_REMOTE_PATH_RE / ADB.write_file — guard against shell injection
# when the device-side redirection path is interpolated into ``sh -c``.
# ---------------------------------------------------------------------

@pytest.mark.parametrize(
    "path",
    [
        "/data/local/tmp/chrome-command-line",
        "/data/local/tmp/com.microsoft.emmx.local-command-line",
        "/sdcard/evidence.png",
        "/system/etc/security/cacerts/abc12345.0",
        "relative/path.txt",
        "-with-dash",
        "@special@",
    ],
)
def test_safe_remote_path_accepts_harness_paths(path: str) -> None:
    assert _SAFE_REMOTE_PATH_RE.fullmatch(path) is not None


@pytest.mark.parametrize(
    "path",
    [
        "/data/local/tmp/foo; rm -rf /",
        "/data/local/tmp/$(echo pwn)",
        "/data/local/tmp/`whoami`",
        "/data/local/tmp/foo'bar",
        "/data/local/tmp/foo\"bar",
        "/data/local/tmp/foo bar",   # plain space
        "/data/local/tmp/foo|tee",
        "/data/local/tmp/foo>bar",
        "/data/local/tmp/foo\nbar",  # newline
    ],
)
def test_safe_remote_path_rejects_shell_metacharacters(path: str) -> None:
    assert _SAFE_REMOTE_PATH_RE.fullmatch(path) is None


def test_write_file_rejects_unsafe_path_with_value_error() -> None:
    """End-to-end guard: unsafe path must raise ``ValueError`` before
    anything is handed to subprocess / ``sh -c``."""
    adb = ADB.__new__(ADB)  # skip constructor (no real device needed)
    with pytest.raises(ValueError, match="refusing unsafe remote path"):
        adb.write_file("/data/local/tmp/evil; rm -rf /", b"payload")
