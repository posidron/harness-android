"""Configuration and path management for android-harness."""

from __future__ import annotations

import os
import platform
import shutil
from pathlib import Path


def _detect_platform() -> str:
    system = platform.system().lower()
    if system == "darwin":
        return "mac"
    if system == "windows":
        return "windows"
    return "linux"


PLATFORM = _detect_platform()
IS_WINDOWS = PLATFORM == "windows"
IS_MAC = PLATFORM == "mac"

# Default Android API level (Android 15)
DEFAULT_API_LEVEL = 35
DEFAULT_AVD_NAME = "harness_device"
DEFAULT_DEVICE_PROFILE = "pixel_7"

# SDK download metadata — bump these when newer cmdline-tools ship
_CMDLINE_TOOLS_VERSION = "11076708"
_CMDLINE_TOOLS_URLS = {
    "windows": f"https://dl.google.com/android/repository/commandlinetools-win-{_CMDLINE_TOOLS_VERSION}_latest.zip",
    "mac": f"https://dl.google.com/android/repository/commandlinetools-mac-{_CMDLINE_TOOLS_VERSION}_latest.zip",
    "linux": f"https://dl.google.com/android/repository/commandlinetools-linux-{_CMDLINE_TOOLS_VERSION}_latest.zip",
}

# Adoptium Eclipse Temurin JDK 17 — portable, no installer needed
_JDK_VERSION = "17.0.13+11"
_JDK_TAG = _JDK_VERSION.replace("+", "%2B")  # URL-encoded
_JDK_BASE = "https://github.com/adoptium/temurin17-binaries/releases/download"
_JDK_URLS = {
    "windows": f"{_JDK_BASE}/jdk-{_JDK_TAG}/OpenJDK17U-jdk_x64_windows_hotspot_{_JDK_VERSION.replace('+', '_')}.zip",
    "mac": f"{_JDK_BASE}/jdk-{_JDK_TAG}/OpenJDK17U-jdk_x64_mac_hotspot_{_JDK_VERSION.replace('+', '_')}.tar.gz",
    "linux": f"{_JDK_BASE}/jdk-{_JDK_TAG}/OpenJDK17U-jdk_x64_linux_hotspot_{_JDK_VERSION.replace('+', '_')}.tar.gz",
}

# ADB / emulator ports
ADB_DEFAULT_PORT = 5037
EMULATOR_CONSOLE_PORT = 5554
CDP_REMOTE_PORT = 9222  # Chrome DevTools Protocol on device
CDP_LOCAL_PORT = 9222   # Forwarded to host

# Default harness configuration — overridden by config.json, then by CLI flags
_DEFAULT_CONFIG = {
    "emulator": {
        "ram": 4096,
        "gpu": "auto",
        "avd_name": DEFAULT_AVD_NAME,
        "api_level": DEFAULT_API_LEVEL,
        "device_profile": DEFAULT_DEVICE_PROFILE,
        "headless": False,
        "no_boot_anim": True,
    },
    "browser": {
        "package": "com.android.chrome",
        "activity": "com.google.android.apps.chrome.Main",
        "cdp_port": CDP_LOCAL_PORT,
        "chrome_flags": [],
    },
    "proxy": {
        "host": "10.0.2.2",
        "port": 8080,
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    """Merge *override* into *base* recursively."""
    result = base.copy()
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def load_config() -> dict:
    """Load harness configuration.

    Searches (in order):
    1. ``./harness.json`` (project-local)
    2. ``~/.android-harness/config.json`` (user-global)
    3. Built-in defaults

    Returns the merged config dict.
    """
    import json as _json
    config = _DEFAULT_CONFIG.copy()

    paths = [
        get_harness_home() / "config.json",
        Path("harness.json"),
    ]
    for p in paths:
        if p.is_file():
            try:
                with open(p) as f:
                    user = _json.load(f)
                config = _deep_merge(config, user)
            except Exception:  # noqa: BLE001
                pass

    return config


def get_config_value(section: str, key: str, default=None):
    """Get a single config value, e.g. ``get_config_value("emulator", "ram")``."""
    cfg = load_config()
    return cfg.get(section, {}).get(key, default)


def get_harness_home() -> Path:
    """Return the root directory for android-harness data.

    Respects ``ANDROID_HARNESS_HOME``; falls back to ``~/.android-harness``.
    """
    env = os.environ.get("ANDROID_HARNESS_HOME")
    if env:
        return Path(env)
    return Path.home() / ".android-harness"


def get_sdk_root() -> Path:
    """Return the Android SDK root.

    Uses ``ANDROID_HOME`` / ``ANDROID_SDK_ROOT`` if set, otherwise places
    the SDK under the harness home directory.
    """
    for var in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        val = os.environ.get(var)
        if val and Path(val).is_dir():
            return Path(val)
    return get_harness_home() / "sdk"


def get_avd_root() -> Path:
    return get_harness_home() / "avd"


def get_jdk_root() -> Path:
    return get_harness_home() / "jdk"


def get_java_home() -> Path | None:
    """Return a usable JAVA_HOME.

    Priority: ``JAVA_HOME`` env var → bundled JDK under harness home.
    Returns *None* if nothing is available yet (caller should bootstrap).
    """
    env = os.environ.get("JAVA_HOME")
    if env and (Path(env) / "bin" / _exe("java")).exists():
        return Path(env)
    # Check for our bundled JDK
    jdk_root = get_jdk_root()
    if jdk_root.is_dir():
        # The archive may nest the JDK one level deep
        for candidate in [jdk_root, *jdk_root.iterdir()]:
            java_bin = candidate / "bin" / _exe("java")
            if java_bin.exists():
                return candidate
            # macOS Temurin puts Contents/Home inside the dir
            mac_home = candidate / "Contents" / "Home"
            if (mac_home / "bin" / "java").exists():
                return mac_home
    return None


def get_jdk_url() -> str:
    return _JDK_URLS[PLATFORM]


def _exe(name: str) -> str:
    return f"{name}.exe" if IS_WINDOWS else name


def _bat(name: str) -> str:
    return f"{name}.bat" if IS_WINDOWS else name


def get_sdkmanager() -> Path:
    return get_sdk_root() / "cmdline-tools" / "latest" / "bin" / _bat("sdkmanager")


def get_avdmanager() -> Path:
    return get_sdk_root() / "cmdline-tools" / "latest" / "bin" / _bat("avdmanager")


def get_adb() -> Path:
    return get_sdk_root() / "platform-tools" / _exe("adb")


def get_emulator_bin() -> Path:
    return get_sdk_root() / "emulator" / _exe("emulator")


def get_cmdline_tools_url() -> str:
    return _CMDLINE_TOOLS_URLS[PLATFORM]


def get_system_image_package(api: int = DEFAULT_API_LEVEL) -> str:
    """Return the sdkmanager package string for a Google-APIs x86_64 image."""
    abi = "x86_64"
    return f"system-images;android-{api};google_apis;{abi}"


def find_executable(name: str) -> Path | None:
    """Search PATH for *name* (adds .exe on Windows)."""
    result = shutil.which(_exe(name))
    return Path(result) if result else None
