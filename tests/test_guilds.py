"""The server list, and the way out of one.

    python -m pytest tests/test_guilds.py -q

The dashboard runs in a Flask thread while the Discord client lives on the
event loop, so this module exists to keep those apart: a request may read a
plain list of dicts, and leaving must schedule the coroutine onto the loop
that actually owns the Discord connection.
"""

from __future__ import annotations

import asyncio
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

import pytest

from yuyu import guilds


class FakeIcon:
    url = "https://cdn.discordapp.com/icons/1/abc.png"


class FakeGuild:
    """Just enough of discord.Guild to be described and left."""

    def __init__(self, gid, name="The Loft", members=42, icon=None, joined=None, channels=None):
        self.id = gid
        self.name = name
        self.member_count = members
        self.icon = icon
        self.joined_at = joined
        self.channels = channels if channels is not None else [object(), object(), object()]
        self.left = False

    async def leave(self):
        self.left = True


class FakeClient:
    def __init__(self, guild_list, user=None):
        self.guilds = guild_list
        self.user = user

    def get_guild(self, gid):
        return next((g for g in self.guilds if g.id == gid), None)


@pytest.fixture(autouse=True)
def clean():
    guilds.detach()
    yield
    guilds.detach()


@contextmanager
def running_loop():
    """A real event loop on its own thread - the shape the bot actually has."""
    loop = asyncio.new_event_loop()
    started = threading.Event()

    def run():
        asyncio.set_event_loop(loop)
        loop.call_soon(started.set)
        loop.run_forever()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert started.wait(5), "the loop never started"
    try:
        yield loop
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        loop.close()


# --- describing -------------------------------------------------------------

def test_the_snapshot_is_sorted_and_ids_come_down_as_strings():
    """Snowflakes do not fit in a JS number, so the id must stay a string all
    the way to the browser or the Leave button posts a rounded id."""
    client = FakeClient([FakeGuild(2, "zzz"), FakeGuild(1, "Alpha", members=7)])

    guilds.update(client)
    got = guilds.list_guilds()

    assert [g["name"] for g in got] == ["Alpha", "zzz"], "sorted for display"
    assert all(isinstance(g["id"], str) for g in got)
    assert got[0]["members"] == 7


def test_a_half_populated_guild_does_not_break_serialisation():
    """Discord omits icons on servers with none, and joined_at can be missing."""

    class Bare:
        id = 9
        name = "Bare"

    guilds.update(FakeClient([Bare()]))
    [got] = guilds.list_guilds()

    assert got["icon"] == ""
    assert got["members"] == 0
    assert got["channels"] == 0
    assert got["joined"] == ""


def test_icon_and_join_date_are_pulled_through():
    g = FakeGuild(1, "The Loft", icon=FakeIcon(),
                  joined=datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc))

    guilds.update(FakeClient([g]))
    [got] = guilds.list_guilds()

    assert got["icon"].startswith("https://cdn.discordapp.com/")
    assert got["joined"].startswith("2026-01-02")


def test_callers_get_a_copy_so_a_refresh_underneath_them_is_harmless():
    guilds.update(FakeClient([FakeGuild(1, "One")]))

    snapshot = guilds.list_guilds()
    snapshot.clear()

    assert len(guilds.list_guilds()) == 1, "the caller must not be able to empty the real list"


def test_an_empty_client_reads_as_no_servers():
    guilds.update(FakeClient([]))
    assert guilds.list_guilds() == []


def test_detach_forgets_everything():
    guilds.attach(object(), FakeClient([FakeGuild(1, "One")]))
    guilds.update(FakeClient([FakeGuild(1, "One")]))

    guilds.detach()

    assert guilds.list_guilds() == []
    assert guilds.leave(1) == (False, "the bot is not connected, so there is nowhere to leave")


# --- refusing --------------------------------------------------------------

def test_a_junk_id_is_refused_before_anything_else_touches_the_client():
    assert guilds.leave("not-an-id") == (False, "that is not a server id")
    assert guilds.leave("") == (False, "that is not a server id")
    assert guilds.leave(None) == (False, "that is not a server id")


def test_leaving_somewhere_that_is_not_ours_is_refused():
    client = FakeClient([])
    guilds.attach(object(), client)
    guilds.update(client)

    ok, why = guilds.leave(999)

    assert ok is False
    assert "not one of hers" in why


def test_a_stale_entry_drops_out_when_she_is_already_gone():
    """Kicked while nobody was watching: the snapshot lags reality."""
    client = FakeClient([FakeGuild(1, "Gone")])
    guilds.attach(object(), client)
    guilds.update(client)
    client.guilds = []  # she is out; the list still says otherwise

    ok, why = guilds.leave(1)

    assert ok is False
    assert "not in that server any more" in why
    assert guilds.list_guilds() == [], "the stale row should clear itself"


# --- actually leaving -------------------------------------------------------

def test_leaving_runs_the_coroutine_on_the_bots_own_loop():
    """The whole reason this module exists.

    Awaiting guild.leave() in the request thread would run it on a closed or
    wrong event loop, so it has to be scheduled onto the loop that owns the
    connection - and the caller has to wait, or the UI refreshes before she is
    actually gone.
    """
    guild = FakeGuild(555, "The Loft")
    client = FakeClient([guild])

    with running_loop() as loop:
        guilds.attach(loop, client)
        guilds.update(client)

        ok, why = guilds.leave(555)

        assert ok, why
        assert guild.left, "the coroutine never ran"
        assert guilds.list_guilds() == [], "the row should be gone the moment it succeeds"


def test_leaving_one_server_leaves_every_other_alone():
    keep = FakeGuild(1, "Keep")
    gone = FakeGuild(2, "Gone")
    client = FakeClient([keep, gone])

    with running_loop() as loop:
        guilds.attach(loop, client)
        guilds.update(client)

        ok, why = guilds.leave(2)

        assert ok, why
        assert keep.left is False, "she must not walk out of every server"
        assert [g["name"] for g in guilds.list_guilds()] == ["Keep"]


def test_when_discord_refuses_the_entry_stays_where_it_is():
    class Angry(FakeGuild):
        async def leave(self):
            raise RuntimeError("missing permissions")

    guild = Angry(3, "Angry")
    client = FakeClient([guild])

    with running_loop() as loop:
        guilds.attach(loop, client)
        guilds.update(client)

        ok, why = guilds.leave(3)

        assert ok is False
        assert "missing permissions" in why
        assert [g["name"] for g in guilds.list_guilds()] == ["Angry"], "she did not actually leave"
