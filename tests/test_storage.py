"""Where things are kept on disk: cache/, tmp/, and how a file gets replaced.

    python -m pytest tests/test_storage.py -q

Every write that another thread might read mid-flight - config.json, the quiet
list, a memory file, an affect record - goes through `atomic_write`. The point
of the two folders is that derived data survives a restart (`cache/`) while
half-written data survives nothing at all (`tmp/`).
"""

from __future__ import annotations

import json

import yuyu.config as config_mod
from yuyu import mute, util


def test_the_folders_are_named_and_created(monkeypatch, tmp_path):
    monkeypatch.setattr(config_mod, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config_mod, "TMP_DIR", tmp_path / "tmp")

    assert config_mod.CACHE_DIR.name == "cache", "the panel and the docs both say cache/"
    assert config_mod.TMP_DIR.name == "tmp", "scratch lives in tmp/, not beside real data"

    config_mod.ensure_dirs()
    assert (tmp_path / "cache").is_dir()
    assert (tmp_path / "tmp").is_dir()


def test_a_write_stages_in_tmp_and_leaves_nothing_behind(monkeypatch, tmp_path):
    """The half-written copy must not sit next to the real file, and must not
    outlive the write."""
    stage_dir = tmp_path / "tmp"
    monkeypatch.setattr(config_mod, "TMP_DIR", stage_dir)
    target = tmp_path / "data" / "thing.json"

    util.atomic_write(target, json.dumps({"a": 1}))

    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 1}
    assert list(tmp_path.rglob("*.tmp")) == [], "a staging file survived the rename"


def test_a_write_on_another_volume_stages_beside_the_target(monkeypatch, tmp_path):
    """`os.replace` refuses to move a file across drives, so staging in tmp/
    would fail outright - and a rename that fails is worse than a scratch file
    in the wrong folder. Beside the target is still atomic."""
    class _OtherVolume:
        drive = "Z:"

    monkeypatch.setattr(config_mod, "TMP_DIR", _OtherVolume())
    target = tmp_path / "thing.json"

    util.atomic_write(target, "hello")

    assert target.read_text(encoding="utf-8") == "hello"
    assert list(tmp_path.glob("*.tmp")) == []


def test_a_replaced_file_never_exposes_the_old_one(monkeypatch, tmp_path):
    """Rewriting must leave the new contents, in one step, not a blend of both."""
    stage_dir = tmp_path / "tmp"
    monkeypatch.setattr(config_mod, "TMP_DIR", stage_dir)
    target = tmp_path / "thing.json"
    util.atomic_write(target, json.dumps({"v": 1}))

    util.atomic_write(target, json.dumps({"v": 2}))

    assert json.loads(target.read_text(encoding="utf-8")) == {"v": 2}
    assert list(stage_dir.glob("*")) == []


def test_the_quiet_list_stages_its_writes_in_tmp(monkeypatch, tmp_path):
    """This used to leave `muted.json.tmp` beside the real file, which read like
    a second, half-written switch list."""
    stage_dir = tmp_path / "tmp"
    monkeypatch.setattr(mute, "MUTED_FILE", tmp_path / "muted.json")
    monkeypatch.setattr(config_mod, "TMP_DIR", stage_dir)
    mute.invalidate()
    try:
        mute.set_muted("someone", True)
        on_disk = json.loads((tmp_path / "muted.json").read_text(encoding="utf-8"))
        assert on_disk["muted"] == ["someone"]
        assert list(tmp_path.glob("*.tmp")) == [], "a .tmp must not be left next to muted.json"
        assert list(stage_dir.glob("*")) == [], "and not left in tmp/ either"
    finally:
        mute.invalidate()


def test_conversation_summaries_live_with_learned_memory():
    from yuyu.config import MEMORY_DIR
    from yuyu.context import SUMMARY_FILE

    assert SUMMARY_FILE.parent == MEMORY_DIR, "conversation summaries belong with learned memory"
