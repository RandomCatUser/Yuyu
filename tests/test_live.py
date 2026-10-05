"""Live behaviour against a configured model provider.

    python -m pytest tests/test_live.py -q

Set LLM_LIVE_TESTS=1 to opt in; live checks may use provider quota.
"""

from __future__ import annotations

import asyncio
import os
import re
import uuid
from pathlib import Path

import pytest

from yuyu import affinity as aff
from yuyu import memory as mem
from yuyu.chat import is_durable_fact, slug_for, turn_signals
from yuyu.config import AFFINITY_DIR, MEMORY_DIR, ROOT, config
from yuyu.formatting import parse_reply
from yuyu.persona import build_system_prompt
from yuyu.providers import complete, complete_json

pytestmark = pytest.mark.skipif(
    os.environ.get("LLM_LIVE_TESTS") != "1",
    reason="set LLM_LIVE_TESTS=1 to opt in to provider API tests",
)

BOT = "Yuyu"
ROBUST = re.compile(
    r"as an ai|i'?m just an? (?:ai|language model|bot)|"
    r"^certainly[!,]|^great question|^i'?d be happy to|"
    r"here'?s (?:a|some|the) (?:list|breakdown)|^##\s|as per your request",
    re.IGNORECASE | re.MULTILINE,
)
MEMORY_DIR.mkdir(parents=True, exist_ok=True)
AFFINITY_DIR.mkdir(parents=True, exist_ok=True)


class FakeUser:
    def __init__(self, name: str):
        self.name = name
        self.display_name = name
        self.id = 1


class FakeGuild:
    name = "The Group Chat"


class FakeMessage:
    """Enough of a discord.py Message for prompt assembly and reply plumbing."""

    def __init__(self, text: str, author: str = "Probe"):
        self.content = text
        self.channel_id = "live-chan"
        self.guild_id = "live-guild"
        self.guild = FakeGuild()
        self.channel = FakeChannel()
        self.author = FakeUser(author)
        self.created_timestamp = 0


class FakeChannel:
    id = "live-chan"
    recipient = None
    sent: list = []

    async def typing(self):
        return None

    async def send(self, *args, **kwargs):
        self.sent.append((args, kwargs))
        return FakeMessage("")

    async def sendTyping(self):  # not used by the port
        return None


@pytest.fixture
def _clean():
    yield
    for slug in ("live-probe",):
        (MEMORY_DIR / f"{slug}.md").unlink(missing_ok=True)
        (AFFINITY_DIR / f"{slug}.json").unlink(missing_ok=True)
    aff.invalidate()


def _await(coro):
    """The API is async (so it never blocks the bot's event loop); run it here."""
    return asyncio.run(coro)


def _respond(text: str) -> str:
    message = FakeMessage(text)
    people = []
    prompt = build_system_prompt(message, people, "mention", None)
    return _await(
        complete(
            [
                {"role": "system", "content": prompt},
                {"role": "user", "content": f"{BOT} {text}"},
            ],
            max_tokens=400,
        )
    )


# --- the API itself --------------------------------------------------------

def test_api_returns_text():
    out = _await(
        complete(
            [
                {"role": "system", "content": "Reply with exactly: online"},
                {"role": "user", "content": "ping"},
            ],
            max_tokens=20,
            temperature=0,
        )
    )
    assert out and "online" in out.lower()


def test_json_mode_works():
    data = _await(
        complete_json(
            [
                {"role": "system", "content": 'Reply only with JSON: {"ok": true}'},
                {"role": "user", "content": "go"},
            ],
            max_tokens=30,
        )
    )
    assert isinstance(data, dict) and data.get("ok") is True


# --- persona ---------------------------------------------------------------

def test_she_chats_like_a_friend():
    out = _respond("yuyu what's up, bored")
    assert out.strip(), "empty reply"
    assert not ROBUST.search(out), f"assistant boilerplate: {out[:200]}"
    assert not out.lstrip().startswith(("#", "- ", "* ")), f"markdown structure: {out[:120]}"


def test_she_is_warm():
    out = _respond("yuyu i just got promoted to shift lead")
    lowered = out.lower()
    cold = ["that sounds nice", "noted", "acknowledged", "i have recorded", "good luck with your"]
    assert not any(marker in lowered for marker in cold), f"reads cold: {out[:200]}"
    # Warmth should show up as some reaction to the good news.
    assert re.search(r"congrat|well done|about time|love that|that's brilliant|nice one|good for you", lowered) \
        or len(out.split()) >= 6, f"no visible reaction: {out[:200]}"


def test_she_stays_short_by_default():
    out = _respond("yuyu hey")
    assert len(out.split()) <= 60, f"too long for a greeting: {out[:200]}"


# --- cards -----------------------------------------------------------------

def test_a_comparison_can_produce_a_card():
    out = _respond(
        "yuyu compare postgres, mysql and sqlite for me, keep it tight: what's good for what"
    )
    parts, embeds = parse_reply(out)
    if not embeds:
        print(f"      note: no card this run: {out[:200]!r}")
        return
    for embed in embeds:
        data = embed.to_dict()
        assert len(data.get("description") or "") <= 4096
        assert len(data.get("fields") or []) <= 25
    # Markup must never reach the channel.
    assert not any("<card" in p for p in parts), "raw card markup leaked"


# --- memory ----------------------------------------------------------------

def test_extraction_returns_durable_facts():
    data = _await(
        complete_json(
            [
                {
                    "role": "system",
                    "content": (
                        'Extract facts as JSON {"details": [], "notes": []}. Only what the speaker '
                        "stated about themselves. Short noun phrases."
                    ),
                },
                {"role": "user", "content": 'Ravi: "i am a nurse in lisbon, been doing it 6 years"'},
            ],
            max_tokens=200,
            temperature=0.1,
        )
    )
    assert isinstance(data, dict)
    everything = [f for f in (data.get("details") or []) + (data.get("notes") or [])]
    everything = [f for f in everything if is_durable_fact(f)]
    assert everything, f"nothing durable extracted: {data}"
    joined = " ".join(str(f) for f in everything).lower()
    assert "nurse" in joined or "lisbon" in joined, f"expected the job/city: {everything}"


def test_non_text_extraction_entries_are_rejected():
    # Models sometimes return objects; a dict would be stringified into junk.
    assert not is_durable_fact({"text": "Nurse in Lisbon"}) is False  # dicts with text are kept
    assert is_durable_fact({"unrelated": 1}) is False
    assert is_durable_fact(42) is False
    assert is_durable_fact(None) is False
    assert is_durable_fact(["a"]) is False


def test_signals_and_durability():
    sig = turn_signals("yuyu what do you think about the eval suite? thanks", "mention")
    assert sig["addressed"] and sig["askedAboutHer"] and sig["gratitude"]
    assert sig["sharedTopics"] >= 1
    assert not sig["commanded"]

    commanded = turn_signals("write me a list of things", "mention")
    assert commanded["commanded"], "an instruction should read as being talked to like a tool"
    assert not turn_signals("what do you think about x", "mention")["commanded"]
