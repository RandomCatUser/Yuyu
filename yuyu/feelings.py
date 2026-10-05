"""How she feels right now - the fast numbers.

Warmth and the crush take weeks; these move a few points per message and decay on
their own over the next hour or two. Injected into the prompt and editable by hand."""

from __future__ import annotations

import time
from datetime import datetime, timezone

from .config import config
from .util import clamp

# Kept short on purpose. A long list of feelings mostly produces mush - these
# eight cover what actually happens to a person over a conversation.
FEELINGS = ("happy", "calm", "playful", "excited", "worried", "sad", "annoyed", "tired")

BRIGHT = ("happy", "excited", "playful")
HEAVY = ("sad", "worried", "annoyed", "tired")

# Wording she actually reads. The panel shows the raw names instead.
WORDS = {
    "happy": "happy",
    "calm": "settled",
    "playful": "in a playful mood",
    "excited": "genuinely excited",
    "worried": "worried about them",
    "sad": "a bit sad",
    "annoyed": "a bit put off",
    "tired": "tired",
}

# Below this a feeling is noise and gets left out of the prompt entirely.
NOTABLE = 22.0


def blank() -> dict:
    return {name: 0.0 for name in FEELINGS}


def _setting(key: str, fallback: float) -> float:
    try:
        return float(config["affinity"].get(key) or fallback)
    except (TypeError, ValueError):
        return fallback


def half_life_minutes() -> float:
    """How long one feeling takes to halve. Long enough to carry a conversation."""
    return max(1.0, _setting("feelingHalfLifeMinutes", 120.0))


def enabled() -> bool:
    return config["affinity"].get("feelings", True) is not False


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_moment(record: dict) -> float:
    try:
        return datetime.fromisoformat(record["feelingsAt"]).timestamp()
    except (KeyError, TypeError, ValueError):
        return time.time()


def _clamp_all(feelings: dict) -> dict:
    for name in FEELINGS:
        feelings[name] = clamp(float(feelings.get(name) or 0), 0, 100)
    # Over a list, not the dict itself: deleting while iterating a dict raises.
    for name in [key for key in feelings if key not in FEELINGS]:
        del feelings[name]
    return feelings


def normalise(record: dict) -> dict:
    """Make sure the record has a usable feelings block, whatever is on disk."""
    feelings = record.get("feelings")
    if not isinstance(feelings, dict):
        feelings = blank()
    for name in FEELINGS:
        try:
            feelings[name] = float(feelings.get(name) or 0)
        except (TypeError, ValueError):
            feelings[name] = 0.0
    _clamp_all(feelings)
    record["feelings"] = feelings
    return feelings


def decay(record: dict, now: float | None = None) -> dict:
    """Fade the feelings toward neutral, on a half-life.

    Done on read like the other decay, so a record nobody has touched still
    reports what she would actually feel right now rather than what she felt
    when it was written.
    """
    if not enabled() or not record.get("feelings"):
        return record
    now = now if now is not None else time.time()
    minutes = max(0.0, (now - _read_moment(record)) / 60.0)
    if minutes < 0.5:
        return record
    factor = 0.5 ** (minutes / half_life_minutes())
    feelings = normalise(record)
    for name in FEELINGS:
        # Very small values round to nothing rather than crawling forever.
        feelings[name] = 0.0 if feelings[name] * factor < 0.5 else feelings[name] * factor
    record["feelingsAt"] = _stamp()
    return record


def _bump(feelings: dict, name: str, amount: float) -> None:
    feelings[name] = clamp(feelings.get(name, 0.0) + amount, 0, 100)


def apply(record: dict, sig: dict, their_mood: float = 0.0) -> dict:
    """Move her feelings from one turn.

    Small steps on purpose. A single good message should not leave her delighted
    for the rest of the day - that reads as a bot on a high - while a run of
    bad ones should stack, because that is how being ignored actually lands.
    """
    if not enabled():
        return record
    feelings = normalise(record)

    if sig.get("harsh"):
        _bump(feelings, "annoyed", 16)
        _bump(feelings, "sad", 7)
    if sig.get("dismissed"):
        _bump(feelings, "annoyed", 9)
    if sig.get("commanded"):
        _bump(feelings, "annoyed", 6)
    if sig.get("ignored") or sig.get("distant"):
        _bump(feelings, "sad", 8)
        _bump(feelings, "worried", 3)
    if sig.get("apologised"):
        _bump(feelings, "annoyed", -14)
        _bump(feelings, "happy", 4)
    if sig.get("warm"):
        _bump(feelings, "happy", 7)
        _bump(feelings, "calm", 3)
    if sig.get("checkedIn"):
        _bump(feelings, "happy", 6)
        _bump(feelings, "calm", 4)
    if sig.get("gratitude"):
        _bump(feelings, "happy", 5)
    if sig.get("teased"):
        _bump(feelings, "playful", 9)
    if sig.get("sharedTopics", 0) > 0:
        _bump(feelings, "excited", 4)
        _bump(feelings, "happy", 3)
    if sig.get("revealedSomething"):
        _bump(feelings, "calm", 5)
        _bump(feelings, "happy", 3)
    if sig.get("lateNight"):
        _bump(feelings, "tired", 9)
    if sig.get("directReply"):
        _bump(feelings, "happy", 3)

    # Someone she cares about sounding bad is its own thing. It is not her
    # mood - it is what she feels for them, which is why it lands softer.
    if their_mood < -25:
        _bump(feelings, "worried", min(16, abs(their_mood) * 0.25))
    elif their_mood > 30:
        _bump(feelings, "happy", 4)

    # Standing in her own light company reads as flat, so let it top up on its
    # own between the bigger things.
    _bump(feelings, "calm", 2)

    record["feelings"] = feelings
    record["feelingsAt"] = _stamp()
    return feelings


def top(record: dict, count: int = 2) -> list[tuple[str, float]]:
    """The feelings worth mentioning, strongest first."""
    feelings = normalise(record)
    ranked = sorted(((name, feelings[name]) for name in FEELINGS), key=lambda pair: -pair[1])
    return [(name, value) for name, value in ranked[:count] if value >= NOTABLE]


def balance(record: dict) -> float:
    """One number for the panel: bright minus heavy, -100 to 100."""
    feelings = normalise(record)
    bright = sum(feelings[name] for name in BRIGHT)
    heavy = sum(feelings[name] for name in HEAVY)
    return round(clamp((bright - heavy * 0.7) / 1.5, -100, 100), 1)


def label(value: float) -> str:
    if value >= 45:
        return "great"
    if value >= 15:
        return "good"
    if value > -15:
        return "flat"
    if value > -45:
        return "low"
    return "rough"


def describe(record: dict) -> str:
    """One clean line for the prompt, or nothing when she is level."""
    if not enabled():
        return ""
    leading = top(record)
    if not leading:
        return "you feel pretty level about them right now"

    bits = [f"{WORDS[name]} ({round(value)})" for name, value in leading]
    if len(bits) == 1:
        body = bits[0]
    else:
        body = f"{bits[0]}, and {bits[1]}"
    return f"right now, talking to them, you feel {body}"


def tone_hint(record: dict) -> str:
    """The one instruction that keeps the feelings from being decoration."""
    feelings = normalise(record)
    heavy = sum(feelings[name] for name in HEAVY)
    bright = sum(feelings[name] for name in BRIGHT)
    if heavy >= 45 and heavy > bright:
        return "You are a bit off with them. Let it show - shorter replies, less patience, less warmth."
    if bright >= 60:
        return "You are in a good mood. Let it come through instead of sounding polite."
    return ""


def set_feelings(record: dict, values: dict) -> dict:
    """Host edit. Only the names she actually has, numbers only."""
    feelings = normalise(record)
    for name in FEELINGS:
        if name not in values:
            continue
        try:
            feelings[name] = clamp(float(values[name]), 0, 100)
        except (TypeError, ValueError):
            raise ValueError(f"{name} has to be a number") from None
    record["feelings"] = feelings
    record["feelingsAt"] = _stamp()
    return feelings


def render_for_prompt(records: list[dict]) -> str:
    """Feelings, in words, for the people in this conversation."""
    if not enabled():
        return ""
    lines = []
    for record in records:
        if record.get("interactions", 0) <= 0:
            continue
        line = f"- {record['name']}: {describe(record)}"
        hint = tone_hint(record)
        if hint:
            line += f"\n  {hint}"
        lines.append(line)
    return "\n".join(lines)


__all__ = [
    "FEELINGS", "WORDS", "BRIGHT", "HEAVY", "blank", "enabled", "half_life_minutes",
    "normalise", "decay", "apply", "top", "balance", "label", "describe", "tone_hint",
    "set_feelings", "render_for_prompt",
]
