"""Live end-to-end smoke test against a running emulator.

Not run by pytest by default — invoke manually::

    python tests/smoke_live.py [--browser chromium]

Steps exercised:
  1. Find/boot the emulator
  2. enable_cdp() + connect()
  3. navigate() and verify Page.loadEventFired wait works
  4. evaluate_js() round-trip
  5. enable_mojojs() and assert Mojo bound in renderer
  6. MojoJS.fuzz_interface() with default payloads (crash-detect path)
  7. LogcatCapture start/stop + find_crashes()
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness_android.adb import ADB, poll_until  # noqa: E402
from harness_android.browser import Browser  # noqa: E402
from harness_android.emulator import Emulator  # noqa: E402
from harness_android.logcat import LogcatCapture  # noqa: E402
from harness_android.mojo import MojoJS, enable_mojojs  # noqa: E402


GREEN = "\x1b[32m"
RED = "\x1b[31m"
YELLOW = "\x1b[33m"
RESET = "\x1b[0m"


def step(msg: str) -> None:
    print(f"\n{YELLOW}-- {msg}{RESET}", flush=True)


def ok(msg: str) -> None:
    print(f"  {GREEN}OK{RESET} {msg}", flush=True)


def fail(msg: str) -> None:
    print(f"  {RED}FAIL {msg}{RESET}", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--browser", default="chromium",
                    choices=["chrome", "chromium", "edge"])
    ap.add_argument("--no-boot", action="store_true",
                    help="Assume emulator is already running")
    ap.add_argument("--mojo-interface", default="blink.mojom.ClipboardHost")
    args = ap.parse_args()

    failures = 0
    emu = None

    # ── 1. Emulator ──────────────────────────────────────────────────
    step("Locate or boot emulator")
    devices = ADB.list_devices()
    serial = next(
        (d["serial"] for d in devices
         if d["serial"].startswith("emulator-") and d["state"] == "device"),
        None,
    )
    if serial:
        adb = ADB(serial=serial)
        ok(f"Found running emulator: {serial}")
    elif args.no_boot:
        fail("No emulator running and --no-boot given")
        return 1
    else:
        emu = Emulator()
        adb = emu.start(headless=True, boot_timeout=300)
        serial = adb.serial
        ok(f"Booted emulator: {serial}")

    adb.wait_for_boot(timeout=300)
    ok("Device boot complete (PackageManager ready)")

    # ── 2. Logcat (background) ───────────────────────────────────────
    step("Start logcat capture")
    log = LogcatCapture(adb)
    log_path = log.start(output="smoke_logcat.txt")
    ok(f"Logcat → {log_path}")

    # ── 3. Browser CDP ───────────────────────────────────────────────
    step(f"Bring up {args.browser} with CDP + MojoJS")
    browser = Browser(adb, browser=args.browser)
    if not adb.is_installed(browser.package):
        fail(f"{browser.package} is not installed on the device")
        log.stop()
        return 1
    try:
        enable_mojojs(browser)
        ok(f"CDP connected (port {browser.local_port}); Mojo available in renderer")
    except Exception as exc:
        fail(f"enable_mojojs failed: {exc}")
        failures += 1
        # Fall back to plain CDP so the rest of the smoke can run.
        browser.enable_cdp()
        browser.connect()

    # ── 4. Navigate + JS round-trip ──────────────────────────────────
    step("navigate() waits for Page.loadEventFired")
    t0 = time.monotonic()
    browser.navigate("data:text/html,<title>smoke</title><h1 id=h>hello</h1>")
    dt = time.monotonic() - t0
    title = browser.evaluate_js("document.title")
    if title == "smoke":
        ok(f"Loaded in {dt:.2f}s, document.title == 'smoke'")
    else:
        fail(f"document.title == {title!r} (expected 'smoke')")
        failures += 1

    h = browser.evaluate_js("document.querySelector('#h').textContent")
    if h == "hello":
        ok("DOM query round-trip works")
    else:
        fail(f"#h textContent == {h!r}")
        failures += 1

    # ── 5. Browser-target routing ────────────────────────────────────
    step("Browser-target CDP routing (Browser.getVersion)")
    try:
        ver = browser.send("Browser.getVersion")
        ok(f"product={ver.get('product')!r}")
    except Exception as exc:
        fail(f"Browser.getVersion failed: {exc}")
        failures += 1

    # ── 6. MojoJS raw fuzz ───────────────────────────────────────────
    step(f"MojoJS.fuzz_interface({args.mojo_interface})")
    try:
        if browser.evaluate_js("typeof Mojo !== 'undefined'"):
            mojo = MojoJS(browser)
            payloads = MojoJS.default_payloads()[:6]
            results = mojo.fuzz_interface(args.mojo_interface, payloads,
                                          scope="context", settle_ms=50)
            crashed = sum(1 for r in results if r.crashed)
            errored = sum(1 for r in results if r.error and not r.crashed)
            ok(f"{len(results)} payloads sent — crashed={crashed} errored={errored}")
            if crashed and not browser.is_alive():
                fail("Browser did not auto-reconnect after crash")
                failures += 1
            elif crashed:
                ok("Auto-reconnect after crash succeeded")
        else:
            fail("Mojo not available in renderer — skipping fuzz")
            failures += 1
    except Exception as exc:
        fail(f"fuzz_interface raised: {exc}")
        failures += 1

    # ── 7. Logcat crash scan ─────────────────────────────────────────
    step("Stop logcat and scan for crashes")
    log.stop()
    crashes = LogcatCapture.find_crashes(log_path)
    ok(f"{len(crashes)} crash event(s) detected in logcat")
    for c in crashes[:5]:
        print(f"    [{c.severity}] {c.event_type}: {c.message[:80]}")

    # ── Summary ──────────────────────────────────────────────────────
    print()
    if failures == 0:
        print(f"{GREEN}SMOKE TEST PASSED{RESET}")
    else:
        print(f"{RED}SMOKE TEST FAILED — {failures} failure(s){RESET}")

    if emu is not None:
        step("Leaving emulator running (use `harness-android stop` to shut down)")

    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
