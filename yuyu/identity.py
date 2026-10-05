"""Her picture and her display name.

Both are pushed through the bot loop for the same reason as presence - the panel
thread does not own the Discord connection. The image file is the whole truth:
no "portrait set" flag to drift, if the file is gone she has no picture."""

from __future__ import annotations

import asyncio
import base64
import binascii
import threading
from pathlib import Path

from .config import ROOT, bot_name, config

TEMPLATE_DIR = Path(__file__).parent / "dashboard" / "templates"

# Discord accepts these four and nothing else. Kept as a map so the extension we
# save under is the one we verified, rather than whatever the browser claimed.
ALLOWED = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}
MAX_BYTES = 8 * 1024 * 1024  # Discord's own ceiling for an avatar
MAX_EDGE = 1024  # what it will actually render at

_lock = threading.Lock()
_loop: asyncio.AbstractEventLoop | None = None
_client = None


def attach(loop, client) -> None:
    """Remember the loop and client, so a dashboard request can push an edit."""
    global _loop, _client
    with _lock:
        _loop, _client = loop, client


def detach() -> None:
    """Forget them. Used on shutdown and between tests."""
    global _loop, _client
    with _lock:
        _loop, _client = None, None


def portrait_path() -> Path:
    """Where her picture is written. Extension chosen by what she uploaded."""
    saved = config.get("bot", {}).get("portraitFile") or "portrait.png"
    return TEMPLATE_DIR / Path(saved).name


def find_portrait() -> Path | None:
    """Her picture if there is one, else None.

    Checks the configured name first, then any allowed extension beside it - so
    uploading a PNG then a JPEG leaves exactly one file, not a pile of orphans
    for the panel to choose between.
    """
    target = portrait_path()
    if target.exists():
        return target
    stem = target.with_suffix("")
    for ext in ALLOWED.values():
        for candidate in (Path(str(stem) + ext),):
            if candidate.exists():
                return candidate
    return None


def _sniff(blob: bytes) -> str | None:
    """The MIME type from the file's own bytes, or None if it isn't an image.

    Checked against magic bytes rather than trusting the extension or the
    Content-Type the browser offered, because this value ends up being uploaded
    to Discord as an avatar.
    """
    if blob.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if blob.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        return "image/webp"
    if blob[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return None


def save_portrait(data_url: str) -> tuple[Path | None, str]:
    """Decode and store her picture. Returns (path, why-not).

    Every other portrait in the app is a path the browser draws; this is the one
    place bytes arrive from outside, so the size cap, the type check and the
    "not actually an image" case all have to be refused here rather than left to
    a 400 from Discord later.
    """
    raw = str(data_url or "").strip()
    if raw.startswith("data:"):
        raw = raw.split(",", 1)[1] if "," in raw else ""
    if not raw:
        return None, "no image data"

    try:
        blob = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        return None, "that is not valid image data"
    if not blob:
        return None, "no image data"
    if len(blob) > MAX_BYTES:
        return None, f"image is too large ({len(blob) // 1024 // 1024} MB, max 8)"

    mime = _sniff(blob)
    if mime is None:
        return None, "that file is not a png, jpg, webp or gif"

    target = portrait_path().with_suffix(ALLOWED[mime])
    target.write_bytes(blob)

    # Drop whatever she had before, whatever it was called. A stale image left
    # beside the new one is how the panel ends up showing the wrong face.
    for ext in set(ALLOWED.values()) | {portrait_path().suffix}:
        other = target.with_suffix(ext)
        if other != target and other.exists():
            try:
                other.unlink()
            except OSError:
                pass

    # Remember the name so the panel can find it next boot without guessing.
    config.setdefault("bot", {})["portraitFile"] = target.name
    _patch_bot("portraitFile", target.name)
    return target, "saved"


def _patch_bot(key: str, value) -> None:
    """Write one `bot.*` key into config.json, leaving the rest alone."""
    from .config import _patch_config

    _patch_config("bot", key, value)


def has_portrait() -> bool:
    return find_portrait() is not None


# --- the Discord side --------------------------------------------------------
#
# Both pushes below return (ok, why) rather than raising: the panel must be able
# to say "saved on disk, but Discord refused" instead of a 500 with a traceback.


def push_avatar() -> tuple[bool, str]:
    """Make the saved picture her Discord avatar, right now.

    Scheduled onto the bot's loop for the same reason the presence push is.
    Returns "saved but not pushed" rather than failing when the bot is not
    connected - that is the normal state during first-run setup.
    """
    with _lock:
        loop, client = _loop, _client
    if loop is None or client is None:
        return False, "she is not connected to Discord yet, so the picture is saved but not pushed"
    if client.user is None:
        return False, "she is not connected to Discord yet, so the picture is saved but not pushed"

    path = find_portrait()
    if path is None:
        return False, "there is no picture to push"

    try:
        blob = path.read_bytes()
    except OSError as exc:
        return False, f"could not read the picture: {exc}"

    async def _set() -> None:
        await client.user.edit(avatar=blob)

    try:
        asyncio.run_coroutine_threadsafe(_set(), loop).result(timeout=20)
    except Exception as exc:
        return False, f"Discord refused the picture: {type(exc).__name__}: {exc}"
    return True, "her Discord avatar is updated"


def push_display_name() -> tuple[bool, str]:
    """Set her Discord username to match the name she was given.

    Deliberately separate from the in-repo name and reported separately: this is
    an application-level change. Every server this bot token is in sees the new
    username at once, and it is a privileged write - so a refusal is reported,
    never worked around.
    """
    with _lock:
        loop, client = _loop, _client
    if loop is None or client is None or client.user is None:
        return False, "she is not connected to Discord yet, so the name is saved locally only"
    wanted = bot_name()

    async def _set() -> None:
        await client.user.edit(username=wanted)

    try:
        asyncio.run_coroutine_threadsafe(_set(), loop).result(timeout=20)
    except Exception as exc:
        return False, f"Discord refused the name: {type(exc).__name__}: {exc}"
    return True, f"her Discord username is now {wanted}"


def connected() -> bool:
    """Whether the bot is on its loop and logged in right now."""
    with _lock:
        loop, client = _loop, _client
    return loop is not None and client is not None and client.user is not None


def discord_username() -> str:
    """Her Discord username as it currently stands, or '' when not connected."""
    with _lock:
        client = _client
    if client is None:
        return ""
    user = getattr(client, "user", None)
    return str(getattr(user, "name", "") or "") if user is not None else ""


def setup_state() -> dict:
    """What the wizard needs to decide whether it is the first run."""
    from .config import provider_key_names

    providers = [p for p in (config["model"].get("providers") or [])
                 if isinstance(p, dict) and p.get("id")]
    # An entry with no key behind it is a shape, not a working model: the file
    # ships twenty free model ids and none of them has a key until someone adds
    # one, so counting entries would report "configured" on a fresh clone.
    keyed = [p for p in providers if provider_key_names(p)]
    return {
        "name": bot_name(),
        "hasPortrait": has_portrait(),
        "providers": len(providers),
        "hasWorkingModel": bool(keyed),
        # The first entry with a key is the one she would actually talk on.
        "workingModel": str(keyed[0].get("model") or keyed[0].get("id")) if keyed else "",
    }