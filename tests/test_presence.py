"""Rich presence, and everyone who chats being kept on file.

    python -m pytest tests/test_presence.py -q
"""

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


# --- presence ----------------------------------------------------------------

def test_presence_is_filled_in_ready_to_send():
    """Rich presence: the {prefix} placeholder must be resolved, and the status
    must be one Discord actually understands, or change_presence rejects it."""
    p = build_presence()
    assert p["type"] in ("playing", "watching", "listening", "competing", "custom")
    assert p["status"] in ("online", "idle", "dnd", "invisible")
    assert "{prefix}" not in p["name"]
    assert "{prefix}" not in p["state"]
    assert PREFIX in p["state"] or p["state"] == ""
    assert p["name"], "an activity with no name renders as no presence at all"


@pytest.fixture
def stock_presence(monkeypatch):
    """The shipped default presence, not whatever config.json happens to say.

    These two tests are about how placeholders resolve and about asset keys
    being empty by default. Reading the live config made them fail the moment
    the presence was customised, which is not a bug in anything.
    """
    monkeypatch.setitem(config_mod.config["bot"], "presence",
                        dict(config_mod.DEFAULTS["bot"]["presence"]))


def test_default_presence_uses_empty_registered_asset_keys(stock_presence):
    p = build_presence()
    assert p["type"] == "watching"
    assert p["name"] == "the server"
    assert p["largeImage"] == ""
    assert p["smallImage"] == ""


def test_the_two_lines_follow_what_is_actually_happening(stock_presence):
    """Presence used to be a fixed string, so her profile said the same thing
    whether she was mid-sentence or had been quiet all week."""
    p = build_presence(live={"servers": "3 servers", "activity": "chatting right now"})
    assert "chatting right now" in p["details"]
    assert "3 servers" in p["state"] and PREFIX in p["state"]


def test_a_missing_placeholder_leaves_no_stray_separator():
    """`{people} · {activity}` with the activity unknown must not render as a
    trailing `·` - or, with nothing at all known, as a lonely dot."""
    p = build_presence(live={})
    if p["details"]:
        assert all(part.strip() for part in p["details"].split(" · ")), p["details"]
    # The server count is absent, but the command hint still has to survive.
    assert p["state"] == f"{PREFIX}help for commands", p["state"]


def test_an_unknown_placeholder_disappears_rather_than_leaking(monkeypatch):
    monkeypatch.setitem(config_mod.config["bot"], "presence", {
        "status": "online", "type": "watching", "name": "{name}",
        "details": "hi {nope}", "state": "ok {nope}",
    })
    p = build_presence()
    assert (p["details"], p["state"]) == ("hi", "ok")
    assert p["name"], "the known placeholder still resolves"


def test_a_stray_brace_is_handed_back_untouched(monkeypatch):
    """Someone's hand-edited config is allowed to have an unbalanced brace."""
    monkeypatch.setitem(config_mod.config["bot"], "presence", {
        "status": "online", "type": "watching", "name": "chat",
        "details": "", "state": "level {7",
    })
    assert build_presence()["state"] == "level {7"


def test_editing_presence_updates_now_and_after_a_restart(monkeypatch):
    """config.json is what survives a restart; the in-memory dict is what
    build_presence() reads. Patching only one of them makes the panel lie -
    either it needs restarting, or the restart forgets the edit."""
    written = {}
    monkeypatch.setattr(config_mod, "_patch_config",
                        lambda s, k, v: written.update({(s, k): v}))
    monkeypatch.setitem(config_mod.config["bot"], "presence",
                        dict(config_mod.DEFAULTS["bot"]["presence"]))

    out = config_mod.set_presence({"status": "idle", "type": "listening",
                                   "name": "Yuyu", "details": "d", "state": "s"})

    assert out["status"] == "idle"
    assert config_mod.config["bot"]["presence"]["status"] == "idle", "takes effect now"
    assert written[("bot", "presence")]["status"] == "idle", "and survives a restart"
    assert build_presence()["status"] == "idle"


def test_the_presence_editor_refuses_what_discord_cannot_show():
    """Fail before writing, or the panel shows a saved edit she will never
    actually display."""
    before = dict(config_mod.config["bot"]["presence"])
    for bad in ({"status": "purple", "type": "playing", "name": "x"},
                {"status": "online", "type": "flying", "name": "x"},
                {"status": "online", "type": "playing", "name": "x" * 500}):
        with pytest.raises(ValueError):
            config_mod.set_presence(bad)
    assert config_mod.config["bot"]["presence"] == before, "nothing half-written"


def test_pushing_presence_without_a_bot_says_so(monkeypatch):
    """Reported, not raised: the panel runs whether or not she has connected."""
    presence.detach()
    monkeypatch.setattr(presence, "_last_sent", {})
    ok, why = presence.apply_now()
    assert ok is False and "not connected" in why


def test_an_unchanged_presence_is_never_re_sent(monkeypatch):
    """One gateway packet per reply would buy no visible difference."""
    sent = []

    class FakeClient:
        async def change_presence(self, **kwargs):
            sent.append(kwargs)

    monkeypatch.setattr(presence, "_last_sent", {})
    monkeypatch.setattr(presence, "_last_push", 0.0)

    asyncio.run(presence.refresh(FakeClient(), force=True))
    assert len(sent) == 1
    asyncio.run(presence.refresh(FakeClient(), force=True))
    assert len(sent) == 1, "identical text must not be pushed a second time"


def test_presence_says_she_is_busy_but_never_with_whom():
    """A profile card is visible to everyone in every server she is in, so the
    labels are a closed set - no name, channel or id can slip into one."""
    presence.note_activity()
    assert presence.activity_label() == "chatting right now"

    held = presence._last_activity
    presence._last_activity = time.time() - presence.ACTIVE_FOR - 1
    try:
        assert presence.activity_label() == "hanging out"
    finally:
        presence._last_activity = held
    assert presence.activity_label() in ("chatting right now", "hanging out")


# --- memory: everyone who chats ---------------------------------------------

def test_someone_is_saved_as_soon_as_they_say_anything(tmp_path, monkeypatch):
    """Being remembered must not depend on happening to say a fact.

    Only people whose words the extractor happened to like used to get a file,
    so most of the server stayed invisible: no dashboard entry, nothing for
    `!about` to open, and nowhere for their pronouns to live.
    """
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
    # normalize_fact capitalises on the way in, so compare on its terms - the
    # point is that the fact is still there at all.
    assert len(notes) == 1 and notes[0].lower() == "hates mornings"


def test_bots_are_not_given_memory_files(tmp_path, monkeypatch):
    monkeypatch.setattr(mem, "MEMORY_DIR", tmp_path)
    chat.remember_person(_user("otherbot", "OtherBot", 99, bot=True))
    assert list(tmp_path.glob("*.md")) == []
