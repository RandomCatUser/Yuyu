"""Entry point: starts the Discord bot and the local dashboard."""

from __future__ import annotations

import asyncio
import os
import re
import sys
import threading

import discord

from yuyu import guilds, identity, logger, mute, presence
from yuyu.chat import is_busy, log_message, reply, reply_target, slug_for
from yuyu.commands import router
from yuyu.commands.memory_editor import AddFactModal, RemoveFactModal, is_modal
from yuyu.config import (
    PREFIX,
    ROOT,
    bot_name,
    config,
    ensure_dirs,
    migrate_legacy_dirs,
    secrets,
    validate_bot_token,
)
from yuyu.persona import create_default_persona
from yuyu.providers import ProviderError, configured_provider_names
from yuyu.stickers import index_guild_emoji

log = logger.install()
STARTED_AS = __import__("time").time()

_LOCK_FD: int | None = None
_NAME_PATTERNS: tuple[str, tuple] = ("", ())


def name_patterns() -> tuple:
    """The two name regexes, rebuilt only when her name actually changes.

    `detect_trigger` runs on every message in every channel, so compiling here
    per call would be waste; keyed on the name it is one compile per rename.
    """
    global _NAME_PATTERNS
    name = bot_name()
    if _NAME_PATTERNS[0] != name:
        _NAME_PATTERNS = (
            name,
            (
                re.compile(rf"\b{re.escape(name)}\b", re.IGNORECASE),
                re.compile(rf"^\s*{re.escape(name)}\b[,:]?\s*", re.IGNORECASE),
            ),
        )
    return _NAME_PATTERNS[1]


def acquire_instance_lock() -> bool:
    """Refuse to start a second bot next to a running one.

    Two live instances each hold their own gateway connection, so both see the
    same MESSAGE_CREATE and both answer it - and because each generation is
    separate, the two replies read as different text rather than a literal copy.
    That is what looked like a "repeat error" in her chat. The lock is held on
    an open descriptor, so the OS releases it the moment this process dies,
    including on a crash: no stale lock to clear by hand.

    Returns True when it is safe to run.
    """
    global _LOCK_FD
    try:
        fd = os.open(ROOT / ".yuyu.lock", os.O_RDWR | os.O_CREAT, 0o644)
    except OSError:
        # If the lock cannot be taken at all, still let the bot boot - a
        # permissions problem should not make her mute.
        return True
    try:
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        os.lseek(fd, 0, os.SEEK_SET)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return False
    _LOCK_FD = fd
    return True


def build_bot() -> discord.Client:
    intents = discord.Intents.default()
    intents.message_content = True  # privileged - enable in the Developer Portal
    intents.members = False

    client = discord.Client(intents=intents)
    # Trigger patterns belong to the bot: mentioning her by name at the start
    # of a line counts, elsewhere only if configured.
    #
    # Built through name_patterns() rather than once here, because the setup
    # wizard can rename her while this client is already connected - a regex
    # frozen at startup would keep matching the old name and she would go deaf
    # to the new one. The cache is keyed on the name, so this stays one compile
    # per rename rather than one per message.
    def detect_trigger(message) -> str | None:
        name_re, name_start_re = name_patterns()
        if message.guild is None:
            return "dm"
        ref = reply_target(message)
        if ref is not None and getattr(ref.author, "id", None) == client.user.id:
            return "reply"
        if client.user in message.mentions:
            return "mention"
        if name_start_re.search(message.content or ""):
            return "name-start"
        if config["behavior"]["respondToNameAnywhere"] and name_re.search(message.content or ""):
            return "mention"
        if config["behavior"]["respondInGuildsWithoutMention"] and (message.content or "").rstrip().endswith("?"):
            # An ambient question is not addressed to anyone in particular.
            return "ambient"
        return None

    @client.event
    async def on_ready():
        me = client.user
        log(f'[ready] {me} online as "{bot_name()}"')
        # Attach both first: the server list, the presence push and the
        # dashboard all need somewhere to hand their work, and the panel is
        # about to start serving a snapshot built from them.
        loop = asyncio.get_running_loop()
        guilds.attach(loop, client)
        guilds.update(client)
        presence.attach(loop, client)
        # Same loop, same reason: the setup wizard pushes her avatar and username
        # through here, and `client.user.edit()` is not safe from the panel's
        # Flask thread.
        identity.attach(loop, client)
        await presence.refresh(client, force=True)
        if config["emoji"]["autoDiscoverServerEmoji"]:
            total = index_guild_emoji(client.guilds)
            log(f"[ready] indexed {total} custom emoji across {len(client.guilds)} server(s)")
        log(f"[ready] prefix: {PREFIX} | model: {config['model']['default']}")
        available_providers = configured_provider_names()
        if available_providers:
            log(f"[ready] model providers: {', '.join(available_providers)}")
        else:
            log("[warning] no model API keys configured - add one or more to .env")
        log(f"[ready] memory: {'on' if config['memory']['enabled'] else 'off'} | "
            f"auto-extract: {'on' if config['memory']['autoExtract'] else 'off'}")
        log(f"[ready] cards: {'on' if config['formatting']['allowCards'] else 'off'} | "
            f"reactions: {'on' if config['emoji']['reactions'] else 'off'}")
        log(f"[ready] affect: {'on' if config['affinity']['enabled'] else 'off'} | "
            f"eligible: {', '.join(config['affinity']['eligiblePronouns'])}")
        if mute.muted_slugs():
            log(f"[ready] quiet for: {', '.join(sorted(mute.muted_slugs()))}")
        log(f"[ready] in {len(client.guilds)} server(s)")
        _start_dashboard_thread()

    @client.event
    async def on_guild_create(guild):
        guilds.update(client)
        # The server count is a placeholder in her presence, so a new server is
        # a reason to re-send it rather than wait for somebody to talk.
        await presence.refresh(client, force=True)
        log(f"[guild] joined {guild.name} - now in {len(client.guilds)} server(s)")
        if config["emoji"]["autoDiscoverServerEmoji"]:
            index_guild_emoji(client.guilds)
            log(f"[emoji] indexed emoji for new server: {guild.name}")

    @client.event
    async def on_guild_remove(guild):
        # Fires for both "someone kicked her" and the dashboard's own Leave
        # button, so the list stays honest either way.
        guilds.update(client)
        await presence.refresh(client, force=True)
        log(f"[guild] left {guild.name} - now in {len(client.guilds)} server(s)")

    @client.event
    async def on_message(message):
        if message.author.id == client.user.id or message.author.bot:
            return
        if not (message.content or "").strip():
            return

        try:
            await _handle_message(
                client, message, detect_trigger, router,
                log_message, reply, is_busy, config,
            )
        except Exception:
            # Never let one bad message kill the handler silently.
            import traceback

            log(f"[message] failed for {message.author} in {message.channel}: {traceback.format_exc()}")


    @client.event
    async def on_interaction(interaction):
        section = is_modal(interaction.custom_id or "")
        if section is None:
            return
        try:
            if section == "remove":
                await RemoveFactModal().await_interaction(interaction)
            else:
                await AddFactModal(section).await_interaction(interaction)
        except Exception:
            import traceback

            log(f"[interaction] modal failed: {traceback.format_exc()}")

    @client.event
    async def on_error(event, *args, **kwargs):
        import traceback

        # discord.py passes the event *name* here and the real exception as the
        # last positional argument. Without this the handler logs "error in str"
        # and hides the actual cause.
        name = event if isinstance(event, str) else type(event).__name__
        exc = args[-1] if args else None
        detail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)) if exc else "no exception passed"
        log(f"[discord] {name} failed:\n{detail}")

    return client


async def _handle_message(client, message, detect_trigger, router, log_message, reply,
                          is_busy, config):
    # Recorded either way: the line still enters the conversation buffer and
    # they still get a memory file. Being quiet is not the same as forgetting.
    log_message(message)

    # The per-person switch. No answers, no command replies, no reactions -
    # she simply has nothing to say to them right now.
    if mute.is_muted(slug_for(message.author)):
        return

    if await router.run(message):
        return

    trigger = detect_trigger(message)
    if not trigger:
        return

    if is_busy():
        await message.reply("one sec, talking to someone else")
        return

    if config["behavior"]["humanDelay"]:
        await asyncio.sleep(0.4 + random_delay())

    if trigger == "name-start":
        # Re-read here rather than passing the pattern down: the name can change
        # between this message and the next, and stripping with a stale pattern
        # would leave the old name sitting in the reply.
        text = name_patterns()[1].sub("", message.content or "").strip()
    else:
        text = None

    try:
        await reply(message, trigger=trigger, text=text)
    except ProviderError as exc:
        log(f"[chat] provider failover exhausted for {message.author}: {exc}")
        public = exc.public
        try:
            await message.reply(public)
        except Exception:
            pass
    except Exception as exc:
        import traceback

        # Full detail goes to the log. The channel only ever gets `public`,
        # which is written in her voice and names no config keys or files -
        # a raw "recent() missing 1 required positional argument" reads like a
        # crash dump and tells the server how she is built.
        log(f"[chat] failed for {message.author}: {traceback.format_exc()}")
        public = getattr(exc, "public", None) or "lost the plot for a sec, say that again?"
        try:
            await message.reply(public)
        except Exception:
            pass
    else:
        # She actually answered, so her profile may say she is busy. Its own
        # try: a presence hiccup must never turn into a message in the channel.
        # The push is throttled and skipped when nothing changed.
        presence.note_activity()
        try:
            await presence.refresh(client)
        except Exception as exc:
            log(f"[presence] {exc}")


def random_delay() -> float:
    import random

    return random.random() * 0.7


_DASHBOARD_STARTED = False


def _start_dashboard_thread() -> None:
    global _DASHBOARD_STARTED

    from yuyu.dashboard.app import start_dashboard

    if not config["dashboard"]["enabled"]:
        return
    # Both accounts fire on_ready, and only one may own the port.
    if _DASHBOARD_STARTED:
        return
    _DASHBOARD_STARTED = True

    def run() -> None:
        try:
            start_dashboard()
        except Exception as exc:
            log(f"[dashboard] {exc}")

    threading.Thread(target=run, daemon=True, name="dashboard").start()


async def _run_forever(client: discord.Client, token: str) -> None:
    """Log in and stay logged in, retrying the failures that are worth retrying.

    A connection dropped *during login* - aiohttp timing out waiting for the
    first byte - used to kill the process: one bad moment on the wire and the
    bot was simply gone with no one left to notice. Transient failures now back
    off and come back. `discord.LoginFailure` is the exception: a token Discord
    actively rejects will never start working, so that one stops her for real
    and says why.
    """
    delay = 3.0
    attempt = 0
    while True:
        try:
            await client.start(token)
            # start() only returns once the connection is gone for good.
            log("[bot] disconnected")
        except discord.LoginFailure:
            raise
        except Exception as exc:
            attempt += 1
            log(f"[bot] connection failed ({type(exc).__name__}: {exc}); retry {attempt} in {int(delay)}s")

        await asyncio.sleep(delay)
        delay = min(delay * 2, 60.0)


def run_setup_wizard() -> int:
    """Open the panel on its own, for before the bot can connect.

    The normal panel only starts from `on_ready`, which needs a working token -
    so a first run with an empty .env had no way to reach the wizard that would
    fix it. This path starts the dashboard with nothing else running, takes no
    instance lock (it is not a second bot, and locking would refuse it while a
    real one runs), and says plainly that Discord is not connected so a refused
    avatar push does not look broken.
    """
    print("[setup] opening the panel on its own - she is NOT connected to Discord.")
    print("        Her avatar and username cannot be pushed until she has a token,")
    print("        but everything you enter here is saved and used on next start.\n")
    config["dashboard"]["enabled"] = True
    config["dashboard"]["openBrowser"] = True
    from yuyu.dashboard.app import start_dashboard

    start_dashboard()
    return 0


def main() -> int:
    ensure_dirs()
    create_default_persona()
    migrate_legacy_dirs()  # sync: it only touches the filesystem

    # Checked before the lock: the wizard is a panel, not a bot, so it must not
    # be turned away by a running instance - and it must not be able to start one.
    if "--setup" in sys.argv[1:] or "setup" in sys.argv[1:]:
        return run_setup_wizard()

    if not acquire_instance_lock():
        print("\n[boot] Another copy of this bot is already running - not starting a second one.")
        print("       A second copy holds its own Discord connection and answers the")
        print("       same message, so she ends up replying twice to one line.")
        print("       Stop the other instance first (it is recorded in .yuyu.lock).\n")
        return 1

    ok, hint = validate_bot_token(secrets["discord_token"])
    if not ok:
        print("\n[boot] Cannot start - DISCORD_TOKEN is not a usable bot token.")
        print(f"       {hint}")
        print("       Note: the OAuth2 *client secret* cannot log a bot in.\n")
        return 1

    print(f"[boot] logging in as application {secrets['discord_application_id'] or '?'}...")

    try:
        asyncio.run(_run_forever(build_bot(), secrets["discord_token"]))
    except discord.LoginFailure as exc:
        print(f"\n[boot] Discord login failed: {exc}")
        print("       If it says disallowed intents, enable MESSAGE CONTENT INTENT in the\n"
              "       Developer Portal under Bot -> Privileged Gateway Intents.\n")
        return 1
    except KeyboardInterrupt:
        print("\n[shutdown] interrupted, bye")
    return 0


if __name__ == "__main__":
    sys.exit(main())
