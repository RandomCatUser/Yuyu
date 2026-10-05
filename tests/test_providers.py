"""Offline tests for provider selection, failover, and response parsing."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from yuyu import providers as llm
from yuyu.config import DEFAULTS, config


PROVIDERS = [
    {
        "id": "first",
        "name": "First",
        "baseUrl": "https://first.example/v1",
        "model": "first-model",
        "apiKeyEnv": "FIRST_TEST_KEY",
        "enabled": True,
    },
    {
        "id": "second",
        "name": "Second",
        "baseUrl": "https://second.example/v1",
        "model": "second-model",
        "apiKeyEnv": "SECOND_TEST_KEY",
        "enabled": True,
    },
]


@pytest.fixture
def provider_config(monkeypatch):
    monkeypatch.setitem(config["model"], "providers", PROVIDERS)
    monkeypatch.setitem(config["model"], "default", "first")
    monkeypatch.setenv("FIRST_TEST_KEY", "first-secret")
    monkeypatch.setenv("SECOND_TEST_KEY", "second-secret")
    monkeypatch.setattr(llm, "_quota_backoff_until", {})
    # Both cooldown tables, not one. A rejected key is remembered separately now,
    # so leaving it out would let one test's 401 silently disable a provider for
    # every test after it - which looks like a failover bug and is not one.
    monkeypatch.setattr(llm, "_key_backoff_until", {})


class _Response:
    def __init__(self, status=200, body=None, lines=None):
        self.status_code = status
        self._body = body or {"choices": [{"message": {"content": "hello"}}]}
        self._lines = lines or []

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body

    def iter_lines(self, chunk_size=None):
        return iter(self._lines)


def test_provider_urls_support_standard_and_gemini_openai_endpoints():
    assert llm._provider_url(PROVIDERS[0]) == "https://first.example/v1/chat/completions"
    assert llm._provider_url({
        **PROVIDERS[0],
        "baseUrl": "https://generativelanguage.googleapis.com/v1beta/openai",
    }) == "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
    assert llm._provider_url({
        **PROVIDERS[0],
        "baseUrl": "https://custom.example/v1/chat/completions",
    }) == "https://custom.example/v1/chat/completions"


def _provider_by_id(providers, provider_id):
    return next(p for p in providers if p["id"] == provider_id)


def test_primary_provider_uses_a_compatible_endpoint_and_key_env():
    """The contract is the endpoint, the key env and being primary - not which
    provider or model it currently points at, since that is a config.json choice
    and the dashboard edits it."""
    default_id = config["model"]["default"]
    provider = _provider_by_id(config["model"]["providers"], default_id)
    assert llm._provider_url(provider).endswith("/chat/completions")
    assert provider["model"], "it needs a model id or every request fails"
    assert provider["apiKeyEnv"]


def test_builtin_default_provider_is_gemini():
    assert DEFAULTS["model"]["default"] == "gemini"
    provider = _provider_by_id(DEFAULTS["model"]["providers"], "gemini")
    assert llm._provider_url(provider) == (
        "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
    )
    assert provider["model"] == "gemini-3.1-flash-lite"
    assert provider["apiKeyEnv"] == "GEMINI_API_KEY"


def test_primary_provider_is_tried_first_then_fallback(provider_config):
    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs["json"]["model"]))
        if len(calls) == 1:
            return _Response(429, {"error": {"message": "limit"}})
        return _Response(body={"choices": [{"message": {"content": "fallback answer"}}]})

    with patch.object(llm.requests, "post", side_effect=post):
        assert llm._complete_sync([{"role": "user", "content": "hi"}], False, 10, 0) == "fallback answer"
    assert calls == [
        ("https://first.example/v1/chat/completions", "first-model"),
        ("https://second.example/v1/chat/completions", "second-model"),
    ]


def test_quota_error_skips_provider_keys_and_switches_to_next(provider_config):
    calls = []

    def post(url, **kwargs):
        calls.append(kwargs["json"]["model"])
        if len(calls) == 1:
            return _Response(402)
        return _Response(body={"choices": [{"message": {"content": "fallback answer"}}]})

    with patch.object(llm.requests, "post", side_effect=post):
        result = llm._complete_sync([{"role": "user", "content": "hi"}], False, 10, 0)
        assert result == "fallback answer"

        # The quota-exhausted provider is temporarily skipped on the next request.
        assert llm._complete_sync([{"role": "user", "content": "again"}], False, 10, 0) == "fallback answer"

    assert calls == ["first-model", "second-model", "second-model"]


def test_incompatible_primary_model_falls_back_to_next_provider(provider_config):
    calls = []

    def post(url, **kwargs):
        calls.append(kwargs["json"]["model"])
        if len(calls) == 1:
            return _Response(400, {"error": {"message": "model is not compatible"}})
        return _Response(body={"choices": [{"message": {"content": "fallback answer"}}]})

    with patch.object(llm.requests, "post", side_effect=post):
        result = llm._complete_sync([{"role": "user", "content": "hi"}], False, 10, 0)

    assert result == "fallback answer"
    assert calls == ["first-model", "second-model"]


def test_invalid_key_also_fails_over(provider_config):
    calls = []

    def post(url, **kwargs):
        calls.append(url)
        return _Response(403) if len(calls) == 1 else _Response()

    with patch.object(llm.requests, "post", side_effect=post):
        assert llm._complete_sync([], False, 10, None) == "hello"
    assert len(calls) == 2


def test_numbered_keys_are_tried_before_falling_back(provider_config, monkeypatch):
    provider = {**PROVIDERS[0], "apiKeyEnvPrefix": "FIRST_TEST_KEY_"}
    monkeypatch.setenv("FIRST_TEST_KEY_3", "third-secret")
    monkeypatch.setenv("FIRST_TEST_KEY_2", "second-secret")
    calls = []

    def post(url, **kwargs):
        calls.append(kwargs["headers"]["Authorization"])
        if len(calls) < 3:
            return _Response(429)
        return _Response()

    with patch.object(llm, "_ordered_providers", return_value=[provider]), \
            patch.object(llm, "current_model", return_value="first"), \
            patch.object(llm, "_provider_entries", return_value=[provider]), \
            patch.object(llm.requests, "post", side_effect=post):
        assert llm._complete_sync([], False, 10, None) == "hello"

    assert calls == [
        "Bearer first-secret",
        "Bearer second-secret",
        "Bearer third-secret",
    ]


def test_keys_rotate_after_successful_requests(provider_config, monkeypatch):
    provider = {**PROVIDERS[0], "apiKeyEnvPrefix": "FIRST_TEST_KEY_"}
    monkeypatch.setenv("FIRST_TEST_KEY_2", "second-secret")
    monkeypatch.setitem(llm._key_cursor, "first", 0)
    calls = []

    def post(url, **kwargs):
        calls.append(kwargs["headers"]["Authorization"])
        return _Response()

    with patch.object(llm, "_provider_entries", return_value=[provider]), \
            patch.object(llm, "_ordered_providers", return_value=[provider]), \
            patch.object(llm.requests, "post", side_effect=post):
        assert llm._complete_sync([], False, 10, None) == "hello"
        assert llm._complete_sync([], False, 10, None) == "hello"

    assert calls == ["Bearer first-secret", "Bearer second-secret"]


def test_no_configured_keys_raises_safe_provider_error(provider_config, monkeypatch):
    monkeypatch.delenv("FIRST_TEST_KEY")
    monkeypatch.delenv("SECOND_TEST_KEY")
    with pytest.raises(llm.ProviderError) as caught:
        llm._complete_sync([], False, 10, None)
    assert caught.value.public == "i need at least one model API key before i can think"


def test_all_provider_failures_return_a_channel_safe_error(provider_config):
    with patch.object(llm.requests, "post", return_value=_Response(429)):
        with pytest.raises(llm.ProviderError) as caught:
            llm._complete_sync([], False, 10, None)
    assert caught.value.status == 429
    assert caught.value.public == "i couldn't get a model response just now, try again in a moment"
    assert "FIRST_TEST_KEY" not in caught.value.public


# --- a rejected key is not a busy provider ----------------------------------
#
# 401/403 means the credential is wrong. Nothing the bot does can change that, so
# it is treated differently from a rate limit or an empty balance: remembered for
# much longer, reported differently, and not re-tried on the next message.

def test_a_rejected_key_is_remembered(provider_config):
    with patch.object(llm.requests, "post", return_value=_Response(401)):
        with pytest.raises(llm.ProviderError):
            llm._complete_sync([], False, 10, None)

    assert set(llm._key_backoff_until) == {"first", "second"}
    assert llm._quota_backoff_until == {}, "a bad key is not a quota problem"


def test_a_rejected_key_is_not_retried_on_the_next_message(provider_config):
    """Three providers, three doomed round trips, each up to the 120s timeout.

    Arriving at the same failure more slowly is the whole cost, so once every key
    is known bad the next message should be answered without touching the
    network at all.
    """
    with patch.object(llm.requests, "post", return_value=_Response(401)):
        with pytest.raises(llm.ProviderError):
            llm._complete_sync([], False, 10, None)

    with patch.object(llm.requests, "post", return_value=_Response(200)) as post:
        with pytest.raises(llm.ProviderError) as caught:
            llm._complete_sync([], False, 10, None)

    assert post.call_count == 0
    assert "the provider refuses" in str(caught.value)


def test_an_exhausted_balance_is_still_retried_so_a_top_up_recovers(provider_config):
    """The opposite case, and the reason the two cooldowns are separate.

    Credits get topped up and rate limits expire, so a provider in quota
    cooldown must still be attempted when nothing else is available - otherwise
    a ten-minute dip becomes an outage that only a restart clears.
    """
    with patch.object(llm.requests, "post", return_value=_Response(402)):
        with pytest.raises(llm.ProviderError):
            llm._complete_sync([], False, 10, None)

    assert llm._key_backoff_until == {}
    assert set(llm._quota_backoff_until) == {"first", "second"}

    # Still attempted, and now it succeeds - the balance was topped up.
    with patch.object(llm.requests, "post", return_value=_Response(200)) as post:
        assert llm._complete_sync([], False, 10, None) == "hello"
    assert post.call_count == 1


def test_one_working_provider_is_enough_despite_others_being_blocked(provider_config):
    """A rejected key must not disable the providers that still work."""
    good = {"id": "good", "name": "Good", "baseUrl": "https://good.example/v1",
            "model": "m", "apiKeyEnv": "GOOD_TEST_KEY", "enabled": True}
    monkey = pytest.MonkeyPatch()
    monkey.setenv("GOOD_TEST_KEY", "good-secret")
    monkey.setattr(llm, "_ordered_providers", lambda: [PROVIDERS[0], good])
    try:
        with patch.object(llm.requests, "post", side_effect=[_Response(401), _Response(200)]):
            assert llm._complete_sync([], False, 10, None) == "hello"
        assert "first" in llm._key_backoff_until
        assert "good" not in llm._key_backoff_until
    finally:
        monkey.undo()


def test_the_rejected_key_memory_is_temporary_and_clears_on_recovery(provider_config):
    """It has to expire, or the only way back is editing config.json.

    A success cannot clear it by itself - a provider that is never attempted
    never succeeds - so the cooldown is what makes recovery possible. Moving the
    deadline into the past stands in for waiting out the hour.
    """
    with patch.object(llm.requests, "post", return_value=_Response(401)):
        with pytest.raises(llm.ProviderError):
            llm._complete_sync([], False, 10, None)
    assert llm._key_backoff_until

    llm._key_backoff_until.update({pid: 0.0 for pid in llm._key_backoff_until})

    with patch.object(llm.requests, "post", return_value=_Response(200)):
        assert llm._complete_sync([], False, 10, None) == "hello"
    assert llm._key_backoff_until == {}, "a working provider should not stay flagged"


def test_the_exhausted_message_names_every_provider_not_just_the_last(provider_config):
    """The old form named one provider, so a wall of problems looked like one."""
    with patch.object(llm.requests, "post", side_effect=[_Response(401), _Response(402)]):
        with pytest.raises(llm.ProviderError) as caught:
            llm._complete_sync([], False, 10, None)

    message = str(caught.value)
    assert "First" in message and "Second" in message, message
    assert "last failure" not in message, "the whole point is that 'last' is not enough"


def test_the_exhausted_message_says_which_providers_had_no_key(provider_config, monkeypatch):
    """A provider with no key is skipped, not failed - and the difference is the
    whole diagnosis. Six providers configured and three unreachable looks
    identical to three broken ones until the log says which."""
    monkeypatch.delenv("SECOND_TEST_KEY")
    with patch.object(llm.requests, "post", return_value=_Response(401)):
        with pytest.raises(llm.ProviderError) as caught:
            llm._complete_sync([], False, 10, None)

    message = str(caught.value)
    assert "skipped, no API key set: Second" in message, message
    assert "skipped, no API key set: First" not in message


def test_rejected_keys_do_not_get_told_to_try_again_later(provider_config):
    """"Try again in a moment" is bad advice for a key that will still be wrong
    in a moment, and it is the message the channel actually gets."""
    with patch.object(llm.requests, "post", return_value=_Response(401)):
        with pytest.raises(llm.ProviderError) as caught:
            llm._complete_sync([], False, 10, None)
    assert "try again" not in caught.value.public
    assert "key" in caught.value.public


def test_a_transient_failure_still_gets_the_try_again_advice(provider_config):
    with patch.object(llm.requests, "post", return_value=_Response(503)):
        with pytest.raises(llm.ProviderError) as caught:
            llm._complete_sync([], False, 10, None)
    assert "try again" in caught.value.public


def test_no_keys_at_all_is_still_reported_as_needing_a_key(provider_config, monkeypatch):
    monkeypatch.delenv("FIRST_TEST_KEY")
    monkeypatch.delenv("SECOND_TEST_KEY")
    with patch.object(llm.requests, "post", return_value=_Response(200)) as post:
        with pytest.raises(llm.ProviderError) as caught:
            llm._complete_sync([], False, 10, None)
    assert caught.value.public == "i need at least one model API key before i can think"
    assert post.call_count == 0


def test_stream_falls_back_and_assembles_deltas(provider_config):
    first = _Response(503)
    second = _Response(lines=[
        'data: {"choices":[{"delta":{"content":"hello "}}]}',
        'data: {"choices":[{"delta":{"content":"there"}}]}',
        "data: [DONE]",
    ])
    with patch.object(llm.requests, "post", side_effect=[first, second]):
        assert llm._stream_sync([], None, 30, None) == "hello there"


def test_stream_falls_back_after_quota_exhaustion(provider_config):
    first = _Response(402)
    second = _Response(lines=[
        'data: {"choices":[{"delta":{"content":"fallback worked"}}]}',
        "data: [DONE]",
    ])
    with patch.object(llm.requests, "post", side_effect=[first, second]) as post:
        assert llm._stream_sync([], None, 30, None) == "fallback worked"
    assert [c.kwargs["json"]["model"] for c in post.call_args_list] == [
        "first-model", "second-model",
    ]


def test_stream_reports_partial_callback_failure_without_replaying(provider_config):
    def broken_lines(chunk_size=None):
        yield b'data: {"choices":[{"delta":{"content":"partial"}}]}'
        raise llm.requests.ConnectionError("connection lost")

    response = _Response(lines=[])
    response.iter_lines = broken_lines
    seen = []
    with patch.object(llm.requests, "post", return_value=response):
        with pytest.raises(llm.ProviderError):
            llm._stream_sync([], lambda delta, full: seen.append(full), 30, None)
    assert seen == ["partial"]


def test_json_completion_is_parsed(provider_config):
    async def run():
        async def fake_complete(*args, **kwargs):
            return 'result: {"ok": true}'

        with patch.object(llm, "complete", side_effect=fake_complete):
            return await llm.complete_json([], max_tokens=20)

    import asyncio

    assert asyncio.run(run()) == {"ok": True}


def test_dashboard_choices_come_from_provider_config(provider_config):
    choices = llm.models_snapshot()
    assert [choice["id"] for choice in choices] == ["first", "second"]
    assert choices[0]["chat"] is True
    assert "first-model" in choices[0]["title"]


def test_sambanova_uses_configured_gemma_31b_model():
    provider = next(p for p in config["model"]["providers"] if p["id"] == "sambanova")
    assert provider["model"] == "gemma-4-31B-it"
    assert provider["apiKeyEnvPrefix"] == "SAMBANOVA_API_KEY_"
