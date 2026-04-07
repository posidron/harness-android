"""APK forensics: secret scanning, manifest audit, on-device app data extraction.

Scans APK files and installed app data for hardcoded secrets (API keys,
tokens, passwords, private keys) and manifest security misconfigurations.

No external tools required — APKs are ZIP files and the binary
AndroidManifest.xml is decoded with a minimal parser.
"""

from __future__ import annotations

import io
import json
import re
import sqlite3
import struct
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from rich.console import Console
from rich.table import Table

from harness_android.adb import ADB

console = Console()


# ======================================================================
# Secret patterns — regex + severity
# ======================================================================

@dataclass
class SecretPattern:
    name: str
    pattern: re.Pattern[str]
    severity: str = "high"


SECRET_PATTERNS: list[SecretPattern] = [
    SecretPattern("AWS Access Key", re.compile(r"AKIA[0-9A-Z]{16}"), "critical"),
    SecretPattern("AWS Secret Key", re.compile(r"(?i)aws[_\-]?secret[_\-]?access[_\-]?key[\s:=\"']+([A-Za-z0-9/+=]{40})"), "critical"),
    SecretPattern("Google API Key", re.compile(r"AIza[0-9A-Za-z\-_]{35}"), "high"),
    SecretPattern("Google OAuth Token", re.compile(r"ya29\.[0-9A-Za-z\-_]+"), "high"),
    SecretPattern("Firebase URL", re.compile(r"https://[a-z0-9-]+\.firebaseio\.com"), "medium"),
    SecretPattern("Firebase API Key", re.compile(r"(?i)firebase[_\-]?api[_\-]?key[\s:=\"']+([A-Za-z0-9\-_]{20,})"), "high"),
    SecretPattern("GitHub Token", re.compile(r"gh[pousr]_[A-Za-z0-9_]{36,}"), "critical"),
    SecretPattern("GitHub Classic Token", re.compile(r"ghp_[A-Za-z0-9]{36}"), "critical"),
    SecretPattern("Slack Token", re.compile(r"xox[baprs]-[0-9]{10,}-[A-Za-z0-9-]+"), "critical"),
    SecretPattern("Slack Webhook", re.compile(r"https://hooks\.slack\.com/services/T[A-Z0-9]+/B[A-Z0-9]+/[A-Za-z0-9]+"), "high"),
    SecretPattern("Stripe Secret Key", re.compile(r"sk_live_[0-9a-zA-Z]{24,}"), "critical"),
    SecretPattern("Stripe Publishable Key", re.compile(r"pk_live_[0-9a-zA-Z]{24,}"), "medium"),
    SecretPattern("Twilio API Key", re.compile(r"SK[0-9a-fA-F]{32}"), "high"),
    SecretPattern("SendGrid API Key", re.compile(r"SG\.[A-Za-z0-9\-_]{22,}\.[A-Za-z0-9\-_]{43,}"), "high"),
    SecretPattern("Mailgun API Key", re.compile(r"key-[0-9a-zA-Z]{32}"), "high"),
    SecretPattern("Square Access Token", re.compile(r"sq0atp-[0-9A-Za-z\-_]{22,}"), "high"),
    SecretPattern("Square OAuth Secret", re.compile(r"sq0csp-[0-9A-Za-z\-_]{43,}"), "high"),
    SecretPattern("JWT Token", re.compile(r"eyJ[A-Za-z0-9\-_]+\.eyJ[A-Za-z0-9\-_]+\.[A-Za-z0-9\-_]+"), "high"),
    SecretPattern("Private Key (PEM)", re.compile(r"-----BEGIN (?:RSA |EC |DSA )?PRIVATE KEY-----"), "critical"),
    SecretPattern("Generic API Key", re.compile(r"(?i)(?:api[_\-]?key|apikey|api[_\-]?secret)[\s]*[=:]\s*[\"']([A-Za-z0-9\-_]{16,})[\"']"), "medium"),
    SecretPattern("Generic Secret", re.compile(r"(?i)(?:secret|password|passwd|pwd|token|auth[_\-]?token)[\s]*[=:]\s*[\"']([^\s\"']{8,})[\"']"), "medium"),
    SecretPattern("Bearer Token", re.compile(r"(?i)bearer\s+[A-Za-z0-9\-_.~+/]+=*"), "high"),
    SecretPattern("Base64 Encoded Key", re.compile(r"(?i)(?:key|secret|password|token)[\s]*[=:]\s*[\"']([A-Za-z0-9+/]{40,}={0,2})[\"']"), "medium"),
    SecretPattern("Azure Storage Key", re.compile(r"(?i)DefaultEndpointsProtocol=https?;AccountName=[^;]+;AccountKey=[A-Za-z0-9+/=]{44,}"), "critical"),
    SecretPattern("Azure Connection String", re.compile(r"(?i)(?:AccountKey|SharedAccessKey)=[A-Za-z0-9+/=]{44,}"), "high"),
    SecretPattern("Hardcoded IP Address", re.compile(r"\b(?:10|172\.(?:1[6-9]|2\d|3[01])|192\.168)\.\d{1,3}\.\d{1,3}\b"), "low"),
    SecretPattern("Hardcoded URL with Credentials", re.compile(r"https?://[^:]+:[^@]+@[^\s\"']+"), "critical"),
]


# ======================================================================
# Findings
# ======================================================================

@dataclass
class ForensicFinding:
    category: str          # "secret", "manifest", "appdata"
    severity: str          # "critical", "high", "medium", "low", "info"
    title: str
    description: str = ""
    evidence: str = ""
    file_path: str = ""
    line_number: int = 0


# ======================================================================
# APK string extraction
# ======================================================================

def _extract_strings_from_bytes(data: bytes, min_length: int = 8) -> list[str]:
    """Extract printable ASCII strings from raw bytes."""
    result = re.findall(rb"[\x20-\x7e]{%d,}" % min_length, data)
    return [s.decode("ascii", errors="ignore") for s in result]


def _extract_apk_strings(apk_path: Path) -> dict[str, list[str]]:
    """Extract strings from all files in an APK, grouped by entry name."""
    strings_by_file: dict[str, list[str]] = {}
    with zipfile.ZipFile(apk_path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            # Skip images, audio, video
            if any(info.filename.lower().endswith(ext) for ext in (
                ".png", ".jpg", ".jpeg", ".gif", ".webp", ".mp3", ".mp4",
                ".ogg", ".wav", ".ttf", ".otf", ".woff", ".woff2",
            )):
                continue
            try:
                data = zf.read(info)
                strings = _extract_strings_from_bytes(data)
                if strings:
                    strings_by_file[info.filename] = strings
            except Exception:  # noqa: BLE001
                pass
    return strings_by_file


# ======================================================================
# Secret scanner
# ======================================================================

def scan_strings_for_secrets(
    strings_by_file: dict[str, list[str]],
) -> list[ForensicFinding]:
    """Scan extracted strings against secret patterns."""
    findings: list[ForensicFinding] = []
    for filepath, strings in strings_by_file.items():
        for s in strings:
            for pat in SECRET_PATTERNS:
                match = pat.pattern.search(s)
                if match:
                    matched = match.group(0)
                    findings.append(ForensicFinding(
                        category="secret",
                        severity=pat.severity,
                        title=pat.name,
                        description=f"Found in {filepath}",
                        evidence=matched,
                        file_path=filepath,
                    ))
    return findings


def scan_apk_secrets(apk_path: str | Path) -> list[ForensicFinding]:
    """Scan an APK file for hardcoded secrets."""
    apk_path = Path(apk_path)
    if not apk_path.exists():
        raise FileNotFoundError(f"APK not found: {apk_path}")
    console.print(f"[bold]Scanning {apk_path.name} for secrets …")
    strings = _extract_apk_strings(apk_path)
    total_strings = sum(len(v) for v in strings.values())
    console.print(f"[dim]Extracted {total_strings} strings from {len(strings)} files")
    findings = scan_strings_for_secrets(strings)
    console.print(f"[green]Found {len(findings)} potential secrets")
    return findings


# ======================================================================
# Manifest analysis (binary XML)
# ======================================================================

_MANIFEST_ISSUES = {
    "debuggable": ("android:debuggable=true", "critical",
                   "App is debuggable — allows attaching a debugger in production"),
    "allowBackup": ("android:allowBackup=true", "high",
                    "App data can be backed up via adb backup — data extraction risk"),
    "usesCleartextTraffic": ("android:usesCleartextTraffic=true", "high",
                             "App allows cleartext HTTP traffic"),
    "testOnly": ("android:testOnly=true", "medium",
                 "App is marked as test-only"),
}


def _try_parse_manifest_text(manifest_text: str) -> list[ForensicFinding]:
    """Analyze a text AndroidManifest.xml for security issues."""
    findings: list[ForensicFinding] = []

    # Check application attributes
    for attr, (title, severity, desc) in _MANIFEST_ISSUES.items():
        if re.search(rf'android:{attr}\s*=\s*"true"', manifest_text):
            findings.append(ForensicFinding(
                category="manifest", severity=severity,
                title=title, description=desc,
                file_path="AndroidManifest.xml",
            ))

    # Exported components without permission
    for tag in ("activity", "service", "receiver", "provider"):
        pattern = rf"<{tag}\b[^>]*android:exported\s*=\s*\"true\"[^>]*>"
        for match in re.finditer(pattern, manifest_text):
            block = match.group(0)
            name_match = re.search(r'android:name\s*=\s*"([^"]+)"', block)
            name = name_match.group(1) if name_match else "unknown"
            has_perm = "android:permission" in block
            if not has_perm:
                findings.append(ForensicFinding(
                    category="manifest", severity="high",
                    title=f"Exported {tag} without permission: {name}",
                    description=f"<{tag}> is exported but has no android:permission set",
                    file_path="AndroidManifest.xml",
                ))

    # Dangerous permissions
    dangerous = [
        "CAMERA", "RECORD_AUDIO", "READ_CONTACTS", "WRITE_CONTACTS",
        "ACCESS_FINE_LOCATION", "ACCESS_COARSE_LOCATION", "READ_PHONE_STATE",
        "READ_EXTERNAL_STORAGE", "WRITE_EXTERNAL_STORAGE", "SEND_SMS",
        "READ_SMS", "RECEIVE_SMS", "READ_CALL_LOG", "WRITE_CALL_LOG",
        "ACCESS_BACKGROUND_LOCATION", "BODY_SENSORS",
    ]
    used_perms = re.findall(r'<uses-permission\s+android:name="android\.permission\.(\w+)"', manifest_text)
    for perm in used_perms:
        if perm in dangerous:
            findings.append(ForensicFinding(
                category="manifest", severity="info",
                title=f"Dangerous permission: {perm}",
                description=f"App requests android.permission.{perm}",
                file_path="AndroidManifest.xml",
            ))

    # Deep links / intent filters
    schemes = re.findall(r'<data\s+[^>]*android:scheme\s*=\s*"([^"]+)"', manifest_text)
    for scheme in schemes:
        if scheme not in ("http", "https"):
            findings.append(ForensicFinding(
                category="manifest", severity="info",
                title=f"Custom URI scheme: {scheme}://",
                description="Custom deep link handler — potential for URI hijacking",
                file_path="AndroidManifest.xml",
            ))

    return findings


def analyze_apk_manifest(apk_path: str | Path) -> list[ForensicFinding]:
    """Extract and analyze the AndroidManifest.xml from an APK."""
    apk_path = Path(apk_path)
    console.print(f"[bold]Analyzing manifest in {apk_path.name} …")

    with zipfile.ZipFile(apk_path) as zf:
        # Try to read AndroidManifest.xml
        try:
            manifest_data = zf.read("AndroidManifest.xml")
        except KeyError:
            return [ForensicFinding(
                category="manifest", severity="info",
                title="No AndroidManifest.xml found",
                description="APK does not contain AndroidManifest.xml",
            )]

    # Binary XML — extract strings as fallback
    # Try to decode as UTF-8 first (sometimes it's text)
    try:
        manifest_text = manifest_data.decode("utf-8")
        if "<manifest" in manifest_text:
            return _try_parse_manifest_text(manifest_text)
    except UnicodeDecodeError:
        pass

    # Binary manifest — extract what we can from the string pool
    strings = _extract_strings_from_bytes(manifest_data, min_length=4)
    # Reconstruct a searchable blob
    blob = " ".join(strings)

    findings: list[ForensicFinding] = []

    # Check for telltale strings in binary manifest
    if "true" in strings:
        for attr in ("debuggable", "allowBackup", "usesCleartextTraffic", "testOnly"):
            if attr in strings:
                title, severity, desc = _MANIFEST_ISSUES[attr]
                findings.append(ForensicFinding(
                    category="manifest", severity=severity,
                    title=f"{title} (probable — binary manifest)",
                    description=desc,
                    file_path="AndroidManifest.xml",
                ))

    # Exported components
    for tag in ("activity", "service", "receiver", "provider"):
        if tag in strings and "exported" in blob.lower():
            findings.append(ForensicFinding(
                category="manifest", severity="medium",
                title=f"Exported {tag} detected (binary manifest — verify manually)",
                file_path="AndroidManifest.xml",
            ))

    # Permissions from binary manifest
    dangerous = {"CAMERA", "RECORD_AUDIO", "READ_CONTACTS", "ACCESS_FINE_LOCATION",
                 "READ_PHONE_STATE", "READ_EXTERNAL_STORAGE", "SEND_SMS", "READ_SMS"}
    for s in strings:
        for perm in dangerous:
            if perm in s:
                findings.append(ForensicFinding(
                    category="manifest", severity="info",
                    title=f"Dangerous permission: {perm}",
                    file_path="AndroidManifest.xml",
                ))

    return findings


# ======================================================================
# On-device app data extraction
# ======================================================================

def extract_app_data(
    adb: ADB,
    package: str,
    local_dir: str = "app_data",
) -> tuple[Path, list[ForensicFinding]]:
    """Pull an app's private data from the emulator and scan for secrets.

    The emulator runs as root so we can access /data/data/<package>/.
    """
    local_path = Path(local_dir) / package
    local_path.mkdir(parents=True, exist_ok=True)
    remote_base = f"/data/data/{package}"

    console.print(f"[bold]Extracting data for {package} …")

    # Ensure root access (emulator only)
    adb.run("root", check=False, timeout=10)
    import time; time.sleep(2)

    # Check if package exists
    result = adb.run("shell", f"ls {remote_base}", check=False)
    if result.returncode != 0:
        console.print(f"[red]Package {package} not found or not accessible on device")
        return local_path, []

    # Pull the whole directory
    adb.run("pull", remote_base, str(local_path), check=False, timeout=120)

    # Scan shared_prefs XML files
    findings: list[ForensicFinding] = []

    # Scan all pulled files for secrets
    console.print("[dim]Scanning extracted files for secrets …")
    strings_by_file: dict[str, list[str]] = {}
    for f in local_path.rglob("*"):
        if f.is_file() and f.stat().st_size < 10_000_000:  # skip files > 10MB
            try:
                data = f.read_bytes()
                strings = _extract_strings_from_bytes(data, min_length=6)
                if strings:
                    rel = str(f.relative_to(local_path))
                    strings_by_file[rel] = strings
            except Exception:  # noqa: BLE001
                pass

    secret_findings = scan_strings_for_secrets(strings_by_file)
    for sf in secret_findings:
        sf.category = "appdata"
    findings.extend(secret_findings)

    # Scan SQLite databases for interesting tables
    for db_file in local_path.rglob("*.db"):
        try:
            db_findings = _scan_sqlite(db_file, package)
            findings.extend(db_findings)
        except Exception:  # noqa: BLE001
            pass

    console.print(f"[green]Extracted to {local_path} — {len(findings)} findings")
    return local_path, findings


def _scan_sqlite(db_path: Path, package: str) -> list[ForensicFinding]:
    """Scan a SQLite database for sensitive tables/data."""
    findings: list[ForensicFinding] = []
    interesting_tables = {
        "cookies", "logins", "passwords", "credentials", "tokens",
        "accounts", "sessions", "auth", "keys", "secrets",
    }

    try:
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [row[0] for row in cursor.fetchall()]

        for table in tables:
            table_lower = table.lower()
            if any(kw in table_lower for kw in interesting_tables):
                # Count rows
                try:
                    cursor.execute(f'SELECT COUNT(*) FROM "{table}"')
                    count = cursor.fetchone()[0]
                    if count > 0:
                        findings.append(ForensicFinding(
                            category="appdata",
                            severity="high",
                            title=f"Sensitive table: {table} ({count} rows)",
                            description=f"Database {db_path.name} contains table '{table}'",
                            file_path=str(db_path.relative_to(db_path.parent.parent)),
                        ))
                except Exception:  # noqa: BLE001
                    pass

            # Scan all text columns for secrets
            try:
                cursor.execute(f'SELECT * FROM "{table}" LIMIT 100')
                col_names = [desc[0] for desc in cursor.description] if cursor.description else []
                for row in cursor.fetchall():
                    for i, val in enumerate(row):
                        if isinstance(val, str) and len(val) > 8:
                            for pat in SECRET_PATTERNS:
                                m = pat.pattern.search(val)
                                if m:
                                    col = col_names[i] if i < len(col_names) else f"col{i}"
                                    matched = m.group(0)
                                    findings.append(ForensicFinding(
                                        category="appdata",
                                        severity=pat.severity,
                                        title=f"{pat.name} in DB {db_path.name}.{table}.{col}",
                                        evidence=matched,
                                        file_path=str(db_path.name),
                                    ))
            except Exception:  # noqa: BLE001
                pass

        conn.close()
    except Exception:  # noqa: BLE001
        pass

    return findings


# ======================================================================
# Full scan
# ======================================================================

def full_apk_scan(apk_path: str | Path, output: str | None = None) -> dict[str, Any]:
    """Run full forensic scan: secrets + manifest analysis."""
    apk_path = Path(apk_path)
    console.print(f"\n[bold]Full forensic scan of {apk_path.name}\n")

    secret_findings = scan_apk_secrets(apk_path)
    manifest_findings = analyze_apk_manifest(apk_path)

    all_findings = secret_findings + manifest_findings

    print_findings(all_findings)

    report = {
        "apk": str(apk_path),
        "total_findings": len(all_findings),
        "by_severity": _count_by_severity(all_findings),
        "findings": [_finding_to_dict(f) for f in all_findings],
    }

    if output:
        with open(output, "w") as f:
            json.dump(report, f, indent=2)
        console.print(f"\n[green]Report saved to {output}")

    return report


# ======================================================================
# Output helpers
# ======================================================================

_SEVERITY_COLORS = {
    "critical": "red bold",
    "high": "red",
    "medium": "yellow",
    "low": "cyan",
    "info": "dim",
}


def print_findings(findings: list[ForensicFinding]) -> None:
    if not findings:
        console.print("[green]No findings.")
        return

    t = Table(title=f"Forensic Findings ({len(findings)})", show_lines=True)
    t.add_column("Sev", style="bold", width=8)
    t.add_column("Category", width=10)
    t.add_column("Title", max_width=40)
    t.add_column("Evidence", overflow="fold")
    t.add_column("File", max_width=35)

    for f in sorted(findings, key=lambda x: ["critical", "high", "medium", "low", "info"].index(x.severity)):
        color = _SEVERITY_COLORS.get(f.severity, "")
        t.add_row(
            f"[{color}]{f.severity.upper()}[/]",
            f.category,
            f.title,
            f.evidence or "",
            f.file_path or "",
        )
    console.print(t)

    # Summary
    counts = _count_by_severity(findings)
    parts = []
    for sev in ("critical", "high", "medium", "low", "info"):
        c = counts.get(sev, 0)
        if c:
            color = _SEVERITY_COLORS[sev]
            parts.append(f"[{color}]{c} {sev}[/]")
    console.print("Summary: " + " · ".join(parts))


def _count_by_severity(findings: list[ForensicFinding]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for f in findings:
        counts[f.severity] = counts.get(f.severity, 0) + 1
    return counts


def _finding_to_dict(f: ForensicFinding) -> dict[str, Any]:
    return {
        "category": f.category,
        "severity": f.severity,
        "title": f.title,
        "description": f.description,
        "evidence": f.evidence,
        "file_path": f.file_path,
    }
