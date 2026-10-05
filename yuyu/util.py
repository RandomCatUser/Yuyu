"""Shared helpers: message splitting, safe slugs, fact hygiene, keyword extraction."""

from __future__ import annotations

import os
import re
from pathlib import Path

DISCORD_MAX = 2000


def atomic_write(path, text: str, encoding: str = "utf-8") -> None:
    """Replace a file in one step, with the half-written copy staged in tmp/.

    config.json, a memory file and the quiet list are all read by other
    threads while this one writes them, and a reader must never catch one part
    through a write - least of all after a crash, where a truncated config
    would fail to parse and take the whole bot down with it.

    The stage lives in tmp/ because that is what the folder is for: nothing
    half-written sits beside real data. Only when the target is on another
    volume does it fall back to staging alongside the target, because
    `os.replace` refuses to move a file across drives and a rename that fails
    is worse than a stray scratch file. Beside the target is still atomic -
    just not where anyone asked for.
    """
    from .config import TMP_DIR

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    def _swap(stage_dir: Path) -> None:
        stage_dir.mkdir(parents=True, exist_ok=True)
        stage = stage_dir / f"{path.name}.{os.getpid()}.tmp"
        try:
            stage.write_text(text, encoding=encoding)
            os.replace(stage, path)
        except OSError:
            try:
                stage.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    if TMP_DIR.drive == path.drive:
        try:
            _swap(TMP_DIR)
            return
        except OSError:
            # Same reported volume but still refused - Windows and POSIX differ
            # on how they tell you about the network and the temp drives.
            pass
    _swap(path.parent)


def scope_of_message(message) -> tuple:
    """(guild_id, channel_id) for a discord.py Message, across library versions.

    discord.py 2.7 removed `Message.guild_id` and `Message.channel_id`; the scope
    now has to come from the channel and guild objects. Older versions still have
    the attributes, so check both. Lives here rather than in `chat` because
    `persona` needs it too and `chat` already imports `persona`.

    Without this, a version mismatch resolves to (None, None) and the recent
    transcript silently vanishes from the prompt.
    """
    guild_id = getattr(message, "guild_id", None)
    if guild_id is None:
        guild = getattr(message, "guild", None)
        guild_id = getattr(guild, "id", None)

    channel_id = getattr(message, "channel_id", None)
    if channel_id is None:
        channel = getattr(message, "channel", None)
        channel_id = getattr(channel, "id", None)

    return (guild_id, channel_id)


def reply_target(message):
    """The message being replied to, across discord.py versions.

    Older releases expose `Message.referenced_message`; 2.7 dropped it and keeps
    the resolved copy on `Message.reference.resolved` instead.
    """
    ref = getattr(message, "referenced_message", None)
    if ref is not None:
        return ref
    reference = getattr(message, "reference", None)
    if reference is None:
        return None
    return getattr(reference, "resolved", None) or getattr(reference, "cached_message", None)


def split_message(text: str, max_length: int = 1900) -> list[str]:
    """Split on whitespace so a message never exceeds Discord's 2000 char limit.

    Prefers blank lines, then newlines, then spaces. Never cuts a word in half.
    """
    clean = (text or "").strip()
    if not clean:
        return []
    if len(clean) <= max_length:
        return [clean]

    out: list[str] = []
    buf = ""

    def flush() -> None:
        nonlocal buf
        if buf.strip():
            out.append(buf.strip())
        buf = ""

    for block in re.split(r"\n{2,}", clean):
        if len(buf + block) > max_length:
            flush()
        if len(block) > max_length:
            for line in block.split("\n"):
                if len(buf + line) > max_length:
                    flush()
                if len(line) > max_length:
                    for word in line.split(" "):
                        if len(buf + word) > max_length:
                            flush()
                        buf += f"{buf and ' ' or ''}{word}"
                else:
                    buf += f"{buf and chr(10) or ''}{line}"
        else:
            buf += f"{buf and chr(10) * 2 or ''}{block}"
    flush()
    return [p for p in out if p]


def slugify(value, max_len: int = 40) -> str:
    """Reduce to a safe filename stem: only [a-z0-9_-] survives.

    Path traversal and Windows-reserved characters are impossible by construction.
    """
    import unicodedata

    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower()
    text = re.sub(r"[^a-z0-9_-]+", "-", text)
    text = re.sub(r"-{2,}", "-", text)
    text = re.sub(r"^[-_]+|[-_]+$", "", text)
    text = text[:max_len].rstrip("-_")
    return text or "unknown"


def truncate(text: str, max_len: int) -> str:
    text = str(text or "")
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "…"


# Noise words for keyword matching. They pass a length filter but carry no topical
# signal, and letting them through makes sticker and skill matching fire on
# almost any sentence.
STOPWORDS = frozenset(
    """
    with that this these those there their them they then than have has had been being
    were from some just like really about would could should what when where which while
    into over under after before because please thanks thank does doing done very much
    more most other such only also even still back here your yours mine ours will shall
    ever been
    """.split()
)


def keywords_of(text: str) -> set[str]:
    """Topic words for skill and sticker matching.

    Short words are usually noise, but short tokens containing a digit carry real
    signal - "3am", "1o", "4o" - so those are kept regardless of length.
    """
    words = re.sub(r"[^a-z0-9\s]", " ", str(text or "").lower()).split()
    return {w for w in words if any(c.isdigit() for c in w) or (len(w) > 3 and w not in STOPWORDS)}


def dedupe_case_insensitive(existing, incoming) -> list[str]:
    """Items in `incoming` not already present in `existing` (case-insensitive)."""
    seen = {str(s).lower().strip() for s in (existing or [])}
    added: list[str] = []
    for item in incoming or []:
        text = re.sub(r"^[-*]\s*", "", str(item or "")).strip()
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        added.append(text)
    return added


def clamp(value, low: float, high: float) -> float:
    return max(low, min(high, value))
