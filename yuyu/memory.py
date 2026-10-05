"""Per-person memory as plain markdown in memory/<slug>.md.

Six sections (Details, Notes, Likes, Dislikes, Projects, People) so a fact lands
somewhere meaningful. Plain markdown on purpose: open one, edit it, delete it, and
the next message picks the change up."""

from __future__ import annotations

import re
from datetime import datetime, timezone

from .config import MEMORY_DIR, config
from .util import atomic_write, dedupe_case_insensitive, slugify, truncate

SECTION_NAMES = {
    "details": "Details",
    "notes": "Notes",
    "likes": "Likes",
    "dislikes": "Dislikes",
    "projects": "Projects",
    "people": "People",
}
SECTION_KEYS = list(SECTION_NAMES)
MAX_CHARS = 2000  # Discord embed description limit


def blank_record() -> dict[str, list[str]]:
    return {key: [] for key in SECTION_KEYS}


_FIRST_PERSON = re.compile(
    r"^(?:i'm|im|i've|ive|i have|i|we're|we've|weve|we|my|me|also|so|but|and|then|well|actually)\b[\s,]*",
    re.IGNORECASE,
)


def normalize_fact(raw: str) -> str:
    """Turn a raw extracted sentence into a short standalone fact.

    "i just got promoted to shift lead" -> "Just got promoted to shift lead".
    Idempotent, so repeated rewrites do not keep eating the front of a fact.
    """
    text = re.sub(r"^[-*]\s*", "", str(raw or "")).strip()
    text = _FIRST_PERSON.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"[.,;!?]+$", "", text).strip()
    return text[:1].upper() + text[1:] if text else ""


def _file_for(slug: str):
    return MEMORY_DIR / f"{slug}.md"


def _parse_sections(body: str) -> dict[str, list[str]]:
    out = blank_record()
    current: str | None = None
    by_lower = {name.lower(): key for key, name in SECTION_NAMES.items()}

    for raw in body.split("\n"):
        heading = re.match(r"^##\s+(.+?)\s*$", raw)
        if heading:
            current = by_lower.get(heading.group(1).strip().lower())
            continue
        if not current:
            continue
        bullet = re.match(r"^\s*[-*]\s+(.*\S)\s*$", raw)
        if not bullet:
            continue
        text = bullet.group(1).strip()
        if text and not re.match(r"^\(nothing yet\)$", text, re.IGNORECASE):
            out[current].append(text)
    return out


def _frontmatter(text: str, key: str) -> str:
    match = re.search(rf"^{key}:[ \t]*(.*)$", text, re.IGNORECASE | re.MULTILINE)
    return match.group(1).strip() if match else ""


def read_person(slug: str) -> dict | None:
    path = _file_for(slug)
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    end = text.find("---", 3)
    body = text[end + 3 :] if end != -1 else text
    record = {
        "slug": slug,
        "name": _frontmatter(text, "name") or slug,
        "username": _frontmatter(text, "username"),
        "discordId": _frontmatter(text, "discordId"),
        "updated": _frontmatter(text, "updated"),
    }
    record.update(_parse_sections(body))
    return record


def _serialize(name: str, username: str, discord_id: str, sections: dict[str, list[str]]) -> tuple[dict, str]:
    cap = config["memory"]["maxFactsPerFile"]
    clean: dict[str, list[str]] = {}
    for key in SECTION_KEYS:
        clean[key] = [normalize_fact(f) for f in dedupe_case_insensitive([], sections.get(key, [])) if normalize_fact(f)]

    def bullets(items: list[str]) -> str:
        return "\n".join(f"- {i}" for i in items) if items else "- (nothing yet)"

    body: list[str] = []
    for key in SECTION_KEYS:
        body += [f"## {SECTION_NAMES[key]}", bullets(clean[key]), ""]

    text = "\n".join(
        [
            "---",
            f"name: {name}",
            f"username: {username}",
            f"discordId: {discord_id}",
            f"updated: {datetime.now(timezone.utc).isoformat()}",
            "---",
            "",
            f"# {name}",
            "",
            *body,
        ]
    )
    return clean, text


def _meta(slug: str, person: dict, existing: dict | None) -> tuple[str, str, str]:
    return (
        person.get("name") or (existing or {}).get("name") or slug,
        person.get("username") if person.get("username") is not None else (existing or {}).get("username", ""),
        person.get("discordId") if person.get("discordId") is not None else (existing or {}).get("discordId", ""),
    )


def _ensure_dir() -> None:
    """Guarantee memory/ exists before a write.

    The folder is normally created at boot, but if it is ever missing the write
    raises FileNotFoundError and the fact is dropped on the floor - silently,
    mid-reply, for every person. Cheap to prevent, expensive to notice late.
    """
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)


def write_person(slug: str, person: dict, incoming: dict | None = None) -> dict:
    """Create or update a memory file, MERGING new facts into what is stored."""
    existing = read_person(slug)
    name, username, discord_id = _meta(slug, person or {}, existing)

    merged = blank_record()
    for key in SECTION_KEYS:
        merged[key] = list((existing or {}).get(key, []))
        # Add new facts - we want to preserve all facts permanently
        new_items = list((incoming or {}).get(key, []))
        for item in new_items:
            if item not in merged[key]:
                merged[key].append(item)

    clean, text = _serialize(name, username, discord_id, merged)
    _ensure_dir()
    atomic_write(_file_for(slug), text)
    return {"slug": slug, "name": name, **clean}


def replace_facts(slug: str, incoming: dict | None = None) -> dict:
    """Overwrite the fact lists outright. Used by removals so deletes stick."""
    existing = read_person(slug)
    name, username, discord_id = _meta(slug, {}, existing)
    clean, text = _serialize(
        name, username, discord_id, {k: (incoming or {}).get(k, []) for k in SECTION_KEYS}
    )
    _ensure_dir()
    atomic_write(_file_for(slug), text)
    return {"slug": slug, **clean}


def count_facts(person: dict | None) -> int:
    if not person:
        return 0
    return sum(len(person.get(key, [])) for key in SECTION_KEYS)


def add_facts(slug: str, person: dict, incoming: dict | None = None) -> tuple[list[dict], dict | None]:
    """Append facts, returning only what was genuinely new."""
    before = read_person(slug)
    fresh = blank_record()
    total = 0
    for key in SECTION_KEYS:
        fresh[key] = dedupe_case_insensitive((before or {}).get(key, []), (incoming or {}).get(key, []))
        total += len(fresh[key])

    if total == 0:
        return [], before

    write_person(slug, person, fresh)
    added = [{"kind": key, "text": text} for key in SECTION_KEYS for text in fresh[key]]
    return added, read_person(slug)


def forget(slug: str, query: str) -> dict:
    person = read_person(slug)
    if not person:
        return {"ok": False, "reason": "no-file"}
    needle = (query or "").strip().lower()
    if not needle:
        return {"ok": False, "reason": "no-query"}

    kept = blank_record()
    removed_items: list[str] = []
    for key in SECTION_KEYS:
        kept[key] = [i for i in person[key] if needle not in i.lower()]
        removed_items += [i for i in person[key] if needle in i.lower()]

    removed = sum(len(person[k]) for k in SECTION_KEYS) - sum(len(kept[k]) for k in SECTION_KEYS)
    if removed == 0:
        return {"ok": False, "reason": "not-found"}

    replace_facts(slug, kept)
    return {"ok": True, "removed": removed, "removedItems": removed_items}


def forget_all(slug: str) -> bool:
    path = _file_for(slug)
    if not path.exists():
        return False
    path.unlink()
    return True


def list_people() -> list[dict]:
    out = []
    for path in sorted(MEMORY_DIR.glob("*.md")):
        person = read_person(path.stem)
        if person:
            out.append(person)
    return out


def search_facts(person: dict, query: str) -> list[dict]:
    needle = (query or "").strip().lower()
    if not needle:
        return []
    hits = []
    for key in SECTION_KEYS:
        for item in person.get(key, []):
            if needle in item.lower():
                hits.append({"section": key, "text": item})
    return hits


def describe_person(person: dict, max_len: int = 240) -> str:
    lines = [f"**{person.get('name')}** - memory/{person.get('slug')}.md"]
    any_fact = False
    for key in SECTION_KEYS:
        items = person.get(key, [])
        if not items:
            continue
        any_fact = True
        lines.append(f"**{SECTION_NAMES[key]}**: {truncate('; '.join(items), max_len)}")
    if not any_fact:
        lines.append("no facts stored yet")
    return "\n".join(lines)


__all__ = [
    "SECTION_NAMES",
    "SECTION_KEYS",
    "MAX_CHARS",
    "blank_record",
    "normalize_fact",
    "read_person",
    "write_person",
    "replace_facts",
    "count_facts",
    "add_facts",
    "forget",
    "forget_all",
    "list_people",
    "search_facts",
    "describe_person",
    "slugify",
]
