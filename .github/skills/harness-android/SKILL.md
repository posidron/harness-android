---
name: harness-android
description: "Drive the harness-android CLI (Android emulator + mobile browser pentest toolkit) from Copilot. USE FOR: boot Android emulator, install APK, run adb shell, control Chrome/Edge via CDP, interactive JS REPL on a mobile browser, list debuggable WebViews, connect to Android WebView sockets, enumerate Sapphire/mini-app WebViews, proxy/MITM emulator traffic, install CA cert, intercept Android HTTPS, spider a site from mobile, extract cookies/localStorage, inject JS hooks (fetch/xhr/postmessage/forms), APK secret scanning, AndroidManifest audit, pull app private data, fuzz Android intents, stream logcat, capture tombstones, Mojo IPC tracing/fuzzing, enable MojoJS bindings, run pentest scripts with ctx, serve local files to emulator on 10.0.2.2, take device screenshots. DO NOT USE FOR: iOS, non-Android mobile, generic Chromium desktop debugging."
---

# harness-android

Cross-platform Android emulator + mobile-browser pentest toolkit. Every command in this skill is run from the repo root via `poetry run harness-android <...>`.

## When to use

Trigger phrases: "boot the emulator", "install this apk", "run adb", "open Chrome on Android", "connect CDP", "JS REPL on mobile browser", "list webviews", "attach to a mini app", "enumerate sapphire webview", "inspect Edge Android", "proxy emulator traffic", "install mitm CA", "scan apk for secrets", "fuzz intents", "capture logcat", "Mojo trace", "enable MojoJS", "dump storage/cookies", "CSP audit on mobile", "run a pentest script".

Do not use for iOS, Frida, physical iOS devices, or non-mobile Chromium.

## Rules

1. Always prefix commands with `poetry run` — this is a Poetry project.
2. Never propose installing the Android SDK by hand. Use `harness-android setup`.
3. Chrome DevTools Protocol requires a debuggable build on API 35+. Use `install-chromium` or the `edge-local` preset for Edge.
4. `enable_cdp()` and the default `browser cdp` flow **restart the browser**, which wipes any native surface state (Edge NTP, open tabs, in-memory auth). When the user needs to preserve a running state, use the `--prepare` + manual launch + `--attach` flow documented below.
5. The `shell` subcommand accepts flags only via `argparse.REMAINDER`. When passing flags like `-n`, `-a`, `--user`, put them after `--` to be safe:
   `harness-android shell -- am start -n com.pkg/.Activity`
6. To connect to a non-Chrome WebView (mini app, embedded browser, third-party app), use `webview list` → `webview connect <socket>`. Do **not** try to reach them through the main `chrome_devtools_remote` socket.
7. Choose the browser with the global `-b` flag: `chrome`, `chromium`, `edge`, `edge-canary`, `edge-dev`, `edge-local`. Put `-b` **before** the subcommand: `harness-android -b edge-local browser cdp ...`.

## Procedures

### A. First-time setup
1. `poetry install`
2. `poetry run harness-android setup` — downloads SDK + API 35 x86_64 image (~5 GB, one-time).
3. `poetry run harness-android start` — boots the emulator, auto-creates an AVD if needed.
4. `poetry run harness-android install-chromium` — required if the task involves CDP on API 35+.

### B. Install & launch a custom browser build (e.g. x86 Edge `ChromePublic.apk`)
```powershell
poetry run harness-android install C:\path\to\ChromePublic.apk
# verify
poetry run harness-android shell -- pm path com.microsoft.emmx.local
# launch manually (do NOT use `browser cdp` yet — that would restart it)
poetry run harness-android shell -- am start -n com.microsoft.emmx.local/com.google.android.apps.chrome.Main
```

### C. Non-destructive CDP attach (preserve native NTP / mini-app state)
This is the canonical flow for reproducing Edge Sapphire / mini-app findings.

```powershell
# 1. Write the Chrome/Edge command-line flags WITHOUT restarting
poetry run harness-android -b edge-local browser cdp --prepare

# 2. Fully stop + cold-launch Edge so it picks up the flags naturally
poetry run harness-android shell -- am force-stop com.microsoft.emmx.local
poetry run harness-android shell -- am start -n com.microsoft.emmx.local/com.google.android.apps.chrome.Main
# let the user tap the mini-app they want (Copilot/Rewards/Discover/etc.)

# 3. Enumerate every CDP page target on the main socket
poetry run harness-android -b edge-local browser cdp --attach --list-pages

# 4. Attach to a specific page by URL substring
poetry run harness-android -b edge-local browser cdp --attach --target-url "sapphire" --interactive
# or by exact CDP id
poetry run harness-android -b edge-local browser cdp --attach --target-id <id> --interactive
```

### D. Mini-app / third-party WebView not on the main socket
Mini apps and embedded WebViews often run in a **separate renderer process** with their own `webview_devtools_remote_<pid>` socket.

```powershell
poetry run harness-android webview list
poetry run harness-android webview connect webview_devtools_remote_12345 --interactive
poetry run harness-android webview connect webview_devtools_remote_12345 --js "typeof sapphireWebViewBridge"

# Inject an on-load script into this WebView (survives future navigations)
poetry run harness-android webview connect webview_devtools_remote_12345 --inject ./my_hooks.js
```

### D2. Inject JS into any page (Edge, Chrome, WebView)
`--inject` installs a script via CDP `Page.addScriptToEvaluateOnNewDocument`. It runs **before any page script** on every subsequent navigation in the attached target. Accepts either a file path or inline JS.

```powershell
# Inline: stamp a marker on every page Edge loads
poetry run harness-android -b edge-local browser cdp --attach \
  --inject "window.__harness_marker = 'X';" --navigate "https://target" --js "window.__harness_marker"

# File: load a hook bundle
poetry run harness-android -b edge-local browser cdp --attach --inject ./hooks.js --interactive
```
Verified working with `edge-local` (x86 `ChromePublic.apk`).

**Important**: `Page.addScriptToEvaluateOnNewDocument` applies to the **next** navigation — it does not retroactively run on the already-loaded page. Combine with `--navigate` (or `navigate()` in Python) to trigger it. For an already-loaded page, use `--js` / `evaluate_js()` instead.

### D3. Defeat races — wait for late-appearing symbols
`Page.loadEventFired` fires when DOM is ready, **but host-injected globals (`sapphireWebViewBridge`, `edgeSapphire`, framework state) typically appear *after* that.** Reading them immediately after `navigate()` is racy.

```powershell
# CLI: poll until the symbol exists, then run --js
poetry run harness-android -b edge-local browser cdp --attach \
  --navigate "https://some-mini-app" \
  --wait-for "typeof sapphireWebViewBridge !== 'undefined'" --wait-timeout 15 \
  --js  "Object.getOwnPropertyNames(sapphireWebViewBridge)"
```

In Python:
```python
b.navigate("https://some-mini-app")
b.wait_for_expression("typeof sapphireWebViewBridge !== 'undefined'", timeout=15)
surface = b.evaluate_js("Object.getOwnPropertyNames(sapphireWebViewBridge)")
```

### D4. `evaluate_js` on non-serializable values (window, DOM, bridges)
`Runtime.evaluate` with `returnByValue:true` throws `-32000 "Object reference chain is too long"` on circular objects (`window`, `document`, many bridge objects) and returns a useless empty `{}` on DOM nodes. Our `evaluate_js` detects both cases automatically and returns a structured preview:

```python
>>> b.evaluate_js("window")
{"__cdp_type": "object", "__cdp_class": "Window",
 "__cdp_properties": {"window": "Window", "self": "Window",
                      "document": "#document", "location": "Location"},
 "__cdp_truncated": True}

>>> b.evaluate_js("document.body")
{"__cdp_type": "object", "__cdp_subtype": "node",
 "__cdp_class": "HTMLBodyElement", "__cdp_desc": "body",
 "__cdp_properties": {...}}

>>> b.evaluate_js("({a:1,b:2})")     # real objects still round-trip as JSON
{"a": 1, "b": 2}
```
If you need the real remote handle (e.g. to pass to `Runtime.callFunctionOn`), use `evaluate_js(expr, return_by_value=False)` and inspect `__cdp_*` fields.

### E. Proxy + MITM emulator traffic
```powershell
poetry run harness-android proxy install-ca --mitmproxy       # or --cert <pem>
poetry run harness-android proxy enable                       # default 10.0.2.2:8080
# … run mitmproxy / Burp on the host …
poetry run harness-android proxy disable
```

### F. Recon a site from the mobile browser
```powershell
poetry run harness-android recon --url "https://target" -o recon.json
# or module-by-module: --fingerprint | --spider | --storage | --csp
```

### G. JS hooks (capture fetch/xhr/postmessage/forms)
```powershell
poetry run harness-android hooks --url "https://target" --wait 30 -o captured.json
poetry run harness-android hooks --hooks fetch,xhr,forms --url "https://target"
```

### H. APK forensics
```powershell
poetry run harness-android forensics scan-app com.example.app --app-data -o report.json
poetry run harness-android forensics installed -o all_apps.json
```

### I. Intent fuzzing
```powershell
poetry run harness-android intent enumerate com.example.app
poetry run harness-android intent fuzz com.example.app --component .DeepLinkActivity
```

### J. Mojo IPC
```powershell
poetry run harness-android mojo trigger --url "https://target"
poetry run harness-android mojo trace --trigger -o mojo.json
poetry run harness-android mojo fuzz Clipboard.writeText --url "https://target"
# MojoJS bindings in JS (requires debuggable Chromium):
poetry run harness-android mojo enable --gen-dir C:\chromium\out\Release\gen --interactive
```

### K. Pentest script
Minimum viable script — see [example](./references/pentest-ctx.md) for the full `ctx` surface.
```python
def run(ctx):
    ctx.navigate("https://target/login")
    ctx.hooks.install("fetch", "forms")
    ctx.type_in("#u", "admin"); ctx.type_in("#p", "x")
    ctx.click("button[type=submit]"); ctx.wait(3)
    ctx.report(path="out.json")
```
Run: `poetry run harness-android pentest run my_test.py --report findings.json`

### L. Python API — remote-control Edge from code
Every CLI capability is exposed on [`Browser`](../../../harness_android/browser.py) in Python. This is the stable surface for scripting.

```python
from harness_android.adb import ADB
from harness_android.browser import Browser

adb = ADB()                                       # picks up running emulator
b = Browser(adb, browser="edge-local")
b.attach_cdp()                                    # no restart — keep current state
tid = b.find_target(url_substring="sapphire")     # or url_substring="http" / None for default
b.connect(target_id=tid) if tid else b.connect()

# Inject a hook that survives every subsequent navigation
b.inject_script_on_load("window.__tap = e => fetch('/log?' + e);")

b.navigate("https://target")
print(b.evaluate_js("document.title"))
b.page_screenshot("shot.png")
b.close()
```

Key methods:
- `attach_cdp()` — connect without restart; `prepare_cdp()` writes flags only; `enable_cdp()` = flags + restart + connect
- `list_targets()` / `find_target(url_substring=..., target_id=...)` — pick a page (returns id or None)
- `connect(target_id=...)` / `navigate(url)` / `evaluate_js(expr, await_promise=True)`
- `evaluate_js` returns primitives / arrays / plain JSON directly; returns a structured `__cdp_*` preview for DOM nodes, `window`, and other non-serializable objects instead of throwing or returning `{}`
- `wait_for_expression(expr, timeout=10)` — race-free: poll until the expression is truthy (e.g. `"typeof sapphireWebViewBridge !== 'undefined'"`); raises `TimeoutError` on failure
- `inject_script_on_load(js)` — **verified end-to-end on `edge-local`**; returns an identifier you can pass to `remove_injected_script(id)`. Applies to the *next* navigation
- `dispatch_touch(x, y)` / `dispatch_swipe(...)` / `dispatch_key(...)` for realistic browser input
- `get_page_title()` / `get_page_url()` / `page_screenshot(path)`
- `CDPError` — raised for protocol-level errors with `.code` / `.message` attributes (catch `exc.code == -32000` etc.)

### M. MCP server — structured tools (preferred over shell for AI agents)
An MCP server (`harness-android-mcp`) exposes **every CLI feature** as JSON-RPC tools. When available, **prefer calling these tools over spawning `poetry run harness-android …` subshells** — they maintain a persistent CDP session, return JSON-serializable data, and sidestep pyenv / PowerShell quoting fragility.

VS Code registration (already in `.vscode/mcp.json`):
```json
{ "servers": { "harness-android": {
  "type": "stdio", "command": "poetry",
  "args": ["run", "harness-android-mcp"], "cwd": "${workspaceFolder}" } } }
```

After editing the server code, run `MCP: List Servers` → `harness-android` → **Restart** to pick up changes.

Tools (82 total, grouped; all arguments are keyword):

- **Device / ADB**: `device_status`, `device_info`, `device_screenshot(path)`, `adb_shell(command)`, `adb_forward_list`, `adb_unix_sockets(filter_regex)`
- **Emulator / SDK**: `emulator_setup(api_level, arch)`, `emulator_install_chromium`, `avd_create`, `avd_delete`, `emulator_start(...)`, `emulator_stop`
- **Install / files**: `install_apk(path)`, `push_file(local, remote)`, `pull_file(remote, local)`, `browser_open(url)` (intent-based)
- **CDP lifecycle**: `list_browsers`, `cdp_status`, `cdp_prepare_and_launch(browser='edge-local', wait_socket_timeout=30)` — **atomic cold-launch that polls `/proc/net/unix` for the socket (appears ~15 s after cold-start, not instant)**, `cdp_attach(browser)`, `cdp_list_pages`, `cdp_connect_to(target_id|url_substring)`, `cdp_disconnect`
- **Page ops**: `cdp_eval(expression)` — preview-aware, safe for `window` / DOM, `cdp_navigate(url, wait_for_expression=None)`, `cdp_wait_for(expression, timeout)`, `cdp_inject_on_load(script)` / `cdp_remove_injected(id)`, `cdp_page_screenshot(path)`
- **WebView** (embedded, mini apps, 3rd-party apps): `webview_list` (socket list), `webview_enumerate` (full page-target query per socket), `webview_connect_socket(socket_name)`
- **Input** (with safety rails — refuses swipes known to crash Edge's renderer on x86_64 emulators): `input_tap(x, y)`, `input_swipe(x1, y1, x2, y2, duration_ms=500)`, `input_key(keycode)`
- **UI automation**: `ui_dump`, `ui_find_by_text`, `ui_find_by_resource_id`, `ui_tap_text`, `ui_tap_resource_id`, `ui_type_into`, `ui_monkey(package=None, event_count=500, ...)`
- **Proxy / MITM**: `proxy_enable(host, port)`, `proxy_disable`, `proxy_status`, `proxy_install_mitmproxy_ca`, `proxy_install_ca(cert_path)`, `proxy_tcpdump_start/stop`, `proxy_hosts_add/reset/show`
- **Recon** (requires attached browser): `recon_full(output=None)`, `recon_fingerprint`, `recon_spider`, `recon_storage`, `recon_csp`, `recon_cookies`, `recon_security_headers`
- **JS hooks**: `hooks_install(names='all')`, `hooks_install_custom(name, script)`, `hooks_collect(clear=False)`, `hooks_remove_all`
- **Forensics**: `forensics_scan_apk(apk_path)`, `forensics_scan_secrets`, `forensics_scan_manifest`, `forensics_scan_app_data(package)`
- **Intents**: `intent_enumerate(package)`, `intent_fuzz_package(package)`, `intent_fuzz_component(component, component_type='activity')`
- **Logs**: `logcat_tail(since_seconds, filter_regex)` (quick `logcat -d` snapshot), `logcat_start` / `logcat_stop` (background file capture), `logcat_find_crashes(path)`
- **Mojo IPC**: `mojo_enable_js(gen_dir=None)`, `mojo_trigger_all`, `mojo_trigger_selected(names=[...])`, `mojo_trace_start` / `mojo_trace_stop(dump_path)`
- **Pentest**: `pentest_run(script_path)` — runs a `def run(ctx):` script against the attached session
- **Local HTTP**: `fileserver_start(directory, port=8089)` / `fileserver_stop` — serves to emulator at `http://10.0.2.2:<port>/`

Typical flow: `emulator_start` → `cdp_prepare_and_launch` → `cdp_navigate(url, wait_for_expression="typeof sapphireWebViewBridge !== 'undefined'")` → `hooks_install("fetch,forms")` → `cdp_eval("Object.getOwnPropertyNames(sapphireWebViewBridge)")` → `hooks_collect(clear=true)`. Zero shells.

## References

- [Full command catalog](./references/commands.md)
- [Browser presets and sockets](./references/browsers.md)
- [PentestContext `ctx` API](./references/pentest-ctx.md)
- [Troubleshooting recipes](./references/troubleshooting.md)

## Anti-patterns

- Running `browser cdp --navigate ...` when the user needs to preserve the currently open tab / NTP / mini app. Use `--prepare` + manual launch + `--attach` instead.
- Assuming `sapphireWebViewBridge` or any mini-app symbol will be present on the `chrome-native://newtab/` tab. It is not — mini apps are separate page targets or separate WebView sockets.
- Passing `-n`, `-a`, `-p` to `harness-android shell` without `--` separator.
- Expecting an official ARM Edge APK to run renderer processes reliably on x86_64 emulators (berberis crashes). Use a real x86 build (`edge-local`) or a `-arch arm64` emulator on Apple Silicon.
