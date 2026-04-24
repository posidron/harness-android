"""Regression tests for recon header/CSP capture changes."""

from __future__ import annotations

from harness_android import recon


class _FakeBrowser:
    """Minimal Browser stand-in whose main-frame headers we control."""

    def __init__(self, headers=None, meta_csp=""):
        self.main_frame_response_headers = headers or {}
        self._meta_csp = meta_csp

    def evaluate_js(self, expr, **_kw):
        if "meta[http-equiv" in expr:
            return self._meta_csp
        return None

    # The rest of the Browser surface isn't touched by analyze_* in this config.
    def get_page_url(self):
        return "https://example.test/"

    def get_page_title(self):
        return ""

    def send(self, *a, **kw):
        return {}

    def drain_events(self, *a, **kw):
        return []


# ----------------------------------------------------------------------
# analyze_security_headers must read the captured response headers,
# NOT issue a secondary fetch.
# ----------------------------------------------------------------------

def test_security_headers_use_captured_main_frame_headers():
    b = _FakeBrowser(headers={
        "Strict-Transport-Security": "max-age=31536000",
        "Content-Security-Policy": "default-src 'self'",
        "Server": "nginx/1.2",
    })
    r = recon.analyze_security_headers(b)  # type: ignore[arg-type]
    assert "Strict-Transport-Security" in r.present
    assert "Content-Security-Policy" in r.present
    assert any("Info disclosure" in i["issue"] for i in r.issues)


def test_security_headers_when_nothing_captured_is_honest():
    """If navigation didn't happen through the harness, the cache is empty.
    The report should flag everything as missing rather than lying."""
    b = _FakeBrowser(headers={})
    r = recon.analyze_security_headers(b)  # type: ignore[arg-type]
    assert r.present == []
    assert "Content-Security-Policy" in r.missing


# ----------------------------------------------------------------------
# analyze_csp must check the response header, not just the meta tag.
# ----------------------------------------------------------------------

def test_csp_picked_up_from_response_header():
    b = _FakeBrowser(
        headers={"Content-Security-Policy": "default-src 'self'; script-src 'self' 'unsafe-inline'"},
    )
    result = recon.analyze_csp(b)  # type: ignore[arg-type]
    assert result["csp_header"]
    assert "script-src" in result["directives"]
    assert any("unsafe-inline" in issue for issue in result["issues"])


def test_csp_report_only_is_flagged_as_non_enforcing():
    b = _FakeBrowser(
        headers={"Content-Security-Policy-Report-Only": "default-src 'self'"},
    )
    result = recon.analyze_csp(b)  # type: ignore[arg-type]
    assert result["csp_report_only"]
    assert any("report-only" in issue.lower() for issue in result["issues"])


def test_csp_meta_only_is_flagged_as_weaker_than_header():
    b = _FakeBrowser(headers={}, meta_csp="default-src 'self'")
    result = recon.analyze_csp(b)  # type: ignore[arg-type]
    assert result["csp_meta"]
    assert any("meta" in issue.lower() for issue in result["issues"])


def test_csp_absent_is_reported_missing():
    b = _FakeBrowser()
    result = recon.analyze_csp(b)  # type: ignore[arg-type]
    assert any("No CSP found" in i for i in result["issues"])
