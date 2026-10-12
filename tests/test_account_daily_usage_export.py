"""Account daily buckets are retained without mislabeling local differences."""

import json
import tempfile
import unittest
from pathlib import Path

from export_cycle_data import account_daily_usage_snapshot


class AccountDailyUsageExportTests(unittest.TestCase):
    def test_pairs_account_bucket_with_local_totals_but_does_not_call_difference_external(self):
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "usage.log"
            payload = {
                "total": {
                    "requests": 2,
                    "input_tokens": 900,
                    "output_tokens": 100,
                    "known_cost": "0.25",
                }
            }
            log_path.write_text(
                "2026-09-23 23:59:00 UTC primary=1% secondary=25% balance=0 "
                f"usage_v2={json.dumps(payload)}\n",
                encoding="utf-8",
            )
            evidence = [{
                "timestamp": "2026-09-24 13:40:17",
                "timestamp_epoch": 1790257217,
                "account_lifetime_tokens": 21_598_493_260,
                "account_daily_buckets": [
                    {"startDate": "2026-09-23", "tokens": 719_765_470}
                ],
            }]
            rows = [
                {
                    "timestamp_utc": "2026-09-23T10:00:00+00:00",
                    "model": "gpt-6-luna", "input_tokens": 800,
                    "output_tokens": 90, "cost_usd": "0.2",
                },
                {
                    "timestamp_utc": "2026-09-23T11:00:00+00:00",
                    "model": "other-provider", "input_tokens": 5000,
                    "output_tokens": 500, "cost_usd": "0",
                },
            ]

            result = account_daily_usage_snapshot(
                evidence, rows, frozenset({"other-provider"}), log_path
            )

        self.assertEqual(result["daily_buckets"][0]["account_reported_tokens"], 719_765_470)
        self.assertEqual(result["daily_buckets"][0]["retained_headroom_tokens"], 890)
        self.assertEqual(result["daily_buckets"][0]["monitor_checkpoint_end_date_tokens"], 1_000)
        self.assertEqual(result["daily_buckets"][0]["account_minus_monitor_checkpoint_tokens"], 719_764_470)
        self.assertFalse(result["daily_buckets"][0]["differences_are_external_usage_estimates"])
        self.assertIn("can span midnight", result["interpretation"])


if __name__ == "__main__":
    unittest.main()
