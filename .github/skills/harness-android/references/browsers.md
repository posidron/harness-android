# Browser presets & DevTools sockets

All Edge Android variants expose the **same** abstract socket: `chrome_devtools_remote`. The socket is **not** package-suffixed (contrary to what you may read elsewhere).

| Preset | Package | Socket | Notes |
|---|---|---|---|
| `chrome` | `com.android.chrome` | `chrome_devtools_remote` | Release Chrome — ignores `--remote-debugging-port` on API 35+ |
| `chromium` | `org.chromium.chrome` | `chrome_devtools_remote` | Debuggable. Install via `install-chromium` |
| `edge` | `com.microsoft.emmx` | `chrome_devtools_remote` | Edge Stable (ARM-only; renders under berberis on x86_64) |
| `edge-canary` | `com.microsoft.emmx.canary` | `chrome_devtools_remote` | Edge Canary (ARM-only) |
| `edge-dev` | `com.microsoft.emmx.dev` | `chrome_devtools_remote` | Edge Dev (ARM-only) |
| `edge-local` | `com.microsoft.emmx.local` | `chrome_devtools_remote` | Local x86 build of MSEdge chromium fork (`ChromePublic.apk`) |

## Berberis on x86_64

x86_64 Android emulators ship with **berberis** (ARM → x86 translator). Official Edge Canary/Dev/Stable APKs are ARM-only, and their renderer subprocesses routinely SIGSEGV inside `libndk_translation.so` under load. For reproducible Edge work on Windows hosts, prefer:

1. An x86 `ChromePublic.apk` built from the MSEdge chromium fork (→ `edge-local`).
2. On Apple Silicon Macs, boot an ARM64 emulator: `harness-android start --arch arm64`.

## When to use `--prepare` / `--attach`

`enable_cdp()` (default) does: write flags → `am force-stop` → `am start` → forward → poll `/json`. The restart destroys:

- Edge's native NTP (Android view where `sapphireWebViewBridge` lives)
- Any open mini app
- In-memory session state
- Proxy hook injection timing

Use the three-step flow from the SKILL:
1. `browser cdp --prepare` (flags only, no restart)
2. Manual `shell am start` (or user taps the launcher)
3. `browser cdp --attach` (connect-only, no restart)

## Finding Sapphire / mini-app targets

```powershell
# 1. Does this APK even contain Sapphire?
poetry run harness-android forensics scan <apk> | Select-String -Pattern "sapphire|SapphireWebView"

# 2. List every CDP page on the main socket
poetry run harness-android -b edge-local browser cdp --attach --list-pages

# 3. Separate WebView sockets (mini apps sometimes run in their own process)
poetry run harness-android webview list
```

If only `chrome-native://newtab/` shows and no `webview_devtools_remote_*` socket appears after opening a mini app, the installed APK likely does **not** contain Sapphire — it's a plain Chromium build renamed to `com.microsoft.emmx.local`.
