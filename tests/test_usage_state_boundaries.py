import json
import sqlite3
import tempfile
import threading
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import client_usage_export
import monitor


class CompactNumberTests(unittest.TestCase):
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

    def test_today_and_30d_sort_by_token_usage(self) -> None:
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

        self.assertEqual(ordered_today[0]["name"], "old-heavy")
        self.assertEqual(ordered_30d[0]["name"], "old-heavy")


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

    def test_api_service_latest_request_reuses_confirmed_session_account(self) -> None:
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

        self.assertEqual(latest["provider"], "Codex local - confirmed@example.com")

    def test_api_service_events_are_moved_to_concrete_accounts_without_duplication(self) -> None:
        first = client_usage_export.UsageEvent(
            when=datetime(2026, 7, 12, 8, 0, 0),
            model="gpt-test",
            input_tokens=900,
            cached_tokens=0,
            output_tokens=100,
            session_id="session-1",
        )
        second = client_usage_export.UsageEvent(
            when=datetime(2026, 7, 12, 8, 1, 0),
            model="gpt-test",
            input_tokens=1_800,
            cached_tokens=0,
            output_tokens=200,
            session_id="session-1",
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
        previous = self.snapshot(self.day, 0)
        current = self.snapshot(self.day, 0)
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
        self.assertTrue(hourly["failure"])
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


class ActiveSessionLifecycleTests(unittest.TestCase):
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
        app._loading = False
        app.client = FakeClient()
        app._draw = lambda: None
        app._pulse_tick = lambda: None

        with patch.object(monitor.threading, "Thread", FakeThread):
            started = app.refresh_async(force=True)

        self.assertTrue(started)
        self.assertEqual(app.client.clear_calls, 1)
        app._refresh_lock.release()


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

    def test_error_marks_only_the_adjacent_observed_zero_hour(self) -> None:
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
        )

        self.assertTrue(hourly[4]["failure"])
        self.assertEqual(hourly[4]["failure_count"], 1)
        self.assertFalse(any(row.get("failure") for row in hourly[5:]))

    def test_error_marker_waits_for_the_hour_and_clears_when_activity_resumes(self) -> None:
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
        )
        self.assertFalse(any(row.get("failure") for row in hourly))

        hourly[4]["tokens"] = 50
        client_usage_export.mark_codex_failure_hours(
            hourly,
            failures,
            date(2026, 7, 12),
            datetime(2026, 7, 12, 5, 30),
        )
        self.assertFalse(any(row.get("failure") for row in hourly))

    def test_repeated_desktop_network_failures_mark_adjacent_idle_hour(self) -> None:
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
        )
        self.assertTrue(hourly[4]["failure"])
        self.assertEqual(hourly[4]["failure_kind"], "desktop_network")

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
