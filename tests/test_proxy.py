"""Tests for harness_android.proxy."""

from __future__ import annotations

import pytest

from harness_android.proxy import Proxy


class _FakeADB:
    def __init__(self):
        self.shell_calls: list[tuple[str, ...]] = []
        self.run_calls: list[tuple[str, ...]] = []

    def shell(self, *args):
        self.shell_calls.append(args)
        return ""

    def run(self, *args, **kwargs):
        self.run_calls.append(args)
        class _R: returncode = 0; stdout = ""; stderr = ""
        return _R()


# ----------------------------------------------------------------------
# add_hosts_entry shell-injection guard
# ----------------------------------------------------------------------

@pytest.mark.parametrize("ip,hostname", [
    ("1.2.3.4", "'; rm -rf /sdcard; echo '"),
    ("1.2.3.4", "evil$(whoami)"),
    ("1.2.3.4", "a b"),              # space
    ("1.2.3.4", "foo`id`bar"),
    ("1.2.3.4", "'"),
    ("$(whoami)", "example.com"),
    ("1.2.3.4;rm", "example.com"),
])
def test_add_hosts_entry_rejects_shell_metacharacters(ip, hostname):
    p = Proxy(_FakeADB())  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        p.add_hosts_entry(ip, hostname)


@pytest.mark.parametrize("ip,hostname", [
    ("127.0.0.1", "localhost"),
    ("10.0.2.2", "host.docker.internal"),
    ("::1", "ip6-localhost"),
    ("fe80::1", "node-1.internal"),
    ("1.2.3.4", "api.v2.example.com"),
])
def test_add_hosts_entry_accepts_normal_values(ip, hostname):
    fake = _FakeADB()
    p = Proxy(fake)  # type: ignore[arg-type]
    p.add_hosts_entry(ip, hostname)
    assert fake.run_calls, "shell command should have been sent"
