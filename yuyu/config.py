"""Environment + config.json loading, directory setup, and the legacy-folder migration."""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

from dotenv import load_dotenv

from .util import atomic_write

ROOT = Path(__file__).resolve().parent.parent
MEMORY_DIR = ROOT / "memory"
STICKER_DIR = ROOT / "stickers"
SKILLS_DIR = ROOT / "skills"
AFFINITY_DIR = ROOT / "affinity"
PRIVATE_DIR = ROOT / "private"
BACKUP_DIR = ROOT / "backups"
# Derived data we keep rather than rebuild, plus scratch that is never real data.
# Half-written files stage in TMP_DIR so nothing torn is ever left beside a
# config, a memory file or the quiet list.
CACHE_DIR = ROOT / "cache"
TMP_DIR = ROOT / "tmp"
# One append-only file per day of model calls, read back by the dashboard.
USAGE_DIR = ROOT / "usage"

load_dotenv(ROOT / ".env")

DEFAULTS: dict = {
    "bot": {
        "name": "Yuyu",
        "prefix": "!",
        "personaFile": "persona.md",
        # Rich presence: what Discord shows on her profile. `type` picks the
        # verb, `name` is the headline, details/state are the two lines under it.
        # Image fields are asset keys uploaded in the Developer Portal, not URLs.
        #
        # Placeholders: {prefix}, {name}, {people} counted off disk, and
        # {servers} / {activity} supplied by the running bot - so the text
        # follows what she is doing instead of standing still.
        "presence": {
            "status": "online",
            "type": "watching",
            "name": "the server",
            "details": "{activity}",
            "state": "{servers} · {prefix}help for commands",
            "largeImage": "",
            "largeText": "Yuyu",
            "smallImage": "",
            "smallText": "",
        },
    },
    "model": {
        "default": "gemini",
        "providers": [
            {
                "id": "gemini",
                "name": "Gemini 3.1 Flash-Lite",
                "baseUrl": "https://generativelanguage.googleapis.com/v1beta/openai",
                "model": "gemini-3.1-flash-lite",
                "apiKeyEnv": "GEMINI_API_KEY",
                "apiKeyEnvPrefix": "GEMINI_API_KEY_",
                "enabled": True,
            },
            {
                "id": "gemini-3.5-flash-lite",
                "name": "Gemini 3.5 Flash-Lite",
                "baseUrl": "https://generativelanguage.googleapis.com/v1beta/openai",
                "model": "gemini-3.5-flash-lite",
                "apiKeyEnv": "GEMINI_API_KEY",
                "enabled": True,
            },
            {
                "id": "gemini-flash-lite-latest",
                "name": "Gemini Flash-Lite (latest)",
                "baseUrl": "https://generativelanguage.googleapis.com/v1beta/openai",
                "model": "gemini-flash-lite-latest",
                "apiKeyEnv": "GEMINI_API_KEY",
                "enabled": True,
            },
            {
                "id": "gemini-3-flash-preview",
                "name": "Gemini 3 Flash Preview",
                "baseUrl": "https://generativelanguage.googleapis.com/v1beta/openai",
                "model": "gemini-3-flash-preview",
                "apiKeyEnv": "GEMINI_API_KEY",
                "enabled": True,
            },
            {
                "id": "gemini-3.5-flash",
                "name": "Gemini 3.5 Flash",
                "baseUrl": "https://generativelanguage.googleapis.com/v1beta/openai",
                "model": "gemini-3.5-flash",
                "apiKeyEnv": "GEMINI_API_KEY",
                "enabled": True,
            },
            {
                "id": "gemini-3.6-flash",
                "name": "Gemini 3.6 Flash",
                "baseUrl": "https://generativelanguage.googleapis.com/v1beta/openai",
                "model": "gemini-3.6-flash",
                "apiKeyEnv": "GEMINI_API_KEY",
                "enabled": True,
            },
            {
                "id": "gemini-3.7-flash",
                "name": "Gemini 3.7 Flash",
                "baseUrl": "https://generativelanguage.googleapis.com/v1beta/openai",
                "model": "gemini-3.7-flash",
                "apiKeyEnv": "GEMINI_API_KEY",
                "enabled": True,
            },
            {
                "id": "gemini-3.8-flash",
                "name": "Gemini 3.8 Flash",
                "baseUrl": "https://generativelanguage.googleapis.com/v1beta/openai",
                "model": "gemini-3.8-flash",
                "apiKeyEnv": "GEMINI_API_KEY",
                "enabled": True,
            },
            {
                "id": "gemini-3.1-flash-lite-preview",
                "name": "Gemini 3.1 Flash-Lite Preview",
                "baseUrl": "https://generativelanguage.googleapis.com/v1beta/openai",
                "model": "gemini-3.1-flash-lite-preview",
                "apiKeyEnv": "GEMINI_API_KEY",
                "enabled": True,
            },
            {
                "id": "gemini-gemma-4-31b-it",
                "name": "Gemma 4 31B (Gemini)",
                "baseUrl": "https://generativelanguage.googleapis.com/v1beta/openai",
                "model": "gemma-4-31b-it",
                "apiKeyEnv": "GEMINI_API_KEY",
                "enabled": True,
            },
            {
                "id": "gemini-gemma-4-26b-a4b-it",
                "name": "Gemma 4 26B (Gemini)",
                "baseUrl": "https://generativelanguage.googleapis.com/v1beta/openai",
                "model": "gemma-4-26b-a4b-it",
                "apiKeyEnv": "GEMINI_API_KEY",
                "enabled": True,
            },
            {
                "id": "openrouter",
                "name": "OpenRouter Nemotron 3 Ultra",
                "baseUrl": "https://openrouter.ai/api/v1",
                "model": "nvidia/nemotron-3-ultra-550b-a55b:free",
                "apiKeyEnv": "OPENROUTER_API_KEY",
                "apiKeyEnvPrefix": "OPENROUTER_API_KEY_",
                "enabled": True,
            },
            {
                "id": "openrouter-lightning",
                "name": "OpenRouter Nemotron 3.5 Lightning",
                "baseUrl": "https://openrouter.ai/api/v1",
                "model": "nvidia/nemotron-3.5-lightning:free",
                "apiKeyEnv": "OPENROUTER_API_KEY",
                "enabled": True,
            },
            {
                "id": "openrouter-super",
                "name": "OpenRouter Nemotron 3 Super",
                "baseUrl": "https://openrouter.ai/api/v1",
                "model": "nvidia/nemotron-3-super-120b-a12b:free",
                "apiKeyEnv": "OPENROUTER_API_KEY",
                "enabled": True,
            },
            {
                "id": "openrouter-gemma-31b",
                "name": "OpenRouter Gemma 4 31B",
                "baseUrl": "https://openrouter.ai/api/v1",
                "model": "google/gemma-4-31b-it:free",
                "apiKeyEnv": "OPENROUTER_API_KEY",
                "enabled": True,
            },
            {
                "id": "openrouter-gemma-26b",
                "name": "OpenRouter Gemma 4 26B",
                "baseUrl": "https://openrouter.ai/api/v1",
                "model": "google/gemma-4-26b-a4b-it:free",
                "apiKeyEnv": "OPENROUTER_API_KEY",
                "enabled": True,
            },
            {
                "id": "openrouter-laguna",
                "name": "OpenRouter Laguna S",
                "baseUrl": "https://openrouter.ai/api/v1",
                "model": "poolside/laguna-s-2.1:free",
                "apiKeyEnv": "OPENROUTER_API_KEY",
                "enabled": True,
            },
            {
                "id": "openrouter-lfm",
                "name": "OpenRouter LFM 2.5 2.6B",
                "baseUrl": "https://openrouter.ai/api/v1",
                "model": "liquid/lfm-2.5-2.6b:free",
                "apiKeyEnv": "OPENROUTER_API_KEY",
                "enabled": True,
            },
            {
                "id": "groq",
                "name": "Groq",
                "baseUrl": "https://api.groq.com/openai/v1",
                "model": "llama-3.3-70b-versatile",
                "apiKeyEnv": "GROQ_API_KEY",
                "enabled": True,
            },
            {
                "id": "cerebras",
                "name": "Cerebras",
                "baseUrl": "https://api.cerebras.ai/v1",
                "model": "llama-3.3-70b",
                "apiKeyEnv": "CEREBRAS_API_KEY",
                "enabled": True,
            },
            {
                "id": "sambanova",
                "name": "SambaNova",
                "baseUrl": "https://api.sambanova.ai/v1",
                "model": "gemma-4-31B-it",
                "apiKeyEnv": "SAMBANOVA_API_KEY",
                "apiKeyEnvPrefix": "SAMBANOVA_API_KEY_",
                "enabled": True,
            },
        ],
        "maxTokens": 800,
        "temperature": 0.95,
        "requestTimeoutMs": 120_000,
    },
    "memory": {
        "enabled": True,
        "autoExtract": True,
        "extractCooldownMs": 15_000,
        "maxFactsPerExtraction": 5,
        "maxFactsPerFile": 60,
    },
    "context": {
        "maxMessages": 30,
        "promptMessages": 12,
        "summaryIntervalMinutes": 30,
        "summaryKeepRecentMessages": 8,
        "summaryMaxChars": 1200,
        "ttlMinutes": 240,
        "maxPeopleInPrompt": 8,
        "maxSkillFiles": 4,
    },
    "behavior": {
        "typingIndicator": True,
        "humanDelay": True,
        "maxReplyChars": 1900,
        "respondToNameAnywhere": True,
        "respondInGuildsWithoutMention": False,
    },
    "formatting": {"allowCards": True, "cardHint": True},
    "affinity": {
        "enabled": True,
        "eligiblePronouns": ["he/him", "xe/xim"],
        # Master switch for the whole crush feature. Off means nobody is the
        # crush, the panel hides every crush control, and she never acts on it -
        # but warmth, familiarity, romance and the feelings all keep moving.
        "crush": True,
        "crushBehaviours": True,
        # Who she has a crush on is the host's choice: by default only an
        # explicit pick in the panel gives her a crush, and it stays on that
        # person until it is released or reset. Set this true and she elects one
        # herself from whoever leans hardest - the switch margin below only
        # matters then. Romance and the feelings still build on their own either
        # way; this only decides who, if anyone, gets the crush.
        "crushAutoElect": False,
        "romanceThreshold": 40,
        # How far ahead of the holder somebody has to be to take the spot.
        # Without it, two people a point apart swap the crush every time either
        # of them says hello.
        "crushSwitchMargin": 20,
        "romanceFloor": 25,
        "decayWarmthPerDay": 1.5,
        "decayFamiliarityPerDay": 0.35,
        "decayRomancePerDay": 0.7,
        "injectPrompt": True,
        # Being let down lands harder than being treated well, and forgiving
        # someone takes a few turns rather than one.
        "warmthRecoverScale": 0.85,
        "warmthLossScale": 1.25,
        "streakBonus": 0.3,
        "badStreakBonus": 0.45,
        # How she feels in the moment, as opposed to the long numbers above.
        "feelings": True,
        "feelingHalfLifeMinutes": 120,
    },
    # The bond with whoever built her. ownerIds falls back to private.ownerIds.
    "bond": {
        "enabled": True,
        "ownerIds": [],
        # A single message reading this low is enough to make her ask. One bad
        # afternoon is not a crisis, but this is a crisis.
        "lowThreshold": -35,
        # Consecutive messages below the line needed before she asks. Raise it to
        # make her slower to worry - 1 is the whole point of the feature.
        "lowTurnsNeeded": 1,
        # ...and a mood this good for a couple of turns ends her window early.
        "betterThreshold": 10,
        "graceTurns": 2,
        # Hard stop on how long she stays gentle. Short on purpose.
        "caringMinutes": 120,
        "levelGain": 0.9,
        # Never ask twice inside this window, however bad it looks.
        "checkInCooldownMinutes": 90,
    },
    "emoji": {
        # Off: a bare emoji reaction on someone's message reads as a bot tell.
        # Sticker *images* stay on - those are the attachments she uses instead.
        "reactions": False,
        "reactionChance": 0.30,
        "stickers": True,
        "stickerChance": 0.16,
        "autoDiscoverServerEmoji": True,
        "stickersFile": "stickers.json",
    },
    "dashboard": {
        "enabled": True,
        # Loopback only. The panel can rewrite her persona, skills and memory, so
        # it refuses to start on anything a browser on the network could reach.
        "host": "127.0.0.1",
        "port": 7373,
        "openBrowser": True,
    },
    "setup": {
        # Whether the panel has stopped opening the setup wizard by itself.
        # Written on dismiss so it persists per install - not per browser, which
        # would bring the wizard back on a new machine. Ships false, so a fresh
        # clone still offers it once. The wizard is always reachable from the
        # Overview tab regardless.
        "wizardDismissed": False,
    },
    "limits": {
        "maxReplyChars": 1900,
        "maxMessageChars": 2000,
        "maxConcurrentGenerations": 2,
    },
    "usage": {
        # Token counting is on by default: it costs one dict per call and never
        # leaves the machine. Cost is only worked out for the models priced
        # below, because a made-up price is worse than no price.
        "enabled": True,
        "keepDays": 90,
        # Ask streaming responses to include their usage block. Some
        # OpenAI-compatible gateways reject the field, so a provider that does
        # is remembered and retried without it.
        "streamUsage": True,
        # Dollars per million tokens, as {"<provider>:<model>": {"in": ..,
        # "out": ..}}. "cached" is optional and defaults to the input rate. A
        # bare model name or "*" matches anything not listed more specifically.
        "prices": {},
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _read_config() -> dict:
    path = ROOT / "config.json"
    try:
        return _deep_merge(DEFAULTS, json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return dict(DEFAULTS)
    except (json.JSONDecodeError, OSError) as exc:
        print(f"[config] could not read config.json: {exc} - using defaults")
        return dict(DEFAULTS)


config = _read_config()

secrets = {
    "discord_token": os.environ.get("DISCORD_TOKEN", "").strip() or None,
    "discord_client_id": os.environ.get("DISCORD_CLIENT_ID", "").strip() or None,
    "discord_application_id": os.environ.get("DISCORD_APPLICATION_ID", "").strip() or None,
    "discord_public_key": os.environ.get("DISCORD_PUBLIC_KEY", "").strip() or None,
    "discord_client_secret": os.environ.get("DISCORD_CLIENT_SECRET", "").strip() or None,
}

MODEL = config["model"]["default"]
PREFIX = config["bot"]["prefix"]
PERSONA_FILE = ROOT / config["bot"]["personaFile"]
STICKERS_FILE = ROOT / config["emoji"]["stickersFile"]


def bot_name() -> str:
    """Her name, read live.

    Not a module constant: the setup wizard renames her while the bot is already
    running, and every module that did `from .config import BOT_NAME` captured
    the old string at import time - so the panel would have shown the new name
    while she still answered to the old one. Reading it through this function is
    what keeps those two honest.
    """
    return str(config["bot"].get("name") or "Yuyu").strip() or "Yuyu"


def set_bot_name(value: str) -> str:
    """Change her name, live and on disk. Returns what was saved."""
    name = str(value or "").strip()
    if not name:
        raise ValueError("a name is required")
    if len(name) > 32:
        raise ValueError("that name is too long (max 32 characters)")
    # She is addressed by name in every channel she is in, so the name has to
    # survive being typed into a message and a regex.
    if not all(c.isalnum() or c in " _-." for c in name):
        raise ValueError("letters, numbers, spaces, - _ and . only")
    config["bot"]["name"] = name
    _patch_config("bot", "name", name)
    # The default persona is written from the name and is only a starting point,
    # but an untouched one still says the old name in its heading.
    return name


class _Placeholders(dict):
    """An unknown placeholder resolves to nothing.

    Printing `{people}` at whoever is looking at her profile would be worse
    than quietly dropping it, and someone's hand-edited config is allowed to
    have a stray brace in it.
    """

    def __missing__(self, key):
        return ""


def _people_phrase() -> str:
    """`42 people`, or nothing at all before she has met anybody.

    Counted here so `build_presence()` still works with no arguments - the
    dashboard preview and the tests both call it that way.
    """
    try:
        count = sum(1 for _ in MEMORY_DIR.glob("*.md"))
    except OSError:
        return ""
    return f"{count} people" if count else ""


def _tidy(text: str) -> str:
    """Drop the fragments a missing placeholder left behind.

    `{people} · {activity}` with nobody known yet must read as an empty string,
    not as a stranded `·`.
    """
    return " · ".join(part.strip() for part in text.split(" · ") if part.strip())


def build_presence(live: dict | None = None) -> dict:
    """Turn the presence config into what change_presence() expects.

    `{prefix}` and `{name}` are always known, `{people}` is counted off disk,
    and `{servers}` / `{activity}` arrive in `live` because only the bot knows
    them. Placeholders carry their own noun - `42 people`, not `42` - so a
    missing one tidies the line instead of stranding a word on it.
    """
    p = dict(config["bot"].get("presence") or {})
    fmt = _Placeholders(prefix=PREFIX, name=bot_name(), people=_people_phrase())
    for key, value in (live or {}).items():
        fmt[str(key)] = str(value)

    def resolve(raw) -> str:
        text = str(raw or "")
        if "{" not in text:
            return text
        try:
            return _tidy(text.format_map(fmt))
        except (ValueError, IndexError):
            # A stray brace, not a placeholder. Show it back untouched.
            return text

    return {
        "type": str(p.get("type") or "watching").strip().lower(),
        "name": resolve(p.get("name")).strip() or f"{PREFIX}help",
        "details": resolve(p.get("details")),
        "state": resolve(p.get("state")),
        "status": str(p.get("status") or "online").strip().lower(),
        "largeImage": str(p.get("largeImage") or ""),
        "largeText": resolve(p.get("largeText") or p.get("name") or bot_name()),
        "smallImage": str(p.get("smallImage") or ""),
        "smallText": resolve(p.get("smallText") or ""),
    }


def current_model() -> str:
    """The configured primary PROVIDER id - not a model name.

    Named for what the dashboard calls it, not what it returns. The model name
    lives in that provider's own entry, so anything that needs to tell someone
    which model is running must read `model` off the provider rather than
    reporting this string: answering "Gemini" to "what model are you on" is the
    bug that name invites.

    Kept as-is because it is the selector for the fallback chain, where a
    provider id is exactly what is wanted.
    """
    providers = config["model"].get("providers") or []
    default = str(config["model"].get("default") or "")
    if default:
        return default
    return str(providers[0].get("id") or "") if providers else ""


def _patch_config(section: str, key: str, value) -> None:
    """Change ONE key in config.json, in place.

    Re-reads and rewrites the file rather than dumping the in-memory `config`,
    so unknown keys and anything hand-edited survive a save from the panel.
    """
    path = ROOT / "config.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    if not isinstance(raw.get(section), dict):
        raw[section] = {}
    raw[section][key] = value
    atomic_write(path, json.dumps(raw, indent=2, ensure_ascii=False) + "\n")


def set_presence(value: dict) -> dict:
    """Change her rich presence, live and on disk.

    Both halves matter: `_patch_config` only rewrites the file while
    `build_presence()` reads the in-memory dict, so patching alone would need a
    restart. Anything Discord would reject is refused here so the panel can say
    so rather than appear to work.
    """
    status = str(value.get("status") or "online").strip().lower()
    kind = str(value.get("type") or "watching").strip().lower()
    if status not in ("online", "idle", "dnd", "invisible"):
        raise ValueError(f"unknown status: {status}")
    if kind not in ("playing", "watching", "listening", "competing", "custom"):
        raise ValueError(f"unknown activity type: {kind}")

    # Discord truncates around here anyway; refusing to store more is friendlier
    # than saving text she will never show anyone.
    def line(raw, limit=128) -> str:
        text = str(raw or "")
        if len(text) > limit:
            raise ValueError(f"too long ({len(text)} chars, max {limit})")
        return text

    presence = {
        "status": status,
        "type": kind,
        "name": line(value.get("name")),
        "details": line(value.get("details")),
        "state": line(value.get("state")),
        "largeImage": line(value.get("largeImage")),
        "largeText": line(value.get("largeText") or value.get("name") or bot_name()),
        "smallImage": line(value.get("smallImage")),
        "smallText": line(value.get("smallText") or ""),
    }
    config["bot"]["presence"] = presence
    _patch_config("bot", "presence", presence)
    return presence


def set_model(model_id: str) -> str:
    """Switch the chat model for every future reply, now and after a restart."""
    name = str(model_id).strip()
    if not name:
        raise ValueError("empty model name")
    config["model"]["default"] = name
    _patch_config("model", "default", name)
    return name


def set_crush_enabled(enabled: bool) -> bool:
    """Turn the whole crush feature on or off, live and on disk.

    Off does not erase anyone's scores - it just stops there being a crush. The
    panel hides its crush controls and the bot stops carrying the behaviour.
    """
    value = bool(enabled)
    config["affinity"]["crush"] = value
    _patch_config("affinity", "crush", value)
    return value


def set_provider_model(provider_id: str, model_name: str) -> str:
    """Update one provider's model in memory and config.json."""
    providers = [dict(provider) for provider in config["model"].get("providers") or []]
    provider = next((item for item in providers if item.get("id") == provider_id), None)
    if provider is None:
        raise ValueError("unknown provider")
    name = str(model_name).strip()
    if not name:
        raise ValueError("model name cannot be empty")
    provider["model"] = name
    _patch_config("model", "providers", providers)
    config["model"]["providers"] = providers
    return name


# --- providers, as the dashboard sees them ---------------------------------
#
# A provider names the environment variable its key lives in. The variable's
# *name* is configuration and may be shown; its *value* never leaves .env.

PROVIDER_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,39}\Z")
ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
MODEL_NAME_CHARS = set("-_./+:")


def provider_env_names(provider: dict) -> list[str]:
    """Every environment variable this provider reads a key from, in order.

    One key can be named directly (`apiKeyEnv`), and a whole numbered family
    (`apiKeyEnvPrefix` -> `PREFIX_1`, `PREFIX_2`) can sit behind it so a single
    provider holds several keys and rotates between them.
    """
    configured = provider.get("apiKeyEnvs") or provider.get("apiKeyEnv") or []
    if isinstance(configured, str):
        configured = [configured]
    if not isinstance(configured, list):
        configured = []
    names = [str(name).strip() for name in configured if str(name).strip()]
    prefix = str(provider.get("apiKeyEnvPrefix") or "").strip()
    if prefix:
        numbered = [
            name for name in os.environ
            if name.startswith(prefix) and name[len(prefix):].isdigit()
        ]
        names.extend(sorted(numbered, key=lambda name: int(name[len(prefix):])))

    out: list[str] = []
    for name in names:
        if name not in out:
            out.append(name)
    return out


def provider_key_names(provider: dict) -> list[str]:
    """The subset of `provider_env_names` that actually has a value set."""
    return [name for name in provider_env_names(provider)
            if (os.environ.get(name) or "").strip()]


def public_provider(provider: dict) -> dict:
    """What the dashboard may be told about one provider.

    The key *values* are never here - only the variable names and whether each
    one currently holds something, which is all the editor needs to draw.
    """
    names = provider_key_names(provider)
    return {
        "id": str(provider.get("id") or ""),
        "name": str(provider.get("name") or provider.get("id") or ""),
        "baseUrl": str(provider.get("baseUrl") or ""),
        "model": str(provider.get("model") or ""),
        "apiKeyEnv": str(provider.get("apiKeyEnv") or ""),
        "apiKeyEnvPrefix": str(provider.get("apiKeyEnvPrefix") or ""),
        "enabled": bool(provider.get("enabled", True)),
        "hasKey": bool(names),
        "keyCount": len(names),
        "keyNames": names,
    }


def upsert_provider(payload: dict) -> dict:
    """Add or update one provider entry, in memory and in config.json.

    Only the fields the panel draws are touched; anything hand-set on the entry
    (a comment, a custom header, a second key list) survives an edit. The key
    value is deliberately absent from `payload` - it goes to .env instead.
    """
    provider_id = str(payload.get("id") or "").strip()
    if not PROVIDER_ID_RE.match(provider_id):
        raise ValueError("provider id must be letters, numbers, - or _ (max 40)")

    providers = [dict(item) for item in config["model"].get("providers") or []]
    index = next((i for i, item in enumerate(providers)
                  if str(item.get("id") or "") == provider_id), None)
    existing = providers[index] if index is not None else {}

    def text(key, limit, label, required=False) -> str:
        raw = payload.get(key, existing.get(key, ""))
        value = str(raw or "").strip()
        if required and not value:
            raise ValueError(f"a {label} is required")
        if len(value) > limit:
            raise ValueError(f"{label} is too long (max {limit})")
        return value

    name = text("name", 60, "name") or provider_id
    base_url = text("baseUrl", 300, "base URL", required=True)
    if not re.match(r"^https?://", base_url):
        raise ValueError("base URL must start with http:// or https://")
    model = text("model", 160, "model", required=True)
    if not all(c.isalnum() or c in MODEL_NAME_CHARS for c in model):
        raise ValueError("model names may only contain letters, numbers, - _ . / + :")

    api_key_env = text("apiKeyEnv", 80, "API key variable")
    if api_key_env and not ENV_NAME_RE.match(api_key_env):
        raise ValueError("the API key variable must be a plain ENV_NAME")
    api_key_prefix = text("apiKeyEnvPrefix", 80, "API key prefix")
    if api_key_prefix and not ENV_NAME_RE.match(api_key_prefix):
        raise ValueError("the API key prefix must be a plain ENV_NAME")

    entry = dict(existing)
    entry.update({
        "id": provider_id,
        "name": name,
        "baseUrl": base_url,
        "model": model,
        "enabled": bool(payload.get("enabled", existing.get("enabled", True))),
    })
    if api_key_env:
        entry["apiKeyEnv"] = api_key_env
    else:
        entry.pop("apiKeyEnv", None)
    if api_key_prefix:
        entry["apiKeyEnvPrefix"] = api_key_prefix
    else:
        entry.pop("apiKeyEnvPrefix", None)

    if index is None:
        providers.append(entry)
    else:
        providers[index] = entry
    config["model"]["providers"] = providers
    _patch_config("model", "providers", providers)
    if not str(config["model"].get("default") or "").strip():
        set_model(provider_id)
    return entry


def remove_provider(provider_id: str) -> bool:
    """Drop one provider. Refuses to remove the last one.

    Removing the primary moves the primary to the first provider still enabled,
    because a `default` pointing at a provider that is gone would leave her
    with nothing to talk to.
    """
    provider_id = str(provider_id or "").strip()
    providers = [dict(item) for item in config["model"].get("providers") or []]
    remaining = [item for item in providers if str(item.get("id") or "") != provider_id]
    if len(remaining) == len(providers):
        return False
    if not remaining:
        raise ValueError("that is the only provider - add another before removing it")
    config["model"]["providers"] = remaining
    _patch_config("model", "providers", remaining)
    if current_model() == provider_id:
        pick = next((item for item in remaining if item.get("enabled", True)), remaining[0])
        set_model(str(pick.get("id") or ""))
    return True


def set_env_secret(name: str, value: str) -> str:
    """Write an API key into .env and into this process, with no restart.

    The key lives in .env rather than config.json so a config export never
    carries the secret with it. Existing lines and comments are kept; only the
    one variable is replaced.
    """
    name = str(name or "").strip()
    if not ENV_NAME_RE.match(name):
        raise ValueError("the API key variable must be a plain ENV_NAME")
    value = str(value or "")
    if not value.strip():
        raise ValueError("the API key is empty")
    if "\n" in value or "\r" in value:
        raise ValueError("an API key cannot contain a newline")

    env_path = ROOT / ".env"
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []

    pattern = re.compile(rf"^\s*(?:export\s+)?{re.escape(name)}\s*=")
    out: list[str] = []
    replaced = False
    for line in lines:
        if pattern.match(line):
            if not replaced:
                out.append(f"{name}={value}")
                replaced = True
            continue
        out.append(line)
    if not replaced:
        if out and out[-1].strip():
            out.append("")
        out.append(f"{name}={value}")

    atomic_write(env_path, "\n".join(out).rstrip("\n") + "\n")
    os.environ[name] = value
    return name


def validate_bot_token(token: str | None) -> tuple[bool, str]:
    """A bot token is `<userId>.<timestamp>.<hmac>` - always two dots.

    The OAuth2 client secret has none, which is the usual mix-up.
    """
    if not token:
        return False, (
            "DISCORD_TOKEN is empty. Developer Portal -> your app -> Bot -> Reset Token, "
            "then copy it into .env."
        )
    parts = token.split(".")
    if len(parts) != 3:
        if len(token) < 50:
            return False, (
                f"DISCORD_TOKEN looks like an OAuth2 client secret ({len(token)} chars, no dots). "
                'That cannot log a bot in. Use the Bot page\'s "Reset Token" instead.'
            )
        return False, (
            f"DISCORD_TOKEN has {len(parts)} dot-separated parts, expected 3. "
            "Copy the whole token with no extra spaces."
        )
    return True, ""


def ensure_dirs() -> None:
    for directory in (MEMORY_DIR, STICKER_DIR, SKILLS_DIR, AFFINITY_DIR, PRIVATE_DIR, BACKUP_DIR,
                      CACHE_DIR, TMP_DIR):
        directory.mkdir(parents=True, exist_ok=True)
    for directory in (MEMORY_DIR, STICKER_DIR, AFFINITY_DIR):
        keep = directory / ".gitkeep"
        if not keep.exists():
            keep.write_text("", encoding="utf-8")


def migrate_legacy_dirs() -> tuple[list[str], list[str]]:
    """Move anything left in the old people/ folder into memory/.

    Idempotent, and it never overwrites: if both exist for the same slug the
    memory/ copy wins and the old one is kept aside, so nothing is ever lost.
    """
    legacy = ROOT / "people"
    if not legacy.exists():
        return [], []

    moved: list[str] = []
    kept: list[str] = []
    for path in sorted(legacy.glob("*.md")):
        target = MEMORY_DIR / path.name
        if target.exists():
            aside = MEMORY_DIR / f"{path.stem}.from-people.md"
            if not aside.exists():
                shutil.copy2(path, aside)
            kept.append(f"people/{path.name} -> memory/{aside.name}")
        else:
            shutil.move(str(path), str(target))
            moved.append(f"people/{path.name} -> memory/{path.name}")

    if not any(legacy.glob("*.md")):
        shutil.rmtree(legacy, ignore_errors=True)

    if moved:
        print(f"[migrate] memory files moved: {', '.join(moved)}")
    if kept:
        print(f"[migrate] kept both copies: {', '.join(kept)}")
    return moved, kept
