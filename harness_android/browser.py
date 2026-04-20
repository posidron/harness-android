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
from rich.console import Console

from harness_android.adb import ADB, poll_until
from harness_android.config import CDP_LOCAL_PORT

console = Console()


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
        # Edge appends the PID; matched by prefix in _wait_for_devtools_socket.
        devtools_socket="com.microsoft.emmx_devtools_remote",
        cmdline_files=(
            "/data/local/tmp/microsoft-edge-command-line",
            "/data/local/tmp/com.microsoft.emmx-command-line",
            "/data/local/tmp/chrome-command-line",
        ),
    ),
}


def resolve_browser(name_or_pkg: str | None) -> BrowserSpec:
    """Resolve a preset name or package string to a :class:`BrowserSpec`."""
    if not name_or_pkg:
        return BROWSERS["chrome"]
    if name_or_pkg in BROWSERS:
        return BROWSERS[name_or_pkg]
    for spec in BROWSERS.values():
        if spec.package == name_or_pkg:
            return spec
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
                        raise RuntimeError(f"CDP {method}: {msg['error']}")
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

    def enable_cdp(self, timeout: float = 60) -> None:
        """Start the browser with remote debugging and forward the socket."""
        if not self.adb.is_installed(self.spec.package):
            raise RuntimeError(
                f"{self.spec.package} is not installed on the device. "
                "Run `harness-android install-chromium` or install your APK."
            )

        self._write_chrome_flags()
        self.force_stop()
        # `-W` blocks until the activity reports launched — no fixed sleep.
        self.adb.shell(
            "am", "start", "-W",
            "-n", f"{self.spec.package}/{self.spec.activity}",
            "-d", "about:blank",
        )

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

    def _ensure_page_target(self) -> dict:
        """Return a page target, creating one via /json/new if none exists."""
        def _find() -> dict | None:
            for t in self.list_targets():
                if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
                    return t
            return None

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
            self._page.drain_events()  # discard stale Page.* events
        result = self.send("Page.navigate", {"url": url})
        if err := result.get("errorText"):
            raise RuntimeError(f"Navigation to {url} failed: {err}")
        if wait:
            self.wait_for_load(timeout=timeout)
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
    ) -> Any:
        result = self.send(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": await_promise,
            },
            timeout=timeout,
        )
        if exc := result.get("exceptionDetails"):
            desc = exc.get("exception", {}).get("description") or exc.get("text")
            raise RuntimeError(f"JS exception: {desc}")
        return result.get("result", {}).get("value")

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
