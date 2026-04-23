# harness-android — Command Catalog

All commands run as `poetry run harness-android <subcommand>`. Global flags go before the subcommand:

- `-s, --serial <serial>` — target a specific ADB device
- `-b, --browser {chrome|chromium|edge|edge-canary|edge-dev|edge-local}` — pick a preset

## Emulator lifecycle
| Command | Purpose |
|---|---|
| `setup [--api N] [--arch {x86_64,arm64}]` | Download SDK + system image (~5 GB, one-time) |
| `create [--name] [--api] [--device] [--arch] [--force]` | Create an AVD |
| `delete [--name]` | Delete an AVD |
| `start [--headless] [--gpu host] [--ram 4096] [--wipe] [--cold-boot] [--no-snapshot-save] [--name] [--arch]` | Boot emulator (auto-creates AVD) |
| `stop` | Kill all running emulators |
| `status` | Show SDK paths + connected devices |
| `install-chromium` | Install a debuggable Chromium (needed for CDP on API 35+) |

Note: `writable_system=True` is now the default — installed APKs persist across emulator restarts.

## Device control
| Command | Purpose |
|---|---|
| `shell -- <cmd ...>` | Run shell on device. **Use `--` before flags** |
| `install <apk> [--sdcard]` | Install APK |
| `screenshot [-o shot.png]` | Device screenshot |
| `push <local> <remote>` | Push file |
| `pull <remote> <local>` | Pull file |
| `input tap <x> <y>` | Tap |
| `input text "..."` | Type |
| `input key <keycode>` | Send keycode (4 = BACK) |

## Browser control
| Command | Purpose |
|---|---|
| `browser open <url>` | Launch via Android intent |
| `browser cdp --navigate <url>` | Navigate via CDP (restarts browser) |
| `browser cdp --js "<expr>"` | One-shot JS eval |
| `browser cdp --title` | Print page title |
| `browser cdp --page-screenshot <path>` | CDP-level screenshot |
| `browser cdp --interactive` | JS REPL |
| `browser cdp --prepare` | Write CDP flags **without restarting** |
| `browser cdp --attach` | Connect to already-running browser |
| `browser cdp --list-pages` | Enumerate all CDP page targets |
| `browser cdp --target-url <substr>` | Attach to page whose URL contains `<substr>` |
| `browser cdp --target-id <id>` | Attach to an exact CDP page id |
| `browser cdp --inject <file-or-js>` | Install on-load JS (CDP `Page.addScriptToEvaluateOnNewDocument`) — file path or inline |
| `browser cdp --chrome-flags "--flag ..."` | Extra Chrome flags |

## Proxy / MITM
| Command | Purpose |
|---|---|
| `proxy enable [--host] [--port]` | Route device traffic through proxy |
| `proxy disable` | Remove proxy |
| `proxy status` | Show current proxy |
| `proxy install-ca --mitmproxy` | Install mitmproxy CA |
| `proxy install-ca --cert <pem>` | Install arbitrary CA |
| `proxy hosts --add "IP=HOST"` | Edit `/etc/hosts` on device |
| `proxy hosts --reset` | Reset hosts file |
| `proxy tcpdump` / `--stop -o pcap` | Packet capture on device |

## Recon / hooks / pentest
| Command | Purpose |
|---|---|
| `recon --url <u> [-o] [--fingerprint] [--spider] [--storage] [--csp]` | Full or per-module recon |
| `hooks --url <u> [--hooks xhr,fetch,...] [--wait N] [-o]` | Inject JS hooks |
| `pentest run <script.py> [--report]` | Run `run(ctx)` script |

## Mojo
| Command | Purpose |
|---|---|
| `mojo enable [--gen-dir <dir>] [--interactive] [--navigate <url>]` | Enable MojoJS bindings, optionally serve `gen/` |
| `mojo trigger --url <u>` | Exercise 23 Mojo-backed Web APIs |
| `mojo trace [--trigger] [--duration N] [--chrome-trace trace.json] [-o]` | Chrome tracing for Mojo |
| `mojo fuzz <Interface.method> [--url]` | Boundary-input fuzz |

## File server / forensics / intents / logcat / UI / webview
| Command | Purpose |
|---|---|
| `serve <dir> [--port 8089]` | HTTP server, reachable as `http://10.0.2.2:PORT/` from emulator |
| `forensics scan <apk>` | Secrets + manifest |
| `forensics scan-app <pkg> [--app-data]` | Pulls APK from device then scans |
| `forensics installed` | Pull + scan all 3rd-party APKs |
| `forensics secrets <apk>` | Secrets only |
| `forensics manifest <apk>` | Manifest audit |
| `forensics app-data <pkg>` | Extract + scan private data |
| `intent enumerate <pkg>` | List exported components |
| `intent fuzz <pkg> [--component]` | Fuzz intents, watch logcat |
| `logcat stream [--tag]` | Real-time logcat |
| `logcat capture --duration N [-o]` | Fixed capture + crash scan |
| `ui dump [--clickable] [--depth N]` | UIAutomator hierarchy |
| `ui tap --text/--resource-id` | Find-and-tap |
| `ui type <resource-id> <text>` | Type into a field |
| `ui monkey -p <pkg> -n N` | Android monkey |
| `webview list` | Enumerate `*_devtools_remote*` sockets |
| `webview connect <socket> [--navigate|--js|--page-screenshot|--inject|--interactive]` | Attach to any WebView; `--inject` supports file path or inline JS |
