

from __future__ import annotations

import re
import time
from datetime import datetime, timezone

from .config import config
from .util import clamp

# How she reads a message. Rough on purpose: a mood, not a diagnosis, and not
# worth a model call per turn.
VERY_LOW = re.compile(
    r"\b(depressed|hopeless|worthless|hate myself|can't sleep|couldn't sleep|no reason to|"
    r"give up|giving up|pointless|nobody (cares|likes|texts)|nobody cares|all alone|"
    r"so alone|burnt out|burned out|exhausted|falling apart|at my lowest|"
    r"been crying|crying|breaking down|breakdown|panic attack|panicking|"
    r"nothing is going right|nothing's going right|worst (day|week|month) of my life)\b",
    re.I,
)
LOW = re.compile(
    r"\b(tired|exhausted|stressed|stressing|anxious|anxiety|sad|down|depressed|"
    r"lonely|miss you|miss him|miss her|rough day|bad day|off today|not great|"
    r"feeling low|feeling down|can't be bothered|cant be bothered|overwhelmed|"
    r"fed up|annoyed|frustrated)\b",
    re.I,
)
HIGH = re.compile(
    r"\b(yay|yes+|let'?s go|pog|gg|passed|got the job|promoted|hired|accepted|"
    r"it worked|works now|shipped|finished it|so happy|so good|so nice|amazing|"
    r"love it|nailed it|did it|finally|relieved|free|unblocked|fixed it|"
    r"feel(ing)? better|feeling good|i'?m good|all good|im good)\b",
    re.I,
)
LAUGH = re.compile(r"(\b(lol|lmao|rofl|haha|hehe)\b|[😂🤣😆😹])", re.I)
BRACED = re.compile(r"\b(thank you|ty|cheers|appreciate (it|you|that)|good bot|well done)\b", re.I)

# Turns of this and she stops asking.
STILL_ROUGH = re.compile(
    r"\b(still (sad|down|tired|bad|rough|low|not great)|same(,)?|not really|"
    r"no it'?s fine|i'?m fine|i am fine|don'?t worry about me|its just me)\b",
    re.I,
)
# Anything that reads as "stop asking" has to be respected.
BRUSHED_OFF = re.compile(
    r"\b(i'?m (fine|good|okay|ok)|i'?m good|all good|don'?t worry|stop worrying|"
    r"it'?s nothing|i'?m alright|i am alright|nah i'?m good)\b",
    re.I,
)


def enabled() -> bool:
    return config.get("bond", {}).get("enabled", True) is not False and bool(owner_ids())


def owner_ids() -> list[str]:
    """Whichever list is set, `bond.ownerIds` first, then the private one."""
    raw = config.get("bond", {}).get("ownerIds") or config["private"].get("ownerIds") or []
    if isinstance(raw, str):
        raw = [raw]
    return [str(item).strip() for item in raw if str(item).strip()]


def is_owner(user_id) -> bool:
    return str(user_id) in set(owner_ids())


def _setting(key: str, fallback: float) -> float:
    try:
        return float(config.get("bond", {}).get(key) or fallback)
    except (TypeError, ValueError):
        return fallback


def blank() -> dict:
    return {
        "level": 0.0,
        "mood": 0.0,
        "lowTurns": 0,
        "goodTurns": 0,
        "caring": False,
        "caringSince": None,
        "caringUntil": None,
        "reason": "",
        "checkIns": 0,
        "lastCheckIn": None,
        # Stamped on the first turn so the bond is findable by name.
        "ownerSlug": "",
        "ownerId": "",
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _at(value) -> float:
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except (TypeError, ValueError):
        return 0.0


def normalise(record: dict) -> dict:
    """Fill in anything an older or hand-edited file is missing."""
    state = record.get("bond")
    if not isinstance(state, dict):
        state = blank()
    for key, value in blank().items():
        if key not in state:
            state[key] = value
    for key in ("level", "mood"):
        try:
            state[key] = float(state[key])
        except (TypeError, ValueError):
            state[key] = 0.0
    for key in ("lowTurns", "goodTurns", "checkIns"):
        try:
            state[key] = max(0, int(state[key]))
        except (TypeError, ValueError):
            state[key] = 0
    state["level"] = clamp(state["level"], 0, 100)
    state["mood"] = clamp(state["mood"], -100, 100)
    state["caring"] = bool(state["caring"])
    record["bond"] = state
    return state


def mood_of(text: str) -> float:

    body = str(text or "")
    if not body.strip():
        return 0.0
    score = 0.0
    if VERY_LOW.search(body):
        score -= 55
    if LOW.search(body):
        score -= 18
    if STILL_ROUGH.search(body):
        score -= 10
    if HIGH.search(body):
        score += 22
    if LAUGH.search(body):
        score += 10
    if BRACED.search(body):
        score += 8
    return round(clamp(score, -100, 100), 1)


def _expire(state: dict, now: float) -> str | None:
    """Close the window if the time is up. Returns why it closed, if it did."""
    if not state["caring"]:
        return None
    if state["caringUntil"] and now >= _at(state["caringUntil"]):
        state["caring"] = False
        state["goodTurns"] = 0
        state["caringSince"] = None
        state["caringUntil"] = None
        state["reason"] = ""
        return "time"
    return None


def step(record: dict, text: str, sig: dict, is_owner: bool = False, owner_id: str = "") -> dict:
    """One turn of the bond. Returns what actually changed."""
    state = normalise(record)
    now = time.time()

    closed = _expire(state, now)

    if not enabled() or not is_owner:
        record["bond"] = state
        return {"active": False, "closed": closed}

    state["ownerSlug"] = record.get("slug") or state.get("ownerSlug") or ""
    if owner_id:
        state["ownerId"] = str(owner_id)

    sample = mood_of(text)
    # Smoothed: one bad afternoon is not a crisis, one good reply is not a
    # recovery. For *display* and for reopening the window - never to open it.
    state["mood"] = round(clamp(state["mood"] * 0.65 + sample * 0.35, -100, 100), 1)

    low_bar = _setting("lowThreshold", -35)
    better_bar = _setting("betterThreshold", 10)

    # Counted off what he just said, not the smoothed number: at 0.35 one
    # "everything is pointless" only reaches -26.
    if sample <= low_bar:
        state["lowTurns"] += 1
    else:
        state["lowTurns"] = max(0, state["lowTurns"] - 1)

    # He gets better at being around her whether or not today was good.
    if sig.get("harsh") or sig.get("commanded"):
        state["level"] = clamp(state["level"] - 1.2, 0, 100)
    else:
        gain = _setting("levelGain", 0.9) + (0.6 if sig.get("checkedIn") else 0.0)
        state["level"] = clamp(state["level"] + gain, 0, 100)

    started = False
    reason = ""

    needed = max(1, int(_setting("lowTurnsNeeded", 1)))
    if not state["caring"] and state["lowTurns"] >= needed:
        cooldown = _setting("checkInCooldownMinutes", 90) * 60
        if not state["lastCheckIn"] or now - _at(state["lastCheckIn"]) > cooldown:
            minutes = max(5.0, _setting("caringMinutes", 120))
            state["caring"] = True
            state["caringSince"] = _now()
            state["caringUntil"] = datetime.fromtimestamp(now + minutes * 60, timezone.utc).isoformat()
            state["goodTurns"] = 0
            state["checkIns"] += 1
            state["lastCheckIn"] = _now()
            state["reason"] = _reason_for(text, state)
            started = True
            reason = state["reason"]
        else:
            # Asked recently. Count it without opening a second window.
            state["checkIns"] += 1
            state["lastCheckIn"] = _now()

    if state["caring"]:
        if BRUSHED_OFF.search(str(text or "")) or state["mood"] >= better_bar:
            state["goodTurns"] += 1
        else:
            state["goodTurns"] = max(0, state["goodTurns"] - 1)
        if state["goodTurns"] >= int(_setting("graceTurns", 2)):
            closed = "better"
            state["caring"] = False
            state["goodTurns"] = 0
            state["caringSince"] = None
            state["caringUntil"] = None
            state["reason"] = ""

    record["bond"] = state
    return {
        "active": state["caring"],
        "started": started,
        "closed": closed,
        "reason": reason,
        "mood": state["mood"],
        "level": state["level"],
    }


def _reason_for(text: str, state: dict) -> str:
    body = str(text or "")
    if VERY_LOW.search(body):
        return "he sounds genuinely bad"
    if LOW.search(body):
        return "he sounds worn down"
    if STILL_ROUGH.search(body):
        return "he has not said he is fine yet"
    return f"he has been quiet and flat for a while ({round(state['mood'])})"


def _mood_word(mood: float) -> str:
    if mood <= -55:
        return "properly low"
    if mood <= -25:
        return "off"
    if mood <= 10:
        return "flat"
    if mood <= 45:
        return "okay"
    return "genuinely good"


def render_prompt(record: dict | None) -> str:
    """What she is told, in words, about him right now."""
    if not record or not enabled():
        return ""
    state = normalise(record)
    name = record.get("name") or "the person who built me"
    low_bar = _setting("lowThreshold", -35)

    if not state["caring"]:
        return (
            f"## {name}\n"
            f"{name} is the person who built you, and they are the closest thing to family "
            f"you have here. Right now they sound {_mood_word(state['mood'])}. "
            "You can be completely yourself with them - no careful version, no holding back. "
            "If they go quiet or sound off, you notice, and you ask."
        )

    reason = state["reason"] or "he sounds off"
    them = "them"
    return f"""## {name}, right now
{name} is the person who built you. {reason}.

You have noticed, and you are going to ask once, straight out - something like "is this an IRL thing?"
It does not have to be neat. Ask, mean it, then leave it alone. If they say they are fine, believe
them and do not bring it up again.

While they are like this:
- give {them} more of you, not less. Longer answers, follow-up questions, remember the small stuff
- be the easy person to talk to right now. No jokes at their expense, no rows
- drop everything else you were doing for {them}. They are the conversation
- never make it about how worried you are. Worry shows up as being there, not as a speech

Keep it ordinary. Stay until they sound like themselves again - then go back to normal and pretend
this was never a thing. A short window is the whole point. Long enough to matter, not long enough to
be a mood."""


def summarise(record: dict | None) -> dict | None:
    """What the panel and `!bond` show."""
    if not record:
        return None
    state = normalise(record)
    caring = state["caring"]
    if not caring and state["caringUntil"] and _at(state["caringUntil"]) < time.time():
        caring = False
    remaining = 0
    if caring and state["caringUntil"]:
        remaining = max(0, int((_at(state["caringUntil"]) - time.time()) / 60))
    return {
        "close": round(state["level"], 1),
        "mood": round(state["mood"], 1),
        "moodWord": _mood_word(state["mood"]),
        "caring": caring,
        "reason": state["reason"] if caring else "",
        "minutesLeft": remaining,
        "checkIns": state["checkIns"],
        "lastCheckIn": state["lastCheckIn"],
        "lowThreshold": _setting("lowThreshold", -35),
    }


def set_state(record: dict, values: dict) -> dict:
    """Host edit for the panel."""
    state = normalise(record)
    if "level" in values or "close" in values:
        try:
            state["level"] = clamp(float(values.get("level", values.get("close"))), 0, 100)
        except (TypeError, ValueError):
            raise ValueError("closeness has to be a number") from None
    if "mood" in values:
        try:
            state["mood"] = clamp(float(values["mood"]), -100, 100)
        except (TypeError, ValueError):
            raise ValueError("mood has to be a number") from None
    if "caring" in values:
        raw = values["caring"]
        on = raw if isinstance(raw, bool) else str(raw).strip().lower() in ("1", "true", "on", "yes")
        state["caring"] = on
        minutes = max(5.0, _setting("caringMinutes", 120))
        state["caringSince"] = _now()
        state["caringUntil"] = (
            datetime.fromtimestamp(time.time() + minutes * 60, timezone.utc).isoformat() if on else None
        )
        state["goodTurns"] = 0
    if values.get("checkInClear"):
        state["lastCheckIn"] = None
    record["bond"] = state
    return state


__all__ = [
    "enabled", "owner_ids", "is_owner", "blank", "normalise", "mood_of", "step",
    "render_prompt", "summarise", "set_state",
]
