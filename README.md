# android-harness

Cross-platform Android emulator harness and **mobile browser penetration testing toolkit**. Boots a real Android emulator, controls Chrome via DevTools Protocol, intercepts traffic, injects JS hooks, and generates recon reports — all from a single CLI or Python API. Works on **Windows** and **macOS**.

Under the hood it uses the official **Android Emulator** (QEMU-based) and **ADB**, managed automatically so you never have to touch `sdkmanager` by hand. See [ARCHITECTURE.md](ARCHITECTURE.md) for the full design.

---

## Quick-start

```bash
# 1 — Install
pip install poetry        # if you don't have it
poetry install

# 2 — Download SDK + system image (one-time, ~5 GB)
poetry run android-harness setup

# 3 — Boot the emulator (creates AVD automatically)
poetry run android-harness start

# 4 — Open a URL in Chrome
poetry run android-harness browser open "https://example.com"

# 5 — Control Chrome via DevTools Protocol
poetry run android-harness browser cdp --navigate "https://example.com" --title

# 6 — Interactive JS REPL on the browser
poetry run android-harness browser cdp --interactive

# 7 — Run a full recon against a target
poetry run android-harness recon --url "https://target.example.com" -o recon.json

# 8 — Run a pentest script
poetry run android-harness pentest run my_test.py --report findings.json
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
android-harness [-s SERIAL] <command> [options]
```

### Global options

| Flag | Description |
|---|---|
| `-s`, `--serial` | Target a specific ADB device serial |

### Emulator management

#### `setup`
Download SDK, accept licences, install platform-tools, emulator, system image.

```bash
android-harness setup              # defaults to API 35 (Android 15)
android-harness setup --api 34     # use Android 14 instead
```

#### `create` / `delete`
Create or delete an AVD (Android Virtual Device).

```bash
android-harness create
android-harness create --name my_phone --api 35 --device pixel_7 --force
android-harness delete --name my_phone
```

#### `start`
Boot the emulator. Automatically creates an AVD if none exists.

```bash
android-harness start
android-harness start --headless           # no window (CI)
android-harness start --gpu host --ram 4096
android-harness start --wipe               # fresh data
```

#### `stop`
Kill all running emulators.

#### `status`
Show SDK/AVD paths and connected devices.

### Device control

#### `shell`
Run shell commands on the device.

```bash
android-harness shell ls /sdcard
android-harness shell pm list packages
android-harness shell dumpsys battery
```

#### `install`
Install an APK.

```bash
android-harness install my_app.apk
```

#### `screenshot`

```bash
android-harness screenshot -o shot.png
```

#### `push` / `pull`
Transfer files.

```bash
android-harness push local.txt /sdcard/local.txt
android-harness pull /sdcard/photo.jpg ./photo.jpg
```

#### `input`
Send touch / keyboard events.

```bash
android-harness input tap 540 960
android-harness input text "hello world"
android-harness input key 4          # KEYCODE_BACK
```

### Browser control

#### `browser open`
Open a URL in Chrome via an Android intent.

```bash
android-harness browser open "https://example.com"
```

#### `browser cdp`
Full Chrome DevTools Protocol control. Enables CDP port forwarding, connects, and lets you:

```bash
# Navigate and print the page title
android-harness browser cdp --navigate "https://example.com" --title

# Run JavaScript
android-harness browser cdp --js "document.querySelectorAll('a').length"

# Save a page-level screenshot
android-harness browser cdp --page-screenshot page.png

# Interactive JS REPL
android-harness browser cdp --interactive
```

### Proxy & traffic interception

#### `proxy enable` / `proxy disable`
Route emulator traffic through an intercepting proxy on the host.

```bash
android-harness proxy enable                        # default: 10.0.2.2:8080
android-harness proxy enable --host 10.0.2.2 --port 8080
android-harness proxy disable
android-harness proxy status
```

#### `proxy install-ca`
Install a CA certificate for TLS interception.

```bash
android-harness proxy install-ca --mitmproxy        # auto-find mitmproxy CA
android-harness proxy install-ca --cert /path/to/burp-ca.pem
```

#### `proxy hosts`
Manipulate `/etc/hosts` on the emulator for DNS spoofing.

```bash
android-harness proxy hosts --add "10.0.2.2=api.target.local"
android-harness proxy hosts                         # show current
android-harness proxy hosts --reset
```

#### `proxy tcpdump`
Capture raw packet traffic on the device.

```bash
android-harness proxy tcpdump                       # start capture
android-harness proxy tcpdump --stop -o traffic.pcap  # stop and pull pcap
```

### Reconnaissance

#### `recon`
Automated reconnaissance against the current or specified page.

```bash
# Full recon: fingerprint + spider + storage + CSP analysis
android-harness recon --url "https://target.example.com" -o recon.json

# Individual modules
android-harness recon --url "https://target.example.com" --fingerprint
android-harness recon --url "https://target.example.com" --spider
android-harness recon --url "https://target.example.com" --storage
android-harness recon --url "https://target.example.com" --csp
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
android-harness hooks --url "https://target.example.com" --wait 30 -o captured.json

# Specific hooks only
android-harness hooks --hooks fetch,xhr,forms --url "https://target.example.com"
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
android-harness pentest run my_test.py --report findings.json
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

Chromium's Mojo IPC is the communication layer between the sandboxed renderer and privileged browser processes — a critical attack surface. android-harness can trace, trigger, and fuzz Mojo interfaces from outside the browser.

#### `mojo trigger`
Exercise all 23 Mojo-backed Web APIs and see which interfaces are reachable:

```bash
android-harness mojo trigger --url "https://target.example.com"
```

This calls APIs like Permissions, Clipboard, Geolocation, MediaDevices, WebUSB, WebBluetooth, StorageManager, IndexedDB, ServiceWorker, WakeLock, etc. — each one exercises a different `*.mojom.*` interface.

#### `mojo trace`
Capture a Chrome trace with Mojo IPC categories while triggering APIs:

```bash
# Trace + trigger all APIs
android-harness mojo trace --url "https://target.example.com" --trigger -o mojo.json

# Passive trace for 30 seconds (capture during manual interaction)
android-harness mojo trace --duration 30 --verbose -o mojo.json

# Save raw trace for chrome://tracing visualizer
android-harness mojo trace --trigger --chrome-trace trace.json
```

#### `mojo fuzz`
Fuzz a specific Mojo-backed Web API with boundary inputs:

```bash
android-harness mojo fuzz Clipboard.writeText --url "https://example.com"
android-harness mojo fuzz StorageManager.estimate
android-harness mojo fuzz Permissions.query -o fuzz_results.json
```

Built-in fuzz payloads include: empty strings, megabyte-length strings, null, undefined, NaN, typed arrays, blobs, lone surrogates, null bytes, and more.

#### Python API

```python
from android_harness.mojo import MojoTracer

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
```

---

## Python API

### High-level Device API

```python
from android_harness.device import Device

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
from android_harness.adb import ADB
from android_harness.browser import Browser

adb = ADB(serial="emulator-5554")
browser = Browser(adb)
browser.enable_cdp()
browser.connect()
html = browser.get_page_html()
browser.close()
```

### Pentest scripting API

```python
from android_harness.adb import ADB
from android_harness.browser import Browser
from android_harness.hooks import Hooks
from android_harness.intercept import Interceptor
from android_harness.proxy import Proxy
from android_harness.recon import full_recon, extract_storage

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
| `ANDROID_HARNESS_HOME` | `~/.android-harness` | Root for harness data |
| `ANDROID_HOME` / `ANDROID_SDK_ROOT` | (from harness home) | Use an existing SDK |

---

## Headless / CI usage

```bash
android-harness setup --api 35
android-harness start --headless --gpu swiftshader_indirect
android-harness recon --url "https://target.example.com" -o recon.json
android-harness stop
```

Use `--gpu swiftshader_indirect` for software rendering in environments without GPU access.

---

## Project structure

```
android-harness/
├── pyproject.toml              # Poetry config, dependencies, entry point
├── README.md                   # This file
├── ARCHITECTURE.md             # Detailed design & data-flow docs
├── .gitignore
├── examples/
│   └── example_pentest.py      # Sample pentest script
└── android_harness/
    ├── __init__.py
    ├── config.py               # Paths, platform detection, constants
    ├── sdk.py                  # JDK + SDK bootstrap & package management
    ├── adb.py                  # ADB wrapper (shell, install, input, files)
    ├── emulator.py             # AVD creation & emulator lifecycle
    ├── browser.py              # Chrome CDP control + security helpers
    ├── device.py               # High-level facade (Device context manager)
    ├── proxy.py                # HTTP proxy, CA certs, tcpdump, DNS
    ├── hooks.py                # JS API hooks (fetch, XHR, cookies, forms …)
    ├── intercept.py            # CDP Fetch request/response interception
    ├── recon.py                # Fingerprint, spider, storage, CSP analysis
    ├── pentest.py              # PentestContext + script runner
    ├── mojo.py                 # Mojo IPC tracing, Web API triggers, fuzzing
    └── cli.py                  # argparse CLI (all commands)
```

---

## Troubleshooting

| Problem | Solution |
|---|---|
| `sdkmanager` not found | Run `android-harness setup` |
| Emulator won't start | Ensure hardware acceleration is available (WHPX / HAXM / Hypervisor.framework) |
| `No inspectable page found` | Chrome may still be starting — `enable_cdp()` retries for 20s automatically |
| CDP WebSocket 403 | Fixed automatically — harness writes `--remote-allow-origins=*` flag |
| Proxy not intercepting HTTPS | Run `proxy install-ca --mitmproxy` or `--cert` to install the CA cert |
| `tcpdump` not found | Some images lack it; use `proxy enable` with mitmproxy instead |
| Slow on CI | Use `--headless --gpu swiftshader_indirect --ram 2048` |
| Java not found | Usually auto-installed by `setup`. Set `JAVA_HOME` if you prefer your own JDK |

---

## License

MIT
