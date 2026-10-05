"""Which servers she is in, and the way out of one.

Only events touch the Discord client: the list is a snapshot rebuilt on join and
leave, and leaving hands the coroutine back to the loop. client.guilds is derived
on every access and can raise mid-iteration from the wrong thread; get_guild()
is a plain dict lookup and is safe anywhere."""

from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timezone

_lock = threading.RLock()
_guilds: list[dict] = []
_loop: asyncio.AbstractEventLoop | None = None
_client = None


def attach(loop, client) -> None:
    """Remember the event loop and client, so a dashboard request can ask to leave."""
    global _loop, _client
    with _lock:
        _loop = loop
        _client = client


def detach() -> None:
    """Forget everything. Used on shutdown and between tests."""
    global _loop, _client, _guilds
    with _lock:
        _loop = None
        _client = None
        _guilds = []


def _describe(guild) -> dict:
    """One server as a plain dict the dashboard can serialise as-is."""
    icon = ""
    try:
        asset = getattr(guild, "icon", None)
        if asset:
            icon = str(asset.url)
    except Exception:
        icon = ""

    joined = ""
    when = getattr(guild, "joined_at", None)
    if isinstance(when, datetime):
        joined = when.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M")
    elif when:
        joined = str(when)

    try:
        members = int(getattr(guild, "member_count", 0) or 0)
    except (TypeError, ValueError):
        members = 0
    channels = getattr(guild, "channels", None)

    return {
        "id": str(getattr(guild, "id", "")),
        "name": str(getattr(guild, "name", "") or "unknown"),
        "icon": icon,
        "members": members,
        "channels": len(channels) if channels is not None else 0,
        "joined": joined,
    }


def update(client) -> None:
    """Rebuild the snapshot from whatever the client currently holds.

    Only ever called from the event loop's own thread - see the module note.
    """
    global _guilds
    entries = [_describe(g) for g in (getattr(client, "guilds", None) or [])]
    entries.sort(key=lambda e: e["name"].lower())
    with _lock:
        _guilds = entries


def list_guilds() -> list[dict]:
    """A copy, so a request can walk it while the loop writes a new one."""
    with _lock:
        return [dict(g) for g in _guilds]


def leave(guild_id) -> tuple[bool, str]:
    """Make the bot leave one server. Returns (ok, why-not).

    Admin-gated by the caller and confirmed in the browser, because an invite
    link is the only way back in. The coroutine is scheduled onto the bot's own
    loop: `guild.leave()` talks to Discord over a connection this thread does
    not own, so awaiting it here would run it on the wrong event loop.
    """
    # Declared up front because this function both reads and replaces it below.
    # Without this, `_guilds = [...]` makes the earlier read a local lookup and
    # every leave dies with UnboundLocalError.
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
        # The snapshot is stale - she was kicked while nobody was looking.
        with _lock:
            _guilds = [g for g in _guilds if g["id"] != key]
        return False, "she is not in that server any more"

    async def _leave_one() -> None:
        await guild.leave()

    try:
        asyncio.run_coroutine_threadsafe(_leave_one(), loop).result(timeout=20)
    except Exception as exc:
        return False, f"Discord refused: {type(exc).__name__}: {exc}"

    # Drop it now rather than waiting for GUILD_DELETE to arrive, so the list
    # the browser refreshes from is already right. on_guild_remove reconciles.
    with _lock:
        _guilds = [g for g in _guilds if g["id"] != key]
    return True, f"left {known['name']}"
