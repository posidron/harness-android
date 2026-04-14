"""Mojo IPC observation and testing via CDP Tracing + Web API triggers.

Chromium's Mojo IPC connects the renderer (sandboxed) to privileged browser
processes.  This module provides tools to:

1. **Trace** — capture Mojo IPC messages via the CDP ``Tracing`` domain
2. **Trigger** — exercise Web APIs that use Mojo interfaces under the hood
3. **Analyze** — parse traces to identify interfaces, methods, message patterns
4. **Fuzz** — send malformed/boundary inputs to Mojo-backed Web APIs

Usage::

    from harness_android.mojo import MojoTracer

    tracer = MojoTracer(browser)
    tracer.start_trace()
    # ... trigger Web APIs ...
    events = tracer.stop_trace()
    mojo_msgs = tracer.extract_mojo_messages(events)
    tracer.print_summary(mojo_msgs)
"""

from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass, field
from typing import Any

from rich.console import Console
from rich.table import Table

from harness_android.browser import Browser

console = Console()


# ======================================================================
# Trace categories that capture Mojo IPC
# ======================================================================

MOJO_TRACE_CATEGORIES = [
    "mojom",
    "ipc",
    "toplevel",
    "toplevel.flow",
    "disabled-by-default-ipc.flow",
    "disabled-by-default-toplevel.flow",
]

# Additional categories for deeper visibility
MOJO_VERBOSE_CATEGORIES = [
    *MOJO_TRACE_CATEGORIES,
    "disabled-by-default-mojom",
    "blink",
    "content",
    "navigation",
    "ServiceWorker",
    "loading",
]


# ======================================================================
# Web APIs that trigger Mojo IPC (renderer → browser process)
# ======================================================================

# Each entry: (name, JS code that triggers it, Mojo interface it exercises)
MOJO_WEB_API_TRIGGERS: list[tuple[str, str, str]] = [
    (
        "Permissions.query",
        "navigator.permissions.query({name: 'geolocation'}).then(r => r.state).catch(e => e.message)",
        "blink.mojom.PermissionService",
    ),
    (
        "Notifications.permission",
        "Notification.requestPermission().catch(e => e.message)",
        "blink.mojom.NotificationService",
    ),
    (
        "Clipboard.readText",
        "navigator.clipboard.readText().catch(e => e.message)",
        "blink.mojom.ClipboardHost",
    ),
    (
        "Clipboard.writeText",
        "navigator.clipboard.writeText('harness_test').catch(e => e.message)",
        "blink.mojom.ClipboardHost",
    ),
    (
        "Geolocation.getCurrentPosition",
        "new Promise(r => navigator.geolocation.getCurrentPosition(p => r(p.coords.latitude), e => r(e.message)))",
        "device.mojom.Geolocation",
    ),
    (
        "MediaDevices.enumerateDevices",
        "navigator.mediaDevices.enumerateDevices().then(d => d.length).catch(e => e.message)",
        "blink.mojom.MediaDevicesDispatcherHost",
    ),
    (
        "MediaDevices.getUserMedia",
        "navigator.mediaDevices.getUserMedia({audio: true}).then(() => 'ok').catch(e => e.message)",
        "blink.mojom.MediaStreamDispatcherHost",
    ),
    (
        "Credentials.get",
        "navigator.credentials.get({password: true}).then(c => c ? 'found' : 'none').catch(e => e.message)",
        "blink.mojom.CredentialManager",
    ),
    (
        "WebUSB.getDevices",
        "navigator.usb.getDevices().then(d => d.length).catch(e => e.message)",
        "device.mojom.UsbDeviceManager",
    ),
    (
        "WebBluetooth.getAvailability",
        "navigator.bluetooth.getAvailability().catch(e => e.message)",
        "blink.mojom.WebBluetoothService",
    ),
    (
        "WebNFC",
        "typeof NDEFReader !== 'undefined' ? 'available' : 'unavailable'",
        "device.mojom.NFC",
    ),
    (
        "StorageManager.estimate",
        "navigator.storage.estimate().then(e => e.quota).catch(e => e.message)",
        "blink.mojom.QuotaManagerHost",
    ),
    (
        "StorageManager.persist",
        "navigator.storage.persist().catch(e => e.message)",
        "blink.mojom.QuotaManagerHost",
    ),
    (
        "CacheStorage.open",
        "caches.open('harness_test').then(() => 'ok').catch(e => e.message)",
        "blink.mojom.CacheStorage",
    ),
    (
        "Locks.request",
        "navigator.locks.request('harness_test', () => 'ok').catch(e => e.message)",
        "blink.mojom.LockManager",
    ),
    (
        "IndexedDB.open",
        "new Promise(r => { var req = indexedDB.open('harness_test'); req.onsuccess = () => r('ok'); req.onerror = e => r(e.target.error.message); })",
        "blink.mojom.IDBFactory",
    ),
    (
        "ServiceWorker.register",
        "navigator.serviceWorker.register('/sw_harness_test.js').then(() => 'ok').catch(e => e.message)",
        "blink.mojom.ServiceWorkerContainerHost",
    ),
    (
        "BarcodeDetector",
        "typeof BarcodeDetector !== 'undefined' ? new BarcodeDetector().detect(new ImageData(1,1)).then(() => 'ok').catch(e => e.message) : 'unavailable'",
        "shape_detection.mojom.BarcodeDetection",
    ),
    (
        "PaymentRequest",
        "typeof PaymentRequest !== 'undefined' ? 'available' : 'unavailable'",
        "payments.mojom.PaymentRequest",
    ),
    (
        "FileSystem.showOpenFilePicker",
        "typeof showOpenFilePicker !== 'undefined' ? showOpenFilePicker().catch(e => e.message) : 'unavailable'",
        "blink.mojom.FileSystemAccessManager",
    ),
    (
        "Sensor.Accelerometer",
        "typeof Accelerometer !== 'undefined' ? new Accelerometer().start() || 'started' : 'unavailable'",
        "device.mojom.SensorProvider",
    ),
    (
        "WakeLock.request",
        "navigator.wakeLock.request('screen').then(s => { s.release(); return 'ok'; }).catch(e => e.message)",
        "blink.mojom.WakeLockService",
    ),
    (
        "SharedWorker",
        "try { new SharedWorker('data:text/javascript,'); 'ok'; } catch(e) { e.message; }",
        "blink.mojom.SharedWorkerConnector",
    ),
]


# ======================================================================
# Data types
# ======================================================================

@dataclass
class MojoMessage:
    """A single Mojo IPC message extracted from a trace."""
    interface: str = ""
    method: str = ""
    phase: str = ""          # "send" | "receive"
    process_name: str = ""
    process_id: int = 0
    thread_name: str = ""
    timestamp_us: int = 0
    duration_us: int = 0
    args: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class TriggerResult:
    """Result of triggering a Mojo-backed Web API."""
    api_name: str
    mojo_interface: str
    result: Any = None
    error: str = ""
    duration_ms: float = 0


# ======================================================================
# MojoTracer
# ======================================================================

class MojoTracer:
    """Trace and analyze Mojo IPC through Chrome's tracing infrastructure.

    Usage::

        tracer = MojoTracer(browser)

        # Capture a trace while triggering Web APIs
        tracer.start_trace()
        results = tracer.trigger_all_apis()
        events = tracer.stop_trace()

        # Analyze
        messages = tracer.extract_mojo_messages(events)
        tracer.print_summary(messages)
        tracer.dump("mojo_trace.json", events, messages)
    """

    def __init__(self, browser: Browser, verbose: bool = False):
        self.browser = browser
        self.verbose = verbose
        self._tracing = False
        self._trace_events: list[dict] = []

    # ------------------------------------------------------------------
    # Tracing
    # ------------------------------------------------------------------

    def start_trace(self) -> None:
        """Start capturing Mojo IPC via the CDP Tracing domain."""
        categories = MOJO_VERBOSE_CATEGORIES if self.verbose else MOJO_TRACE_CATEGORIES
        self.browser.send("Tracing.start", {
            "traceConfig": {
                "includedCategories": categories,
                "recordMode": "recordUntilFull",
            },
            "transferMode": "ReturnAsStream",
        })
        self._tracing = True
        self._trace_events = []
        console.print(f"[green]Mojo trace started ({len(categories)} categories)")

    def stop_trace(self) -> list[dict]:
        """Stop tracing and return all trace events."""
        if not self._tracing:
            return []

        # End tracing — Chrome will send events via Tracing.tracingComplete
        self.browser.send("Tracing.end")
        self._tracing = False

        # Collect trace data by polling
        # The events come as Tracing.dataCollected events and finally
        # Tracing.tracingComplete. We read from the WebSocket directly.
        events: list[dict] = []
        ws = self.browser._ws
        assert ws is not None

        deadline = time.monotonic() + 30
        complete = False
        while time.monotonic() < deadline and not complete:
            try:
                ws.settimeout(5.0)
                raw = ws.recv()
                data = json.loads(raw)
                method = data.get("method", "")
                if method == "Tracing.dataCollected":
                    chunk = data.get("params", {}).get("value", [])
                    events.extend(chunk)
                elif method == "Tracing.tracingComplete":
                    # Check if there's a stream to read
                    stream = data.get("params", {}).get("stream")
                    if stream:
                        events.extend(self._read_trace_stream(stream))
                    complete = True
            except Exception:  # noqa: BLE001
                # Timeout — check if we have data
                if events:
                    complete = True

        self._trace_events = events
        console.print(f"[green]Trace stopped — {len(events)} events captured")
        return events

    def _read_trace_stream(self, stream_handle: str) -> list[dict]:
        """Read trace data from an IO stream handle."""
        events: list[dict] = []
        buf = ""
        while True:
            result = self.browser.send("IO.read", {
                "handle": stream_handle,
                "size": 1 << 20,  # 1 MB chunks
            })
            chunk = result.get("data", "")
            if result.get("base64Encoded", False):
                chunk = base64.b64decode(chunk).decode("utf-8", errors="replace")
            buf += chunk
            eof = result.get("eof", False)
            if eof:
                break

        self.browser.send("IO.close", {"handle": stream_handle})

        # Parse the JSON trace format
        try:
            parsed = json.loads(buf)
            if isinstance(parsed, list):
                events = parsed
            elif isinstance(parsed, dict):
                events = parsed.get("traceEvents", [])
        except json.JSONDecodeError:
            # Try line-by-line (JSON array stream)
            for line in buf.strip().rstrip(",").split("\n"):
                line = line.strip().rstrip(",")
                if line and line not in ("[", "]"):
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass

        return events

    # ------------------------------------------------------------------
    # Message extraction
    # ------------------------------------------------------------------

    def extract_mojo_messages(self, events: list[dict] | None = None) -> list[MojoMessage]:
        """Parse trace events and extract Mojo IPC messages."""
        if events is None:
            events = self._trace_events

        messages: list[MojoMessage] = []
        for ev in events:
            cat = ev.get("cat", "")
            name = ev.get("name", "")
            args = ev.get("args", {})

            # Mojo IPC events have patterns like:
            # cat=mojom, name=<Interface>::<Method>
            # cat=ipc, name=IPC_Message (older)
            # cat=toplevel, name=Receive ... mojo::...

            msg = None

            if "mojom" in cat:
                msg = MojoMessage(
                    raw=ev,
                    args=args,
                    timestamp_us=ev.get("ts", 0),
                    duration_us=ev.get("dur", 0),
                    process_id=ev.get("pid", 0),
                    phase=ev.get("ph", ""),
                )
                # Parse interface::method from name
                if "::" in name:
                    parts = name.rsplit("::", 1)
                    msg.interface = parts[0]
                    msg.method = parts[1]
                else:
                    msg.interface = name

            elif "ipc" in cat and ("mojo" in name.lower() or "message" in name.lower()):
                msg = MojoMessage(
                    raw=ev,
                    args=args,
                    interface=args.get("interface", name),
                    method=args.get("method", ""),
                    timestamp_us=ev.get("ts", 0),
                    duration_us=ev.get("dur", 0),
                    process_id=ev.get("pid", 0),
                    phase=ev.get("ph", ""),
                )

            elif "toplevel" in cat and "mojo" in name.lower():
                msg = MojoMessage(
                    raw=ev,
                    args=args,
                    interface=name,
                    timestamp_us=ev.get("ts", 0),
                    duration_us=ev.get("dur", 0),
                    process_id=ev.get("pid", 0),
                    phase=ev.get("ph", ""),
                )

            if msg:
                messages.append(msg)

        return messages

    # ------------------------------------------------------------------
    # Web API triggering
    # ------------------------------------------------------------------

    def trigger_api(self, name: str, js: str, mojo_interface: str) -> TriggerResult:
        """Trigger a single Mojo-backed Web API and return the result."""
        result = TriggerResult(api_name=name, mojo_interface=mojo_interface)
        start = time.monotonic()
        try:
            # Wrap in a 5-second timeout so permission-blocked APIs don't hang
            timeout_js = (
                f"Promise.race(["
                f"  (async () => {{ return {js}; }})(),"
                f"  new Promise((_, rej) => setTimeout(() => rej(new Error('timeout')), 5000))"
                f"])"
            )
            val = self.browser.send(
                "Runtime.evaluate",
                {
                    "expression": timeout_js,
                    "returnByValue": True,
                    "awaitPromise": True,
                },
            )
            exc_details = val.get("exceptionDetails")
            if exc_details:
                result.result = exc_details.get("text", "error")
            else:
                remote_obj = val.get("result", {})
                result.result = remote_obj.get("value")
        except Exception as exc:  # noqa: BLE001
            result.error = str(exc)
        result.duration_ms = (time.monotonic() - start) * 1000
        return result

    def trigger_all_apis(self) -> list[TriggerResult]:
        """Trigger all known Mojo-backed Web APIs and return results."""
        # Auto-grant permissions for the current origin to suppress prompts
        try:
            origin = self.browser.evaluate_js("window.location.origin") or ""
            self.browser.send("Browser.grantPermissions", {
                "origin": origin,
                "permissions": [
                    "geolocation", "notifications", "clipboardReadWrite",
                    "clipboardSanitizedWrite", "midi", "cameraPanTiltZoom",
                    "audioCapture", "videoCapture", "sensors",
                    "backgroundSync", "durableStorage",
                ],
            })
        except Exception:  # noqa: BLE001
            pass  # older Chrome may not support all permissions

        console.print(f"[bold]Triggering {len(MOJO_WEB_API_TRIGGERS)} Mojo-backed Web APIs …")
        results: list[TriggerResult] = []
        for name, js, iface in MOJO_WEB_API_TRIGGERS:
            r = self.trigger_api(name, js, iface)
            status = "[green]ok" if not r.error else "[red]err"
            console.print(f"  {status}[/]  {name:40s} → {r.result or r.error}")
            results.append(r)
        return results

    def trigger_selected_apis(self, *api_names: str) -> list[TriggerResult]:
        """Trigger specific APIs by name."""
        results: list[TriggerResult] = []
        for name, js, iface in MOJO_WEB_API_TRIGGERS:
            if name in api_names:
                results.append(self.trigger_api(name, js, iface))
        return results

    # ------------------------------------------------------------------
    # Fuzzing helpers
    # ------------------------------------------------------------------

    def fuzz_api(
        self,
        api_name: str,
        js_template: str,
        inputs: list[str],
        mojo_interface: str = "",
    ) -> list[TriggerResult]:
        """Fuzz a Web API by substituting each input into *js_template*.

        The template should contain ``{FUZZ}`` as a placeholder::

            tracer.fuzz_api(
                "Clipboard.writeText",
                "navigator.clipboard.writeText({FUZZ}).catch(e => e.message)",
                ["'a'*10000", "null", "undefined", "0", "[]", "{{}}"],
                "blink.mojom.ClipboardHost",
            )
        """
        console.print(f"[bold]Fuzzing {api_name} with {len(inputs)} inputs …")
        results: list[TriggerResult] = []
        for inp in inputs:
            js = js_template.replace("{FUZZ}", inp)
            r = self.trigger_api(f"{api_name}[{inp[:30]}]", js, mojo_interface)
            results.append(r)
        return results

    # Common fuzz payloads for string-accepting APIs
    FUZZ_STRINGS: list[str] = [
        "''",
        "'a'.repeat(10000)",
        "'a'.repeat(1000000)",
        "null",
        "undefined",
        "0",
        "-1",
        "NaN",
        "Infinity",
        "true",
        "false",
        "[]",
        "{}",
        "new ArrayBuffer(0)",
        "new Uint8Array(0)",
        "new Blob([])",
        "Symbol('test')",
        "Object.create(null)",
        "'\\x00'.repeat(100)",
        "'\\ud800'",                   # lone surrogate
        "'\\udbff\\udfff'",            # max surrogate pair
        "String.fromCharCode(...Array.from({length: 256}, (_, i) => i))",
    ]

    # ------------------------------------------------------------------
    # Output
    # ------------------------------------------------------------------

    def print_summary(self, messages: list[MojoMessage] | None = None) -> None:
        """Print a summary table of Mojo messages."""
        if messages is None:
            messages = self.extract_mojo_messages()

        if not messages:
            console.print("[yellow]No Mojo IPC messages captured.")
            return

        # Count by interface
        iface_counts: dict[str, int] = {}
        method_counts: dict[str, int] = {}
        for m in messages:
            iface_counts[m.interface] = iface_counts.get(m.interface, 0) + 1
            if m.method:
                key = f"{m.interface}::{m.method}"
                method_counts[key] = method_counts.get(key, 0) + 1

        t = Table(title=f"Mojo IPC Summary ({len(messages)} messages)")
        t.add_column("Interface", style="bold")
        t.add_column("Count", justify="right")
        for iface, count in sorted(iface_counts.items(), key=lambda x: -x[1]):
            t.add_row(iface, str(count))
        console.print(t)

        if method_counts:
            t2 = Table(title="Top methods")
            t2.add_column("Interface::Method", style="bold")
            t2.add_column("Count", justify="right")
            for method, count in sorted(method_counts.items(), key=lambda x: -x[1])[:30]:
                t2.add_row(method, str(count))
            console.print(t2)

    def print_trigger_results(self, results: list[TriggerResult]) -> None:
        t = Table(title="Web API Trigger Results")
        t.add_column("API", style="bold")
        t.add_column("Mojo Interface")
        t.add_column("Result")
        t.add_column("Time (ms)", justify="right")
        for r in results:
            val = str(r.error or r.result)[:60]
            t.add_row(r.api_name, r.mojo_interface, val, f"{r.duration_ms:.0f}")
        console.print(t)

    def dump(
        self,
        path: str = "mojo_trace.json",
        events: list[dict] | None = None,
        messages: list[MojoMessage] | None = None,
        trigger_results: list[TriggerResult] | None = None,
    ) -> None:
        """Write trace data, extracted messages, and trigger results to JSON."""
        data: dict[str, Any] = {}

        if events is None:
            events = self._trace_events
        data["trace_event_count"] = len(events)

        if messages is not None:
            data["mojo_messages"] = [
                {
                    "interface": m.interface,
                    "method": m.method,
                    "phase": m.phase,
                    "process_id": m.process_id,
                    "timestamp_us": m.timestamp_us,
                    "duration_us": m.duration_us,
                }
                for m in messages
            ]

        if trigger_results is not None:
            data["trigger_results"] = [
                {
                    "api": r.api_name,
                    "mojo_interface": r.mojo_interface,
                    "result": str(r.result),
                    "error": r.error,
                    "duration_ms": r.duration_ms,
                }
                for r in trigger_results
            ]

        # Save the raw trace separately (it can be huge)
        if events:
            trace_path = path.replace(".json", "_raw.json")
            with open(trace_path, "w") as f:
                json.dump({"traceEvents": events}, f)
            console.print(f"[dim]Raw trace ({len(events)} events) → {trace_path}")

        with open(path, "w") as f:
            json.dump(data, f, indent=2, default=str)
        console.print(f"[green]Mojo analysis saved to {path}")

    def dump_chrome_trace(self, path: str = "chrome_trace.json") -> None:
        """Save raw trace in Chrome's trace viewer format (chrome://tracing)."""
        with open(path, "w") as f:
            json.dump({"traceEvents": self._trace_events}, f)
        console.print(
            f"[green]Chrome trace saved to {path}\n"
            "[dim]Open chrome://tracing and load this file to visualize."
        )
