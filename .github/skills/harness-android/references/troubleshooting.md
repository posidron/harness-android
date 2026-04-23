# Troubleshooting

## Unicode arrows appear as `?` when piping output on Windows
Not a bug. The harness uses a single shared [`Console`](../../../../harness_android/console.py) that reconfigures stdout with `errors='replace'` to avoid `UnicodeEncodeError` on Windows cp1252 when stdout is piped (non-TTY). Any char the stream can't encode becomes `?`. In an actual terminal, arrows render normally.

## `harness-android: error: unrecognized arguments: -n ...`
`shell` uses `argparse.REMAINDER`, but Windows PowerShell sometimes still consumes leading flags. Put `--` **before** the command:
```powershell
poetry run harness-android shell -- am start -n com.pkg/.Main
poetry run harness-android shell -- am force-stop com.pkg
```

## `typeof sapphireWebViewBridge` → `"undefined"`
You are attached to `chrome-native://newtab/`. The bridge only exists inside the mini-app WebView. Either:
1. `browser cdp --attach --list-pages` after opening a mini app → attach via `--target-url sapphire` (or similar substring).
2. `webview list` → `webview connect webview_devtools_remote_<pid>`.
3. Confirm Sapphire is in the APK: `forensics scan <apk> | Select-String sapphire`.

## `RuntimeError: CDP Runtime.evaluate: 'Object reference chain is too long'`
Don't evaluate `window` directly — it has circular refs. Use:
```js
Object.keys(window).slice(0, 200)
Object.keys(window).filter(k => /sapphire|webview|bridge/i.test(k))
```

## Emulator shows `about:blank` after every restart
That's `enable_cdp()` restarting the browser. Switch to `--prepare` + manual launch + `--attach` (see SKILL section C).

## Edge Canary/Dev/Stable crashes on x86_64 emulator
ARM renderer SIGSEGV in `libndk_translation.so` under berberis. Use `edge-local` with an x86 `ChromePublic.apk`, or boot an ARM64 AVD on Apple Silicon.

## APK vanishes after emulator restart
`writable_system=True` is the new default — if an older AVD still shows this, recreate it with `harness-android delete --name <name>` then `create`/`start`.

## CDP not reachable after `--prepare`
`--prepare` only writes flags; it doesn't launch the browser. You must `am start` it manually, or the user must tap the launcher icon, before `--attach` can succeed.

## `webview list` shows only `chrome_devtools_remote`
No other app on the device currently has a debuggable WebView. Open the mini app / target app first, then rerun `webview list`.

## Ports already forwarded
```powershell
poetry run harness-android shell -- forward --list
# or via adb directly
adb forward --remove-all
```
