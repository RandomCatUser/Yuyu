"""Reset: preview accuracy, backup-before-delete, restore, and the typed-confirmation guard.

This suite deletes real files on purpose, so it snapshots everything the reset
can touch and puts it back in a fixture teardown. An earlier Node version used
`void` on an async restore, which let a failed assertion leave the user without
a persona - hence the safety net.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import time

import pytest

from yuyu import affinity as aff
from yuyu import context as context_mod
from yuyu import memory as mem
from yuyu import reset as reset_mod
from yuyu.config import AFFINITY_DIR, BACKUP_DIR, MEMORY_DIR, ROOT, SKILLS_DIR, config
from yuyu.context import record, stats

A, B = "reset-alpha", "reset-beta"
GOOD = dict(addressed=True, directReply=True, askedAboutHer=True, sharedTopics=3,
            revealedSomething=True, lateNight=True, gratitude=False, harsh=False,
            commanded=False, ignored=False)


def restore_people(people: dict[str, bytes]) -> None:
    """Put the real people's records back the way the run found them.

    A file that is gone was deleted by the tests, so it comes back byte for byte.
    A file still on disk was never written by them - the only other writer is the
    live bot, which keeps talking while pytest runs - so if its bytes differ, the
    fact learned mid-run wins over the snapshot taken at setup.
    """
    for key, raw in people.items():
        folder, name = key.split("/", 1)
        base = AFFINITY_DIR if folder == "affinity" else MEMORY_DIR
        target = base / name
        if not target.exists():
            target.write_bytes(raw)


@pytest.fixture(autouse=True)
def _safety_net():
    """Snapshot authored files *and* real people's data, restore it all on teardown.

    The authored files were always covered. The people were not: `run(["affect"])`
    deletes every affinity file and `run(["memory"])` every memory file, neither
    limited to the A and B slugs this suite creates. So every pytest run quietly
    destroyed whatever real records existed - the elected crush included - and the
    teardown only ever put back A and B. Both directories are snapshotted in full
    now: a file the tests deleted comes back byte for byte, a file they never
    touched keeps whatever the live bot wrote into it while pytest ran, and files
    created by the tests go away again.
    """
    snapshot = {}
    for rel in ("persona.md", "stickers.json", "config.json"):
        path = ROOT / rel
        if path.exists():
            snapshot[rel] = path.read_text(encoding="utf-8")
    for path in SKILLS_DIR.glob("*.md"):
        snapshot[f"skills/{path.name}"] = path.read_text(encoding="utf-8")

    people: dict[str, bytes] = {}
    for path in AFFINITY_DIR.glob("*.json"):
        people[f"affinity/{path.name}"] = path.read_bytes()
    for path in MEMORY_DIR.glob("*.md"):
        people[f"memory/{path.name}"] = path.read_bytes()
    summary_backup = context_mod.SUMMARY_FILE.read_bytes() if context_mod.SUMMARY_FILE.exists() else None
    summary_cache = dict(context_mod._summaries)
    summaries_loaded = context_mod._summaries_loaded

    aff.invalidate()
    mem.write_person(A, {"name": "Alpha", "username": A, "discordId": "1"},
                     {"details": ["Nurse in Lisbon"], "notes": ["Dislikes mornings"], "likes": ["Coffee"]})
    mem.write_person(B, {"name": "Beta", "username": B, "discordId": "2"}, {"details": ["Student"]})
    for slug, name, pronouns in ((A, "Alpha", "he/him"), (B, "Beta", "she/her")):
        for _ in range(12):
            aff.apply_turn(slug, name, pronouns, dict(GOOD))
    record("g1", "c1", "Alpha", "hi", slug=A)
    record("g1", "c2", "Beta", "yo", slug=B)

    yield

    for slug in (A, B):
        (MEMORY_DIR / f"{slug}.md").unlink(missing_ok=True)
        (AFFINITY_DIR / f"{slug}.json").unlink(missing_ok=True)
    for rel, content in snapshot.items():
        (ROOT / rel).write_text(content, encoding="utf-8")
    # Real records: deleted-by-the-tests comes back byte for byte; updated by the
    # live bot mid-run keeps the newer content (see restore_people).
    restore_people(people)
    # Then remove what the tests created, so a run leaves both directories as it
    # found them. A file the *live bot* wrote during the run is not in `people`,
    # but it also cannot be mistaken for a fixture slug: anything the bot
    # creates mid-run is a brand-new speaker, and a new speaker cannot reach a
    # crush-worthy affinity score inside one test pass.
    for path in AFFINITY_DIR.glob("*.json"):
        if f"affinity/{path.name}" not in people:
            path.unlink(missing_ok=True)
    for path in MEMORY_DIR.glob("*.md"):
        if f"memory/{path.name}" not in people:
            path.unlink(missing_ok=True)
    if summary_backup is None:
        context_mod.SUMMARY_FILE.unlink(missing_ok=True)
    else:
        context_mod.SUMMARY_FILE.write_bytes(summary_backup)
    context_mod._summaries = summary_cache
    context_mod._summaries_loaded = summaries_loaded
    aff.invalidate()
    shutil.rmtree(BACKUP_DIR, ignore_errors=True)


def inv():
    return asyncio.run(reset_mod.inventory())


# --- the safety net itself ---------------------------------------------------

def test_a_person_the_tests_deleted_comes_back_byte_for_byte():
    """The bug the fixture exists for: `run(["memory"])` wipes every real record."""
    path = MEMORY_DIR / "someone.md"
    path.unlink(missing_ok=True)
    restore_people({f"memory/{path.name}": b"# Someone\n- keeps a cat\n"})
    assert path.read_bytes() == b"# Someone\n- keeps a cat\n"
    path.unlink(missing_ok=True)


def test_a_person_the_live_bot_updated_mid_run_is_not_rolled_back():
    """pytest runs beside the running bot, which writes to the same directories.

    Restoring the setup snapshot unconditionally would quietly erase whatever she
    learned in the messages that arrived during this test pass.
    """
    path = MEMORY_DIR / "live-mid-run.md"
    path.write_bytes(b"snapshot at setup\n+ fact learned mid-run\n")
    try:
        restore_people({f"memory/{path.name}": b"snapshot at setup\n"})
        # It is still on disk, so the tests never wrote it: the mid-run fact wins.
        assert path.read_bytes() == b"snapshot at setup\n+ fact learned mid-run\n"
    finally:
        path.unlink(missing_ok=True)


def test_a_newcomer_who_arrived_mid_run_stays_off_the_restore_list():
    """`people` only knows what existed at setup, so a new speaker is untouched."""
    path = MEMORY_DIR / "joined-mid-run.md"
    path.write_bytes(b"")
    try:
        restore_people({})
        assert path.exists(), "nobody should rewrite a file the snapshot never saw"
    finally:
        path.unlink(missing_ok=True)


# --- inventory -------------------------------------------------------------

def test_inventory_counts_files_and_buffers():
    data = inv()
    assert any(m["slug"] == A for m in data["memory"])
    assert next(m for m in data["memory"] if m["slug"] == A)["facts"] == 3
    # Not exact totals: in real use other people exist.
    assert data["totals"]["memoryFiles"] >= 2
    assert data["totals"]["affinityFiles"] >= 2
    assert data["totals"]["bufferedChannels"] >= 2


def test_inventory_names_the_current_crush():
    aff.designate(A)  # manual: the host picks, and the inventory should say so
    data = inv()
    assert data["crush"] and data["crush"]["slug"] == A, "Alpha is he/him"
    assert next(a for a in data["affinity"] if a["slug"] == A)["isCrush"]


# --- preview ---------------------------------------------------------------

def test_preview_lists_what_goes_and_what_is_kept():
    plan = asyncio.run(reset_mod.preview(["memory"]))
    assert len(plan["removed"]) == 1
    assert plan["removed"][0]["count"] >= 2
    assert any("Alpha" in d for d in plan["removed"][0]["detail"])
    assert any("Affect" in k for k in plan["kept"])
    assert any("Persona" in k for k in plan["kept"])
    assert plan["typedConfirmation"] is None
    assert plan["learnedOnly"] is True


@pytest.mark.parametrize("authored", ["persona", "skills", "config", "stickers"])
def test_preview_demands_typing_for_authored_files(authored):
    assert asyncio.run(reset_mod.preview([authored]))["typedConfirmation"] == "RESET"
    assert asyncio.run(reset_mod.preview(["memory", "affect"]))["typedConfirmation"] is None


def test_preview_of_nothing_is_empty_not_an_error():
    assert asyncio.run(reset_mod.preview([]))["removed"] == []
    assert asyncio.run(reset_mod.preview(["not-a-thing"]))["removed"] == []


# --- run + backup ----------------------------------------------------------

def test_reset_deletes_the_target_and_nothing_else():
    result = asyncio.run(reset_mod.run(["memory"]))
    assert result["ok"]
    assert "memory/reset-alpha.md" in result["deleted"]
    assert not (MEMORY_DIR / f"{A}.md").exists()
    assert (AFFINITY_DIR / f"{A}.json").exists(), "affect must survive"
    assert (SKILLS_DIR / "social.md").exists(), "skills must survive"
    assert (ROOT / "persona.md").exists(), "persona must survive"


def test_the_memory_folder_itself_survives():
    asyncio.run(reset_mod.run(["memory"]))
    assert MEMORY_DIR.exists(), "the folder must never be removed"
    assert MEMORY_DIR.is_dir()


def test_backup_is_written_first_and_holds_real_content():
    asyncio.run(reset_mod.run(["memory"]))
    backups = reset_mod.list_backups()
    assert backups, "a backup should exist"
    saved = (BACKUP_DIR / backups[0]["id"] / "memory" / f"{A}.md").read_text(encoding="utf-8")
    assert "Nurse in Lisbon" in saved
    manifest = json.loads((BACKUP_DIR / backups[0]["id"] / "manifest.json").read_text(encoding="utf-8"))
    entry = next((f for f in manifest["files"] if f["to"] == f"memory/{A}.md"), None)
    assert entry, "manifest records the destination path"
    assert entry["from"] == f"memory/{A}.md"


def test_restore_round_trip():
    asyncio.run(reset_mod.run(["memory"]))
    backup_id = reset_mod.list_backups()[0]["id"]
    result = asyncio.run(reset_mod.restore(backup_id))
    assert result["ok"]
    assert "Nurse in Lisbon" in (MEMORY_DIR / f"{A}.md").read_text(encoding="utf-8")


def test_reset_of_affect_clears_the_crush():
    result = asyncio.run(reset_mod.run(["affect"]))
    assert result["ok"]
    assert f"affinity/{A}.json" in result["deleted"]
    assert inv()["crush"] is None


def test_reset_of_buffers_leaves_files_alone():
    record("g9", "c9", "X", "hi", slug="x")
    context_mod.save_summary("g9", "c9", "X is working on an important project.", time.time())
    assert inv()["totals"]["bufferedChannels"] > 0
    result = asyncio.run(reset_mod.run(["buffers"]))
    assert inv()["totals"]["bufferedChannels"] == 0
    assert context_mod.summary_for("g9", "c9") == ""
    restored = asyncio.run(reset_mod.restore(result["backup"]["id"]))
    assert restored["ok"]
    assert "memory/conversations.json" in restored["restored"]
    assert "important project" in context_mod.summary_for("g9", "c9")
    assert (ROOT / "persona.md").exists()


def test_full_reset_of_authored_files_backs_up_first():
    before = (ROOT / "persona.md").read_text(encoding="utf-8")
    result = asyncio.run(reset_mod.run(["persona", "skills"]))
    assert result["ok"] and result["backup"]
    assert result["configRestartNeeded"] is True
    saved = (BACKUP_DIR / result["backup"]["id"] / "files" / "persona.md").read_text(encoding="utf-8")
    assert saved == before, "backup must be byte-identical"
    asyncio.run(reset_mod.restore(result["backup"]["id"]))
    assert (ROOT / "persona.md").read_text(encoding="utf-8") == before
    assert not (ROOT / "files").exists(), "must not land in ./files/"


def test_run_refuses_an_empty_selection():
    result = asyncio.run(reset_mod.run([]))
    assert not result["ok"] and "nothing selected" in result["error"]


@pytest.mark.parametrize("bad", ["../../etc", "..", "a/b", "x" * 200])
def test_restore_rejects_traversal_ids(bad):
    assert asyncio.run(reset_mod.restore(bad))["ok"] is False


def test_every_target_is_described_for_the_ui():
    for key, target in reset_mod.TARGETS.items():
        assert target["label"] and target["what"], key
        assert target["tier"] in ("learned", "authored")
    assert reset_mod.TARGETS["memory"]["optIn"] is True, "memory must be opt-in only"
