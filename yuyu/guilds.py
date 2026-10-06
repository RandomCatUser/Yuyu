
from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timezone

_lock = threading.RLock()
_guilds: list[dict] = []
_loop: asyncio.AbstractEventLoop | None = None
_client = None


def attach(loop, client) -> None:
    global _guilds
    entries = [_describe(g) for g in (getattr(client, "guilds", None) or [])]
    entries.sort(key=lambda e: e["name"].lower())
    with _lock:
        _guilds = entries


def list_guilds() -> list[dict]:
    # Declared up front: this reads and replaces it, so without this the earlier
    # read becomes local and every leave dies with UnboundLocalError.
    global _guilds

    key = str(guild_id or "").strip()
    if not key.isdigit():
        return False, "that is not a server id"

    with _lock:
        loop, client = _loop, _client
        known = next((g for g in _guilds if g["id"] == key), None)

    if loop is None or client is None:
        return False, "the bot is not connected, so there is nowhere to leave"
    if known is None:
        return False, "that server is not one of hers"

    guild = client.get_guild(int(key))
    if guild is None:
        # The snapshot is stale: she was kicked while nobody looked.
        with _lock:
            _guilds = [g for g in _guilds if g["id"] != key]
        return False, "she is not in that server any more"

    async def _leave_one() -> None:
        await guild.leave()

    try:
        asyncio.run_coroutine_threadsafe(_leave_one(), loop).result(timeout=20)
    except Exception as exc:
        return False, f"Discord refused: {type(exc).__name__}: {exc}"

    # Drop it now rather than waiting for GUILD_DELETE, so the list the browser
    # refreshes from is already right.
    with _lock:
        _guilds = [g for g in _guilds if g["id"] != key]
    return True, f"left {known['name']}"
