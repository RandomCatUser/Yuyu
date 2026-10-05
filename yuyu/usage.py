"""Token and cost accounting, one JSON line per call in usage/<date>.jsonl.

Append-only: provider calls arrive from several threads at once, so a day file
that is only ever added to cannot tear. The dashboard aggregates on read.
Cost is only shown for models priced in config.json - a guessed price is worse
than no price."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import USAGE_DIR, config

# How many days of history to keep on disk. Older files are pruned on write, so
# this folder cannot grow without bound.
KEEP_DAYS = max(7, int(config["usage"].get("keepDays", 90)))

_write_lock = threading.Lock()


# --- reading the numbers a provider sent --------------------------------------


def _int(value) -> int:
    """A token count as a non-negative int, whatever the provider called it."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, number)


def _detail_tokens(usage: dict, key: str, *path: str) -> int:
    """Pull a nested token count, e.g. prompt_tokens_details.cached_tokens.

    Providers disagree on the shape here and several omit it entirely, so this
    walks a path and gives up quietly rather than guessing.
    """
    node: object = usage
    for step in path:
        if not isinstance(node, dict):
            return 0
        node = node.get(step)
    if isinstance(node, dict):
        return _int(node.get(key))
    return 0


def normalise(usage: object) -> dict:
    """The token counts out of whatever shape one provider used.

    OpenAI-compatible APIs send prompt_tokens/completion_tokens/total_tokens.
    Some only send the first two, so the total is derived when it is missing
    rather than reported as zero. The Anthropic spelling is accepted too because
    a couple of the OpenAI-compatible gateways pass it straight through.
    """
    if not isinstance(usage, dict):
        return {"prompt": 0, "completion": 0, "total": 0, "cached": 0, "reasoning": 0}

    prompt = _int(usage.get("prompt_tokens") or usage.get("input_tokens"))
    completion = _int(usage.get("completion_tokens") or usage.get("output_tokens"))
    total = _int(usage.get("total_tokens")) or (prompt + completion)
    cached = _detail_tokens(usage, "cached_tokens", "prompt_tokens_details") or _detail_tokens(
        usage, "cached_tokens", "input_tokens_details"
    )
    reasoning = _detail_tokens(usage, "reasoning_tokens", "completion_tokens_details")
    return {
        "prompt": prompt,
        "completion": completion,
        "total": total,
        "cached": cached,
        "reasoning": reasoning,
    }


# --- pricing ------------------------------------------------------------------


def _price_table() -> dict:
    """Model -> {in, out}, in dollars per million tokens.

    Keys are matched most-specific first: `provider:model`, then `model` on its
    own, then `*`. The colon matters - some model names contain colons
    themselves (`qwen/qwen3.8-27b:free`), so only the first one splits.
    """
    prices = config["usage"].get("prices") or {}
    return prices if isinstance(prices, dict) else {}


def _lookup_price(provider: str, model: str) -> dict | None:
    table = _price_table()
    for key in (f"{provider}:{model}", model, "*"):
        entry = table.get(key)
        if isinstance(entry, dict):
            return entry
    return None


def cost_of(provider: str, model: str, counts: dict) -> float | None:
    """Dollars for one call, or None when no price is configured for it."""
    price = _lookup_price(provider, model)
    if not price:
        return None
    try:
        rate_in = float(price.get("in", 0))
        rate_out = float(price.get("out", 0))
    except (TypeError, ValueError):
        return None
    # Cached prompt tokens are billed at their own (much lower) rate when the
    # price table gives one; otherwise they are billed as ordinary input.
    cached = _int(counts.get("cached"))
    fresh_in = max(0, _int(counts.get("prompt")) - cached)
    cached_in = cached * float(price.get("cached", rate_in))
    total = (fresh_in * rate_in + cached_in + _int(counts.get("completion")) * rate_out) / 1_000_000
    return round(total, 8)


# --- writing ------------------------------------------------------------------


def _day_path(moment: datetime) -> Path:
    return USAGE_DIR / f"{moment.strftime('%Y-%m-%d')}.jsonl"


def _prune(now: datetime) -> None:
    """Drop day files past the retention window. Never raises."""
    cutoff = (now - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
    try:
        for path in USAGE_DIR.glob("*.jsonl"):
            if path.stem < cutoff:
                path.unlink(missing_ok=True)
    except OSError:
        pass


def record(
    provider: str,
    model: str,
    usage: object,
    *,
    source: str = "chat",
    latency_ms: int = 0,
    streamed: bool = False,
) -> None:
    """Note one completed model call.

    Called from the provider thread, so it must never raise into a reply: a
    failure to write the log is not a reason to lose someone's answer.
    """
    if not config["usage"].get("enabled", True):
        return
    counts = normalise(usage)
    now = datetime.now(timezone.utc)
    entry = {
        "ts": now.timestamp() * 1000,
        "day": now.strftime("%Y-%m-%d"),
        "provider": str(provider or "unknown"),
        "model": str(model or "unknown"),
        "source": str(source or "chat"),
        "latencyMs": max(0, int(latency_ms or 0)),
        "streamed": bool(streamed),
        **counts,
        "cost": cost_of(str(provider or ""), str(model or ""), counts),
    }
    line = json.dumps(entry, ensure_ascii=False)
    try:
        with _write_lock:
            USAGE_DIR.mkdir(parents=True, exist_ok=True)
            with _day_path(now).open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
            _prune(now)
    except (OSError, TypeError, ValueError):
        pass


# --- reading ------------------------------------------------------------------


def _iter_entries(days: int | None = None) -> list[dict]:
    """Every stored call, oldest first, skipping any torn or unreadable line."""
    try:
        paths = sorted(USAGE_DIR.glob("*.jsonl"))
    except OSError:
        return []
    if days:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
        paths = [p for p in paths if p.stem >= cutoff]
    entries: list[dict] = []
    for path in paths:
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        # A crash mid-append can leave one partial line. Losing a
                        # single call's row is fine; losing the file is not.
                        continue
                    if isinstance(row, dict):
                        entries.append(row)
        except OSError:
            continue
    entries.sort(key=lambda row: row.get("ts") or 0)
    return entries


def _blank() -> dict:
    return {
        "calls": 0, "prompt": 0, "completion": 0, "total": 0,
        "cached": 0, "reasoning": 0, "cost": 0.0, "unpriced": 0, "latencyMs": 0,
    }


def _add(bucket: dict, row: dict) -> None:
    bucket["calls"] += 1
    for key in ("prompt", "completion", "total", "cached", "reasoning"):
        bucket[key] += _int(row.get(key))
    bucket["latencyMs"] += _int(row.get("latencyMs"))
    cost = row.get("cost")
    if cost is None:
        # An unpriced call means the money figure is a floor rather than the whole
        # spend, so the panel gets told instead of quietly under-reporting.
        bucket["unpriced"] += 1
    else:
        try:
            bucket["cost"] += float(cost)
        except (TypeError, ValueError):
            bucket["unpriced"] += 1


def _mean_latency(bucket: dict) -> int:
    return int(bucket["latencyMs"] / bucket["calls"]) if bucket["calls"] else 0


def _derived(entries: list[dict], window: dict) -> dict:
    """The numbers you can only get by reading every row, not by summing a bucket.

    Each one is either a true fact about the window or explicitly absent. Nothing
    here is extrapolated or filled in: a window with one call has no meaningful
    hourly spread, so `busiestHour` says so rather than implying a pattern.

    `outPerSecond` is completion tokens over total call time. For a streamed call
    that time includes reading the prompt, so it reads low against a benchmark
    figure - it is here to compare her calls against each other, not to quote.
    """
    calls = window["calls"]
    if not calls:
        return {"tokensPerCall": 0, "outPerSecond": 0.0, "firstTs": None, "lastTs": None,
                "busiestHour": None, "hoursActive": 0, "peakDay": None, "pricedCalls": 0}

    stamps = sorted(float(row["ts"]) for row in entries if row.get("ts") is not None)
    # Local hour, because "she is busiest at 23:00" means the clock on the wall.
    # `ts` is milliseconds, the same value the page hands to `new Date(...)`.
    per_hour: dict[int, int] = {}
    for row in entries:
        if row.get("ts") is None:
            continue
        hour = datetime.fromtimestamp(float(row["ts"]) / 1000.0).hour
        per_hour[hour] = per_hour.get(hour, 0) + 1

    top_hour, top_calls = max(per_hour.items(), key=lambda kv: kv[1]) if per_hour else (None, 0)
    # Only a real spread of hours makes "busiest" worth saying.
    busiest = None
    if len(per_hour) > 1:
        busiest = {"hour": top_hour, "calls": top_calls, "hoursActive": len(per_hour)}

    by_day: dict[str, int] = {}
    for row in entries:
        day = str(row.get("day") or "")
        if day:
            by_day[day] = by_day.get(day, 0) + _int(row.get("total"))
    peak_day = max(by_day.items(), key=lambda kv: kv[1]) if by_day else None

    return {
        "tokensPerCall": round(window["total"] / calls, 1),
        "outPerSecond": round(window["completion"] / (window["latencyMs"] / 1000), 2)
        if window["latencyMs"] else 0.0,
        "firstTs": stamps[0] if stamps else None,
        "lastTs": stamps[-1] if stamps else None,
        "busiestHour": busiest,
        "hoursActive": len(per_hour),
        "peakDay": {"day": peak_day[0], "total": peak_day[1]} if peak_day else None,
        # How much of the window has a real price behind it. Zero means the money
        # figure is unknown, which is not the same as zero.
        "pricedCalls": calls - window["unpriced"],
    }


def summary(days: int = 30) -> dict:
    """Everything the panel draws, in one payload.

    `days` is the window the daily chart and the by-provider split cover; the
    totals also carry the same window's cost floor, so the panel can say when a
    number is incomplete rather than quietly under-reporting.
    """
    entries = _iter_entries(days)
    window = _blank()
    by_day: dict[str, dict] = {}
    by_provider: dict[str, dict] = {}
    by_model: dict[str, dict] = {}
    by_source: dict[str, dict] = {}

    for row in entries:
        _add(window, row)
        for table, key in (
            (by_day, str(row.get("day") or "")),
            (by_provider, str(row.get("provider") or "unknown")),
            (by_model, str(row.get("model") or "unknown")),
            (by_source, str(row.get("source") or "chat")),
        ):
            if not key:
                continue
            _add(table.setdefault(key, _blank()), row)

    def finish(bucket: dict) -> dict:
        out = dict(bucket)
        out["avgLatencyMs"] = _mean_latency(bucket)
        out.pop("latencyMs", None)
        out["cost"] = round(out["cost"], 6)
        return out

    unpriced = window["unpriced"]
    recent = [
        {
            "ts": row.get("ts"),
            "provider": row.get("provider"),
            "model": row.get("model"),
            "source": row.get("source"),
            "prompt": _int(row.get("prompt")),
            "completion": _int(row.get("completion")),
            "total": _int(row.get("total")),
            "cached": _int(row.get("cached")),
            "cost": row.get("cost"),
            "latencyMs": _int(row.get("latencyMs")),
            "streamed": bool(row.get("streamed")),
        }
        for row in entries[-60:]
    ]

    return {
        "enabled": bool(config["usage"].get("enabled", True)),
        "days": days,
        "keepDays": KEEP_DAYS,
        "totals": finish(window),
        "derived": _derived(entries, window),
        "byDay": [{"day": day, **finish(bucket)} for day, bucket in sorted(by_day.items())],
        "byProvider": [
            {"provider": name, **finish(bucket)}
            for name, bucket in sorted(by_provider.items(), key=lambda kv: -kv[1]["total"])
        ],
        "byModel": [
            {"model": name, **finish(bucket)}
            for name, bucket in sorted(by_model.items(), key=lambda kv: -kv[1]["total"])[:25]
        ],
        "bySource": [
            {"source": name, **finish(bucket)}
            for name, bucket in sorted(by_source.items(), key=lambda kv: -kv[1]["total"])
        ],
        "recent": list(reversed(recent)),
        # Money is only a real total when every call in the window had a price.
        "costComplete": unpriced == 0,
        "unpricedCalls": unpriced,
        "pricedModels": sorted(
            {str(row.get("model") or "") for row in entries if row.get("cost") is not None}
        ),
    }


def clear() -> int:
    """Delete every stored day file. Returns how many were removed."""
    removed = 0
    with _write_lock:
        try:
            paths = list(USAGE_DIR.glob("*.jsonl"))
        except OSError:
            return 0
        for path in paths:
            try:
                path.unlink()
                removed += 1
            except OSError:
                continue
    return removed