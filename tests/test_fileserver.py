import sys
import urllib.request

import pytest

from harness_android.fileserver import FileServer


def _free_port():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_serve_and_stop(tmp_path):
    (tmp_path / "hello.js").write_text("console.log('hi')")
    port = _free_port()
    with FileServer(tmp_path, port=port, bind="127.0.0.1") as srv:
        assert srv.local_url == f"http://localhost:{port}"
        assert srv.device_url == f"http://127.0.0.1:{port}"
        assert srv.emulator_url == f"http://10.0.2.2:{port}"

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/hello.js", timeout=5) as r:
            body = r.read().decode()
            headers = {k.lower(): v for k, v in r.getheaders()}
        assert "console.log" in body
        assert headers.get("access-control-allow-origin") == "*"
        assert "no-store" in headers.get("cache-control", "")
        assert "javascript" in headers.get("content-type", "")

    # After stop(), the port should be released (no exception).
    assert srv._httpd is None


def test_start_missing_dir_raises(tmp_path):
    srv = FileServer(tmp_path / "nope")
    with pytest.raises(FileNotFoundError):
        srv.start()


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="SO_REUSEADDR on Windows permits a second bind to the same port",
)
def test_bind_in_use_raises(tmp_path):
    (tmp_path / "x").write_text("x")
    port = _free_port()
    a = FileServer(tmp_path, port=port, bind="127.0.0.1")
    a.start()
    b = FileServer(tmp_path, port=port, bind="127.0.0.1")
    try:
        with pytest.raises(RuntimeError, match="Cannot bind"):
            b.start()
    finally:
        if b._httpd:
            b.stop()
        a.stop()
