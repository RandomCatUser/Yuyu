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
    # Both cooldown tables. A rejected key is remembered separately, so leaving
    # it out would let one 401 disable a provider for every later test.
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
    with patch.object(llm.requests, "post", return_value=_Response(401)):
        with pytest.raises(llm.ProviderError):
            llm._complete_sync([], False, 10, None)

    with patch.object(llm.requests, "post", return_value=_Response(200)) as post:
        with pytest.raises(llm.ProviderError) as caught:
            llm._complete_sync([], False, 10, None)

    assert post.call_count == 0
    assert "the provider refuses" in str(caught.value)


def test_an_exhausted_balance_is_still_retried_so_a_top_up_recovers(provider_config):
    with patch.object(llm.requests, "post", return_value=_Response(402)):
        with pytest.raises(llm.ProviderError):
            llm._complete_sync([], False, 10, None)

    assert llm._key_backoff_until == {}
    assert set(llm._quota_backoff_until) == {"first", "second"}

    # Still attempted, and now it succeeds: the balance was topped up.
    with patch.object(llm.requests, "post", return_value=_Response(200)) as post:
        assert llm._complete_sync([], False, 10, None) == "hello"
    assert post.call_count == 1


def test_one_working_provider_is_enough_despite_others_being_blocked(provider_config):
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
    monkeypatch.delenv("SECOND_TEST_KEY")
    with patch.object(llm.requests, "post", return_value=_Response(401)):
        with pytest.raises(llm.ProviderError) as caught:
            llm._complete_sync([], False, 10, None)

    message = str(caught.value)
    assert "skipped, no API key set: Second" in message, message
    assert "skipped, no API key set: First" not in message


def test_rejected_keys_do_not_get_told_to_try_again_later(provider_config):
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
