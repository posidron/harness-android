"""Tests for the zip/tar extraction guards in harness_android.sdk.

The SDK bootstrap downloads cmdline-tools / JDK archives and unpacks them
into the user's workspace.  A malicious archive with traversal entries
(zip-slip / tar-slip, CVE-2007-4559 class) would otherwise write
arbitrary files anywhere on the host.
"""

from __future__ import annotations

import io
import tarfile
import zipfile
from pathlib import Path

import pytest

from harness_android.sdk import _safe_extract_tar, _safe_extract_zip


# ----------------------------------------------------------------------
# zip-slip
# ----------------------------------------------------------------------

def _make_zip(entries: dict[str, bytes]) -> zipfile.ZipFile:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    buf.seek(0)
    return zipfile.ZipFile(buf)


def test_zip_rejects_parent_dir_traversal(tmp_path):
    zf = _make_zip({"../evil": b"pwn"})
    with pytest.raises(RuntimeError, match="escapes destination"):
        _safe_extract_zip(zf, tmp_path)


def test_zip_rejects_absolute_path(tmp_path):
    zf = _make_zip({"/etc/passwd": b"pwn"})
    with pytest.raises(RuntimeError, match="unsafe path"):
        _safe_extract_zip(zf, tmp_path)


def test_zip_rejects_backslash_traversal(tmp_path):
    # Some zip tools put backslashes in the name to bypass naive checks.
    zf = _make_zip({"..\\evil.exe": b"pwn"})
    # zipfile may normalise the backslash to ``/`` before we see it, so the
    # entry is rejected either as "unsafe path" or as "escapes destination" –
    # both are safe outcomes.
    with pytest.raises(RuntimeError, match="unsafe path|escapes destination"):
        _safe_extract_zip(zf, tmp_path)


def test_zip_extracts_normal_entries(tmp_path):
    zf = _make_zip({"bin/java": b"#!/bin/java\n", "lib/x.jar": b"abc"})
    _safe_extract_zip(zf, tmp_path)
    assert (tmp_path / "bin" / "java").read_bytes() == b"#!/bin/java\n"
    assert (tmp_path / "lib" / "x.jar").read_bytes() == b"abc"


def test_zip_strip_prefix_keeps_safety(tmp_path):
    zf = _make_zip({
        "cmdline-tools/bin/sdkmanager": b"ok",
        "cmdline-tools/../escape": b"pwn",
    })
    with pytest.raises(RuntimeError, match="escapes destination"):
        _safe_extract_zip(zf, tmp_path, strip_prefix="cmdline-tools/")


def test_zip_strip_prefix_skips_unprefixed_entries(tmp_path):
    zf = _make_zip({
        "cmdline-tools/bin/sdkmanager": b"ok",
        "top-level/readme.txt": b"noise",
    })
    _safe_extract_zip(zf, tmp_path, strip_prefix="cmdline-tools/")
    assert (tmp_path / "bin" / "sdkmanager").read_bytes() == b"ok"
    assert not (tmp_path / "top-level").exists()


# ----------------------------------------------------------------------
# tar-slip
# ----------------------------------------------------------------------

def _make_tar(entries: dict[str, bytes], symlinks: dict[str, str] | None = None) -> tarfile.TarFile:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, content in entries.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(content)
            tf.addfile(info, io.BytesIO(content))
        for link_name, target in (symlinks or {}).items():
            info = tarfile.TarInfo(name=link_name)
            info.type = tarfile.SYMTYPE
            info.linkname = target
            tf.addfile(info)
    buf.seek(0)
    return tarfile.open(fileobj=buf, mode="r:gz")


def test_tar_extracts_normal_entries(tmp_path):
    tf = _make_tar({"bin/java": b"ok"})
    _safe_extract_tar(tf, tmp_path)
    assert (tmp_path / "bin" / "java").read_bytes() == b"ok"


def test_tar_rejects_parent_traversal(tmp_path):
    tf = _make_tar({"../evil": b"pwn"})
    with pytest.raises((RuntimeError, tarfile.OutsideDestinationError, Exception)):
        _safe_extract_tar(tf, tmp_path)
    assert not (tmp_path.parent / "evil").exists()


def test_tar_rejects_dangerous_symlink(tmp_path):
    tf = _make_tar({}, symlinks={"link": "/etc/passwd"})
    with pytest.raises(Exception):
        _safe_extract_tar(tf, tmp_path)
