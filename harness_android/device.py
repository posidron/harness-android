"""High-level ``Device`` facade combining emulator + ADB + browser control."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from rich.console import Console

from harness_android.adb import ADB
from harness_android.browser import Browser
from harness_android.config import DEFAULT_API_LEVEL, DEFAULT_AVD_NAME
from harness_android.emulator import Emulator
from harness_android.sdk import full_setup

console = Console()


class Device:
    """One-stop handle for an Android emulator with browser control.

    Usage::

        from harness_android.device import Device

        dev = Device()
        dev.setup()           # downloads SDK + system image (once)
        dev.launch()           # creates AVD if needed, boots emulator

        dev.open_url("https://example.com")
        dev.run_shell("ls /sdcard")

        # Advanced: Chrome DevTools Protocol
        dev.browser.enable_cdp()
        dev.browser.connect()
        title = dev.browser.get_page_title()

        dev.shutdown()
    """

    def __init__(
        self,
        avd_name: str = DEFAULT_AVD_NAME,
        api_level: int = DEFAULT_API_LEVEL,
        headless: bool = False,
        gpu: str = "auto",
        ram: int = 2048,
    ):
        self.avd_name = avd_name
        self.api_level = api_level
        self.headless = headless
        self.gpu = gpu
        self.ram = ram

        self._emulator = Emulator(avd_name=avd_name, api_level=api_level)
        self._adb: Optional[ADB] = None
        self._browser: Optional[Browser] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def setup(self) -> None:
        """Download SDK, tools, system image, accept licences."""
        full_setup(self.api_level)

    def launch(self, wipe_data: bool = False) -> None:
        """Create AVD (if needed) and boot the emulator."""
        if not self._emulator.avd_exists():
            self._emulator.create_avd(force=True)
        self._adb = self._emulator.start(
            headless=self.headless,
            gpu=self.gpu,
            ram=self.ram,
            wipe_data=wipe_data,
        )
        self._browser = Browser(self._adb)
        console.print("[bold green]Device is ready!")

    def shutdown(self) -> None:
        if self._browser:
            self._browser.disable_cdp()
            self._browser = None
        self._emulator.stop()
        self._adb = None

    # ------------------------------------------------------------------
    # Convenience accessors
    # ------------------------------------------------------------------

    @property
    def adb(self) -> ADB:
        if self._adb is None:
            raise RuntimeError("Device is not running. Call launch() first.")
        return self._adb

    @property
    def browser(self) -> Browser:
        if self._browser is None:
            raise RuntimeError("Device is not running. Call launch() first.")
        return self._browser

    # ------------------------------------------------------------------
    # Shortcuts
    # ------------------------------------------------------------------

    def run_shell(self, command: str) -> str:
        """Execute a shell command on the device."""
        return self.adb.shell(command)

    def open_url(self, url: str) -> None:
        self.browser.open_url(url)

    def install_apk(self, path: str | Path) -> None:
        self.adb.install(path)

    def screenshot(self, path: str = "screenshot.png") -> Path:
        return self.adb.screenshot(path)

    def tap(self, x: int, y: int) -> None:
        self.adb.tap(x, y)

    def swipe(self, x1: int, y1: int, x2: int, y2: int) -> None:
        self.adb.swipe(x1, y1, x2, y2)

    def type_text(self, text: str) -> None:
        self.adb.text(text)

    def press_home(self) -> None:
        self.adb.press_home()

    def press_back(self) -> None:
        self.adb.press_back()

    def push_file(self, local: str, remote: str) -> None:
        self.adb.push(local, remote)

    def pull_file(self, remote: str, local: str) -> None:
        self.adb.pull(remote, local)

    def get_info(self) -> dict[str, str]:
        return {
            "serial": self.adb.get_serialno(),
            "android_version": self.adb.get_android_version(),
            "api_level": self.adb.get_api_level(),
            "avd": self.avd_name,
        }

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> Device:
        self.launch()
        return self

    def __exit__(self, *exc: object) -> None:
        self.shutdown()
