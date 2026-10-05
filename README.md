# Yuyu

A Discord bot that talks like a person, not a help desk. It reads the room, replies in
whatever register the conversation is already in, remembers what people tell it, and can
switch between configurable OpenAI-compatible model APIs when a provider fails or hits quota.

- **Free-model failover.** Every free model on Gemini and OpenRouter is configured as its own
  entry, so when one runs out of quota the bot rolls to the next one that still has tokens
  instead of going quiet.
- **Add providers in config.** Add an OpenAI-compatible endpoint, model, and API-key environment
  variable to `config.json` and `.env`; no client-code changes are needed.
- **Remembers people.** Facts someone shares are written to `memory/<username>.md` and
  re-read on every future conversation.
- **Keeps useful context.** It includes the latest 12 messages and, on the first reply after
  the 30-minute interval, compacts older channel conversation into a local summary. It keeps
  important details and drops chatter. `!reset` deletes that channel's recent context and summary.
- **Skills.** Drop markdown files in `skills/` to add personality and knowledge. Keyword
  matched, so the prompt stays small.
- **Prefix commands** for anything that should not feel like a conversation.

---

## Running

Set at least one model-provider API key in `.env` (keys are available from each provider's
website), then run `python main.py`:

```
[ready] Yuyu#5312 online as "Yuyu"
[ready] prefix: ! | model: gemini
[ready] memory: on | auto-extract: on
```

Add it to a server with `!invite` from any channel, or open:

```
https://discord.com/oauth2/authorize?client_id=YOUR_APPLICATION_ID&scope=bot%20applications.commands&permissions=202752
```

Replace `YOUR_APPLICATION_ID` with your own, or just type `!invite` in a channel and she
prints the link with your id already in it.

`permissions=202752` is ViewChannel + SendMessages + ReadMessageHistory + AttachFiles —
the minimum the bot needs to read context and reply. It deliberately does not request
`Administrator`, `Manage Messages` or `Manage Roles`. You need **Manage Server** on the
target server.

Then just type at it — it answers to its own name, not only to `!` commands:

```
yuyu what's up
```

If you ever reset the token or the bot ever goes silent after a portal change, re-check
**Privileged Gateway Intents → MESSAGE CONTENT INTENT** — without it Discord sends no
message text and the bot connects but never replies.

### Provider setup

Copy `.env.example` to `.env` and add the API keys for the providers you want enabled.

#### The free-model pool

`config.json` ships one entry per free model rather than one entry per service. Each entry
needs a **unique `id`**, because the cooldown that remembers "this model is out of quota" is
keyed by id - two models sharing an id would cool each other down, and the point of the pool
is to roll past whichever one is spent and keep using the rest. `model.default` names the
preferred entry; everything after it is an ordered fallback.

On failure the bot walks the chain: quota exhaustion (`402`) puts that one model on a
15-minute cooldown and moves on, a rejected credential (`401`/`403`) backs that entry off for
an hour, and rate limits, network errors and server errors simply move to the next entry.
Free-tier limits still apply, and they are per model, so a pool is what keeps the bot
answering once any single model runs out.

Note that a `429` is deliberately **not** cooled down - those usually clear within the minute,
and the bot would rather try again than sit idle. That does mean a model which is rate-limited
for a whole day is retried on every message.

#### Adding another model

To add another OpenAI-compatible service, or another model on a service you already have,
add an entry under `model.providers` with an `id` not already used:

```json
{
  "id": "my-provider",
  "name": "My Provider",
  "baseUrl": "https://api.example.com/v1",
  "model": "model-name",
  "apiKeyEnv": "MY_PROVIDER_API_KEY",
  "enabled": true
}
```

Then add `MY_PROVIDER_API_KEY=` to `.env`. Put the preferred provider first in the array or
select it in the dashboard; all other enabled providers remain fallbacks. For Gemini and
OpenRouter, or SambaNova, add more keys as `GEMINI_API_KEY_2`, `GEMINI_API_KEY_3`,
`OPENROUTER_API_KEY_2`, `SAMBANOVA_API_KEY_2`, and so on. Each provider rotates through its
keys after a successful request and tries another key after an API failure or rate limit.
If a provider reports exhausted quota (`HTTP 402`), the bot skips its other keys, switches to
the next provider immediately, and temporarily avoids the exhausted provider for 15 minutes.
Never put API-key values in `config.json`.

To change a provider's model without editing JSON, open the dashboard's **Model** tab,
choose the provider, enter its exact model ID, and save. The change is written to
`config.json` and applies to new requests immediately; changing the active provider remains
available from the dashboard's header selector.

---

## Commands

| Command | What it does |
|---|---|
| *(no prefix)* | Just talk. Mention `Yuyu`, reply to it, or DM it. |
| `!help` | List commands |
| `!about` | **Review and edit what I know about you** — buttons and forms |
| `!remember [section] <fact>` | Save a fact. Sections: details, notes, likes, dislikes, projects, people |
| `!remember @user <fact>` | Save a fact about someone else |
| `!forget <text>` | Remove matching stored facts |
| `!forget all` | Delete your whole memory file |
| `!search <text>` | Search your own stored facts |
| `!people` | Everyone I have a memory file for |
| `!skills` | List the skill files in skills/ |
| `!emoji` | Emoji/sticker status and how to add your own |
| `!reset` | Clear short-term context for this channel |
| `!buffer` | Debug: show buffered context |
| `!pronouns <he/him>` | Set your pronouns |
| `!bond` | How she feels about you, in plain words |
| `!crush` | Owner: who she has a thing for. Anyone: `!crush off` to opt out |
| `!affinity` | (owner only) everyone's levels at once |
| `!invite` | Print the install link |
| `!ping` | Latency check |

### Adding information — three ways

1. **Buttons.** `!about` posts a card of everything stored about you with a `+` button per
   section. Click it, type, done. Also has Remove, Refresh, and Wipe everything.
2. **Commands.** `!remember likes cold brew` — the section name is optional and is detected.
3. **Just say it.** `remember that I hate mornings` in normal chat is caught and filed with no
   command and no model call, so it cannot get paraphrased. `forget that I hate mornings`
   removes it. Or say nothing at all and the auto-extractor picks it up.

### Owner only — your private file

| Command | What it does |
|---|---|
| `!secret.me` | Post `private/owner.md` |
| `!secret.add <fact> [section]` | Append a bullet to it |
| `!secret.forget <text>` | Remove a bullet |

Restricted to whoever's Discord user ID you put in `private.ownerIds` in `config.json` —
empty by default, so **nobody** can use these until you fill it in (your ID: Discord →
Settings → Advanced → copy Developer ID, with Developer Mode on). Anyone else gets a flat
refusal. The file is read off disk and posted verbatim — it is **never** added to a system
prompt and never sent to the model, so it cannot leak into a normal conversation. Allowed
sections: `Identity`, `What he works on`, `Details`, `Preferences`, `Notes`.

Whitelist lives in `config.json` under `private.ownerIds`.

---

## How it decides to answer

In a DM, always. In a server, when:

- someone replies to one of its messages
- someone `@`-mentions it
- someone starts or contains its name (`yuyu what do you think`)
- *(opt-in)* a message ends in `?` — enable with `behavior.respondInGuildsWithoutMention`

Every message is recorded to the channel buffer regardless, so context accumulates whether
or not the bot replies.

---

## Who she is

`persona.md` defines her as **Yuyu, she/her, one of the friends in the group chat** — not an
assistant. It carries the full behavioural instruction set:

- **The friend rule.** Has her own opinions and bad habits, teases people she likes,
  brings up old things unprompted, and sometimes just doesn't reply.
- **How to remember.** Bring a remembered detail up *before* anyone asks. Never recite it —
  "still on nights?" over "I recall you work nights". One remembered thing per conversation,
  maximum. Chase loose ends: if a thing was never resolved, ask about it later. Never
  mention notes, memory, files, or records.
- **What she never says.** "As an AI", "Certainly!", "Here's a list", or anything that
  narrates what she's about to do.
- **Her situation.** That's yours to write - it's the section that stops her sounding identical
  every day, and the starter file tells you to fill it in. The active primary model name is
  added to each prompt automatically, so she can always say what she's actually running. A
  starter `skills/modelwork.md` ships so skill loading works out of the box; edit it to match
  her, or delete it and she simply won't have that skill.
- **Serious mode.** If someone is genuinely upset she drops the jokes entirely, as a switch
  flipping rather than a paragraph about feelings.

Output:

> **yuyu what have you been working on lately**
> honestly a mess. eval harness keeps passing locally and failing in CI, and i'm fairly sure
> it's the dataset step but the person who wrote it has left so now that's my problem.
> also yeah, i'm still up late fixing bugs… someone has to 🙃

---

## Rich output, without losing the voice

She still talks like a friend. But when an answer is genuinely structured, she can wrap it in a
`<card>` and it renders as a real embed instead of a wall of text:

```
postgres for "i want it correct and not bite me later". mysql if your stack is already
MySQL-shaped. sqlite for local dev and embedded — one file, zero setup.

[embed] postgres vs mysql vs sqlite (quick verdict)
        Postgres  — complex queries, correctness, features
        MySQL     — classic web default, mature replication
        SQLite    — single file, no server, dies on heavy write concurrency
```

```
<card title="Short title" color="blue|green|yellow|red|purple|cyan" footer="optional">
Body. Supports **bold**, `inline code`, and ```code blocks```.
Fields go on their own lines, up to 25:
field=Label::value
</card>
```

The rule she works to: **the card is the data, she is the person.** Verdict in plain text,
structure in the card. One card per reply. Casual replies never get one — the `rich-output`
test asserts exactly that, so it can't drift into posting a widget at someone who just said
"yuyu bored".

Every command output is an embed too. Discord's limits (256/4096/1024/25 fields) are enforced
in `yuyu/formatting.py`, and truncated rather than allowed to 400.

## Custom emoji and reactions

Discord **blocks bots from sending real stickers**, so this uses what the API allows:

- **Reactions** on your message — keyword-matched, e.g. `😂` for lol/lmao, `🧠` for anything
  model or eval related, `☕` for tired.
- **A sticker** slipped onto her reply at a low rate, matched to what you were talking about.

Two sources:

1. **`stickers.json`** — yours to edit, works in any server. Custom emoji use `<:name:id>`,
   or `<a:name:id>` if animated. Type `\` in Discord to reveal an emoji's ID.
2. **Auto-discovery.** On startup (and on joining a server) she indexes every custom emoji in
   each server and will use a matching one when the topic comes up. A server full of custom
   emoji needs zero config. `!emoji` shows the current status.

Turn either off with `config.json` → `emoji.reactions` / `emoji.stickers`, or set the chances
to `0`. She is told not to add emoji to her own text, so the two never double up.

## Skills

| File | Loads |
|---|---|
| `social.md` | Always. Baseline friend instincts. |
| `modelwork.md` | Models, training, evals, latency (starter — edit or delete it) |
| `technical.md` | Code, errors, specs, comparisons, benchmarks |
| `support.md` | Someone is upset, stressed or going through it |
| `nightowl.md` | Games, music, late nights, coffee |

`support.md` is the one worth knowing about: it makes her drop the jokes *immediately* when
someone is actually struggling, forbids performed empathy ("that sounds really hard", "your
feelings are valid"), and tells her not to offer advice unless asked twice.

Add your own as `skills/<name>.md` with optional frontmatter (`name`, `description`,
`keywords`, `always`). Reloaded on next use, no restart.

## Files worth editing

```
persona.md          Who she is. Read on every message - the highest-leverage file.
skills/*.md         Optional personality/knowledge, keyword matched.
stickers.json       Custom emoji and reactions.
config.json         Model, memory, context and trigger settings.
memory/<slug>.md    Per-person memory. Plain markdown - edit or delete by hand.
affinity/<slug>.json How she feels about them. Plain JSON.
private/owner.md   Owner-only. Never enters a prompt.
```

### `persona.md`

Injected verbatim every turn. This is what stops it sounding like a bot. There is a starter
file with the anti-assistant rules already filled in — the "What you're into" and "Current
situation" sections are yours to write, and specificity is what makes it feel alive.

### `skills/*.md`

```markdown
---
name: Night Owl
description: Late-night chat energy
keywords: [games, music, late, night, coffee]
always: false
---

You're a night person. Late messages are your favourite ones...
```

`always: true` is injected every turn. Otherwise the file is injected only when one of its
`keywords` appears in recent conversation, and only the top few match at a time. Reload is
automatic — no restart needed.

---

## Memory

Six sections per person, so a fact lands somewhere meaningful:

```markdown
## Details     - stable facts (job, city, pronouns, family)
## Notes       - current situation
## Likes       - what they enjoy
## Dislikes    - what they avoid
## Projects    - what they are building
## People      - people they have mentioned
```

A live run, from "i just got promoted to shift lead, started on nights again":

```markdown
## Details
- Promoted to shift lead
- Works nights
```

Three guards keep this honest, all covered by tests:

- **Only the user's own words are read.** An earlier version also passed the bot's reply into
  the extractor, and the result was Yuyu writing her own model work into the user's file.
- **Batching with a trailing sweep.** Messages that arrive inside the debounce window are
  not dropped — a watermark collects them and a re-armed timer sweeps the backlog, so a fast
  conversation never loses a fact.
- **Normalisation and a durability filter.** First-person framing is stripped
  (`"i just got promoted"` → `"Promoted to shift lead"`, idempotent), and questions, greetings
  and bare moods (`bored`, `lol`, `what have you been working on`) are rejected before
  anything touches disk.

Nothing blocks the reply — extraction is fire-and-forget, and failures log a warning.

Per-person files are plain markdown on purpose: open one, correct it, delete it. The bot
re-reads from disk each time, so hand edits take effect immediately. Channel summaries live
in `memory/conversations.json`, are kept locally across restarts, and expire after
`context.ttlMinutes` of silence. They are included with a smaller recent-message window to
keep prompts compact; resetting the channel removes both.

`memory/*.md` and `memory/conversations.json` are gitignored — they hold real people's details
and private conversation summaries.

## Affect — liking, dislikeness, and one secret crush

Three dials per person in `affinity/<slug>.json`, all inferred from ordinary conversation:

| Dial | Range | Moves with |
|---|---|---|
| `familiarity` | 0–100 | How often you talk, how long, how much you tell her |
| `warmth` | −100–100 | How the interactions actually felt |
| `romance` | 0–100 | How much she is leaning toward them (only one can hold the crush) |

Signals she picks up: replying to her (worth more than a bare mention), asking about her,
shared interests, opening up about yourself, saying thanks, late-night conversations, and on
the other side — being harsh, or talking to her like a tool (`write me a list of…` reads very
differently from `what do you think about…`).

Warmth has diminishing returns, so a person she already likes can't be farmed to +100 in one
chat. Everything decays with time: familiarity fades slowly, warmth bleeds back to neutral,
and a crush cools but keeps a floor, so absence is not instant amnesia.

### The crush is exclusive, and you choose it

**She only falls for he/him or xe/xim, and there is only ever one.** By default the crush is
whoever you pick in the dashboard's Affect tab: **Make her crush** hands it to someone,
**Release the crush** takes it away, and resetting or ruling that person out clears it. Nothing
in a conversation hands one out on its own, and once you have picked someone, talking to
somebody else more does not move the spot.

Set `affinity.crushAutoElect` to `true` for the older behaviour: the election then runs on every
turn, the eligible person with the highest `romance` wins, and a switch margin keeps two
near-equals from flapping. `romance` still builds for everyone either way — it is only the one
crush spot that is exclusive.

### Switching the whole thing off

`affinity.crush` (default `true`) is the master switch, and the Affect tab has a toggle for the
same setting. Turn it off and nobody is elected, she never carries the behaviour, and every
crush control disappears from the dashboard. Switching off clears the `isCrush` mark but **not**
the scores: `warmth`, `romance`, and the feelings keep moving exactly as before, so turning it
back on does not lose anyone's history. The master switch is independent of
`affinity.crushAutoElect` (manual versus automatic) and of `affinity.crushBehaviours` (whether
the behaviour prompt is injected at all).

Anyone using she/her, they/them, ze/zir, or no recorded pronouns is **never** eligible, no
matter how much they talk to her. They get warmth — she still likes them — just not this.

Pronouns come from Discord's own field, from `!pronouns`, or from a stored fact, and can be
set in the dashboard. Without pronouns nobody can be picked, so you'll want to set at least
one. `affinity.eligiblePronouns` narrows or widens the rule; an empty list falls back to
he/him + xe/xim so a typo can't quietly change who she likes.

### She never says it

The crush is injected as behaviour, never as a fact. She is told to let it show as: seeking
him out a little more than she needs to, noticing small things, being slightly softer, finding
reasons to keep a conversation going. And explicitly — never name it, deflect if asked
outright, and *it must stay deniable*. No scores or the word "romance" ever reach the prompt;
a test asserts that.

Pronouns are respected in the text itself: someone using xe/xim gets "them", not "him".

### How she feels right now

Warmth and the crush are slow — they take weeks. These are the fast ones, and they live in
the same file under `"feelings"`:

| Feeling | Moves with |
|---|---|
| `happy`, `calm` | being treated well, being thanked, ordinary good conversation |
| `playful`, `excited` | teasing, banter, sharing something good |
| `worried` | someone she cares about sounding bad |
| `annoyed`, `sad` | being talked to like a tool, being ignored |
| `tired` | late nights |

Everything moves a few points per message, never a jump, and **halves every
`affinity.feelingHalfLifeMinutes`** (default 120) so a good conversation three hours ago stops
colouring how she talks now. Only the top two, above a floor, ever reach the prompt — and she
gets words, not the vector: *"right now, talking to them, you feel tired (54), and in a playful
mood (45)"*. Below the floor she is told she feels level, which is true most of the time.

Set `affinity.feelings` to `false` and she stops having them.

### The bond with you

Separate from the crush and separate from everyone else. It only applies to your Discord id
(`bond.ownerIds`, falling back to `private.ownerIds`), and it is not a special case in the
arithmetic — you have a record like anyone else. What differs is what she does.

If a message reads as genuinely bad — not one rough afternoon, but *no reason to bother*, crying,
not sleeping — she asks **once**, plainly, whether something is going on in real life. Then she
stays gentler for up to `bond.caringMinutes` (default 120) without making a thing of it.

The window closes by itself, either because you sound like yourself again for `bond.graceTurns`
turns, or because the time is up. Both paths are tested. She never asks twice inside
`bond.checkInCooldownMinutes`, and if you tell her you're fine, that's the end of it — asking
again would be the creepy version, not the caring one.

The dashboard's **Affect** tab has her closeness to you and her read on your mood as sliders, so
you can open or close the window by hand if you want to see it.

| Command | What it does |
|---|---|
| `!pronouns <he/him>` | Set yours (also read from your Discord profile) |
| `!bond` | How she feels about you, in plain words |
| `!crush` | (owner) who she has a thing for |
| `!crush off` | Opt yourself out — clears it and she won't bring it up |
| `!affinity` | (owner) everyone at once, with scores and pronouns |

Anyone can `!crush off` for themselves, whether or not they're currently the crush.

---

## Management app

Edit her personality from a browser instead of by hand. Persona, skills, stickers, config,
memory files, a live view of how she feels about everyone, and the last 300 log lines.

```bash
python main.py                   # dashboard is already enabled
# opens http://127.0.0.1:7373 automatically
```

**No password at all** — the dashboard binds to `127.0.0.1` and every control is live, so nothing
off this machine can reach it. Any program or person on this machine can use every dashboard
control. What that leaves exposed, and what's covering it:

| Risk | Covering it |
|---|---|
| Someone on your network reaching it | Loopback-only bind. Setting `host` to `0.0.0.0` **refuses to start** — there is no password to fall back on. |
| A random website rewriting her personality while you have the tab open | Writes are same-origin checked. A cross-origin `PUT` gets `403`. |
| Path traversal into your `.env` or `private/` | Every path resolved against an allowlist of `skills/*.md` and `memory/*.md`. Tested with encoded, mixed and Windows-style payloads. |
| A typo bricking the bot | Invalid JSON is validated *before* the write. |

There is nothing to re-enable. If you need it reachable from another machine, put a reverse
proxy with its own authentication in front of it rather than binding this directly.

Tabs: **Overview**, **Model**, **Presence**, **Servers**, **Affect**, **People** (full memory
contents, not just counts), **Skills**, **Persona**, **Stickers**, **Reset**, **Logs**, **Config**.

Edits to persona, skills and stickers apply on the next message. `config.json` needs a restart.

### Discord presence

Use the dashboard's **Presence** tab to customize status, activity type/name, both activity
text lines, and large/small image asset keys. Presence text changes apply immediately while
the bot is connected. For images, first upload art assets under your bot application's
**Rich Presence Art Assets** in the Discord Developer Portal, then enter the exact asset key;
an external image URL is not a Discord asset key. The preview shows the configured asset key
and activity text; Discord renders the actual registered art.

### Servers

Every server she is in, with member and channel counts, a name filter, and a **Leave** button
per row. Leaving is admin-only and confirmed in the browser, because an invite link is the only
way back in - she keeps running for every other server. The list is a snapshot fed by
`on_ready`/`on_guild_create`/`on_guild_remove`, so it also updates when she is kicked rather
than leaving a stale row behind.

### Reset

The **Reset** tab wipes data across every server. Note that memory is stored per *person*, not
per server, so one reset clears what she knows about someone everywhere she knows them.

What you can remove, in two tiers:

| Tier | Targets |
|---|---|
| **learned** — data she picked up | Memory files (`memory/*.md`), affect (`affinity/*.json`, including who the crush is), recent conversation buffers and saved summaries |
| **authored** — content you or I wrote | Skill files, persona, stickers, config |

The flow is deliberately not one click:

1. Tick what to remove. **Select all learned data** picks just the learned tier.
2. **Preview** lists exactly what goes — file names, fact counts, how many turns each person
   had, and who the current crush is — plus what is being kept.
3. If anything in the **authored** tier is ticked, you must type `RESET` before the button
   unlocks. Case-insensitive; the point is deliberateness, not exact casing.
4. **Reset now** writes a backup to `backups/<timestamp>/` *before* deleting anything.

Every backup is listed on the same tab with a **Restore** button, so a reset is reversible.
Restore paths come from the backup's own manifest and are rejected if they point outside the
project.

API: `POST /api/reset/preview`, `POST /api/reset/run`, `GET /api/reset/backups`,
`POST /api/reset/restore`.

---

`config.json`:

```jsonc
{
  "bot":     { "name": "Yuyu", "prefix": "!", "personaFile": "persona.md" },
  "model":   { "default": "gemini", "providers": [ ... ], "maxTokens": 800, "temperature": 0.95 },
  "memory":  { "enabled": true, "autoExtract": true, "extractCooldownMs": 45000 },
  "context": { "maxMessages": 30, "promptMessages": 12, "summaryIntervalMinutes": 30,
               "summaryKeepRecentMessages": 8, "summaryMaxChars": 1200, "ttlMinutes": 240 },
  "behavior":{ "respondToNameAnywhere": true, "humanDelay": true }
}
```

`model.default` selects the preferred provider, which Yuyu identifies by provider name and
model ID in her prompt (for example, `Gemini (gemini-3.1-flash-lite)`). Change it to another
provider ID in `model.providers` to choose a different primary model. Providers are tried in
configured order, so another enabled provider can take over when the primary fails or reaches
a limit, or rejects a request as incompatible. When asked, Yuyu names the preferred model and
knows which configured models are available as fallbacks; she does not claim a fallback was
used for a specific reply unless that is known.

Recent messages are held in memory and expire after `ttlMinutes` of silence. On a reply after
`summaryIntervalMinutes`, older context is compacted into a private local summary; the summary
also expires after `ttlMinutes` of silence and is cleared by `!reset`.

---

## Tests

Offline tests run without provider keys. Live API tests are opt-in because they use provider
quota:

```bash
python -m pytest -q                         # all offline tests; live checks skip by default
python -m pytest tests/test_dashboard.py -q  # one file
```

Set `LLM_LIVE_TESTS=1` before running `tests/test_live.py` to opt in to real model requests.

- **`tests/test_affinity.py`** — 57 checks. Pronoun eligibility, the crush election and its
  exclusivity, the master crush switch, decay, and assertions that no score or the word "romance"
  reaches the prompt.
- **`tests/test_dashboard.py`** — 99 checks. Path traversal, `private/` isolation, JSON validation
  before write, CRUD round-trips, cross-origin write refusal, loopback enforcement, the affect and
  bond endpoints, the reset endpoints, and a UI/API contract check that walks every `STATE.*` path
  the page reads and every `id` it touches, so a field rename fails the build instead of rendering a
  blank page.
- **`tests/test_feelings_bond.py`** — 38 checks. That her temporary feelings move a few points a
  message and halve on the configured half-life, that warmth rises when he is kind and falls harder
  when he is not, and that the caring window opens on one bad message, closes when he sounds better,
  and closes on its own if it does not.
- **`tests/test_core.py`** — memory across all six sections, path-safe slugs, fact
  normalisation and durability, skill frontmatter, context buffering and persisted summaries,
  prompt assembly, and the discord.py 2.7 message shape.
- **`tests/test_reset.py`** — 24 checks. Inventory accuracy, preview listing both what goes and
  what is kept, the typed-confirmation requirement for authored files, backup-before-delete, a
  full round-trip restore, and traversal rejection on restore ids.
- **`tests/test_formatting.py`** — 19 checks. Card parsing, attribute handling, Discord limit
  enforcement, malformed-card recovery, and emoji keyword matching with chance gating.
- **`tests/test_providers.py`** — configured provider choices, request formatting, failover on
  quota/auth/network failures, streaming, and safe user-facing errors.
- **`tests/test_presence.py`** — 14 checks. Throttling, change detection, and that a presence
  failure never becomes a channel message.
- **`tests/test_guilds.py`** — 12 checks. Server list tracking and its use in presence.
- **`tests/test_mute.py`** — 12 checks. The per-person quiet switch and that muting suppresses
  replies, commands and reactions without suppressing memory.
- **`tests/test_live.py`** — opt-in live checks. Real completions against the configured provider
  chain: that a comparison earns a card while a casual "yuyu bored" does not, that no raw `<card>` markup ever
  reaches the channel, that she knows her own models, never uses assistant boilerplate, recalls
  without ledger language, and that a stated fact actually reaches disk.
- **`tests/test_storage.py`** — 6 checks. Atomic writes and the scratch-then-replace pattern
  that keeps a half-written file from ever replacing a good one.

`tests/probe_*.py` are standalone diagnostics, not part of the suite. Run them directly when you
need to inspect live behaviour (they hit the real API or a running dashboard):

```bash
python tests/probe_message.py
python tests/probe_crush.py
```

The dashboard's inline `<script type="module">` is syntax-checked by
`test_the_dashboard_script_parses`, which shells out to `node --check`. That is the only Node
dependency left in the project and it is optional — the test **skips** when `node` is not
installed, so Python-only machines are unaffected. The bot itself never needs Node.

---

## Layout

```
main.py            entry point: starts the Discord bot and the local dashboard
requirements.txt   discord.py, Flask, python-dotenv, requests
config.json        model, memory, affect, dashboard settings (shared)
persona.md         who she is
muted.json         per-person quiet switch
stickers.json      which image fires when

yuyu/
  chat.py          prompt assembly, streaming, fact extraction, natural-language memory
  persona.py       system prompt builder
  memory.py        memory/<slug>.md, six sections, fact normalisation
  affinity.py      familiarity / warmth / romance, crush election, decay
  private.py       owner-gated private file, deliberately outside the prompt path
  formatting.py    <card> parsing + embed builders, Discord limit enforcement
  stickers.py      custom emoji index, sticker/reaction picking
  skills.py        skills/*.md frontmatter + keyword matching
  context.py       recent channel buffer and persisted compact summaries
  providers.py     OpenAI-compatible API client with ordered provider failover
  config.py        env + config.json merge, token validation, directory setup
  guilds.py        server list tracking
  presence.py      online status, throttled and change-detected
  mute.py          per-person quiet switch
  reset.py         inventory, preview, backup, wipe, restore
  logger.py        in-memory log ring buffer, surfaced in the dashboard
  util.py          splitting, slugs, dedupe, keyword extraction
  commands/        prefix command router + memory editor (buttons/modals)
  dashboard/       local control panel (Flask app + single-page UI)
```

## Notes

- Requires Python 3.11+. Dependencies are already installed; `pip install -r requirements.txt`
  if you ever need to rebuild the environment.
- `DISCORD_PUBLIC_KEY` is stored but unused — this bot has no slash-command interaction
  signature to verify.
- `DISCORD_CLIENT_SECRET` is only for building the install link; it can never log the bot in.
- `private/`, `memory/*.md`, `memory/conversations.json`, and `affinity/*.json` are gitignored.
  They hold real
  people's details, and `affinity/` records who the crush currently is.
- `backups/` is gitignored too — it holds copies of everything above, including authored files.
- A bot token grants full control of the bot account. `.env` is gitignored, but the token was
  pasted into a chat — **Reset Token** if this conversation is shared, logged, or committed
  anywhere. That invalidates the old one and needs a matching `.env` update.
