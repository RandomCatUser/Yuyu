"""Sticker images and reply emoji.

Discord refuses bot tokens on the sticker API, so a "sticker" here is the image
sent as an attachment. Drop files in stickers/, wire them up in stickers.json,
and custom emoji are the fallback when no image matches."""

from __future__ import annotations

import json
import random
import threading
from pathlib import Path

import discord

from .config import STICKER_DIR, STICKERS_FILE, config
from .util import keywords_of

IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp", ".gif")
_lock = threading.Lock()
_cache: dict | None = None
_cache_stamp: str = ""


def _is_image(name: str) -> bool:
    return str(name or "").lower().endswith(IMAGE_EXT)


def list_images() -> list[str]:
    """Names (not Paths) so they compare and serialise consistently."""
    try:
        return sorted(f.name for f in STICKER_DIR.iterdir() if f.is_file() and _is_image(f.name))
    except OSError:
        return []


def _stamp() -> str:
    try:
        return f"{STICKERS_FILE.stat().st_mtime_ns}:{','.join(list_images())}"
    except OSError:
        return ""


def _lower(values) -> list[str]:
    return [str(v).lower() for v in (values or []) if str(v).strip()]


def _empty() -> dict:
    return {"images": [], "emojiReactions": [], "emojiStickers": [], "defaults": []}


def load_stickers(force: bool = False) -> dict:
    global _cache, _cache_stamp
    stamp = _stamp()
    with _lock:
        if _cache is not None and not force and stamp == _cache_stamp:
            return _cache

    data = _empty()
    try:
        raw = json.loads(STICKERS_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raw = {}
    except (json.JSONDecodeError, OSError) as exc:
        print(f"[stickers] {STICKERS_FILE.name}: {exc}")
        raw = {}

    for entry in raw.get("reactions") or []:
        if not isinstance(entry, dict):
            continue
        if entry.get("file"):
            data["images"].append({"kind": "reaction", "file": entry["file"],
                                    "keywords": _lower(entry.get("when") or entry.get("keywords"))})
        elif str(entry.get("emoji", "")).strip():
            data["emojiReactions"].append({"emoji": entry["emoji"].strip(),
                                           "keywords": _lower(entry.get("when") or entry.get("keywords"))})

    for entry in raw.get("sticker") or []:
        if not isinstance(entry, dict):
            continue
        if entry.get("file"):
            data["images"].append({"kind": "sticker", "file": entry["file"],
                                    "keywords": _lower(entry.get("keywords"))})
        elif str(entry.get("emoji", "")).strip():
            data["emojiStickers"].append({"emoji": entry["emoji"].strip(),
                                          "keywords": _lower(entry.get("keywords"))})

    data["defaults"] = [str(f) for f in (raw.get("default") or []) if str(f).strip()]

    # Drop entries whose file is missing, so a typo cannot break sending.
    available = set(list_images())
    data["images"] = [s for s in data["images"] if s["file"] in available]

    with _lock:
        _cache = data
        _cache_stamp = stamp
    return data


# --- server emoji (fallback) -----------------------------------------------

_guild_emoji: dict[int, list[dict]] = {}


def index_guild_emoji(guilds) -> int:
    """Cache every custom emoji per guild so topic-matched ones can be used."""
    _guild_emoji.clear()
    for guild in guilds:
        try:
            items = [{"id": e.id, "name": e.name, "animated": e.animated} for e in guild.emojis]
        except Exception:
            items = []
        _guild_emoji[guild.id] = items
    return guild_emoji_count()


def guild_emoji_count() -> int:
    return sum(len(v) for v in _guild_emoji.values())


def _mention(entry: dict) -> str:
    return f"<a:{entry['name']}:{entry['id']}>" if entry["animated"] else f"<:{entry['name']}:{entry['id']}>"


def _match_guild_emoji(guild_id, words: set[str]) -> str | None:
    items = _guild_emoji.get(guild_id) or []
    best, best_score = None, 0
    for entry in items:
        parts = [p for p in str(entry["name"]).lower().replace("-", "_").split("_") if p]
        score = 0
        for part in parts:
            if part in words:
                score += 2
            elif any(part in w or w in part for w in words):
                score += 1
        if score > best_score:
            best, best_score = entry, score
    return _mention(best) if best_score > 0 else None


def _best(entries: list[dict], words: set[str]) -> dict | None:
    scored = [(e, sum(1 for k in e["keywords"] if k in words)) for e in entries]
    scored = [(e, n) for e, n in scored if n > 0]
    if not scored:
        return None
    scored.sort(key=lambda pair: -pair[1])
    return scored[0][0]


# --- picking ---------------------------------------------------------------

def pick_sticker_image(text: str, chance: float | None = None) -> dict | None:
    if not config["emoji"]["stickers"]:
        return None
    chance = config["emoji"]["stickerChance"] if chance is None else chance
    if random.random() > chance:
        return None

    data = load_stickers()
    hit = _best([s for s in data["images"] if s["kind"] == "sticker"], keywords_of(text))
    if hit:
        return {"file": hit["file"], "matched": hit["keywords"]}
    if data["defaults"] and random.random() < 0.2:
        return {"file": random.choice(data["defaults"]), "matched": "default"}
    return None


def pick_reaction_image(text: str, chance: float | None = None) -> dict | None:
    if not config["emoji"]["reactions"]:
        return None
    chance = config["emoji"]["reactionChance"] if chance is None else chance
    if random.random() > chance:
        return None
    hit = _best([s for s in load_stickers()["images"] if s["kind"] == "reaction"], keywords_of(text))
    return {"file": hit["file"], "matched": hit["keywords"]} if hit else None


def pick_reaction_emoji(text: str, guild_id=None, chance: float | None = None) -> str | None:
    if not config["emoji"]["reactions"]:
        return None
    chance = config["emoji"]["reactionChance"] if chance is None else chance
    if random.random() > chance:
        return None
    words = keywords_of(text)
    from_guild = _match_guild_emoji(guild_id, words)
    if from_guild:
        return from_guild
    hit = _best(load_stickers()["emojiReactions"], words)
    return hit["emoji"] if hit else None


def pick_sticker_emoji(text: str, guild_id=None, chance: float | None = None) -> str | None:
    if not config["emoji"]["stickers"]:
        return None
    chance = config["emoji"]["stickerChance"] if chance is None else chance
    if random.random() > chance:
        return None
    words = keywords_of(text)
    hit = _best(load_stickers()["emojiStickers"], words)
    if hit:
        return hit["emoji"]
    return _match_guild_emoji(guild_id, words)


# --- sending ---------------------------------------------------------------

async def send_sticker_image(channel, sticker: dict | None):
    """Attach a sticker image. Silently does nothing if it fails."""
    if not sticker or not sticker.get("file"):
        return False
    path = sticker_path(sticker["file"])
    if not path or not path.exists():
        return False
    try:
        await channel.send(file=discord.File(str(path)))
        return True
    except Exception:
        return False


async def react_to(message, emoji: str | None) -> bool:
    """Image attachments cannot be reactions, so this is custom emoji only."""
    if not emoji:
        return False
    try:
        await message.add_reaction(emoji)
        return True
    except Exception:
        return False


# --- dashboard support -----------------------------------------------------

def sticker_inventory() -> dict:
    data = load_stickers(force=True)
    files = []
    for name in list_images():
        try:
            size = (STICKER_DIR / name).stat().st_size
        except OSError:
            size = 0
        files.append({"name": name, "bytes": size, "url": f"/stickers/{name}"})
    available = {f["name"] for f in files}
    return {
        "files": files,
        "images": data["images"],
        "missing": [s["file"] for s in data["images"] if s["file"] not in available],
        "emojiReactions": len(data["emojiReactions"]),
        "emojiStickers": len(data["emojiStickers"]),
        "serverEmoji": guild_emoji_count(),
    }


def sticker_path(name: str) -> Path | None:
    """Resolve a name inside stickers/, refusing anything that escapes it."""
    safe = Path(str(name or "")).name
    if not _is_image(safe):
        return None
    full = (STICKER_DIR / safe).resolve()
    if STICKER_DIR.resolve() not in full.parents:
        return None
    return full
