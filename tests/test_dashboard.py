"""Flask dashboard: path safety, CRUD, affect editing, reset guards, the UI script.

Includes a parse check on the page's inline script, added after a single stray
`)` in the affinity view made the whole module fail to parse - the page rendered
but nothing responded, with no error anywhere to say why.
"""

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
    """The panel exactly as it runs locally: no password, no PIN.

    Some tests rewrite persona.md or config.json, so the originals go back
    afterwards instead of being left edited.
    """
    saved = {rel: (ROOT / rel).read_text(encoding="utf-8") for rel in EDITED_FILES
             if (ROOT / rel).exists()}
    app = create_app()
    app.config["TESTING"] = True
    with app.test_client() as test_client:
        yield test_client
    for rel, content in saved.items():
        (ROOT / rel).write_text(content, encoding="utf-8")


# --- no auth at all ---------------------------------------------------------

def test_the_shell_loads(client):
    res = client.get("/")
    assert res.status_code == 200
    assert "Yuyu - control" in res.get_data(as_text=True)


def test_the_api_is_open_with_no_credentials(client):
    """There is no password. Anything else here means one had crept back in."""
    res = client.get("/api/")
    assert res.status_code == 200
    data = res.get_json()
    assert "skills" in data and "affinity" in data
    for gone in ("authRequired", "admin", "sudo"):
        assert gone not in data, f"{gone} is the removed gate showing up again"


def test_nothing_refuses_an_unauthenticated_write(client):
    for path, body in (
        ("/api/model", {"model": "nope/nope"}),
        ("/api/mute", {"slug": "x", "muted": True}),
        ("/api/guilds/leave", {"id": "nope"}),
        ("/api/affinity/adjust", {"slug": "x", "warmth": 1}),
        ("/api/affinity/crush", {"slug": ""}),
    ):
        res = client.post(path, json=body)
        assert res.status_code != 401, f"{path} still wants a credential"


# --- path safety -----------------------------------------------------------

@pytest.mark.parametrize("evil", [
    "/api/file/..%2F..%2F.env",
    "/api/file/..%2F..%2F..%2FWindows%2Fwin.ini",
    "/api/file/....%2F%2F.env",
    "/api/file/..%5C..%5C.env",
    "/api/file/private%2Fowner.md",
    "/api/file/..%2Fprivate%2Fowner.md",
    "/api/file/affinity%2Fsomeone.json",
    "/api/file/notes.txt",
])
def test_traversal_and_non_markdown_are_refused(client, evil):
    res = client.get(evil)
    assert res.status_code in (400, 404), f"{evil} -> {res.status_code}"
    assert "DISCORD_TOKEN" not in res.get_data(as_text=True)
    assert "SECRET-OWNER-NAME" not in res.get_data(as_text=True)


def test_private_content_is_never_in_the_snapshot(client):
    """Only `!secret.me` may read private/*.md - never the dashboard.

    The check has to be on the file's CONTENT. The owner's display name is
    ordinary, deliberately public data: it lands in the affect table as soon as
    they say anything to her, and `!people` prints it anyway. Asserting on the
    name alone made this pass only for as long as affinity/ happened to be
    empty, then fail the moment real records existed - the opposite of what it
    was meant to protect.
    """
    text = client.get("/api/").get_data(as_text=True)
    assert "DISCORD_TOKEN" not in text, "the bot token name should not be in the panel"

    # A provider's key *variable name* is configuration and is drawn on purpose,
    # so the editor can say which .env entry holds the key. The *value* is what
    # must never travel: assert every non-trivial key value in the environment is
    # absent from the snapshot.
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
            # Headings and short fragments occur by chance; a whole sentence
            # from the private file appearing verbatim is a real leak.
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


# --- CRUD ------------------------------------------------------------------

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
    # A bare name is ambiguous, so a destructive call must name the folder.
    assert client.delete("/api/file/someone.md").status_code == 400
    assert client.delete("/api/file/memory%2Fsomeone.md").status_code == 400


# --- stickers --------------------------------------------------------------

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


# --- provider logos ---------------------------------------------------------
#
# One file per provider under templates/logos, named after the provider id. The
# page is loopback-only and has to render with no network, so these are
# self-hosted rather than hot-linked, and a provider we hold no artwork for falls
# back to a letter tile instead of a broken image.

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
    # Anything with a directory part is flattened to a basename and then has to
    # exist as a real file in LOGO_DIR, so no shape of this reaches the source.
    res = client.get(f"/logos/{name}")
    assert res.status_code == 404, f"{name} was served"
    assert not res.get_data().lstrip().startswith(b"from __future__")


def test_a_provider_with_no_artwork_gets_a_clean_404_not_an_error(client):
    # The letter tile depends on this: a miss has to be a plain 404 the page can
    # fall through, never a 500 that would leave a hole in the card.
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
    # One request per provider per mark drawn - the card plus whoever is primary.
    assert len(asked) <= 2 * len(providers), f"too many logo requests: {asked}"


def test_a_hostile_provider_id_never_becomes_a_logo_url(client):
    """The server flattens the path; the page must not even ask for one.

    Without the id guard the view would request /logos/../../.env.svg, which is
    harmless here only because the route happens to be closed. Better that the
    page cannot compose the request at all.
    """
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
    # The logos are the reason this matters: a CDN badge would look fine right up
    # until the panel is opened with no network.
    html = client.get("/").get_data(as_text=True)
    remote = re.findall(r'(?:src|href)\s*=\s*["\'](?:https?:)?//[^"\']+', html)
    assert not remote, f"the page reaches out to the network: {remote}"


# --- usage: not inventing a number ------------------------------------------

def _usage_view_text(snapshot):
    from dashboard_render import render_view
    text, _ = render_view("usage", snapshot)
    return text


def _usage_day(offset_days: int = 0) -> str:
    """A UTC day key, because that is how the recorder buckets them.

    Derived from the clock rather than written down: a literal date in this file
    would quietly stop matching the panel's window the day after it was added,
    and the failure would read as a chart bug.
    """
    return (datetime.now(timezone.utc) - timedelta(days=offset_days)).strftime("%Y-%m-%d")


def _usage_snapshot(client, **overrides):
    """A populated usage window, built rather than borrowed.

    The live `/api/` is no help here: the suite redirects USAGE_DIR to a temp
    folder, so its usage block is always empty and anything that defaulted to it
    would be testing the zero state by accident.
    """
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
    """The bug worth guarding: 12 unpriced calls summed to "$0".

    Zero is a claim - that the calls were free. Nobody knows that, so the tile
    has to say the answer is unknown instead of printing a number.
    """
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
    """A 30-day window on a same-day install used to draw 29 empty stubs.

    They are indistinguishable from a chart of activity at a glance, so the
    window now starts at the first recorded call.
    """
    if shutil.which("node") is None:
        pytest.skip("node is not installed - cannot render the views")
    snapshot = _usage_snapshot(client)
    _, payload = render_usage_dom(client, snapshot)
    assert payload["columns"] == 1, (
        f"expected a single column for one recorded day, got {payload['columns']}")
    assert payload["empty"] == 0


def test_the_chart_still_keeps_quiet_days_inside_the_recorded_span(client):
    # A gap between two real days is a real gap and should stay visible; only the
    # leading run is trimmed.
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
    # The honesty of the output-rate label: it says what the number includes.
    assert "incl. prompt" in text

    # And a window too thin for an hourly pattern must not claim one.
    thin = _usage_snapshot(client)
    thin["usage"]["derived"]["busiestHour"] = None
    assert "busiest hour" not in _usage_view_text(thin)


def render_usage_dom(client, snapshot):
    """Render usage and count the chart columns the DOM ended up with."""
    from dashboard_render import render_view

    _, payload = render_view("usage", snapshot, probe="usagechart")
    return _usage_view_text(snapshot), payload


# --- reset guards ----------------------------------------------------------

def test_reset_refuses_empty_selection(client):
    res = client.post("/api/reset/run", json={"targets": []})
    assert res.status_code == 400 and "nothing selected" in res.get_json()["error"]


def test_reset_will_not_delete_authored_files_without_confirmation(client):
    # The most important guard: a stray click must not take persona.md.
    before = client.get("/api/named/persona").get_json()["content"]
    for confirm in (None, "", "yes", "reseting", "delete", "  ", "rm -rf", "1"):
        body = {"targets": ["persona"]}
        if confirm is not None:
            body["confirm"] = confirm
        res = client.post("/api/reset/run", json=body)
        assert res.status_code == 400, f"confirm={confirm!r} -> {res.status_code}"
        assert res.get_json()["needs"] == "RESET"
    assert client.get("/api/named/persona").get_json()["content"] == before


def test_reset_preview_is_a_dry_run(client):
    before = client.get("/api/named/persona").get_json()["content"]
    res = client.post("/api/reset/preview",
                      json={"targets": ["memory", "affect", "persona"]})
    assert res.status_code == 200
    plan = res.get_json()
    assert plan["removed"] and plan["typedConfirmation"] == "RESET" and plan["kept"]
    assert client.get("/api/named/persona").get_json()["content"] == before


def test_snapshot_exposes_reset_targets_with_opt_in(client):
    data = client.get("/api/").get_json()
    targets = {t["key"]: t for t in data["resetTargets"]}
    assert targets["memory"]["optIn"] is True, "memory must be opt-in only"
    assert targets["buffers"]["optIn"] is False
    assert "memoryFiles" in data["totals"]


# --- CSRF: the origin check is all that is left ----------------------------

def test_a_cross_origin_write_is_refused(client):
    """No password means this is the only thing between a random website and
    her persona file."""
    before = client.get("/api/named/persona").get_json()["content"]
    res = client.put("/api/named/persona", json={"content": "hijacked"},
                     headers={"Origin": "https://evil.example.com"})
    assert res.status_code == 403
    assert client.get("/api/named/persona").get_json()["content"] == before


def test_same_origin_write_is_allowed(client):
    before = client.get("/api/named/persona").get_json()["content"]
    res = client.put("/api/named/persona", headers={"Origin": "http://127.0.0.1:7373"},
                     json={"content": before})
    assert res.status_code == 200


# --- usage -------------------------------------------------------------------

def test_the_snapshot_carries_usage(client):
    """The panel draws the usage tab from the one snapshot it already fetches."""
    data = client.get("/api/").get_json()
    assert "usage" in data, "the usage tab has nothing to draw from"
    assert "totals" in data["usage"] and "byDay" in data["usage"]


def test_the_usage_route_serves_the_window_that_was_asked_for(client):
    res = client.get("/api/usage?days=7")
    assert res.status_code == 200
    assert res.get_json()["days"] == 7


def test_an_absurd_window_snaps_to_the_nearest_real_one(client):
    """`?days=999999` must not make the panel read ten thousand files."""
    for asked, expected in (("999999", 365), ("0", 1), ("banana", 30), ("-5", 1)):
        res = client.get(f"/api/usage?days={asked}")
        assert res.get_json()["days"] == expected, f"days={asked} gave the wrong window"


def test_clearing_usage_is_a_write_and_leaves_config_alone(client, tmp_path, monkeypatch):
    """It only removes history. Prices live in config and must survive it."""
    from yuyu import usage as usage_mod

    monkeypatch.setattr(usage_mod, "USAGE_DIR", tmp_path / "usage")
    usage_mod.record("prov", "model", {"prompt_tokens": 10, "completion_tokens": 2})

    res = client.post("/api/usage/clear")
    assert res.status_code == 200
    assert res.get_json()["removed"] == 1
    assert usage_mod.summary(30)["totals"]["calls"] == 0
    assert "usage" in config and "prices" in config["usage"]


# --- the UI script ---------------------------------------------------------

def test_the_dashboard_script_parses():
    html = TEMPLATE.read_text(encoding="utf-8")
    open_tag = '<script type="module">'
    start = html.index(open_tag) + len(open_tag)
    code = html[start: html.index("</script>", start)]

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "extracted.mjs"
        path.write_text(code, encoding="utf-8")
        # The dashboard ships as one HTML file with its script inline, so the
        # only way to catch a syntax error is to hand that script to a JS
        # parser. `node --check` is that parser and nothing else - the bot
        # itself has no Node dependency - so skip when Node is not installed
        # rather than failing on a machine that never needed it.
        if shutil.which("node") is None:
            pytest.skip("node is not installed - cannot parse the inline script")
        result = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True)
    assert result.returncode == 0, f"the dashboard script does not parse:\n{result.stderr}"


def test_the_affect_editor_reflows_instead_of_scrolling_sideways():
    """It used to be a 760px-wide table. On a phone that meant a horizontal
    scrollbar inside every card, with half the numbers off-screen."""
    html = TEMPLATE.read_text(encoding="utf-8")
    assert ".affinity-table" not in html, "the wide table is back"
    # Every knob row is a grid that is allowed to reflow, and none is fixed wide.
    assert ".knob {" in html
    assert "min-width" not in html.split(".affect-card")[1].split(".boot")[0]
    assert 'className: "feelings-grid"' in html
    assert re.search(r"\.feelings-grid\s*\{[^}]*auto-fit", html)


def test_dashboard_has_no_decorative_background_image():
    html = TEMPLATE.read_text(encoding="utf-8")
    assert ".doc-body::before" not in html
    assert "i.pinimg.com" not in html


def test_there_is_no_login_gate_left_in_the_markup():
    """The panel used to open on a password box that hid the app until you
    typed one - including on a dashboard where no password was set, so the page
    sat there doing nothing."""
    html = TEMPLATE.read_text(encoding="utf-8")
    open_tag = '<script type="module">'
    start = html.index(open_tag) + len(open_tag)
    code = html[start: html.index("</script>", start)]

    assert 'id="gate"' not in html, "the gate is still in the markup"
    for gone in ('id="unlock"', 'id="logout"', 'id="lockrow"', "prompt(",
                 "X-Admin-Pin", "Authorization", "sessionStorage"):
        assert gone not in html and gone not in code, f"{gone} is the removed gate showing up again"
    # Nothing may sit between the page loading and the panel being usable.
    boot = code[code.index('api("/").then'):]
    assert re.search(r'api\("/"\)\.then\(s => \{\s*STATE = s;', boot)
    assert boot.index("STATE = s") < boot.index("enter();"), "the gate came back"


def test_the_page_says_something_while_it_loads():
    """Nothing to unlock means the old page sat on a password box that could
    never be satisfied. It needs a visible loading state instead."""
    html = TEMPLATE.read_text(encoding="utf-8")
    assert 'id="boot"' in html
    assert "<title>Yuyu - control</title>" in html


def test_every_element_the_script_uses_exists_in_the_markup():
    html = TEMPLATE.read_text(encoding="utf-8")
    open_tag = '<script type="module">'
    start = html.index(open_tag) + len(open_tag)
    code = html[start: html.index("</script>", start)]
    ids = set(re.findall(r'\$\("#([\w-]+)"\)', code))
    missing = [i for i in ids if f'id="{i}"' not in html]
    assert not missing, f"markup missing {missing}"


def test_snapshot_paths_the_script_reads_all_exist(client):
    html = TEMPLATE.read_text(encoding="utf-8")
    open_tag = '<script type="module">'
    start = html.index(open_tag) + len(open_tag)
    code = html[start: html.index("</script>", start)]
    snapshot = client.get("/api/").get_json()

    for path in sorted(set(re.findall(r"STATE\.([\w.]+)", code))):
        segments = path.split(".")
        current = snapshot
        for index, segment in enumerate(segments):
            if current is None:
                # A nullable field is fine if the script guards it.
                previous = segments[index - 1]
                guarded = (f"?.{previous}" in code or f"{previous} ?" in code
                           or f"{previous}?." in code)
                assert guarded, f"STATE.{path} is null and {previous} is not guarded"
                break
            if segment not in current:
                # Past this point we are in JS-land (e.g. eligible.join), which is
                # not a snapshot field. Only a dict parent means a real rename.
                if isinstance(current, dict):
                    pytest.fail(f"snapshot is missing STATE.{path} (no {segment!r})")
                break
            current = current[segment]


# --- the page actually renders (node DOM stub) -----------------------------
#
# `node --check` only proves the script parses. These run it against a fake DOM
# and look at what lands on the page, which is the only way to catch a view that
# throws halfway through, an editor that loses what you typed, or a reset card
# that vanishes before it can be read.

EXPECTED_TABS = {
    "overview", "model", "usage", "presence", "servers", "affinity", "people",
    "skills", "persona", "stickers", "reset", "logs", "config",
}


def _inline_code(html: str) -> str:
    start = html.index('<script type="module">') + len('<script type="module">')
    return html[start: html.index("</script>", start)]


def test_the_tab_list_matches_the_views_that_exist():
    code = _inline_code(TEMPLATE.read_text(encoding="utf-8"))
    block = code[code.index("const TABS = ["):]
    tabs = set(re.findall(r'\["(\w+)"', block[: block.index("];")]))
    assert tabs == EXPECTED_TABS, f"tabs changed: {sorted(tabs ^ EXPECTED_TABS)}"


def test_every_tab_renders_without_throwing(client):
    if shutil.which("node") is None:
        pytest.skip("node is not installed - cannot render the views")
    from dashboard_render import render_views

    snapshot = client.get("/api/").get_json()
    pages = render_views(sorted(EXPECTED_TABS), snapshot)
    threw = {view: text for view, text in pages.items() if text.startswith("THREW:")}
    assert not threw, "these views threw while rendering:\n" + "\n".join(
        f"  {view}: {text.splitlines()[0]}" for view, text in threw.items())
    empty = [view for view, text in pages.items() if not text.strip()]
    assert not empty, f"these views rendered an empty page: {empty}"


def test_the_editor_keeps_what_was_typed_through_a_re_render(client):
    """A background refresh used to build a second editor for the same file and
    the slower fetch finished second, wiping whatever was being typed."""
    if shutil.which("node") is None:
        pytest.skip("node is not installed - cannot render the editor")
    from dashboard_render import render_view

    snapshot = client.get("/api/").get_json()
    _, payload = render_view("persona", snapshot, probe="editor")
    assert payload["built"] is True, "no editor was built for persona.md"
    assert payload["kept"] == "half typed", "the editor lost the unsaved text"


def test_the_reset_outcome_survives_re_rendering(client):
    if shutil.which("node") is None:
        pytest.skip("node is not installed - cannot render the reset view")
    from dashboard_render import render_view

    snapshot = client.get("/api/").get_json()
    _, payload = render_view("reset", snapshot, probe="reset")
    text = payload["text"]
    assert "affinity/atulyakant.json" in text, "the done card did not list what was deleted"
    assert "Dismiss" in text, "there is no way to dismiss the done card"


def test_every_class_the_panel_draws_has_a_rule_and_none_is_dead():
    """A redesign renames things. This catches a class renamed in the script but
    not the stylesheet, and a rule left behind pointing at nothing."""
    html = TEMPLATE.read_text(encoding="utf-8")
    code = _inline_code(html)
    raw_style = html[html.index("<style>") + 7: html.index("</style>")]
    style = re.sub(r"/\*.*?\*/", "", raw_style, flags=re.S)
    markup = html[: html.index("<script")]
    rest = code + markup  # everything that is not the stylesheet

    def literals(text):
        return [m.group(1)[1:-1] for m in re.finditer(
            r'class(?:Name)?\s*:\s*(`[^`]*`|"[^"]*"|\'[^\']*\')', text)]

    used = set()
    for literal in literals(code):
        used.update(re.findall(r"[A-Za-z][\w-]*", re.sub(r"\$\{.*?\}", " ", literal)))
    for m in re.finditer(r'class="([^"]+)"', markup):
        used.update(m.group(1).split())
    for m in re.finditer(r"classList\.(?:add|remove)\('([\w -]+)'\)", code):
        used.update(m.group(1).split())

    styled = set()
    for m in re.finditer(r"([^{}]+)\{", style):
        for selector in m.group(1).split(","):
            if selector.strip().startswith("@"):
                continue
            styled.update(re.findall(r"\.([\w-]+)", selector))

    unstyled = sorted(c for c in used if c not in styled)
    assert not unstyled, f"these classes are drawn but have no rule: {unstyled}"

    # Applied from data rather than written as a literal: the log level comes
    # back from the server, so no amount of scanning the file will see it.
    dynamic = {"error"}
    dead = sorted(
        c for c in styled
        if c not in used and c not in dynamic
        and not re.search(rf"(?<![\w-]){re.escape(c)}(?![\w-])", rest))
    assert not dead, f"these CSS rules are never drawn (dead): {dead}"


# --- model switching -------------------------------------------------------

CATALOGUE = [
    {"id": "openai/gpt-5.4-nano", "title": "GPT-5.4 Nano", "description": "default",
     "owned_by": "OpenAI", "category": "text", "chat": True,
     "context_length": 400000, "success_rate": 99.9, "status": "healthy", "reasoning": True},
    {"id": "openai/gpt-5.4-mini", "title": "GPT-5.4 Mini", "description": "smaller",
     "owned_by": "OpenAI", "category": "text", "chat": True,
     "context_length": 200000, "success_rate": 99.5, "status": "healthy", "reasoning": True},
    {"id": "black-forest-labs/flux.1-schnell", "title": "FLUX.1 schnell", "description": "image",
     "owned_by": "Black Forest Labs", "category": "image", "chat": False,
     "context_length": 0, "success_rate": 98.0, "status": "healthy", "reasoning": False},
]


@pytest.fixture
def catalogue(monkeypatch):
    """Keep the tests off the real /v1/models endpoint."""
    monkeypatch.setattr("yuyu.dashboard.app.models_snapshot", lambda: list(CATALOGUE))
    monkeypatch.setitem(config["model"], "default", CATALOGUE[0]["id"])
    return CATALOGUE


def test_model_change_applies_immediately(client, catalogue):
    before = config["model"]["default"]
    config["model"]["default"] = "openai/gpt-5.4-nano"
    try:
        res = client.post("/api/model", json={"model": "openai/gpt-5.4-mini"})
        assert res.status_code == 200
        assert res.get_json()["model"] == "openai/gpt-5.4-mini"
        assert config["model"]["default"] == "openai/gpt-5.4-mini", "must take effect without a restart"
    finally:
        config["model"]["default"] = before


def test_the_snapshot_offers_every_model_and_flags_the_chat_ones(client, catalogue):
    data = client.get("/api/").get_json()
    choices = data["models"]["choices"]
    ids = {m["id"] for m in choices}
    # Every configured provider is selectable in the dashboard.
    assert ids == {m["id"] for m in catalogue}
    by_id = {m["id"]: m for m in choices}
    assert by_id["openai/gpt-5.4-mini"]["chat"] is True
    assert by_id["black-forest-labs/flux.1-schnell"]["chat"] is False


def test_any_model_can_be_selected_not_just_the_chat_ones(client, catalogue):
    """You asked for any model, not only the chat-capable ones - so a non-chat
    pick must not be rejected, even though the UI labels it as one that will
    not answer in conversation."""
    before = config["model"]["default"]
    try:
        res = client.post("/api/model",
                          json={"model": "black-forest-labs/flux.1-schnell"})
        assert res.status_code == 200, res.get_json()
        assert config["model"]["default"] == "black-forest-labs/flux.1-schnell"
    finally:
        config["model"]["default"] = before


def test_model_change_rejects_a_model_that_is_not_in_the_catalogue(client, catalogue):
    before = config["model"]["default"]
    try:
        res = client.post("/api/model", json={"model": "nope/not-a-model"})
        assert res.status_code == 400
        assert res.get_json()["error"] == "unknown model"
        assert config["model"]["default"] == before
    finally:
        config["model"]["default"] = before


def test_model_change_rejects_a_malformed_id(client, catalogue):
    before = config["model"]["default"]
    try:
        for bad in ("", "  ", "model with spaces", "a" * 200, "x;y"):
            assert client.post("/api/model", json={"model": bad}).status_code == 400
        assert config["model"]["default"] == before
    finally:
        config["model"]["default"] = before


def test_provider_model_can_be_edited_from_dashboard(client):
    provider = next(p for p in config["model"]["providers"] if p["id"] == "gemini")
    original = provider["model"]
    try:
        res = client.post(
            "/api/provider-model",
            json={"provider": "gemini", "model": "gemini-2.5-flash-lite"},
        )
        assert res.status_code == 200, res.get_json()
        assert res.get_json()["model"] == "gemini-2.5-flash-lite"
        updated = next(p for p in config["model"]["providers"] if p["id"] == "gemini")
        assert updated["model"] == "gemini-2.5-flash-lite"

        saved = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        gemini = next(p for p in saved["model"]["providers"] if p["id"] == "gemini")
        assert gemini["model"] == "gemini-2.5-flash-lite"
    finally:
        from yuyu.config import set_provider_model

        set_provider_model("gemini", original)


def test_provider_model_edit_needs_no_credential(client, monkeypatch):
    import yuyu.dashboard.app as dashboard

    monkeypatch.setattr(dashboard, "set_provider_model", lambda _provider, model: model)
    res = client.post(
        "/api/provider-model",
        json={"provider": "gemini", "model": "gemini-2.5-flash-lite"},
    )
    assert res.status_code == 200


def test_provider_model_edit_rejects_unknown_or_malformed_values(client):
    for body in (
        {"provider": "unknown", "model": "valid-model"},
        {"provider": "gemini", "model": ""},
        {"provider": "gemini", "model": "invalid model name"},
    ):
        res = client.post("/api/provider-model", json=body)
        assert res.status_code == 400


def test_ordinary_reads_need_nothing(client, catalogue):
    """Ordinary reads need nothing at all."""
    assert client.get("/api/").status_code == 200
    assert client.get("/api/models").status_code == 200


def test_persona_and_config_writes_go_through(client, monkeypatch, tmp_path):
    from yuyu.dashboard import app as dashboard

    for key, content in (("persona", "updated persona"), ("config", "{}")):
        target = tmp_path / f"{key}.json"
        monkeypatch.setitem(dashboard.NAMED_FILES, key, lambda target=target: target)
        res = client.put(f"/api/named/{key}", json={"content": content})
        assert res.status_code == 200, res.get_json()
        assert target.read_text(encoding="utf-8") == content


def test_the_quiet_switch_works(client, tmp_path, monkeypatch):
    monkeypatch.setattr(mute, "MUTED_FILE", tmp_path / "muted.json")
    mute.invalidate()
    try:
        res = client.post("/api/mute", json={"slug": "mewo", "muted": True})
        assert res.status_code == 200, res.get_json()
        assert res.get_json()["isMuted"] is True
        assert mute.is_muted("mewo") is True
        on_disk = json.loads(mute.MUTED_FILE.read_text(encoding="utf-8"))
        assert "mewo" in on_disk["muted"], "must still be quiet after a restart"

        # Switching them back on takes them off the list for good.
        res = client.post("/api/mute", json={"slug": "mewo", "muted": False})
        assert res.get_json()["isMuted"] is False
        assert mute.muted_slugs() == set()

        # A stray body must not silently quiet someone.
        assert client.post("/api/mute", json={"slug": "mewo"}).status_code == 400
        assert client.post("/api/mute", json={}).status_code == 400
        assert client.post("/api/mute", json={"slug": "a" * 200, "muted": True}).status_code == 400
    finally:
        mute.invalidate()


def test_the_snapshot_reports_who_is_quiet(client, tmp_path, monkeypatch):
    """The switch is drawn from this list, so a missing key is a dead button."""
    monkeypatch.setattr(mute, "MUTED_FILE", tmp_path / "muted.json")
    mute.invalidate()
    try:
        data = client.get("/api/").get_json()
        assert data["muted"] == []

        mute.set_muted("mewo", True)
        data = client.get("/api/").get_json()
        assert data["muted"] == ["mewo"]
    finally:
        mute.invalidate()


# --- servers: see them, and leave one --------------------------------------

def test_the_snapshot_lists_her_servers(client):
    """The whole Servers view is drawn from this, so a missing key is a blank tab."""
    guilds.detach()
    data = client.get("/api/").get_json()
    assert isinstance(data["guilds"], list)


def test_leaving_a_server_needs_no_credential(client):
    guilds.detach()
    res = client.post("/api/guilds/leave", json={"id": "123456789"})
    assert res.status_code == 409


def test_leaving_refuses_a_junk_id(client):
    guilds.detach()
    for bad in ("not an id", "", "12e4", "1" * 40):
        res = client.post("/api/guilds/leave", json={"id": bad})
        assert res.status_code == 400, f"{bad!r} should be rejected: {res.get_json()}"
    res = client.post("/api/guilds/leave", json={})
    assert res.status_code == 400


def test_leaving_somewhere_she_is_not_is_a_conflict_not_a_crash(client):
    """409, not 500: the request was well formed, she is just not leaving."""
    guilds.detach()
    res = client.post("/api/guilds/leave", json={"id": "999999999"})
    assert res.status_code == 409
    body = res.get_json()
    assert "error" in body and "not connected" in body["error"]
    assert body["guilds"] == [], "the panel still needs a list to redraw from"


def test_skills_and_memory_stay_editable(client, tmp_path):
    """Skills and memory stay editable."""
    from yuyu.config import SKILLS_DIR

    path = SKILLS_DIR / "panel-probe.md"
    path.write_text("probe", encoding="utf-8")
    try:
        res = client.put("/api/file/skills/panel-probe.md", json={"content": "edited"})
        assert res.status_code == 200
        assert path.read_text(encoding="utf-8") == "edited"
    finally:
        path.unlink(missing_ok=True)


def test_reset_actions_go_through(client, monkeypatch):
    from yuyu.dashboard import app as dashboard

    monkeypatch.setattr(dashboard, "_preview", lambda _targets: {
        "removed": ["memory"], "typedConfirmation": "",
    })
    monkeypatch.setattr(dashboard, "_run", lambda targets: {"ok": True, "targets": targets})
    monkeypatch.setattr(dashboard, "_restore", lambda backup_id: {"ok": True, "id": backup_id})
    assert client.post("/api/reset/run", json={"targets": ["memory"]}).status_code == 200
    assert client.post("/api/reset/restore", json={"id": "test-backup"}).status_code == 200

# --- rich presence ----------------------------------------------------------

def test_the_snapshot_carries_her_presence(client):
    """The whole Presence card is drawn from this, so a missing key is a dead editor."""
    data = client.get("/api/").get_json()
    assert "presence" in data, "the panel needs both what is set and what Discord would show"
    assert "config" in data["presence"] and "preview" in data["presence"]
    assert data["presence"]["preview"]["name"], "an unnamed activity renders as no presence"
    html = TEMPLATE.read_text(encoding="utf-8")
    assert 'href="#presence" data-nav="presence">Presence</a>' in html
    assert 'className: "presence-preview"' in html
    assert 'field("Large image asset key"' in html


def test_presence_route_needs_no_credential(client, monkeypatch):
    import yuyu.dashboard.app as dashboard

    monkeypatch.setattr(dashboard, "set_presence", lambda _body: {})
    res = client.post("/api/presence", json={})
    assert res.status_code == 200


def test_editing_presence_saves_it_and_previews_what_would_show(client, monkeypatch):
    """Written to config.json *and* reflected in the preview: patching only the
    file would need a restart, which is the whole point of the panel."""
    import yuyu.config as config_mod
    from yuyu import presence

    written = {}
    monkeypatch.setattr(config_mod, "_patch_config",
                        lambda s, k, v: written.update({(s, k): v}))
    monkeypatch.setitem(config_mod.config["bot"], "presence",
                        dict(config_mod.DEFAULTS["bot"]["presence"]))
    presence.detach()

    res = client.post("/api/presence", json={
        "status": "idle", "type": "listening", "name": "Yuyu",
        "details": "around", "state": "!help",
        "largeImage": "yuyu_main", "largeText": "Yuyu online",
        "smallImage": "green_dot", "smallText": "Available",
    })

    assert res.status_code == 200, res.get_json()
    body = res.get_json()
    assert body["ok"] is True
    assert body["config"]["type"] == "listening"
    assert written[("bot", "presence")]["type"] == "listening", "and it survives a restart"
    assert body["config"]["largeImage"] == "yuyu_main"
    assert body["config"]["smallImage"] == "green_dot"
    assert body["preview"]["largeText"] == "Yuyu online"
    assert body["preview"]["type"] == "listening"
    assert body["applied"] is False, "she is not connected here, so say so rather than claim it"


def test_the_presence_editor_refuses_what_discord_cannot_show(client, monkeypatch):
    """Fail before writing, or the panel reports a saved edit she will never display."""
    import yuyu.config as config_mod

    written = []
    monkeypatch.setattr(config_mod, "_patch_config", lambda *a: written.append(a))
    before = dict(config_mod.config["bot"]["presence"])

    for bad in ({"status": "purple"}, {"type": "flying"},
                {"status": "online", "type": "playing", "name": "x" * 500},
                {"largeImage": "a" * 129}, {"smallImage": "b" * 129}):
        res = client.post("/api/presence", json=bad)
        assert res.status_code == 400, f"{bad} should be refused: {res.get_json()}"

    assert config_mod.config["bot"]["presence"] == before, "nothing half-written"
    assert written == [], "a refused edit must not reach config.json"

# --- feelings panel: hand-editing one person --------------------------------

GOOD_TURN = dict(addressed=True, directReply=True, askedAboutHer=True, sharedTopics=3,
                 revealedSomething=True, lateNight=True, gratitude=False, harsh=False,
                 commanded=False, ignored=False)


@pytest.fixture
def affect(monkeypatch, tmp_path):
    """Real records, in a scratch directory rather than the live ones."""
    from yuyu import affinity as aff
    monkeypatch.setattr(aff, "AFFINITY_DIR", tmp_path / "affinity")
    aff.invalidate()
    yield aff
    aff.invalidate()


def _turns(aff, slug, name, pronouns="he/him", times=25):
    for _ in range(times):
        aff.apply_turn(slug, name, pronouns, dict(GOOD_TURN))


def test_feelings_controls_need_no_credential(client, monkeypatch):
    from yuyu.dashboard import app as dashboard

    monkeypatch.setattr(dashboard, "release_crush_affinity", lambda: None)
    for path, body in (
        ("/api/affinity/adjust", {}),
        ("/api/affinity/crush", {}),
        ("/api/affinity/reset", {}),
        ("/api/people/forget", {}),
    ):
        res = client.post(path, json=body)
        assert res.status_code != 403, f"{path} unexpectedly remained locked"


def test_setting_a_feeling_moves_it_and_is_written_back(client, affect):
    _turns(affect, "ray", "Ray")
    res = client.post("/api/affinity/adjust",
                      json={"slug": "ray", "warmth": -80, "romance": 5})
    assert res.status_code == 200, res.get_json()
    ray = next(p for p in res.get_json()["people"] if p["slug"] == "ray")
    assert (ray["warmth"], ray["romance"]) == (-80, 5)
    assert affect.load("ray")["warmth"] == -80, "on disk, not just in the reply"


def test_a_refused_adjust_changes_nothing(client, affect):
    _turns(affect, "ray", "Ray")
    for body in ({"slug": "nobody-here", "warmth": 10},
                 {"slug": "ray", "warmth": "warm"},
                 {"slug": "ray"}):
        res = client.post("/api/affinity/adjust", json=body)
        assert res.status_code == 400, f"{body} should be refused: {res.get_json()}"
    assert affect.load("ray")["warmth"] > 0, "the real score is untouched"


def test_the_crush_can_be_handed_to_someone_on_purpose(client, affect):
    _turns(affect, "ray", "Ray")  # the incumbent
    _turns(affect, "kim", "Kim", times=5)

    res = client.post("/api/affinity/crush", json={"slug": "kim"})

    assert res.status_code == 200, res.get_json()
    body = res.get_json()
    assert body["crush"]["slug"] == "kim"
    assert body["crush"]["romance"] >= affect.config["affinity"]["romanceThreshold"], \
        "a crush she is not acting on would be a lie on the panel"
    assert affect.load("ray").get("isCrush") is not True, "only ever one"


def test_handing_over_the_crush_opts_a_ruled_out_person_back_in(client, affect):
    _turns(affect, "ray", "Ray")
    record = affect.load("ray")
    record["crushEnabled"] = False
    record["romance"] = 0
    affect.save(record)

    res = client.post("/api/affinity/crush", json={"slug": "ray"})

    assert res.status_code == 200, res.get_json()
    assert res.get_json()["crush"]["slug"] == "ray"
    assert affect.load("ray")["crushEnabled"] is True, "picking them outright rules them back in"


def test_handing_over_the_crush_refuses_ineligible_pronouns(client, affect):
    _turns(affect, "ana", "Ana", pronouns="she/her")

    res = client.post("/api/affinity/crush", json={"slug": "ana"})

    assert res.status_code == 400
    assert "only falls for" in res.get_json()["error"]


def test_resetting_the_admin_crush_clears_it(client, affect):
    _turns(affect, "ray", "Ray")
    client.post("/api/affinity/crush", json={"slug": "ray"})
    assert affect.current_crush()["slug"] == "ray"

    res = client.post("/api/affinity/reset", json={"slug": "ray"})

    assert res.status_code == 200, res.get_json()
    assert res.get_json()["crush"] is None, "a reset crush is gone, not handed on"
    assert affect.load("ray").get("isCrush") is not True


def test_opting_the_admin_crush_out_clears_it_and_persists(client, affect):
    _turns(affect, "ray", "Ray")
    client.post("/api/affinity/crush", json={"slug": "ray"})
    assert affect.current_crush()["slug"] == "ray"

    res = client.post("/api/affinity/toggle", json={"slug": "ray"})

    assert res.status_code == 200, res.get_json()
    assert res.get_json()["crush"] is None
    assert affect.load("ray")["crushEnabled"] is False
    assert affect.load("ray").get("isCrush") is not True, "no stale mark on disk"


# --- feelings: what she feels right now -------------------------------------

def test_a_feeling_can_be_set_by_hand_and_is_written_back(client, affect):
    _turns(affect, "ray", "Ray")
    res = client.post("/api/affinity/feelings",
                      json={"slug": "ray", "feelings": {"happy": 71, "tired": 30}})
    assert res.status_code == 200, res.get_json()
    ray = next(p for p in res.get_json()["people"] if p["slug"] == "ray")
    assert ray["feelings"]["happy"] == 71
    assert ray["feelings"]["tired"] == 30
    assert affect.load("ray")["feelings"]["happy"] == 71, "on disk, not just in the reply"


def test_a_feeling_the_panel_made_up_is_refused(client, affect):
    _turns(affect, "ray", "Ray")
    res = client.post("/api/affinity/feelings",
                      json={"slug": "ray", "feelings": {"vibecount": 50}})
    assert res.status_code == 400
    assert "feelings are" in res.get_json()["hint"]


@pytest.mark.parametrize("body", [
    {"slug": "ray", "feelings": {}},
    {"slug": "ray", "feelings": {"happy": "loads"}},
    {"slug": "ray", "feelings": "happy"},
    {"slug": "nobody-here", "feelings": {"happy": 10}},
])
def test_a_refused_feeling_changes_nothing(client, affect, body):
    _turns(affect, "ray", "Ray")
    before = affect.load("ray")["feelings"]
    res = client.post("/api/affinity/feelings", json=body)
    assert res.status_code == 400, f"{body} should be refused: {res.get_json()}"
    assert affect.load("ray")["feelings"] == before


def test_the_snapshot_carries_every_feeling_name_and_the_bond(client):
    """The Affect panel is drawn entirely from these, so a missing key is a
    dead editor."""
    from yuyu import feelings as feelings_mod

    data = client.get("/api/").get_json()
    assert data["affinity"]["feelingNames"] == list(feelings_mod.FEELINGS)
    assert set(data["affinity"]["feelingWords"]) == set(feelings_mod.FEELINGS)
    assert data["affinity"]["feelingHalfLife"] > 0
    assert "bond" in data["affinity"] and "bondOn" in data["affinity"]


def test_every_key_the_affect_panel_reads_is_in_the_snapshot(client, bonded):
    """The snapshot check above only walks literal `STATE.x` paths. The Affect
    view passes its half of the state down as plain arguments - `affinity(v)`
    reads `a.people`, `bondPanel(a)` reads `a.bond` - so none of it was covered
    and a rename would have shipped as a blank panel instead of a failing build.
    """
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

    # The feelings sliders are built from the snapshot's names, one POST each.
    assert "a.feelingNames.map(name =>" in script
    assert "feelings: { [name]: v }" in script
    assert "/affinity/feelings" in script
    assert "/affinity/bond" in script


# --- the bond with whoever built her ----------------------------------------

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
    """Leftover scores would elect the very same person again on the next read,
    so 'clear' would not have cleared a thing."""
    _turns(affect, "ray", "Ray")
    assert affect.load("ray")["romance"] > 0

    res = client.post("/api/affinity/crush", json={"slug": ""})

    assert res.status_code == 200, res.get_json()
    records = affect.load_all()
    assert records and all(r["romance"] == 0 for r in records), \
        f"still scoring: {[(r['slug'], r['romance']) for r in records]}"
    assert res.get_json()["crush"] is None, "nothing for her to be acting on"


# --- the master crush switch -------------------------------------------------

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


# --- people: bulk -------------------------------------------------------------

def test_a_selection_can_be_quietened_in_one_action(client, tmp_path, monkeypatch):
    monkeypatch.setattr(mute, "MUTED_FILE", tmp_path / "muted.json")
    mute.invalidate()
    try:
        res = client.post("/api/mute", json={"slugs": ["ray", "kim", "ray"], "muted": True})
        assert res.status_code == 200, res.get_json()
        assert mute.muted_slugs() == {"ray", "kim"}, "deduped, and both applied"

        # One bad entry applies none of it: a bulk action that half-applied
        # would leave the panel describing a group that is not actually quiet.
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
