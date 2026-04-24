"""harness-android MCP server.

Exposes the most useful harness-android primitives as MCP tools so an AI
agent (Claude, Copilot Chat, etc.) can debug Android + Edge/Chrome
interactively without shelling out to ``poetry run harness-android`` for
every little step.

Run with::

    poetry run harness-android-mcp

Register in a client (``.vscode/mcp.json`` or ``claude_desktop_config.json``)::

    {
      "mcpServers": {
        "harness-android": {
          "command": "poetry",
          "args": ["run", "harness-android-mcp"],
          "cwd": "C:/Users/chdieh/Downloads/android-harness"
        }
      }
    }

Design notes
------------
* **Stateful**. A single :class:`harness_android.browser.Browser` instance
  lives on the server so successive ``cdp_eval`` / ``cdp_navigate`` calls
  re-use the WebSocket. Re-attaching would cost ~seconds per call.
* **Every tool returns JSON-serializable data**. Rich / formatted output
  is suppressed; callers get machine-readable structures.
* **Defensive inputs**. Input helpers refuse swipes that previously caused
  renderer crashes on the x86_64 emulator (very long vertical swipes on
  Edge NTP).
"""
from __future__ import annotations

import base64
import dataclasses
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

from harness_android.adb import ADB
from harness_android.browser import Browser, BROWSERS, CDPError, TargetCrashed

# --------------------------------------------------------------------------
# Server state
# --------------------------------------------------------------------------

mcp = FastMCP("harness-android")


@dataclass
class _State:
    adb: ADB | None = None
    browser: Browser | None = None
    browser_name: str | None = None

    def ensure_adb(self) -> ADB:
        if self.adb is None:
            self.adb = ADB()
        return self.adb

    def ensure_browser(self, name: str = "edge-local") -> Browser:
        if self.browser is not None and self.browser_name == name:
            return self.browser
        if self.browser is not None:
            try:
                self.browser.close()
            except Exception:
                pass
            self.browser = None
        self.browser = Browser(self.ensure_adb(), browser=name)
        self.browser_name = name
        return self.browser


S = _State()


def _shell(cmd: str, check: bool = False) -> str:
    """Run ``cmd`` through ``sh -c`` so pipes and redirections work."""
    return S.ensure_adb().shell(f"sh -c {json.dumps(cmd)}", check=check)


# --------------------------------------------------------------------------
# Device state
# --------------------------------------------------------------------------

@mcp.tool()
def device_status() -> dict:
    """Return connected-emulator state: serial, Edge PID, foreground activity.

    This is the safest first call — it tells you whether the emulator and
    Edge are actually alive before you try any CDP operations.
    """
    a = S.ensure_adb()
    devices = a.run("devices", check=False).stdout.strip()
    emmx_pid = _shell("pidof com.microsoft.emmx.local || true").strip()
    screen = _shell(
        "dumpsys power | grep 'Display Power' || true"
    ).strip()
    activity = _shell(
        "dumpsys activity activities | grep -E 'topResumedActivity|mResumedActivity' "
        "| head -3 || true"
    ).strip()
    chrome_pid = _shell("pidof com.android.chrome || true").strip()
    return {
        "devices": devices,
        "emmx_local_pid": emmx_pid or None,
        "chrome_pid": chrome_pid or None,
        "foreground": activity,
        "display_power": screen,
    }


@mcp.tool()
def device_screenshot(path: str = "mcp_screenshot.png") -> dict:
    """Capture the device screen to *path* (PNG). Use BEFORE and AFTER any
    risky input action so you can confirm the renderer didn't crash."""
    p = Path(path).resolve()
    p.parent.mkdir(parents=True, exist_ok=True)
    S.ensure_adb().screenshot(p)
    return {"path": str(p), "size": p.stat().st_size}


@mcp.tool()
def adb_shell(command: str, check: bool = False) -> dict:
    """Run ``command`` in the emulator's ``sh``. Pipes/redirections work.

    Returns ``{stdout, rc}``. Use for inspection; avoid destructive commands
    without explicit user consent.
    """
    a = S.ensure_adb()
    cp = a.run("shell", "sh", "-c", command, check=check)
    return {"stdout": cp.stdout, "stderr": cp.stderr, "rc": cp.returncode}


@mcp.tool()
def adb_forward_list() -> dict:
    """List active ``adb forward`` rules — useful for confirming the CDP
    port forward is set."""
    cp = S.ensure_adb().run("forward", "--list", check=False)
    return {"rules": [l for l in cp.stdout.splitlines() if l.strip()]}


@mcp.tool()
def adb_unix_sockets(filter_regex: str = "devtools|webview") -> dict:
    """Dump abstract unix sockets matching *filter_regex* (egrep).

    Confirms whether the browser actually opened a ``chrome_devtools_remote``
    socket — the #1 failure mode for CDP flows.
    """
    raw = _shell(f"cat /proc/net/unix | grep -iE '{filter_regex}' || true")
    return {"lines": [l.strip() for l in raw.splitlines() if l.strip()]}


# --------------------------------------------------------------------------
# Browser management
# --------------------------------------------------------------------------

@mcp.tool()
def list_browsers() -> dict:
    """Return the browser presets this harness knows about."""
    return {
        name: {"package": spec.package, "activity": spec.activity,
               "socket": spec.devtools_socket}
        for name, spec in BROWSERS.items()
    }


@mcp.tool()
def cdp_status() -> dict:
    """High-signal summary: is a DevTools socket present? is a Browser
    session attached in this MCP process? what pages does it see?"""
    socks = _shell(
        "cat /proc/net/unix | grep -iE 'devtools' || true"
    ).strip().splitlines()
    forwards = [l for l in S.ensure_adb().run("forward", "--list", check=False)
                .stdout.splitlines() if "chrome_devtools" in l]
    attached = S.browser is not None and S.browser._page is not None  # noqa: SLF001
    pages: list[dict] = []
    current: dict | None = None
    if attached:
        try:
            pages = [
                {"id": t.get("id"), "url": t.get("url"),
                 "title": t.get("title"), "type": t.get("type")}
                for t in S.browser.list_targets()  # type: ignore[union-attr]
            ]
        except Exception as exc:
            pages = [{"error": str(exc)}]
        try:
            current = {"url": S.browser.get_page_url(),  # type: ignore[union-attr]
                       "title": S.browser.get_page_title()}  # type: ignore[union-attr]
        except Exception:
            current = None
    return {
        "browser": S.browser_name,
        "attached": attached,
        "devtools_sockets": socks,
        "forwards": forwards,
        "pages": pages,
        "current_target": current,
    }


@mcp.tool()
def cdp_prepare_and_launch(
    browser: str = "edge-local",
    wait_socket_timeout: float = 30.0,
) -> dict:
    """Atomic cold-launch: write CDP flags, force-stop the browser, cold-
    start it, poll for the ``chrome_devtools_remote`` socket, then attach.

    This is the reliable replacement for the manual ``--prepare`` + ``am
    force-stop`` + ``am start`` + ``Start-Sleep`` dance that keeps failing.
    """
    b = S.ensure_browser(browser)
    spec = b.spec
    b.prepare_cdp()
    S.ensure_adb().run("shell", "am", "force-stop", spec.package, check=False)
    time.sleep(1.0)
    S.ensure_adb().run(
        "shell", "am", "start", "-n", f"{spec.package}/{spec.activity}",
        check=False,
    )
    # Poll for socket
    deadline = time.monotonic() + wait_socket_timeout
    socket_seen_at: float | None = None
    while time.monotonic() < deadline:
        raw = _shell("cat /proc/net/unix | grep chrome_devtools_remote || true")
        if raw.strip():
            socket_seen_at = wait_socket_timeout - (deadline - time.monotonic())
            break
        time.sleep(1.0)
    if socket_seen_at is None:
        return {
            "ok": False,
            "reason": f"DevTools socket did not appear within {wait_socket_timeout}s",
            "hint": "The browser may have started before the cmdline file was "
                    "readable, or the preset's cmdline_files list is wrong.",
        }
    b.attach_cdp()
    return {
        "ok": True,
        "browser": browser,
        "socket_seen_after_s": round(socket_seen_at, 2),
        "pages": [
            {"id": t.get("id"), "url": t.get("url"), "title": t.get("title")}
            for t in b.list_targets() if t.get("type") == "page"
        ],
    }


@mcp.tool()
def cdp_attach(browser: str = "edge-local") -> dict:
    """Attach to an already-running browser without restarting it. Fails
    fast with a clear error if no DevTools socket is open."""
    b = S.ensure_browser(browser)
    try:
        b.attach_cdp()
    except TimeoutError as exc:
        return {"ok": False, "reason": str(exc),
                "hint": "Run cdp_prepare_and_launch instead."}
    return cdp_status()


@mcp.tool()
def cdp_list_pages() -> dict:
    """List every CDP page target."""
    if S.browser is None:
        return {"error": "Not attached. Call cdp_attach or cdp_prepare_and_launch first."}
    targets = S.browser.list_targets()
    return {"pages": [
        {"id": t.get("id"), "url": t.get("url"), "title": t.get("title"),
         "type": t.get("type")}
        for t in targets
    ]}


@mcp.tool()
def cdp_connect_to(target_id: Optional[str] = None,
                   url_substring: Optional[str] = None) -> dict:
    """Pick a page target and connect to it.

    Provide either *target_id* (exact CDP id) or *url_substring*. Without
    either, connects to the first page target (CDP's ``GET /json`` default).
    """
    if S.browser is None:
        return {"error": "Not attached."}
    if target_id:
        S.browser.connect(target_id=target_id)
    elif url_substring:
        tid = S.browser.find_target(url_substring=url_substring)
        if not tid:
            return {"error": f"No page target URL contains {url_substring!r}"}
        S.browser.connect(target_id=tid)
    else:
        S.browser.connect()
    return {"current_url": S.browser.get_page_url(),
            "current_title": S.browser.get_page_title()}


# --------------------------------------------------------------------------
# Page ops
# --------------------------------------------------------------------------

@mcp.tool()
def cdp_eval(expression: str, await_promise: bool = False,
             timeout: float = 15.0) -> dict:
    """Evaluate JavaScript in the currently-attached page.

    Returns a structured ``__cdp_*`` preview for non-serializable values
    (``window``, DOM nodes, bridge objects) instead of crashing.
    """
    if S.browser is None:
        return {"error": "Not attached."}
    try:
        value = S.browser.evaluate_js(expression, await_promise=await_promise,
                                      timeout=timeout)
    except CDPError as exc:
        return {"error": "cdp_error", "code": exc.code, "message": exc.message}
    except RuntimeError as exc:
        return {"error": "js_exception", "message": str(exc)}
    return {"value": value}


@mcp.tool()
def cdp_navigate(url: str, wait_for_load: bool = True,
                 wait_for_expression: Optional[str] = None,
                 wait_timeout: float = 10.0,
                 timeout: float = 30.0) -> dict:
    """Navigate the attached page to *url*.

    * If *wait_for_load* is true, blocks until ``Page.loadEventFired``.
    * If *wait_for_expression* is provided, additionally polls the JS
      expression until it is truthy (defeats races where host-injected
      globals like ``sapphireWebViewBridge`` appear after load).
    """
    if S.browser is None:
        return {"error": "Not attached."}
    S.browser.navigate(url, wait=wait_for_load, timeout=timeout)
    waited: Any = None
    if wait_for_expression:
        try:
            waited = S.browser.wait_for_expression(wait_for_expression,
                                                   timeout=wait_timeout)
        except TimeoutError as exc:
            return {"url": S.browser.get_page_url(), "wait_timed_out": str(exc)}
    return {"url": S.browser.get_page_url(),
            "title": S.browser.get_page_title(),
            "wait_for_resolved": waited}


@mcp.tool()
def cdp_wait_for(expression: str, timeout: float = 10.0) -> dict:
    """Poll *expression* until it evaluates truthy, or time out."""
    if S.browser is None:
        return {"error": "Not attached."}
    try:
        val = S.browser.wait_for_expression(expression, timeout=timeout)
    except TimeoutError as exc:
        return {"ok": False, "reason": str(exc)}
    return {"ok": True, "value": val}


@mcp.tool()
def cdp_inject_on_load(script: str) -> dict:
    """Install a ``Page.addScriptToEvaluateOnNewDocument`` hook that runs
    before any page script on every subsequent navigation. Returns an id
    you can pass to ``cdp_remove_injected``."""
    if S.browser is None:
        return {"error": "Not attached."}
    ident = S.browser.inject_script_on_load(script)
    return {"id": ident}


@mcp.tool()
def cdp_remove_injected(identifier: str) -> dict:
    """Remove an on-load script installed with ``cdp_inject_on_load``."""
    if S.browser is None:
        return {"error": "Not attached."}
    S.browser.remove_injected_script(identifier)
    return {"ok": True}


@mcp.tool()
def cdp_page_screenshot(path: str = "mcp_page_screenshot.png") -> dict:
    """Screenshot the attached CDP page (not the device chrome)."""
    if S.browser is None:
        return {"error": "Not attached."}
    p = Path(path).resolve()
    p.parent.mkdir(parents=True, exist_ok=True)
    S.browser.page_screenshot(str(p))
    return {"path": str(p), "size": p.stat().st_size}


@mcp.tool()
def cdp_disconnect() -> dict:
    """Close the CDP session (but leave the browser running)."""
    if S.browser is not None:
        try:
            S.browser.close()
        finally:
            S.browser = None
            S.browser_name = None
    return {"ok": True}


# --------------------------------------------------------------------------
# WebView enumeration (separate sockets: mini-apps, third-party apps)
# --------------------------------------------------------------------------

@mcp.tool()
def webview_list() -> dict:
    """List every debuggable WebView socket system-wide.

    Mini apps and embedded WebViews (Sapphire mini apps, 3rd-party apps
    with WebView, ...) expose their own ``webview_devtools_remote_<pid>``
    sockets — they will NOT show up in ``cdp_list_pages`` on the main
    browser socket.
    """
    raw = _shell(
        "cat /proc/net/unix | grep -iE 'devtools|webview' || true"
    )
    out: list[dict] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        # Last field is the socket name (absolute or abstract with @)
        parts = line.split()
        name = parts[-1] if parts else line
        # Only real DevTools sockets, not WebViewZygoteInit
        if "devtools" in name.lower() or "webview_devtools_remote" in name.lower():
            # Extract pid from e.g. @webview_devtools_remote_12345
            pid = None
            if "webview_devtools_remote_" in name:
                try:
                    pid = int(name.rsplit("_", 1)[-1])
                except ValueError:
                    pid = None
            out.append({"socket": name.lstrip("@"), "pid": pid, "raw": line})
    return {"sockets": out}


# --------------------------------------------------------------------------
# Input (with safety rails — previous sessions crashed Edge renderer on
# long swipes)
# --------------------------------------------------------------------------

_SCREEN_W = 1080
_SCREEN_H = 2400


@mcp.tool()
def input_tap(x: int, y: int) -> dict:
    """Tap at screen coordinates *(x, y)* in pixels."""
    if not (0 <= x <= _SCREEN_W and 0 <= y <= _SCREEN_H):
        return {"error": f"({x},{y}) outside screen {_SCREEN_W}x{_SCREEN_H}"}
    S.ensure_adb().shell(f"input tap {x} {y}", check=False)
    return {"ok": True}


@mcp.tool()
def input_swipe(x1: int, y1: int, x2: int, y2: int,
                duration_ms: int = 500) -> dict:
    """Swipe from (x1,y1) to (x2,y2) over *duration_ms*.

    Safety: refuses very long or very fast vertical swipes on x86_64
    emulators — Edge's renderer crashed on 1600-px swipes at <500 ms in
    earlier runs. If you need a long swipe, break it into smaller chunks.
    """
    dx = abs(x2 - x1)
    dy = abs(y2 - y1)
    if dy > _SCREEN_H * 0.55 and duration_ms < 600:
        return {"error": "refused",
                "reason": f"vertical displacement {dy}px at {duration_ms}ms "
                          f"is known to crash renderers; chunk it or slow "
                          f"down to >= 600ms"}
    if duration_ms < 150:
        return {"error": "refused", "reason": "duration_ms < 150ms is hostile"}
    S.ensure_adb().shell(
        f"input swipe {x1} {y1} {x2} {y2} {duration_ms}", check=False
    )
    return {"ok": True}


@mcp.tool()
def input_key(keycode: str) -> dict:
    """Send an Android keycode (e.g. ``KEYCODE_BACK``, ``KEYCODE_HOME``,
    ``KEYCODE_APP_SWITCH``, numeric 4 for back, 3 for home)."""
    S.ensure_adb().shell(f"input keyevent {keycode}", check=False)
    return {"ok": True}


# --------------------------------------------------------------------------
# Logcat
# --------------------------------------------------------------------------

@mcp.tool()
def logcat_tail(since_seconds: int = 30, max_lines: int = 200,
                filter_regex: Optional[str] = None) -> dict:
    """Return recent logcat lines.

    *since_seconds* looks back in time; *filter_regex* is applied with
    grep -E. Useful after a swipe / tap that crashed something.
    """
    a = S.ensure_adb()
    cmd = ["logcat", "-d", "-t", f"{since_seconds}s"]
    raw = a.run(*cmd, check=False).stdout
    lines = raw.splitlines()
    if filter_regex:
        import re
        pat = re.compile(filter_regex)
        lines = [l for l in lines if pat.search(l)]
    return {"count": len(lines), "lines": lines[-max_lines:]}


# --------------------------------------------------------------------------
# Emulator & SDK lifecycle
# --------------------------------------------------------------------------

@mcp.tool()
def emulator_setup(api_level: int = 35, arch: str = "x86_64") -> dict:
    """One-time: download Android SDK + system image (~5 GB).

    Long-running. Blocks until complete. Only needed on a fresh machine.
    """
    from harness_android import sdk
    sdk.full_setup(api_level=api_level, arch=arch)
    return {"ok": True}


@mcp.tool()
def emulator_install_chromium() -> dict:
    """Download and install a debuggable Chromium APK on the emulator
    (required for CDP on API 35+ unless using `edge-local`)."""
    from harness_android import sdk
    sdk.install_chromium()
    return {"ok": True}


@mcp.tool()
def avd_create(avd_name: str = "harness_device", api_level: int = 35,
               arch: str = "x86_64", device_profile: str = "pixel_7",
               force: bool = False) -> dict:
    """Create an Android Virtual Device."""
    from harness_android.emulator import Emulator
    Emulator(avd_name=avd_name, api_level=api_level, arch=arch
             ).create_avd(device_profile=device_profile, force=force)
    return {"ok": True, "avd": avd_name}


@mcp.tool()
def avd_delete(avd_name: str = "harness_device") -> dict:
    """Delete an AVD."""
    from harness_android.emulator import Emulator
    Emulator(avd_name=avd_name).delete_avd()
    return {"ok": True}


@mcp.tool()
def emulator_start(avd_name: str = "harness_device", api_level: int = 35,
                   arch: str = "x86_64", headless: bool = False,
                   gpu: str = "auto", ram: int = 4096,
                   wipe_data: bool = False, cold_boot: bool = False,
                   writable_system: bool = True,
                   boot_timeout: float = 300.0) -> dict:
    """Boot the emulator. Long-running (blocks until `sys.boot_completed=1`).

    Creates the AVD automatically if missing. MCP clients may need a
    generous request timeout for this call.
    """
    from harness_android.emulator import Emulator
    emu = Emulator(avd_name=avd_name, api_level=api_level, arch=arch)
    if not emu.avd_exists():
        emu.create_avd()
    emu.start(headless=headless, gpu=gpu, ram=ram, wipe_data=wipe_data,
              cold_boot=cold_boot, writable_system=writable_system,
              boot_timeout=boot_timeout)
    # Refresh ADB cache
    S.adb = ADB()
    return {"ok": True, "avd": avd_name}


@mcp.tool()
def emulator_stop() -> dict:
    """Stop all running emulators."""
    from harness_android.emulator import Emulator
    Emulator().stop()
    S.browser = None
    S.browser_name = None
    S.adb = None
    return {"ok": True}


@mcp.tool()
def device_info() -> dict:
    """Return `getprop` device info (model, android version, SDK, abi, …)."""
    from harness_android.device import Device
    try:
        return Device().get_info()
    except Exception as exc:
        return {"error": str(exc)}


# --------------------------------------------------------------------------
# APK / files
# --------------------------------------------------------------------------

@mcp.tool()
def install_apk(path: str) -> dict:
    """Install an APK onto the emulator."""
    p = Path(path)
    if not p.exists():
        return {"error": f"APK not found: {p}"}
    S.ensure_adb().run("install", "-r", "-t", str(p), check=False, timeout=300)
    return {"ok": True, "path": str(p)}


@mcp.tool()
def push_file(local: str, remote: str) -> dict:
    """Push a file to the emulator."""
    S.ensure_adb().run("push", local, remote, check=False)
    return {"ok": True, "remote": remote}


@mcp.tool()
def pull_file(remote: str, local: str) -> dict:
    """Pull a file from the emulator."""
    S.ensure_adb().run("pull", remote, local, check=False)
    return {"ok": True, "local": local}


# --------------------------------------------------------------------------
# Browser convenience
# --------------------------------------------------------------------------

@mcp.tool()
def browser_open(url: str) -> dict:
    """Open *url* using Android's default browser intent (no CDP).

    Good for bringing a page into view without restarting the browser
    process. Use `cdp_navigate` when you already have a CDP session.
    """
    S.ensure_adb().run(
        "shell", "am", "start", "-a", "android.intent.action.VIEW",
        "-d", url, check=False,
    )
    return {"ok": True, "url": url}


# --------------------------------------------------------------------------
# Proxy / MITM
# --------------------------------------------------------------------------

def _proxy(host: str = "10.0.2.2", port: int = 8080):
    from harness_android.proxy import Proxy
    return Proxy(S.ensure_adb(), host=host, port=port)


@mcp.tool()
def proxy_enable(host: str = "10.0.2.2", port: int = 8080) -> dict:
    """Set the device-wide HTTP proxy (`settings put global http_proxy`)."""
    _proxy(host, port).enable()
    return {"ok": True, "proxy": f"{host}:{port}"}


@mcp.tool()
def proxy_disable() -> dict:
    """Remove the device-wide HTTP proxy."""
    _proxy().disable()
    return {"ok": True}


@mcp.tool()
def proxy_status() -> dict:
    """Show current `http_proxy` setting."""
    return {"proxy": _proxy().get_current()}


@mcp.tool()
def proxy_install_mitmproxy_ca() -> dict:
    """Install mitmproxy's CA cert into the system trust store (requires
    writable_system emulator + `adb root`)."""
    _proxy().install_mitmproxy_ca()
    return {"ok": True}


@mcp.tool()
def proxy_install_ca(cert_path: str) -> dict:
    """Install a custom CA certificate (PEM) into the system trust store."""
    _proxy().install_ca_cert(cert_path)
    return {"ok": True}


@mcp.tool()
def proxy_tcpdump_start(remote_path: str = "/sdcard/capture.pcap") -> dict:
    """Start tcpdump on-device to capture all traffic to *remote_path*."""
    return {"ok": True, "pid": _proxy().start_tcpdump(remote_path)}


@mcp.tool()
def proxy_tcpdump_stop(remote_path: str = "/sdcard/capture.pcap",
                       local_path: str = "capture.pcap") -> dict:
    """Stop tcpdump and pull the pcap to *local_path*."""
    p = _proxy()
    p.stop_tcpdump()
    out = p.pull_capture(remote=remote_path, local=local_path)
    return {"ok": True, "local": str(out)}


@mcp.tool()
def proxy_hosts_add(ip: str, hostname: str) -> dict:
    """Add an entry to the emulator's `/etc/hosts`."""
    _proxy().add_hosts_entry(ip, hostname)
    return {"ok": True}


@mcp.tool()
def proxy_hosts_reset() -> dict:
    """Reset `/etc/hosts` to its default."""
    _proxy().reset_hosts()
    return {"ok": True}


@mcp.tool()
def proxy_hosts_show() -> dict:
    """Show current `/etc/hosts`."""
    return {"hosts": _proxy().show_hosts()}


# --------------------------------------------------------------------------
# Recon (needs an attached Browser session)
# --------------------------------------------------------------------------

def _require_browser() -> Browser | dict:
    if S.browser is None or S.browser._page is None:  # noqa: SLF001
        return {"error": "Not attached. Call cdp_prepare_and_launch or cdp_attach."}
    return S.browser


def _asdict(obj: Any) -> Any:
    if dataclasses.is_dataclass(obj):
        return dataclasses.asdict(obj)
    if isinstance(obj, list):
        return [_asdict(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _asdict(v) for k, v in obj.items()}
    return obj


@mcp.tool()
def recon_full(output: Optional[str] = None) -> dict:
    """Run every recon module (fingerprint + spider + storage + csp +
    security headers + cookies) on the currently-loaded page."""
    from harness_android import recon
    b = _require_browser()
    if isinstance(b, dict): return b
    return _asdict(recon.full_recon(b, output=output))


@mcp.tool()
def recon_fingerprint() -> dict:
    """Fingerprint the currently-loaded page (server, frameworks, meta)."""
    from harness_android import recon
    b = _require_browser()
    if isinstance(b, dict): return b
    return _asdict(recon.fingerprint_page(b))


@mcp.tool()
def recon_spider() -> dict:
    """Spider links / forms / iframes / api endpoints from the current page."""
    from harness_android import recon
    b = _require_browser()
    if isinstance(b, dict): return b
    return _asdict(recon.spider_page(b))


@mcp.tool()
def recon_storage() -> dict:
    """Dump cookies, localStorage, sessionStorage."""
    from harness_android import recon
    b = _require_browser()
    if isinstance(b, dict): return b
    return _asdict(recon.extract_storage(b))


@mcp.tool()
def recon_csp() -> dict:
    """Analyze Content-Security-Policy on the current page.

    Checks both the response header (``Content-Security-Policy`` and
    ``Content-Security-Policy-Report-Only``) *and* any ``<meta http-equiv>``
    tag, and flags the common weaknesses (``unsafe-inline``, ``unsafe-eval``,
    wildcards, report-only, meta-only).

    The response-header path reads ``Browser.main_frame_response_headers``,
    which is populated by ``cdp_navigate``. If the page was loaded outside
    the harness (e.g. attached post-launch and never navigated), only the
    meta-tag path is available — results will say so.
    """
    from harness_android import recon
    b = _require_browser()
    if isinstance(b, dict): return b
    return _asdict(recon.analyze_csp(b))


@mcp.tool()
def recon_cookies() -> dict:
    """Analyze cookie security (secure/httpOnly/sameSite)."""
    from harness_android import recon
    b = _require_browser()
    if isinstance(b, dict): return b
    return {"cookies": _asdict(recon.analyze_cookies(b))}


@mcp.tool()
def recon_security_headers() -> dict:
    """Check presence/absence of common security headers.

    Reads the real response headers captured during ``cdp_navigate`` (via
    CDP ``Network.responseReceived`` for the main-frame Document response).
    No second fetch is issued, so CORS-stripped page-JS views are never
    substituted for the real server response.

    If the page was attached to without a harness navigate(), the cache is
    empty and every header is reported as missing — call ``cdp_navigate``
    first for accurate results.
    """
    from harness_android import recon
    b = _require_browser()
    if isinstance(b, dict): return b
    return _asdict(recon.analyze_security_headers(b))


# --------------------------------------------------------------------------
# JS hooks
# --------------------------------------------------------------------------

_hooks_instance = {"obj": None}


def _hooks():
    from harness_android.hooks import Hooks
    if _hooks_instance["obj"] is None:
        b = _require_browser()
        if isinstance(b, dict): return b
        _hooks_instance["obj"] = Hooks(b)
    return _hooks_instance["obj"]


@mcp.tool()
def hooks_install(names: str = "all") -> dict:
    """Install JS hooks. *names* is comma-separated from:
    `xhr,fetch,cookies,websocket,postmessage,console,storage,forms` (or `all`).
    Applies to the next navigation (uses `addScriptToEvaluateOnNewDocument`)."""
    h = _hooks()
    if isinstance(h, dict): return h
    parts = [n.strip() for n in names.split(",") if n.strip()]
    h.install(*parts)
    return {"ok": True, "installed": parts}


@mcp.tool()
def hooks_install_custom(name: str, script: str) -> dict:
    """Install a custom JS hook. *script* must push captured data into
    `window.__harness_captures` for `hooks_collect` to pick it up."""
    h = _hooks()
    if isinstance(h, dict): return h
    h.install_custom(name, script)
    return {"ok": True, "name": name}


@mcp.tool()
def hooks_collect(clear: bool = False) -> dict:
    """Return captured events. With *clear* also wipes server-side buffer."""
    h = _hooks()
    if isinstance(h, dict): return h
    data = h.collect_and_clear() if clear else h.collect()
    return {"events": data}


@mcp.tool()
def hooks_remove_all() -> dict:
    """Remove all installed hooks."""
    h = _hooks()
    if isinstance(h, dict): return h
    h.remove_all()
    return {"ok": True}


# --------------------------------------------------------------------------
# Forensics (APK, app data, manifest, secrets)
# --------------------------------------------------------------------------

@mcp.tool()
def forensics_scan_apk(apk_path: str, output: Optional[str] = None) -> dict:
    """Full APK scan: secrets + manifest (local, no emulator needed)."""
    from harness_android import forensics
    return _asdict(forensics.full_apk_scan(apk_path, output=output))


@mcp.tool()
def forensics_scan_secrets(apk_path: str) -> dict:
    """Scan an APK for hardcoded secrets (API keys, tokens, etc.)."""
    from harness_android import forensics
    return {"findings": _asdict(forensics.scan_apk_secrets(apk_path))}


@mcp.tool()
def forensics_scan_manifest(apk_path: str) -> dict:
    """Audit AndroidManifest.xml for misconfigurations (exported comps,
    backup allowed, cleartext, etc.)."""
    from harness_android import forensics
    return {"findings": _asdict(forensics.analyze_apk_manifest(apk_path))}


@mcp.tool()
def forensics_scan_app_data(package: str, local_dir: str = "app_data") -> dict:
    """Pull a running app's private data dir (needs `run-as` access or
    debuggable build) and scan strings for secrets."""
    from harness_android import forensics
    path, findings = forensics.extract_app_data(S.ensure_adb(), package,
                                                local_dir=local_dir)
    return {"path": str(path), "findings": _asdict(findings)}


# --------------------------------------------------------------------------
# Intents
# --------------------------------------------------------------------------

@mcp.tool()
def intent_enumerate(package: str) -> dict:
    """List every exported activity / service / receiver / provider of
    *package* (from the installed APK's manifest)."""
    from harness_android import intents
    return {"components": _asdict(intents.enumerate_exported(
        S.ensure_adb(), package))}


@mcp.tool()
def intent_fuzz_package(package: str) -> dict:
    """Send smart intent payloads (path traversal, SQLi, deep-link abuse,
    huge strings, …) to every exported component of *package* and report
    crashes."""
    from harness_android import intents
    return {"results": _asdict(intents.fuzz_package(S.ensure_adb(), package))}


@mcp.tool()
def intent_fuzz_component(component: str, component_type: str = "activity"
                          ) -> dict:
    """Fuzz a single component (`com.pkg/.Activity`). *component_type* is
    one of `activity`, `service`, `broadcast`."""
    from harness_android import intents
    return {"results": _asdict(intents.fuzz_component(
        S.ensure_adb(), component, component_type=component_type))}


# --------------------------------------------------------------------------
# WebView (embedded)
# --------------------------------------------------------------------------

@mcp.tool()
def webview_enumerate() -> dict:
    """Full WebView enumeration: for every webview socket, open a port
    forward and query `GET /json` to list page targets."""
    from harness_android import webview as wv
    # Use the currently-selected browser preset as the package hint for
    # the PID-less chrome_devtools_remote row.
    pkg = S.browser.spec.package if S.browser is not None else ""
    targets = wv.list_all_webviews(S.ensure_adb(), default_chrome_package=pkg)
    return {"targets": _asdict(targets)}


@mcp.tool()
def webview_connect_socket(socket_name: str, local_port: int = 9300) -> dict:
    """Attach a Browser session to a specific WebView socket
    (`webview_devtools_remote_<pid>`). Replaces the current CDP session."""
    from harness_android import webview as wv
    if S.browser is not None:
        try: S.browser.close()
        except Exception: pass
    S.browser = wv.connect_webview(S.ensure_adb(), socket_name,
                                   local_port=local_port)
    S.browser_name = f"webview:{socket_name}"
    return cdp_status()


# --------------------------------------------------------------------------
# Logcat
# --------------------------------------------------------------------------

_logcat_instance: dict[str, Any] = {"obj": None}


@mcp.tool()
def logcat_start(output: str = "logcat.txt",
                 filter_tag: Optional[str] = None,
                 clear_first: bool = True) -> dict:
    """Start streaming logcat to *output* in the background.

    Call `logcat_stop` to finish. Only one capture can be active.
    """
    from harness_android.logcat import LogcatCapture
    if _logcat_instance["obj"] is not None:
        return {"error": "already running; call logcat_stop first"}
    cap = LogcatCapture(S.ensure_adb())
    path = cap.start(output=output, filter_tag=filter_tag,
                     clear_first=clear_first)
    _logcat_instance["obj"] = cap
    return {"ok": True, "path": str(path)}


@mcp.tool()
def logcat_stop() -> dict:
    """Stop the background logcat capture and return the log path."""
    cap = _logcat_instance["obj"]
    if cap is None:
        return {"error": "not running"}
    path = cap.stop()
    _logcat_instance["obj"] = None
    return {"ok": True, "path": str(path)}


@mcp.tool()
def logcat_find_crashes(path: str) -> dict:
    """Scan a logcat file for ASan / SIGSEGV / ANR / tombstones."""
    from harness_android.logcat import LogcatCapture
    return {"crashes": _asdict(LogcatCapture.find_crashes(path))}


# --------------------------------------------------------------------------
# UI automation (UIAutomator dumps, smart tap, monkey)
# --------------------------------------------------------------------------

@mcp.tool()
def ui_dump(max_depth: int = 10) -> dict:
    """Dump the current screen UI hierarchy via UIAutomator."""
    from harness_android import ui
    root = ui.dump_hierarchy(S.ensure_adb())
    return _asdict(root) if max_depth >= 999 else {"root": _asdict(root)}


@mcp.tool()
def ui_find_by_text(text: str, exact: bool = False) -> dict:
    """Dump + filter UI elements by visible text."""
    from harness_android import ui
    root = ui.dump_hierarchy(S.ensure_adb())
    return {"matches": _asdict(ui.find_by_text(root, text, exact=exact))}


@mcp.tool()
def ui_find_by_resource_id(resource_id: str) -> dict:
    """Dump + filter UI elements by resource-id."""
    from harness_android import ui
    root = ui.dump_hierarchy(S.ensure_adb())
    return {"matches": _asdict(ui.find_by_resource_id(root, resource_id))}


@mcp.tool()
def ui_tap_text(text: str) -> dict:
    """Dump the UI, find the first element with *text*, tap its center."""
    from harness_android import ui
    adb = S.ensure_adb()
    root = ui.dump_hierarchy(adb)
    ok = ui.tap_element(adb, root, text)
    return {"ok": ok}


@mcp.tool()
def ui_tap_resource_id(resource_id: str) -> dict:
    """Tap the first element matching *resource_id*."""
    from harness_android import ui
    adb = S.ensure_adb()
    root = ui.dump_hierarchy(adb)
    ok = ui.tap_by_resource_id(adb, root, resource_id)
    return {"ok": ok}


@mcp.tool()
def ui_type_into(resource_id: str, text: str) -> dict:
    """Tap an input by resource-id and type *text* into it."""
    from harness_android import ui
    adb = S.ensure_adb()
    root = ui.dump_hierarchy(adb)
    ok = ui.type_into(adb, root, resource_id, text)
    return {"ok": ok}


@mcp.tool()
def ui_monkey(package: Optional[str] = None, event_count: int = 500,
              throttle_ms: int = 50, seed: Optional[int] = None,
              ignore_crashes: bool = False) -> dict:
    """Run Android's `monkey` random-event stress test. Scoped to *package*
    when given. Returns stdout; scan it for crashes."""
    from harness_android import ui
    out = ui.run_monkey(S.ensure_adb(), package=package,
                        event_count=event_count, throttle_ms=throttle_ms,
                        seed=seed, ignore_crashes=ignore_crashes)
    return {"output": out}


# --------------------------------------------------------------------------
# Mojo IPC
# --------------------------------------------------------------------------

_mojo_instance: dict[str, Any] = {"tracer": None, "js": None, "fileserver": None}


@mcp.tool()
def mojo_enable_js(gen_dir: Optional[str] = None, serve_port: int = 8089,
                   extra_flags: Optional[list[str]] = None) -> dict:
    """Restart the browser with MojoJS bindings (`--enable-blink-features=
    MojoJS,MojoJSTest`). If *gen_dir* is given, serve it on 10.0.2.2:port
    so the page can `importScripts` the Mojo JS bindings."""
    from harness_android import mojo
    b = _require_browser()
    if isinstance(b, dict): return b
    fs = mojo.enable_mojojs(b, gen_dir=gen_dir, serve_port=serve_port,
                            extra_flags=tuple(extra_flags or ()))
    if fs is not None:
        _mojo_instance["fileserver"] = fs
    return {"ok": True, "gen_dir_served": gen_dir is not None}


@mcp.tool()
def mojo_trigger_all(origin: Optional[str] = None) -> dict:
    """Call every Mojo-backed Web API (Clipboard, Gamepad, Serial, USB, …)
    from the currently-loaded page and return per-API results."""
    from harness_android.mojo import MojoTracer
    b = _require_browser()
    if isinstance(b, dict): return b
    tr = MojoTracer(b)
    return {"results": _asdict(tr.trigger_all_apis(origin=origin))}


@mcp.tool()
def mojo_trigger_selected(names: list[str]) -> dict:
    """Trigger only the named Mojo APIs."""
    from harness_android.mojo import MojoTracer
    b = _require_browser()
    if isinstance(b, dict): return b
    tr = MojoTracer(b)
    return {"results": _asdict(tr.trigger_selected_apis(*names))}


@mcp.tool()
def mojo_trace_start() -> dict:
    """Start a Chrome tracing session scoped to Mojo categories."""
    from harness_android.mojo import MojoTracer
    if _mojo_instance["tracer"] is not None:
        return {"error": "already tracing; call mojo_trace_stop first"}
    b = _require_browser()
    if isinstance(b, dict): return b
    tr = MojoTracer(b)
    tr.start_trace()
    _mojo_instance["tracer"] = tr
    return {"ok": True}


@mcp.tool()
def mojo_trace_stop(dump_path: str = "mojo_trace.json",
                   timeout: float = 60.0) -> dict:
    """Stop the trace, extract Mojo IPC messages, dump to *dump_path*."""
    tr = _mojo_instance["tracer"]
    if tr is None:
        return {"error": "not tracing"}
    events = tr.stop_trace(timeout=timeout)
    msgs = tr.extract_mojo_messages(events)
    tr.dump(path=dump_path, events=events, messages=msgs)
    _mojo_instance["tracer"] = None
    return {"ok": True, "path": dump_path, "message_count": len(msgs)}


# --------------------------------------------------------------------------
# Pentest script
# --------------------------------------------------------------------------

@mcp.tool()
def pentest_run(script_path: str) -> dict:
    """Run a pentest script (must expose `def run(ctx):`). Uses the
    currently-attached browser session."""
    from harness_android import pentest
    b = _require_browser()
    if isinstance(b, dict): return b
    ctx = pentest.run_script(script_path, S.ensure_adb(), b)
    findings = [
        {"title": f.get("title"), "severity": f.get("severity"),
         "description": f.get("description", "")[:200]}
        for f in getattr(ctx, "findings", [])
    ]
    return {"ok": True, "findings": findings}


# --------------------------------------------------------------------------
# Local HTTP file server (for Mojo gen_dir, payload hosting)
# --------------------------------------------------------------------------

_fileserver_instance: dict[str, Any] = {"obj": None}


@mcp.tool()
def fileserver_start(directory: str, port: int = 8089,
                     bind: str = "0.0.0.0") -> dict:
    """Serve *directory* over HTTP, reachable from the emulator at
    `http://10.0.2.2:<port>/`. Use for MojoJS bindings or hosting payloads."""
    from harness_android.fileserver import FileServer
    if _fileserver_instance["obj"] is not None:
        return {"error": "already running; call fileserver_stop"}
    fs = FileServer(directory, port=port, bind=bind)
    fs.start()
    _fileserver_instance["obj"] = fs
    return {"ok": True, "url": f"http://10.0.2.2:{port}/",
            "directory": directory}


@mcp.tool()
def fileserver_stop() -> dict:
    """Stop the local HTTP file server."""
    fs = _fileserver_instance["obj"]
    if fs is None:
        return {"error": "not running"}
    fs.stop()
    _fileserver_instance["obj"] = None
    return {"ok": True}


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
