from __future__ import annotations

import argparse
import atexit
import base64
import hashlib
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import time
import zlib
from bisect import bisect_left, bisect_right
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any
from urllib import parse, request


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


SOURCE_DIR = Path(__file__).resolve().parent
IS_FROZEN = bool(getattr(sys, "frozen", False))
if IS_FROZEN:
    local_app_data = Path(
        os.environ.get("LOCALAPPDATA")
        or Path.home() / "AppData" / "Local"
    )
    APP_DIR = Path(
        os.environ.get("TOKEN_PULSE_DATA_DIR")
        or local_app_data / "Token Pulse"
    )
    APP_DIR.mkdir(parents=True, exist_ok=True)
else:
    APP_DIR = SOURCE_DIR
LOG_PATH = APP_DIR / "tokenpulse-export.log"
logger = logging.getLogger("tokenpulse.export")
if not logger.handlers:
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    try:
        _log_handler = RotatingFileHandler(
            LOG_PATH,
            maxBytes=2 * 1024 * 1024,
            backupCount=2,
            encoding="utf-8",
            delay=True,
        )
        _log_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(_log_handler)
    except OSError:
        logger.addHandler(logging.NullHandler())


def recover_corrupt_json(path: Path) -> Any:
    """Quarantine corrupt json and atomically restore its .bak copy."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    corrupt = path.with_name(f"{path.name}.corrupt-{stamp}")
    try:
        os.replace(path, corrupt)
        logger.warning("corrupt json quarantined: %s -> %s", path.name, corrupt.name)
    except OSError as exc:
        logger.warning("failed to quarantine corrupt json %s: %s", path.name, exc)
    backup = path.with_name(f"{path.name}.bak")
    try:
        data = json.loads(backup.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        logger.warning("no usable backup for corrupt json %s", path.name)
        return None
    try:
        write_json_atomic(path, data)
    except OSError as exc:
        logger.warning("failed to restore %s from %s: %s", path.name, backup.name, exc)
    else:
        logger.warning("restored %s from %s", path.name, backup.name)
    logger.warning("recovered %s from %s", path.name, backup.name)
    return data


def refresh_json_backup(path: Path, max_age_seconds: float = 3600.0) -> None:
    """Copy path to <name>.bak when the backup is missing or older than an hour."""
    backup = path.with_name(f"{path.name}.bak")
    try:
        if backup.exists() and datetime.now().timestamp() - backup.stat().st_mtime < max_age_seconds:
            return
        payload = path.read_bytes()
        temporary = backup.with_name(f".{backup.name}.{os.getpid()}.tmp")
        try:
            temporary.write_bytes(payload)
            os.replace(temporary, backup)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
    except OSError as exc:
        logger.warning("failed to refresh backup for %s: %s", path.name, exc)


_COCKPIT_SQLITE_WARNED: set[str] = set()


def connect_cockpit_sqlite_readonly(db_path: Path) -> sqlite3.Connection:
    # timeout matches the old default-mode connect (5s) so lock contention is
    # not more likely to produce a transient empty scan than before.
    quoted = parse.quote(Path(db_path).as_posix())
    return sqlite3.connect(f"file:{quoted}?mode=ro", uri=True, timeout=5.0)


def warn_cockpit_sqlite_error(context: str, exc: Exception) -> None:
    key = f"{context}:{type(exc).__name__}"
    if key in _COCKPIT_SQLITE_WARNED:
        return
    _COCKPIT_SQLITE_WARNED.add(key)
    logger.warning("%s: cockpit sqlite read failed: %s", context, exc)


DEFAULT_OUTPUT = APP_DIR / "client_usage_today.json"
CONFIG_PATH = Path(os.environ.get("CLIENT_USAGE_CONFIG") or APP_DIR / "client_usage_config.json")
SPEED_HISTORY_PATH = Path(os.environ.get("CLIENT_USAGE_SPEED_HISTORY") or APP_DIR / "client_usage_speed_history.json")
ACCOUNT_TIMELINE_PATH = Path(os.environ.get("CLIENT_USAGE_ACCOUNT_TIMELINE") or APP_DIR / "client_usage_account_timeline.json")
AUTH_SWITCH_EVENTS_PATH = Path(
    os.environ.get("CLIENT_USAGE_AUTH_SWITCH_EVENTS")
    or APP_DIR / "client_usage_auth_switch_events.jsonl"
)
ATTRIBUTION_LEDGER_PATH = Path(os.environ.get("CLIENT_USAGE_ATTRIBUTION_LEDGER") or APP_DIR / "client_usage_attribution_ledger.json")
USAGE_HISTORY_PATH = Path(
    os.environ.get("TOKEN_PULSE_USAGE_HISTORY_JSON")
    or os.environ.get("USAGE_HISTORY_JSON")
    or os.environ.get("SUB2API_USAGE_HISTORY_JSON")
    or APP_DIR / "usage_history.json"
)
MODEL_PRICE_CACHE_PATH = Path(
    os.environ.get("CLIENT_USAGE_MODEL_PRICE_CACHE") or APP_DIR / "client_usage_model_prices.json"
)
CODEX_EVENT_CACHE_PATH = Path(
    os.environ.get("CLIENT_USAGE_CODEX_EVENT_CACHE")
    or APP_DIR / "client_usage_codex_event_cache.json"
)
OPENCODEX_USAGE_CACHE_PATH = Path(
    os.environ.get("CLIENT_USAGE_OPENCODEX_USAGE_CACHE")
    or APP_DIR / "client_usage_opencodex_usage_cache.json"
)
OPENCODEX_ACCOUNT_TIMELINE_PATH = Path(
    os.environ.get("CLIENT_USAGE_OPENCODEX_ACCOUNT_TIMELINE")
    or APP_DIR / "client_usage_opencodex_account_timeline.json"
)
OPENCODEX_ACCOUNT_MAP_PATH = Path(
    os.environ.get("CLIENT_USAGE_OPENCODEX_ACCOUNT_MAP")
    or APP_DIR / "client_usage_opencodex_accounts.json"
)
OFFLINE_BACKFILL_LOCK_PATH = Path(
    os.environ.get("CLIENT_USAGE_OFFLINE_BACKFILL_LOCK")
    or APP_DIR / ".offline-history-backfill.lock"
)
OFFLINE_BACKFILL_STATUS_PATH = Path(
    os.environ.get("CLIENT_USAGE_OFFLINE_BACKFILL_STATUS")
    or APP_DIR / "client_usage_offline_backfill_status.json"
)
OFFLINE_BACKFILL_LEASE_SECONDS = max(
    60,
    int(os.environ.get("CLIENT_USAGE_OFFLINE_BACKFILL_LEASE_SECONDS", "300")),
)
OFFLINE_BACKFILL_CHECK_INTERVAL_SECONDS = max(
    60,
    int(os.environ.get("CLIENT_USAGE_OFFLINE_BACKFILL_CHECK_INTERVAL_SECONDS", "3600")),
)


@contextmanager
def attribution_ledger_write_lock(
    path: Path = ATTRIBUTION_LEDGER_PATH,
    timeout_seconds: float = 30.0,
):
    """Serialize ledger writers across monitor/exporter processes."""
    timeout_seconds = max(0.1, float(timeout_seconds))
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = (
            ctypes.c_void_p,
            wintypes.BOOL,
            wintypes.LPCWSTR,
        )
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.ReleaseMutex.argtypes = (wintypes.HANDLE,)
        kernel32.ReleaseMutex.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL

        identity = os.path.normcase(str(path.resolve(strict=False))).encode("utf-8")
        mutex_name = f"Local\\TokenPulseLedger-{hashlib.sha256(identity).hexdigest()}"
        handle = kernel32.CreateMutexW(None, False, mutex_name)
        if not handle:
            raise OSError(ctypes.get_last_error(), "failed to create ledger mutex")
        acquired = False
        try:
            result = kernel32.WaitForSingleObject(
                handle,
                min(0xFFFFFFFE, int(timeout_seconds * 1000)),
            )
            if result not in {0x00000000, 0x00000080}:
                if result == 0x00000102:
                    raise TimeoutError("timed out waiting for attribution ledger lock")
                raise OSError(ctypes.get_last_error(), "failed to acquire ledger mutex")
            acquired = True
            yield
        finally:
            if acquired:
                kernel32.ReleaseMutex(handle)
            kernel32.CloseHandle(handle)
        return

    import fcntl

    lock_path = path.with_name(f".{path.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("timed out waiting for attribution ledger lock")
                time.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


CODEX_EVENT_CACHE_SCHEMA = 2
CODEX_COMPACT_ROW_SCHEMA = 3
OPENCODEX_COMPACT_ROW_SCHEMA = 101
CODEX_EVENT_CACHE_HASH_BYTES = max(
    1024,
    int(os.environ.get("CLIENT_USAGE_CODEX_EVENT_CACHE_HASH_BYTES", "4096")),
)
CODEX_EVENT_CACHE_MAX_ENTRIES = max(
    128,
    int(os.environ.get("CLIENT_USAGE_CODEX_EVENT_CACHE_MAX_ENTRIES", "4096")),
)
MODEL_PRICE_SOURCE_URL = os.environ.get(
    "CLIENT_USAGE_MODEL_PRICE_URL",
    "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json",
)
MODELS_DEV_PRICE_SOURCE_URL = os.environ.get(
    "CLIENT_USAGE_MODELS_DEV_PRICE_URL",
    "https://models.dev/api.json",
)
MODEL_PRICE_CACHE_SECONDS = int(os.environ.get("CLIENT_USAGE_MODEL_PRICE_CACHE_SECONDS", "86400"))
MODEL_PRICE_FETCH_TIMEOUT_SECONDS = float(os.environ.get("CLIENT_USAGE_MODEL_PRICE_FETCH_TIMEOUT_SECONDS", "4"))
MODEL_PRICE_REFRESH_COOLDOWN_SECONDS = max(
    30,
    int(os.environ.get("CLIENT_USAGE_MODEL_PRICE_REFRESH_COOLDOWN_SECONDS", "300")),
)
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
COCKPIT_OFFICIAL_QUOTA_ACTIVE_CACHE_SECONDS = max(
    60,
    int(os.environ.get("CLIENT_USAGE_OFFICIAL_QUOTA_ACTIVE_CACHE_SECONDS", "120")),
)
COCKPIT_OFFICIAL_QUOTA_ACTIVE_LOOKBACK_SECONDS = max(
    60,
    int(
        os.environ.get(
            "CLIENT_USAGE_OFFICIAL_QUOTA_ACTIVE_LOOKBACK_SECONDS",
            "300",
        )
    ),
)
COCKPIT_OFFICIAL_QUOTA_FAILURE_RETRY_SECONDS = max(
    5,
    int(
        os.environ.get(
            "CLIENT_USAGE_OFFICIAL_QUOTA_FAILURE_RETRY_SECONDS",
            "10",
        )
    ),
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
USAGE_ACCOUNTING_SCHEMA = 1
CLAUDE_USAGE_DEDUPE_SCHEMA = 2
COCKPIT_USAGE_DEDUPE_SCHEMA = 2
GROK_USAGE_DEDUPE_SCHEMA = 1
OPENCODEX_ACCOUNT_ATTRIBUTION_SCHEMA = 1
GROK_LOCAL_LABEL = "Grok local"
GROK_SUBAGENT_LABEL = "Grok subagent"
GROK_BUILD_USAGE_MODEL = "grok-4.6-build"
GROK_CANONICAL_DEFAULT_MODELS = frozenset({"grok-4.6", "xai/grok-4.6"})
GROK_CANONICAL_PRICING_MODEL = "xai/grok-4.6"
OPENCODE_SUBAGENT_LABEL = "OpenCode subagent"
EXTERNAL_CODEX_PROVIDER_LABELS = frozenset({GROK_SUBAGENT_LABEL, OPENCODE_SUBAGENT_LABEL})
CODEX_ACCOUNT_MATCH_WINDOW_SECONDS = int(os.environ.get("CLIENT_USAGE_CODEX_ACCOUNT_MATCH_WINDOW_SECONDS", "600"))
OPENCODEX_ACCOUNT_MATCH_WINDOW_SECONDS = max(
    1.0,
    env_float("CLIENT_USAGE_OPENCODEX_ACCOUNT_MATCH_WINDOW_SECONDS", 180.0),
)
OPENCODEX_RECONCILIATION_MATCH_WINDOW_SECONDS = max(
    OPENCODEX_ACCOUNT_MATCH_WINDOW_SECONDS,
    env_float("CLIENT_USAGE_OPENCODEX_RECONCILIATION_MATCH_WINDOW_SECONDS", 21_600.0),
)
OPENCODEX_RECONCILIATION_CLOCK_SKEW_SECONDS = max(
    0.0,
    env_float("CLIENT_USAGE_OPENCODEX_RECONCILIATION_CLOCK_SKEW_SECONDS", 60.0),
)
OPENCODEX_ACCOUNT_MATCH_AMBIGUITY_SECONDS = max(
    0.0,
    env_float("CLIENT_USAGE_OPENCODEX_ACCOUNT_MATCH_AMBIGUITY_SECONDS", 0.05),
)
OPENCODEX_CROSS_SESSION_MATCH_WINDOW_SECONDS = max(
    1.0,
    env_float("CLIENT_USAGE_OPENCODEX_CROSS_SESSION_MATCH_WINDOW_SECONDS", 30.0),
)
OPENCODEX_TURN_START_MATCH_SECONDS = max(
    0.1,
    env_float("CLIENT_USAGE_OPENCODEX_TURN_START_MATCH_SECONDS", 5.0),
)
OPENCODEX_ACCOUNT_TIMELINE_RETENTION_DAYS = max(
    1,
    int(os.environ.get("CLIENT_USAGE_OPENCODEX_TIMELINE_RETENTION_DAYS", "120")),
)
OPENCODEX_ACCOUNT_HINT_SOURCE = "opencodex_usage"
OPENCODEX_UNRESOLVED_HINT_SOURCE = "opencodex_usage_unresolved"
OPENCODEX_TURN_HINT_SOURCE = "opencodex_turn"
OPENCODEX_USAGE_PROVENANCE = "opencodex_usage"
TRUSTED_CODEX_ACCOUNT_HINT_SOURCES = frozenset(
    {"quota_fingerprint", OPENCODEX_ACCOUNT_HINT_SOURCE, OPENCODEX_TURN_HINT_SOURCE}
)
API_SERVICE_ACTIVITY_MATCH_SECONDS = float(os.environ.get("CLIENT_USAGE_API_ACTIVITY_MATCH_SECONDS", "300"))
COCKPIT_AFFINITY_TURN_MATCH_SECONDS = max(
    0.1,
    env_float("CLIENT_USAGE_COCKPIT_AFFINITY_TURN_MATCH_SECONDS", 2.0),
)
COCKPIT_AFFINITY_TURN_AMBIGUITY_SECONDS = max(
    0.0,
    env_float("CLIENT_USAGE_COCKPIT_AFFINITY_TURN_AMBIGUITY_SECONDS", 0.2),
)
COCKPIT_AFFINITY_EVENT_MATCH_SECONDS = max(
    0.01,
    env_float("CLIENT_USAGE_COCKPIT_AFFINITY_EVENT_MATCH_SECONDS", 0.25),
)
COCKPIT_FINAL_TURN_START_MATCH_SECONDS = max(
    COCKPIT_AFFINITY_EVENT_MATCH_SECONDS,
    env_float("CLIENT_USAGE_COCKPIT_FINAL_TURN_START_MATCH_SECONDS", 0.5),
)
COCKPIT_AFFINITY_MIN_STABLE_EVENTS = max(
    2,
    int(os.environ.get("CLIENT_USAGE_COCKPIT_AFFINITY_MIN_STABLE_EVENTS", "2")),
)
COCKPIT_FALLBACK_GRACE_SECONDS = max(
    0.0,
    env_float("CLIENT_USAGE_COCKPIT_FALLBACK_GRACE_SECONDS", 45.0),
)
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
# Evidence strength behind a resolved api-service account. Only verdicts at or
# above the archive floor are persisted, so a pure time-window guess can never
# be frozen into the ledger.
API_SERVICE_VERDICT_TIER_RANKS = {
    "temporal": 1,
    "affinity_confirmed": 2,
    "cockpit_usage_row": 3,
    "opencodex_usage_row": 4,
}
API_SERVICE_VERDICT_ARCHIVE_MIN_TIER = max(
    2,
    int(os.environ.get("CLIENT_USAGE_VERDICT_ARCHIVE_MIN_TIER", "2")),
)
API_SERVICE_VERDICT_RETENTION_DAYS = max(
    1,
    int(os.environ.get("CLIENT_USAGE_VERDICT_RETENTION_DAYS", "45")),
)
API_SERVICE_VERDICT_ARCHIVE_LIMIT = max(
    1,
    int(os.environ.get("CLIENT_USAGE_VERDICT_ARCHIVE_LIMIT", "20000")),
)
COCKPIT_CONFIRMED_AFFINITY_ACTIONS = {
    "binding confirmed",
    "spillover binding confirmed",
    "confirmed binding hit",
}
COCKPIT_STABLE_NATIVE_AFFINITY_ACTIONS = {
    "cache hit before new k12 routing",
    "cache hit",
    "cache miss, new binding",
    "temporary spillover binding hit during k12 cooldown",
    "recovery spillover retained after k12 hard release",
}
COCKPIT_FAILED_AFFINITY_ACTION_FRAGMENTS = (
    "released after failure",
    "suspended for failover",
    "auth unavailable",
    "reselected",
    "failure recovery",
    "quarantined auth",
)
COCKPIT_AFFINITY_LINE_RE = re.compile(
    r'^(?P<timestamp>\S+).*?msg="(?:k12-)?session-affinity:\s*'
    r'(?P<action>[^|\"]+?)\s*\|\s*(?P<fields>[^\"]*)"\s+'
    r'request_id=(?P<request_id>[^\s]+)'
)

_ONLINE_PRICE_TABLE: dict[str, tuple[float, float, float]] | None = None
_ONLINE_PRICE_DETAILS: dict[str, dict[str, float]] | None = None
_ONLINE_PRICE_FETCHED_AT: float | None = None
_ONLINE_PRICE_LAST_ATTEMPT_AT: float | None = None
_ONLINE_PRICE_CACHE_PATH: str | None = None


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
    unpriced_tokens: int = 0
    unpriced_models: dict[str, int] = field(default_factory=dict)
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

    def add_unpriced_model(self, model: str, tokens: int) -> None:
        token_count = max(0, int(tokens or 0))
        if token_count <= 0:
            return
        model = (model or "unknown").strip() or "unknown"
        self.unpriced_tokens += token_count
        self.unpriced_models[model] = self.unpriced_models.get(model, 0) + token_count

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
    conversation_id: str = ""
    request_key: str = ""
    route: str = ""
    request_at: datetime | None = None
    account_at: datetime | None = None
    quota_fingerprints: tuple[tuple[int, int], ...] = ()
    account_label_hint: str = ""
    account_hint_source: str = ""
    pricing_model: str = ""
    source_request_key: str = ""
    usage_provenance: str = ""
    reconciliation_status: str = ""
    canonical_id: str = ""
    supersedes_event_ids: tuple[str, ...] = ()

    @property
    def total_tokens(self) -> int:
        return max(0, self.input_tokens) + max(0, self.cached_tokens) + max(0, self.output_tokens)


@dataclass(frozen=True)
class OpenCodexAccountSnapshot:
    when: datetime
    label: str
    plan_type: str = ""


@dataclass(frozen=True)
class OpenCodexUsageMarker:
    request_at: datetime
    when: datetime
    model: str
    input_tokens: int
    cached_tokens: int
    output_tokens: int
    total_tokens: int
    label: str = ""
    account_log_label: str = ""
    request_id: str = ""
    conversation_id: str = ""
    attempt_ordinal: int = 0
    provider: str = ""
    requested_model: str = ""
    resolved_model: str = ""
    response_service_tier: str = ""
    requested_service_tier: str = ""
    requested_speed_label: str = ""
    pricing_tier: str = ""
    pricing_model: str = ""
    app_speed: str = ""
    admission_kind: str = ""
    inbound_protocol: str = ""
    usage_status: str = ""
    status: int = 0
    route_kind: str = ""
    source_instance: str = ""
    provenance: str = OPENCODEX_USAGE_PROVENANCE


@dataclass
class OpenCodexReconciliationDiagnostics:
    requests: dict[str, int] = field(
        default_factory=lambda: {
            "matched": 0,
            "proxy_only": 0,
            "local_only": 0,
            "ambiguous": 0,
            "conflict": 0,
            "rejected": 0,
        }
    )
    tokens: dict[str, int] = field(
        default_factory=lambda: {
            "matched": 0,
            "proxy_only": 0,
            "local_only": 0,
            "ambiguous": 0,
            "conflict": 0,
            "rejected": 0,
        }
    )

    def add(self, status: str, token_count: int) -> None:
        if status not in self.requests:
            return
        self.requests[status] += 1
        self.tokens[status] += max(0, int(token_count or 0))

    def as_dict(self) -> dict[str, dict[str, int]]:
        return {
            "requests": dict(self.requests),
            "tokens": dict(self.tokens),
        }


@dataclass
class OpenCodexReconciliationResult:
    events: list[UsageEvent]
    diagnostics: OpenCodexReconciliationDiagnostics


@dataclass
class ClaudeUsageEvent:
    event_id: str
    when: datetime
    model: str
    input_tokens: int
    output_tokens: int
    cache_creation_tokens: int
    cache_read_tokens: int
    pricing_tier: str = "standard"

    @property
    def total_tokens(self) -> int:
        return (
            max(0, self.input_tokens)
            + max(0, self.output_tokens)
            + max(0, self.cache_creation_tokens)
            + max(0, self.cache_read_tokens)
        )


def live_usage_event_id(event: UsageEvent) -> str:
    canonical_id = str(event.canonical_id or "").strip()
    if canonical_id:
        return canonical_id
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
    request_id: str = ""
    account_id: str = ""
    latency_ms: int = 0


@dataclass
class CockpitAffinityEvent:
    when: datetime
    request_id: str
    source: str = ""
    session_key: str = ""
    account_id: str = ""
    label: str = ""
    action: str = ""
    confirmed: bool = False


@dataclass
class CockpitAffinitySegment:
    request_id: str
    index: int
    start_at: datetime
    end_at: datetime | None = None
    events: list[CockpitAffinityEvent] = field(default_factory=list)
    started_after_failure: bool = False
    failed: bool = False

    @property
    def key(self) -> tuple[str, int]:
        return (self.request_id, self.index)


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

ONLINE_PRICE_PROVIDERS = frozenset({"openai", "anthropic", "xai", "deepseek"})
ONLINE_PRICE_PROVIDER_PREFIXES = tuple(f"{provider}/" for provider in sorted(ONLINE_PRICE_PROVIDERS))
MODEL_ROUTE_PREFIXES = frozenset({
    "openai",
    "anthropic",
    "xai",
    "deepseek",
    "opencode-go",
})
CACHE_READ_COST_ALIASES = (
    "cache_read_input_token_cost",
    "input_cost_per_token_cache_hit",
)
MODELS_DEV_COST_FIELD_MAP = (
    ("input", "input_cost_per_token"),
    ("output", "output_cost_per_token"),
    ("cache_read", "cache_read_input_token_cost"),
    ("cache_write", "cache_creation_input_token_cost"),
)

# Keep provider-qualified OpenCode Go fallbacks exact for times when neither
# online catalog has the route, so they cannot leak into bare model IDs.
OPENCODE_GO_OFFICIAL_PRICES = {
    "opencode-go/kimi-k3": {
        "input_cost_per_token": 3.0,
        "cache_read_input_token_cost": 0.30,
        "output_cost_per_token": 15.0,
    },
    "opencode-go/deepseek-v4-pro": {
        "input_cost_per_token": 0.66,
        "cache_read_input_token_cost": 0.022,
        "output_cost_per_token": 1.98,
    },
    "opencode-go/deepseek-v4-flash": {
        "input_cost_per_token": 0.22,
        "cache_read_input_token_cost": 0.007,
        "output_cost_per_token": 0.66,
    },
}
OPENCODE_GO_PEAK_PRICES = {
    "opencode-go/deepseek-v4-pro": {
        "input_cost_per_token": 1.32,
        "cache_read_input_token_cost": 0.044,
        "output_cost_per_token": 3.96,
    },
    "opencode-go/deepseek-v4-flash": {
        "input_cost_per_token": 0.44,
        "cache_read_input_token_cost": 0.014,
        "output_cost_per_token": 1.32,
    },
}


def model_price_candidates(model: str) -> tuple[str, ...]:
    name = str(model or "").strip().lower()
    if not name:
        return ()
    candidates = [name]
    if "/" in name:
        prefix, remainder = name.split("/", 1)
        if prefix in MODEL_ROUTE_PREFIXES and prefix != "opencode-go" and remainder:
            candidates.append(remainder)
    return tuple(dict.fromkeys(candidates))


def _models_dev_provider_catalog(payload: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(payload, dict):
        return {}
    catalog: dict[str, dict[str, Any]] = {}
    for provider_id, row in payload.items():
        if not isinstance(row, dict):
            continue
        models = row.get("models")
        if not isinstance(models, dict) or not models:
            continue
        provider = str(row.get("id") or provider_id or "").strip().lower()
        if provider:
            catalog[provider] = row
    return catalog


def is_models_dev_price_payload(payload: Any) -> bool:
    catalog = _models_dev_provider_catalog(payload)
    if not catalog:
        return False
    return any(
        isinstance(row, dict) and "litellm_provider" not in row
        for row in catalog.values()
    )


def _positive_models_dev_million_cost(value: Any) -> float:
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return 0.0
    return amount if amount > 0 else 0.0


def _models_dev_cost_to_token_fields(cost: Any) -> dict[str, float]:
    if not isinstance(cost, dict):
        return {}
    detail: dict[str, float] = {}
    for source_field, target_field in MODELS_DEV_COST_FIELD_MAP:
        amount = _positive_models_dev_million_cost(cost.get(source_field))
        if amount > 0:
            detail[target_field] = amount
    if detail.get("input_cost_per_token", 0) <= 0 or detail.get("output_cost_per_token", 0) <= 0:
        return {}
    return detail


def _models_dev_route_names(provider: str, model_id: str) -> tuple[str, ...]:
    names: list[str] = []
    if provider in ONLINE_PRICE_PROVIDERS:
        if model_id.startswith(f"{provider}/"):
            remainder = model_id.split("/", 1)[1]
            names.extend([model_id, remainder])
        elif "/" in model_id:
            remainder = model_id.rsplit("/", 1)[-1]
            names.extend([model_id, f"{provider}/{remainder}", remainder])
        else:
            names.extend([model_id, f"{provider}/{model_id}"])
    elif provider == "opencode-go":
        remainder = model_id.rsplit("/", 1)[-1] if "/" in model_id else model_id
        if remainder:
            names.append(f"opencode-go/{remainder}")
    elif provider in MODEL_ROUTE_PREFIXES:
        return ()
    else:
        names.append(f"{provider}/{model_id}")
        remainder = model_id.rsplit("/", 1)[-1]
        if remainder:
            names.append(f"{provider}/{remainder}")
        prefix = model_id.split("/", 1)[0] if "/" in model_id else ""
        if prefix and prefix not in MODEL_ROUTE_PREFIXES:
            names.append(model_id)
    return tuple(dict.fromkeys(name for name in names if name))


def flatten_models_dev_price_payload(payload: Any) -> dict[str, dict[str, Any]]:
    flattened: dict[str, dict[str, Any]] = {}
    catalog = _models_dev_provider_catalog(payload)
    official_providers = [
        provider for provider in ("openai", "anthropic", "xai", "deepseek") if provider in catalog
    ]
    remaining_providers = [
        provider for provider in catalog if provider not in set(official_providers)
    ]
    for provider in official_providers + remaining_providers:
        row = catalog[provider]
        models = row.get("models")
        if not isinstance(models, dict):
            continue
        for raw_name, model_row in models.items():
            if not isinstance(model_row, dict):
                continue
            detail = _models_dev_cost_to_token_fields(model_row.get("cost"))
            if not detail:
                continue
            model_id = str(model_row.get("id") or raw_name or "").strip().lower()
            if not model_id:
                continue
            token_costs = {
                field_name: value / 1_000_000
                for field_name, value in detail.items()
            }
            for name in _models_dev_route_names(provider, model_id):
                if name in flattened:
                    continue
                flattened[name] = {
                    "litellm_provider": provider,
                    **token_costs,
                }
    return flattened


def _positive_token_cost(row: dict[str, Any], field_name: str) -> float:
    try:
        value = float(row.get(field_name) or 0) * 1_000_000
    except (TypeError, ValueError):
        return 0.0
    return value if value > 0 else 0.0


def extract_online_price_details(payload: Any) -> dict[str, dict[str, float]]:
    if not isinstance(payload, dict):
        return {}
    if is_models_dev_price_payload(payload):
        payload = flatten_models_dev_price_payload(payload)
    prices: dict[str, dict[str, float]] = {}
    for raw_name, row in payload.items():
        if not isinstance(row, dict):
            continue
        provider = str(row.get("litellm_provider") or "").strip().lower()
        detail: dict[str, float] = {}
        for field_name in ONLINE_TOKEN_COST_FIELDS:
            value = _positive_token_cost(row, field_name)
            if value > 0:
                detail[field_name] = value
        cache_read = 0.0
        for alias in CACHE_READ_COST_ALIASES:
            cache_read = _positive_token_cost(row, alias)
            if cache_read > 0:
                detail["cache_read_input_token_cost"] = cache_read
                break
        if detail.get("input_cost_per_token", 0) <= 0 or detail.get("output_cost_per_token", 0) <= 0:
            continue
        name = str(raw_name or "").strip().lower()
        if not name:
            continue
        prices[name] = detail
        if provider in ONLINE_PRICE_PROVIDERS and name.startswith(f"{provider}/"):
            prices.setdefault(name.split("/", 1)[1], detail)
        elif not provider and name.startswith(ONLINE_PRICE_PROVIDER_PREFIXES):
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


def _price_table_from_details(
    details: dict[str, dict[str, float]],
) -> dict[str, tuple[float, float, float]]:
    return {
        name: (
            detail["input_cost_per_token"],
            detail.get("cache_read_input_token_cost", detail["input_cost_per_token"]),
            detail["output_cost_per_token"],
        )
        for name, detail in details.items()
    }


def _cache_models_from_details(
    details: dict[str, dict[str, float]],
) -> dict[str, dict[str, float]]:
    return {
        name: {field_name: value / 1_000_000 for field_name, value in detail.items()}
        for name, detail in details.items()
    }


def _price_cache_is_fresh(fetched_at: float, now_timestamp: float) -> bool:
    return 0 <= now_timestamp - fetched_at < MODEL_PRICE_CACHE_SECONDS


def _price_refresh_is_cooled_down(last_attempt_at: float, now_timestamp: float) -> bool:
    return 0 <= now_timestamp - last_attempt_at < MODEL_PRICE_REFRESH_COOLDOWN_SECONDS


def _read_model_price_cache() -> dict[str, Any]:
    try:
        cached = json.loads(MODEL_PRICE_CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return cached if isinstance(cached, dict) else {}


def _write_model_price_cache(
    details: dict[str, dict[str, float]],
    fetched_at: float,
    last_attempt_at: float,
) -> None:
    write_json_atomic(
        MODEL_PRICE_CACHE_PATH,
        {
            "schema": 2,
            "source": MODEL_PRICE_SOURCE_URL,
            "fetched_at": fetched_at,
            "last_attempt_at": last_attempt_at,
            "models": _cache_models_from_details(details),
        },
    )


def _remember_online_prices(
    details: dict[str, dict[str, float]],
    fetched_at: float | None,
    last_attempt_at: float | None,
) -> dict[str, tuple[float, float, float]]:
    global _ONLINE_PRICE_TABLE, _ONLINE_PRICE_DETAILS
    global _ONLINE_PRICE_FETCHED_AT, _ONLINE_PRICE_LAST_ATTEMPT_AT, _ONLINE_PRICE_CACHE_PATH
    table = _price_table_from_details(details)
    _ONLINE_PRICE_DETAILS = details
    _ONLINE_PRICE_TABLE = table
    _ONLINE_PRICE_FETCHED_AT = fetched_at
    _ONLINE_PRICE_LAST_ATTEMPT_AT = last_attempt_at
    try:
        _ONLINE_PRICE_CACHE_PATH = str(MODEL_PRICE_CACHE_PATH.resolve())
    except OSError:
        _ONLINE_PRICE_CACHE_PATH = str(MODEL_PRICE_CACHE_PATH)
    return table


def _fetch_json_price_payload(url: str) -> dict[str, Any] | None:
    req = request.Request(
        url,
        headers={"User-Agent": "token-floating-monitor/1.0"},
    )
    with request.urlopen(req, timeout=MODEL_PRICE_FETCH_TIMEOUT_SECONDS) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return payload if isinstance(payload, dict) else None


def _fetch_online_price_payload() -> dict[str, Any] | None:
    return _fetch_json_price_payload(MODEL_PRICE_SOURCE_URL)


def _normalized_price_source_url(url: str) -> str:
    return str(url or "").strip().rstrip("/")


def _fetch_models_dev_price_payload() -> dict[str, Any] | None:
    url = _normalized_price_source_url(MODELS_DEV_PRICE_SOURCE_URL)
    if not url:
        return None
    if url == _normalized_price_source_url(MODEL_PRICE_SOURCE_URL):
        return None
    return _fetch_json_price_payload(url)


def _merge_online_price_details(
    primary: dict[str, dict[str, float]],
    extra: dict[str, dict[str, float]],
) -> dict[str, dict[str, float]]:
    merged = dict(primary)
    for name, detail in extra.items():
        merged.setdefault(name, detail)
    return merged


def opencode_go_official_price_details(
    model: str,
    when: datetime | None = None,
) -> dict[str, float] | None:
    name = str(model or "").strip().lower()
    standard = OPENCODE_GO_OFFICIAL_PRICES.get(name)
    if standard is None:
        return None
    if name not in OPENCODE_GO_PEAK_PRICES or not opencode_go_peak_at(when):
        return dict(standard)
    return dict(OPENCODE_GO_PEAK_PRICES[name])


def opencode_go_peak_at(when: datetime | None) -> bool:
    if when is None:
        return False
    aware = when if when.tzinfo is not None else when.replace(tzinfo=LOCAL_TZ)
    utc_hour = aware.astimezone(timezone.utc).hour
    return 1 <= utc_hour < 4 or 6 <= utc_hour < 10


def apply_opencode_go_time_pricing(
    model: str,
    detail: dict[str, float],
    when: datetime | None,
) -> dict[str, float]:
    name = str(model or "").strip().lower()
    standard = OPENCODE_GO_OFFICIAL_PRICES.get(name)
    peak = OPENCODE_GO_PEAK_PRICES.get(name)
    if standard is None or peak is None or not opencode_go_peak_at(when):
        return detail
    adjusted = dict(detail)
    for field_name, peak_price in peak.items():
        standard_price = float(standard.get(field_name) or 0)
        current_price = float(adjusted.get(field_name) or 0)
        if standard_price > 0 and current_price > 0:
            adjusted[field_name] = current_price * peak_price / standard_price
    return adjusted


def _refresh_online_price_table(
    cached_details: dict[str, dict[str, float]],
    fetched_at: float,
    now_timestamp: float,
) -> dict[str, tuple[float, float, float]]:
    online_details: dict[str, dict[str, float]] = {}
    try:
        payload = _fetch_online_price_payload()
        online_details = extract_online_price_details(payload)
    except (OSError, ValueError, json.JSONDecodeError):
        online_details = {}
    try:
        models_dev_payload = _fetch_models_dev_price_payload()
        if models_dev_payload:
            online_details = _merge_online_price_details(
                online_details,
                extract_online_price_details(models_dev_payload),
            )
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    if online_details:
        # A successful primary refresh must not erase provider-exact prices
        # retained from a temporarily unavailable secondary source.
        online_details = _merge_online_price_details(online_details, cached_details)
        _write_model_price_cache(online_details, now_timestamp, now_timestamp)
        return _remember_online_prices(online_details, now_timestamp, now_timestamp)
    if cached_details:
        try:
            _write_model_price_cache(cached_details, fetched_at, now_timestamp)
        except OSError:
            pass
        return _remember_online_prices(cached_details, fetched_at, now_timestamp)
    return _remember_online_prices({}, fetched_at, now_timestamp)


def load_online_price_table(*, force_refresh: bool = False) -> dict[str, tuple[float, float, float]]:
    now_timestamp = datetime.now().timestamp()
    try:
        cache_path = str(MODEL_PRICE_CACHE_PATH.resolve())
    except OSError:
        cache_path = str(MODEL_PRICE_CACHE_PATH)
    memory_ready = _ONLINE_PRICE_TABLE is not None and _ONLINE_PRICE_DETAILS is not None
    injected_memory = memory_ready and _ONLINE_PRICE_CACHE_PATH is None
    memory_matches_path = _ONLINE_PRICE_CACHE_PATH in {None, cache_path}
    if injected_memory and not force_refresh:
        return _ONLINE_PRICE_TABLE or {}
    if memory_ready and memory_matches_path and not force_refresh:
        return _ONLINE_PRICE_TABLE or {}
    if (
        force_refresh
        and memory_ready
        and memory_matches_path
        and (_ONLINE_PRICE_LAST_ATTEMPT_AT or 0) > 0
        and _price_refresh_is_cooled_down(_ONLINE_PRICE_LAST_ATTEMPT_AT or 0, now_timestamp)
    ):
        return _ONLINE_PRICE_TABLE or {}

    cached = _read_model_price_cache()
    cached_details = extract_online_price_details(cached.get("models"))
    try:
        fetched_at = float(cached.get("fetched_at") or 0)
    except (TypeError, ValueError):
        fetched_at = 0.0
    try:
        last_attempt_at = float(cached.get("last_attempt_at") or 0)
    except (TypeError, ValueError):
        last_attempt_at = fetched_at
    schema_ok = int(cached.get("schema") or 0) >= 2
    if (
        not force_refresh
        and schema_ok
        and cached_details
        and _price_cache_is_fresh(fetched_at, now_timestamp)
    ):
        return _remember_online_prices(cached_details, fetched_at, last_attempt_at)
    if force_refresh and last_attempt_at > 0 and _price_refresh_is_cooled_down(last_attempt_at, now_timestamp):
        if cached_details:
            return _remember_online_prices(cached_details, fetched_at, last_attempt_at)
        if memory_ready and memory_matches_path:
            return _ONLINE_PRICE_TABLE or {}
        return _remember_online_prices(cached_details, fetched_at, last_attempt_at)
    return _refresh_online_price_table(cached_details, fetched_at, now_timestamp)


def _lookup_online_price_details(model: str) -> dict[str, float] | None:
    candidates = model_price_candidates(model)
    if not candidates:
        return None
    details = _ONLINE_PRICE_DETAILS or {}
    for candidate in candidates:
        detail = details.get(candidate)
        if detail:
            return detail
    return None


def _lookup_online_price(model: str) -> tuple[float, float, float] | None:
    candidates = model_price_candidates(model)
    if not candidates:
        return None
    prices = _ONLINE_PRICE_TABLE or {}
    for candidate in candidates:
        price = prices.get(candidate)
        if price:
            return price
    return None


def online_model_price(model: str) -> tuple[float, float, float] | None:
    if not model_price_candidates(model):
        return None
    load_online_price_table()
    price = _lookup_online_price(model)
    if price:
        return price
    if _ONLINE_PRICE_CACHE_PATH is None:
        return None
    load_online_price_table(force_refresh=True)
    return _lookup_online_price(model)


def online_model_price_details(model: str) -> dict[str, float] | None:
    if not model_price_candidates(model):
        return None
    load_online_price_table()
    detail = _lookup_online_price_details(model)
    if detail:
        return detail
    if _ONLINE_PRICE_CACHE_PATH is None:
        return None
    load_online_price_table(force_refresh=True)
    return _lookup_online_price_details(model)


def hardcoded_model_price(model: str) -> tuple[float, float, float] | None:
    name = (model or "").lower()
    for needle, price in PRICE_PER_MILLION:
        if needle in name:
            return price
    return None


def fallback_family_model_price(model: str) -> tuple[float, float, float] | None:
    name = (model or "").lower()
    if re.search(r"\bgpt-5(?:\.|\b)", name):
        return next(price for needle, price in PRICE_PER_MILLION if needle == "gpt-5.5")
    return None


def model_price(model: str) -> tuple[float, float, float]:
    hardcoded = hardcoded_model_price(model)
    if hardcoded is not None:
        return hardcoded
    online_price = online_model_price(model)
    if online_price is not None:
        return online_price
    return fallback_family_model_price(model) or (0.0, 0.0, 0.0)


def price_profile_from_rates(
    rates: tuple[float, float, float],
) -> dict[str, float]:
    input_price, cache_price, output_price = rates
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




def resolve_model_price_details(
    model: str,
    pricing_model: str = "",
    when: datetime | None = None,
) -> tuple[dict[str, float], bool]:
    lookup_model = str(pricing_model or model or "").strip() or str(model or "")
    exact_candidates = tuple(
        dict.fromkeys(
            str(candidate or "").strip().lower()
            for candidate in (lookup_model, model)
            if str(candidate or "").strip().lower().startswith("opencode-go/")
        )
    )
    if exact_candidates:
        load_online_price_table()
        for candidate in exact_candidates:
            exact_online = (_ONLINE_PRICE_DETAILS or {}).get(candidate)
            if exact_online is not None:
                return apply_opencode_go_time_pricing(candidate, exact_online, when), True
        if _ONLINE_PRICE_CACHE_PATH is not None:
            load_online_price_table(force_refresh=True)
            for candidate in exact_candidates:
                exact_online = (_ONLINE_PRICE_DETAILS or {}).get(candidate)
                if exact_online is not None:
                    return apply_opencode_go_time_pricing(candidate, exact_online, when), True
        for candidate in exact_candidates:
            official = opencode_go_official_price_details(candidate, when=when)
            if official is not None:
                return official, True
        return {}, False
    online_details = online_model_price_details(lookup_model)
    if online_details is None and lookup_model != model:
        online_details = online_model_price_details(model)
    if online_details is not None:
        return online_details, True
    online_rates = online_model_price(lookup_model)
    if online_rates is None and lookup_model != model:
        online_rates = online_model_price(model)
    if online_rates is not None:
        return price_profile_from_rates(online_rates), True
    local_rates = hardcoded_model_price(lookup_model) or hardcoded_model_price(model)
    if local_rates is None:
        local_rates = fallback_family_model_price(lookup_model) or fallback_family_model_price(model)
    if local_rates is None:
        return {}, False
    return price_profile_from_rates(local_rates), True

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
    pricing_model: str = "",
    when: datetime | None = None,
) -> float:
    cost, _resolved = estimate_cost_with_resolution(
        model,
        input_tokens,
        cached_tokens,
        output_tokens,
        cache_creation_tokens=cache_creation_tokens,
        pricing_tier=pricing_tier,
        pricing_model=pricing_model,
        when=when,
    )
    return cost


def estimate_cost_with_resolution(
    model: str,
    input_tokens: int,
    cached_tokens: int,
    output_tokens: int,
    cache_creation_tokens: int = 0,
    pricing_tier: str = "standard",
    pricing_model: str = "",
    when: datetime | None = None,
) -> tuple[float, bool]:
    profile, resolved = resolve_model_price_details(
        model,
        pricing_model=pricing_model,
        when=when,
    )
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
    cost = (
        input_count * input_price
        + cached_count * cache_price
        + cache_creation_count * cache_creation_price
        + output_count * output_price
    ) / 1_000_000
    return cost, resolved


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


def is_official_codex_quota_model(model: str) -> bool:
    """Whether a local event can plausibly consume an OpenAI Codex quota.

    A provider-qualified model name belongs to an external route even when the
    local Codex session happens to retain the same account label. The fallback
    is intentionally limited to the unqualified model families exposed by the
    official Codex client; an exact quota fingerprint remains stronger evidence.
    """
    name = str(model or "").strip().lower()
    if not name or "/" in name:
        return False
    return name.startswith(("gpt-", "o1", "o3", "o4", "o5", "codex-", "chatgpt-"))


def external_codex_provider_label(model: str) -> str:
    name = str(model or "").strip().lower()
    if name.startswith("xai/"):
        return GROK_SUBAGENT_LABEL
    if name.startswith("opencode-go/"):
        return OPENCODE_SUBAGENT_LABEL
    return ""


def is_external_codex_provider_label(label: str) -> bool:
    return str(label or "").strip() in EXTERNAL_CODEX_PROVIDER_LABELS


def is_codex_account_provider_name(name: str) -> bool:
    label = str(name or "").strip()
    if not label or is_external_codex_provider_label(label):
        return False
    if label in {GROK_LOCAL_LABEL, "Claude local"}:
        return False
    return label.startswith("Codex local") or label == UNASSIGNED_CODEX_LABEL


def assign_external_codex_provider_label(
    event: UsageEvent,
    ledger: dict[str, str] | None,
) -> str:
    label = external_codex_provider_label(event.model)
    if not label:
        return ""
    if ledger is not None:
        stable_id = codex_event_id(event)
        if stable_id:
            ledger_assign(ledger, stable_id, label)
        legacy_id = legacy_codex_event_id(event)
        if legacy_id and legacy_id != stable_id:
            ledger_assign(ledger, legacy_id, label)
    return label


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




def make_codex_event(
    model: str,
    input_tokens: int,
    cached_tokens: int,
    output_tokens: int,
    when: datetime | None,
    app_speed: str = "",
    cost_multiplier: float | None = None,
    session_id: str = "",
    conversation_id: str = "",
    request_key: str = "",
    route: str = "",
    request_at: datetime | None = None,
    account_at: datetime | None = None,
    pricing_tier: str = "",
    quota_fingerprints: tuple[tuple[int, int], ...] = (),
    pricing_model: str = "",
    usage_provenance: str = "",
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
        conversation_id=str(conversation_id or "").strip().casefold(),
        request_key=str(request_key or "").strip(),
        route=str(route or "").strip().lower(),
        request_at=request_at,
        account_at=account_at,
        quota_fingerprints=tuple(quota_fingerprints or ()),
        pricing_model=str(pricing_model or "").strip(),
        usage_provenance=str(usage_provenance or "").strip(),
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
    cost, price_resolved = estimate_cost_with_resolution(
        event.model,
        event.input_tokens,
        event.cached_tokens,
        event.output_tokens,
        pricing_tier=pricing_tier,
        pricing_model=event.pricing_model,
        when=event.when,
    )
    bucket.cost += cost
    if not price_resolved:
        bucket.add_unpriced_model(event.model, event.total_tokens)
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
    target.unpriced_tokens += source.unpriced_tokens
    for model, tokens in source.unpriced_models.items():
        target.unpriced_models[model] = target.unpriced_models.get(model, 0) + tokens
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


def quota_reset_epoch(value: Any) -> int:
    if isinstance(value, (int, float)):
        epoch = int(value)
        if epoch > 10_000_000_000:
            epoch //= 1000
        return max(0, epoch)
    reset_at = parse_dt(value)
    if reset_at is None:
        return 0
    return max(0, int(reset_at.replace(tzinfo=LOCAL_TZ).timestamp()))


def quota_window_fingerprint(window: Any) -> tuple[int, int] | None:
    if not isinstance(window, dict):
        return None
    try:
        window_minutes = int(window.get("window_minutes") or 0)
    except (TypeError, ValueError):
        window_minutes = 0
    reset_epoch = quota_reset_epoch(window.get("resets_at"))
    if window_minutes <= 0 or reset_epoch <= 0:
        return None
    return window_minutes, reset_epoch


def codex_quota_fingerprints(rate_limits: Any) -> tuple[tuple[int, int], ...]:
    if not isinstance(rate_limits, dict):
        return ()
    fingerprints = {
        fingerprint
        for value in rate_limits.values()
        if (fingerprint := quota_window_fingerprint(value)) is not None
    }
    return tuple(sorted(fingerprints, reverse=True))


def event_counts_toward_official_quota_window(
    event: UsageEvent,
    quota_window: Any,
) -> bool:
    """Keep quota cards tied to the official model family and reset window.

    Cockpit's per-request usage is ideal when it exists. Local events can
    arrive without a matching Cockpit row, however, so an unqualified official
    Codex model is accepted as the bounded fallback. Provider-qualified
    DeepSeek/Grok routes never inflate an OpenAI account's quota card, even
    when they happen to carry the same official quota fingerprint.
    """
    if "/" in str(event.model or "") or external_codex_provider_label(event.model):
        return False
    fingerprint = quota_window_fingerprint(quota_window)
    if fingerprint is not None and fingerprint in event.quota_fingerprints:
        return True
    return is_official_codex_quota_model(event.model)


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
        usage_provenance="codex-logs2",
    )






CODEX_TOKEN_USAGE_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)

CODEX_RELEVANT_EVENT_TYPES = frozenset(
    {
        "error",
        "task_complete",
        "task_started",
        "token_count",
        "turn_aborted",
        "turn_complete",
        "turn_started",
    }
)


def compact_codex_cache_row(row: dict[str, Any]) -> dict[str, Any] | None:
    """Keep only fields needed by the usage scanner; never cache conversation text."""
    row_type = str(row.get("type") or "")
    payload = row.get("payload") or {}
    if row_type == "session_meta":
        return {
            "type": row_type,
            "timestamp": row.get("timestamp"),
            "payload": {
                key: payload.get(key)
                for key in (
                    "id",
                    "session_id",
                    "parent_thread_id",
                    "forked_from_id",
                    "timestamp",
                )
                if payload.get(key) is not None
            },
        }
    if row_type == "turn_context":
        compact = {"type": row_type}
        if row.get("model") is not None:
            compact["model"] = row.get("model")
        if payload.get("model") is not None:
            compact["payload"] = {"model": payload.get("model")}
        return compact
    if row_type != "event_msg":
        return None
    payload_type = str(payload.get("type") or "")
    if payload_type not in CODEX_RELEVANT_EVENT_TYPES:
        return None

    compact_payload: dict[str, Any] = {"type": payload_type}
    if payload.get("turn_id") is not None:
        compact_payload["turn_id"] = payload.get("turn_id")
    if payload.get("model") is not None:
        compact_payload["model"] = payload.get("model")
    if payload_type == "token_count":
        info = payload.get("info") or {}
        compact_info: dict[str, Any] = {}
        for key in ("total_token_usage", "last_token_usage"):
            usage = info.get(key)
            if isinstance(usage, dict):
                compact_info[key] = {
                    field: usage.get(field)
                    for field in CODEX_TOKEN_USAGE_FIELDS
                    if usage.get(field) is not None
                }
        if compact_info:
            compact_payload["info"] = compact_info
        rate_limits = payload.get("rate_limits")
        if isinstance(rate_limits, dict):
            compact_rate_limits: dict[str, Any] = {}
            plan_type = str(rate_limits.get("plan_type") or "").strip()
            if plan_type:
                compact_rate_limits["plan_type"] = plan_type
            for key, value in rate_limits.items():
                if not isinstance(value, dict):
                    continue
                fingerprint = quota_window_fingerprint(value)
                if fingerprint is None:
                    continue
                compact_rate_limits[str(key)] = {
                    field: value.get(field)
                    for field in ("used_percent", "window_minutes", "resets_at")
                    if value.get(field) is not None
                }
            if len(compact_rate_limits) > bool(plan_type):
                compact_payload["rate_limits"] = compact_rate_limits
    elif payload_type == "error":
        if payload.get("codex_error_info") is not None:
            compact_payload["codex_error_info"] = payload.get("codex_error_info")
    elif isinstance(payload.get("error"), dict):
        error_info = payload["error"].get("codex_error_info")
        if error_info is not None:
            compact_payload["error"] = {"codex_error_info": error_info}

    compact_row: dict[str, Any] = {
        "type": row_type,
        "timestamp": row.get("timestamp"),
        "payload": compact_payload,
    }
    if row.get("model") is not None:
        compact_row["model"] = row.get("model")
    return compact_row


def compact_opencodex_usage_row(row: dict[str, Any]) -> dict[str, Any] | None:
    """Retain only final OpenAI routing evidence and reported usage fields."""
    def token_value(source: dict[str, Any], key: str) -> int:
        try:
            return max(0, int(source.get(key) or 0))
        except (TypeError, ValueError, OverflowError):
            return 0

    selected: dict[str, Any] = row
    selected_usage = row.get("usage")
    attempts = row.get("attempts")
    if isinstance(attempts, list):
        positive_attempts: list[tuple[int, int, dict[str, Any], dict[str, Any]]] = []
        for position, attempt in enumerate(attempts):
            if not isinstance(attempt, dict):
                continue
            attempt_usage = attempt.get("usage")
            if not isinstance(attempt_usage, dict):
                continue
            reported = token_value(attempt_usage, "totalTokens")
            components = max(
                token_value(attempt_usage, "inputTokens"),
                token_value(attempt_usage, "cachedInputTokens"),
                token_value(attempt_usage, "outputTokens"),
            )
            if max(reported, components) <= 0:
                continue
            try:
                ordinal = int(attempt.get("ordinal") or position + 1)
            except (TypeError, ValueError, OverflowError):
                ordinal = position + 1
            positive_attempts.append((ordinal, position, attempt, attempt_usage))
        if positive_attempts:
            _ordinal, _position, selected, selected_usage = max(
                positive_attempts,
                key=lambda item: (item[0], item[1]),
            )
    if not isinstance(selected_usage, dict):
        return None

    provider = str(selected.get("provider") or row.get("provider") or "").strip().lower()
    if provider != "openai" and not provider.startswith("openai-"):
        return None
    model = str(
        selected.get("resolvedModel")
        or selected.get("model")
        or row.get("resolvedModel")
        or row.get("model")
        or row.get("requestedModel")
        or ""
    ).strip()
    if not is_official_codex_quota_model(model):
        return None

    input_tokens = token_value(selected_usage, "inputTokens")
    cached_tokens = token_value(selected_usage, "cachedInputTokens")
    output_tokens = token_value(selected_usage, "outputTokens")
    reported_total = token_value(selected_usage, "totalTokens")
    if reported_total <= 0:
        reported_total = token_value(selected, "totalTokens")
    computed_total = input_tokens + output_tokens
    if max(reported_total, input_tokens, cached_tokens, output_tokens) <= 0:
        return None
    if cached_tokens > input_tokens or computed_total <= 0:
        return None
    # The local Codex event stores uncached input and cached input separately;
    # OpenCodex's inputTokens already includes cachedInputTokens.
    total_tokens = computed_total
    if reported_total > 0 and reported_total != computed_total:
        return None
    response_service_tier = str(
        selected.get("responseServiceTier")
        or row.get("responseServiceTier")
        or ""
    ).strip()
    requested_service_tier = str(
        selected.get("requestedServiceTier")
        or row.get("requestedServiceTier")
        or ""
    ).strip()
    requested_speed_label = str(
        selected.get("requestedSpeedLabel")
        or row.get("requestedSpeedLabel")
        or ""
    ).strip()
    route_decision = row.get("routeDecision")
    route_kind = (
        str(route_decision.get("routeKind") or "").strip()
        if isinstance(route_decision, dict)
        else ""
    )
    try:
        status = int(selected.get("status") or row.get("status") or 0)
    except (TypeError, ValueError, OverflowError):
        status = 0
    return {
        "timestamp": row.get("timestamp"),
        "duration_ms": row.get("durationMs"),
        "model": model,
        "provider": provider,
        "requested_model": row.get("requestedModel"),
        "resolved_model": model,
        "response_service_tier": response_service_tier,
        "requested_service_tier": requested_service_tier,
        "requested_speed_label": requested_speed_label,
        "pricing_tier": normalize_pricing_tier(response_service_tier),
        "pricing_model": (
            selected.get("pricingModel")
            or row.get("pricingModel")
            or model
        ),
        "admission_kind": row.get("admissionKind"),
        "inbound_protocol": row.get("inboundProtocol"),
        "usage_status": selected.get("usageStatus") or row.get("usageStatus"),
        "status": status,
        "route_kind": route_kind,
        "provenance": OPENCODEX_USAGE_PROVENANCE,
        "account_log_label": selected.get("accountLogLabel") or row.get("accountLogLabel"),
        "request_id": row.get("requestId"),
        "conversation_id": row.get("conversationId"),
        "attempt_ordinal": selected.get("ordinal") if selected is not row else 0,
        "input_tokens": input_tokens - cached_tokens,
        "cached_tokens": cached_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
    }


class CodexEventRowCache:
    """Persistent append-only cache for scanner-relevant JSONL records."""

    row_schema = CODEX_COMPACT_ROW_SCHEMA

    def __init__(self, cache_path: Path = CODEX_EVENT_CACHE_PATH) -> None:
        self.cache_path = cache_path
        self.entries: dict[str, dict[str, Any]] = {}
        self._persistent_keys: set[str] = set()
        self._loaded = False
        self._dirty = False

    @staticmethod
    def _key(path: Path) -> str:
        try:
            value = str(path.resolve())
        except OSError:
            value = str(path)
        return os.path.normcase(value)

    @staticmethod
    def _is_default_codex_path(path: Path) -> bool:
        if os.environ.get("CLIENT_USAGE_CODEX_EVENT_CACHE"):
            return True
        try:
            path.resolve().relative_to((Path.home() / ".codex").resolve())
            return True
        except (OSError, ValueError):
            return False

    @staticmethod
    def _compact_row(row: dict[str, Any]) -> dict[str, Any] | None:
        return compact_codex_cache_row(row)

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            payload = recover_corrupt_json(self.cache_path)
        except OSError:
            return
        if not isinstance(payload, dict):
            return
        if int(payload.get("schema") or 0) != CODEX_EVENT_CACHE_SCHEMA:
            return
        entries = payload.get("entries")
        if not isinstance(entries, dict):
            return
        for key, entry in entries.items():
            if not isinstance(key, str) or not isinstance(entry, dict):
                continue
            if not isinstance(entry.get("rows_zlib"), str):
                continue
            self.entries[key] = entry
        self._persistent_keys.update(self.entries)

    @staticmethod
    def _entry_rows(entry: dict[str, Any]) -> list[dict[str, Any]]:
        rows = entry.get("rows")
        if isinstance(rows, list):
            return rows
        encoded = entry.get("rows_zlib")
        if not isinstance(encoded, str) or not encoded:
            rows = []
        else:
            try:
                raw = zlib.decompress(base64.b64decode(encoded.encode("ascii")))
                decoded = json.loads(raw.decode("utf-8"))
                rows = decoded if isinstance(decoded, list) else []
            except (ValueError, TypeError, zlib.error, json.JSONDecodeError, UnicodeDecodeError):
                rows = []
        entry["rows"] = rows
        return rows

    @staticmethod
    def _serialized_entry(entry: dict[str, Any]) -> dict[str, Any]:
        result = {
            key: value
            for key, value in entry.items()
            if key not in {"rows", "rows_zlib", "_rows_dirty"}
        }
        encoded = entry.get("rows_zlib")
        if not isinstance(encoded, str) or bool(entry.get("_rows_dirty")):
            raw = json.dumps(
                CodexEventRowCache._entry_rows(entry),
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            encoded = base64.b64encode(zlib.compress(raw, level=6)).decode("ascii")
            entry["rows_zlib"] = encoded
            entry["_rows_dirty"] = False
        result["rows_zlib"] = encoded
        return result

    @staticmethod
    def _hash_range(path: Path, start: int, length: int) -> str:
        if length <= 0:
            return ""
        try:
            with path.open("rb") as handle:
                handle.seek(max(0, int(start)))
                data = handle.read(max(0, int(length)))
        except OSError:
            return ""
        return hashlib.sha256(data).hexdigest()

    def _append_is_valid(self, path: Path, entry: dict[str, Any], size: int) -> bool:
        try:
            old_size = int(entry.get("size") or 0)
            processed = int(entry.get("processed_length") or 0)
            head_length = int(entry.get("head_length") or 0)
            boundary_start = int(entry.get("boundary_start") or 0)
            boundary_length = int(entry.get("boundary_length") or 0)
        except (TypeError, ValueError):
            return False
        if old_size > size or processed < 0 or processed > old_size:
            return False
        if head_length and self._hash_range(path, 0, head_length) != entry.get("head_hash"):
            return False
        if (
            boundary_length
            and self._hash_range(path, boundary_start, boundary_length)
            != entry.get("boundary_hash")
        ):
            return False
        return True

    @classmethod
    def _read_complete_rows(
        cls,
        path: Path,
        start: int,
        end: int,
    ) -> tuple[list[dict[str, Any]], int]:
        rows: list[dict[str, Any]] = []
        processed = max(0, int(start))
        try:
            with path.open("rb") as handle:
                handle.seek(processed)
                while handle.tell() < end:
                    line_start = handle.tell()
                    raw_line = handle.readline(max(0, end - line_start))
                    if not raw_line:
                        processed = line_start
                        break
                    terminated = raw_line.endswith(b"\n")
                    try:
                        row = json.loads(raw_line.decode("utf-8", errors="ignore"))
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        if not terminated:
                            processed = line_start
                            break
                        processed = handle.tell()
                        continue
                    processed = handle.tell()
                    if not isinstance(row, dict):
                        continue
                    compact = cls._compact_row(row)
                    if compact is not None:
                        rows.append(compact)
        except OSError:
            return [], max(0, int(start))
        return rows, processed

    def rows_for_path(self, path: Path) -> list[dict[str, Any]]:
        persistent = self._is_default_codex_path(path)
        if persistent:
            self._load()
        key = self._key(path)
        if persistent:
            self._persistent_keys.add(key)
        try:
            stat = path.stat()
        except OSError:
            entry = self.entries.get(key)
            return self._entry_rows(entry) if entry is not None else []
        size = max(0, int(stat.st_size))
        modified_ns = int(stat.st_mtime_ns)
        entry = self.entries.get(key)
        current_row_schema = (
            int(entry.get("row_schema") or 0)
            if isinstance(entry, dict)
            else 0
        )
        if (
            entry is not None
            and current_row_schema >= self.row_schema
            and int(entry.get("size") or -1) == size
            and int(entry.get("mtime_ns") or -1) == modified_ns
        ):
            return self._entry_rows(entry)

        append = bool(
            entry is not None
            and current_row_schema >= self.row_schema
            and size > int(entry.get("size") or 0)
            and self._append_is_valid(path, entry, size)
        )
        if append:
            rows = list(self._entry_rows(entry))
            start = int(entry.get("processed_length") or 0)
        else:
            rows = []
            start = 0
        added, processed = self._read_complete_rows(path, start, size)
        rows.extend(added)

        head_length = min(CODEX_EVENT_CACHE_HASH_BYTES, processed)
        boundary_start = max(0, processed - CODEX_EVENT_CACHE_HASH_BYTES)
        boundary_length = max(0, processed - boundary_start)
        self.entries[key] = {
            "row_schema": self.row_schema,
            "size": size,
            "mtime_ns": modified_ns,
            "processed_length": processed,
            "head_length": head_length,
            "head_hash": self._hash_range(path, 0, head_length),
            "boundary_start": boundary_start,
            "boundary_length": boundary_length,
            "boundary_hash": self._hash_range(path, boundary_start, boundary_length),
            "rows": rows,
            "_rows_dirty": True,
        }
        if persistent:
            self._dirty = True
        return rows

    def cached_missing_paths(self, root: Path, start: datetime) -> list[Path]:
        self._load()
        allowed_roots: list[Path] = []
        for candidate in (root, root.parent / "archived_sessions"):
            try:
                allowed_roots.append(candidate.resolve(strict=False))
            except OSError:
                allowed_roots.append(candidate)
        threshold_ns = int((start - timedelta(hours=2)).timestamp() * 1_000_000_000)
        result: list[tuple[int, Path]] = []
        for key, entry in self.entries.items():
            try:
                modified_ns = int(entry.get("mtime_ns") or 0)
            except (TypeError, ValueError):
                continue
            if modified_ns < threshold_ns:
                continue
            path = Path(key)
            try:
                resolved = path.resolve(strict=False)
            except OSError:
                continue
            if not any(
                resolved.is_relative_to(allowed_root)
                for allowed_root in allowed_roots
            ):
                continue
            if path.exists():
                continue
            result.append((modified_ns, path))
        return [path for _modified_ns, path in sorted(result)]

    @staticmethod
    def _entry_rank(entry: dict[str, Any]) -> tuple[int, int, int, int]:
        def integer(key: str) -> int:
            try:
                return int(entry.get(key) or 0)
            except (TypeError, ValueError):
                return 0

        return (
            integer("mtime_ns"),
            integer("processed_length"),
            integer("size"),
            integer("row_schema"),
        )

    def _merge_entries_from_disk(self) -> None:
        try:
            payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(payload, dict):
            return
        if int(payload.get("schema") or 0) != CODEX_EVENT_CACHE_SCHEMA:
            return
        entries = payload.get("entries")
        if not isinstance(entries, dict):
            return
        for key, disk_entry in entries.items():
            if not isinstance(key, str) or not isinstance(disk_entry, dict):
                continue
            if not isinstance(disk_entry.get("rows_zlib"), str):
                continue
            current = self.entries.get(key)
            if current is None or self._entry_rank(disk_entry) > self._entry_rank(current):
                self.entries[key] = disk_entry
            self._persistent_keys.add(key)

    def flush(self) -> None:
        if not self._dirty or not self._persistent_keys:
            return
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.cache_path.with_name(f".{self.cache_path.name}.{os.getpid()}.tmp")
        try:
            with attribution_ledger_write_lock(self.cache_path):
                self._merge_entries_from_disk()
                keys = sorted(
                    (key for key in self._persistent_keys if key in self.entries),
                    key=lambda key: self._entry_rank(self.entries[key]),
                    reverse=True,
                )[:CODEX_EVENT_CACHE_MAX_ENTRIES]
                payload = {
                    "schema": CODEX_EVENT_CACHE_SCHEMA,
                    "updated_at": datetime.now().isoformat(timespec="seconds"),
                    "entries": {
                        key: self._serialized_entry(self.entries[key])
                        for key in keys
                    },
                }
                temporary.write_text(
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    encoding="utf-8",
                )
                os.replace(temporary, self.cache_path)
                self._dirty = False
                self._persistent_keys = set(keys)
            refresh_json_backup(self.cache_path)
        except (OSError, TimeoutError) as exc:
            logger.warning("Codex event cache save deferred: %s", exc)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


class OpenCodexUsageRowCache(CodexEventRowCache):
    row_schema = OPENCODEX_COMPACT_ROW_SCHEMA

    @staticmethod
    def _is_default_codex_path(path: Path) -> bool:
        return True

    @staticmethod
    def _compact_row(row: dict[str, Any]) -> dict[str, Any] | None:
        return compact_opencodex_usage_row(row)


_CODEX_EVENT_ROW_CACHE = CodexEventRowCache()
_OPENCODEX_USAGE_ROW_CACHE = OpenCodexUsageRowCache(OPENCODEX_USAGE_CACHE_PATH)
atexit.register(_CODEX_EVENT_ROW_CACHE.flush)
atexit.register(_OPENCODEX_USAGE_ROW_CACHE.flush)


def codex_relevant_rows_from_path(path: Path) -> list[dict[str, Any]]:
    return _CODEX_EVENT_ROW_CACHE.rows_for_path(path)


def codex_session_header_from_rows(rows: list[dict[str, Any]]) -> tuple[str, str]:
    for row in rows:
        if row.get("type") != "session_meta":
            continue
        payload = row.get("payload") or {}
        return (
            str(payload.get("id") or payload.get("session_id") or "").strip(),
            str(payload.get("forked_from_id") or "").strip(),
        )
    return "", ""


def codex_opencodex_conversation_id_from_rows(rows: list[dict[str, Any]]) -> str:
    for row in rows:
        if row.get("type") != "session_meta":
            continue
        payload = row.get("payload") or {}
        source_id = str(
            payload.get("session_id")
            or payload.get("parent_thread_id")
            or payload.get("id")
            or ""
        ).strip()
        if source_id:
            return hashlib.sha256(source_id.encode("utf-8")).hexdigest()[:32]
    return ""


def codex_fork_replay_cutoff_from_rows(rows: list[dict[str, Any]]) -> datetime | None:
    _session_id, parent_id = codex_session_header_from_rows(rows)
    if not parent_id:
        return None
    for row in rows:
        if row.get("type") == "session_meta":
            started = parse_dt(row.get("timestamp"))
            return started + timedelta(seconds=2) if started is not None else None
    return None


def codex_token_count_signature(payload: dict[str, Any]) -> tuple[int, ...]:
    info = payload.get("info") or {}
    total = info.get("total_token_usage") or {}
    last = info.get("last_token_usage") or {}
    # Field order stays CODEX_TOKEN_USAGE_FIELDS; the calls are spelled out
    # because this runs once per token_count row of every scanned session.
    return (
        int(bool(info.get("last_token_usage"))),
        usage_int(total, "input_tokens"),
        usage_int(total, "cached_input_tokens"),
        usage_int(total, "output_tokens"),
        usage_int(total, "reasoning_output_tokens"),
        usage_int(total, "total_tokens"),
        usage_int(last, "input_tokens"),
        usage_int(last, "cached_input_tokens"),
        usage_int(last, "output_tokens"),
        usage_int(last, "reasoning_output_tokens"),
        usage_int(last, "total_tokens"),
    )


_CODEX_SIGNATURE_CACHE: dict[
    int,
    tuple[list[dict[str, Any]], int, list[tuple[int, ...]]],
] = {}


def codex_token_count_signatures_for_rows(
    rows: list[dict[str, Any]],
) -> list[tuple[int, ...]]:
    """Signatures only depend on row content, so build them once per row list."""
    key = id(rows)
    cached = _CODEX_SIGNATURE_CACHE.get(key)
    if cached is not None and cached[0] is rows and cached[1] == len(rows):
        return cached[2]
    signatures: list[tuple[int, ...]] = []
    for row in rows:
        if row.get("type") != "event_msg":
            continue
        payload = row.get("payload") or {}
        if payload.get("type") == "token_count":
            signatures.append(codex_token_count_signature(payload))
    # The row list is kept alive so id() cannot be recycled behind the cache.
    _CODEX_SIGNATURE_CACHE[key] = (rows, len(rows), signatures)
    return signatures


def codex_token_count_signatures_from_path(path: Path) -> list[tuple[int, ...]]:
    return codex_token_count_signatures_for_rows(codex_relevant_rows_from_path(path))


def codex_state_rollout_paths(
    root: Path,
    start: datetime,
    session_paths: dict[str, Path] | None = None,
) -> tuple[list[Path], bool]:
    """Use Codex's thread index to avoid recursively walking every session file."""
    codex_root = root.parent
    try:
        sessions_root = root.resolve()
    except OSError:
        sessions_root = root
    databases = (
        codex_root / "state_5.sqlite",
        codex_root / "sqlite" / "state_5.sqlite",
    )
    threshold_ms = int(
        (start - timedelta(hours=2)).replace(tzinfo=LOCAL_TZ).timestamp() * 1000
    )
    candidates: list[Path] = []
    seen: set[Path] = set()
    database_available = False
    for database in databases:
        if not database.exists():
            continue
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
            columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(threads)").fetchall()
            }
            if "id" not in columns or "rollout_path" not in columns:
                connection.close()
                connection = None
                continue
            rows = connection.execute(
                """
                SELECT id, rollout_path
                FROM threads
                WHERE rollout_path IS NOT NULL AND rollout_path != ''
                """
            ).fetchall()
            database_available = True
        except sqlite3.Error:
            continue
        finally:
            if connection is not None:
                connection.close()
        for session_id, raw_path in rows:
            path_text = str(raw_path or "")
            if os.name == "nt" and path_text.startswith("\\\\?\\UNC\\"):
                path_text = "\\\\" + path_text[8:]
            elif os.name == "nt" and path_text.startswith("\\\\?\\"):
                path_text = path_text[4:]
            path = Path(path_text).expanduser()
            try:
                resolved = path.resolve()
            except OSError:
                continue
            try:
                resolved.relative_to(sessions_root)
            except ValueError:
                # Preserve Token Pulse's existing sessions-only data boundary.
                continue
            if session_paths is not None and session_id:
                session_paths.setdefault(str(session_id).lower(), resolved)
            try:
                stat = resolved.stat()
            except OSError:
                continue
            modified_ms = int(stat.st_mtime * 1000)
            if modified_ms < threshold_ms or resolved in seen:
                continue
            seen.add(resolved)
            candidates.append(resolved)
    return candidates, database_available


def iter_recent_jsonl(
    root: Path,
    start: datetime,
    *,
    session_paths: dict[str, Path] | None = None,
) -> list[Path]:
    if not root.exists() and not root.parent.exists():
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
        if session_paths is not None:
            match = CODEX_SESSION_FILE_ID_RE.search(resolved.name)
            if match is not None:
                session_key = match.group("id").lower()
                preferred = session_paths.get(session_key)
                if preferred is not None and preferred != resolved:
                    if preferred.exists():
                        return
                    session_paths[session_key] = resolved
        seen.add(resolved)
        paths.append(resolved)

    indexed_paths, database_available = codex_state_rollout_paths(
        root,
        start,
        session_paths,
    )
    for path in indexed_paths:
        add_path(path)

    # New rollouts can appear before state_5.sqlite commits the corresponding row.
    today = datetime.now().date()
    current_day = start.date()
    days_checked = 0
    while current_day <= today and days_checked < 62:
        day_dir = (
            root
            / f"{current_day.year:04d}"
            / f"{current_day.month:02d}"
            / f"{current_day.day:02d}"
        )
        if day_dir.exists():
            for path in day_dir.glob("*.jsonl"):
                add_path(path)
        current_day += timedelta(days=1)
        days_checked += 1

    if not database_available and root.exists():
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

    # Codex moves completed rollouts out of sessions. Scan recent archives
    # after live paths so an existing sessions copy remains authoritative.
    archived_root = root.parent / "archived_sessions"
    if archived_root.exists():
        threshold = start - timedelta(hours=2)
        try:
            archived_paths = sorted(
                archived_root.rglob("*.jsonl"),
                key=lambda path: str(path).lower(),
            )
        except OSError:
            archived_paths = []
        for path in archived_paths:
            try:
                modified = datetime.fromtimestamp(path.stat().st_mtime)
            except OSError:
                continue
            if modified >= threshold:
                add_path(path)
    # Completed child tasks can be removed from both sessions and archives.
    # Their compact token rows remain authoritative and must stay in today's
    # totals instead of disappearing with the rollout file.
    for path in _CODEX_EVENT_ROW_CACHE.cached_missing_paths(root, start):
        add_path(path)
    return paths


def codex_session_headers(paths: list[Path]) -> dict[Path, tuple[str, str]]:
    headers: dict[Path, tuple[str, str]] = {}
    for path in paths:
        headers[path] = codex_session_header_from_rows(
            codex_relevant_rows_from_path(path)
        )
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
        rows = codex_relevant_rows_from_path(path)
        opencodex_conversation_id = codex_opencodex_conversation_id_from_rows(rows)
        session_id, parent_id = headers.get(path) or codex_session_header_from_rows(rows)
        parent_signatures: list[tuple[int, ...]] = []
        if parent_id:
            parent_signatures = signature_cache.get(parent_id.lower(), [])
            if not parent_signatures:
                parent_path = session_paths.get(parent_id.lower())
                if parent_path is not None:
                    parent_signatures = codex_token_count_signatures_from_path(parent_path)
                    signature_cache[parent_id.lower()] = parent_signatures
        session_signatures = codex_token_count_signatures_for_rows(rows)
        signature_index = 0
        parent_prefix_index = 0
        parent_prefix_open = bool(parent_signatures)
        fork_replay_cutoff = codex_fork_replay_cutoff_from_rows(rows)
        session_key = session_id or str(path)
        active_turn_id = ""
        active_turn_started_at: datetime | None = None
        for row in rows:
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
                lifecycle_at = event_ts
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
            ts = event_ts
            quota_fingerprints = codex_quota_fingerprints(payload.get("rate_limits"))
            if signature_index < len(session_signatures):
                signature = session_signatures[signature_index]
            else:
                signature = codex_token_count_signature(payload)
            signature_index += 1
            # signature already holds every CODEX_TOKEN_USAGE_FIELDS value for
            # total_token_usage (1..5) and last_token_usage (6..10).
            reasoning_total = signature[4]
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
                "input_tokens": signature[1],
                "cached_input_tokens": signature[2],
                "output_tokens": signature[3],
            }
            key = (
                signature[1],
                signature[2],
                signature[3],
                reasoning_total,
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
                signature[1],
                signature[2],
                signature[3],
                reasoning_total,
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
            if signature[0]:
                input_tokens = signature[6]
                cached_tokens = signature[7]
                output_tokens = signature[8]
                event_key = (
                    str(row.get("timestamp") or ""),
                    model,
                    input_tokens,
                    cached_tokens,
                    output_tokens,
                    signature[9],
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
                        conversation_id=opencodex_conversation_id,
                        request_key=session_id,
                        account_at=active_turn_started_at,
                        quota_fingerprints=quota_fingerprints,
                        usage_provenance="codex-rollout",
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
                reasoning_total,
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
                    conversation_id=opencodex_conversation_id,
                    request_key=session_id,
                    account_at=active_turn_started_at,
                    quota_fingerprints=quota_fingerprints,
                    usage_provenance="codex-rollout",
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
            usage_provenance="codex-logs2",
        )
        if event is None:
            continue
        if response_id:
            seen_response_ids.add(response_id)
        events.append(event)
    return events


def usage_event_strong_identity_keys(event: UsageEvent) -> set[str]:
    return {
        str(value or "").strip().casefold()
        for value in (
            event.canonical_id,
            event.request_key,
            event.source_request_key,
        )
        if str(value or "").strip()
    }


def usage_event_physical_identity_keys(event: UsageEvent) -> set[str]:
    identities = {
        str(value or "").strip().casefold()
        for value in (event.canonical_id, event.source_request_key)
        if str(value or "").strip()
    }
    request_key = str(event.request_key or "").strip().casefold()
    session_key = str(event.session_id or "").strip().casefold()
    conversation_key = str(event.conversation_id or "").strip().casefold()
    if request_key and request_key not in {session_key, conversation_key}:
        identities.add(request_key)
    return identities


def usage_event_weak_identity_keys(event: UsageEvent) -> set[str]:
    identities = {
        str(value or "").strip().casefold()
        for value in (event.conversation_id, event.session_id)
        if str(value or "").strip()
    }
    session_id = str(event.session_id or "").strip()
    if session_id:
        identities.add(hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32])
    return identities


def usage_events_share_identity(first: UsageEvent, second: UsageEvent) -> bool:
    first_strong = usage_event_strong_identity_keys(first)
    second_strong = usage_event_strong_identity_keys(second)
    if first_strong and second_strong:
        return bool(first_strong.intersection(second_strong))
    return bool(
        usage_event_weak_identity_keys(first).intersection(
            usage_event_weak_identity_keys(second)
        )
    )


def usage_events_are_cross_source_aliases(first: UsageEvent, second: UsageEvent) -> bool:
    """Bridge a rollout/logs_2 copy without collapsing real keyed concurrency."""
    if opencodex_event_signature(first) != opencodex_event_signature(second):
        return False
    if abs((first.when - second.when).total_seconds()) > 1.0:
        return False
    provenances = {
        str(first.usage_provenance or "").strip().casefold(),
        str(second.usage_provenance or "").strip().casefold(),
    }
    if provenances != {"codex-rollout", "codex-logs2"}:
        return False
    first_weak = usage_event_weak_identity_keys(first)
    second_weak = usage_event_weak_identity_keys(second)
    if not first_weak.intersection(second_weak):
        return False
    first_strong = usage_event_strong_identity_keys(first)
    second_strong = usage_event_strong_identity_keys(second)
    if first_strong.intersection(second_strong):
        return True
    first_key = str(first.request_key or "").strip()
    second_key = str(second.request_key or "").strip()
    first_session = str(first.session_id or "").strip()
    second_session = str(second.session_id or "").strip()
    # Rollout token_count rows usually carry only the session identity while
    # logs_2 carries the response/request id. Two distinct non-session request
    # ids remain separate concurrent requests.
    return bool(
        (first_key and first_key == first_session and second_key != second_session)
        or (second_key and second_key == second_session and first_key != first_session)
    )


def dedupe_usage_events(events: list[UsageEvent]) -> list[UsageEvent]:
    seen: dict[tuple[str, int, int, int], list[int]] = {}
    result: list[UsageEvent] = []
    cross_source_matched_indexes: set[int] = set()
    for event in sorted(events, key=lambda item: item.when):
        event_second = int(event.when.replace(tzinfo=LOCAL_TZ).timestamp())
        key = (
            event.model,
            event.input_tokens,
            event.cached_tokens,
            event.output_tokens,
        )
        candidate_indexes = seen.get(key, [])
        alias_indexes = [
            existing_idx
            for existing_idx in candidate_indexes
            if existing_idx not in cross_source_matched_indexes
            and usage_events_are_cross_source_aliases(event, result[existing_idx])
        ]
        identity_indexes = [
            existing_idx
            for existing_idx in candidate_indexes
            if (
                int(
                    result[existing_idx].when.replace(tzinfo=LOCAL_TZ).timestamp()
                )
                == event_second
                and (
                    bool(
                        usage_event_physical_identity_keys(event).intersection(
                            usage_event_physical_identity_keys(result[existing_idx])
                        )
                    )
                    or (
                        event.when == result[existing_idx].when
                        and usage_events_share_identity(event, result[existing_idx])
                    )
                )
            )
        ]
        duplicate_indexes = alias_indexes or identity_indexes
        if duplicate_indexes:
            existing_idx = max(
                duplicate_indexes,
                key=lambda index: usage_event_info_score(result[index]),
            )
            existing = result[existing_idx]
            existing_score = usage_event_info_score(existing)
            event_score = usage_event_info_score(event)
            provenances = {
                str(existing.usage_provenance or "").strip().casefold(),
                str(event.usage_provenance or "").strip().casefold(),
            }
            if provenances == {"codex-rollout", "codex-logs2"}:
                cross_source_matched_indexes.add(existing_idx)
                if str(event.usage_provenance or "").strip().casefold() == "codex-logs2":
                    result[existing_idx] = event
            elif event_score > existing_score:
                result[existing_idx] = event
            continue
        seen.setdefault(key, []).append(len(result))
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


_LEDGER_DIRTY = False
_LEDGER_WRITES: set[str] = set()
_VERDICT_ARCHIVE_DIRTY = False
# Event ids this run decided from live evidence. They win over the copy on disk
# when the archive is merged at save time; every other key keeps whatever a
# concurrent exporter wrote, so parallel runs cannot drop each other's verdicts.
_VERDICT_ARCHIVE_WRITES: set[str] = set()


def ledger_assign(ledger: dict[str, str], event_id: str, label: str) -> None:
    global _LEDGER_DIRTY
    if ledger.get(event_id) != label:
        ledger[event_id] = label
        _LEDGER_DIRTY = True
        _LEDGER_WRITES.add(event_id)


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
        ledger_assign(ledger, stable_id, label)
    return label, stable_id


def usage_event_attribution_time(event: UsageEvent) -> datetime:
    return event.request_at or event.when


def usage_event_account_time(event: UsageEvent) -> datetime:
    return event.account_at or usage_event_attribution_time(event)


def usage_event_info_score(event: UsageEvent) -> int:
    score = 0
    if event.quota_fingerprints:
        score += 16
    if event.route:
        score += 8
    if event.session_id:
        score += 4
    if event.conversation_id:
        score += 2
    if event.request_key:
        score += 2
    if event.cost_multiplier is not None:
        score += 1
    if event.request_at is not None:
        score += 1
    if event.account_at is not None:
        score += 1
    return score


_LAST_OPENCODEX_RECONCILIATION_DIAGNOSTICS: dict[str, dict[str, int]] = {}


def last_opencodex_reconciliation_diagnostics() -> dict[str, dict[str, int]]:
    return {
        key: dict(value)
        for key, value in _LAST_OPENCODEX_RECONCILIATION_DIAGNOSTICS.items()
    }


def scan_all_codex_events(
    home: Path,
    sessions_root: Path,
    start: datetime,
    end: datetime,
    session_lifecycle: dict[str, SessionLifecycle] | None = None,
    *,
    failure_events: list[CodexFailureEvent] | None = None,
) -> list[UsageEvent]:
    global _LAST_OPENCODEX_RECONCILIATION_DIAGNOSTICS
    reconciliation_padding = timedelta(
        seconds=OPENCODEX_RECONCILIATION_MATCH_WINDOW_SECONDS
    )
    # Local token_count rows are written at or after completion. Scan forward
    # for retrospective windows, while padding OpenCodex in both directions so
    # a late local row just after midnight can suppress the prior day's proxy
    # request without re-reading six hours of unrelated local rollouts.
    local_scan_start = start
    local_scan_end = end + reconciliation_padding
    marker_scan_start = start - reconciliation_padding
    marker_scan_end = end + reconciliation_padding
    padded_failures: list[CodexFailureEvent] | None = (
        [] if failure_events is not None else None
    )
    events = scan_codex_events(
        sessions_root,
        local_scan_start,
        local_scan_end,
        session_lifecycle=session_lifecycle,
        failure_events=padded_failures,
    )
    events.extend(scan_codex_logs2_events(home, local_scan_start, local_scan_end))
    route_markers = scan_codex_route_markers(home, local_scan_start, local_scan_end)
    apply_codex_route_hints(events, route_markers)
    events = dedupe_usage_events(events)
    opencodex_markers = scan_opencodex_usage_markers(
        home,
        marker_scan_start,
        marker_scan_end,
    )
    reconciled = reconcile_opencodex_usage_events(
        events,
        opencodex_markers,
        start,
        end,
    )
    apply_opencodex_account_hints(
        home,
        reconciled.events,
        start,
        end,
        markers=opencodex_markers,
    )
    _LAST_OPENCODEX_RECONCILIATION_DIAGNOSTICS = (
        reconciled.diagnostics.as_dict()
    )
    if failure_events is not None and padded_failures is not None:
        failure_events.extend(
            failure
            for failure in padded_failures
            if start <= failure.when < end
        )
    return reconciled.events


def local_codex_window_source_available(
    home: Path,
    sessions_root: Path,
    start: datetime,
    end: datetime,
) -> bool:
    """Tell an empty local scan apart from a missing local source.

    A readable recent session JSONL or a readable ``logs_2.sqlite`` row in the
    requested interval means the local event ledger covered this window even
    when it produced no eligible usage rows. That explicit zero must not be
    replaced with an older Cockpit total.
    """
    # The exporter always scans this canonical root.  Do not let an arbitrary
    # test or diagnostics directory containing a JSONL turn into a false
    # "covered local ledger" signal.
    try:
        canonical_sessions_root = (home / ".codex" / "sessions").resolve()
        if sessions_root.resolve() != canonical_sessions_root:
            return False
    except OSError:
        return False
    try:
        if iter_recent_jsonl(sessions_root, start):
            return True
    except OSError:
        pass
    db_path = home / ".codex" / "logs_2.sqlite"
    if not db_path.is_file():
        return False
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        start_epoch = int(start.replace(tzinfo=LOCAL_TZ).timestamp()) - 300
        end_epoch = int(end.replace(tzinfo=LOCAL_TZ).timestamp()) + 300
        return connection.execute(
            "SELECT 1 FROM logs WHERE ts >= ? AND ts < ? LIMIT 1",
            (start_epoch, end_epoch),
        ).fetchone() is not None
    except sqlite3.Error:
        return False
    finally:
        if connection is not None:
            connection.close()


def bucket_from_codex_events(events: list[UsageEvent]) -> UsageBucket:
    bucket = UsageBucket()
    for event in events:
        add_codex_event_to_bucket(bucket, event)
    return bucket




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


def cockpit_account_label_quality(value: Any) -> int:
    identity = str(value or "").strip()
    prefix = "Codex local - "
    if identity.casefold().startswith(prefix.casefold()):
        identity = identity[len(prefix):].strip()
    lowered = identity.casefold()
    if not identity or lowered in {"unknown", "api-service-local"}:
        return 0
    if "@" in identity:
        return 3
    normalized = lowered[:-5] if lowered.endswith(".json") else lowered
    if lowered.endswith(".json") or re.fullmatch(r"codex_[0-9a-f]{24,}", normalized):
        return 1
    return 2


def prefer_cockpit_account_label(primary: Any, candidate: Any) -> str:
    primary_label = str(primary or "").strip()
    candidate_label = str(candidate or "").strip()
    if not primary_label:
        return candidate_label
    if not candidate_label:
        return primary_label
    if cockpit_account_label_quality(primary_label) >= cockpit_account_label_quality(candidate_label):
        return primary_label
    return candidate_label


def cockpit_account_label_with_manifest(
    account_id: Any,
    email: Any,
    api_key_label: Any,
    labels_by_id: dict[str, str],
) -> str:
    fallback = cockpit_account_label(
        str(account_id or ""),
        str(email or ""),
        str(api_key_label or ""),
    )
    normalized = normalize_cockpit_auth_id(account_id)
    known = labels_by_id.get(normalized) or labels_by_id.get(str(account_id or "").strip())
    return prefer_cockpit_account_label(known, fallback)


def usable_cockpit_identity(value: Any) -> str:
    """Accept compact auth metadata, but reject diagnostic routing text."""
    identity = str(value or "").strip()
    if not identity:
        return ""
    if identity.lower() in API_SERVICE_MIRROR_LABELS:
        return identity
    if any(char.isspace() or char in ":;{}[]" for char in identity):
        return ""
    return identity


def usable_cockpit_account_label(value: Any) -> str:
    label = str(value or "").strip()
    prefix = "Codex local - "
    identity = label[len(prefix):].strip() if label.lower().startswith(prefix.lower()) else label
    return label if usable_cockpit_identity(identity) else ""


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

    provider_identity = str(
        data.get("api_provider_name")
        or data.get("api_provider_id")
        or ""
    ).strip()
    # A routing diagnostic is not an account identity.  Only compact metadata
    # values may seed the local account timeline; free-form log text must stay
    # unresolved instead of becoming a guessed account or plan type.
    return usable_cockpit_identity(provider_identity)


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
        if when is None or not usable_cockpit_account_label(label):
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
        if when is None or not usable_cockpit_account_label(label):
            continue
        markers.append(AccountMarker(when=when, label=label, model=CODEX_DEFAULT_MODEL, kind="switch"))
    deduped: dict[tuple[datetime, str], AccountMarker] = {
        (marker.when, marker.label): marker
        for marker in markers
    }
    markers = list(deduped.values())
    markers.sort(key=lambda marker: marker.when)
    return markers


def record_account_timeline_snapshot(
    label: str,
    changed_at: datetime | None,
    now: datetime,
) -> None:
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


def record_current_account_snapshot(home: Path, now: datetime) -> None:
    label, _source_path, changed_at = current_codex_account_snapshot(home)
    record_account_timeline_snapshot(label, changed_at, now)


def opencodex_root(home: Path) -> Path:
    configured = os.environ.get("CLIENT_USAGE_OPENCODEX_DIR", "").strip()
    return Path(configured).expanduser() if configured else home / ".opencodex"


def opencodex_source_instance(home: Path) -> str:
    try:
        source = os.path.normcase(str(opencodex_root(home).resolve(strict=False)))
    except OSError:
        source = os.path.normcase(str(opencodex_root(home)))
    return hashlib.sha256(source.encode("utf-8", errors="ignore")).hexdigest()[:12]


def opencodex_current_account_snapshot(home: Path) -> OpenCodexAccountSnapshot | None:
    # OpenCodex's "main" route executes with the credential installed in
    # ~/.codex/auth.json.  ~/.opencodex/auth.json describes OpenCodex-managed
    # accounts and is not evidence of which credential completed the request.
    path = home / ".codex" / "auth.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except (OSError, json.JSONDecodeError):
        return None
    identity = codex_auth_identity(data)
    changed_at = file_mtime_local(path)
    if not identity or changed_at is None:
        return None
    label = f"Codex local - {identity}"
    if is_api_service_mirror_label(label):
        return None
    return OpenCodexAccountSnapshot(
        when=changed_at,
        label=label,
    )




def record_current_opencodex_account_snapshot(home: Path, now: datetime) -> None:
    record_opencodex_log_label_accounts(home, now)
    current = opencodex_current_account_snapshot(home)
    if current is not None:
        record_account_timeline_snapshot(current.label, current.when, now)


def opencodex_account_snapshots(
    home: Path,
    now: datetime,
) -> list[OpenCodexAccountSnapshot]:
    snapshots = [
        OpenCodexAccountSnapshot(when=marker.when, label=marker.label)
        for marker in load_account_timeline()
        if marker.label
        and marker.label != "Codex local"
        and not is_api_service_mirror_label(marker.label)
    ]
    current = opencodex_current_account_snapshot(home)
    if current is not None:
        current_when = min(current.when, now + timedelta(minutes=5))
        if not snapshots or snapshots[-1].label != current.label:
            snapshots.append(replace(current, when=current_when))
    snapshots.sort(key=lambda item: item.when)
    return snapshots


def opencodex_account_label_at(
    when: datetime,
    snapshots: list[OpenCodexAccountSnapshot],
) -> str:
    if not snapshots:
        return ""
    times = [item.when for item in snapshots]
    position = bisect_right(times, when) - 1
    return snapshots[position].label if position >= 0 else ""


def discover_opencodex_log_label_accounts(
    home: Path,
) -> dict[str, tuple[str, str]]:
    root = opencodex_root(home)
    try:
        paths = sorted(
            root.glob("config.json*"),
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        )
    except OSError:
        paths = []
    candidates: dict[str, dict[str, tuple[int, str]]] = {}
    for path in paths:
        try:
            modified_ns = path.stat().st_mtime_ns
            data = json.loads(path.read_text(encoding="utf-8-sig", errors="ignore"))
        except (OSError, json.JSONDecodeError):
            continue
        accounts = data.get("codexAccounts") if isinstance(data, dict) else None
        if not isinstance(accounts, list):
            continue
        for account in accounts:
            if not isinstance(account, dict):
                continue
            log_label = str(account.get("logLabel") or "").strip().casefold()
            email = str(account.get("email") or "").strip()
            if not log_label or log_label == "main" or not usable_cockpit_identity(email):
                continue
            label = cockpit_account_label("", email, "")
            plan_type = str(
                account.get("planType") or account.get("plan") or ""
            ).strip().lower()
            by_account = candidates.setdefault(log_label, {})
            previous = by_account.get(label)
            if previous is None or modified_ns > previous[0]:
                by_account[label] = (modified_ns, plan_type)
    result: dict[str, tuple[str, str]] = {}
    for log_label, by_account in candidates.items():
        if len(by_account) != 1:
            continue
        label, (_modified_ns, plan_type) = next(iter(by_account.items()))
        result[log_label] = (label, plan_type)
    return result


def load_opencodex_persisted_log_label_accounts(
    path: Path | None = None,
) -> dict[str, tuple[str, str]]:
    data = load_json_object(path or OPENCODEX_ACCOUNT_MAP_PATH)
    accounts = data.get("accounts")
    if not isinstance(accounts, dict):
        return {}
    result: dict[str, tuple[str, str]] = {}
    for raw_log_label, item in accounts.items():
        log_label = str(raw_log_label or "").strip().casefold()
        if not re.fullmatch(r"p[0-9a-f]{6}", log_label) or not isinstance(item, dict):
            continue
        label = usable_cockpit_account_label(item.get("label"))
        if not label or is_api_service_mirror_label(label):
            continue
        result[log_label] = (
            label,
            str(item.get("plan_type") or "").strip().lower(),
        )
    return result


def opencodex_log_label_accounts(home: Path) -> dict[str, tuple[str, str]]:
    discovered = discover_opencodex_log_label_accounts(home)
    try:
        use_persisted = (
            home.resolve() == Path.home().resolve()
            or bool(os.environ.get("CLIENT_USAGE_OPENCODEX_ACCOUNT_MAP"))
        )
    except OSError:
        use_persisted = False
    if not use_persisted:
        return discovered
    persisted = load_opencodex_persisted_log_label_accounts()
    for log_label, account in persisted.items():
        current = discovered.get(log_label)
        if current is None:
            discovered[log_label] = account
        elif current[0] != account[0]:
            discovered.pop(log_label, None)
    return discovered


def record_opencodex_log_label_accounts(home: Path, now: datetime) -> None:
    try:
        is_real_home = (
            home.resolve() == Path.home().resolve()
            or bool(os.environ.get("CLIENT_USAGE_OPENCODEX_ACCOUNT_MAP"))
        )
    except OSError:
        is_real_home = False
    if not is_real_home:
        return
    discovered = discover_opencodex_log_label_accounts(home)
    if not discovered:
        return
    path = OPENCODEX_ACCOUNT_MAP_PATH
    try:
        with attribution_ledger_write_lock(path):
            persisted = load_opencodex_persisted_log_label_accounts(path)
            for log_label, account in discovered.items():
                previous = persisted.get(log_label)
                if previous is not None and previous[0] != account[0]:
                    persisted.pop(log_label, None)
                    continue
                persisted[log_label] = account
            write_json_atomic(
                path,
                {
                    "schema": 1,
                    "updated_at": now.replace(tzinfo=LOCAL_TZ).isoformat(timespec="seconds"),
                    "accounts": {
                        log_label: {
                            "label": account[0],
                            "plan_type": account[1],
                        }
                        for log_label, account in sorted(persisted.items())
                    },
                },
            )
    except (OSError, TimeoutError) as exc:
        logger.warning("OpenCodex account map save deferred: %s", exc)


def opencodex_anonymous_account_label(account_log_label: str) -> str:
    normalized = str(account_log_label or "").strip().casefold()
    if not re.fullmatch(r"p[0-9a-f]{6}", normalized):
        return ""
    return f"Codex local - OpenCodex-{normalized}"


def opencodex_usage_rows(home: Path) -> list[dict[str, Any]]:
    path = opencodex_root(home) / "usage.jsonl"
    if not path.exists():
        return []
    try:
        real_home = home.resolve() == Path.home().resolve()
    except OSError:
        real_home = False
    if real_home or os.environ.get("CLIENT_USAGE_OPENCODEX_DIR"):
        return _OPENCODEX_USAGE_ROW_CACHE.rows_for_path(path)
    try:
        size = path.stat().st_size
    except OSError:
        return []
    rows, _processed = OpenCodexUsageRowCache._read_complete_rows(path, 0, size)
    return rows


def scan_opencodex_usage_markers(
    home: Path,
    start: datetime,
    end: datetime,
    *,
    rows: list[dict[str, Any]] | None = None,
    snapshots: list[OpenCodexAccountSnapshot] | None = None,
    log_label_accounts: dict[str, tuple[str, str]] | None = None,
) -> list[OpenCodexUsageMarker]:
    source_rows = opencodex_usage_rows(home) if rows is None else rows
    account_snapshots = (
        opencodex_account_snapshots(home, end) if snapshots is None else snapshots
    )
    named_accounts = (
        opencodex_log_label_accounts(home)
        if log_label_accounts is None
        else log_label_accounts
    )
    source_instance = opencodex_source_instance(home)
    lower = start - timedelta(seconds=OPENCODEX_ACCOUNT_MATCH_WINDOW_SECONDS)
    upper = end + timedelta(seconds=OPENCODEX_ACCOUNT_MATCH_WINDOW_SECONDS)
    markers: list[OpenCodexUsageMarker] = []
    seen_requests: set[tuple[Any, ...]] = set()
    for row in source_rows:
        request_at = ms_to_local_datetime(row.get("timestamp"))
        if request_at is None:
            continue
        try:
            duration_ms = max(0, min(86_400_000, int(row.get("duration_ms") or 0)))
            input_tokens = max(0, int(row.get("input_tokens") or 0))
            cached_tokens = max(0, int(row.get("cached_tokens") or 0))
            output_tokens = max(0, int(row.get("output_tokens") or 0))
            total_tokens = max(0, int(row.get("total_tokens") or 0))
        except (TypeError, ValueError, OverflowError):
            continue
        when = request_at + timedelta(milliseconds=duration_ms)
        if when < lower or when > upper or total_tokens <= 0:
            continue
        account_log_label = str(row.get("account_log_label") or "").strip()
        normalized_log_label = account_log_label.casefold()
        if normalized_log_label == "main":
            label = opencodex_account_label_at(request_at, account_snapshots)
        else:
            label = named_accounts.get(normalized_log_label, ("", ""))[0]
            if not label:
                label = opencodex_anonymous_account_label(normalized_log_label)
        request_id = str(row.get("request_id") or "").strip()
        try:
            attempt_ordinal = max(0, int(row.get("attempt_ordinal") or 0))
        except (TypeError, ValueError, OverflowError):
            attempt_ordinal = 0
        try:
            status = int(row.get("status") or 0)
        except (TypeError, ValueError, OverflowError):
            status = 0
        response_service_tier = str(row.get("response_service_tier") or "").strip()
        pricing_tier = normalize_pricing_tier(
            row.get("pricing_tier") or response_service_tier
        )
        app_speed = codex_service_tier_to_speed(response_service_tier)
        dedupe_key: tuple[Any, ...]
        if request_id:
            dedupe_key = ("request", request_id, attempt_ordinal)
        else:
            dedupe_key = (
                "usage",
                local_epoch_ms(request_at),
                str(row.get("model") or "").casefold(),
                input_tokens,
                cached_tokens,
                output_tokens,
                account_log_label.casefold(),
            )
        if dedupe_key in seen_requests:
            continue
        seen_requests.add(dedupe_key)
        markers.append(
            OpenCodexUsageMarker(
                request_at=request_at,
                when=when,
                model=str(row.get("model") or "").strip(),
                input_tokens=input_tokens,
                cached_tokens=cached_tokens,
                output_tokens=output_tokens,
                total_tokens=total_tokens,
                label=label,
                account_log_label=account_log_label,
                request_id=request_id,
                conversation_id=str(row.get("conversation_id") or "").strip(),
                attempt_ordinal=attempt_ordinal,
                provider=str(row.get("provider") or "").strip().lower(),
                requested_model=str(row.get("requested_model") or "").strip(),
                resolved_model=str(
                    row.get("resolved_model") or row.get("model") or ""
                ).strip(),
                response_service_tier=response_service_tier,
                requested_service_tier=str(
                    row.get("requested_service_tier") or ""
                ).strip(),
                requested_speed_label=str(
                    row.get("requested_speed_label") or ""
                ).strip(),
                pricing_tier=pricing_tier,
                pricing_model=str(
                    row.get("pricing_model")
                    or row.get("resolved_model")
                    or row.get("model")
                    or ""
                ).strip(),
                app_speed=app_speed,
                admission_kind=str(row.get("admission_kind") or "").strip(),
                inbound_protocol=str(row.get("inbound_protocol") or "").strip(),
                usage_status=str(row.get("usage_status") or "").strip(),
                status=status,
                route_kind=str(row.get("route_kind") or "").strip(),
                source_instance=source_instance,
                provenance=str(
                    row.get("provenance") or OPENCODEX_USAGE_PROVENANCE
                ).strip(),
            )
        )
    return markers


def opencodex_model_key(value: Any) -> str:
    model = str(value or "").strip().casefold()
    return model.removeprefix("openai/")


def opencodex_event_signature(event: UsageEvent) -> tuple[str, int, int, int, int]:
    return (
        opencodex_model_key(event.model),
        max(0, event.input_tokens),
        max(0, event.cached_tokens),
        max(0, event.output_tokens),
        event.total_tokens,
    )


def opencodex_marker_signature(
    marker: OpenCodexUsageMarker,
) -> tuple[str, int, int, int, int]:
    return (
        opencodex_model_key(marker.model),
        marker.input_tokens,
        marker.cached_tokens,
        marker.output_tokens,
        marker.total_tokens,
    )


def opencodex_conversation_id_for_event(event: UsageEvent) -> str:
    explicit = str(event.conversation_id or "").strip().casefold()
    if explicit:
        return explicit
    session_id = str(event.session_id or "").strip()
    if not session_id:
        return ""
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]


def opencodex_marker_request_key(marker: OpenCodexUsageMarker) -> str:
    instance = str(marker.source_instance or "default").strip().casefold() or "default"
    request_id = str(marker.request_id or "").strip()
    if request_id:
        return f"opencodex:{instance}:{request_id}:{max(0, marker.attempt_ordinal)}"
    parts = (
        str(local_epoch_ms(marker.request_at)),
        str(local_epoch_ms(marker.when)),
        marker.conversation_id.strip().casefold(),
        *[str(value) for value in opencodex_marker_signature(marker)],
        marker.account_log_label.strip().casefold(),
    )
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:24]
    return f"opencodex:{instance}:usage:{digest}"


def opencodex_marker_route_evidence(marker: OpenCodexUsageMarker) -> bool:
    return bool(
        marker.request_id
        or marker.account_log_label
        or marker.route_kind
        or marker.provider.startswith("openai")
    )


def opencodex_proxy_rejection_reason(marker: OpenCodexUsageMarker) -> str:
    protocol = marker.inbound_protocol.strip().casefold()
    if protocol and protocol != "responses":
        return "unsupported_inbound_protocol"
    conversation_id = marker.conversation_id.strip()
    route_kind = marker.route_kind.strip().casefold()
    admission_kind = marker.admission_kind.strip().casefold()
    if not conversation_id and (
        "probe" in admission_kind
        or route_kind in {"model-probe", "models", "probe", "health-check"}
    ):
        return "conversationless_probe"
    return ""


def opencodex_canonical_usage_event(
    marker: OpenCodexUsageMarker,
    local_event: UsageEvent | None = None,
    *,
    status: str,
) -> UsageEvent:
    canonical_key = opencodex_marker_request_key(marker)
    model = marker.resolved_model or marker.model
    pricing_tier = marker.pricing_tier or normalize_pricing_tier(
        marker.response_service_tier
    )
    app_speed = marker.app_speed or codex_service_tier_to_speed(
        marker.response_service_tier
    )
    account_label = marker.label or API_SERVICE_AGGREGATE_LABEL
    account_source = (
        OPENCODEX_ACCOUNT_HINT_SOURCE
        if marker.label
        else OPENCODEX_UNRESOLVED_HINT_SOURCE
    )
    if local_event is None:
        conversation_id = marker.conversation_id.strip().casefold()
        return UsageEvent(
            when=marker.when,
            model=codex_model_name(model),
            input_tokens=max(0, marker.input_tokens),
            cached_tokens=max(0, marker.cached_tokens),
            output_tokens=max(0, marker.output_tokens),
            app_speed=normalize_codex_speed(app_speed),
            cost_multiplier=codex_speed_cost_multiplier(app_speed),
            pricing_tier=pricing_tier,
            session_id=f"opencodex:{conversation_id}" if conversation_id else "",
            conversation_id=conversation_id,
            request_key=canonical_key,
            route="opencodex",
            request_at=marker.request_at,
            account_at=marker.request_at,
            account_label_hint=account_label,
            account_hint_source=account_source,
            pricing_model=marker.pricing_model or model,
            usage_provenance=marker.provenance or OPENCODEX_USAGE_PROVENANCE,
            reconciliation_status=status,
            canonical_id=canonical_key,
        )

    superseded_event_id = live_usage_event_id(local_event)
    event = replace(local_event)
    event.when = marker.when
    event.model = codex_model_name(model)
    event.input_tokens = max(0, marker.input_tokens)
    event.cached_tokens = max(0, marker.cached_tokens)
    event.output_tokens = max(0, marker.output_tokens)
    event.app_speed = normalize_codex_speed(app_speed)
    event.cost_multiplier = codex_speed_cost_multiplier(app_speed)
    event.pricing_tier = pricing_tier
    event.source_request_key = local_event.source_request_key or local_event.request_key
    event.request_key = canonical_key
    event.route = local_event.route or "opencodex"
    event.request_at = marker.request_at
    if not event.conversation_id:
        event.conversation_id = marker.conversation_id.strip().casefold()
    event.account_label_hint = account_label
    event.account_hint_source = account_source
    event.pricing_model = marker.pricing_model or model
    event.usage_provenance = marker.provenance or OPENCODEX_USAGE_PROVENANCE
    event.reconciliation_status = status
    event.canonical_id = canonical_key
    event.supersedes_event_ids = tuple(
        dict.fromkeys(
            (
                *local_event.supersedes_event_ids,
                *(
                    (superseded_event_id,)
                    if superseded_event_id and superseded_event_id != canonical_key
                    else ()
                ),
            )
        )
    )
    return event


def _opencodex_pair_candidates(
    local_events: list[UsageEvent],
    markers: list[OpenCodexUsageMarker],
    local_indexes: set[int],
    marker_indexes: set[int],
    max_seconds: float,
    *,
    require_conversation: bool,
) -> tuple[list[tuple[float, datetime, int, int]], set[int], set[int]]:
    pairs: list[tuple[float, datetime, int, int]] = []
    by_local: dict[int, list[tuple[float, int]]] = {}
    by_marker: dict[int, list[tuple[float, int]]] = {}
    local_by_signature: dict[tuple[str, int, int, int, int], list[int]] = {}
    for local_index in local_indexes:
        event = local_events[local_index]
        if external_codex_provider_label(event.model) or event.route == "official":
            continue
        local_by_signature.setdefault(opencodex_event_signature(event), []).append(
            local_index
        )
    for marker_index in marker_indexes:
        marker = markers[marker_index]
        for local_index in local_by_signature.get(
            opencodex_marker_signature(marker), []
        ):
            event = local_events[local_index]
            event_conversation = opencodex_conversation_id_for_event(event)
            marker_conversation = marker.conversation_id.strip().casefold()
            if require_conversation:
                if not event_conversation or event_conversation != marker_conversation:
                    continue
            else:
                if event_conversation and marker_conversation:
                    continue
                if event.route == "official" or not opencodex_marker_route_evidence(marker):
                    continue
            completion_delay = (event.when - marker.when).total_seconds()
            allowed_marker_lag = min(
                OPENCODEX_RECONCILIATION_CLOCK_SKEW_SECONDS,
                max_seconds,
            )
            if completion_delay < -allowed_marker_lag or completion_delay > max_seconds:
                continue
            distance = abs(completion_delay)
            pairs.append((distance, event.when, local_index, marker_index))
            by_local.setdefault(local_index, []).append((distance, marker_index))
            by_marker.setdefault(marker_index, []).append((distance, local_index))

    ambiguous_local: set[int] = set()
    ambiguous_marker: set[int] = set()
    for local_index, candidates in by_local.items():
        candidates.sort(key=lambda item: (item[0], item[1]))
        if (
            len(candidates) > 1
            and abs(candidates[1][0] - candidates[0][0])
            <= OPENCODEX_ACCOUNT_MATCH_AMBIGUITY_SECONDS
        ):
            ambiguous_local.add(local_index)
    for marker_index, candidates in by_marker.items():
        candidates.sort(key=lambda item: (item[0], item[1]))
        if (
            len(candidates) > 1
            and abs(candidates[1][0] - candidates[0][0])
            <= OPENCODEX_ACCOUNT_MATCH_AMBIGUITY_SECONDS
        ):
            ambiguous_marker.add(marker_index)
    return pairs, ambiguous_local, ambiguous_marker


def reconcile_opencodex_usage_events(
    local_events: list[UsageEvent],
    markers: list[OpenCodexUsageMarker],
    start: datetime,
    end: datetime,
) -> OpenCodexReconciliationResult:
    diagnostics = OpenCodexReconciliationDiagnostics()
    if not markers:
        events = [event for event in local_events if start <= event.when < end]
        for event in events:
            event.reconciliation_status = event.reconciliation_status or "local_only"
            diagnostics.add("local_only", event.total_tokens)
        return OpenCodexReconciliationResult(events=events, diagnostics=diagnostics)

    unique_markers: list[OpenCodexUsageMarker] = []
    marker_position_by_key: dict[str, int] = {}
    conflict_keys: set[str] = set()
    for marker in sorted(markers, key=lambda item: (item.when, item.request_at)):
        canonical_key = opencodex_marker_request_key(marker)
        previous_position = marker_position_by_key.get(canonical_key)
        if previous_position is None:
            marker_position_by_key[canonical_key] = len(unique_markers)
            unique_markers.append(marker)
            continue
        previous = unique_markers[previous_position]
        if opencodex_marker_signature(previous) != opencodex_marker_signature(marker):
            conflict_keys.add(canonical_key)
        if (marker.when, marker.request_at) >= (previous.when, previous.request_at):
            unique_markers[previous_position] = marker
    markers = unique_markers

    available_local = {
        index
        for index, event in enumerate(local_events)
        if not external_codex_provider_label(event.model)
    }
    available_markers = set(range(len(markers)))
    matched_pairs: list[tuple[int, int, str]] = []

    local_by_request: dict[str, list[int]] = {}
    for local_index in available_local:
        event = local_events[local_index]
        for value in (event.request_key, event.source_request_key):
            request_key = str(value or "").strip().casefold()
            if request_key:
                local_by_request.setdefault(request_key, []).append(local_index)
    for marker_index, marker in enumerate(markers):
        request_id = marker.request_id.strip().casefold()
        if not request_id:
            continue
        candidates = [
            local_index
            for local_index in local_by_request.get(request_id, [])
            if local_index in available_local
            and local_events[local_index].route != "official"
            and -OPENCODEX_RECONCILIATION_CLOCK_SKEW_SECONDS
            <= (local_events[local_index].when - marker.when).total_seconds()
            <= OPENCODEX_RECONCILIATION_MATCH_WINDOW_SECONDS
        ]
        if not candidates:
            continue
        marker_signature = opencodex_marker_signature(marker)
        candidates.sort(
            key=lambda local_index: (
                opencodex_event_signature(local_events[local_index]) != marker_signature,
                abs((local_events[local_index].when - marker.when).total_seconds()),
                local_events[local_index].when,
                local_index,
            )
        )
        local_index = candidates[0]
        canonical_key = opencodex_marker_request_key(marker)
        status = (
            "conflict"
            if canonical_key in conflict_keys
            or opencodex_event_signature(local_events[local_index]) != marker_signature
            else "matched"
        )
        matched_pairs.append((local_index, marker_index, status))
        available_local.remove(local_index)
        available_markers.remove(marker_index)

    pairs, ambiguous_local, ambiguous_marker = _opencodex_pair_candidates(
        local_events,
        markers,
        available_local,
        available_markers,
        OPENCODEX_RECONCILIATION_MATCH_WINDOW_SECONDS,
        require_conversation=True,
    )
    for _distance, _when, local_index, marker_index in sorted(pairs):
        if local_index not in available_local or marker_index not in available_markers:
            continue
        canonical_key = opencodex_marker_request_key(markers[marker_index])
        if canonical_key in conflict_keys:
            status = "conflict"
        elif local_index in ambiguous_local or marker_index in ambiguous_marker:
            status = "ambiguous"
        else:
            status = "matched"
        matched_pairs.append((local_index, marker_index, status))
        available_local.remove(local_index)
        available_markers.remove(marker_index)

    fallback_pairs, _ambiguous_local, _ambiguous_marker = _opencodex_pair_candidates(
        local_events,
        markers,
        available_local,
        available_markers,
        OPENCODEX_CROSS_SESSION_MATCH_WINDOW_SECONDS,
        require_conversation=False,
    )
    fallback_by_local: dict[int, list[int]] = {}
    fallback_by_marker: dict[int, list[int]] = {}
    for _distance, _when, local_index, marker_index in fallback_pairs:
        fallback_by_local.setdefault(local_index, []).append(marker_index)
        fallback_by_marker.setdefault(marker_index, []).append(local_index)
    for _distance, _when, local_index, marker_index in sorted(fallback_pairs):
        if local_index not in available_local or marker_index not in available_markers:
            continue
        if len(fallback_by_local.get(local_index, [])) != 1:
            continue
        if len(fallback_by_marker.get(marker_index, [])) != 1:
            continue
        canonical_key = opencodex_marker_request_key(markers[marker_index])
        status = "conflict" if canonical_key in conflict_keys else "matched"
        matched_pairs.append((local_index, marker_index, status))
        available_local.remove(local_index)
        available_markers.remove(marker_index)

    canonical_events: list[UsageEvent] = []
    for local_index, marker_index, status in matched_pairs:
        marker = markers[marker_index]
        event = opencodex_canonical_usage_event(
            marker,
            local_events[local_index],
            status=status,
        )
        if start <= event.when < end:
            canonical_events.append(event)
            diagnostics.add(status, event.total_tokens)
    for marker_index in sorted(available_markers):
        marker = markers[marker_index]
        if not (start <= marker.when < end):
            continue
        if opencodex_proxy_rejection_reason(marker):
            diagnostics.add("rejected", marker.total_tokens)
            continue
        canonical_key = opencodex_marker_request_key(marker)
        status = "conflict" if canonical_key in conflict_keys else "proxy_only"
        event = opencodex_canonical_usage_event(marker, status=status)
        canonical_events.append(event)
        diagnostics.add(status, event.total_tokens)
    for local_index in sorted(available_local):
        event = local_events[local_index]
        if not (start <= event.when < end):
            continue
        event.reconciliation_status = event.reconciliation_status or "local_only"
        canonical_events.append(event)
        diagnostics.add("local_only", event.total_tokens)

    external_indexes = {
        index
        for index, event in enumerate(local_events)
        if external_codex_provider_label(event.model)
    }
    for local_index in sorted(external_indexes):
        event = local_events[local_index]
        if not (start <= event.when < end):
            continue
        event.reconciliation_status = event.reconciliation_status or "local_only"
        canonical_events.append(event)
        diagnostics.add("local_only", event.total_tokens)
    canonical_events.sort(key=lambda event: (event.when, event.request_key, event.model))
    return OpenCodexReconciliationResult(
        events=canonical_events,
        diagnostics=diagnostics,
    )


def apply_opencodex_account_hints(
    home: Path,
    events: list[UsageEvent],
    start: datetime,
    end: datetime,
    *,
    markers: list[OpenCodexUsageMarker] | None = None,
) -> int:
    if not events:
        return 0
    usage_markers = (
        scan_opencodex_usage_markers(home, start, end)
        if markers is None
        else markers
    )
    if not usage_markers:
        return 0
    events_by_signature: dict[tuple[str, int, int, int, int], list[int]] = {}
    for index, event in enumerate(events):
        if external_codex_provider_label(event.model):
            continue
        events_by_signature.setdefault(opencodex_event_signature(event), []).append(index)
    markers_by_signature: dict[tuple[str, int, int, int, int], list[int]] = {}
    for index, marker in enumerate(usage_markers):
        markers_by_signature.setdefault(opencodex_marker_signature(marker), []).append(index)

    matched = 0
    for signature, event_indexes in events_by_signature.items():
        marker_indexes = markers_by_signature.get(signature)
        if not marker_indexes:
            continue
        candidates_by_event: dict[int, list[tuple[float, int]]] = {}
        pairs: list[tuple[float, datetime, int, int]] = []
        for event_index in event_indexes:
            event = events[event_index]
            event_conversation_id = opencodex_conversation_id_for_event(event)
            for marker_index in marker_indexes:
                marker = usage_markers[marker_index]
                marker_conversation_id = marker.conversation_id.strip().casefold()
                if (
                    marker_conversation_id
                    and event_conversation_id
                    and marker_conversation_id != event_conversation_id
                ):
                    continue
                match_window = (
                    OPENCODEX_ACCOUNT_MATCH_WINDOW_SECONDS
                    if marker_conversation_id
                    and marker_conversation_id == event_conversation_id
                    else OPENCODEX_CROSS_SESSION_MATCH_WINDOW_SECONDS
                )
                distance = abs((event.when - marker.when).total_seconds())
                if distance > match_window:
                    continue
                candidates_by_event.setdefault(event_index, []).append(
                    (distance, marker_index)
                )
                pairs.append((distance, event.when, event_index, marker_index))
        ambiguous_events: set[int] = set()
        for event_index, candidates in candidates_by_event.items():
            candidates.sort(key=lambda item: (item[0], item[1]))
            if len(candidates) < 2:
                continue
            first_distance, first_index = candidates[0]
            second_distance, second_index = candidates[1]
            first_label = usage_markers[first_index].label
            second_label = usage_markers[second_index].label
            if (
                abs(second_distance - first_distance)
                <= OPENCODEX_ACCOUNT_MATCH_AMBIGUITY_SECONDS
                and first_label != second_label
            ):
                ambiguous_events.add(event_index)
        used_events: set[int] = set()
        used_markers: set[int] = set()
        for _distance, _when, event_index, marker_index in sorted(pairs):
            if (
                event_index in ambiguous_events
                or event_index in used_events
                or marker_index in used_markers
            ):
                continue
            event = events[event_index]
            marker = usage_markers[marker_index]
            if marker.label:
                event.account_label_hint = marker.label
                event.account_hint_source = OPENCODEX_ACCOUNT_HINT_SOURCE
            else:
                event.account_label_hint = API_SERVICE_AGGREGATE_LABEL
                event.account_hint_source = OPENCODEX_UNRESOLVED_HINT_SOURCE
            used_events.add(event_index)
            used_markers.add(marker_index)
            matched += 1

    markers_by_conversation: dict[str, list[OpenCodexUsageMarker]] = {}
    for marker in usage_markers:
        conversation_id = marker.conversation_id.strip().casefold()
        if conversation_id:
            markers_by_conversation.setdefault(conversation_id, []).append(marker)
    directly_anchored_turns = {
        turn_key
        for event in events
        if event.account_hint_source in {
            OPENCODEX_ACCOUNT_HINT_SOURCE,
            OPENCODEX_UNRESOLVED_HINT_SOURCE,
        }
        and (turn_key := api_service_event_turn_key(event))
    }
    for event in events:
        if event.account_hint_source or external_codex_provider_label(event.model):
            continue
        if api_service_event_turn_key(event) in directly_anchored_turns:
            continue
        conversation_id = opencodex_conversation_id_for_event(event)
        conversation_markers = markers_by_conversation.get(conversation_id, [])
        if not conversation_markers:
            continue
        turn_start = api_service_event_turn_start(event)
        route_confirmed = False
        for marker in conversation_markers:
            if opencodex_model_key(marker.model) != opencodex_model_key(event.model):
                continue
            completion_near = (
                abs((event.when - marker.when).total_seconds())
                <= OPENCODEX_CROSS_SESSION_MATCH_WINDOW_SECONDS
            )
            turn_near = (
                turn_start is not None
                and abs((turn_start - marker.request_at).total_seconds())
                <= OPENCODEX_TURN_START_MATCH_SECONDS
            )
            if completion_near or turn_near:
                route_confirmed = True
                break
        if route_confirmed:
            event.account_label_hint = API_SERVICE_AGGREGATE_LABEL
            event.account_hint_source = OPENCODEX_UNRESOLVED_HINT_SOURCE

    anchors_by_turn: dict[str, list[tuple[datetime, str]]] = {}
    for event in events:
        if event.account_hint_source not in {
            OPENCODEX_ACCOUNT_HINT_SOURCE,
            OPENCODEX_UNRESOLVED_HINT_SOURCE,
        }:
            continue
        turn_key = api_service_event_turn_key(event)
        if not turn_key:
            continue
        label = event.account_label_hint or API_SERVICE_AGGREGATE_LABEL
        anchors_by_turn.setdefault(turn_key, []).append((event.when, label))
    for anchors in anchors_by_turn.values():
        anchors.sort(key=lambda item: item[0])
    for event in events:
        if event.account_hint_source in {
            OPENCODEX_ACCOUNT_HINT_SOURCE,
            OPENCODEX_UNRESOLVED_HINT_SOURCE,
        } or external_codex_provider_label(event.model):
            continue
        turn_key = api_service_event_turn_key(event)
        anchors = anchors_by_turn.get(turn_key)
        if not anchors:
            continue
        anchor_times = [item[0] for item in anchors]
        position = bisect_right(anchor_times, event.when) - 1
        if position < 0:
            position = 0
        event.account_label_hint = anchors[position][1]
        event.account_hint_source = OPENCODEX_TURN_HINT_SOURCE
    return matched




_ATTRIBUTION_LEDGER_DOCUMENT_CACHE: tuple[tuple[Any, ...], dict[str, Any]] | None = None


def attribution_ledger_document(refresh: bool = False) -> dict[str, Any]:
    """Parse a stable ledger snapshot; it carries both events and verdicts."""
    global _ATTRIBUTION_LEDGER_DOCUMENT_CACHE

    def current_stamp() -> tuple[Any, ...]:
        try:
            stat = ATTRIBUTION_LEDGER_PATH.stat()
            return (
                str(ATTRIBUTION_LEDGER_PATH),
                stat.st_mtime_ns,
                stat.st_size,
            )
        except OSError:
            return (str(ATTRIBUTION_LEDGER_PATH),)

    for attempt in range(2):
        before = current_stamp()
        cached = _ATTRIBUTION_LEDGER_DOCUMENT_CACHE
        if not refresh and attempt == 0 and cached is not None and cached[0] == before:
            return cached[1]
        document = load_json_object(ATTRIBUTION_LEDGER_PATH)
        after = current_stamp()
        if before == after:
            _ATTRIBUTION_LEDGER_DOCUMENT_CACHE = (after, document)
            return document
        # Recovery or a concurrent atomic replace changed the file while it was
        # read. Re-open it once so cached data and its signature describe the
        # same generation.
        refresh = True

    _ATTRIBUTION_LEDGER_DOCUMENT_CACHE = (after, document)
    return document


def load_attribution_ledger() -> dict[str, str]:
    data = attribution_ledger_document()
    ledger = data.get("events")
    if not isinstance(ledger, dict):
        return {}
    result: dict[str, str] = {}
    for key, value in ledger.items():
        label = str(value or "").strip()
        if key and label:
            result[str(key)] = label
    return result


def api_service_verdict_tier_rank(tier: str) -> int:
    return API_SERVICE_VERDICT_TIER_RANKS.get(str(tier or "").strip(), 0)


def load_attribution_verdicts() -> dict[str, dict[str, str]]:
    """Read the archived account verdicts stored beside the legacy label map."""
    data = attribution_ledger_document().get("verdicts")
    if not isinstance(data, dict):
        return {}
    result: dict[str, dict[str, str]] = {}
    for key, value in data.items():
        if not key or not isinstance(value, dict):
            continue
        label = str(value.get("label") or "").strip()
        tier = str(value.get("tier") or "").strip()
        if not label or is_api_service_mirror_label(label):
            continue
        if api_service_verdict_tier_rank(tier) < API_SERVICE_VERDICT_ARCHIVE_MIN_TIER:
            continue
        result[str(key)] = {
            "label": label,
            "tier": tier,
            "at": str(value.get("at") or "").strip(),
        }
    return result


def record_attribution_verdict(
    verdicts: dict[str, dict[str, str]],
    event_id: str,
    label: str,
    tier: str,
    event_at: datetime,
) -> bool:
    """Archive a high-evidence account verdict so it survives evidence rotation.

    "at" carries the event's own time, not the run time, so retention prunes by
    how old the decided event is instead of re-stamping the archive every run.

    Live evidence wins: a verdict decided this run replaces an archived entry
    that names a different account, even from a lower tier. The archive must
    agree with the number this run just produced, otherwise a stale wrong entry
    would silently outlive the evidence that contradicts it. Within one account
    the tier only ever climbs, so a weaker re-confirmation cannot downgrade it.
    """
    global _VERDICT_ARCHIVE_DIRTY
    rank = api_service_verdict_tier_rank(tier)
    if rank < API_SERVICE_VERDICT_ARCHIVE_MIN_TIER:
        return False
    if not event_id or not label or is_api_service_mirror_label(label):
        return False
    stamp = event_at.replace(tzinfo=LOCAL_TZ).isoformat(timespec="seconds")
    _VERDICT_ARCHIVE_WRITES.add(event_id)
    previous = verdicts.get(event_id)
    if previous is not None and previous.get("label") == label:
        previous_rank = api_service_verdict_tier_rank(previous.get("tier", ""))
        if previous_rank > rank:
            return True
        if (
            previous_rank == rank
            and str(previous.get("at") or "")[:10] == stamp[:10]
        ):
            return True
    verdicts[event_id] = {"label": label, "tier": tier, "at": stamp}
    _VERDICT_ARCHIVE_DIRTY = True
    return True


def archived_attribution_verdict(
    verdicts: dict[str, dict[str, str]],
    event_id: str,
) -> str:
    entry = verdicts.get(event_id) if event_id else None
    if not isinstance(entry, dict):
        return ""
    label = str(entry.get("label") or "").strip()
    if not label or is_api_service_mirror_label(label):
        return ""
    if api_service_verdict_tier_rank(entry.get("tier", "")) < API_SERVICE_VERDICT_ARCHIVE_MIN_TIER:
        return ""
    return label


def merge_attribution_verdicts(
    stored: dict[str, Any] | None,
    verdicts: dict[str, dict[str, str]],
    decided_now: set[str] | None = None,
) -> dict[str, dict[str, str]]:
    """Fold this run's archive into whatever sits on disk at save time.

    The floating monitor spawns its own exporter, so two runs can archive
    verdicts at the same time; writing only the in-memory snapshot would drop
    the other run's entries. Keys this run decided from live evidence win, every
    other key keeps the stronger tier - and, at equal tiers, the newer stamp.
    """
    decided = decided_now or set()
    merged: dict[str, dict[str, str]] = {}
    for source in (stored, verdicts):
        if not isinstance(source, dict):
            continue
        for key, entry in source.items():
            event_id = str(key or "")
            if not event_id or not isinstance(entry, dict):
                continue
            label = str(entry.get("label") or "").strip()
            tier = str(entry.get("tier") or "").strip()
            if not label or not tier:
                continue
            candidate = {"label": label, "tier": tier, "at": str(entry.get("at") or "").strip()}
            previous = merged.get(event_id)
            if previous is not None and not (source is verdicts and event_id in decided):
                previous_rank = api_service_verdict_tier_rank(previous.get("tier", ""))
                rank = api_service_verdict_tier_rank(tier)
                if previous_rank > rank:
                    continue
                if previous_rank == rank and str(previous.get("at") or "") >= candidate["at"]:
                    continue
            merged[event_id] = candidate
    return merged


def prune_attribution_verdicts(
    verdicts: dict[str, dict[str, str]],
    now: datetime,
) -> dict[str, dict[str, str]]:
    """Bound the archive by retention window first, then by entry count."""
    cutoff = (
        (now - timedelta(days=API_SERVICE_VERDICT_RETENTION_DAYS))
        .replace(tzinfo=LOCAL_TZ)
        .isoformat(timespec="seconds")
    )
    kept = {
        event_id: entry
        for event_id, entry in verdicts.items()
        if str(entry.get("at") or "") >= cutoff
    }
    if len(kept) > API_SERVICE_VERDICT_ARCHIVE_LIMIT:
        newest = sorted(
            kept.items(),
            key=lambda item: (str(item[1].get("at") or ""), item[0]),
            reverse=True,
        )
        kept = dict(newest[:API_SERVICE_VERDICT_ARCHIVE_LIMIT])
    return kept


def save_attribution_ledger(
    ledger: dict[str, str],
    now: datetime,
    verdicts: dict[str, dict[str, str]] | None = None,
) -> None:
    global _LEDGER_DIRTY, _LEDGER_WRITES
    global _VERDICT_ARCHIVE_DIRTY, _VERDICT_ARCHIVE_WRITES
    try:
        with attribution_ledger_write_lock(ATTRIBUTION_LEDGER_PATH):
            verdicts_dirty = verdicts is not None and _VERDICT_ARCHIVE_DIRTY
            if not _LEDGER_DIRTY and not verdicts_dirty:
                logger.debug("attribution ledger unchanged; skipped save")
                return
            # Re-read while holding the process lock so no writer can replace
            # the generation between this merge and the atomic save.
            document = attribution_ledger_document(refresh=True)
            previous_events = document.get("events")
            refused_events = (
                isinstance(previous_events, dict)
                and len(previous_events) > 1000
                and len(ledger) < len(previous_events) * 0.5
            )
            if refused_events:
                logger.warning(
                    "refused attribution ledger save: %d entries would replace %d",
                    len(ledger),
                    len(previous_events),
                )
                if not verdicts_dirty:
                    return
                # The shrink guard only protects the event labels. The archive
                # still has to land, otherwise a truncated scan would also cost
                # every verdict this run decided.
                events = {
                    str(key): str(value or "").strip()
                    for key, value in previous_events.items()
                    if key and str(value or "").strip()
                }
            elif isinstance(previous_events, dict):
                # Preserve keys written by another exporter after this process
                # loaded its snapshot. Only labels changed by this run may
                # override the copy currently on disk.
                events = {
                    str(key): str(value or "").strip()
                    for key, value in previous_events.items()
                    if key and str(value or "").strip()
                }
                for key, value in ledger.items():
                    if key not in events or key in _LEDGER_WRITES:
                        events[str(key)] = str(value or "").strip()
            else:
                events = dict(ledger)
            if verdicts is None:
                stored = document.get("verdicts")
                archived = stored if isinstance(stored, dict) else {}
            else:
                archived = prune_attribution_verdicts(
                    merge_attribution_verdicts(
                        document.get("verdicts"),
                        verdicts,
                        _VERDICT_ARCHIVE_WRITES,
                    ),
                    now,
                )
            payload: dict[str, Any] = {
                "schema": 1,
                "updated_at": now.replace(tzinfo=LOCAL_TZ).isoformat(timespec="seconds"),
                "events": dict(sorted(events.items())),
            }
            if archived:
                payload["verdicts"] = dict(sorted(archived.items()))
            saved = write_json_object(ATTRIBUTION_LEDGER_PATH, payload)
            if not saved:
                return
            if not refused_events:
                _LEDGER_DIRTY = False
                _LEDGER_WRITES = set()
            if verdicts is not None:
                _VERDICT_ARCHIVE_DIRTY = False
                _VERDICT_ARCHIVE_WRITES = set()
            attribution_ledger_document(refresh=True)
            refresh_json_backup(ATTRIBUTION_LEDGER_PATH)
    except (OSError, TimeoutError) as exc:
        # Keep the dirty flags set so a later exporter retries the save.
        logger.warning("attribution ledger save deferred: %s", exc)


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


_COCKPIT_ACCOUNT_LABEL_CACHE: dict[str, tuple[tuple[Any, ...], dict[str, str]]] = {}


def cockpit_codex_account_manifest_stamp(home: Path) -> tuple[Any, ...]:
    """Identify the account manifest files so labels are parsed once per run."""
    stamps: list[Any] = []
    manifest = home / ".antigravity_cockpit" / "codex_accounts.json"
    accounts_dir = home / ".antigravity_cockpit" / "codex_accounts"
    candidates = [manifest]
    try:
        candidates.extend(sorted(accounts_dir.glob("*.json*")))
    except OSError:
        pass
    for path in candidates:
        try:
            stat = path.stat()
        except OSError:
            stamps.append((path.name, 0, -1))
            continue
        stamps.append((path.name, int(stat.st_mtime_ns), int(stat.st_size)))
    return tuple(stamps)


def cockpit_codex_account_label_by_id(home: Path) -> dict[str, str]:
    stamp = cockpit_codex_account_manifest_stamp(home)
    cache_key = os.path.normcase(str(home))
    cached = _COCKPIT_ACCOUNT_LABEL_CACHE.get(cache_key)
    if cached is not None and cached[0] == stamp:
        return dict(cached[1])
    labels = cockpit_codex_account_labels_from_disk(home)
    _COCKPIT_ACCOUNT_LABEL_CACHE[cache_key] = (stamp, dict(labels))
    return labels


def cockpit_codex_account_labels_from_disk(home: Path) -> dict[str, str]:
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


def normalize_cockpit_auth_id(value: Any) -> str:
    account_id = str(value or "").strip().strip('"').strip("'")
    if account_id.lower().endswith(".json"):
        account_id = account_id[:-5]
    return account_id


def cockpit_affinity_account_label_by_id(
    home: Path,
    request_markers: list[AccountMarker] | None = None,
) -> dict[str, str]:
    labels: dict[str, str] = {}
    for account_id, label in cockpit_codex_account_label_by_id(home).items():
        normalized = normalize_cockpit_auth_id(account_id)
        if not normalized or not usable_cockpit_account_label(label):
            continue
        labels[normalized] = prefer_cockpit_account_label(
            labels.get(normalized),
            label,
        )
    for marker in request_markers or []:
        account_id = normalize_cockpit_auth_id(marker.account_id)
        if account_id and usable_cockpit_account_label(marker.label):
            labels[account_id] = prefer_cockpit_account_label(
                labels.get(account_id),
                marker.label,
            )

    db_path = home / ".antigravity_cockpit" / "codex_local_access_logs.sqlite"
    if not db_path.exists():
        return labels
    try:
        connection = connect_cockpit_sqlite_readonly(db_path)
        rows = connection.execute(
            """
            SELECT logs.account_id, logs.email, logs.api_key_label
            FROM request_logs AS logs
            INNER JOIN (
                SELECT account_id, MAX(id) AS latest_id
                FROM request_logs
                WHERE account_id <> ''
                GROUP BY account_id
            ) AS latest
                ON latest.latest_id = logs.id
            """
        ).fetchall()
        connection.close()
    except sqlite3.Error as exc:
        warn_cockpit_sqlite_error("cockpit_affinity_account_label_by_id", exc)
        return labels
    for account_id, email, api_key_label in rows:
        normalized = normalize_cockpit_auth_id(account_id)
        label = cockpit_account_label(
            str(account_id or ""),
            str(email or ""),
            str(api_key_label or ""),
        )
        if normalized and usable_cockpit_account_label(label):
            labels[normalized] = prefer_cockpit_account_label(
                labels.get(normalized),
                label,
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
    except json.JSONDecodeError:
        data = recover_corrupt_json(path)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def write_json_object(path: Path, data: dict[str, Any]) -> bool:
    try:
        write_json_atomic(path, data)
        return True
    except Exception as exc:
        logger.warning("failed to write %s: %s", path.name, exc)
        return False


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
    if remaining_percent is None:
        return None
    window = quota_window_payload(
        remaining_percent,
        reset_at,
        False,
        max(1, round(window_seconds / 60)),
    )
    if not window.get("resets_at"):
        return None
    window.update(
        {
            "quota_source": "official-wham",
            "quota_snapshot_at": checked_at.isoformat(timespec="seconds"),
            "quota_reset_unavailable": False,
        }
    )
    return window


def official_quota_from_usage_response(
    payload: Any,
    checked_at: datetime | None = None,
    fallback_plan_type: str = "",
) -> dict[str, dict[str, Any]] | None:
    if not isinstance(payload, dict):
        return None
    rate_limit = payload.get("rate_limit")
    if not isinstance(rate_limit, dict) or not rate_limit:
        return None
    checked_at = checked_at or datetime.now(LOCAL_TZ)
    seven_day_seconds = 7 * 24 * 60 * 60
    windows = (
        (rate_limit.get("primary_window"), 5 * 60 * 60),
        (rate_limit.get("secondary_window"), seven_day_seconds),
    )
    def absent_window() -> dict[str, Any]:
        return {
            "quota_available": False,
            "quota_stale": False,
            "quota_source": "official-wham",
            "quota_snapshot_at": checked_at.isoformat(timespec="seconds"),
            "quota_absent_confirmed": True,
        }

    five_hour: dict[str, Any] = absent_window()
    seven_day: dict[str, Any] = absent_window()
    cycle: dict[str, Any] = absent_window()
    short_window_present = False
    seven_day_present = False
    cycle_present = False
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
            cycle_present = True
    if not (short_window_present or seven_day_present or cycle_present):
        return None
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


def quota_window_has_usable_value(window: Any) -> bool:
    if not isinstance(window, dict):
        return False
    return bool(
        window.get("quota_unlimited")
        or window.get("quota_available")
        or window.get("remaining_percent") is not None
        or window.get("utilization") is not None
    )


def quota_window_is_live(window: Any) -> bool:
    if not isinstance(window, dict) or window.get("quota_stale"):
        return False
    if window.get("quota_unlimited"):
        return True
    return bool(window.get("quota_available") and window.get("resets_at"))


def quota_window_source_rank(window: Any) -> int:
    if not isinstance(window, dict):
        return -1
    source = str(window.get("quota_source") or "").strip()
    if source == "official-wham":
        return 2
    if source == "sidecar-reserve":
        return 1
    return 0


def quota_window_snapshot_epoch(window: Any, fallback: float = 0.0) -> float:
    if not isinstance(window, dict):
        return fallback
    parsed = parse_dt(window.get("quota_snapshot_at"))
    if parsed is None:
        return fallback
    return parsed.replace(tzinfo=LOCAL_TZ).timestamp()


def prefer_quota_window(
    current: Any,
    incoming: Any,
    current_success: float = 0.0,
    incoming_success: float = 0.0,
) -> dict[str, Any]:
    current_window = current if isinstance(current, dict) else None
    incoming_window = incoming if isinstance(incoming, dict) else None
    if current_window is None and incoming_window is None:
        return {"quota_available": False, "quota_stale": False}
    if incoming_window is None:
        return dict(current_window)
    if current_window is None:
        return dict(incoming_window)

    current_epoch = quota_window_snapshot_epoch(current_window, current_success)
    incoming_epoch = quota_window_snapshot_epoch(incoming_window, incoming_success)
    current_absent = bool(
        current_window.get("quota_absent_confirmed")
        and not current_window.get("quota_stale")
        and quota_window_source_rank(current_window) >= 2
    )
    incoming_absent = bool(
        incoming_window.get("quota_absent_confirmed")
        and not incoming_window.get("quota_stale")
        and quota_window_source_rank(incoming_window) >= 2
    )
    # A successful official response that omits a window is stronger evidence
    # than a lower-tier sidecar value or an older official generation.
    if incoming_absent and (
        quota_window_source_rank(incoming_window)
        > quota_window_source_rank(current_window)
        or incoming_epoch >= current_epoch
    ):
        return dict(incoming_window)
    if current_absent and (
        quota_window_source_rank(current_window)
        > quota_window_source_rank(incoming_window)
        or current_epoch > incoming_epoch
    ):
        return dict(current_window)

    current_live = quota_window_is_live(current_window)
    incoming_live = quota_window_is_live(incoming_window)
    if current_live and not incoming_live:
        return dict(current_window)
    if incoming_live and not current_live:
        return dict(incoming_window)

    current_usable = quota_window_has_usable_value(current_window)
    incoming_usable = quota_window_has_usable_value(incoming_window)
    current_rank = quota_window_source_rank(current_window)
    incoming_rank = quota_window_source_rank(incoming_window)
    if incoming_rank != current_rank:
        if incoming_rank > current_rank and incoming_usable:
            return dict(incoming_window)
        if current_rank > incoming_rank and current_usable:
            return dict(current_window)
        if incoming_usable:
            return dict(incoming_window)
        if current_usable:
            return dict(current_window)

    if incoming_epoch > current_epoch and (incoming_usable or not current_usable):
        return dict(incoming_window)
    if current_epoch > incoming_epoch and (current_usable or not incoming_usable):
        return dict(current_window)
    if incoming_usable:
        return dict(incoming_window)
    if current_usable:
        return dict(current_window)
    if incoming_epoch >= current_epoch:
        return dict(incoming_window)
    return dict(current_window)


def merge_quota_rows(current: Any, incoming: Any) -> dict[str, dict[str, Any]]:
    current_row = current if isinstance(current, dict) else {}
    incoming_row = incoming if isinstance(incoming, dict) else {}
    merged: dict[str, dict[str, Any]] = {}
    for window_key in ("window_5h", "window_7d", "window_cycle"):
        merged[window_key] = prefer_quota_window(
            current_row.get(window_key),
            incoming_row.get(window_key),
        )
    return merged


def mark_quota_snapshot_stale(quota: Any) -> dict[str, Any] | None:
    if not isinstance(quota, dict):
        return None
    stale = dict(quota)
    for window_key in ("window_5h", "window_7d", "window_cycle"):
        raw_window = quota.get(window_key)
        if not isinstance(raw_window, dict):
            continue
        window = dict(raw_window)
        window["quota_stale"] = True
        stale[window_key] = window
    return stale


def stale_official_quota_snapshot(quota: Any) -> dict[str, dict[str, Any]] | None:
    stale = mark_quota_snapshot_stale(quota)
    if stale is None:
        return None
    has_last_known_value = any(
        quota_window_has_usable_value(stale.get(window_key))
        for window_key in ("window_5h", "window_7d", "window_cycle")
    )
    return stale if has_last_known_value else None


def load_official_quota_cache() -> dict[str, Any]:
    try:
        data = json.loads(COCKPIT_OFFICIAL_QUOTA_CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    accounts = data.get("accounts") if isinstance(data, dict) else None
    return accounts if isinstance(accounts, dict) else {}


@contextmanager
def official_quota_cache_lock():
    # Reuse the process-safe mutex implementation already used by the
    # attribution ledger.  In particular, lock acquisition failures must
    # propagate to the writer; writing without the lock would reintroduce the
    # stale-process cache overwrite this guard is meant to prevent.
    with attribution_ledger_write_lock(
        COCKPIT_OFFICIAL_QUOTA_CACHE_PATH,
        timeout_seconds=5.0,
    ):
        yield


def official_quota_entry_success_epoch(entry: Any) -> float:
    if not isinstance(entry, dict):
        return 0.0
    try:
        success = float(entry.get("last_success_at") or 0)
    except (TypeError, ValueError):
        success = 0.0
    if success > 0:
        return success
    if entry.get("refresh_failed"):
        return 0.0
    try:
        return float(entry.get("checked_at") or 0)
    except (TypeError, ValueError):
        return 0.0


def official_quota_entry_checked_epoch(entry: Any) -> float:
    if not isinstance(entry, dict):
        return 0.0
    try:
        return float(entry.get("checked_at") or 0)
    except (TypeError, ValueError):
        return 0.0


def official_quota_row_is_live(quota: Any) -> bool:
    if not isinstance(quota, dict):
        return False
    return any(
        quota_window_is_live(quota.get(window_key))
        for window_key in ("window_5h", "window_7d", "window_cycle")
    )


def official_quota_entry_source_rank(entry: Any) -> int:
    quota = entry.get("quota") if isinstance(entry, dict) else None
    if not isinstance(quota, dict):
        return -1
    return max(
        quota_window_source_rank(quota.get(window_key))
        for window_key in ("window_5h", "window_7d", "window_cycle")
    )


def prefer_official_cache_window(
    disk_window: Any,
    incoming_window: Any,
    disk_success: float,
    incoming_success: float,
) -> dict[str, Any]:
    return prefer_quota_window(
        disk_window,
        incoming_window,
        disk_success,
        incoming_success,
    )


def merge_official_quota_cache_entry(disk_entry: Any, incoming_entry: Any) -> dict[str, Any] | None:
    if not isinstance(incoming_entry, dict):
        return dict(disk_entry) if isinstance(disk_entry, dict) else None
    if not isinstance(disk_entry, dict):
        return dict(incoming_entry)
    disk_success = official_quota_entry_success_epoch(disk_entry)
    incoming_success = official_quota_entry_success_epoch(incoming_entry)
    disk_quota = disk_entry.get("quota") if isinstance(disk_entry.get("quota"), dict) else {}
    incoming_quota = incoming_entry.get("quota") if isinstance(incoming_entry.get("quota"), dict) else {}
    merged_quota: dict[str, dict[str, Any]] = {}
    for window_key in ("window_5h", "window_7d", "window_cycle"):
        merged_quota[window_key] = prefer_official_cache_window(
            disk_quota.get(window_key),
            incoming_quota.get(window_key),
            disk_success,
            incoming_success,
        )
    merged = dict(disk_entry)
    for key, value in incoming_entry.items():
        if key in {"quota", "refresh_failed", "last_success_at", "checked_at"}:
            continue
        if value not in (None, ""):
            merged[key] = value
    merged["quota"] = merged_quota
    disk_live = official_quota_row_is_live(disk_quota)
    incoming_live = official_quota_row_is_live(incoming_quota)
    disk_rank = official_quota_entry_source_rank(disk_entry)
    incoming_rank = official_quota_entry_source_rank(incoming_entry)
    same_generation_failure = bool(
        incoming_entry.get("refresh_failed")
        and not incoming_live
        and incoming_success > 0
        and incoming_success == disk_success
        and incoming_rank >= disk_rank
    )
    if same_generation_failure:
        merged_quota = {}
        for window_key in ("window_5h", "window_7d", "window_cycle"):
            incoming_window = incoming_quota.get(window_key)
            merged_quota[window_key] = (
                dict(incoming_window)
                if isinstance(incoming_window, dict)
                else prefer_official_cache_window(
                    disk_quota.get(window_key),
                    incoming_window,
                    disk_success,
                    incoming_success,
                )
            )
        merged["quota"] = merged_quota
        timestamp_source = incoming_entry
    elif incoming_live and (
        not disk_live
        or incoming_rank > disk_rank
        or (incoming_rank == disk_rank and incoming_success > disk_success)
    ):
        timestamp_source = incoming_entry
    elif disk_live and (
        not incoming_live
        or disk_rank > incoming_rank
        or disk_success > incoming_success
    ):
        timestamp_source = disk_entry
    elif incoming_rank != disk_rank:
        timestamp_source = incoming_entry if incoming_rank > disk_rank else disk_entry
    elif incoming_success > disk_success:
        timestamp_source = incoming_entry
    elif disk_success > incoming_success:
        timestamp_source = disk_entry
    else:
        timestamp_source = incoming_entry
    source_success = official_quota_entry_success_epoch(timestamp_source)
    if source_success > 0:
        merged["last_success_at"] = source_success
    else:
        newest_success = max(disk_success, incoming_success)
        if newest_success > 0:
            merged["last_success_at"] = newest_success
    checked_at = official_quota_entry_checked_epoch(timestamp_source)
    if checked_at > 0:
        merged["checked_at"] = checked_at
    if timestamp_source.get("refresh_failed"):
        merged["refresh_failed"] = True
    elif official_quota_row_is_live(merged_quota) and not quota_row_needs_official_refresh(
        merged_quota
    ):
        merged.pop("refresh_failed", None)
    else:
        merged.pop("refresh_failed", None)
    return merged


def merge_official_quota_cache_accounts(
    disk_accounts: dict[str, Any],
    incoming_accounts: dict[str, Any],
) -> dict[str, Any]:
    disk = disk_accounts if isinstance(disk_accounts, dict) else {}
    incoming = incoming_accounts if isinstance(incoming_accounts, dict) else {}
    merged: dict[str, Any] = {}
    for account_id in set(disk) | set(incoming):
        entry = merge_official_quota_cache_entry(disk.get(account_id), incoming.get(account_id))
        if isinstance(entry, dict):
            merged[str(account_id)] = entry
    return merged


def write_official_quota_cache(accounts: dict[str, Any]) -> None:
    incoming = {
        str(account_id): dict(entry)
        for account_id, entry in accounts.items()
        if isinstance(entry, dict)
    }
    try:
        with official_quota_cache_lock():
            merged = merge_official_quota_cache_accounts(load_official_quota_cache(), incoming)
            write_json_atomic(
                COCKPIT_OFFICIAL_QUOTA_CACHE_PATH,
                {"schema": 2, "accounts": merged},
            )
    except OSError:
        pass


def cached_official_quota_by_label(
    cache: dict[str, Any],
    active_account_ids: set[str] | None = None,
) -> dict[str, dict[str, dict[str, Any]]]:
    """Return stale last-known quota for accounts no longer in Cockpit's pool."""
    active_ids = active_account_ids or set()
    result: dict[str, dict[str, dict[str, Any]]] = {}
    for account_id, entry in cache.items():
        if account_id in active_ids or not isinstance(entry, dict):
            continue
        label = str(entry.get("label") or entry.get("email") or "").strip()
        if not label:
            continue
        stale_quota = stale_official_quota_snapshot(entry.get("quota"))
        if stale_quota is not None:
            result[label] = stale_quota
    return result


def cockpit_backup_quota_by_label(
    home: Path,
    now: datetime | None = None,
) -> dict[str, dict[str, dict[str, Any]]]:
    """Read a recent decrypted Cockpit backup when sidecar auth is unavailable."""
    backup_dir = home / ".antigravity_cockpit" / "backups"
    if not backup_dir.exists():
        return {}
    paths = sorted(
        backup_dir.glob("cockpit_auto_backup_full_*.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    current = now or datetime.now(LOCAL_TZ)
    current_local = (
        current.astimezone(LOCAL_TZ).replace(tzinfo=None)
        if current.tzinfo is not None
        else current
    )
    for path in paths[:4]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        exported_at = parse_dt(payload.get("exported_at"))
        if exported_at is None:
            continue
        age_seconds = (current_local - exported_at).total_seconds()
        if age_seconds < -300 or age_seconds > max(
            60,
            COCKPIT_QUOTA_RESERVE_STALE_SECONDS,
        ):
            return {}
        accounts = payload.get("accounts")
        platforms = accounts.get("platforms") if isinstance(accounts, dict) else None
        codex = platforms.get("codex") if isinstance(platforms, dict) else None
        rows = codex.get("exported_data") if isinstance(codex, dict) else None
        if not isinstance(rows, list):
            continue
        snapshot_at = exported_at.replace(tzinfo=LOCAL_TZ).isoformat(timespec="seconds")
        result: dict[str, dict[str, dict[str, Any]]] = {}
        for account in rows:
            if not isinstance(account, dict):
                continue
            quota = account.get("quota")
            if not isinstance(quota, dict):
                continue
            account_id = str(account.get("id") or "").strip()
            label = cockpit_account_label(
                account_id,
                str(account.get("email") or ""),
                str(account.get("api_provider_name") or account.get("name") or ""),
            )
            if not label:
                continue
            plan_type = str(
                account.get("plan_type") or quota.get("plan_type") or ""
            ).strip().lower()
            primary_present = bool(quota.get("hourly_window_present"))
            weekly_present = bool(quota.get("weekly_window_present"))
            try:
                primary_minutes = int(quota.get("hourly_window_minutes") or 5 * 60)
            except (TypeError, ValueError):
                primary_minutes = 5 * 60
            if primary_minutes <= 0:
                primary_minutes = 5 * 60
            seven_day_minutes = 7 * 24 * 60
            five_hour: dict[str, Any] = {
                "quota_available": False,
                "quota_stale": False,
            }
            seven_day: dict[str, Any] = {
                "quota_available": False,
                "quota_stale": False,
            }
            cycle: dict[str, Any] = {
                "quota_available": False,
                "quota_stale": False,
            }
            primary_is_7d = primary_present and primary_minutes == seven_day_minutes
            if primary_present:
                primary = quota_window_payload(
                    quota.get("hourly_percentage"),
                    quota.get("hourly_reset_time"),
                    False,
                    primary_minutes,
                )
                if primary_minutes < seven_day_minutes:
                    five_hour = primary
                elif primary_minutes == seven_day_minutes:
                    seven_day = primary
                else:
                    cycle = primary
            if weekly_present and not primary_is_7d:
                seven_day = quota_window_payload(
                    quota.get("weekly_percentage"),
                    quota.get("weekly_reset_time"),
                    False,
                    seven_day_minutes,
                )
            if plan_type == "plus" and not (
                primary_present and primary_minutes < seven_day_minutes
            ) and (primary_is_7d or weekly_present):
                five_hour = {
                    "quota_available": False,
                    "quota_stale": False,
                    "quota_unlimited": True,
                }
            for window in (five_hour, seven_day, cycle):
                window["quota_source"] = "official-wham"
                window["quota_snapshot_at"] = snapshot_at
                window["quota_transport"] = "cockpit-backup"
            result[label] = {
                "window_5h": five_hour,
                "window_7d": seven_day,
                "window_cycle": cycle,
            }
        return result
    return {}


def persist_quota_snapshots_by_account(
    accounts: dict[str, dict[str, Any]],
    labels_by_id: dict[str, str],
    quota_by_label: dict[str, dict[str, dict[str, Any]]],
    now: datetime | None = None,
) -> None:
    """Persist every real quota snapshot, including sidecar-derived values."""
    cache = load_official_quota_cache()
    snapshot_epoch = (now or datetime.now(LOCAL_TZ)).timestamp()
    dirty: dict[str, Any] = {}
    for account_id, account in accounts.items():
        label = labels_by_id.get(account_id, "")
        quota = quota_by_label.get(label)
        if not label or stale_official_quota_snapshot(quota) is None:
            continue
        previous = cache.get(account_id)
        entry = dict(previous) if isinstance(previous, dict) else {}
        quota_changed = entry.get("quota") != quota
        entry.update(
            {
                "label": label,
                "email": str(account.get("email") or ""),
                "plan_type": str(account.get("plan_type") or ""),
                "quota": quota,
            }
        )
        has_official_window = any(
            str((quota.get(window_key) or {}).get("quota_source") or "")
            == "official-wham"
            for window_key in ("window_5h", "window_7d", "window_cycle")
        )
        if not has_official_window and not quota_row_needs_official_refresh(quota) and (
            quota_changed or not entry.get("last_success_at")
        ):
            entry["checked_at"] = snapshot_epoch
            entry["last_success_at"] = snapshot_epoch
            entry.pop("refresh_failed", None)
        if entry != previous:
            cache[account_id] = entry
            dirty[account_id] = entry
    if not dirty:
        return
    write_official_quota_cache(dirty)


def official_quota_auth_from_payload(data: Any) -> dict[str, Any] | None:
    if not isinstance(data, dict) or data.get("disabled"):
        return None
    tokens = data.get("tokens") if isinstance(data.get("tokens"), dict) else {}
    access_token = str(data.get("access_token") or tokens.get("access_token") or "").strip()
    claims = decode_jwt_payload(
        tokens.get("id_token") or data.get("id_token") or access_token
    )
    auth_claims = claims.get("https://api.openai.com/auth")
    auth_claims = auth_claims if isinstance(auth_claims, dict) else {}
    account_id = str(
        data.get("account_id")
        or tokens.get("account_id")
        or auth_claims.get("chatgpt_account_id")
        or claims.get("chatgpt_account_id")
        or ""
    ).strip()
    email = str(
        data.get("email")
        or tokens.get("email")
        or claims.get("email")
        or claims.get("preferred_username")
        or ""
    ).strip().lower()
    expired = data.get("expired")
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
    access_claims = decode_jwt_payload(access_token)
    try:
        token_exp = float(access_claims.get("exp") or 0)
    except (TypeError, ValueError):
        token_exp = 0.0
    if token_exp > 1 and token_exp <= datetime.now().timestamp():
        return None
    if not access_token or not account_id:
        return None
    auth = {
        "access_token": access_token,
        "account_id": account_id,
        "email": email,
    }
    proxy_url = str(data.get("proxy_url") or "").strip()
    if proxy_url:
        auth["proxy_url"] = proxy_url
    return auth


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
    return official_quota_auth_from_payload(auth)


def read_codex_home_official_auth(home: Path) -> dict[str, Any] | None:
    codex_dir = home / ".codex"
    # Prefer the live Codex login. Cockpit's sidecar metadata file can name the
    # current account without carrying an access token.
    for name in ("auth.json", ".cockpit_codex_auth.json"):
        path = codex_dir / name
        try:
            auth = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        parsed = official_quota_auth_from_payload(auth)
        if parsed is not None:
            return parsed
    return None


def read_official_quota_auth(
    home: Path,
    account_id: str,
    account: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    sidecar = read_cockpit_sidecar_auth(home, account_id)
    if sidecar is not None:
        return sidecar
    current = read_codex_home_official_auth(home)
    if current is None:
        return None
    account = account if isinstance(account, dict) else {}
    email = str(account.get("email") or "").strip().lower()
    if email and current.get("email") == email:
        return current
    return None


def normalize_http_proxy_url(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    mapped: dict[str, str] = {}
    for segment in raw.split(";"):
        key, separator, candidate = segment.partition("=")
        if separator and key.strip().lower() in {"http", "https"}:
            mapped[key.strip().lower()] = candidate.strip()
    candidate = mapped.get("https") or mapped.get("http") or raw
    if ";" in candidate:
        candidate = candidate.split(";", 1)[0].strip()
    if "://" not in candidate:
        candidate = f"http://{candidate}"
    parsed = parse.urlparse(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    return candidate


def windows_user_proxy_url() -> str:
    if os.name != "nt":
        return ""
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        ) as key:
            enabled = int(winreg.QueryValueEx(key, "ProxyEnable")[0] or 0)
            proxy_server = winreg.QueryValueEx(key, "ProxyServer")[0]
    except (ImportError, OSError, TypeError, ValueError):
        return ""
    return normalize_http_proxy_url(proxy_server) if enabled else ""


def official_quota_proxy_url(auth: dict[str, Any]) -> str:
    explicit = normalize_http_proxy_url(auth.get("proxy_url"))
    if explicit:
        return explicit
    configured = request.getproxies()
    if configured.get("https") or configured.get("http"):
        # urlopen already honors process-level proxy configuration.
        return ""
    return windows_user_proxy_url()


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
        proxy_url = official_quota_proxy_url(auth)
        if proxy_url:
            opener = request.build_opener(
                request.ProxyHandler({"http": proxy_url, "https": proxy_url})
            )
            response_context = opener.open(
                req,
                timeout=COCKPIT_OFFICIAL_QUOTA_TIMEOUT_SECONDS,
            )
        else:
            response_context = request.urlopen(
                req,
                timeout=COCKPIT_OFFICIAL_QUOTA_TIMEOUT_SECONDS,
            )
        with response_context as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:
        return None
    return official_quota_from_usage_response(payload, checked_at, plan_type)


def cockpit_official_quota_by_account(
    home: Path,
    accounts: dict[str, dict[str, Any]],
    now: datetime | None = None,
    force_refresh_account_ids: set[str] | None = None,
    active_account_ids: set[str] | None = None,
) -> dict[str, dict[str, dict[str, Any]]]:
    if not COCKPIT_OFFICIAL_QUOTA_ENABLED or not accounts:
        return {}
    now = now or datetime.now(LOCAL_TZ)
    now_epoch = now.timestamp()
    cache = load_official_quota_cache()
    result: dict[str, dict[str, dict[str, Any]]] = {}
    pending: dict[str, tuple[dict[str, Any], str]] = {}
    dirty: dict[str, Any] = {}
    force_refresh_ids = force_refresh_account_ids or set()
    active_refresh_ids = active_account_ids or set()
    for account_id, account in accounts.items():
        cached = cache.get(account_id)
        label = cockpit_account_label(
            account_id,
            str(account.get("email") or ""),
            str(account.get("api_provider_name") or account.get("name") or ""),
        )
        plan_type = str(account.get("plan_type") or "")
        if isinstance(cached, dict):
            metadata = {
                "label": label,
                "email": str(account.get("email") or ""),
                "plan_type": plan_type,
            }
            for key, value in metadata.items():
                if value and cached.get(key) != value:
                    cached[key] = value
                    dirty[account_id] = cached
        try:
            checked_epoch = float(cached.get("checked_at") or 0) if isinstance(cached, dict) else 0.0
        except (TypeError, ValueError):
            checked_epoch = 0.0
        cache_age = now_epoch - checked_epoch if checked_epoch > 0 else float("inf")
        if isinstance(cached, dict) and cached.get("refresh_failed"):
            cache_seconds = COCKPIT_OFFICIAL_QUOTA_FAILURE_RETRY_SECONDS
        elif account_id in active_refresh_ids:
            cache_seconds = COCKPIT_OFFICIAL_QUOTA_ACTIVE_CACHE_SECONDS
        else:
            cache_seconds = COCKPIT_OFFICIAL_QUOTA_CACHE_SECONDS
        if account_id not in force_refresh_ids and 0 <= cache_age < cache_seconds:
            quota = cached.get("quota") if isinstance(cached, dict) else None
            if isinstance(quota, dict):
                result[account_id] = quota
            continue
        auth = read_official_quota_auth(home, account_id, account)
        if auth is not None:
            pending[account_id] = (auth, plan_type)
        else:
            previous_quota = cached.get("quota") if isinstance(cached, dict) else None
            stale_quota = stale_official_quota_snapshot(previous_quota)
            if stale_quota is not None:
                result[account_id] = stale_quota

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
                previous_entry = cache.get(account_id)
                previous_quota = (
                    previous_entry.get("quota")
                    if isinstance(previous_entry, dict)
                    else None
                )
                account = accounts.get(account_id) or {}
                label = cockpit_account_label(
                    account_id,
                    str(account.get("email") or ""),
                    str(account.get("api_provider_name") or account.get("name") or ""),
                )
                metadata = {
                    "label": label,
                    "email": str(account.get("email") or ""),
                    "plan_type": str(account.get("plan_type") or ""),
                }
                if isinstance(quota, dict):
                    cache_entry = {
                        **metadata,
                        "checked_at": now_epoch,
                        "last_success_at": now_epoch,
                        "quota": quota,
                    }
                    cache[account_id] = cache_entry
                    dirty[account_id] = cache_entry
                    result[account_id] = quota
                else:
                    cache_entry: dict[str, Any] = (
                        dict(previous_entry) if isinstance(previous_entry, dict) else {}
                    )
                    cache_entry.update(metadata)
                    cache_entry["checked_at"] = now_epoch
                    cache_entry["refresh_failed"] = True
                    retained_quota = mark_quota_snapshot_stale(previous_quota)
                    if retained_quota is not None:
                        cache_entry["quota"] = retained_quota
                        result[account_id] = retained_quota
                    else:
                        cache_entry.pop("quota", None)
                    if isinstance(previous_entry, dict):
                        last_success_at = (
                            previous_entry.get("last_success_at")
                            or previous_entry.get("checked_at")
                        )
                        if last_success_at not in (None, ""):
                            cache_entry["last_success_at"] = last_success_at
                    cache[account_id] = cache_entry
                    dirty[account_id] = cache_entry

    if dirty:
        write_official_quota_cache(dirty)
    return result


def cockpit_recent_usage_account_ids(
    home: Path,
    now: datetime | None = None,
) -> set[str]:
    db_path = home / ".antigravity_cockpit" / "codex_local_access_logs.sqlite"
    if not db_path.exists():
        return set()
    now = now or datetime.now(LOCAL_TZ)
    start = now - timedelta(seconds=COCKPIT_OFFICIAL_QUOTA_ACTIVE_LOOKBACK_SECONDS)
    try:
        con = connect_cockpit_sqlite_readonly(db_path)
        rows = con.execute(
            f"""
            SELECT DISTINCT account_id
            FROM request_logs
            WHERE timestamp >= ? AND timestamp <= ?
              AND account_id IS NOT NULL
              AND TRIM(account_id) != ''
              AND {COCKPIT_RECORDED_USAGE_SQL}
            """,
            (local_epoch_ms(start), local_epoch_ms(now)),
        ).fetchall()
        con.close()
    except (OSError, sqlite3.Error):
        return set()
    return {str(row[0]).strip() for row in rows if row and str(row[0]).strip()}


def cockpit_codex_quota_by_label(
    home: Path,
    force_active_official_refresh: bool = False,
) -> dict[str, dict[str, dict[str, Any]]]:
    accounts_dir = home / ".antigravity_cockpit" / "codex_accounts"
    result: dict[str, dict[str, dict[str, Any]]] = {}
    source_accounts_by_id: dict[str, dict[str, Any]] = {}
    account_paths = accounts_dir.glob("*.json") if accounts_dir.exists() else ()
    for path in account_paths:
        try:
            account = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            continue
        if not isinstance(account, dict):
            continue
        account_id = str(account.get("id") or path.stem)
        source_accounts_by_id[account_id] = account
        quota = account.get("quota")
        if not isinstance(quota, dict):
            continue

        label = cockpit_account_label(
            account_id,
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
    all_accounts_by_id = dict(source_accounts_by_id)
    all_accounts_by_id.update(manifest_by_id)
    labels_by_id = {
        account_id: cockpit_account_label(
            account_id,
            str(account.get("email") or ""),
            str(account.get("api_provider_name") or account.get("name") or ""),
        )
        for account_id, account in all_accounts_by_id.items()
    }
    official_cache = load_official_quota_cache()
    retained_quota = cached_official_quota_by_label(
        official_cache,
        set(all_accounts_by_id),
    )

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
            if label not in result:
                result[label] = local_quota
            else:
                result[label] = merge_quota_rows(result[label], local_quota)

    backup_quota = cockpit_backup_quota_by_label(home)
    for label, quota in backup_quota.items():
        if label in result:
            result[label] = merge_quota_rows(result[label], quota)
        else:
            result[label] = quota

    # Resolve every Cockpit-owned local source before using account credentials
    # for a direct official request. A future sidecar version can therefore add
    # reset timestamps without causing duplicate network traffic.
    # A fresh sidecar snapshot already is a refreshed inactive-account value.
    # Use official credentials only when that local source is incomplete or
    # stale; active accounts are added below on their shorter cadence.
    official_candidates = {
        account_id: account
        for account_id, account in all_accounts_by_id.items()
        if quota_row_needs_official_refresh(result.get(labels_by_id.get(account_id, "")))
    }
    active_account_ids: set[str] = set()
    if force_active_official_refresh:
        active_account_ids.update(cockpit_recent_usage_account_ids(home))
        current_auth = read_codex_home_official_auth(home)
        current_email = str((current_auth or {}).get("email") or "").strip().lower()
        if current_email:
            matched_current_ids = {
                account_id
                for account_id, account in all_accounts_by_id.items()
                if str(account.get("email") or "").strip().lower() == current_email
            }
            if not matched_current_ids:
                matched_current_ids = {
                    account_id
                    for account_id, entry in official_cache.items()
                    if isinstance(entry, dict)
                    and str(entry.get("email") or "").strip().lower() == current_email
                }
                for account_id in matched_current_ids:
                    entry = official_cache.get(account_id) or {}
                    all_accounts_by_id.setdefault(
                        account_id,
                        {
                            "id": account_id,
                            "email": current_email,
                            "plan_type": str(entry.get("plan_type") or ""),
                        },
                    )
                    labels_by_id[account_id] = cockpit_account_label(
                        account_id,
                        current_email,
                        "",
                    )
            if not matched_current_ids:
                current_account_id = str((current_auth or {}).get("account_id") or "").strip()
                if current_account_id:
                    direct_id = "codex_direct_" + hashlib.sha256(
                        current_account_id.encode("utf-8")
                    ).hexdigest()[:32]
                    all_accounts_by_id[direct_id] = {
                        "id": direct_id,
                        "email": current_email,
                        "plan_type": "",
                    }
                    labels_by_id[direct_id] = cockpit_account_label(
                        direct_id,
                        current_email,
                        "",
                    )
                    matched_current_ids.add(direct_id)
            active_account_ids.update(matched_current_ids)
        active_account_ids.intersection_update(all_accounts_by_id)
        official_candidates.update(
            {
                account_id: all_accounts_by_id[account_id]
                for account_id in active_account_ids
            }
        )

    for account_id, entry in official_cache.items():
        if account_id not in all_accounts_by_id or not isinstance(entry, dict):
            continue
        label = labels_by_id.get(account_id, "")
        quota = entry.get("quota")
        if not label or not isinstance(quota, dict):
            continue
        if not any(
            quota_window_source_rank(quota.get(window_key)) >= 2
            for window_key in ("window_5h", "window_7d", "window_cycle")
        ):
            continue
        if label in result:
            result[label] = merge_quota_rows(result[label], quota)
        else:
            result[label] = quota

    for label, quota in retained_quota.items():
        if label in result:
            result[label] = merge_quota_rows(result[label], quota)
        else:
            result[label] = quota
    official_by_id = cockpit_official_quota_by_account(
        home,
        official_candidates,
        active_account_ids=active_account_ids,
    )
    for account_id, quota in official_by_id.items():
        label = labels_by_id.get(account_id)
        if not label:
            continue
        if label in result:
            result[label] = merge_quota_rows(result[label], quota)
        else:
            result[label] = quota
    persist_quota_snapshots_by_account(
        all_accounts_by_id,
        labels_by_id,
        result,
    )
    return result


def quota_window_start(
    window: dict[str, Any],
    now: datetime,
    duration: timedelta,
) -> datetime | None:
    if not window.get("quota_available"):
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
    calculated_cost, _price_resolved = estimate_cost_with_resolution(
        model,
        max(0, input_tokens - cached_tokens),
        cached_tokens,
        output_tokens,
        pricing_tier="priority" if multiplier > 1 else "standard",
    )
    upstream_cost = max(0.0, cost * multiplier)
    effective_cost = calculated_cost if calculated_cost > 0 else upstream_cost
    bucket.cost += effective_cost
    if effective_cost <= 0:
        bucket.add_unpriced_model(model, event_total)
    bucket.add_model(model, event_total)
    bucket.mark_latest(ms_to_local_datetime(timestamp), model, app_speed, multiplier)
    return True


COCKPIT_RECORDED_USAGE_SQL = """(
    COALESCE(total_tokens, 0) > 0
    OR COALESCE(input_tokens, 0) > 0
    OR COALESCE(output_tokens, 0) > 0
    OR COALESCE(cached_tokens, 0) > 0
)"""


def scan_cockpit_codex_accounts(root: Path, start: datetime, end: datetime) -> dict[str, UsageBucket]:
    db_path = root / ".antigravity_cockpit" / "codex_local_access_logs.sqlite"
    if not db_path.exists():
        return {}
    start_ms = local_epoch_ms(start)
    end_ms = local_epoch_ms(end)
    speed_by_label = cockpit_codex_speed_by_label(root)
    labels_by_id = cockpit_codex_account_label_by_id(root)
    speed_markers = codex_speed_history(root, start, end)
    buckets: dict[str, UsageBucket] = {}
    try:
        con = connect_cockpit_sqlite_readonly(db_path)
        rows = con.execute(
            f"""
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
              AND {COCKPIT_RECORDED_USAGE_SQL}
            """,
            (start_ms, end_ms),
        ).fetchall()
        con.close()
    except sqlite3.Error as exc:
        warn_cockpit_sqlite_error("scan_cockpit_codex_accounts", exc)
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
        label = cockpit_account_label_with_manifest(
            account_id,
            email,
            api_key_label,
            labels_by_id,
        )
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
    labels_by_id = cockpit_codex_account_label_by_id(root)
    speed_markers = codex_speed_history(root, query_start, end)
    buckets_5h = {label: UsageBucket() for label in starts_5h}
    buckets_7d = {label: UsageBucket() for label in starts_7d}
    buckets_cycle = {label: UsageBucket() for label in starts_cycle}
    latest_by_label: dict[str, datetime] = {}
    try:
        con = connect_cockpit_sqlite_readonly(db_path)
        rows = con.execute(
            f"""
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
              AND {COCKPIT_RECORDED_USAGE_SQL}
            """,
            (local_epoch_ms(query_start), local_epoch_ms(end)),
        ).fetchall()
        con.close()
    except sqlite3.Error as exc:
        warn_cockpit_sqlite_error("scan_cockpit_codex_quota_windows", exc)
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
        label = cockpit_account_label_with_manifest(
            account_id,
            email,
            api_key_label,
            labels_by_id,
        )
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
        con = connect_cockpit_sqlite_readonly(db_path)
        columns = {
            str(row[1])
            for row in con.execute("PRAGMA table_info(request_logs)").fetchall()
        }
        request_id_sql = "request_id" if "request_id" in columns else "''"
        latency_ms_sql = "latency_ms" if "latency_ms" in columns else "0"
        rows = con.execute(
            f"""
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
                event_key,
                {request_id_sql} AS request_id,
                {latency_ms_sql} AS latency_ms
            FROM request_logs
            WHERE timestamp >= ? AND timestamp < ?
              AND {COCKPIT_RECORDED_USAGE_SQL}
            ORDER BY timestamp ASC
            """,
            (start_ms, end_ms),
        ).fetchall()
        con.close()
    except sqlite3.Error as exc:
        warn_cockpit_sqlite_error("scan_cockpit_codex_account_markers", exc)
        return []

    labels_by_id = cockpit_codex_account_label_by_id(root)
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
        request_id,
        latency_ms,
    ) in rows:
        when = ms_to_local_datetime(timestamp)
        if when is None:
            continue
        input_token_count = max(0, int(input_tokens or 0))
        cached_token_count = max(0, int(cached_tokens or 0))
        output_token_count = max(0, int(output_tokens or 0))
        reported_total = max(
            max(0, int(total_tokens or 0)),
            input_token_count + output_token_count,
            cached_token_count + output_token_count,
        )
        if reported_total <= 0:
            continue
        label = cockpit_account_label_with_manifest(
            account_id,
            email,
            api_key_label,
            labels_by_id,
        )
        if label == "Codex local - Unknown":
            continue
        markers.append(
            AccountMarker(
                when=when,
                label=label,
                model=codex_model_name(str(model or "codex")),
                kind="request",
                total_tokens=reported_total,
                input_tokens=input_token_count,
                cached_tokens=cached_token_count,
                output_tokens=output_token_count,
                event_key=str(event_key or "").strip(),
                request_id=str(request_id or "").strip(),
                account_id=normalize_cockpit_auth_id(account_id),
                latency_ms=max(0, int(latency_ms or 0)),
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


def cockpit_auth_result_affinity_event(
    line: str,
    labels: dict[str, str],
) -> CockpitAffinityEvent | None:
    """Parse Cockpit's structured final account selection for one request."""
    json_start = line.find("{")
    if json_start < 0:
        return None
    timestamp = line[:json_start].strip().split(" ", 1)[0]
    when = parse_local_log_dt(timestamp)
    if when is None:
        return None
    try:
        payload = json.loads(line[json_start:])
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("type") != "auth_result":
        return None
    request_id = str(payload.get("requestId") or "").strip()
    account_id = normalize_cockpit_auth_id(
        payload.get("accountId") or payload.get("authId")
    )
    if not request_id or not account_id:
        return None
    succeeded = (
        payload.get("success") is True
        and payload.get("authAvailable") is not False
    )
    label = labels.get(account_id, "")
    if not usable_cockpit_account_label(label):
        payload_label = cockpit_account_label(
            account_id,
            str(payload.get("accountEmail") or ""),
            "",
        )
        if usable_cockpit_account_label(payload_label):
            label = payload_label
    return CockpitAffinityEvent(
        when=when,
        request_id=request_id,
        source="auth_result",
        account_id=account_id,
        label=label,
        action="auth result" if succeeded else "auth unavailable",
        confirmed=succeeded,
    )


def cockpit_affinity_event_failed(event: CockpitAffinityEvent) -> bool:
    return any(
        fragment in event.action
        for fragment in COCKPIT_FAILED_AFFINITY_ACTION_FRAGMENTS
    )


def cockpit_affinity_event_concrete_account_id(
    event: CockpitAffinityEvent,
) -> str:
    if cockpit_affinity_event_failed(event):
        return ""
    if not usable_cockpit_account_label(event.label):
        return ""
    if (
        not event.confirmed
        and event.action not in COCKPIT_STABLE_NATIVE_AFFINITY_ACTIONS
    ):
        return ""
    return normalize_cockpit_auth_id(event.account_id)


def cockpit_affinity_segment_account_ids(
    segment: CockpitAffinitySegment,
) -> set[str]:
    return {
        account_id
        for event in segment.events
        if (account_id := cockpit_affinity_event_concrete_account_id(event))
    }


def cockpit_affinity_segment_confirmed_account_ids(
    segment: CockpitAffinitySegment,
) -> set[str]:
    return {
        account_id
        for event in segment.events
        if event.confirmed
        and usable_cockpit_account_label(event.label)
        and (account_id := normalize_cockpit_auth_id(event.account_id))
    }


def cockpit_affinity_segment_native_account_ids(
    segment: CockpitAffinitySegment,
) -> set[str]:
    return {
        account_id
        for event in segment.events
        if not cockpit_affinity_event_failed(event)
        and event.source.startswith("execution_session_id")
        and event.action in COCKPIT_STABLE_NATIVE_AFFINITY_ACTIONS
        and usable_cockpit_account_label(event.label)
        and (account_id := normalize_cockpit_auth_id(event.account_id))
    }


def cockpit_affinity_segment_route_is_trusted(
    segment: CockpitAffinitySegment,
    account_id: str,
) -> bool:
    normalized = normalize_cockpit_auth_id(account_id)
    if not normalized:
        return False
    if normalized in cockpit_affinity_segment_confirmed_account_ids(segment):
        return True
    if segment.failed:
        return False
    if segment.started_after_failure:
        return normalized in cockpit_affinity_segment_native_account_ids(segment)
    return cockpit_affinity_segment_account_ids(segment) == {normalized}


def cockpit_affinity_segments(
    events: list[CockpitAffinityEvent],
) -> dict[str, list[CockpitAffinitySegment]]:
    """Split reused Cockpit request ids at each account-selection boundary."""
    events_by_request: dict[str, list[CockpitAffinityEvent]] = {}
    for event in events:
        if event.request_id:
            events_by_request.setdefault(event.request_id, []).append(event)

    result: dict[str, list[CockpitAffinitySegment]] = {}
    for request_id, request_events in events_by_request.items():
        request_segments: list[CockpitAffinitySegment] = []
        current: CockpitAffinitySegment | None = None
        for event in sorted(
            request_events,
            key=lambda item: (item.when, item.action, item.account_id),
        ):
            failed = cockpit_affinity_event_failed(event)
            if failed:
                current_has_route = current is not None and any(
                    not cockpit_affinity_event_failed(item)
                    for item in current.events
                )
                if current_has_route:
                    current.failed = True
                    current.end_at = event.when
                    current = None
                if current is None:
                    current = CockpitAffinitySegment(
                        request_id=request_id,
                        index=len(request_segments),
                        start_at=event.when,
                        started_after_failure=True,
                    )
                    request_segments.append(current)
                current.events.append(event)
                continue

            event_account_id = cockpit_affinity_event_concrete_account_id(event)
            current_account_ids = (
                cockpit_affinity_segment_account_ids(current)
                if current is not None
                else set()
            )
            if (
                current is not None
                and event_account_id
                and current_account_ids
                and event_account_id not in current_account_ids
            ):
                current.end_at = event.when
                current = None
            if current is None:
                current = CockpitAffinitySegment(
                    request_id=request_id,
                    index=len(request_segments),
                    start_at=event.when,
                )
                request_segments.append(current)
            current.events.append(event)

        for index, segment in enumerate(request_segments[:-1]):
            if segment.end_at is None:
                segment.end_at = request_segments[index + 1].start_at
        if request_segments:
            result[request_id] = request_segments
    return result


def cockpit_affinity_segment_by_event_id(
    segments_by_request: dict[str, list[CockpitAffinitySegment]],
) -> dict[int, CockpitAffinitySegment]:
    return {
        id(event): segment
        for request_segments in segments_by_request.values()
        for segment in request_segments
        for event in segment.events
    }


def cockpit_marker_segment_score(
    marker: AccountMarker,
    segment: CockpitAffinitySegment,
) -> tuple[int, float, float]:
    request_start = account_marker_request_start(marker)
    completion_inside = (
        marker.when >= segment.start_at
        and (segment.end_at is None or marker.when < segment.end_at)
    )
    request_overlaps = (
        request_start is not None
        and marker.when >= segment.start_at
        and (segment.end_at is None or request_start < segment.end_at)
    )
    distances = [abs((marker.when - event.when).total_seconds()) for event in segment.events]
    if request_start is not None:
        distances.extend(
            abs((request_start - event.when).total_seconds())
            for event in segment.events
        )
    distance = min(distances, default=float("inf"))
    return (
        0 if completion_inside else 1 if request_overlaps else 2,
        distance,
        -account_marker_epoch(segment.start_at),
    )


def cockpit_account_markers_by_segment(
    account_markers: list[AccountMarker],
    segments_by_request: dict[str, list[CockpitAffinitySegment]],
) -> dict[tuple[str, int], list[AccountMarker]]:
    """Bind final usage rows only to their compatible auth segment."""
    result: dict[tuple[str, int], list[AccountMarker]] = {}
    for marker in account_markers:
        if marker.kind != "request" or not marker.request_id:
            continue
        request_segments = segments_by_request.get(marker.request_id, [])
        if not request_segments:
            continue
        marker_account_id = normalize_cockpit_auth_id(marker.account_id)
        strong_candidates = [
            segment
            for segment in request_segments
            if marker_account_id
            and marker_account_id in cockpit_affinity_segment_account_ids(segment)
        ]
        weak_candidates = [
            segment
            for segment in request_segments
            if marker_account_id
            and any(
                cockpit_affinity_event_failed(event)
                and normalize_cockpit_auth_id(event.account_id) == marker_account_id
                for event in segment.events
            )
            and not cockpit_affinity_segment_account_ids(segment)
        ]
        unknown_candidates = [
            segment
            for segment in request_segments
            if not cockpit_affinity_segment_account_ids(segment)
        ]
        candidates = strong_candidates or weak_candidates
        if not candidates and len(request_segments) == 1:
            only_segment = request_segments[0]
            if (
                not marker_account_id
                or not cockpit_affinity_segment_account_ids(only_segment)
            ):
                candidates = request_segments
        if not candidates and len(unknown_candidates) == 1:
            candidates = unknown_candidates
        if not candidates:
            continue
        segment = min(
            candidates,
            key=lambda item: cockpit_marker_segment_score(marker, item),
        )
        result.setdefault(segment.key, []).append(marker)
    for markers in result.values():
        markers.sort(key=lambda marker: marker.when)
    return result


def enrich_cockpit_affinity_from_auth_results(
    events: list[CockpitAffinityEvent],
) -> None:
    """Attach one segment's confirmed account to its opaque route hits."""
    for request_segments in cockpit_affinity_segments(events).values():
        for segment in request_segments:
            confirmed_accounts = {
                (event.account_id, event.label)
                for event in segment.events
                if event.action == "auth result"
                and event.confirmed
                and usable_cockpit_account_label(event.label)
            }
            if len(confirmed_accounts) != 1:
                continue
            account_id, label = next(iter(confirmed_accounts))
            for event in segment.events:
                if (
                    event.action in COCKPIT_STABLE_NATIVE_AFFINITY_ACTIONS
                    and not usable_cockpit_account_label(event.label)
                ):
                    event.account_id = account_id
                    event.label = label
                    event.confirmed = True


def scan_cockpit_codex_affinity_events(
    root: Path,
    start: datetime,
    end: datetime,
    request_markers: list[AccountMarker] | None = None,
) -> list[CockpitAffinityEvent]:
    logs_dir = root / ".antigravity_cockpit" / "logs"
    if not logs_dir.exists():
        return []
    labels = cockpit_affinity_account_label_by_id(root, request_markers)
    scan_start = start - timedelta(seconds=COCKPIT_AFFINITY_TURN_MATCH_SECONDS)
    events: list[CockpitAffinityEvent] = []
    seen: set[tuple[datetime, str, str, str, str]] = set()
    for path in sorted(logs_dir.glob("codex-api.log*")):
        dated_suffix = re.search(r"(\d{4}-\d{2}-\d{2})$", path.name)
        if dated_suffix is not None:
            try:
                log_day = datetime.fromisoformat(dated_suffix.group(1)).date()
            except ValueError:
                log_day = None
            if log_day is not None and not (
                scan_start.date() - timedelta(days=1) <= log_day <= end.date()
            ):
                continue
        try:
            modified = datetime.fromtimestamp(path.stat().st_mtime)
        except OSError:
            continue
        if modified < scan_start - timedelta(days=1):
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        for line in lines:
            auth_result = cockpit_auth_result_affinity_event(line, labels)
            if auth_result is not None:
                if scan_start <= auth_result.when < end:
                    identity = (
                        auth_result.when,
                        auth_result.request_id,
                        auth_result.action,
                        auth_result.account_id,
                        auth_result.session_key,
                    )
                    if identity not in seen:
                        seen.add(identity)
                        events.append(auth_result)
                continue
            match = COCKPIT_AFFINITY_LINE_RE.search(line)
            if match is None:
                continue
            when = parse_local_log_dt(match.group("timestamp"))
            if when is None or when < scan_start or when >= end:
                continue
            fields = {
                item.group("key"): item.group("value").strip().strip('"')
                for item in LOG_FIELD_RE.finditer(match.group("fields"))
            }
            request_id = match.group("request_id").strip().strip('"')
            account_id = normalize_cockpit_auth_id(fields.get("auth"))
            if not request_id or not account_id:
                continue
            action = " ".join(match.group("action").strip().lower().split())
            source = str(fields.get("source") or "").strip()
            session_key = str(fields.get("session") or "").strip()
            identity = (when, request_id, action, account_id, session_key)
            if identity in seen:
                continue
            seen.add(identity)
            events.append(
                CockpitAffinityEvent(
                    when=when,
                    request_id=request_id,
                    source=source,
                    session_key=session_key,
                    account_id=account_id,
                    label=labels.get(account_id, ""),
                    action=action,
                    confirmed=action in COCKPIT_CONFIRMED_AFFINITY_ACTIONS,
                )
            )
    events.sort(key=lambda item: (item.when, item.request_id, item.action))
    enrich_cockpit_affinity_from_auth_results(events)
    return events


_COCKPIT_SWITCH_LOG_CACHE: dict[tuple[str, int, int], list[tuple[datetime, str]]] = {}


def cockpit_switch_log_entries(path: Path) -> list[tuple[datetime, str]]:
    """Switch lines only depend on file content, so parse each log once per run."""
    key: tuple[str, int, int] | None
    try:
        stat = path.stat()
        key = (os.path.normcase(str(path)), int(stat.st_mtime_ns), int(stat.st_size))
    except OSError:
        key = None
    if key is not None:
        cached = _COCKPIT_SWITCH_LOG_CACHE.get(key)
        if cached is not None:
            return cached
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return []
    entries: list[tuple[datetime, str]] = []
    for line in lines:
        match = SWITCH_LOG_RE.search(line)
        if not match:
            continue
        when = parse_local_log_dt(match.group("ts"))
        if when is None:
            continue
        entries.append((when, match.group("account_id").strip()))
    if key is not None:
        _COCKPIT_SWITCH_LOG_CACHE[key] = entries
    return entries


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
        for when, account_id in cockpit_switch_log_entries(path):
            if when >= end:
                continue
            label = labels.get(account_id) or cockpit_account_label(account_id, "", "")
            if not usable_cockpit_account_label(label):
                continue
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
    verdicts: dict[str, dict[str, str]] | None = None,
) -> dict[str, UsageBucket]:
    attributed = attribute_codex_events_by_account(
        events,
        markers,
        attribution_ledger,
        current_label,
        now,
    )
    request_markers = [
        marker
        for marker in markers
        if marker.kind == "request" and marker.total_tokens > 0
    ]
    if request_markers:
        attributed, _session_accounts, _unresolved = resolve_api_service_event_accounts(
            attributed,
            request_markers,
            None,
            None,
            verdicts,
            record_verdicts=False,
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


def quota_fingerprint_account_candidates(
    events: list[UsageEvent],
    markers: list[AccountMarker],
) -> dict[tuple[int, int], set[str]]:
    candidates: dict[tuple[int, int], set[str]] = {}
    for account_id, entry in load_official_quota_cache().items():
        if not isinstance(entry, dict):
            continue
        label = str(entry.get("label") or "").strip()
        if not label.startswith("Codex local - "):
            label = cockpit_account_label(
                str(account_id),
                str(entry.get("email") or ""),
                label,
            )
        quota = entry.get("quota")
        if not label or not isinstance(quota, dict):
            continue
        for window in quota.values():
            fingerprint = quota_window_fingerprint(window)
            if fingerprint is not None:
                candidates.setdefault(fingerprint, set()).add(label)

    request_markers = [
        marker
        for marker in markers
        if marker.kind == "request" and account_marker_has_recorded_usage(marker)
    ]
    if not request_markers:
        return candidates
    marker_index = account_markers_by_total_tokens(request_markers)
    used_marker_ids: set[int] = set()
    for event in sorted(events, key=lambda item: item.when):
        if external_codex_provider_label(event.model):
            continue
        if not event.quota_fingerprints:
            continue
        marker, exact = concrete_api_service_account_match(
            event,
            request_markers,
            marker_index,
            used_marker_ids,
        )
        if marker is None or not exact or is_api_service_mirror_label(marker.label):
            continue
        used_marker_ids.add(id(marker))
        for fingerprint in event.quota_fingerprints:
            candidates.setdefault(fingerprint, set()).add(marker.label)
    return candidates


def apply_quota_fingerprint_account_hints(
    events: list[UsageEvent],
    markers: list[AccountMarker],
) -> None:
    candidates = quota_fingerprint_account_candidates(events, markers)
    unique_labels = {
        fingerprint: next(iter(labels))
        for fingerprint, labels in candidates.items()
        if len(labels) == 1
    }
    for event in events:
        if external_codex_provider_label(event.model):
            continue
        if event.account_hint_source in {
            OPENCODEX_ACCOUNT_HINT_SOURCE,
            OPENCODEX_UNRESOLVED_HINT_SOURCE,
        }:
            continue
        labels = {
            unique_labels[fingerprint]
            for fingerprint in event.quota_fingerprints
            if fingerprint in unique_labels
        }
        if len(labels) != 1:
            continue
        event.account_label_hint = next(iter(labels))
        event.account_hint_source = "quota_fingerprint"


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
    apply_quota_fingerprint_account_hints(events, markers)
    if not markers:
        for event in events:
            event_id = codex_event_id(event)
            label = assign_external_codex_provider_label(event, attribution_ledger)
            if label:
                attributed.setdefault(label, []).append(event)
                continue
            label = (
                event.account_label_hint
                if event.account_hint_source in TRUSTED_CODEX_ACCOUNT_HINT_SOURCES
                else ""
            )
            if event.account_hint_source == OPENCODEX_UNRESOLVED_HINT_SOURCE:
                label = API_SERVICE_AGGREGATE_LABEL
            if (
                label
                and attribution_ledger is not None
                and event.account_hint_source != OPENCODEX_TURN_HINT_SOURCE
            ):
                ledger_assign(attribution_ledger, event_id, label)
            if not label:
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
                    ledger_assign(attribution_ledger, event_id, label)
            attributed.setdefault(label, []).append(event)
        return attributed

    markers = sorted(markers, key=lambda marker: marker.when)
    switch_markers = [marker for marker in markers if marker.kind == "switch"]
    switch_times = [marker.when for marker in switch_markers]
    request_markers = [marker for marker in markers if marker.kind != "switch"]
    request_times = [marker.when for marker in request_markers]
    ledger = attribution_ledger
    for event in events:
        event_id = codex_event_id(event)
        label = assign_external_codex_provider_label(event, ledger)
        if label:
            attributed.setdefault(label, []).append(event)
            continue
        label = (
            event.account_label_hint
            if event.account_hint_source in TRUSTED_CODEX_ACCOUNT_HINT_SOURCES
            else ""
        )
        if event.account_hint_source == OPENCODEX_UNRESOLVED_HINT_SOURCE:
            label = API_SERVICE_AGGREGATE_LABEL
        if (
            label
            and ledger is not None
            and event.account_hint_source != OPENCODEX_TURN_HINT_SOURCE
        ):
            ledger_assign(ledger, event_id, label)
        if not label:
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
                ledger_assign(ledger, event_id, label)
        attributed.setdefault(label, []).append(event)
    return attributed


def claude_usage_int(usage: dict[str, Any], key: str) -> int:
    try:
        return max(0, int(usage.get(key) or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def iter_recent_claude_jsonl(root: Path, start: datetime) -> list[Path]:
    if not root.exists():
        return []
    threshold = start - timedelta(hours=2)
    paths: list[Path] = []
    seen: set[Path] = set()
    try:
        candidates = root.rglob("*.jsonl")
        for path in candidates:
            try:
                resolved = path.resolve()
                stat = resolved.stat()
            except OSError:
                continue
            parts = {part.lower() for part in resolved.parts}
            if any(part.startswith("backup-") for part in parts) or ".tmp" in parts:
                continue
            if resolved in seen or datetime.fromtimestamp(stat.st_mtime) < threshold:
                continue
            seen.add(resolved)
            paths.append(resolved)
    except OSError:
        return []
    return sorted(paths, key=lambda path: str(path).lower())


def claude_event_from_row(row: dict[str, Any], fallback_id: str) -> ClaudeUsageEvent | None:
    message = row.get("message")
    if not isinstance(message, dict) or message.get("role") != "assistant":
        return None
    usage = message.get("usage")
    if not isinstance(usage, dict) or not usage:
        return None
    when = parse_dt(row.get("timestamp"))
    if when is None:
        return None

    input_tokens = claude_usage_int(usage, "input_tokens")
    output_tokens = claude_usage_int(usage, "output_tokens")
    cache_creation = claude_usage_int(usage, "cache_creation_input_tokens")
    if cache_creation <= 0 and isinstance(usage.get("cache_creation"), dict):
        cache_creation = sum(
            claude_usage_int(usage["cache_creation"], key)
            for key in ("ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens")
        )
    cache_read = claude_usage_int(usage, "cache_read_input_tokens")
    total = input_tokens + output_tokens + cache_creation + cache_read
    if total <= 0 or total > MAX_SINGLE_EVENT_TOKENS:
        return None

    message_id = str(message.get("id") or "").strip()
    row_id = str(row.get("uuid") or "").strip()
    session_id = str(row.get("sessionId") or "").strip()
    if message_id:
        event_id = f"message:{message_id}"
    elif row_id:
        event_id = f"row:{session_id}:{row_id}"
    else:
        event_id = f"line:{fallback_id}"
    model = str(message.get("model") or row.get("model") or "claude").strip() or "claude"
    pricing_tier = normalize_pricing_tier(
        usage.get("service_tier") or usage.get("speed") or "standard"
    )
    return ClaudeUsageEvent(
        event_id=event_id,
        when=when,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_tokens=cache_creation,
        cache_read_tokens=cache_read,
        pricing_tier=pricing_tier,
    )


def claude_event_snapshot_score(event: ClaudeUsageEvent) -> tuple[int, int, datetime]:
    populated_components = sum(
        value > 0
        for value in (
            event.input_tokens,
            event.output_tokens,
            event.cache_creation_tokens,
            event.cache_read_tokens,
        )
    )
    return event.total_tokens, populated_components, event.when


def scan_claude_events(root: Path, start: datetime, end: datetime) -> list[ClaudeUsageEvent]:
    # Claude Code may persist one API response once per thinking/text/tool block.
    grouped: dict[str, tuple[datetime, ClaudeUsageEvent]] = {}
    for path in iter_recent_claude_jsonl(root, start):
        try:
            handle = path.open("r", encoding="utf-8", errors="ignore")
        except OSError:
            continue
        with handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    continue
                if not isinstance(row, dict):
                    continue
                message = row.get("message")
                needs_fallback_id = (
                    isinstance(message, dict)
                    and message.get("role") == "assistant"
                    and not str(message.get("id") or "").strip()
                    and not str(row.get("uuid") or "").strip()
                )
                fallback_id = (
                    hashlib.sha256(line.encode("utf-8", errors="ignore")).hexdigest()
                    if needs_fallback_id
                    else ""
                )
                event = claude_event_from_row(row, fallback_id)
                if event is None:
                    continue
                existing = grouped.get(event.event_id)
                if existing is None:
                    grouped[event.event_id] = (event.when, event)
                    continue
                first_when, best = existing
                if claude_event_snapshot_score(event) > claude_event_snapshot_score(best):
                    best = event
                grouped[event.event_id] = (min(first_when, event.when), best)

    events: list[ClaudeUsageEvent] = []
    for first_when, best in grouped.values():
        if first_when < start or first_when >= end:
            continue
        events.append(
            ClaudeUsageEvent(
                event_id=best.event_id,
                when=first_when,
                model=best.model,
                input_tokens=best.input_tokens,
                output_tokens=best.output_tokens,
                cache_creation_tokens=best.cache_creation_tokens,
                cache_read_tokens=best.cache_read_tokens,
                pricing_tier=best.pricing_tier,
            )
        )
    events.sort(key=lambda event: (event.when, event.event_id))
    return events


def add_claude_event_to_bucket(bucket: UsageBucket, event: ClaudeUsageEvent) -> None:
    bucket.requests += 1
    bucket.input_tokens += event.input_tokens
    bucket.output_tokens += event.output_tokens
    bucket.cache_creation_input_tokens += event.cache_creation_tokens
    bucket.cache_read_input_tokens += event.cache_read_tokens
    cost, price_resolved = estimate_cost_with_resolution(
        event.model,
        event.input_tokens,
        event.cache_read_tokens,
        event.output_tokens,
        cache_creation_tokens=event.cache_creation_tokens,
        pricing_tier=event.pricing_tier,
        when=event.when,
    )
    bucket.cost += cost
    if not price_resolved:
        bucket.add_unpriced_model(event.model, event.total_tokens)
    bucket.add_model(event.model, event.total_tokens)
    bucket.mark_latest(event.when, event.model)


def bucket_from_claude_events(events: list[ClaudeUsageEvent]) -> UsageBucket:
    bucket = UsageBucket()
    for event in events:
        add_claude_event_to_bucket(bucket, event)
    return bucket


def scan_claude(root: Path, start: datetime, end: datetime) -> UsageBucket:
    return bucket_from_claude_events(scan_claude_events(root, start, end))


def claude_hourly_from_events(events: list[ClaudeUsageEvent]) -> list[dict[str, Any]]:
    buckets = [
        {"hour": hour, "requests": 0, "tokens": 0, "cost": 0.0}
        for hour in range(24)
    ]
    for event in events:
        bucket = buckets[max(0, min(23, event.when.hour))]
        bucket["requests"] += 1
        bucket["tokens"] += event.total_tokens
        bucket["cost"] += estimate_cost(
            event.model,
            event.input_tokens,
            event.cache_read_tokens,
            event.output_tokens,
            cache_creation_tokens=event.cache_creation_tokens,
            pricing_tier=event.pricing_tier,
            when=event.when,
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
            pricing_model=event.pricing_model,
            when=event.when,
        )
    for bucket in buckets:
        bucket["cost"] = round(float(bucket["cost"] or 0), 6)
    return buckets


def grok_update_timestamp(value: Any) -> datetime | None:
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=LOCAL_TZ).replace(tzinfo=None)
        except (OSError, OverflowError, ValueError):
            return None
    return parse_dt(value)


def split_grok_token_count(total: int, parts: int, index: int) -> int:
    total = max(0, int(total or 0))
    parts = max(1, int(parts or 1))
    quotient, remainder = divmod(total, parts)
    return quotient + (1 if 0 <= index < remainder else 0)


def grok_canonical_model_name(value: Any) -> str:
    name = str(value or "").strip().lower()
    if name.startswith("xai/"):
        name = name.split("/", 1)[1]
    return name


def grok_cli_default_model(home: Path | None = None) -> str:
    path = (home or Path.home()) / ".grok" / "config.toml"
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return ""
    current_section = ""
    for raw_line in lines:
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            current_section = line.strip("[]").strip().lower()
            continue
        match = re.match(r'^default\s*=\s*["\']([^"\']+)["\']', line)
        if match and current_section == "models":
            return str(match.group(1) or "").strip()
    return ""


def grok_local_pricing_model(
    model: str,
    *,
    default_model: str = "",
) -> str:
    usage_model = grok_canonical_model_name(model)
    if usage_model != GROK_BUILD_USAGE_MODEL:
        return ""
    canonical_default = grok_canonical_model_name(default_model)
    if canonical_default not in {grok_canonical_model_name(name) for name in GROK_CANONICAL_DEFAULT_MODELS}:
        return ""
    return GROK_CANONICAL_PRICING_MODEL


def grok_usage_events_from_update_row(
    row: dict[str, Any],
    fallback_session_id: str = "",
    *,
    default_model: str | None = None,
) -> list[UsageEvent]:
    params = row.get("params")
    if not isinstance(params, dict):
        return []
    update = params.get("update")
    if not isinstance(update, dict) or update.get("sessionUpdate") != "turn_completed":
        return []
    usage = update.get("usage")
    if not isinstance(usage, dict):
        return []
    when = grok_update_timestamp(row.get("timestamp"))
    if when is None:
        return []

    session_id = str(params.get("sessionId") or fallback_session_id or "").strip()
    prompt_id = str(update.get("prompt_id") or "").strip()
    if not prompt_id:
        identity_payload = {
            "session_id": session_id,
            "timestamp": row.get("timestamp"),
            "usage": usage,
        }
        prompt_id = "fallback-" + hashlib.sha256(
            json.dumps(
                identity_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8", errors="ignore")
        ).hexdigest()
    model_usage = usage.get("modelUsage")
    if not isinstance(model_usage, dict) or not model_usage:
        fallback_model = str(
            usage.get("model")
            or update.get("model")
            or update.get("model_id")
            or "grok"
        ).strip() or "grok"
        model_usage = {fallback_model: usage}

    events: list[UsageEvent] = []
    event_offset = 0
    grok_default_model = grok_cli_default_model() if default_model is None else str(default_model or "")
    for raw_model, raw_detail in sorted(model_usage.items(), key=lambda item: str(item[0])):
        if not isinstance(raw_detail, dict):
            continue
        model = str(raw_model or "grok").strip() or "grok"
        raw_input = max(0, usage_int(raw_detail, "inputTokens"))
        output_tokens = max(0, usage_int(raw_detail, "outputTokens"))
        total_tokens = max(0, usage_int(raw_detail, "totalTokens"))
        if total_tokens <= 0:
            total_tokens = raw_input + output_tokens
        if total_tokens <= 0:
            continue

        output_tokens = min(output_tokens, total_tokens)
        input_tokens = max(0, total_tokens - output_tokens)
        cache_read = max(0, usage_int(raw_detail, "cachedReadTokens"))
        cache_creation = max(0, usage_int(raw_detail, "cacheCreationTokens"))
        if cache_read <= 0 and cache_creation <= 0:
            cache_read = max(0, usage_int(raw_detail, "cachedTokens"))
        cached_tokens = min(input_tokens, cache_read + cache_creation)
        uncached_input = max(0, input_tokens - cached_tokens)
        model_calls = max(1, usage_int(raw_detail, "modelCalls"))
        pricing_model = grok_local_pricing_model(
            model,
            default_model=grok_default_model,
        )

        for call_index in range(model_calls):
            call_when = when + timedelta(microseconds=event_offset)
            event_offset += 1
            event = UsageEvent(
                when=call_when,
                model=model,
                input_tokens=split_grok_token_count(
                    uncached_input,
                    model_calls,
                    call_index,
                ),
                cached_tokens=split_grok_token_count(
                    cached_tokens,
                    model_calls,
                    call_index,
                ),
                output_tokens=split_grok_token_count(
                    output_tokens,
                    model_calls,
                    call_index,
                ),
                session_id=session_id,
                request_key=(
                    f"grok:{session_id}:{prompt_id}:{model}:{call_index}"
                ),
                route="grok-local",
                request_at=call_when,
                account_at=when,
                pricing_model=pricing_model,
            )
            if event.total_tokens > 0:
                events.append(event)
    return events


def iter_recent_grok_updates(root: Path, start: datetime) -> list[Path]:
    if not root.exists():
        return []
    earliest_mtime = start.replace(tzinfo=LOCAL_TZ).timestamp()
    paths: list[Path] = []
    try:
        candidates = root.rglob("updates.jsonl")
        for path in candidates:
            try:
                if path.stat().st_mtime < earliest_mtime:
                    continue
            except OSError:
                continue
            paths.append(path)
    except OSError:
        return []
    return sorted(paths, key=lambda path: str(path).casefold())


def scan_grok_events(root: Path, start: datetime, end: datetime) -> list[UsageEvent]:
    grouped: dict[str, tuple[datetime, UsageEvent]] = {}
    for path in iter_recent_grok_updates(root, start):
        try:
            handle = path.open("r", encoding="utf-8", errors="ignore")
        except OSError:
            continue
        fallback_session_id = path.parent.name
        with handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    continue
                if not isinstance(row, dict):
                    continue
                for event in grok_usage_events_from_update_row(row, fallback_session_id):
                    identity = event.request_key or live_usage_event_id(event)
                    existing = grouped.get(identity)
                    if existing is None:
                        grouped[identity] = (event.when, event)
                        continue
                    first_when, best = existing
                    if event.total_tokens > best.total_tokens:
                        best = event
                    grouped[identity] = (min(first_when, event.when), best)

    events: list[UsageEvent] = []
    for first_when, best in grouped.values():
        if first_when < start or first_when >= end:
            continue
        if best.when != first_when:
            best = replace(best, when=first_when, request_at=first_when)
        events.append(best)
    events.sort(key=lambda event: (event.when, event.request_key))
    return events


def bucket_from_grok_events(events: list[UsageEvent]) -> UsageBucket:
    return bucket_from_codex_events(events)


def scan_grok(root: Path, start: datetime, end: datetime) -> UsageBucket:
    return bucket_from_grok_events(scan_grok_events(root, start, end))


def scan_grok_daily_buckets(
    root: Path,
    start: datetime,
    end: datetime,
) -> dict[date, UsageBucket]:
    buckets: dict[date, UsageBucket] = {}
    for event in scan_grok_events(root, start, end):
        bucket = buckets.setdefault(event.when.date(), UsageBucket())
        add_codex_event_to_bucket(bucket, event)
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
    *,
    activity_events: list[UsageEvent],
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

    latest_activity_by_hour: dict[int, datetime] = {}
    for event in activity_events:
        if event.when.date() != day or event.when > as_of:
            continue
        event_hour = max(0, min(23, event.when.hour))
        previous_activity = latest_activity_by_hour.get(event_hour)
        if previous_activity is None or event.when > previous_activity:
            latest_activity_by_hour[event_hour] = event.when

    failures_by_hour: dict[int, list[CodexFailureEvent]] = {}
    for failure in failures:
        if failure.when.date() != day or failure.when > as_of:
            continue
        candidate_hour = failure.when.hour
        candidate = by_hour.get(candidate_hour)
        if candidate is None or candidate_hour > last_observed_hour:
            continue
        if (
            failure.kind == "desktop_network"
            and int(candidate.get("requests") or 0) <= 0
            and int(candidate.get("tokens") or 0) <= 0
        ):
            # Background network polling can fail while Codex is idle. Keep
            # that separate from token activity so an empty hour stays empty.
            continue
        failures_by_hour.setdefault(candidate_hour, []).append(failure)

    for candidate_hour, hour_failures in failures_by_hour.items():
        latest_failure = max(hour_failures, key=lambda failure: failure.when)
        latest_activity = latest_activity_by_hour.get(candidate_hour)
        if latest_activity is not None and latest_activity > latest_failure.when:
            continue
        candidate = by_hour[candidate_hour]
        candidate["failure"] = True
        candidate["failure_count"] = len(hour_failures)
        candidate["failure_at"] = latest_failure.when.replace(tzinfo=LOCAL_TZ).isoformat(
            timespec="seconds"
        )
        candidate["failure_kind"] = latest_failure.kind


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


def account_marker_epoch(value: datetime) -> float:
    aware = value if value.tzinfo is not None else value.replace(tzinfo=LOCAL_TZ)
    return aware.timestamp()


class AccountMarkerTokenIndex(dict[int, list[AccountMarker]]):
    def __init__(self, markers: list[AccountMarker]) -> None:
        super().__init__()
        timed: list[tuple[float, int, AccountMarker]] = []
        for position, marker in enumerate(markers):
            if marker.kind != "request" or not account_marker_has_recorded_usage(marker):
                continue
            timed.append((account_marker_epoch(marker.when), position, marker))
            if marker.total_tokens > 0:
                self.setdefault(marker.total_tokens, []).append(marker)
        timed.sort(key=lambda item: (item[0], item[1]))
        self.timed = timed
        self.times = [item[0] for item in timed]

    def near_time(self, when: datetime, seconds: float) -> list[AccountMarker]:
        center = account_marker_epoch(when)
        left = bisect_left(self.times, center - seconds)
        right = bisect_right(self.times, center + seconds)
        # Preserve the original marker order for exact tie behavior.
        return [
            item[2]
            for item in sorted(self.timed[left:right], key=lambda item: item[1])
        ]


def account_marker_has_recorded_usage(marker: AccountMarker) -> bool:
    return any(
        value > 0
        for value in (
            marker.total_tokens,
            marker.input_tokens,
            marker.cached_tokens,
            marker.output_tokens,
        )
    )


def account_markers_by_total_tokens(markers: list[AccountMarker]) -> dict[int, list[AccountMarker]]:
    indexed: dict[int, list[AccountMarker]] = AccountMarkerTokenIndex(markers)
    return indexed


def account_marker_covers_event_time(
    marker: AccountMarker,
    event_time: datetime,
) -> bool:
    delta_seconds = (marker.when - event_time).total_seconds()
    if abs(delta_seconds) <= API_SERVICE_ACTIVITY_MATCH_SECONDS:
        return True
    latency_seconds = max(0.0, float(marker.latency_ms or 0) / 1000.0)
    # Cockpit versions have stored request_logs.timestamp as either the
    # request start or the response completion time.  Exact token matches can
    # therefore sit on either side of the marker by up to the request latency.
    return latency_seconds > 0 and abs(delta_seconds) <= latency_seconds


def concrete_api_service_account_match(
    event: UsageEvent,
    markers: list[AccountMarker],
    marker_index: dict[int, list[AccountMarker]] | None = None,
    used_marker_ids: set[int] | None = None,
) -> tuple[AccountMarker | None, bool]:
    """Pair an event with its Cockpit usage row and report how it was matched.

    The second element is True only for an exact match (identical token totals
    inside the activity window). A fuzzy match is a token-count guess inside a
    30 second window, so callers that persist a verdict must treat it as weak
    evidence even though it still decides this run's label.
    """
    # Cockpit writes request markers when the response finishes, matching the
    # Codex token_count timestamp rather than the task-start attribution edge.
    event_time = event.when
    exact_candidates = (
        (marker_index or {}).get(event.total_tokens, [])
        if marker_index is not None
        else [
            marker
            for marker in markers
            if marker.kind == "request"
            and account_marker_has_recorded_usage(marker)
            and marker.total_tokens == event.total_tokens
        ]
    )
    if used_marker_ids:
        exact_candidates = [
            marker for marker in exact_candidates if id(marker) not in used_marker_ids
        ]
    nearby = [
        marker
        for marker in exact_candidates
        if account_marker_covers_event_time(marker, event_time)
    ]
    if nearby:
        return (
            min(nearby, key=lambda marker: abs((marker.when - event_time).total_seconds())),
            True,
        )
    fuzzy_token_delta = max(256, int(event.total_tokens * 0.005))
    fuzzy_pool = (
        marker_index.near_time(event_time, 30)
        if isinstance(marker_index, AccountMarkerTokenIndex)
        else markers
    )
    fuzzy_candidates = [
        marker
        for marker in fuzzy_pool
        if marker.kind == "request"
        and account_marker_has_recorded_usage(marker)
        and (not used_marker_ids or id(marker) not in used_marker_ids)
        and abs((marker.when - event_time).total_seconds()) <= 30
        and abs(marker.total_tokens - event.total_tokens) <= fuzzy_token_delta
    ]
    if fuzzy_candidates:
        return (
            min(
                fuzzy_candidates,
                key=lambda marker: (
                    abs(marker.total_tokens - event.total_tokens),
                    abs((marker.when - event_time).total_seconds()),
                ),
            ),
            False,
        )
    return None, False


def concrete_api_service_account_marker(
    event: UsageEvent,
    markers: list[AccountMarker],
    marker_index: dict[int, list[AccountMarker]] | None = None,
    used_marker_ids: set[int] | None = None,
) -> AccountMarker | None:
    marker, _exact = concrete_api_service_account_match(
        event,
        markers,
        marker_index,
        used_marker_ids,
    )
    return marker


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


def api_service_event_turn_key(event: UsageEvent) -> str:
    session_id = (event.session_id or event.request_key or "").strip()
    if not session_id:
        return ""
    turn_started_at = event.account_at
    if turn_started_at is None and event.request_at is not None and event.request_at != event.when:
        turn_started_at = event.request_at
    if turn_started_at is None:
        return ""
    return f"{session_id}|{account_marker_epoch(turn_started_at):.6f}"


def api_service_event_turn_start(event: UsageEvent) -> datetime | None:
    turn_started_at = event.account_at
    if turn_started_at is None and event.request_at is not None and event.request_at != event.when:
        turn_started_at = event.request_at
    return turn_started_at


def account_marker_request_start(marker: AccountMarker) -> datetime | None:
    latency_ms = max(0, int(marker.latency_ms or 0))
    if latency_ms <= 0:
        return None
    return marker.when - timedelta(milliseconds=latency_ms)




def reconcile_cockpit_request_usage_events(
    events: list[UsageEvent],
    account_markers: list[AccountMarker],
    affinity_events: list[CockpitAffinityEvent],
) -> list[UsageEvent]:
    """Preserve each distinct Codex ``last_token_usage`` event.

    A Cockpit request can span multiple model calls while Codex handles tool
    results.  Every token_count row advances ``total_token_usage`` by exactly
    that row's ``last_token_usage`` and is therefore independently billable.
    Cockpit request ids remain attribution evidence, but they must not collapse
    those model calls into the final response usage row.
    """
    del account_markers, affinity_events
    return events


def cockpit_request_start_turn_anchors(
    records: list[tuple[str, UsageEvent, str, str, AccountMarker | None]],
    account_markers: list[AccountMarker],
    affinity_events: list[CockpitAffinityEvent],
) -> dict[str, tuple[datetime, AccountMarker]]:
    """Join a turn to a completed Cockpit request by its measured start time."""
    if not records or not account_markers or not affinity_events:
        return {}

    turn_starts: dict[str, datetime] = {}
    events_by_turn: dict[str, list[UsageEvent]] = {}
    for _label, event, _session_id, turn_key, _marker in records:
        turn_start = api_service_event_turn_start(event)
        if not turn_key or turn_start is None:
            continue
        previous = turn_starts.get(turn_key)
        if previous is None or turn_start < previous:
            turn_starts[turn_key] = turn_start
        events_by_turn.setdefault(turn_key, []).append(event)
    if not turn_starts:
        return {}

    segments_by_request = cockpit_affinity_segments(affinity_events)
    markers_by_segment = cockpit_account_markers_by_segment(
        account_markers,
        segments_by_request,
    )

    ordered_turns = sorted(
        (account_marker_epoch(turn_start), turn_key, turn_start)
        for turn_key, turn_start in turn_starts.items()
    )
    turn_epochs = [item[0] for item in ordered_turns]
    match_seconds = COCKPIT_AFFINITY_TURN_MATCH_SECONDS
    ambiguity_seconds = COCKPIT_AFFINITY_TURN_AMBIGUITY_SECONDS
    candidates_by_turn: dict[str, list[tuple[float, AccountMarker]]] = {}

    for request_segments in segments_by_request.values():
        for segment in request_segments:
            request_affinity = [
                item
                for item in segment.events
                if not item.source.lower().startswith("prompt_cache_key")
            ]
            if not request_affinity:
                continue
            for marker in markers_by_segment.get(segment.key, []):
                request_start = account_marker_request_start(marker)
                if (
                    request_start is None
                    or not marker.model
                    or not usable_cockpit_account_label(marker.label)
                    or not account_marker_has_recorded_usage(marker)
                ):
                    continue
                center = account_marker_epoch(request_start)
                left = bisect_left(turn_epochs, center - match_seconds)
                right = bisect_right(turn_epochs, center + match_seconds)
                turn_candidates: list[tuple[float, str, datetime]] = []
                for epoch, turn_key, turn_start in ordered_turns[left:right]:
                    if not any(
                        abs((item.when - turn_start).total_seconds()) <= match_seconds
                        for item in request_affinity
                    ):
                        continue
                    events = events_by_turn.get(turn_key, [])
                    marker_model = codex_model_name(marker.model).lower()
                    if not any(
                        codex_model_name(event.model).lower() == marker_model
                        for event in events
                    ):
                        continue
                    if not any(
                        account_marker_covers_event_time(marker, event.when)
                        for event in events
                    ):
                        continue
                    turn_candidates.append(
                        (abs(epoch - center), turn_key, turn_start)
                    )

                turn_candidates.sort(key=lambda item: (item[0], item[1]))
                if not turn_candidates:
                    continue
                if (
                    len(turn_candidates) > 1
                    and turn_candidates[1][0] - turn_candidates[0][0]
                    < ambiguity_seconds
                ):
                    continue
                delta, turn_key, _turn_start = turn_candidates[0]
                candidates_by_turn.setdefault(turn_key, []).append((delta, marker))

    anchors: dict[str, tuple[datetime, AccountMarker]] = {}
    for turn_key, candidates in candidates_by_turn.items():
        candidates.sort(
            key=lambda item: (
                item[0],
                -account_marker_epoch(item[1].when),
                item[1].request_id,
            )
        )
        best_delta, best_marker = candidates[0]
        competing = [
            delta
            for delta, marker in candidates[1:]
            if marker.label != best_marker.label
        ]
        if competing and competing[0] - best_delta < ambiguity_seconds:
            continue
        anchors[turn_key] = (turn_starts[turn_key], best_marker)
    return anchors


def cockpit_near_time_turn_anchors(
    records: list[tuple[str, UsageEvent, str, str, AccountMarker | None]],
    marker_index: AccountMarkerTokenIndex,
    used_marker_ids: set[int],
) -> dict[str, list[tuple[datetime, AccountMarker]]]:
    """Match final Cockpit rows to turns when Codex and Cockpit token totals differ."""
    match_seconds = COCKPIT_AFFINITY_TURN_MATCH_SECONDS
    ambiguity_seconds = COCKPIT_AFFINITY_TURN_AMBIGUITY_SECONDS
    event_candidates: dict[int, list[tuple[float, AccountMarker]]] = {}
    marker_turn_distances: dict[int, tuple[AccountMarker, dict[str, float]]] = {}

    for record_index, (_label, event, _session_id, turn_key, matched_marker) in enumerate(records):
        if not turn_key or matched_marker is not None:
            continue
        for marker in marker_index.near_time(event.when, match_seconds):
            marker_id = id(marker)
            if marker_id in used_marker_ids or not account_marker_has_recorded_usage(marker):
                continue
            delta = abs((marker.when - event.when).total_seconds())
            event_candidates.setdefault(record_index, []).append((delta, marker))
            _stored_marker, distances = marker_turn_distances.setdefault(
                marker_id,
                (marker, {}),
            )
            previous = distances.get(turn_key)
            if previous is None or delta < previous:
                distances[turn_key] = delta

    marker_turns: dict[int, str] = {}
    for marker_id, (_marker, distances) in marker_turn_distances.items():
        candidates = sorted((delta, turn_key) for turn_key, delta in distances.items())
        if not candidates:
            continue
        if len(candidates) > 1 and candidates[1][0] - candidates[0][0] < ambiguity_seconds:
            continue
        marker_turns[marker_id] = candidates[0][1]

    claims_by_marker: dict[int, list[tuple[float, int, AccountMarker]]] = {}
    for record_index, candidates in event_candidates.items():
        turn_key = records[record_index][3]
        eligible = sorted(
            (
                (delta, marker)
                for delta, marker in candidates
                if marker_turns.get(id(marker)) == turn_key
            ),
            key=lambda item: item[0],
        )
        if not eligible:
            continue
        best_delta, best_marker = eligible[0]
        competing_labels = [
            delta
            for delta, marker in eligible[1:]
            if marker.label != best_marker.label
        ]
        if competing_labels and competing_labels[0] - best_delta < ambiguity_seconds:
            continue
        claims_by_marker.setdefault(id(best_marker), []).append(
            (best_delta, record_index, best_marker)
        )

    anchors: dict[str, list[tuple[datetime, AccountMarker]]] = {}
    for marker_id, claims in claims_by_marker.items():
        _delta, record_index, marker = min(claims, key=lambda item: (item[0], item[1]))
        if marker_id in used_marker_ids:
            continue
        used_marker_ids.add(marker_id)
        event = records[record_index][1]
        turn_key = records[record_index][3]
        anchors.setdefault(turn_key, []).append((event.when, marker))

    for turn_anchors in anchors.values():
        turn_anchors.sort(key=lambda item: item[0])
    return anchors


def cockpit_affinity_turn_anchors(
    events: list[UsageEvent],
    account_markers: list[AccountMarker],
    affinity_events: list[CockpitAffinityEvent],
) -> dict[str, AccountMarker]:
    if not events or not affinity_events:
        return {}

    turn_starts: dict[str, datetime] = {}
    for event in events:
        turn_key = api_service_event_turn_key(event)
        turn_start = api_service_event_turn_start(event)
        if turn_key and turn_start is not None:
            turn_starts[turn_key] = turn_start
    if not turn_starts:
        return {}

    ordered_turns = sorted(
        (account_marker_epoch(turn_start), turn_key, turn_start)
        for turn_key, turn_start in turn_starts.items()
    )
    turn_epochs = [item[0] for item in ordered_turns]
    segments_by_request = cockpit_affinity_segments(affinity_events)
    segment_by_event_id = cockpit_affinity_segment_by_event_id(segments_by_request)
    segments_by_key = {
        segment.key: segment
        for request_segments in segments_by_request.values()
        for segment in request_segments
    }
    markers_by_segment = cockpit_account_markers_by_segment(
        account_markers,
        segments_by_request,
    )
    request_turns: dict[tuple[str, int], dict[str, datetime]] = {}
    turn_request_sources: dict[
        str,
        dict[tuple[str, int], set[str]],
    ] = {}
    match_seconds = COCKPIT_AFFINITY_TURN_MATCH_SECONDS
    ambiguity_seconds = COCKPIT_AFFINITY_TURN_AMBIGUITY_SECONDS
    for affinity in affinity_events:
        segment = segment_by_event_id.get(id(affinity))
        if segment is None:
            continue
        center = account_marker_epoch(affinity.when)
        left = bisect_left(turn_epochs, center - match_seconds)
        right = bisect_right(turn_epochs, center + match_seconds)
        candidates_by_turn: dict[str, tuple[float, datetime]] = {}
        for epoch, turn_key, turn_start in ordered_turns[left:right]:
            delta = abs(epoch - center)
            previous = candidates_by_turn.get(turn_key)
            if previous is None or delta < previous[0]:
                candidates_by_turn[turn_key] = (delta, turn_start)
        candidates = sorted(
            (delta, turn_key, turn_start)
            for turn_key, (delta, turn_start) in candidates_by_turn.items()
        )
        if not candidates:
            continue
        if (
            len(candidates) > 1
            and candidates[1][0] - candidates[0][0] < ambiguity_seconds
        ):
            continue
        _delta, turn_key, turn_start = candidates[0]
        request_turns.setdefault(segment.key, {})[turn_key] = turn_start
        turn_request_sources.setdefault(turn_key, {}).setdefault(
            segment.key,
            set(),
        ).add(affinity.source)

    candidates_by_turn: dict[
        str,
        list[tuple[str, str, tuple[str, int], datetime]],
    ] = {}
    evidenced_requests_by_turn: dict[str, set[tuple[str, int]]] = {}
    for segment_key, matched_turns in request_turns.items():
        request_segment = segments_by_key.get(segment_key)
        if request_segment is None:
            continue
        trace = sorted(
            request_segment.events,
            key=lambda item: item.when,
        )
        if not trace:
            continue
        boundaries = sorted(
            (turn_start, turn_key)
            for turn_key, turn_start in matched_turns.items()
        )
        segment_markers = markers_by_segment.get(segment_key, [])
        for position, (turn_start, turn_key) in enumerate(boundaries):
            segment_start = turn_start - timedelta(seconds=match_seconds)
            segment_end = (
                boundaries[position + 1][0] - timedelta(seconds=match_seconds)
                if position + 1 < len(boundaries)
                else None
            )
            segment = [
                item
                for item in trace
                if item.when >= segment_start
                and (segment_end is None or item.when < segment_end)
            ]
            if not segment:
                continue
            segment_account_ids = {
                normalize_cockpit_auth_id(item.account_id)
                for item in segment
                if normalize_cockpit_auth_id(item.account_id)
            }
            failure_times = [
                item.when
                for item in segment
                if any(
                    fragment in item.action
                    for fragment in COCKPIT_FAILED_AFFINITY_ACTION_FRAGMENTS
                )
            ]
            last_failure_at = max(failure_times) if failure_times else None
            confirmed_items = [
                item
                for item in segment
                if item.confirmed
                and usable_cockpit_account_label(item.label)
                and (last_failure_at is None or item.when > last_failure_at)
            ]
            confirmed_labels = {item.label for item in confirmed_items}
            label = ""
            evidence_kind = ""
            evidence_when = turn_start
            final_marker = (
                min(
                    segment_markers,
                    key=lambda marker: abs(
                        (marker.when - turn_start).total_seconds()
                    ),
                )
                if segment_markers
                else None
            )
            if final_marker is not None and usable_cockpit_account_label(
                final_marker.label
            ):
                label = final_marker.label
                evidence_kind = "final"
            elif len(confirmed_labels) == 1:
                label = next(iter(confirmed_labels))
                evidence_kind = "confirmed"
                evidence_when = min(item.when for item in confirmed_items)
            else:
                stable_native_items = [
                    item
                    for item in segment
                    if item.source.startswith("execution_session_id")
                    and item.action in COCKPIT_STABLE_NATIVE_AFFINITY_ACTIONS
                    and usable_cockpit_account_label(item.label)
                    and (last_failure_at is None or item.when > last_failure_at)
                ]
                stable_native_labels = {item.label for item in stable_native_items}
                stable_native_account_ids = {
                    normalize_cockpit_auth_id(item.account_id)
                    for item in stable_native_items
                    if normalize_cockpit_auth_id(item.account_id)
                }
                if (
                    len(stable_native_account_ids) == 1
                    and len(stable_native_labels) == 1
                ):
                    label = next(iter(stable_native_labels))
                    evidence_kind = "stable-native"
                    evidence_when = min(item.when for item in stable_native_items)
            if not usable_cockpit_account_label(label):
                continue
            candidates_by_turn.setdefault(turn_key, []).append(
                (label, evidence_kind, segment_key, evidence_when)
            )
            evidenced_requests_by_turn.setdefault(turn_key, set()).add(segment_key)

    anchors: dict[str, AccountMarker] = {}
    for turn_key, candidates in candidates_by_turn.items():
        sources_by_request = turn_request_sources.get(turn_key, {})
        native_requests = {
            request_id
            for request_id, sources in sources_by_request.items()
            if any(source.startswith("execution_session_id") for source in sources)
        }
        expected_requests = native_requests or set(sources_by_request)
        expected_requests = {
            segment_key
            for segment_key in expected_requests
            if not (
                segments_by_key.get(segment_key) is not None
                and segments_by_key[segment_key].failed
                and not cockpit_affinity_segment_confirmed_account_ids(
                    segments_by_key[segment_key]
                )
                and not markers_by_segment.get(segment_key)
            )
        }
        evidenced_requests = evidenced_requests_by_turn.get(turn_key, set()) & expected_requests
        selected_candidates = [
            candidate
            for candidate in candidates
            if candidate[2] in expected_requests
        ]
        labels = {
            label
            for label, _kind, _request_id, _evidence_when in selected_candidates
        }
        if (
            not expected_requests
            or evidenced_requests != expected_requests
            or len(labels) != 1
        ):
            continue
        label = next(iter(labels))
        anchors[turn_key] = AccountMarker(
            when=min(candidate[3] for candidate in selected_candidates),
            label=label,
            kind="affinity",
        )
    return anchors


def cockpit_nearest_turn_start_affinity_anchors(
    events: list[UsageEvent],
    account_markers: list[AccountMarker],
    affinity_events: list[CockpitAffinityEvent],
) -> dict[str, AccountMarker]:
    """Use a clearly separated stable affinity hit at the Codex turn edge."""
    if not events or not affinity_events:
        return {}

    turn_starts: dict[str, datetime] = {}
    for event in events:
        turn_key = api_service_event_turn_key(event)
        turn_start = api_service_event_turn_start(event)
        if not turn_key or turn_start is None:
            continue
        previous = turn_starts.get(turn_key)
        if previous is None or turn_start < previous:
            turn_starts[turn_key] = turn_start
    if not turn_starts:
        return {}

    ordered_turns = sorted(
        (account_marker_epoch(turn_start), turn_key, turn_start)
        for turn_key, turn_start in turn_starts.items()
    )
    turn_epochs = [item[0] for item in ordered_turns]
    segments_by_request = cockpit_affinity_segments(affinity_events)
    segment_by_event_id = cockpit_affinity_segment_by_event_id(segments_by_request)
    markers_by_segment = cockpit_account_markers_by_segment(
        account_markers,
        segments_by_request,
    )

    match_seconds = COCKPIT_FINAL_TURN_START_MATCH_SECONDS
    ambiguity_seconds = COCKPIT_AFFINITY_TURN_AMBIGUITY_SECONDS
    candidates_by_turn: dict[
        str,
        list[tuple[int, float, str, str, str]],
    ] = {}
    for item in affinity_events:
        account_id = normalize_cockpit_auth_id(item.account_id)
        segment = segment_by_event_id.get(id(item))
        if (
            segment is None
            or item.action not in COCKPIT_STABLE_NATIVE_AFFINITY_ACTIONS
            or not usable_cockpit_account_label(item.label)
            or not account_id
            or (
                item.source
                and not item.source.startswith("execution_session_id")
            )
        ):
            continue

        validating_markers = [
            marker
            for marker in markers_by_segment.get(segment.key, [])
            if normalize_cockpit_auth_id(marker.account_id) == account_id
            and -COCKPIT_FINAL_TURN_START_MATCH_SECONDS
            <= (marker.when - item.when).total_seconds()
            <= API_SERVICE_ACTIVITY_MATCH_SECONDS
        ]
        if (
            not validating_markers
            and not cockpit_affinity_segment_route_is_trusted(segment, account_id)
        ):
            continue

        center = account_marker_epoch(item.when)
        left = bisect_left(turn_epochs, center - match_seconds)
        right = bisect_right(turn_epochs, center + match_seconds)
        turn_candidates = sorted(
            (abs(epoch - center), turn_key, turn_start)
            for epoch, turn_key, turn_start in ordered_turns[left:right]
        )
        if not turn_candidates:
            continue
        if (
            len(turn_candidates) > 1
            and turn_candidates[1][0] - turn_candidates[0][0] < ambiguity_seconds
        ):
            continue
        delta, turn_key, _turn_start = turn_candidates[0]

        label = item.label
        strength = 2 if item.confirmed else 0
        if validating_markers:
            final_marker = min(
                validating_markers,
                key=lambda marker: abs((marker.when - item.when).total_seconds()),
            )
            if usable_cockpit_account_label(final_marker.label):
                label = final_marker.label
        if strength < 2 and item.source.startswith("execution_session_id"):
            strength = 1
        candidates_by_turn.setdefault(turn_key, []).append(
            (strength, delta, label, item.request_id, account_id)
        )

    anchors: dict[str, AccountMarker] = {}
    for turn_key, candidates in candidates_by_turn.items():
        strongest = max(candidate[0] for candidate in candidates)
        strongest_candidates = [
            candidate
            for candidate in candidates
            if candidate[0] == strongest
        ]
        closest_by_account: dict[
            str,
            tuple[float, str, str],
        ] = {}
        for _strength, delta, label, request_id, account_id in strongest_candidates:
            previous = closest_by_account.get(account_id)
            if previous is None or delta < previous[0]:
                closest_by_account[account_id] = (delta, label, request_id)
        account_candidates = sorted(
            (delta, account_id, label, request_id)
            for account_id, (delta, label, request_id) in closest_by_account.items()
        )
        if not account_candidates:
            continue
        if (
            len(account_candidates) > 1
            and account_candidates[1][0] - account_candidates[0][0]
            < ambiguity_seconds
        ):
            continue
        _delta, account_id, label, request_id = account_candidates[0]
        anchors[turn_key] = AccountMarker(
            when=turn_starts[turn_key],
            label=label,
            kind="affinity",
            request_id=request_id,
            account_id=account_id,
        )
    return anchors


def cockpit_final_request_event_markers(
    records: list[tuple[str, UsageEvent, str, str, AccountMarker | None]],
    account_markers: list[AccountMarker],
    affinity_events: list[CockpitAffinityEvent],
) -> dict[int, AccountMarker]:
    """Join a turn-start affinity trace to its authoritative final usage row."""
    if not records or not account_markers or not affinity_events:
        return {}

    turn_starts: dict[str, datetime] = {}
    record_indexes_by_turn: dict[str, list[int]] = {}
    for record_index, (_label, event, _session_id, turn_key, _marker) in enumerate(records):
        turn_start = api_service_event_turn_start(event)
        if not turn_key or turn_start is None:
            continue
        previous = turn_starts.get(turn_key)
        if previous is None or turn_start < previous:
            turn_starts[turn_key] = turn_start
        record_indexes_by_turn.setdefault(turn_key, []).append(record_index)
    if not turn_starts:
        return {}

    segments_by_request = cockpit_affinity_segments(affinity_events)
    markers_by_segment = cockpit_account_markers_by_segment(
        account_markers,
        segments_by_request,
    )
    event_match_seconds = COCKPIT_FINAL_TURN_START_MATCH_SECONDS
    ambiguity_seconds = COCKPIT_AFFINITY_TURN_AMBIGUITY_SECONDS
    segment_turns: dict[tuple[str, int], tuple[str, datetime]] = {}
    for request_segments in segments_by_request.values():
        for segment in request_segments:
            request_items = [
                item
                for item in segment.events
                if item.action in COCKPIT_STABLE_NATIVE_AFFINITY_ACTIONS
                and normalize_cockpit_auth_id(item.account_id)
            ]
            if not request_items:
                continue
            candidates_by_turn: dict[str, tuple[float, datetime]] = {}
            for turn_key, turn_start in turn_starts.items():
                delta = min(
                    abs((item.when - turn_start).total_seconds())
                    for item in request_items
                )
                if delta <= event_match_seconds:
                    candidates_by_turn[turn_key] = (delta, turn_start)
            candidates = sorted(
                (delta, turn_key, turn_start)
                for turn_key, (delta, turn_start) in candidates_by_turn.items()
            )
            if not candidates:
                continue
            if (
                len(candidates) > 1
                and candidates[1][0] - candidates[0][0] < ambiguity_seconds
            ):
                continue
            _delta, turn_key, turn_start = candidates[0]
            segment_turns[segment.key] = (turn_key, turn_start)

    proposals_by_marker: dict[
        int,
        list[tuple[float, int, AccountMarker]],
    ] = {}
    for request_segments in segments_by_request.values():
        for segment in request_segments:
            turn = segment_turns.get(segment.key)
            if turn is None:
                continue
            turn_key, _turn_start = turn
            for marker in markers_by_segment.get(segment.key, []):
                if (
                    not account_marker_has_recorded_usage(marker)
                    or not usable_cockpit_account_label(marker.label)
                ):
                    continue
                matching_records = [
                    record_index
                    for record_index in record_indexes_by_turn.get(turn_key, [])
                    if records[record_index][1].route != "cockpit-db-fallback"
                    and records[record_index][1].total_tokens == marker.total_tokens
                ]
                for record_index in matching_records:
                    event = records[record_index][1]
                    proposals_by_marker.setdefault(id(marker), []).append(
                        (
                            abs((marker.when - event.when).total_seconds()),
                            record_index,
                            marker,
                        )
                    )

    best_by_record: dict[int, list[tuple[float, AccountMarker]]] = {}
    for marker_proposals in proposals_by_marker.values():
        delta, record_index, marker = min(
            marker_proposals,
            key=lambda item: (item[0], -account_marker_epoch(item[2].when), item[1]),
        )
        best_by_record.setdefault(record_index, []).append((delta, marker))

    matched: dict[int, AccountMarker] = {}
    for record_index, candidates in best_by_record.items():
        candidates.sort(
            key=lambda item: (
                item[0],
                -account_marker_epoch(item[1].when),
                item[1].request_id,
            )
        )
        best_delta, best_marker = candidates[0]
        competing = [
            delta
            for delta, marker in candidates[1:]
            if marker.label != best_marker.label
        ]
        if competing and competing[0] - best_delta < ambiguity_seconds:
            continue
        matched[record_index] = best_marker
    return matched


def cockpit_confirmed_auth_result_event_markers(
    events: list[UsageEvent],
    affinity_events: list[CockpitAffinityEvent],
) -> dict[int, AccountMarker]:
    """Pair structured final auth results with one clearly nearest event."""
    if not events or not affinity_events:
        return {}
    timed_events = sorted(
        (
            account_marker_epoch(event.when),
            api_service_event_turn_key(event) or f"event:{id(event)}",
            event,
        )
        for event in events
    )
    event_epochs = [item[0] for item in timed_events]
    match_seconds = max(COCKPIT_AFFINITY_EVENT_MATCH_SECONDS, 0.35)
    ambiguity_seconds = min(
        COCKPIT_AFFINITY_TURN_AMBIGUITY_SECONDS,
        0.01,
    )
    candidates_by_event: dict[
        int,
        list[tuple[float, CockpitAffinityEvent, UsageEvent]],
    ] = {}
    for affinity in affinity_events:
        account_id = normalize_cockpit_auth_id(affinity.account_id)
        if (
            not affinity.confirmed
            or affinity.source != "auth_result"
            or affinity.action != "auth result"
            or not account_id
            or not usable_cockpit_account_label(affinity.label)
        ):
            continue
        center = account_marker_epoch(affinity.when)
        left = bisect_left(event_epochs, center - match_seconds)
        right = bisect_right(event_epochs, center + match_seconds)
        closest_by_turn: dict[str, tuple[float, UsageEvent]] = {}
        for event_epoch, turn_key, event in timed_events[left:right]:
            delta = abs(event_epoch - center)
            previous = closest_by_turn.get(turn_key)
            if previous is None or delta < previous[0]:
                closest_by_turn[turn_key] = (delta, event)
        turn_candidates = sorted(
            (delta, turn_key, event)
            for turn_key, (delta, event) in closest_by_turn.items()
        )
        if not turn_candidates:
            continue
        if (
            len(turn_candidates) > 1
            and turn_candidates[1][0] - turn_candidates[0][0]
            < ambiguity_seconds
        ):
            continue
        delta, _turn_key, event = turn_candidates[0]
        candidates_by_event.setdefault(id(event), []).append(
            (delta, affinity, event)
        )

    matched: dict[int, AccountMarker] = {}
    for event_id, candidates in candidates_by_event.items():
        candidates.sort(
            key=lambda item: (
                item[0],
                item[1].request_id,
                item[1].account_id,
            )
        )
        best_delta, best_affinity, event = candidates[0]
        competing = [
            delta
            for delta, affinity, _event in candidates[1:]
            if normalize_cockpit_auth_id(affinity.account_id)
            != normalize_cockpit_auth_id(best_affinity.account_id)
        ]
        if competing and competing[0] - best_delta < ambiguity_seconds:
            continue
        matched[event_id] = AccountMarker(
            when=event.when,
            label=best_affinity.label,
            kind="affinity-confirmed",
            request_id=best_affinity.request_id,
            account_id=normalize_cockpit_auth_id(best_affinity.account_id),
        )
    return matched


def cockpit_consistent_temporal_affinity_turn_anchors(
    events: list[UsageEvent],
    account_markers: list[AccountMarker],
    affinity_events: list[CockpitAffinityEvent],
) -> dict[str, list[tuple[datetime, AccountMarker]]]:
    """Build event-level account anchors from uniquely paired affinity traces."""
    if not events or not affinity_events:
        return {}

    events_by_turn: dict[str, list[UsageEvent]] = {}
    for event in events:
        turn_key = api_service_event_turn_key(event)
        if turn_key:
            events_by_turn.setdefault(turn_key, []).append(event)

    timed_events = sorted(
        (
            (
                account_marker_epoch(event.when),
                turn_key,
                event,
            )
            for turn_key, turn_events in events_by_turn.items()
            for event in turn_events
        ),
        key=lambda item: (item[0], item[1], id(item[2])),
    )
    event_epochs = [item[0] for item in timed_events]
    event_by_id = {id(event): event for _epoch, _turn_key, event in timed_events}
    segments_by_request = cockpit_affinity_segments(affinity_events)
    segment_by_event_id = cockpit_affinity_segment_by_event_id(segments_by_request)
    markers_by_segment = cockpit_account_markers_by_segment(
        account_markers,
        segments_by_request,
    )

    event_match_seconds = COCKPIT_AFFINITY_EVENT_MATCH_SECONDS
    route_items = [
        item
        for item in affinity_events
        if item.request_id
        and item.action in COCKPIT_STABLE_NATIVE_AFFINITY_ACTIONS
        and normalize_cockpit_auth_id(item.account_id)
    ]

    claims_by_item: dict[int, list[tuple[float, str, UsageEvent]]] = {}
    match_when_by_item: dict[int, datetime] = {}
    confirmed_by_item: dict[int, bool] = {}
    for item_index, item in enumerate(route_items):
        match_when = item.when
        segment = segment_by_event_id.get(id(item))
        account_id = normalize_cockpit_auth_id(item.account_id)
        route_is_confirmed = bool(
            item.confirmed
            or (
                segment is not None
                and account_id
                in cockpit_affinity_segment_confirmed_account_ids(segment)
            )
        )
        confirmed_by_item[item_index] = route_is_confirmed
        if segment is not None and account_id:
            confirmations = [
                candidate
                for candidate in segment.events
                if candidate.confirmed
                and normalize_cockpit_auth_id(candidate.account_id) == account_id
                and abs((candidate.when - item.when).total_seconds()) <= 1.0
            ]
            if confirmations:
                match_when = min(
                    confirmations,
                    key=lambda candidate: abs(
                        (candidate.when - item.when).total_seconds()
                    ),
                ).when
        match_when_by_item[item_index] = match_when
        center = account_marker_epoch(match_when)
        item_match_seconds = (
            max(event_match_seconds, 0.35)
            if route_is_confirmed
            else event_match_seconds
        )
        left = bisect_left(event_epochs, center - item_match_seconds)
        right = bisect_right(event_epochs, center + item_match_seconds)
        for event_epoch, turn_key, event in timed_events[left:right]:
            claims_by_item.setdefault(item_index, []).append(
                (abs(event_epoch - center), turn_key, event)
            )

    item_owner: dict[int, int] = {}
    turn_by_event_id = {
        id(event): turn_key
        for _event_epoch, turn_key, event in timed_events
    }
    ambiguity_seconds = COCKPIT_AFFINITY_TURN_AMBIGUITY_SECONDS
    for item_index, claims in claims_by_item.items():
        closest_by_turn: dict[str, tuple[float, UsageEvent]] = {}
        for delta, turn_key, event in claims:
            previous = closest_by_turn.get(turn_key)
            if previous is None or delta < previous[0]:
                closest_by_turn[turn_key] = (delta, event)
        turn_claims = sorted(
            (delta, turn_key, event)
            for turn_key, (delta, event) in closest_by_turn.items()
        )
        if not turn_claims:
            continue
        item_ambiguity_seconds = (
            min(ambiguity_seconds, 0.01)
            if confirmed_by_item.get(item_index, False)
            else ambiguity_seconds
        )
        if (
            len(turn_claims) > 1
            and turn_claims[1][0] - turn_claims[0][0]
            < item_ambiguity_seconds
        ):
            continue
        item_owner[item_index] = id(turn_claims[0][2])

    segment_turn_votes: dict[tuple[str, int], dict[str, int]] = {}
    for item_index, owner_id in item_owner.items():
        segment = segment_by_event_id.get(id(route_items[item_index]))
        turn_key = turn_by_event_id.get(owner_id, "")
        if segment is None or not turn_key:
            continue
        votes = segment_turn_votes.setdefault(segment.key, {})
        votes[turn_key] = votes.get(turn_key, 0) + 1

    preferred_turn_by_segment: dict[tuple[str, int], str] = {}
    for segment_key, votes in segment_turn_votes.items():
        ranked = sorted(
            ((count, turn_key) for turn_key, count in votes.items()),
            reverse=True,
        )
        if not ranked or ranked[0][0] < 2:
            continue
        runner_up = ranked[1][0] if len(ranked) > 1 else 0
        if ranked[0][0] - runner_up < 2:
            continue
        preferred_turn_by_segment[segment_key] = ranked[0][1]

    for item_index, claims in claims_by_item.items():
        segment = segment_by_event_id.get(id(route_items[item_index]))
        if segment is None:
            continue
        preferred_turn = preferred_turn_by_segment.get(segment.key, "")
        if not preferred_turn:
            continue
        preferred_claims = [
            (delta, event)
            for delta, turn_key, event in claims
            if turn_key == preferred_turn
        ]
        if preferred_claims:
            _delta, owner = min(preferred_claims, key=lambda item: item[0])
            item_owner[item_index] = id(owner)

    candidates_by_event: dict[
        int,
        list[tuple[float, str, str, str, bool]],
    ] = {}
    for item_index, item in enumerate(route_items):
        owner_id = item_owner.get(item_index)
        if owner_id is None:
            continue
        segment = segment_by_event_id.get(id(item))
        owner = event_by_id.get(owner_id)
        if segment is None or owner is None:
            continue
        segment_markers = [
            marker
            for marker in markers_by_segment.get(segment.key, [])
            if account_marker_has_recorded_usage(marker)
            and usable_cockpit_account_label(marker.label)
        ]
        marker_account_ids = {
            normalize_cockpit_auth_id(marker.account_id)
            or f"request:{segment.request_id}:{segment.index}"
            for marker in segment_markers
        }
        if segment_markers and len(marker_account_ids) == 1:
            final_marker = min(
                segment_markers,
                key=lambda marker: (
                    0 if marker.total_tokens == owner.total_tokens else 1,
                    0 if account_marker_covers_event_time(marker, owner.when) else 1,
                    abs((marker.when - owner.when).total_seconds()),
                ),
            )
            label = final_marker.label
            account_id = next(iter(marker_account_ids))
            evidence_confirmed = (
                account_id
                in cockpit_affinity_segment_confirmed_account_ids(segment)
            )
        else:
            route_account_id = normalize_cockpit_auth_id(item.account_id)
            route_is_concrete = (
                cockpit_affinity_segment_route_is_trusted(
                    segment,
                    route_account_id,
                )
                and usable_cockpit_account_label(item.label)
            )
            if route_is_concrete:
                label = item.label
                account_id = route_account_id
                evidence_confirmed = (
                    item.confirmed
                    or route_account_id
                    in cockpit_affinity_segment_confirmed_account_ids(segment)
                )
            else:
                # A new request exists, but its final account is not known yet.
                # Emit an explicit boundary so the previous request's account
                # cannot leak forward through the rest of the Codex turn.
                label = API_SERVICE_AGGREGATE_LABEL
                account_id = ""
                evidence_confirmed = False
        match_when = match_when_by_item.get(item_index, item.when)
        delta = abs((match_when - owner.when).total_seconds())
        candidates_by_event.setdefault(owner_id, []).append(
            (delta, label, item.request_id, account_id, evidence_confirmed)
        )

    anchors: dict[str, list[tuple[datetime, AccountMarker]]] = {}
    for turn_key, turn_events in events_by_turn.items():
        ordered_turn_events = sorted(turn_events, key=lambda item: item.when)
        all_event_ids = {id(event) for event in ordered_turn_events}
        account_coverage: dict[str, set[int]] = {}
        for event in ordered_turn_events:
            for candidate in candidates_by_event.get(id(event), []):
                _delta, label, _request_id, account_id, _confirmed = candidate
                if account_id and not is_api_service_mirror_label(label):
                    account_coverage.setdefault(account_id, set()).add(id(event))
        fully_covering_accounts = {
            account_id
            for account_id, covered_event_ids in account_coverage.items()
            if all_event_ids and covered_event_ids == all_event_ids
        }
        if len(fully_covering_accounts) == 1:
            account_id = next(iter(fully_covering_accounts))
            turn_anchors: list[tuple[datetime, AccountMarker]] = []
            for event_index, event in enumerate(ordered_turn_events):
                account_candidates = [
                    candidate
                    for candidate in candidates_by_event.get(id(event), [])
                    if candidate[3] == account_id
                ]
                if not account_candidates:
                    continue
                _delta, label, request_id, _account_id, _confirmed = min(
                    account_candidates
                )
                confirmed = any(candidate[4] for candidate in account_candidates)
                if event_index > 0 and not confirmed:
                    continue
                turn_anchors.append(
                    (
                        event.when,
                        AccountMarker(
                            when=event.when,
                            label=label,
                            kind=(
                                "affinity-confirmed"
                                if confirmed
                                else "affinity"
                            ),
                            request_id=request_id,
                            account_id=account_id,
                        ),
                    )
                )
            if turn_anchors:
                anchors[turn_key] = turn_anchors
                continue
        last_state: tuple[str, str] | None = None
        for event in ordered_turn_events:
            candidates = candidates_by_event.get(id(event), [])
            if not candidates:
                continue
            if any(candidate[4] for candidate in candidates):
                candidates = [
                    candidate for candidate in candidates if candidate[4]
                ]
            concrete_candidates = [
                candidate
                for candidate in candidates
                if candidate[3]
                and not is_api_service_mirror_label(candidate[1])
            ]
            concrete_accounts = {
                candidate[3]
                for candidate in concrete_candidates
            }
            has_unresolved_candidate = len(concrete_candidates) != len(candidates)
            if has_unresolved_candidate or len(concrete_accounts) != 1:
                state = (API_SERVICE_AGGREGATE_LABEL, "")
                marker = AccountMarker(
                    when=event.when,
                    label=API_SERVICE_AGGREGATE_LABEL,
                    kind="affinity-ambiguous",
                )
            else:
                account_id = next(iter(concrete_accounts))
                account_candidates = [
                    candidate
                    for candidate in concrete_candidates
                    if candidate[3] == account_id
                ]
                _delta, label, request_id, _account_id, _confirmed = min(
                    account_candidates
                )
                confirmed = any(candidate[4] for candidate in account_candidates)
                state = (label, account_id)
                marker = AccountMarker(
                    when=event.when,
                    label=label,
                    kind=(
                        "affinity-confirmed"
                        if confirmed
                        else "affinity"
                    ),
                    request_id=request_id,
                    account_id=account_id,
                )
            if state == last_state:
                if marker.kind == "affinity-confirmed":
                    anchors.setdefault(turn_key, []).append((event.when, marker))
                continue
            anchors.setdefault(turn_key, []).append((event.when, marker))
            last_state = state
    return anchors


def api_service_verdict_tier(marker: AccountMarker, exact_usage_row_match: bool) -> str:
    """Name the evidence tier behind a marker-derived account verdict.

    Only an exact Cockpit usage row for this very event is archive-grade: the
    token totals matched and the row sits inside the event's activity window.
    Every weaker join - a fuzzy token guess, or a turn-level anchor inherited by
    the other events of the same turn - stays temporal so it can never freeze a
    guess into the archive.
    """
    if exact_usage_row_match and marker.kind == "request":
        return "cockpit_usage_row"
    return "temporal"


def resolve_api_service_event_accounts(
    attributed: dict[str, list[UsageEvent]],
    account_markers: list[AccountMarker],
    known_session_accounts: dict[str, str] | None = None,
    affinity_events: list[CockpitAffinityEvent] | None = None,
    verdicts: dict[str, dict[str, str]] | None = None,
    record_verdicts: bool = True,
    preserve_direct_official_usage: bool = False,
) -> tuple[dict[str, list[UsageEvent]], dict[str, str], int]:
    """Resolve api-service mirror labels into concrete Cockpit accounts.

    ``verdicts`` is read to fill events this run cannot decide. Window and
    live-catch-up callers pass ``record_verdicts=False``: they must read the same
    archive as the today pass so every view agrees, but their scans carry less
    evidence, so they are not allowed to write into the archive that the today
    pass just decided.

    ``preserve_direct_official_usage`` is used only while reconstructing an
    official quota window. That view may scan older than the Cockpit database
    retention horizon, so a historical marker alone cannot erase a concrete,
    unqualified local GPT event from the active quota period. General daily
    attribution deliberately keeps the stricter aggregate fallback.
    """
    marker_index = account_markers_by_total_tokens(account_markers)
    cockpit_mode = bool(account_markers or affinity_events)
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
    remaining: list[tuple[str, UsageEvent]] = []
    latest_by_session: dict[str, tuple[str, bool, datetime]] = {}
    for label, event in ordered:
        opencodex_confirmed = (
            event.account_hint_source == OPENCODEX_ACCOUNT_HINT_SOURCE
            and bool(usable_cockpit_account_label(event.account_label_hint))
            and not is_api_service_mirror_label(event.account_label_hint)
        )
        opencodex_unresolved = (
            event.account_hint_source == OPENCODEX_UNRESOLVED_HINT_SOURCE
        )
        if opencodex_confirmed:
            resolved_label = event.account_label_hint
        elif opencodex_unresolved:
            resolved_label = API_SERVICE_AGGREGATE_LABEL
        else:
            resolved_label = (
                label
                if is_external_codex_provider_label(label)
                else assign_external_codex_provider_label(event, None)
            )
        if not resolved_label:
            remaining.append((label, event))
            continue
        session_id = event.session_id or event.request_key or codex_event_id(event)
        resolved.setdefault(resolved_label, []).append(event)
        event_time = usage_event_attribution_time(event)
        confirmed = not opencodex_unresolved
        if opencodex_unresolved:
            unresolved += 1
        elif opencodex_confirmed and verdicts is not None and record_verdicts:
            record_attribution_verdict(
                verdicts,
                codex_event_id(event),
                resolved_label,
                "opencodex_usage_row",
                event_time,
            )
        previous = latest_by_session.get(session_id)
        if previous is None or event_time >= previous[2]:
            latest_by_session[session_id] = (resolved_label, confirmed, event_time)
    ordered = remaining
    records: list[tuple[str, UsageEvent, str, str, AccountMarker | None]] = []
    # Only the quota-window reconstruction asks for the limited direct-label
    # escape hatch. Its scan can include old rows from a prior quota cycle;
    # retain the normal strict fallback everywhere else.
    cockpit_context_times = (
        sorted(
            [
                account_marker_epoch(marker.when)
                for marker in account_markers
                if account_marker_has_recorded_usage(marker)
            ]
            + [
                account_marker_epoch(item.when)
                for item in affinity_events or []
                if cockpit_affinity_event_concrete_account_id(item)
            ]
        )
        if preserve_direct_official_usage
        else []
    )

    def has_nearby_cockpit_context(event: UsageEvent) -> bool:
        if not cockpit_context_times:
            return False
        event_epoch = account_marker_epoch(event.when)
        position = bisect_left(cockpit_context_times, event_epoch)
        for index in (position - 1, position):
            if index < 0 or index >= len(cockpit_context_times):
                continue
            if abs(cockpit_context_times[index] - event_epoch) <= API_SERVICE_ACTIVITY_MATCH_SECONDS:
                return True
        return False
    anchors_by_turn: dict[str, list[tuple[datetime, AccountMarker]]] = {}
    # The verdict key must be taken from the freshly scanned event, because a
    # matched marker rewrites event.model further down and codex_event_id()
    # folds the model into the id.
    verdict_ids: list[str] = []
    for label, event in ordered:
        verdict_id = codex_event_id(event) if verdicts is not None else ""
        session_id = event.session_id or event.request_key or verdict_id or codex_event_id(event)
        turn_key = api_service_event_turn_key(event)
        verdict_ids.append(verdict_id)
        records.append((label, event, session_id, turn_key, None))

    confirmed_auth_result_markers = cockpit_confirmed_auth_result_event_markers(
        [record[1] for record in records],
        affinity_events or [],
    )
    final_request_markers = cockpit_final_request_event_markers(
        records,
        account_markers,
        affinity_events or [],
    )
    used_marker_ids = {
        id(marker)
        for marker in final_request_markers.values()
    }
    # Records whose own token totals matched a Cockpit usage row exactly. Only
    # these may enter the verdict archive.
    exact_match_records: set[int] = set()
    for record_index, (label, event, session_id, turn_key, _marker) in enumerate(records):
        matched_marker = final_request_markers.get(record_index)
        if matched_marker is not None:
            # The request_id join already required equal token totals, so this
            # is an exact usage-row match with affinity evidence on top.
            exact_match_records.add(record_index)
        else:
            matched_marker, exact_match = concrete_api_service_account_match(
                event,
                account_markers,
                marker_index,
                used_marker_ids,
            )
            if matched_marker is not None:
                used_marker_ids.add(id(matched_marker))
                if exact_match:
                    exact_match_records.add(record_index)
        records[record_index] = (
            label,
            event,
            session_id,
            turn_key,
            matched_marker,
        )
        if matched_marker is not None and turn_key:
            anchors_by_turn.setdefault(turn_key, []).append((event.when, matched_marker))

    for anchors in anchors_by_turn.values():
        anchors.sort(key=lambda item: item[0])

    request_start_anchors = cockpit_request_start_turn_anchors(
        records,
        account_markers,
        affinity_events or [],
    )
    for turn_key, anchor in request_start_anchors.items():
        if anchors_by_turn.get(turn_key):
            continue
        anchors_by_turn[turn_key] = [anchor]

    if isinstance(marker_index, AccountMarkerTokenIndex):
        near_time_anchors = cockpit_near_time_turn_anchors(
            records,
            marker_index,
            used_marker_ids,
        )
        for turn_key, anchors in near_time_anchors.items():
            anchors_by_turn.setdefault(turn_key, []).extend(anchors)
            anchors_by_turn[turn_key].sort(key=lambda item: item[0])

    affinity_anchors = cockpit_affinity_turn_anchors(
        [record[1] for record in records],
        account_markers,
        affinity_events or [],
    )
    for turn_key, marker in affinity_anchors.items():
        if anchors_by_turn.get(turn_key):
            continue
        anchors_by_turn[turn_key] = [(marker.when, marker)]

    nearest_start_anchors = cockpit_nearest_turn_start_affinity_anchors(
        [record[1] for record in records],
        account_markers,
        affinity_events or [],
    )
    for turn_key, marker in nearest_start_anchors.items():
        if anchors_by_turn.get(turn_key):
            continue
        anchors_by_turn[turn_key] = [(marker.when, marker)]

    consistent_affinity_anchors = cockpit_consistent_temporal_affinity_turn_anchors(
        [record[1] for record in records],
        account_markers,
        affinity_events or [],
    )
    for turn_key, anchors in consistent_affinity_anchors.items():
        existing_anchors = anchors_by_turn.setdefault(turn_key, [])
        for anchor in anchors:
            existing_anchors.append(anchor)
        anchors_by_turn[turn_key].sort(
            key=lambda item: (
                item[0],
                1 if item[1].kind == "request" else 0,
            )
        )

    for record_index, (label, event, session_id, turn_key, matched_marker) in enumerate(records):
        resolved_label = label
        confirmed = False
        verdict_tier = ""
        counted_unresolved = False
        auth_result_marker = confirmed_auth_result_markers.get(id(event))
        direct_affinity_marker: AccountMarker | None = None
        if turn_key and anchors_by_turn.get(turn_key):
            direct_affinity_candidates = [
                marker
                for anchor_when, marker in anchors_by_turn[turn_key]
                if anchor_when == event.when
                and marker.kind == "affinity-confirmed"
                and usable_cockpit_account_label(marker.label)
            ]
            direct_affinity_labels = {
                marker.label for marker in direct_affinity_candidates
            }
            if len(direct_affinity_labels) == 1:
                direct_affinity_marker = min(
                    direct_affinity_candidates,
                    key=lambda marker: (marker.request_id, marker.account_id),
                )
        quota_hint_label = (
            event.account_label_hint
            if event.account_hint_source == "quota_fingerprint"
            and event.account_label_hint
            and not is_api_service_mirror_label(event.account_label_hint)
            else ""
        )
        opencodex_turn_label = (
            event.account_label_hint
            if event.account_hint_source == OPENCODEX_TURN_HINT_SOURCE
            and event.account_label_hint
            else ""
        )
        # Exact Cockpit usage is strongest. A successful auth_result tied to
        # this event is next and can correct a stale quota fingerprint. A quota
        # hint still beats fuzzy token/time matches and inherited turn state.
        if matched_marker is not None and record_index in exact_match_records:
            resolved_label = matched_marker.label
            confirmed = True
            verdict_tier = api_service_verdict_tier(
                matched_marker,
                True,
            )
            if matched_marker.model:
                event.model = matched_marker.model
        elif auth_result_marker is not None:
            resolved_label = auth_result_marker.label
            confirmed = True
            verdict_tier = "affinity_confirmed"
        elif direct_affinity_marker is not None:
            resolved_label = direct_affinity_marker.label
            confirmed = True
            verdict_tier = "affinity_confirmed"
        elif quota_hint_label:
            resolved_label = quota_hint_label
            confirmed = True
        elif opencodex_turn_label:
            resolved_label = opencodex_turn_label
            if is_api_service_mirror_label(resolved_label):
                resolved_label = API_SERVICE_AGGREGATE_LABEL
                unresolved += 1
                counted_unresolved = True
            else:
                confirmed = True
        elif matched_marker is not None:
            resolved_label = matched_marker.label
            confirmed = True
            verdict_tier = api_service_verdict_tier(matched_marker, False)
            if matched_marker.model:
                event.model = matched_marker.model
        elif turn_key and anchors_by_turn.get(turn_key):
            anchors = anchors_by_turn[turn_key]
            anchor_times = [item[0] for item in anchors]
            position = bisect_right(anchor_times, event.when) - 1
            if position < 0:
                position = 0
            anchor_marker = anchors[position][1]
            resolved_label = anchor_marker.label
            if is_api_service_mirror_label(resolved_label):
                resolved_label = API_SERVICE_AGGREGATE_LABEL
                unresolved += 1
                counted_unresolved = True
            else:
                confirmed = True
                # A turn anchor is inherited by every event of the turn, including
                # events that never matched a marker themselves, so it decides this
                # run's label but stays out of the archive.
                verdict_tier = ""
        elif cockpit_mode or is_api_service_mirror_label(label):
            preserve_direct_label = (
                preserve_direct_official_usage
                and not is_api_service_mirror_label(label)
                and bool(usable_cockpit_account_label(label))
                and is_official_codex_quota_model(event.model)
                and not has_nearby_cockpit_context(event)
            )
            if preserve_direct_label:
                confirmed = True
            else:
                resolved_label = API_SERVICE_AGGREGATE_LABEL
                unresolved += 1
                counted_unresolved = True
        else:
            confirmed = True
        if verdicts is not None:
            # Only evidence attached to this event writes the archive. Inherited
            # turn anchors and fuzzy matches remain temporary.
            concrete = bool(resolved_label) and not is_api_service_mirror_label(resolved_label)
            if concrete:
                if record_verdicts and confirmed and verdict_tier:
                    record_attribution_verdict(
                        verdicts,
                        verdict_ids[record_index],
                        resolved_label,
                        verdict_tier,
                        usage_event_attribution_time(event),
                    )
            else:
                archived_label = archived_attribution_verdict(
                    verdicts,
                    verdict_ids[record_index],
                )
                if archived_label:
                    resolved_label = archived_label
                    confirmed = True
                    if counted_unresolved:
                        unresolved -= 1
        resolved.setdefault(resolved_label, []).append(event)
        event_time = usage_event_attribution_time(event)
        previous = latest_by_session.get(session_id)
        if previous is None or event_time >= previous[2]:
            latest_by_session[session_id] = (resolved_label, confirmed, event_time)

    for session_id, (label, confirmed, _event_time) in latest_by_session.items():
        if confirmed and label and not is_api_service_mirror_label(label):
            session_accounts[session_id] = label
        else:
            session_accounts.pop(session_id, None)
    return resolved, session_accounts, unresolved


def build_active_session_rows(
    attributed_events: dict[str, list[UsageEvent]],
    session_account_labels: dict[str, str],
    session_lifecycle: dict[str, SessionLifecycle],
    current_label: str,
    now: datetime,
    api_service_routed: bool = False,
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
        if not resolved_label and not api_service_routed:
            resolved_label = labels_by_session.get(session_id, "")
            if is_api_service_mirror_label(resolved_label):
                resolved_label = ""
        if (
            not resolved_label
            and not api_service_routed
            and current_label
            and not is_api_service_mirror_label(current_label)
        ):
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
    affinity_events: list[CockpitAffinityEvent] | None = None,
    fallback_before: datetime | None = None,
) -> tuple[dict[str, list[UsageEvent]], int]:
    merged = {label: list(events) for label, events in attributed.items()}
    marker_index = account_markers_by_total_tokens(account_markers)
    represented: set[tuple[str, str, int, str]] = set()
    ordered = sorted(
        (
            (label, event)
            for label, events in attributed.items()
            for event in events
        ),
        key=lambda item: usage_event_attribution_time(item[1]),
    )
    records: list[tuple[str, UsageEvent, str, str, AccountMarker | None]] = []
    for label, event in ordered:
        if is_external_codex_provider_label(label) or external_codex_provider_label(event.model):
            continue
        session_id = event.session_id or event.request_key or codex_event_id(event)
        records.append(
            (
                label,
                event,
                session_id,
                api_service_event_turn_key(event),
                None,
            )
        )

    final_request_markers = cockpit_final_request_event_markers(
        records,
        account_markers,
        affinity_events or [],
    )
    used_marker_ids = {
        id(marker)
        for marker in final_request_markers.values()
    }
    for marker in final_request_markers.values():
        represented.add(cockpit_marker_identity(marker))
    for record_index, (label, event, session_id, turn_key, _marker) in enumerate(records):
        marker = final_request_markers.get(record_index)
        if marker is None:
            marker = concrete_api_service_account_marker(
                event,
                account_markers,
                marker_index,
                used_marker_ids,
            )
            if marker is not None:
                used_marker_ids.add(id(marker))
                represented.add(cockpit_marker_identity(marker))
        records[record_index] = (label, event, session_id, turn_key, marker)

    request_start_anchors = cockpit_request_start_turn_anchors(
        records,
        account_markers,
        affinity_events or [],
    )
    for _turn_start, marker in request_start_anchors.values():
        used_marker_ids.add(id(marker))
        represented.add(cockpit_marker_identity(marker))

    if isinstance(marker_index, AccountMarkerTokenIndex):
        near_time_anchors = cockpit_near_time_turn_anchors(
            records,
            marker_index,
            used_marker_ids,
        )
        for anchors in near_time_anchors.values():
            for _when, marker in anchors:
                represented.add(cockpit_marker_identity(marker))

    added = 0
    for marker in account_markers:
        if marker.total_tokens <= 0 or cockpit_marker_identity(marker) in represented:
            continue
        if (
            fallback_before is not None
            and account_marker_epoch(marker.when)
            > account_marker_epoch(fallback_before)
        ):
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
    history = read_usage_history_json()
    if history is None:
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
    affinity_turn_starts = [
        turn_start
        for event in events
        if (turn_start := api_service_event_turn_start(event)) is not None
        and turn_start >= start - timedelta(days=1)
    ]
    affinity_scan_start = min([start, *affinity_turn_starts])
    affinity_events = scan_cockpit_codex_affinity_events(
        home,
        affinity_scan_start,
        end,
        account_markers,
    )
    events = reconcile_cockpit_request_usage_events(
        events,
        account_markers,
        affinity_events,
    )
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
    # No verdicts on purpose: a day already written into usage history keeps the
    # split it was archived with, so filling it from the archive would rewrite
    # numbers the user has already seen.
    resolved, _session_accounts, _unresolved = resolve_api_service_event_accounts(
        attributed,
        account_markers,
        affinity_events=affinity_events,
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
    grok_by_day = scan_grok_daily_buckets(
        home / ".grok" / "sessions",
        start,
        end,
    )
    for grok_day, grok_bucket in grok_by_day.items():
        key = grok_day.isoformat()
        if key not in wanted:
            continue
        target = buckets_by_day.setdefault(key, {}).setdefault(
            GROK_LOCAL_LABEL,
            UsageBucket(),
        )
        add_bucket(target, grok_bucket)

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
        refresh_json_backup(USAGE_HISTORY_PATH)
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
        "unpriced_tokens": sum(int(row.get("unpriced_tokens") or 0) for row in mirrors),
        "unpriced_models": {},
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
        if isinstance(models, dict):
            for model, tokens in models.items():
                aggregate["models"][str(model)] = (
                    aggregate["models"].get(str(model), 0) + int(tokens or 0)
                )
        unpriced_models = row.get("unpriced_models")
        if isinstance(unpriced_models, dict):
            for model, tokens in unpriced_models.items():
                aggregate["unpriced_models"][str(model)] = (
                    aggregate["unpriced_models"].get(str(model), 0) + int(tokens or 0)
                )
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


def read_usage_history_json() -> dict[str, Any] | None:
    """Read usage_history.json, recovering from corruption via the .bak copy."""
    if not USAGE_HISTORY_PATH.exists():
        return None
    try:
        history = json.loads(USAGE_HISTORY_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        history = recover_corrupt_json(USAGE_HISTORY_PATH)
    except OSError:
        return None
    return history if isinstance(history, dict) else None


def load_usage_history_for_backfill() -> dict[str, Any]:
    history = read_usage_history_json()
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


def opencodex_accounting_migration_dates(
    home: Path,
    now: datetime,
    max_days: int = OFFLINE_HISTORY_BACKFILL_MAX_DAYS,
) -> set[date]:
    """Return closed local dates backed by positive OpenCodex usage evidence."""
    max_days = max(0, int(max_days or 0))
    if max_days <= 0:
        return set()
    today = now.date()
    floor = today - timedelta(days=max_days)
    start = datetime.combine(floor, datetime.min.time())
    end = datetime.combine(today, datetime.min.time())
    padding = timedelta(seconds=OPENCODEX_RECONCILIATION_MATCH_WINDOW_SECONDS)
    markers = scan_opencodex_usage_markers(
        home,
        start - padding,
        end + padding,
    )
    return {
        marker.request_at.date()
        for marker in markers
        if marker.total_tokens > 0
        and not opencodex_proxy_rejection_reason(marker)
        and floor <= marker.request_at.date() < today
    }


def offline_history_dates_to_reconcile(
    history: dict[str, Any],
    now: datetime,
    max_days: int = OFFLINE_HISTORY_BACKFILL_MAX_DAYS,
    *,
    accounting_evidence_dates: set[date] | None = None,
) -> list[date]:
    max_days = max(0, int(max_days or 0))
    if max_days <= 0:
        return []
    today = now.date()
    last_complete_day = today - timedelta(days=1)
    floor = today - timedelta(days=max_days)
    accounting_evidence = {
        item
        for item in (accounting_evidence_dates or set())
        if floor <= item < today
    }
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

    previous_row = days.get(last_complete_day.isoformat())
    if isinstance(previous_row, dict):
        closed_at = datetime.combine(today, datetime.min.time())
        reconciled_at = parse_dt(previous_row.get("offline_reconciled_at"))
        offline_sync = history.get("offline_sync")
        sync = offline_sync if isinstance(offline_sync, dict) else {}
        synced_at = parse_dt(sync.get("last_successful_at"))
        try:
            synced_through = date.fromisoformat(str(sync.get("through") or ""))
        except ValueError:
            synced_through = None
        row_reconciled_after_close = (
            reconciled_at is not None and reconciled_at >= closed_at
        )
        completed_sync_covers_day = (
            str(sync.get("state") or "") == "complete"
            and synced_at is not None
            and synced_at >= closed_at
            and synced_through is not None
            and synced_through >= last_complete_day
        )
        if not row_reconciled_after_close and not completed_sync_covers_day:
            targets.add(last_complete_day)

    for key, row in days.items():
        try:
            parsed = date.fromisoformat(str(key))
        except ValueError:
            continue
        if parsed < floor or parsed >= today:
            continue
        if not isinstance(row, dict):
            targets.add(parsed)
            continue
        try:
            accounting_schema = int(row.get("usage_accounting_schema") or 0)
        except (TypeError, ValueError):
            accounting_schema = 0
        try:
            cockpit_schema = int(row.get("cockpit_usage_schema") or 0)
        except (TypeError, ValueError):
            cockpit_schema = 0
        if (
            (
                accounting_schema < USAGE_ACCOUNTING_SCHEMA
                and parsed in accounting_evidence
            )
            or cockpit_schema < COCKPIT_USAGE_DEDUPE_SCHEMA
        ):
            targets.add(parsed)
    return sorted(day for day in targets if floor <= day < today)


def scan_claude_daily_buckets(
    root: Path,
    start: datetime,
    end: datetime,
) -> dict[date, UsageBucket]:
    buckets: dict[date, UsageBucket] = {}
    for event in scan_claude_events(root, start, end):
        bucket = buckets.setdefault(event.when.date(), UsageBucket())
        add_claude_event_to_bucket(bucket, event)
    return buckets


def group_contiguous_dates(target_days: list[date]) -> list[list[date]]:
    groups: list[list[date]] = []
    for target_day in sorted(set(target_days)):
        if not groups or target_day != groups[-1][-1] + timedelta(days=1):
            groups.append([target_day])
        else:
            groups[-1].append(target_day)
    return groups


def build_historical_usage_rows(
    home: Path,
    sessions_root: Path,
    target_days: list[date],
    attribution_ledger: dict[str, str],
    now: datetime,
) -> dict[str, dict[str, Any]]:
    target_days = sorted(set(target_days))
    if not target_days:
        return {}
    groups = group_contiguous_dates(target_days)
    if len(groups) > 1:
        rows: dict[str, dict[str, Any]] = {}
        for group in groups:
            rows.update(
                build_historical_usage_rows(
                    home,
                    sessions_root,
                    group,
                    attribution_ledger,
                    now,
                )
            )
        return rows
    wanted = {item.isoformat() for item in target_days}
    start = datetime.combine(min(target_days), datetime.min.time())
    end = datetime.combine(max(target_days) + timedelta(days=1), datetime.min.time())
    events = scan_all_codex_events(home, sessions_root, start, end)
    apply_codex_speed_fallback(events, codex_speed_history(home, start, end))
    account_markers = scan_cockpit_codex_account_markers(home, start, end)
    affinity_turn_starts = [
        turn_start
        for event in events
        if (turn_start := api_service_event_turn_start(event)) is not None
        and turn_start >= start - timedelta(days=1)
    ]
    affinity_scan_start = min([start, *affinity_turn_starts])
    affinity_events = scan_cockpit_codex_affinity_events(
        home,
        affinity_scan_start,
        end,
        account_markers,
    )
    events = reconcile_cockpit_request_usage_events(
        events,
        account_markers,
        affinity_events,
    )
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
        affinity_events,
    )
    # No verdicts on purpose: history rows are rebuilt for days that are already
    # closed, and the archive must not retro-edit a split the user has seen.
    resolved, _session_accounts, _unresolved = resolve_api_service_event_accounts(
        attributed,
        account_markers,
        affinity_events=affinity_events,
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
    grok_by_day = scan_grok_daily_buckets(home / ".grok" / "sessions", start, end)
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
        grok = grok_by_day.get(target_day, UsageBucket())
        if grok.total_tokens > 0 or grok.requests > 0:
            providers.append(bucket_to_dict(GROK_LOCAL_LABEL, grok))
            add_bucket(total, grok)
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
            "usage_accounting_schema": USAGE_ACCOUNTING_SCHEMA,
            "claude_usage_schema": CLAUDE_USAGE_DEDUPE_SCHEMA,
            "cockpit_usage_schema": COCKPIT_USAGE_DEDUPE_SCHEMA,
            "grok_usage_schema": GROK_USAGE_DEDUPE_SCHEMA,
            "opencodex_attribution_schema": OPENCODEX_ACCOUNT_ATTRIBUTION_SCHEMA,
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
        int(row.get("usage_accounting_schema") or 0),
        int(row.get("requests") or 0),
        int(row.get("tokens") or 0),
        int(row.get("input_tokens") or 0),
        int(row.get("cached_input_tokens") or 0),
        int(row.get("cache_creation_input_tokens") or 0),
        int(row.get("output_tokens") or 0),
        round(float(row.get("cost") or 0), 6),
        json.dumps(row.get("models") or {}, ensure_ascii=False, sort_keys=True),
        json.dumps(row.get("providers") or [], ensure_ascii=False, sort_keys=True),
        json.dumps(row.get("source_gap") or {}, ensure_ascii=False, sort_keys=True),
    )


def append_history_high_water_residual(
    row: dict[str, Any],
    residual_tokens: int,
    residual_requests: int = 0,
    residual_cost: float = 0.0,
) -> None:
    """Append an explicit residual while preserving canonical account details."""
    residual_tokens = max(0, int(residual_tokens or 0))
    residual_requests = max(0, int(residual_requests or 0))
    residual_cost = max(0.0, float(residual_cost or 0))
    if residual_tokens <= 0:
        return
    label = "Historical high-water"
    row["input_tokens"] = max(0, int(row.get("input_tokens") or 0)) + residual_tokens
    models = dict(row.get("models") or {}) if isinstance(row.get("models"), dict) else {}
    models[label] = max(0, int(models.get(label) or 0)) + residual_tokens
    row["models"] = models
    providers = [
        dict(provider)
        for provider in (row.get("providers") or [])
        if isinstance(provider, dict)
    ]
    providers.append(
        {
            "name": label,
            "requests": residual_requests,
            "tokens": residual_tokens,
            "input_tokens": residual_tokens,
            "cached_input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "output_tokens": 0,
            "cost": round(residual_cost, 6),
            "models": {label: residual_tokens},
        }
    )
    row["providers"] = providers
    row["detail_tokens"] = int(row.get("detail_tokens") or 0) + residual_tokens


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
    try:
        existing_accounting_schema = int(
            existing.get("usage_accounting_schema") or 0
        )
    except (TypeError, ValueError):
        existing_accounting_schema = 0
    try:
        rebuilt_accounting_schema = int(
            rebuilt.get("usage_accounting_schema") or 0
        )
    except (TypeError, ValueError):
        rebuilt_accounting_schema = 0
    accounting_schema_upgrade = (
        rebuilt_accounting_schema > existing_accounting_schema
    )
    try:
        existing_schema = int(existing.get("cockpit_usage_schema") or 0)
    except (TypeError, ValueError):
        existing_schema = 0
    try:
        rebuilt_schema = int(rebuilt.get("cockpit_usage_schema") or 0)
    except (TypeError, ValueError):
        rebuilt_schema = 0
    cockpit_schema_upgrade = rebuilt_schema > existing_schema
    schema_upgrade = accounting_schema_upgrade or cockpit_schema_upgrade
    use_rebuilt = schema_upgrade or rebuilt_tokens > existing_tokens or (
        rebuilt_tokens == existing_tokens and rebuilt_detail > existing_detail
    )
    merged = dict(rebuilt if use_rebuilt else existing)
    merged["date"] = str(existing.get("date") or rebuilt.get("date") or "")
    merged["source"] = str(existing.get("source") or rebuilt.get("source") or "local-backfill")
    merged["source_date"] = str(
        existing.get("source_date") or rebuilt.get("source_date") or merged["date"]
    )
    if accounting_schema_upgrade and rebuilt_tokens >= existing_tokens:
        merged["usage_accounting_schema"] = rebuilt_accounting_schema
        merged["requests"] = int(rebuilt.get("requests") or 0)
        merged["tokens"] = rebuilt_tokens
        merged["input_tokens"] = int(rebuilt.get("input_tokens") or 0)
        merged["cached_input_tokens"] = int(rebuilt.get("cached_input_tokens") or 0)
        merged["cache_creation_input_tokens"] = int(
            rebuilt.get("cache_creation_input_tokens") or 0
        )
        merged["output_tokens"] = int(rebuilt.get("output_tokens") or 0)
        merged["cost"] = round(float(rebuilt.get("cost") or 0), 6)
        merged["models"] = rebuilt.get("models") or {}
        merged["providers"] = (
            rebuilt["providers"]
            if isinstance(rebuilt.get("providers"), list)
            else []
        )
        merged["detail_tokens"] = rebuilt_detail
        merged.pop("source_gap", None)
    elif accounting_schema_upgrade:
        residual_tokens = existing_tokens - rebuilt_tokens
        merged = dict(rebuilt)
        merged["date"] = str(existing.get("date") or rebuilt.get("date") or "")
        merged["source"] = str(
            existing.get("source") or rebuilt.get("source") or "local-backfill"
        )
        merged["source_date"] = str(
            existing.get("source_date") or rebuilt.get("source_date") or merged["date"]
        )
        merged["usage_accounting_schema"] = rebuilt_accounting_schema
        merged["requests"] = max(
            int(existing.get("requests") or 0),
            int(rebuilt.get("requests") or 0),
        )
        merged["tokens"] = existing_tokens
        merged["cost"] = round(
            max(float(existing.get("cost") or 0), float(rebuilt.get("cost") or 0)),
            6,
        )
        canonical_provider_requests = sum(
            int(provider.get("requests") or 0)
            for provider in (merged.get("providers") or [])
            if isinstance(provider, dict)
        )
        canonical_provider_cost = sum(
            float(provider.get("cost") or 0)
            for provider in (merged.get("providers") or [])
            if isinstance(provider, dict)
        )
        residual_requests = max(
            0,
            int(merged["requests"]) - canonical_provider_requests,
        )
        residual_cost = max(0.0, float(merged["cost"]) - canonical_provider_cost)
        merged.pop("source_gap", None)
        merged["accounting_migration_high_water_guard"] = {
            "retained_tokens": existing_tokens,
            "canonical_tokens": rebuilt_tokens,
            "residual_tokens": residual_tokens,
            "residual_requests": residual_requests,
            "residual_cost": round(residual_cost, 6),
            "reason": "canonical_below_existing_high_water",
        }
        append_history_high_water_residual(
            merged,
            residual_tokens,
            residual_requests,
            residual_cost,
        )
    else:
        merged["requests"] = max(
            int(existing.get("requests") or 0),
            int(rebuilt.get("requests") or 0),
        )
        merged["tokens"] = max(existing_tokens, rebuilt_tokens)
        merged["cost"] = round(
            max(float(existing.get("cost") or 0), float(rebuilt.get("cost") or 0)),
            6,
        )
        if cockpit_schema_upgrade:
            merged["cockpit_usage_schema"] = rebuilt_schema
            if rebuilt_tokens >= existing_tokens:
                merged["input_tokens"] = int(rebuilt.get("input_tokens") or 0)
                merged["cached_input_tokens"] = int(
                    rebuilt.get("cached_input_tokens") or 0
                )
                merged["cache_creation_input_tokens"] = int(
                    rebuilt.get("cache_creation_input_tokens") or 0
                )
                merged["output_tokens"] = int(rebuilt.get("output_tokens") or 0)
        if (
            (cockpit_schema_upgrade or rebuilt_detail > existing_detail)
            and isinstance(rebuilt.get("providers"), list)
        ):
            merged["providers"] = rebuilt["providers"]
            merged["models"] = rebuilt.get("models") or {}
            merged["detail_tokens"] = rebuilt_detail
        else:
            if not isinstance(merged.get("providers"), list) and isinstance(
                rebuilt.get("providers"), list
            ):
                merged["providers"] = rebuilt["providers"]
            if not isinstance(merged.get("models"), dict) and isinstance(
                rebuilt.get("models"), dict
            ):
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
        completed_at = datetime.now().replace(tzinfo=LOCAL_TZ).isoformat(timespec="seconds")
        with attribution_ledger_write_lock(USAGE_HISTORY_PATH):
            latest_history = read_usage_history_json()
            if not isinstance(latest_history, dict):
                latest_history = dict(history)
            latest_days = latest_history.setdefault("days", {})
            if not isinstance(latest_days, dict):
                latest_days = {}
                latest_history["days"] = latest_days
            changed = 0
            for target_day in targets:
                key = target_day.isoformat()
                rebuilt = rebuilt_rows.get(key)
                if not isinstance(rebuilt, dict):
                    continue
                merged, row_changed = merge_rebuilt_history_day(
                    latest_days.get(key)
                    if isinstance(latest_days.get(key), dict)
                    else None,
                    rebuilt,
                    started_at,
                )
                latest_days[key] = merged
                changed += int(row_changed)
            result.update(
                {
                    "state": "complete",
                    "updated_days": changed,
                    "completed_at": completed_at,
                }
            )
            latest_history["schema"] = max(
                2,
                int(latest_history.get("schema") or 1),
            )
            latest_history["offline_sync"] = {
                "state": "complete",
                "last_successful_at": completed_at,
                "from": result["from"],
                "through": result["through"],
                "scanned_days": result["scanned_days"],
                "updated_days": changed,
            }
            write_json_atomic(USAGE_HISTORY_PATH, latest_history)
            refresh_json_backup(USAGE_HISTORY_PATH, max_age_seconds=0)
        return result
    except Exception as exc:
        completed_at = datetime.now().replace(tzinfo=LOCAL_TZ).isoformat(timespec="seconds")
        message = f"{type(exc).__name__}: {exc}"[:240]
        try:
            with attribution_ledger_write_lock(USAGE_HISTORY_PATH):
                latest_history = read_usage_history_json()
                if not isinstance(latest_history, dict):
                    latest_history = dict(history)
                previous_sync = latest_history.get("offline_sync")
                sync = dict(previous_sync) if isinstance(previous_sync, dict) else {}
                sync.update(
                    {
                        "state": "error",
                        "last_attempt_at": completed_at,
                        "error": message,
                    }
                )
                latest_history["offline_sync"] = sync
                write_json_atomic(USAGE_HISTORY_PATH, latest_history)
                refresh_json_backup(USAGE_HISTORY_PATH)
        except (OSError, TimeoutError):
            pass
        result.update(
            {
                "state": "error",
                "completed_at": completed_at,
                "message": message,
            }
        )
        return result


@contextmanager
def offline_backfill_singleton_lock(
    path: Path | None = None,
):
    path = path or OFFLINE_BACKFILL_LOCK_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    acquired = False
    try:
        if os.name == "nt":
            import msvcrt

            try:
                handle.seek(0)
                if handle.read(1) == b"":
                    handle.seek(0)
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                yield False
                return
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
        acquired = True
        yield True
    finally:
        if acquired:
            try:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()


def read_offline_backfill_status() -> dict[str, Any]:
    try:
        value = json.loads(OFFLINE_BACKFILL_STATUS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def offline_backfill_status_is_fresh(status: dict[str, Any], now: datetime) -> bool:
    if str(status.get("state") or "") not in {"queued", "running"}:
        return False
    heartbeat = parse_dt(status.get("heartbeat_at") or status.get("queued_at"))
    if heartbeat is None:
        return False
    age = (now - heartbeat).total_seconds()
    return -OFFLINE_BACKFILL_LEASE_SECONDS <= age < OFFLINE_BACKFILL_LEASE_SECONDS


def offline_backfill_check_is_due(status: dict[str, Any], now: datetime) -> bool:
    if offline_backfill_status_is_fresh(status, now):
        return False
    next_check = parse_dt(status.get("next_check_at") or status.get("retry_after"))
    return next_check is None or now >= next_check


def offline_backfill_next_check_at(now: datetime | None = None) -> str:
    current = now or datetime.now()
    return (current + timedelta(seconds=OFFLINE_BACKFILL_CHECK_INTERVAL_SECONDS)).replace(
        tzinfo=LOCAL_TZ
    ).isoformat(timespec="seconds")


def write_offline_backfill_status(status: dict[str, Any]) -> None:
    write_json_atomic(OFFLINE_BACKFILL_STATUS_PATH, status)


def run_offline_backfill_worker(
    home: Path,
    sessions_root: Path,
    now: datetime,
    run_id: str = "",
) -> dict[str, Any]:
    with offline_backfill_singleton_lock() as acquired:
        if not acquired:
            return {"state": "already_running", "scanned_days": 0}
        status = read_offline_backfill_status()
        expected_run_id = str(status.get("run_id") or "")
        if run_id and expected_run_id and run_id != expected_run_id:
            return {"state": "superseded", "scanned_days": 0}
        run_id = run_id or expected_run_id or f"{os.getpid()}-{time.time_ns()}"
        heartbeat = datetime.now().replace(tzinfo=LOCAL_TZ).isoformat(timespec="seconds")
        write_offline_backfill_status(
            {
                "state": "running",
                "run_id": run_id,
                "started_at": heartbeat,
                "heartbeat_at": heartbeat,
            }
        )
        history = load_usage_history_for_backfill()
        evidence_dates = opencodex_accounting_migration_dates(home, now)
        targets = offline_history_dates_to_reconcile(
            history,
            now,
            accounting_evidence_dates=evidence_dates,
        )
        if not targets:
            result = {
                "state": "idle",
                "run_id": run_id,
                "scanned_days": 0,
                "next_check_at": offline_backfill_next_check_at(),
            }
            write_offline_backfill_status(result)
            return result
        ledger = load_attribution_ledger()
        total_updated = 0
        completed = 0
        last_result: dict[str, Any] = {}
        for target_group in group_contiguous_dates(targets):
            history = load_usage_history_for_backfill()
            last_result = backfill_offline_usage_history(
                home,
                sessions_root,
                now,
                ledger,
                history=history,
                target_days=target_group,
            )
            if last_result.get("state") == "error":
                last_result["run_id"] = run_id
                last_result["retry_after"] = offline_backfill_next_check_at()
                write_offline_backfill_status(last_result)
                return last_result
            completed += len(target_group)
            total_updated += int(last_result.get("updated_days") or 0)
            write_offline_backfill_status(
                {
                    "state": "running",
                    "run_id": run_id,
                    "started_at": heartbeat,
                    "heartbeat_at": datetime.now().replace(tzinfo=LOCAL_TZ).isoformat(timespec="seconds"),
                    "scanned_days": completed,
                    "total_days": len(targets),
                }
            )
        result = {
            "state": "complete",
            "run_id": run_id,
            "scanned_days": completed,
            "updated_days": total_updated,
            "completed_at": datetime.now().replace(tzinfo=LOCAL_TZ).isoformat(timespec="seconds"),
            "next_check_at": offline_backfill_next_check_at(),
        }
        write_offline_backfill_status(result)
        return result


def offline_backfill_command(output_path: Path, run_id: str) -> list[str]:
    if IS_FROZEN:
        return [
            sys.executable,
            "--offline-backfill",
            "--offline-backfill-run-id",
            run_id,
            "--output",
            str(output_path),
        ]
    return [
        sys.executable,
        str(Path(__file__).resolve()),
        "--offline-backfill",
        "--offline-backfill-run-id",
        run_id,
        "--output",
        str(output_path),
    ]


def spawn_offline_backfill_worker(output_path: Path, run_id: str) -> bool:
    creationflags = 0
    if os.name == "nt":
        creationflags = (
            getattr(subprocess, "CREATE_NO_WINDOW", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        )
    try:
        subprocess.Popen(
            offline_backfill_command(output_path, run_id),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=creationflags,
            cwd=str(APP_DIR),
            shell=False,
        )
        return True
    except OSError as exc:
        logger.warning("offline history worker launch failed: %s", exc)
        return False


def queue_offline_backfill_worker(output_path: Path, now: datetime) -> dict[str, Any]:
    status = read_offline_backfill_status()
    if not offline_backfill_check_is_due(status, now):
        return status
    should_spawn = False
    run_id = ""
    with offline_backfill_singleton_lock() as acquired:
        if not acquired:
            status = read_offline_backfill_status()
            return status or {"state": "running"}
        status = read_offline_backfill_status()
        if not offline_backfill_check_is_due(status, now):
            return status
        run_id = f"{os.getpid()}-{time.time_ns()}"
        queued_at = now.replace(tzinfo=LOCAL_TZ).isoformat(timespec="seconds")
        status = {
            "state": "queued",
            "run_id": run_id,
            "queued_at": queued_at,
            "heartbeat_at": queued_at,
        }
        write_offline_backfill_status(status)
        should_spawn = True
    if should_spawn and not spawn_offline_backfill_worker(output_path, run_id):
        with offline_backfill_singleton_lock() as acquired:
            current = read_offline_backfill_status()
            if (
                acquired
                and str(current.get("run_id") or "") == run_id
                and str(current.get("state") or "") == "queued"
            ):
                status = dict(current)
                status["state"] = "launch_failed"
                status["retry_after"] = offline_backfill_next_check_at(now)
                write_offline_backfill_status(status)
    return status


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
    try:
        current_accounting_schema = int(output.get("usage_accounting_schema") or 0)
        existing_accounting_schema = int(
            existing.get("usage_accounting_schema") or 0
        )
    except (TypeError, ValueError):
        current_accounting_schema = 0
        existing_accounting_schema = 0
    accounting_schema_upgrade = (
        current_accounting_schema > existing_accounting_schema
    )
    try:
        current_claude_schema = int(output.get("claude_usage_schema") or 0)
        existing_claude_schema = int(existing.get("claude_usage_schema") or 0)
    except (TypeError, ValueError):
        current_claude_schema = 0
        existing_claude_schema = 0
    claude_schema_upgrade = current_claude_schema > existing_claude_schema
    try:
        current_cockpit_schema = int(output.get("cockpit_usage_schema") or 0)
        existing_cockpit_schema = int(existing.get("cockpit_usage_schema") or 0)
    except (TypeError, ValueError):
        current_cockpit_schema = 0
        existing_cockpit_schema = 0
    cockpit_schema_upgrade = current_cockpit_schema > existing_cockpit_schema
    try:
        current_opencodex_schema = int(
            output.get("opencodex_attribution_schema") or 0
        )
        existing_opencodex_schema = int(
            existing.get("opencodex_attribution_schema") or 0
        )
    except (TypeError, ValueError):
        current_opencodex_schema = 0
        existing_opencodex_schema = 0
    opencodex_schema_upgrade = (
        current_opencodex_schema > existing_opencodex_schema
    )
    usage_schema_upgrade = (
        accounting_schema_upgrade
        or claude_schema_upgrade
        or cockpit_schema_upgrade
        or opencodex_schema_upgrade
    )
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
            "unpriced_tokens",
            "unpriced_models",
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
    if (
        not current_api_aggregate
        and not usage_schema_upgrade
        and isinstance(existing_today, dict)
        and isinstance(current_today, dict)
    ):
        merge_cumulative(current_today, existing_today)
    merge_latest_request()
    if not current_api_aggregate and not usage_schema_upgrade:
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
        if accounting_schema_upgrade:
            current = current_by_name.get(name)
            if (
                current is not None
                and "window_30d" not in current
                and isinstance(previous.get("window_30d"), dict)
            ):
                current["window_30d"] = dict(previous["window_30d"])
            continue
        if claude_schema_upgrade and name == "Claude local":
            continue
        if opencodex_schema_upgrade and is_codex_account_provider_name(name):
            continue
        current = current_by_name.get(name)
        if current_api_account_routing or (
            cockpit_schema_upgrade and is_codex_account_provider_name(name)
        ):
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
        current_today["unpriced_tokens"] = sum(
            int(provider.get("unpriced_tokens") or 0) for provider in provider_totals
        )
        unpriced_models: dict[str, int] = {}
        for provider in provider_totals:
            provider_unpriced = provider.get("unpriced_models")
            if not isinstance(provider_unpriced, dict):
                continue
            for model, tokens in provider_unpriced.items():
                name = str(model or "unknown")
                unpriced_models[name] = unpriced_models.get(name, 0) + int(tokens or 0)
        current_today["unpriced_models"] = unpriced_models


def restore_today_from_usage_history(output: dict[str, Any], day: date) -> None:
    if output.get("api_service_aggregate"):
        return
    history = read_usage_history_json()
    if history is None:
        return
    days = history.get("days") if isinstance(history, dict) else None
    row = days.get(day.isoformat()) if isinstance(days, dict) else None
    today = output.get("today")
    if not isinstance(row, dict) or not isinstance(today, dict):
        return
    row = dict(row)
    try:
        current_accounting_schema = int(output.get("usage_accounting_schema") or 0)
        history_accounting_schema = int(row.get("usage_accounting_schema") or 0)
    except (TypeError, ValueError):
        current_accounting_schema = 0
        history_accounting_schema = 0
    if current_accounting_schema > history_accounting_schema:
        return
    try:
        current_claude_schema = int(output.get("claude_usage_schema") or 0)
        history_claude_schema = int(row.get("claude_usage_schema") or 0)
    except (TypeError, ValueError):
        current_claude_schema = 0
        history_claude_schema = 0
    if current_claude_schema > history_claude_schema:
        return
    try:
        current_cockpit_schema = int(output.get("cockpit_usage_schema") or 0)
        history_cockpit_schema = int(row.get("cockpit_usage_schema") or 0)
    except (TypeError, ValueError):
        current_cockpit_schema = 0
        history_cockpit_schema = 0
    if current_cockpit_schema > history_cockpit_schema:
        return
    try:
        current_opencodex_schema = int(
            output.get("opencodex_attribution_schema") or 0
        )
        history_opencodex_schema = int(
            row.get("opencodex_attribution_schema") or 0
        )
    except (TypeError, ValueError):
        current_opencodex_schema = 0
        history_opencodex_schema = 0
    if current_opencodex_schema > history_opencodex_schema:
        return
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
        gap["unpriced_tokens"] = max(
            0,
            int(row.get("unpriced_tokens") or 0)
            - int(today.get("unpriced_tokens") or 0),
        )
        history_row = days.get(day.isoformat()) if isinstance(days, dict) else None
        if isinstance(history_row, dict):
            history_row["source_gap"] = gap
            try:
                write_json_atomic(USAGE_HISTORY_PATH, history)
                refresh_json_backup(USAGE_HISTORY_PATH)
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
    today["unpriced_tokens"] = (
        int(today.get("unpriced_tokens") or 0)
        + int(gap.get("unpriced_tokens") or 0)
    )
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
        "unpriced_tokens": max(
            0,
            int(today.get("unpriced_tokens") or 0)
            - int(provider_sum("unpriced_tokens")),
        ),
        "unpriced_models": {},
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
        "unpriced_tokens": bucket.unpriced_tokens,
        "unpriced_models": dict(
            sorted(bucket.unpriced_models.items(), key=lambda item: item[1], reverse=True)[:8]
        ),
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
        "unpriced_tokens": bucket.unpriced_tokens,
        "unpriced_models": dict(
            sorted(bucket.unpriced_models.items(), key=lambda item: item[1], reverse=True)[:8]
        ),
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
    if window.get("quota_stale"):
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


def prefer_local_usage_buckets(
    cockpit_fallback: dict[str, UsageBucket],
    local_buckets: dict[str, UsageBucket],
    *,
    local_window_covered: bool,
) -> dict[str, UsageBucket]:
    """Choose one complete token source for a window, never a per-label mix.

    Cockpit can establish a reset boundary and an account identity, but local
    ``UsageEvent`` rows are the authoritative token ledger once their source
    covered the window. If the source was unavailable, retain Cockpit as the
    explicit fallback rather than treating an empty local map as a real zero.
    """
    return dict(local_buckets) if local_window_covered else dict(cockpit_fallback)


def build_codex_window_stats(
    home: Path,
    sessions_root: Path,
    now: datetime,
    attribution_ledger: dict[str, str],
    current_label: str,
    attribution_verdicts: dict[str, dict[str, str]] | None = None,
    include_30d: bool = False,
) -> dict[str, dict[str, dict[str, Any]]]:
    """Build the per-account 5h/7d/cycle/30d windows.

    The archived verdicts are read here as well, so a window that contains today
    resolves the same accounts today's own totals do. Windows never write into
    the archive: their scans carry no affinity evidence, so they must not be able
    to overrule what the today pass decided.
    """
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
        account_markers_7d = scan_cockpit_codex_account_markers(
            home,
            window_7d_start,
            window_end,
        )
        affinity_events_7d = scan_cockpit_codex_affinity_events(
            home,
            window_7d_start,
            window_end,
            account_markers_7d,
        )
        events_7d = reconcile_cockpit_request_usage_events(
            events_7d,
            account_markers_7d,
            affinity_events_7d,
        )
        markers_7d.extend(account_markers_7d)
        buckets_7d = attribute_codex_events_to_account_markers(
            events_7d,
            markers_7d,
            cost_multiplier_by_label,
            attribution_ledger,
            current_label,
            now,
            attribution_verdicts,
        )

        events_5h = scan_all_codex_events(home, sessions_root, window_5h_start, window_end)
        apply_codex_speed_fallback(events_5h, speed_markers)
        markers_5h = scan_cockpit_codex_switch_markers(home, window_5h_start, window_end)
        account_markers_5h = scan_cockpit_codex_account_markers(
            home,
            window_5h_start,
            window_end,
        )
        affinity_events_5h = scan_cockpit_codex_affinity_events(
            home,
            window_5h_start,
            window_end,
            account_markers_5h,
        )
        events_5h = reconcile_cockpit_request_usage_events(
            events_5h,
            account_markers_5h,
            affinity_events_5h,
        )
        markers_5h.extend(account_markers_5h)
        buckets_5h = attribute_codex_events_to_account_markers(
            events_5h,
            markers_5h,
            cost_multiplier_by_label,
            attribution_ledger,
            current_label,
            now,
            attribution_verdicts,
        )
        if include_30d:
            speed_markers_30d = codex_speed_history(home, window_30d_start, window_end)
            events_30d = scan_all_codex_events(home, sessions_root, window_30d_start, window_end)
            apply_codex_speed_fallback(events_30d, speed_markers_30d)
            markers_30d = scan_cockpit_codex_switch_markers(home, window_30d_start, window_end)
            account_markers_30d = scan_cockpit_codex_account_markers(
                home,
                window_30d_start,
                window_end,
            )
            affinity_events_30d = scan_cockpit_codex_affinity_events(
                home,
                window_30d_start,
                window_end,
                account_markers_30d,
            )
            events_30d = reconcile_cockpit_request_usage_events(
                events_30d,
                account_markers_30d,
                affinity_events_30d,
            )
            markers_30d.extend(account_markers_30d)
            buckets_30d = attribute_codex_events_to_account_markers(
                events_30d,
                markers_30d,
                cost_multiplier_by_label,
                attribution_ledger,
                current_label,
                now,
                attribution_verdicts,
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
        affinity_events_30d = scan_cockpit_codex_affinity_events(
            home,
            window_30d_start,
            window_end,
            account_markers_30d,
        )
        events_30d = reconcile_cockpit_request_usage_events(
            events_30d,
            account_markers_30d,
            affinity_events_30d,
        )
        markers_30d = switch_markers_30d + account_markers_30d
        attributed_30d = attribute_codex_events_by_account(
            events_30d,
            markers_30d,
            attribution_ledger,
            current_label,
            now,
        )
        attributed_30d, _session_accounts_30d, _unresolved_30d = resolve_api_service_event_accounts(
            attributed_30d,
            account_markers_30d,
            None,
            None,
            attribution_verdicts,
            record_verdicts=False,
        )
        raw_30d: dict[str, UsageBucket] = {}
        for label, account_events in attributed_30d.items():
            for event in account_events:
                resolved_label = label
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
    local_window_covered = False
    scan_starts = aligned_starts + [window_7d_start]
    if scan_starts:
        aligned_scan_start = min(scan_starts) - timedelta(
            seconds=max(0, QUOTA_WINDOW_START_TOLERANCE_SECONDS)
        )
        aligned_events = scan_all_codex_events(home, sessions_root, aligned_scan_start, window_end)
        local_window_covered = bool(aligned_events) or local_codex_window_source_available(
            home,
            sessions_root,
            aligned_scan_start,
            window_end,
        )
        speed_markers = codex_speed_history(home, aligned_scan_start, window_end)
        apply_codex_speed_fallback(aligned_events, speed_markers)
        aligned_markers = scan_cockpit_codex_switch_markers(home, aligned_scan_start, window_end)
        aligned_account_markers = scan_cockpit_codex_account_markers(home, aligned_scan_start, window_end)
        aligned_affinity_events = scan_cockpit_codex_affinity_events(
            home,
            aligned_scan_start,
            window_end,
            aligned_account_markers,
        )
        aligned_events = reconcile_cockpit_request_usage_events(
            aligned_events,
            aligned_account_markers,
            aligned_affinity_events,
        )
        aligned_markers.extend(aligned_account_markers)
        attributed_events = attribute_codex_events_by_account(
            aligned_events,
            aligned_markers,
            attribution_ledger,
            current_label,
            now,
        )
        attributed_events, _session_accounts_aligned, _unresolved_aligned = resolve_api_service_event_accounts(
            attributed_events,
            aligned_account_markers,
            None,
            None,
            attribution_verdicts,
            record_verdicts=False,
            preserve_direct_official_usage=True,
        )
        raw_5h = {label: UsageBucket() for label in aligned_starts_5h}
        raw_7d = {label: UsageBucket() for label in aligned_starts_7d}
        raw_cycle = {label: UsageBucket() for label in aligned_starts_cycle}
        raw_rolling_5h: dict[str, UsageBucket] = {}
        raw_rolling_7d: dict[str, UsageBucket] = {}
        for label, account_events in attributed_events.items():
            for event in account_events:
                resolved_label = label
                event_time = usage_event_attribution_time(event)
                multiplier = cost_multiplier_by_label.get(resolved_label, 1.0)
                if event_time >= window_5h_start:
                    add_codex_event_to_bucket(
                        raw_rolling_5h.setdefault(resolved_label, UsageBucket()),
                        event,
                        multiplier,
                        bucket_time=event_time,
                    )
                if event_time >= window_7d_start:
                    add_codex_event_to_bucket(
                        raw_rolling_7d.setdefault(resolved_label, UsageBucket()),
                        event,
                        multiplier,
                        bucket_time=event_time,
                    )
                if resolved_label in aligned_starts_5h and event_time >= aligned_starts_5h[resolved_label]:
                    quota_5h = (quota_by_account.get(resolved_label) or {}).get("window_5h")
                    if event_counts_toward_official_quota_window(event, quota_5h):
                        add_codex_event_to_bucket(raw_5h[resolved_label], event, multiplier, bucket_time=event_time)
                if resolved_label in aligned_starts_7d and event_time >= aligned_starts_7d[resolved_label]:
                    quota_7d = (quota_by_account.get(resolved_label) or {}).get("window_7d")
                    if event_counts_toward_official_quota_window(event, quota_7d):
                        add_codex_event_to_bucket(raw_7d[resolved_label], event, multiplier, bucket_time=event_time)
                if resolved_label in aligned_starts_cycle and event_time >= aligned_starts_cycle[resolved_label]:
                    quota_cycle = (quota_by_account.get(resolved_label) or {}).get("window_cycle")
                    if event_counts_toward_official_quota_window(event, quota_cycle):
                        add_codex_event_to_bucket(raw_cycle[resolved_label], event, multiplier, bucket_time=event_time)
        local_5h_buckets = dict(raw_rolling_5h)
        # A quota-limited 5h window must use its filtered local bucket, not the
        # wider analysis bucket that can contain external-model activity.
        local_5h_buckets.update(raw_5h)
        local_7d_buckets = dict(raw_rolling_7d)
        local_7d_buckets.update(raw_7d)
        aligned_5h = prefer_local_usage_buckets(
            aligned_5h,
            raw_5h,
            local_window_covered=local_window_covered,
        )
        aligned_7d = prefer_local_usage_buckets(
            aligned_7d,
            raw_7d,
            local_window_covered=local_window_covered,
        )
        aligned_cycle = prefer_local_usage_buckets(
            aligned_cycle,
            raw_cycle,
            local_window_covered=local_window_covered,
        )
        buckets_5h = prefer_local_usage_buckets(
            buckets_5h,
            local_5h_buckets,
            local_window_covered=local_window_covered,
        )
        buckets_7d = prefer_local_usage_buckets(
            buckets_7d,
            local_7d_buckets,
            local_window_covered=local_window_covered,
        )
        rolling_7d_buckets = prefer_local_usage_buckets(
            rolling_7d_buckets,
            raw_rolling_7d,
            local_window_covered=local_window_covered,
        )
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
            "usage_accounting_schema": USAGE_ACCOUNTING_SCHEMA,
            "claude_usage_schema": CLAUDE_USAGE_DEDUPE_SCHEMA,
            "cockpit_usage_schema": COCKPIT_USAGE_DEDUPE_SCHEMA,
            "grok_usage_schema": GROK_USAGE_DEDUPE_SCHEMA,
            "opencodex_attribution_schema": OPENCODEX_ACCOUNT_ATTRIBUTION_SCHEMA,
            "since": since.replace(tzinfo=LOCAL_TZ).isoformat(timespec="microseconds"),
            "through": through.replace(tzinfo=LOCAL_TZ).isoformat(timespec="microseconds"),
            "events": [],
            "latest_request": {},
        }

    usage_day_start = datetime.combine(through.date(), datetime.min.time())
    # A quota cycle may start before midnight even though the absolute totals
    # in this payload must still describe today. Scan from the earlier quota
    # boundary for window reconstruction, then filter provider totals back to
    # the current calendar day below.
    scan_start = min(since, usage_day_start)
    record_current_opencodex_account_snapshot(home, through)
    session_lifecycle: dict[str, SessionLifecycle] = {}
    codex_events = scan_all_codex_events(
        home,
        sessions_root,
        scan_start,
        through,
        session_lifecycle=session_lifecycle,
    )
    speed_markers = codex_speed_history(home, scan_start, through)
    apply_codex_speed_fallback(codex_events, speed_markers)

    current_label = current_codex_account_label(home)
    attribution_ledger = load_attribution_ledger()
    # Read-only: the catch-up must report the same accounts the full export does,
    # otherwise the monitor would flip between an aggregate label and a concrete
    # account between refreshes. It never archives, so it cannot decide anything.
    attribution_verdicts = load_attribution_verdicts()
    markers = scan_cockpit_codex_switch_markers(home, scan_start, through)
    markers.extend(load_account_timeline())
    account_markers = scan_cockpit_codex_account_markers(home, scan_start, through)
    affinity_turn_starts = [
        turn_start
        for event in codex_events
        if (turn_start := api_service_event_turn_start(event)) is not None
        and turn_start >= scan_start - timedelta(days=1)
    ]
    affinity_scan_start = min([scan_start, *affinity_turn_starts])
    affinity_events = scan_cockpit_codex_affinity_events(
        home,
        affinity_scan_start,
        through,
        account_markers,
    )
    codex_events = reconcile_cockpit_request_usage_events(
        codex_events,
        account_markers,
        affinity_events,
    )
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
        affinity_events,
        fallback_before=through - timedelta(seconds=COCKPIT_FALLBACK_GRACE_SECONDS),
    )
    attributed, _session_accounts, unresolved_events = resolve_api_service_event_accounts(
        raw_attributed,
        account_markers,
        previous_active_session_account_labels(output_path, through.date()),
        affinity_events,
        attribution_verdicts,
        record_verdicts=False,
    )
    speed_by_account = cockpit_codex_speed_by_label(home)
    cost_multiplier_by_label = {
        label: float(meta.get("cost_multiplier") or 1.0)
        for label, meta in speed_by_account.items()
    }

    daily_attributed = {
        provider: [
            event
            for event in events
            if usage_day_start <= event.when < through
        ]
        for provider, events in attributed.items()
    }
    daily_attributed = {
        provider: events
        for provider, events in daily_attributed.items()
        if events
    }
    provider_buckets = buckets_from_attributed_events(
        daily_attributed,
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
    claude = scan_claude(
        home / ".claude" / "projects",
        usage_day_start,
        through,
    )
    if claude.requests or claude.total_tokens or claude.cost:
        add_bucket(total, claude)
        provider_totals.append(bucket_to_dict("Claude local", claude))
    grok_events = scan_grok_events(
        home / ".grok" / "sessions",
        usage_day_start,
        through,
    )
    grok = bucket_from_grok_events(grok_events)
    if grok.requests or grok.total_tokens or grok.cost:
        add_bucket(total, grok)
        provider_totals.append(bucket_to_dict(GROK_LOCAL_LABEL, grok))

    # Keep the provider/model pair together for the monitor's live catch-up.
    # The aggregate summary has a model and timestamp but no provider, which
    # is ambiguous when Grok and Codex finish close together.
    latest_request_provider = ""
    latest_request_model = ""
    latest_request_at: datetime | None = None

    def consider_latest_request(provider_name: str, bucket: UsageBucket) -> None:
        nonlocal latest_request_provider, latest_request_model, latest_request_at
        if bucket.latest_at is None:
            return
        if latest_request_at is None or bucket.latest_at > latest_request_at:
            latest_request_at = bucket.latest_at
            latest_request_provider = provider_name
            latest_request_model = bucket.latest_model

    for provider_name, bucket in provider_buckets.items():
        consider_latest_request(provider_name, bucket)
    consider_latest_request("Claude local", claude)
    consider_latest_request(GROK_LOCAL_LABEL, grok)
    latest_request = {
        "provider": latest_request_provider,
        "model": latest_request_model,
        "created_at": (
            latest_request_at.replace(tzinfo=LOCAL_TZ).isoformat(timespec="seconds")
            if latest_request_at is not None and latest_request_at.tzinfo is None
            else latest_request_at.isoformat(timespec="seconds")
            if latest_request_at is not None
            else ""
        ),
        "kind": "success" if latest_request_at is not None else "",
    }

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
                    "canonical_id": str(event.canonical_id or ""),
                    "supersedes_event_ids": list(event.supersedes_event_ids),
                    "when": aware_when.isoformat(timespec="microseconds"),
                    "provider": provider,
                    "model": event.model,
                    "session_id": event.session_id,
                    "request_key": event.request_key,
                    "route": event.route,
                    "total_tokens": event.total_tokens,
                    "input_tokens": event.input_tokens + event.cached_tokens,
                    "cached_tokens": event.cached_tokens,
                    "output_tokens": event.output_tokens,
                    "cost": round(event_bucket.cost, 12),
                    "price_resolved": event_bucket.unpriced_tokens == 0,
                    "unpriced_tokens": event_bucket.unpriced_tokens,
                }
            )
    for event in grok_events:
        if not since < event.when < through:
            continue
        event_bucket = UsageBucket()
        add_codex_event_to_bucket(event_bucket, event)
        aware_when = (
            event.when
            if event.when.tzinfo is not None
            else event.when.replace(tzinfo=LOCAL_TZ)
        )
        rows.append(
            {
                "event_id": live_usage_event_id(event),
                "canonical_id": str(event.canonical_id or ""),
                "supersedes_event_ids": list(event.supersedes_event_ids),
                "when": aware_when.isoformat(timespec="microseconds"),
                "provider": GROK_LOCAL_LABEL,
                "model": event.model,
                "session_id": event.session_id,
                "request_key": event.request_key,
                "route": event.route,
                "total_tokens": event.total_tokens,
                "input_tokens": event.input_tokens + event.cached_tokens,
                "cached_tokens": event.cached_tokens,
                "output_tokens": event.output_tokens,
                "cost": round(event_bucket.cost, 12),
                "price_resolved": event_bucket.unpriced_tokens == 0,
                "unpriced_tokens": event_bucket.unpriced_tokens,
            }
        )
    rows.sort(key=lambda row: (str(row.get("when") or ""), str(row.get("event_id") or "")))
    return {
        "schema": 1,
        "usage_accounting_schema": USAGE_ACCOUNTING_SCHEMA,
        "claude_usage_schema": CLAUDE_USAGE_DEDUPE_SCHEMA,
        "cockpit_usage_schema": COCKPIT_USAGE_DEDUPE_SCHEMA,
        "grok_usage_schema": GROK_USAGE_DEDUPE_SCHEMA,
        "opencodex_attribution_schema": OPENCODEX_ACCOUNT_ATTRIBUTION_SCHEMA,
        "since": since.replace(tzinfo=LOCAL_TZ).isoformat(timespec="microseconds"),
        "through": through.replace(tzinfo=LOCAL_TZ).isoformat(timespec="microseconds"),
        "events": rows,
        "summary": bucket_to_dict("Client live catch-up", total),
        "providers": provider_totals,
        "latest_request": latest_request,
        "fallback_events": int(fallback_events),
        "unresolved_events": int(unresolved_events),
    }


def export_usage_report(
    output_path: Path,
    home: Path,
    now: datetime,
    day: date,
    *,
    include_30d: bool = False,
    queue_offline_history: bool = True,
    backfill_history_details: bool = False,
) -> dict[str, Any]:
    out = output_path
    cached_30d_valid = False
    cached_30d_updated_at = ""
    cached_30d_windows: dict[str, dict[str, Any]] = {}
    if include_30d:
        (
            cached_30d_valid,
            cached_30d_updated_at,
            cached_30d_windows,
        ) = load_cached_account_30d_windows(out, now)
    refresh_30d = include_30d and not cached_30d_valid
    start = datetime.combine(day, datetime.min.time())
    end = start + timedelta(days=1)
    scan_end = min(now, end) if day == now.date() else end

    codex_sessions_root = home / ".codex" / "sessions"
    record_current_account_snapshot(home, now)
    record_current_opencodex_account_snapshot(home, now)
    attribution_ledger = load_attribution_ledger()
    attribution_verdicts = load_attribution_verdicts()
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
    today_reconciliation = last_opencodex_reconciliation_diagnostics()
    # A manual auth switch can happen during a long session scan. Re-read the
    # identity here and retain the auth file's mtime as the actual switch edge.
    snapshot_now = datetime.now()
    record_current_account_snapshot(home, snapshot_now)
    record_current_opencodex_account_snapshot(home, snapshot_now)
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
    affinity_turn_starts = [
        turn_start
        for event in codex_events
        if (turn_start := api_service_event_turn_start(event)) is not None
        and turn_start >= start - timedelta(days=1)
    ]
    affinity_scan_start = min([start, *affinity_turn_starts])
    affinity_events = scan_cockpit_codex_affinity_events(
        home,
        affinity_scan_start,
        scan_end,
        account_markers,
    )
    codex_events = reconcile_cockpit_request_usage_events(
        codex_events,
        account_markers,
        affinity_events,
    )
    markers.extend(account_markers)
    raw_attributed_events = attribute_codex_events_by_account(
        codex_events,
        markers,
        attribution_ledger,
        current_label,
        now,
    )
    api_service_routed = (
        any(
            is_api_service_mirror_label(label)
            for label in raw_attributed_events
        )
        or bool(account_markers)
        # An in-flight Cockpit request may have affinity evidence before its
        # usage row is written. Treat that as routed so an old session label
        # cannot leak into the active-session display during the handoff.
        or bool(affinity_events)
    )
    raw_attributed_events, cockpit_fallback_events = merge_missing_cockpit_account_events(
        raw_attributed_events,
        account_markers,
        affinity_events,
        fallback_before=scan_end - timedelta(seconds=COCKPIT_FALLBACK_GRACE_SECONDS),
    )
    attributed_events, provider_session_accounts, unresolved_provider_events = resolve_api_service_event_accounts(
        raw_attributed_events,
        account_markers,
        previous_active_session_account_labels(out, day),
        affinity_events,
        attribution_verdicts,
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
    claude_events = scan_claude_events(claude_root, start, scan_end)
    claude = bucket_from_claude_events(claude_events)
    grok_root = home / ".grok" / "sessions"
    grok_events = scan_grok_events(grok_root, start, scan_end)
    grok = bucket_from_grok_events(grok_events)
    hourly_codex_events = [
        event
        for events in attributed_events.values()
        for event in events
    ]
    hourly_today = merge_hourly_buckets(
        codex_hourly_from_events(hourly_codex_events),
        claude_hourly_from_events(claude_events),
        codex_hourly_from_events(grok_events),
    )
    mark_codex_failure_hours(
        hourly_today,
        codex_failures,
        day,
        now,
        activity_events=codex_events,
    )
    if include_30d and cached_30d_valid:
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
            attribution_verdicts,
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
    save_attribution_ledger(attribution_ledger, now, attribution_verdicts)

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
        api_service_routed=api_service_routed,
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
            if include_30d and name in cached_30d_windows:
                provider["window_30d"] = cached_30d_windows[name]
        codex_providers.append(provider)
    providers = codex_providers + [
        bucket_to_dict("Claude local", claude),
        bucket_to_dict(GROK_LOCAL_LABEL, grok),
    ]
    total = UsageBucket()
    for bucket in (codex, claude, grok):
        total.requests += bucket.requests
        total.input_tokens += bucket.input_tokens
        total.cached_input_tokens += bucket.cached_input_tokens
        total.cache_creation_input_tokens += bucket.cache_creation_input_tokens
        total.cache_read_input_tokens += bucket.cache_read_input_tokens
        total.output_tokens += bucket.output_tokens
        total.cost += bucket.cost
        total.unpriced_tokens += bucket.unpriced_tokens
        for model, tokens in bucket.unpriced_models.items():
            total.unpriced_models[model] = total.unpriced_models.get(model, 0) + tokens
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
    latest_candidates = [
        ("Claude local", claude),
        (GROK_LOCAL_LABEL, grok),
    ]
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
            lookback_account_markers = scan_cockpit_codex_account_markers(
                home,
                lookback_start,
                scan_end,
            )
            lookback_markers.extend(lookback_account_markers)
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
                lookback_account_markers,
            )
            recent_dt = parse_dt(recent_latest_request.get("created_at"))
            if recent_dt is not None:
                latest_dt = recent_dt
                latest_provider = str(recent_latest_request.get("provider") or "")
                latest_model = str(recent_latest_request.get("model") or "")
                latest_at = str(recent_latest_request.get("created_at") or "")
        lookback_grok = scan_grok(grok_root, lookback_start, scan_end)
        if lookback_grok.latest_at is not None and (
            latest_dt is None or lookback_grok.latest_at > latest_dt
        ):
            latest_dt = lookback_grok.latest_at
            latest_provider = GROK_LOCAL_LABEL
            latest_model = lookback_grok.latest_model
            latest_at = latest_at_text(lookback_grok)

    output = {
        "schema": 1,
        "usage_accounting_schema": USAGE_ACCOUNTING_SCHEMA,
        "claude_usage_schema": CLAUDE_USAGE_DEDUPE_SCHEMA,
        "cockpit_usage_schema": COCKPIT_USAGE_DEDUPE_SCHEMA,
        "grok_usage_schema": GROK_USAGE_DEDUPE_SCHEMA,
        "opencodex_attribution_schema": OPENCODEX_ACCOUNT_ATTRIBUTION_SCHEMA,
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
            "reconciliation": today_reconciliation,
        },
    }
    offline_queue_enabled = (
        queue_offline_history
        and day == now.date()
        and OFFLINE_HISTORY_BACKFILL_MAX_DAYS > 0
    )
    if offline_queue_enabled:
        queued = read_offline_backfill_status()
        output["offline_catchup"] = {
            key: value
            for key, value in queued.items()
            if key
            in {
                "state",
                "run_id",
                "queued_at",
                "started_at",
                "heartbeat_at",
                "completed_at",
                "next_check_at",
                "retry_after",
                "scanned_days",
                "updated_days",
            }
        }
    if include_30d:
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
    if offline_queue_enabled:
        queued = queue_offline_backfill_worker(out, datetime.now())
        if isinstance(output.get("offline_catchup"), dict):
            output["offline_catchup"] = {
                key: value
                for key, value in queued.items()
                if key
                in {
                    "state",
                    "run_id",
                    "queued_at",
                    "started_at",
                    "heartbeat_at",
                    "completed_at",
                    "next_check_at",
                    "retry_after",
                    "scanned_days",
                    "updated_days",
                }
            }
            write_json_atomic(out, output)
    if backfill_history_details:
        backfill_usage_history_details(home, codex_sessions_root)
    logger.info(
        "export run finished in %.1fs: codex_events=%d grok_events=%d providers=%d ledger_entries=%d verdicts=%d",
        (datetime.now() - now).total_seconds(),
        len(codex_events),
        len(grok_events),
        len(providers),
        len(attribution_ledger),
        len(attribution_verdicts),
    )
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description="Export local Claude/Codex client token usage for Sub2API monitor.")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--date", default="")
    parser.add_argument("--include-30d", action="store_true")
    parser.add_argument("--backfill-history-details", action="store_true")
    parser.add_argument("--offline-backfill", action="store_true")
    parser.add_argument("--offline-backfill-run-id", default="")
    parser.add_argument("--quota-only", action="store_true")
    parser.add_argument("--live-since", default="")
    parser.add_argument("--live-through", default="")
    args = parser.parse_args()

    now = datetime.now()
    out = Path(args.output)
    home = Path(os.path.expanduser("~"))
    if args.offline_backfill:
        result = run_offline_backfill_worker(
            home,
            home / ".codex" / "sessions",
            now,
            args.offline_backfill_run_id,
        )
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return 0 if result.get("state") != "error" else 1
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
                    "accounts": cockpit_codex_quota_by_label(
                        home,
                        force_active_official_refresh=True,
                    ),
                },
                ensure_ascii=False,
            )
        )
        return 0
    day = datetime.fromisoformat(args.date).date() if args.date else now.date()
    output = export_usage_report(
        out,
        home,
        now,
        day,
        include_30d=args.include_30d,
        queue_offline_history=not bool(args.date),
        backfill_history_details=args.backfill_history_details,
    )
    print(json.dumps(output["today"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
