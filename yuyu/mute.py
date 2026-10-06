
from __future__ import annotations

import json
import threading

from .config import ROOT
from .util import atomic_write

MUTED_FILE = ROOT / "muted.json"

_lock = threading.RLock()
_cache: dict | None = None


def _read() -> dict:
    global _cache
    with _lock:
        _cache = data
        atomic_write(MUTED_FILE, json.dumps(data, indent=2, sort_keys=True) + "\n")


def invalidate() -> None:
    """Forget the cached file - used by tests and after an outside edit."""
    global _cache
    with _lock:
        _cache = None


def muted_slugs() -> set[str]:
    """Everyone she will not answer."""
    with _lock:
        return set(_read()["muted"])


def is_muted(slug) -> bool:
    if not slug:
        return False
    with _lock:
        return str(slug).strip().lower() in _read()["muted"]


def set_muted(slug, muted: bool) -> set[str]:
    """Add or drop one person from the list. Returns the new set."""
    key = str(slug or "").strip().lower()
    if not key:
        return muted_slugs()
    with _lock:
        current = list(_read()["muted"])
        if muted and key not in current:
            current.append(key)
        elif not muted and key in current:
            current.remove(key)
        _write({"muted": sorted(current)})
        return set(_read()["muted"])
