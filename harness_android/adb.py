"""Thin wrapper around the ``adb`` command-line tool."""

from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path
from typing import Optional

from rich.console import Console

from harness_android.config import get_adb

console = Console()


class ADB:
    """Interface to a single Android device/emulator via ADB."""

    def __init__(self, serial: Optional[str] = None):
        self.serial = serial

    # ------------------------------------------------------------------
    # Low-level helpers
    # ------------------------------------------------------------------

    def _base_cmd(self) -> list[str]:
        cmd = [str(get_adb())]
        if self.serial:
            cmd += ["-s", self.serial]
        return cmd

    def run(
        self,
        *args: str,
        timeout: int = 60,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        """Execute an adb command and return the completed process."""
        cmd = self._base_cmd() + list(args)
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if check and result.returncode != 0:
            raise RuntimeError(
                f"adb {' '.join(args)} failed (rc={result.returncode}):\n"
                f"{result.stderr.strip()}"
            )
        return result

    # ------------------------------------------------------------------
    # Connection helpers
    # ------------------------------------------------------------------

    def start_server(self) -> None:
        self.run("start-server")

    def kill_server(self) -> None:
        self.run("kill-server", check=False)

    def wait_for_device(self, timeout: int = 120) -> None:
        """Block until the device is online."""
        console.print("[bold]Waiting for device …")
        self.run("wait-for-device", timeout=timeout)

    def wait_for_boot(self, timeout: int = 300) -> None:
        """Poll ``sys.boot_completed`` until the device has fully booted."""
        console.print("[bold]Waiting for boot to complete …")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = self.run(
                "shell", "getprop", "sys.boot_completed",
                check=False, timeout=10,
            )
            if result.stdout.strip() == "1":
                console.print("[green]Device booted.")
                return
            time.sleep(2)
        raise TimeoutError("Device did not finish booting in time")

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

    def list_devices(self) -> list[dict[str, str]]:
        """Return a list of ``{serial, state}`` dicts."""
        result = self.run("devices", check=False)
        devices: list[dict[str, str]] = []
        for line in result.stdout.splitlines():
            m = re.match(r"^(\S+)\s+(device|offline|unauthorized)", line)
            if m:
                devices.append({"serial": m.group(1), "state": m.group(2)})
        return devices

    # ------------------------------------------------------------------
    # Shell
    # ------------------------------------------------------------------

    def shell(self, *args: str, timeout: int = 60) -> str:
        """Run a shell command on the device and return stdout."""
        return self.run("shell", *args, timeout=timeout).stdout

    # ------------------------------------------------------------------
    # App management
    # ------------------------------------------------------------------

    def install(self, apk_path: str | Path, replace: bool = True) -> None:
        args = ["install"]
        if replace:
            args.append("-r")
        args.append(str(apk_path))
        console.print(f"[bold]Installing {apk_path} …")
        self.run(*args, timeout=180)
        console.print("[green]Installed.")

    def uninstall(self, package: str) -> None:
        self.run("uninstall", package, check=False)

    def launch_activity(self, component: str) -> None:
        """Start an activity. *component* is ``package/activity``."""
        self.shell("am", "start", "-n", component)

    def launch_url(self, url: str) -> None:
        """Open *url* using an ACTION_VIEW intent."""
        self.shell(
            "am", "start", "-a", "android.intent.action.VIEW", "-d", url,
        )

    # ------------------------------------------------------------------
    # File transfer
    # ------------------------------------------------------------------

    def push(self, local: str | Path, remote: str) -> None:
        self.run("push", str(local), remote, timeout=120)

    def pull(self, remote: str, local: str | Path) -> None:
        self.run("pull", remote, str(local), timeout=120)

    # ------------------------------------------------------------------
    # Screenshots
    # ------------------------------------------------------------------

    def screenshot(self, local_path: str | Path) -> Path:
        """Capture a screenshot and pull it to *local_path*."""
        remote = "/sdcard/harness_screenshot.png"
        self.shell("screencap", "-p", remote)
        local_path = Path(local_path)
        self.pull(remote, local_path)
        self.shell("rm", remote)
        console.print(f"[green]Screenshot saved to {local_path}")
        return local_path

    # ------------------------------------------------------------------
    # Screen recording
    # ------------------------------------------------------------------

    def screenrecord_start(self, remote_path: str = "/sdcard/harness_rec.mp4") -> str:
        """Start recording in the background. Returns remote path."""
        # screenrecord runs until killed
        subprocess.Popen(
            self._base_cmd() + ["shell", "screenrecord", remote_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return remote_path

    def screenrecord_stop(self) -> None:
        """Kill any screenrecord processes on device."""
        self.shell("pkill", "-f", "screenrecord", timeout=10)

    # ------------------------------------------------------------------
    # Port forwarding
    # ------------------------------------------------------------------

    def forward(self, local_port: int, remote_port: int) -> None:
        self.run("forward", f"tcp:{local_port}", f"tcp:{remote_port}")

    def forward_remove(self, local_port: int) -> None:
        self.run("forward", "--remove", f"tcp:{local_port}", check=False)

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
        """Type text on the device. Spaces are replaced with ``%s``."""
        escaped = value.replace(" ", "%s")
        self.shell("input", "text", escaped)

    def press_home(self) -> None:
        self.key_event(3)

    def press_back(self) -> None:
        self.key_event(4)
