
from __future__ import annotations

import re

import pytest

from yuyu import memory as mem
from yuyu import util
from yuyu import context as context_mod
from yuyu.config import MEMORY_DIR, config
from yuyu.context import clear, recent, record, stats
from yuyu.persona import build_system_prompt
from yuyu.skills import load_skills, render_skills, select_skills

SLUG = "pytest-user"


@pytest.fixture(autouse=True)
def _clean():
    clear(None, "none")
    yield
    (MEMORY_DIR / f"{SLUG}.md").unlink(missing_ok=True)
    from yuyu.affinity import invalidate

    invalidate()


# util

def test_split_message_leaves_short_text():
    assert util.split_message("hello there") == ["hello there"]


def test_split_message_respects_the_limit():
    parts = util.split_message("word " * 2000, 1900)
    assert len(parts) > 1
    assert all(len(p) <= 1900 for p in parts)


def test_split_message_never_cuts_a_word():
    text = "alpha beta gamma delta epsilon"
    assert " ".join(util.split_message(text, 20)) == text


def test_slugify_is_path_safe():
    for evil in ["../../etc/passwd", 'a<b>c:d"e/f\\g', "  ..dots..  ", ""]:
        slug = util.slugify(evil)
        assert "/" not in slug and "\\" not in slug and "." not in slug
        assert re.fullmatch(r"[a-z0-9_-]*", slug)
    assert util.slugify("../../etc/passwd") == "etc-passwd"
    assert util.slugify("") == "unknown"


def test_keywords_keeps_short_digit_tokens():
    words = util.keywords_of("up at 3am with the 1o flash")
    assert "3am" in words and "1o" in words
    assert "the" not in words and "with" not in words
    assert "flash" in words


def test_dedupe_case_insensitive():
    assert util.dedupe_case_insensitive(["Likes tea"], ["likes TEA", "Likes tea"]) == []
    assert util.dedupe_case_insensitive(["Likes tea"], ["Runs marathons"]) == ["Runs marathons"]


# fact hygiene

def test_normalize_fact_strips_first_person():
    assert mem.normalize_fact("i just got promoted to shift lead") == "Just got promoted to shift lead"
    assert mem.normalize_fact("my name is Ravi") == "Name is Ravi"
    assert mem.normalize_fact("- works nights") == "Works nights"
    assert mem.normalize_fact("works nights.") == "Works nights"


def test_normalize_fact_is_idempotent():
    once = mem.normalize_fact("my sister lives in perth")
    assert mem.normalize_fact(once) == once
    assert mem.normalize_fact(mem.normalize_fact(once)) == once


def test_is_durable_fact_rejects_noise():
    from yuyu.chat import is_durable_fact

    for junk in ["bored", "Bored", "tired", "lol", "haha", "ok", "me too", "hey", "y",
                 "what have you been working on", "do you remember me?"]:
        assert not is_durable_fact(junk), junk


def test_is_durable_fact_keeps_real_facts():
    from yuyu.chat import is_durable_fact

    for real in ["Nurse in Lisbon", "Promoted to shift lead", "Works nights",
                 "my dog is called Rex", "Learning Rust", "Have two cats"]:
        assert is_durable_fact(real), real


# memory

def test_write_and_read_person():
    mem.write_person(SLUG, {"name": "Tester", "username": SLUG, "discordId": "1"},
                     {"details": ["Works as a lighthouse keeper"], "notes": ["Prefers tea"]})
    person = mem.read_person(SLUG)
    assert person["name"] == "Tester"
    assert person["details"] == ["Works as a lighthouse keeper"]
    assert person["notes"] == ["Prefers tea"]
    # Every section exists even when empty.
    assert person["likes"] == [] and person["projects"] == []


def test_add_facts_dedupes():
    mem.write_person(SLUG, {"name": "Tester"}, {"notes": ["Prefers tea"]})
    added, _ = mem.add_facts(SLUG, {"name": "Tester"}, {"notes": ["prefers tea"]})
    assert added == [], "duplicate should not be added"
    added, _ = mem.add_facts(SLUG, {"name": "Tester"}, {"notes": ["Dislikes mornings"]})
    assert len(added) == 1
    assert len(mem.read_person(SLUG)["notes"]) == 2


def test_add_facts_routes_sections_independently():
    mem.write_person(SLUG, {"name": "Tester"}, {"details": ["Nurse in Lisbon"]})
    mem.add_facts(SLUG, {"name": "Tester"}, {
        "likes": ["Stormy seas"], "dislikes": ["Landlocked offices"],
        "projects": ["Rebuilding the lantern"], "people": ["Marta, a fellow keeper"],
    })
    person = mem.read_person(SLUG)
    assert person["likes"] == ["Stormy seas"]
    assert person["dislikes"] == ["Landlocked offices"]
    assert person["projects"] == ["Rebuilding the lantern"]
    assert person["people"] == ["Marta, a fellow keeper"]
    assert person["details"] == ["Nurse in Lisbon"], "original facts survive"


def test_forget_searches_every_section():
    mem.write_person(SLUG, {"name": "Tester"},
                     {"details": ["Nurse"], "people": ["Marta, a fellow keeper"]})
    result = mem.forget(SLUG, "marta")
    assert result["ok"] and result["removed"] == 1
    assert mem.read_person(SLUG)["people"] == []
    assert mem.read_person(SLUG)["details"] == ["Nurse"], "other sections untouched"


def test_forget_reports_a_miss():
    mem.write_person(SLUG, {"name": "Tester"}, {"details": ["Nurse"]})
    assert mem.forget(SLUG, "helicopters")["reason"] == "not-found"
    assert mem.forget("nobody-here", "x")["reason"] == "no-file"


def test_no_placeholder_is_stored_as_a_fact():
    mem.write_person(SLUG, {"name": "Tester"}, {"likes": []})
    person = mem.read_person(SLUG)
    assert "(nothing yet)" not in person["likes"]
    mem.add_facts(SLUG, {"name": "Tester"}, {"likes": ["Coffee"]})
    assert "(nothing yet)" not in mem.read_person(SLUG)["likes"]


def test_count_and_search():
    mem.write_person(SLUG, {"name": "Tester"},
                     {"details": ["Nurse"], "projects": ["Rebuilding the lantern"]})
    person = mem.read_person(SLUG)
    assert mem.count_facts(person) == 2
    hits = mem.search_facts(person, "lantern")
    assert len(hits) == 1 and hits[0]["section"] == "projects"


def test_describe_person_copes_with_partial_records():
    assert "no facts stored yet" in mem.describe_person({"name": "Y", "slug": "y"})


# context

def test_context_records_and_returns_in_order():
    where = ("g1", "c1")
    record(*where, author="Ada", slug="ada", text="first")
    record(*where, author="Yuyu", text="reply", is_bot=True)
    record(*where, author="Bob", slug="bob", text="second")
    assert [e["text"] for e in recent(*where)] == ["first", "reply", "second"]
    clear(*where)


def test_context_is_capped():
    where = ("g2", "c2")
    limit = config["context"]["maxMessages"]
    for i in range(limit + 25):
        record(*where, author="A", slug="a", text=f"msg {i}")
    entries = recent(*where)
    assert len(entries) == limit
    assert entries[-1]["text"] == f"msg {limit + 24}"
    clear(*where)


def test_channel_summary_is_persisted_and_cleared(monkeypatch, tmp_path):
    where = ("summary-guild", "summary-channel")
    monkeypatch.setattr(context_mod, "SUMMARY_FILE", tmp_path / "conversations.json")
    monkeypatch.setattr(context_mod, "_summaries", {})
    monkeypatch.setattr(context_mod, "_summaries_loaded", False)
    monkeypatch.setitem(config["context"], "summaryKeepRecentMessages", 2)
    clear(*where)

    for i in range(5):
        record(*where, author="Ada", text=f"message {i}")
    context_mod._buffers[context_mod._key(*where)]["lastCompacted"] -= 31 * 60
    batch = context_mod.compaction_batch(*where)

    assert batch is not None
    assert len(batch["entries"]) == 3
    context_mod.save_summary(*where, "Ada plans to finish the project next week.", batch["through"])
    assert context_mod.SUMMARY_FILE.exists()
    assert "finish the project" in context_mod.summary_for(*where)
    assert context_mod.compaction_batch(*where) is None

    clear(*where)
    assert context_mod.summary_for(*where) == ""
    assert context_mod.recent(*where) == []


# skills

def test_load_skills_parses_frontmatter():
    skills = load_skills(force=True)
    assert len(skills) >= 2
    social = next((s for s in skills if s["name"] == "Social Basics"), None)
    assert social is not None and social["always"] is True
    nightowl = next(s for s in skills if s["name"] == "Night Owl")
    assert "gaming" in nightowl["keywords"] and nightowl["always"] is False
    assert "---" not in nightowl["content"], "frontmatter stripped"


def test_select_skills_always_and_keyword():
    assert any(s["name"] == "Social Basics" for s in select_skills("spreadsheets")["always"])
    assert select_skills("spreadsheets")["matched"] == []
    assert any(s["name"] == "Night Owl" for s in select_skills("been playing games all night")["matched"])
    assert "<skill name=\"Night Owl\">" in render_skills(select_skills("games music"))


# discord.py 2.7 message shape
#
# discord.py 2.7 dropped `guild_id`, `channel_id`, `referenced_message` and
# `created_timestamp`. getattr() quietly yields None, and the transcript
# vanishes from the prompt with no error at all.

class _Guild:
    def __init__(self, gid: int):
        self.id = gid
        self.name = "The Group Chat"


class _Channel:
    def __init__(self, cid: int, guild: _Guild):
        self.id = cid
        self.guild = guild


class _ModernMessage:
    def __init__(self, content: str = "hello"):
        self.content = content
        self.guild = _Guild(99)
        self.channel = _Channel(500, self.guild)
        self.reference = None


class _LegacyMessage(_ModernMessage):
    def __init__(self, content: str = "hello"):
        super().__init__(content)
        self.guild_id = 99
        self.channel_id = 500
        self.referenced_message = None


def test_transcript_reaches_the_prompt_on_the_modern_message_shape():
    clear(None, "none")
    record(99, 500, author="Robin", text="i just adopted a cat named Biscuit", slug=SLUG)
    try:
        prompt = build_system_prompt(_ModernMessage("yuyu what did I just say?"), [], "mention", None)
        assert "Biscuit" in prompt, "recent messages missing from the system prompt"

        legacy = build_system_prompt(_LegacyMessage("yuyu what did I just say?"), [], "mention", None)
        assert "Biscuit" in legacy
    finally:
        clear(99, 500)


def test_system_prompt_leads_with_the_model_not_the_provider(monkeypatch):
    provider = next(
        provider for provider in config["model"]["providers"]
        if provider["id"] == config["model"]["default"]
    )
    monkeypatch.setitem(provider, "model", "test-chat-model")
    provider_name = provider.get("name") or provider["id"]

    prompt = build_system_prompt(_ModernMessage("yuyu hey"), [], "mention", None)

    # The model leads; the provider is only ever the parenthetical.
    assert "The chat model running you is test-chat-model" in prompt
    assert f"is {provider_name} (test-chat-model)" not in prompt, (
        "the provider is back in front of the model name")
    assert f"test-chat-model (hosted by {provider_name})" in prompt
    # The fallback list is derived from config, so re-pointing a provider at a
    # different model does not break this.
    fallback = next(
        p for p in config["model"]["providers"]
        if p["id"] != config["model"]["default"] and p.get("model")
    )
    fallback_model = fallback["model"]
    fallback_name = fallback.get("name") or fallback["id"]
    assert f"{fallback_model} (hosted by {fallback_name})" in prompt
    assert f"{fallback_name} ({fallback_model})" not in prompt
    assert "answer with the model name - not the provider" in prompt


def test_only_a_few_fallbacks_are_named_in_the_prompt(monkeypatch):
    prompt = build_system_prompt(_ModernMessage("yuyu hey"), [], "mention", None)
    line = next(l for l in prompt.splitlines() if l.startswith("Fallbacks, when they have keys:"))

    described = line.removeprefix("Fallbacks, when they have keys:").count("hosted by")
    assert described <= 3, f"{described} fallbacks named in one prompt"
    # The chain stays long; the cap is on the prompt, not failover.
    assert len([p for p in config["model"]["providers"] if p.get("enabled", True)]) > described


def test_a_provider_with_no_model_name_is_skipped_not_described(monkeypatch):
    provider = next(
        provider for provider in config["model"]["providers"]
        if provider["id"] == config["model"]["default"]
    )
    provider_name = provider.get("name") or provider["id"]
    monkeypatch.setitem(provider, "model", "")

    prompt = build_system_prompt(_ModernMessage("yuyu hey"), [], "mention", None)

    assert f"The chat model running you is {provider_name}" not in prompt
    assert "hosted by) " not in prompt, "an empty model left a dangling description"
