from __future__ import annotations

import copy
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import monitor
from remote_usage import ExporterReportProvider, RemoteUsageServer, merge_configured_remote_usage


def usage_report(
    *,
    tokens: int,
    provider: str = "Codex local",
    report_date: str = "2026-07-19",
) -> dict:
    return {
        "schema": 1,
        "source": "client-jsonl",
        "updated_at": "2026-07-19T12:00:00+08:00",
        "date": report_date,
        "scan_status": {
            "state": "complete",
            "from": "2026-07-19T00:00:00+08:00",
            "through": "2026-07-19T12:00:00+08:00",
            "source": "local-logs",
        },
        "today": {
            "name": "Client local",
            "requests": 1,
            "tokens": tokens,
            "input_tokens": tokens - 20,
            "cached_input_tokens": tokens - 40,
            "cache_creation_input_tokens": 0,
            "output_tokens": 20,
            "cost": 0.5,
            "models": {"gpt-test": tokens},
            "latest_at": "2026-07-19T11:59:00+08:00",
            "latest_model": "gpt-test",
            "show_zero": False,
        },
        "providers": [
            {
                "name": provider,
                "requests": 1,
                "tokens": tokens,
                "input_tokens": tokens - 20,
                "cached_input_tokens": tokens - 40,
                "cache_creation_input_tokens": 0,
                "output_tokens": 20,
                "cost": 0.5,
                "models": {"gpt-test": tokens},
                "latest_at": "2026-07-19T11:59:00+08:00",
                "latest_model": "gpt-test",
            }
        ],
        "latest_request": {
            "provider": provider,
            "model": "gpt-test",
            "created_at": "2026-07-19T11:59:00+08:00",
            "kind": "success",
        },
        "active_sessions": [],
        "dashboard": {
            "hourly_today": [
                {"hour": hour, "requests": 1 if hour == 11 else 0, "tokens": tokens if hour == 11 else 0, "cost": 0.5 if hour == 11 else 0.0}
                for hour in range(24)
            ]
        },
    }


class RemoteUsageIntegrationTests(unittest.TestCase):
    def test_remote_report_is_added_to_local_totals_over_http(self) -> None:
        remote = RemoteUsageServer(
            host="127.0.0.1",
            port=0,
            node_id="build-box",
            report_provider=lambda requested_date, _include_30d: usage_report(
                tokens=300,
                report_date=requested_date,
            ),
        )
        remote.start()
        self.addCleanup(remote.stop)
        local = usage_report(tokens=100, provider="Codex laptop")

        merged = merge_configured_remote_usage(
            copy.deepcopy(local),
            nodes=f"build={remote.url}",
            token="",
            timeout=2,
            include_30d=False,
        )

        self.assertEqual(merged["today"]["tokens"], 400)
        self.assertEqual(merged["today"]["requests"], 2)
        self.assertEqual(merged["dashboard"]["hourly_today"][11]["tokens"], 400)
        self.assertEqual(merged["providers"][-1]["name"], "Codex local @ build-box")
        self.assertEqual(merged["remote_nodes"][0]["state"], "ok")

    def test_bearer_token_blocks_unauthorized_collectors(self) -> None:
        remote = RemoteUsageServer(
            host="127.0.0.1",
            port=0,
            node_id="secure-box",
            token="shared-secret",
            report_provider=lambda _date, _include_30d: usage_report(tokens=300),
        )
        remote.start()
        self.addCleanup(remote.stop)

        rejected = merge_configured_remote_usage(
            usage_report(tokens=100),
            nodes=f"secure={remote.url}",
            token="wrong-secret",
            timeout=2,
        )
        self.assertEqual(rejected["today"]["tokens"], 100)
        self.assertEqual(rejected["remote_nodes"][0]["state"], "error")
        self.assertEqual(rejected["scan_status"]["state"], "partial")

        accepted = merge_configured_remote_usage(
            usage_report(tokens=100),
            nodes=f"secure={remote.url}",
            token="shared-secret",
            timeout=2,
        )
        self.assertEqual(accepted["today"]["tokens"], 400)

    def test_same_remote_node_is_not_counted_twice_through_two_aliases(self) -> None:
        remote = RemoteUsageServer(
            host="127.0.0.1",
            port=0,
            node_id="one-physical-box",
            report_provider=lambda _date, _include_30d: usage_report(tokens=300),
        )
        remote.start()
        self.addCleanup(remote.stop)

        merged = merge_configured_remote_usage(
            usage_report(tokens=100),
            nodes=f"primary={remote.url};alias={remote.url}",
            token="",
            timeout=2,
        )

        self.assertEqual(merged["today"]["tokens"], 400)
        self.assertEqual(
            sorted(status["state"] for status in merged["remote_nodes"]),
            ["duplicate", "ok"],
        )

    def test_collector_does_not_count_itself_as_a_remote_node(self) -> None:
        remote = RemoteUsageServer(
            host="127.0.0.1",
            port=0,
            node_id=socket.gethostname(),
            report_provider=lambda _date, _include_30d: usage_report(tokens=300),
        )
        remote.start()
        self.addCleanup(remote.stop)

        merged = merge_configured_remote_usage(
            usage_report(tokens=100),
            nodes=f"loop={remote.url}",
            token="",
            timeout=2,
        )

        self.assertEqual(merged["today"]["tokens"], 100)
        self.assertEqual(merged["remote_nodes"][0]["state"], "duplicate")

    def test_malformed_remote_report_is_ignored_without_breaking_local_export(self) -> None:
        bad_report = usage_report(tokens=300)
        bad_report["today"]["tokens"] = "not-a-number"
        remote = RemoteUsageServer(
            host="127.0.0.1",
            port=0,
            node_id="bad-box",
            report_provider=lambda _date, _include_30d: bad_report,
        )
        remote.start()
        self.addCleanup(remote.stop)

        merged = merge_configured_remote_usage(
            usage_report(tokens=100),
            nodes=f"bad={remote.url}",
            timeout=2,
        )

        self.assertEqual(merged["today"]["tokens"], 100)
        self.assertEqual(merged["today"]["requests"], 1)
        self.assertEqual(merged["remote_nodes"][0]["state"], "error")

    def test_remote_agent_runs_local_exporter_without_recursive_remote_collection(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            exporter = temp / "fake_exporter.py"
            output = temp / "remote_report.json"
            exporter.write_text(
                """
import argparse
import json
import os
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--output", required=True)
parser.add_argument("--date", required=True)
parser.add_argument("--include-30d", action="store_true")
args = parser.parse_args()
report = {
    "date": args.date,
    "include_30d": args.include_30d,
    "recursive_remote_config": bool(os.environ.get("TOKEN_PULSE_REMOTE_NODES")),
    "remote_collection_disabled": os.environ.get("TOKEN_PULSE_DISABLE_REMOTE_COLLECTION") == "1",
}
Path(args.output).write_text(json.dumps(report), encoding="utf-8")
""",
                encoding="utf-8",
            )
            provider = ExporterReportProvider(
                exporter=exporter,
                output=output,
                python=sys.executable,
                timeout=5,
            )

            with patch.dict(os.environ, {"TOKEN_PULSE_REMOTE_NODES": "loop=http://127.0.0.1"}):
                report = provider("2026-07-19", True)

        self.assertEqual(report["date"], "2026-07-19")
        self.assertTrue(report["include_30d"])
        self.assertFalse(report["recursive_remote_config"])
        self.assertTrue(report["remote_collection_disabled"])

    def test_client_exporter_includes_configured_remote_node(self) -> None:
        remote = RemoteUsageServer(
            host="127.0.0.1",
            port=0,
            node_id="export-box",
            report_provider=lambda _date, _include_30d: usage_report(tokens=300),
        )
        remote.start()
        self.addCleanup(remote.stop)
        project_root = Path(__file__).resolve().parents[1]

        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            output = temp / "usage.json"
            env = os.environ.copy()
            env.update(
                {
                    "HOME": str(temp),
                    "USERPROFILE": str(temp),
                    "CLIENT_USAGE_OFFICIAL_QUOTA_REFRESH": "0",
                    "CLIENT_USAGE_OFFLINE_BACKFILL_MAX_DAYS": "0",
                    "CLIENT_USAGE_MODEL_PRICE_URL": "file:///missing-token-pulse-prices.json",
                    "TOKEN_PULSE_REMOTE_NODES": f"export={remote.url}",
                    "TOKEN_PULSE_REMOTE_TIMEOUT_SECONDS": "2",
                }
            )
            completed = subprocess.run(
                [
                    sys.executable,
                    str(project_root / "client_usage_export.py"),
                    "--output",
                    str(output),
                    "--date",
                    "2026-07-19",
                ],
                cwd=project_root,
                env=env,
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            report = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(report["today"]["tokens"], 300)
        self.assertEqual(report["providers"][-1]["name"], "Codex local @ export-box")
        self.assertEqual(report["remote_nodes"][0]["state"], "ok")

    def test_monitor_surfaces_remote_collection_failure_as_partial(self) -> None:
        report = usage_report(tokens=100)
        report["date"] = date.today().isoformat()
        report["remote_nodes"] = [
            {"name": "build", "state": "error", "message": "connection refused"}
        ]
        report["scan_status"]["state"] = "partial"
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "usage.json"
            output.write_text(json.dumps(report), encoding="utf-8")
            with patch.object(monitor, "CLIENT_USAGE_JSON", output):
                loaded = monitor.load_client_usage(run_export=False)

        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["sync"]["state"], "partial")
        self.assertIn("build", loaded["sync"]["message"])

    def test_remote_outage_does_not_reduce_a_same_day_high_water_mark(self) -> None:
        remote = RemoteUsageServer(
            host="127.0.0.1",
            port=0,
            node_id="sometimes-offline",
            report_provider=lambda requested_date, _include_30d: usage_report(
                tokens=300,
                report_date=requested_date,
            ),
        )
        remote.start()
        project_root = Path(__file__).resolve().parents[1]
        report_date = date.today().isoformat()

        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            output = temp / "usage.json"
            env = os.environ.copy()
            env.update(
                {
                    "HOME": str(temp),
                    "USERPROFILE": str(temp),
                    "CLIENT_USAGE_OFFICIAL_QUOTA_REFRESH": "0",
                    "CLIENT_USAGE_OFFLINE_BACKFILL_MAX_DAYS": "0",
                    "CLIENT_USAGE_MODEL_PRICE_URL": "file:///missing-token-pulse-prices.json",
                    "TOKEN_PULSE_REMOTE_NODES": f"remote={remote.url}",
                    "TOKEN_PULSE_REMOTE_TIMEOUT_SECONDS": "0.2",
                }
            )
            command = [
                sys.executable,
                str(project_root / "client_usage_export.py"),
                "--output",
                str(output),
                "--date",
                report_date,
            ]
            first = subprocess.run(command, cwd=project_root, env=env, capture_output=True, text=True, timeout=15)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["today"]["tokens"], 300)

            remote.stop()
            second = subprocess.run(command, cwd=project_root, env=env, capture_output=True, text=True, timeout=15)
            self.assertEqual(second.returncode, 0, second.stderr)
            after_outage = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(after_outage["today"]["tokens"], 300)
        self.assertEqual(after_outage["remote_nodes"][0]["state"], "error")
        self.assertEqual(after_outage["scan_status"]["state"], "partial")


if __name__ == "__main__":
    unittest.main()
