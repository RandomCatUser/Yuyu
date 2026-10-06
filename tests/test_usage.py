
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from yuyu import providers as llm
from yuyu import usage
from yuyu.config import USAGE_DIR, config


@pytest.fixture(autouse=True)
def usage_dir(tmp_path, monkeypatch):
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


# the provider hooks

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
    import yuyu.config as config_mod

    assert usage.USAGE_DIR != config_mod.USAGE_DIR, (
        "USAGE_DIR is still the real folder - usage history is being written to "
        "her live dashboard data instead of a temp path"
    )