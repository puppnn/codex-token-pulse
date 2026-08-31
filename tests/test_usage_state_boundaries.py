import base64
import copy
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import client_usage_export
import monitor


_TEST_STATE_DIRECTORY: tempfile.TemporaryDirectory[str] | None = None
_TEST_STATE_PATCHERS: list[patch] = []


def setUpModule() -> None:
    """Keep integration-style unit tests away from the live Pulse state.

    Several tests intentionally exercise real persistence helpers.  Patching
    only the path used by the assertion leaves secondary writes (quota cache,
    attribution verdicts, backups, and monitor checkpoints) pointing at the
    application directory.  A single module-scoped sandbox makes those writes
    deterministic and prevents fixture accounts from leaking into live data.
    """
    global _TEST_STATE_DIRECTORY, _TEST_STATE_PATCHERS
    _TEST_STATE_DIRECTORY = tempfile.TemporaryDirectory(prefix="token-pulse-tests-")
    root = Path(_TEST_STATE_DIRECTORY.name)
    export_paths = {
        "LOG_PATH": "tokenpulse-export.log",
        "DEFAULT_OUTPUT": "client_usage_today.json",
        "CONFIG_PATH": "client_usage_config.json",
        "SPEED_HISTORY_PATH": "client_usage_speed_history.json",
        "ACCOUNT_TIMELINE_PATH": "client_usage_account_timeline.json",
        "AUTH_SWITCH_EVENTS_PATH": "client_usage_auth_switch_events.jsonl",
        "ATTRIBUTION_LEDGER_PATH": "client_usage_attribution_ledger.json",
        "USAGE_HISTORY_PATH": "usage_history.json",
        "MODEL_PRICE_CACHE_PATH": "client_usage_model_prices.json",
        "CODEX_EVENT_CACHE_PATH": "client_usage_codex_event_cache.json",
        "OPENCODEX_USAGE_CACHE_PATH": "client_usage_opencodex_usage_cache.json",
        "OPENCODEX_ACCOUNT_TIMELINE_PATH": "client_usage_opencodex_account_timeline.json",
        "OPENCODEX_ACCOUNT_MAP_PATH": "client_usage_opencodex_accounts.json",
        "COCKPIT_OFFICIAL_QUOTA_CACHE_PATH": "client_usage_official_quota_cache.json",
    }
    monitor_paths = {
        "CLIENT_USAGE_JSON": "monitor-client-usage.json",
        "MODEL_PRICE_CACHE_JSON": "monitor-model-prices.json",
        "USAGE_HISTORY_JSON": "monitor-usage-history.json",
        "ACCOUNT_TYPE_HISTORY_JSON": "monitor-account-types.json",
        "LIVE_USAGE_CHECKPOINT_JSON": "monitor-live-checkpoint.json",
        "ATTRIBUTION_DIAGNOSTICS_PATH": "monitor-attribution-diagnostics.jsonl",
        "CLIENT_USAGE_ROUTE_LABELS_JSON": "monitor-route-labels.json",
        "AUTH_SWITCH_EVENTS_PATH": "monitor-auth-switch-events.jsonl",
    }
    targets = (
        (client_usage_export, export_paths),
        (monitor, monitor_paths),
    )
    _TEST_STATE_PATCHERS = [
        patch.object(module, attribute, root / relative_path)
        for module, paths in targets
        for attribute, relative_path in paths.items()
    ]
    _TEST_STATE_PATCHERS.extend(
        [
            patch.object(client_usage_export, "_ATTRIBUTION_LEDGER_DOCUMENT_CACHE", None),
            patch.object(client_usage_export, "_ONLINE_PRICE_CACHE_PATH", None),
            patch.object(client_usage_export.logger, "disabled", True),
            patch.object(monitor, "_USAGE_HISTORY_CACHE", None),
            patch.object(monitor.LOGGER, "disabled", True),
        ]
    )
    for patcher in _TEST_STATE_PATCHERS:
        patcher.start()


def tearDownModule() -> None:
    global _TEST_STATE_DIRECTORY, _TEST_STATE_PATCHERS
    for patcher in reversed(_TEST_STATE_PATCHERS):
        patcher.stop()
    _TEST_STATE_PATCHERS = []
    if _TEST_STATE_DIRECTORY is not None:
        _TEST_STATE_DIRECTORY.cleanup()
        _TEST_STATE_DIRECTORY = None


class PersistentStateIsolationTests(unittest.TestCase):
    def test_runtime_state_paths_are_redirected_to_module_sandbox(self) -> None:
        assert _TEST_STATE_DIRECTORY is not None
        root = Path(_TEST_STATE_DIRECTORY.name).resolve()
        export_attributes = (
            "LOG_PATH",
            "DEFAULT_OUTPUT",
            "CONFIG_PATH",
            "SPEED_HISTORY_PATH",
            "ACCOUNT_TIMELINE_PATH",
            "AUTH_SWITCH_EVENTS_PATH",
            "ATTRIBUTION_LEDGER_PATH",
            "USAGE_HISTORY_PATH",
            "MODEL_PRICE_CACHE_PATH",
            "CODEX_EVENT_CACHE_PATH",
            "OPENCODEX_USAGE_CACHE_PATH",
            "OPENCODEX_ACCOUNT_TIMELINE_PATH",
            "OPENCODEX_ACCOUNT_MAP_PATH",
            "COCKPIT_OFFICIAL_QUOTA_CACHE_PATH",
        )
        monitor_attributes = (
            "CLIENT_USAGE_JSON",
            "MODEL_PRICE_CACHE_JSON",
            "USAGE_HISTORY_JSON",
            "ACCOUNT_TYPE_HISTORY_JSON",
            "LIVE_USAGE_CHECKPOINT_JSON",
            "ATTRIBUTION_DIAGNOSTICS_PATH",
            "CLIENT_USAGE_ROUTE_LABELS_JSON",
            "AUTH_SWITCH_EVENTS_PATH",
        )

        for module, attributes in (
            (client_usage_export, export_attributes),
            (monitor, monitor_attributes),
        ):
            for attribute in attributes:
                path = Path(getattr(module, attribute)).resolve()
                self.assertEqual(path.parent, root, f"{module.__name__}.{attribute}")


class CodexAuthIdentityTests(unittest.TestCase):
    @staticmethod
    def jwt(claims: dict) -> str:
        def encode(value: dict) -> str:
            raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
            return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

        return f"{encode({'alg': 'none'})}.{encode(claims)}.signature"

    @classmethod
    def official_auth(cls, email: str, account_id: str = "account-official") -> dict:
        return {
            "tokens": {
                "id_token": cls.jwt(
                    {
                        "email": email,
                        "https://api.openai.com/auth": {
                            "chatgpt_account_id": account_id,
                        },
                    }
                ),
                "account_id": account_id,
            }
        }

    def test_modern_official_auth_reads_jwt_email_and_nested_account(self) -> None:
        auth = self.official_auth("manual@example.com")

        self.assertEqual(
            client_usage_export.codex_auth_identity(auth),
            "manual@example.com",
        )
        self.assertEqual(monitor.codex_auth_identity(auth), "manual@example.com")

        auth["tokens"]["id_token"] = self.jwt({})
        self.assertEqual(
            client_usage_export.codex_auth_identity(auth),
            "account-official",
        )

    def test_routing_diagnostic_is_not_treated_as_account_identity(self) -> None:
        auth = {
            "api_provider_name": "session-affinity: cache hit before new K12 routing"
        }

        self.assertEqual(client_usage_export.codex_auth_identity(auth), "")

    def test_account_plan_type_reads_nested_auth_claim(self) -> None:
        auth = {
            "tokens": {
                "id_token": self.jwt(
                    {
                        "email": "pro@example.com",
                        "https://api.openai.com/auth": {
                            "chatgpt_plan_type": "pro",
                        },
                    }
                )
            }
        }

        self.assertEqual(monitor.codex_auth_plan_type(auth), "PRO")

    def test_account_display_removes_nested_local_prefixes(self) -> None:
        self.assertEqual(
            monitor.ranking_account_display_name(
                "LOCAL - Codex local - account@example.com"
            ),
            "account@example.com",
        )
        self.assertEqual(
            monitor.ranking_account_display_name("Codex local - api-service-local"),
            "API \u670d\u52a1",
        )
        self.assertEqual(
            monitor.ranking_account_display_name("Grok local"),
            "Grok",
        )
        self.assertEqual(
            monitor.ranking_account_display_name("Grok subagent"),
            "Grok \u5b50\u4ee3\u7406",
        )
        self.assertEqual(
            monitor.ranking_account_display_name("OpenCode subagent"),
            "OpenCode \u5b50\u4ee3\u7406",
        )

    def test_internal_cockpit_filename_displays_manifest_email_and_plan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            cockpit = home / ".antigravity_cockpit"
            cockpit.mkdir(parents=True)
            history_path = root / "client_usage_account_types.json"
            account_id = "codex_a537e71a6393d78bbac5e57d3d128fbc"
            (cockpit / "codex_accounts.json").write_text(
                json.dumps(
                    {
                        "accounts": [
                            {
                                "id": account_id,
                                "email": "fixture-a@example.com",
                                "plan_type": "free",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            raw_name = f"Codex local - {account_id}.json"

            with (
                patch.object(monitor.Path, "home", return_value=home),
                patch.object(monitor, "ACCOUNT_TYPE_HISTORY_JSON", history_path),
                patch.object(monitor, "_ACCOUNT_DISPLAY_ALIAS_SIGNATURE", None),
                patch.object(monitor, "_ACCOUNT_DISPLAY_ALIASES", {}),
                patch.object(monitor, "_ACCOUNT_DISPLAY_ALIAS_CHECKED_AT", 0.0),
                patch.object(monitor, "_ACCOUNT_TYPE_CACHE_SIGNATURE", ()),
                patch.object(monitor, "_ACCOUNT_TYPE_CACHE", {}),
            ):
                self.assertEqual(
                    monitor.ranking_account_display_name(raw_name),
                    "fixture-a@example.com",
                )
                self.assertEqual(
                    monitor.account_type_label({"name": raw_name}),
                    "FREE",
                )

    def test_account_type_prefers_explicit_plan_then_local_metadata(self) -> None:
        self.assertEqual(monitor.account_type_label({"plan_type": "plus"}), "PLUS")
        with patch.object(
            monitor,
            "local_account_type_map",
            return_value={"account@example.com": "K12"},
        ):
            self.assertEqual(
                monitor.account_type_label(
                    {"name": "LOCAL - Codex local - account@example.com"}
                ),
                "K12",
            )
        with patch.object(monitor, "local_account_type_map", return_value={}):
            self.assertEqual(
                monitor.account_type_label(
                    {"name": "Codex local - removed@example.com"}
                ),
                "\u672a\u77e5",
            )

    def test_api_service_badge_distinguishes_pending_from_pool_total(self) -> None:
        with patch.object(monitor, "local_account_type_map", return_value={}):
            self.assertEqual(
                monitor.account_type_label(
                    {
                        "name": "Codex local - api-service-local",
                        "is_api_service_aggregate": True,
                    }
                ),
                "\u5f85\u5f52\u56e0",
            )
            self.assertEqual(
                monitor.account_type_label(
                    {
                        "name": "api-service-local",
                        "is_pool_aggregate": True,
                    }
                ),
                "\u8d26\u53f7\u6c60",
            )

    def test_recent_api_service_aggregate_is_marked_as_attributing(self) -> None:
        now = datetime.now(timezone.utc)
        with patch.object(monitor, "local_account_type_map", return_value={}):
            self.assertEqual(
                monitor.account_type_label(
                    {
                        "name": "Codex local - api-service-local",
                        "is_api_service_aggregate": True,
                        "latest_at": now.isoformat(),
                    }
                ),
                "\u5f52\u56e0\u4e2d",
            )
            self.assertEqual(
                monitor.account_type_label(
                    {
                        "name": "Codex local - api-service-local",
                        "is_api_service_aggregate": True,
                        "latest_at": (
                            now
                            - timedelta(
                                seconds=monitor.ATTRIBUTION_IN_PROGRESS_SECONDS + 1
                            )
                        ).isoformat(),
                    }
                ),
                "\u5f85\u5f52\u56e0",
            )

    def test_account_type_history_survives_cockpit_account_deletion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            auth_dir = (
                home
                / ".antigravity_cockpit"
                / "codex_local_access_sidecar"
                / "auths"
            )
            auth_dir.mkdir(parents=True)
            auth = self.official_auth("removed@example.com", "removed-account")
            claims = {
                "email": "removed@example.com",
                "https://api.openai.com/auth": {
                    "chatgpt_account_id": "removed-account",
                    "chatgpt_plan_type": "k12",
                },
            }
            auth["tokens"]["id_token"] = self.jwt(claims)
            backup = auth_dir / "removed.json.bak"
            backup.write_text(json.dumps(auth), encoding="utf-8")
            history_path = root / "client_usage_account_types.json"

            with (
                patch.object(monitor.Path, "home", return_value=home),
                patch.object(monitor, "ACCOUNT_TYPE_HISTORY_JSON", history_path),
                patch.object(monitor, "_ACCOUNT_TYPE_CACHE_SIGNATURE", ()),
                patch.object(monitor, "_ACCOUNT_TYPE_CACHE", {}),
            ):
                first = monitor.local_account_type_map()
                self.assertEqual(first["removed@example.com"], "K12")
                self.assertTrue(history_path.exists())

                backup.unlink()
                monitor._ACCOUNT_TYPE_CACHE_SIGNATURE = ()
                monitor._ACCOUNT_TYPE_CACHE = {}
                second = monitor.local_account_type_map()

            self.assertEqual(second["removed@example.com"], "K12")

    def test_current_manifest_overrides_last_known_account_type(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            cockpit_root = home / ".antigravity_cockpit"
            cockpit_root.mkdir(parents=True)
            history_path = root / "client_usage_account_types.json"
            monitor.write_json_atomic(
                history_path,
                {
                    "schema": 1,
                    "accounts": {
                        "upgraded@example.com": {
                            "plan_type": "PLUS",
                            "updated_at": "2026-01-01T00:00:00+08:00",
                        }
                    },
                },
            )
            (cockpit_root / "codex_accounts.json").write_text(
                json.dumps(
                    {
                        "accounts": [
                            {
                                "email": "upgraded@example.com",
                                "plan_type": "pro",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            with (
                patch.object(monitor.Path, "home", return_value=home),
                patch.object(monitor, "ACCOUNT_TYPE_HISTORY_JSON", history_path),
                patch.object(monitor, "_ACCOUNT_TYPE_CACHE_SIGNATURE", ()),
                patch.object(monitor, "_ACCOUNT_TYPE_CACHE", {}),
            ):
                account_types = monitor.local_account_type_map()

            self.assertEqual(account_types["upgraded@example.com"], "PRO")
            saved = json.loads(history_path.read_text(encoding="utf-8"))
            self.assertEqual(
                saved["accounts"]["upgraded@example.com"]["plan_type"],
                "PRO",
            )

    def test_direct_provider_ignores_stale_cockpit_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            codex_dir = home / ".codex"
            codex_dir.mkdir()
            (codex_dir / "config.toml").write_text(
                'model_provider = "openai"\n',
                encoding="utf-8",
            )
            (codex_dir / "auth.json").write_text(
                json.dumps(self.official_auth("official@example.com")),
                encoding="utf-8",
            )
            (codex_dir / ".cockpit_codex_auth.json").write_text(
                json.dumps({"email": "api-service-local"}),
                encoding="utf-8",
            )

            label = client_usage_export.current_codex_account_label(home)
            with patch.object(monitor.os.path, "expanduser", return_value=str(home)):
                monitor_identity = monitor.current_codex_auth_identity()

        self.assertEqual(label, "Codex local - official@example.com")
        self.assertEqual(monitor_identity, "official@example.com")

    def test_api_service_provider_still_prefers_cockpit_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            codex_dir = home / ".codex"
            codex_dir.mkdir()
            (codex_dir / "config.toml").write_text(
                'model_provider = "codex_local_access"\n',
                encoding="utf-8",
            )
            (codex_dir / "auth.json").write_text(
                json.dumps(self.official_auth("official@example.com")),
                encoding="utf-8",
            )
            (codex_dir / ".cockpit_codex_auth.json").write_text(
                json.dumps({"email": "api-service-local"}),
                encoding="utf-8",
            )

            label = client_usage_export.current_codex_account_label(home)
            with patch.object(monitor.os.path, "expanduser", return_value=str(home)):
                monitor_identity = monitor.current_codex_auth_identity()

        self.assertEqual(label, "Codex local - api-service-local")
        self.assertEqual(monitor_identity, "api-service-local")

    def test_switch_timeline_uses_auth_file_modification_time(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            codex_dir = home / ".codex"
            codex_dir.mkdir()
            timeline_path = home / "timeline.json"
            auth_events_path = home / "auth-switches.jsonl"
            auth_path = codex_dir / "auth.json"
            (codex_dir / "config.toml").write_text(
                'model_provider = "openai"\n',
                encoding="utf-8",
            )
            auth_path.write_text(
                json.dumps(self.official_auth("switched@example.com")),
                encoding="utf-8",
            )
            switched_at = datetime(2026, 7, 14, 10, 5, 0).timestamp()
            os.utime(auth_path, (switched_at, switched_at))
            now = datetime(2026, 7, 14, 10, 35, 0)

            with (
                patch.object(client_usage_export, "ACCOUNT_TIMELINE_PATH", timeline_path),
                patch.object(client_usage_export, "AUTH_SWITCH_EVENTS_PATH", auth_events_path),
            ):
                client_usage_export.record_current_account_snapshot(home, now)
                markers = client_usage_export.load_account_timeline()

        self.assertEqual(len(markers), 1)
        self.assertEqual(markers[0].label, "Codex local - switched@example.com")
        self.assertEqual(markers[0].when, datetime.fromtimestamp(switched_at))

    def test_no_cockpit_manual_switch_splits_tasks_at_auth_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            codex_dir = home / ".codex"
            codex_dir.mkdir()
            timeline_path = home / "timeline.json"
            auth_events_path = home / "auth-switches.jsonl"
            auth_path = codex_dir / "auth.json"
            (codex_dir / "config.toml").write_text(
                'model_provider = "openai"\n',
                encoding="utf-8",
            )
            first_switch = datetime(2026, 7, 14, 10, 0, 0)
            second_switch = datetime(2026, 7, 14, 10, 5, 0)

            with (
                patch.object(client_usage_export, "ACCOUNT_TIMELINE_PATH", timeline_path),
                patch.object(client_usage_export, "AUTH_SWITCH_EVENTS_PATH", auth_events_path),
            ):
                auth_path.write_text(
                    json.dumps(self.official_auth("account-a@example.com", "account-a")),
                    encoding="utf-8",
                )
                os.utime(auth_path, (first_switch.timestamp(), first_switch.timestamp()))
                client_usage_export.record_current_account_snapshot(
                    home,
                    datetime(2026, 7, 14, 10, 1, 0),
                )

                auth_path.write_text(
                    json.dumps(self.official_auth("account-b@example.com", "account-b")),
                    encoding="utf-8",
                )
                os.utime(auth_path, (second_switch.timestamp(), second_switch.timestamp()))
                client_usage_export.record_current_account_snapshot(
                    home,
                    datetime(2026, 7, 14, 10, 35, 0),
                )
                markers = client_usage_export.load_account_timeline()

            events = [
                client_usage_export.UsageEvent(
                    when=datetime(2026, 7, 14, 10, 6, 0),
                    request_at=datetime(2026, 7, 14, 10, 4, 0),
                    model="gpt-test",
                    input_tokens=100,
                    cached_tokens=20,
                    output_tokens=10,
                    session_id="session-a",
                ),
                client_usage_export.UsageEvent(
                    when=datetime(2026, 7, 14, 10, 7, 0),
                    request_at=datetime(2026, 7, 14, 10, 6, 0),
                    model="gpt-test",
                    input_tokens=200,
                    cached_tokens=40,
                    output_tokens=20,
                    session_id="session-b",
                ),
            ]
            attributed = client_usage_export.attribute_codex_events_by_account(
                events,
                markers,
            )

        self.assertEqual(
            sum(event.total_tokens for rows in attributed.values() for event in rows),
            sum(event.total_tokens for event in events),
        )
        self.assertEqual(
            [event.session_id for event in attributed["Codex local - account-a@example.com"]],
            ["session-a"],
        )
        self.assertEqual(
            [event.session_id for event in attributed["Codex local - account-b@example.com"]],
            ["session-b"],
        )

    def test_monitor_switch_log_deduplicates_the_last_valid_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            events_path = Path(directory) / "auth-switches.jsonl"
            first_switch = datetime(2026, 7, 14, 10, 0, tzinfo=monitor.CN_TZ)
            second_switch = datetime(2026, 7, 14, 10, 5, tzinfo=monitor.CN_TZ)

            with patch.object(monitor, "AUTH_SWITCH_EVENTS_PATH", events_path):
                self.assertTrue(
                    monitor.append_codex_auth_switch_event(
                        "account-a@example.com",
                        first_switch,
                    )
                )
                with events_path.open("a", encoding="utf-8") as handle:
                    handle.write("not-json\n")
                self.assertFalse(
                    monitor.append_codex_auth_switch_event(
                        "account-a@example.com",
                        first_switch + timedelta(minutes=1),
                    )
                )
                self.assertTrue(
                    monitor.append_codex_auth_switch_event(
                        "account-b@example.com",
                        second_switch,
                    )
                )

            records = []
            for line in events_path.read_text(encoding="utf-8").splitlines():
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

        self.assertEqual(
            [record["label"] for record in records],
            [
                "Codex local - account-a@example.com",
                "Codex local - account-b@example.com",
            ],
        )

    def test_exporter_loads_ordered_switch_log_without_timeline_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            timeline_path = root / "missing-timeline.json"
            events_path = root / "auth-switches.jsonl"
            events_path.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "at": "2026-07-14T10:05:00+08:00",
                                "label": "Codex local - account-b@example.com",
                            }
                        ),
                        json.dumps(
                            {
                                "at": "2026-07-14T10:00:00+08:00",
                                "label": "Codex local - account-a@example.com",
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            with (
                patch.object(client_usage_export, "ACCOUNT_TIMELINE_PATH", timeline_path),
                patch.object(client_usage_export, "AUTH_SWITCH_EVENTS_PATH", events_path),
            ):
                markers = client_usage_export.load_account_timeline()

        self.assertEqual(
            [marker.label for marker in markers],
            [
                "Codex local - account-a@example.com",
                "Codex local - account-b@example.com",
            ],
        )

    def test_auth_switch_capture_refreshes_only_live_accounts(self) -> None:
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        app._last_auth_identity = "account-a@example.com"
        app._refresh_live_active_async = MagicMock(return_value=True)
        app.refresh_async = MagicMock(return_value=True)
        changed_at = datetime(2026, 7, 14, 10, 5, tzinfo=monitor.CN_TZ)

        with (
            patch.object(
                monitor,
                "current_codex_auth_snapshot",
                return_value=("account-b@example.com", Path("auth.json"), changed_at),
            ),
            patch.object(monitor, "append_codex_auth_switch_event") as append_event,
        ):
            changed = app._capture_auth_switch()

        self.assertTrue(changed)
        self.assertEqual(app._last_auth_identity, "account-b@example.com")
        append_event.assert_called_once_with("account-b@example.com", changed_at)
        app._refresh_live_active_async.assert_called_once_with()
        app.refresh_async.assert_not_called()


class CompactNumberTests(unittest.TestCase):
    def test_trend_chart_labels_make_recent_bars_explicit(self) -> None:
        self.assertEqual(monitor.trend_chart_day_label("2026-07-09", 0), "7/9")
        self.assertEqual(monitor.trend_chart_day_label("2026-07-14", 5), "昨日")
        self.assertEqual(monitor.trend_chart_day_label("2026-07-15", 6), "今日")
        self.assertEqual(monitor.trend_chart_day_label("invalid", 2), "-")

    def test_billions_use_b_suffix(self) -> None:
        self.assertEqual(monitor.compact_number(1_000_000_000), "1.0B")
        self.assertEqual(monitor.compact_number(1_260_000_000), "1.3B")
        self.assertEqual(monitor.compact_number(-2_500_000_000), "-2.5B")

    def test_values_below_one_billion_keep_m_suffix(self) -> None:
        self.assertEqual(monitor.compact_number(999_900_000), "999.9M")

    def test_exact_token_count_never_uses_compact_suffixes(self) -> None:
        self.assertEqual(monitor.exact_token_count(1_260_000_000), "1,260,000,000")
        self.assertEqual(monitor.exact_token_count(999_900_000), "999,900,000")
        self.assertEqual(monitor.exact_token_count(None), "0")

    def test_single_segmented_flow_meter_grows_with_token_volume(self) -> None:
        zero = monitor.FloatingMonitorApp._token_flow_meter_fill_top(0.0, 10, 110)
        low = monitor.FloatingMonitorApp._token_flow_meter_fill_top(0.2, 10, 110)
        high = monitor.FloatingMonitorApp._token_flow_meter_fill_top(0.9, 10, 110)

        self.assertEqual(zero, 110)
        self.assertEqual(low, 90)
        self.assertEqual(high, 20)

    def test_flow_meter_fade_is_limited_to_the_moving_head(self) -> None:
        solid_top, bands = monitor.FloatingMonitorApp._token_flow_meter_head_geometry(
            60.0,
            110.0,
        )

        self.assertEqual(solid_top, 68.0)
        self.assertEqual(len(bands), monitor.TOKEN_FLOW_METER_HEAD_BANDS)
        self.assertEqual(bands[0][0], 60.0)
        self.assertEqual(bands[-1][1], 68.0)

    def test_flow_meter_level_eases_up_and_down(self) -> None:
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        app._token_flow_meter_display_level = 0.0
        app._token_flow_meter_last_tick = 0.0

        rising = app._smooth_token_flow_meter_level(1.0, now=0.016)
        app._token_flow_meter_last_tick = 0.016
        falling = app._smooth_token_flow_meter_level(0.0, now=0.032)

        self.assertGreater(rising, 0.2)
        self.assertLess(rising, 1.0)
        self.assertGreater(falling, 0.0)
        self.assertLess(falling, rising)

    def test_token_delta_badge_merges_nearby_events_and_restarts_later(self) -> None:
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)

        app._record_token_delta_badge(12_000, now=10.0)
        app._record_token_delta_badge(345, now=10.2)
        merged = app._token_delta_badge_visual(now=10.2)
        app._record_token_delta_badge(90, now=11.0)
        restarted = app._token_delta_badge_visual(now=11.0)

        self.assertEqual(merged, ("+12,345", monitor.Theme.live, True))
        self.assertEqual(restarted, ("+90", monitor.Theme.live, True))

    def test_token_delta_badge_fades_smoothly_then_hides(self) -> None:
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        app._record_token_delta_badge(12_345, now=10.0)

        text, middle_color, visible = app._token_delta_badge_visual(
            now=10.0 + monitor.TOKEN_DELTA_BADGE_DURATION_SECONDS / 2
        )
        expired = app._token_delta_badge_visual(
            now=10.0 + monitor.TOKEN_DELTA_BADGE_DURATION_SECONDS
        )

        self.assertEqual(text, "+12,345")
        self.assertTrue(visible)
        self.assertNotEqual(middle_color, monitor.Theme.live)
        self.assertNotEqual(middle_color, monitor.Theme.ag_surface)
        self.assertEqual(expired, ("", monitor.Theme.ag_surface, False))

    def test_live_cost_estimate_uses_cached_model_token_rates(self) -> None:
        prices = {
            "schema": 2,
            "models": {
                "gpt-test": {
                    "input_cost_per_token": 0.000001,
                    "cache_read_input_token_cost": 0.0000001,
                    "output_cost_per_token": 0.000002,
                }
            },
        }
        usage = {
            "total_tokens": 110,
            "input_tokens": 100,
            "cached_tokens": 40,
            "output_tokens": 10,
        }
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(monitor, "_LIVE_MODEL_PRICE_CACHE", None),
        ):
            cache_path = Path(directory) / "prices.json"
            cache_path.write_text(json.dumps(prices), encoding="utf-8")
            with patch.object(monitor, "MODEL_PRICE_CACHE_JSON", cache_path):
                cost = monitor.estimate_live_usage_cost(usage, "gpt-test")

        self.assertAlmostEqual(cost, 0.000084)

    def test_cost_delta_badge_merges_and_fades(self) -> None:
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        app._record_cost_delta_badge(0.04, now=10.0)
        app._record_cost_delta_badge(0.03, now=10.2)

        merged = app._cost_delta_badge_visual(now=10.2)
        middle = app._cost_delta_badge_visual(
            now=10.2 + monitor.TOKEN_DELTA_BADGE_DURATION_SECONDS / 2
        )
        expired = app._cost_delta_badge_visual(
            now=10.2 + monitor.TOKEN_DELTA_BADGE_DURATION_SECONDS
        )

        self.assertEqual(merged, ("+$0.070", monitor.Theme.warn, True))
        self.assertEqual(middle[0], "+$0.070")
        self.assertTrue(middle[2])
        self.assertNotEqual(middle[1], monitor.Theme.warn)
        self.assertEqual(expired, ("", monitor.Theme.ag_surface, False))

    def test_usage_overview_columns_fit_both_delta_groups_at_minimum_width(self) -> None:
        meter_l, meter_r, divider_x, cost_x = (
            monitor.FloatingMonitorApp._usage_overview_columns(
                14,
                346,
                147,
                147,
            )
        )

        self.assertGreater(meter_l, 26 + 147)
        self.assertGreater(meter_r, meter_l)
        self.assertGreater(divider_x, meter_r)
        self.assertGreater(cost_x, divider_x)
        self.assertLessEqual(cost_x + 147, 334)

    def test_usage_overview_compacts_when_both_values_gain_a_digit(self) -> None:
        needs_compact = monitor.FloatingMonitorApp._usage_overview_needs_compact_values(
            14,
            346,
            144,
            165,
        )
        meter_l, meter_r, divider_x, cost_x = (
            monitor.FloatingMonitorApp._usage_overview_columns(
                14,
                346,
                124,
                136,
            )
        )

        self.assertTrue(needs_compact)
        self.assertGreater(meter_r, meter_l)
        self.assertGreater(divider_x, meter_r)
        self.assertLessEqual(cost_x + 136, 334)

    def test_usage_overview_columns_share_extra_space_on_wide_windows(self) -> None:
        meter_l, meter_r, divider_x, cost_x = (
            monitor.FloatingMonitorApp._usage_overview_columns(
                14,
                506,
                147,
                147,
            )
        )
        left_space = meter_l - (26 + 147)
        right_space = 494 - (cost_x + 147)

        self.assertGreaterEqual(meter_r - meter_l, 39)
        self.assertLessEqual(abs(left_space - right_space), 6)
        self.assertGreater(divider_x, meter_r)

    def test_common_models_prioritize_five_rows_and_one_provider_row(self) -> None:
        visible = monitor.FloatingMonitorApp._top_model_visible_count(
            model_count=5,
            provider_count=3,
            available_height=236,
        )

        self.assertEqual(visible, 5)

    def test_common_models_badge_can_report_a_truncated_sixth_model(self) -> None:
        visible = monitor.FloatingMonitorApp._top_model_visible_count(
            model_count=6,
            provider_count=3,
            available_height=236,
        )

        self.assertEqual(visible, 5)


class UsageSyncLabelTests(unittest.TestCase):
    def test_routine_cached_baseline_does_not_replace_normal_update_time(self) -> None:
        self.assertEqual(monitor.usage_sync_label({"state": "cached"}), "")

    def test_timeout_with_recent_live_coverage_reports_queued_full_check(self) -> None:
        sync = {
            "state": "timeout",
            "live_overlay_covers_cache": True,
            "live_overlay_latest_at": datetime.now(timezone.utc).isoformat(),
        }

        self.assertTrue(monitor.usage_sync_has_live_coverage(sync))
        self.assertEqual(
            monitor.usage_sync_label(sync),
            "\u5b9e\u65f6\u7edf\u8ba1\u4e2d / \u5168\u91cf\u6838\u5bf9\u6392\u961f",
        )

    def test_timeout_with_stale_live_coverage_keeps_warning(self) -> None:
        sync = {
            "state": "timeout",
            "live_overlay_covers_cache": True,
            "live_overlay_latest_at": (
                datetime.now(timezone.utc)
                - timedelta(seconds=monitor.LIVE_USAGE_SYNC_FRESH_SECONDS + 1)
            ).isoformat(),
        }

        self.assertFalse(monitor.usage_sync_has_live_coverage(sync))
        self.assertEqual(
            monitor.usage_sync_label(sync),
            "\u8865\u5f55\u8d85\u65f6 / \u663e\u793a\u4e0a\u6b21\u6570\u636e",
        )

    def test_sync_failures_still_show_a_warning(self) -> None:
        self.assertTrue(monitor.usage_sync_label({"state": "timeout"}))
        self.assertTrue(monitor.usage_sync_label({"state": "error"}))
        self.assertTrue(monitor.usage_sync_label({"state": "stale"}))

    def test_current_totals_raise_the_today_trend_without_rewriting_history(self) -> None:
        summary = monitor.summarize_trend_rows([])
        original = copy.deepcopy(summary)

        updated = monitor.trend_with_current_totals(summary, 123_456, 12, 3.5)

        self.assertEqual(updated["today_tokens"], 123_456)
        self.assertEqual(updated["series"][-1]["tokens"], 123_456)
        self.assertEqual(summary, original)


class TooltipLayoutTests(unittest.TestCase):
    class FixedWidthFont:
        @staticmethod
        def measure(value: str) -> int:
            return len(value)

    def test_four_line_failure_tooltip_is_not_truncated(self) -> None:
        text = "03:00-04:00\n100 tokens\n2 calls · $0.01\nCodex task error detected at 03:55"

        lines = monitor.FloatingMonitorApp._wrap_tooltip_lines(text, self.FixedWidthFont(), 80)

        self.assertEqual(lines, text.splitlines())


class ModelPricingFallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_online_prices = client_usage_export._ONLINE_PRICE_TABLE
        self.original_online_details = client_usage_export._ONLINE_PRICE_DETAILS

    def tearDown(self) -> None:
        client_usage_export._ONLINE_PRICE_TABLE = self.original_online_prices
        client_usage_export._ONLINE_PRICE_DETAILS = self.original_online_details

    def test_unknown_model_uses_online_exact_price(self) -> None:
        client_usage_export._ONLINE_PRICE_TABLE = {
            "gpt-5.6-sol": (6.0, 0.6, 36.0),
        }
        client_usage_export._ONLINE_PRICE_DETAILS = {}

        self.assertEqual(
            client_usage_export.model_price("gpt-5.6-sol"),
            (6.0, 0.6, 36.0),
        )

    def test_unknown_gpt5_minor_uses_latest_known_family_price(self) -> None:
        client_usage_export._ONLINE_PRICE_TABLE = {}
        client_usage_export._ONLINE_PRICE_DETAILS = {}

        self.assertEqual(
            client_usage_export.model_price("gpt-5.6-sol"),
            client_usage_export.model_price("gpt-5.5"),
        )
        self.assertGreater(
            client_usage_export.estimate_cost("gpt-5.6-sol", 1000, 1000, 1000),
            0,
        )

    def test_known_model_keeps_exact_price(self) -> None:
        self.assertEqual(
            client_usage_export.model_price("gpt-5.4-mini"),
            (0.75, 0.075, 4.5),
        )

    def test_online_payload_is_converted_to_per_million_prices(self) -> None:
        prices = client_usage_export.extract_online_price_table(
            {
                "gpt-new": {
                    "litellm_provider": "openai",
                    "input_cost_per_token": 0.000006,
                    "cache_read_input_token_cost": 0.0000006,
                    "output_cost_per_token": 0.000036,
                }
            }
        )

        self.assertEqual(prices["gpt-new"], (6.0, 0.6, 36.0))

    def test_online_xai_prices_are_available_to_local_grok_models(self) -> None:
        prices = client_usage_export.extract_online_price_table(
            {
                "xai/grok-new": {
                    "litellm_provider": "xai",
                    "input_cost_per_token": 0.000003,
                    "cache_read_input_token_cost": 0.00000075,
                    "output_cost_per_token": 0.000015,
                }
            }
        )

        self.assertEqual(prices["xai/grok-new"], (3.0, 0.75, 15.0))
        self.assertEqual(prices["grok-new"], (3.0, 0.75, 15.0))

    def test_complete_online_pricing_rules_ignore_long_context_surcharge(self) -> None:
        profile = {
            "input_cost_per_token": 5.0,
            "input_cost_per_token_above_272k_tokens": 10.0,
            "input_cost_per_token_batches": 2.5,
            "input_cost_per_token_flex": 2.5,
            "input_cost_per_token_priority": 10.0,
            "cache_read_input_token_cost": 0.5,
            "cache_read_input_token_cost_above_272k_tokens": 1.0,
            "cache_read_input_token_cost_flex": 0.25,
            "cache_read_input_token_cost_priority": 1.0,
            "cache_creation_input_token_cost": 6.25,
            "cache_creation_input_token_cost_above_272k_tokens": 12.5,
            "cache_creation_input_token_cost_flex": 3.125,
            "cache_creation_input_token_cost_priority": 12.5,
            "output_cost_per_token": 30.0,
            "output_cost_per_token_above_272k_tokens": 45.0,
            "output_cost_per_token_batches": 15.0,
            "output_cost_per_token_flex": 15.0,
            "output_cost_per_token_priority": 60.0,
        }
        client_usage_export._ONLINE_PRICE_TABLE = {"gpt-new": (5.0, 0.5, 30.0)}
        client_usage_export._ONLINE_PRICE_DETAILS = {"gpt-new": profile}

        args = ("gpt-new", 100_000, 100_000, 10_000)
        self.assertAlmostEqual(
            client_usage_export.estimate_cost(*args, cache_creation_tokens=50_000),
            1.1625,
        )
        self.assertAlmostEqual(
            client_usage_export.estimate_cost(*args, cache_creation_tokens=50_000, pricing_tier="priority"),
            2.325,
        )
        self.assertAlmostEqual(
            client_usage_export.estimate_cost(*args, cache_creation_tokens=50_000, pricing_tier="flex"),
            0.58125,
        )
        self.assertAlmostEqual(
            client_usage_export.estimate_cost(*args, cache_creation_tokens=50_000, pricing_tier="batch"),
            0.7625,
        )
        self.assertAlmostEqual(
            client_usage_export.estimate_cost("gpt-new", 200_000, 100_000, 10_000),
            1.35,
        )

    def test_priority_event_is_not_multiplied_twice(self) -> None:
        client_usage_export._ONLINE_PRICE_TABLE = {"gpt-new": (5.0, 0.5, 30.0)}
        client_usage_export._ONLINE_PRICE_DETAILS = {
            "gpt-new": {
                "input_cost_per_token": 5.0,
                "input_cost_per_token_priority": 10.0,
                "cache_read_input_token_cost": 0.5,
                "cache_read_input_token_cost_priority": 1.0,
                "output_cost_per_token": 30.0,
                "output_cost_per_token_priority": 60.0,
            }
        }
        event = client_usage_export.UsageEvent(
            when=datetime(2026, 7, 11, 12, 0, 0),
            model="gpt-new",
            input_tokens=100_000,
            cached_tokens=0,
            output_tokens=0,
            app_speed="fast",
            cost_multiplier=2.0,
            pricing_tier="priority",
        )
        bucket = client_usage_export.UsageBucket()

        client_usage_export.add_codex_event_to_bucket(bucket, event)

        self.assertAlmostEqual(bucket.cost, 1.0)

    def test_flex_and_batch_tiers_survive_speed_fallback(self) -> None:
        self.assertEqual(client_usage_export.codex_service_tier_to_speed("flex"), "flex")
        self.assertEqual(client_usage_export.codex_service_tier_to_speed("batch"), "batch")
        self.assertEqual(client_usage_export.codex_speed_cost_multiplier("flex"), 1.0)


class UsageHistoryIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.original_history_path = monitor.USAGE_HISTORY_JSON
        monitor.USAGE_HISTORY_JSON = Path(self.temporary_directory.name) / "usage_history.json"
        monitor._USAGE_HISTORY_CACHE = None
        self.day = monitor.today_key()

    def tearDown(self) -> None:
        monitor._USAGE_HISTORY_CACHE = None
        monitor.USAGE_HISTORY_JSON = self.original_history_path
        self.temporary_directory.cleanup()

    def seed_history(self, source: str = "both") -> None:
        monitor.write_json_atomic(
            monitor.USAGE_HISTORY_JSON,
            {
                "schema": 1,
                "days": {
                    self.day: {
                        "date": self.day,
                        "source": source,
                        "requests": 100,
                        "tokens": 1_000_000,
                        "cost": 10.0,
                        "source_date": self.day,
                    }
                },
            },
        )

    def test_combined_usage_accepts_service_day_reset(self) -> None:
        self.seed_history("both")
        state = monitor.MonitorState(
            usage_source="both",
            today_requests=10,
            today_tokens=100_000,
            today_account_cost=1.0,
            client_usage={"date": self.day, "providers": []},
        )

        monitor.update_usage_history(state)

        saved = monitor.load_usage_history()["days"][self.day]
        self.assertEqual(state.today_tokens, 100_000)
        self.assertEqual(saved["tokens"], 100_000)

    def test_monitor_reloads_history_inside_shared_writer_transaction(self) -> None:
        previous_day = (datetime.now(monitor.CN_TZ).date() - timedelta(days=1)).isoformat()
        stale = {
            "schema": 2,
            "days": {
                previous_day: {
                    "date": previous_day,
                    "tokens": 100,
                    "usage_accounting_schema": 0,
                }
            },
        }
        migrated = {
            "schema": 2,
            "days": {
                previous_day: {
                    "date": previous_day,
                    "tokens": 200,
                    "usage_accounting_schema": 1,
                }
            },
        }
        monitor.write_json_atomic(monitor.USAGE_HISTORY_JSON, migrated)
        monitor._USAGE_HISTORY_CACHE = (monitor._usage_history_signature(), stale)
        state = monitor.MonitorState(
            usage_source="local",
            today_requests=1,
            today_tokens=10,
            today_account_cost=0.1,
            client_usage={"date": self.day, "providers": []},
        )

        monitor.update_usage_history(state)
        saved = json.loads(monitor.USAGE_HISTORY_JSON.read_text(encoding="utf-8"))

        self.assertEqual(saved["days"][previous_day]["tokens"], 200)
        self.assertEqual(saved["days"][previous_day]["usage_accounting_schema"], 1)
        self.assertEqual(saved["days"][self.day]["tokens"], 10)

    def test_local_history_high_water_never_mutates_live_state(self) -> None:
        self.seed_history("local")
        state = monitor.MonitorState(
            usage_source="local",
            today_requests=10,
            today_tokens=100_000,
            today_account_cost=1.0,
            client_usage={"date": self.day, "providers": []},
        )

        monitor.update_usage_history(state)

        saved = monitor.load_usage_history()["days"][self.day]
        self.assertEqual(state.today_tokens, 100_000)
        self.assertEqual(saved["tokens"], 1_000_000)

    def test_midnight_rollover_archives_live_overlay_to_previous_day(self) -> None:
        self.seed_history("local")
        next_day = (date.fromisoformat(self.day) + timedelta(days=1)).isoformat()
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        app._current_day_key = self.day
        app.client = MagicMock()
        app.state = monitor.MonitorState(
            usage_source="local",
            today_requests=180,
            today_tokens=1_800_000,
            today_account_cost=18.0,
            client_usage={
                "date": self.day,
                "providers": [
                    {
                        "name": "Codex local - account@example.com",
                        "requests": 180,
                        "tokens": 1_800_000,
                        "cost": 18.0,
                        "models": {"gpt-test": 1_800_000},
                    }
                ],
            },
        )
        app._live_usage_overlay = {"tokens": 800_000}
        app._clear_live_usage_checkpoint = MagicMock()
        app._live_usage_rate_samples = []
        app._account_range_user_selected = False

        with patch.object(monitor, "today_key", return_value=next_day):
            changed = app._handle_day_rollover()

        saved = monitor.load_usage_history()["days"]
        self.assertTrue(changed)
        self.assertEqual(saved[self.day]["tokens"], 1_800_000)
        self.assertEqual(saved[self.day]["requests"], 180)
        self.assertEqual(saved[self.day]["providers"][0]["tokens"], 1_800_000)
        self.assertNotIn(next_day, saved)
        self.assertIsNone(app._live_usage_overlay)

    def test_claude_schema_upgrade_replaces_legacy_history_high_water(self) -> None:
        self.seed_history("local")
        state = monitor.MonitorState(
            usage_source="local",
            today_requests=9,
            today_tokens=900_000,
            today_account_cost=9.0,
            client_usage={
                "date": self.day,
                "claude_usage_schema": client_usage_export.CLAUDE_USAGE_DEDUPE_SCHEMA,
                "providers": [],
            },
        )

        monitor.update_usage_history(state)

        saved = monitor.load_usage_history()["days"][self.day]
        self.assertEqual(saved["tokens"], 900_000)
        self.assertEqual(
            saved["claude_usage_schema"],
            client_usage_export.CLAUDE_USAGE_DEDUPE_SCHEMA,
        )

    def test_cockpit_schema_upgrade_replaces_legacy_history_high_water(self) -> None:
        self.seed_history("local")
        state = monitor.MonitorState(
            usage_source="local",
            today_requests=8,
            today_tokens=800_000,
            today_account_cost=8.0,
            client_usage={
                "date": self.day,
                "cockpit_usage_schema": (
                    client_usage_export.COCKPIT_USAGE_DEDUPE_SCHEMA
                ),
                "providers": [],
            },
        )

        monitor.update_usage_history(state)

        saved = monitor.load_usage_history()["days"][self.day]
        self.assertEqual(saved["tokens"], 800_000)
        self.assertEqual(
            saved["cockpit_usage_schema"],
            client_usage_export.COCKPIT_USAGE_DEDUPE_SCHEMA,
        )

    def test_opencodex_schema_upgrade_replaces_legacy_history_high_water(self) -> None:
        self.seed_history("local")
        state = monitor.MonitorState(
            usage_source="local",
            today_requests=7,
            today_tokens=700_000,
            today_account_cost=7.0,
            client_usage={
                "date": self.day,
                "opencodex_attribution_schema": (
                    client_usage_export.OPENCODEX_ACCOUNT_ATTRIBUTION_SCHEMA
                ),
                "providers": [],
            },
        )

        monitor.update_usage_history(state)

        saved = monitor.load_usage_history()["days"][self.day]
        self.assertEqual(saved["tokens"], 700_000)
        self.assertEqual(
            saved["opencodex_attribution_schema"],
            client_usage_export.OPENCODEX_ACCOUNT_ATTRIBUTION_SCHEMA,
        )

    def test_accounting_schema_upgrade_replaces_high_water_and_source_gap(self) -> None:
        monitor.write_json_atomic(
            monitor.USAGE_HISTORY_JSON,
            {
                "schema": 1,
                "days": {
                    self.day: {
                        "date": self.day,
                        "source": "local",
                        "requests": 4,
                        "tokens": 200,
                        "cost": 2.0,
                        "source_date": self.day,
                        "providers": [
                            {
                                "name": "Codex local - wrong@example.com",
                                "requests": 4,
                                "tokens": 200,
                                "cost": 2.0,
                                "models": {"gpt-old": 200},
                            }
                        ],
                        "models": {"gpt-old": 200},
                        "source_gap": {"tokens": 50, "reason": "legacy"},
                    }
                },
            },
        )
        state = monitor.MonitorState(
            usage_source="local",
            today_requests=3,
            today_tokens=150,
            today_account_cost=1.5,
            client_usage={
                "date": self.day,
                "usage_accounting_schema": 1,
                "providers": [
                    {
                        "name": "Codex local - canonical@example.com",
                        "requests": 3,
                        "tokens": 150,
                        "cost": 1.5,
                        "models": {"gpt-new": 150},
                    }
                ],
            },
        )

        monitor.update_usage_history(state)

        saved = monitor.load_usage_history()["days"][self.day]
        self.assertEqual(saved["tokens"], 150)
        self.assertEqual(saved["requests"], 3)
        self.assertEqual(saved["usage_accounting_schema"], 1)
        self.assertNotIn("source_gap", saved)
        self.assertEqual(saved["models"], {"gpt-new": 150})
        self.assertEqual(
            [row["name"] for row in saved["providers"]],
            ["Codex local - canonical@example.com"],
        )

    def test_same_accounting_schema_keeps_confirmed_high_water(self) -> None:
        self.seed_history("local")
        history = monitor.load_usage_history()
        history["days"][self.day]["usage_accounting_schema"] = 1
        history["days"][self.day]["source_gap"] = {
            "tokens": 25,
            "reason": "confirmed",
        }
        monitor.write_json_atomic(monitor.USAGE_HISTORY_JSON, history)
        state = monitor.MonitorState(
            usage_source="local",
            today_requests=10,
            today_tokens=100_000,
            today_account_cost=1.0,
            client_usage={
                "date": self.day,
                "usage_accounting_schema": 1,
                "providers": [],
            },
        )

        monitor.update_usage_history(state)

        saved = monitor.load_usage_history()["days"][self.day]
        self.assertEqual(saved["tokens"], 1_000_000)
        self.assertEqual(saved["source_gap"]["tokens"], 25)


class AccountUsageSortTests(unittest.TestCase):
    def test_5h_and_7d_sort_recently_used_accounts_first(self) -> None:
        rows = [
            {
                "name": "old-heavy",
                "tokens": 20_000_000,
                "requests": 50,
                "latest_at": "2026-06-25T10:00:00+08:00",
            },
            {
                "name": "current-light",
                "tokens": 1_000,
                "requests": 1,
                "latest_at": "2026-06-25T11:00:00+08:00",
            },
        ]

        ordered_5h = sorted(rows, key=lambda row: monitor.account_usage_sort_key(row, "5h"))
        ordered_7d = sorted(rows, key=lambda row: monitor.account_usage_sort_key(row, "7d"))

        self.assertEqual(ordered_5h[0]["name"], "current-light")
        self.assertEqual(ordered_7d[0]["name"], "current-light")

    def test_today_and_30d_sort_by_tokens_but_cycle_sorts_by_recent_use(self) -> None:
        rows = [
            {
                "name": "recent-light",
                "tokens": 1_000,
                "requests": 100,
                "latest_at": "2026-06-25T11:00:00+08:00",
            },
            {
                "name": "old-heavy",
                "tokens": 20_000_000,
                "requests": 1,
                "latest_at": "2026-06-25T10:00:00+08:00",
            },
        ]

        ordered_today = sorted(rows, key=lambda row: monitor.account_usage_sort_key(row, "today"))
        ordered_30d = sorted(rows, key=lambda row: monitor.account_usage_sort_key(row, "30d"))
        ordered_cycle = sorted(rows, key=lambda row: monitor.account_usage_sort_key(row, "cycle"))

        self.assertEqual(ordered_today[0]["name"], "old-heavy")
        self.assertEqual(ordered_30d[0]["name"], "old-heavy")
        self.assertEqual(ordered_cycle[0]["name"], "recent-light")

    def test_accounts_without_usage_time_sort_after_used_accounts(self) -> None:
        rows = [
            {"name": "never-used", "tokens": 50_000_000},
            {
                "name": "used",
                "tokens": 1_000,
                "latest_at": "2026-06-25T11:00:00+08:00",
            },
        ]

        ordered = sorted(rows, key=lambda row: monitor.account_usage_sort_key(row, "cycle"))

        self.assertEqual([row["name"] for row in ordered], ["used", "never-used"])

    def test_30d_history_sorts_by_total_usage_when_exact_time_is_missing(self) -> None:
        recent_day = monitor.date_key(1)
        old_day = monitor.date_key(10)
        history = {
            "days": {
                old_day: {
                    "requests": 50,
                    "tokens": 20_000_000,
                    "cost": 20.0,
                    "updated_at": f"{old_day}T18:00:00+08:00",
                    "providers": [
                        {
                            "name": "old-heavy",
                            "requests": 50,
                            "tokens": 20_000_000,
                            "cost": 20.0,
                        }
                    ],
                },
                recent_day: {
                    "requests": 1,
                    "tokens": 1_000,
                    "cost": 0.01,
                    "updated_at": f"{recent_day}T10:00:00+08:00",
                    "providers": [
                        {
                            "name": "recent-light",
                            "requests": 1,
                            "tokens": 1_000,
                            "cost": 0.01,
                        }
                    ],
                },
            }
        }
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)

        with patch.object(monitor, "load_usage_history", return_value=history):
            rows = app._usage_range_providers("30d")
        ordered = sorted(rows, key=lambda row: monitor.account_usage_sort_key(row, "30d"))

        self.assertEqual([row["name"] for row in ordered], ["old-heavy", "recent-light"])

    def test_window_only_accounts_are_hidden_only_from_today(self) -> None:
        row = {
            "name": "Codex local - previous@example.com",
            "window_only": True,
        }

        self.assertFalse(monitor.account_row_available_for_range(row, "today"))
        self.assertTrue(monitor.account_row_available_for_range(row, "5h"))
        self.assertTrue(monitor.account_row_available_for_range(row, "7d"))
        self.assertTrue(monitor.account_row_available_for_range(row, "cycle"))

    def test_5h_quota_tab_excludes_unlimited_and_analysis_only_rows(self) -> None:
        self.assertFalse(
            monitor.account_has_5h_quota(
                {
                    "window_5h": {
                        "quota_available": False,
                        "quota_unlimited": True,
                        "tokens": 123_456,
                    }
                }
            )
        )
        self.assertFalse(
            monitor.account_has_5h_quota(
                {"window_5h": {"quota_available": False, "tokens": 123_456}}
            )
        )
        self.assertTrue(
            monitor.account_has_5h_quota(
                {"window_5h": {"quota_available": True, "window_minutes": 300}}
            )
        )

    def test_cycle_quota_tab_requires_a_real_cycle_account(self) -> None:
        self.assertFalse(monitor.account_has_cycle_quota_window({"window_cycle": {}}))
        self.assertFalse(
            monitor.account_has_cycle_quota_window(
                {"window_cycle": {"window_minutes": 7 * 24 * 60}}
            )
        )
        self.assertTrue(
            monitor.account_has_cycle_quota_window(
                {"window_cycle": {"quota_available": True}}
            )
        )
        self.assertTrue(
            monitor.account_has_cycle_quota_window(
                {"window_cycle": {"quota_available": False, "window_days": 30.4}}
            )
        )
        self.assertFalse(
            monitor.account_has_cycle_quota_window(
                {
                    "is_api_service_aggregate": True,
                    "window_cycle": {"quota_available": True, "window_days": 30.4},
                }
            )
        )

    def test_unattributed_gap_is_not_a_ranked_account(self) -> None:
        row = {"name": "Pending attribution", "is_unattributed_gap": True}

        self.assertFalse(monitor.account_row_available_for_range(row, "today"))
        self.assertFalse(monitor.account_row_available_for_range(row, "7d"))

    def test_window_stats_add_only_missing_email_accounts(self) -> None:
        existing = "Codex local - current@example.com"
        missing = "Codex local - previous@example.com"
        labels = client_usage_export.window_only_provider_labels(
            {
                existing: {"window_7d": {"tokens": 100}},
                missing: {"window_7d": {"tokens": 200}},
                "Codex local - api-key-test": {"window_7d": {"tokens": 300}},
            },
            {existing: client_usage_export.UsageBucket()},
        )

        self.assertEqual(labels, {missing})


class AccountDisplayRetentionTests(unittest.TestCase):
    def test_only_inactive_account_without_weekly_quota_is_hidden(self) -> None:
        now = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
        inactive = {"name": "Codex local - inactive@example.com", "tokens": 9_000_000}
        recent_request = {"name": "Codex local - request@example.com"}
        recent_tokens = {"name": "Codex local - tokens@example.com"}
        quota_only = {
            "name": "Codex local - quota@example.com",
            "window_7d": {
                "quota_available": True,
                "quota_stale": False,
                "remaining_percent": 100.0,
            },
        }

        self.assertFalse(
            monitor.account_should_remain_visible(inactive, {}, now=now)
        )
        self.assertTrue(
            monitor.account_should_remain_visible(
                recent_request,
                {"requests": 1, "tokens": 0},
                now=now,
            )
        )
        self.assertTrue(
            monitor.account_should_remain_visible(
                recent_tokens,
                {"requests": 0, "tokens": 100},
                now=now,
            )
        )
        self.assertTrue(
            monitor.account_should_remain_visible(quota_only, {}, now=now)
        )

    def test_stale_weekly_quota_does_not_keep_inactive_account(self) -> None:
        row = {
            "name": "Codex local - stale@example.com",
            "window_7d": {
                "quota_available": True,
                "quota_stale": True,
                "remaining_percent": 80.0,
            },
        }

        self.assertFalse(monitor.account_should_remain_visible(row, {}))

    def test_account_cumulative_filter_keeps_recent_quota_and_gap_rows(self) -> None:
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        app.state = monitor.MonitorState(
            today_tokens=123_456,
            top_accounts=[
                {"name": "old@example.com", "window_7d": {}},
                {
                    "name": "quota@example.com",
                    "window_7d": {
                        "quota_available": True,
                        "quota_stale": False,
                        "utilization": 0.0,
                    },
                },
            ],
        )
        cumulative_rows = [
            {"name": "Codex local - old@example.com", "tokens": 9_000_000},
            {"name": "Codex local - recent@example.com", "tokens": 8_000_000},
            {"name": "Codex local - quota@example.com", "tokens": 7_000_000},
            {"name": "Historical detail gap", "tokens": 6_000_000},
        ]
        recent_rows = [
            {
                "name": "Codex local - recent@example.com",
                "requests": 2,
                "tokens": 200,
            }
        ]

        with patch.object(app, "_usage_range_providers", return_value=recent_rows):
            visible = app._filter_account_display_rows(cumulative_rows)

        self.assertEqual(
            [row["name"] for row in visible],
            [
                "Codex local - recent@example.com",
                "Codex local - quota@example.com",
                "Historical detail gap",
            ],
        )
        self.assertEqual(app.state.today_tokens, 123_456)
        self.assertEqual(cumulative_rows[0]["tokens"], 9_000_000)


class ApiServicePoolAggregateTests(unittest.TestCase):
    def test_api_service_pool_row_sums_pool_accounts(self) -> None:
        rows = [
            {
                "name": "tissue",
                "tokens": 700,
                "requests": 7,
                "cost": 0.7,
                "latest_at": "2026-06-29T09:20:00+08:00",
                "latest_model": "gpt-5.4",
                "window_5h": {
                    "tokens": 650,
                    "requests": 6,
                    "cost": 0.65,
                    "remaining_percent": 99.0,
                    "utilization": 1.0,
                    "quota_available": True,
                    "latest_at": "2026-06-29T09:20:00+08:00",
                },
            },
            {
                "name": "hails",
                "tokens": 300,
                "requests": 3,
                "cost": 0.3,
                "latest_at": "2026-06-29T09:23:00+08:00",
                "latest_model": "gpt-5.5",
                "window_5h": {
                    "tokens": 300,
                    "requests": 3,
                    "cost": 0.3,
                    "remaining_percent": 98.0,
                    "utilization": 2.0,
                    "quota_available": True,
                    "latest_at": "2026-06-29T09:23:00+08:00",
                },
            },
        ]

        aggregate = monitor.build_api_service_pool_row(rows)

        self.assertIsNotNone(aggregate)
        assert aggregate is not None
        self.assertEqual(aggregate["tokens"], 1000)
        self.assertEqual(aggregate["requests"], 10)
        self.assertAlmostEqual(aggregate["cost"], 1.0)
        self.assertEqual(aggregate["latest_at"], "2026-06-29T09:23:00+08:00")
        self.assertEqual(aggregate["latest_model"], "gpt-5.5")
        self.assertEqual(aggregate["window_5h"]["tokens"], 950)
        self.assertNotIn("quota_available", aggregate["window_5h"])
        self.assertNotIn("remaining_percent", aggregate["window_5h"])
        self.assertNotIn("utilization", aggregate["window_5h"])

    def test_api_service_local_mirror_is_subtracted_from_client_usage(self) -> None:
        usage = {
            "requests": 11,
            "tokens": 1100,
            "cost": 1.1,
            "providers": [
                {
                    "name": "Codex local - api-service-local",
                    "requests": 10,
                    "tokens": 1000,
                    "cost": 1.0,
                },
                {
                    "name": "Codex local - direct-account",
                    "requests": 1,
                    "tokens": 100,
                    "cost": 0.1,
                },
            ],
        }

        result = monitor.subtract_sub2api_mirrored_api_key_usage(usage, 1000, {})

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["tokens"], 100)
        self.assertEqual(len(result["providers"]), 1)
        self.assertEqual(result["providers"][0]["name"], "Codex local - direct-account")

    def test_account_row_pool_filter_uses_manifest_emails(self) -> None:
        pool = {"fixture-b@example.com", "fixture-c+g5@example.com"}

        self.assertTrue(
            monitor.account_row_matches_pool(
                {"name": "Codex local - fixture-b@example.com"},
                pool,
            )
        )
        self.assertFalse(
            monitor.account_row_matches_pool(
                {"name": "Codex local - fixture-d@example.com"},
                pool,
            )
        )


class LocalActiveAccountTests(unittest.TestCase):
    def test_active_accounts_are_deduped_by_active_sessions(self) -> None:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        usage = {
            "active_sessions": [
                {
                    "session_id": "session-1",
                    "provider": "Codex local - fixture-b@example.com",
                    "model": "gpt-5.5",
                    "latest_at": now,
                }
            ],
            "providers": [
                {
                    "name": "Codex local - fixture-b@example.com",
                    "latest_at": now,
                    "latest_model": "gpt-5.5",
                    "recent_sessions": 0,
                },
                {
                    "name": "Codex local - api-service-local",
                    "latest_at": now,
                    "latest_model": "gpt-5.5",
                    "recent_sessions": 0,
                },
                {
                    "name": "Codex local - codex_local_access_runtime",
                    "latest_at": now,
                    "latest_model": "gpt-5.5",
                    "recent_sessions": 0,
                },
            ],
        }

        active = monitor.local_active_accounts_from_client_usage(usage)

        self.assertEqual(len(active), 1)
        self.assertIn("fixture-b@example.com", active[0]["name"])
        self.assertEqual(active[0]["current"], 1)

    def test_lifecycle_active_session_does_not_expire_by_token_timestamp(self) -> None:
        old_timestamp = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds")
        usage = {
            "active_sessions": [
                {
                    "session_id": "session-running",
                    "provider": "Codex local - account@example.com",
                    "model": "gpt-test",
                    "latest_at": old_timestamp,
                    "active": True,
                    "activity_source": "task-lifecycle",
                }
            ],
            "providers": [],
        }

        active = monitor.local_active_accounts_from_client_usage(usage)

        self.assertEqual(len(active), 1)
        self.assertIn("account@example.com", active[0]["name"])

    def test_explicitly_inactive_session_is_not_shown(self) -> None:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        usage = {
            "active_sessions": [
                {
                    "session_id": "session-complete",
                    "provider": "Codex local - account@example.com",
                    "latest_at": now,
                    "active": False,
                }
            ],
            "providers": [],
        }

        self.assertEqual(monitor.local_active_accounts_from_client_usage(usage), [])

    def test_latest_request_provider_is_first_when_it_is_still_active(self) -> None:
        now = datetime.now(timezone.utc)
        usage = {
            "latest_request": {
                "provider": "Codex local - hails@example.com",
                "created_at": now.isoformat(timespec="seconds"),
            },
            "active_sessions": [
                {
                    "session_id": "session-ginny",
                    "provider": "Codex local - ginny@example.com",
                    "latest_at": now.isoformat(timespec="seconds"),
                    "active": True,
                },
                {
                    "session_id": "session-hails",
                    "provider": "Codex local - hails@example.com",
                    "latest_at": (now - timedelta(seconds=5)).isoformat(timespec="seconds"),
                    "active": True,
                },
            ],
            "providers": [],
        }

        active = monitor.local_active_accounts_from_client_usage(usage)

        self.assertEqual(len(active), 2)
        self.assertIn("hails@example.com", active[0]["name"])

    def test_recent_provider_without_recent_session_is_not_active(self) -> None:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        usage = {
            "providers": [
                {
                    "name": "Codex local - stale-provider",
                    "latest_at": now,
                    "latest_model": "gpt-5.5",
                    "recent_sessions": 0,
                }
            ],
            "latest_request": {},
        }

        active = monitor.local_active_accounts_from_client_usage(usage)

        self.assertEqual(active, [])

    def test_active_sessions_survive_client_usage_loading(self) -> None:
        session = {
            "session_id": "session-1",
            "provider": "Codex local - account@example.com",
            "model": "gpt-test",
            "latest_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        payload = {
            "date": monitor.today_key(),
            "today": {"requests": 1, "tokens": 100, "cost": 0.1},
            "providers": [],
            "active_sessions": [session],
            "latest_request": {},
            "updated_at": session["latest_at"],
        }
        with (
            tempfile.TemporaryDirectory() as temporary_directory,
            patch.object(monitor, "CLIENT_USAGE_EXPORT", Path(temporary_directory) / "missing.py"),
            patch.object(monitor, "CLIENT_USAGE_JSON", Path(temporary_directory) / "usage.json"),
        ):
            monitor.CLIENT_USAGE_JSON.write_text(json.dumps(payload), encoding="utf-8")
            usage = monitor.load_client_usage()

        self.assertEqual(usage["active_sessions"], [session])

    def test_detailed_usage_history_keeps_provider_and_model_totals(self) -> None:
        details = monitor.detailed_usage_from_client_usage(
            {
                "providers": [
                    {
                        "name": "Codex local - account@example.com",
                        "requests": 3,
                        "tokens": 5_000,
                        "cost": 4.5,
                        "models": {"gpt-5.6-sol": 5_000},
                    }
                ]
            }
        )

        self.assertEqual(details["models"], {"gpt-5.6-sol": 5_000})
        self.assertEqual(details["providers"][0]["tokens"], 5_000)

    def test_sub2api_account_details_match_combined_today_total(self) -> None:
        state = monitor.MonitorState(
            usage_source="both",
            today_requests=10,
            today_tokens=1_000,
            today_account_cost=2.0,
            client_usage={
                "tokens": 100,
                "providers": [
                    {
                        "name": "Codex local - direct@example.com",
                        "requests": 1,
                        "tokens": 100,
                        "input_tokens": 100,
                        "cost": 0.2,
                        "models": {"gpt-local": 100},
                    }
                ],
            },
            top_accounts=[
                {
                    "name": "pool@example.com",
                    "source_badge": "SUB",
                    "requests": 9,
                    "tokens": 900,
                    "cost": 1.8,
                },
                {
                    "name": "Codex local - direct@example.com",
                    "source_badge": "LOCAL",
                    "requests": 1,
                    "tokens": 100,
                    "cost": 0.2,
                    "models": {"gpt-local": 100},
                },
                {
                    "name": "API service pool",
                    "requests": 9,
                    "tokens": 900,
                    "cost": 1.8,
                    "is_pool_aggregate": True,
                },
            ],
        )
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        app.state = state
        app._live_usage_overlay = None

        providers = app._usage_range_providers("24h")
        summary = app._usage_range_summary("24h")
        mix = app._summary_token_mix(summary)

        self.assertEqual(sum(int(row["tokens"]) for row in providers), 1_000)
        self.assertEqual({row["name"] for row in providers}, {"pool@example.com", "Codex local - direct@example.com"})
        self.assertEqual(summary["tokens"], 1_000)
        self.assertEqual(mix["unknown"], 900)

    def test_usage_history_excludes_pool_aggregate_but_keeps_pool_accounts(self) -> None:
        state = monitor.MonitorState(
            usage_source="both",
            today_requests=2,
            today_tokens=1_000,
            today_account_cost=1.0,
            client_usage={"date": monitor.today_key(), "providers": []},
            top_accounts=[
                {"name": "a@example.com", "requests": 1, "tokens": 600, "cost": 0.6},
                {"name": "b@example.com", "requests": 1, "tokens": 400, "cost": 0.4},
                {
                    "name": "API service pool",
                    "requests": 2,
                    "tokens": 1_000,
                    "cost": 1.0,
                    "is_pool_aggregate": True,
                },
            ],
        )
        with (
            tempfile.TemporaryDirectory() as temporary_directory,
            patch.object(monitor, "USAGE_HISTORY_JSON", Path(temporary_directory) / "history.json"),
        ):
            monitor.update_usage_history(state)
            saved = monitor.load_usage_history()["days"][monitor.today_key()]

        self.assertEqual(sum(int(row["tokens"]) for row in saved["providers"]), 1_000)
        self.assertEqual({row["name"] for row in saved["providers"]}, {"a@example.com", "b@example.com"})

    def test_usage_range_accounts_and_models_use_the_same_history_days(self) -> None:
        app = object.__new__(monitor.FloatingMonitorApp)
        app.state = monitor.MonitorState(client_usage={"providers": []})
        today = monitor.today_key()
        old_day = (datetime.now(monitor.CN_TZ).date() - timedelta(days=8)).isoformat()
        history = {
            "schema": 2,
            "days": {
                today: {
                    "requests": 1,
                    "tokens": 1_000,
                    "cost": 1.0,
                    "providers": [
                        {
                            "name": "today@example.com",
                            "requests": 1,
                            "tokens": 1_000,
                            "cost": 1.0,
                            "models": {"today-model": 1_000},
                        }
                    ],
                },
                old_day: {
                    "requests": 1,
                    "tokens": 9_000,
                    "cost": 9.0,
                    "providers": [
                        {
                            "name": "old@example.com",
                            "requests": 1,
                            "tokens": 9_000,
                            "cost": 9.0,
                            "models": {"old-model": 9_000},
                        }
                    ],
                },
            },
        }
        with (
            tempfile.TemporaryDirectory() as temporary_directory,
            patch.object(monitor, "USAGE_HISTORY_JSON", Path(temporary_directory) / "history.json"),
        ):
            monitor.USAGE_HISTORY_JSON.write_text(json.dumps(history), encoding="utf-8")
            seven_day = app._usage_range_providers("7d")
            seven_day_models = app._top_models("7d")
            all_time = app._usage_range_providers("all")

        self.assertEqual([row["name"] for row in seven_day], ["today@example.com"])
        self.assertEqual(seven_day_models, [("today-model", 1_000)])
        self.assertEqual(sum(int(row["tokens"]) for row in all_time), 10_000)

    def test_7d_history_fallback_adds_only_missing_real_accounts(self) -> None:
        app = object.__new__(monitor.FloatingMonitorApp)
        app.state = monitor.MonitorState(
            client_usage={"providers": []},
            top_accounts=[{"name": "Codex local - current@example.com"}],
        )
        history = {
            "schema": 2,
            "days": {
                monitor.today_key(): {
                    "requests": 4,
                    "tokens": 1_000,
                    "cost": 1.0,
                    "providers": [
                        {"name": "Codex local - current@example.com", "requests": 1, "tokens": 100, "cost": 0.1},
                        {"name": "Codex local - previous@example.com", "requests": 2, "tokens": 800, "cost": 0.8},
                        {"name": "Codex local - api-key-test", "requests": 1, "tokens": 100, "cost": 0.1},
                    ],
                }
            },
        }
        with (
            tempfile.TemporaryDirectory() as temporary_directory,
            patch.object(monitor, "USAGE_HISTORY_JSON", Path(temporary_directory) / "history.json"),
        ):
            monitor.USAGE_HISTORY_JSON.write_text(json.dumps(history), encoding="utf-8")
            fallback = app._history_7d_fallback_rows(
                [{"name": "Codex local - current@example.com"}]
            )

        self.assertEqual([row["name"] for row in fallback], ["Codex local - previous@example.com"])
        self.assertEqual(fallback[0]["tokens"], 800)
        self.assertTrue(fallback[0]["historical_fallback"])

    def test_local_30d_account_rows_use_history_without_export_scan(self) -> None:
        app = object.__new__(monitor.FloatingMonitorApp)
        app.state = monitor.MonitorState(
            client_usage={"providers": []},
            top_accounts=[
                {
                    "name": "Codex local - account@example.com",
                    "source_badge": "LOCAL",
                }
            ],
        )
        today = monitor.today_key()
        history = {
            "schema": 2,
            "days": {
                today: {
                    "requests": 3,
                    "tokens": 1_000,
                    "cost": 1.0,
                    "providers": [
                        {
                            "name": "Codex local - account@example.com",
                            "requests": 2,
                            "tokens": 900,
                            "cost": 0.9,
                            "models": {"gpt-test": 900},
                        }
                    ],
                }
            },
        }
        with (
            tempfile.TemporaryDirectory() as temporary_directory,
            patch.object(monitor, "USAGE_HISTORY_JSON", Path(temporary_directory) / "history.json"),
        ):
            monitor.USAGE_HISTORY_JSON.write_text(json.dumps(history), encoding="utf-8")
            rows = app._history_account_rows("30d")

        self.assertEqual(sum(int(row["tokens"]) for row in rows), 1_000)
        account = next(row for row in rows if row["name"].endswith("account@example.com"))
        gap = next(row for row in rows if row.get("is_history_detail_gap"))
        self.assertEqual(account["tokens"], 900)
        self.assertEqual(account["source_badge"], "LOCAL")
        self.assertEqual(gap["name"], "历史明细缺口")
        self.assertEqual(gap["tokens"], 100)
        self.assertFalse(app._needs_server_account_30d())
        app.state.top_accounts.append({"name": "server-account", "source_badge": "SUB"})
        self.assertTrue(app._needs_server_account_30d())

    def test_client_usage_cache_never_requests_expensive_local_30d_scan(self) -> None:
        client = monitor.Sub2APIClient()
        client.include_account_30d = True
        with patch.object(monitor, "load_client_usage", return_value={"providers": []}) as loader:
            client._load_client_usage_cached()

        loader.assert_called_once_with(
            include_30d=False,
            backfill_history_details=False,
        )


class LatestRequestFallbackTests(unittest.TestCase):
    def test_account_fallback_events_merge_after_direct_bucket_latest(self) -> None:
        label = "Codex local - account@example.com"
        direct = client_usage_export.UsageBucket()
        direct.requests = 1
        direct.input_tokens = 100
        direct.mark_latest(datetime(2026, 7, 8, 16, 21, 22), "gpt-old")

        old_event = client_usage_export.UsageEvent(
            when=datetime(2026, 7, 8, 16, 20, 0),
            model="gpt-old",
            input_tokens=900,
            cached_tokens=0,
            output_tokens=100,
        )
        new_event = client_usage_export.UsageEvent(
            when=datetime(2026, 7, 8, 17, 22, 34),
            model="gpt-new",
            input_tokens=2000,
            cached_tokens=0,
            output_tokens=300,
            request_at=datetime(2026, 7, 8, 17, 22, 30),
        )

        client_usage_export.merge_codex_account_fallback_events(
            {label: direct},
            {label: [old_event, new_event]},
            {label: 1.0},
        )

        self.assertEqual(direct.requests, 2)
        self.assertEqual(direct.total_tokens, 2400)
        self.assertEqual(direct.latest_model, "gpt-new")
        self.assertEqual(direct.latest_at, datetime(2026, 7, 8, 17, 22, 30))

    def test_latest_request_from_attributed_events_uses_newest_event(self) -> None:
        older = client_usage_export.UsageEvent(
            when=datetime(2026, 7, 1, 22, 0, 0),
            model="gpt-old",
            input_tokens=100,
            cached_tokens=0,
            output_tokens=1,
            session_id="older",
        )
        newer = client_usage_export.UsageEvent(
            when=datetime(2026, 7, 1, 22, 3, 44),
            model="gpt-new",
            input_tokens=200,
            cached_tokens=0,
            output_tokens=2,
            session_id="newer",
        )

        latest = client_usage_export.latest_request_from_attributed_events(
            {
                "Codex local - old@example.com": [older],
                "Codex local - new@example.com": [newer],
            }
        )

        self.assertEqual(latest["provider"], "Codex local - new@example.com")
        self.assertEqual(latest["model"], "gpt-new")
        self.assertTrue(latest["created_at"].startswith("2026-07-01T22:03:44"))

    def test_latest_request_prefers_email_label_on_tie(self) -> None:
        event = client_usage_export.UsageEvent(
            when=datetime(2026, 7, 1, 22, 0, 0),
            model="gpt-test",
            input_tokens=100,
            cached_tokens=0,
            output_tokens=1,
            session_id="same",
        )

        latest = client_usage_export.latest_request_from_attributed_events(
            {
                "Codex local - api-service-local": [event],
                "Codex local - account@example.com": [event],
            }
        )

        self.assertEqual(latest["provider"], "Codex local - account@example.com")

    def test_api_service_latest_request_resolves_concrete_pool_account(self) -> None:
        event = client_usage_export.UsageEvent(
            when=datetime(2026, 7, 11, 20, 15, 29, 616000),
            model="gpt-test",
            input_tokens=250_000,
            cached_tokens=10_000,
            output_tokens=562,
            session_id="api-session",
        )
        markers = [
            client_usage_export.AccountMarker(
                when=datetime(2026, 7, 11, 20, 15, 29, 382000),
                label="Codex local - wrong@example.com",
                total_tokens=88_798,
            ),
            client_usage_export.AccountMarker(
                when=datetime(2026, 7, 11, 20, 15, 29, 615000),
                label="Codex local - matched@example.com",
                total_tokens=260_562,
            ),
        ]

        latest = client_usage_export.latest_request_from_attributed_events(
            {"Codex local - api-service-local": [event]},
            markers,
        )

        self.assertEqual(latest["provider"], "Codex local - matched@example.com")

    def test_api_service_account_match_allows_delayed_client_event(self) -> None:
        event = client_usage_export.UsageEvent(
            when=datetime(2026, 7, 11, 21, 0, 32, 334000),
            model="gpt-test",
            input_tokens=4_076,
            cached_tokens=17_152,
            output_tokens=1_005,
            session_id="delayed-session",
        )
        marker = client_usage_export.AccountMarker(
            when=datetime(2026, 7, 11, 20, 58, 16, 156000),
            label="Codex local - delayed@example.com",
            total_tokens=22_233,
        )

        label = client_usage_export.concrete_api_service_account_label(event, [marker])

        self.assertEqual(label, "Codex local - delayed@example.com")

    def test_api_service_account_match_uses_response_time_after_manual_switch(self) -> None:
        event = client_usage_export.UsageEvent(
            when=datetime(2026, 7, 11, 21, 6, 0),
            request_at=datetime(2026, 7, 11, 21, 0, 0),
            model="gpt-test",
            input_tokens=90_000,
            cached_tokens=9_000,
            output_tokens=1_000,
            session_id="long-api-session",
        )
        markers = [
            client_usage_export.AccountMarker(
                when=datetime(2026, 7, 11, 21, 0, 0),
                label="Codex local - wrong-at-start@example.com",
                total_tokens=100_000,
            ),
            client_usage_export.AccountMarker(
                when=datetime(2026, 7, 11, 21, 6, 0),
                label="Codex local - response-owner@example.com",
                total_tokens=100_000,
            ),
        ]

        label = client_usage_export.concrete_api_service_account_label(event, markers)

        self.assertEqual(label, "Codex local - response-owner@example.com")

    def test_api_service_account_match_allows_small_total_token_difference(self) -> None:
        event = client_usage_export.UsageEvent(
            when=datetime(2026, 7, 12, 0, 0, 21, 677000),
            model="gpt-test",
            input_tokens=1_032,
            cached_tokens=205_568,
            output_tokens=41,
            session_id="midnight-session",
        )
        marker = client_usage_export.AccountMarker(
            when=datetime(2026, 7, 12, 0, 0, 31, 298000),
            label="Codex local - midnight@example.com",
            total_tokens=206_708,
        )

        label = client_usage_export.concrete_api_service_account_label(event, [marker])

        self.assertEqual(label, "Codex local - midnight@example.com")

    def test_api_service_time_index_preserves_fuzzy_match_result(self) -> None:
        event = client_usage_export.UsageEvent(
            when=datetime(2026, 7, 12, 0, 0, 21, 677000),
            model="gpt-test",
            input_tokens=1_032,
            cached_tokens=205_568,
            output_tokens=41,
            session_id="indexed-session",
        )
        markers = [
            client_usage_export.AccountMarker(
                when=event.when - timedelta(hours=3) + timedelta(seconds=index),
                label=f"Codex local - distractor-{index}@example.com",
                total_tokens=event.total_tokens + 100,
            )
            for index in range(200)
        ]
        expected = client_usage_export.AccountMarker(
            when=event.when + timedelta(seconds=9),
            label="Codex local - indexed@example.com",
            total_tokens=event.total_tokens + 100,
        )
        markers.insert(75, expected)

        legacy = client_usage_export.concrete_api_service_account_marker(
            event,
            markers,
        )
        indexed = client_usage_export.concrete_api_service_account_marker(
            event,
            markers,
            client_usage_export.account_markers_by_total_tokens(markers),
        )

        self.assertIs(legacy, expected)
        self.assertIs(indexed, expected)

    def test_api_service_time_index_matches_brute_force_across_many_events(self) -> None:
        base = datetime(2026, 7, 12, 12, 0, 0)
        markers = [
            client_usage_export.AccountMarker(
                when=base + timedelta(seconds=index * 3 - 450),
                label=f"Codex local - account-{index}@example.com",
                total_tokens=20_000 + (index % 19) * 137,
            )
            for index in range(300)
        ]
        marker_index = client_usage_export.account_markers_by_total_tokens(markers)
        for index in range(120):
            total_tokens = 20_000 + (index % 23) * 131
            event = client_usage_export.UsageEvent(
                when=base + timedelta(seconds=index * 5 - 300),
                model="gpt-test",
                input_tokens=total_tokens - 1,
                cached_tokens=0,
                output_tokens=1,
            )
            brute = client_usage_export.concrete_api_service_account_marker(
                event,
                markers,
            )
            indexed = client_usage_export.concrete_api_service_account_marker(
                event,
                markers,
                marker_index,
            )
            self.assertIs(indexed, brute)

    def test_api_service_latest_request_does_not_reuse_stale_session_account(self) -> None:
        event = client_usage_export.UsageEvent(
            when=datetime(2026, 7, 11, 21, 0, 0),
            model="gpt-test",
            input_tokens=100,
            cached_tokens=0,
            output_tokens=1,
            session_id="known-session",
        )

        latest = client_usage_export.latest_request_from_attributed_events(
            {"Codex local - api-service-local": [event]},
            [],
            {"known-session": "Codex local - confirmed@example.com"},
        )

        self.assertEqual(latest["provider"], client_usage_export.API_SERVICE_AGGREGATE_LABEL)

    def test_api_service_events_are_moved_to_concrete_accounts_without_duplication(self) -> None:
        turn_started_at = datetime(2026, 7, 12, 7, 59, 30)
        first = client_usage_export.UsageEvent(
            when=datetime(2026, 7, 12, 8, 0, 0),
            model="gpt-test",
            input_tokens=900,
            cached_tokens=0,
            output_tokens=100,
            session_id="session-1",
            account_at=turn_started_at,
        )
        second = client_usage_export.UsageEvent(
            when=datetime(2026, 7, 12, 8, 1, 0),
            model="gpt-test",
            input_tokens=1_800,
            cached_tokens=0,
            output_tokens=200,
            session_id="session-1",
            account_at=turn_started_at,
        )
        marker = client_usage_export.AccountMarker(
            when=first.when,
            label="Codex local - account@example.com",
            model="gpt-5.6-sol",
            total_tokens=1_000,
        )

        resolved, session_accounts, unresolved = client_usage_export.resolve_api_service_event_accounts(
            {"Codex local - api-service-local": [first, second]},
            [marker],
        )

        self.assertEqual(list(resolved), ["Codex local - account@example.com"])
        self.assertEqual(sum(event.total_tokens for events in resolved.values() for event in events), 3_000)
        self.assertEqual(session_accounts["session-1"], "Codex local - account@example.com")
        self.assertEqual(unresolved, 0)
        self.assertEqual(first.model, "gpt-5.6-sol")

    def test_api_service_turn_uses_unique_near_time_marker_when_token_totals_differ(self) -> None:
        turn_started_at = datetime(2026, 7, 23, 9, 56, 30)
        first = client_usage_export.UsageEvent(
            when=datetime(2026, 7, 23, 9, 56, 50),
            model="gpt-test",
            input_tokens=61_000,
            cached_tokens=50_000,
            output_tokens=500,
            session_id="session-time-fallback",
            account_at=turn_started_at,
        )
        second = client_usage_export.UsageEvent(
            when=datetime(2026, 7, 23, 9, 57, 6, 553000),
            model="gpt-test",
            input_tokens=123_000,
            cached_tokens=100_000,
            output_tokens=1_305,
            session_id="session-time-fallback",
            account_at=turn_started_at,
        )
        marker = client_usage_export.AccountMarker(
            when=datetime(2026, 7, 23, 9, 57, 6),
            label="Codex local - final-plus@example.com",
            model="gpt-5.6-sol",
            total_tokens=8_786,
            input_tokens=8_000,
            cached_tokens=7_000,
            output_tokens=786,
        )
        expected_total = first.total_tokens + second.total_tokens

        resolved, session_accounts, unresolved = (
            client_usage_export.resolve_api_service_event_accounts(
                {client_usage_export.API_SERVICE_AGGREGATE_LABEL: [first, second]},
                [marker],
            )
        )

        self.assertEqual(resolved[marker.label], [first, second])
        self.assertEqual(
            sum(event.total_tokens for events in resolved.values() for event in events),
            expected_total,
        )
        self.assertEqual(session_accounts["session-time-fallback"], marker.label)
        self.assertEqual(unresolved, 0)

    def test_api_service_near_time_marker_is_given_to_closest_concurrent_turn(self) -> None:
        base = datetime(2026, 7, 23, 10, 0, 0)
        closest = client_usage_export.UsageEvent(
            when=base,
            model="gpt-test",
            input_tokens=100_000,
            cached_tokens=90_000,
            output_tokens=1_000,
            session_id="closest-session",
            account_at=base - timedelta(seconds=10),
        )
        other = client_usage_export.UsageEvent(
            when=base + timedelta(milliseconds=350),
            model="gpt-test",
            input_tokens=200_000,
            cached_tokens=180_000,
            output_tokens=2_000,
            session_id="other-session",
            account_at=base - timedelta(seconds=9),
        )
        marker = client_usage_export.AccountMarker(
            when=base + timedelta(milliseconds=50),
            label="Codex local - closest@example.com",
            total_tokens=7_777,
        )

        resolved, session_accounts, unresolved = (
            client_usage_export.resolve_api_service_event_accounts(
                {client_usage_export.API_SERVICE_AGGREGATE_LABEL: [closest, other]},
                [marker],
            )
        )

        self.assertEqual(resolved[marker.label], [closest])
        self.assertEqual(
            resolved[client_usage_export.API_SERVICE_AGGREGATE_LABEL],
            [other],
        )
        self.assertEqual(session_accounts["closest-session"], marker.label)
        self.assertNotIn("other-session", session_accounts)
        self.assertEqual(unresolved, 1)

    def test_api_service_near_time_marker_stays_unresolved_when_turns_are_ambiguous(self) -> None:
        base = datetime(2026, 7, 23, 10, 10, 0)
        events = [
            client_usage_export.UsageEvent(
                when=base + timedelta(milliseconds=offset),
                model="gpt-test",
                input_tokens=100_000 + offset,
                cached_tokens=90_000,
                output_tokens=1_000,
                session_id=f"ambiguous-{offset}",
                account_at=base - timedelta(seconds=10 - offset / 1000),
            )
            for offset in (0, 100)
        ]
        marker = client_usage_export.AccountMarker(
            when=base + timedelta(milliseconds=50),
            label="Codex local - ambiguous@example.com",
            total_tokens=7_777,
        )

        resolved, session_accounts, unresolved = (
            client_usage_export.resolve_api_service_event_accounts(
                {client_usage_export.API_SERVICE_AGGREGATE_LABEL: events},
                [marker],
            )
        )

        self.assertEqual(
            resolved[client_usage_export.API_SERVICE_AGGREGATE_LABEL],
            events,
        )
        self.assertEqual(session_accounts, {})
        self.assertEqual(unresolved, 2)

    def test_api_service_near_time_fallback_ignores_zero_usage_marker(self) -> None:
        event = client_usage_export.UsageEvent(
            when=datetime(2026, 7, 23, 10, 20, 0),
            model="gpt-test",
            input_tokens=10_000,
            cached_tokens=9_000,
            output_tokens=100,
            session_id="zero-marker-session",
            account_at=datetime(2026, 7, 23, 10, 19, 30),
        )
        marker = client_usage_export.AccountMarker(
            when=event.when,
            label="Codex local - failed-k12@example.com",
        )

        resolved, session_accounts, unresolved = (
            client_usage_export.resolve_api_service_event_accounts(
                {client_usage_export.API_SERVICE_AGGREGATE_LABEL: [event]},
                [marker],
            )
        )

        self.assertEqual(
            resolved[client_usage_export.API_SERVICE_AGGREGATE_LABEL],
            [event],
        )
        self.assertEqual(session_accounts, {})
        self.assertEqual(unresolved, 1)

    def test_api_service_final_evidence_overrides_stale_initial_account(self) -> None:
        event = client_usage_export.UsageEvent(
            when=datetime(2026, 7, 12, 8, 10, 0),
            model="gpt-test",
            input_tokens=1_000,
            cached_tokens=0,
            output_tokens=10,
            session_id="session-1",
            account_at=datetime(2026, 7, 12, 8, 9, 30),
        )
        marker = client_usage_export.AccountMarker(
            when=datetime(2026, 7, 12, 7, 0, 0),
            label="Codex local - unrelated-plus@example.com",
            total_tokens=999,
        )

        resolved, session_accounts, unresolved = client_usage_export.resolve_api_service_event_accounts(
            {"Codex local - stale-k12@example.com": [event]},
            [marker],
            {"session-1": "Codex local - stale-k12@example.com"},
        )

        self.assertEqual(
            resolved[client_usage_export.API_SERVICE_AGGREGATE_LABEL],
            [event],
        )
        self.assertNotIn("session-1", session_accounts)
        self.assertEqual(unresolved, 1)

    def test_api_service_account_is_not_reused_across_unconfirmed_turns(self) -> None:
        first = client_usage_export.UsageEvent(
            when=datetime(2026, 7, 12, 8, 0, 0),
            model="gpt-test",
            input_tokens=900,
            cached_tokens=0,
            output_tokens=100,
            session_id="session-1",
            account_at=datetime(2026, 7, 12, 7, 59, 30),
        )
        later_turn = client_usage_export.UsageEvent(
            when=datetime(2026, 7, 12, 8, 10, 0),
            model="gpt-test",
            input_tokens=1_800,
            cached_tokens=0,
            output_tokens=200,
            session_id="session-1",
            account_at=datetime(2026, 7, 12, 8, 9, 30),
        )
        marker = client_usage_export.AccountMarker(
            when=first.when,
            label="Codex local - k12@example.com",
            model="gpt-5.6-sol",
            total_tokens=first.total_tokens,
        )

        resolved, session_accounts, unresolved = (
            client_usage_export.resolve_api_service_event_accounts(
                {"Codex local - api-service-local": [first, later_turn]},
                [marker],
                {"session-1": "Codex local - k12@example.com"},
            )
        )

        self.assertEqual(
            sum(event.total_tokens for event in resolved["Codex local - k12@example.com"]),
            first.total_tokens,
        )
        self.assertEqual(
            sum(event.total_tokens for event in resolved[client_usage_export.API_SERVICE_AGGREGATE_LABEL]),
            later_turn.total_tokens,
        )
        self.assertNotIn("session-1", session_accounts)
        self.assertEqual(unresolved, 1)

    def test_api_service_turn_uses_new_final_account_after_reselection(self) -> None:
        turn_started_at = datetime(2026, 7, 12, 8, 0, 0)
        before_reselection = client_usage_export.UsageEvent(
            when=datetime(2026, 7, 12, 8, 1, 0),
            model="gpt-test",
            input_tokens=900,
            cached_tokens=0,
            output_tokens=100,
            session_id="session-1",
            account_at=turn_started_at,
        )
        after_reselection = client_usage_export.UsageEvent(
            when=datetime(2026, 7, 12, 8, 2, 0),
            model="gpt-test",
            input_tokens=1_800,
            cached_tokens=0,
            output_tokens=200,
            session_id="session-1",
            account_at=turn_started_at,
        )
        markers = [
            client_usage_export.AccountMarker(
                when=before_reselection.when,
                label="Codex local - k12@example.com",
                total_tokens=before_reselection.total_tokens,
            ),
            client_usage_export.AccountMarker(
                when=after_reselection.when,
                label="Codex local - plus@example.com",
                total_tokens=after_reselection.total_tokens,
            ),
        ]

        resolved, session_accounts, unresolved = (
            client_usage_export.resolve_api_service_event_accounts(
                {
                    "Codex local - api-service-local": [
                        before_reselection,
                        after_reselection,
                    ]
                },
                markers,
            )
        )

        self.assertEqual(resolved["Codex local - k12@example.com"], [before_reselection])
        self.assertEqual(resolved["Codex local - plus@example.com"], [after_reselection])
        self.assertEqual(session_accounts["session-1"], "Codex local - plus@example.com")
        self.assertEqual(unresolved, 0)

    def test_api_service_affinity_final_row_overrides_failed_initial_route(self) -> None:
        turn_started_at = datetime(2026, 7, 12, 8, 0, 0)
        event = client_usage_export.UsageEvent(
            when=datetime(2026, 7, 12, 8, 1, 0),
            model="gpt-test",
            input_tokens=900,
            cached_tokens=0,
            output_tokens=100,
            session_id="session-1",
            account_at=turn_started_at,
        )
        final_marker = client_usage_export.AccountMarker(
            when=event.when,
            label="Codex local - plus@example.com",
            total_tokens=999,
            request_id="request-1",
            account_id="plus-id",
        )
        affinity_events = [
            client_usage_export.CockpitAffinityEvent(
                when=turn_started_at + timedelta(milliseconds=50),
                request_id="request-1",
                source="execution_session_id",
                session_key="execution-1",
                account_id="k12-id",
                label="Codex local - k12@example.com",
                action="cache miss, new binding",
            ),
            client_usage_export.CockpitAffinityEvent(
                when=turn_started_at + timedelta(milliseconds=100),
                request_id="request-1",
                source="execution_session_id",
                session_key="execution-1",
                account_id="plus-id",
                label="Codex local - plus@example.com",
                action="cache hit but auth unavailable, reselected",
            ),
        ]

        resolved, session_accounts, unresolved = (
            client_usage_export.resolve_api_service_event_accounts(
                {client_usage_export.API_SERVICE_AGGREGATE_LABEL: [event]},
                [final_marker],
                affinity_events=affinity_events,
            )
        )

        self.assertEqual(resolved["Codex local - plus@example.com"], [event])
        self.assertEqual(session_accounts["session-1"], "Codex local - plus@example.com")
        self.assertEqual(unresolved, 0)

    def test_api_service_native_affinity_is_scoped_to_each_turn(self) -> None:
        first_turn = datetime(2026, 7, 12, 8, 0, 0)
        second_turn = datetime(2026, 7, 12, 8, 10, 0)
        first = client_usage_export.UsageEvent(
            when=first_turn + timedelta(seconds=30),
            model="gpt-test",
            input_tokens=900,
            cached_tokens=0,
            output_tokens=100,
            session_id="session-1",
            account_at=first_turn,
        )
        second = client_usage_export.UsageEvent(
            when=second_turn + timedelta(seconds=30),
            model="gpt-test",
            input_tokens=1_800,
            cached_tokens=0,
            output_tokens=200,
            session_id="session-1",
            account_at=second_turn,
        )
        affinity_events = [
            client_usage_export.CockpitAffinityEvent(
                when=first_turn + timedelta(milliseconds=50),
                request_id="request-1",
                source="execution_session_id",
                session_key="execution-1",
                account_id="k12-id",
                label="Codex local - k12@example.com",
                action="cache hit before new k12 routing",
            ),
            client_usage_export.CockpitAffinityEvent(
                when=second_turn + timedelta(milliseconds=50),
                request_id="request-1",
                source="execution_session_id",
                session_key="execution-1",
                account_id="plus-id",
                label="Codex local - plus@example.com",
                action="cache hit before new k12 routing",
            ),
        ]

        resolved, session_accounts, unresolved = (
            client_usage_export.resolve_api_service_event_accounts(
                {client_usage_export.API_SERVICE_AGGREGATE_LABEL: [first, second]},
                [],
                affinity_events=affinity_events,
            )
        )

        self.assertEqual(resolved["Codex local - k12@example.com"], [first])
        self.assertEqual(resolved["Codex local - plus@example.com"], [second])
        self.assertEqual(session_accounts["session-1"], "Codex local - plus@example.com")
        self.assertEqual(unresolved, 0)

    def test_api_service_consistent_temporal_affinity_resolves_non_native_turn(self) -> None:
        turn_started_at = datetime(2026, 7, 23, 9, 55, 35, 837000)
        events = [
            client_usage_export.UsageEvent(
                when=turn_started_at + timedelta(seconds=offset),
                model="gpt-test",
                input_tokens=100_000 + offset,
                cached_tokens=90_000,
                output_tokens=1_000,
                session_id="stable-temporal-session",
                account_at=turn_started_at,
            )
            for offset in (18, 24, 32)
        ]
        affinity_events = [
            client_usage_export.CockpitAffinityEvent(
                when=event.when + timedelta(milliseconds=30),
                request_id="stable-request",
                source="",
                session_key="client-request",
                account_id="pawns-id",
                label="Codex local - pawns@example.com",
                action="cache hit",
            )
            for event in events
        ]
        affinity_events.append(
            client_usage_export.CockpitAffinityEvent(
                when=events[0].when + timedelta(milliseconds=50),
                request_id="concurrent-request",
                source="",
                session_key="other-client-request",
                account_id="other-id",
                label="Codex local - other@example.com",
                action="cache hit",
            )
        )

        resolved, session_accounts, unresolved = (
            client_usage_export.resolve_api_service_event_accounts(
                {client_usage_export.API_SERVICE_AGGREGATE_LABEL: events},
                [],
                affinity_events=affinity_events,
            )
        )

        self.assertEqual(resolved["Codex local - pawns@example.com"], events)
        self.assertEqual(
            session_accounts["stable-temporal-session"],
            "Codex local - pawns@example.com",
        )
        self.assertEqual(unresolved, 0)

    def test_cockpit_request_preserves_each_last_usage_event(self) -> None:
        turn_started_at = datetime(2026, 7, 27, 9, 40, 26)
        events = [
            client_usage_export.UsageEvent(
                when=turn_started_at + timedelta(seconds=offset),
                model="gpt-test",
                input_tokens=tokens - 100,
                cached_tokens=0,
                output_tokens=100,
                session_id="snapshot-session",
                account_at=turn_started_at,
            )
            for offset, tokens in ((10, 40_000), (20, 90_000), (30, 150_000))
        ]
        affinity_events = [
            client_usage_export.CockpitAffinityEvent(
                when=event.when + timedelta(milliseconds=30),
                request_id="long-request",
                account_id="plus-id",
                label="Codex local - plus@example.com",
                action="cache hit",
            )
            for event in events
        ]
        marker = client_usage_export.AccountMarker(
            when=events[-1].when + timedelta(seconds=1),
            label="Codex local - plus@example.com",
            model="gpt-test",
            total_tokens=150_000,
            input_tokens=149_900,
            output_tokens=100,
            request_id="long-request",
            account_id="plus-id",
            latency_ms=31_000,
        )

        reconciled = client_usage_export.reconcile_cockpit_request_usage_events(
            events,
            [marker],
            affinity_events,
        )

        self.assertEqual(reconciled, events)
        self.assertEqual(
            sum(event.total_tokens for event in reconciled),
            280_000,
        )

    def test_cockpit_inflight_request_preserves_each_model_call(self) -> None:
        turn_started_at = datetime(2026, 7, 27, 10, 0, 0)
        events = [
            client_usage_export.UsageEvent(
                when=turn_started_at + timedelta(seconds=offset),
                model="gpt-test",
                input_tokens=tokens,
                cached_tokens=0,
                output_tokens=0,
                session_id="live-snapshot-session",
                account_at=turn_started_at,
            )
            for offset, tokens in ((10, 50_000), (20, 80_000), (30, 120_000))
        ]
        affinity_events = [
            client_usage_export.CockpitAffinityEvent(
                when=event.when + timedelta(milliseconds=25),
                request_id="inflight-request",
                account_id="plus-id",
                label="Codex local - plus@example.com",
                action="cache hit",
            )
            for event in events
        ]

        reconciled = client_usage_export.reconcile_cockpit_request_usage_events(
            events,
            [],
            affinity_events,
        )

        self.assertEqual(reconciled, events)
        self.assertEqual(
            sum(event.total_tokens for event in reconciled),
            250_000,
        )

    def test_cockpit_request_rotation_keeps_all_model_calls_and_direct_events(self) -> None:
        turn_started_at = datetime(2026, 7, 27, 10, 10, 0)
        routed = [
            client_usage_export.UsageEvent(
                when=turn_started_at + timedelta(seconds=offset),
                model="gpt-test",
                input_tokens=tokens,
                cached_tokens=0,
                output_tokens=0,
                session_id="rotating-snapshot-session",
                account_at=turn_started_at,
            )
            for offset, tokens in ((10, 50_000), (20, 80_000), (30, 60_000), (40, 90_000))
        ]
        direct = client_usage_export.UsageEvent(
            when=turn_started_at + timedelta(seconds=25),
            model="gpt-test",
            input_tokens=12_345,
            cached_tokens=0,
            output_tokens=0,
            session_id="official-direct-session",
            account_at=turn_started_at + timedelta(seconds=1),
        )
        affinity_events = [
            client_usage_export.CockpitAffinityEvent(
                when=event.when + timedelta(milliseconds=25),
                request_id="request-a" if position < 2 else "request-b",
                account_id="plus-id",
                label="Codex local - plus@example.com",
                action="cache hit",
            )
            for position, event in enumerate(routed)
        ]

        reconciled = client_usage_export.reconcile_cockpit_request_usage_events(
            [*routed, direct],
            [],
            affinity_events,
        )

        self.assertEqual(len(reconciled), 5)
        self.assertEqual(
            sorted(event.total_tokens for event in reconciled),
            [12_345, 50_000, 60_000, 80_000, 90_000],
        )
        self.assertIn(direct, reconciled)

    def test_api_service_temporal_affinity_allows_request_id_rotation_on_same_account(self) -> None:
        turn_started_at = datetime(2026, 7, 23, 11, 4, 48, 703000)
        events = [
            client_usage_export.UsageEvent(
                when=turn_started_at + timedelta(seconds=offset),
                model="gpt-test",
                input_tokens=100_000 + offset,
                cached_tokens=90_000,
                output_tokens=1_000,
                session_id="rotating-request-session",
                account_at=turn_started_at,
            )
            for offset in (10, 20, 30, 40)
        ]
        affinity_events = [
            client_usage_export.CockpitAffinityEvent(
                when=event.when + timedelta(milliseconds=40),
                request_id="request-a" if position < 2 else "request-b",
                source="",
                account_id="stable-account-id",
                label="Codex local - stable-account@example.com",
                action="cache hit",
            )
            for position, event in enumerate(events)
        ]

        resolved, session_accounts, unresolved = (
            client_usage_export.resolve_api_service_event_accounts(
                {client_usage_export.API_SERVICE_AGGREGATE_LABEL: events},
                [],
                affinity_events=affinity_events,
            )
        )

        self.assertEqual(
            resolved["Codex local - stable-account@example.com"],
            events,
        )
        self.assertEqual(
            session_accounts["rotating-request-session"],
            "Codex local - stable-account@example.com",
        )
        self.assertEqual(unresolved, 0)

    def test_api_service_temporal_affinity_changes_anchor_when_account_changes(self) -> None:
        turn_started_at = datetime(2026, 7, 23, 11, 20, 0)
        events = [
            client_usage_export.UsageEvent(
                when=turn_started_at + timedelta(seconds=offset),
                model="gpt-test",
                input_tokens=100_000 + offset,
                cached_tokens=90_000,
                output_tokens=1_000,
                session_id="account-change-session",
                account_at=turn_started_at,
            )
            for offset in (10, 20, 30, 40)
        ]
        affinity_events = [
            client_usage_export.CockpitAffinityEvent(
                when=event.when + timedelta(milliseconds=40),
                request_id="request-a" if position < 2 else "request-b",
                source="",
                account_id="account-a" if position < 2 else "account-b",
                label=(
                    "Codex local - account-a@example.com"
                    if position < 2
                    else "Codex local - account-b@example.com"
                ),
                action="cache hit",
            )
            for position, event in enumerate(events)
        ]

        resolved, session_accounts, unresolved = (
            client_usage_export.resolve_api_service_event_accounts(
                {client_usage_export.API_SERVICE_AGGREGATE_LABEL: events},
                [],
                affinity_events=affinity_events,
            )
        )

        self.assertEqual(
            resolved["Codex local - account-a@example.com"],
            events[:2],
        )
        self.assertEqual(
            resolved["Codex local - account-b@example.com"],
            events[2:],
        )
        self.assertEqual(
            session_accounts["account-change-session"],
            "Codex local - account-b@example.com",
        )
        self.assertEqual(unresolved, 0)

    def test_api_service_consistent_temporal_affinity_rejects_failed_request(self) -> None:
        turn_started_at = datetime(2026, 7, 23, 10, 0, 0)
        events = [
            client_usage_export.UsageEvent(
                when=turn_started_at + timedelta(seconds=offset),
                model="gpt-test",
                input_tokens=100_000,
                cached_tokens=90_000,
                output_tokens=1_000,
                session_id="failed-temporal-session",
                account_at=turn_started_at,
            )
            for offset in (10, 20)
        ]
        affinity_events = [
            client_usage_export.CockpitAffinityEvent(
                when=event.when + timedelta(milliseconds=30),
                request_id="failed-request",
                source="",
                account_id="stale-id",
                label="Codex local - stale@example.com",
                action="cache hit",
            )
            for event in events
        ]
        affinity_events.append(
            client_usage_export.CockpitAffinityEvent(
                when=turn_started_at + timedelta(seconds=15),
                request_id="failed-request",
                source="",
                account_id="replacement-id",
                label="Codex local - replacement@example.com",
                action="cache hit but auth unavailable, reselected",
            )
        )

        resolved, session_accounts, unresolved = (
            client_usage_export.resolve_api_service_event_accounts(
                {client_usage_export.API_SERVICE_AGGREGATE_LABEL: events},
                [],
                affinity_events=affinity_events,
            )
        )

        self.assertEqual(
            resolved[client_usage_export.API_SERVICE_AGGREGATE_LABEL],
            events,
        )
        self.assertEqual(session_accounts, {})
        self.assertEqual(unresolved, 2)

    def test_api_service_single_temporal_affinity_event_resolves_when_unique(self) -> None:
        turn_started_at = datetime(2026, 7, 23, 10, 30, 0)
        event = client_usage_export.UsageEvent(
            when=turn_started_at + timedelta(seconds=10),
            model="gpt-test",
            input_tokens=100_000,
            cached_tokens=90_000,
            output_tokens=1_000,
            session_id="single-temporal-session",
            account_at=turn_started_at,
        )
        affinity = client_usage_export.CockpitAffinityEvent(
            when=event.when + timedelta(milliseconds=30),
            request_id="single-request",
            source="",
            account_id="single-id",
            label="Codex local - single@example.com",
            action="cache hit",
        )

        resolved, session_accounts, unresolved = (
            client_usage_export.resolve_api_service_event_accounts(
                {client_usage_export.API_SERVICE_AGGREGATE_LABEL: [event]},
                [],
                affinity_events=[affinity],
            )
        )

        self.assertEqual(resolved[affinity.label], [event])
        self.assertEqual(
            session_accounts["single-temporal-session"],
            affinity.label,
        )
        self.assertEqual(unresolved, 0)

    def test_api_service_single_temporal_affinity_resolves_close_events_in_same_turn(self) -> None:
        turn_started_at = datetime(2026, 7, 23, 10, 32, 0)
        events = [
            client_usage_export.UsageEvent(
                when=turn_started_at + timedelta(seconds=10, milliseconds=offset),
                model="gpt-test",
                input_tokens=100_000 + offset,
                cached_tokens=90_000,
                output_tokens=1_000,
                session_id="single-close-turn-session",
                account_at=turn_started_at,
            )
            for offset in (0, 80)
        ]
        affinity = client_usage_export.CockpitAffinityEvent(
            when=events[0].when + timedelta(milliseconds=30),
            request_id="single-close-turn-request",
            source="",
            account_id="single-close-turn-id",
            label="Codex local - single-close-turn@example.com",
            action="cache hit",
        )

        resolved, session_accounts, unresolved = (
            client_usage_export.resolve_api_service_event_accounts(
                {client_usage_export.API_SERVICE_AGGREGATE_LABEL: events},
                [],
                affinity_events=[affinity],
            )
        )

        self.assertEqual(resolved[affinity.label], events)
        self.assertEqual(
            session_accounts["single-close-turn-session"],
            affinity.label,
        )
        self.assertEqual(unresolved, 0)

    def test_api_service_single_temporal_affinity_stays_unresolved_when_events_are_ambiguous(self) -> None:
        event_time = datetime(2026, 7, 23, 10, 35, 10)
        events = [
            client_usage_export.UsageEvent(
                when=event_time + timedelta(milliseconds=offset),
                model="gpt-test",
                input_tokens=100_000 + offset,
                cached_tokens=90_000,
                output_tokens=1_000,
                session_id=f"single-ambiguous-{offset}",
                account_at=event_time - timedelta(seconds=10 - offset / 1000),
            )
            for offset in (0, 80)
        ]
        affinity = client_usage_export.CockpitAffinityEvent(
            when=event_time + timedelta(milliseconds=30),
            request_id="single-ambiguous-request",
            source="",
            account_id="single-ambiguous-id",
            label="Codex local - single-ambiguous@example.com",
            action="cache hit",
        )

        resolved, session_accounts, unresolved = (
            client_usage_export.resolve_api_service_event_accounts(
                {client_usage_export.API_SERVICE_AGGREGATE_LABEL: events},
                [],
                affinity_events=[affinity],
            )
        )

        self.assertEqual(
            resolved[client_usage_export.API_SERVICE_AGGREGATE_LABEL],
            events,
        )
        self.assertEqual(session_accounts, {})
        self.assertEqual(unresolved, 2)

    def test_api_service_final_request_id_matches_long_request_past_time_limit(self) -> None:
        turn_started_at = datetime(2026, 7, 23, 10, 36, 8, 927000)
        event = client_usage_export.UsageEvent(
            when=datetime(2026, 7, 23, 10, 36, 31, 212000),
            model="gpt-test",
            input_tokens=30_000,
            cached_tokens=151_000,
            output_tokens=2_715,
            session_id="long-request-session",
            account_at=turn_started_at,
        )
        marker = client_usage_export.AccountMarker(
            when=event.when + timedelta(seconds=300, milliseconds=221),
            label="Codex local - final-long@example.com",
            model="gpt-5.6-sol",
            total_tokens=event.total_tokens,
            input_tokens=event.input_tokens,
            cached_tokens=event.cached_tokens,
            output_tokens=event.output_tokens,
            event_key="long-final-row",
            request_id="long-request-id",
            account_id="final-long-id",
        )
        affinity = client_usage_export.CockpitAffinityEvent(
            when=turn_started_at + timedelta(milliseconds=450),
            request_id=marker.request_id,
            source="execution_session_id",
            account_id=marker.account_id,
            label=marker.label,
            action="cache hit",
        )

        resolved, session_accounts, unresolved = (
            client_usage_export.resolve_api_service_event_accounts(
                {client_usage_export.API_SERVICE_AGGREGATE_LABEL: [event]},
                [marker],
                affinity_events=[affinity],
            )
        )

        self.assertGreater(
            (marker.when - event.when).total_seconds(),
            client_usage_export.API_SERVICE_ACTIVITY_MATCH_SECONDS,
        )
        self.assertGreater(
            (affinity.when - turn_started_at).total_seconds(),
            client_usage_export.COCKPIT_AFFINITY_EVENT_MATCH_SECONDS,
        )
        self.assertEqual(resolved[marker.label], [event])
        self.assertEqual(session_accounts[event.session_id], marker.label)
        self.assertEqual(
            sum(item.total_tokens for items in resolved.values() for item in items),
            event.total_tokens,
        )
        self.assertEqual(unresolved, 0)

    def test_api_service_native_reselection_waits_for_stable_new_route(self) -> None:
        turn_started_at = datetime(2026, 7, 12, 8, 0, 0)
        event = client_usage_export.UsageEvent(
            when=turn_started_at + timedelta(seconds=30),
            model="gpt-test",
            input_tokens=900,
            cached_tokens=0,
            output_tokens=100,
            session_id="session-1",
            account_at=turn_started_at,
        )
        affinity_events = [
            client_usage_export.CockpitAffinityEvent(
                when=turn_started_at + timedelta(milliseconds=50),
                request_id="request-1",
                source="execution_session_id",
                account_id="k12-id",
                label="Codex local - k12@example.com",
                action="cache miss, new binding",
            ),
            client_usage_export.CockpitAffinityEvent(
                when=turn_started_at + timedelta(seconds=2),
                request_id="request-1",
                source="execution_session_id",
                account_id="plus-id",
                label="Codex local - plus@example.com",
                action="cache hit but auth unavailable, reselected",
            ),
            client_usage_export.CockpitAffinityEvent(
                when=turn_started_at + timedelta(seconds=20),
                request_id="request-1",
                source="execution_session_id",
                account_id="plus-id",
                label="Codex local - plus@example.com",
                action="cache hit before new k12 routing",
            ),
        ]

        resolved, session_accounts, unresolved = (
            client_usage_export.resolve_api_service_event_accounts(
                {client_usage_export.API_SERVICE_AGGREGATE_LABEL: [event]},
                [],
                affinity_events=affinity_events,
            )
        )

        self.assertEqual(resolved["Codex local - plus@example.com"], [event])
        self.assertEqual(session_accounts["session-1"], "Codex local - plus@example.com")
        self.assertEqual(unresolved, 0)

    def test_api_service_concurrent_affinity_accounts_remain_unresolved(self) -> None:
        turn_started_at = datetime(2026, 7, 12, 8, 0, 0)
        event = client_usage_export.UsageEvent(
            when=turn_started_at + timedelta(seconds=30),
            model="gpt-test",
            input_tokens=900,
            cached_tokens=0,
            output_tokens=100,
            session_id="session-1",
            account_at=turn_started_at,
        )
        affinity_events = [
            client_usage_export.CockpitAffinityEvent(
                when=turn_started_at + timedelta(milliseconds=50),
                request_id="request-1",
                source="execution_session_id",
                account_id="account-a",
                label="Codex local - account-a@example.com",
                action="cache hit before new k12 routing",
            ),
            client_usage_export.CockpitAffinityEvent(
                when=turn_started_at + timedelta(milliseconds=100),
                request_id="request-2",
                source="execution_session_id",
                account_id="account-b",
                label="Codex local - account-b@example.com",
                action="cache hit before new k12 routing",
            ),
        ]

        resolved, session_accounts, unresolved = (
            client_usage_export.resolve_api_service_event_accounts(
                {client_usage_export.API_SERVICE_AGGREGATE_LABEL: [event]},
                [],
                affinity_events=affinity_events,
            )
        )

        self.assertEqual(
            resolved[client_usage_export.API_SERVICE_AGGREGATE_LABEL],
            [event],
        )
        self.assertNotIn("session-1", session_accounts)
        self.assertEqual(unresolved, 1)

    def test_api_service_turn_uses_clearly_nearest_non_native_affinity(self) -> None:
        turn_started_at = datetime(2026, 7, 23, 11, 28, 50, 376000)
        event = client_usage_export.UsageEvent(
            when=turn_started_at + timedelta(seconds=22, milliseconds=827),
            model="gpt-test",
            input_tokens=32_629,
            cached_tokens=1_408,
            output_tokens=945,
            session_id="nearest-non-native-session",
            account_at=turn_started_at,
        )
        affinity_events = [
            client_usage_export.CockpitAffinityEvent(
                when=turn_started_at - timedelta(milliseconds=350),
                request_id="nearby-concurrent-request",
                source="",
                account_id="nearby-concurrent-account",
                label="Codex local - nearby-concurrent@example.com",
                action="cache hit",
            ),
            client_usage_export.CockpitAffinityEvent(
                when=turn_started_at + timedelta(milliseconds=68),
                request_id="current-turn-request",
                source="",
                account_id="current-turn-account",
                label="Codex local - current-turn@example.com",
                action="cache hit",
            ),
        ]

        resolved, session_accounts, unresolved = (
            client_usage_export.resolve_api_service_event_accounts(
                {client_usage_export.API_SERVICE_AGGREGATE_LABEL: [event]},
                [],
                affinity_events=affinity_events,
            )
        )

        expected_label = "Codex local - current-turn@example.com"
        self.assertEqual(resolved[expected_label], [event])
        self.assertEqual(
            session_accounts["nearest-non-native-session"],
            expected_label,
        )
        self.assertEqual(unresolved, 0)

    def test_api_service_turn_keeps_close_long_lived_requests_ambiguous(self) -> None:
        turn_started_at = datetime(2026, 7, 23, 10, 4, 59, 40_000)
        event = client_usage_export.UsageEvent(
            when=turn_started_at + timedelta(seconds=31, milliseconds=824),
            model="gpt-test",
            input_tokens=33_488,
            cached_tokens=3_840,
            output_tokens=1_479,
            session_id="close-long-lived-session",
            account_at=turn_started_at,
        )
        affinity_events = [
            client_usage_export.CockpitAffinityEvent(
                when=turn_started_at - timedelta(minutes=5),
                request_id="long-lived-a",
                source="",
                account_id="old-account",
                label="Codex local - old@example.com",
                action="cache miss, new binding",
            ),
            client_usage_export.CockpitAffinityEvent(
                when=turn_started_at - timedelta(minutes=4, seconds=59),
                request_id="long-lived-a",
                source="",
                account_id="account-a",
                label="Codex local - account-a@example.com",
                action="cache hit but auth unavailable, reselected",
            ),
            client_usage_export.CockpitAffinityEvent(
                when=turn_started_at + timedelta(milliseconds=218),
                request_id="long-lived-a",
                source="",
                account_id="account-a",
                label="Codex local - account-a@example.com",
                action="cache hit",
            ),
            client_usage_export.CockpitAffinityEvent(
                when=turn_started_at + timedelta(milliseconds=273),
                request_id="long-lived-b",
                source="",
                account_id="account-b",
                label="Codex local - account-b@example.com",
                action="cache hit",
            ),
        ]
        final_marker = client_usage_export.AccountMarker(
            when=turn_started_at + timedelta(seconds=123),
            label="Codex local - account-a@example.com",
            kind="request",
            total_tokens=192_171,
            input_tokens=192_068,
            output_tokens=103,
            request_id="long-lived-a",
            account_id="account-a",
        )

        resolved, session_accounts, unresolved = (
            client_usage_export.resolve_api_service_event_accounts(
                {client_usage_export.API_SERVICE_AGGREGATE_LABEL: [event]},
                [final_marker],
                affinity_events=affinity_events,
            )
        )

        self.assertEqual(
            resolved[client_usage_export.API_SERVICE_AGGREGATE_LABEL],
            [event],
        )
        self.assertEqual(session_accounts, {})
        self.assertEqual(unresolved, 1)

    def test_api_service_prompt_cache_candidate_is_not_token_evidence(self) -> None:
        turn_started_at = datetime(2026, 7, 12, 8, 0, 0)
        event = client_usage_export.UsageEvent(
            when=turn_started_at + timedelta(seconds=30),
            model="gpt-test",
            input_tokens=900,
            cached_tokens=0,
            output_tokens=100,
            session_id="session-1",
            account_at=turn_started_at,
        )
        affinity = client_usage_export.CockpitAffinityEvent(
            when=turn_started_at + timedelta(milliseconds=50),
            request_id="request-1",
            source="prompt_cache_key",
            session_key="shared-content-hash",
            account_id="k12-id",
            label="Codex local - k12@example.com",
            action="cache hit before new k12 routing",
        )

        resolved, _session_accounts, unresolved = (
            client_usage_export.resolve_api_service_event_accounts(
                {client_usage_export.API_SERVICE_AGGREGATE_LABEL: [event]},
                [],
                affinity_events=[affinity],
            )
        )

        self.assertEqual(
            resolved[client_usage_export.API_SERVICE_AGGREGATE_LABEL],
            [event],
        )
        self.assertEqual(unresolved, 1)

    def test_cockpit_affinity_log_marks_only_confirmed_actions_as_confirmed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cockpit = root / ".antigravity_cockpit"
            logs = cockpit / "logs"
            logs.mkdir(parents=True)
            (cockpit / "codex_accounts.json").write_text(
                json.dumps(
                    {
                        "accounts": [
                            {
                                "id": "codex_plus",
                                "email": "plus@example.com",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            # Cockpit can keep writing to the previous day's rotated filename
            # for a short time after midnight; line timestamps stay authoritative.
            (logs / "codex-api.log.2026-07-11").write_text(
                "\n".join(
                    [
                        '2026-07-12T08:00:00+08:00 WARN msg="session-affinity: cache miss, new binding | source=execution_session_id session=native-1 auth=codex_plus.json provider=mixed model=gpt-test" request_id=request-1',
                        '2026-07-12T08:00:01+08:00 WARN msg="k12-session-affinity: binding confirmed | source=execution_session_id session=native-1 auth=codex_plus.json" request_id=request-1',
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            events = client_usage_export.scan_cockpit_codex_affinity_events(
                root,
                datetime(2026, 7, 12, 7, 59, 0),
                datetime(2026, 7, 12, 8, 1, 0),
            )

        self.assertEqual(len(events), 2)
        self.assertFalse(events[0].confirmed)
        self.assertTrue(events[1].confirmed)
        self.assertEqual(events[1].label, "Codex local - plus@example.com")
        self.assertEqual(events[1].account_id, "codex_plus")

    def test_cockpit_auth_result_resolves_opaque_api_key_route(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cockpit = root / ".antigravity_cockpit"
            logs = cockpit / "logs"
            logs.mkdir(parents=True)
            account_id = "codex_apikey_8311cc24"
            account_label = "Codex local - api-key-8311cc24"
            (cockpit / "codex_accounts.json").write_text(
                json.dumps(
                    {
                        "accounts": [
                            {
                                "id": account_id,
                                "email": "api-key-8311cc24",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            auth_at = datetime(2026, 8, 2, 15, 20, 6, 200000)
            initial_route_at = auth_at - timedelta(seconds=20)
            route_at = auth_at + timedelta(milliseconds=850)
            usage_at = route_at - timedelta(milliseconds=100)
            auth_result = {
                "type": "auth_result",
                "requestId": "request-api",
                "model": "gpt-test",
                "authId": "codex:apikey:opaque-route",
                "accountId": account_id,
                "accountEmail": "api-key-8311cc24",
                "success": True,
                "authAvailable": True,
            }
            (logs / "codex-api.log.2026-08-02").write_text(
                (
                    f'{initial_route_at.isoformat()} WARN msg="session-affinity: cache hit | '
                    "session=test auth=codex:apikey:opaque-route provider=mixed "
                    'model=gpt-test" request_id=request-api\n'
                    f"{auth_at.isoformat()} INFO [CodexLocalAccess][sidecar] "
                    f"{json.dumps(auth_result, separators=(',', ':'))}\n"
                    f'{route_at.isoformat()} WARN msg="session-affinity: cache hit | '
                    "session=test auth=codex:apikey:opaque-route provider=mixed "
                    'model=gpt-test" request_id=request-api\n'
                ),
                encoding="utf-8",
            )

            events = client_usage_export.scan_cockpit_codex_affinity_events(
                root,
                initial_route_at - timedelta(seconds=1),
                route_at + timedelta(seconds=1),
            )

        self.assertEqual(len(events), 3)
        structured = next(event for event in events if event.action == "auth result")
        routes = [event for event in events if event.action == "cache hit"]
        self.assertTrue(structured.confirmed)
        self.assertEqual(structured.account_id, account_id)
        self.assertEqual(structured.label, account_label)
        self.assertTrue(all(route.confirmed for route in routes))
        self.assertEqual({route.account_id for route in routes}, {account_id})
        self.assertEqual({route.label for route in routes}, {account_label})

        usage = client_usage_export.UsageEvent(
            when=usage_at,
            model="gpt-test",
            input_tokens=900,
            cached_tokens=0,
            output_tokens=100,
            session_id="api-session",
            account_at=auth_at - timedelta(minutes=5),
        )
        resolved, session_accounts, unresolved = (
            client_usage_export.resolve_api_service_event_accounts(
                {client_usage_export.API_SERVICE_AGGREGATE_LABEL: [usage]},
                [],
                affinity_events=events,
            )
        )

        self.assertEqual(resolved[account_label], [usage])
        self.assertEqual(session_accounts[usage.session_id], account_label)
        self.assertEqual(unresolved, 0)

    def test_failed_auth_result_does_not_confirm_opaque_route(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            logs = root / ".antigravity_cockpit" / "logs"
            logs.mkdir(parents=True)
            auth_at = datetime(2026, 8, 2, 15, 20, 6, 200000)
            route_at = auth_at + timedelta(milliseconds=100)
            auth_result = {
                "type": "auth_result",
                "requestId": "request-failed",
                "authId": "codex:apikey:opaque-route",
                "accountId": "codex_apikey_failed",
                "accountEmail": "api-key-failed",
                "success": False,
                "authAvailable": False,
            }
            (logs / "codex-api.log.2026-08-02").write_text(
                (
                    f"{auth_at.isoformat()} INFO [CodexLocalAccess][sidecar] "
                    f"{json.dumps(auth_result, separators=(',', ':'))}\n"
                    f'{route_at.isoformat()} WARN msg="session-affinity: cache hit | '
                    "session=test auth=codex:apikey:opaque-route provider=mixed "
                    'model=gpt-test" request_id=request-failed\n'
                ),
                encoding="utf-8",
            )

            events = client_usage_export.scan_cockpit_codex_affinity_events(
                root,
                auth_at - timedelta(seconds=1),
                route_at + timedelta(seconds=1),
            )

        route = next(event for event in events if event.action == "cache hit")
        self.assertFalse(route.confirmed)
        self.assertEqual(route.account_id, "codex:apikey:opaque-route")
        self.assertEqual(route.label, "")

    def test_auth_result_enrichment_stops_at_reselect_boundary(self) -> None:
        first_label = "Codex local - first@example.com"
        second_label = "Codex local - second@example.com"
        started_at = datetime(2026, 8, 2, 15, 20, 0)
        events = [
            client_usage_export.CockpitAffinityEvent(
                when=started_at,
                request_id="request-reselect",
                account_id="codex:apikey:opaque-route",
                action="cache hit",
            ),
            client_usage_export.CockpitAffinityEvent(
                when=started_at + timedelta(seconds=1),
                request_id="request-reselect",
                source="auth_result",
                account_id="first-id",
                label=first_label,
                action="auth result",
                confirmed=True,
            ),
            client_usage_export.CockpitAffinityEvent(
                when=started_at + timedelta(seconds=2),
                request_id="request-reselect",
                account_id="codex:apikey:opaque-route",
                action="cache hit",
            ),
            client_usage_export.CockpitAffinityEvent(
                when=started_at + timedelta(seconds=3),
                request_id="request-reselect",
                account_id="second-id",
                label=second_label,
                action="cache hit but auth unavailable, reselected",
            ),
            client_usage_export.CockpitAffinityEvent(
                when=started_at + timedelta(seconds=4),
                request_id="request-reselect",
                source="auth_result",
                account_id="second-id",
                label=second_label,
                action="auth result",
                confirmed=True,
            ),
            client_usage_export.CockpitAffinityEvent(
                when=started_at + timedelta(seconds=5),
                request_id="request-reselect",
                account_id="codex:apikey:opaque-route",
                action="cache hit",
            ),
        ]

        client_usage_export.enrich_cockpit_affinity_from_auth_results(events)

        stable = [event for event in events if event.action == "cache hit"]
        self.assertEqual(
            [(event.account_id, event.label) for event in stable],
            [
                ("first-id", first_label),
                ("first-id", first_label),
                ("second-id", second_label),
            ],
        )

    def test_reused_request_id_keeps_earlier_api_key_segment(self) -> None:
        api_label = "Codex local - api-key-8311cc24"
        oauth_label = "Codex local - hails@example.com"
        api_turn = datetime(2026, 8, 2, 20, 55, 20)
        oauth_turn = datetime(2026, 8, 2, 21, 20, 10)
        api_event = client_usage_export.UsageEvent(
            when=api_turn + timedelta(seconds=11, milliseconds=691),
            model="gpt-test",
            input_tokens=90_000,
            cached_tokens=80_000,
            output_tokens=1_000,
            session_id="api-key-turn",
            account_at=api_turn,
        )
        oauth_event = client_usage_export.UsageEvent(
            when=oauth_turn + timedelta(seconds=9, milliseconds=902),
            model="gpt-test",
            input_tokens=100_000,
            cached_tokens=90_000,
            output_tokens=2_000,
            session_id="oauth-turn",
            account_at=oauth_turn,
        )
        request_id = "2f63c07a"
        affinity_events = [
            client_usage_export.CockpitAffinityEvent(
                when=api_event.when - timedelta(milliseconds=80),
                request_id=request_id,
                source="auth_result",
                account_id="api-id",
                label=api_label,
                action="auth result",
                confirmed=True,
            ),
            client_usage_export.CockpitAffinityEvent(
                when=api_event.when + timedelta(milliseconds=30),
                request_id=request_id,
                account_id="api-id",
                label=api_label,
                action="cache hit",
            ),
            client_usage_export.CockpitAffinityEvent(
                when=datetime(2026, 8, 2, 21, 4, 11),
                request_id=request_id,
                account_id="oauth-id",
                label=oauth_label,
                action="cache hit but auth unavailable, reselected",
            ),
            client_usage_export.CockpitAffinityEvent(
                when=oauth_event.when - timedelta(milliseconds=70),
                request_id=request_id,
                source="auth_result",
                account_id="oauth-id",
                label=oauth_label,
                action="auth result",
                confirmed=True,
            ),
            client_usage_export.CockpitAffinityEvent(
                when=oauth_event.when + timedelta(milliseconds=30),
                request_id=request_id,
                account_id="oauth-id",
                label=oauth_label,
                action="cache hit",
            ),
        ]
        oauth_marker = client_usage_export.AccountMarker(
            when=oauth_event.when + timedelta(milliseconds=100),
            label=oauth_label,
            total_tokens=oauth_event.total_tokens,
            request_id=request_id,
            account_id="oauth-id",
        )

        resolved, session_accounts, unresolved = (
            client_usage_export.resolve_api_service_event_accounts(
                {
                    client_usage_export.API_SERVICE_AGGREGATE_LABEL: [
                        api_event,
                        oauth_event,
                    ]
                },
                [oauth_marker],
                affinity_events=affinity_events,
            )
        )

        self.assertEqual(resolved[api_label], [api_event])
        self.assertEqual(resolved[oauth_label], [oauth_event])
        self.assertEqual(session_accounts[api_event.session_id], api_label)
        self.assertEqual(session_accounts[oauth_event.session_id], oauth_label)
        self.assertEqual(unresolved, 0)

    def test_failed_request_recovers_after_successful_api_key_auth(self) -> None:
        turn_started_at = datetime(2026, 8, 2, 21, 40, 0)
        api_label = "Codex local - api-key-8311cc24"
        event = client_usage_export.UsageEvent(
            when=turn_started_at + timedelta(seconds=20),
            model="gpt-test",
            input_tokens=120_000,
            cached_tokens=100_000,
            output_tokens=2_000,
            session_id="recovered-api-session",
            account_at=turn_started_at,
        )
        affinity_events = [
            client_usage_export.CockpitAffinityEvent(
                when=turn_started_at + timedelta(seconds=1),
                request_id="aad4a595",
                account_id="old-id",
                label="Codex local - old@example.com",
                action="cache hit but auth unavailable, reselected",
            ),
            client_usage_export.CockpitAffinityEvent(
                when=event.when - timedelta(milliseconds=100),
                request_id="aad4a595",
                source="auth_result",
                account_id="api-id",
                label=api_label,
                action="auth result",
                confirmed=True,
            ),
            client_usage_export.CockpitAffinityEvent(
                when=event.when + timedelta(milliseconds=30),
                request_id="aad4a595",
                account_id="api-id",
                label=api_label,
                action="cache hit",
            ),
        ]

        resolved, session_accounts, unresolved = (
            client_usage_export.resolve_api_service_event_accounts(
                {client_usage_export.API_SERVICE_AGGREGATE_LABEL: [event]},
                [],
                affinity_events=affinity_events,
            )
        )

        self.assertEqual(resolved[api_label], [event])
        self.assertEqual(session_accounts[event.session_id], api_label)
        self.assertEqual(unresolved, 0)

    def test_future_conflicting_marker_cannot_override_confirmed_segment(self) -> None:
        account_label = "Codex local - confirmed@example.com"
        turn_started_at = datetime(2026, 8, 2, 22, 0, 0)
        event = client_usage_export.UsageEvent(
            when=turn_started_at + timedelta(seconds=10),
            model="gpt-test",
            input_tokens=50_000,
            cached_tokens=40_000,
            output_tokens=1_000,
            session_id="confirmed-segment",
            account_at=turn_started_at,
        )
        affinity_events = [
            client_usage_export.CockpitAffinityEvent(
                when=event.when - timedelta(milliseconds=50),
                request_id="reused-request",
                source="auth_result",
                account_id="confirmed-id",
                label=account_label,
                action="auth result",
                confirmed=True,
            ),
            client_usage_export.CockpitAffinityEvent(
                when=event.when + timedelta(milliseconds=30),
                request_id="reused-request",
                account_id="confirmed-id",
                label=account_label,
                action="cache hit",
            ),
        ]
        future_marker = client_usage_export.AccountMarker(
            when=event.when + timedelta(minutes=20),
            label="Codex local - future@example.com",
            total_tokens=event.total_tokens,
            request_id="reused-request",
            account_id="future-id",
        )

        resolved, session_accounts, unresolved = (
            client_usage_export.resolve_api_service_event_accounts(
                {client_usage_export.API_SERVICE_AGGREGATE_LABEL: [event]},
                [future_marker],
                affinity_events=affinity_events,
            )
        )

        self.assertEqual(resolved[account_label], [event])
        self.assertEqual(session_accounts[event.session_id], account_label)
        self.assertEqual(unresolved, 0)

    def test_similar_token_requests_keep_their_own_segment_markers(self) -> None:
        started_at = datetime(2026, 8, 2, 22, 20, 0)
        labels = [
            "Codex local - first@example.com",
            "Codex local - second@example.com",
        ]
        events = [
            client_usage_export.UsageEvent(
                when=started_at + timedelta(seconds=index * 2),
                model="gpt-test",
                input_tokens=99_000,
                cached_tokens=0,
                output_tokens=1_000,
                session_id=f"similar-{index}",
                account_at=started_at + timedelta(seconds=index * 2),
            )
            for index in range(2)
        ]
        affinity_events = [
            client_usage_export.CockpitAffinityEvent(
                when=event.when + timedelta(milliseconds=40),
                request_id=f"request-{index}",
                source="auth_result",
                account_id=f"account-{index}",
                label=labels[index],
                action="auth result",
                confirmed=True,
            )
            for index, event in enumerate(events)
        ]
        affinity_events.extend(
            client_usage_export.CockpitAffinityEvent(
                when=event.when + timedelta(milliseconds=60),
                request_id=f"request-{index}",
                account_id=f"account-{index}",
                label=labels[index],
                action="cache hit",
            )
            for index, event in enumerate(events)
        )
        markers = [
            client_usage_export.AccountMarker(
                when=event.when + timedelta(milliseconds=100),
                label=labels[index],
                total_tokens=event.total_tokens,
                request_id=f"request-{index}",
                account_id=f"account-{index}",
            )
            for index, event in enumerate(events)
        ]

        resolved, session_accounts, unresolved = (
            client_usage_export.resolve_api_service_event_accounts(
                {client_usage_export.API_SERVICE_AGGREGATE_LABEL: events},
                markers,
                affinity_events=affinity_events,
            )
        )

        self.assertEqual(resolved[labels[0]], [events[0]])
        self.assertEqual(resolved[labels[1]], [events[1]])
        self.assertEqual(session_accounts[events[0].session_id], labels[0])
        self.assertEqual(session_accounts[events[1].session_id], labels[1])
        self.assertEqual(unresolved, 0)

    def test_confirmed_auth_results_disambiguate_close_concurrent_turns(self) -> None:
        base = datetime(2026, 8, 2, 21, 24, 44, 267000)
        labels = [
            "Codex local - oauth@example.com",
            "Codex local - api-key-8311cc24",
        ]
        events = [
            client_usage_export.UsageEvent(
                when=base + timedelta(milliseconds=index * 61),
                model="gpt-test",
                input_tokens=40_000 + index * 1_000,
                cached_tokens=0,
                output_tokens=100,
                session_id=f"close-session-{index}",
                account_at=base - timedelta(seconds=10 - index),
            )
            for index in range(2)
        ]
        affinity_events: list[client_usage_export.CockpitAffinityEvent] = []
        for index, event in enumerate(events):
            affinity_events.extend(
                [
                    client_usage_export.CockpitAffinityEvent(
                        when=event.when - timedelta(milliseconds=1),
                        request_id=f"close-request-{index}",
                        source="auth_result",
                        account_id=f"close-account-{index}",
                        label=labels[index],
                        action="auth result",
                        confirmed=True,
                    ),
                    client_usage_export.CockpitAffinityEvent(
                        when=event.when + timedelta(milliseconds=30),
                        request_id=f"close-request-{index}",
                        account_id=f"close-account-{index}",
                        label=labels[index],
                        action="cache hit",
                    ),
                ]
            )

        resolved, session_accounts, unresolved = (
            client_usage_export.resolve_api_service_event_accounts(
                {client_usage_export.API_SERVICE_AGGREGATE_LABEL: events},
                [],
                affinity_events=affinity_events,
            )
        )

        self.assertEqual(resolved[labels[0]], [events[0]])
        self.assertEqual(resolved[labels[1]], [events[1]])
        self.assertEqual(session_accounts[events[0].session_id], labels[0])
        self.assertEqual(session_accounts[events[1].session_id], labels[1])
        self.assertEqual(unresolved, 0)

    def test_long_segments_keep_their_turn_during_close_concurrency(self) -> None:
        base = datetime(2026, 8, 2, 21, 25, 42, 822000)
        labels = [
            "Codex local - stream-api@example.com",
            "Codex local - stream-oauth@example.com",
        ]
        turn_starts = [base - timedelta(hours=3), base - timedelta(minutes=2)]
        event_times = [
            [base - timedelta(seconds=4), base - timedelta(seconds=2), base],
            [
                base - timedelta(seconds=3),
                base - timedelta(seconds=1),
                base + timedelta(milliseconds=31),
            ],
        ]
        events: list[client_usage_export.UsageEvent] = []
        affinity_events: list[client_usage_export.CockpitAffinityEvent] = []
        for stream_index in range(2):
            request_id = f"stream-request-{stream_index}"
            for event_index, event_time in enumerate(event_times[stream_index]):
                event = client_usage_export.UsageEvent(
                    when=event_time,
                    model="gpt-test",
                    input_tokens=50_000 + stream_index * 1_000 + event_index,
                    cached_tokens=0,
                    output_tokens=100,
                    session_id=f"stream-session-{stream_index}",
                    account_at=turn_starts[stream_index],
                )
                events.append(event)
                if event_index == 0:
                    affinity_events.append(
                        client_usage_export.CockpitAffinityEvent(
                            when=event.when - timedelta(milliseconds=1),
                            request_id=request_id,
                            source="auth_result",
                            account_id=f"stream-account-{stream_index}",
                            label=labels[stream_index],
                            action="auth result",
                            confirmed=True,
                        )
                    )
                route_offset = (
                    53
                    if stream_index == 0 and event_index == 2
                    else 33
                    if stream_index == 1 and event_index == 2
                    else 30
                )
                affinity_events.append(
                    client_usage_export.CockpitAffinityEvent(
                        when=event.when + timedelta(milliseconds=route_offset),
                        request_id=request_id,
                        account_id=f"stream-account-{stream_index}",
                        label=labels[stream_index],
                        action="cache hit",
                    )
                )

        resolved, session_accounts, unresolved = (
            client_usage_export.resolve_api_service_event_accounts(
                {client_usage_export.API_SERVICE_AGGREGATE_LABEL: events},
                [],
                affinity_events=affinity_events,
            )
        )

        for stream_index, label in enumerate(labels):
            expected = [
                event
                for event in events
                if event.session_id == f"stream-session-{stream_index}"
            ]
            self.assertEqual(resolved[label], expected)
            self.assertEqual(
                session_accounts[f"stream-session-{stream_index}"],
                label,
            )
        self.assertEqual(unresolved, 0)

    def test_equally_close_confirmed_auth_results_remain_unresolved(self) -> None:
        event = client_usage_export.UsageEvent(
            when=datetime(2026, 8, 2, 22, 30, 0),
            model="gpt-test",
            input_tokens=1_000,
            cached_tokens=0,
            output_tokens=100,
            session_id="ambiguous-auth-session",
            account_at=datetime(2026, 8, 2, 22, 29, 50),
        )
        affinity_events = [
            client_usage_export.CockpitAffinityEvent(
                when=event.when + timedelta(milliseconds=offset),
                request_id=f"ambiguous-auth-{index}",
                source="auth_result",
                account_id=f"ambiguous-account-{index}",
                label=f"Codex local - ambiguous-{index}@example.com",
                action="auth result",
                confirmed=True,
            )
            for index, offset in enumerate((-1, 1))
        ]

        markers = client_usage_export.cockpit_confirmed_auth_result_event_markers(
            [event],
            affinity_events,
        )

        self.assertEqual(markers, {})

    def test_cockpit_manifest_email_beats_internal_log_filename(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cockpit = root / ".antigravity_cockpit"
            logs = cockpit / "logs"
            logs.mkdir(parents=True)
            account_id = "codex_a537e71a6393d78bbac5e57d3d128fbc"
            email = "fixture-a@example.com"
            (cockpit / "codex_accounts.json").write_text(
                json.dumps(
                    {
                        "accounts": [
                            {
                                "id": account_id,
                                "email": email,
                                "plan_type": "free",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            accounts_dir = cockpit / "codex_accounts"
            accounts_dir.mkdir()
            (accounts_dir / "internal-alias.json").write_text(
                json.dumps(
                    {
                        "id": f"{account_id}.json",
                        "api_provider_name": f"{account_id}.json",
                    }
                ),
                encoding="utf-8",
            )
            (logs / "codex-api.log.2026-07-23").write_text(
                (
                    "2026-07-23T11:14:04+08:00 WARN "
                    'msg="session-affinity: cache miss, new binding | '
                    f"source=execution_session_id session=native-1 auth={account_id}.json "
                    'provider=mixed model=gpt-test" request_id=request-1\n'
                ),
                encoding="utf-8",
            )
            internal_marker = client_usage_export.AccountMarker(
                when=datetime(2026, 7, 23, 11, 14, 10),
                label=f"Codex local - {account_id}.json",
                kind="request",
                total_tokens=1_000,
                account_id=account_id,
            )

            events = client_usage_export.scan_cockpit_codex_affinity_events(
                root,
                datetime(2026, 7, 23, 11, 13, 0),
                datetime(2026, 7, 23, 11, 15, 0),
                [internal_marker],
            )

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].label, f"Codex local - {email}")

    def test_live_catchup_uses_same_affinity_evidence_as_full_export(self) -> None:
        day_start = datetime(2026, 7, 12, 0, 0, 0)
        turn_started_at = datetime(2026, 7, 12, 8, 0, 0)
        event = client_usage_export.UsageEvent(
            when=turn_started_at + timedelta(seconds=30),
            model="gpt-test",
            input_tokens=900,
            cached_tokens=0,
            output_tokens=100,
            session_id="session-1",
            account_at=turn_started_at,
        )
        final_marker = client_usage_export.AccountMarker(
            when=turn_started_at + timedelta(seconds=40),
            label="Codex local - plus@example.com",
            model="gpt-test",
            total_tokens=5_000,
            request_id="request-1",
            account_id="plus-id",
        )
        affinity = client_usage_export.CockpitAffinityEvent(
            when=turn_started_at + timedelta(milliseconds=50),
            request_id="request-1",
            source="execution_session_id",
            session_key="execution-1",
            account_id="plus-id",
            label="Codex local - plus@example.com",
            action="cache miss, new binding",
        )
        with (
            tempfile.TemporaryDirectory() as temporary_directory,
            patch.object(
                client_usage_export,
                "scan_all_codex_events",
                return_value=[event],
            ),
            patch.object(client_usage_export, "codex_speed_history", return_value=[]),
            patch.object(client_usage_export, "current_codex_account_label", return_value=""),
            patch.object(client_usage_export, "load_attribution_ledger", return_value={}),
            patch.object(
                client_usage_export,
                "scan_cockpit_codex_switch_markers",
                return_value=[],
            ),
            patch.object(client_usage_export, "load_account_timeline", return_value=[]),
            patch.object(
                client_usage_export,
                "scan_cockpit_codex_account_markers",
                return_value=[final_marker],
            ),
            patch.object(
                client_usage_export,
                "scan_cockpit_codex_affinity_events",
                return_value=[affinity],
            ) as affinity_scan,
            patch.object(
                client_usage_export,
                "merge_missing_cockpit_account_events",
                side_effect=lambda attributed, _markers, _affinity=None, **_kwargs: (
                    attributed,
                    0,
                ),
            ),
            patch.object(
                client_usage_export,
                "previous_active_session_account_labels",
                return_value={},
            ),
            patch.object(client_usage_export, "cockpit_codex_speed_by_label", return_value={}),
        ):
            root = Path(temporary_directory)
            payload = client_usage_export.build_live_catchup_payload(
                root,
                root / ".codex" / "sessions",
                root / "usage.json",
                day_start,
                turn_started_at + timedelta(minutes=2),
            )

        affinity_scan.assert_called_once_with(
            root,
            day_start,
            turn_started_at + timedelta(minutes=2),
            [final_marker],
        )
        providers = {row["name"]: row for row in payload["providers"]}
        self.assertEqual(payload["unresolved_events"], 0)
        self.assertEqual(providers["Codex local - plus@example.com"]["tokens"], 1_000)
        self.assertNotIn(client_usage_export.API_SERVICE_AGGREGATE_LABEL, providers)

    def test_live_catchup_scans_quota_boundary_but_summarizes_current_day(self) -> None:
        quota_start = datetime(2026, 8, 25, 22, 17, 0)
        day_start = datetime(2026, 8, 26, 0, 0, 0)
        through = day_start + timedelta(hours=1)
        provider = "Codex local - plus@example.com"
        previous_day_event = client_usage_export.UsageEvent(
            when=quota_start + timedelta(minutes=10),
            model="gpt-test",
            input_tokens=70,
            cached_tokens=20,
            output_tokens=10,
            session_id="session-previous",
        )
        current_day_event = client_usage_export.UsageEvent(
            when=day_start + timedelta(minutes=10),
            model="gpt-test",
            input_tokens=150,
            cached_tokens=30,
            output_tokens=20,
            session_id="session-current",
        )
        events = [previous_day_event, current_day_event]

        with (
            tempfile.TemporaryDirectory() as temporary_directory,
            patch.object(client_usage_export, "record_current_opencodex_account_snapshot"),
            patch.object(
                client_usage_export,
                "scan_all_codex_events",
                return_value=events,
            ) as codex_scan,
            patch.object(client_usage_export, "codex_speed_history", return_value=[]),
            patch.object(client_usage_export, "current_codex_account_label", return_value=""),
            patch.object(client_usage_export, "load_attribution_ledger", return_value={}),
            patch.object(client_usage_export, "load_attribution_verdicts", return_value={}),
            patch.object(client_usage_export, "scan_cockpit_codex_switch_markers", return_value=[]),
            patch.object(client_usage_export, "load_account_timeline", return_value=[]),
            patch.object(client_usage_export, "scan_cockpit_codex_account_markers", return_value=[]),
            patch.object(client_usage_export, "scan_cockpit_codex_affinity_events", return_value=[]),
            patch.object(
                client_usage_export,
                "reconcile_cockpit_request_usage_events",
                return_value=events,
            ),
            patch.object(
                client_usage_export,
                "attribute_codex_events_by_account",
                return_value={provider: events},
            ),
            patch.object(
                client_usage_export,
                "merge_missing_cockpit_account_events",
                return_value=({provider: events}, 0),
            ),
            patch.object(
                client_usage_export,
                "resolve_api_service_event_accounts",
                return_value=({provider: events}, {}, 0),
            ),
            patch.object(client_usage_export, "previous_active_session_account_labels", return_value={}),
            patch.object(client_usage_export, "cockpit_codex_speed_by_label", return_value={}),
            patch.object(client_usage_export, "scan_claude", return_value=client_usage_export.UsageBucket()),
            patch.object(client_usage_export, "scan_grok_events", return_value=[]),
        ):
            root = Path(temporary_directory)
            payload = client_usage_export.build_live_catchup_payload(
                root,
                root / ".codex" / "sessions",
                root / "usage.json",
                quota_start,
                through,
            )

        self.assertEqual(codex_scan.call_args.args[2:4], (quota_start, through))
        self.assertEqual(len(payload["events"]), 2)
        self.assertEqual(payload["summary"]["tokens"], 200)
        self.assertEqual(payload["summary"]["requests"], 1)
        providers = {row["name"]: row for row in payload["providers"]}
        self.assertEqual(providers[provider]["tokens"], 200)
        self.assertEqual(providers[provider]["requests"], 1)

    def test_cockpit_zero_usage_failures_are_not_account_markers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            cockpit = root / ".antigravity_cockpit"
            cockpit.mkdir()
            database = cockpit / "codex_local_access_logs.sqlite"
            connection = sqlite3.connect(database)
            connection.execute(
                """
                CREATE TABLE request_logs (
                    timestamp INTEGER,
                    account_id TEXT,
                    email TEXT,
                    api_key_label TEXT,
                    model_id TEXT,
                    success INTEGER,
                    total_tokens INTEGER,
                    input_tokens INTEGER,
                    cached_tokens INTEGER,
                    output_tokens INTEGER,
                    event_key TEXT,
                    latency_ms INTEGER
                )
                """
            )
            at = datetime(2026, 7, 12, 8, 0, 0)
            connection.executemany(
                "INSERT INTO request_logs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        client_usage_export.local_epoch_ms(at),
                        "k12-id",
                        "k12@example.com",
                        "Default",
                        "gpt-test",
                        0,
                        0,
                        0,
                        0,
                        0,
                        "failed",
                        500,
                    ),
                    (
                        client_usage_export.local_epoch_ms(at + timedelta(seconds=1)),
                        "plus-id",
                        "plus@example.com",
                        "Default",
                        "gpt-test",
                        1,
                        123,
                        120,
                        20,
                        3,
                        "completed",
                        1_250,
                    ),
                    (
                        client_usage_export.local_epoch_ms(at + timedelta(seconds=2)),
                        "cancelled-id",
                        "cancelled@example.com",
                        "Default",
                        "gpt-test",
                        0,
                        321,
                        300,
                        20,
                        1,
                        "cancelled-with-usage",
                        2_500,
                    ),
                ],
            )
            connection.commit()
            connection.close()

            markers = client_usage_export.scan_cockpit_codex_account_markers(
                root,
                at - timedelta(seconds=1),
                at + timedelta(seconds=3),
            )

        self.assertEqual(
            [marker.label for marker in markers],
            [
                "Codex local - plus@example.com",
                "Codex local - cancelled@example.com",
            ],
        )
        self.assertEqual(markers[0].total_tokens, 123)
        self.assertEqual(markers[1].total_tokens, 321)
        self.assertEqual(markers[0].latency_ms, 1_250)
        self.assertEqual(markers[1].latency_ms, 2_500)

    def test_unique_token_match_outside_activity_window_is_not_reused(self) -> None:
        event = client_usage_export.UsageEvent(
            when=datetime(2026, 7, 12, 8, 10, 0),
            model="gpt-test",
            input_tokens=900,
            cached_tokens=0,
            output_tokens=100,
        )
        stale_marker = client_usage_export.AccountMarker(
            when=datetime(2026, 7, 12, 8, 0, 0),
            label="Codex local - stale-k12@example.com",
            total_tokens=event.total_tokens,
        )

        self.assertIsNone(
            client_usage_export.concrete_api_service_account_marker(
                event,
                [stale_marker],
            )
        )

    def test_cockpit_union_adds_only_requests_missing_from_client_logs(self) -> None:
        client_event = client_usage_export.UsageEvent(
            when=datetime(2026, 7, 12, 8, 0, 0),
            model="gpt-test",
            input_tokens=900,
            cached_tokens=0,
            output_tokens=100,
            session_id="session-1",
        )
        represented = client_usage_export.AccountMarker(
            when=client_event.when,
            label="Codex local - account@example.com",
            model="gpt-5.6-sol",
            total_tokens=1_000,
            input_tokens=900,
            output_tokens=100,
            event_key="represented",
        )
        missing = client_usage_export.AccountMarker(
            when=datetime(2026, 7, 12, 8, 1, 0),
            label="Codex local - account@example.com",
            model="gpt-5.6-sol",
            total_tokens=2_000,
            input_tokens=1_800,
            output_tokens=200,
            event_key="missing",
        )

        merged, added = client_usage_export.merge_missing_cockpit_account_events(
            {"Codex local - api-service-local": [client_event]},
            [represented, missing],
        )

        self.assertEqual(added, 1)
        self.assertEqual(sum(event.total_tokens for events in merged.values() for event in events), 3_000)
        fallback = merged["Codex local - account@example.com"][0]
        self.assertEqual(fallback.route, "cockpit-db-fallback")
        self.assertEqual(fallback.model, "gpt-5.6-sol")

    def test_cockpit_union_defers_recent_unrepresented_request_during_grace_period(self) -> None:
        marker = client_usage_export.AccountMarker(
            when=datetime(2026, 7, 23, 11, 5, 6, 843000),
            label="Codex local - delayed-client-log@example.com",
            model="gpt-5.6-sol",
            total_tokens=188_786,
            input_tokens=188_679,
            cached_tokens=184_064,
            output_tokens=107,
            event_key="recent-marker",
            request_id="recent-request",
            account_id="recent-account",
        )

        deferred, deferred_count = (
            client_usage_export.merge_missing_cockpit_account_events(
                {},
                [marker],
                fallback_before=marker.when - timedelta(seconds=1),
            )
        )
        settled, settled_count = (
            client_usage_export.merge_missing_cockpit_account_events(
                {},
                [marker],
                fallback_before=marker.when + timedelta(seconds=1),
            )
        )

        self.assertEqual(deferred, {})
        self.assertEqual(deferred_count, 0)
        self.assertEqual(settled_count, 1)
        self.assertEqual(settled[marker.label][0].total_tokens, marker.total_tokens)

    def test_exact_token_match_uses_explicit_request_latency_interval(self) -> None:
        event = client_usage_export.UsageEvent(
            when=datetime(2026, 7, 23, 18, 57, 43),
            model="gpt-5.5",
            input_tokens=76_752,
            cached_tokens=10_624,
            output_tokens=772,
        )
        marker = client_usage_export.AccountMarker(
            when=event.when + timedelta(seconds=360),
            label="Codex local - final@example.com",
            total_tokens=event.total_tokens,
            input_tokens=event.input_tokens,
            cached_tokens=event.cached_tokens,
            output_tokens=event.output_tokens,
            latency_ms=360_500,
        )

        self.assertIs(
            client_usage_export.concrete_api_service_account_marker(
                event,
                [marker],
            ),
            marker,
        )

    def test_exact_token_match_accepts_request_start_timestamp(self) -> None:
        event = client_usage_export.UsageEvent(
            when=datetime(2026, 7, 29, 8, 36, 30, 118000),
            model="gpt-5.6-sol",
            input_tokens=1_127,
            cached_tokens=204_544,
            output_tokens=31,
        )
        marker = client_usage_export.AccountMarker(
            when=datetime(2026, 7, 29, 8, 31, 29, 292000),
            label="Codex local - final@example.com",
            total_tokens=event.total_tokens,
            input_tokens=event.input_tokens,
            cached_tokens=event.cached_tokens,
            output_tokens=event.output_tokens,
            latency_ms=309_565,
        )

        self.assertIs(
            client_usage_export.concrete_api_service_account_marker(
                event,
                [marker],
            ),
            marker,
        )

    def test_cockpit_union_does_not_duplicate_request_start_marker(self) -> None:
        event = client_usage_export.UsageEvent(
            when=datetime(2026, 7, 29, 8, 36, 30, 118000),
            model="gpt-5.6-sol",
            input_tokens=1_127,
            cached_tokens=204_544,
            output_tokens=31,
            session_id="request-start-session",
        )
        marker = client_usage_export.AccountMarker(
            when=datetime(2026, 7, 29, 8, 31, 29, 292000),
            label="Codex local - final@example.com",
            model=event.model,
            total_tokens=event.total_tokens,
            input_tokens=event.input_tokens,
            cached_tokens=event.cached_tokens,
            output_tokens=event.output_tokens,
            event_key="request-start-marker",
            latency_ms=309_565,
        )

        merged, added = client_usage_export.merge_missing_cockpit_account_events(
            {marker.label: [event]},
            [marker],
            fallback_before=marker.when + timedelta(hours=1),
        )

        self.assertEqual(added, 0)
        self.assertEqual(merged[marker.label], [event])

    def test_request_start_latency_resolves_intermediate_turn_to_final_account(self) -> None:
        turn_started_at = datetime(2026, 7, 23, 10, 4, 59, 40000)
        intermediate = client_usage_export.UsageEvent(
            when=datetime(2026, 7, 23, 10, 5, 30, 864000),
            model="gpt-5.6-sol",
            input_tokens=29_648,
            cached_tokens=3_840,
            output_tokens=1_479,
            session_id="long-cockpit-session",
            account_at=turn_started_at,
        )
        later = client_usage_export.UsageEvent(
            when=datetime(2026, 7, 23, 10, 22, 46, 579000),
            model="gpt-5.6-sol",
            input_tokens=483,
            cached_tokens=185_088,
            output_tokens=73,
            session_id=intermediate.session_id,
            account_at=datetime(2026, 7, 23, 10, 6, 6, 327000),
        )
        target = client_usage_export.AccountMarker(
            when=datetime(2026, 7, 23, 10, 23, 20, 511000),
            label="Codex local - final-long@example.com",
            model="gpt-5.6-sol",
            total_tokens=later.total_tokens,
            input_tokens=185_571,
            cached_tokens=later.cached_tokens,
            output_tokens=later.output_tokens,
            request_id="final-long-request",
            account_id="final-long-account",
            latency_ms=1_101_207,
        )
        overlapping = client_usage_export.AccountMarker(
            when=datetime(2026, 7, 23, 10, 7, 2, 490000),
            label="Codex local - older-overlap@example.com",
            model="gpt-5.6-sol",
            total_tokens=192_171,
            input_tokens=192_068,
            cached_tokens=183_040,
            output_tokens=103,
            request_id="older-overlap-request",
            account_id="older-overlap-account",
            latency_ms=478_367,
        )
        affinity_events = [
            client_usage_export.CockpitAffinityEvent(
                when=turn_started_at + timedelta(milliseconds=218),
                request_id=overlapping.request_id,
                account_id=overlapping.account_id,
                label=overlapping.label,
                action="cache hit",
            ),
            client_usage_export.CockpitAffinityEvent(
                when=turn_started_at + timedelta(milliseconds=273),
                request_id=target.request_id,
                account_id=target.account_id,
                label=target.label,
                action="cache hit",
            ),
        ]

        resolved, session_accounts, unresolved = (
            client_usage_export.resolve_api_service_event_accounts(
                {client_usage_export.API_SERVICE_AGGREGATE_LABEL: [intermediate, later]},
                [overlapping, target],
                affinity_events=affinity_events,
            )
        )

        self.assertEqual(resolved[target.label], [intermediate, later])
        self.assertEqual(session_accounts[intermediate.session_id], target.label)
        self.assertEqual(unresolved, 0)

    def test_request_start_latency_filters_concurrent_request_by_model(self) -> None:
        turn_started_at = datetime(2026, 7, 23, 13, 54, 34, 432000)
        event = client_usage_export.UsageEvent(
            when=datetime(2026, 7, 23, 13, 54, 50, 254000),
            model="gpt-5.6-sol",
            input_tokens=29_267,
            cached_tokens=3_840,
            output_tokens=524,
            session_id="model-filter-session",
            account_at=turn_started_at,
        )
        target = client_usage_export.AccountMarker(
            when=datetime(2026, 7, 23, 14, 8, 42, 125000),
            label="Codex local - sol-final@example.com",
            model=event.model,
            total_tokens=132_442,
            input_tokens=132_366,
            cached_tokens=131_840,
            output_tokens=76,
            request_id="sol-final-request",
            account_id="sol-final-account",
            latency_ms=847_432,
        )
        other_model = client_usage_export.AccountMarker(
            when=datetime(2026, 7, 23, 14, 0, 0),
            label="Codex local - other-model@example.com",
            model="gpt-5.5",
            total_tokens=110_717,
            input_tokens=110_108,
            cached_tokens=106_880,
            output_tokens=609,
            request_id="other-model-request",
            account_id="other-model-account",
            latency_ms=325_700,
        )
        affinity_events = [
            client_usage_export.CockpitAffinityEvent(
                when=turn_started_at - timedelta(milliseconds=90),
                request_id=other_model.request_id,
                account_id=other_model.account_id,
                label=other_model.label,
                action="cache hit",
            ),
            client_usage_export.CockpitAffinityEvent(
                when=turn_started_at + timedelta(milliseconds=270),
                request_id=target.request_id,
                account_id=target.account_id,
                label=target.label,
                action="cache hit",
            ),
        ]

        resolved, session_accounts, unresolved = (
            client_usage_export.resolve_api_service_event_accounts(
                {client_usage_export.API_SERVICE_AGGREGATE_LABEL: [event]},
                [other_model, target],
                affinity_events=affinity_events,
            )
        )

        self.assertEqual(resolved[target.label], [event])
        self.assertEqual(session_accounts[event.session_id], target.label)
        self.assertEqual(unresolved, 0)

    def test_request_start_latency_keeps_concurrent_accounts_ambiguous(self) -> None:
        turn_started_at = datetime(2026, 7, 23, 15, 0, 0)
        event = client_usage_export.UsageEvent(
            when=turn_started_at + timedelta(seconds=5),
            model="gpt-5.6-sol",
            input_tokens=10_000,
            cached_tokens=2_000,
            output_tokens=100,
            session_id="ambiguous-request-start-session",
            account_at=turn_started_at,
        )
        markers = [
            client_usage_export.AccountMarker(
                when=turn_started_at + timedelta(seconds=60),
                label=f"Codex local - account-{index}@example.com",
                model=event.model,
                total_tokens=20_000 + index,
                input_tokens=19_900 + index,
                output_tokens=100,
                request_id=f"ambiguous-request-{index}",
                account_id=f"ambiguous-account-{index}",
                latency_ms=59_700 - index * 100,
            )
            for index in range(2)
        ]
        affinity_events = [
            client_usage_export.CockpitAffinityEvent(
                when=turn_started_at + timedelta(milliseconds=50),
                request_id=marker.request_id,
                account_id=marker.account_id,
                label=marker.label,
                action="cache hit",
            )
            for marker in markers
        ]

        resolved, session_accounts, unresolved = (
            client_usage_export.resolve_api_service_event_accounts(
                {client_usage_export.API_SERVICE_AGGREGATE_LABEL: [event]},
                markers,
                affinity_events=affinity_events,
            )
        )

        self.assertEqual(
            resolved[client_usage_export.API_SERVICE_AGGREGATE_LABEL],
            [event],
        )
        self.assertEqual(session_accounts, {})
        self.assertEqual(unresolved, 1)

    def test_cockpit_union_does_not_duplicate_near_time_request_with_different_token_total(self) -> None:
        event = client_usage_export.UsageEvent(
            when=datetime(2026, 7, 23, 9, 57, 6, 553000),
            model="gpt-5.6-sol",
            input_tokens=1_537,
            cached_tokens=122_624,
            output_tokens=144,
            session_id="session-different-token-total",
            account_at=datetime(2026, 7, 23, 9, 52, 35, 171000),
        )
        marker = client_usage_export.AccountMarker(
            when=datetime(2026, 7, 23, 9, 57, 6, 38000),
            label="Codex local - final@example.com",
            model="gpt-5.6-sol",
            total_tokens=8_786,
            input_tokens=8_786,
            event_key="final-row",
        )

        merged, added = client_usage_export.merge_missing_cockpit_account_events(
            {client_usage_export.API_SERVICE_AGGREGATE_LABEL: [event]},
            [marker],
        )
        resolved, _session_accounts, unresolved = (
            client_usage_export.resolve_api_service_event_accounts(
                merged,
                [marker],
            )
        )

        self.assertEqual(added, 0)
        self.assertEqual(resolved[marker.label], [event])
        self.assertEqual(
            sum(item.total_tokens for events in resolved.values() for item in events),
            event.total_tokens,
        )
        self.assertEqual(unresolved, 0)

    def test_cockpit_union_does_not_duplicate_delayed_final_request_id_match(self) -> None:
        turn_started_at = datetime(2026, 7, 23, 10, 36, 8, 927000)
        event = client_usage_export.UsageEvent(
            when=datetime(2026, 7, 23, 10, 36, 31, 212000),
            model="gpt-5.6-sol",
            input_tokens=30_000,
            cached_tokens=151_000,
            output_tokens=2_715,
            session_id="delayed-union-session",
            account_at=turn_started_at,
        )
        marker = client_usage_export.AccountMarker(
            when=event.when + timedelta(seconds=300, milliseconds=221),
            label="Codex local - delayed-union@example.com",
            model="gpt-5.6-sol",
            total_tokens=event.total_tokens,
            input_tokens=event.input_tokens,
            cached_tokens=event.cached_tokens,
            output_tokens=event.output_tokens,
            event_key="delayed-union-final",
            request_id="delayed-union-request",
            account_id="delayed-union-id",
        )
        affinity = client_usage_export.CockpitAffinityEvent(
            when=turn_started_at + timedelta(milliseconds=60),
            request_id=marker.request_id,
            source="execution_session_id",
            account_id=marker.account_id,
            label=marker.label,
            action="cache hit",
        )

        merged, added = client_usage_export.merge_missing_cockpit_account_events(
            {client_usage_export.API_SERVICE_AGGREGATE_LABEL: [event]},
            [marker],
            [affinity],
        )
        resolved, _session_accounts, unresolved = (
            client_usage_export.resolve_api_service_event_accounts(
                merged,
                [marker],
                affinity_events=[affinity],
            )
        )

        self.assertEqual(added, 0)
        self.assertEqual(resolved[marker.label], [event])
        self.assertEqual(
            sum(item.total_tokens for items in resolved.values() for item in items),
            event.total_tokens,
        )
        self.assertEqual(unresolved, 0)


class MonitorModeIsolationTests(unittest.TestCase):
    def test_auto_mode_uses_local_state_when_codex_endpoint_is_not_sub2api(self) -> None:
        sentinel = monitor.MonitorState(
            loading=False,
            mode="local-codex",
            usage_source="local",
            usage_note="local-only",
            today_requests=1,
            today_tokens=100,
        )
        with (
            patch.dict("os.environ", {"TOKEN_MONITOR_MODE": "auto"}, clear=False),
            patch.object(
                monitor.Sub2APIClient,
                "_codex_points_to_sub2api",
                return_value=(False, ["https://api.openai.com/v1"]),
            ),
            patch.object(monitor.Sub2APIClient, "fetch_sub2api_state") as fetch_sub2api,
            patch.object(monitor, "build_local_monitor_state", return_value=sentinel) as local_state,
        ):
            client = monitor.Sub2APIClient()
            state = client.fetch_state()

        self.assertIs(state, sentinel)
        fetch_sub2api.assert_not_called()
        local_state.assert_called_once()

    def test_auto_mode_uses_sub2api_state_when_codex_endpoint_matches(self) -> None:
        sentinel = monitor.MonitorState(
            loading=False,
            mode="sub2api",
            usage_source="sub2api",
            usage_note="sub2api",
            today_requests=2,
            today_tokens=200,
        )
        with (
            patch.dict("os.environ", {"TOKEN_MONITOR_MODE": "auto"}, clear=False),
            patch.object(
                monitor.Sub2APIClient,
                "_codex_points_to_sub2api",
                return_value=(True, ["http://127.0.0.1:63685/v1"]),
            ),
            patch.object(monitor.Sub2APIClient, "fetch_sub2api_state", return_value=sentinel) as fetch_sub2api,
            patch.object(monitor, "build_local_monitor_state") as local_state,
        ):
            client = monitor.Sub2APIClient()
            state = client.fetch_state()

        self.assertIs(state, sentinel)
        fetch_sub2api.assert_called_once()
        local_state.assert_not_called()


class LocalExportHighWaterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.output_path = Path(self.temporary_directory.name) / "client_usage_today.json"
        self.history_path = Path(self.temporary_directory.name) / "usage_history.json"
        self.original_history_path = client_usage_export.USAGE_HISTORY_PATH
        client_usage_export.USAGE_HISTORY_PATH = self.history_path
        self.day = date.today()

    def tearDown(self) -> None:
        client_usage_export.USAGE_HISTORY_PATH = self.original_history_path
        self.temporary_directory.cleanup()

    def snapshot(self, snapshot_day: date, tokens: int) -> dict:
        return {
            "date": snapshot_day.isoformat(),
            "today": {
                "requests": 10,
                "tokens": tokens,
                "input_tokens": tokens,
                "cached_input_tokens": 0,
                "cache_creation_input_tokens": 0,
                "output_tokens": 0,
                "cost": 1.0,
            },
            "providers": [
                {
                    "name": "Codex local - account@example.com",
                    "requests": 10,
                    "tokens": tokens,
                    "input_tokens": tokens,
                    "cached_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "output_tokens": 0,
                    "cost": 1.0,
                    "window_7d": {
                        "requests": 20,
                        "tokens": tokens * 2,
                        "cost": 2.0,
                        "quota_available": True,
                    },
                }
            ],
            "latest_request": {
                "provider": "Codex local - account@example.com",
                "model": "gpt-test",
                "created_at": f"{snapshot_day.isoformat()}T09:00:00+08:00",
                "kind": "success",
            },
            "dashboard": {
                "hourly_today": [
                    {"hour": 9, "requests": 10, "tokens": tokens, "cost": 1.0}
                ]
            },
        }

    def test_same_day_switch_preserves_totals_but_uses_current_quota(self) -> None:
        previous = self.snapshot(self.day, 1_000_000)
        current = self.snapshot(self.day, 100_000)
        previous["providers"][0]["window_7d"].update(
            {"remaining_percent": 31.0, "utilization": 69.0}
        )
        current["providers"][0]["window_7d"].update(
            {"remaining_percent": 17.0, "utilization": 83.0}
        )
        self.output_path.write_text(json.dumps(previous), encoding="utf-8")

        client_usage_export.same_day_output_high_water(current, self.output_path, self.day)

        self.assertEqual(current["today"]["tokens"], 1_000_000)
        self.assertEqual(current["providers"][0]["window_7d"]["tokens"], 200_000)
        self.assertEqual(current["providers"][0]["window_7d"]["utilization"], 83.0)
        self.assertEqual(current["dashboard"]["hourly_today"][0]["tokens"], 1_000_000)
        self.assertEqual(current["latest_request"]["model"], "gpt-test")

    def test_high_water_never_restores_cached_failure_annotations(self) -> None:
        for previous_tokens, current_tokens in ((0, 0), (1_000_000, 100_000)):
            with self.subTest(
                previous_tokens=previous_tokens,
                current_tokens=current_tokens,
            ):
                previous = self.snapshot(self.day, previous_tokens)
                current = self.snapshot(self.day, current_tokens)
                previous["dashboard"]["hourly_today"][0].update(
                    {
                        "failure": True,
                        "failure_count": 1,
                        "failure_at": f"{self.day.isoformat()}T08:59:53+08:00",
                        "failure_kind": "desktop_network",
                    }
                )
                self.output_path.write_text(json.dumps(previous), encoding="utf-8")

                client_usage_export.same_day_output_high_water(
                    current,
                    self.output_path,
                    self.day,
                )

                hourly = current["dashboard"]["hourly_today"][0]
                self.assertFalse(hourly.get("failure"))
                self.assertNotIn("failure_count", hourly)
                self.assertNotIn("failure_at", hourly)
                self.assertNotIn("failure_kind", hourly)

    def test_high_water_preserves_failure_from_current_scan(self) -> None:
        previous = self.snapshot(self.day, 1_000_000)
        current = self.snapshot(self.day, 100_000)
        current["dashboard"]["hourly_today"][0].update(
            {
                "failure": True,
                "failure_count": 1,
                "failure_at": f"{self.day.isoformat()}T08:59:53+08:00",
                "failure_kind": "desktop_network",
            }
        )
        self.output_path.write_text(json.dumps(previous), encoding="utf-8")

        client_usage_export.same_day_output_high_water(current, self.output_path, self.day)

        hourly = current["dashboard"]["hourly_today"][0]
        self.assertEqual(hourly["tokens"], 1_000_000)
        self.assertTrue(hourly["failure"])
        self.assertEqual(hourly["failure_count"], 1)
        self.assertEqual(hourly["failure_at"], f"{self.day.isoformat()}T08:59:53+08:00")
        self.assertEqual(hourly["failure_kind"], "desktop_network")

    def test_high_water_preserves_totals_but_keeps_newer_latest_timestamp(self) -> None:
        previous = self.snapshot(self.day, 1_000_000)
        current = self.snapshot(self.day, 100_000)
        previous["today"]["latest_at"] = f"{self.day.isoformat()}T16:21:22+08:00"
        previous["today"]["latest_model"] = "gpt-old"
        previous["providers"][0]["latest_at"] = f"{self.day.isoformat()}T16:21:22+08:00"
        previous["providers"][0]["latest_model"] = "gpt-old"
        current["today"]["latest_at"] = f"{self.day.isoformat()}T17:26:35+08:00"
        current["today"]["latest_model"] = "gpt-new"
        current["providers"][0]["latest_at"] = f"{self.day.isoformat()}T17:26:35+08:00"
        current["providers"][0]["latest_model"] = "gpt-new"
        self.output_path.write_text(json.dumps(previous), encoding="utf-8")

        client_usage_export.same_day_output_high_water(current, self.output_path, self.day)

        self.assertEqual(current["today"]["tokens"], 1_000_000)
        self.assertEqual(current["today"]["latest_at"], f"{self.day.isoformat()}T17:26:35+08:00")
        self.assertEqual(current["today"]["latest_model"], "gpt-new")
        self.assertEqual(current["providers"][0]["tokens"], 1_000_000)
        self.assertEqual(current["providers"][0]["latest_at"], f"{self.day.isoformat()}T17:26:35+08:00")
        self.assertEqual(current["providers"][0]["latest_model"], "gpt-new")

    def test_new_day_never_inherits_previous_day_high_water(self) -> None:
        yesterday = self.day - timedelta(days=1)
        previous = self.snapshot(yesterday, 1_000_000)
        current = self.snapshot(self.day, 100_000)
        self.output_path.write_text(json.dumps(previous), encoding="utf-8")

        client_usage_export.same_day_output_high_water(current, self.output_path, self.day)

        self.assertEqual(current["today"]["tokens"], 100_000)
        self.assertEqual(current["providers"][0]["window_7d"]["tokens"], 200_000)

    def test_normal_refresh_preserves_cached_30d_account_window(self) -> None:
        previous = self.snapshot(self.day, 1_000_000)
        current = self.snapshot(self.day, 1_100_000)
        previous["account_30d_updated_at"] = f"{self.day.isoformat()}T09:00:00+08:00"
        previous["providers"][0]["window_30d"] = {
            "requests": 120,
            "tokens": 88_000_000,
            "cost": 84.0,
        }
        self.output_path.write_text(json.dumps(previous), encoding="utf-8")

        client_usage_export.same_day_output_high_water(current, self.output_path, self.day)

        self.assertEqual(
            current["account_30d_updated_at"],
            previous["account_30d_updated_at"],
        )
        self.assertEqual(
            current["providers"][0]["window_30d"]["tokens"],
            88_000_000,
        )

    def test_usage_history_restores_today_high_water(self) -> None:
        current = self.snapshot(self.day, 100_000)
        self.history_path.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "days": {
                        self.day.isoformat(): {
                            "requests": 817,
                            "tokens": 114_001_494,
                            "input_tokens": 7_768_363,
                            "cached_input_tokens": 102_232_192,
                            "cache_creation_input_tokens": 0,
                            "output_tokens": 462_026,
                            "cost": 107.739207,
                        }
                    },
                }
            ),
            encoding="utf-8",
        )

        client_usage_export.restore_today_from_usage_history(current, self.day)

        self.assertEqual(current["today"]["tokens"], 114_001_494)
        self.assertEqual(current["today"]["requests"], 817)
        self.assertEqual(current["providers"][0]["tokens"], 100_000)
        self.assertEqual(current["providers"][1]["name"], client_usage_export.HIGH_WATER_UNATTRIBUTED_LABEL)
        self.assertEqual(current["providers"][1]["tokens"], 113_901_494)

    def test_unattributed_gap_provider_matches_today_total(self) -> None:
        current = self.snapshot(self.day, 1_000_000)
        current["providers"][0]["tokens"] = 600_000
        current["providers"][0]["requests"] = 6
        current["providers"][0]["cost"] = 0.6

        client_usage_export.add_unattributed_provider_gap(current)

        self.assertEqual(current["providers"][1]["name"], client_usage_export.HIGH_WATER_UNATTRIBUTED_LABEL)
        self.assertEqual(current["providers"][1]["tokens"], 400_000)
        self.assertEqual(
            sum(int(provider.get("tokens") or 0) for provider in current["providers"]),
            current["today"]["tokens"],
        )

    def test_api_service_providers_are_collapsed_without_changing_total(self) -> None:
        current = self.snapshot(self.day, 1_000)
        current["providers"] = [
            {"name": "Codex local - account@example.com", "requests": 4, "tokens": 400, "cost": 0.4},
            {"name": "Codex local - codex_local_access_runtime", "requests": 3, "tokens": 300, "cost": 0.3},
            {"name": "Codex local - api-service-local", "requests": 3, "tokens": 300, "cost": 0.3},
        ]

        aggregate = client_usage_export.collapse_api_service_mirror_providers(current)

        self.assertEqual(aggregate["tokens"], 600)
        self.assertEqual(current["today"]["tokens"], 1_000)
        self.assertEqual(
            [row["name"] for row in current["providers"]],
            ["Codex local - account@example.com", client_usage_export.API_SERVICE_AGGREGATE_LABEL],
        )
        self.assertEqual(sum(row["tokens"] for row in current["providers"]), current["today"]["tokens"])

    def test_api_service_routing_keeps_total_high_water_without_restoring_account_rows(self) -> None:
        previous = self.snapshot(self.day, 1_000_000)
        current = self.snapshot(self.day, 100_000)
        current["api_service_routed"] = True
        self.output_path.write_text(json.dumps(previous), encoding="utf-8")

        client_usage_export.same_day_output_high_water(current, self.output_path, self.day)

        self.assertEqual(current["today"]["tokens"], 1_000_000)
        self.assertEqual(current["dashboard"]["hourly_today"][0]["tokens"], 1_000_000)
        self.assertEqual(current["providers"][0]["tokens"], 100_000)

    def test_high_water_does_not_replace_direct_account_with_api_pool_high_water(self) -> None:
        previous = self.snapshot(self.day, 1_400)
        previous["providers"] = [
            {"name": "Codex local - account@example.com", "requests": 8, "tokens": 800, "cost": 0.8},
            {"name": "Codex local - codex_local_access_runtime", "requests": 6, "tokens": 600, "cost": 0.6},
            {"name": client_usage_export.HIGH_WATER_UNATTRIBUTED_LABEL, "requests": 4, "tokens": 400, "cost": 0.4},
        ]
        current = self.snapshot(self.day, 1_100)
        current["providers"] = [
            {"name": "Codex local - account@example.com", "requests": 5, "tokens": 500, "cost": 0.5},
            {"name": client_usage_export.API_SERVICE_AGGREGATE_LABEL, "requests": 6, "tokens": 600, "cost": 0.6},
        ]
        current["api_service_aggregate"] = {"requests": 6, "tokens": 600, "cost": 0.6}
        self.output_path.write_text(json.dumps(previous), encoding="utf-8")

        client_usage_export.same_day_output_high_water(current, self.output_path, self.day)

        direct = next(row for row in current["providers"] if row["name"] == "Codex local - account@example.com")
        self.assertEqual(direct["tokens"], 500)
        self.assertEqual(current["today"]["tokens"], 1_100)
        self.assertFalse(
            any(row["name"] == client_usage_export.HIGH_WATER_UNATTRIBUTED_LABEL for row in current["providers"])
        )

    def test_history_restore_is_skipped_when_api_aggregate_is_present(self) -> None:
        current = self.snapshot(self.day, 1_100)
        current["api_service_aggregate"] = {"requests": 6, "tokens": 600, "cost": 0.6}
        self.history_path.write_text(
            json.dumps({"days": {self.day.isoformat(): {"requests": 20, "tokens": 2_000, "input_tokens": 2_000, "cost": 2.0}}}),
            encoding="utf-8",
        )

        client_usage_export.restore_today_from_usage_history(current, self.day)

        self.assertEqual(current["today"]["tokens"], 1_100)


    def test_claude_schema_upgrade_does_not_restore_legacy_duplicate_high_water(self) -> None:
        previous = self.snapshot(self.day, 800_000)
        previous["today"]["tokens"] = 1_000_000
        previous["today"]["input_tokens"] = 1_000_000
        previous["providers"].append(
            {
                "name": "Claude local",
                "requests": 2,
                "tokens": 200_000,
                "input_tokens": 200_000,
                "cached_input_tokens": 0,
                "cache_creation_input_tokens": 0,
                "output_tokens": 0,
                "cost": 2.0,
            }
        )
        previous["dashboard"]["hourly_today"][0]["tokens"] = 1_000_000
        self.output_path.write_text(json.dumps(previous), encoding="utf-8")
        self.history_path.write_text(
            json.dumps({"days": {self.day.isoformat(): previous["today"]}}),
            encoding="utf-8",
        )

        current = self.snapshot(self.day, 800_000)
        current["claude_usage_schema"] = client_usage_export.CLAUDE_USAGE_DEDUPE_SCHEMA
        current["today"]["tokens"] = 880_000
        current["today"]["input_tokens"] = 880_000
        current["providers"].append(
            {
                "name": "Claude local",
                "requests": 1,
                "tokens": 80_000,
                "input_tokens": 80_000,
                "cached_input_tokens": 0,
                "cache_creation_input_tokens": 0,
                "output_tokens": 0,
                "cost": 0.8,
            }
        )
        current["dashboard"]["hourly_today"][0]["tokens"] = 880_000

        client_usage_export.same_day_output_high_water(current, self.output_path, self.day)
        client_usage_export.restore_today_from_usage_history(current, self.day)

        claude = next(
            provider
            for provider in current["providers"]
            if provider["name"] == "Claude local"
        )
        self.assertEqual(current["today"]["tokens"], 880_000)
        self.assertEqual(claude["tokens"], 80_000)
        self.assertEqual(current["dashboard"]["hourly_today"][0]["tokens"], 880_000)

    def test_cockpit_schema_upgrade_does_not_restore_duplicate_high_water(self) -> None:
        previous = self.snapshot(self.day, 1_000_000)
        self.output_path.write_text(json.dumps(previous), encoding="utf-8")
        self.history_path.write_text(
            json.dumps({"days": {self.day.isoformat(): previous["today"]}}),
            encoding="utf-8",
        )

        current = self.snapshot(self.day, 800_000)
        current["api_service_routed"] = True
        current["cockpit_usage_schema"] = (
            client_usage_export.COCKPIT_USAGE_DEDUPE_SCHEMA
        )

        client_usage_export.same_day_output_high_water(
            current,
            self.output_path,
            self.day,
        )
        client_usage_export.restore_today_from_usage_history(current, self.day)

        self.assertEqual(current["today"]["tokens"], 800_000)
        self.assertEqual(
            current["dashboard"]["hourly_today"][0]["tokens"],
            800_000,
        )

    def test_opencodex_schema_upgrade_drops_stale_account_buckets(self) -> None:
        previous = self.snapshot(self.day, 1_000_000)
        previous["providers"] = [
            {
                "name": "Codex local - wrong@example.com",
                "requests": 6,
                "tokens": 600_000,
                "input_tokens": 600_000,
                "cached_input_tokens": 0,
                "cache_creation_input_tokens": 0,
                "output_tokens": 0,
                "cost": 6.0,
            },
            {
                "name": client_usage_export.API_SERVICE_AGGREGATE_LABEL,
                "requests": 4,
                "tokens": 400_000,
                "input_tokens": 400_000,
                "cached_input_tokens": 0,
                "cache_creation_input_tokens": 0,
                "output_tokens": 0,
                "cost": 4.0,
            },
        ]
        self.output_path.write_text(json.dumps(previous), encoding="utf-8")
        self.history_path.write_text(
            json.dumps({"days": {self.day.isoformat(): previous["today"]}}),
            encoding="utf-8",
        )

        current = self.snapshot(self.day, 800_000)
        current["providers"][0]["name"] = "Codex local - will@example.com"
        current["opencodex_attribution_schema"] = (
            client_usage_export.OPENCODEX_ACCOUNT_ATTRIBUTION_SCHEMA
        )

        client_usage_export.same_day_output_high_water(
            current,
            self.output_path,
            self.day,
        )
        client_usage_export.restore_today_from_usage_history(current, self.day)

        self.assertEqual(current["today"]["tokens"], 800_000)
        self.assertEqual(
            [provider["name"] for provider in current["providers"]],
            ["Codex local - will@example.com"],
        )
        self.assertEqual(
            sum(int(provider.get("tokens") or 0) for provider in current["providers"]),
            current["today"]["tokens"],
        )

    def test_cockpit_schema_upgrade_does_not_restore_polluted_gpt_account(self) -> None:
        previous = self.snapshot(self.day, 1_000_000)
        previous["cockpit_usage_schema"] = 1
        previous["providers"][0]["models"] = {
            "gpt-test": 400_000,
            "xai/grok-4.6": 600_000,
        }
        self.output_path.write_text(json.dumps(previous), encoding="utf-8")

        current = self.snapshot(self.day, 400_000)
        current["cockpit_usage_schema"] = (
            client_usage_export.COCKPIT_USAGE_DEDUPE_SCHEMA
        )
        current["today"] = {
            "requests": 10,
            "tokens": 1_000_000,
            "input_tokens": 1_000_000,
            "cached_input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "output_tokens": 0,
            "cost": 1.0,
        }
        current["providers"] = [
            {
                "name": "Codex local - account@example.com",
                "requests": 4,
                "tokens": 400_000,
                "input_tokens": 400_000,
                "cached_input_tokens": 0,
                "cache_creation_input_tokens": 0,
                "output_tokens": 0,
                "cost": 0.4,
                "models": {"gpt-test": 400_000},
                "window_7d": {
                    "requests": 8,
                    "tokens": 800_000,
                    "cost": 0.8,
                    "quota_available": True,
                },
            },
            {
                "name": client_usage_export.GROK_SUBAGENT_LABEL,
                "requests": 6,
                "tokens": 600_000,
                "input_tokens": 600_000,
                "cached_input_tokens": 0,
                "cache_creation_input_tokens": 0,
                "output_tokens": 0,
                "cost": 0.6,
                "models": {"xai/grok-4.6": 600_000},
            },
        ]
        current["dashboard"]["hourly_today"][0]["tokens"] = 1_000_000

        client_usage_export.same_day_output_high_water(
            current,
            self.output_path,
            self.day,
        )

        gpt = next(
            provider
            for provider in current["providers"]
            if provider["name"] == "Codex local - account@example.com"
        )
        grok = next(
            provider
            for provider in current["providers"]
            if provider["name"] == client_usage_export.GROK_SUBAGENT_LABEL
        )
        self.assertEqual(gpt["tokens"], 400_000)
        self.assertEqual(gpt["models"], {"gpt-test": 400_000})
        self.assertEqual(grok["tokens"], 600_000)
        self.assertEqual(current["today"]["tokens"], 1_000_000)


class WindowSemanticsTests(unittest.TestCase):
    def test_fresh_cockpit_backup_supplies_newer_plus_quota(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backup_dir = root / ".antigravity_cockpit" / "backups"
            backup_dir.mkdir(parents=True)
            now = datetime(2026, 8, 25, 15, 0, tzinfo=client_usage_export.LOCAL_TZ)
            reset_at = int(
                datetime(
                    2026,
                    9,
                    1,
                    10,
                    5,
                    37,
                    tzinfo=client_usage_export.LOCAL_TZ,
                ).timestamp()
            )
            backup = backup_dir / "cockpit_auto_backup_full_2026-08-25_14-48-55.json"
            backup.write_text(
                json.dumps(
                    {
                        "exported_at": "2026-08-25T06:48:55Z",
                        "accounts": {
                            "platforms": {
                                "codex": {
                                    "exported_data": [
                                        {
                                            "id": "pawns-id",
                                            "email": "pawns@example.com",
                                            "plan_type": "plus",
                                            "quota": {
                                                "hourly_percentage": 5,
                                                "hourly_reset_time": reset_at,
                                                "hourly_window_minutes": 10_080,
                                                "hourly_window_present": True,
                                                "weekly_percentage": 100,
                                                "weekly_reset_time": None,
                                                "weekly_window_present": False,
                                            },
                                        }
                                    ]
                                }
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            quota = client_usage_export.cockpit_backup_quota_by_label(root, now)

        windows = quota["Codex local - pawns@example.com"]
        self.assertEqual(windows["window_7d"]["remaining_percent"], 5.0)
        self.assertEqual(windows["window_7d"]["utilization"], 95.0)
        self.assertEqual(windows["window_7d"]["window_minutes"], 10_080)
        self.assertTrue(windows["window_5h"]["quota_unlimited"])
        self.assertEqual(
            windows["window_7d"]["quota_transport"],
            "cockpit-backup",
        )



    def write_quota_account(
        self,
        root: Path,
        *,
        plan_type: str,
        hourly_minutes: int,
        hourly_percentage: int = 80,
        hourly_present: bool = True,
        weekly_percentage: int = 70,
        weekly_present: bool = False,
    ) -> str:
        accounts_dir = root / ".antigravity_cockpit" / "codex_accounts"
        accounts_dir.mkdir(parents=True)
        reset_5h = datetime(2026, 7, 13, 18, 0, tzinfo=client_usage_export.LOCAL_TZ)
        reset_7d = datetime(2026, 7, 19, 12, 0, tzinfo=client_usage_export.LOCAL_TZ)
        email = f"{plan_type}@example.com"
        payload = {
            "id": f"{plan_type}-account",
            "email": email,
            "plan_type": plan_type,
            "quota": {
                "hourly_percentage": hourly_percentage,
                "hourly_reset_time": int(
                    (reset_7d if hourly_minutes == 7 * 24 * 60 else reset_5h).timestamp()
                ),
                "hourly_window_minutes": hourly_minutes,
                "hourly_window_present": hourly_present,
                "weekly_percentage": weekly_percentage,
                "weekly_reset_time": int(reset_7d.timestamp()),
                "weekly_window_present": weekly_present,
            },
        }
        (accounts_dir / "account.json").write_text(json.dumps(payload), encoding="utf-8")
        return f"Codex local - {email}"

    def write_request_log(
        self,
        root: Path,
        email: str,
        events: list[tuple[datetime, int]],
    ) -> None:
        db_dir = root / ".antigravity_cockpit"
        db_dir.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(db_dir / "codex_local_access_logs.sqlite")
        con.execute(
            """
            CREATE TABLE request_logs (
                timestamp INTEGER,
                account_id TEXT,
                email TEXT,
                api_key_label TEXT,
                model_id TEXT,
                input_tokens INTEGER,
                output_tokens INTEGER,
                total_tokens INTEGER,
                cached_tokens INTEGER,
                estimated_cost_usd REAL
            )
            """
        )
        con.executemany(
            "INSERT INTO request_logs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    client_usage_export.local_epoch_ms(when),
                    "account-id",
                    email,
                    "",
                    "gpt-5.4",
                    tokens,
                    0,
                    tokens,
                    0,
                    0.0,
                )
                for when, tokens in events
            ],
        )
        con.commit()
        con.close()

    def test_encrypted_cockpit_accounts_use_sidecar_quota_reserve(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            cockpit = root / ".antigravity_cockpit"
            accounts_dir = cockpit / "codex_accounts"
            reserve_dir = cockpit / "codex_local_access_sidecar"
            accounts_dir.mkdir(parents=True)
            reserve_dir.mkdir(parents=True)
            accounts = [
                {
                    "id": "codex_k12",
                    "email": "k12@example.com",
                    "plan_type": "k12",
                },
                {
                    "id": "codex_plus",
                    "email": "plus@example.com",
                    "plan_type": "plus",
                },
            ]
            (cockpit / "codex_accounts.json").write_text(
                json.dumps({"accounts": accounts}),
                encoding="utf-8",
            )
            for account in accounts:
                (accounts_dir / f"{account['id']}.json").write_text(
                    json.dumps({"version": 1, "ciphertext": "encrypted"}),
                    encoding="utf-8",
                )
            snapshot_at = int(datetime.now().timestamp())
            (reserve_dir / "quota-reserve.json").write_text(
                json.dumps(
                    {
                        "accounts": {
                            "codex_k12": {
                                "hourlyRemainingPercent": 3,
                                "hourlyWindowPresent": True,
                                "weeklyRemainingPercent": 85,
                                "weeklyWindowPresent": True,
                                "snapshotUpdatedAtUnixSeconds": snapshot_at,
                            },
                            "codex_plus": {
                                "hourlyRemainingPercent": 26,
                                "hourlyWindowPresent": True,
                                "weeklyRemainingPercent": 100,
                                "weeklyWindowPresent": False,
                                "snapshotUpdatedAtUnixSeconds": snapshot_at,
                            },
                        }
                    }
                ),
                encoding="utf-8",
            )
            cache_path = root / "official-quota-cache.json"
            with patch.object(
                client_usage_export,
                "COCKPIT_OFFICIAL_QUOTA_CACHE_PATH",
                cache_path,
            ):
                quota = client_usage_export.cockpit_codex_quota_by_label(root)

        k12 = quota["Codex local - k12@example.com"]
        self.assertEqual(k12["window_5h"]["remaining_percent"], 3.0)
        self.assertEqual(k12["window_7d"]["remaining_percent"], 85.0)
        self.assertTrue(k12["window_5h"]["quota_reset_unavailable"])
        plus = quota["Codex local - plus@example.com"]
        self.assertTrue(plus["window_5h"]["quota_unlimited"])
        self.assertEqual(plus["window_7d"]["remaining_percent"], 26.0)
        self.assertTrue(plus["window_7d"]["quota_reset_unavailable"])

    def test_cockpit_sidecar_reset_times_prevent_official_request(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            cockpit = root / ".antigravity_cockpit"
            accounts_dir = cockpit / "codex_accounts"
            sidecar = cockpit / "codex_local_access_sidecar"
            auth_dir = sidecar / "auths"
            accounts_dir.mkdir(parents=True)
            auth_dir.mkdir(parents=True)
            account_id = "codex_local_reset"
            account = {
                "id": account_id,
                "email": "local-reset@example.com",
                "plan_type": "k12",
            }
            (cockpit / "codex_accounts.json").write_text(
                json.dumps({"accounts": [account]}),
                encoding="utf-8",
            )
            (accounts_dir / f"{account_id}.json").write_text(
                json.dumps({"version": 1, "ciphertext": "encrypted"}),
                encoding="utf-8",
            )
            now = datetime.now(client_usage_export.LOCAL_TZ)
            reset_5h = int((now + timedelta(hours=4)).timestamp())
            reset_7d = int((now + timedelta(days=6)).timestamp())
            (sidecar / "quota-reserve.json").write_text(
                json.dumps(
                    {
                        "accounts": {
                            account_id: {
                                "hourlyRemainingPercent": 64,
                                "hourlyWindowPresent": True,
                                "hourlyResetAtUnixSeconds": reset_5h,
                                "weeklyRemainingPercent": 41,
                                "weeklyWindowPresent": True,
                                "weeklyResetAtUnixSeconds": reset_7d,
                                "snapshotUpdatedAtUnixSeconds": int(now.timestamp()),
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            (auth_dir / f"{account_id}.json").write_text(
                json.dumps(
                    {
                        "access_token": "must-not-be-used",
                        "account_id": "chatgpt-account-id",
                        "expired": datetime.now().timestamp() + 3600,
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(client_usage_export.request, "urlopen") as urlopen:
                quota = client_usage_export.cockpit_codex_quota_by_label(root)

        urlopen.assert_not_called()
        windows = quota["Codex local - local-reset@example.com"]
        self.assertEqual(windows["window_5h"]["quota_source"], "sidecar-reserve")
        self.assertTrue(windows["window_5h"]["resets_at"])
        self.assertTrue(windows["window_7d"]["resets_at"])
        self.assertFalse(windows["window_5h"]["quota_reset_unavailable"])

    def test_official_quota_response_maps_5h_and_7d_reset_times(self) -> None:
        checked_at = datetime(2026, 7, 14, 12, 0, tzinfo=client_usage_export.LOCAL_TZ)
        reset_5h = int(datetime(2026, 7, 14, 17, 0, tzinfo=client_usage_export.LOCAL_TZ).timestamp())
        reset_7d = int(datetime(2026, 7, 20, 12, 0, tzinfo=client_usage_export.LOCAL_TZ).timestamp())

        quota = client_usage_export.official_quota_from_usage_response(
            {
                "plan_type": "k12",
                "rate_limit": {
                    "primary_window": {
                        "limit_window_seconds": 5 * 60 * 60,
                        "reset_at": reset_5h,
                        "used_percent": 34,
                    },
                    "secondary_window": {
                        "limit_window_seconds": 7 * 24 * 60 * 60,
                        "reset_at": reset_7d,
                        "used_percent": 61,
                    },
                },
            },
            checked_at,
        )

        assert quota is not None
        self.assertEqual(quota["window_5h"]["remaining_percent"], 66.0)
        self.assertEqual(quota["window_7d"]["remaining_percent"], 39.0)
        self.assertEqual(quota["window_5h"]["window_minutes"], 300)
        self.assertEqual(quota["window_7d"]["window_minutes"], 10080)
        self.assertTrue(quota["window_5h"]["resets_at"].startswith("2026-07-14T17:00:00"))
        self.assertTrue(quota["window_7d"]["resets_at"].startswith("2026-07-20T12:00:00"))

    def test_official_plus_7d_response_marks_5h_unlimited(self) -> None:
        checked_at = datetime(2026, 7, 14, 12, 0, tzinfo=client_usage_export.LOCAL_TZ)
        quota = client_usage_export.official_quota_from_usage_response(
            {
                "plan_type": "plus",
                "rate_limit": {
                    "primary_window": {
                        "limit_window_seconds": 7 * 24 * 60 * 60,
                        "reset_at": checked_at.timestamp() + 3 * 24 * 60 * 60,
                        "used_percent": 74,
                    },
                    "secondary_window": None,
                },
            },
            checked_at,
        )

        assert quota is not None
        self.assertTrue(quota["window_5h"]["quota_unlimited"])
        self.assertEqual(quota["window_7d"]["remaining_percent"], 26.0)

    def test_official_pro_7d_response_confirms_5h_absent(self) -> None:
        checked_at = datetime(2026, 8, 26, 12, 0, tzinfo=client_usage_export.LOCAL_TZ)
        quota = client_usage_export.official_quota_from_usage_response(
            {
                "plan_type": "pro",
                "rate_limit": {
                    "primary_window": {
                        "limit_window_seconds": 7 * 24 * 60 * 60,
                        "reset_at": checked_at.timestamp() + 6 * 24 * 60 * 60,
                        "used_percent": 13,
                    },
                    "secondary_window": None,
                },
            },
            checked_at,
        )

        assert quota is not None
        self.assertFalse(quota["window_5h"]["quota_available"])
        self.assertTrue(quota["window_5h"]["quota_absent_confirmed"])
        self.assertEqual(quota["window_5h"]["quota_source"], "official-wham")
        self.assertEqual(quota["window_7d"]["remaining_percent"], 87.0)

    def test_official_absent_window_overrides_newer_sidecar_value(self) -> None:
        current = {
            "quota_available": True,
            "quota_stale": False,
            "quota_source": "sidecar-reserve",
            "quota_snapshot_at": "2026-08-26T12:01:00+08:00",
            "window_minutes": 300,
            "remaining_percent": 87.0,
            "utilization": 13.0,
            "resets_at": "",
            "quota_reset_unavailable": True,
        }
        official = {
            "quota_available": False,
            "quota_stale": False,
            "quota_source": "official-wham",
            "quota_snapshot_at": "2026-08-26T12:00:00+08:00",
            "quota_absent_confirmed": True,
        }

        merged = client_usage_export.prefer_quota_window(current, official)

        self.assertFalse(merged["quota_available"])
        self.assertTrue(merged["quota_absent_confirmed"])
        self.assertEqual(merged["quota_source"], "official-wham")

    def test_official_quota_empty_rate_limit_is_not_success(self) -> None:
        checked_at = datetime(2026, 7, 14, 12, 0, tzinfo=client_usage_export.LOCAL_TZ)
        for payload in (
            {"plan_type": "plus", "rate_limit": {}},
            {"plan_type": "plus", "rate_limit": {"primary_window": None, "secondary_window": None}},
            {
                "plan_type": "k12",
                "rate_limit": {
                    "primary_window": {"limit_window_seconds": 5 * 60 * 60, "used_percent": 20},
                    "secondary_window": None,
                },
            },
            {
                "plan_type": "plus",
                "rate_limit": {
                    "primary_window": {
                        "limit_window_seconds": 7 * 24 * 60 * 60,
                        "reset_at": checked_at.timestamp() + 3600,
                    }
                },
            },
        ):
            self.assertIsNone(
                client_usage_export.official_quota_from_usage_response(payload, checked_at)
            )

    def test_official_quota_request_is_cached_for_ten_minutes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            auth_dir = (
                root
                / ".antigravity_cockpit"
                / "codex_local_access_sidecar"
                / "auths"
            )
            auth_dir.mkdir(parents=True)
            account_id = "codex_cached"
            (auth_dir / f"{account_id}.json").write_text(
                json.dumps(
                    {
                        "access_token": "secret-access-token",
                        "account_id": "chatgpt-account-id",
                        "disabled": False,
                        "expired": datetime.now().timestamp() + 3600,
                    }
                ),
                encoding="utf-8",
            )
            cache_path = root / "official-quota-cache.json"
            checked_at = datetime(2026, 7, 14, 12, 0, tzinfo=client_usage_export.LOCAL_TZ)
            response_payload = {
                "plan_type": "plus",
                "rate_limit": {
                    "primary_window": {
                        "limit_window_seconds": 7 * 24 * 60 * 60,
                        "reset_at": checked_at.timestamp() + 2 * 24 * 60 * 60,
                        "used_percent": 20,
                    },
                    "secondary_window": None,
                },
            }
            response = MagicMock()
            response.__enter__.return_value.read.return_value = json.dumps(response_payload).encode("utf-8")
            accounts = {account_id: {"plan_type": "plus"}}
            with (
                patch.object(client_usage_export, "COCKPIT_OFFICIAL_QUOTA_CACHE_PATH", cache_path),
                patch.object(client_usage_export.request, "urlopen", return_value=response) as urlopen,
            ):
                first = client_usage_export.cockpit_official_quota_by_account(root, accounts, checked_at)
                second = client_usage_export.cockpit_official_quota_by_account(
                    root,
                    accounts,
                    checked_at + timedelta(minutes=9, seconds=59),
                )
                third = client_usage_export.cockpit_official_quota_by_account(
                    root,
                    accounts,
                    checked_at + timedelta(minutes=10),
                )

            self.assertEqual(urlopen.call_count, 2)
            self.assertEqual(first, second)
            self.assertEqual(
                first[account_id]["window_7d"]["remaining_percent"],
                third[account_id]["window_7d"]["remaining_percent"],
            )
            self.assertNotIn("secret-access-token", cache_path.read_text(encoding="utf-8"))

    def test_active_official_quota_refresh_uses_two_minute_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            auth_dir = (
                root
                / ".antigravity_cockpit"
                / "codex_local_access_sidecar"
                / "auths"
            )
            auth_dir.mkdir(parents=True)
            account_id = "codex_active"
            (auth_dir / f"{account_id}.json").write_text(
                json.dumps(
                    {
                        "access_token": "secret-access-token",
                        "account_id": "chatgpt-account-id",
                        "expired": datetime.now().timestamp() + 3600,
                    }
                ),
                encoding="utf-8",
            )
            cache_path = root / "official-quota-cache.json"
            checked_at = datetime(2026, 7, 14, 12, 0, tzinfo=client_usage_export.LOCAL_TZ)
            response_payload = {
                "plan_type": "plus",
                "rate_limit": {
                    "primary_window": {
                        "limit_window_seconds": 7 * 24 * 60 * 60,
                        "reset_at": checked_at.timestamp() + 2 * 24 * 60 * 60,
                        "used_percent": 20,
                    },
                    "secondary_window": None,
                },
            }
            response = MagicMock()
            response.__enter__.return_value.read.return_value = json.dumps(response_payload).encode("utf-8")
            accounts = {account_id: {"plan_type": "plus"}}
            with (
                patch.object(client_usage_export, "COCKPIT_OFFICIAL_QUOTA_CACHE_PATH", cache_path),
                patch.object(client_usage_export.request, "urlopen", return_value=response) as urlopen,
            ):
                client_usage_export.cockpit_official_quota_by_account(
                    root,
                    accounts,
                    checked_at,
                )
                client_usage_export.cockpit_official_quota_by_account(
                    root,
                    accounts,
                    checked_at + timedelta(minutes=1, seconds=59),
                    active_account_ids={account_id},
                )
                client_usage_export.cockpit_official_quota_by_account(
                    root,
                    accounts,
                    checked_at + timedelta(minutes=2),
                    active_account_ids={account_id},
                )

            self.assertEqual(urlopen.call_count, 2)

    def test_current_direct_account_outside_cockpit_pool_is_still_active(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            codex_dir = root / ".codex"
            codex_dir.mkdir(parents=True)
            email = "direct-current@example.com"
            (codex_dir / "auth.json").write_text(
                json.dumps(
                    {
                        "tokens": {
                            "access_token": "home-access-token",
                            "id_token": CodexAuthIdentityTests.jwt(
                                {
                                    "email": email,
                                    "https://api.openai.com/auth": {
                                        "chatgpt_account_id": "direct-chatgpt-account-id",
                                    },
                                }
                            ),
                            "account_id": "direct-chatgpt-account-id",
                        }
                    }
                ),
                encoding="utf-8",
            )
            cache_path = root / "official-quota-cache.json"
            with (
                patch.object(client_usage_export, "COCKPIT_OFFICIAL_QUOTA_CACHE_PATH", cache_path),
                patch.object(
                    client_usage_export,
                    "cockpit_official_quota_by_account",
                    return_value={},
                ) as refresh,
            ):
                client_usage_export.cockpit_codex_quota_by_label(
                    root,
                    force_active_official_refresh=True,
                )

            candidates = refresh.call_args.args[1]
            active_ids = refresh.call_args.kwargs["active_account_ids"]
            self.assertEqual(set(candidates), active_ids)
            self.assertEqual(len(active_ids), 1)
            current = candidates[next(iter(active_ids))]
            self.assertEqual(current["email"], email)

    def test_recent_quota_accounts_require_real_usage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            cockpit = root / ".antigravity_cockpit"
            cockpit.mkdir(parents=True)
            db_path = cockpit / "codex_local_access_logs.sqlite"
            now = datetime(2026, 7, 14, 12, 0, tzinfo=client_usage_export.LOCAL_TZ)
            con = sqlite3.connect(db_path)
            con.execute(
                """
                CREATE TABLE request_logs (
                    timestamp INTEGER,
                    account_id TEXT,
                    total_tokens INTEGER,
                    input_tokens INTEGER,
                    output_tokens INTEGER,
                    cached_tokens INTEGER
                )
                """
            )
            con.executemany(
                "INSERT INTO request_logs VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (client_usage_export.local_epoch_ms(now - timedelta(seconds=10)), "failed", 0, 0, 0, 0),
                    (client_usage_export.local_epoch_ms(now - timedelta(seconds=20)), "active", 100, 80, 20, 0),
                    (client_usage_export.local_epoch_ms(now - timedelta(minutes=10)), "old", 100, 80, 20, 0),
                ],
            )
            con.commit()
            con.close()

            self.assertEqual(
                client_usage_export.cockpit_recent_usage_account_ids(root, now),
                {"active"},
            )

    def test_retained_official_quota_does_not_clear_failed_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            cache_path = root / "official-quota-cache.json"
            account_id = "codex_retry"
            label = "Codex local - retry@example.com"
            checked_at = datetime(2026, 7, 14, 12, 0, tzinfo=client_usage_export.LOCAL_TZ)
            last_success_at = checked_at - timedelta(minutes=1)
            live_quota = client_usage_export.official_quota_from_usage_response(
                {
                    "plan_type": "plus",
                    "rate_limit": {
                        "primary_window": {
                            "limit_window_seconds": 7 * 24 * 60 * 60,
                            "reset_at": checked_at.timestamp() + 2 * 24 * 60 * 60,
                            "used_percent": 20,
                        },
                        "secondary_window": None,
                    },
                },
                last_success_at,
            )
            self.assertIsNotNone(live_quota)
            failed_quota = client_usage_export.stale_official_quota_snapshot(live_quota)
            cache_path.write_text(
                json.dumps(
                    {
                        "schema": 2,
                        "accounts": {
                            account_id: {
                                "label": label,
                                "email": "retry@example.com",
                                "checked_at": checked_at.timestamp(),
                                "last_success_at": last_success_at.timestamp(),
                                "refresh_failed": True,
                                "quota": failed_quota,
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            with patch.object(
                client_usage_export,
                "COCKPIT_OFFICIAL_QUOTA_CACHE_PATH",
                cache_path,
            ):
                client_usage_export.persist_quota_snapshots_by_account(
                    {account_id: {"email": "retry@example.com", "plan_type": "plus"}},
                    {account_id: label},
                    {label: live_quota},
                    now=checked_at + timedelta(seconds=1),
                )

            saved = json.loads(cache_path.read_text(encoding="utf-8"))["accounts"][account_id]
            self.assertTrue(saved["refresh_failed"])
            self.assertEqual(saved["last_success_at"], last_success_at.timestamp())

    def test_official_quota_proxy_parser_prefers_https_mapping(self) -> None:
        self.assertEqual(
            client_usage_export.normalize_http_proxy_url(
                "http=127.0.0.1:7890;https=127.0.0.1:7897"
            ),
            "http://127.0.0.1:7897",
        )
        self.assertEqual(
            client_usage_export.normalize_http_proxy_url("127.0.0.1:7897"),
            "http://127.0.0.1:7897",
        )

    def test_official_quota_uses_windows_proxy_when_process_has_none(self) -> None:
        with (
            patch.object(client_usage_export.request, "getproxies", return_value={}),
            patch.object(
                client_usage_export,
                "windows_user_proxy_url",
                return_value="http://127.0.0.1:7897",
            ) as windows_proxy,
        ):
            proxy_url = client_usage_export.official_quota_proxy_url({})

        self.assertEqual(proxy_url, "http://127.0.0.1:7897")
        windows_proxy.assert_called_once_with()

    def test_official_quota_request_uses_account_proxy(self) -> None:
        checked_at = datetime(2026, 7, 14, 12, 0, tzinfo=client_usage_export.LOCAL_TZ)
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(
            {
                "plan_type": "k12",
                "rate_limit": {
                    "primary_window": {
                        "limit_window_seconds": 5 * 60 * 60,
                        "reset_at": checked_at.timestamp() + 3600,
                        "used_percent": 20,
                    }
                },
            }
        ).encode("utf-8")
        opener = MagicMock()
        opener.open.return_value = response
        with (
            patch.object(client_usage_export.request, "ProxyHandler") as proxy_handler,
            patch.object(client_usage_export.request, "build_opener", return_value=opener),
        ):
            quota = client_usage_export.fetch_cockpit_official_quota(
                {
                    "access_token": "secret-access-token",
                    "account_id": "chatgpt-account-id",
                    "proxy_url": "http://127.0.0.1:7897",
                },
                "k12",
                checked_at,
            )

        self.assertIsNotNone(quota)
        proxy_handler.assert_called_once_with(
            {
                "http": "http://127.0.0.1:7897",
                "https": "http://127.0.0.1:7897",
            }
        )
        opener.open.assert_called_once()

    def test_official_quota_failure_retains_stale_percent_then_retries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            auth_dir = (
                root
                / ".antigravity_cockpit"
                / "codex_local_access_sidecar"
                / "auths"
            )
            auth_dir.mkdir(parents=True)
            account_id = "codex_stale"
            (auth_dir / f"{account_id}.json").write_text(
                json.dumps(
                    {
                        "access_token": "expired-access-token",
                        "account_id": "chatgpt-account-id",
                        "expired": datetime.now().timestamp() + 3600,
                    }
                ),
                encoding="utf-8",
            )
            cache_path = root / "official-quota-cache.json"
            checked_at = datetime(2026, 7, 14, 12, 0, tzinfo=client_usage_export.LOCAL_TZ)
            previous_checked_at = checked_at - timedelta(minutes=11)
            previous_quota = client_usage_export.official_quota_from_usage_response(
                {
                    "plan_type": "plus",
                    "rate_limit": {
                        "primary_window": {
                            "limit_window_seconds": 7 * 24 * 60 * 60,
                            "reset_at": checked_at.timestamp() + 2 * 24 * 60 * 60,
                            "used_percent": 74,
                        },
                        "secondary_window": None,
                    },
                },
                previous_checked_at,
            )
            cache_path.write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "accounts": {
                            account_id: {
                                "checked_at": previous_checked_at.timestamp(),
                                "quota": previous_quota,
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            accounts = {account_id: {"plan_type": "plus"}}
            recovered_quota = client_usage_export.official_quota_from_usage_response(
                {
                    "plan_type": "plus",
                    "rate_limit": {
                        "primary_window": {
                            "limit_window_seconds": 7 * 24 * 60 * 60,
                            "reset_at": checked_at.timestamp() + 2 * 24 * 60 * 60,
                            "used_percent": 60,
                        },
                        "secondary_window": None,
                    },
                },
                checked_at + timedelta(seconds=11),
            )
            with (
                patch.object(client_usage_export, "COCKPIT_OFFICIAL_QUOTA_CACHE_PATH", cache_path),
                patch.object(
                    client_usage_export,
                    "COCKPIT_OFFICIAL_QUOTA_FAILURE_RETRY_SECONDS",
                    10,
                ),
                patch.object(
                    client_usage_export,
                    "fetch_cockpit_official_quota",
                    side_effect=[None, recovered_quota],
                ) as fetch,
            ):
                failed = client_usage_export.cockpit_official_quota_by_account(
                    root,
                    accounts,
                    checked_at,
                )
                cached = client_usage_export.cockpit_official_quota_by_account(
                    root,
                    accounts,
                    checked_at + timedelta(seconds=9),
                )
                recovered = client_usage_export.cockpit_official_quota_by_account(
                    root,
                    accounts,
                    checked_at + timedelta(seconds=11),
                )

            self.assertEqual(fetch.call_count, 2)
            self.assertEqual(failed[account_id]["window_7d"]["remaining_percent"], 26.0)
            self.assertTrue(failed[account_id]["window_7d"]["quota_stale"])
            self.assertEqual(cached, failed)
            self.assertEqual(recovered[account_id]["window_7d"]["remaining_percent"], 40.0)
            self.assertFalse(recovered[account_id]["window_7d"]["quota_stale"])
            saved = json.loads(cache_path.read_text(encoding="utf-8"))["accounts"][account_id]
            self.assertNotIn("refresh_failed", saved)
            self.assertEqual(saved["quota"]["window_7d"]["remaining_percent"], 40.0)

    def test_official_quota_empty_success_retains_cached_7d_then_recovers_live(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            auth_dir = (
                root
                / ".antigravity_cockpit"
                / "codex_local_access_sidecar"
                / "auths"
            )
            auth_dir.mkdir(parents=True)
            account_id = "codex_empty_success"
            (auth_dir / f"{account_id}.json").write_text(
                json.dumps(
                    {
                        "access_token": "access-token",
                        "account_id": "chatgpt-account-id",
                        "expired": datetime.now().timestamp() + 3600,
                    }
                ),
                encoding="utf-8",
            )
            cache_path = root / "official-quota-cache.json"
            checked_at = datetime(2026, 7, 14, 12, 0, tzinfo=client_usage_export.LOCAL_TZ)
            previous_checked_at = checked_at - timedelta(minutes=11)
            previous_quota = client_usage_export.official_quota_from_usage_response(
                {
                    "plan_type": "plus",
                    "rate_limit": {
                        "primary_window": {
                            "limit_window_seconds": 7 * 24 * 60 * 60,
                            "reset_at": checked_at.timestamp() + 2 * 24 * 60 * 60,
                            "used_percent": 74,
                        },
                        "secondary_window": None,
                    },
                },
                previous_checked_at,
            )
            cache_path.write_text(
                json.dumps(
                    {
                        "schema": 2,
                        "accounts": {
                            account_id: {
                                "checked_at": previous_checked_at.timestamp(),
                                "last_success_at": previous_checked_at.timestamp(),
                                "quota": previous_quota,
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            empty_response = MagicMock()
            empty_response.__enter__.return_value.read.return_value = json.dumps(
                {"plan_type": "plus", "rate_limit": {}}
            ).encode("utf-8")
            live_payload = {
                "plan_type": "plus",
                "rate_limit": {
                    "primary_window": {
                        "limit_window_seconds": 7 * 24 * 60 * 60,
                        "reset_at": checked_at.timestamp() + 2 * 24 * 60 * 60,
                        "used_percent": 55,
                    },
                    "secondary_window": None,
                },
            }
            live_response = MagicMock()
            live_response.__enter__.return_value.read.return_value = json.dumps(
                live_payload
            ).encode("utf-8")
            accounts = {account_id: {"plan_type": "plus"}}
            with (
                patch.object(client_usage_export, "COCKPIT_OFFICIAL_QUOTA_CACHE_PATH", cache_path),
                patch.object(
                    client_usage_export,
                    "COCKPIT_OFFICIAL_QUOTA_FAILURE_RETRY_SECONDS",
                    10,
                ),
                patch.object(
                    client_usage_export.request,
                    "urlopen",
                    side_effect=[empty_response, live_response],
                ) as urlopen,
            ):
                empty = client_usage_export.cockpit_official_quota_by_account(
                    root,
                    accounts,
                    checked_at,
                )
                held = json.loads(cache_path.read_text(encoding="utf-8"))["accounts"][account_id]
                recovered = client_usage_export.cockpit_official_quota_by_account(
                    root,
                    accounts,
                    checked_at + timedelta(seconds=11),
                )

            self.assertEqual(urlopen.call_count, 2)
            self.assertEqual(empty[account_id]["window_7d"]["remaining_percent"], 26.0)
            self.assertTrue(empty[account_id]["window_7d"]["quota_stale"])
            self.assertTrue(empty[account_id]["window_5h"]["quota_unlimited"])
            self.assertEqual(held["quota"]["window_7d"]["remaining_percent"], 26.0)
            self.assertIsNotNone(held["quota"])
            self.assertTrue(held["refresh_failed"])
            self.assertEqual(held["last_success_at"], previous_checked_at.timestamp())
            self.assertEqual(recovered[account_id]["window_7d"]["remaining_percent"], 45.0)
            self.assertFalse(recovered[account_id]["window_7d"]["quota_stale"])
            saved = json.loads(cache_path.read_text(encoding="utf-8"))["accounts"][account_id]
            self.assertNotIn("refresh_failed", saved)
            self.assertEqual(saved["quota"]["window_7d"]["remaining_percent"], 45.0)

    def test_stale_official_quota_writer_does_not_overwrite_newer_live_7d(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            cache_path = root / "official-quota-cache.json"
            live_account = "codex_live"
            other_account = "codex_other"
            checked_at = datetime(2026, 7, 14, 12, 0, tzinfo=client_usage_export.LOCAL_TZ)
            live_success = (checked_at + timedelta(minutes=1)).timestamp()
            stale_success = (checked_at - timedelta(minutes=11)).timestamp()
            live_quota = {
                "window_5h": {
                    "quota_available": False,
                    "quota_stale": False,
                    "quota_unlimited": True,
                },
                "window_7d": {
                    "quota_available": True,
                    "quota_stale": False,
                    "remaining_percent": 45.0,
                    "utilization": 55.0,
                    "resets_at": (checked_at + timedelta(days=2)).isoformat(),
                },
                "window_cycle": {"quota_available": False, "quota_stale": False},
            }
            stale_quota = {
                "window_5h": {
                    "quota_available": False,
                    "quota_stale": True,
                    "quota_unlimited": True,
                },
                "window_7d": {
                    "quota_available": True,
                    "quota_stale": True,
                    "remaining_percent": 26.0,
                    "utilization": 74.0,
                    "resets_at": (checked_at + timedelta(days=2)).isoformat(),
                },
                "window_cycle": {"quota_available": False, "quota_stale": True},
            }
            other_quota = {
                "window_5h": {"quota_available": False, "quota_stale": False},
                "window_7d": {
                    "quota_available": True,
                    "quota_stale": False,
                    "remaining_percent": 81.0,
                    "utilization": 19.0,
                    "resets_at": (checked_at + timedelta(days=5)).isoformat(),
                },
                "window_cycle": {"quota_available": False, "quota_stale": False},
            }
            with patch.object(client_usage_export, "COCKPIT_OFFICIAL_QUOTA_CACHE_PATH", cache_path):
                client_usage_export.write_official_quota_cache(
                    {
                        live_account: {
                            "label": "Codex local - live@example.com",
                            "checked_at": live_success,
                            "last_success_at": live_success,
                            "quota": live_quota,
                        },
                        other_account: {
                            "label": "Codex local - other@example.com",
                            "checked_at": live_success,
                            "last_success_at": live_success,
                            "quota": other_quota,
                        },
                    }
                )
                client_usage_export.write_official_quota_cache(
                    {
                        live_account: {
                            "label": "Codex local - live@example.com",
                            "checked_at": checked_at.timestamp(),
                            "last_success_at": stale_success,
                            "refresh_failed": True,
                            "quota": stale_quota,
                        }
                    }
                )
                saved = json.loads(cache_path.read_text(encoding="utf-8"))["accounts"]

            self.assertEqual(saved[live_account]["quota"]["window_7d"]["remaining_percent"], 45.0)
            self.assertFalse(saved[live_account]["quota"]["window_7d"]["quota_stale"])
            self.assertNotIn("refresh_failed", saved[live_account])
            self.assertEqual(saved[live_account]["last_success_at"], live_success)
            self.assertEqual(saved[other_account]["quota"]["window_7d"]["remaining_percent"], 81.0)

    def test_newer_sidecar_writer_does_not_overwrite_older_live_official(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            cache_path = root / "official-quota-cache.json"
            account_id = "codex_official_keep"
            checked_at = datetime(2026, 7, 14, 12, 0, tzinfo=client_usage_export.LOCAL_TZ)
            official_success = checked_at.timestamp()
            sidecar_success = (checked_at + timedelta(minutes=8)).timestamp()
            live_official = {
                "window_5h": {
                    "quota_available": False,
                    "quota_stale": False,
                    "quota_unlimited": True,
                    "quota_source": "official-wham",
                    "quota_snapshot_at": checked_at.isoformat(timespec="seconds"),
                },
                "window_7d": {
                    "quota_available": True,
                    "quota_stale": False,
                    "remaining_percent": 45.0,
                    "utilization": 55.0,
                    "resets_at": (checked_at + timedelta(days=2)).isoformat(),
                    "quota_source": "official-wham",
                    "quota_snapshot_at": checked_at.isoformat(timespec="seconds"),
                },
                "window_cycle": {"quota_available": False, "quota_stale": False},
            }
            newer_sidecar = {
                "window_5h": {
                    "quota_available": False,
                    "quota_stale": False,
                    "quota_unlimited": True,
                    "quota_source": "sidecar-reserve",
                    "quota_snapshot_at": (checked_at + timedelta(minutes=8)).isoformat(
                        timespec="seconds"
                    ),
                },
                "window_7d": {
                    "quota_available": True,
                    "quota_stale": False,
                    "remaining_percent": 26.0,
                    "utilization": 74.0,
                    "resets_at": (checked_at + timedelta(days=2)).isoformat(),
                    "quota_source": "sidecar-reserve",
                    "quota_snapshot_at": (checked_at + timedelta(minutes=8)).isoformat(
                        timespec="seconds"
                    ),
                },
                "window_cycle": {"quota_available": False, "quota_stale": False},
            }
            with patch.object(client_usage_export, "COCKPIT_OFFICIAL_QUOTA_CACHE_PATH", cache_path):
                client_usage_export.write_official_quota_cache(
                    {
                        account_id: {
                            "label": "Codex local - official-keep@example.com",
                            "checked_at": official_success,
                            "last_success_at": official_success,
                            "quota": live_official,
                        }
                    }
                )
                client_usage_export.write_official_quota_cache(
                    {
                        account_id: {
                            "label": "Codex local - official-keep@example.com",
                            "checked_at": sidecar_success,
                            "last_success_at": sidecar_success,
                            "quota": newer_sidecar,
                        }
                    }
                )
                saved = json.loads(cache_path.read_text(encoding="utf-8"))["accounts"][account_id]

            self.assertEqual(saved["quota"]["window_7d"]["remaining_percent"], 45.0)
            self.assertEqual(saved["quota"]["window_7d"]["quota_source"], "official-wham")
            self.assertFalse(saved["quota"]["window_7d"]["quota_stale"])
            self.assertEqual(saved["last_success_at"], official_success)
            self.assertEqual(saved["checked_at"], official_success)

    def test_failed_entry_without_last_success_does_not_overwrite_live(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            cache_path = root / "official-quota-cache.json"
            account_id = "codex_live_keep"
            checked_at = datetime(2026, 7, 14, 12, 0, tzinfo=client_usage_export.LOCAL_TZ)
            live_success = checked_at.timestamp()
            failed_checked = (checked_at + timedelta(minutes=3)).timestamp()
            live_quota = {
                "window_5h": {
                    "quota_available": False,
                    "quota_stale": False,
                    "quota_unlimited": True,
                    "quota_source": "official-wham",
                    "quota_snapshot_at": checked_at.isoformat(timespec="seconds"),
                },
                "window_7d": {
                    "quota_available": True,
                    "quota_stale": False,
                    "remaining_percent": 45.0,
                    "utilization": 55.0,
                    "resets_at": (checked_at + timedelta(days=2)).isoformat(),
                    "quota_source": "official-wham",
                    "quota_snapshot_at": checked_at.isoformat(timespec="seconds"),
                },
                "window_cycle": {"quota_available": False, "quota_stale": False},
            }
            failed_quota = {
                "window_5h": {
                    "quota_available": False,
                    "quota_stale": True,
                    "quota_unlimited": True,
                    "quota_source": "official-wham",
                    "quota_snapshot_at": checked_at.isoformat(timespec="seconds"),
                },
                "window_7d": {
                    "quota_available": True,
                    "quota_stale": True,
                    "remaining_percent": 26.0,
                    "utilization": 74.0,
                    "resets_at": (checked_at + timedelta(days=2)).isoformat(),
                    "quota_source": "official-wham",
                    "quota_snapshot_at": checked_at.isoformat(timespec="seconds"),
                },
                "window_cycle": {"quota_available": False, "quota_stale": True},
            }
            with patch.object(client_usage_export, "COCKPIT_OFFICIAL_QUOTA_CACHE_PATH", cache_path):
                client_usage_export.write_official_quota_cache(
                    {
                        account_id: {
                            "label": "Codex local - live-keep@example.com",
                            "checked_at": live_success,
                            "last_success_at": live_success,
                            "quota": live_quota,
                        }
                    }
                )
                client_usage_export.write_official_quota_cache(
                    {
                        account_id: {
                            "label": "Codex local - live-keep@example.com",
                            "checked_at": failed_checked,
                            "refresh_failed": True,
                            "quota": failed_quota,
                        }
                    }
                )
                saved = json.loads(cache_path.read_text(encoding="utf-8"))["accounts"][account_id]

            self.assertEqual(saved["quota"]["window_7d"]["remaining_percent"], 45.0)
            self.assertFalse(saved["quota"]["window_7d"]["quota_stale"])
            self.assertNotIn("refresh_failed", saved)
            self.assertEqual(saved["last_success_at"], live_success)
            self.assertEqual(saved["checked_at"], live_success)

    def test_failed_sidecar_generation_does_not_stale_live_official(self) -> None:
        checked_at = datetime(2026, 7, 14, 12, 0, tzinfo=client_usage_export.LOCAL_TZ)
        live_quota = {
            "window_5h": {
                "quota_available": False,
                "quota_stale": False,
                "quota_unlimited": True,
                "quota_source": "official-wham",
            },
            "window_7d": {
                "quota_available": True,
                "quota_stale": False,
                "remaining_percent": 45.0,
                "utilization": 55.0,
                "resets_at": (checked_at + timedelta(days=2)).isoformat(),
                "quota_source": "official-wham",
                "quota_snapshot_at": checked_at.isoformat(timespec="seconds"),
            },
            "window_cycle": {"quota_available": False, "quota_stale": False},
        }
        failed_sidecar = {
            "window_5h": {
                "quota_available": False,
                "quota_stale": True,
                "quota_unlimited": True,
                "quota_source": "sidecar-reserve",
            },
            "window_7d": {
                "quota_available": True,
                "quota_stale": True,
                "remaining_percent": 26.0,
                "utilization": 74.0,
                "resets_at": (checked_at + timedelta(days=2)).isoformat(),
                "quota_source": "sidecar-reserve",
                "quota_snapshot_at": checked_at.isoformat(timespec="seconds"),
            },
            "window_cycle": {"quota_available": False, "quota_stale": True},
        }
        merged = client_usage_export.merge_official_quota_cache_entry(
            {
                "checked_at": checked_at.timestamp(),
                "last_success_at": checked_at.timestamp(),
                "quota": live_quota,
            },
            {
                "checked_at": (checked_at + timedelta(minutes=3)).timestamp(),
                "last_success_at": checked_at.timestamp(),
                "refresh_failed": True,
                "quota": failed_sidecar,
            },
        )
        self.assertIsNotNone(merged)
        self.assertEqual(merged["quota"]["window_7d"]["remaining_percent"], 45.0)
        self.assertFalse(merged["quota"]["window_7d"]["quota_stale"])
        self.assertEqual(merged["quota"]["window_7d"]["quota_source"], "official-wham")
        self.assertNotIn("refresh_failed", merged)

    def test_sidecar_quota_is_not_overwritten_by_empty_official_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            cockpit = root / ".antigravity_cockpit"
            accounts_dir = cockpit / "codex_accounts"
            sidecar = cockpit / "codex_local_access_sidecar"
            auth_dir = sidecar / "auths"
            accounts_dir.mkdir(parents=True)
            auth_dir.mkdir(parents=True)
            account_id = "codex_sidecar_keep"
            account = {
                "id": account_id,
                "email": "sidecar-keep@example.com",
                "plan_type": "k12",
            }
            (cockpit / "codex_accounts.json").write_text(
                json.dumps({"accounts": [account]}),
                encoding="utf-8",
            )
            (accounts_dir / f"{account_id}.json").write_text(
                json.dumps({"version": 1, "ciphertext": "encrypted"}),
                encoding="utf-8",
            )
            now = datetime.now(client_usage_export.LOCAL_TZ)
            (sidecar / "quota-reserve.json").write_text(
                json.dumps(
                    {
                        "accounts": {
                            account_id: {
                                "hourlyRemainingPercent": 64,
                                "hourlyWindowPresent": True,
                                "weeklyRemainingPercent": 41,
                                "weeklyWindowPresent": True,
                                "snapshotUpdatedAtUnixSeconds": int(now.timestamp()),
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            (auth_dir / f"{account_id}.json").write_text(
                json.dumps(
                    {
                        "access_token": "access-token",
                        "account_id": "chatgpt-account-id",
                        "expired": datetime.now().timestamp() + 3600,
                    }
                ),
                encoding="utf-8",
            )
            cache_path = root / "official-quota-cache.json"
            empty_response = MagicMock()
            empty_response.__enter__.return_value.read.return_value = json.dumps(
                {"plan_type": "k12", "rate_limit": {}}
            ).encode("utf-8")
            with (
                patch.object(client_usage_export, "COCKPIT_OFFICIAL_QUOTA_CACHE_PATH", cache_path),
                patch.object(client_usage_export.request, "urlopen", return_value=empty_response) as urlopen,
            ):
                quota = client_usage_export.cockpit_codex_quota_by_label(root)

            urlopen.assert_called()
            windows = quota["Codex local - sidecar-keep@example.com"]
            self.assertEqual(windows["window_5h"]["remaining_percent"], 64.0)
            self.assertEqual(windows["window_7d"]["remaining_percent"], 41.0)
            self.assertEqual(windows["window_5h"]["quota_source"], "sidecar-reserve")
            self.assertTrue(windows["window_5h"]["quota_reset_unavailable"])

    def test_missing_auth_retains_expired_cached_percent_as_stale(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            cache_path = root / "official-quota-cache.json"
            checked_at = datetime(2026, 7, 14, 12, 0, tzinfo=client_usage_export.LOCAL_TZ)
            previous_checked_at = checked_at - timedelta(minutes=11)
            previous_quota = {
                "window_5h": {"quota_available": False, "quota_unlimited": True},
                "window_7d": {
                    "quota_available": True,
                    "remaining_percent": 42.0,
                    "utilization": 58.0,
                    "resets_at": (checked_at + timedelta(days=2)).isoformat(),
                },
                "window_cycle": {"quota_available": False},
            }
            cache_path.write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "accounts": {
                            "codex_no_auth": {
                                "checked_at": previous_checked_at.timestamp(),
                                "quota": previous_quota,
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(
                client_usage_export,
                "COCKPIT_OFFICIAL_QUOTA_CACHE_PATH",
                cache_path,
            ):
                result = client_usage_export.cockpit_official_quota_by_account(
                    root,
                    {"codex_no_auth": {"plan_type": "plus"}},
                    checked_at,
                )

            quota = result["codex_no_auth"]
            self.assertEqual(quota["window_7d"]["remaining_percent"], 42.0)
            self.assertTrue(quota["window_7d"]["quota_stale"])
            self.assertTrue(quota["window_5h"]["quota_unlimited"])

    def test_official_quota_uses_codex_home_auth_when_sidecar_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            account_id = "codex_home_auth"
            email = "hyenas-ingot.1a@example.com"
            (root / ".codex").mkdir(parents=True)
            (root / ".codex" / "auth.json").write_text(
                json.dumps(
                    {
                        "tokens": {
                            "access_token": "home-access-token",
                            "id_token": CodexAuthIdentityTests.jwt(
                                {
                                    "email": email,
                                    "https://api.openai.com/auth": {
                                        "chatgpt_account_id": "chatgpt-account-id",
                                    },
                                }
                            ),
                            "account_id": "chatgpt-account-id",
                        }
                    }
                ),
                encoding="utf-8",
            )
            cache_path = root / "official-quota-cache.json"
            checked_at = datetime(2026, 8, 21, 12, 0, tzinfo=client_usage_export.LOCAL_TZ)
            previous_checked_at = checked_at - timedelta(minutes=11)
            previous_quota = {
                "window_5h": {"quota_available": False, "quota_unlimited": True},
                "window_7d": {
                    "quota_available": True,
                    "quota_stale": True,
                    "remaining_percent": 0.0,
                    "utilization": 100.0,
                    "resets_at": (checked_at - timedelta(days=1)).isoformat(),
                },
                "window_cycle": {"quota_available": False},
            }
            cache_path.write_text(
                json.dumps(
                    {
                        "schema": 2,
                        "accounts": {
                            account_id: {
                                "checked_at": previous_checked_at.timestamp(),
                                "quota": previous_quota,
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            recovered_quota = {
                "window_5h": {
                    "quota_available": False,
                    "quota_stale": False,
                    "quota_unlimited": True,
                },
                "window_7d": {
                    "quota_available": True,
                    "quota_stale": False,
                    "remaining_percent": 81.0,
                    "utilization": 19.0,
                    "resets_at": (checked_at + timedelta(days=6)).isoformat(),
                },
                "window_cycle": {"quota_available": False, "quota_stale": False},
            }
            accounts = {account_id: {"email": email, "plan_type": "plus"}}
            with (
                patch.object(client_usage_export, "COCKPIT_OFFICIAL_QUOTA_CACHE_PATH", cache_path),
                patch.object(
                    client_usage_export,
                    "fetch_cockpit_official_quota",
                    return_value=recovered_quota,
                ) as fetch,
            ):
                result = client_usage_export.cockpit_official_quota_by_account(
                    root,
                    accounts,
                    checked_at,
                )

            self.assertEqual(fetch.call_count, 1)
            auth = fetch.call_args.args[0]
            self.assertEqual(auth["access_token"], "home-access-token")
            self.assertEqual(auth["account_id"], "chatgpt-account-id")
            self.assertEqual(result[account_id]["window_7d"]["remaining_percent"], 81.0)
            self.assertFalse(result[account_id]["window_7d"]["quota_stale"])

    def test_official_quota_skips_cockpit_auth_metadata_without_token(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            account_id = "codex_c272c75c9bc64b31011c82626e40d006"
            email = "hyenas-ingot.1a@example.com"
            (root / ".codex").mkdir(parents=True)
            (root / ".codex" / ".cockpit_codex_auth.json").write_text(
                json.dumps(
                    {
                        "account_id": account_id,
                        "email": email,
                        "writer": "cockpit",
                        "version": 1,
                    }
                ),
                encoding="utf-8",
            )
            (root / ".codex" / "auth.json").write_text(
                json.dumps(
                    {
                        "tokens": {
                            "access_token": "home-access-token",
                            "id_token": CodexAuthIdentityTests.jwt(
                                {
                                    "email": email,
                                    "https://api.openai.com/auth": {
                                        "chatgpt_account_id": "chatgpt-account-id",
                                    },
                                }
                            ),
                            "account_id": "chatgpt-account-id",
                        }
                    }
                ),
                encoding="utf-8",
            )
            cache_path = root / "official-quota-cache.json"
            checked_at = datetime(2026, 8, 21, 12, 0, tzinfo=client_usage_export.LOCAL_TZ)
            previous_checked_at = checked_at - timedelta(minutes=11)
            cache_path.write_text(
                json.dumps(
                    {
                        "schema": 2,
                        "accounts": {
                            account_id: {
                                "email": email,
                                "checked_at": previous_checked_at.timestamp(),
                                "refresh_failed": True,
                                "quota": {
                                    "window_5h": {"quota_available": False, "quota_unlimited": True},
                                    "window_7d": {
                                        "quota_available": True,
                                        "quota_stale": True,
                                        "remaining_percent": 0.0,
                                        "utilization": 100.0,
                                    },
                                    "window_cycle": {"quota_available": False},
                                },
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            recovered_quota = {
                "window_5h": {
                    "quota_available": False,
                    "quota_stale": False,
                    "quota_unlimited": True,
                },
                "window_7d": {
                    "quota_available": True,
                    "quota_stale": False,
                    "remaining_percent": 33.0,
                    "utilization": 67.0,
                    "resets_at": (checked_at + timedelta(days=6)).isoformat(),
                },
                "window_cycle": {"quota_available": False, "quota_stale": False},
            }
            with (
                patch.object(client_usage_export, "COCKPIT_OFFICIAL_QUOTA_CACHE_PATH", cache_path),
                patch.object(
                    client_usage_export,
                    "fetch_cockpit_official_quota",
                    return_value=recovered_quota,
                ) as fetch,
            ):
                result = client_usage_export.cockpit_official_quota_by_account(
                    root,
                    {account_id: {"email": email, "plan_type": "plus"}},
                    checked_at,
                )

            self.assertEqual(fetch.call_count, 1)
            auth = fetch.call_args.args[0]
            self.assertEqual(auth["access_token"], "home-access-token")
            self.assertEqual(auth["account_id"], "chatgpt-account-id")
            self.assertEqual(result[account_id]["window_7d"]["remaining_percent"], 33.0)
            self.assertFalse(result[account_id]["window_7d"]["quota_stale"])

    def test_removed_account_keeps_last_quota_by_label(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            cache_path = root / "official-quota-cache.json"
            checked_at = datetime(2026, 7, 14, 12, 0, tzinfo=client_usage_export.LOCAL_TZ)
            removed_id = "codex_removed"
            active_id = "codex_active"
            removed_label = "Codex local - removed@example.com"
            quota = {
                "window_5h": {"quota_available": False},
                "window_7d": {
                    "quota_available": True,
                    "quota_stale": False,
                    "remaining_percent": 37.0,
                    "utilization": 63.0,
                    "resets_at": (checked_at + timedelta(days=2)).isoformat(),
                },
                "window_cycle": {"quota_available": False},
            }
            cache_path.write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "accounts": {
                            removed_id: {
                                "label": removed_label,
                                "checked_at": (checked_at - timedelta(days=1)).timestamp(),
                                "quota": quota,
                            },
                            active_id: {
                                "checked_at": checked_at.timestamp(),
                                "quota": quota,
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(
                client_usage_export,
                "COCKPIT_OFFICIAL_QUOTA_CACHE_PATH",
                cache_path,
            ):
                client_usage_export.cockpit_official_quota_by_account(
                    root,
                    {active_id: {"email": "active@example.com", "plan_type": "k12"}},
                    checked_at + timedelta(minutes=1),
                )
                retained = client_usage_export.cockpit_codex_quota_by_label(root)

            saved = json.loads(cache_path.read_text(encoding="utf-8"))["accounts"]
            self.assertIn(removed_id, saved)
            self.assertEqual(saved[active_id]["label"], "Codex local - active@example.com")
            self.assertEqual(
                retained[removed_label]["window_7d"]["remaining_percent"],
                37.0,
            )
            self.assertTrue(retained[removed_label]["window_7d"]["quota_stale"])

    def test_sidecar_quota_is_persisted_before_account_removal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            cache_path = root / "official-quota-cache.json"
            label = self.write_quota_account(
                root,
                plan_type="k12",
                hourly_minutes=300,
                weekly_present=True,
            )
            with patch.object(
                client_usage_export,
                "COCKPIT_OFFICIAL_QUOTA_CACHE_PATH",
                cache_path,
            ):
                fresh = client_usage_export.cockpit_codex_quota_by_label(root)
                (root / ".antigravity_cockpit" / "codex_accounts" / "account.json").unlink()
                retained = client_usage_export.cockpit_codex_quota_by_label(root)

            self.assertEqual(fresh[label]["window_7d"]["remaining_percent"], 70.0)
            self.assertEqual(retained[label]["window_7d"]["remaining_percent"], 70.0)
            self.assertTrue(retained[label]["window_7d"]["quota_stale"])

    def test_plus_primary_7d_maps_to_official_7d_and_unlimited_5h(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            label = self.write_quota_account(
                root,
                plan_type="plus",
                hourly_minutes=7 * 24 * 60,
            )
            cache_path = root / "official-quota-cache.json"
            with patch.object(
                client_usage_export,
                "COCKPIT_OFFICIAL_QUOTA_CACHE_PATH",
                cache_path,
            ):
                quota = client_usage_export.cockpit_codex_quota_by_label(root)[label]

        self.assertTrue(quota["window_5h"]["quota_unlimited"])
        self.assertFalse(quota["window_5h"]["quota_available"])
        self.assertTrue(quota["window_7d"]["quota_available"])
        self.assertEqual(quota["window_7d"]["window_minutes"], 7 * 24 * 60)
        self.assertEqual(quota["window_7d"]["remaining_percent"], 80.0)
        self.assertFalse(quota["window_cycle"]["quota_available"])

    def test_plus_restored_5h_recovers_both_official_windows(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            label = self.write_quota_account(
                root,
                plan_type="plus",
                hourly_minutes=300,
                weekly_present=True,
            )
            cache_path = root / "official-quota-cache.json"
            with patch.object(
                client_usage_export,
                "COCKPIT_OFFICIAL_QUOTA_CACHE_PATH",
                cache_path,
            ):
                quota = client_usage_export.cockpit_codex_quota_by_label(root)[label]

        self.assertTrue(quota["window_5h"]["quota_available"])
        self.assertNotIn("quota_unlimited", quota["window_5h"])
        self.assertEqual(quota["window_5h"]["window_minutes"], 300)
        self.assertTrue(quota["window_7d"]["quota_available"])

    def test_k12_primary_5h_and_weekly_7d_stay_separate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            label = self.write_quota_account(
                root,
                plan_type="k12",
                hourly_minutes=300,
                weekly_present=True,
            )
            cache_path = root / "official-quota-cache.json"
            with patch.object(
                client_usage_export,
                "COCKPIT_OFFICIAL_QUOTA_CACHE_PATH",
                cache_path,
            ):
                quota = client_usage_export.cockpit_codex_quota_by_label(root)[label]

        self.assertEqual(quota["window_5h"]["remaining_percent"], 80.0)
        self.assertEqual(quota["window_7d"]["remaining_percent"], 70.0)
        self.assertFalse(quota["window_cycle"]["quota_available"])

    def test_official_quota_without_reset_is_unavailable_and_stale(self) -> None:
        window = client_usage_export.quota_window_payload(
            50,
            None,
            False,
            7 * 24 * 60,
        )

        self.assertFalse(window["quota_available"])
        self.assertTrue(window["quota_stale"])
        self.assertEqual(window["resets_at"], "")

    def test_quota_scanner_filters_events_at_exact_cycle_start(self) -> None:
        now = datetime(2026, 7, 13, 12, 0, 0)
        reset_at = datetime(2026, 7, 14, 12, 0, 0)
        cycle_start = reset_at - timedelta(days=7)
        label = "Codex local - plus@example.com"
        quota = {
            label: {
                "window_5h": {"quota_available": False, "quota_unlimited": True},
                "window_7d": {
                    "quota_available": True,
                    "quota_stale": False,
                    "resets_at": reset_at.replace(
                        tzinfo=client_usage_export.LOCAL_TZ
                    ).isoformat(timespec="seconds"),
                    "window_minutes": 7 * 24 * 60,
                },
                "window_cycle": {"quota_available": False},
            }
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.write_request_log(
                root,
                "plus@example.com",
                [(cycle_start - timedelta(seconds=5), 100), (cycle_start, 200)],
            )

            _, buckets_7d, _, _, starts_7d, _, _ = (
                client_usage_export.scan_cockpit_codex_quota_windows(
                    root,
                    quota,
                    now,
                    now + timedelta(seconds=1),
                )
            )

        self.assertEqual(starts_7d[label], cycle_start)
        self.assertEqual(buckets_7d[label].total_tokens, 200)

    def test_manual_reset_moves_boundary_and_excludes_old_cycle(self) -> None:
        now = datetime(2026, 7, 13, 12, 0, 0)
        reset_at = datetime(2026, 7, 20, 11, 0, 0)
        cycle_start = reset_at - timedelta(days=7)
        label = "Codex local - plus@example.com"
        quota = {
            label: {
                "window_7d": {
                    "quota_available": True,
                    "quota_stale": False,
                    "resets_at": reset_at.replace(
                        tzinfo=client_usage_export.LOCAL_TZ
                    ).isoformat(timespec="seconds"),
                    "window_minutes": 7 * 24 * 60,
                }
            }
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.write_request_log(
                root,
                "plus@example.com",
                [(cycle_start - timedelta(hours=1), 900), (cycle_start + timedelta(minutes=1), 100)],
            )

            _, buckets_7d, _, _, starts_7d, _, _ = (
                client_usage_export.scan_cockpit_codex_quota_windows(
                    root,
                    quota,
                    now,
                    now + timedelta(seconds=1),
                )
            )

        self.assertEqual(starts_7d[label], cycle_start)
        self.assertEqual(buckets_7d[label].total_tokens, 100)

    def test_window_dict_preserves_model_and_token_breakdown(self) -> None:
        bucket = client_usage_export.UsageBucket(
            requests=2,
            input_tokens=1_000,
            cached_input_tokens=2_000,
            output_tokens=300,
            cost=1.25,
            models={"gpt-5.6-sol": 3_300},
        )

        window = client_usage_export.bucket_to_window_dict(
            bucket,
            datetime(2026, 7, 1, 0, 0, 0),
            datetime(2026, 7, 8, 0, 0, 0),
        )

        self.assertEqual(window["models"], {"gpt-5.6-sol": 3_300})
        self.assertEqual(window["input_tokens"], 1_000)
        self.assertEqual(window["cached_input_tokens"], 2_000)
        self.assertEqual(window["output_tokens"], 300)

    def test_more_complete_raw_window_replaces_partial_direct_database_bucket(self) -> None:
        label = "Codex local - account@example.com"
        partial_direct = client_usage_export.UsageBucket(requests=80, input_tokens=14_000_000, cost=11.0)
        complete_raw = client_usage_export.UsageBucket(requests=732, input_tokens=108_000_000, cost=90.0)

        merged = client_usage_export.prefer_more_complete_usage_buckets(
            {label: partial_direct},
            {label: complete_raw},
        )

        self.assertIs(merged[label], complete_raw)

    def test_partial_raw_window_does_not_replace_complete_direct_database_bucket(self) -> None:
        label = "Codex local - account@example.com"
        complete_direct = client_usage_export.UsageBucket(requests=100, input_tokens=20_000_000, cost=18.0)
        partial_raw = client_usage_export.UsageBucket(requests=20, input_tokens=4_000_000, cost=3.0)

        merged = client_usage_export.prefer_more_complete_usage_buckets(
            {label: complete_direct},
            {label: partial_raw},
        )

        self.assertIs(merged[label], complete_direct)

    def test_local_window_is_authoritative_even_when_cockpit_total_is_larger(self) -> None:
        label = "Codex local - account@example.com"
        cockpit = client_usage_export.UsageBucket(requests=900, input_tokens=90_000_000, cost=90.0)
        local = client_usage_export.UsageBucket(requests=1_023, input_tokens=80_000_000, cost=80.0)

        selected = client_usage_export.prefer_local_usage_buckets(
            {label: cockpit},
            {label: local},
            local_window_covered=True,
        )

        self.assertEqual(selected, {label: local})

    def test_uncovered_local_window_keeps_cockpit_fallback(self) -> None:
        label = "Codex local - account@example.com"
        cockpit = client_usage_export.UsageBucket(requests=80, input_tokens=8_000_000, cost=8.0)

        selected = client_usage_export.prefer_local_usage_buckets(
            {label: cockpit},
            {},
            local_window_covered=False,
        )

        self.assertEqual(selected, {label: cockpit})

    def test_covered_empty_local_window_does_not_mix_in_cockpit_accounts(self) -> None:
        cockpit = {
            "Codex local - stale@example.com": client_usage_export.UsageBucket(
                requests=80,
                input_tokens=8_000_000,
            )
        }

        selected = client_usage_export.prefer_local_usage_buckets(
            cockpit,
            {},
            local_window_covered=True,
        )

        self.assertEqual(selected, {})

    def test_local_window_coverage_requires_canonical_codex_sessions_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = root / "home"
            canonical_sessions = home / ".codex" / "sessions"
            diagnostics = root / "diagnostics"
            canonical_sessions.mkdir(parents=True)
            diagnostics.mkdir()
            start = datetime(2026, 8, 21, 12, 0, 0)

            with patch.object(
                client_usage_export,
                "iter_recent_jsonl",
                return_value=[diagnostics / "unrelated.jsonl"],
            ):
                self.assertFalse(
                    client_usage_export.local_codex_window_source_available(
                        home,
                        diagnostics,
                        start,
                        start + timedelta(hours=1),
                    )
                )
                self.assertTrue(
                    client_usage_export.local_codex_window_source_available(
                        home,
                        canonical_sessions,
                        start,
                        start + timedelta(hours=1),
                    )
                )

    def test_local_window_coverage_ignores_unreadable_logs2_database(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = root / "home"
            canonical_sessions = home / ".codex" / "sessions"
            canonical_sessions.mkdir(parents=True)
            logs2 = home / ".codex" / "logs_2.sqlite"
            logs2.write_bytes(b"not a sqlite database")
            start = datetime(2026, 8, 21, 12, 0, 0)

            with patch.object(client_usage_export, "iter_recent_jsonl", return_value=[]):
                self.assertFalse(
                    client_usage_export.local_codex_window_source_available(
                        home,
                        canonical_sessions,
                        start,
                        start + timedelta(hours=1),
                    )
                )

    def test_30d_window_uses_rolling_account_usage(self) -> None:
        now = datetime(2026, 6, 23, 12, 0, 0)
        label = "Codex local - account@example.com"
        rolling_30d = client_usage_export.UsageBucket(
            requests=120,
            input_tokens=88_000_000,
            cost=84.0,
        )
        rolling_7d = client_usage_export.UsageBucket(
            requests=30,
            input_tokens=20_000_000,
            cost=19.0,
        )

        def scan_accounts(_home: Path, start: datetime, _end: datetime):
            return {label: rolling_30d if now - start > timedelta(days=20) else rolling_7d}

        aligned = ({}, {}, {}, {}, {}, {}, {})
        with (
            patch.object(client_usage_export, "cockpit_codex_quota_by_label", return_value={}),
            patch.object(client_usage_export, "cockpit_codex_speed_by_label", return_value={}),
            patch.object(client_usage_export, "scan_cockpit_codex_accounts", side_effect=scan_accounts),
            patch.object(client_usage_export, "scan_cockpit_codex_quota_windows", return_value=aligned),
            patch.object(client_usage_export, "all_cockpit_codex_account_labels", return_value=[label]),
        ):
            result = client_usage_export.build_codex_window_stats(
                Path("."),
                Path("."),
                now,
                {},
                label,
                include_30d=True,
            )

        window_30d = result[label]["window_30d"]
        self.assertEqual(window_30d["requests"], 120)
        self.assertEqual(window_30d["tokens"], 88_000_000)
        self.assertEqual(window_30d["cost"], 84.0)
        self.assertTrue(window_30d["start_at"].startswith("2026-05-24T12:00:00"))

    def test_full_unused_5h_quota_waits_for_first_request(self) -> None:
        now = datetime(2026, 6, 23, 12, 0, 0)
        window = {
            "requests": 0,
            "tokens": 0,
            "cost": 0.0,
            "quota_available": True,
            "quota_stale": False,
            "remaining_percent": 99.0,
            "utilization": 1.0,
            "resets_at": "2026-06-23T17:00:00+08:00",
        }

        client_usage_export.apply_5h_countdown_state(window, now)

        self.assertTrue(window["quota_idle"])
        self.assertFalse(window["countdown_active"])

        window["requests"] = 1
        window["tokens"] = 100
        client_usage_export.apply_5h_countdown_state(window, now)

        self.assertFalse(window["quota_idle"])
        self.assertTrue(window["countdown_active"])

    def test_quota_window_start_uses_exact_official_boundary(self) -> None:
        now = datetime(2026, 6, 29, 11, 0, 0)
        window = {
            "quota_available": True,
            "quota_stale": False,
            "resets_at": "2026-06-29T14:22:53+08:00",
        }

        start = client_usage_export.quota_window_start(window, now, timedelta(hours=5))

        self.assertIsNotNone(start)
        assert start is not None
        self.assertEqual(start, datetime(2026, 6, 29, 9, 22, 53))

    def test_stale_quota_window_keeps_last_known_official_boundary(self) -> None:
        now = datetime(2026, 7, 17, 10, 45, 0)
        window = {
            "quota_available": True,
            "quota_stale": True,
            "resets_at": "2026-07-17T13:33:07+08:00",
            "window_minutes": 5 * 60,
        }

        start = client_usage_export.quota_window_start(window, now, timedelta(hours=5))

        self.assertEqual(start, datetime(2026, 7, 17, 8, 33, 7))

    def test_stale_quota_window_keeps_request_count(self) -> None:
        now = datetime(2026, 7, 17, 10, 45, 0)
        reset_at = datetime(2026, 7, 17, 13, 33, 7)
        cycle_start = reset_at - timedelta(hours=5)
        label = "Codex local - account@example.com"
        quota = {
            label: {
                "window_5h": {
                    "quota_available": True,
                    "quota_stale": True,
                    "resets_at": reset_at.replace(
                        tzinfo=client_usage_export.LOCAL_TZ
                    ).isoformat(timespec="seconds"),
                    "window_minutes": 5 * 60,
                },
                "window_7d": {"quota_available": False},
                "window_cycle": {"quota_available": False},
            }
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.write_request_log(
                root,
                "account@example.com",
                [
                    (cycle_start + timedelta(minutes=1), 200),
                    (cycle_start + timedelta(minutes=2), 300),
                ],
            )

            buckets_5h, _, _, starts_5h, _, _, _ = (
                client_usage_export.scan_cockpit_codex_quota_windows(
                    root,
                    quota,
                    now,
                    now + timedelta(seconds=1),
                )
            )

        self.assertEqual(starts_5h[label], cycle_start)
        self.assertEqual(buckets_5h[label].requests, 2)
        self.assertEqual(buckets_5h[label].total_tokens, 500)

    def test_full_unused_7d_quota_keeps_official_countdown(self) -> None:
        now = datetime(2026, 7, 13, 12, 0, 0)
        window = {
            "requests": 0,
            "tokens": 0,
            "cost": 0.0,
            "quota_available": True,
            "quota_stale": False,
            "remaining_percent": 99.0,
            "utilization": 1.0,
            "resets_at": "2026-07-19T12:00:00+08:00",
            "window_minutes": 7 * 24 * 60,
        }

        client_usage_export.apply_quota_countdown_state(
            window,
            now,
            idle_until_first_use=False,
        )

        self.assertEqual(window["remaining_percent"], 100.0)
        self.assertEqual(window["utilization"], 0.0)
        self.assertFalse(window["quota_idle"])
        self.assertTrue(window["countdown_active"])
        self.assertEqual(window["resets_at"], "2026-07-19T12:00:00+08:00")

    def test_expired_quota_reset_becomes_new_window_boundary(self) -> None:
        now = datetime(2026, 7, 12, 19, 18, 0)
        reset_at = datetime(2026, 7, 12, 19, 16, 47)
        window = {
            "quota_available": True,
            "quota_stale": False,
            "resets_at": "2026-07-12T19:16:47+08:00",
        }

        start = client_usage_export.quota_window_start(window, now, timedelta(hours=5))

        self.assertEqual(start, reset_at)

    def test_expired_unused_5h_window_becomes_idle_and_full(self) -> None:
        now = datetime(2026, 7, 12, 19, 18, 0)
        window = {
            "requests": 0,
            "tokens": 0,
            "cost": 0.0,
            "quota_available": True,
            "quota_stale": False,
            "remaining_percent": 0.0,
            "utilization": 100.0,
            "resets_at": "2026-07-12T19:16:47+08:00",
        }

        client_usage_export.apply_5h_countdown_state(window, now)

        self.assertTrue(window["quota_snapshot_expired"])
        self.assertTrue(window["quota_idle"])
        self.assertFalse(window["countdown_active"])
        self.assertEqual(window["remaining_percent"], 100.0)
        self.assertEqual(window["utilization"], 0.0)

    def test_expired_used_5h_window_hides_old_quota_percentage(self) -> None:
        now = datetime(2026, 7, 12, 19, 18, 0)
        window = {
            "requests": 2,
            "tokens": 200_000,
            "cost": 0.2,
            "quota_available": True,
            "quota_stale": False,
            "remaining_percent": 0.0,
            "utilization": 100.0,
            "resets_at": "2026-07-12T19:16:47+08:00",
        }

        client_usage_export.apply_5h_countdown_state(window, now)

        self.assertTrue(window["quota_snapshot_expired"])
        self.assertTrue(window["quota_stale"])
        self.assertFalse(window["quota_idle"])
        self.assertTrue(window["countdown_active"])
        self.assertIsNone(window["remaining_percent"])
        self.assertIsNone(window["utilization"])

    def test_failed_stale_snapshot_keeps_last_percentage_after_reset(self) -> None:
        now = datetime(2026, 7, 12, 19, 18, 0)
        window = {
            "quota_available": True,
            "quota_stale": True,
            "remaining_percent": 26.0,
            "utilization": 74.0,
            "resets_at": "2026-07-12T18:00:00+08:00",
            "window_minutes": 7 * 24 * 60,
            "requests": 1,
            "tokens": 100,
            "cost": 0.01,
        }

        client_usage_export.apply_quota_countdown_state(
            window,
            now,
            idle_until_first_use=False,
        )

        self.assertEqual(window["remaining_percent"], 26.0)
        self.assertEqual(window["utilization"], 74.0)
        self.assertFalse(window["quota_idle"])
        self.assertFalse(window["countdown_active"])

    def test_quota_windows_use_quota_cycle_boundaries(self) -> None:
        now = datetime(2026, 6, 22, 12, 0, 0)
        label = "Codex local - account@example.com"
        rolling_5h = client_usage_export.UsageBucket(
            requests=8,
            input_tokens=5_000_000,
            cost=5.0,
        )
        rolling_7d = client_usage_export.UsageBucket(
            requests=70,
            input_tokens=66_000_000,
            cost=62.0,
        )
        quota_cycle_5h = client_usage_export.UsageBucket(
            requests=4,
            input_tokens=2_000_000,
            cost=2.0,
        )
        quota_cycle_7d = client_usage_export.UsageBucket(
            requests=40,
            input_tokens=40_000_000,
            cost=38.0,
        )
        quota = {
            label: {
                "window_5h": {
                    "quota_available": True,
                    "remaining_percent": 10.0,
                    "utilization": 90.0,
                    "resets_at": "2026-06-22T14:00:00+08:00",
                },
                "window_7d": {
                    "quota_available": True,
                    "remaining_percent": 17.0,
                    "utilization": 83.0,
                    "resets_at": "2026-06-25T15:00:00+08:00",
                },
            }
        }

        def scan_accounts(_home: Path, start: datetime, _end: datetime):
            return {label: rolling_7d if now - start > timedelta(days=1) else rolling_5h}

        aligned = (
            {label: quota_cycle_5h},
            {label: quota_cycle_7d},
            {},
            {label: now - timedelta(hours=2)},
            {label: datetime(2026, 6, 18, 15, 0, 0)},
            {},
            {},
        )
        with (
            patch.object(client_usage_export, "cockpit_codex_quota_by_label", return_value=quota),
            patch.object(client_usage_export, "cockpit_codex_speed_by_label", return_value={}),
            patch.object(client_usage_export, "scan_cockpit_codex_accounts", side_effect=scan_accounts),
            patch.object(client_usage_export, "scan_cockpit_codex_quota_windows", return_value=aligned),
            patch.object(client_usage_export, "all_cockpit_codex_account_labels", return_value=[label]),
        ):
            result = client_usage_export.build_codex_window_stats(
                Path("."),
                Path("."),
                now,
                {},
                label,
            )

        window_5h = result[label]["window_5h"]
        window_7d = result[label]["window_7d"]
        self.assertEqual(window_5h["tokens"], 2_000_000)
        self.assertEqual(window_5h["utilization"], 90.0)
        self.assertTrue(window_5h["start_at"].startswith("2026-06-22T10:00:00"))
        self.assertEqual(window_7d["tokens"], 40_000_000)
        self.assertEqual(window_7d["utilization"], 83.0)
        self.assertTrue(window_7d["start_at"].startswith("2026-06-18T15:00:00"))

    def test_7d_without_quota_remains_rolling(self) -> None:
        now = datetime(2026, 6, 22, 12, 0, 0)
        label = "Codex local - account@example.com"
        rolling_7d = client_usage_export.UsageBucket(
            requests=70,
            input_tokens=66_000_000,
            cost=62.0,
        )

        def scan_accounts(_home: Path, start: datetime, _end: datetime):
            return {label: rolling_7d}

        aligned = ({}, {}, {}, {}, {}, {}, {})
        with (
            patch.object(client_usage_export, "cockpit_codex_quota_by_label", return_value={}),
            patch.object(client_usage_export, "cockpit_codex_speed_by_label", return_value={}),
            patch.object(client_usage_export, "scan_cockpit_codex_accounts", side_effect=scan_accounts),
            patch.object(client_usage_export, "scan_cockpit_codex_quota_windows", return_value=aligned),
            patch.object(client_usage_export, "all_cockpit_codex_account_labels", return_value=[label]),
        ):
            result = client_usage_export.build_codex_window_stats(
                Path("."),
                Path("."),
                now,
                {},
                label,
            )

        window_7d = result[label]["window_7d"]
        self.assertEqual(window_7d["tokens"], 66_000_000)
        self.assertTrue(window_7d["start_at"].startswith("2026-06-15T12:00:00"))

    def test_unavailable_official_7d_keeps_tokens_only_in_rolling_window(self) -> None:
        now = datetime(2026, 6, 22, 12, 0, 0)
        label = "Codex local - plus@example.com"
        rolling_7d = client_usage_export.UsageBucket(
            requests=70,
            input_tokens=66_000_000,
            cost=62.0,
        )
        quota = {
            label: {
                "window_5h": {"quota_available": False, "quota_unlimited": True},
                "window_7d": {
                    "quota_available": False,
                    "quota_stale": True,
                    "resets_at": "",
                    "window_minutes": 7 * 24 * 60,
                },
            }
        }

        def scan_accounts(_home: Path, _start: datetime, _end: datetime):
            return {label: rolling_7d}

        aligned = ({}, {}, {}, {}, {}, {}, {})
        with (
            patch.object(client_usage_export, "cockpit_codex_quota_by_label", return_value=quota),
            patch.object(client_usage_export, "cockpit_codex_speed_by_label", return_value={}),
            patch.object(client_usage_export, "scan_cockpit_codex_accounts", side_effect=scan_accounts),
            patch.object(client_usage_export, "scan_cockpit_codex_quota_windows", return_value=aligned),
            patch.object(client_usage_export, "all_cockpit_codex_account_labels", return_value=[label]),
        ):
            result = client_usage_export.build_codex_window_stats(
                Path("."),
                Path("."),
                now,
                {},
                label,
            )

        self.assertEqual(result[label]["window_7d"]["tokens"], 0)
        self.assertTrue(result[label]["window_7d"]["quota_stale"])
        self.assertEqual(result[label]["window_rolling_7d"]["tokens"], 66_000_000)


class QuotaFingerprintAttributionTests(unittest.TestCase):
    def test_near_time_marker_does_not_override_unique_quota_fingerprint(self) -> None:
        turn_started_at = datetime(2026, 7, 28, 10, 0, 0)
        quota_label = "Codex local - quota-owner@example.com"
        event = client_usage_export.UsageEvent(
            when=turn_started_at + timedelta(seconds=30),
            model="gpt-test",
            input_tokens=100_000,
            cached_tokens=80_000,
            output_tokens=1_000,
            session_id="quota-session",
            account_at=turn_started_at,
            account_label_hint=quota_label,
            account_hint_source="quota_fingerprint",
        )
        nearby_marker = client_usage_export.AccountMarker(
            when=event.when,
            label="Codex local - concurrent@example.com",
            total_tokens=7_777,
        )

        resolved, session_accounts, unresolved = (
            client_usage_export.resolve_api_service_event_accounts(
                {quota_label: [event]},
                [nearby_marker],
            )
        )

        self.assertEqual(resolved, {quota_label: [event]})
        self.assertEqual(session_accounts["quota-session"], quota_label)
        self.assertEqual(unresolved, 0)

    def test_confirmed_auth_result_overrides_stale_quota_fingerprint(self) -> None:
        turn_started_at = datetime(2026, 8, 2, 21, 18, 0)
        stale_label = "Codex local - stale-quota@example.com"
        api_label = "Codex local - api-key-8311cc24"
        event = client_usage_export.UsageEvent(
            when=turn_started_at + timedelta(seconds=16),
            model="gpt-test",
            input_tokens=20_000,
            cached_tokens=200_000,
            output_tokens=1_000,
            session_id="quota-auth-result-session",
            account_at=turn_started_at,
            account_label_hint=stale_label,
            account_hint_source="quota_fingerprint",
        )
        affinity_events = [
            client_usage_export.CockpitAffinityEvent(
                when=event.when - timedelta(milliseconds=100),
                request_id="api-request",
                source="auth_result",
                account_id="api-id",
                label=api_label,
                action="auth result",
                confirmed=True,
            ),
            client_usage_export.CockpitAffinityEvent(
                when=event.when + timedelta(milliseconds=50),
                request_id="api-request",
                account_id="api-id",
                label=api_label,
                action="cache hit",
            ),
        ]
        event_id = client_usage_export.codex_event_id(event)
        verdicts = {
            event_id: {
                "label": stale_label,
                "tier": "cockpit_usage_row",
                "at": "2026-08-02T21:18:16+08:00",
            },
            "unrelated-event": {
                "label": "Codex local - unrelated@example.com",
                "tier": "cockpit_usage_row",
                "at": "2026-08-02T20:00:00+08:00",
            },
        }

        resolved, session_accounts, unresolved = (
            client_usage_export.resolve_api_service_event_accounts(
                {stale_label: [event]},
                [],
                affinity_events=affinity_events,
                verdicts=verdicts,
            )
        )

        self.assertEqual(resolved[api_label], [event])
        self.assertEqual(session_accounts[event.session_id], api_label)
        self.assertEqual(unresolved, 0)
        self.assertEqual(verdicts[event_id]["label"], api_label)
        self.assertEqual(verdicts[event_id]["tier"], "affinity_confirmed")
        self.assertEqual(
            verdicts["unrelated-event"]["label"],
            "Codex local - unrelated@example.com",
        )

    def test_final_request_id_resolves_opaque_api_route_and_stops_at_new_request(self) -> None:
        turn_started_at = datetime(2026, 8, 2, 15, 0, 0)
        api_first = client_usage_export.UsageEvent(
            when=turn_started_at + timedelta(seconds=10),
            model="gpt-test",
            input_tokens=9_000,
            cached_tokens=8_000,
            output_tokens=100,
            session_id="opaque-api-session",
            account_at=turn_started_at,
        )
        api_final = client_usage_export.UsageEvent(
            when=turn_started_at + timedelta(seconds=20),
            model="gpt-test",
            input_tokens=10_000,
            cached_tokens=9_000,
            output_tokens=200,
            session_id="opaque-api-session",
            account_at=turn_started_at,
        )
        pending = client_usage_export.UsageEvent(
            when=turn_started_at + timedelta(seconds=30),
            model="gpt-test",
            input_tokens=11_000,
            cached_tokens=10_000,
            output_tokens=300,
            session_id="opaque-api-session",
            account_at=turn_started_at,
        )
        switched = client_usage_export.UsageEvent(
            when=turn_started_at + timedelta(seconds=40),
            model="gpt-test",
            input_tokens=12_000,
            cached_tokens=11_000,
            output_tokens=400,
            session_id="opaque-api-session",
            account_at=turn_started_at,
        )
        api_label = "Codex local - api-key-example"
        plus_label = "Codex local - plus@example.com"
        api_marker = client_usage_export.AccountMarker(
            when=api_final.when + timedelta(seconds=1),
            label=api_label,
            total_tokens=api_final.total_tokens,
            request_id="api-request",
            account_id="codex_apikey_manifest_id",
        )
        plus_marker = client_usage_export.AccountMarker(
            when=switched.when + timedelta(seconds=1),
            label=plus_label,
            total_tokens=switched.total_tokens,
            request_id="plus-request",
            account_id="plus-id",
        )
        affinity_events = [
            client_usage_export.CockpitAffinityEvent(
                when=api_first.when + timedelta(milliseconds=120),
                request_id="api-request",
                account_id="codex:apikey:opaque-hash",
                label="",
                action="cache hit",
            ),
            client_usage_export.CockpitAffinityEvent(
                when=api_final.when + timedelta(milliseconds=130),
                request_id="api-request",
                account_id="codex:apikey:opaque-hash",
                label="",
                action="cache hit",
            ),
            client_usage_export.CockpitAffinityEvent(
                when=pending.when + timedelta(milliseconds=140),
                request_id="pending-request",
                account_id="codex:apikey:opaque-hash",
                label="",
                action="cache hit",
            ),
            client_usage_export.CockpitAffinityEvent(
                when=switched.when + timedelta(milliseconds=150),
                request_id="plus-request",
                account_id="plus-id",
                label=plus_label,
                action="cache hit",
            ),
        ]

        resolved, session_accounts, unresolved = (
            client_usage_export.resolve_api_service_event_accounts(
                {
                    client_usage_export.API_SERVICE_AGGREGATE_LABEL: [
                        api_first,
                        api_final,
                        pending,
                        switched,
                    ]
                },
                [api_marker, plus_marker],
                affinity_events=affinity_events,
            )
        )

        self.assertEqual(resolved[api_label], [api_first, api_final])
        self.assertEqual(
            resolved[client_usage_export.API_SERVICE_AGGREGATE_LABEL],
            [pending],
        )
        self.assertEqual(resolved[plus_label], [switched])
        self.assertEqual(session_accounts[switched.session_id], plus_label)
        self.assertEqual(unresolved, 1)
        self.assertEqual(
            sum(
                event.total_tokens
                for provider_events in resolved.values()
                for event in provider_events
            ),
            sum(
                event.total_tokens
                for event in (api_first, api_final, pending, switched)
            ),
        )

    def test_exact_usage_marker_still_overrides_quota_fingerprint(self) -> None:
        quota_label = "Codex local - quota-owner@example.com"
        event = client_usage_export.UsageEvent(
            when=datetime(2026, 7, 28, 10, 0, 30),
            model="gpt-test",
            input_tokens=100_000,
            cached_tokens=80_000,
            output_tokens=1_000,
            session_id="quota-session",
            account_at=datetime(2026, 7, 28, 10, 0, 0),
            account_label_hint=quota_label,
            account_hint_source="quota_fingerprint",
        )
        final_label = "Codex local - final-account@example.com"
        exact_marker = client_usage_export.AccountMarker(
            when=event.when,
            label=final_label,
            total_tokens=event.total_tokens,
            input_tokens=event.input_tokens + event.cached_tokens,
            cached_tokens=event.cached_tokens,
            output_tokens=event.output_tokens,
        )

        resolved, session_accounts, unresolved = (
            client_usage_export.resolve_api_service_event_accounts(
                {quota_label: [event]},
                [exact_marker],
            )
        )

        self.assertEqual(resolved, {final_label: [event]})
        self.assertEqual(session_accounts["quota-session"], final_label)
        self.assertEqual(unresolved, 0)

    def test_exact_marker_maps_entire_quota_fingerprint_and_overrides_ledger(self) -> None:
        base = datetime(2026, 7, 28, 10, 0, 0)
        fingerprint = (10_080, 1_785_634_970)
        correct_label = "Codex local - plus@example.com"
        events = [
            client_usage_export.UsageEvent(
                when=base + timedelta(minutes=index),
                model="gpt-test",
                input_tokens=1_000 + index,
                cached_tokens=2_000,
                output_tokens=100,
                session_id="same-session",
                account_at=base + timedelta(minutes=index, seconds=-30),
                quota_fingerprints=(fingerprint,),
            )
            for index in range(2)
        ]
        marker = client_usage_export.AccountMarker(
            when=events[0].when,
            label=correct_label,
            total_tokens=events[0].total_tokens,
            input_tokens=events[0].input_tokens + events[0].cached_tokens,
            cached_tokens=events[0].cached_tokens,
            output_tokens=events[0].output_tokens,
        )
        ledger = {
            client_usage_export.codex_event_id(event): "Codex local - stale@example.com"
            for event in events
        }

        with patch.object(client_usage_export, "load_official_quota_cache", return_value={}):
            attributed = client_usage_export.attribute_codex_events_by_account(
                events,
                [marker],
                ledger,
            )
            resolved, session_accounts, unresolved = (
                client_usage_export.resolve_api_service_event_accounts(
                    attributed,
                    [marker],
                )
            )

        self.assertEqual(resolved[correct_label], events)
        self.assertEqual(session_accounts["same-session"], correct_label)
        self.assertEqual(unresolved, 0)
        self.assertTrue(all(event.account_hint_source == "quota_fingerprint" for event in events))
        self.assertEqual(set(ledger.values()), {correct_label})

    def test_official_quota_cache_maps_direct_event_without_request_marker(self) -> None:
        reset_at = datetime(2026, 8, 4, 11, 13, 25)
        fingerprint = (
            10_080,
            int(reset_at.replace(tzinfo=client_usage_export.LOCAL_TZ).timestamp()),
        )
        event = client_usage_export.UsageEvent(
            when=datetime(2026, 7, 28, 14, 38, 24),
            model="gpt-test",
            input_tokens=1_000,
            cached_tokens=2_000,
            output_tokens=100,
            session_id="direct-session",
            quota_fingerprints=(fingerprint,),
        )
        event_id = client_usage_export.codex_event_id(event)
        correct_label = "Codex local - current@example.com"
        cache = {
            "account-id": {
                "label": correct_label,
                "quota": {
                    "window_7d": {
                        "window_minutes": 10_080,
                        "resets_at": reset_at.replace(
                            tzinfo=client_usage_export.LOCAL_TZ
                        ).isoformat(),
                    }
                },
            }
        }
        ledger = {event_id: "Codex local - stale@example.com"}

        with patch.object(
            client_usage_export,
            "load_official_quota_cache",
            return_value=cache,
        ):
            attributed = client_usage_export.attribute_codex_events_by_account(
                [event],
                [],
                ledger,
            )

        self.assertEqual(attributed, {correct_label: [event]})
        self.assertEqual(ledger[event_id], correct_label)

    def test_conflicting_fingerprint_evidence_does_not_guess_an_account(self) -> None:
        base = datetime(2026, 7, 28, 10, 0, 0)
        fingerprint = (10_080, 1_785_634_970)
        events = [
            client_usage_export.UsageEvent(
                when=base + timedelta(seconds=index),
                model="gpt-test",
                input_tokens=1_000 + index,
                cached_tokens=2_000,
                output_tokens=100,
                session_id=f"session-{index}",
                quota_fingerprints=(fingerprint,),
            )
            for index in range(2)
        ]
        markers = [
            client_usage_export.AccountMarker(
                when=event.when,
                label=f"Codex local - account-{index}@example.com",
                total_tokens=event.total_tokens,
            )
            for index, event in enumerate(events)
        ]

        with patch.object(client_usage_export, "load_official_quota_cache", return_value={}):
            client_usage_export.apply_quota_fingerprint_account_hints(events, markers)

        self.assertTrue(all(not event.account_label_hint for event in events))


class CodexEventRowCacheTests(unittest.TestCase):
    @staticmethod
    def token_row(timestamp: str, total_tokens: int, cumulative_tokens: int) -> dict:
        return {
            "timestamp": timestamp,
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "last_token_usage": {
                        "input_tokens": total_tokens - 10,
                        "cached_input_tokens": 0,
                        "output_tokens": 10,
                        "total_tokens": total_tokens,
                    },
                    "total_token_usage": {
                        "input_tokens": cumulative_tokens - 10,
                        "cached_input_tokens": 0,
                        "output_tokens": 10,
                        "total_tokens": cumulative_tokens,
                    },
                },
            },
        }

    def test_persistent_cache_reuses_unchanged_rows_and_reads_only_append(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "rollout-session.jsonl"
            cache_path = root / "events-cache.json"
            rows = [
                {"type": "session_meta", "payload": {"id": "session"}},
                {
                    "type": "response_item",
                    "payload": {"text": "conversation text must not be cached"},
                },
                self.token_row("2026-07-15T10:00:00Z", 50, 50),
            ]
            path.write_text(
                "".join(json.dumps(row) + "\n" for row in rows),
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {"CLIENT_USAGE_CODEX_EVENT_CACHE": str(cache_path)},
            ):
                first = client_usage_export.CodexEventRowCache(cache_path)
                cached_rows = first.rows_for_path(path)
                self.assertEqual(len(cached_rows), 2)
                self.assertNotIn("response_item", {row["type"] for row in cached_rows})
                first.flush()

                second = client_usage_export.CodexEventRowCache(cache_path)
                with patch.object(
                    second,
                    "_read_complete_rows",
                    wraps=second._read_complete_rows,
                ) as read_rows:
                    self.assertEqual(len(second.rows_for_path(path)), 2)
                read_rows.assert_not_called()

                old_size = path.stat().st_size
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(
                        json.dumps(self.token_row("2026-07-15T10:00:01Z", 75, 125))
                        + "\n"
                    )
                with patch.object(
                    second,
                    "_read_complete_rows",
                    wraps=second._read_complete_rows,
                ) as read_rows:
                    cached_rows = second.rows_for_path(path)
                self.assertEqual(len(cached_rows), 3)
                self.assertEqual(read_rows.call_args.args[1], old_size)

    def test_compact_cache_preserves_quota_window_fingerprint(self) -> None:
        row = self.token_row("2026-07-15T10:00:00Z", 50, 50)
        row["payload"]["rate_limits"] = {
            "plan_type": "plus",
            "primary": {
                "used_percent": 12.0,
                "window_minutes": 10_080,
                "resets_at": 1_789_000_000,
            },
            "credits": {"has_credits": False},
        }

        compact = client_usage_export.compact_codex_cache_row(row)

        self.assertIsNotNone(compact)
        rate_limits = compact["payload"]["rate_limits"]
        self.assertEqual(rate_limits["plan_type"], "plus")
        self.assertEqual(rate_limits["primary"]["window_minutes"], 10_080)
        self.assertEqual(rate_limits["primary"]["resets_at"], 1_789_000_000)
        self.assertNotIn("credits", rate_limits)

    def test_deleted_rollout_is_recovered_from_persistent_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex_root = Path(directory) / ".codex"
            sessions_root = codex_root / "sessions"
            now = datetime.now().replace(microsecond=0)
            start = now - timedelta(hours=1)
            end = now + timedelta(hours=1)
            session_id = "019fa386-6c16-7ee1-8410-3e01fd8d97d1"
            day_dir = sessions_root / f"{now.year:04d}" / f"{now.month:02d}" / f"{now.day:02d}"
            day_dir.mkdir(parents=True)
            path = day_dir / f"rollout-{now:%Y-%m-%dT%H-%M-%S}-{session_id}.jsonl"
            rows = [
                {"type": "session_meta", "payload": {"id": session_id}},
                self.token_row(
                    now.replace(tzinfo=client_usage_export.LOCAL_TZ).isoformat(),
                    75,
                    75,
                ),
            ]
            path.write_text(
                "".join(json.dumps(row) + "\n" for row in rows),
                encoding="utf-8",
            )
            cache_path = Path(directory) / "events-cache.json"
            with patch.dict(
                os.environ,
                {"CLIENT_USAGE_CODEX_EVENT_CACHE": str(cache_path)},
            ):
                first_cache = client_usage_export.CodexEventRowCache(cache_path)
                with patch.object(
                    client_usage_export,
                    "_CODEX_EVENT_ROW_CACHE",
                    first_cache,
                ):
                    first = client_usage_export.scan_codex_events(
                        sessions_root,
                        start,
                        end,
                    )
                    first_cache.flush()
                self.assertEqual([event.total_tokens for event in first], [75])

                path.unlink()
                second_cache = client_usage_export.CodexEventRowCache(cache_path)
                with patch.object(
                    client_usage_export,
                    "_CODEX_EVENT_ROW_CACHE",
                    second_cache,
                ):
                    recovered = client_usage_export.scan_codex_events(
                        sessions_root,
                        start,
                        end,
                    )

            self.assertEqual([event.total_tokens for event in recovered], [75])
            self.assertEqual(recovered[0].session_id, session_id)

    def test_deleted_rollout_cache_does_not_cross_codex_roots(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first_sessions = root / "first" / ".codex" / "sessions"
            second_sessions = root / "second" / ".codex" / "sessions"
            now = datetime.now().replace(microsecond=0)
            foreign_path = (
                first_sessions
                / f"{now.year:04d}"
                / f"{now.month:02d}"
                / f"{now.day:02d}"
                / "rollout-foreign.jsonl"
            )
            foreign_path.parent.mkdir(parents=True)
            foreign_path.write_text(
                json.dumps(self.token_row(now.isoformat(), 75, 75)) + "\n",
                encoding="utf-8",
            )
            second_sessions.mkdir(parents=True)
            cache_path = root / "events-cache.json"
            cache = client_usage_export.CodexEventRowCache(cache_path)
            cache.rows_for_path(foreign_path)
            foreign_path.unlink()

            missing = cache.cached_missing_paths(
                second_sessions,
                now - timedelta(hours=1),
            )

            self.assertEqual(missing, [])

    def test_stale_cache_writer_merges_entries_written_by_another_process(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache_path = root / "events-cache.json"
            first_path = root / "first.jsonl"
            second_path = root / "second.jsonl"
            first_path.write_text(
                json.dumps(self.token_row("2026-07-15T10:00:00Z", 50, 50)) + "\n",
                encoding="utf-8",
            )
            second_path.write_text(
                json.dumps(self.token_row("2026-07-15T10:00:01Z", 75, 75)) + "\n",
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {"CLIENT_USAGE_CODEX_EVENT_CACHE": str(cache_path)},
            ):
                stale_writer = client_usage_export.CodexEventRowCache(cache_path)
                concurrent_writer = client_usage_export.CodexEventRowCache(cache_path)
                stale_writer.rows_for_path(first_path)
                concurrent_writer.rows_for_path(second_path)
                concurrent_writer.flush()
                stale_writer.flush()

                merged = client_usage_export.CodexEventRowCache(cache_path)
                merged._load()

            self.assertEqual(len(merged.entries), 2)
            self.assertIn(merged._key(first_path), merged.entries)
            self.assertIn(merged._key(second_path), merged.entries)

    def test_incomplete_last_json_row_is_retried_after_append(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "rollout-session.jsonl"
            cache = client_usage_export.CodexEventRowCache(root / "cache.json")
            path.write_text(
                '{"type":"session_meta","payload":{"id":"session"}}\n'
                '{"type":"event_msg","payload":',
                encoding="utf-8",
            )

            self.assertEqual(len(cache.rows_for_path(path)), 1)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(
                    '{"type":"token_count","info":{"last_token_usage":'
                    '{"input_tokens":40,"cached_input_tokens":0,'
                    '"output_tokens":10,"total_tokens":50}}}}\n'
                )

            rows = cache.rows_for_path(path)
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[-1]["payload"]["type"], "token_count")

    def test_truncated_file_discards_cached_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "rollout-session.jsonl"
            cache = client_usage_export.CodexEventRowCache(root / "cache.json")
            path.write_text(
                json.dumps(self.token_row("2026-07-15T10:00:00Z", 500, 500)) + "\n",
                encoding="utf-8",
            )
            self.assertEqual(len(cache.rows_for_path(path)), 1)

            path.write_text(
                json.dumps(self.token_row("2026-07-15T10:00:01Z", 25, 25)) + "\n",
                encoding="utf-8",
            )
            rows = cache.rows_for_path(path)

            usage = rows[0]["payload"]["info"]["last_token_usage"]
            self.assertEqual(usage["total_tokens"], 25)

    def test_state_database_indexes_sessions_but_not_archived_rollouts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex_root = Path(directory) / ".codex"
            sessions_root = codex_root / "sessions"
            session_path = sessions_root / "2026" / "07" / "15" / (
                "rollout-2026-07-15T10-00-00-"
                "019f54a2-9034-7651-a517-89989e6d6b1b.jsonl"
            )
            archived_path = codex_root / "archived_sessions" / (
                "rollout-2026-07-15T09-00-00-"
                "019f54a2-9034-7651-a517-89989e6d6b1c.jsonl"
            )
            session_path.parent.mkdir(parents=True)
            archived_path.parent.mkdir(parents=True)
            session_path.write_text("{}\n", encoding="utf-8")
            archived_path.write_text("{}\n", encoding="utf-8")
            database = codex_root / "state_5.sqlite"
            connection = sqlite3.connect(database)
            connection.execute(
                "CREATE TABLE threads (id TEXT, rollout_path TEXT, updated_at_ms INTEGER)"
            )
            now_ms = int(datetime.now().timestamp() * 1000)
            connection.executemany(
                "INSERT INTO threads VALUES (?, ?, ?)",
                (
                    (
                        "session",
                        f"\\\\?\\{session_path}" if os.name == "nt" else str(session_path),
                        now_ms,
                    ),
                    ("archived", str(archived_path), now_ms),
                ),
            )
            connection.commit()
            connection.close()

            paths, available = client_usage_export.codex_state_rollout_paths(
                sessions_root,
                datetime.now() - timedelta(hours=1),
            )

            self.assertTrue(available)
            self.assertEqual(paths, [session_path.resolve()])

    def test_recent_rollouts_include_archives_and_prefer_live_session_copy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex_root = Path(directory) / ".codex"
            sessions_root = codex_root / "sessions"
            now = datetime.now()
            day_dir = sessions_root / f"{now.year:04d}" / f"{now.month:02d}" / f"{now.day:02d}"
            archived_root = codex_root / "archived_sessions"
            day_dir.mkdir(parents=True)
            archived_root.mkdir(parents=True)

            duplicate_id = "019f54a2-9034-7651-a517-89989e6d6b1b"
            archived_only_id = "019f54a2-9034-7651-a517-89989e6d6b1c"
            duplicate_name = f"rollout-{now:%Y-%m-%dT%H-%M-%S}-{duplicate_id}.jsonl"
            live_path = day_dir / duplicate_name
            archived_duplicate = archived_root / duplicate_name
            archived_only = archived_root / (
                f"rollout-{now:%Y-%m-%dT%H-%M-%S}-{archived_only_id}.jsonl"
            )
            for path in (live_path, archived_duplicate, archived_only):
                path.write_text("{}\n", encoding="utf-8")

            session_paths: dict[str, Path] = {}
            paths = client_usage_export.iter_recent_jsonl(
                sessions_root,
                now - timedelta(hours=1),
                session_paths=session_paths,
            )

            self.assertEqual(set(paths), {live_path.resolve(), archived_only.resolve()})
            self.assertEqual(session_paths[duplicate_id], live_path.resolve())
            self.assertEqual(session_paths[archived_only_id], archived_only.resolve())

    def test_archived_token_event_is_counted_once_when_live_copy_exists(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex_root = Path(directory) / ".codex"
            sessions_root = codex_root / "sessions"
            archived_root = codex_root / "archived_sessions"
            now = datetime.now()
            start = now - timedelta(hours=1)
            end = now + timedelta(hours=1)
            event_at = (now - timedelta(minutes=5)).replace(
                tzinfo=client_usage_export.LOCAL_TZ
            )
            session_id = "019f54a2-9034-7651-a517-89989e6d6b1d"
            filename = f"rollout-{now:%Y-%m-%dT%H-%M-%S}-{session_id}.jsonl"
            rows = [
                {"type": "session_meta", "payload": {"id": session_id}},
                self.token_row(event_at.isoformat(), 50, 50),
            ]
            content = "".join(json.dumps(row) + "\n" for row in rows)
            archived_root.mkdir(parents=True)
            archived_path = archived_root / filename
            archived_path.write_text(content, encoding="utf-8")

            archived_events = client_usage_export.scan_codex_events(
                sessions_root,
                start,
                end,
            )
            self.assertEqual(len(archived_events), 1)
            self.assertEqual(archived_events[0].total_tokens, 50)

            live_dir = sessions_root / f"{now.year:04d}" / f"{now.month:02d}" / f"{now.day:02d}"
            live_dir.mkdir(parents=True)
            (live_dir / filename).write_text(content, encoding="utf-8")
            deduplicated_events = client_usage_export.scan_codex_events(
                sessions_root,
                start,
                end,
            )
            self.assertEqual(len(deduplicated_events), 1)
            self.assertEqual(deduplicated_events[0].total_tokens, 50)


class CodexUsageFileWatcherTests(unittest.TestCase):
    @staticmethod
    def append_text(path: Path, text: str) -> None:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(text)

    def test_only_appended_token_count_triggers_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "rollout-session.jsonl"
            path.write_text('{"type":"session_meta"}\n', encoding="utf-8")
            watcher = monitor.CodexUsageFileWatcher(root)

            self.assertFalse(watcher.poll())
            self.append_text(path, '{"type":"event_msg","payload":{"type":"task_started"}}\n')
            self.assertFalse(watcher.poll())
            token_row = {
                "timestamp": "2026-07-14T10:00:00Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "last_token_usage": {
                            "input_tokens": 40,
                            "cached_input_tokens": 20,
                            "output_tokens": 10,
                            "total_tokens": 50,
                        },
                        "total_token_usage": {
                            "input_tokens": 400,
                            "cached_input_tokens": 200,
                            "output_tokens": 100,
                        },
                    },
                },
            }
            self.append_text(path, json.dumps(token_row) + "\n")
            events = watcher.poll_events()
            self.assertTrue(watcher.token_count_changed)
            self.assertEqual([event["total_tokens"] for event in events], [50])
            self.assertTrue(events[0]["event_id"])
            self.assertFalse(watcher.poll())

            token_row["timestamp"] = "2026-07-14T10:00:01Z"
            self.append_text(path, json.dumps(token_row) + "\n")
            self.assertEqual(watcher.poll_events(), [])
            self.assertTrue(watcher.token_count_changed)

    def test_initial_checkpoint_cutoff_recovers_only_new_tail_events(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "rollout-session.jsonl"

            def token_row(timestamp: str, total_tokens: int) -> dict:
                return {
                    "timestamp": timestamp,
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "last_token_usage": {
                                "input_tokens": total_tokens,
                                "cached_input_tokens": 0,
                                "output_tokens": 0,
                                "total_tokens": total_tokens,
                            },
                            "total_token_usage": {
                                "input_tokens": total_tokens,
                                "cached_input_tokens": 0,
                                "output_tokens": 0,
                            },
                        },
                    },
                }

            rows = [
                {"type": "session_meta", "payload": {}},
                token_row("2026-08-24T10:00:00Z", 50),
                token_row("2026-08-24T10:05:00Z", 75),
            ]
            path.write_text(
                "".join(json.dumps(row) + "\n" for row in rows),
                encoding="utf-8",
            )
            watcher = monitor.CodexUsageFileWatcher(
                root,
                initial_since=datetime.fromisoformat(
                    "2026-08-24T10:02:00+00:00"
                ),
            )

            recovered = watcher.poll_events()

            self.assertEqual(
                [event["total_tokens"] for event in recovered],
                [75],
            )
            self.assertTrue(watcher.token_count_changed)
            self.assertEqual(watcher.poll_events(), [])

    def test_live_event_carries_the_session_id_from_its_rollout_path(self) -> None:
        session_id = "019f54a2-9034-7651-a517-89989e6d6b1b"
        watcher = monitor.CodexUsageFileWatcher(Path("unused"))
        row = {
            "timestamp": "2026-07-14T10:00:00Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "last_token_usage": {
                        "input_tokens": 40,
                        "cached_input_tokens": 20,
                        "output_tokens": 10,
                        "total_tokens": 50,
                    }
                },
            },
        }

        events = watcher._extract_live_events(
            Path(f"rollout-2026-07-14T10-00-00-{session_id}.jsonl"),
            (json.dumps(row) + "\n").encode("utf-8"),
        )

        self.assertEqual(events[0]["session_id"], session_id)

    def test_fork_replay_rows_are_never_emitted_as_live_usage(self) -> None:
        def token_row(timestamp: str, total_tokens: int, cumulative_tokens: int) -> dict:
            return {
                "timestamp": timestamp,
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "last_token_usage": {
                            "input_tokens": total_tokens - 10,
                            "cached_input_tokens": 0,
                            "output_tokens": 10,
                            "total_tokens": total_tokens,
                        },
                        "total_token_usage": {
                            "input_tokens": cumulative_tokens - 10,
                            "cached_input_tokens": 0,
                            "output_tokens": 10,
                        },
                    },
                },
            }

        with tempfile.TemporaryDirectory() as directory:
            session_id = "019f63da-8783-7a50-96e9-fa642c195631"
            path = Path(directory) / f"rollout-2026-07-15T11-38-19-{session_id}.jsonl"
            rows = [
                {
                    "timestamp": "2026-07-15T03:00:00Z",
                    "type": "session_meta",
                    "payload": {
                        "id": session_id,
                        "forked_from_id": "019f54f7-a5e8-76e2-be50-853c7be0d373",
                    },
                },
                token_row("2026-07-15T02:59:59Z", 1_000_000, 1_000_000),
                token_row("2026-07-15T03:00:03Z", 75, 1_000_075),
            ]
            path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            watcher = monitor.CodexUsageFileWatcher(path.parent)

            events = watcher._extract_live_events(path, path.read_bytes())

        self.assertEqual([event["total_tokens"] for event in events], [75])

    def test_new_session_with_token_count_triggers_after_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            watcher = monitor.CodexUsageFileWatcher(root)

            self.assertFalse(watcher.poll())
            today = datetime.now()
            path = (
                root
                / f"{today.year:04d}"
                / f"{today.month:02d}"
                / f"{today.day:02d}"
                / "rollout-new.jsonl"
            )
            path.parent.mkdir(parents=True)
            path.write_text(
                '{"type":"event_msg","payload":{"type": "token_count"}}\n',
                encoding="utf-8",
            )

            self.assertTrue(watcher.poll())
            self.assertTrue(watcher.reconciliation_needed)

    def test_new_rollout_events_stay_provisional_during_observation_window(self) -> None:
        def token_row(second: int, total_tokens: int) -> str:
            row = {
                "timestamp": f"2026-07-15T03:00:{second:02d}Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "last_token_usage": {
                            "input_tokens": total_tokens - 10,
                            "cached_input_tokens": 0,
                            "output_tokens": 10,
                            "total_tokens": total_tokens,
                        },
                        "total_token_usage": {
                            "input_tokens": total_tokens - 10,
                            "cached_input_tokens": 0,
                            "output_tokens": 10,
                        },
                    },
                },
            }
            return json.dumps(row) + "\n"

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "rollout-new.jsonl"
            path.write_text(token_row(0, 100), encoding="utf-8")
            watcher = monitor.CodexUsageFileWatcher(root)
            watcher._primed = True
            watcher._last_full_scan_at = 100.0
            watcher._hot_files[path] = path.stat().st_mtime_ns

            with patch.object(monitor.time, "monotonic", return_value=100.0):
                self.assertEqual(watcher.poll_events(), [])
            self.assertTrue(watcher.reconciliation_needed)
            self.assertIn(path, watcher._reconciliation_paths)

            self.append_text(path, token_row(1, 200))
            with patch.object(monitor.time, "monotonic", return_value=105.0):
                self.assertEqual(watcher.poll_events(), [])
            self.assertTrue(watcher.reconciliation_needed)

            watcher.mark_reconciled()
            self.append_text(path, token_row(2, 300))
            with patch.object(monitor.time, "monotonic", return_value=110.0):
                self.assertEqual(watcher.poll_events(), [])
            self.assertTrue(watcher.reconciliation_needed)

            watcher.mark_reconciled()
            self.append_text(path, token_row(3, 400))
            with patch.object(monitor.time, "monotonic", return_value=121.0):
                events = watcher.poll_events()
            self.assertEqual([event["total_tokens"] for event in events], [400])

    def test_new_non_fork_rollout_emits_immediately_when_metadata_is_known(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            watcher = monitor.CodexUsageFileWatcher(root)
            watcher.poll_events()
            today = datetime.now()
            path = (
                root
                / f"{today.year:04d}"
                / f"{today.month:02d}"
                / f"{today.day:02d}"
                / "rollout-new-non-fork.jsonl"
            )
            path.parent.mkdir(parents=True)
            rows = [
                {
                    "timestamp": "2026-07-15T03:00:00Z",
                    "type": "session_meta",
                    "payload": {"id": "session-new"},
                },
                {
                    "timestamp": "2026-07-15T03:00:03Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "last_token_usage": {
                                "input_tokens": 90,
                                "cached_input_tokens": 0,
                                "output_tokens": 10,
                                "total_tokens": 100,
                            },
                            "total_token_usage": {
                                "input_tokens": 90,
                                "cached_input_tokens": 0,
                                "output_tokens": 10,
                            },
                        },
                    },
                },
            ]
            path.write_text(
                "".join(json.dumps(row) + "\n" for row in rows),
                encoding="utf-8",
            )

            events = watcher.poll_events()

            self.assertEqual([event["total_tokens"] for event in events], [100])
            self.assertFalse(watcher.reconciliation_needed)
            self.assertNotIn(path, watcher._reconciliation_paths)

    def test_new_fork_rollout_emits_only_rows_after_known_cutoff(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            watcher = monitor.CodexUsageFileWatcher(root)
            watcher.poll_events()
            today = datetime.now()
            path = (
                root
                / f"{today.year:04d}"
                / f"{today.month:02d}"
                / f"{today.day:02d}"
                / "rollout-new-fork.jsonl"
            )
            path.parent.mkdir(parents=True)

            def token_row(timestamp: str, total_tokens: int) -> dict:
                return {
                    "timestamp": timestamp,
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "last_token_usage": {
                                "input_tokens": total_tokens - 10,
                                "cached_input_tokens": 0,
                                "output_tokens": 10,
                                "total_tokens": total_tokens,
                            },
                            "total_token_usage": {
                                "input_tokens": total_tokens - 10,
                                "cached_input_tokens": 0,
                                "output_tokens": 10,
                            },
                        },
                    },
                }

            rows = [
                {
                    "timestamp": "2026-07-15T03:00:00Z",
                    "type": "session_meta",
                    "payload": {
                        "id": "session-fork",
                        "forked_from_id": "session-parent",
                    },
                },
                token_row("2026-07-15T02:59:59Z", 1_000_000),
                token_row("2026-07-15T03:00:03Z", 75),
            ]
            path.write_text(
                "".join(json.dumps(row) + "\n" for row in rows),
                encoding="utf-8",
            )

            events = watcher.poll_events()

            self.assertEqual([event["total_tokens"] for event in events], [75])
            self.assertFalse(watcher.reconciliation_needed)

    def test_hot_file_capacity_covers_many_concurrent_sessions(self) -> None:
        self.assertGreaterEqual(monitor.LIVE_USAGE_WATCH_HOT_FILE_LIMIT, 64)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            today = datetime.now()
            day_root = (
                root
                / f"{today.year:04d}"
                / f"{today.month:02d}"
                / f"{today.day:02d}"
            )
            day_root.mkdir(parents=True)
            paths: list[Path] = []
            for index in range(24):
                path = day_root / f"rollout-{index:02d}.jsonl"
                path.write_text(
                    json.dumps(
                        {
                            "timestamp": "2026-07-15T03:00:00Z",
                            "type": "session_meta",
                            "payload": {"id": f"session-{index:02d}"},
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
                paths.append(path)
            watcher = monitor.CodexUsageFileWatcher(root)
            watcher.poll_events()
            for index, path in enumerate(paths):
                row = {
                    "timestamp": f"2026-07-15T03:01:{index:02d}Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "last_token_usage": {
                                "input_tokens": 90 + index,
                                "cached_input_tokens": 0,
                                "output_tokens": 10,
                                "total_tokens": 100 + index,
                            },
                            "total_token_usage": {
                                "input_tokens": 90 + index,
                                "cached_input_tokens": 0,
                                "output_tokens": 10,
                            },
                        },
                    },
                }
                self.append_text(path, json.dumps(row) + "\n")

            events = watcher.poll_events()

            self.assertEqual(len(events), 24)
            self.assertEqual(
                {event["total_tokens"] for event in events},
                set(range(100, 124)),
            )

    def test_hot_file_fallback_polling_rotates_across_the_full_set(self) -> None:
        watcher = monitor.CodexUsageFileWatcher(Path("unused"))
        tracked = {
            Path(f"rollout-{index:02d}.jsonl")
            for index in range(monitor.LIVE_USAGE_WATCH_HOT_POLL_LIMIT + 8)
        }
        watcher._hot_files = {
            path: index for index, path in enumerate(sorted(tracked))
        }

        first = watcher._hot_poll_paths()
        second = watcher._hot_poll_paths()

        self.assertEqual(len(first), monitor.LIVE_USAGE_WATCH_HOT_POLL_LIMIT)
        self.assertEqual(first | second, tracked)

    def test_marker_split_across_writes_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "rollout-session.jsonl"
            path.write_text('{"type":"session_meta"}\n', encoding="utf-8")
            watcher = monitor.CodexUsageFileWatcher(root)
            watcher.poll()

            self.append_text(path, '{"type":"event_msg","payload":{"type":"token_')
            self.assertFalse(watcher.poll())
            self.append_text(path, 'count"}}\n')

            self.assertTrue(watcher.poll())

    def test_incremental_read_uses_only_a_small_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rollout-session.jsonl"
            path.write_bytes(b"x" * 100_000)
            watcher = monitor.CodexUsageFileWatcher(
                path.parent,
                max_read_bytes=64 * 1024,
            )
            start = path.stat().st_size
            self.append_text(path, '\n{"type":"event_msg"}\n')
            end = path.stat().st_size

            data = watcher._read_region(path, start, end)

            self.assertLessEqual(
                len(data),
                monitor.LIVE_USAGE_WATCH_OVERLAP_BYTES + (end - start),
            )

    def test_windows_notification_buffer_decodes_relative_jsonl_paths(self) -> None:
        def record(name: str, *, last: bool) -> bytes:
            encoded = name.encode("utf-16-le")
            length = 12 + len(encoded)
            padded = (length + 3) & ~3
            next_offset = 0 if last else padded
            return (
                next_offset.to_bytes(4, "little")
                + (3).to_bytes(4, "little")
                + len(encoded).to_bytes(4, "little")
                + encoded
                + b"\x00" * (padded - length)
            )

        data = record("2026\\07\\15\\rollout-a.jsonl", last=False) + record(
            "2026\\07\\15\\rollout-b.jsonl",
            last=True,
        )

        self.assertEqual(
            monitor.WindowsDirectoryChangeSignal._decode_paths(data),
            [
                "2026\\07\\15\\rollout-a.jsonl",
                "2026\\07\\15\\rollout-b.jsonl",
            ],
        )

    def test_native_change_signal_feeds_existing_incremental_parser(self) -> None:
        class FakeSignal:
            def __init__(self, path: Path) -> None:
                self.path = path

            def drain(self) -> tuple[set[Path], bool]:
                path, self.path = self.path, Path()
                return ({path} if str(path) != "." else set()), False

            def close(self) -> None:
                return None

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "rollout-session.jsonl"
            path.write_text('{"type":"session_meta"}\n', encoding="utf-8")
            watcher = monitor.CodexUsageFileWatcher(root)
            watcher.poll_events()
            watcher._hot_files.clear()
            watcher._last_full_scan_at = monitor.time.monotonic()
            token_row = {
                "timestamp": "2026-07-15T10:00:00Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "last_token_usage": {
                            "input_tokens": 40,
                            "cached_input_tokens": 0,
                            "output_tokens": 10,
                            "total_tokens": 50,
                        }
                    },
                },
            }
            self.append_text(path, json.dumps(token_row) + "\n")
            watcher._directory_changes = FakeSignal(path)

            with patch.object(watcher, "_discover_recent_paths", return_value=set()), patch.object(
                watcher,
                "_full_scan_paths",
                wraps=watcher._full_scan_paths,
            ) as full_scan:
                events = watcher.poll_events()

            full_scan.assert_not_called()
            self.assertEqual([event["total_tokens"] for event in events], [50])
            watcher.close()

    def test_hot_poll_does_not_repeat_the_full_directory_scan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "rollout-session.jsonl"
            path.write_text('{"type":"session_meta"}\n', encoding="utf-8")
            watcher = monitor.CodexUsageFileWatcher(root)
            watcher.poll()

            with patch.object(
                watcher,
                "_full_scan_paths",
                wraps=watcher._full_scan_paths,
            ) as full_scan:
                watcher.poll()

            full_scan.assert_not_called()

    def test_poll_interval_backs_off_while_idle(self) -> None:
        with patch.object(monitor.time, "monotonic", return_value=100.0):
            watcher = monitor.CodexUsageFileWatcher(Path("unused"))

        self.assertEqual(
            watcher.next_poll_interval_ms(105.0),
            monitor.LIVE_USAGE_WATCH_INTERVAL_MS,
        )
        self.assertEqual(
            watcher.next_poll_interval_ms(130.0),
            monitor.LIVE_USAGE_WATCH_IDLE_INTERVAL_MS,
        )
        self.assertEqual(
            watcher.next_poll_interval_ms(170.0),
            monitor.LIVE_USAGE_WATCH_COLD_INTERVAL_MS,
        )
        self.assertFalse(watcher.has_recent_activity(60.0))
        watcher._activity_observed = True
        watcher._last_activity_at = 100.0
        with patch.object(monitor.time, "monotonic", return_value=105.0):
            self.assertTrue(watcher.has_recent_activity(60.0))

    def test_new_rollout_reconciliation_waits_for_observation_window(self) -> None:
        watcher = monitor.CodexUsageFileWatcher(Path("unused"))
        watched = Path("rollout-new.jsonl")
        watcher.reconciliation_needed = True
        watcher._reconciliation_paths = {
            watched: 120.0,
            Path("rollout-newer.jsonl"): 125.0,
        }
        watcher._last_reconciliation_change_at = 100.0

        with patch.object(monitor.time, "monotonic", return_value=103.0):
            self.assertFalse(watcher.reconciliation_ready(5.0))
        with patch.object(monitor.time, "monotonic", return_value=106.0):
            self.assertFalse(watcher.reconciliation_ready(5.0))
        with patch.object(monitor.time, "monotonic", return_value=120.0):
            self.assertFalse(watcher.reconciliation_ready(5.0))
        with patch.object(monitor.time, "monotonic", return_value=125.0):
            self.assertTrue(watcher.reconciliation_ready(5.0))

        watcher.mark_reconciled()
        self.assertFalse(watcher.reconciliation_needed)

    def test_full_scan_fallback_discovers_a_cold_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            watcher = monitor.CodexUsageFileWatcher(root)
            watcher.poll()
            path = root / "2025" / "01" / "01" / "rollout-cold.jsonl"
            path.parent.mkdir(parents=True)
            path.write_text(
                '{"type":"event_msg","payload":{"type":"token_count"}}\n',
                encoding="utf-8",
            )
            watcher._last_full_scan_at -= monitor.LIVE_USAGE_WATCH_FULL_SCAN_SECONDS + 1

            self.assertTrue(watcher.poll())


class LiveActiveSessionScanTests(unittest.TestCase):
    SESSION_ID = "019f54a2-9034-7651-a517-89989e6d6b1b"

    def write_cockpit_marker(
        self,
        path: Path,
        when: datetime,
        *,
        email: str,
        input_tokens: int,
        cached_tokens: int,
        output_tokens: int,
    ) -> None:
        connection = sqlite3.connect(path)
        try:
            connection.execute(
                """
                CREATE TABLE request_logs (
                    timestamp INTEGER,
                    account_id TEXT,
                    email TEXT,
                    api_key_label TEXT,
                    model_id TEXT,
                    total_tokens INTEGER,
                    input_tokens INTEGER,
                    cached_tokens INTEGER,
                    output_tokens INTEGER
                )
                """
            )
            connection.execute(
                "INSERT INTO request_logs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    int(when.timestamp() * 1000),
                    "account-1",
                    email,
                    "Default",
                    "gpt-test",
                    input_tokens + output_tokens,
                    input_tokens,
                    cached_tokens,
                    output_tokens,
                ),
            )
            connection.commit()
        finally:
            connection.close()

    def write_live_rows(self, root: Path, rows: list[dict]) -> Path:
        root.mkdir(parents=True, exist_ok=True)
        path = root / f"rollout-2026-07-14T09-00-00-{self.SESSION_ID}.jsonl"
        path.write_text(
            "\n".join(json.dumps(row) for row in rows) + "\n",
            encoding="utf-8",
        )
        return path

    def write_session(self, root: Path, lifecycle: str) -> Path:
        root.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        path = root / f"rollout-2026-07-14T09-00-00-{self.SESSION_ID}.jsonl"
        rows = [
            {
                "type": "session_meta",
                "payload": {"id": self.SESSION_ID},
            },
            {
                "timestamp": timestamp,
                "type": "event_msg",
                "payload": {"type": lifecycle, "turn_id": "turn-1"},
            },
            {
                "timestamp": timestamp,
                "type": "event_msg",
                "payload": {"type": "token_count", "info": {}},
            },
        ]
        path.write_text(
            "\n".join(json.dumps(row) for row in rows) + "\n",
            encoding="utf-8",
        )
        return path

    def test_running_tail_reuses_cached_account_without_full_export(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_session(root, "task_started")
            with patch.object(
                monitor,
                "_current_codex_account_label",
                return_value="Codex local - account@example.com",
            ):
                rows = monitor.scan_live_codex_active_sessions(
                    root,
                    [
                        {
                            "session_id": self.SESSION_ID,
                            "provider": "Codex local - account@example.com",
                            "model": "gpt-test",
                        }
                    ],
                )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["provider"], "Codex local - account@example.com")
        self.assertEqual(rows[0]["activity_source"], "live-session-tail")

    def test_direct_account_switch_overrides_stale_cached_session_account(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_session(root, "task_started")
            with patch.object(
                monitor,
                "_current_codex_account_label",
                return_value="Codex local - current@example.com",
            ):
                rows = monitor.scan_live_codex_active_sessions(
                    root,
                    [
                        {
                            "session_id": self.SESSION_ID,
                            "provider": "Codex local - previous@example.com",
                            "model": "gpt-test",
                        }
                    ],
                )

        self.assertEqual(len(rows), 1)
        self.assertEqual(
            rows[0]["provider"],
            "Codex local - current@example.com",
        )

    def test_unchanged_live_tail_is_not_read_twice(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_session(root, "task_started")
            tail_cache: dict = {}
            with patch.object(
                monitor,
                "_read_jsonl_tail",
                wraps=monitor._read_jsonl_tail,
            ) as read_tail:
                first = monitor.scan_live_codex_active_sessions(
                    root,
                    [],
                    tail_cache=tail_cache,
                )
                second = monitor.scan_live_codex_active_sessions(
                    root,
                    [],
                    tail_cache=tail_cache,
                )

        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 1)
        session_tail_reads = [
            call
            for call in read_tail.call_args_list
            if Path(call.args[0]).suffix.casefold() == ".jsonl"
        ]
        self.assertEqual(len(session_tail_reads), 1)

    def test_completed_tail_is_removed_immediately(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_session(root, "task_complete")
            rows = monitor.scan_live_codex_active_sessions(root, [])

        self.assertEqual(rows, [])

    def test_api_service_session_uses_recent_cockpit_token_marker(self) -> None:
        now = datetime.now(timezone.utc)
        started_at = now - timedelta(seconds=2)
        token_at = now - timedelta(seconds=1)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db_path = root / "requests.sqlite"
            self.write_live_rows(
                root / "sessions",
                [
                    {"type": "session_meta", "payload": {"id": self.SESSION_ID}},
                    {
                        "timestamp": started_at.isoformat(),
                        "type": "event_msg",
                        "payload": {"type": "task_started", "turn_id": "turn-1"},
                    },
                    {
                        "timestamp": token_at.isoformat(),
                        "type": "event_msg",
                        "payload": {
                            "type": "token_count",
                            "info": {
                                "last_token_usage": {
                                    "input_tokens": 10_000,
                                    "cached_input_tokens": 8_000,
                                    "output_tokens": 500,
                                    "total_tokens": 10_500,
                                }
                            },
                        },
                    },
                ],
            )
            self.write_cockpit_marker(
                db_path,
                token_at + timedelta(milliseconds=150),
                email="routed@example.com",
                input_tokens=10_000,
                cached_tokens=8_000,
                output_tokens=500,
            )
            with patch.object(
                monitor,
                "_current_codex_account_label",
                return_value="Codex local - api-service-local",
            ):
                rows = monitor.scan_live_codex_active_sessions(
                    root / "sessions",
                    [{"session_id": self.SESSION_ID, "provider": "正在识别账号"}],
                    now=now,
                    cockpit_db_path=db_path,
                )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["provider"], "Codex local - routed@example.com")
        self.assertEqual(rows[0]["model"], "gpt-test")
        self.assertTrue(rows[0]["provider_confirmed"])
        self.assertEqual(
            rows[0]["usage_event_id"],
            monitor._live_usage_event_id(
                token_at,
                self.SESSION_ID,
                10_000,
                8_000,
                500,
            ),
        )

    def test_api_service_session_uses_route_hint_before_final_usage(self) -> None:
        now = datetime.now(timezone.utc)
        started_at = now - timedelta(seconds=2)
        route_at = started_at + timedelta(milliseconds=350)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sessions = root / "sessions"
            self.write_live_rows(
                sessions,
                [
                    {"type": "session_meta", "payload": {"id": self.SESSION_ID}},
                    {
                        "timestamp": started_at.isoformat(),
                        "type": "event_msg",
                        "payload": {"type": "task_started", "turn_id": "turn-route"},
                    },
                ],
            )
            (root / "codex_accounts.json").write_text(
                json.dumps(
                    {
                        "accounts": [
                            {
                                "id": "codex-route-account",
                                "email": "route-hint@example.com",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            logs = root / "logs"
            logs.mkdir()
            (logs / "codex-api.log.test").write_text(
                (
                    f'{route_at.isoformat()} WARN msg="session-affinity: cache hit | '
                    "session=test auth=codex-route-account.json provider=mixed "
                    'model=gpt-route" request_id=request-route\n'
                ),
                encoding="utf-8",
            )
            with patch.object(
                monitor,
                "_current_codex_account_label",
                return_value="Codex local - api-service-local",
            ):
                rows = monitor.scan_live_codex_active_sessions(
                    sessions,
                    [],
                    now=now,
                    cockpit_db_path=root / "requests.sqlite",
                )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["provider"], "Codex local - route-hint@example.com")
        self.assertFalse(rows[0]["provider_confirmed"])
        self.assertTrue(rows[0]["provider_provisional"])
        self.assertEqual(rows[0]["provider_confirmation_source"], "active_route_hint")
        self.assertEqual(rows[0]["provider_route_request_id"], "request-route")

    def test_live_route_hint_uses_reselected_account(self) -> None:
        started_at = datetime(2026, 7, 24, 7, 0, tzinfo=timezone.utc)
        events = [
            {
                "when": started_at + timedelta(milliseconds=100),
                "request_id": "request-1",
                "kind": "route",
                "account_id": "k12-account",
                "label": "Codex local - k12@example.com",
                "model": "gpt-test",
            },
            {
                "when": started_at + timedelta(milliseconds=300),
                "request_id": "request-1",
                "kind": "invalid",
                "account_id": "k12-account",
                "label": "Codex local - k12@example.com",
                "model": "gpt-test",
            },
            {
                "when": started_at + timedelta(milliseconds=500),
                "request_id": "request-1",
                "kind": "route",
                "account_id": "plus-account",
                "label": "Codex local - plus@example.com",
                "model": "gpt-test",
            },
        ]

        hints = monitor._match_live_cockpit_route_hints([started_at], events)

        self.assertEqual(hints[0]["label"], "Codex local - plus@example.com")
        self.assertEqual(hints[0]["account_id"], "plus-account")

    def test_live_route_hint_does_not_cross_ambiguous_sessions(self) -> None:
        started_at = datetime(2026, 7, 24, 7, 0, tzinfo=timezone.utc)
        events = [
            {
                "when": started_at + timedelta(milliseconds=50),
                "request_id": "ambiguous-request",
                "kind": "route",
                "account_id": "account-1",
                "label": "Codex local - ambiguous@example.com",
                "model": "gpt-test",
            }
        ]

        hints = monitor._match_live_cockpit_route_hints(
            [started_at, started_at + timedelta(milliseconds=100)],
            events,
        )

        self.assertEqual(hints[0]["source"], "active_route_ambiguous")
        self.assertEqual(hints[1]["source"], "active_route_ambiguous")
        self.assertTrue(hints[0]["resolved"])
        self.assertFalse(hints[0]["label"])

    def test_live_route_hint_jointly_matches_concurrent_requests(self) -> None:
        first_boundary = datetime(2026, 7, 24, 7, 0, tzinfo=timezone.utc)
        second_boundary = first_boundary + timedelta(milliseconds=130)
        events = [
            {
                "when": first_boundary + timedelta(milliseconds=26),
                "request_id": "request-enoch",
                "kind": "route",
                "account_id": "enoch-account",
                "label": "Codex local - enoch@example.com",
                "model": "gpt-test",
            },
            {
                "when": first_boundary + timedelta(milliseconds=429),
                "request_id": "request-hyenas",
                "kind": "route",
                "account_id": "hyenas-account",
                "label": "Codex local - hyenas@example.com",
                "model": "gpt-test",
            },
        ]

        hints = monitor._match_live_cockpit_route_hints(
            [first_boundary, second_boundary],
            events,
        )

        self.assertEqual(hints[0]["label"], "Codex local - enoch@example.com")
        self.assertEqual(hints[0]["request_id"], "request-enoch")
        self.assertEqual(hints[1]["label"], "Codex local - hyenas@example.com")
        self.assertEqual(hints[1]["request_id"], "request-hyenas")

    def test_live_route_hint_is_cleared_when_request_completes(self) -> None:
        boundary_at = datetime(2026, 7, 24, 7, 0, tzinfo=timezone.utc)
        events = [
            {
                "when": boundary_at,
                "request_id": "completed-request",
                "kind": "started",
                "account_id": "",
                "label": "",
                "model": "gpt-test",
            },
            {
                "when": boundary_at + timedelta(milliseconds=100),
                "request_id": "completed-request",
                "kind": "route",
                "account_id": "old-account",
                "label": "Codex local - old@example.com",
                "model": "gpt-test",
            },
            {
                "when": boundary_at + timedelta(seconds=1),
                "request_id": "completed-request",
                "kind": "completed",
                "account_id": "",
                "label": "",
                "model": "gpt-test",
            },
        ]

        hints = monitor._match_live_cockpit_route_hints([boundary_at], events)

        self.assertEqual(hints[0]["source"], "active_route_ended")
        self.assertTrue(hints[0]["resolved"])
        self.assertFalse(hints[0]["label"])

    def test_api_service_long_turn_uses_route_after_latest_tool_output(self) -> None:
        now = datetime.now(timezone.utc)
        started_at = now - timedelta(minutes=10)
        boundary_at = now - timedelta(seconds=2)
        token_at = now - timedelta(seconds=1)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sessions = root / "sessions"
            self.write_live_rows(
                sessions,
                [
                    {"type": "session_meta", "payload": {"id": self.SESSION_ID}},
                    {
                        "timestamp": started_at.isoformat(),
                        "type": "event_msg",
                        "payload": {"type": "task_started", "turn_id": "long-turn"},
                    },
                    {
                        "timestamp": boundary_at.isoformat(),
                        "type": "response_item",
                        "payload": {
                            "type": "custom_tool_call_output",
                            "call_id": "call-1",
                            "output": "done",
                        },
                    },
                    {
                        "timestamp": token_at.isoformat(),
                        "type": "event_msg",
                        "payload": {
                            "type": "token_count",
                            "info": {
                                "last_token_usage": {
                                    "input_tokens": 20_000,
                                    "cached_input_tokens": 18_000,
                                    "output_tokens": 700,
                                    "total_tokens": 20_700,
                                }
                            },
                        },
                    },
                ],
            )
            (root / "codex_accounts.json").write_text(
                json.dumps(
                    {
                        "accounts": [
                            {"id": "old-account", "email": "old@example.com"},
                            {"id": "new-account", "email": "new@example.com"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            logs = root / "logs"
            logs.mkdir()
            (logs / "codex-api.log.test").write_text(
                (
                    f'{(started_at + timedelta(milliseconds=100)).isoformat()} WARN '
                    'msg="session-affinity: cache hit | session=test '
                    'auth=old-account.json provider=mixed model=gpt-test" '
                    'request_id=old-request\n'
                    f'{(boundary_at + timedelta(milliseconds=100)).isoformat()} WARN '
                    'msg="session-affinity: cache hit | session=test '
                    'auth=new-account.json provider=mixed model=gpt-test" '
                    'request_id=new-request\n'
                ),
                encoding="utf-8",
            )
            cached = [
                {
                    "session_id": self.SESSION_ID,
                    "provider": "Codex local - old@example.com",
                    "provider_confirmed": True,
                    "turn_started_at": started_at.isoformat(),
                }
            ]
            with patch.object(
                monitor,
                "_current_codex_account_label",
                return_value="Codex local - api-service-local",
            ):
                rows = monitor.scan_live_codex_active_sessions(
                    sessions,
                    cached,
                    now=now,
                    cockpit_db_path=root / "requests.sqlite",
                )

        self.assertEqual(rows[0]["provider"], "Codex local - new@example.com")
        self.assertEqual(rows[0]["provider_route_request_id"], "new-request")
        self.assertEqual(rows[0]["request_boundary_at"], boundary_at.isoformat())
        self.assertFalse(rows[0]["provider_confirmed"])
        self.assertTrue(rows[0]["provider_provisional"])

    def test_api_service_session_keeps_confirmed_provider_within_same_turn(self) -> None:
        now = datetime.now(timezone.utc)
        started_at = now - timedelta(seconds=30)
        first_token_at = now - timedelta(seconds=20)
        latest_token_at = now - timedelta(seconds=1)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sessions = root / "sessions"
            db_path = root / "requests.sqlite"
            path = self.write_live_rows(
                sessions,
                [
                    {"type": "session_meta", "payload": {"id": self.SESSION_ID}},
                    {
                        "timestamp": started_at.isoformat(),
                        "type": "event_msg",
                        "payload": {"type": "task_started", "turn_id": "turn-1"},
                    },
                    {
                        "timestamp": first_token_at.isoformat(),
                        "type": "event_msg",
                        "payload": {
                            "type": "token_count",
                            "info": {
                                "last_token_usage": {
                                    "input_tokens": 10_000,
                                    "cached_input_tokens": 8_000,
                                    "output_tokens": 500,
                                    "total_tokens": 10_500,
                                }
                            },
                        },
                    },
                ],
            )
            self.write_cockpit_marker(
                db_path,
                first_token_at + timedelta(milliseconds=150),
                email="routed@example.com",
                input_tokens=10_000,
                cached_tokens=8_000,
                output_tokens=500,
            )
            with patch.object(
                monitor,
                "_current_codex_account_label",
                return_value="Codex local - api-service-local",
            ):
                confirmed = monitor.scan_live_codex_active_sessions(
                    sessions,
                    [],
                    now=now,
                    cockpit_db_path=db_path,
                )
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(
                        json.dumps(
                            {
                                "timestamp": latest_token_at.isoformat(),
                                "type": "event_msg",
                                "payload": {
                                    "type": "token_count",
                                    "info": {
                                        "last_token_usage": {
                                            "input_tokens": 20_000,
                                            "cached_input_tokens": 18_000,
                                            "output_tokens": 700,
                                            "total_tokens": 20_700,
                                        }
                                    },
                                },
                            }
                        )
                        + "\n"
                    )
                carried = monitor.scan_live_codex_active_sessions(
                    sessions,
                    confirmed,
                    now=now,
                    cockpit_db_path=db_path,
                )

        self.assertEqual(confirmed[0]["provider"], "Codex local - routed@example.com")
        self.assertEqual(carried[0]["provider"], "Codex local - routed@example.com")
        self.assertTrue(carried[0]["provider_confirmed"])
        self.assertEqual(
            carried[0]["provider_confirmation_source"],
            "same_turn_final_anchor",
        )
        self.assertEqual(
            carried[0]["usage_event_id"],
            monitor._live_usage_event_id(
                latest_token_at,
                self.SESSION_ID,
                20_000,
                18_000,
                700,
            ),
        )

    def test_api_service_session_keeps_same_turn_cached_account_for_display(self) -> None:
        now = datetime.now(timezone.utc)
        started_at = now - timedelta(seconds=30)
        token_at = now - timedelta(seconds=1)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sessions = root / "sessions"
            self.write_live_rows(
                sessions,
                [
                    {"type": "session_meta", "payload": {"id": self.SESSION_ID}},
                    {
                        "timestamp": started_at.isoformat(),
                        "type": "event_msg",
                        "payload": {"type": "task_started", "turn_id": "turn-1"},
                    },
                    {
                        "timestamp": token_at.isoformat(),
                        "type": "event_msg",
                        "payload": {
                            "type": "token_count",
                            "info": {
                                "last_token_usage": {
                                    "input_tokens": 20_000,
                                    "cached_input_tokens": 18_000,
                                    "output_tokens": 700,
                                    "total_tokens": 20_700,
                                }
                            },
                        },
                    },
                ],
            )
            cached = [
                {
                    "session_id": self.SESSION_ID,
                    "provider": "Codex local - plus@example.com",
                    "started_at": started_at.isoformat(),
                }
            ]
            with patch.object(
                monitor,
                "_current_codex_account_label",
                return_value="Codex local - api-service-local",
            ):
                rows = monitor.scan_live_codex_active_sessions(
                    sessions,
                    cached,
                    now=now,
                    cockpit_db_path=root / "missing.sqlite",
                )

        self.assertEqual(rows[0]["provider"], "Codex local - plus@example.com")
        self.assertFalse(rows[0]["provider_confirmed"])
        self.assertTrue(rows[0]["provider_provisional"])
        self.assertEqual(
            rows[0]["provider_confirmation_source"],
            "same_turn_cached_account",
        )

    def test_api_service_session_does_not_carry_provider_into_new_turn(self) -> None:
        now = datetime.now(timezone.utc)
        new_started_at = now - timedelta(seconds=2)
        token_at = now - timedelta(seconds=1)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sessions = root / "sessions"
            self.write_live_rows(
                sessions,
                [
                    {"type": "session_meta", "payload": {"id": self.SESSION_ID}},
                    {
                        "timestamp": new_started_at.isoformat(),
                        "type": "event_msg",
                        "payload": {"type": "task_started", "turn_id": "turn-new"},
                    },
                    {
                        "timestamp": token_at.isoformat(),
                        "type": "event_msg",
                        "payload": {
                            "type": "token_count",
                            "info": {
                                "last_token_usage": {
                                    "input_tokens": 20_000,
                                    "cached_input_tokens": 18_000,
                                    "output_tokens": 700,
                                    "total_tokens": 20_700,
                                }
                            },
                        },
                    },
                ],
            )
            cached = [
                {
                    "session_id": self.SESSION_ID,
                    "provider": "Codex local - previous@example.com",
                    "provider_confirmed": True,
                    "turn_started_at": (now - timedelta(minutes=5)).isoformat(),
                }
            ]
            with patch.object(
                monitor,
                "_current_codex_account_label",
                return_value="Codex local - api-service-local",
            ):
                rows = monitor.scan_live_codex_active_sessions(
                    sessions,
                    cached,
                    now=now,
                    cockpit_db_path=root / "missing.sqlite",
                )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["provider"], "API 服务 · 正在确认账号")
        self.assertFalse(rows[0]["provider_confirmed"])
        self.assertEqual(rows[0]["provider_confirmation_source"], "")

    def test_api_service_same_turn_carry_uses_each_sessions_own_usage(self) -> None:
        now = datetime.now(timezone.utc)
        started_at = now - timedelta(seconds=2)
        other_session_id = "11111111-2222-3333-4444-555555555555"
        with tempfile.TemporaryDirectory() as directory:
            sessions = Path(directory) / "sessions"
            sessions.mkdir(parents=True)
            waiting_path = sessions / f"rollout-current-{self.SESSION_ID}.jsonl"
            waiting_path.write_text(
                "\n".join(
                    json.dumps(row)
                    for row in (
                        {"type": "session_meta", "payload": {"id": self.SESSION_ID}},
                        {
                            "timestamp": started_at.isoformat(),
                            "type": "event_msg",
                            "payload": {"type": "task_started", "turn_id": "turn-a"},
                        },
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            active_path = sessions / f"rollout-other-{other_session_id}.jsonl"
            active_path.write_text(
                "\n".join(
                    json.dumps(row)
                    for row in (
                        {"type": "session_meta", "payload": {"id": other_session_id}},
                        {
                            "timestamp": started_at.isoformat(),
                            "type": "event_msg",
                            "payload": {"type": "task_started", "turn_id": "turn-b"},
                        },
                        {
                            "timestamp": (now - timedelta(seconds=1)).isoformat(),
                            "type": "event_msg",
                            "payload": {
                                "type": "token_count",
                                "info": {
                                    "last_token_usage": {
                                        "input_tokens": 1_000,
                                        "cached_input_tokens": 800,
                                        "output_tokens": 50,
                                        "total_tokens": 1_050,
                                    }
                                },
                            },
                        },
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            os.utime(active_path, (now.timestamp() - 1, now.timestamp() - 1))
            os.utime(waiting_path, (now.timestamp(), now.timestamp()))
            cached = [
                {
                    "session_id": self.SESSION_ID,
                    "provider": "Codex local - confirmed@example.com",
                    "provider_confirmed": True,
                    "turn_started_at": started_at.isoformat(),
                }
            ]
            with patch.object(
                monitor,
                "_current_codex_account_label",
                return_value="Codex local - api-service-local",
            ):
                rows = monitor.scan_live_codex_active_sessions(
                    sessions,
                    cached,
                    now=now,
                    cockpit_db_path=Path(directory) / "missing.sqlite",
                )

        by_session = {row["session_id"]: row for row in rows}
        waiting = by_session[self.SESSION_ID]
        self.assertEqual(waiting["provider"], "API 服务 · 等待首个响应")
        self.assertFalse(waiting["provider_confirmed"])

    def test_api_service_session_uses_near_time_marker_when_token_totals_differ(self) -> None:
        now = datetime.now(timezone.utc)
        started_at = now - timedelta(seconds=2)
        token_at = now - timedelta(seconds=1)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db_path = root / "requests.sqlite"
            self.write_live_rows(
                root / "sessions",
                [
                    {"type": "session_meta", "payload": {"id": self.SESSION_ID}},
                    {
                        "timestamp": started_at.isoformat(),
                        "type": "event_msg",
                        "payload": {"type": "task_started", "turn_id": "turn-1"},
                    },
                    {
                        "timestamp": token_at.isoformat(),
                        "type": "event_msg",
                        "payload": {
                            "type": "token_count",
                            "info": {
                                "last_token_usage": {
                                    "input_tokens": 123_000,
                                    "cached_input_tokens": 100_000,
                                    "output_tokens": 1_305,
                                    "total_tokens": 124_305,
                                }
                            },
                        },
                    },
                ],
            )
            self.write_cockpit_marker(
                db_path,
                token_at + timedelta(milliseconds=150),
                email="different-totals@example.com",
                input_tokens=8_000,
                cached_tokens=7_000,
                output_tokens=786,
            )
            with patch.object(
                monitor,
                "_current_codex_account_label",
                return_value="Codex local - api-service-local",
            ):
                rows = monitor.scan_live_codex_active_sessions(
                    root / "sessions",
                    [],
                    now=now,
                    cockpit_db_path=db_path,
                )

        self.assertEqual(len(rows), 1)
        self.assertEqual(
            rows[0]["provider"],
            "Codex local - different-totals@example.com",
        )

    def test_api_service_session_does_not_reuse_cached_account_without_current_turn_evidence(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_live_rows(
                root / "sessions",
                [
                    {"type": "session_meta", "payload": {"id": self.SESSION_ID}},
                    {
                        "timestamp": (now - timedelta(seconds=1)).isoformat(),
                        "type": "event_msg",
                        "payload": {"type": "task_started", "turn_id": "turn-new"},
                    },
                ],
            )
            with patch.object(
                monitor,
                "_current_codex_account_label",
                return_value="Codex local - api-service-local",
            ):
                rows = monitor.scan_live_codex_active_sessions(
                    root / "sessions",
                    [
                        {
                            "session_id": self.SESSION_ID,
                            "provider": "Codex local - stale-k12@example.com",
                        }
                    ],
                    now=now,
                    cockpit_db_path=root / "missing.sqlite",
                )

        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["provider"].startswith("API 服务"))

    def test_live_near_time_marker_stays_unmatched_for_ambiguous_sessions(self) -> None:
        base = datetime(2026, 7, 23, 2, 0, 0, tzinfo=timezone.utc)
        usages = [
            {
                "when": base + timedelta(milliseconds=offset),
                "session_id": f"session-{offset}",
                "total_tokens": 100_000 + offset,
                "input_tokens": 99_000,
                "cached_tokens": 90_000,
                "output_tokens": 1_000,
            }
            for offset in (0, 100)
        ]
        marker = {
            "when": base + timedelta(milliseconds=50),
            "label": "Codex local - ambiguous@example.com",
            "total_tokens": 8_786,
            "input_tokens": 8_000,
            "cached_tokens": 7_000,
            "output_tokens": 786,
        }

        matches = monitor._match_live_cockpit_markers(usages, [marker])

        self.assertEqual(matches, {})

    def test_live_exact_marker_uses_explicit_request_latency_interval(self) -> None:
        event_when = datetime(2026, 7, 23, 10, 57, 43, tzinfo=timezone.utc)
        usage = {
            "when": event_when,
            "session_id": "long-response-session",
            "total_tokens": 77_524,
            "input_tokens": 76_752,
            "cached_tokens": 10_624,
            "output_tokens": 772,
        }
        marker = {
            "when": event_when + timedelta(seconds=360),
            "label": "Codex local - final@example.com",
            "total_tokens": 77_524,
            "input_tokens": 76_752,
            "cached_tokens": 10_624,
            "output_tokens": 772,
            "latency_ms": 360_500,
        }

        self.assertIs(
            monitor._match_live_cockpit_marker(usage, [marker]),
            marker,
        )

    def test_live_exact_marker_accepts_request_start_timestamp(self) -> None:
        event_when = datetime(2026, 7, 29, 8, 36, 30, 118000, tzinfo=timezone.utc)
        usage = {
            "when": event_when,
            "session_id": "request-start-session",
            "total_tokens": 205_702,
            "input_tokens": 1_127,
            "cached_tokens": 204_544,
            "output_tokens": 31,
        }
        marker = {
            "when": event_when - timedelta(seconds=300.826),
            "label": "Codex local - final@example.com",
            "total_tokens": 205_702,
            "input_tokens": 1_127,
            "cached_tokens": 204_544,
            "output_tokens": 31,
            "latency_ms": 309_565,
        }

        self.assertIs(
            monitor._match_live_cockpit_marker(usage, [marker]),
            marker,
        )

    def test_cockpit_usage_revision_ignores_zero_usage_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "requests.sqlite"
            connection = sqlite3.connect(database)
            connection.execute(
                """
                CREATE TABLE request_logs (
                    timestamp INTEGER,
                    account_id TEXT,
                    email TEXT,
                    api_key_label TEXT,
                    total_tokens INTEGER,
                    input_tokens INTEGER,
                    cached_tokens INTEGER,
                    output_tokens INTEGER
                )
                """
            )
            connection.execute(
                "INSERT INTO request_logs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (1_000, "account-1", "final@example.com", "", 100, 90, 50, 10),
            )
            connection.commit()
            first = monitor._latest_cockpit_usage_revision(database)
            connection.execute(
                "INSERT INTO request_logs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (2_000, "failed-k12", "k12@example.com", "", 0, 0, 0, 0),
            )
            connection.commit()
            second = monitor._latest_cockpit_usage_revision(database)
            connection.close()

        self.assertEqual(second, first)

    def test_api_service_session_does_not_match_token_before_current_turn(self) -> None:
        now = datetime.now(timezone.utc)
        token_at = now - timedelta(seconds=3)
        started_at = now - timedelta(seconds=1)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db_path = root / "requests.sqlite"
            self.write_live_rows(
                root / "sessions",
                [
                    {"type": "session_meta", "payload": {"id": self.SESSION_ID}},
                    {
                        "timestamp": token_at.isoformat(),
                        "type": "event_msg",
                        "payload": {
                            "type": "token_count",
                            "info": {
                                "last_token_usage": {
                                    "input_tokens": 9_000,
                                    "cached_input_tokens": 8_000,
                                    "output_tokens": 400,
                                    "total_tokens": 9_400,
                                }
                            },
                        },
                    },
                    {
                        "timestamp": started_at.isoformat(),
                        "type": "event_msg",
                        "payload": {"type": "task_started", "turn_id": "turn-2"},
                    },
                ],
            )
            self.write_cockpit_marker(
                db_path,
                token_at,
                email="previous@example.com",
                input_tokens=9_000,
                cached_tokens=8_000,
                output_tokens=400,
            )
            with patch.object(
                monitor,
                "_current_codex_account_label",
                return_value="Codex local - api-service-local",
            ):
                rows = monitor.scan_live_codex_active_sessions(
                    root / "sessions",
                    [],
                    now=now,
                    cockpit_db_path=db_path,
                )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["provider"], "API 服务 · 等待首个响应")


class ActiveSessionLifecycleTests(unittest.TestCase):
    def test_active_unconfirmed_cockpit_turn_does_not_fallback_to_old_account(self) -> None:
        now = datetime(2026, 7, 12, 14, 0, 0)
        event = client_usage_export.UsageEvent(
            when=now - timedelta(seconds=1),
            model="gpt-test",
            input_tokens=100,
            cached_tokens=0,
            output_tokens=10,
            session_id="session-1",
        )
        lifecycle = client_usage_export.SessionLifecycle(
            session_id="session-1",
            state="task_started",
            when=now - timedelta(minutes=1),
            file_activity_at=now - timedelta(seconds=1),
        )

        rows, active_by_label, sessions_by_label, unresolved = (
            client_usage_export.build_active_session_rows(
                {client_usage_export.API_SERVICE_AGGREGATE_LABEL: [event]},
                {"session-1": "Codex local - stale-k12@example.com"},
                {"session-1": lifecycle},
                "Codex local - stale-k12@example.com",
                now,
                api_service_routed=True,
            )
        )

        self.assertEqual(rows[0]["provider"], "")
        self.assertEqual(active_by_label, {})
        self.assertEqual(sessions_by_label, {})
        self.assertEqual(unresolved, 1)

    def test_affinity_only_cockpit_evidence_disables_old_session_fallback(self) -> None:
        now = datetime(2026, 7, 12, 14, 0, 0)
        event = client_usage_export.UsageEvent(
            when=now - timedelta(seconds=1),
            model="gpt-test",
            input_tokens=100,
            cached_tokens=0,
            output_tokens=10,
            session_id="session-1",
        )
        lifecycle = client_usage_export.SessionLifecycle(
            session_id="session-1",
            state="task_started",
            when=now - timedelta(minutes=1),
            file_activity_at=now - timedelta(seconds=1),
        )

        rows, active_by_label, sessions_by_label, unresolved = (
            client_usage_export.build_active_session_rows(
                {client_usage_export.API_SERVICE_AGGREGATE_LABEL: [event]},
                {"session-1": "Codex local - stale-k12@example.com"},
                {"session-1": lifecycle},
                "Codex local - stale-k12@example.com",
                now,
                api_service_routed=True,
            )
        )

        self.assertEqual(rows[0]["provider"], "")
        self.assertEqual(active_by_label, {})
        self.assertEqual(sessions_by_label, {})
        self.assertEqual(unresolved, 1)

    def test_running_lifecycle_wins_over_old_token_activity(self) -> None:
        now = datetime(2026, 7, 12, 14, 0, 0)
        label = "Codex local - account@example.com"
        event = client_usage_export.UsageEvent(
            when=now - timedelta(minutes=20),
            model="gpt-test",
            input_tokens=100,
            cached_tokens=200,
            output_tokens=10,
            session_id="session-1",
        )
        lifecycle = client_usage_export.SessionLifecycle(
            session_id="session-1",
            state="task_started",
            when=now - timedelta(minutes=15),
            file_activity_at=now - timedelta(seconds=1),
        )

        rows, active_by_label, sessions_by_label, unresolved = (
            client_usage_export.build_active_session_rows(
                {label: [event]},
                {"session-1": label},
                {"session-1": lifecycle},
                label,
                now,
            )
        )

        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["active"])
        self.assertEqual(rows[0]["activity_source"], "task-lifecycle")
        self.assertEqual(active_by_label[label], 1)
        self.assertEqual(sessions_by_label[label], 1)
        self.assertEqual(unresolved, 0)

    def test_completed_lifecycle_suppresses_recent_token_fallback(self) -> None:
        now = datetime(2026, 7, 12, 14, 0, 0)
        label = "Codex local - account@example.com"
        event = client_usage_export.UsageEvent(
            when=now - timedelta(seconds=1),
            model="gpt-test",
            input_tokens=100,
            cached_tokens=200,
            output_tokens=10,
            session_id="session-1",
        )
        lifecycle = client_usage_export.SessionLifecycle(
            session_id="session-1",
            state="task_complete",
            when=now,
            file_activity_at=now,
        )

        rows, active_by_label, sessions_by_label, unresolved = (
            client_usage_export.build_active_session_rows(
                {label: [event]},
                {"session-1": label},
                {"session-1": lifecycle},
                label,
                now,
            )
        )

        self.assertEqual(rows, [])
        self.assertEqual(active_by_label, {})
        self.assertEqual(sessions_by_label, {})
        self.assertEqual(unresolved, 0)

    def test_latest_session_event_label_cannot_be_overwritten_by_older_provider_group(self) -> None:
        now = datetime(2026, 7, 12, 20, 0, 0)
        latest_label = "Codex local - hails@example.com"
        older_label = "Codex local - ginny@example.com"
        latest_event = client_usage_export.UsageEvent(
            when=now - timedelta(seconds=1),
            model="gpt-test",
            input_tokens=100,
            cached_tokens=200,
            output_tokens=10,
            session_id="session-1",
        )
        older_event = client_usage_export.UsageEvent(
            when=now - timedelta(minutes=1),
            model="gpt-test",
            input_tokens=90,
            cached_tokens=180,
            output_tokens=9,
            session_id="session-1",
        )
        lifecycle = client_usage_export.SessionLifecycle(
            session_id="session-1",
            state="task_started",
            when=now - timedelta(minutes=2),
            file_activity_at=now,
        )

        rows, active_by_label, _sessions_by_label, unresolved = (
            client_usage_export.build_active_session_rows(
                {
                    latest_label: [latest_event],
                    older_label: [older_event],
                },
                {"session-1": older_label},
                {"session-1": lifecycle},
                older_label,
                now,
            )
        )

        self.assertEqual(rows[0]["provider"], latest_label)
        self.assertEqual(active_by_label, {latest_label: 1})
        self.assertEqual(unresolved, 0)

    def test_scanner_keeps_latest_task_lifecycle_event(self) -> None:
        start = datetime(2026, 7, 12, 0, 0, 0)
        end = start + timedelta(days=1)
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            day_dir = root / "2026" / "07" / "12"
            day_dir.mkdir(parents=True)
            path = day_dir / "session.jsonl"
            rows = [
                {
                    "timestamp": "2026-07-12T05:00:00Z",
                    "type": "session_meta",
                    "payload": {"id": "session-1"},
                },
                {
                    "timestamp": "2026-07-12T05:01:00Z",
                    "type": "event_msg",
                    "payload": {"type": "task_started", "turn_id": "turn-1"},
                },
                {
                    "timestamp": "2026-07-12T05:02:00Z",
                    "type": "event_msg",
                    "payload": {"type": "turn_aborted", "turn_id": "turn-1"},
                },
            ]
            path.write_text(
                "\n".join(json.dumps(row) for row in rows) + "\n",
                encoding="utf-8",
            )
            lifecycle: dict[str, client_usage_export.SessionLifecycle] = {}

            client_usage_export.scan_codex_events(
                root,
                start,
                end,
                session_lifecycle=lifecycle,
            )

        self.assertEqual(lifecycle["session-1"].state, "turn_aborted")
        self.assertEqual(lifecycle["session-1"].turn_id, "turn-1")


class ManualRefreshTests(unittest.TestCase):
    def test_manual_refresh_is_queued_while_refresh_is_running(self) -> None:
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        app._refresh_lock = threading.Lock()
        app._refresh_lock.acquire()
        app._refresh_pending = False
        app._refresh_pending_force = False
        app._refresh_pending_usage = False
        app._draw = lambda: None

        started = app.refresh_async(force=True)

        self.assertFalse(started)
        self.assertTrue(app._refresh_pending)
        app._refresh_lock.release()

    def test_manual_refresh_clears_all_runtime_caches(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.clear_calls = 0

            def clear_runtime_caches(self) -> None:
                self.clear_calls += 1

        class FakeThread:
            def __init__(self, target, daemon: bool) -> None:
                self.target = target
                self.daemon = daemon

            def start(self) -> None:
                return

        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        app._refresh_lock = threading.Lock()
        app._refresh_pending = False
        app._refresh_pending_force = False
        app._refresh_pending_usage = False
        app._loading = False
        app.client = FakeClient()
        app._draw = lambda: None
        app._pulse_tick = lambda: None

        with patch.object(monitor.threading, "Thread", FakeThread):
            started = app.refresh_async(force=True)

        self.assertTrue(started)
        self.assertEqual(app.client.clear_calls, 1)
        app._refresh_lock.release()

    def test_manual_refresh_waits_for_live_catchup_export(self) -> None:
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        app._refresh_lock = threading.Lock()
        app._live_catchup_lock = threading.Lock()
        app._live_catchup_lock.acquire()
        app._refresh_pending = False
        app._draw = lambda: None

        started = app.refresh_async(force=True)

        self.assertFalse(started)
        self.assertTrue(app._refresh_pending)
        self.assertFalse(app._refresh_lock.locked())
        app._live_catchup_lock.release()


class LiveUsageOverlayTests(unittest.TestCase):
    def setUp(self) -> None:
        self._append_attribution_diagnostic = monitor.append_attribution_diagnostic
        diagnostic_patch = patch.object(
            monitor,
            "append_attribution_diagnostic",
            return_value=True,
        )
        diagnostic_patch.start()
        self.addCleanup(diagnostic_patch.stop)

    @staticmethod
    def state(tokens: int = 100, requests: int = 2, *, fresh: bool = False) -> monitor.MonitorState:
        hour = datetime.now(monitor.CN_TZ).hour
        client_usage = {
            "tokens": tokens,
            "requests": requests,
            "input_tokens": 60,
            "cached_input_tokens": 30,
            "output_tokens": 10,
            "dashboard": {
                "hourly_today": [
                    {"hour": value, "tokens": tokens if value == hour else 0, "requests": requests if value == hour else 0}
                    for value in range(24)
                ]
            },
        }
        return monitor.MonitorState(
            loading=False,
            updated_at=0.0,
            mode="local",
            usage_source="local",
            today_requests=requests,
            today_tokens=tokens,
            client_usage=client_usage,
            usage_sync={"fresh": fresh},
            latest_request={},
        )

    @staticmethod
    def event() -> dict:
        return {
            "when": datetime.now(timezone.utc),
            "total_tokens": 50,
            "input_tokens": 40,
            "cached_tokens": 20,
            "output_tokens": 10,
        }

    @staticmethod
    def events_with_total(total_tokens: int) -> list[dict]:
        events: list[dict] = []
        remaining = max(0, int(total_tokens))
        now = datetime.now(timezone.utc)
        while remaining > 0:
            amount = min(monitor.LIVE_USAGE_MAX_SINGLE_EVENT_TOKENS, remaining)
            index = len(events)
            events.append(
                {
                    "when": now - timedelta(milliseconds=index),
                    "event_id": (
                        f"large-live-event-{index}-{int(now.timestamp() * 1_000_000)}-"
                        f"{total_tokens}"
                    ),
                    "total_tokens": amount,
                    "input_tokens": amount,
                    "cached_tokens": 0,
                    "output_tokens": 0,
                }
            )
            remaining -= amount
        return events

    def test_live_event_updates_only_top_level_totals_immediately(self) -> None:
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        app.state = self.state()
        app._live_usage_overlay = None
        original_client_usage = copy.deepcopy(app.state.client_usage)

        self.assertTrue(app._record_live_usage_events([self.event()]))

        self.assertEqual(app.state.today_tokens, 150)
        self.assertEqual(app.state.today_requests, 3)
        self.assertEqual(app.state.client_usage, original_client_usage)

    def test_live_overlay_clears_timeout_presentation_only_when_it_covers_cache(self) -> None:
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        app.state = self.state()
        latest = datetime.now(timezone.utc)
        app.state.usage_sync = {
            "state": "timeout",
            "fresh": False,
            "cache_used": True,
        }
        app.state.client_usage["scan_status"] = {
            "through": (latest - timedelta(minutes=1)).isoformat(),
        }
        app._live_usage_overlay = {
            "base_today_tokens": 100,
            "base_today_requests": 2,
            "base_today_cost": 0.0,
            "tokens": 50,
            "requests": 1,
            "cost": 0.5,
            "input_tokens": 30,
            "cached_input_tokens": 10,
            "output_tokens": 10,
            "latest_when": latest,
            "providers": {},
            "base_hourly": app._live_hourly_snapshot(),
            "hourly": {},
        }

        app._apply_live_usage_overlay(app.state)

        self.assertTrue(
            app.state.usage_sync["live_overlay_covers_cache"]
        )
        self.assertEqual(
            app.state.usage_sync["live_overlay_latest_at"],
            latest.astimezone(monitor.CN_TZ).isoformat(timespec="seconds"),
        )
        self.assertEqual(
            monitor.usage_sync_label(app.state.usage_sync),
            "\u5b9e\u65f6\u7edf\u8ba1\u4e2d / \u5168\u91cf\u6838\u5bf9\u6392\u961f",
        )

    def test_live_overlay_does_not_hide_timeout_before_cached_cutoff(self) -> None:
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        app.state = self.state()
        latest = datetime.now(timezone.utc)
        app.state.usage_sync = {
            "state": "timeout",
            "fresh": False,
            "cache_used": True,
        }
        app.state.client_usage["scan_status"] = {
            "through": latest.isoformat(),
        }
        app._live_usage_overlay = {
            "base_today_tokens": 100,
            "base_today_requests": 2,
            "base_today_cost": 0.0,
            "tokens": 50,
            "requests": 1,
            "cost": 0.5,
            "input_tokens": 30,
            "cached_input_tokens": 10,
            "output_tokens": 10,
            "latest_when": latest - timedelta(minutes=2),
            "providers": {},
            "base_hourly": app._live_hourly_snapshot(),
            "hourly": {},
        }

        app._apply_live_usage_overlay(app.state)

        self.assertNotIn(
            "live_overlay_covers_cache",
            app.state.usage_sync,
        )
        self.assertEqual(
            monitor.usage_sync_label(app.state.usage_sync),
            "\u8865\u5f55\u8d85\u65f6 / \u663e\u793a\u4e0a\u6b21\u6570\u636e",
        )

    def test_live_event_updates_its_hourly_bucket_immediately(self) -> None:
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        app.state = self.state()
        app._live_usage_overlay = None
        event = self.event()

        self.assertTrue(app._record_live_usage_events([event]))

        summary = app._usage_range_summary("24h")
        hour = event["when"].astimezone(monitor.CN_TZ).hour
        bucket = next(row for row in summary["series"] if row["hour"] == hour)
        self.assertEqual(bucket["tokens"], 150)
        self.assertEqual(bucket["requests"], 3)

    def test_runtime_spike_waits_for_verification_before_updating_total(self) -> None:
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        app.state = self.state()
        app._live_usage_overlay = None
        app._live_usage_seen_ids = {}
        app._live_usage_event_records = {}
        app._live_usage_rate_samples = []
        app._live_usage_verification_pending = False
        app._live_usage_verification_latest_when = None
        app._live_usage_verification_pending_tokens = 0
        events = self.events_with_total(monitor.LIVE_USAGE_VERIFY_THRESHOLD_TOKENS)

        self.assertFalse(app._record_live_usage_events(events, animate=False))

        self.assertEqual(app.state.today_tokens, 100)
        self.assertIsNone(app._live_usage_overlay)
        self.assertTrue(app._live_usage_verification_pending)
        self.assertEqual(
            app._live_usage_verification_pending_tokens,
            monitor.LIVE_USAGE_VERIFY_THRESHOLD_TOKENS,
        )
        self.assertEqual(app._live_usage_event_records, {})

    def test_runtime_spike_guard_accumulates_several_batches_in_its_time_window(self) -> None:
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        app.state = self.state()
        app._live_usage_overlay = None
        app._live_usage_seen_ids = {}
        app._live_usage_event_records = {}
        app._live_usage_rate_samples = []
        app._live_usage_verification_pending = False
        app._live_usage_verification_latest_when = None
        app._live_usage_verification_pending_tokens = 0
        first_total = monitor.LIVE_USAGE_VERIFY_THRESHOLD_TOKENS // 2
        final_batch = monitor.LIVE_USAGE_VERIFY_THRESHOLD_TOKENS - first_total
        first_events = self.events_with_total(first_total)
        final_events = self.events_with_total(final_batch)
        for event in final_events:
            event["event_id"] = f"final-{event['event_id']}"

        with patch.object(monitor, "_current_codex_account_label", return_value=""):
            self.assertTrue(
                app._record_live_usage_events(
                    first_events,
                    animate=False,
                )
            )
            self.assertFalse(
                app._record_live_usage_events(
                    final_events,
                    animate=False,
                )
            )

        self.assertEqual(app.state.today_tokens, 100 + first_total)
        self.assertTrue(app._live_usage_verification_pending)
        self.assertEqual(
            app._live_usage_verification_pending_tokens,
            monitor.LIVE_USAGE_VERIFY_THRESHOLD_TOKENS,
        )

    def test_startup_historical_load_is_not_blocked_by_runtime_spike_guard(self) -> None:
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        app.state = self.state()
        app._live_usage_overlay = None
        app._live_usage_seen_ids = {}
        app._live_usage_event_records = {}
        app._live_usage_rate_samples = []
        app._live_usage_verification_pending = False
        events = self.events_with_total(monitor.LIVE_USAGE_VERIFY_THRESHOLD_TOKENS)

        self.assertTrue(
            app._record_live_usage_events(
                events,
                allow_historical=True,
                animate=False,
            )
        )

        self.assertEqual(
            app.state.today_tokens,
            100 + monitor.LIVE_USAGE_VERIFY_THRESHOLD_TOKENS,
        )
        self.assertFalse(app._live_usage_verification_pending)

    def test_verified_cutoff_releases_runtime_spike_guard(self) -> None:
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        pending_latest = datetime.now(timezone.utc)
        app._live_usage_verification_pending = True
        app._live_usage_verification_latest_when = pending_latest
        app._live_usage_verification_pending_tokens = 12_000_000
        app._live_usage_rate_samples = [(100.0, 1_000_000)]

        self.assertFalse(
            app._complete_live_usage_verification(pending_latest - timedelta(microseconds=1))
        )
        self.assertTrue(app._live_usage_verification_pending)
        self.assertTrue(app._complete_live_usage_verification(pending_latest))
        self.assertFalse(app._live_usage_verification_pending)
        self.assertEqual(app._live_usage_verification_pending_tokens, 0)
        self.assertEqual(app._live_usage_rate_samples, [])

    def test_live_cockpit_marker_updates_matching_account_usage(self) -> None:
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        app.state = self.state(tokens=1_000, requests=10)
        app.state.client_usage["api_service_routed"] = True
        app.state.client_usage["providers"] = [
            {
                "name": "Codex local - routed@example.com",
                "tokens": 1_000,
                "requests": 10,
                "input_tokens": 400,
                "cached_input_tokens": 500,
                "output_tokens": 100,
            }
        ]
        app.state.top_accounts = [
            {
                "name": "routed@example.com",
                "tokens": 1_000,
                "requests": 10,
            }
        ]
        app._live_usage_overlay = None
        event = self.event()
        marker = {
            "when": event["when"],
            "label": "Codex local - routed@example.com",
            "model": "gpt-test",
            # Cockpit's final request row may contain a cumulative/final value;
            # it identifies the account but must not replace this model call's
            # own 50-token usage event.
            "total_tokens": 150,
            "input_tokens": 40,
            "cached_tokens": 20,
            "output_tokens": 10,
        }

        with (
            patch.object(
                monitor,
                "_current_codex_account_label",
                return_value="Codex local - api-service-local",
            ),
            patch.object(monitor, "_load_live_cockpit_markers", return_value=[marker]),
        ):
            app._record_live_usage_events([event])

        provider = app.state.client_usage["providers"][0]
        account = app.state.top_accounts[0]
        self.assertEqual((account["tokens"], account["requests"]), (1_050, 11))
        self.assertEqual((provider["tokens"], provider["requests"]), (1_050, 11))
        self.assertEqual(provider["input_tokens"], 420)
        self.assertEqual(provider["cached_input_tokens"], 520)
        self.assertEqual(provider["output_tokens"], 110)
        self.assertEqual(app.state.latest_account_name, "Codex local - routed@example.com")

    def test_same_cockpit_request_keeps_each_model_call(self) -> None:
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        app.state = self.state(tokens=0, requests=0)
        app.state.client_usage["api_service_routed"] = True
        app._live_usage_overlay = None
        app._live_usage_seen_ids = {}
        app._live_usage_event_records = {}

        base = datetime.now(timezone.utc)

        def snapshot(event_id: str, total: int, offset: int) -> dict:
            return {
                "event_id": event_id,
                "when": base + timedelta(seconds=offset),
                "request_key": "request-1",
                "route": "cockpit-request",
                "provider": "Codex local - account@example.com",
                "total_tokens": total,
                "input_tokens": total,
                "cached_tokens": 0,
                "output_tokens": 0,
                "cost": total / 1000,
            }

        with patch.object(
            monitor,
            "_current_codex_account_label",
            return_value="Codex local - api-service-local",
        ):
            self.assertTrue(
                app._record_live_usage_events(
                    [snapshot("snapshot-100", 100, 1)],
                    animate=False,
                    cockpit_markers=[],
                )
            )
            self.assertTrue(
                app._record_live_usage_events(
                    [snapshot("snapshot-150", 150, 2)],
                    animate=False,
                    cockpit_markers=[],
                )
            )
            self.assertTrue(
                app._record_live_usage_events(
                    [snapshot("snapshot-180", 180, 3)],
                    animate=False,
                    cockpit_markers=[],
                )
            )

        self.assertEqual(app.state.today_tokens, 430)
        self.assertEqual(app.state.today_requests, 3)
        provider = app._live_usage_overlay["providers"][
            "Codex local - account@example.com"
        ]
        self.assertEqual(provider["tokens"], 430)
        self.assertEqual(provider["requests"], 3)

    def test_different_cockpit_requests_remain_separate(self) -> None:
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        app.state = self.state(tokens=0, requests=0)
        app.state.client_usage["api_service_routed"] = True
        app._live_usage_overlay = None
        app._live_usage_seen_ids = {}
        app._live_usage_event_records = {}
        base = datetime.now(timezone.utc)
        events = [
            {
                "event_id": "request-a-event",
                "when": base + timedelta(seconds=1),
                "request_key": "request-a",
                "route": "cockpit-request",
                "provider": "Codex local - account@example.com",
                "total_tokens": 100,
                "input_tokens": 100,
                "cached_tokens": 0,
                "output_tokens": 0,
            },
            {
                "event_id": "request-b-event",
                "when": base + timedelta(seconds=2),
                "request_key": "request-b",
                "route": "cockpit-request",
                "provider": "Codex local - account@example.com",
                "total_tokens": 50,
                "input_tokens": 50,
                "cached_tokens": 0,
                "output_tokens": 0,
            },
        ]
        with patch.object(
            monitor,
            "_current_codex_account_label",
            return_value="Codex local - api-service-local",
        ):
            self.assertTrue(
                app._record_live_usage_events(
                    events,
                    animate=False,
                    cockpit_markers=[],
                )
            )

        self.assertEqual(app.state.today_tokens, 150)
        self.assertEqual(app.state.today_requests, 2)

    def test_repeated_live_event_id_is_still_deduplicated(self) -> None:
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        app.state = self.state(tokens=0, requests=0)
        app.state.client_usage["api_service_routed"] = True
        app._live_usage_overlay = None
        app._live_usage_seen_ids = {}
        app._live_usage_event_records = {}
        base = datetime.now(timezone.utc)

        def event(total: int, offset: int) -> dict:
            return {
                "event_id": "same-live-event",
                "when": base + timedelta(seconds=offset),
                "provider": "Codex local - account@example.com",
                "total_tokens": total,
                "input_tokens": total,
                "cached_tokens": 0,
                "output_tokens": 0,
            }

        with patch.object(
            monitor,
            "_current_codex_account_label",
            return_value="Codex local - api-service-local",
        ):
            app._record_live_usage_events(
                [event(100, 1)],
                animate=False,
                cockpit_markers=[],
            )
            app._record_live_usage_events(
                [event(80, 2)],
                animate=False,
                cockpit_markers=[],
            )

        self.assertEqual(app.state.today_tokens, 100)
        provider = app._live_usage_overlay["providers"][
            "Codex local - account@example.com"
        ]
        self.assertEqual(provider["tokens"], 100)

    def test_unconfirmed_api_service_turn_does_not_reuse_old_account_context(self) -> None:
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        app.state = self.state()
        app.state.client_usage["api_service_routed"] = True
        app.state.client_usage["active_sessions"] = [
            {
                "session_id": "current-session",
                "provider": "API \u670d\u52a1 \u00b7 \u7b49\u5f85\u9996\u4e2a\u54cd\u5e94",
                "model": "gpt-test",
            }
        ]
        app.state.active_accounts = [
            {
                "name": "Codex local - old-k12@example.com",
                "provider": "Codex local - old-k12@example.com",
                "model": "gpt-old",
            }
        ]
        app.state.latest_account_name = "Codex local - old-k12@example.com"

        with patch.object(
            monitor,
            "_current_codex_account_label",
            return_value="Codex local - api-service-local",
        ):
            provider, model = app._live_event_request_context(
                {"session_id": "current-session"}
            )

        self.assertEqual(provider, "")
        self.assertEqual(model, "gpt-test")

    def test_confirmed_api_service_turn_reuses_account_for_later_snapshot(self) -> None:
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        app.state = self.state()
        app.state.client_usage["api_service_routed"] = True
        turn_started_at = datetime.now(timezone.utc) - timedelta(seconds=10)
        app.state.client_usage["active_sessions"] = [
            {
                "session_id": "current-session",
                "provider": "Codex local - final@example.com",
                "provider_confirmed": True,
                "usage_event_id": "older-snapshot",
                "turn_started_at": turn_started_at.isoformat(),
            }
        ]

        with patch.object(
            monitor,
            "_current_codex_account_label",
            return_value="Codex local - api-service-local",
        ):
            provider, model = app._live_event_request_context(
                {
                    "event_id": "newer-snapshot",
                    "session_id": "current-session",
                    "when": turn_started_at + timedelta(seconds=9),
                    "model": "gpt-test",
                }
            )

        self.assertEqual(provider, "Codex local - final@example.com")
        self.assertEqual(model, "gpt-test")

    def test_unconfirmed_live_api_event_is_marked_pending_not_old_account(self) -> None:
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        app.state = self.state()
        app.state.client_usage["api_service_routed"] = True
        app.state.latest_account_name = "Codex local - old-k12@example.com"
        app._live_usage_overlay = None
        event = self.event()
        event["session_id"] = "current-session"

        with (
            patch.object(
                monitor,
                "_current_codex_account_label",
                return_value="Codex local - api-service-local",
            ),
            patch.object(monitor, "_load_live_cockpit_markers", return_value=[]),
        ):
            app._record_live_usage_events([event], animate=False)

        self.assertTrue(event["attribution_pending"])
        self.assertEqual(
            app.state.latest_account_name,
            "Codex local - api-service-local",
        )

    def test_pending_live_api_event_preserves_confirmed_recent_request(self) -> None:
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        app.state = self.state()
        app.state.client_usage["api_service_routed"] = True
        confirmed_at = datetime.now(monitor.CN_TZ) - timedelta(minutes=2)
        app.state.latest_account_name = "Codex local - confirmed@example.com"
        app.state.latest_request = {
            "kind": "success",
            "model": "gpt-confirmed",
            "created_at": confirmed_at.isoformat(timespec="seconds"),
        }
        app._live_usage_overlay = None
        app._live_usage_event_records = {}
        event = self.event()
        event.update({"event_id": "pending-event", "session_id": "session-new"})

        with (
            patch.object(
                monitor,
                "_current_codex_account_label",
                return_value="Codex local - api-service-local",
            ),
            patch.object(monitor, "_load_live_cockpit_markers", return_value=[]),
            patch.object(monitor, "append_attribution_diagnostic") as diagnostic,
        ):
            app._record_live_usage_events([event], animate=False)

        self.assertTrue(event["attribution_pending"])
        self.assertEqual(app._pending_latest_event_id, "pending-event")
        self.assertEqual(
            app.state.latest_account_name,
            "Codex local - confirmed@example.com",
        )
        self.assertEqual(app.state.latest_request["model"], "gpt-confirmed")
        self.assertEqual(app.state.latest_request["created_at"], confirmed_at.isoformat(timespec="seconds"))
        self.assertNotIn(
            "Codex local - confirmed@example.com",
            app._live_usage_overlay.get("providers", {}),
        )
        diagnostic.assert_called_once()
        self.assertEqual(diagnostic.call_args.args[0], "pending")
        self.assertTrue(diagnostic.call_args.kwargs["display_preserved"])
        self.assertEqual(
            diagnostic.call_args.kwargs["reason"],
            "no_final_usage_marker",
        )

    def test_api_service_context_does_not_reuse_previous_live_event_account(self) -> None:
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        app.state = self.state()
        app.state.client_usage["api_service_routed"] = True
        app.state.client_usage["active_sessions"] = [
            {
                "session_id": "shared-session",
                "provider": "Codex local - previous@example.com",
                "provider_confirmed": True,
                "usage_event_id": "previous-event",
                "model": "gpt-test",
            }
        ]

        with patch.object(
            monitor,
            "_current_codex_account_label",
            return_value="Codex local - api-service-local",
        ):
            provider, model = app._live_event_request_context(
                {
                    "session_id": "shared-session",
                    "event_id": "new-event",
                }
            )

        self.assertEqual(provider, "")
        self.assertEqual(model, "gpt-test")

    def test_direct_current_account_overrides_stale_session_account(self) -> None:
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        app.state = self.state()
        app.state.client_usage["active_sessions"] = [
            {
                "session_id": "older-session",
                "provider": "Codex local - hyenas@example.com",
                "model": "gpt-5.6-sol",
            }
        ]
        app.state.active_accounts = [
            {
                "provider": "Codex local - hyenas@example.com",
                "model": "gpt-5.6-sol",
            }
        ]

        with patch.object(
            monitor,
            "_current_codex_account_label",
            return_value="Codex local - pawns@example.com",
        ):
            provider, model = app._live_event_request_context(
                {
                    "session_id": "shared-session",
                    "event_id": "new-event",
                    "model": "gpt-5.6-sol",
                }
            )

        self.assertEqual(provider, "Codex local - pawns@example.com")
        self.assertEqual(model, "gpt-5.6-sol")

    def test_direct_placeholder_model_falls_back_to_latest_confirmed_model(self) -> None:
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        app.state = self.state()
        app.state.client_usage["active_sessions"] = [
            {
                "session_id": "current-session",
                "provider": "Codex local - pawns@example.com",
                "model": "-",
            }
        ]
        app.state.latest_request = {
            "provider": "Codex local - pawns@example.com",
            "model": "gpt-5.6-sol",
        }

        with patch.object(
            monitor,
            "_current_codex_account_label",
            return_value="Codex local - pawns@example.com",
        ):
            provider, model = app._live_event_request_context(
                {
                    "session_id": "current-session",
                    "event_id": "new-event",
                    "model": "-",
                }
            )

        self.assertEqual(provider, "Codex local - pawns@example.com")
        self.assertEqual(model, "gpt-5.6-sol")

    def test_direct_placeholder_model_is_priced_before_live_overlay(self) -> None:
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        app.state = self.state(tokens=0, requests=0)
        app.state.client_usage["active_sessions"] = [
            {
                "session_id": "current-session",
                "provider": "Codex local - pawns@example.com",
                "model": "-",
            }
        ]
        app.state.latest_request = {
            "provider": "Codex local - pawns@example.com",
            "model": "gpt-5.6-sol",
        }
        app._live_usage_overlay = None
        event = self.event()
        event.update(
            {
                "event_id": "placeholder-model-event",
                "session_id": "current-session",
                "model": "-",
            }
        )

        with (
            patch.object(
                monitor,
                "_current_codex_account_label",
                return_value="Codex local - pawns@example.com",
            ),
            patch.object(
                monitor,
                "estimate_live_usage_cost_with_resolution",
                return_value=(1.25, True),
            ) as estimate,
        ):
            self.assertTrue(
                app._record_live_usage_events(
                    [event],
                    animate=False,
                    cockpit_markers=[],
                )
            )

        estimate.assert_called_once_with(event, "gpt-5.6-sol")
        self.assertEqual(event["model"], "gpt-5.6-sol")
        self.assertEqual(event["unpriced_tokens"], 0)
        target = app._live_usage_overlay["providers"][
            "Codex local - pawns@example.com"
        ]
        self.assertEqual(target["models"], {"gpt-5.6-sol": 50})
        self.assertAlmostEqual(target["cost"], 1.25)

    def test_current_direct_account_leads_7d_after_live_activity_sync(self) -> None:
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        app.state = self.state()
        app.state.active_accounts = [
            {
                "provider": "Codex local - current@example.com",
                "current": 2,
            }
        ]
        app.state.latest_account_name = "Codex local - previous@example.com"
        app.state.latest_request = {
            "provider": "Codex local - previous@example.com",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        app.state.top_accounts = [
            {
                "name": "previous@example.com",
                "tokens": 500,
                "requests": 5,
                "latest_at": datetime.now(timezone.utc).isoformat(),
                "active_now": True,
                "is_latest": True,
            },
            {
                "name": "current@example.com",
                "tokens": 100,
                "requests": 1,
                "latest_at": (
                    datetime.now(timezone.utc) - timedelta(minutes=1)
                ).isoformat(),
                "active_now": False,
                "is_latest": False,
            },
        ]

        with patch.object(
            monitor,
            "_current_codex_account_label",
            return_value="Codex local - current@example.com",
        ):
            app._synchronize_account_activity_flags(app.state)

        rows = list(app.state.top_accounts)
        rows.sort(key=lambda row: monitor.account_usage_sort_key(row, "7d"))
        self.assertEqual(rows[0]["name"], "current@example.com")
        self.assertTrue(rows[0]["active_now"])
        self.assertTrue(rows[0]["is_latest"])
        self.assertFalse(rows[1]["active_now"])
        self.assertFalse(rows[1]["is_latest"])

    def test_idle_direct_login_is_latest_but_not_marked_active(self) -> None:
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        app.state = self.state()
        app.state.active_accounts = []
        app.state.top_accounts = [
            {
                "name": "current@example.com",
                "active_now": True,
                "is_latest": False,
            }
        ]

        with patch.object(
            monitor,
            "_current_codex_account_label",
            return_value="Codex local - current@example.com",
        ):
            app._synchronize_account_activity_flags(app.state)

        self.assertFalse(app.state.top_accounts[0]["active_now"])
        self.assertTrue(app.state.top_accounts[0]["is_latest"])

    def test_active_account_reuses_known_model_only_within_same_provider(self) -> None:
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        app.state = self.state()
        app.state.client_usage["providers"] = []
        app.state.top_accounts = []
        app._live_active_lock = threading.Lock()
        app._live_active_lock.acquire()
        app._observe_cockpit_usage_revision = MagicMock()
        app._reconcile_pending_live_events_with_markers = MagicMock(
            return_value=False
        )
        app._promote_pending_latest_account_from_sessions = MagicMock()
        app._draw = MagicMock()
        now = datetime.now(timezone.utc)
        sessions = [
            {
                "session_id": "new-placeholder",
                "provider": "Codex local - current@example.com",
                "model": "-",
                "latest_at": now.isoformat(),
                "active": True,
            },
            {
                "session_id": "known-model",
                "provider": "Codex local - current@example.com",
                "model": "gpt-5.6-sol",
                "latest_at": (now - timedelta(seconds=1)).isoformat(),
                "active": True,
            },
            {
                "session_id": "other-provider",
                "provider": "Codex local - other@example.com",
                "model": "gpt-other",
                "latest_at": now.isoformat(),
                "active": True,
            },
        ]

        with patch.object(
            monitor,
            "_current_codex_account_label",
            return_value="Codex local - current@example.com",
        ):
            app._apply_live_active_sessions(sessions)

        self.assertEqual(sessions[0]["model"], "gpt-5.6-sol")
        self.assertEqual(sessions[2]["model"], "gpt-other")
        current = next(
            row
            for row in app.state.active_accounts
            if "current@example.com" in str(row.get("provider") or "")
        )
        self.assertEqual(current["model"], "gpt-5.6-sol")
        self.assertFalse(app._live_active_lock.locked())

    def test_api_service_context_does_not_book_provisional_route_hint(self) -> None:
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        app.state = self.state()
        app.state.client_usage["api_service_routed"] = True
        app.state.client_usage["active_sessions"] = [
            {
                "session_id": "route-session",
                "provider": "Codex local - route-hint@example.com",
                "provider_confirmed": False,
                "provider_provisional": True,
                "provider_confirmation_source": "active_route_hint",
                "usage_event_id": "route-event",
                "model": "gpt-route",
            }
        ]

        with patch.object(
            monitor,
            "_current_codex_account_label",
            return_value="Codex local - api-service-local",
        ):
            provider, model = app._live_event_request_context(
                {
                    "session_id": "route-session",
                    "event_id": "route-event",
                }
            )

        self.assertEqual(provider, "")
        self.assertEqual(model, "gpt-route")

    def test_provisional_active_route_repairs_pending_request_display_only(self) -> None:
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        app.state = self.state()
        app.state.latest_account_name = "Codex local - api-service-local"
        event_when = datetime.now(timezone.utc)
        app.state.latest_request = {
            "event_id": "event-live",
            "session_id": "session-live",
            "provider": "Codex local - api-service-local",
            "model": "gpt-test",
        }
        pending_event = {
            "event_id": "event-live",
            "session_id": "session-live",
            "model": "gpt-test",
            "when": event_when,
            "attribution_pending": True,
        }
        app._pending_latest_event_id = "event-live"
        app._live_usage_event_records = {"event-live": pending_event}
        app._live_usage_overlay = {"providers": {}}
        provisional_session = {
            "session_id": "session-live",
            "usage_event_id": "older-cumulative-snapshot",
            "provider": "Codex local - routed@example.com",
            "provider_confirmed": False,
            "provider_provisional": True,
            "provider_confirmation_source": "active_route_hint",
            "turn_started_at": (event_when - timedelta(seconds=5)).isoformat(),
            "active": True,
        }

        with patch.object(monitor, "append_attribution_diagnostic") as diagnostic:
            changed = app._promote_pending_latest_account_from_sessions(
                [provisional_session]
            )

        self.assertTrue(changed)
        self.assertEqual(
            app.state.latest_request["provider"],
            "Codex local - routed@example.com",
        )
        self.assertTrue(app.state.latest_request["provider_provisional"])
        self.assertTrue(app.state.latest_request["attribution_pending"])
        self.assertEqual(
            app.state.latest_account_name,
            "Codex local - api-service-local",
        )
        self.assertTrue(pending_event["attribution_pending"])
        self.assertNotIn("provider", pending_event)
        self.assertEqual(app._live_usage_overlay["providers"], {})
        self.assertEqual(app._pending_latest_event_id, "event-live")
        diagnostic.assert_called_once()
        self.assertEqual(diagnostic.call_args.args[0], "display_provisional")

    def test_provisional_pending_request_follows_reselect_and_can_be_cleared(self) -> None:
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        app.state = self.state()
        app.state.latest_account_name = "Codex local - api-service-local"
        event_when = datetime.now(timezone.utc)
        app.state.latest_request = {
            "event_id": "event-live",
            "session_id": "session-live",
            "provider": "Codex local - api-service-local",
        }
        pending_event = {
            "event_id": "event-live",
            "session_id": "session-live",
            "model": "gpt-test",
            "when": event_when,
            "attribution_pending": True,
        }
        app._pending_latest_event_id = "event-live"
        app._live_usage_event_records = {"event-live": pending_event}
        route = {
            "session_id": "session-live",
            "provider": "Codex local - first@example.com",
            "provider_provisional": True,
            "provider_confirmation_source": "active_route_hint",
            "turn_started_at": (event_when - timedelta(seconds=5)).isoformat(),
            "active": True,
        }

        self.assertTrue(app._promote_pending_latest_account_from_sessions([route]))
        route["provider"] = "Codex local - second@example.com"
        self.assertTrue(app._promote_pending_latest_account_from_sessions([route]))
        self.assertEqual(
            app.state.latest_request["provider"],
            "Codex local - second@example.com",
        )
        self.assertTrue(app._promote_pending_latest_account_from_sessions([]))
        self.assertEqual(
            app.state.latest_request["provider"],
            "Codex local - api-service-local",
        )
        self.assertNotIn("provider_provisional", app.state.latest_request)
        self.assertTrue(pending_event["attribution_pending"])
        self.assertEqual(app._pending_latest_event_id, "event-live")

    def test_confirmed_active_event_repairs_pending_latest_request_label_only(self) -> None:
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        app.state = self.state()
        app.state.latest_account_name = "Codex local - api-service-local"
        app.state.latest_request = {
            "event_id": "event-1",
            "session_id": "session-1",
            "model": "gpt-test",
        }
        pending_event = {
            "event_id": "event-1",
            "session_id": "session-1",
            "attribution_pending": True,
        }
        app._live_usage_event_records = {"event-1": pending_event}
        app._live_usage_overlay = {"providers": {}}

        changed = app._promote_pending_latest_account_from_sessions(
            [
                {
                    "session_id": "session-1",
                    "usage_event_id": "event-1",
                    "provider": "Codex local - final@example.com",
                    "provider_confirmed": True,
                }
            ]
        )

        self.assertTrue(changed)
        self.assertEqual(app.state.latest_account_name, "Codex local - final@example.com")
        self.assertTrue(pending_event["attribution_pending"])
        self.assertEqual(app._live_usage_overlay["providers"], {})

    def test_confirmed_active_event_repairs_suppressed_pending_request(self) -> None:
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        app.state = self.state()
        previous_at = datetime.now(monitor.CN_TZ) - timedelta(minutes=2)
        app.state.latest_account_name = "Codex local - previous@example.com"
        app.state.latest_request = {
            "kind": "success",
            "model": "gpt-previous",
            "created_at": previous_at.isoformat(timespec="seconds"),
        }
        event_when = datetime.now(timezone.utc)
        pending_event = {
            "event_id": "event-new",
            "session_id": "session-new",
            "model": "gpt-new",
            "when": event_when,
            "attribution_pending": True,
        }
        app._pending_latest_event_id = "event-new"
        app._live_usage_event_records = {"event-new": pending_event}

        sessions = [
            {
                "session_id": "session-new",
                "usage_event_id": "event-new",
                "provider": "Codex local - final@example.com",
                "provider_confirmed": True,
            }
        ]
        with patch.object(monitor, "append_attribution_diagnostic") as diagnostic:
            changed = app._promote_pending_latest_account_from_sessions(sessions)
            repeated = app._promote_pending_latest_account_from_sessions(sessions)

        self.assertTrue(changed)
        self.assertFalse(repeated)
        self.assertEqual(app._pending_latest_event_id, "")
        self.assertEqual(app.state.latest_account_name, "Codex local - final@example.com")
        self.assertEqual(app.state.latest_request["event_id"], "event-new")
        self.assertEqual(app.state.latest_request["model"], "gpt-new")
        diagnostic.assert_called_once()
        self.assertEqual(diagnostic.call_args.args[0], "display_resolved")

    def test_pending_latest_request_is_not_repaired_from_ambiguous_accounts(self) -> None:
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        app.state = self.state()
        app.state.latest_account_name = "Codex local - api-service-local"
        app.state.latest_request = {
            "event_id": "event-1",
            "session_id": "session-1",
        }
        app._live_usage_event_records = {
            "event-1": {"attribution_pending": True}
        }
        sessions = [
            {
                "session_id": "session-1",
                "usage_event_id": "event-1",
                "provider": f"Codex local - {name}@example.com",
                "provider_confirmed": True,
            }
            for name in ("first", "second")
        ]

        self.assertFalse(
            app._promote_pending_latest_account_from_sessions(sessions)
        )
        self.assertEqual(
            app.state.latest_account_name,
            "Codex local - api-service-local",
        )

    def test_delayed_cockpit_marker_reconciles_pending_event_after_next_turn_started(self) -> None:
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        app.state = self.state()
        app.state.latest_account_name = "Codex local - api-service-local"
        app.state.latest_request = {
            "event_id": "event-1",
            "session_id": "session-1",
            "model": "gpt-old",
        }
        event = self.event()
        event.update(
            {
                "event_id": "event-1",
                "session_id": "session-1",
                "attribution_pending": True,
                "cost": 0.5,
            }
        )
        app._live_usage_event_records = {"event-1": event}
        app._live_usage_overlay = {"providers": {}}
        app._apply_live_usage_overlay = MagicMock()
        marker = {
            "when": event["when"] + timedelta(seconds=1),
            "label": "Codex local - final@example.com",
            "model": "gpt-final",
            "total_tokens": event["total_tokens"],
            "input_tokens": event["input_tokens"],
            "cached_tokens": event["cached_tokens"],
            "output_tokens": event["output_tokens"],
        }

        changed = app._reconcile_pending_live_events_with_markers([marker])

        self.assertTrue(changed)
        self.assertNotIn("attribution_pending", event)
        self.assertEqual(event["provider"], "Codex local - final@example.com")
        self.assertEqual(
            app.state.latest_account_name,
            "Codex local - final@example.com",
        )
        self.assertEqual(
            app._live_usage_overlay["providers"]["Codex local - final@example.com"]["tokens"],
            event["total_tokens"],
        )
        app._apply_live_usage_overlay.assert_called_once_with(app.state)

    def test_later_snapshot_uses_resolved_same_turn_anchor(self) -> None:
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        app.state = self.state()
        app.state.latest_account_name = "Codex local - api-service-local"
        app.state.latest_request = {
            "event_id": "later",
            "session_id": "session-1",
        }
        app._live_usage_overlay = {"providers": {}}
        app._apply_live_usage_overlay = MagicMock()
        base = datetime.now(timezone.utc)
        anchor = {
            "event_id": "anchor",
            "session_id": "session-1",
            "when": base + timedelta(seconds=1),
            "turn_started_at": base,
            "total_tokens": 100,
            "input_tokens": 90,
            "cached_tokens": 0,
            "output_tokens": 10,
            "provider": "Codex local - final@example.com",
        }
        later = {
            "event_id": "later",
            "session_id": "session-1",
            "when": base + timedelta(seconds=8),
            "turn_started_at": base,
            "total_tokens": 300,
            "input_tokens": 290,
            "cached_tokens": 0,
            "output_tokens": 10,
            "cost": 0.2,
            "attribution_pending": True,
        }
        app._live_usage_event_records = {
            "anchor": anchor,
            "later": later,
        }
        marker = {
            "when": anchor["when"],
            "label": "Codex local - final@example.com",
            "model": "gpt-test",
            "total_tokens": anchor["total_tokens"],
            "input_tokens": anchor["input_tokens"],
            "cached_tokens": anchor["cached_tokens"],
            "output_tokens": anchor["output_tokens"],
        }

        changed = app._reconcile_pending_live_events_with_markers(
            [marker],
            [
                {
                    "session_id": "session-1",
                    "turn_started_at": base.isoformat(),
                }
            ],
        )

        self.assertTrue(changed)
        self.assertNotIn("attribution_pending", later)
        self.assertEqual(later["provider"], "Codex local - final@example.com")
        self.assertEqual(
            app.state.latest_account_name,
            "Codex local - final@example.com",
        )

    def test_authoritative_refresh_recovers_pending_recent_request_display(self) -> None:
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        app.state = self.state()
        event_when = datetime.now(timezone.utc)
        pending_event = {
            "event_id": "event-1",
            "session_id": "session-1",
            "when": event_when,
            "total_tokens": 50,
            "attribution_pending": True,
        }
        app._pending_latest_event_id = "event-1"
        app._live_usage_event_records = {"event-1": pending_event}
        app._last_attribution_diagnostic_signature = None
        authoritative = self.state(fresh=True)
        authoritative.latest_account_name = "Codex local - final@example.com"
        authoritative.client_usage["latest_request"] = {
            "provider": "Codex local - final@example.com",
            "model": "gpt-final",
            "created_at": event_when.astimezone(monitor.CN_TZ).isoformat(
                timespec="seconds"
            ),
        }

        with patch.object(monitor, "append_attribution_diagnostic") as diagnostic:
            app._diagnose_authoritative_attribution_refresh(
                authoritative,
                event_when + timedelta(seconds=1),
            )

        self.assertEqual(app._pending_latest_event_id, "")
        self.assertTrue(pending_event["attribution_pending"])
        diagnostic.assert_called_once()
        self.assertEqual(diagnostic.call_args.args[0], "authoritative_refresh")
        self.assertEqual(
            diagnostic.call_args.kwargs["status"],
            "confirmed_by_authoritative_refresh",
        )

    def test_refresh_does_not_replace_confirmed_card_with_pending_api_service(self) -> None:
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        app.state = self.state()
        confirmed_at = datetime.now(monitor.CN_TZ) - timedelta(minutes=2)
        app.state.latest_account_name = "Codex local - confirmed@example.com"
        app.state.latest_request = {
            "kind": "success",
            "model": "gpt-confirmed",
            "created_at": confirmed_at.isoformat(timespec="seconds"),
        }
        pending_event = {
            "event_id": "event-pending",
            "when": datetime.now(timezone.utc),
            "attribution_pending": True,
        }
        app._pending_latest_event_id = "event-pending"
        app._live_usage_event_records = {"event-pending": pending_event}
        refreshed = self.state(fresh=True)
        refreshed.latest_account_name = "LOCAL - Codex local - api-service-local"
        refreshed.latest_request = {
            "kind": "success",
            "model": "gpt-pending",
            "created_at": pending_event["when"].isoformat(timespec="seconds"),
        }
        refreshed.client_usage["latest_request"] = {
            "provider": "Codex local - api-service-local",
            "model": "gpt-pending",
            "created_at": pending_event["when"].isoformat(timespec="seconds"),
        }

        self.assertTrue(
            app._preserve_confirmed_latest_request_during_pending(refreshed)
        )
        self.assertEqual(
            refreshed.latest_account_name,
            "Codex local - confirmed@example.com",
        )
        self.assertEqual(refreshed.latest_request["model"], "gpt-confirmed")

    def test_attribution_diagnostic_rotates_and_filters_conversation_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "attribution.jsonl"
            path.write_text("old diagnostic\n", encoding="utf-8")
            event = {
                "event_id": "event-safe",
                "session_id": "session-safe",
                "total_tokens": 123,
                "content": "must not be written",
            }
            with (
                patch.object(monitor, "ATTRIBUTION_DIAGNOSTICS_PATH", path),
                patch.object(monitor, "ATTRIBUTION_DIAGNOSTICS_MAX_BYTES", 1),
            ):
                self.assertTrue(
                    self._append_attribution_diagnostic(
                        "pending",
                        event,
                        message="secret",
                        nested={"prompt": "secret", "reason": "safe"},
                    )
                )

            archive = path.with_name(f"{path.name}.1")
            self.assertTrue(archive.exists())
            record = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(record["event_id"], "event-safe")
            self.assertEqual(record["total_tokens"], 123)
            self.assertNotIn("content", record)
            self.assertNotIn("message", record)
            self.assertNotIn("prompt", record["nested"])
            self.assertEqual(record["nested"]["reason"], "safe")

    def test_delayed_marker_propagates_only_inside_the_current_turn(self) -> None:
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        app.state = self.state()
        app.state.latest_account_name = "Codex local - api-service-local"
        app._live_usage_overlay = {"providers": {}}
        app._apply_live_usage_overlay = MagicMock()
        base = datetime.now(timezone.utc)

        def event(event_id: str, offset: int, total: int) -> dict:
            return {
                "event_id": event_id,
                "session_id": "session-1",
                "when": base + timedelta(seconds=offset),
                "total_tokens": total,
                "input_tokens": total - 10,
                "cached_tokens": 0,
                "output_tokens": 10,
                "cost": 0.1,
                "attribution_pending": True,
            }

        previous_turn = event("previous", -10, 101)
        first_current = event("first", 1, 111)
        anchored = event("anchored", 5, 222)
        later_current = event("later", 8, 333)
        app._live_usage_event_records = {
            item["event_id"]: item
            for item in (previous_turn, first_current, anchored, later_current)
        }
        app.state.latest_request = {
            "event_id": "later",
            "session_id": "session-1",
        }
        marker = {
            "when": base + timedelta(seconds=6),
            "label": "Codex local - final@example.com",
            "model": "gpt-final",
            "total_tokens": 222,
            "input_tokens": 212,
            "cached_tokens": 0,
            "output_tokens": 10,
        }

        app._reconcile_pending_live_events_with_markers(
            [marker],
            [
                {
                    "session_id": "session-1",
                    "started_at": base.isoformat(),
                }
            ],
        )

        self.assertIn("attribution_pending", previous_turn)
        for current in (first_current, anchored, later_current):
            self.assertNotIn("attribution_pending", current)
            self.assertEqual(current["provider"], "Codex local - final@example.com")
        self.assertEqual(
            app.state.latest_account_name,
            "Codex local - final@example.com",
        )

    def test_new_cockpit_usage_revision_requests_pending_attribution_refresh(self) -> None:
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        app.state = self.state()
        app._live_usage_event_records = {}
        app._attribution_refresh_requested = False
        app._last_cockpit_usage_revision = None
        app._attributed_cockpit_usage_revision = None
        app._attribution_refresh_inflight_revision = None
        baseline = (100, 1_000, "account-1")
        final = (101, 2_000, "account-2")

        self.assertFalse(app._observe_cockpit_usage_revision(baseline))
        self.assertEqual(app._attributed_cockpit_usage_revision, baseline)
        app.state.client_usage["providers"] = [
            {
                "name": "Codex local - api-service-local",
                "tokens": 77_524,
                "requests": 1,
                "is_api_service_aggregate": True,
            }
        ]

        self.assertTrue(app._observe_cockpit_usage_revision(final))
        self.assertTrue(app._attribution_refresh_requested)

    def test_live_trace_starts_new_events_at_detection_time(self) -> None:
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        app.state = self.state()
        app._live_usage_overlay = None
        app._token_flow_samples = []
        now = datetime.now(timezone.utc)
        first = self.event()
        first["when"] = now - timedelta(seconds=1)
        second = self.event()
        second["when"] = now - timedelta(seconds=3)

        with patch.object(monitor.time, "monotonic", return_value=100.0):
            app._record_live_usage_events([first, second])

        self.assertEqual(len(app._token_flow_samples), 2)
        spacing = app._token_flow_samples[0][0] - app._token_flow_samples[1][0]
        self.assertAlmostEqual(app._token_flow_samples[0][0], 100.0)
        self.assertAlmostEqual(spacing, 0.02)

    def test_live_events_feed_exact_batch_total_to_delta_badge(self) -> None:
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        app.state = self.state()
        app._live_usage_overlay = None
        first = self.event()
        second = self.event()
        second["total_tokens"] = 75
        second["input_tokens"] = 75
        second["cached_tokens"] = 0
        second["output_tokens"] = 0

        with (
            patch.object(app, "_record_token_delta_badge") as record_badge,
            patch.object(app, "_record_cost_delta_badge") as record_cost_badge,
            patch.object(app, "_live_event_request_context", return_value=("", "gpt-test")),
            patch.object(
                monitor,
                "estimate_live_usage_cost_with_resolution",
                return_value=(0.25, True),
            ),
        ):
            app._record_live_usage_events([first, second])

        record_badge.assert_called_once()
        self.assertEqual(record_badge.call_args.args, (125,))
        self.assertIsInstance(record_badge.call_args.kwargs["now"], float)
        record_cost_badge.assert_called_once()
        self.assertEqual(record_cost_badge.call_args.args, (0.5,))
        self.assertIsInstance(record_cost_badge.call_args.kwargs["now"], float)
        self.assertEqual(app.state.today_tokens, 225)
        self.assertAlmostEqual(app.state.today_account_cost, 0.5)

    def test_historical_catchup_uses_snapshot_date_and_deduplicates_it(self) -> None:
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        app.state = self.state()
        app._live_usage_overlay = None
        app._live_usage_seen_ids = {}
        event = self.event()
        snapshot_date = datetime.now(monitor.CN_TZ).date() - timedelta(days=1)
        event["when"] = datetime(
            snapshot_date.year,
            snapshot_date.month,
            snapshot_date.day,
            23,
            30,
            tzinfo=monitor.CN_TZ,
        ).astimezone(timezone.utc)
        event["event_id"] = "catchup-event-1"
        app.state.client_usage["date"] = snapshot_date.isoformat()

        self.assertTrue(
            app._record_live_usage_events(
                [event],
                allow_historical=True,
                animate=False,
            )
        )
        self.assertEqual(app.state.today_tokens, 150)

        different_day = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        different_day.state = self.state()
        different_day._live_usage_overlay = None
        different_day._live_usage_seen_ids = {}
        different_day.state.client_usage["date"] = (
            snapshot_date + timedelta(days=1)
        ).isoformat()
        self.assertFalse(
            different_day._record_live_usage_events(
                [dict(event)],
                allow_historical=True,
                animate=False,
            )
        )
        self.assertFalse(
            app._record_live_usage_events(
                [event],
                allow_historical=True,
                animate=False,
            )
        )
        self.assertEqual(app.state.today_tokens, 150)

    def test_live_checkpoint_restores_totals_after_process_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = Path(directory) / "live-checkpoint.json"
            first = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
            first.state = self.state()
            first._live_usage_overlay = None
            first._live_usage_seen_ids = {}
            first._last_live_checkpoint_write_at = float("-inf")
            quota_reconcile_since = datetime.now(timezone.utc) - timedelta(
                minutes=5
            )
            first._live_quota_reconcile_since = quota_reconcile_since
            event = self.event()
            event["event_id"] = "persisted-event-1"
            event["cost"] = 1.25

            with patch.object(monitor, "LIVE_USAGE_CHECKPOINT_JSON", checkpoint):
                first._record_live_usage_events([event], animate=False)
                self.assertTrue(checkpoint.exists())

                restarted = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
                restarted.state = self.state()
                restarted._live_usage_overlay = None
                restarted._live_usage_seen_ids = {}
                restarted._last_live_checkpoint_write_at = float("-inf")
                self.assertTrue(restarted._restore_live_usage_checkpoint())

            self.assertEqual(restarted.state.today_tokens, 150)
            self.assertEqual(restarted.state.today_requests, 3)
            self.assertAlmostEqual(restarted.state.today_account_cost, 1.25)
            self.assertEqual(
                restarted._live_quota_reconcile_since,
                quota_reconcile_since,
            )
            self.assertTrue(restarted._live_usage_verification_pending)
            summary = restarted._usage_range_summary("24h")
            hour = event["when"].astimezone(monitor.CN_TZ).hour
            bucket = next(row for row in summary["series"] if row["hour"] == hour)
            self.assertEqual(bucket["tokens"], 150)
            self.assertEqual(bucket["requests"], 3)

    def test_live_checkpoint_restores_quota_window_delta(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = Path(directory) / "live-checkpoint.json"
            now = datetime.now(timezone.utc).replace(microsecond=0)
            start_at = now - timedelta(days=1)
            reset_at = start_at + timedelta(days=7)

            def quota_state() -> monitor.MonitorState:
                state = self.state(tokens=100, requests=2)
                state.client_usage["providers"] = [
                    {
                        "name": "Codex local - account@example.com",
                        "window_7d": {
                            "requests": 2,
                            "tokens": 100,
                            "input_tokens": 60,
                            "cached_input_tokens": 30,
                            "output_tokens": 10,
                            "cost": 1.0,
                            "models": {"gpt-5.6-sol": 100},
                            "start_at": start_at.isoformat(),
                            "end_at": (now - timedelta(seconds=10)).isoformat(),
                            "quota_available": True,
                            "window_minutes": 10_080,
                            "resets_at": reset_at.isoformat(),
                        },
                    }
                ]
                state.top_accounts = []
                return state

            first = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
            first.state = quota_state()
            first._last_live_checkpoint_write_at = float("-inf")
            first._live_usage_overlay = {
                "base_today_tokens": 100,
                "base_today_requests": 2,
                "base_today_cost": 1.0,
                "base_authoritative_tokens": 100,
                "base_updated_at": "",
                "usage_accounting_schema": 0,
                "tokens": 50,
                "requests": 1,
                "cost": 0.25,
                "unpriced_tokens": 0,
                "unpriced_models": {},
                "input_tokens": 20,
                "cached_input_tokens": 20,
                "output_tokens": 10,
                "latest_when": now,
                "providers": {},
                "base_hourly": [],
                "hourly": {},
            }
            first._record_live_quota_window_overlay(
                first._live_usage_overlay,
                "Codex local - account@example.com",
                {
                    "event_id": "persisted-quota-event",
                    "model": "gpt-5.6-sol",
                    "total_tokens": 50,
                    "input_tokens": 40,
                    "cached_tokens": 20,
                    "output_tokens": 10,
                    "cost": 0.25,
                    "unpriced_tokens": 0,
                },
                now,
            )

            with patch.object(monitor, "LIVE_USAGE_CHECKPOINT_JSON", checkpoint):
                self.assertTrue(first._persist_live_usage_checkpoint(force=True))
                restarted = monitor.FloatingMonitorApp.__new__(
                    monitor.FloatingMonitorApp
                )
                restarted.state = quota_state()
                restarted._live_usage_overlay = None
                restarted._live_usage_seen_ids = {}
                restarted._last_live_checkpoint_write_at = float("-inf")
                restarted._live_quota_reconcile_since = None
                restarted._live_usage_verification_pending = False
                restarted._live_usage_verification_latest_when = None
                self.assertTrue(restarted._restore_live_usage_checkpoint())

            window = restarted.state.client_usage["providers"][0]["window_7d"]
            self.assertEqual(window["tokens"], 150)
            self.assertEqual(window["requests"], 3)
            self.assertAlmostEqual(window["cost"], 1.25)
            self.assertTrue(window["window_usage_live"])

    def test_checkpoint_rebases_quota_delta_after_newer_authoritative_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = Path(directory) / "live-checkpoint.json"
            now = datetime.now(timezone.utc).replace(microsecond=0)
            start_at = now - timedelta(days=1)
            reset_at = start_at + timedelta(days=7)
            provider = "Codex local - account@example.com"
            first = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
            first.state = self.state(tokens=100, requests=2)
            first._last_live_checkpoint_write_at = float("-inf")
            first._live_usage_overlay = {
                "base_today_tokens": 100,
                "base_today_requests": 2,
                "base_today_cost": 1.0,
                "base_authoritative_tokens": 100,
                "base_updated_at": "",
                "usage_accounting_schema": 0,
                "tokens": 100,
                "requests": 2,
                "cost": 1.0,
                "unpriced_tokens": 0,
                "unpriced_models": {},
                "input_tokens": 60,
                "cached_input_tokens": 30,
                "output_tokens": 10,
                "latest_when": now,
                "providers": {
                    provider: {
                        "base_tokens": 100,
                        "base_requests": 2,
                        "base_cost": 1.0,
                        "base_input_tokens": 60,
                        "base_cached_input_tokens": 30,
                        "base_output_tokens": 10,
                        "base_models": {"gpt-5.6-sol": 100},
                        "tokens": 100,
                        "requests": 2,
                        "cost": 1.0,
                        "input_tokens": 60,
                        "cached_input_tokens": 30,
                        "output_tokens": 10,
                        "models": {"gpt-5.6-sol": 100},
                        "replace_existing": True,
                        "latest_when": now,
                        "latest_model": "gpt-5.6-sol",
                    }
                },
                "quota_windows": {
                    provider: {
                        "window_7d": {
                            "signature": [10_080, reset_at.isoformat()],
                            "start_at": start_at,
                            "resets_at": reset_at,
                            "base_through": now - timedelta(seconds=20),
                            "base_tokens": 0,
                            "base_requests": 0,
                            "base_cost": 0.0,
                            "tokens": 70,
                            "requests": 2,
                            "cost": 0.7,
                            "models": {"gpt-5.6-sol": 70},
                            "event_ids": {},
                            "latest_when": now,
                        }
                    }
                },
                "base_hourly": [],
                "hourly": {},
            }

            with patch.object(monitor, "LIVE_USAGE_CHECKPOINT_JSON", checkpoint):
                self.assertTrue(first._persist_live_usage_checkpoint(force=True))
                restarted = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
                restarted.state = self.state(tokens=150, requests=3)
                restarted.state.today_account_cost = 1.5
                restarted.state.client_usage["providers"] = [
                    {
                        "name": provider,
                        "tokens": 150,
                        "requests": 3,
                        "cost": 1.5,
                        "input_tokens": 90,
                        "cached_input_tokens": 45,
                        "output_tokens": 15,
                        "models": {"gpt-5.6-sol": 150},
                        "window_7d": {
                            "tokens": 150,
                            "requests": 3,
                            "cost": 1.5,
                            "input_tokens": 90,
                            "cached_input_tokens": 45,
                            "output_tokens": 15,
                            "models": {"gpt-5.6-sol": 150},
                            "start_at": start_at.isoformat(),
                            "end_at": (now - timedelta(seconds=5)).isoformat(),
                            "quota_available": True,
                            "window_minutes": 10_080,
                            "resets_at": reset_at.isoformat(),
                        },
                    }
                ]
                restarted.state.top_accounts = []
                restarted._live_usage_overlay = None
                restarted._live_usage_seen_ids = {}
                restarted._last_live_checkpoint_write_at = float("-inf")

                self.assertTrue(restarted._restore_live_usage_checkpoint())

            window = restarted.state.client_usage["providers"][0]["window_7d"]
            self.assertEqual(restarted.state.today_tokens, 200)
            self.assertEqual(window["tokens"], 200)
            self.assertEqual(window["requests"], 4)
            self.assertAlmostEqual(window["cost"], 2.0)
            self.assertEqual(window["models"]["gpt-5.6-sol"], 200)

    def test_quota_rebase_runs_when_daily_checkpoint_base_already_matches(self) -> None:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        start_at = now - timedelta(days=1)
        reset_at = start_at + timedelta(days=7)
        provider = "Codex local - account@example.com"
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        app.state = self.state(tokens=150, requests=3)
        app.state.client_usage["providers"] = [
            {
                "name": provider,
                "tokens": 150,
                "requests": 3,
                "cost": 1.5,
                "models": {"gpt-5.6-sol": 150},
                "window_7d": {
                    "tokens": 150,
                    "requests": 3,
                    "cost": 1.5,
                    "models": {"gpt-5.6-sol": 150},
                    "start_at": start_at.isoformat(),
                    "end_at": (now - timedelta(seconds=5)).isoformat(),
                    "quota_available": True,
                    "window_minutes": 10_080,
                    "resets_at": reset_at.isoformat(),
                },
            }
        ]
        app.state.top_accounts = []
        app._live_usage_overlay = {
            "providers": {
                provider: {
                    "base_tokens": 150,
                    "base_requests": 3,
                    "base_cost": 1.5,
                    "base_models": {"gpt-5.6-sol": 150},
                    "tokens": 50,
                    "requests": 1,
                    "cost": 0.5,
                    "models": {"gpt-5.6-sol": 50},
                    "latest_when": now,
                    "latest_model": "gpt-5.6-sol",
                }
            },
            "quota_windows": {
                provider: {
                    "window_7d": {
                        "signature": [10_080, reset_at.isoformat()],
                        "base_through": now - timedelta(seconds=20),
                        "base_tokens": 0,
                        "tokens": 20,
                        "models": {"gpt-5.6-sol": 20},
                    }
                }
            },
        }

        app._rebase_restored_live_quota_windows(app._live_usage_overlay)
        app._apply_live_quota_window_overlay(app.state)

        window = app.state.client_usage["providers"][0]["window_7d"]
        self.assertEqual(window["tokens"], 200)
        self.assertEqual(window["requests"], 4)
        self.assertAlmostEqual(window["cost"], 2.0)

    def test_quota_rebase_repairs_missing_event_at_same_authoritative_cutoff(self) -> None:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        start_at = now - timedelta(days=1)
        reset_at = start_at + timedelta(days=7)
        through = now - timedelta(seconds=5)
        provider = "Codex local - account@example.com"
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        app.state = self.state(tokens=150, requests=3)
        app.state.client_usage["providers"] = [
            {
                "name": provider,
                "tokens": 150,
                "requests": 3,
                "cost": 1.5,
                "models": {"gpt-5.6-sol": 150},
                "window_7d": {
                    "tokens": 150,
                    "requests": 3,
                    "cost": 1.5,
                    "models": {"gpt-5.6-sol": 150},
                    "start_at": start_at.isoformat(),
                    "end_at": through.isoformat(),
                    "quota_available": True,
                    "window_minutes": 10_080,
                    "resets_at": reset_at.isoformat(),
                },
            }
        ]
        app.state.top_accounts = []
        app._live_usage_overlay = {
            "providers": {
                provider: {
                    "base_tokens": 150,
                    "base_requests": 3,
                    "base_cost": 1.5,
                    "base_models": {"gpt-5.6-sol": 150},
                    "tokens": 50,
                    "requests": 1,
                    "cost": 0.5,
                    "models": {"gpt-5.6-sol": 50},
                    "latest_when": now,
                    "latest_model": "gpt-5.6-sol",
                }
            },
            "quota_windows": {
                provider: {
                    "window_7d": {
                        "signature": [10_080, reset_at.isoformat()],
                        "base_through": through,
                        "base_tokens": 150,
                        "base_requests": 3,
                        "base_cost": 1.5,
                        "base_models": {"gpt-5.6-sol": 150},
                        "tokens": 20,
                        "requests": 0,
                        "cost": 0.2,
                        "models": {"gpt-5.6-sol": 20},
                    }
                }
            },
        }

        app._rebase_restored_live_quota_windows(app._live_usage_overlay)
        app._apply_live_quota_window_overlay(app.state)

        window = app.state.client_usage["providers"][0]["window_7d"]
        self.assertEqual(window["tokens"], 200)
        self.assertEqual(window["requests"], 4)
        self.assertAlmostEqual(window["cost"], 2.0)

    def test_quota_rebase_seeds_stable_window_when_catchup_rows_miss_parallel_session(self) -> None:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        start_at = now - timedelta(days=1)
        reset_at = start_at + timedelta(days=7)
        through = now - timedelta(minutes=10)
        provider = "Codex local - account@example.com"
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        app.state = self.state(tokens=150, requests=3)
        app.state.client_usage["providers"] = [
            {
                "name": provider,
                "tokens": 150,
                "requests": 3,
                "cost": 1.5,
                "input_tokens": 90,
                "cached_input_tokens": 45,
                "output_tokens": 15,
                "models": {"gpt-5.6-sol": 150},
                "window_7d": {
                    "tokens": 150,
                    "requests": 3,
                    "cost": 1.5,
                    "input_tokens": 90,
                    "cached_input_tokens": 45,
                    "output_tokens": 15,
                    "models": {"gpt-5.6-sol": 150},
                    "start_at": start_at.isoformat(),
                    "end_at": through.isoformat(),
                    "quota_available": True,
                    "window_minutes": 10_080,
                    "resets_at": reset_at.isoformat(),
                },
            }
        ]
        app.state.top_accounts = []
        app._live_usage_overlay = {
            "providers": {
                provider: {
                    "replace_existing": True,
                    "base_tokens": 200,
                    "base_requests": 4,
                    "base_cost": 2.0,
                    "base_input_tokens": 120,
                    "base_cached_input_tokens": 60,
                    "base_output_tokens": 20,
                    "base_models": {"gpt-5.6-sol": 200},
                    "tokens": 0,
                    "requests": 0,
                    "cost": 0.0,
                    "input_tokens": 0,
                    "cached_input_tokens": 0,
                    "output_tokens": 0,
                    "models": {},
                    "latest_when": now,
                    "latest_model": "gpt-5.6-sol",
                }
            },
            # No event rows for this provider reached the catch-up payload, so
            # the quota target has not been created yet.
            "quota_windows": {},
        }

        app._rebase_restored_live_quota_windows(app._live_usage_overlay)
        app._apply_live_quota_window_overlay(app.state)

        window = app.state.client_usage["providers"][0]["window_7d"]
        self.assertEqual(window["tokens"], 200)
        self.assertEqual(window["requests"], 4)
        self.assertAlmostEqual(window["cost"], 2.0)
        self.assertEqual(window["models"], {"gpt-5.6-sol": 200})

    def test_quota_rebase_removes_reassigned_event_from_absolute_provider(self) -> None:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        start_at = now - timedelta(days=1)
        reset_at = start_at + timedelta(days=7)
        through = now - timedelta(seconds=5)
        provider = "Codex local - account@example.com"
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        app.state = self.state(tokens=150, requests=3)
        app.state.client_usage["providers"] = [
            {
                "name": provider,
                "tokens": 150,
                "requests": 3,
                "cost": 1.5,
                "models": {"gpt-5.6-sol": 150},
                "window_7d": {
                    "tokens": 150,
                    "requests": 3,
                    "cost": 1.5,
                    "models": {"gpt-5.6-sol": 150},
                    "start_at": start_at.isoformat(),
                    "end_at": through.isoformat(),
                    "quota_available": True,
                    "window_minutes": 10_080,
                    "resets_at": reset_at.isoformat(),
                },
            }
        ]
        app.state.top_accounts = []
        app._live_usage_overlay = {
            "providers": {
                provider: {
                    "replace_existing": True,
                    "base_tokens": 150,
                    "base_requests": 3,
                    "base_cost": 1.5,
                    "base_models": {"gpt-5.6-sol": 150},
                    "tokens": 50,
                    "requests": 1,
                    "cost": 0.5,
                    "models": {"gpt-5.6-terra": 50},
                    "latest_when": now,
                    "latest_model": "gpt-5.6-terra",
                }
            },
            "quota_windows": {
                provider: {
                    "window_7d": {
                        "signature": [10_080, reset_at.isoformat()],
                        "base_through": through,
                        "base_tokens": 150,
                        "base_requests": 3,
                        "base_cost": 1.5,
                        "base_models": {"gpt-5.6-sol": 150},
                        "tokens": 70,
                        "requests": 2,
                        "cost": 0.7,
                        "models": {"gpt-5.6-sol": 70},
                    }
                }
            },
        }

        app._rebase_restored_live_quota_windows(app._live_usage_overlay)
        app._apply_live_quota_window_overlay(app.state)

        window = app.state.client_usage["providers"][0]["window_7d"]
        self.assertEqual(window["tokens"], 200)
        self.assertEqual(window["requests"], 4)
        self.assertAlmostEqual(window["cost"], 2.0)
        self.assertEqual(
            window["models"],
            {"gpt-5.6-sol": 150, "gpt-5.6-terra": 50},
        )

    def test_schema_three_live_checkpoint_is_discarded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = Path(directory) / "live-checkpoint.json"
            checkpoint.write_text(
                json.dumps(
                    {
                        "schema": 3,
                        "date": monitor.today_key(),
                        "overlay": {
                            "base_today_tokens": 100,
                            "base_today_requests": 2,
                            "base_today_cost": 0.0,
                            "tokens": 1_000_000,
                            "requests": 1,
                            "cost": 1.0,
                        },
                    }
                ),
                encoding="utf-8",
            )
            app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
            app.state = self.state()
            app._live_usage_overlay = None

            with patch.object(monitor, "LIVE_USAGE_CHECKPOINT_JSON", checkpoint):
                self.assertFalse(app._restore_live_usage_checkpoint())

            self.assertFalse(checkpoint.exists())
            self.assertEqual(app.state.today_tokens, 100)

    def test_live_checkpoint_accounting_schema_mismatch_is_discarded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = Path(directory) / "live-checkpoint.json"
            checkpoint.write_text(
                json.dumps(
                    {
                        "schema": monitor.LIVE_USAGE_CHECKPOINT_SCHEMA,
                        "usage_accounting_schema": 0,
                        "date": monitor.today_key(),
                        "overlay": {
                            "usage_accounting_schema": 0,
                            "base_today_tokens": 100,
                            "base_today_requests": 1,
                            "base_today_cost": 1.0,
                            "tokens": 50,
                            "requests": 1,
                            "cost": 0.5,
                        },
                    }
                ),
                encoding="utf-8",
            )
            app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
            app.state = self.state(tokens=120, requests=2)
            app.state.client_usage["usage_accounting_schema"] = 1
            app.state.today_account_cost = 1.2
            app._live_usage_overlay = None

            with patch.object(monitor, "LIVE_USAGE_CHECKPOINT_JSON", checkpoint):
                self.assertFalse(app._restore_live_usage_checkpoint())

            self.assertFalse(checkpoint.exists())
            self.assertEqual(app.state.today_tokens, 120)
            self.assertEqual(app.state.today_requests, 2)
            self.assertAlmostEqual(app.state.today_account_cost, 1.2)

    def test_live_checkpoint_supplies_schema_when_authoritative_cache_is_stale(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = Path(directory) / "live-checkpoint.json"
            checkpoint.write_text(
                json.dumps(
                    {
                        "schema": monitor.LIVE_USAGE_CHECKPOINT_SCHEMA,
                        "usage_accounting_schema": 1,
                        "date": monitor.today_key(),
                        "overlay": {
                            "usage_accounting_schema": 1,
                            "base_today_tokens": 100,
                            "base_today_requests": 2,
                            "base_today_cost": 1.0,
                            "tokens": 50,
                            "requests": 1,
                            "cost": 0.5,
                        },
                    }
                ),
                encoding="utf-8",
            )
            app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
            app.state = self.state(tokens=0, requests=0)
            app.state.client_usage["usage_accounting_schema"] = 0
            app.state.today_account_cost = 0.0
            app._live_usage_overlay = None

            with patch.object(monitor, "LIVE_USAGE_CHECKPOINT_JSON", checkpoint):
                self.assertTrue(app._restore_live_usage_checkpoint())

            self.assertTrue(checkpoint.exists())
            self.assertEqual(app.state.client_usage["usage_accounting_schema"], 1)
            self.assertEqual(app.state.today_tokens, 150)
            self.assertEqual(app.state.today_requests, 3)
            self.assertAlmostEqual(app.state.today_account_cost, 1.5)

    def test_live_history_write_is_throttled(self) -> None:
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        app.state = self.state(tokens=150, requests=3)
        app._last_live_history_write_at = float("-inf")

        with patch.object(
            monitor,
            "update_usage_history",
            return_value={"today_tokens": 150},
        ) as update_history:
            self.assertTrue(app._update_live_usage_history_if_due())
            self.assertFalse(app._update_live_usage_history_if_due())

        update_history.assert_called_once_with(app.state)
        self.assertEqual(app.state.cost_history, {"today_tokens": 150})

    def test_second_precision_catchup_boundary_overlaps_same_second(self) -> None:
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        app.state = self.state()
        app.state.client_usage["latest_request"] = {
            "created_at": (datetime.now(monitor.CN_TZ) - timedelta(seconds=10))
            .replace(microsecond=0)
            .isoformat(timespec="seconds")
        }
        app.state.latest_request = {
            "created_at": datetime.now(monitor.CN_TZ).isoformat(timespec="microseconds")
        }
        app.state.client_usage["updated_at"] = (
            datetime.now(monitor.CN_TZ) - timedelta(minutes=1)
        ).isoformat(timespec="seconds")
        app._live_usage_overlay = None

        since = app._live_usage_catchup_since()

        expected = monitor._parse_time(
            app.state.client_usage["latest_request"]["created_at"]
        ) - timedelta(microseconds=1)
        self.assertEqual(since, expected)
        same_second_event = monitor._parse_time(
            app.state.client_usage["latest_request"]["created_at"]
        ) + timedelta(milliseconds=500)
        self.assertLess(since, same_second_event)

    def test_quota_boundary_forces_catchup_before_newer_canonical_cutoff(self) -> None:
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        app.state = self.state()
        now = datetime.now(monitor.CN_TZ)
        boundary = now - timedelta(hours=2)
        app.state.client_usage["latest_request"] = {
            "created_at": (now - timedelta(seconds=10)).isoformat(
                timespec="microseconds"
            )
        }
        app.state.client_usage["updated_at"] = (
            now - timedelta(seconds=5)
        ).isoformat(timespec="microseconds")
        app._live_usage_overlay = {
            "catchup_through": now - timedelta(seconds=3),
        }
        app._live_quota_reconcile_since = boundary

        since = app._live_usage_catchup_since()

        self.assertEqual(since, boundary.astimezone(timezone.utc))

    def test_previous_day_quota_boundary_is_not_clamped_to_today(self) -> None:
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        app.state = self.state()
        now = datetime.now(monitor.CN_TZ)
        boundary = datetime.combine(
            now.date() - timedelta(days=1),
            datetime.min.time(),
            tzinfo=monitor.CN_TZ,
        ) + timedelta(hours=22)
        app.state.client_usage["updated_at"] = now.isoformat(timespec="microseconds")
        app._live_usage_overlay = {"catchup_through": now - timedelta(seconds=2)}
        app._live_quota_reconcile_since = boundary

        since = app._live_usage_catchup_since()

        self.assertEqual(since, boundary.astimezone(timezone.utc))

    def test_monitor_and_exporter_use_the_same_live_event_id(self) -> None:
        when = datetime.now(monitor.CN_TZ).replace(microsecond=123000)
        exported = client_usage_export.UsageEvent(
            when=when.replace(tzinfo=None),
            model="gpt-test",
            input_tokens=20,
            cached_tokens=30,
            output_tokens=10,
            session_id="session-1",
        )

        self.assertEqual(
            client_usage_export.live_usage_event_id(exported),
            monitor._live_usage_event_id(when, "session-1", 50, 30, 10),
        )

    def test_absolute_catchup_survives_partial_authoritative_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = Path(directory) / "live-checkpoint.json"
            app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
            app.state = self.state(tokens=150, requests=3)
            app.state.today_tokens = 500
            app.state.today_requests = 20
            app.state.today_account_cost = 10.0
            app._live_usage_overlay = None
            app._live_usage_seen_ids = {}
            app._last_live_checkpoint_write_at = float("-inf")
            app._live_catchup_lock = threading.Lock()
            app._live_catchup_lock.acquire()
            app.closed = False
            app._draw = lambda: None
            through = datetime.now(timezone.utc) - timedelta(seconds=1)
            app._live_usage_verification_pending = True
            app._live_usage_verification_latest_when = through
            app._live_usage_verification_pending_tokens = 10_000_000
            app._live_usage_rate_samples = []
            tail = self.event()
            tail["when"] = through + timedelta(milliseconds=500)
            tail["event_id"] = "tail-event"
            tail["cost"] = 0.5
            app._live_usage_event_records = {"tail-event": tail}
            payload = {
                "through": through.isoformat(),
                "events": [],
                "summary": {
                    "tokens": 200,
                    "requests": 4,
                    "cost": 2.0,
                    "input_tokens": 120,
                    "cached_input_tokens": 60,
                    "output_tokens": 20,
                    "latest_at": through.isoformat(),
                    "latest_model": "gpt-test",
                },
                "providers": [],
            }

            with patch.object(monitor, "LIVE_USAGE_CHECKPOINT_JSON", checkpoint):
                app._apply_live_usage_catchup(payload)

            self.assertEqual(app.state.today_tokens, 250)
            self.assertEqual(app.state.today_requests, 5)
            self.assertAlmostEqual(app.state.today_account_cost, 2.5)
            self.assertFalse(app._live_usage_verification_pending)
            self.assertFalse(app._live_catchup_lock.locked())

    def test_catchup_provider_totals_survive_checkpoint_restore(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = Path(directory) / "live-checkpoint.json"
            provider_name = "Codex local - hyenas@example.com"
            through = datetime.now(timezone.utc) - timedelta(seconds=1)

            first = monitor.FloatingMonitorApp.__new__(
                monitor.FloatingMonitorApp
            )
            first.state = self.state(tokens=100, requests=1)
            first.state.client_usage.update(
                {
                    "cost": 1.0,
                    "models": {"gpt-old": 100},
                    "providers": [
                        {
                            "name": provider_name,
                            "tokens": 100,
                            "requests": 1,
                            "cost": 1.0,
                            "models": {"gpt-old": 100},
                        }
                    ],
                }
            )
            first.state.today_account_cost = 1.0
            first.state.top_accounts = [
                {
                    "name": provider_name,
                    "tokens": 100,
                    "requests": 1,
                    "cost": 1.0,
                    "models": {"gpt-old": 100},
                }
            ]
            first._live_usage_overlay = None
            first._live_usage_seen_ids = {}
            first._live_usage_event_records = {}
            first._live_usage_verification_pending = False
            first._live_usage_verification_latest_when = None
            first._live_usage_verification_pending_tokens = 0
            first._live_usage_rate_samples = []
            first._last_live_checkpoint_write_at = float("-inf")
            first._live_catchup_lock = threading.Lock()
            first._live_catchup_lock.acquire()
            first.closed = False
            first._draw = lambda: None
            payload = {
                "through": through.isoformat(),
                "events": [],
                "summary": {
                    "tokens": 400,
                    "requests": 4,
                    "cost": 4.0,
                    "models": {"gpt-5.6-sol": 400},
                    "input_tokens": 40,
                    "cached_input_tokens": 350,
                    "output_tokens": 10,
                    "latest_at": through.isoformat(),
                    "latest_model": "gpt-5.6-sol",
                },
                "providers": [
                    {
                        "name": provider_name,
                        "tokens": 375,
                        "requests": 3,
                        "cost": 3.75,
                        "models": {"gpt-5.6-sol": 375},
                        "input_tokens": 35,
                        "cached_input_tokens": 330,
                        "output_tokens": 10,
                        "latest_at": through.isoformat(),
                        "latest_model": "gpt-5.6-sol",
                    }
                ],
            }

            with patch.object(monitor, "LIVE_USAGE_CHECKPOINT_JSON", checkpoint):
                first._apply_live_usage_catchup(payload)

            first_provider = first.state.client_usage["providers"][0]
            self.assertEqual(first_provider["tokens"], 375)
            self.assertEqual(first_provider["models"], {"gpt-5.6-sol": 375})
            persisted = json.loads(checkpoint.read_text(encoding="utf-8"))
            persisted_provider = persisted["overlay"]["providers"][provider_name]
            self.assertTrue(persisted_provider["replace_existing"])
            self.assertEqual(persisted_provider["base_tokens"], 375)
            self.assertEqual(
                persisted_provider["base_models"],
                {"gpt-5.6-sol": 375},
            )

            restored = monitor.FloatingMonitorApp.__new__(
                monitor.FloatingMonitorApp
            )
            restored.state = self.state(tokens=150, requests=2)
            restored.state.client_usage.update(
                {
                    "cost": 1.5,
                    "providers": [
                        {
                            "name": provider_name,
                            "tokens": 100,
                            "requests": 1,
                            "cost": 1.0,
                            "models": {"gpt-old": 100},
                        }
                    ],
                }
            )
            restored.state.today_account_cost = 1.5
            restored.state.top_accounts = [
                {
                    "name": provider_name,
                    "tokens": 100,
                    "requests": 1,
                    "cost": 1.0,
                    "models": {"gpt-old": 100},
                }
            ]
            restored._live_usage_overlay = None

            with patch.object(monitor, "LIVE_USAGE_CHECKPOINT_JSON", checkpoint):
                self.assertTrue(restored._restore_live_usage_checkpoint())

            restored_provider = restored.state.client_usage["providers"][0]
            self.assertEqual(restored_provider["tokens"], 375)
            self.assertEqual(restored_provider["requests"], 3)
            self.assertAlmostEqual(restored_provider["cost"], 3.75)
            self.assertEqual(
                restored_provider["models"],
                {"gpt-5.6-sol": 375},
            )
            self.assertEqual(restored.state.top_accounts[0]["tokens"], 375)

    def test_accounting_schema_upgrade_replaces_old_live_overlay_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = Path(directory) / "live-checkpoint.json"
            app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
            app.state = self.state(tokens=100, requests=1)
            app.state.client_usage.update(
                {
                    "usage_accounting_schema": 0,
                    "cost": 1.0,
                    "models": {"gpt-old": 100},
                    "providers": [
                        {
                            "name": "Codex local - old@example.com",
                            "tokens": 100,
                            "requests": 1,
                            "cost": 1.0,
                            "models": {"gpt-old": 100},
                        }
                    ],
                }
            )
            app.state.today_tokens = 150
            app.state.today_requests = 2
            app.state.today_account_cost = 1.5
            app.state.top_accounts = [
                {
                    "name": "Codex local - old@example.com",
                    "tokens": 150,
                    "requests": 2,
                    "cost": 1.5,
                    "models": {"gpt-old": 150},
                }
            ]
            app.state.cost_history = monitor.trend_with_current_totals(
                None,
                150,
                2,
                1.5,
            )
            app._live_usage_overlay = {
                "usage_accounting_schema": 0,
                "base_today_tokens": 100,
                "base_today_requests": 1,
                "base_today_cost": 1.0,
                "tokens": 50,
                "requests": 1,
                "cost": 0.5,
                "providers": {},
            }
            app._live_usage_event_aliases = {"old-live-id": "old-canonical-id"}
            app._live_usage_reconciled_ids = {"old-canonical-id": None}
            app._live_usage_seen_ids = {}
            app._live_usage_event_records = {}
            app._live_usage_verification_pending = False
            app._live_usage_verification_latest_when = None
            app._live_usage_verification_pending_tokens = 0
            app._live_usage_rate_samples = []
            app._last_live_checkpoint_write_at = float("-inf")
            app._live_catchup_lock = threading.Lock()
            app._live_catchup_lock.acquire()
            app._live_initial_recheck_scheduled = True
            app.closed = False
            app._draw = lambda: None
            through = datetime.now(timezone.utc)
            payload = {
                "usage_accounting_schema": 1,
                "through": through.isoformat(),
                "events": [],
                "summary": {
                    "tokens": 120,
                    "requests": 2,
                    "cost": 1.2,
                    "models": {"gpt-canonical": 120},
                    "input_tokens": 80,
                    "cached_input_tokens": 20,
                    "output_tokens": 20,
                    "latest_at": through.isoformat(),
                    "latest_model": "gpt-canonical",
                },
                "providers": [
                    {
                        "name": "Codex local - canonical@example.com",
                        "tokens": 120,
                        "requests": 2,
                        "cost": 1.2,
                        "models": {"gpt-canonical": 120},
                        "input_tokens": 80,
                        "cached_input_tokens": 20,
                        "output_tokens": 20,
                        "latest_at": through.isoformat(),
                        "latest_model": "gpt-canonical",
                    }
                ],
            }

            with patch.object(monitor, "LIVE_USAGE_CHECKPOINT_JSON", checkpoint):
                app._apply_live_usage_catchup(payload)

            self.assertEqual(app.state.today_tokens, 120)
            self.assertEqual(app.state.today_requests, 2)
            self.assertAlmostEqual(app.state.today_account_cost, 1.2)
            self.assertEqual(app.state.cost_history["today_tokens"], 120)
            self.assertEqual(app.state.client_usage["usage_accounting_schema"], 1)
            self.assertEqual(app.state.client_usage["models"], {"gpt-canonical": 120})
            self.assertEqual(
                [row["name"] for row in app.state.client_usage["providers"]],
                ["Codex local - canonical@example.com"],
            )
            self.assertEqual(
                [monitor.account_display_key(row["name"]) for row in app.state.top_accounts],
                ["canonical@example.com"],
            )
            self.assertEqual(app._live_usage_overlay["tokens"], 0)
            self.assertEqual(app._live_usage_overlay["usage_accounting_schema"], 1)
            self.assertEqual(app._live_usage_event_aliases, {})
            self.assertEqual(app._live_usage_reconciled_ids, {})
            persisted = json.loads(checkpoint.read_text(encoding="utf-8"))
            self.assertEqual(persisted["usage_accounting_schema"], 1)
            self.assertEqual(persisted["overlay"]["usage_accounting_schema"], 1)
            self.assertFalse(app._live_catchup_lock.locked())

    def test_schema_less_catchup_does_not_overwrite_newer_accounting_state(self) -> None:
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        app.state = self.state(tokens=120, requests=2)
        app.state.client_usage["usage_accounting_schema"] = 1
        app.state.client_usage["cost"] = 1.2
        app.state.today_account_cost = 1.2
        app._live_usage_overlay = None
        app._live_catchup_lock = threading.Lock()
        app._live_catchup_lock.acquire()
        app.closed = False
        app._apply_live_usage_catchup(
            {
                "through": datetime.now(timezone.utc).isoformat(),
                "events": [],
                "summary": {
                    "tokens": 500,
                    "requests": 5,
                    "cost": 5.0,
                },
                "providers": [],
            }
        )

        self.assertEqual(app.state.today_tokens, 120)
        self.assertEqual(app.state.today_requests, 2)
        self.assertAlmostEqual(app.state.today_account_cost, 1.2)
        self.assertIsNone(app._live_usage_overlay)
        self.assertFalse(app._live_catchup_lock.locked())

    def test_catchup_event_updates_its_hourly_bucket(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = Path(directory) / "live-checkpoint.json"
            app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
            app.state = self.state()
            app._live_usage_overlay = None
            app._live_usage_seen_ids = {}
            app._live_usage_event_records = {}
            app._live_usage_verification_pending = False
            app._live_usage_verification_latest_when = None
            app._live_usage_verification_pending_tokens = 0
            app._live_usage_rate_samples = []
            app._last_live_checkpoint_write_at = float("-inf")
            app._live_catchup_lock = threading.Lock()
            app._live_catchup_lock.acquire()
            app._live_initial_recheck_scheduled = True
            app.closed = False
            app._draw = lambda: None
            when = datetime.now(timezone.utc)
            payload = {
                "through": (when + timedelta(seconds=1)).isoformat(),
                "events": [
                    {
                        "event_id": "catchup-hourly-event",
                        "when": when.isoformat(),
                        "total_tokens": 50,
                        "input_tokens": 40,
                        "cached_tokens": 20,
                        "output_tokens": 10,
                        "cost": 0.5,
                    }
                ],
                "summary": {
                    "tokens": 150,
                    "requests": 3,
                    "cost": 0.5,
                    "input_tokens": 100,
                    "cached_input_tokens": 50,
                    "output_tokens": 20,
                    "latest_at": when.isoformat(),
                    "latest_model": "gpt-test",
                },
                "providers": [],
            }

            with patch.object(monitor, "LIVE_USAGE_CHECKPOINT_JSON", checkpoint):
                app._apply_live_usage_catchup(payload)

            summary = app._usage_range_summary("24h")
            hour = when.astimezone(monitor.CN_TZ).hour
            bucket = next(row for row in summary["series"] if row["hour"] == hour)
            self.assertEqual(bucket["tokens"], 150)
            self.assertEqual(bucket["requests"], 3)

    def test_catchup_keeps_full_tail_model_call(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = Path(directory) / "live-checkpoint.json"
            app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
            app.state = self.state(tokens=100, requests=1)
            app.state.client_usage["providers"] = [
                {
                    "name": "Codex local - account@example.com",
                    "tokens": 100,
                    "requests": 1,
                    "cost": 1.0,
                }
            ]
            app.state.top_accounts = [
                {
                    "name": "Codex local - account@example.com",
                    "tokens": 100,
                    "requests": 1,
                    "cost": 1.0,
                }
            ]
            app._live_usage_overlay = None
            app._live_usage_seen_ids = {}
            app._live_usage_event_records = {}
            app._live_usage_verification_pending = False
            app._live_usage_verification_latest_when = None
            app._live_usage_verification_pending_tokens = 0
            app._live_usage_rate_samples = []
            app._last_live_checkpoint_write_at = float("-inf")
            app._live_catchup_lock = threading.Lock()
            app._live_catchup_lock.acquire()
            app._live_initial_recheck_scheduled = True
            app.closed = False
            app._draw = lambda: None

            through = datetime.now(timezone.utc) - timedelta(seconds=1)
            tail = {
                "event_id": "request-1-tail",
                "when": through + timedelta(milliseconds=500),
                "request_key": "request-1",
                "route": "cockpit-request",
                "provider": "Codex local - account@example.com",
                "total_tokens": 180,
                "input_tokens": 180,
                "cached_tokens": 0,
                "output_tokens": 0,
                "cost": 1.8,
            }
            app._live_usage_event_records = {tail["event_id"]: tail}
            payload = {
                "through": through.isoformat(),
                "events": [
                    {
                        "event_id": "request-1-baseline",
                        "when": (through - timedelta(seconds=1)).isoformat(),
                        "request_key": "request-1",
                        "route": "cockpit-request",
                        "provider": "Codex local - account@example.com",
                        "total_tokens": 150,
                        "input_tokens": 150,
                        "cached_tokens": 0,
                        "output_tokens": 0,
                        "cost": 1.5,
                    }
                ],
                "summary": {
                    "tokens": 150,
                    "requests": 2,
                    "cost": 1.5,
                    "input_tokens": 150,
                    "cached_input_tokens": 0,
                    "output_tokens": 0,
                    "latest_at": (through - timedelta(seconds=1)).isoformat(),
                    "latest_model": "gpt-test",
                },
                "providers": [
                    {
                        "name": "Codex local - account@example.com",
                        "tokens": 150,
                        "requests": 2,
                        "cost": 1.5,
                        "input_tokens": 150,
                        "cached_input_tokens": 0,
                        "output_tokens": 0,
                    }
                ],
            }

            with patch.object(monitor, "LIVE_USAGE_CHECKPOINT_JSON", checkpoint):
                app._apply_live_usage_catchup(payload)

            self.assertEqual(app.state.today_tokens, 330)
            self.assertEqual(app.state.today_requests, 3)
            self.assertEqual(
                app._live_usage_overlay["catchup_tail_tokens"],
                180,
            )
            self.assertEqual(
                app.state.client_usage["providers"][0]["tokens"],
                330,
            )
            self.assertEqual(
                app.state.client_usage["providers"][0]["requests"],
                3,
            )

    def test_catchup_keeps_latest_provider_and_model_from_one_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = Path(directory) / "live-checkpoint.json"
            app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
            app.state = self.state(tokens=100, requests=1)
            app.state.client_usage["api_service_routed"] = True
            app.state.client_usage["providers"] = [
                {
                    "name": "Codex local - plus@example.com",
                    "tokens": 100,
                    "requests": 1,
                    "cost": 1.0,
                }
            ]
            app.state.top_accounts = [
                {
                    "name": "Codex local - plus@example.com",
                    "tokens": 100,
                    "requests": 1,
                    "cost": 1.0,
                }
            ]
            app.state.latest_account_name = "Grok local"
            previous_at = datetime.now(timezone.utc) - timedelta(minutes=1)
            app.state.latest_request = {
                "kind": "success",
                "provider": "Grok local",
                "model": "grok-4.5-build",
                "created_at": previous_at.isoformat(timespec="seconds"),
            }
            app._live_usage_overlay = None
            app._live_usage_seen_ids = {}
            app._live_usage_event_records = {}
            app._live_usage_verification_pending = False
            app._live_usage_verification_latest_when = None
            app._live_usage_verification_pending_tokens = 0
            app._live_usage_rate_samples = []
            app._last_live_checkpoint_write_at = float("-inf")
            app._live_catchup_lock = threading.Lock()
            app._live_catchup_lock.acquire()
            app._live_initial_recheck_scheduled = True
            app.closed = False
            app._draw = lambda: None

            latest_when = datetime.now(timezone.utc)
            payload = {
                "through": latest_when.isoformat(),
                "events": [],
                "summary": {
                    "tokens": 150,
                    "requests": 2,
                    "cost": 1.5,
                    "input_tokens": 100,
                    "cached_input_tokens": 30,
                    "output_tokens": 20,
                    "latest_at": latest_when.isoformat(),
                    "latest_model": "gpt-5.6-sol",
                },
                "providers": [
                    {
                        "name": "Codex local - plus@example.com",
                        "tokens": 150,
                        "requests": 2,
                        "cost": 1.5,
                        "latest_at": latest_when.isoformat(),
                        "latest_model": "gpt-5.6-sol",
                    }
                ],
            }

            with patch.object(monitor, "LIVE_USAGE_CHECKPOINT_JSON", checkpoint):
                app._apply_live_usage_catchup(payload)

            self.assertEqual(
                app.state.latest_request["provider"],
                "Codex local - plus@example.com",
            )
            self.assertEqual(app.state.latest_request["model"], "gpt-5.6-sol")
            self.assertEqual(
                app.state.latest_account_name,
                "Codex local - plus@example.com",
            )

    def test_live_event_updates_recent_request_from_matching_session(self) -> None:
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        app.state = self.state()
        app.state.client_usage["active_sessions"] = [
            {
                "session_id": "session-1",
                "provider": "Codex local - current@example.com",
                "model": "gpt-current",
                "active": True,
            }
        ]
        app._live_usage_overlay = None
        event = self.event()
        event["session_id"] = "session-1"

        with patch.object(
            monitor,
            "_current_codex_account_label",
            return_value="Codex local - current@example.com",
        ):
            app._record_live_usage_events([event])

        self.assertEqual(app.state.latest_account_name, "Codex local - current@example.com")
        self.assertEqual(app.state.latest_request["model"], "gpt-current")
        self.assertEqual(
            app.state.latest_request["created_at"],
            event["when"].astimezone(monitor.CN_TZ).isoformat(timespec="seconds"),
        )
        self.assertEqual(app.state.cost_history["today_tokens"], 150)

    def test_live_overlay_does_not_become_unclassified_token_mix(self) -> None:
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        app.state = self.state()
        app._live_usage_overlay = None
        app._record_live_usage_events([self.event()])

        summary = app._usage_range_summary("24h")
        mix = app._summary_token_mix(summary)

        self.assertEqual(summary["label"], "今日")
        self.assertEqual(summary["tokens"], 150)
        self.assertEqual(summary["breakdown_tokens"], 150)
        self.assertEqual(mix["input"], 80)
        self.assertEqual(mix["cached"], 50)
        self.assertEqual(mix["output"], 20)
        self.assertEqual(mix["unknown"], 0)

    def test_authoritative_unknown_is_preserved_during_live_overlay(self) -> None:
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        app.state = self.state(tokens=120)
        app._live_usage_overlay = None
        app._record_live_usage_events([self.event()])

        summary = app._usage_range_summary("24h")
        mix = app._summary_token_mix(summary)

        self.assertEqual(summary["tokens"], 170)
        self.assertEqual(mix["unknown"], 20)

    def test_live_overlay_respects_authoritative_single_event_limit(self) -> None:
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        app.state = self.state()
        app._live_usage_overlay = None
        event = self.event()
        event["total_tokens"] = monitor.LIVE_USAGE_MAX_SINGLE_EVENT_TOKENS + 1

        self.assertFalse(app._record_live_usage_events([event]))
        self.assertEqual(app.state.today_tokens, 100)

    def test_cached_state_keeps_overlay_until_authoritative_total_catches_up(self) -> None:
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        app.state = self.state()
        app._live_usage_overlay = None
        app._record_live_usage_events([self.event()])
        cached = self.state()

        self.assertFalse(app._authoritative_state_covers_live_overlay(cached))
        app._apply_live_usage_overlay(cached)
        self.assertEqual(cached.today_tokens, 150)

        authoritative = self.state(tokens=150, requests=3, fresh=True)
        self.assertTrue(app._authoritative_state_covers_live_overlay(authoritative))

    def test_optimistic_overlay_is_not_written_to_usage_history(self) -> None:
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        app.state = self.state()
        app._live_usage_overlay = None
        app._record_live_usage_events([self.event()])
        app._refresh_pending = False
        app._refresh_pending_force = False
        app._refresh_pending_usage = False
        app._refresh_lock = threading.Lock()
        app._refresh_lock.acquire()
        app._loading = True
        app.closed = False
        app.error = None
        app._last_quota_refresh_at = 123.0
        app._draw = lambda: None
        captured: list[int] = []

        def update_history(state: monitor.MonitorState) -> dict:
            captured.append(state.today_tokens)
            return {}

        with patch.object(monitor, "update_usage_history", side_effect=update_history):
            app._apply_state(self.state())

        self.assertEqual(captured, [100])
        self.assertEqual(app.state.today_tokens, 150)
        self.assertEqual(app._last_quota_refresh_at, 123.0)

    def test_live_change_does_not_start_a_full_export(self) -> None:
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        app.state = self.state()
        app._live_usage_overlay = None
        app._live_usage_lock = threading.Lock()
        app._live_usage_lock.acquire()
        app.closed = False
        app._draw = lambda: None
        app.refresh_async = lambda *args, **kwargs: self.fail("live change started a full export")

        app._apply_live_usage_change(True, [self.event()])

        self.assertEqual(app.state.today_tokens, 150)
        self.assertFalse(app._live_usage_lock.locked())

    def test_runtime_spike_draws_verification_status_and_schedules_reconciliation(self) -> None:
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        app.state = self.state()
        app._live_usage_overlay = None
        app._live_usage_seen_ids = {}
        app._live_usage_event_records = {}
        app._live_usage_rate_samples = []
        app._live_usage_verification_pending = False
        app._live_usage_verification_latest_when = None
        app._live_usage_verification_pending_tokens = 0
        app._live_usage_lock = threading.Lock()
        app._live_usage_lock.acquire()
        app.closed = False
        app._draw = MagicMock()
        app._schedule_live_usage_reconcile = MagicMock(return_value=True)

        app._apply_live_usage_change(
            True,
            self.events_with_total(monitor.LIVE_USAGE_VERIFY_THRESHOLD_TOKENS),
        )

        self.assertEqual(app.state.today_tokens, 100)
        self.assertTrue(app._live_usage_verification_pending)
        app._schedule_live_usage_reconcile.assert_called_once_with()
        app._draw.assert_called_once_with()
        self.assertFalse(app._live_usage_lock.locked())

    def test_accumulated_unverified_tokens_schedule_reconciliation(self) -> None:
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        app.state = self.state()
        app._live_usage_overlay = {
            "providers": {
                "Codex local - account@example.com": {
                    "tokens": monitor.LIVE_USAGE_VERIFY_THRESHOLD_TOKENS - 50,
                }
            }
        }
        app._live_usage_seen_ids = {}
        app._live_usage_event_records = {}
        app._live_usage_rate_samples = []
        app._live_usage_verification_pending = False
        app._live_usage_verification_latest_when = None
        app._live_usage_verification_pending_tokens = 0
        app._live_usage_lock = threading.Lock()
        app._live_usage_lock.acquire()
        app.closed = False
        app._draw = MagicMock()
        app._schedule_live_usage_reconcile = MagicMock(return_value=True)

        app._apply_live_usage_change(True, [self.event()])

        self.assertTrue(app._live_usage_verification_pending)
        self.assertEqual(
            app._live_usage_verification_pending_tokens,
            monitor.LIVE_USAGE_VERIFY_THRESHOLD_TOKENS,
        )
        app._schedule_live_usage_reconcile.assert_called_once_with()
        self.assertFalse(app._live_usage_lock.locked())

    def test_runtime_spike_verification_bypasses_normal_reconcile_cooldown(self) -> None:
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        app.closed = False
        app._live_reconcile_scheduled = True
        app._last_live_reconcile_at = monitor.time.monotonic()
        app._live_usage_verification_pending = True
        app._refresh_live_usage_catchup_async = MagicMock(return_value=True)
        app._schedule_live_usage_reconcile = MagicMock(return_value=True)

        app._run_live_usage_reconcile()

        app._refresh_live_usage_catchup_async.assert_called_once_with()
        app._schedule_live_usage_reconcile.assert_not_called()
        self.assertFalse(app._live_reconcile_scheduled)

    def test_runtime_verification_bypasses_busy_watcher_quiet_period(self) -> None:
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        app.closed = False
        app._live_reconcile_scheduled = True
        app._last_live_reconcile_at = monitor.time.monotonic()
        app._live_usage_verification_pending = True
        watcher = monitor.CodexUsageFileWatcher.__new__(monitor.CodexUsageFileWatcher)
        watcher.reconciliation_ready = MagicMock(return_value=False)
        watcher.mark_reconciled = MagicMock()
        app._live_usage_watcher = watcher
        app._codex_logs_busy = MagicMock(return_value=True)
        app._refresh_live_usage_catchup_async = MagicMock(return_value=True)
        app._schedule_live_usage_reconcile = MagicMock(return_value=True)

        app._run_live_usage_reconcile()

        watcher.reconciliation_ready.assert_not_called()
        watcher.mark_reconciled.assert_called_once_with()
        app._refresh_live_usage_catchup_async.assert_called_once_with()
        app._schedule_live_usage_reconcile.assert_not_called()

    def test_busy_live_reconcile_defers_heavy_catchup(self) -> None:
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        app.closed = False
        app._live_reconcile_scheduled = True
        app._last_live_reconcile_at = float("-inf")
        app._live_usage_verification_pending = False
        app._codex_logs_busy = MagicMock(return_value=True)
        app._refresh_live_usage_catchup_async = MagicMock(return_value=True)
        app._schedule_live_usage_reconcile = MagicMock(return_value=True)

        app._run_live_usage_reconcile()

        app._refresh_live_usage_catchup_async.assert_not_called()
        app._schedule_live_usage_reconcile.assert_called_once_with()
        self.assertFalse(app._live_reconcile_scheduled)

    def test_busy_startup_defers_catchup_until_idle(self) -> None:
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        app.closed = False
        app._full_refresh_requested = False
        app._codex_logs_busy = MagicMock(return_value=True)
        app._refresh_live_usage_catchup_async = MagicMock(return_value=True)

        app._start_initial_live_catchup()

        app._refresh_live_usage_catchup_async.assert_not_called()
        self.assertTrue(app._full_refresh_requested)

    def test_live_catchup_waits_for_full_usage_export(self) -> None:
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        app._refresh_lock = threading.Lock()
        app._refresh_lock.acquire()
        app._live_catchup_lock = threading.Lock()
        app._live_usage_catchup_since = MagicMock(
            return_value=datetime.now(monitor.CN_TZ) - timedelta(seconds=1)
        )

        started = app._refresh_live_usage_catchup_async()

        self.assertFalse(started)
        self.assertFalse(app._live_catchup_lock.locked())
        app._refresh_lock.release()

    def test_new_rollout_schedules_lightweight_reconciliation(self) -> None:
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        app.state = self.state()
        app._live_usage_overlay = None
        app._live_usage_lock = threading.Lock()
        app._live_usage_lock.acquire()
        app._live_usage_watcher = monitor.CodexUsageFileWatcher(Path("unused"))
        app._live_usage_watcher.reconciliation_needed = True
        app.closed = False
        app._draw = lambda: None
        app._schedule_live_usage_reconcile = MagicMock(return_value=True)
        app._full_refresh_requested = False

        app._apply_live_usage_change(True, [])

        app._schedule_live_usage_reconcile.assert_called_once_with()
        self.assertFalse(app._full_refresh_requested)
        self.assertFalse(app._live_usage_lock.locked())

    def test_recent_active_session_defers_automatic_full_export(self) -> None:
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        app.state = self.state()
        app.state.client_usage["active_sessions"] = [
            {
                "session_id": "session-1",
                "active": True,
                "latest_at": datetime.now(timezone.utc).isoformat(),
            }
        ]

        self.assertTrue(app._codex_logs_busy())

        app.state.client_usage["active_sessions"][0]["latest_at"] = (
            datetime.now(timezone.utc)
            - timedelta(seconds=monitor.LIVE_USAGE_EXPORT_IDLE_SECONDS + 1)
        ).isoformat()
        self.assertFalse(app._codex_logs_busy())

    def test_quota_snapshot_updates_percentages_without_replacing_window_usage(self) -> None:
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        app.state = self.state()
        app.state.client_usage["providers"] = [
            {
                "name": "Codex local - account@example.com",
                "window_5h": {"tokens": 12_345, "remaining_percent": 90.0},
                "window_7d": {"tokens": 67_890, "remaining_percent": 80.0},
            }
        ]
        app.state.top_accounts = [
            {
                "name": "account@example.com",
                "window_5h": {"tokens": 12_345, "remaining_percent": 90.0},
                "window_7d": {"tokens": 67_890, "remaining_percent": 80.0},
            }
        ]
        app.closed = False
        app._quota_refresh_lock = threading.Lock()
        app._quota_refresh_lock.acquire()
        app._draw = lambda: None
        payload = {
            "accounts": {
                "Codex local - account@example.com": {
                    "window_5h": {
                        "quota_available": True,
                        "remaining_percent": 72.0,
                        "utilization": 28.0,
                        "resets_at": "2026-07-15T14:00:00+08:00",
                    },
                    "window_7d": {
                        "quota_available": True,
                        "remaining_percent": 61.0,
                        "utilization": 39.0,
                        "resets_at": "2026-07-20T14:00:00+08:00",
                    },
                }
            }
        }

        app._apply_quota_snapshot(payload)

        provider = app.state.client_usage["providers"][0]
        account = app.state.top_accounts[0]
        self.assertEqual(provider["window_5h"]["tokens"], 12_345)
        self.assertEqual(provider["window_7d"]["tokens"], 67_890)
        self.assertEqual(account["window_5h"]["remaining_percent"], 72.0)
        self.assertEqual(account["window_7d"]["remaining_percent"], 61.0)
        self.assertTrue(app._full_refresh_requested)
        self.assertFalse(app._quota_refresh_lock.locked())

    def test_new_quota_boundary_removes_previous_cycle_usage_immediately(self) -> None:
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        app.state = self.state()
        app.state.client_usage["providers"] = [
            {
                "name": "Codex local - account@example.com",
                "window_7d": {
                    "requests": 20,
                    "tokens": 2_000_000,
                    "cost": 2.0,
                    "models": {"gpt-5.6-sol": 2_000_000},
                    "start_at": "2026-08-10T12:00:00+08:00",
                    "end_at": "2026-08-17T11:59:00+08:00",
                    "quota_available": True,
                    "window_minutes": 10_080,
                    "resets_at": "2026-08-17T12:00:00+08:00",
                    "remaining_percent": 5.0,
                    "utilization": 95.0,
                },
            }
        ]
        app.state.top_accounts = [
            {
                "name": "account@example.com",
                "window_7d": {
                    "requests": 20,
                    "tokens": 2_000_000,
                    "cost": 2.0,
                    "models": {"gpt-5.6-sol": 2_000_000},
                    "start_at": "2026-08-10T12:00:00+08:00",
                    "end_at": "2026-08-17T11:59:00+08:00",
                    "quota_available": True,
                    "window_minutes": 10_080,
                    "resets_at": "2026-08-17T12:00:00+08:00",
                    "remaining_percent": 5.0,
                    "utilization": 95.0,
                },
            }
        ]
        app.closed = False
        app._quota_refresh_lock = threading.Lock()
        app._quota_refresh_lock.acquire()
        app._live_usage_overlay = None
        app._live_usage_event_records = {}
        app._full_refresh_requested = False
        app._schedule_live_usage_reconcile = MagicMock(return_value=True)
        app._draw = lambda: None

        app._apply_quota_snapshot(
            {
                "accounts": {
                    "Codex local - account@example.com": {
                        "window_7d": {
                            "quota_available": True,
                            "window_minutes": 10_080,
                            "resets_at": "2026-08-24T12:00:00+08:00",
                            "remaining_percent": 100.0,
                            "utilization": 0.0,
                            "quota_snapshot_at": "2026-08-17T12:00:05+08:00",
                        }
                    }
                }
            }
        )

        window = app.state.client_usage["providers"][0]["window_7d"]
        self.assertEqual(window["tokens"], 0)
        self.assertEqual(window["requests"], 0)
        self.assertEqual(window["cost"], 0.0)
        self.assertEqual(window["models"], {})
        self.assertEqual(window["start_at"], "2026-08-17T12:00:00+08:00")
        self.assertEqual(window["end_at"], "2026-08-17T12:00:00+08:00")
        self.assertEqual(window["remaining_percent"], 100.0)
        top_window = app.state.top_accounts[0]["window_7d"]
        self.assertEqual(top_window["tokens"], 0)
        self.assertEqual(top_window["remaining_percent"], 100.0)
        self.assertEqual(top_window["resets_at"], "2026-08-24T12:00:00+08:00")
        self.assertTrue(app._full_refresh_requested)
        expected_start = monitor._parse_time("2026-08-17T12:00:00+08:00")
        self.assertEqual(app._live_quota_reconcile_since, expected_start)
        self.assertTrue(app._live_usage_verification_pending)
        app._schedule_live_usage_reconcile.assert_called_once_with(
            monitor.LIVE_USAGE_VERIFY_DELAY_MS
        )

    def test_new_quota_boundary_replays_events_before_quota_snapshot(self) -> None:
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        app.state = self.state()
        provider = "Codex local - account@example.com"
        app.state.client_usage["providers"] = [
            {
                "name": provider,
                "window_7d": {
                    "requests": 20,
                    "tokens": 2_000_000,
                    "cost": 2.0,
                    "models": {"gpt-5.6-sol": 2_000_000},
                    "start_at": "2026-08-10T12:00:00+08:00",
                    "end_at": "2026-08-17T11:59:00+08:00",
                    "quota_available": True,
                    "window_minutes": 10_080,
                    "resets_at": "2026-08-17T12:00:00+08:00",
                },
            }
        ]
        app.state.top_accounts = []
        app.closed = False
        app._quota_refresh_lock = threading.Lock()
        app._quota_refresh_lock.acquire()
        app._live_usage_overlay = {}
        app._full_refresh_requested = False
        app._draw = lambda: None
        first_when = datetime(2026, 8, 17, 12, 5, tzinfo=monitor.CN_TZ)
        second_when = datetime(2026, 8, 17, 12, 15, tzinfo=monitor.CN_TZ)
        app._live_usage_event_records = {
            "first": {
                "event_id": "first",
                "provider": provider,
                "when": first_when,
                "model": "gpt-5.6-sol",
                "total_tokens": 120,
                "input_tokens": 100,
                "cached_tokens": 40,
                "output_tokens": 20,
                "cost": 0.12,
            },
            "second": {
                "event_id": "second",
                "provider": provider,
                "when": second_when,
                "model": "gpt-5.6-sol",
                "total_tokens": 230,
                "input_tokens": 200,
                "cached_tokens": 60,
                "output_tokens": 30,
                "cost": 0.23,
            },
        }

        app._apply_quota_snapshot(
            {
                "accounts": {
                    provider: {
                        "window_7d": {
                            "quota_available": True,
                            "window_minutes": 10_080,
                            "resets_at": "2026-08-24T12:00:00+08:00",
                            "remaining_percent": 85.0,
                            "utilization": 15.0,
                            "quota_snapshot_at": "2026-08-17T12:20:00+08:00",
                        }
                    }
                }
            }
        )

        window = app.state.client_usage["providers"][0]["window_7d"]
        self.assertEqual(window["tokens"], 350)
        self.assertEqual(window["requests"], 2)
        self.assertAlmostEqual(window["cost"], 0.35)
        self.assertEqual(window["end_at"], "2026-08-17T12:15:00+08:00")
        self.assertEqual(window["remaining_percent"], 85.0)

    def test_forced_catchup_rebuilds_new_quota_cycle_from_local_events(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = Path(directory) / "live-checkpoint.json"
            provider = "Codex local - account@example.com"
            through = datetime.now(timezone.utc).replace(microsecond=0)
            new_start = through - timedelta(minutes=30)
            new_reset = new_start + timedelta(days=7)
            old_start = new_start - timedelta(days=7)
            old_reset = new_start

            app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
            app.state = self.state(tokens=1_000, requests=10)
            app.state.client_usage.update(
                {
                    "cost": 10.0,
                    "models": {"gpt-5.6-sol": 1_000},
                    "providers": [
                        {
                            "name": provider,
                            "tokens": 1_000,
                            "requests": 10,
                            "cost": 10.0,
                            "models": {"gpt-5.6-sol": 1_000},
                            "window_7d": {
                                "tokens": 50,
                                "requests": 1,
                                "cost": 0.5,
                                "models": {"gpt-5.6-sol": 50},
                                "start_at": new_start.isoformat(),
                                "end_at": (new_start + timedelta(minutes=2)).isoformat(),
                                "quota_available": True,
                                "window_minutes": 10_080,
                                "resets_at": new_reset.isoformat(),
                                "remaining_percent": 90.0,
                            },
                        }
                    ],
                }
            )
            top_account = copy.deepcopy(app.state.client_usage["providers"][0])
            top_account["name"] = "account@example.com"
            app.state.top_accounts = [top_account]
            app.state.today_account_cost = 10.0
            app._live_usage_overlay = None
            app._live_usage_seen_ids = {}
            app._live_usage_event_records = {}
            app._live_usage_event_aliases = {}
            app._live_usage_reconciled_ids = {}
            app._live_usage_verification_pending = True
            app._live_usage_verification_latest_when = new_start
            app._live_usage_verification_pending_tokens = 0
            app._live_quota_reconcile_since = new_start
            app._live_usage_rate_samples = []
            app._last_live_checkpoint_write_at = float("-inf")
            app._live_catchup_lock = threading.Lock()
            app._live_catchup_lock.acquire()
            app.closed = False
            app.root = object()
            app._draw = lambda: None

            authoritative_usage = {
                "tokens": 1_000,
                "requests": 10,
                "cost": 10.0,
                "models": {"gpt-5.6-sol": 1_000},
                "providers": [
                    {
                        "name": provider,
                        "tokens": 1_000,
                        "requests": 10,
                        "cost": 10.0,
                        "models": {"gpt-5.6-sol": 1_000},
                        "window_7d": {
                            "tokens": 800,
                            "requests": 8,
                            "cost": 8.0,
                            "models": {"gpt-5.6-sol": 800},
                            "start_at": old_start.isoformat(),
                            "end_at": (old_reset - timedelta(seconds=1)).isoformat(),
                            "quota_available": True,
                            "window_minutes": 10_080,
                            "resets_at": old_reset.isoformat(),
                        },
                    }
                ],
            }
            rows = [
                {
                    "event_id": "new-cycle-1",
                    "when": (new_start + timedelta(minutes=5)).isoformat(),
                    "provider": provider,
                    "model": "gpt-5.6-sol",
                    "total_tokens": 120,
                    "input_tokens": 100,
                    "cached_tokens": 40,
                    "output_tokens": 20,
                    "cost": 1.2,
                },
                {
                    "event_id": "new-cycle-2",
                    "when": (new_start + timedelta(minutes=15)).isoformat(),
                    "provider": provider,
                    "model": "gpt-5.6-sol",
                    "total_tokens": 230,
                    "input_tokens": 200,
                    "cached_tokens": 60,
                    "output_tokens": 30,
                    "cost": 2.3,
                },
            ]
            payload = {
                "since": new_start.isoformat(),
                "through": through.isoformat(),
                "events": rows,
                "summary": {
                    "tokens": 1_350,
                    "requests": 12,
                    "cost": 13.5,
                    "models": {"gpt-5.6-sol": 1_350},
                    "input_tokens": 1_000,
                    "cached_input_tokens": 200,
                    "output_tokens": 150,
                    "latest_at": rows[-1]["when"],
                    "latest_model": "gpt-5.6-sol",
                },
                "providers": [
                    {
                        "name": provider,
                        "tokens": 1_350,
                        "requests": 12,
                        "cost": 13.5,
                        "models": {"gpt-5.6-sol": 1_350},
                        "input_tokens": 1_000,
                        "cached_input_tokens": 200,
                        "output_tokens": 150,
                        "latest_at": rows[-1]["when"],
                        "latest_model": "gpt-5.6-sol",
                    }
                ],
            }

            with (
                patch.object(monitor, "LIVE_USAGE_CHECKPOINT_JSON", checkpoint),
                patch.object(
                    monitor,
                    "load_client_usage",
                    return_value=authoritative_usage,
                ),
            ):
                app._apply_live_usage_catchup(payload)

            window = app.state.client_usage["providers"][0]["window_7d"]
            self.assertEqual(window["tokens"], 350)
            self.assertEqual(window["requests"], 2)
            self.assertAlmostEqual(window["cost"], 3.5)
            self.assertEqual(window["start_at"], new_start.astimezone(monitor.CN_TZ).isoformat(timespec="seconds"))
            top_window = app.state.top_accounts[0]["window_7d"]
            self.assertEqual(top_window["tokens"], 350)
            self.assertEqual(top_window["requests"], 2)
            self.assertAlmostEqual(top_window["cost"], 3.5)
            self.assertEqual(
                top_window["start_at"],
                new_start.astimezone(monitor.CN_TZ).isoformat(timespec="seconds"),
            )
            self.assertIsNone(app._live_quota_reconcile_since)
            self.assertFalse(app._live_usage_verification_pending)
            self.assertFalse(app._live_catchup_lock.locked())

    def test_catchup_started_after_quota_boundary_does_not_clear_rebuild(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = Path(directory) / "live-checkpoint.json"
            app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
            app.state = self.state(tokens=100, requests=2)
            app._live_usage_overlay = None
            app._live_usage_seen_ids = {}
            app._live_usage_event_records = {}
            app._live_usage_event_aliases = {}
            app._live_usage_reconciled_ids = {}
            app._live_usage_rate_samples = []
            app._last_live_checkpoint_write_at = float("-inf")
            app._live_catchup_lock = threading.Lock()
            app._live_catchup_lock.acquire()
            app.closed = False
            app.root = object()
            app._draw = lambda: None
            boundary = datetime.now(timezone.utc) - timedelta(minutes=10)
            through = datetime.now(timezone.utc)
            app._live_quota_reconcile_since = boundary
            app._live_usage_verification_pending = True
            app._live_usage_verification_latest_when = boundary
            app._live_usage_verification_pending_tokens = 0
            app._schedule_live_usage_reconcile = MagicMock(return_value=True)
            payload = {
                "since": (boundary + timedelta(minutes=5)).isoformat(),
                "through": through.isoformat(),
                "events": [],
                "summary": {
                    "tokens": 100,
                    "requests": 2,
                    "cost": 0.0,
                    "input_tokens": 60,
                    "cached_input_tokens": 30,
                    "output_tokens": 10,
                },
                "providers": [],
            }

            with (
                patch.object(monitor, "LIVE_USAGE_CHECKPOINT_JSON", checkpoint),
                patch.object(
                    monitor,
                    "load_client_usage",
                    return_value=dict(app.state.client_usage),
                ),
            ):
                app._apply_live_usage_catchup(payload)

            self.assertEqual(app._live_quota_reconcile_since, boundary)
            self.assertTrue(app._live_usage_verification_pending)
            app._schedule_live_usage_reconcile.assert_called_once_with(
                monitor.LIVE_USAGE_VERIFY_DELAY_MS
            )
            self.assertFalse(app._live_catchup_lock.locked())

    def test_live_official_event_updates_matching_7d_quota_window(self) -> None:
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        app.state = self.state(tokens=1_000, requests=2)
        now = datetime.now(timezone.utc).replace(microsecond=0)
        start_at = now - timedelta(days=1)
        reset_at = start_at + timedelta(days=7)
        window = {
            "requests": 2,
            "tokens": 1_000,
            "input_tokens": 600,
            "cached_input_tokens": 300,
            "cache_creation_input_tokens": 0,
            "output_tokens": 100,
            "cost": 1.5,
            "models": {"gpt-5.6-sol": 1_000},
            "unpriced_tokens": 0,
            "unpriced_models": {},
            "start_at": start_at.isoformat(),
            "end_at": (now - timedelta(seconds=10)).isoformat(),
            "latest_at": (now - timedelta(seconds=10)).isoformat(),
            "quota_available": True,
            "quota_unlimited": False,
            "window_minutes": 10_080,
            "resets_at": reset_at.isoformat(),
            "remaining_percent": 10.0,
            "utilization": 90.0,
        }
        app.state.client_usage["providers"] = [
            {
                "name": "Codex local - hyenas@example.com",
                "window_7d": copy.deepcopy(window),
            }
        ]
        app.state.top_accounts = [
            {
                "name": "hyenas@example.com",
                "window_7d": copy.deepcopy(window),
            }
        ]
        app._live_usage_overlay = {
            "base_today_tokens": 1_000,
            "base_today_requests": 2,
            "base_today_cost": 1.5,
            "tokens": 0,
            "requests": 0,
            "cost": 0.0,
            "providers": {},
            "base_hourly": [],
            "hourly": {},
        }
        event = {
            "event_id": "hyenas-live-event",
            "model": "gpt-5.6-sol",
            "total_tokens": 50,
            "input_tokens": 40,
            "cached_tokens": 20,
            "output_tokens": 10,
            "cost": 0.25,
            "unpriced_tokens": 0,
        }

        app._record_live_quota_window_overlay(
            app._live_usage_overlay,
            "Codex local - hyenas@example.com",
            event,
            now,
        )
        # An overlapping catch-up must remain idempotent.
        app._record_live_quota_window_overlay(
            app._live_usage_overlay,
            "Codex local - hyenas@example.com",
            event,
            now,
        )
        app._apply_live_quota_window_overlay(app.state)

        for row in (
            app.state.client_usage["providers"][0],
            app.state.top_accounts[0],
        ):
            live_window = row["window_7d"]
            self.assertEqual(live_window["tokens"], 1_050)
            self.assertEqual(live_window["requests"], 3)
            self.assertEqual(live_window["input_tokens"], 620)
            self.assertEqual(live_window["cached_input_tokens"], 320)
            self.assertEqual(live_window["output_tokens"], 110)
            self.assertAlmostEqual(live_window["cost"], 1.75)
            self.assertEqual(live_window["models"]["gpt-5.6-sol"], 1_050)
            self.assertEqual(live_window["remaining_percent"], 10.0)
            self.assertTrue(live_window["window_usage_live"])

    def test_external_or_out_of_window_event_does_not_enter_official_quota(self) -> None:
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        app.state = self.state(tokens=1_000, requests=2)
        now = datetime.now(timezone.utc).replace(microsecond=0)
        start_at = now - timedelta(hours=1)
        reset_at = start_at + timedelta(days=7)
        app.state.client_usage["providers"] = [
            {
                "name": "Codex local - account@example.com",
                "window_7d": {
                    "tokens": 1_000,
                    "requests": 2,
                    "start_at": start_at.isoformat(),
                    "end_at": (now - timedelta(seconds=10)).isoformat(),
                    "quota_available": True,
                    "window_minutes": 10_080,
                    "resets_at": reset_at.isoformat(),
                },
            }
        ]
        app.state.top_accounts = []
        overlay: dict = {}
        base_event = {
            "total_tokens": 50,
            "input_tokens": 40,
            "cached_tokens": 20,
            "output_tokens": 10,
            "cost": 0.25,
        }

        app._record_live_quota_window_overlay(
            overlay,
            "Codex local - account@example.com",
            {**base_event, "event_id": "grok-event", "model": "xai/grok-4.6"},
            now,
        )
        app._record_live_quota_window_overlay(
            overlay,
            "Codex local - account@example.com",
            {**base_event, "event_id": "old-event", "model": "gpt-5.6-sol"},
            start_at - timedelta(seconds=1),
        )

        self.assertNotIn("quota_windows", overlay)

    def test_busy_auto_refresh_defers_stale_full_usage_snapshot(self) -> None:
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        app.state = self.state()
        app.state.usage_source = "local"
        app.state.client_usage["updated_at"] = (
            datetime.now(timezone.utc)
            - timedelta(seconds=monitor.FULL_USAGE_REFRESH_MAX_STALE_SECONDS + 1)
        ).isoformat()
        app.closed = False
        app.root = MagicMock()
        app._refresh_lock = threading.Lock()
        app._quota_refresh_lock = threading.Lock()
        app._full_refresh_requested = False
        app._last_forced_full_refresh_at = float("-inf")
        app._handle_day_rollover = MagicMock()
        app._refresh_quota_async = MagicMock(return_value=False)
        app._codex_logs_busy = MagicMock(return_value=True)
        app.refresh_async = MagicMock(return_value=True)

        app._schedule_auto_refresh()

        app.refresh_async.assert_not_called()
        self.assertEqual(app._last_forced_full_refresh_at, float("-inf"))
        app.root.after.assert_called_once()

    def test_idle_without_pending_usage_does_not_repeat_full_export(self) -> None:
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        app.state = self.state()
        app.state.usage_source = "local"
        app.state.client_usage["updated_at"] = datetime.now(timezone.utc).isoformat()
        app.closed = False
        app.root = MagicMock()
        app._refresh_lock = threading.Lock()
        app._quota_refresh_lock = threading.Lock()
        app._full_refresh_requested = False
        app._last_forced_full_refresh_at = monitor.time.monotonic()
        app._live_usage_overlay = None
        app._handle_day_rollover = MagicMock()
        app._refresh_quota_async = MagicMock(return_value=False)
        app._codex_logs_busy = MagicMock(return_value=False)
        app.refresh_async = MagicMock(return_value=True)

        app._schedule_auto_refresh()

        app.refresh_async.assert_not_called()
        app.root.after.assert_called_once()

    def test_busy_auto_refresh_still_schedules_lightweight_quota_sync(self) -> None:
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        app.closed = False
        app.root = MagicMock()
        app._refresh_lock = threading.Lock()
        app._quota_refresh_lock = threading.Lock()
        app._handle_day_rollover = MagicMock()
        app._refresh_quota_async = MagicMock()
        app._codex_logs_busy = MagicMock(return_value=True)
        app.refresh_async = MagicMock()

        app._schedule_auto_refresh()

        app._refresh_quota_async.assert_called_once_with()
        app.refresh_async.assert_not_called()
        app.root.after.assert_called_once()

    def test_full_usage_refresh_does_not_block_lightweight_quota_sync(self) -> None:
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        app.closed = False
        app.root = MagicMock()
        app._refresh_lock = threading.Lock()
        app._refresh_lock.acquire()
        app._quota_refresh_lock = threading.Lock()
        app._live_catchup_lock = threading.Lock()
        app._handle_day_rollover = MagicMock()
        app._refresh_quota_async = MagicMock(return_value=True)
        app._codex_logs_busy = MagicMock(return_value=True)
        app.refresh_async = MagicMock()

        app._schedule_auto_refresh()

        app._refresh_quota_async.assert_called_once_with()
        app.refresh_async.assert_not_called()
        app.root.after.assert_called_once()

    def test_pending_attribution_refresh_waits_while_codex_is_busy(self) -> None:
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        app.state = self.state()
        app.state.usage_source = "local"
        app.state.client_usage["updated_at"] = datetime.now(timezone.utc).isoformat()
        app.closed = False
        app.root = MagicMock()
        app._refresh_lock = threading.Lock()
        app._quota_refresh_lock = threading.Lock()
        app._full_refresh_requested = False
        app._last_forced_full_refresh_at = monitor.time.monotonic()
        app._attribution_refresh_requested = True
        app._last_attribution_refresh_at = float("-inf")
        app._last_cockpit_usage_revision = (101, 2_000, "account-2")
        app._attribution_refresh_inflight_revision = None
        app._live_usage_overlay = None
        app._handle_day_rollover = MagicMock()
        app._refresh_quota_async = MagicMock(return_value=False)
        app._codex_logs_busy = MagicMock(return_value=True)
        app.refresh_async = MagicMock(return_value=True)

        app._schedule_auto_refresh()

        app.refresh_async.assert_not_called()
        self.assertTrue(app._attribution_refresh_requested)
        app.root.after.assert_called_once()

    def test_pending_attribution_refresh_runs_immediately_after_idle(self) -> None:
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        app.state = self.state()
        app.state.usage_source = "local"
        app.state.client_usage["updated_at"] = datetime.now(timezone.utc).isoformat()
        app.closed = False
        app.root = MagicMock()
        app._refresh_lock = threading.Lock()
        app._quota_refresh_lock = threading.Lock()
        app._full_refresh_requested = False
        app._last_forced_full_refresh_at = monitor.time.monotonic()
        app._attribution_refresh_requested = True
        app._last_attribution_refresh_at = float("-inf")
        revision = (101, 2_000, "account-2")
        app._last_cockpit_usage_revision = revision
        app._attribution_refresh_inflight_revision = None
        app._live_usage_overlay = None
        app._handle_day_rollover = MagicMock()
        app._refresh_quota_async = MagicMock(return_value=False)
        app._codex_logs_busy = MagicMock(return_value=False)
        app.refresh_async = MagicMock(return_value=True)

        app._schedule_auto_refresh()

        app.refresh_async.assert_called_once_with(force=True)
        app._refresh_quota_async.assert_not_called()
        self.assertEqual(app._attribution_refresh_inflight_revision, revision)
        app.root.after.assert_called_once()

    def test_token_flow_level_scales_with_recent_token_volume_and_decays(self) -> None:
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        app._token_flow_samples = [(100.0, 20_000)]

        with patch.object(monitor.time, "monotonic", return_value=100.0):
            low_level, low_tokens = app._token_flow_snapshot()
        app._token_flow_samples = [(100.0, 800_000)]
        with patch.object(monitor.time, "monotonic", return_value=100.0):
            high_level, high_tokens = app._token_flow_snapshot()

        self.assertGreater(high_level, low_level)
        self.assertEqual(low_tokens, 20_000)
        self.assertEqual(high_tokens, 800_000)

        with patch.object(monitor.time, "monotonic", return_value=113.0):
            expired_level, expired_tokens = app._token_flow_snapshot()
        self.assertEqual((expired_level, expired_tokens), (0.0, 0))

    def test_account_trace_uses_one_taller_pulse_for_more_tokens(self) -> None:
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        app._token_flow_samples = [(100.0, 20_000)]
        with patch.object(monitor.time, "monotonic", return_value=103.0):
            low_pulses = app._token_flow_trace_pulses(160, 18)

        app._token_flow_samples = [(100.0, 800_000)]
        with patch.object(monitor.time, "monotonic", return_value=103.0):
            high_pulses = app._token_flow_trace_pulses(160, 18)

        self.assertEqual(len(low_pulses), 1)
        self.assertEqual(len(high_pulses), 1)
        self.assertGreater(
            max(height for _x, height, _level in high_pulses),
            max(height for _x, height, _level in low_pulses),
        )

    def test_account_trace_draws_exactly_one_pulse_per_event(self) -> None:
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        app._token_flow_samples = [
            (100.0, 20_000),
            (100.5, 200_000),
            (101.0, 800_000),
        ]

        with patch.object(monitor.time, "monotonic", return_value=103.0):
            pulses = app._token_flow_trace_pulses(160, 18)

        self.assertEqual(len(pulses), 3)

    def test_account_trace_pulses_move_from_left_to_right(self) -> None:
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        app._token_flow_samples = [(100.0, 200_000)]
        with patch.object(monitor.time, "monotonic", return_value=101.0):
            early_pulses = app._token_flow_trace_pulses(160, 18)
        with patch.object(monitor.time, "monotonic", return_value=105.0):
            late_pulses = app._token_flow_trace_pulses(160, 18)

        early_center = sum(x for x, _height, _level in early_pulses) / len(early_pulses)
        late_center = sum(x for x, _height, _level in late_pulses) / len(late_pulses)
        self.assertGreater(late_center, early_center)

    def test_account_trace_keeps_rendered_spacing_stable_between_frames(self) -> None:
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        app._token_flow_samples = [
            (100.001, 200_000),
            (100.146, 300_000),
        ]

        with patch.object(monitor.time, "monotonic", return_value=102.001):
            first_frame = app._token_flow_trace_pulses(160, 18)
        with patch.object(monitor.time, "monotonic", return_value=102.017):
            second_frame = app._token_flow_trace_pulses(160, 18)

        first_gap = round(first_frame[0][0]) - round(first_frame[1][0])
        second_gap = round(second_frame[0][0]) - round(second_frame[1][0])
        self.assertGreater(first_gap, 0)
        self.assertEqual(first_gap, second_gap)

    def test_account_ecg_peak_height_scales_with_token_event(self) -> None:
        low_points = monitor.FloatingMonitorApp._token_flow_ecg_points(
            160,
            18,
            [(80.0, 3, 0.2)],
        )
        high_points = monitor.FloatingMonitorApp._token_flow_ecg_points(
            160,
            18,
            [(80.0, 8, 0.9)],
        )
        center_y = 9.0

        self.assertLess(min(y for _x, y in high_points), min(y for _x, y in low_points))
        self.assertLess(min(y for _x, y in high_points), center_y)
        self.assertGreater(max(y for _x, y in high_points), center_y)

    def test_account_ecg_is_one_continuous_baseline_when_idle(self) -> None:
        points = monitor.FloatingMonitorApp._token_flow_ecg_points(40, 18, [])

        self.assertEqual(points, [(0.0, 9.0), (40.0, 9.0)])
        self.assertEqual({y for _x, y in points}, {9.0})

    def test_account_ecg_event_keeps_fixed_peak_height_while_moving(self) -> None:
        first = monitor.FloatingMonitorApp._token_flow_ecg_points(
            160,
            18,
            [(80.15, 8, 0.9)],
        )
        second = monitor.FloatingMonitorApp._token_flow_ecg_points(
            160,
            18,
            [(80.65, 8, 0.9)],
        )

        self.assertAlmostEqual(min(y for _x, y in first), 1.0)
        self.assertAlmostEqual(min(y for _x, y in second), 1.0)

    def test_account_ecg_points_share_one_pixel_phase(self) -> None:
        points = monitor.FloatingMonitorApp._token_flow_ecg_points(
            160,
            18,
            [(80.35, 8, 0.9)],
        )
        peak_x = min(points, key=lambda point: point[1])[0]
        waveform_points = points[1:-1]

        self.assertGreaterEqual(
            max(point_x for point_x, _point_y in waveform_points)
            - min(point_x for point_x, _point_y in waveform_points),
            20.0,
        )
        for point_x, _point_y in waveform_points:
            relative_x = point_x - peak_x
            self.assertAlmostEqual(relative_x, round(relative_x))

    def test_account_trace_reuses_canvas_lines_at_60fps(self) -> None:
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        app._main_tab = "accounts"
        app._token_flow_trace_rect = (10, 20, 170, 40)
        app.canvas = MagicMock()
        app.canvas.find_withtag.return_value = (11,)

        with (
            patch.object(app, "_token_flow_snapshot", return_value=(0.5, 100_000)),
            patch.object(
                app,
                "_token_flow_trace_pulses",
                return_value=[(20.0, 4, 0.2), (40.0, 6, 0.6)],
            ),
            patch.object(
                app,
                "_token_flow_ecg_points",
                return_value=[(0.0, 10.0), (160.0, 10.0)],
            ),
        ):
            redrawn = app._redraw_token_flow_trace()

        self.assertTrue(redrawn)
        self.assertLessEqual(monitor.TOKEN_FLOW_ANIMATION_INTERVAL_MS, 17)
        app.canvas.delete.assert_not_called()
        app.canvas.create_line.assert_not_called()
        app.canvas.coords.assert_called_once_with(11, 10.0, 30.0, 170.0, 30.0)
        app.canvas.itemconfigure.assert_called_once_with(
            11,
            fill=monitor.Theme.live,
            state="normal",
        )

    def test_stats_meter_reuses_canvas_segments_at_60fps(self) -> None:
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        app._main_tab = "stats"
        app._token_flow_meter_rect = (10, 20, 30, 80)
        app._token_flow_meter_fill_bounds = (12.0, 22.0, 26.0, 78.0)
        app.canvas = MagicMock()
        head_items = tuple(range(21, 21 + monitor.TOKEN_FLOW_METER_HEAD_BANDS))
        app.canvas.find_withtag.side_effect = [(11,), head_items]

        with (
            patch.object(app, "_token_flow_snapshot", return_value=(0.5, 100_000)),
            patch.object(app, "_smooth_token_flow_meter_level", return_value=0.5),
        ):
            redrawn = app._redraw_token_flow_meter()

        self.assertTrue(redrawn)
        self.assertLessEqual(monitor.TOKEN_FLOW_ANIMATION_INTERVAL_MS, 17)
        app.canvas.delete.assert_not_called()
        self.assertEqual(
            app.canvas.coords.call_count,
            1 + monitor.TOKEN_FLOW_METER_HEAD_BANDS,
        )
        app.canvas.itemconfigure.assert_not_called()

    def test_stats_delta_badge_reuses_one_canvas_text_item(self) -> None:
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        app._main_tab = "stats"
        app.canvas = MagicMock()
        app.canvas.find_withtag.return_value = (17,)

        with patch.object(
            app,
            "_token_delta_badge_visual",
            return_value=("+12,345", "#3A8A72", True),
        ):
            redrawn = app._redraw_token_delta_badge()

        self.assertTrue(redrawn)
        app.canvas.delete.assert_not_called()
        app.canvas.create_text.assert_not_called()
        app.canvas.itemconfigure.assert_called_once_with(
            17,
            text="+12,345",
            fill="#3A8A72",
            state="normal",
        )

    def test_stats_cost_delta_badge_reuses_one_canvas_text_item(self) -> None:
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        app._main_tab = "stats"
        app.canvas = MagicMock()
        app.canvas.find_withtag.return_value = (23,)

        with patch.object(
            app,
            "_cost_delta_badge_visual",
            return_value=("+$0.25", "#B78E48", True),
        ):
            redrawn = app._redraw_cost_delta_badge()

        self.assertTrue(redrawn)
        app.canvas.delete.assert_not_called()
        app.canvas.create_text.assert_not_called()
        app.canvas.itemconfigure.assert_called_once_with(
            23,
            text="+$0.25",
            fill="#B78E48",
            state="normal",
        )


class CodexSessionModelTests(unittest.TestCase):
    def write_session(self, root: Path, name: str, rows: list[dict]) -> None:
        session_dir = root / "2026" / "07" / "12"
        session_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / name).write_text(
            "\n".join(json.dumps(row) for row in rows),
            encoding="utf-8",
        )

    def token_count(
        self,
        timestamp: str,
        input_tokens: int,
        output_tokens: int,
        *,
        cached_tokens: int = 0,
        total_input_tokens: int | None = None,
        total_cached_tokens: int | None = None,
        total_output_tokens: int | None = None,
    ) -> dict:
        return {
            "timestamp": timestamp,
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "last_token_usage": {
                        "input_tokens": input_tokens,
                        "cached_input_tokens": cached_tokens,
                        "output_tokens": output_tokens,
                    },
                    "total_token_usage": {
                        "input_tokens": total_input_tokens if total_input_tokens is not None else input_tokens,
                        "cached_input_tokens": (
                            total_cached_tokens if total_cached_tokens is not None else cached_tokens
                        ),
                        "output_tokens": total_output_tokens if total_output_tokens is not None else output_tokens,
                    },
                },
            },
        }

    def usage_event(self, when: datetime, session_id: str = "recovery-session") -> client_usage_export.UsageEvent:
        return client_usage_export.UsageEvent(
            when=when,
            model="gpt-test",
            input_tokens=1,
            cached_tokens=0,
            output_tokens=0,
            session_id=session_id,
        )

    def test_token_event_keeps_task_start_as_attribution_time(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_session(
                root,
                "rollout-manual-switch.jsonl",
                [
                    {
                        "timestamp": "2026-07-12T10:00:00",
                        "type": "session_meta",
                        "payload": {"id": "manual-switch-session"},
                    },
                    {
                        "timestamp": "2026-07-12T10:01:00",
                        "type": "event_msg",
                        "payload": {"type": "task_started", "turn_id": "turn-a"},
                    },
                    self.token_count("2026-07-12T10:05:00", 100, 10),
                ],
            )

            events = client_usage_export.scan_codex_events(
                root,
                datetime(2026, 7, 12, 9, 0),
                datetime(2026, 7, 12, 11, 0),
            )

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].when, datetime(2026, 7, 12, 10, 5))
        self.assertIsNone(events[0].request_at)
        self.assertEqual(events[0].account_at, datetime(2026, 7, 12, 10, 1))
        self.assertEqual(
            client_usage_export.usage_event_attribution_time(events[0]),
            datetime(2026, 7, 12, 10, 5),
        )

        markers = [
            client_usage_export.AccountMarker(
                when=datetime(2026, 7, 12, 9, 0),
                label="Codex local - account-a@example.com",
                kind="switch",
            ),
            client_usage_export.AccountMarker(
                when=datetime(2026, 7, 12, 10, 3),
                label="Codex local - account-b@example.com",
                kind="switch",
            ),
        ]
        attributed = client_usage_export.attribute_codex_events_by_account(
            events,
            markers,
        )
        self.assertIn("Codex local - account-a@example.com", attributed)
        self.assertNotIn("Codex local - account-b@example.com", attributed)

    def test_token_count_inherits_model_from_turn_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_session(
                root,
                "rollout-session-1.jsonl",
                [
                    {
                        "timestamp": "2026-07-12T09:59:00",
                        "type": "session_meta",
                        "payload": {"id": "session-1"},
                    },
                    {
                        "timestamp": "2026-07-12T10:00:00",
                        "type": "turn_context",
                        "payload": {"model": "gpt-5.6-sol"},
                    },
                    self.token_count("2026-07-12T10:01:00", 100, 10),
                ],
            )

            events = client_usage_export.scan_codex_events(
                root,
                datetime(2026, 7, 12, 9, 0),
                datetime(2026, 7, 12, 11, 0),
            )

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].model, "gpt-5.6-sol")

    def test_model_switch_only_applies_to_later_token_counts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = self.token_count("2026-07-12T10:01:00", 100, 10)
            second = self.token_count("2026-07-12T10:03:00", 200, 20)
            second["payload"]["info"]["total_token_usage"].update(
                {"input_tokens": 300, "output_tokens": 30}
            )
            self.write_session(
                root,
                "rollout-session-2.jsonl",
                [
                    {
                        "timestamp": "2026-07-12T09:59:00",
                        "type": "session_meta",
                        "payload": {"id": "session-2"},
                    },
                    {
                        "timestamp": "2026-07-12T10:00:00",
                        "type": "turn_context",
                        "payload": {"model": "gpt-5.6-sol"},
                    },
                    first,
                    {
                        "timestamp": "2026-07-12T10:02:00",
                        "type": "turn_context",
                        "payload": {"model": "gpt-5.4"},
                    },
                    second,
                ],
            )

            events = client_usage_export.scan_codex_events(
                root,
                datetime(2026, 7, 12, 9, 0),
                datetime(2026, 7, 12, 11, 0),
            )

        self.assertEqual([event.model for event in events], ["gpt-5.6-sol", "gpt-5.4"])

    def test_long_fork_replay_is_skipped_while_parent_and_child_requests_remain(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_session(
                root,
                "rollout-parent.jsonl",
                [
                    {
                        "timestamp": "2026-07-11T09:59:00",
                        "type": "session_meta",
                        "payload": {"id": "parent"},
                    },
                    self.token_count(
                        "2026-07-11T10:00:00",
                        100,
                        10,
                        total_input_tokens=100,
                        total_output_tokens=10,
                    ),
                    self.token_count(
                        "2026-07-11T10:01:00",
                        200,
                        20,
                        total_input_tokens=300,
                        total_output_tokens=30,
                    ),
                    self.token_count(
                        "2026-07-12T10:00:30",
                        50,
                        5,
                        total_input_tokens=350,
                        total_output_tokens=35,
                    ),
                ],
            )
            self.write_session(
                root,
                "rollout-child.jsonl",
                [
                    {
                        "timestamp": "2026-07-12T10:00:00",
                        "type": "session_meta",
                        "payload": {"id": "child", "forked_from_id": "parent"},
                    },
                    self.token_count(
                        "2026-07-12T10:00:01",
                        100,
                        10,
                        total_input_tokens=100,
                        total_output_tokens=10,
                    ),
                    self.token_count(
                        "2026-07-12T10:00:03.500",
                        200,
                        20,
                        total_input_tokens=300,
                        total_output_tokens=30,
                    ),
                    self.token_count(
                        "2026-07-12T10:01:00",
                        40,
                        4,
                        total_input_tokens=350,
                        total_output_tokens=35,
                    ),
                ],
            )
            self.write_session(
                root,
                "rollout-sibling.jsonl",
                [
                    {
                        "timestamp": "2026-07-12T10:00:00.500",
                        "type": "session_meta",
                        "payload": {"id": "sibling", "forked_from_id": "parent"},
                    },
                    self.token_count(
                        "2026-07-12T10:00:01.500",
                        100,
                        10,
                        total_input_tokens=100,
                        total_output_tokens=10,
                    ),
                    self.token_count(
                        "2026-07-12T10:00:04",
                        200,
                        20,
                        total_input_tokens=300,
                        total_output_tokens=30,
                    ),
                    self.token_count(
                        "2026-07-12T10:02:00",
                        45,
                        6,
                        total_input_tokens=360,
                        total_output_tokens=36,
                    ),
                ],
            )

            events = client_usage_export.scan_codex_events(
                root,
                datetime(2026, 7, 12, 9, 0),
                datetime(2026, 7, 12, 11, 0),
            )

        self.assertEqual(
            [(event.session_id, event.input_tokens, event.output_tokens) for event in events],
            [("parent", 50, 5), ("child", 40, 4), ("sibling", 45, 6)],
        )

    def test_non_fork_session_keeps_all_token_counts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_session(
                root,
                "rollout-regular.jsonl",
                [
                    {
                        "timestamp": "2026-07-12T09:59:00",
                        "type": "session_meta",
                        "payload": {"id": "regular"},
                    },
                    self.token_count(
                        "2026-07-12T10:00:00",
                        70,
                        7,
                        total_input_tokens=70,
                        total_output_tokens=7,
                    ),
                    self.token_count(
                        "2026-07-12T10:01:00",
                        80,
                        8,
                        total_input_tokens=150,
                        total_output_tokens=15,
                    ),
                ],
            )

            events = client_usage_export.scan_codex_events(
                root,
                datetime(2026, 7, 12, 9, 0),
                datetime(2026, 7, 12, 11, 0),
            )

        self.assertEqual(
            [(event.input_tokens, event.output_tokens) for event in events],
            [(70, 7), (80, 8)],
        )

    def test_non_fork_repeated_cumulative_snapshots_keep_legacy_dedupe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_session(
                root,
                "rollout-regular-a.jsonl",
                [
                    {
                        "timestamp": "2026-07-12T09:59:00",
                        "type": "session_meta",
                        "payload": {"id": "regular-a"},
                    },
                    self.token_count(
                        "2026-07-12T10:00:00",
                        100,
                        10,
                        total_input_tokens=100,
                        total_output_tokens=10,
                    ),
                    self.token_count(
                        "2026-07-12T10:01:00",
                        40,
                        4,
                        total_input_tokens=100,
                        total_output_tokens=10,
                    ),
                    self.token_count(
                        "2026-07-12T10:02:00",
                        25,
                        2,
                        total_input_tokens=100,
                        total_output_tokens=10,
                    ),
                ],
            )
            self.write_session(
                root,
                "rollout-regular-b.jsonl",
                [
                    {
                        "timestamp": "2026-07-12T10:03:00",
                        "type": "session_meta",
                        "payload": {"id": "regular-b"},
                    },
                    self.token_count(
                        "2026-07-12T10:04:00",
                        60,
                        6,
                        total_input_tokens=100,
                        total_output_tokens=10,
                    ),
                ],
            )

            events = client_usage_export.scan_codex_events(
                root,
                datetime(2026, 7, 12, 9, 0),
                datetime(2026, 7, 12, 11, 0),
            )

        self.assertEqual(
            [(event.session_id, event.input_tokens, event.output_tokens) for event in events],
            [("regular-a", 100, 10)],
        )

    def test_only_terminal_turn_errors_are_collected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_session(
                root,
                "rollout-failures.jsonl",
                [
                    {
                        "timestamp": "2026-07-12T02:50:00",
                        "type": "session_meta",
                        "payload": {"id": "failure-session"},
                    },
                    {
                        "timestamp": "2026-07-12T03:00:00",
                        "type": "event_msg",
                        "payload": {"type": "task_started", "turn_id": "retry-turn"},
                    },
                    {
                        "timestamp": "2026-07-12T03:05:00",
                        "type": "event_msg",
                        "payload": {"type": "stream_error", "message": "retrying"},
                    },
                    {
                        "timestamp": "2026-07-12T03:06:00",
                        "type": "event_msg",
                        "payload": {"type": "task_complete", "turn_id": "retry-turn"},
                    },
                    {
                        "timestamp": "2026-07-12T03:10:00",
                        "type": "event_msg",
                        "payload": {"type": "task_started", "turn_id": "interrupted-turn"},
                    },
                    {
                        "timestamp": "2026-07-12T03:11:00",
                        "type": "event_msg",
                        "payload": {
                            "type": "turn_aborted",
                            "turn_id": "interrupted-turn",
                            "reason": "interrupted",
                        },
                    },
                    {
                        "timestamp": "2026-07-12T03:20:00",
                        "type": "event_msg",
                        "payload": {"type": "task_started", "turn_id": "legacy-error"},
                    },
                    {
                        "timestamp": "2026-07-12T03:21:00",
                        "type": "event_msg",
                        "payload": {
                            "type": "error",
                            "message": "stream disconnected",
                            "codex_error_info": {
                                "response_stream_disconnected": {"http_status_code": 502}
                            },
                        },
                    },
                    {
                        "timestamp": "2026-07-12T03:22:00",
                        "type": "event_msg",
                        "payload": {"type": "task_complete", "turn_id": "legacy-error"},
                    },
                    {
                        "timestamp": "2026-07-12T03:30:00",
                        "type": "event_msg",
                        "payload": {"type": "task_started", "turn_id": "terminal-error"},
                    },
                    {
                        "timestamp": "2026-07-12T03:31:00",
                        "type": "event_msg",
                        "payload": {
                            "type": "task_complete",
                            "turn_id": "terminal-error",
                            "error": {
                                "message": "server failed",
                                "codex_error_info": "internal_server_error",
                            },
                        },
                    },
                ],
            )
            failures: list[client_usage_export.CodexFailureEvent] = []

            client_usage_export.scan_codex_events(
                root,
                datetime(2026, 7, 12, 2, 0),
                datetime(2026, 7, 12, 4, 0),
                failure_events=failures,
            )

        self.assertEqual([failure.turn_id for failure in failures], ["legacy-error", "terminal-error"])
        self.assertEqual([failure.when.minute for failure in failures], [22, 31])

    def test_error_marks_the_actual_hour_even_when_it_has_usage(self) -> None:
        hourly = [
            {"hour": hour, "requests": 0, "tokens": 0, "cost": 0.0}
            for hour in range(24)
        ]
        hourly[3].update({"requests": 137, "tokens": 23_100_000})
        failures = [
            client_usage_export.CodexFailureEvent(
                when=datetime(2026, 7, 12, 3, 55),
                session_id="failure-session",
                turn_id="failed-turn",
            )
        ]

        client_usage_export.mark_codex_failure_hours(
            hourly,
            failures,
            date(2026, 7, 12),
            datetime(2026, 7, 12, 4, 20),
            activity_events=[],
        )

        self.assertTrue(hourly[3]["failure"])
        self.assertEqual(hourly[3]["failure_count"], 1)
        self.assertFalse(any(row.get("failure") for row in hourly[4:]))

    def test_error_marker_clears_when_codex_activity_resumes_in_same_hour(self) -> None:
        hourly = [
            {"hour": hour, "requests": 0, "tokens": 0, "cost": 0.0}
            for hour in range(24)
        ]
        hourly[3]["tokens"] = 100
        failures = [
            client_usage_export.CodexFailureEvent(
                when=datetime(2026, 7, 12, 3, 55),
                session_id="failure-session",
                turn_id="failed-turn",
            )
        ]

        client_usage_export.mark_codex_failure_hours(
            hourly,
            failures,
            date(2026, 7, 12),
            datetime(2026, 7, 12, 3, 59),
            activity_events=[],
        )
        self.assertTrue(hourly[3]["failure"])

        client_usage_export.mark_codex_failure_hours(
            hourly,
            failures,
            date(2026, 7, 12),
            datetime(2026, 7, 12, 3, 59),
            activity_events=[self.usage_event(datetime(2026, 7, 12, 3, 58))],
        )
        self.assertFalse(hourly[3].get("failure"))

    def test_error_marker_survives_activity_in_a_later_hour(self) -> None:
        hourly = [
            {"hour": hour, "requests": 0, "tokens": 0, "cost": 0.0}
            for hour in range(24)
        ]
        failures = [
            client_usage_export.CodexFailureEvent(
                when=datetime(2026, 7, 12, 3, 55),
                session_id="failure-session",
                turn_id="failed-turn",
            )
        ]

        client_usage_export.mark_codex_failure_hours(
            hourly,
            failures,
            date(2026, 7, 12),
            datetime(2026, 7, 12, 4, 30),
            activity_events=[self.usage_event(datetime(2026, 7, 12, 4, 5))],
        )

        self.assertTrue(hourly[3]["failure"])
        self.assertFalse(hourly[4].get("failure"))

    def test_idle_hour_network_failure_does_not_mark_token_activity(self) -> None:
        hourly = [
            {"hour": hour, "requests": 0, "tokens": 0, "cost": 0.0}
            for hour in range(24)
        ]
        failures = [
            client_usage_export.CodexFailureEvent(
                when=datetime(2026, 7, 12, 4, 0, 14),
                session_id="codex-desktop",
                kind="desktop_network",
            )
        ]

        client_usage_export.mark_codex_failure_hours(
            hourly,
            failures,
            date(2026, 7, 12),
            datetime(2026, 7, 12, 5, 0),
            activity_events=[],
        )

        self.assertFalse(hourly[4].get("failure"))
        self.assertNotIn("failure_count", hourly[4])
        self.assertNotIn("failure_at", hourly[4])
        self.assertNotIn("failure_kind", hourly[4])

    def test_activity_before_the_latest_failure_does_not_clear_the_hour(self) -> None:
        hourly = [
            {"hour": hour, "requests": 0, "tokens": 0, "cost": 0.0}
            for hour in range(24)
        ]
        failures = [
            client_usage_export.CodexFailureEvent(
                when=datetime(2026, 7, 12, 3, 20),
                session_id="failure-session",
                turn_id="first-failed-turn",
            ),
            client_usage_export.CodexFailureEvent(
                when=datetime(2026, 7, 12, 3, 55),
                session_id="failure-session",
                turn_id="latest-failed-turn",
            ),
        ]

        client_usage_export.mark_codex_failure_hours(
            hourly,
            failures,
            date(2026, 7, 12),
            datetime(2026, 7, 12, 3, 59),
            activity_events=[self.usage_event(datetime(2026, 7, 12, 3, 30))],
        )

        self.assertTrue(hourly[3]["failure"])
        self.assertEqual(hourly[3]["failure_count"], 2)
        self.assertEqual(hourly[3]["failure_at"], "2026-07-12T03:55:00+08:00")

    def test_repeated_desktop_network_failures_mark_the_failure_hour(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log_root = Path(directory)
            log_dir = log_root / "2026" / "07" / "11"
            log_dir.mkdir(parents=True)
            (log_dir / "codex-desktop-fixture.log").write_text(
                "\n".join(
                    [
                        "2026-07-11T19:59:53.000Z warning [electron-message-handler] "
                        "sa_server_request_failed errorMessage=net::ERR_NETWORK_CHANGED",
                        "2026-07-11T20:00:01.000Z warning [electron-message-handler] "
                        "sa_server_request_failed errorMessage=net::ERR_NETWORK_CHANGED",
                        "2026-07-11T20:00:16.000Z warning [electron-message-handler] "
                        "sa_server_request_failed errorMessage=net::ERR_CONNECTION_CLOSED",
                    ]
                ),
                encoding="utf-8",
            )

            failures = client_usage_export.scan_codex_desktop_failure_events(
                log_root,
                datetime(2026, 7, 12),
                datetime(2026, 7, 13),
            )

        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0].when, datetime(2026, 7, 12, 3, 59, 53))
        hourly = [
            {"hour": hour, "requests": 0, "tokens": 0, "cost": 0.0}
            for hour in range(24)
        ]
        hourly[3]["tokens"] = 23_100_000
        client_usage_export.mark_codex_failure_hours(
            hourly,
            failures,
            date(2026, 7, 12),
            datetime(2026, 7, 12, 5, 0),
            activity_events=[],
        )
        self.assertTrue(hourly[3]["failure"])
        self.assertEqual(hourly[3]["failure_kind"], "desktop_network")

    def test_desktop_log_roots_discover_msix_app_data_and_install_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            local_app_data = root / "LocalAppData"
            roaming_app_data = root / "RoamingAppData"
            program_files = root / "Program Files"
            package_logs = (
                local_app_data
                / "Packages"
                / "OpenAI.Codex_2p2nqsd0c76g0"
                / "LocalCache"
                / "Local"
                / "Codex"
                / "Logs"
            )
            install_logs = (
                program_files
                / "WindowsApps"
                / "OpenAI.Codex_26.707.3748.0_x64__2p2nqsd0c76g0"
                / "app"
                / "Logs"
            )
            package_logs.mkdir(parents=True)
            log_dir = install_logs / "2026" / "07" / "11"
            log_dir.mkdir(parents=True)
            (log_dir / "codex-desktop-fixture.log").write_text(
                "\n".join(
                    [
                        "2026-07-11T19:59:53.000Z warning [electron-message-handler] "
                        "sa_server_request_failed errorMessage=net::ERR_NETWORK_CHANGED",
                        "2026-07-11T20:00:01.000Z warning [electron-message-handler] "
                        "sa_server_request_failed errorMessage=net::ERR_NETWORK_CHANGED",
                        "2026-07-11T20:00:16.000Z warning [electron-message-handler] "
                        "sa_server_request_failed errorMessage=net::ERR_CONNECTION_CLOSED",
                    ]
                ),
                encoding="utf-8",
            )

            with patch.dict(
                "os.environ",
                {
                    "LOCALAPPDATA": str(local_app_data),
                    "APPDATA": str(roaming_app_data),
                    "PROGRAMFILES": str(program_files),
                    "CLIENT_USAGE_CODEX_DESKTOP_LOG_ROOT": "",
                },
                clear=False,
            ):
                log_roots = client_usage_export.default_codex_desktop_log_roots()
                failures = client_usage_export.scan_codex_desktop_failure_events(
                    log_roots,
                    datetime(2026, 7, 12),
                    datetime(2026, 7, 13),
                )

        self.assertIn(package_logs.resolve(), log_roots)
        self.assertIn(install_logs.resolve(), log_roots)
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0].kind, "desktop_network")

    def test_transient_or_unrelated_desktop_errors_are_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log_root = Path(directory)
            log_dir = log_root / "2026" / "07" / "11"
            log_dir.mkdir(parents=True)
            (log_dir / "codex-desktop-fixture.log").write_text(
                "\n".join(
                    [
                        "2026-07-11T19:59:53.000Z warning [electron-message-handler] "
                        "sa_server_request_failed errorMessage=net::ERR_NETWORK_CHANGED",
                        "2026-07-11T20:00:01.000Z error [desktop-notifications][global-error] "
                        "ResizeObserver loop completed with undelivered notifications.",
                        "2026-07-11T20:00:16.000Z error [windows-store-updater] "
                        "Failed to check for updates errorMessage=net::ERR_CONNECTION_CLOSED",
                    ]
                ),
                encoding="utf-8",
            )

            failures = client_usage_export.scan_codex_desktop_failure_events(
                log_root,
                datetime(2026, 7, 12),
                datetime(2026, 7, 13),
            )

        self.assertEqual(failures, [])


class OfflineHistoryCatchupTests(unittest.TestCase):
    def history_row(self, day: date, tokens: int, provider: str = "account@example.com") -> dict:
        return {
            "date": day.isoformat(),
            "source": "local",
            "requests": max(0, tokens // 100),
            "tokens": tokens,
            "input_tokens": tokens,
            "cached_input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "output_tokens": 0,
            "cost": round(tokens / 1_000_000, 6),
            "models": {"gpt-test": tokens} if tokens else {},
            "providers": [
                {
                    "name": f"Codex local - {provider}",
                    "requests": max(0, tokens // 100),
                    "tokens": tokens,
                    "input_tokens": tokens,
                    "cached_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "output_tokens": 0,
                    "cost": round(tokens / 1_000_000, 6),
                    "models": {"gpt-test": tokens} if tokens else {},
                }
            ] if tokens else [],
            "detail_tokens": tokens,
            "updated_at": f"{day.isoformat()}T12:00:00+08:00",
            "source_date": day.isoformat(),
        }

    def test_reconcile_dates_cover_partial_last_day_and_closed_days(self) -> None:
        last_seen = date(2026, 7, 10)
        now = datetime(2026, 7, 13, 9, 0, 0)
        history = {
            "schema": 2,
            "days": {
                last_seen.isoformat(): self.history_row(last_seen, 100),
            },
        }

        targets = client_usage_export.offline_history_dates_to_reconcile(
            history,
            now,
            max_days=31,
        )

        self.assertEqual(
            targets,
            [date(2026, 7, 10), date(2026, 7, 11), date(2026, 7, 12)],
        )

    def test_reconcile_does_not_rescan_last_day_after_today_success(self) -> None:
        last_day = date(2026, 7, 12)
        now = datetime(2026, 7, 13, 9, 0, 0)
        history = {
            "schema": 2,
            "days": {
                last_day.isoformat(): {
                    **self.history_row(last_day, 100),
                    "cockpit_usage_schema": client_usage_export.COCKPIT_USAGE_DEDUPE_SCHEMA,
                    "usage_accounting_schema": client_usage_export.USAGE_ACCOUNTING_SCHEMA,
                },
            },
            "offline_sync": {
                "state": "complete",
                "last_successful_at": "2026-07-13T08:30:00+08:00",
                "through": last_day.isoformat(),
            },
        }

        targets = client_usage_export.offline_history_dates_to_reconcile(
            history,
            now,
            max_days=31,
        )

        self.assertEqual(targets, [])

    def test_yesterday_is_reconciled_even_after_today_history_is_written(self) -> None:
        last_day = date(2026, 7, 12)
        today = date(2026, 7, 13)
        now = datetime(2026, 7, 13, 9, 0, 0)
        history = {
            "schema": 2,
            "days": {
                last_day.isoformat(): {
                    **self.history_row(last_day, 100),
                    "cockpit_usage_schema": client_usage_export.COCKPIT_USAGE_DEDUPE_SCHEMA,
                    "usage_accounting_schema": client_usage_export.USAGE_ACCOUNTING_SCHEMA,
                    "updated_at": "2026-07-12T23:59:39+08:00",
                },
                today.isoformat(): {
                    **self.history_row(today, 50),
                    "updated_at": "2026-07-13T08:30:00+08:00",
                },
            },
            "offline_sync": {
                "state": "complete",
                "last_successful_at": "2026-07-12T08:30:00+08:00",
                "through": "2026-07-11",
            },
        }

        targets = client_usage_export.offline_history_dates_to_reconcile(
            history,
            now,
            max_days=31,
        )

        self.assertEqual(targets, [last_day])

    def test_yesterday_row_reconciled_after_midnight_is_not_scanned_again(self) -> None:
        last_day = date(2026, 7, 12)
        now = datetime(2026, 7, 13, 9, 0, 0)
        history = {
            "schema": 2,
            "days": {
                last_day.isoformat(): {
                    **self.history_row(last_day, 100),
                    "cockpit_usage_schema": client_usage_export.COCKPIT_USAGE_DEDUPE_SCHEMA,
                    "usage_accounting_schema": client_usage_export.USAGE_ACCOUNTING_SCHEMA,
                    "offline_reconciled_at": "2026-07-13T00:10:00+08:00",
                },
            },
            "offline_sync": {
                "state": "complete",
                "last_successful_at": "2026-07-13T00:10:00+08:00",
                "through": "2026-07-11",
            },
        }

        targets = client_usage_export.offline_history_dates_to_reconcile(
            history,
            now,
            max_days=31,
        )

        self.assertEqual(targets, [])

    def test_reconcile_never_reduces_existing_high_water(self) -> None:
        day = date(2026, 7, 10)
        existing = self.history_row(day, 1_000)
        rebuilt = self.history_row(day, 400)

        merged, changed = client_usage_export.merge_rebuilt_history_day(
            existing,
            rebuilt,
            "2026-07-13T09:00:00+08:00",
        )

        self.assertFalse(changed)
        self.assertEqual(merged["tokens"], 1_000)
        self.assertEqual(merged["providers"][0]["tokens"], 1_000)

    def test_historical_rows_initialize_affinity_evidence_before_resolving(self) -> None:
        target_day = date(2026, 7, 10)
        now = datetime(2026, 7, 12, 9, 0, 0)
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(client_usage_export, "scan_all_codex_events", return_value=[]),
            patch.object(client_usage_export, "codex_speed_history", return_value=[]),
            patch.object(
                client_usage_export,
                "scan_cockpit_codex_account_markers",
                return_value=[],
            ),
            patch.object(
                client_usage_export,
                "scan_cockpit_codex_affinity_events",
                return_value=[],
            ) as affinity_scan,
            patch.object(
                client_usage_export,
                "scan_cockpit_codex_switch_markers",
                return_value=[],
            ),
            patch.object(client_usage_export, "load_account_timeline", return_value=[]),
            patch.object(client_usage_export, "current_codex_account_label", return_value=""),
            patch.object(client_usage_export, "cockpit_codex_speed_by_label", return_value={}),
            patch.object(client_usage_export, "scan_claude_daily_buckets", return_value={}),
        ):
            root = Path(directory)
            rows = client_usage_export.build_historical_usage_rows(
                root,
                root / "sessions",
                [target_day],
                {},
                now,
            )

        affinity_scan.assert_called_once()
        self.assertEqual(rows[target_day.isoformat()]["tokens"], 0)

    def test_reconcile_can_enrich_details_without_reducing_total(self) -> None:
        day = date(2026, 7, 10)
        existing = self.history_row(day, 1_000)
        existing["providers"] = []
        existing["models"] = {}
        existing["detail_tokens"] = 0
        rebuilt = self.history_row(day, 400)

        merged, changed = client_usage_export.merge_rebuilt_history_day(
            existing,
            rebuilt,
            "2026-07-13T09:00:00+08:00",
        )

        self.assertTrue(changed)
        self.assertEqual(merged["tokens"], 1_000)
        self.assertEqual(merged["providers"][0]["tokens"], 400)

    def test_schema_upgrade_replaces_provider_split_at_same_token_total(self) -> None:
        day = date(2026, 8, 21)
        existing = self.history_row(day, 1_000, "william@example.com")
        existing["cockpit_usage_schema"] = 1
        rebuilt = {
            "date": day.isoformat(),
            "source": "local-backfill",
            "cockpit_usage_schema": client_usage_export.COCKPIT_USAGE_DEDUPE_SCHEMA,
            "requests": 10,
            "tokens": 1_000,
            "input_tokens": 900,
            "cached_input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "output_tokens": 100,
            "cost": 0.001,
            "models": {
                "gpt-test": 400,
                "xai/grok-4.6": 600,
            },
            "providers": [
                {
                    "name": "Codex local - william@example.com",
                    "requests": 4,
                    "tokens": 400,
                    "input_tokens": 360,
                    "cached_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "output_tokens": 40,
                    "cost": 0.0004,
                    "models": {"gpt-test": 400},
                },
                {
                    "name": client_usage_export.GROK_SUBAGENT_LABEL,
                    "requests": 6,
                    "tokens": 600,
                    "input_tokens": 540,
                    "cached_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "output_tokens": 60,
                    "cost": 0.0006,
                    "models": {"xai/grok-4.6": 600},
                },
            ],
            "detail_tokens": 1_000,
            "updated_at": "2026-08-22T09:00:00+08:00",
            "source_date": day.isoformat(),
        }

        merged, changed = client_usage_export.merge_rebuilt_history_day(
            existing,
            rebuilt,
            "2026-08-22T09:00:00+08:00",
        )

        self.assertTrue(changed)
        self.assertEqual(merged["tokens"], 1_000)
        self.assertEqual(merged["cockpit_usage_schema"], 2)
        self.assertEqual(
            [provider["name"] for provider in merged["providers"]],
            ["Codex local - william@example.com", client_usage_export.GROK_SUBAGENT_LABEL],
        )
        self.assertEqual(merged["models"]["xai/grok-4.6"], 600)

    def test_stale_schema_days_are_reconcile_targets(self) -> None:
        last_day = date(2026, 8, 21)
        now = datetime(2026, 8, 22, 9, 0, 0)
        history = {
            "schema": 2,
            "days": {
                last_day.isoformat(): {
                    **self.history_row(last_day, 100),
                    "cockpit_usage_schema": 1,
                    "updated_at": "2026-08-22T08:30:00+08:00",
                }
            },
            "offline_sync": {
                "state": "complete",
                "last_successful_at": "2026-08-22T08:30:00+08:00",
            },
        }

        targets = client_usage_export.offline_history_dates_to_reconcile(
            history,
            now,
            max_days=31,
        )

        self.assertEqual(targets, [last_day])

    def test_missing_accounting_schema_is_reconciled_with_current_cockpit_schema(self) -> None:
        last_day = date(2026, 8, 21)
        now = datetime(2026, 8, 22, 9, 0, 0)
        history = {
            "schema": 2,
            "days": {
                last_day.isoformat(): {
                    **self.history_row(last_day, 100),
                    "cockpit_usage_schema": client_usage_export.COCKPIT_USAGE_DEDUPE_SCHEMA,
                    "updated_at": "2026-08-22T08:30:00+08:00",
                }
            },
            "offline_sync": {
                "state": "complete",
                "last_successful_at": "2026-08-22T08:30:00+08:00",
            },
        }

        targets = client_usage_export.offline_history_dates_to_reconcile(
            history,
            now,
            max_days=31,
            accounting_evidence_dates={last_day},
        )

        self.assertEqual(targets, [last_day])

    def test_backfill_replaces_partial_day_and_is_idempotent(self) -> None:
        first_day = date(2026, 7, 10)
        second_day = date(2026, 7, 11)
        now = datetime(2026, 7, 12, 9, 0, 0)
        history = {
            "schema": 2,
            "days": {
                first_day.isoformat(): self.history_row(first_day, 100),
            },
        }
        rebuilt = {
            first_day.isoformat(): self.history_row(first_day, 250),
            second_day.isoformat(): self.history_row(second_day, 500),
        }
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(
                client_usage_export,
                "USAGE_HISTORY_PATH",
                Path(directory) / "usage_history.json",
            ),
            patch.object(
                client_usage_export,
                "build_historical_usage_rows",
                return_value=rebuilt,
            ),
        ):
            first = client_usage_export.backfill_offline_usage_history(
                Path(directory),
                Path(directory) / "sessions",
                now,
                {},
                history=history,
                target_days=[first_day, second_day],
            )
            saved_once = json.loads(
                client_usage_export.USAGE_HISTORY_PATH.read_text(encoding="utf-8")
            )
            second = client_usage_export.backfill_offline_usage_history(
                Path(directory),
                Path(directory) / "sessions",
                now,
                {},
                history=saved_once,
                target_days=[first_day, second_day],
            )
            saved_twice = json.loads(
                client_usage_export.USAGE_HISTORY_PATH.read_text(encoding="utf-8")
            )

        self.assertEqual(first["updated_days"], 2)
        self.assertEqual(second["updated_days"], 0)
        self.assertEqual(saved_twice["days"][first_day.isoformat()]["tokens"], 250)
        self.assertEqual(saved_twice["days"][second_day.isoformat()]["tokens"], 500)
        self.assertEqual(saved_twice["offline_sync"]["state"], "complete")

    def test_claude_daily_buckets_keep_days_separate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "claude.jsonl"
            rows = [
                {
                    "timestamp": "2026-07-10T10:00:00+08:00",
                    "message": {
                        "role": "assistant",
                        "model": "claude-test",
                        "usage": {"input_tokens": 100, "output_tokens": 20},
                    },
                },
                {
                    "timestamp": "2026-07-11T10:00:00+08:00",
                    "message": {
                        "role": "assistant",
                        "model": "claude-test",
                        "usage": {"input_tokens": 200, "output_tokens": 30},
                    },
                },
            ]
            path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")

            buckets = client_usage_export.scan_claude_daily_buckets(
                root,
                datetime(2026, 7, 10),
                datetime(2026, 7, 12),
            )

        self.assertEqual(buckets[date(2026, 7, 10)].total_tokens, 120)
        self.assertEqual(buckets[date(2026, 7, 11)].total_tokens, 230)


class ClaudeUsageEventTests(unittest.TestCase):
    @staticmethod
    def _row(
        timestamp: str,
        message_id: str,
        row_id: str,
        usage: dict[str, object],
        model: str = "claude-test",
    ) -> dict[str, object]:
        message: dict[str, object] = {
            "role": "assistant",
            "model": model,
            "usage": usage,
        }
        if message_id:
            message["id"] = message_id
        return {
            "type": "assistant",
            "timestamp": timestamp,
            "sessionId": "session-1",
            "uuid": row_id,
            "message": message,
        }

    @staticmethod
    def _write(path: Path, rows: list[dict[str, object]]) -> None:
        path.write_text(
            "\n".join(json.dumps(row) for row in rows),
            encoding="utf-8",
        )

    def test_same_claude_message_is_counted_once_across_content_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            usage = {
                "input_tokens": 100,
                "output_tokens": 20,
                "cache_creation_input_tokens": 30,
                "cache_read_input_tokens": 40,
            }
            self._write(
                root / "session.jsonl",
                [
                    self._row("2026-07-23T10:00:00+08:00", "msg-1", "row-1", usage),
                    self._row("2026-07-23T10:00:01+08:00", "msg-1", "row-2", usage),
                    self._row("2026-07-23T10:00:04+08:00", "msg-1", "row-3", usage),
                ],
            )
            start = datetime(2026, 7, 23)
            end = datetime(2026, 7, 24)
            events = client_usage_export.scan_claude_events(root, start, end)
            bucket = client_usage_export.bucket_from_claude_events(events)
            hourly = client_usage_export.claude_hourly_from_events(events)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].when, datetime(2026, 7, 23, 10, 0, 0))
        self.assertEqual(bucket.requests, 1)
        self.assertEqual(bucket.total_tokens, 190)
        self.assertEqual(bucket.input_tokens, 100)
        self.assertEqual(bucket.output_tokens, 20)
        self.assertEqual(bucket.cache_creation_input_tokens, 30)
        self.assertEqual(bucket.cache_read_input_tokens, 40)
        self.assertEqual(hourly[10]["requests"], 1)
        self.assertEqual(hourly[10]["tokens"], 190)

    def test_updated_snapshot_wins_without_merging_distinct_messages(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write(
                root / "session.jsonl",
                [
                    self._row(
                        "2026-07-23T11:00:00+08:00",
                        "msg-growing",
                        "row-1",
                        {"input_tokens": 100, "output_tokens": 10},
                    ),
                    self._row(
                        "2026-07-23T11:00:02+08:00",
                        "msg-growing",
                        "row-2",
                        {"input_tokens": 200, "output_tokens": 20},
                    ),
                    self._row(
                        "2026-07-23T11:00:02+08:00",
                        "msg-distinct",
                        "row-3",
                        {"input_tokens": 200, "output_tokens": 20},
                    ),
                ],
            )
            events = client_usage_export.scan_claude_events(
                root,
                datetime(2026, 7, 23),
                datetime(2026, 7, 24),
            )
            bucket = client_usage_export.bucket_from_claude_events(events)

        self.assertEqual(len(events), 2)
        self.assertEqual(bucket.requests, 2)
        self.assertEqual(bucket.total_tokens, 440)
        self.assertEqual(events[0].when, datetime(2026, 7, 23, 11, 0, 0))

    def test_claude_message_id_deduplicates_across_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shared = {"input_tokens": 300, "output_tokens": 30}
            self._write(
                root / "first.jsonl",
                [self._row("2026-07-23T12:00:00+08:00", "msg-shared", "row-1", shared)],
            )
            self._write(
                root / "second.jsonl",
                [self._row("2026-07-23T12:00:03+08:00", "msg-shared", "row-2", shared)],
            )
            bucket = client_usage_export.scan_claude(
                root,
                datetime(2026, 7, 23),
                datetime(2026, 7, 24),
            )

        self.assertEqual(bucket.requests, 1)
        self.assertEqual(bucket.total_tokens, 330)

    def test_missing_message_ids_are_not_fuzzily_merged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            usage = {"input_tokens": 80, "output_tokens": 20}
            self._write(
                root / "session.jsonl",
                [
                    self._row("2026-07-23T13:00:00+08:00", "", "row-1", usage),
                    self._row("2026-07-23T13:00:00+08:00", "", "row-2", usage),
                ],
            )
            bucket = client_usage_export.scan_claude(
                root,
                datetime(2026, 7, 23),
                datetime(2026, 7, 24),
            )

        self.assertEqual(bucket.requests, 2)
        self.assertEqual(bucket.total_tokens, 200)

    def test_duplicate_across_midnight_stays_on_first_observed_day(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            usage = {"input_tokens": 500, "output_tokens": 50}
            self._write(
                root / "session.jsonl",
                [
                    self._row("2026-07-23T23:59:59+08:00", "msg-midnight", "row-1", usage),
                    self._row("2026-07-24T00:00:01+08:00", "msg-midnight", "row-2", usage),
                ],
            )
            buckets = client_usage_export.scan_claude_daily_buckets(
                root,
                datetime(2026, 7, 23),
                datetime(2026, 7, 25),
            )

        self.assertEqual(buckets[date(2026, 7, 23)].requests, 1)
        self.assertEqual(buckets[date(2026, 7, 23)].total_tokens, 550)
        self.assertNotIn(date(2026, 7, 24), buckets)


class GrokUsageEventTests(unittest.TestCase):
    @staticmethod
    def _row(
        timestamp: str,
        prompt_id: str,
        model_usage: dict[str, dict[str, int]],
        *,
        session_id: str = "grok-session-1",
        session_update: str = "turn_completed",
        stop_reason: str = "end_turn",
    ) -> dict[str, object]:
        return {
            "timestamp": timestamp,
            "method": "session/update",
            "params": {
                "sessionId": session_id,
                "update": {
                    "sessionUpdate": session_update,
                    "prompt_id": prompt_id,
                    "stop_reason": stop_reason,
                    "usage": {"modelUsage": model_usage},
                },
            },
        }

    @staticmethod
    def _write(path: Path, rows: list[dict[str, object]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "\n".join(json.dumps(row) for row in rows) + "\n",
            encoding="utf-8",
        )

    @staticmethod
    def _detail(
        *,
        input_tokens: int,
        output_tokens: int,
        total_tokens: int,
        cached_read_tokens: int = 0,
        cache_creation_tokens: int = 0,
        model_calls: int = 1,
    ) -> dict[str, int]:
        return {
            "inputTokens": input_tokens,
            "outputTokens": output_tokens,
            "totalTokens": total_tokens,
            "cachedReadTokens": cached_read_tokens,
            "cacheCreationTokens": cache_creation_tokens,
            "modelCalls": model_calls,
        }

    def test_model_calls_preserve_request_count_tokens_and_cache_partition(self) -> None:
        row = self._row(
            "2026-07-23T10:00:00+08:00",
            "prompt-1",
            {
                "grok-test": self._detail(
                    input_tokens=900,
                    output_tokens=100,
                    total_tokens=1_000,
                    cached_read_tokens=600,
                    cache_creation_tokens=100,
                    model_calls=3,
                )
            },
        )

        events = client_usage_export.grok_usage_events_from_update_row(row)
        bucket = client_usage_export.bucket_from_grok_events(events)

        self.assertEqual(len(events), 3)
        self.assertEqual(bucket.requests, 3)
        self.assertEqual(bucket.total_tokens, 1_000)
        self.assertEqual(bucket.input_tokens, 200)
        self.assertEqual(bucket.cached_input_tokens, 700)
        self.assertEqual(bucket.output_tokens, 100)
        self.assertEqual(sum(event.total_tokens for event in events), 1_000)

    def test_multiple_models_and_duplicate_files_are_counted_once(self) -> None:
        row = self._row(
            "2026-07-23T11:00:00+08:00",
            "prompt-shared",
            {
                "grok-a": self._detail(
                    input_tokens=180,
                    output_tokens=20,
                    total_tokens=200,
                    model_calls=2,
                ),
                "grok-b": self._detail(
                    input_tokens=250,
                    output_tokens=50,
                    total_tokens=300,
                ),
            },
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write(root / "copy-a" / "updates.jsonl", [row])
            self._write(root / "copy-b" / "updates.jsonl", [row])

            events = client_usage_export.scan_grok_events(
                root,
                datetime(2026, 7, 23),
                datetime(2026, 7, 24),
            )
            bucket = client_usage_export.bucket_from_grok_events(events)

        self.assertEqual(len(events), 3)
        self.assertEqual(bucket.requests, 3)
        self.assertEqual(bucket.total_tokens, 500)
        self.assertEqual(bucket.models, {"grok-a": 200, "grok-b": 300})

    def test_cancelled_turn_with_usage_counts_but_incomplete_rows_do_not(self) -> None:
        completed = self._row(
            "2026-07-23T12:00:00+08:00",
            "prompt-cancelled",
            {
                "grok-test": self._detail(
                    input_tokens=80,
                    output_tokens=20,
                    total_tokens=100,
                )
            },
            stop_reason="cancelled",
        )
        incomplete = self._row(
            "2026-07-23T12:01:00+08:00",
            "prompt-incomplete",
            {
                "grok-test": self._detail(
                    input_tokens=900,
                    output_tokens=100,
                    total_tokens=1_000,
                )
            },
            session_update="turn_started",
        )
        no_usage = self._row(
            "2026-07-23T12:02:00+08:00",
            "prompt-empty",
            {"grok-test": self._detail(input_tokens=0, output_tokens=0, total_tokens=0)},
        )

        events = [
            *client_usage_export.grok_usage_events_from_update_row(completed),
            *client_usage_export.grok_usage_events_from_update_row(incomplete),
            *client_usage_export.grok_usage_events_from_update_row(no_usage),
        ]

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].total_tokens, 100)

    def test_missing_prompt_ids_use_stable_but_turn_specific_fallbacks(self) -> None:
        first = self._row(
            "2026-07-23T13:00:00+08:00",
            "",
            {"grok-test": self._detail(input_tokens=90, output_tokens=10, total_tokens=100)},
        )
        second = self._row(
            "2026-07-23T13:01:00+08:00",
            "",
            {"grok-test": self._detail(input_tokens=90, output_tokens=10, total_tokens=100)},
        )

        first_event = client_usage_export.grok_usage_events_from_update_row(first)[0]
        duplicate_event = client_usage_export.grok_usage_events_from_update_row(first)[0]
        second_event = client_usage_export.grok_usage_events_from_update_row(second)[0]

        self.assertEqual(first_event.request_key, duplicate_event.request_key)
        self.assertNotEqual(first_event.request_key, second_event.request_key)

    def test_live_watcher_emits_only_appended_completed_usage(self) -> None:
        completed = self._row(
            "2026-07-23T14:00:00+08:00",
            "prompt-live",
            {
                "grok-live": self._detail(
                    input_tokens=900,
                    output_tokens=100,
                    total_tokens=1_000,
                    cached_read_tokens=400,
                    model_calls=2,
                )
            },
        )
        incomplete = self._row(
            "2026-07-23T13:59:00+08:00",
            "prompt-started",
            {},
            session_update="turn_started",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "grok-session-1" / "updates.jsonl"
            self._write(path, [incomplete])
            watcher = monitor.GrokUsageFileWatcher(root)

            self.assertFalse(watcher.poll())
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(incomplete) + "\n")
            self.assertEqual(watcher.poll_events(), [])
            self.assertFalse(watcher.usage_changed)

            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(completed) + "\n")
            events = watcher.poll_events()

            exported = client_usage_export.grok_usage_events_from_update_row(completed)
            exported_ids = [client_usage_export.live_usage_event_id(event) for event in exported]
            watcher.close()

        self.assertEqual(len(events), 2)
        self.assertTrue(watcher.usage_changed)
        self.assertEqual(sum(int(event["total_tokens"]) for event in events), 1_000)
        self.assertEqual([event["event_id"] for event in events], exported_ids)
        self.assertTrue(all(event["provider"] == "Grok local" for event in events))
        self.assertTrue(all(event["route"] == "grok-local" for event in events))

    def test_grok_live_events_never_match_cockpit_markers(self) -> None:
        when = datetime(2026, 7, 23, 14, 0, tzinfo=timezone.utc)
        usage = {
            "when": when,
            "route": "grok-local",
            "session_id": "grok-session",
            "total_tokens": 1_000,
            "input_tokens": 900,
            "cached_tokens": 0,
            "output_tokens": 100,
        }
        marker = {
            "when": when,
            "label": "Codex local - account@example.com",
            "total_tokens": 1_000,
            "input_tokens": 900,
            "cached_tokens": 0,
            "output_tokens": 100,
        }

        self.assertEqual(monitor._match_live_cockpit_markers([usage], [marker]), {})

    def test_unknown_grok_live_model_does_not_borrow_codex_average_cost(self) -> None:
        usage = {
            "route": "grok-local",
            "total_tokens": 1_000,
            "input_tokens": 900,
            "cached_tokens": 0,
            "output_tokens": 100,
        }
        with patch.object(monitor, "_load_live_model_prices", return_value={}):
            cost = monitor.estimate_live_usage_cost(
                usage,
                "grok-unknown",
                fallback_cost_per_token=0.001,
            )

        self.assertEqual(cost, 0.0)

    def test_grok_build_parser_uses_local_default_pricing_model(self) -> None:
        row = self._row(
            "2026-08-22T10:00:00+08:00",
            "prompt-build",
            {
                "grok-4.6-build": self._detail(
                    input_tokens=900,
                    output_tokens=100,
                    total_tokens=1_000,
                )
            },
        )

        events = client_usage_export.grok_usage_events_from_update_row(
            row,
            default_model="grok-4.6",
        )

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].model, "grok-4.6-build")
        self.assertEqual(events[0].pricing_model, "xai/grok-4.6")
        self.assertEqual(events[0].route, "grok-local")

    def test_grok_build_parser_stays_unpriced_without_matching_default(self) -> None:
        row = self._row(
            "2026-08-22T10:00:00+08:00",
            "prompt-build",
            {
                "grok-4.6-build": self._detail(
                    input_tokens=900,
                    output_tokens=100,
                    total_tokens=1_000,
                )
            },
        )

        missing = client_usage_export.grok_usage_events_from_update_row(
            row,
            default_model="",
        )
        mismatched = client_usage_export.grok_usage_events_from_update_row(
            row,
            default_model="grok-build-0.1",
        )

        self.assertEqual(missing[0].model, "grok-4.6-build")
        self.assertEqual(missing[0].pricing_model, "")
        self.assertEqual(mismatched[0].model, "grok-4.6-build")
        self.assertEqual(mismatched[0].pricing_model, "")

    def test_grok_live_parser_uses_local_default_pricing_model(self) -> None:
        row = self._row(
            "2026-08-22T10:00:00+08:00",
            "prompt-build",
            {
                "grok-4.6-build": self._detail(
                    input_tokens=900,
                    output_tokens=100,
                    total_tokens=1_000,
                )
            },
        )

        events = monitor._grok_live_events_from_update_row(
            row,
            default_model="xai/grok-4.6",
        )

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["model"], "grok-4.6-build")
        self.assertEqual(events[0]["pricing_model"], "xai/grok-4.6")
        self.assertEqual(events[0]["provider"], "Grok local")


class ClientUsageSyncStatusTests(unittest.TestCase):
    def test_export_command_uses_python_for_source_script(self) -> None:
        with (
            patch.object(monitor, "CLIENT_USAGE_EXPORT", Path("C:/tools/export.py")),
            patch.object(monitor, "CLIENT_USAGE_PYTHON", "python.exe"),
        ):
            command = monitor.client_usage_export_command("--quota-only")

        self.assertEqual(
            command,
            ["python.exe", "C:\\tools\\export.py", "--quota-only"],
        )

    def test_export_command_runs_packaged_exporter_directly(self) -> None:
        with patch.object(
            monitor,
            "CLIENT_USAGE_EXPORT",
            Path("C:/Program Files/Token Pulse/TokenPulseExporter.exe"),
        ):
            command = monitor.client_usage_export_command("--quota-only")

        self.assertEqual(
            command,
            [
                "C:\\Program Files\\Token Pulse\\TokenPulseExporter.exe",
                "--quota-only",
            ],
        )

    def payload(self, tokens: int) -> dict:
        return {
            "date": monitor.today_key(),
            "today": {"requests": 1, "tokens": tokens, "cost": 0.1},
            "providers": [],
            "active_sessions": [],
            "latest_request": {},
            "dashboard": {},
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }

    def test_timeout_keeps_same_day_cache_and_marks_it_stale(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            export_path = root / "export.py"
            usage_path = root / "usage.json"
            export_path.write_text("# fixture", encoding="utf-8")
            usage_path.write_text(json.dumps(self.payload(100)), encoding="utf-8")
            with (
                patch.object(monitor, "CLIENT_USAGE_EXPORT", export_path),
                patch.object(monitor, "CLIENT_USAGE_JSON", usage_path),
                patch.object(
                    monitor.subprocess,
                    "run",
                    side_effect=monitor.subprocess.TimeoutExpired(["python"], 90),
                ),
            ):
                usage = monitor.load_client_usage()

        self.assertEqual(usage["tokens"], 100)
        self.assertEqual(usage["sync"]["state"], "timeout")
        self.assertTrue(usage["sync"]["cache_used"])
        self.assertTrue(usage["stale"])

    def test_cached_startup_load_does_not_wait_for_exporter(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            export_path = root / "export.py"
            usage_path = root / "usage.json"
            export_path.write_text("# fixture", encoding="utf-8")
            usage_path.write_text(json.dumps(self.payload(125)), encoding="utf-8")
            with (
                patch.object(monitor, "CLIENT_USAGE_EXPORT", export_path),
                patch.object(monitor, "CLIENT_USAGE_JSON", usage_path),
                patch.object(monitor.subprocess, "run") as run_export,
            ):
                usage = monitor.load_client_usage(run_export=False)

        run_export.assert_not_called()
        self.assertEqual(usage["tokens"], 125)
        self.assertEqual(usage["sync"]["state"], "cached")

    def test_cached_payload_preserves_usage_schema_versions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            export_path = root / "export.py"
            usage_path = root / "usage.json"
            export_path.write_text("# fixture", encoding="utf-8")
            payload = self.payload(125)
            payload.update(
                {
                    "schema": 1,
                    "usage_accounting_schema": 1,
                    "claude_usage_schema": 2,
                    "cockpit_usage_schema": 2,
                    "grok_usage_schema": 1,
                    "opencodex_attribution_schema": 1,
                }
            )
            usage_path.write_text(json.dumps(payload), encoding="utf-8")
            with (
                patch.object(monitor, "CLIENT_USAGE_EXPORT", export_path),
                patch.object(monitor, "CLIENT_USAGE_JSON", usage_path),
            ):
                usage = monitor.load_client_usage(run_export=False)

        self.assertEqual(usage["schema"], 1)
        self.assertEqual(usage["usage_accounting_schema"], 1)
        self.assertEqual(usage["claude_usage_schema"], 2)
        self.assertEqual(usage["cockpit_usage_schema"], 2)
        self.assertEqual(usage["grok_usage_schema"], 1)
        self.assertEqual(usage["opencodex_attribution_schema"], 1)

    def test_previous_day_cache_preserves_schema_for_live_checkpoint_restore(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            export_path = root / "export.py"
            usage_path = root / "usage.json"
            export_path.write_text("# fixture", encoding="utf-8")
            payload = self.payload(125)
            payload.update(
                {
                    "date": (datetime.now(monitor.CN_TZ).date() - timedelta(days=1)).isoformat(),
                    "schema": 1,
                    "usage_accounting_schema": 1,
                    "claude_usage_schema": 2,
                    "cockpit_usage_schema": 2,
                    "grok_usage_schema": 1,
                    "opencodex_attribution_schema": 1,
                }
            )
            usage_path.write_text(json.dumps(payload), encoding="utf-8")
            with (
                patch.object(monitor, "CLIENT_USAGE_EXPORT", export_path),
                patch.object(monitor, "CLIENT_USAGE_JSON", usage_path),
            ):
                usage = monitor.load_client_usage(run_export=False)

        self.assertEqual(usage["tokens"], 0)
        self.assertTrue(usage["stale"])
        self.assertEqual(usage["schema"], 1)
        self.assertEqual(usage["usage_accounting_schema"], 1)
        self.assertEqual(usage["claude_usage_schema"], 2)
        self.assertEqual(usage["cockpit_usage_schema"], 2)
        self.assertEqual(usage["grok_usage_schema"], 1)
        self.assertEqual(usage["opencodex_attribution_schema"], 1)

    def test_accounting_schema_survives_load_build_and_history_update(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            usage_path = root / "usage.json"
            history_path = root / "usage_history.json"
            day = monitor.today_key()
            payload = self.payload(150)
            payload.update(
                {
                    "schema": 1,
                    "usage_accounting_schema": 1,
                    "providers": [
                        {
                            "name": "Codex local - canonical@example.com",
                            "requests": 3,
                            "tokens": 150,
                            "cost": 1.5,
                            "models": {"gpt-new": 150},
                        }
                    ],
                }
            )
            payload["today"] = {
                "requests": 3,
                "tokens": 150,
                "cost": 1.5,
            }
            usage_path.write_text(json.dumps(payload), encoding="utf-8")
            monitor.write_json_atomic(
                history_path,
                {
                    "schema": 1,
                    "days": {
                        day: {
                            "date": day,
                            "source": "local",
                            "requests": 4,
                            "tokens": 200,
                            "cost": 2.0,
                            "source_date": day,
                            "models": {"gpt-old": 200},
                            "providers": [
                                {
                                    "name": "Codex local - wrong@example.com",
                                    "requests": 4,
                                    "tokens": 200,
                                    "cost": 2.0,
                                    "models": {"gpt-old": 200},
                                }
                            ],
                            "source_gap": {
                                "tokens": 50,
                                "reason": "legacy",
                            },
                        }
                    },
                },
            )

            with (
                patch.object(monitor, "CLIENT_USAGE_JSON", usage_path),
                patch.object(monitor, "USAGE_HISTORY_JSON", history_path),
                patch.object(monitor, "_USAGE_HISTORY_CACHE", None),
            ):
                state = monitor.build_local_monitor_state(refresh_usage=False)
                self.assertEqual(state.client_usage["usage_accounting_schema"], 1)
                monitor.update_usage_history(state)
                saved = monitor.load_usage_history()["days"][day]

        self.assertEqual(state.today_tokens, 150)
        self.assertEqual(saved["tokens"], 150)
        self.assertEqual(saved["requests"], 3)
        self.assertEqual(saved["usage_accounting_schema"], 1)
        self.assertNotIn("source_gap", saved)
        self.assertEqual(saved["models"], {"gpt-new": 150})
        self.assertEqual(
            [row["name"] for row in saved["providers"]],
            ["Codex local - canonical@example.com"],
        )

    def test_timeout_after_current_output_write_is_only_partial(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            export_path = root / "export.py"
            usage_path = root / "usage.json"
            export_path.write_text("# fixture", encoding="utf-8")
            usage_path.write_text(json.dumps(self.payload(100)), encoding="utf-8")

            def write_then_timeout(*_args, **_kwargs):
                usage_path.write_text(json.dumps(self.payload(250)), encoding="utf-8")
                raise monitor.subprocess.TimeoutExpired(["python"], 90)

            with (
                patch.object(monitor, "CLIENT_USAGE_EXPORT", export_path),
                patch.object(monitor, "CLIENT_USAGE_JSON", usage_path),
                patch.object(monitor.subprocess, "run", side_effect=write_then_timeout),
            ):
                usage = monitor.load_client_usage()

        self.assertEqual(usage["tokens"], 250)
        self.assertEqual(usage["sync"]["state"], "partial")
        self.assertTrue(usage["sync"]["fresh"])
        self.assertFalse(usage["stale"])

    def test_successful_export_reports_fresh_data(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            export_path = root / "export.py"
            usage_path = root / "usage.json"
            export_path.write_text("# fixture", encoding="utf-8")
            usage_path.write_text(json.dumps(self.payload(100)), encoding="utf-8")

            def write_success(*args, **_kwargs):
                usage_path.write_text(json.dumps(self.payload(300)), encoding="utf-8")
                return monitor.subprocess.CompletedProcess(args[0], 0, stderr="")

            with (
                patch.object(monitor, "CLIENT_USAGE_EXPORT", export_path),
                patch.object(monitor, "CLIENT_USAGE_JSON", usage_path),
                patch.object(monitor.subprocess, "run", side_effect=write_success),
            ):
                usage = monitor.load_client_usage()

        self.assertEqual(usage["tokens"], 300)
        self.assertEqual(usage["sync"]["state"], "ok")
        self.assertTrue(usage["sync"]["fresh"])
        self.assertFalse(usage["stale"])


class PackagedSmokeTestTests(unittest.TestCase):
    def test_smoke_test_initializes_and_closes_tk(self) -> None:
        root = MagicMock()
        with patch.object(monitor.tk, "Tk", return_value=root):
            result = monitor.run_monitor_smoke_test()

        self.assertEqual(result, 0)
        root.withdraw.assert_called_once_with()
        root.update_idletasks.assert_called_once_with()
        root.update.assert_called_once_with()
        root.destroy.assert_called_once_with()

    def test_smoke_test_returns_failure_when_tk_cannot_initialize(self) -> None:
        with patch.object(
            monitor.tk,
            "Tk",
            side_effect=monitor.tk.TclError("broken Tcl"),
        ):
            result = monitor.run_monitor_smoke_test()

        self.assertEqual(result, 1)


class AttributionLedgerTests(unittest.TestCase):
    def test_stable_event_id_wins_when_route_time_changes(self) -> None:
        event = client_usage_export.UsageEvent(
            when=datetime(2026, 6, 23, 16, 49, 45),
            request_at=datetime(2026, 6, 23, 16, 2, 46),
            model="gpt-test",
            input_tokens=100,
            cached_tokens=200,
            output_tokens=10,
            session_id="session-1",
        )
        stable_id = client_usage_export.codex_event_id(event)
        legacy_id = client_usage_export.legacy_codex_event_id(event)
        ledger = {
            stable_id: "Codex local - new-account@example.com",
            legacy_id: "Codex local - old-account@example.com",
        }

        attributed = client_usage_export.attribute_codex_events_by_account(
            [event],
            [],
            ledger,
        )

        self.assertIn("Codex local - new-account@example.com", attributed)
        self.assertNotIn("Codex local - old-account@example.com", attributed)

    def test_legacy_event_id_is_migrated_without_losing_attribution(self) -> None:
        event = client_usage_export.UsageEvent(
            when=datetime(2026, 6, 23, 16, 49, 45),
            request_at=datetime(2026, 6, 23, 16, 2, 46),
            model="gpt-test",
            input_tokens=100,
            cached_tokens=200,
            output_tokens=10,
            session_id="session-1",
        )
        stable_id = client_usage_export.codex_event_id(event)
        legacy_id = client_usage_export.legacy_codex_event_id(event)
        ledger = {legacy_id: "Codex local - account@example.com"}

        attributed = client_usage_export.attribute_codex_events_by_account(
            [event],
            [],
            ledger,
        )

        self.assertIn("Codex local - account@example.com", attributed)
        self.assertEqual(ledger[stable_id], "Codex local - account@example.com")

    def _external_event(
        self,
        model: str,
        when: datetime,
        tokens: int = 1_000,
        session_id: str = "mixed-session",
    ) -> client_usage_export.UsageEvent:
        return client_usage_export.UsageEvent(
            when=when,
            request_at=when - timedelta(seconds=5),
            model=model,
            input_tokens=tokens - 100,
            cached_tokens=0,
            output_tokens=100,
            session_id=session_id,
        )

    def test_xai_event_ignores_stale_gpt_ledger_and_current_account(self) -> None:
        event = self._external_event(
            "xai/grok-4.6",
            datetime(2026, 8, 21, 12, 0, 0),
        )
        stable_id = client_usage_export.codex_event_id(event)
        legacy_id = client_usage_export.legacy_codex_event_id(event)
        ledger = {
            stable_id: "Codex local - stale@example.com",
            legacy_id: "Codex local - stale@example.com",
        }
        switch = client_usage_export.AccountMarker(
            when=datetime(2026, 8, 21, 11, 0, 0),
            label="Codex local - current@example.com",
            kind="switch",
        )

        attributed = client_usage_export.attribute_codex_events_by_account(
            [event],
            [switch],
            ledger,
            current_label="Codex local - current@example.com",
            now=datetime(2026, 8, 21, 12, 0, 30),
        )

        self.assertEqual(list(attributed), [client_usage_export.GROK_SUBAGENT_LABEL])
        self.assertEqual(ledger[stable_id], client_usage_export.GROK_SUBAGENT_LABEL)
        self.assertEqual(ledger[legacy_id], client_usage_export.GROK_SUBAGENT_LABEL)

    def test_opencode_event_is_rewritten_off_stale_ledger(self) -> None:
        event = self._external_event(
            "opencode-go/deepseek-v4-pro",
            datetime(2026, 8, 21, 12, 1, 0),
        )
        stable_id = client_usage_export.codex_event_id(event)
        legacy_id = client_usage_export.legacy_codex_event_id(event)
        ledger = {
            stable_id: "Codex local - stale@example.com",
            legacy_id: "Codex local - stale@example.com",
        }

        attributed = client_usage_export.attribute_codex_events_by_account(
            [event],
            [],
            ledger,
            current_label="Codex local - current@example.com",
            now=datetime(2026, 8, 21, 12, 1, 10),
        )

        self.assertEqual(list(attributed), [client_usage_export.OPENCODE_SUBAGENT_LABEL])
        self.assertEqual(ledger[stable_id], client_usage_export.OPENCODE_SUBAGENT_LABEL)
        self.assertEqual(ledger[legacy_id], client_usage_export.OPENCODE_SUBAGENT_LABEL)

    def test_mixed_session_keeps_gpt_and_external_events_separate(self) -> None:
        gpt = client_usage_export.UsageEvent(
            when=datetime(2026, 8, 21, 12, 0, 0),
            model="gpt-5.4",
            input_tokens=900,
            cached_tokens=0,
            output_tokens=100,
            session_id="mixed-session",
        )
        grok = self._external_event(
            "xai/grok-4.6",
            datetime(2026, 8, 21, 12, 0, 10),
        )
        switch = client_usage_export.AccountMarker(
            when=datetime(2026, 8, 21, 11, 50, 0),
            label="Codex local - current@example.com",
            kind="switch",
        )

        attributed = client_usage_export.attribute_codex_events_by_account(
            [gpt, grok],
            [switch],
            {},
            current_label="Codex local - current@example.com",
            now=datetime(2026, 8, 21, 12, 0, 20),
        )
        resolved, session_accounts, unresolved = (
            client_usage_export.resolve_api_service_event_accounts(
                attributed,
                [],
            )
        )

        self.assertEqual(
            [event.model for event in resolved["Codex local - current@example.com"]],
            ["gpt-5.4"],
        )
        self.assertEqual(
            [event.model for event in resolved[client_usage_export.GROK_SUBAGENT_LABEL]],
            ["xai/grok-4.6"],
        )
        self.assertEqual(session_accounts["mixed-session"], client_usage_export.GROK_SUBAGENT_LABEL)
        self.assertEqual(unresolved, 0)

    def test_exact_cockpit_marker_cannot_reassign_external_event(self) -> None:
        event = self._external_event(
            "xai/grok-4.6",
            datetime(2026, 8, 21, 12, 2, 0),
            tokens=1_200,
        )
        marker = client_usage_export.AccountMarker(
            when=event.when,
            label="Codex local - api@example.com",
            model="gpt-5.4",
            total_tokens=event.total_tokens,
            input_tokens=event.input_tokens + event.cached_tokens,
            cached_tokens=event.cached_tokens,
            output_tokens=event.output_tokens,
            kind="request",
        )
        attributed = client_usage_export.attribute_codex_events_by_account(
            [event],
            [marker],
            {},
        )
        resolved, session_accounts, unresolved = (
            client_usage_export.resolve_api_service_event_accounts(
                attributed,
                [marker],
            )
        )

        self.assertEqual(list(resolved), [client_usage_export.GROK_SUBAGENT_LABEL])
        self.assertEqual(resolved[client_usage_export.GROK_SUBAGENT_LABEL][0].model, "xai/grok-4.6")
        self.assertEqual(session_accounts["mixed-session"], client_usage_export.GROK_SUBAGENT_LABEL)
        self.assertEqual(unresolved, 0)

    def test_archived_verdict_cannot_reassign_external_event(self) -> None:
        event = self._external_event(
            "opencode-go/deepseek-v4-flash",
            datetime(2026, 8, 21, 12, 3, 0),
        )
        event_id = client_usage_export.codex_event_id(event)
        verdicts = {
            event_id: {
                "label": "Codex local - archived@example.com",
                "tier": "cockpit_usage_row",
                "at": "2026-08-21T12:03:00+08:00",
            }
        }

        resolved, session_accounts, unresolved = (
            client_usage_export.resolve_api_service_event_accounts(
                {client_usage_export.API_SERVICE_AGGREGATE_LABEL: [event]},
                [],
                None,
                None,
                verdicts,
            )
        )

        self.assertEqual(list(resolved), [client_usage_export.OPENCODE_SUBAGENT_LABEL])
        self.assertEqual(session_accounts["mixed-session"], client_usage_export.OPENCODE_SUBAGENT_LABEL)
        self.assertEqual(unresolved, 0)

    def test_external_event_does_not_consume_matching_gpt_marker(self) -> None:
        gpt = client_usage_export.UsageEvent(
            when=datetime(2026, 8, 21, 12, 4, 0),
            model="gpt-5.4",
            input_tokens=1_100,
            cached_tokens=0,
            output_tokens=100,
            session_id="gpt-session",
        )
        grok = self._external_event(
            "xai/grok-4.6",
            datetime(2026, 8, 21, 12, 4, 1),
            tokens=1_200,
            session_id="grok-session",
        )
        marker = client_usage_export.AccountMarker(
            when=gpt.when,
            label="Codex local - api@example.com",
            model="gpt-5.4",
            total_tokens=gpt.total_tokens,
            input_tokens=gpt.input_tokens + gpt.cached_tokens,
            cached_tokens=gpt.cached_tokens,
            output_tokens=gpt.output_tokens,
            kind="request",
        )

        attributed = {
            client_usage_export.API_SERVICE_AGGREGATE_LABEL: [grok, gpt],
        }
        resolved, session_accounts, unresolved = (
            client_usage_export.resolve_api_service_event_accounts(
                attributed,
                [marker],
            )
        )

        self.assertEqual(
            [event.session_id for event in resolved["Codex local - api@example.com"]],
            ["gpt-session"],
        )
        self.assertEqual(
            [event.session_id for event in resolved[client_usage_export.GROK_SUBAGENT_LABEL]],
            ["grok-session"],
        )
        self.assertEqual(session_accounts["gpt-session"], "Codex local - api@example.com")
        self.assertEqual(session_accounts["grok-session"], client_usage_export.GROK_SUBAGENT_LABEL)
        self.assertEqual(unresolved, 0)

    def test_external_event_with_official_fingerprint_stays_off_quota_window(self) -> None:
        window = {
            "window_minutes": 10_080,
            "resets_at": "2026-08-27T12:00:00+08:00",
        }
        fingerprint = client_usage_export.quota_window_fingerprint(window)
        event = self._external_event(
            "xai/grok-4.6",
            datetime(2026, 8, 21, 12, 5, 0),
        )
        event.quota_fingerprints = (fingerprint,)

        self.assertIsNotNone(fingerprint)
        self.assertFalse(
            client_usage_export.event_counts_toward_official_quota_window(event, window)
        )


class AttributionVerdictArchiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.original_ledger_path = client_usage_export.ATTRIBUTION_LEDGER_PATH
        client_usage_export.ATTRIBUTION_LEDGER_PATH = (
            Path(self.temporary_directory.name) / "client_usage_attribution_ledger.json"
        )
        client_usage_export._ATTRIBUTION_LEDGER_DOCUMENT_CACHE = None
        client_usage_export._LEDGER_DIRTY = False
        client_usage_export._LEDGER_WRITES = set()
        client_usage_export._VERDICT_ARCHIVE_DIRTY = False
        client_usage_export._VERDICT_ARCHIVE_WRITES = set()

    def tearDown(self) -> None:
        client_usage_export.ATTRIBUTION_LEDGER_PATH = self.original_ledger_path
        client_usage_export._ATTRIBUTION_LEDGER_DOCUMENT_CACHE = None
        client_usage_export._LEDGER_DIRTY = False
        client_usage_export._LEDGER_WRITES = set()
        client_usage_export._VERDICT_ARCHIVE_DIRTY = False
        client_usage_export._VERDICT_ARCHIVE_WRITES = set()
        self.temporary_directory.cleanup()

    @staticmethod
    def api_service_event(
        when: datetime,
        total_tokens: int,
        session_id: str = "session-verdict",
        turn_started_at: datetime | None = None,
    ) -> client_usage_export.UsageEvent:
        return client_usage_export.UsageEvent(
            when=when,
            model="gpt-test",
            input_tokens=total_tokens - 100,
            cached_tokens=0,
            output_tokens=100,
            session_id=session_id,
            account_at=turn_started_at or (when - timedelta(seconds=30)),
        )

    def test_archived_verdict_only_fills_events_this_run_cannot_resolve(self) -> None:
        event = self.api_service_event(datetime(2026, 7, 26, 9, 0, 0), 1_000)
        event_id = client_usage_export.codex_event_id(event)
        stale_marker = client_usage_export.AccountMarker(
            when=datetime(2026, 7, 26, 3, 0, 0),
            label="Codex local - other@example.com",
            total_tokens=999_999,
            kind="request",
        )
        verdicts = {
            event_id: {
                "label": "Codex local - archived@example.com",
                "tier": "cockpit_usage_row",
                "at": "2026-07-26T09:00:00+08:00",
            }
        }

        resolved, _session_accounts, unresolved = (
            client_usage_export.resolve_api_service_event_accounts(
                {client_usage_export.API_SERVICE_AGGREGATE_LABEL: [event]},
                [stale_marker],
                None,
                None,
                verdicts,
            )
        )

        self.assertEqual(list(resolved), ["Codex local - archived@example.com"])
        self.assertEqual(unresolved, 0)
        self.assertEqual(
            sum(item.total_tokens for events in resolved.values() for item in events),
            1_000,
        )

    def test_archive_never_overrides_a_concrete_verdict_from_this_run(self) -> None:
        event = self.api_service_event(datetime(2026, 7, 26, 9, 0, 0), 1_000)
        event_id = client_usage_export.codex_event_id(event)
        marker = client_usage_export.AccountMarker(
            when=event.when,
            label="Codex local - live@example.com",
            model="gpt-5.6-sol",
            total_tokens=1_000,
            kind="request",
        )
        verdicts = {
            event_id: {
                "label": "Codex local - archived@example.com",
                "tier": "cockpit_usage_row",
                "at": "2026-07-20T09:00:00+08:00",
            }
        }

        resolved, _session_accounts, unresolved = (
            client_usage_export.resolve_api_service_event_accounts(
                {client_usage_export.API_SERVICE_AGGREGATE_LABEL: [event]},
                [marker],
                None,
                None,
                verdicts,
            )
        )

        self.assertEqual(list(resolved), ["Codex local - live@example.com"])
        self.assertEqual(unresolved, 0)
        # The archive is upgraded to this run's verdict under the pre-mutation id,
        # even though the matched marker rewrote event.model.
        self.assertEqual(event.model, "gpt-5.6-sol")
        self.assertEqual(verdicts[event_id]["label"], "Codex local - live@example.com")
        self.assertEqual(verdicts[event_id]["tier"], "cockpit_usage_row")

    def test_unresolved_events_stay_unresolved_without_an_archived_verdict(self) -> None:
        event = self.api_service_event(datetime(2026, 7, 26, 9, 0, 0), 1_000)
        stale_marker = client_usage_export.AccountMarker(
            when=datetime(2026, 7, 26, 3, 0, 0),
            label="Codex local - other@example.com",
            total_tokens=999_999,
            kind="request",
        )
        verdicts: dict[str, dict[str, str]] = {}

        resolved, _session_accounts, unresolved = (
            client_usage_export.resolve_api_service_event_accounts(
                {client_usage_export.API_SERVICE_AGGREGATE_LABEL: [event]},
                [stale_marker],
                None,
                None,
                verdicts,
            )
        )

        self.assertEqual(list(resolved), [client_usage_export.API_SERVICE_AGGREGATE_LABEL])
        self.assertEqual(unresolved, 1)
        self.assertEqual(verdicts, {})

    def test_historical_cockpit_marker_does_not_override_direct_account_label(self) -> None:
        now = datetime(2026, 7, 26, 9, 0, 0)
        direct_label = "Codex local - direct@example.com"
        event = self.api_service_event(now, 1_000)
        stale_marker = client_usage_export.AccountMarker(
            when=now - timedelta(days=7),
            label="Codex local - old-api@example.com",
            total_tokens=999_999,
            kind="request",
        )

        resolved, session_accounts, unresolved = (
            client_usage_export.resolve_api_service_event_accounts(
                {direct_label: [event]},
                [stale_marker],
                preserve_direct_official_usage=True,
            )
        )

        self.assertEqual(resolved, {direct_label: [event]})
        self.assertEqual(session_accounts[event.session_id], direct_label)
        self.assertEqual(unresolved, 0)

    def test_nearby_cockpit_marker_keeps_unconfirmed_event_pending(self) -> None:
        now = datetime(2026, 7, 26, 9, 0, 0)
        direct_label = "Codex local - direct@example.com"
        event = self.api_service_event(now, 1_000)
        nearby_marker = client_usage_export.AccountMarker(
            when=now + timedelta(seconds=30),
            label="Codex local - api@example.com",
            total_tokens=999_999,
            kind="request",
        )

        resolved, _session_accounts, unresolved = (
            client_usage_export.resolve_api_service_event_accounts(
                {direct_label: [event]},
                [nearby_marker],
                preserve_direct_official_usage=True,
            )
        )

        self.assertEqual(
            list(resolved),
            [client_usage_export.API_SERVICE_AGGREGATE_LABEL],
        )
        self.assertEqual(unresolved, 1)

    def test_quota_window_filter_excludes_provider_qualified_external_models(self) -> None:
        window = {
            "window_minutes": 10_080,
            "resets_at": "2026-08-27T12:00:00+08:00",
        }
        official = self.api_service_event(datetime(2026, 8, 21, 12, 0, 0), 1_000)
        external = self.api_service_event(datetime(2026, 8, 21, 12, 1, 0), 1_000)
        external.model = "xai/grok-4.1"

        self.assertTrue(
            client_usage_export.event_counts_toward_official_quota_window(official, window)
        )
        self.assertFalse(
            client_usage_export.event_counts_toward_official_quota_window(external, window)
        )
        window_with_fingerprint = {
            "window_minutes": 10_080,
            "resets_at": "2026-08-27T12:00:00+08:00",
        }
        fingerprint = client_usage_export.quota_window_fingerprint(window_with_fingerprint)
        self.assertIsNotNone(fingerprint)
        external.quota_fingerprints = (fingerprint,)
        self.assertFalse(
            client_usage_export.event_counts_toward_official_quota_window(
                external,
                window_with_fingerprint,
            )
        )

    def test_low_tier_verdicts_are_not_archived(self) -> None:
        verdicts: dict[str, dict[str, str]] = {}

        stored = client_usage_export.record_attribution_verdict(
            verdicts,
            "event-1",
            "Codex local - guess@example.com",
            "temporal",
            datetime(2026, 7, 26, 9, 0, 0),
        )

        self.assertFalse(stored)
        self.assertEqual(verdicts, {})
        self.assertFalse(client_usage_export._VERDICT_ARCHIVE_DIRTY)

    def test_mirror_labels_are_never_archived(self) -> None:
        verdicts: dict[str, dict[str, str]] = {}

        stored = client_usage_export.record_attribution_verdict(
            verdicts,
            "event-1",
            client_usage_export.API_SERVICE_AGGREGATE_LABEL,
            "cockpit_usage_row",
            datetime(2026, 7, 26, 9, 0, 0),
        )

        self.assertFalse(stored)
        self.assertEqual(verdicts, {})

    def test_verdict_tier_follows_the_evidence_behind_the_marker(self) -> None:
        request_marker = client_usage_export.AccountMarker(
            when=datetime(2026, 7, 26, 9, 0, 0),
            label="Codex local - account@example.com",
            kind="request",
        )
        affinity_marker = client_usage_export.AccountMarker(
            when=datetime(2026, 7, 26, 9, 0, 0),
            label="Codex local - account@example.com",
            kind="affinity",
        )
        switch_marker = client_usage_export.AccountMarker(
            when=datetime(2026, 7, 26, 9, 0, 0),
            label="Codex local - account@example.com",
            kind="switch",
        )

        # Only an exact usage-row match for this very event is archive-grade.
        self.assertEqual(
            client_usage_export.api_service_verdict_tier(request_marker, True),
            "cockpit_usage_row",
        )
        self.assertEqual(
            client_usage_export.api_service_verdict_tier(request_marker, False),
            "temporal",
        )
        self.assertEqual(
            client_usage_export.api_service_verdict_tier(affinity_marker, True),
            "temporal",
        )
        self.assertEqual(
            client_usage_export.api_service_verdict_tier(affinity_marker, False),
            "temporal",
        )
        self.assertEqual(
            client_usage_export.api_service_verdict_tier(switch_marker, False),
            "temporal",
        )
        self.assertLess(
            client_usage_export.api_service_verdict_tier_rank("temporal"),
            client_usage_export.API_SERVICE_VERDICT_ARCHIVE_MIN_TIER,
        )

    def test_lower_tier_verdict_cannot_downgrade_the_same_archived_account(self) -> None:
        verdicts = {
            "event-1": {
                "label": "Codex local - strong@example.com",
                "tier": "cockpit_usage_row",
                "at": "2026-07-26T09:00:00+08:00",
            }
        }

        client_usage_export.record_attribution_verdict(
            verdicts,
            "event-1",
            "Codex local - strong@example.com",
            "affinity_confirmed",
            datetime(2026, 7, 26, 10, 0, 0),
        )

        self.assertEqual(verdicts["event-1"]["label"], "Codex local - strong@example.com")
        self.assertEqual(verdicts["event-1"]["tier"], "cockpit_usage_row")

    def test_live_verdict_overrides_an_archived_entry_that_names_another_account(self) -> None:
        verdicts = {
            "event-1": {
                "label": "Codex local - wrong@example.com",
                "tier": "cockpit_usage_row",
                "at": "2026-07-20T09:00:00+08:00",
            }
        }

        stored = client_usage_export.record_attribution_verdict(
            verdicts,
            "event-1",
            "Codex local - right@example.com",
            "affinity_confirmed",
            datetime(2026, 7, 26, 10, 0, 0),
        )

        # A lower tier still wins: this run's evidence decided the number the
        # user is looking at, so the archive may not keep contradicting it.
        self.assertTrue(stored)
        self.assertEqual(verdicts["event-1"]["label"], "Codex local - right@example.com")
        self.assertEqual(verdicts["event-1"]["tier"], "affinity_confirmed")
        self.assertTrue(verdicts["event-1"]["at"].startswith("2026-07-26T10:00:00"))

    def test_expired_verdicts_are_pruned_on_save(self) -> None:
        now = datetime(2026, 7, 26, 9, 0, 0)
        verdicts = {
            "fresh": {
                "label": "Codex local - fresh@example.com",
                "tier": "cockpit_usage_row",
                "at": (now - timedelta(days=1)).replace(
                    tzinfo=client_usage_export.LOCAL_TZ
                ).isoformat(timespec="seconds"),
            },
            "expired": {
                "label": "Codex local - expired@example.com",
                "tier": "cockpit_usage_row",
                "at": (
                    now
                    - timedelta(days=client_usage_export.API_SERVICE_VERDICT_RETENTION_DAYS + 1)
                ).replace(tzinfo=client_usage_export.LOCAL_TZ).isoformat(timespec="seconds"),
            },
        }

        kept = client_usage_export.prune_attribution_verdicts(verdicts, now)

        self.assertEqual(list(kept), ["fresh"])

    def test_verdict_archive_is_capped_by_entry_count(self) -> None:
        now = datetime(2026, 7, 26, 9, 0, 0)
        verdicts = {
            f"event-{index}": {
                "label": f"Codex local - account{index}@example.com",
                "tier": "cockpit_usage_row",
                "at": (now - timedelta(hours=index)).replace(
                    tzinfo=client_usage_export.LOCAL_TZ
                ).isoformat(timespec="seconds"),
            }
            for index in range(5)
        }

        with patch.object(client_usage_export, "API_SERVICE_VERDICT_ARCHIVE_LIMIT", 2):
            kept = client_usage_export.prune_attribution_verdicts(verdicts, now)

        self.assertEqual(sorted(kept), ["event-0", "event-1"])

    def test_legacy_ledger_without_verdicts_loads_and_keeps_events(self) -> None:
        client_usage_export.write_json_atomic(
            client_usage_export.ATTRIBUTION_LEDGER_PATH,
            {
                "schema": 1,
                "updated_at": "2026-07-25T09:00:00+08:00",
                "events": {"event-1": "Codex local - account@example.com"},
            },
        )

        ledger = client_usage_export.load_attribution_ledger()
        verdicts = client_usage_export.load_attribution_verdicts()

        self.assertEqual(ledger, {"event-1": "Codex local - account@example.com"})
        self.assertEqual(verdicts, {})

    def test_corrupt_ledger_restores_events_and_verdicts_for_later_reads(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger_path = Path(directory) / "attribution.json"
            backup_path = ledger_path.with_name(f"{ledger_path.name}.bak")
            document = {
                "schema": 1,
                "events": {"event-1": "Codex local - account@example.com"},
                "verdicts": {
                    "event-1": {
                        "label": "Codex local - account@example.com",
                        "tier": "cockpit_usage_row",
                        "at": "2026-07-26T09:00:00+08:00",
                    }
                },
            }
            client_usage_export.write_json_atomic(backup_path, document)
            ledger_path.write_text("{broken", encoding="utf-8")

            with patch.object(
                client_usage_export,
                "ATTRIBUTION_LEDGER_PATH",
                ledger_path,
            ):
                client_usage_export._ATTRIBUTION_LEDGER_DOCUMENT_CACHE = None
                ledger = client_usage_export.load_attribution_ledger()
                verdicts = client_usage_export.load_attribution_verdicts()
                client_usage_export._ATTRIBUTION_LEDGER_DOCUMENT_CACHE = None
                reloaded = client_usage_export.load_attribution_verdicts()
                primary_recreated = ledger_path.exists()

            client_usage_export._ATTRIBUTION_LEDGER_DOCUMENT_CACHE = None

        self.assertEqual(
            ledger,
            {"event-1": "Codex local - account@example.com"},
        )
        self.assertEqual(verdicts, document["verdicts"])
        self.assertEqual(reloaded, document["verdicts"])
        self.assertTrue(primary_recreated)

    def test_verdicts_round_trip_beside_the_legacy_event_labels(self) -> None:
        now = datetime(2026, 7, 26, 9, 0, 0)
        ledger = {"event-1": client_usage_export.API_SERVICE_AGGREGATE_LABEL}
        verdicts: dict[str, dict[str, str]] = {}
        client_usage_export.record_attribution_verdict(
            verdicts,
            "event-1",
            "Codex local - account@example.com",
            "cockpit_usage_row",
            now,
        )

        client_usage_export.save_attribution_ledger(ledger, now, verdicts)
        client_usage_export._ATTRIBUTION_LEDGER_DOCUMENT_CACHE = None
        document = json.loads(
            client_usage_export.ATTRIBUTION_LEDGER_PATH.read_text(encoding="utf-8")
        )

        self.assertEqual(document["events"], ledger)
        self.assertEqual(
            client_usage_export.load_attribution_verdicts()["event-1"]["label"],
            "Codex local - account@example.com",
        )

    def test_saving_without_verdicts_keeps_the_existing_archive(self) -> None:
        now = datetime(2026, 7, 26, 9, 0, 0)
        client_usage_export.write_json_atomic(
            client_usage_export.ATTRIBUTION_LEDGER_PATH,
            {
                "schema": 1,
                "updated_at": "2026-07-25T09:00:00+08:00",
                "events": {"event-1": client_usage_export.API_SERVICE_AGGREGATE_LABEL},
                "verdicts": {
                    "event-1": {
                        "label": "Codex local - account@example.com",
                        "tier": "cockpit_usage_row",
                        "at": "2026-07-25T09:00:00+08:00",
                    }
                },
            },
        )
        client_usage_export._ATTRIBUTION_LEDGER_DOCUMENT_CACHE = None
        ledger = client_usage_export.load_attribution_ledger()
        client_usage_export.ledger_assign(ledger, "event-2", "Codex local - account@example.com")

        client_usage_export.save_attribution_ledger(ledger, now)
        client_usage_export._ATTRIBUTION_LEDGER_DOCUMENT_CACHE = None

        self.assertEqual(
            client_usage_export.load_attribution_verdicts()["event-1"]["label"],
            "Codex local - account@example.com",
        )

    def test_below_floor_tiers_are_ignored_when_the_archive_is_read(self) -> None:
        client_usage_export.write_json_atomic(
            client_usage_export.ATTRIBUTION_LEDGER_PATH,
            {
                "schema": 1,
                "events": {},
                "verdicts": {
                    "event-1": {
                        "label": "Codex local - guess@example.com",
                        "tier": "temporal",
                        "at": "2026-07-26T09:00:00+08:00",
                    },
                    "event-2": {
                        "label": client_usage_export.API_SERVICE_AGGREGATE_LABEL,
                        "tier": "cockpit_usage_row",
                        "at": "2026-07-26T09:00:00+08:00",
                    },
                },
            },
        )
        client_usage_export._ATTRIBUTION_LEDGER_DOCUMENT_CACHE = None

        self.assertEqual(client_usage_export.load_attribution_verdicts(), {})

    def test_fuzzy_token_match_decides_the_label_but_is_never_archived(self) -> None:
        event = self.api_service_event(datetime(2026, 7, 26, 9, 0, 0), 100_000)
        event_id = client_usage_export.codex_event_id(event)
        fuzzy_marker = client_usage_export.AccountMarker(
            when=event.when + timedelta(seconds=12),
            label="Codex local - fuzzy@example.com",
            total_tokens=100_400,
            kind="request",
        )
        verdicts: dict[str, dict[str, str]] = {}

        resolved, _session_accounts, unresolved = (
            client_usage_export.resolve_api_service_event_accounts(
                {client_usage_export.API_SERVICE_AGGREGATE_LABEL: [event]},
                [fuzzy_marker],
                None,
                None,
                verdicts,
            )
        )

        # The token totals differ, so this is a 30 second guess: it still decides
        # this run's label, but it must never freeze into the archive.
        self.assertEqual(list(resolved), ["Codex local - fuzzy@example.com"])
        self.assertEqual(unresolved, 0)
        self.assertNotIn(event_id, verdicts)
        self.assertEqual(verdicts, {})
        self.assertFalse(client_usage_export._VERDICT_ARCHIVE_DIRTY)

    def test_near_time_turn_anchor_decides_the_label_but_is_never_archived(self) -> None:
        turn_started_at = datetime(2026, 7, 26, 8, 59, 30)
        first = self.api_service_event(
            datetime(2026, 7, 26, 9, 0, 0),
            1_000,
            turn_started_at=turn_started_at,
        )
        second = self.api_service_event(
            datetime(2026, 7, 26, 9, 5, 0),
            2_000,
            turn_started_at=turn_started_at,
        )
        near_time_marker = client_usage_export.AccountMarker(
            when=datetime(2026, 7, 26, 9, 0, 1),
            label="Codex local - neartime@example.com",
            total_tokens=5_000,
            kind="request",
        )
        verdicts: dict[str, dict[str, str]] = {}

        resolved, _session_accounts, unresolved = (
            client_usage_export.resolve_api_service_event_accounts(
                {client_usage_export.API_SERVICE_AGGREGATE_LABEL: [first, second]},
                [near_time_marker],
                None,
                None,
                verdicts,
            )
        )

        # Pure +-2s proximity between differing token totals is not affinity
        # evidence, so nothing here is archive-grade.
        self.assertEqual(list(resolved), ["Codex local - neartime@example.com"])
        self.assertEqual(unresolved, 0)
        self.assertEqual(verdicts, {})

    def test_nearest_turn_start_affinity_anchor_is_never_archived(self) -> None:
        turn_started_at = datetime(2026, 7, 26, 8, 59, 30)
        event = self.api_service_event(
            datetime(2026, 7, 26, 9, 0, 0),
            1_000,
            turn_started_at=turn_started_at,
        )
        affinity_events = [
            client_usage_export.CockpitAffinityEvent(
                when=turn_started_at + timedelta(milliseconds=50),
                request_id="request-stable",
                source="execution_session_id",
                session_key="execution-1",
                account_id="plus-id",
                label="Codex local - plus@example.com",
                action="cache hit",
            ),
            client_usage_export.CockpitAffinityEvent(
                when=turn_started_at + timedelta(milliseconds=80),
                request_id="request-unevidenced",
                source="execution_session_id",
                session_key="execution-2",
                account_id="other-id",
                label="Codex local - other@example.com",
                action="cache miss",
            ),
        ]
        verdicts: dict[str, dict[str, str]] = {}

        resolved, _session_accounts, unresolved = (
            client_usage_export.resolve_api_service_event_accounts(
                {client_usage_export.API_SERVICE_AGGREGATE_LABEL: [event]},
                [],
                None,
                affinity_events,
                verdicts,
            )
        )

        # An unconfirmed execution_session_id hit at the turn edge names the
        # account for this run only; no request_id was confirmed, so it stays
        # out of the archive.
        self.assertEqual(list(resolved), ["Codex local - plus@example.com"])
        self.assertEqual(unresolved, 0)
        self.assertEqual(verdicts, {})

    def test_only_the_directly_matched_event_of_a_turn_is_archived(self) -> None:
        turn_started_at = datetime(2026, 7, 26, 8, 59, 30)
        matched = self.api_service_event(
            datetime(2026, 7, 26, 9, 0, 0),
            1_000,
            turn_started_at=turn_started_at,
        )
        inherited = self.api_service_event(
            datetime(2026, 7, 26, 9, 5, 0),
            2_000,
            turn_started_at=turn_started_at,
        )
        matched_id = client_usage_export.codex_event_id(matched)
        inherited_id = client_usage_export.codex_event_id(inherited)
        marker = client_usage_export.AccountMarker(
            when=datetime(2026, 7, 26, 9, 0, 1),
            label="Codex local - turn@example.com",
            total_tokens=1_000,
            kind="request",
        )
        verdicts: dict[str, dict[str, str]] = {}

        resolved, _session_accounts, unresolved = (
            client_usage_export.resolve_api_service_event_accounts(
                {client_usage_export.API_SERVICE_AGGREGATE_LABEL: [matched, inherited]},
                [marker],
                None,
                None,
                verdicts,
            )
        )

        # Both events land on the account this run, but the second one only
        # inherited the turn anchor, so one match may not archive the whole turn.
        self.assertEqual(list(resolved), ["Codex local - turn@example.com"])
        self.assertEqual(unresolved, 0)
        self.assertEqual(list(verdicts), [matched_id])
        self.assertNotIn(inherited_id, verdicts)
        self.assertEqual(verdicts[matched_id]["tier"], "cockpit_usage_row")

    def test_concurrent_saves_merge_instead_of_dropping_verdicts(self) -> None:
        now = datetime(2026, 7, 26, 9, 0, 0)
        ledger = {"event-a": client_usage_export.API_SERVICE_AGGREGATE_LABEL}
        first_run: dict[str, dict[str, str]] = {}
        client_usage_export.record_attribution_verdict(
            first_run,
            "event-a",
            "Codex local - first@example.com",
            "cockpit_usage_row",
            now,
        )
        client_usage_export.save_attribution_ledger(ledger, now, first_run)

        # A second exporter that loaded the ledger before the first one saved.
        client_usage_export._VERDICT_ARCHIVE_WRITES = set()
        second_run: dict[str, dict[str, str]] = {}
        client_usage_export.record_attribution_verdict(
            second_run,
            "event-b",
            "Codex local - second@example.com",
            "cockpit_usage_row",
            now,
        )
        client_usage_export.save_attribution_ledger(ledger, now, second_run)
        client_usage_export._ATTRIBUTION_LEDGER_DOCUMENT_CACHE = None

        archived = client_usage_export.load_attribution_verdicts()
        self.assertEqual(sorted(archived), ["event-a", "event-b"])
        self.assertEqual(archived["event-a"]["label"], "Codex local - first@example.com")
        self.assertEqual(archived["event-b"]["label"], "Codex local - second@example.com")

    def test_stale_writer_preserves_event_labels_saved_by_another_exporter(self) -> None:
        now = datetime(2026, 7, 26, 9, 0, 0)
        with tempfile.TemporaryDirectory() as directory:
            ledger_path = Path(directory) / "attribution.json"
            base = {"base": "Codex local - base@example.com"}
            client_usage_export.write_json_atomic(
                ledger_path,
                {"schema": 1, "events": base},
            )
            with patch.object(
                client_usage_export,
                "ATTRIBUTION_LEDGER_PATH",
                ledger_path,
            ):
                client_usage_export._ATTRIBUTION_LEDGER_DOCUMENT_CACHE = None
                first = {**base, "event-a": "Codex local - first@example.com"}
                client_usage_export._LEDGER_DIRTY = True
                client_usage_export._LEDGER_WRITES = {"event-a"}
                client_usage_export.save_attribution_ledger(first, now)

                second = {**base, "event-b": "Codex local - second@example.com"}
                client_usage_export._LEDGER_DIRTY = True
                client_usage_export._LEDGER_WRITES = {"event-b"}
                client_usage_export.save_attribution_ledger(second, now)
                saved = json.loads(ledger_path.read_text(encoding="utf-8"))

            client_usage_export._ATTRIBUTION_LEDGER_DOCUMENT_CACHE = None
            client_usage_export._LEDGER_DIRTY = False
            client_usage_export._LEDGER_WRITES = set()

        self.assertEqual(
            saved["events"],
            {
                **base,
                "event-a": "Codex local - first@example.com",
                "event-b": "Codex local - second@example.com",
            },
        )

    def test_attribution_ledger_lock_serializes_writers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger_path = Path(directory) / "attribution.json"
            first_entered = threading.Event()
            release_first = threading.Event()
            second_entered = threading.Event()

            def first_writer() -> None:
                with client_usage_export.attribution_ledger_write_lock(
                    ledger_path,
                    timeout_seconds=2,
                ):
                    first_entered.set()
                    release_first.wait(2)

            def second_writer() -> None:
                first_entered.wait(2)
                with client_usage_export.attribution_ledger_write_lock(
                    ledger_path,
                    timeout_seconds=2,
                ):
                    second_entered.set()

            first = threading.Thread(target=first_writer)
            second = threading.Thread(target=second_writer)
            first.start()
            self.assertTrue(first_entered.wait(2))
            second.start()
            self.assertFalse(second_entered.wait(0.1))
            release_first.set()
            first.join(2)
            second.join(2)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertTrue(second_entered.is_set())

    def test_shrink_guard_keeps_the_events_on_disk_but_still_saves_verdicts(self) -> None:
        now = datetime(2026, 7, 26, 9, 0, 0)
        stored_events = {
            f"event-{index}": "Codex local - account@example.com"
            for index in range(1_500)
        }
        client_usage_export.write_json_atomic(
            client_usage_export.ATTRIBUTION_LEDGER_PATH,
            {"schema": 1, "events": stored_events},
        )
        client_usage_export._ATTRIBUTION_LEDGER_DOCUMENT_CACHE = None
        verdicts: dict[str, dict[str, str]] = {}
        client_usage_export.record_attribution_verdict(
            verdicts,
            "event-0",
            "Codex local - archived@example.com",
            "cockpit_usage_row",
            now,
        )
        client_usage_export._LEDGER_DIRTY = True

        client_usage_export.save_attribution_ledger({"event-0": "x"}, now, verdicts)
        document = json.loads(
            client_usage_export.ATTRIBUTION_LEDGER_PATH.read_text(encoding="utf-8")
        )

        # The truncated event map is refused, but the archive still lands.
        self.assertEqual(len(document["events"]), 1_500)
        self.assertEqual(
            document["verdicts"]["event-0"]["label"],
            "Codex local - archived@example.com",
        )
        self.assertTrue(client_usage_export._LEDGER_DIRTY)
        self.assertFalse(client_usage_export._VERDICT_ARCHIVE_DIRTY)

    def test_today_and_the_window_stats_read_the_same_archive(self) -> None:
        now = datetime(2026, 7, 26, 12, 0, 0)
        archived_label = "Codex local - archived@example.com"

        def codex_event() -> client_usage_export.UsageEvent:
            return client_usage_export.UsageEvent(
                when=now - timedelta(hours=1),
                model="gpt-test",
                input_tokens=900,
                cached_tokens=0,
                output_tokens=100,
                session_id="session-window",
                account_at=now - timedelta(hours=1, seconds=30),
            )

        stale_marker = client_usage_export.AccountMarker(
            when=now - timedelta(hours=6),
            label="Codex local - other@example.com",
            total_tokens=999_999,
            kind="request",
        )
        event_id = client_usage_export.codex_event_id(codex_event())
        verdicts = {
            event_id: {
                "label": archived_label,
                "tier": "cockpit_usage_row",
                "at": (now - timedelta(hours=1))
                .replace(tzinfo=client_usage_export.LOCAL_TZ)
                .isoformat(timespec="seconds"),
            }
        }
        aligned = ({}, {}, {}, {}, {}, {}, {})

        def build(passed_verdicts: dict[str, dict[str, str]] | None):
            with (
                patch.object(client_usage_export, "cockpit_codex_quota_by_label", return_value={}),
                patch.object(client_usage_export, "cockpit_codex_speed_by_label", return_value={}),
                patch.object(client_usage_export, "scan_cockpit_codex_accounts", return_value={}),
                patch.object(client_usage_export, "scan_cockpit_codex_quota_windows", return_value=aligned),
                patch.object(client_usage_export, "all_cockpit_codex_account_labels", return_value=[]),
                patch.object(client_usage_export, "codex_speed_history", return_value=[]),
                patch.object(client_usage_export, "scan_cockpit_codex_switch_markers", return_value=[]),
                patch.object(
                    client_usage_export,
                    "scan_cockpit_codex_account_markers",
                    return_value=[stale_marker],
                ),
                patch.object(
                    client_usage_export,
                    "scan_all_codex_events",
                    side_effect=lambda *_args, **_kwargs: [codex_event()],
                ),
            ):
                return client_usage_export.build_codex_window_stats(
                    Path("."),
                    Path("."),
                    now,
                    {},
                    "",
                    passed_verdicts,
                )

        today, _session_accounts, today_unresolved = (
            client_usage_export.resolve_api_service_event_accounts(
                {client_usage_export.API_SERVICE_AGGREGATE_LABEL: [codex_event()]},
                [stale_marker],
                None,
                None,
                verdicts,
            )
        )
        with_archive = build(verdicts)
        without_archive = build(None)

        # Today's totals and every window that contains today must agree, or the
        # user sees a day that is larger than the 7 day window holding it.
        self.assertEqual(list(today), [archived_label])
        self.assertEqual(today_unresolved, 0)
        self.assertEqual(with_archive[archived_label]["window_5h"]["tokens"], 1_000)
        self.assertEqual(with_archive[archived_label]["window_7d"]["tokens"], 1_000)
        self.assertEqual(
            with_archive[archived_label]["window_rolling_7d"]["tokens"],
            1_000,
        )
        self.assertNotIn(archived_label, without_archive)
        self.assertEqual(
            without_archive[client_usage_export.API_SERVICE_AGGREGATE_LABEL][
                "window_7d"
            ]["tokens"],
            1_000,
        )
        # Windows read the archive but never write to it.
        self.assertEqual(list(verdicts), [event_id])
        self.assertEqual(verdicts[event_id]["label"], archived_label)


class SingleInstanceTests(unittest.TestCase):
    def test_second_monitor_launch_exits_without_creating_window(self) -> None:
        with (
            patch.object(monitor, "acquire_single_instance_mutex", return_value=None),
            patch.object(monitor, "release_single_instance_mutex") as release,
            patch.object(monitor, "FloatingMonitorApp") as app_factory,
        ):
            started = monitor.run_monitor_app()

        self.assertFalse(started)
        app_factory.assert_not_called()
        release.assert_not_called()

    def test_mutex_is_released_after_monitor_closes(self) -> None:
        with (
            patch.object(monitor, "acquire_single_instance_mutex", return_value=123),
            patch.object(monitor, "release_single_instance_mutex") as release,
            patch.object(monitor, "FloatingMonitorApp") as app_factory,
        ):
            started = monitor.run_monitor_app()

        self.assertTrue(started)
        app_factory.return_value.run.assert_called_once_with()
        release.assert_called_once_with(123)


class ListScrollbarTests(unittest.TestCase):
    def test_added_height_is_shared_with_account_usage_rows(self) -> None:
        default_height = monitor.FloatingMonitorApp.HEIGHT
        enlarged_height = default_height + 260

        active_capacity = monitor.balanced_active_row_capacity(
            enlarged_height,
            default_height,
        )
        active_growth = (active_capacity - 3) * 26
        usage_growth = (enlarged_height - default_height) - active_growth

        self.assertEqual(active_capacity, 6)
        self.assertGreater(usage_growth, active_growth)
        self.assertGreaterEqual(usage_growth, 2 * 64)

    def test_small_height_increase_expands_both_sections(self) -> None:
        default_height = monitor.FloatingMonitorApp.HEIGHT
        enlarged_height = default_height + 100

        active_capacity = monitor.balanced_active_row_capacity(
            enlarged_height,
            default_height,
        )
        usage_growth = 100 - (active_capacity - 3) * 26

        self.assertEqual(active_capacity, 4)
        self.assertGreaterEqual(usage_growth, 64)

    def test_active_scrollbar_is_selected_independently(self) -> None:
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        app._main_tab = "accounts"
        app._list_scrollbar_tracks = {
            "active": (0, 10, 12, 80),
            "accounts": (0, 100, 12, 200),
            "stats": None,
        }

        self.assertEqual(app._scrollbar_tab_at(5, 40), "active")
        self.assertEqual(app._scrollbar_tab_at(5, 140), "accounts")
        self.assertIsNone(app._scrollbar_tab_at(30, 40))

    def test_thumb_position_maps_to_stats_scroll_range(self) -> None:
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        app._list_scrollbar_tracks = {"accounts": None, "stats": (0, 10, 12, 110)}
        app._list_scrollbar_thumbs = {"accounts": None, "stats": (0, 10, 12, 30)}
        app._scroll_limits = {"accounts": 0, "stats": 480}
        app._scroll_offsets = {"accounts": 0, "stats": 0}

        app._set_list_scroll_from_thumb("stats", -100)
        self.assertEqual(app._scroll_offsets["stats"], 0)
        app._set_list_scroll_from_thumb("stats", 50)
        self.assertEqual(app._scroll_offsets["stats"], 240)
        app._set_list_scroll_from_thumb("stats", 999)
        self.assertEqual(app._scroll_offsets["stats"], 480)

    def test_thumb_position_maps_to_account_scroll_range(self) -> None:
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        app._list_scrollbar_tracks = {"accounts": (0, 20, 12, 100), "stats": None}
        app._list_scrollbar_thumbs = {"accounts": (0, 20, 12, 40), "stats": None}
        app._scroll_limits = {"accounts": 390, "stats": 0}
        app._scroll_offsets = {"accounts": 0, "stats": 0}

        app._set_list_scroll_from_thumb("accounts", 999)

        self.assertEqual(app._scroll_offsets["accounts"], 390)

    def test_thumb_position_maps_to_active_scroll_range(self) -> None:
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        app._list_scrollbar_tracks = {"active": (0, 20, 12, 100)}
        app._list_scrollbar_thumbs = {"active": (0, 20, 12, 40)}
        app._scroll_limits = {"active": 156}
        app._scroll_offsets = {"active": 0}

        app._set_list_scroll_from_thumb("active", 999)

        self.assertEqual(app._scroll_offsets["active"], 156)

    def test_thumb_without_travel_stays_at_top(self) -> None:
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        app._list_scrollbar_tracks = {"accounts": None, "stats": (0, 10, 12, 30)}
        app._list_scrollbar_thumbs = {"accounts": None, "stats": (0, 10, 12, 30)}
        app._scroll_limits = {"stats": 48}
        app._scroll_offsets = {"stats": 48}

        app._set_list_scroll_from_thumb("stats", 20)

        self.assertEqual(app._scroll_offsets["stats"], 0)


class PageRenderingSeamTests(unittest.TestCase):
    def test_current_tab_uses_its_page_renderer(self) -> None:
        for tab, expected, skipped in (
            ("accounts", "_draw_accounts_page", "_draw_usage_stats_page"),
            ("stats", "_draw_usage_stats_page", "_draw_accounts_page"),
        ):
            with self.subTest(tab=tab):
                app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
                app._main_tab = tab
                app._draw_accounts_page = MagicMock()
                app._draw_usage_stats_page = MagicMock()

                app._draw_current_page(1, 2, 3, 4)

                getattr(app, expected).assert_called_once_with(1, 2, 3, 4)
                getattr(app, skipped).assert_not_called()


class UsageExportInterfaceTests(unittest.TestCase):
    def test_main_delegates_standard_export_to_report_interface(self) -> None:
        report = {"today": {"tokens": 0}}
        with (
            patch(
                "sys.argv",
                [
                    "client_usage_export.py",
                    "--output",
                    "report.json",
                    "--date",
                    "2026-08-30",
                ],
            ),
            patch.object(
                client_usage_export,
                "export_usage_report",
                return_value=report,
            ) as exporter,
            patch("builtins.print"),
        ):
            exit_code = client_usage_export.main()

        self.assertEqual(exit_code, 0)
        args, kwargs = exporter.call_args
        self.assertEqual(args[0], Path("report.json"))
        self.assertEqual(args[3], date(2026, 8, 30))
        self.assertFalse(kwargs["include_30d"])
        self.assertFalse(kwargs["queue_offline_history"])
        self.assertFalse(kwargs["backfill_history_details"])


if __name__ == "__main__":
    unittest.main()
