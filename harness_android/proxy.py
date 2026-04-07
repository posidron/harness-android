"""HTTP(S) proxy integration and CA certificate management.

Supports routing emulator traffic through an intercepting proxy (mitmproxy,
Burp Suite, ZAP, etc.) and auto-installing a CA certificate so TLS
interception works transparently.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from rich.console import Console

from harness_android.adb import ADB

console = Console()

# Default proxy address — the emulator sees the host as 10.0.2.2
EMULATOR_HOST_LOOPBACK = "10.0.2.2"
DEFAULT_PROXY_PORT = 8080


class Proxy:
    """Configure the Android emulator to route traffic through a proxy."""

    def __init__(self, adb: ADB, host: str = EMULATOR_HOST_LOOPBACK, port: int = DEFAULT_PROXY_PORT):
        self.adb = adb
        self.host = host
        self.port = port

    # ------------------------------------------------------------------
    # Proxy toggle
    # ------------------------------------------------------------------

    def enable(self) -> None:
        """Set the global HTTP proxy on the device."""
        proxy = f"{self.host}:{self.port}"
        self.adb.shell("settings", "put", "global", "http_proxy", proxy)
        console.print(f"[green]Proxy set to {proxy}")

    def disable(self) -> None:
        """Remove the global HTTP proxy."""
        self.adb.shell("settings", "put", "global", "http_proxy", ":0")
        console.print("[yellow]Proxy disabled.")

    def get_current(self) -> str:
        """Return the current proxy setting."""
        return self.adb.shell("settings", "get", "global", "http_proxy").strip()

    # ------------------------------------------------------------------
    # CA certificate installation
    # ------------------------------------------------------------------

    def install_ca_cert(self, cert_path: str | Path) -> None:
        """Install a CA certificate into the system trust store.

        The emulator must be running with a writable system partition
        (``-writable-system``), or this uses the Android user CA store
        as fallback.  The cert should be PEM or DER format.
        """
        cert_path = Path(cert_path)
        if not cert_path.exists():
            raise FileNotFoundError(f"Certificate not found: {cert_path}")

        # Convert to PEM if needed and compute the hash-based filename
        pem_data = cert_path.read_bytes()
        hash_name = self._compute_cert_hash(cert_path)

        remote_tmp = f"/sdcard/{hash_name}"
        self.adb.push(cert_path, remote_tmp)

        # Try system store first (needs root + writable /system)
        system_cert_dir = "/system/etc/security/cacerts"
        result = self.adb.run(
            "shell", f"mount -o rw,remount /system 2>/dev/null; "
                     f"cp {remote_tmp} {system_cert_dir}/{hash_name} && "
                     f"chmod 644 {system_cert_dir}/{hash_name}",
            check=False,
        )
        if result.returncode == 0:
            self.adb.shell("rm", remote_tmp)
            console.print(f"[green]CA cert installed to system store as {hash_name}")
            return

        # Fallback: user CA store via settings intent
        self.adb.shell(
            "am", "start", "-a", "android.credentials.INSTALL",
            "-t", "application/x-x509-ca-cert",
            "-d", f"file://{remote_tmp}",
        )
        console.print(
            "[yellow]CA cert pushed. You may need to accept it manually in "
            "Settings → Security → Encryption & credentials → Install a certificate."
        )

    def install_mitmproxy_ca(self) -> None:
        """Download and install the mitmproxy CA cert.

        Assumes mitmproxy is running on the host. The CA cert is available
        at ``http://mitm.it/cert/pem`` when the proxy is active, or from
        the default location ``~/.mitmproxy/mitmproxy-ca-cert.pem``.
        """
        ca_path = Path.home() / ".mitmproxy" / "mitmproxy-ca-cert.pem"
        if ca_path.exists():
            console.print(f"[dim]Using mitmproxy CA from {ca_path}")
            self.install_ca_cert(ca_path)
            return

        # Try fetching from the running proxy
        import requests
        try:
            resp = requests.get(
                f"http://{self.host}:{self.port}/cert/pem",
                timeout=5,
                proxies={"http": f"http://localhost:{self.port}"},
            )
            resp.raise_for_status()
            with tempfile.NamedTemporaryFile(suffix=".pem", delete=False) as f:
                f.write(resp.content)
                tmp = Path(f.name)
            self.install_ca_cert(tmp)
            tmp.unlink(missing_ok=True)
        except Exception as exc:
            raise RuntimeError(
                f"Could not find mitmproxy CA cert at {ca_path} or fetch from proxy: {exc}"
            ) from exc

    def _compute_cert_hash(self, cert_path: Path) -> str:
        """Compute the OpenSSL subject_hash_old filename for a cert.

        Android expects system CA certs named ``<hash>.0``.
        Falls back to the original filename if openssl is unavailable.
        """
        openssl = shutil.which("openssl")
        if openssl:
            result = subprocess.run(
                [openssl, "x509", "-inform", "PEM", "-subject_hash_old", "-noout",
                 "-in", str(cert_path)],
                capture_output=True, text=True,
            )
            if result.returncode == 0:
                return result.stdout.strip() + ".0"
        return cert_path.stem + ".0"

    # ------------------------------------------------------------------
    # Traffic capture
    # ------------------------------------------------------------------

    def start_tcpdump(self, remote_path: str = "/sdcard/capture.pcap") -> str:
        """Start tcpdump on the device in the background. Returns remote path."""
        self.adb.run(
            "shell",
            f"nohup tcpdump -i any -w {remote_path} &",
            check=False,
        )
        console.print(f"[green]tcpdump started → {remote_path}")
        return remote_path

    def stop_tcpdump(self) -> None:
        self.adb.shell("pkill", "-f", "tcpdump")
        console.print("[yellow]tcpdump stopped.")

    def pull_capture(self, remote: str = "/sdcard/capture.pcap", local: str = "capture.pcap") -> Path:
        local_path = Path(local)
        self.adb.pull(remote, local_path)
        console.print(f"[green]Capture saved to {local_path}")
        return local_path

    # ------------------------------------------------------------------
    # DNS manipulation
    # ------------------------------------------------------------------

    def add_hosts_entry(self, ip: str, hostname: str) -> None:
        """Add an entry to /etc/hosts on the emulator (needs root)."""
        self.adb.run(
            "shell",
            f"echo '{ip} {hostname}' >> /etc/hosts",
        )
        console.print(f"[green]Added hosts entry: {ip} → {hostname}")

    def show_hosts(self) -> str:
        return self.adb.shell("cat", "/etc/hosts")

    def reset_hosts(self) -> None:
        """Reset /etc/hosts to default."""
        default = "127.0.0.1       localhost\n::1             ip6-localhost\n"
        self.adb.run("shell", f"echo '{default}' > /etc/hosts")
        console.print("[yellow]Hosts file reset.")
