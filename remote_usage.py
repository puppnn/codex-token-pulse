from __future__ import annotations

import argparse
import copy
import hmac
import ipaddress
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date as date_type
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib import error, parse, request


REPORT_PATH = "/v1/usage"
MAX_REPORT_BYTES = 32 * 1024 * 1024
APP_DIR = (
    Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent
)
BUCKET_INTEGER_FIELDS = (
    "requests",
    "tokens",
    "input_tokens",
    "cached_input_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "output_tokens",
)


class ExporterReportProvider:
    """Serialize report generation through the existing local JSONL exporter."""

    def __init__(
        self,
        *,
        exporter: Path,
        output: Path,
        python: str = sys.executable,
        timeout: float = 90,
    ) -> None:
        self.exporter = Path(exporter)
        self.output = Path(output)
        self.python = python
        self.timeout = float(timeout)
        self._lock = threading.Lock()

    def __call__(self, report_date: str, include_30d: bool) -> dict[str, Any]:
        command = [
            self.python,
            str(self.exporter),
            "--output",
            str(self.output),
            "--date",
            report_date,
        ]
        if include_30d:
            command.append("--include-30d")
        env = os.environ.copy()
        for name in (
            "TOKEN_PULSE_REMOTE_NODES",
            "TOKEN_PULSE_REMOTE_TOKEN",
            "TOKEN_PULSE_REMOTE_TIMEOUT_SECONDS",
        ):
            env.pop(name, None)
        env["TOKEN_PULSE_DISABLE_REMOTE_COLLECTION"] = "1"
        with self._lock:
            completed = subprocess.run(
                command,
                cwd=str(self.exporter.resolve().parent),
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=max(1.0, self.timeout),
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            if completed.returncode != 0:
                detail = str(completed.stderr or "").strip().splitlines()
                message = detail[-1] if detail else f"exporter exited with code {completed.returncode}"
                raise RuntimeError(message[:240])
            try:
                report = json.loads(self.output.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeError(f"cannot read exported report: {exc}") from exc
        if not isinstance(report, dict):
            raise RuntimeError("exported report must be a JSON object")
        return report


class _UsageHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    # Finish in-flight JSON responses before closing the listening socket. The
    # exporter has its own timeout, so shutdown remains bounded in practice.
    daemon_threads = False


class _UsageRequestHandler(BaseHTTPRequestHandler):
    server_version = "TokenPulseRemote/1.0"

    @property
    def owner(self) -> "RemoteUsageServer":
        return self.server.owner  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: object) -> None:
        return

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        expected = self.owner.token
        if not expected:
            return True
        value = self.headers.get("Authorization", "")
        supplied = value[7:] if value.startswith("Bearer ") else ""
        return hmac.compare_digest(supplied, expected)

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        parsed = parse.urlsplit(self.path)
        if parsed.path != REPORT_PATH:
            self._send_json(404, {"ok": False, "error": "not found"})
            return
        if not self._authorized():
            self._send_json(401, {"ok": False, "error": "unauthorized"})
            return
        query = parse.parse_qs(parsed.query)
        report_date = query.get("date", [date_type.today().isoformat()])[0]
        try:
            date_type.fromisoformat(report_date)
        except ValueError:
            self._send_json(400, {"ok": False, "error": "date must use YYYY-MM-DD"})
            return
        include_30d = query.get("include_30d", ["0"])[0].casefold() in {"1", "true", "yes"}
        try:
            report = self.owner.report_provider(report_date, include_30d)
        except Exception as exc:
            self._send_json(500, {"ok": False, "error": str(exc)[:240]})
            return
        self._send_json(
            200,
            {
                "schema": 1,
                "node_id": self.owner.node_id,
                "report": report,
            },
        )


class RemoteUsageServer:
    """HTTP endpoint that exposes an aggregate usage report, never raw logs or auth files."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        node_id: str,
        report_provider: Callable[[str, bool], dict[str, Any]],
        token: str = "",
    ) -> None:
        self.host = host
        self.port = int(port)
        self.node_id = node_id.strip() or socket.gethostname()
        self.report_provider = report_provider
        self.token = token.strip()
        self._httpd: _UsageHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}{REPORT_PATH}"

    def start(self) -> None:
        if self._httpd is not None:
            return
        httpd = _UsageHTTPServer((self.host, self.port), _UsageRequestHandler)
        httpd.owner = self  # type: ignore[attr-defined]
        self._httpd = httpd
        self.port = int(httpd.server_address[1])
        self._thread = threading.Thread(target=httpd.serve_forever, name="token-pulse-remote", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        httpd, thread = self._httpd, self._thread
        self._httpd = None
        self._thread = None
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
        if thread is not None and thread.is_alive():
            thread.join(timeout=2)


def read_remote_env(path: Path | None = None) -> dict[str, str]:
    values: dict[str, str] = {}
    env_path = path or APP_DIR / ".env"
    try:
        lines = env_path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return values
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[key] = value
    return values


def remote_setting(name: str, default: str = "", *, file_values: dict[str, str] | None = None) -> str:
    value = os.environ.get(name)
    if value is not None:
        return value
    values = read_remote_env() if file_values is None else file_values
    return values.get(name, default)


def resolve_tailscale_ipv4() -> str | None:
    candidates = [shutil.which("tailscale"), r"C:\Program Files\Tailscale\tailscale.exe"]
    for candidate in candidates:
        if not candidate:
            continue
        try:
            completed = subprocess.run(
                [candidate, "ip", "-4"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError):
            continue
        for line in completed.stdout.splitlines():
            value = line.strip()
            try:
                address = ipaddress.ip_address(value)
            except ValueError:
                continue
            if address.version == 4:
                return value
    return None


def parse_remote_nodes(value: str) -> list[tuple[str, str]]:
    nodes: list[tuple[str, str]] = []
    names: set[str] = set()
    for item in value.split(";"):
        item = item.strip()
        if not item:
            continue
        if "=" not in item:
            raise ValueError(f"remote node must use name=url: {item}")
        name, url = (part.strip() for part in item.split("=", 1))
        if not name or not url:
            raise ValueError(f"remote node must use name=url: {item}")
        key = name.casefold()
        if key in names:
            raise ValueError(f"duplicate remote node name: {name}")
        parsed = parse.urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError(f"remote node URL must be HTTP(S): {url}")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError(f"remote node URL cannot contain credentials, query, or fragment: {url}")
        path = parsed.path.rstrip("/")
        if path not in {"", REPORT_PATH}:
            raise ValueError(f"remote node URL path must be empty or {REPORT_PATH}: {url}")
        names.add(key)
        normalized = url.rstrip("/")
        if not normalized.endswith(REPORT_PATH):
            normalized += REPORT_PATH
        nodes.append((name, normalized))
    return nodes


def fetch_remote_report(
    configured_name: str,
    url: str,
    *,
    report_date: str,
    include_30d: bool,
    token: str,
    timeout: float,
) -> tuple[str, str, dict[str, Any]]:
    separator = "&" if "?" in url else "?"
    endpoint = (
        f"{url}{separator}"
        + parse.urlencode({"date": report_date, "include_30d": "1" if include_30d else "0"})
    )
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    opener = request.build_opener(request.ProxyHandler({}))
    with opener.open(request.Request(endpoint, headers=headers), timeout=max(0.2, timeout)) as response:
        body = response.read(MAX_REPORT_BYTES + 1)
    if len(body) > MAX_REPORT_BYTES:
        raise ValueError("remote report is too large")
    payload = json.loads(body.decode("utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("report"), dict):
        raise ValueError("remote response does not contain a report")
    node_id = str(payload.get("node_id") or configured_name).strip() or configured_name
    report = payload["report"]
    if str(report.get("date") or "") != report_date:
        raise ValueError(f"remote report date mismatch: {report.get('date')}")
    return configured_name, node_id, report


def _newer_timestamp(left: Any, right: Any) -> bool:
    return bool(right) and (not left or str(right) > str(left))


def merge_bucket(target: dict[str, Any], source: dict[str, Any]) -> None:
    for key in BUCKET_INTEGER_FIELDS:
        target[key] = int(target.get(key) or 0) + int(source.get(key) or 0)
    target["cost"] = round(float(target.get("cost") or 0.0) + float(source.get("cost") or 0.0), 12)
    target_models = target.get("models")
    if not isinstance(target_models, dict):
        target_models = {}
        target["models"] = target_models
    source_models = source.get("models") if isinstance(source.get("models"), dict) else {}
    for model, tokens in source_models.items():
        target_models[str(model)] = int(target_models.get(str(model)) or 0) + int(tokens or 0)
    if _newer_timestamp(target.get("latest_at"), source.get("latest_at")):
        target["latest_at"] = source.get("latest_at")
        target["latest_model"] = source.get("latest_model") or ""


def _annotate_provider(provider: dict[str, Any], node_id: str) -> dict[str, Any]:
    result = copy.deepcopy(provider)
    result["name"] = f"{str(provider.get('name') or 'Remote usage')} @ {node_id}"
    result["node"] = node_id
    return result


def _merge_hourly(target: dict[str, Any], source: dict[str, Any]) -> None:
    target_rows = target.setdefault("dashboard", {}).setdefault("hourly_today", [])
    source_dashboard = source.get("dashboard") if isinstance(source.get("dashboard"), dict) else {}
    source_rows = source_dashboard.get("hourly_today") if isinstance(source_dashboard.get("hourly_today"), list) else []
    by_hour = {int(row.get("hour") or 0): row for row in target_rows if isinstance(row, dict)}
    for source_row in source_rows:
        if not isinstance(source_row, dict):
            continue
        hour = int(source_row.get("hour") or 0)
        target_row = by_hour.get(hour)
        if target_row is None:
            target_row = {"hour": hour, "requests": 0, "tokens": 0, "cost": 0.0}
            target_rows.append(target_row)
            by_hour[hour] = target_row
        target_row["requests"] = int(target_row.get("requests") or 0) + int(source_row.get("requests") or 0)
        target_row["tokens"] = int(target_row.get("tokens") or 0) + int(source_row.get("tokens") or 0)
        target_row["cost"] = round(float(target_row.get("cost") or 0.0) + float(source_row.get("cost") or 0.0), 12)
        if source_row.get("failure"):
            target_row["failure"] = True
            target_row["failure_count"] = int(target_row.get("failure_count") or 0) + int(source_row.get("failure_count") or 1)
            if _newer_timestamp(target_row.get("failure_at"), source_row.get("failure_at")):
                target_row["failure_at"] = source_row.get("failure_at")
                target_row["failure_kind"] = source_row.get("failure_kind") or "remote"
    target_rows.sort(key=lambda row: int(row.get("hour") or 0))


def merge_remote_report(target: dict[str, Any], report: dict[str, Any], node_id: str) -> None:
    target_today = target.setdefault("today", {})
    source_today = report.get("today") if isinstance(report.get("today"), dict) else {}
    merge_bucket(target_today, source_today)
    target["source"] = "client-jsonl+remote"
    target.setdefault("providers", []).extend(
        _annotate_provider(provider, node_id)
        for provider in report.get("providers", [])
        if isinstance(provider, dict)
    )
    for session in report.get("active_sessions", []):
        if not isinstance(session, dict):
            continue
        row = copy.deepcopy(session)
        row["node"] = node_id
        if row.get("provider"):
            row["provider"] = f"{row['provider']} @ {node_id}"
        target.setdefault("active_sessions", []).append(row)
    source_latest = report.get("latest_request") if isinstance(report.get("latest_request"), dict) else {}
    target_latest = target.setdefault("latest_request", {})
    if _newer_timestamp(target_latest.get("created_at"), source_latest.get("created_at")):
        target["latest_request"] = copy.deepcopy(source_latest)
        if target["latest_request"].get("provider"):
            target["latest_request"]["provider"] = f"{target['latest_request']['provider']} @ {node_id}"
        target["latest_request"]["node"] = node_id
    _merge_hourly(target, report)
    for key in ("unresolved_active_sessions", "unresolved_api_service_events", "cockpit_fallback_events"):
        target[key] = int(target.get(key) or 0) + int(report.get(key) or 0)


def merge_configured_remote_usage(
    output: dict[str, Any],
    *,
    nodes: str | None = None,
    token: str | None = None,
    timeout: float | None = None,
    include_30d: bool = False,
) -> dict[str, Any]:
    if os.environ.get("TOKEN_PULSE_DISABLE_REMOTE_COLLECTION", "").strip().casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        output.setdefault("node_id", os.environ.get("TOKEN_PULSE_LOCAL_NODE_ID") or socket.gethostname())
        return output
    file_values = read_remote_env() if nodes is None or token is None or timeout is None else {}
    local_node_id = str(
        output.get("node_id")
        or remote_setting("TOKEN_PULSE_LOCAL_NODE_ID", file_values=file_values)
        or socket.gethostname()
    ).strip()
    output["node_id"] = local_node_id
    configured = remote_setting("TOKEN_PULSE_REMOTE_NODES", file_values=file_values) if nodes is None else nodes
    if not configured.strip():
        return output
    auth_token = remote_setting("TOKEN_PULSE_REMOTE_TOKEN", file_values=file_values) if token is None else token
    if timeout is None:
        try:
            timeout = float(remote_setting("TOKEN_PULSE_REMOTE_TIMEOUT_SECONDS", "5", file_values=file_values))
        except ValueError:
            timeout = 5.0
    try:
        parsed_nodes = parse_remote_nodes(configured)
    except ValueError as exc:
        output["remote_nodes"] = [{"state": "error", "message": str(exc)}]
        output.setdefault("scan_status", {})["state"] = "partial"
        return output
    if not parsed_nodes:
        output["remote_nodes"] = []
        return output

    statuses: list[dict[str, Any]] = []
    fetched: list[tuple[str, str, dict[str, Any]]] = []
    with ThreadPoolExecutor(max_workers=min(8, len(parsed_nodes))) as executor:
        futures = {
            executor.submit(
                fetch_remote_report,
                name,
                url,
                report_date=str(output.get("date") or date_type.today().isoformat()),
                include_30d=include_30d,
                token=auth_token,
                timeout=timeout,
            ): (name, url)
            for name, url in parsed_nodes
        }
        for future in as_completed(futures):
            name, url = futures[future]
            try:
                fetched.append(future.result())
            except (OSError, ValueError, json.JSONDecodeError, error.URLError, error.HTTPError) as exc:
                statuses.append({"name": name, "state": "error", "message": str(exc)[:240]})

    seen_node_ids: set[str] = {local_node_id.casefold()} if local_node_id else set()
    for configured_name, node_id, report in sorted(fetched, key=lambda item: item[0].casefold()):
        key = node_id.casefold()
        if key in seen_node_ids:
            statuses.append(
                {
                    "name": configured_name,
                    "node_id": node_id,
                    "state": "duplicate",
                    "message": "same remote node was already merged",
                }
            )
            continue
        seen_node_ids.add(key)
        try:
            candidate = copy.deepcopy(output)
            merge_remote_report(candidate, report, node_id)
            output.clear()
            output.update(candidate)
        except (TypeError, ValueError, KeyError) as exc:
            statuses.append(
                {
                    "name": configured_name,
                    "node_id": node_id,
                    "state": "error",
                    "message": f"invalid remote report: {exc}"[:240],
                }
            )
            continue
        statuses.append(
            {
                "name": configured_name,
                "node_id": node_id,
                "state": "ok",
                "updated_at": report.get("updated_at") or "",
                "tokens": int((report.get("today") or {}).get("tokens") or 0),
            }
        )
    statuses.sort(key=lambda row: str(row.get("name") or "").casefold())
    output["remote_nodes"] = statuses
    scan_status = output.setdefault("scan_status", {})
    scan_status["source"] = "local+remote"
    if any(status.get("state") == "error" for status in statuses):
        scan_status["state"] = "partial"
    return output


def build_argument_parser() -> argparse.ArgumentParser:
    file_values = read_remote_env()
    parser = argparse.ArgumentParser(
        description="Serve this machine's aggregate Token Pulse usage over its Tailscale address."
    )
    parser.add_argument(
        "--host",
        default=remote_setting("TOKEN_PULSE_REMOTE_HOST", "tailscale", file_values=file_values),
        help="Bind address, or 'tailscale' to auto-detect the active Tailscale IPv4.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(remote_setting("TOKEN_PULSE_REMOTE_PORT", "8765", file_values=file_values)),
    )
    parser.add_argument(
        "--node-id",
        default=remote_setting("TOKEN_PULSE_LOCAL_NODE_ID", socket.gethostname(), file_values=file_values),
    )
    parser.add_argument(
        "--token",
        default=remote_setting("TOKEN_PULSE_REMOTE_TOKEN", "", file_values=file_values),
        help="Optional shared bearer token; prefer TOKEN_PULSE_REMOTE_TOKEN in .env.",
    )
    parser.add_argument("--exporter", type=Path, default=APP_DIR / "client_usage_export.py")
    parser.add_argument("--output", type=Path, default=APP_DIR / "client_usage_remote.json")
    parser.add_argument(
        "--export-timeout",
        type=float,
        default=float(remote_setting("SUB2API_CLIENT_USAGE_EXPORT_TIMEOUT_SECONDS", "90", file_values=file_values)),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_argument_parser()
    args = parser.parse_args(argv)
    host = str(args.host).strip()
    if host.casefold() in {"tailscale", "auto"}:
        detected = resolve_tailscale_ipv4()
        if detected is None:
            parser.error("Tailscale has no active IPv4 address; start Tailscale or pass --host explicitly")
        host = detected
    if not args.exporter.is_file():
        parser.error(f"client usage exporter does not exist: {args.exporter}")
    provider = ExporterReportProvider(
        exporter=args.exporter,
        output=args.output,
        timeout=args.export_timeout,
    )
    server = RemoteUsageServer(
        host=host,
        port=args.port,
        node_id=args.node_id,
        token=args.token,
        report_provider=provider,
    )
    try:
        server.start()
    except OSError as exc:
        parser.error(f"cannot bind remote usage endpoint: {exc}")
    print(f"Token Pulse remote usage: {server.url}", flush=True)
    if not args.token:
        print("Access is controlled by Tailscale ACLs only (no bearer token configured).", flush=True)
    try:
        while True:
            threading.Event().wait(3600)
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
