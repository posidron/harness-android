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

from rich.table import Table

from harness_android.console import console

from harness_android.adb import ADB



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


# ----------------------------------------------------------------------
# Declarative payload corpus
# ----------------------------------------------------------------------
# Each entry is independent; add/remove rows without touching logic.
#
# Placeholders substituted per-run via str.format:
#   {authority}  – a content-provider authority of the target package,
#                  or "" if the package exposes none.
#   {package}    – the target package name.
#
# Entries whose am_args reference {authority} are *skipped* when the
# package has no discoverable authorities, instead of being fired at a
# dummy one.  Set ``needs_authority=True`` on such rows.
# ----------------------------------------------------------------------

_LARGE_JSON = json.dumps({"key": "value" * 500, "nested": {"a": list(range(200))}})[:8000]

_PAYLOAD_CORPUS: list[dict[str, Any]] = [
    # --- Null / missing intent data ---
    dict(name="null_action", bug_class="null_handling",
         description="Intent with no action or data — tests default handling",
         am_args=[]),

    # --- Path traversal via data URI ---
    dict(name="path_traversal_content", bug_class="path_traversal", needs_authority=True,
         description="Path traversal via content:// authority",
         am_args=["-d", "content://{authority}/../../../system/etc/hosts"]),
    dict(name="path_traversal_content_urlencoded", bug_class="path_traversal", needs_authority=True,
         description="URL-encoded path traversal via content:// authority",
         am_args=["-d", "content://{authority}/..%2F..%2F..%2Fsystem%2Fetc%2Fhosts"]),
    dict(name="path_traversal_app_db", bug_class="path_traversal",
         description="Direct file:// to the app's own private DB",
         am_args=["-d", "file:///data/data/{package}/databases/secret.db"]),
    dict(name="path_traversal_proc_cmdline", bug_class="path_traversal",
         description="file:///proc/self/cmdline",
         am_args=["-d", "file:///proc/self/cmdline"]),
    dict(name="path_traversal_urandom", bug_class="path_traversal",
         description="file:///dev/urandom (resource exhaustion)",
         am_args=["-d", "file:///dev/urandom"]),

    # --- SQL injection via content URI ---
    dict(name="sqli_tautology", bug_class="sql_injection", needs_authority=True,
         description="Classic tautology in content:// path",
         am_args=["-d", "content://{authority}/items/1' OR '1'='1"]),
    dict(name="sqli_stacked_drop", bug_class="sql_injection", needs_authority=True,
         description="Stacked-statement DROP",
         am_args=["-d", "content://{authority}/items/1; DROP TABLE users;--"]),
    dict(name="sqli_union_sqlite_master", bug_class="sql_injection", needs_authority=True,
         description="UNION SELECT against sqlite_master",
         am_args=["-d", "content://{authority}/items/1 UNION SELECT * FROM sqlite_master--"]),
    dict(name="sqli_projection_injection", bug_class="sql_injection", needs_authority=True,
         description="SQL in content URI selection parameter",
         am_args=["-d", "content://{authority}/items?selection=1=1"]),

    # --- Type confusion extras ---
    dict(name="type_confusion_int_as_string", bug_class="type_confusion",
         description="Send string where int expected (key 'id')",
         am_args=["--es", "id", "not_an_integer"]),
    dict(name="type_confusion_negative", bug_class="type_confusion",
         description="Negative integer for array index (key 'index')",
         am_args=["--ei", "index", "-1"]),
    dict(name="type_confusion_maxint", bug_class="type_confusion",
         description="Integer overflow (key 'count')",
         am_args=["--ei", "count", "2147483647"]),
    dict(name="type_confusion_zero", bug_class="type_confusion",
         description="Zero value for size/count (key 'size')",
         am_args=["--ei", "size", "0"]),

    # --- Unicode edge cases ---
    dict(name="unicode_rtl_override", bug_class="unicode",
         description="RTL override — can confuse UI rendering",
         am_args=["--es", "text", "\u202eevil_reversed"]),
    dict(name="unicode_null_byte", bug_class="unicode",
         description="Null byte in string — C string termination",
         am_args=["--es", "data", "before\x00after"]),
    dict(name="unicode_surrogates", bug_class="unicode",
         description="Lone UTF-16 surrogate — encoding edge case",
         am_args=["--es", "text", "\ud800"]),
    dict(name="unicode_overlong", bug_class="unicode",
         description="Multi-byte zero-width chars",
         am_args=["--es", "text", "\u200b\u200b\u200b\ufeff\u200d"]),

    # --- Format string ---
    dict(name="format_string_printf", bug_class="format_string",
         description="printf-style format specifiers",
         am_args=["--es", "message", "%s%s%s%s%s%n%n%n%n%n"]),
    dict(name="format_string_java", bug_class="format_string",
         description="Java String.format specifiers with high positional arg",
         am_args=["--es", "title", "%1$s %2$s %3$s %4$s %5$s %99$s"]),

    # --- Deep link scheme abuse ---
    dict(name="scheme_javascript", bug_class="scheme_abuse",
         description="javascript: URI in Intent data",
         am_args=["-d", "javascript:alert(1)"]),
    dict(name="scheme_data_html", bug_class="scheme_abuse",
         description="data: URI carrying <script>",
         am_args=["-d", "data:text/html,<script>alert(1)</script>"]),
    dict(name="scheme_intent_view", bug_class="scheme_abuse",
         description="Nested intent: URI (view action)",
         am_args=["-d", "intent:#Intent;action=android.intent.action.VIEW;end"]),
    dict(name="scheme_intent_settings", bug_class="scheme_abuse",
         description="Nested intent: URI pointing at system Settings",
         am_args=["-d", "intent:#Intent;component=com.android.settings/.Settings;end"]),

    # --- Empty / minimal data ---
    dict(name="empty_string_extra", bug_class="empty_input",
         description="Empty string for common extra names",
         am_args=["--es", "url", "", "--es", "data", "", "--es", "path", ""]),

    # --- Parcel size attack ---
    dict(name="large_parcel", bug_class="parcel_size",
         description="Large JSON-like string extra (~8KB) — Binder transaction limit",
         am_args=["--es", "payload", _LARGE_JSON]),

    # --- MIME type confusion ---
    dict(name="mime_confusion", bug_class="type_confusion",
         description="Unexpected MIME type",
         am_args=["-t", "application/x-executable", "-d", "file:///data/local/tmp/test"]),

    # --- Privileged action abuse ---
    dict(name="action_delete", bug_class="action_abuse",
         description="Privileged action: DELETE",
         am_args=["-a", "android.intent.action.DELETE"]),
    dict(name="action_factory_test", bug_class="action_abuse",
         description="Privileged action: FACTORY_TEST",
         am_args=["-a", "android.intent.action.FACTORY_TEST"]),
    dict(name="action_master_clear", bug_class="action_abuse",
         description="Privileged action: MASTER_CLEAR",
         am_args=["-a", "android.intent.action.MASTER_CLEAR"]),
]


def _format_arg(value: str, ctx: dict[str, str]) -> str:
    """Safely substitute {placeholder} tokens; leave other braces alone."""
    try:
        return value.format(**ctx)
    except (KeyError, IndexError):
        return value


def _build_payloads(
    package: str,
    authorities: list[str] | None = None,
) -> list[IntentPayload]:
    """Realise the declarative corpus against a concrete package.

    Parameters
    ----------
    package
        Target package name (substituted for ``{package}``).
    authorities
        Content-provider authorities discovered for the package.  The first
        entry is substituted for ``{authority}``.  When empty, rows marked
        ``needs_authority=True`` are skipped rather than fired at a fake
        authority.
    """
    authority = authorities[0] if authorities else ""
    ctx = {"authority": authority, "package": package}
    payloads: list[IntentPayload] = []
    for row in _PAYLOAD_CORPUS:
        if row.get("needs_authority") and not authority:
            continue
        payloads.append(IntentPayload(
            name=row["name"],
            description=_format_arg(row["description"], ctx),
            bug_class=row["bug_class"],
            am_args=[_format_arg(a, ctx) for a in row["am_args"]],
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
    pkg = component.split("/")[0]
    if payloads is None:
        # No authorities known at this entry point; rows that require one
        # are skipped by _build_payloads.
        payloads = _build_payloads(pkg, authorities=None)

    am_cmd = {"activity": "start", "service": "startservice", "receiver": "broadcast"}
    am_verb = am_cmd.get(component_type, "start")

    results: list[FuzzResult] = []
    console.print(f"[bold]Fuzzing {component} ({len(payloads)} payloads) …")

    for payload in payloads:
        pid_before = adb.pidof(pkg)

        cmd_args = ["am", am_verb, "-n", component, *payload.am_args]
        start = time.monotonic()
        result = adb.run("shell", *cmd_args, check=False, timeout=15)
        elapsed = (time.monotonic() - start) * 1000

        # Poll for process death/restart (don't trust a single 300ms sample
        # on a laggy emulator).
        crashed = False
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            pid_after = adb.pidof(pkg)
            if pid_before and (pid_after is None or pid_after != pid_before):
                crashed = True
                break
            time.sleep(0.2)

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

        if crashed:
            # Wait for the process to come back before the next iteration.
            restart_deadline = time.monotonic() + 5.0
            while time.monotonic() < restart_deadline and not adb.pidof(pkg):
                time.sleep(0.3)

    return results


def fuzz_package(adb: ADB, package: str) -> list[FuzzResult]:
    """Enumerate and fuzz all exported components of a package."""
    components = enumerate_exported(adb, package)
    print_components(components)

    if not components:
        console.print("[yellow]No exported components found to fuzz.")
        return []

    # Collect every content-provider authority discovered on the package
    # so content:// payloads hit a real URI, not a placeholder.
    authorities: list[str] = []
    for comp in components:
        if comp.component_type == "provider" and comp.authorities:
            for auth in comp.authorities.split(";"):
                auth = auth.strip()
                if auth and auth not in authorities:
                    authorities.append(auth)
    if authorities:
        console.print(f"[dim]Discovered authorities: {', '.join(authorities)}")
    else:
        console.print("[dim]No provider authorities found — content:// payloads will be skipped.")

    all_results: list[FuzzResult] = []
    for comp in components:
        full_name = f"{comp.package}/{comp.name}"
        if comp.name.startswith("."):
            full_name = f"{comp.package}/{comp.package}{comp.name}"

        payloads = _build_payloads(package, authorities=authorities)
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
