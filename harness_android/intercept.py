"""CDP Fetch-based request/response interception.

Uses the ``Fetch`` domain to pause, inspect, modify, and continue HTTP
requests and responses flowing through Chrome — all from outside the
emulator.
"""

from __future__ import annotations

import base64
import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import websocket

from harness_android.browser import Browser
from harness_android.console import console



@dataclass
class InterceptedRequest:
    """Snapshot of a paused network request."""

    request_id: str
    url: str
    method: str
    headers: dict[str, str]
    post_data: str | None = None
    resource_type: str = ""
    # Populated only for response-stage interceptions
    response_status: int | None = None
    response_headers: dict[str, str] = field(default_factory=dict)
    response_body: bytes | None = None


# Type alias for user-supplied handler callbacks
RequestHandler = Callable[[InterceptedRequest], Optional[dict]]


class Interceptor:
    """Intercept and modify HTTP traffic via CDP ``Fetch`` domain.

    Usage::

        interceptor = Interceptor(browser)

        @interceptor.on_request("*login*")
        def spy_login(req):
            print(f"Login POST → {req.url}")
            print(f"  body: {req.post_data}")
            # Return None to let it through unmodified

        @interceptor.on_response("*.js")
        def patch_js(req):
            # Modify the response body
            original = req.response_body.decode()
            patched = original.replace("isAdmin=false", "isAdmin=true")
            return {"body": patched.encode()}

        interceptor.start()   # blocks; Ctrl-C to stop
    """

    def __init__(self, browser: Browser):
        self.browser = browser
        self._request_handlers: list[tuple[str, RequestHandler]] = []
        self._response_handlers: list[tuple[str, RequestHandler]] = []
        self._running = False
        self._listener_thread: Optional[threading.Thread] = None
        self._log: list[InterceptedRequest] = []

    # ------------------------------------------------------------------
    # Decorator-based handler registration
    # ------------------------------------------------------------------

    def on_request(self, url_pattern: str = "*"):
        """Register a request-stage handler for URLs matching *url_pattern*."""
        def decorator(fn: RequestHandler) -> RequestHandler:
            self._request_handlers.append((url_pattern, fn))
            return fn
        return decorator

    def on_response(self, url_pattern: str = "*"):
        """Register a response-stage handler."""
        def decorator(fn: RequestHandler) -> RequestHandler:
            self._response_handlers.append((url_pattern, fn))
            return fn
        return decorator

    # ------------------------------------------------------------------
    # Enable / disable
    # ------------------------------------------------------------------

    def _build_patterns(self) -> list[dict]:
        """Build Fetch.RequestPattern list from registered handlers."""
        patterns: list[dict] = []

        if self._request_handlers:
            for pat, _ in self._request_handlers:
                patterns.append({
                    "urlPattern": pat,
                    "requestStage": "Request",
                })

        if self._response_handlers:
            for pat, _ in self._response_handlers:
                patterns.append({
                    "urlPattern": pat,
                    "requestStage": "Response",
                })

        if not patterns:
            # Catch-all for logging
            patterns.append({"urlPattern": "*", "requestStage": "Request"})

        return patterns

    def enable(self) -> None:
        """Enable Fetch interception with the registered patterns."""
        patterns = self._build_patterns()
        self.browser.send("Fetch.enable", {"patterns": patterns})
        console.print(f"[green]Fetch interception enabled ({len(patterns)} patterns)")

    def disable(self) -> None:
        self.browser.send("Fetch.disable")
        console.print("[yellow]Fetch interception disabled.")

    # ------------------------------------------------------------------
    # Event loop
    # ------------------------------------------------------------------

    def _url_matches(self, pattern: str, url: str) -> bool:
        """Simple glob-style match: ``*`` matches any substring."""
        import fnmatch
        return fnmatch.fnmatch(url, pattern)

    def _handle_request_paused(self, params: dict) -> None:
        """Process a ``Fetch.requestPaused`` event."""
        request_data = params.get("request", {})
        req = InterceptedRequest(
            request_id=params["requestId"],
            url=request_data.get("url", ""),
            method=request_data.get("method", "GET"),
            headers=request_data.get("headers", {}),
            post_data=request_data.get("postData"),
            resource_type=params.get("resourceType", ""),
        )

        # Check if this is a response-stage pause
        response_status = params.get("responseStatusCode")
        if response_status is not None:
            req.response_status = response_status
            req.response_headers = {
                h["name"]: h["value"]
                for h in params.get("responseHeaders", [])
            }
            # Fetch the response body
            try:
                body_result = self.browser.send(
                    "Fetch.getResponseBody",
                    {"requestId": req.request_id},
                )
                body_b64 = body_result.get("body", "")
                is_base64 = body_result.get("base64Encoded", False)
                req.response_body = (
                    base64.b64decode(body_b64) if is_base64
                    else body_b64.encode()
                )
            except Exception:  # noqa: BLE001
                req.response_body = b""

            self._log.append(req)
            self._dispatch_response(req)
        else:
            self._log.append(req)
            self._dispatch_request(req)

    def _dispatch_request(self, req: InterceptedRequest) -> None:
        for pattern, handler in self._request_handlers:
            if self._url_matches(pattern, req.url):
                result = handler(req)
                if result is not None:
                    # Modify and continue
                    continue_params: dict[str, Any] = {"requestId": req.request_id}
                    if "url" in result:
                        continue_params["url"] = result["url"]
                    if "method" in result:
                        continue_params["method"] = result["method"]
                    if "headers" in result:
                        continue_params["headers"] = [
                            {"name": k, "value": v}
                            for k, v in result["headers"].items()
                        ]
                    if "post_data" in result:
                        continue_params["postData"] = base64.b64encode(
                            result["post_data"].encode()
                        ).decode()
                    self.browser.send("Fetch.continueRequest", continue_params)
                    return

        # No handler modified — continue normally
        self.browser.send("Fetch.continueRequest", {"requestId": req.request_id})

    def _dispatch_response(self, req: InterceptedRequest) -> None:
        for pattern, handler in self._response_handlers:
            if self._url_matches(pattern, req.url):
                result = handler(req)
                if result is not None:
                    # Fulfill with modified response
                    fulfill_params: dict[str, Any] = {
                        "requestId": req.request_id,
                        "responseCode": result.get("status", req.response_status or 200),
                    }
                    if "headers" in result:
                        fulfill_params["responseHeaders"] = [
                            {"name": k, "value": v}
                            for k, v in result["headers"].items()
                        ]
                    if "body" in result:
                        body = result["body"]
                        if isinstance(body, str):
                            body = body.encode()
                        fulfill_params["body"] = base64.b64encode(body).decode()
                    self.browser.send("Fetch.fulfillRequest", fulfill_params)
                    return

        # No modification — continue
        self.browser.send("Fetch.continueRequest", {"requestId": req.request_id})

    def _listen_loop(self) -> None:
        """Read CDP events and dispatch Fetch.requestPaused."""
        ws = self.browser._ws
        assert ws is not None
        while self._running:
            try:
                ws.settimeout(1.0)
                raw = ws.recv()
            except websocket.WebSocketTimeoutException:
                continue
            except websocket.WebSocketConnectionClosedException:
                # Browser went away — stop listening cleanly.
                self._running = False
                return
            except KeyboardInterrupt:
                self._running = False
                raise
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if data.get("method") == "Fetch.requestPaused":
                try:
                    self._handle_request_paused(data["params"])
                except Exception as exc:  # noqa: BLE001 — keep loop alive
                    console.print(f"[red]Interceptor handler error: {exc}")

    def start(self, background: bool = False) -> None:
        """Enable interception and start the event loop.

        If *background* is True, runs in a daemon thread and returns
        immediately.  Otherwise blocks until Ctrl-C.
        """
        self.enable()
        self._running = True

        if background:
            self._listener_thread = threading.Thread(
                target=self._listen_loop, daemon=True,
            )
            self._listener_thread.start()
            console.print("[green]Interceptor running in background.")
        else:
            console.print("[bold]Interceptor running — press Ctrl-C to stop.")
            try:
                self._listen_loop()
            except KeyboardInterrupt:
                pass
            finally:
                self.stop()

    def stop(self) -> None:
        self._running = False
        if self._listener_thread:
            self._listener_thread.join(timeout=3)
            self._listener_thread = None
        self.disable()

    # ------------------------------------------------------------------
    # Log access
    # ------------------------------------------------------------------

    @property
    def log(self) -> list[InterceptedRequest]:
        return list(self._log)

    def clear_log(self) -> None:
        self._log.clear()

    def dump_log(self, path: str = "intercept_log.json") -> None:
        """Write the request log to a JSON file."""
        entries = []
        for req in self._log:
            entry: dict[str, Any] = {
                "url": req.url,
                "method": req.method,
                "headers": req.headers,
                "resource_type": req.resource_type,
            }
            if req.post_data:
                entry["post_data"] = req.post_data
            if req.response_status is not None:
                entry["response_status"] = req.response_status
                entry["response_headers"] = req.response_headers
            entries.append(entry)
        with open(path, "w") as f:
            json.dump(entries, f, indent=2)
        console.print(f"[green]Intercept log written to {path} ({len(entries)} entries)")
