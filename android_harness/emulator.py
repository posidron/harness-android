"""Android Emulator (AVD) lifecycle management."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Optional

from rich.console import Console

from android_harness.adb import ADB
from android_harness.config import (
    DEFAULT_API_LEVEL,
    DEFAULT_AVD_NAME,
    DEFAULT_DEVICE_PROFILE,
    IS_WINDOWS,
    get_avd_root,
    get_avdmanager,
    get_emulator_bin,
    get_java_home,
    get_sdk_root,
    get_system_image_package,
)

console = Console()


def _emulator_env(**extra: str) -> dict[str, str]:
    """Build env dict with JAVA_HOME, ANDROID_SDK_ROOT, and extras."""
    env = {
        **os.environ,
        "ANDROID_SDK_ROOT": str(get_sdk_root()),
        **extra,
    }
    java_home = get_java_home()
    if java_home:
        env["JAVA_HOME"] = str(java_home)
        sep = ";" if IS_WINDOWS else ":"
        env["PATH"] = str(java_home / "bin") + sep + env.get("PATH", "")
    return env


class Emulator:
    """Manage a single Android emulator instance."""

    def __init__(
        self,
        avd_name: str = DEFAULT_AVD_NAME,
        api_level: int = DEFAULT_API_LEVEL,
    ):
        self.avd_name = avd_name
        self.api_level = api_level
        self._process: Optional[subprocess.Popen[str]] = None
        self._serial: Optional[str] = None

    # ------------------------------------------------------------------
    # AVD creation
    # ------------------------------------------------------------------

    def create_avd(
        self,
        device_profile: str = DEFAULT_DEVICE_PROFILE,
        force: bool = False,
    ) -> None:
        """Create an AVD using avdmanager."""
        avdmanager = get_avdmanager()
        if not avdmanager.exists():
            raise FileNotFoundError(
                "avdmanager not found. Run `android-harness setup` first."
            )

        avd_root = get_avd_root()
        avd_root.mkdir(parents=True, exist_ok=True)

        cmd = [
            str(avdmanager),
            "create", "avd",
            "--name", self.avd_name,
            "--package", get_system_image_package(self.api_level),
            "--device", device_profile,
            "--path", str(avd_root / self.avd_name),
        ]
        if force:
            cmd.append("--force")

        env = _emulator_env(ANDROID_AVD_HOME=str(avd_root))

        console.print(f"[bold]Creating AVD '{self.avd_name}' (API {self.api_level}) …")
        result = subprocess.run(
            cmd,
            input="no\n",  # don't create custom hardware profile
            capture_output=True,
            text=True,
            env=env,
        )
        if result.returncode != 0:
            console.print(f"[red]avdmanager error:\n{result.stderr}")
            raise RuntimeError("Failed to create AVD")
        console.print(f"[green]AVD '{self.avd_name}' created.")

    def avd_exists(self) -> bool:
        avd_path = get_avd_root() / self.avd_name
        return avd_path.is_dir()

    def delete_avd(self) -> None:
        avdmanager = get_avdmanager()
        env = _emulator_env(ANDROID_AVD_HOME=str(get_avd_root()))
        subprocess.run(
            [str(avdmanager), "delete", "avd", "--name", self.avd_name],
            capture_output=True,
            text=True,
            env=env,
        )
        console.print(f"[yellow]AVD '{self.avd_name}' deleted.")

    # ------------------------------------------------------------------
    # Emulator lifecycle
    # ------------------------------------------------------------------

    def start(
        self,
        *,
        headless: bool = False,
        gpu: str = "auto",
        ram: int = 2048,
        wipe_data: bool = False,
        extra_args: Optional[list[str]] = None,
    ) -> ADB:
        """Launch the emulator and return an :class:`ADB` handle.

        Parameters
        ----------
        headless:
            Run without a window (``-no-window``).
        gpu:
            GPU mode – ``auto``, ``host``, ``swiftshader_indirect``, ``off``.
        ram:
            RAM in MB.
        wipe_data:
            Wipe user data on start.
        extra_args:
            Any additional emulator flags.
        """
        emulator = get_emulator_bin()
        if not emulator.exists():
            raise FileNotFoundError(
                "Emulator binary not found. Run `android-harness setup` first."
            )

        cmd = [
            str(emulator),
            "-avd", self.avd_name,
            "-gpu", gpu,
            "-memory", str(ram),
        ]
        if headless:
            cmd.append("-no-window")
        if wipe_data:
            cmd.append("-wipe-data")
        if extra_args:
            cmd.extend(extra_args)

        env = _emulator_env(ANDROID_AVD_HOME=str(get_avd_root()))

        console.print(f"[bold]Starting emulator '{self.avd_name}' …")
        # Launch in the background; emulator writes to its own console
        self._process = subprocess.Popen(
            cmd,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # Give the process a moment, then figure out the serial
        time.sleep(3)
        self._serial = self._detect_serial()
        adb = ADB(serial=self._serial)
        adb.wait_for_device()
        adb.wait_for_boot()
        return adb

    def _detect_serial(self) -> str:
        """Return the serial string for the running emulator."""
        adb = ADB()
        # Poll a few times – the emulator may need a moment to register
        for _ in range(30):
            devices = adb.list_devices()
            for d in devices:
                if d["serial"].startswith("emulator-") and d["state"] == "device":
                    return d["serial"]
            time.sleep(2)
        raise TimeoutError("Could not detect emulator serial")

    def stop(self) -> None:
        """Shut down the emulator gracefully."""
        if self._serial:
            adb = ADB(serial=self._serial)
            adb.run("emu", "kill", check=False, timeout=15)
        if self._process:
            try:
                self._process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self._process.kill()
            self._process = None
        console.print("[yellow]Emulator stopped.")

    @property
    def serial(self) -> Optional[str]:
        return self._serial

    @property
    def running(self) -> bool:
        if self._process is None:
            return False
        return self._process.poll() is None
