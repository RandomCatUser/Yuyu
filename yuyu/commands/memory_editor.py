"""Buttons and modals for adding and removing a remembered fact in Discord."""

from __future__ import annotations

import discord

from ..formatting import error_embed, info_embed, ok_embed, warn_embed
from ..memory import SECTION_KEYS, SECTION_NAMES, add_facts, count_facts, forget, forget_all, read_person
from ..chat import slug_for
from ..util import truncate

BTN_ADD = "mem:add:"
BTN_REMOVE = "mem:remove"
BTN_WIPE = "mem:wipe"
BTN_CLOSE = "mem:close"
BTN_REFRESH = "mem:refresh"
MODAL_PREFIX = "mem:modal:"


def profile_embed(person: dict | None, note: str | None = None) -> discord.Embed:
    person = person or {}
    embed = discord.Embed(
        title=f"What I know about {person.get('name', 'you')}",
        color=0x5865F2,
        timestamp=discord.utils.utcnow(),
    )
    for key in SECTION_KEYS:
        items = person.get(key, [])
        value = "\n".join(f"• {i}" for i in items)[:1024] if items else "*nothing yet*"
        embed.add_field(name=f"{SECTION_NAMES[key]} ({len(items)})", value=value or "*nothing yet*", inline=False)
    footer = f"{person.get('slug', '?')} · {count_facts(person)} fact(s)"
    if note:
        footer += f" · {note}"
    embed.set_footer(text=footer)
    return embed


class MemoryView(discord.ui.View):
    """Ephemeral: only the person who ran the command can use the buttons."""

    def __init__(self, user):
        super().__init__(timeout=600)
        self.user = user
        self.done = False

    async def interaction_check(self, interaction) -> bool:
        if interaction.user.id == self.user.id:
            return True
        await interaction.response.send_message("this isn't yours to click.", ephemeral=True)
        return False

    def _rows(self) -> list[discord.ui.Button]:
        return [b for b in self.children if isinstance(b, discord.ui.Button)]

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.secondary)
    async def refresh(self, interaction, button):
        person = read_person(slug_for(self.user))
        await interaction.response.edit_message(embed=profile_embed(person), view=self)

    @discord.ui.button(label="Remove a fact", style=discord.ButtonStyle.secondary)
    async def remove(self, interaction, button):
        person = read_person(slug_for(self.user))
        total = count_facts(person)
        if not total:
            await interaction.response.send_message(embed=warn_embed(title="Nothing stored yet"), ephemeral=True)
            return
        await interaction.response.send_modal(remove_modal(total))

    @discord.ui.button(label="Wipe everything", style=discord.ButtonStyle.danger)
    async def wipe(self, interaction, button):
        removed = forget_all(slug_for(self.user))
        self.done = True
        for item in self._rows():
            item.disabled = True
        await interaction.response.edit_message(
            embed=warn_embed(title="Wiped", description="Everything I had about you is gone.") if removed
            else info_embed(title="Nothing to wipe"),
            view=self,
        )

    @discord.ui.button(label="Done", style=discord.ButtonStyle.success)
    async def close(self, interaction, button):
        self.done = True
        for item in self._rows():
            item.disabled = True
        await interaction.response.edit_message(view=None)
        await interaction.response.send_message("closed. nothing changed.", ephemeral=True)
        self.stop()


def build_view(user) -> MemoryView:
    view = MemoryView(user)
    for key in SECTION_KEYS:
        button = discord.ui.Button(
            label=f"+ {SECTION_NAMES[key]}", style=discord.ButtonStyle.secondary, custom_id=f"{BTN_ADD}{key}"
        )
        button.callback = _make_add_callback(key)
        view.add_item(button)
    return view


def _make_add_callback(section: str):
    async def callback(self, interaction, button):
        await interaction.response.send_modal(add_modal(section))

    return callback


def add_modal(section: str) -> discord.ui.Modal:
    modal = discord.ui.Modal(title=f"Add to {SECTION_NAMES[section]}")
    modal.add_item(
        discord.ui.TextInput(
            label=f"New {SECTION_NAMES[section].lower()}",
            placeholder="works nights",
            style=discord.TextStyle.paragraph,
            max_length=1000,
            required=True,
        )
    )
    modal.custom_id = f"{MODAL_PREFIX}{section}"
    return modal


def remove_modal(total: int) -> discord.ui.Modal:
    modal = discord.ui.Modal(title=f"Remove a fact ({total} stored)")
    modal.add_item(
        discord.ui.TextInput(
            label="Which one? Type any part of it",
            placeholder="nights",
            style=discord.TextStyle.paragraph,
            max_length=200,
            required=True,
        )
    )
    modal.custom_id = f"{MODAL_PREFIX}remove"
    return modal


class AddFactModal(discord.ui.Modal):
    def __init__(self, section: str):
        super().__init__(title=f"Add to {SECTION_NAMES.get(section, 'Notes')}")
        self.section = section if section in SECTION_KEYS else "notes"
        self.fact = discord.ui.TextInput(
            label=f"New {SECTION_NAMES[self.section].lower()}",
            placeholder="works nights",
            style=discord.TextStyle.paragraph,
            max_length=1000,
            required=True,
        )
        self.add_item(self.fact)

    async def on_submit(self, interaction):
        text = (self.fact.value or "").strip()
        if not text:
            await interaction.response.send_message(embed=warn_embed(title="Nothing entered"), ephemeral=True)
            return
        user = interaction.user
        added, _ = add_facts(
            slug_for(user),
            {"name": user.display_name, "username": user.name, "discordId": str(user.id)},
            {self.section: [text]},
        )
        if not added:
            await interaction.response.send_message(
                embed=warn_embed(title="Already stored", description=f"\"{truncate(text, 80)}\" is already there."),
                ephemeral=True,
            )
            return
        person = read_person(slug_for(user))
        await interaction.response.send_message(
            embed=ok_embed(title=f"Added to {SECTION_NAMES[self.section]}", description="\n".join(a["text"] for a in added)),
            view=build_view(user),
        )


class RemoveFactModal(discord.ui.Modal):
    def __init__(self):
        super().__init__(title="Remove a fact")
        self.query = discord.ui.TextInput(
            label="Which one? Type any part of it",
            placeholder="nights",
            style=discord.TextStyle.paragraph,
            max_length=200,
            required=True,
        )
        self.add_item(self.query)

    async def on_submit(self, interaction):
        text = (self.query.value or "").strip()
        result = forget(slug_for(interaction.user), text)
        if not result["ok"]:
            title = "No match" if result["reason"] == "not-found" else "Nothing stored"
            description = f"Nothing about \"{truncate(text, 60)}\"." if result["reason"] == "not-found" else None
            await interaction.response.send_message(embed=warn_embed(title=title, description=description), ephemeral=True)
            return
        person = read_person(slug_for(interaction.user))
        await interaction.response.send_message(
            embed=ok_embed(title=f"Removed {result['removed']}", description="\n".join(result.get("removedItems") or [])),
            view=build_view(interaction.user),
        )


def is_modal(custom_id: str) -> str | None:
    """Return the section name if this modal belongs to the memory editor."""
    if not str(custom_id or "").startswith(MODAL_PREFIX):
        return None
    return str(custom_id)[len(MODAL_PREFIX):]


async def open_editor(message) -> None:
    user = message.author
    person = read_person(slug_for(user))
    if not person:
        view = build_view(user)
        await message.channel.send(
            embed=info_embed(
                title="Nothing stored about you yet",
                description=(
                    "Use the buttons below to add your first facts, or just tell me in normal chat and\n"
                    "I'll pick it up. You can also say `remember that I work nights`."
                ),
            ),
            view=view,
        )
        return

    view = build_view(user)
    await message.channel.send(embed=profile_embed(person), view=view)
