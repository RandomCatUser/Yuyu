
from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import USAGE_DIR, config

# Days of history kept on disk. Older files are pruned on write, so the folder
# cannot grow without bound.
KEEP_DAYS = max(7, int(config["usage"].get("keepDays", 90)))

_write_lock = threading.Lock()


# reading the numbers a provider sent


def _int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _detail_tokens(usage: object, key: str, *path: str) -> int:
    node: object = usage
    for step in path:
        if not isinstance(node, dict):
            return 0
        node = node.get(step)
    if isinstance(node, dict):
        return _int(node.get(key))
    return 0


def normalise(usage: object) -> dict:
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


# pricing


def _price_table() -> dict:
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
    """USD estimate for a call, or None when the provider/model has no price entry."""
    if not config["usage"].get("enabled", True):
        return None
    price = _lookup_price(provider, model)
    if price is None:
        return None
    return round(
        (
            _int(counts.get("prompt")) * _float(price.get("in"))
            + _int(counts.get("completion")) * _float(price.get("out"))
        )
        / 1_000_000,
        6,
    )


def record(
    provider: str,
    model: str,
    counts: dict,
    *,
    source: str = "chat",
    latency_ms: int | None = None,
    streamed: bool = False,
) -> None:
    if not config["usage"].get("enabled", True):
        return
    normal = normalise(counts)
    now = datetime.now(timezone.utc)
    entry = {
        "ts": now.timestamp() * 1000,
        "day": now.strftime("%Y-%m-%d"),
        "provider": str(provider or "unknown"),
        "model": str(model or "unknown"),
        "source": str(source or "chat"),
        "latencyMs": max(0, int(latency_ms or 0)),
        "streamed": bool(streamed),
        **normal,
        "cost": cost_of(str(provider or ""), str(model or ""), normal),
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


def _day_path(day: datetime) -> Path:
    return USAGE_DIR / f"{day.strftime('%Y-%m-%d')}.jsonl"


def _prune(now: datetime) -> None:
    keep_from = (now - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
    try:
        for path in USAGE_DIR.glob("*.jsonl"):
            if path.stem < keep_from:
                path.unlink(missing_ok=True)
    except OSError:
        pass


# reading


def _iter_entries(days: int | None = None) -> list[dict]:
    if days is None:
        days = KEEP_DAYS
    if days <= 0:
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000
    rows: list[dict] = []
    try:
        paths = sorted(USAGE_DIR.glob("*.jsonl"))
    except OSError:
        return rows
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict) and row.get("ts") is not None and _int(row.get("ts")) >= cutoff:
                rows.append(row)
    rows.sort(key=lambda row: _int(row.get("ts")))
    return rows


def _blank() -> dict:
    return {
        "prompt": 0,
        "completion": 0,
        "total": 0,
        "cached": 0,
        "reasoning": 0,
        "latencyMs": 0,
        "cost": 0.0,
        "calls": 0,
        "unpriced": 0,
    }


def _add(agg: dict, row: dict) -> None:
    agg["prompt"] += _int(row.get("prompt"))
    agg["completion"] += _int(row.get("completion"))
    agg["total"] += _int(row.get("total"))
    agg["cached"] += _int(row.get("cached"))
    agg["reasoning"] += _int(row.get("reasoning"))
    agg["latencyMs"] += _int(row.get("latencyMs"))
    agg["calls"] += 1
    cost = row.get("cost")
    if cost is None:
        agg["unpriced"] += 1
    else:
        try:
            agg["cost"] += float(cost)
        except (TypeError, ValueError):
            agg["unpriced"] += 1


def _mean_latency(bucket: dict) -> float:
    calls = bucket.get("calls") or 0
    if not calls:
        return 0.0
    return round((bucket.get("latencyMs") or 0) / calls, 1)


def _derived(entries: list[dict], window: dict) -> dict:
    calls = window["calls"]
    if not calls:
        return {"tokensPerCall": 0, "outPerSecond": 0.0, "firstTs": None, "lastTs": None,
                "busiestHour": None, "hoursActive": 0, "peakDay": None, "pricedCalls": 0}

    stamps = sorted(float(row["ts"]) for row in entries if row.get("ts") is not None)
    # Local hour: "busiest at 23:00" means the clock on the wall. `ts` is
    # milliseconds, the same value the page hands to `new Date(...)`.
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
        # How much of the window has a real price behind it. Zero means the figure
        # is unknown, which is not the same as zero.
        "pricedCalls": calls - window["unpriced"],
    }


def summary(days: int = 30) -> dict:
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
        # Money is a real total only when every call in the window had a price.
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