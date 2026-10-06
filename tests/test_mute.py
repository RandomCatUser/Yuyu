
from __future__ import annotations

import asyncio
import json
import re

import pytest

from yuyu import chat, mute
from yuyu.config import config


def _user(name: str, display: str, uid: int, bot: bool = False):
    class U:
        pass

    u = U()
    u.name = name
    u.display_name = display
    u.id = uid
    u.bot = bot
    return u


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Point the switch at a throwaway file so real people are never touched."""
    monkeypatch.setattr(mute, "MUTED_FILE", tmp_path / "muted.json")
    mute.invalidate()
    yield
    mute.invalidate()


# storage

def test_nobody_is_muted_by_default():
    assert mute.muted_slugs() == set()
    assert mute.is_muted("mewo") is False
    assert mute.is_muted("") is False
    assert mute.is_muted(None) is False


def test_muting_a_person_persists_across_a_restart():
    """Off has to survive a restart, or the button is a placebo."""
    mute.set_muted("mewo", True)

    on_disk = json.loads(mute.MUTED_FILE.read_text(encoding="utf-8"))
    assert "mewo" in on_disk["muted"]

    mute.invalidate()  # what a fresh process would do
    assert mute.is_muted("mewo") is True


def test_unmuting_takes_them_back_off_the_list():
    mute.set_muted("mewo", True)
    mute.set_muted("mewo", False)
    assert mute.muted_slugs() == set()
    assert json.loads(mute.MUTED_FILE.read_text(encoding="utf-8"))["muted"] == []


def test_several_people_can_be_quiet_at_once():
    mute.set_muted("mewo", True)
    mute.set_muted("amiminnie", True)
    mute.set_muted("mewo", False)
    assert mute.muted_slugs() == {"amiminnie"}


def test_slugs_are_normalised_so_the_switch_stuck_once():
    mute.set_muted("  Mewo ", True)
    assert mute.is_muted("mewo") is True
    assert mute.is_muted("MEWO") is True


def test_repeating_a_toggle_does_not_duplicate_the_entry():
    mute.set_muted("mewo", True)
    mute.set_muted("mewo", True)
    assert json.loads(mute.MUTED_FILE.read_text(encoding="utf-8"))["muted"].count("mewo") == 1


def test_a_missing_or_broken_file_reads_as_nobody_muted(tmp_path, monkeypatch):
    mute.MUTED_FILE.write_text("not json at all", encoding="utf-8")
    mute.invalidate()
    assert mute.muted_slugs() == set(), "one bad file must not stop her booting"

    mute.MUTED_FILE.unlink()
    mute.invalidate()
    assert mute.muted_slugs() == set()


def test_an_empty_slug_is_refused():
    mute.set_muted("", True)
    assert mute.muted_slugs() == set()
    assert not mute.MUTED_FILE.exists(), "nothing to store means nothing stored"


# the reply gate

def _drive(slug: str, muted: bool) -> list[str]:
    """Run one message through _handle_message and report what actually happened."""
    import main as bot_main

    events: list[str] = []

    class Msg:
        content = "yuyu you around?"
        author = _user(slug, slug, 5903)
        channel = "somewhere"

    class FakeRouter:
        async def run(self, message):
            events.append("command")
            return False

    def recorded(message):  # log_message is synchronous
        events.append("recorded")

    async def answered(message, *, trigger, text=None):
        events.append(f"replied:{trigger}")

    async def go():
        await bot_main._handle_message(
            object(),
            Msg(),
            lambda m: "mention",
            FakeRouter(),
            recorded,
            answered,
            lambda: False,
            config,
        )

    mute.set_muted(slug, muted)
    asyncio.run(go())
    return events


def test_a_muted_person_is_recorded_but_never_answered(monkeypatch):
    monkeypatch.setitem(config["behavior"], "humanDelay", False)
    events = _drive("mewo", muted=True)
    # Written down - she still knows what they said - and then nothing else.
    assert events == ["recorded"], f"a muted person must get no replies: {events}"


def test_an_everyone_else_still_gets_a_reply(monkeypatch):
    monkeypatch.setitem(config["behavior"], "humanDelay", False)
    events = _drive("mewo", muted=False)
    assert events == ["recorded", "command", "replied:mention"]


def test_switching_back_on_restores_the_conversation(monkeypatch):
    monkeypatch.setitem(config["behavior"], "humanDelay", False)
    _drive("mewo", muted=True)
    assert _drive("mewo", muted=False) == ["recorded", "command", "replied:mention"]


def test_exhausted_provider_failover_gets_a_friendly_reply_without_traceback(monkeypatch):
    import main as bot_main
    from yuyu.providers import ProviderError

    monkeypatch.setitem(config["behavior"], "humanDelay", False)
    logged = []
    replies = []

    class Msg:
        content = "yuyu hey"
        author = _user("mewo", "Mewo", 5903)

        async def reply(self, text):
            replies.append(text)

    class FakeRouter:
        async def run(self, _message):
            return False

    async def failed_reply(*_args, **_kwargs):
        raise ProviderError(
            "provider details",
            public="i couldn't get a model response just now, try again in a moment",
        )

    monkeypatch.setattr(bot_main, "reply", failed_reply)
    monkeypatch.setattr(bot_main, "log", logged.append)

    asyncio.run(bot_main._handle_message(
        object(),
        Msg(),
        lambda _message: "mention",
        FakeRouter(),
        lambda _message: None,
        failed_reply,
        lambda: False,
        config,
    ))

    assert replies == ["i couldn't get a model response just now, try again in a moment"]
    assert any("provider failover exhausted" in line for line in logged)
    assert all("Traceback" not in line for line in logged)


def test_the_gate_survives_a_case_mismatch_with_the_real_slug():
    mute.set_muted("Mewo", True)
    assert mute.is_muted(chat.slug_for(_user("mewo", "mewo", 5903))) is True
