"""Skill files: skills/*.md with optional frontmatter and keyword matching."""

from __future__ import annotations

import re
import threading
from pathlib import Path

from .config import SKILLS_DIR, config
from .util import keywords_of, truncate

_lock = threading.Lock()
_cache: list[dict] | None = None
_cached_mtime = 0.0


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    match = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n?", text, re.DOTALL)
    if not match:
        return {}, text

    meta: dict = {}
    for line in match.group(1).splitlines():
        kv = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", line)
        if not kv:
            continue
        key, raw = kv.group(1), kv.group(2).strip()
        if re.fullmatch(r"\[.*\]", raw):
            meta[key] = [
                s.strip().strip("\"'") for s in raw[1:-1].split(",") if s.strip()
            ]
        elif raw in ("true", "false"):
            meta[key] = raw == "true"
        else:
            meta[key] = raw.strip("\"'")
    return meta, text[match.end() :]


def load_skills(force: bool = False) -> list[dict]:
    global _cache, _cached_mtime
    with _lock:
        if _cache is not None and not force:
            try:
                if SKILLS_DIR.stat().st_mtime == _cached_mtime:
                    return _cache
            except OSError:
                pass

    skills: list[dict] = []
    for path in sorted(SKILLS_DIR.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        meta, body = _parse_frontmatter(text)
        content = body.strip()
        if not content:
            continue
        skills.append(
            {
                "file": path.name,
                "name": meta.get("name") or path.stem,
                "description": meta.get("description") or truncate(re.sub(r"\s+", " ", content), 140),
                "keywords": [str(k).lower() for k in (meta.get("keywords") or [])],
                "always": meta.get("always") is True,
                "content": content,
            }
        )

    with _lock:
        _cache = skills
        try:
            _cached_mtime = SKILLS_DIR.stat().st_mtime
        except OSError:
            _cached_mtime = 0.0
    return skills


def select_skills(conversation_text: str) -> dict:
    """`always` skills plus the best few keyword matches, so the prompt stays small."""
    skills = load_skills()
    if not skills:
        return {"always": [], "matched": []}

    always = [s for s in skills if s["always"]]
    rest = [s for s in skills if not s["always"]]
    room = max(0, config["context"]["maxSkillFiles"] - len(always))
    if room == 0:
        return {"always": always, "matched": []}

    words = keywords_of(conversation_text)
    scored = [
        (s, sum(1 for k in s["keywords"] if k in words))
        for s in rest
    ]
    scored = [(s, n) for s, n in scored if n > 0]
    scored.sort(key=lambda pair: -pair[1])
    return {"always": always, "matched": [s for s, _ in scored[:room]]}


def render_skills(selected: dict) -> str:
    always = selected.get("always") or []
    matched = selected.get("matched") or []
    if not always and not matched:
        return ""

    blocks: list[str] = []
    if always:
        blocks.append(
            "The user can drop extra personality/knowledge files into the skills/ folder. "
            "These are already in effect:\n"
            + "\n".join(f'<skill name="{s["name"]}">\n{s["content"]}\n</skill>' for s in always)
        )
    if matched:
        blocks.append(
            "These additional skills look relevant to what is being discussed - blend them in naturally:\n"
            + "\n".join(f'<skill name="{s["name"]}">\n{s["content"]}\n</skill>' for s in matched)
        )
    return "\n\n".join(blocks)
