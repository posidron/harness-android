"""Unit tests for the forensics secret-scanner.

The goal is to pin down the regex + false-positive logic so the
painstakingly-tuned pattern set doesn't regress into either:
  * surfacing localisation strings, library markers, or placeholder
    creds as real findings, or
  * missing actual credentials.

These tests touch only pure-Python helpers — no ADB or APK needed.
"""

from __future__ import annotations

import pytest

from harness_android.forensics import (
    SECRET_PATTERNS,
    _is_false_positive,
)


def _first_kept_match(text: str, pattern_name: str) -> str | None:
    """Return the raw match for *pattern_name* in *text*, or ``None``
    if the pattern either doesn't match or the match is classified as
    a false positive by the scanner."""
    for pat in SECRET_PATTERNS:
        if pat.name != pattern_name:
            continue
        m = pat.pattern.search(text)
        if not m:
            return None
        captured = m.group(1) if m.groups() else None
        if _is_false_positive(pat.name, m.group(0), captured):
            return None
        return m.group(0)
    raise AssertionError(f"Unknown SECRET_PATTERNS entry: {pattern_name!r}")


# ---------------------------------------------------------------------
# Hardcoded URL with Credentials
# ---------------------------------------------------------------------

@pytest.mark.parametrize(
    "text",
    [
        "curl http://alice:s3cret%21@api.example.com/v1",
        'fetch("https://deploy:p@ss@ci.internal/artifact")',
    ],
)
def test_url_creds_real_hits(text: str) -> None:
    assert _first_kept_match(text, "Hardcoded URL with Credentials") is not None


@pytest.mark.parametrize(
    "text",
    [
        # Localisation blob — "atau" = Indonesian "or". The old regex
        # matched `://example.com atau https://example.com.Organ...@` as
        # a userinfo run. The tightened pattern must reject it.
        "http://example.com atau https://example.com.Organisasi Anda",
        # Emscripten doc fragment that happens to contain `@` after a
        # URL. Not real creds.
        "https://emscripten.org/docs/FAQ.html#local-webserver some @ later",
        # Classic placeholder values — must be filtered.
        "https://user:pass@host/",
        "https://username:password@host/",
    ],
)
def test_url_creds_false_positives(text: str) -> None:
    assert _first_kept_match(text, "Hardcoded URL with Credentials") is None


# ---------------------------------------------------------------------
# Bearer Token
# ---------------------------------------------------------------------

def test_bearer_real_jwt_is_flagged() -> None:
    jwt = "Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJhbGljZSJ9.signature-bytes"
    assert _first_kept_match(jwt, "Bearer Token") is not None


@pytest.mark.parametrize(
    "text",
    [
        # Documentation prose — common in SDK strings.
        "Bearer Authentication",
        "Bearer flows",
        "Bearer Challenge",
        # The literal word "Bearer" followed by a short lowercase word.
        "Bearer token",
    ],
)
def test_bearer_prose_false_positives(text: str) -> None:
    assert _first_kept_match(text, "Bearer Token") is None


# ---------------------------------------------------------------------
# Private Key (PEM)
# ---------------------------------------------------------------------

def test_pem_with_body_is_flagged() -> None:
    pem = (
        "-----BEGIN PRIVATE KEY-----\n"
        "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7VJTUt9Us8cKj"
        "MzEfYyjiWA4R4/M2bS1GB4t7NXp98C3SC6dVMvDuictGeurT8jNbvJZHtCSuYEvu"
    )
    assert _first_kept_match(pem, "Private Key (PEM)") is not None


def test_pem_bare_marker_not_flagged() -> None:
    # Just the begin marker with nothing following — library artefact.
    bare = "-----BEGIN PRIVATE KEY-----\x00\x00next unrelated thing"
    assert _first_kept_match(bare, "Private Key (PEM)") is None


# ---------------------------------------------------------------------
# Generic Secret
# ---------------------------------------------------------------------

def test_generic_real_password_is_flagged() -> None:
    assert _first_kept_match(
        'password="Xk9mQ2vL8pR4tW7abc"',
        "Generic Secret",
    ) is not None


@pytest.mark.parametrize(
    "text",
    [
        # Library markers / placeholders that used to be flagged.
        'Token="fileToken"',
        'AuthToken="MipNoAuthToken"',
        'password="password"',
        'secret="secret"',
    ],
)
def test_generic_placeholder_false_positives(text: str) -> None:
    assert _first_kept_match(text, "Generic Secret") is None


# ---------------------------------------------------------------------
# Catalogue sanity check — keep every pattern unique and named.
# ---------------------------------------------------------------------

def test_pattern_catalogue_is_well_formed() -> None:
    names = [p.name for p in SECRET_PATTERNS]
    assert len(names) == len(set(names)), "duplicate pattern name(s)"
    for p in SECRET_PATTERNS:
        assert p.severity in {"critical", "high", "medium", "low", "info"}, p
        assert p.pattern.pattern, p.name
