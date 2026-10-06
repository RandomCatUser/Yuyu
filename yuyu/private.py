
from __future__ import annotations

import random
import re
from pathlib import Path

from .config import PRIVATE_DIR, ROOT, config

SECTIONS = ["Identity", "What he works on", "Details", "Preferences", "Notes"]
DENIALS = [
    "no.",
    "not for you.",
    "that's not something you get to read.",
]


def profile_file() -> Path:
    return ROOT / config["private"]["profileFile"]


def is_owner(user_id) -> bool:
    return bool(config["private"]["enabled"]) and str(user_id) in {
        str(u) for u in config["private"]["ownerIds"]
    }


def denial_for(user_id) -> str | None:
    return None if is_owner(user_id) else random.choice(DENIALS)


async def read_profile() -> str:
    return profile_file().read_text(encoding="utf-8")


def render_for_chat(text: str) -> str:
    """Strip the leading access notice so the channel never sees the warning."""
    end = text.find("---")
    return (text[end + 3 :] if end != -1 else text).strip()


def _find_section(lines: list[str], wanted: str) -> int:
    target = wanted.strip().lower()
    for index, line in enumerate(lines):
        match = re.match(r"^##\s+(.+?)\s*$", line)
        if match and match.group(1).strip().lower() == target:
            return index
    return -1


def add_fact(fact: str, section: str = "Notes") -> dict:
    text = str(fact or "").strip()
    if not text:
        return {"ok": False, "reason": "empty"}

    target = next((s for s in SECTIONS if s.lower() == str(section).lower()), None)
    if not target:
        return {"ok": False, "reason": "bad-section", "sections": SECTIONS}

    path = profile_file()
    lines = path.read_text(encoding="utf-8").split("\n")
    at = _find_section(lines, target)

    if at == -1:
        lines += ["", f"## {target}", f"- {text}"]
    else:
        end = at + 1
        while end < len(lines) and not re.match(r"^##\s", lines[end]):
            end += 1
        while end > at + 1 and not lines[end - 1].strip():
            end -= 1
        lines.insert(end, f"- {text}")

    path.write_text("\n".join(lines), encoding="utf-8")
    return {"ok": True, "fact": text, "section": target}


def forget_fact(query: str) -> dict:
    needle = str(query or "").strip().lower()
    if not needle:
        return {"ok": False, "reason": "empty"}

    path = profile_file()
    lines = path.read_text(encoding="utf-8").split("\n")
    removed = 0
    kept: list[str] = []
    for line in lines:
        match = re.match(r"^\s*[-*]\s+(.*\S)\s*$", line)
        if match and "<!--" not in line and needle in match.group(1).lower():
            removed += 1
            continue
        kept.append(line)

    if not removed:
        return {"ok": False, "reason": "not-found"}
    path.write_text("\n".join(kept), encoding="utf-8")
    return {"ok": True, "removed": removed}
