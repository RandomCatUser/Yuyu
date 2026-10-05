"""Output formatting: <card> markup into Discord embeds.

Her text stays plain; only the <card> becomes an embed, one per reply, never
around a casual line."""

from __future__ import annotations

import re

import discord

from .util import split_message

CARD_OPEN = re.compile(r"<card(?P<attrs>[^>]*)>", re.IGNORECASE)
CARD_CLOSE = re.compile(r"</card>", re.IGNORECASE)
FIELD_LINE = re.compile(r"^\s*field\s*=\s*(.+?)\s*::\s*(.*)$", re.IGNORECASE)
ATTR = re.compile(r"(\w+)\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s>]+))")

COLORS = {
    "blue": 0x5865F2, "blurple": 0x5865F2, "green": 0x57F287, "red": 0xED4245,
    "yellow": 0xFEE75C, "orange": 0xFAA61A, "purple": 0x9B59B6, "pink": 0xF47BBA,
    "grey": 0x808080, "gray": 0x808080, "teal": 0x1ABC9C, "cyan": 0x00B0F4,
}

LIMITS = {"title": 256, "description": 4096, "field_name": 256, "field_value": 1024, "fields": 25}


def _parse_attrs(raw: str) -> dict:
    attrs = {}
    for match in ATTR.finditer(raw or ""):
        attrs[match.group(1).lower()] = match.group(2) or match.group(3) or match.group(4) or ""
    return attrs


def _color(value: str | None) -> int:
    if not value:
        return COLORS["blurple"]
    text = str(value).strip()
    # Discord colours run 0x000000-0xFFFFFF, i.e. up to 8 decimal digits.
    if text.isdigit():
        number = int(text)
        if 0 <= number <= 0xFFFFFF:
            return number
    return COLORS.get(text.lower(), COLORS["blurple"])


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _build_card(attrs: dict, body: str) -> discord.Embed:
    embed = discord.Embed(color=_color(attrs.get("color")), timestamp=discord.utils.utcnow())
    if attrs.get("title"):
        embed.title = _truncate(attrs["title"], LIMITS["title"])
    if attrs.get("footer"):
        embed.set_footer(text=_truncate(attrs["footer"], 2048))
    if attrs.get("url", "").startswith(("http://", "https://")):
        embed.url = attrs["url"]

    fields: list[tuple[str, str]] = []
    body_lines: list[str] = []
    for line in str(body or "").split("\n"):
        match = FIELD_LINE.match(line)
        if match:
            fields.append((match.group(1), match.group(2)))
        else:
            body_lines.append(line)

    description = "\n".join(body_lines).strip()
    if description:
        embed.description = _truncate(description, LIMITS["description"])

    for name, value in fields[: LIMITS["fields"]]:
        name = name.strip()
        embed.add_field(
            name=_truncate(name, LIMITS["field_name"]),
            value=_truncate(value.strip(), LIMITS["field_value"]) or " ",
            inline=len(name) < 25,
        )
    return embed


def parse_reply(raw: str) -> tuple[list[str], list[discord.Embed]]:
    """Split a reply into conversational text and any card blocks, in order."""
    text = str(raw or "")
    parts: list[str] = []
    embeds: list[discord.Embed] = []
    rest = text

    while True:
        opening = CARD_OPEN.search(rest)
        if not opening:
            break
        closing = CARD_CLOSE.search(rest, opening.end())
        if not closing:
            break  # unterminated - treat the rest as plain text

        before = rest[: opening.start()].strip()
        if before:
            parts.append(before)
        embeds.append(_build_card(_parse_attrs(opening.group("attrs") or ""), rest[opening.end() : closing.start()]))
        rest = rest[closing.end() :]

    tail = rest.strip()
    if tail:
        parts.append(tail)
    return parts, embeds


def looks_structured(text: str) -> bool:
    t = str(text or "")
    if CARD_OPEN.search(t):
        return True
    bullets = len(re.findall(r"^\s*[-*]\s+", t, re.MULTILINE))
    headers = len(re.findall(r"^#{1,3}\s+", t, re.MULTILINE))
    return bullets >= 4 or headers >= 2


# --- command embeds --------------------------------------------------------

def _base(title: str | None, description: str | None, color: str, footer: str) -> discord.Embed:
    embed = discord.Embed(color=_color(color), timestamp=discord.utils.utcnow())
    if title:
        embed.title = _truncate(title, LIMITS["title"])
    if description:
        embed.description = _truncate(description, LIMITS["description"])
    embed.set_footer(text=_truncate(footer, 2048))
    return embed


def _add_fields(embed: discord.Embed, fields) -> None:
    for field in fields or []:
        if not field or not field.get("name"):
            continue
        name = str(field["name"])
        embed.add_field(
            name=_truncate(name, LIMITS["field_name"]),
            value=_truncate(str(field.get("value", ""))[: LIMITS["field_value"]], LIMITS["field_value"]) or " ",
            inline=field.get("inline", len(name) < 25),
        )


def info_embed(title=None, description=None, fields=None, footer="Yuyu", color="blue", thumbnail=None):
    embed = _base(title, description, color, footer)
    if thumbnail:
        try:
            embed.set_thumbnail(url=thumbnail)
        except Exception:
            pass
    _add_fields(embed, fields)
    return embed


def ok_embed(**kwargs):
    kwargs.setdefault("color", "green")
    return info_embed(**kwargs)


def warn_embed(**kwargs):
    kwargs.setdefault("color", "yellow")
    return info_embed(**kwargs)


def error_embed(**kwargs):
    kwargs.setdefault("color", "red")
    return info_embed(**kwargs)


def plan_output(description: str, embeds=None, max_chars: int = 1900) -> list[dict]:
    """A mix of plain messages and embeds that respect Discord's limits."""
    out: list[dict] = [{"embed": e} for e in (embeds or [])]
    for chunk in split_message(description, max_chars):
        out.append({"content": chunk})
    return out
