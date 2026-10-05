"""Runs the dashboard's inline script against a fake DOM and renders one view.

    from dashboard_render import render_view   ->  (text, stdout)

`node --check` catches a syntax error and nothing else. It cannot tell you that
two editors for one file raced and the slower one won, which is exactly what
happened: the page parsed, rendered, and threw away whatever you were typing.
So this builds a DOM stub just rich enough for the view functions, feeds them a
real snapshot, and returns what ended up on the page.

Deliberately not a test module - `tests/test_dashboard.py` owns the assertions.
"""

from __future__ import annotations

import json
import pathlib
import re
import subprocess
import tempfile

TEMPLATE = pathlib.Path(__file__).resolve().parents[1] / "yuyu" / "dashboard" / "templates" / "index.html"

# Enough of a Document for el(), which dispatches on nodeType and appends
# everything else as a text node.
_HARNESS = """
function mk(tag) {
  const n = {
    nodeType: tag === "#text" ? 3 : 1,
    tagName: String(tag).toUpperCase(), children: [], attrs: {},
    classList: { add() {}, remove() {}, contains() { return false; } },
    append(...k) { for (const x of k.flat()) if (x != null) this.children.push(x); },
    appendChild(x) { this.children.push(x); return x; },
    prepend(x) { this.children.unshift(x); },
    replaceChildren(...k) { this.children = k.flat().filter(x => x != null); },
    setAttribute() {}, removeAttribute() {},
    querySelector() { return mk("div"); },
    querySelectorAll() { return []; },
    remove() {}, addEventListener() {},
    _t: "", value: "",
  };
  Object.defineProperty(n, "textContent", {
    get() { return this._t; }, set(v) { this._t = v; }, configurable: true,
  });
  return n;
}
const byId = new Map();
for (const id of __IDS__) byId.set(id, mk("div"));
globalThis.document = {
  createElement: mk,
  createTextNode: (t) => { const n = mk("#text"); n._t = String(t); return n; },
  querySelector: (s) => byId.get(String(s).replace(/^#/, "")) || mk("div"),
  querySelectorAll: () => [],
  body: mk("body"),
  addEventListener() {},
};
globalThis.window = {
  addEventListener() {},
  location: { hash: "", replace() {} },
  requestAnimationFrame(f) { f(); },
};
globalThis.history = { replaceState() {} };
globalThis.location = globalThis.window.location;
globalThis.confirm = () => false;
globalThis.setTimeout = () => 0;
// The boot handler runs too, so /api/ has to hand back the real snapshot or
// enter() trips over a hollow STATE.
globalThis.fetch = async (url) => {
  const body = url.endsWith("/api/") ? __SNAP__
             : url.endsWith("/reset/backups") ? { backups: [] }
             : { content: "canned file body" };
  return { ok: true, status: 200, json: async () => body, text: async () => JSON.stringify(body) };
};
const __tick = async (n = 30) => {
  for (let i = 0; i < n; i++) await new Promise(r => setImmediate(r));
};
const __find = (node, tag) => {
  const out = [];
  const walk = (x) => {
    for (const c of x.children || []) {
      if (!c) continue;
      if (c.tagName === tag.toUpperCase()) out.push(c);
      walk(c);
    }
  };
  walk(node);
  return out;
};
const __text = (node) => {
  const out = [];
  const walk = (x) => {
    if (x._t) out.push(x._t);
    for (const c of x.children || []) if (c) walk(c);
  };
  walk(node);
  return out.join(" ").replace(/\\s+/g, " ");
};
"""

_RUNNER = """
// The boot handler at the bottom of the script paints Overview from its own
// fetch. Let it finish first, then take the page - otherwise every view comes
// back as Overview no matter which one was asked for.
await __tick();
STATE = __SNAP__;
STATE.affinity.bond = __BOND__;
VIEW = __VIEW__;
const __host = document.querySelector("#view");
render();
await __tick();
const __area = () => __find(__host, "textarea")[0];
let __extra = {};
"""

_EDITOR_PROBE = """
if (VIEW === "persona") {
  const box = __area();
  __extra.built = !!box;
  if (box) {
    box.value = "half typed";
    box.oninput();
    VIEW = "overview"; render();
    VIEW = "persona"; render();
    await __tick();
    __extra.kept = __area() && __area().value;
  }
}
"""

# Every view in one process. A throw is caught per view and recorded, so one
# broken tab names itself instead of hiding the other eleven.
_MULTI_RUNNER = """
await __tick();
STATE = __SNAP__;
const __host = document.querySelector("#view");
const __out = {};
for (const view of __VIEWS__) {
  try {
    VIEW = view;
    render();
    await __tick(5);
    __out[view] = __text(__host);
  } catch (e) {
    __out[view] = "THREW: " + ((e && e.stack) || e);
  }
}
console.log(JSON.stringify(__out));
"""

_RESET_PROBE = """
if (VIEW === "reset") {
  LAST_RESET = { deleted: ["affinity/atulyakant.json"], backup: null, configRestartNeeded: false };
  render();
  __extra.text = __text(__host);
}
"""

# How many columns the daily chart actually drew, and how many were quiet days.
_USAGE_CHART_PROBE = """
if (VIEW === "usage") {
  const cols = __find(__host, "div").filter(n => String(n.className || "").includes("ucol"));
  __extra.columns = cols.length;
  __extra.empty = cols.filter(n => String(n.className || "").includes("empty-day")).length;
}
"""

# Which logo files the Model view actually asked for. `el()` assigns `src` as a
# property, so it lands straight on the node - the stub's no-op setAttribute is
# not involved and the value is readable here.
_LOGO_PROBE = """
if (VIEW === "model") {
  __extra.logos = __find(__host, "img")
    .map(n => String(n.src || ""))
    .filter(s => s.includes("/logos/"));
}
"""


def _script() -> str:
    html = TEMPLATE.read_text(encoding="utf-8")
    tag = '<script type="module">'
    start = html.index(tag) + len(tag)
    return html[start: html.index("</script>", start)]


# A bond that exists, so the panel takes its populated branch. The live snapshot
# has none until the owner has had a turn the running bot can score.
_BOND = {
    "name": "Ray", "slug": "ray", "discordId": "1",
    "close": 42.0, "mood": -30.0, "moodWord": "rough", "caring": True,
    "reason": "he sounds like he is going through it",
    "minutesLeft": 74, "checkIns": 2, "lastCheckIn": "2026-10-03T10:00:00+00:00",
    "lowThreshold": -35.0,
}


def render_view(view: str, snapshot: dict, *, probe: str = "") -> tuple[str, dict]:
    """Render one view. Returns (text-on-page, marker-object as JSON).

    `probe` is one of the private probe constants above, or "" for a plain render.
    Raises RuntimeError with node's own output if the script throws.
    """
    code = _script()
    ids = sorted(set(re.findall(r'\$\("#([\w-]+)"\)', code)))
    body = (
        _HARNESS.replace("__IDS__", json.dumps(ids))
        .replace("__SNAP__", json.dumps(snapshot))
        + code
        + _RUNNER.replace("__SNAP__", json.dumps(snapshot))
        .replace("__BOND__", json.dumps(_BOND))
        .replace("__VIEW__", json.dumps(view))
        + {"editor": _EDITOR_PROBE, "reset": _RESET_PROBE,
           "logos": _LOGO_PROBE,
           "usagechart": _USAGE_CHART_PROBE}.get(probe, "")
        + "\nconsole.log(JSON.stringify({ text: __text(__host), ...__extra }));\n"
    )
    with tempfile.TemporaryDirectory() as tmp:
        path = pathlib.Path(tmp) / "render.mjs"
        path.write_text(body, encoding="utf-8")
        # utf-8 explicitly: the page text carries em dashes and stars, and the
        # default Windows codec would throw on them instead of on a real fault.
        done = subprocess.run(["node", str(path)], capture_output=True, text=True,
                              encoding="utf-8", timeout=60)
    if done.returncode != 0:
        raise RuntimeError(f"rendering {view} threw:\n{done.stderr.strip()}")
    payload = json.loads(done.stdout.strip().splitlines()[-1])
    return payload["text"], payload


def render_views(views, snapshot: dict) -> dict[str, str]:
    """Render several views in one node process. Returns {view: text-on-page}.

    A view that throws comes back as "THREW: ..." rather than taking the whole
    run down, so a single broken tab is reported by name.
    """
    code = _script()
    ids = sorted(set(re.findall(r'\$\("#([\w-]+)"\)', code)))
    body = (
        _HARNESS.replace("__IDS__", json.dumps(ids))
        .replace("__SNAP__", json.dumps(snapshot))
        + code
        + _MULTI_RUNNER
        .replace("__SNAP__", json.dumps(snapshot))
        .replace("__VIEWS__", json.dumps(list(views)))
    )
    with tempfile.TemporaryDirectory() as tmp:
        path = pathlib.Path(tmp) / "render.mjs"
        path.write_text(body, encoding="utf-8")
        done = subprocess.run(["node", str(path)], capture_output=True, text=True,
                              encoding="utf-8", timeout=60)
    if done.returncode != 0:
        raise RuntimeError(f"rendering {list(views)} threw:\n{done.stderr.strip()}")
    return json.loads(done.stdout.strip().splitlines()[-1])
