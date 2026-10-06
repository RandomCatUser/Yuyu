"""In-memory ring buffer of recent log lines, so the dashboard can show activity."""

from __future__ import annotations

import json
import logging
import sys
import threading
from datetime import datetime, timezone

LIMIT = 300
_lock = threading.Lock()
_lines: list[dict] = []


class _RingHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = record.getMessage()
        except Exception:  # pragma: no cover - defensive
            text = str(record.msg)
        push(record.levelname.lower(), text)


def push(level: str, text: str) -> None:
    with _lock:
        _lines.append(
            {"ts": datetime.now(timezone.utc).timestamp() * 1000, "level": level, "text": text}
        )
        if len(_lines) > LIMIT:
            del _lines[: len(_lines) - LIMIT]


def recent_logs(limit: int = LIMIT, since: float = 0) -> list[dict]:
    with _lock:
        return [line for line in _lines if line["ts"] >= since][-limit:]


def clear_logs() -> None:
    with _lock:
        _lines.clear()


def install(level: int = logging.INFO) -> logging.Logger:
    """Route stdlib logging (and print) into the buffer as well as the console."""
    # Her name can hold characters cp1252 cannot encode, turning every log line
    # into a UnicodeEncodeError. Force UTF-8; never let logging kill the process.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    root = logging.getLogger()
    if any(isinstance(h, _RingHandler) for h in root.handlers):
        return root
    root.setLevel(level)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(console)
    root.addHandler(_RingHandler())

    # A tiny helper so `log(...)` works without importing logging.
    class _Log:
        def __call__(self, message: str) -> None:
            root.info(message)

        def warn(self, message: str) -> None:
            root.warning(message)

        def error(self, message: str) -> None:
            root.error(message)

    return _Log()  # type: ignore[return-value]


def dumps(value) -> str:
    return json.dumps(value, default=str)
