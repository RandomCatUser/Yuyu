"""Prefix commands.

A hand-rolled router rather than discord.py's framework, so the exact command
set (including `!secret.me`) behaves identically to the previous build.
"""

from __future__ import annotations

import re
import shlex

from .. import private as private_mod
from .. import reset as reset_mod
from ..affinity import crush_summary as crush_summary_affinity
from ..affinity import get as get_affinity
from ..affinity import is_crush_eligible_pronoun
from ..affinity import ranked as ranked_affinity
from ..affinity import save as save_affinity
from ..affinity import set_pronouns
from ..affinity import summarise as summarise_affinity
from ..chat import scope_of_message, slug_for
from ..config import PREFIX, SKILLS_DIR, config, secrets
from ..context import clear as clear_context
from ..context import stats as buffer_stats
from ..formatting import error_embed, info_embed, ok_embed, warn_embed
from ..memory import (
    SECTION_KEYS,
    SECTION_NAMES,
    add_facts,
    count_facts,
    describe_person,
    forget,
    forget_all,
    list_people,
    read_person,
    search_facts,
)
from ..skills import load_skills
from ..stickers import sticker_inventory
from . import memory_editor

# ViewChannel + SendMessages + ReadMessageHistory + AttachFiles
INVITE_PERMISSIONS = 202752

COMMANDS: dict[str, dict] = {}


def command(name: str, description: str, usage: str, aliases: list[str] | None = None,
            owner_only: bool = False):
    def decorator(func):
        entry = {"name": name, "description": description, "usage": usage, "handler": func,
                 "aliases": aliases or [], "owner_only": owner_only}
        COMMANDS[name] = entry
        for alias in (aliases or []):
            COMMANDS[alias] = entry
        return func

    return decorator


def parse(message) -> tuple[str, str] | None:
    content = (message.content or "").strip()
    if not content.startswith(PREFIX):
        return None
    body = content[len(PREFIX):]
    parts = body.split(None, 1)
    return parts[0].lower(), (parts[1].strip() if len(parts) > 1 else "")


def is_command(message) -> bool:
    parsed = parse(message)
    return bool(parsed and parsed[0] in COMMANDS)


async def run(message) -> bool:
    parsed = parse(message)
    if not parsed or parsed[0] not in COMMANDS:
        return False
    entry = COMMANDS[parsed[0]]

    if entry["owner_only"] and not private_mod.is_owner(message.author.id):
        await message.reply(embed=warn_embed(title=private_mod.denial_for(message.author.id)))
        return True

    try:
        args = shlex.split(parsed[1]) if parsed[1] else []
    except ValueError:
        args = parsed[1].split()
    try:
        await entry["handler"](message, parsed[1], args)
    except Exception as exc:
        print(f"[cmd:{entry['name']}] {exc}")
        await message.reply(embed=error_embed(title="That command broke", description=str(exc)))
    return True


def _section(raw: str | None) -> str | None:
    if not raw:
        return None
    low = raw.lower()
    for key in SECTION_KEYS:
        if key == low or SECTION_NAMES[key].lower() == low:
            return key
    return None


# --- help / overview -------------------------------------------------------

@command("help", "List commands", f"{PREFIX}help")
async def _help(message, raw: str, args: list[str]):
    seen: set[str] = set()
    fields = []
    for entry in COMMANDS.values():
        if entry["name"] in seen:
            continue
        seen.add(entry["name"])
        note = "  *(owner only)*" if entry["owner_only"] else ""
        fields.append({"name": f"`{entry['usage']}`", "value": f"{entry['description']}{note}", "inline": True})
    await message.channel.send(
        embed=info_embed(
            title="Yuyu - what I can do",
            description=(
                "You can also just talk to me normally. Mention my name, reply to me, or DM me - "
                f"I keep up with the conversation and remember things you tell me.\n\n"
                f"Dashboard: http://{config['dashboard']['host']}:{config['dashboard']['port']}"
            ),
            fields=fields,
            footer=f"{PREFIX}about to review what I know about you",
        )
    )


# --- memory ----------------------------------------------------------------

@command("about", "Review and edit what I know about you (buttons and forms)",
         f"{PREFIX}about", aliases=["aboutme", "whoknowsme", "profile", "memories"])
async def _about(message, raw: str, args: list[str]):
    await memory_editor.open_editor(message)


@command("remember", "Save a fact. Optional section: " + ", ".join(SECTION_NAMES.values()),
         f"{PREFIX}remember [section] <fact>", aliases=["note", "save"])
async def _remember(message, raw: str, args: list[str]):
    if not config["memory"]["enabled"]:
        await message.reply(embed=warn_embed(title="Memory is off"))
        return

    mentioned = message.mentions[0] if message.mentions else None
    if mentioned:
        args = args[1:]

    section = _section(args[0]) if args else None
    if section:
        args = args[1:]

    fact = " ".join(args).strip()
    if not fact:
        await message.reply(
            embed=warn_embed(
                title="What should I remember?",
                description=(
                    f"Try `{PREFIX}remember works nights` or `{PREFIX}remember likes coffee`.\n"
                    f"Sections: {', '.join(SECTION_NAMES.values())}\n"
                    f"For someone else: `{PREFIX}remember @user <fact>`. Or use {PREFIX}about and the buttons."
                ),
            )
        )
        return

    target = mentioned or message.author
    slug = slug_for(target)
    added, _ = add_facts(
        slug,
        {"name": target.display_name, "username": target.name, "discordId": str(target.id)},
        {section or "notes": [fact]},
    )
    if added:
        await message.channel.send(
            embed=ok_embed(
                title=f"Saved to {SECTION_NAMES[section or 'notes']}",
                description=f"memory/{slug}.md\n" + "\n".join(a["text"] for a in added),
            )
        )
    else:
        await message.reply(embed=warn_embed(title="Already had that"))


@command("forget", "Remove matching stored facts, or everything with 'all'",
         f"{PREFIX}forget <text>  |  {PREFIX}forget all", aliases=["unremember"])
async def _forget(message, raw: str, args: list[str]):
    slug = slug_for(message.author)
    query = raw.strip()
    if not query:
        await message.reply(embed=warn_embed(title="What should I forget?", description=f"Try `{PREFIX}forget nights`"))
        return

    if query.lower() == "all":
        removed = forget_all(slug)
        await message.reply(
            embed=warn_embed(title="Wiped", description=f"memory/{slug}.md is gone.") if removed
            else info_embed(title="Nothing to wipe")
        )
        return

    result = forget(slug, query)
    if result["ok"]:
        await message.reply(
            embed=ok_embed(title=f"Removed {result['removed']} fact(s)", description="\n".join(result.get("removedItems") or []))
        )
    elif result["reason"] == "no-file":
        await message.reply(embed=info_embed(title="No file for you yet"))
    elif result["reason"] == "not-found":
        await message.reply(embed=warn_embed(title="No match", description=f"Nothing about \"{query[:60]}\"."))
    else:
        await message.reply(embed=warn_embed(title="Give me something to match"))


@command("search", "Search your own stored facts", f"{PREFIX}search <text>")
async def _search(message, raw: str, args: list[str]):
    if not raw.strip():
        await message.reply(embed=warn_embed(title="Search for what?"))
        return
    person = read_person(slug_for(message.author))
    if not person:
        await message.reply(embed=info_embed(title="No file for you yet"))
        return
    hits = search_facts(person, raw)
    if hits:
        await message.reply(
            embed=info_embed(
                title=f"{len(hits)} match(es) for \"{raw.strip()[:40]}\"",
                fields=[{"name": SECTION_NAMES[h["section"]], "value": h["text"], "inline": True} for h in hits],
            )
        )
    else:
        await message.reply(embed=warn_embed(title="No matches", description=f"Nothing stored about \"{raw.strip()[:60]}\"."))


@command("people", "Everyone I have a memory file for", f"{PREFIX}people")
async def _people(message, raw: str, args: list[str]):
    people = sorted(list_people(), key=count_facts, reverse=True)
    if not people:
        await message.reply(embed=info_embed(title="No memory files yet"))
        return

    fields = []
    for person in people[:20]:
        summary = describe_person(person, max_len=120).split("\n")[1:]
        fields.append({
            "name": f"{person['name']} ({count_facts(person)})",
            "value": "\n".join(summary) or "empty",
            "inline": True,
        })
    if len(people) > 20:
        fields.append({"name": "more", "value": f"{len(people) - 20} others", "inline": True})
    await message.channel.send(embed=info_embed(title=f"People I know ({len(people)})", fields=fields, footer="memory/*.md"))


# --- skills / stickers -----------------------------------------------------

@command("skills", "List the skill files in skills/", f"{PREFIX}skills")
async def _skills(message, raw: str, args: list[str]):
    skills = load_skills(force=True)
    if not skills:
        await message.reply(embed=info_embed(title="No skills yet", description=f"Drop .md files in `{SKILLS_DIR.name}/`."))
        return
    fields = [
        {
            "name": f"{s['file']}{' - always on' if s['always'] else ''}",
            "value": (s["description"][:200] + (f"\n_Keywords:_ {', '.join(s['keywords'])}" if s["keywords"] else "")),
            "inline": False,
        }
        for s in skills
    ]
    orphans = [p.name for p in SKILLS_DIR.iterdir() if p.suffix != ".md"]
    if orphans:
        fields.append({"name": "ignored", "value": f"not .md: {', '.join(orphans)}", "inline": False})
    await message.channel.send(embed=info_embed(title=f"Skills ({len(skills)})", fields=fields, footer="skills/*.md"))


@command("stickers", "Sticker images and emoji status", f"{PREFIX}stickers")
async def _stickers(message, raw: str, args: list[str]):
    data = sticker_inventory()
    await message.reply(
        embed=info_embed(
            title="Stickers",
            description=(
                "Bots cannot send real Discord stickers, so I send the image as an attachment instead - "
                "same look in chat.\nAdd images to `stickers/` and point at them from `stickers.json`."
            ),
            fields=[
                {"name": "Images on disk", "value": str(len(data["files"])), "inline": True},
                {"name": "Keyword-linked", "value": str(len(data["images"])), "inline": True},
                {"name": "Missing files", "value": str(len(data["missing"])) if data["missing"] else "none", "inline": True},
                {"name": "Custom emoji", "value": str(data["serverEmoji"]), "inline": True},
                {"name": "Sticker chance", "value": f"{round(config['emoji']['stickerChance'] * 100)}%", "inline": True},
                {"name": "Reaction chance", "value": f"{round(config['emoji']['reactionChance'] * 100)}%", "inline": True},
            ]
            + ([{"name": "missing", "value": ", ".join(data["missing"]), "inline": False}] if data["missing"] else []),
        )
    )


# --- affect ----------------------------------------------------------------

@command("pronouns", "Set your pronouns so I know how to talk to you", f"{PREFIX}pronouns <he/him>")
async def _pronouns(message, raw: str, args: list[str]):
    if not raw.strip():
        record = get_affinity(slug_for(message.author))
        await message.reply(
            embed=info_embed(
                title="Your pronouns",
                description=(record or {}).get("pronouns") or "not set yet",
                footer=f"set with: {PREFIX}pronouns he/him",
            )
        )
        return

    if len(raw) > 40:
        await message.reply(embed=warn_embed(title="That is too long to be pronouns"))
        return
    set_pronouns(slug_for(message.author), message.author.display_name, raw.strip())
    eligible = is_crush_eligible_pronoun(raw)
    await message.reply(
        embed=ok_embed(
            title="Got it",
            description=(
                f"Saved as **{raw.strip().lower()}**.\n"
                + ("" if eligible else "I'll treat you exactly the same either way - it only changes one thing I keep to myself.")
            ),
        )
    )


@command("bond", "How she feels about you", f"{PREFIX}bond", aliases=["vibes", "howshefeels"])
async def _bond(message, raw: str, args: list[str]):
    record = get_affinity(slug_for(message.author))
    if not record:
        await message.reply(embed=info_embed(title="Nothing yet", description="Talk to me a bit and this will fill in."))
        return
    summary = summarise_affinity(record)
    line = {
        "positive": "We get on.",
        "negative": "I am not always fond of you.",
        "neutral": "We are still getting to know each other.",
    }[summary["read"]["warmth"]]
    await message.reply(
        embed=info_embed(
            title="Between us",
            description=f"{line}\nHow well I know you: **{summary['read']['familiarity']}**\n"
            f"Times we have talked: **{summary['interactions']}**",
            footer="the numbers are hers, not yours - she keeps those to herself",
        )
    )


@command("affinity", "(owner only) everyone's levels, and who she has a thing for",
         f"{PREFIX}affinity", owner_only=True)
async def _affinity(message, raw: str, args: list[str]):
    if not config["affinity"]["enabled"]:
        await message.reply(embed=warn_embed(title="Affect is off"))
        return
    rows = ranked_affinity()
    if not rows:
        await message.reply(embed=info_embed(title="Nobody yet", description="No one has interacted enough to have a profile."))
        return

    crush = crush_summary_affinity()
    fields = []
    for entry in rows[:20]:
        if entry["pronouns"]:
            label = f"{entry['name']} ({entry['pronouns']})"
        else:
            label = f"{entry['name']} - pronouns unknown"
        if crush and entry["slug"] == crush["slug"]:
            label += " ★"
        state = "eligible" if entry["eligible"] else "not eligible"
        if not entry["crushEnabled"]:
            state += " · opt-out"
        fields.append({
            "name": label,
            "value": f"warmth {entry['warmth']} · known {entry['familiarity']} · turns {entry['interactions']}\n{state}",
            "inline": True,
        })

    unknown = sum(1 for r in rows if not r["pronouns"])
    blocked = sum(1 for r in rows if not r["crushEnabled"])
    footer_bits = []
    if unknown:
        footer_bits.append(f"{unknown} without pronouns (use {PREFIX}pronouns)")
    if blocked:
        footer_bits.append(f"{blocked} opted out")
    footer_bits.append("affinity/*.json")

    if crush:
        headline = f"She has a thing for {crush['name']}"
        detail = (f"romance {crush['romance']} ({crush['read']['romance']}) · warmth {crush['warmth']}\n"
                  "She will not say it. You are the only one who can see this.")
    else:
        headline = "She has nobody"
        detail = ("No one is currently eligible. She only falls for "
                  f"{', '.join(config['affinity']['eligiblePronouns'])}, and only one person at a time.")

    await message.channel.send(
        embed=info_embed(title=headline, description=detail, fields=fields, footer=" · ".join(footer_bits))
    )


@command("crush", "Turn the crush off for yourself, or (owner) view it", f"{PREFIX}crush [off|on]")
async def _crush(message, raw: str, args: list[str]):
    slug = slug_for(message.author)
    action = raw.strip().lower()

    if action in ("off", "on"):
        record = get_affinity(slug)
        if not record:
            await message.reply(embed=info_embed(title="Nothing to change yet"))
            return
        record["crushEnabled"] = action == "on"
        if action == "off":
            record["romance"] = 0
        save_affinity(record)
        await message.reply(
            embed=ok_embed(
                title="That is back on" if action == "on" else "Done - I will not",
                description=None if action == "on" else "Anything I felt is cleared. We are just friends. I will not bring it up.",
            )
        )
        return

    if not private_mod.is_owner(message.author.id):
        await message.reply(
            embed=info_embed(title="Nothing to see",
                             description=f"I am not telling. Use `{PREFIX}crush off` if you would rather I did not.")
        )
        return
    await _affinity(message, raw, args)


# --- context / misc --------------------------------------------------------

@command("reset", "Clear short-term conversation context for this channel",
         f"{PREFIX}reset", aliases=["clear"])
async def _reset(message, raw: str, args: list[str]):
    guild_id, channel_id = scope_of_message(message)
    clear_context(guild_id, channel_id)
    await message.reply(
        embed=ok_embed(
            title="Context cleared",
            description="I have forgotten the last bit of this conversation.\nLong-term stuff about you is untouched.",
        )
    )


@command("buffer", "Debug: show buffered channel context", f"{PREFIX}buffer")
async def _buffer(message, raw: str, args: list[str]):
    rows = [b for b in buffer_stats() if str(scope_of_message(message)[1]) in b["key"]]
    await message.reply(
        embed=info_embed(
            title="Buffered context",
            description="\n".join(f"`{b['key']}`: {b['entries']} msg(s)" for b in rows) or "Nothing buffered in this channel.",
        )
    )


@command("wipe", "(owner only) full data reset - or use the dashboard Reset tab",
         f"{PREFIX}wipe [learned]", aliases=["factoryreset"], owner_only=True)
async def _wipe(message, raw: str, args: list[str]):
    targets = ["buffers"] if raw.strip().lower() == "learned" else ["buffers", "affect"]
    plan = await reset_mod.preview(targets)
    listing = "\n".join(f"• {r['label']} ({r['count']} {r['unit']})" for r in plan["removed"]) or "nothing"
    await message.reply(
        embed=warn_embed(
            title="This wipes learned data in every server",
            description=f"{listing}\n\nA backup is written first. For anything else, use the dashboard Reset tab.",
        )
    )
    if "confirm" in raw.lower():
        result = await reset_mod.run(targets)
        await message.channel.send(embed=ok_embed(title="Done", description="\n".join(result["deleted"]) or "nothing"))


@command("invite", "Print the OAuth2 install link for this bot", f"{PREFIX}invite")
async def _invite(message, raw: str, args: list[str]):
    app_id = secrets.get("discord_application_id") or secrets.get("discord_client_id")
    if not app_id:
        await message.reply(embed=warn_embed(title="No application id in .env"))
        return
    url = (f"https://discord.com/oauth2/authorize?client_id={app_id}"
           f"&scope=bot%20applications.commands&permissions={INVITE_PERMISSIONS}")
    await message.reply(embed=info_embed(title="Add me to a server", description=url))


@command("ping", "Check the bot is alive", f"{PREFIX}ping")
async def _ping(message, raw: str, args: list[str]):
    sent = await message.reply("pong")
    # discord.py 2.7 exposes `created_at`, not `created_timestamp`.
    sent_at = getattr(sent, "created_at", None)
    asked_at = getattr(message, "created_at", None)
    if sent_at and asked_at:
        delta = (sent_at - asked_at).total_seconds() * 1000
        await sent.edit(content=f"pong - {delta:.0f}ms")
    else:
        await sent.edit(content="pong")


# --- owner-only: the private file -----------------------------------------

@command("secret.me", "(owner only) your private file", f"{PREFIX}secret.me",
         aliases=["secret", "whois.me"], owner_only=True)
async def _secret_me(message, raw: str, args: list[str]):
    if not config["private"]["enabled"]:
        await message.reply(embed=warn_embed(title="Turned off"))
        return
    try:
        text = await private_mod.read_profile()
    except FileNotFoundError:
        await message.reply(embed=warn_embed(title="That file does not exist"))
        return
    title = str(config["private"].get("profileTitle") or "Private profile")
    await message.channel.send(
        embed=info_embed(title=title, description=text[:3900], color="purple", footer="private/ · owner only")
    )


@command("secret.add", "(owner only) add a line to the private file", f"{PREFIX}secret.add <fact> [section]",
         owner_only=True)
async def _secret_add(message, raw: str, args: list[str]):
    parts = list(args)
    sections = {"identity", "details", "preferences", "notes"}
    section = "Notes"
    if parts and parts[-1].lower() in sections:
        section = parts.pop()
    fact = " ".join(parts).strip()
    if not fact:
        await message.reply(embed=warn_embed(title="What should I add?", description=f"Try `{PREFIX}secret.add drinks too much coffee`"))
        return
    result = private_mod.add_fact(fact, section)
    if not result["ok"]:
        await message.reply(embed=error_embed(title="Could not add that", description=result["reason"]))
        return
    await message.reply(embed=ok_embed(title=f"Added to {result['section']}"))


@command("secret.forget", "(owner only) remove a line from the private file",
         f"{PREFIX}secret.forget <text>", owner_only=True)
async def _secret_forget(message, raw: str, args: list[str]):
    result = private_mod.forget_fact(raw)
    if not result["ok"]:
        await message.reply(
            embed=warn_embed(title="No matching line" if result["reason"] == "not-found" else "Give me something to match")
        )
        return
    await message.reply(embed=ok_embed(title=f"Dropped {result['removed']} line(s)"))


# --- owner-only: reset -----------------------------------------------------

@command("reset.preview", "(owner only) what a reset would remove", f"{PREFIX}reset.preview [targets]",
         owner_only=True)
async def _reset_preview(message, raw: str, args: list[str]):
    targets = [a for a in args if a in reset_mod.TARGETS] or ["buffers"]
    plan = await reset_mod.preview(targets)
    lines = []
    for entry in plan["removed"]:
        lines.append(f"**{entry['label']}** - {entry['count']} {entry['unit']}")
    lines.append("")
    lines.append("Kept: " + "; ".join(plan["kept"]))
    if plan["typedConfirmation"]:
        lines.append(f"\nThis needs `{PREFIX}reset.run` with confirm={plan['typedConfirmation']}")
    await message.channel.send(
        embed=warn_embed(title="Reset preview", description="\n".join(lines) or "nothing selected")
    )


@command("reset.run", "(owner only) actually reset", f"{PREFIX}reset.run <targets> [confirm=RESET]",
         owner_only=True)
async def _reset_run(message, raw: str, args: list[str]):
    targets = [a for a in args if a in reset_mod.TARGETS]
    plan = await reset_mod.preview(targets)
    if not plan["removed"]:
        await message.reply(embed=warn_embed(title="Nothing selected"))
        return
    if plan["typedConfirmation"] and "confirm=RESET" not in raw:
        await message.reply(embed=error_embed(title=f"Type confirm=RESET to continue", description="This deletes hand-written files."))
        return
    result = await reset_mod.run(targets)
    description = "\n".join(result["deleted"]) or "nothing"
    if result.get("backup"):
        description += f"\n\nBackup: backups/{result['backup']['id']} ({result['backup']['files']} file(s))"
    await message.channel.send(embed=ok_embed(title="Reset complete", description=description))
