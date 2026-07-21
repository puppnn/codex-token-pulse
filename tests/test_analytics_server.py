from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import analytics_server as analytics


def provider(name: str, tokens: int, model: str = "gpt-test") -> dict:
    return {
        "name": name,
        "requests": 2,
        "tokens": tokens,
        "input_tokens": tokens // 4,
        "cached_input_tokens": tokens // 2,
        "cache_creation_input_tokens": 0,
        "output_tokens": tokens // 4,
        "cost": tokens / 1_000_000,
        "models": {model: tokens},
    }


def day(day_key: str, tokens: int, providers: list[dict], source: str = "local") -> dict:
    return {
        "date": day_key,
        "source": source,
        "requests": sum(item["requests"] for item in providers),
        "tokens": tokens,
        "cost": sum(item["cost"] for item in providers),
        "input_tokens": sum(item["input_tokens"] for item in providers),
        "cached_input_tokens": sum(item["cached_input_tokens"] for item in providers),
        "cache_creation_input_tokens": 0,
        "output_tokens": sum(item["output_tokens"] for item in providers),
        "models": {"gpt-test": sum(item["tokens"] for item in providers)},
        "providers": providers,
    }


class AnalyticsServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_cache_key = analytics._cache_key
        self.original_cache_value = analytics._cache_value

    def tearDown(self) -> None:
        analytics._cache_key = self.original_cache_key
        analytics._cache_value = self.original_cache_value

    def test_primary_explicit_zero_wins_over_ccswitch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history_path = Path(tmp) / "usage_history.json"
            history_path.write_text(
                json.dumps(
                    {
                        "schema": 2,
                        "days": {
                            "2026-06-01": {
                                "date": "2026-06-01",
                                "source": "sub2api",
                                "requests": 0,
                                "tokens": 0,
                                "cost": 0,
                                "providers": [],
                                "models": {},
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            cc_overlap = analytics.normalize_day(
                "2026-06-01",
                day("2026-06-01", 900, [provider("ccSwitch Codex session", 900)], "ccswitch_backfill"),
                "ccswitch_backfill",
            )
            cc_gap = analytics.normalize_day(
                "2026-05-31",
                day("2026-05-31", 500, [provider("ccSwitch Codex session", 500)], "ccswitch_backfill"),
                "ccswitch_backfill",
            )
            with (
                patch.object(analytics, "USAGE_HISTORY_JSON", history_path),
                patch.object(analytics, "CCSWITCH_DB", Path(tmp) / "cc-switch.db"),
                patch.object(
                    analytics,
                    "load_ccswitch_days",
                    return_value=(
                        {"2026-06-01": cc_overlap, "2026-05-31": cc_gap},
                        {"available": True, "days": 2},
                    ),
                ),
            ):
                merged = analytics.load_merged_history(force=True)

        self.assertEqual(merged["days"]["2026-06-01"]["tokens"], 0)
        self.assertEqual(merged["days"]["2026-06-01"]["origin"], "primary")
        self.assertEqual(merged["days"]["2026-05-31"]["tokens"], 500)
        self.assertEqual(merged["days"]["2026-05-31"]["origin"], "ccswitch_backfill")

    def test_residual_provider_preserves_authoritative_total(self) -> None:
        row = analytics.normalize_day(
            "2026-06-01",
            day("2026-06-01", 1_000, [provider("known@example.com", 600)]),
        )
        rows = analytics.providers_with_residual(row)
        self.assertEqual(sum(item["tokens"] for item in rows), 1_000)
        self.assertEqual(rows[-1]["tokens"], 400)
        self.assertEqual(rows[-1]["name"], "Unassigned local")

    def test_account_model_filter_marks_proportional_fields_estimated(self) -> None:
        first = provider("one@example.com", 1_000, "gpt-a")
        first["models"] = {"gpt-a": 700, "gpt-b": 300}
        normalized = analytics.normalize_day("2026-06-01", day("2026-06-01", 1_000, [first]))
        history = {
            "days": {"2026-06-01": normalized},
            "ccswitch": {"available": False},
            "history_path": "test.json",
            "history_exists": True,
        }
        with patch.object(analytics, "load_merged_history", return_value=history):
            dashboard = analytics.build_dashboard(
                {
                    "start": ["2026-06-01"],
                    "end": ["2026-06-01"],
                    "account": ["one@example.com"],
                    "model": ["gpt-a"],
                }
            )
        self.assertEqual(dashboard["summary"]["tokens"], 700)
        self.assertTrue(dashboard["summary"]["estimated"])
        self.assertIn("cost", dashboard["daily"][0]["estimated_fields"])

    def test_csv_export_has_utf8_bom_and_headers(self) -> None:
        payload = analytics.csv_bytes(
            [{"date": "2026-06-01", "tokens": 123}],
            [("date", "日期"), ("tokens", "Token")],
        )
        self.assertTrue(payload.startswith(b"\xef\xbb\xbf"))
        decoded = payload.decode("utf-8-sig")
        self.assertIn("日期,Token", decoded)
        self.assertIn("2026-06-01,123", decoded)

    def test_blank_day_is_labeled_missing(self) -> None:
        row = analytics.blank_day("2026-06-02")
        self.assertEqual(row["origin"], "missing")
        self.assertEqual(row["source"], "MISSING")


if __name__ == "__main__":
    unittest.main()
