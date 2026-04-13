"""WebView enumeration: discover and connect to debuggable WebViews in any app.

Every Android app with debuggable WebViews exposes abstract UNIX sockets
named ``webview_devtools_remote_<PID>``.  This module enumerates them,
identifies the owning process/package, and connects via CDP.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import requests
from rich.console import Console
from rich.table import Table

from harness_android.adb import ADB
from harness_android.browser import Browser

console = Console()


@dataclass
class WebViewTarget:
    """A discovered debuggable WebView socket."""
    socket_name: str       # e.g. "webview_devtools_remote_12345"
    pid: int
    package: str = ""
    app_name: str = ""
    pages: list[dict[str, str]] = field(default_factory=list)
    local_port: int = 0    # assigned when forwarded


def enumerate_webviews(adb: ADB) -> list[WebViewTarget]:
    """Find all debuggable DevTools sockets on the device.

    Scans ``/proc/net/unix`` for sockets matching the DevTools naming
    convention and resolves each PID to its package name.
    """
    output = adb.shell("cat", "/proc/net/unix")

    # Match both chrome_devtools_remote and webview_devtools_remote_<PID>
    socket_pattern = re.compile(r"((?:webview_devtools_remote|chrome_devtools_remote)(?:_(\d+))?)\s*$")

    targets: list[WebViewTarget] = []
    seen_sockets: set[str] = set()

    for line in output.splitlines():
        m = socket_pattern.search(line)
        if m:
            socket_name = m.group(1)
            if socket_name in seen_sockets:
                continue
            seen_sockets.add(socket_name)

            pid = int(m.group(2)) if m.group(2) else 0

            target = WebViewTarget(socket_name=socket_name, pid=pid)

            # Resolve PID to package name
            if pid:
                cmdline = adb.run(
                    "shell", f"cat /proc/{pid}/cmdline 2>/dev/null",
                    check=False, timeout=5,
                ).stdout.strip().split("\x00")[0]
                target.package = cmdline
            elif "chrome_devtools_remote" in socket_name:
                target.package = "com.android.chrome"

            targets.append(target)

    console.print(f"[green]Found {len(targets)} debuggable WebView socket(s)")
    return targets


def forward_and_query(adb: ADB, target: WebViewTarget, base_port: int = 9300) -> WebViewTarget:
    """Forward a WebView socket to a local port and query its /json endpoint."""
    # Pick a port that doesn't collide
    local_port = base_port + target.pid % 100

    adb.run(
        "forward",
        f"tcp:{local_port}",
        f"localabstract:{target.socket_name}",
    )
    target.local_port = local_port

    # Query the /json endpoint
    time.sleep(0.5)
    try:
        resp = requests.get(f"http://localhost:{local_port}/json", timeout=5)
        if resp.status_code == 200:
            target.pages = resp.json()
    except Exception:  # noqa: BLE001
        pass

    return target


def list_all_webviews(adb: ADB) -> list[WebViewTarget]:
    """Enumerate all WebViews and query their pages."""
    targets = enumerate_webviews(adb)
    for i, target in enumerate(targets):
        forward_and_query(adb, target, base_port=9300 + i * 10)
    return targets


def connect_to_webview(adb: ADB, target: WebViewTarget) -> Browser:
    """Create a Browser (CDP) instance connected to a specific WebView.

    The returned Browser object has the same API as the Chrome browser
    controller — navigate, evaluate JS, take screenshots, etc.

    For Chrome's own ``chrome_devtools_remote`` socket, the full
    ``enable_cdp()`` setup is used (restart Chrome, ensure an active tab).
    """
    if not target.local_port:
        forward_and_query(adb, target)

    browser = Browser(adb, local_port=target.local_port)

    if target.socket_name == "chrome_devtools_remote":
        # Chrome needs enable_cdp() to have an active tab + working CDP.
        # Without it, the socket exists but commands go nowhere.
        browser.enable_cdp()
        browser.connect()
    else:
        # Third-party WebViews are already running — just connect.
        browser._ws = None
        browser._msg_id = 0
        browser.connect()

    console.print(
        f"[green]Connected to WebView in {target.package} "
        f"(PID {target.pid}, port {target.local_port})"
    )
    return browser


def connect_webview(adb: ADB, socket_name: str, local_port: int = 9300) -> Browser:
    """Convenience wrapper: look up a WebView by socket name and connect.

    This is the entry point used by the CLI ``webview connect`` command.
    """
    targets = enumerate_webviews(adb)
    for target in targets:
        if target.socket_name == socket_name:
            if local_port:
                target.local_port = 0  # force re-forward
                forward_and_query(adb, target, base_port=local_port)
            return connect_to_webview(adb, target)

    raise SystemExit(f"Socket '{socket_name}' not found on device.")


def cleanup_forwards(adb: ADB, targets: list[WebViewTarget]) -> None:
    """Remove all ADB forwards set up for WebView targets."""
    for target in targets:
        if target.local_port:
            adb.forward_remove(target.local_port)


def print_webviews(targets: list[WebViewTarget]) -> None:
    if not targets:
        console.print("[dim]No debuggable WebViews found.")
        return

    t = Table(title=f"Debuggable WebViews ({len(targets)})", show_lines=True)
    t.add_column("Socket", style="bold")
    t.add_column("PID")
    t.add_column("Package")
    t.add_column("Port")
    t.add_column("Pages")
    for wv in targets:
        page_info = ""
        for p in wv.pages[:3]:
            title = p.get("title", "")[:30]
            url = p.get("url", "")[:50]
            page_info += f"{title} ({url})\n"
        if len(wv.pages) > 3:
            page_info += f"… and {len(wv.pages) - 3} more"
        t.add_row(
            wv.socket_name,
            str(wv.pid),
            wv.package or "[dim]unknown",
            str(wv.local_port) if wv.local_port else "",
            page_info.strip() or "[dim]no pages",
        )
    console.print(t)
