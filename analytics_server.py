from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sqlite3
import statistics
import sys
import threading
import time
import webbrowser
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib import parse


SOURCE_DIR = Path(__file__).resolve().parent
IS_FROZEN = bool(getattr(sys, "frozen", False))
INSTALL_DIR = Path(sys.executable).resolve().parent if IS_FROZEN else SOURCE_DIR
BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", INSTALL_DIR))
if IS_FROZEN:
    APP_DIR = Path(
        os.environ.get("TOKEN_PULSE_DATA_DIR")
        or Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "Token Pulse"
    )
else:
    APP_DIR = Path(os.environ.get("TOKEN_PULSE_DATA_DIR") or SOURCE_DIR)

STATIC_DIR = BUNDLE_DIR / "analytics"
if not STATIC_DIR.exists():
    STATIC_DIR = SOURCE_DIR / "analytics"

USAGE_HISTORY_JSON = Path(
    os.environ.get("TOKEN_PULSE_USAGE_HISTORY_JSON")
    or os.environ.get("USAGE_HISTORY_JSON")
    or os.environ.get("SUB2API_USAGE_HISTORY_JSON")
    or APP_DIR / "usage_history.json"
)
CCSWITCH_DB = Path(
    os.environ.get("TOKEN_PULSE_CCSWITCH_DB")
    or os.environ.get("CCSWITCH_DB")
    or Path.home() / ".cc-switch" / "cc-switch.db"
)

CN_TZ = timezone(timedelta(hours=8), "CST")
DEFAULT_HOST = os.environ.get("TOKEN_PULSE_ANALYTICS_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("TOKEN_PULSE_ANALYTICS_PORT", "8765"))
MAX_RANGE_DAYS = 1826

METRIC_KEYS = (
    "requests",
    "tokens",
    "cost",
    "input_tokens",
    "cached_input_tokens",
    "cache_creation_input_tokens",
    "output_tokens",
)

_cache_lock = threading.Lock()
_cache_key: tuple[Any, ...] | None = None
_cache_value: dict[str, Any] | None = None


def now_local() -> datetime:
    return datetime.now(CN_TZ)


def safe_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def safe_float(value: Any) -> float:
    try:
        return max(0.0, float(value or 0))
    except (TypeError, ValueError):
        return 0.0


def parse_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value or "").strip())
    except (TypeError, ValueError):
        return None


def file_signature(path: Path) -> tuple[str, int, int]:
    try:
        stat = path.stat()
        return str(path), stat.st_mtime_ns, stat.st_size
    except OSError:
        return str(path), 0, 0


def read_json_stable(path: Path, fallback: Any) -> Any:
    for attempt in range(3):
        try:
            return json.loads(path.read_text(encoding="utf-8", errors="strict"))
        except (OSError, json.JSONDecodeError):
            if attempt < 2:
                time.sleep(0.03)
    return fallback


def source_label(raw_source: str, origin: str) -> str:
    if origin == "ccswitch_backfill":
        return "CCSWITCH"
    if origin == "missing":
        return "MISSING"
    source = (raw_source or "").strip().lower()
    if source == "both" or "mixed" in source:
        return "MIXED"
    if "sub2api" in source or source == "sub":
        return "SUB"
    return "LOCAL"


def normalize_models(value: Any) -> dict[str, int]:
    result: dict[str, int] = {}
    if not isinstance(value, dict):
        return result
    for name, tokens in value.items():
        amount = safe_int(tokens)
        if amount > 0:
            key = str(name or "unknown").strip() or "unknown"
            result[key] = result.get(key, 0) + amount
    return result


def normalize_provider(value: Any, fallback_name: str = "Unassigned") -> dict[str, Any]:
    row = value if isinstance(value, dict) else {}
    models = normalize_models(row.get("models"))
    normalized = {
        "name": str(row.get("name") or row.get("account_name") or fallback_name).strip() or fallback_name,
        "plan_type": str(row.get("plan_type") or "").strip(),
        "requests": safe_int(row.get("requests")),
        "tokens": safe_int(row.get("tokens")),
        "cost": round(safe_float(row.get("cost")), 6),
        "input_tokens": safe_int(row.get("input_tokens")),
        "cached_input_tokens": safe_int(row.get("cached_input_tokens")),
        "cache_creation_input_tokens": safe_int(row.get("cache_creation_input_tokens")),
        "output_tokens": safe_int(row.get("output_tokens")),
        "models": models,
        "latest_at": str(row.get("latest_at") or ""),
        "latest_model": str(row.get("latest_model") or ""),
        "cost_multiplier": safe_float(row.get("cost_multiplier")) or 1.0,
        "speed_badge": str(row.get("speed_badge") or ""),
    }
    if normalized["tokens"] <= 0:
        normalized["tokens"] = sum(normalized[key] for key in METRIC_KEYS[3:])
    return normalized


def normalize_day(day_key: str, value: Any, origin: str = "primary") -> dict[str, Any]:
    row = value if isinstance(value, dict) else {}
    providers = [normalize_provider(item) for item in (row.get("providers") or []) if isinstance(item, dict)]
    models = normalize_models(row.get("models"))
    raw_source = str(row.get("source") or "")
    normalized = {
        "date": day_key,
        "origin": origin,
        "source": source_label(raw_source, origin),
        "source_raw": raw_source,
        "requests": safe_int(row.get("requests")),
        "tokens": safe_int(row.get("tokens")),
        "cost": round(safe_float(row.get("cost")), 6),
        "input_tokens": safe_int(row.get("input_tokens")),
        "cached_input_tokens": safe_int(row.get("cached_input_tokens")),
        "cache_creation_input_tokens": safe_int(row.get("cache_creation_input_tokens")),
        "output_tokens": safe_int(row.get("output_tokens")),
        "providers": providers,
        "models": models,
        "updated_at": str(row.get("updated_at") or row.get("offline_reconciled_at") or ""),
        "detail_source": str(row.get("detail_source") or ""),
        "detail_tokens": safe_int(row.get("detail_tokens")),
        "source_gap": row.get("source_gap") if isinstance(row.get("source_gap"), dict) else {},
    }
    if normalized["tokens"] <= 0:
        component_total = sum(normalized[key] for key in METRIC_KEYS[3:])
        normalized["tokens"] = component_total
    return normalized


def _ccswitch_provider_name(app_type: str, provider_id: str, provider_name: str) -> str:
    app = (app_type or "unknown").strip().lower() or "unknown"
    provider_id = (provider_id or "").strip()
    provider_name = (provider_name or "").strip()
    if provider_id == "_codex_session":
        return "ccSwitch Codex session"
    if provider_id == "_claude_session" or (app == "claude" and provider_id.startswith("_")):
        return "ccSwitch Claude session"
    if provider_name and provider_name != provider_id:
        return f"ccSwitch {app} - {provider_name}"
    return f"ccSwitch {app} - {provider_id or 'unknown'}"


def load_ccswitch_days() -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    if not CCSWITCH_DB.exists():
        return {}, {"available": False, "path": str(CCSWITCH_DB), "message": "ccSwitch database not found"}

    buckets: dict[tuple[str, str], dict[str, Any]] = {}
    proxy_keys: set[tuple[str, str, str, str]] = set()

    def add_row(row: sqlite3.Row, latest_at: str = "") -> None:
        day_key = str(row["local_date"] or row["date"] or "")
        if not parse_date(day_key):
            return
        app_type = str(row["app_type"] or "")
        provider_id = str(row["provider_id"] or "")
        name = _ccswitch_provider_name(app_type, provider_id, str(row["provider_name"] or ""))
        target = buckets.setdefault(
            (day_key, name),
            {
                "name": name,
                "requests": 0,
                "tokens": 0,
                "cost": 0.0,
                "input_tokens": 0,
                "cached_input_tokens": 0,
                "cache_creation_input_tokens": 0,
                "output_tokens": 0,
                "models": {},
                "latest_at": "",
            },
        )
        target["requests"] += safe_int(row["request_count"])
        target["input_tokens"] += safe_int(row["input_tokens"])
        target["cached_input_tokens"] += safe_int(row["cache_read_tokens"])
        target["cache_creation_input_tokens"] += safe_int(row["cache_creation_tokens"])
        target["output_tokens"] += safe_int(row["output_tokens"])
        target["tokens"] += safe_int(row["tokens"])
        target["cost"] += safe_float(row["cost"])
        model = str(row["model"] or "unknown").strip() or "unknown"
        target["models"][model] = target["models"].get(model, 0) + safe_int(row["tokens"])
        if latest_at and latest_at > target["latest_at"]:
            target["latest_at"] = latest_at

    try:
        con = sqlite3.connect(f"file:{CCSWITCH_DB}?mode=ro", uri=True, timeout=2)
        con.row_factory = sqlite3.Row
        proxy_rows = con.execute(
            """
            SELECT
                date(l.created_at, 'unixepoch', '+8 hours') AS local_date,
                '' AS date,
                l.app_type,
                l.provider_id,
                COALESCE(p.name, l.provider_id) AS provider_name,
                l.model,
                COUNT(*) AS request_count,
                SUM(l.input_tokens) AS input_tokens,
                SUM(l.output_tokens) AS output_tokens,
                SUM(l.cache_read_tokens) AS cache_read_tokens,
                SUM(l.cache_creation_tokens) AS cache_creation_tokens,
                SUM(l.input_tokens + l.output_tokens + l.cache_read_tokens + l.cache_creation_tokens) AS tokens,
                SUM(CAST(l.total_cost_usd AS REAL)) AS cost,
                MAX(l.created_at) AS latest_created_at
            FROM proxy_request_logs l
            LEFT JOIN providers p ON p.id = l.provider_id AND p.app_type = l.app_type
            GROUP BY local_date, l.app_type, l.provider_id, l.model
            """
        ).fetchall()
        for row in proxy_rows:
            day_key = str(row["local_date"] or "")
            proxy_keys.add((day_key, str(row["app_type"] or ""), str(row["provider_id"] or ""), str(row["model"] or "")))
            latest = ""
            if safe_int(row["latest_created_at"]):
                latest = datetime.fromtimestamp(safe_int(row["latest_created_at"]), tz=CN_TZ).isoformat(timespec="seconds")
            add_row(row, latest)

        rollup_rows = con.execute(
            """
            SELECT
                '' AS local_date,
                r.date,
                r.app_type,
                r.provider_id,
                COALESCE(p.name, r.provider_id) AS provider_name,
                r.model,
                SUM(r.request_count) AS request_count,
                SUM(r.input_tokens) AS input_tokens,
                SUM(r.output_tokens) AS output_tokens,
                SUM(r.cache_read_tokens) AS cache_read_tokens,
                SUM(r.cache_creation_tokens) AS cache_creation_tokens,
                SUM(r.input_tokens + r.output_tokens + r.cache_read_tokens + r.cache_creation_tokens) AS tokens,
                SUM(CAST(r.total_cost_usd AS REAL)) AS cost,
                0 AS latest_created_at
            FROM usage_daily_rollups r
            LEFT JOIN providers p ON p.id = r.provider_id AND p.app_type = r.app_type
            GROUP BY r.date, r.app_type, r.provider_id, r.model
            """
        ).fetchall()
        con.close()
        for row in rollup_rows:
            key = (str(row["date"] or ""), str(row["app_type"] or ""), str(row["provider_id"] or ""), str(row["model"] or ""))
            if key not in proxy_keys:
                add_row(row)
    except sqlite3.Error as exc:
        return {}, {"available": False, "path": str(CCSWITCH_DB), "message": str(exc)}

    by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for (day_key, _name), provider in buckets.items():
        provider["cost"] = round(provider["cost"], 6)
        by_day[day_key].append(provider)

    result: dict[str, dict[str, Any]] = {}
    for day_key, providers in by_day.items():
        models: dict[str, int] = {}
        row: dict[str, Any] = {
            "date": day_key,
            "source": "ccswitch_backfill",
            "providers": providers,
            "models": models,
            "updated_at": "",
        }
        for metric in METRIC_KEYS:
            row[metric] = sum(safe_float(p.get(metric)) if metric == "cost" else safe_int(p.get(metric)) for p in providers)
        row["cost"] = round(float(row["cost"]), 6)
        for provider in providers:
            for model, amount in provider.get("models", {}).items():
                models[model] = models.get(model, 0) + safe_int(amount)
        result[day_key] = normalize_day(day_key, row, "ccswitch_backfill")

    return result, {
        "available": True,
        "path": str(CCSWITCH_DB),
        "days": len(result),
        "first_date": min(result) if result else "",
        "last_date": max(result) if result else "",
        "message": "ok",
    }


def load_merged_history(force: bool = False) -> dict[str, Any]:
    global _cache_key, _cache_value
    signature = (file_signature(USAGE_HISTORY_JSON), file_signature(CCSWITCH_DB))
    with _cache_lock:
        if not force and signature == _cache_key and _cache_value is not None:
            return _cache_value

        raw = read_json_stable(USAGE_HISTORY_JSON, {"schema": 2, "days": {}})
        raw_days = raw.get("days") if isinstance(raw, dict) else {}
        primary: dict[str, dict[str, Any]] = {}
        if isinstance(raw_days, dict):
            for day_key, row in raw_days.items():
                if parse_date(day_key) and isinstance(row, dict):
                    primary[day_key] = normalize_day(day_key, row, "primary")

        ccswitch, cc_meta = load_ccswitch_days()
        merged = dict(primary)
        for day_key, row in ccswitch.items():
            if day_key not in primary:
                merged[day_key] = row

        _cache_key = signature
        _cache_value = {
            "schema": 1,
            "days": merged,
            "primary_days": primary,
            "ccswitch_days": ccswitch,
            "ccswitch": cc_meta,
            "history_path": str(USAGE_HISTORY_JSON),
            "history_exists": USAGE_HISTORY_JSON.exists(),
            "loaded_at": now_local().isoformat(timespec="seconds"),
        }
        return _cache_value


def iter_dates(start: date, end: date) -> list[date]:
    count = max(0, min(MAX_RANGE_DAYS, (end - start).days + 1))
    return [start + timedelta(days=index) for index in range(count)]


def resolve_bounds(query: dict[str, list[str]], history: dict[str, Any]) -> tuple[date, date, str]:
    today = now_local().date()
    explicit_start = parse_date((query.get("start") or [""])[0])
    explicit_end = parse_date((query.get("end") or [""])[0])
    range_key = str((query.get("range") or ["90d"])[0]).strip().lower()
    available = sorted(parse_date(key) for key in history.get("days", {}) if parse_date(key))
    earliest = available[0] if available else today
    latest = max(today, available[-1]) if available else today
    if explicit_start or explicit_end:
        end = explicit_end or latest
        start = explicit_start or earliest
        range_key = "custom"
    elif range_key in {"all", "max"}:
        start, end = earliest, latest
        range_key = "all"
    else:
        try:
            days = int(range_key.removesuffix("d"))
        except ValueError:
            days = 90
        days = max(1, min(MAX_RANGE_DAYS, days))
        end = latest
        start = end - timedelta(days=days - 1)
        range_key = f"{days}d"
    if end < start:
        start, end = end, start
    if (end - start).days + 1 > MAX_RANGE_DAYS:
        start = end - timedelta(days=MAX_RANGE_DAYS - 1)
    return start, end, range_key


def blank_day(day_key: str) -> dict[str, Any]:
    return normalize_day(day_key, {"source": "", "providers": [], "models": {}}, "missing")


def providers_with_residual(day: dict[str, Any]) -> list[dict[str, Any]]:
    providers = [dict(item) for item in day.get("providers", []) if isinstance(item, dict)]
    sums = {metric: sum(safe_float(p.get(metric)) if metric == "cost" else safe_int(p.get(metric)) for p in providers) for metric in METRIC_KEYS}
    residual = {
        metric: max(0.0 if metric == "cost" else 0, day.get(metric, 0) - sums[metric])
        for metric in METRIC_KEYS
    }
    if residual["tokens"] > 0 or residual["requests"] > 0 or residual["cost"] > 0.000001:
        label = {
            "SUB": "Unassigned Sub2API",
            "CCSWITCH": "Unassigned ccSwitch backfill",
            "MIXED": "Unassigned mixed source",
        }.get(day.get("source"), "Unassigned local")
        model_residual = max(0, safe_int(day.get("tokens")) - sum(safe_int(value) for value in day.get("models", {}).values()))
        providers.append(
            normalize_provider(
                {
                    "name": label,
                    **residual,
                    "models": {"Unassigned model": model_residual} if model_residual else {},
                }
            )
        )
    return providers


def merge_provider_rows(rows: list[dict[str, Any]], name: str = "Selected account") -> dict[str, Any]:
    result = normalize_provider({"name": name})
    result["models"] = {}
    result["latest_at"] = ""
    for row in rows:
        for metric in METRIC_KEYS:
            if metric == "cost":
                result[metric] += safe_float(row.get(metric))
            else:
                result[metric] += safe_int(row.get(metric))
        for model, amount in row.get("models", {}).items():
            result["models"][model] = result["models"].get(model, 0) + safe_int(amount)
        latest = str(row.get("latest_at") or "")
        if latest > result["latest_at"]:
            result["latest_at"] = latest
    result["cost"] = round(result["cost"], 6)
    return result


def scale_day_for_model(day: dict[str, Any], model: str) -> dict[str, Any]:
    base_tokens = safe_int(day.get("tokens"))
    model_tokens = safe_int(day.get("models", {}).get(model))
    ratio = model_tokens / base_tokens if base_tokens else 0.0
    result = dict(day)
    result["tokens"] = model_tokens
    result["models"] = {model: model_tokens} if model_tokens else {}
    for metric in ("requests", "input_tokens", "cached_input_tokens", "cache_creation_input_tokens", "output_tokens"):
        result[metric] = int(round(safe_int(day.get(metric)) * ratio))
    result["cost"] = round(safe_float(day.get("cost")) * ratio, 6)
    result["estimated"] = bool(model_tokens)
    result["estimated_fields"] = ["requests", "cost", "token_composition"] if model_tokens else []
    return result


def project_day(day: dict[str, Any], account: str, model: str) -> dict[str, Any]:
    result = dict(day)
    result["estimated"] = False
    result["estimated_fields"] = []
    if account:
        selected = [row for row in providers_with_residual(day) if row.get("name") == account]
        provider = merge_provider_rows(selected, account)
        for metric in METRIC_KEYS:
            result[metric] = provider[metric]
        result["models"] = provider["models"]
        result["providers"] = selected
    if model:
        result = scale_day_for_model(result, model)
    return result


def selected_days(
    history: dict[str, Any],
    start: date,
    end: date,
    source: str = "ALL",
    account: str = "",
    model: str = "",
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    source = source.upper()
    for current in iter_dates(start, end):
        key = current.isoformat()
        original = history.get("days", {}).get(key) or blank_day(key)
        if source not in {"", "ALL"} and original.get("source") != source:
            projected = project_day(original, account, model)
            for metric in METRIC_KEYS:
                projected[metric] = 0.0 if metric == "cost" else 0
            projected["filtered_out"] = True
        else:
            projected = project_day(original, account, model)
            projected["filtered_out"] = False
        projected["account_count"] = len([p for p in providers_with_residual(original) if safe_int(p.get("tokens")) or safe_int(p.get("requests"))])
        projected["model_count"] = len(original.get("models", {}))
        result.append(projected)

    running = 0
    for index, row in enumerate(result):
        window = [safe_int(item.get("tokens")) for item in result[max(0, index - 6) : index + 1]]
        row["moving_average_7"] = round(sum(window) / len(window), 2) if window else 0
        running += safe_int(row.get("tokens"))
        row["cumulative_tokens"] = running
    return result


def sum_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    result = {metric: 0.0 if metric == "cost" else 0 for metric in METRIC_KEYS}
    for row in rows:
        for metric in METRIC_KEYS:
            if metric == "cost":
                result[metric] += safe_float(row.get(metric))
            else:
                result[metric] += safe_int(row.get(metric))
    result["cost"] = round(result["cost"], 6)
    return result


def percent_change(current: float, previous: float) -> float | None:
    if previous == 0:
        return None if current == 0 else 100.0
    return round((current - previous) / previous * 100, 2)


def build_summary(rows: list[dict[str, Any]], previous_rows: list[dict[str, Any]]) -> dict[str, Any]:
    totals = sum_metrics(rows)
    previous = sum_metrics(previous_rows)
    active = [row for row in rows if safe_int(row.get("tokens")) or safe_int(row.get("requests")) or safe_float(row.get("cost"))]
    peak = max(rows, key=lambda item: safe_int(item.get("tokens")), default=blank_day(""))
    input_pool = totals["input_tokens"] + totals["cached_input_tokens"] + totals["cache_creation_input_tokens"]
    calendar_days = max(1, len(rows))
    active_days = len(active)
    return {
        **totals,
        "active_days": active_days,
        "calendar_days": len(rows),
        "average_tokens_per_day": round(totals["tokens"] / calendar_days, 2),
        "average_tokens_per_active_day": round(totals["tokens"] / max(1, active_days), 2),
        "tokens_per_request": round(totals["tokens"] / max(1, totals["requests"]), 2),
        "cost_per_million_tokens": round(totals["cost"] * 1_000_000 / max(1, totals["tokens"]), 4),
        "cache_hit_rate": round(totals["cached_input_tokens"] / max(1, input_pool) * 100, 2),
        "output_share": round(totals["output_tokens"] / max(1, totals["tokens"]) * 100, 2),
        "peak_day": {"date": peak.get("date", ""), "tokens": safe_int(peak.get("tokens")), "cost": safe_float(peak.get("cost"))},
        "previous": previous,
        "changes": {
            "tokens": percent_change(totals["tokens"], previous["tokens"]),
            "requests": percent_change(totals["requests"], previous["requests"]),
            "cost": percent_change(totals["cost"], previous["cost"]),
        },
        "estimated": any(bool(row.get("estimated")) for row in rows),
    }


def build_accounts(
    history: dict[str, Any], start: date, end: date, source: str, account: str, model: str
) -> list[dict[str, Any]]:
    buckets: dict[tuple[str, str], dict[str, Any]] = {}
    source = source.upper()
    for current in iter_dates(start, end):
        day = history.get("days", {}).get(current.isoformat())
        if not day or (source not in {"", "ALL"} and day.get("source") != source):
            continue
        for provider in providers_with_residual(day):
            name = str(provider.get("name") or "Unassigned")
            if account and name != account:
                continue
            projected = scale_day_for_model({**provider, "date": current.isoformat(), "source": day.get("source")}, model) if model else provider
            if model and not safe_int(projected.get("tokens")):
                continue
            key = (str(day.get("source") or "LOCAL"), name)
            bucket = buckets.setdefault(
                key,
                {
                    "source": key[0],
                    "account_name": name,
                    "plan_type": str(provider.get("plan_type") or ""),
                    "requests": 0,
                    "tokens": 0,
                    "cost": 0.0,
                    "input_tokens": 0,
                    "cached_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "output_tokens": 0,
                    "active_days": 0,
                    "first_date": "",
                    "last_date": "",
                    "latest_at": "",
                    "models": {},
                    "estimated": False,
                },
            )
            for metric in METRIC_KEYS:
                if metric == "cost":
                    bucket[metric] += safe_float(projected.get(metric))
                else:
                    bucket[metric] += safe_int(projected.get(metric))
            if safe_int(projected.get("tokens")) or safe_int(projected.get("requests")) or safe_float(projected.get("cost")):
                bucket["active_days"] += 1
                bucket["first_date"] = bucket["first_date"] or current.isoformat()
                bucket["last_date"] = current.isoformat()
            latest = str(provider.get("latest_at") or "")
            if latest > bucket["latest_at"]:
                bucket["latest_at"] = latest
            for model_name, amount in provider.get("models", {}).items():
                bucket["models"][model_name] = bucket["models"].get(model_name, 0) + safe_int(amount)
            bucket["estimated"] = bucket["estimated"] or bool(projected.get("estimated"))

    result = list(buckets.values())
    for row in result:
        row["cost"] = round(row["cost"], 6)
        input_pool = row["input_tokens"] + row["cached_input_tokens"] + row["cache_creation_input_tokens"]
        row["cache_hit_rate"] = round(row["cached_input_tokens"] / max(1, input_pool) * 100, 2)
        row["tokens_per_request"] = round(row["tokens"] / max(1, row["requests"]), 2)
        row["average_tokens_per_active_day"] = round(row["tokens"] / max(1, row["active_days"]), 2)
        row["top_models"] = [
            {"name": name, "tokens": amount}
            for name, amount in sorted(row["models"].items(), key=lambda item: item[1], reverse=True)[:3]
        ]
        row.pop("models", None)
    result.sort(key=lambda item: (-safe_int(item.get("tokens")), -safe_float(item.get("cost")), item["account_name"]))
    return result


def build_models(rows: list[dict[str, Any]], account: str = "") -> list[dict[str, Any]]:
    buckets: dict[str, dict[str, Any]] = {}
    total_tokens = sum(safe_int(row.get("tokens")) for row in rows)
    for row in rows:
        source_day = row
        if account:
            providers = [provider for provider in providers_with_residual(row) if provider.get("name") == account]
            source_day = merge_provider_rows(providers, account)
        models = source_day.get("models", {})
        day_tokens = safe_int(source_day.get("tokens"))
        for name, amount in models.items():
            tokens = safe_int(amount)
            if tokens <= 0:
                continue
            bucket = buckets.setdefault(name, {"name": name, "tokens": 0, "cost": 0.0, "requests": 0, "active_days": 0, "sources": set()})
            ratio = tokens / max(1, day_tokens)
            bucket["tokens"] += tokens
            bucket["cost"] += safe_float(source_day.get("cost")) * ratio
            bucket["requests"] += int(round(safe_int(source_day.get("requests")) * ratio))
            bucket["active_days"] += 1
            bucket["sources"].add(str(row.get("source") or "LOCAL"))
    result = []
    for bucket in buckets.values():
        bucket["cost"] = round(bucket["cost"], 6)
        bucket["share"] = round(bucket["tokens"] / max(1, total_tokens) * 100, 2)
        bucket["sources"] = sorted(bucket["sources"])
        bucket["estimated_cost"] = True
        result.append(bucket)
    result.sort(key=lambda item: (-item["tokens"], item["name"]))
    return result


def build_source_mix(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if not row.get("filtered_out"):
            buckets[str(row.get("source") or "MISSING")].append(row)
    grand_total = sum(safe_int(row.get("tokens")) for row in rows)
    result = []
    for name, items in buckets.items():
        totals = sum_metrics(items)
        result.append({"source": name, **totals, "days": len(items), "share": round(totals["tokens"] / max(1, grand_total) * 100, 2)})
    result.sort(key=lambda item: -item["tokens"])
    return result


def build_periods(rows: list[dict[str, Any]], mode: str = "week") -> list[dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        parsed = parse_date(row.get("date"))
        if not parsed:
            continue
        if mode == "month":
            key = parsed.strftime("%Y-%m")
            label = parsed.strftime("%Y-%m")
        else:
            iso = parsed.isocalendar()
            key = f"{iso.year}-W{iso.week:02d}"
            label = f"W{iso.week:02d}"
        buckets[f"{key}|{label}"].append(row)
    result = []
    for composite, items in sorted(buckets.items()):
        key, label = composite.split("|", 1)
        totals = sum_metrics(items)
        result.append({"key": key, "label": label, **totals, "active_days": sum(1 for row in items if safe_int(row.get("tokens")) > 0)})
    return result


def day_quality(day: dict[str, Any]) -> dict[str, Any]:
    providers = day.get("providers", [])
    provider_tokens = sum(safe_int(item.get("tokens")) for item in providers)
    model_tokens = sum(safe_int(value) for value in day.get("models", {}).values())
    component_tokens = sum(safe_int(day.get(metric)) for metric in METRIC_KEYS[3:])
    total = safe_int(day.get("tokens"))

    def coverage(value: int) -> float:
        return round(min(value, total) / max(1, total) * 100, 2) if total else 100.0

    return {
        "provider_tokens": provider_tokens,
        "model_tokens": model_tokens,
        "component_tokens": component_tokens,
        "provider_coverage": coverage(provider_tokens),
        "model_coverage": coverage(model_tokens),
        "component_coverage": coverage(component_tokens),
        "provider_delta": provider_tokens - total,
        "model_delta": model_tokens - total,
        "component_delta": component_tokens - total,
        "has_details": bool(providers or day.get("models")),
    }


def build_quality(history: dict[str, Any], start: date, end: date, rows: list[dict[str, Any]]) -> dict[str, Any]:
    actual_days = [history.get("days", {}).get(current.isoformat()) for current in iter_dates(start, end)]
    actual_days = [row for row in actual_days if row]
    quality_rows = [day_quality(row) for row in actual_days]
    total_tokens = sum(safe_int(row.get("tokens")) for row in actual_days)

    def weighted(field: str) -> float:
        if not actual_days or total_tokens <= 0:
            return 0.0
        value = sum(min(safe_int(row.get("tokens")), safe_int(q.get(field.replace("coverage", "tokens")))) for row, q in zip(actual_days, quality_rows))
        return round(value / total_tokens * 100, 2)

    primary_days = sum(1 for row in actual_days if row.get("origin") == "primary")
    backfill_days = sum(1 for row in actual_days if row.get("origin") == "ccswitch_backfill")
    missing_days = len(rows) - len(actual_days)
    explicit_zero_days = sum(1 for row in actual_days if safe_int(row.get("tokens")) == 0)
    provider_tokens = sum(min(safe_int(row.get("tokens")), day_quality(row)["provider_tokens"]) for row in actual_days)
    model_tokens = sum(min(safe_int(row.get("tokens")), day_quality(row)["model_tokens"]) for row in actual_days)
    component_tokens = sum(min(safe_int(row.get("tokens")), day_quality(row)["component_tokens"]) for row in actual_days)
    detailed_days = sum(1 for quality in quality_rows if quality["has_details"])
    latest_updated = max((str(row.get("updated_at") or "") for row in actual_days), default="")
    return {
        "calendar_days": len(rows),
        "recorded_days": len(actual_days),
        "primary_days": primary_days,
        "backfill_days": backfill_days,
        "missing_days": missing_days,
        "explicit_zero_days": explicit_zero_days,
        "detailed_days": detailed_days,
        "day_coverage": round(len(actual_days) / max(1, len(rows)) * 100, 2),
        "provider_coverage": round(provider_tokens / max(1, total_tokens) * 100, 2),
        "model_coverage": round(model_tokens / max(1, total_tokens) * 100, 2),
        "component_coverage": round(component_tokens / max(1, total_tokens) * 100, 2),
        "last_updated_at": latest_updated,
        "ccswitch": history.get("ccswitch", {}),
    }


def build_anomalies(rows: list[dict[str, Any]], history: dict[str, Any]) -> list[dict[str, Any]]:
    anomalies: list[dict[str, Any]] = []
    positive_tokens = [safe_int(row.get("tokens")) for row in rows if safe_int(row.get("tokens")) > 0]
    global_median = statistics.median(positive_tokens) if positive_tokens else 0
    cost_rates = [safe_float(row.get("cost")) * 1_000_000 / safe_int(row.get("tokens")) for row in rows if safe_int(row.get("tokens")) > 0]
    median_rate = statistics.median(cost_rates) if cost_rates else 0
    previous_source = ""
    for index, row in enumerate(rows):
        tokens = safe_int(row.get("tokens"))
        if row.get("origin") == "missing":
            if rows and rows[0].get("date") != row.get("date") and rows[-1].get("date") != row.get("date"):
                anomalies.append({"type": "missing", "severity": "medium", "date": row.get("date"), "title": "历史缺口", "message": "该日期没有主历史或 ccSwitch 回填记录。"})
            continue
        trailing = [safe_int(item.get("tokens")) for item in rows[max(0, index - 7) : index] if safe_int(item.get("tokens")) > 0]
        baseline = statistics.median(trailing) if trailing else global_median
        if baseline > 0 and tokens >= baseline * 2.5 and tokens - baseline >= 1_000_000:
            anomalies.append({"type": "spike", "severity": "high", "date": row.get("date"), "title": "Token 突增", "message": f"较近期中位数高 {tokens / baseline:.1f} 倍。", "value": tokens, "baseline": baseline})
        elif baseline > 0 and 0 < tokens <= baseline * 0.25:
            anomalies.append({"type": "drop", "severity": "medium", "date": row.get("date"), "title": "Token 突降", "message": "低于近期中位数的 25%。", "value": tokens, "baseline": baseline})
        if tokens > 0 and median_rate > 0:
            rate = safe_float(row.get("cost")) * 1_000_000 / tokens
            if rate > median_rate * 2.2 and safe_float(row.get("cost")) > 1:
                anomalies.append({"type": "cost_rate", "severity": "medium", "date": row.get("date"), "title": "单位 Token 成本偏高", "message": f"${rate:.2f}/M，近期中位数 ${median_rate:.2f}/M。", "value": rate, "baseline": median_rate})
        original = history.get("days", {}).get(str(row.get("date")))
        if original:
            quality = day_quality(original)
            if safe_int(original.get("tokens")) > 0 and quality["provider_coverage"] < 70:
                anomalies.append({"type": "detail_gap", "severity": "low", "date": row.get("date"), "title": "账号明细不完整", "message": f"账号归属覆盖 {quality['provider_coverage']:.1f}%。"})
        source = str(row.get("source") or "")
        if previous_source and source and source != previous_source:
            anomalies.append({"type": "source_switch", "severity": "info", "date": row.get("date"), "title": "统计来源切换", "message": f"{previous_source} → {source}"})
        if source:
            previous_source = source
    severity_order = {"high": 0, "medium": 1, "low": 2, "info": 3}
    anomalies.sort(key=lambda item: (severity_order.get(item.get("severity"), 9), str(item.get("date") or "")), reverse=False)
    return anomalies[:120]


def build_options(history: dict[str, Any]) -> dict[str, Any]:
    accounts: set[str] = set()
    models: set[str] = set()
    sources: set[str] = set()
    for day in history.get("days", {}).values():
        sources.add(str(day.get("source") or "LOCAL"))
        models.update(str(name) for name in day.get("models", {}))
        accounts.update(str(provider.get("name") or "") for provider in providers_with_residual(day))
    return {
        "accounts": sorted(name for name in accounts if name),
        "models": sorted(name for name in models if name),
        "sources": sorted(name for name in sources if name),
    }


def build_dashboard(query: dict[str, list[str]], force: bool = False) -> dict[str, Any]:
    history = load_merged_history(force=force)
    start, end, range_key = resolve_bounds(query, history)
    source = str((query.get("source") or ["ALL"])[0]).strip().upper() or "ALL"
    account = str((query.get("account") or [""])[0]).strip()
    model = str((query.get("model") or [""])[0]).strip()
    rows = selected_days(history, start, end, source, account, model)
    span = max(1, (end - start).days + 1)
    previous_end = start - timedelta(days=1)
    previous_start = previous_end - timedelta(days=span - 1)
    previous_rows = selected_days(history, previous_start, previous_end, source, account, model)
    account_rows = build_accounts(history, start, end, source, account, model)
    model_rows = build_models(rows, account)
    composition = sum_metrics(rows)
    return {
        "meta": {
            "generated_at": now_local().isoformat(timespec="seconds"),
            "timezone": "Asia/Shanghai",
            "history_path": history.get("history_path"),
            "history_exists": history.get("history_exists"),
            "range": range_key,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "source": source,
            "account": account,
            "model": model,
            "ccswitch": history.get("ccswitch", {}),
        },
        "options": build_options(history),
        "summary": build_summary(rows, previous_rows),
        "daily": rows,
        "previous_daily": previous_rows,
        "accounts": account_rows,
        "models": model_rows,
        "sources": build_source_mix(rows),
        "composition": {key: composition[key] for key in METRIC_KEYS[3:]},
        "weekly": build_periods(rows, "week"),
        "monthly": build_periods(rows, "month"),
        "anomalies": build_anomalies(rows, history),
        "quality": build_quality(history, start, end, rows),
    }


def day_detail(day_key: str) -> dict[str, Any]:
    history = load_merged_history()
    day = history.get("days", {}).get(day_key)
    if not day:
        return {"date": day_key, "found": False}
    providers = providers_with_residual(day)
    models = [
        {"name": name, "tokens": tokens, "share": round(tokens / max(1, safe_int(day.get("tokens"))) * 100, 2)}
        for name, tokens in sorted(day.get("models", {}).items(), key=lambda item: item[1], reverse=True)
    ]
    return {"date": day_key, "found": True, "day": day, "providers": providers, "models": models, "quality": day_quality(day)}


def account_detail(name: str, query: dict[str, list[str]]) -> dict[str, Any]:
    query = dict(query)
    query["account"] = [name]
    dashboard = build_dashboard(query)
    return {
        "name": name,
        "meta": dashboard["meta"],
        "summary": dashboard["summary"],
        "daily": dashboard["daily"],
        "models": dashboard["models"],
        "accounts": dashboard["accounts"],
    }


def csv_bytes(rows: list[dict[str, Any]], columns: list[tuple[str, str]]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow([label for _key, label in columns])
    for row in rows:
        writer.writerow([row.get(key, "") for key, _label in columns])
    return ("\ufeff" + buffer.getvalue()).encode("utf-8")


class AnalyticsHandler(BaseHTTPRequestHandler):
    server_version = "TokenPulseAnalytics/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def send_bytes(self, payload: bytes, content_type: str, status: int = 200, filename: str = "") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(payload)))
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(payload)

    def send_json(self, payload: Any, status: int = 200, filename: str = "") -> None:
        self.send_bytes(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            "application/json; charset=utf-8",
            status,
            filename,
        )

    def do_GET(self) -> None:
        parsed = parse.urlparse(self.path)
        query = parse.parse_qs(parsed.query)
        try:
            if parsed.path in {"/", "/index.html"}:
                self.send_bytes((STATIC_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")
                return
            if parsed.path == "/favicon.ico":
                self.send_bytes(b"", "image/x-icon", HTTPStatus.NO_CONTENT)
                return
            if parsed.path == "/api/health":
                history = load_merged_history()
                self.send_json({"ok": True, "history_exists": history.get("history_exists"), "days": len(history.get("days", {})), "generated_at": now_local().isoformat(timespec="seconds")})
                return
            if parsed.path == "/api/dashboard":
                self.send_json(build_dashboard(query))
                return
            if parsed.path == "/api/day":
                self.send_json(day_detail(str((query.get("date") or [""])[0])))
                return
            if parsed.path == "/api/account":
                self.send_json(account_detail(str((query.get("name") or [""])[0]), query))
                return
            if parsed.path in {"/api/export.csv", "/api/export.json"}:
                dashboard = build_dashboard(query)
                kind = str((query.get("kind") or ["daily"])[0]).lower()
                rows = dashboard.get(kind) if kind in {"accounts", "models"} else dashboard.get("daily")
                rows = rows if isinstance(rows, list) else []
                if parsed.path.endswith(".json"):
                    self.send_json({"meta": dashboard["meta"], "kind": kind, "rows": rows}, filename=f"token-pulse-{kind}.json")
                    return
                if kind == "accounts":
                    columns = [("source", "来源"), ("account_name", "账号"), ("requests", "请求"), ("tokens", "Token"), ("cost", "成本USD"), ("active_days", "活跃天"), ("cache_hit_rate", "缓存命中率%"), ("first_date", "首次日期"), ("last_date", "最后日期")]
                elif kind == "models":
                    columns = [("name", "模型"), ("requests", "估算请求"), ("tokens", "Token"), ("cost", "估算成本USD"), ("share", "占比%"), ("active_days", "活跃天")]
                else:
                    columns = [("date", "日期"), ("source", "来源"), ("requests", "请求"), ("tokens", "Token"), ("cost", "成本USD"), ("input_tokens", "输入Token"), ("cached_input_tokens", "缓存读取Token"), ("cache_creation_input_tokens", "缓存创建Token"), ("output_tokens", "输出Token"), ("origin", "数据来源状态")]
                self.send_bytes(csv_bytes(rows, columns), "text/csv; charset=utf-8", filename=f"token-pulse-{kind}.csv")
                return
            self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self) -> None:
        parsed = parse.urlparse(self.path)
        if parsed.path != "/api/refresh":
            self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return
        try:
            load_merged_history(force=True)
            self.send_json({"ok": True, "generated_at": now_local().isoformat(timespec="seconds")})
        except Exception as exc:
            self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)


def main() -> int:
    parser = argparse.ArgumentParser(description="Token Pulse local usage analytics dashboard")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--open", action="store_true", dest="open_browser")
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), AnalyticsHandler)
    url = f"http://{args.host}:{args.port}"
    print(f"Token Pulse Analytics: {url}")
    print(f"History: {USAGE_HISTORY_JSON}")
    if args.open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
