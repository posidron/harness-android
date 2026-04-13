"""Command-line interface powered by argparse."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from rich.console import Console
from rich.table import Table

from harness_android.adb import ADB
from harness_android.browser import Browser
from harness_android.config import (
    CDP_LOCAL_PORT,
    DEFAULT_API_LEVEL,
    DEFAULT_AVD_NAME,
    get_avd_root,
    get_sdk_root,
)
from harness_android.emulator import Emulator
from harness_android.sdk import bootstrap_sdk, full_setup

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
    if args.install_chromium:
        from harness_android.sdk import install_chromium
        install_chromium()
    console.print("\n[bold green]Setup complete! Run `harness-android create` next.")


def cmd_install_chromium(args: argparse.Namespace) -> None:
    """Download and install Chromium (debuggable) on the running emulator."""
    from harness_android.sdk import install_chromium
    install_chromium()


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
    from harness_android.config import load_config
    cfg = load_config().get("emulator", {})

    # CLI flags override config file values; config overrides built-in defaults
    name = args.name if args.name != DEFAULT_AVD_NAME else cfg.get("avd_name", DEFAULT_AVD_NAME)
    api = args.api if args.api != DEFAULT_API_LEVEL else cfg.get("api_level", DEFAULT_API_LEVEL)
    ram = args.ram if args.ram != 4096 else cfg.get("ram", 4096)
    gpu = args.gpu if args.gpu != "auto" else cfg.get("gpu", "auto")
    headless = args.headless or cfg.get("headless", False)

    emu = Emulator(avd_name=name, api_level=api)
    if not emu.avd_exists():
        console.print(f"[yellow]AVD '{name}' not found, creating …")
        emu.create_avd(force=True)
    adb = emu.start(
        headless=headless,
        gpu=gpu,
        ram=ram,
        wipe_data=args.wipe,
        cold_boot=getattr(args, "cold_boot", False),
        no_snapshot_save=getattr(args, "no_snapshot_save", False),
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
# Pentest sub-command handlers
# ======================================================================


def cmd_proxy_enable(args: argparse.Namespace) -> None:
    from harness_android.proxy import Proxy
    adb = ADB(serial=_find_serial(args))
    proxy = Proxy(adb, host=args.host, port=args.port)
    proxy.enable()


def cmd_proxy_disable(args: argparse.Namespace) -> None:
    from harness_android.proxy import Proxy
    adb = ADB(serial=_find_serial(args))
    Proxy(adb).disable()


def cmd_proxy_status(args: argparse.Namespace) -> None:
    from harness_android.proxy import Proxy
    adb = ADB(serial=_find_serial(args))
    current = Proxy(adb).get_current()
    console.print(f"Current proxy: {current or '(none)'}")


def cmd_proxy_install_ca(args: argparse.Namespace) -> None:
    from harness_android.proxy import Proxy
    adb = ADB(serial=_find_serial(args))
    proxy = Proxy(adb)
    if args.mitmproxy:
        proxy.install_mitmproxy_ca()
    else:
        proxy.install_ca_cert(args.cert)


def cmd_proxy_tcpdump(args: argparse.Namespace) -> None:
    from harness_android.proxy import Proxy
    adb = ADB(serial=_find_serial(args))
    proxy = Proxy(adb)
    if args.stop:
        proxy.stop_tcpdump()
        if args.output:
            proxy.pull_capture(local=args.output)
    else:
        proxy.start_tcpdump()
        console.print("[bold]Press Ctrl+C to stop, then run with --stop to pull capture.")


def cmd_proxy_hosts(args: argparse.Namespace) -> None:
    from harness_android.proxy import Proxy
    adb = ADB(serial=_find_serial(args))
    proxy = Proxy(adb)
    if args.reset:
        proxy.reset_hosts()
    elif args.add:
        parts = args.add.split("=", 1)
        if len(parts) != 2:
            console.print("[red]Use --add IP=hostname")
            return
        proxy.add_hosts_entry(parts[0], parts[1])
    else:
        console.print(proxy.show_hosts())


def cmd_recon(args: argparse.Namespace) -> None:
    from harness_android.recon import full_recon, fingerprint_page, spider_page, extract_storage, analyze_csp
    from harness_android.recon import print_fingerprint, print_spider, print_storage, print_csp

    adb = ADB(serial=_find_serial(args))
    browser = Browser(adb, local_port=args.port)
    browser.enable_cdp()
    browser.connect()

    if args.url:
        browser.navigate(args.url)
        import time; time.sleep(2)

    if args.full:
        full_recon(browser, output=args.output)
    elif args.fingerprint:
        fp = fingerprint_page(browser)
        print_fingerprint(fp)
    elif args.spider:
        sp = spider_page(browser)
        print_spider(sp)
    elif args.storage:
        data = extract_storage(browser)
        print_storage(data)
        if args.output:
            with open(args.output, "w") as f:
                json.dump(data, f, indent=2, default=str)
    elif args.csp:
        csp_data = analyze_csp(browser)
        print_csp(csp_data)
    else:
        # Default: full
        full_recon(browser, output=args.output)

    browser.close()


def cmd_hooks(args: argparse.Namespace) -> None:
    from harness_android.hooks import Hooks

    adb = ADB(serial=_find_serial(args))
    browser = Browser(adb, local_port=args.port)
    browser.enable_cdp()
    browser.connect()

    hooks = Hooks(browser)
    hook_names = args.hooks.split(",") if args.hooks else ["all"]
    hooks.install(*hook_names)

    if args.url:
        browser.navigate(args.url)

    if args.wait:
        console.print(f"[bold]Collecting for {args.wait}s …")
        import time; time.sleep(args.wait)
    else:
        console.print("[bold]Hooks active. Press Ctrl+C to collect and exit.")
        try:
            while True:
                import time; time.sleep(1)
        except KeyboardInterrupt:
            pass

    data = hooks.collect()
    total = sum(len(v) for v in data.values() if isinstance(v, list))
    console.print(f"\n[green]Collected {total} events")

    if args.output:
        hooks.dump(args.output)
    else:
        console.print(json.dumps(data, indent=2, default=str))

    browser.close()


def cmd_pentest_run(args: argparse.Namespace) -> None:
    from harness_android.pentest import run_script

    adb = ADB(serial=_find_serial(args))
    browser = Browser(adb, local_port=args.port)
    browser.enable_cdp()
    browser.connect()

    ctx = run_script(args.script, adb, browser)

    if args.report:
        ctx.report(path=args.report)

    browser.close()


def cmd_mojo_trace(args: argparse.Namespace) -> None:
    """Capture Mojo IPC trace while triggering Web APIs."""
    from harness_android.mojo import MojoTracer

    adb = ADB(serial=_find_serial(args))
    browser = Browser(adb, local_port=args.port)
    if args.verbose:
        browser._extra_chrome_flags.append("--enable-logging")
        browser._extra_chrome_flags.append("--vmodule=*mojo*=3")
    browser.enable_cdp()
    browser.connect()

    if args.url:
        browser.navigate(args.url)
        import time; time.sleep(2)

    tracer = MojoTracer(browser, verbose=args.verbose)
    tracer.start_trace()

    if args.trigger:
        results = tracer.trigger_all_apis()
        tracer.print_trigger_results(results)
    else:
        duration = args.duration or 10
        console.print(f"[bold]Recording Mojo trace for {duration}s …")
        import time; time.sleep(duration)

    events = tracer.stop_trace()
    messages = tracer.extract_mojo_messages(events)
    tracer.print_summary(messages)

    if args.output:
        tracer.dump(args.output, events, messages,
                    results if args.trigger else None)

    if args.chrome_trace:
        tracer.dump_chrome_trace(args.chrome_trace)

    browser.close()


def cmd_mojo_trigger(args: argparse.Namespace) -> None:
    """Trigger Mojo-backed Web APIs and show results."""
    from harness_android.mojo import MojoTracer

    adb = ADB(serial=_find_serial(args))
    browser = Browser(adb, local_port=args.port)
    browser.enable_cdp()
    browser.connect()

    if args.url:
        browser.navigate(args.url)
        import time; time.sleep(2)

    tracer = MojoTracer(browser)
    results = tracer.trigger_all_apis()
    tracer.print_trigger_results(results)

    if args.output:
        tracer.dump(args.output, trigger_results=results)

    browser.close()


def cmd_mojo_fuzz(args: argparse.Namespace) -> None:
    """Fuzz a Mojo-backed Web API with various inputs."""
    from harness_android.mojo import MojoTracer, MOJO_WEB_API_TRIGGERS

    adb = ADB(serial=_find_serial(args))
    browser = Browser(adb, local_port=args.port)
    browser.enable_cdp()
    browser.connect()

    if args.url:
        browser.navigate(args.url)
        import time; time.sleep(2)

    tracer = MojoTracer(browser)

    # Find the API template
    target = None
    for name, js, iface in MOJO_WEB_API_TRIGGERS:
        if name.lower() == args.api.lower():
            target = (name, js, iface)
            break

    if target is None:
        console.print(f"[red]Unknown API: {args.api}")
        console.print("Available APIs:")
        for name, _, iface in MOJO_WEB_API_TRIGGERS:
            console.print(f"  {name:40s}  {iface}")
        browser.close()
        return

    name, js_template, iface = target
    # Replace the original JS with a fuzzable version if possible
    # For most APIs the original JS code IS the template
    if "{FUZZ}" not in js_template:
        console.print(f"[yellow]API '{name}' doesn't have a {{FUZZ}} template — running with default fuzz strings against it")
        # We'll still exercise it with each fuzz input as extra context
        js_template_fuzz = js_template
    else:
        js_template_fuzz = js_template

    tracer.start_trace()
    results = tracer.fuzz_api(name, js_template_fuzz, MojoTracer.FUZZ_STRINGS, iface)
    events = tracer.stop_trace()
    messages = tracer.extract_mojo_messages(events)

    tracer.print_trigger_results(results)
    tracer.print_summary(messages)

    if args.output:
        tracer.dump(args.output, events, messages, results)

    browser.close()


# ======================================================================
# Forensics sub-command handlers
# ======================================================================


def cmd_forensics_scan(args: argparse.Namespace) -> None:
    from harness_android.forensics import full_apk_scan
    full_apk_scan(args.apk, output=args.output)


def cmd_forensics_scan_app(args: argparse.Namespace) -> None:
    """Pull an app's APK from the device by package name and run full forensic scan."""
    import tempfile
    from harness_android.forensics import full_apk_scan, extract_app_data, print_findings

    adb = ADB(serial=_find_serial(args))
    package = args.package

    # Resolve APK path on device
    console.print(f"[bold]Resolving APK path for {package} …")
    result = adb.shell("pm", "path", package)
    lines = [l.strip() for l in result.strip().splitlines() if l.strip().startswith("package:")]
    if not lines:
        console.print(f"[red]Package '{package}' not found on device.")
        console.print("[dim]Tip: use `harness-android shell pm list packages` to see installed packages.")
        return

    apk_remote = lines[0].replace("package:", "")
    console.print(f"[dim]Found: {apk_remote}")

    # Pull APK to temp file
    with tempfile.NamedTemporaryFile(suffix=".apk", delete=False, prefix=f"{package}_") as tmp:
        tmp_path = Path(tmp.name)
    console.print(f"[bold]Pulling APK …")
    adb.pull(apk_remote, tmp_path)

    # Run full APK scan (secrets + manifest)
    try:
        full_apk_scan(str(tmp_path), output=args.output)
    finally:
        tmp_path.unlink(missing_ok=True)

    # Also scan app data if requested
    if args.app_data:
        console.print(f"\n[bold]Also scanning app data …")
        _, data_findings = extract_app_data(adb, package)
        if data_findings:
            print_findings(data_findings)


def cmd_forensics_secrets(args: argparse.Namespace) -> None:
    from harness_android.forensics import scan_apk_secrets, print_findings
    findings = scan_apk_secrets(args.apk)
    print_findings(findings)
    if args.output:
        import json as _json
        from harness_android.forensics import _finding_to_dict
        with open(args.output, "w") as f:
            _json.dump([_finding_to_dict(x) for x in findings], f, indent=2)
        console.print(f"[green]Saved to {args.output}")


def cmd_forensics_manifest(args: argparse.Namespace) -> None:
    from harness_android.forensics import analyze_apk_manifest, print_findings
    findings = analyze_apk_manifest(args.apk)
    print_findings(findings)


def cmd_forensics_appdata(args: argparse.Namespace) -> None:
    from harness_android.forensics import extract_app_data, print_findings
    adb = ADB(serial=_find_serial(args))
    _, findings = extract_app_data(adb, args.package, local_dir=args.output_dir)
    print_findings(findings)
    if args.report:
        import json as _json
        from harness_android.forensics import _finding_to_dict
        with open(args.report, "w") as f:
            _json.dump([_finding_to_dict(x) for x in findings], f, indent=2)
        console.print(f"[green]Report saved to {args.report}")


def cmd_forensics_installed(args: argparse.Namespace) -> None:
    """Pull and scan all 3rd-party APKs from the device."""
    from harness_android.forensics import scan_apk_secrets, analyze_apk_manifest, print_findings

    adb = ADB(serial=_find_serial(args))
    # List 3rd-party packages
    output = adb.shell("pm", "list", "packages", "-3", "-f")
    all_findings = []
    for line in output.strip().splitlines():
        # Format: package:<path>=<name>
        line = line.strip()
        if not line.startswith("package:"):
            continue
        parts = line[len("package:"):].split("=", 1)
        if len(parts) != 2:
            continue
        apk_remote, pkg_name = parts
        console.print(f"\n[bold]Scanning {pkg_name} ({apk_remote}) …")

        # Pull APK to temp
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".apk", delete=False) as tmp:
            tmp_path = Path(tmp.name)
        try:
            adb.pull(apk_remote, tmp_path)
            findings = scan_apk_secrets(tmp_path) + analyze_apk_manifest(tmp_path)
            for f in findings:
                f.description = f"[{pkg_name}] {f.description}"
            all_findings.extend(findings)
        except Exception as exc:  # noqa: BLE001
            console.print(f"[red]Failed: {exc}")
        finally:
            tmp_path.unlink(missing_ok=True)

    console.print(f"\n[bold]{'='*60}")
    print_findings(all_findings)

    if args.output:
        import json as _json
        from harness_android.forensics import _finding_to_dict
        with open(args.output, "w") as f:
            _json.dump({
                "total_apps_scanned": len(output.strip().splitlines()),
                "total_findings": len(all_findings),
                "findings": [_finding_to_dict(x) for x in all_findings],
            }, f, indent=2)
        console.print(f"[green]Report saved to {args.output}")


# ======================================================================
# WebView sub-command handlers
# ======================================================================


def cmd_webview_list(args: argparse.Namespace) -> None:
    from harness_android.webview import enumerate_webviews, print_webviews
    adb = ADB(serial=_find_serial(args))
    webviews = enumerate_webviews(adb)
    print_webviews(webviews)


def cmd_webview_connect(args: argparse.Namespace) -> None:
    from harness_android.webview import connect_webview
    adb = ADB(serial=_find_serial(args))
    browser = connect_webview(adb, args.socket, local_port=args.port)
    console.print(f"[green]Connected to {args.socket} on localhost:{args.port}")

    # Enable CDP domains so Runtime.evaluate etc. work reliably.
    browser.enable_domains()

    # Navigate FIRST — the initial page (e.g. chrome://newtab) may not
    # have a functioning JS context.
    if args.navigate:
        browser.navigate(args.navigate)
        import time; time.sleep(3)  # let the page load

    # Now it's safe to query page info.
    try:
        title = browser.get_page_title()
        url = browser.get_page_url()
        console.print(f"Page: {title} — {url}")
    except Exception:  # noqa: BLE001
        console.print("[dim]Could not read page title (page may still be loading)")

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

    browser.close()


# ======================================================================
# Intent sub-command handlers
# ======================================================================


def cmd_intent_enumerate(args: argparse.Namespace) -> None:
    from harness_android.intents import IntentFuzzer
    adb = ADB(serial=_find_serial(args))
    fuzzer = IntentFuzzer(adb)
    components = fuzzer.enumerate_exported(args.package)
    fuzzer.print_components(components)


def cmd_intent_fuzz(args: argparse.Namespace) -> None:
    from harness_android.intents import IntentFuzzer
    adb = ADB(serial=_find_serial(args))
    fuzzer = IntentFuzzer(adb)
    results = fuzzer.fuzz_package(args.package)
    fuzzer.print_results(results)
    if args.output:
        fuzzer.dump_results(results, args.output)


# ======================================================================
# Logcat sub-command handlers
# ======================================================================


def cmd_logcat_crashes(args: argparse.Namespace) -> None:
    from harness_android.logcat import LogcatCapture
    adb = ADB(serial=_find_serial(args))
    logcat = LogcatCapture(adb)
    crashes = logcat.find_crashes(args.file)
    logcat.print_crashes(crashes)
    if args.output:
        logcat.dump_crashes(crashes, args.output)


def cmd_logcat_stream(args: argparse.Namespace) -> None:
    """Stream live logcat to terminal."""
    import subprocess
    from harness_android.config import get_adb
    adb_bin = str(get_adb())
    serial = _find_serial(args)

    # Clear the buffer first so we only see new logs
    clear_cmd = [adb_bin]
    if serial:
        clear_cmd += ["-s", serial]
    clear_cmd += ["logcat", "-c"]
    subprocess.run(clear_cmd, capture_output=True)

    cmd = [adb_bin]
    if serial:
        cmd += ["-s", serial]
    cmd += ["logcat", "-v", "threadtime"]
    if args.tag:
        cmd += ["-s", args.tag]
    if args.level:
        cmd += ["*:" + args.level.upper()]
    console.print(f"[bold]Streaming logcat (Ctrl+C to stop) …")
    try:
        subprocess.run(cmd)
    except KeyboardInterrupt:
        pass


def cmd_logcat_capture(args: argparse.Namespace) -> None:
    """Capture logcat, save to file, auto-scan for crashes."""
    import time
    from harness_android.logcat import LogcatCapture
    adb = ADB(serial=_find_serial(args))
    logcat = LogcatCapture(adb)
    logcat.start()
    duration = args.duration
    if duration > 0:
        console.print(f"[bold]Capturing logcat for {duration}s …")
        try:
            time.sleep(duration)
        except KeyboardInterrupt:
            pass
    else:
        console.print("[bold]Capturing logcat (Ctrl+C to stop) …")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
    path = logcat.stop(args.output)
    console.print(f"[green]Logcat saved to {path}")
    crashes = logcat.find_crashes(str(path))
    if crashes:
        logcat.print_crashes(crashes)
        console.print(f"[bold red]{len(crashes)} crash(es) detected!")
    else:
        console.print("[green]No crashes detected.")


# ======================================================================
# Parser construction
# ======================================================================


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="harness-android",
        description="Cross-platform Android emulator harness with remote control",
    )
    parser.add_argument(
        "-s", "--serial", default=None, help="ADB device serial (default: auto)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # ---- setup ----
    p = sub.add_parser("setup", help="Download SDK and system images")
    p.add_argument("--api", type=int, default=DEFAULT_API_LEVEL, help="API level")
    p.add_argument("--install-chromium", action="store_true",
                    help="Also install Chromium (debuggable) for CDP on API 35+")
    p.set_defaults(func=cmd_setup)

    # ---- install-chromium ----
    p = sub.add_parser("install-chromium", help="Download and install Chromium (debuggable) on emulator")
    p.set_defaults(func=cmd_install_chromium)

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
    p.add_argument("--ram", type=int, default=4096, help="RAM in MB (default: 4096)")
    p.add_argument("--wipe", action="store_true", help="Wipe user data + cold boot")
    p.add_argument("--cold-boot", action="store_true", help="Force cold boot, ignore saved snapshot")
    p.add_argument("--no-snapshot-save", action="store_true", help="Don't save snapshot on exit")
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

    # ---- proxy ----
    proxy_sub = sub.add_parser("proxy", help="HTTP proxy & traffic capture")
    psub = proxy_sub.add_subparsers(dest="proxy_cmd", required=True)

    p = psub.add_parser("enable", help="Set device HTTP proxy")
    p.add_argument("--host", default="10.0.2.2", help="Proxy host (default: host loopback)")
    p.add_argument("--port", type=int, default=8080, help="Proxy port")
    p.set_defaults(func=cmd_proxy_enable)

    p = psub.add_parser("disable", help="Remove device proxy")
    p.set_defaults(func=cmd_proxy_disable)

    p = psub.add_parser("status", help="Show current proxy setting")
    p.set_defaults(func=cmd_proxy_status)

    p = psub.add_parser("install-ca", help="Install CA certificate for TLS interception")
    p.add_argument("--cert", help="Path to CA cert (PEM)")
    p.add_argument("--mitmproxy", action="store_true", help="Auto-find mitmproxy CA")
    p.set_defaults(func=cmd_proxy_install_ca)

    p = psub.add_parser("tcpdump", help="Capture traffic with tcpdump")
    p.add_argument("--stop", action="store_true", help="Stop capture and pull pcap")
    p.add_argument("-o", "--output", default="capture.pcap", help="Local pcap output path")
    p.set_defaults(func=cmd_proxy_tcpdump)

    p = psub.add_parser("hosts", help="Manage /etc/hosts on device")
    p.add_argument("--add", help="Add entry: IP=hostname")
    p.add_argument("--reset", action="store_true", help="Reset to default")
    p.set_defaults(func=cmd_proxy_hosts)

    # ---- recon ----
    p = sub.add_parser("recon", help="Reconnaissance: fingerprint, spider, storage, CSP")
    p.add_argument("--url", "-u", help="Navigate to URL first")
    p.add_argument("--port", type=int, default=CDP_LOCAL_PORT, help="CDP port")
    p.add_argument("--full", action="store_true", help="Run all recon modules (default)")
    p.add_argument("--fingerprint", action="store_true", help="Tech fingerprint only")
    p.add_argument("--spider", action="store_true", help="Spider links/forms only")
    p.add_argument("--storage", action="store_true", help="Dump cookies/localStorage/sessionStorage")
    p.add_argument("--csp", action="store_true", help="CSP analysis only")
    p.add_argument("-o", "--output", help="Save report to JSON file")
    p.set_defaults(func=cmd_recon)

    # ---- hooks ----
    p = sub.add_parser("hooks", help="Install JS hooks to capture browser API calls")
    p.add_argument("--hooks", default="all",
                    help="Comma-separated: xhr,fetch,cookies,websocket,postmessage,console,storage,forms,all")
    p.add_argument("--url", "-u", help="Navigate to URL after installing hooks")
    p.add_argument("--port", type=int, default=CDP_LOCAL_PORT)
    p.add_argument("--wait", type=int, help="Collect for N seconds then exit")
    p.add_argument("-o", "--output", help="Save captured data to JSON")
    p.set_defaults(func=cmd_hooks)

    # ---- pentest ----
    pentest_sub = sub.add_parser("pentest", help="Pentest automation")
    ptsub = pentest_sub.add_subparsers(dest="pentest_cmd", required=True)

    p = ptsub.add_parser("run", help="Run a pentest script")
    p.add_argument("script", help="Path to Python script with run(ctx) function")
    p.add_argument("--port", type=int, default=CDP_LOCAL_PORT)
    p.add_argument("--report", "-r", help="Save report to JSON path")
    p.set_defaults(func=cmd_pentest_run)

    # ---- mojo ----
    mojo_sub = sub.add_parser("mojo", help="Mojo IPC tracing and testing")
    msub = mojo_sub.add_subparsers(dest="mojo_cmd", required=True)

    p = msub.add_parser("trace", help="Capture Mojo IPC trace")
    p.add_argument("--url", "-u", help="Navigate to URL first")
    p.add_argument("--port", type=int, default=CDP_LOCAL_PORT)
    p.add_argument("--trigger", action="store_true", help="Trigger all Mojo-backed Web APIs during trace")
    p.add_argument("--duration", type=int, help="Record for N seconds (default: 10, ignored with --trigger)")
    p.add_argument("--verbose", "-v", action="store_true", help="Enable verbose Mojo trace categories")
    p.add_argument("-o", "--output", help="Save analysis to JSON")
    p.add_argument("--chrome-trace", help="Save raw trace for chrome://tracing")
    p.set_defaults(func=cmd_mojo_trace)

    p = msub.add_parser("trigger", help="Trigger all Mojo-backed Web APIs and show results")
    p.add_argument("--url", "-u", help="Navigate to URL first")
    p.add_argument("--port", type=int, default=CDP_LOCAL_PORT)
    p.add_argument("-o", "--output", help="Save results to JSON")
    p.set_defaults(func=cmd_mojo_trigger)

    p = msub.add_parser("fuzz", help="Fuzz a Mojo-backed Web API")
    p.add_argument("api", help="API name (e.g. Clipboard.writeText, Permissions.query)")
    p.add_argument("--url", "-u", help="Navigate to URL first")
    p.add_argument("--port", type=int, default=CDP_LOCAL_PORT)
    p.add_argument("-o", "--output", help="Save results to JSON")
    p.set_defaults(func=cmd_mojo_fuzz)

    # ---- forensics ----
    forensics_sub = sub.add_parser("forensics", help="APK forensics and secret scanning")
    fsub = forensics_sub.add_subparsers(dest="forensics_cmd", required=True)

    p = fsub.add_parser("scan", help="Full APK scan: secrets + manifest (local, no emulator needed)")
    p.add_argument("apk", help="Path to .apk file")
    p.add_argument("-o", "--output", help="Save report to JSON")
    p.set_defaults(func=cmd_forensics_scan)

    p = fsub.add_parser("scan-app", help="Pull APK from device by package name and scan (emulator required)")
    p.add_argument("package", help="Package name (e.g. com.android.chrome)")
    p.add_argument("-o", "--output", help="Save report to JSON")
    p.add_argument("--app-data", action="store_true", help="Also scan app's private data")
    p.set_defaults(func=cmd_forensics_scan_app)

    p = fsub.add_parser("secrets", help="Scan APK for hardcoded secrets (local, no emulator needed)")
    p.add_argument("apk", help="Path to .apk file")
    p.add_argument("-o", "--output", help="Save findings to JSON")
    p.set_defaults(func=cmd_forensics_secrets)

    p = fsub.add_parser("manifest", help="Analyze AndroidManifest.xml security (local, no emulator needed)")
    p.add_argument("apk", help="Path to .apk file")
    p.set_defaults(func=cmd_forensics_manifest)

    p = fsub.add_parser("app-data", help="Pull and scan installed app's private data (emulator required)")
    p.add_argument("package", help="Package name (e.g. com.example.app)")
    p.add_argument("-o", "--output-dir", default="app_data", help="Local output directory")
    p.add_argument("--report", help="Save findings to JSON")
    p.set_defaults(func=cmd_forensics_appdata)

    p = fsub.add_parser("installed", help="Pull and scan all 3rd-party APKs (emulator required)")
    p.add_argument("-o", "--output", help="Save combined report to JSON")
    p.set_defaults(func=cmd_forensics_installed)

    # ---- webview ----
    webview_sub = sub.add_parser("webview", help="WebView enumeration and control")
    wvsub = webview_sub.add_subparsers(dest="webview_cmd", required=True)

    p = wvsub.add_parser("list", help="List all debuggable WebViews on device")
    p.set_defaults(func=cmd_webview_list)

    p = wvsub.add_parser("connect", help="Connect to a WebView by socket name")
    p.add_argument("socket", help="Socket name (from webview list)")
    p.add_argument("--port", type=int, default=9333, help="Local port to forward")
    p.add_argument("--navigate", help="Navigate to URL")
    p.add_argument("--js", help="Evaluate JavaScript expression")
    p.add_argument("--title", action="store_true", help="Print page title")
    p.add_argument("--page-screenshot", metavar="FILE", help="Save page screenshot")
    p.add_argument("--interactive", action="store_true", help="Interactive JS REPL")
    p.set_defaults(func=cmd_webview_connect)

    # ---- intents ----
    intent_sub = sub.add_parser("intent", help="Intent fuzzing")
    itsub = intent_sub.add_subparsers(dest="intent_cmd", required=True)

    p = itsub.add_parser("enumerate", help="List exported components of a package")
    p.add_argument("package", help="Package name")
    p.set_defaults(func=cmd_intent_enumerate)

    p = itsub.add_parser("fuzz", help="Fuzz exported components with smart payloads")
    p.add_argument("package", help="Package name")
    p.add_argument("-o", "--output", help="Save results to JSON")
    p.set_defaults(func=cmd_intent_fuzz)

    # ---- logcat ----
    logcat_sub = sub.add_parser("logcat", help="Logcat capture and crash detection")
    lcsub = logcat_sub.add_subparsers(dest="logcat_cmd", required=True)

    p = lcsub.add_parser("stream", help="Stream live logcat to terminal")
    p.add_argument("--tag", "-t", help="Filter by tag (e.g. chromium, ActivityManager)")
    p.add_argument("--level", "-l", help="Min log level: V, D, I, W, E, F")
    p.set_defaults(func=cmd_logcat_stream)

    p = lcsub.add_parser("capture", help="Capture logcat, auto-scan for crashes (ASan, SIGSEGV, ANR, etc.)")
    p.add_argument("--duration", "-d", type=int, default=30, help="Seconds to capture (default: 30, 0=until Ctrl+C)")
    p.add_argument("-o", "--output", default="logcat.txt", help="Save logcat to file")
    p.add_argument("--crashes-only", action="store_true", help="Only show detected crashes, not the full log")
    p.set_defaults(func=cmd_logcat_capture)

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
