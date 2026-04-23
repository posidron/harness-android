"""Shared Rich console — UTF-8 safe on Windows, single instance for the whole package.

Rationale
---------
* Windows default console codec is cp1252 → `→` (U+2192) and other fancy chars
  silently crash with ``UnicodeEncodeError`` when stdout is piped (non-TTY).
* Each module used to construct its own ``Console()``. Now they share one.
"""

from __future__ import annotations

import io
import sys

from rich.console import Console


def _make_console() -> Console:
    """Return a shared Console that never crashes on Windows cp1252.

    Strategy: keep the terminal's native encoding (so box-drawing renders
    correctly in the TTY it actually is), but set ``errors='replace'`` so
    any char the stream can't encode becomes ``?`` instead of raising
    ``UnicodeEncodeError``. This fixes the `→` (U+2192) crashes on
    Windows without producing mojibake in cp1252 terminals.
    """
    stream = sys.stdout
    enc = (getattr(stream, "encoding", None) or "utf-8")
    # Only reconfigure errors handling; keep encoding as-is.
    try:
        stream.reconfigure(errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        try:
            stream = io.TextIOWrapper(
                stream.buffer, encoding=enc, errors="replace", line_buffering=True
            )
        except Exception:  # noqa: BLE001
            pass
    return Console(file=stream, soft_wrap=True)


console: Console = _make_console()
