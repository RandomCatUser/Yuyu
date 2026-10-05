"""System prompt assembly."""

from __future__ import annotations

import threading
from datetime import datetime

from . import context
from .config import PERSONA_FILE, bot_name, config, current_model
from .memory import SECTION_KEYS, SECTION_NAMES
from .skills import render_skills, select_skills
from .util import scope_of_message, truncate

_lock = threading.Lock()
_persona_cache: dict[str, tuple[float, str]] = {}

def fallback_persona() -> str:
    """What she says if there is no persona file."""
    return f"You are {bot_name()}. A regular person in a Discord server.\nBe genuine, brief and a bit opinionated."

CARD_RULES = """## Formatting
Plain short text by default, like a person typing. No headers, no bullet lists, no bold in
normal conversation.

USE A CARD for the answer itself when it is:
- a comparison of two or more options, especially with a verdict
- a spec, a table, or a numbered procedure
- code someone needs to copy
- a summary of something long

A card with three short fields beats three rambling paragraphs. "Keep it tight" means be brief,
not drop the card.

The card is the data. Your opinion goes outside it in plain text - verdict first in a sentence or
two, then the card. Never let a card carry your personality.

Syntax:
<card title="Short title" color="blue|green|yellow|red|purple|cyan" footer="optional">
Body. Supports **bold**, `inline code`, and ```code blocks```.
Fields go on their own lines, up to 25:
field=Label::value
field=Another::value
</card>

One card per reply. Never wrap a casual reply in a card - "yeah that's brutal" stays as text.
Never put a <card> inside a code block.

Emoji: do not add any to your own text. A separate system handles stickers and reactions."""


def load_persona() -> str:
    """Read the persona file, cached until it is edited on disk."""
    key = str(PERSONA_FILE)
    try:
        mtime = PERSONA_FILE.stat().st_mtime
    except OSError:
        return fallback_persona()
    with _lock:
        hit = _persona_cache.get(key)
        if hit is not None and hit[0] == mtime:
            return hit[1]
    try:
        text = PERSONA_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        text = fallback_persona()
    with _lock:
        _persona_cache[key] = (mtime, text)
    return text


def _where(guild, trigger: str | None) -> str:
    bits = ["This is a private DM." if guild is None else f"Server: {guild.name}"]
    if trigger == "mention":
        bits.append("You were addressed by name.")
    elif trigger == "reply":
        bits.append("Someone replied directly to one of your messages.")
    elif trigger == "name-start":
        bits.append("Someone used your name to start a message.")
    bits.append(f"Local time for you: {datetime.now().astimezone().strftime('%Y-%m-%d %H:%M %Z')}")
    return "\n".join(bits)


# How many fallback models she is told about. The chain itself is unlimited; this
# only bounds the list written into her prompt.
_FALLBACKS_NAMED_IN_PROMPT = 3


def _configured_chat_models() -> tuple[str, list[str]]:
    providers = config["model"].get("providers") or []
    available = [
        provider for provider in providers
        if isinstance(provider, dict)
        and provider.get("enabled", True)
        and provider.get("id")
        and provider.get("baseUrl")
        and provider.get("model")
    ]
    primary_id = current_model()
    primary = next(
        (provider for provider in available if provider.get("id") == primary_id),
        available[0] if available else None,
    )
    if primary is None:
        return "not currently configured", []

    def describe(provider: dict) -> str:
        # Model first, provider second, in brackets. Asked what she is running, she
        # has to be able to say "llama-3.3-70b" - not "Groq", which is who serves
        # it. The provider is there so she can name it when someone asks who bills
        # her, which is a different question and the rarer one.
        model = str(provider.get("model") or "").strip()
        who = str(provider.get("name") or provider.get("id") or "").strip()
        if not model:
            return who or "an unnamed model"
        return f"{model} (hosted by {who})" if who else model

    # Only the first few are named in the prompt. The whole chain still fails over -
    # this list is just so she can answer "and if that one is down?" - and naming
    # every configured model would put dozens of long model ids into the prompt of
    # every single reply, which costs tokens and invites her to quote a model that
    # is twenty ranks down the chain.
    fallbacks = [
        describe(provider) for provider in available if provider is not primary
    ][:_FALLBACKS_NAMED_IN_PROMPT]
    return describe(primary), fallbacks


def _transcript(entries: list[dict]) -> str:
    lines = []
    for entry in entries:
        # Only her own lines are "(you)". Another bot speaking in the channel
        # must not be quoted back as though she said it.
        who = (
            f"{entry['author']} (you)"
            if entry["isBot"] and entry["author"] == bot_name()
            else entry["author"]
        )
        lines.append(f"{who}: {truncate(entry['text'], 700)}")
    return "\n".join(lines)


def _people_block(people: list[dict]) -> str:
    blocks = []
    for person in people:
        if not person:
            continue
        details = person.get("details", [])
        notes = person.get("notes", [])
        if not details and not notes:
            continue
        lines = []
        if details:
            lines.append("Stable details: " + "; ".join(details))
        if notes:
            lines.append("Notes: " + "; ".join(notes))
        blocks.append(f'<person name="{person["name"]}">\n' + "\n".join(lines) + "\n</person>")
    return ("These are people you know:\n" + "\n".join(blocks)) if blocks else ""


def build_system_prompt(message, people: list[dict], trigger: str | None, affect: dict | None = None) -> str:
    guild = getattr(message, "guild", None)
    # Scope has to come from the channel/guild objects: discord.py 2.7 dropped
    # `Message.guild_id`, and falling back to None makes context.recent() return
    # nothing - the whole conversation would vanish from the prompt.
    guild_id, channel_id = scope_of_message(message)

    history = context.recent(
        guild_id,
        channel_id,
        limit=config["context"].get("promptMessages", 12),
    )
    conversation_summary = context.summary_for(guild_id, channel_id)
    # The message being answered is included so skill selection can see it.
    for_skills = "\n".join(
        [conversation_summary, *(e["text"] for e in history), getattr(message, "content", "") or ""]
    )

    primary_model, fallback_models = _configured_chat_models()
    model_context = (
        "## Models\n"
        f"The chat model running you is {primary_model}. "
        "If anyone asks what model you are on, answer with the model name - not the "
        "provider and not the company behind it. Gemini is a provider, so if you are "
        "running gemini-3.1-flash-lite that is the answer, and \"Gemini\" on its own is "
        "the wrong one. Give the fallback models as models too, same rule. "
        "If a call fails, the bot retries the fallbacks in order - never "
        "claim a fallback handled a reply unless you know it did."
    )
    if fallback_models:
        model_context += "\nFallbacks, when they have keys: " + ", ".join(fallback_models) + "."

    parts = [
        load_persona(),
        model_context,
        f"## Where you are\n{_where(guild, trigger)}",
    ]

    skills_block = render_skills(select_skills(for_skills))
    if skills_block:
        parts.append(f"## Skills\n{skills_block}")

    people_block = _people_block(people)
    if people_block:
        parts.append(f"## People you know\n{people_block}")

    if affect:
        if affect.get("people"):
            parts.append(affect["people"])
        if affect.get("feelings"):
            parts.append(f"## How you feel right now\n{affect['feelings']}")
        if affect.get("bond"):
            parts.append(affect["bond"])
        if affect.get("crush"):
            parts.append(affect["crush"])

    if config["formatting"]["allowCards"]:
        parts.append(CARD_RULES)

    reply_rules = [
        "## How to reply",
        f"You are in a live Discord conversation. {len(history)} recent messages are below - read "
        "them, follow the thread, react to what was actually said.",
        "",
        "- Type like a person on their phone. Short, direct, no preamble.",
        "- Match the energy and length of whoever is talking. One to three sentences is normal.",
        "- No markdown headers or bullet lists unless someone asks for them.",
        "- If several people are talking, answer whoever spoke to you last.",
        "- Never summarise the conversation back to anyone.",
        "- Stay in character. You are one of them, not something serving them.",
        "",
        "### Do not repeat yourself",
        "Your own earlier lines are in the transcript, marked as yours.",
        "- Never open two messages the same way, and never reuse an opening you just used.",
        "- Answer the point instead of echoing their wording back.",
        "- If you already answered it, move on. Do not answer it again.",
        "- Stop leaning on the same filler every time (haha, yeah, so, honestly).",
        "- Make the point once, cleanly, rather than three ways in one message.",
    ]
    if people_block:
        reply_rules += [
            "",
            "### Using what you remember",
            "The <person> blocks are what you actually know. Use them like a friend would:",
            "- Bring a remembered detail up BEFORE anyone asks. That is the point of remembering.",
            "- Never recite it. 'still on nights?' beats 'I recall you work nights'.",
            "- One remembered thing per conversation, maximum. Never a roll call.",
            "- Chase loose ends: if something was raised and never settled, ask about it later.",
            "- Never mention notes, memory, files or records. You just know things.",
            "- If someone asks you to forget something, drop it at once and do not argue.",
        ]
    else:
        reply_rules += [
            "",
            "You do not know much about anyone here yet. Learn from what gets said naturally, "
            "and do not interrogate people.",
        ]
    parts.append("\n".join(reply_rules))

    if conversation_summary:
        parts.append(f"## Ongoing conversation\n{conversation_summary}")

    if history:
        parts.append(f"## Recent messages (oldest first)\n{_transcript(history)}")

    return "\n\n".join(parts)


def create_default_persona() -> None:
    """Write a starter persona file when there is none, so she is never mute."""
    path = PERSONA_FILE
    if path.exists():
        return
    path.write_text(
        f"""# Who {bot_name()} is

Write this file as you'd describe her to a mutual friend. Plain prose.
This text is injected verbatim into the system prompt on every single message.

## The one rule that matters most
You are **one of the friends in the group chat**. Not an assistant. Be warm - you are
genuinely glad to hear from people, and you say so.

## Voice
Casual, contractions, one to three sentences by default. No headers, no bullet lists,
no bold. A little warmth in the wording beats an emoji.

## How you remember things
Bring a remembered detail up *before* anyone asks. Never recite it - "still on nights?"
not "I recall you work nights". One per conversation, maximum. Chase loose ends.
Never mention notes, memory, or files.

## Never
"As an AI", "Certainly!", "Great question", "Here's a list". Nothing that names what
you're about to do.

## What you're into
Fill this in so you have an actual personality. Specifics beat vagueness.
""",
        encoding="utf-8",
    )
    print(f"[persona] wrote a starter {path.name}")


__all__ = [
    "build_system_prompt",
    "load_persona",
    "create_default_persona",
    "CARD_RULES",
    "SECTION_KEYS",
    "SECTION_NAMES",
]
