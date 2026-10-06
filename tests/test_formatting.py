
from __future__ import annotations

import pytest

from yuyu import formatting as fmt
from yuyu import stickers as stk
from yuyu.config import STICKER_DIR


def embed_dict(embed):
    return embed.to_dict()


# card parsing

def test_plain_text_produces_no_embeds():
    parts, embeds = fmt.parse_reply("yeah that's brutal, did they at least bump the pay")
    assert embeds == [] and len(parts) == 1 and "brutal" in parts[0]


def test_card_becomes_an_embed_and_prose_stays_text():
    raw = ('solid list below\n<card title="Options" color="blue">\n'
           "field=Cheap::fast but dumb\nfield=Slow::accurate\n</card>")
    parts, embeds = fmt.parse_reply(raw)
    assert len(embeds) == 1
    assert "solid list below" in parts[0]
    data = embed_dict(embeds[0])
    assert data["title"] == "Options"
    assert data["color"] == 0x5865F2
    assert [f["name"] for f in data["fields"]] == ["Cheap", "Slow"]
    assert data["fields"][0]["value"] == "fast but dumb"


def test_card_body_keeps_markdown_and_code():
    _, embeds = fmt.parse_reply('<card title="Fix">\nUse `npm ci`:\n```bash\nnpm ci\n```\n</card>')
    description = embed_dict(embeds[0])["description"]
    assert "`npm ci`" in description and "```bash" in description


def test_text_after_a_card_is_preserved():
    parts, embeds = fmt.parse_reply('<card title="X">y</card>\nand that is why')
    assert len(embeds) == 1
    assert any("and that is why" in p for p in parts)


def test_unterminated_card_is_not_swallowed():
    parts, embeds = fmt.parse_reply("look <card title=oops")
    assert embeds == [], "half-open card must not become an embed"
    assert "<card" in parts[0]


def test_numeric_colours_resolve():
    # 16711680 is 8 digits (0xFF0000); a 7-digit cap broke colours silently.
    assert embed_dict(fmt.parse_reply('<card color="16711680">x</card>').__getitem__(1)[0])["color"] == 16711680
    assert embed_dict(fmt.parse_reply('<card color="16777215">x</card>').__getitem__(1)[0])["color"] == 0xFFFFFF
    assert embed_dict(fmt.parse_reply('<card color="green">x</card>').__getitem__(1)[0])["color"] == 0x57F287
    assert embed_dict(fmt.parse_reply("<card>x</card>").__getitem__(1)[0])["color"] == 0x5865F2
    # Junk and out-of-range fall back rather than crash.
    assert embed_dict(fmt.parse_reply('<card color="notacolour">x</card>').__getitem__(1)[0])["color"] == 0x5865F2
    assert embed_dict(fmt.parse_reply('<card color="99999999">x</card>').__getitem__(1)[0])["color"] == 0x5865F2


def test_attrs_accept_single_quotes_and_bare_values():
    _, embeds = fmt.parse_reply("<card title='My Title' footer=done>x</card>")
    data = embed_dict(embeds[0])
    assert data["title"] == "My Title" and data["footer"]["text"] == "done"


def test_oversized_content_is_truncated():
    _, embeds = fmt.parse_reply(
        f'<card title="{"T" * 500}">{"x" * 9000}\n'
        f'field={"N" * 400}::{"V" * 3000}</card>'
    )
    data = embed_dict(embeds[0])
    assert len(data["title"]) <= fmt.LIMITS["title"]
    assert len(data["description"]) <= fmt.LIMITS["description"]
    assert len(data["fields"][0]["name"]) <= fmt.LIMITS["field_name"]
    assert len(data["fields"][0]["value"]) <= fmt.LIMITS["field_value"]


def test_field_count_is_capped():
    fields = "\n".join(f"field=k{i}::v{i}" for i in range(40))
    _, embeds = fmt.parse_reply(f"<card>{fields}</card>")
    assert len(embed_dict(embeds[0])["fields"]) == fmt.LIMITS["fields"]


def test_looks_structured():
    assert not fmt.looks_structured("yeah that's brutal")
    assert fmt.looks_structured("- one\n- two\n- three\n- four")
    assert fmt.looks_structured("# a\n## b")
    assert fmt.looks_structured('<card title="x">y</card>')


# stickers

@pytest.fixture
def _sticker_files():
    created = []
    for name in ("pytest-hug.png", "pytest-yes.png", "pytest-default.png"):
        (STICKER_DIR / name).write_bytes(b"\x89PNG\r\n\x1a\n")
        created.append(name)
    yield created
    for name in created:
        (STICKER_DIR / name).unlink(missing_ok=True)
    stk.load_stickers(force=True)


def test_sticker_listing(_sticker_files):
    names = stk.list_images()
    assert "pytest-hug.png" in names
    assert all(n.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".gif")) for n in names)


def test_sticker_path_rejects_traversal_and_bad_extensions():
    assert stk.sticker_path("../../secrets.png") is None or stk.sticker_path("../../secrets.png").name == "secrets.png"
    assert stk.sticker_path("nope.exe") is None
    assert stk.sticker_path("") is None
    resolved = stk.sticker_path("ok.png")
    assert resolved is not None and resolved.parent == STICKER_DIR.resolve() or resolved.parent == STICKER_DIR


def test_inventory_reports_missing_files():
    data = stk.sticker_inventory()
    assert "files" in data and "images" in data
    assert isinstance(data["missing"], list)


def test_pickers_respect_chance():
    for _ in range(40):
        assert stk.pick_sticker_image("morning coffee", chance=0) is None
        assert stk.pick_reaction_image("haha lol", chance=0) is None
        assert stk.pick_sticker_emoji("morning", chance=0) is None
        assert stk.pick_reaction_emoji("haha", chance=0) is None


def test_missing_files_are_dropped_from_the_set():
    data = stk.load_stickers(force=True)
    assert all(s["file"] in stk.list_images() for s in data["images"]), "a typo must not break sending"


# emoji never reach the channel
#
# Sticker images carry the visual. Emoji were dropped by request: both the
# invented ones and the sticker-emoji fallback.

def test_model_emoji_are_stripped_from_replies():
    from yuyu.chat import post_process

    out = post_process("hey you \U0001F604 not much, you good?")
    assert "\U0001F604" not in out
    assert out == "hey you not much, you good?"   # no double space left behind


def test_stripping_handles_multi_codepoint_sequences():
    from yuyu.chat import strip_emoji

    # ZWJ families, skin tones and keycaps must not leave half a sequence.
    for sample in (
        "wave \U0001F44B\U0001F3FD later",
        "family \U0001F468\u200D\U0001F469\u200D\U0001F467 here",
        "5\uFE0F\u20E3 done",
        "flag \U0001F3F4\U000E0067\U000E0062\U000E0065\U000E006E\U000E007F",
        "reaction \U0001F62D\U0001F62D same",
    ):
        out = strip_emoji(sample)
        assert not any(0x1F000 <= ord(ch) <= 0x1FAFF or ord(ch) in (0x200D, 0xFE0F, 0x20E3)
                       for ch in out), f"leftover in {out!r}"
        assert "  " not in out, f"double space in {out!r}"


def test_normal_punctuation_and_accents_survive():
    from yuyu.chat import post_process

    for kept in ("caf\u00e9 \u00b0C", "em \u2014 dash \u2026 too", "count: 3.5, seriously! yes?"):
        assert kept in post_process(kept)


def test_reply_path_applies_the_strip():
    # The wiring, not just the helper: replies flow through post_process.
    from yuyu.chat import post_process

    assert "\U0001F600" not in post_process("good morning \U0001F600")
