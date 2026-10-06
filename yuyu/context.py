
from __future__ import annotations

import json
import threading
import time

from .config import MEMORY_DIR, config
from .util import atomic_write

_buffers: dict[str, dict] = {}
_lock = threading.Lock()
SUMMARY_FILE = MEMORY_DIR / "conversations.json"
_summaries: dict[str, dict] = {}
_summaries_loaded = False


def scope_of(guild_id, channel_id) -> tuple[str | None, str | None]:
    """Single source of truth for "where is this conversation"."""
    return (guild_id or None, channel_id or None)


def _key(guild_id, channel_id) -> str:
    return f"g:{guild_id}:{channel_id}" if guild_id else f"dm:{channel_id}"


def _load_summaries() -> None:
    global _summaries_loaded, _summaries
    if _summaries_loaded:
        return
    _summaries_loaded = True
    try:
        data = json.loads(SUMMARY_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[context] could not read {SUMMARY_FILE.name}: {type(exc).__name__}")
        return
    if isinstance(data, dict):
        _summaries = {
            key: value for key, value in data.items()
            if isinstance(key, str) and isinstance(value, dict)
            and isinstance(value.get("summary"), str)
            and isinstance(value.get("updated"), (int, float))
        }


def _save_summaries(summaries: dict[str, dict]) -> None:
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    atomic_write(SUMMARY_FILE, json.dumps(summaries, ensure_ascii=False, separators=(",", ":")))


def _summary_at(key: str, now: float) -> dict:
    global _summaries
    item = _summaries.get(key) or {}
    updated = float(item.get("updated") or 0)
    ttl = config["context"]["ttlMinutes"] * 60
    if item and updated and now - updated >= ttl:
        remaining = dict(_summaries)
        remaining.pop(key, None)
        try:
            _save_summaries(remaining)
        except OSError as exc:
            print(f"[context] could not expire {SUMMARY_FILE.name}: {type(exc).__name__}")
        _summaries = remaining
        return {}
    return item


def _prune(bucket: dict, now: float) -> bool:
    ttl = config["context"]["ttlMinutes"] * 60
    bucket["entries"] = [e for e in bucket["entries"] if now - e["ts"] < ttl]
    bucket["pending"] = [e for e in bucket["pending"] if now - e["ts"] < ttl]
    return bool(bucket["entries"])


def record(guild_id, channel_id, author: str, text: str, slug: str | None = None,
           author_id: int | None = None, is_bot: bool = False) -> None:
    key = _key(guild_id, channel_id)
    now = time.time()
    with _lock:
        bucket = _buffers.setdefault(key, {"entries": [], "pending": [], "lastCompacted": now})
        if not _prune(bucket, now):
            bucket["entries"] = []
            bucket["lastCompacted"] = now
        bucket["entries"].append(
            {"author": author, "slug": slug, "authorId": author_id, "text": text,
             "ts": now, "isBot": is_bot}
        )
        bucket["pending"].append(
            {"author": author, "text": text, "ts": now, "isBot": is_bot}
        )
        limit = config["context"]["maxMessages"]
        if len(bucket["entries"]) > limit:
            bucket["entries"] = bucket["entries"][-limit:]
        pending_limit = max(limit * 4, 120)
        if len(bucket["pending"]) > pending_limit:
            bucket["pending"] = bucket["pending"][-pending_limit:]


def recent(guild_id, channel_id, limit: int | None = None) -> list[dict]:
    key = _key(guild_id, channel_id)
    now = time.time()
    with _lock:
        bucket = _buffers.get(key)
        if not bucket:
            return []
        if not _prune(bucket, now):
            _buffers.pop(key, None)
            return []
        limit = limit or config["context"]["maxMessages"]
        return list(bucket["entries"][-limit:])


def recent_text(guild_id, channel_id, limit: int | None = None) -> str:
    return "\n".join(e["text"] for e in recent(guild_id, channel_id, limit))


def summary_for(guild_id, channel_id) -> str:
    global _summaries
    key = _key(guild_id, channel_id)
    now = time.time()
    with _lock:
        _load_summaries()
        item = _summary_at(key, now)
        if not item:
            return ""
        return str(item.get("summary") or "")


def compaction_batch(guild_id, channel_id) -> dict | None:
    """Return due older messages while retaining the newest few as raw context."""
    key = _key(guild_id, channel_id)
    now = time.time()
    with _lock:
        bucket = _buffers.get(key)
        if not bucket:
            return None
        _prune(bucket, now)
        if not bucket["pending"]:
            return None
        _load_summaries()
        prior = _summary_at(key, now)
        interval = max(1, int(config["context"].get("summaryIntervalMinutes", 30))) * 60
        last_compacted = float(prior.get("updated") or bucket.get("lastCompacted") or now)
        if now - last_compacted < interval:
            return None
        keep = max(1, int(config["context"].get("summaryKeepRecentMessages", 8)))
        candidates = bucket["pending"][:-keep]
        if not candidates:
            return None
        bucket["lastCompacted"] = now
        return {
            "summary": str(prior.get("summary") or ""),
            "entries": [dict(entry) for entry in candidates],
            "through": candidates[-1]["ts"],
        }


def save_summary(guild_id, channel_id, summary: str, through: float) -> None:
    global _summaries
    key = _key(guild_id, channel_id)
    now = time.time()
    with _lock:
        bucket = _buffers.get(key)
        _load_summaries()
        text = summary.strip()[: int(config["context"].get("summaryMaxChars", 1200))]
        updated_summaries = dict(_summaries)
        if text:
            updated_summaries[key] = {"summary": text, "updated": now}
        else:
            updated_summaries.pop(key, None)
        _save_summaries(updated_summaries)
        _summaries = updated_summaries
        if bucket:
            bucket["pending"] = [entry for entry in bucket["pending"] if entry["ts"] > through]
            bucket["lastCompacted"] = now


def clear(guild_id, channel_id) -> None:
    global _summaries
    with _lock:
        key = _key(guild_id, channel_id)
        _load_summaries()
        if key in _summaries:
            updated_summaries = dict(_summaries)
            updated_summaries.pop(key, None)
            _save_summaries(updated_summaries)
            _summaries = updated_summaries
        _buffers.pop(key, None)


def clear_all() -> int:
    with _lock:
        _load_summaries()
        count = len(set(_buffers) | set(_summaries))
        if _summaries:
            _save_summaries({})
        _buffers.clear()
        if _summaries:
            _summaries.clear()
        return count


def reload_summaries() -> None:
    """Reload persisted summaries after a backup restore."""
    global _summaries, _summaries_loaded
    with _lock:
        _summaries = {}
        _summaries_loaded = False
        _load_summaries()


def stats() -> list[dict]:
    with _lock:
        _load_summaries()
        keys = set(_buffers) | set(_summaries)
        return [
            {
                "key": key,
                "entries": len(_buffers.get(key, {}).get("entries", [])),
                "summary": bool((_summaries.get(key) or {}).get("summary")),
            }
            for key in sorted(keys)
        ]
