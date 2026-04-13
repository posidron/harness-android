"""Intent fuzzing: enumerate exported components and send targeted malformed intents.

The fuzz payloads are designed to trigger real bug classes, not just crash
with oversized data.  Each payload targets a specific vulnerability pattern:
type confusion, path traversal, SQL injection via content URIs, format strings,
serialization issues, null handling, and Unicode edge cases.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

from rich.console import Console
from rich.table import Table

from harness_android.adb import ADB

console = Console()


# ======================================================================
# Component enumeration
# ======================================================================

@dataclass
class ExportedComponent:
    """An exported Activity, Service, Receiver, or Provider."""
    component_type: str   # "activity", "service", "receiver", "provider"
    name: str             # fully qualified class name
    package: str
    intent_filters: list[dict[str, Any]] = field(default_factory=list)
    permission: str = ""
    authorities: str = ""  # for providers


def enumerate_exported(adb: ADB, package: str) -> list[ExportedComponent]:
    """Parse `dumpsys package` to find all exported components."""
    output = adb.shell("dumpsys", "package", package)
    components: list[ExportedComponent] = []

    # Parse activity/service/receiver/provider sections
    for comp_type in ("activity", "service", "receiver", "provider"):
        # Look for exported components in the dumpsys output
        # Format varies by Android version, but key markers are consistent
        pattern = rf"({package}/[\w.$]+).*?exported=(true)"
        for m in re.finditer(pattern, output, re.IGNORECASE):
            full_name = m.group(1)
            components.append(ExportedComponent(
                component_type=comp_type,
                name=full_name.split("/")[-1],
                package=package,
            ))

    # Also get exported activities from the manifest-style output
    in_section = ""
    current_comp = None
    for line in output.splitlines():
        stripped = line.strip()

        # Track which section we're in
        for ct in ("Activity", "Service", "Receiver", "Provider"):
            if stripped.startswith(f"{ct} Resolver Table:") or stripped.startswith(f"Registered {ct}"):
                in_section = ct.lower()

        # Look for component references with the target package
        if package in stripped and "/" in stripped:
            comp_match = re.search(rf"({re.escape(package)}/[\w.$]+)", stripped)
            if comp_match:
                full = comp_match.group(1)
                cls_name = full.split("/")[-1]
                if cls_name.startswith("."):
                    cls_name = package + cls_name
                # Avoid duplicates
                if not any(c.name == cls_name for c in components):
                    components.append(ExportedComponent(
                        component_type=in_section or "activity",
                        name=cls_name,
                        package=package,
                    ))

    # Get content provider authorities
    for comp in components:
        if comp.component_type == "provider":
            auth_match = re.search(
                rf"{re.escape(comp.name)}.*?authorities[=:](\S+)",
                output, re.IGNORECASE,
            )
            if auth_match:
                comp.authorities = auth_match.group(1).strip(";")

    return components


def print_components(components: list[ExportedComponent]) -> None:
    if not components:
        console.print("[dim]No exported components found.")
        return
    t = Table(title=f"Exported Components ({len(components)})")
    t.add_column("Type", style="bold")
    t.add_column("Name", overflow="fold")
    t.add_column("Permission")
    for c in components:
        t.add_row(c.component_type, f"{c.package}/{c.name}", c.permission or "[dim]none")
    console.print(t)


# ======================================================================
# Smart fuzz payloads
# ======================================================================

@dataclass
class IntentPayload:
    """A single intent fuzz test case."""
    name: str
    description: str
    bug_class: str
    am_args: list[str]     # arguments to pass after `am start -n <component>`


def _build_payloads(component: str) -> list[IntentPayload]:
    """Generate targeted fuzz payloads for a component.

    Each payload is designed to trigger a specific bug class, not just
    throw random data at the target.
    """
    payloads: list[IntentPayload] = []

    # --- Null / missing intent data ---
    payloads.append(IntentPayload(
        name="null_action",
        description="Intent with no action or data — tests default handling",
        bug_class="null_handling",
        am_args=[],
    ))

    # --- Path traversal via data URI ---
    for path in [
        "content://com.target/../../../system/etc/hosts",
        "file:///data/data/com.target/databases/secret.db",
        "content://com.target/..%2F..%2F..%2Fsystem%2Fetc%2Fhosts",
        "file:///proc/self/cmdline",
        "file:///dev/urandom",
    ]:
        payloads.append(IntentPayload(
            name=f"path_traversal_{len(payloads)}",
            description=f"Path traversal: {path}",
            bug_class="path_traversal",
            am_args=["-d", path],
        ))

    # --- SQL injection via content URI ---
    for uri in [
        "content://com.target/items/1' OR '1'='1",
        "content://com.target/items/1; DROP TABLE users;--",
        "content://com.target/items/1 UNION SELECT * FROM sqlite_master--",
    ]:
        payloads.append(IntentPayload(
            name=f"sqli_{len(payloads)}",
            description=f"SQL injection in content URI",
            bug_class="sql_injection",
            am_args=["-d", uri],
        ))

    # --- Type confusion extras ---
    # Send wrong types for common extra names
    payloads.append(IntentPayload(
        name="type_confusion_int_as_string",
        description="Send string where int expected (key: 'id')",
        bug_class="type_confusion",
        am_args=["--es", "id", "not_an_integer"],
    ))
    payloads.append(IntentPayload(
        name="type_confusion_negative",
        description="Negative integer for array index (key: 'index')",
        bug_class="type_confusion",
        am_args=["--ei", "index", "-1"],
    ))
    payloads.append(IntentPayload(
        name="type_confusion_maxint",
        description="Integer overflow (key: 'count')",
        bug_class="type_confusion",
        am_args=["--ei", "count", "2147483647"],
    ))
    payloads.append(IntentPayload(
        name="type_confusion_zero",
        description="Zero value for size/count (key: 'size')",
        bug_class="type_confusion",
        am_args=["--ei", "size", "0"],
    ))

    # --- Unicode edge cases ---
    payloads.append(IntentPayload(
        name="unicode_rtl_override",
        description="RTL override character — can confuse UI rendering",
        bug_class="unicode",
        am_args=["--es", "text", "\u202eevil_reversed"],
    ))
    payloads.append(IntentPayload(
        name="unicode_null_byte",
        description="Null byte in string — C string termination",
        bug_class="unicode",
        am_args=["--es", "data", "before\x00after"],
    ))
    payloads.append(IntentPayload(
        name="unicode_surrogates",
        description="Lone UTF-16 surrogate — encoding edge case",
        bug_class="unicode",
        am_args=["--es", "text", "\ud800"],
    ))
    payloads.append(IntentPayload(
        name="unicode_overlong",
        description="Multi-byte zero-width chars",
        bug_class="unicode",
        am_args=["--es", "text", "\u200b\u200b\u200b\ufeff\u200d"],
    ))

    # --- Format string ---
    payloads.append(IntentPayload(
        name="format_string",
        description="Format string specifiers — crashes if used in printf-style",
        bug_class="format_string",
        am_args=["--es", "message", "%s%s%s%s%s%n%n%n%n%n"],
    ))
    payloads.append(IntentPayload(
        name="format_string_log",
        description="Format string for Java String.format",
        bug_class="format_string",
        am_args=["--es", "title", "%1$s %2$s %3$s %4$s %5$s %99$s"],
    ))

    # --- Deep link scheme abuse ---
    for scheme in [
        "javascript:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "intent:#Intent;action=android.intent.action.VIEW;end",
        "intent:#Intent;component=com.android.settings/.Settings;end",
    ]:
        payloads.append(IntentPayload(
            name=f"scheme_abuse_{len(payloads)}",
            description=f"Malicious scheme: {scheme[:50]}",
            bug_class="scheme_abuse",
            am_args=["-d", scheme],
        ))

    # --- Empty / minimal data ---
    payloads.append(IntentPayload(
        name="empty_string_extra",
        description="Empty string for all common extra names",
        bug_class="empty_input",
        am_args=["--es", "url", "", "--es", "data", "", "--es", "path", ""],
    ))

    # --- Parcel size attack ---
    # Large but realistic string (not just "A"*N, but JSON-like structure)
    large_json = json.dumps({"key": "value" * 500, "nested": {"a": list(range(200))}})
    payloads.append(IntentPayload(
        name="large_parcel",
        description="Large JSON-like string extra (~10KB) — Binder transaction limit",
        bug_class="parcel_size",
        am_args=["--es", "payload", large_json[:8000]],
    ))

    # --- Content provider projection injection ---
    payloads.append(IntentPayload(
        name="projection_injection",
        description="SQL in content URI selection parameter",
        bug_class="sql_injection",
        am_args=["-d", "content://com.target/items?selection=1=1"],
    ))

    # --- MIME type confusion ---
    payloads.append(IntentPayload(
        name="mime_confusion",
        description="Unexpected MIME type",
        bug_class="type_confusion",
        am_args=["-t", "application/x-executable", "-d", "file:///data/local/tmp/test"],
    ))

    # --- Action abuse ---
    for action in [
        "android.intent.action.DELETE",
        "android.intent.action.FACTORY_TEST",
        "android.intent.action.MASTER_CLEAR",
    ]:
        payloads.append(IntentPayload(
            name=f"action_{action.split('.')[-1].lower()}",
            description=f"Privileged action: {action}",
            bug_class="action_abuse",
            am_args=["-a", action],
        ))

    return payloads


# ======================================================================
# Fuzzer
# ======================================================================

@dataclass
class FuzzResult:
    payload_name: str
    bug_class: str
    component: str
    crashed: bool = False
    error_output: str = ""
    duration_ms: float = 0


def fuzz_component(
    adb: ADB,
    component: str,
    payloads: list[IntentPayload] | None = None,
    component_type: str = "activity",
) -> list[FuzzResult]:
    """Fuzz a single exported component with all payloads.

    Parameters
    ----------
    component : "package/class" format
    component_type : "activity", "service", "receiver"
    """
    if payloads is None:
        payloads = _build_payloads(component)

    am_cmd = {"activity": "start", "service": "startservice", "receiver": "broadcast"}
    am_verb = am_cmd.get(component_type, "start")

    results: list[FuzzResult] = []
    console.print(f"[bold]Fuzzing {component} ({len(payloads)} payloads) …")

    for payload in payloads:
        # Check if target process is alive before
        pkg = component.split("/")[0]
        pid_before = adb.run("shell", f"pidof {pkg}", check=False, timeout=5).stdout.strip()

        # Send the intent
        cmd_args = ["am", am_verb, "-n", component] + payload.am_args
        start = time.monotonic()
        result = adb.run("shell", *cmd_args, check=False, timeout=10)
        elapsed = (time.monotonic() - start) * 1000

        # Small delay for crash to register
        time.sleep(0.3)

        # Check if process is still alive
        pid_after = adb.run("shell", f"pidof {pkg}", check=False, timeout=5).stdout.strip()
        crashed = bool(pid_before) and not bool(pid_after)

        error = ""
        if result.returncode != 0:
            error = result.stderr.strip() or result.stdout.strip()
        if "Error" in result.stdout or "Exception" in result.stdout:
            error = result.stdout.strip()

        status = "[red]CRASH" if crashed else ("[yellow]ERR" if error else "[green]OK")
        console.print(f"  {status}[/]  {payload.name:35s}  {payload.bug_class}")

        results.append(FuzzResult(
            payload_name=payload.name,
            bug_class=payload.bug_class,
            component=component,
            crashed=crashed,
            error_output=error[:500],
            duration_ms=elapsed,
        ))

        # If crashed, wait for restart
        if crashed:
            time.sleep(2)

    return results


def fuzz_package(adb: ADB, package: str) -> list[FuzzResult]:
    """Enumerate and fuzz all exported components of a package."""
    components = enumerate_exported(adb, package)
    print_components(components)

    if not components:
        console.print("[yellow]No exported components found to fuzz.")
        return []

    all_results: list[FuzzResult] = []
    for comp in components:
        full_name = f"{comp.package}/{comp.name}"
        if comp.name.startswith("."):
            full_name = f"{comp.package}/{comp.package}{comp.name}"

        payloads = _build_payloads(full_name)
        results = fuzz_component(
            adb, full_name, payloads,
            component_type=comp.component_type,
        )
        all_results.extend(results)

    return all_results


def print_fuzz_results(results: list[FuzzResult]) -> None:
    if not results:
        return

    crashes = [r for r in results if r.crashed]
    errors = [r for r in results if r.error_output and not r.crashed]

    if crashes:
        t = Table(title=f"[red bold]Crashes ({len(crashes)})", show_lines=True)
        t.add_column("Payload", style="bold")
        t.add_column("Bug Class")
        t.add_column("Component")
        t.add_column("Error", overflow="fold")
        for r in crashes:
            t.add_row(r.payload_name, r.bug_class, r.component, r.error_output[:100])
        console.print(t)

    console.print(
        f"\n[bold]Summary: {len(results)} payloads, "
        f"[red]{len(crashes)} crashes[/], "
        f"[yellow]{len(errors)} errors[/], "
        f"[green]{len(results) - len(crashes) - len(errors)} ok[/]"
    )
