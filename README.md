# android-harness

Cross-platform Android emulator harness with a built-in communication pipeline to control Android (including Chrome) from the outside. Works on **Windows** and **macOS**.

Under the hood it uses the official **Android Emulator** (QEMU-based) and **ADB**, managed automatically so you never have to touch `sdkmanager` by hand.

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

### Commands

#### `setup`
Download SDK, accept licences, install platform-tools, emulator, system image.

```bash
android-harness setup              # defaults to API 35 (Android 15)
android-harness setup --api 34     # use Android 14 instead
```

#### `create`
Create an AVD (Android Virtual Device).

```bash
android-harness create
android-harness create --name my_phone --api 35 --device pixel_7 --force
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

```bash
android-harness stop
```

#### `status`
Show SDK/AVD paths and connected devices.

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
Take a screenshot and save it locally.

```bash
android-harness screenshot -o shot.png
```

#### `push` / `pull`
Transfer files.

```bash
android-harness push local.txt /sdcard/local.txt
android-harness pull /sdcard/photo.jpg ./photo.jpg
```

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

#### `input`
Send touch / keyboard events.

```bash
android-harness input tap 540 960
android-harness input text "hello world"
android-harness input key 4          # KEYCODE_BACK
```

---

## Python API

Use android-harness as a library for scripting and automation:

```python
from android_harness.device import Device

# Context manager handles setup + teardown
with Device(headless=True) as dev:
    # ADB shell
    dev.run_shell("pm list packages")

    # Open a URL
    dev.open_url("https://example.com")

    # Screenshot
    dev.screenshot("home.png")

    # Chrome DevTools Protocol
    dev.browser.enable_cdp()
    dev.browser.connect()
    dev.browser.navigate("https://example.com")
    title = dev.browser.get_page_title()
    print(f"Title: {title}")

    # Evaluate JavaScript
    count = dev.browser.evaluate_js("document.querySelectorAll('a').length")
    print(f"Links on page: {count}")

    # DOM interaction
    dev.browser.click_element("a.nav-link")
    dev.browser.type_in_element("#search", "android")
    dev.browser.wait_for_selector(".results")

    # Cookies
    cookies = dev.browser.get_cookies()
    dev.browser.clear_cookies()

    # Page screenshot via CDP (higher quality than ADB screencap)
    dev.browser.page_screenshot("page.png")
```

### Lower-level access

```python
from android_harness.adb import ADB
from android_harness.emulator import Emulator
from android_harness.browser import Browser

# Attach to an already-running emulator
adb = ADB(serial="emulator-5554")
adb.wait_for_boot()

# Browser control
browser = Browser(adb)
browser.enable_cdp()
browser.connect()
html = browser.get_page_html()
browser.close()
```

---

## Architecture

```
┌──────────────────────────────────────────────────────┐
│  Your script / CLI                                   │
├──────────────┬───────────────┬────────────────────────┤
│  Device      │  Browser      │   (high-level API)     │
├──────────────┼───────────────┼────────────────────────┤
│  Emulator    │  ADB          │   (lifecycle + comms)   │
├──────────────┴───────────────┴────────────────────────┤
│  SDK manager  (bootstrap, sdkmanager, avdmanager)     │
├───────────────────────────────────────────────────────┤
│  Android Emulator (QEMU)  ←→  ADB  ←→  Chrome CDP    │
└───────────────────────────────────────────────────────┘
```

### Communication pipeline

1. **ADB** — primary channel for device control (shell, file transfer, app install, input events, port forwarding)
2. **Chrome DevTools Protocol (CDP)** — full browser automation over WebSocket, forwarded through ADB from Chrome's `chrome_devtools_remote` abstract socket
3. **Emulator console** — used for emulator lifecycle (kill, snapshot)

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
android-harness browser cdp --navigate "https://example.com" --page-screenshot result.png
android-harness stop
```

Use `--gpu swiftshader_indirect` for software rendering in environments without GPU access.

---

## Troubleshooting

| Problem | Solution |
|---|---|
| `sdkmanager` not found | Run `android-harness setup` |
| Emulator won't start | Ensure hardware acceleration is available (WHPX / HAXM / Hypervisor.framework) |
| `No inspectable page found` | Ensure Chrome is running; wait a few seconds after `enable_cdp()` |
| Slow on CI | Use `--headless --gpu swiftshader_indirect --ram 2048` |
| Java not found | Usually auto-installed by `setup`. Set `JAVA_HOME` if you prefer your own JDK. |

---

## License

MIT
