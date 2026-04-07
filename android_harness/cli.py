"""Command-line interface powered by argparse."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from rich.console import Console
from rich.table import Table

from android_harness.adb import ADB
from android_harness.browser import Browser
from android_harness.config import (
    CDP_LOCAL_PORT,
    DEFAULT_API_LEVEL,
    DEFAULT_AVD_NAME,
    get_avd_root,
    get_sdk_root,
)
from android_harness.emulator import Emulator
from android_harness.sdk import bootstrap_sdk, full_setup

console = Console()

# ======================================================================
# Helpers
# ======================================================================


def _find_serial(args: argparse.Namespace) -> str | None:
    return getattr(args, "serial", None)


# ======================================================================
# Sub-command handlers
# ======================================================================


def cmd_setup(args: argparse.Namespace) -> None:
    """Download the Android SDK, accept licences, install packages."""
    full_setup(args.api)
    console.print("\n[bold green]Setup complete! Run `android-harness create` next.")


def cmd_create(args: argparse.Namespace) -> None:
    """Create an AVD."""
    emu = Emulator(avd_name=args.name, api_level=args.api)
    emu.create_avd(device_profile=args.device, force=args.force)


def cmd_delete(args: argparse.Namespace) -> None:
    """Delete an AVD."""
    emu = Emulator(avd_name=args.name)
    emu.delete_avd()


def cmd_start(args: argparse.Namespace) -> None:
    """Start the emulator."""
    emu = Emulator(avd_name=args.name, api_level=args.api)
    if not emu.avd_exists():
        console.print(f"[yellow]AVD '{args.name}' not found, creating …")
        emu.create_avd(force=True)
    adb = emu.start(
        headless=args.headless,
        gpu=args.gpu,
        ram=args.ram,
        wipe_data=args.wipe,
    )
    info = {
        "serial": adb.get_serialno(),
        "android": adb.get_android_version(),
        "api": adb.get_api_level(),
    }
    table = Table(title="Emulator running")
    table.add_column("Property")
    table.add_column("Value")
    for k, v in info.items():
        table.add_row(k, v)
    console.print(table)
    console.print("[bold]Press Ctrl+C to stop.")
    try:
        emu._process.wait()  # type: ignore[union-attr]
    except KeyboardInterrupt:
        emu.stop()


def cmd_stop(_args: argparse.Namespace) -> None:
    """Kill all running emulators."""
    adb = ADB()
    for d in adb.list_devices():
        if d["serial"].startswith("emulator-"):
            console.print(f"[yellow]Killing {d['serial']} …")
            ADB(serial=d["serial"]).run("emu", "kill", check=False)
    console.print("[green]Done.")


def cmd_status(_args: argparse.Namespace) -> None:
    """Show ADB device list and SDK paths."""
    table = Table(title="Paths")
    table.add_column("Item")
    table.add_column("Path")
    table.add_row("SDK root", str(get_sdk_root()))
    table.add_row("AVD root", str(get_avd_root()))
    console.print(table)

    adb = ADB()
    devices = adb.list_devices()
    if devices:
        dt = Table(title="Connected devices")
        dt.add_column("Serial")
        dt.add_column("State")
        for d in devices:
            dt.add_row(d["serial"], d["state"])
        console.print(dt)
    else:
        console.print("[dim]No devices connected.")


def cmd_shell(args: argparse.Namespace) -> None:
    """Run a shell command on the device."""
    adb = ADB(serial=_find_serial(args))
    output = adb.shell(*args.command)
    sys.stdout.write(output)


def cmd_install(args: argparse.Namespace) -> None:
    """Install an APK."""
    adb = ADB(serial=_find_serial(args))
    adb.install(args.apk)


def cmd_screenshot(args: argparse.Namespace) -> None:
    """Capture a screenshot."""
    adb = ADB(serial=_find_serial(args))
    adb.screenshot(args.output)


def cmd_push(args: argparse.Namespace) -> None:
    """Push a file to the device."""
    adb = ADB(serial=_find_serial(args))
    adb.push(args.local, args.remote)
    console.print("[green]Pushed.")


def cmd_pull(args: argparse.Namespace) -> None:
    """Pull a file from the device."""
    adb = ADB(serial=_find_serial(args))
    adb.pull(args.remote, args.local)
    console.print("[green]Pulled.")


def cmd_browser_open(args: argparse.Namespace) -> None:
    """Open a URL in Chrome."""
    adb = ADB(serial=_find_serial(args))
    browser = Browser(adb)
    browser.open_url(args.url)


def cmd_browser_cdp(args: argparse.Namespace) -> None:
    """Set up CDP and optionally navigate / run JS."""
    adb = ADB(serial=_find_serial(args))
    browser = Browser(adb, local_port=args.port)
    browser.enable_cdp()
    browser.connect()

    if args.navigate:
        browser.navigate(args.navigate)

    if args.js:
        result = browser.evaluate_js(args.js)
        console.print(json.dumps(result, indent=2, default=str))

    if args.title:
        console.print(f"Page title: {browser.get_page_title()}")

    if args.page_screenshot:
        browser.page_screenshot(args.page_screenshot)

    if args.interactive:
        console.print("[bold]Interactive CDP REPL — type JS (or 'quit'):")
        while True:
            try:
                expr = input("cdp> ")
            except (EOFError, KeyboardInterrupt):
                break
            if expr.strip().lower() in ("quit", "exit"):
                break
            try:
                result = browser.evaluate_js(expr)
                console.print(json.dumps(result, indent=2, default=str))
            except Exception as exc:  # noqa: BLE001
                console.print(f"[red]{exc}")

    if not (args.navigate or args.js or args.title or args.page_screenshot or args.interactive):
        console.print(
            f"[green]CDP ready on http://localhost:{args.port}/json\n"
            "Use --navigate, --js, --title, --page-screenshot, or --interactive."
        )

    browser.close()


def cmd_input_tap(args: argparse.Namespace) -> None:
    adb = ADB(serial=_find_serial(args))
    adb.tap(args.x, args.y)


def cmd_input_text(args: argparse.Namespace) -> None:
    adb = ADB(serial=_find_serial(args))
    adb.text(args.text)


def cmd_input_key(args: argparse.Namespace) -> None:
    adb = ADB(serial=_find_serial(args))
    adb.key_event(args.keycode)


# ======================================================================
# Parser construction
# ======================================================================


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="android-harness",
        description="Cross-platform Android emulator harness with remote control",
    )
    parser.add_argument(
        "-s", "--serial", default=None, help="ADB device serial (default: auto)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # ---- setup ----
    p = sub.add_parser("setup", help="Download SDK and system images")
    p.add_argument("--api", type=int, default=DEFAULT_API_LEVEL, help="API level")
    p.set_defaults(func=cmd_setup)

    # ---- create ----
    p = sub.add_parser("create", help="Create an AVD")
    p.add_argument("--name", default=DEFAULT_AVD_NAME, help="AVD name")
    p.add_argument("--api", type=int, default=DEFAULT_API_LEVEL)
    p.add_argument("--device", default="pixel_7", help="Device profile")
    p.add_argument("--force", action="store_true", help="Overwrite existing AVD")
    p.set_defaults(func=cmd_create)

    # ---- delete ----
    p = sub.add_parser("delete", help="Delete an AVD")
    p.add_argument("--name", default=DEFAULT_AVD_NAME)
    p.set_defaults(func=cmd_delete)

    # ---- start ----
    p = sub.add_parser("start", help="Boot the emulator")
    p.add_argument("--name", default=DEFAULT_AVD_NAME)
    p.add_argument("--api", type=int, default=DEFAULT_API_LEVEL)
    p.add_argument("--headless", action="store_true", help="No GUI window")
    p.add_argument("--gpu", default="auto", help="GPU mode (auto|host|swiftshader_indirect|off)")
    p.add_argument("--ram", type=int, default=2048, help="RAM in MB")
    p.add_argument("--wipe", action="store_true", help="Wipe user data")
    p.set_defaults(func=cmd_start)

    # ---- stop ----
    p = sub.add_parser("stop", help="Stop all running emulators")
    p.set_defaults(func=cmd_stop)

    # ---- status ----
    p = sub.add_parser("status", help="Show paths and connected devices")
    p.set_defaults(func=cmd_status)

    # ---- shell ----
    p = sub.add_parser("shell", help="Run a shell command on the device")
    p.add_argument("command", nargs="+", help="Shell command")
    p.set_defaults(func=cmd_shell)

    # ---- install ----
    p = sub.add_parser("install", help="Install an APK")
    p.add_argument("apk", help="Path to .apk file")
    p.set_defaults(func=cmd_install)

    # ---- screenshot ----
    p = sub.add_parser("screenshot", help="Take a screenshot")
    p.add_argument("-o", "--output", default="screenshot.png")
    p.set_defaults(func=cmd_screenshot)

    # ---- push / pull ----
    p = sub.add_parser("push", help="Push file to device")
    p.add_argument("local", help="Local file path")
    p.add_argument("remote", help="Remote path on device")
    p.set_defaults(func=cmd_push)

    p = sub.add_parser("pull", help="Pull file from device")
    p.add_argument("remote", help="Remote path on device")
    p.add_argument("local", help="Local file path")
    p.set_defaults(func=cmd_pull)

    # ---- browser ----
    browser_sub = sub.add_parser("browser", help="Browser control")
    bsub = browser_sub.add_subparsers(dest="browser_cmd", required=True)

    # browser open
    p = bsub.add_parser("open", help="Open URL in Chrome")
    p.add_argument("url")
    p.set_defaults(func=cmd_browser_open)

    # browser cdp
    p = bsub.add_parser("cdp", help="Chrome DevTools Protocol control")
    p.add_argument("--port", type=int, default=CDP_LOCAL_PORT, help="Local CDP port")
    p.add_argument("--navigate", "-n", help="Navigate to URL")
    p.add_argument("--js", "-j", help="Evaluate JavaScript expression")
    p.add_argument("--title", action="store_true", help="Print page title")
    p.add_argument("--page-screenshot", help="Save page screenshot to path")
    p.add_argument("--interactive", "-i", action="store_true", help="Enter CDP REPL")
    p.set_defaults(func=cmd_browser_cdp)

    # ---- input ----
    input_sub = sub.add_parser("input", help="Send input events")
    isub = input_sub.add_subparsers(dest="input_cmd", required=True)

    p = isub.add_parser("tap", help="Tap at coordinates")
    p.add_argument("x", type=int)
    p.add_argument("y", type=int)
    p.set_defaults(func=cmd_input_tap)

    p = isub.add_parser("text", help="Type text")
    p.add_argument("text")
    p.set_defaults(func=cmd_input_text)

    p = isub.add_parser("key", help="Send a keycode")
    p.add_argument("keycode")
    p.set_defaults(func=cmd_input_key)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.func(args)
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted.")
        sys.exit(130)
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red bold]Error:[/] {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
