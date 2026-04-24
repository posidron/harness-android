# Architecture

This document describes the design, module structure, and data flows of harness-android.

---

## Overview

harness-android is a cross-platform (Windows/macOS) Android emulator harness built for browser penetration testing. It automates the entire stack: downloading a JDK and Android SDK, booting a QEMU-based emulator, establishing ADB communication, and providing Chrome browser control via the DevTools Protocol — all from Python.

```
┌─────────────────────────────────────────────────────────────────────────┐
│                      User: CLI / Python Script                          │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│   cli.py ─────► device.py ─────► pentest.py                             │
│     │               │                │                                  │
│     │          ┌────┴────┐     ┌─────┴──────┐                           │
│     │          │         │     │            │                           │
│     ▼          ▼         ▼     ▼            ▼                           │
│  emulator.py  adb.py  browser.py  hooks.py  intercept.py                │
│     │          │         │         │         │                          │
│     │          │         │         │         │         proxy.py         │
│     │          │         │         │         │           │              │
│     ▼          ▼         ▼         ▼         ▼           ▼              │
│  ┌───────────────────────────────────────────────────────────────┐      │
│  │              Android Emulator (QEMU)                          │      │
│  │  ┌─────────┐  ┌──────────┐  ┌──────────────────────────┐      │      │
│  │  │ Android │  │   ADB    │  │  Chrome + DevTools socket│      │      │
│  │  │  OS     │◄─┤  daemon  ├──┤  chrome_devtools_remote  │      │      │
│  │  └─────────┘  └──────────┘  └──────────────────────────┘      │      │
│  └───────────────────────────────────────────────────────────────┘      │
│                                                                         │
│  sdk.py + config.py  (JDK bootstrap, SDK paths, platform detection)     │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## Module inventory

### Foundation layer

| Module | Responsibility |
|---|---|
| **config.py** | Platform detection (Windows/macOS/Linux), default constants (API level, AVD name, ports), path resolution for SDK/JDK/AVD roots, download URLs for cmdline-tools and Adoptium JDK 17 |
| **sdk.py** | JDK bootstrap (downloads portable OpenJDK 17 if no `JAVA_HOME`), SDK cmdline-tools download and extraction, `sdkmanager` wrapper for package installation, licence acceptance. Builds env dicts with `JAVA_HOME` injected. All archive extraction goes through `_safe_extract_zip` / `_safe_extract_tar` helpers that reject zip-slip / tar-slip entries (CVE-2007-4559 class). |

### Emulator & device layer

| Module | Responsibility |
|---|---|
| **emulator.py** | AVD creation/deletion via `avdmanager`, emulator process launch (headless, GPU, RAM config), serial detection, graceful shutdown. Always injects `JAVA_HOME` into subprocess env. |
| **adb.py** | Thin wrapper around the `adb` CLI. Shell commands, app install/uninstall, file push/pull, screenshots, screen recording, port forwarding, input events (tap, swipe, text, keycodes), device property queries. |
| **device.py** | High-level facade combining `Emulator`, `ADB`, and `Browser`. Provides a context manager (`with Device() as dev`) for one-liner scripting. Delegates to the lower layers. |

### Browser control layer

| Module | Responsibility |
|---|---|
| **browser.py** | Chrome lifecycle (start, stop, clear data) via ADB intents. CDP setup: writes `chrome-command-line` flags (`--enable-remote-debugging`, `--remote-allow-origins=*`), restarts Chrome, sets up ADB abstract socket forwarding, polls `/json` endpoint. WebSocket messaging: send CDP commands, receive responses (120s deadline, handles timeout recovery). High-level CDP helpers: navigate, evaluate JS, DOM interaction, screenshots, cookies, user-agent override, Security domain, certificate error bypass, device emulation, cache control. **Main-frame response headers** are captured during `navigate()` from CDP `Network.responseReceived` events and exposed via `Browser.main_frame_response_headers` so recon can inspect the real server response without a CORS-filtered page-JS fetch. **CDP Input domain**: `dispatch_touch()`, `dispatch_swipe()`, `dispatch_key()` for realistic browser-level touch/keyboard events. |

### Pentest layer

| Module | Responsibility |
|---|---|
| **proxy.py** | Device HTTP proxy toggle via `settings put global http_proxy`. CA certificate installation (system trust store or user store fallback). mitmproxy CA auto-detection. tcpdump start/stop/pull. DNS manipulation via `/etc/hosts`. |
| **hooks.py** | 8 built-in JavaScript hooks injected via `Page.addScriptToEvaluateOnNewDocument`. Each hook wraps a browser API (fetch, XHR, document.cookie, WebSocket, postMessage, console, localStorage/sessionStorage, form submit) with its own per-slot guard flag so installing one hook never blocks the others. Hooks log calls to `window.__harness_hooks__`. Collect/clear/dump captured data. Custom hook support. |
| **intercept.py** | CDP `Fetch` domain wrapper. Decorator-based handler registration (`@interceptor.on_request`, `@interceptor.on_response`) with glob-style URL matching. Pauses requests at request or response stage, lets handlers inspect/modify/replace, then continues or fulfills. Background listener thread. Request log with JSON dump. |
| **recon.py** | **Fingerprint**: detects JS frameworks (React, Angular, Vue, jQuery, Next.js, Nuxt, Svelte, Bootstrap, Tailwind), meta generator tag. **Spider**: extracts all links, forms (action/method/fields), script sources, iframes. **Storage**: dumps cookies, localStorage, sessionStorage. **CSP**: reads both `Content-Security-Policy` and `Content-Security-Policy-Report-Only` from the real main-frame response headers (cached by `Browser.navigate()`), falls back to `<meta http-equiv>`, and flags unsafe-inline, unsafe-eval, wildcards, missing directives, meta-only / report-only delivery. **Security headers**: honest missing/present report based on the captured response headers — no secondary fetch. **full_recon()**: runs all of the above and writes a UTF-8 JSON report. |
| **pentest.py** | `PentestContext` — rich object passed to user pentest scripts, bundles ADB + Browser + Hooks + Interceptor + Proxy + Recon with convenience methods (navigate, click, type, screenshot, add_finding, report). `run_script()` loads a user `.py` file with `run(ctx)` and executes it. |
| **mojo.py** | **Mojo IPC testing** via CDP `Tracing` domain. `MojoTracer` starts/stops Chrome traces with Mojo-specific categories (`mojom`, `ipc`, `toplevel`). 23 built-in Web API triggers that exercise specific `*.mojom.*` interfaces (Permissions, Clipboard, Geolocation, MediaDevices, WebUSB, WebBluetooth, Storage, IndexedDB, ServiceWorker, etc.). Trace parser extracts Mojo messages (interface, method, process, timing). Fuzz helper substitutes payloads into Web API calls. Output in JSON + Chrome trace format (loadable in `chrome://tracing`). |
| **forensics.py** | APK secret scanning (27 regex patterns: AWS, Google, GitHub, Slack, Stripe, JWT, PEM, Azure, etc.), AndroidManifest.xml security audit (debuggable, allowBackup, exported components, permissions), on-device app data extraction with SQLite scanning. `scan-app` command auto-pulls APK by package name. |
| **logcat.py** | Background logcat capture with `logcat -c` buffer clear. Crash detection for FATAL EXCEPTION, SIGSEGV, ANR, ASan/AddressSanitizer, tombstones. Dedup key is `(event_type, pid, timestamp, message[:80])` so distinct crashes in the same app are preserved. `print_crashes()` / `dump_crashes()` for analysis. |
| **intents.py** | Intent fuzzing for exported components. Parses AndroidManifest.xml for exported activities/services/receivers/providers, generates type-appropriate payloads from a declarative corpus (path traversal, SQLi, deep-link abuse, huge strings, null bytes, etc.) using real provider authorities per target, monitors logcat for crashes after each fuzz attempt. |
| **webview.py** | Enumerates debuggable WebView sockets via `/proc/net/unix`. `connect_webview()` establishes a CDP connection to any listed WebView. Auto-detects `chrome_devtools_remote` for full `enable_cdp()` flow. |
| **ui.py** | **UIAutomator**: dumps screen hierarchy via `uiautomator dump`, parses XML into element tree with bounds/text/resource-id/class. Find helpers: `find_by_text()`, `find_by_resource_id()`, `find_by_content_desc()`, `find_clickable()`. **Smart tap**: `tap_element()` locates element by text and taps its computed centre. **Monkey**: `run_monkey()` wraps the `monkey` random event generator with crash/ANR parsing. |
| **fileserver.py** | Simple HTTP server that serves a local directory to the emulator (host = `10.0.2.2`). Used to serve Chromium `gen/` folders with MojoJS bindings without pushing files to the device. Context manager support (`with FileServer(...) as server`). |

### CLI layer

| Module | Responsibility |
|---|---|
| **cli.py** | argparse-based CLI. 30+ commands across 14 groups: emulator management (setup, install-chromium, create, delete, start, stop, status), device control (shell, install, screenshot, push, pull, input), browser (open, cdp with --chrome-flags), proxy (enable, disable, status, install-ca, tcpdump, hosts), recon, hooks, pentest (run), mojo (trace, trigger, fuzz, enable), forensics (scan, secrets, manifest, scan-app, app-data, installed), intent (enumerate, fuzz), logcat (stream, capture), webview (list, connect), ui (dump, tap, type, monkey), serve. |

---

## Communication channels

### 1. ADB (Android Debug Bridge)

```
Host                              Emulator
┌──────────┐   USB/TCP 5037   ┌──────────────┐
│ adb.py   │ ───────────────► │  adbd        │
│          │ ◄─────────────── │  (on device) │
└──────────┘                  └──────────────┘
```

ADB is the primary control channel. All device interaction goes through it:

- **Shell commands**: `adb shell <cmd>` — run anything on the device
- **File transfer**: `adb push` / `adb pull`
- **App management**: `adb install` / `adb uninstall`
- **Port forwarding**: `adb forward tcp:9222 localabstract:chrome_devtools_remote`
- **Input injection**: `adb shell input tap/swipe/text/keyevent`
- **Property queries**: `adb shell getprop <prop>`

### 2. Chrome DevTools Protocol (CDP)

```
Host                                        Emulator
┌───────────┐    WebSocket     ┌─────────────────────────────┐
│ browser.py│ ──────────────►  │ Chrome                      │
│           │    (port 9222)   │  └─ DevTools socket         │
│           │ ◄──────────────  │     chrome_devtools_remote  │
└───────────┘                  └─────────────────────────────┘
                    ▲
                    │  ADB forward (abstract socket → TCP)
```

CDP is used for everything above the ADB layer:

1. **Page.navigate** — URL navigation
2. **Runtime.evaluate** — JavaScript execution
3. **Page.captureScreenshot** — pixel-perfect page screenshots
4. **Page.addScriptToEvaluateOnNewDocument** — JS hook injection
5. **Fetch.enable / Fetch.requestPaused** — HTTP request/response interception
6. **Network.enable / Network.getCookies** — cookie and network inspection
7. **Security.enable** — TLS/certificate state
8. **Emulation.setDeviceMetricsOverride** — viewport/fingerprint spoofing

**Setup sequence**:
1. Write `--enable-remote-debugging --remote-allow-origins=*` to `/data/local/tmp/chrome-command-line`
2. Force-stop and restart Chrome
3. `adb forward tcp:9222 localabstract:chrome_devtools_remote`
4. Poll `http://localhost:9222/json` until it responds
5. Open WebSocket to the first inspectable page's `webSocketDebuggerUrl`

### 3. HTTP proxy (intercepting)

```
Host                                          Emulator
┌────────────┐                   ┌──────────────────────────┐
│ mitmproxy  │ ◄──── HTTP(S) ──  │ All app traffic          │
│ / Burp     │ ────────────────► │ (proxy = 10.0.2.2:8080)  │
│ / ZAP      │                   └──────────────────────────┘
└────────────┘
       │
       ▼
  Inspect / modify / log all network traffic
```

The emulator sees the host machine as `10.0.2.2`. Proxy is set via:
```
adb shell settings put global http_proxy 10.0.2.2:8080
```

For HTTPS interception, the proxy's CA cert must be installed in the device's trust store. The harness handles this automatically for mitmproxy or any PEM cert.

### 4. tcpdump (packet capture)

```
Emulator                            Host
┌────────────────┐    adb pull    ┌──────────────┐
│ tcpdump -w     │ ────────────►  │ .pcap file   │
│ /sdcard/*.pcap │                │ (Wireshark)  │
└────────────────┘                └──────────────┘
```

Runs on the device as a background process. Captures all interfaces. Pull the pcap for offline analysis.

---

## Data flows

### Pentest script execution

```
1. CLI parses `pentest run script.py`
2. browser.enable_cdp()  →  writes chrome flags, restarts Chrome, sets up ADB forward
3. browser.connect()     →  WebSocket to CDP
4. pentest.run_script()  →  loads user script, creates PentestContext
5. Script calls:
   ├── ctx.hooks.install("fetch", "forms")
   │   └── Page.addScriptToEvaluateOnNewDocument  →  JS injected before page load
   ├── ctx.navigate(url)
   │   └── Page.navigate via CDP
   ├── ctx.type_in(selector, text)
   │   └── Runtime.evaluate via CDP
   ├── ctx.hooks.collect()
   │   └── Runtime.evaluate("window.__harness_hooks__") via CDP
   ├── ctx.recon()
   │   ├── fingerprint: Runtime.evaluate (framework detection)
   │   ├── spider: Runtime.evaluate (DOM queries for links/forms)
   │   ├── storage: Network.getCookies + Runtime.evaluate (localStorage)
   │   └── CSP: Runtime.evaluate (meta tag parse)
   ├── ctx.add_finding(title, severity)
   │   └── appends to in-memory findings list
   └── ctx.report(path)
       └── JSON.dump(findings + hook data)
```

### Request interception flow

```
1. interceptor.enable()
   └── Fetch.enable with URL patterns + request/response stage

2. Chrome sends Fetch.requestPaused event for matching URLs

3. _handle_request_paused():
   ├── Request stage:
   │   ├── build InterceptedRequest from event params
   │   ├── call matching @on_request handlers
   │   ├── if handler returns modifications → Fetch.continueRequest(modified)
   │   └── if no modification → Fetch.continueRequest(original)
   │
   └── Response stage:
       ├── Fetch.getResponseBody → read original response
       ├── call matching @on_response handlers
       ├── if handler returns modifications → Fetch.fulfillRequest(modified body)
       └── if no modification → Fetch.continueRequest(original)
```

### JS hook capture flow

```
1. hooks.install("fetch")
   └── Page.addScriptToEvaluateOnNewDocument(HOOK_FETCH)
       (script wraps window.fetch, logs calls to window.__harness_hooks__.fetch)

2. Page loads → hook script runs BEFORE any page JS

3. Page JS calls fetch("/api/data", {method: "POST", body: "..."})
   └── hook wrapper:
       ├── push {url, method, body, timestamp} to __harness_hooks__.fetch
       └── call original fetch()

4. hooks.collect()
   └── Runtime.evaluate("window.__harness_hooks__")
       → returns {fetch: [{url, method, body, timestamp}, ...], ...}
```

### Mojo IPC observation flow

```
                           Chrome (in emulator)
                    ┌──────────────────────────────────┐
                    │  Browser Process (privileged)    │
                    │     ▲                            │
                    │     │  Mojo IPC                  │
                    │     ▼                            │
                    │  Renderer Process (sandboxed)    │
                    │     ▲                            │
                    │     │  Web API call              │
                    │     │  (Permissions, Clipboard,  │
                    │     │   WebUSB, Geolocation, …)  │
                    └─────┼────────────────────────────┘
                          │
    ┌─────────────────────┼──────────────────────────────────────┐
    │  harness-android    │                                      │
    │                     │                                      │
    │  1. Tracing.start(categories=["mojom","ipc","toplevel"])   │
    │     → Chrome enables Mojo trace points                     │
    │                                                            │
    │  2. Runtime.evaluate(Web API JS)                           │
    │     → renderer calls navigator.permissions.query()         │
    │     → Mojo message: blink.mojom.PermissionService          │
    │     → traced by Chrome's internal instrumentation          │
    │                                                            │
    │  3. Tracing.end()                                          │
    │     → Tracing.dataCollected events with trace data         │
    │     → parse: extract interface names, methods, counts      │
    │                                                            │
    │  4. Output:                                                │
    │     ├── Summary table (interface → count)                  │
    │     ├── JSON analysis (mojo_trace.json)                    │
    │     └── Chrome trace (chrome://tracing loadable)           │
    └────────────────────────────────────────────────────────────┘
```

**Why this works**: Chrome's tracing infrastructure (`chrome://tracing`) records all Mojo IPC
messages as trace events when the `mojom` / `ipc` categories are enabled. The CDP `Tracing`
domain exposes this programmatically. By triggering Web APIs via `Runtime.evaluate`, we force
the renderer to send Mojo messages to the browser process, and the trace captures exactly which
interfaces and methods are exercised.

**What gets captured**:
- Interface name (e.g., `blink.mojom.PermissionService`)
- Method called (e.g., `HasPermission`)
- Process IDs for sender and receiver
- Timing (timestamp + duration in microseconds)
- Message direction (send / receive / reply)

**23 Web APIs tested**, covering:
- `blink.mojom.*` — Permissions, Notifications, Clipboard, MediaDevices, Credentials, WebBluetooth, QuotaManager, CacheStorage, LockManager, IDBFactory, ServiceWorker, FileSystemAccess, WakeLock, SharedWorker
- `device.mojom.*` — Geolocation, UsbDeviceManager, NFC, SensorProvider
- `shape_detection.mojom.*` — BarcodeDetection
- `payments.mojom.*` — PaymentRequest

---

## SDK bootstrap sequence

```
setup command
│
├── 1. bootstrap_jdk()
│   ├── check JAVA_HOME env → use if valid
│   ├── check ~/.android-harness/jdk/ → use if java binary exists
│   └── download Adoptium Temurin JDK 17 (zip on Windows, tar.gz on macOS)
│       └── extract to ~/.android-harness/jdk/
│
├── 2. bootstrap_sdk()
│   ├── check if sdkmanager exists → skip if yes
│   └── download commandlinetools zip from dl.google.com
│       └── extract to ~/.android-harness/sdk/cmdline-tools/latest/
│
├── 3. accept_licenses()
│   └── sdkmanager --licenses (auto-accepts with "y" input)
│
└── 4. install_packages(api_level)
    └── sdkmanager platform-tools emulator platforms;android-35 system-images;...
```

All `sdkmanager` and `avdmanager` calls include `JAVA_HOME` and prepend `<JDK>/bin` to `PATH` in the subprocess environment, so the bundled JDK is used even if none is installed system-wide.

---

## Emulator lifecycle

```
start command
│
├── 1. Check AVD exists → create if not (avdmanager create avd)
│
├── 2. Launch emulator process (subprocess.Popen, background)
│      Flags: -avd <name> -gpu auto -memory 2048 [-no-window] [-wipe-data]
│
├── 3. Detect serial: poll `adb devices` for emulator-XXXX
│
├── 4. adb wait-for-device
│
├── 5. Poll getprop sys.boot_completed == 1
│
└── 6. Return ADB handle → ready for use
```

Shutdown: `adb emu kill` → wait for process exit → fallback to `process.kill()`.

---

## Chrome CDP setup

```
enable_cdp()
│
├── 1. Write chrome-command-line flags to /data/local/tmp/
│      "_ --disable-fre --no-default-browser-check --no-first-run
│         --enable-remote-debugging --remote-allow-origins=*"
│
├── 2. am force-stop com.android.chrome
│
├── 3. am start -n com.android.chrome/...Main
│
├── 4. sleep 4s (Chrome startup)
│
├── 5. adb forward tcp:9222 localabstract:chrome_devtools_remote
│
└── 6. Poll http://localhost:9222/json (up to 20s)
       └── success → CDP ready
```

---

## Platform differences

| Aspect | Windows | macOS |
|---|---|---|
| Binary suffix | `.exe` / `.bat` | (none) |
| JDK archive | `.zip` | `.tar.gz` |
| JDK inner path | `jdk-17.0.13+11/` | `jdk-17.0.13+11/Contents/Home/` |
| PATH separator | `;` | `:` |
| Hypervisor | WHPX / HAXM | Hypervisor.framework (built-in) |
| SDK tools URL | `commandlinetools-win-*` | `commandlinetools-mac-*` |

These differences are handled in `config.py` via `_detect_platform()`, `_exe()`, `_bat()`, and in `sdk.py`/`emulator.py` via the env builder helpers.

---

## Directory layout at runtime

```
~/.android-harness/
├── jdk/
│   └── jdk-17.0.13+11/          # Portable OpenJDK 17
│       ├── bin/java[.exe]
│       └── ...
├── sdk/
│   ├── cmdline-tools/latest/     # sdkmanager, avdmanager
│   ├── platform-tools/           # adb
│   ├── emulator/                 # emulator binary
│   ├── platforms/android-35/     # Android 15 platform
│   └── system-images/            # Google APIs x86_64 image
└── avd/
    └── harness_device/           # AVD data
```

Overridable via `ANDROID_HARNESS_HOME`, `ANDROID_HOME`, and `JAVA_HOME` environment variables.

---

## Dependencies

| Package | Purpose |
|---|---|
| `requests` | HTTP client for SDK downloads and CDP `/json` endpoint |
| `websocket-client` | WebSocket connection to Chrome DevTools Protocol |
| `rich` | Terminal UI: progress bars, tables, colored output |
| `Pillow` | Image handling for screenshots |

All external communication is with:
- `dl.google.com` (SDK downloads)
- `github.com/adoptium` (JDK downloads)
- `localhost:9222` (CDP, forwarded from emulator via ADB)
- The emulator itself (via ADB on localhost:5037)
