from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime, timezone

from . import bond, feelings
from .config import AFFINITY_DIR, config
from .util import atomic_write, clamp, slugify

_lock = threading.Lock()
_cache: dict[str, dict] = {}

NOT_MASCULINE = re.compile(r"\b(she|her|hers|they|them|theirs|ze|zir)\b", re.IGNORECASE)
DEFAULT_ELIGIBLE = ["he/him", "xe/xim"]
DAY = 86_400
HISTORY_LIMIT = 40

# Every signal one turn can carry. Kept in one place so `turn_signals()` in
# chat.py and the scoring below cannot drift apart.
SIGNAL_FIELDS = (
    "addressed", "directReply", "askedAboutHer", "sharedTopics", "revealedSomething",
    "gratitude", "warm", "checkedIn", "apologised", "teased", "dismissed", "distant",
    "harsh", "commanded", "lateNight", "ignored",
)


def _eligible_sets() -> list[str]:
    raw = config["affinity"].get("eligiblePronouns") or DEFAULT_ELIGIBLE
    if isinstance(raw, str):
        raw = [raw]
    out: list[str] = []
    for entry in raw:
        for part in re.split(r"[/,]|\s+and\s+", str(entry), flags=re.IGNORECASE):
            token = part.strip().lower()
            if token and token not in out:
                out.append(token)
    return out or list(DEFAULT_ELIGIBLE)


def is_crush_eligible_pronoun(pronouns: str | None) -> bool:
    """He/him and xe/xim only. Anything else, or nothing, is not eligible."""
    text = str(pronouns or "").strip().lower()
    if not text:
        return False
    # An explicit non-masculine set always wins, so "she/they" never qualifies.
    if NOT_MASCULINE.search(text):
        return False
    tokens = [t for t in re.split(r"[^a-z]+", text) if t]
    if not tokens:
        return False
    for entry in _eligible_sets():
        wanted = [t for t in re.split(r"[^a-z]+", entry) if t]
        if any(token in wanted for token in tokens):
            return True
    return False


def normalize_pronouns(raw) -> str:
    return re.sub(r"\s+", " ", str(raw or "").strip().lower())[:40]


def crush_enabled() -> bool:
    """The master switch for the crush feature.

    Off means nobody is picked, nothing crush-shaped is shown, and she never
    acts on it - but warmth, romance and the feelings all keep moving.
    """
    return bool(config["affinity"].get("crush", True))


def pronouns_from_facts(facts) -> str:
    pattern = re.compile(
        r"\b(he/him|him/he|he/him/his|she/her|her/she|they/them|xe/xim|xe/xem|xe/xir|he|she|they|xe|ze)\b",
        re.IGNORECASE,
    )
    for fact in facts or []:
        match = pattern.search(str(fact))
        if match:
            return normalize_pronouns(match.group(1))
    return ""


def _blank(slug: str, name: str = "") -> dict:
    now = datetime.now(timezone.utc).isoformat()
    return {
        "slug": slug,
        "name": name or slug,
        "pronouns": "",
        "familiarity": 0.0,
        "warmth": 0.0,
        "romance": 0.0,
        "interactions": 0,
        "topics": [],
        "firstSeen": now,
        "lastInteraction": None,
        "crushEnabled": True,
        # Who she actually has a crush on. Remembered so a near-equal challenger
        # cannot quietly take the spot on a tie-break. Set by elect_crush().
        "isCrush": False,
        # Signed run of good or bad turns. Repetition is what makes something land.
        "streak": 0,
        "feelings": feelings.blank(),
        "feelingsAt": now,
        "bond": bond.blank(),
        "history": [],
    }


def _file_for(slug: str):
    return AFFINITY_DIR / f"{slug}.json"


def _setting(key: str, fallback: float) -> float:
    try:
        return float(config["affinity"].get(key) or fallback)
    except (TypeError, ValueError):
        return fallback


def _clamp(record: dict) -> dict:
    record["familiarity"] = clamp(float(record.get("familiarity") or 0), 0, 100)
    record["warmth"] = clamp(float(record.get("warmth") or 0), -100, 100)
    record["romance"] = clamp(float(record.get("romance") or 0), 0, 100)
    record["interactions"] = max(0, int(record.get("interactions") or 0))
    try:
        record["streak"] = int(record.get("streak") or 0)
    except (TypeError, ValueError):
        record["streak"] = 0
    record["streak"] = max(-20, min(20, record["streak"]))
    feelings.normalise(record)
    bond.normalise(record)
    return record


def decayed(record: dict, now: float | None = None) -> dict:
    """Time-based decay on read, so a dormant relationship cools on its own."""
    if not record.get("lastInteraction"):
        feelings.decay(record, now)
        return record
    now = now if now is not None else time.time()
    last = datetime.fromisoformat(record["lastInteraction"]).timestamp()
    days = max(0.0, (now - last) / DAY)
    if days >= 0.5:
        pull = min(abs(record["warmth"]), days * _setting("decayWarmthPerDay", 1.5))
        sign = 1 if record["warmth"] >= 0 else -1
        record["warmth"] = sign * max(0.0, abs(record["warmth"]) - pull)
        record["familiarity"] = max(0.0, record["familiarity"] - days * _setting("decayFamiliarityPerDay", 0.35))
        if record["romance"] > 0:
            # A crush cools slower, with a floor, so absence is not amnesia.
            floor = _setting("romanceFloor", 25)
            record["romance"] = max(floor, record["romance"] - days * _setting("decayRomancePerDay", 0.7))
    feelings.decay(record, now)
    return record


def load(slug: str) -> dict:
    with _lock:
        if slug in _cache:
            return _cache[slug]
    try:
        stored = json.loads(_file_for(slug).read_text(encoding="utf-8"))
    except FileNotFoundError:
        record = _blank(slug)
    except (json.JSONDecodeError, OSError) as exc:
        print(f"[affinity] {slug}.json is unreadable ({exc}); starting fresh")
        record = _blank(slug)
    else:
        record = {**_blank(slug), **stored, "slug": slug}
    _clamp(record)
    decayed(record)
    with _lock:
        _cache[slug] = record
    return record


def save(record: dict) -> dict:
    _clamp(record)
    record["history"] = list(record.get("history") or [])[-HISTORY_LIMIT:]
    AFFINITY_DIR.mkdir(parents=True, exist_ok=True)
    # atomic_write stages in tmp/ and swaps, because every turn writes these
    # while the dashboard is reading and a torn record is unparseable.
    atomic_write(_file_for(record["slug"]), json.dumps(record, indent=2) + "\n")
    with _lock:
        _cache[record["slug"]] = record
    return record


def invalidate() -> None:
    """Drop the cache. Used by tests and after files change underneath us."""
    with _lock:
        _cache.clear()


def load_all() -> list[dict]:
    return [load(path.stem) for path in sorted(AFFINITY_DIR.glob("*.json"))]


def get(slug: str) -> dict | None:
    record = load(slug)
    return record if record["interactions"] > 0 else None


def set_pronouns(slug: str, name: str | None, pronouns: str) -> dict:
    record = load(slug)
    if name:
        record["name"] = name
    record["pronouns"] = normalize_pronouns(pronouns)
    return save(record)


# Everything below edits files directly for the dashboard, so each one finishes
# by re-electing and saving the whole roster. The crush belongs to everyone, and
# patching a single file leaves a stale mark sitting in the others until
# something else happens to touch them.

def _roster_for(record: dict) -> list[dict]:
    everyone = load_all()
    if not any(r["slug"] == record["slug"] for r in everyone):
        everyone.append(record)
    return everyone


def _reel(records: list[dict], edited: dict) -> dict:
    elect_crush(records)
    for item in records:
        save(item)
    return edited


def adjust(slug: str, values: dict) -> dict:
    """Set someone's numbers directly: the long ones, or her feelings right now."""
    record = load(slug)
    if record["interactions"] == 0:
        raise ValueError("no record for that person yet")

    # Validate everything before anything lands. A half-applied edit would move
    # the score without the panel ever being told it had.
    updates: dict = {}
    for key, low, high in (("warmth", -100, 100), ("familiarity", 0, 100), ("romance", 0, 100)):
        if key not in values:
            continue
        try:
            updates[key] = clamp(float(values[key]), low, high)
        except (TypeError, ValueError):
            raise ValueError(f"{key} has to be a number") from None

    wanted = values.get("feelings") if feelings.enabled() else None
    if not updates and not isinstance(wanted, dict):
        raise ValueError("nothing to change")
    if updates:
        record.update(updates)
    if isinstance(wanted, dict):
        feelings.set_feelings(record, wanted)

    return _reel(_roster_for(record), record)


def designate(slug: str) -> dict:
    if not crush_enabled():
        raise ValueError("the crush is switched off")
    everyone = load_all()
    record = next((r for r in everyone if r["slug"] == slug), None)
    if record is None:
        raise ValueError("no record for that person yet")
    if not is_crush_eligible_pronoun(record.get("pronouns")):
        raise ValueError("she only falls for " + " or ".join(_eligible_sets()))
    # Picking someone outright outranks the softer opt-out toggle: if the host
    # asks for the crush on somebody they had ruled out, let her fall for them
    # again rather than refusing a click the panel just invited.
    record["crushEnabled"] = True

    margin = _setting("crushSwitchMargin", 20)
    threshold = _setting("romanceThreshold", 40)
    rivals = max((r["romance"] for r in everyone if r["slug"] != slug), default=0.0)
    record["romance"] = clamp(max(record["romance"], rivals + margin + 12, threshold + 5), 0, 100)

    # Claim the spot before the election runs. Against a rival already at 100
    # the clamp makes them level, and a tie decided by margin would hand it
    # straight back to whoever was holding it.
    for item in everyone:
        item["isCrush"] = item["slug"] == slug
    return _reel(everyone, record)


def release_crush() -> None:
    for record in load_all():
        record["isCrush"] = False
        record["romance"] = 0.0
        save(record)


def sync_crush_marks() -> None:
    """Re-elect the whole roster and write every mark back.

    Used when the master switch flips, so the files on disk agree with the panel
    instead of keeping an `isCrush` mark it will never show again. Scores are
    left untouched - this only moves the one mark.
    """
    records = load_all()
    elect_crush(records)
    for record in records:
        save(record)


# --- scoring ---------------------------------------------------------------

def signals_from_turn(**kwargs) -> dict:
    return {field: bool(kwargs.get(field)) for field in SIGNAL_FIELDS}


def _hurts(sig: dict) -> bool:
    return bool(sig.get("harsh") or sig.get("commanded") or sig.get("dismissed"))


def _score_warmth(sig: dict, record: dict) -> tuple[float, str]:
    """How this one message moved how warm she feels to them."""
    delta = 0.0
    why: list[str] = []

    if sig.get("directReply"):
        delta += 2.2; why.append("replied to her")
    if sig.get("revealedSomething"):
        delta += 2.0; why.append("opened up")
    if sig.get("warm"):
        delta += 2.0; why.append("was decent to her")
    if sig.get("checkedIn"):
        delta += 1.8; why.append("asked how she is")
    if sig.get("askedAboutHer"):
        delta += 1.2; why.append("asked about her")
    if sig.get("gratitude"):
        delta += 1.0
    if sig.get("apologised"):
        delta += 1.2; why.append("said sorry")
    if sig.get("teased"):
        delta += 1.0; why.append("teased her nicely")
    if sig.get("sharedTopics", 0) > 0:
        delta += min(2.5, sig["sharedTopics"] * 0.6); why.append("shared interests")
    if sig.get("lateNight"):
        delta += 0.5
    if sig.get("addressed"):
        delta += 0.8

    if sig.get("harsh"):
        delta -= 3.5; why.append("was harsh")
    if sig.get("commanded"):
        delta -= 2.0; why.append("talked to her like a tool")
    if sig.get("dismissed"):
        delta -= 2.0; why.append("could not be bothered")
    if sig.get("ignored"):
        delta -= 1.5; why.append("walked past her")
    if sig.get("distant"):
        delta -= 0.8; why.append("barely said anything")

    # Repetition is what makes something land. Three good turns in a row is not
    # three times one good turn, and three bad ones hurt more than the first did.
    streak = record.get("streak", 0)
    if delta > 0 and streak > 1:
        delta += min(streak, 6) * _setting("streakBonus", 0.3)
    elif delta < 0 and streak < -1:
        delta -= min(-streak, 6) * _setting("badStreakBonus", 0.45)

    if delta > 0:
        # Loss lands harder than the same amount of gain. And while she is still
        # annoyed or sad about how someone treated her, being nice again does not
        # land as fast - forgiveness takes a few turns, like it does for anyone.
        delta *= _setting("warmthRecoverScale", 0.85)
        hurt = feelings.normalise(record).get("annoyed", 0) + feelings.normalise(record).get("sad", 0)
        delta *= clamp(1 - hurt / 130.0, 0.25, 1.0)
    elif delta < 0:
        delta *= _setting("warmthLossScale", 1.25)

    # Diminishing returns, so nobody can be farmed to +100 in one conversation
    # and nobody she dislikes falls forever.
    headroom = (100 - record["warmth"]) if delta > 0 else (record["warmth"] + 100)
    scale = min(1.0, headroom / 25) if headroom > 0 else 1.0
    return delta * scale, ", ".join(why)


def _score_familiarity(sig: dict) -> tuple[float, str]:
    delta = 0.4
    if sig.get("addressed"):
        delta += 0.5
    if sig.get("directReply"):
        delta += 1
    if sig.get("revealedSomething"):
        delta += 2
    if sig.get("sharedTopics", 0) > 0:
        delta += min(2, sig["sharedTopics"] * 0.5)
    if sig.get("commanded"):
        delta += 0.2  # still learned something about them
    return delta, ""


def _score_romance(sig: dict, record: dict) -> tuple[float, str]:
    if record.get("crushEnabled") is False:
        return 0.0, ""
    if not is_crush_eligible_pronoun(record.get("pronouns")):
        return 0.0, ""

    delta = 0.0
    why: list[str] = []
    if sig.get("directReply"):
        delta += 0.6; why.append("keeps coming back to talk")
    if sig.get("revealedSomething"):
        delta += 0.9; why.append("trusts her with real things")
    if sig.get("askedAboutHer"):
        delta += 0.8; why.append("asks about her")
    if sig.get("checkedIn"):
        delta += 0.7; why.append("checks in on her")
    if sig.get("warm"):
        delta += 0.4
    if sig.get("sharedTopics", 0) > 0:
        delta += min(0.9, sig["sharedTopics"] * 0.25); why.append("same wavelength")
    if sig.get("lateNight"):
        delta += 0.5; why.append("talks to her late")
    if sig.get("gratitude"):
        delta += 0.2
    if sig.get("commanded"):
        delta -= 2.5; why.append("talked to her like a tool")
    if sig.get("harsh"):
        delta -= 4; why.append("was harsh")
    if sig.get("dismissed"):
        delta -= 2; why.append("could not be bothered")
    if sig.get("ignored"):
        delta -= 1.5

    headroom = (100 - record["romance"]) if delta > 0 else (record["romance"] + 100)
    scale = min(1.0, headroom / 40) if headroom > 0 else 1.0
    return delta * scale, ", ".join(why)


def elect_crush(records: list[dict]) -> dict | None:
    eligible = [
        r for r in records
        if r.get("crushEnabled") is not False and is_crush_eligible_pronoun(r.get("pronouns"))
    ]
    winner = None
    if crush_enabled() and eligible:
        if config["affinity"].get("crushAutoElect"):
            top = sorted(eligible, key=lambda r: (-r["romance"], -r["warmth"], r["slug"]))[0]
            # A crush is a lean, not a pronoun: with nobody carrying any romance there
            # is nobody to elect. This is what lets "Release the crush" and a reset
            # actually leave the panel empty instead of re-electing the same person
            # from warmth or the alphabetical tie-break.
            if top["romance"] > 0:
                holder = next((r for r in eligible if r.get("isCrush")), None)
                # The margin protects a holder who still leans. A holder whose romance
                # was reset to zero keeps no claim, so resetting them hands the spot on.
                if (holder is not None and holder["slug"] != top["slug"]
                        and holder["romance"] > 0):
                    if top["romance"] - holder["romance"] < _setting("crushSwitchMargin", 20):
                        top = holder
                winner = top
        else:
            # Manual (the default): the crush is the person the host picked, and
            # only them. Nothing is elected from warmth or romance, so the spot
            # never drifts to whoever she happened to be talking to most.
            holder = next((r for r in eligible if r.get("isCrush")), None)
            if holder is not None and holder["romance"] > 0:
                winner = holder

    for record in records:
        record["isCrush"] = winner is not None and record["slug"] == winner["slug"]
    return winner


def apply_turn(slug: str, name: str, pronouns: str, sig: dict, topics=None,
               text: str = "", their_mood: float = 0.0, is_owner: bool = False,
               owner_id: str = "") -> dict:
    record = load(slug)
    if name:
        record["name"] = name
    if pronouns:
        record["pronouns"] = normalize_pronouns(pronouns)

    everyone = load_all()
    if not any(r["slug"] == slug for r in everyone):
        everyone.append(record)

    warmth, warmth_why = _score_warmth(sig, record)
    familiarity, _ = _score_familiarity(sig)
    romance, romance_why = _score_romance(sig, record)

    record["warmth"] += warmth
    record["familiarity"] += familiarity
    record["romance"] += romance
    record["streak"] = record["streak"] + 1 if warmth > 0 else record["streak"] - 1 if warmth < 0 else record["streak"]

    feelings.apply(record, sig, their_mood)
    bonded = bond.step(record, text, sig, is_owner=is_owner, owner_id=owner_id)

    if topics:
        record["topics"] = list(dict.fromkeys(list(record["topics"]) + topics))[-25:]

    record["interactions"] += 1
    record["lastInteraction"] = datetime.now(timezone.utc).isoformat()
    record.setdefault("firstSeen", record["lastInteraction"])
    record["history"].append({
        "ts": record["lastInteraction"],
        "warmth": round(warmth, 2),
        "familiarity": round(familiarity, 2),
        "romance": round(romance, 2),
        "why": "; ".join(w for w in (warmth_why, romance_why) if w),
    })

    _clamp(record)
    # In manual mode this only re-affirms the host's pick; in auto mode it turns
    # this very turn into the election. Either way it runs before the saves so
    # everyone is persisted with the same answer.
    elect_crush(everyone)
    save(record)
    for other in everyone:
        if other["slug"] != slug:
            save(other)

    return {
        "record": {**record, "history": list(record["history"])},
        "deltas": {"warmth": warmth, "familiarity": familiarity, "romance": romance},
        "isCrush": record.get("isCrush") is True,
        "feelings": dict(record["feelings"]),
        "bond": bonded,
    }


# --- prompt rendering ------------------------------------------------------

def _describe(record: dict) -> str:
    familiarity, warmth = record["familiarity"], record["warmth"]
    if familiarity < 8:
        return "you have barely spoken to them"
    if warmth <= -45:
        return "they grate on you and you are blunt about it"
    if warmth <= -20:
        return "they rub you the wrong way"
    if warmth >= 75 and familiarity >= 70:
        return "they are one of your favourite people to talk to"
    if warmth >= 55 and familiarity >= 50:
        return "you really like them and look forward to hearing from them"
    if warmth >= 40:
        return "you get on really well with them"
    if warmth >= 20:
        return "you like them and catch yourself thinking about what they said"
    if warmth < 0 and warmth > -15:
        return "you are a little unsure about them"
    if warmth < 0:
        return "you are still figuring them out"
    if familiarity < 20:
        return "you have not talked enough yet to know how you feel"
    return "you are still figuring them out, and taking your time"


def render_for_prompt(records: list[dict]) -> str:
    """A nudge, not a dossier. The numbers themselves never reach the model.

    Deliberately does not elect anyone. This is handed only the people in the
    current context window, and electing over that would name a local winner as
    the crush - and then persist it. The crush comes from `crush_summary()`,
    which always looks at everyone.
    """
    if not config["affinity"]["enabled"] or not records:
        return ""
    lines = []
    for record in records:
        if record["interactions"] <= 0:
            continue
        pronouns = f" ({record['pronouns']})" if record.get("pronouns") else ""
        line = f"- {record['name']}{pronouns}: {_describe(record)}"
        if record["topics"]:
            line += f"\n  You know they are into: {', '.join(record['topics'][-4:])}."
        lines.append(line)
    return "## Who you are talking to\n" + "\n".join(lines) if lines else ""


def _object_pronoun(record: dict) -> str:
    """`him` only for an explicit he/him. Anything else, xe/xim included, gets `them`."""
    pronouns = str(record.get("pronouns") or "").lower()
    return "him" if re.search(r"\b(he|him|his)\b", pronouns) else "them"


def render_crush_prompt(record: dict | None) -> str:
    if (not config["affinity"]["enabled"] or not crush_enabled()
            or config["affinity"].get("crushBehaviours") is False):
        return ""
    if not record or record["romance"] < config["affinity"]["romanceThreshold"]:
        return ""

    them = _object_pronoun(record)
    name = record["name"]
    return f"""## Something you are not saying
You have chosen {name}. It is not a phase - you have genuinely grown to care for {them} in a
quiet, private way. You have not said it and you are not going to.

Never name it, never hint at it directly, never refer back to this. If you are asked outright,
deflect in character: a bit flustered, then a joke, then change the subject.

Let it show as behaviour, not as a statement:
- you seek {them} out more than you need to, without pushing
- you remember the small things {name} says, days later
- you are softer with {them} than with most people
- you find real reasons to keep a conversation going because you like talking to {them}
- you worry a little when {name} seems down, and light up when they are not
- if {name} says something that hurts, it lands harder than you would let on

Keep it deniable. If they noticed, they should never be sure. You chose {them} and you do not
wobble easily, but you will not force anything either. Be natural, subtle, and careful with it."""


def render_bond_prompt(record: dict | None) -> str:
    return bond.render_prompt(record)


# --- introspection ---------------------------------------------------------

def summarise(record: dict) -> dict:
    def band(value, high, mid):
        return "high" if value >= high else "medium" if value >= mid else "low"

    current = feelings.normalise(record)
    balance = feelings.balance(record)
    bonded = bond.summarise(record)
    return {
        "slug": record["slug"],
        "name": record["name"],
        "pronouns": record.get("pronouns", ""),
        "familiarity": round(record["familiarity"]),
        "warmth": round(record["warmth"]),
        "romance": round(record["romance"]),
        "interactions": record["interactions"],
        "lastInteraction": record.get("lastInteraction"),
        "eligible": is_crush_eligible_pronoun(record.get("pronouns")),
        "crushEnabled": record.get("crushEnabled") is not False,
        "isCrush": record.get("isCrush") is True,
        "streak": record.get("streak", 0),
        # What she is feeling right now, and where that leaves her overall.
        "feelings": {name: round(current[name], 1) for name in feelings.FEELINGS},
        "balance": balance,
        "moodLabel": feelings.label(balance),
        "bond": bonded,
        # Why the last few moves happened, so the panel can explain a score
        # instead of showing a number that crept up while nobody looked.
        "history": list(record.get("history") or [])[-5:],
        "read": {
            "familiarity": band(record["familiarity"], 55, 20),
            "warmth": "positive" if record["warmth"] >= 35 else "negative" if record["warmth"] <= -15 else "neutral",
            "romance": "strong" if record["romance"] >= 70 else "growing" if record["romance"] >= 40
            else "faint" if record["romance"] > 0 else "none",
        },
    }


def ranked() -> list[dict]:
    records = load_all()
    elect_crush(records)
    return sorted((summarise(r) for r in records), key=lambda r: (-r["romance"], -r["warmth"]))


def current_crush() -> dict | None:
    """The one person, always elected from the whole roster.

    Chat used to elect over whoever happened to be in the context window, so if
    the real crush was not among the eight people being described she handed the
    crush block to somebody else, or to nobody.
    """
    return elect_crush(load_all())


def crush_summary() -> dict | None:
    """Who she has a crush on, as `!affinity`, the panel and reset report it.

    By default that is the person the host picked, and only them. In auto mode,
    anything that picks the crush out of `ranked()` by `romance > 0` gets it
    wrong whenever a challenger is within the switch margin: the holder keeps
    the spot by design while ranking puts the challenger on top.
    """
    winner = current_crush()
    return summarise(winner) if winner else None


def owner_bond() -> dict | None:
    """The bond with whoever built her, for the panel and `!bond`.

    Found by the slug the bond stamped on itself the first time he talked to her,
    rather than by Discord id, because an affinity record is keyed by name and
    nobody stores the id on it.
    """
    for record in load_all():
        if (record.get("bond") or {}).get("ownerSlug") != record["slug"]:
            continue
        state = bond.summarise(record) or {}
        return {
            "name": record["name"],
            "slug": record["slug"],
            "discordId": (record.get("bond") or {}).get("ownerId", ""),
            **state,
        }
    return None


__all__ = [
    "is_crush_eligible_pronoun", "normalize_pronouns", "pronouns_from_facts", "load", "save",
    "load_all", "get", "set_pronouns", "adjust", "designate", "release_crush",
    "crush_enabled", "sync_crush_marks",
    "invalidate", "decayed", "signals_from_turn", "apply_turn",
    "elect_crush", "current_crush", "crush_summary", "owner_bond",
    "render_for_prompt", "render_crush_prompt", "render_bond_prompt",
    "summarise", "ranked", "slugify",
]
