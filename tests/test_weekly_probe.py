"""Regression tests for cheap weekly polling without dropping interval usage."""

import os
import tempfile
from decimal import Decimal
import unittest
from pathlib import Path
from unittest.mock import patch

import monitor


def snapshot(at, weekly, primary=40, reset_at=20000):
    return monitor.Snapshot(
        captured_at=at,
        primary=monitor.LimitWindow(primary, 30000, 300),
        secondary=monitor.LimitWindow(weekly, reset_at, 10080),
        credit_balance=None,
        reset_credit_available_count=0,
        reset_credit_ids=(),
    )


class WeeklyProbeTests(unittest.TestCase):
    def test_decision_ignores_primary_and_moving_reset_time_but_records_weekly_change(self):
        previous = snapshot(1000, 23)
        accounting = {"last_sample_at": 1000}
        self.assertFalse(monitor.weekly_checkpoint_due(previous, snapshot(1060, 23, 55), accounting, 600))
        self.assertTrue(monitor.weekly_checkpoint_due(previous, snapshot(1060, 24), accounting, 600))
        self.assertTrue(monitor.weekly_checkpoint_due(previous, snapshot(1060, 0), accounting, 600))
        self.assertFalse(monitor.weekly_checkpoint_due(previous, snapshot(1060, 23, reset_at=21000), accounting, 600))
        self.assertFalse(monitor.weekly_checkpoint_due(previous, snapshot(1599, 23), accounting, 600))
        self.assertTrue(monitor.weekly_checkpoint_due(previous, snapshot(1600, 23), accounting, 600))
        self.assertTrue(monitor.weekly_checkpoint_due(None, snapshot(1060, 23), accounting, 600))

    def test_probe_does_not_read_account_usage_unless_checkpoint_due(self):
        previous = snapshot(1000, 23)
        with patch("monitor.CodexAppServerClient") as client:
            client.return_value.__enter__.return_value.read_rate_limits.return_value = {
                "rateLimits": {
                    "secondary": {"usedPercent": 23, "resetsAt": 20000, "windowDurationMins": 10080},
                    "primary": {"usedPercent": 55, "resetsAt": 30000, "windowDurationMins": 300},
                }
            }
            with patch("monitor.snapshot_from_payload", return_value=snapshot(1060, 23, 55)):
                _, _, due = monitor.query_weekly_probe(previous, {"last_sample_at": 1000})
            self.assertFalse(due)
            client.return_value.__enter__.return_value.read_account_usage.assert_not_called()
            with patch("monitor.snapshot_from_payload", return_value=snapshot(1120, 24)):
                _, _, due = monitor.query_weekly_probe(previous, {"last_sample_at": 1000})
            self.assertTrue(due)
            client.return_value.__enter__.return_value.read_account_usage.assert_called_once()

    def test_unchanged_probe_does_not_scan_or_write_state(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            log_path = Path(directory) / "usage.log"
            old = snapshot(1000, 23)
            accounting = monitor.empty_accounting_state()
            accounting["last_sample_at"] = old.captured_at
            monitor.save_state(state_path, old, accounting)
            before = state_path.read_bytes()
            with patch("monitor.query_weekly_probe", return_value=(snapshot(1060, 23), None, False)), \
                 patch("monitor.collect_headroom_usage") as collect, \
                 patch("monitor.load_model_pricing") as pricing:
                self.assertEqual(monitor.run_once(state_path, log_path, weekly_probe=True), 0)
            self.assertEqual(state_path.read_bytes(), before)
            self.assertFalse(log_path.exists())
            collect.assert_not_called()
            pricing.assert_not_called()

    def test_bounded_checkpoint_collects_calls_while_weekly_percent_is_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            log_path = Path(directory) / "usage.log"
            old = snapshot(1000, 23)
            new = snapshot(1600, 24)
            pricing = monitor.PricingTable(
                revision="test", currency="USD", unit="per_1m_tokens",
                models={"test-model": monitor.ModelPricing(
                    model_id="test-model", input_rate=Decimal("1"),
                    output_rate=Decimal("0"), cached_input_rate=Decimal("0"),
                    cache_write_rate=Decimal("0"),
                )}, aliases={},
            )
            initial = monitor.empty_accounting_state()
            initial, _ = monitor.update_accounting_state(
                initial, old, old, monitor.aggregate_period((), pricing, 1000, 1000),
                pricing, {}, weekly_only=True,
            )
            monitor.save_state(state_path, old, initial)
            row = monitor.HeadroomUsageRecord(1300, "test-model", 1_000_000, 0, 0, 0, "request-1")
            with patch("monitor.query_weekly_probe", return_value=(new, None, True)), \
                 patch("monitor.load_model_pricing", return_value=pricing), \
                 patch("monitor.collect_headroom_usage", return_value=([row], {}, {"coverage_gap": False})) as collect:
                self.assertEqual(monitor.run_once(state_path, log_path, weekly_probe=True), 0)
            self.assertEqual(collect.call_args.args[1:3], (1000.0, 1600.0))
            _, accounting = monitor.load_state_bundle(state_path)
            self.assertEqual(accounting["windows"]["secondary"]["known_cost"], "1")
            self.assertIsNone(accounting["windows"]["primary"])
            self.assertTrue(accounting["windows"]["secondary"]["has_external_usage_unquantified"])
            logged = log_path.read_text()
            self.assertIn("weekly_100pct_usd=-", logged)
            self.assertIn("weekly_local_100pct_usd=-", logged)
            payload = __import__("json").loads(logged.split(" usage_v2=", 1)[1])
            weekly = payload["windows"]["secondary"]
            self.assertEqual(weekly["external_usage_status"], "unquantified")
            self.assertIsNone(weekly["locally_attributed_percentage_points"])
            self.assertIsNone(weekly["local_token_backed_100_usd"])
            self.assertIsNone(weekly["full_observed_estimate"]["headroom_only_100_usd"])

    def test_detected_external_usage_withholds_all_projections(self):
        old = snapshot(1000, 23)
        current = snapshot(1600, 24)
        pricing = monitor.PricingTable(
            revision="test", currency="USD", unit="per_1m_tokens",
            models={"test-model": monitor.ModelPricing(
                model_id="test-model", input_rate=Decimal("1"),
                output_rate=Decimal("0"), cached_input_rate=Decimal("0"),
                cache_write_rate=Decimal("0"),
            )}, aliases={},
        )
        row = monitor.HeadroomUsageRecord(1300, "test-model", 1_000_000, 0, 0, 0, "request-1")
        period = monitor.aggregate_period([row], pricing, 1000, 1600)
        accounting = monitor.empty_accounting_state()
        accounting, _ = monitor.update_accounting_state(
            accounting, old, old, monitor.aggregate_period((), pricing, 1000, 1000),
            pricing, {}, weekly_only=True,
        )
        accounting, estimates = monitor.update_accounting_state(
            accounting, old, current, period, pricing, {}, weekly_only=True,
        )
        usage_v2 = {"windows": {"secondary": estimates["secondary"]}}

        monitor._mark_weekly_external_usage_unquantified(
            accounting, estimates, usage_v2, current.secondary,
            [{"reason": "ok", "external_tokens_estimate": 900}],
        )

        weekly = usage_v2["windows"]["secondary"]
        self.assertEqual(weekly["status"], "external_usage_unquantified")
        self.assertIsNone(weekly["account_total_100_usd"])
        self.assertEqual(weekly["counted_percentage_points"], 1)
        self.assertEqual(weekly["excluded_no_token_percentage_points"], 0)
        self.assertIsNone(weekly["locally_attributed_percentage_points"])
        self.assertIsNone(weekly["local_token_backed_100_usd"])
        self.assertIsNone(weekly["full_observed_estimate"]["headroom_only_100_usd"])
        self.assertEqual(weekly["model_shares_at_100_usd"], {})
        self.assertEqual(weekly["rolling_estimates"], {})
        self.assertEqual(weekly["headroom_only_rolling_estimates"], {})
        self.assertEqual(weekly["headroom_model_shares_at_100_usd"], {})

    def test_weekly_only_accounting_does_not_invent_missing_calls(self):
        pricing = monitor.unavailable_pricing("test", Path("/missing"))
        old = snapshot(1000, 23)
        current = snapshot(1060, 29)
        baseline, _ = monitor.update_accounting_state(
            monitor.empty_accounting_state(), old, old,
            monitor.aggregate_period((), pricing, 1000, 1000), pricing, {}, weekly_only=True,
        )
        updated, estimates = monitor.update_accounting_state(
            baseline, old, current,
            monitor.aggregate_period((), pricing, 1000, 1060), pricing, {}, weekly_only=True,
        )
        weekly = updated["windows"]["secondary"]
        self.assertEqual(weekly["anchor"]["used_percent"], 23)
        self.assertEqual(weekly["unobserved_percentage_ranges"][0]["to_percent"], 29)
        self.assertEqual(weekly["usage_total"]["requests"], 0)
        self.assertEqual(weekly["known_cost"], "0")
        self.assertIsNone(estimates["secondary"]["full_window_usd"])
        self.assertEqual(estimates["secondary"]["status"], "unobserved_percentage_change")

    def test_weekly_only_accounting_preserves_existing_primary_history(self):
        old = snapshot(1000, 20)
        current = snapshot(1600, 20, primary=99)
        pricing = monitor.unavailable_pricing("test", Path("/missing"))
        period = monitor.aggregate_period((), pricing, 1000, 1600)
        existing = monitor.empty_accounting_state()
        existing["windows"]["primary"] = {"anchor": {"used_percent": 5}, "cycle_history": [{"old": True}]}
        existing["last_sample_at"] = 1000
        new, estimates = monitor.update_accounting_state(
            existing, old, current, period, pricing, {}, weekly_only=True
        )
        self.assertEqual(new["windows"]["primary"], existing["windows"]["primary"])
        self.assertEqual(estimates["primary"]["status"], "missing_window")
        self.assertEqual(new["windows"]["secondary"]["anchor"]["used_percent"], 20)


if __name__ == "__main__":
    unittest.main()
