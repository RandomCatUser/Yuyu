"""Data reset: preview, backup, run, restore.

Learned data is swept by the bulk button; authored and opt-in data is never
touched without saying so, and hand-written files need a typed confirmation. A
backup is written before anything is deleted."""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone

from . import affinity as affinity_mod
from . import context
from .affinity import load_all as load_affinity
from .affinity import invalidate as invalidate_affinity
from .config import AFFINITY_DIR, BACKUP_DIR, MEMORY_DIR, ROOT, SKILLS_DIR, STICKER_DIR, config
from .memory import SECTION_KEYS, count_facts, list_people, read_person
from .skills import load_skills

TARGETS = {
    "memory": {
        "label": "Memory files",
        "what": "memory/*.md - everything people told her",
        "tier": "learned",
        "optIn": True,
    },
    "affect": {
        "label": "Affect (familiarity / warmth / crush)",
        "what": "affinity/*.json - including who she currently has a crush on",
        "tier": "learned",
        "optIn": True,
    },
    "buffers": {
        "label": "Conversation context",
        "what": "Recent messages and saved conversation summaries per channel.",
        "tier": "learned",
        "optIn": False,
    },
    "skills": {
        "label": "Skill files",
        "what": "skills/*.md - files you or I wrote by hand",
        "tier": "authored",
        "optIn": True,
    },
    "persona": {
        "label": "Persona",
        "what": "persona.md - who she is",
        "tier": "authored",
        "optIn": True,
    },
    "stickerImages": {
        "label": "Sticker images",
        "what": "stickers/*.png|jpg|webp|gif",
        "tier": "authored",
        "optIn": True,
    },
    "config": {
        "label": "Config",
        "what": "config.json - model, memory, affect, dashboard settings",
        "tier": "authored",
        "optIn": True,
    },
    "stickers": {
        "label": "Sticker definitions",
        "what": "stickers.json - which image fires when",
        "tier": "authored",
        "optIn": True,
    },
}

FILE_KEYS = {"persona", "stickers", "config"}


def _file_for_target(key: str) -> Path | None:
    return {
        "persona": ROOT / config["bot"]["personaFile"],
        "stickers": ROOT / config["emoji"]["stickersFile"],
        "config": ROOT / "config.json",
    }.get(key)


def _normalise(selected) -> list[str]:
    keys = [str(k) for k in (selected or []) if str(k) in TARGETS]
    return list(dict.fromkeys(keys))


# --- inventory -------------------------------------------------------------

async def inventory() -> dict:
    people = list_people()
    records = load_affinity()

    memory = []
    for person in people:
        full = read_person(person["slug"]) or person
        memory.append(
            {
                "slug": person["slug"],
                "name": person["name"],
                "facts": count_facts(full),
                "detail": [item for key in SECTION_KEYS for item in full.get(key, [])][:6],
            }
        )

    crush = affinity_mod.current_crush()
    buffers = context.stats()
    skills = load_skills(force=True)

    return {
        "memory": memory,
        "affinity": [
            {
                "slug": r["slug"], "name": r["name"], "pronouns": r.get("pronouns", ""),
                "turns": r["interactions"], "romance": round(r["romance"]),
                "isCrush": bool(crush and crush["slug"] == r["slug"]),
            }
            for r in records
        ],
        "crush": ({"slug": crush["slug"], "name": crush["name"], "romance": round(crush["romance"])} if crush else None),
        "buffers": buffers,
        "skills": [{"file": s["file"], "always": s["always"]} for s in skills],
        "totals": {
            "memoryFiles": len(memory),
            "memoryFacts": sum(m["facts"] for m in memory),
            "affinityFiles": len(records),
            "bufferedChannels": len(buffers),
            "bufferedMessages": sum(b["entries"] for b in buffers),
            "skills": len(skills),
        },
    }


async def preview(selected) -> dict:
    """A dry run: exactly what would go, and what is being kept."""
    keys = _normalise(selected)
    inv = await inventory()
    removed: list[dict] = []
    kept: list[str] = []

    for key in keys:
        if key == "memory":
            removed.append({
                "key": key, "label": TARGETS["memory"]["label"],
                "count": inv["totals"]["memoryFiles"], "unit": "file(s)",
                "detail": [f"{m['name']} - {m['facts']} fact(s)" for m in inv["memory"]],
                "risk": "learned",
            })
        elif key == "affect":
            removed.append({
                "key": key, "label": TARGETS["affect"]["label"],
                "count": inv["totals"]["affinityFiles"], "unit": "file(s)",
                "detail": [
                    f"{a['name']}{' (current crush)' if a['isCrush'] else ''} - {a['turns']} turn(s)"
                    for a in inv["affinity"]
                ],
                "risk": "learned",
            })
        elif key == "buffers":
            removed.append({
                "key": key, "label": TARGETS["buffers"]["label"],
                "count": inv["totals"]["bufferedChannels"], "unit": "channel(s)",
                "detail": [
                    f"{b['key']} - {b['entries']} msg(s)"
                    + (" + saved summary" if b["summary"] else "")
                    for b in inv["buffers"]
                ],
                "risk": "learned",
            })
        elif key == "skills":
            removed.append({
                "key": key, "label": TARGETS["skills"]["label"],
                "count": inv["totals"]["skills"], "unit": "file(s)",
                "detail": [f"{s['file']}{' (always on)' if s['always'] else ''}" for s in inv["skills"]],
                "risk": "authored",
            })
        elif key == "stickerImages":
            images = sorted(p.name for p in STICKER_DIR.glob("*") if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp", ".gif"))
            removed.append({
                "key": key, "label": TARGETS["stickerImages"]["label"],
                "count": len(images), "unit": "file(s)", "detail": images, "risk": "authored",
            })
        elif key in FILE_KEYS:
            path = _file_for_target(key)
            removed.append({
                "key": key, "label": TARGETS[key]["label"], "count": 1, "unit": "file",
                "detail": [path.name if path else key], "risk": "authored",
            })

    for key, target in TARGETS.items():
        if key not in keys:
            kept.append(f"{target['label']} - {target['what']}")

    return {
        "removed": removed,
        "kept": kept,
        # A reset that touches hand-written files has to be typed out, not clicked.
        "typedConfirmation": "RESET" if any(r["risk"] == "authored" for r in removed) else None,
        "learnedOnly": all(r["risk"] == "learned" for r in removed) if removed else True,
    }


# --- backup ----------------------------------------------------------------

def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")


def list_backups() -> list[dict]:
    out = []
    if not BACKUP_DIR.exists():
        return out
    for entry in sorted(BACKUP_DIR.iterdir(), reverse=True):
        if not entry.is_dir():
            continue
        try:
            manifest = json.loads((entry / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            manifest = None
        out.append({
            "id": entry.name,
            "created": (manifest or {}).get("created"),
            "targets": (manifest or {}).get("targets"),
            "files": len(_manifest_files(manifest)) if manifest else None,
        })
    return out


def _manifest_files(manifest) -> list[dict]:
    """Normalise a manifest to {from, to} records.

    Older manifests stored bare backup-relative strings and put the fixed-name
    files under files/ - which is not where they belong.
    """
    out = []
    for entry in (manifest or {}).get("files") or []:
        if isinstance(entry, str):
            to = entry[len("files/"):] if entry.startswith("files/") else entry
            out.append({"from": entry, "to": to})
        else:
            out.append({"from": entry.get("from"), "to": entry.get("to") or entry.get("from")})
    return out


def _make_backup(keys: list[str]) -> dict | None:
    root = BACKUP_DIR / _stamp()
    files: list[dict] = []

    def record(from_rel: str, to_rel: str) -> None:
        source = ROOT / to_rel
        if not source.exists():
            return
        dest = root / from_rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest)
        files.append({"from": from_rel, "to": to_rel})

    if "memory" in keys:
        for path in sorted(MEMORY_DIR.glob("*.md")):
            if path.name != ".gitkeep":
                record(f"memory/{path.name}", f"memory/{path.name}")
    if "affect" in keys:
        for path in sorted(AFFINITY_DIR.glob("*.json")):
            record(f"affinity/{path.name}", f"affinity/{path.name}")
    if "skills" in keys:
        for path in sorted(SKILLS_DIR.glob("*.md")):
            record(f"skills/{path.name}", f"skills/{path.name}")
    if "stickerImages" in keys:
        for path in sorted(STICKER_DIR.iterdir()):
            if path.is_file() and path.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
                record(f"stickers/{path.name}", f"stickers/{path.name}")
    if "buffers" in keys and context.SUMMARY_FILE.exists():
        record("memory/conversations.json", "memory/conversations.json")
    for key in FILE_KEYS:
        if key in keys:
            path = _file_for_target(key)
            if path:
                record(f"files/{path.name}", path.name)

    if not files:
        return None

    root.mkdir(parents=True, exist_ok=True)
    (root / "manifest.json").write_text(
        json.dumps(
            {"created": datetime.now(timezone.utc).isoformat(), "targets": keys, "files": files},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return {"id": root.name, "files": files}


async def restore(backup_id: str) -> dict:
    if not all(c.isalnum() or c in "-_." for c in str(backup_id or "")):
        return {"ok": False, "error": "bad backup id"}
    root = BACKUP_DIR / str(backup_id)
    try:
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"ok": False, "error": "no such backup"}

    restored: list[str] = []
    for entry in _manifest_files(manifest):
        dest = (ROOT / entry["to"]).resolve()
        # Never let a manifest point outside the project.
        if ROOT.resolve() not in dest.parents:
            continue
        source = root / entry["from"]
        if not source.exists():
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest)
        restored.append(entry["to"])

    invalidate_affinity()
    context.reload_summaries()
    return {"ok": True, "restored": restored}


# --- execute ---------------------------------------------------------------

async def run(selected) -> dict:
    keys = _normalise(selected)
    if not keys:
        return {"ok": False, "error": "nothing selected"}

    backup = _make_backup(keys)
    deleted: list[str] = []
    errors: list[str] = []

    def rm(path: Path, label: str) -> None:
        try:
            path.unlink()
            deleted.append(label)
        except FileNotFoundError:
            pass
        except OSError as exc:
            errors.append(f"{label}: {exc}")

    if "memory" in keys:
        for path in sorted(MEMORY_DIR.glob("*.md")):
            if path.name != ".gitkeep":
                rm(path, f"memory/{path.name}")
        # The folder itself always survives - only the files inside are removed.
        MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    if "affect" in keys:
        for path in sorted(AFFINITY_DIR.glob("*.json")):
            rm(path, f"affinity/{path.name}")
        invalidate_affinity()
    if "buffers" in keys:
        deleted.append(f"{context.clear_all()} conversation context(s) cleared")
    if "skills" in keys:
        for path in sorted(SKILLS_DIR.glob("*.md")):
            rm(path, f"skills/{path.name}")
    if "stickerImages" in keys:
        for path in sorted(STICKER_DIR.iterdir()):
            if path.is_file() and path.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
                rm(path, f"stickers/{path.name}")
    for key in FILE_KEYS:
        if key in keys:
            path = _file_for_target(key)
            if path:
                rm(path, path.name)

    return {
        "ok": not errors,
        "targets": keys,
        "deleted": deleted,
        "errors": errors,
        "backup": {"id": backup["id"], "files": len(backup["files"])} if backup else None,
        "configRestartNeeded": any(k in keys for k in ("config", "persona", "stickers", "skills")),
    }
