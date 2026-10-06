"""OpenAI-compatible text APIs with ordered provider failover."""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from typing import Iterable

import requests

from .config import config, current_model, provider_env_names

_key_cursor: dict[str, int] = {}
# Two cooldowns, because the failures need opposite handling. Quota and rate
# limits clear on their own, so trying anyway is right. A rejected key does not,
# so re-attempting spends a doomed round trip.
_quota_backoff_until: dict[str, float] = {}
_key_backoff_until: dict[str, float] = {}
_quota_backoff_lock = threading.Lock()
_QUOTA_BACKOFF_SECONDS = 15 * 60
_KEY_BACKOFF_SECONDS = 60 * 60
# Providers that answered a stream_options request with a 400. Learned at runtime,
# because which gateways accept it varies per account.
_no_stream_usage: set[str] = set()


class ProviderError(Exception):
    def __init__(
        self,
        message: str,
        status: int | None = None,
        provider: str | None = None,
        public: str | None = None,
    ):
        super().__init__(message)
        self.status = status
        self.provider = provider
        self.public = public or "all my model APIs are unavailable right now, try again in a bit"


def _provider_entries() -> list[dict]:
    entries = config["model"].get("providers") or []
    return [
        provider for provider in entries
        if isinstance(provider, dict)
        and provider.get("enabled", True)
        and provider.get("id")
        and provider.get("baseUrl")
        and provider.get("model")
    ]


def _ordered_providers() -> list[dict]:
    providers = _provider_entries()
    primary = current_model()
    return sorted(providers, key=lambda provider: provider.get("id") != primary)


def _provider_url(provider: dict) -> str:
    base_url = str(provider["baseUrl"]).rstrip("/")
    if base_url.endswith("/chat/completions"):
        return base_url
    if base_url.endswith(("/openai", "/v1")):
        return f"{base_url}/chat/completions"
    return f"{base_url}/v1/chat/completions"


def _models_url(provider: dict) -> str:
    ordered = _ordered_providers()
    keyed = [provider for provider in ordered if _api_keys(provider)]
    if not keyed:
        return ProviderError(
            "No enabled model provider has an API key configured.",
            public="i need at least one model API key before i can think",
        )
    now = time.monotonic()
    rejected = [
        _label(provider) for provider in keyed
        if _key_backoff_until.get(str(provider["id"]), 0) > now
    ]
    names = ", ".join(rejected) or ", ".join(_label(provider) for provider in keyed)
    return ProviderError(
        f"Every configured provider has an API key the provider refuses: {names}. "
        "A 401/403 means the key is wrong, revoked, or for a different account - "
        "not that the provider is busy.",
        provider=str(keyed[0]["id"]),
        public=(
            "none of my model keys are being accepted at the moment - the one i "
            "tried last said the key itself is wrong"
        ),
    )


def _exhausted_error(failures: list[ProviderError]) -> ProviderError:
    last = failures[-1]
    per_provider: dict[str, list[str]] = {}
    for failure in failures:
        # The message already begins with the provider's name; prefixing here too
        # would print "Gemini: Gemini: ...".
        per_provider.setdefault(_label_by_id(failure.provider), []).append(str(failure))
    tried = "; ".join(" | ".join(details) for details in per_provider.values())
    unkeyed = [
        _label(provider) for provider in _ordered_providers() if not _api_keys(provider)
    ]
    parts = [f"all {len(per_provider)} provider(s) with a key failed -> {tried}"]
    if unkeyed:
        parts.append(f"skipped, no API key set: {', '.join(unkeyed)}")

    permanent = all(f.status in (401, 403) for f in failures if f.status is not None)
    public = (
        "none of my model keys are being accepted right now - the provider says "
        "the key itself is wrong"
        if permanent and failures
        else "i couldn't get a model response just now, try again in a moment"
    )
    return ProviderError(
        "All configured providers failed; " + "; ".join(parts),
        status=last.status,
        provider=last.provider,
        public=public,
    )


def _label_by_id(provider_id: str | None) -> str:
    if not provider_id:
        return "unknown"
    for provider in _ordered_providers():
        if str(provider.get("id")) == str(provider_id):
            return _label(provider)
    return str(provider_id)


def configured_provider_names() -> list[str]:
    try:
        from .usage import record

        record(
            str(provider.get("id") or "unknown"),
            str(provider.get("model") or "unknown"),
            usage,
            source=source,
            latency_ms=int((time.monotonic() - started) * 1000),
            streamed=streamed,
        )
    except Exception:  # pragma: no cover - accounting must never break a reply
        pass


def _complete_sync(
    messages,
    json_mode: bool,
    max_tokens: int | None,
    temperature: float | None,
    source: str = "chat",
) -> str:
    providers = _configured_with_keys()
    if not providers:
        raise _no_usable_provider_error()

    failures: list[ProviderError] = []
    for provider in providers:
        for key_index, api_key in _key_attempts(provider):
            started = time.monotonic()
            try:
                response = requests.post(
                    _provider_url(provider),
                    headers=_headers(provider, api_key),
                    json=_body(provider, messages, False, max_tokens, temperature, json_mode),
                    timeout=_response_timeout(),
                )
                if response.status_code != 200:
                    _raise_response_error(provider, response)
                data = response.json()
                choices = data.get("choices") if isinstance(data, dict) else None
                first = choices[0] if isinstance(choices, list) and choices else {}
                message = first.get("message") if isinstance(first, dict) else None
                text = message.get("content", "") if isinstance(message, dict) else ""
                if not isinstance(text, str) or not text.strip():
                    raise ProviderError(
                        f"{provider['name']} returned an empty completion.",
                        provider=str(provider["id"]),
                    )
                _key_succeeded(provider, key_index)
                _note_usage(provider, data.get("usage"), source, started, streamed=False)
                return text.strip()
            except ProviderError as exc:
                failures.append(exc)
                _provider_failure(provider, exc)
                if exc.status == 402:
                    break
            except requests.RequestException as exc:
                error = ProviderError(
                    f"{provider['name']}: network error ({type(exc).__name__})",
                    provider=str(provider["id"]),
                    public=_public_error(network=True),
                )
                failures.append(error)
                _provider_failure(provider, error)
            except (ValueError, KeyError, IndexError, TypeError) as exc:
                error = ProviderError(
                    f"{provider['name']}: invalid completion response ({type(exc).__name__})",
                    provider=str(provider["id"]),
                )
                failures.append(error)
                _provider_failure(provider, error)

    raise _exhausted_error(failures)


async def complete(
    messages,
    json_mode: bool = False,
    max_tokens: int | None = None,
    temperature: float | None = None,
    source: str = "chat",
) -> str:
    if api_key is None:
        api_key = _api_key(provider)
    headers = _headers(provider, api_key) if api_key else {"Content-Type": "application/json"}
    try:
        response = requests.get(_models_url(provider), headers=headers, timeout=20)
    except requests.RequestException as exc:
        raise ProviderError(
            f"{provider.get('name') or provider.get('id')}: network error ({type(exc).__name__})",
            provider=str(provider.get("id") or ""),
            public="could not reach that provider to list its models",
        ) from exc

    if response.status_code != 200:
        _raise_response_error(provider, response)
    try:
        data = response.json()
    except (ValueError, requests.exceptions.JSONDecodeError):
        raise ProviderError(
            f"{provider.get('name') or provider.get('id')}: the model list was not JSON",
            provider=str(provider.get("id") or ""),
            public="that provider's model list came back in a shape i do not understand",
        )

    rows = data.get("data") if isinstance(data, dict) else data
    ids: list[str] = []
    seen: set[str] = set()
    for row in rows or []:
        candidate = (row.get("id") or row.get("name")) if isinstance(row, dict) else row
        if isinstance(candidate, str) and candidate.strip() and candidate not in seen:
            seen.add(candidate)
            ids.append(candidate.strip())
    ids.sort()
    return ids[:limit]


async def complete_json(messages, **kwargs):
    """Request JSON and parse the first complete JSON object in the response."""
    raw = await complete(
        messages,
        json_mode=True,
        temperature=kwargs.pop("temperature", 0.2),
        **kwargs,
    )
    if not raw:
        return None
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        return json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return None


def fetch_models(provider: dict, api_key: str | None = None, *, limit: int = 500) -> list[str]:
    if api_key is None:
        api_key = _api_key(provider)
    headers = _headers(provider, api_key) if api_key else {"Content-Type": "application/json"}
    try:
        response = requests.get(_models_url(provider), headers=headers, timeout=20)
    except requests.RequestException as exc:
        raise ProviderError(
            f"{provider.get('name') or provider.get('id')}: network error ({type(exc).__name__})",
            provider=str(provider.get("id") or ""),
            public="could not reach that provider to list its models",
        ) from exc

    if response.status_code != 200:
        _raise_response_error(provider, response)
    try:
        data = response.json()
    except (ValueError, requests.exceptions.JSONDecodeError):
        raise ProviderError(
            f"{provider.get('name') or provider.get('id')}: the model list was not JSON",
            provider=str(provider.get("id") or ""),
            public="that provider's model list came back in a shape i do not understand",
        )

    rows = data.get("data") if isinstance(data, dict) else data
    ids: list[str] = []
    seen: set[str] = set()
    for row in rows or []:
        candidate = (row.get("id") or row.get("name")) if isinstance(row, dict) else row
        if isinstance(candidate, str) and candidate.strip() and candidate not in seen:
            seen.add(candidate)
            ids.append(candidate.strip())
    ids.sort()
    return ids[:limit]


def models_snapshot() -> list[dict]:
    """Dashboard choices from configured providers; never needs a live request."""
    return [
        {
            "id": str(provider["id"]),
            "title": f"{provider.get('name') or provider['id']} — {provider['model']}",
            "model": str(provider["model"]),
            "description": "Configured OpenAI-compatible text provider",
            "owned_by": str(provider.get("name") or provider["id"]),
            "category": "text",
            "chat": True,
            "context_length": 0,
            "success_rate": None,
            "status": f"{len(_api_keys(provider))} key(s) configured" if _api_keys(provider) else "needs API key",
            "reasoning": False,
        }
        for provider in _provider_entries()
    ]
