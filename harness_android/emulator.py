"""Android Emulator (AVD) lifecycle management."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Optional

from rich.console import Console

from harness_android.adb import ADB, poll_until
from harness_android.config import (
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
        arch: str = "x86_64",
    ):
        self.avd_name = avd_name
        self.api_level = api_level
        self.arch = arch
        self._process: Optional[subprocess.Popen] = None
        self._serial: Optional[str] = None

    # ------------------------------------------------------------------
    # AVD creation
    # ------------------------------------------------------------------

    def create_avd(
        self,
        device_profile: str = DEFAULT_DEVICE_PROFILE,
        force: bool = False,
    ) -> None:
        avdmanager = get_avdmanager()
        if not avdmanager.exists():
            raise FileNotFoundError(
                "avdmanager not found. Run `harness-android setup` first."
            )

        avd_root = get_avd_root()
        avd_root.mkdir(parents=True, exist_ok=True)

        cmd = [
            str(avdmanager),
            "create", "avd",
            "--name", self.avd_name,
            "--package", get_system_image_package(self.api_level, self.arch),
            "--device", device_profile,
            "--path", str(avd_root / self.avd_name),
        ]
        if force:
            cmd.append("--force")

        env = _emulator_env(ANDROID_AVD_HOME=str(avd_root))

        console.print(f"[bold]Creating AVD '{self.avd_name}' (API {self.api_level}) …")
        result = subprocess.run(
            cmd, input="no\n", capture_output=True, text=True, env=env,
        )
        if result.returncode != 0:
            console.print(f"[red]avdmanager error:\n{result.stderr}")
            raise RuntimeError("Failed to create AVD")
        console.print(f"[green]AVD '{self.avd_name}' created.")

    def avd_exists(self) -> bool:
        return (get_avd_root() / self.avd_name).is_dir()

    def delete_avd(self) -> None:
        avdmanager = get_avdmanager()
        env = _emulator_env(ANDROID_AVD_HOME=str(get_avd_root()))
        subprocess.run(
            [str(avdmanager), "delete", "avd", "--name", self.avd_name],
            capture_output=True, text=True, env=env,
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
        ram: int = 4096,
        wipe_data: bool = False,
        cold_boot: bool = False,
        no_snapshot_save: bool = False,
        writable_system: bool = False,
        extra_args: Optional[list[str]] = None,
        boot_timeout: float = 300,
    ) -> ADB:
        """Launch the emulator and return an :class:`ADB` handle once booted."""
        emulator = get_emulator_bin()
        if not emulator.exists():
            raise FileNotFoundError(
                "Emulator binary not found. Run `harness-android setup` first."
            )

        # Detect cross-architecture emulation (e.g. ARM64 guest on x86_64 host).
        import platform as _platform
        host_arch = _platform.machine().lower()  # e.g. 'amd64', 'x86_64'
        guest_is_arm = self.arch.startswith("arm")
        host_is_x86 = host_arch in ("x86_64", "amd64", "x86")
        if guest_is_arm and host_is_x86:
            raise RuntimeError(
                "The Android emulator does not support ARM64 guests on x86_64 hosts.\n"
                "ARM APKs typically run on x86_64 emulators via built-in binary\n"
                "translation (API 30+). If a specific APK crashes through translation,\n"
                "use a physical ARM64 device over USB instead:\n"
                "    harness-android -s <serial> install app.apk\n"
                "\n"
                "The --arch arm64 option is for ARM64 hosts (e.g. Apple Silicon Macs)."
            )

        cmd = [
            str(emulator),
            "-avd", self.avd_name,
            "-gpu", gpu,
            "-memory", str(ram),
            "-no-boot-anim",
            "-read-only" if not writable_system else "-writable-system",
        ]
        if headless:
            cmd.append("-no-window")
        if wipe_data:
            cmd.append("-wipe-data")
        if cold_boot:
            cmd.append("-no-snapshot-load")
        if no_snapshot_save:
            cmd.append("-no-snapshot-save")
        if extra_args:
            cmd.extend(extra_args)

        env = _emulator_env(ANDROID_AVD_HOME=str(get_avd_root()))

        # Snapshot existing devices so we can identify the *new* one.
        before = {d["serial"] for d in ADB.list_devices()}

        console.print(f"[bold]Starting emulator '{self.avd_name}' …")
        # Redirect emulator output to a file (NOT a pipe — the emulator is
        # chatty and would deadlock once the pipe buffer fills).
        self._log_path = get_avd_root() / f"{self.avd_name}.log"
        log_fh = open(self._log_path, "w", encoding="utf-8", errors="replace")
        self._process = subprocess.Popen(
            cmd, env=env, stdout=log_fh, stderr=subprocess.STDOUT,
        )
        log_fh.close()  # child holds its own handle now

        deadline = time.monotonic() + boot_timeout

        def _new_serial() -> str | None:
            if self._process and self._process.poll() is not None:
                tail = ""
                try:
                    tail = self._log_path.read_text(errors="replace")[-2000:]
                except Exception:  # noqa: BLE001
                    pass
                raise RuntimeError(
                    f"Emulator process exited (rc={self._process.returncode}) "
                    f"before registering with ADB. See {self._log_path}\n{tail}"
                )
            for d in ADB.list_devices():
                if d["serial"].startswith("emulator-") and d["serial"] not in before:
                    return d["serial"]
            return None

        self._serial = poll_until(
            _new_serial,
            timeout=max(10.0, deadline - time.monotonic()),
            interval=1.0,
            desc="emulator serial",
        )
        console.print(f"[dim]Emulator serial: {self._serial}")

        adb = ADB(serial=self._serial)
        adb.wait_for_device(timeout=max(10.0, deadline - time.monotonic()))
        adb.wait_for_boot(timeout=max(30.0, deadline - time.monotonic()))
        return adb

    def stop(self) -> None:
        """Shut down the emulator gracefully."""
        if self._serial:
            try:
                ADB(serial=self._serial).run("emu", "kill", check=False, timeout=15)
            except Exception:  # noqa: BLE001
                pass
        if self._process:
            try:
                self._process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=5)
            self._process = None
        console.print("[yellow]Emulator stopped.")

    @property
    def serial(self) -> Optional[str]:
        return self._serial

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.poll() is None
