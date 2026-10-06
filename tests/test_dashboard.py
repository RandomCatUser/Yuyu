
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from yuyu import guilds, mute
from yuyu.config import ROOT, config
from yuyu.dashboard.app import LOGO_DIR, TEMPLATE_DIR, create_app

TEMPLATE = TEMPLATE_DIR / "index.html"
EDITED_FILES = ("persona.md", "config.json", "stickers.json")
PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 32).decode()


@pytest.fixture
def client():
    saved = {rel: (ROOT / rel).read_text(encoding="utf-8") for rel in EDITED_FILES
             if (ROOT / rel).exists()}
    app = create_app()
    app.config["TESTING"] = True
    with app.test_client() as test_client:
        yield test_client
    for rel, content in saved.items():
        (ROOT / rel).write_text(content, encoding="utf-8")


# no auth at all

def test_the_shell_loads(client):
    res = client.get("/")
    assert res.status_code == 200
    assert "Yuyu - control" in res.get_data(as_text=True)


def test_the_api_is_open_with_no_credentials(client):
    text = client.get("/api/").get_data(as_text=True)
    assert "DISCORD_TOKEN" not in text, "the bot token name should not be in the panel"

    # The key *variable name* is config and is drawn on purpose; the *value*
    # must never travel. Assert every key value is absent from the snapshot.
    for name, value in os.environ.items():
        value = value.strip()
        if "API_KEY" in name.upper() and len(value) >= 8:
            assert value not in text, f"secret value for {name} leaked into the snapshot"

    files = sorted((ROOT / "private").glob("*.md"))
    if not files:
        pytest.skip("no private file to check")

    checked = 0
    for path in files:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            # Fragments occur by chance; a whole sentence verbatim is a real leak.
            if len(line) < 40:
                continue
            checked += 1
            assert line not in text, f"private line leaked into the snapshot: {line[:70]!r}"
    assert checked, "expected substantial lines in the private file to check"


def test_traversal_put_cannot_write_outside(client):
    marker = ROOT / "TRAVERSAL-CANARY.txt"
    res = client.put("/api/file/..%2FTRAVERSAL-CANARY.txt",
                     json={"content": "owned"})
    assert res.status_code in (400, 404)
    assert not marker.exists()


def test_missing_file_is_404_not_empty_200(client):
    assert client.get("/api/file/not-a-real-person.md").status_code == 404


# CRUD

def test_persona_round_trips(client):
    original = client.get("/api/named/persona").get_json()["content"]
    assert "Yuyu" in original
    edited = original + "\n<!-- test -->\n"
    assert client.put("/api/named/persona", json={"content": edited}).status_code == 200
    assert "<!-- test -->" in client.get("/api/named/persona").get_json()["content"]
    client.put("/api/named/persona", json={"content": original})
    assert client.get("/api/named/persona").get_json()["content"] == original


def test_invalid_json_is_rejected_before_it_can_break_the_bot(client):
    original = (ROOT / "stickers.json").read_text(encoding="utf-8")
    res = client.put("/api/named/stickers", json={"content": "{not json"})
    assert res.status_code >= 400
    json.loads((ROOT / "stickers.json").read_text(encoding="utf-8"))  # still valid


def test_non_string_content_is_refused(client):
    assert client.put("/api/named/persona", json={"content": {"evil": 1}}).status_code == 400
    assert client.put("/api/named/persona", json={}).status_code == 400


def test_malformed_body_does_not_crash(client):
    res = client.put("/api/named/persona", data="{broken",
                     content_type="application/json")
    assert res.status_code >= 400
    assert client.get("/api/").status_code == 200, "server still alive"


def test_skill_create_read_delete(client):
    name = f"dash-test-{__import__('os').getpid()}"
    try:
        assert client.post("/api/skill",
                           json={"name": name, "content": "---\nname: T\n---\n\nBody\n"}).status_code == 200
        got = client.get(f"/api/file/skills%2F{name}.md").get_json()["content"]
        assert "Body" in got
        assert client.delete(f"/api/file/skills%2F{name}.md").status_code == 200
    finally:
        (ROOT / "skills" / f"{name}.md").unlink(missing_ok=True)


@pytest.mark.parametrize("name", ["../evil", "a/b", "", "x" * 50, "a b"])
def test_bad_skill_names_are_refused(client, name):
    assert client.post("/api/skill", json={"name": name, "content": "x"}).status_code == 400


def test_memory_files_cannot_be_deleted_via_the_editor(client):
    # A bare name is ambiguous, so a destructive call names the folder.
    assert client.delete("/api/file/someone.md").status_code == 400
    assert client.delete("/api/file/memory%2Fsomeone.md").status_code == 400


# stickers

def test_sticker_upload_and_delete(client):
    res = client.post("/api/sticker", json={"name": "dash-test.png", "data": PNG})
    assert res.status_code == 200, res.get_data(as_text=True)
    assert (ROOT / "stickers" / "dash-test.png").exists()
    assert client.get("/stickers/dash-test.png").status_code == 200
    assert client.delete("/api/sticker/dash-test.png").status_code == 200
    assert not (ROOT / "stickers" / "dash-test.png").exists()


@pytest.mark.parametrize("payload", [
    {"name": "evil.exe", "data": PNG},
    {"name": "ok.png", "data": "!!!not base64!!!"},
    {"name": "ok.png", "data": ""},
])
def test_sticker_upload_validation(client, payload):
    assert client.post("/api/sticker", json=payload).status_code == 400


def test_sticker_delete_refuses_traversal(client):
    assert client.delete("/api/sticker/..%2F..%2F.env").status_code in (400, 404)


# provider logos
#
# One file per provider under templates/logos. Self-hosted, because the page
# is loopback-only and must render with no network. No artwork means a letter
# tile, not a broken image.

def test_every_shipped_logo_serves(client):
    files = sorted(p for p in LOGO_DIR.iterdir() if p.suffix in (".svg", ".png"))
    assert files, "no provider artwork found - the tile would never light up"
    for path in files:
        res = client.get(f"/logos/{path.name}")
        assert res.status_code == 200, f"{path.name} did not serve"
        want = "image/svg+xml" if path.suffix == ".svg" else "image/png"
        assert want in res.headers["Content-Type"], path.name
        assert res.headers["Cache-Control"], f"{path.name} is not cacheable"


@pytest.mark.parametrize("name", [
    "gemini.woff2", "index.html", "app.py", "gemini.svg.bak", "gemini", "..",
])
def test_logo_route_serves_only_images(client, name):
    assert client.get(f"/logos/{name}").status_code == 404


@pytest.mark.parametrize("name", [
    "../app.py", "..%2Fapp.py", "../../config.json", "..%5c..%5c.env",
    "....//....//config.json", "gemini.svg/../../app.py",
])
def test_logo_route_cannot_escape_its_folder(client, name):
    # A directory part is flattened to a basename, then has to exist in LOGO_DIR.
    res = client.get(f"/logos/{name}")
    assert res.status_code == 404, f"{name} was served"
    assert not res.get_data().lstrip().startswith(b"from __future__")


def test_a_provider_with_no_artwork_gets_a_clean_404_not_an_error(client):
    # The letter tile depends on this: a miss must be a 404, never a 500.
    for suffix in (".svg", ".png"):
        assert client.get(f"/logos/nosuchprovider{suffix}").status_code == 404


def test_the_model_view_asks_for_each_provider_logo_by_its_own_id(client):
    if shutil.which("node") is None:
        pytest.skip("node is not installed - cannot render the views")
    from dashboard_render import render_view

    snapshot = client.get("/api/").get_json()
    providers = snapshot["models"]["providers"]
    assert providers, "no providers configured to check"
    _, payload = render_view("model", snapshot, probe="logos")
    asked = payload.get("logos") or []
    for provider in providers:
        assert f"/logos/{provider['id']}.svg" in asked, (
            f"{provider['id']} has no logo request in the view; got {asked}")
    # One request per provider per mark drawn: the card plus the primary.
    assert len(asked) <= 2 * len(providers), f"too many logo requests: {asked}"


def test_a_hostile_provider_id_never_becomes_a_logo_url(client):
    if shutil.which("node") is None:
        pytest.skip("node is not installed - cannot render the views")
    from dashboard_render import render_view

    snapshot = json.loads(json.dumps(client.get("/api/").get_json()))
    snapshot["models"]["providers"].insert(0, {
        "id": "../../.env", "name": "Hostile", "model": "x",
        "baseUrl": "https://example.invalid/v1", "enabled": True, "keyCount": 1,
    })
    text, payload = render_view("model", snapshot, probe="logos")
    asked = payload.get("logos") or []
    assert not [s for s in asked if ".." in s or "%2f" in s.lower()], asked
    assert "/logos/hostile.svg" not in asked, asked
    assert "Hostile" in text, "the provider should still be listed, just not illustrated"


def test_the_panel_hot_links_nothing(client):
    # The logos are why: a CDN badge looks fine until the panel opens offline.
    html = client.get("/").get_data(as_text=True)
    remote = re.findall(r'(?:src|href)\s*=\s*["\'](?:https?:)?//[^"\']+', html)
    assert not remote, f"the page reaches out to the network: {remote}"


# usage: not inventing a number

def _usage_view_text(snapshot):
    from dashboard_render import render_view
    text, _ = render_view("usage", snapshot)
    return text


def _usage_day(offset_days: int = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=offset_days)).strftime("%Y-%m-%d")


def _usage_snapshot(client, **overrides):
    snapshot = json.loads(json.dumps(client.get("/api/").get_json()))
    usage = snapshot.setdefault("usage", {})
    totals = {
        "calls": 12, "prompt": 44632, "completion": 539, "total": 45171,
        "cached": 0, "reasoning": 0, "cost": 0.0, "unpriced": 12,
        "avgLatencyMs": 2948,
    }
    today = _usage_day()
    usage.update({
        "enabled": True,
        "days": 30,
        "keepDays": 90,
        "totals": totals,
        "derived": {
            "tokensPerCall": 3764.2, "outPerSecond": 14.16,
            "firstTs": 1791098678668.0, "lastTs": 1791124492710.0,
            "busiestHour": {"hour": 19, "calls": 6, "hoursActive": 4},
            "hoursActive": 4,
            "peakDay": {"day": today, "total": 45171},
            "pricedCalls": 0,
        },
        "byDay": [{"day": today, **totals}],
        "byProvider": [], "byModel": [], "bySource": [],
        "recent": [],
        "costComplete": False, "unpricedCalls": 12, "pricedModels": [],
    })
    usage.update(overrides)
    return snapshot


def test_cost_is_not_reported_as_zero_when_nothing_is_priced(client):
    if shutil.which("node") is None:
        pytest.skip("node is not installed - cannot render the views")
    snapshot = _usage_snapshot(client, pricedModels=[], costComplete=False)
    text = _usage_view_text(snapshot)

    assert "Cost is unknown, not zero" in text
    assert "unknown · no price set for 12 call(s)" in text
    assert "$0" not in text, "an unpriced window must not render a money figure"


def test_a_partly_priced_window_shows_the_real_floor_instead(client):
    if shutil.which("node") is None:
        pytest.skip("node is not installed - cannot render the views")
    snapshot = _usage_snapshot(
        client, pricedModels=["gemini-3.1-flash-lite"], costComplete=False)
    snapshot["usage"]["totals"]["cost"] = 0.0412
    snapshot["usage"]["totals"]["unpriced"] = 9
    snapshot["usage"]["derived"]["pricedCalls"] = 3
    text = _usage_view_text(snapshot)

    assert "The cost total is a floor" in text
    assert "floor — 9 of 12 call(s) had no price" in text
    assert "$0.0412" in text, "the priced calls' real total should still be shown"
    assert "Cost is unknown, not zero" not in text


def test_the_chart_does_not_pad_days_that_predate_recording(client):
    if shutil.which("node") is None:
        pytest.skip("node is not installed - cannot render the views")
    snapshot = _usage_snapshot(client)
    _, payload = render_usage_dom(client, snapshot)
    assert payload["columns"] == 1, (
        f"expected a single column for one recorded day, got {payload['columns']}")
    assert payload["empty"] == 0


def test_the_chart_still_keeps_quiet_days_inside_the_recorded_span(client):
    # A gap between real days stays visible; only the leading run is trimmed.
    if shutil.which("node") is None:
        pytest.skip("node is not installed - cannot render the views")
    snapshot = _usage_snapshot(client)
    base = snapshot["usage"]["totals"]
    snapshot["usage"]["byDay"] = [
        {"day": _usage_day(2), **base},
        {"day": _usage_day(), **base},
    ]
    _, payload = render_usage_dom(client, snapshot)
    assert payload["columns"] == 3, "two recorded days three apart span three columns"
    assert payload["empty"] == 1, "the quiet day in between should still be drawn"


def test_the_daily_table_prints_the_numbers_behind_the_bars(client):
    if shutil.which("node") is None:
        pytest.skip("node is not installed - cannot render the views")
    text = _usage_view_text(_usage_snapshot(client))
    assert "Every day" in text
    assert _usage_day() in text
    assert "44,632" in text or "45,171" in text, "the table should carry real token counts"


def test_the_rhythm_panel_shows_only_what_the_window_supports(client):
    if shutil.which("node") is None:
        pytest.skip("node is not installed - cannot render the views")
    text = _usage_view_text(_usage_snapshot(client))
    for label in ("tokens per call", "output tok/s", "busiest hour",
                  "first call", "last call", "priced calls"):
        assert label in text, f"missing {label!r}"
    # The output-rate label says what the number includes.
    assert "incl. prompt" in text

    # And a window too thin for an hourly pattern must not claim one.
    thin = _usage_snapshot(client)
    thin["usage"]["derived"]["busiestHour"] = None
    assert "busiest hour" not in _usage_view_text(thin)


def render_usage_dom(client, snapshot):
    html = TEMPLATE.read_text(encoding="utf-8")
    open_tag = '<script type="module">'
    start = html.index(open_tag) + len(open_tag)
    script = html[start: html.index("</script>", start)]

    snapshot = client.get("/api/").get_json()
    affect = snapshot["affinity"]
    assert affect["people"], "this test needs at least one person to read"

    # `affinity(v)` and `bondPanel(a)` both name their argument `a`.
    top = set(re.findall(r"\ba\.([\w]+)", script))
    assert {"people", "bond", "bondOn", "feelingsOn", "feelingNames",
            "feelingHalfLife", "bondThresholds", "crush", "crushOn",
            "eligible"} <= top

    # Each person's card reads these off the person object.
    person = affect["people"][0]
    card = {"slug", "name", "pronouns", "interactions", "crushEnabled", "warmth",
            "familiarity", "romance", "moodLabel", "balance", "streak",
            "history", "feelings"}
    assert card <= set(person), f"person is missing {card - set(person)}"
    assert set(person["feelings"]) == set(affect["feelingNames"])

    assert set(affect["bondThresholds"]) == {"low", "better", "minutes"}
    assert set(affect["bond"]) >= {"slug", "close", "mood", "moodWord", "caring",
                                   "reason", "minutesLeft", "checkIns", "lowThreshold"}

    # The feelings sliders come from the snapshot's names, one POST each.
    assert "a.feelingNames.map(name =>" in script
    assert "feelings: { [name]: v }" in script
    assert "/affinity/feelings" in script
    assert "/affinity/bond" in script


# the bond with whoever built her

@pytest.fixture
def bonded(affect, monkeypatch):
    """A record that is definitely the owner's."""
    from yuyu import bond as bond_mod

    monkeypatch.setitem(bond_mod.config["bond"], "ownerIds", ["1"])
    _turns(affect, "ray", "Ray")
    record = affect.load("ray")
    state = bond_mod.normalise(record)
    state["ownerSlug"] = "ray"
    state["ownerId"] = "1"
    affect.save(record)
    return affect


def test_the_bond_can_be_opened_and_edited_by_hand(client, bonded):
    assert bonded.owner_bond()["slug"] == "ray"
    res = client.post("/api/affinity/bond", json={"level": 40, "caring": True})
    assert res.status_code == 200, res.get_json()
    assert res.get_json()["bond"]["close"] == 40
    assert res.get_json()["bond"]["caring"] is True
    assert bonded.load("ray")["bond"]["level"] == 40


def test_the_bond_route_says_so_when_there_is_no_bond_yet(client, affect, monkeypatch):
    from yuyu import bond as bond_mod

    monkeypatch.setitem(bond_mod.config["bond"], "ownerIds", ["1"])
    _turns(affect, "ray", "Ray")
    assert affect.owner_bond() is None
    res = client.post("/api/affinity/bond", json={"level": 40})
    assert res.status_code == 400
    assert "no bond yet" in res.get_json()["error"]


def test_a_refused_bond_edit_changes_nothing(client, bonded):
    for body in ({"level": "very"}, {"mood": "sour"}, {}):
        res = client.post("/api/affinity/bond", json=body)
        assert res.status_code == 400, f"{body} should be refused: {res.get_json()}"
    assert bonded.load("ray")["bond"]["level"] == 0


def test_clearing_the_crush_clears_the_scores_not_just_the_mark(client, affect):
    _turns(affect, "ray", "Ray")
    assert affect.load("ray")["romance"] > 0

    res = client.post("/api/affinity/crush", json={"slug": ""})

    assert res.status_code == 200, res.get_json()
    records = affect.load_all()
    assert records and all(r["romance"] == 0 for r in records), \
        f"still scoring: {[(r['slug'], r['romance']) for r in records]}"
    assert res.get_json()["crush"] is None, "nothing for her to be acting on"


# the master crush switch

@pytest.fixture
def crush_master(monkeypatch):
    """Isolate the in-memory switch so one test cannot leave the crush off."""
    monkeypatch.setitem(config["affinity"], "crush", True)
    return config


def _crush_off(client):
    res = client.post("/api/affinity/crush-enabled", json={"enabled": False})
    assert res.status_code == 200, res.get_json()
    return res


def test_the_snapshot_says_whether_the_crush_is_on(client):
    body = client.get("/api/").get_json()
    assert "crushOn" in body["affinity"]
    assert body["affinity"]["crushOn"] is True


def test_the_master_switch_turns_the_crush_off_and_back(client, affect, crush_master):
    _turns(affect, "ray", "Ray")
    client.post("/api/affinity/crush", json={"slug": "ray"})
    assert affect.current_crush()["slug"] == "ray"

    res = _crush_off(client)
    assert res.get_json()["crushOn"] is False
    assert affect.crush_enabled() is False
    assert client.get("/api/").get_json()["affinity"]["crushOn"] is False

    res = client.post("/api/affinity/crush-enabled", json={"enabled": True})
    assert res.status_code == 200
    assert res.get_json()["crushOn"] is True
    assert affect.crush_enabled() is True


def test_switching_the_crush_off_clears_the_mark_not_the_scores(client, affect, crush_master):
    _turns(affect, "ray", "Ray")
    client.post("/api/affinity/crush", json={"slug": "ray"})
    record = affect.load("ray")
    romance, warmth = record["romance"], record["warmth"]
    assert record.get("isCrush") is True

    _crush_off(client)

    kept = affect.load("ray")
    assert kept.get("isCrush") is not True, "no stale mark on disk"
    assert kept["romance"] == romance and kept["warmth"] == warmth, "off is not a reset"


def test_a_crush_cannot_be_handed_over_while_switched_off(client, affect, crush_master):
    _turns(affect, "ray", "Ray")
    _crush_off(client)

    res = client.post("/api/affinity/crush", json={"slug": "ray"})

    assert res.status_code == 400, res.get_json()
    assert "switched off" in res.get_json()["error"]


def test_the_affect_panel_hides_crush_controls_when_switched_off(client):
    if shutil.which("node") is None:
        pytest.skip("node is not installed - cannot render the views")
    from dashboard_render import render_view

    snapshot = client.get("/api/").get_json()
    on_text, _ = render_view("affinity", snapshot)
    assert "She can fall for one person, chosen below." in on_text

    snapshot["affinity"]["crushOn"] = False
    off_text, _ = render_view("affinity", snapshot)
    assert "Off. Nobody is the crush" in off_text
    assert "She can fall for one person, chosen below." not in off_text


def test_the_model_panel_lists_every_configured_provider(client):
    """The panel must draw every provider in the snapshot, key or no key."""
    if shutil.which("node") is None:
        pytest.skip("node is not installed - cannot render the views")
    from dashboard_render import render_view

    snapshot = client.get("/api/").get_json()
    providers = snapshot["models"]["providers"]
    assert providers, "this test needs at least one provider configured"

    text, _ = render_view("model", snapshot)

    assert "No providers yet" not in text
    for provider in providers:
        assert (provider["name"] or provider["id"]) in text, \
            f"{provider['id']} is configured but not on the page"


# people: bulk

def test_a_selection_can_be_quietened_in_one_action(client, tmp_path, monkeypatch):
    monkeypatch.setattr(mute, "MUTED_FILE", tmp_path / "muted.json")
    mute.invalidate()
    try:
        res = client.post("/api/mute", json={"slugs": ["ray", "kim", "ray"], "muted": True})
        assert res.status_code == 200, res.get_json()
        assert mute.muted_slugs() == {"ray", "kim"}, "deduped, and both applied"

        # One bad entry applies none of it: a half-applied bulk toggle would
        # leave the panel describing a group that is not quiet.
        res = client.post("/api/mute", json={"slugs": ["ray", "a" * 200], "muted": False})
        assert res.status_code == 400
        assert mute.is_muted("ray") is True, "the rejected list must not have applied"
    finally:
        mute.invalidate()


def test_forgetting_one_person_leaves_the_others_alone(client, affect, tmp_path, monkeypatch):
    from yuyu import memory as mem
    from yuyu.dashboard import app as app_mod

    monkeypatch.setattr(mem, "MEMORY_DIR", tmp_path / "memory")
    monkeypatch.setattr(app_mod, "AFFINITY_DIR", affect.AFFINITY_DIR)
    _turns(affect, "ray", "Ray")
    _turns(affect, "kim", "Kim", times=5)
    mem.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    (mem.MEMORY_DIR / "ray.md").write_text("# ray", encoding="utf-8")
    (mem.MEMORY_DIR / "kim.md").write_text("# kim", encoding="utf-8")

    res = client.post("/api/people/forget", json={"slugs": ["ray"]})

    assert res.status_code == 200, res.get_json()
    assert res.get_json()["forgotten"] == ["ray"]
    assert not (mem.MEMORY_DIR / "ray.md").exists()
    assert (mem.MEMORY_DIR / "kim.md").exists(), "bulk must not mean everybody"
    assert not (affect.AFFINITY_DIR / "ray.json").exists(), \
        "a crush on someone she has been told to forget would be a strange leftover"
    assert (affect.AFFINITY_DIR / "kim.json").exists()


def test_forgetting_refuses_a_junk_or_missing_list(client):
    for body in ({}, {"slugs": []}, {"slugs": ["a" * 200]}, {"slugs": ["ok", "../../etc"]}):
        res = client.post("/api/people/forget", json=body)
        assert res.status_code == 400, f"{body} should be refused: {res.get_json()}"
