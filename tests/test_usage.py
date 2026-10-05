"""Token accounting: what gets recorded, what it costs, and how it aggregates.

The number on the panel is only worth reading if it is the number the provider
billed, so most of these check the parsing rather than the arithmetic - a provider
that renames a field should show up here, not as a quietly wrong total.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from yuyu import providers as llm
from yuyu import usage
from yuyu.config import USAGE_DIR, config


@pytest.fixture(autouse=True)
def usage_dir(tmp_path, monkeypatch):
    """Point the day files at a temp folder so no test writes real history."""
    monkeypatch.setattr(usage, "USAGE_DIR", tmp_path / "usage")
    monkeypatch.setitem(config["usage"], "enabled", True)
    monkeypatch.setitem(config["usage"], "prices", {})
    return usage.USAGE_DIR


def _rows():
    """Every stored row, skipping a torn line the way the reader does."""
    rows = []
    for path in sorted(usage.USAGE_DIR.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


# --- parsing what a provider sent --------------------------------------------

def test_the_openai_shape_is_read_as_sent():
    counts = usage.normalise({
        "prompt_tokens": 1200, "completion_tokens": 340, "total_tokens": 1540,
    })
    assert counts == {"prompt": 1200, "completion": 340, "total": 1540,
                      "cached": 0, "reasoning": 0}


def test_a_missing_total_is_derived_rather_than_reported_as_zero():
    """Several gateways send only the two halves. Zero would read as free."""
    counts = usage.normalise({"prompt_tokens": 90, "completion_tokens": 12})
    assert counts["total"] == 102


def test_the_anthropic_spelling_is_accepted_too():
    counts = usage.normalise({"input_tokens": 500, "output_tokens": 25})
    assert (counts["prompt"], counts["completion"], counts["total"]) == (500, 25, 525)


def test_nested_details_are_pulled_out_when_present():
    counts = usage.normalise({
        "prompt_tokens": 1000, "completion_tokens": 100,
        "prompt_tokens_details": {"cached_tokens": 250},
        "completion_tokens_details": {"reasoning_tokens": 64},
    })
    assert counts["cached"] == 250
    assert counts["reasoning"] == 64


@pytest.mark.parametrize("junk", [None, {}, "nope", 12, [1, 2], {"prompt_tokens": "many"}])
def test_anything_unrecognisable_reads_as_zero_rather_than_raising(junk):
    """A reply must never be lost to a malformed usage block."""
    counts = usage.normalise(junk)
    assert counts["total"] == 0
    assert counts["prompt"] >= 0


def test_a_negative_count_is_clamped_rather_than_carried():
    assert usage.normalise({"prompt_tokens": -5})["prompt"] == 0


# --- pricing ------------------------------------------------------------------

def test_no_price_configured_means_no_cost_not_a_guess():
    """Better a dash than a confident wrong number."""
    assert usage.cost_of("prov", "model", {"prompt": 1000, "completion": 100}) is None


def test_cost_is_dollars_per_million_tokens(monkeypatch):
    monkeypatch.setitem(config["usage"], "prices", {"prov:model": {"in": 3.0, "out": 15.0}})
    cost = usage.cost_of("prov", "model", {"prompt": 1_000_000, "completion": 1_000_000})
    assert cost == pytest.approx(18.0)


def test_a_model_name_containing_a_colon_still_matches(monkeypatch):
    """OpenRouter ids look like `vendor/model:free` - the first colon splits."""
    monkeypatch.setitem(config["usage"], "prices",
                        {"openrouter:qwen/qwen3.8-27b:free": {"in": 1.0, "out": 2.0}})
    assert usage.cost_of("openrouter", "qwen/qwen3.8-27b:free",
                         {"prompt": 1_000_000, "completion": 0}) == pytest.approx(1.0)


def test_a_bare_model_name_matches_any_provider(monkeypatch):
    monkeypatch.setitem(config["usage"], "prices", {"gemini-3.1-flash-lite": {"in": 1.0, "out": 2.0}})
    assert usage.cost_of("anything", "gemini-3.1-flash-lite",
                         {"prompt": 1_000_000, "completion": 0}) == pytest.approx(1.0)


def test_cached_tokens_are_billed_at_their_own_rate_when_one_is_given(monkeypatch):
    monkeypatch.setitem(config["usage"], "prices",
                        {"m": {"in": 10.0, "out": 0.0, "cached": 1.0}})
    # 900 fresh at 10/M + 100 cached at 1/M
    assert usage.cost_of("p", "m", {"prompt": 1000, "completion": 0, "cached": 100}) \
        == pytest.approx((900 * 10.0 + 100 * 1.0) / 1_000_000)


def test_cached_defaults_to_the_input_rate_when_unpriced_separately(monkeypatch):
    monkeypatch.setitem(config["usage"], "prices", {"m": {"in": 10.0, "out": 0.0}})
    assert usage.cost_of("p", "m", {"prompt": 1000, "completion": 0, "cached": 100}) \
        == pytest.approx(1000 * 10.0 / 1_000_000)


# --- recording ----------------------------------------------------------------

def test_a_recorded_call_lands_in_todays_file():
    usage.record("prov", "model", {"prompt_tokens": 10, "completion_tokens": 5}, source="chat")
    rows = _rows()
    assert len(rows) == 1
    assert rows[0]["total"] == 15
    assert rows[0]["source"] == "chat"
    assert rows[0]["day"] == rows[0]["day"].strip()  # a real calendar day


def test_nothing_is_written_when_counting_is_off():
    config["usage"]["enabled"] = False
    try:
        usage.record("prov", "model", {"prompt_tokens": 10})
    finally:
        config["usage"]["enabled"] = True
    assert _rows() == []


def test_a_write_failure_does_not_raise():
    """Accounting runs after the reply is written; it must never break one."""
    with patch.object(type(usage.USAGE_DIR), "mkdir", side_effect=OSError("disk full")):
        usage.record("prov", "model", {"prompt_tokens": 10})


def test_one_torn_line_does_not_lose_the_whole_day():
    usage.record("prov", "model", {"prompt_tokens": 10, "completion_tokens": 5})
    path = next(usage.USAGE_DIR.glob("*.jsonl"))
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"prompt_tokens": 99, "compl\n')  # a crash mid-append
    assert len(_rows()) == 1
    assert usage.summary(30)["totals"]["calls"] == 1


# --- aggregation --------------------------------------------------------------

def test_the_summary_adds_up_every_call():
    for i in range(5):
        usage.record("prov", "model", {"prompt_tokens": 100, "completion_tokens": 10},
                     source="chat" if i % 2 else "extract")
    s = usage.summary(30)
    assert s["totals"]["calls"] == 5
    assert s["totals"]["prompt"] == 500
    assert s["totals"]["completion"] == 50
    assert s["totals"]["total"] == 550
    assert s["bySource"] and {r["source"] for r in s["bySource"]} == {"chat", "extract"}


def test_providers_are_split_and_ranked_by_tokens():
    usage.record("small", "m", {"prompt_tokens": 10, "completion_tokens": 1})
    usage.record("big", "m", {"prompt_tokens": 9000, "completion_tokens": 900})
    ranked = usage.summary(30)["byProvider"]
    assert [r["provider"] for r in ranked] == ["big", "small"]


def test_an_unpriced_call_makes_the_cost_total_a_floor_not_a_wrong_number(monkeypatch):
    """The panel has to be able to say the money figure is incomplete."""
    monkeypatch.setitem(config["usage"], "prices", {"prov:priced": {"in": 1.0, "out": 1.0}})
    usage.record("prov", "priced", {"prompt_tokens": 1_000_000, "completion_tokens": 0})
    usage.record("prov", "unpriced", {"prompt_tokens": 1_000_000, "completion_tokens": 0})

    s = usage.summary(30)
    assert s["costComplete"] is False
    assert s["unpricedCalls"] == 1
    assert s["totals"]["cost"] == pytest.approx(1.0)      # only the priced call
    assert s["totals"]["unpriced"] == 1


def test_clearing_removes_the_files_and_nothing_else():
    usage.record("prov", "model", {"prompt_tokens": 10})
    keep = usage.USAGE_DIR / "notes.txt"
    keep.write_text("not a day file", encoding="utf-8")

    assert usage.clear() == 1
    assert usage.summary(30)["totals"]["calls"] == 0
    assert keep.exists(), "clear() touched a file that is not history"


def test_the_window_only_counts_recent_days():
    from datetime import datetime, timedelta, timezone

    old = (datetime.now(timezone.utc) - timedelta(days=40)).strftime("%Y-%m-%d")
    recent = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    usage.USAGE_DIR.mkdir(parents=True, exist_ok=True)
    (usage.USAGE_DIR / f"{old}.jsonl").write_text(
        json.dumps({"ts": 0, "day": old, "provider": "p", "model": "m", "source": "chat",
                    "prompt": 5000, "completion_tokens": 500, "total": 5500}) + "\n",
        encoding="utf-8")
    usage.record("p", "m", {"prompt_tokens": 10, "completion_tokens": 1})

    assert usage.summary(7)["totals"]["calls"] == 1
    assert usage.summary(90)["totals"]["calls"] == 2


def test_an_empty_folder_summarises_to_zero_rather_than_raising():
    s = usage.summary(30)
    assert s["totals"]["calls"] == 0
    assert s["byDay"] == [] and s["byProvider"] == []
    assert s["recent"] == []


# --- derived figures ---------------------------------------------------------
#
# The ones that need every row rather than a bucket. They are the easiest place
# to invent a number that looks like a measurement, so each is pinned to what it
# actually divides, and the ones that would be meaningless on thin data are
# required to be absent instead.

def test_tokens_per_call_is_the_window_average():
    usage.record("p", "m", {"prompt_tokens": 100, "completion_tokens": 20}, latency_ms=1000)
    usage.record("p", "m", {"prompt_tokens": 300, "completion_tokens": 40}, latency_ms=1000)

    d = usage.summary(30)["derived"]
    assert d["tokensPerCall"] == 230.0, "460 tokens over 2 calls"


def test_output_rate_divides_completion_by_total_call_time():
    # 60 completion tokens across 2000ms of call time is 30/s. Deliberately the
    # whole call, prompt processing included - the panel says so on the row.
    usage.record("p", "m", {"prompt_tokens": 900, "completion_tokens": 30}, latency_ms=1000)
    usage.record("p", "m", {"prompt_tokens": 100, "completion_tokens": 30}, latency_ms=1000)

    assert usage.summary(30)["derived"]["outPerSecond"] == 30.0


def test_the_busiest_hour_needs_more_than_one_hour_to_mean_anything():
    """A single call is not a pattern.

    Reported anyway it would claim she is a 14:00 person on the strength of one
    call, so one distinct hour returns nothing at all.
    """
    usage.record("p", "m", {"prompt_tokens": 10, "completion_tokens": 1})
    d = usage.summary(30)["derived"]
    assert d["busiestHour"] is None
    assert d["hoursActive"] == 1

    usage.record("p", "m", {"prompt_tokens": 10, "completion_tokens": 1})
    d = usage.summary(30)["derived"]
    if d["hoursActive"] > 1:
        assert d["busiestHour"]["calls"] >= 1
        assert 0 <= d["busiestHour"]["hour"] <= 23
    else:
        assert d["busiestHour"] is None


def test_first_and_last_call_are_real_timestamps_not_guessed():
    usage.record("p", "m", {"prompt_tokens": 10})
    first = usage.summary(30)["derived"]["firstTs"]
    usage.record("p", "m", {"prompt_tokens": 10})
    d = usage.summary(30)["derived"]

    assert first is not None and d["lastTs"] is not None
    assert first <= d["lastTs"]
    assert _rows()[0]["ts"] <= first <= d["lastTs"] <= _rows()[-1]["ts"]


def test_priced_calls_distinguishes_unknown_from_free():
    """The number that decides whether the panel may print a money figure.

    Summing priced calls gives 0.0 when nothing is priced, and $0 reads as "these
    were free" - a claim about money that was never made. So the panel needs to
    be able to tell the two apart, and this is that count.
    """
    usage.record("p", "priced", {"prompt_tokens": 10, "completion_tokens": 1})
    usage.record("p", "free", {"prompt_tokens": 10, "completion_tokens": 1})

    assert usage.summary(30)["derived"]["pricedCalls"] == 0
    assert usage.summary(30)["totals"]["cost"] == 0.0

    with patch.dict(config["usage"]["prices"],
                    {"priced": {"in": 1.0, "out": 1.0}}, clear=False):
        usage.record("p", "priced", {"prompt_tokens": 10, "completion_tokens": 1})
        s = usage.summary(30)
        assert s["derived"]["pricedCalls"] == 1
        assert s["totals"]["cost"] > 0


def test_derived_figures_are_absent_rather_than_zero_on_an_empty_window():
    d = usage.summary(30)["derived"]
    assert d["tokensPerCall"] == 0 and d["outPerSecond"] == 0.0
    assert d["firstTs"] is None and d["lastTs"] is None
    assert d["busiestHour"] is None and d["peakDay"] is None
    assert d["pricedCalls"] == 0


def test_the_peak_day_is_the_busiest_one_by_tokens():
    usage.record("p", "m", {"prompt_tokens": 10, "completion_tokens": 1})
    usage.record("p", "m", {"prompt_tokens": 9000, "completion_tokens": 1})

    peak = usage.summary(30)["derived"]["peakDay"]
    assert peak is not None
    assert peak["total"] == max(d["total"] for d in usage.summary(30)["byDay"])


# --- the provider hooks -------------------------------------------------------

PROVIDER = {"id": "prov", "name": "Prov", "baseUrl": "https://p.example/v1",
            "model": "m1", "apiKeyEnv": "USAGE_TEST_KEY", "enabled": True}


class _Resp:
    def __init__(self, status=200, body=None, lines=None):
        self.status_code = status
        self._body = body if body is not None else {"choices": [{"message": {"content": "hi"}}]}
        self._lines = lines or []

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body

    def iter_lines(self, chunk_size=None):
        return iter(self._lines)


@pytest.fixture
def one_provider(monkeypatch):
    monkeypatch.setitem(config["model"], "providers", [PROVIDER])
    monkeypatch.setitem(config["model"], "default", "prov")
    monkeypatch.setenv("USAGE_TEST_KEY", "secret")
    monkeypatch.setattr(llm, "_quota_backoff_until", {})
    monkeypatch.setattr(llm, "_key_backoff_until", {})
    monkeypatch.setattr(llm, "_no_stream_usage", set())


def test_a_non_streaming_call_records_what_the_provider_reported(one_provider):
    body = {"choices": [{"message": {"content": "hi"}}],
            "usage": {"prompt_tokens": 1200, "completion_tokens": 340, "total_tokens": 1540}}
    with patch.object(llm.requests, "post", return_value=_Resp(body=body)):
        llm._complete_sync([{"role": "user", "content": "x"}], False, 50, None, "chat")

    row = _rows()[0]
    assert (row["prompt"], row["completion"], row["total"]) == (1200, 340, 1540)
    assert row["provider"] == "prov" and row["model"] == "m1"
    assert row["source"] == "chat" and row["streamed"] is False


def test_a_provider_that_sends_no_usage_still_records_the_call(one_provider):
    """A zero row is better than a gap: the call happened and cost tokens."""
    with patch.object(llm.requests, "post", return_value=_Resp()):
        llm._complete_sync([{"role": "user", "content": "x"}], False, 50, None, "chat")
    assert _rows()[0]["total"] == 0


def test_a_streamed_call_records_the_usage_chunk(one_provider):
    lines = [
        b'data: {"choices": [{"delta": {"content": "he"}}]}',
        b'data: {"choices": [{"delta": {"content": "llo"}}]}',
        b'data: {"choices": [], "usage": {"prompt_tokens": 90, "completion_tokens": 12,'
        b' "total_tokens": 102}}',
        b"data: [DONE]",
    ]
    with patch.object(llm.requests, "post", return_value=_Resp(lines=lines)):
        assert llm._stream_sync([], None, 50, None, "chat") == "hello"

    row = _rows()[0]
    assert (row["prompt"], row["completion"]) == (90, 12)
    assert row["streamed"] is True


def test_streams_ask_for_their_usage_by_default(one_provider):
    sent = {}

    def post(url, headers=None, json=None, timeout=None, stream=False):
        sent.update(json or {})
        return _Resp(lines=[b'data: {"choices": [{"delta": {"content": "x"}}]}'])

    with patch.object(llm.requests, "post", side_effect=post):
        llm._stream_sync([], None, 50, None)
    assert sent["stream_options"] == {"include_usage": True}


def test_a_gateway_that_rejects_stream_options_is_retried_without_it(one_provider):
    """Some OpenAI-compatible gateways do not know the field. The reply still lands."""
    bodies = []

    def post(url, headers=None, json=None, timeout=None, stream=False):
        bodies.append(json)
        if json.get("stream_options"):
            return _Resp(status=400, body={"error": "unknown field"})
        return _Resp(lines=[
            b'data: {"choices": [{"delta": {"content": "hey"}}]}',
            b'data: {"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 2}}',
        ])

    with patch.object(llm.requests, "post", side_effect=post):
        assert llm._stream_sync([], None, 50, None) == "hey"

    assert len(bodies) == 2, "the identical call was not retried without the field"
    assert "stream_options" in bodies[0] and "stream_options" not in bodies[1]
    assert _rows()[0]["total"] == 7


def test_the_rejection_is_remembered_so_the_retry_happens_once(one_provider, monkeypatch):
    remembered = set()
    monkeypatch.setattr(llm, "_no_stream_usage", remembered)

    def post(url, headers=None, json=None, timeout=None, stream=False):
        if json.get("stream_options"):
            return _Resp(status=400, body={"error": "unknown field"})
        return _Resp(lines=[b'data: {"choices": [{"delta": {"content": "x"}}]}'])

    with patch.object(llm.requests, "post", side_effect=post) as spy:
        llm._stream_sync([], None, 50, None)
        llm._stream_sync([], None, 50, None)

    assert spy.call_count == 3, "the second call should not have tried the field again"
    assert remembered == {"prov"}


def test_turning_stream_usage_off_never_sends_the_field(one_provider):
    config["usage"]["streamUsage"] = False
    try:
        bodies = []

        def post(url, headers=None, json=None, timeout=None, stream=False):
            bodies.append(json)
            return _Resp(lines=[b'data: {"choices": [{"delta": {"content": "x"}}]}'])

        with patch.object(llm.requests, "post", side_effect=post):
            llm._stream_sync([], None, 50, None)
    finally:
        config["usage"]["streamUsage"] = True
    assert "stream_options" not in bodies[0]


def test_a_failed_call_records_nothing(one_provider):
    with patch.object(llm.requests, "post", return_value=_Resp(status=500, body={})):
        with pytest.raises(llm.ProviderError):
            llm._complete_sync([], False, 50, None)
    assert _rows() == [], "a call that produced nothing was still billed"


# --- the suite must not write into her real history --------------------------

def test_running_the_provider_helpers_does_not_touch_the_real_folder(one_provider):
    """A passing test run must leave usage/*.jsonl exactly as it found it.

    The provider helpers record a line per call now, so any test that calls them
    writes history. If that lands in the real folder the dashboard carries the
    test's zeros and unpriced rows forever - it would read as real spend and pin
    the cost total to a floor. conftest.py redirects USAGE_DIR for the session;
    this is the check that the redirect is actually in force.
    """
    import yuyu.config as config_mod

    real = config_mod.USAGE_DIR
    before = {p.name: p.stat().st_mtime_ns for p in real.glob("*.jsonl")} if real.exists() else {}

    with patch.object(llm.requests, "post", side_effect=[
        _Resp(body={"choices": [{"message": {"content": "hi"}}],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 1}}),
        _Resp(lines=[b'data: {"choices": [{"delta": {"content": "hi"}}]}',
                     b'data: {"choices": [], "usage": {"prompt_tokens": 4, "completion_tokens": 1}}']),
    ]):
        llm._complete_sync([], False, 50, None, "chat")
        llm._stream_sync([], None, 50, None, "chat")

    after = {p.name: p.stat().st_mtime_ns for p in real.glob("*.jsonl")} if real.exists() else {}
    assert after == before, f"the test run wrote real usage history: {sorted(set(after) - set(before))}"


def test_the_session_fixture_points_recording_somewhere_other_than_disk(one_provider):
    """Guards the redirect itself, so a broken fixture fails loudly here."""
    import yuyu.config as config_mod

    assert usage.USAGE_DIR != config_mod.USAGE_DIR, (
        "USAGE_DIR is still the real folder - usage history is being written to "
        "her live dashboard data instead of a temp path"
    )