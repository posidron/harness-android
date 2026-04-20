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
