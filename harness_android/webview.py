"""WebView enumeration: discover and connect to debuggable WebViews."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import requests
from rich.console import Console
from rich.table import Table

from harness_android.adb import ADB, poll_until
from harness_android.browser import Browser

console = Console()

_SOCKET_RE = re.compile(
    r"((?:webview_devtools_remote|chrome_devtools_remote|\S+_devtools_remote)(?:_(\d+))?)$"
)


@dataclass
class WebViewTarget:
    socket_name: str
    pid: int = 0
    package: str = ""
    pages: list[dict] = field(default_factory=list)
    local_port: int = 0


def enumerate_webviews(adb: ADB) -> list[WebViewTarget]:
    """Find all debuggable DevTools sockets on the device."""
    targets: list[WebViewTarget] = []
    seen: set[str] = set()
    for sock in adb.list_abstract_sockets("devtools_remote"):
        m = _SOCKET_RE.match(sock)
        if not m or sock in seen:
            continue
        seen.add(sock)
        pid = int(m.group(2)) if m.group(2) else 0
        t = WebViewTarget(socket_name=sock, pid=pid)
        if pid:
            cmdline = adb.run(
                "shell", "cat", f"/proc/{pid}/cmdline",
                check=False, timeout=5,
            ).stdout
            t.package = cmdline.split("\x00", 1)[0].strip()
        elif "chrome" in sock:
            t.package = "com.android.chrome"
        targets.append(t)
    console.print(f"[green]Found {len(targets)} debuggable DevTools socket(s)")
    return targets


def forward_and_query(adb: ADB, target: WebViewTarget, local_port: int) -> WebViewTarget:
    """Forward a WebView socket and poll its /json endpoint until ready."""
    adb.forward_remove(local_port)
    adb.forward(local_port, f"localabstract:{target.socket_name}")
    target.local_port = local_port

    def _query() -> list | None:
        r = requests.get(f"http://127.0.0.1:{local_port}/json", timeout=3)
        return r.json() if r.status_code == 200 else None

    try:
        target.pages = poll_until(_query, timeout=10, interval=0.3,
                                  desc=f"/json on {target.socket_name}")
    except TimeoutError as exc:
        console.print(f"[yellow]{target.socket_name}: {exc}")
        target.pages = []
    return target


def list_all_webviews(adb: ADB, base_port: int = 9300) -> list[WebViewTarget]:
    targets = enumerate_webviews(adb)
    for i, t in enumerate(targets):
        forward_and_query(adb, t, local_port=base_port + i)
    return targets


def connect_to_webview(adb: ADB, target: WebViewTarget) -> Browser:
    """Return a connected :class:`Browser` for the given WebView socket."""
    if not target.local_port:
        forward_and_query(adb, target, local_port=9300)
    b = Browser(adb, local_port=target.local_port,
                browser=target.package or "chrome")
    b.connect()
    console.print(
        f"[green]Connected to WebView in {target.package or '?'} "
        f"(PID {target.pid}, port {target.local_port})"
    )
    return b


def connect_webview(adb: ADB, socket_name: str, local_port: int = 9300) -> Browser:
    for t in enumerate_webviews(adb):
        if t.socket_name == socket_name:
            forward_and_query(adb, t, local_port=local_port)
            return connect_to_webview(adb, t)
    raise SystemExit(f"Socket '{socket_name}' not found on device.")


def cleanup_forwards(adb: ADB, targets: list[WebViewTarget]) -> None:
    for t in targets:
        if t.local_port:
            adb.forward_remove(t.local_port)


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
        info = "\n".join(
            f"{p.get('title','')[:30]} ({p.get('url','')[:50]})" for p in wv.pages[:3]
        )
        if len(wv.pages) > 3:
            info += f"\n… and {len(wv.pages) - 3} more"
        t.add_row(wv.socket_name, str(wv.pid) or "—",
                  wv.package or "[dim]unknown",
                  str(wv.local_port) if wv.local_port else "",
                  info or "[dim]no pages")
    console.print(t)
