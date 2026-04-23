"""HTTP file server for serving local directories to the emulator/device.

The Android emulator reaches the host at ``10.0.2.2``; physical devices
can use ``adb reverse`` and reach it at ``127.0.0.1``.  CORS headers are
sent so MojoJS bindings (``*.mojom.m.js``) load cross-origin.
"""

from __future__ import annotations

import functools
import http.server
import socketserver
import threading
from pathlib import Path

from harness_android.console import console



class _Handler(http.server.SimpleHTTPRequestHandler):
    extensions_map = {
        **http.server.SimpleHTTPRequestHandler.extensions_map,
        ".js": "text/javascript",
        ".mjs": "text/javascript",
        ".json": "application/json",
    }

    def end_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass


class _ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class FileServer:
    """Serve a local directory over HTTP for the device to access."""

    def __init__(self, directory: str | Path, port: int = 8089, bind: str = "0.0.0.0"):
        self.directory = Path(directory).resolve()
        self.port = port
        self.bind = bind
        self._httpd: _ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if not self.directory.is_dir():
            raise FileNotFoundError(f"Directory not found: {self.directory}")
        handler = functools.partial(_Handler, directory=str(self.directory))
        try:
            self._httpd = _ThreadingHTTPServer((self.bind, self.port), handler)
        except OSError as exc:
            raise RuntimeError(
                f"Cannot bind {self.bind}:{self.port} — {exc}. "
                "Pick another port or stop the conflicting server."
            ) from exc
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        console.print(
            f"[green]File server: http://localhost:{self.port}/  "
            f"(emulator: http://10.0.2.2:{self.port}/, "
            f"adb-reverse: http://127.0.0.1:{self.port}/)\n"
            f"  Serving: {self.directory}"
        )

    def stop(self) -> None:
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
        console.print("[dim]File server stopped.")

    @property
    def emulator_url(self) -> str:
        return f"http://10.0.2.2:{self.port}"

    @property
    def device_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def local_url(self) -> str:
        return f"http://localhost:{self.port}"

    def __enter__(self) -> "FileServer":
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()
