"""Unit tests for the WebView enumerator.

The live harness tests (`smoke_live.py`) cover the happy path against a
real emulator. These pure-Python tests pin down the pieces that have
been bitten by regressions:

* the socket-name regex accepts the Chrome-style sockets we care about
  and rejects noise like ``@WebViewZygoteInit``;
* ``enumerate_webviews`` respects the ``default_chrome_package`` hint
  instead of hard-coding ``com.android.chrome`` for every PID-less
  ``chrome_devtools_remote`` row (regression surfaced when driving
  Edge — the PID-less row was always reported as Chrome).
"""

from __future__ import annotations

from harness_android import webview as wv


# ---------------------------------------------------------------------
# Socket-name regex
# ---------------------------------------------------------------------

def test_socket_regex_matches_chrome_socket() -> None:
    m = wv._SOCKET_RE.match("chrome_devtools_remote")
    assert m is not None
    assert m.group(1) == "chrome_devtools_remote"
    assert m.group(2) is None  # no pid suffix


def test_socket_regex_extracts_pid_from_webview_socket() -> None:
    m = wv._SOCKET_RE.match("webview_devtools_remote_12345")
    assert m is not None
    assert m.group(2) == "12345"


def test_socket_regex_matches_package_prefixed_devtools_remote() -> None:
    # Some apps expose `com.example.myapp_devtools_remote` sockets.
    m = wv._SOCKET_RE.match("com.example.myapp_devtools_remote")
    assert m is not None


def test_socket_regex_rejects_non_devtools() -> None:
    assert wv._SOCKET_RE.match("WebViewZygoteInit") is None
    assert wv._SOCKET_RE.match("some_random_socket") is None


# ---------------------------------------------------------------------
# enumerate_webviews() fake-adb smoke test
# ---------------------------------------------------------------------

class _FakeProc:
    def __init__(self, stdout: str = "") -> None:
        self.stdout = stdout


class _FakeADB:
    """Minimal ADB stand-in: answers ``list_abstract_sockets`` and
    ``run`` the way the real class does for the subset we need."""

    def __init__(self, sockets: list[str], cmdlines: dict[int, str] | None = None) -> None:
        self._sockets = sockets
        self._cmdlines = cmdlines or {}

    def list_abstract_sockets(self, needle: str = "") -> list[str]:
        return [s for s in self._sockets if needle in s]

    def run(self, *args: str, check: bool = True, timeout: float = 0) -> _FakeProc:  # noqa: ARG002
        # Only /proc/<pid>/cmdline reads are exercised here.
        for a in args:
            if a.startswith("/proc/"):
                pid = int(a.split("/")[2])
                return _FakeProc(stdout=self._cmdlines.get(pid, ""))
        return _FakeProc()


def test_enumerate_picks_up_pid_and_package_from_cmdline() -> None:
    adb = _FakeADB(
        sockets=["webview_devtools_remote_1702"],
        cmdlines={1702: "com.example.app\x00--some-arg"},
    )
    targets = wv.enumerate_webviews(adb)
    assert len(targets) == 1
    t = targets[0]
    assert t.socket_name == "webview_devtools_remote_1702"
    assert t.pid == 1702
    assert t.package == "com.example.app"


def test_enumerate_uses_default_package_for_pidless_chrome_socket() -> None:
    # Driving Edge: the chrome_devtools_remote row in /proc/net/unix has
    # inode 0 (PID unavailable). Historically the enumerator hard-coded
    # `com.android.chrome` here, giving a misleading report for Edge.
    adb = _FakeADB(sockets=["chrome_devtools_remote"])
    targets = wv.enumerate_webviews(
        adb, default_chrome_package="com.microsoft.emmx.local",
    )
    assert len(targets) == 1
    assert targets[0].pid == 0
    assert targets[0].package == "com.microsoft.emmx.local"


def test_enumerate_pidless_chrome_socket_no_hint_leaves_package_blank() -> None:
    adb = _FakeADB(sockets=["chrome_devtools_remote"])
    targets = wv.enumerate_webviews(adb)
    assert len(targets) == 1
    assert targets[0].package == ""


def test_enumerate_deduplicates_identical_sockets() -> None:
    adb = _FakeADB(sockets=[
        "chrome_devtools_remote",
        "chrome_devtools_remote",  # appears twice in /proc/net/unix
        "webview_devtools_remote_1",
    ])
    targets = wv.enumerate_webviews(adb)
    assert len(targets) == 2
    assert {t.socket_name for t in targets} == {
        "chrome_devtools_remote", "webview_devtools_remote_1",
    }
