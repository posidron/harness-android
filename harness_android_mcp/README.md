# harness-android-mcp

**Model Context Protocol server for [harness-android](../README.md)** — exposes every feature of the harness as structured JSON-RPC tools so an AI agent (Claude Code, Copilot Chat, Cursor, …) can drive the Android emulator, MITM proxy, Chromium/Edge CDP, Mojo IPC, WebView enumeration, APK forensics, intent fuzzing, UIAutomator, and more **without shelling out**.

Why an MCP instead of just `poetry run harness-android …`?

- **No PowerShell / bash quoting hell.** JSON-RPC arguments are typed and safe.
- **No subprocess / pyenv shim timeouts.** The server lives for the whole session.
- **Persistent CDP session.** `cdp_navigate` → `cdp_eval` → `cdp_inject_on_load` all re-use the same WebSocket. Sub-millisecond round-trip instead of multi-second `poetry run` spin-up.
- **Structured returns.** Dataclasses (`PageFingerprint`, `SpiderResult`, `ForensicFinding`, `CrashEvent`, `ExportedComponent`, `MojoMessage`, …) become JSON dicts automatically — ideal for agent reasoning.
- **Safety rails** encode the failure modes we learned the hard way (e.g. the input helpers refuse swipes long enough to crash Edge's renderer on x86_64 emulators).

---

## Install & run

```powershell
# one-time
poetry install

# run the server (stdio transport)
poetry run harness-android-mcp
```

The server speaks JSON-RPC over stdin/stdout and blocks waiting for a client — that is the correct behaviour. Register it in a client instead of running it manually.

### VS Code

The repo ships [`.vscode/mcp.json`](../.vscode/mcp.json) — VS Code picks it up automatically. Use:

- **Command Palette → `MCP: List Servers` → `harness-android` → Start Server**
- **Command Palette → `MCP: List Servers` → `harness-android` → Show Output** (to see stdio logs)
- **Copilot Chat → 🛠 tools icon** → toggle individual tools on/off

After you edit the server code, **Restart** the server from that same menu for changes to take effect.

### Claude Desktop / other clients

```json
{
  "mcpServers": {
    "harness-android": {
      "command": "poetry",
      "args": ["run", "harness-android-mcp"],
      "cwd": "C:/path/to/android-harness"
    }
  }
}
```

### Quick sanity check (no client needed)

```powershell
npx @modelcontextprotocol/inspector poetry run harness-android-mcp
```

Opens a web UI listing every tool with its input schema — click, fill, run.

---

## Design

- **Stateful.** A single `Browser` session, `Hooks` attachment, `LogcatCapture`, `MojoTracer`, and `FileServer` instance live on the server. Calling `cdp_prepare_and_launch` once and then many `cdp_eval`s is the fast path.
- **One tool per verb.** Each tool is a thin wrapper over a single `harness_android.*` Python function — no composite "do-everything" tools. Agents compose them.
- **Every return value is JSON-serializable.** Dataclasses are converted with `dataclasses.asdict`. `RemoteObject` previews (DOM nodes, `window`, bridge objects) come back as `__cdp_*`-keyed dicts.
- **`_require_browser()` guard.** Any recon / hooks / Mojo tool that depends on an attached CDP session returns `{"error": "Not attached. …"}` instead of crashing when called out of order.

---

## Tool catalog (82 tools)

Every tool takes keyword arguments. Defaults are shown in `code style`; `REQUIRED` means the agent must supply the value.

### Device / ADB

| Tool | Args | Purpose |
|---|---|---|
| `device_status` | — | Serial + Edge/Chrome PID + foreground activity. Safest first call. |
| `device_info` | — | Full `getprop` device info (android version, SDK, ABI, model). |
| `device_screenshot` | `path='mcp_screenshot.png'` | Capture the device screen (PNG). |
| `adb_shell` | `command` REQUIRED, `check=False` | Run `sh -c <command>` on the device. Pipes/redirections work. |
| `adb_forward_list` | — | List `adb forward` rules. |
| `adb_unix_sockets` | `filter_regex='devtools|webview'` | Dump abstract unix sockets matching regex. Confirms DevTools socket is actually open. |

### Emulator / SDK lifecycle

| Tool | Args | Purpose |
|---|---|---|
| `emulator_setup` | `api_level=35`, `arch='x86_64'` | Download Android SDK + system image (~5 GB, one-time). |
| `emulator_install_chromium` | — | Download and install a debuggable Chromium APK (required for CDP on API 35+). |
| `avd_create` | `avd_name='harness_device'`, `api_level=35`, `arch='x86_64'`, `device_profile='pixel_7'`, `force=False` | Create an AVD. |
| `avd_delete` | `avd_name='harness_device'` | Delete an AVD. |
| `emulator_start` | `avd_name`, `api_level`, `arch`, `headless=False`, `gpu='auto'`, `ram=4096`, `wipe_data=False`, `cold_boot=False`, `writable_system=True`, `boot_timeout=300.0` | Boot the emulator (blocks until `sys.boot_completed=1`). Auto-creates AVD if missing. |
| `emulator_stop` | — | Stop all running emulators. |

### Install / files

| Tool | Args | Purpose |
|---|---|---|
| `install_apk` | `path` REQUIRED | `adb install -r -t <path>`. |
| `push_file` | `local`, `remote` REQUIRED | `adb push`. |
| `pull_file` | `remote`, `local` REQUIRED | `adb pull`. |
| `browser_open` | `url` REQUIRED | Open URL via Android `VIEW` intent (no CDP restart). |

### Browser (Chromium / Edge) — CDP lifecycle

| Tool | Args | Purpose |
|---|---|---|
| `list_browsers` | — | Known browser presets (`chrome`, `chromium`, `edge`, `edge-canary`, `edge-dev`, `edge-local`). |
| `cdp_status` | — | Is DevTools socket present? Attached? Page list. |
| `cdp_prepare_and_launch` | `browser='edge-local'`, `wait_socket_timeout=30.0` | **Atomic cold-launch**: write flags → force-stop → start → poll `/proc/net/unix` for `chrome_devtools_remote` (typically appears ~15 s later) → attach. The reliable replacement for manual `--prepare` + `am force-stop` + `am start` + `sleep`. |
| `cdp_attach` | `browser='edge-local'` | Attach to an already-running browser without restarting. |
| `cdp_list_pages` | — | List every CDP page target. |
| `cdp_connect_to` | `target_id=None`, `url_substring=None` | Pick a page target by id or URL substring. |
| `cdp_disconnect` | — | Close the CDP session (browser keeps running). |

### CDP page ops

| Tool | Args | Purpose |
|---|---|---|
| `cdp_eval` | `expression` REQUIRED, `await_promise=False`, `timeout=15.0` | Evaluate JS. **Preview-aware** — returns a structured `__cdp_*` dict for `window` / DOM nodes / bridge objects instead of throwing `-32000 Object reference chain is too long`. |
| `cdp_navigate` | `url` REQUIRED, `wait_for_load=True`, `wait_for_expression=None`, `wait_timeout=10.0`, `timeout=30.0` | Navigate; optionally poll a JS expression until truthy (defeats races on host-injected globals). |
| `cdp_wait_for` | `expression` REQUIRED, `timeout=10.0` | Poll `expression` until truthy. |
| `cdp_inject_on_load` | `script` REQUIRED | `Page.addScriptToEvaluateOnNewDocument`; applies from the **next** navigation. Returns an id. |
| `cdp_remove_injected` | `identifier` REQUIRED | Remove an on-load script. |
| `cdp_page_screenshot` | `path='mcp_page_screenshot.png'` | Screenshot the attached page (not the device chrome). |

### WebView (mini apps, embedded WebViews, 3rd-party apps)

| Tool | Args | Purpose |
|---|---|---|
| `webview_list` | — | Fast socket list from `/proc/net/unix` (no port forwarding). |
| `webview_enumerate` | — | Full enumeration: forward each `webview_devtools_remote_<pid>` socket and query `GET /json`. |
| `webview_connect_socket` | `socket_name` REQUIRED, `local_port=9300` | Replace the current CDP session with one pointed at a specific WebView. |

### Input (with safety rails)

| Tool | Args | Purpose |
|---|---|---|
| `input_tap` | `x`, `y` REQUIRED | Tap at coordinates. Validates bounds. |
| `input_swipe` | `x1`, `y1`, `x2`, `y2` REQUIRED, `duration_ms=500` | Swipe. **Refuses** vertical displacements > 55 % of screen height at < 600 ms (known to crash Edge's renderer on x86_64). **Refuses** durations < 150 ms. |
| `input_key` | `keycode` REQUIRED | `input keyevent <keycode>` (e.g. `KEYCODE_BACK`, numeric `4`, `KEYCODE_APP_SWITCH`). |

### UI automation (UIAutomator, smart taps, monkey)

| Tool | Args | Purpose |
|---|---|---|
| `ui_dump` | `max_depth=10` | Dump current-screen hierarchy. |
| `ui_find_by_text` | `text` REQUIRED, `exact=False` | Find nodes matching visible text. |
| `ui_find_by_resource_id` | `resource_id` REQUIRED | Find nodes matching resource-id. |
| `ui_tap_text` | `text` REQUIRED | Dump → find by text → tap center. |
| `ui_tap_resource_id` | `resource_id` REQUIRED | Tap by resource-id. |
| `ui_type_into` | `resource_id`, `text` REQUIRED | Tap input by resource-id then type. |
| `ui_monkey` | `package=None`, `event_count=500`, `throttle_ms=50`, `seed=None`, `ignore_crashes=False` | Android `monkey` stress test. |

### Proxy / MITM

| Tool | Args | Purpose |
|---|---|---|
| `proxy_enable` | `host='10.0.2.2'`, `port=8080` | `settings put global http_proxy`. |
| `proxy_disable` | — | Clear device HTTP proxy. |
| `proxy_status` | — | Current `http_proxy` setting. |
| `proxy_install_mitmproxy_ca` | — | Install mitmproxy CA into system trust store (needs writable_system + root). |
| `proxy_install_ca` | `cert_path` REQUIRED | Install a custom PEM cert. |
| `proxy_tcpdump_start` | `remote_path='/sdcard/capture.pcap'` | Start on-device tcpdump. |
| `proxy_tcpdump_stop` | `remote_path`, `local_path='capture.pcap'` | Stop tcpdump and pull pcap. |
| `proxy_hosts_add` | `ip`, `hostname` REQUIRED | Add `/etc/hosts` entry. |
| `proxy_hosts_reset` | — | Reset `/etc/hosts`. |
| `proxy_hosts_show` | — | Show `/etc/hosts`. |

### Recon (requires attached browser)

All of these run against the currently-loaded page. Call `cdp_navigate` first.

| Tool | Args | Purpose |
|---|---|---|
| `recon_full` | `output=None` | Fingerprint + spider + storage + CSP + security headers + cookies, in one shot. |
| `recon_fingerprint` | — | Server / frameworks / generator / meta tags. |
| `recon_spider` | — | Links, forms, iframes, scripts, API endpoints, emails, comments. |
| `recon_storage` | — | Cookies + localStorage + sessionStorage. |
| `recon_csp` | — | Analyze Content-Security-Policy. |
| `recon_cookies` | — | Cookie security (secure/httpOnly/sameSite). |
| `recon_security_headers` | — | Presence/absence of common security headers. |

### JS hooks

Captures fetch / XHR / WebSocket / postMessage / console / storage / forms / cookies.

| Tool | Args | Purpose |
|---|---|---|
| `hooks_install` | `names='all'` | Install a comma-separated list from `xhr,fetch,cookies,websocket,postmessage,console,storage,forms` or `all`. Applies from the next navigation. |
| `hooks_install_custom` | `name`, `script` REQUIRED | Install a custom hook. Must push captured data into `window.__harness_captures`. |
| `hooks_collect` | `clear=False` | Return captured events (optionally clear the buffer). |
| `hooks_remove_all` | — | Remove all hooks. |

### Forensics (APK, app data, manifest, secrets)

| Tool | Args | Purpose |
|---|---|---|
| `forensics_scan_apk` | `apk_path` REQUIRED, `output=None` | Full scan: secrets + manifest (local only). |
| `forensics_scan_secrets` | `apk_path` REQUIRED | Hardcoded API keys, tokens, etc. |
| `forensics_scan_manifest` | `apk_path` REQUIRED | AndroidManifest.xml audit. |
| `forensics_scan_app_data` | `package` REQUIRED, `local_dir='app_data'` | Pull private data dir (needs debuggable / run-as) and scan. |

### Intents

| Tool | Args | Purpose |
|---|---|---|
| `intent_enumerate` | `package` REQUIRED | List exported activities / services / receivers / providers. |
| `intent_fuzz_package` | `package` REQUIRED | Send smart payloads (traversal, SQLi, deep-link abuse, large strings) to every exported component. |
| `intent_fuzz_component` | `component` REQUIRED, `component_type='activity'` | Fuzz a single component. |

### Logcat

| Tool | Args | Purpose |
|---|---|---|
| `logcat_tail` | `since_seconds=30`, `max_lines=200`, `filter_regex=None` | One-shot `logcat -d -t <Ns>` with optional regex filter. |
| `logcat_start` | `output='logcat.txt'`, `filter_tag=None`, `clear_first=True` | Start streaming to a file in the background. |
| `logcat_stop` | — | Stop the background capture; returns the path. |
| `logcat_find_crashes` | `path` REQUIRED | Scan a logcat file for ASan / SIGSEGV / ANR / tombstones. |

### Mojo IPC

| Tool | Args | Purpose |
|---|---|---|
| `mojo_enable_js` | `gen_dir=None`, `serve_port=8089`, `extra_flags=None` | Restart browser with `MojoJS,MojoJSTest` bindings. If `gen_dir` given, serve it on `10.0.2.2:port`. |
| `mojo_trigger_all` | `origin=None` | Call every Mojo-backed Web API (Clipboard, Gamepad, Serial, USB, …). |
| `mojo_trigger_selected` | `names` REQUIRED (list) | Trigger only named APIs. |
| `mojo_trace_start` | — | Start a Chrome tracing session scoped to Mojo categories. |
| `mojo_trace_stop` | `dump_path='mojo_trace.json'`, `timeout=60.0` | Stop trace, extract IPC messages, dump. |

### Pentest runner

| Tool | Args | Purpose |
|---|---|---|
| `pentest_run` | `script_path` REQUIRED | Execute a pentest script (`def run(ctx):`) against the attached session. |

### Local HTTP file server

| Tool | Args | Purpose |
|---|---|---|
| `fileserver_start` | `directory` REQUIRED, `port=8089`, `bind='0.0.0.0'` | Serve a directory; reachable from the emulator at `http://10.0.2.2:<port>/`. |
| `fileserver_stop` | — | Stop the server. |

---

## Recipes

### 1. From cold to Edge CDP REPL

```text
emulator_start(wipe_data=False)
cdp_prepare_and_launch(browser="edge-local")
cdp_navigate(url="https://example.com")
cdp_eval(expression="document.title")
```

### 2. Find the Sapphire mini-app bridge in Discover

```text
cdp_prepare_and_launch(browser="edge-local")
# User/UI triggers the Discover feed here…
webview_enumerate                              # lists every webview socket
webview_connect_socket(socket_name="@webview_devtools_remote_<pid>")
cdp_wait_for(expression="typeof sapphireWebViewBridge !== 'undefined'", timeout=15)
cdp_eval(expression="Object.getOwnPropertyNames(sapphireWebViewBridge)")
```

### 3. Full recon + fetch/XHR capture on a target

```text
cdp_prepare_and_launch
hooks_install(names="fetch,xhr,forms,storage")
cdp_navigate(url="https://target.example")
recon_full(output="recon.json")
hooks_collect(clear=true)
```

### 4. APK + app-data forensics

```text
install_apk(path="C:/samples/app.apk")
forensics_scan_apk(apk_path="C:/samples/app.apk", output="findings.json")
forensics_scan_app_data(package="com.example.app")
```

### 5. Intent fuzzing with crash detection

```text
logcat_start(output="fuzz_logcat.txt")
intent_fuzz_package(package="com.example.app")
logcat_stop
logcat_find_crashes(path="fuzz_logcat.txt")
```

### 6. MITM a mobile app's HTTPS

```text
proxy_install_mitmproxy_ca
proxy_enable(host="10.0.2.2", port=8080)
# start mitmproxy on the host…
browser_open(url="https://target")
# when done
proxy_disable
```

### 7. Mojo IPC tracing

Requires a **debuggable** Chromium/Edge build (`edge-local` or the Chromium from `emulator_install_chromium`; stable `chrome` will silently drop `--enable-blink-features=MojoJS`). The *gen_dir* must be the `gen/` sub-directory of a Chromium-style build output — that's where the `.mojom-lite.js` / `bindings.js` files live. `enable_mojojs` sets up `adb reverse` so the page fetches bindings from `http://127.0.0.1:8089/…` (no `10.0.2.2` needed).

```text
cdp_prepare_and_launch(browser="edge-local")
mojo_enable_js(gen_dir="Q:/Edge/src/win_x64_asan_release/gen")
mojo_trace_start
mojo_trigger_all
mojo_trace_stop(dump_path="mojo.json")
```

---

## Failure modes & hard-won notes

- **DevTools socket does not appear instantly.** On `com.microsoft.emmx.local` it takes ~15 s after cold-launch. `cdp_prepare_and_launch` polls for up to `wait_socket_timeout` seconds — bump it on slow hosts.
- **`evaluate_js` on `window` / DOM nodes** used to throw `-32000 Object reference chain is too long` or return `{}`. `cdp_eval` now auto-falls back to a preview: values come back as `{"__cdp_type": "object", "__cdp_class": "Window", "__cdp_properties": {...}}`.
- **Swipes crash Edge on x86_64.** Long vertical swipes in < 500 ms killed the renderer repeatedly during development. `input_swipe` refuses them; chunk the motion or slow to ≥ 600 ms.
- **`cdp_inject_on_load` fires on the _next_ navigation.** If you install the hook after already loading the target page, navigate once more (or reload) before evaluating.
- **Mini-app / Sapphire bridges are not on the main `chrome_devtools_remote` socket.** Use `webview_enumerate` + `webview_connect_socket`.
- **Recon / hooks / mojo tools require an attached browser.** Call `cdp_prepare_and_launch` or `cdp_attach` first, otherwise you get `{"error": "Not attached. …"}`.

---

## Related

- [harness-android CLI](../README.md) — everything here exists as a CLI subcommand too.
- [harness-android skill](../.github/skills/harness-android/SKILL.md) — instructions for Copilot that reference these tools by name (§M).
- [Browser / CDP internals](../harness_android/browser.py) — the Python surface behind `cdp_*`.
