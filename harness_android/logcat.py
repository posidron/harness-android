"""Logcat capture and crash/sanitizer detection."""

from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.table import Table

from harness_android.adb import ADB

console = Console()


@dataclass
class CrashEvent:
    """A detected crash or sanitizer report from logcat."""
    event_type: str        # "java_exception", "native_crash", "anr", "asan", "fdsan", "abort"
    severity: str          # "critical", "high", "medium"
    message: str
    pid: str = ""
    process: str = ""
    timestamp: str = ""
    stacktrace: str = ""


# Patterns that indicate crashes/sanitizer issues in logcat
_CRASH_PATTERNS: list[tuple[str, str, re.Pattern[str]]] = [
    # Java fatal exceptions
    ("java_exception", "critical", re.compile(
        r"FATAL EXCEPTION:\s*(.+)", re.IGNORECASE)),
    # Native signal crashes
    ("native_crash", "critical", re.compile(
        r"signal\s+\d+\s+\(SIG(?:SEGV|ABRT|BUS|FPE|ILL|TRAP)\).*fault addr")),
    # AddressSanitizer
    ("asan", "critical", re.compile(
        r"==\d+==ERROR:\s*AddressSanitizer:\s*(.+)")),
    ("asan", "critical", re.compile(
        r"AddressSanitizer:\s*(heap-buffer-overflow|stack-buffer-overflow|"
        r"heap-use-after-free|stack-use-after-return|"
        r"use-after-poison|double-free|alloc-dealloc-mismatch|"
        r"attempting free on address|"
        r"global-buffer-overflow|stack-overflow|"
        r"use-after-scope|initialization-order-fiasco|"
        r"new-delete-type-mismatch|odr-violation|"
        r"SEGV on unknown address)")),
    # ASan shadow/summary lines
    ("asan", "critical", re.compile(
        r"SUMMARY:\s*AddressSanitizer:\s*(.+)")),
    # MemorySanitizer
    ("msan", "critical", re.compile(
        r"==\d+==WARNING:\s*MemorySanitizer:\s*(.+)")),
    # UndefinedBehaviorSanitizer
    ("ubsan", "high", re.compile(
        r"runtime error:\s*(.+)")),
    # ThreadSanitizer
    ("tsan", "high", re.compile(
        r"==\d+==WARNING:\s*ThreadSanitizer:\s*(.+)")),
    # fdsan (file descriptor sanitizer)
    ("fdsan", "high", re.compile(
        r"fdsan:\s*(.+)")),
    # ANR
    ("anr", "high", re.compile(
        r"ANR in\s+(\S+)")),
    # Tombstone reference
    ("native_crash", "critical", re.compile(
        r"Tombstone written to:\s*(.+)")),
    # libc abort
    ("abort", "high", re.compile(
        r"Fatal signal \d+ \(SIG\w+\),\s*code\s+\S+,\s*fault addr")),
    # GWP-ASan
    ("gwp_asan", "critical", re.compile(
        r"GWP-ASan\s+detected\s+(.+)")),
    # MTE (Memory Tagging Extension, Arm)
    ("mte", "critical", re.compile(
        r"signal \d+ \(SIGSEGV\), code \d+ \(SEGV_MTE")),
    # Scudo allocator errors
    ("scudo", "critical", re.compile(
        r"Scudo ERROR:\s*(.+)")),
    # Generic crash dump header
    ("native_crash", "critical", re.compile(
        r"DEBUG\s*:\s*pid:\s*\d+,\s*tid:\s*\d+.*>>>")),
    # Java OutOfMemoryError
    ("java_exception", "high", re.compile(
        r"java\.lang\.OutOfMemoryError")),
    # Security exceptions
    ("security", "medium", re.compile(
        r"java\.lang\.SecurityException:\s*(.+)")),
    # Mojo / IPC validation kills the renderer (browser-side)
    ("mojo_bad_msg", "high", re.compile(
        r"Terminating renderer for bad (?:IPC|Mojo) message", re.IGNORECASE)),
    ("mojo_bad_msg", "high", re.compile(
        r"\[mojo\].*Validation failed for\s+(\S+)", re.IGNORECASE)),
    ("mojo_bad_msg", "high", re.compile(
        r"bad_message[_:].*reason\s*[=:]?\s*(.+)", re.IGNORECASE)),
    # Chromium FATAL log lines
    ("chromium_fatal", "critical", re.compile(
        r"\[FATAL:[^\]]+\]\s*(.+)")),
]


class LogcatCapture:
    """Capture and analyze logcat output."""

    def __init__(self, adb: ADB):
        self.adb = adb
        self._process: Optional[subprocess.Popen] = None
        self._output_path: Optional[Path] = None
        self._file = None

    def start(
        self,
        output: str = "logcat.txt",
        filter_tag: str | None = None,
        clear_first: bool = True,
    ) -> Path:
        """Start capturing logcat in the background."""
        if clear_first:
            self.adb.run("logcat", "-c", check=False, timeout=5)

        self._output_path = Path(output)
        args = ["logcat", "-v", "threadtime"]
        if filter_tag:
            args += ["-s", filter_tag]

        f = open(self._output_path, "w", encoding="utf-8", errors="replace")
        try:
            self._process = self.adb.popen(*args, stdout=f, stderr=subprocess.STDOUT)
        except Exception:
            f.close()
            raise
        self._file = f
        console.print(f"[green]Logcat capture started → {self._output_path}")
        return self._output_path

    def stop(self) -> Path:
        """Stop capturing and return the output path."""
        if self._process:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait()
            self._process = None
        if self._file:
            self._file.flush()
            self._file.close()
            self._file = None
        console.print(f"[yellow]Logcat capture stopped → {self._output_path}")
        return self._output_path or Path("logcat.txt")

    def dump(self, output: str = "logcat_dump.txt", lines: int = 0) -> Path:
        """Dump current logcat buffer to a file (non-streaming)."""
        cmd = ["logcat", "-d", "-v", "threadtime"]
        if lines > 0:
            cmd += ["-t", str(lines)]
        result = self.adb.run(*cmd, timeout=30, check=False)
        path = Path(output)
        path.write_text(result.stdout)
        return path

    @staticmethod
    def find_crashes(logcat_path: str | Path) -> list[CrashEvent]:
        """Parse a logcat file for crashes, sanitizer reports, and ANRs."""
        logcat_path = Path(logcat_path)
        if not logcat_path.exists():
            return []

        text = logcat_path.read_text(errors="replace")
        lines = text.splitlines()
        events: list[CrashEvent] = []
        seen: set[str] = set()  # dedup by (type, message)

        for i, line in enumerate(lines):
            for event_type, severity, pattern in _CRASH_PATTERNS:
                m = pattern.search(line)
                if m:
                    message = m.group(0)
                    detail = m.group(1) if m.lastindex and m.lastindex >= 1 else ""

                    # Dedup
                    key = (event_type, message[:80])
                    if key in seen:
                        continue
                    seen.add(key)

                    # Extract timestamp and PID from logcat threadtime format:
                    # MM-DD HH:MM:SS.mmm  PID  TID LEVEL TAG: message
                    ts = ""
                    pid = ""
                    parts = line.split(None, 5)
                    if len(parts) >= 3:
                        ts = f"{parts[0]} {parts[1]}"
                        pid = parts[2]

                    # Grab a few lines of context as stacktrace
                    context_lines = lines[i:i+15]
                    stacktrace = "\n".join(context_lines)

                    events.append(CrashEvent(
                        event_type=event_type,
                        severity=severity,
                        message=detail or message,
                        pid=pid,
                        timestamp=ts,
                        stacktrace=stacktrace,
                    ))
        return events

    @staticmethod
    def print_crashes(events: list[CrashEvent]) -> None:
        if not events:
            console.print("[green]No crashes or sanitizer issues detected.")
            return

        t = Table(title=f"Crash Events ({len(events)})", show_lines=True)
        t.add_column("Type", style="bold", width=15)
        t.add_column("Severity", width=10)
        t.add_column("Message", overflow="fold")
        t.add_column("PID", width=8)
        t.add_column("Time", width=18)
        for e in events:
            color = {"critical": "red bold", "high": "red", "medium": "yellow"}.get(e.severity, "")
            t.add_row(
                e.event_type,
                f"[{color}]{e.severity.upper()}[/]",
                e.message[:200],
                e.pid,
                e.timestamp,
            )
        console.print(t)
