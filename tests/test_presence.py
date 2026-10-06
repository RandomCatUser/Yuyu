
from __future__ import annotations

import asyncio
import time

import pytest

import yuyu.config as config_mod
from yuyu import chat, presence
from yuyu import memory as mem
from yuyu.config import PREFIX, build_presence


def _user(name: str, display: str, uid: int, bot: bool = False):
    class U:
        pass

    u = U()
    u.name = name
    u.display_name = display
    u.id = uid
    u.bot = bot
    return u


# presence

def test_presence_is_filled_in_ready_to_send():
    monkeypatch.setitem(config_mod.config["bot"], "presence",
                        dict(config_mod.DEFAULTS["bot"]["presence"]))


def test_default_presence_uses_empty_registered_asset_keys(stock_presence):
    p = build_presence()
    assert p["type"] == "watching"
    assert p["name"] == "the server"
    assert p["largeImage"] == ""
    assert p["smallImage"] == ""


def test_the_two_lines_follow_what_is_actually_happening(stock_presence):
    monkeypatch.setattr(mem, "MEMORY_DIR", tmp_path)
    user = _user("newbie", "Newbie", 4242)

    chat.remember_person(user)

    person = mem.read_person(chat.slug_for(user))
    assert person is not None, "a person who spoke must have a file"
    assert person["name"] == "Newbie"
    assert person["discordId"] == "4242"
    assert mem.count_facts(person) == 0, "an empty record is still a record"


def test_a_later_message_never_wipes_what_is_already_known(tmp_path, monkeypatch):
    monkeypatch.setattr(mem, "MEMORY_DIR", tmp_path)
    user = _user("newbie", "Newbie", 4242)
    chat.remember_person(user)
    slug = chat.slug_for(user)
    mem.write_person(slug, {"name": "Newbie"}, {"notes": ["hates mornings"]})

    chat.remember_person(user)  # they say hello again

    notes = mem.read_person(slug)["notes"]
    # normalize_fact capitalises on the way in, so compare on its terms; the
    # point is that the fact survives at all.
    assert len(notes) == 1 and notes[0].lower() == "hates mornings"


def test_bots_are_not_given_memory_files(tmp_path, monkeypatch):
    monkeypatch.setattr(mem, "MEMORY_DIR", tmp_path)
    chat.remember_person(_user("otherbot", "OtherBot", 99, bot=True))
    assert list(tmp_path.glob("*.md")) == []
