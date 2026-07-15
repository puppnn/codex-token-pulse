from __future__ import annotations

import base64
import json
import math
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from typing import Any
from urllib import error, parse, request


SOURCE_DIR = Path(__file__).resolve().parent
IS_FROZEN = bool(getattr(sys, "frozen", False))
INSTALL_DIR = Path(sys.executable).resolve().parent if IS_FROZEN else SOURCE_DIR
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
ENV_FILES = list(
    dict.fromkeys(
        [
            APP_DIR / ".env",
            INSTALL_DIR / ".env",
            INSTALL_DIR / "deploy" / ".env",
            SOURCE_DIR.parent.parent / "deploy" / ".env",
        ]
    )
)
DEFAULT_BASE_URL = "http://127.0.0.1:8080"
REFRESH_SECONDS = 3
CLIENT_USAGE_CACHE_SECONDS = int(os.environ.get("SUB2API_CLIENT_USAGE_CACHE_SECONDS", "10"))
CLIENT_USAGE_EXPORT_TIMEOUT_SECONDS = int(os.environ.get("SUB2API_CLIENT_USAGE_EXPORT_TIMEOUT_SECONDS", "90"))
ACCOUNT_WINDOW_CACHE_SECONDS = int(os.environ.get("SUB2API_ACCOUNT_WINDOW_CACHE_SECONDS", "60"))
ACCOUNT_STATS_CACHE_SECONDS = int(os.environ.get("SUB2API_ACCOUNT_STATS_CACHE_SECONDS", "300"))
LOCAL_ACTIVE_WINDOW_SECONDS = int(os.environ.get("SUB2API_LOCAL_ACTIVE_WINDOW_SECONDS", "300"))
ACCOUNT_DISPLAY_ACTIVITY_DAYS = 30
LIVE_ACTIVE_STALE_SECONDS = int(os.environ.get("TOKEN_PULSE_LIVE_ACTIVE_STALE_SECONDS", "7200"))
LIVE_ACTIVE_TAIL_BYTES = int(os.environ.get("TOKEN_PULSE_LIVE_ACTIVE_TAIL_BYTES", str(2 * 1024 * 1024)))
LIVE_ACTIVE_MAX_FILES = int(os.environ.get("TOKEN_PULSE_LIVE_ACTIVE_MAX_FILES", "64"))
LIVE_ACCOUNT_MATCH_SECONDS = float(os.environ.get("TOKEN_PULSE_LIVE_ACCOUNT_MATCH_SECONDS", "300"))
LIVE_ACCOUNT_MARKER_LIMIT = int(os.environ.get("TOKEN_PULSE_LIVE_ACCOUNT_MARKER_LIMIT", "4096"))
LIVE_USAGE_WATCH_INTERVAL_MS = max(
    50,
    int(os.environ.get("TOKEN_PULSE_LIVE_USAGE_WATCH_INTERVAL_MS", "100")),
)
LIVE_USAGE_WATCH_IDLE_INTERVAL_MS = max(
    LIVE_USAGE_WATCH_INTERVAL_MS,
    int(os.environ.get("TOKEN_PULSE_LIVE_USAGE_WATCH_IDLE_INTERVAL_MS", "250")),
)
LIVE_USAGE_WATCH_COLD_INTERVAL_MS = max(
    LIVE_USAGE_WATCH_IDLE_INTERVAL_MS,
    int(os.environ.get("TOKEN_PULSE_LIVE_USAGE_WATCH_COLD_INTERVAL_MS", "500")),
)
LIVE_USAGE_WATCH_HOT_SECONDS = 10.0
LIVE_USAGE_WATCH_IDLE_SECONDS = 60.0
LIVE_USAGE_WATCH_FULL_SCAN_SECONDS = max(
    5.0,
    float(os.environ.get("TOKEN_PULSE_LIVE_USAGE_FULL_SCAN_SECONDS", "30")),
)
LIVE_USAGE_WATCH_HOT_FILE_LIMIT = 16
AUTH_SWITCH_WATCH_INTERVAL_MS = max(
    500,
    int(os.environ.get("TOKEN_PULSE_AUTH_SWITCH_WATCH_INTERVAL_MS", "1000")),
)
LIVE_USAGE_WATCH_READ_BYTES = max(
    4096,
    int(os.environ.get("TOKEN_PULSE_LIVE_USAGE_WATCH_READ_BYTES", str(256 * 1024))),
)
LIVE_USAGE_WATCH_OVERLAP_BYTES = 8 * 1024
LIVE_USAGE_MAX_SINGLE_EVENT_TOKENS = max(
    1,
    int(os.environ.get("CLIENT_USAGE_MAX_SINGLE_EVENT_TOKENS", "2000000")),
)
LIVE_USAGE_VERIFY_THRESHOLD_TOKENS = max(
    LIVE_USAGE_MAX_SINGLE_EVENT_TOKENS,
    int(os.environ.get("TOKEN_PULSE_LIVE_VERIFY_THRESHOLD_TOKENS", "50000000")),
)
LIVE_USAGE_VERIFY_WINDOW_SECONDS = max(
    1.0,
    float(os.environ.get("TOKEN_PULSE_LIVE_VERIFY_WINDOW_SECONDS", "5")),
)
LIVE_USAGE_VERIFY_DELAY_MS = max(
    1_000,
    int(os.environ.get("TOKEN_PULSE_LIVE_VERIFY_DELAY_MS", "1000")),
)
LIVE_USAGE_EXPORT_IDLE_SECONDS = max(
    5,
    int(os.environ.get("TOKEN_PULSE_LIVE_USAGE_EXPORT_IDLE_SECONDS", "30")),
)
QUOTA_REFRESH_SECONDS = max(
    10,
    int(os.environ.get("TOKEN_PULSE_QUOTA_REFRESH_SECONDS", "10")),
)
QUOTA_REFRESH_TIMEOUT_SECONDS = max(
    10,
    int(os.environ.get("TOKEN_PULSE_QUOTA_REFRESH_TIMEOUT_SECONDS", "60")),
)
FULL_USAGE_REFRESH_MAX_STALE_SECONDS = max(
    QUOTA_REFRESH_SECONDS,
    int(os.environ.get("TOKEN_PULSE_FULL_USAGE_MAX_STALE_SECONDS", "600")),
)
FULL_USAGE_REFRESH_RETRY_SECONDS = max(
    30,
    int(os.environ.get("TOKEN_PULSE_FULL_USAGE_RETRY_SECONDS", "60")),
)
TOKEN_FLOW_ANIMATION_INTERVAL_MS = 16
TOKEN_FLOW_FULL_REDRAW_INTERVAL_MS = 90
TOKEN_FLOW_TRACE_TRAVEL_SECONDS = 10.0
TOKEN_FLOW_METER_HEAD_BANDS = 6
TOKEN_FLOW_METER_HEAD_HEIGHT = 8.0
TOKEN_DELTA_BADGE_DURATION_SECONDS = 30.0
TOKEN_DELTA_BADGE_MERGE_SECONDS = 0.35
COCKPIT_REQUEST_LOG_DB = Path(
    os.environ.get("TOKEN_PULSE_COCKPIT_REQUEST_LOG_DB")
    or Path.home() / ".antigravity_cockpit" / "codex_local_access_logs.sqlite"
)
CLIENT_USAGE_EXPORT = Path(
    os.environ.get("CLIENT_USAGE_EXPORT")
    or (
        INSTALL_DIR / "TokenPulseExporter.exe"
        if IS_FROZEN
        else APP_DIR / "client_usage_export.py"
    )
)
if not CLIENT_USAGE_EXPORT.exists():
    fallback_export = APP_DIR.parent / "client-token-importer" / "client_usage_export.py"
    if fallback_export.exists():
        CLIENT_USAGE_EXPORT = fallback_export
CLIENT_USAGE_JSON = Path(os.environ.get("CLIENT_USAGE_JSON") or APP_DIR / "client_usage_today.json")
if not CLIENT_USAGE_JSON.exists():
    fallback_json = APP_DIR.parent / "client-token-importer" / "client_usage_today.json"
    if fallback_json.exists():
        CLIENT_USAGE_JSON = fallback_json
MODEL_PRICE_CACHE_JSON = Path(
    os.environ.get("CLIENT_USAGE_MODEL_PRICE_CACHE")
    or APP_DIR / "client_usage_model_prices.json"
)
_LIVE_MODEL_PRICE_CACHE: tuple[str, int, dict[str, dict[str, float]]] | None = None
CLIENT_USAGE_PYTHON = os.environ.get("SUB2API_CLIENT_USAGE_PYTHON") or sys.executable
if Path(CLIENT_USAGE_PYTHON).name.lower() == "pythonw.exe":
    console_python = Path(CLIENT_USAGE_PYTHON).with_name("python.exe")
    if console_python.exists():
        CLIENT_USAGE_PYTHON = str(console_python)
USAGE_HISTORY_JSON = Path(os.environ.get("SUB2API_USAGE_HISTORY_JSON") or APP_DIR / "usage_history.json")
ACCOUNT_TYPE_HISTORY_JSON = Path(
    os.environ.get("TOKEN_PULSE_ACCOUNT_TYPE_HISTORY_JSON")
    or APP_DIR / "client_usage_account_types.json"
)
LIVE_USAGE_CHECKPOINT_JSON = Path(
    os.environ.get("TOKEN_PULSE_LIVE_USAGE_CHECKPOINT_JSON")
    or APP_DIR / "client_usage_live_checkpoint.json"
)
LIVE_USAGE_CHECKPOINT_SCHEMA = 2
LIVE_USAGE_CHECKPOINT_WRITE_SECONDS = max(
    0.25,
    float(os.environ.get("TOKEN_PULSE_LIVE_CHECKPOINT_WRITE_SECONDS", "1")),
)
LIVE_USAGE_RECONCILE_DELAY_MS = max(
    1_000,
    int(os.environ.get("TOKEN_PULSE_LIVE_RECONCILE_DELAY_MS", "5000")),
)
LIVE_USAGE_INITIAL_RECHECK_MS = max(
    LIVE_USAGE_RECONCILE_DELAY_MS,
    int(os.environ.get("TOKEN_PULSE_LIVE_INITIAL_RECHECK_MS", "10000")),
)
LIVE_USAGE_NEW_ROLLOUT_WATCH_SECONDS = max(
    5.0,
    float(os.environ.get("TOKEN_PULSE_NEW_ROLLOUT_WATCH_SECONDS", "20")),
)
LIVE_USAGE_RECONCILE_MIN_INTERVAL_SECONDS = max(
    10.0,
    float(os.environ.get("TOKEN_PULSE_RECONCILE_MIN_INTERVAL_SECONDS", "30")),
)
CLIENT_USAGE_ROUTE_LABELS_JSON = Path(
    os.environ.get("CLIENT_USAGE_ROUTE_LABELS_JSON") or APP_DIR / "client_usage_route_labels.json"
)
AUTH_SWITCH_EVENTS_PATH = Path(
    os.environ.get("CLIENT_USAGE_AUTH_SWITCH_EVENTS")
    or APP_DIR / "client_usage_auth_switch_events.jsonl"
)
CN_TZ = timezone(timedelta(hours=8), "CST")
DISPLAY_TIMEZONE = "Asia/Shanghai"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}
SINGLE_INSTANCE_MUTEX_NAME = os.environ.get(
    "TOKEN_PULSE_MUTEX_NAME",
    "Local\\TokenPulseFloatingMonitor",
)
ERROR_ALREADY_EXISTS = 183


def acquire_single_instance_mutex() -> int | None:
    if os.name != "nt":
        return -1
    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p)
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        kernel32.CloseHandle.restype = ctypes.c_bool
        ctypes.set_last_error(0)
        handle = kernel32.CreateMutexW(None, False, SINGLE_INSTANCE_MUTEX_NAME)
        if not handle:
            return -1
        if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)
            return None
        return int(handle)
    except (AttributeError, OSError):
        return -1


def release_single_instance_mutex(handle: int | None) -> None:
    if handle in {None, -1} or os.name != "nt":
        return
    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        kernel32.CloseHandle.restype = ctypes.c_bool
        kernel32.CloseHandle(ctypes.c_void_p(handle))
    except (AttributeError, OSError):
        pass


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def read_env_files(paths: list[Path]) -> dict[str, str]:
    values: dict[str, str] = {}
    for path in paths:
        values.update(read_env_file(path))
    return values


def env_bool(values: dict[str, str], key: str, default: bool = False) -> bool:
    raw = os.environ.get(key)
    if raw is None:
        raw = values.get(key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def normalize_url(value: str | None) -> str:
    url = (value or "").strip().strip('"').strip("'")
    if not url:
        return ""
    if "://" not in url:
        url = "http://" + url
    return url.rstrip("/")


def same_endpoint(left: str, right: str) -> bool:
    left = normalize_url(left)
    right = normalize_url(right)
    if not left or not right:
        return False
    try:
        left_url = parse.urlparse(left)
        right_url = parse.urlparse(right)
    except Exception:
        return False
    left_host = (left_url.hostname or "").lower()
    right_host = (right_url.hostname or "").lower()
    left_port = left_url.port or (443 if left_url.scheme == "https" else 80)
    right_port = right_url.port or (443 if right_url.scheme == "https" else 80)
    if left_port != right_port:
        return False
    if left_host == right_host:
        return True
    return left_host in LOCAL_HOSTS and right_host in LOCAL_HOSTS


def strip_url_path(url: str) -> str:
    normalized = normalize_url(url)
    if not normalized:
        return ""
    try:
        parts = parse.urlparse(normalized)
    except Exception:
        return normalized
    netloc = parts.netloc
    if not netloc:
        return normalized
    return parse.urlunparse((parts.scheme or "http", netloc, "", "", "", ""))


def extract_json_urls(value: Any) -> list[str]:
    urls: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            key_text = str(key).lower()
            if isinstance(child, str) and any(part in key_text for part in ("base_url", "api_base", "api_base_url")):
                urls.append(child)
            urls.extend(extract_json_urls(child))
    elif isinstance(value, list):
        for child in value:
            urls.extend(extract_json_urls(child))
    return urls


def read_codex_toml_urls(path: Path) -> list[str]:
    if not path.exists():
        return []
    active_provider = ""
    current_section = ""
    root_urls: list[str] = []
    provider_urls: dict[str, list[str]] = {}
    key_value = re.compile(r"^([A-Za-z0-9_.-]+)\s*=\s*[\"']([^\"']+)[\"']")
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return []
    for raw_line in lines:
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            current_section = line.strip("[]").strip()
            continue
        match = key_value.match(line)
        if not match:
            continue
        key, value = match.groups()
        if key == "model_provider" and not current_section:
            active_provider = value
            continue
        if key != "base_url":
            continue
        if current_section.startswith("model_providers."):
            provider = current_section.split(".", 1)[1]
            provider_urls.setdefault(provider, []).append(value)
        elif not current_section:
            root_urls.append(value)
    urls: list[str] = []
    if active_provider:
        urls.extend(provider_urls.get(active_provider, []))
    urls.extend(root_urls)
    for provider, values in provider_urls.items():
        if provider != active_provider:
            urls.extend(values)
    return urls


def read_codex_active_provider(path: Path) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return ""
    for raw_line in lines:
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("["):
            break
        match = re.match(r"^model_provider\s*=\s*[\"']([^\"']+)[\"']", line)
        if match:
            return match.group(1).strip()
    return ""


def decode_codex_jwt_payload(value: Any) -> dict[str, Any]:
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
    claims = decode_codex_jwt_payload(tokens.get("id_token"))
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
    return str(
        data.get("account_id")
        or tokens.get("account_id")
        or claims.get("account_id")
        or claims.get("chatgpt_account_id")
        or auth_claims.get("chatgpt_account_id")
        or auth_claims.get("account_id")
        or data.get("api_provider_name")
        or data.get("api_provider_id")
        or ""
    ).strip()


def current_codex_auth_snapshot() -> tuple[str, Path | None, datetime | None]:
    codex_dir = Path(os.path.expanduser("~")) / ".codex"
    provider = read_codex_active_provider(codex_dir / "config.toml").lower()
    uses_cockpit = "codex_local_access" in provider or "api-service" in provider
    names = (".cockpit_codex_auth.json",) if uses_cockpit else ("auth.json",)
    for name in names:
        path = codex_dir / name
        try:
            before = path.stat()
            data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
            after = path.stat()
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            continue
        if before.st_mtime_ns != after.st_mtime_ns or before.st_size != after.st_size:
            continue
        identity = codex_auth_identity(data)
        if identity:
            changed_at = datetime.fromtimestamp(after.st_mtime, tz=CN_TZ)
            return identity, path, changed_at
    return "", None, None


def current_codex_auth_identity() -> str:
    identity, _path, _changed_at = current_codex_auth_snapshot()
    return identity


def append_codex_auth_switch_event(
    identity: str,
    changed_at: datetime | None = None,
) -> bool:
    identity = str(identity or "").strip()
    if not identity:
        return False
    label = identity if identity.startswith("Codex local - ") else f"Codex local - {identity}"

    try:
        existing = AUTH_SWITCH_EVENTS_PATH.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        existing = ""
    for line in reversed(existing.splitlines()):
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict) and str(item.get("label") or "").strip():
            if str(item.get("label") or "").strip() == label:
                return False
            break

    now = datetime.now(CN_TZ)
    event_at = changed_at or now
    if event_at.tzinfo is None:
        event_at = event_at.replace(tzinfo=CN_TZ)
    else:
        event_at = event_at.astimezone(CN_TZ)
    if event_at > now + timedelta(minutes=5):
        event_at = now
    record = {
        "at": event_at.isoformat(timespec="milliseconds"),
        "label": label,
    }
    try:
        AUTH_SWITCH_EVENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with AUTH_SWITCH_EVENTS_PATH.open("a", encoding="utf-8", newline="") as handle:
            if existing and not existing.endswith(("\n", "\r")):
                handle.write("\n")
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
            handle.write("\n")
    except OSError:
        return False
    return True


def detect_codex_base_urls() -> list[str]:
    urls: list[str] = []
    for key in ("OPENAI_BASE_URL", "OPENAI_API_BASE", "OPENAI_API_BASE_URL", "CODEX_BASE_URL"):
        value = os.environ.get(key)
        if value:
            urls.append(value)
    codex_dir = Path(os.path.expanduser("~")) / ".codex"
    urls.extend(read_codex_toml_urls(codex_dir / "config.toml"))
    for name in ("auth.json", ".cockpit_codex_auth.json"):
        path = codex_dir / name
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            continue
        urls.extend(extract_json_urls(data))
    unique: list[str] = []
    for url in urls:
        normalized = normalize_url(url)
        if normalized and normalized not in unique:
            unique.append(normalized)
    return unique


def current_cockpit_account_label() -> str:
    return current_codex_auth_identity()


def local_provider_display_name(provider_name: str) -> str:
    name = (provider_name or "Local client").strip() or "Local client"
    if name.lower().startswith("codex local - "):
        return name
    label = current_cockpit_account_label()
    if label and name.lower().startswith("codex"):
        return f"Codex local - {label}"
    return name


def ranking_account_display_name(account_name: str) -> str:
    name = (account_name or "-").strip() or "-"
    prefixes = (
        "LOCAL - ",
        "Codex local - ",
        "Codex OAuth - ",
        "Relay - ",
        "SUB2 - ",
    )
    for _ in range(len(prefixes)):
        matched = False
        for prefix in prefixes:
            if name.lower().startswith(prefix.lower()):
                name = name[len(prefix):].strip() or name
                matched = True
                break
        if not matched:
            break
    aliases = {
        "api-service-local": "API \u670d\u52a1",
        "claude local": "Claude",
        "local client": "\u5ba2\u6237\u7aef",
        "local client logs": "\u5ba2\u6237\u7aef\u65e5\u5fd7",
    }
    if name.lower() in aliases:
        return aliases[name.lower()]
    return name


def normalize_account_plan_type(value: Any) -> str:
    raw = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if not raw:
        return ""
    aliases = {
        "k12": "K12",
        "edu": "EDU",
        "education": "EDU",
        "student": "EDU",
        "plus": "PLUS",
        "chatgpt_plus": "PLUS",
        "pro": "PRO",
        "chatgpt_pro": "PRO",
        "team": "TEAM",
        "business": "BUSINESS",
        "enterprise": "ENTERPRISE",
        "free": "FREE",
        "api": "API KEY",
        "api_key": "API KEY",
        "apikey": "API KEY",
    }
    return aliases.get(raw, raw.upper().replace("_", " ")[:14])


def codex_auth_plan_type(data: dict[str, Any] | None) -> str:
    if not isinstance(data, dict):
        return ""
    tokens = data.get("tokens") if isinstance(data.get("tokens"), dict) else {}
    claims = decode_codex_jwt_payload(tokens.get("id_token"))
    auth_claims = claims.get("https://api.openai.com/auth")
    auth_claims = auth_claims if isinstance(auth_claims, dict) else {}
    for source in (data, tokens, claims, auth_claims):
        for key in (
            "plan_type",
            "chatgpt_plan_type",
            "subscription_type",
            "account_type",
            "subscription",
        ):
            label = normalize_account_plan_type(source.get(key))
            if label:
                return label
    return ""


_ACCOUNT_TYPE_CACHE_SIGNATURE: tuple[tuple[str, int], ...] = ()
_ACCOUNT_TYPE_CACHE: dict[str, str] = {}


def _account_type_lookup_key(value: Any) -> str:
    return ranking_account_display_name(str(value or "")).strip().casefold()


def _account_type_source_signature(paths: list[Path]) -> tuple[tuple[str, int], ...]:
    signature: list[tuple[str, int]] = []
    for path in paths:
        try:
            signature.append((str(path), int(path.stat().st_mtime_ns)))
        except OSError:
            signature.append((str(path), -1))
    return tuple(signature)


def load_account_type_history() -> dict[str, str]:
    try:
        data = json.loads(ACCOUNT_TYPE_HISTORY_JSON.read_text(encoding="utf-8", errors="ignore"))
    except (OSError, json.JSONDecodeError):
        return {}
    rows = data.get("accounts") if isinstance(data, dict) else None
    if not isinstance(rows, dict):
        return {}
    result: dict[str, str] = {}
    for identity, value in rows.items():
        raw_label = value.get("plan_type") if isinstance(value, dict) else value
        key = _account_type_lookup_key(identity)
        label = normalize_account_plan_type(raw_label)
        if key and key != "-" and label:
            result[key] = label
    return result


def save_account_type_history(account_types: dict[str, str]) -> bool:
    saved = load_account_type_history()
    normalized = {
        key: normalize_account_plan_type(label)
        for identity, label in account_types.items()
        if (key := _account_type_lookup_key(identity)) and key != "-"
    }
    normalized = {key: label for key, label in normalized.items() if label}
    if normalized == saved:
        return False
    now_text = datetime.now(CN_TZ).isoformat(timespec="seconds")
    try:
        existing_data = json.loads(
            ACCOUNT_TYPE_HISTORY_JSON.read_text(encoding="utf-8", errors="ignore")
        )
    except (OSError, json.JSONDecodeError):
        existing_data = {}
    existing_rows = existing_data.get("accounts") if isinstance(existing_data, dict) else {}
    existing_rows = existing_rows if isinstance(existing_rows, dict) else {}
    rows: dict[str, dict[str, str]] = {}
    for identity, label in sorted(normalized.items()):
        previous = existing_rows.get(identity)
        previous_label = normalize_account_plan_type(
            previous.get("plan_type") if isinstance(previous, dict) else previous
        )
        previous_at = str(previous.get("updated_at") or "") if isinstance(previous, dict) else ""
        rows[identity] = {
            "plan_type": label,
            "updated_at": previous_at if previous_label == label and previous_at else now_text,
        }
    write_json_atomic(
        ACCOUNT_TYPE_HISTORY_JSON,
        {
            "schema": 1,
            "updated_at": now_text,
            "accounts": rows,
        },
    )
    return True


def _add_manifest_account_types(
    path: Path,
    account_types: dict[str, str],
    *,
    overwrite: bool,
) -> None:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except (OSError, json.JSONDecodeError):
        return
    rows = manifest.get("accounts") if isinstance(manifest, dict) else None
    for account in rows if isinstance(rows, list) else []:
        if not isinstance(account, dict):
            continue
        plan_label = normalize_account_plan_type(
            account.get("plan_type")
            or account.get("subscription_type")
            or account.get("account_type")
        )
        if not plan_label:
            continue
        for identity in (
            account.get("email"),
            account.get("api_provider_name"),
            account.get("name"),
            account.get("id"),
        ):
            key = _account_type_lookup_key(identity)
            if not key or key == "-":
                continue
            if overwrite or key not in account_types:
                account_types[key] = plan_label


def _add_auth_account_type(
    path: Path,
    account_types: dict[str, str],
    *,
    overwrite: bool,
) -> None:
    try:
        auth_data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(auth_data, dict):
        return
    identity = codex_auth_identity(auth_data)
    plan_label = codex_auth_plan_type(auth_data)
    key = _account_type_lookup_key(identity)
    if key and key != "-" and plan_label and (overwrite or key not in account_types):
        account_types[key] = plan_label


def local_account_type_map() -> dict[str, str]:
    global _ACCOUNT_TYPE_CACHE_SIGNATURE, _ACCOUNT_TYPE_CACHE
    cockpit_root = Path.home() / ".antigravity_cockpit"
    manifest_path = cockpit_root / "codex_accounts.json"
    sidecar_auth_dir = cockpit_root / "codex_local_access_sidecar" / "auths"
    current_auth_paths = [
        Path.home() / ".codex" / "auth.json",
        Path.home() / ".codex" / ".cockpit_codex_auth.json",
    ]
    signature_paths = [
        ACCOUNT_TYPE_HISTORY_JSON,
        manifest_path,
        cockpit_root,
        sidecar_auth_dir,
        *current_auth_paths,
    ]
    signature_key = _account_type_source_signature(signature_paths)
    if signature_key == _ACCOUNT_TYPE_CACHE_SIGNATURE:
        return _ACCOUNT_TYPE_CACHE

    account_types = load_account_type_history()

    # Cockpit keeps deleted-account snapshots and sidecar auth backups. Import
    # them only as missing history; current metadata below remains authoritative.
    try:
        manifest_backups = sorted(
            (
                path
                for path in cockpit_root.glob("codex_accounts.json*")
                if path != manifest_path and path.is_file()
            ),
            key=lambda path: path.stat().st_mtime_ns,
        )
    except OSError:
        manifest_backups = []
    for backup_path in manifest_backups:
        _add_manifest_account_types(backup_path, account_types, overwrite=False)

    try:
        sidecar_auth_paths = sorted(
            (path for path in sidecar_auth_dir.glob("*.json*") if path.is_file()),
            key=lambda path: path.stat().st_mtime_ns,
        )
    except OSError:
        sidecar_auth_paths = []
    for auth_path in sidecar_auth_paths:
        _add_auth_account_type(auth_path, account_types, overwrite=False)

    _add_manifest_account_types(manifest_path, account_types, overwrite=True)
    for auth_path in current_auth_paths:
        _add_auth_account_type(auth_path, account_types, overwrite=True)

    if save_account_type_history(account_types):
        signature_key = _account_type_source_signature(signature_paths)

    _ACCOUNT_TYPE_CACHE_SIGNATURE = signature_key
    _ACCOUNT_TYPE_CACHE = account_types
    return account_types


def account_type_label(account: dict[str, Any] | None = None, name: Any = "") -> str:
    row = account if isinstance(account, dict) else {}
    for key in (
        "plan_type",
        "subscription_type",
        "account_type",
        "subscription",
    ):
        label = normalize_account_plan_type(row.get(key))
        if label:
            return label
    candidates = (
        name,
        row.get("provider"),
        row.get("name"),
        row.get("email"),
    )
    type_map = local_account_type_map()
    for candidate in candidates:
        lookup_key = _account_type_lookup_key(candidate)
        if lookup_key in type_map:
            return type_map[lookup_key]
    display_name = ranking_account_display_name(
        str(next((candidate for candidate in candidates if candidate), ""))
    ).casefold()
    if row.get("is_pool_aggregate") or display_name == "api \u670d\u52a1":
        return "\u8d26\u53f7\u6c60"
    if "api-key" in display_name or "api key" in display_name:
        return "API KEY"
    if display_name == "claude":
        return "CLAUDE"
    if "@" in display_name:
        return "\u672a\u77e5"
    return ""


def balanced_active_row_capacity(
    window_height: int,
    default_height: int,
    row_height: int = 26,
    base_rows: int = 3,
) -> int:
    """Give one third of added page height to the active-account list."""
    row_height = max(1, int(row_height))
    base_rows = max(1, int(base_rows))
    extra_height = max(0, int(window_height) - int(default_height))
    active_extra_budget = extra_height // 3
    return base_rows + active_extra_budget // row_height


def compact_number(value: float | int | None) -> str:
    number = float(value or 0)
    sign = "-" if number < 0 else ""
    number = abs(number)
    if number >= 1_000_000_000:
        return f"{sign}{number / 1_000_000_000:.1f}B"
    if number >= 1_000_000:
        return f"{sign}{number / 1_000_000:.1f}M"
    if number >= 1_000:
        return f"{sign}{number / 1_000:.1f}K"
    return f"{sign}{int(number):,}"


def exact_token_count(value: float | int | None) -> str:
    try:
        number = int(float(value or 0))
    except (TypeError, ValueError, OverflowError):
        number = 0
    return f"{number:,}"


def money(value: float | int | None) -> str:
    number = float(value or 0)
    if 0 < number < 0.01:
        return f"${number:.6f}"
    return f"${number:.2f}"


def _load_live_model_prices() -> dict[str, dict[str, float]]:
    global _LIVE_MODEL_PRICE_CACHE
    try:
        modified_ns = int(MODEL_PRICE_CACHE_JSON.stat().st_mtime_ns)
        cache_path = str(MODEL_PRICE_CACHE_JSON.resolve())
    except OSError:
        return {}
    if (
        _LIVE_MODEL_PRICE_CACHE is not None
        and _LIVE_MODEL_PRICE_CACHE[0] == cache_path
        and _LIVE_MODEL_PRICE_CACHE[1] == modified_ns
    ):
        return _LIVE_MODEL_PRICE_CACHE[2]
    try:
        payload = json.loads(MODEL_PRICE_CACHE_JSON.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    raw_models = payload.get("models") if isinstance(payload, dict) else {}
    prices: dict[str, dict[str, float]] = {}
    for raw_name, raw_detail in raw_models.items() if isinstance(raw_models, dict) else []:
        if not isinstance(raw_detail, dict):
            continue
        detail: dict[str, float] = {}
        for field in (
            "input_cost_per_token",
            "cache_read_input_token_cost",
            "output_cost_per_token",
        ):
            try:
                rate = max(0.0, float(raw_detail.get(field) or 0.0))
            except (TypeError, ValueError):
                rate = 0.0
            if rate > 0:
                detail[field] = rate
        name = str(raw_name or "").strip().lower()
        if name and detail.get("input_cost_per_token") and detail.get("output_cost_per_token"):
            prices[name] = detail
            if name.startswith(("openai/", "anthropic/")):
                prices.setdefault(name.split("/", 1)[1], detail)
    _LIVE_MODEL_PRICE_CACHE = (cache_path, modified_ns, prices)
    return prices


def estimate_live_usage_cost(
    usage: dict[str, Any],
    model: str,
    *,
    fallback_cost_per_token: float = 0.0,
) -> float:
    raw_input = max(0, int(usage.get("input_tokens") or 0))
    cached_input = min(raw_input, max(0, int(usage.get("cached_tokens") or 0)))
    output_tokens = max(0, int(usage.get("output_tokens") or 0))
    total_tokens = max(0, int(usage.get("total_tokens") or raw_input + output_tokens))
    normalized_model = str(model or "").strip().lower()
    candidates = [normalized_model]
    if "/" in normalized_model:
        candidates.append(normalized_model.split("/", 1)[1])
    prices = _load_live_model_prices()
    detail = next((prices[name] for name in candidates if name in prices), None)
    if detail is not None:
        input_rate = float(detail.get("input_cost_per_token") or 0.0)
        cache_rate = float(detail.get("cache_read_input_token_cost") or input_rate)
        output_rate = float(detail.get("output_cost_per_token") or 0.0)
        return max(
            0.0,
            (raw_input - cached_input) * input_rate
            + cached_input * cache_rate
            + output_tokens * output_rate,
        )
    return max(0.0, total_tokens * max(0.0, float(fallback_cost_per_token or 0.0)))


def quota_color(utilization: float | int | None) -> str:
    try:
        value = float(utilization or 0)
    except (TypeError, ValueError):
        value = 0
    if value >= 90:
        return Theme.accent_red
    if value >= 60:
        return Theme.ag_warn
    return Theme.accent_green


def quota_reset_text(value: str | None) -> str:
    target = _parse_time(value)
    if target is None:
        return ""
    seconds = int((target - datetime.now(timezone.utc)).total_seconds())
    if seconds <= 0:
        return "\u5f85\u5237\u65b0"
    minutes = max(1, seconds // 60)
    days, minutes = divmod(minutes, 24 * 60)
    hours, minutes = divmod(minutes, 60)
    if days:
        return f"{days}d {hours}h \u540e\u91cd\u7f6e"
    if hours:
        return f"{hours}h {minutes}m \u540e\u91cd\u7f6e"
    return f"{minutes}m \u540e\u91cd\u7f6e"


def today_key() -> str:
    return datetime.now(CN_TZ).date().isoformat()


def date_key(days_ago: int) -> str:
    return (datetime.now(CN_TZ).date() - timedelta(days=days_ago)).isoformat()


def trend_chart_day_label(date_value: str, index: int, total: int = 7) -> str:
    """Return a compact, unambiguous label for a daily trend bar."""
    if index == total - 1:
        return "今日"
    if index == total - 2:
        return "昨日"
    try:
        parsed = date.fromisoformat(str(date_value))
        return f"{parsed.month}/{parsed.day}"
    except (TypeError, ValueError):
        return "-"


def load_usage_history() -> dict[str, Any]:
    if not USAGE_HISTORY_JSON.exists():
        return {"schema": 1, "days": {}}
    try:
        data = json.loads(USAGE_HISTORY_JSON.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return {"schema": 1, "days": {}}
    if not isinstance(data, dict):
        return {"schema": 1, "days": {}}
    days = data.get("days")
    if not isinstance(days, dict):
        data["days"] = {}
    data["schema"] = int(data.get("schema") or 1)
    return data


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


def token_mix_from_client_usage(client_usage: dict[str, Any] | None) -> dict[str, int]:
    mix = {
        "input": 0,
        "cached": 0,
        "cache_create": 0,
        "output": 0,
    }
    if not isinstance(client_usage, dict):
        return mix
    providers = client_usage.get("providers")
    rows = providers if isinstance(providers, list) else [client_usage]
    for row in rows:
        if not isinstance(row, dict):
            continue
        mix["input"] += int(row.get("input_tokens") or 0)
        mix["cached"] += int(row.get("cached_input_tokens") or 0)
        mix["cache_create"] += int(row.get("cache_creation_input_tokens") or 0)
        mix["output"] += int(row.get("output_tokens") or 0)
    if isinstance(providers, list):
        mix["input"] = max(mix["input"], int(client_usage.get("input_tokens") or 0))
        mix["cached"] = max(mix["cached"], int(client_usage.get("cached_input_tokens") or 0))
        mix["cache_create"] = max(mix["cache_create"], int(client_usage.get("cache_creation_input_tokens") or 0))
        mix["output"] = max(mix["output"], int(client_usage.get("output_tokens") or 0))
    return mix


def detailed_usage_from_client_usage(client_usage: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(client_usage, dict):
        return {"models": {}, "providers": []}
    providers = client_usage.get("providers")
    if not isinstance(providers, list):
        return {"models": {}, "providers": []}
    model_totals: dict[str, int] = {}
    provider_rows: list[dict[str, Any]] = []
    for provider in providers:
        if not isinstance(provider, dict):
            continue
        requests_count = int(provider.get("requests") or 0)
        tokens = int(provider.get("tokens") or 0)
        cost = float(provider.get("cost") or 0)
        if requests_count <= 0 and tokens <= 0 and cost <= 0:
            continue
        models = provider.get("models") if isinstance(provider.get("models"), dict) else {}
        normalized_models: dict[str, int] = {}
        for model, amount in models.items():
            value = max(0, int(amount or 0))
            if value <= 0:
                continue
            name = str(model or "unknown")
            normalized_models[name] = normalized_models.get(name, 0) + value
            model_totals[name] = model_totals.get(name, 0) + value
        provider_rows.append(
            {
                "name": str(provider.get("name") or "Local client"),
                "plan_type": str(provider.get("plan_type") or ""),
                "requests": requests_count,
                "tokens": tokens,
                "cost": round(cost, 6),
                "models": normalized_models,
            }
        )
    return {"models": model_totals, "providers": provider_rows}


def detailed_usage_from_account_rows(rows: list[dict[str, Any]] | None) -> dict[str, Any]:
    aggregated: dict[str, dict[str, Any]] = {}
    for source in rows if isinstance(rows, list) else []:
        if not isinstance(source, dict) or source.get("is_pool_aggregate"):
            continue
        requests_count = max(0, int(source.get("requests") or 0))
        tokens = max(0, int(source.get("tokens") or 0))
        cost = max(0.0, float(source.get("cost") or 0))
        if requests_count <= 0 and tokens <= 0 and cost <= 0:
            continue
        name = str(source.get("name") or "Local client")
        key = account_display_key(name) or name.casefold()
        target = aggregated.setdefault(
            key,
            {
                "name": name,
                "plan_type": str(source.get("plan_type") or ""),
                "requests": 0,
                "tokens": 0,
                "cost": 0.0,
                "models": {},
            },
        )
        target["requests"] += requests_count
        target["tokens"] += tokens
        target["cost"] += cost
        models = source.get("models") if isinstance(source.get("models"), dict) else {}
        for model, amount in models.items():
            value = max(0, int(amount or 0))
            if value > 0:
                model_name = str(model or "unknown")
                target["models"][model_name] = target["models"].get(model_name, 0) + value

    model_totals: dict[str, int] = {}
    providers = list(aggregated.values())
    for provider in providers:
        provider["cost"] = round(float(provider.get("cost") or 0), 6)
        for model, amount in provider["models"].items():
            model_totals[model] = model_totals.get(model, 0) + int(amount or 0)
    return {"models": model_totals, "providers": providers}


def detailed_usage_from_state(state: Any) -> dict[str, Any]:
    client_details = detailed_usage_from_client_usage(
        state.client_usage if isinstance(getattr(state, "client_usage", None), dict) else None
    )
    account_details = detailed_usage_from_account_rows(
        state.top_accounts if isinstance(getattr(state, "top_accounts", None), list) else None
    )
    account_rows = account_details.get("providers") or []
    if not account_rows:
        return client_details
    target_tokens = max(0, int(getattr(state, "today_tokens", 0) or 0))
    client_tokens = sum(int(row.get("tokens") or 0) for row in client_details.get("providers") or [])
    account_tokens = sum(int(row.get("tokens") or 0) for row in account_rows)
    return (
        account_details
        if abs(account_tokens - target_tokens) <= abs(client_tokens - target_tokens)
        else client_details
    )


def summarize_usage_history(history: dict[str, Any]) -> dict[str, Any]:
    days = history.get("days") if isinstance(history, dict) else {}
    if not isinstance(days, dict):
        days = {}
    series: list[dict[str, Any]] = []
    for offset in range(6, -1, -1):
        key = date_key(offset)
        row = days.get(key) if isinstance(days.get(key), dict) else {}
        series.append(
            {
                "date": key,
                "cost": float(row.get("cost") or 0),
                "tokens": int(row.get("tokens") or 0),
                "requests": int(row.get("requests") or 0),
            }
        )
    today = series[-1]
    yesterday = series[-2] if len(series) >= 2 else {"cost": 0.0, "tokens": 0, "requests": 0}
    return {
        "today_cost": today["cost"],
        "today_tokens": today["tokens"],
        "today_requests": today["requests"],
        "yesterday_cost": yesterday["cost"],
        "yesterday_tokens": yesterday["tokens"],
        "yesterday_requests": yesterday["requests"],
        "seven_day_cost": sum(item["cost"] for item in series),
        "seven_day_tokens": sum(item["tokens"] for item in series),
        "seven_day_requests": sum(item["requests"] for item in series),
        "series": series,
    }


def trend_with_current_totals(
    summary: dict[str, Any] | None,
    tokens: int,
    requests: int,
    cost: float,
) -> dict[str, Any]:
    result = dict(summary) if isinstance(summary, dict) else summarize_trend_rows([])
    series = [
        dict(item)
        for item in (result.get("series") or [])
        if isinstance(item, dict)
    ]
    if not series:
        series = list(summarize_trend_rows([])["series"])
    today = series[-1]
    today["tokens"] = max(int(today.get("tokens") or 0), int(tokens or 0))
    today["requests"] = max(int(today.get("requests") or 0), int(requests or 0))
    today["cost"] = max(float(today.get("cost") or 0), float(cost or 0))
    result.update(
        {
            "today_tokens": today["tokens"],
            "today_requests": today["requests"],
            "today_cost": today["cost"],
            "seven_day_tokens": sum(int(item.get("tokens") or 0) for item in series),
            "seven_day_requests": sum(int(item.get("requests") or 0) for item in series),
            "seven_day_cost": sum(float(item.get("cost") or 0) for item in series),
            "series": series,
        }
    )
    return result


def summarize_trend_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_date: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = str(row.get("date") or "").strip()
        if key:
            by_date[key] = row

    series: list[dict[str, Any]] = []
    for offset in range(6, -1, -1):
        key = date_key(offset)
        row = by_date.get(key, {})
        series.append(
            {
                "date": key,
                "cost": float(row.get("actual_cost") or row.get("cost") or 0),
                "tokens": int(row.get("total_tokens") or row.get("tokens") or 0),
                "requests": int(row.get("requests") or 0),
            }
        )

    today = series[-1] if series else {"cost": 0.0, "tokens": 0, "requests": 0}
    yesterday = series[-2] if len(series) >= 2 else {"cost": 0.0, "tokens": 0, "requests": 0}
    return {
        "today_cost": today["cost"],
        "today_tokens": today["tokens"],
        "today_requests": today["requests"],
        "yesterday_cost": yesterday["cost"],
        "yesterday_tokens": yesterday["tokens"],
        "yesterday_requests": yesterday["requests"],
        "seven_day_cost": sum(item["cost"] for item in series),
        "seven_day_tokens": sum(item["tokens"] for item in series),
        "seven_day_requests": sum(item["requests"] for item in series),
        "series": series,
    }


def update_usage_history(state: "MonitorState") -> dict[str, Any]:
    history = load_usage_history()
    days = history.setdefault("days", {})
    if not isinstance(days, dict):
        days = {}
        history["days"] = days

    key = today_key()
    existing = days.get(key) if isinstance(days.get(key), dict) else {}
    new_cost = float(state.today_account_cost or 0)
    new_tokens = int(state.today_tokens or 0)
    new_requests = int(state.today_requests or 0)
    existing_cost = float(existing.get("cost") or 0)
    existing_tokens = int(existing.get("tokens") or 0)
    existing_requests = int(existing.get("requests") or 0)
    source_date = ""
    if isinstance(state.client_usage, dict):
        source_date = str(state.client_usage.get("date") or "").strip()
    existing_source_date = str(existing.get("source_date") or "").strip()
    mix = token_mix_from_client_usage(state.client_usage if isinstance(state.client_usage, dict) else None)
    details = detailed_usage_from_state(state)
    preserve_existing_details = False

    # Same-day client usage is reconstructed from local logs and account
    # markers. Account switches can briefly make attribution smaller than the
    # previous snapshot, so keep a high-water total for the current day.
    use_local_high_water = state.usage_source in {"local", "client", "local-codex"}
    if use_local_high_water and existing_source_date in {"", source_date, key}:
        if existing_tokens > new_tokens and existing_tokens >= max(1, int(new_tokens * 1.05)):
            new_tokens = existing_tokens
            new_requests = max(new_requests, existing_requests)
            new_cost = max(new_cost, existing_cost)
            mix["input"] = max(mix["input"], int(existing.get("input_tokens") or 0))
            mix["cached"] = max(mix["cached"], int(existing.get("cached_input_tokens") or 0))
            mix["cache_create"] = max(mix["cache_create"], int(existing.get("cache_creation_input_tokens") or 0))
            mix["output"] = max(mix["output"], int(existing.get("output_tokens") or 0))
            preserve_existing_details = True

    if preserve_existing_details:
        if isinstance(existing.get("models"), dict):
            details["models"] = existing["models"]
        if isinstance(existing.get("providers"), list):
            details["providers"] = existing["providers"]

    updated_row = {
        "date": key,
        "source": state.usage_source,
        "requests": new_requests,
        "tokens": new_tokens,
        "input_tokens": mix["input"],
        "cached_input_tokens": mix["cached"],
        "cache_creation_input_tokens": mix["cache_create"],
        "output_tokens": mix["output"],
        "cost": round(new_cost, 6),
        "models": details["models"],
        "providers": details["providers"],
        "updated_at": datetime.now(CN_TZ).isoformat(timespec="seconds"),
        "source_date": source_date,
    }
    if isinstance(existing.get("source_gap"), dict):
        updated_row["source_gap"] = existing["source_gap"]
    days[key] = updated_row
    try:
        write_json_atomic(USAGE_HISTORY_JSON, history)
    except Exception:
        pass
    return summarize_usage_history(history)


def relative_time(value: str | None) -> str:
    if not value:
        return "-"
    try:
        normalized = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        seconds = max(0, int((datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds()))
    except Exception:
        return "-"
    if seconds < 60:
        return "刚刚"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}分钟前"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}小时前"
    return f"{hours // 24}天前"


def usage_sync_label(sync: dict[str, Any] | None) -> str:
    if not isinstance(sync, dict):
        return ""
    state = str(sync.get("state") or "").lower()
    labels = {
        "partial": "\u4eca\u65e5\u5df2\u66f4\u65b0 / \u5386\u53f2\u8865\u5f55\u672a\u5b8c\u6210",
        "timeout": "\u8865\u5f55\u8d85\u65f6 / \u663e\u793a\u4e0a\u6b21\u6570\u636e",
        "error": "\u5237\u65b0\u5931\u8d25 / \u663e\u793a\u4e0a\u6b21\u6570\u636e",
        "unavailable": "\u65e5\u5fd7\u91c7\u96c6\u4e0d\u53ef\u7528",
        "stale": "\u7b49\u5f85\u4eca\u65e5\u6570\u636e",
        "cached": "",
    }
    return labels.get(state, "")


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        normalized = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def account_usage_sort_key(row: dict[str, Any], account_range: str) -> tuple[Any, ...]:
    name = str(row.get("name") or "")
    try:
        tokens = int(row.get("tokens") or 0)
    except (TypeError, ValueError):
        tokens = 0
    try:
        requests = int(row.get("requests") or 0)
    except (TypeError, ValueError):
        requests = 0
    if account_range in {"5h", "7d"}:
        latest_at = str(
            row.get("latest_at")
            or row.get("latest_request_at")
            or row.get("created_at")
            or ""
        )
        latest = _parse_time(latest_at)
        latest_ts = latest.timestamp() if latest is not None else 0.0
        active_rank = 1 if row.get("active_now") or row.get("is_latest") else 0
        return (-active_rank, -latest_ts, -tokens, -requests, name)
    return (-tokens, -requests, name)


def account_row_available_for_range(row: dict[str, Any], account_range: str) -> bool:
    if row.get("is_unattributed_gap"):
        return False
    return not (account_range == "today" and row.get("window_only"))


def account_display_key(value: Any) -> str:
    name = str(value or "").strip().lower()
    for prefix in ("codex local - ", "local - "):
        if name.startswith(prefix):
            name = name[len(prefix) :].strip()
            break
    return name


def account_has_weekly_quota(row: dict[str, Any] | None) -> bool:
    if not isinstance(row, dict):
        return False
    window = row.get("window_7d")
    if not isinstance(window, dict) or not window or window.get("quota_stale"):
        return False
    if "quota_available" in window:
        return bool(window.get("quota_available"))
    return window.get("utilization") is not None or window.get("remaining_percent") is not None


def account_should_remain_visible(
    row: dict[str, Any],
    recent_usage: dict[str, Any] | None = None,
    quota_row: dict[str, Any] | None = None,
    *,
    now: datetime | None = None,
) -> bool:
    name = str(row.get("name") or "")
    if row.get("is_history_detail_gap") or name in {"Historical detail gap", "\u5386\u53f2\u660e\u7ec6\u7f3a\u53e3"}:
        return True

    recent = recent_usage if isinstance(recent_usage, dict) else {}
    requests = int(recent.get("requests") or 0)
    tokens = int(recent.get("tokens") or 0)
    for candidate in (row, quota_row):
        if not isinstance(candidate, dict):
            continue
        window_30d = candidate.get("window_30d")
        if isinstance(window_30d, dict):
            requests = max(requests, int(window_30d.get("requests") or 0))
            tokens = max(tokens, int(window_30d.get("tokens") or 0))
        latest_at = str(
            candidate.get("latest_at")
            or candidate.get("latest_request_at")
            or candidate.get("created_at")
            or ""
        )
        latest = _parse_time(latest_at)
        if latest is not None:
            current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
            age = (current - latest).total_seconds()
            if -300 <= age <= ACCOUNT_DISPLAY_ACTIVITY_DAYS * 86400:
                requests = max(1, requests)

    if requests > 0 or tokens > 0:
        return True
    return account_has_weekly_quota(quota_row) or account_has_weekly_quota(row)


def is_recent_activity(value: str | None, window_seconds: int = LOCAL_ACTIVE_WINDOW_SECONDS) -> bool:
    dt = _parse_time(value)
    if dt is None:
        return False
    seconds = (datetime.now(timezone.utc) - dt).total_seconds()
    return 0 <= seconds <= max(1, window_seconds)


def _read_jsonl_tail(path: Path, max_bytes: int = LIVE_ACTIVE_TAIL_BYTES) -> list[str]:
    try:
        with path.open("rb") as handle:
            size = handle.seek(0, os.SEEK_END)
            offset = max(0, size - max(1, max_bytes))
            handle.seek(offset)
            data = handle.read()
    except OSError:
        return []
    if offset:
        newline = data.find(b"\n")
        data = data[newline + 1 :] if newline >= 0 else b""
    return data.decode("utf-8", errors="ignore").splitlines()


TOKEN_COUNT_BYTES_RE = re.compile(rb'"type"\s*:\s*"token_count"')


def _live_usage_event_id(
    when: datetime,
    session_id: str,
    input_tokens: int,
    cached_tokens: int,
    output_tokens: int,
) -> str:
    aware = when if when.tzinfo is not None else when.replace(tzinfo=CN_TZ)
    timestamp_us = int(round(aware.timestamp() * 1_000_000))
    return "|".join(
        (
            str(session_id or ""),
            str(timestamp_us),
            str(max(0, int(input_tokens or 0))),
            str(max(0, int(cached_tokens or 0))),
            str(max(0, int(output_tokens or 0))),
        )
    )


class WindowsDirectoryChangeSignal:
    """Small ReadDirectoryChangesW bridge used only as a change signal."""

    BUFFER_BYTES = 64 * 1024
    CHANGE_FILTER = 0x00000001 | 0x00000002 | 0x00000008 | 0x00000010 | 0x00000040

    def __init__(self, root: Path) -> None:
        self.root = root
        self.available = False
        self._closed = False
        self._overflow = False
        self._paths: set[Path] = set()
        self._lock = threading.Lock()
        self._handle: int | None = None
        self._kernel32: Any = None
        self._thread: threading.Thread | None = None
        if os.name != "nt" or not root.is_dir():
            return
        try:
            import ctypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateFileW.argtypes = (
                ctypes.c_wchar_p,
                ctypes.c_uint32,
                ctypes.c_uint32,
                ctypes.c_void_p,
                ctypes.c_uint32,
                ctypes.c_uint32,
                ctypes.c_void_p,
            )
            kernel32.CreateFileW.restype = ctypes.c_void_p
            kernel32.ReadDirectoryChangesW.argtypes = (
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_uint32,
                ctypes.c_bool,
                ctypes.c_uint32,
                ctypes.POINTER(ctypes.c_uint32),
                ctypes.c_void_p,
                ctypes.c_void_p,
            )
            kernel32.ReadDirectoryChangesW.restype = ctypes.c_bool
            kernel32.CancelIoEx.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
            kernel32.CancelIoEx.restype = ctypes.c_bool
            kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
            kernel32.CloseHandle.restype = ctypes.c_bool
            handle = kernel32.CreateFileW(
                str(root),
                0x0001,
                0x00000001 | 0x00000002 | 0x00000004,
                None,
                3,
                0x02000000,
                None,
            )
            invalid_handle = ctypes.c_void_p(-1).value
            if not handle or int(handle) == int(invalid_handle or -1):
                return
            self._kernel32 = kernel32
            self._handle = int(handle)
            self.available = True
            self._thread = threading.Thread(
                target=self._run,
                name="TokenPulseDirectoryChanges",
                daemon=True,
            )
            self._thread.start()
        except (AttributeError, OSError, TypeError, ValueError):
            self.available = False

    @staticmethod
    def _decode_paths(data: bytes) -> list[str]:
        names: list[str] = []
        offset = 0
        while offset + 12 <= len(data):
            next_offset = int.from_bytes(data[offset : offset + 4], "little")
            name_length = int.from_bytes(data[offset + 8 : offset + 12], "little")
            name_start = offset + 12
            name_end = name_start + name_length
            if name_length < 0 or name_end > len(data):
                break
            name = data[name_start:name_end].decode("utf-16-le", errors="ignore")
            if name:
                names.append(name)
            if next_offset <= 0:
                break
            offset += next_offset
        return names

    def _mark_overflow(self) -> None:
        with self._lock:
            self._overflow = True

    def _run(self) -> None:
        try:
            import ctypes

            buffer = ctypes.create_string_buffer(self.BUFFER_BYTES)
            while not self._closed and self._handle is not None:
                returned = ctypes.c_uint32(0)
                ok = self._kernel32.ReadDirectoryChangesW(
                    ctypes.c_void_p(self._handle),
                    buffer,
                    self.BUFFER_BYTES,
                    True,
                    self.CHANGE_FILTER,
                    ctypes.byref(returned),
                    None,
                    None,
                )
                if not ok:
                    if not self._closed:
                        self._mark_overflow()
                    break
                if returned.value <= 0:
                    self._mark_overflow()
                    continue
                changed = {
                    self.root / Path(name)
                    for name in self._decode_paths(buffer.raw[: returned.value])
                    if name.lower().endswith(".jsonl")
                }
                if changed:
                    with self._lock:
                        self._paths.update(changed)
        except (AttributeError, OSError, TypeError, ValueError):
            if not self._closed:
                self._mark_overflow()

    def drain(self) -> tuple[set[Path], bool]:
        with self._lock:
            paths = set(self._paths)
            overflow = self._overflow
            self._paths.clear()
            self._overflow = False
        return paths, overflow

    def close(self) -> None:
        self._closed = True
        handle = self._handle
        self._handle = None
        if handle is not None and self._kernel32 is not None:
            try:
                import ctypes

                native_handle = ctypes.c_void_p(handle)
                self._kernel32.CancelIoEx(native_handle, None)
                self._kernel32.CloseHandle(native_handle)
            except (AttributeError, OSError, TypeError, ValueError):
                pass
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=0.25)
        self.available = False


class CodexUsageFileWatcher:
    """Detect appended token_count records with hot-file and fallback scans."""

    def __init__(self, sessions_root: Path, max_read_bytes: int = LIVE_USAGE_WATCH_READ_BYTES) -> None:
        self.sessions_root = sessions_root
        self.max_read_bytes = max(4096, int(max_read_bytes))
        self._files: dict[Path, tuple[int, int]] = {}
        self._hot_files: dict[Path, int] = {}
        self._recent_dir_mtimes: dict[Path, int] = {}
        self._seen_events: dict[tuple[Any, ...], None] = {}
        self._last_cumulative_by_path: dict[Path, tuple[int, int, int, int]] = {}
        self._fork_replay_cutoffs: dict[Path, datetime | None] = {}
        self._last_full_scan_at = float("-inf")
        self._last_activity_at = time.monotonic()
        self._primed = False
        self.token_count_changed = False
        self.reconciliation_needed = False
        self._reconciliation_paths: dict[Path, float] = {}
        self._last_reconciliation_change_at = float("-inf")
        self._directory_changes: WindowsDirectoryChangeSignal | None = None
        native_setting = os.environ.get("TOKEN_PULSE_NATIVE_FILE_WATCH", "auto").strip().lower()
        native_enabled = native_setting not in {"0", "false", "no", "off"}
        if native_enabled and os.name == "nt":
            try:
                sessions_root.resolve().relative_to((Path.home() / ".codex").resolve())
                is_default_root = True
            except (OSError, ValueError):
                is_default_root = native_setting in {"1", "true", "yes", "on"}
            if is_default_root:
                signal = WindowsDirectoryChangeSignal(sessions_root)
                if signal.available:
                    self._directory_changes = signal

    def close(self) -> None:
        if self._directory_changes is not None:
            self._directory_changes.close()
            self._directory_changes = None

    @staticmethod
    def _eligible(path: Path) -> bool:
        parts = {part.lower() for part in path.parts}
        return not any(part.startswith("backup-") for part in parts) and ".tmp" not in parts

    def _read_region(self, path: Path, start: int, end: int) -> bytes:
        if end <= 0:
            return b""
        # Keep a small overlap so a JSON line split across writes is detected
        # without re-reading the entire bounded tail on every append.
        overlap = min(self.max_read_bytes, LIVE_USAGE_WATCH_OVERLAP_BYTES)
        read_start = max(0, min(start, end) - overlap)
        read_start = max(read_start, end - self.max_read_bytes)
        try:
            with path.open("rb") as handle:
                handle.seek(read_start)
                data = handle.read(max(0, end - read_start))
        except OSError:
            return b""
        return data

    def _remember_hot_file(self, path: Path, modified_ns: int) -> None:
        self._hot_files[path] = int(modified_ns)
        while len(self._hot_files) > LIVE_USAGE_WATCH_HOT_FILE_LIMIT:
            coldest = min(self._hot_files, key=self._hot_files.get)
            self._hot_files.pop(coldest, None)

    def _fork_replay_cutoff(self, path: Path) -> datetime | None:
        if path in self._fork_replay_cutoffs:
            return self._fork_replay_cutoffs[path]
        try:
            with path.open(encoding="utf-8", errors="ignore") as handle:
                for _ in range(4):
                    line = handle.readline()
                    if not line:
                        return None
                    row = json.loads(line)
                    if row.get("type") != "session_meta":
                        continue
                    payload = row.get("payload") or {}
                    if not payload.get("forked_from_id"):
                        self._fork_replay_cutoffs[path] = None
                        return None
                    started = _parse_time(str(row.get("timestamp") or payload.get("timestamp") or ""))
                    if started is None:
                        return None
                    cutoff = started + timedelta(seconds=2)
                    self._fork_replay_cutoffs[path] = cutoff
                    return cutoff
        except (OSError, json.JSONDecodeError):
            return None
        return None

    def _recent_session_directories(self) -> list[Path]:
        today = datetime.now()
        directories: list[Path] = []
        for days_ago in (0, 1):
            day = today - timedelta(days=days_ago)
            directories.append(
                self.sessions_root
                / f"{day.year:04d}"
                / f"{day.month:02d}"
                / f"{day.day:02d}"
            )
        return directories

    def _discover_recent_paths(self) -> set[Path]:
        paths: set[Path] = set()
        active_directories = set(self._recent_session_directories())
        for directory in active_directories:
            try:
                modified_ns = int(directory.stat().st_mtime_ns)
            except OSError:
                modified_ns = -1
            previous = self._recent_dir_mtimes.get(directory)
            self._recent_dir_mtimes[directory] = modified_ns
            if previous == modified_ns:
                continue
            try:
                paths.update(
                    path
                    for path in directory.glob("*.jsonl")
                    if self._eligible(path)
                )
            except OSError:
                continue
        for directory in list(self._recent_dir_mtimes):
            if directory not in active_directories:
                self._recent_dir_mtimes.pop(directory, None)
        return paths

    def _full_scan_paths(self) -> set[Path]:
        try:
            return {
                path
                for path in self.sessions_root.rglob("*.jsonl")
                if self._eligible(path)
            }
        except OSError:
            return set()

    def next_poll_interval_ms(self, now: float | None = None) -> int:
        current = time.monotonic() if now is None else float(now)
        idle_seconds = max(0.0, current - self._last_activity_at)
        if idle_seconds <= LIVE_USAGE_WATCH_HOT_SECONDS:
            return LIVE_USAGE_WATCH_INTERVAL_MS
        if idle_seconds <= LIVE_USAGE_WATCH_IDLE_SECONDS:
            return LIVE_USAGE_WATCH_IDLE_INTERVAL_MS
        return LIVE_USAGE_WATCH_COLD_INTERVAL_MS

    def has_recent_activity(self, window_seconds: float) -> bool:
        return time.monotonic() - self._last_activity_at <= max(0.0, float(window_seconds))

    def reconciliation_ready(self, quiet_seconds: float) -> bool:
        if not self.reconciliation_needed:
            return True
        now = time.monotonic()
        if self._reconciliation_paths:
            return now >= max(self._reconciliation_paths.values())
        return now - self._last_reconciliation_change_at >= max(0.0, float(quiet_seconds))

    def mark_reconciled(self) -> None:
        self.reconciliation_needed = False

    def _extract_live_events(self, path: Path, data: bytes) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        fork_replay_cutoff = self._fork_replay_cutoff(path)
        session_match = re.search(
            r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$",
            path.stem,
            re.IGNORECASE,
        )
        session_id = session_match.group(1) if session_match else ""
        for raw_line in data.splitlines():
            try:
                row = json.loads(raw_line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            usage = _live_token_usage(row)
            if usage is None:
                continue
            if fork_replay_cutoff is not None and usage["when"] <= fork_replay_cutoff:
                continue
            payload = row.get("payload") or {}
            info = payload.get("info") or {}
            total = info.get("total_token_usage") or {}
            cumulative_key = (
                _token_usage_int(total, "input_tokens"),
                _token_usage_int(total, "cached_input_tokens"),
                _token_usage_int(total, "output_tokens"),
                _token_usage_int(total, "reasoning_output_tokens"),
            )
            if any(cumulative_key):
                event_key = (path, *cumulative_key)
                if self._last_cumulative_by_path.get(path) == cumulative_key:
                    continue
                self._last_cumulative_by_path[path] = cumulative_key
            else:
                event_key = (
                    path,
                    str(row.get("timestamp") or ""),
                    int(usage.get("input_tokens") or 0),
                    int(usage.get("cached_tokens") or 0),
                    int(usage.get("output_tokens") or 0),
                )
            if event_key in self._seen_events:
                continue
            self._seen_events[event_key] = None
            if session_id:
                usage["session_id"] = session_id
            usage["event_id"] = _live_usage_event_id(
                usage["when"],
                session_id,
                int(usage.get("input_tokens") or 0),
                int(usage.get("cached_tokens") or 0),
                int(usage.get("output_tokens") or 0),
            )
            events.append(usage)
        while len(self._seen_events) > 4096:
            self._seen_events.pop(next(iter(self._seen_events)))
        return events

    def poll_events(self) -> list[dict[str, Any]]:
        live_events: list[dict[str, Any]] = []
        token_count_changed = False
        was_primed = self._primed
        now = time.monotonic()
        for watched_path, expires_at in list(self._reconciliation_paths.items()):
            if now >= expires_at:
                self._reconciliation_paths.pop(watched_path, None)
        activity_detected = False
        paths = set(self._hot_files)
        native_overflow = False
        if self._directory_changes is not None:
            notified_paths, native_overflow = self._directory_changes.drain()
            paths.update(
                path
                for path in notified_paths
                if self._eligible(path)
            )
        paths.update(self._discover_recent_paths())
        full_scan_due = (
            not was_primed
            or native_overflow
            or now - self._last_full_scan_at >= LIVE_USAGE_WATCH_FULL_SCAN_SECONDS
        )
        if full_scan_due:
            scanned_paths = self._full_scan_paths()
            paths.update(scanned_paths)
            self._last_full_scan_at = now
            for missing in set(self._files) - scanned_paths:
                self._files.pop(missing, None)
                self._hot_files.pop(missing, None)
                self._fork_replay_cutoffs.pop(missing, None)

        for path in paths:
            try:
                stat = path.stat()
            except OSError:
                self._files.pop(path, None)
                self._hot_files.pop(path, None)
                self._fork_replay_cutoffs.pop(path, None)
                continue
            size = max(0, int(stat.st_size))
            modified_ns = int(stat.st_mtime_ns)
            current = (size, modified_ns)
            previous = self._files.get(path)
            self._files[path] = current
            self._remember_hot_file(path, modified_ns)
            if previous == current:
                continue
            if was_primed and path in self._reconciliation_paths:
                self.reconciliation_needed = True
                self._last_reconciliation_change_at = now
            if was_primed:
                activity_detected = True
            if not was_primed:
                data = self._read_region(path, max(0, size - self.max_read_bytes), size)
                self._extract_live_events(path, data)
                continue
            if previous is None:
                scan_start = max(0, size - self.max_read_bytes)
            elif size > previous[0]:
                scan_start = previous[0]
            else:
                scan_start = max(0, size - self.max_read_bytes)
            data = self._read_region(path, scan_start, size)
            if TOKEN_COUNT_BYTES_RE.search(data) is None:
                continue
            token_count_changed = True
            extracted = self._extract_live_events(path, data)
            # A brand-new rollout may contain a copied fork prefix. Keep all of
            # its events provisional during the observation window so delayed
            # fork metadata cannot make the visible total jump and then fall.
            if previous is None:
                self.reconciliation_needed = True
                self._reconciliation_paths[path] = now + LIVE_USAGE_NEW_ROLLOUT_WATCH_SECONDS
                self._last_reconciliation_change_at = now
            elif path not in self._reconciliation_paths:
                live_events.extend(extracted)

        if activity_detected:
            self._last_activity_at = now
        self._primed = True
        self.token_count_changed = token_count_changed
        return live_events

    def poll(self) -> bool:
        self.poll_events()
        return self.token_count_changed


def _current_codex_account_label() -> str:
    identity = current_codex_auth_identity()
    return f"Codex local - {identity}" if identity else ""


def _token_usage_int(row: dict[str, Any], key: str) -> int:
    try:
        return max(0, int(row.get(key) or 0))
    except (TypeError, ValueError):
        return 0


def _live_token_usage(row: dict[str, Any]) -> dict[str, Any] | None:
    payload = row.get("payload") or {}
    if row.get("type") != "event_msg" or payload.get("type") != "token_count":
        return None
    info = payload.get("info") or {}
    last = info.get("last_token_usage") or {}
    if not isinstance(last, dict) or not last:
        return None
    when = _parse_time(str(row.get("timestamp") or ""))
    if when is None:
        return None
    input_tokens = _token_usage_int(last, "input_tokens")
    cached_tokens = _token_usage_int(last, "cached_input_tokens")
    output_tokens = _token_usage_int(last, "output_tokens")
    total_tokens = _token_usage_int(last, "total_tokens") or input_tokens + output_tokens
    if total_tokens <= 0:
        return None
    return {
        "when": when,
        "total_tokens": total_tokens,
        "input_tokens": input_tokens,
        "cached_tokens": cached_tokens,
        "output_tokens": output_tokens,
    }


def _cockpit_account_label(account_id: Any, email: Any, api_key_label: Any) -> str:
    identity = str(email or "").strip() or str(api_key_label or "").strip() or str(account_id or "").strip()
    return f"Codex local - {identity}" if identity else ""


def _load_live_cockpit_markers(
    db_path: Path,
    now_utc: datetime,
) -> list[dict[str, Any]]:
    if not db_path.exists():
        return []
    start_ms = int((now_utc - timedelta(seconds=max(60.0, LIVE_ACCOUNT_MATCH_SECONDS))).timestamp() * 1000)
    connection: sqlite3.Connection | None = None
    try:
        uri = f"file:{db_path.resolve().as_posix()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=0.1)
        rows = connection.execute(
            """
            SELECT timestamp, account_id, email, api_key_label, model_id,
                   total_tokens, input_tokens, cached_tokens, output_tokens
            FROM request_logs
            WHERE timestamp >= ? AND total_tokens > 0
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            (start_ms, max(1, LIVE_ACCOUNT_MARKER_LIMIT)),
        ).fetchall()
    except (OSError, sqlite3.Error):
        return []
    finally:
        if connection is not None:
            connection.close()

    markers: list[dict[str, Any]] = []
    for timestamp, account_id, email, api_key_label, model, total, input_value, cached, output in rows:
        label = _cockpit_account_label(account_id, email, api_key_label)
        if not label:
            continue
        try:
            when = datetime.fromtimestamp(int(timestamp) / 1000.0, tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            continue
        markers.append(
            {
                "when": when,
                "label": label,
                "model": str(model or "").strip(),
                "total_tokens": max(0, int(total or 0)),
                "input_tokens": max(0, int(input_value or 0)),
                "cached_tokens": max(0, int(cached or 0)),
                "output_tokens": max(0, int(output or 0)),
            }
        )
    return markers


def _match_live_cockpit_marker(
    usage: dict[str, Any] | None,
    markers: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if not usage:
        return None
    when = usage.get("when")
    if not isinstance(when, datetime):
        return None
    total_tokens = int(usage.get("total_tokens") or 0)
    exact = [
        marker
        for marker in markers
        if int(marker.get("total_tokens") or 0) == total_tokens
        and abs((marker["when"] - when).total_seconds()) <= LIVE_ACCOUNT_MATCH_SECONDS
    ]
    component_exact = [
        marker
        for marker in exact
        if int(marker.get("input_tokens") or 0) == int(usage.get("input_tokens") or 0)
        and int(marker.get("cached_tokens") or 0) == int(usage.get("cached_tokens") or 0)
        and int(marker.get("output_tokens") or 0) == int(usage.get("output_tokens") or 0)
    ]
    candidates = component_exact or exact
    if candidates:
        return min(candidates, key=lambda marker: abs((marker["when"] - when).total_seconds()))

    fuzzy_delta = max(256, int(total_tokens * 0.005))
    fuzzy = [
        marker
        for marker in markers
        if abs((marker["when"] - when).total_seconds()) <= 30
        and abs(int(marker.get("total_tokens") or 0) - total_tokens) <= fuzzy_delta
    ]
    if not fuzzy:
        return None
    return min(
        fuzzy,
        key=lambda marker: (
            abs(int(marker.get("total_tokens") or 0) - total_tokens),
            abs((marker["when"] - when).total_seconds()),
        ),
    )


def _concrete_live_provider(value: Any) -> str:
    label = str(value or "").strip()
    if not label or label == "正在识别账号" or label.startswith("API 服务 ·"):
        return ""
    return label


def scan_live_codex_active_sessions(
    sessions_root: Path,
    cached_sessions: list[dict[str, Any]] | None = None,
    *,
    now: datetime | None = None,
    cockpit_db_path: Path | None = None,
    tail_cache: dict[Path, tuple[tuple[int, int], dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    now_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    cached_by_id = {
        str(row.get("session_id") or ""): dict(row)
        for row in (cached_sessions or [])
        if isinstance(row, dict) and row.get("session_id")
    }
    candidates: list[tuple[float, Path, tuple[int, int]]] = []
    try:
        paths = sessions_root.rglob("*.jsonl")
        for path in paths:
            try:
                stat = path.stat()
                modified = stat.st_mtime
            except OSError:
                continue
            age = now_utc.timestamp() - modified
            if -5 <= age <= max(1, LIVE_ACTIVE_STALE_SECONDS):
                candidates.append(
                    (
                        modified,
                        path,
                        (max(0, int(stat.st_size)), int(stat.st_mtime_ns)),
                    )
                )
    except OSError:
        return []
    candidates.sort(key=lambda item: item[0], reverse=True)

    current_label = _current_codex_account_label()
    api_service_route = "api-service" in current_label.lower()
    if api_service_route:
        current_label = ""
    cockpit_markers = (
        _load_live_cockpit_markers(cockpit_db_path or COCKPIT_REQUEST_LOG_DB, now_utc)
        if api_service_route
        else []
    )
    active_rows: list[dict[str, Any]] = []
    session_pattern = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$")
    terminal_states = {"task_complete", "turn_aborted"}
    selected_candidates = candidates[: max(1, LIVE_ACTIVE_MAX_FILES)]
    if tail_cache is not None:
        selected_paths = {path for _modified, path, _state in selected_candidates}
        for cached_path in list(tail_cache):
            if cached_path not in selected_paths:
                tail_cache.pop(cached_path, None)
    for modified, path, file_state in selected_candidates:
        match = session_pattern.search(path.stem)
        if match is None:
            continue
        session_id = match.group(1)
        parsed = tail_cache.get(path) if tail_cache is not None else None
        if parsed is not None and parsed[0] == file_state:
            tail_state = parsed[1]
            lifecycle_state = str(tail_state.get("lifecycle_state") or "")
            lifecycle_at = str(tail_state.get("lifecycle_at") or "")
            started_at = str(tail_state.get("started_at") or "")
            latest_usage = tail_state.get("latest_usage")
        else:
            lifecycle_state = ""
            lifecycle_at = ""
            started_at = ""
            latest_usage: dict[str, Any] | None = None
            for line in reversed(_read_jsonl_tail(path)):
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if latest_usage is None:
                    latest_usage = _live_token_usage(row)
                if lifecycle_state and latest_usage is not None:
                    break
                if row.get("type") != "event_msg":
                    continue
                payload = row.get("payload") or {}
                payload_type = str(payload.get("type") or "")
                if lifecycle_state or payload_type not in {"task_started", *terminal_states}:
                    continue
                lifecycle_state = payload_type
                lifecycle_at = str(row.get("timestamp") or "")
                if payload_type == "task_started":
                    started_at = lifecycle_at
                if latest_usage is not None:
                    break
            if tail_cache is not None:
                tail_cache[path] = (
                    file_state,
                    {
                        "lifecycle_state": lifecycle_state,
                        "lifecycle_at": lifecycle_at,
                        "started_at": started_at,
                        "latest_usage": latest_usage,
                    },
                )
        cached = cached_by_id.get(session_id, {})
        if lifecycle_state in terminal_states:
            continue
        recently_written = now_utc.timestamp() - modified <= max(1, LOCAL_ACTIVE_WINDOW_SECONDS)
        if lifecycle_state != "task_started" and not recently_written:
            continue
        started_dt = _parse_time(started_at)
        if started_dt is not None and latest_usage is not None and latest_usage["when"] < started_dt:
            latest_usage = None
        matched_marker = _match_live_cockpit_marker(latest_usage, cockpit_markers)
        matched_label = str((matched_marker or {}).get("label") or "")
        cached_provider = _concrete_live_provider(cached.get("provider"))
        if api_service_route and matched_label:
            provider = matched_label
        else:
            provider = cached_provider or current_label or matched_label
        if not provider:
            provider = "API 服务 · 等待首个响应" if api_service_route else "正在识别账号"
        latest_at = datetime.fromtimestamp(modified, tz=timezone.utc).isoformat(timespec="seconds")
        live = dict(cached)
        live.update(
            {
                "session_id": session_id,
                "provider": provider,
                "model": cached.get("model") or (matched_marker or {}).get("model") or "-",
                "latest_at": latest_at,
                "started_at": started_at or cached.get("started_at") or lifecycle_at,
                "active": True,
                "activity_source": "live-session-tail",
            }
        )
        active_rows.append(live)
    active_rows.sort(key=lambda row: str(row.get("latest_at") or ""), reverse=True)
    return active_rows


def local_active_accounts_from_client_usage(
    client_usage: dict[str, Any] | None,
    *,
    include_when_routed_to_sub2api: bool = True,
) -> list[dict[str, Any]]:
    if not include_when_routed_to_sub2api:
        return []
    if not isinstance(client_usage, dict):
        return []
    providers = client_usage.get("providers")
    providers = providers if isinstance(providers, list) else []
    providers_by_name = {
        str(provider.get("name") or ""): provider
        for provider in providers
        if isinstance(provider, dict) and str(provider.get("name") or "")
    }
    active_sessions = client_usage.get("active_sessions")
    client_latest = client_usage.get("latest_request")
    client_latest = client_latest if isinstance(client_latest, dict) else {}
    latest_provider_name = str(client_latest.get("provider") or "")
    if isinstance(active_sessions, list):
        active_by_provider: dict[str, dict[str, Any]] = {}
        for index, session in enumerate(active_sessions):
            if not isinstance(session, dict):
                continue
            latest_at = str(session.get("latest_at") or "")
            lifecycle_active = session.get("active") is True
            if session.get("active") is False:
                continue
            if not lifecycle_active and not is_recent_activity(latest_at):
                continue
            provider_name = str(session.get("provider") or "").strip() or "正在识别账号"
            provider_meta = providers_by_name.get(provider_name, {})
            row = active_by_provider.get(provider_name)
            if row is None:
                row = {
                    "id": f"local-session-{index}",
                    "name": f"LOCAL - {local_provider_display_name(provider_name)}",
                    "provider": provider_name,
                    "current": 0,
                    "max": 0,
                    "model": session.get("model") or "-",
                    "source": "LOCAL",
                    "plan_type": provider_meta.get("plan_type") or "",
                    "speed_badge": "",
                    "latest_at": latest_at,
                }
                active_by_provider[provider_name] = row
            row["current"] = int(row.get("current") or 0) + 1
            row["max"] = int(row.get("current") or 0)
            existing_latest = _parse_time(str(row.get("latest_at") or ""))
            session_latest = _parse_time(latest_at)
            if session_latest is not None and (existing_latest is None or session_latest > existing_latest):
                row["latest_at"] = latest_at
                row["model"] = session.get("model") or row.get("model") or "-"
        active = list(active_by_provider.values())
        if active:
            active.sort(
                key=lambda row: (
                    str(row.get("provider") or "") == latest_provider_name,
                    _parse_time(str(row.get("latest_at") or ""))
                    or datetime.min.replace(tzinfo=timezone.utc),
                ),
                reverse=True,
            )
            return active

    active: list[dict[str, Any]] = []
    for index, provider in enumerate(providers):
        if not isinstance(provider, dict):
            continue
        recent_sessions_raw = provider.get("recent_sessions")
        try:
            recent_sessions = int(recent_sessions_raw or 0)
        except (TypeError, ValueError):
            recent_sessions = 0
        if recent_sessions <= 0:
            continue
        latest_at = str(provider.get("latest_at") or "")
        if not is_recent_activity(latest_at):
            continue
        provider_name = str(provider.get("name") or "Local client")
        active.append(
            {
                "id": f"local-{index}",
                "name": f"LOCAL - {local_provider_display_name(provider_name)}",
                "provider": provider_name,
                "current": recent_sessions,
                "max": recent_sessions,
                "model": provider.get("latest_model") or "-",
                "source": "LOCAL",
                "plan_type": provider.get("plan_type") or "",
                "speed_badge": provider.get("speed_badge") or "",
                "latest_at": latest_at,
            }
        )
    if active:
        active.sort(key=lambda row: _parse_time(str(row.get("latest_at") or "")) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        return active

    if not client_latest.get("created_at"):
        return []
    if not is_recent_activity(str(client_latest.get("created_at") or "")):
        return []
    provider_name = str(client_latest.get("provider") or "Local client")
    latest_provider = next(
        (provider for provider in providers if isinstance(provider, dict) and str(provider.get("name") or "") == provider_name),
        {},
    )
    return [
        {
            "id": "local-latest",
            "name": f"LOCAL - {local_provider_display_name(provider_name)}",
            "current": 1,
            "max": 1,
            "model": client_latest.get("model") or "-",
            "source": "LOCAL",
            "plan_type": latest_provider.get("plan_type") or "",
            "speed_badge": latest_provider.get("speed_badge") or "",
        }
    ]


def account_health_badge(account: dict[str, Any]) -> str:
    """Return a short account health label for the ranking list."""
    status = str(account.get("status") or "").strip().lower()
    error_message = str(account.get("error_message") or account.get("last_error") or "").strip().lower()
    schedulable = account.get("schedulable")
    temp_until = _parse_time(account.get("temp_unschedulable_until") or account.get("cooldown_until"))

    if account.get("quota_exceeded") is True or status in {"quota_exceeded", "quota-exceeded", "quota"}:
        return "\u9650\u989d"
    if temp_until and temp_until > datetime.now(timezone.utc):
        return "\u51b7\u5374"
    if schedulable is False:
        return "\u4e0d\u53ef\u7528"
    if status in {"disabled", "inactive", "suspended", "banned", "unavailable"}:
        return "\u505c\u7528"
    if status in {"error", "failed"}:
        return "\u9519\u8bef"
    return ""


def account_has_email(account: dict[str, Any]) -> bool:
    extra = account.get("extra") if isinstance(account.get("extra"), dict) else {}
    credentials = account.get("credentials") if isinstance(account.get("credentials"), dict) else {}
    candidates = (
        account.get("name"),
        extra.get("email_address"),
        extra.get("email"),
        credentials.get("email"),
    )
    return any("@" in str(value or "") for value in candidates)


def normalize_usage_window(progress: Any) -> dict[str, Any]:
    if not isinstance(progress, dict):
        return {}
    stats = progress.get("window_stats")
    if not isinstance(stats, dict):
        return {}
    utilization = progress.get("utilization")
    result = {
        "requests": int(stats.get("requests") or 0),
        "tokens": int(stats.get("tokens") or 0),
        "cost": float(stats.get("cost") or 0),
        "resets_at": str(progress.get("resets_at") or ""),
        "quota_available": utilization is not None,
        "quota_stale": False,
    }
    if utilization is not None:
        try:
            used = float(utilization)
            result["utilization"] = used
            result["remaining_percent"] = max(0.0, min(100.0, 100.0 - used))
        except (TypeError, ValueError):
            result["quota_available"] = False
    latest_at = (
        stats.get("latest_at")
        or stats.get("last_request_at")
        or stats.get("latest_request_at")
        or progress.get("latest_at")
        or progress.get("last_request_at")
        or progress.get("latest_request_at")
    )
    if latest_at:
        result["latest_at"] = str(latest_at)
    latest_model = stats.get("latest_model") or progress.get("latest_model")
    if latest_model:
        result["latest_model"] = str(latest_model)
    return result


def client_usage_export_command(*arguments: str) -> list[str]:
    exporter = str(CLIENT_USAGE_EXPORT)
    suffix = CLIENT_USAGE_EXPORT.suffix.casefold()
    if suffix in {".exe", ".com", ".bat", ".cmd"}:
        return [exporter, *arguments]
    return [CLIENT_USAGE_PYTHON, exporter, *arguments]


def load_client_usage(
    include_30d: bool = False,
    backfill_history_details: bool = False,
    run_export: bool = True,
) -> dict[str, Any] | None:
    attempted_at = datetime.now(CN_TZ).isoformat(timespec="seconds")
    before_mtime_ns: int | None = None
    before_content: bytes | None = None
    try:
        before_mtime_ns = CLIENT_USAGE_JSON.stat().st_mtime_ns
        before_content = CLIENT_USAGE_JSON.read_bytes()
    except OSError:
        pass
    export_state = "cached"
    export_message = ""
    export_attempted = False
    if run_export and CLIENT_USAGE_EXPORT.exists():
        export_attempted = True
        try:
            command = client_usage_export_command(
                "--output",
                str(CLIENT_USAGE_JSON),
            )
            if include_30d:
                command.append("--include-30d")
            if backfill_history_details:
                command.append("--backfill-history-details")
            completed = subprocess.run(
                command,
                cwd=str(APP_DIR),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=CLIENT_USAGE_EXPORT_TIMEOUT_SECONDS,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            if completed.returncode == 0:
                export_state = "ok"
            else:
                export_state = "error"
                detail = str(completed.stderr or "").strip().splitlines()
                export_message = (
                    detail[-1][:240]
                    if detail
                    else f"Exporter exited with code {completed.returncode}"
                )
        except subprocess.TimeoutExpired:
            export_state = "timeout"
            export_message = f"Exporter timed out after {CLIENT_USAGE_EXPORT_TIMEOUT_SECONDS}s"
        except OSError as exc:
            export_state = "error"
            export_message = f"{type(exc).__name__}: {exc}"[:240]
        except Exception as exc:
            export_state = "error"
            export_message = f"{type(exc).__name__}: {exc}"[:240]
    elif not CLIENT_USAGE_JSON.exists():
        export_state = "unavailable"
        export_message = "Usage exporter and cache are unavailable"

    after_mtime_ns: int | None = None
    after_content: bytes | None = None
    try:
        after_mtime_ns = CLIENT_USAGE_JSON.stat().st_mtime_ns
        after_content = CLIENT_USAGE_JSON.read_bytes()
    except OSError:
        pass
    output_changed = after_mtime_ns is not None and (
        after_mtime_ns != before_mtime_ns or after_content != before_content
    )

    sync_status: dict[str, Any] = {
        "state": export_state,
        "message": export_message,
        "attempted_at": attempted_at,
        "fresh": False,
        "cache_used": False,
    }
    if not CLIENT_USAGE_JSON.exists():
        sync_status["cache_used"] = True
        return {
            "requests": 0,
            "tokens": 0,
            "cost": 0.0,
            "providers": [],
            "active_sessions": [],
            "latest_request": {},
            "dashboard": {},
            "updated_at": "",
            "date": "",
            "stale": True,
            "sync": sync_status,
        }
    try:
        raw_text = (
            after_content.decode("utf-8", errors="ignore")
            if after_content is not None
            else CLIENT_USAGE_JSON.read_text(encoding="utf-8", errors="ignore")
        )
        data = json.loads(raw_text)
    except Exception:
        sync_status.update(
            {
                "state": "error",
                "message": "Usage cache is not valid JSON",
                "cache_used": True,
            }
        )
        return {
            "requests": 0,
            "tokens": 0,
            "cost": 0.0,
            "providers": [],
            "active_sessions": [],
            "latest_request": {},
            "dashboard": {},
            "updated_at": "",
            "date": "",
            "stale": True,
            "sync": sync_status,
        }
    today = data.get("today") if isinstance(data, dict) else None
    if not isinstance(today, dict):
        sync_status.update(
            {
                "state": "error",
                "message": "Usage cache has no today payload",
                "cache_used": True,
            }
        )
        return {
            "requests": 0,
            "tokens": 0,
            "cost": 0.0,
            "providers": [],
            "active_sessions": [],
            "latest_request": {},
            "dashboard": {},
            "updated_at": data.get("updated_at") if isinstance(data, dict) else "",
            "date": str(data.get("date") or "") if isinstance(data, dict) else "",
            "stale": True,
            "sync": sync_status,
        }
    data_date = str(data.get("date") or "").strip()
    offline_catchup = data.get("offline_catchup")
    if isinstance(offline_catchup, dict):
        sync_status["offline_catchup"] = offline_catchup
    if export_state == "ok" and isinstance(offline_catchup, dict):
        catchup_state = str(offline_catchup.get("state") or "")
        if catchup_state == "error":
            sync_status["state"] = "partial"
            sync_status["message"] = str(
                offline_catchup.get("message") or "Historical catch-up failed"
            )[:240]
    if export_state in {"timeout", "error"} and output_changed:
        sync_status["state"] = "partial"
        sync_status["message"] = (
            "Today is updated, but historical catch-up did not finish"
            if export_state == "timeout"
            else "Today is updated, but post-processing did not finish"
        )
    current_date = data_date == today_key()
    sync_status["fresh"] = bool(current_date and (output_changed or export_state == "ok"))
    sync_status["cache_used"] = not bool(sync_status["fresh"])
    sync_status["updated_at"] = data.get("updated_at") or ""
    if data_date and data_date != today_key():
        sync_status.update(
            {
                "state": "stale",
                "message": f"Cached usage belongs to {data_date}",
                "fresh": False,
                "cache_used": True,
            }
        )
        return {
            "requests": 0,
            "tokens": 0,
            "cost": 0.0,
            "providers": [],
            "active_sessions": [],
            "latest_request": {},
            "dashboard": data.get("dashboard") if isinstance(data.get("dashboard"), dict) else {},
            "updated_at": data.get("updated_at") or "",
            "date": data_date,
            "stale": True,
            "sync": sync_status,
        }
    if export_attempted and export_state == "ok" and not output_changed:
        sync_status.update(
            {
                "state": "stale",
                "message": "Exporter finished without updating the cache",
                "fresh": False,
                "cache_used": True,
            }
        )
    return {
        "requests": int(today.get("requests") or 0),
        "tokens": int(today.get("tokens") or 0),
        "cost": float(today.get("cost") or 0),
        "providers": data.get("providers") or [],
        "active_sessions": data.get("active_sessions") or [],
        "latest_request": data.get("latest_request") or {},
        "dashboard": data.get("dashboard") if isinstance(data.get("dashboard"), dict) else {},
        "scan_status": data.get("scan_status") if isinstance(data.get("scan_status"), dict) else {},
        "api_service_routed": bool(data.get("api_service_routed")),
        "updated_at": data.get("updated_at") or "",
        "date": data_date,
        "stale": bool(sync_status.get("cache_used")),
        "sync": sync_status,
    }


def latest_request_from_client_providers(providers: list[dict[str, Any]]) -> dict[str, Any]:
    latest_provider = ""
    latest_model = ""
    latest_at = ""
    latest_dt: datetime | None = None
    for provider in providers:
        if not isinstance(provider, dict):
            continue
        provider_latest_at = str(provider.get("latest_at") or "")
        provider_dt = _parse_time(provider_latest_at)
        if provider_dt is None:
            continue
        if latest_dt is None or provider_dt > latest_dt:
            latest_dt = provider_dt
            latest_provider = str(provider.get("name") or "Local client")
            latest_model = str(provider.get("latest_model") or "-")
            latest_at = provider_latest_at
    if not latest_at:
        return {}
    return {
        "provider": latest_provider,
        "model": latest_model,
        "created_at": latest_at,
        "kind": "success",
    }


def subtract_provider_from_client_usage(client_usage: dict[str, Any] | None, provider_name: str) -> dict[str, Any] | None:
    if not isinstance(client_usage, dict) or not provider_name:
        return client_usage
    providers = client_usage.get("providers")
    if not isinstance(providers, list):
        return client_usage

    kept: list[dict[str, Any]] = []
    removed_requests = 0
    removed_tokens = 0
    removed_cost = 0.0
    for provider in providers:
        if not isinstance(provider, dict):
            continue
        if str(provider.get("name") or "") == provider_name:
            removed_requests += int(provider.get("requests") or 0)
            removed_tokens += int(provider.get("tokens") or 0)
            removed_cost += float(provider.get("cost") or 0)
            continue
        kept.append(provider)

    if removed_requests <= 0 and removed_tokens <= 0 and removed_cost <= 0:
        return client_usage

    result = dict(client_usage)
    result["providers"] = kept
    result["requests"] = max(0, int(client_usage.get("requests") or 0) - removed_requests)
    result["tokens"] = max(0, int(client_usage.get("tokens") or 0) - removed_tokens)
    result["cost"] = max(0.0, float(client_usage.get("cost") or 0) - removed_cost)
    result["sub2api_routed_provider"] = provider_name
    latest = client_usage.get("latest_request")
    if isinstance(latest, dict) and str(latest.get("provider") or "") == provider_name:
        result["latest_request"] = latest_request_from_client_providers(kept)
    return result


def subtract_sub2api_routed_client_usage(client_usage: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(client_usage, dict):
        return client_usage
    providers = client_usage.get("providers")
    if not isinstance(providers, list):
        return client_usage

    result = client_usage
    routed_names = [
        str(provider.get("name") or "")
        for provider in providers
        if isinstance(provider, dict)
        and (
            provider.get("routed_to_sub2api") is True
            or str(provider.get("name") or "").strip().lower() == "codex via sub2api"
        )
    ]
    for name in routed_names:
        result = subtract_provider_from_client_usage(result, name)
    return result


def is_local_api_key_provider_name(name: str) -> bool:
    return name.strip().lower().startswith("codex local - api-key-")


def is_local_api_service_provider_name(name: str) -> bool:
    provider_key = account_display_key(name)
    return provider_key in {"api-service-local", "api service local"}


def load_local_api_service_pool_emails(home: Path | None = None) -> set[str]:
    root = home or Path.home()
    manifest = root / ".antigravity_cockpit" / "codex_local_access_sidecar" / "manifest.json"
    try:
        data = json.loads(manifest.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return set()
    items = data.get("accounts") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return set()
    emails: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        email = str(item.get("email") or "").strip().lower()
        if "@" in email:
            emails.add(email)
    return emails


def account_row_matches_pool(row: dict[str, Any], pool_emails: set[str]) -> bool:
    if not pool_emails:
        return "@" in str(row.get("name") or "")
    haystack = " ".join(
        str(row.get(key) or "").lower()
        for key in ("name", "email", "account_email")
    )
    return any(email in haystack for email in pool_emails)


def _max_time_text(values: list[str]) -> str:
    latest_text = ""
    latest_dt: datetime | None = None
    for value in values:
        dt = _parse_time(value)
        if dt is None:
            continue
        if latest_dt is None or dt > latest_dt:
            latest_dt = dt
            latest_text = value
    return latest_text


def _soonest_future_time_text(values: list[str]) -> str:
    now = datetime.now(timezone.utc)
    selected_text = ""
    selected_dt: datetime | None = None
    for value in values:
        dt = _parse_time(value)
        if dt is None or dt <= now:
            continue
        if selected_dt is None or dt < selected_dt:
            selected_dt = dt
            selected_text = value
    return selected_text


def merge_account_windows(rows: list[dict[str, Any]], window_key: str) -> dict[str, Any]:
    windows = [
        row.get(window_key)
        for row in rows
        if isinstance(row.get(window_key), dict)
    ]
    if not windows:
        return {}
    result: dict[str, Any] = {
        "requests": sum(int(window.get("requests") or 0) for window in windows),
        "tokens": sum(int(window.get("tokens") or 0) for window in windows),
        "cost": round(sum(float(window.get("cost") or 0) for window in windows), 6),
    }
    latest_at = _max_time_text([str(window.get("latest_at") or "") for window in windows])
    if latest_at:
        result["latest_at"] = latest_at
    latest_model = ""
    latest_dt = _parse_time(latest_at)
    if latest_dt is not None:
        for window in windows:
            if _parse_time(str(window.get("latest_at") or "")) == latest_dt:
                latest_model = str(window.get("latest_model") or "")
                break
    if latest_model:
        result["latest_model"] = latest_model
    starts = [str(window.get("start_at") or "") for window in windows if window.get("start_at")]
    ends = [str(window.get("end_at") or "") for window in windows if window.get("end_at")]
    if starts:
        result["start_at"] = min(starts)
    if ends:
        result["end_at"] = max(ends)

    quota_windows = [window for window in windows if window.get("quota_available")]
    if quota_windows:
        result["quota_available"] = True
        result["quota_stale"] = any(bool(window.get("quota_stale")) for window in quota_windows)
        result["utilization"] = round(
            sum(float(window.get("utilization") or 0) for window in quota_windows),
            2,
        )
        result["remaining_percent"] = round(
            sum(float(window.get("remaining_percent") or 0) for window in quota_windows),
            2,
        )
        reset = _soonest_future_time_text([str(window.get("resets_at") or "") for window in quota_windows])
        if reset:
            result["resets_at"] = reset
    return result


def build_api_service_pool_row(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not rows:
        return None
    requests_count = sum(int(row.get("requests") or 0) for row in rows)
    tokens = sum(int(row.get("tokens") or 0) for row in rows)
    cost = round(sum(float(row.get("cost") or 0) for row in rows), 6)
    if requests_count <= 0 and tokens <= 0 and cost <= 0:
        return None
    latest_at = _max_time_text([str(row.get("latest_at") or "") for row in rows])
    latest_model = ""
    latest_dt = _parse_time(latest_at)
    if latest_dt is not None:
        for row in rows:
            if _parse_time(str(row.get("latest_at") or "")) == latest_dt:
                latest_model = str(row.get("latest_model") or "")
                break
    window_5h = merge_account_windows(rows, "window_5h")
    window_7d = merge_account_windows(rows, "window_7d")
    window_30d = merge_account_windows(rows, "window_30d")
    window_cycle = merge_account_windows(rows, "window_cycle")
    for window in (window_5h, window_7d, window_cycle):
        for key in ("quota_available", "quota_stale", "utilization", "remaining_percent", "resets_at"):
            window.pop(key, None)
    return {
        "name": "api-service-local",
        "tokens": tokens,
        "requests": requests_count,
        "cost": cost,
        "health_badge": "",
        "source_badge": "LOCAL",
        "latest_at": latest_at,
        "latest_model": latest_model,
        "window_5h": window_5h,
        "window_7d": window_7d,
        "window_30d": window_30d,
        "window_cycle": window_cycle,
        "active_now": any(bool(row.get("active_now")) for row in rows),
        "is_latest": any(bool(row.get("is_latest")) for row in rows),
        "is_pool_aggregate": True,
    }


def load_client_route_labels() -> dict[str, set[str]]:
    empty = {"sub2api_mirrored": set(), "direct": set()}
    try:
        data = json.loads(CLIENT_USAGE_ROUTE_LABELS_JSON.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return empty
    if not isinstance(data, dict):
        return empty
    result: dict[str, set[str]] = {}
    for key in empty:
        values = data.get(key)
        if isinstance(values, list):
            result[key] = {str(value) for value in values if str(value).strip()}
        else:
            result[key] = set()
    return result


def write_client_route_labels(labels: dict[str, set[str]]) -> None:
    payload = {
        "schema": 1,
        "updated_at": datetime.now(CN_TZ).isoformat(timespec="seconds"),
        "sub2api_mirrored": sorted(labels.get("sub2api_mirrored", set())),
        "direct": sorted(labels.get("direct", set())),
    }
    try:
        CLIENT_USAGE_ROUTE_LABELS_JSON.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


def update_client_route_label(provider_name: str, points_to_sub2api: bool | None) -> dict[str, set[str]]:
    labels = load_client_route_labels()
    if not is_local_api_key_provider_name(provider_name):
        return labels
    if points_to_sub2api is True:
        if provider_name not in labels["direct"]:
            labels["sub2api_mirrored"].add(provider_name)
            write_client_route_labels(labels)
    elif points_to_sub2api is False:
        labels["direct"].add(provider_name)
        labels["sub2api_mirrored"].discard(provider_name)
        write_client_route_labels(labels)
    return labels


def backfill_sub2api_mirrored_api_key_labels(
    client_usage: dict[str, Any] | None,
    server_tokens: int,
    current_provider_name: str,
    points_to_sub2api: bool | None,
    labels: dict[str, set[str]],
) -> dict[str, set[str]]:
    if not isinstance(client_usage, dict) or server_tokens <= 0:
        return labels
    providers = client_usage.get("providers")
    if not isinstance(providers, list):
        return labels

    changed = False
    token_ceiling = max(1, int(server_tokens * 1.25))
    for provider in providers:
        if not isinstance(provider, dict):
            continue
        name = str(provider.get("name") or "")
        tokens = int(provider.get("tokens") or 0)
        if not is_local_api_key_provider_name(name) or tokens <= 0:
            continue
        if name in labels["direct"] or name in labels["sub2api_mirrored"]:
            continue
        if points_to_sub2api is False and name == current_provider_name:
            continue
        if tokens <= token_ceiling:
            labels["sub2api_mirrored"].add(name)
            changed = True
    if changed:
        write_client_route_labels(labels)
    return labels


def subtract_sub2api_mirrored_api_key_usage(
    client_usage: dict[str, Any] | None,
    server_tokens: int,
    route_labels: dict[str, set[str]] | None = None,
) -> dict[str, Any] | None:
    """Remove local API service/key rows that mirror already-counted Sub2API traffic."""
    if not isinstance(client_usage, dict):
        return client_usage
    providers = client_usage.get("providers")
    if not isinstance(providers, list):
        return client_usage

    result = client_usage
    mirrored = (route_labels or {}).get("sub2api_mirrored", set())
    for provider in providers:
        if not isinstance(provider, dict):
            continue
        name = str(provider.get("name") or "")
        tokens = int(provider.get("tokens") or 0)
        if server_tokens > 0 and is_local_api_key_provider_name(name) and name in mirrored and tokens > 0:
            result = subtract_provider_from_client_usage(result, name)
        elif is_local_api_service_provider_name(name) and tokens > 0:
            result = subtract_provider_from_client_usage(result, name)
    return result


def local_usage_from_providers(client_usage: dict[str, Any] | None, prefixes: tuple[str, ...]) -> dict[str, Any] | None:
    if not client_usage:
        return None
    providers = client_usage.get("providers")
    if not isinstance(providers, list):
        return None

    selected: list[dict[str, Any]] = []
    for provider in providers:
        if not isinstance(provider, dict):
            continue
        name = str(provider.get("name") or "")
        if any(name.lower().startswith(prefix.lower()) for prefix in prefixes):
            selected.append(provider)
    if not selected:
        return None

    requests_count = sum(int(provider.get("requests") or 0) for provider in selected)
    tokens = sum(int(provider.get("tokens") or 0) for provider in selected)
    cost = sum(float(provider.get("cost") or 0) for provider in selected)
    if requests_count <= 0 and tokens <= 0 and cost <= 0:
        return None

    return {
        "requests": requests_count,
        "tokens": tokens,
        "cost": cost,
        "providers": selected,
        "updated_at": client_usage.get("updated_at") or "",
    }


def combine_client_usage(usages: list[dict[str, Any] | None]) -> dict[str, Any] | None:
    selected = [usage for usage in usages if usage and (usage.get("requests") or usage.get("tokens") or usage.get("cost"))]
    if not selected:
        return None

    providers: list[dict[str, Any]] = []
    for usage in selected:
        usage_providers = usage.get("providers")
        if isinstance(usage_providers, list):
            providers.extend([provider for provider in usage_providers if isinstance(provider, dict)])

    return {
        "requests": sum(int(usage.get("requests") or 0) for usage in selected),
        "tokens": sum(int(usage.get("tokens") or 0) for usage in selected),
        "cost": sum(float(usage.get("cost") or 0) for usage in selected),
        "providers": providers,
        "updated_at": max([str(usage.get("updated_at") or "") for usage in selected], default=""),
    }


def residual_client_usage(
    client_usage: dict[str, Any] | None,
    server_requests: int,
    server_tokens: int,
    server_cost: float,
) -> dict[str, Any] | None:
    if not client_usage:
        return None

    raw_requests = int(client_usage.get("requests") or 0)
    raw_tokens = int(client_usage.get("tokens") or 0)
    raw_cost = float(client_usage.get("cost") or 0)
    if raw_requests <= 0 and raw_tokens <= 0 and raw_cost <= 0:
        return None

    local_requests = max(0, raw_requests - max(0, int(server_requests or 0)))
    local_tokens = max(0, raw_tokens - max(0, int(server_tokens or 0)))
    local_cost = max(0.0, raw_cost - max(0.0, float(server_cost or 0)))
    if local_tokens > 0 and local_requests == 0:
        local_requests = 1
    if local_tokens > 0 and local_cost == 0 and raw_tokens > 0:
        local_cost = raw_cost * (local_tokens / raw_tokens)

    if local_requests <= 0 and local_tokens <= 0 and local_cost <= 0:
        return None

    result = dict(client_usage)
    result["requests"] = local_requests
    result["tokens"] = local_tokens
    result["cost"] = local_cost
    result["raw_requests"] = raw_requests
    result["raw_tokens"] = raw_tokens
    result["raw_cost"] = raw_cost
    result["deducted_requests"] = max(0, int(server_requests or 0))
    result["deducted_tokens"] = max(0, int(server_tokens or 0))
    result["deducted_cost"] = max(0.0, float(server_cost or 0))
    return result


@dataclass
class MonitorState:
    loading: bool = True
    error: str | None = None
    updated_at: float | None = None
    mode: str = "sub2api"
    source_label: str = "MONITOR"
    usage_source: str = "sub2api"
    usage_note: str = ""
    active_accounts: list[dict[str, Any]] | None = None
    latest_request: dict[str, Any] | None = None
    latest_account_name: str = ""
    today_requests: int = 0
    today_tokens: int = 0
    today_account_cost: float = 0.0
    cost_history: dict[str, Any] | None = None
    top_accounts: list[dict[str, Any]] | None = None
    client_usage: dict[str, Any] | None = None
    client_usage_history: dict[str, Any] | None = None
    usage_sync: dict[str, Any] | None = None


def build_local_monitor_state(
    error_text: str | None = None,
    usage_note: str = "客户端日志",
    include_30d: bool = False,
    refresh_usage: bool = True,
) -> MonitorState:
    client_usage = load_client_usage(
        include_30d=include_30d,
        run_export=refresh_usage,
    ) or {
        "requests": 0,
        "tokens": 0,
        "cost": 0.0,
        "providers": [],
        "updated_at": "",
    }
    providers = client_usage.get("providers") if isinstance(client_usage, dict) else []
    client_latest = client_usage.get("latest_request") if isinstance(client_usage, dict) else {}
    latest_provider_name = str(client_latest.get("provider") or "") if isinstance(client_latest, dict) else ""
    top_accounts: list[dict[str, Any]] = []
    if isinstance(providers, list):
        for provider in providers:
            if not isinstance(provider, dict):
                continue
            top_accounts.append(
                {
                    "name": local_provider_display_name(str(provider.get("name") or "Local client")),
                    "tokens": int(provider.get("tokens") or 0),
                    "requests": int(provider.get("requests") or 0),
                    "cost": float(provider.get("cost") or 0),
                    "health_badge": "",
                    "source_badge": "LOCAL",
                    "plan_type": provider.get("plan_type") or "",
                    "app_speed": provider.get("app_speed") or "",
                    "cost_multiplier": provider.get("cost_multiplier") or 1,
                    "speed_badge": provider.get("speed_badge") or "",
                    "models": dict(provider.get("models") or {}),
                    "latest_at": provider.get("latest_at") or "",
                    "latest_model": provider.get("latest_model") or "",
                    "window_5h": provider.get("window_5h") or {},
                    "window_7d": provider.get("window_7d") or {},
                    "window_rolling_7d": provider.get("window_rolling_7d") or {},
                    "window_30d": provider.get("window_30d") or {},
                    "window_cycle": provider.get("window_cycle") or {},
                    "active_now": False,
                    "window_only": bool(provider.get("window_only")),
                    "is_unattributed_gap": bool(provider.get("is_unattributed_gap")),
                    "is_latest": str(provider.get("name") or "") == latest_provider_name,
                }
            )
    top_accounts.sort(key=lambda row: (-row["tokens"], -row["requests"], row["name"]))

    updated_at = client_usage.get("updated_at") if isinstance(client_usage, dict) else ""
    latest_request = None
    latest_account_name = "客户端日志"
    active_accounts: list[dict[str, Any]] = []
    if isinstance(client_latest, dict) and client_latest.get("created_at"):
        provider_name = str(client_latest.get("provider") or "Local client")
        latest_provider = next(
            (provider for provider in providers if isinstance(provider, dict) and str(provider.get("name") or "") == provider_name),
            {},
        )
        latest_request = {
            "kind": client_latest.get("kind") or "success",
            "model": client_latest.get("model") or "-",
            "created_at": client_latest.get("created_at"),
            "source": "LOCAL",
            "speed_badge": latest_provider.get("speed_badge") or "",
        }
        latest_account_name = f"LOCAL - {local_provider_display_name(provider_name)}"
        if is_recent_activity(str(client_latest.get("created_at") or "")):
                active_accounts.append(
                    {
                    "id": "local",
                    "name": latest_account_name,
                    "current": 1,
                    "max": 1,
                    "model": client_latest.get("model") or "-",
                    "source": "LOCAL",
                    "speed_badge": latest_provider.get("speed_badge") or "",
                }
            )
    elif updated_at:
        latest_request = {
            "kind": "success",
            "model": "Codex",
            "created_at": updated_at,
            "source": "LOCAL",
        }
        if is_recent_activity(str(updated_at)):
            active_accounts.append(
                {
                    "id": "local",
                    "name": latest_account_name,
                    "current": 1,
                    "max": 1,
                    "model": "Codex",
                    "source": "LOCAL",
                }
            )

    active_accounts = local_active_accounts_from_client_usage(client_usage)
    usage_sync = client_usage.get("sync") if isinstance(client_usage.get("sync"), dict) else {}
    history_summary = summarize_usage_history(load_usage_history())
    trend_history = trend_with_current_totals(
        history_summary,
        int(client_usage.get("tokens") or 0),
        int(client_usage.get("requests") or 0),
        float(client_usage.get("cost") or 0),
    )
    return MonitorState(
        loading=False,
        error=error_text,
        updated_at=time.time(),
        mode="local-codex",
        source_label="CLIENT",
        usage_source="local",
        usage_note=usage_note,
        active_accounts=active_accounts,
        latest_request=latest_request,
        latest_account_name=latest_account_name,
        today_requests=int(client_usage.get("requests") or 0),
        today_tokens=int(client_usage.get("tokens") or 0),
        today_account_cost=float(client_usage.get("cost") or 0),
        cost_history=trend_history,
        top_accounts=top_accounts,
        client_usage=client_usage,
        client_usage_history=history_summary,
        usage_sync=usage_sync,
    )


def build_sub2api_error_state(error_text: str, usage_note: str) -> MonitorState:
    return MonitorState(
        loading=False,
        error=error_text,
        updated_at=time.time(),
        mode="sub2api",
        source_label="MONITOR",
        usage_source="sub2api",
        usage_note=usage_note,
        active_accounts=[],
        latest_request=None,
        latest_account_name="",
        today_requests=0,
        today_tokens=0,
        today_account_cost=0.0,
        top_accounts=[],
        client_usage=None,
        client_usage_history=summarize_usage_history(load_usage_history()),
    )


def empty_client_usage() -> dict[str, Any]:
    return {
        "requests": 0,
        "tokens": 0,
        "cost": 0.0,
        "providers": [],
        "updated_at": "",
    }


class Sub2APIClient:
    def __init__(self) -> None:
        env = read_env_files(ENV_FILES)
        self.base_url = os.environ.get("SUB2API_BASE_URL") or env.get("SUB2API_BASE_URL") or DEFAULT_BASE_URL
        self.base_url = self.base_url.rstrip("/")
        self.email = os.environ.get("SUB2API_ADMIN_EMAIL") or env.get("ADMIN_EMAIL") or "admin@sub2api.local"
        self.password = os.environ.get("SUB2API_ADMIN_PASSWORD") or env.get("ADMIN_PASSWORD") or ""
        self.mode = (
            os.environ.get("TOKEN_MONITOR_MODE")
            or env.get("TOKEN_MONITOR_MODE")
            or os.environ.get("SUB2API_MONITOR_MODE")
            or env.get("SUB2API_MONITOR_MODE")
            or "auto"
        ).strip().lower()
        usage_source = os.environ.get("SUB2API_MONITOR_USAGE_SOURCE") or env.get("SUB2API_MONITOR_USAGE_SOURCE") or ""
        self.usage_source = usage_source.strip().lower() or ("both" if env_bool(env, "SUB2API_INCLUDE_LOCAL_USAGE", False) else "auto")
        self.token: str | None = None
        self._client_usage_cache: dict[str, Any] | None = None
        self._client_usage_cache_at: float = 0.0
        self._client_usage_cache_has_30d = False
        self._client_usage_cache_has_history_details = False
        self._account_window_cache: dict[int, tuple[float, dict[str, Any]]] = {}
        self._account_30d_cache: dict[int, tuple[float, dict[str, Any]]] = {}
        self.include_account_30d = False
        self.include_history_details = False

    def _sub2api_match_urls(self) -> list[str]:
        env = read_env_files(ENV_FILES)
        urls = [self.base_url]
        extra = os.environ.get("SUB2API_MATCH_BASE_URLS") or env.get("SUB2API_MATCH_BASE_URLS") or ""
        for item in extra.split(","):
            item = item.strip()
            if item:
                urls.append(item)
        return [strip_url_path(url) for url in urls if strip_url_path(url)]

    def _codex_points_to_sub2api(self) -> tuple[bool | None, list[str]]:
        codex_urls = detect_codex_base_urls()
        if not codex_urls:
            return None, []
        sub2api_urls = self._sub2api_match_urls()
        active_url = codex_urls[0]
        if any(same_endpoint(active_url, sub2api_url) for sub2api_url in sub2api_urls):
            return True, codex_urls
        return False, codex_urls

    def _resolve_usage_source(self) -> tuple[str, str]:
        if self.usage_source in {"sub2api", "server"}:
            return "sub2api", "手动: Sub2API"
        if self.usage_source in {"local", "local-codex", "client"}:
            return "local", "手动: 客户端日志"
        if self.usage_source in {"both", "merge", "all"}:
            return "both", "手动: 合并显示"
        if self.mode in {"local", "local-codex", "client", "client-local"}:
            return "local", "客户端日志"
        if self.mode in {"sub2api", "server"}:
            return "sub2api", "手动: Sub2API"
        points_to_sub2api, codex_urls = self._codex_points_to_sub2api()
        if points_to_sub2api is True:
            return "sub2api", "Auto: Codex -> Sub2API"
        if points_to_sub2api is False:
            first = codex_urls[0] if codex_urls else ""
            return "local", f"Auto: Codex -> {strip_url_path(first) or 'other API'}"
        return "local", "Auto: 未确认 Codex endpoint"

    def _should_include_client_usage(self, resolved_source: str) -> bool:
        if self.usage_source in {"sub2api", "server"}:
            return False
        if self.usage_source in {"both", "merge", "all", "local", "local-codex", "client"}:
            return True
        return resolved_source in {"sub2api", "local", "both"}

    def _load_client_usage_cached(self) -> dict[str, Any] | None:
        now = time.time()
        if (
            self._client_usage_cache is not None
            and now - self._client_usage_cache_at < CLIENT_USAGE_CACHE_SECONDS
            and (not self.include_history_details or self._client_usage_cache_has_history_details)
        ):
            return self._client_usage_cache
        client_usage = load_client_usage(
            include_30d=False,
            backfill_history_details=self.include_history_details,
        )
        self._client_usage_cache = client_usage
        self._client_usage_cache_at = now
        self._client_usage_cache_has_30d = False
        self._client_usage_cache_has_history_details = self.include_history_details
        return client_usage

    def clear_client_usage_cache(self) -> None:
        self._client_usage_cache = None
        self._client_usage_cache_at = 0.0
        self._client_usage_cache_has_30d = False
        self._client_usage_cache_has_history_details = False

    def clear_runtime_caches(self) -> None:
        self.clear_client_usage_cache()
        self._account_window_cache.clear()
        self._account_30d_cache.clear()

    def _load_account_windows_cached(self, account: dict[str, Any]) -> dict[str, Any]:
        account_id = int(account.get("id") or 0)
        if account_id <= 0 or not account_has_email(account) or str(account.get("type") or "").lower() != "oauth":
            return {}

        now = time.time()
        cached = self._account_window_cache.get(account_id)
        if cached and now - cached[0] < ACCOUNT_WINDOW_CACHE_SECONDS:
            return cached[1]

        try:
            usage = self._request("GET", f"/api/v1/admin/accounts/{account_id}/usage") or {}
            result = {
                "window_5h": normalize_usage_window(usage.get("five_hour")),
                "window_7d": normalize_usage_window(usage.get("seven_day")),
                "window_cycle": normalize_usage_window(usage.get("cycle") or usage.get("primary_window")),
            }
            self._account_window_cache[account_id] = (now, result)
            return result
        except Exception:
            return cached[1] if cached else {}

    def _load_account_30d_cached(self, account: dict[str, Any]) -> dict[str, Any]:
        account_id = int(account.get("id") or 0)
        if account_id <= 0:
            return {}
        now = time.time()
        cached = self._account_30d_cache.get(account_id)
        if cached and now - cached[0] < ACCOUNT_STATS_CACHE_SECONDS:
            return cached[1]
        try:
            stats = self._request(
                "GET",
                f"/api/v1/admin/accounts/{account_id}/stats",
                params={"days": 30},
            ) or {}
            summary = stats.get("summary") if isinstance(stats, dict) else {}
            if not isinstance(summary, dict):
                summary = {}
            result = {
                "requests": int(summary.get("total_requests") or 0),
                "tokens": int(summary.get("total_tokens") or 0),
                "cost": float(summary.get("total_cost") or 0),
            }
            self._account_30d_cache[account_id] = (now, result)
            return result
        except Exception:
            return cached[1] if cached else {}

    def _load_account_30d_batch(self, accounts: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
        valid_accounts = [account for account in accounts if int(account.get("id") or 0) > 0]
        if not valid_accounts:
            return {}
        with ThreadPoolExecutor(max_workers=min(8, len(valid_accounts))) as executor:
            windows = executor.map(self._load_account_30d_cached, valid_accounts)
            return {
                int(account.get("id") or 0): window
                for account, window in zip(valid_accounts, windows)
            }

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        retry_auth: bool = True,
    ) -> Any:
        query = ""
        if params:
            query = "?" + parse.urlencode({k: v for k, v in params.items() if v is not None})
        body = None
        headers = {"Content-Type": "application/json"}
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        req = request.Request(f"{self.base_url}{path}{query}", data=body, headers=headers, method=method)
        try:
            with request.urlopen(req, timeout=10) as resp:
                raw = resp.read().decode("utf-8")
        except error.HTTPError as exc:
            if exc.code == 401 and retry_auth:
                self.login()
                return self._request(method, path, payload, params, retry_auth=False)
            detail = exc.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"HTTP {exc.code}: {detail[:160]}")
        except error.URLError as exc:
            raise RuntimeError(f"无法连接 {self.base_url}: {exc.reason}")

        data = json.loads(raw) if raw else {}
        if isinstance(data, dict) and "code" in data:
            if data.get("code") == 0:
                return data.get("data")
            raise RuntimeError(str(data.get("message") or data.get("reason") or "接口返回错误"))
        return data

    def _fetch_dashboard_trend(self) -> dict[str, Any]:
        params = {
            "start_date": date_key(6),
            "end_date": today_key(),
            "granularity": "day",
            "timezone": DISPLAY_TIMEZONE,
        }
        data = self._request("GET", "/api/v1/admin/dashboard/trend", params=params) or {}
        trend = data.get("trend") if isinstance(data, dict) else []
        if not isinstance(trend, list):
            trend = []
        return summarize_trend_rows([row for row in trend if isinstance(row, dict)])

    def login(self) -> None:
        if not self.password:
            raise RuntimeError("没有找到管理员密码，请检查 deploy/.env 或 SUB2API_ADMIN_PASSWORD")
        data = self._request(
            "POST",
            "/api/v1/auth/login",
            {"email": self.email, "password": self.password},
            retry_auth=False,
        )
        if isinstance(data, dict) and data.get("requires_2fa"):
            raise RuntimeError("管理员账号开启了 2FA，桌面监控暂不支持自动登录")
        token = data.get("access_token") if isinstance(data, dict) else None
        if not token:
            raise RuntimeError("登录成功但没有返回 access_token")
        self.token = str(token)

    def fetch_state(self) -> MonitorState:
        if self.mode in {"local", "local-codex", "client", "client-local"}:
            return build_local_monitor_state(
                usage_note="客户端日志（独立监控）",
                include_30d=self.include_account_30d,
            )

        if self.mode in {"", "auto", "fallback", "auto-sub2api"}:
            points_to_sub2api, codex_urls = self._codex_points_to_sub2api()
            if points_to_sub2api is not True:
                first = codex_urls[0] if codex_urls else ""
                endpoint_note = strip_url_path(first) or "未确认 Codex endpoint"
                return build_local_monitor_state(
                    usage_note=f"Auto: Codex -> {endpoint_note} / 客户端日志",
                    include_30d=self.include_account_30d,
                )
            try:
                return self.fetch_sub2api_state()
            except Exception as exc:
                return build_local_monitor_state(
                    str(exc),
                    "Auto: Codex -> Sub2API / Sub2API 不可用，已切到客户端日志",
                    include_30d=self.include_account_30d,
                )

        resolved_source, usage_note = self._resolve_usage_source()
        try:
            return self.fetch_sub2api_state()
        except Exception as exc:
            if self.mode in {"fallback", "auto-sub2api"}:
                return build_local_monitor_state(
                    str(exc),
                    f"{usage_note} / Sub2API 不可用，已切到客户端日志",
                    include_30d=self.include_account_30d,
                )
            raise

    def fetch_sub2api_state(self) -> MonitorState:
        resolved_source, usage_note = self._resolve_usage_source()
        if not self.token:
            self.login()

        stats = self._request("GET", "/api/v1/admin/dashboard/stats") or {}
        accounts_resp = self._request(
            "GET",
            "/api/v1/admin/accounts",
            params={"page": 1, "page_size": 1000, "platform": "openai", "sort_by": "priority", "sort_order": "asc"},
        ) or {}
        accounts = accounts_resp.get("items") or []
        account_map = {int(item.get("id")): item for item in accounts if item.get("id") is not None}

        try:
            concurrency_resp = self._request("GET", "/api/v1/admin/ops/concurrency", params={"platform": "openai"}) or {}
            concurrency = concurrency_resp.get("account") or {}
        except Exception:
            concurrency = {}

        try:
            requests_resp = self._request(
                "GET",
                "/api/v1/admin/ops/requests",
                params={
                    "time_range": "30d",
                    "kind": "all",
                    "platform": "openai",
                    "page": 1,
                    "page_size": 1,
                    "sort": "created_at_desc",
                },
            ) or {}
            latest = (requests_resp.get("items") or [None])[0]
        except Exception:
            latest = None

        try:
            trend_history = self._fetch_dashboard_trend()
        except Exception:
            trend_history = summarize_usage_history(load_usage_history())

        account_ids = [int(item["id"]) for item in accounts if item.get("id") is not None]
        today_by_account: dict[str, Any] = {}
        if account_ids:
            try:
                batch = self._request(
                    "POST",
                    "/api/v1/admin/accounts/today-stats/batch",
                    {"account_ids": account_ids},
                ) or {}
                today_by_account = batch.get("stats") or {}
            except Exception:
                today_by_account = {}

        active_accounts = []
        for item in concurrency.values():
            current = int(item.get("current_in_use") or item.get("current_concurrency") or item.get("current") or item.get("in_use") or 0)
            if current <= 0:
                continue
            account_id = int(item.get("account_id") or 0)
            account_info = account_map.get(account_id, {})
            active_accounts.append(
                {
                    "id": account_id,
                    "name": account_info.get("name") or item.get("account_name") or f"账号 #{account_id}",
                    "current": current,
                        "max": int(item.get("max_capacity") or item.get("concurrency") or account_info.get("concurrency") or current),
                        "plan_type": account_info.get("plan_type") or account_info.get("subscription_type") or account_info.get("account_type") or "",
                    }
            )
        active_by_id = {int(row.get("id") or 0): row for row in active_accounts if row.get("id") is not None}
        for account in accounts:
            account_id = int(account.get("id") or 0)
            current = int(account.get("current_concurrency") or account.get("current_in_use") or 0)
            if current <= 0:
                continue
            existing = active_by_id.get(account_id)
            if existing:
                existing["current"] = max(int(existing.get("current") or 0), current)
                existing["max"] = max(int(existing.get("max") or 0), int(account.get("concurrency") or current))
                continue
            row = {
                "id": account_id,
                "name": account.get("name") or f"账号 #{account_id}",
                "current": current,
                "max": int(account.get("concurrency") or current),
                "plan_type": account.get("plan_type") or account.get("subscription_type") or account.get("account_type") or "",
            }
            active_accounts.append(row)
            active_by_id[account_id] = row
        active_accounts.sort(key=lambda row: (-row["current"], row["id"]))

        latest_account_name = ""
        if latest and latest.get("account_id"):
            latest_id = int(latest["account_id"])
            latest_account_name = account_map.get(latest_id, {}).get("name") or f"账号 #{latest_id}"

        top_accounts = []
        account_30d_by_id = (
            self._load_account_30d_batch(accounts)
            if self.include_account_30d
            else {}
        )
        realtime_today_requests = 0
        realtime_today_tokens = 0
        realtime_today_cost = 0.0
        for account in accounts:
            account_id = int(account.get("id") or 0)
            account_stats = today_by_account.get(str(account_id)) or {}
            account_windows = self._load_account_windows_cached(account)
            tokens = int(account_stats.get("tokens") or 0)
            requests_count = int(account_stats.get("requests") or 0)
            cost = float(account_stats.get("cost") or 0)
            is_latest = bool(latest and int(latest.get("account_id") or 0) == account_id)
            realtime_today_requests += requests_count
            realtime_today_tokens += tokens
            realtime_today_cost += cost
            top_accounts.append(
                {
                    "name": account.get("name") or f"账号 #{account_id}",
                    "tokens": tokens,
                    "requests": requests_count,
                    "cost": cost,
                    "health_badge": account_health_badge(account),
                    "source_badge": "SUB",
                    "plan_type": account.get("plan_type") or account.get("subscription_type") or account.get("account_type") or "",
                    "window_5h": account_windows.get("window_5h") or {},
                    "window_7d": account_windows.get("window_7d") or {},
                    "window_30d": account_30d_by_id.get(account_id) or {},
                    "window_cycle": account_windows.get("window_cycle") or {},
                    "active_now": any(int(row.get("id") or 0) == account_id for row in active_accounts),
                    "is_latest": is_latest,
                    "latest_at": str(latest.get("created_at") or "") if is_latest else "",
                    "latest_model": str(latest.get("model") or "") if is_latest else "",
                }
            )
        top_accounts.sort(key=lambda row: (-row["tokens"], -row["requests"], row["name"]))
        sub2api_pool_rows = list(top_accounts)
        include_client_usage = self._should_include_client_usage(resolved_source)
        points_to_sub2api, _codex_urls = self._codex_points_to_sub2api()
        show_local_activity = include_client_usage and points_to_sub2api is not True
        raw_client_usage = self._load_client_usage_cached() if include_client_usage else None
        client_usage = subtract_sub2api_routed_client_usage(raw_client_usage)
        raw_providers = raw_client_usage.get("providers") if isinstance(raw_client_usage, dict) else []
        has_api_service_local_provider = any(
            isinstance(provider, dict)
            and is_local_api_service_provider_name(str(provider.get("name") or ""))
            for provider in (raw_providers if isinstance(raw_providers, list) else [])
        )
        api_service_pool_emails = (
            load_local_api_service_pool_emails()
            if has_api_service_local_provider
            else set()
        )
        route_labels = load_client_route_labels()
        raw_latest = raw_client_usage.get("latest_request") if isinstance(raw_client_usage, dict) else {}
        latest_provider_name = (
            str(raw_latest.get("provider") or "")
            if isinstance(raw_latest, dict)
            else ""
        )
        if latest_provider_name:
            route_labels = update_client_route_label(latest_provider_name, points_to_sub2api)
        route_labels = backfill_sub2api_mirrored_api_key_labels(
            client_usage,
            realtime_today_tokens,
            latest_provider_name,
            points_to_sub2api,
            route_labels,
        )
        client_usage = subtract_sub2api_mirrored_api_key_usage(
            client_usage,
            realtime_today_tokens,
            route_labels,
        )
        if (
            points_to_sub2api is True
            and isinstance(raw_client_usage, dict)
            and client_usage is raw_client_usage
        ):
            routed_provider = (
                str(raw_latest.get("provider") or "")
                if isinstance(raw_latest, dict)
                else ""
            )
            if routed_provider and routed_provider.lower().startswith("codex local - api-key-"):
                client_usage = subtract_provider_from_client_usage(client_usage, routed_provider)
        if client_usage and (client_usage["tokens"] or client_usage["requests"] or client_usage["cost"]):
            today_requests = realtime_today_requests + int(client_usage.get("requests") or 0)
            today_tokens = realtime_today_tokens + int(client_usage.get("tokens") or 0)
            today_account_cost = realtime_today_cost + float(client_usage.get("cost") or 0)
            ledger_source = "both"
            ledger_note = f"{usage_note} / Sub2API + 客户端日志"
        else:
            today_requests = int(stats.get("today_requests") or realtime_today_requests)
            today_tokens = int(stats.get("today_tokens") or realtime_today_tokens)
            today_account_cost = float(stats.get("today_actual_cost") or realtime_today_cost)
            ledger_source = resolved_source
            ledger_note = usage_note

        sub2api_pool_candidates = [
            row
            for row in sub2api_pool_rows
            if account_row_matches_pool(row, api_service_pool_emails)
        ]
        api_service_pool_row = (
            build_api_service_pool_row(sub2api_pool_candidates)
            if has_api_service_local_provider
            else None
        )

        providers = client_usage.get("providers") if isinstance(client_usage, dict) else []
        local_pool_rows: list[dict[str, Any]] = []
        if isinstance(providers, list):
            for provider in providers:
                if not isinstance(provider, dict):
                    continue
                provider_name = str(provider.get("name") or "")
                if has_api_service_local_provider and is_local_api_service_provider_name(provider_name):
                    continue
                provider_tokens = int(provider.get("tokens") or 0)
                provider_requests = int(provider.get("requests") or 0)
                provider_cost = float(provider.get("cost") or 0)
                if (
                    provider_tokens <= 0
                    and provider_requests <= 0
                    and provider_cost <= 0
                    and not provider.get("show_zero")
                ):
                    continue
                row = {
                    "name": local_provider_display_name(provider_name or "Local client"),
                    "tokens": provider_tokens,
                    "requests": provider_requests,
                    "cost": provider_cost,
                    "health_badge": "",
                    "source_badge": "LOCAL",
                    "plan_type": provider.get("plan_type") or "",
                    "app_speed": provider.get("app_speed") or "",
                    "cost_multiplier": provider.get("cost_multiplier") or 1,
                    "speed_badge": provider.get("speed_badge") or "",
                    "models": dict(provider.get("models") or {}),
                    "latest_at": provider.get("latest_at") or "",
                    "latest_model": provider.get("latest_model") or "",
                    "window_5h": provider.get("window_5h") or {},
                    "window_7d": provider.get("window_7d") or {},
                    "window_rolling_7d": provider.get("window_rolling_7d") or {},
                    "window_30d": provider.get("window_30d") or {},
                    "window_cycle": provider.get("window_cycle") or {},
                    "active_now": False,
                    "window_only": bool(provider.get("window_only")),
                    "is_unattributed_gap": bool(provider.get("is_unattributed_gap")),
                    "is_latest": provider_name == latest_provider_name,
                }
                top_accounts.append(row)
                if account_row_matches_pool(row, api_service_pool_emails):
                    local_pool_rows.append(row)
            top_accounts.sort(key=lambda row: (-row["tokens"], -row["requests"], row["name"]))
        if api_service_pool_row is None and has_api_service_local_provider:
            api_service_pool_row = build_api_service_pool_row(local_pool_rows)
        if api_service_pool_row is not None:
            top_accounts.append(api_service_pool_row)
        top_accounts.sort(key=lambda row: (-row["tokens"], -row["requests"], row["name"]))

        display_latest = latest
        display_latest_account_name = latest_account_name
        client_latest = client_usage.get("latest_request") if isinstance(client_usage, dict) else {}
        if show_local_activity and isinstance(client_latest, dict) and client_latest.get("created_at"):
            provider_name = str(client_latest.get("provider") or "Local client")
            latest_provider = next(
                (provider for provider in providers if isinstance(provider, dict) and str(provider.get("name") or "") == provider_name),
                {},
            )
            local_latest = {
                "kind": client_latest.get("kind") or "success",
                "model": client_latest.get("model") or "-",
                "created_at": client_latest.get("created_at"),
                "source": "LOCAL",
                "speed_badge": latest_provider.get("speed_badge") or "",
            }
            local_dt = _parse_time(str(client_latest.get("created_at") or ""))
            sub_dt = _parse_time(str(latest.get("created_at") or "")) if isinstance(latest, dict) else None
            if sub_dt is None or (local_dt is not None and local_dt >= sub_dt):
                display_latest = local_latest
                display_latest_account_name = f"LOCAL - {local_provider_display_name(provider_name)}"

        for local_active in local_active_accounts_from_client_usage(
            client_usage,
            include_when_routed_to_sub2api=show_local_activity,
        ):
            local_name = str(local_active.get("name") or "")
            if not any(str(account.get("name") or "") == local_name for account in active_accounts):
                active_accounts.append(local_active)
        active_accounts.sort(
            key=lambda row: (-int(row.get("current") or 0), str(row.get("id") or ""), str(row.get("name") or ""))
        )

        return MonitorState(
            loading=False,
            updated_at=time.time(),
            mode="sub2api",
            source_label="MONITOR",
            usage_source=ledger_source,
            usage_note=ledger_note,
            active_accounts=active_accounts,
            latest_request=display_latest,
            latest_account_name=display_latest_account_name,
            today_requests=today_requests,
            today_tokens=today_tokens,
            today_account_cost=today_account_cost,
            cost_history=trend_history,
            top_accounts=top_accounts,
            client_usage=client_usage,
            client_usage_history=summarize_usage_history(load_usage_history()),
            usage_sync=(
                raw_client_usage.get("sync")
                if isinstance(raw_client_usage, dict)
                and isinstance(raw_client_usage.get("sync"), dict)
                else {}
            ),
        )


class Theme:
    """Graphite telemetry palette with clear live, data, and warning roles."""
    # ── base surfaces ──
    bg_dark = "#111417"
    bg_card = "#171B1F"
    bg_section = "#1D2328"
    bg_lift = "#242C32"
    bg_hover = "#2B353C"

    # ── signature accents ──
    live = "#58D6AD"
    data = "#78A8FF"
    warn = "#F2BF62"
    coral = "#FF756B"

    # Compatibility aliases used by the drawing code.
    amber_dim = "#28584F"
    amber = live
    amber_bright = "#F4F2ED"
    amber_glow = "#A7C2FF"
    cyan = data
    cyan_dim = "#29424C"
    violet = "#C7A6FF"
    blue = "#5A83D8"

    # ── text ──
    text_primary = "#F4F2ED"
    text_secondary = "#C1C8CB"
    text_muted = "#98A2A8"

    # ── semantic ──
    accent_cyan = data
    accent_red = coral
    accent_green = live
    quota_red_bg = "#3B2527"
    quota_amber_bg = "#3A3122"
    quota_green_bg = "#20352F"
    ag_bg = "#151A1E"
    ag_surface = "#1D2328"
    ag_surface_hover = "#29333A"
    ag_border = "#303A41"
    ag_divider = "#2A3238"
    ag_accent = data
    ag_bar = "#82AFFF"
    ag_success = live
    ag_warn = warn
    ag_crit = coral
    ag_muted = "#929CA2"
    ag_input = "#83D4E4"
    ag_cache = data
    ag_output = "#C7A6FF"
    ag_reason = warn

    # ── misc ──
    border = "#303940"
    shadow = "#080A0C"
    transparent = "#010203"

    # ── fonts (family, size, weight) ──
    font_title = ("Bahnschrift", 19, "bold")
    font_hero = ("Microsoft YaHei UI", 11, "bold")
    font_section = ("Microsoft YaHei UI", 10, "bold")
    font_label = ("Microsoft YaHei UI", 9, "normal")
    font_label_bold = ("Microsoft YaHei UI", 9, "bold")
    font_value = ("Bahnschrift", 19, "bold")
    font_value_sm = ("Bahnschrift", 14, "bold")
    font_value_xs = ("Bahnschrift", 12, "bold")
    font_tiny = ("Microsoft YaHei UI", 8, "normal")
    font_micro = ("Microsoft YaHei UI", 8, "normal")
    font_data = ("Cascadia Mono", 8, "normal")
    font_delta = ("Cascadia Mono", 10, "bold")
    font_icon = ("Segoe Fluent Icons", 10, "normal")


class FloatingMonitorApp:
    """Borderless always-on-top floating monitor built entirely on tk.Canvas."""

    WIDTH = 390
    HEIGHT = 760
    MIN_WIDTH = 360
    MIN_HEIGHT = 640
    WINDOW_ALPHA = 0.99

    def __init__(self) -> None:
        self.WIDTH = int(type(self).WIDTH)
        self.HEIGHT = int(type(self).HEIGHT)
        self.client = Sub2APIClient()
        self.state: MonitorState | None = None
        try:
            if self.client.mode in {"local", "local-codex", "client", "client-local"}:
                self.state = build_local_monitor_state(
                    usage_note="\u5ba2\u6237\u7aef\u65e5\u5fd7\uff08\u72ec\u7acb\u76d1\u63a7\uff09",
                    include_30d=self.client.include_account_30d,
                    refresh_usage=False,
                )
            elif self.client.mode in {"", "auto", "fallback", "auto-sub2api"}:
                points_to_sub2api, codex_urls = self.client._codex_points_to_sub2api()
                if points_to_sub2api is not True:
                    first = codex_urls[0] if codex_urls else ""
                    endpoint_note = strip_url_path(first) or "\u672a\u786e\u8ba4 Codex endpoint"
                    self.state = build_local_monitor_state(
                        usage_note=f"Auto: Codex -> {endpoint_note} / \u5ba2\u6237\u7aef\u65e5\u5fd7",
                        include_30d=self.client.include_account_30d,
                        refresh_usage=False,
                    )
        except Exception:
            self.state = None
        self.error: str | None = None
        self.closed = False
        self._pinned = True
        self._loading = False
        self._refresh_lock = threading.Lock()
        self._refresh_pending = False
        self._live_active_lock = threading.Lock()
        self._live_usage_lock = threading.Lock()
        self._live_catchup_lock = threading.Lock()
        self._quota_refresh_lock = threading.Lock()
        self._last_quota_refresh_at = 0.0
        self._full_refresh_requested = False
        self._last_forced_full_refresh_at = float("-inf")
        self._live_active_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="active-session-watch",
        )
        self._live_active_tail_cache: dict[
            Path,
            tuple[tuple[int, int], dict[str, Any]],
        ] = {}
        self._live_usage_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="token-usage-watch",
        )
        self._live_catchup_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="token-usage-catchup",
        )
        self._quota_refresh_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="quota-refresh",
        )
        self._last_auth_identity = ""
        self._capture_auth_switch(refresh_active=False)
        self._live_usage_watcher = CodexUsageFileWatcher(Path.home() / ".codex" / "sessions")
        try:
            self._live_usage_watcher.poll()
        except Exception:
            pass
        self._live_usage_overlay: dict[str, Any] | None = None
        self._live_usage_seen_ids: dict[str, None] = {}
        self._live_usage_event_records: dict[str, dict[str, Any]] = {}
        self._live_usage_rate_samples: list[tuple[float, int]] = []
        self._live_usage_verification_pending = False
        self._live_usage_verification_latest_when: datetime | None = None
        self._live_usage_verification_pending_tokens = 0
        self._last_live_checkpoint_write_at = float("-inf")
        self._live_reconcile_scheduled = False
        self._live_initial_recheck_scheduled = False
        self._last_live_reconcile_at = float("-inf")
        self._restore_live_usage_checkpoint()
        self._pulse_phase = 0.0
        self._pulse_tick_scheduled = False
        self._token_flow_samples: list[tuple[float, int]] = []
        self._token_flow_trace_rect: tuple[int, int, int, int] | None = None
        self._token_flow_meter_rect: tuple[int, int, int, int] | None = None
        self._token_flow_meter_fill_bounds: tuple[float, float, float, float] | None = None
        self._token_flow_meter_display_level = 0.0
        self._token_flow_meter_last_tick = time.monotonic()
        self._token_delta_value = 0
        self._token_delta_started_at = 0.0
        self._token_delta_last_event_at = 0.0
        self._cost_delta_value = 0.0
        self._cost_delta_started_at = 0.0
        self._cost_delta_last_event_at = 0.0
        self._fade_alpha = 0.0
        self._drag_data = {"x": 0, "y": 0}
        self._resize_data = {"x": 0, "y": 0, "w": self.WIDTH, "h": self.HEIGHT}
        self._resizing = False
        self._hover_btn: str | None = None
        self._btn_rects: dict[str, tuple[int, int, int, int]] = {}
        self._tooltip_rects: list[tuple[int, int, int, int, str]] = []
        self._tooltip_text = ""
        self._tooltip_pos = (0, 0)
        self._main_tab = "accounts"
        self._scroll_offsets = {"accounts": 0, "active": 0, "stats": 0}
        self._scroll_limits = {"accounts": 0, "active": 0, "stats": 0}
        self._active_scroll_rect: tuple[int, int, int, int] | None = None
        self._list_scrollbar_tracks: dict[str, tuple[int, int, int, int] | None] = {
            "accounts": None,
            "active": None,
            "stats": None,
        }
        self._list_scrollbar_thumbs: dict[str, tuple[int, int, int, int] | None] = {
            "accounts": None,
            "active": None,
            "stats": None,
        }
        self._list_scrollbar_drag_tab: str | None = None
        self._list_scrollbar_drag_offset = 0
        self._usage_range = "24h"
        self._account_range = "today"
        self._account_range_user_selected = False
        self._account_range_auto_selected = False
        self._topmost_repair_scheduled = False
        self._ignore_configure = False
        self._current_day_key = today_key()

        # ── root window ──
        self.root = tk.Tk()
        self.root.title("Token Monitor")
        self.root.overrideredirect(True)
        self.root.geometry(f"{self.WIDTH}x{self.HEIGHT}+1120+70")
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.0)
        self.root.configure(bg=Theme.transparent)
        try:
            self.root.attributes("-transparentcolor", Theme.transparent)
        except tk.TclError:
            self.root.configure(bg=Theme.bg_dark)

        # ── canvas ──
        self.canvas = tk.Canvas(
            self.root,
            width=self.WIDTH,
            height=self.HEIGHT,
            bg=Theme.transparent,
            highlightthickness=0,
            bd=0,
        )
        self.canvas.pack(fill="both", expand=True)

        # ── fonts (resolved) ──
        self._fonts: dict[str, tkfont.Font] = {}
        available_fonts = {family.casefold(): family for family in tkfont.families(self.root)}
        self._fluent_icons = False
        for attr in dir(Theme):
            if attr.startswith("font_"):
                family, size, weight = getattr(Theme, attr)
                if attr == "font_icon":
                    icon_family = available_fonts.get(family.casefold())
                    if not icon_family:
                        icon_family = available_fonts.get("segoe mdl2 assets")
                    if icon_family:
                        family = icon_family
                        self._fluent_icons = True
                    else:
                        family = "Segoe UI Symbol"
                else:
                    resolved_family = available_fonts.get(family.casefold())
                    if not resolved_family:
                        if attr == "font_data":
                            fallback_names = ("Consolas", "Segoe UI")
                        elif attr in {"font_title", "font_value", "font_value_sm", "font_value_xs"}:
                            fallback_names = ("Segoe UI Variable", "Segoe UI")
                        else:
                            fallback_names = ("Segoe UI Variable", "Segoe UI")
                        resolved_family = next(
                            (
                                available_fonts[name.casefold()]
                                for name in fallback_names
                                if name.casefold() in available_fonts
                            ),
                            family,
                        )
                    family = resolved_family
                self._fonts[attr] = tkfont.Font(
                    family=family, size=size, weight=weight
                )

        # ── bindings ──
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<Leave>", self._on_leave)
        self.canvas.bind("<Configure>", self._on_configure)
        self.canvas.bind("<MouseWheel>", self._on_mousewheel)
        self.canvas.bind("<Button-4>", self._on_mousewheel)
        self.canvas.bind("<Button-5>", self._on_mousewheel)

        # ── initial draw & data ──
        self._draw()
        self._fade_in()
        if self.state is None:
            self.refresh_async()
        self.root.after(REFRESH_SECONDS * 1000, self._schedule_auto_refresh)
        self._schedule_live_active_refresh()
        self._schedule_auth_switch_refresh()
        self._schedule_live_usage_refresh()
        self._refresh_live_usage_catchup_async()
        self._schedule_midnight_refresh()

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    #  GEOMETRY HELPERS
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    @staticmethod
    def _rounded_rect_points(
        x1: int, y1: int, x2: int, y2: int, r: int
    ) -> list[int]:
        """Return point list for a rounded rectangle (for create_polygon smooth)."""
        r = min(r, (x2 - x1) // 2, (y2 - y1) // 2)
        pts = []
        for a in range(180, 270 + 1, 10):
            rad = math.radians(a)
            pts += [x1 + r + r * math.cos(rad), y1 + r + r * math.sin(rad)]
        for a in range(270, 360 + 1, 10):
            rad = math.radians(a)
            pts += [x2 - r + r * math.cos(rad), y1 + r + r * math.sin(rad)]
        for a in range(0, 90 + 1, 10):
            rad = math.radians(a)
            pts += [x2 - r + r * math.cos(rad), y2 - r + r * math.sin(rad)]
        for a in range(90, 180 + 1, 10):
            rad = math.radians(a)
            pts += [x1 + r + r * math.cos(rad), y2 - r + r * math.sin(rad)]
        return pts

    def _draw_rounded_rect(
        self,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        r: int = 10,
        **kw: Any,
    ) -> int:
        pts = self._rounded_rect_points(x1, y1, x2, y2, r)
        return self.canvas.create_polygon(pts, smooth=True, **kw)

    def _draw_panel(
        self,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        *,
        fill: str | None = None,
        outline: str | None = None,
        radius: int = 8,
    ) -> int:
        return self._draw_rounded_rect(
            x1,
            y1,
            x2,
            y2,
            r=radius,
            fill=fill or Theme.ag_surface,
            outline=outline or Theme.ag_border,
            width=1,
        )

    def _draw_header_mark(self, x: int, y: int, color: str) -> None:
        """Draw the compact pulse-line product mark."""
        self._draw_rounded_rect(
            x,
            y,
            x + 29,
            y + 29,
            r=7,
            fill=Theme.bg_section,
            outline=Theme.border,
            width=1,
        )
        points = [
            x + 6, y + 16,
            x + 10, y + 16,
            x + 13, y + 10,
            x + 17, y + 21,
            x + 20, y + 14,
            x + 24, y + 14,
        ]
        self.canvas.create_line(
            *points,
            fill=color,
            width=2,
            capstyle="round",
            joinstyle="round",
        )

    def _token_flow_snapshot(self) -> tuple[float, int]:
        now = time.monotonic()
        retention_seconds = TOKEN_FLOW_TRACE_TRAVEL_SECONDS + 2.0
        samples = [
            (created_at, tokens)
            for created_at, tokens in getattr(self, "_token_flow_samples", [])
            if 0 <= now - created_at <= retention_seconds
        ]
        self._token_flow_samples = samples
        if not samples:
            return 0.0, 0
        weighted_tokens = sum(
            tokens * math.exp(-(now - created_at) / 3.2)
            for created_at, tokens in samples
        )
        recent_tokens = sum(
            tokens
            for created_at, tokens in samples
            if now - created_at <= TOKEN_FLOW_TRACE_TRAVEL_SECONDS
        )
        level = min(
            1.0,
            math.log1p(weighted_tokens / 10_000.0) / math.log1p(200.0),
        )
        return max(0.0, level), max(0, int(recent_tokens))

    def _draw_token_flow_meter(
        self,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        *,
        bars: int,
    ) -> None:
        width = max(1, x2 - x1)
        height = max(3, y2 - y1)
        segments = max(4, min(12, int(bars)))
        segment_gap = 1
        segment_h = max(2, (height - segment_gap * (segments - 1)) // segments)
        used_h = segments * segment_h + segment_gap * (segments - 1)
        bottom_y = y1 + max(0, (height - used_h) // 2) + used_h
        column_w = max(4, min(14, width))
        bx = x1 + max(0, (width - column_w) // 2)
        target_level, recent_tokens = self._token_flow_snapshot()
        display_level = self._smooth_token_flow_meter_level(target_level)
        self._token_flow_meter_rect = (x1, y1, x2, y2)
        column_top = bottom_y - used_h
        self._token_flow_meter_fill_bounds = (
            float(bx),
            float(column_top),
            float(bx + column_w),
            float(bottom_y),
        )
        for index in range(segments):
            segment_bottom = bottom_y - index * (segment_h + segment_gap)
            segment_top = segment_bottom - segment_h
            self._draw_rounded_rect(
                bx,
                segment_top,
                bx + column_w,
                segment_bottom,
                r=1,
                fill="#183B35",
                outline="",
                tags=("token_flow_meter_segment",),
            )
        fill_top = self._token_flow_meter_fill_top(
            display_level,
            column_top,
            bottom_y,
        )
        solid_top, head_bands = self._token_flow_meter_head_geometry(
            fill_top,
            bottom_y,
        )
        self.canvas.create_rectangle(
            bx,
            solid_top,
            bx + column_w,
            bottom_y,
            fill="#55E3B0",
            outline="",
            tags=("token_flow_meter_fill",),
        )
        head_colors = (
            "#203F39",
            "#245448",
            "#2A6555",
            "#32846B",
            "#40B38A",
            "#55E3B0",
        )
        for (band_top, band_bottom), color in zip(head_bands, head_colors):
            self.canvas.create_rectangle(
                bx,
                band_top,
                bx + column_w,
                band_bottom,
                fill=color,
                outline="",
                tags=("token_flow_meter_head",),
            )
        for index in range(segments - 1):
            gap_bottom = bottom_y - index * (segment_h + segment_gap) - segment_h
            self.canvas.create_rectangle(
                bx,
                gap_bottom - segment_gap,
                bx + column_w,
                gap_bottom,
                fill=Theme.ag_surface,
                outline="",
                tags=("token_flow_meter_separator",),
            )
        self._add_tooltip(
            x1,
            y1,
            x2,
            y2,
            f"实时 Token 流量\n最近 {TOKEN_FLOW_TRACE_TRAVEL_SECONDS:g} 秒 {exact_token_count(recent_tokens)} Token",
        )

    @staticmethod
    def _token_flow_meter_fill_top(
        level: float,
        top: float,
        bottom: float,
    ) -> float:
        level = max(0.0, min(1.0, float(level)))
        top = float(top)
        bottom = max(top, float(bottom))
        return bottom - (bottom - top) * level

    @staticmethod
    def _token_flow_meter_head_geometry(
        fill_top: float,
        bottom: float,
        *,
        cap_height: float = TOKEN_FLOW_METER_HEAD_HEIGHT,
        bands: int = TOKEN_FLOW_METER_HEAD_BANDS,
    ) -> tuple[float, list[tuple[float, float]]]:
        bottom = float(bottom)
        fill_top = max(0.0, min(bottom, float(fill_top)))
        bands = max(1, int(bands))
        visible_height = max(0.0, bottom - fill_top)
        actual_cap_height = min(max(0.0, float(cap_height)), visible_height)
        solid_top = fill_top + actual_cap_height
        band_height = actual_cap_height / bands
        band_bounds = [
            (
                fill_top + index * band_height,
                fill_top + (index + 1) * band_height,
            )
            for index in range(bands)
        ]
        return solid_top, band_bounds

    def _smooth_token_flow_meter_level(
        self,
        target_level: float,
        *,
        now: float | None = None,
    ) -> float:
        current_time = time.monotonic() if now is None else float(now)
        previous_time = float(
            getattr(self, "_token_flow_meter_last_tick", current_time - 1 / 60)
        )
        elapsed = max(1 / 240, min(0.12, current_time - previous_time))
        current = max(
            0.0,
            min(1.0, float(getattr(self, "_token_flow_meter_display_level", 0.0))),
        )
        target = max(0.0, min(1.0, float(target_level)))
        time_constant = 0.05 if target > current else 0.45
        blend = 1.0 - math.exp(-elapsed / time_constant)
        current += (target - current) * blend
        if abs(target - current) < 0.001:
            current = target
        self._token_flow_meter_display_level = current
        self._token_flow_meter_last_tick = current_time
        return current

    def _redraw_token_flow_meter(self) -> bool:
        if (
            self._main_tab != "stats"
            or self._token_flow_meter_rect is None
            or self._token_flow_meter_fill_bounds is None
        ):
            return False
        fill_items = self.canvas.find_withtag("token_flow_meter_fill")
        head_items = self.canvas.find_withtag("token_flow_meter_head")
        if len(fill_items) != 1 or len(head_items) != TOKEN_FLOW_METER_HEAD_BANDS:
            return False
        target_level, _recent_tokens = self._token_flow_snapshot()
        display_level = self._smooth_token_flow_meter_level(target_level)
        x1, top, x2, bottom = self._token_flow_meter_fill_bounds
        fill_top = self._token_flow_meter_fill_top(display_level, top, bottom)
        solid_top, head_bands = self._token_flow_meter_head_geometry(fill_top, bottom)
        self.canvas.coords(fill_items[0], x1, solid_top, x2, bottom)
        for item, (band_top, band_bottom) in zip(head_items, head_bands):
            self.canvas.coords(item, x1, band_top, x2, band_bottom)
        return True

    @staticmethod
    def _blend_hex_colors(start: str, end: str, amount: float) -> str:
        amount = max(0.0, min(1.0, float(amount)))
        start_rgb = tuple(int(start[index:index + 2], 16) for index in (1, 3, 5))
        end_rgb = tuple(int(end[index:index + 2], 16) for index in (1, 3, 5))
        blended = tuple(
            round(start_channel + (end_channel - start_channel) * amount)
            for start_channel, end_channel in zip(start_rgb, end_rgb)
        )
        return "#{:02X}{:02X}{:02X}".format(*blended)

    def _record_token_delta_badge(
        self,
        tokens: int,
        *,
        now: float | None = None,
    ) -> None:
        tokens = max(0, int(tokens or 0))
        if tokens <= 0:
            return
        current_time = time.monotonic() if now is None else float(now)
        last_event_at = float(getattr(self, "_token_delta_last_event_at", 0.0))
        if current_time - last_event_at <= TOKEN_DELTA_BADGE_MERGE_SECONDS:
            self._token_delta_value = int(getattr(self, "_token_delta_value", 0)) + tokens
        else:
            self._token_delta_value = tokens
        self._token_delta_started_at = current_time
        self._token_delta_last_event_at = current_time
        if hasattr(self, "root"):
            self._ensure_pulse_animation()

    def _token_delta_badge_visual(
        self,
        *,
        now: float | None = None,
    ) -> tuple[str, str, bool]:
        value = max(0, int(getattr(self, "_token_delta_value", 0)))
        started_at = float(getattr(self, "_token_delta_started_at", 0.0))
        current_time = time.monotonic() if now is None else float(now)
        elapsed = max(0.0, current_time - started_at)
        if value <= 0 or started_at <= 0 or elapsed >= TOKEN_DELTA_BADGE_DURATION_SECONDS:
            return "", Theme.ag_surface, False
        progress = elapsed / TOKEN_DELTA_BADGE_DURATION_SECONDS
        smooth_progress = progress * progress * (3.0 - 2.0 * progress)
        color = self._blend_hex_colors(Theme.live, Theme.ag_surface, smooth_progress)
        return f"+{exact_token_count(value)}", color, True

    def _redraw_token_delta_badge(self) -> bool:
        if self._main_tab != "stats":
            return False
        items = self.canvas.find_withtag("token_delta_badge")
        if len(items) != 1:
            return False
        text, color, visible = self._token_delta_badge_visual()
        self.canvas.itemconfigure(
            items[0],
            text=text,
            fill=color,
            state="normal" if visible else "hidden",
        )
        return True

    def _record_cost_delta_badge(
        self,
        cost: float,
        *,
        now: float | None = None,
    ) -> None:
        cost = max(0.0, float(cost or 0.0))
        if cost <= 0:
            return
        current_time = time.monotonic() if now is None else float(now)
        last_event_at = float(getattr(self, "_cost_delta_last_event_at", 0.0))
        if current_time - last_event_at <= TOKEN_DELTA_BADGE_MERGE_SECONDS:
            self._cost_delta_value = float(getattr(self, "_cost_delta_value", 0.0)) + cost
        else:
            self._cost_delta_value = cost
        self._cost_delta_started_at = current_time
        self._cost_delta_last_event_at = current_time
        if hasattr(self, "root"):
            self._ensure_pulse_animation()

    def _cost_delta_badge_visual(
        self,
        *,
        now: float | None = None,
    ) -> tuple[str, str, bool]:
        value = max(0.0, float(getattr(self, "_cost_delta_value", 0.0)))
        started_at = float(getattr(self, "_cost_delta_started_at", 0.0))
        current_time = time.monotonic() if now is None else float(now)
        elapsed = max(0.0, current_time - started_at)
        if value <= 0 or started_at <= 0 or elapsed >= TOKEN_DELTA_BADGE_DURATION_SECONDS:
            return "", Theme.ag_surface, False
        progress = elapsed / TOKEN_DELTA_BADGE_DURATION_SECONDS
        smooth_progress = progress * progress * (3.0 - 2.0 * progress)
        color = self._blend_hex_colors(Theme.warn, Theme.ag_surface, smooth_progress)
        decimals = 3 if value < 0.1 else 2
        return f"+${value:,.{decimals}f}", color, True

    def _redraw_cost_delta_badge(self) -> bool:
        if self._main_tab != "stats":
            return False
        items = self.canvas.find_withtag("cost_delta_badge")
        if len(items) != 1:
            return False
        text, color, visible = self._cost_delta_badge_visual()
        self.canvas.itemconfigure(
            items[0],
            text=text,
            fill=color,
            state="normal" if visible else "hidden",
        )
        return True

    def _token_flow_trace_pulses(
        self,
        width: int,
        height: int,
        *,
        travel_seconds: float = TOKEN_FLOW_TRACE_TRAVEL_SECONDS,
    ) -> list[tuple[float, int, float]]:
        width = max(1, int(width))
        height = max(5, int(height))
        travel_seconds = max(1.0, float(travel_seconds))
        now = time.monotonic()
        pulses: list[tuple[float, int, float]] = []
        samples = getattr(self, "_token_flow_samples", [])[-96:]
        travel_width = float(max(0, width - 1))
        pixels_per_second = travel_width / travel_seconds
        frame_position = now * pixels_per_second
        for created_at, raw_tokens in samples:
            age = now - created_at
            tokens = max(0, int(raw_tokens or 0))
            if tokens <= 0 or age < 0 or age > travel_seconds:
                continue
            event_level = min(
                1.0,
                math.log1p(tokens / 10_000.0) / math.log1p(200.0),
            )
            max_half_height = max(2, (height - 2) // 2)
            # Keep every event on the same sub-pixel phase. Tk snaps Canvas
            # lines to screen pixels; independent snapping makes their spacing
            # alternate by one pixel while a group moves across the trace.
            event_position = round(created_at * pixels_per_second)
            pulse_x = min(
                travel_width,
                max(0.0, frame_position - event_position),
            )
            half_height = max(
                2,
                int(max_half_height * (0.38 + 0.62 * event_level)),
            )
            pulses.append((pulse_x, half_height, event_level))
        return pulses

    @staticmethod
    def _token_flow_ecg_points(
        width: int,
        height: int,
        pulses: list[tuple[float, int, float]],
    ) -> list[tuple[float, float]]:
        width = max(1, int(width))
        height = max(5, int(height))
        center_y = height / 2.0
        # P-QRS-T profile. The R peak uses the full event-scaled height.
        profile = (
            (-1.00, 0.00),
            (-0.72, 0.00),
            (-0.56, -0.18),
            (-0.40, 0.00),
            (-0.22, 0.00),
            (-0.10, 0.36),
            (0.00, -1.00),
            (0.12, 0.56),
            (0.28, 0.00),
            (0.52, 0.00),
            (0.70, -0.24),
            (0.90, 0.00),
            (1.00, 0.00),
        )
        half_span = max(9.0, min(18.0, width / 14.0))
        profile_offsets = tuple(
            (float(round(profile_x * half_span)), profile_y)
            for profile_x, profile_y in profile
        )
        events: list[tuple[float, int]] = []
        positions = {0.0, float(width)}
        for pulse_x, half_height, _event_level in pulses:
            pulse_x = min(float(width), max(0.0, float(pulse_x)))
            events.append((pulse_x, max(2, int(half_height))))
            for profile_offset, _profile_y in profile_offsets:
                positions.add(
                    min(float(width), max(0.0, pulse_x + profile_offset))
                )

        points: list[tuple[float, float]] = []
        for point_x in sorted(positions):
            offset = 0.0
            for pulse_x, half_height in events:
                phase = (point_x - pulse_x) / half_span
                if phase < -1.0 or phase > 1.0:
                    continue
                wave = 0.0
                for index in range(len(profile) - 1):
                    left_x, left_y = profile[index]
                    right_x, right_y = profile[index + 1]
                    if phase > right_x:
                        continue
                    ratio = (phase - left_x) / max(0.001, right_x - left_x)
                    wave = left_y + (right_y - left_y) * ratio
                    break
                event_offset = wave * half_height
                if abs(event_offset) > abs(offset):
                    offset = event_offset
            points.append((point_x, center_y + offset))
        return points

    def _draw_token_flow_trace(
        self,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        *,
        register: bool = True,
    ) -> None:
        if register:
            self._token_flow_trace_rect = (x1, y1, x2, y2)
        width = max(1, x2 - x1)
        height = max(5, y2 - y1)
        level, recent_tokens = self._token_flow_snapshot()
        pulses = self._token_flow_trace_pulses(width, height)
        points = self._token_flow_ecg_points(width, height, pulses)
        coords = [
            coordinate
            for point_x, point_y in points
            for coordinate in (x1 + point_x, y1 + point_y)
        ]
        signal_color = Theme.live if pulses or level > 0.02 else "#284B49"
        self.canvas.create_line(
            *coords,
            fill=signal_color,
            width=1,
            capstyle="round",
            joinstyle="round",
            tags=("token_flow_trace", "token_flow_trace_signal"),
        )
        if register:
            self._add_tooltip(
                x1,
                y1,
                x2,
                y2,
                f"实时 Token 流量\n最近 {TOKEN_FLOW_TRACE_TRAVEL_SECONDS:g} 秒 {exact_token_count(recent_tokens)} Token",
            )

    def _redraw_token_flow_trace(self) -> bool:
        rect = self._token_flow_trace_rect
        if self._main_tab != "accounts" or rect is None:
            return False
        x1, y1, x2, y2 = rect
        width = max(1, x2 - x1)
        height = max(5, y2 - y1)
        level, _recent_tokens = self._token_flow_snapshot()
        pulses = self._token_flow_trace_pulses(width, height)
        points = self._token_flow_ecg_points(width, height, pulses)
        coords = [
            coordinate
            for point_x, point_y in points
            for coordinate in (x1 + point_x, y1 + point_y)
        ]
        signal_color = Theme.live if pulses or level > 0.02 else "#284B49"

        signal_items = self.canvas.find_withtag("token_flow_trace_signal")
        if not signal_items:
            self._draw_token_flow_trace(*rect, register=False)
            return True
        signal_item = signal_items[0]
        self.canvas.coords(signal_item, *coords)
        self.canvas.itemconfigure(signal_item, fill=signal_color, state="normal")
        return True

    def _draw_section_label(
        self,
        col_l: int,
        col_r: int,
        y: int,
        title: str,
        meta: str = "",
    ) -> int:
        self.canvas.create_text(
            col_l,
            y,
            anchor="nw",
            text=title,
            font=self._fonts["font_section"],
            fill=Theme.text_primary,
        )
        if meta:
            self.canvas.create_text(
                col_r,
                y + 2,
                anchor="ne",
                text=meta,
                font=self._fonts["font_tiny"],
                fill=Theme.text_muted,
            )
        return y + 24

    def _account_rank_row_height(self) -> int:
        if self._account_range in {"5h", "7d"}:
            return 51 if self.HEIGHT < 653 else 64
        return 39

    def _apply_window_size(self, width: int, height: int) -> None:
        width = int(max(self.MIN_WIDTH, width))
        height = int(max(self.MIN_HEIGHT, height))
        self.WIDTH = width
        self.HEIGHT = height
        x = self.root.winfo_x()
        y = self.root.winfo_y()
        self._ignore_configure = True
        try:
            self.root.geometry(f"{width}x{height}+{x}+{y}")
            self.canvas.configure(width=width, height=height)
        finally:
            self.root.after_idle(self._clear_ignore_configure)

    def _clear_ignore_configure(self) -> None:
        self._ignore_configure = False

    def _text_width(self, text: str, font_key: str) -> int:
        return self._fonts[font_key].measure(text)

    def _add_tooltip(self, x1: int, y1: int, x2: int, y2: int, text: str) -> None:
        if text:
            self._tooltip_rects.append((int(x1), int(y1), int(x2), int(y2), text))

    def _hit_tooltip(self, x: int, y: int) -> str:
        for x1, y1, x2, y2, text in reversed(self._tooltip_rects):
            if x1 <= x <= x2 and y1 <= y <= y2:
                return text
        return ""

    @staticmethod
    def _wrap_tooltip_lines(text: str, font: Any, max_text_width: int, max_lines: int = 4) -> list[str]:
        lines: list[str] = []
        truncated = False
        raw_lines = text.splitlines() or [text]
        for raw_index, raw_line in enumerate(raw_lines):
            remaining = raw_line or " "
            while remaining and len(lines) < max_lines:
                if font.measure(remaining) <= max_text_width:
                    lines.append(remaining)
                    remaining = ""
                    continue
                low, high = 1, len(remaining)
                while low < high:
                    middle = (low + high + 1) // 2
                    if font.measure(remaining[:middle]) <= max_text_width:
                        low = middle
                    else:
                        high = middle - 1
                split_at = max(1, low)
                lines.append(remaining[:split_at])
                remaining = remaining[split_at:]
            if remaining or (len(lines) >= max_lines and raw_index < len(raw_lines) - 1):
                truncated = True
                break
        if truncated:
            last_line = lines[-1]
            while last_line and font.measure(last_line + "...") > max_text_width:
                last_line = last_line[:-1]
            lines[-1] = last_line + "..."
        return lines

    def _draw_tooltip(self, W: int, H: int) -> None:
        if not self._tooltip_text:
            return
        font = self._fonts["font_micro"]
        max_text_width = max(80, W - 34)
        lines = self._wrap_tooltip_lines(self._tooltip_text, font, max_text_width)
        if not lines:
            return
        width = min(W - 16, max(font.measure(line) for line in lines) + 18)
        height = 18 * len(lines) + 8
        x = min(max(8, self._tooltip_pos[0] + 12), max(8, W - width - 8))
        y = min(max(8, self._tooltip_pos[1] + 14), max(8, H - height - 8))
        self._draw_rounded_rect(x, y, x + width, y + height, r=6,
                                fill=Theme.bg_lift, outline=Theme.data, width=1)
        for index, line in enumerate(lines):
            self.canvas.create_text(x + 9, y + 7 + index * 18, anchor="nw",
                                    text=line, font=self._fonts["font_micro"], fill=Theme.text_primary)

    def _ensure_topmost(self, force: bool = False, raise_window: bool = False) -> None:
        if not self._pinned and not force:
            return
        try:
            self.root.attributes("-topmost", True)
            if raise_window:
                self.root.deiconify()
                self.root.lift()
        except tk.TclError:
            pass

    def _schedule_topmost_repair(self) -> None:
        # Do not periodically lift/reassert topmost. Screenshot overlays are
        # often topmost windows too; repeated reassertion can jump above them.
        return

    def _truncate(self, text: str, font_key: str, max_w: int) -> str:
        f = self._fonts[font_key]
        if f.measure(text) <= max_w:
            return text
        while text and f.measure(text + "...") > max_w:
            text = text[:-1]
        return text + "..."

    def _latest_status(self) -> tuple[str, str, str, str]:
        if not self.state or not self.state.latest_request:
            return "-", "-", "-", Theme.text_muted
        req = self.state.latest_request
        kind = req.get("kind", "-")
        model = req.get("model", "-")
        created = req.get("created_at", "")
        status = "\u9519\u8bef" if kind == "error" else ("\u6210\u529f" if kind else "-")
        color = Theme.accent_red if kind == "error" else Theme.accent_green
        return status, model, relative_time(created) if created else "-", color

    def _draw_pill(self, x: int, y: int, text: str, color: str, max_w: int) -> None:
        label = self._truncate(text, "font_tiny", max_w - 14)
        width = min(max_w, self._text_width(label, "font_tiny") + 14)
        self._draw_rounded_rect(x, y, x + width, y + 21, r=6, fill=Theme.bg_dark, outline=Theme.border)
        self.canvas.create_text(x + 7, y + 4, anchor="nw", text=label, font=self._fonts["font_tiny"], fill=color)

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    #  DRAWING
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _health_color(self, label: str) -> str:
        if label in {"K12", "EDU"}:
            return Theme.data
        if label in {"PLUS", "TEAM"}:
            return Theme.violet
        if label in {"PRO", "BUSINESS", "ENTERPRISE"}:
            return Theme.warn
        if label in {"API KEY", "CLAUDE"}:
            return Theme.cyan
        if label == "\u8d26\u53f7\u6c60":
            return Theme.live
        if label in {"LOCAL", "\u672c\u5730"}:
            return Theme.accent_green
        if label in {"SUB", "SUB2"}:
            return Theme.cyan
        if label.upper().startswith("FAST"):
            return Theme.amber_bright
        if label in {"\u9650\u989d", "\u9650\u6d41", "\u51b7\u5374"}:
            return Theme.amber_bright
        if label in {"\u4e0d\u53ef\u7528", "\u505c\u7528", "\u9519\u8bef"}:
            return Theme.accent_red
        return Theme.text_muted

    def _draw_health_badge(self, x: int, y: int, label: str) -> int:
        if not label:
            return 0
        color = self._health_color(label)
        width = self._text_width(label, "font_micro") + 14
        self._draw_rounded_rect(x, y, x + width, y + 17, r=7, fill=Theme.bg_dark, outline=color)
        self.canvas.create_text(x + 7, y + 2, anchor="nw", text=label, font=self._fonts["font_micro"], fill=color)
        return width

    def _draw_footer(self, W: int, H: int) -> None:
        now_str = datetime.now(CN_TZ).strftime("%H:%M:%S UTC+8")
        self.canvas.create_text(W // 2, H - 10, anchor="s", text=now_str,
                                font=self._fonts["font_data"], fill=Theme.text_muted)
        self.canvas.create_line(W - 18, H - 7, W - 7, H - 18, fill=Theme.border, width=1)
        self.canvas.create_line(W - 13, H - 7, W - 7, H - 13, fill=Theme.text_muted, width=1)

    def _draw_list_scrollbar(
        self,
        tab: str,
        track_x: int,
        track_top: int,
        track_bottom: int,
        visible_items: int,
        total_items: int,
        max_scroll: int,
    ) -> None:
        if max_scroll <= 0 or visible_items <= 0 or total_items <= visible_items:
            return
        track_h = max(0, track_bottom - track_top)
        if track_h < 12:
            return
        thumb_h = max(12, int(track_h * visible_items / total_items))
        thumb_h = min(track_h, thumb_h)
        thumb_travel = max(0, track_h - thumb_h)
        offset = max(0, min(max_scroll, int(self._scroll_offsets.get(tab, 0) or 0)))
        thumb_top = track_top
        if thumb_travel > 0:
            thumb_top += int(thumb_travel * offset / max_scroll)
        thumb_bottom = thumb_top + thumb_h

        self.canvas.create_rectangle(
            track_x,
            track_top,
            track_x + 2,
            track_bottom,
            fill=Theme.border,
            outline="",
        )
        self.canvas.create_rectangle(
            track_x,
            thumb_top,
            track_x + 2,
            thumb_bottom,
            fill=Theme.amber,
            outline="",
        )

        hit_pad = 5
        self._list_scrollbar_tracks[tab] = (
            track_x - hit_pad,
            track_top,
            track_x + 2 + hit_pad,
            track_bottom,
        )
        self._list_scrollbar_thumbs[tab] = (
            track_x - hit_pad,
            thumb_top,
            track_x + 2 + hit_pad,
            thumb_bottom,
        )

    def _draw_main_tabs(self, col_l: int, col_r: int, y: int) -> int:
        tabs = [
            ("main_accounts", "\u8d26\u53f7", "accounts"),
            ("main_stats", "\u7528\u91cf\u7edf\u8ba1", "stats"),
        ]
        gap = 2
        tab_h = 29
        inset = 3
        self._draw_rounded_rect(col_l, y, col_r, y + tab_h, r=8,
                                fill=Theme.ag_bg, outline=Theme.border)
        inner_l = col_l + inset
        inner_r = col_r - inset
        total_gap = gap * (len(tabs) - 1)
        tab_w = max(62, (inner_r - inner_l - total_gap) // len(tabs))
        for index, (button_name, label, value) in enumerate(tabs):
            x1 = inner_l + index * (tab_w + gap)
            x2 = inner_r if index == len(tabs) - 1 else x1 + tab_w
            self._btn_rects[button_name] = (x1, y + 2, x2, y + tab_h - 2)
            selected = self._main_tab == value
            hovered = self._hover_btn == button_name
            fill = Theme.bg_lift if selected else (Theme.ag_surface_hover if hovered else Theme.ag_bg)
            text_color = Theme.text_primary if selected or hovered else Theme.ag_muted
            self._draw_rounded_rect(x1, y + 3, x2, y + tab_h - 3, r=6, fill=fill, outline="")
            if selected:
                self.canvas.create_line(
                    x1 + 18,
                    y + tab_h - 3,
                    x2 - 18,
                    y + tab_h - 3,
                    fill=Theme.data,
                    width=2,
                )
            self.canvas.create_text((x1 + x2) // 2, y + 14, anchor="center", text=label,
                                    font=self._fonts["font_label_bold"], fill=text_color)
        return y + tab_h + 10

    def _draw_ag_section(self, col_l: int, col_r: int, y: int, title: str, badge: str = "") -> int:
        self.canvas.create_text(col_l, y, anchor="nw", text=title,
                                font=self._fonts["font_section"], fill=Theme.text_primary)
        if badge:
            bw = self._text_width(badge, "font_micro") + 14
            self._draw_rounded_rect(col_r - bw, y - 1, col_r, y + 18, r=6,
                                    fill=Theme.ag_bg, outline=Theme.ag_border)
            self.canvas.create_text(col_r - bw // 2, y + 8, anchor="center", text=badge,
                                    font=self._fonts["font_tiny"], fill=Theme.ag_muted)
        return y + 24

    def _draw_donut(self, x: int, y: int, size: int, pct: float, color: str, label: str) -> None:
        pct = max(0.0, min(100.0, float(pct or 0)))
        pad = 5
        self.canvas.create_oval(x + pad, y + pad, x + size - pad, y + size - pad,
                                outline=Theme.ag_border, width=5)
        if pct > 0:
            self.canvas.create_arc(
                x + pad,
                y + pad,
                x + size - pad,
                y + size - pad,
                start=90,
                extent=-360 * pct / 100,
                style="arc",
                outline=color,
                width=5,
            )
        self.canvas.create_text(x + size // 2, y + size // 2, anchor="center",
                                text=label, font=self._fonts["font_label_bold"], fill=color)

    def _draw_ag_chip(self, x: int, y: int, text: str, dot: str | None = None) -> int:
        width = self._text_width(text, "font_micro") + (24 if dot else 14)
        self._draw_rounded_rect(x, y, x + width, y + 20, r=8, fill=Theme.ag_surface, outline=Theme.ag_border)
        tx = x + 7
        if dot:
            self.canvas.create_oval(x + 7, y + 7, x + 13, y + 13, fill=dot, outline="")
            tx += 12
        self.canvas.create_text(tx, y + 4, anchor="nw", text=text,
                                font=self._fonts["font_micro"], fill=Theme.text_secondary)
        return width

    @staticmethod
    def _ag_quota_color(utilization: float | int | None) -> str:
        try:
            value = float(utilization or 0)
        except (TypeError, ValueError):
            value = 0.0
        if value >= 90:
            return Theme.ag_crit
        if value >= 60:
            return Theme.ag_warn
        return Theme.ag_success

    @staticmethod
    def _activity_color(intensity: float) -> str:
        if intensity <= 0:
            return "#252C31"
        if intensity < 0.18:
            return "#284B49"
        if intensity < 0.38:
            return "#347A6A"
        if intensity < 0.68:
            return "#4FB895"
        if intensity < 0.9:
            return "#68DAB2"
        return "#8AE9C8"

    def _trend_token_color(self, intensity: float, is_today: bool = False) -> str:
        if is_today:
            return Theme.ag_accent
        return self._activity_color(intensity)

    def _client_providers(self) -> list[dict[str, Any]]:
        if not self.state or not isinstance(self.state.client_usage, dict):
            return []
        providers = self.state.client_usage.get("providers")
        if not isinstance(providers, list):
            return []
        return [provider for provider in providers if isinstance(provider, dict)]

    def _token_mix(self) -> dict[str, int]:
        return token_mix_from_client_usage(self.state.client_usage if self.state else None)

    def _summary_token_mix(self, summary: dict[str, Any]) -> dict[str, int]:
        mix = {
            "input": int(summary.get("input_tokens") or 0),
            "cached": int(summary.get("cached_input_tokens") or 0),
            "cache_create": int(summary.get("cache_creation_input_tokens") or 0),
            "output": int(summary.get("output_tokens") or 0),
        }
        known = sum(mix.values())
        breakdown_tokens = summary.get("breakdown_tokens")
        if breakdown_tokens is None:
            breakdown_tokens = summary.get("tokens")
        mix["unknown"] = max(0, int(breakdown_tokens or 0) - known)
        return mix

    def _usage_range_providers(self, range_key: str) -> list[dict[str, Any]]:
        if range_key == "24h":
            details = detailed_usage_from_state(self.state) if self.state is not None else {}
            providers = details.get("providers") if isinstance(details, dict) else []
            return [provider for provider in providers if isinstance(provider, dict)]

        history = load_usage_history()
        days = history.get("days") if isinstance(history, dict) else {}
        if not isinstance(days, dict):
            return []
        if range_key in {"7d", "30d"}:
            count = 7 if range_key == "7d" else 30
            selected_keys = {date_key(offset) for offset in range(count)}
            selected_days = [row for key, row in days.items() if key in selected_keys]
        else:
            selected_days = list(days.values())
        aggregated: dict[str, dict[str, Any]] = {}
        history_requests = 0
        history_tokens = 0
        history_cost = 0.0
        for day in selected_days:
            day_requests = int(day.get("requests") or 0) if isinstance(day, dict) else 0
            day_tokens = int(day.get("tokens") or 0) if isinstance(day, dict) else 0
            day_cost = float(day.get("cost") or 0) if isinstance(day, dict) else 0.0
            history_requests += day_requests
            history_tokens += day_tokens
            history_cost += day_cost
            providers = day.get("providers") if isinstance(day, dict) else None
            if not isinstance(providers, list):
                continue
            valid_providers = [provider for provider in providers if isinstance(provider, dict)]
            detail_tokens = sum(max(0, int(provider.get("tokens") or 0)) for provider in valid_providers)
            detail_requests = sum(max(0, int(provider.get("requests") or 0)) for provider in valid_providers)
            detail_cost = sum(max(0.0, float(provider.get("cost") or 0)) for provider in valid_providers)
            token_scale = min(1.0, day_tokens / detail_tokens) if detail_tokens > 0 else 1.0
            request_scale = min(1.0, day_requests / detail_requests) if detail_requests > 0 else 1.0
            cost_scale = min(1.0, day_cost / detail_cost) if detail_cost > 0 else 1.0
            for provider in valid_providers:
                name = str(provider.get("name") or "Local client")
                row = aggregated.setdefault(
                    name,
                    {"name": name, "requests": 0, "tokens": 0, "cost": 0.0, "models": {}},
                )
                row["requests"] += int(max(0, int(provider.get("requests") or 0)) * request_scale)
                row["tokens"] += int(max(0, int(provider.get("tokens") or 0)) * token_scale)
                row["cost"] += max(0.0, float(provider.get("cost") or 0)) * cost_scale
                models = provider.get("models")
                if isinstance(models, dict):
                    for model, amount in models.items():
                        model_name = str(model or "unknown")
                        row["models"][model_name] = row["models"].get(model_name, 0) + int(
                            max(0, int(amount or 0)) * token_scale
                        )
        tracked_requests = sum(int(row.get("requests") or 0) for row in aggregated.values())
        tracked_tokens = sum(int(row.get("tokens") or 0) for row in aggregated.values())
        tracked_cost = sum(float(row.get("cost") or 0) for row in aggregated.values())
        missing_tokens = max(0, history_tokens - tracked_tokens)
        if missing_tokens > 0:
            aggregated["Historical detail gap"] = {
                "name": "Historical detail gap",
                "requests": max(0, history_requests - tracked_requests),
                "tokens": missing_tokens,
                "cost": round(max(0.0, history_cost - tracked_cost), 6),
                "models": {},
            }
        return list(aggregated.values())

    def _filter_account_display_rows(
        self,
        rows: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        recent_by_account: dict[str, dict[str, int]] = {}
        for provider in self._usage_range_providers("30d"):
            key = account_display_key(provider.get("name"))
            if not key:
                continue
            recent = recent_by_account.setdefault(key, {"requests": 0, "tokens": 0})
            recent["requests"] += max(0, int(provider.get("requests") or 0))
            recent["tokens"] += max(0, int(provider.get("tokens") or 0))

        current_by_account: dict[str, dict[str, Any]] = {}
        for account in (self.state.top_accounts or []) if self.state else []:
            if not isinstance(account, dict):
                continue
            key = account_display_key(account.get("name"))
            if not key:
                continue
            previous = current_by_account.get(key)
            if previous is None or account_has_weekly_quota(account):
                current_by_account[key] = account

        visible: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            key = account_display_key(row.get("name"))
            if account_should_remain_visible(
                row,
                recent_by_account.get(key),
                current_by_account.get(key),
            ):
                visible.append(row)
        return visible

    def _history_account_rows(self, range_key: str) -> list[dict[str, Any]]:
        current_rows = list(self.state.top_accounts or []) if self.state else []
        current_by_name = {
            str(row.get("name") or ""): row
            for row in current_rows
            if isinstance(row, dict) and row.get("name")
        }
        rows: list[dict[str, Any]] = []
        for provider in self._usage_range_providers(range_key):
            raw_name = str(provider.get("name") or "Local client")
            is_gap = raw_name == "Historical detail gap"
            row = dict(current_by_name.get(raw_name) or {})
            row.update(
                {
                    "name": "历史明细缺口" if is_gap else raw_name,
                    "requests": int(provider.get("requests") or 0),
                    "tokens": int(provider.get("tokens") or 0),
                    "cost": float(provider.get("cost") or 0),
                    "models": dict(provider.get("models") or {}),
                    "plan_type": str(provider.get("plan_type") or ""),
                    "source_badge": "" if is_gap else str(row.get("source_badge") or "LOCAL"),
                    "health_badge": "",
                    "is_history_detail_gap": is_gap,
                }
            )
            rows.append(row)
        return rows

    def _history_7d_fallback_rows(
        self,
        existing_rows: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        existing_keys = {
            account_display_key(row.get("name"))
            for row in existing_rows
            if isinstance(row, dict)
        }
        fallback: list[dict[str, Any]] = []
        for row in self._filter_account_display_rows(self._history_account_rows("7d")):
            key = account_display_key(row.get("name"))
            if not key or "@" not in key or key in existing_keys:
                continue
            if (
                int(row.get("requests") or 0) <= 0
                and int(row.get("tokens") or 0) <= 0
                and float(row.get("cost") or 0) <= 0
            ):
                continue
            item = dict(row)
            item.update(
                {
                    "quota_available": False,
                    "quota_unlimited": False,
                    "quota_stale": False,
                    "quota_reset_unavailable": False,
                    "quota_idle": False,
                    "historical_fallback": True,
                }
            )
            fallback.append(item)
        return fallback

    def _needs_server_account_30d(self) -> bool:
        return any(
            isinstance(row, dict) and str(row.get("source_badge") or "") == "SUB"
            for row in (self.state.top_accounts or [])
        ) if self.state else False

    def _top_models(self, range_key: str) -> list[tuple[str, int]]:
        totals: dict[str, int] = {}
        for provider in self._usage_range_providers(range_key):
            models = provider.get("models")
            models = models if isinstance(models, dict) else {}
            tracked = 0
            for model, tokens in models.items():
                try:
                    amount = int(tokens or 0)
                except (TypeError, ValueError):
                    amount = 0
                if amount > 0:
                    name = str(model or "unknown")
                    totals[name] = totals.get(name, 0) + amount
                    tracked += amount
            untracked = max(0, int(provider.get("tokens") or 0) - tracked)
            if untracked > 0:
                totals["Untracked"] = totals.get("Untracked", 0) + untracked
        return sorted(totals.items(), key=lambda item: item[1], reverse=True)[:6]

    @staticmethod
    def _top_model_visible_count(
        model_count: int,
        provider_count: int,
        available_height: int,
    ) -> int:
        model_count = max(0, int(model_count))
        if model_count <= 0:
            return 0
        section_headers_height = 48
        provider_reserve = 48 if int(provider_count) > 0 else 0
        model_row_height = 28
        capacity = max(
            0,
            (int(available_height) - section_headers_height - provider_reserve)
            // model_row_height,
        )
        return max(1, min(5, model_count, capacity))

    def _live_usage_summary_delta(self, authoritative_tokens: int) -> dict[str, int]:
        empty = {
            "tokens": 0,
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "output_tokens": 0,
        }
        overlay = self._live_usage_overlay
        if not isinstance(overlay, dict) or self.state is None:
            return empty

        overlay_tokens = max(0, int(overlay.get("tokens") or 0))
        if overlay_tokens <= 0:
            return empty

        base_today_tokens = max(0, int(overlay.get("base_today_tokens") or 0))
        base_authoritative_tokens = max(
            0,
            int(overlay.get("base_authoritative_tokens", base_today_tokens) or 0),
        )
        baseline_gap = max(0, base_today_tokens - base_authoritative_tokens)
        current_gap = max(0, int(self.state.today_tokens or 0) - authoritative_tokens)
        uncovered_tokens = min(overlay_tokens, max(0, current_gap - baseline_gap))
        if uncovered_tokens <= 0:
            return empty

        component_keys = (
            "input_tokens",
            "cached_input_tokens",
            "output_tokens",
        )
        components = [max(0, int(overlay.get(key) or 0)) for key in component_keys]
        if uncovered_tokens < overlay_tokens:
            weighted = [value * uncovered_tokens / overlay_tokens for value in components]
            scaled = [int(value) for value in weighted]
            target_known = min(
                uncovered_tokens,
                int(round(sum(components) * uncovered_tokens / overlay_tokens)),
            )
            remainder = max(0, target_known - sum(scaled))
            fractions = sorted(
                range(len(weighted)),
                key=lambda index: weighted[index] - scaled[index],
                reverse=True,
            )
            for index in fractions[:remainder]:
                scaled[index] += 1
            components = scaled

        result = dict(empty)
        result["tokens"] = uncovered_tokens
        for key, value in zip(component_keys, components):
            result[key] = value
        return result

    def _usage_range_summary(self, range_key: str) -> dict[str, Any]:
        if range_key == "24h":
            hourly: list[dict[str, Any]] = []
            if self.state and isinstance(self.state.client_usage, dict):
                dashboard = self.state.client_usage.get("dashboard")
                if isinstance(dashboard, dict):
                    raw_hourly = dashboard.get("hourly_today")
                    if isinstance(raw_hourly, list):
                        hourly = [row for row in raw_hourly if isinstance(row, dict)]
            if not hourly:
                hourly = [
                    {
                        "hour": hour,
                        "requests": 0,
                        "tokens": 0,
                        "cost": 0.0,
                    }
                    for hour in range(24)
                ]
            mix = self._token_mix()
            authoritative_tokens = 0
            if self.state and isinstance(self.state.client_usage, dict):
                authoritative_tokens = int(self.state.client_usage.get("tokens") or 0)
            live_delta = self._live_usage_summary_delta(authoritative_tokens)
            live_tokens = int(self.state.today_tokens if self.state else 0)
            return {
                "label": "今日",
                "requests": int(self.state.today_requests if self.state else 0),
                "tokens": live_tokens,
                "breakdown_tokens": live_tokens,
                "input_tokens": mix["input"] + live_delta["input_tokens"],
                "cached_input_tokens": mix["cached"] + live_delta["cached_input_tokens"],
                "cache_creation_input_tokens": mix["cache_create"],
                "output_tokens": mix["output"] + live_delta["output_tokens"],
                "cost": float(self.state.today_account_cost if self.state else 0),
                "series": hourly,
            }
        history = load_usage_history()
        days = history.get("days") if isinstance(history, dict) else {}
        if not isinstance(days, dict):
            days = {}
        series: list[dict[str, Any]] = []
        if range_key == "all":
            parsed_dates = []
            for key, row in days.items():
                if not isinstance(row, dict):
                    continue
                try:
                    parsed_dates.append(datetime.fromisoformat(str(key)).date())
                except ValueError:
                    continue
            if parsed_dates:
                start_date = min(parsed_dates)
                end_date = datetime.now(CN_TZ).date()
                day_count = max(1, (end_date - start_date).days + 1)
                keys = [(start_date + timedelta(days=offset)).isoformat() for offset in range(day_count)]
            else:
                keys = []
        else:
            days_count = 7 if range_key == "7d" else 30
            keys = [date_key(offset) for offset in range(days_count - 1, -1, -1)]
        for key in keys:
            row = days.get(key) if isinstance(days.get(key), dict) else {}
            series.append(
                {
                    "date": key,
                    "requests": int(row.get("requests") or 0),
                    "tokens": int(row.get("tokens") or 0),
                    "input_tokens": int(row.get("input_tokens") or 0),
                    "cached_input_tokens": int(row.get("cached_input_tokens") or 0),
                    "cache_creation_input_tokens": int(row.get("cache_creation_input_tokens") or 0),
                    "output_tokens": int(row.get("output_tokens") or 0),
                    "cost": float(row.get("cost") or 0),
                }
            )
        return {
            "label": range_key,
            "requests": sum(int(item.get("requests") or 0) for item in series),
            "tokens": sum(int(item.get("tokens") or 0) for item in series),
            "input_tokens": sum(int(item.get("input_tokens") or 0) for item in series),
            "cached_input_tokens": sum(int(item.get("cached_input_tokens") or 0) for item in series),
            "cache_creation_input_tokens": sum(int(item.get("cache_creation_input_tokens") or 0) for item in series),
            "output_tokens": sum(int(item.get("output_tokens") or 0) for item in series),
            "cost": sum(float(item.get("cost") or 0) for item in series),
            "series": series,
        }

    def _budget_rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for account in list(self.state.top_accounts or []) if self.state else []:
            windows = []
            account_tokens = int(account.get("tokens") or 0)
            account_requests = int(account.get("requests") or 0)
            pressure_active = bool(account.get("active_now") or account.get("is_latest") or account_tokens > 0 or account_requests > 0)
            for key, label in (("window_5h", "5h"), ("window_7d", "7d"), ("window_cycle", "\u5468\u671f")):
                window = account.get(key)
                if not isinstance(window, dict) or not window:
                    continue
                quota_available = bool(window.get("quota_available", window.get("utilization") is not None))
                quota_unlimited = bool(window.get("quota_unlimited"))
                try:
                    utilization = float(window.get("utilization") or 0)
                except (TypeError, ValueError):
                    utilization = 0.0
                remaining = window.get("remaining_percent")
                if remaining is None and quota_available:
                    remaining = max(0.0, min(100.0, 100.0 - utilization))
                windows.append(
                    {
                        "label": label,
                        "quota_available": quota_available,
                        "quota_unlimited": quota_unlimited,
                        "quota_stale": bool(window.get("quota_stale")),
                        "quota_reset_unavailable": bool(window.get("quota_reset_unavailable")),
                        "quota_idle": bool(window.get("quota_idle")) if key == "window_5h" else False,
                        "utilization": utilization,
                        "remaining": remaining,
                        "resets_at": str(window.get("resets_at") or ""),
                        "tokens": int(window.get("tokens") or 0),
                        "cost": float(window.get("cost") or 0),
                        "pressure_active": (
                            pressure_active
                            and not bool(window.get("quota_idle"))
                            and not quota_unlimited
                        ),
                    }
                )
            if not windows:
                continue
            quota_windows = [item for item in windows if item["quota_available"]]
            min_remaining = min(
                [float(item["remaining"]) for item in quota_windows if item["remaining"] is not None],
                default=999.0,
            )
            rows.append(
                {
                    "name": str(account.get("name") or "-"),
                    "source_badge": str(account.get("source_badge") or ""),
                    "health_badge": str(account.get("health_badge") or ""),
                    "windows": windows,
                    "has_quota": bool(quota_windows),
                    "min_remaining": min_remaining,
                    "pressure_active": pressure_active,
                    "tokens": account_tokens,
                    "requests": account_requests,
                }
            )
        rows.sort(
            key=lambda row: (
                0 if row.get("pressure_active") else 1,
                0 if row["has_quota"] else 1,
                row["min_remaining"],
                row["name"],
            )
        )
        return rows

    def _draw_activity_heatmap(self, col_l: int, col_r: int, y: int, summary: dict[str, Any], series: list[dict[str, Any]]) -> int:
        c = self.canvas
        c.create_text(col_l, y, anchor="nw", text="\u6d3b\u8dc3\u5206\u5e03",
                      font=self._fonts["font_section"], fill=Theme.text_primary)
        heatmap_label = (
            "ALL  \u00b7  \u8fd1\u671f"
            if self._usage_range == "all"
            else str(summary.get("label", "-")).upper()
        )
        badge = f"{heatmap_label}  \u00b7  TOKEN"
        bw = self._text_width(badge, "font_micro") + 14
        self._draw_rounded_rect(col_r - bw, y - 1, col_r, y + 18, r=6,
                                fill=Theme.ag_bg, outline=Theme.ag_border)
        c.create_text(col_r - bw // 2, y + 8, anchor="center", text=badge,
                      font=self._fonts["font_tiny"], fill=Theme.ag_muted)
        y += 28
        if not series:
            c.create_text(col_l + 4, y, anchor="nw", text="\u6682\u65e0\u8d8b\u52bf\u6570\u636e",
                          font=self._fonts["font_label"], fill=Theme.ag_muted)
            return y + 30

        compact_all = self._usage_range == "all" and self.HEIGHT < 726
        if self._usage_range == "24h":
            visible = series[-24:]
        elif self._usage_range == "7d":
            visible = series[-7:]
        elif self._usage_range == "30d" or compact_all:
            visible = series[-30:]
        else:
            visible = series
            available_w = max(1, col_r - col_l - 24)
            max_cols = max(1, (available_w + 4) // 9)
            max_days = max(7, (max_cols - 2) * 7)
            if len(visible) > max_days:
                visible = visible[-max_days:]
        max_tokens = max([float(item.get("tokens") or 0) for item in visible], default=0.0) or 1.0
        peak = max(visible, key=lambda item: float(item.get("tokens") or 0))
        if self._usage_range == "24h":
            cols = 24
            rows_count = 1
            cell_gap = 3
            cell = max(7, min(13, int((col_r - col_l - cell_gap * (cols - 1)) / cols)))
            for label, col_index in (("00", 0), ("06", 6), ("12", 12), ("18", 18)):
                c.create_text(col_l + col_index * (cell + cell_gap), y, anchor="nw", text=label,
                              font=self._fonts["font_micro"], fill=Theme.ag_muted)
            grid_x = col_l
            grid_y = y + 15
            for index, item in enumerate(visible):
                row = index // cols
                col = index % cols
                tokens = float(item.get("tokens") or 0)
                intensity = min(1.0, tokens / max_tokens) if tokens > 0 else 0.0
                failed = bool(item.get("failure"))
                x1 = grid_x + col * (cell + cell_gap)
                y1 = grid_y + row * (cell + cell_gap)
                fill = Theme.ag_crit if failed else self._activity_color(intensity)
                outline = Theme.ag_crit if failed else Theme.ag_border
                self._draw_rounded_rect(x1, y1, x1 + cell, y1 + cell, r=3,
                                        fill=fill, outline=outline)
                hour = int(item.get("hour") if item.get("hour") is not None else index)
                failure_note = ""
                if failed:
                    failure_count = max(1, int(item.get("failure_count") or 1))
                    failure_at = str(item.get("failure_at") or "")
                    failure_time = failure_at[11:16] if len(failure_at) >= 16 else ""
                    if item.get("failure_kind") == "desktop_network":
                        if failure_count == 1:
                            failure_note = "\nCodex network outage detected"
                        else:
                            failure_note = f"\n{failure_count} Codex network outages detected"
                    elif failure_count == 1:
                        failure_note = "\nCodex task error detected"
                    else:
                        failure_note = f"\n{failure_count} Codex task errors detected"
                    if failure_time:
                        failure_note += f" at {failure_time}"
                self._add_tooltip(
                    x1, y1, x1 + cell, y1 + cell,
                    f"{hour:02d}:00-{(hour + 1) % 24:02d}:00\n{exact_token_count(tokens)} Token\n{compact_number(item.get('requests', 0))} calls \u00b7 {money(item.get('cost', 0))}{failure_note}",
                )
            legend_y = grid_y + rows_count * (cell + cell_gap) + 8
        elif self._usage_range == "7d":
            cols = 7
            cell_gap = 5
            cell_w = max(28, int((col_r - col_l - cell_gap * (cols - 1)) / cols))
            cell_h = 22
            grid_x = col_l
            grid_y = y + 15
            for index, item in enumerate(visible):
                day_text = str(item.get("date") or "")[-2:] or "-"
                x1 = grid_x + index * (cell_w + cell_gap)
                c.create_text(x1 + cell_w // 2, y, anchor="n", text=day_text,
                              font=self._fonts["font_micro"], fill=Theme.ag_muted)
                tokens = float(item.get("tokens") or 0)
                intensity = min(1.0, tokens / max_tokens) if tokens > 0 else 0.0
                self._draw_rounded_rect(x1, grid_y, x1 + cell_w, grid_y + cell_h, r=5,
                                        fill=self._activity_color(intensity), outline=Theme.ag_border)
                if tokens > 0:
                    shine_w = max(3, int(cell_w * min(1.0, intensity) * 0.18))
                    self._draw_rounded_rect(x1 + 3, grid_y + 3, x1 + 3 + shine_w, grid_y + 6,
                                            r=2, fill=Theme.amber_glow, outline="")
                self._add_tooltip(
                    x1,
                    grid_y,
                    x1 + cell_w,
                    grid_y + cell_h,
                    f"{item.get('date', '-')}\n{exact_token_count(tokens)} Token\n{compact_number(item.get('requests', 0))} calls \u00b7 {money(item.get('cost', 0))}",
                )
            legend_y = grid_y + cell_h + 9
        elif self._usage_range == "30d" or compact_all:
            cols = 30
            rows_count = 1
            cell_gap = 3
            cell = max(7, min(12, int((col_r - col_l - cell_gap * (cols - 1)) / cols)))
            grid_w = cols * cell + (cols - 1) * cell_gap
            grid_x = col_l + max(0, (col_r - col_l - grid_w) // 2)
            grid_y = y + 8
            padded = visible[-30:]
            while len(padded) < 30:
                padded.insert(0, {"date": "-", "requests": 0, "tokens": 0, "cost": 0.0})
            for index, item in enumerate(padded):
                row = 0
                col = index
                tokens = float(item.get("tokens") or 0)
                intensity = min(1.0, tokens / max_tokens) if tokens > 0 else 0.0
                x1 = grid_x + col * (cell + cell_gap)
                y1 = grid_y + row * (cell + cell_gap)
                self._draw_rounded_rect(x1, y1, x1 + cell, y1 + cell, r=4,
                                        fill=self._activity_color(intensity), outline=Theme.ag_border)
                self._add_tooltip(
                    x1,
                    y1,
                    x1 + cell,
                    y1 + cell,
                    f"{item.get('date', '-')}\n{exact_token_count(tokens)} Token\n{compact_number(item.get('requests', 0))} calls \u00b7 {money(item.get('cost', 0))}",
                )
            axis_y = grid_y + cell + 3
            c.create_text(grid_x, axis_y, anchor="nw", text="30d ago",
                          font=self._fonts["font_micro"], fill=Theme.ag_muted)
            c.create_text(grid_x + grid_w, axis_y, anchor="ne", text="today",
                          font=self._fonts["font_micro"], fill=Theme.ag_muted)
            legend_y = axis_y + 14
        else:
            label_w = 24
            cell_gap = 4
            rows_count = 7
            dates = []
            for item in visible:
                try:
                    dates.append(datetime.fromisoformat(str(item.get("date") or "")).date())
                except ValueError:
                    dates.append(None)
            first_date = next((value for value in dates if value is not None), None)
            last_date = next((value for value in reversed(dates) if value is not None), None)
            if first_date and last_date:
                grid_start = first_date - timedelta(days=first_date.weekday())
                grid_end = last_date + timedelta(days=6 - last_date.weekday())
            else:
                grid_start = datetime.now(CN_TZ).date()
                grid_end = grid_start
            span_days = (grid_end - grid_start).days
            cols = max(1, math.ceil((span_days + 1) / 7))
            min_cell = 5 if self._usage_range == "all" else 9
            max_cell = 10 if self._usage_range == "all" else 14
            cell = max(min_cell, min(max_cell, int((col_r - col_l - label_w - cell_gap * max(0, cols - 1)) / max(1, cols))))
            grid_x = col_l + label_w
            month_seen: set[tuple[int, int]] = set()
            data_by_date = {
                item_date: item
                for item, item_date in zip(visible, dates)
                if item_date is not None
            }
            for offset in range(span_days + 1):
                item_date = grid_start + timedelta(days=offset)
                col = offset // 7
                month_key = (item_date.year, item_date.month)
                if month_key in month_seen:
                    continue
                month_seen.add(month_key)
                c.create_text(grid_x + col * (cell + cell_gap), y, anchor="nw",
                              text=item_date.strftime("%b"), font=self._fonts["font_micro"], fill=Theme.ag_muted)
            grid_y = y + 15
            for label, row in (("Mon", 0), ("Wed", 2), ("Fri", 4)):
                c.create_text(col_l, grid_y + row * (cell + cell_gap), anchor="nw",
                              text=label, font=self._fonts["font_micro"], fill=Theme.ag_muted)
            for offset in range(span_days + 1):
                item_date = grid_start + timedelta(days=offset)
                item = data_by_date.get(item_date, {"date": item_date.isoformat(), "requests": 0, "tokens": 0, "cost": 0.0})
                col = offset // rows_count
                row = item_date.weekday()
                tokens = float(item.get("tokens") or 0)
                intensity = min(1.0, tokens / max_tokens) if tokens > 0 else 0.0
                x1 = grid_x + col * (cell + cell_gap)
                y1 = grid_y + row * (cell + cell_gap)
                self._draw_rounded_rect(x1, y1, x1 + cell, y1 + cell, r=3,
                                        fill=self._activity_color(intensity), outline=Theme.ag_border)
                self._add_tooltip(
                    x1, y1, x1 + cell, y1 + cell,
                    f"{item.get('date', '-')}\n{exact_token_count(tokens)} Token\n{compact_number(item.get('requests', 0))} calls \u00b7 {money(item.get('cost', 0))}",
                )
            legend_y = grid_y + rows_count * (cell + cell_gap) + 8

        c.create_text(col_l, legend_y + 1, anchor="nw", text="\u4f4e",
                      font=self._fonts["font_micro"], fill=Theme.ag_muted)
        legend_x = col_l + 18
        for idx, color in enumerate(["#252C31", "#284B49", "#347A6A", "#4FB895", "#8AE9C8"]):
            self._draw_rounded_rect(legend_x + idx * 14, legend_y, legend_x + idx * 14 + 10, legend_y + 10,
                                    r=2, fill=color, outline=Theme.ag_border)
        c.create_text(legend_x + 74, legend_y + 1, anchor="nw", text="\u9ad8",
                      font=self._fonts["font_micro"], fill=Theme.ag_muted)
        if self._usage_range == "24h" and any(item.get("failure") for item in visible):
            error_x = legend_x + 112
            self._draw_rounded_rect(error_x, legend_y, error_x + 10, legend_y + 10,
                                    r=2, fill=Theme.ag_crit, outline=Theme.ag_crit)
            c.create_text(error_x + 14, legend_y + 1, anchor="nw", text="\u9519\u8bef",
                          font=self._fonts["font_micro"], fill=Theme.ag_muted)
        peak_tokens = int(float(peak.get("tokens") or 0))
        peak_label = f"{int(peak.get('hour')):02d}:00" if self._usage_range == "24h" and peak.get("hour") is not None else str(peak.get("date") or "-")
        peak_text = f"\u5cf0\u503c {peak_label}  {compact_number(peak_tokens)}"
        c.create_text(col_r, legend_y + 1, anchor="ne",
                      text=peak_text,
                      font=self._fonts["font_micro"], fill=Theme.ag_muted)
        peak_width = self._text_width(peak_text, "font_micro")
        self._add_tooltip(
            col_r - peak_width,
            legend_y - 2,
            col_r,
            legend_y + 14,
            f"\u5cf0\u503c {peak_label}\n{exact_token_count(peak_tokens)} Token",
        )
        return legend_y + 23

    def _draw_token_budget_page(self, col_l: int, col_r: int, y: int, H: int) -> None:
        c = self.canvas
        rows = self._budget_rows()
        quota_rows = [row for row in rows if row["has_quota"]]
        stale_count = sum(
            1
            for row in rows
            for window in row["windows"]
            if window.get("quota_stale")
        )
        low_count = sum(1 for row in quota_rows if float(row.get("min_remaining") or 999) <= 20)
        effective_windows = [
            dict(window, account=row.get("name"))
            for row in rows
            for window in row["windows"]
            if window.get("quota_available") and window.get("pressure_active") and not window.get("quota_stale")
        ]
        inactive_low_count = sum(
            1
            for row in quota_rows
            if not row.get("pressure_active") and float(row.get("min_remaining") or 999) <= 20
        )
        pressure_window: dict[str, Any] | None = None
        for window in effective_windows:
            if pressure_window is None or float(window.get("utilization") or 0) > float(pressure_window.get("utilization") or 0):
                pressure_window = window
        worst_used = float(pressure_window.get("utilization") or 0) if pressure_window else 0.0
        try:
            worst_remaining = float(pressure_window.get("remaining")) if pressure_window else None
        except (TypeError, ValueError):
            worst_remaining = None
        pressure_label = str(pressure_window.get("label") or "") if pressure_window else ""
        donut_color = Theme.ag_crit if worst_used >= 80 else (Theme.ag_warn if worst_used >= 50 else Theme.ag_success)

        self._draw_rounded_rect(col_l, y, col_r, y + 92, r=8, fill=Theme.ag_surface, outline=Theme.ag_border)
        self._draw_donut(col_l + 10, y + 14, 64, worst_used, donut_color, f"{worst_used:.0f}%")
        c.create_text(col_l + 88, y + 14, anchor="nw", text="\u989d\u5ea6\u538b\u529b",
                      font=self._fonts["font_label_bold"], fill=Theme.text_primary)
        remaining_label = f"\u6700\u4f4e\u5269\u4f59 {worst_remaining:.0f}%" if worst_remaining is not None else "\u6682\u65e0\u5269\u4f59\u6570\u636e"
        detail = f"{remaining_label}  \u00b7  {pressure_label or '-'}  \u00b7  {len(effective_windows)} \u4e2a\u6d3b\u8dc3\u7a97\u53e3"
        c.create_text(col_l + 88, y + 36, anchor="nw", text=detail,
                      font=self._fonts["font_label"], fill=Theme.text_secondary)
        warning = "\u6d3b\u8dc3\u8d26\u53f7\u53ef\u80fd\u5373\u5c06\u9650\u989d" if pressure_window and worst_used >= 80 else "\u6d3b\u8dc3\u989d\u5ea6\u72b6\u6001\u6b63\u5e38"
        if not effective_windows:
            warning = "\u6682\u65e0\u6d3b\u8dc3\u989d\u5ea6\u538b\u529b"
        if stale_count:
            warning = f"{stale_count} \u4e2a\u7a97\u53e3\u5f85\u5237\u65b0"
        c.create_text(col_l + 88, y + 57, anchor="nw", text=warning,
                      font=self._fonts["font_tiny"], fill=Theme.ag_warn if stale_count or worst_used >= 80 else Theme.ag_success)
        x = col_l + 88
        y_chip = y + 70
        low_text = f"\u4f4e\u4f59\u989d {low_count}" if worst_remaining is None else f"\u6700\u4f4e {worst_remaining:.0f}%"
        x += self._draw_ag_chip(x, y_chip, low_text, Theme.ag_crit if worst_used >= 80 else Theme.ag_success) + 5
        x += self._draw_ag_chip(x, y_chip, f"\u975e\u6d3b\u8dc3\u4f4e\u989d {inactive_low_count}", Theme.ag_muted) + 5
        self._draw_ag_chip(x, y_chip, f"\u5f85\u5237\u65b0 {stale_count}", Theme.ag_warn)
        y += 106

        cats: list[dict[str, Any]] = []
        for key, label in (("5h", "5h"), ("7d", "7d"), ("cycle", "\u5468\u671f")):
            all_windows = [
                window
                for row in rows
                for window in row["windows"]
                if window.get("label") == label
            ]
            quota_windows = [window for window in all_windows if window.get("quota_available")]
            pressure_windows = [
                window
                for window in quota_windows
                if window.get("pressure_active") and not window.get("quota_stale")
            ]
            avg_used = (
                sum(float(window.get("utilization") or 0) for window in pressure_windows) / len(pressure_windows)
                if pressure_windows
                else 0.0
            )
            cats.append(
                {
                    "name": label,
                    "count": len(all_windows),
                    "quota_count": len(quota_windows),
                    "active_count": len(pressure_windows),
                    "unlimited_count": sum(1 for window in all_windows if window.get("quota_unlimited")),
                    "tokens": sum(int(window.get("tokens") or 0) for window in all_windows),
                    "cost": sum(float(window.get("cost") or 0) for window in all_windows),
                    "used": avg_used,
                    "stale": sum(1 for window in all_windows if window.get("quota_stale")),
                }
            )
        cats.sort(key=lambda item: item["used"], reverse=True)
        y = self._draw_ag_section(col_l, col_r, y, "Category Breakdown", "\u771f\u5b9e\u989d\u5ea6")
        for cat in cats:
            color = Theme.ag_crit if cat["used"] >= 80 else (Theme.ag_warn if cat["used"] >= 50 else Theme.ag_success)
            self._draw_rounded_rect(col_l, y, col_r, y + 38, r=6, fill=Theme.ag_surface, outline=Theme.ag_border)
            c.create_text(col_l + 10, y + 10, anchor="nw", text=str(cat["name"]),
                          font=self._fonts["font_label_bold"], fill=Theme.text_primary)
            c.create_text(col_l + 52, y + 10, anchor="nw", text=f"{compact_number(cat['tokens'])} tok",
                          font=self._fonts["font_micro"], fill=Theme.text_secondary)
            if cat["active_count"] <= 0 and cat["quota_count"] > 0:
                c.create_text(col_l + 52, y + 23, anchor="nw", text="\u6682\u65e0\u6d3b\u8dc3\u538b\u529b",
                              font=self._fonts["font_micro"], fill=Theme.ag_muted)
            c.create_text(col_r - 62, y + 10, anchor="ne", text=money(cat["cost"]),
                          font=self._fonts["font_micro"], fill=Theme.ag_muted)
            pct_text = (
                "无限"
                if cat["unlimited_count"] > 0 and cat["quota_count"] <= 0
                else f"{cat['used']:.0f}%"
            )
            self._draw_rounded_rect(col_r - 54, y + 8, col_r - 10, y + 27, r=6,
                                    fill=Theme.ag_bg, outline=color)
            c.create_text(col_r - 32, y + 17, anchor="center", text=pct_text,
                          font=self._fonts["font_micro"], fill=color)
            y += 44

        y = self._draw_ag_section(col_l, col_r, y + 4, "\u989d\u5ea6\u7a97\u53e3", "\u5269\u4f59\u4ece\u4f4e\u5230\u9ad8")

        list_top = y
        list_bottom = H - 38
        row_h = 84
        max_scroll = max(0, len(rows) * row_h - max(1, list_bottom - list_top))
        self._scroll_limits["budget"] = max_scroll
        self._scroll_offsets["budget"] = max(0, min(self._scroll_offsets.get("budget", 0), max_scroll))
        offset = self._scroll_offsets.get("budget", 0)
        if not rows:
            c.create_text(col_l + 8, y, anchor="nw", text="\u6682\u65e0\u989d\u5ea6\u7a97\u53e3\u6570\u636e",
                          font=self._fonts["font_label"], fill=Theme.text_muted)
            return
        for index, row in enumerate(rows):
            row_y = list_top + index * row_h - offset
            if row_y < list_top or row_y > list_bottom:
                continue
            name = self._truncate(ranking_account_display_name(row["name"]), "font_label_bold", col_r - col_l - 88)
            self._draw_rounded_rect(col_l, row_y, col_r, row_y + row_h - 8, r=10,
                                    fill=Theme.ag_surface, outline=Theme.ag_border)
            c.create_text(col_l + 10, row_y + 8, anchor="nw", text=name,
                          font=self._fonts["font_label_bold"], fill=Theme.text_primary)
            badge = account_type_label(row, row.get("name"))
            if not badge and row["source_badge"] == "SUB":
                badge = "SUB2"
            if badge:
                badge_w = self._text_width(badge, "font_micro") + 14
                self._draw_rounded_rect(col_r - badge_w - 10, row_y + 7, col_r - 10, row_y + 25, r=6,
                                        fill=Theme.ag_bg, outline=Theme.ag_border)
                c.create_text(col_r - badge_w - 3, row_y + 10, anchor="nw", text=badge,
                              font=self._fonts["font_micro"], fill=Theme.ag_muted)
            for win_index, window in enumerate(row["windows"][:3]):
                x1 = col_l + 10 + win_index * ((col_r - col_l - 28) // 3)
                x2 = col_l + 10 + (win_index + 1) * ((col_r - col_l - 28) // 3) - 5
                wy = row_y + 35
                label = str(window["label"])
                quota_unlimited = bool(window.get("quota_unlimited"))
                if quota_unlimited:
                    utilization = 0.0
                    color = Theme.ag_success
                    detail = "无5h限制"
                    reset = f"{compact_number(window.get('tokens', 0))} tok · {money(window.get('cost', 0))}"
                elif window["quota_available"]:
                    utilization = float(window.get("utilization") or 0)
                    color = self._ag_quota_color(utilization)
                    remaining = window.get("remaining")
                    try:
                        detail = f"\u5269\u4f59 {float(remaining):.0f}%"
                    except (TypeError, ValueError):
                        detail = "\u5269\u4f59 --"
                    if window.get("quota_idle"):
                        utilization = 0.0
                        detail = "\u6ee1\u989d\u5f85\u4f7f\u7528"
                        reset = "\u4f7f\u7528\u540e\u5f00\u59cb 5h \u5012\u8ba1\u65f6"
                    elif window.get("quota_reset_unavailable"):
                        reset = "\u91cd\u7f6e\u65f6\u95f4\u5f85\u540c\u6b65"
                    else:
                        reset = quota_reset_text(window.get("resets_at")) or "\u91cd\u7f6e -"
                    if window.get("quota_stale"):
                        detail = "\u5f85\u5237\u65b0"
                        color = Theme.ag_warn
                else:
                    utilization = 0.0
                    color = Theme.ag_muted
                    detail = "\u672a\u914d\u7f6e"
                    reset = "\u65e0\u989d\u5ea6"
                c.create_text(x1, wy, anchor="nw", text=label,
                              font=self._fonts["font_micro"], fill=Theme.ag_muted)
                c.create_text(x1 + 24, wy, anchor="nw", text=self._truncate(detail, "font_micro", max(30, x2 - x1 - 24)),
                              font=self._fonts["font_micro"], fill=color)
                bar_y = wy + 20
                if not quota_unlimited:
                    self._draw_rounded_rect(x1, bar_y, x2, bar_y + 5, r=2, fill=Theme.ag_bg, outline="")
                if window["quota_available"] and not quota_unlimited:
                    fill_w = int((x2 - x1) * max(0.02, min(1.0, utilization / 100.0)))
                    self._draw_rounded_rect(x1, bar_y, x1 + fill_w, bar_y + 5, r=2, fill=color, outline="")
                reset_y = bar_y + (1 if quota_unlimited else 9)
                c.create_text(x1, reset_y, anchor="nw", text=self._truncate(reset, "font_micro", max(40, x2 - x1)),
                              font=self._fonts["font_micro"], fill=Theme.ag_muted)

    @staticmethod
    def _usage_overview_needs_compact_values(
        col_l: int,
        col_r: int,
        left_group_width: int,
        right_group_width: int,
    ) -> bool:
        available = max(1, int(col_r) - int(col_l) - 24)
        minimum_chrome_width = 7  # Three gaps plus the narrowest useful meter.
        return (
            max(0, int(left_group_width))
            + max(0, int(right_group_width))
            + minimum_chrome_width
            > available
        )

    @staticmethod
    def _usage_overview_columns(
        col_l: int,
        col_r: int,
        left_group_width: int,
        right_group_width: int,
    ) -> tuple[int, int, int, int]:
        inner_l = int(col_l) + 12
        inner_r = max(inner_l + 1, int(col_r) - 12)
        available = inner_r - inner_l
        left_width = max(0, int(left_group_width))
        right_width = max(0, int(right_group_width))
        spare = max(0, available - left_width - right_width)
        if spare >= 58:
            left_gap, divider_gap, cost_gap = 6, 5, 8
            meter_width = 39
        elif spare >= 16:
            left_gap = divider_gap = cost_gap = 2
            meter_width = min(39, spare - 6)
        else:
            left_gap = divider_gap = cost_gap = 1
            meter_width = max(4, spare - 3)
        used = (
            left_width
            + left_gap
            + meter_width
            + divider_gap
            + cost_gap
            + right_width
        )
        leading_extra = max(0, available - used) // 2
        meter_l = inner_l + left_width + left_gap + leading_extra
        meter_r = meter_l + meter_width
        divider_x = meter_r + divider_gap
        cost_x = divider_x + cost_gap
        return meter_l, meter_r, divider_x, cost_x

    def _draw_usage_stats_page(self, col_l: int, col_r: int, y: int, H: int) -> None:
        c = self.canvas
        summary = self._usage_range_summary(self._usage_range)
        c.create_text(col_l, y, anchor="nw", text="\u7528\u91cf\u6982\u89c8",
                      font=self._fonts["font_section"], fill=Theme.text_primary)
        range_buttons = [("今日", "24h"), ("7d", "7d"), ("30d", "30d"), ("\u5168\u90e8", "all")]
        btn_w = 38
        gap = 2
        x = col_r - (btn_w * len(range_buttons) + gap * (len(range_buttons) - 1))
        self._draw_rounded_rect(x - 3, y - 2, col_r + 3, y + 22, r=7,
                                fill=Theme.ag_bg, outline=Theme.border)
        for label, value in range_buttons:
            name = f"usage_range_{value}"
            selected = self._usage_range == value
            self._btn_rects[name] = (x, y - 3, x + btn_w, y + 23)
            hovered = self._hover_btn == name
            fill = Theme.bg_lift if selected else (Theme.ag_surface_hover if hovered else Theme.ag_bg)
            text_color = Theme.text_primary if selected or hovered else Theme.ag_muted
            self._draw_rounded_rect(x, y, x + btn_w, y + 20, r=5, fill=fill, outline="")
            if selected:
                c.create_line(x + 9, y + 20, x + btn_w - 9, y + 20, fill=Theme.data, width=2)
            c.create_text(x + btn_w // 2, y + 10, anchor="center", text=label,
                          font=self._fonts["font_tiny"], fill=text_color)
            x += btn_w + gap
        y += 28

        compact_layout = H < 700
        hero_h = 82 if compact_layout else 86
        token_x = col_l + 12
        compact_tokens = compact_number(summary["tokens"])
        delta_text, delta_color, delta_visible = self._token_delta_badge_visual()
        cost_text = money(summary["cost"])
        cost_delta_text, cost_delta_color, cost_delta_visible = self._cost_delta_badge_visual()
        token_delta_width = max(
            self._fonts["font_delta"].measure(delta_text),
            self._fonts["font_delta"].measure("+000,000"),
        )
        cost_delta_width = max(
            self._fonts["font_delta"].measure(cost_delta_text),
            self._fonts["font_delta"].measure("+$0.000"),
        )
        value_font_key = "font_value"
        token_value_width = self._fonts[value_font_key].measure(compact_tokens)
        cost_value_width = self._fonts[value_font_key].measure(cost_text)
        if self._usage_overview_needs_compact_values(
            col_l,
            col_r,
            token_value_width + 5 + token_delta_width,
            cost_value_width + 5 + cost_delta_width,
        ):
            value_font_key = "font_value_sm"
            token_value_width = self._fonts[value_font_key].measure(compact_tokens)
            cost_value_width = self._fonts[value_font_key].measure(cost_text)
        meter_l, meter_r, divider_x, cost_x = self._usage_overview_columns(
            col_l,
            col_r,
            token_value_width + 5 + token_delta_width,
            cost_value_width + 5 + cost_delta_width,
        )
        self._draw_panel(col_l, y, col_r, y + hero_h, fill=Theme.ag_surface, radius=8)
        c.create_text(token_x, y + 9, anchor="nw", text="\u603b TOKEN",
                      font=self._fonts["font_tiny"], fill=Theme.ag_muted)
        c.create_text(token_x, y + 23, anchor="nw", text=compact_tokens,
                      font=self._fonts[value_font_key], fill=Theme.ag_accent)
        delta_x = token_x + token_value_width + 5
        c.create_text(
            delta_x,
            y + 26,
            anchor="nw",
            text=delta_text,
            font=self._fonts["font_delta"],
            fill=delta_color,
            state="normal" if delta_visible else "hidden",
            tags=("token_delta_badge",),
        )
        c.create_text(token_x, y + 50, anchor="nw", text=exact_token_count(summary["tokens"]),
                      font=self._fonts["font_data"], fill=Theme.text_secondary)
        c.create_text(token_x, y + 67, anchor="nw", text=f"{compact_number(summary['requests'])} \u6b21\u8bf7\u6c42",
                      font=self._fonts["font_tiny"], fill=Theme.text_secondary)
        c.create_line(divider_x, y + 12, divider_x, y + hero_h - 12, fill=Theme.ag_divider, width=1)
        self._draw_token_flow_meter(meter_l, y + 12, meter_r, y + hero_h - 10, bars=10)
        c.create_text(cost_x, y + 9, anchor="nw", text="\u9884\u4f30\u6210\u672c",
                      font=self._fonts["font_tiny"], fill=Theme.ag_muted)
        cost_delta_x = cost_x + cost_value_width + 5
        c.create_text(cost_x, y + 25, anchor="nw", text=cost_text,
                      font=self._fonts[value_font_key], fill=Theme.warn)
        c.create_text(
            cost_delta_x,
            y + 26,
            anchor="nw",
            text=cost_delta_text,
            font=self._fonts["font_delta"],
            fill=cost_delta_color,
            state="normal" if cost_delta_visible else "hidden",
            tags=("cost_delta_badge",),
        )
        c.create_text(cost_x, y + 58, anchor="nw", text=f"{summary['label']} \u65f6\u95f4\u7a97\u53e3",
                      font=self._fonts["font_tiny"], fill=Theme.text_secondary)
        self._add_tooltip(
            col_l,
            y,
            meter_l - 4,
            y + hero_h,
            f"\u603b Token\n{exact_token_count(summary['tokens'])}\n{int(summary['requests'] or 0):,} \u6b21\u8bf7\u6c42",
        )
        y += hero_h + 10

        mix = self._summary_token_mix(summary)
        token_items = [
            ("\u8f93\u5165", mix["input"], Theme.ag_input),
            ("\u7f13\u5b58\u8bfb\u53d6", mix["cached"], Theme.ag_cache),
            ("\u7f13\u5b58\u5199\u5165", mix["cache_create"], Theme.ag_reason),
            ("\u8f93\u51fa", mix["output"], Theme.ag_output),
        ]
        cache_base = mix["input"] + mix["cached"] + mix["cache_create"]
        cache_hit_text = f"{mix['cached'] * 100 / cache_base:.1f}%" if cache_base > 0 else "-"
        chip_items = [
            ("\u8f93\u5165", compact_number(mix["input"]), Theme.ag_input),
            ("\u7f13\u5b58\u8bfb\u53d6", compact_number(mix["cached"]), Theme.ag_cache),
            ("\u7f13\u5b58\u5199\u5165", compact_number(mix["cache_create"]), Theme.ag_reason),
            ("\u8f93\u51fa", compact_number(mix["output"]), Theme.ag_output),
        ]
        if mix.get("unknown", 0) > 0:
            token_items.append(("\u672a\u5f52\u7c7b", mix["unknown"], Theme.ag_muted))
            chip_items.append(("\u672a\u5f52\u7c7b", compact_number(mix["unknown"]), Theme.ag_muted))
        mix_total = sum(value for _label, value, _color in token_items)
        chip_badge = f"{summary['label']}  \u00b7  \u7f13\u5b58\u547d\u4e2d {cache_hit_text}"
        if mix.get("unknown", 0) > 0:
            chip_badge += "  \u00b7  \u90e8\u5206"
        y = self._draw_ag_section(col_l, col_r, y, "Token \u6784\u6210", chip_badge)
        chip_w = (col_r - col_l - 8) // 2
        for index, (label, value_text, color) in enumerate(chip_items):
            cx = col_l + (index % 2) * (chip_w + 8)
            cy = y + (index // 2) * 34
            self._draw_rounded_rect(cx, cy, cx + chip_w, cy + 27, r=6,
                                    fill=Theme.ag_surface, outline=Theme.ag_border)
            c.create_oval(cx + 8, cy + 10, cx + 15, cy + 17, fill=color, outline="")
            c.create_text(cx + 22, cy + 6, anchor="nw", text=label,
                          font=self._fonts["font_micro"], fill=Theme.text_secondary)
            c.create_text(cx + chip_w - 8, cy + 6, anchor="ne", text=value_text,
                          font=self._fonts["font_data"], fill=Theme.text_primary)
            self._add_tooltip(
                cx,
                cy,
                cx + chip_w,
                cy + 27,
                f"{label}\n{exact_token_count(token_items[index][1])} Token",
            )
        y += max(72, math.ceil(len(chip_items) / 2) * 34 + 4)
        bar_x = col_l
        bar_w = col_r - col_l
        self._draw_rounded_rect(bar_x, y, bar_x + bar_w, y + 8, r=3, fill=Theme.ag_bg, outline="")
        cursor = bar_x
        for label, value, color in token_items:
            if mix_total <= 0 or value <= 0:
                continue
            seg_w = int(bar_w * value / mix_total)
            segment_end = min(bar_x + bar_w, cursor + seg_w)
            c.create_rectangle(cursor, y, segment_end, y + 8, fill=color, outline="")
            if segment_end > cursor:
                self._add_tooltip(
                    cursor,
                    y,
                    segment_end,
                    y + 8,
                    f"{label}\n{exact_token_count(value)} Token",
                )
            cursor = segment_end
        y += 22

        series = summary.get("series") if isinstance(summary, dict) else []
        series = [item for item in series if isinstance(item, dict)]
        y = self._draw_activity_heatmap(col_l, col_r, y, summary, series)
        if False and series:
            if self._usage_range == "24h":
                visible = series[-24:]
            else:
                visible = series[-30:] if self._usage_range == "30d" else series[-7:]
            max_tokens = max([float(item.get("tokens") or 0) for item in visible], default=0.0) or 1.0
            cols = 12 if self._usage_range == "24h" else (15 if self._usage_range == "30d" else 7)
            cell_gap = 4
            cell = max(8, min(18, int((col_r - col_l - cell_gap * (cols - 1)) / cols)))
            for index, item in enumerate(visible):
                row = index // cols
                col = index % cols
                tokens = float(item.get("tokens") or 0)
                intensity = min(1.0, tokens / max_tokens) if tokens > 0 else 0.0
                color = Theme.ag_bg
                if intensity > 0.66:
                    color = Theme.ag_accent
                elif intensity > 0.33:
                    color = Theme.ag_bar
                elif intensity > 0:
                    color = Theme.amber_glow
                x1 = col_l + col * (cell + cell_gap)
                y1 = y + row * (cell + cell_gap)
                self._draw_rounded_rect(x1, y1, x1 + cell, y1 + cell, r=3, fill=color, outline=Theme.ag_border)
                if self._usage_range == "24h":
                    hour = int(item.get("hour") if item.get("hour") is not None else index)
                    next_hour = (hour + 1) % 24
                    tip_title = f"{hour:02d}:00-{next_hour:02d}:00"
                else:
                    tip_title = str(item.get("date") or "-")
                self._add_tooltip(
                    x1,
                    y1,
                    x1 + cell,
                    y1 + cell,
                    f"{tip_title}\n{exact_token_count(tokens)}\n{compact_number(item.get('requests', 0))} calls · {money(item.get('cost', 0))}",
                )
            heatmap_rows = max(1, math.ceil(len(visible) / cols))
            if self._usage_range == "24h":
                label_y = y + heatmap_rows * (cell + cell_gap) + 1
                for label, col_index in (("00", 0), ("06", 3), ("12", 6), ("18", 9)):
                    lx = col_l + col_index * (cell + cell_gap)
                    c.create_text(lx, label_y, anchor="nw", text=label,
                                  font=self._fonts["font_micro"], fill=Theme.ag_muted)
                y += 12
            y += heatmap_rows * (cell + cell_gap) + 10
        elif False:
            c.create_text(col_l + 4, y, anchor="nw", text="\u6682\u65e0\u8d8b\u52bf\u6570\u636e",
                          font=self._fonts["font_label"], fill=Theme.ag_muted)
            y += 30

        providers = sorted(
            self._filter_account_display_rows(
                self._usage_range_providers(self._usage_range)
            ),
            key=lambda row: (-float(row.get("cost") or 0), -int(row.get("tokens") or 0), str(row.get("name") or "")),
        )
        models = self._top_models(self._usage_range)
        list_bottom = H - 38
        model_limit = self._top_model_visible_count(
            len(models),
            len(providers),
            list_bottom - y,
        )
        model_badge = (
            f"{len(models)} \u4e2a\u6a21\u578b"
            if model_limit >= len(models)
            else f"{model_limit}/{len(models)} \u4e2a\u6a21\u578b"
        )
        y = self._draw_ag_section(col_l, col_r, y, "\u5e38\u7528\u6a21\u578b", model_badge)
        if not models:
            c.create_text(col_l + 4, y, anchor="nw", text="\u6682\u65e0\u6a21\u578b\u7edf\u8ba1",
                          font=self._fonts["font_label"], fill=Theme.ag_muted)
            y += 28
        else:
            max_model_tokens = max(tokens for _model, tokens in models) or 1
            for model, tokens in models[:model_limit]:
                self._draw_panel(col_l, y, col_r, y + 24, fill=Theme.ag_surface, radius=6)
                model_token_text = f"{compact_number(tokens)} tok"
                model_token_width = self._text_width(model_token_text, "font_micro")
                c.create_text(col_l + 9, y + 4, anchor="nw",
                              text=self._truncate(
                                  model,
                                  "font_data",
                                  max(70, col_r - col_l - model_token_width - 30),
                              ),
                              font=self._fonts["font_data"], fill=Theme.text_primary)
                pct = int(tokens * 100 / max_model_tokens)
                c.create_text(col_r - 9, y + 4, anchor="ne", text=model_token_text,
                              font=self._fonts["font_micro"], fill=Theme.text_secondary)
                self._draw_rounded_rect(col_l + 9, y + 18, col_r - 9, y + 21, r=1, fill=Theme.ag_bg, outline="")
                fill_w = int((col_r - col_l - 18) * pct / 100)
                self._draw_rounded_rect(col_l + 9, y + 18, col_l + 9 + fill_w, y + 21, r=1,
                                        fill=Theme.ag_accent, outline="")
                self._add_tooltip(
                    col_l,
                    y,
                    col_r,
                    y + 24,
                    f"{model}\n{exact_token_count(tokens)} Token",
                )
                y += 28

        y = self._draw_ag_section(col_l, col_r, y, "\u8d26\u53f7\u7d2f\u8ba1", f"{len(providers)} \u4e2a\u8d26\u53f7")
        list_top = y
        row_h = 48
        available_rows = max(0, (list_bottom - list_top) // row_h)
        max_start_index = max(0, len(providers) - available_rows)
        max_scroll = max_start_index * row_h
        self._scroll_limits["stats"] = max_scroll
        self._scroll_offsets["stats"] = max(0, min(self._scroll_offsets.get("stats", 0), max_scroll))
        if not providers:
            c.create_text(col_l + 8, y, anchor="nw", text="\u6682\u65e0 provider \u6570\u636e",
                          font=self._fonts["font_label"], fill=Theme.ag_muted)
            return
        max_provider_cost = max([float(row.get("cost") or 0) for row in providers], default=0.0) or 1.0
        first_index = min(max_start_index, self._scroll_offsets["stats"] // row_h)
        visible_providers = providers[first_index:first_index + available_rows]
        for visible_index, provider in enumerate(visible_providers):
            row_y = list_top + visible_index * row_h
            self._draw_panel(col_l, row_y, col_r, row_y + row_h - 7,
                             fill=Theme.ag_surface, radius=6)
            raw_provider_name = str(provider.get("name") or "-")
            provider_name = ranking_account_display_name(raw_provider_name)
            provider_type = account_type_label(provider, raw_provider_name)
            provider_type_w = self._text_width(provider_type, "font_micro") + 14 if provider_type else 0
            provider_name_x = col_l + 9 + (provider_type_w + 6 if provider_type else 0)
            name = self._truncate(
                provider_name,
                "font_label",
                max(60, col_r - provider_name_x - 132),
            )
            provider_tokens = provider.get("tokens", 0)
            compact_tokens = compact_number(provider_tokens)
            exact_tokens = exact_token_count(provider_tokens)
            cost_value = float(provider.get("cost") or 0)
            requests_count = compact_number(provider.get("requests", 0))
            if provider_type:
                self._draw_health_badge(col_l + 9, row_y + 4, provider_type)
            c.create_text(provider_name_x, row_y + 6, anchor="nw", text=name,
                          font=self._fonts["font_label"], fill=Theme.text_primary)
            c.create_text(col_r - 9, row_y + 6, anchor="ne", text=money(cost_value),
                          font=self._fonts["font_label_bold"], fill=Theme.warn)
            c.create_text(col_l + 9, row_y + 24, anchor="nw", text=f"{compact_tokens} tok  \u00b7  {requests_count} \u6b21",
                          font=self._fonts["font_micro"], fill=Theme.ag_muted)
            bar_w = int((col_r - col_l - 18) * min(1.0, cost_value / max_provider_cost))
            if bar_w > 0:
                c.create_rectangle(col_l + 9, row_y + 38, col_l + 9 + bar_w, row_y + 40,
                                   fill=Theme.ag_bar, outline="")
            self._add_tooltip(
                col_l,
                row_y,
                col_r,
                row_y + row_h - 7,
                f"{provider_name}{f'  ·  {provider_type}' if provider_type else ''}\n{exact_tokens} tokens\n{money(cost_value)}",
            )

        self._draw_list_scrollbar(
            "stats",
            col_r - 1,
            list_top,
            min(list_bottom - 2, list_top + len(visible_providers) * row_h - 7),
            len(visible_providers),
            len(providers),
            max_scroll,
        )

    def _draw(self) -> None:
        if self.closed:
            return
        c = self.canvas
        c.delete("all")
        self._token_flow_trace_rect = None
        self._token_flow_meter_rect = None
        self._token_flow_meter_fill_bounds = None
        self._tooltip_rects = []
        self._active_scroll_rect = None
        self._list_scrollbar_tracks = {"accounts": None, "active": None, "stats": None}
        self._list_scrollbar_thumbs = {"accounts": None, "active": None, "stats": None}
        self._scroll_limits["active"] = 0
        W, H = self.WIDTH, self.HEIGHT
        actual_w = self.root.winfo_width()
        actual_h = self.root.winfo_height()
        if actual_w > 50 and actual_h > 50 and (actual_w != W or actual_h != H):
            self._apply_window_size(W, H)
        PAD = 14
        COL_L = PAD
        COL_R = W - PAD

        # ── outer card background ──
        self._draw_rounded_rect(3, 6, W - 2, H - 2, r=14, fill=Theme.shadow, outline="")
        self._draw_rounded_rect(0, 0, W, H - 5, r=14, fill=Theme.bg_card, outline=Theme.border, width=1)
        c.create_line(18, 1, W - 18, 1, fill=Theme.border, width=1)

        # ════════════════════════════════════════════════════════
        #  HEADER  (row y=10..48)
        # ════════════════════════════════════════════════════════
        sync_state = str((self.state.usage_sync or {}).get("state") or "") if self.state else ""
        verifying_live_usage = bool(
            getattr(self, "_live_usage_verification_pending", False)
        )
        y = 12
        if self._loading:
            phase = (math.sin(self._pulse_phase) + 1.0) / 2.0
            pulse_rgb = (
                int(40 + 48 * phase),
                int(88 + 126 * phase),
                int(79 + 94 * phase),
            )
            pulse_color = "#%02x%02x%02x" % pulse_rgb
        elif verifying_live_usage:
            pulse_color = Theme.warn
        elif self.state:
            if sync_state in {"timeout", "error", "unavailable", "stale"}:
                pulse_color = Theme.coral
            elif sync_state == "partial":
                pulse_color = Theme.warn
            else:
                pulse_color = Theme.live
        else:
            pulse_color = Theme.text_muted
        self._draw_header_mark(COL_L, y, pulse_color)

        title_x = COL_L + 39
        c.create_text(title_x, y - 1, anchor="nw", text="Token Pulse",
                      font=self._fonts["font_title"], fill=Theme.text_primary)

        active_count = len(self.state.active_accounts or []) if self.state else 0
        if verifying_live_usage:
            pending_tokens = max(
                0,
                int(getattr(self, "_live_usage_verification_pending_tokens", 0) or 0),
            )
            updated = f"核对 {compact_number(pending_tokens)} Token"
        elif self._refresh_pending:
            updated = "\u5237\u65b0\u5df2\u6392\u961f"
        else:
            updated = "\u6b63\u5728\u5237\u65b0" if self._loading else "\u7b49\u5f85\u5237\u65b0"
        if (
            self.state
            and self.state.updated_at
            and not self._loading
            and not self._refresh_pending
            and not verifying_live_usage
        ):
            updated = relative_time(datetime.fromtimestamp(self.state.updated_at, timezone.utc).isoformat())
        sync_label = usage_sync_label(self.state.usage_sync if self.state else None)
        if (
            sync_label
            and not self._loading
            and not self._refresh_pending
            and not verifying_live_usage
        ):
            updated = sync_label
        subtitle = f"\u6d3b\u8dc3 {active_count}  \u00b7  {updated}"
        c.create_text(title_x, y + 24, anchor="nw", text=subtitle,
                      font=self._fonts["font_tiny"],
                      fill=pulse_color if self.state else Theme.text_muted)

        btn_y = y + 1
        close_glyph = "\ue8bb" if self._fluent_icons else "\u00d7"
        pin_glyph = (
            ("\ue718" if self._pinned else "\ue77a")
            if self._fluent_icons
            else ("\u25c6" if self._pinned else "\u25c7")
        )
        refresh_glyph = "\ue72c" if self._fluent_icons else "\u21bb"
        btn_specs = [
            ("btn_close", close_glyph, COL_R - 11, "\u5173\u95ed"),
            (
                "btn_pin",
                pin_glyph,
                COL_R - 37,
                "\u53d6\u6d88\u7f6e\u9876" if self._pinned else "\u4fdd\u6301\u7f6e\u9876",
            ),
            ("btn_refresh", refresh_glyph, COL_R - 63, "\u5237\u65b0\u6570\u636e"),
        ]
        self._btn_rects.clear()
        for name, glyph, bx, tooltip in btn_specs:
            bx1, by1, bx2, by2 = bx - 10, btn_y - 2, bx + 10, btn_y + 18
            self._btn_rects[name] = (bx1, by1, bx2, by2)
            self._add_tooltip(bx1, by1, bx2, by2, tooltip)
            is_hover = self._hover_btn == name
            bg = Theme.bg_hover if is_hover else ""
            if bg:
                self._draw_rounded_rect(bx1, by1, bx2, by2, r=6, fill=bg, outline="")
            fg = Theme.text_primary if is_hover else Theme.text_secondary
            if name == "btn_close":
                fg = Theme.accent_red if is_hover else Theme.text_secondary
            elif name == "btn_refresh" and (self._loading or self._refresh_pending):
                fg = Theme.live
            c.create_text(bx, btn_y + 8, text=glyph, font=self._fonts["font_icon"],
                           fill=fg, anchor="center")

        y = 55
        c.create_line(COL_L, y, COL_R, y, fill=Theme.border, width=1)
        y += 8
        y = self._draw_main_tabs(COL_L, COL_R, y)
        if self._main_tab == "stats":
            self._draw_usage_stats_page(COL_L, COL_R, y, H)
            self._draw_footer(W, H)
            self._draw_tooltip(W, H)
            return

        # ════════════════════════════════════════════════════════
        #  CURRENT CHANNEL HERO
        # ════════════════════════════════════════════════════════
        y += 8
        self._draw_panel(COL_L, y, COL_R, y + 72, fill=Theme.bg_section, radius=8)
        all_active_accounts = list(self.state.active_accounts or []) if self.state else []
        accounts = all_active_accounts
        latest_name = self.state.latest_account_name if self.state else ""
        total_current = sum(int(account.get("current") or 0) for account in all_active_accounts)
        if accounts:
            raw_hero_name = accounts[0].get("name", latest_name or "-")
            hero_name = ranking_account_display_name(str(raw_hero_name))
            hero_type = account_type_label(accounts[0], raw_hero_name)
            hero_sub = f"{len(all_active_accounts)} \u4e2a\u8d26\u53f7\u5728\u7ebf  \u00b7  \u8def\u7531\u6b63\u5e38"
            if hero_type:
                hero_sub = f"{hero_type}  \u00b7  {hero_sub}"
            hero_color = Theme.accent_green
        else:
            status, _model, ago, color = self._latest_status()
            hero_name = ranking_account_display_name(str(latest_name)) if latest_name else (
                "\u6b63\u5728\u8bfb\u53d6\u6570\u636e"
                if self._loading or not self.state
                else "\u6682\u65e0\u6570\u636e"
            )
            hero_sub = (
                f"\u6700\u8fd1 {status}  \u00b7  {ago}"
                if status != "-"
                else ("\u521d\u59cb\u5316\u4e2d" if self._loading or not self.state else "\u6682\u65e0\u6d3b\u8dc3\u8bf7\u6c42")
            )
            hero_color = color if status != "-" else Theme.cyan
        metric_l = COL_R - 72
        c.create_rectangle(COL_L, y + 12, COL_L + 3, y + 60, fill=hero_color, outline="")
        c.create_oval(COL_L + 13, y + 13, COL_L + 21, y + 21, fill=hero_color, outline="")
        c.create_text(COL_L + 28, y + 10, anchor="nw", text="\u5f53\u524d\u8def\u7531",
                      font=self._fonts["font_tiny"], fill=Theme.text_muted)
        self._draw_token_flow_trace(COL_L + 92, y + 5, metric_l - 12, y + 26)
        display_name = self._truncate(str(hero_name), "font_hero", metric_l - COL_L - 42)
        c.create_text(COL_L + 14, y + 29, anchor="nw", text=display_name,
                      font=self._fonts["font_hero"], fill=Theme.text_primary)
        c.create_text(COL_L + 14, y + 52, anchor="nw", text=hero_sub,
                      font=self._fonts["font_tiny"], fill=hero_color)
        if display_name != str(hero_name):
            self._add_tooltip(COL_L + 14, y + 27, metric_l - 8, y + 48, str(hero_name))
        c.create_line(metric_l, y + 13, metric_l, y + 59, fill=Theme.ag_divider, width=1)
        metric_x = (metric_l + COL_R) // 2
        metric_value_y = y + 17
        metric_label_y = y + 44
        if not accounts:
            source_label = (
                "DIRECT"
                if self.state and self.state.client_usage
                else str(self.state.source_label if self.state else "WAIT").upper()
            )
            c.create_text(metric_x, y + 9, anchor="n",
                          text=self._truncate(source_label, "font_data", 54),
                          font=self._fonts["font_data"], fill=Theme.data)
            metric_value_y = y + 28
            metric_label_y = y + 51
        c.create_text(metric_x, metric_value_y, anchor="n", text=compact_number(total_current),
                      font=self._fonts["font_value_sm"], fill=Theme.text_primary)
        c.create_text(metric_x, metric_label_y, anchor="n", text="\u5e76\u53d1",
                      font=self._fonts["font_tiny"], fill=Theme.text_muted)
        y += 82

        # ════════════════════════════════════════════════════════
        #  ACTIVE ACCOUNTS
        # ════════════════════════════════════════════════════════
        section_y = y
        c.create_text(COL_L, section_y, anchor="nw", text="\u5f53\u524d\u6d3b\u8dc3",
                      font=self._fonts["font_section"], fill=Theme.text_primary)

        y += 24
        active_row_h = 26
        default_height = int(type(self).HEIGHT)
        base_capacity = 1 + max(
            0,
            (min(H, default_height) - 700) // active_row_h,
        )
        active_capacity = balanced_active_row_capacity(
            H,
            default_height,
            active_row_h,
            base_rows=base_capacity,
        )
        visible_active_rows = min(len(accounts), active_capacity)
        max_active_start = max(0, len(accounts) - visible_active_rows)
        active_scroll_limit = max_active_start * active_row_h
        self._scroll_limits["active"] = active_scroll_limit
        self._scroll_offsets["active"] = max(
            0,
            min(int(self._scroll_offsets.get("active", 0) or 0), active_scroll_limit),
        )
        first_active_index = min(
            max_active_start,
            self._scroll_offsets["active"] // active_row_h,
        )
        visible_accounts = accounts[
            first_active_index:first_active_index + visible_active_rows
        ]
        if active_scroll_limit > 0:
            last_active_index = first_active_index + len(visible_accounts)
            c.create_text(
                COL_R,
                section_y + 1,
                anchor="ne",
                text=f"{first_active_index + 1}-{last_active_index}/{len(accounts)}",
                font=self._fonts["font_micro"],
                fill=Theme.text_muted,
            )

        if not accounts:
            c.create_text(COL_L + 8, y, anchor="nw", text=("\u6b63\u5728\u8bfb\u53d6\u8d26\u53f7\u72b6\u6001" if self._loading or not self.state else "\u6682\u65e0\u6d3b\u8dc3"),
                           font=self._fonts["font_label"], fill=Theme.text_muted)
            y += 20
        else:
            active_list_top = y
            active_list_bottom = y + visible_active_rows * active_row_h
            self._active_scroll_rect = (
                COL_L,
                active_list_top,
                COL_R,
                active_list_bottom,
            )

        for acc in visible_accounts:
            raw_name = str(acc.get("name") or "-")
            full_name = ranking_account_display_name(raw_name)
            type_badge = account_type_label(acc, raw_name)
            cur = acc.get("current", 0)
            mx = acc.get("max", 1)
            pill_w = 54
            type_w = self._text_width(type_badge, "font_micro") + 14 if type_badge else 0
            name_x = COL_L + 8 + (type_w + 6 if type_badge else 0)
            pill_left = COL_R - pill_w
            name_max_w = max(60, pill_left - name_x - 10)
            name = self._truncate(full_name, "font_label", name_max_w)

            if type_badge:
                self._draw_health_badge(COL_L + 8, y - 1, type_badge)
            c.create_text(name_x, y, anchor="nw", text=name,
                           font=self._fonts["font_label"], fill=Theme.text_primary)
            if name != full_name:
                self._add_tooltip(name_x, y, pill_left - 8, y + 21, full_name)
            frac_text = f"{compact_number(cur)}/{compact_number(mx)}"
            self._draw_rounded_rect(COL_R - pill_w, y - 2, COL_R - 4, y + 21, r=8,
                                    fill=Theme.bg_section, outline=Theme.border)
            c.create_text(COL_R - 4 - pill_w / 2, y + 9, anchor="center", text=frac_text,
                           font=self._fonts["font_label_bold"], fill=Theme.accent_green)
            y += active_row_h

        if active_scroll_limit > 0 and self._active_scroll_rect is not None:
            self._draw_list_scrollbar(
                "active",
                COL_R - 1,
                self._active_scroll_rect[1],
                self._active_scroll_rect[3] - 3,
                visible_active_rows,
                len(accounts),
                active_scroll_limit,
            )

        y += 4
        c.create_line(COL_L, y, COL_R, y, fill=Theme.border, width=1)

        # ════════════════════════════════════════════════════════
        #  LATEST REQUEST
        # ════════════════════════════════════════════════════════
        y += 10
        y = self._draw_section_label(COL_L, COL_R, y, "\u6700\u8fd1\u8bf7\u6c42")
        request_h = 52
        self._draw_panel(COL_L, y, COL_R, y + request_h, fill=Theme.ag_surface, radius=7)

        if self.state and self.state.latest_request:
            req = self.state.latest_request
            kind = req.get("kind", "-")
            model = req.get("model", "-")
            speed_badge = str(req.get("speed_badge") or "")
            if speed_badge:
                model = f"{model} / {speed_badge}"
            created = req.get("created_at", "")
            raw_acct = self.state.latest_account_name or "-"
            acct = ranking_account_display_name(raw_acct)
            acct_type = account_type_label(name=raw_acct)
            status_text = "\u9519\u8bef" if kind == "error" else ("\u6210\u529f" if kind else "-")
            status_color = Theme.accent_red if kind == "error" else Theme.accent_green

            status_w = self._text_width(status_text, "font_tiny") + 22
            status_x = COL_R - status_w - 9
            self._draw_rounded_rect(status_x, y + 8, COL_R - 9, y + 27, r=6,
                                    fill=Theme.bg_dark, outline=Theme.border)
            c.create_oval(status_x + 7, y + 14, status_x + 13, y + 20,
                          fill=status_color, outline="")
            c.create_text(status_x + 17, y + 11, anchor="nw", text=status_text,
                          font=self._fonts["font_tiny"], fill=status_color)
            account_type_w = self._text_width(acct_type, "font_micro") + 14 if acct_type else 0
            account_x = COL_L + 10 + (account_type_w + 6 if acct_type else 0)
            account_text = self._truncate(acct, "font_label_bold", status_x - account_x - 6)
            if acct_type:
                self._draw_health_badge(COL_L + 10, y + 8, acct_type)
            c.create_text(account_x, y + 9, anchor="nw", text=account_text,
                          font=self._fonts["font_label_bold"], fill=Theme.text_primary)
            if account_text != acct:
                self._add_tooltip(COL_L + 10, y + 7, status_x - 6, y + 27, acct)
            c.create_text(COL_L + 10, y + 32, anchor="nw", text="MODEL",
                          font=self._fonts["font_data"], fill=Theme.text_muted)
            c.create_text(COL_L + 54, y + 31, anchor="nw",
                          text=self._truncate(model, "font_data", COL_R - COL_L - 142),
                          font=self._fonts["font_data"], fill=Theme.data)
            c.create_text(COL_R - 10, y + 31, anchor="ne",
                          text=relative_time(created) if created else "-",
                          font=self._fonts["font_tiny"], fill=Theme.text_muted)
        else:
            empty_request = (
                "\u6b63\u5728\u8bfb\u53d6\u6700\u8fd1\u8bf7\u6c42"
                if self._loading or not self.state
                else "\u6682\u65e0\u8bf7\u6c42\u8bb0\u5f55"
            )
            c.create_text(COL_L + 10, y + 17, anchor="nw", text=empty_request,
                          font=self._fonts["font_label"], fill=Theme.text_muted)

        y += request_h + 8
        c.create_line(COL_L, y, COL_R, y, fill=Theme.border, width=1)

        # ════════════════════════════════════════════════════════
        #  TODAY STATS
        # ════════════════════════════════════════════════════════
        y += 10
        source_text = ""
        if self.state and self.state.client_usage:
            client_tokens = int(self.state.client_usage.get("tokens") or 0)
            client_requests = int(self.state.client_usage.get("requests") or 0)
            if client_tokens or client_requests:
                source_text = f"\u603b\u91cf {compact_number(client_tokens)} tok"
        elif self.state and self.state.usage_note:
            source_text = self._truncate(self.state.usage_note, "font_tiny", 180)
        y = self._draw_section_label(COL_L, COL_R, y, "\u4eca\u65e5\u6982\u89c8", source_text)

        today_requests = int(self.state.today_requests or 0) if self.state else 0
        today_tokens = int(self.state.today_tokens or 0) if self.state else 0
        today_cost = float(self.state.today_account_cost or 0) if self.state else 0.0
        stats = [
            (
                "\u8bf7\u6c42",
                compact_number(today_requests),
                Theme.amber_bright,
                f"\u4eca\u65e5\u8bf7\u6c42\n{today_requests:,} \u6b21",
            ),
            (
                "Token",
                compact_number(today_tokens),
                Theme.cyan,
                f"\u4eca\u65e5 Token\n{exact_token_count(today_tokens)} Token",
            ),
            (
                "\u6210\u672c",
                money(today_cost),
                Theme.warn,
                f"\u4eca\u65e5\u6210\u672c\n{money(today_cost)}",
            ),
        ]
        overview_width = COL_R - COL_L
        column_edges = (
            COL_L,
            COL_L + round(overview_width * 0.29),
            COL_L + round(overview_width * 0.64),
            COL_R,
        )
        col_w = overview_width // 3
        self._draw_panel(COL_L, y - 5, COL_R, y + 43, fill=Theme.ag_surface, radius=7)
        for i, (lbl, val, color, tooltip) in enumerate(stats):
            stat_x1 = column_edges[i]
            stat_x2 = column_edges[i + 1]
            cx = (stat_x1 + stat_x2) // 2
            if i:
                c.create_line(stat_x1, y + 2, stat_x1, y + 36,
                              fill=Theme.ag_divider, width=1)
            available_width = max(32, stat_x2 - stat_x1 - 10)
            display_value = val
            value_font_key = "font_value_xs"
            value_width = 0
            for candidate in ("font_value", "font_value_sm", "font_value_xs"):
                candidate_width = self._fonts[candidate].measure(display_value)
                if candidate_width <= available_width:
                    value_font_key = candidate
                    value_width = candidate_width
                    break
            if value_width <= 0:
                display_value = self._truncate(
                    display_value,
                    value_font_key,
                    available_width,
                )
                value_width = self._fonts[value_font_key].measure(display_value)
            value_y = y + (3 if value_font_key != "font_value" else 0)
            c.create_text(cx, value_y, anchor="n", text=display_value,
                           font=self._fonts[value_font_key], fill=color)
            c.create_text(cx, y + 26, anchor="n", text=lbl,
                           font=self._fonts["font_tiny"], fill=Theme.text_secondary)
            self._add_tooltip(stat_x1, y - 5, stat_x2, y + 43, tooltip)

        y += 50
        c.create_line(COL_L, y, COL_R, y, fill=Theme.border, width=1)

        y += 10
        history = (self.state.cost_history if self.state else None) or summarize_trend_rows([])
        seven_day_tokens = float(history.get("seven_day_tokens") or 0)
        trend_header_y = y
        trend_meta = f"近 7 日  {compact_number(seven_day_tokens)} TOK"
        y = self._draw_section_label(
            COL_L,
            COL_R,
            y,
            "Token \u8d8b\u52bf",
            trend_meta,
        )
        trend_meta_width = self._text_width(trend_meta, "font_tiny")
        self._add_tooltip(
            COL_R - trend_meta_width - 4,
            trend_header_y,
            COL_R,
            trend_header_y + 22,
            f"\u8fd1 7 \u5929 Token\n{exact_token_count(seven_day_tokens)} Token",
        )
        trend_today = float(history.get("today_tokens") or 0)
        trend_yesterday = float(history.get("yesterday_tokens") or 0)
        trend_average = seven_day_tokens / 7
        cost_stats = [
            (
                "\u4eca\u65e5",
                f"{compact_number(trend_today)} tok",
                Theme.ag_accent,
                f"\u4eca\u65e5 Token\n{exact_token_count(trend_today)} Token",
            ),
            (
                "\u6628\u65e5",
                f"{compact_number(trend_yesterday)} tok",
                Theme.ag_success,
                f"\u6628\u65e5 Token\n{exact_token_count(trend_yesterday)} Token",
            ),
            (
                "\u65e5\u5747",
                f"{compact_number(trend_average)} tok",
                Theme.amber_glow,
                f"\u8fd1 7 \u5929\u65e5\u5747\n{exact_token_count(trend_average)} Token",
            ),
        ]
        for i, (lbl, val, color, tooltip) in enumerate(cost_stats):
            metric_x1 = COL_L + col_w * i
            metric_x2 = COL_R if i == len(cost_stats) - 1 else metric_x1 + col_w
            cx = COL_L + col_w * i + col_w // 2
            c.create_text(cx, y, anchor="n", text=val,
                           font=self._fonts["font_value_sm"], fill=color)
            c.create_text(cx, y + 21, anchor="n", text=lbl,
                           font=self._fonts["font_micro"], fill=Theme.text_secondary)
            self._add_tooltip(metric_x1, y - 3, metric_x2, y + 38, tooltip)
        series = history.get("series") if isinstance(history, dict) else []
        if isinstance(series, list) and series:
            bar_y = y + 40
            bar_h = 34
            gap = 5
            bar_w = max(8, int((COL_R - COL_L - gap * 6) / 7))
            max_cost = max([float(item.get("tokens") or 0) for item in series if isinstance(item, dict)], default=0) or 1
            for index, item in enumerate(series[:7]):
                cost = float(item.get("tokens") or 0) if isinstance(item, dict) else 0
                intensity = min(1.0, cost / max_cost) if cost > 0 else 0.0
                x1 = COL_L + index * (bar_w + gap)
                x2 = min(COL_R, x1 + bar_w)
                fill_h = max(2, int(bar_h * min(1.0, cost / max_cost))) if cost > 0 else 2
                self._draw_rounded_rect(x1, bar_y, x2, bar_y + bar_h, r=3, fill=Theme.ag_bg, outline="")
                color = self._trend_token_color(intensity, index == 6)
                self._draw_rounded_rect(x1, bar_y + bar_h - fill_h, x2, bar_y + bar_h, r=3, fill=color, outline="")
                c.create_text(
                    (x1 + x2) / 2,
                    bar_y + bar_h + 4,
                    anchor="n",
                    text=trend_chart_day_label(str(item.get("date") or ""), index, min(7, len(series))),
                    font=self._fonts["font_micro"],
                    fill=color if index >= 5 else Theme.text_muted,
                )
                self._add_tooltip(
                    x1,
                    bar_y,
                    x2,
                    bar_y + bar_h + 18,
                    f"{item.get('date', '-')}\n{exact_token_count(cost)} Token\n{int(item.get('requests') or 0):,} calls · {money(item.get('cost', 0))}",
                )
            y += 94
        else:
            y += 46
        c.create_line(COL_L, y, COL_R, y, fill=Theme.border, width=1)

        # ════════════════════════════════════════════════════════
        #  TOP ACCOUNTS
        # ════════════════════════════════════════════════════════
        raw_top = [
            account
            for account in (list(self.state.top_accounts or []) if self.state else [])
            if account_row_available_for_range(account, self._account_range)
        ]
        raw_top = self._filter_account_display_rows(raw_top)
        range_key = {
            "5h": "window_5h",
            "7d": "window_7d",
            "30d": "window_30d",
        }.get(self._account_range)
        range_label = {
            "today": "\u4eca\u65e5",
            "5h": "5h \u989d\u5ea6\u7a97\u53e3",
            "7d": "7d \u989d\u5ea6\u7a97\u53e3",
            "30d": "\u8fd1 30 \u5929",
        }.get(self._account_range, "\u4eca\u65e5")
        if self._account_range == "30d" and not self._needs_server_account_30d():
            top = self._filter_account_display_rows(self._history_account_rows("30d"))
            top.sort(key=lambda row: account_usage_sort_key(row, "30d"))
        elif range_key:
            top = []
            for account in raw_top:
                if self._account_range in {"5h", "7d"} and account.get("is_pool_aggregate"):
                    continue
                window = account.get(range_key)
                if not isinstance(window, dict) or not window:
                    continue
                window_tokens = int(window.get("tokens") or 0)
                window_requests = int(window.get("requests") or 0)
                window_cost = float(window.get("cost") or 0)
                has_quota = bool(window.get("quota_available", window.get("utilization") is not None))
                quota_unlimited = bool(window.get("quota_unlimited"))
                if (
                    window_tokens <= 0
                    and window_requests <= 0
                    and window_cost <= 0
                    and not has_quota
                    and not quota_unlimited
                ):
                    continue
                item = dict(account)
                item["tokens"] = window_tokens
                item["requests"] = window_requests
                item["cost"] = window_cost
                item["utilization"] = window.get("utilization")
                item["remaining_percent"] = window.get("remaining_percent")
                item["resets_at"] = str(window.get("resets_at") or "")
                item["latest_at"] = window.get("latest_at") or account.get("latest_at") or ""
                item["latest_model"] = window.get("latest_model") or account.get("latest_model") or ""
                item["quota_available"] = has_quota
                item["quota_unlimited"] = quota_unlimited
                item["quota_stale"] = bool(window.get("quota_stale"))
                item["quota_reset_unavailable"] = bool(window.get("quota_reset_unavailable"))
                item["quota_idle"] = bool(window.get("quota_idle")) if self._account_range == "5h" else False
                top.append(item)
            if self._account_range == "7d":
                top.extend(self._history_7d_fallback_rows(top))
            top.sort(key=lambda row: account_usage_sort_key(row, self._account_range))
        else:
            top = raw_top

        y += 9
        c.create_text(COL_L, y + 2, anchor="nw", text="\u8d26\u53f7\u7528\u91cf",
                       font=self._fonts["font_section"], fill=Theme.text_primary)
        c.create_text(COL_L + 67, y + 5, anchor="nw", text=f"{len(top)} \u4e2a\u8d26\u53f7",
                      font=self._fonts["font_micro"], fill=Theme.text_muted)

        tab_specs = [
            ("rank_today", "\u4eca\u65e5", "today"),
            ("rank_5h", "5h\u989d\u5ea6", "5h"),
            ("rank_7d", "7d\u989d\u5ea6", "7d"),
            ("rank_30d", "30d", "30d"),
        ]
        tab_w = 44
        tab_gap = 3
        tab_h = 21
        tabs_x = COL_R - (tab_w * len(tab_specs) + tab_gap * (len(tab_specs) - 1))
        self._draw_rounded_rect(tabs_x - 3, y - 3, COL_R + 3, y + tab_h,
                                r=7, fill=Theme.ag_bg, outline=Theme.border)
        for tab_index, (button_name, label, value) in enumerate(tab_specs):
            x1 = tabs_x + tab_index * (tab_w + tab_gap)
            x2 = x1 + tab_w
            self._btn_rects[button_name] = (x1, y - 1, x2, y - 1 + tab_h)
            selected = self._account_range == value
            hovered = self._hover_btn == button_name
            fill = Theme.bg_lift if selected else (Theme.ag_surface_hover if hovered else Theme.ag_bg)
            text_color = Theme.text_primary if selected or hovered else Theme.ag_muted
            self._draw_rounded_rect(x1, y - 1, x2, y - 1 + tab_h, r=5, fill=fill, outline="")
            if selected:
                c.create_line(x1 + 9, y + tab_h - 1, x2 - 9, y + tab_h - 1,
                              fill=Theme.data, width=2)
            c.create_text((x1 + x2) // 2, y + 9, anchor="center", text=label,
                          font=self._fonts["font_tiny"], fill=text_color)
        y += 27
        rank_list_top = y

        if not top:
            if self._loading or not self.state:
                empty_text = "\u6b63\u5728\u8bfb\u53d6\u7528\u91cf\u6570\u636e"
            else:
                empty_text = "\u8be5\u65f6\u95f4\u8303\u56f4\u6682\u65e0\u8d26\u53f7\u8bb0\u5f55" if range_key else "\u6682\u65e0\u7528\u91cf"
            c.create_text(COL_L + 8, y, anchor="nw", text=empty_text,
                          font=self._fonts["font_label"], fill=Theme.text_muted)
        window_mode = self._account_range in {"5h", "7d"}
        row_h = self._account_rank_row_height()
        available_rank_rows = max(1, (H - 44 - y) // row_h)
        max_start_index = max(0, len(top) - available_rank_rows)
        max_scroll = max_start_index * row_h
        self._scroll_limits["accounts"] = max_scroll
        self._scroll_offsets["accounts"] = max(
            0,
            min(int(self._scroll_offsets.get("accounts", 0) or 0), max_scroll),
        )
        first_index = min(max_start_index, self._scroll_offsets["accounts"] // row_h)
        display_top = list(enumerate(top[first_index:first_index + available_rank_rows], start=first_index))
        for index, acc in display_top:
            health_badge = str(acc.get("health_badge") or "")
            source_badge = str(acc.get("source_badge") or "")
            speed_badge = str(acc.get("speed_badge") or "")
            source_label = account_type_label(acc, acc.get("name"))
            if not source_label and source_badge == "SUB":
                source_label = "SUB2"
            source_w = self._text_width(source_label, "font_micro") + 14 if source_label else 0
            speed_w = self._text_width(speed_badge, "font_micro") + 14 if speed_badge else 0
            badges_w = (source_w + 7 if source_label else 0) + (speed_w + 7 if speed_badge else 0)
            name_x = COL_L + 8 + badges_w
            metric_start_x = COL_R - (76 if window_mode else 150)
            name_max_w = max(60, metric_start_x - name_x - 6)
            name = self._truncate(
                ranking_account_display_name(str(acc.get("name") or "-")),
                "font_label",
                name_max_w,
            )
            tokens = compact_number(acc.get("tokens", 0))
            reqs = compact_number(acc.get("requests", 0))
            cost = money(acc.get("cost", 0))
            utilization = acc.get("utilization")
            remaining_percent = acc.get("remaining_percent")
            if remaining_percent is None and utilization is not None:
                try:
                    remaining_percent = max(0.0, min(100.0, 100.0 - float(utilization)))
                except (TypeError, ValueError):
                    remaining_percent = None
            quota_available = bool(acc.get("quota_available")) if window_mode else False
            quota_unlimited = bool(acc.get("quota_unlimited")) if window_mode else False
            quota_stale = bool(acc.get("quota_stale")) if window_mode else False
            quota_reset_unavailable = bool(acc.get("quota_reset_unavailable")) if window_mode else False
            quota_idle = bool(acc.get("quota_idle")) if window_mode else False
            historical_fallback = bool(acc.get("historical_fallback")) if window_mode else False
            cycle_window = acc.get("window_cycle") if isinstance(acc.get("window_cycle"), dict) else {}
            has_cycle_quota = bool(cycle_window.get("quota_available"))
            if window_mode and quota_unlimited:
                bar_color = Theme.accent_green
            elif window_mode and quota_available:
                bar_color = quota_color(utilization)
            else:
                bar_color = Theme.amber if index == 0 else (Theme.cyan if index == 1 else (Theme.violet if index == 2 else Theme.blue))

            self._draw_rounded_rect(COL_L, y - 3, COL_R, y + row_h - 5, r=6,
                                    fill=Theme.ag_surface, outline=Theme.ag_border)
            marker_bottom = y + (42 if window_mode else 23)
            c.create_rectangle(COL_L, y + 2, COL_L + 3, marker_bottom, fill=bar_color, outline="")
            if source_label:
                self._draw_health_badge(COL_L + 8, y + 1, source_label)
            if speed_badge:
                speed_x = COL_L + 8 + (source_w + 4 if source_label else 0)
                self._draw_health_badge(speed_x, y + 1, speed_badge)
            c.create_text(name_x, y, anchor="nw", text=name,
                          font=self._fonts["font_label"], fill=Theme.text_primary)

            if window_mode:
                if quota_unlimited:
                    percentage_text = "无5h限制"
                elif quota_idle:
                    percentage_text = "\u6ee1\u989d"
                elif quota_available:
                    try:
                        percent_value = float(utilization)
                        percentage_text = f"\u5df2\u7528 {percent_value:.0f}%"
                    except (TypeError, ValueError):
                        percentage_text = "--%"
                elif has_cycle_quota and self._account_range in {"5h", "7d"}:
                    percentage_text = "\u5468\u671f\u8d26\u53f7"
                elif historical_fallback:
                    percentage_text = "\u8fd17\u65e5"
                else:
                    percentage_text = "\u6682\u65e0\u989d\u5ea6"
                percentage_color = (
                    Theme.accent_green
                    if quota_unlimited
                    else (Theme.text_muted if quota_stale or not quota_available else bar_color)
                )
                if has_cycle_quota and not quota_available and self._account_range in {"5h", "7d"}:
                    percentage_color = Theme.amber_bright
                if quota_unlimited:
                    percentage_fill = Theme.quota_green_bg
                    percentage_outline = Theme.accent_green
                elif quota_stale or not quota_available:
                    percentage_fill = Theme.bg_lift
                    percentage_outline = Theme.border
                elif percentage_color == Theme.accent_red:
                    percentage_fill = Theme.quota_red_bg
                    percentage_outline = Theme.accent_red
                elif percentage_color in {Theme.ag_warn, Theme.amber_bright}:
                    percentage_fill = Theme.quota_amber_bg
                    percentage_outline = Theme.ag_warn
                else:
                    percentage_fill = Theme.quota_green_bg
                    percentage_outline = Theme.accent_green
                pill_w = self._text_width(percentage_text, "font_label_bold") + 15
                pill_x1 = COL_R - max(56, pill_w)
                pill_x2 = COL_R - 2
                self._draw_rounded_rect(pill_x1, y - 2, pill_x2, y + 18,
                                        r=6, fill=percentage_fill, outline=percentage_outline, width=1)
                c.create_text((pill_x1 + pill_x2) // 2, y + 8, anchor="center", text=percentage_text,
                              font=self._fonts["font_label_bold"], fill=percentage_color)

                metric_text = f"{tokens} Token  \u00b7  {cost}"
                c.create_text(name_x, y + 20, anchor="nw", text=metric_text,
                              font=self._fonts["font_label_bold"], fill=Theme.cyan)
                if health_badge:
                    right_detail = health_badge
                    right_color = self._health_color(health_badge)
                elif quota_unlimited:
                    right_detail = f"最近 5h · {reqs} 次"
                    right_color = Theme.accent_green
                elif quota_stale:
                    right_detail = "\u989d\u5ea6\u5f85\u5237\u65b0"
                    right_color = Theme.amber_bright
                elif quota_idle:
                    right_detail = "\u6ee1\u989d\u5f85\u4f7f\u7528"
                    right_color = Theme.accent_green
                elif has_cycle_quota and not quota_available and self._account_range in {"5h", "7d"}:
                    right_detail = "\u770b\u5468\u671f\u9875"
                    right_color = Theme.amber_bright
                elif historical_fallback:
                    right_detail = f"\u5386\u53f2 \u00b7 {reqs} \u6b21"
                    right_color = Theme.text_muted
                elif not quota_available:
                    right_detail = "\u65e0\u8be5\u7a97\u53e3\u989d\u5ea6"
                    right_color = Theme.text_muted
                else:
                    try:
                        remaining_text = f"{float(remaining_percent):.0f}%"
                    except (TypeError, ValueError):
                        remaining_text = "--%"
                    right_detail = f"\u5269\u4f59 {remaining_text} \u00b7 {reqs} \u6b21"
                    right_color = Theme.text_muted
                c.create_text(COL_R - 4, y + 20, anchor="ne", text=right_detail,
                              font=self._fonts["font_micro"], fill=right_color)

                if quota_unlimited:
                    reset_text = "无5h限制 · 最近5小时分析"
                elif quota_available:
                    if quota_idle:
                        reset_text = "\u9996\u6b21\u4f7f\u7528\u540e\u5f00\u59cb 5h \u5012\u8ba1\u65f6"
                    elif quota_reset_unavailable:
                        reset_text = "\u91cd\u7f6e\u65f6\u95f4\u5f85\u540c\u6b65"
                    else:
                        reset_text = quota_reset_text(str(acc.get("resets_at") or ""))
                    if self._account_range == "7d" and reset_text:
                        reset_text = f"\u5468\u9650\u989d \u00b7 {reset_text}"
                    elif self._account_range == "cycle" and reset_text:
                        reset_text = f"\u5468\u671f \u00b7 {reset_text}"
                    elif self._account_range == "5h" and reset_text and not quota_idle:
                        reset_text = f"5h \u9650\u989d \u00b7 {reset_text}"
                elif historical_fallback:
                    reset_text = "\u65e0\u5b98\u65b9\u5468\u989d\u5ea6 \u00b7 \u8fd17\u65e5\u6c47\u603b"
                elif self._account_range == "7d":
                    reset_text = "\u8be5\u8d26\u53f7\u4f7f\u7528\u5468\u671f\u989d\u5ea6" if has_cycle_quota else "\u672a\u63d0\u4f9b\u5468\u989d\u5ea6"
                elif self._account_range == "cycle":
                    reset_text = "\u672a\u63d0\u4f9b\u5468\u671f\u989d\u5ea6"
                else:
                    reset_text = "\u6682\u65e0\u989d\u5ea6\u6570\u636e"
                reset_w = self._text_width(reset_text, "font_micro") if reset_text else 0
                progress_x1 = name_x
                progress_x2 = max(progress_x1 + 42, COL_R - reset_w - 14)
                progress_y = y + 41
                if not quota_unlimited:
                    self._draw_rounded_rect(progress_x1, progress_y, progress_x2, progress_y + 4,
                                            r=2, fill=Theme.bg_lift, outline="")
                if quota_available and not quota_unlimited:
                    try:
                        ratio = 0.0 if quota_idle else max(0.0, min(1.0, float(utilization) / 100.0))
                    except (TypeError, ValueError):
                        ratio = 0.0
                    if ratio > 0:
                        fill_x2 = progress_x1 + max(3, int((progress_x2 - progress_x1) * ratio))
                        self._draw_rounded_rect(progress_x1, progress_y, fill_x2, progress_y + 4,
                                                r=2, fill=bar_color, outline="")
                if reset_text:
                    c.create_text(COL_R - 4, y + 33, anchor="ne", text=reset_text,
                                  font=self._fonts["font_micro"], fill=Theme.text_muted)
                divider_y = y + min(48, row_h - 5)
                c.create_line(COL_L + 8, divider_y, COL_R - 4, divider_y, fill=Theme.border, width=1)
            else:
                cost_w = self._text_width(cost, "font_label_bold")
                c.create_text(COL_R - 4, y, anchor="ne", text=cost,
                              font=self._fonts["font_label_bold"], fill=Theme.amber_bright)
                c.create_text(COL_R - 12 - cost_w, y, anchor="ne", text=f"{tokens} Token",
                              font=self._fonts["font_label_bold"], fill=bar_color)

                detail_text = f"{range_label}  \u00b7  {reqs} \u6b21\u8bf7\u6c42"
                c.create_text(name_x, y + 15, anchor="nw", text=detail_text,
                              font=self._fonts["font_micro"], fill=Theme.text_muted)
                if health_badge:
                    c.create_text(COL_R - 4, y + 15, anchor="ne", text=health_badge,
                                  font=self._fonts["font_micro"], fill=self._health_color(health_badge))
                c.create_line(COL_L + 8, y + 27, COL_R - 4, y + 27, fill=Theme.border, width=1)
            y += row_h

        self._draw_list_scrollbar(
            "accounts",
            COL_R - 1,
            rank_list_top - 3,
            min(H - 44, rank_list_top + len(display_top) * row_h - 5),
            len(display_top),
            len(top),
            max_scroll,
        )

        self._draw_footer(W, H)
        self._draw_tooltip(W, H)

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    #  ANIMATION
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _fade_in(self) -> None:
        if self.closed:
            return
        if self._fade_alpha < self.WINDOW_ALPHA:
            self._fade_alpha = min(self._fade_alpha + 0.06, self.WINDOW_ALPHA)
            self.root.attributes("-alpha", self._fade_alpha)
            self.root.after(16, self._fade_in)
        else:
            self.root.attributes("-alpha", self.WINDOW_ALPHA)

    def _pulse_tick(self) -> None:
        if self.closed:
            self._pulse_tick_scheduled = False
            return
        flow_level, _recent_tokens = self._token_flow_snapshot()
        has_recent_samples = bool(getattr(self, "_token_flow_samples", []))
        meter_level = float(getattr(self, "_token_flow_meter_display_level", 0.0))
        meter_animating = self._main_tab == "stats" and meter_level > 0.01
        badge_animating = self._main_tab == "stats" and (
            self._token_delta_badge_visual()[2]
            or self._cost_delta_badge_visual()[2]
        )
        if (
            not self._loading
            and flow_level <= 0.01
            and not meter_animating
            and not badge_animating
            and not has_recent_samples
        ):
            if self._main_tab != "stats":
                self._token_flow_meter_display_level = 0.0
            self._pulse_tick_scheduled = False
            self._draw()
            return
        self._pulse_phase += 0.32
        redrawn = False
        if not self._loading:
            redrawn = any((
                self._redraw_token_flow_trace(),
                self._redraw_token_flow_meter(),
                self._redraw_token_delta_badge(),
                self._redraw_cost_delta_badge(),
            ))
        if redrawn:
            interval_ms = TOKEN_FLOW_ANIMATION_INTERVAL_MS
        else:
            self._draw()
            interval_ms = TOKEN_FLOW_FULL_REDRAW_INTERVAL_MS
        self.root.after(interval_ms, self._pulse_tick)

    def _ensure_pulse_animation(self) -> None:
        if (
            getattr(self, "closed", False)
            or not hasattr(self, "root")
            or getattr(self, "_pulse_tick_scheduled", False)
        ):
            return
        self._pulse_tick_scheduled = True
        self.root.after(0, self._pulse_tick)

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    #  DRAG
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    @staticmethod
    def _point_in_rect(x: int, y: int, rect: tuple[int, int, int, int] | None) -> bool:
        if rect is None:
            return False
        x1, y1, x2, y2 = rect
        return x1 <= x <= x2 and y1 <= y <= y2

    def _scrollbar_tab_at(self, x: int, y: int) -> str | None:
        if self._main_tab == "accounts":
            candidates = ("active", "accounts")
        elif self._main_tab == "stats":
            candidates = ("stats",)
        else:
            candidates = ()
        for tab in candidates:
            if self._point_in_rect(x, y, self._list_scrollbar_tracks.get(tab)):
                return tab
        return None

    def _set_list_scroll_from_thumb(self, tab: str, thumb_top: int) -> None:
        track = self._list_scrollbar_tracks.get(tab)
        thumb = self._list_scrollbar_thumbs.get(tab)
        limit = int(self._scroll_limits.get(tab, 0) or 0)
        if track is None or thumb is None or limit <= 0:
            return
        _x1, track_top, _x2, track_bottom = track
        thumb_h = max(1, thumb[3] - thumb[1])
        travel = max(0, track_bottom - track_top - thumb_h)
        if travel <= 0:
            self._scroll_offsets[tab] = 0
            return
        clamped_top = max(track_top, min(track_bottom - thumb_h, int(thumb_top)))
        ratio = (clamped_top - track_top) / travel
        self._scroll_offsets[tab] = max(0, min(limit, int(round(limit * ratio))))

    def _hit_button(self, x: int, y: int) -> str | None:
        for name, (x1, y1, x2, y2) in self._btn_rects.items():
            if name.startswith("main_") and x1 <= x <= x2 and y1 - 8 <= y <= y2 + 8:
                return name
            if x1 <= x <= x2 and y1 <= y <= y2:
                return name
        return None

    def _hit_resize_handle(self, x: int, y: int) -> bool:
        return x >= self.WIDTH - 24 and y >= self.HEIGHT - 24

    def _on_press(self, event: tk.Event) -> None:
        scrollbar_tab = self._scrollbar_tab_at(event.x, event.y)
        if scrollbar_tab:
            self._resizing = False
            self._list_scrollbar_drag_tab = scrollbar_tab
            thumb = self._list_scrollbar_thumbs.get(scrollbar_tab)
            if self._point_in_rect(event.x, event.y, thumb) and thumb is not None:
                self._list_scrollbar_drag_offset = int(event.y - thumb[1])
            elif thumb is not None:
                self._list_scrollbar_drag_offset = max(0, (thumb[3] - thumb[1]) // 2)
                self._set_list_scroll_from_thumb(
                    scrollbar_tab,
                    int(event.y - self._list_scrollbar_drag_offset),
                )
                self._draw()
            return
        btn = self._hit_button(event.x, event.y)
        if btn == "btn_close":
            self._resizing = False
            self.close_app()
            return
        if btn == "btn_pin":
            self._resizing = False
            self._pinned = not self._pinned
            self.root.attributes("-topmost", self._pinned)
            self._draw()
            return
        if btn == "btn_refresh":
            self._resizing = False
            self.refresh_async(force=True)
            return
        if btn in {"main_accounts", "main_stats"}:
            self._resizing = False
            self._main_tab = {
                "main_accounts": "accounts",
                "main_stats": "stats",
            }[btn]
            self._scroll_offsets[self._main_tab] = 0
            if self._main_tab == "accounts":
                self._scroll_offsets["active"] = 0
            self._draw()
            return
        if 56 <= event.y <= 96 and 14 <= event.x <= self.WIDTH - 14:
            self._resizing = False
            tab_width = max(1, (self.WIDTH - 28) / 2)
            tab_index = int(max(0, min(1, (event.x - 14) // tab_width)))
            tab_value = ("accounts", "stats")[tab_index]
            if self._main_tab != tab_value:
                self._main_tab = tab_value
                self._scroll_offsets[tab_value] = 0
                if tab_value == "accounts":
                    self._scroll_offsets["active"] = 0
                self._draw()
            return
        if btn in {"usage_range_24h", "usage_range_7d", "usage_range_30d", "usage_range_all"}:
            self._resizing = False
            self._usage_range = btn.replace("usage_range_", "")
            self._scroll_offsets["stats"] = 0
            if self._usage_range == "all" and not self.client.include_history_details:
                self.client.include_history_details = True
                self.client.clear_client_usage_cache()
                self.refresh_async()
            else:
                self._draw()
            return
        if btn in {"rank_today", "rank_5h", "rank_7d", "rank_30d"}:
            self._resizing = False
            self._account_range = {
                "rank_today": "today",
                "rank_5h": "5h",
                "rank_7d": "7d",
                "rank_30d": "30d",
            }[btn]
            self._account_range_user_selected = True
            self._scroll_offsets["accounts"] = 0
            if (
                self._account_range == "30d"
                and self._needs_server_account_30d()
                and not self.client.include_account_30d
            ):
                self.client.include_account_30d = True
                self.client.clear_client_usage_cache()
                self.refresh_async()
            else:
                self._draw()
            return
        if self._hit_resize_handle(event.x, event.y):
            self._resizing = True
            self._resize_data = {"x": event.x_root, "y": event.y_root, "w": self.WIDTH, "h": self.HEIGHT}
            return
        self._resizing = False
        self._drag_data["x"] = event.x
        self._drag_data["y"] = event.y

    def _on_release(self, _event: tk.Event) -> None:
        self._resizing = False
        if self._list_scrollbar_drag_tab is not None:
            self._list_scrollbar_drag_tab = None
            self._draw()

    def _on_drag(self, event: tk.Event) -> None:
        if self._list_scrollbar_drag_tab is not None:
            self._set_list_scroll_from_thumb(
                self._list_scrollbar_drag_tab,
                int(event.y - self._list_scrollbar_drag_offset),
            )
            self._draw()
            return
        if self._resizing:
            new_w = max(self.MIN_WIDTH, self._resize_data["w"] + event.x_root - self._resize_data["x"])
            new_h = max(self.MIN_HEIGHT, self._resize_data["h"] + event.y_root - self._resize_data["y"])
            self._apply_window_size(int(new_w), int(new_h))
            self._draw()
            return
        dx = event.x - self._drag_data["x"]
        dy = event.y - self._drag_data["y"]
        x = self.root.winfo_x() + dx
        y = self.root.winfo_y() + dy
        self.root.geometry(f"+{x}+{y}")

    def _on_motion(self, event: tk.Event) -> None:
        if self._list_scrollbar_drag_tab is not None or self._scrollbar_tab_at(
            event.x, event.y
        ) is not None:
            self.canvas.configure(cursor="sb_v_double_arrow")
        elif self._hit_resize_handle(event.x, event.y):
            self.canvas.configure(cursor="size_nw_se")
        else:
            self.canvas.configure(cursor="")
        btn = self._hit_button(event.x, event.y)
        tooltip = self._hit_tooltip(event.x, event.y)
        tooltip_pos = (int(event.x), int(event.y))
        if btn != self._hover_btn or tooltip != self._tooltip_text or (tooltip and tooltip_pos != self._tooltip_pos):
            self._hover_btn = btn
            self._tooltip_text = tooltip
            self._tooltip_pos = tooltip_pos
            self._draw()

    def _on_leave(self, _event: tk.Event) -> None:
        self.canvas.configure(cursor="")
        if self._hover_btn is not None or self._tooltip_text:
            self._hover_btn = None
            self._tooltip_text = ""
            self._draw()

    def _on_configure(self, event: tk.Event) -> None:
        if self._ignore_configure:
            return
        width = int(getattr(event, "width", self.WIDTH) or self.WIDTH)
        height = int(getattr(event, "height", self.HEIGHT) or self.HEIGHT)
        if width <= 50 or height <= 50:
            return
        if self._resizing:
            self.WIDTH = max(self.MIN_WIDTH, width)
            self.HEIGHT = max(self.MIN_HEIGHT, height)
            return
        if width != self.WIDTH or height != self.HEIGHT:
            self._apply_window_size(self.WIDTH, self.HEIGHT)

    def _on_mousewheel(self, event: tk.Event) -> None:
        if getattr(event, "num", None) == 4:
            delta = -1
        elif getattr(event, "num", None) == 5:
            delta = 1
        else:
            wheel_delta = int(getattr(event, "delta", 0) or 0)
            if wheel_delta == 0:
                return
            delta = -1 if wheel_delta > 0 else 1

        if self._main_tab == "accounts" and self._active_scroll_rect is not None:
            x1, y1, x2, y2 = self._active_scroll_rect
            if x1 <= event.x <= x2 and y1 <= event.y <= y2:
                active_limit = int(self._scroll_limits.get("active", 0) or 0)
                if active_limit <= 0:
                    return
                active_current = int(self._scroll_offsets.get("active", 0) or 0)
                self._scroll_offsets["active"] = max(
                    0,
                    min(active_limit, active_current + delta * 26),
                )
                self._draw()
                return

        tab = self._main_tab
        limit = int(self._scroll_limits.get(tab, 0) or 0)
        if limit <= 0:
            return
        current = int(self._scroll_offsets.get(tab, 0) or 0)
        if tab == "accounts":
            step = self._account_rank_row_height()
        else:
            step = 48
        self._scroll_offsets[tab] = max(0, min(limit, current + delta * step))
        self._draw()

    def _on_focus_in(self, _event: tk.Event) -> None:
        self._ensure_topmost()

    def _on_visibility(self, _event: tk.Event) -> None:
        self._ensure_topmost()

    def _refresh_live_active_async(self) -> bool:
        if not self._live_active_lock.acquire(blocking=False):
            return False
        cached_sessions: list[dict[str, Any]] = []
        if self.state is not None and isinstance(self.state.client_usage, dict):
            raw_sessions = self.state.client_usage.get("active_sessions")
            if isinstance(raw_sessions, list):
                cached_sessions = [
                    dict(row) for row in raw_sessions if isinstance(row, dict)
                ]

        def _worker() -> None:
            try:
                sessions = scan_live_codex_active_sessions(
                    Path.home() / ".codex" / "sessions",
                    cached_sessions,
                    tail_cache=self._live_active_tail_cache,
                )
            except Exception:
                sessions = None
            try:
                self.root.after(0, lambda: self._apply_live_active_sessions(sessions))
            except tk.TclError:
                self._live_active_lock.release()

        try:
            self._live_active_executor.submit(_worker)
        except RuntimeError:
            self._live_active_lock.release()
            return False
        return True

    def _apply_live_active_sessions(
        self,
        sessions: list[dict[str, Any]] | None,
    ) -> None:
        try:
            if (
                sessions is None
                or self.state is None
                or self.state.usage_source != "local"
                or not isinstance(self.state.client_usage, dict)
            ):
                return
            self.state.client_usage["active_sessions"] = sessions
            self.state.active_accounts = local_active_accounts_from_client_usage(
                self.state.client_usage
            )
            self._draw()
        finally:
            self._live_active_lock.release()

    def _schedule_live_active_refresh(self) -> None:
        if self.closed:
            return
        self._refresh_live_active_async()
        self.root.after(REFRESH_SECONDS * 1000, self._schedule_live_active_refresh)

    def _capture_auth_switch(self, *, refresh_active: bool = True) -> bool:
        identity, _source_path, changed_at = current_codex_auth_snapshot()
        if not identity or identity == self._last_auth_identity:
            return False
        self._last_auth_identity = identity
        append_codex_auth_switch_event(identity, changed_at)
        if refresh_active:
            self._refresh_live_active_async()
        return True

    def _schedule_auth_switch_refresh(self) -> None:
        if self.closed:
            return
        self._capture_auth_switch()
        self.root.after(
            AUTH_SWITCH_WATCH_INTERVAL_MS,
            self._schedule_auth_switch_refresh,
        )

    @staticmethod
    def _checkpoint_json_value(value: Any) -> Any:
        if isinstance(value, datetime):
            return value.astimezone(CN_TZ).isoformat(timespec="microseconds")
        if isinstance(value, dict):
            return {
                str(key): FloatingMonitorApp._checkpoint_json_value(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [FloatingMonitorApp._checkpoint_json_value(item) for item in value]
        return value

    def _clear_live_usage_checkpoint(self) -> None:
        try:
            LIVE_USAGE_CHECKPOINT_JSON.unlink(missing_ok=True)
        except OSError:
            pass

    def _persist_live_usage_checkpoint(self, *, force: bool = False) -> bool:
        overlay = getattr(self, "_live_usage_overlay", None)
        if not isinstance(overlay, dict) or not hasattr(self, "_last_live_checkpoint_write_at"):
            return False
        now_clock = time.monotonic()
        if (
            not force
            and now_clock - float(getattr(self, "_last_live_checkpoint_write_at", float("-inf")))
            < LIVE_USAGE_CHECKPOINT_WRITE_SECONDS
        ):
            return False
        payload = {
            "schema": LIVE_USAGE_CHECKPOINT_SCHEMA,
            "date": today_key(),
            "updated_at": datetime.now(CN_TZ).isoformat(timespec="seconds"),
            "overlay": self._checkpoint_json_value(overlay),
        }
        try:
            write_json_atomic(LIVE_USAGE_CHECKPOINT_JSON, payload)
        except OSError:
            return False
        self._last_live_checkpoint_write_at = now_clock
        return True

    def _restore_live_usage_checkpoint(self) -> bool:
        if self.state is None:
            return False
        try:
            payload = json.loads(
                LIVE_USAGE_CHECKPOINT_JSON.read_text(encoding="utf-8", errors="ignore")
            )
        except (OSError, json.JSONDecodeError):
            return False
        if (
            not isinstance(payload, dict)
            or payload.get("schema") != LIVE_USAGE_CHECKPOINT_SCHEMA
            or str(payload.get("date") or "") != today_key()
        ):
            self._clear_live_usage_checkpoint()
            return False
        raw_overlay = payload.get("overlay")
        if not isinstance(raw_overlay, dict):
            return False
        try:
            base_tokens = max(0, int(raw_overlay.get("base_today_tokens") or 0))
            delta_tokens = max(0, int(raw_overlay.get("tokens") or 0))
            base_requests = max(0, int(raw_overlay.get("base_today_requests") or 0))
            delta_requests = max(0, int(raw_overlay.get("requests") or 0))
            base_cost = max(0.0, float(raw_overlay.get("base_today_cost") or 0.0))
            delta_cost = max(0.0, float(raw_overlay.get("cost") or 0.0))
        except (TypeError, ValueError):
            return False
        target_tokens = base_tokens + delta_tokens
        target_requests = base_requests + delta_requests
        target_cost = base_cost + delta_cost
        current_tokens = max(0, int(self.state.today_tokens or 0))
        current_requests = max(0, int(self.state.today_requests or 0))
        current_cost = max(0.0, float(self.state.today_account_cost or 0.0))
        if current_tokens >= target_tokens:
            self._clear_live_usage_checkpoint()
            return False

        overlay = dict(raw_overlay)
        latest_when = _parse_time(str(overlay.get("latest_when") or ""))
        if latest_when is not None:
            overlay["latest_when"] = latest_when
        providers = overlay.get("providers")
        if isinstance(providers, dict):
            for target in providers.values():
                if not isinstance(target, dict):
                    continue
                parsed = _parse_time(str(target.get("latest_when") or ""))
                if parsed is not None:
                    target["latest_when"] = parsed

        exact_base = current_tokens == base_tokens and current_requests == base_requests
        if not exact_base:
            remaining_tokens = max(0, target_tokens - current_tokens)
            ratio = remaining_tokens / delta_tokens if delta_tokens > 0 else 0.0
            overlay.update(
                {
                    "base_today_tokens": current_tokens,
                    "base_today_requests": current_requests,
                    "base_today_cost": current_cost,
                    "base_authoritative_tokens": int(
                        (self.state.client_usage or {}).get("tokens") or current_tokens
                    ) if isinstance(self.state.client_usage, dict) else current_tokens,
                    "tokens": remaining_tokens,
                    "requests": max(0, target_requests - current_requests),
                    "cost": max(0.0, target_cost - current_cost),
                    "input_tokens": int(max(0, int(overlay.get("input_tokens") or 0)) * ratio),
                    "cached_input_tokens": int(max(0, int(overlay.get("cached_input_tokens") or 0)) * ratio),
                    "output_tokens": int(max(0, int(overlay.get("output_tokens") or 0)) * ratio),
                    "providers": {},
                }
            )
        self._live_usage_overlay = overlay
        self._apply_live_usage_overlay(self.state)
        self._last_live_checkpoint_write_at = time.monotonic()
        return True

    def _live_usage_catchup_since(self) -> datetime | None:
        if self.state is None or self.state.usage_source != "local":
            return None
        candidates: list[tuple[datetime, bool]] = []
        overlay = self._live_usage_overlay
        if isinstance(overlay, dict):
            parsed = overlay.get("latest_when")
            if isinstance(parsed, datetime):
                candidates.append((parsed, False))
        client_usage = self.state.client_usage if isinstance(self.state.client_usage, dict) else {}
        scan_status = client_usage.get("scan_status") if isinstance(client_usage.get("scan_status"), dict) else {}
        for value in (
            (self.state.latest_request or {}).get("created_at") if isinstance(self.state.latest_request, dict) else "",
            (client_usage.get("latest_request") or {}).get("created_at")
            if isinstance(client_usage.get("latest_request"), dict)
            else "",
            scan_status.get("through"),
            client_usage.get("updated_at"),
        ):
            raw_value = str(value or "")
            try:
                local_value = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
                if local_value.tzinfo is None:
                    local_value = local_value.replace(tzinfo=CN_TZ)
                parsed = local_value.astimezone(timezone.utc)
            except (TypeError, ValueError):
                parsed = None
            if parsed is not None:
                has_fraction = bool(re.search(r"T\d{2}:\d{2}:\d{2}\.\d+", raw_value))
                candidates.append((parsed, not has_fraction))
        now = datetime.now(CN_TZ)
        day_start = datetime.combine(now.date(), datetime.min.time(), tzinfo=CN_TZ)
        if candidates:
            since, second_precision = max(candidates, key=lambda item: item[0])
            if second_precision:
                since += timedelta(seconds=1)
        else:
            since = day_start
        if since < day_start:
            return day_start
        return min(since, now)

    def _complete_live_usage_verification(self, through: datetime | None) -> bool:
        if not bool(getattr(self, "_live_usage_verification_pending", False)):
            return False
        if through is None:
            return False
        pending_latest = getattr(self, "_live_usage_verification_latest_when", None)
        if isinstance(pending_latest, datetime) and through < pending_latest:
            return False
        self._live_usage_verification_pending = False
        self._live_usage_verification_latest_when = None
        self._live_usage_verification_pending_tokens = 0
        samples = getattr(self, "_live_usage_rate_samples", None)
        if isinstance(samples, list):
            samples.clear()
        return True

    def _apply_live_usage_catchup(self, payload: dict[str, Any] | None) -> None:
        try:
            if self.closed or not isinstance(payload, dict):
                return
            summary = payload.get("summary")
            if not isinstance(summary, dict) or self.state is None:
                return
            through = _parse_time(str(payload.get("through") or ""))
            if through is None:
                return

            seen_ids = getattr(self, "_live_usage_seen_ids", None)
            if not isinstance(seen_ids, dict):
                seen_ids = {}
                self._live_usage_seen_ids = seen_ids
            payload_event_ids: set[str] = set()
            rows = payload.get("events")
            for row in rows if isinstance(rows, list) else []:
                if isinstance(row, dict) and row.get("event_id"):
                    event_id = str(row["event_id"])
                    payload_event_ids.add(event_id)
                    seen_ids[event_id] = None

            provider_targets: dict[str, dict[str, Any]] = {}
            for provider in payload.get("providers") if isinstance(payload.get("providers"), list) else []:
                if not isinstance(provider, dict):
                    continue
                name = str(provider.get("name") or "").strip()
                if name:
                    provider_targets[name] = dict(provider)

            target = {
                "tokens": max(0, int(summary.get("tokens") or 0)),
                "requests": max(0, int(summary.get("requests") or 0)),
                "cost": max(0.0, float(summary.get("cost") or 0.0)),
                "input_tokens": max(0, int(summary.get("input_tokens") or 0)),
                "cached_input_tokens": max(0, int(summary.get("cached_input_tokens") or 0)),
                "output_tokens": max(0, int(summary.get("output_tokens") or 0)),
            }
            latest_when = _parse_time(str(summary.get("latest_at") or ""))
            latest_provider = ""
            latest_model = str(summary.get("latest_model") or "")
            records = getattr(self, "_live_usage_event_records", {})
            tail_tokens = 0
            for event_id, event in records.items() if isinstance(records, dict) else []:
                if event_id in payload_event_ids:
                    continue
                when = event.get("when")
                if not isinstance(when, datetime) or when < through:
                    continue
                raw_input = max(0, int(event.get("input_tokens") or 0))
                cached = min(raw_input, max(0, int(event.get("cached_tokens") or 0)))
                output = max(0, int(event.get("output_tokens") or 0))
                tokens = max(0, int(event.get("total_tokens") or 0))
                event_cost = max(0.0, float(event.get("cost") or 0.0))
                target["tokens"] += tokens
                tail_tokens += tokens
                target["requests"] += 1
                target["cost"] += event_cost
                target["input_tokens"] += max(0, raw_input - cached)
                target["cached_input_tokens"] += cached
                target["output_tokens"] += output
                provider_name = str(event.get("provider") or "").strip()
                if provider_name:
                    provider = provider_targets.setdefault(
                        provider_name,
                        {
                            "name": provider_name,
                            "tokens": 0,
                            "requests": 0,
                            "cost": 0.0,
                            "input_tokens": 0,
                            "cached_input_tokens": 0,
                            "output_tokens": 0,
                        },
                    )
                    provider["tokens"] = int(provider.get("tokens") or 0) + tokens
                    provider["requests"] = int(provider.get("requests") or 0) + 1
                    provider["cost"] = float(provider.get("cost") or 0.0) + event_cost
                    provider["input_tokens"] = int(provider.get("input_tokens") or 0) + max(0, raw_input - cached)
                    provider["cached_input_tokens"] = int(provider.get("cached_input_tokens") or 0) + cached
                    provider["output_tokens"] = int(provider.get("output_tokens") or 0) + output
                    provider["latest_at"] = when.astimezone(CN_TZ).isoformat(timespec="seconds")
                if latest_when is None or when > latest_when:
                    latest_when = when
                    latest_provider = provider_name
                    latest_model = str(event.get("model") or latest_model)

            client_usage = self.state.client_usage if isinstance(self.state.client_usage, dict) else {}
            authoritative_tokens = max(0, int(client_usage.get("tokens") or 0))
            authoritative_requests = max(0, int(client_usage.get("requests") or 0))
            authoritative_cost = max(0.0, float(client_usage.get("cost") or 0.0))
            self.state.today_tokens = authoritative_tokens
            self.state.today_requests = authoritative_requests
            self.state.today_account_cost = authoritative_cost
            overlay: dict[str, Any] = {
                "base_today_tokens": authoritative_tokens,
                "base_today_requests": authoritative_requests,
                "base_today_cost": authoritative_cost,
                "base_authoritative_tokens": authoritative_tokens,
                "base_updated_at": str(client_usage.get("updated_at") or ""),
                "tokens": max(0, target["tokens"] - authoritative_tokens),
                "requests": max(0, target["requests"] - authoritative_requests),
                "cost": max(0.0, target["cost"] - authoritative_cost),
                "input_tokens": 0,
                "cached_input_tokens": 0,
                "output_tokens": 0,
                "latest_when": latest_when or through,
                "providers": {},
                "catchup_summary_tokens": int(summary.get("tokens") or 0),
                "catchup_tail_tokens": tail_tokens,
                "catchup_through": through,
            }
            raw_providers = client_usage.get("providers") if isinstance(client_usage.get("providers"), list) else []
            top_accounts = self.state.top_accounts if isinstance(self.state.top_accounts, list) else []
            for provider_name, desired in provider_targets.items():
                provider_key = account_display_key(provider_name)
                raw_row = next(
                    (
                        row for row in raw_providers
                        if isinstance(row, dict) and account_display_key(row.get("name")) == provider_key
                    ),
                    {},
                )
                top_row = next(
                    (
                        row for row in top_accounts
                        if isinstance(row, dict) and account_display_key(row.get("name")) == provider_key
                    ),
                    {},
                )
                desired_tokens = max(0, int(desired.get("tokens") or 0))
                desired_requests = max(0, int(desired.get("requests") or 0))
                desired_cost = max(0.0, float(desired.get("cost") or 0.0))
                desired_values = {
                    "tokens": desired_tokens,
                    "requests": desired_requests,
                    "cost": desired_cost,
                    "input_tokens": max(0, int(desired.get("input_tokens") or 0)),
                    "cached_input_tokens": max(0, int(desired.get("cached_input_tokens") or 0)),
                    "output_tokens": max(0, int(desired.get("output_tokens") or 0)),
                }
                if isinstance(raw_row, dict):
                    raw_row.update(desired_values)
                    raw_row["latest_at"] = str(desired.get("latest_at") or raw_row.get("latest_at") or "")
                if isinstance(top_row, dict):
                    top_row["tokens"] = desired_tokens
                    top_row["requests"] = desired_requests
                    top_row["cost"] = desired_cost
                    top_row["latest_at"] = str(desired.get("latest_at") or top_row.get("latest_at") or "")

            self._live_usage_overlay = overlay
            self._apply_live_usage_overlay(self.state)
            self._complete_live_usage_verification(through)
            if latest_when is not None:
                self.state.latest_request = {
                    "kind": "success",
                    "model": latest_model or "-",
                    "created_at": latest_when.astimezone(CN_TZ).isoformat(timespec="seconds"),
                    "source": "CLIENT",
                }
            if latest_provider:
                self.state.latest_account_name = latest_provider
            self._persist_live_usage_checkpoint(force=True)
            self._last_live_reconcile_at = time.monotonic()
            self._draw()
            if not bool(getattr(self, "_live_initial_recheck_scheduled", False)):
                self._live_initial_recheck_scheduled = True
                self._schedule_live_usage_reconcile(LIVE_USAGE_INITIAL_RECHECK_MS)
        finally:
            try:
                self._live_catchup_lock.release()
            except RuntimeError:
                pass
            if (
                bool(getattr(self, "_live_usage_verification_pending", False))
                and not self.closed
                and hasattr(self, "root")
            ):
                self._schedule_live_usage_reconcile(LIVE_USAGE_VERIFY_DELAY_MS)

    def _refresh_live_usage_catchup_async(self) -> bool:
        since = self._live_usage_catchup_since()
        if since is None or not CLIENT_USAGE_EXPORT.exists():
            return False
        if not self._live_catchup_lock.acquire(blocking=False):
            return False
        through = datetime.now(CN_TZ)
        if through <= since + timedelta(milliseconds=50):
            self._live_catchup_lock.release()
            return False

        def _worker() -> None:
            payload: dict[str, Any] | None = None
            try:
                completed = subprocess.run(
                    client_usage_export_command(
                        "--output",
                        str(CLIENT_USAGE_JSON),
                        "--live-since",
                        since.isoformat(timespec="microseconds"),
                        "--live-through",
                        through.isoformat(timespec="microseconds"),
                    ),
                    cwd=str(APP_DIR),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    timeout=CLIENT_USAGE_EXPORT_TIMEOUT_SECONDS,
                    check=False,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
                lines = [line for line in str(completed.stdout or "").splitlines() if line.strip()]
                if completed.returncode == 0 and lines:
                    candidate = json.loads(lines[-1])
                    if isinstance(candidate, dict):
                        payload = candidate
            except (OSError, ValueError, json.JSONDecodeError, subprocess.TimeoutExpired):
                payload = None
            try:
                self.root.after(0, lambda: self._apply_live_usage_catchup(payload))
            except tk.TclError:
                try:
                    self._live_catchup_lock.release()
                except RuntimeError:
                    pass

        try:
            self._live_catchup_executor.submit(_worker)
        except RuntimeError:
            self._live_catchup_lock.release()
            return False
        return True

    def _refresh_live_usage_async(self) -> bool:
        if not self._live_usage_lock.acquire(blocking=False):
            return False

        def _worker() -> None:
            try:
                events = self._live_usage_watcher.poll_events()
                changed = self._live_usage_watcher.token_count_changed
            except Exception:
                events = []
                changed = False
            try:
                self.root.after(0, lambda: self._apply_live_usage_change(changed, events))
            except tk.TclError:
                self._live_usage_lock.release()

        try:
            self._live_usage_executor.submit(_worker)
        except RuntimeError:
            self._live_usage_lock.release()
            return False
        return True

    def _live_event_request_context(self, event: dict[str, Any]) -> tuple[str, str]:
        session_id = str(event.get("session_id") or "")
        provider = ""
        model = str(event.get("model") or "")
        client_usage = self.state.client_usage if self.state and isinstance(self.state.client_usage, dict) else {}
        sessions = client_usage.get("active_sessions")
        for session in sessions if isinstance(sessions, list) else []:
            if not isinstance(session, dict) or str(session.get("session_id") or "") != session_id:
                continue
            provider = _concrete_live_provider(session.get("provider"))
            model = model or str(session.get("model") or "")
            break
        if not provider and self.state:
            for account in self.state.active_accounts or []:
                if not isinstance(account, dict):
                    continue
                candidate = _concrete_live_provider(
                    account.get("provider") or account.get("name")
                )
                if candidate:
                    provider = candidate
                    model = model or str(account.get("model") or "")
                    break
        if not provider:
            current_provider = _concrete_live_provider(_current_codex_account_label())
            if current_provider and not is_local_api_service_provider_name(current_provider):
                provider = current_provider
        existing = self.state.latest_request if self.state and isinstance(self.state.latest_request, dict) else {}
        if not provider and self.state:
            provider = str(self.state.latest_account_name or "")
        model = model or str(existing.get("model") or "-")
        return provider, model

    def _record_live_provider_overlay(
        self,
        overlay: dict[str, Any],
        provider: str,
        usage: dict[str, Any],
        when: datetime,
    ) -> None:
        provider = str(provider or "").strip()
        if not provider:
            return
        targets = overlay.setdefault("providers", {})
        target = targets.get(provider)
        if not isinstance(target, dict):
            provider_key = account_display_key(provider)
            client_usage = self.state.client_usage if self.state and isinstance(self.state.client_usage, dict) else {}
            raw_providers = client_usage.get("providers")
            raw_provider_rows = raw_providers if isinstance(raw_providers, list) else []
            top_account_rows = self.state.top_accounts if self.state and isinstance(self.state.top_accounts, list) else []
            raw_row = next(
                (
                    row
                    for row in raw_provider_rows
                    if isinstance(row, dict)
                    and account_display_key(row.get("name")) == provider_key
                ),
                {},
            )
            top_row = next(
                (
                    row
                    for row in top_account_rows
                    if isinstance(row, dict)
                    and account_display_key(row.get("name")) == provider_key
                ),
                {},
            )
            target = {
                "base_tokens": max(int(raw_row.get("tokens") or 0), int(top_row.get("tokens") or 0)),
                "base_requests": max(int(raw_row.get("requests") or 0), int(top_row.get("requests") or 0)),
                "base_cost": max(float(raw_row.get("cost") or 0.0), float(top_row.get("cost") or 0.0)),
                "base_input_tokens": int(raw_row.get("input_tokens") or 0),
                "base_cached_input_tokens": int(raw_row.get("cached_input_tokens") or 0),
                "base_output_tokens": int(raw_row.get("output_tokens") or 0),
                "tokens": 0,
                "requests": 0,
                "cost": 0.0,
                "input_tokens": 0,
                "cached_input_tokens": 0,
                "output_tokens": 0,
                "latest_when": when,
            }
            targets[provider] = target
        total_tokens = max(0, int(usage.get("total_tokens") or 0))
        raw_input = max(0, int(usage.get("input_tokens") or 0))
        cached_input = min(raw_input, max(0, int(usage.get("cached_tokens") or 0)))
        output_tokens = max(0, int(usage.get("output_tokens") or 0))
        target["tokens"] += total_tokens
        target["requests"] += 1
        target["cost"] += max(0.0, float(usage.get("cost") or 0.0))
        target["input_tokens"] += max(0, raw_input - cached_input)
        target["cached_input_tokens"] += cached_input
        target["output_tokens"] += output_tokens
        target["latest_when"] = max(target["latest_when"], when)

    def _live_usage_batch_requires_verification(
        self,
        events: list[dict[str, Any]],
        now_clock: float,
    ) -> bool:
        samples = getattr(self, "_live_usage_rate_samples", None)
        if not isinstance(samples, list):
            samples = []
            self._live_usage_rate_samples = samples
        cutoff = now_clock - LIVE_USAGE_VERIFY_WINDOW_SECONDS
        samples[:] = [
            (timestamp, tokens)
            for timestamp, tokens in samples
            if timestamp >= cutoff
        ]
        batch_tokens = sum(max(0, int(event.get("total_tokens") or 0)) for event in events)
        projected_tokens = sum(tokens for _timestamp, tokens in samples) + batch_tokens
        verification_pending = bool(
            getattr(self, "_live_usage_verification_pending", False)
        )
        if not verification_pending and projected_tokens < LIVE_USAGE_VERIFY_THRESHOLD_TOKENS:
            samples.append((now_clock, batch_tokens))
            return False

        self._live_usage_verification_pending = True
        self._live_usage_verification_pending_tokens = max(
            0,
            int(getattr(self, "_live_usage_verification_pending_tokens", 0) or 0),
        ) + batch_tokens
        latest_when = max(
            (
                event.get("when")
                for event in events
                if isinstance(event.get("when"), datetime)
            ),
            default=None,
        )
        previous_latest = getattr(self, "_live_usage_verification_latest_when", None)
        if latest_when is not None and (
            not isinstance(previous_latest, datetime) or latest_when > previous_latest
        ):
            self._live_usage_verification_latest_when = latest_when
        samples.clear()

        watcher = getattr(self, "_live_usage_watcher", None)
        if isinstance(watcher, CodexUsageFileWatcher):
            watcher.reconciliation_needed = True
            watcher._last_reconciliation_change_at = now_clock
        if hasattr(self, "root"):
            self._schedule_live_usage_reconcile(LIVE_USAGE_VERIFY_DELAY_MS)
        return True

    def _record_live_usage_events(
        self,
        events: list[dict[str, Any]],
        *,
        allow_historical: bool = False,
        animate: bool = True,
    ) -> bool:
        if self.state is None:
            return False
        now_utc = datetime.now(timezone.utc)
        today = now_utc.astimezone(CN_TZ).date()
        seen_ids = getattr(self, "_live_usage_seen_ids", None)
        if not isinstance(seen_ids, dict):
            seen_ids = {}
            self._live_usage_seen_ids = seen_ids
        recent: list[dict[str, Any]] = []
        for event in events:
            when = event.get("when")
            if not isinstance(when, datetime):
                continue
            age_seconds = (now_utc - when.astimezone(timezone.utc)).total_seconds()
            if age_seconds < -30:
                continue
            if allow_historical:
                if when.astimezone(CN_TZ).date() != today:
                    continue
            elif age_seconds > 600:
                continue
            total_tokens = int(event.get("total_tokens") or 0)
            if not 0 < total_tokens <= LIVE_USAGE_MAX_SINGLE_EVENT_TOKENS:
                continue
            event_id = str(event.get("event_id") or "")
            if not event_id:
                event_id = _live_usage_event_id(
                    when,
                    str(event.get("session_id") or ""),
                    int(event.get("input_tokens") or 0),
                    int(event.get("cached_tokens") or 0),
                    int(event.get("output_tokens") or 0),
                )
                event["event_id"] = event_id
            if event_id in seen_ids:
                continue
            seen_ids[event_id] = None
            recent.append(event)
        while len(seen_ids) > 16_384:
            seen_ids.pop(next(iter(seen_ids)))
        if not recent:
            return False
        sample_clock = time.monotonic()
        if not allow_historical and self._live_usage_batch_requires_verification(
            recent,
            sample_clock,
        ):
            return False
        records = getattr(self, "_live_usage_event_records", None)
        if not isinstance(records, dict):
            records = {}
            self._live_usage_event_records = records
        for event in recent:
            records[str(event["event_id"])] = event
        while len(records) > 16_384:
            records.pop(next(iter(records)))
        overlay = self._live_usage_overlay
        if overlay is None:
            client_usage = self.state.client_usage if isinstance(self.state.client_usage, dict) else {}
            overlay = {
                "base_today_tokens": int(self.state.today_tokens or 0),
                "base_today_requests": int(self.state.today_requests or 0),
                "base_today_cost": float(self.state.today_account_cost or 0.0),
                "base_authoritative_tokens": int(client_usage.get("tokens") or 0),
                "base_updated_at": str(client_usage.get("updated_at") or ""),
                "tokens": 0,
                "requests": 0,
                "cost": 0.0,
                "input_tokens": 0,
                "cached_input_tokens": 0,
                "output_tokens": 0,
                "latest_when": recent[0]["when"],
            }
            self._live_usage_overlay = overlay
        accepted_tokens = 0
        accepted_cost = 0.0
        client_usage = self.state.client_usage if isinstance(self.state.client_usage, dict) else {}
        fallback_cost_per_token = (
            max(0.0, float(self.state.today_account_cost or 0.0))
            / max(1, int(self.state.today_tokens or 0))
        )
        api_service_current = bool(client_usage.get("api_service_routed"))
        cockpit_markers = (
            _load_live_cockpit_markers(COCKPIT_REQUEST_LOG_DB, now_utc)
            if api_service_current
            else []
        )
        latest_event = max(recent, key=lambda item: item["when"])
        latest_provider = ""
        latest_model = ""
        for event_index, event in enumerate(recent):
            tokens = int(event.get("total_tokens") or 0)
            accepted_tokens += tokens
            event_cost = max(0.0, float(event.get("cost") or 0.0))
            raw_input = max(0, int(event.get("input_tokens") or 0))
            cached_input = max(0, int(event.get("cached_tokens") or 0))
            output = max(0, int(event.get("output_tokens") or 0))
            overlay["tokens"] += tokens
            overlay["requests"] += 1
            overlay["input_tokens"] += max(0, raw_input - cached_input)
            overlay["cached_input_tokens"] += cached_input
            overlay["output_tokens"] += output
            overlay["latest_when"] = max(overlay["latest_when"], event["when"])
            if animate:
                if not hasattr(self, "_token_flow_samples"):
                    self._token_flow_samples = []
                # Statistics retain the log timestamp, while the trace starts when
                # the watcher detects the event. A tiny capped offset keeps events
                # discovered in the same poll visually distinct at the left edge.
                visual_offset = min(0.12, event_index * 0.02)
                self._token_flow_samples.append((sample_clock - visual_offset, tokens))
            provider = _concrete_live_provider(event.get("provider"))
            model = str(event.get("model") or "")
            matched_marker = _match_live_cockpit_marker(event, cockpit_markers)
            provider_usage = event
            record_provider = bool(provider)
            if matched_marker is not None:
                cockpit_markers.remove(matched_marker)
                provider = str(matched_marker.get("label") or "")
                model = str(matched_marker.get("model") or "")
                provider_usage = matched_marker
                record_provider = bool(provider)
            context_provider, context_model = ("", "")
            if not provider or not model:
                context_provider, context_model = self._live_event_request_context(event)
            if not model:
                model = context_model
            if not provider:
                provider = context_provider
                if api_service_current:
                    # Cockpit may write its routing marker a moment after the
                    # Codex token event. Keep the total optimistic, but never
                    # guess which pool account should receive it.
                    provider = ""
                record_provider = bool(provider)
            if event_cost <= 0:
                event_cost = estimate_live_usage_cost(
                    provider_usage,
                    model,
                    fallback_cost_per_token=fallback_cost_per_token,
                )
            event["cost"] = event_cost
            provider_usage["cost"] = event_cost
            overlay["cost"] += event_cost
            accepted_cost += event_cost
            if record_provider and provider:
                self._record_live_provider_overlay(
                    overlay,
                    provider,
                    provider_usage,
                    event["when"],
                )
            if provider:
                event["provider"] = provider
            if model:
                event["model"] = model
            if event is latest_event:
                latest_provider = provider
                latest_model = model
        if animate:
            self._record_token_delta_badge(accepted_tokens, now=sample_clock)
            self._record_cost_delta_badge(accepted_cost, now=sample_clock)
        if not latest_model:
            _provider, latest_model = self._live_event_request_context(latest_event)
            if not latest_provider and not api_service_current:
                latest_provider = _provider
        latest_when = latest_event["when"].astimezone(CN_TZ).isoformat(timespec="seconds")
        self.state.latest_request = {
            "kind": "success",
            "model": latest_model or "-",
            "created_at": latest_when,
            "source": "CLIENT",
        }
        if latest_provider:
            self.state.latest_account_name = latest_provider
        if animate and hasattr(self, "root"):
            self._ensure_pulse_animation()
        self._apply_live_usage_overlay(self.state)
        self._persist_live_usage_checkpoint(force=allow_historical)
        return True

    def _apply_live_usage_overlay(self, state: MonitorState) -> None:
        overlay = self._live_usage_overlay
        if not isinstance(overlay, dict):
            return
        state.today_tokens = max(
            int(state.today_tokens or 0),
            int(overlay["base_today_tokens"]) + int(overlay["tokens"]),
        )
        state.today_requests = max(
            int(state.today_requests or 0),
            int(overlay["base_today_requests"]) + int(overlay["requests"]),
        )
        state.today_account_cost = max(
            float(state.today_account_cost or 0.0),
            float(overlay.get("base_today_cost") or 0.0) + float(overlay.get("cost") or 0.0),
        )
        provider_targets = overlay.get("providers")
        if isinstance(provider_targets, dict):
            raw_providers = (
                state.client_usage.get("providers")
                if isinstance(state.client_usage, dict)
                and isinstance(state.client_usage.get("providers"), list)
                else []
            )
            top_accounts = state.top_accounts if isinstance(state.top_accounts, list) else []
            for provider, target in provider_targets.items():
                if not isinstance(target, dict):
                    continue
                provider_key = account_display_key(provider)
                desired = {
                    "tokens": int(target["base_tokens"]) + int(target["tokens"]),
                    "requests": int(target["base_requests"]) + int(target["requests"]),
                    "input_tokens": int(target["base_input_tokens"]) + int(target["input_tokens"]),
                    "cached_input_tokens": int(target["base_cached_input_tokens"]) + int(target["cached_input_tokens"]),
                    "output_tokens": int(target["base_output_tokens"]) + int(target["output_tokens"]),
                }
                desired_cost = float(target.get("base_cost") or 0.0) + float(target.get("cost") or 0.0)
                latest_when = target.get("latest_when")
                latest_at = (
                    latest_when.astimezone(CN_TZ).isoformat(timespec="seconds")
                    if isinstance(latest_when, datetime)
                    else ""
                )
                raw_row = next(
                    (
                        row
                        for row in raw_providers
                        if isinstance(row, dict)
                        and account_display_key(row.get("name")) == provider_key
                    ),
                    None,
                )
                if isinstance(raw_row, dict):
                    for key, value in desired.items():
                        raw_row[key] = max(int(raw_row.get(key) or 0), value)
                    raw_row["cost"] = max(float(raw_row.get("cost") or 0.0), desired_cost)
                    if latest_at:
                        raw_row["latest_at"] = latest_at
                top_row = next(
                    (
                        row
                        for row in top_accounts
                        if isinstance(row, dict)
                        and account_display_key(row.get("name")) == provider_key
                    ),
                    None,
                )
                if isinstance(top_row, dict):
                    top_row["tokens"] = max(int(top_row.get("tokens") or 0), desired["tokens"])
                    top_row["requests"] = max(int(top_row.get("requests") or 0), desired["requests"])
                    top_row["cost"] = max(float(top_row.get("cost") or 0.0), desired_cost)
                    if latest_at:
                        top_row["latest_at"] = latest_at
        state.cost_history = trend_with_current_totals(
            state.cost_history,
            state.today_tokens,
            state.today_requests,
            state.today_account_cost,
        )
        state.updated_at = time.time()

    def _authoritative_state_covers_live_overlay(self, state: MonitorState) -> bool:
        overlay = self._live_usage_overlay
        if not isinstance(overlay, dict):
            return True
        target_today = int(overlay["base_today_tokens"]) + int(overlay["tokens"])
        client_usage = state.client_usage if isinstance(state.client_usage, dict) else None
        if int(state.today_tokens or 0) >= target_today:
            return True
        sync = state.usage_sync if isinstance(state.usage_sync, dict) else {}
        if not sync.get("fresh"):
            return False
        latest_candidates = [state.latest_request]
        if client_usage is not None:
            latest_candidates.append(client_usage.get("latest_request"))
        latest_times = [
            _parse_time(
                str((candidate or {}).get("created_at") or (candidate or {}).get("latest_at") or "")
            )
            for candidate in latest_candidates
            if isinstance(candidate, dict)
        ]
        latest = max((value for value in latest_times if value is not None), default=None)
        overlay_latest = overlay.get("latest_when")
        return bool(
            latest is not None
            and isinstance(overlay_latest, datetime)
            and latest >= overlay_latest - timedelta(seconds=1)
        )

    def _apply_live_usage_change(self, changed: bool, events: list[dict[str, Any]]) -> None:
        try:
            if self.closed:
                return
            recorded = self._record_live_usage_events(events)
            watcher = getattr(self, "_live_usage_watcher", None)
            needs_reconcile = (changed and not recorded) or (
                isinstance(watcher, CodexUsageFileWatcher)
                and watcher.reconciliation_needed
            )
            if needs_reconcile:
                self._schedule_live_usage_reconcile()
            if recorded or bool(
                getattr(self, "_live_usage_verification_pending", False)
            ):
                self._draw()
        finally:
            self._live_usage_lock.release()

    def _schedule_live_usage_reconcile(self, delay_ms: int = LIVE_USAGE_RECONCILE_DELAY_MS) -> bool:
        if (
            self.closed
            or not hasattr(self, "root")
            or bool(getattr(self, "_live_reconcile_scheduled", False))
        ):
            return False
        self._live_reconcile_scheduled = True
        self.root.after(max(1_000, int(delay_ms)), self._run_live_usage_reconcile)
        return True

    def _run_live_usage_reconcile(self) -> None:
        self._live_reconcile_scheduled = False
        if self.closed:
            return
        watcher = getattr(self, "_live_usage_watcher", None)
        elapsed = time.monotonic() - float(
            getattr(self, "_last_live_reconcile_at", float("-inf"))
        )
        urgent_verification = bool(
            getattr(self, "_live_usage_verification_pending", False)
        )
        if elapsed < LIVE_USAGE_RECONCILE_MIN_INTERVAL_SECONDS and not urgent_verification:
            remaining_ms = int(
                (LIVE_USAGE_RECONCILE_MIN_INTERVAL_SECONDS - elapsed) * 1000
            )
            self._schedule_live_usage_reconcile(max(1_000, remaining_ms))
            return
        if isinstance(watcher, CodexUsageFileWatcher):
            quiet_seconds = LIVE_USAGE_RECONCILE_DELAY_MS / 1000.0
            if not watcher.reconciliation_ready(quiet_seconds):
                self._schedule_live_usage_reconcile()
                return
            watcher.mark_reconciled()
        if not self._refresh_live_usage_catchup_async():
            self._schedule_live_usage_reconcile()

    def _schedule_live_usage_refresh(self) -> None:
        if self.closed:
            return
        self._refresh_live_usage_async()
        interval_ms = self._live_usage_watcher.next_poll_interval_ms()
        self.root.after(interval_ms, self._schedule_live_usage_refresh)

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    #  DATA REFRESH
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def refresh_async(self, force: bool = False) -> bool:
        if not self._refresh_lock.acquire(blocking=False):
            if force:
                self._refresh_pending = True
                self._draw()
            return False
        if force:
            self.client.clear_runtime_caches()
        self._refresh_pending = False
        self._loading = True
        self._draw()
        self._ensure_pulse_animation()

        def _worker() -> None:
            err = None
            try:
                result = self.client.fetch_state()
            except Exception as exc:
                result = None
                err = f"\u8bf7\u6c42\u5931\u8d25: {exc}"
            self.root.after(0, lambda: self._apply_state(result, err))

        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        return True

    def _apply_state(self, result: MonitorState | None, error: str | None = None) -> None:
        refresh_pending = self._refresh_pending
        self._refresh_pending = False
        self._loading = False
        try:
            self._refresh_lock.release()
        except RuntimeError:
            pass
        self.error = error or (result.error if result is not None else None)
        if result is not None:
            try:
                result.cost_history = update_usage_history(result)
            except Exception:
                result.cost_history = summarize_usage_history(load_usage_history())
            if bool((result.usage_sync or {}).get("fresh")):
                client_usage = result.client_usage if isinstance(result.client_usage, dict) else {}
                scan_status = (
                    client_usage.get("scan_status")
                    if isinstance(client_usage.get("scan_status"), dict)
                    else {}
                )
                self._complete_live_usage_verification(
                    _parse_time(str(scan_status.get("through") or ""))
                )
            if self._authoritative_state_covers_live_overlay(result):
                self._live_usage_overlay = None
                self._clear_live_usage_checkpoint()
            else:
                self._apply_live_usage_overlay(result)
            self.state = result
            if result.usage_source == "local":
                self._last_quota_refresh_at = time.monotonic()
                self._last_forced_full_refresh_at = time.monotonic()
                if bool((result.usage_sync or {}).get("fresh")):
                    self._full_refresh_requested = False
        self._draw()
        if refresh_pending and not self.closed:
            self.refresh_async(force=True)

    def _handle_day_rollover(self, force: bool = False) -> bool:
        current_day = today_key()
        if not force and current_day == self._current_day_key:
            return False
        self._current_day_key = current_day
        self.client.clear_runtime_caches()
        self._live_usage_overlay = None
        self._clear_live_usage_checkpoint()
        self._live_usage_verification_pending = False
        self._live_usage_verification_latest_when = None
        self._live_usage_verification_pending_tokens = 0
        samples = getattr(self, "_live_usage_rate_samples", None)
        if isinstance(samples, list):
            samples.clear()
        self._account_range_auto_selected = False
        if not self._account_range_user_selected:
            self._account_range = "today"
        return True

    def _apply_quota_snapshot(self, payload: dict[str, Any] | None) -> None:
        try:
            if self.closed or self.state is None or not isinstance(payload, dict):
                return
            accounts = payload.get("accounts")
            if not isinstance(accounts, dict):
                return
            client_usage = self.state.client_usage if isinstance(self.state.client_usage, dict) else {}
            raw_providers = client_usage.get("providers")
            raw_rows = raw_providers if isinstance(raw_providers, list) else []
            top_rows = self.state.top_accounts if isinstance(self.state.top_accounts, list) else []
            quota_fields = (
                "quota_available",
                "quota_stale",
                "quota_unlimited",
                "quota_source",
                "quota_snapshot_at",
                "quota_reset_unavailable",
                "quota_snapshot_expired",
                "quota_idle",
                "countdown_active",
                "remaining_percent",
                "utilization",
                "resets_at",
                "window_minutes",
                "window_days",
            )
            quota_signature_fields = (
                "quota_available",
                "quota_stale",
                "quota_unlimited",
                "remaining_percent",
                "utilization",
                "resets_at",
                "window_minutes",
            )
            quota_changed = False

            def quota_signature(window: dict[str, Any]) -> tuple[Any, ...]:
                return tuple(window.get(field) for field in quota_signature_fields)

            def merge_account_windows(row: dict[str, Any], quota: dict[str, Any]) -> None:
                nonlocal quota_changed
                for window_key in ("window_5h", "window_7d", "window_cycle"):
                    quota_window = quota.get(window_key)
                    if not isinstance(quota_window, dict):
                        continue
                    current = dict(row.get(window_key) or {})
                    previous_signature = quota_signature(current)
                    for field in quota_fields:
                        current.pop(field, None)
                    current.update(quota_window)
                    if quota_signature(current) != previous_signature:
                        quota_changed = True
                    row[window_key] = current

            for provider, quota in accounts.items():
                if not isinstance(quota, dict):
                    continue
                provider_key = account_display_key(provider)
                for row in raw_rows:
                    if isinstance(row, dict) and account_display_key(row.get("name")) == provider_key:
                        merge_account_windows(row, quota)
                        break
                for row in top_rows:
                    if isinstance(row, dict) and account_display_key(row.get("name")) == provider_key:
                        merge_account_windows(row, quota)
                        break
            if quota_changed:
                # The live watcher updates today's totals, but a changed quota
                # boundary also requires rebuilding the 5h/7d Token and cost.
                self._full_refresh_requested = True
            self.state.updated_at = time.time()
            self._draw()
        finally:
            try:
                self._quota_refresh_lock.release()
            except RuntimeError:
                pass

    def _refresh_quota_async(self, force: bool = False) -> bool:
        if (
            self.closed
            or self.state is None
            or self.state.usage_source != "local"
            or not CLIENT_USAGE_EXPORT.exists()
        ):
            return False
        now = time.monotonic()
        if not force and now - self._last_quota_refresh_at < QUOTA_REFRESH_SECONDS:
            return False
        if not self._quota_refresh_lock.acquire(blocking=False):
            return False
        self._last_quota_refresh_at = now

        def _worker() -> None:
            payload: dict[str, Any] | None = None
            try:
                completed = subprocess.run(
                    client_usage_export_command("--quota-only"),
                    cwd=str(APP_DIR),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    timeout=QUOTA_REFRESH_TIMEOUT_SECONDS,
                    check=False,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
                if completed.returncode == 0:
                    lines = [line for line in str(completed.stdout or "").splitlines() if line.strip()]
                    if lines:
                        parsed = json.loads(lines[-1])
                        if isinstance(parsed, dict):
                            payload = parsed
            except (OSError, ValueError, json.JSONDecodeError, subprocess.TimeoutExpired):
                payload = None
            try:
                self.root.after(0, lambda: self._apply_quota_snapshot(payload))
            except tk.TclError:
                try:
                    self._quota_refresh_lock.release()
                except RuntimeError:
                    pass

        try:
            self._quota_refresh_executor.submit(_worker)
        except RuntimeError:
            self._quota_refresh_lock.release()
            return False
        return True

    def _local_usage_snapshot_age_seconds(self) -> float:
        if self.state is None or self.state.usage_source != "local":
            return 0.0
        client_usage = self.state.client_usage if isinstance(self.state.client_usage, dict) else {}
        updated_at = _parse_time(str(client_usage.get("updated_at") or ""))
        if updated_at is None:
            try:
                updated_at = datetime.fromtimestamp(CLIENT_USAGE_JSON.stat().st_mtime, tz=CN_TZ)
            except OSError:
                return float("inf")
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=CN_TZ)
        return max(0.0, (datetime.now(updated_at.tzinfo) - updated_at).total_seconds())

    def _full_usage_refresh_due(self) -> bool:
        if self.state is None or self.state.usage_source != "local":
            return False
        requested = bool(getattr(self, "_full_refresh_requested", False))
        stale = self._local_usage_snapshot_age_seconds() >= FULL_USAGE_REFRESH_MAX_STALE_SECONDS
        if not requested and not stale:
            return False
        last_attempt = float(getattr(self, "_last_forced_full_refresh_at", float("-inf")))
        return time.monotonic() - last_attempt >= FULL_USAGE_REFRESH_RETRY_SECONDS

    def _codex_logs_busy(self) -> bool:
        watcher = getattr(self, "_live_usage_watcher", None)
        if (
            isinstance(watcher, CodexUsageFileWatcher)
            and watcher.has_recent_activity(LIVE_USAGE_EXPORT_IDLE_SECONDS)
        ):
            return True
        if self.state is None or not isinstance(self.state.client_usage, dict):
            return False
        sessions = self.state.client_usage.get("active_sessions")
        return any(
            isinstance(row, dict)
            and row.get("active", True)
            and is_recent_activity(
                str(row.get("latest_at") or ""),
                window_seconds=LIVE_USAGE_EXPORT_IDLE_SECONDS,
            )
            for row in (sessions if isinstance(sessions, list) else [])
        )

    def _schedule_auto_refresh(self) -> None:
        if self.closed:
            return
        self._handle_day_rollover()
        catchup_lock = getattr(self, "_live_catchup_lock", None)
        refresh_in_progress = (
            self._refresh_lock.locked()
            or self._quota_refresh_lock.locked()
            or bool(catchup_lock is not None and catchup_lock.locked())
        )
        quota_started = False if refresh_in_progress else self._refresh_quota_async()
        if not refresh_in_progress and not quota_started:
            full_refresh_due = self._full_usage_refresh_due()
            logs_busy = self._codex_logs_busy()
            last_attempt = float(getattr(self, "_last_forced_full_refresh_at", float("-inf")))
            reconcile_live_overlay = bool(getattr(self, "_live_usage_overlay", None)) and not logs_busy and (
                time.monotonic() - last_attempt >= FULL_USAGE_REFRESH_RETRY_SECONDS
            )
            if full_refresh_due or reconcile_live_overlay:
                if self.refresh_async():
                    self._last_forced_full_refresh_at = time.monotonic()
        self.root.after(REFRESH_SECONDS * 1000, self._schedule_auto_refresh)

    def _schedule_midnight_refresh(self) -> None:
        if self.closed:
            return
        now = datetime.now(CN_TZ)
        next_day = now.date() + timedelta(days=1)
        next_midnight = datetime.combine(next_day, datetime.min.time(), tzinfo=CN_TZ)
        delay_ms = max(1000, int((next_midnight - now).total_seconds() * 1000) + 5000)
        self.root.after(delay_ms, self._on_midnight_refresh)

    def _on_midnight_refresh(self) -> None:
        if self.closed:
            return
        self._handle_day_rollover(force=True)
        self.refresh_async()
        self._schedule_midnight_refresh()

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    #  LIFECYCLE
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def close_app(self) -> None:
        self.closed = True
        self._persist_live_usage_checkpoint(force=True)
        live_watcher = getattr(self, "_live_usage_watcher", None)
        if isinstance(live_watcher, CodexUsageFileWatcher):
            live_watcher.close()
        for name in (
            "_live_active_executor",
            "_live_usage_executor",
            "_live_catchup_executor",
            "_quota_refresh_executor",
        ):
            executor = getattr(self, name, None)
            if executor is not None:
                executor.shutdown(wait=False)
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def run_monitor_app() -> bool:
    mutex_handle = acquire_single_instance_mutex()
    if mutex_handle is None:
        return False
    try:
        FloatingMonitorApp().run()
    finally:
        release_single_instance_mutex(mutex_handle)
    return True


def run_monitor_smoke_test() -> int:
    root: tk.Tk | None = None
    try:
        root = tk.Tk()
        root.withdraw()
        root.update_idletasks()
        root.update()
        return 0
    except Exception:
        return 1
    finally:
        if root is not None:
            try:
                root.destroy()
            except Exception:
                pass


if __name__ == "__main__":
    if "--smoke-test" in sys.argv:
        raise SystemExit(run_monitor_smoke_test())
    run_monitor_app()
