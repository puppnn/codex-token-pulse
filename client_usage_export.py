from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sqlite3
from bisect import bisect_left, bisect_right
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib import request


def env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def load_cached_account_30d_windows(
    path: Path,
    now: datetime,
) -> tuple[bool, str, dict[str, dict[str, Any]]]:
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False, "", {}
    updated_text = str(existing.get("account_30d_updated_at") or "")
    updated_at = parse_dt(updated_text)
    if updated_at is None:
        return False, "", {}
    age_seconds = (now - updated_at).total_seconds()
    if age_seconds < 0 or age_seconds >= ACCOUNT_30D_CACHE_SECONDS:
        return False, "", {}
    windows = {
        str(provider.get("name") or ""): dict(provider["window_30d"])
        for provider in existing.get("providers") or []
        if isinstance(provider, dict)
        and provider.get("name")
        and isinstance(provider.get("window_30d"), dict)
    }
    return True, updated_text, windows


APP_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT = APP_DIR / "client_usage_today.json"
CONFIG_PATH = Path(os.environ.get("CLIENT_USAGE_CONFIG") or APP_DIR / "client_usage_config.json")
SPEED_HISTORY_PATH = Path(os.environ.get("CLIENT_USAGE_SPEED_HISTORY") or APP_DIR / "client_usage_speed_history.json")
ACCOUNT_TIMELINE_PATH = Path(os.environ.get("CLIENT_USAGE_ACCOUNT_TIMELINE") or APP_DIR / "client_usage_account_timeline.json")
AUTH_SWITCH_EVENTS_PATH = Path(
    os.environ.get("CLIENT_USAGE_AUTH_SWITCH_EVENTS")
    or APP_DIR / "client_usage_auth_switch_events.jsonl"
)
ATTRIBUTION_LEDGER_PATH = Path(os.environ.get("CLIENT_USAGE_ATTRIBUTION_LEDGER") or APP_DIR / "client_usage_attribution_ledger.json")
USAGE_HISTORY_PATH = Path(os.environ.get("USAGE_HISTORY_JSON") or APP_DIR / "usage_history.json")
MODEL_PRICE_CACHE_PATH = Path(
    os.environ.get("CLIENT_USAGE_MODEL_PRICE_CACHE") or APP_DIR / "client_usage_model_prices.json"
)
MODEL_PRICE_SOURCE_URL = os.environ.get(
    "CLIENT_USAGE_MODEL_PRICE_URL",
    "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json",
)
MODEL_PRICE_CACHE_SECONDS = int(os.environ.get("CLIENT_USAGE_MODEL_PRICE_CACHE_SECONDS", "86400"))
MODEL_PRICE_FETCH_TIMEOUT_SECONDS = float(os.environ.get("CLIENT_USAGE_MODEL_PRICE_FETCH_TIMEOUT_SECONDS", "4"))
COCKPIT_OFFICIAL_QUOTA_CACHE_PATH = Path(
    os.environ.get("CLIENT_USAGE_OFFICIAL_QUOTA_CACHE")
    or APP_DIR / "client_usage_official_quota_cache.json"
)
COCKPIT_OFFICIAL_QUOTA_URL = os.environ.get(
    "CLIENT_USAGE_OFFICIAL_QUOTA_URL",
    "https://chatgpt.com/backend-api/wham/usage",
)
COCKPIT_OFFICIAL_QUOTA_CACHE_SECONDS = max(
    60,
    int(os.environ.get("CLIENT_USAGE_OFFICIAL_QUOTA_CACHE_SECONDS", "600")),
)
COCKPIT_OFFICIAL_QUOTA_TIMEOUT_SECONDS = max(
    1.0,
    env_float("CLIENT_USAGE_OFFICIAL_QUOTA_TIMEOUT_SECONDS", 4.0),
)
COCKPIT_OFFICIAL_QUOTA_MAX_WORKERS = max(
    1,
    min(8, int(os.environ.get("CLIENT_USAGE_OFFICIAL_QUOTA_MAX_WORKERS", "4"))),
)
COCKPIT_OFFICIAL_QUOTA_ENABLED = os.environ.get(
    "CLIENT_USAGE_OFFICIAL_QUOTA_REFRESH",
    "1",
).strip().lower() not in {"0", "false", "no", "off"}
CODEX_DEFAULT_MODEL = os.environ.get("CLIENT_USAGE_CODEX_DEFAULT_MODEL", "gpt-5.5")
MAX_SINGLE_EVENT_TOKENS = int(os.environ.get("CLIENT_USAGE_MAX_SINGLE_EVENT_TOKENS", "2000000"))
CODEX_ACCOUNT_MATCH_WINDOW_SECONDS = int(os.environ.get("CLIENT_USAGE_CODEX_ACCOUNT_MATCH_WINDOW_SECONDS", "600"))
API_SERVICE_ACTIVITY_MATCH_SECONDS = float(os.environ.get("CLIENT_USAGE_API_ACTIVITY_MATCH_SECONDS", "300"))
CODEX_CURRENT_ACCOUNT_RECENT_SECONDS = int(os.environ.get("CLIENT_USAGE_CURRENT_ACCOUNT_RECENT_SECONDS", "1800"))
CLIENT_USAGE_ACTIVE_WINDOW_SECONDS = int(os.environ.get("CLIENT_USAGE_ACTIVE_WINDOW_SECONDS", "60"))
CLIENT_USAGE_ACTIVE_TASK_STALE_SECONDS = int(
    os.environ.get("CLIENT_USAGE_ACTIVE_TASK_STALE_SECONDS", "7200")
)
ACCOUNT_30D_CACHE_SECONDS = int(os.environ.get("CLIENT_USAGE_ACCOUNT_30D_CACHE_SECONDS", "300"))
QUOTA_WINDOW_START_TOLERANCE_SECONDS = int(os.environ.get("CLIENT_USAGE_QUOTA_WINDOW_START_TOLERANCE_SECONDS", "10"))
COCKPIT_QUOTA_RESERVE_STALE_SECONDS = int(
    os.environ.get("CLIENT_USAGE_COCKPIT_QUOTA_RESERVE_STALE_SECONDS", "1800")
)
LATEST_REQUEST_LOOKBACK_DAYS = int(os.environ.get("CLIENT_USAGE_LATEST_REQUEST_LOOKBACK_DAYS", "7"))
OFFLINE_HISTORY_BACKFILL_MAX_DAYS = max(
    0,
    int(os.environ.get("CLIENT_USAGE_OFFLINE_BACKFILL_MAX_DAYS", "31")),
)
UNASSIGNED_CODEX_LABEL = os.environ.get("CLIENT_USAGE_UNASSIGNED_CODEX_LABEL", "Unassigned local")
CODEX_FAST_COST_MULTIPLIER = env_float("CLIENT_USAGE_CODEX_FAST_COST_MULTIPLIER", 2.0)
CODEX_FORCE_SPEED = os.environ.get("CLIENT_USAGE_CODEX_FORCE_SPEED", "").strip().lower()
CODEX_SPEED_OVERRIDES = os.environ.get("CLIENT_USAGE_CODEX_SPEED_OVERRIDES", "").strip()
LOCAL_TZ = timezone(timedelta(hours=8))
JSON_DECODER = json.JSONDecoder()
LOG_FIELD_RE = re.compile(r'(?<![A-Za-z0-9_.-])(?P<key>[A-Za-z0-9_.-]+)=(?P<value>"[^"]*"|\S+)')
DESKTOP_LOG_LINE_RE = re.compile(r"^(?P<timestamp>\S+)\s+\S+\s+(?P<body>.*)$")
DESKTOP_NETWORK_ERROR_CODES = (
    "net::ERR_CONNECTION_CLOSED",
    "net::ERR_CONNECTION_RESET",
    "net::ERR_INTERNET_DISCONNECTED",
    "net::ERR_NETWORK_CHANGED",
    "net::ERR_TIMED_OUT",
)
DESKTOP_NETWORK_FAILURE_MIN_COUNT = 3
DESKTOP_NETWORK_FAILURE_CLUSTER_GAP = timedelta(minutes=5)
INTERNAL_SERVICE_TIER_RE = re.compile(
    r'service_tier:\s*Some\((?:Some\()?\"(?P<tier>[^\"]+)\"'
)
PROMPT_CACHE_KEY_RE = re.compile(r'prompt_cache_key:\s*Some\(\"(?P<key>[^\"]+)\"\)')
JSON_PROMPT_CACHE_KEY_RE = re.compile(r'"prompt_cache_key"\s*:\s*"(?P<key>[^"]+)"')
THREAD_ID_RE = re.compile(r'\bthread\.id=(?P<key>[A-Za-z0-9_-]+)')
TURN_ID_RE = re.compile(r'\b(?:turn\.id|turn_id)=(?P<key>[A-Za-z0-9_-]+)')
CONVERSATION_ID_RE = re.compile(r'\bconversation\.id=(?P<key>[A-Za-z0-9_-]+)')
SESSION_LOOP_THREAD_ID_RE = re.compile(r'\bsession_loop\{thread_id=(?P<key>[A-Za-z0-9_-]+)\}')
CODEX_SESSION_FILE_ID_RE = re.compile(
    r"(?P<id>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$",
    re.IGNORECASE,
)
SUB2API_ROUTED_CODEX_LABEL = os.environ.get("CLIENT_USAGE_SUB2API_ROUTED_CODEX_LABEL", "Codex via Sub2API")
HIGH_WATER_UNATTRIBUTED_LABEL = os.environ.get(
    "CLIENT_USAGE_HIGH_WATER_UNATTRIBUTED_LABEL",
    "Codex local - 历史高水位未归因",
)
API_SERVICE_MIRROR_LABELS = {
    "api-service-local",
    "api service local",
    "codex_local_access_runtime",
}
if "CLIENT_USAGE_HIGH_WATER_UNATTRIBUTED_LABEL" not in os.environ:
    HIGH_WATER_UNATTRIBUTED_LABEL = "Codex local - \u5386\u53f2\u7f3a\u53e3\u672a\u5f52\u5c5e"
API_SERVICE_AGGREGATE_LABEL = "Codex local - api-service-local"

_ONLINE_PRICE_TABLE: dict[str, tuple[float, float, float]] | None = None
_ONLINE_PRICE_DETAILS: dict[str, dict[str, float]] | None = None


@dataclass
class UsageBucket:
    requests: int = 0
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    cost: float = 0.0
    models: dict[str, int] = field(default_factory=dict)
    latest_at: datetime | None = None
    latest_model: str = ""
    latest_app_speed: str = ""
    latest_cost_multiplier: float | None = None
    latest_speed_badge: str = ""

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.cached_input_tokens
            + self.output_tokens
            + self.cache_creation_input_tokens
            + self.cache_read_input_tokens
        )

    def add_model(self, model: str, tokens: int) -> None:
        model = (model or "unknown").strip() or "unknown"
        self.models[model] = self.models.get(model, 0) + max(0, int(tokens or 0))

    def mark_latest(
        self,
        when: datetime | None,
        model: str,
        app_speed: str = "",
        cost_multiplier: float | None = None,
    ) -> None:
        if when is None:
            return
        if self.latest_at is None or when > self.latest_at:
            self.latest_at = when
            self.latest_model = (model or "unknown").strip() or "unknown"
            normalized_speed = normalize_codex_speed(app_speed)
            self.latest_app_speed = normalized_speed
            self.latest_cost_multiplier = cost_multiplier
            self.latest_speed_badge = speed_badge(cost_multiplier)


@dataclass
class UsageEvent:
    when: datetime
    model: str
    input_tokens: int
    cached_tokens: int
    output_tokens: int
    app_speed: str = ""
    cost_multiplier: float | None = None
    pricing_tier: str = ""
    session_id: str = ""
    request_key: str = ""
    route: str = ""
    request_at: datetime | None = None
    account_at: datetime | None = None

    @property
    def total_tokens(self) -> int:
        return max(0, self.input_tokens) + max(0, self.cached_tokens) + max(0, self.output_tokens)


def live_usage_event_id(event: UsageEvent) -> str:
    when = event.when
    aware = when if when.tzinfo is not None else when.replace(tzinfo=LOCAL_TZ)
    timestamp_us = int(round(aware.timestamp() * 1_000_000))
    raw_input = max(0, event.input_tokens) + max(0, event.cached_tokens)
    return "|".join(
        (
            str(event.session_id or ""),
            str(timestamp_us),
            str(raw_input),
            str(max(0, event.cached_tokens)),
            str(max(0, event.output_tokens)),
        )
    )


@dataclass
class SessionLifecycle:
    session_id: str
    state: str
    when: datetime
    turn_id: str = ""
    file_activity_at: datetime | None = None


@dataclass
class CodexFailureEvent:
    when: datetime
    session_id: str = ""
    turn_id: str = ""
    kind: str = "task"


@dataclass
class AccountMarker:
    when: datetime
    label: str
    model: str = ""
    kind: str = "request"
    total_tokens: int = 0
    input_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0
    event_key: str = ""


@dataclass
class SpeedMarker:
    when: datetime
    speed: str


@dataclass
class RouteMarker:
    when: datetime
    route: str
    session_id: str = ""
    request_key: str = ""


PRICE_PER_MILLION: list[tuple[str, tuple[float, float, float]]] = [
    ("gpt-5.5", (5.0, 0.5, 30.0)),
    ("gpt-5.4-mini", (0.75, 0.075, 4.5)),
    ("gpt-5.4", (2.5, 0.25, 15.0)),
    ("gpt-5.3", (2.5, 0.25, 15.0)),
    ("gpt-5.2", (2.5, 0.25, 15.0)),
    ("opus", (15.0, 1.5, 75.0)),
    ("sonnet", (3.0, 0.3, 15.0)),
    ("haiku", (0.8, 0.08, 4.0)),
]


ONLINE_TOKEN_COST_FIELDS = (
    "input_cost_per_token",
    "input_cost_per_token_above_272k_tokens",
    "input_cost_per_token_batches",
    "input_cost_per_token_flex",
    "input_cost_per_token_priority",
    "input_cost_per_token_above_272k_tokens_priority",
    "cache_read_input_token_cost",
    "cache_read_input_token_cost_above_272k_tokens",
    "cache_read_input_token_cost_flex",
    "cache_read_input_token_cost_priority",
    "cache_read_input_token_cost_above_272k_tokens_priority",
    "cache_creation_input_token_cost",
    "cache_creation_input_token_cost_above_272k_tokens",
    "cache_creation_input_token_cost_flex",
    "cache_creation_input_token_cost_priority",
    "cache_creation_input_token_cost_above_272k_tokens_priority",
    "output_cost_per_token",
    "output_cost_per_token_above_272k_tokens",
    "output_cost_per_token_batches",
    "output_cost_per_token_flex",
    "output_cost_per_token_priority",
    "output_cost_per_token_above_272k_tokens_priority",
)


def extract_online_price_details(payload: Any) -> dict[str, dict[str, float]]:
    if not isinstance(payload, dict):
        return {}
    prices: dict[str, dict[str, float]] = {}
    for raw_name, row in payload.items():
        if not isinstance(row, dict):
            continue
        provider = str(row.get("litellm_provider") or "").strip().lower()
        if provider and provider not in {"openai", "anthropic"}:
            continue
        detail: dict[str, float] = {}
        for field_name in ONLINE_TOKEN_COST_FIELDS:
            try:
                value = float(row.get(field_name) or 0) * 1_000_000
            except (TypeError, ValueError):
                value = 0.0
            if value > 0:
                detail[field_name] = value
        if detail.get("input_cost_per_token", 0) <= 0 or detail.get("output_cost_per_token", 0) <= 0:
            continue
        name = str(raw_name or "").strip().lower()
        if not name:
            continue
        prices[name] = detail
        if name.startswith(("openai/", "anthropic/")):
            prices.setdefault(name.split("/", 1)[1], detail)
    return prices


def extract_online_price_table(payload: Any) -> dict[str, tuple[float, float, float]]:
    return {
        name: (
            detail["input_cost_per_token"],
            detail.get("cache_read_input_token_cost", detail["input_cost_per_token"]),
            detail["output_cost_per_token"],
        )
        for name, detail in extract_online_price_details(payload).items()
    }


def load_online_price_table() -> dict[str, tuple[float, float, float]]:
    global _ONLINE_PRICE_TABLE, _ONLINE_PRICE_DETAILS
    if _ONLINE_PRICE_TABLE is not None and _ONLINE_PRICE_DETAILS is not None:
        return _ONLINE_PRICE_TABLE

    cached: dict[str, Any] = {}
    try:
        cached = json.loads(MODEL_PRICE_CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        cached = {}
    cached_details = extract_online_price_details(cached.get("models"))
    cached_prices = {
        name: (
            detail["input_cost_per_token"],
            detail.get("cache_read_input_token_cost", detail["input_cost_per_token"]),
            detail["output_cost_per_token"],
        )
        for name, detail in cached_details.items()
    }
    try:
        fetched_at = float(cached.get("fetched_at") or 0)
    except (TypeError, ValueError):
        fetched_at = 0.0
    now_timestamp = datetime.now().timestamp()
    if int(cached.get("schema") or 0) >= 2 and cached_prices and 0 <= now_timestamp - fetched_at < MODEL_PRICE_CACHE_SECONDS:
        _ONLINE_PRICE_DETAILS = cached_details
        _ONLINE_PRICE_TABLE = cached_prices
        return _ONLINE_PRICE_TABLE

    try:
        req = request.Request(
            MODEL_PRICE_SOURCE_URL,
            headers={"User-Agent": "token-floating-monitor/1.0"},
        )
        with request.urlopen(req, timeout=MODEL_PRICE_FETCH_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
        online_details = extract_online_price_details(payload)
        if online_details:
            online_prices = {
                name: (
                    detail["input_cost_per_token"],
                    detail.get("cache_read_input_token_cost", detail["input_cost_per_token"]),
                    detail["output_cost_per_token"],
                )
                for name, detail in online_details.items()
            }
            cache_models = {
                name: {field_name: value / 1_000_000 for field_name, value in detail.items()}
                for name, detail in online_details.items()
            }
            write_json_atomic(
                MODEL_PRICE_CACHE_PATH,
                {
                    "schema": 2,
                    "source": MODEL_PRICE_SOURCE_URL,
                    "fetched_at": now_timestamp,
                    "models": cache_models,
                },
            )
            _ONLINE_PRICE_DETAILS = online_details
            _ONLINE_PRICE_TABLE = online_prices
            return _ONLINE_PRICE_TABLE
    except (OSError, ValueError, json.JSONDecodeError):
        pass

    _ONLINE_PRICE_DETAILS = cached_details
    _ONLINE_PRICE_TABLE = cached_prices
    return _ONLINE_PRICE_TABLE


def online_model_price(model: str) -> tuple[float, float, float] | None:
    name = (model or "").strip().lower()
    if not name:
        return None
    prices = load_online_price_table()
    for candidate in (name, name.split("/", 1)[-1]):
        price = prices.get(candidate)
        if price:
            return price
    return None


def online_model_price_details(model: str) -> dict[str, float] | None:
    name = (model or "").strip().lower()
    if not name:
        return None
    load_online_price_table()
    details = _ONLINE_PRICE_DETAILS or {}
    for candidate in (name, name.split("/", 1)[-1]):
        detail = details.get(candidate)
        if detail:
            return detail
    return None


def model_price(model: str) -> tuple[float, float, float]:
    name = (model or "").lower()
    for needle, price in PRICE_PER_MILLION:
        if needle in name:
            return price
    online_price = online_model_price(name)
    if online_price is not None:
        return online_price
    if re.search(r"\bgpt-5(?:\.|\b)", name):
        return next(price for needle, price in PRICE_PER_MILLION if needle == "gpt-5.5")
    return (0.0, 0.0, 0.0)


def local_model_price_details(model: str) -> dict[str, float]:
    input_price, cache_price, output_price = model_price(model)
    multiplier = max(1.0, CODEX_FAST_COST_MULTIPLIER)
    return {
        "input_cost_per_token": input_price,
        "cache_read_input_token_cost": cache_price,
        "cache_creation_input_token_cost": input_price,
        "output_cost_per_token": output_price,
        "input_cost_per_token_priority": input_price * multiplier,
        "cache_read_input_token_cost_priority": cache_price * multiplier,
        "cache_creation_input_token_cost_priority": input_price * multiplier,
        "output_cost_per_token_priority": output_price * multiplier,
    }


def normalize_pricing_tier(value: Any) -> str:
    tier = str(value or "").strip().lower()
    if tier in {"priority", "fast", "quick", "turbo"}:
        return "priority"
    if tier in {"flex", "batch", "batches"}:
        return "batch" if tier in {"batch", "batches"} else "flex"
    return "standard"


def token_price_for_request(profile: dict[str, float], base_field: str, tier: str) -> float:
    tier_suffix = {"priority": "priority", "flex": "flex", "batch": "batches"}.get(tier, "")
    if tier_suffix:
        tier_price = profile.get(f"{base_field}_{tier_suffix}")
        if tier_price:
            return tier_price
    return float(profile.get(base_field) or 0)


def estimate_cost(
    model: str,
    input_tokens: int,
    cached_tokens: int,
    output_tokens: int,
    cache_creation_tokens: int = 0,
    pricing_tier: str = "standard",
) -> float:
    profile = online_model_price_details(model) or local_model_price_details(model)
    tier = normalize_pricing_tier(pricing_tier)
    input_count = max(0, input_tokens)
    cached_count = max(0, cached_tokens)
    cache_creation_count = max(0, cache_creation_tokens)
    output_count = max(0, output_tokens)
    input_price = token_price_for_request(profile, "input_cost_per_token", tier)
    cache_price = token_price_for_request(profile, "cache_read_input_token_cost", tier)
    cache_creation_price = token_price_for_request(profile, "cache_creation_input_token_cost", tier)
    output_price = token_price_for_request(profile, "output_cost_per_token", tier)
    if cache_price <= 0:
        cache_price = input_price
    if cache_creation_price <= 0:
        cache_creation_price = input_price
    return (
        input_count * input_price
        + cached_count * cache_price
        + cache_creation_count * cache_creation_price
        + output_count * output_price
    ) / 1_000_000


def codex_speed_cost_multiplier(speed: str) -> float:
    normalized = (speed or "").strip().lower()
    if normalized in {"fast", "quick", "turbo"}:
        return max(1.0, CODEX_FAST_COST_MULTIPLIER)
    return 1.0


def speed_badge(cost_multiplier: float | None) -> str:
    try:
        multiplier = float(cost_multiplier or 1.0)
    except (TypeError, ValueError):
        multiplier = 1.0
    return f"FAST x{multiplier:g}" if multiplier > 1 else ""


def codex_service_tier_to_speed(service_tier: Any) -> str:
    tier = str(service_tier or "").strip().lower()
    if tier in {"priority", "fast"}:
        return "fast"
    if tier in {"flex", "batch", "batches"}:
        return "batch" if tier in {"batch", "batches"} else "flex"
    if tier in {"standard"}:
        return "standard"
    if tier in {"default", "auto", "none", "null", ""}:
        return ""
    return ""


def codex_internal_service_tier(text: str) -> str:
    match = INTERNAL_SERVICE_TIER_RE.search(text or "")
    if not match:
        return ""
    return normalize_pricing_tier(match.group("tier"))


def codex_internal_service_tier_speed(text: str) -> str:
    return codex_service_tier_to_speed(codex_internal_service_tier(text))


def codex_log_request_key(text: str, response: dict[str, Any] | None = None) -> str:
    if response:
        key = str(response.get("prompt_cache_key") or "").strip()
        if key:
            return key
    for pattern in (PROMPT_CACHE_KEY_RE, JSON_PROMPT_CACHE_KEY_RE, CONVERSATION_ID_RE, THREAD_ID_RE):
        match = pattern.search(text or "")
        if match:
            return match.group("key").strip()
    return ""


def codex_log_ids(text: str, response: dict[str, Any] | None = None) -> list[str]:
    keys: list[str] = []
    if response:
        for value in (response.get("conversation_id"), response.get("thread_id"), response.get("id")):
            key = str(value or "").strip()
            if key and key not in keys:
                keys.append(key)
    for pattern in (CONVERSATION_ID_RE, THREAD_ID_RE, TURN_ID_RE, SESSION_LOOP_THREAD_ID_RE, PROMPT_CACHE_KEY_RE, JSON_PROMPT_CACHE_KEY_RE):
        for match in pattern.finditer(text or ""):
            key = match.group("key").strip()
            if key and key not in keys:
                keys.append(key)
    return keys


def detect_codex_route(text: str) -> str:
    lowered = (text or "").lower()
    if not lowered:
        return ""
    if (
        "127.0.0.1:8080/v1/responses" in lowered
        or "localhost:8080/v1/responses" in lowered
        or "[::1]:8080/v1/responses" in lowered
    ):
        return "sub2api"
    if "chatgpt.com/backend-api/codex" in lowered or "responses_websocket" in lowered:
        return "official"
    return ""


def codex_model_name(model: str) -> str:
    name = (model or "").strip()
    if not name or name.lower() in {"codex", "unknown"}:
        return CODEX_DEFAULT_MODEL
    return name


NON_TURN_ERROR_KINDS = {"active_turn_not_steerable", "thread_rollback_failed"}


def codex_error_kind(error: Any) -> str:
    if not isinstance(error, dict):
        return ""
    info = error.get("codex_error_info")
    if isinstance(info, str):
        return re.sub(r"[^a-z0-9]+", "_", info.lower()).strip("_")
    if not isinstance(info, dict):
        return ""
    for key in ("type", "kind", "code"):
        value = info.get(key)
        if isinstance(value, str) and value.strip():
            return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    if len(info) == 1:
        value = str(next(iter(info))).strip()
        return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return ""


def codex_error_affects_turn(error: Any) -> bool:
    if not error:
        return False
    return codex_error_kind(error) not in NON_TURN_ERROR_KINDS


def usage_int(usage: dict[str, Any], key: str) -> int:
    try:
        return int(usage.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def add_codex_usage(
    bucket: UsageBucket,
    model: str,
    input_tokens: int,
    cached_tokens: int,
    output_tokens: int,
    when: datetime | None = None,
    cost_multiplier: float = 1.0,
) -> None:
    uncached_input = max(0, input_tokens - max(0, cached_tokens))
    cached_input = max(0, cached_tokens)
    output = max(0, output_tokens)
    total = uncached_input + cached_input + output
    if total <= 0 or total > MAX_SINGLE_EVENT_TOKENS:
        return
    bucket.requests += 1
    bucket.input_tokens += uncached_input
    bucket.cached_input_tokens += cached_input
    bucket.output_tokens += output
    multiplier = max(1.0, cost_multiplier)
    pricing_tier = "priority" if multiplier > 1 else "standard"
    bucket.cost += estimate_cost(model, uncached_input, cached_input, output, pricing_tier=pricing_tier)
    bucket.add_model(model, total)
    bucket.mark_latest(when, model, "fast" if multiplier > 1 else "", multiplier)


def make_codex_event(
    model: str,
    input_tokens: int,
    cached_tokens: int,
    output_tokens: int,
    when: datetime | None,
    app_speed: str = "",
    cost_multiplier: float | None = None,
    session_id: str = "",
    request_key: str = "",
    route: str = "",
    request_at: datetime | None = None,
    account_at: datetime | None = None,
    pricing_tier: str = "",
) -> UsageEvent | None:
    if when is None:
        return None
    uncached_input = max(0, input_tokens - max(0, cached_tokens))
    cached_input = max(0, cached_tokens)
    output = max(0, output_tokens)
    total = uncached_input + cached_input + output
    if total <= 0 or total > MAX_SINGLE_EVENT_TOKENS:
        return None
    return UsageEvent(
        when=when,
        model=codex_model_name(model),
        input_tokens=uncached_input,
        cached_tokens=cached_input,
        output_tokens=output,
        app_speed=normalize_codex_speed(app_speed),
        cost_multiplier=cost_multiplier,
        pricing_tier=normalize_pricing_tier(pricing_tier or app_speed),
        session_id=str(session_id or "").strip(),
        request_key=str(request_key or "").strip(),
        route=str(route or "").strip().lower(),
        request_at=request_at,
        account_at=account_at,
    )


def add_codex_event_to_bucket(
    bucket: UsageBucket,
    event: UsageEvent,
    cost_multiplier: float = 1.0,
    bucket_time: datetime | None = None,
) -> None:
    effective_multiplier = event.cost_multiplier if event.cost_multiplier is not None else cost_multiplier
    effective_multiplier = max(1.0, float(effective_multiplier or 1.0))
    bucket.requests += 1
    bucket.input_tokens += event.input_tokens
    bucket.cached_input_tokens += event.cached_tokens
    bucket.output_tokens += event.output_tokens
    pricing_tier = event.pricing_tier or ("priority" if effective_multiplier > 1 else "standard")
    bucket.cost += estimate_cost(
        event.model,
        event.input_tokens,
        event.cached_tokens,
        event.output_tokens,
        pricing_tier=pricing_tier,
    )
    bucket.add_model(event.model, event.total_tokens)
    event_speed = event.app_speed or ("fast" if effective_multiplier > 1 else "")
    bucket.mark_latest(bucket_time or event.when, event.model, event_speed, effective_multiplier)


def add_bucket(target: UsageBucket, source: UsageBucket) -> None:
    target.requests += source.requests
    target.input_tokens += source.input_tokens
    target.cached_input_tokens += source.cached_input_tokens
    target.output_tokens += source.output_tokens
    target.cache_creation_input_tokens += source.cache_creation_input_tokens
    target.cache_read_input_tokens += source.cache_read_input_tokens
    target.cost += source.cost
    for model, tokens in source.models.items():
        target.models[model] = target.models.get(model, 0) + tokens
    target.mark_latest(
        source.latest_at,
        source.latest_model,
        source.latest_app_speed,
        source.latest_cost_multiplier,
    )


def parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is not None:
            dt = dt.astimezone(LOCAL_TZ).replace(tzinfo=None)
        return dt
    except Exception:
        return None


def epoch_to_local_datetime(value: Any) -> datetime | None:
    try:
        seconds = int(value or 0)
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    try:
        return datetime.fromtimestamp(seconds, tz=LOCAL_TZ).replace(tzinfo=None)
    except (OSError, OverflowError, ValueError):
        return None


def parse_json_after_marker(text: str, marker: str) -> dict[str, Any] | None:
    pos = text.find(marker)
    if pos < 0:
        return None
    payload = text[pos + len(marker):].lstrip()
    try:
        value, _ = JSON_DECODER.raw_decode(payload)
    except json.JSONDecodeError:
        return None
    if isinstance(value, dict):
        return value
    return None


def parse_log_fields(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for match in LOG_FIELD_RE.finditer(text):
        value = match.group("value")
        if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
            value = value[1:-1]
        fields[match.group("key")] = value
    return fields


def field_int(fields: dict[str, str], key: str) -> int:
    try:
        return int(fields.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def codex_event_from_log_fields(text: str, ts: Any) -> UsageEvent | None:
    if "event.kind=response.completed" not in text or "input_token_count=" not in text:
        return None
    fields = parse_log_fields(text)
    input_tokens = field_int(fields, "input_token_count")
    cached_tokens = field_int(fields, "cached_token_count")
    output_tokens = field_int(fields, "output_token_count")
    if input_tokens <= 0 and cached_tokens <= 0 and output_tokens <= 0:
        return None
    when = parse_dt(fields.get("event.timestamp")) or epoch_to_local_datetime(ts)
    pricing_tier = normalize_pricing_tier(fields.get("service_tier"))
    app_speed = codex_service_tier_to_speed(pricing_tier)
    multiplier = codex_speed_cost_multiplier(app_speed) if app_speed else None
    request_key = codex_log_request_key(text)
    ids = codex_log_ids(text)
    session_id = ids[0] if ids else request_key
    return make_codex_event(
        fields.get("slug") or fields.get("model") or CODEX_DEFAULT_MODEL,
        input_tokens,
        cached_tokens,
        output_tokens,
        when,
        app_speed,
        multiplier,
        session_id=session_id,
        request_key=request_key or session_id,
        route=detect_codex_route(text),
        request_at=when,
        pricing_tier=pricing_tier,
    )


def codex_session_header(lines: list[str]) -> tuple[str, str]:
    for line in lines[:20]:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("type") != "session_meta":
            continue
        payload = row.get("payload") or {}
        return (
            str(payload.get("id") or payload.get("session_id") or "").strip(),
            str(payload.get("forked_from_id") or "").strip(),
        )
    return "", ""


def codex_fork_replay_cutoff(lines: list[str]) -> datetime | None:
    _session_id, parent_id = codex_session_header(lines)
    if not parent_id:
        return None
    for line in lines[:20]:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("type") == "session_meta":
            started = parse_dt(row.get("timestamp"))
            return started + timedelta(seconds=2) if started is not None else None
    return None


CODEX_TOKEN_USAGE_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)


def codex_token_count_signature(payload: dict[str, Any]) -> tuple[int, ...]:
    info = payload.get("info") or {}
    total = info.get("total_token_usage") or {}
    last = info.get("last_token_usage") or {}
    return (
        int(bool(info.get("last_token_usage"))),
        *(usage_int(total, field) for field in CODEX_TOKEN_USAGE_FIELDS),
        *(usage_int(last, field) for field in CODEX_TOKEN_USAGE_FIELDS),
    )


def codex_token_count_signatures_from_path(path: Path) -> list[tuple[int, ...]]:
    signatures: list[tuple[int, ...]] = []
    try:
        with path.open(encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("type") != "event_msg":
                    continue
                payload = row.get("payload") or {}
                if payload.get("type") == "token_count":
                    signatures.append(codex_token_count_signature(payload))
    except OSError:
        return []
    return signatures


def iter_recent_jsonl(
    root: Path,
    start: datetime,
    *,
    session_paths: dict[str, Path] | None = None,
) -> list[Path]:
    if not root.exists():
        return []
    paths: list[Path] = []
    seen: set[Path] = set()

    def resolve_path(path: Path) -> Path | None:
        try:
            resolved = path.resolve()
        except OSError:
            return None
        if resolved in seen:
            return resolved
        parts = {part.lower() for part in resolved.parts}
        if any(part.startswith("backup-") for part in parts) or ".tmp" in parts:
            return None
        if session_paths is not None:
            match = CODEX_SESSION_FILE_ID_RE.search(resolved.name)
            if match is not None:
                session_paths.setdefault(match.group("id").lower(), resolved)
        return resolved

    def add_path(path: Path) -> None:
        resolved = resolve_path(path)
        if resolved is None or resolved in seen:
            return
        seen.add(resolved)
        paths.append(resolved)

    day_dir = root / f"{start.year:04d}" / f"{start.month:02d}" / f"{start.day:02d}"
    if day_dir.exists():
        for path in day_dir.glob("*.jsonl"):
            add_path(path)
    for path in root.rglob("*.jsonl"):
        resolved = resolve_path(path)
        if resolved is None:
            continue
        try:
            modified = datetime.fromtimestamp(resolved.stat().st_mtime)
        except OSError:
            continue
        if modified >= start - timedelta(hours=2):
            add_path(resolved)
    return paths


def codex_session_headers(paths: list[Path]) -> dict[Path, tuple[str, str]]:
    headers: dict[Path, tuple[str, str]] = {}
    for path in paths:
        try:
            with path.open(encoding="utf-8", errors="ignore") as handle:
                lines = []
                for _ in range(20):
                    line = handle.readline()
                    if not line:
                        break
                    lines.append(line)
        except OSError:
            headers[path] = ("", "")
            continue
        headers[path] = codex_session_header(lines)
    return headers


def order_codex_session_paths(
    paths: list[Path],
    headers: dict[Path, tuple[str, str]],
    session_paths: dict[str, Path],
) -> list[Path]:
    path_set = set(paths)
    ordered: list[Path] = []
    visiting: set[Path] = set()
    visited: set[Path] = set()

    def visit(path: Path) -> None:
        if path in visited:
            return
        if path in visiting:
            return
        visiting.add(path)
        _session_id, parent_id = headers.get(path, ("", ""))
        parent_path = session_paths.get(parent_id.lower()) if parent_id else None
        if parent_path in path_set:
            visit(parent_path)
        visiting.remove(path)
        visited.add(path)
        ordered.append(path)

    for path in paths:
        visit(path)
    return ordered


def default_codex_desktop_log_roots() -> list[Path]:
    """Find desktop log roots for standalone and MSIX Codex installs."""
    configured = os.environ.get("CLIENT_USAGE_CODEX_DESKTOP_LOG_ROOT", "").strip()
    if configured:
        configured_roots = [
            Path(value.strip())
            for value in configured.split(os.pathsep)
            if value.strip()
        ]
        return configured_roots

    roots: list[Path] = []
    seen: set[Path] = set()

    def add_existing(path: Path) -> None:
        try:
            resolved = path.resolve()
            is_directory = resolved.is_dir()
        except OSError:
            return
        if not is_directory or resolved in seen:
            return
        seen.add(resolved)
        roots.append(resolved)

    def add_package_roots(base: Path, relatives: tuple[Path, ...]) -> None:
        try:
            packages = list(base.glob("OpenAI.Codex_*"))
        except OSError:
            return
        for package in packages:
            for relative in relatives:
                add_existing(package / relative)

    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if local_app_data:
        local_root = Path(local_app_data)
        add_existing(local_root / "Codex" / "Logs")
        add_existing(local_root / "Programs" / "Codex" / "Logs")
        add_existing(local_root / "Programs" / "Codex" / "app" / "Logs")
        add_package_roots(
            local_root / "Packages",
            (
                Path("LocalCache") / "Local" / "Codex" / "Logs",
                Path("LocalCache") / "Roaming" / "Codex" / "Logs",
                Path("LocalState") / "Codex" / "Logs",
                Path("LocalState") / "Logs",
            ),
        )

    roaming_app_data = os.environ.get("APPDATA", "").strip()
    if roaming_app_data:
        add_existing(Path(roaming_app_data) / "Codex" / "Logs")

    install_relatives = (
        Path("Logs"),
        Path("logs"),
        Path("app") / "Logs",
        Path("app") / "logs",
    )
    for variable in ("PROGRAMFILES", "ProgramW6432", "PROGRAMFILES(X86)"):
        program_files = os.environ.get(variable, "").strip()
        if program_files:
            add_package_roots(Path(program_files) / "WindowsApps", install_relatives)
    return roots


def iter_codex_desktop_logs(root: Path, start: datetime, end: datetime) -> list[Path]:
    if not root.exists():
        return []
    utc_start = start.replace(tzinfo=LOCAL_TZ).astimezone(timezone.utc)
    utc_end = end.replace(tzinfo=LOCAL_TZ).astimezone(timezone.utc)
    current_day = utc_start.date()
    end_day = utc_end.date()
    paths: list[Path] = []
    while current_day <= end_day:
        day_dir = root / f"{current_day.year:04d}" / f"{current_day.month:02d}" / f"{current_day.day:02d}"
        if day_dir.exists():
            paths.extend(sorted(day_dir.glob("codex-desktop-*.log")))
        current_day += timedelta(days=1)
    return paths


def scan_codex_desktop_failure_events(
    roots: Path | list[Path] | tuple[Path, ...],
    start: datetime,
    end: datetime,
) -> list[CodexFailureEvent]:
    network_failures: list[datetime] = []
    log_roots = (roots,) if isinstance(roots, Path) else tuple(roots)
    for root in log_roots:
        for path in iter_codex_desktop_logs(root, start, end):
            try:
                lines = path.open(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            with lines:
                for line in lines:
                    match = DESKTOP_LOG_LINE_RE.match(line)
                    if match is None:
                        continue
                    body = match.group("body")
                    if "sa_server_request_failed" not in body:
                        continue
                    if not any(code in body for code in DESKTOP_NETWORK_ERROR_CODES):
                        continue
                    when = parse_dt(match.group("timestamp"))
                    if when is not None and start <= when < end:
                        network_failures.append(when)

    failures: list[CodexFailureEvent] = []
    cluster: list[datetime] = []

    def finish_cluster() -> None:
        if len(cluster) < DESKTOP_NETWORK_FAILURE_MIN_COUNT:
            return
        failures.append(
            CodexFailureEvent(
                when=cluster[0],
                session_id="codex-desktop",
                kind="desktop_network",
            )
        )

    for when in sorted(set(network_failures)):
        if cluster and when - cluster[-1] > DESKTOP_NETWORK_FAILURE_CLUSTER_GAP:
            finish_cluster()
            cluster = []
        cluster.append(when)
    finish_cluster()
    return failures


def scan_codex_events(
    root: Path,
    start: datetime,
    end: datetime,
    session_lifecycle: dict[str, SessionLifecycle] | None = None,
    *,
    failure_events: list[CodexFailureEvent] | None = None,
) -> list[UsageEvent]:
    events: list[UsageEvent] = []
    failures_by_turn: dict[tuple[str, str], CodexFailureEvent] = {}
    seen_events: set[tuple[str, str, int, int, int, int]] = set()
    seen_totals: set[tuple[str, int, int, int, int]] = set()
    signatures_by_total: dict[
        tuple[str, int, int, int, int],
        set[tuple[int, ...]],
    ] = {}
    session_paths: dict[str, Path] = {}
    paths = iter_recent_jsonl(root, start, session_paths=session_paths)
    headers = codex_session_headers(paths)
    for path, (session_id, _parent_id) in headers.items():
        if session_id:
            session_paths.setdefault(session_id.lower(), path)
    paths = order_codex_session_paths(paths, headers, session_paths)
    signature_cache: dict[str, list[tuple[int, ...]]] = {}
    for path in paths:
        try:
            file_activity_at = datetime.fromtimestamp(path.stat().st_mtime)
        except OSError:
            file_activity_at = None
        last_total = {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0}
        current_model = CODEX_DEFAULT_MODEL
        seen: set[tuple[int, int, int, int]] = set()
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        session_id, parent_id = headers.get(path) or codex_session_header(lines)
        parent_signatures: list[tuple[int, ...]] = []
        if parent_id:
            parent_signatures = signature_cache.get(parent_id.lower(), [])
            if not parent_signatures:
                parent_path = session_paths.get(parent_id.lower())
                if parent_path is not None:
                    parent_signatures = codex_token_count_signatures_from_path(parent_path)
                    signature_cache[parent_id.lower()] = parent_signatures
        session_signatures: list[tuple[int, ...]] = []
        parent_prefix_index = 0
        parent_prefix_open = bool(parent_signatures)
        fork_replay_cutoff = codex_fork_replay_cutoff(lines)
        session_key = session_id or str(path)
        active_turn_id = ""
        active_turn_started_at: datetime | None = None
        for line in lines:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            row_type = row.get("type")
            payload = row.get("payload") or {}
            if row_type == "turn_context":
                context_model = str(row.get("model") or payload.get("model") or "").strip()
                if context_model:
                    current_model = codex_model_name(context_model)
                continue
            if row_type != "event_msg":
                continue
            payload_type = str(payload.get("type") or "")
            event_ts = parse_dt(row.get("timestamp"))
            if (
                session_lifecycle is not None
                and session_id
                and payload_type in {"task_started", "task_complete", "turn_aborted"}
            ):
                lifecycle_at = parse_dt(row.get("timestamp"))
                if lifecycle_at is not None and lifecycle_at < end:
                    candidate = SessionLifecycle(
                        session_id=session_id,
                        state=payload_type,
                        when=lifecycle_at,
                        turn_id=str(payload.get("turn_id") or ""),
                        file_activity_at=file_activity_at,
                    )
                    existing_lifecycle = session_lifecycle.get(session_id)
                    if existing_lifecycle is None or candidate.when >= existing_lifecycle.when:
                        session_lifecycle[session_id] = candidate
            if payload_type in {"task_started", "turn_started"}:
                active_turn_id = str(payload.get("turn_id") or "").strip()
                if not active_turn_id:
                    active_turn_id = f"started:{row.get('timestamp') or ''}"
                active_turn_started_at = event_ts
                continue
            if payload_type == "error":
                if (
                    active_turn_id
                    and event_ts is not None
                    and (fork_replay_cutoff is None or event_ts > fork_replay_cutoff)
                    and codex_error_affects_turn(payload)
                ):
                    key = (session_key, active_turn_id)
                    failures_by_turn[key] = CodexFailureEvent(
                        when=event_ts,
                        session_id=session_id,
                        turn_id=active_turn_id,
                    )
                continue
            if payload_type in {"task_complete", "turn_complete"}:
                turn_id = str(payload.get("turn_id") or active_turn_id).strip()
                key = (session_key, turn_id)
                valid_event = (
                    event_ts is not None
                    and (fork_replay_cutoff is None or event_ts > fork_replay_cutoff)
                )
                if valid_event and codex_error_affects_turn(payload.get("error")):
                    failures_by_turn[key] = CodexFailureEvent(
                        when=event_ts,
                        session_id=session_id,
                        turn_id=turn_id,
                    )
                elif valid_event and key in failures_by_turn:
                    failures_by_turn[key].when = event_ts
                if not turn_id or turn_id == active_turn_id:
                    active_turn_id = ""
                    active_turn_started_at = None
                continue
            if payload_type == "turn_aborted":
                turn_id = str(payload.get("turn_id") or active_turn_id).strip()
                if not turn_id or turn_id == active_turn_id:
                    active_turn_id = ""
                    active_turn_started_at = None
                continue
            if payload_type != "token_count":
                continue
            info = payload.get("info") or {}
            total = info.get("total_token_usage") or {}
            ts = event_ts
            signature = codex_token_count_signature(payload)
            session_signatures.append(signature)
            inherited_replay = False
            if parent_prefix_open:
                if (
                    parent_prefix_index < len(parent_signatures)
                    and signature == parent_signatures[parent_prefix_index]
                ):
                    inherited_replay = True
                    parent_prefix_index += 1
                else:
                    parent_prefix_open = False
            current = {
                "input_tokens": usage_int(total, "input_tokens"),
                "cached_input_tokens": usage_int(total, "cached_input_tokens"),
                "output_tokens": usage_int(total, "output_tokens"),
            }
            key = (
                current["input_tokens"],
                current["cached_input_tokens"],
                current["output_tokens"],
                usage_int(total, "reasoning_output_tokens"),
            )
            if key in seen:
                if inherited_replay:
                    last_total = current
                continue
            seen.add(key)
            if inherited_replay:
                last_total = current
                continue
            if ts is None or ts < start:
                last_total = current
                continue
            if ts >= end:
                continue
            explicit_model = str(row.get("model") or payload.get("model") or "").strip()
            model = codex_model_name(explicit_model) if explicit_model else current_model
            total_key = (
                model,
                current["input_tokens"],
                current["cached_input_tokens"],
                current["output_tokens"],
                usage_int(total, "reasoning_output_tokens"),
            )
            if (
                parent_prefix_index == 0
                and fork_replay_cutoff is not None
                and ts <= fork_replay_cutoff
            ):
                seen_totals.add(total_key)
                signatures_by_total.setdefault(total_key, set()).add(signature)
                last_total = current
                continue
            if total_key in seen_totals:
                known_signatures = signatures_by_total.get(total_key, set())
                # Only confirmed forks may contain distinct branch requests at one cumulative total.
                if parent_prefix_index == 0 or signature in known_signatures:
                    last_total = current
                    continue
            else:
                seen_totals.add(total_key)
            signatures_by_total.setdefault(total_key, set()).add(signature)
            last_usage = info.get("last_token_usage") or {}
            if last_usage:
                input_tokens = usage_int(last_usage, "input_tokens")
                cached_tokens = usage_int(last_usage, "cached_input_tokens")
                output_tokens = usage_int(last_usage, "output_tokens")
                event_key = (
                    str(row.get("timestamp") or ""),
                    model,
                    input_tokens,
                    cached_tokens,
                    output_tokens,
                    usage_int(last_usage, "reasoning_output_tokens"),
                )
                if event_key not in seen_events:
                    seen_events.add(event_key)
                    event = make_codex_event(
                        model,
                        input_tokens,
                        cached_tokens,
                        output_tokens,
                        ts,
                        session_id=session_id,
                        request_key=session_id,
                        account_at=active_turn_started_at,
                    )
                    if event is not None:
                        events.append(event)
                last_total = current
                continue
            delta_input = current["input_tokens"] - last_total["input_tokens"]
            delta_cached = current["cached_input_tokens"] - last_total["cached_input_tokens"]
            delta_output = current["output_tokens"] - last_total["output_tokens"]
            if delta_input < 0 or delta_cached < 0 or delta_output < 0:
                delta_input = current["input_tokens"]
                delta_cached = current["cached_input_tokens"]
                delta_output = current["output_tokens"]
            if delta_input <= 0 and delta_cached <= 0 and delta_output <= 0:
                continue
            event_key = (
                str(row.get("timestamp") or ""),
                model,
                delta_input,
                delta_cached,
                delta_output,
                usage_int(total, "reasoning_output_tokens"),
            )
            if event_key not in seen_events:
                seen_events.add(event_key)
                event = make_codex_event(
                    model,
                    delta_input,
                    delta_cached,
                    delta_output,
                    ts,
                    session_id=session_id,
                    request_key=session_id,
                    account_at=active_turn_started_at,
                )
                if event is not None:
                    events.append(event)
            last_total = current
        if session_id:
            signature_cache[session_id.lower()] = session_signatures
    events.sort(key=lambda event: event.when)
    if failure_events is not None:
        failure_events.extend(
            sorted(
                (
                    failure
                    for failure in failures_by_turn.values()
                    if start <= failure.when < end
                ),
                key=lambda failure: failure.when,
            )
        )
    return events


def scan_codex_route_markers(home: Path, start: datetime, end: datetime) -> list[RouteMarker]:
    db_path = home / ".codex" / "logs_2.sqlite"
    if not db_path.exists():
        return []

    start_epoch = int(start.replace(tzinfo=LOCAL_TZ).timestamp()) - 1800
    end_epoch = int(end.replace(tzinfo=LOCAL_TZ).timestamp()) + 300
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        rows = con.execute(
            """
            SELECT ts, feedback_log_body
            FROM logs
            WHERE ts >= ?
              AND ts < ?
              AND (
                feedback_log_body LIKE '%/v1/responses%'
                OR feedback_log_body LIKE '%chatgpt.com/backend-api/codex%'
                OR feedback_log_body LIKE '%responses_websocket%'
              )
            ORDER BY ts ASC, ts_nanos ASC
            """,
            (start_epoch, end_epoch),
        ).fetchall()
        con.close()
    except sqlite3.Error:
        return []

    markers: list[RouteMarker] = []
    for ts, body in rows:
        text = str(body or "")
        route = detect_codex_route(text)
        if not route:
            continue
        when = epoch_to_local_datetime(ts)
        if when is None:
            continue
        ids = codex_log_ids(text)
        request_key = codex_log_request_key(text)
        if request_key and request_key not in ids:
            ids.append(request_key)
        for key in ids:
            markers.append(RouteMarker(when=when, route=route, session_id=key, request_key=key))
    markers.sort(key=lambda marker: marker.when)
    return markers


def apply_codex_route_hints(events: list[UsageEvent], markers: list[RouteMarker]) -> None:
    if not events or not markers:
        return
    by_key: dict[str, list[RouteMarker]] = {}
    for marker in markers:
        for key in (marker.session_id, marker.request_key):
            key = (key or "").strip()
            if key:
                by_key.setdefault(key, []).append(marker)
    marker_times = {
        key: [marker.when for marker in sorted(value, key=lambda item: item.when)]
        for key, value in by_key.items()
    }
    for key, value in list(by_key.items()):
        by_key[key] = sorted(value, key=lambda item: item.when)

    for event in events:
        if event.route:
            continue
        candidates = [key for key in (event.session_id, event.request_key) if key]
        for key in candidates:
            markers_for_key = by_key.get(key)
            times_for_key = marker_times.get(key)
            if not markers_for_key or not times_for_key:
                continue
            pos = bisect_right(times_for_key, event.when) - 1
            if pos >= 0:
                marker = markers_for_key[pos]
                event.route = marker.route
                if event.request_at is None:
                    event.request_at = marker.when
                break


def scan_codex_logs2_events(home: Path, start: datetime, end: datetime) -> list[UsageEvent]:
    db_path = home / ".codex" / "logs_2.sqlite"
    if not db_path.exists():
        return []

    marker = "Received message "
    start_epoch = int(start.replace(tzinfo=LOCAL_TZ).timestamp()) - 300
    end_epoch = int(end.replace(tzinfo=LOCAL_TZ).timestamp()) + 300
    events: list[UsageEvent] = []
    seen_response_ids: set[str] = set()
    speed_by_request: dict[str, str] = {}
    tier_by_request: dict[str, str] = {}
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        rows = con.execute(
            """
            SELECT ts, feedback_log_body
            FROM logs
            WHERE ts >= ?
              AND ts < ?
              AND (
                feedback_log_body LIKE '%response.completed%'
                OR feedback_log_body LIKE '%service_tier: Some(%'
              )
            ORDER BY ts ASC, ts_nanos ASC
            """,
            (start_epoch, end_epoch),
        ).fetchall()
        con.close()
    except sqlite3.Error:
        return []

    for ts, body in rows:
        text = str(body or "")
        request_key = codex_log_request_key(text)
        internal_tier = codex_internal_service_tier(text)
        internal_speed = codex_service_tier_to_speed(internal_tier)
        if request_key and internal_speed:
            speed_by_request[request_key] = internal_speed
        if request_key and internal_tier:
            tier_by_request[request_key] = internal_tier
        message = parse_json_after_marker(text, marker)
        if message is None:
            event = codex_event_from_log_fields(text, ts)
            if event is not None:
                event_key = codex_log_request_key(text)
                if event_key and event_key in speed_by_request:
                    event.app_speed = speed_by_request[event_key]
                    event.cost_multiplier = codex_speed_cost_multiplier(event.app_speed)
                if event_key and event_key in tier_by_request:
                    event.pricing_tier = tier_by_request[event_key]
            if event is not None:
                events.append(event)
            continue
        if message.get("type") != "response.completed":
            event = codex_event_from_log_fields(text, ts)
            if event is not None:
                event_key = codex_log_request_key(text)
                if event_key and event_key in speed_by_request:
                    event.app_speed = speed_by_request[event_key]
                    event.cost_multiplier = codex_speed_cost_multiplier(event.app_speed)
                if event_key and event_key in tier_by_request:
                    event.pricing_tier = tier_by_request[event_key]
            if event is not None:
                events.append(event)
            continue
        response = message.get("response") or {}
        if not isinstance(response, dict):
            continue
        response_id = str(response.get("id") or "")
        if response_id and response_id in seen_response_ids:
            continue
        usage = response.get("usage") or {}
        if not isinstance(usage, dict):
            continue
        details = usage.get("input_tokens_details") or {}
        if not isinstance(details, dict):
            details = {}
        when = (
            epoch_to_local_datetime(response.get("completed_at"))
            or epoch_to_local_datetime(response.get("created_at"))
            or epoch_to_local_datetime(ts)
        )
        response_key = codex_log_request_key(text, response)
        ids = codex_log_ids(text, response)
        session_id = ids[0] if ids else response_key
        app_speed = internal_speed or ""
        if response_key and not app_speed:
            app_speed = speed_by_request.get(response_key, "")
        if not app_speed:
            app_speed = codex_service_tier_to_speed(response.get("service_tier"))
        pricing_tier = internal_tier or tier_by_request.get(response_key, "")
        if not pricing_tier:
            pricing_tier = normalize_pricing_tier(response.get("service_tier") or app_speed)
        multiplier = codex_speed_cost_multiplier(app_speed) if app_speed else None
        if when is None or when < start or when >= end:
            continue
        event = make_codex_event(
            str(response.get("model") or CODEX_DEFAULT_MODEL),
            usage_int(usage, "input_tokens"),
            usage_int(details, "cached_tokens"),
            usage_int(usage, "output_tokens"),
            when,
            app_speed,
            multiplier,
            session_id=session_id,
            request_key=response_key or session_id,
            route=detect_codex_route(text),
            pricing_tier=pricing_tier,
        )
        if event is None:
            continue
        if response_id:
            seen_response_ids.add(response_id)
        events.append(event)
    return events


def dedupe_usage_events(events: list[UsageEvent]) -> list[UsageEvent]:
    seen: dict[tuple[int, str, int, int, int], int] = {}
    result: list[UsageEvent] = []
    for event in sorted(events, key=lambda item: item.when):
        key = (
            int(event.when.replace(tzinfo=LOCAL_TZ).timestamp()),
            event.model,
            event.input_tokens,
            event.cached_tokens,
            event.output_tokens,
        )
        if key in seen:
            existing_idx = seen[key]
            existing = result[existing_idx]
            existing_score = usage_event_info_score(existing)
            event_score = usage_event_info_score(event)
            if event_score > existing_score:
                result[existing_idx] = event
            continue
        seen[key] = len(result)
        result.append(event)
    return result


def codex_event_id(event: UsageEvent) -> str:
    parts = [
        (event.session_id or event.request_key or "").strip(),
        str(int(event.when.replace(tzinfo=LOCAL_TZ).timestamp() * 1000)),
        event.model,
        str(event.input_tokens),
        str(event.cached_tokens),
        str(event.output_tokens),
    ]
    return "|".join(parts)


def legacy_codex_event_id(event: UsageEvent) -> str:
    if event.request_at is None:
        return codex_event_id(event)
    parts = [
        (event.session_id or event.request_key or "").strip(),
        str(int(event.request_at.replace(tzinfo=LOCAL_TZ).timestamp() * 1000)),
        event.model,
        str(event.input_tokens),
        str(event.cached_tokens),
        str(event.output_tokens),
    ]
    return "|".join(parts)


def ledger_label_for_event(
    event: UsageEvent,
    ledger: dict[str, str] | None,
) -> tuple[str, str]:
    stable_id = codex_event_id(event)
    if ledger is None:
        return "", stable_id
    label = ledger.get(stable_id, "")
    if label in {UNASSIGNED_CODEX_LABEL, "Codex local"}:
        label = ""
    if label:
        return label, stable_id
    legacy_id = legacy_codex_event_id(event)
    label = ledger.get(legacy_id, "")
    if label in {UNASSIGNED_CODEX_LABEL, "Codex local"}:
        label = ""
    if label and stable_id:
        ledger[stable_id] = label
    return label, stable_id


def usage_event_attribution_time(event: UsageEvent) -> datetime:
    return event.request_at or event.when


def usage_event_account_time(event: UsageEvent) -> datetime:
    return event.account_at or usage_event_attribution_time(event)


def usage_event_info_score(event: UsageEvent) -> int:
    score = 0
    if event.route:
        score += 8
    if event.session_id:
        score += 4
    if event.request_key:
        score += 2
    if event.cost_multiplier is not None:
        score += 1
    if event.request_at is not None:
        score += 1
    if event.account_at is not None:
        score += 1
    return score


def scan_all_codex_events(
    home: Path,
    sessions_root: Path,
    start: datetime,
    end: datetime,
    session_lifecycle: dict[str, SessionLifecycle] | None = None,
    *,
    failure_events: list[CodexFailureEvent] | None = None,
) -> list[UsageEvent]:
    events = scan_codex_events(
        sessions_root,
        start,
        end,
        session_lifecycle=session_lifecycle,
        failure_events=failure_events,
    )
    events.extend(scan_codex_logs2_events(home, start, end))
    route_markers = scan_codex_route_markers(home, start, end)
    apply_codex_route_hints(events, route_markers)
    return dedupe_usage_events(events)


def bucket_from_codex_events(events: list[UsageEvent]) -> UsageBucket:
    bucket = UsageBucket()
    for event in events:
        add_codex_event_to_bucket(bucket, event)
    return bucket


def scan_codex(root: Path, start: datetime, end: datetime) -> UsageBucket:
    return bucket_from_codex_events(scan_codex_events(root, start, end))


def local_epoch_ms(value: datetime) -> int:
    return int(value.replace(tzinfo=LOCAL_TZ).timestamp() * 1000)


def ms_to_local_datetime(value: int | float | str | None) -> datetime | None:
    try:
        millis = int(value or 0)
    except (TypeError, ValueError):
        return None
    if millis <= 0:
        return None
    return datetime.fromtimestamp(millis / 1000, tz=LOCAL_TZ).replace(tzinfo=None)


def cockpit_account_label(account_id: str, email: str, api_key_label: str) -> str:
    email = (email or "").strip()
    if email:
        return f"Codex local - {email}"
    api_key_label = (api_key_label or "").strip()
    if api_key_label:
        return f"Codex local - {api_key_label}"
    account_id = (account_id or "").strip()
    if account_id:
        return f"Codex local - {account_id}"
    return "Codex local - Unknown"


def decode_jwt_payload(value: Any) -> dict[str, Any]:
    token = str(value or "").strip()
    if token.count(".") < 2:
        return {}
    try:
        payload = token.split(".", 2)[1]
        payload += "=" * (-len(payload) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
    except Exception:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def codex_auth_identity(data: dict[str, Any] | None) -> str:
    if not isinstance(data, dict):
        return ""
    tokens = data.get("tokens") if isinstance(data.get("tokens"), dict) else {}
    claims = decode_jwt_payload(tokens.get("id_token"))
    auth_claims = claims.get("https://api.openai.com/auth")
    auth_claims = auth_claims if isinstance(auth_claims, dict) else {}

    email = str(
        data.get("email")
        or data.get("OPENAI_EMAIL")
        or tokens.get("email")
        or claims.get("email")
        or claims.get("preferred_username")
        or ""
    ).strip()
    if email:
        return email

    account_id = str(
        data.get("account_id")
        or tokens.get("account_id")
        or claims.get("account_id")
        or claims.get("chatgpt_account_id")
        or auth_claims.get("chatgpt_account_id")
        or auth_claims.get("account_id")
        or ""
    ).strip()
    if account_id:
        return account_id

    return str(
        data.get("api_provider_name")
        or data.get("api_provider_id")
        or ""
    ).strip()


def active_codex_model_provider(home: Path) -> str:
    path = home / ".codex" / "config.toml"
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return ""
    for line in lines:
        stripped = line.split("#", 1)[0].strip()
        if not stripped:
            continue
        if stripped.startswith("["):
            break
        if "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        if key.strip() == "model_provider":
            return value.strip().strip('"').strip("'")
    return ""


def codex_uses_cockpit_provider(home: Path) -> bool:
    provider = active_codex_model_provider(home).strip().lower()
    return (
        provider in API_SERVICE_MIRROR_LABELS
        or "codex_local_access" in provider
        or "api-service" in provider
    )


def current_codex_account_snapshot(home: Path) -> tuple[str, Path | None, datetime | None]:
    codex_dir = home / ".codex"
    names = (
        (".cockpit_codex_auth.json",)
        if codex_uses_cockpit_provider(home)
        else ("auth.json",)
    )
    for name in names:
        path = codex_dir / name
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            continue
        identity = codex_auth_identity(data)
        if identity:
            return f"Codex local - {identity}", path, file_mtime_local(path)
    return "Codex local", None, None


def current_codex_account_label(home: Path) -> str:
    label, _path, _changed_at = current_codex_account_snapshot(home)
    return label


def load_account_timeline() -> list[AccountMarker]:
    data = load_json_object(ACCOUNT_TIMELINE_PATH)
    raw_records = data.get("records")
    if not isinstance(raw_records, list):
        raw_records = []
    markers: list[AccountMarker] = []
    for item in raw_records:
        if not isinstance(item, dict):
            continue
        when = parse_dt(item.get("at"))
        label = str(item.get("label") or "").strip()
        if when is None or not label:
            continue
        markers.append(AccountMarker(when=when, label=label, model=CODEX_DEFAULT_MODEL, kind="switch"))
    try:
        auth_event_lines = AUTH_SWITCH_EVENTS_PATH.read_text(
            encoding="utf-8",
            errors="ignore",
        ).splitlines()
    except OSError:
        auth_event_lines = []
    for line in auth_event_lines:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(item, dict):
            continue
        when = parse_dt(item.get("at"))
        label = str(item.get("label") or "").strip()
        if when is None or not label:
            continue
        markers.append(AccountMarker(when=when, label=label, model=CODEX_DEFAULT_MODEL, kind="switch"))
    deduped: dict[tuple[datetime, str], AccountMarker] = {
        (marker.when, marker.label): marker
        for marker in markers
    }
    markers = list(deduped.values())
    markers.sort(key=lambda marker: marker.when)
    return markers


def record_current_account_snapshot(home: Path, now: datetime) -> None:
    label, _source_path, changed_at = current_codex_account_snapshot(home)
    if not label or label == "Codex local":
        return
    markers = load_account_timeline()
    if markers and markers[-1].label == label:
        return
    cutoff = now - timedelta(days=120)
    marker_time = changed_at or now
    if marker_time > now + timedelta(minutes=5):
        marker_time = now
    marker_time = max(cutoff, marker_time)
    if markers and marker_time <= markers[-1].when:
        marker_time = now
    markers.append(AccountMarker(when=marker_time, label=label, model=CODEX_DEFAULT_MODEL, kind="switch"))
    compact = [marker for marker in markers if marker.when >= cutoff]
    write_json_object(
        ACCOUNT_TIMELINE_PATH,
        {
            "schema": 1,
            "updated_at": now.replace(tzinfo=LOCAL_TZ).isoformat(timespec="seconds"),
            "records": [
                {
                    "at": marker.when.replace(tzinfo=LOCAL_TZ).isoformat(timespec="seconds"),
                    "label": marker.label,
                }
                for marker in compact
            ],
        },
    )


def account_label_for_event(
    event: UsageEvent,
    markers: list[AccountMarker],
    current_label: str = "",
    now: datetime | None = None,
) -> str:
    markers = sorted(markers, key=lambda marker: marker.when)
    switch_markers = [marker for marker in markers if marker.kind == "switch"]
    switch_times = [marker.when for marker in switch_markers]
    request_markers = [marker for marker in markers if marker.kind != "switch"]
    request_times = [marker.when for marker in request_markers]
    label = account_label_at_time(event, switch_markers, switch_times, request_markers, request_times)
    if (
        label == UNASSIGNED_CODEX_LABEL
        and current_label
        and now is not None
        and 0 <= (now - usage_event_account_time(event)).total_seconds() <= CODEX_CURRENT_ACCOUNT_RECENT_SECONDS
    ):
        label = current_label
    return label


def load_attribution_ledger() -> dict[str, str]:
    data = load_json_object(ATTRIBUTION_LEDGER_PATH)
    ledger = data.get("events")
    if not isinstance(ledger, dict):
        return {}
    result: dict[str, str] = {}
    for key, value in ledger.items():
        label = str(value or "").strip()
        if key and label:
            result[str(key)] = label
    return result


def save_attribution_ledger(ledger: dict[str, str], now: datetime) -> None:
    write_json_object(
        ATTRIBUTION_LEDGER_PATH,
        {
            "schema": 1,
            "updated_at": now.replace(tzinfo=LOCAL_TZ).isoformat(timespec="seconds"),
            "events": dict(sorted(ledger.items())),
        },
    )


def all_cockpit_codex_account_labels(home: Path) -> list[str]:
    path = home / ".antigravity_cockpit" / "codex_accounts.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return []
    accounts = data.get("accounts") if isinstance(data, dict) else None
    if not isinstance(accounts, list):
        return []
    labels: list[str] = []
    for account in accounts:
        if not isinstance(account, dict):
            continue
        label = cockpit_account_label(
            str(account.get("id") or ""),
            str(account.get("email") or ""),
            str(account.get("api_provider_name") or account.get("name") or ""),
        )
        if label not in labels:
            labels.append(label)
    return labels


def cockpit_codex_account_label_by_id(home: Path) -> dict[str, str]:
    path = home / ".antigravity_cockpit" / "codex_accounts.json"
    labels: dict[str, str] = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            data = {}
        accounts = data.get("accounts") if isinstance(data, dict) else None
        if isinstance(accounts, list):
            for account in accounts:
                if not isinstance(account, dict):
                    continue
                account_id = str(account.get("id") or "").strip()
                if not account_id:
                    continue
                labels[account_id] = cockpit_account_label(
                    account_id,
                    str(account.get("email") or ""),
                    str(account.get("api_provider_name") or account.get("name") or ""),
                )

    accounts_dir = home / ".antigravity_cockpit" / "codex_accounts"
    if accounts_dir.exists():
        for path in accounts_dir.glob("*.json*"):
            try:
                account = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
            except Exception:
                continue
            if not isinstance(account, dict):
                continue
            account_id = str(account.get("id") or path.stem).strip()
            if not account_id or account_id in labels:
                continue
            labels[account_id] = cockpit_account_label(
                account_id,
                str(account.get("email") or ""),
                str(account.get("api_provider_name") or account.get("name") or ""),
            )
    return labels


def normalize_codex_speed(speed: Any) -> str:
    value = str(speed or "").strip().lower()
    if value in {"fast", "quick", "turbo"}:
        return "fast"
    if value in {"standard", "normal", "default"}:
        return "standard"
    if value in {"auto", "detect"}:
        return ""
    return value


def codex_speed_meta(speed: str) -> dict[str, Any]:
    normalized = normalize_codex_speed(speed) or "standard"
    multiplier = codex_speed_cost_multiplier(normalized)
    return {
        "app_speed": normalized,
        "cost_multiplier": multiplier,
        "speed_badge": f"FAST x{multiplier:g}" if multiplier > 1 else "",
    }


def load_client_usage_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return {}
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def load_json_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def write_json_object(path: Path, data: dict[str, Any]) -> None:
    try:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def parse_speed_overrides(value: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in (value or "").split(","):
        if "=" not in item:
            continue
        key, speed = item.split("=", 1)
        key = key.strip().lower()
        speed = normalize_codex_speed(speed)
        if key and speed:
            result[key] = speed
    return result


def config_speed_overrides(config: dict[str, Any]) -> dict[str, str]:
    codex_config = config.get("codex") if isinstance(config, dict) else None
    overrides = codex_config.get("speed_overrides") if isinstance(codex_config, dict) else None
    result: dict[str, str] = {}
    if isinstance(overrides, dict):
        for key, speed in overrides.items():
            normalized = normalize_codex_speed(speed)
            if normalized:
                result[str(key).strip().lower()] = normalized
    result.update(parse_speed_overrides(CODEX_SPEED_OVERRIDES))
    return result


def config_current_speed(config: dict[str, Any]) -> str:
    codex_config = config.get("codex") if isinstance(config, dict) else None
    if CODEX_FORCE_SPEED:
        return normalize_codex_speed(CODEX_FORCE_SPEED)
    if isinstance(codex_config, dict):
        return normalize_codex_speed(codex_config.get("current_speed"))
    return ""


def codex_config_service_tier_speed(config_path: Path) -> str:
    if not config_path.exists():
        return ""
    try:
        text = config_path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        text = ""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        if key.strip() != "service_tier":
            continue
        tier = value.split("#", 1)[0].strip().strip('"').strip("'").lower()
        if tier in {"priority", "fast"}:
            return "fast"
        if tier in {"flex", "batch", "batches"}:
            return "batch" if tier in {"batch", "batches"} else "flex"
        if tier in {"standard", "default", "auto", "none", "null", ""}:
            return "standard"
    return "standard"


def file_mtime_local(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime)
    except OSError:
        return None


def codex_service_tier_speed(home: Path) -> str:
    config_path = home / ".codex" / "config.toml"
    speed = codex_config_service_tier_speed(config_path)
    if speed:
        return speed

    state_path = home / ".codex" / ".codex-global-state.json"
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            state = {}
        if isinstance(state, dict):
            tier = str(state.get("default-service-tier") or "").strip().lower()
            if tier in {"priority", "fast"}:
                return "fast"
            if tier in {"flex", "batch", "batches"}:
                return "batch" if tier in {"batch", "batches"} else "flex"
            if tier in {"standard", "default", "auto", "none", "null", ""}:
                return "standard"
    return ""


def load_speed_history() -> list[SpeedMarker]:
    if not SPEED_HISTORY_PATH.exists():
        return []
    try:
        data = json.loads(SPEED_HISTORY_PATH.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return []
    raw_records = data.get("records") if isinstance(data, dict) else data
    if not isinstance(raw_records, list):
        return []
    records: list[SpeedMarker] = []
    for item in raw_records:
        if not isinstance(item, dict):
            continue
        when = parse_dt(item.get("at"))
        speed = normalize_codex_speed(item.get("speed"))
        if when is not None and speed:
            records.append(SpeedMarker(when, speed))
    records.sort(key=lambda marker: marker.when)
    return records


def save_speed_history(records: list[SpeedMarker]) -> None:
    compact: list[SpeedMarker] = []
    for marker in sorted(records, key=lambda item: item.when):
        if compact and compact[-1].speed == marker.speed:
            continue
        compact.append(marker)
    try:
        SPEED_HISTORY_PATH.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "updated_at": datetime.now(LOCAL_TZ).isoformat(timespec="seconds"),
                    "records": [
                        {
                            "at": marker.when.replace(tzinfo=LOCAL_TZ).isoformat(timespec="seconds"),
                            "speed": marker.speed,
                        }
                        for marker in compact
                    ],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except Exception:
        pass


def codex_speed_history(home: Path, start: datetime, end: datetime) -> list[SpeedMarker]:
    records = load_speed_history()
    config_path = home / ".codex" / "config.toml"
    backup_path = home / ".codex" / "config.toml.bak"
    current_speed = codex_service_tier_speed(home) or "standard"
    change_at = file_mtime_local(config_path) or datetime.now()
    backup_speed = codex_config_service_tier_speed(backup_path)

    if not records:
        if backup_speed and backup_speed != current_speed and start <= change_at < end:
            records.extend([SpeedMarker(start, backup_speed), SpeedMarker(change_at, current_speed)])
        else:
            records.append(SpeedMarker(start, current_speed))
    else:
        last = records[-1]
        if last.speed != current_speed:
            marker_time = change_at if change_at > last.when else datetime.now()
            records.append(SpeedMarker(marker_time, current_speed))

    if records[0].when > start:
        records.insert(0, SpeedMarker(start, records[0].speed))
    save_speed_history(records)
    return sorted(records, key=lambda marker: marker.when)


def codex_speed_at(markers: list[SpeedMarker], when: datetime | None) -> str:
    if when is None or not markers:
        return ""
    speed = markers[0].speed
    for marker in markers:
        if marker.when <= when:
            speed = marker.speed
        else:
            break
    return speed


def apply_codex_speed_fallback(events: list[UsageEvent], markers: list[SpeedMarker]) -> None:
    for event in events:
        if event.cost_multiplier is not None and event.pricing_tier:
            continue
        speed = codex_speed_at(markers, event.when)
        if not speed:
            continue
        event.app_speed = speed
        event.cost_multiplier = codex_speed_cost_multiplier(speed)
        event.pricing_tier = normalize_pricing_tier(speed)


def account_speed_override(
    label: str,
    account: dict[str, Any],
    overrides: dict[str, str],
) -> str:
    keys = {
        label,
        str(account.get("email") or ""),
        str(account.get("id") or ""),
        str(account.get("account_id") or ""),
        str(account.get("api_provider_name") or ""),
        str(account.get("name") or ""),
    }
    for key in keys:
        override = overrides.get(key.strip().lower())
        if override:
            return override
    return ""


def cockpit_codex_speed_by_label(home: Path) -> dict[str, dict[str, Any]]:
    accounts_dir = home / ".antigravity_cockpit" / "codex_accounts"
    config = load_client_usage_config()
    overrides = config_speed_overrides(config)
    forced_current_speed = config_current_speed(config)
    detected_current_speed = codex_service_tier_speed(home)
    # Codex service_tier is a global client mode, not a per-account setting.
    # Apply it to every local Codex account unless a user override exists.
    current_speed = forced_current_speed or detected_current_speed

    result: dict[str, dict[str, Any]] = {}
    if accounts_dir.exists():
        for path in accounts_dir.glob("*.json"):
            try:
                account = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
            except Exception:
                continue
            if not isinstance(account, dict):
                continue
            label = cockpit_account_label(
                str(account.get("id") or path.stem),
                str(account.get("email") or ""),
                str(account.get("api_provider_name") or account.get("name") or ""),
            )
            speed = normalize_codex_speed(account.get("app_speed")) or "standard"
            if current_speed:
                speed = current_speed
            override = account_speed_override(label, account, overrides)
            if override:
                speed = override
            result[label] = codex_speed_meta(speed)
    return result


def epoch_seconds_to_local_iso(value: Any) -> str:
    try:
        seconds = int(value or 0)
    except (TypeError, ValueError):
        return ""
    if seconds <= 0:
        return ""
    try:
        return datetime.fromtimestamp(seconds, tz=LOCAL_TZ).isoformat(timespec="seconds")
    except (OSError, OverflowError, ValueError):
        return ""


def quota_window_payload(
    percent_remaining: Any,
    reset_at: Any,
    stale: bool,
    window_minutes: int | None = None,
) -> dict[str, Any]:
    resets_at = epoch_seconds_to_local_iso(reset_at)
    missing_reset = percent_remaining is not None and not resets_at
    window: dict[str, Any] = {
        "quota_available": percent_remaining is not None and not missing_reset,
        "quota_stale": stale or missing_reset or percent_remaining is None,
        "resets_at": resets_at,
    }
    if window_minutes:
        window["window_minutes"] = int(window_minutes)
        window["window_days"] = round(float(window_minutes) / (24 * 60), 1)
    if percent_remaining is not None:
        try:
            remaining = max(0.0, min(100.0, float(percent_remaining)))
            window["remaining_percent"] = remaining
            window["utilization"] = 100.0 - remaining
        except (TypeError, ValueError):
            window["quota_available"] = False
            window["quota_stale"] = True
    return window


def official_quota_window_payload(
    raw_window: Any,
    fallback_seconds: int,
    checked_at: datetime,
) -> dict[str, Any] | None:
    if not isinstance(raw_window, dict):
        return None
    try:
        window_seconds = int(raw_window.get("limit_window_seconds") or fallback_seconds)
    except (TypeError, ValueError):
        window_seconds = fallback_seconds
    if window_seconds <= 0:
        window_seconds = fallback_seconds
    try:
        used_percent = float(raw_window.get("used_percent"))
        remaining_percent: float | None = 100.0 - used_percent
    except (TypeError, ValueError):
        remaining_percent = None
    reset_at = raw_window.get("reset_at")
    if not reset_at:
        try:
            reset_after = float(raw_window.get("reset_after_seconds") or 0)
        except (TypeError, ValueError):
            reset_after = 0.0
        if reset_after > 0:
            reset_at = checked_at.timestamp() + reset_after
    window = quota_window_payload(
        remaining_percent,
        reset_at,
        False,
        max(1, round(window_seconds / 60)),
    )
    window.update(
        {
            "quota_source": "official-wham",
            "quota_snapshot_at": checked_at.isoformat(timespec="seconds"),
        }
    )
    if window.get("resets_at"):
        window["quota_reset_unavailable"] = False
    return window


def official_quota_from_usage_response(
    payload: Any,
    checked_at: datetime | None = None,
    fallback_plan_type: str = "",
) -> dict[str, dict[str, Any]] | None:
    if not isinstance(payload, dict):
        return None
    rate_limit = payload.get("rate_limit")
    if not isinstance(rate_limit, dict):
        return None
    checked_at = checked_at or datetime.now(LOCAL_TZ)
    seven_day_seconds = 7 * 24 * 60 * 60
    windows = (
        (rate_limit.get("primary_window"), 5 * 60 * 60),
        (rate_limit.get("secondary_window"), seven_day_seconds),
    )
    five_hour: dict[str, Any] = {"quota_available": False, "quota_stale": False}
    seven_day: dict[str, Any] = {"quota_available": False, "quota_stale": False}
    cycle: dict[str, Any] = {"quota_available": False, "quota_stale": False}
    short_window_present = False
    seven_day_present = False
    for raw_window, fallback_seconds in windows:
        parsed = official_quota_window_payload(raw_window, fallback_seconds, checked_at)
        if parsed is None:
            continue
        try:
            window_seconds = int(parsed.get("window_minutes") or 0) * 60
        except (TypeError, ValueError):
            window_seconds = fallback_seconds
        if window_seconds < seven_day_seconds:
            five_hour = parsed
            short_window_present = True
        elif window_seconds == seven_day_seconds:
            seven_day = parsed
            seven_day_present = True
        else:
            cycle = parsed
    plan_type = str(payload.get("plan_type") or fallback_plan_type or "").strip().lower()
    if plan_type == "plus" and seven_day_present and not short_window_present:
        five_hour = {
            "quota_available": False,
            "quota_stale": False,
            "quota_unlimited": True,
            "quota_source": "official-wham",
            "quota_snapshot_at": checked_at.isoformat(timespec="seconds"),
        }
    return {
        "window_5h": five_hour,
        "window_7d": seven_day,
        "window_cycle": cycle,
    }


def quota_row_needs_official_refresh(quota: Any) -> bool:
    if not isinstance(quota, dict):
        return True
    available_windows = []
    for key in ("window_5h", "window_7d", "window_cycle"):
        window = quota.get(key)
        if not isinstance(window, dict) or window.get("quota_unlimited"):
            continue
        if window.get("quota_available"):
            available_windows.append(window)
    if not available_windows:
        return True
    return any(window.get("quota_stale") or not window.get("resets_at") for window in available_windows)


def load_official_quota_cache() -> dict[str, Any]:
    try:
        data = json.loads(COCKPIT_OFFICIAL_QUOTA_CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    accounts = data.get("accounts") if isinstance(data, dict) else None
    return accounts if isinstance(accounts, dict) else {}


def read_cockpit_sidecar_auth(home: Path, account_id: str) -> dict[str, Any] | None:
    path = (
        home
        / ".antigravity_cockpit"
        / "codex_local_access_sidecar"
        / "auths"
        / f"{account_id}.json"
    )
    try:
        auth = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(auth, dict) or auth.get("disabled"):
        return None
    expired = auth.get("expired")
    if isinstance(expired, bool):
        if expired:
            return None
    elif expired not in (None, ""):
        try:
            expires_at = float(expired)
        except (TypeError, ValueError):
            expires_at = 0.0
        if expires_at > 1 and expires_at <= datetime.now().timestamp():
            return None
    if not str(auth.get("access_token") or "").strip():
        return None
    if not str(auth.get("account_id") or "").strip():
        return None
    return auth


def fetch_cockpit_official_quota(
    auth: dict[str, Any],
    plan_type: str,
    checked_at: datetime,
) -> dict[str, dict[str, Any]] | None:
    access_token = str(auth.get("access_token") or "").strip()
    account_id = str(auth.get("account_id") or "").strip()
    if not access_token or not account_id:
        return None
    req = request.Request(
        COCKPIT_OFFICIAL_QUOTA_URL,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {access_token}",
            "ChatGPT-Account-Id": account_id,
            "User-Agent": "codex-token-pulse/1.0",
        },
    )
    try:
        with request.urlopen(req, timeout=COCKPIT_OFFICIAL_QUOTA_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:
        return None
    return official_quota_from_usage_response(payload, checked_at, plan_type)


def cockpit_official_quota_by_account(
    home: Path,
    accounts: dict[str, dict[str, Any]],
    now: datetime | None = None,
) -> dict[str, dict[str, dict[str, Any]]]:
    if not COCKPIT_OFFICIAL_QUOTA_ENABLED or not accounts:
        return {}
    now = now or datetime.now(LOCAL_TZ)
    now_epoch = now.timestamp()
    cache = load_official_quota_cache()
    result: dict[str, dict[str, dict[str, Any]]] = {}
    pending: dict[str, tuple[dict[str, Any], str]] = {}
    cache_changed = False
    for account_id, account in accounts.items():
        cached = cache.get(account_id)
        try:
            checked_epoch = float(cached.get("checked_at") or 0) if isinstance(cached, dict) else 0.0
        except (TypeError, ValueError):
            checked_epoch = 0.0
        cache_age = now_epoch - checked_epoch if checked_epoch > 0 else float("inf")
        if 0 <= cache_age < COCKPIT_OFFICIAL_QUOTA_CACHE_SECONDS:
            quota = cached.get("quota") if isinstance(cached, dict) else None
            if isinstance(quota, dict):
                result[account_id] = quota
            continue
        auth = read_cockpit_sidecar_auth(home, account_id)
        if auth is not None:
            pending[account_id] = (auth, str(account.get("plan_type") or ""))

    if pending:
        worker_count = min(COCKPIT_OFFICIAL_QUOTA_MAX_WORKERS, len(pending))
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = {
                executor.submit(fetch_cockpit_official_quota, auth, plan_type, now): account_id
                for account_id, (auth, plan_type) in pending.items()
            }
            for future in as_completed(futures):
                account_id = futures[future]
                try:
                    quota = future.result()
                except Exception:
                    quota = None
                cache[account_id] = {
                    "checked_at": now_epoch,
                    "quota": quota if isinstance(quota, dict) else None,
                }
                cache_changed = True
                if isinstance(quota, dict):
                    result[account_id] = quota

    if cache_changed:
        active_ids = set(accounts)
        compact_cache = {
            account_id: entry
            for account_id, entry in cache.items()
            if account_id in active_ids and isinstance(entry, dict)
        }
        try:
            write_json_atomic(
                COCKPIT_OFFICIAL_QUOTA_CACHE_PATH,
                {"schema": 1, "accounts": compact_cache},
            )
        except OSError:
            pass
    return result


def cockpit_codex_quota_by_label(home: Path) -> dict[str, dict[str, dict[str, Any]]]:
    accounts_dir = home / ".antigravity_cockpit" / "codex_accounts"
    if not accounts_dir.exists():
        return {}

    result: dict[str, dict[str, dict[str, Any]]] = {}
    for path in accounts_dir.glob("*.json"):
        try:
            account = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            continue
        if not isinstance(account, dict):
            continue
        quota = account.get("quota")
        if not isinstance(quota, dict):
            continue

        label = cockpit_account_label(
            str(account.get("id") or path.stem),
            str(account.get("email") or ""),
            str(account.get("api_provider_name") or account.get("name") or ""),
        )
        stale = bool(account.get("quota_error"))
        plan_type = str(account.get("plan_type") or quota.get("plan_type") or "").strip().lower()
        primary_present = bool(quota.get("hourly_window_present"))
        primary_value = quota.get("hourly_percentage") if primary_present else None
        weekly_value = quota.get("weekly_percentage")
        weekly_present = bool(quota.get("weekly_window_present"))
        try:
            hourly_window_minutes = int(quota.get("hourly_window_minutes") or 5 * 60)
        except (TypeError, ValueError):
            hourly_window_minutes = 5 * 60
        if hourly_window_minutes <= 0:
            hourly_window_minutes = 5 * 60
        seven_day_minutes = 7 * 24 * 60
        five_hour: dict[str, Any] = {"quota_available": False, "quota_stale": stale}
        seven_day: dict[str, Any] = {"quota_available": False, "quota_stale": stale}
        cycle: dict[str, Any] = {"quota_available": False, "quota_stale": stale}
        primary_is_7d = primary_present and hourly_window_minutes == seven_day_minutes
        short_window_present = primary_present and hourly_window_minutes < seven_day_minutes

        if primary_present:
            primary_window = quota_window_payload(
                primary_value,
                quota.get("hourly_reset_time"),
                stale,
                hourly_window_minutes,
            )
            if hourly_window_minutes < seven_day_minutes:
                five_hour = primary_window
            elif hourly_window_minutes == seven_day_minutes:
                seven_day = primary_window
            else:
                cycle = primary_window

        if weekly_present and not primary_is_7d:
            seven_day = quota_window_payload(
                weekly_value,
                quota.get("weekly_reset_time"),
                stale,
                seven_day_minutes,
            )

        if plan_type == "plus" and not short_window_present and (primary_is_7d or weekly_present):
            five_hour = {
                "quota_available": False,
                "quota_stale": False,
                "quota_unlimited": True,
            }

        result[label] = {
            "window_5h": five_hour,
            "window_7d": seven_day,
            "window_cycle": cycle,
        }

    # Newer Cockpit versions encrypt per-account JSON. The sidecar keeps a
    # non-secret routing snapshot with current quota percentages, while the
    # account manifest retains the ID/email/plan mapping needed for labels.
    manifest_path = home / ".antigravity_cockpit" / "codex_accounts.json"
    reserve_path = (
        home
        / ".antigravity_cockpit"
        / "codex_local_access_sidecar"
        / "quota-reserve.json"
    )
    try:
        manifest_data = json.loads(manifest_path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        manifest_data = {}
    try:
        reserve_data = json.loads(reserve_path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        reserve_data = {}
    manifest_rows = manifest_data.get("accounts") if isinstance(manifest_data, dict) else None
    reserve_rows = reserve_data.get("accounts") if isinstance(reserve_data, dict) else None
    manifest_by_id = {
        str(row.get("id") or "").strip(): row
        for row in (manifest_rows if isinstance(manifest_rows, list) else [])
        if isinstance(row, dict) and str(row.get("id") or "").strip()
    }
    labels_by_id = {
        account_id: cockpit_account_label(
            account_id,
            str(account.get("email") or ""),
            str(account.get("api_provider_name") or account.get("name") or ""),
        )
        for account_id, account in manifest_by_id.items()
    }

    def reserve_window(
        snapshot: dict[str, Any],
        prefix: str,
        stale: bool,
        snapshot_at: str,
        default_window_minutes: int,
    ) -> dict[str, Any]:
        present = snapshot.get(f"{prefix}WindowPresent") is True
        value = snapshot.get(f"{prefix}RemainingPercent")
        reset_value = next(
            (
                snapshot.get(key)
                for key in (
                    f"{prefix}ResetTime",
                    f"{prefix}ResetAt",
                    f"{prefix}ResetAtUnixSeconds",
                    f"{prefix}ResetUnixSeconds",
                )
                if snapshot.get(key) not in (None, "")
            ),
            None,
        )
        resets_at = epoch_seconds_to_local_iso(reset_value)
        if not resets_at:
            parsed_reset = parse_dt(reset_value)
            if parsed_reset is not None:
                resets_at = parsed_reset.replace(tzinfo=LOCAL_TZ).isoformat(timespec="seconds")
        try:
            window_minutes = int(snapshot.get(f"{prefix}WindowMinutes") or 0)
        except (TypeError, ValueError):
            window_minutes = 0
        if window_minutes <= 0:
            try:
                window_seconds = int(
                    snapshot.get(f"{prefix}LimitWindowSeconds")
                    or snapshot.get(f"{prefix}WindowSeconds")
                    or 0
                )
            except (TypeError, ValueError):
                window_seconds = 0
            window_minutes = max(1, round(window_seconds / 60)) if window_seconds > 0 else default_window_minutes
        window: dict[str, Any] = {
            "quota_available": False,
            "quota_stale": stale,
            "quota_source": "sidecar-reserve",
            "quota_snapshot_at": snapshot_at,
            "window_minutes": window_minutes,
            "window_days": round(float(window_minutes) / (24 * 60), 1),
        }
        if not present or value is None:
            return window
        try:
            remaining = max(0.0, min(100.0, float(value)))
        except (TypeError, ValueError):
            window["quota_stale"] = True
            return window
        window.update(
            {
                "quota_available": True,
                "remaining_percent": remaining,
                "utilization": 100.0 - remaining,
                "resets_at": resets_at,
                "quota_reset_unavailable": not bool(resets_at),
            }
        )
        return window

    if isinstance(reserve_rows, dict):
        now_epoch = datetime.now().timestamp()
        for account_id, raw_snapshot in reserve_rows.items():
            if not isinstance(raw_snapshot, dict):
                continue
            account = manifest_by_id.get(str(account_id), {})
            label = cockpit_account_label(
                str(account_id),
                str(account.get("email") or ""),
                str(account.get("api_provider_name") or account.get("name") or ""),
            )
            try:
                snapshot_epoch = float(raw_snapshot.get("snapshotUpdatedAtUnixSeconds") or 0)
            except (TypeError, ValueError):
                snapshot_epoch = 0.0
            snapshot_age = now_epoch - snapshot_epoch if snapshot_epoch > 0 else float("inf")
            stale = snapshot_age < -300 or snapshot_age > max(
                60,
                COCKPIT_QUOTA_RESERVE_STALE_SECONDS,
            )
            snapshot_at = epoch_seconds_to_local_iso(snapshot_epoch)
            plan_type = str(account.get("plan_type") or "").strip().lower()
            hourly_present = raw_snapshot.get("hourlyWindowPresent") is True
            weekly_present = raw_snapshot.get("weeklyWindowPresent") is True
            hourly_minutes = 7 * 24 * 60 if plan_type == "plus" and hourly_present and not weekly_present else 5 * 60
            hourly = reserve_window(raw_snapshot, "hourly", stale, snapshot_at, hourly_minutes)
            weekly = reserve_window(raw_snapshot, "weekly", stale, snapshot_at, 7 * 24 * 60)
            if plan_type == "plus" and hourly_present and not weekly_present:
                five_hour = {
                    "quota_available": False,
                    "quota_stale": stale,
                    "quota_unlimited": True,
                    "quota_source": "sidecar-reserve",
                    "quota_snapshot_at": snapshot_at,
                }
                seven_day = hourly
            else:
                five_hour = hourly
                seven_day = weekly
                if plan_type == "plus" and not hourly_present and weekly_present:
                    five_hour = {
                        "quota_available": False,
                        "quota_stale": stale,
                        "quota_unlimited": True,
                        "quota_source": "sidecar-reserve",
                        "quota_snapshot_at": snapshot_at,
                    }
            local_quota = {
                "window_5h": five_hour,
                "window_7d": seven_day,
                "window_cycle": {
                    "quota_available": False,
                    "quota_stale": stale,
                    "quota_source": "sidecar-reserve",
                    "quota_snapshot_at": snapshot_at,
                },
            }
            if label not in result or (
                quota_row_needs_official_refresh(result[label])
                and not quota_row_needs_official_refresh(local_quota)
            ):
                result[label] = local_quota

    # Resolve every Cockpit-owned local source before using account credentials
    # for a direct official request. A future sidecar version can therefore add
    # reset timestamps without causing duplicate network traffic.
    official_candidates = {
        account_id: account
        for account_id, account in manifest_by_id.items()
        if quota_row_needs_official_refresh(result.get(labels_by_id.get(account_id, "")))
    }
    official_by_id = cockpit_official_quota_by_account(home, official_candidates)
    for account_id, quota in official_by_id.items():
        label = labels_by_id.get(account_id)
        if label:
            result[label] = quota
    return result


def quota_window_start(
    window: dict[str, Any],
    now: datetime,
    duration: timedelta,
) -> datetime | None:
    if not window.get("quota_available") or window.get("quota_stale"):
        return None
    reset_at = parse_dt(window.get("resets_at"))
    if reset_at is None or reset_at > now + duration:
        return None
    if reset_at <= now:
        # A stale upstream snapshot may still expose the previous reset time.
        # That timestamp is also the lower bound of the newly reset window;
        # using it prevents old-cycle usage from leaking into a rolling window.
        return reset_at if now - reset_at <= duration else None
    start_at = reset_at - duration
    return start_at if start_at <= now else None


def quota_window_duration(window: dict[str, Any], fallback: timedelta) -> timedelta:
    try:
        minutes = int(window.get("window_minutes") or 0)
    except (TypeError, ValueError):
        minutes = 0
    return timedelta(minutes=minutes) if minutes > 0 else fallback


def add_cockpit_usage_to_bucket(
    bucket: UsageBucket,
    timestamp: Any,
    model: Any,
    input_tokens: Any,
    output_tokens: Any,
    total_tokens: Any,
    cached_tokens: Any,
    estimated_cost_usd: Any,
    cost_multiplier: float = 1.0,
    app_speed: str = "",
) -> bool:
    total_tokens = max(0, int(total_tokens or 0))
    input_tokens = max(0, int(input_tokens or 0))
    output_tokens = max(0, int(output_tokens or 0))
    cached_tokens = max(0, int(cached_tokens or 0))
    if total_tokens <= 0 and input_tokens <= 0 and output_tokens <= 0 and cached_tokens <= 0:
        return False
    model = codex_model_name(str(model or "codex"))
    bucket.requests += 1
    bucket.input_tokens += max(0, input_tokens - cached_tokens)
    bucket.cached_input_tokens += cached_tokens
    bucket.output_tokens += output_tokens
    event_total = total_tokens or (input_tokens + output_tokens + cached_tokens)
    try:
        cost = float(estimated_cost_usd or 0)
    except (TypeError, ValueError):
        cost = 0.0
    multiplier = max(1.0, cost_multiplier)
    calculated_cost = estimate_cost(
        model,
        max(0, input_tokens - cached_tokens),
        cached_tokens,
        output_tokens,
        pricing_tier="priority" if multiplier > 1 else "standard",
    )
    bucket.cost += calculated_cost if calculated_cost > 0 else cost * multiplier
    bucket.add_model(model, event_total)
    bucket.mark_latest(ms_to_local_datetime(timestamp), model, app_speed, multiplier)
    return True


def scan_cockpit_codex_accounts(root: Path, start: datetime, end: datetime) -> dict[str, UsageBucket]:
    db_path = root / ".antigravity_cockpit" / "codex_local_access_logs.sqlite"
    if not db_path.exists():
        return {}
    start_ms = local_epoch_ms(start)
    end_ms = local_epoch_ms(end)
    speed_by_label = cockpit_codex_speed_by_label(root)
    speed_markers = codex_speed_history(root, start, end)
    buckets: dict[str, UsageBucket] = {}
    try:
        con = sqlite3.connect(db_path)
        rows = con.execute(
            """
            SELECT
                timestamp,
                account_id,
                email,
                api_key_label,
                model_id,
                input_tokens,
                output_tokens,
                total_tokens,
                cached_tokens,
                estimated_cost_usd
            FROM request_logs
            WHERE timestamp >= ? AND timestamp < ?
            """,
            (start_ms, end_ms),
        ).fetchall()
        con.close()
    except sqlite3.Error:
        return {}

    for row in rows:
        (
            timestamp,
            account_id,
            email,
            api_key_label,
            model,
            input_tokens,
            output_tokens,
            total_tokens,
            cached_tokens,
            estimated_cost_usd,
        ) = row
        label = cockpit_account_label(str(account_id or ""), str(email or ""), str(api_key_label or ""))
        when = ms_to_local_datetime(timestamp)
        app_speed = codex_speed_at(speed_markers, when)
        if not app_speed:
            app_speed = str((speed_by_label.get(label) or {}).get("app_speed") or "")
        multiplier = codex_speed_cost_multiplier(app_speed)
        bucket = buckets.setdefault(label, UsageBucket())
        add_cockpit_usage_to_bucket(
            bucket,
            timestamp,
            model,
            input_tokens,
            output_tokens,
            total_tokens,
            cached_tokens,
            estimated_cost_usd,
            multiplier,
            app_speed,
        )
    return buckets


def scan_cockpit_codex_quota_windows(
    root: Path,
    quota_by_account: dict[str, dict[str, dict[str, Any]]],
    now: datetime,
    end: datetime,
) -> tuple[
    dict[str, UsageBucket],
    dict[str, UsageBucket],
    dict[str, UsageBucket],
    dict[str, datetime],
    dict[str, datetime],
    dict[str, datetime],
    dict[str, datetime],
]:
    db_path = root / ".antigravity_cockpit" / "codex_local_access_logs.sqlite"
    if not db_path.exists():
        return {}, {}, {}, {}, {}, {}, {}

    starts_5h: dict[str, datetime] = {}
    starts_7d: dict[str, datetime] = {}
    starts_cycle: dict[str, datetime] = {}
    for label, quota in quota_by_account.items():
        five_hour_window = quota.get("window_5h") or {}
        seven_day_window = quota.get("window_7d") or {}
        start_5h = quota_window_start(
            five_hour_window,
            now,
            quota_window_duration(five_hour_window, timedelta(hours=5)),
        )
        start_7d = quota_window_start(
            seven_day_window,
            now,
            quota_window_duration(seven_day_window, timedelta(days=7)),
        )
        cycle_window = quota.get("window_cycle") or {}
        try:
            cycle_minutes = int(cycle_window.get("window_minutes") or 0)
        except (TypeError, ValueError):
            cycle_minutes = 0
        start_cycle = (
            quota_window_start(cycle_window, now, timedelta(minutes=cycle_minutes))
            if cycle_minutes > 0
            else None
        )
        if start_5h is not None:
            starts_5h[label] = start_5h
        if start_7d is not None:
            starts_7d[label] = start_7d
        if start_cycle is not None:
            starts_cycle[label] = start_cycle
    all_starts = list(starts_5h.values()) + list(starts_7d.values()) + list(starts_cycle.values())
    if not all_starts:
        return {}, {}, {}, starts_5h, starts_7d, starts_cycle, {}

    query_start = min(all_starts) - timedelta(
        seconds=max(0, QUOTA_WINDOW_START_TOLERANCE_SECONDS)
    )
    speed_by_label = cockpit_codex_speed_by_label(root)
    speed_markers = codex_speed_history(root, query_start, end)
    buckets_5h = {label: UsageBucket() for label in starts_5h}
    buckets_7d = {label: UsageBucket() for label in starts_7d}
    buckets_cycle = {label: UsageBucket() for label in starts_cycle}
    latest_by_label: dict[str, datetime] = {}
    try:
        con = sqlite3.connect(db_path)
        rows = con.execute(
            """
            SELECT
                timestamp,
                account_id,
                email,
                api_key_label,
                model_id,
                input_tokens,
                output_tokens,
                total_tokens,
                cached_tokens,
                estimated_cost_usd
            FROM request_logs
            WHERE timestamp >= ? AND timestamp < ?
            """,
            (local_epoch_ms(query_start), local_epoch_ms(end)),
        ).fetchall()
        con.close()
    except sqlite3.Error:
        return {}, {}, {}, starts_5h, starts_7d, starts_cycle, {}

    for row in rows:
        (
            timestamp,
            account_id,
            email,
            api_key_label,
            model,
            input_tokens,
            output_tokens,
            total_tokens,
            cached_tokens,
            estimated_cost_usd,
        ) = row
        label = cockpit_account_label(str(account_id or ""), str(email or ""), str(api_key_label or ""))
        when = ms_to_local_datetime(timestamp)
        if when is None:
            continue
        previous_latest = latest_by_label.get(label)
        if previous_latest is None or when > previous_latest:
            latest_by_label[label] = when
        app_speed = codex_speed_at(speed_markers, when)
        if not app_speed:
            app_speed = str((speed_by_label.get(label) or {}).get("app_speed") or "")
        multiplier = codex_speed_cost_multiplier(app_speed)
        if label in starts_5h and when >= starts_5h[label]:
            add_cockpit_usage_to_bucket(
                buckets_5h[label],
                timestamp,
                model,
                input_tokens,
                output_tokens,
                total_tokens,
                cached_tokens,
                estimated_cost_usd,
                multiplier,
                app_speed,
            )
        if label in starts_7d and when >= starts_7d[label]:
            add_cockpit_usage_to_bucket(
                buckets_7d[label],
                timestamp,
                model,
                input_tokens,
                output_tokens,
                total_tokens,
                cached_tokens,
                estimated_cost_usd,
                multiplier,
                app_speed,
            )
        if label in starts_cycle and when >= starts_cycle[label]:
            add_cockpit_usage_to_bucket(
                buckets_cycle[label],
                timestamp,
                model,
                input_tokens,
                output_tokens,
                total_tokens,
                cached_tokens,
                estimated_cost_usd,
                multiplier,
                app_speed,
            )
    return buckets_5h, buckets_7d, buckets_cycle, starts_5h, starts_7d, starts_cycle, latest_by_label


def scan_cockpit_codex_account_markers(root: Path, start: datetime, end: datetime) -> list[AccountMarker]:
    db_path = root / ".antigravity_cockpit" / "codex_local_access_logs.sqlite"
    if not db_path.exists():
        return []
    start_ms = local_epoch_ms(start)
    end_ms = local_epoch_ms(end)
    try:
        con = sqlite3.connect(db_path)
        rows = con.execute(
            """
            SELECT
                timestamp,
                account_id,
                email,
                api_key_label,
                model_id,
                total_tokens,
                input_tokens,
                cached_tokens,
                output_tokens,
                event_key
            FROM request_logs
            WHERE timestamp >= ? AND timestamp < ?
            ORDER BY timestamp ASC
            """,
            (start_ms, end_ms),
        ).fetchall()
        con.close()
    except sqlite3.Error:
        return []

    markers: list[AccountMarker] = []
    for (
        timestamp,
        account_id,
        email,
        api_key_label,
        model,
        total_tokens,
        input_tokens,
        cached_tokens,
        output_tokens,
        event_key,
    ) in rows:
        when = ms_to_local_datetime(timestamp)
        if when is None:
            continue
        label = cockpit_account_label(str(account_id or ""), str(email or ""), str(api_key_label or ""))
        if label == "Codex local - Unknown":
            continue
        markers.append(
            AccountMarker(
                when=when,
                label=label,
                model=codex_model_name(str(model or "codex")),
                kind="request",
                total_tokens=max(0, int(total_tokens or 0)),
                input_tokens=max(0, int(input_tokens or 0)),
                cached_tokens=max(0, int(cached_tokens or 0)),
                output_tokens=max(0, int(output_tokens or 0)),
                event_key=str(event_key or "").strip(),
            )
        )
    return markers


SWITCH_LOG_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}T[^\s]+)\s+.*?\[Codex[^\]]+\].*?account_id=(?P<account_id>[^,\s]+)"
)


def parse_local_log_dt(value: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is not None:
            return dt.astimezone(LOCAL_TZ).replace(tzinfo=None)
        return dt
    except Exception:
        return None


def scan_cockpit_codex_switch_markers(root: Path, start: datetime, end: datetime) -> list[AccountMarker]:
    logs_dir = root / ".antigravity_cockpit" / "logs"
    if not logs_dir.exists():
        return []
    labels = cockpit_codex_account_label_by_id(root)
    markers: list[AccountMarker] = []
    scan_start = start - timedelta(days=7)
    for path in sorted(logs_dir.glob("app.log*")):
        try:
            modified = datetime.fromtimestamp(path.stat().st_mtime)
        except OSError:
            continue
        if modified < scan_start:
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        for line in lines:
            match = SWITCH_LOG_RE.search(line)
            if not match:
                continue
            when = parse_local_log_dt(match.group("ts"))
            if when is None or when >= end:
                continue
            account_id = match.group("account_id").strip()
            label = labels.get(account_id) or cockpit_account_label(account_id, "", "")
            markers.append(AccountMarker(when=when, label=label, model=CODEX_DEFAULT_MODEL, kind="switch"))

    markers.sort(key=lambda marker: marker.when)
    if markers:
        last_before_start = None
        in_range: list[AccountMarker] = []
        for marker in markers:
            if marker.when < start:
                last_before_start = marker
            elif marker.when < end:
                in_range.append(marker)
        if last_before_start is not None:
            in_range.insert(0, AccountMarker(when=start, label=last_before_start.label, model=last_before_start.model, kind="switch"))
        return in_range
    return []


def attribute_codex_events_to_account_markers(
    events: list[UsageEvent],
    markers: list[AccountMarker],
    cost_multiplier_by_label: dict[str, float] | None = None,
    attribution_ledger: dict[str, str] | None = None,
    current_label: str = "",
    now: datetime | None = None,
) -> dict[str, UsageBucket]:
    attributed = attribute_codex_events_by_account(
        events,
        markers,
        attribution_ledger,
        current_label,
        now,
    )
    return buckets_from_attributed_events(attributed, cost_multiplier_by_label)


def buckets_from_attributed_events(
    attributed: dict[str, list[UsageEvent]],
    cost_multiplier_by_label: dict[str, float] | None = None,
) -> dict[str, UsageBucket]:
    multipliers = cost_multiplier_by_label or {}
    buckets: dict[str, UsageBucket] = {}
    for label, account_events in attributed.items():
        bucket = buckets.setdefault(label, UsageBucket())
        multiplier = float(multipliers.get(label) or 1.0)
        for event in account_events:
            add_codex_event_to_bucket(bucket, event, multiplier)
    return buckets


def merge_codex_account_fallback_events(
    codex_accounts: dict[str, UsageBucket],
    attributed_events: dict[str, list[UsageEvent]],
    cost_multiplier_by_label: dict[str, float] | None = None,
    direct_latest: dict[str, datetime] | None = None,
) -> None:
    multipliers = cost_multiplier_by_label or {}
    latest_markers = direct_latest or {}
    for label, account_events in attributed_events.items():
        direct_bucket = codex_accounts.get(label)
        cutoff = direct_bucket.latest_at if direct_bucket and direct_bucket.latest_at is not None else latest_markers.get(label)
        filtered = UsageBucket()
        multiplier = float(multipliers.get(label) or 1.0)
        for event in account_events:
            event_time = usage_event_attribution_time(event)
            if cutoff is not None and event_time <= cutoff + timedelta(seconds=2):
                continue
            add_codex_event_to_bucket(filtered, event, multiplier, bucket_time=event_time)
        if filtered.requests or filtered.total_tokens or filtered.cost:
            add_bucket(codex_accounts.setdefault(label, UsageBucket()), filtered)


def latest_marker_by_label(markers: list[AccountMarker]) -> dict[str, datetime]:
    latest: dict[str, datetime] = {}
    for marker in markers:
        if marker.kind != "request":
            continue
        previous = latest.get(marker.label)
        if previous is None or marker.when > previous:
            latest[marker.label] = marker.when
    return latest


def account_label_at_time(
    event: UsageEvent,
    switch_markers: list[AccountMarker],
    switch_times: list[datetime],
    request_markers: list[AccountMarker],
    request_times: list[datetime],
) -> str:
    event_time = usage_event_account_time(event)
    label = ""
    if switch_markers:
        switch_pos = bisect_right(switch_times, event_time) - 1
        if switch_pos >= 0:
            label = switch_markers[switch_pos].label
    if label:
        return label

    pos = bisect_left(request_times, event_time)
    best_marker: AccountMarker | None = None
    best_delta = float("inf")
    for idx in (pos - 1, pos):
        if idx < 0 or idx >= len(request_markers):
            continue
        delta = abs((event_time - request_markers[idx].when).total_seconds())
        if delta < best_delta:
            best_delta = delta
            best_marker = request_markers[idx]
    return (
        best_marker.label
        if best_marker is not None and best_delta <= CODEX_ACCOUNT_MATCH_WINDOW_SECONDS
        else UNASSIGNED_CODEX_LABEL
    )


def attribute_codex_events_by_account(
    events: list[UsageEvent],
    markers: list[AccountMarker],
    attribution_ledger: dict[str, str] | None = None,
    current_label: str = "",
    now: datetime | None = None,
) -> dict[str, list[UsageEvent]]:
    attributed: dict[str, list[UsageEvent]] = {}
    if not events:
        return attributed
    if not markers:
        for event in events:
            label, event_id = ledger_label_for_event(event, attribution_ledger)
            if not label:
                label = UNASSIGNED_CODEX_LABEL
                if (
                    current_label
                    and now is not None
                    and 0 <= (now - usage_event_account_time(event)).total_seconds() <= CODEX_CURRENT_ACCOUNT_RECENT_SECONDS
                ):
                    label = current_label
                if attribution_ledger is not None and event_id:
                    attribution_ledger[event_id] = label
            attributed.setdefault(label, []).append(event)
        return attributed

    markers = sorted(markers, key=lambda marker: marker.when)
    switch_markers = [marker for marker in markers if marker.kind == "switch"]
    switch_times = [marker.when for marker in switch_markers]
    request_markers = [marker for marker in markers if marker.kind != "switch"]
    request_times = [marker.when for marker in request_markers]
    ledger = attribution_ledger
    for event in events:
        label, event_id = ledger_label_for_event(event, ledger)
        if not label:
            label = account_label_at_time(event, switch_markers, switch_times, request_markers, request_times)
            if (
                label == UNASSIGNED_CODEX_LABEL
                and current_label
                and now is not None
                and 0 <= (now - usage_event_account_time(event)).total_seconds() <= CODEX_CURRENT_ACCOUNT_RECENT_SECONDS
            ):
                label = current_label
            if ledger is not None and event_id:
                ledger[event_id] = label
        attributed.setdefault(label, []).append(event)
    return attributed


def scan_claude(root: Path, start: datetime, end: datetime) -> UsageBucket:
    bucket = UsageBucket()
    for path in iter_recent_jsonl(root, start):
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = parse_dt(row.get("timestamp"))
            if ts is None or ts < start or ts >= end:
                continue
            message = row.get("message") or {}
            if message.get("role") != "assistant":
                continue
            usage = message.get("usage") or {}
            if not usage:
                continue
            input_tokens = int(usage.get("input_tokens") or 0)
            output_tokens = int(usage.get("output_tokens") or 0)
            cache_creation = int(usage.get("cache_creation_input_tokens") or 0)
            cache_read = int(usage.get("cache_read_input_tokens") or 0)
            total = input_tokens + output_tokens + cache_creation + cache_read
            if total <= 0:
                continue
            model = str(message.get("model") or row.get("model") or "claude")
            bucket.requests += 1
            bucket.input_tokens += input_tokens
            bucket.output_tokens += output_tokens
            bucket.cache_creation_input_tokens += cache_creation
            bucket.cache_read_input_tokens += cache_read
            bucket.cost += estimate_cost(
                model,
                input_tokens,
                cache_read,
                output_tokens,
                cache_creation_tokens=cache_creation,
            )
            bucket.add_model(model, total)
            bucket.mark_latest(ts, model)
    return bucket


def scan_claude_hourly(root: Path, start: datetime, end: datetime) -> list[dict[str, Any]]:
    buckets = [
        {"hour": hour, "requests": 0, "tokens": 0, "cost": 0.0}
        for hour in range(24)
    ]
    for path in iter_recent_jsonl(root, start):
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = parse_dt(row.get("timestamp"))
            if ts is None or ts < start or ts >= end:
                continue
            message = row.get("message") or {}
            if message.get("role") != "assistant":
                continue
            usage = message.get("usage") or {}
            if not usage:
                continue
            input_tokens = int(usage.get("input_tokens") or 0)
            output_tokens = int(usage.get("output_tokens") or 0)
            cache_creation = int(usage.get("cache_creation_input_tokens") or 0)
            cache_read = int(usage.get("cache_read_input_tokens") or 0)
            total = input_tokens + output_tokens + cache_creation + cache_read
            if total <= 0:
                continue
            model = str(message.get("model") or row.get("model") or "claude")
            bucket = buckets[max(0, min(23, ts.hour))]
            bucket["requests"] += 1
            bucket["tokens"] += total
            bucket["cost"] += estimate_cost(
                model,
                input_tokens,
                cache_read,
                output_tokens,
                cache_creation_tokens=cache_creation,
            )
    for bucket in buckets:
        bucket["cost"] = round(float(bucket["cost"] or 0), 6)
    return buckets


def codex_hourly_from_events(events: list[UsageEvent]) -> list[dict[str, Any]]:
    buckets = [
        {"hour": hour, "requests": 0, "tokens": 0, "cost": 0.0}
        for hour in range(24)
    ]
    for event in events:
        hour = max(0, min(23, event.when.hour))
        bucket = buckets[hour]
        bucket["requests"] += 1
        bucket["tokens"] += event.total_tokens
        multiplier = event.cost_multiplier if event.cost_multiplier is not None else 1.0
        pricing_tier = event.pricing_tier or ("priority" if float(multiplier or 1.0) > 1 else "standard")
        bucket["cost"] += estimate_cost(
            event.model,
            event.input_tokens,
            event.cached_tokens,
            event.output_tokens,
            pricing_tier=pricing_tier,
        )
    for bucket in buckets:
        bucket["cost"] = round(float(bucket["cost"] or 0), 6)
    return buckets


def merge_hourly_buckets(*sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged = [
        {"hour": hour, "requests": 0, "tokens": 0, "cost": 0.0}
        for hour in range(24)
    ]
    for source in sources:
        for row in source:
            if not isinstance(row, dict):
                continue
            hour = max(0, min(23, int(row.get("hour") or 0)))
            merged[hour]["requests"] += int(row.get("requests") or 0)
            merged[hour]["tokens"] += int(row.get("tokens") or 0)
            merged[hour]["cost"] += float(row.get("cost") or 0)
    for bucket in merged:
        bucket["cost"] = round(float(bucket["cost"] or 0), 6)
    return merged


def mark_codex_failure_hours(
    buckets: list[dict[str, Any]],
    failures: list[CodexFailureEvent],
    day: date,
    as_of: datetime,
) -> None:
    by_hour = {
        max(0, min(23, int(bucket.get("hour") or 0))): bucket
        for bucket in buckets
        if isinstance(bucket, dict)
    }
    for bucket in by_hour.values():
        bucket.pop("failure", None)
        bucket.pop("failure_count", None)
        bucket.pop("failure_at", None)
        bucket.pop("failure_kind", None)

    if as_of.date() < day:
        return
    last_observed_hour = as_of.hour if as_of.date() == day else 23
    for failure in failures:
        if failure.when.date() != day:
            continue
        candidate_hour = failure.when.hour
        candidate = by_hour.get(candidate_hour)
        if candidate is None or candidate_hour > last_observed_hour:
            continue
        candidate["failure"] = True
        candidate["failure_count"] = int(candidate.get("failure_count") or 0) + 1
        failure_at = failure.when.replace(tzinfo=LOCAL_TZ).isoformat(timespec="seconds")
        if failure_at > str(candidate.get("failure_at") or ""):
            candidate["failure_at"] = failure_at
            candidate["failure_kind"] = failure.kind


def latest_at_text(bucket: UsageBucket) -> str:
    if bucket.latest_at is None:
        return ""
    return bucket.latest_at.replace(tzinfo=LOCAL_TZ).isoformat(timespec="seconds")


def latest_request_from_attributed_events(
    attributed: dict[str, list[UsageEvent]],
    account_markers: list[AccountMarker] | None = None,
    session_account_labels: dict[str, str] | None = None,
) -> dict[str, Any]:
    latest_label = ""
    latest_event: UsageEvent | None = None
    for label, events in attributed.items():
        for event in events:
            if latest_event is None or event.when > latest_event.when:
                latest_label = label
                latest_event = event
            elif latest_event is not None and event.when == latest_event.when:
                if "@" in label and "@" not in latest_label:
                    latest_label = label
                    latest_event = event
    if latest_event is None:
        return {}
    if is_api_service_mirror_label(latest_label):
        latest_label = (
            concrete_api_service_account_label(latest_event, account_markers or [])
            or (session_account_labels or {}).get(latest_event.session_id, "")
            or API_SERVICE_AGGREGATE_LABEL
        )
    return {
        "provider": latest_label,
        "model": latest_event.model,
        "created_at": latest_event.when.replace(tzinfo=LOCAL_TZ).isoformat(timespec="seconds"),
        "kind": "success",
    }


def is_api_service_mirror_label(label: str) -> bool:
    name = str(label or "").strip().lower()
    if name.startswith("codex local - "):
        name = name[len("codex local - "):].strip()
    return name in API_SERVICE_MIRROR_LABELS


def account_markers_by_total_tokens(markers: list[AccountMarker]) -> dict[int, list[AccountMarker]]:
    indexed: dict[int, list[AccountMarker]] = {}
    for marker in markers:
        if marker.kind == "request" and marker.total_tokens > 0:
            indexed.setdefault(marker.total_tokens, []).append(marker)
    return indexed


def concrete_api_service_account_marker(
    event: UsageEvent,
    markers: list[AccountMarker],
    marker_index: dict[int, list[AccountMarker]] | None = None,
) -> AccountMarker | None:
    # Cockpit writes request markers when the response finishes, matching the
    # Codex token_count timestamp rather than the task-start attribution edge.
    event_time = event.when
    exact_candidates = (
        (marker_index or {}).get(event.total_tokens, [])
        if marker_index is not None
        else [
            marker
            for marker in markers
            if marker.kind == "request" and marker.total_tokens == event.total_tokens
        ]
    )
    nearby = [
        marker
        for marker in exact_candidates
        if abs((marker.when - event_time).total_seconds()) <= API_SERVICE_ACTIVITY_MATCH_SECONDS
    ]
    if nearby:
        return min(nearby, key=lambda marker: abs((marker.when - event_time).total_seconds()))
    fuzzy_token_delta = max(256, int(event.total_tokens * 0.005))
    fuzzy_candidates = [
        marker
        for marker in markers
        if marker.kind == "request"
        and abs((marker.when - event_time).total_seconds()) <= 30
        and abs(marker.total_tokens - event.total_tokens) <= fuzzy_token_delta
    ]
    if fuzzy_candidates:
        return min(
            fuzzy_candidates,
            key=lambda marker: (
                abs(marker.total_tokens - event.total_tokens),
                abs((marker.when - event_time).total_seconds()),
            ),
        )
    if len(exact_candidates) == 1:
        return exact_candidates[0]
    return None


def concrete_api_service_account_label(
    event: UsageEvent,
    markers: list[AccountMarker],
    marker_index: dict[int, list[AccountMarker]] | None = None,
) -> str:
    marker = concrete_api_service_account_marker(event, markers, marker_index)
    return marker.label if marker is not None else ""


def previous_active_session_account_labels(path: Path, day: date) -> dict[str, str]:
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if str(existing.get("date") or "") != day.isoformat():
        return {}
    sessions = existing.get("active_sessions")
    if not isinstance(sessions, list):
        return {}
    result: dict[str, str] = {}
    for session in sessions:
        if not isinstance(session, dict):
            continue
        session_id = str(session.get("session_id") or "").strip()
        provider = str(session.get("provider") or "").strip()
        if session_id and provider and not is_api_service_mirror_label(provider):
            result[session_id] = provider
    return result


def resolve_api_service_event_accounts(
    attributed: dict[str, list[UsageEvent]],
    account_markers: list[AccountMarker],
    known_session_accounts: dict[str, str] | None = None,
) -> tuple[dict[str, list[UsageEvent]], dict[str, str], int]:
    marker_index = account_markers_by_total_tokens(account_markers)
    session_accounts = dict(known_session_accounts or {})
    resolved: dict[str, list[UsageEvent]] = {}
    unresolved = 0
    ordered = sorted(
        (
            (label, event)
            for label, events in attributed.items()
            for event in events
        ),
        key=lambda item: usage_event_attribution_time(item[1]),
    )
    for label, event in ordered:
        session_id = event.session_id or event.request_key or codex_event_id(event)
        resolved_label = label
        matched_marker = concrete_api_service_account_marker(event, account_markers, marker_index)
        if matched_marker is not None:
            resolved_label = matched_marker.label
            session_accounts[session_id] = matched_marker.label
            if matched_marker.model:
                event.model = matched_marker.model
        elif is_api_service_mirror_label(label):
            resolved_label = session_accounts.get(session_id, "") or label
            if is_api_service_mirror_label(resolved_label):
                unresolved += 1
        else:
            session_accounts[session_id] = label
        resolved.setdefault(resolved_label, []).append(event)
    return resolved, session_accounts, unresolved


def build_active_session_rows(
    attributed_events: dict[str, list[UsageEvent]],
    session_account_labels: dict[str, str],
    session_lifecycle: dict[str, SessionLifecycle],
    current_label: str,
    now: datetime,
) -> tuple[list[dict[str, Any]], dict[str, int], dict[str, int], int]:
    """Build concurrency from task lifecycle, with token activity as a legacy fallback."""
    latest_by_session: dict[str, tuple[str, UsageEvent]] = {}
    labels_by_session = dict(session_account_labels)
    for label, account_events in attributed_events.items():
        for event in account_events:
            if event.route == "cockpit-db-fallback":
                continue
            session_id = (event.session_id or event.request_key or codex_event_id(event)).strip()
            if not session_id:
                continue
            previous = latest_by_session.get(session_id)
            if previous is None or usage_event_attribution_time(event) > usage_event_attribution_time(previous[1]):
                latest_by_session[session_id] = (label, event)

    active_sessions: list[dict[str, Any]] = []
    unresolved = 0

    def add_active(
        session_id: str,
        label: str,
        event: UsageEvent | None,
        activity_at: datetime,
        source: str,
        started_at: datetime | None = None,
    ) -> None:
        nonlocal unresolved
        resolved_label = label if label and not is_api_service_mirror_label(label) else ""
        if not resolved_label:
            resolved_label = labels_by_session.get(session_id, "")
            if is_api_service_mirror_label(resolved_label):
                resolved_label = ""
        if not resolved_label and current_label and not is_api_service_mirror_label(current_label):
            resolved_label = current_label
        if not resolved_label:
            unresolved += 1
        active_sessions.append(
            {
                "session_id": session_id,
                "provider": resolved_label,
                "model": event.model if event is not None else CODEX_DEFAULT_MODEL,
                "latest_at": activity_at.replace(tzinfo=LOCAL_TZ).isoformat(timespec="seconds"),
                "started_at": (
                    started_at.replace(tzinfo=LOCAL_TZ).isoformat(timespec="seconds")
                    if started_at is not None
                    else ""
                ),
                "tokens": event.total_tokens if event is not None else 0,
                "active": True,
                "activity_source": source,
            }
        )

    for session_id, lifecycle in session_lifecycle.items():
        if lifecycle.state != "task_started":
            continue
        file_activity_at = lifecycle.file_activity_at or lifecycle.when
        activity_age = (now - file_activity_at).total_seconds()
        if activity_age < -300 or activity_age > max(1, CLIENT_USAGE_ACTIVE_TASK_STALE_SECONDS):
            continue
        event_label, event = latest_by_session.get(session_id, ("", None))
        event_at = usage_event_attribution_time(event) if event is not None else lifecycle.when
        activity_at = max(lifecycle.when, file_activity_at, event_at)
        add_active(
            session_id,
            event_label,
            event,
            activity_at,
            "task-lifecycle",
            lifecycle.when,
        )

    # Older Codex logs may not contain task lifecycle events. Only in that
    # case retain a short token-activity fallback instead of a five-minute lag.
    if not session_lifecycle:
        recent_cutoff = now - timedelta(seconds=max(1, CLIENT_USAGE_ACTIVE_WINDOW_SECONDS))
        for session_id, (label, event) in latest_by_session.items():
            activity_at = usage_event_attribution_time(event)
            if activity_at < recent_cutoff:
                continue
            add_active(
                session_id,
                label,
                event,
                activity_at,
                "token-activity-fallback",
            )

    active_sessions.sort(key=lambda row: str(row.get("latest_at") or ""), reverse=True)
    active_by_label: dict[str, int] = {}
    sessions_by_label: dict[str, int] = {}
    for row in active_sessions:
        label = str(row.get("provider") or "")
        if not label:
            continue
        active_by_label[label] = active_by_label.get(label, 0) + 1
        sessions_by_label[label] = sessions_by_label.get(label, 0) + 1
    return active_sessions, active_by_label, sessions_by_label, unresolved


def cockpit_marker_identity(marker: AccountMarker) -> tuple[str, str, int, str]:
    return (
        marker.when.isoformat(timespec="milliseconds"),
        marker.label,
        marker.total_tokens,
        marker.event_key,
    )


def merge_missing_cockpit_account_events(
    attributed: dict[str, list[UsageEvent]],
    account_markers: list[AccountMarker],
) -> tuple[dict[str, list[UsageEvent]], int]:
    merged = {label: list(events) for label, events in attributed.items()}
    marker_index = account_markers_by_total_tokens(account_markers)
    represented: set[tuple[str, str, int, str]] = set()
    for events in attributed.values():
        for event in events:
            marker = concrete_api_service_account_marker(event, account_markers, marker_index)
            if marker is not None:
                represented.add(cockpit_marker_identity(marker))

    added = 0
    for marker in account_markers:
        if marker.total_tokens <= 0 or cockpit_marker_identity(marker) in represented:
            continue
        cached = min(marker.cached_tokens, marker.total_tokens)
        output = min(marker.output_tokens, max(0, marker.total_tokens - cached))
        uncached_input = max(0, marker.total_tokens - cached - output)
        event = UsageEvent(
            when=marker.when,
            model=marker.model or CODEX_DEFAULT_MODEL,
            input_tokens=uncached_input,
            cached_tokens=cached,
            output_tokens=output,
            session_id="",
            request_key=marker.event_key or f"cockpit-{marker.when.timestamp()}-{marker.total_tokens}",
            route="cockpit-db-fallback",
            request_at=marker.when,
        )
        merged.setdefault(marker.label, []).append(event)
        added += 1
    return merged, added


def backfill_usage_history_details(home: Path, sessions_root: Path) -> int:
    try:
        history = json.loads(USAGE_HISTORY_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    days = history.get("days") if isinstance(history, dict) else None
    if not isinstance(days, dict):
        return 0
    missing: list[date] = []
    for key, row in days.items():
        if not isinstance(row, dict) or int(row.get("tokens") or 0) <= 0:
            continue
        if isinstance(row.get("providers"), list) and isinstance(row.get("models"), dict):
            continue
        try:
            missing.append(datetime.fromisoformat(str(key)).date())
        except ValueError:
            continue
    if not missing:
        return 0

    start = datetime.combine(min(missing), datetime.min.time())
    end = datetime.combine(max(missing) + timedelta(days=1), datetime.min.time())
    events = scan_all_codex_events(home, sessions_root, start, end)
    apply_codex_speed_fallback(events, codex_speed_history(home, start, end))
    account_markers = scan_cockpit_codex_account_markers(home, start, end)
    markers = scan_cockpit_codex_switch_markers(home, start, end)
    markers.extend(load_account_timeline())
    markers.extend(account_markers)
    attributed = attribute_codex_events_by_account(
        events,
        markers,
        load_attribution_ledger(),
        current_codex_account_label(home),
        datetime.now(),
    )
    resolved, _session_accounts, _unresolved = resolve_api_service_event_accounts(
        attributed,
        account_markers,
    )
    speed_by_account = cockpit_codex_speed_by_label(home)
    multipliers = {
        label: float(meta.get("cost_multiplier") or 1.0)
        for label, meta in speed_by_account.items()
    }
    wanted = {item.isoformat() for item in missing}
    buckets_by_day: dict[str, dict[str, UsageBucket]] = {}
    for label, account_events in resolved.items():
        for event in account_events:
            key = usage_event_attribution_time(event).date().isoformat()
            if key not in wanted:
                continue
            bucket = buckets_by_day.setdefault(key, {}).setdefault(label, UsageBucket())
            add_codex_event_to_bucket(
                bucket,
                event,
                multipliers.get(label, 1.0),
                bucket_time=usage_event_attribution_time(event),
            )

    updated = 0
    for key in wanted:
        row = days.get(key)
        buckets = buckets_by_day.get(key)
        if not isinstance(row, dict) or not buckets:
            continue
        providers = [
            bucket_to_dict(label, bucket)
            for label, bucket in sorted(
                buckets.items(),
                key=lambda item: (-item[1].total_tokens, item[0]),
            )
            if bucket.total_tokens > 0 or bucket.requests > 0
        ]
        models: dict[str, int] = {}
        for provider in providers:
            for model, tokens in (provider.get("models") or {}).items():
                models[str(model)] = models.get(str(model), 0) + int(tokens or 0)
        row["providers"] = providers
        row["models"] = models
        row["detail_tokens"] = sum(int(provider.get("tokens") or 0) for provider in providers)
        updated += 1
    if updated:
        history["schema"] = max(2, int(history.get("schema") or 1))
        write_json_atomic(USAGE_HISTORY_PATH, history)
    return updated


def collapse_api_service_mirror_providers(output: dict[str, Any]) -> dict[str, Any]:
    providers = output.get("providers")
    if not isinstance(providers, list):
        return {}
    mirrors = [
        provider
        for provider in providers
        if isinstance(provider, dict) and is_api_service_mirror_label(str(provider.get("name") or ""))
    ]
    if not mirrors:
        return {}
    latest = max(mirrors, key=lambda row: parse_dt(row.get("latest_at")) or datetime.min)
    aggregate = {
        "name": API_SERVICE_AGGREGATE_LABEL,
        "requests": sum(int(row.get("requests") or 0) for row in mirrors),
        "tokens": sum(int(row.get("tokens") or 0) for row in mirrors),
        "input_tokens": sum(int(row.get("input_tokens") or 0) for row in mirrors),
        "cached_input_tokens": sum(int(row.get("cached_input_tokens") or 0) for row in mirrors),
        "cache_creation_input_tokens": sum(int(row.get("cache_creation_input_tokens") or 0) for row in mirrors),
        "output_tokens": sum(int(row.get("output_tokens") or 0) for row in mirrors),
        "cost": round(sum(float(row.get("cost") or 0) for row in mirrors), 6),
        "models": {},
        "latest_at": str(latest.get("latest_at") or ""),
        "latest_model": str(latest.get("latest_model") or ""),
        "recent_active": sum(int(row.get("recent_active") or 0) for row in mirrors),
        "recent_sessions": sum(int(row.get("recent_sessions") or 0) for row in mirrors),
        "show_zero": False,
        "is_api_service_aggregate": True,
    }
    for row in mirrors:
        models = row.get("models")
        if not isinstance(models, dict):
            continue
        for model, tokens in models.items():
            aggregate["models"][str(model)] = aggregate["models"].get(str(model), 0) + int(tokens or 0)
    providers[:] = [
        provider
        for provider in providers
        if not (isinstance(provider, dict) and is_api_service_mirror_label(str(provider.get("name") or "")))
    ] + [aggregate]
    output["api_service_aggregate"] = {
        "requests": aggregate["requests"],
        "tokens": aggregate["tokens"],
        "cost": aggregate["cost"],
    }
    output.pop("api_service_mirror_deduction", None)
    return aggregate


def load_usage_history_for_backfill() -> dict[str, Any]:
    try:
        history = json.loads(USAGE_HISTORY_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"schema": 2, "days": {}}
    if not isinstance(history, dict):
        return {"schema": 2, "days": {}}
    if not isinstance(history.get("days"), dict):
        history["days"] = {}
    return history


def latest_history_observation(history: dict[str, Any]) -> datetime | None:
    candidates: list[datetime] = []
    offline_sync = history.get("offline_sync")
    if isinstance(offline_sync, dict):
        for key in ("last_successful_at", "completed_at"):
            parsed = parse_dt(offline_sync.get(key))
            if parsed is not None:
                candidates.append(parsed)
    days = history.get("days")
    if isinstance(days, dict):
        for row in days.values():
            if not isinstance(row, dict):
                continue
            parsed = parse_dt(row.get("updated_at"))
            if parsed is not None:
                candidates.append(parsed)
    return max(candidates) if candidates else None


def offline_history_dates_to_reconcile(
    history: dict[str, Any],
    now: datetime,
    max_days: int = OFFLINE_HISTORY_BACKFILL_MAX_DAYS,
) -> list[date]:
    max_days = max(0, int(max_days or 0))
    if max_days <= 0:
        return []
    today = now.date()
    last_complete_day = today - timedelta(days=1)
    floor = today - timedelta(days=max_days)
    raw_days = history.get("days")
    days = raw_days if isinstance(raw_days, dict) else {}
    known_dates: list[date] = []
    for key in days:
        try:
            parsed = date.fromisoformat(str(key))
        except ValueError:
            continue
        if parsed < today:
            known_dates.append(parsed)

    targets: set[date] = set()
    observed_at = latest_history_observation(history)
    if observed_at is not None:
        if observed_at.date() < today:
            cursor = max(floor, observed_at.date())
            while cursor <= last_complete_day:
                targets.add(cursor)
                cursor += timedelta(days=1)
    elif known_dates:
        targets.add(max(floor, max(known_dates)))
    else:
        targets.add(last_complete_day)

    if known_dates:
        cursor = max(floor, min(known_dates))
        while cursor <= last_complete_day:
            if cursor.isoformat() not in days:
                targets.add(cursor)
            cursor += timedelta(days=1)

    # The floating monitor always renders a seven-day chart. A history file can
    # be created after local logs already exist, so looking only forward from
    # the earliest persisted row leaves older chart slots permanently empty.
    # Reconcile every missing closed day in the visible chart window once;
    # zero-usage rows are persisted too, preventing repeated scans.
    chart_floor = max(floor, today - timedelta(days=7))
    cursor = chart_floor
    while cursor <= last_complete_day:
        if cursor.isoformat() not in days:
            targets.add(cursor)
        cursor += timedelta(days=1)
    return sorted(day for day in targets if floor <= day < today)


def scan_claude_daily_buckets(
    root: Path,
    start: datetime,
    end: datetime,
) -> dict[date, UsageBucket]:
    buckets: dict[date, UsageBucket] = {}
    for path in iter_recent_jsonl(root, start):
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            when = parse_dt(row.get("timestamp"))
            if when is None or when < start or when >= end:
                continue
            message = row.get("message") or {}
            if message.get("role") != "assistant":
                continue
            usage = message.get("usage") or {}
            if not usage:
                continue
            input_tokens = int(usage.get("input_tokens") or 0)
            output_tokens = int(usage.get("output_tokens") or 0)
            cache_creation = int(usage.get("cache_creation_input_tokens") or 0)
            cache_read = int(usage.get("cache_read_input_tokens") or 0)
            total_tokens = input_tokens + output_tokens + cache_creation + cache_read
            if total_tokens <= 0:
                continue
            model = str(message.get("model") or row.get("model") or "claude")
            bucket = buckets.setdefault(when.date(), UsageBucket())
            bucket.requests += 1
            bucket.input_tokens += input_tokens
            bucket.output_tokens += output_tokens
            bucket.cache_creation_input_tokens += cache_creation
            bucket.cache_read_input_tokens += cache_read
            bucket.cost += estimate_cost(
                model,
                input_tokens,
                cache_read,
                output_tokens,
                cache_creation_tokens=cache_creation,
            )
            bucket.add_model(model, total_tokens)
            bucket.mark_latest(when, model)
    return buckets


def build_historical_usage_rows(
    home: Path,
    sessions_root: Path,
    target_days: list[date],
    attribution_ledger: dict[str, str],
    now: datetime,
) -> dict[str, dict[str, Any]]:
    if not target_days:
        return {}
    wanted = {item.isoformat() for item in target_days}
    start = datetime.combine(min(target_days), datetime.min.time())
    end = datetime.combine(max(target_days) + timedelta(days=1), datetime.min.time())
    events = scan_all_codex_events(home, sessions_root, start, end)
    apply_codex_speed_fallback(events, codex_speed_history(home, start, end))
    account_markers = scan_cockpit_codex_account_markers(home, start, end)
    markers = scan_cockpit_codex_switch_markers(home, start, end)
    markers.extend(load_account_timeline())
    markers.extend(account_markers)
    attributed = attribute_codex_events_by_account(
        events,
        markers,
        attribution_ledger,
        current_codex_account_label(home),
        now,
    )
    attributed, _fallback_events = merge_missing_cockpit_account_events(
        attributed,
        account_markers,
    )
    resolved, _session_accounts, _unresolved = resolve_api_service_event_accounts(
        attributed,
        account_markers,
    )
    speed_by_account = cockpit_codex_speed_by_label(home)
    multipliers = {
        label: float(meta.get("cost_multiplier") or 1.0)
        for label, meta in speed_by_account.items()
    }
    codex_by_day: dict[str, dict[str, UsageBucket]] = {}
    for label, account_events in resolved.items():
        for event in account_events:
            event_time = usage_event_attribution_time(event)
            key = event_time.date().isoformat()
            if key not in wanted:
                continue
            bucket = codex_by_day.setdefault(key, {}).setdefault(label, UsageBucket())
            add_codex_event_to_bucket(
                bucket,
                event,
                multipliers.get(label, 1.0),
                bucket_time=event_time,
            )

    claude_by_day = scan_claude_daily_buckets(home / ".claude" / "projects", start, end)
    updated_at = now.replace(tzinfo=LOCAL_TZ).isoformat(timespec="seconds")
    rows: dict[str, dict[str, Any]] = {}
    for target_day in target_days:
        key = target_day.isoformat()
        account_buckets = codex_by_day.get(key, {})
        providers = [
            bucket_to_dict(label, bucket)
            for label, bucket in sorted(
                account_buckets.items(),
                key=lambda item: (-item[1].total_tokens, item[0]),
            )
            if bucket.total_tokens > 0 or bucket.requests > 0
        ]
        total = UsageBucket()
        for bucket in account_buckets.values():
            add_bucket(total, bucket)
        claude = claude_by_day.get(target_day, UsageBucket())
        if claude.total_tokens > 0 or claude.requests > 0:
            providers.append(bucket_to_dict("Claude local", claude))
            add_bucket(total, claude)
        temporary_output = {
            "today": bucket_to_dict("Client local", total),
            "providers": providers,
        }
        collapse_api_service_mirror_providers(temporary_output)
        providers = temporary_output["providers"]
        providers.sort(
            key=lambda provider: (
                -int(provider.get("tokens") or 0),
                str(provider.get("name") or ""),
            )
        )
        models: dict[str, int] = {}
        for provider in providers:
            provider_models = provider.get("models")
            if not isinstance(provider_models, dict):
                continue
            for model, tokens in provider_models.items():
                name = str(model or "unknown")
                models[name] = models.get(name, 0) + int(tokens or 0)
        total_row = temporary_output["today"]
        rows[key] = {
            "date": key,
            "source": "local-backfill",
            "requests": int(total_row.get("requests") or 0),
            "tokens": int(total_row.get("tokens") or 0),
            "input_tokens": int(total_row.get("input_tokens") or 0),
            "cached_input_tokens": int(total_row.get("cached_input_tokens") or 0),
            "cache_creation_input_tokens": int(
                total_row.get("cache_creation_input_tokens") or 0
            ),
            "output_tokens": int(total_row.get("output_tokens") or 0),
            "cost": round(float(total_row.get("cost") or 0), 6),
            "models": models,
            "providers": providers,
            "detail_tokens": sum(int(provider.get("tokens") or 0) for provider in providers),
            "updated_at": updated_at,
            "source_date": key,
        }
    return rows


def history_row_signature(row: dict[str, Any]) -> tuple[Any, ...]:
    return (
        int(row.get("requests") or 0),
        int(row.get("tokens") or 0),
        int(row.get("input_tokens") or 0),
        int(row.get("cached_input_tokens") or 0),
        int(row.get("cache_creation_input_tokens") or 0),
        int(row.get("output_tokens") or 0),
        round(float(row.get("cost") or 0), 6),
        json.dumps(row.get("models") or {}, ensure_ascii=False, sort_keys=True),
        json.dumps(row.get("providers") or [], ensure_ascii=False, sort_keys=True),
    )


def merge_rebuilt_history_day(
    existing: dict[str, Any] | None,
    rebuilt: dict[str, Any],
    reconciled_at: str,
) -> tuple[dict[str, Any], bool]:
    if not isinstance(existing, dict):
        merged = dict(rebuilt)
        merged["offline_reconciled_at"] = reconciled_at
        return merged, True

    existing_tokens = int(existing.get("tokens") or 0)
    rebuilt_tokens = int(rebuilt.get("tokens") or 0)
    existing_detail = sum(
        int(provider.get("tokens") or 0)
        for provider in (existing.get("providers") or [])
        if isinstance(provider, dict)
    )
    rebuilt_detail = int(rebuilt.get("detail_tokens") or 0)
    use_rebuilt = rebuilt_tokens > existing_tokens or (
        rebuilt_tokens == existing_tokens and rebuilt_detail > existing_detail
    )
    merged = dict(rebuilt if use_rebuilt else existing)
    merged["date"] = str(existing.get("date") or rebuilt.get("date") or "")
    merged["source"] = str(existing.get("source") or rebuilt.get("source") or "local-backfill")
    merged["source_date"] = str(
        existing.get("source_date") or rebuilt.get("source_date") or merged["date"]
    )
    merged["requests"] = max(
        int(existing.get("requests") or 0),
        int(rebuilt.get("requests") or 0),
    )
    merged["tokens"] = max(existing_tokens, rebuilt_tokens)
    merged["cost"] = round(
        max(float(existing.get("cost") or 0), float(rebuilt.get("cost") or 0)),
        6,
    )
    if rebuilt_detail > existing_detail and isinstance(rebuilt.get("providers"), list):
        merged["providers"] = rebuilt["providers"]
        merged["models"] = rebuilt.get("models") or {}
        merged["detail_tokens"] = rebuilt_detail
    else:
        if not isinstance(merged.get("providers"), list) and isinstance(rebuilt.get("providers"), list):
            merged["providers"] = rebuilt["providers"]
        if not isinstance(merged.get("models"), dict) and isinstance(rebuilt.get("models"), dict):
            merged["models"] = rebuilt["models"]
        if "detail_tokens" not in merged:
            merged["detail_tokens"] = rebuilt_detail
    before = history_row_signature(existing)
    after = history_row_signature(merged)
    if before != after:
        merged["updated_at"] = rebuilt.get("updated_at") or reconciled_at
        merged["offline_reconciled_at"] = reconciled_at
    return merged, before != after


def backfill_offline_usage_history(
    home: Path,
    sessions_root: Path,
    now: datetime,
    attribution_ledger: dict[str, str],
    history: dict[str, Any] | None = None,
    target_days: list[date] | None = None,
) -> dict[str, Any]:
    started_at = now.replace(tzinfo=LOCAL_TZ).isoformat(timespec="seconds")
    if OFFLINE_HISTORY_BACKFILL_MAX_DAYS <= 0:
        return {"state": "disabled", "started_at": started_at, "scanned_days": 0}
    history = history if isinstance(history, dict) else load_usage_history_for_backfill()
    days = history.setdefault("days", {})
    if not isinstance(days, dict):
        days = {}
        history["days"] = days
    targets = (
        list(target_days)
        if target_days is not None
        else offline_history_dates_to_reconcile(history, now)
    )
    if not targets:
        return {"state": "idle", "started_at": started_at, "scanned_days": 0}

    result = {
        "state": "running",
        "started_at": started_at,
        "from": min(targets).isoformat(),
        "through": max(targets).isoformat(),
        "scanned_days": len(targets),
        "updated_days": 0,
    }
    try:
        rebuilt_rows = build_historical_usage_rows(
            home,
            sessions_root,
            targets,
            attribution_ledger,
            now,
        )
        changed = 0
        for target_day in targets:
            key = target_day.isoformat()
            rebuilt = rebuilt_rows.get(key)
            if not isinstance(rebuilt, dict):
                continue
            merged, row_changed = merge_rebuilt_history_day(
                days.get(key) if isinstance(days.get(key), dict) else None,
                rebuilt,
                started_at,
            )
            days[key] = merged
            changed += int(row_changed)
        completed_at = datetime.now().replace(tzinfo=LOCAL_TZ).isoformat(timespec="seconds")
        result.update(
            {
                "state": "complete",
                "updated_days": changed,
                "completed_at": completed_at,
            }
        )
        history["schema"] = max(2, int(history.get("schema") or 1))
        history["offline_sync"] = {
            "state": "complete",
            "last_successful_at": completed_at,
            "from": result["from"],
            "through": result["through"],
            "scanned_days": result["scanned_days"],
            "updated_days": changed,
        }
        write_json_atomic(USAGE_HISTORY_PATH, history)
        return result
    except Exception as exc:
        completed_at = datetime.now().replace(tzinfo=LOCAL_TZ).isoformat(timespec="seconds")
        previous_sync = history.get("offline_sync")
        sync = dict(previous_sync) if isinstance(previous_sync, dict) else {}
        sync.update(
            {
                "state": "error",
                "last_attempt_at": completed_at,
                "error": f"{type(exc).__name__}: {exc}"[:240],
            }
        )
        history["offline_sync"] = sync
        try:
            write_json_atomic(USAGE_HISTORY_PATH, history)
        except OSError:
            pass
        result.update(
            {
                "state": "error",
                "completed_at": completed_at,
                "message": sync["error"],
            }
        )
        return result


def same_day_output_high_water(output: dict[str, Any], existing_path: Path, day: date) -> None:
    """Keep same-day local totals monotonic across account switches.

    Codex can keep writing a long-running session while the selected account
    marker changes. During that handoff, attribution may briefly miss the older
    account even though the raw token events still exist. Preserve the previous
    same-day snapshot so a transient empty attribution pass does not erase the
    floating monitor's today totals.
    """
    try:
        existing = json.loads(existing_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if str(existing.get("date") or "") != day.isoformat():
        return
    collapse_api_service_mirror_providers(existing)
    current_api_aggregate = bool(output.get("api_service_aggregate"))
    current_api_account_routing = current_api_aggregate or bool(output.get("api_service_routed"))

    def tokens_of(row: Any) -> int:
        if not isinstance(row, dict):
            return 0
        try:
            return int(row.get("tokens") or 0)
        except (TypeError, ValueError):
            return 0

    def merge_cumulative(current: dict[str, Any], previous: dict[str, Any]) -> None:
        if tokens_of(previous) <= tokens_of(current):
            return
        current_latest_dt = latest_time(current)
        previous_latest_dt = latest_time(previous)
        current_latest_at = current.get("latest_at")
        current_latest_model = current.get("latest_model")
        for key in (
            "requests",
            "tokens",
            "input_tokens",
            "cached_input_tokens",
            "cache_creation_input_tokens",
            "output_tokens",
            "cost",
            "models",
            "latest_at",
            "latest_model",
            "show_zero",
        ):
            if key in previous:
                current[key] = previous[key]
        if current_latest_dt is not None and (previous_latest_dt is None or current_latest_dt >= previous_latest_dt):
            current["latest_at"] = current_latest_at
            if current_latest_model:
                current["latest_model"] = current_latest_model

    def latest_time(row: Any) -> datetime | None:
        if not isinstance(row, dict):
            return None
        return parse_dt(row.get("created_at") or row.get("latest_at"))

    def merge_latest_request() -> None:
        current = output.get("latest_request")
        previous = existing.get("latest_request")
        if not isinstance(previous, dict):
            return
        if not isinstance(current, dict) or not current.get("created_at"):
            output["latest_request"] = previous
            return
        previous_dt = latest_time(previous)
        current_dt = latest_time(current)
        if previous_dt is not None and (current_dt is None or previous_dt > current_dt):
            output["latest_request"] = previous

    def merge_hourly_today() -> None:
        failure_keys = ("failure", "failure_count", "failure_at", "failure_kind")
        current_dashboard = output.get("dashboard")
        previous_dashboard = existing.get("dashboard")
        if not isinstance(current_dashboard, dict) or not isinstance(previous_dashboard, dict):
            return
        current_hourly = current_dashboard.get("hourly_today")
        previous_hourly = previous_dashboard.get("hourly_today")
        if not isinstance(current_hourly, list) or not isinstance(previous_hourly, list):
            return
        current_by_hour = {
            int(row.get("hour") or 0): row
            for row in current_hourly
            if isinstance(row, dict)
        }
        for previous_row in previous_hourly:
            if not isinstance(previous_row, dict):
                continue
            hour = max(0, min(23, int(previous_row.get("hour") or 0)))
            current_row = current_by_hour.get(hour)
            if current_row is None:
                restored_row = dict(previous_row)
                for key in failure_keys:
                    restored_row.pop(key, None)
                current_hourly.append(restored_row)
                current_by_hour[hour] = restored_row
                continue
            current_failure = {
                key: current_row[key]
                for key in failure_keys
                if key in current_row
            }
            if tokens_of(previous_row) > tokens_of(current_row):
                current_row.update(previous_row)
            # Failure annotations are live scan results, not cumulative high-water data.
            for key in failure_keys:
                current_row.pop(key, None)
            if current_failure.get("failure"):
                current_row.update(current_failure)

    existing_today = existing.get("today")
    current_today = output.get("today")
    if not current_api_aggregate and isinstance(existing_today, dict) and isinstance(current_today, dict):
        merge_cumulative(current_today, existing_today)
    merge_latest_request()
    if not current_api_aggregate:
        merge_hourly_today()
    if "account_30d_updated_at" not in output and existing.get("account_30d_updated_at"):
        output["account_30d_updated_at"] = existing["account_30d_updated_at"]

    current_providers = output.get("providers")
    existing_providers = existing.get("providers")
    if not isinstance(current_providers, list) or not isinstance(existing_providers, list):
        return
    current_by_name = {
        str(provider.get("name") or ""): provider
        for provider in current_providers
        if isinstance(provider, dict) and provider.get("name")
    }
    for previous in existing_providers:
        if not isinstance(previous, dict):
            continue
        name = str(previous.get("name") or "")
        if not name:
            continue
        current = current_by_name.get(name)
        if current_api_account_routing:
            if current is not None and "window_30d" not in current and isinstance(previous.get("window_30d"), dict):
                current["window_30d"] = dict(previous["window_30d"])
            continue
        if current is None:
            recovered = dict(previous)
            for window_key in ("window_5h", "window_7d", "window_cycle"):
                window = recovered.get(window_key)
                if isinstance(window, dict):
                    window = dict(window)
                    window["quota_stale"] = True
                    recovered[window_key] = window
            current_providers.append(recovered)
            current_by_name[name] = recovered
            continue
        merge_cumulative(current, previous)
        if "window_30d" not in current and isinstance(previous.get("window_30d"), dict):
            current["window_30d"] = dict(previous["window_30d"])

    provider_totals = [provider for provider in current_providers if isinstance(provider, dict)]
    provider_tokens = sum(tokens_of(provider) for provider in provider_totals)
    if isinstance(current_today, dict) and provider_tokens > tokens_of(current_today):
        current_today["requests"] = sum(int(provider.get("requests") or 0) for provider in provider_totals)
        current_today["tokens"] = provider_tokens
        current_today["input_tokens"] = sum(int(provider.get("input_tokens") or 0) for provider in provider_totals)
        current_today["cached_input_tokens"] = sum(int(provider.get("cached_input_tokens") or 0) for provider in provider_totals)
        current_today["cache_creation_input_tokens"] = sum(int(provider.get("cache_creation_input_tokens") or 0) for provider in provider_totals)
        current_today["output_tokens"] = sum(int(provider.get("output_tokens") or 0) for provider in provider_totals)
        current_today["cost"] = round(sum(float(provider.get("cost") or 0) for provider in provider_totals), 6)


def restore_today_from_usage_history(output: dict[str, Any], day: date) -> None:
    if output.get("api_service_aggregate"):
        return
    try:
        history = json.loads(USAGE_HISTORY_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    days = history.get("days") if isinstance(history, dict) else None
    row = days.get(day.isoformat()) if isinstance(days, dict) else None
    today = output.get("today")
    if not isinstance(row, dict) or not isinstance(today, dict):
        return
    row = dict(row)
    try:
        history_tokens = int(row.get("tokens") or 0)
        current_tokens = int(today.get("tokens") or 0)
    except (TypeError, ValueError):
        return
    gap = row.get("source_gap") if isinstance(row.get("source_gap"), dict) else None
    if gap is None:
        if history_tokens <= current_tokens:
            return
        gap = {}
        for key in (
            "requests",
            "tokens",
            "input_tokens",
            "cached_input_tokens",
            "cache_creation_input_tokens",
            "output_tokens",
        ):
            gap[key] = max(0, int(row.get(key) or 0) - int(today.get(key) or 0))
        gap["cost"] = round(max(0.0, float(row.get("cost") or 0) - float(today.get("cost") or 0)), 6)
        history_row = days.get(day.isoformat()) if isinstance(days, dict) else None
        if isinstance(history_row, dict):
            history_row["source_gap"] = gap
            try:
                write_json_atomic(USAGE_HISTORY_PATH, history)
            except OSError:
                pass
    gap = dict(gap)
    gap_tokens = max(0, int(gap.get("tokens") or 0))
    token_fields = (
        "input_tokens",
        "cached_input_tokens",
        "cache_creation_input_tokens",
        "output_tokens",
    )
    excess = max(0, sum(max(0, int(gap.get(key) or 0)) for key in token_fields) - gap_tokens)
    for key in ("cached_input_tokens", "input_tokens", "cache_creation_input_tokens", "output_tokens"):
        value = max(0, int(gap.get(key) or 0))
        reduction = min(value, excess)
        gap[key] = value - reduction
        excess -= reduction
    gap["tokens"] = gap_tokens
    providers = output.get("providers")
    for key in (
        "requests",
        "tokens",
        "input_tokens",
        "cached_input_tokens",
        "cache_creation_input_tokens",
        "output_tokens",
    ):
        today[key] = int(today.get(key) or 0) + int(gap.get(key) or 0)
    today["cost"] = round(float(today.get("cost") or 0) + float(gap.get("cost") or 0), 6)
    if not isinstance(providers, list):
        return
    add_unattributed_provider_gap(output)


def add_unattributed_provider_gap(output: dict[str, Any]) -> None:
    today = output.get("today")
    providers = output.get("providers")
    if not isinstance(today, dict) or not isinstance(providers, list):
        return
    normal_providers = [
        provider
        for provider in providers
        if isinstance(provider, dict)
        and str(provider.get("name") or "") != HIGH_WATER_UNATTRIBUTED_LABEL
    ]

    def provider_sum(key: str) -> float:
        total = 0.0
        for provider in normal_providers:
            try:
                total += float(provider.get(key) or 0)
            except (TypeError, ValueError):
                pass
        return total

    delta_tokens = int(today.get("tokens") or 0) - int(provider_sum("tokens"))
    if delta_tokens <= 0:
        providers[:] = normal_providers
        return
    delta = {
        "name": HIGH_WATER_UNATTRIBUTED_LABEL,
        "is_unattributed_gap": True,
        "requests": max(0, int(today.get("requests") or 0) - int(provider_sum("requests"))),
        "tokens": delta_tokens,
        "input_tokens": max(0, int(today.get("input_tokens") or 0) - int(provider_sum("input_tokens"))),
        "cached_input_tokens": max(0, int(today.get("cached_input_tokens") or 0) - int(provider_sum("cached_input_tokens"))),
        "cache_creation_input_tokens": max(0, int(today.get("cache_creation_input_tokens") or 0) - int(provider_sum("cache_creation_input_tokens"))),
        "output_tokens": max(0, int(today.get("output_tokens") or 0) - int(provider_sum("output_tokens"))),
        "cost": round(max(0.0, float(today.get("cost") or 0) - provider_sum("cost")), 6),
        "models": {},
        "latest_at": str(today.get("latest_at") or ""),
        "latest_model": str(today.get("latest_model") or ""),
        "show_zero": False,
    }
    providers[:] = normal_providers + [delta]


def bucket_to_dict(name: str, bucket: UsageBucket, show_zero: bool = False) -> dict[str, Any]:
    result = {
        "name": name,
        "requests": bucket.requests,
        "tokens": bucket.total_tokens,
        "input_tokens": bucket.input_tokens,
        "cached_input_tokens": bucket.cached_input_tokens + bucket.cache_read_input_tokens,
        "cache_creation_input_tokens": bucket.cache_creation_input_tokens,
        "output_tokens": bucket.output_tokens,
        "cost": round(bucket.cost, 6),
        "models": dict(sorted(bucket.models.items(), key=lambda item: item[1], reverse=True)[:8]),
        "latest_at": latest_at_text(bucket),
        "latest_model": bucket.latest_model,
        "show_zero": show_zero,
    }
    if bucket.latest_app_speed:
        result.update(
            {
                "app_speed": bucket.latest_app_speed,
                "cost_multiplier": float(bucket.latest_cost_multiplier or 1.0),
                "speed_badge": bucket.latest_speed_badge,
            }
        )
    return result


def bucket_to_window_dict(bucket: UsageBucket, start: datetime, end: datetime) -> dict[str, Any]:
    result = {
        "requests": bucket.requests,
        "tokens": bucket.total_tokens,
        "input_tokens": bucket.input_tokens,
        "cached_input_tokens": bucket.cached_input_tokens + bucket.cache_read_input_tokens,
        "cache_creation_input_tokens": bucket.cache_creation_input_tokens,
        "output_tokens": bucket.output_tokens,
        "cost": round(bucket.cost, 6),
        "models": dict(sorted(bucket.models.items(), key=lambda item: item[1], reverse=True)[:8]),
        "start_at": start.replace(tzinfo=LOCAL_TZ).isoformat(timespec="seconds"),
        "end_at": end.replace(tzinfo=LOCAL_TZ).isoformat(timespec="seconds"),
    }
    latest = latest_at_text(bucket)
    if latest:
        result["latest_at"] = latest
        result["latest_model"] = bucket.latest_model
    return result


def apply_quota_countdown_state(
    window: dict[str, Any],
    now: datetime,
    idle_until_first_use: bool,
) -> None:
    if window.get("quota_unlimited"):
        window["quota_idle"] = False
        window["countdown_active"] = False
        return
    try:
        remaining = float(window.get("remaining_percent"))
    except (TypeError, ValueError):
        remaining = -1.0
    reset_at = parse_dt(window.get("resets_at"))
    quota_available = bool(window.get("quota_available"))
    has_usage = (
        int(window.get("requests") or 0) > 0
        or int(window.get("tokens") or 0) > 0
        or float(window.get("cost") or 0) > 0
    )
    if quota_available and reset_at is not None and reset_at <= now:
        window["quota_snapshot_expired"] = True
        duration = quota_window_duration(
            window,
            timedelta(hours=5) if idle_until_first_use else timedelta(days=7),
        )
        next_reset = reset_at + duration if now - reset_at <= duration else None
        if next_reset is None:
            window["quota_stale"] = True
            window["remaining_percent"] = None
            window["utilization"] = None
            window["quota_idle"] = False
            window["countdown_active"] = False
            return
        if not has_usage:
            window["quota_stale"] = False
            window["remaining_percent"] = 100.0
            window["utilization"] = 0.0
            window["quota_idle"] = idle_until_first_use
            window["countdown_active"] = not idle_until_first_use and next_reset is not None
            window["resets_at"] = (
                ""
                if idle_until_first_use or next_reset is None
                else next_reset.replace(tzinfo=LOCAL_TZ).isoformat(timespec="seconds")
            )
        else:
            # Usage is already from the new boundary, but the upstream quota
            # percentage still describes the expired window.
            window["quota_stale"] = True
            window["remaining_percent"] = None
            window["utilization"] = None
            window["quota_idle"] = False
            window["countdown_active"] = True
            if next_reset is not None:
                window["resets_at"] = next_reset.replace(tzinfo=LOCAL_TZ).isoformat(
                    timespec="seconds"
                )
        return
    # The upstream quota snapshot commonly reports a reset/full window as 99%.
    full_unused = (
        quota_available
        and not bool(window.get("quota_stale"))
        and remaining >= 99.0
        and not has_usage
    )
    if full_unused:
        window["remaining_percent"] = 100.0
        window["utilization"] = 0.0
    quota_idle = full_unused and idle_until_first_use
    window["quota_idle"] = quota_idle
    window["countdown_active"] = quota_available and not quota_idle


def apply_5h_countdown_state(window: dict[str, Any], now: datetime | None = None) -> None:
    apply_quota_countdown_state(window, now or datetime.now(), idle_until_first_use=True)


def prefer_more_complete_usage_buckets(
    primary: dict[str, UsageBucket],
    candidate: dict[str, UsageBucket],
) -> dict[str, UsageBucket]:
    result = dict(primary)
    for label, bucket in candidate.items():
        existing = result.get(label)
        if (
            existing is None
            or bucket.total_tokens > existing.total_tokens
            or (bucket.total_tokens == existing.total_tokens and bucket.requests > existing.requests)
        ):
            result[label] = bucket
    return result


def build_codex_window_stats(
    home: Path,
    sessions_root: Path,
    now: datetime,
    attribution_ledger: dict[str, str],
    current_label: str,
    include_30d: bool = False,
) -> dict[str, dict[str, dict[str, Any]]]:
    window_end = now + timedelta(seconds=1)
    window_5h_start = now - timedelta(hours=5)
    window_7d_start = now - timedelta(days=7)
    window_30d_start = now - timedelta(days=30)

    quota_by_account = cockpit_codex_quota_by_label(home)
    speed_by_account = cockpit_codex_speed_by_label(home)
    cost_multiplier_by_label = {
        label: float(meta.get("cost_multiplier") or 1.0)
        for label, meta in speed_by_account.items()
    }
    direct_7d = scan_cockpit_codex_accounts(home, window_7d_start, window_end)
    direct_30d = (
        scan_cockpit_codex_accounts(home, window_30d_start, window_end)
        if include_30d
        else {}
    )
    direct_total = UsageBucket()
    for bucket in (direct_30d if include_30d else direct_7d).values():
        add_bucket(direct_total, bucket)

    buckets_5h: dict[str, UsageBucket]
    buckets_7d: dict[str, UsageBucket]
    buckets_30d: dict[str, UsageBucket] = {}
    if direct_total.total_tokens > 0 or direct_total.requests > 0:
        buckets_7d = direct_7d
        buckets_5h = scan_cockpit_codex_accounts(home, window_5h_start, window_end)
        buckets_30d = direct_30d
    else:
        speed_markers = codex_speed_history(home, window_7d_start, window_end)
        events_7d = scan_all_codex_events(home, sessions_root, window_7d_start, window_end)
        apply_codex_speed_fallback(events_7d, speed_markers)
        markers_7d = scan_cockpit_codex_switch_markers(home, window_7d_start, window_end)
        markers_7d.extend(scan_cockpit_codex_account_markers(home, window_7d_start, window_end))
        buckets_7d = attribute_codex_events_to_account_markers(
            events_7d,
            markers_7d,
            cost_multiplier_by_label,
            attribution_ledger,
            current_label,
            now,
        )

        events_5h = scan_all_codex_events(home, sessions_root, window_5h_start, window_end)
        apply_codex_speed_fallback(events_5h, speed_markers)
        markers_5h = scan_cockpit_codex_switch_markers(home, window_5h_start, window_end)
        markers_5h.extend(scan_cockpit_codex_account_markers(home, window_5h_start, window_end))
        buckets_5h = attribute_codex_events_to_account_markers(
            events_5h,
            markers_5h,
            cost_multiplier_by_label,
            attribution_ledger,
            current_label,
            now,
        )
        if include_30d:
            speed_markers_30d = codex_speed_history(home, window_30d_start, window_end)
            events_30d = scan_all_codex_events(home, sessions_root, window_30d_start, window_end)
            apply_codex_speed_fallback(events_30d, speed_markers_30d)
            markers_30d = scan_cockpit_codex_switch_markers(home, window_30d_start, window_end)
            markers_30d.extend(
                scan_cockpit_codex_account_markers(home, window_30d_start, window_end)
            )
            buckets_30d = attribute_codex_events_to_account_markers(
                events_30d,
                markers_30d,
                cost_multiplier_by_label,
                attribution_ledger,
                current_label,
                now,
            )
    if include_30d and (direct_total.total_tokens > 0 or direct_total.requests > 0):
        speed_markers_30d = codex_speed_history(home, window_30d_start, window_end)
        events_30d = scan_all_codex_events(home, sessions_root, window_30d_start, window_end)
        apply_codex_speed_fallback(events_30d, speed_markers_30d)
        switch_markers_30d = scan_cockpit_codex_switch_markers(
            home,
            window_30d_start,
            window_end,
        )
        account_markers_30d = scan_cockpit_codex_account_markers(
            home,
            window_30d_start,
            window_end,
        )
        markers_30d = switch_markers_30d + account_markers_30d
        marker_index_30d = account_markers_by_total_tokens(account_markers_30d)
        attributed_30d = attribute_codex_events_by_account(
            events_30d,
            markers_30d,
            attribution_ledger,
            current_label,
            now,
        )
        raw_30d: dict[str, UsageBucket] = {}
        for label, account_events in attributed_30d.items():
            for event in account_events:
                resolved_label = label
                matched_marker = concrete_api_service_account_marker(
                    event,
                    account_markers_30d,
                    marker_index_30d,
                )
                if matched_marker is not None:
                    resolved_label = matched_marker.label
                    if matched_marker.model:
                        event.model = matched_marker.model
                multiplier = cost_multiplier_by_label.get(resolved_label, 1.0)
                add_codex_event_to_bucket(
                    raw_30d.setdefault(resolved_label, UsageBucket()),
                    event,
                    multiplier,
                    bucket_time=usage_event_attribution_time(event),
                )
        buckets_30d = prefer_more_complete_usage_buckets(buckets_30d, raw_30d)

    (
        aligned_5h,
        aligned_7d,
        aligned_cycle,
        aligned_starts_5h,
        aligned_starts_7d,
        aligned_starts_cycle,
        _direct_latest,
    ) = scan_cockpit_codex_quota_windows(
        home,
        quota_by_account,
        now,
        window_end,
    )
    aligned_starts = (
        list(aligned_starts_5h.values())
        + list(aligned_starts_7d.values())
        + list(aligned_starts_cycle.values())
    )
    rolling_7d_buckets = dict(direct_7d)
    scan_starts = aligned_starts + [window_7d_start]
    if scan_starts:
        aligned_scan_start = min(scan_starts) - timedelta(
            seconds=max(0, QUOTA_WINDOW_START_TOLERANCE_SECONDS)
        )
        aligned_events = scan_all_codex_events(home, sessions_root, aligned_scan_start, window_end)
        speed_markers = codex_speed_history(home, aligned_scan_start, window_end)
        apply_codex_speed_fallback(aligned_events, speed_markers)
        aligned_markers = scan_cockpit_codex_switch_markers(home, aligned_scan_start, window_end)
        aligned_account_markers = scan_cockpit_codex_account_markers(home, aligned_scan_start, window_end)
        aligned_markers.extend(aligned_account_markers)
        aligned_marker_index = account_markers_by_total_tokens(aligned_account_markers)
        attributed_events = attribute_codex_events_by_account(
            aligned_events,
            aligned_markers,
            attribution_ledger,
            current_label,
            now,
        )
        raw_5h = {label: UsageBucket() for label in aligned_starts_5h}
        raw_7d = {label: UsageBucket() for label in aligned_starts_7d}
        raw_cycle = {label: UsageBucket() for label in aligned_starts_cycle}
        raw_rolling_7d: dict[str, UsageBucket] = {}
        for label, account_events in attributed_events.items():
            for event in account_events:
                resolved_label = label
                matched_marker = concrete_api_service_account_marker(
                    event,
                    aligned_account_markers,
                    aligned_marker_index,
                )
                if matched_marker is not None:
                    resolved_label = matched_marker.label
                    if matched_marker.model:
                        event.model = matched_marker.model
                event_time = usage_event_attribution_time(event)
                multiplier = cost_multiplier_by_label.get(resolved_label, 1.0)
                if event_time >= window_7d_start:
                    add_codex_event_to_bucket(
                        raw_rolling_7d.setdefault(resolved_label, UsageBucket()),
                        event,
                        multiplier,
                        bucket_time=event_time,
                    )
                if resolved_label in aligned_starts_5h and event_time >= aligned_starts_5h[resolved_label]:
                    add_codex_event_to_bucket(raw_5h[resolved_label], event, multiplier, bucket_time=event_time)
                if resolved_label in aligned_starts_7d and event_time >= aligned_starts_7d[resolved_label]:
                    add_codex_event_to_bucket(raw_7d[resolved_label], event, multiplier, bucket_time=event_time)
                if resolved_label in aligned_starts_cycle and event_time >= aligned_starts_cycle[resolved_label]:
                    add_codex_event_to_bucket(raw_cycle[resolved_label], event, multiplier, bucket_time=event_time)
        aligned_5h = prefer_more_complete_usage_buckets(aligned_5h, raw_5h)
        aligned_7d = prefer_more_complete_usage_buckets(aligned_7d, raw_7d)
        aligned_cycle = prefer_more_complete_usage_buckets(aligned_cycle, raw_cycle)
        rolling_7d_buckets = prefer_more_complete_usage_buckets(rolling_7d_buckets, raw_rolling_7d)
    buckets_5h.update(aligned_5h)
    buckets_7d.update(aligned_7d)
    buckets_cycle = aligned_cycle

    result: dict[str, dict[str, dict[str, Any]]] = {}
    labels = (
        set(buckets_5h)
        | set(buckets_7d)
        | set(rolling_7d_buckets)
        | set(buckets_30d)
        | set(buckets_cycle)
        | set(all_cockpit_codex_account_labels(home))
        | set(quota_by_account)
    )
    for label in labels:
        quota = quota_by_account.get(label) or {}
        quota_5h = quota.get("window_5h") or {}
        quota_7d = quota.get("window_7d") or {}
        quota_cycle = quota.get("window_cycle") or {}
        bucket_5h = buckets_5h.get(label, UsageBucket())
        bucket_7d = buckets_7d.get(label, UsageBucket())
        bucket_cycle = buckets_cycle.get(label, UsageBucket())
        if (
            quota_5h.get("window_minutes")
            and not quota_5h.get("quota_unlimited")
            and label not in aligned_starts_5h
        ):
            bucket_5h = UsageBucket()
        if quota_7d.get("window_minutes") and label not in aligned_starts_7d:
            bucket_7d = UsageBucket()
        if quota_cycle.get("window_minutes") and label not in aligned_starts_cycle:
            bucket_cycle = UsageBucket()
        window_5h = bucket_to_window_dict(
            bucket_5h,
            aligned_starts_5h.get(label, window_5h_start),
            now,
        )
        window_7d = bucket_to_window_dict(
            bucket_7d,
            aligned_starts_7d.get(label, window_7d_start),
            now,
        )
        window_rolling_7d = bucket_to_window_dict(
            rolling_7d_buckets.get(label, UsageBucket()),
            window_7d_start,
            now,
        )
        window_30d = bucket_to_window_dict(
            buckets_30d.get(label, UsageBucket()),
            window_30d_start,
            now,
        )
        window_cycle = bucket_to_window_dict(
            bucket_cycle,
            aligned_starts_cycle.get(label, now),
            now,
        )
        window_5h.update(quota_5h)
        window_7d.update(quota_7d)
        window_cycle.update(quota_cycle)
        apply_5h_countdown_state(window_5h, now)
        apply_quota_countdown_state(window_7d, now, idle_until_first_use=False)
        result[label] = {
            "window_5h": window_5h,
            "window_7d": window_7d,
            "window_rolling_7d": window_rolling_7d,
            "window_cycle": window_cycle,
        }
        if include_30d:
            result[label]["window_30d"] = window_30d
    return result


def window_only_provider_labels(
    window_stats: dict[str, dict[str, dict[str, Any]]],
    provider_map: dict[str, UsageBucket],
) -> set[str]:
    return {
        label
        for label in window_stats
        if "@" in label and label not in provider_map
    }


def build_live_catchup_payload(
    home: Path,
    sessions_root: Path,
    output_path: Path,
    since: datetime,
    through: datetime,
) -> dict[str, Any]:
    """Build a read-only, fixed-cutoff event delta for the floating monitor."""
    if through <= since:
        return {
            "schema": 1,
            "since": since.replace(tzinfo=LOCAL_TZ).isoformat(timespec="microseconds"),
            "through": through.replace(tzinfo=LOCAL_TZ).isoformat(timespec="microseconds"),
            "events": [],
        }

    day_start = datetime.combine(since.date(), datetime.min.time())
    session_lifecycle: dict[str, SessionLifecycle] = {}
    codex_events = scan_all_codex_events(
        home,
        sessions_root,
        day_start,
        through,
        session_lifecycle=session_lifecycle,
    )
    speed_markers = codex_speed_history(home, day_start, through)
    apply_codex_speed_fallback(codex_events, speed_markers)

    current_label = current_codex_account_label(home)
    attribution_ledger = load_attribution_ledger()
    markers = scan_cockpit_codex_switch_markers(home, day_start, through)
    markers.extend(load_account_timeline())
    account_markers = scan_cockpit_codex_account_markers(home, day_start, through)
    markers.extend(account_markers)
    raw_attributed = attribute_codex_events_by_account(
        codex_events,
        markers,
        attribution_ledger,
        current_label,
        through,
    )
    raw_attributed, fallback_events = merge_missing_cockpit_account_events(
        raw_attributed,
        account_markers,
    )
    attributed, _session_accounts, unresolved_events = resolve_api_service_event_accounts(
        raw_attributed,
        account_markers,
        previous_active_session_account_labels(output_path, since.date()),
    )
    speed_by_account = cockpit_codex_speed_by_label(home)
    cost_multiplier_by_label = {
        label: float(meta.get("cost_multiplier") or 1.0)
        for label, meta in speed_by_account.items()
    }

    provider_buckets = buckets_from_attributed_events(
        attributed,
        cost_multiplier_by_label,
    )
    total = UsageBucket()
    provider_totals: list[dict[str, Any]] = []
    for provider, bucket in sorted(
        provider_buckets.items(),
        key=lambda item: (-item[1].total_tokens, -item[1].requests, item[0]),
    ):
        add_bucket(total, bucket)
        provider_totals.append(bucket_to_dict(provider, bucket))
    claude = scan_claude(home / ".claude" / "projects", day_start, through)
    if claude.requests or claude.total_tokens or claude.cost:
        add_bucket(total, claude)
        provider_totals.append(bucket_to_dict("Claude local", claude))

    rows: list[dict[str, Any]] = []
    for provider, events in attributed.items():
        multiplier = float(cost_multiplier_by_label.get(provider) or 1.0)
        for event in events:
            if not since < event.when < through:
                continue
            event_bucket = UsageBucket()
            add_codex_event_to_bucket(event_bucket, event, multiplier)
            aware_when = (
                event.when
                if event.when.tzinfo is not None
                else event.when.replace(tzinfo=LOCAL_TZ)
            )
            rows.append(
                {
                    "event_id": live_usage_event_id(event),
                    "when": aware_when.isoformat(timespec="microseconds"),
                    "provider": provider,
                    "model": event.model,
                    "session_id": event.session_id,
                    "total_tokens": event.total_tokens,
                    "input_tokens": event.input_tokens + event.cached_tokens,
                    "cached_tokens": event.cached_tokens,
                    "output_tokens": event.output_tokens,
                    "cost": round(event_bucket.cost, 12),
                }
            )
    rows.sort(key=lambda row: (str(row.get("when") or ""), str(row.get("event_id") or "")))
    return {
        "schema": 1,
        "since": since.replace(tzinfo=LOCAL_TZ).isoformat(timespec="microseconds"),
        "through": through.replace(tzinfo=LOCAL_TZ).isoformat(timespec="microseconds"),
        "events": rows,
        "summary": bucket_to_dict("Client live catch-up", total),
        "providers": provider_totals,
        "fallback_events": int(fallback_events),
        "unresolved_events": int(unresolved_events),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Export local Claude/Codex client token usage for Sub2API monitor.")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--date", default="")
    parser.add_argument("--include-30d", action="store_true")
    parser.add_argument("--backfill-history-details", action="store_true")
    parser.add_argument("--quota-only", action="store_true")
    parser.add_argument("--live-since", default="")
    parser.add_argument("--live-through", default="")
    args = parser.parse_args()

    now = datetime.now()
    out = Path(args.output)
    home = Path(os.path.expanduser("~"))
    if args.live_since:
        since = parse_dt(args.live_since)
        through = parse_dt(args.live_through) if args.live_through else now
        if since is None or through is None:
            parser.error("--live-since/--live-through must be valid ISO timestamps")
        payload = build_live_catchup_payload(
            home,
            home / ".codex" / "sessions",
            out,
            since,
            through,
        )
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        return 0
    if args.quota_only:
        print(
            json.dumps(
                {
                    "updated_at": now.replace(tzinfo=LOCAL_TZ).isoformat(timespec="seconds"),
                    "accounts": cockpit_codex_quota_by_label(home),
                },
                ensure_ascii=False,
            )
        )
        return 0
    cached_30d_valid = False
    cached_30d_updated_at = ""
    cached_30d_windows: dict[str, dict[str, Any]] = {}
    if args.include_30d:
        (
            cached_30d_valid,
            cached_30d_updated_at,
            cached_30d_windows,
        ) = load_cached_account_30d_windows(out, now)
    refresh_30d = args.include_30d and not cached_30d_valid
    if args.date:
        day = datetime.fromisoformat(args.date).date()
    else:
        day = now.date()
    start = datetime.combine(day, datetime.min.time())
    end = start + timedelta(days=1)
    scan_end = min(now, end) if day == now.date() else end

    codex_sessions_root = home / ".codex" / "sessions"
    record_current_account_snapshot(home, now)
    attribution_ledger = load_attribution_ledger()
    current_label = current_codex_account_label(home)
    speed_by_account = cockpit_codex_speed_by_label(home)
    cost_multiplier_by_label = {
        label: float(meta.get("cost_multiplier") or 1.0)
        for label, meta in speed_by_account.items()
    }
    session_lifecycle: dict[str, SessionLifecycle] = {}
    codex_failures: list[CodexFailureEvent] = []
    codex_events = scan_all_codex_events(
        home,
        codex_sessions_root,
        start,
        scan_end,
        session_lifecycle=session_lifecycle,
        failure_events=codex_failures,
    )
    # A manual auth switch can happen during a long session scan. Re-read the
    # identity here and retain the auth file's mtime as the actual switch edge.
    snapshot_now = datetime.now()
    record_current_account_snapshot(home, snapshot_now)
    current_label = current_codex_account_label(home)
    desktop_log_roots = default_codex_desktop_log_roots()
    if desktop_log_roots:
        codex_failures.extend(
            scan_codex_desktop_failure_events(desktop_log_roots, start, scan_end)
        )
    speed_markers = codex_speed_history(home, start, scan_end)
    apply_codex_speed_fallback(codex_events, speed_markers)
    markers = scan_cockpit_codex_switch_markers(home, start, scan_end)
    markers.extend(load_account_timeline())
    account_markers = scan_cockpit_codex_account_markers(home, start, scan_end)
    markers.extend(account_markers)
    raw_attributed_events = attribute_codex_events_by_account(
        codex_events,
        markers,
        attribution_ledger,
        current_label,
        now,
    )
    api_service_routed = any(
        is_api_service_mirror_label(label)
        for label in raw_attributed_events
    ) or bool(account_markers)
    raw_attributed_events, cockpit_fallback_events = merge_missing_cockpit_account_events(
        raw_attributed_events,
        account_markers,
    )
    attributed_events, provider_session_accounts, unresolved_provider_events = resolve_api_service_event_accounts(
        raw_attributed_events,
        account_markers,
        previous_active_session_account_labels(out, day),
    )
    attributed = buckets_from_attributed_events(
        attributed_events,
        cost_multiplier_by_label,
    )
    codex = UsageBucket()
    for bucket in attributed.values():
        add_bucket(codex, bucket)
    codex_provider_buckets = sorted(
        attributed.items(),
        key=lambda item: (-item[1].total_tokens, -item[1].requests, item[0]),
    )
    codex_provider_map = {name: bucket for name, bucket in codex_provider_buckets}
    for label in all_cockpit_codex_account_labels(home):
        codex_provider_map.setdefault(label, UsageBucket())
    codex_provider_buckets = sorted(
        codex_provider_map.items(),
        key=lambda item: (-item[1].total_tokens, -item[1].requests, item[0]),
    )

    claude_root = home / ".claude" / "projects"
    claude = scan_claude(claude_root, start, scan_end)
    hourly_today = merge_hourly_buckets(
        codex_hourly_from_events(
            [event for events in attributed_events.values() for event in events]
        ),
        scan_claude_hourly(claude_root, start, scan_end),
    )
    mark_codex_failure_hours(hourly_today, codex_failures, day, now)
    if args.include_30d and cached_30d_valid:
        expected_30d_accounts = {
            name
            for name, _bucket in codex_provider_buckets
            if "@" in name
        }
        if not expected_30d_accounts.issubset(cached_30d_windows):
            refresh_30d = True
            cached_30d_windows = {}
    window_stats_by_account: dict[str, dict[str, dict[str, Any]]] = {}
    if day == now.date():
        window_stats_by_account = build_codex_window_stats(
            home,
            codex_sessions_root,
            now,
            attribution_ledger,
            current_label,
            include_30d=refresh_30d,
        )
    window_only_labels = window_only_provider_labels(
        window_stats_by_account,
        codex_provider_map,
    )
    for label in window_only_labels:
        codex_provider_map[label] = UsageBucket()
    codex_provider_buckets = sorted(
        codex_provider_map.items(),
        key=lambda item: (-item[1].total_tokens, -item[1].requests, item[0]),
    )
    save_attribution_ledger(attribution_ledger, now)

    session_account_labels = dict(provider_session_accounts)
    (
        active_sessions,
        recent_active_by_label,
        recent_sessions_by_label,
        unresolved_active_sessions,
    ) = build_active_session_rows(
        attributed_events,
        session_account_labels,
        session_lifecycle,
        current_label,
        now,
    )

    codex_providers = []
    for name, bucket in codex_provider_buckets:
        provider = bucket_to_dict(name, bucket, show_zero=True)
        provider["window_only"] = name in window_only_labels
        provider["recent_active"] = int(recent_active_by_label.get(name) or 0)
        provider["recent_sessions"] = int(recent_sessions_by_label.get(name) or 0)
        for key, value in speed_by_account.get(name, {}).items():
            if key not in provider or provider.get(key) in {"", None}:
                provider[key] = value
        if "@" in name:
            provider.update(window_stats_by_account.get(name, {}))
            if args.include_30d and name in cached_30d_windows:
                provider["window_30d"] = cached_30d_windows[name]
        codex_providers.append(provider)
    providers = codex_providers + [bucket_to_dict("Claude local", claude)]
    total = UsageBucket()
    for bucket in (codex, claude):
        total.requests += bucket.requests
        total.input_tokens += bucket.input_tokens
        total.cached_input_tokens += bucket.cached_input_tokens
        total.cache_creation_input_tokens += bucket.cache_creation_input_tokens
        total.cache_read_input_tokens += bucket.cache_read_input_tokens
        total.output_tokens += bucket.output_tokens
        total.cost += bucket.cost
        total.mark_latest(bucket.latest_at, bucket.latest_model)

    latest_provider = ""
    latest_model = ""
    latest_at = ""
    latest_dt: datetime | None = None
    codex_latest_request = latest_request_from_attributed_events(
        attributed_events,
        account_markers,
        session_account_labels,
    )
    latest_candidates = [("Claude local", claude)]
    codex_latest_at = parse_dt(codex_latest_request.get("created_at"))
    if codex_latest_at is not None:
        latest_dt = codex_latest_at
        latest_provider = str(codex_latest_request.get("provider") or "")
        latest_model = str(codex_latest_request.get("model") or "")
        latest_at = str(codex_latest_request.get("created_at") or "")
    for provider_name, bucket in latest_candidates:
        if bucket.latest_at is None:
            continue
        if latest_dt is None or bucket.latest_at > latest_dt:
            latest_dt = bucket.latest_at
            latest_provider = provider_name
            latest_model = bucket.latest_model
            latest_at = latest_at_text(bucket)
    recent_latest_request: dict[str, Any] = {}
    if not latest_at and day == now.date() and LATEST_REQUEST_LOOKBACK_DAYS > 0:
        lookback_start = now - timedelta(days=LATEST_REQUEST_LOOKBACK_DAYS)
        lookback_events = scan_all_codex_events(home, codex_sessions_root, lookback_start, scan_end)
        if lookback_events:
            lookback_speed_markers = codex_speed_history(home, lookback_start, scan_end)
            apply_codex_speed_fallback(lookback_events, lookback_speed_markers)
            lookback_markers = scan_cockpit_codex_switch_markers(home, lookback_start, scan_end)
            lookback_markers.extend(scan_cockpit_codex_account_markers(home, lookback_start, scan_end))
            lookback_markers.extend(load_account_timeline())
            recent_attributed = attribute_codex_events_by_account(
                    lookback_events,
                    lookback_markers,
                    attribution_ledger,
                    current_label,
                    now,
                )
            recent_latest_request = latest_request_from_attributed_events(
                recent_attributed,
                scan_cockpit_codex_account_markers(home, lookback_start, scan_end),
            )
            latest_provider = str(recent_latest_request.get("provider") or "")
            latest_model = str(recent_latest_request.get("model") or "")
            latest_at = str(recent_latest_request.get("created_at") or "")

    output = {
        "schema": 1,
        "source": "client-jsonl",
        "updated_at": now.replace(tzinfo=LOCAL_TZ).isoformat(timespec="seconds"),
        "date": day.isoformat(),
        "scan_status": {
            "state": "complete",
            "from": start.replace(tzinfo=LOCAL_TZ).isoformat(timespec="seconds"),
            "through": scan_end.replace(tzinfo=LOCAL_TZ).isoformat(timespec="seconds"),
            "source": "local-logs",
        },
        "today": bucket_to_dict("Client local", total),
        "providers": providers,
        "latest_request": {
            "provider": latest_provider,
            "model": latest_model,
            "created_at": latest_at,
            "kind": "success" if latest_at else "",
        },
        "active_sessions": active_sessions,
        "unresolved_active_sessions": unresolved_active_sessions,
        "api_service_routed": api_service_routed,
        "unresolved_api_service_events": unresolved_provider_events,
        "cockpit_fallback_events": cockpit_fallback_events,
        "dashboard": {
            "hourly_today": hourly_today,
        },
    }
    offline_history: dict[str, Any] | None = None
    offline_days: list[date] = []
    if not args.date and day == now.date() and OFFLINE_HISTORY_BACKFILL_MAX_DAYS > 0:
        offline_history = load_usage_history_for_backfill()
        offline_days = offline_history_dates_to_reconcile(offline_history, now)
        output["offline_catchup"] = {
            "state": "running" if offline_days else "idle",
            "started_at": now.replace(tzinfo=LOCAL_TZ).isoformat(timespec="seconds"),
            "from": min(offline_days).isoformat() if offline_days else "",
            "through": max(offline_days).isoformat() if offline_days else "",
            "scanned_days": len(offline_days),
        }
    if args.include_30d:
        output["account_30d_updated_at"] = (
            now.isoformat(timespec="seconds")
            if refresh_30d
            else cached_30d_updated_at
        )

    collapse_api_service_mirror_providers(output)
    same_day_output_high_water(output, out, day)
    restore_today_from_usage_history(output, day)
    add_unattributed_provider_gap(output)
    write_json_atomic(out, output)
    if offline_history is not None:
        catchup = backfill_offline_usage_history(
            home,
            codex_sessions_root,
            now,
            attribution_ledger,
            history=offline_history,
            target_days=offline_days,
        )
        output["offline_catchup"] = catchup
        if offline_days or catchup.get("state") == "error":
            write_json_atomic(out, output)
        save_attribution_ledger(attribution_ledger, datetime.now())
    if args.backfill_history_details:
        backfill_usage_history_details(home, codex_sessions_root)
    print(json.dumps(output["today"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
