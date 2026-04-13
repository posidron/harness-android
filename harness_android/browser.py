"""Control Chrome on Android via ADB intents + Chrome DevTools Protocol (CDP)."""

from __future__ import annotations

import json
import time
from typing import Any, Optional

import requests
import websocket  # websocket-client
from websocket import WebSocketTimeoutException
from rich.console import Console

from harness_android.adb import ADB
from harness_android.config import CDP_LOCAL_PORT, CDP_REMOTE_PORT

console = Console()

CHROME_PACKAGE = "com.android.chrome"
CHROME_ACTIVITY = "com.google.android.apps.chrome.Main"


class Browser:
    """Remote-control Chrome/Chromium running inside the Android emulator.

    The communication has two layers:

    1. **ADB intents** – open URLs, clear data, etc. Works immediately.
    2. **Chrome DevTools Protocol** – evaluate JS, inspect DOM, capture
       network, take page screenshots, etc.  Requires Chrome to be started
       with ``--enable-remote-debugging`` and a port forward through ADB.

    Pass ``package`` and ``activity`` to target a different browser
    (e.g. Chromium, Chrome Dev, Edge).
    """

    def __init__(
        self,
        adb: ADB,
        local_port: int = CDP_LOCAL_PORT,
        package: str = CHROME_PACKAGE,
        activity: str = CHROME_ACTIVITY,
    ):
        self.adb = adb
        self.local_port = local_port
        self.package = package
        self.activity = activity
        self._ws: Optional[websocket.WebSocket] = None
        self._msg_id = 0
        self._extra_chrome_flags: list[str] = []

    # ------------------------------------------------------------------
    # Intent-based control (no CDP needed)
    # ------------------------------------------------------------------

    def open_url(self, url: str) -> None:
        """Open *url* in Chrome via an ACTION_VIEW intent."""
        self.adb.launch_url(url)
        console.print(f"[green]Opened {url}")

    def open_chrome(self) -> None:
        """Launch the browser's main activity."""
        self.adb.launch_activity(f"{self.package}/{self.activity}")

    def clear_data(self) -> None:
        """Clear browser app data (cookies, cache, etc.)."""
        self.adb.shell("pm", "clear", self.package)

    def force_stop(self) -> None:
        self.adb.shell("am", "force-stop", self.package)

    # ------------------------------------------------------------------
    # CDP setup
    # ------------------------------------------------------------------

    def _write_chrome_flags(self) -> None:
        """Write command-line flags so Chrome exposes the DevTools socket.

        Chrome on Android reads ``/data/local/tmp/chrome-command-line``
        for extra flags.  The first token is ignored (arg0 placeholder).
        The emulator runs as root so we can write this directly.
        """
        flags = "_ --disable-fre --no-default-browser-check --no-first-run --enable-remote-debugging --remote-allow-origins=*"
        if self._extra_chrome_flags:
            flags += " " + " ".join(self._extra_chrome_flags)
        # Use a single quoted shell command to avoid quoting issues across
        # the local-subprocess → adb-shell boundary.
        for dest in (
            "/data/local/tmp/chrome-command-line",
            f"/data/local/tmp/{self.package}-command-line",
        ):
            self.adb.run(
                "shell",
                f"echo '{flags}' > {dest}",
            )
        console.print("[dim]Chrome debug flags written.")

    def enable_cdp(self) -> None:
        """Start Chrome with remote debugging and set up the ADB port forward.

        Chrome on Android listens on an abstract-namespace UNIX socket named
        ``chrome_devtools_remote``.  We forward that to a local TCP port.
        """
        # Write flags BEFORE starting Chrome
        self._write_chrome_flags()

        # Restart Chrome so it picks up the command-line flags
        self.force_stop()
        time.sleep(1)
        self.adb.shell(
            "am", "start",
            "-n", f"{self.package}/{self.activity}",
            "--es", "com.android.chrome.extra.OPEN_URL", "about:blank",
        )

        # Wait for Chrome to start and expose the socket
        time.sleep(4)

        # Set up the port forward (abstract socket → local TCP)
        self.adb.run(
            "forward",
            f"tcp:{self.local_port}",
            "localabstract:chrome_devtools_remote",
        )

        # Poll until the /json endpoint responds
        console.print("[bold]Waiting for CDP to become available …")
        deadline = time.monotonic() + 20
        last_err: Exception | None = None
        while time.monotonic() < deadline:
            try:
                resp = requests.get(
                    f"http://localhost:{self.local_port}/json",
                    timeout=3,
                )
                if resp.status_code == 200:
                    console.print(
                        f"[green]CDP enabled – http://localhost:{self.local_port}/json"
                    )
                    return
            except Exception as exc:  # noqa: BLE001
                last_err = exc
            time.sleep(1)

        hint = f" (last error: {last_err})" if last_err else ""
        raise RuntimeError(
            f"CDP did not become available within 20 s{hint}.\n"
            "Make sure Chrome is installed on the emulator image."
        )

    def disable_cdp(self) -> None:
        self.close()
        self.adb.forward_remove(self.local_port)

    # ------------------------------------------------------------------
    # CDP low-level messaging
    # ------------------------------------------------------------------

    def _get_ws_url(self) -> str:
        """Query the /json endpoint for the first inspectable page."""
        deadline = time.monotonic() + 10
        last_err: Exception | None = None
        while time.monotonic() < deadline:
            try:
                resp = requests.get(
                    f"http://localhost:{self.local_port}/json",
                    timeout=5,
                )
                pages = resp.json()
                for page in pages:
                    ws = page.get("webSocketDebuggerUrl")
                    if ws:
                        return ws
            except Exception as exc:  # noqa: BLE001
                last_err = exc
            time.sleep(1)
        if last_err:
            raise RuntimeError(
                f"Could not reach CDP /json endpoint: {last_err}"
            ) from last_err
        raise RuntimeError("No inspectable page found via CDP")

    def connect(self) -> None:
        """Open a WebSocket connection to the first browser tab."""
        ws_url = self._get_ws_url()
        self._ws = websocket.create_connection(ws_url, timeout=60)
        console.print("[green]CDP WebSocket connected.")

    def enable_domains(self) -> None:
        """Enable common CDP domains so commands like Runtime.evaluate work."""
        try:
            self.send("Page.enable")
        except Exception:  # noqa: BLE001
            pass
        try:
            self.send("Runtime.enable")
        except Exception:  # noqa: BLE001
            pass

    def close(self) -> None:
        if self._ws:
            self._ws.close()
            self._ws = None

    def send(self, method: str, params: dict[str, Any] | None = None) -> dict:
        """Send a CDP command and return the response."""
        if self._ws is None:
            self.connect()
        assert self._ws is not None
        self._msg_id += 1
        msg = {"id": self._msg_id, "method": method, "params": params or {}}
        self._ws.send(json.dumps(msg))
        # Read until we get our reply (skip events).
        # Heavy pages (e.g. CNN) generate many CDP events during navigation;
        # use a per-message deadline so we don't die on a single slow recv.
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            try:
                raw = self._ws.recv()
            except WebSocketTimeoutException:
                continue  # keep waiting until overall deadline
            data = json.loads(raw)
            if data.get("id") == self._msg_id:
                if "error" in data:
                    raise RuntimeError(f"CDP error: {data['error']}")
                return data.get("result", {})
        raise RuntimeError(
            f"CDP response for {method} (id={self._msg_id}) not received "
            "within 120 s"
        )

    # ------------------------------------------------------------------
    # High-level CDP helpers
    # ------------------------------------------------------------------

    def navigate(self, url: str) -> dict:
        """Navigate the current tab to *url*."""
        result = self.send("Page.navigate", {"url": url})
        console.print(f"[green]Navigated to {url}")
        return result

    def evaluate_js(self, expression: str) -> Any:
        """Evaluate *expression* in the page and return the result value."""
        result = self.send(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True},
        )
        remote_obj = result.get("result", {})
        return remote_obj.get("value")

    def get_page_title(self) -> str:
        return self.evaluate_js("document.title") or ""

    def get_page_url(self) -> str:
        return self.evaluate_js("window.location.href") or ""

    def get_page_html(self) -> str:
        return self.evaluate_js("document.documentElement.outerHTML") or ""

    def page_screenshot_base64(self) -> str:
        """Capture a CDP screenshot (base64-encoded PNG)."""
        result = self.send("Page.captureScreenshot", {"format": "png"})
        return result.get("data", "")

    def page_screenshot(self, path: str) -> None:
        """Save a CDP page screenshot to *path*."""
        import base64

        data = self.page_screenshot_base64()
        with open(path, "wb") as f:
            f.write(base64.b64decode(data))
        console.print(f"[green]Page screenshot saved to {path}")

    def click_element(self, selector: str) -> None:
        """Click the first element matching *selector*."""
        self.evaluate_js(
            f"document.querySelector({json.dumps(selector)}).click()"
        )

    def type_in_element(self, selector: str, text: str) -> None:
        """Set the value of an input matching *selector*."""
        js = (
            f"var el = document.querySelector({json.dumps(selector)}); "
            f"el.value = {json.dumps(text)}; "
            "el.dispatchEvent(new Event('input', {bubbles: true}));"
        )
        self.evaluate_js(js)

    def wait_for_selector(self, selector: str, timeout: float = 10) -> bool:
        """Poll until *selector* exists in the DOM."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            found = self.evaluate_js(
                f"!!document.querySelector({json.dumps(selector)})"
            )
            if found:
                return True
            time.sleep(0.3)
        return False

    def get_cookies(self) -> list[dict]:
        result = self.send("Network.getCookies")
        return result.get("cookies", [])

    def clear_cookies(self) -> None:
        self.send("Network.clearBrowserCookies")

    def set_user_agent(self, ua: str) -> None:
        self.send("Network.setUserAgentOverride", {"userAgent": ua})

    def enable_network_logging(self) -> None:
        self.send("Network.enable")

    def enable_page_events(self) -> None:
        self.send("Page.enable")

    # ------------------------------------------------------------------
    # Security & pentest helpers
    # ------------------------------------------------------------------

    def enable_security(self) -> None:
        """Enable the Security domain for certificate/TLS info."""
        self.send("Security.enable")

    def get_security_state(self) -> dict:
        """Return the page's security state (cert info, TLS version, etc.)."""
        self.enable_security()
        return self.send("Security.getSecurityState") if hasattr(self, '_ws') and self._ws else {}

    def override_certificate_errors(self, allow: bool = True) -> None:
        """Accept or reject certificate errors (e.g. self-signed certs)."""
        self.send("Security.setIgnoreCertificateErrors", {"ignore": allow})

    def inject_script_on_load(self, source: str) -> str:
        """Inject JS that runs before every page load. Returns script ID."""
        self.send("Page.enable")
        result = self.send(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": source},
        )
        return result.get("identifier", "")

    def remove_injected_script(self, identifier: str) -> None:
        self.send("Page.removeScriptToEvaluateOnNewDocument", {"identifier": identifier})

    def get_response_body(self, request_id: str) -> bytes:
        """Fetch a response body by network request ID."""
        result = self.send("Network.getResponseBody", {"requestId": request_id})
        body = result.get("body", "")
        import base64
        if result.get("base64Encoded", False):
            return base64.b64decode(body)
        return body.encode()

    def emulate_device(
        self,
        width: int = 412,
        height: int = 915,
        device_scale_factor: float = 2.625,
        mobile: bool = True,
        user_agent: str | None = None,
    ) -> None:
        """Override device metrics for fingerprint spoofing."""
        params: dict[str, Any] = {
            "width": width,
            "height": height,
            "deviceScaleFactor": device_scale_factor,
            "mobile": mobile,
        }
        self.send("Emulation.setDeviceMetricsOverride", params)
        if user_agent:
            self.set_user_agent(user_agent)

    def disable_cache(self) -> None:
        self.send("Network.setCacheDisabled", {"cacheDisabled": True})

    def enable_cache(self) -> None:
        self.send("Network.setCacheDisabled", {"cacheDisabled": False})

    # ------------------------------------------------------------------
    # CDP Input domain — synthesize real touch / keyboard events
    # ------------------------------------------------------------------

    def dispatch_touch(
        self, x: float, y: float, touch_type: str = "tap"
    ) -> None:
        """Synthesize a touch event at (x, y) in CSS pixels.

        *touch_type*: ``tap`` (press+release), ``press``, ``release``,
        ``move``.
        """
        if touch_type == "tap":
            tp = [{"x": x, "y": y}]
            self.send("Input.dispatchTouchEvent", {
                "type": "touchStart",
                "touchPoints": tp,
            })
            self.send("Input.dispatchTouchEvent", {
                "type": "touchEnd",
                "touchPoints": [],
            })
        else:
            mapping = {"press": "touchStart", "release": "touchEnd", "move": "touchMove"}
            tp = [{"x": x, "y": y}] if touch_type != "release" else []
            self.send("Input.dispatchTouchEvent", {
                "type": mapping.get(touch_type, touch_type),
                "touchPoints": tp,
            })

    def dispatch_swipe(
        self,
        x1: float, y1: float,
        x2: float, y2: float,
        steps: int = 10,
        duration_ms: int = 300,
    ) -> None:
        """Synthesize a swipe gesture from (x1,y1) to (x2,y2)."""
        import time
        step_delay = (duration_ms / 1000) / steps
        self.send("Input.dispatchTouchEvent", {
            "type": "touchStart",
            "touchPoints": [{"x": x1, "y": y1}],
        })
        for i in range(1, steps + 1):
            frac = i / steps
            cx = x1 + (x2 - x1) * frac
            cy = y1 + (y2 - y1) * frac
            self.send("Input.dispatchTouchEvent", {
                "type": "touchMove",
                "touchPoints": [{"x": cx, "y": cy}],
            })
            time.sleep(step_delay)
        self.send("Input.dispatchTouchEvent", {
            "type": "touchEnd",
            "touchPoints": [],
        })

    def dispatch_key(
        self,
        key: str,
        key_type: str = "press",
        modifiers: int = 0,
    ) -> None:
        """Synthesize a keyboard event.

        *key*: Key value (e.g. ``Enter``, ``a``, ``Tab``).
        *key_type*: ``press`` (down+up), ``down``, ``up``.
        *modifiers*: Bitmask — 1=Alt, 2=Ctrl, 4=Meta, 8=Shift.
        """
        text = key if len(key) == 1 else ""
        if key_type in ("press", "down"):
            self.send("Input.dispatchKeyEvent", {
                "type": "keyDown",
                "key": key,
                "text": text,
                "modifiers": modifiers,
            })
        if key_type in ("press", "up"):
            self.send("Input.dispatchKeyEvent", {
                "type": "keyUp",
                "key": key,
                "modifiers": modifiers,
            })
