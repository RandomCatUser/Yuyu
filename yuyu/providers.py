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
# Two cooldowns, because the two failures need opposite handling. An exhausted
# balance or a rate limit can clear on its own, so the old behaviour of trying
# anyway when everything is cooling down is right for those. A rejected key is
# different: it does not become valid because someone sent another message, so
# re-attempting it just spends a doomed round trip to arrive at the same answer.
_quota_backoff_until: dict[str, float] = {}
_key_backoff_until: dict[str, float] = {}
_quota_backoff_lock = threading.Lock()
_QUOTA_BACKOFF_SECONDS = 15 * 60
_KEY_BACKOFF_SECONDS = 60 * 60
# Providers that answered a stream_options request with a 400. Learned at runtime
# rather than configured, because which gateways accept it changes per account.
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
    """The `/models` endpoint that pairs with this provider's chat URL."""
    base_url = str(provider["baseUrl"]).rstrip("/")
    if base_url.endswith("/chat/completions"):
        base_url = base_url[: -len("/chat/completions")]
    if base_url.endswith(("/openai", "/v1")):
        return f"{base_url}/models"
    return f"{base_url}/v1/models"


def _api_keys(provider: dict) -> list[tuple[str, str]]:
    """Read one or more credentials without ever storing their values in config."""
    keys: list[tuple[str, str]] = []
    for env_name in provider_env_names(provider):
        value = (os.environ.get(env_name) or "").strip()
        if value:
            keys.append((env_name, value))
    return keys


def _api_key(provider: dict) -> str:
    """Compatibility helper returning the first configured key."""
    keys = _api_keys(provider)
    return keys[0][1] if keys else ""


def _headers(provider: dict, api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def _body(
    provider: dict,
    messages,
    stream: bool,
    max_tokens: int | None,
    temperature: float | None,
    json_mode: bool,
    stream_usage: bool = True,
) -> dict:
    payload = {
        "model": provider["model"],
        "messages": messages,
        "stream": stream,
        "max_tokens": max_tokens if max_tokens is not None else config["model"]["maxTokens"],
        "temperature": config["model"]["temperature"] if temperature is None else temperature,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    if stream and stream_usage:
        # Without this a stream carries no token counts at all; the numbers then
        # arrive as a final chunk that only has `usage` and no choices.
        payload["stream_options"] = {"include_usage": True}
    return payload


def _friendly_error(status: int | None, body=None) -> str:
    if status in (401, 403):
        return "API key was rejected."
    if status == 402:
        return "Provider quota or credits are exhausted."
    if status == 429:
        return "Provider rate limit was reached."
    if status == 400:
        if isinstance(body, dict):
            error = body.get("error")
            if isinstance(error, dict) and error.get("message"):
                return f"Provider rejected the request: {error['message']}"
        return "Provider rejected the request."
    if status is not None and status >= 500:
        return "Provider server error."
    return "Provider request failed."


def _public_error(status: int | None = None, network: bool = False) -> str:
    if network:
        return "my connection dropped, give me a sec and try again"
    if status == 429:
        return "all the model APIs are rate limited right now, try again in a bit"
    if status == 402:
        return "all the model APIs ran out of free quota, try again later"
    return "all my model APIs are unavailable right now, try again in a bit"


def _response_error(provider: dict, response) -> ProviderError:
    """The error a bad HTTP status means, without raising it yet."""
    try:
        body = response.json()
    except (ValueError, requests.exceptions.JSONDecodeError):
        body = None
    status = response.status_code
    name = str(provider.get("name") or provider["id"])
    return ProviderError(
        f"{name}: {_friendly_error(status, body)} (HTTP {status})",
        status=status,
        provider=str(provider["id"]),
        public=_public_error(status),
    )


def _raise_response_error(provider: dict, response) -> None:
    raise _response_error(provider, response)


def _provider_failure(provider: dict, error: Exception) -> None:
    status = getattr(error, "status", None)
    pid = str(provider["id"])
    detail = f"HTTP {status}" if status is not None else type(error).__name__
    if status in (401, 403):
        # The credential itself is wrong. Nothing the bot does can fix that, and
        # it is worth remembering for far longer than a rate limit: otherwise
        # every message re-sends a key the provider has already refused.
        with _quota_backoff_lock:
            _key_backoff_until[pid] = time.monotonic() + _KEY_BACKOFF_SECONDS
        print(
            f"[llm] {pid} failed ({detail}); its API key was rejected. "
            f"Set a working key in the environment, then restart, to use it again"
        )
        return
    if status == 402:
        with _quota_backoff_lock:
            _quota_backoff_until[pid] = time.monotonic() + _QUOTA_BACKOFF_SECONDS
        print(
            f"[llm] {pid} failed ({detail}); cooling it down for "
            f"{_QUOTA_BACKOFF_SECONDS // 60} minutes and trying the next provider"
        )
        return
    print(f"[llm] {pid} failed ({detail}); trying the next provider")


def _label(provider: dict) -> str:
    return str(provider.get("name") or provider.get("id") or "?")


def _configured_with_keys() -> list[dict]:
    available: list[dict] = []
    for provider in _ordered_providers():
        if _api_keys(provider):
            available.append(provider)
    if not available:
        return []

    now = time.monotonic()
    with _quota_backoff_lock:
        for table in (_quota_backoff_until, _key_backoff_until):
            for pid, until in list(table.items()):
                if until <= now:
                    del table[pid]
        ready = [
            provider for provider in available
            if _quota_backoff_until.get(str(provider["id"]), 0) <= now
            and _key_backoff_until.get(str(provider["id"]), 0) <= now
        ]
        # Every provider that has a key has had that key refused. Calling them
        # again cannot work, and with a 120s timeout per request it turns a
        # clear misconfiguration into a slow one. So attempt nothing.
        all_keys_rejected = not ready and all(
            _key_backoff_until.get(str(provider["id"]), 0) > now for provider in available
        )
    if all_keys_rejected:
        return []
    # Quota and rate limits do clear on their own, so if those are all that is
    # in the way, allow another attempt rather than turning a temporary failure
    # into a permanent outage.
    return ready or available


def _no_usable_provider_error() -> ProviderError:
    """Why there is nothing left to call, in terms of what is actually wrong.

    "No API key is set" and "every key you have is being rejected" look the same
    from out here - she cannot answer either way - but they need opposite fixes,
    and the second is emphatically not fixed by waiting.
    """
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
    """Report every provider that was tried, not just the one that failed last.

    The old single-line form ("last failure: SambaNova ...") made a wall of
    distinct problems look like one, which is exactly backwards when the fix is
    per-provider. The channel never sees this - only the log and the dashboard do.
    """
    last = failures[-1]
    per_provider: dict[str, list[str]] = {}
    for failure in failures:
        # The message already begins with the provider's name, so it is stored
        # as-is; prefixing the label here too would print "Gemini: Gemini: ...".
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
    """Names of enabled providers whose API keys are present."""
    return [str(provider.get("name") or provider["id"]) for provider in _configured_with_keys()]


def _response_timeout() -> float:
    return max(1.0, float(config["model"]["requestTimeoutMs"]) / 1000)


def _stream_usage_wanted(provider_id: str) -> bool:
    return bool(config["usage"].get("streamUsage", True)) and provider_id not in _no_stream_usage


def _forget_stream_usage(provider_id: str) -> None:
    _no_stream_usage.add(provider_id)


def _key_attempts(provider: dict) -> list[tuple[int, str]]:
    keys = _api_keys(provider)
    if not keys:
        return []
    start = _key_cursor.get(str(provider["id"]), 0) % len(keys)
    attempts = []
    for offset in range(len(keys)):
        index = (start + offset) % len(keys)
        attempts.append((index, keys[index][1]))
    return attempts


def _key_succeeded(provider: dict, index: int) -> None:
    key_count = len(_api_keys(provider))
    if key_count:
        _key_cursor[str(provider["id"])] = (index + 1) % key_count
    with _quota_backoff_lock:
        _quota_backoff_until.pop(str(provider["id"]), None)
        _key_backoff_until.pop(str(provider["id"]), None)


def _note_usage(provider: dict, usage, source: str, started: float, *, streamed: bool) -> None:
    """Hand one call's token counts to the usage log.

    Deliberately silent on failure: the reply is already written by the time this
    runs, and losing a row of accounting is never worth losing an answer.
    """
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
    """Return a completion, moving through configured providers after failure."""
    return await asyncio.to_thread(
        _complete_sync, messages, json_mode, max_tokens, temperature, source
    )


def _sse_events(lines: Iterable[bytes]) -> Iterable[dict]:
    """Parse SSE lines yielded by requests.iter_lines()."""
    for raw in lines:
        line = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
        line = line.rstrip("\r\n")
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            yield json.loads(payload)
        except json.JSONDecodeError:
            continue


def _stream_sync(
    messages,
    on_delta,
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
            text = ""
            # A gateway that rejects `stream_options` gets the same call retried
            # once without it, and is then remembered so the retry only ever
            # happens once per provider per run.
            want_usage = _stream_usage_wanted(str(provider["id"]))
            out_of_quota = False
            while True:
                started = time.monotonic()
                try:
                    response = requests.post(
                        _provider_url(provider),
                        headers=_headers(provider, api_key),
                        json=_body(provider, messages, True, max_tokens, temperature, False, want_usage),
                        timeout=_response_timeout(),
                        stream=True,
                    )
                    if response.status_code != 200:
                        error = _response_error(provider, response)
                        # 400 on a request whose only unusual field is
                        # stream_options means the field is the problem, not the
                        # prompt - so drop it and try the identical call again.
                        if error.status == 400 and want_usage:
                            _forget_stream_usage(str(provider["id"]))
                            want_usage = False
                            continue
                        raise error

                    usage = None
                    for event in _sse_events(response.iter_lines(chunk_size=None)):
                        if not isinstance(event, dict):
                            continue
                        # The counts arrive on a final chunk that carries `usage`
                        # and an empty choices list.
                        if isinstance(event.get("usage"), dict):
                            usage = event["usage"]
                        choices = event.get("choices")
                        first = choices[0] if isinstance(choices, list) and choices else {}
                        delta_data = first.get("delta") if isinstance(first, dict) else None
                        delta = delta_data.get("content") if isinstance(delta_data, dict) else None
                        if isinstance(delta, str) and delta:
                            text += delta
                            if on_delta:
                                on_delta(delta, text)
                    if text.strip():
                        _key_succeeded(provider, key_index)
                        _note_usage(provider, usage, source, started, streamed=True)
                        return text.strip()
                    raise ProviderError(
                        f"{provider['name']} returned an empty stream.",
                        provider=str(provider["id"]),
                    )
                except ProviderError as exc:
                    failures.append(exc)
                    _provider_failure(provider, exc)
                    # 402 means this provider is out of quota, so its remaining
                    # keys would fail the same way. Move on to the next provider.
                    out_of_quota = exc.status == 402
                    break
                except requests.RequestException as exc:
                    error = ProviderError(
                        f"{provider['name']}: network error ({type(exc).__name__})",
                        provider=str(provider["id"]),
                        public=_public_error(network=True),
                    )
                    failures.append(error)
                    _provider_failure(provider, error)
                    if text and on_delta:
                        raise error from exc
                    break
                except (ValueError, KeyError, IndexError, TypeError) as exc:
                    error = ProviderError(
                        f"{provider['name']}: invalid stream response ({type(exc).__name__})",
                        provider=str(provider["id"]),
                    )
                    failures.append(error)
                    _provider_failure(provider, error)
                    break
            if out_of_quota:
                break

    raise _exhausted_error(failures)


async def stream(
    messages,
    on_delta=None,
    max_tokens: int | None = None,
    temperature: float | None = None,
    source: str = "chat",
) -> str:
    """Return a streamed completion, falling back when a provider fails."""
    return await asyncio.to_thread(
        _stream_sync, messages, on_delta, max_tokens, temperature, source
    )


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
    """Ask one provider which models it serves, for the dashboard's picker.

    `api_key` lets the panel try a key that has been typed but not saved yet;
    otherwise the provider's own configured keys are used, first one first. The
    result is sorted and de-duplicated, and capped so a provider with thousands
    of models does not paste all of them into a dropdown.
    """
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
