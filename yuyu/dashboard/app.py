
from __future__ import annotations

import json
import os
import re
import time
import webbrowser
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_from_directory

from .. import bond as bond_mod
from .. import feelings as feelings_mod
from .. import guilds as guild_mod
from .. import identity as identity_mod
from .. import memory as memory_mod
from .. import presence as presence_mod
from .. import reset as reset_mod
from .. import stickers as sticker_mod
from ..affinity import adjust as adjust_affinity
from ..affinity import crush_enabled as crush_enabled_affinity
from ..affinity import crush_summary as crush_summary_affinity
from ..affinity import designate as designate_affinity
from ..affinity import elect_crush as elect_crush_affinity
from ..affinity import invalidate as invalidate_affinity
from ..affinity import load as load_affinity_one
from ..affinity import load_all as load_affinity
from ..affinity import owner_bond as owner_bond_affinity
from ..affinity import ranked as ranked_affinity
from ..affinity import release_crush as release_crush_affinity
from ..affinity import save as save_affinity
from ..affinity import set_pronouns
from ..affinity import summarise as summarise_affinity
from ..affinity import sync_crush_marks as sync_crush_marks_affinity
from ..config import (
    AFFINITY_DIR,
    BACKUP_DIR,
    MEMORY_DIR,
    ROOT,
    SKILLS_DIR,
    STICKER_DIR,
    _patch_config,
    bot_name,
    config,
    current_model,
    public_provider,
    remove_provider,
    secrets,
    set_bot_name,
    set_crush_enabled,
    set_env_secret,
    set_model,
    set_presence,
    set_provider_model,
    upsert_provider,
    validate_bot_token,
)
from ..logger import recent_logs
from ..mute import muted_slugs, set_muted
from ..providers import ProviderError, fetch_models, models_snapshot
from ..skills import load_skills
from .. import usage as usage_mod

MAX_BODY = 512 * 1024
# Days of history the panel asks for. A query parameter, so one snapshot
# can serve a 7-day and a 90-day view.
USAGE_WINDOWS = (1, 7, 14, 30, 90, 365)


def _usage_window() -> int:
    try:
        days = int(request.args.get("days", 30))
    except (TypeError, ValueError):
        return 30
    return min(USAGE_WINDOWS, key=lambda allowed: (abs(allowed - days), allowed))


def _clean_slug(raw) -> str | None:
    slug = str(raw or "").strip().lower()
    if not slug or len(slug) > 80:
        return None
    if not all(c.isalnum() or c in "-_" for c in slug):
        return None
    return slug

LOOPBACK = {"127.0.0.1", "::1", "localhost"}
TEMPLATE_DIR = Path(__file__).parent / "templates"
FONT_DIR = TEMPLATE_DIR / "fonts"
# One file per provider, named after the provider id: templates/logos/groq.svg,
# cerebras.png. Vendors' own artwork, self-hosted so no provider needs the network.
LOGO_DIR = TEMPLATE_DIR / "logos"

NAMED_FILES = {
    "persona": lambda: ROOT / config["bot"]["personaFile"],
    "config": lambda: ROOT / "config.json",
    "stickers": lambda: ROOT / config["emoji"]["stickersFile"],
}
BUILTIN_SKILLS = ["social", "nightowl", "modelwork", "technical", "support"]


def _allowed_targets() -> list[dict]:
    targets = [
        {"key": key, "label": path.name}
        for key, path in (("persona", NAMED_FILES["persona"]()), ("config", NAMED_FILES["config"]()),
                          ("stickers", NAMED_FILES["stickers"]()))
    ]
    targets += [{"key": f"skill:{n}", "label": f"skills/{n}.md"} for n in BUILTIN_SKILLS]
    return targets


def _models_payload() -> list[dict]:
    return {
        "name": bot_name(),
        "hasPortrait": identity_mod.has_portrait(),
        "hasWorkingModel": identity_mod.setup_state()["hasWorkingModel"],
        "workingModel": identity_mod.setup_state()["workingModel"],
        "hasBotToken": bool(secrets["discord_token"]),
        "hasClientSecret": bool(secrets["discord_client_secret"]),
        "appId": secrets["discord_application_id"] or "",
        "publicKey": secrets["discord_public_key"] or "",
        "ownerId": next(iter(config["private"].get("ownerIds") or []), ""),
        "connected": identity_mod.connected(),
        "discordUsername": identity_mod.discord_username(),
    }


def _resolve_editable(rel: str) -> tuple[Path, str, str] | None:
    raw = str(rel or "").strip().replace("\\", "/")
    name = os.path.basename(raw)
    folder = raw.rsplit("/", 1)[0] if "/" in raw else ""
    if folder not in ("skills", "memory"):
        return None
    if not name.lower().endswith(".md"):
        return None
    stem = name[:-3]
    if not stem or not all(c.isalnum() or c in "-_" for c in stem):
        return None
    base = SKILLS_DIR if folder == "skills" else MEMORY_DIR
    target = (base / name).resolve()
    return target if base.resolve() in target.parents else None


def _same_origin() -> bool:
    origin = request.headers.get("Origin")
    if not origin:
        return True  # curl, a script, no Origin header
    from urllib.parse import urlparse

    try:
        return urlparse(origin).hostname in LOOPBACK
    except ValueError:
        return False


def create_app() -> Flask:
    app = Flask(__name__, static_folder=None)
    app.config["MAX_CONTENT_LENGTH"] = MAX_BODY

    @app.before_request
    def _guard():
        if not request.path.startswith("/api/"):
            return None
        if request.method != "GET" and not _same_origin():
            return jsonify({"error": "cross-origin write refused"}), 403
        return None

    # shell

    @app.get("/")
    def index():
        return (TEMPLATE_DIR / "index.html").read_text(encoding="utf-8"), 200, {
            "Content-Type": "text/html; charset=utf-8",
            "Cache-Control": "no-store",
        }

    @app.get("/stickers/<path:name>")
    def sticker_file(name: str):
        path = sticker_mod.sticker_path(name)
        if not path or not path.exists():
            return jsonify({"error": "not found"}), 404
        return send_from_directory(str(STICKER_DIR), path.name)

    @app.get("/fonts/<path:name>")
    def font_file(name: str):
        lowered = name.lower()
        if not lowered.endswith((".svg", ".png")):
            return jsonify({"error": "not found"}), 404
        return send_from_directory(str(LOGO_DIR), os.path.basename(name),
                                   max_age=86400)

    @app.get("/portrait")
    def portrait():
        path = identity_mod.find_portrait()
        if path is None or not path.exists():
            return jsonify({"error": "no portrait"}), 404
        return send_from_directory(str(path.parent), path.name, max_age=3600)

    # snapshot

    @app.get("/api/")
    def snapshot():
        people = await_people()
        skills = await_skills()
        records = load_affinity()
        ranked = ranked_affinity()
        # Off the election, not the ranking: a challenger within the switch
        # margin outranks the holder without being the crush.
        crush = crush_summary_affinity()
        inv = run_inventory()
        return jsonify(
            {
                "bot": {"name": bot_name(), "prefix": config["bot"]["prefix"],
                        "persona": config["bot"]["personaFile"]},
                # The per-person switch: whoever is in here gets no replies.
                "muted": sorted(muted_slugs()),
                # Her servers, snapshotted so the request never touches the
                # Discord client from this thread.
                "guilds": guild_mod.list_guilds(),
                # Her profile as config holds it, and as Discord will show it.
                # The preview uses the same live values the bot pushes, so panel
                # and card agree.
                "presence": {
                    "config": dict(config["bot"].get("presence") or {}),
                    "preview": presence_mod.preview(),
                },
                "model": current_model(),
                "models": {
                    "current": current_model(),
                    "choices": _models_payload(),
                    "providers": _providers_payload(),
                },
                "uptime": int(_uptime()),
                "affinity": {
                    "enabled": config["affinity"]["enabled"],
                    "eligible": config["affinity"]["eligiblePronouns"],
                    "threshold": config["affinity"]["romanceThreshold"],
                    "crushOn": crush_enabled_affinity(),
                    "crush": crush,
                    "people": ranked,
                    # Her temporary feelings, for the panel to show and set.
                    "feelingNames": list(feelings_mod.FEELINGS),
                    "feelingWords": feelings_mod.WORDS,
                    "feelingHalfLife": feelings_mod.half_life_minutes(),
                    "feelingsOn": feelings_mod.enabled(),
                    "bond": owner_bond_affinity(),
                    "bondOn": bond_mod.enabled(),
                    "bondThresholds": {
                        "low": bond_mod._setting("lowThreshold", -35),
                        "better": bond_mod._setting("betterThreshold", 10),
                        "minutes": bond_mod._setting("caringMinutes", 120),
                    },
                },
                # Her connection details. Public values only: the token and client
                # secret travel as booleans and never reach the browser.
                "setup": _setup_public(),
                "people": people,
                "memory": {p["slug"]: memory_mod.read_person(p["slug"]) or p for p in people},
                "skills": skills,
                "stickers": sticker_mod.sticker_inventory(),
                "files": _allowed_targets(),
                "resetTargets": [
                    {"key": k, "label": v["label"], "what": v["what"], "tier": v["tier"],
                     "optIn": v.get("optIn", False)}
                    for k, v in reset_mod.TARGETS.items()
                ],
                "totals": inv["totals"],
                "logs": recent_logs(),
                # Token spend, aggregated on read. Its own key, but the panel
                # still gets it in the same round trip.
                "usage": usage_mod.summary(_usage_window()),
            }
        )

    # first-run setup
    #
    # The wizard must work before the bot has ever logged in - the thing you
    # are missing is usually the token in step 4. So these routes need no
    # Discord connection, and each says plainly what it could not do rather
    # than claiming a success that only happened on disk.

    @app.get("/api/setup")
    def setup_state_route():
        state = _setup_public()
        state["firstRun"] = not state["hasWorkingModel"]
        # Whether the wizard was dismissed. On disk, so it survives a new
        # machine or a different browser.
        state["wizardDismissed"] = bool(
            config.get("setup", {}).get("wizardDismissed", False))
        return jsonify(state)

    @app.post("/api/setup/name")
    def setup_name_route():
        body = request.get_json(silent=True) or {}
        previous = bot_name()
        try:
            saved = set_bot_name(body.get("name"))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

        pushed, why = (True, "")
        if body.get("alsoDiscordName"):
            pushed, why = identity_mod.push_display_name()

        # Her name is in persona.md's heading and in skill descriptions too, so
        # config alone would leave her files calling her by the old name.
        touched = identity_mod.rename_in_files(previous, saved)

        print(f"[setup] name set to {saved}" + (f" (also in {', '.join(touched)})" if touched else ""))
        return jsonify({"ok": True, "name": saved, "discordNamePushed": pushed,
                        "why": why, "renamedIn": touched})

    @app.post("/api/setup/portrait")
    def setup_portrait_route():
        body = request.get_json(silent=True) or {}
        path, why = identity_mod.save_portrait(body.get("data"))
        if path is None:
            return jsonify({"error": why}), 400

        pushed, note = identity_mod.push_avatar()
        print(f"[setup] portrait saved: {path.name} (discord: {'yes' if pushed else 'not yet'})")
        return jsonify({"ok": True, "saved": path.name, "pushedToDiscord": pushed, "why": note})

    @app.post("/api/setup/owner")
    def setup_owner_route():
        body = request.get_json(silent=True) or {}
        raw = str(body.get("userId") or "").strip()
        if not raw:
            return jsonify({"error": "that does not look like a Discord user ID",
                            "hint": "Settings -> Advanced -> Developer Mode, then copy Developer ID"}), 400
        if not raw.isdigit() or not 15 <= len(raw) <= 25:
            return jsonify({"error": "a Discord user ID is 15-25 digits"}), 400

        for section in ("private", "bond"):
            _patch_config(section, "ownerIds", [raw])
            config[section]["ownerIds"] = [raw]

        print(f"[setup] owner set to {raw}")
        return jsonify({"ok": True, "userId": raw,
                        "privateOwnerIds": config["private"]["ownerIds"],
                        "bondOwnerIds": config["bond"]["ownerIds"]})

    @app.post("/api/setup/token")
    def setup_token_route():
        body = request.get_json(silent=True) or {}
        token = str(body.get("token") or "").strip()
        ok, hint = validate_bot_token(token)
        if not ok:
            return jsonify({"error": hint}), 400
        try:
            set_env_secret("DISCORD_TOKEN", token)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        print("[setup] DISCORD_TOKEN written to .env - restart her to connect")
        return jsonify({"ok": True, "restartNeeded": True,
                        "hint": "written to .env - restart the bot to connect"})

    @app.post("/api/setup/discord")
    def setup_discord_route():
        body = request.get_json(silent=True) or {}

        app_id = str(body.get("appId") or "").strip()
        if app_id:
            if not app_id.isdigit() or not 15 <= len(app_id) <= 25:
                return jsonify({"error": "an application ID is 15-25 digits",
                                "hint": "Developer Portal -> General Information"}), 400
            _patch_config("bot", "applicationId", app_id)
            config["bot"]["applicationId"] = app_id
            set_env_secret("DISCORD_APPLICATION_ID", app_id)
            # The client id is the same number by definition; writing it keeps
            # install-link building working without a second field.
            set_env_secret("DISCORD_CLIENT_ID", app_id)
        elif body.get("clearAppId"):
            _patch_config("bot", "applicationId", "")
            config["bot"]["applicationId"] = ""

        public_key = str(body.get("publicKey") or "").strip()
        if public_key:
            if len(public_key) != 64 or not re.fullmatch(r"[0-9a-fA-F]{64}", public_key):
                return jsonify({"error": "the public key is 64 hex characters",
                                "hint": "Developer Portal -> General Information -> Public Key"}), 400
            set_env_secret("DISCORD_PUBLIC_KEY", public_key)

        client_secret = str(body.get("clientSecret") or "").strip()
        if client_secret:
            try:
                set_env_secret("DISCORD_CLIENT_SECRET", client_secret)
            except ValueError as exc:
                return jsonify({"error": str(exc)}), 400

        print(f"[setup] portal details saved (app id: {'yes' if app_id else 'unchanged'})")
        return jsonify({"ok": True, "appId": app_id or config["bot"].get("applicationId", ""),
                        "publicKeySet": bool(secrets["discord_public_key"]),
                        "clientSecretSet": bool(secrets["discord_client_secret"])})

    @app.post("/api/setup/dismissed")
    def setup_dismissed_route():
        body = request.get_json(silent=True) or {}
        value = bool(body.get("dismissed", True))
        config.setdefault("setup", {})["wizardDismissed"] = value
        _patch_config("setup", "wizardDismissed", value)
        print(f"[setup] wizard auto-open {'off' if value else 'on'}")
        return jsonify({"ok": True, "wizardDismissed": value})

    @app.post("/api/setup/finish")
    def setup_finish_route():
        state = identity_mod.setup_state()
        state["hasBotToken"] = bool(secrets["discord_token"])
        missing = []
        if not state["hasWorkingModel"]:
            missing.append("a model API key - she has nothing to talk on")
        if not state["hasBotToken"]:
            missing.append("DISCORD_TOKEN in .env - she cannot connect")
        print(f"[setup] finished: {'; '.join(missing) if missing else 'all set'}")
        return jsonify({"ok": True, "missing": missing, **state})

    # model

    @app.get("/api/models")
    def list_models_route():
        removed = usage_mod.clear()
        return jsonify({"removed": removed, "usage": usage_mod.summary(_usage_window())})

    @app.post("/api/mute")
    def set_mute_route():
        body = request.get_json(silent=True) or {}

        # One person or a whole selection: the People tab sends the group, so
        # twenty toggles cost one round trip.
        group = body.get("slugs")
        if isinstance(group, list):
            if len(group) > 50:
                return jsonify({"error": "50 at a time, tops"}), 400
            slugs = []
            for item in group:
                slug = _clean_slug(item)
                if not slug:
                    return jsonify({"error": f"that does not look like a person's slug: {str(item)[:40]}"}), 400
                if slug not in slugs:
                    slugs.append(slug)
        else:
            raw_slug = str(body.get("slug") or "").strip().lower()
            if not raw_slug:
                return jsonify({"error": "no person given", 'hint': 'send {"slug": "...", "muted": true|false}'}), 400
            slug = _clean_slug(raw_slug)
            if not slug:
                return jsonify({"error": "that does not look like a person's slug"}), 400
            slugs = [slug]

        if "muted" not in body:
            return jsonify({"error": "no state given", 'hint': 'send {"slug": "...", "muted": true|false}'}), 400
        raw = body["muted"]
        on = raw if isinstance(raw, bool) else str(raw).strip().lower() in ("1", "true", "on", "yes")

        # The whole list is checked before any of it is written: entry nine
        # being nonsense must not leave the toggle half-applied.
        after = muted_slugs()
        for slug in slugs:
            after = set_muted(slug, on)
        print(f"[mute] {', '.join(slugs)}: {'quiet' if on else 'answering again'}")
        return jsonify({
            "ok": True,
            "slug": slugs[0] if len(slugs) == 1 else None,
            "slugs": slugs,
            "isMuted": (slugs[0] in after) if len(slugs) == 1 else on,
            "muted": sorted(after),
        })

    @app.post("/api/guilds/leave")
    def leave_guild_route():
        body = request.get_json(silent=True) or {}
        raw = str(body.get("id") or "").strip()
        if not raw:
            return jsonify({"error": "no server given", 'hint': 'send {"id": "123456789"}'}), 400
        if not raw.isdigit() or len(raw) > 25:
            return jsonify({"error": "that does not look like a server id"}), 400

        ok, detail = guild_mod.leave(raw)
        if not ok:
            # 409: the request was well-formed but she is not leaving.
            return jsonify({"error": detail, "guilds": guild_mod.list_guilds()}), 409
        print(f"[guild] {detail}")
        return jsonify({"ok": True, "id": raw, "guilds": guild_mod.list_guilds()})

    @app.post("/api/model")
    def set_model_route():
        body = request.get_json(silent=True) or {}
        key = body.get("apiKey")
        typed_key = key.strip() if isinstance(key, str) else ""
        env_name = str(body.get("apiKeyEnv") or "").strip()
        if typed_key and not env_name:
            return jsonify({"error": "name the API key variable before saving a key"}), 400
        try:
            saved = upsert_provider(body)
            if typed_key:
                set_env_secret(env_name, typed_key)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

        print(f"[model] provider saved: {saved['id']}")
        return jsonify({
            "ok": True,
            "provider": public_provider(saved),
            "choices": _models_payload(),
            "providers": _providers_payload(),
        })

    @app.delete("/api/provider/<path:provider_id>")
    def delete_provider_route(provider_id: str):
        body = request.get_json(silent=True) or {}
        provider_id = str(body.get("id") or "").strip()
        stored = next(
            (dict(provider) for provider in (config["model"].get("providers") or [])
             if isinstance(provider, dict) and str(provider.get("id") or "") == provider_id),
            None,
        )
        if provider_id and stored is None:
            return jsonify({"error": "no such provider"}), 404

        provider = stored or {"id": provider_id or "new"}
        for name in ("name", "baseUrl", "apiKeyEnv", "apiKeyEnvPrefix"):
            value = body.get(name)
            if isinstance(value, str) and value.strip():
                provider[name] = value.strip()
        if not provider.get("baseUrl"):
            return jsonify({"error": "a base URL is needed before fetching models"}), 400
        provider.setdefault("model", "-")

        key = body.get("apiKey")
        typed_key = key.strip() if isinstance(key, str) and key.strip() else None
        try:
            models = fetch_models(provider, typed_key)
        except ProviderError as exc:
            return jsonify({"error": str(exc), "public": getattr(exc, "public", "")}), 502

        return jsonify({"ok": True, "id": provider["id"], "models": models})

    # file read / write

    @app.get("/api/file/<path:rel>")
    def read_file(rel: str):
        target = _resolve_editable(rel)
        if not target:
            return jsonify({"error": "not an editable file"}), 400
        path, group, name = target
        if not path.exists():
            return jsonify({"error": "no such file"}), 404
        return jsonify({"path": f"{group}/{name}", "content": path.read_text(encoding="utf-8")})

    @app.get("/api/named/<key>")
    def read_named(key: str):
        entry = NAMED_FILES.get(key)
        if not entry:
            return jsonify({"error": "unknown file"}), 400
        path = entry()
        return jsonify({"path": path.name, "content": path.read_text(encoding="utf-8") if path.exists() else ""})

    @app.put("/api/file/<path:rel>")
    def write_file(rel: str):
        target = _resolve_editable(rel)
        if not target:
            return jsonify({"error": "not an editable file"}), 400
        path, group, name = target
        content = (request.get_json(silent=True) or {}).get("content")
        if not isinstance(content, str):
            return jsonify({"error": "content must be a string"}), 400
        if len(content) > MAX_BODY:
            return jsonify({"error": "too big"}), 413
        path.write_text(content, encoding="utf-8")
        return jsonify({"ok": True, "saved": f"{group}/{name}"})

    @app.put("/api/named/<key>")
    def write_named(key: str):
        entry = NAMED_FILES.get(key)
        if not entry:
            return jsonify({"error": "unknown file"}), 400
        content = (request.get_json(silent=True) or {}).get("content")
        if not isinstance(content, str):
            return jsonify({"error": "content must be a string"}), 400
        # config.json and stickers.json must stay parseable or nothing loads.
        if key in ("config", "stickers"):
            try:
                json.loads(content)
            except json.JSONDecodeError as exc:
                return jsonify({"error": f"invalid JSON: {exc}"}), 500
        path = entry()
        path.write_text(content, encoding="utf-8")
        return jsonify({"ok": True, "saved": path.name, "restartNeeded": key == "config"})

    @app.delete("/api/file/<path:rel>")
    def delete_file(rel: str):
        # A bare "name.md" is ambiguous (skills/ vs memory/), so a destructive
        # call names the folder explicitly.
        if "skills/" not in str(rel).replace("\\", "/"):
            return jsonify({"error": "delete needs an explicit skills/<name>.md path"}), 400
        target = _resolve_in(rel, SKILLS_DIR)
        if not target:
            return jsonify({"error": "only skill files can be deleted"}), 400
        try:
            target.unlink()
        except OSError:
            pass
        return jsonify({"ok": True})

    # skills

    @app.post("/api/skill")
    def create_skill():
        body = request.get_json(silent=True) or {}
        name = str(body.get("name") or "")
        content = body.get("content")
        if not name or len(name) > 40 or not all(c.isalnum() or c in "-_" for c in name):
            return jsonify({"error": "bad name (letters, numbers, - and _ only)"}), 400
        if not isinstance(content, str) or not content.strip():
            return jsonify({"error": "empty skill"}), 400
        (SKILLS_DIR / f"{name}.md").write_text(content, encoding="utf-8")
        return jsonify({"ok": True, "saved": f"skills/{name}.md"})

    # affect

    @app.post("/api/affinity/pronouns")
    def set_pronouns_route():
        body = request.get_json(silent=True) or {}
        slug = str(body.get("slug") or "")
        if not slug or len(slug) > 40 or not all(c.isalnum() or c in "-_" for c in slug):
            return jsonify({"error": "bad slug"}), 400
        set_pronouns(slug, None, str(body.get("pronouns") or "")[:40])
        return jsonify({"ok": True})

    @app.post("/api/affinity/toggle")
    def toggle_crush():
        from ..affinity import load as load_affinity, load_all as load_all_affinity

        body = request.get_json(silent=True) or {}
        slug = str(body.get("slug") or "")
        if not slug:
            return jsonify({"error": "bad slug"}), 400
        record = load_affinity(slug)
        if record["interactions"] == 0:
            return jsonify({"error": "no record for that person"}), 400
        record["crushEnabled"] = not record["crushEnabled"]
        if not record["crushEnabled"]:
            record["romance"] = 0
        save_affinity(record)
        # Ruling someone out frees the spot; opting back in reclaims a zeroed
        # lean. Re-elect over everyone so the files agree with the panel.
        everyone = load_all_affinity()
        elect_crush_affinity(everyone)
        for item in everyone:
            save_affinity(item)
        return _reel({"crushEnabled": record["crushEnabled"]})

    # feelings / crush

    def _reel(what):
        crush = crush_summary_affinity()
        people = ranked_affinity()
        return jsonify({"ok": True, **what, "crush": crush, "people": people})

    @app.post("/api/affinity/adjust")
    def adjust_affinity_route():
        body = request.get_json(silent=True) or {}
        slug = _clean_slug(body.get("slug"))
        if not slug:
            return jsonify({"error": "bad person id"}), 400
        wanted = {k: body[k] for k in ("warmth", "familiarity", "romance") if k in body}
        if not wanted:
            return jsonify({"error": "nothing to change"}), 400
        try:
            record = adjust_affinity(slug, wanted)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return _reel({"slug": record["slug"], "record": summarise_affinity(record)})

    @app.post("/api/affinity/feelings")
    def set_feelings_route():
        body = request.get_json(silent=True) or {}
        slug = _clean_slug(body.get("slug"))
        if not slug:
            return jsonify({"error": "bad person id"}), 400
        values = body.get("feelings")
        if not isinstance(values, dict) or not values:
            return jsonify({"error": "send {\"slug\": \"...\", \"feelings\": {\"happy\": 40}}"}), 400

        unknown = [name for name in values if name not in feelings_mod.FEELINGS]
        if unknown:
            return jsonify({
                "error": f"she does not feel {unknown[0]}",
                "hint": f"her feelings are: {', '.join(feelings_mod.FEELINGS)}",
            }), 400
        record = load_affinity_one(slug)
        if record["interactions"] == 0:
            return jsonify({"error": "no record for that person yet"}), 400
        try:
            record = adjust_affinity(slug, {"feelings": values})
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return _reel({"slug": record["slug"], "record": summarise_affinity(record)})

    @app.post("/api/affinity/bond")
    def set_bond_route():
        body = request.get_json(silent=True) or {}
        slug = _clean_slug(body.get("slug"))
        if slug:
            try:
                record = designate_affinity(slug)
            except ValueError as exc:
                return jsonify({"error": str(exc)}), 400
            return _reel({"given": record["slug"]})
        release_crush_affinity()
        return _reel({"given": None})

    @app.post("/api/affinity/crush-enabled")
    def set_crush_enabled_route():
        body = request.get_json(silent=True) or {}
        enabled = set_crush_enabled(bool(body.get("enabled")))
        if not enabled:
            sync_crush_marks_affinity()
        return _reel({"crushOn": crush_enabled_affinity()})

    @app.post("/api/affinity/reset")
    def reset_affinity_route():
        body = request.get_json(silent=True) or {}
        slug = _clean_slug(body.get("slug"))
        if not slug:
            return jsonify({"error": "bad person id"}), 400
        record = load_affinity_one(slug)
        if record["interactions"] == 0:
            return jsonify({"error": "no record for that person"}), 400
        try:
            adjust_affinity(slug, {"warmth": 0, "familiarity": 0, "romance": 0})
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        record = load_affinity_one(slug)
        record["history"] = []
        save_affinity(record)
        return _reel({"slug": slug, "record": summarise_affinity(record)})

    # people: bulk

    @app.post("/api/people/forget")
    def forget_people_route():
        body = request.get_json(silent=True) or {}
        raw = body.get("slugs")
        if not isinstance(raw, list) or not raw:
            return jsonify({"error": "no one given", "hint": 'send {"slugs": ["name"] }'}), 400
        if len(raw) > 50:
            return jsonify({"error": "50 at a time, tops"}), 400

        slugs: list[str] = []
        for item in raw:
            slug = _clean_slug(item)
            if not slug:
                return jsonify({"error": f"bad person id: {str(item)[:40]}"}), 400
            if slug not in slugs:
                slugs.append(slug)

        gone, kept = [], []
        for slug in slugs:
            # Affect goes with the memory: a crush on somebody she was told to
            # forget would be a strange leftover.
            if memory_mod.forget_all(slug):
                gone.append(slug)
            else:
                kept.append(slug)
            path = AFFINITY_DIR / f"{slug}.json"
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        invalidate_affinity()
        print(f"[forget] {' '.join(gone) or 'nothing'} - {len(gone)} forgotten, {len(kept)} had no file")
        return jsonify({"ok": True, "forgotten": gone, "missing": kept})

    # presence

    @app.post("/api/presence")
    def set_presence_route():
        body = request.get_json(silent=True) or {}
        try:
            saved = set_presence(body)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        applied, why = presence_mod.apply_now()
        return jsonify({
            "ok": True,
            "applied": applied,
            "why": why,
            "config": saved,
            "preview": presence_mod.preview(),
        })

    # stickers

    @app.post("/api/sticker")
    def upload_sticker():
        body = request.get_json(silent=True) or {}
        name = str(body.get("name") or "")
        data = str(body.get("data") or "")
        if not sticker_mod._is_image(name):
            return jsonify({"error": "png, jpg, webp or gif only"}), 400
        import base64
        import binascii

        if "," in data and data.strip().startswith("data:"):
            data = data.split(",", 1)[1]
        if not data.strip():
            return jsonify({"error": "no image data"}), 400
        try:
            blob = base64.b64decode(data, validate=True)
        except (binascii.Error, ValueError):
            return jsonify({"error": "bad base64"}), 400
        if not blob:
            return jsonify({"error": "no image data"}), 400
        if len(blob) > 4 * 1024 * 1024:
            return jsonify({"error": "image too large (max 4 MB)"}), 413
        path = sticker_mod.sticker_path(name)
        if not path:
            return jsonify({"error": "bad filename"}), 400
        path.write_bytes(blob)
        return jsonify({"ok": True, "saved": path.name})

    @app.delete("/api/sticker/<path:name>")
    def delete_sticker(name: str):
        path = sticker_mod.sticker_path(name)
        if not path or not path.exists():
            return jsonify({"error": "not found"}), 404
        path.unlink()
        return jsonify({"ok": True})

    # reset

    @app.post("/api/reset/preview")
    def reset_preview():
        body = request.get_json(silent=True) or {}
        return jsonify(_preview(body.get("targets") or []))

    @app.post("/api/reset/run")
    def reset_run():
        body = request.get_json(silent=True) or {}
        targets = body.get("targets") or []
        plan = _preview(targets)
        if not plan["removed"]:
            return jsonify({"error": "nothing selected"}), 400
        if plan["typedConfirmation"] and str(body.get("confirm") or "").strip().upper() != plan["typedConfirmation"]:
            return jsonify({
                "error": f"type {plan['typedConfirmation']} to confirm - this deletes hand-written files",
                "needs": plan["typedConfirmation"],
            }), 400
        return jsonify(_run(targets))

    @app.get("/api/reset/backups")
    def reset_backups():
        return jsonify({"backups": reset_mod.list_backups()})

    @app.post("/api/reset/restore")
    def reset_restore():
        body = request.get_json(silent=True) or {}
        return jsonify(_restore(str(body.get("id") or "")))

    @app.errorhandler(413)
    def too_large(_error):
        return jsonify({"error": "payload too large"}), 413

    return app


_STARTED_AT = time.time()


def _uptime() -> float:
    return time.time() - _STARTED_AT


def await_people() -> list[dict]:
    return [{"slug": p["slug"], "name": p["name"], "count": memory_mod.count_facts(p)} for p in memory_mod.list_people()]


def await_skills() -> list[dict]:
    return [
        {"file": s["file"], "name": s["name"], "always": s["always"], "keywords": s["keywords"],
         "description": s["description"], "bytes": len(s["content"])}
        for s in load_skills(force=True)
    ]


def run_inventory() -> dict:
    """The inventory is async, but everything it touches is sync, so drive it here."""
    import asyncio

    return asyncio.run(reset_mod.inventory())


def _preview(targets) -> dict:
    import asyncio

    return asyncio.run(reset_mod.preview(targets))


def _run(targets) -> dict:
    import asyncio

    return asyncio.run(reset_mod.run(targets))


def _restore(backup_id: str) -> dict:
    import asyncio

    return asyncio.run(reset_mod.restore(backup_id))


def start_dashboard() -> Flask | None:
    if not config["dashboard"]["enabled"]:
        print("[dashboard] disabled (set dashboard.enabled = true in config.json)")
        return None

    host = config["dashboard"]["host"]
    # No password, so loopback is the only acceptable bind: this app rewrites
    # her persona, skills and memory.
    if host not in LOOPBACK:
        print(
            f'[dashboard] REFUSING to start: host is "{host}".\n'
            "            This app can rewrite her persona, skills and memory, and there is no\n"
            '            password. Set dashboard.host to "127.0.0.1" in config.json.'
        )
        return None

    app = create_app()
    url = f"http://{host}:{config['dashboard']['port']}"
    print(f"[dashboard] {url}  (loopback only, no password)")
    if config["dashboard"].get("openBrowser"):
        try:
            webbrowser.open(url)
        except Exception:
            pass

    # Flask's dev server is fine for a loopback panel.
    app.run(host=host, port=config["dashboard"]["port"], debug=False, use_reloader=False, threaded=True)
    return app
