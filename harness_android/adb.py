"""Thin wrapper around the ``adb`` command-line tool.

Design notes
------------
* All commands go through :meth:`run` which builds an *argv list* — never a
  shell string — so arguments survive the host shell unchanged.
* :meth:`shell` passes each token as a separate ``adb shell`` argument; ADB
  then quotes them for the device shell, so callers don't need to escape.
* :func:`poll_until` is the canonical replacement for ``time.sleep`` — every
  "wait for X" in the harness uses it instead of fixed sleeps.
"""

from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path
from typing import Callable, Optional, TypeVar

from rich.console import Console

from harness_android.config import get_adb

console = Console()

T = TypeVar("T")


def poll_until(
    fn: Callable[[], T],
    *,
    timeout: float,
    interval: float = 0.5,
    desc: str = "",
) -> T:
    """Call *fn* until it returns truthy or *timeout* elapses.

    Returns the first truthy result.  Raises :class:`TimeoutError` with
    *desc* on timeout.  Exceptions from *fn* are treated as falsy and
    retried — useful when polling something that isn't up yet.
    """
    deadline = time.monotonic() + timeout
    last_exc: Exception | None = None
    while True:
        try:
            result = fn()
            if result:
                return result
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
        if time.monotonic() >= deadline:
            hint = f" (last error: {last_exc})" if last_exc else ""
            raise TimeoutError(f"Timed out after {timeout:.0f}s waiting for {desc or fn}{hint}")
        time.sleep(interval)


# adb's `input text` treats these as shell metacharacters on the device side
_INPUT_TEXT_ESCAPE = re.compile(r"([ \\()<>|;&*~'\"$`])")


class ADB:
    """Interface to a single Android device/emulator via ADB."""

    def __init__(self, serial: Optional[str] = None):
        self.serial = serial
        self._adb_path = get_adb()
        if not self._adb_path.exists():
            raise FileNotFoundError(
                f"adb binary not found at {self._adb_path}. "
                "Run `harness-android setup` first or set ANDROID_HOME."
            )

    # ------------------------------------------------------------------
    # Low-level helpers
    # ------------------------------------------------------------------

    def _base_cmd(self) -> list[str]:
        cmd = [str(self._adb_path)]
        if self.serial:
            cmd += ["-s", self.serial]
        return cmd

    def run(
        self,
        *args: str,
        timeout: float = 60,
        check: bool = True,
        input: str | bytes | None = None,  # noqa: A002
    ) -> subprocess.CompletedProcess[str]:
        """Execute an adb command and return the completed process."""
        cmd = self._base_cmd() + list(args)
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            input=input,
        )
        if check and result.returncode != 0:
            raise RuntimeError(
                f"adb {' '.join(args)} failed (rc={result.returncode}):\n"
                f"{result.stderr.strip() or result.stdout.strip()}"
            )
        return result

    def popen(self, *args: str, **kw) -> subprocess.Popen:
        """Start a long-running adb command (logcat, screenrecord, …)."""
        return subprocess.Popen(self._base_cmd() + list(args), **kw)

    # ------------------------------------------------------------------
    # Connection helpers
    # ------------------------------------------------------------------

    def start_server(self) -> None:
        self.run("start-server")

    def kill_server(self) -> None:
        self.run("kill-server", check=False)

    def wait_for_device(self, timeout: float = 120) -> None:
        """Block until the device is online."""
        console.print("[bold]Waiting for device …")
        self.run("wait-for-device", timeout=timeout)

    def wait_for_boot(self, timeout: float = 300) -> None:
        """Poll until the device has fully booted *and* the package manager is up."""
        console.print("[bold]Waiting for boot to complete …")

        def _booted() -> bool:
            r = self.run("shell", "getprop", "sys.boot_completed", check=False, timeout=10)
            if r.stdout.strip() != "1":
                return False
            # boot_completed flips before PackageManager is ready; verify.
            pm = self.run("shell", "pm", "path", "android", check=False, timeout=10)
            return "package:" in pm.stdout

        poll_until(_booted, timeout=timeout, interval=2, desc="device boot")
        console.print("[green]Device booted.")

    # ------------------------------------------------------------------
    # Device info
    # ------------------------------------------------------------------

    def get_serialno(self) -> str:
        return self.run("get-serialno").stdout.strip()

    def get_property(self, prop: str) -> str:
        return self.run("shell", "getprop", prop).stdout.strip()

    def get_android_version(self) -> str:
        return self.get_property("ro.build.version.release")

    def get_api_level(self) -> str:
        return self.get_property("ro.build.version.sdk")

    @staticmethod
    def list_devices() -> list[dict[str, str]]:
        """Return a list of ``{serial, state}`` dicts (no serial filter)."""
        adb_path = get_adb()
        result = subprocess.run(
            [str(adb_path), "devices"], capture_output=True, text=True, timeout=15,
        )
        devices: list[dict[str, str]] = []
        for line in result.stdout.splitlines():
            m = re.match(r"^(\S+)\s+(device|offline|unauthorized)\b", line)
            if m:
                devices.append({"serial": m.group(1), "state": m.group(2)})
        return devices

    # ------------------------------------------------------------------
    # Shell
    # ------------------------------------------------------------------

    def shell(self, *args: str, timeout: float = 60, check: bool = True) -> str:
        """Run a command on the device and return stdout.

        Each *arg* is passed as a separate token to ``adb shell``; ADB
        quotes them for the device shell so spaces/specials in arguments
        survive intact.
        """
        return self.run("shell", *args, timeout=timeout, check=check).stdout

    def write_file(self, remote_path: str, content: str | bytes) -> None:
        """Write *content* to *remote_path* on the device atomically.

        Uses ``adb shell cat > path`` with stdin so arbitrary content
        (quotes, newlines, binary) survives without shell escaping.
        """
        if isinstance(content, str):
            content = content.encode()
        # `exec-in` streams stdin verbatim to the device-side command.
        proc = subprocess.run(
            self._base_cmd() + ["exec-in", f"sh -c 'cat > {remote_path}'"],
            input=content,
            capture_output=True,
            timeout=30,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"write_file({remote_path}) failed: "
                f"{proc.stderr.decode(errors='replace').strip()}"
            )

    # ------------------------------------------------------------------
    # App management
    # ------------------------------------------------------------------

    def install(self, apk_path: str | Path, replace: bool = True, grant: bool = True) -> None:
        args = ["install"]
        if replace:
            args.append("-r")
        if grant:
            args.append("-g")
        args.append(str(apk_path))
        console.print(f"[bold]Installing {apk_path} …")
        self.run(*args, timeout=300)
        console.print("[green]Installed.")

    def uninstall(self, package: str) -> None:
        self.run("uninstall", package, check=False)

    def is_installed(self, package: str) -> bool:
        out = self.run("shell", "pm", "path", package, check=False).stdout
        return out.startswith("package:")

    def launch_activity(self, component: str) -> None:
        """Start an activity. *component* is ``package/activity``."""
        self.shell("am", "start", "-W", "-n", component)

    def launch_url(self, url: str) -> None:
        """Open *url* using an ACTION_VIEW intent."""
        self.shell("am", "start", "-W", "-a", "android.intent.action.VIEW", "-d", url)

    def pidof(self, package: str) -> int | None:
        out = self.run("shell", "pidof", package, check=False, timeout=5).stdout.strip()
        return int(out.split()[0]) if out else None

    # ------------------------------------------------------------------
    # File transfer
    # ------------------------------------------------------------------

    def push(self, local: str | Path, remote: str) -> None:
        self.run("push", str(local), remote, timeout=300)

    def pull(self, remote: str, local: str | Path) -> None:
        self.run("pull", remote, str(local), timeout=300)

    # ------------------------------------------------------------------
    # Screenshots / recording
    # ------------------------------------------------------------------

    def screenshot(self, local_path: str | Path) -> Path:
        """Capture a screenshot and pull it to *local_path*."""
        remote = "/sdcard/harness_screenshot.png"
        self.shell("screencap", "-p", remote)
        local_path = Path(local_path)
        self.pull(remote, local_path)
        self.shell("rm", "-f", remote)
        console.print(f"[green]Screenshot saved to {local_path}")
        return local_path

    def screenrecord_start(self, remote_path: str = "/sdcard/harness_rec.mp4") -> subprocess.Popen:
        """Start recording in the background. Returns the host-side process."""
        proc = self.popen(
            "shell", "screenrecord", remote_path,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        # Wait until the device-side process actually exists.
        poll_until(
            lambda: self.pidof("screenrecord"),
            timeout=5, interval=0.2, desc="screenrecord start",
        )
        return proc

    def screenrecord_stop(self, proc: subprocess.Popen | None = None) -> None:
        self.run("shell", "pkill", "-INT", "-f", "screenrecord", check=False, timeout=10)
        if proc:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    # ------------------------------------------------------------------
    # Port forwarding
    # ------------------------------------------------------------------

    def forward(self, local_port: int, remote: int | str) -> None:
        """Forward host TCP *local_port* to a device port or abstract socket.

        *remote* may be an int (TCP port) or a string ``localabstract:NAME``.
        """
        remote_spec = remote if isinstance(remote, str) else f"tcp:{remote}"
        self.run("forward", f"tcp:{local_port}", remote_spec)

    def forward_remove(self, local_port: int) -> None:
        self.run("forward", "--remove", f"tcp:{local_port}", check=False)

    def reverse(self, remote_port: int, local_port: int) -> None:
        """Forward device TCP *remote_port* to host *local_port*."""
        self.run("reverse", f"tcp:{remote_port}", f"tcp:{local_port}")

    def reverse_remove(self, remote_port: int) -> None:
        self.run("reverse", "--remove", f"tcp:{remote_port}", check=False)

    def list_abstract_sockets(self, pattern: str = "") -> list[str]:
        """Return abstract-namespace UNIX sockets matching *pattern*."""
        out = self.shell("cat", "/proc/net/unix")
        names: list[str] = []
        for line in out.splitlines():
            # Abstract sockets show as "@name" in the last column
            m = re.search(r"@(\S+)\s*$", line)
            if m and (not pattern or pattern in m.group(1)):
                names.append(m.group(1))
        return sorted(set(names))

    # ------------------------------------------------------------------
    # Input
    # ------------------------------------------------------------------

    def tap(self, x: int, y: int) -> None:
        self.shell("input", "tap", str(x), str(y))

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> None:
        self.shell("input", "swipe", str(x1), str(y1), str(x2), str(y2), str(duration_ms))

    def key_event(self, keycode: str | int) -> None:
        self.shell("input", "keyevent", str(keycode))

    def text(self, value: str) -> None:
        """Type text on the device, escaping all shell metacharacters."""
        escaped = _INPUT_TEXT_ESCAPE.sub(r"\\\1", value).replace(" ", "%s")
        self.shell("input", "text", escaped)

    def press_home(self) -> None:
        self.key_event(3)

    def press_back(self) -> None:
        self.key_event(4)
