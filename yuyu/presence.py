"""Rich presence: what Discord shows on her profile.

The dashboard is a Flask thread and change_presence() belongs to the event loop,
so a request schedules onto that loop and waits for the result. Presence is
public, so it may say she is busy but never who with."""

from __future__ import annotations

import asyncio
import threading
import time

import discord

from . import guilds as guild_mod
from .config import build_presence

MIN_INTERVAL = 45.0  # seconds between unsolicited pushes
ACTIVE_FOR = 300.0  # "chatting" for this long after her last reply

_lock = threading.Lock()
_loop: asyncio.AbstractEventLoop | None = None
_client: discord.Client | None = None
_last_push = 0.0
_last_sent: dict = {}
_last_activity = 0.0

_ACTIVITY_TYPES = {
    "playing": discord.ActivityType.playing,
    "watching": discord.ActivityType.watching,
    "listening": discord.ActivityType.listening,
    "competing": discord.ActivityType.competing,
    "custom": discord.ActivityType.custom,
}
_STATUSES = {
    "online": discord.Status.online,
    "idle": discord.Status.idle,
    "dnd": discord.Status.dnd,
    "invisible": discord.Status.invisible,
}


def activity_label() -> str:
    """What the `{activity}` placeholder says.

    Only ever claims that she is busy, never with whom: a profile card is
    visible to everyone in every server she is in.
    """
    idle = time.time() - _last_activity
    return "chatting right now" if idle < ACTIVE_FOR else "hanging out"


def note_activity() -> None:
    """Called whenever she actually answers somebody."""
    global _last_activity
    _last_activity = time.time()


def attach(loop, client) -> None:
    """Remember the loop and client, so a dashboard request can push an edit."""
    global _loop, _client
    with _lock:
        _loop, _client = loop, client


def detach() -> None:
    """Forget them. Used on shutdown and between tests."""
    global _loop, _client
    with _lock:
        _loop, _client = None, None


def live_values() -> dict:
    """The placeholders only the running bot can answer."""
    servers = len(guild_mod.list_guilds())
    return {
        "servers": f"{servers} servers" if servers else "",
        "activity": activity_label(),
    }


def preview() -> dict:
    """Exactly what would be sent - this is what the panel shows."""
    return build_presence(live_values())


async def _push(client: discord.Client, wanted: dict) -> None:
    activity = discord.Activity(
        type=_ACTIVITY_TYPES.get(wanted["type"], discord.ActivityType.watching),
        name=wanted["name"],
        details=wanted["details"] or None,
        state=wanted["state"] or None,
        assets={
            "large_image": wanted.get("largeImage") or None,
            "large_text": wanted.get("largeText") or wanted.get("name") or None,
            "small_image": wanted.get("smallImage") or None,
            "small_text": wanted.get("smallText") or None,
        },
    )
    await client.change_presence(
        status=_STATUSES.get(wanted["status"], discord.Status.online), activity=activity
    )


async def refresh(client: discord.Client, *, force: bool = False) -> bool:
    """Recompute and push, but only if something actually changed.

    Throttled rather than merely rate-limited: she replies to every message, and
    re-sending an identical presence each time is a gateway packet per reply for
    no visible difference. The check comes before the work, so a quiet bot does
    not scan the memory folder either.
    """
    global _last_push, _last_sent
    now = time.time()
    if not force and now - _last_push < MIN_INTERVAL:
        return False  # her next reply, past the window, will finish the job
    wanted = preview()
    if wanted == _last_sent:
        return False
    await _push(client, wanted)
    _last_sent, _last_push = wanted, now
    return True


def apply_now() -> tuple[bool, str]:
    """Push an edited presence from the dashboard's Flask thread.

    Returns (ok, why). The coroutine is scheduled onto the bot's own loop -
    `change_presence()` talks to Discord over a connection this thread does not
    own - and the wait is deliberate, so the panel shows "updated" only once
    Discord has it.
    """
    global _last_push, _last_sent
    with _lock:
        loop, client = _loop, _client
    if loop is None or client is None:
        return False, "the bot is not connected, so nothing is showing a profile"
    wanted = preview()

    async def _send() -> None:
        await _push(client, wanted)

    try:
        asyncio.run_coroutine_threadsafe(_send(), loop).result(timeout=10)
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    _last_sent, _last_push = wanted, time.time()
    return True, "updated"
