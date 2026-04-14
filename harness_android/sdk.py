"""Android SDK bootstrap and package management."""

from __future__ import annotations

import io
import os
import stat
import subprocess
import zipfile
from pathlib import Path

import requests
from rich.console import Console
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    TextColumn,
    TransferSpeedColumn,
)

from harness_android.config import (
    IS_WINDOWS,
    PLATFORM,
    get_cmdline_tools_url,
    get_harness_home,
    get_java_home,
    get_jdk_root,
    get_jdk_url,
    get_sdk_root,
    get_sdkmanager,
    get_system_image_package,
)

console = Console()


def _download_with_progress(url: str) -> bytes:
    """Download *url* and display a rich progress bar."""
    resp = requests.get(url, stream=True, timeout=120)
    resp.raise_for_status()
    total = int(resp.headers.get("content-length", 0))
    buf = io.BytesIO()
    with Progress(
        TextColumn("[bold blue]{task.description}"),
        BarColumn(),
        DownloadColumn(),
        TransferSpeedColumn(),
    ) as progress:
        task = progress.add_task("Downloading SDK tools", total=total)
        for chunk in resp.iter_content(chunk_size=1 << 16):
            buf.write(chunk)
            progress.advance(task, len(chunk))
    return buf.getvalue()


def _ensure_executable(path: Path) -> None:
    if not IS_WINDOWS:
        path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _make_tree_executable(root: Path) -> None:
    """Ensure all files under *root*/bin are executable (macOS/Linux)."""
    if IS_WINDOWS:
        return
    for p in root.rglob("*"):
        if p.is_file():
            _ensure_executable(p)


def bootstrap_jdk() -> Path:
    """Download a portable OpenJDK if no Java is available. Returns JAVA_HOME."""
    existing = get_java_home()
    if existing:
        console.print(f"[green]Java found at {existing}")
        return existing

    console.print("[bold]No Java found — downloading portable OpenJDK 17 …")
    url = get_jdk_url()
    data = _download_with_progress(url)

    jdk_root = get_jdk_root()
    jdk_root.mkdir(parents=True, exist_ok=True)

    if url.endswith(".zip"):
        import zipfile as _zf

        with _zf.ZipFile(io.BytesIO(data)) as zf:
            zf.extractall(jdk_root)
    else:
        import tarfile

        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
            tf.extractall(jdk_root)

    _make_tree_executable(jdk_root)

    java_home = get_java_home()
    if java_home is None:
        raise RuntimeError(
            f"JDK was extracted to {jdk_root} but java binary not found"
        )
    console.print(f"[green]JDK installed — JAVA_HOME={java_home}")
    return java_home


def _sdk_env() -> dict[str, str]:
    """Build an env dict with JAVA_HOME + ANDROID_SDK_ROOT set."""
    env = {**os.environ, "ANDROID_SDK_ROOT": str(get_sdk_root())}
    java_home = get_java_home()
    if java_home:
        env["JAVA_HOME"] = str(java_home)
        # Prepend JDK bin to PATH so sdkmanager/avdmanager find java
        sep = ";" if IS_WINDOWS else ":"
        env["PATH"] = str(java_home / "bin") + sep + env.get("PATH", "")
    return env


def bootstrap_sdk() -> Path:
    """Download cmdline-tools if missing and return the SDK root."""
    sdk_root = get_sdk_root()
    sdkmanager = get_sdkmanager()

    if sdkmanager.exists():
        console.print(f"[green]SDK tools already present at {sdk_root}")
        return sdk_root

    console.print("[bold]Bootstrapping Android SDK command-line tools …")
    url = get_cmdline_tools_url()
    data = _download_with_progress(url)

    # The zip contains a `cmdline-tools/` folder – we need to place it as
    # <sdk>/cmdline-tools/latest/
    dest = sdk_root / "cmdline-tools" / "latest"
    dest.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        # entries look like cmdline-tools/bin/..., cmdline-tools/lib/... etc.
        for info in zf.infolist():
            # Strip the leading `cmdline-tools/` prefix
            parts = info.filename.split("/", 1)
            if len(parts) < 2 or not parts[1]:
                continue
            target = dest / parts[1]
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(zf.read(info))

    _make_tree_executable(dest)

    console.print(f"[green]SDK tools installed to {sdk_root}")
    return sdk_root


def _run_sdkmanager(*args: str) -> subprocess.CompletedProcess[str]:
    sdkmanager = get_sdkmanager()
    if not sdkmanager.exists():
        raise FileNotFoundError(
            "sdkmanager not found. Run `harness-android setup` first."
        )
    cmd = [str(sdkmanager), f"--sdk_root={get_sdk_root()}", *args]
    return subprocess.run(
        cmd,
        input="y\n" * 10,  # auto-accept licences
        capture_output=True,
        text=True,
        env=_sdk_env(),
    )


def accept_licenses() -> None:
    console.print("[bold]Accepting SDK licences …")
    _run_sdkmanager("--licenses")


def install_packages(api_level: int) -> None:
    """Install platform-tools, emulator, platform, and system image."""
    packages = [
        "platform-tools",
        "emulator",
        f"platforms;android-{api_level}",
        get_system_image_package(api_level),
    ]
    console.print(f"[bold]Installing SDK packages: {', '.join(packages)}")
    result = _run_sdkmanager(*packages)
    if result.returncode != 0:
        console.print(f"[red]sdkmanager error:\n{result.stderr}")
        raise RuntimeError("Failed to install SDK packages")
    console.print("[green]All SDK packages installed.")


def full_setup(api_level: int) -> Path:
    """Bootstrap JDK + SDK, accept licences, install packages. Returns SDK root."""
    bootstrap_jdk()
    sdk_root = bootstrap_sdk()
    accept_licenses()
    install_packages(api_level)
    return sdk_root


def download_chromium_apk(arch: str = "x64") -> Path:
    """Download the latest Chromium snapshot APK (debuggable, supports CDP).

    Chromium snapshot builds have ``android:debuggable=true`` so they read
    command-line flags from ``/data/local/tmp/chromium-command-line``.
    This is required for CDP on API 35+ where release Chrome ignores flags.

    *arch*: ``x64`` (default, for x86_64 emulators) or ``arm64``.
    """
    # Map arch to the Chromium snapshot bucket prefix
    prefix = {"x64": "Android_x64", "arm64": "Android_Arm64", "arm": "Android"}.get(arch, "Android_x64")

    console.print(f"[bold]Downloading latest Chromium snapshot APK ({prefix}) …")
    last_change_url = (
        f"https://www.googleapis.com/download/storage/v1/b/chromium-browser-snapshots"
        f"/o/{prefix}%2FLAST_CHANGE?alt=media"
    )
    try:
        resp = requests.get(last_change_url, timeout=30)
        resp.raise_for_status()
        build = resp.text.strip()
    except Exception as exc:
        raise RuntimeError(f"Could not fetch Chromium build number: {exc}") from exc

    apk_url = (
        f"https://www.googleapis.com/download/storage/v1/b/chromium-browser-snapshots"
        f"/o/{prefix}%2F{build}%2Fchrome-android.zip?alt=media"
    )
    console.print(f"[dim]Build: {build}")
    data = _download_with_progress(apk_url)

    # Extract APK from the zip
    harness_home = get_harness_home()
    chromium_dir = harness_home / "chromium"
    chromium_dir.mkdir(parents=True, exist_ok=True)
    apk_path = chromium_dir / "ChromePublic.apk"

    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        # The zip contains chrome-android/apks/ChromePublic.apk
        for info in zf.infolist():
            if info.filename.endswith("ChromePublic.apk"):
                apk_path.write_bytes(zf.read(info))
                console.print(f"[green]Chromium APK saved to {apk_path}")
                return apk_path

    raise RuntimeError("ChromePublic.apk not found in Chromium snapshot zip")


def install_chromium(adb_path: Path | None = None) -> None:
    """Download and install Chromium on the running emulator."""
    from harness_android.config import get_adb
    from harness_android.adb import ADB

    apk_path = download_chromium_apk()
    adb = ADB()
    console.print("[bold]Installing Chromium on emulator …")
    adb.install(apk_path, replace=True)
    console.print("[green]Chromium installed! CDP will now use org.chromium.chrome.")
