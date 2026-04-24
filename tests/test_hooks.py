"""Tests for harness_android.hooks built-in JS hook scripts."""

from __future__ import annotations

import re

from harness_android import hooks


# ----------------------------------------------------------------------
# Multi-hook installation regression
# ----------------------------------------------------------------------

def test_each_builtin_hook_is_independent_iife():
    """Each hook ships its own IIFE wrapper (preamble + closing })();)."""
    for name, script in hooks.BUILTIN_HOOKS.items():
        assert script.lstrip().startswith("(function()"), name
        assert script.rstrip().endswith("})();"), name


def test_per_hook_guard_uses_unique_flag():
    """The guard variable must be unique per hook so the second-installed
    hook does not short-circuit on a flag set by the first one.

    This is the regression for the original ``if (window.__harness_hooks__) return;``
    bug, where every hook after the first silently failed to wrap its API.
    """
    flags = []
    for script in hooks.BUILTIN_HOOKS.values():
        m = re.search(r"window\.__harness_hook_(\w+)__", script)
        assert m, "missing per-hook guard"
        flags.append(m.group(1))
    # The bag-of-collectors slot may be shared across hooks, but each hook
    # must own a distinct guard flag.
    assert len(set(flags)) == len(flags), f"duplicate guard flags: {flags}"


def test_no_shared_early_return_on_collector_existence():
    """Early-returning when ``__harness_hooks__`` already exists used to
    skip the body of every hook past the first.  Make sure no script
    contains that pattern any more."""
    bad = re.compile(r"if\s*\(\s*window\.__harness_hooks__\s*\)\s*return")
    for name, script in hooks.BUILTIN_HOOKS.items():
        assert not bad.search(script), f"{name} still has the shared early-return guard"


def test_collector_initialised_without_clobber():
    """The collector must be created only when missing so a second hook
    install does not wipe earlier captures."""
    for name, script in hooks.BUILTIN_HOOKS.items():
        # Either there is no init at all (post-refactor hooks may share an
        # init helper) or the init is gated on absence.
        if "__harness_hooks__ =" in script:
            assert "if (!window.__harness_hooks__)" in script, name


# ----------------------------------------------------------------------
# Hook-specific behaviours
# ----------------------------------------------------------------------

def test_websocket_preserves_static_constants():
    """WebSocket.OPEN and friends must survive the wrapper."""
    src = hooks.HOOK_WEBSOCKET
    assert "Object.getOwnPropertyNames(_WS)" in src, \
        "wrapper must copy static props off the original WebSocket"
    assert "HarnessWS.prototype = _WS.prototype" in src


def test_fetch_handles_request_objects():
    """fetch(new Request('/x', {method:'POST'})) must record method=POST."""
    src = hooks.HOOK_FETCH
    assert "input.method" in src, "fetch hook must read method off Request"
    assert "'url' in input" in src, "fetch hook must detect Request objects"


def test_postmessage_falls_back_when_data_unserialisable():
    """JSON.stringify can throw on cyclic data — the hook must not lose the event."""
    src = hooks.HOOK_POSTMESSAGE
    assert "catch (err)" in src or "catch(err)" in src
    assert "String(e.data)" in src
