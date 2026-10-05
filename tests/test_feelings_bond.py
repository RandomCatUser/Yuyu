"""Her temporary feelings, and the bond with whoever built her.

Both are new, and both are the sort of thing that looks fine until something
subtle is wrong: a feeling that never fades, a caring window that never closes,
warmth that climbs no matter how somebody treats her.
"""

from __future__ import annotations

import re
import time

import pytest

from yuyu import affinity as aff
from yuyu import bond, feelings
from yuyu.config import config

GOOD = dict(addressed=True, directReply=True, askedAboutHer=True, sharedTopics=3,
            revealedSomething=True, lateNight=True, gratitude=False, harsh=False,
            commanded=False, ignored=False)
HARSH = dict(addressed=True, harsh=True, commanded=True, directReply=False,
             askedAboutHer=False, sharedTopics=0, revealedSomething=False,
             lateNight=False, gratitude=False, ignored=False)
IGNORE = dict(addressed=False, directReply=False, askedAboutHer=False, sharedTopics=0,
              revealedSomething=False, lateNight=False, gratitude=False, harsh=False,
              commanded=False, ignored=True)


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    """Scratch records, never the live ones. Same reasoning as test_affinity."""
    monkeypatch.setattr(aff, "AFFINITY_DIR", tmp_path)
    monkeypatch.setitem(config["affinity"], "feelings", True)
    monkeypatch.setitem(config["affinity"], "feelingHalfLifeMinutes", 120.0)
    monkeypatch.setitem(config["bond"], "enabled", True)
    monkeypatch.setitem(config["bond"], "ownerIds", ["1"])
    aff.invalidate()
    yield
    aff.invalidate()


def _turn(slug, name, sig, times=1, **kwargs):
    result = None
    for _ in range(times):
        result = aff.apply_turn(slug, name, "he/him", dict(sig), **kwargs)
    return result


def _owner():
    return {**GOOD, "harsh": False, "commanded": False}


# --- the shape of the thing --------------------------------------------------

def test_there_are_eight_feelings_and_eight_words():
    assert len(feelings.FEELINGS) == 8
    assert set(feelings.WORDS) == set(feelings.FEELINGS)
    # A word that is just the key again is not really wording.
    assert feelings.WORDS["calm"] != "calm"


def test_a_blank_feeling_is_flat_and_harmless():
    flat = feelings.balance({"feelings": feelings.blank()})
    assert flat == 0.0
    assert feelings.describe({"feelings": feelings.blank()}) == "you feel pretty level about them right now"


def test_a_junk_feelings_block_is_replaced_not_raised():
    record = {"slug": "ray", "feelings": {"happy": "loads", "nonsense": 40}}
    out = feelings.normalise(record)
    assert out["happy"] == 0.0
    assert "nonsense" not in out
    assert set(out) == set(feelings.FEELINGS)


# --- small steps, not big jumps ---------------------------------------------

def test_one_good_message_moves_nothing_wildly():
    _turn("ray", "Ray", GOOD)
    f = aff.load("ray")["feelings"]
    assert 0 < f["happy"] < 30, f"a single kind message should not leave her delighted: {f}"
    assert f["annoyed"] == 0


def test_a_run_of_ignored_messages_stacks_up():
    for _ in range(4):
        _turn("ray", "Ray", IGNORE)
    f = aff.load("ray")["feelings"]
    assert f["sad"] > 20, f"being ignored should land: {f}"
    assert f["happy"] == 0


def test_an_apology_undoes_most_of_the_annoyance():
    _turn("ray", "Ray", HARSH)
    before = aff.load("ray")["feelings"]["annoyed"]
    _turn("ray", "Ray", {**GOOD, "apologised": True})
    after = aff.load("ray")["feelings"]["annoyed"]
    assert before > 0
    assert after < before * 0.5, f"she should cool off when he says sorry: {before} -> {after}"


def test_feelings_are_capped():
    for _ in range(200):
        _turn("ray", "Ray", {**GOOD, "teased": True, "gratitude": True})
    f = aff.load("ray")["feelings"]
    assert all(0 <= v <= 100 for v in f.values()), f


# --- fading on their own -----------------------------------------------------

def test_feelings_halve_on_the_configured_half_life():
    """Each read fades from the last write, so the factor has to be measured
    from a fresh record every time rather than applied twice to the same one."""
    _turn("ray", "Ray", {**GOOD, "teased": True})
    half = feelings.half_life_minutes()
    start = aff.load("ray")["feelings"]["playful"]
    assert start > 0

    for laps, expected in ((1, start / 2), (2, start / 4), (3, start / 8)):
        # load() hands back the cached object, so drop it between measurements
        # or each lap compounds on the last one's fade.
        aff.invalidate()
        record = aff.load("ray")
        ago = time.time() - half * 60 * laps
        record["feelingsAt"] = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(ago))
        got = feelings.decay(record)["feelings"]["playful"]
        assert got == pytest.approx(expected, abs=0.6), f"after {laps} half-lives: {got} vs {expected}"


def test_faded_feelings_stop_at_nothing_and_do_not_go_negative(monkeypatch):
    _turn("ray", "Ray", {**GOOD, "teased": True})
    record = aff.load("ray")
    record["feelingsAt"] = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() - 86400 * 60))
    out = feelings.decay(record)["feelings"]
    assert all(v == 0 for v in out.values()), f"a day later nothing should still be colouring her: {out}"


def test_turning_feelings_off_means_they_stop_moving(monkeypatch):
    monkeypatch.setitem(config["affinity"], "feelings", False)
    _turn("ray", "Ray", {**GOOD, "teased": True})
    assert aff.load("ray")["feelings"] == feelings.blank()


# --- how the panel sees it ---------------------------------------------------

def test_the_prompt_gets_words_and_not_the_numbers():
    """One tease is not worth telling the model about. A conversation is."""
    TEASE = {k: v for k, v in GOOD.items() if k not in ("askedAboutHer", "revealedSomething")}
    _turn("ray", "Ray", TEASE)
    assert feelings.render_for_prompt([aff.load("ray")]) == "- Ray: you feel pretty level about them right now"

    _turn("ray", "Ray", {**TEASE, "teased": True}, times=5)
    text = feelings.render_for_prompt([aff.load("ray")])
    assert "playful" in text, f"a real mood should reach her: {text}"
    assert text.count("\n") <= 1, "one line per person, plus at most a tone line"
    # At most two, whatever else is going on. All eight at once turns her into a
    # mood dashboard instead of a person.
    assert len(re.findall(r"\(\d+\)", text)) == 2, text
    assert re.fullmatch(r"- Ray: right now, talking to them, you feel .+", text.split("\n")[0])


def test_a_person_she_has_never_met_gets_no_feeling_line():
    ghost = {"slug": "ghost", "name": "Ghost", "interactions": 0, "feelings": feelings.blank()}
    assert feelings.render_for_prompt([ghost]) == ""


def test_the_label_reads_as_a_mood_not_a_score():
    assert feelings.label(90) == "great"
    assert feelings.label(0) == "flat"
    assert feelings.label(-90) == "rough"


# --- host edits --------------------------------------------------------------

def test_the_panel_can_set_a_feeling_by_hand():
    _turn("ray", "Ray", GOOD)
    aff.adjust("ray", {"feelings": {"happy": 90}})
    assert aff.load("ray")["feelings"]["happy"] == 90


@pytest.mark.parametrize("bad", [{"happy": "loads"}, {"happy": None}, {"excited": [1]}])
def test_a_refused_feeling_is_not_half_applied(bad):
    _turn("ray", "Ray", GOOD)
    before = dict(aff.load("ray")["feelings"])
    with pytest.raises(ValueError):
        aff.adjust("ray", {"feelings": bad})
    assert aff.load("ray")["feelings"] == before


# --- warmth: slow, real, and hard to shake -----------------------------------

def test_warmth_rises_when_he_is_kind():
    before = aff.load("ray")["warmth"] if (aff.AFFINITY_DIR / "ray.json").exists() else 0.0
    _turn("ray", "Ray", GOOD, times=6)
    assert aff.load("ray")["warmth"] > before + 6


def test_warmth_falls_when_he_is_not():
    _turn("ray", "Ray", GOOD, times=6)
    warm = aff.load("ray")["warmth"]
    _turn("ray", "Ray", HARSH, times=3)
    assert aff.load("ray")["warmth"] < warm


def test_a_bad_streak_costs_more_than_a_good_one_gains():
    """Asymmetry on purpose: you can undo a good week in a bad afternoon."""
    _turn("ray", "Ray", GOOD, times=8)
    up = aff.load("ray")["warmth"] / 8
    _turn("kim", "Kim", GOOD, times=8)
    _turn("kim", "Kim", HARSH, times=8)
    down = aff.load("kim")["warmth"] / 16
    assert down < up, f"a bad turn should bite harder than a good one ({down} vs {up})"


def test_warmth_gains_are_damped_when_she_is_already_put_off():
    """She does not cheer right back up while still annoyed - it has to wear off."""
    _turn("ray", "Ray", HARSH, times=4)
    annoyed = aff.load("ray")["feelings"]["annoyed"]
    before = aff.load("ray")["warmth"]
    result = _turn("ray", "Ray", {**GOOD, "gratitude": True})
    assert annoyed > 0, "precondition: she is still annoyed"
    assert result["deltas"]["warmth"] > 0


def test_the_streak_is_recorded_and_reverses():
    _turn("ray", "Ray", GOOD, times=3)
    assert aff.load("ray")["streak"] == 3
    _turn("ray", "Ray", HARSH)
    assert aff.load("ray")["streak"] < 3


def test_the_history_says_why_in_words():
    _turn("ray", "Ray", GOOD, times=2)
    entry = aff.load("ray")["history"][-1]
    assert entry["why"], "a score that moved with no explanation is not reviewable"
    assert entry["ts"]


# --- the bond ----------------------------------------------------------------

def test_a_rough_mood_is_read_as_rough():
    assert bond.mood_of("honestly i'm so tired of everything") < 0
    assert bond.mood_of("no reason to keep going") < -40
    assert bond.mood_of("YAY i passed!!") > 0
    assert bond.mood_of("") == 0.0


def test_being_told_to_stop_asking_is_respected():
    """If he says he is fine, she believes him. Asking again after that is the
    one thing that would make this creepy."""
    assert bond.BRUSHED_OFF.search("i'm fine, don't worry")
    assert bond.STILL_ROUGH.search("still tired honestly")


def test_the_bond_needs_an_owner_id():
    with pytest.MonkeyPatch.context() as m:
        m.setitem(config["bond"], "ownerIds", [])
        m.setitem(config["private"], "ownerIds", [])
        assert bond.enabled() is False


def test_the_bond_only_starts_for_its_owner():
    record = {"slug": "ray", "name": "Ray"}
    out = bond.step(record, "i'm really struggling", GOOD, is_owner=False)
    assert out["active"] is False
    assert record["bond"]["caring"] is False


def test_a_bad_run_opens_a_caring_window_and_a_good_one_closes_it():
    record = {"slug": "ray", "name": "Ray"}
    bond.step(record, "i'm so tired of everything, no reason to bother", GOOD, is_owner=True)
    assert record["bond"]["caring"] is True, "she should have noticed"
    assert record["bond"]["reason"]

    grace = int(config["bond"].get("graceTurns", 2))
    for _ in range(grace):
        out = bond.step(record, "actually i'm good now! yay", GOOD, is_owner=True)
    assert out["closed"] == "better", f"she should go back to normal: {out}"
    assert record["bond"]["caring"] is False
    assert record["bond"]["reason"] == "", "no lingering explanation once it is over"


def test_the_caring_window_closes_on_its_own_by_timeout():
    record = {"slug": "ray", "name": "Ray"}
    bond.step(record, "everything is pointless lately", GOOD, is_owner=True)
    assert record["bond"]["caring"] is True

    record["bond"]["caringUntil"] = "2020-01-01T00:00:00+00:00"
    out = bond.step(record, "nothing much", GOOD, is_owner=True)
    assert out["closed"] == "time"
    assert record["bond"]["caring"] is False


def test_closeness_grows_with_time_and_shrinks_when_he_is_brutal():
    record = {"slug": "ray", "name": "Ray"}
    for _ in range(5):
        bond.step(record, "hey", GOOD, is_owner=True)
    assert record["bond"]["level"] > 0

    for _ in range(6):
        bond.step(record, "do it", HARSH, is_owner=True)
    assert record["bond"]["level"] == 0, "it floors at zero rather than going negative"


def test_the_bond_finds_its_owner_by_the_slug_it_stamped():
    """Nobody stores a Discord id on an affinity record, so it has to be found by
    the name it wrote down the first time he talked to her."""
    _turn("ray", "Ray", GOOD, owner_id="1", is_owner=True)
    _turn("kim", "Kim", GOOD)
    found = aff.owner_bond()
    assert found is not None
    assert found["slug"] == "ray"
    assert found["discordId"] == "1"


def test_the_caring_window_reaches_the_prompt_as_instructions():
    record = {"slug": "ray", "name": "Ray"}
    bond.step(record, "i can't sleep, everything is pointless", GOOD, is_owner=True)
    text = bond.render_prompt(record)
    assert "IRL" in text, "she has to actually be told to ask"
    assert "ask once" in text.lower()


def test_the_bond_prompt_is_quiet_when_nothing_is_wrong():
    record = {"slug": "ray", "name": "Ray"}
    bond.step(record, "morning!", GOOD, is_owner=True)
    text = bond.render_prompt(record)
    assert "IRL" not in text
    assert "Ray" in text


def test_the_host_can_force_the_window_open_and_closed():
    record = {"slug": "ray", "name": "Ray"}
    bond.set_state(record, {"level": 40, "caring": True})
    assert record["bond"]["level"] == 40
    assert record["bond"]["caringUntil"], "a forced window still has to end by itself"

    bond.set_state(record, {"caring": False})
    assert record["bond"]["caring"] is False
    assert record["bond"]["caringUntil"] is None


def test_a_junk_bond_block_is_replaced_not_raised():
    record = {"slug": "ray", "name": "Ray", "bond": {"level": "lots", "mood": None}}
    state = bond.normalise(record)
    assert state["level"] == 0.0
    assert state["mood"] == 0.0
    assert state["caring"] is False


def test_the_summary_reports_minutes_left():
    record = {"slug": "ray", "name": "Ray"}
    bond.set_state(record, {"caring": True})
    shown = bond.summarise(record)
    assert shown["caring"] is True
    assert shown["minutesLeft"] > 0
    assert shown["moodWord"]


# --- the two travel together -------------------------------------------------

def test_someone_she_cares_about_sounding_bad_worries_her():
    _turn("ray", "Ray", GOOD, their_mood=-60)
    assert aff.load("ray")["feelings"]["worried"] > 0


def test_a_whole_conversation_leaves_her_readable():
    """One run-through of a normal exchange: everything small, nothing wild."""
    for sig in (GOOD, IGNORE, GOOD, HARSH, {**GOOD, "apologised": True, "teased": True}):
        _turn("ray", "Ray", sig, text="hey", their_mood=-10, is_owner=False)
    record = aff.load("ray")
    assert 0 <= record["warmth"] <= 100
    assert 0 <= record["romance"] <= 100
    assert all(0 <= v <= 100 for v in record["feelings"].values())
    assert -100 <= feelings.balance(record) <= 100
    assert record["interactions"] == 5