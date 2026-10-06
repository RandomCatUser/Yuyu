
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


def test_leaving_runs_the_coroutine_on_the_bots_own_loop():
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
