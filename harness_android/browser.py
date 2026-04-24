"""Control Chrome/Edge on Android via ADB intents + Chrome DevTools Protocol.

Architecture
------------
The CDP client maintains **two** WebSocket connections:

* **page target** — the inspectable tab. ``Runtime.*``, ``Page.*``,
  ``Tracing.*``, ``Input.*`` go here.
* **browser target** — the browser process itself
  (``ws://…/devtools/browser``). ``Browser.*`` and ``Target.*`` commands
  (grant permissions, create tabs, observe crashes) go here.

Every incoming WebSocket frame is either a *response* (has ``id``) or an
*event* (has ``method``).  Responses are returned to the caller; events
are pushed onto an internal deque so callers can :meth:`wait_event` for
them later — nothing is silently dropped.
"""

from __future__ import annotations

import collections
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Optional

import requests
import websocket

from harness_android.adb import ADB, poll_until
from harness_android.config import CDP_LOCAL_PORT
from harness_android.console import console


# ----------------------------------------------------------------------
# Browser presets
# ----------------------------------------------------------------------

@dataclass(frozen=True)
class BrowserSpec:
    name: str
    package: str
    activity: str
    devtools_socket: str
    cmdline_files: tuple[str, ...]
    #: Flags added to every cmdline write, *before* any user-supplied
    #: ``extra_flags`` / ``chrome_flags``.  Use this to flip features
    #: that are only honoured on debuggable builds (e.g. MojoJS).
    #: Release Chrome silently ignores unknown flags, so leaving these
    #: on for non-debuggable presets is a no-op.
    default_flags: tuple[str, ...] = ()


BROWSERS: dict[str, BrowserSpec] = {
    "chrome": BrowserSpec(
        name="chrome",
        package="com.android.chrome",
        activity="com.google.android.apps.chrome.Main",
        devtools_socket="chrome_devtools_remote",
        cmdline_files=(
            "/data/local/tmp/chrome-command-line",
            "/data/local/tmp/com.android.chrome-command-line",
        ),
    ),
    "chromium": BrowserSpec(
        name="chromium",
        package="org.chromium.chrome",
        activity="com.google.android.apps.chrome.Main",
        devtools_socket="chrome_devtools_remote",
        cmdline_files=(
            "/data/local/tmp/chrome-command-line",
            "/data/local/tmp/org.chromium.chrome-command-line",
        ),
    ),
    "edge": BrowserSpec(
        name="edge",
        package="com.microsoft.emmx",
        activity="com.microsoft.ruby.Main",
        devtools_socket="chrome_devtools_remote",
        cmdline_files=(
            "/data/local/tmp/microsoft-edge-command-line",
            "/data/local/tmp/com.microsoft.emmx-command-line",
            "/data/local/tmp/chrome-command-line",
        ),
        # MojoJS on every edge-* preset. Release Edge (non-debuggable)
        # silently ignores the flag, so it's a no-op there; on
        # debuggable builds it exposes Mojo.bindInterface + MojoJSTest.
        default_flags=("--enable-blink-features=MojoJS,MojoJSTest",),
    ),
    "edge-canary": BrowserSpec(
        name="edge-canary",
        package="com.microsoft.emmx.canary",
        activity="com.microsoft.ruby.Main",
        devtools_socket="chrome_devtools_remote",
        cmdline_files=(
            "/data/local/tmp/microsoft-edge-canary-command-line",
            "/data/local/tmp/com.microsoft.emmx.canary-command-line",
            "/data/local/tmp/chrome-command-line",
        ),
        default_flags=("--enable-blink-features=MojoJS,MojoJSTest",),
    ),
    "edge-dev": BrowserSpec(
        name="edge-dev",
        package="com.microsoft.emmx.dev",
        activity="com.microsoft.ruby.Main",
        devtools_socket="chrome_devtools_remote",
        cmdline_files=(
            "/data/local/tmp/microsoft-edge-dev-command-line",
            "/data/local/tmp/com.microsoft.emmx.dev-command-line",
            "/data/local/tmp/chrome-command-line",
        ),
        default_flags=("--enable-blink-features=MojoJS,MojoJSTest",),
    ),
    "edge-local": BrowserSpec(
        name="edge-local",
        package="com.microsoft.emmx.local",
        activity="com.microsoft.ruby.Main",
        devtools_socket="chrome_devtools_remote",
        cmdline_files=(
            "/data/local/tmp/microsoft-edge-local-command-line",
            "/data/local/tmp/com.microsoft.emmx.local-command-line",
            "/data/local/tmp/chrome-command-line",
        ),
        # edge-local is our primary pentest build (debuggable). Enable
        # MojoJS by default so `Mojo.bindInterface` + MojoJSTest test
        # APIs are reachable from every page, including privileged
        # edge:// pages, without having to pass --chrome-flags every run.
        default_flags=("--enable-blink-features=MojoJS,MojoJSTest",),
    ),
}


def _apply_config_overrides(spec: BrowserSpec) -> BrowserSpec:
    """Apply ``[browsers.<name>]`` overrides from ``harness.toml`` if any.

    Every field on :class:`BrowserSpec` is optionally overridable:

    .. code-block:: toml

        [browsers.edge-local]
        package       = "com.microsoft.emmx.local"
        activity      = "com.microsoft.ruby.Main"
        cmdline_files = [
            "/data/local/tmp/microsoft-edge-local-command-line",
            "/data/local/tmp/chrome-command-line",
        ]
        default_flags = ["--enable-blink-features=MojoJS,MojoJSTest"]

    Unspecified fields inherit from the built-in preset. Unknown keys
    are ignored (emit no warning \u2014 TOML typos would otherwise be
    silently ineffective and users expect forward-compat). Tuple-typed
    fields accept TOML arrays.
    """
    try:
        from harness_android.config import load_config
        cfg = load_config()
    except Exception:  # noqa: BLE001
        return spec
    browsers_cfg = cfg.get("browsers") or {}
    override = browsers_cfg.get(spec.name) or {}
    if not override:
        return spec
    import dataclasses
    allowed = {f.name for f in dataclasses.fields(BrowserSpec)}
    kwargs = {}
    for k, v in override.items():
        if k not in allowed:
            continue
        # Preserve dataclass types: cmdline_files / default_flags are tuples.
        if k in ("cmdline_files", "default_flags") and isinstance(v, list):
            v = tuple(v)
        kwargs[k] = v
    if not kwargs:
        return spec
    return dataclasses.replace(spec, **kwargs)


def resolve_browser(name_or_pkg: str | None) -> BrowserSpec:
    """Resolve a preset name or package string to a :class:`BrowserSpec`.

    When ``name_or_pkg`` is ``None`` the harness consults
    ``harness.toml``'s ``default_browser`` (falling back to ``chrome``
    for backwards-compatibility) so users can change the implicit
    default without passing ``-b`` on every command.

    Any matching ``[browsers.<name>]`` table in ``harness.toml`` is
    layered on top of the built-in preset, so users can override
    package, activity, cmdline files or default flags without editing
    source.
    """
    if not name_or_pkg:
        try:
            from harness_android.config import load_config
            name_or_pkg = load_config().get("default_browser", "chrome")
        except Exception:  # noqa: BLE001
            name_or_pkg = "chrome"
    if name_or_pkg in BROWSERS:
        return _apply_config_overrides(BROWSERS[name_or_pkg])
    for spec in BROWSERS.values():
        if spec.package == name_or_pkg:
            return _apply_config_overrides(spec)
    # Unknown package — assume Chromium-based defaults.
    return BrowserSpec(
        name=name_or_pkg,
        package=name_or_pkg,
        activity="com.google.android.apps.chrome.Main",
        devtools_socket="chrome_devtools_remote",
        cmdline_files=(
            "/data/local/tmp/chrome-command-line",
            f"/data/local/tmp/{name_or_pkg}-command-line",
        ),
    )


# ----------------------------------------------------------------------
# Low-level CDP connection
# ----------------------------------------------------------------------

class TargetCrashed(RuntimeError):
    """Raised when the inspected target reports ``Inspector.targetCrashed``."""


class CDPError(RuntimeError):
    """Structured error for a non-zero CDP response.

    ``code`` / ``message`` come from the remote-protocol error object so
    callers can match on ``-32000`` etc. without regex-parsing the message.
    """

    def __init__(self, method: str, error: dict):
        self.method = method
        self.code = error.get("code")
        self.message = error.get("message", "")
        self.data = error.get("data")
        super().__init__(f"CDP {method}: {error}")


def _preview_remote_object(ro: dict) -> Any:
    """Render a CDP ``Runtime.RemoteObject`` into something JSON-printable.

    Used when a caller asks for a value that can't be deep-cloned
    (``window``, DOM nodes, bridge objects, ...). We surface ``type``,
    ``className``, and the ``preview`` (properties list) so the REPL
    still produces useful output.
    """
    if not ro:
        return None
    out: dict[str, Any] = {"__cdp_type": ro.get("type")}
    if "subtype" in ro:
        out["__cdp_subtype"] = ro["subtype"]
    if "className" in ro:
        out["__cdp_class"] = ro["className"]
    if "description" in ro:
        out["__cdp_desc"] = ro["description"]
    preview = ro.get("preview") or {}
    props = preview.get("properties") or []
    if props:
        out["__cdp_properties"] = {
            p.get("name"): p.get("value") if "value" in p else f"<{p.get('type')}>"
            for p in props
        }
        if preview.get("overflow"):
            out["__cdp_truncated"] = True
    return out


@dataclass
class _CDPSession:
    """A single CDP WebSocket with response/event demultiplexing."""

    url: str
    timeout: float = 30.0
    _ws: Optional[websocket.WebSocket] = None
    _msg_id: int = 0
    _events: Deque[dict] = field(default_factory=collections.deque)
    _crashed: bool = False

    def connect(self) -> None:
        self._ws = websocket.create_connection(self.url, timeout=self.timeout)

    def close(self) -> None:
        if self._ws:
            try:
                self._ws.close()
            except Exception:  # noqa: BLE001
                pass
            self._ws = None

    @property
    def connected(self) -> bool:
        return self._ws is not None

    def _recv(self, timeout: float) -> dict | None:
        """Receive one frame, classify, and return it (or None on timeout)."""
        assert self._ws is not None
        self._ws.settimeout(timeout)
        try:
            raw = self._ws.recv()
        except websocket.WebSocketTimeoutException:
            return None
        except websocket.WebSocketConnectionClosedException as exc:
            self._ws = None
            raise TargetCrashed("CDP connection closed by remote") from exc
        if not raw:
            return None
        msg = json.loads(raw)
        if msg.get("method") == "Inspector.targetCrashed":
            self._crashed = True
        return msg

    def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict:
        """Send a command and return its result. Events received while
        waiting are buffered, not dropped."""
        if self._ws is None:
            self.connect()
        assert self._ws is not None
        self._msg_id += 1
        req_id = self._msg_id
        self._ws.send(json.dumps({"id": req_id, "method": method, "params": params or {}}))

        deadline = time.monotonic() + (timeout or self.timeout)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"CDP {method} (id={req_id}) timed out after {timeout or self.timeout:.0f}s")
            msg = self._recv(min(remaining, 5.0))
            if msg is None:
                continue
            if "id" in msg:
                if msg["id"] == req_id:
                    if "error" in msg:
                        raise CDPError(method, msg["error"])
                    return msg.get("result", {})
                # Response to an earlier command we already gave up on; drop.
                continue
            # Event — buffer it.
            self._events.append(msg)
            if self._crashed:
                raise TargetCrashed(f"Target crashed while waiting for {method}")

    def wait_event(
        self,
        method: str | Callable[[dict], bool],
        *,
        timeout: float = 30.0,
    ) -> dict:
        """Block until an event matching *method* arrives (or is already buffered)."""
        pred: Callable[[dict], bool]
        if callable(method):
            pred = method
        else:
            pred = lambda m: m.get("method") == method  # noqa: E731

        # Check buffer first.
        for i, ev in enumerate(self._events):
            if pred(ev):
                del self._events[i]
                return ev

        if self._ws is None:
            raise RuntimeError("wait_event() on a closed session")

        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"CDP event {method!r} not received within {timeout:.0f}s")
            msg = self._recv(min(remaining, 5.0))
            if msg is None:
                continue
            if "id" in msg:
                continue  # stray response
            if pred(msg):
                return msg
            self._events.append(msg)
            if self._crashed:
                raise TargetCrashed("Target crashed while waiting for event")

    def drain_events(self, method: str | None = None) -> list[dict]:
        """Return and clear buffered events (optionally filtered by method)."""
        if method is None:
            out = list(self._events)
            self._events.clear()
            return out
        out, keep = [], collections.deque()
        for ev in self._events:
            (out if ev.get("method") == method else keep).append(ev)
        self._events = keep
        return out


# ----------------------------------------------------------------------
# High-level Browser controller
# ----------------------------------------------------------------------

class Browser:
    """Remote-control a Chromium-based browser running on the device."""

    def __init__(
        self,
        adb: ADB,
        *,
        local_port: int = CDP_LOCAL_PORT,
        browser: str | BrowserSpec = "chrome",
        extra_flags: Optional[list[str]] = None,
        timeout: float = 30.0,
    ):
        self.adb = adb
        self.local_port = local_port
        self.spec = browser if isinstance(browser, BrowserSpec) else resolve_browser(browser)
        self.timeout = timeout
        self._extra_chrome_flags: list[str] = list(extra_flags or [])
        self._page: Optional[_CDPSession] = None
        self._browser: Optional[_CDPSession] = None
        self._frame_id: Optional[str] = None
        #: Response headers of the most recent main-frame navigation.
        #: Populated by :meth:`navigate` when the Network domain is enabled,
        #: consumed by :mod:`harness_android.recon` for security-header /
        #: CSP analysis without re-issuing a HEAD request (which would
        #: follow a different cache / middleware path and miss CORS-filtered
        #: headers on cross-origin documents).
        self.main_frame_response_headers: dict[str, str] = {}

    # Backward-compat aliases used by other modules / cli
    @property
    def package(self) -> str:
        return self.spec.package

    @property
    def activity(self) -> str:
        return self.spec.activity

    @property
    def _ws(self):  # legacy access used by older callers
        return self._page._ws if self._page else None

    # ------------------------------------------------------------------
    # Intent-based control
    # ------------------------------------------------------------------

    def open_url(self, url: str) -> None:
        self.adb.launch_url(url)
        console.print(f"[green]Opened {url}")

    def open_chrome(self) -> None:
        self.adb.launch_activity(f"{self.spec.package}/{self.spec.activity}")

    def clear_data(self) -> None:
        self.adb.shell("pm", "clear", self.spec.package)

    def force_stop(self) -> None:
        self.adb.shell("am", "force-stop", self.spec.package)

    # ------------------------------------------------------------------
    # CDP setup
    # ------------------------------------------------------------------

    def add_flags(self, *flags: str) -> None:
        self._extra_chrome_flags.extend(flags)

    def _write_chrome_flags(self) -> None:
        flags = " ".join([
            "_",  # argv[0] placeholder
            "--disable-fre",
            "--no-default-browser-check",
            "--no-first-run",
            "--remote-debugging-port=0",
            "--remote-allow-origins=*",
            *self.spec.default_flags,
            *self._extra_chrome_flags,
        ])
        for dest in self.spec.cmdline_files:
            self.adb.write_file(dest, flags + "\n")
        # On user builds Chrome only honours the cmdline file when it is the
        # debug app; on userdebug/eng this is harmless.
        self.adb.run("shell", "am", "set-debug-app", "--persistent", self.spec.package, check=False)
        self.adb.run("shell", "setprop", "debug.com.android.chrome.enable_test_features", "1", check=False)
        console.print("[dim]Browser command-line flags written.")

    def _wait_for_devtools_socket(self, timeout: float = 30) -> str:
        """Poll until the browser's DevTools abstract socket appears."""
        prefix = self.spec.devtools_socket

        def _find() -> str | None:
            for s in self.adb.list_abstract_sockets("devtools_remote"):
                if s == prefix or s.startswith(prefix):
                    return s
            return None

        return poll_until(_find, timeout=timeout, interval=0.5,
                          desc=f"DevTools socket '{prefix}'")

    def prepare_cdp(self) -> None:
        """Write CDP command-line flags without restarting the browser.

        Call this *before* launching the browser (or after install) so the
        next natural launch picks up the debug flags.  The browser keeps
        its current state — NTP, mini apps, etc. are not disrupted.

        Follow up with :meth:`attach_cdp` once the browser is running.
        """
        self._write_chrome_flags()

    def attach_cdp(self, timeout: float = 60) -> None:
        """Connect to an already-running browser's DevTools socket.

        Unlike :meth:`enable_cdp`, this does **not** restart the browser.
        Use after :meth:`prepare_cdp` + a natural launch, or when the
        browser is already running with debug flags.
        """
        socket_name = self._wait_for_devtools_socket(timeout=timeout / 2)
        self.adb.forward(self.local_port, f"localabstract:{socket_name}")

        poll_until(
            lambda: requests.get(
                f"http://127.0.0.1:{self.local_port}/json/version", timeout=3
            ).status_code == 200,
            timeout=timeout / 2,
            interval=0.5,
            desc="CDP /json/version",
        )
        console.print(f"[green]CDP attached — http://127.0.0.1:{self.local_port}")

    def enable_cdp(self, timeout: float = 60, url: str = "about:blank") -> None:
        """Start the browser with remote debugging and forward the socket.

        *url*: initial URL to open. Use ``None`` to let the browser open its
        default page (e.g. the native NTP in Edge).
        """
        if not self.adb.is_installed(self.spec.package):
            raise RuntimeError(
                f"{self.spec.package} is not installed on the device. "
                "Run `harness-android install-chromium` or install your APK."
            )

        self._write_chrome_flags()
        self.force_stop()
        # `-W` blocks until the activity reports launched — no fixed sleep.
        cmd = [
            "am", "start", "-W",
            "-n", f"{self.spec.package}/{self.spec.activity}",
        ]
        if url:
            cmd.extend(["-d", url])
        self.adb.shell(*cmd)

        socket_name = self._wait_for_devtools_socket(timeout=timeout / 2)
        self.adb.forward(self.local_port, f"localabstract:{socket_name}")

        poll_until(
            lambda: requests.get(
                f"http://127.0.0.1:{self.local_port}/json/version", timeout=3
            ).status_code == 200,
            timeout=timeout / 2,
            interval=0.5,
            desc="CDP /json/version",
        )
        console.print(f"[green]CDP enabled — http://127.0.0.1:{self.local_port}")

    def disable_cdp(self) -> None:
        self.close()
        self.adb.forward_remove(self.local_port)

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def _http(self, path: str) -> Any:
        r = requests.get(f"http://127.0.0.1:{self.local_port}{path}", timeout=10)
        r.raise_for_status()
        return r.json()

    def list_targets(self) -> list[dict]:
        return self._http("/json/list")

    def find_target(
        self,
        *,
        url_substring: str | None = None,
        target_id: str | None = None,
    ) -> str | None:
        """Look up a CDP page target by exact id or URL substring.

        Returns the target id, or ``None`` if nothing matches. Pass no args
        to have :meth:`connect` fall back to its default page selection.
        """
        if target_id:
            for t in self.list_targets():
                if t.get("id") == target_id:
                    return t.get("id")
            return None
        if url_substring:
            for t in self.list_targets():
                if t.get("type") == "page" and url_substring in (t.get("url") or ""):
                    return t.get("id")
            return None
        return None

    def _ensure_page_target(self) -> dict:
        """Return a page target, creating one via /json/new if none exists."""
        def _find() -> dict | None:
            targets = [
                t for t in self.list_targets()
                if t.get("type") == "page" and t.get("webSocketDebuggerUrl")
            ]
            # Prefer targets with a real URL over empty/unresponsive ones.
            for t in targets:
                if t.get("url"):
                    return t
            return targets[0] if targets else None

        page = _find()
        if page:
            return page
        # No page yet — ask the browser to open one.
        requests.put(
            f"http://127.0.0.1:{self.local_port}/json/new?about:blank", timeout=10
        )
        return poll_until(_find, timeout=10, interval=0.3, desc="inspectable page")

    def connect(self, target_id: str | None = None) -> None:
        """Open page + browser CDP sessions."""
        version = self._http("/json/version")
        browser_ws = version.get("webSocketDebuggerUrl")
        if browser_ws:
            self._browser = _CDPSession(browser_ws, timeout=self.timeout)
            self._browser.connect()

        if target_id:
            page = next(
                (t for t in self.list_targets() if t.get("id") == target_id), None
            )
            if not page:
                raise RuntimeError(f"Target {target_id} not found")
        else:
            page = self._ensure_page_target()

        self._page = _CDPSession(page["webSocketDebuggerUrl"], timeout=self.timeout)
        self._page.connect()
        self._page.send("Page.enable")
        self._page.send("Runtime.enable")
        self._page.send("Inspector.enable")
        tree = self._page.send("Page.getFrameTree")
        self._frame_id = tree.get("frameTree", {}).get("frame", {}).get("id")
        console.print(f"[green]CDP connected → {page.get('url', '?')}")

    def reconnect(self, *, fresh_tab: bool = True) -> None:
        """Re-establish CDP after a renderer crash.

        With *fresh_tab* (default), a new about:blank tab is opened so we
        don't re-attach to a target whose renderer was killed (Aw-Snap).
        """
        self.close()
        target_id: str | None = None
        if fresh_tab:
            try:
                resp = requests.put(
                    f"http://127.0.0.1:{self.local_port}/json/new?about:blank",
                    timeout=10,
                )
                target_id = resp.json().get("id")
            except Exception:  # noqa: BLE001
                pass
        self.connect(target_id=target_id)

    def close(self) -> None:
        for sess in (self._page, self._browser):
            if sess:
                sess.close()
        self._page = None
        self._browser = None

    def is_alive(self) -> bool:
        """Return True if the page session is connected and not crashed."""
        return bool(self._page and self._page.connected and not self._page._crashed)

    # ------------------------------------------------------------------
    # Raw CDP
    # ------------------------------------------------------------------

    def send(self, method: str, params: dict | None = None, *, timeout: float | None = None) -> dict:
        """Send a CDP command on the appropriate session."""
        domain = method.split(".", 1)[0]
        if domain in ("Browser", "Target", "SystemInfo", "Storage") and self._browser:
            return self._browser.send(method, params, timeout=timeout)
        if self._page is None:
            self.connect()
        assert self._page is not None
        return self._page.send(method, params, timeout=timeout)

    def wait_event(self, method: str, *, timeout: float = 30.0) -> dict:
        if self._page is None:
            raise RuntimeError("Not connected")
        return self._page.wait_event(method, timeout=timeout)

    def drain_events(self, method: str | None = None) -> list[dict]:
        return self._page.drain_events(method) if self._page else []

    def enable_domains(self) -> None:
        """No-op kept for backward compatibility — domains are enabled in connect()."""

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    def navigate(self, url: str, *, wait: bool = True, timeout: float = 30.0) -> dict:
        """Navigate the current tab to *url* and (by default) wait for load."""
        assert self._page is not None or not wait
        if self._page:
            self._page.drain_events()  # discard stale Page.* / Network.* events
        # Opt into Network events so we can capture the main-frame response
        # headers for security-header / CSP audits.  Enabling is idempotent
        # and cheap; if the domain was disabled manually the caller can
        # re-disable it after we're done.
        try:
            self.send("Network.enable")
        except Exception:  # noqa: BLE001
            pass
        self.main_frame_response_headers = {}
        result = self.send("Page.navigate", {"url": url})
        if err := result.get("errorText"):
            raise RuntimeError(f"Navigation to {url} failed: {err}")
        if wait:
            self.wait_for_load(timeout=timeout)
            # Drain buffered Network.responseReceived and pick the main-frame
            # Document response.  Anything later (subresources, AJAX) can be
            # fetched separately via drain_events in caller code.
            for ev in self.drain_events("Network.responseReceived"):
                params = ev.get("params", {}) or {}
                if params.get("type") == "Document":
                    response = params.get("response", {}) or {}
                    hdrs = response.get("headers", {}) or {}
                    self.main_frame_response_headers = {
                        str(k): str(v) for k, v in hdrs.items()
                    }
                    break
        console.print(f"[green]Navigated to {url}")
        return result

    def wait_for_load(self, *, timeout: float = 30.0) -> None:
        """Block until ``Page.loadEventFired`` (falls back to readyState poll)."""
        try:
            self.wait_event("Page.loadEventFired", timeout=timeout)
            return
        except TimeoutError:
            pass
        poll_until(
            lambda: self.evaluate_js("document.readyState") == "complete",
            timeout=5, interval=0.3, desc="document.readyState == complete",
        )

    def reload(self, *, wait: bool = True, timeout: float = 30.0) -> None:
        if self._page:
            self._page.drain_events()
        self.send("Page.reload", {"ignoreCache": True})
        if wait:
            self.wait_for_load(timeout=timeout)

    # ------------------------------------------------------------------
    # JS evaluation
    # ------------------------------------------------------------------

    def evaluate_js(
        self,
        expression: str,
        *,
        await_promise: bool = False,
        timeout: float | None = None,
        return_by_value: bool = True,
    ) -> Any:
        """Evaluate *expression* in the page context.

        With ``return_by_value=True`` (default) Chrome deep-clones the result
        to JSON. For values that contain circular references (``window``,
        ``document``, DOM nodes, many frameworks' bridge objects) Chrome
        responds with ``-32000 "Object reference chain is too long"``. We
        catch that, fall back to ``returnByValue=False``, and return a
        structured preview dict — so the REPL never blows up on things like
        ``window``, ``document.body``, or ``sapphireWebViewBridge``.
        """
        try:
            result = self.send(
                "Runtime.evaluate",
                {
                    "expression": expression,
                    "returnByValue": return_by_value,
                    "awaitPromise": await_promise,
                    "generatePreview": not return_by_value,
                },
                timeout=timeout,
            )
        except CDPError as exc:
            # -32000 "Object reference chain is too long" — fall back.
            if return_by_value and exc.code == -32000 and "reference chain" in (exc.message or ""):
                return self.evaluate_js(
                    expression,
                    await_promise=await_promise,
                    timeout=timeout,
                    return_by_value=False,
                )
            raise
        if exc := result.get("exceptionDetails"):
            desc = exc.get("exception", {}).get("description") or exc.get("text")
            raise RuntimeError(f"JS exception: {desc}")
        ro = result.get("result", {})
        if return_by_value:
            val = ro.get("value")
            # With returnByValue=True, DOM nodes / Window / Date / Map / etc.
            # serialize to ``{}`` indistinguishably from a literal ``{}``. If
            # we see that, re-issue by-ref + preview; real ``{}`` still comes
            # back as ``{}`` and non-trivial objects get a useful preview.
            if ro.get("type") == "object" and val in ({}, []):
                return self.evaluate_js(
                    expression,
                    await_promise=await_promise,
                    timeout=timeout,
                    return_by_value=False,
                )
            return val
        # By-ref: surface a readable preview instead of a RemoteObject handle.
        return _preview_remote_object(ro)

    def wait_for_expression(
        self,
        expression: str,
        *,
        timeout: float = 10.0,
        interval: float = 0.2,
    ) -> Any:
        """Poll ``expression`` until it is truthy or *timeout* elapses.

        Use this to defeat races where a page-injected global
        (``sapphireWebViewBridge``, ``__NEXT_DATA__``, frameworks, etc.)
        appears *after* ``Page.loadEventFired``. Returns the first truthy
        value; raises :class:`TimeoutError` otherwise.
        """
        deadline = time.monotonic() + timeout
        last: Any = None
        while time.monotonic() < deadline:
            try:
                last = self.evaluate_js(expression)
            except RuntimeError:
                last = None
            if last:
                return last
            time.sleep(interval)
        raise TimeoutError(f"wait_for_expression({expression!r}) timed out after {timeout}s")

    # ------------------------------------------------------------------
    # Permissions (browser target)
    # ------------------------------------------------------------------

    def grant_permissions(self, permissions: list[str], origin: str | None = None) -> None:
        if self._browser is None:
            console.print("[yellow]grant_permissions: no browser session — skipping")
            return
        params: dict[str, Any] = {"permissions": permissions}
        if origin:
            params["origin"] = origin
        self._browser.send("Browser.grantPermissions", params)

    # ------------------------------------------------------------------
    # Convenience helpers
    # ------------------------------------------------------------------

    def get_page_title(self) -> str:
        return self.evaluate_js("document.title") or ""

    def get_page_url(self) -> str:
        return self.evaluate_js("location.href") or ""

    def get_page_html(self) -> str:
        return self.evaluate_js("document.documentElement.outerHTML") or ""

    def page_screenshot_base64(self) -> str:
        return self.send("Page.captureScreenshot", {"format": "png"}).get("data", "")

    def page_screenshot(self, path: str) -> None:
        import base64
        data = self.page_screenshot_base64()
        with open(path, "wb") as f:
            f.write(base64.b64decode(data))
        console.print(f"[green]Page screenshot saved to {path}")

    def click_element(self, selector: str) -> None:
        self.evaluate_js(f"document.querySelector({json.dumps(selector)}).click()")

    def type_in_element(self, selector: str, text: str) -> None:
        js = (
            f"(()=>{{const el=document.querySelector({json.dumps(selector)});"
            f"el.focus();el.value={json.dumps(text)};"
            "el.dispatchEvent(new Event('input',{bubbles:true}));})()"
        )
        self.evaluate_js(js)

    def wait_for_selector(self, selector: str, timeout: float = 10) -> bool:
        try:
            poll_until(
                lambda: self.evaluate_js(f"!!document.querySelector({json.dumps(selector)})"),
                timeout=timeout, interval=0.2, desc=f"selector {selector}",
            )
            return True
        except TimeoutError:
            return False

    def get_cookies(self) -> list[dict]:
        return self.send("Network.getCookies").get("cookies", [])

    def clear_cookies(self) -> None:
        self.send("Network.clearBrowserCookies")

    def set_user_agent(self, ua: str) -> None:
        self.send("Network.setUserAgentOverride", {"userAgent": ua})

    def enable_network_logging(self) -> None:
        self.send("Network.enable")

    def enable_page_events(self) -> None:
        self.send("Page.enable")

    def enable_security(self) -> None:
        self.send("Security.enable")

    def get_security_state(self) -> dict:
        self.enable_security()
        return self.send("Security.getSecurityState")

    def override_certificate_errors(self, allow: bool = True) -> None:
        self.send("Security.setIgnoreCertificateErrors", {"ignore": allow})

    def inject_script_on_load(self, source: str) -> str:
        self.send("Page.enable")
        return self.send(
            "Page.addScriptToEvaluateOnNewDocument", {"source": source}
        ).get("identifier", "")

    def remove_injected_script(self, identifier: str) -> None:
        self.send("Page.removeScriptToEvaluateOnNewDocument", {"identifier": identifier})

    def get_response_body(self, request_id: str) -> bytes:
        import base64
        result = self.send("Network.getResponseBody", {"requestId": request_id})
        body = result.get("body", "")
        if result.get("base64Encoded"):
            return base64.b64decode(body)
        return body.encode()

    def emulate_device(
        self, width: int = 412, height: int = 915,
        device_scale_factor: float = 2.625, mobile: bool = True,
        user_agent: str | None = None,
    ) -> None:
        self.send("Emulation.setDeviceMetricsOverride", {
            "width": width, "height": height,
            "deviceScaleFactor": device_scale_factor, "mobile": mobile,
        })
        if user_agent:
            self.set_user_agent(user_agent)

    def disable_cache(self) -> None:
        self.send("Network.setCacheDisabled", {"cacheDisabled": True})

    def enable_cache(self) -> None:
        self.send("Network.setCacheDisabled", {"cacheDisabled": False})

    # ------------------------------------------------------------------
    # Input domain
    # ------------------------------------------------------------------

    def dispatch_touch(self, x: float, y: float, touch_type: str = "tap") -> None:
        if touch_type == "tap":
            self.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": x, "y": y}]})
            self.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
        else:
            mapping = {"press": "touchStart", "release": "touchEnd", "move": "touchMove"}
            tp = [] if touch_type == "release" else [{"x": x, "y": y}]
            self.send("Input.dispatchTouchEvent", {"type": mapping.get(touch_type, touch_type), "touchPoints": tp})

    def dispatch_swipe(self, x1: float, y1: float, x2: float, y2: float,
                       steps: int = 10, duration_ms: int = 300) -> None:
        step_delay = (duration_ms / 1000) / max(steps, 1)
        self.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": x1, "y": y1}]})
        for i in range(1, steps + 1):
            frac = i / steps
            self.send("Input.dispatchTouchEvent", {
                "type": "touchMove",
                "touchPoints": [{"x": x1 + (x2 - x1) * frac, "y": y1 + (y2 - y1) * frac}],
            })
            time.sleep(step_delay)
        self.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})

    def dispatch_key(self, key: str, key_type: str = "press", modifiers: int = 0) -> None:
        text = key if len(key) == 1 else ""
        if key_type in ("press", "down"):
            self.send("Input.dispatchKeyEvent", {"type": "keyDown", "key": key, "text": text, "modifiers": modifiers})
        if key_type in ("press", "up"):
            self.send("Input.dispatchKeyEvent", {"type": "keyUp", "key": key, "modifiers": modifiers})


# Legacy module-level constants used by older callers
CHROME_PACKAGE = BROWSERS["chrome"].package
CHROME_ACTIVITY = BROWSERS["chrome"].activity
