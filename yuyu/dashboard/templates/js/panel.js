// The dashboard, as one ES module.
//
// Split out of index.html, which is now a shell that links this file. The
// contents are unchanged: served as a module from /js/panel.js it behaves
// exactly as the inline <script type="module"> did.
//
// Sections run top to bottom: core builders, the first-run wizard, hash
// navigation and rendering, then one function per panel, then boot.

const $ = (s, r = document) => r.querySelector(s);

// Attributes (aria-*, data-*, role, for) must go through setAttribute.
const AS_ATTR = /^(aria-|data-)|^role$|^for$/;

const el = (t, a = {}, ...kids) => {
  const n = document.createElement(t);
  for (const [k, v] of Object.entries(a)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class" || k === "className") n.className = v;
    else if (k === "dataset") Object.assign(n.dataset, v);
    else if (AS_ATTR.test(k)) n.setAttribute(k, v === true ? "" : v);
    else n[k] = v;
  }
  for (const kid of kids.flat(3)) {
    if (kid === null || kid === undefined || kid === false) continue;
    n.append(kid?.nodeType ? kid : document.createTextNode(kid));
  }
  return n;
};

const escapeHtml = s => String(s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const clamp = (n, lo, hi) => Math.max(lo, Math.min(hi, n));

// Skipped children are dropped, never appended as "null".
const add = (v, ...kids) => {
  v.append(...kids.flat(3).filter(k => k !== null && k !== undefined && k !== false));
  return v;
};

// Same, but clears the host first.
const swap = (v, ...kids) => {
  v.replaceChildren(...kids.flat(3).filter(k => k !== null && k !== undefined && k !== false));
  return v;
};

let STATE = null;
let VIEW = "overview";
// File in the textarea, so a refresh does not lose typing.
let EDITING = null;
// Ctrl-S reaches the editor on screen.
let SAVING = null;
let SERVER_FILTER = "";

function toast(msg, err = false) {
  const t = el("div", { className: `toast-item${err ? " err" : ""}` },
    el("span", { className: "dot" }), el("span", {}, msg));
  $("#toast").append(t);
  setTimeout(() => t.remove(), 4200);
}

async function api(path, opts = {}) {
  const res = await fetch(`/api${path}`, {
    ...opts,
    headers: { "Content-Type": "application/json" },
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.error || `HTTP ${res.status}`);
  return body;
}

const post = (path, body) => api(path, { method: "POST", body: JSON.stringify(body) });

// Re-read after a write to be sure it stuck.
async function reload() {
  const res = await api("/");
  Object.assign(STATE, res);
  return res;
}

const saved = msg => toast(msg);

// small builders
const SECTIONS = ["details", "notes", "likes", "dislikes", "projects", "people"];
const SECTION_LABEL = {
  details: "Details", notes: "Notes", likes: "Likes",
  dislikes: "Dislikes", projects: "Projects", people: "People",
};

const GROUPS = {
  overview: "Overview", model: "Overview", usage: "Overview",
  presence: "Overview", servers: "Overview",
  affinity: "Relationships", people: "Relationships",
  skills: "Content", persona: "Content", stickers: "Content",
  reset: "System", logs: "System", config: "System",
};

function head(title, lede, ...actions) {
  return el("div", { className: "page-head" },
    el("div", { className: "page-head-main" },
      el("p", { className: "eyebrow" }, GROUPS[VIEW] || "Dashboard"),
      el("h1", {}, title),
      lede ? el("p", { className: "lede" }, lede) : null),
    actions.length ? el("div", { className: "page-actions" }, ...actions) : null);
}

// Head with her portrait. Overview only.
function heroHead(title, lede, ...actions) {
  return el("div", { className: "page-head with-portrait" },
    el("div", { className: "page-head-body" },
      el("div", { className: "page-head-portrait" },
        STATE.bot.name.charAt(0).toUpperCase(),
        portraitNode()),
      el("div", { className: "page-head-main" },
        el("p", { className: "eyebrow" }, GROUPS[VIEW] || "Dashboard"),
        el("h1", {}, title),
        lede ? el("p", { className: "lede" }, lede) : null)),
    actions.length ? el("div", { className: "page-actions" }, ...actions) : null);
}

function panel(title, meta, ...kids) {
  const p = el("section", { className: "panel" },
    title
      ? el("div", { className: "panel-head" },
          el("h2", {}, title),
          meta ? el("span", { className: "panel-meta" }, meta) : null)
      : null,
    ...kids);
  if (title) p.setAttribute("data-toc", "");
  return p;
}

function stat(label, value, sub) {
  return el("div", { className: "tile" },
    el("div", { className: "k" }, label),
    el("div", { className: "v" }, value),
    sub ? el("div", { className: "s" }, sub) : null);
}

function kv(label, value) {
  return el("div", { className: "kv-row" },
    el("span", { className: "k" }, label),
    el("span", { className: "v" }, value));
}

function chips(...items) {
  return el("div", { className: "row" }, ...items.filter(Boolean));
}

function chip(text, cls = "") {
  return el("span", { className: `chip${cls ? " " + cls : ""}` }, text);
}

function notice(...kids) {
  return el("div", { className: "notice" }, el("span", { className: "dot" }), el("div", {}, ...kids));
}

function empty(title, ...kids) {
  return el("div", { className: "empty" }, el("b", {}, title), ...kids);
}

function field(label, input, hint = "") {
  return el("div", { className: "field" },
    el("label", {}, label),
    input,
    hint ? el("p", { className: "hint" }, hint) : null);
}

// Switch keeps its own state.
function toggleSwitch(on, onToggle, label = "toggle") {
  let state = !!on;
  const sw = el("button", { className: `toggle${state ? " on" : ""}`, type: "button" }, el("i", {}));
  sw.setAttribute("role", "switch");
  sw.setAttribute("aria-checked", String(state));
  sw.setAttribute("aria-label", label);
  sw.onclick = () => {
    state = !state;
    sw.setAttribute("aria-checked", String(state));
    if (state) sw.classList.add("on"); else sw.classList.remove("on");
    if (onToggle) onToggle(state);
  };
  return sw;
}

// Insert at the caret, not the end.
function insertInto(input, text) {
  if (!input) return;
  const start = typeof input.selectionStart === "number" ? input.selectionStart : input.value.length;
  const end = typeof input.selectionEnd === "number" ? input.selectionEnd : start;
  input.value = input.value.slice(0, start) + text + input.value.slice(end);
  if (input.focus) {
    try { input.selectionStart = input.selectionEnd = start + text.length; } catch { /* not selectable */ }
    input.focus();
  }
}

const cap = s => { const t = String(s || ""); return t.charAt(0).toUpperCase() + t.slice(1); };

// A URL is an image, an asset key is a monogram.
const artNode = (value, fallback) => /^https?:\/\//i.test(value || "")
  ? el("img", { src: value, alt: "" })
  : (value ? value.charAt(0).toUpperCase() : fallback);

// Portrait; onerror drops it so the initial stays visible.
const portraitNode = () => el("img", {
  src: "/portrait",
  alt: "",
  decoding: "async",
  onerror: function () { this.remove(); },
});

// Provider mark from templates/logos, letter tile on a miss.
const logoNode = provider => {
  const label = String(provider?.name || provider?.id || "").trim();
  const tile = el("span", { className: "prov-mark", title: label });

  // Only a plain, safe id ever becomes a URL.
  const id = String(provider?.id || "").toLowerCase();
  if (!/^[a-z0-9][a-z0-9._-]*$/.test(id)) {
    tile.textContent = label ? label.charAt(0).toUpperCase() : "?";
    return tile;
  }

  const img = el("img", { className: "prov-logo", alt: "", decoding: "async" });
  const sources = [`/logos/${id}.svg`, `/logos/${id}.png`];
  let tried = 0;
  // Handlers before src: a cached 404 can fire first.
  img.onload = () => tile.classList.add("with-logo");
  img.onerror = () => {
    tried += 1;
    if (tried < sources.length) { img.src = sources[tried]; return; }
    tile.textContent = label ? label.charAt(0).toUpperCase() : "?";
  };
  img.src = sources[0];
  tile.append(img);
  return tile;
};

// Fill the placeholders we know locally.
function resolvePresenceText(text) {
  return String(text || "").replace(/\{(\w+)\}/g, (_, key) => {
    const known = {
      name: STATE.bot.name,
      prefix: STATE.bot.prefix,
      people: (STATE.people || []).length ? `${STATE.people.length} people` : "",
      servers: (STATE.guilds || []).length ? `${STATE.guilds.length} servers` : "",
      activity: "chatting",
    };
    return key in known ? known[key] : "";
  });
}

const uptime = s => {
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  return h ? `${h}h ${m}m` : `${m}m ${s % 60}s`;
};

// Abbreviated for headings; exact numbers live in tooltips.
const shortNum = n => {
  const v = Number(n) || 0;
  const abs = Math.abs(v);
  if (abs >= 1e9) return (v / 1e9).toFixed(abs >= 1e10 ? 0 : 1) + "B";
  if (abs >= 1e6) return (v / 1e6).toFixed(abs >= 1e7 ? 0 : 1) + "M";
  if (abs >= 1e4) return (v / 1e3).toFixed(0) + "k";
  if (abs >= 1e3) return (v / 1e3).toFixed(1) + "k";
  return String(Math.round(v));
};

// Keeps decimals so a tiny cost does not read as $0.00.
const money = (value, priced = true) => {
  if (!priced || value === null || value === undefined) return "—";
  const v = Number(value);
  if (!isFinite(v)) return "—";
  if (v === 0) return "$0";
  if (v < 0.01) {
    const digits = Math.min(9, Math.max(4, Math.ceil(-Math.log10(v)) + 3));
    return "$" + v.toFixed(digits);
  }
  if (v < 1) return "$" + v.toFixed(4);
  return "$" + v.toFixed(2).replace(/\.00$/, "");
};

const ms = v => {
  const n = Number(v) || 0;
  if (n < 1000) return `${Math.round(n)}ms`;
  return `${(n / 1000).toFixed(n < 10000 ? 2 : 1)}s`;
};

// Unknown source names still render.
const SOURCE_LABEL = {
  chat: "replies",
  extract: "memory extraction",
  summary: "conversation summaries",
};

// model
$("#modelgo").onclick = async () => {
  const btn = $("#modelgo");
  const pick = $("#modelsel").value;
  if (!pick) return;
  btn.disabled = true;
  const previous = btn.textContent;
  btn.textContent = "…";
  try {
    await api("/model", { method: "POST", body: JSON.stringify({ model: pick }) });
    STATE = await api("/");
    fillModels();
    toast(`Using ${STATE.model}`);
  } catch (e) {
    toast(e.message, true);
    fillModels();
  } finally {
    btn.textContent = previous;
    btn.disabled = false;
  }
};

function fillModels() {
  const current = STATE.model;
  const choices = (STATE.models && STATE.models.choices) || [];
  const seen = new Set();
  const chat = [], other = [];
  for (const c of choices) {
    if (!c.id || seen.has(c.id)) continue;
    seen.add(c.id);
    const label = c.title && c.title !== c.id ? c.title : c.id;
    const o = el("option", { value: c.id, title: c.description || c.id }, label);
    if (c.id === current) o.selected = true;
    (c.chat === false ? other : chat).push(o);
  }
  if (!seen.has(current)) chat.unshift(el("option", { value: current }, current));

  const groups = [];
  if (chat.length) groups.push(el("optgroup", { label: "Chat" }, ...chat));
  if (other.length) groups.push(el("optgroup", { label: `Other ” not a chat model (${other.length})` }, ...other));
  $("#modelsel").replaceChildren(...groups);
  $("#modelsel").value = current;

  const isChat = choices.find(c => c.id === current)?.chat !== false;
  const why = isChat
    ? "Switch her to the selected model"
    : "This one is not a chat model - she will not be able to reply with it";
  $("#modelgo").title = why;
  $("#modelsel").title = why;
}

function enter() {
  $("#boot").remove();
  $("#app").hidden = false;
  $("#brandname").textContent = STATE.bot.name;
  fillModels();
  const c = STATE.affinity.crush;
  const pill = $("#crushpill");
  pill.hidden = !STATE.affinity.crushOn;
  pill.className = c ? "chip star" : "chip";
  pill.innerHTML = c ? `<span>&#9733;</span> ${escapeHtml(c.name)}` : "no crush yet";
  buildNav();
  render();
  // Opened after the panel is drawn.
  wizAutoOpen();
}

// first-run wizard
// Name, provider, photo, token, owner - each depends on the one before.
// Nothing is a gate: skip is always allowed and Overview reaches every step.
const WIZ = {
  step: 0,
  done: [false, false, false],
  state: null,
};

const WIZ_STEPS = [
  ["name", "Name"],
  ["provider", "Provider"],
  ["photo", "Photo"],
  ["done", "Finish"],
];

function wizPaintSteps() {
  $("#wiz-steps").replaceChildren(...WIZ_STEPS.map(([, label], i) => {
    const node = el("div", { className: "wiz-step", role: "listitem" }, label);
    if (i === WIZ.step) node.setAttribute("aria-current", "true");
    if (WIZ.done[i]) node.setAttribute("data-done", "true");
    return node;
  }));
}

// Real endpoints, so the common case is one tap.
const WIZ_PRESETS = [
  { id: "gemini", name: "Gemini", baseUrl: "https://generativelanguage.googleapis.com/v1beta/openai",
    model: "gemini-2.0-flash", env: "GEMINI_API_KEY", logo: "gemini.svg" },
  { id: "groq", name: "Groq", baseUrl: "https://api.groq.com/openai/v1",
    model: "llama-3.3-70b-versatile", env: "GROQ_API_KEY", logo: "groq.svg" },
  { id: "openrouter", name: "OpenRouter", baseUrl: "https://openrouter.ai/api/v1",
    model: "", env: "OPENROUTER_API_KEY", logo: "openrouter.svg" },
  { id: "cerebras", name: "Cerebras", baseUrl: "https://api.cerebras.ai/v1",
    model: "llama-3.3-70b", env: "CEREBRAS_API_KEY", logo: "cerebras.png" },
  { id: "sambanova", name: "SambaNova", baseUrl: "https://api.sambanova.ai/v1",
    model: "", env: "SAMBANOVA_API_KEY", logo: "sambanova.png" },
];

function wizGo(step) {
  WIZ.step = Math.max(0, Math.min(WIZ_STEPS.length - 1, step));
  WIZ_STEPS.forEach(([id], i) => { $(`#wiz-step-${id}`).hidden = i !== WIZ.step; });
  wizPaintSteps();

  const first = WIZ_STEPS[WIZ.step][0];
  const back = $("#wiz-back");
  const next = $("#wiz-next");
  back.hidden = WIZ.step === 0;
  $("#wiz-skip").hidden = first === "done";
  // Every step saves; the last one just closes.
  next.textContent = first === "done" ? "Open the panel" : "Save and continue";
}

function wizOpen(firstStep = 0) {
  $("#wizard").hidden = false;
  wizGo(firstStep);
}

function wizClose() {
  $("#wizard").hidden = true;
}

function wizNote(where, text, kind = "") {
  const box = $(where);
  box.replaceChildren(el("div", { className: `wiz-note${kind ? " " + kind : ""}` }, text));
}

// step 1: name
async function wizSaveName() {
  const name = $("#wiz-botname").value.trim();
  if (!name) { toast("Give her a name first", true); return false; }
  const alsoDiscord = $("#wiz-name-discord").checked;
  try {
    const res = await post("/setup/name", { name, alsoDiscordName: alsoDiscord });
    $("#brandname").textContent = res.name;
    if (STATE) STATE.bot.name = res.name;
    // The rename touched her own files too.
    WIZ.done[0] = true;
    // Reported separately - the Discord push can be refused on its own.
    const bits = ["Saved"];
    if (res.renamedIn?.length) bits.push(`updated in ${res.renamedIn.join(", ")}`);
    if (res.discordNamePushed) bits.push("her Discord account was renamed too");
    else if (res.why) bits.push(res.why);
    $("#wiz-name-live").textContent = bits.join(" - ") + ".";
    return true;
  } catch (e) {
    toast(e.message, true);
    return false;
  }
}

// step 2: provider
// Keys go to .env, never config.json.
async function wizSaveProvider() {
  const id = $("#wiz-base").value.trim();
  const model = $("#wiz-model").value.trim();
  const env = $("#wiz-env").value.trim();
  const key = $("#wiz-key").value.trim();
  if (!id || !model || !env) {
    toast("A base URL, a model and a key variable name are all needed", true);
    return false;
  }
  if (!key) {
    toast("Paste the API key", true);
    return false;
  }
  const preset = WIZ_PRESETS.find(p => p.baseUrl === id);
  try {
    const res = await post("/provider", {
      id: (preset?.id || env.replace(/[^a-z0-9]+/gi, "-").toLowerCase()),
      name: preset?.name || env.replace(/_API_KEY$/, "").replace(/_/g, " "),
      baseUrl: id,
      model,
      apiKeyEnv: env,
      apiKey: key,
      enabled: true,
    });
    $("#wiz-key").value = "";   // never left sitting in a DOM field
    STATE = await api("/");
    WIZ.done[1] = true;
    wizNote("#wiz-provider-note",
      `Added ${res.provider.name} on ${res.provider.model}. The key is in .env and is never read back out.`,
      "ok");
    fillModels();
    return true;
  } catch (e) {
    wizNote("#wiz-provider-note", e.message, "bad");
    toast(e.message, true);
    return false;
  }
}

// Ask what it serves; works before the provider is saved.
$("#wiz-fetch-models").onclick = async () => {
  const btn = $("#wiz-fetch-models");
  const note = $("#wiz-fetch-note");
  const base = $("#wiz-base").value.trim();
  const env = $("#wiz-env").value.trim();
  const key = $("#wiz-key").value.trim();
  if (!base) { note.textContent = "a base URL first"; return; }
  btn.disabled = true;
  note.textContent = "asking…";
  try {
    const preset = WIZ_PRESETS.find(p => p.baseUrl === base);
    const res = await post("/provider/models", {
      id: preset?.id || env.replace(/[^a-z0-9]+/gi, "-").toLowerCase(),
      name: preset?.name || "",
      baseUrl: base,
      apiKeyEnv: env,
      apiKey: key,
    });
    const pick = $("#wiz-model-pick");
    const models = res.models || [];
    pick.replaceChildren(
      ...models.map(m => el("option", { value: m.id }, `${m.id}${m.context_length ? ` · ${shortNum(m.context_length)} ctx` : ""}`)));
    $("#wiz-model-pick-field").hidden = models.length === 0;
    if (models.length) {
      // Default to the first chat model they serve.
      const first = models.find(m => m.chat !== false) || models[0];
      $("#wiz-model").value = first.id;
    }
    note.textContent = models.length ? `${models.length} model(s)` : "they returned an empty list";
  } catch (e) {
    note.textContent = e.message;
  } finally {
    btn.disabled = false;
  }
};

// step 3: photo, token, owner
async function wizSavePhoto() {
  const token = $("#wiz-token").value.trim();
  const owner = $("#wiz-owner").value.trim();
  const notes = [];

  const dataUrl = await wizReadPicture();
  if (dataUrl) {
    try {
      const res = await post("/setup/portrait", { data: dataUrl });
      WIZ.done[2] = true;
      notes.push(res.pushedToDiscord
        ? "Picture saved and pushed to her Discord avatar."
        : `Picture saved${res.why ? " - " + res.why : ""}`);
      wizPaintFace(dataUrl);
    } catch (e) {
      notes.push("Picture refused: " + e.message);
    }
  } else if (wizFacePending) {
    notes.push("Picture skipped.");
  } else if (!wizHasSavedPicture()) {
    notes.push("No picture yet - she will show her initial instead. You can add one later.");
  } else {
    notes.push("Picture kept as it was.");
  }

  if (token) {
    try {
      await post("/setup/token", { token });
      notes.push("Bot token written to .env (restart her to connect).");
      $("#wiz-token").value = "";
    } catch (e) {
      notes.push("Token refused: " + e.message);
    }
  }

  if (owner) {
    try {
      await post("/setup/owner", { userId: owner });
      notes.push("You are the owner - owner-only commands will work.");
    } catch (e) {
      notes.push("Owner ID refused: " + e.message);
    }
  }

  // Portal extras go as one batch.
  const appId = $("#wiz-appid").value.trim();
  const pubKey = $("#wiz-pubkey").value.trim();
  const clientSecret = $("#wiz-secret").value.trim();
  if (appId || pubKey || clientSecret) {
    try {
      await post("/setup/discord", { appId, publicKey: pubKey, clientSecret });
      notes.push("Developer Portal details saved.");
      $("#wiz-secret").value = "";
    } catch (e) {
      notes.push("Portal details refused: " + e.message);
    }
  }

  const bad = notes.filter(n => /refused/i.test(n));
  wizNote("#wiz-photo-note", notes.join(" "), bad.length ? "bad" : "ok");
  return bad.length === 0;
}

// A pending file, or one already on disk.
function wizHasSavedPicture() {
  return Boolean(wizFacePending) || !$("#wiz-photo-preview").hidden;
}

let wizFacePending = false;

// Size-checked here too, before the giant string.
function wizReadPicture() {
  const input = $("#wiz-file-2");
  const file = input.files && input.files[0];
  if (!file) return Promise.resolve(null);
  if (file.size > 8 * 1024 * 1024) {
    toast("That image is over 8 MB", true);
    return Promise.resolve(null);
  }
  return new Promise(resolve => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ""));
    reader.onerror = () => resolve(null);
    reader.readAsDataURL(file);
  });
}

// Preview the picked file before anything is saved.
function wizPaintFace(dataUrl) {
  const img = $("#wiz-photo-preview");
  img.src = dataUrl;
  img.hidden = false;
  wizFacePending = true;
  $("#wiz-drop-text").textContent = "Picture picked - it is saved when you continue";
}

// Point the header at /portrait.
function wizPaintSaved() {
  const img = $("#wiz-photo-preview");
  img.src = "/portrait";
  img.hidden = false;
  const head = $("#brandimg");
  if (head) {
    head.src = "/portrait";
    head.hidden = false;
  }
  $("#brandface").hidden = true;
  wizFacePending = false;
}

// step 4: finish
async function wizFinish() {
  let state;
  try {
    state = await post("/setup/finish", {});
  } catch {
    state = await api("/setup");
  }

  const rows = [
    [Boolean(state.hasWorkingModel),
      state.hasWorkingModel ? `She can talk on ${state.workingModel}` : "No model API key yet"],
    [Boolean(state.hasBotToken), state.hasBotToken ? "Discord token saved" : "No bot token yet"],
    [state.connected === true, state.connected
      ? `Connected as ${state.discordUsername || "the bot"}`
      : "Not connected - start her with python main.py"],
    [Boolean(state.hasPortrait), state.hasPortrait ? "Picture saved" : "No picture yet"],
  ];
  $("#wiz-tally").replaceChildren(...rows.map(([ok, text]) =>
    el("div", { className: "wiz-tally-row", dataset: { ok: String(ok) } },
      el("i", {}, ok ? "✓" : "!"), el("span", {}, text))));

  const missing = state.missing || [];
  wizNote("#wiz-done-note",
    missing.length
      ? "Still to do: " + missing.join(" · ")
      : "Everything is in place. She is ready.",
    missing.length ? "bad" : "ok");

  if (state.hasPortrait) wizPaintSaved();
}

// wiring
$("#wiz-next").onclick = async () => {
  const btn = $("#wiz-next");
  btn.disabled = true;
  const first = WIZ_STEPS[WIZ.step][0];
  try {
    if (first === "name") { if (await wizSaveName()) wizGo(1); }
    else if (first === "provider") { if (await wizSaveProvider()) wizGo(2); }
    else if (first === "photo") {
      await wizSavePhoto();
      wizGo(3);
      await wizFinish();
      // Keep the panel behind in step.
      try { STATE.setup = await api("/setup"); } catch {}
    }
    else {
      // Overlay, not a wall - all reachable again from Overview.
      wizMarkDismissed();
      STATE = await api("/");
      fillModels();
      buildNav();
      render();
      wizClose();
    }
  } catch (e) {
    toast(e.message, true);
  } finally {
    btn.disabled = false;
  }
};

$("#wiz-back").onclick = () => wizGo(WIZ.step - 1);

// "Skip for now" is the same dismissal as closing.
$("#wiz-skip").onclick = async () => {
  wizMarkDismissed();
  wizClose();
  try { STATE.setup = await api("/setup"); } catch {}
};

// Escape at step 0 closes and counts as dismissed.
$("#wizard").addEventListener("keydown", e => {
  if (e.key !== "Escape") return;
  if (WIZ.step > 0) { e.preventDefault(); wizGo(WIZ.step - 1); }
  else { e.preventDefault(); wizMarkDismissed(); wizClose(); }
});

// Enter advances, from a text field only.
$("#wizard").addEventListener("keydown", e => {
  if (e.key === "Enter" && e.target.tagName === "INPUT" && e.target.type !== "file") {
    e.preventDefault();
    $("#wiz-next").click();
  }
});

// A tap fills the fields but never saves.
function wizBuildPresets() {
  const box = $("#wiz-presets");
  const buttons = WIZ_PRESETS.map(preset => {
    const btn = el("button", { className: "wiz-preset", type: "button" },
      el("img", { src: `/logos/${preset.logo}`, alt: "", decoding: "async",
                   onerror: function () { this.remove(); } }),
      el("span", {}, preset.name));
    btn.setAttribute("aria-pressed", "false");
    btn.onclick = () => {
      $("#wiz-base").value = preset.baseUrl;
      $("#wiz-env").value = preset.env;
      if (preset.model) $("#wiz-model").value = preset.model;
      buttons.forEach(b => b.setAttribute("aria-pressed", "false"));
      btn.setAttribute("aria-pressed", "true");
      $("#wiz-key").focus();
    };
    return btn;
  });
  box.replaceChildren(...buttons);
}

// Both inputs drive the same preview.
const photoInput = $("#wiz-file-2");
photoInput.addEventListener("change", async () => {
  const url = await wizReadPictureFor(photoInput);
  if (url) wizPaintFace(url);
});

function wizReadPictureFor(input) {
  const file = input.files && input.files[0];
  if (!file) return Promise.resolve(null);
  if (file.size > 8 * 1024 * 1024) { toast("That image is over 8 MB", true); return Promise.resolve(null); }
  return new Promise(resolve => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ""));
    reader.onerror = () => resolve(null);
    reader.readAsDataURL(file);
  });
}

// Drop zone; a click already opens the picker.
const drop = $("#wiz-drop");
["dragenter", "dragover"].forEach(ev => drop.addEventListener(ev, e => {
  e.preventDefault();
  drop.classList.add("over");
}));
["dragleave", "drop"].forEach(ev => drop.addEventListener(ev, e => {
  e.preventDefault();
  drop.classList.remove("over");
}));
drop.addEventListener("drop", async e => {
  const file = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
  if (!file) return;
  if (file.size > 8 * 1024 * 1024) { toast("That image is over 8 MB", true); return; }
  const reader = new FileReader();
  reader.onload = () => wizPaintFace(String(reader.result || ""));
  reader.readAsDataURL(file);
});

wizBuildPresets();

// Entry point for the Overview tile; clears the dismissal.
window._wizardOpen = async () => {
  if (WIZ.state && WIZ.state.wizardDismissed) {
    post("/setup/dismissed", { dismissed: false }).catch(() => {});
    if (WIZ.state) WIZ.state.wizardDismissed = false;
  }
  wizOpen(0);
};

// A variable, not a storage key: this also runs under Node.
let wizSeenThisLoad = false;

// Persisted server-side, so it survives a new machine.
function wizMarkDismissed() {
  post("/setup/dismissed", { dismissed: true }).catch(() => {});
  if (WIZ.state) WIZ.state.wizardDismissed = true;
}

async function wizAutoOpen() {
  if (wizSeenThisLoad) return;
  wizSeenThisLoad = true;
  let state;
  try { state = await api("/setup"); } catch { return; }

  WIZ.state = state;
  STATE.setup = state;
  $("#wiz-botname").value = state.name || "";
  // Pre-fill with what is already set.
  $("#wiz-appid").value = state.appId || "";
  $("#wiz-pubkey").value = state.publicKey || "";
  if (state.ownerId) $("#wiz-owner").value = state.ownerId;
  // Never pre-filled with a secret: the server only reports whether one exists.
  $("#wiz-secret").placeholder = state.hasClientSecret
    ? "already set - paste a new one to replace it"
    : "only if you want the install link built";
  if (state.hasPortrait) wizPaintSaved();

  // Preselect a provider she has a key for.
  const withKey = (STATE?.models?.providers || []).find(p => p.hasKey);
  if (withKey) {
    $("#wiz-base").value = withKey.baseUrl;
    $("#wiz-model").value = withKey.model;
    $("#wiz-env").value = withKey.apiKeyEnv || "";
  }
  if (!state.connected) {
    wizNote("#wiz-photo-note",
      "She is not connected to Discord, so a picture or username cannot be pushed yet. Both are saved and applied the moment she starts.");
  }

  if (!state.firstRun) return;         // configured: leave the panel alone
  if (state.wizardDismissed) return;  // already dealt with: leave it alone
  wizOpen(0);
}

// hash navigation
const TABS = [
  ["overview", "Overview"],
  ["model", "Model"],
  ["usage", "Usage"],
  ["presence", "Presence"],
  ["servers", "Servers"],
  ["affinity", "Affect"],
  ["people", "People"],
  ["skills", "Skills"],
  ["persona", "Persona"],
  ["stickers", "Stickers"],
  ["reset", "Reset"],
  ["logs", "Logs"],
  ["config", "Config"],
];

function currentViewFromHash() {
  const hash = location.hash.slice(1);
  const found = TABS.find(([id]) => id === hash);
  return found ? found[0] : "overview";
}

// Counts next to the rail entries.
const COUNTS = {
  people: () => (STATE.people || []).length,
  servers: () => (STATE.guilds || []).length,
  skills: () => (STATE.skills || []).length,
  affinity: () => (STATE.affinity.people || []).length,
  // Check the number, not shortNum(0), which is a truthy "0".
  usage: () => {
    const total = (STATE.usage || {}).totals?.total || 0;
    return total ? shortNum(total) : 0;
  },
};

function buildNav() {
  document.querySelectorAll('#side-nav a').forEach(a => {
    if (a.dataset.nav === VIEW) a.setAttribute('aria-current', 'true');
    else a.removeAttribute('aria-current');

    const count = COUNTS[a.dataset.nav]?.();
    const existing = a.querySelector(".nav-n");
    if (existing) existing.remove();
    if (count) a.append(el("span", { className: "nav-n" }, String(count)));
  });
}

function navigateTo(view) {
  if (VIEW === view) { render(); return; }
  VIEW = view;
  history.replaceState(null, '', '#' + view);
  buildNav();
  render();
  closeNav();
}

window.addEventListener('hashchange', () => {
  VIEW = currentViewFromHash();
  buildNav();
  render();
});

// mobile rail
function openNav() {
  const nav = $("#side-nav");
  const scrim = $("#nav-scrim");
  const btn = $("#nav-toggle");
  nav.classList.add('open');
  document.body.classList.add('nav-open');
  scrim.classList.add('show');
  scrim.removeAttribute('hidden');
  btn.setAttribute('aria-expanded', 'true');
  btn.setAttribute('aria-label', 'Close menu');
}

function closeNav() {
  const nav = $("#side-nav");
  const scrim = $("#nav-scrim");
  const btn = $("#nav-toggle");
  nav.classList.remove('open');
  document.body.classList.remove('nav-open');
  scrim.classList.remove('show');
  scrim.setAttribute('hidden', '');
  btn.setAttribute('aria-expanded', 'false');
  btn.setAttribute('aria-label', 'Open menu');
}

$("#nav-toggle").addEventListener('click', () => {
  if ($("#side-nav").classList.contains('open')) closeNav();
  else openNav();
});

$("#nav-scrim").addEventListener('click', closeNav);

$("#side-nav").addEventListener('click', e => {
  if (e.target.closest('a')) closeNav();
});

document.addEventListener('keydown', e => {
  if (e.key === 'Escape') closeNav();
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's' && SAVING) {
    e.preventDefault();
    SAVING();
  }
});

window.addEventListener('resize', () => {
  if (window.innerWidth > 900) closeNav();
});

// TOC
// Cleared per render, or the handlers pile up.
const scrollHandlers = [];
let scrollQueued = false;

function onScroll(fn) { scrollHandlers.push(fn); }

window.addEventListener('scroll', () => {
  if (scrollQueued) return;
  scrollQueued = true;
  window.requestAnimationFrame(() => {
    for (let i = 0; i < scrollHandlers.length; i++) scrollHandlers[i](110);
    scrollQueued = false;
  });
}, { passive: true });

function buildTOC() {
  scrollHandlers.length = 0;
  const toc = $("#page-toc");
  const inner = $("#view");
  toc.replaceChildren();
  if (!inner) return;

  // Panels, not the headings inside them.
  const hosts = inner.querySelectorAll("[data-toc]");
  if (!hosts.length) return;

  toc.append(el("p", { className: "toc-label" }, "On this page"));

  const items = [];
  hosts.forEach((host, i) => {
    if (!host.id) host.id = "sec-" + (i + 1);
    const title = host.querySelector("h2") || host;
    const a = el("a", { href: "#" + host.id }, title.textContent.trim());
    // Scroll it ourselves; the fragment would change the tab.
    a.onclick = ev => {
      ev.preventDefault();
      host.scrollIntoView({ behavior: "smooth", block: "start" });
    };
    toc.append(a);
    items.push({ link: a, target: host });
  });

  items[0].link.setAttribute("aria-current", "true");
  onScroll(line => {
    let current = items[0].target.id;
    items.forEach(it => {
      if (it.target.getBoundingClientRect().top <= line) current = it.target.id;
    });
    items.forEach(it => {
      if (it.target.id === current) it.link.setAttribute("aria-current", "true");
      else it.link.removeAttribute("aria-current");
    });
  });
}

// render
function render() {
  const v = $("#view");
  v.replaceChildren();
  v.className = 'page fade-in';
  ({ overview, model: modelSettings, usage: usageView, presence: presenceSettings, servers, persona, skills, people, affinity, stickers, reset, logs, config: configView }[VIEW] || overview)(v);
  buildNav();
  buildTOC();
}

function file(path, title, sub) {
  const go = () => {
    const view = path.startsWith("skills/") ? "skills"
      : path.startsWith("memory/") ? "people"
      : path === "persona.md" ? "persona"
      : path === "config.json" ? "config"
      : "stickers";
    navigateTo(view);
    setTimeout(() => openEditor(path), 30);
  };
  return el("a", { className: "file", href: "#", onclick: go }, el("b", {}, title), el("span", {}, sub));
}

function goTile(title, sub, view) {
  return el("button", { className: "go", onclick: () => navigateTo(view) },
    el("b", {}, title), el("span", {}, sub));
}

// overview
function overview(v) {
  const a = STATE.affinity;
  const s = STATE.stickers || {};
  const guilds = STATE.guilds || [];
  const members = guilds.reduce((n, g) => n + (g.members || 0), 0);
  const facts = STATE.people.reduce((n, p) => n + p.count, 0);
  const quiet = (STATE.muted || []).map(slug => STATE.people.find(p => p.slug === slug)?.name || slug);

  add(v,
    heroHead(STATE.bot.name,
      "Everything about her in one place ” model, memory, affect, servers, content. All of it editable, all of it live."),

    el("div", { className: "tiles" },
      ...(a.crushOn ? [stat("Crush", a.crush ? a.crush.name : "nobody yet",
        a.crush ? `${a.crush.romance}/100 romance · ${a.crush.read.romance}` : `needs ${a.eligible.join(" or ")}`),
      ] : []),
      stat("People", String(a.people.length),
        a.crushOn ? `${a.people.filter(p => p.eligible).length} eligible for the crush` : "affect records tracked"),
      stat("Model", STATE.model, `${(STATE.models && STATE.models.choices || []).length} providers configured`),
      stat("Uptime", uptime(STATE.uptime), `in ${guilds.length} server${guilds.length === 1 ? "" : "s"}`)),

    panel("Quick actions", null, el("div", { className: "grid gauto" },
      goTile("Switch model", "Change her brain", "model"),
      goTile("Edit persona", "Who she is", "persona"),
      goTile("Affect", "How she feels", "affinity"),
      goTile("People", "Who she knows", "people"),
      goTile("Skills", "What she can do", "skills"),
      goTile("Config", "Every setting", "config"),
      el("button", { className: "go", onclick: () => { window._wizardOpen(); } },
        el("b", {}, "Setup wizard"),
        el("span", {}, "Name, model, photo, token")))),

    panel("At a glance", `${facts} stored facts`, el("div", { className: "kv" },
      kv("Model", STATE.model),
      kv("People she knows", String(STATE.people.length)),
      kv("Stored facts", `${facts} across ${SECTIONS.length} sections`),
      kv("Skills", `${STATE.skills.length} · ${STATE.skills.filter(x => x.always).length} always on`),
      kv("Sticker images", `${(s.files || []).length} · ${(s.images || []).length} keyword-linked`),
      kv("Server emoji", String(s.serverEmoji ?? 0)),
      kv("Keyword reactions", String(s.emojiReactions ?? 0)),
      kv("Servers", `${guilds.length} · ${members} members reached`))),

    panel("Everything you can edit", "persona.md is the highest leverage file in here",
      el("div", { className: "files" },
        file("persona.md", "persona.md", "who she is ” highest leverage"),
        ...STATE.skills.map(x => file(`skills/${x.file}`, `skills/${x.file}`,
          x.always ? "always on" : x.keywords.length ? x.keywords.slice(0, 7).join(", ") : "no keywords")),
        file("stickers.json", "stickers.json", "custom emoji and reactions"),
        file("config.json", "config.json", "model, memory, affect, dashboard"),
        ...STATE.people.map(p => file(`memory/${p.slug}.md`, p.name, `${p.count} fact(s) ” memory/${p.slug}.md`))))
  );

  if (quiet.length) {
    v.append(notice(
      el("b", {}, quiet.length === 1 ? quiet[0] : quiet.join(", ")),
      " ” still remembered, still recorded, just no replies. Turn them back in ",
      el("b", {}, "People"), "."));
  }

  if (a.crushOn && !a.people.some(p => p.pronouns)) {
    v.append(el("div", { className: "notice info" }, el("span", { className: "dot" }),
      el("div", {}, "She only falls for ", el("code", {}, a.eligible.join(" or ")),
        ", so anyone without pronouns recorded can never be elected. Set one in ",
        el("b", {}, "Affect"), ", or with ", el("code", {}, "!pronouns he/him"), ".")));
  }
}

function muteToggle(p) {
  const quiet = (STATE.muted || []).includes(p.slug);
  const sw = el("button", {
    className: `toggle${quiet ? "" : " on"}`,
    title: quiet
      ? "She is quiet for this person. Switching on lets her reply again."
      : "She stops replying to this person. Their memory and the log stay.",
  }, el("i", {}));
  sw.setAttribute("role", "switch");
  sw.setAttribute("aria-checked", String(!quiet));
  sw.setAttribute("aria-label", `let ${p.name} talk to her`);

  sw.onclick = async () => {
    sw.disabled = true;
    try {
      await api("/mute", { method: "POST", body: JSON.stringify({ slug: p.slug, muted: !quiet }) });
      STATE = await api("/");
      const now = (STATE.muted || []).includes(p.slug);
      toast(`${p.name}: ${now ? "she will not reply" : "answering again"}`);
    } catch (e) {
      toast(e.message, true);
    } finally {
      render();
    }
  };
  return el("div", { className: "row", style: "gap:7px" },
    sw,
    el("span", { className: "hint" }, quiet ? "quiet" : "answering"));
}

// servers
let guildFilter = "";

function leaveButton(g) {
  const btn = el("button", { className: "btn btn-sm btn-danger" }, "Leave");
  btn.title = "Make her leave this server. A fresh invite link is the only way to put her back.";
  btn.onclick = async ev => {
    ev.stopPropagation();
    const sure = confirm(
      `Make ${STATE.bot.name} leave "${g.name}"?\n\n` +
      "She stops talking there right away. Every other server is untouched, but " +
      "getting her back needs a fresh invite link from Discord.");
    if (!sure) return;
    btn.disabled = true;
    try {
      await api("/guilds/leave", { method: "POST", body: JSON.stringify({ id: g.id }) });
      STATE = await api("/");
      toast(`left ${g.name}`);
    } catch (e) {
      toast(e.message, true);
    } finally {
      render();
    }
  };
  return btn;
}

function servers(v) {
  const list = STATE.guilds || [];

  const rows = el("div", {});
  const count = el("span", { className: "hint" }, "");
  const box = el("input", { type: "search", className: "grow", placeholder: "filter by name…", value: SERVER_FILTER });
  box.oninput = () => { SERVER_FILTER = box.value; paint(); };

  const guildRow = g => el("div", { className: "srv" },
    el("div", { className: "srv-art" },
      g.icon
        ? el("img", { src: g.icon, alt: "" })
        : (g.name || "?").trim().charAt(0).toUpperCase()),
    el("div", { className: "grow" },
      el("div", { className: "srv-name" }, g.name),
      el("div", { className: "hint" },
        `${g.members} member${g.members === 1 ? "" : "s"} · ${g.channels} channel${g.channels === 1 ? "" : "s"}` +
        (g.joined ? ` · joined ${g.joined}` : ""))),
    el("div", { className: "srv-actions" },
      chip(g.id, "code"),
      leaveButton(g)));

  function paint() {
    const q = SERVER_FILTER.trim().toLowerCase();
    const hits = q ? list.filter(g => (g.name || "").toLowerCase().includes(q)) : list;
    count.textContent = hits.length === list.length
      ? `${list.length} server${list.length === 1 ? "" : "s"}`
      : `${hits.length} of ${list.length}`;
    rows.replaceChildren(...(hits.length
      ? hits.map(guildRow)
      : [empty(q ? `Nothing matches "${SERVER_FILTER}"` : "Not in any servers right now")]));
  }

  const members = list.reduce((n, g) => n + (g.members || 0), 0);

  add(v,
    head("Servers", "Every server she is currently in. Leaving one lasts until someone sends a fresh invite."),

    el("div", { className: "tiles" },
      stat("Servers", String(list.length), "currently in"),
      stat("People reached", String(members), "across every server"),
      stat("Quiet for", String((STATE.muted || []).length), "people switched off")),

    connectionPanel(),

    panel("All servers", null,
      el("div", { className: "row spread", style: "margin-bottom:12px" }, box, count),
      rows)
  );

  paint();
}

// connection details
// Secrets come back as "set" or "not set", never the value.
function connectionPanel() {
  const s = STATE.setup || {};
  const ownerId = s.ownerId || "";
  const owners = ownerId ? 1 : 0;

  const appId = el("input", { value: s.appId || "", placeholder: "1234567890123456789",
                              inputmode: "numeric", spellcheck: false });
  const publicKey = el("input", { value: s.publicKey || "", placeholder: "64 hex characters",
                                  spellcheck: false });
  const token = el("input", { type: "password", placeholder: s.hasBotToken
      ? "already set - paste a new one to replace it" : "paste the bot token",
      autocomplete: "off", spellcheck: false });
  const clientSecret = el("input", { type: "password", placeholder: s.hasClientSecret
      ? "already set - paste a new one to replace it" : "optional, OAuth2 only",
      autocomplete: "off", spellcheck: false });
  const owner = el("input", { value: ownerId, placeholder: "123456789012345678",
                             inputmode: "numeric", spellcheck: false });

  const note = el("div", { className: "hint" });
  const save = el("button", { className: "btn btn-primary" }, "Save");

  const setNote = (text, bad = false) => {
    note.textContent = text;
    note.style.color = bad ? "var(--bad)" : "var(--ink-3)";
  };

  save.onclick = async () => {
    save.disabled = true;
    save.textContent = "Saving…";
    const problems = [];
    try {
      if (token.value.trim()) {
        await post("/setup/token", { token: token.value.trim() });
        token.value = "";
        problems.push("Token saved to .env - restart her to connect.");
      }
      if (appId.value.trim() || publicKey.value.trim() || clientSecret.value.trim()) {
        await post("/setup/discord", {
          appId: appId.value.trim(),
          publicKey: publicKey.value.trim(),
          clientSecret: clientSecret.value.trim(),
        });
        clientSecret.value = "";
        problems.push("Portal details saved.");
      }
      if (owner.value.trim() !== ownerId) {
        if (!owner.value.trim()) {
          problems.push("Owner ID cannot be cleared here - edit config.json to remove it.");
        } else {
          await post("/setup/owner", { userId: owner.value.trim() });
          problems.push("Owner updated.");
        }
      }
      STATE.setup = await api("/setup");
      setNote(problems.length ? problems.join(" ") : "Nothing changed.");
    } catch (e) {
      setNote(e.message, true);
    } finally {
      save.disabled = false;
      save.textContent = "Save";
    }
  };

  // The invite link needs an application id.
  const appIdForLink = s.appId || "";
  const invite = appIdForLink
    ? el("div", { className: "row" },
        el("code", { className: "hint" },
          `https://discord.com/oauth2/authorize?client_id=${appIdForLink}&scope=bot%20applications.commands&permissions=202752`),
        el("button", {
          className: "btn btn-sm",
          onclick: async () => {
            try {
              await navigator.clipboard.writeText(
                `https://discord.com/oauth2/authorize?client_id=${appIdForLink}&scope=bot%20applications.commands&permissions=202752`);
              toast("Invite link copied");
            } catch { toast("Could not reach the clipboard - select the text and copy it", true); }
          },
        }, "Copy invite link"))
    : el("p", { className: "hint" }, "Add an application id above and the invite link appears here.");

  const picture = el("div", { className: "row" });
  picture.append(
    el("span", { className: "wiz-shot", hidden: !s.hasPortrait },
      s.hasPortrait ? el("img", { src: "/portrait", alt: "", decoding: "async",
                                  onerror: function () { this.closest(".wiz-shot").hidden = true; } }) : null),
    el("label", { className: "btn btn-sm", for: "conn-photo" },
      s.hasPortrait ? "Replace picture" : "Add a picture"),
    el("input", {
      type: "file", id: "conn-photo", hidden: true,
      accept: "image/png,image/jpeg,image/webp,image/gif",
    }));

  const photoFile = picture.querySelector("#conn-photo");
  photoFile.onchange = async () => {
    const file = photoFile.files[0];
    if (!file) return;
    if (file.size > 8 * 1024 * 1024) return toast("That image is over 8 MB", true);
    const dataUrl = await new Promise(resolve => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result || ""));
      reader.onerror = () => resolve("");
      reader.readAsDataURL(file);
    });
    if (!dataUrl) return;
    try {
      const res = await post("/setup/portrait", { data: dataUrl });
      toast(res.pushedToDiscord ? "Picture updated" : "Picture saved - push it when she is connected");
      STATE.setup = await api("/setup");
      render();
    } catch (e) { toast(e.message, true); }
  };

  return panel("Connection", s.connected
      ? `connected as ${s.discordUsername || "the bot"}`
      : "not connected",
    picture,
    el("div", { className: "grid" },
      field("Application id", appId, "Developer Portal → General Information. Public, not a secret."),
      field("Public key", publicKey, "Used to verify interaction signatures. Public."),
      field("Bot token", token, "Developer Portal → Bot → Reset Token. Written to .env, never shown again."),
      field("Client secret", clientSecret, "OAuth2 only, and never needed to log in."),
      field("Your Discord user id", owner,
        `Makes you the owner of !secret.me, !affinity and !wipe. Currently ${owners ? "set" : "nobody"}.`)),
    el("div", { className: "row" }, save, note),
    invite);
}

// editors
// keep reopens with the text already in the box.
async function openEditor(path, keep) {
  // A re-render must not lose half-typed edits.
  if (keep === undefined && EDITING && EDITING.path === path) keep = EDITING.content;
  const isNamed = ["persona.md", "config.json", "stickers.json"].includes(path);
  const url = isNamed
    ? `/named/${path === "persona.md" ? "persona" : path.replace(".json", "")}`
    : `/file/${encodeURIComponent(path)}`;
  let data;
  if (typeof keep === "string") data = { content: keep };
  else {
    try { data = await api(url); } catch (e) { return toast(e.message, true); }
  }

  const area = el("textarea", { className: "editor-area", value: data.content, spellcheck: false });
  const status = el("span", { className: "hint" });
  const dirtyChip = el("span", { className: "chip warn", hidden: true }, "unsaved");
  const saveBtn = el("button", { className: "btn btn-primary" }, "Save");
  // From here on a re-render must bring the editor back rather than replace it.
  EDITING = { path, content: data.content };

  const unsaved = () => { dirtyChip.hidden = area.value === data.content; };
  area.oninput = () => { EDITING = { path, content: area.value }; unsaved(); };

  const save = async () => {
    saveBtn.disabled = true;
    try {
      const res = await api(url, { method: "PUT", body: JSON.stringify({ content: area.value }) });
      toast(`Saved ${res.saved}${res.restartNeeded ? " - restart the bot to apply config" : ""}`);
      EDITING = null;
      data.content = area.value;
      dirtyChip.hidden = true;
      status.textContent = "Saved.";
      STATE = await api("/");
    } catch (e) {
      toast(e.message, true);
    } finally {
      saveBtn.disabled = false;
    }
  };
  saveBtn.onclick = save;
  SAVING = save;

  const copyBtn = el("button", { className: "btn btn-ghost" }, "Copy");
  copyBtn.onclick = async () => {
    try {
      await navigator.clipboard.writeText(area.value);
      toast("Copied to the clipboard");
    } catch {
      toast("Could not reach the clipboard - select the text and copy it", true);
    }
  };

  const back = el("button", { className: "btn btn-ghost" }, "Back");
  back.onclick = () => {
    EDITING = null;
    SAVING = null;
    if (path.startsWith("skills/")) VIEW = "skills";
    else if (path.startsWith("memory/")) VIEW = "people";
    else if (path === "persona.md") VIEW = "persona";
    else if (path === "config.json") VIEW = "config";
    else VIEW = "stickers";
    navigateTo(VIEW);
  };

  const delBtn = el("button", { className: "btn btn-danger" }, "Delete");
  delBtn.title = "Delete this file. A backup is written before a reset, but not before this.";
  delBtn.onclick = async () => {
    if (!confirm(`Delete ${path}?`)) return;
    try {
      await api(`/file/${encodeURIComponent(path)}`, { method: "DELETE" });
      toast(`Deleted ${path}`);
      EDITING = null;
      SAVING = null;
      STATE = await api("/");
      render();
    } catch (e) { toast(e.message, true); }
  };

  const v = $("#view");
  swap(v,
    head(path, "Edit this file directly. Ctrl-S saves."),
    panel("Editor", `${area.value.length.toLocaleString()} characters`,
      area,
      path === "config.json"
        ? el("div", { className: "notice info", style: "margin:12px 0 0" }, el("span", { className: "dot" }),
            el("div", {}, "config.json is re-read on restart. Edits to persona, skills and stickers apply on the next message."))
        : null,
      el("div", { className: "bar" },
        saveBtn,
        copyBtn,
        dirtyChip,
        el("span", { className: "spacer" }),
        status,
        path.startsWith("skills/") ? delBtn : null,
        back)),
    ...STATE.skills.filter(x => path === `skills/${x.file}`).map(x =>
      panel("Skill frontmatter", x.keywords.join(", ") || "no keywords",
        el("p", { className: "hint mb-0" }, x.always
          ? "always: true, so it is pinned to every message she sees."
          : "Matched by keyword, so it only appears in the conversations that need it.")))
  );
}

function persona(v) { openEditor("persona.md"); }
function configView(v) { openEditor("config.json"); }

// model
// The open provider survives a re-render.
let MODEL_EDITING = null;

function modelSettings(v) {
  const providers = (STATE.models && STATE.models.providers) || [];
  const choices = (STATE.models && STATE.models.choices) || [];
  const current = STATE.model;
  const active = providers.find(p => p.id === current) || choices.find(c => c.id === current);
  const listBox = el("div", { className: "provider-list" });

  const setPrimary = async id => {
    try {
      await post("/model", { model: id });
      STATE = await api("/");
      fillModels();
      render();
      toast(`Primary: ${id}`);
    } catch (e) { toast(e.message, true); }
  };

  const removeProvider = async provider => {
    if (!confirm(`Remove the "${provider.name || provider.id}" provider?`)) return;
    try {
      await api(`/provider/${encodeURIComponent(provider.id)}`, { method: "DELETE" });
      STATE = await api("/");
      if (MODEL_EDITING === provider.id) MODEL_EDITING = null;
      fillModels();
      render();
      toast("Provider removed");
    } catch (e) { toast(e.message, true); }
  };

  const providerForm = (entry, isNew) => {
    const idIn = el("input", { value: entry.id || "", placeholder: "gemini", disabled: !isNew, spellcheck: false });
    const nameIn = el("input", { value: entry.name || "", placeholder: "Gemini", spellcheck: false });
    const urlIn = el("input", { value: entry.baseUrl || "", placeholder: "https://api.example.com/v1", spellcheck: false });
    const modelIn = el("input", { value: entry.model || "", placeholder: "model-name", spellcheck: false });
    const envIn = el("input", { value: entry.apiKeyEnv || "", placeholder: "PROVIDER_API_KEY", spellcheck: false });
    const prefixIn = el("input", { value: entry.apiKeyEnvPrefix || "", placeholder: "PROVIDER_API_KEY_", spellcheck: false });
    const keyIn = el("input", {
      type: "password", value: "", autocomplete: "off", spellcheck: false,
      placeholder: entry.hasKey ? "a key is saved - type to replace it" : "paste the API key",
    });
    let enabled = entry.enabled !== false;
    const enabledSw = toggleSwitch(enabled, on => { enabled = on; }, "enabled");

    const picker = el("select", { hidden: true, "aria-label": "Fetched models" });
    picker.onchange = () => { if (picker.value) modelIn.value = picker.value; };

    const fetchBtn = el("button", { className: "btn btn-sm" }, "Fetch models");
    fetchBtn.title = "Ask this provider which models it serves";
    fetchBtn.onclick = async () => {
      fetchBtn.disabled = true;
      fetchBtn.textContent = "Fetching…";
      try {
        const res = await post("/provider/models", {
          id: isNew ? "" : entry.id,
          name: nameIn.value, baseUrl: urlIn.value,
          apiKeyEnv: envIn.value, apiKeyEnvPrefix: prefixIn.value,
          apiKey: keyIn.value,
        });
        const models = res.models || [];
        picker.replaceChildren(
          el("option", { value: "" }, `${models.length} model${models.length === 1 ? "" : "s"} - pick one`),
          ...models.map(m => el("option", { value: m }, m)));
        picker.hidden = models.length === 0;
        toast(`${models.length} models from ${nameIn.value || entry.id}`);
      } catch (e) { toast(e.message, true); }
      finally { fetchBtn.disabled = false; fetchBtn.textContent = "Fetch models"; }
    };

    const saveBtn = el("button", { className: "btn btn-primary" }, isNew ? "Add provider" : "Save provider");
    saveBtn.onclick = async () => {
      saveBtn.disabled = true;
      try {
        await post("/provider", {
          id: idIn.value.trim(), name: nameIn.value.trim(),
          baseUrl: urlIn.value.trim(), model: modelIn.value.trim(),
          apiKeyEnv: envIn.value.trim(), apiKeyEnvPrefix: prefixIn.value.trim(),
          enabled, apiKey: keyIn.value,
        });
        STATE = await api("/");
        MODEL_EDITING = null;
        fillModels();
        render();
        toast("Provider saved");
      } catch (e) { toast(e.message, true); saveBtn.disabled = false; }
    };

    const cancelBtn = el("button", { className: "btn btn-ghost" }, "Cancel");
    cancelBtn.onclick = () => { MODEL_EDITING = null; paint(); };

    return el("div", { className: "prov-form" },
      field("Name", nameIn),
      field("Provider id", idIn, "The id used in config.json and by the primary picker."),
      field("Base URL", urlIn, "The OpenAI-compatible endpoint. /v1 is added to the chat URL when you leave it off."),
      field("Model", modelIn, "Type it, or press Fetch models and choose from the list."),
      field("API key variable", envIn, "The .env name this provider reads its key from."),
      field("Numbered key prefix", prefixIn, "Optional: PREFIX_1, PREFIX_2 ... become extra keys."),
      field("API key", keyIn, "Saved to .env, never to config.json, and never shown again."),
      el("div", { className: "prov-tools" },
        picker, fetchBtn,
        el("span", { className: "spacer" }),
        el("span", { className: "hint" }, "Enabled"),
        enabledSw,
        cancelBtn, saveBtn));
  };

  const providerCard = provider => {
    const editing = MODEL_EDITING === provider.id;
    const primary = provider.id === current;
    const useBtn = el("button", { className: "btn btn-sm" }, "Use");
    useBtn.disabled = primary;
    useBtn.onclick = () => setPrimary(provider.id);

    const editBtn = el("button", { className: "btn btn-sm" }, editing ? "Close" : "Edit");
    editBtn.onclick = () => { MODEL_EDITING = editing ? null : provider.id; paint(); };

    const delBtn = el("button", { className: "btn btn-sm btn-danger" }, "Remove");
    delBtn.onclick = () => removeProvider(provider);

    return el("div", { className: `prov${editing ? " open" : ""}` },
      el("div", { className: "prov-head" },
        logoNode(provider),
        el("div", { className: "prov-body" },
          el("div", { className: "prov-name" },
            el("span", {}, provider.name || provider.id),
            primary ? chip("primary", "accent") : null,
            provider.enabled === false ? chip("disabled", "warn") : null),
          el("div", { className: "prov-meta" },
            el("code", {}, provider.id), " · ", provider.model || "no model set",
            " · ", provider.baseUrl)),
        el("div", { className: "prov-actions" },
          chip(provider.keyCount ? `${provider.keyCount} key${provider.keyCount === 1 ? "" : "s"}` : "no key",
            provider.keyCount ? "ok" : "warn"),
          useBtn, editBtn, delBtn)),
      editing ? providerForm(provider, false) : null);
  };

  function paint() {
    const cards = providers.map(providerCard);
    if (MODEL_EDITING === "__new__") {
      cards.unshift(el("div", { className: "prov open" },
        el("div", { className: "prov-head" },
          el("span", { className: "prov-mark" }, "+"),
          el("div", { className: "prov-body" },
            el("div", { className: "prov-name" }, el("span", {}, "New provider")),
            el("div", { className: "prov-meta" }, "Fill this in, then Fetch models or Save."))),
        providerForm({}, true)));
    }
    listBox.replaceChildren(...(cards.length
      ? cards
      : [empty("No providers yet", "Add one so she has a model to think through.")]));
  }

  const addBtn = el("button", { className: "btn btn-primary" }, "Add provider");
  addBtn.onclick = () => { MODEL_EDITING = MODEL_EDITING === "__new__" ? null : "__new__"; paint(); };

  const primarySel = el("select", { "aria-label": "Primary provider" },
    ...providers.map(p => el("option", { value: p.id }, `${p.name || p.id} — ${p.model || "no model"}`)));
  if (current) primarySel.value = current;
  const usePrimary = el("button", { className: "btn btn-primary" }, "Make primary");
  usePrimary.onclick = () => setPrimary(primarySel.value);

  paint();

  add(v,
    head("Model & providers",
      "Add or edit the OpenAI-compatible APIs she thinks through. API keys go to .env; everything else is saved to config.json and applies to new requests right away."),

    panel("Primary provider", null,
      el("div", { className: "row spread", style: "align-items:flex-end; gap:14px" },
        el("div", { className: "grow" },
          el("div", { className: "hint", style: "margin-bottom:4px" }, "SHE TALKS THROUGH"),
          el("div", { className: "model-hero" },
          active
            ? el("span", { className: "model-hero-row" },
                logoNode(active),
                el("span", {}, active.name || active.id))
            : "No provider"),
          active ? el("div", { className: "hint" }, active.model || "") : null),
        el("div", { className: "row", style: "gap:8px" }, primarySel, usePrimary))),

    panel("Providers", `${providers.length} configured`,
      notice("API keys are written to ", el("code", {}, ".env"), " and never to config.json. This page never reads a key back."),
      el("div", { className: "row spread", style: "margin:12px 0" },
        el("span", { className: "hint" }, "A fetched model list is a one-off lookup - nothing is saved until you press Save."),
        addBtn),
      listBox)
  );
}

// presence
// The one place that knows the placeholder vocabulary.
const PRESENCE_PLACEHOLDERS = ["{name}", "{prefix}", "{people}", "{servers}", "{activity}"];
const PRESENCE_PRESETS = [
  { label: "Watching the server", type: "watching", name: "the server", details: "{activity}", state: "{servers} · {prefix}help for commands" },
  { label: "Listening", type: "listening", name: "lo-fi beats", details: "{activity}", state: "{prefix}help if you need me" },
  { label: "Competing", type: "competing", name: "Hmm, Interesting...", details: "{activity}", state: "{prefix}help for commands" },
  { label: "Custom status", type: "custom", name: "curious", details: "", state: "" },
];
let PRESENCE_FOCUS = null;

// Drawn from the form, so it updates as you type.
function presenceCard(p) {
  const status = p.status || "online";
  const detail = resolvePresenceText(p.details);
  const line = resolvePresenceText(p.state);
  const title = p.type === "custom"
    ? (resolvePresenceText(p.name) || "custom status")
    : `${cap(p.type || "playing")} ${resolvePresenceText(p.name) || ""}`.trim();
  return [
    el("div", { className: "presence-banner" }),
    el("div", { className: "presence-avatar" },
      STATE.bot.name.charAt(0).toUpperCase(),
      portraitNode(),
      el("span", { className: `presence-dot ${status}` })),
    el("div", { className: "presence-name" },
      STATE.bot.name,
      el("span", { className: "presence-tag" }, "BOT"),
      el("span", { className: `presence-status ${status}` }, status)),
    el("div", { className: "presence-activity" },
      el("div", { className: "presence-art-large" }, artNode(p.largeImage, STATE.bot.name.charAt(0).toUpperCase())),
      el("div", { className: "presence-lines" },
        el("div", { className: "presence-title" }, title || "…"),
        detail ? el("div", { className: "presence-line" }, detail) : null,
        line ? el("div", { className: "presence-line" }, line) : null,
        p.smallImage
          ? el("div", { className: "presence-art-small", title: resolvePresenceText(p.smallText) || "" },
              artNode(p.smallImage, "•"))
          : null)),
  ];
}

// usage
// Costs come from usage/*.jsonl; unpriced calls make the total a floor.
let USAGE_DAYS = 30;

function usageBars(byDay, days) {
  // Day keys are UTC, as the recorder buckets them.
  const byDate = new Map(byDay.map(d => [d.day, d]));
  const now = new Date();
  const keys = [];
  const cursor = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate()));
  for (let i = days - 1; i >= 0; i--) {
    const d = new Date(cursor);
    d.setUTCDate(d.getUTCDate() - i);
    keys.push(d.toISOString().slice(0, 10));
  }

  // Trim to recorded days; a real quiet day keeps a stub.
  const firstKey = byDay.length ? byDay[0].day : null;
  const from = firstKey ? keys.indexOf(firstKey) : -1;
  // -1 means recording starts after this window.
  const shown = from >= 0 ? keys.slice(from) : keys;

  const out = shown.map(key =>
    byDate.get(key) || { day: key, prompt: 0, completion: 0, total: 0, calls: 0, unpriced: 0 });
  const peak = Math.max(1, ...out.map(d => d.total || 0));
  const tallest = 100;
  return el("div", {},
    el("div", { className: "usage-chart" },
      ...out.map(day => {
        const prompt = day.prompt || 0, completion = day.completion || 0;
        const total = prompt + completion;
        const height = total ? Math.max(3, (total / peak) * tallest) : 2;
        const title = `${day.day} · ${total.toLocaleString()} tokens`
          + ` (in ${prompt.toLocaleString()}, out ${completion.toLocaleString()})`
          + ` · ${day.calls || 0} call(s)`;
        if (!total) {
          return el("div", { className: "ucol empty-day", title }, el("div", { className: "seg" }));
        }
        const completionH = total ? (completion / total) * height : 0;
        const promptH = height - completionH;
        return el("div", { className: "ucol", title },
          el("div", { className: "seg completion", style: `height:${completionH.toFixed(2)}px` }),
          el("div", { className: "seg prompt", style: `height:${promptH.toFixed(2)}px` }));
      })),
    el("div", { className: "usage-axis" },
      el("span", {}, out.length ? out[0].day.slice(5) : ""),
      el("span", {}, `${out.length} day(s) shown · peak ${shortNum(peak)}`),
      el("span", {}, out.length ? out[out.length - 1].day.slice(5) : "")));
}

function usageRow(name, sub, row, peak, { withCost = true } = {}) {
  const priced = row.unpriced === 0 && row.calls > 0;
  return el("div", { className: "usage-row" },
    el("div", { className: "who" },
      el("b", {}, name),
      sub ? el("span", { className: "hint" }, sub) : null,
      el("div", { className: "usage-share" },
        el("i", { style: `width:${peak ? Math.max(2, (row.total / peak) * 100).toFixed(1) : 0}%` }))),
    el("div", { className: "num-cell", title: `${(row.prompt || 0).toLocaleString()} prompt tokens` },
      shortNum(row.prompt)),
    el("div", { className: "num-cell", title: `${(row.completion || 0).toLocaleString()} completion tokens` },
      shortNum(row.completion)),
    el("div", { className: "num-cell", title: `${(row.total || 0).toLocaleString()} tokens total` },
      shortNum(row.total)),
    el("div", { className: "num-cell", title: `${row.calls || 0} call(s), ${ms(row.avgLatencyMs)} average` },
      `${row.calls || 0} · ${ms(row.avgLatencyMs)}`),
    withCost
      ? el("div", { className: "num-cell money", title: priced ? "priced in config.json" : "no price configured for this model" },
          money(row.cost, priced))
      : null);
}

// Her rhythm: what summing a column cannot tell you.
function usageFacts(d, u) {
  if (!d || !u || !u.totals || !u.totals.calls) return null;
  const when = ts => (ts ? new Date(ts).toLocaleString(undefined, {
    month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
  }) : "—");
  const rows = [
    kv("tokens per call", shortNum(d.tokensPerCall)),
    // Includes prompt processing, so it reads low.
    kv("output tok/s", `${d.outPerSecond} (incl. prompt)`),
    kv("prompt : completion",
      `${((u.totals.prompt || 0) / Math.max(1, u.totals.completion || 0)).toFixed(1)} : 1`),
    d.peakDay ? kv("busiest day", `${d.peakDay.day} · ${shortNum(d.peakDay.total)} tokens`) : null,
    d.busiestHour
      ? kv("busiest hour", `${String(d.busiestHour.hour).padStart(2, "0")}:00`
          + `–${String((d.busiestHour.hour + 1) % 24).padStart(2, "0")}:00`
          + ` · ${d.busiestHour.calls} call(s) over ${d.busiestHour.hoursActive}h active`)
      : null,
    kv("first call", when(d.firstTs)),
    kv("last call", when(d.lastTs)),
    kv("priced calls", `${d.pricedCalls} of ${u.totals.calls}`),
  ].filter(Boolean);
  return el("div", { className: "kv" }, ...rows);
}

// The same table shape, pointed at days.
function usageDayTable(byDay) {
  const days = (byDay || []).filter(d => d.calls > 0);
  if (!days.length) return null;
  const peak = Math.max(1, ...days.map(d => d.total || 0));
  const weekday = key => {
    const d = new Date(`${key}T00:00:00Z`);
    return Number.isNaN(d.getTime()) ? "" : d.toLocaleDateString(undefined, {
      weekday: "short", timeZone: "UTC",
    });
  };
  return el("div", { className: "usage-list" },
    el("div", { className: "usage-row head" },
      el("div", {}, "Day"),
      el("div", { className: "num-cell" }, "in"),
      el("div", { className: "num-cell" }, "out"),
      el("div", { className: "num-cell" }, "total"),
      el("div", { className: "num-cell" }, "calls · avg"),
      el("div", { className: "num-cell" }, "cost")),
    ...days.map(day => usageRow(day.day, weekday(day.day), day, peak)));
}

function usageView(v) {
  const u = STATE.usage || {};
  const t = u.totals || { calls: 0, prompt: 0, completion: 0, total: 0, cost: 0, avgLatencyMs: 0 };
  const byProvider = u.byProvider || [];
  const peakProvider = Math.max(0, ...byProvider.map(p => p.total || 0));
  const complete = u.costComplete !== false && t.calls > 0;

  const windows = el("div", { className: "row", role: "group", "aria-label": "time window" },
    ...[1, 7, 14, 30, 90, 365].map(days =>
      el("button", {
        className: `btn btn-sm${USAGE_DAYS === days ? " btn-primary" : " btn-ghost"}`,
        onclick: async () => {
          if (USAGE_DAYS === days) return;
          USAGE_DAYS = days;
          try {
            STATE.usage = await api(`/api/usage?days=${days}`);
            render();
          } catch (e) { toast(e.message, true); }
        },
      }, days === 1 ? "today" : `${days}d`)));

  const refresh = async () => {
    try {
      STATE.usage = await api(`/api/usage?days=${USAGE_DAYS}`);
      render();
      toast("usage refreshed");
    } catch (e) { toast(e.message, true); }
  };

  const confirmIn = el("input", { placeholder: "type CLEAR to confirm", style: "display:none" });
  const clearBtn = el("button", { className: "btn btn-danger btn-sm" }, "Clear history");
  let armed = false;
  clearBtn.onclick = async () => {
    if (!armed) {
      armed = true;
      confirmIn.style.display = "";
      confirmIn.value = "";
      clearBtn.textContent = "Confirm";
      confirmIn.focus();
      return;
    }
    clearBtn.disabled = true;
    try {
      const out = await api("/api/usage/clear", { method: "POST" });
      STATE.usage = out.usage;
      toast(`removed ${out.removed} day file(s)`);
      render();
    } catch (e) { toast(e.message, true); }
  };
  confirmIn.oninput = () => { clearBtn.disabled = confirmIn.value.trim().toUpperCase() !== "CLEAR"; };
  confirmIn.onkeydown = e => { if (e.key === "Enter" && !clearBtn.disabled) clearBtn.click(); };

  // An all-unpriced window means unknown, not $0.
  const d = u.derived || {};
  const pricedCalls = Number.isFinite(d.pricedCalls) ? d.pricedCalls : 0;
  const somePriced = pricedCalls > 0;
  const costText = money(t.cost, t.calls > 0 && somePriced);
  const costSub = !somePriced
    ? `unknown · no price set for ${t.calls} call(s)`
    : complete
      ? `${u.pricedModels.length} priced model(s)`
      : `floor — ${t.unpriced} of ${t.calls} call(s) had no price`;
  const splitTotal = Math.max(1, (t.prompt || 0) + (t.completion || 0));

  add(v,
    head("Usage",
      "What every model call cost, in tokens and money. One line per call is written to usage/*.jsonl as she answers; nothing here is estimated.",
      windows,
      el("button", { className: "btn btn-sm", onclick: refresh }, "Refresh"),
      clearBtn,
      confirmIn),

    !u.enabled
      ? notice(el("b", {}, "Usage counting is off in config.json"), " — set usage.enabled to true and it starts recording again.")
      : null,

    u.calls === 0 && !t.calls
      ? empty("No calls recorded yet",
          el("p", { className: "hint mb-0" }, "She has not used a model since this was switched on, or the history was cleared."))
      : null,

    t.calls
      ? el("div", { className: "tiles" },
          stat("Tokens", shortNum(t.total), `${shortNum(t.prompt)} in · ${shortNum(t.completion)} out`),
          stat("Cost", costText, costSub),
          stat("Calls", String(t.calls), `over ${u.days} day(s)`),
          stat("Average latency", ms(t.avgLatencyMs),
            t.cached ? `${shortNum(t.cached)} cached prompt tokens` : "no cached prompt tokens"),
          t.reasoning
            ? stat("Reasoning tokens", shortNum(t.reasoning), "counted inside completion")
            : null)
      : null,

    t.calls && !somePriced
      ? notice(el("b", {}, "Cost is unknown, not zero"),
          ` — none of the ${t.calls} call(s) in this window used a model with a price in `,
          el("code", {}, "usage.prices"),
          ". Token counts are real and unaffected; add a price and the spend shows up here.")
      : t.calls && !complete
        ? notice(el("b", {}, "The cost total is a floor"),
            ` — ${t.unpriced} of ${t.calls} call(s) in this window used a model with no price configured.`)
        : null,

    t.calls
      ? panel("Where the tokens go", `${u.days} day window`,
          el("div", { className: "usage-split", title: `${t.prompt} prompt, ${t.completion} completion` },
            el("i", {
              className: "prompt",
              style: `width:${((t.prompt || 0) / splitTotal * 100).toFixed(2)}%`,
            }),
            el("i", {
              className: "completion",
              style: `width:${((t.completion || 0) / splitTotal * 100).toFixed(2)}%`,
            })),
          el("div", { className: "usage-legend", style: "margin-top:9px" },
            el("span", {}, el("i", { className: "prompt" }), `prompt ${(t.prompt || 0).toLocaleString()}`),
            el("span", {}, el("i", { className: "completion" }), `completion ${(t.completion || 0).toLocaleString()}`)))
      : null,

    t.calls
      ? panel("Daily", `${(u.byDay || []).length} day(s) with calls`, usageBars(u.byDay || [], u.days))
      : null,

    t.calls
      ? panel("Every day", `${(u.byDay || []).filter(x => x.calls > 0).length} day(s) with calls`,
          usageDayTable(u.byDay))
      : null,

    t.calls
      ? panel("Her rhythm", "measured from every call in the window", usageFacts(d, u))
      : null,

    byProvider.length
      ? panel("By provider", `${byProvider.length} provider(s)`,
          el("div", { className: "usage-list" },
            el("div", { className: "usage-row head" },
              el("div", {}, "Provider"),
              el("div", { className: "num-cell" }, "in"),
              el("div", { className: "num-cell" }, "out"),
              el("div", { className: "num-cell" }, "total"),
              el("div", { className: "num-cell" }, "calls · avg"),
              el("div", { className: "num-cell" }, "cost")),
            ...byProvider.map(p => usageRow(p.provider, null, p, peakProvider))))
      : null,

    (u.bySource || []).length
      ? panel("What she spent it on", "which part of her was thinking",
          el("div", { className: "usage-list" },
            el("div", { className: "usage-row head" },
              el("div", {}, "Purpose"),
              el("div", { className: "num-cell" }, "in"),
              el("div", { className: "num-cell" }, "out"),
              el("div", { className: "num-cell" }, "total"),
              el("div", { className: "num-cell" }, "calls · avg"),
              el("div", { className: "num-cell" }, "cost")),
            ...u.bySource.map(s => usageRow(
              SOURCE_LABEL[s.source] || s.source, null, s, peakProvider))))
      : null,

    (u.byModel || []).length
      ? panel("By model", `${u.byModel.length} model(s) used`,
          el("div", { className: "usage-list" },
            el("div", { className: "usage-row head" },
              el("div", {}, "Model"),
              el("div", { className: "num-cell" }, "in"),
              el("div", { className: "num-cell" }, "out"),
              el("div", { className: "num-cell" }, "total"),
              el("div", { className: "num-cell" }, "calls · avg"),
              el("div", { className: "num-cell" }, "cost")),
            ...u.byModel.map(m => usageRow(m.model, null, m, peakProvider))))
      : null,

    (u.recent || []).length
      ? panel("Recent calls", `last ${u.recent.length}`, el("div", { className: "log-box" },
          ...u.recent.map(r => el("div", { className: "log-row" },
            el("span", { className: "log-t" }, new Date(r.ts).toLocaleTimeString()),
            el("span", { className: "log-l" }, SOURCE_LABEL[r.source] || r.source || "call"),
            el("span", { className: "log-m" },
              `${r.provider} / ${r.model} — ${(r.total || 0).toLocaleString()} tokens`
              + ` (${(r.prompt || 0).toLocaleString()} in, ${(r.completion || 0).toLocaleString()} out)`
              + ` · ${money(r.cost, r.cost !== null && r.cost !== undefined)}`
              + ` · ${ms(r.latencyMs)}${r.streamed ? " · streamed" : ""}`)))))
      : null,

    panel("Reading these numbers", null,
      el("div", { className: "grid gauto" },
        el("p", { className: "hint mb-0" },
          "Tokens come from the provider's own usage block, so they are what was billed. ",
          "Cached prompt tokens are counted separately because they are billed at their own rate."),
        el("p", { className: "hint mb-0" },
          "Cost only appears for models priced in config.json under ",
          el("code", {}, "usage.prices"),
          " — dollars per million tokens, as ",
          el("code", {}, '{"provider:model": {"in": 3.0, "out": 15.0}}'),
          ". Unpriced models still show their tokens."),
        el("p", { className: "hint mb-0" },
          "History is kept for ",
          `${u.keepDays || 90} day(s) in usage/*.jsonl, one file per day. Clearing removes those files and nothing else.`)))
  );
}

function presenceSettings(v) {
  const cfg = STATE.presence.config || {};
  const status = el("select", {},
    ...["online", "idle", "dnd", "invisible"].map(value => el("option", { value }, value === "dnd" ? "Do not disturb" : value)));
  const type = el("select", {},
    ...["playing", "watching", "listening", "competing", "custom"].map(value => el("option", { value }, value)));
  const name = el("input", { value: cfg.name || "", maxLength: 128, placeholder: "the server" });
  const details = el("input", { value: cfg.details || "", maxLength: 128, placeholder: "{activity}" });
  const state = el("input", { value: cfg.state || "", maxLength: 128, placeholder: "{servers} · {prefix}help for commands" });
  const largeImage = el("input", { value: cfg.largeImage || "", maxLength: 128, placeholder: "yuyu_main or https://…/art.png" });
  const largeText = el("input", { value: cfg.largeText || "", maxLength: 128, placeholder: "Yuyu" });
  const smallImage = el("input", { value: cfg.smallImage || "", maxLength: 128, placeholder: "online_badge" });
  const smallText = el("input", { value: cfg.smallText || "", maxLength: 128, placeholder: "Online" });
  status.value = cfg.status || "online";
  type.value = cfg.type || "watching";

  const read = () => ({
    status: status.value, type: type.value, name: name.value,
    details: details.value, state: state.value,
    largeImage: largeImage.value, largeText: largeText.value,
    smallImage: smallImage.value, smallText: smallText.value,
  });

  const preview = el("div", { className: "presence-preview" });
  const paint = () => swap(preview, ...presenceCard(read()));

  [status, type].forEach(select => { select.onchange = paint; });
  [name, details, state, largeImage, largeText, smallImage, smallText].forEach(input => {
    input.oninput = paint;
    input.onfocus = () => { PRESENCE_FOCUS = input; };
  });
  paint();

  const applyPreset = preset => {
    status.value = preset.status; type.value = preset.type;
    name.value = preset.name; details.value = preset.details; state.value = preset.state;
    paint();
  };

  const save = el("button", { className: "btn btn-primary", title: "Save and apply this presence to Discord" }, "Apply presence");
  save.onclick = async () => {
    save.disabled = true;
    save.textContent = "Applying…";
    try {
      const result = await api("/presence", { method: "POST", body: JSON.stringify(read()) });
      STATE = await api("/");
      render();
      toast(result.applied ? "Discord presence updated" : `Saved; ${result.why || "presence applies when the bot reconnects"}`);
    } catch (e) {
      toast(e.message, true);
      save.disabled = false;
    } finally {
      save.textContent = "Apply presence";
    }
  };

  add(v,
    head("Discord presence", "Customize what people see on her Discord profile. Changes save to config and apply immediately while the bot is connected."),

    panel("Live preview", null,
      preview,
      el("p", { className: "hint", style: "margin-top:10px" },
        "Placeholders are filled in as Discord shows them. ",
        el("code", {}, "{activity}"), " is the one the running bot supplies.")),

    panel("Presets", "start from one of these, then tweak",
      el("div", { className: "preset-row" },
        ...PRESENCE_PRESETS.map(preset => {
          const b = el("button", { className: "btn btn-sm" }, preset.label);
          b.onclick = () => applyPreset(preset);
          return b;
        }))),

    panel("Activity", "every field below is what she pushes to Discord",
      el("div", { className: "grid g2" },
        field("Online status", status),
        field("Activity type", type),
        field("Activity name", name, "The short headline shown after Playing, Watching, Listening, or Competing."),
        field("Details", details, "First activity line. Supports {name}, {prefix}, {people}, {servers}, and {activity}."),
        field("State", state, "Second activity line. Supports the same placeholders."),
        field("Large image asset key", largeImage, "A rich-presence asset key uploaded in the Developer Portal, or a direct https image URL."),
        field("Large image hover text", largeText),
        field("Small image", smallImage, "Optional small badge: another registered asset key, or an https URL."),
        field("Small image hover text", smallText)),
      el("div", { className: "ph-chips" },
        el("span", { className: "hint" }, "Insert into the focused field:"),
        ...PRESENCE_PLACEHOLDERS.map(token => {
          const b = el("button", { className: "ph-chip", type: "button" }, token);
          b.onclick = () => { insertInto(PRESENCE_FOCUS, token); paint(); };
          return b;
        })),
      el("div", { className: "row", style: "margin-top:16px" }, save))
  );
}

// stickers
function stickers(v) {
  const s = STATE.stickers || {};
  const fileIn = el("input", { type: "file", accept: "image/*", style: "display:none" });
  fileIn.onchange = async () => {
    const file = fileIn.files[0];
    if (!file) return;
    const data = await new Promise((res) => {
      const reader = new FileReader();
      reader.onload = () => res(reader.result);
      reader.readAsDataURL(file);
    });
    try {
      await api("/sticker", { method: "POST", body: JSON.stringify({ name: file.name, data }) });
      toast(`Saved stickers/${file.name}`);
      STATE = await api("/");
      render();
    } catch (e) { toast(e.message, true); }
    fileIn.value = "";
  };

  const tiles = (s.files || []).map(f => el("div", { className: "sticker-item" },
    el("img", { src: f.url, alt: f.name }),
    el("b", {}, f.name),
    el("span", {}, `${Math.round(f.bytes / 1024)} KB`),
    el("button", { className: "btn btn-sm btn-danger", onclick: async ev => {
      ev.stopPropagation();
      if (!confirm(`Delete ${f.name}?`)) return;
      try {
        await api(`/sticker/${encodeURIComponent(f.name)}`, { method: "DELETE" });
        toast("Deleted");
        STATE = await api("/"); render();
      } catch (e) { toast(e.message, true); }
    } }, "Delete")
  ));

  add(v,
    head("Stickers", "Discord blocks bots from sending real stickers, so she sends the image as an attachment instead."),

    panel("Sticker images", `${(s.files || []).length} uploaded`,
      el("div", { className: "row", style: "margin-bottom:12px" },
        el("button", { className: "btn", onclick: () => fileIn.click() }, "Upload image"), fileIn),
      tiles.length
        ? el("div", { className: "sticker-grid" }, tiles)
        : empty("No images yet", "Upload sticker images to get started."),
      s.missing && s.missing.length
        ? el("div", { className: "notice", style: "margin:14px 0 0" }, el("span", { className: "dot" }),
            el("div", {}, "stickers.json references files that do not exist: ", el("code", {}, s.missing.join(", "))))
        : null),

    panel("Emoji fallback", "images win, emoji are the backup",
      el("div", { className: "grid gauto" },
        stat("Server emoji", String(s.serverEmoji ?? 0), "auto-discovered"),
        stat("Keyword reactions", String(s.emojiReactions ?? 0), "custom emoji"),
        stat("Keyword stickers", String(s.emojiStickers ?? 0), "custom emoji"),
        stat("Linked images", String((s.images || []).length), "in stickers.json")),
      el("p", { className: "hint", style: "margin-top:12px" },
        "Emoji are only used when no image matches, so images win by default.")),

    panel("stickers.json", null,
      el("pre", {}, el("code", {},
        'Reference an image with a "file" key, or a custom emoji with "emoji":\n' +
        '  { "file": "hug.png", "keywords": ["hug", "thanks"] }\n' +
        '  { "emoji": "<:pepe:123456>", "keywords": ["pepe"] }')),
      el("button", { className: "btn", onclick: () => openEditor("stickers.json") }, "Edit stickers.json"))
  );
}

// skills
function skills(v) {
  const area = el("textarea", { placeholder: "---\nname: My Skill\nkeywords: [a, b]\n---\n\nWhat it tells her to do.", spellcheck: false });
  const nameIn = el("input", { placeholder: "skill-name" });

  add(v,
    head("Skills", "Skills teach her new things. Always-on skills are pinned to every message."),

    panel("New skill", null, el("div", { className: "skill-new" },
      el("div", { className: "row" },
        nameIn,
        el("button", {
          className: "btn btn-primary",
          onclick: async () => {
            if (!nameIn.value.trim()) return toast("give it a name", true);
            try {
              await api("/skill", { method: "POST", body: JSON.stringify({ name: nameIn.value.trim(), content: area.value }) });
              toast(`Created skills/${nameIn.value.trim()}.md`);
              STATE = await api("/");
              nameIn.value = ""; area.value = "";
              render();
            } catch (e) { toast(e.message, true); }
          }
        }, "Create"),
        el("span", { className: "hint" }, "always: true in the frontmatter pins it to every message")),
      area)),

    panel("Installed", `${STATE.skills.length} skills`, el("div", { className: "files" },
      STATE.skills.map(s => el("a", {
        className: "file", href: "#",
        onclick: () => openEditor(`skills/${s.file}`),
      },
        el("b", {}, s.file),
        el("span", {}, s.always ? "always on" : s.keywords.length ? s.keywords.slice(0, 6).join(", ") : "no keywords")))))
  );
}

// people
function people(v) {
  if (!STATE.people.length) {
    v.append(
      head("People", "Nobody yet."),
      empty("No people yet", "When someone talks to her, a memory file is created here automatically.")
    );
    return;
  }

  add(v,
    head("People", "Everyone she knows. Facts are stored permanently in memory/*.md files, one per person.")
  );

  for (const p of STATE.people) {
    const m = STATE.memory[p.slug];
    const fields = SECTIONS
      .filter(k => (m?.[k] ?? []).length)
      .map(k => ({ name: `${SECTION_LABEL[k]} (${m[k].length})`, value: m[k].map(x => `• ${x}`).join("\n") }));

    v.append(panel(p.name, m?.updated ? `updated ${m.updated.slice(0, 16).replace("T", " ")}` : null,
      el("div", { className: "row spread" },
        el("div", { className: "row" },
          el("span", { className: "chip code" }, `memory/${p.slug}.md`),
          m?.discordId ? el("span", { className: "chip" }, `discord ${m.discordId}`) : null,
          m?.username ? el("span", { className: "chip" }, m.username) : null,
          chip(`${p.count} fact${p.count === 1 ? "" : "s"}`)),
        el("div", { className: "row" },
          muteToggle(p),
          el("button", { className: "btn btn-sm btn-ghost", onclick: () => openEditor(`memory/${p.slug}.md`) }, "Edit raw"))),
      fields.length
        ? el("div", { className: "grid g2", style: "margin-top:14px" },
            fields.map(f => el("div", { className: "fact-block" },
              el("div", { className: "k" }, f.name),
              el("div", { className: "v" }, f.value))))
        : el("p", { className: "hint", style: "margin-top:12px" }, "No facts stored yet.")
    ));
  }
}

// reset
// Outside the view, which is rebuilt after a reset.
let LAST_RESET = null;

function reset(v) {
  const chosen = new Set();
  const planBox = el("div", {}, el("p", { className: "hint mb-0" }, "Tick what to remove, then Preview."));
  const confirmIn = el("input", { placeholder: "type RESET to confirm", style: "display:none" });
  const goBtn = el("button", { className: "btn btn-danger" }, "Reset now");
  const learnedBtn = el("button", { className: "btn btn-ghost" }, "Select all learned data");

  const refresh = async () => { STATE = await api("/"); };

  const boxes = STATE.resetTargets.map(t => {
    const cb = el("input", { type: "checkbox" });
    const row = el("label", { className: "target" },
      cb,
      el("span", { className: "target-name" }, t.label),
      el("span", { className: "spacer" }),
      chip(t.tier, t.tier === "authored" ? "warn" : ""),
      t.optIn ? chip("opt-in") : null);
    cb.onchange = () => {
      if (cb.checked) chosen.add(t.key); else chosen.delete(t.key);
      confirmIn.style.display = "none";
      confirmIn.value = "";
      goBtn.disabled = true;
      planBox.replaceChildren(el("p", { className: "hint mb-0" }, "Tick what to remove, then Preview."));
    };
    return { t, row, cb };
  });

  learnedBtn.onclick = () => {
    for (const { t, cb } of boxes) {
      const on = t.tier === "learned" && !t.optIn;
      cb.checked = on;
      if (on) chosen.add(t.key); else chosen.delete(t.key);
    }
    confirmIn.style.display = "none"; confirmIn.value = ""; goBtn.disabled = true;
  };

  const doPreview = async () => {
    try {
      const plan = await api("/reset/preview", { method: "POST", body: JSON.stringify({ targets: [...chosen] }) });
      if (!plan.removed.length) {
        planBox.replaceChildren(el("p", { className: "hint mb-0" }, "Nothing selected."));
        goBtn.disabled = true;
        confirmIn.style.display = "none";
        return;
      }
      planBox.replaceChildren(
        el("div", { className: "plan" },
          el("h3", {}, "This will permanently delete:"),
          ...plan.removed.map(r => el("div", { style: "margin-bottom:10px" },
            el("div", { className: "row", style: "gap:8px" },
              el("b", {}, r.label),
              chip(`${r.count} ${r.unit}`)),
            r.detail?.length ? el("pre", {}, r.detail.join("\n")) : null)),
          el("div", { className: "hint" }, "Kept: " + plan.kept.join(" · ")),
          el("div", { className: "hint", style: "margin-top:8px" }, "A backup is written to backups/ first, so this is reversible."),
          plan.typedConfirmation
            ? el("div", { className: "chip warn", style: "margin-top:10px" },
                `This deletes hand-written files - type ${plan.typedConfirmation} to continue.`)
            : null));
      goBtn.disabled = false;
      confirmIn.style.display = plan.typedConfirmation ? "block" : "none";
    } catch (e) {
      planBox.replaceChildren(el("p", { className: "hint mb-0", style: "color:var(--bad)" }, e.message));
    }
  };

  goBtn.disabled = true;
  goBtn.onclick = async () => {
    goBtn.disabled = true;
    goBtn.textContent = "Working…";
    try {
      const res = await api("/reset/run", {
        method: "POST",
        body: JSON.stringify({ targets: [...chosen], confirm: confirmIn.value }),
      });
      LAST_RESET = res;
      toast("Reset complete");
      await refresh();
      buildNav();
      render();
    } catch (e) {
      LAST_RESET = null;
      planBox.replaceChildren(el("p", { className: "hint mb-0", style: "color:var(--bad)" }, e.message));
      toast(e.message, true);
    } finally {
      goBtn.textContent = "Reset now";
    }
  };

  const backupBox = el("div", { className: "hint" }, "loading backups…");
  (async () => {
    try {
      const { backups } = await api("/reset/backups");
      backupBox.replaceChildren(
        ...(backups.length
          ? backups.map(b => el("div", { className: "backup-item" },
              el("span", { className: "row" }, el("span", { className: "mono" }, b.id), chip(`${b.files?.length ?? 0} file(s)`)),
              el("button", { className: "btn btn-sm", onclick: async ev => {
                if (!confirm(`Restore ${b.id}? Current files with the same names are overwritten.`)) return;
                try {
                  const r = await api("/reset/restore", { method: "POST", body: JSON.stringify({ id: b.id }) });
                  toast(`Restored ${r.restored.length} file(s)`);
                  STATE = await api("/"); render();
                } catch (err) { toast(err.message, true); }
              } }, "Restore")))
          : [el("div", { className: "hint" }, "No backups yet. One is written automatically before every reset.")])
      );
    } catch (e) {
      backupBox.textContent = e.message;
    }
  })();

  const t = STATE.totals ?? {};
  add(v,
    head("Reset", "Wipe what she knows. A backup is always written first, so this is reversible."),

    LAST_RESET
      ? panel("Done", "most recent reset",
          el("div", { className: "row spread" },
            el("div", { className: "grow" },
              el("pre", { style: "max-height:120px" }, res_deleted_text(LAST_RESET)),
              LAST_RESET.backup
                ? el("div", { className: "hint" }, `Backup: backups/${LAST_RESET.backup.id} (${LAST_RESET.backup.files} file(s))`)
                : null,
              LAST_RESET.configRestartNeeded
                ? el("div", { className: "hint", style: "color:var(--warn)" }, "Restart the bot to pick up the remaining changes.")
                : null),
            el("button", { className: "btn btn-sm btn-ghost", onclick: () => { LAST_RESET = null; render(); } }, "Dismiss")))
      : null,

    el("div", { className: "tiles" },
      stat("Memory files", String(t.memoryFiles ?? 0), `${t.memoryFacts ?? 0} fact(s)`),
      stat("Affect records", String(t.affinityFiles ?? 0),
        STATE.affinity.crush ? `&#9733; ${STATE.affinity.crush.name}` : "no crush"),
      stat("Buffered channels", String(t.bufferedChannels ?? 0), `${t.bufferedMessages ?? 0} msg(s)`),
      stat("Skill files", String(t.skills ?? 0), "authored")),

    panel("Reset data", "applies to every server - memory is per person, not per server",
      el("p", { className: "hint" },
        el("b", {}, "learned"), " is data she picked up from conversations. ",
        el("b", {}, "authored"), " is content you or I wrote. ",
        el("b", {}, "opt-in"), " items are never swept by the bulk button - tick them on purpose."),
      el("div", { className: "row", style: "margin:12px 0 4px" }, learnedBtn),
      el("div", { className: "target-list" }, ...boxes.map(b => b.row)),
      el("div", { className: "row", style: "margin-top:14px" },
        el("button", { className: "btn", onclick: doPreview }, "Preview"),
        confirmIn,
        goBtn),
      planBox),

    panel("Backups", null, backupBox)
  );
}

function res_deleted_text(res) {
  return (res.deleted || []).join("\n") || "nothing to delete";
}

// logs
function logs(v) {
  const box = el("div", { className: "log-box" });
  const paint = () => {
    box.replaceChildren(
      ...STATE.logs.map(l => {
        const time = new Date(l.ts).toLocaleTimeString();
        return el("div", { className: "log-row" },
          el("span", { className: "log-t" }, time),
          el("span", { className: `log-l ${l.level}` }, l.level),
          el("span", { className: "log-m" }, l.text));
      })
    );
    if (!STATE.logs.length) box.textContent = "no log lines buffered yet";
  };

  const refresh = async () => {
    try { STATE = await api("/"); paint(); } catch (e) { toast(e.message, true); }
  };

  add(v,
    head("Logs", "Recent log lines from the running bot. Last 300 lines, kept in memory."),
    panel("Recent log lines", `${STATE.logs.length} buffered`,
      el("div", { className: "row spread", style: "margin-bottom:12px" },
        el("span", { className: "hint" }, "Newest last, scrolled to the end as they arrive."),
        el("button", { className: "btn btn-sm", onclick: refresh }, "Refresh")),
      box)
  );
  paint();
}

// affect editor
// Slow numbers (weeks) and fast feelings (per message), both editable.

// Saves on change, so one drag is one write.
function knob(label, { value, min, max, step = 1, onCommit, format = v => v, cls = "" }) {
  const out = el("span", { className: "num" }, format(value));
  const input = el("input", { type: "range", class: `slider ${cls}`.trim(), min, max, step, value });
  input.setAttribute("aria-label", label);

  const paint = () => {
    const raw = Number(input.value);
    const pct = ((raw - min) / (max - min)) * 100;
    // The fill is a gradient on the element itself.
    input.setAttribute("style", `--fill:${clamp(pct, 0, 100).toFixed(2)}%`);
    out.textContent = format(raw);
    input.setAttribute("aria-valuetext", out.textContent);
  };

  paint();
  input.oninput = paint;
  input.onchange = async () => {
    const next = Number(input.value);
    input.disabled = true;
    try {
      await onCommit(next);
      out.textContent = format(next);
    } catch (e) {
      toast(e.message, true);
      input.value = value;   // put it back rather than lie about the number
      out.textContent = format(value);
      paint();
    } finally {
      input.disabled = false;
    }
  };
  return el("div", { className: "knob" }, el("label", {}, label), input, out);
}

function balanceBar(value) {
  const pct = Math.min(50, Math.abs(value) / 2);
  const cls = value >= 15 ? "" : value <= -15 ? " low" : " mid";
  return el("div", { className: `balance${cls}`, title: `Overall ${value}`, "aria-label": `Overall balance ${value}` },
    el("i", { style: value >= 0 ? `left:50%;width:${pct}%` : `left:${50 - pct}%;width:${pct}%` }));
}

function whyRows(history) {
  if (!history || !history.length) {
    return [el("div", { className: "why-row" },
      el("span", { className: "deltas" }, "”"),
      el("span", { className: "what" }, "Nothing recorded yet."))];
  }
  return history.slice().reverse().map(h => {
    const bits = ["warmth", "familiarity", "romance"]
      .filter(k => Math.abs(h[k] || 0) >= 0.05)
      .map(k => `${k.slice(0, 4)} ${h[k] > 0 ? "+" : ""}${h[k]}`);
    const when = h.ts ? String(h.ts).slice(5, 16).replace("T", " ") : "";
    return el("div", { className: "why-row" },
      el("span", { className: `deltas ${bits.some(b => b.includes("-")) ? "down" : "up"}` },
        bits.join("  ") || "no change"),
      el("span", { className: "what" }, when ? `${when} ” ${h.why || "no clear signal"}` : (h.why || "no clear signal")));
  });
}

function feelingsGrid(person, a) {
  if (!a.feelingsOn) {
    return el("p", { className: "hint mb-0" }, "Feelings are switched off in config.json.");
  }
  return el("div", { className: "feelings-grid" },
    a.feelingNames.map(name => knob(name, {
      value: person.feelings?.[name] ?? 0,
      min: 0, max: 100, step: 1,
      format: v => Math.round(v),
      // Warm / cold / neutral, so eight sliders are not eight colours.
      cls: (person.feelings?.[name] ?? 0) >= 55 ? "hot" : (person.feelings?.[name] ?? 0) <= 15 ? "cold" : "",
      onCommit: async v => {
        await post("/affinity/feelings", { slug: person.slug, feelings: { [name]: v } });
        person.feelings = { ...person.feelings, [name]: v };
      },
    })));
}

function personCard(person, a) {
  const isCrush = a.crush?.slug === person.slug;

  const pronouns = el("input", {
    className: "pronouns-in",
    value: person.pronouns || "",
    placeholder: "unknown",
    "aria-label": `pronouns for ${person.name}`,
    onchange: async e => {
      const box = e.target;
      box.disabled = true;
      try {
        await post("/affinity/pronouns", { slug: person.slug, pronouns: box.value });
        person.pronouns = box.value;
        saved("Pronouns saved");
      } catch (err) {
        toast(err.message, true);
      } finally {
        box.disabled = false;
      }
    },
  });

  const optBtn = el("button", { className: "btn btn-sm btn-ghost" }, person.crushEnabled ? "opted in" : "opted out");
  optBtn.title = person.crushEnabled
    ? "She can fall for them. Click to rule them out."
    : "Ruled out of the crush. Click to let her fall for them.";
  optBtn.onclick = async () => {
    optBtn.disabled = true;
    try {
      await post("/affinity/toggle", { slug: person.slug });
      await reload();
      render();
    } catch (e) { toast(e.message, true); optBtn.disabled = false; }
  };

  const crushBtn = el("button", {
    className: `btn btn-sm ${isCrush ? "" : "btn-ghost"}`,
    // A crush needs a pronoun she can fall for.
    disabled: !isCrush && !person.eligible,
  }, isCrush ? "Release the crush" : "Make her crush");
  crushBtn.title = isCrush
    ? "Take the crush away from everyone."
    : !person.eligible
      ? `She only falls for ${a.eligible.join(" or ")}. Set their pronouns first.`
      : person.crushEnabled
        ? `She falls for ${person.name}.`
        : `This also opts ${person.name} back in.`;
  crushBtn.onclick = async () => {
    crushBtn.disabled = true;
    try {
      await post("/affinity/crush", { slug: isCrush ? "" : person.slug });
      await reload();
      render();
      saved(isCrush ? `${person.name} is not the crush any more` : `${person.name} has the crush`);
    } catch (e) { toast(e.message, true); crushBtn.disabled = false; }
  };

  const resetBtn = el("button", { className: "btn btn-sm btn-danger" }, "Reset");
  resetBtn.title = "Zero this person's warmth, familiarity and romance, and clear the history.";
  resetBtn.onclick = async () => {
    if (!confirm(`Reset ${person.name}'s affect back to zero?`)) return;
    resetBtn.disabled = true;
    try {
      await post("/affinity/reset", { slug: person.slug });
      await reload();
      render();
      saved(`${person.name} reset`);
    } catch (e) { toast(e.message, true); resetBtn.disabled = false; }
  };

  const streak = person.streak > 0
    ? chip(`${person.streak} good in a row`, "ok")
    : person.streak < 0
      ? chip(`${-person.streak} poor in a row`, "bad")
      : chip("neutral streak");

  const warmthTone = person.warmth > 0 ? "pos" : person.warmth < 0 ? "neg" : "";
  const romanceTone = person.romance > 0 ? "hot" : "";

  return el("div", { className: `affect-card${isCrush ? " crush" : ""}`, "data-toc": "" },
    el("div", { className: "person-top" },
      el("div", { className: "disc" }, (person.name || "?").trim().charAt(0).toUpperCase()),
      el("div", { className: "who" },
        el("b", {}, person.name),
        el("div", { className: "hint" },
          `${person.interactions} turn${person.interactions === 1 ? "" : "s"}`,
          !a.crushOn ? "" : isCrush ? " · the crush" : person.crushEnabled ? "" : " · ruled out of the crush")),
      el("div", { className: "person-actions" },
        pronouns,
        a.crushOn ? optBtn : null,
        a.crushOn ? crushBtn : null,
        resetBtn)),

    el("div", { className: "mood-strip" },
      el("span", {}, `Right now: ${person.moodLabel}`),
      balanceBar(person.balance),
      el("span", { className: "mono hint" }, `balance ${person.balance}`),
      streak),

    el("div", { className: "knobs" },
      knob("warmth", {
        value: person.warmth, min: -100, max: 100, step: 1, cls: warmthTone,
        onCommit: async v => {
          const res = await post("/affinity/adjust", { slug: person.slug, warmth: v });
          Object.assign(STATE, { people: res.people, affinity: { ...a, crush: res.crush } });
          person.warmth = v;
        },
      }),
      knob("knows", {
        value: person.familiarity, min: 0, max: 100, step: 1,
        onCommit: async v => {
          const res = await post("/affinity/adjust", { slug: person.slug, familiarity: v });
          Object.assign(STATE, { people: res.people, affinity: { ...a, crush: res.crush } });
          person.familiarity = v;
        },
      }),
      knob("romance", {
        value: person.romance, min: 0, max: 100, step: 1, cls: romanceTone,
        onCommit: async v => {
          const res = await post("/affinity/adjust", { slug: person.slug, romance: v });
          Object.assign(STATE, { people: res.people, affinity: { ...a, crush: res.crush } });
          person.romance = v;
        },
      })),

    el("details", { className: "more" },
      el("summary", {}, "How she feels right now"),
      el("p", { className: "hint mb-0" },
        `These move a few points per message and fade on their own ” roughly halving every `,
        `${a.feelingHalfLife} minutes. Only two or three of them ever reach her.`),
      feelingsGrid(person, a)),

    el("details", { className: "more" },
      el("summary", {}, "Why the numbers moved"),
      el("div", { className: "why-list" }, ...whyRows(person.history)))
  );
}

function bondPanel(a) {
  const b = a.bond;

  const title = el("h2", {}, "The bond with you");

  if (!a.bondOn) {
    return el("section", { className: "bond-panel", "data-toc": "" },
      title,
      el("p", { className: "hint mb-0" },
        "The bond is switched off, or there is no owner id set. It needs ",
        el("code", {}, "private.ownerIds"), " or ", el("code", {}, "bond.ownerIds"),
        " in config.json, and then a conversation with you in it."));
  }

  if (!b) {
    return el("section", { className: "bond-panel", "data-toc": "" },
      title,
      el("p", { className: "hint mb-0" },
        "She has not talked to you in a conversation she could score yet. Once she has, ",
        "you can nudge it by hand here."));
  }

  const status = b.caring
    ? el("div", { className: "notice", style: "margin:0 0 4px" }, el("span", { className: "dot" }),
        el("div", {}, el("b", {}, "Being careful with him right now. "),
          b.reason ? `${b.reason}. ` : "",
          b.minutesLeft > 0
            ? `About ${b.minutesLeft} minute${b.minutesLeft === 1 ? "" : "s"} left on this window.`
            : "The window has run out."))
    : el("p", { className: "hint", style: "margin:0" },
        `Sounds ${b.moodWord} (${b.mood}). She asks once when it drops past ${b.lowThreshold}, then leaves it alone.`);

  const caring = el("button", { className: "btn btn-sm btn-ghost" }, b.caring ? "End the caring window" : "Start caring now");
  caring.title = "Manual override. The window closes on its own after "
    + `${a.bondThresholds.minutes} minutes either way.`;
  caring.onclick = async () => {
    caring.disabled = true;
    try {
      await post("/affinity/bond", { slug: b.slug, caring: !b.caring });
      await reload();
      render();
    } catch (e) { toast(e.message, true); caring.disabled = false; }
  };

  const clear = el("button", { className: "btn btn-sm btn-ghost" }, "Allow her to ask again");
  clear.title = "Clears the cooldown, so she can check in on you straight away if you sound bad.";
  clear.onclick = async () => {
    clear.disabled = true;
    try {
      await post("/affinity/bond", { slug: b.slug, checkInClear: true });
      await reload();
      render();
      saved("Cooldown cleared");
    } catch (e) { toast(e.message, true); clear.disabled = false; }
  };

  return el("section", { className: `bond-panel${b.caring ? " caring" : ""}`, "data-toc": "" },
    el("div", { className: "person-top" },
      title,
      el("div", { className: "person-actions" },
        chip(b.slug, "code"),
        chip(`${b.checkIns} check-in${b.checkIns === 1 ? "" : "s"}`),
        caring, clear)),
    status,
    el("div", { className: "knobs" },
      knob("closeness", {
        value: b.close, min: 0, max: 100, step: 1, cls: "pos",
        onCommit: async v => {
          await post("/affinity/bond", { slug: b.slug, level: v });
          b.close = v;
        },
      }),
      knob("his mood", {
        value: b.mood, min: -100, max: 100, step: 1, cls: b.mood < -20 ? "neg" : b.mood > 20 ? "hot" : "",
        onCommit: async v => {
          await post("/affinity/bond", { slug: b.slug, mood: v });
          b.mood = v;
        },
      })),
    el("p", { className: "hint", style: "margin:12px 0 0" },
      "Closeness grows a little every time you talk to her, faster when you actually check in. ",
      "Mood is her own smoothed read on how you sound ” it moves on its own, this is only for nudging it.")
  );
}

// Master switch; the scores keep moving either way.
function crushSwitch(a) {
  const on = !!a.crushOn;
  const sw = el("button", { className: `toggle${on ? " on" : ""}` }, el("i", {}));
  sw.type = "button";
  sw.setAttribute("style", "margin-left:auto");
  sw.setAttribute("role", "switch");
  sw.setAttribute("aria-checked", String(on));
  sw.setAttribute("aria-label", "the crush feature");
  sw.title = on ? "Turn the crush off" : "Turn the crush on";
  sw.onclick = async () => {
    sw.disabled = true;
    try {
      await post("/affinity/crush-enabled", { enabled: !on });
      await reload();
      render();
      saved(!on ? "Crush is on" : "Crush is off");
    } catch (e) { toast(e.message, true); sw.disabled = false; }
  };

  return panel(null, null,
    el("div", { className: "person-top" },
      el("div", {},
        el("h2", {}, "Crush"),
        el("p", { className: "hint mb-0" },
          on ? "She can fall for one person, chosen below."
             : "Off. Nobody is the crush, and nothing here shows it.")),
      sw));
}

function affinity(v) {
  const a = STATE.affinity;
  const people = a.people || [];
  const crush = a.crush;

  add(v,
    head("Affect", "How she feels about everyone, and how she feels right now. ",
      a.crushOn
        ? "One crush at a time, always. She never says any of this out loud, and she does not know it as numbers."
        : "The crush is switched off. The scores and feelings still move; nobody is picked."),

    !a.enabled
      ? notice(el("b", {}, "Affect is disabled in config.json"), " ” nothing here is being tracked.")
      : null,

    crushSwitch(a),

    el("div", { className: "tiles" },
      ...(a.crushOn ? [stat("Crush", crush ? crush.name : "nobody",
        crush ? crush.pronouns || "pronouns unknown" : "not elected yet")] : []),
      stat("People", String(people.length),
        a.crushOn ? `${people.filter(p => p.isCrush).length} elected` : "affect records"),
      stat("Closest", people.length ? people[0].name : "”",
        people.length ? `warmth ${people[0].warmth}` : "no records"),
      stat("Feelings", a.feelingsOn ? "on" : "off",
        a.feelingsOn ? `fade over ~${a.feelingHalfLife} min` : "set affinity.feelings")),

    people.length === 0
      ? empty("Nobody yet", "Records appear here as people talk to her. Until then there is nothing to edit.")
      : panel("How she feels about everyone", `${people.length} record${people.length === 1 ? "" : "s"}`,
          el("p", { className: "hint" },
            "Warmth moves when you treat her well and drops when you do not. ",
            "A run of good messages builds faster than one bad message costs, and the gap narrows as she ",
            "gets close ” she does not swing wildly at someone she likes."),
          el("div", { style: "margin-top:16px" }, ...people.map(p => personCard(p, a))),
          a.crushOn
            ? el("p", { className: "hint", style: "margin:14px 0 0" },
                "She can only fall for ", el("code", {}, a.eligible.join(" / ")),
                ", so anyone without pronouns recorded can never be elected. ",
                "Set one above, or with ", el("code", {}, "!pronouns he/him"), ".")
            : null),

    bondPanel(a)
  );
}

// init
api("/").then(s => {
  STATE = s;
  VIEW = currentViewFromHash();
  enter();
}).catch(e => {
  // No login to unlock, so a failure here means nothing is being served.
  $("#boot").replaceChildren(
    el("b", {}, "Could not reach the panel"),
    el("p", { className: "hint mb-0" }, e.message),
  );
});
