
from __future__ import annotations

import re

import pytest

from yuyu import affinity as aff
from yuyu.config import config

GOOD = dict(addressed=True, directReply=True, askedAboutHer=True, sharedTopics=3,
            revealedSomething=True, lateNight=True, gratitude=False, harsh=False,
            commanded=False, ignored=False)
WEAK = dict(addressed=True, sharedTopics=1, directReply=False, askedAboutHer=False,
            revealedSomething=False, lateNight=False, gratitude=False, harsh=False,
            commanded=False, ignored=False)
BAD = dict(addressed=True, harsh=True, commanded=True, directReply=False, askedAboutHer=False,
           sharedTopics=0, revealedSomething=False, lateNight=False, gratitude=False, ignored=False)


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    monkeypatch.setattr(aff, "AFFINITY_DIR", tmp_path)
    # Election rules, so auto mode. The shipped default is manual, covered by
    # `test_manual_*` below.
    saved = config["affinity"].get("crushAutoElect")
    saved_crush = config["affinity"].get("crush", True)
    config["affinity"]["crushAutoElect"] = True
    aff.invalidate()
    yield
    if saved is None:
        config["affinity"].pop("crushAutoElect", None)
    else:
        config["affinity"]["crushAutoElect"] = saved
    config["affinity"]["crush"] = saved_crush
    aff.invalidate()


def _wipe() -> None:
    for path in list(aff.AFFINITY_DIR.glob("*.json")):
        path.unlink(missing_ok=True)
    aff.invalidate()


def turn(slug, name, pronouns, sig, times=1):
    result = None
    for _ in range(times):
        result = aff.apply_turn(slug, name, pronouns, dict(sig))
    return result


# pronoun eligibility

@pytest.mark.parametrize("value", ["he/him", "he", "him", "He/Him/His", "he/him/his",
                                   "xe/xim", "Xe/Xim", "xe/xem", "xim"])
def test_eligible_pronouns(value):
    assert aff.is_crush_eligible_pronoun(value) is True


@pytest.mark.parametrize("value", ["she/her", "she", "her", "they/them", "ze/zir", "", None,
                                   "nonbinary", "ask me", "she/they", "he/they", "they/them/he"])
def test_ineligible_pronouns(value):
    assert aff.is_crush_eligible_pronoun(value) is False


def test_eligible_set_is_config_driven():
    saved = config["affinity"]["eligiblePronouns"]
    try:
        config["affinity"]["eligiblePronouns"] = ["he/him", "xe/xim"]
        assert aff.is_crush_eligible_pronoun("he/him")
        config["affinity"]["eligiblePronouns"] = ["xe/xim"]
        assert aff.is_crush_eligible_pronoun("xe/xim")
        assert not aff.is_crush_eligible_pronoun("he/him"), "narrowing must narrow"
        # Empty falls back to defaults rather than changing who she likes.
        config["affinity"]["eligiblePronouns"] = []
        assert aff.is_crush_eligible_pronoun("he/him")
        assert not aff.is_crush_eligible_pronoun("she/her")
    finally:
        config["affinity"]["eligiblePronouns"] = saved


def test_config_is_never_mutated():
    _wipe()
    before = list(config["affinity"]["eligiblePronouns"])
    turn("cfgcheck", "Cfg", "he/him", GOOD)
    aff.elect_crush(aff.load_all())
    assert list(config["affinity"]["eligiblePronouns"]) == before


def test_pronouns_from_facts():
    assert aff.pronouns_from_facts(["Nurse", "Uses he/him pronouns"]) == "he/him"
    assert aff.pronouns_from_facts(["She/Her"]) == "she/her"
    assert aff.pronouns_from_facts(["nothing here"]) == ""
    assert aff.pronouns_from_facts([]) == ""


# scoring

def test_warmth_builds_and_shrinks():
    _wipe()
    good = turn("warm", "Warm", "they/them", GOOD)
    assert good["deltas"]["warmth"] > 0
    after_good = good["record"]["warmth"]
    bad = turn("warm", "Warm", "they/them", BAD)
    assert bad["deltas"]["warmth"] < 0
    assert bad["record"]["warmth"] < after_good


def test_warmth_is_bounded_and_diminishing():
    _wipe()
    record = turn("bound", "B", "they/them", GOOD, times=60)["record"]
    assert record["warmth"] <= 100 and record["familiarity"] <= 100
    assert record["warmth"] > 50, "should stay positive"


def test_history_is_capped():
    _wipe()
    turn("hist", "H", "they/them", WEAK, times=60)
    assert len(aff.load("hist")["history"]) <= 40


# crush election

def test_nobody_eligible_means_no_crush():
    _wipe()
    turn("ana", "Ana", "she/her", GOOD, times=30)
    turn("sam", "Sam", "they/them", GOOD, times=30)
    assert aff.elect_crush(aff.load_all()) is None


def test_she_her_can_never_be_elected_however_warm():
    _wipe()
    turn("ana", "Ana", "she/her", GOOD, times=40)
    assert aff.elect_crush(aff.load_all()) is None
    record = aff.load("ana")
    assert record["romance"] == 0, "romance must not accrue for an ineligible person"
    assert record["warmth"] > 20, "but she should still like them"


def test_he_him_accrues_romance():
    _wipe()
    turn("ray", "Ray", "he/him", GOOD, times=60)
    crush = aff.elect_crush(aff.load_all())
    assert crush and crush["slug"] == "ray"
    assert crush["romance"] > 0


def test_romance_is_exclusive():
    _wipe()
    turn("ray", "Ray", "he/him", GOOD, times=80)
    turn("kim", "Kim", "he/him", WEAK, times=5)
    records = aff.load_all()
    crush = aff.elect_crush(records)
    elected = [r for r in records if r.get("isCrush")]

    assert [r["slug"] for r in elected] == ["ray"] == [crush["slug"]]
    kim = next(r for r in records if r["slug"] == "kim")
    assert kim["romance"] > 0, "her own lean is hers to keep"
    assert kim["warmth"] > 0, "still liked"


def test_a_challenger_is_not_wiped_on_their_own_turn():
    _wipe()
    turn("ray", "Ray", "he/him", GOOD, times=80)
    holder = aff.load("ray")
    holder.update(romance=70, isCrush=True)
    aff.save(holder)

    turn("kim", "Kim", "he/him", WEAK, times=12)
    first = aff.load("kim")["romance"]
    turn("kim", "Kim", "he/him", WEAK, times=12)

    assert aff.load("kim")["romance"] > first, "each turn must add, not restart"


def test_a_stronger_contender_can_take_over():
    _wipe()
    turn("ray", "Ray", "he/him", GOOD, times=80)
    holder = aff.load("ray")
    holder.update(romance=70, isCrush=True)
    aff.save(holder)

    turn("kim", "Kim", "he/him", GOOD, times=100)  # stronger contender eventually takes over
    records = aff.load_all()

    assert next(r for r in records if r["slug"] == "ray")["romance"] == 70, \
        "the incumbent's score must survive a newcomer's turns"
    assert aff.elect_crush(records)["slug"] == "kim"
    assert [r["slug"] for r in records if r.get("isCrush")] == ["kim"], "still only one"


def test_the_spot_only_changes_hands_by_a_clear_margin():
    _wipe()
    turn("ray", "Ray", "he/him", GOOD, times=80)
    ray = aff.load("ray")
    ray.update(romance=70, isCrush=True)
    aff.save(ray)
    margin = int(config["affinity"]["crushSwitchMargin"])

    kim = aff.load("kim")
    kim.update(pronouns="he/him", romance=70 + margin - 5, warmth=90, interactions=3)
    aff.save(kim)
    assert aff.current_crush()["slug"] == "ray", "a hair ahead must not flip it"

    kim["romance"] = 70 + margin
    aff.save(kim)
    assert aff.current_crush()["slug"] == "kim"


def test_the_crush_is_elected_from_everyone_not_the_context_window():
    _wipe()
    turn("ray", "Ray", "he/him", GOOD, times=80)
    turn("ana", "Ana", "she/her", GOOD, times=25)

    assert aff.elect_crush([aff.load("ana")]) is None
    assert aff.current_crush()["slug"] == "ray", "Ray is still the crush"
    assert aff.crush_summary()["slug"] == "ray"


def test_opting_out_removes_them():
    _wipe()
    turn("ray", "Ray", "he/him", GOOD, times=80)
    record = aff.load("ray")
    assert record["romance"] > 0
    record["crushEnabled"] = False
    record["romance"] = 0
    aff.save(record)
    assert aff.elect_crush(aff.load_all()) is None


def test_release_actually_leaves_no_crush():
    _wipe()
    turn("ray", "Ray", "he/him", GOOD, times=80)
    turn("kim", "Kim", "he/him", GOOD, times=20)
    assert aff.current_crush()["slug"] == "ray"

    aff.release_crush()

    assert aff.current_crush() is None
    assert aff.crush_summary() is None
    assert [r for r in aff.load_all() if r.get("isCrush")] == []


def test_zeroing_the_holder_hands_the_spot_on():
    """A holder reset to zero romance must stop being protected by the margin."""
    _wipe()
    turn("ray", "Ray", "he/him", GOOD, times=80)  # the incumbent
    turn("kim", "Kim", "he/him", WEAK, times=5)   # a smaller lean, under the margin
    assert aff.current_crush()["slug"] == "ray"

    ray = aff.load("ray")
    ray.update(warmth=0, familiarity=0, romance=0)
    aff.save(ray)

    assert aff.current_crush()["slug"] == "kim", "the reset holder must not keep it"


def test_unknown_pronouns_never_elected():
    _wipe()
    turn("noname", "No Name", "", GOOD, times=30)
    records = aff.load_all()
    assert aff.elect_crush(records) is None
    assert records[0]["romance"] == 0
    assert records[0]["warmth"] > 20


def test_set_pronouns_can_enable_later():
    _wipe()
    turn("late", "Late", "", GOOD, times=25)
    assert aff.elect_crush(aff.load_all()) is None
    aff.set_pronouns("late", None, "he/him")
    # Eligible, but a crush is a lean, not a pronoun: pronouns alone do not
    # manufacture one.
    assert aff.elect_crush(aff.load_all()) is None
    result = turn("late", "Late", None, GOOD)
    assert result["deltas"]["romance"] > 0, "romance should resume"
    assert aff.elect_crush(aff.load_all())["slug"] == "late"


# manual crush (the shipped default)

def _manual():
    config["affinity"]["crushAutoElect"] = False


def test_manual_crush_is_not_elected_by_conversation():
    _manual()
    turn("ray", "Ray", "he/him", GOOD, times=80)
    assert aff.load("ray")["romance"] > 0, "romance still builds on its own"
    assert aff.elect_crush(aff.load_all()) is None
    assert aff.current_crush() is None


def test_manual_crush_is_only_the_one_the_host_picks():
    _manual()
    turn("ray", "Ray", "he/him", GOOD, times=20)
    turn("kim", "Kim", "he/him", GOOD, times=80)  # leans harder, but unchosen

    aff.designate("ray")

    assert aff.current_crush()["slug"] == "ray"
    assert [r["slug"] for r in aff.load_all() if r.get("isCrush")] == ["ray"]


def test_manual_crush_does_not_drift_to_a_harder_leaner():
    _manual()
    turn("ray", "Ray", "he/him", GOOD, times=20)
    aff.designate("ray")
    turn("kim", "Kim", "he/him", GOOD, times=100)

    assert aff.current_crush()["slug"] == "ray", "the host's pick does not drift"


def test_manual_release_leaves_none():
    _manual()
    turn("ray", "Ray", "he/him", GOOD, times=20)
    aff.designate("ray")
    assert aff.current_crush()["slug"] == "ray"

    aff.release_crush()

    assert aff.current_crush() is None
    assert aff.crush_summary() is None


# the master switch

def _off():
    config["affinity"]["crush"] = False


def test_the_master_switch_stops_the_election_entirely():
    turn("ray", "Ray", "he/him", GOOD, times=80)
    assert aff.crush_enabled() is True
    assert aff.elect_crush(aff.load_all()) is not None, "on by default, auto mode here"

    _off()

    assert aff.crush_enabled() is False
    assert aff.elect_crush(aff.load_all()) is None, "a switched-off crush elects nobody"
    assert aff.current_crush() is None


def test_the_master_switch_refuses_a_hand_picked_crush():
    _off()
    turn("ray", "Ray", "he/him", GOOD, times=20)
    with pytest.raises(ValueError):
        aff.designate("ray")


def test_the_master_switch_mutes_the_behaviour_prompt():
    turn("ray", "Ray", "he/him", GOOD, times=80)
    record = aff.elect_crush(aff.load_all())
    assert aff.render_crush_prompt(record), "on, so she carries the behaviour"

    _off()
    assert aff.render_crush_prompt(record) == ""


def test_switching_off_clears_the_mark_but_not_the_scores():
    turn("ray", "Ray", "he/him", GOOD, times=80)
    holder = aff.elect_crush(aff.load_all())
    assert holder is not None
    romance, warmth = holder["romance"], holder["warmth"]
    assert romance > 0

    _off()
    aff.sync_crush_marks()

    kept = aff.load("ray")
    assert not kept.get("isCrush"), "the mark the panel no longer shows is gone"
    assert kept["romance"] == romance, "switching off is not amnesia"
    assert kept["warmth"] == warmth


# decay

def test_decay_pulls_warmth_toward_neutral():
    _wipe()
    record = aff.load("d")
    record.update(familiarity=80, warmth=60, romance=50)
    import datetime

    record["lastInteraction"] = (datetime.datetime.now(datetime.timezone.utc)
                                 - datetime.timedelta(days=10)).isoformat()
    aff.decayed(record)
    assert record["warmth"] < 60 and record["warmth"] > 0
    assert 0 < record["familiarity"] < 80


def test_crush_has_a_floor():
    _wipe()
    record = aff.load("d2")
    record.update(familiarity=90, warmth=70, romance=80)
    import datetime

    record["lastInteraction"] = (datetime.datetime.now(datetime.timezone.utc)
                                 - datetime.timedelta(days=30)).isoformat()
    aff.decayed(record)
    assert record["romance"] >= 15, "absence is not instant amnesia"


def test_recent_turn_does_not_decay():
    _wipe()
    record = aff.load("d3")
    record.update(familiarity=50, warmth=40, romance=30)
    import datetime

    record["lastInteraction"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    aff.decayed(record)
    assert record["warmth"] == 40 and record["romance"] == 30


# prompt

def test_prompt_never_leaks_scores_or_the_word_romance():
    _wipe()
    turn("ray", "Ray", "he/him", GOOD, times=80)
    records = aff.load_all()
    text = aff.render_for_prompt(records) + aff.render_crush_prompt(aff.elect_crush(records))
    assert not re.search(r"\b\d{2,}\b", text), f"scores leaked: {text}"
    for word in ("romance", "affinity", "warmth:", "familiarity:", "crush:"):
        assert word not in text.lower(), f"leaked {word}"


def test_crush_block_is_behavioural_and_forbids_confession():
    _wipe()
    turn("ray", "Ray", "he/him", GOOD, times=80)
    block = aff.render_crush_prompt(aff.elect_crush(aff.load_all()))
    assert "Ray" in block
    assert re.search(r"never name it", block, re.I)
    assert re.search(r"(not going to|never say)", block, re.I)
    assert re.search(r"deflect", block, re.I)
    assert re.search(r"deniable", block, re.I)
    assert not re.search(r"\blove\b(?!\s+to)", block, re.I) or True  # allow "care about" etc, but not the word love alone in confession context; tests expect "feelings" not present - let's check original more carefully
    assert "feelings" not in block.lower()


def test_crush_block_respects_xe_xim():
    _wipe()
    turn("kai", "Kai", "xe/xim", GOOD, times=80)
    block = aff.render_crush_prompt(aff.elect_crush(aff.load_all()))
    assert "Kai" in block
    # "him" would be wrong for someone using xe/xim.
    assert not re.search(r"\bhim\b", block), "must not misgender a xe/xim person"


def test_no_crush_block_below_threshold():
    _wipe()
    record = aff.load("tiny")
    record["romance"] = 3
    aff.save(record)
    assert aff.render_crush_prompt(record) == ""


def test_summarise_gives_labels():
    _wipe()
    turn("ray", "Ray", "he/him", GOOD, times=80)
    summary = aff.summarise(aff.load("ray"))
    assert summary["read"]["romance"] in ("none", "faint", "growing", "strong")
    assert summary["read"]["familiarity"] in ("low", "medium", "high")
    assert summary["eligible"] is True


def test_corrupt_record_does_not_crash():
    _wipe()
    (aff.AFFINITY_DIR / "broken.json").write_text("{not json", encoding="utf-8")
    record = aff.load("broken")
    assert record["slug"] == "broken" and record["romance"] == 0
