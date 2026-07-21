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
        self.day = monitor.today_key()

    def tearDown(self) -> None:
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

    def test_today_30d_and_cycle_sort_by_token_usage(self) -> None:
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
        self.assertEqual(ordered_cycle[0]["name"], "old-heavy")

    def test_window_only_accounts_are_hidden_only_from_today(self) -> None:
        row = {
            "name": "Codex local - previous@example.com",
            "window_only": True,
        }

        self.assertFalse(monitor.account_row_available_for_range(row, "today"))
        self.assertTrue(monitor.account_row_available_for_range(row, "5h"))
        self.assertTrue(monitor.account_row_available_for_range(row, "7d"))
        self.assertTrue(monitor.account_row_available_for_range(row, "cycle"))

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
        pool = {"hails24.uranium@icloud.com", "tissue_wisp.24+g5@icloud.com"}

        self.assertTrue(
            monitor.account_row_matches_pool(
                {"name": "Codex local - hails24.uranium@icloud.com"},
                pool,
            )
        )
        self.assertFalse(
            monitor.account_row_matches_pool(
                {"name": "Codex local - rollers_tubers4s@icloud.com"},
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
                    "provider": "Codex local - hails24.uranium@icloud.com",
                    "model": "gpt-5.5",
                    "latest_at": now,
                }
            ],
            "providers": [
                {
                    "name": "Codex local - hails24.uranium@icloud.com",
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
        self.assertIn("hails24.uranium@icloud.com", active[0]["name"])
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
                side_effect=lambda attributed, _markers: (attributed, 0),
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
                    event_key TEXT
                )
                """
            )
            at = datetime(2026, 7, 12, 8, 0, 0)
            connection.executemany(
                "INSERT INTO request_logs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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


class WindowSemanticsTests(unittest.TestCase):
    def test_unlimited_5h_window_is_not_counted_as_quota_pressure(self) -> None:
        app = object.__new__(monitor.FloatingMonitorApp)
        app.state = monitor.MonitorState(
            top_accounts=[
                {
                    "name": "Codex local - plus@example.com",
                    "tokens": 500,
                    "requests": 2,
                    "active_now": True,
                    "window_5h": {
                        "tokens": 500,
                        "requests": 2,
                        "cost": 0.5,
                        "quota_available": False,
                        "quota_unlimited": True,
                    },
                }
            ]
        )

        rows = app._budget_rows()

        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["has_quota"])
        self.assertTrue(rows[0]["windows"][0]["quota_unlimited"])
        self.assertFalse(rows[0]["windows"][0]["pressure_active"])

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

            self.assertEqual(urlopen.call_count, 1)
            self.assertEqual(first, second)
            self.assertNotIn("secret-access-token", cache_path.read_text(encoding="utf-8"))

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

    def test_official_quota_failure_retains_last_percent_as_stale(self) -> None:
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
            with (
                patch.object(client_usage_export, "COCKPIT_OFFICIAL_QUOTA_CACHE_PATH", cache_path),
                patch.object(client_usage_export, "fetch_cockpit_official_quota", return_value=None) as fetch,
            ):
                failed = client_usage_export.cockpit_official_quota_by_account(
                    root,
                    accounts,
                    checked_at,
                )
                cached = client_usage_export.cockpit_official_quota_by_account(
                    root,
                    accounts,
                    checked_at + timedelta(minutes=1),
                )

            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(failed[account_id]["window_7d"]["remaining_percent"], 26.0)
            self.assertTrue(failed[account_id]["window_7d"]["quota_stale"])
            self.assertEqual(cached, failed)
            saved = json.loads(cache_path.read_text(encoding="utf-8"))["accounts"][account_id]
            self.assertTrue(saved["refresh_failed"])
            self.assertEqual(saved["quota"]["window_7d"]["remaining_percent"], 26.0)

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
        self.assertEqual(read_tail.call_count, 1)

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


class LiveUsageOverlayTests(unittest.TestCase):
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

        self.assertTrue(
            app._record_live_usage_events(
                self.events_with_total(first_total),
                animate=False,
            )
        )
        self.assertFalse(
            app._record_live_usage_events(
                self.events_with_total(final_batch),
                animate=False,
            )
        )

        self.assertEqual(app.state.today_tokens, 100 + first_total)
        self.assertTrue(app._live_usage_verification_pending)
        self.assertEqual(app._live_usage_verification_pending_tokens, final_batch)

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
            "total_tokens": 50,
            "input_tokens": 40,
            "cached_tokens": 20,
            "output_tokens": 10,
        }

        with patch.object(monitor, "_load_live_cockpit_markers", return_value=[marker]):
            app._record_live_usage_events([event])

        provider = app.state.client_usage["providers"][0]
        account = app.state.top_accounts[0]
        self.assertEqual((account["tokens"], account["requests"]), (1_050, 11))
        self.assertEqual((provider["tokens"], provider["requests"]), (1_050, 11))
        self.assertEqual(provider["input_tokens"], 420)
        self.assertEqual(provider["cached_input_tokens"], 520)
        self.assertEqual(provider["output_tokens"], 110)
        self.assertEqual(app.state.latest_account_name, "Codex local - routed@example.com")

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
            patch.object(monitor, "estimate_live_usage_cost", return_value=0.25),
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

    def test_historical_catchup_accepts_old_event_and_deduplicates_it(self) -> None:
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        app.state = self.state()
        app._live_usage_overlay = None
        app._live_usage_seen_ids = {}
        event = self.event()
        event["when"] = datetime.now(timezone.utc) - timedelta(hours=2)
        event["event_id"] = "catchup-event-1"

        self.assertTrue(
            app._record_live_usage_events(
                [event],
                allow_historical=True,
                animate=False,
            )
        )
        self.assertEqual(app.state.today_tokens, 150)
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
            summary = restarted._usage_range_summary("24h")
            hour = event["when"].astimezone(monitor.CN_TZ).hour
            bucket = next(row for row in summary["series"] if row["hour"] == hour)
            self.assertEqual(bucket["tokens"], 150)
            self.assertEqual(bucket["requests"], 3)

    def test_legacy_live_checkpoint_is_discarded_after_fork_filter_upgrade(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = Path(directory) / "live-checkpoint.json"
            checkpoint.write_text(
                json.dumps(
                    {
                        "schema": monitor.LIVE_USAGE_CHECKPOINT_SCHEMA - 1,
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

    def test_second_precision_catchup_boundary_advances_one_second(self) -> None:
        app = monitor.FloatingMonitorApp.__new__(monitor.FloatingMonitorApp)
        app.state = self.state()
        app.state.latest_request = {
            "created_at": (datetime.now(monitor.CN_TZ) - timedelta(seconds=10))
            .replace(microsecond=0)
            .isoformat(timespec="seconds")
        }
        app.state.client_usage["updated_at"] = (
            datetime.now(monitor.CN_TZ) - timedelta(minutes=1)
        ).isoformat(timespec="seconds")
        app._live_usage_overlay = None

        since = app._live_usage_catchup_since()

        expected = monitor._parse_time(app.state.latest_request["created_at"]) + timedelta(seconds=1)
        self.assertEqual(since, expected)

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
        app._draw = lambda: None
        captured: list[int] = []

        def update_history(state: monitor.MonitorState) -> dict:
            captured.append(state.today_tokens)
            return {}

        with patch.object(monitor, "update_usage_history", side_effect=update_history):
            app._apply_state(self.state())

        self.assertEqual(captured, [100])
        self.assertEqual(app.state.today_tokens, 150)

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

    def test_busy_auto_refresh_forces_stale_full_usage_snapshot(self) -> None:
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

        app.refresh_async.assert_called_once_with()
        self.assertGreater(app._last_forced_full_refresh_at, 0.0)
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
                last_day.isoformat(): self.history_row(last_day, 100),
            },
            "offline_sync": {
                "state": "complete",
                "last_successful_at": "2026-07-13T08:30:00+08:00",
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


if __name__ == "__main__":
    unittest.main()
