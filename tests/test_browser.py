"""Tests for Browser high-level routing and BROWSERS presets."""

import pytest

from harness_android import browser as browser_mod
from harness_android.browser import Browser, BROWSERS, BrowserSpec


def test_browser_presets():
    assert set(BROWSERS) >= {"chrome", "chromium", "edge", "edge-canary", "edge-dev"}
    edge = BROWSERS["edge"]
    assert edge.package == "com.microsoft.emmx"
    assert "devtools_remote" in edge.devtools_socket
    canary = BROWSERS["edge-canary"]
    assert canary.package == "com.microsoft.emmx.canary"
    assert "devtools_remote" in canary.devtools_socket
    dev = BROWSERS["edge-dev"]
    assert dev.package == "com.microsoft.emmx.dev"
    assert "devtools_remote" in dev.devtools_socket
    chrome = BROWSERS["chrome"]
    assert chrome.package == "com.android.chrome"
    chromium = BROWSERS["chromium"]
    assert chromium.package == "org.chromium.chrome"


def test_backward_compat_constants():
    assert browser_mod.CHROME_PACKAGE == BROWSERS["chrome"].package
    assert browser_mod.CHROME_ACTIVITY == BROWSERS["chrome"].activity


def test_resolve_browser_uses_default_browser_from_config(monkeypatch):
    """resolve_browser(None) must honour harness.toml's default_browser."""
    from harness_android import browser as bmod

    monkeypatch.setattr(
        bmod, "BROWSERS", BROWSERS, raising=False,
    )
    # Point load_config at a fake so we don't depend on the filesystem.
    def fake_load_config():
        return {"default_browser": "edge-local"}
    import harness_android.config as cfg_mod
    monkeypatch.setattr(cfg_mod, "load_config", fake_load_config)

    spec = bmod.resolve_browser(None)
    assert spec.name == "edge-local"


def test_resolve_browser_falls_back_to_chrome_on_config_error(monkeypatch):
    import harness_android.config as cfg_mod
    from harness_android import browser as bmod

    def broken():
        raise RuntimeError("boom")
    monkeypatch.setattr(cfg_mod, "load_config", broken)
    spec = bmod.resolve_browser(None)
    assert spec.name == "chrome"


class _FakeSession:
    def __init__(self):
        self.calls = []
        self.connected = True
        self._crashed = False

    def send(self, method, params=None, *, timeout=None):
        self.calls.append(method)
        return {"via": id(self)}


def test_send_routes_browser_domains():
    """Browser/Target/SystemInfo/Storage go to the browser session, the
    rest to the page session."""
    b = Browser.__new__(Browser)  # bypass __init__ (no ADB)
    b._page = _FakeSession()
    b._browser = _FakeSession()
    b.timeout = 30.0

    b.send("Browser.getVersion")
    b.send("Target.getTargets")
    b.send("SystemInfo.getInfo")
    b.send("Storage.getCookies")
    b.send("Page.enable")
    b.send("Runtime.evaluate", {"expression": "1"})

    assert b._browser.calls == [
        "Browser.getVersion",
        "Target.getTargets",
        "SystemInfo.getInfo",
        "Storage.getCookies",
    ]
    assert b._page.calls == ["Page.enable", "Runtime.evaluate"]


def test_send_falls_back_to_page_when_no_browser_session():
    b = Browser.__new__(Browser)
    b._page = _FakeSession()
    b._browser = None
    b.timeout = 30.0
    b.send("Browser.getVersion")
    assert b._page.calls == ["Browser.getVersion"]


def test_browser_spec_from_string(monkeypatch):
    """Browser('edge') should resolve to the Edge BrowserSpec."""
    # Bypass ADB existence check
    class FakeADB:
        pass

    spec = BROWSERS["edge"]
    assert isinstance(spec, BrowserSpec)
    assert spec.name == "edge"


@pytest.mark.parametrize("name", ["edge", "edge-canary", "edge-dev", "edge-local"])
def test_every_edge_preset_enables_mojojs_by_default(name: str) -> None:
    """MojoJS bindings must be on by default for every edge-* preset so
    ``Mojo.bindInterface`` is reachable on every page (incl. privileged
    ``edge://`` origins) without having to pass ``--chrome-flags``.

    Release Edge silently ignores the flag, so it's a no-op on
    non-debuggable builds — but leaving it on everywhere keeps the
    story consistent for the user.
    """
    spec = BROWSERS[name]
    flags = " ".join(spec.default_flags)
    assert "MojoJS" in flags, (
        f"{name} is missing --enable-blink-features=MojoJS,MojoJSTest "
        f"in default_flags ({spec.default_flags!r})"
    )


@pytest.mark.parametrize("name", ["chrome", "chromium"])
def test_non_edge_presets_have_no_extra_default_flags(name: str) -> None:
    """``chrome`` and ``chromium`` presets intentionally don't flip
    MojoJS — a previous refactor briefly added it to ``chromium`` by
    mistake. Keep them minimal."""
    assert BROWSERS[name].default_flags == ()


def test_write_chrome_flags_prepends_spec_defaults(monkeypatch):
    """``_write_chrome_flags`` must include every ``spec.default_flags``
    entry alongside the common CDP flags, before any extra_flags."""
    b = Browser.__new__(Browser)
    b.spec = BROWSERS["edge-local"]
    b._extra_chrome_flags = ["--my-custom-flag"]
    b.adb = None  # we'll patch adb.write_file / .run below

    written: dict[str, str] = {}

    class _FakeADB:
        def write_file(self, path, content):
            written[path] = content

        def run(self, *args, **kwargs):  # noqa: ARG002
            class R:
                returncode = 0
                stdout = ""
            return R()

    b.adb = _FakeADB()
    b._write_chrome_flags()

    flags = next(iter(written.values()))
    assert "--enable-blink-features=MojoJS,MojoJSTest" in flags
    assert "--remote-debugging-port=0" in flags
    assert "--my-custom-flag" in flags
    # default_flags must come before extra_flags.
    assert flags.index("MojoJS") < flags.index("--my-custom-flag")
