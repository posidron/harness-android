"""Simple HTTP file server for serving local directories to the emulator.

The Android emulator reaches the host at ``10.0.2.2``, so a local HTTP
server on the host can serve any directory (e.g. a Chromium ``gen/``
folder with MojoJS bindings) directly to Chrome on the device.

Usage::

    from harness_android.fileserver import FileServer

    server = FileServer("/path/to/gen", port=8089)
    server.start()
    # Chrome on emulator loads http://10.0.2.2:8089/mojo/public/js/bindings.js
    server.stop()
"""

from __future__ import annotations

import functools
import http.server
import threading
from pathlib import Path

from rich.console import Console

console = Console()


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    """Serves files without printing every request to stderr."""

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass  # silence


class FileServer:
    """Serve a local directory over HTTP for the emulator to access.

    The emulator sees the host as ``10.0.2.2``, so files are reachable
    at ``http://10.0.2.2:<port>/<path>``.
    """

    def __init__(self, directory: str | Path, port: int = 8089, bind: str = "0.0.0.0"):
        self.directory = Path(directory).resolve()
        self.port = port
        self.bind = bind
        self._httpd: http.server.HTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start serving in a background thread."""
        if not self.directory.is_dir():
            raise FileNotFoundError(f"Directory not found: {self.directory}")

        handler = functools.partial(_QuietHandler, directory=str(self.directory))
        self._httpd = http.server.HTTPServer((self.bind, self.port), handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        console.print(
            f"[green]File server started: http://localhost:{self.port}/\n"
            f"  Serving: {self.directory}\n"
            f"  Emulator: http://10.0.2.2:{self.port}/"
        )

    def stop(self) -> None:
        """Shut down the server."""
        if self._httpd:
            self._httpd.shutdown()
            self._httpd = None
            self._thread = None
            console.print("[dim]File server stopped.")

    @property
    def emulator_url(self) -> str:
        """Base URL as seen from the emulator."""
        return f"http://10.0.2.2:{self.port}"

    @property
    def local_url(self) -> str:
        return f"http://localhost:{self.port}"

    def __enter__(self) -> "FileServer":
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()
