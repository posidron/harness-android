"""Mojo IPC observation and testing.

Two complementary approaches:

1. **Passive tracing** (:class:`MojoTracer`)
   Capture ``mojom``/``ipc`` trace events via the CDP ``Tracing`` domain
   while exercising Web APIs.  Maps the renderer→browser attack surface.

2. **Active MojoJS** (:class:`MojoJS`)
   Drive ``Mojo.bindInterface`` / ``MojoHandle.writeMessage`` directly
   from the harness over CDP.  Requires Chrome started with
   ``--enable-blink-features=MojoJS,MojoJSTest`` (call
   :func:`enable_mojojs`).  Lets you bind any browser-process interface
   and send raw or typed messages without an HTML page.
"""

from __future__ import annotations

import base64
import json
import re
import struct
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from rich.table import Table

from harness_android.browser import Browser, TargetCrashed
from harness_android.fileserver import FileServer
from harness_android.console import console



# ======================================================================
# Constants
# ======================================================================

MOJOJS_FLAGS = ["--enable-blink-features=MojoJS,MojoJSTest"]

MOJO_TRACE_CATEGORIES = [
    "mojom",
    "ipc",
    "toplevel",
    "toplevel.flow",
    "disabled-by-default-mojom",
    "disabled-by-default-ipc.flow",
]

MOJO_VERBOSE_CATEGORIES = [
    *MOJO_TRACE_CATEGORIES,
    "blink", "content", "navigation", "ServiceWorker", "loading",
]

# (name, JS expression, expected mojom interface)
MOJO_WEB_API_TRIGGERS: list[tuple[str, str, str]] = [
    ("Permissions.query",
     "navigator.permissions.query({name:'geolocation'}).then(r=>r.state)",
     "blink.mojom.PermissionService"),
    ("Notifications.permission",
     "Notification.requestPermission()",
     "blink.mojom.NotificationService"),
    ("Clipboard.readText",
     "navigator.clipboard.readText()",
     "blink.mojom.ClipboardHost"),
    ("Clipboard.writeText",
     "navigator.clipboard.writeText('harness_test')",
     "blink.mojom.ClipboardHost"),
    ("Geolocation.getCurrentPosition",
     "new Promise(r=>navigator.geolocation.getCurrentPosition("
     "p=>r(p.coords.latitude),e=>r(e.message),{timeout:3000}))",
     "device.mojom.Geolocation"),
    ("MediaDevices.enumerateDevices",
     "navigator.mediaDevices.enumerateDevices().then(d=>d.length)",
     "blink.mojom.MediaDevicesDispatcherHost"),
    ("MediaDevices.getUserMedia",
     "navigator.mediaDevices.getUserMedia({audio:true}).then(s=>{s.getTracks().forEach(t=>t.stop());return 'ok'})",
     "blink.mojom.MediaStreamDispatcherHost"),
    ("Credentials.get",
     "navigator.credentials.get({password:true}).then(c=>c?'found':'none')",
     "blink.mojom.CredentialManager"),
    ("WebUSB.getDevices",
     "navigator.usb?navigator.usb.getDevices().then(d=>d.length):'unavailable'",
     "device.mojom.UsbDeviceManager"),
    ("WebBluetooth.getAvailability",
     "navigator.bluetooth?navigator.bluetooth.getAvailability():'unavailable'",
     "blink.mojom.WebBluetoothService"),
    ("StorageManager.estimate",
     "navigator.storage.estimate().then(e=>e.quota)",
     "blink.mojom.QuotaManagerHost"),
    ("CacheStorage.open",
     "caches.open('harness_test').then(()=>'ok')",
     "blink.mojom.CacheStorage"),
    ("Locks.request",
     "navigator.locks.request('harness_test',()=>'ok')",
     "blink.mojom.LockManager"),
    ("IndexedDB.open",
     "new Promise(r=>{const q=indexedDB.open('harness_test');"
     "q.onsuccess=()=>r('ok');q.onerror=e=>r(String(e.target.error))})",
     "blink.mojom.IDBFactory"),
    ("ServiceWorker.getRegistrations",
     "navigator.serviceWorker.getRegistrations().then(r=>r.length)",
     "blink.mojom.ServiceWorkerContainerHost"),
    ("WakeLock.request",
     "navigator.wakeLock.request('screen').then(s=>{s.release();return 'ok'})",
     "blink.mojom.WakeLockService"),
    ("Sensor.Accelerometer",
     "typeof Accelerometer!=='undefined'?(new Accelerometer().start(),'started'):'unavailable'",
     "device.mojom.SensorProvider"),
    ("BroadcastChannel",
     "(()=>{const c=new BroadcastChannel('harness');c.postMessage('x');c.close();return 'ok'})()",
     "blink.mojom.BroadcastChannelProvider"),
]

DEFAULT_PERMISSIONS = [
    "geolocation", "notifications", "clipboardReadWrite",
    "clipboardSanitizedWrite", "audioCapture", "videoCapture",
    "sensors", "backgroundSync", "durableStorage", "wakeLockScreen",
]

FUZZ_STRINGS: list[str] = [
    "''", "'a'.repeat(10000)", "'a'.repeat(1000000)", "null", "undefined",
    "0", "-1", "NaN", "Infinity", "true", "false", "[]", "{}",
    "new ArrayBuffer(0)", "new Uint8Array(0)", "new Blob([])",
    "Symbol('x')", "Object.create(null)",
    "'\\x00'.repeat(100)", "'\\ud800'", "'\\udbff\\udfff'",
    "String.fromCharCode(...Array.from({length:256},(_,i)=>i))",
]


# ======================================================================
# Data types
# ======================================================================

@dataclass
class MojoMessage:
    interface: str = ""
    method: str = ""
    phase: str = ""
    process_id: int = 0
    timestamp_us: int = 0
    duration_us: int = 0
    args: dict[str, Any] = field(default_factory=dict)


@dataclass
class TriggerResult:
    api_name: str
    mojo_interface: str
    result: Any = None
    error: str = ""
    crashed: bool = False
    duration_ms: float = 0.0


# ======================================================================
# Setup
# ======================================================================

def enable_mojojs(
    browser: Browser,
    *,
    gen_dir: str | None = None,
    serve_port: int = 8089,
    extra_flags: Iterable[str] = (),
) -> FileServer | None:
    """Restart the browser with MojoJS enabled and (optionally) serve
    a Chromium ``gen/`` directory so generated mojom JS bindings resolve.

    Returns the :class:`FileServer` if *gen_dir* was provided, else None.
    """
    browser.add_flags(*MOJOJS_FLAGS, *extra_flags)
    browser.enable_cdp()

    server: FileServer | None = None
    if gen_dir:
        server = FileServer(gen_dir, port=serve_port)
        server.start()
        # Reverse-forward so the device reaches the host on 127.0.0.1.
        browser.adb.reverse(serve_port, serve_port)

    browser.connect()
    if not browser.evaluate_js("typeof Mojo !== 'undefined'"):
        raise RuntimeError(
            "MojoJS not available — the browser ignored "
            "--enable-blink-features=MojoJS,MojoJSTest. Use a debuggable build."
        )
    console.print("[green bold]MojoJS enabled — Mojo.bindInterface is available.")
    return server


# ======================================================================
# Active MojoJS driver
# ======================================================================

class MojoJS:
    """Drive ``Mojo.*`` from the harness over CDP — no HTML page required.

    Maintains a JS-side registry of open pipe handles so multiple
    interfaces can be bound and addressed by index.
    """

    _BOOTSTRAP = """
    if (!window.__mojo) {
      window.__mojo = {
        handles: [],
        bind(name, scope) {
          const p = Mojo.createMessagePipe();
          Mojo.bindInterface(name, p.handle1, scope || 'process');
          this.handles.push(p.handle0);
          return this.handles.length - 1;
        },
        write(idx, b64, handles) {
          const raw = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
          const h = this.handles[idx];
          if (!h || !h.writeMessage)
            throw new Error('writeMessage unavailable (need MojoJSTest)');
          return h.writeMessage(raw, handles || []);
        },
        watch(idx) {
          return new Promise(res => {
            const h = this.handles[idx];
            const w = h.watch({readable: true}, () => {
              w.cancel();
              res(h.readMessage());
            });
            setTimeout(() => { try { w.cancel(); } catch(e){} res(null); }, 5000);
          });
        },
        close(idx) {
          const h = this.handles[idx];
          if (h) { h.close(); this.handles[idx] = null; }
        },
        // Runtime probe: bind `name`, write a minimal header with method 0,
        // then wait up to `timeoutMs` for the remote peer to close the pipe.
        // A closed peer strongly suggests `name` is not registered with the
        // current frame's BinderMap (browser/renderer dropped the request).
        // Staying open OR replying means the interface IS registered.
        probeRegistered(name, scope, timeoutMs) {
          return new Promise(res => {
            let pipe;
            try {
              pipe = Mojo.createMessagePipe();
              Mojo.bindInterface(name, pipe.handle1, scope || 'process');
            } catch (e) {
              return res({registered: false, error: String(e && e.message || e)});
            }
            const h = pipe.handle0;
            // 24-byte v1 header, method 0, no flags, request_id 0.
            const hdr = new Uint8Array([
              24,0,0,0,  1,0,0,0,  0,0,0,0,  0,0,0,0,  0,0,0,0,0,0,0,0,
            ]);
            try {
              if (h.writeMessage) h.writeMessage(hdr, []);
            } catch (e) { /* ignore — probe is the wait, not the write */ }
            let done = false;
            const finish = r => { if (done) return; done = true; try { h.close(); } catch(e){} res(r); };
            let peer, reader;
            try {
              peer = h.watch({peerClosed: true}, () => finish({registered: false, reason: 'peer_closed'}));
            } catch (e) { /* older builds: no peerClosed */ }
            try {
              reader = h.watch({readable: true}, () => finish({registered: true, reason: 'readable'}));
            } catch (e) { /* ignore */ }
            setTimeout(() => {
              try { if (peer) peer.cancel(); } catch(e){}
              try { if (reader) reader.cancel(); } catch(e){}
              finish({registered: true, reason: 'timeout_no_disconnect'});
            }, timeoutMs || 200);
          });
        },
      };
    }
    true
    """

    def __init__(self, browser: Browser):
        self.browser = browser
        if not browser.evaluate_js("typeof Mojo !== 'undefined'"):
            raise RuntimeError("MojoJS not enabled — call enable_mojojs() first.")
        browser.evaluate_js(self._BOOTSTRAP)

    def bind(self, interface: str, scope: str = "process") -> int:
        """Bind *interface* in the browser process. Returns a handle index."""
        return int(self.browser.evaluate_js(
            f"__mojo.bind({json.dumps(interface)}, {json.dumps(scope)})"
        ))

    def write(self, handle: int, payload: bytes) -> Any:
        b64 = base64.b64encode(payload).decode("ascii")
        return self.browser.evaluate_js(
            f"__mojo.write({handle}, {json.dumps(b64)})",
            await_promise=False,
        )

    def read(self, handle: int) -> dict | None:
        return self.browser.evaluate_js(f"__mojo.watch({handle})", await_promise=True)

    def close(self, handle: int) -> None:
        self.browser.evaluate_js(f"__mojo.close({handle})")

    @staticmethod
    def make_header(name: int = 0, flags: int = 0, request_id: int = 0) -> bytes:
        """Build a minimal v1 Mojo message header (for raw fuzzing)."""
        # struct: num_bytes(u32) version(u32) name(u32) flags(u32) request_id(u64)
        return struct.pack("<IIIIq", 24, 1, name, flags, request_id)

    def fuzz_interface(
        self,
        interface: str,
        payloads: Iterable[bytes],
        *,
        scope: str = "process",
        settle_ms: int = 50,
    ) -> list[TriggerResult]:
        """Bind *interface* and write each payload, detecting renderer crashes."""
        results: list[TriggerResult] = []
        console.print(f"[bold]Fuzzing {interface} (raw IPC) …")
        for i, payload in enumerate(payloads):
            r = TriggerResult(api_name=f"raw#{i}", mojo_interface=interface)
            t0 = time.monotonic()
            try:
                h = self.bind(interface, scope)
                self.write(h, payload)
                # Give the browser process time to handle the message before
                # we close — otherwise it may be discarded on disconnect.
                time.sleep(settle_ms / 1000)
                self.close(h)
                # Probe liveness — raises TargetCrashed if the renderer died.
                self.browser.evaluate_js("1")
                r.result = f"{len(payload)}b sent"
            except TargetCrashed as exc:
                r.crashed = True
                r.error = str(exc)
                console.print(f"  [red bold]CRASH[/]  payload #{i} ({len(payload)}b)")
                self.browser.reconnect()
                self.browser.evaluate_js(self._BOOTSTRAP)
            except Exception as exc:  # noqa: BLE001
                r.error = str(exc)
            r.duration_ms = (time.monotonic() - t0) * 1000
            status = "[red]CRASH" if r.crashed else ("[yellow]err" if r.error else "[green]ok")
            console.print(f"  {status}[/]  #{i:<3} {len(payload):>6}b  {r.error[:60]}")
            results.append(r)
        return results

    @staticmethod
    def default_payloads() -> list[bytes]:
        hdr = MojoJS.make_header
        return [
            b"",
            b"\x00" * 4,
            hdr(),
            hdr(name=0xFFFFFFFF),
            hdr(flags=0xFFFFFFFF),
            hdr() + b"\x41" * 256,
            hdr() + b"\xFF" * 256,
            hdr() + bytes(range(256)),
            b"\xFF" * 24,
            hdr() + b"\x00" * 65536,
        ]

    # -- self-enumeration ----------------------------------------------

    def probe(self, interface: str, scope: str = "process",
              timeout_ms: int = 200) -> dict[str, Any]:
        """Probe whether *interface* is registered with the current frame.

        Binds a pipe, writes a minimal v1 header with method 0, then waits
        up to *timeout_ms* for the remote peer to close. Registered
        interfaces usually keep the pipe open or produce a readable reply;
        unregistered names trigger an immediate peer-close.

        Returns ``{"interface": name, "registered": bool, "reason": str}``.
        Note: this is a **heuristic**. Some real interfaces deliberately
        close on malformed input (false negative), and peer-close timing
        varies by build. Use together with ``discover_interfaces_from_gen``
        for best coverage.
        """
        js = (
            "__mojo.probeRegistered("
            f"{json.dumps(interface)}, {json.dumps(scope)}, {int(timeout_ms)})"
        )
        try:
            out = self.browser.evaluate_js(js, await_promise=True,
                                           timeout=timeout_ms / 1000 + 5) or {}
        except Exception as exc:  # noqa: BLE001
            out = {"registered": False, "reason": f"eval_error: {exc}"}
        return {"interface": interface,
                "registered": bool(out.get("registered")),
                "reason": out.get("reason", out.get("error", ""))}

    def probe_all(
        self,
        interfaces: Iterable[str],
        *,
        scope: str = "process",
        timeout_ms: int = 200,
        stop_on_crash: bool = True,
    ) -> list[dict[str, Any]]:
        """Probe every interface sequentially; reconnects on renderer crash.

        Warning: sequential cost is O(len(interfaces) * timeout_ms). For a
        full ``discover_interfaces_from_gen`` result (thousands of names)
        this takes minutes; filter first (e.g. to ``blink.mojom.*`` or a
        trace-derived subset).
        """
        results: list[dict[str, Any]] = []
        names = list(interfaces)
        console.print(
            f"[bold]Probing {len(names)} Mojo interfaces "
            f"(timeout={timeout_ms}ms each) …"
        )
        for iface in names:
            try:
                r = self.probe(iface, scope=scope, timeout_ms=timeout_ms)
            except TargetCrashed as exc:
                console.print(f"  [red bold]CRASH[/] probing {iface}: {exc}")
                results.append({"interface": iface, "registered": False,
                                "reason": f"renderer_crash: {exc}",
                                "crashed": True})
                self.browser.reconnect()
                self.browser.evaluate_js(self._BOOTSTRAP)
                if stop_on_crash:
                    break
                continue
            results.append(r)
        registered = sum(1 for r in results if r.get("registered"))
        console.print(
            f"[green]{registered}/{len(results)} interfaces appear registered "
            f"from this origin."
        )
        return results


# ======================================================================
# Interface discovery (static + observational)
# ======================================================================

# Matches the module+interface name emitted in generated JS bindings, e.g.
#   mojo.internal.interfaceProxy('blink.mojom.ClipboardHost', ...)
#   or class definitions like
#   export const ClipboardHost = { $: { name: 'blink.mojom.ClipboardHost' ...
_MOJOM_NAME_RE = re.compile(
    r"""['"]([a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*\.mojom(?:\.[A-Z][A-Za-z0-9_]*)?\.[A-Z][A-Za-z0-9_]+)['"]""",
    re.VERBOSE,
)


def discover_interfaces_from_gen(gen_dir: str | Path) -> list[str]:
    """Walk a Chromium ``gen/`` tree and extract every mojom interface
    name that appears in the generated JS bindings. Returns a sorted
    de-duplicated list of ``module.mojom.Interface`` strings.

    This is the authoritative inventory of *every* interface the current
    build can expose — independent of which ones the trace happened to
    capture or which ones ``MOJO_WEB_API_TRIGGERS`` knows about.
    """
    gen = Path(gen_dir)
    if not gen.exists():
        raise FileNotFoundError(f"gen directory not found: {gen}")

    found: set[str] = set()
    # Prefer the smaller *-lite.js if present; fall back to mojom.js otherwise.
    for pattern in ("*.mojom-lite.js", "*.mojom.js"):
        for path in gen.rglob(pattern):
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for m in _MOJOM_NAME_RE.finditer(text):
                name = m.group(1)
                # Reject module-only strings with no trailing Interface.
                if name.count(".") >= 2 and name.split(".")[-1][0].isupper():
                    found.add(name)
    return sorted(found)


# ======================================================================
# Passive tracer
# ======================================================================

class MojoTracer:
    """Capture and analyse Mojo IPC via the CDP ``Tracing`` domain."""

    # Backward-compat alias for older example scripts.
    FUZZ_STRINGS = FUZZ_STRINGS

    def __init__(self, browser: Browser, verbose: bool = False):
        self.browser = browser
        self.verbose = verbose
        self._tracing = False
        self._trace_events: list[dict] = []

    # -- tracing --------------------------------------------------------

    def start_trace(self) -> None:
        cats = MOJO_VERBOSE_CATEGORIES if self.verbose else MOJO_TRACE_CATEGORIES
        self.browser.send("Tracing.start", {
            "traceConfig": {"includedCategories": cats, "recordMode": "recordUntilFull"},
            "transferMode": "ReturnAsStream",
        })
        self._tracing = True
        self._trace_events = []
        console.print(f"[green]Mojo trace started ({len(cats)} categories)")

    def stop_trace(self, *, timeout: float = 60) -> list[dict]:
        if not self._tracing:
            return []
        self.browser.send("Tracing.end")
        self._tracing = False

        # All events are buffered by the CDP session — drain them properly.
        ev = self.browser.wait_event("Tracing.tracingComplete", timeout=timeout)
        events: list[dict] = []
        for chunk in self.browser.drain_events("Tracing.dataCollected"):
            events.extend(chunk.get("params", {}).get("value", []))

        stream = ev.get("params", {}).get("stream")
        if stream:
            events.extend(self._read_trace_stream(stream))

        self._trace_events = events
        console.print(f"[green]Trace stopped — {len(events)} events captured")
        return events

    def _read_trace_stream(self, handle: str) -> list[dict]:
        chunks: list[str] = []
        while True:
            r = self.browser.send("IO.read", {"handle": handle, "size": 1 << 20})
            data = r.get("data", "")
            if r.get("base64Encoded"):
                data = base64.b64decode(data).decode("utf-8", errors="replace")
            chunks.append(data)
            if r.get("eof"):
                break
        self.browser.send("IO.close", {"handle": handle})

        buf = "".join(chunks)
        try:
            parsed = json.loads(buf)
        except json.JSONDecodeError:
            # Trace JSON is sometimes emitted unterminated — only try the
            # "close the array" repair when the buffer actually *looks*
            # truncated (trailing comma after an object), otherwise a
            # mid-value corruption would be silently replaced with [].
            stripped = buf.rstrip()
            if stripped.endswith(",") or stripped.endswith("}"):
                try:
                    parsed = json.loads(stripped.rstrip(",") + "]")
                except json.JSONDecodeError as exc:
                    console.print(
                        f"[yellow]Trace JSON could not be repaired: {exc}"
                    )
                    return []
            else:
                console.print(
                    "[yellow]Trace stream does not look like valid JSON "
                    "(no trailing ',' or '}'); returning no events."
                )
                return []
        if isinstance(parsed, dict):
            return parsed.get("traceEvents", [])
        return parsed if isinstance(parsed, list) else []

    # -- discovery ------------------------------------------------------

    def discover_interfaces_from_trace(
        self,
        messages: list[MojoMessage] | None = None,
    ) -> list[str]:
        """Unique ``module.mojom.Interface`` names observed in the trace.

        This is *observational* discovery — only interfaces that actually
        sent or received at least one IPC during the capture window. For
        the authoritative inventory of everything the build can expose,
        see :func:`discover_interfaces_from_gen`.
        """
        msgs = messages if messages is not None else self.extract_mojo_messages()
        seen: set[str] = set()
        for m in msgs:
            if m.interface and ".mojom" in m.interface.lower():
                seen.add(m.interface)
        return sorted(seen)

    # -- analysis -------------------------------------------------------

    def extract_mojo_messages(self, events: list[dict] | None = None) -> list[MojoMessage]:
        if events is None:
            events = self._trace_events

        msgs: list[MojoMessage] = []
        for ev in events:
            cat = ev.get("cat", "")
            name = ev.get("name", "")
            args = ev.get("args", {})

            iface, method = "", ""
            if "mojom" in cat or ".mojom." in name:
                # name format: "Send mojo::Foo::Bar" / "blink.mojom.X::Y" / "X.Y"
                token = name
                for prefix in ("Send ", "Receive ", "Call ", "Invoke "):
                    if token.startswith(prefix):
                        token = token[len(prefix):]
                        break
                if "::" in token:
                    iface, method = token.rsplit("::", 1)
                elif "." in token:
                    iface, method = token.rsplit(".", 1)
                else:
                    iface = token
            elif "ipc" in cat and isinstance(args, dict) and "name" in args.get("info", {}):
                iface = args["info"]["name"]
            else:
                continue

            msgs.append(MojoMessage(
                interface=iface,
                method=method,
                phase=ev.get("ph", ""),
                process_id=ev.get("pid", 0),
                timestamp_us=ev.get("ts", 0),
                duration_us=ev.get("dur", 0),
                args=args if isinstance(args, dict) else {},
            ))
        return msgs

    # -- triggering -----------------------------------------------------

    def _eval_with_timeout(self, js: str, ms: int = 5000) -> Any:
        wrapped = (
            "Promise.race(["
            f"(async()=>{{try{{return await ({js})}}catch(e){{return 'err:'+e.message}}}})(),"
            f"new Promise(r=>setTimeout(()=>r('timeout'),{ms}))"
            "])"
        )
        return self.browser.evaluate_js(wrapped, await_promise=True, timeout=ms / 1000 + 10)

    def trigger_api(self, name: str, js: str, iface: str) -> TriggerResult:
        r = TriggerResult(api_name=name, mojo_interface=iface)
        t0 = time.monotonic()
        try:
            r.result = self._eval_with_timeout(js)
        except TargetCrashed as exc:
            r.crashed, r.error = True, str(exc)
            self.browser.reconnect()
        except Exception as exc:  # noqa: BLE001
            r.error = str(exc)
        r.duration_ms = (time.monotonic() - t0) * 1000
        return r

    def trigger_all_apis(self, *, origin: str | None = None) -> list[TriggerResult]:
        origin = origin or self.browser.evaluate_js("location.origin")
        try:
            self.browser.grant_permissions(DEFAULT_PERMISSIONS, origin=origin)
        except Exception as exc:  # noqa: BLE001
            console.print(f"[dim]grant_permissions skipped: {exc}")

        console.print(f"[bold]Triggering {len(MOJO_WEB_API_TRIGGERS)} Mojo-backed Web APIs …")
        out: list[TriggerResult] = []
        for name, js, iface in MOJO_WEB_API_TRIGGERS:
            r = self.trigger_api(name, js, iface)
            tag = "[red]CRASH" if r.crashed else ("[yellow]err" if r.error else "[green]ok")
            console.print(f"  {tag}[/]  {name:38s} → {r.error or r.result}")
            out.append(r)
            if r.crashed:
                break
        return out

    def trigger_selected_apis(self, *names: str) -> list[TriggerResult]:
        return [self.trigger_api(n, j, i)
                for n, j, i in MOJO_WEB_API_TRIGGERS if n in names]

    def fuzz_api(self, api_name: str, js_template: str,
                 inputs: list[str], iface: str = "") -> list[TriggerResult]:
        console.print(f"[bold]Fuzzing {api_name} with {len(inputs)} inputs …")
        out: list[TriggerResult] = []
        for inp in inputs:
            r = self.trigger_api(f"{api_name}[{inp[:30]}]",
                                 js_template.replace("{FUZZ}", inp), iface)
            out.append(r)
            if r.crashed:
                break
        return out

    # -- output ---------------------------------------------------------

    def print_summary(self, messages: list[MojoMessage] | None = None) -> None:
        messages = messages if messages is not None else self.extract_mojo_messages()
        if not messages:
            console.print("[yellow]No Mojo IPC messages captured.")
            return
        counts: dict[str, int] = {}
        for m in messages:
            key = f"{m.interface}::{m.method}" if m.method else m.interface
            counts[key] = counts.get(key, 0) + 1
        t = Table(title=f"Mojo IPC Summary ({len(messages)} messages)")
        t.add_column("Interface::Method", style="bold")
        t.add_column("Count", justify="right")
        for k, v in sorted(counts.items(), key=lambda x: -x[1])[:50]:
            t.add_row(k, str(v))
        console.print(t)

    def print_trigger_results(self, results: list[TriggerResult]) -> None:
        t = Table(title="Web API Trigger Results")
        t.add_column("API", style="bold")
        t.add_column("Mojo Interface")
        t.add_column("Result")
        t.add_column("ms", justify="right")
        for r in results:
            mark = "[red]CRASH " if r.crashed else ""
            t.add_row(r.api_name, r.mojo_interface,
                      f"{mark}{str(r.error or r.result)[:60]}", f"{r.duration_ms:.0f}")
        console.print(t)

    def dump(self, path: str = "mojo_trace.json",
             events: list[dict] | None = None,
             messages: list[MojoMessage] | None = None,
             trigger_results: list[TriggerResult] | None = None) -> None:
        events = events if events is not None else self._trace_events
        data: dict[str, Any] = {"trace_event_count": len(events)}
        if messages is not None:
            data["mojo_messages"] = [
                {"interface": m.interface, "method": m.method, "phase": m.phase,
                 "pid": m.process_id, "ts": m.timestamp_us, "dur": m.duration_us}
                for m in messages
            ]
        if trigger_results is not None:
            data["trigger_results"] = [
                {"api": r.api_name, "interface": r.mojo_interface,
                 "result": str(r.result), "error": r.error,
                 "crashed": r.crashed, "ms": r.duration_ms}
                for r in trigger_results
            ]
        if events:
            raw_path = path.replace(".json", "_raw.json")
            with open(raw_path, "w") as f:
                json.dump({"traceEvents": events}, f)
            console.print(f"[dim]Raw trace ({len(events)} events) → {raw_path}")
        with open(path, "w") as f:
            json.dump(data, f, indent=2, default=str)
        console.print(f"[green]Mojo analysis saved to {path}")

    def dump_chrome_trace(self, path: str = "chrome_trace.json") -> None:
        with open(path, "w") as f:
            json.dump({"traceEvents": self._trace_events}, f)
        console.print(f"[green]Chrome trace → {path} (open in chrome://tracing)")
