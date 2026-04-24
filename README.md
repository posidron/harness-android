# harness-android

Cross-platform Android emulator harness and **mobile browser penetration testing toolkit**. Boots a real Android emulator, controls Chrome via DevTools Protocol, intercepts traffic, injects JS hooks, and generates recon reports — all from a single CLI or Python API. Works on **Windows** and **macOS**.

Under the hood it uses the official **Android Emulator** (QEMU-based) and **ADB**, managed automatically so you never have to touch `sdkmanager` by hand. See [ARCHITECTURE.md](ARCHITECTURE.md) for the full design.

- [harness-android](#harness-android)
  - [Quick-start](#quick-start)
  - [Prerequisites](#prerequisites)
  - [CLI reference](#cli-reference)
    - [Global options](#global-options)
    - [Emulator management](#emulator-management)
      - [`setup`](#setup)
      - [`create` / `delete`](#create--delete)
      - [`start`](#start)
      - [`stop`](#stop)
      - [`status`](#status)
      - [`install-chromium`](#install-chromium)
    - [Device control](#device-control)
      - [`shell`](#shell)
      - [`install`](#install)
      - [`screenshot`](#screenshot)
      - [`push` / `pull`](#push--pull)
      - [`input`](#input)
    - [Browser control](#browser-control)
      - [`browser open`](#browser-open)
      - [`browser cdp`](#browser-cdp)
        - [Running a REPL against a privileged Edge page](#running-a-repl-against-a-privileged-edge-page)
        - [Example REPL session on `edge://version`](#example-repl-session-on-edgeversion)
        - [MojoJS is on by default for debuggable builds](#mojojs-is-on-by-default-for-debuggable-builds)
        - [Gotcha — CDP is single-client per target](#gotcha--cdp-is-single-client-per-target)
    - [Proxy \& traffic interception](#proxy--traffic-interception)
      - [`proxy enable` / `proxy disable`](#proxy-enable--proxy-disable)
      - [`proxy install-ca`](#proxy-install-ca)
      - [`proxy hosts`](#proxy-hosts)
      - [`proxy tcpdump`](#proxy-tcpdump)
    - [Reconnaissance](#reconnaissance)
      - [`recon`](#recon)
    - [JS hooks](#js-hooks)
      - [`hooks`](#hooks)
    - [Pentest automation](#pentest-automation)
      - [`pentest run`](#pentest-run)
    - [Mojo IPC testing](#mojo-ipc-testing)
      - [`mojo enable` (MojoJS bindings)](#mojo-enable-mojojs-bindings)
      - [`mojo trigger`](#mojo-trigger)
      - [`mojo trace`](#mojo-trace)
      - [`mojo fuzz`](#mojo-fuzz)
    - [Chrome flags](#chrome-flags)
    - [File server](#file-server)
      - [Python API](#python-api)
    - [APK forensics](#apk-forensics)
      - [`forensics scan`](#forensics-scan)
      - [`forensics scan-app`](#forensics-scan-app)
      - [`forensics secrets`](#forensics-secrets)
      - [`forensics manifest`](#forensics-manifest)
      - [`forensics app-data`](#forensics-app-data)
      - [`forensics installed`](#forensics-installed)
    - [Intent fuzzing](#intent-fuzzing)
      - [`intent enumerate`](#intent-enumerate)
      - [`intent fuzz`](#intent-fuzz)
    - [Logcat](#logcat)
      - [`logcat stream`](#logcat-stream)
      - [`logcat capture`](#logcat-capture)
    - [UI automation](#ui-automation)
      - [`ui dump`](#ui-dump)
      - [`ui tap`](#ui-tap)
      - [`ui type`](#ui-type)
      - [`ui monkey`](#ui-monkey)
      - [CDP Input (Python API)](#cdp-input-python-api)
    - [WebView enumeration](#webview-enumeration)
      - [`webview list`](#webview-list)
      - [`webview connect`](#webview-connect)
  - [Python API](#python-api-1)
    - [High-level Device API](#high-level-device-api)
    - [Lower-level access](#lower-level-access)
    - [Pentest scripting API](#pentest-scripting-api)
  - [Environment variables](#environment-variables)
  - [Headless / CI usage](#headless--ci-usage)
  - [Physical device usage](#physical-device-usage)
  - [Project structure](#project-structure)
  - [Configuration](#configuration)
  - [Troubleshooting](#troubleshooting)
  - [License](#license)


---

## Quick-start

```bash
# 1 — Install
pip install poetry        # if you don't have it
poetry install

# 2 — Download SDK + system image (one-time, ~5 GB)
poetry run harness-android setup

# 3 — Boot the emulator (creates AVD automatically)
poetry run harness-android start

# 4 — Open a URL in Chrome
poetry run harness-android browser open "https://example.com"

# 5 — Control Chrome via DevTools Protocol
poetry run harness-android browser cdp --navigate "https://example.com" --title

# 6 — Interactive JS REPL on the browser
poetry run harness-android browser cdp --interactive

# 7 — Run a full recon against a target
poetry run harness-android recon --url "https://target.example.com" -o recon.json

# 8 — Scan an installed app for hardcoded secrets (auto-pulls APK)
poetry run harness-android forensics scan-app com.microsoft.emmx.local

# 9 — Run a pentest script
poetry run harness-android pentest run my_test.py --report findings.json
```

---

## Prerequisites

| Requirement | Notes |
|---|---|
| **Python 3.10+** | With `pip` / `poetry` |
| **Hardware acceleration** | Windows: WHPX or Intel HAXM · macOS: Hypervisor.framework (automatic on Apple Silicon & Intel Macs) |

The `setup` command downloads everything else for you: a portable OpenJDK 17, Android SDK cmdline-tools, platform-tools, emulator, system image. No manual Java install needed.

---

## CLI reference

```
harness-android [-s SERIAL] <command> [options]
```

### Global options

| Flag | Description |
|---|---|
| `-s`, `--serial` | Target a specific ADB device serial |

### Emulator management

#### `setup`
Download SDK, accept licences, install platform-tools, emulator, system image.

```bash
harness-android setup              # defaults to API 35 (Android 15), x86_64
harness-android setup --api 34     # use Android 14 instead
harness-android setup --arch arm64 # ARM64 system image (for Apple Silicon Macs)
```

#### `create` / `delete`
Create or delete an AVD (Android Virtual Device).

```bash
harness-android create
harness-android create --name my_phone --api 35 --device pixel_7 --force
harness-android create --name arm_phone --arch arm64   # ARM64 AVD (Apple Silicon Macs)
harness-android delete --name my_phone
```

#### `start`
Boot the emulator. Automatically creates an AVD if none exists.

```bash
harness-android start
harness-android start --headless           # no window (CI)
harness-android start --gpu host --ram 4096
harness-android start --cores 6            # more vCPUs (default 4)
harness-android start --wipe               # fresh data
harness-android start --cold-boot          # skip snapshot, full boot
harness-android start --no-snapshot-save   # don't save snapshot on exit
harness-android start --name arm_phone --arch arm64  # boot ARM64 AVD (Apple Silicon only)
```

#### `stop`
Kill all running emulators.

#### `status`
Show SDK/AVD paths and connected devices.

#### `install-chromium`
Download and install a debuggable Chromium build. Required for CDP on API 35+ where release Chrome ignores debug flags.

```bash
harness-android install-chromium
```

### Device control

#### `shell`
Run shell commands on the device.

```bash
harness-android shell ls /sdcard
harness-android shell pm list packages
harness-android shell dumpsys battery
```

#### `install`
Install an APK.

```bash
harness-android install my_app.apk
harness-android install my_app.apk --sdcard   # install to SD card
```

#### `screenshot`

```bash
harness-android screenshot -o shot.png
```

#### `push` / `pull`
Transfer files.

```bash
harness-android push local.txt /sdcard/local.txt
harness-android pull /sdcard/photo.jpg ./photo.jpg
```

#### `input`
Send touch / keyboard events.

```bash
harness-android input tap 540 960
harness-android input text "hello world"
harness-android input key 4          # KEYCODE_BACK
```

### Browser control

#### `browser open`
Open a URL in Chrome via an Android intent.

```bash
harness-android browser open "https://example.com"
```

#### `browser cdp`
Full Chrome DevTools Protocol control. Enables CDP port forwarding, connects, and lets you:

```bash
# Navigate and print the page title
harness-android browser cdp --navigate "https://example.com" --title

# Run JavaScript
harness-android browser cdp --js "document.querySelectorAll('a').length"

# Save a page-level screenshot
harness-android browser cdp --page-screenshot page.png

# Interactive JS REPL
harness-android browser cdp --interactive

# Attach to an already-running browser without restarting it
harness-android -b edge-local browser cdp --attach --list-pages
harness-android -b edge-local browser cdp --attach --target-url "sapphire" --interactive

# Prepare CDP flags without restart; you launch the browser yourself
harness-android -b edge-local browser cdp --prepare

# Install an on-load JS payload (survives every subsequent navigation)
# --inject accepts a file path OR inline JavaScript
harness-android browser cdp --attach --inject ./my_hooks.js --navigate "https://target"
harness-android browser cdp --attach --inject "window.__marker='X';" --js "window.__marker"
```

The `--interactive` flag opens a JavaScript REPL that evaluates expressions directly in the page context via CDP `Runtime.evaluate`. Everything you type runs as JS inside the target page — including **privileged `chrome://` / `edge://` pages** where `chrome.send`, internal cr APIs, and (when the build has `MojoJS` enabled) Mojo bindings are reachable.

##### Running a REPL against a privileged Edge page

Three ways to reach `edge://version`, `edge://settings`, `edge://new-tab`, etc. Pick the one that matches your current browser state.

**1. Cold launch (no browser currently running with CDP flags)**

Force-stops the browser, starts it with debug flags, navigates, drops into a REPL:

```bash
poetry run harness-android -b edge-local browser cdp \
    --navigate "edge://version" \
    --interactive
```

**2. Attach to an already-running browser (keeps state)**

When the browser is already up with CDP flags — e.g. launched via `cdp_prepare_and_launch` from the MCP server — attach without restart and pick the page by URL substring (or exact id from `--list-pages`):

```bash
# Enumerate first
poetry run harness-android -b edge-local browser cdp --attach --list-pages
#   9  about:blank              about:blank
#   1  https://www.bing.com/    Search - Microsoft Bing
#   5  https://example.com/     Example Domain
#   7  edge://new-tab/          edge://new-tab/
#   8  edge://version/          About version

# REPL pinned to edge://version
poetry run harness-android -b edge-local browser cdp \
    --attach --target-url "edge://version" \
    --interactive

# or by exact target id
poetry run harness-android -b edge-local browser cdp \
    --attach --target-id 8 --interactive
```

**3. One-shot `--js` (scriptable, no REPL)**

Same flags, but prints a single expression result and exits — ideal for CI and shell pipelines:

```bash
poetry run harness-android -b edge-local browser cdp \
    --attach --target-url "edge://version" \
    --js "JSON.stringify({ver: document.querySelector('#version')?.textContent?.trim(), ua: navigator.userAgent})"
```

##### Example REPL session on `edge://version`

```
$ poetry run harness-android -b edge-local browser cdp --attach --target-url "edge://version" --interactive
CDP attached — http://127.0.0.1:9222
CDP connected → edge://version/
REPL (js) — JavaScript expression. Type 'quit' or Ctrl+C to exit.

js> location.href
"edge://version/"

js> document.querySelector('#version').textContent.trim()
"149.0.3991.0 (Developer build) unknown (64-bit)"

js> document.querySelector('#os_type').textContent
"Android 15"

js> navigator.userAgent
"Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Mobile Safari/537.36 EdgA/149.0.3991.0"

# Privileged pages expose chrome.send on many WebUIs
js> typeof chrome?.send
"function"

# Enumerate WebUI handlers cheaply
js> Object.keys(chrome).slice(0, 20)
["send","getVariableValue","timeTicks","csi","loadTimes"]

# MojoJS is enabled by default on every edge-* preset
# (edge, edge-canary, edge-dev, edge-local — built-in BrowserSpec.default_flags
# includes --enable-blink-features=MojoJS,MojoJSTest)
js> typeof Mojo
"function"

js> typeof MojoInterfaceInterceptor
"function"

# Bind any browser-process interface from a privileged origin
js> const h = Mojo.bindInterface('blink.mojom.ClipboardHost', 'context', true); typeof h
"object"

# On settings / new-tab pages, cr.* helpers are usually present
js> typeof cr !== 'undefined' ? Object.keys(cr).slice(0,10) : "no cr.*"
"no cr.*"

js> quit
```

Type `quit`, `exit`, or press Ctrl+C to leave the REPL.

##### MojoJS is on by default for every edge-* preset

All four Edge presets (`edge`, `edge-canary`, `edge-dev`, `edge-local`) ship `--enable-blink-features=MojoJS,MojoJSTest` in `BrowserSpec.default_flags`, so `Mojo.bindInterface(...)`, `MojoInterfaceInterceptor`, and the MojoJSTest helpers are available on **every page** (including privileged `edge://…` origins) without having to pass `--chrome-flags` on every run.

Release Edge / stable Chrome (`-b chrome`) are **not** debuggable and silently ignore the flag — `typeof Mojo` stays `"undefined"` there. For MojoJS you need a debuggable build such as `-b edge-local` or a dev/canary channel with the feature honoured.

If you want to disable MojoJS for a specific run, pick `-b chrome` (release Chrome, no MojoJS) or extend with your own `chrome_flags` that override the feature list.

##### Gotcha — CDP is single-client per target

Chrome's DevTools protocol only allows **one client per page target**. If the MCP server (`harness-android-mcp`) is already attached to a page and you then run `browser cdp --attach --target-url …`, `Page.enable` will hang and time out with:

```
CDP Page.enable (id=1) timed out after 30s
```

To recover, either:

- Call the MCP tool `cdp_disconnect` to release the page session, then retry the CLI, **or**
- Use the cold-launch path (`--navigate` without `--attach`) which force-stops the browser and starts fresh, **or**
- Target a different page — each page is independent, so a second client can safely attach to a target the MCP is *not* using.

Browser-level inspection (`--list-pages`) is multi-client safe; only per-page `connect()` is exclusive.

### Proxy & traffic interception

#### `proxy enable` / `proxy disable`
Route emulator traffic through an intercepting proxy on the host.

```bash
harness-android proxy enable                        # default: 10.0.2.2:8080
harness-android proxy enable --host 10.0.2.2 --port 8080
harness-android proxy disable
harness-android proxy status
```

#### `proxy install-ca`
Install a CA certificate for TLS interception.

```bash
harness-android proxy install-ca --mitmproxy        # auto-find mitmproxy CA
harness-android proxy install-ca --cert /path/to/burp-ca.pem
```

#### `proxy hosts`
Manipulate `/etc/hosts` on the emulator for DNS spoofing.

```bash
harness-android proxy hosts --add "10.0.2.2=api.target.local"
harness-android proxy hosts                         # show current
harness-android proxy hosts --reset
```

#### `proxy tcpdump`
Capture raw packet traffic on the device.

```bash
harness-android proxy tcpdump                       # start capture
harness-android proxy tcpdump --stop -o traffic.pcap  # stop and pull pcap
```

### Reconnaissance

#### `recon`
Automated reconnaissance against the current or specified page.

```bash
# Full recon: fingerprint + spider + storage + CSP analysis
harness-android recon --url "https://target.example.com" -o recon.json

# Individual modules
harness-android recon --url "https://target.example.com" --fingerprint
harness-android recon --url "https://target.example.com" --spider
harness-android recon --url "https://target.example.com" --storage
harness-android recon --url "https://target.example.com" --csp
```

**Fingerprint** detects: React, Angular, Vue, jQuery, Next.js, Nuxt, Svelte, Bootstrap, Tailwind, meta generator tags.

**Spider** extracts: all links (href + text), forms (action, method, field names/types), script sources, iframe sources.

**Storage** dumps: cookies (name, value, domain, secure, httpOnly), localStorage, sessionStorage.

**CSP analysis** parses Content-Security-Policy and flags: `unsafe-inline`, `unsafe-eval`, wildcard `*`, `data:` in script-src, missing default-src/script-src.

### JS hooks

#### `hooks`
Inject JavaScript hooks that run before page scripts to capture browser API calls.

```bash
# Install all hooks and capture for 30 seconds
harness-android hooks --url "https://target.example.com" --wait 30 -o captured.json

# Specific hooks only
harness-android hooks --hooks fetch,xhr,forms --url "https://target.example.com"
```

| Hook | What it captures |
|---|---|
| `xhr` | `XMLHttpRequest.open()` / `.send()` — method, URL, body |
| `fetch` | `window.fetch()` — URL, method, body |
| `cookies` | `document.cookie` setter — every cookie write |
| `websocket` | `new WebSocket()` and `.send()` — URL, data |
| `postmessage` | `window.postMessage` events — origin, data |
| `console` | `console.log/warn/error/info/debug` — level, args |
| `storage` | `localStorage.setItem` / `sessionStorage.setItem` — key, value |
| `forms` | `<form>` submit events — action, method, all field values |
| `all` | All of the above |

### Pentest automation

#### `pentest run`
Execute a Python pentest script with a rich context object.

```bash
harness-android pentest run my_test.py --report findings.json
```

The script must define a `run(ctx)` function. The `ctx` (PentestContext) provides:

| Method | Description |
|---|---|
| `ctx.navigate(url)` | Navigate Chrome to URL |
| `ctx.click(selector)` | Click a DOM element |
| `ctx.type_in(selector, text)` | Type into an input |
| `ctx.js(expression)` | Evaluate JavaScript |
| `ctx.wait(seconds)` | Sleep |
| `ctx.wait_for(selector)` | Poll until element exists |
| `ctx.screenshot(path)` | CDP page screenshot |
| `ctx.hooks.install(...)` | Install JS hooks |
| `ctx.hooks.collect()` | Retrieve captured hook data |
| `ctx.interceptor` | CDP Fetch request interceptor |
| `ctx.proxy` | Proxy/CA/hosts/tcpdump control |
| `ctx.recon()` | Full recon report |
| `ctx.fingerprint()` | Tech fingerprint only |
| `ctx.spider()` | Link/form spider only |
| `ctx.storage()` | Cookie/storage extraction |
| `ctx.csp()` | CSP analysis |
| `ctx.add_finding(...)` | Record a vulnerability finding |
| `ctx.report(path=...)` | Generate JSON report |

Example script:

```python
# my_test.py
def run(ctx):
    ctx.navigate("https://target.example.com/login")
    ctx.hooks.install("fetch", "forms", "cookies")

    ctx.type_in("#username", "admin")
    ctx.type_in("#password", "test123")
    ctx.click("button[type=submit]")
    ctx.wait(3)

    data = ctx.hooks.collect()
    print(f"Captured {len(data.get('forms', []))} form submissions")

    csp = ctx.csp()
    for issue in csp.get("issues", []):
        ctx.add_finding(title=issue, severity="medium")

    ctx.screenshot("evidence.png")
    ctx.report(path="login_test_report.json")
```

### Mojo IPC testing

Chromium's Mojo IPC is the communication layer between the sandboxed renderer and privileged browser processes — a critical attack surface. harness-android supports two approaches:

1. **Passive tracing** — trace, trigger, and fuzz Mojo interfaces from outside via CDP Tracing
2. **MojoJS bindings** — enable `Mojo.bindInterface()` in JS so you can call any Mojo interface directly

#### `mojo enable` (MojoJS bindings)

Restart Chrome with `--enable-blink-features=MojoJS,MojoJSTest` and optionally serve your Chromium `gen/` folder so the emulator can load the mojom JS bindings:

```bash
# Enable MojoJS + serve gen/ folder from a local Chromium build
harness-android mojo enable --gen-dir /path/to/chromium/out/Release/gen --interactive

# Enable and navigate directly to your test page
harness-android mojo enable --gen-dir ./gen --navigate "http://10.0.2.2:8089/test.html"

# Just enable MojoJS (no gen/ serving)
harness-android mojo enable --interactive
```

A ready-to-use test page is included at `examples/mojo_bindings_test.html` — it checks Mojo availability, creates message pipes, binds to `blink.mojom.ClipboardHost`, and runs a basic raw-message fuzz loop. Copy it into your Chromium output directory and navigate to it:

```bash
harness-android mojo enable \
  --gen-dir /path/to/chromium/out/Release \
  --navigate "http://10.0.2.2:8089/examples/mojo_bindings_test.html"

# Monitor for crashes in another terminal:
harness-android logcat capture --duration 60 -o mojo_fuzz.log
```

For passive recon (mapping which interfaces are reachable without MojoJS), use the `examples/mojo_recon.py` pentest plugin:

```bash
harness-android pentest run examples/mojo_recon.py
```

The `gen/` folder is served over HTTP from the host. Chrome on the emulator loads it from `http://10.0.2.2:8089/`. Your test HTML can then:

```html
<script type="module">
  // Import the generated mojom bindings from the served gen/ folder
  import {ClipboardHost, ClipboardHostRemote}
    from '/third_party/blink/public/mojom/clipboard/clipboard.mojom-webui.js';

  // Bind to the interface
  const clipboard = ClipboardHostRemote.getNewPipeAndPassReceiver();
  // ... call methods on the interface
</script>
```

> **Requirements:** MojoJS bindings require a **debuggable** Chrome/Chromium build. Release Chrome won't have these bindings available. Use `harness-android install-chromium` to install a debuggable Chromium build.

#### `mojo trigger`
Exercise all 23 Mojo-backed Web APIs and see which interfaces are reachable:

```bash
harness-android mojo trigger --url "https://target.example.com"
```

This calls APIs like Permissions, Clipboard, Geolocation, MediaDevices, WebUSB, WebBluetooth, StorageManager, IndexedDB, ServiceWorker, WakeLock, etc. — each one exercises a different `*.mojom.*` interface.

#### `mojo trace`
Capture a Chrome trace with Mojo IPC categories while triggering APIs:

```bash
# Trace + trigger all APIs
harness-android mojo trace --url "https://target.example.com" --trigger -o mojo.json

# Passive trace for 30 seconds (capture during manual interaction)
harness-android mojo trace --duration 30 --verbose -o mojo.json

# Save raw trace for chrome://tracing visualizer
harness-android mojo trace --trigger --chrome-trace trace.json
```

#### `mojo fuzz`
Fuzz a specific Mojo-backed Web API with boundary inputs:

```bash
harness-android mojo fuzz Clipboard.writeText --url "https://example.com"
harness-android mojo fuzz StorageManager.estimate
harness-android mojo fuzz Permissions.query -o fuzz_results.json
```

Built-in fuzz payloads include: empty strings, megabyte-length strings, null, undefined, NaN, typed arrays, blobs, lone surrogates, null bytes, and more.

### Chrome flags

Pass arbitrary command-line flags to Chrome via `browser cdp --chrome-flags`:

```bash
# Single flag
harness-android browser cdp --chrome-flags="--disable-web-security" --interactive

# Multiple flags in one string
harness-android browser cdp --chrome-flags="--enable-blink-features=MojoJS --disable-site-isolation-trials --v=1" --interactive
```

Flags can also be set in `harness.toml` (applied to every browser launch):

```toml
extra_chrome_flags = ["--enable-logging", "--v=1"]
```

### File server

Serve any local directory to the emulator over HTTP (the emulator sees the host as `10.0.2.2`):

```bash
harness-android serve ./my_test_pages --port 8089
# Chrome on emulator → http://10.0.2.2:8089/index.html
```

#### Python API

```python
from harness_android.mojo import MojoTracer

tracer = MojoTracer(browser, verbose=True)
tracer.start_trace()
results = tracer.trigger_all_apis()       # exercises 23 Mojo interfaces
events = tracer.stop_trace()              # raw Chrome trace events

messages = tracer.extract_mojo_messages(events)
tracer.print_summary(messages)

# Fuzz a specific API
fuzz_results = tracer.fuzz_api(
    "Clipboard.writeText",
    "navigator.clipboard.writeText({FUZZ}).catch(e => e.message)",
    MojoTracer.FUZZ_STRINGS,
    "blink.mojom.ClipboardHost",
)

# Save for offline analysis / chrome://tracing
tracer.dump("analysis.json", events, messages, results)
tracer.dump_chrome_trace("trace.json")

# Serve files to the emulator
from harness_android.fileserver import FileServer
with FileServer("/path/to/gen", port=8089) as server:
    browser.navigate(server.emulator_url + "/test.html")
    # ...
```

### APK forensics

Scan APK files and installed app data for hardcoded secrets, manifest security issues, and sensitive database contents.

#### `forensics scan`
Full APK scan — secrets + manifest analysis:

```bash
harness-android forensics scan app.apk -o report.json
```

Scans 27 secret patterns: AWS keys, Google API keys, GitHub/Slack/Stripe tokens, JWTs, PEM private keys, Azure connection strings, generic passwords, hardcoded URLs with credentials, and more.

#### `forensics scan-app`
**One command to scan any installed app** — auto-pulls the APK from the device by package name and runs a full scan:

```bash
harness-android forensics scan-app com.microsoft.emmx.local
harness-android forensics scan-app com.example.app --app-data -o report.json
```

Add `--app-data` to also scan the app's private data (shared prefs, SQLite DBs, internal files).

#### `forensics secrets`
Secret scanning only:

```bash
harness-android forensics secrets app.apk
```

#### `forensics manifest`
AndroidManifest.xml security audit — flags `debuggable`, `allowBackup`, `usesCleartextTraffic`, exported components without permissions, dangerous permissions, custom URI schemes:

```bash
harness-android forensics manifest app.apk
```

#### `forensics app-data`
Extract an installed app's private data from the emulator (runs `adb root`) and scan everything — shared preferences, SQLite databases, internal files:

```bash
harness-android forensics app-data com.example.app --report findings.json
```

Automatically detects sensitive database tables (cookies, logins, passwords, tokens, sessions) and scans all text columns for secrets.

#### `forensics installed`
Pull and scan every 3rd-party APK installed on the device:

```bash
harness-android forensics installed -o all_apps_report.json
```

### Intent fuzzing

#### `intent enumerate`
List all exported components (activities, services, receivers) from an installed app:

```bash
harness-android intent enumerate com.example.app
```

#### `intent fuzz`
Fuzz exported components with type-appropriate payloads (strings, URIs, numbers, booleans). Monitors logcat for crashes after each payload.

```bash
harness-android intent fuzz com.example.app
harness-android intent fuzz com.example.app --component .DeepLinkActivity
```

### Logcat

#### `logcat stream`
Stream logcat output in real time (clears the buffer first):

```bash
harness-android logcat stream
harness-android logcat stream --tag "chromium"
```

#### `logcat capture`
Capture logcat for a fixed duration, then scan for crashes (FATAL EXCEPTION, SIGSEGV, ANR, ASan/AddressSanitizer, tombstones):

```bash
harness-android logcat capture --duration 30 -o logcat.txt
```

### UI automation

Three approaches for controlling the UI, each with different trade-offs:

| Method | Works on | How |
|---|---|---|
| **UIAutomator** (`ui dump/tap/type`) | Any app, any screen | Parses the view hierarchy XML — finds elements by text, resource-id, class |
| **CDP Input** (Python API) | Browser / WebView only | `Input.dispatchTouchEvent` / `Input.dispatchKeyEvent` via CDP — realistic browser-level events |
| **Monkey** (`ui monkey`) | Any app | Random events (taps, swipes, rotations) for stress testing / crash discovery |

#### `ui dump`
Dump the full screen UI hierarchy (works for any app, not just browsers):

```bash
harness-android ui dump                    # tree view
harness-android ui dump --clickable        # table of clickable elements with coordinates
harness-android ui dump --depth 5          # limit tree depth
```

#### `ui tap`
Automatically find an element and tap its centre — no need to know coordinates:

```bash
harness-android ui tap --text "Sign in"
harness-android ui tap --resource-id "com.example:id/login_button"
```

#### `ui type`
Tap a text field by resource-id and type text into it:

```bash
harness-android ui type "com.example:id/username" "admin@example.com"
```

#### `ui monkey`
Run the Android monkey random event generator for stress testing:

```bash
harness-android ui monkey -p com.microsoft.emmx.local -n 10000
harness-android ui monkey -p com.example.app --seed 42 -o monkey.log
harness-android ui monkey --ignore-crashes --ignore-timeouts -n 50000
```

#### CDP Input (Python API)

For browser-level touch and keyboard events (more realistic than JS `.click()`):

```python
browser.dispatch_touch(200, 400)                    # tap at (200, 400)
browser.dispatch_swipe(200, 800, 200, 200)           # swipe up
browser.dispatch_key("Enter")                        # press Enter
browser.dispatch_key("a", modifiers=2)               # Ctrl+A
```

### WebView enumeration

Android apps that embed a `WebView` with debugging enabled expose a Unix socket (`webview_devtools_remote_<PID>`) that speaks the Chrome DevTools Protocol. If a WebView appears in `webview list`, it **already has CDP enabled** — the socket *is* the CDP endpoint.

| Scenario | Visible? | CDP works? |
|---|---|---|
| App calls `WebView.setWebContentsDebuggingEnabled(true)` | Yes | Yes — connect and use immediately |
| APK has `android:debuggable="true"` in manifest | Yes | Yes — same as above |
| App does not enable WebView debugging | No | No — no socket exists, invisible to harness |
| `chrome_devtools_remote` (Chrome's own socket) | Yes | Needs `enable_cdp()` first — harness handles this automatically |

> **Note:** `chrome_devtools_remote` is special. Chrome always exposes this socket, but without the debug command-line flags (written by `enable_cdp()`), it has no active inspectable page. When you `webview connect chrome_devtools_remote`, the harness automatically restarts Chrome with debug flags so CDP works. For all other WebView sockets, the connection is direct — no restart needed.

#### `webview list`
Enumerate all debuggable WebView sockets on the device:

```bash
harness-android webview list
```

Example output:
```
Found 2 debuggable WebView socket(s)
┃ Socket                       ┃ PID  ┃ Package                                        ┃ Pages    ┃
│ webview_devtools_remote_1702 │ 1702 │ com.google.android.googlequicksearchbox:search │ no pages │
│ chrome_devtools_remote       │ 0    │ com.microsoft.emmx.local                       │ no pages │
```

#### `webview connect`
Connect to a WebView and control it via CDP — navigate, run JS, take screenshots, or drop into an interactive REPL:

```bash
# Connect and navigate
harness-android webview connect chrome_devtools_remote --navigate "https://cnn.com"

# Run JavaScript inside a third-party WebView
harness-android webview connect webview_devtools_remote_1702 --js "document.title"

# Interactive JS REPL
harness-android webview connect chrome_devtools_remote --interactive

# Take a screenshot of what the WebView is rendering
harness-android webview connect webview_devtools_remote_1702 --page-screenshot shot.png

# Install an on-load JS hook into a mini-app / third-party WebView
harness-android webview connect webview_devtools_remote_1702 --inject ./hooks.js
```

---

## Python API

### High-level Device API

```python
from harness_android.device import Device

with Device(headless=True) as dev:
    dev.open_url("https://example.com")
    dev.screenshot("home.png")

    dev.browser.enable_cdp()
    dev.browser.connect()
    dev.browser.navigate("https://example.com")
    title = dev.browser.get_page_title()
```

### Lower-level access

```python
from harness_android.adb import ADB
from harness_android.browser import Browser

adb = ADB(serial="emulator-5554")
browser = Browser(adb)
browser.enable_cdp()
browser.connect()
html = browser.get_page_html()
browser.close()
```

### Pentest scripting API

```python
from harness_android.adb import ADB
from harness_android.browser import Browser
from harness_android.hooks import Hooks
from harness_android.intercept import Interceptor
from harness_android.proxy import Proxy
from harness_android.recon import full_recon, extract_storage

adb = ADB(serial="emulator-5554")
browser = Browser(adb)
browser.enable_cdp()
browser.connect()

# --- Proxy setup ---
proxy = Proxy(adb)
proxy.enable()                        # route through mitmproxy
proxy.install_mitmproxy_ca()          # install CA cert
proxy.add_hosts_entry("10.0.2.2", "api.target.local")

# --- JS hooks ---
hooks = Hooks(browser)
hooks.install("fetch", "xhr", "cookies", "forms")
browser.navigate("https://target.example.com")
data = hooks.collect()                # {fetch: [...], xhr: [...], ...}
hooks.dump("captured_api_calls.json")

# --- Request interception ---
interceptor = Interceptor(browser)

@interceptor.on_request("*api/login*")
def log_login(req):
    print(f"Login: {req.method} {req.url}")
    print(f"  Body: {req.post_data}")

@interceptor.on_response("*.js")
def patch_js(req):
    body = req.response_body.decode()
    return {"body": body.replace("isAdmin=false", "isAdmin=true")}

interceptor.start(background=True)

# --- Recon ---
full_recon(browser, output="recon_report.json")

# --- Security ---
browser.override_certificate_errors(allow=True)
browser.disable_cache()
browser.emulate_device(user_agent="Custom-Agent/1.0")

# Cleanup
interceptor.stop()
proxy.disable()
browser.close()
```

---

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `ANDROID_HARNESS_HOME` | `~/.harness-android` | Root for harness data |
| `ANDROID_HOME` / `ANDROID_SDK_ROOT` | (from harness home) | Use an existing SDK |

---

## Headless / CI usage

```bash
harness-android setup --api 35
harness-android start --headless --gpu swiftshader_indirect
harness-android recon --url "https://target.example.com" -o recon.json
harness-android stop
```

Use `--gpu swiftshader_indirect` for software rendering in environments without GPU access.

---

## Physical device usage

All commands work on real Android devices over USB — not just the emulator. Enable **USB debugging** in Developer Settings, connect via USB, then use `-s` to target the device:

```bash
# List connected devices
adb devices

# Target a specific device by serial
harness-android -s SERIAL install app.apk
harness-android -s SERIAL -b edge browser cdp --interactive
harness-android -s SERIAL recon --url "https://target.example.com" -o recon.json
harness-android -s SERIAL forensics scan-app com.example.app
harness-android -s SERIAL screenshot -o phone.png
```

If only one device is connected, `-s` is optional — ADB auto-selects it.

---

## Project structure

```
harness-android/
├── pyproject.toml              # Poetry config, dependencies, entry point
├── README.md                   # This file
├── ARCHITECTURE.md             # Detailed design & data-flow docs
├── harness.toml.example        # Example config file (copy as harness.toml)
├── .gitignore
├── examples/
│   ├── recon_pentest.py         # Recon plugin (fingerprint, spider, headers, CSP)
│   ├── mojo_recon.py            # Mojo IPC recon (trace APIs, map attack surface)
│   └── mojo_bindings_test.html  # MojoJS bindings test page (serve from gen/)
└── harness_android/
    ├── __init__.py
    ├── config.py               # Paths, platform detection, config loading
    ├── sdk.py                  # JDK + SDK bootstrap & package management
    ├── adb.py                  # ADB wrapper (shell, install, input, files)
    ├── emulator.py             # AVD creation & emulator lifecycle + snapshots
    ├── browser.py              # Chrome/Chromium CDP control + Input domain
    ├── device.py               # High-level facade (Device context manager)
    ├── proxy.py                # HTTP proxy, CA certs, tcpdump, DNS
    ├── hooks.py                # JS API hooks (fetch, XHR, cookies, forms …)
    ├── intercept.py            # CDP Fetch request/response interception
    ├── recon.py                # Fingerprint, spider, storage, CSP analysis
    ├── pentest.py              # PentestContext + script runner
    ├── mojo.py                 # Mojo IPC tracing, Web API triggers, fuzzing
    ├── forensics.py            # APK secret scanning, manifest audit, app data
    ├── logcat.py               # Logcat capture + crash detection (ASan, SIGSEGV)
    ├── intents.py              # Intent fuzzing (exported components, deep links)
    ├── webview.py              # WebView enumeration + CDP connection
    ├── ui.py                   # UIAutomator dump, smart tap, monkey testing
    ├── fileserver.py           # HTTP file server (serve gen/ to emulator)
    └── cli.py                  # argparse CLI (all commands)
```

---

## Configuration

Settings are loaded from (in priority order):

1. **CLI flags** — always win
2. **`./harness.toml`** — project-local config
3. **`~/.android-harness/config.toml`** — user-global config
4. **Built-in defaults**

Legacy `harness.json` / `config.json` files are still read for one
release but emit a deprecation warning — convert them to TOML.

Example `harness.toml`:

```toml
# Browser preset used when -b is not passed on the CLI.
default_browser    = "edge-local"
# Appended to every browser launch (preset default_flags still apply).
extra_chrome_flags = []

[emulator]
ram       = 4096
cores     = 4              # 0 = auto: min(4, host_cpu/2)
gpu       = "auto"         # auto | host | swiftshader_indirect | off
api_level = 35
headless  = false
```

> Each browser preset (package, activity, cmdline files, default
> flags) is defined in `harness_android/browser.py`. To override any
> field for a preset, add a `[browsers.<name>]` table to
> `harness.toml` — only list fields you want to change, the rest are
> inherited:
>
> ```toml
> [browsers.edge-local]
> default_flags = ["--enable-blink-features=MojoJS,MojoJSTest", "--v=1"]
> ```

---

## Troubleshooting

| Problem | Solution |
|---|---|
| `sdkmanager` not found | Run `harness-android setup` |
| Emulator won't start | Ensure hardware acceleration is available (WHPX / HAXM / Hypervisor.framework) |
| Emulator frozen / ADB offline | Kill emulator, restart with `harness-android start --cold-boot --ram 4096` |
| `No inspectable page found` | Chrome may still be starting — `enable_cdp()` retries for 20s automatically |
| CDP WebSocket 403 | Fixed automatically — harness writes `--remote-allow-origins=*` flag |
| CDP not working on API 35 | Release Chrome ignores debug flags; run `harness-android install-chromium` |
| Proxy not intercepting HTTPS | Run `proxy install-ca --mitmproxy` or `--cert` to install the CA cert |
| `tcpdump` not found | Some images lack it; use `proxy enable` with mitmproxy instead |
| Slow emulator startup | Use snapshots (default) — first boot is slow, subsequent boots load from snapshot |
| Slow on CI | Use `--headless --gpu swiftshader_indirect --ram 2048` |
| Java not found | Usually auto-installed by `setup`. Set `JAVA_HOME` if you prefer your own JDK |

---

## License

MIT
