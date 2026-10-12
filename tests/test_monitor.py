import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from monitor import (
    HeadroomUsageRecord,
    LimitWindow,
    ModelPricing,
    NtfyNotifier,
    PricingTable,
    Snapshot,
    aggregate_period,
    backfill_accounting_usage_from_log,
    collect_headroom_usage,
    detect_events,
    headroom_excluded_models,
    headroom_log_paths,
    load_state_bundle,
    log_usage,
    load_model_pricing,
    parse_headroom_perf_line,
    price_usage_record,
    prime_headroom_cursor,
    save_state,
    snapshot_from_payload,
    snapshot_to_dict,
    update_accounting_state,
    update_external_evidence,
    update_window_calibration,
)


def snap(*, p_used=80, p_reset=1000, s_used=60, s_reset=2000, balance=None, count=0, ids=()):
    return Snapshot(
        captured_at=900,
        primary=LimitWindow(p_used, p_reset, 300),
        secondary=LimitWindow(s_used, s_reset, 10080),
        credit_balance=balance,
        reset_credit_available_count=count,
        reset_credit_ids=tuple(ids),
    )


class MonitorTests(unittest.TestCase):
    def setUp(self):
        os.environ["RESET_MIN_PERCENT_DROP"] = "1"

    def test_parse_official_camel_case_payload(self):
        payload = {
            "rateLimits": {
                "primary": {"usedPercent": 42, "resetsAt": 123, "windowDurationMins": 300},
                "secondary": {"usedPercent": 5, "resetsAt": 456, "windowDurationMins": 10080},
                "credits": {"balance": "17.5"},
            },
            "rateLimitResetCredits": {
                "availableCount": 2,
                "credits": [{"id": "b", "status": "available"}, {"id": "a", "status": "available"}],
            },
        }
        result = snapshot_from_payload(payload, captured_at=1)
        self.assertEqual(result.primary.used_percent, 42)
        self.assertEqual(result.secondary.resets_at, 456)
        self.assertEqual(result.credit_balance, "17.5")
        self.assertEqual(result.reset_credit_ids, ("a", "b"))

    def test_detect_primary_reset(self):
        events = detect_events(snap(), snap(p_used=3, p_reset=2800))
        self.assertEqual([event.kind for event in events], ["primary_window_reset"])

    def test_no_reset_when_usage_only_increases(self):
        events = detect_events(snap(p_used=20), snap(p_used=40, p_reset=1000))
        self.assertEqual(events, [])

    def test_detect_new_reset_credit(self):
        events = detect_events(snap(count=0), snap(count=1, ids=("credit-1",)))
        self.assertEqual([event.kind for event in events], ["reset_credit_granted"])

    def test_detect_balance_increase(self):
        events = detect_events(snap(balance="1.0"), snap(balance="5.0"))
        self.assertEqual([event.kind for event in events], ["credit_balance_increased"])


class NtfyNotifierTests(unittest.TestCase):
    def setUp(self):
        self._env = os.environ.copy()

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)

    def test_missing_topic_raises(self):
        os.environ["NTFY_TOPIC"] = ""
        with self.assertRaisesRegex(Exception, "NTFY_TOPIC"):
            NtfyNotifier().send("title", "body")

    @patch("monitor.urllib.request.urlopen")
    def test_sends_to_default_server(self, mock_urlopen):
        os.environ["NTFY_TOPIC"] = "test-topic"
        mock_urlopen.return_value.__enter__.return_value.status = 200
        NtfyNotifier().send("Alert", "Something changed")
        args, _ = mock_urlopen.call_args
        request = args[0]
        self.assertEqual(request.full_url, "https://ntfy.sh/test-topic")
        self.assertEqual(request.get_header("Title"), "Alert")
        self.assertEqual(request.get_header("Priority"), "default")
        self.assertEqual(request.data, b"Something changed")

    @patch("monitor.urllib.request.urlopen")
    def test_uses_token_tags_and_custom_server(self, mock_urlopen):
        os.environ.update(
            {
                "NTFY_URL": "https://ntfy.example.com",
                "NTFY_TOPIC": "alerts",
                "NTFY_TOKEN": "tk_abc",
                "NTFY_PRIORITY": "high",
                "NTFY_TAGS": "rotating_light",
            }
        )
        mock_urlopen.return_value.__enter__.return_value.status = 200
        NtfyNotifier().send("Hi", "There")
        args, _ = mock_urlopen.call_args
        request = args[0]
        self.assertEqual(request.full_url, "https://ntfy.example.com/alerts")
        self.assertEqual(request.get_header("Authorization"), "Bearer tk_abc")
        self.assertEqual(request.get_header("Priority"), "high")
        self.assertEqual(request.get_header("Tags"), "rotating_light")

    @patch("monitor.urllib.request.urlopen")
    def test_http_error_raised(self, mock_urlopen):
        os.environ["NTFY_TOPIC"] = "topic"
        fp = io.BytesIO(b"error detail")
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="https://ntfy.sh/topic", code=403, msg="Forbidden", hdrs={}, fp=fp
        )
        with self.assertRaisesRegex(Exception, "403.*error detail"):
            NtfyNotifier().send("title", "body")


class LogUsageTests(unittest.TestCase):
    def test_logs_one_line_per_snapshot(self):
        temp = Path("/tmp/test_usage.log")
        try:
            temp.unlink()
        except FileNotFoundError:
            pass
        snapshot = snap(p_used=42, s_used=7, balance="12.50")
        log_usage(temp, snapshot)
        log_usage(temp, snapshot)
        lines = temp.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn("primary=42%", lines[0])
        self.assertIn("secondary=7%", lines[0])
        self.assertIn("balance=12.50", lines[0])
        temp.unlink()


class PricingAndHeadroomTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pricing = __import__("monitor").load_model_pricing(
            Path(__file__).resolve().parents[1] / "model-pricing.json.example"
        )
        cls.report_fixture = json.loads(
            (
                Path(__file__).resolve().parent
                / "fixtures"
                / "current_report_2026-09-10.json"
            ).read_text(encoding="utf-8")
        )

    def test_current_report_fixture_token_categories_reconcile(self):
        fixture = self.report_fixture
        for row in fixture["models"].values():
            self.assertEqual(
                row["cached_input_tokens"] + row["uncached_input_tokens"],
                row["total_input_tokens"],
            )
        total = fixture["total"]
        self.assertEqual(
            total["cached_input_tokens"] + total["uncached_input_tokens"],
            total["total_input_tokens"],
        )
        self.assertEqual(
            sum(row["total_input_tokens"] for row in fixture["models"].values()),
            total["total_input_tokens"],
        )
        self.assertEqual(
            sum(row["cached_input_tokens"] for row in fixture["models"].values()),
            total["cached_input_tokens"],
        )
        self.assertEqual(
            sum(row["uncached_input_tokens"] for row in fixture["models"].values()),
            total["uncached_input_tokens"],
        )
        self.assertEqual(
            sum(row["output_tokens"] for row in fixture["models"].values()),
            total["output_tokens"],
        )

    def test_current_report_fixture_old_costs_are_explicitly_legacy(self):
        self.assertEqual(self.report_fixture["pricing_mode"], "codex")
        self.assertEqual(
            self.report_fixture["total"]["reported_cost_usd"], "14.02215239"
        )
        self.assertNotEqual(
            Decimal(self.report_fixture["total"]["reported_cost_usd"]),
            Decimal("8.34993452"),
        )

    def test_only_codex_pricing_mode_is_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pricing.json"
            raw = json.loads(
                (
                    Path(__file__).resolve().parents[1]
                    / "model-pricing.json.example"
                ).read_text(encoding="utf-8")
            )
            raw["pricing_mode"] = "api"
            path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaisesRegex(Exception, "pricing mode"):
                __import__("monitor").load_model_pricing(path)

    def test_fixture_sized_request_replay_is_authoritative(self):
        pricing = PricingTable(
            revision="fixture",
            currency="USD",
            unit="per_1m_tokens",
            models={
                "test-model": ModelPricing(
                    model_id="test-model",
                    input_rate=Decimal("0.20"),
                    output_rate=Decimal("1.20"),
                    cached_input_rate=Decimal("0.02"),
                    cache_write_rate=Decimal("999"),
                    long_context_threshold_tokens=272000,
                    long_context_input_multiplier=Decimal("2"),
                    long_context_output_multiplier=Decimal("1.5"),
                )
            },
            aliases={},
            pricing_mode="codex",
        )
        records = (
            HeadroomUsageRecord(1, "test-model", 300_000, 1_000, 100_000, 200_000, "a"),
            HeadroomUsageRecord(2, "test-model", 100_000, 2_000, 50_000, 50_000, "b"),
        )
        period = aggregate_period(records, pricing, 0, 3)
        expected = (
            (Decimal("200000") * Decimal("0.20") * Decimal("2"))
            + (Decimal("100000") * Decimal("0.02") * Decimal("2"))
            + (Decimal("1000") * Decimal("1.20") * Decimal("1.5"))
            + (Decimal("50000") * Decimal("0.20"))
            + (Decimal("50000") * Decimal("0.02"))
            + (Decimal("2000") * Decimal("1.20"))
        ) / Decimal("1000000")
        self.assertEqual(period["total"]["known_cost"], expected)
        self.assertEqual(period["total"]["uncached_input_tokens"], 250_000)
        self.assertEqual(period["total"]["cached_input_tokens"], 150_000)
        self.assertEqual(period["total"]["priced_cache_write_tokens"], 0)

    def test_long_context_boundary_is_strictly_above_272k_input(self):
        pricing = self.simple_pricing(
            input_rate="1",
            output_rate="2",
            cached_rate="0.1",
            threshold=272000,
        )
        below = price_usage_record(
            HeadroomUsageRecord(1, "test-model", 271999, 1, 0, 271999, "below"),
            pricing,
        )
        at = price_usage_record(
            HeadroomUsageRecord(2, "test-model", 272000, 1, 0, 272000, "at"),
            pricing,
        )
        above = price_usage_record(
            HeadroomUsageRecord(3, "test-model", 272001, 1, 0, 272001, "above"),
            pricing,
        )
        self.assertFalse(below.long_context)
        self.assertFalse(at.long_context)
        self.assertTrue(above.long_context)
        self.assertEqual(below.uncached_input_tokens, 271999)
        self.assertEqual(at.uncached_input_tokens, 272000)
        self.assertEqual(above.uncached_input_tokens, 272001)
        self.assertEqual(
            below.total_cost,
            (Decimal("271999") + Decimal("2") / Decimal("1")) / Decimal("1000000"),
        )
        self.assertEqual(
            at.total_cost,
            (Decimal("272000") + Decimal("2") / Decimal("1")) / Decimal("1000000"),
        )
        self.assertEqual(
            above.total_cost,
            (
                Decimal("272001") * Decimal("2")
                + Decimal("1") * Decimal("2") * Decimal("1.5")
            )
            / Decimal("1000000"),
        )

    @staticmethod
    def simple_pricing(*, input_rate="1", output_rate="0", cached_rate="0", threshold=None):
        return PricingTable(
            revision="test",
            currency="USD",
            unit="per_1m_tokens",
            models={
                "test-model": ModelPricing(
                    model_id="test-model",
                    input_rate=Decimal(input_rate),
                    output_rate=Decimal(output_rate),
                    cached_input_rate=Decimal(cached_rate),
                    cache_write_rate=Decimal("0"),
                    long_context_threshold_tokens=threshold,
                    long_context_input_multiplier=Decimal("2"),
                    long_context_output_multiplier=Decimal("1.5"),
                    processing_multipliers=(("standard", Decimal("1")), ("fast", Decimal("2"))),
                )
            },
            aliases={},
        )

    @staticmethod
    def priced_period(pricing, timestamp, *, input_tokens=1_000_000, cached=0, output=0, tier=None):
        record = HeadroomUsageRecord(
            timestamp,
            "test-model",
            input_tokens,
            output,
            cached,
            max(0, input_tokens - cached),
            f"request-{timestamp}",
            processing_tier=tier,
        )
        return aggregate_period((record,), pricing, timestamp, timestamp + 1)

    def test_processing_tier_and_long_context_are_applied_per_request(self):
        pricing = self.simple_pricing(input_rate="1", output_rate="2", cached_rate="0.1", threshold=100)
        records = (
            HeadroomUsageRecord(1, "test-model", 100, 0, 0, 100, "short"),
            HeadroomUsageRecord(2, "test-model", 101, 10, 1, 100, "long", processing_tier="fast"),
        )
        period = aggregate_period(records, pricing, 0, 3)
        # Short request: 100 * $1/M. Long fast request: (100*$1 + 1*$0.1)*2/M
        # plus 10*$2*1.5*2/M output.
        self.assertEqual(period["total"]["known_cost"], Decimal("0.0005604"))
        row = period["models"]["test-model"]
        self.assertEqual(row["long_context_requests"], 1)
        self.assertEqual(row["processing_tiers"], ["standard", "fast"])
        self.assertEqual(row["uncached_input_tokens"], 200)
        self.assertEqual(row["cached_input_tokens"], 1)

    def test_repeated_samples_inside_bucket_do_not_create_crossings(self):
        pricing = self.simple_pricing()
        empty = aggregate_period((), pricing, 0, 1)
        paid = self.priced_period(pricing, 1)
        baseline = LimitWindow(10, 1000, 10080)
        state, _ = update_window_calibration("secondary", None, baseline, empty, None, "test")

        state, first = update_window_calibration(
            "secondary", baseline, LimitWindow(10, 1000, 10080), paid, state, "test"
        )
        state, repeated = update_window_calibration(
            "secondary", LimitWindow(10, 1000, 10080), LimitWindow(10, 1000, 10080), paid, state, "test"
        )
        self.assertIsNone(first["full_window_usd"])
        self.assertIsNone(repeated["full_window_usd"])
        self.assertEqual(len(state["crossings"]), 1)
        self.assertEqual(repeated["known_cost_usd"], "2")

        state, crossed = update_window_calibration(
            "secondary", LimitWindow(10, 1000, 10080), LimitWindow(11, 1000, 10080), paid, state, "test"
        )
        self.assertEqual(len(state["crossings"]), 2)
        self.assertEqual(Decimal(crossed["full_window_usd"]), Decimal("300"))
        self.assertEqual(crossed["full_observed_estimate"]["observed_range"], "10% -> 11%")

    def test_full_estimate_uses_all_cost_since_anchor_after_crossing(self):
        pricing = self.simple_pricing(input_rate="0.01")
        empty = aggregate_period((), pricing, 0, 1)
        before_bucket_end = self.priced_period(
            pricing, 1, input_tokens=1_150_047_728
        )
        inside_same_bucket = self.priced_period(
            pricing, 2, input_tokens=56_740_716
        )
        anchor = LimitWindow(9, 1000, 10080)
        state, _ = update_window_calibration(
            "secondary", None, anchor, empty, None, "test"
        )
        state, _ = update_window_calibration(
            "secondary", anchor, LimitWindow(16, 1000, 10080),
            before_bucket_end, state, "test"
        )
        state, estimate = update_window_calibration(
            "secondary", LimitWindow(16, 1000, 10080),
            LimitWindow(16, 1000, 10080), inside_same_bucket, state, "test"
        )

        displayed_cost = Decimal(estimate["known_cost_usd"])
        displayed_delta = Decimal(estimate["delta_percent_points"])
        displayed_estimate = Decimal(estimate["full_window_usd"])
        self.assertEqual(displayed_cost, Decimal("12.06788444"))
        self.assertEqual(displayed_delta, Decimal("7"))
        self.assertEqual(
            displayed_cost * Decimal("100") / displayed_delta,
            displayed_estimate,
        )
        self.assert_displayed_estimates_reconcile(estimate)

    def assert_displayed_estimates_reconcile(self, estimate):
        """Every displayed projection must use the cost shown for its interval."""

        tolerance = Decimal("0.00000001")
        full = estimate.get("full_observed_estimate")
        if full is not None:
            displayed_cost = Decimal(estimate["known_cost_usd"])
            displayed_delta = Decimal(estimate["delta_percent_points"])
            displayed_full = Decimal(estimate["full_window_usd"])
            self.assertEqual(displayed_cost, Decimal(full["cost_usd"]))
            self.assertLessEqual(
                abs(displayed_cost * Decimal("100") / displayed_delta - displayed_full),
                tolerance,
            )
        for interval in [full, *estimate.get("rolling_estimates", {}).values()]:
            if interval is None:
                continue
            cost = Decimal(interval["cost_usd"])
            points = Decimal(interval["completed_percentage_points"])
            projected = Decimal(interval["estimate_100_usd"])
            self.assertLessEqual(
                abs(cost * Decimal("100") / points - projected), tolerance
            )

    def test_skipped_percentage_values_keep_only_observed_endpoint(self):
        pricing = self.simple_pricing()
        empty = aggregate_period((), pricing, 0, 1)
        paid = self.priced_period(pricing, 1, input_tokens=3_000_000)
        baseline = LimitWindow(10, 1000, 10080)
        state, _ = update_window_calibration("secondary", None, baseline, empty, None, "test")
        state, estimate = update_window_calibration(
            "secondary", baseline, LimitWindow(13, 1000, 10080), paid, state, "test"
        )
        self.assertEqual([row["percent"] for row in state["crossings"]], [10, 13])
        self.assertEqual(state["crossings"][-1]["skipped_points"], [11, 12])
        self.assertEqual(Decimal(estimate["full_window_usd"]), Decimal("100"))
        self.assertIn("3", estimate["rolling_estimates"])
        self.assertNotIn("1", estimate["rolling_estimates"])

    def test_rolling_estimates_and_confidence_use_completed_points(self):
        pricing = self.simple_pricing()
        empty = aggregate_period((), pricing, 0, 1)
        baseline = LimitWindow(9, 1000, 10080)
        state, _ = update_window_calibration("secondary", None, baseline, empty, None, "test")
        previous = baseline
        estimate = None
        for percent in range(10, 17):
            estimate_period = self.priced_period(pricing, percent, input_tokens=1_000_000)
            current = LimitWindow(percent, 1000, 10080)
            state, estimate = update_window_calibration(
                "secondary", previous, current, estimate_period, state, "test"
            )
            previous = current
        assert estimate is not None
        self.assertEqual(estimate["completed_percentage_points"], 7)
        self.assertEqual(estimate["confidence"], "Medium")
        self.assertEqual(estimate["full_observed_estimate"]["observed_range"], "9% -> 16%")
        self.assertEqual(estimate["rolling_estimates"]["1"]["observed_range"], "15% -> 16%")
        self.assertEqual(estimate["rolling_estimates"]["3"]["observed_range"], "13% -> 16%")
        self.assertEqual(estimate["rolling_estimates"]["5"]["observed_range"], "11% -> 16%")
        self.assertNotIn("10", estimate["rolling_estimates"])
        self.assert_displayed_estimates_reconcile(estimate)

    def test_model_attribution_is_reported_for_each_interval(self):
        pricing = PricingTable(
            revision="test",
            currency="USD",
            unit="per_1m_tokens",
            models={
                "luna": ModelPricing("luna", Decimal("1"), Decimal("0"), Decimal("0"), Decimal("0")),
                "sol": ModelPricing("sol", Decimal("4"), Decimal("0"), Decimal("0"), Decimal("0")),
            },
            aliases={},
        )
        records = (
            HeadroomUsageRecord(1, "luna", 1_000_000, 0, 0, 1_000_000, "luna"),
            HeadroomUsageRecord(1, "sol", 1_000_000, 0, 0, 1_000_000, "sol"),
        )
        period = aggregate_period(records, pricing, 0, 1)
        baseline = LimitWindow(10, 1000, 10080)
        state, _ = update_window_calibration(
            "secondary", None, baseline, aggregate_period((), pricing, 0, 1), None, "test"
        )
        _, estimate = update_window_calibration(
            "secondary", baseline, LimitWindow(11, 1000, 10080), period, state, "test"
        )
        attribution = estimate["full_observed_estimate"]["model_attribution"]
        self.assertEqual(attribution["luna"]["requests"], 1)
        self.assertEqual(attribution["luna"]["uncached_input_tokens"], 1_000_000)
        self.assertEqual(attribution["luna"]["cost_usd"], "1")
        self.assertEqual(attribution["sol"]["cost_usd"], "4")
        self.assertEqual(attribution["sol"]["interval_cost_percent"], "80")

    def test_primary_and_secondary_accounting_are_independent(self):
        pricing = self.simple_pricing()
        empty = aggregate_period((), pricing, 0, 1)
        paid = self.priced_period(pricing, 1)
        primary, _ = update_window_calibration(
            "primary", None, LimitWindow(20, 1000, 300), empty, None, "test"
        )
        secondary, _ = update_window_calibration(
            "secondary", None, LimitWindow(10, 2000, 10080), empty, None, "test"
        )
        primary, primary_estimate = update_window_calibration(
            "primary", LimitWindow(20, 1000, 300), LimitWindow(22, 1000, 300), paid, primary, "test"
        )
        secondary, secondary_estimate = update_window_calibration(
            "secondary", LimitWindow(10, 2000, 10080), LimitWindow(10, 2000, 10080), paid, secondary, "test"
        )
        self.assertEqual(primary_estimate["full_observed_estimate"]["observed_range"], "20% -> 22%")
        self.assertIsNone(secondary_estimate["full_window_usd"])
        self.assertEqual(primary["anchor"]["window_duration_mins"], 300)
        self.assertEqual(secondary["anchor"]["window_duration_mins"], 10080)

    def test_reset_closes_cycle_and_starts_clean_crossing_series(self):
        pricing = self.simple_pricing()
        empty = aggregate_period((), pricing, 0, 1)
        paid = self.priced_period(pricing, 1)
        baseline = LimitWindow(10, 1000, 10080)
        state, _ = update_window_calibration("secondary", None, baseline, empty, None, "test")
        state, before_reset = update_window_calibration(
            "secondary", baseline, LimitWindow(12, 1000, 10080), paid, state, "test"
        )
        new_state, after_reset = update_window_calibration(
            "secondary", LimitWindow(12, 1000, 10080), LimitWindow(3, 2000, 10080), empty, state, "test"
        )
        self.assertEqual(before_reset["completed_percentage_points"], 2)
        self.assertEqual(after_reset["status"], "reset")
        self.assertEqual(new_state["anchor"]["used_percent"], 3)
        self.assertEqual(len(new_state["crossings"]), 1)
        self.assertEqual(len(new_state["cycle_history"]), 1)
        # +2 was one interval: its per-point attribution is unresolved.
        self.assertEqual(new_state["cycle_history"][0]["confidence"], "Very low")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            snapshot = snap(s_used=3, s_reset=2000)
            save_state(path, snapshot, {"schema_version": 3, "windows": {"secondary": new_state}})
            persisted, persisted_accounting = load_state_bundle(path)
        self.assertEqual(snapshot_to_dict(persisted)["secondary"]["used_percent"], 3)
        self.assertEqual(
            persisted_accounting["windows"]["secondary"]["cycle_history"][0]["close_reason"],
            "reset",
        )

    def test_expensive_period_is_preserved_as_spike(self):
        pricing = self.simple_pricing()
        empty = aggregate_period((), pricing, 0, 1)
        normal = self.priced_period(pricing, 1, input_tokens=1_000_000)
        expensive = self.priced_period(pricing, 2, input_tokens=4_000_000)
        baseline = LimitWindow(10, 1000, 10080)
        state, _ = update_window_calibration("secondary", None, baseline, empty, None, "test")
        state, _ = update_window_calibration(
            "secondary", baseline, LimitWindow(11, 1000, 10080), normal, state, "test"
        )
        state, estimate = update_window_calibration(
            "secondary", LimitWindow(11, 1000, 10080), LimitWindow(11, 1000, 10080), expensive, state, "test"
        )
        self.assertEqual(len(state["spikes"]), 1)
        self.assertEqual(state["spikes"][0]["cost_usd"], "4")
        self.assertGreater(Decimal(estimate["spikes"][0]["share_of_observed_cost"]), Decimal("79"))

    def test_schema_migration_preserves_legacy_summary_without_reusing_cost(self):
        old_state = {
            "captured_at": 100,
            "primary": {"used_percent": 20, "resets_at": 1000, "window_duration_mins": 300},
            "secondary": {"used_percent": 10, "resets_at": 2000, "window_duration_mins": 10080},
            "credit_balance": None,
            "reset_credit_available_count": 0,
            "reset_credit_ids": [],
            "accounting": {
                "schema_version": 2,
                "windows": {
                    "secondary": {
                        "anchor": {"captured_at": 1, "used_percent": 9, "window_duration_mins": 10080},
                        "known_cost": "123.45",
                    }
                },
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text(json.dumps(old_state), encoding="utf-8")
            snapshot, accounting = load_state_bundle(path)
        self.assertIsNotNone(snapshot)
        self.assertTrue(accounting["migration_required"])
        self.assertEqual(accounting["schema_version"], 3)
        self.assertEqual(
            accounting["legacy_cycles_by_window"]["secondary"][0]["known_cost_usd"], "123.45"
        )

    def test_default_models_and_sol_breakdown(self):
        self.assertEqual(
            set(self.pricing.models),
            {
                "gpt-5.5",
                "gpt-5.6-luna",
                "gpt-5.6-terra",
                "gpt-5.6-sol",
                "gpt-6-astra",
                "gpt-6-sol",
                "gpt-6-luna",
                "gpt-6.1-sol",
            },
        )
        record = HeadroomUsageRecord(
            timestamp=1,
            model="sol",
            input_tokens=1000,
            output_tokens=100,
            cache_read_tokens=600,
            raw_cache_write_tokens=200,
            request_id="turn-1",
        )
        result = price_usage_record(record, self.pricing)
        self.assertEqual(result.total_cost, Decimal("0.00384"))
        self.assertEqual(result.uncached_input_tokens, 400)
        self.assertEqual(result.ordinary_input_tokens, 400)
        self.assertEqual(result.cache_write_cost, Decimal("0"))

    def test_inferred_write_policy_is_not_a_second_billable_category(self):
        record = HeadroomUsageRecord(
            timestamp=1,
            model="gpt-5.6-sol",
            input_tokens=1000,
            output_tokens=0,
            cache_read_tokens=600,
            raw_cache_write_tokens=400,
            request_id="turn-1",
            cache_write_inferred=True,
        )
        estimated = price_usage_record(record, self.pricing, inferred_cache_write_mode="estimate")
        ordinary = price_usage_record(record, self.pricing, inferred_cache_write_mode="ordinary_input")
        self.assertEqual(estimated.total_cost, Decimal("0.00184"))
        self.assertEqual(ordinary.total_cost, Decimal("0.00184"))
        self.assertEqual(estimated.uncached_input_tokens, 400)
        self.assertEqual(estimated.raw_cache_write_tokens, 400)

    def test_invalid_record_keeps_same_model_period_incomplete(self):
        records = (
            HeadroomUsageRecord(1, "sol", 10, 0, 11, 4, "bad"),
            HeadroomUsageRecord(2, "sol", 10, 0, 5, 5, "good"),
        )
        period = aggregate_period(records, self.pricing, 0, 3)
        self.assertFalse(period["models"]["gpt-5.6-sol"]["complete"])
        self.assertIsNone(period["models"]["gpt-5.6-sol"]["estimated_cost"])
        self.assertEqual(period["total"]["reason"], "invalid_counters")

    def test_parser_and_incremental_cursor_count_repeated_connection_ids(self):
        line_one = (
            "2026-09-08 12:00:01,000 - headroom.proxy - INFO - [hr_same] PERF "
            "model=gpt-5.6-luna tok_after=100 tok_out=10 cache_read=60 cache_write=40 client=codex\n"
        )
        parsed = parse_headroom_perf_line(line_one, timezone_name="UTC")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.model, "gpt-5.6-luna")
        self.assertEqual(parsed.raw_cache_write_tokens, 40)
        self.assertEqual(parsed.cache_write_tokens, 40)
        line_two = line_one.replace("12:00:01,000", "12:00:02,000")
        with tempfile.TemporaryDirectory() as directory:
            log_dir = Path(directory)
            log_path = log_dir / "proxy.log"
            log_path.write_text(line_one + line_two, encoding="utf-8")
            records, cursor, diagnostics = collect_headroom_usage(
                log_dir, 0, 2_000_000_000, {}, client="codex", timezone_name="UTC"
            )
            self.assertEqual(len(records), 2)
            self.assertEqual(records[0].request_id, records[1].request_id)
            self.assertEqual(diagnostics["accepted"], 2)
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(line_one.replace("12:00:01,000", "12:00:03,000").rstrip("\n"))
            records, cursor, _ = collect_headroom_usage(
                log_dir, 0, 2_000_000_000, cursor, client="codex", timezone_name="UTC"
            )
            self.assertEqual(records, [])
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write("\n")
            records, _, _ = collect_headroom_usage(
                log_dir, 0, 2_000_000_000, cursor, client="codex", timezone_name="UTC"
            )
            self.assertEqual(len(records), 1)

    def test_parser_preserves_request_processing_tier(self):
        line = (
            "2026-09-08 12:00:01,000 - headroom.proxy - INFO - [hr_tier] PERF "
            "model=gpt-5.6-luna tok_after=100 tok_out=10 cache_read=60 cache_write=40 "
            "processing_tier=fast client=codex\n"
        )
        parsed = parse_headroom_perf_line(line, timezone_name="UTC")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.processing_tier, "fast")

    def test_copied_rotation_does_not_replay_new_suffix(self):
        line_one = (
            "2026-09-08 12:00:01,000 - headroom.proxy - INFO - [hr_one] PERF "
            "model=gpt-5.6-sol tok_after=100 tok_out=10 cache_read=60 cache_write=40 client=codex\n"
        )
        line_two = line_one.replace("12:00:01,000", "12:00:02,000").replace("hr_one", "hr_two")
        with tempfile.TemporaryDirectory() as directory:
            log_dir = Path(directory)
            log_path = log_dir / "proxy.log"
            log_path.write_text(line_one, encoding="utf-8")
            _, cursor, _ = collect_headroom_usage(
                log_dir, 0, 2_000_000_000, {}, client="codex", timezone_name="UTC"
            )
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(line_two)
            (log_dir / "proxy.log.1").write_bytes(log_path.read_bytes())
            records, _, diagnostics = collect_headroom_usage(
                log_dir, 0, 2_000_000_000, cursor, client="codex", timezone_name="UTC"
            )
            self.assertEqual([record.request_id for record in records], ["hr_two"])
            self.assertEqual(diagnostics["duplicate_rows_skipped"], 1)

    def test_port_suffixed_proxy_log_is_discovered_and_counted(self):
        line = (
            "2026-09-08 12:00:01,000 - headroom.proxy - INFO - [hr_port] PERF "
            "model=gpt-5.6-luna tok_after=100 tok_out=10 cache_read=60 cache_write=40 client=codex\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            log_dir = Path(directory)
            (log_dir / "proxy.log").write_text(line, encoding="utf-8")
            (log_dir / "proxy-8787.log").write_text(line, encoding="utf-8")
            (log_dir / "proxy-8787.log.1").write_text(line, encoding="utf-8")
            (log_dir / "notes.jsonl").write_text(line, encoding="utf-8")
            self.assertEqual(
                [path.name for path in headroom_log_paths(log_dir)],
                ["proxy-8787.log", "proxy-8787.log.1", "proxy.log"],
            )
            records, _, diagnostics = collect_headroom_usage(
                log_dir, 0, 2_000_000_000, {}, client="codex", timezone_name="UTC"
            )
            self.assertEqual(len(records), 3)
            self.assertEqual(diagnostics["files_seen"], 3)

    def test_untagged_perf_row_is_accepted_with_missing_client(self):
        line = (
            "2026-09-08 12:00:01,000 - headroom.proxy - INFO - [hr_pi] PERF "
            "model=gpt-5.5 tok_after=100 tok_out=10 cache_read=0 cache_write=0\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            log_dir = Path(directory)
            (log_dir / "proxy-8787.log").write_text(line, encoding="utf-8")
            records, _, diagnostics = collect_headroom_usage(
                log_dir, 0, 2_000_000_000, {}, client="codex", timezone_name="UTC"
            )
            self.assertEqual([record.model for record in records], ["gpt-5.5"])
            self.assertEqual(diagnostics["missing_client"], 1)
            self.assertEqual(diagnostics["excluded"], 0)

    def test_port_suffixed_rotation_does_not_replay_new_suffix(self):
        line_one = (
            "2026-09-08 12:00:01,000 - headroom.proxy - INFO - [hr_one] PERF "
            "model=gpt-5.6-sol tok_after=100 tok_out=10 cache_read=60 cache_write=40 client=codex\n"
        )
        line_two = line_one.replace("12:00:01,000", "12:00:02,000").replace("hr_one", "hr_two")
        with tempfile.TemporaryDirectory() as directory:
            log_dir = Path(directory)
            log_path = log_dir / "proxy-8787.log"
            log_path.write_text(line_one, encoding="utf-8")
            _, cursor, _ = collect_headroom_usage(
                log_dir, 0, 2_000_000_000, {}, client="codex", timezone_name="UTC"
            )
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(line_two)
            (log_dir / "proxy-8787.log.1").write_bytes(log_path.read_bytes())
            records, _, diagnostics = collect_headroom_usage(
                log_dir, 0, 2_000_000_000, cursor, client="codex", timezone_name="UTC"
            )
            self.assertEqual([record.request_id for record in records], ["hr_two"])
            self.assertEqual(diagnostics["duplicate_rows_skipped"], 1)

    def test_prime_cursor_baselines_port_suffixed_logs(self):
        line = (
            "2026-09-08 12:00:01,000 - headroom.proxy - INFO - [hr_old] PERF "
            "model=gpt-5.6-luna tok_after=100 tok_out=10 cache_read=60 cache_write=40 client=codex\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            log_dir = Path(directory)
            (log_dir / "proxy.log").write_text(line, encoding="utf-8")
            log_path = log_dir / "proxy-8787.log"
            log_path.write_text(line, encoding="utf-8")
            cursor, diagnostics = prime_headroom_cursor(log_dir)
            self.assertTrue(diagnostics["baseline"])
            self.assertEqual(diagnostics["files_seen"], 2)
            self.assertEqual(
                sorted(Path(entry["path"]).name for entry in cursor["files"].values()),
                ["proxy-8787.log", "proxy.log"],
            )
            for entry in cursor["files"].values():
                self.assertEqual(entry["offset"], len(line.encode("utf-8")))
            records, _, _ = collect_headroom_usage(
                log_dir, 0, 2_000_000_000, cursor, client="codex", timezone_name="UTC"
            )
            self.assertEqual(records, [])
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(line.replace("hr_old", "hr_new"))
            records, _, _ = collect_headroom_usage(
                log_dir, 0, 2_000_000_000, cursor, client="codex", timezone_name="UTC"
            )
            self.assertEqual([record.request_id for record in records], ["hr_new"])

    def test_window_estimate_accumulates_cost_over_percentage_delta(self):
        pricing = PricingTable(
            revision="test",
            currency="USD",
            unit="per_1m_tokens",
            models={
                "test-model": ModelPricing(
                    model_id="test-model",
                    input_rate=Decimal("500"),
                    output_rate=Decimal("0"),
                    cached_input_rate=Decimal("0"),
                    cache_write_rate=Decimal("500"),
                )
            },
            aliases={},
        )
        record = HeadroomUsageRecord(1, "test-model", 1000, 0, 0, 0, "x")
        period = aggregate_period((record,), pricing, 100, 200)
        window20 = LimitWindow(20, 1000, 300)
        state, baseline = update_window_calibration(
            "primary", None, window20, aggregate_period((), pricing, 0, 100), None, "test"
        )
        self.assertEqual(baseline["status"], "baseline")
        _, estimate = update_window_calibration(
            "primary", window20, LimitWindow(22, 1000, 300), period, state, "test"
        )
        self.assertEqual(estimate["status"], "estimated")
        self.assertEqual(Decimal(estimate["full_window_usd"]), Decimal("25"))

    def test_window_estimate_reports_cumulative_usage_until_reset(self):
        pricing = PricingTable(
            revision="test",
            currency="USD",
            unit="per_1m_tokens",
            models={
                "test-model": ModelPricing(
                    model_id="test-model",
                    input_rate=Decimal("500"),
                    output_rate=Decimal("0"),
                    cached_input_rate=Decimal("0"),
                    cache_write_rate=Decimal("500"),
                )
            },
            aliases={},
        )
        first = aggregate_period(
            (HeadroomUsageRecord(1, "test-model", 1000, 10, 600, 400, "one"),),
            pricing,
            100,
            200,
        )
        second = aggregate_period(
            (HeadroomUsageRecord(2, "test-model", 2000, 20, 1000, 1000, "two"),),
            pricing,
            200,
            300,
        )
        baseline_window = LimitWindow(20, 1000, 10080)
        state, _ = update_window_calibration(
            "secondary", None, baseline_window, aggregate_period((), pricing, 0, 100), None, "test"
        )

        state, first_estimate = update_window_calibration(
            "secondary", baseline_window, LimitWindow(21, 1000, 10080), first, state, "test"
        )
        self.assertEqual(first_estimate["cumulative_usage"]["requests"], 1)
        self.assertEqual(first_estimate["cumulative_usage"]["input_tokens"], 1000)
        self.assertEqual(first_estimate["cumulative_usage"]["models"]["test-model"]["output_tokens"], 10)

        state, second_estimate = update_window_calibration(
            "secondary", LimitWindow(21, 1000, 10080), LimitWindow(21, 1000, 10080), second, state, "test"
        )
        cumulative = second_estimate["cumulative_usage"]
        self.assertEqual(cumulative["requests"], 2)
        self.assertEqual(cumulative["input_tokens"], 3000)
        self.assertEqual(cumulative["output_tokens"], 30)
        self.assertEqual(cumulative["cached_input_tokens"], 1600)
        self.assertEqual(cumulative["uncached_input_tokens"], 1400)
        self.assertEqual(cumulative["ordinary_input_tokens"], 1400)
        self.assertEqual(cumulative["priced_cache_write_tokens"], 0)
        self.assertEqual(cumulative["models"]["test-model"]["requests"], 2)

        _, reset_estimate = update_window_calibration(
            "secondary", LimitWindow(21, 1000, 10080), LimitWindow(10, 2000, 10080), second, state, "test"
        )
        self.assertEqual(reset_estimate["status"], "reset")
        self.assertEqual(reset_estimate["cumulative_usage"]["requests"], 0)

    def test_backfill_adds_logged_usage_after_existing_anchor(self):
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "usage.log"
            base = {
                "period": {"start": "1970-01-01T00:01:40+00:00", "elapsed_seconds": 1},
                "total": {
                    "requests": 1,
                    "input_tokens": 100,
                    "output_tokens": 10,
                    "cache_read_tokens": 60,
                    "cache_write_tokens": 40,
                    "ordinary_input_tokens": 0,
                    "cached_input_tokens": 60,
                    "priced_cache_write_tokens": 40,
                },
                "models": {
                    "test-model": {
                        "requests": 1,
                        "input_tokens": 100,
                        "output_tokens": 10,
                        "cache_read_tokens": 60,
                        "cache_write_tokens": 40,
                        "ordinary_input_tokens": 0,
                        "cached_input_tokens": 60,
                        "priced_cache_write_tokens": 40,
                    }
                },
            }
            first = dict(base, period=dict(base["period"], end="1970-01-01T00:03:20+00:00"), period_id="one")
            second = dict(base, period=dict(base["period"], end="1970-01-01T00:06:40+00:00"), period_id="two")
            log_usage(log_path, snap(), first)
            log_usage(log_path, snap(), second)
            accounting = {
                "last_sample_at": 300,
                "windows": {
                    "secondary": {"anchor": {"captured_at": 100}},
                },
            }

            changed = backfill_accounting_usage_from_log(accounting, log_path)

            self.assertTrue(changed)
            cumulative = accounting["windows"]["secondary"]["usage_total"]
            self.assertEqual(cumulative["requests"], 1)
            self.assertEqual(cumulative["input_tokens"], 100)
            self.assertEqual(
                accounting["windows"]["secondary"]["usage_by_model"]["test-model"]["output_tokens"],
                10,
            )

    def test_moving_reset_timestamp_does_not_reset_calibration(self):
        pricing = PricingTable(
            revision="test",
            currency="USD",
            unit="per_1m_tokens",
            models={
                "test-model": ModelPricing(
                    model_id="test-model",
                    input_rate=Decimal("500"),
                    output_rate=Decimal("0"),
                    cached_input_rate=Decimal("0"),
                    cache_write_rate=Decimal("500"),
                )
            },
            aliases={},
        )
        record = HeadroomUsageRecord(1, "test-model", 1000, 0, 0, 0, "x")
        period = aggregate_period((record,), pricing, 100, 200)
        baseline_window = LimitWindow(20, 1000, 300)
        state, _ = update_window_calibration(
            "primary", None, baseline_window, aggregate_period((), pricing, 0, 100), None, "test"
        )

        state, unchanged = update_window_calibration(
            "primary", baseline_window, LimitWindow(20, 2000, 300), period, state, "test"
        )
        self.assertEqual(unchanged["status"], "insufficient_percentage_crossings")
        self.assertEqual(state["anchor"]["resets_at"], 1000)

        _, estimate = update_window_calibration(
            "primary", LimitWindow(20, 2000, 300), LimitWindow(22, 3000, 300),
            period, state, "test"
        )
        self.assertEqual(estimate["status"], "estimated")
        self.assertEqual(Decimal(estimate["full_window_usd"]), Decimal("50"))

    def test_unattributed_percentage_jump_cannot_distort_estimate(self):
        pricing = PricingTable(
            revision="test",
            currency="USD",
            unit="per_1m_tokens",
            models={
                "test-model": ModelPricing(
                    model_id="test-model",
                    input_rate=Decimal("500"),
                    output_rate=Decimal("0"),
                    cached_input_rate=Decimal("0"),
                    cache_write_rate=Decimal("500"),
                )
            },
            aliases={},
        )
        record = HeadroomUsageRecord(1, "test-model", 1000, 0, 0, 0, "x")
        paid_period = aggregate_period((record,), pricing, 100, 200)
        empty_period = aggregate_period((), pricing, 200, 300)
        baseline_window = LimitWindow(20, 1000, 300)
        state, _ = update_window_calibration(
            "secondary", None, baseline_window, empty_period, None, "test"
        )

        state, first_estimate = update_window_calibration(
            "secondary", baseline_window, LimitWindow(21, 1000, 300), paid_period, state, "test"
        )
        self.assertEqual(first_estimate["status"], "estimated")

        state, unattributed = update_window_calibration(
            "secondary", LimitWindow(21, 1000, 300), LimitWindow(50, 2000, 300),
            empty_period, state, "test"
        )
        self.assertEqual(unattributed["status"], "unattributed_usage")
        self.assertIsNone(unattributed["full_window_usd"])
        self.assertEqual(state["anchor"]["used_percent"], 50)
        self.assertEqual(state["known_cost"], "0")

        _, recovered = update_window_calibration(
            "secondary", LimitWindow(50, 2000, 300), LimitWindow(51, 3000, 300),
            paid_period, state, "test"
        )
        self.assertEqual(recovered["status"], "estimated")
        self.assertEqual(Decimal(recovered["full_window_usd"]), Decimal("50"))

    def test_idle_sample_after_attributed_change_preserves_accumulation(self):
        pricing = PricingTable(
            revision="test",
            currency="USD",
            unit="per_1m_tokens",
            models={
                "test-model": ModelPricing(
                    model_id="test-model",
                    input_rate=Decimal("500"),
                    output_rate=Decimal("0"),
                    cached_input_rate=Decimal("0"),
                    cache_write_rate=Decimal("500"),
                )
            },
            aliases={},
        )
        paid_period = aggregate_period(
            (HeadroomUsageRecord(1, "test-model", 1000, 0, 0, 0, "paid"),),
            pricing,
            100,
            200,
        )
        empty_period = aggregate_period((), pricing, 200, 300)
        baseline_window = LimitWindow(10, 1000, 10080)
        state, _ = update_window_calibration(
            "secondary", None, baseline_window, empty_period, None, "test"
        )
        state, first_estimate = update_window_calibration(
            "secondary", baseline_window, LimitWindow(12, 1000, 10080), paid_period, state, "test"
        )

        state, idle_estimate = update_window_calibration(
            "secondary", LimitWindow(12, 1000, 10080), LimitWindow(12, 1000, 10080),
            empty_period, state, "test"
        )

        self.assertEqual(first_estimate["status"], "estimated")
        self.assertEqual(idle_estimate["status"], "estimated")
        self.assertEqual(idle_estimate["full_window_usd"], first_estimate["full_window_usd"])
        self.assertEqual(state["anchor"]["used_percent"], 10)
        self.assertEqual(state["known_cost"], first_estimate["known_cost_usd"])
        self.assertEqual(idle_estimate["cumulative_usage"]["requests"], 1)

    def test_unpriced_model_does_not_restart_window_or_drop_priced_cost(self):
        # Regression for the 2026-09-11 weekly split: 85% -> 87% (+2 points)
        # with 65 priced + 1 gpt-reserve row restarted the whole window,
        # discarding $1.58 and splitting 9% -> 100% into two cycles.
        pricing = self.simple_pricing(input_rate="1")
        empty = aggregate_period((), pricing, 0, 1)
        anchor = LimitWindow(85, 1000, 10080)
        state, _ = update_window_calibration(
            "secondary", None, anchor, empty, None, "test"
        )
        mixed = aggregate_period(
            (
                HeadroomUsageRecord(1, "test-model", 1_000_000, 0, 0, 1_000_000, "priced"),
                HeadroomUsageRecord(1, "gpt-reserve", 500, 0, 0, 500, "reserve"),
            ),
            pricing,
            1,
            2,
        )
        self.assertFalse(mixed["models"]["gpt-reserve"]["complete"])
        state, estimate = update_window_calibration(
            "secondary", anchor, LimitWindow(87, 1000, 10080), mixed, state, "test"
        )
        # Anchor preserved: still one continuous cycle, not 87% -> 87%.
        self.assertEqual(state["anchor"]["used_percent"], 85)
        self.assertEqual(len(state["cycle_history"]), 0)
        # Priced $1.00 accumulated; unpriced row kept for diagnostics at $0.
        self.assertEqual(Decimal(state["known_cost"]), Decimal("1"))
        self.assertTrue(state["has_unpriced"])
        # 2-point jump creates one crossing with the skip recorded.
        self.assertEqual(
            [(row["percent"], row.get("skipped_from")) for row in state["crossings"]],
            [(85, None), (87, 85)],
        )
        self.assertEqual(state["crossings"][-1]["skipped_points"], [86])
        self.assertTrue(state["crossings"][-1].get("has_unpriced"))
        # Full estimate uses the 2-point delta: 1 / 2 * 100 = 50.
        self.assertEqual(Decimal(estimate["full_window_usd"]), Decimal("50"))
        self.assertEqual(
            estimate["full_observed_estimate"]["observed_range"], "85% -> 87%"
        )
        self.assert_displayed_estimates_reconcile(estimate)

    def test_two_point_jump_without_unpriced_models_calculates(self):
        pricing = self.simple_pricing(input_rate="1")
        empty = aggregate_period((), pricing, 0, 1)
        anchor = LimitWindow(85, 1000, 10080)
        state, _ = update_window_calibration(
            "secondary", None, anchor, empty, None, "test"
        )
        paid = self.priced_period(pricing, 1, input_tokens=2_000_000)
        state, estimate = update_window_calibration(
            "secondary", anchor, LimitWindow(87, 1000, 10080), paid, state, "test"
        )
        self.assertEqual(state["anchor"]["used_percent"], 85)
        self.assertEqual(Decimal(state["known_cost"]), Decimal("2"))
        self.assertEqual(Decimal(estimate["full_window_usd"]), Decimal("100"))
        self.assert_displayed_estimates_reconcile(estimate)

    def test_coverage_gap_flags_uncertainty_without_splitting_cycle(self):
        # A log-rotation gap at a flat percentage must not discard history.
        pricing = self.simple_pricing(input_rate="1")
        empty = aggregate_period((), pricing, 0, 1)
        anchor = LimitWindow(9, 1000, 10080)
        state, _ = update_window_calibration(
            "secondary", None, anchor, empty, None, "test"
        )
        paid = self.priced_period(pricing, 1, input_tokens=1_000_000)
        state, _ = update_window_calibration(
            "secondary", anchor, LimitWindow(10, 1000, 10080), paid, state, "test"
        )
        gapped = dict(self.priced_period(pricing, 2, input_tokens=1_000_000))
        gapped["diagnostics"] = dict(gapped.get("diagnostics", {}))
        gapped["diagnostics"]["coverage_gap"] = True
        state, estimate = update_window_calibration(
            "secondary",
            LimitWindow(10, 1000, 10080),
            LimitWindow(10, 1000, 10080),
            gapped,
            state,
            "test",
        )
        self.assertEqual(state["anchor"]["used_percent"], 9)
        self.assertEqual(len(state["cycle_history"]), 0)
        self.assertEqual(Decimal(state["known_cost"]), Decimal("2"))
        self.assertTrue(state["has_coverage_gap"])
        self.assertTrue(state["has_uncertain_attribution"])
        self.assertEqual(estimate["status"], "partial_coverage")
        self.assertIsNotNone(estimate["full_window_usd"])
        self.assert_displayed_estimates_reconcile(estimate)

    def test_last_jump_reports_multi_point_crossing(self):
        pricing = self.simple_pricing(input_rate="1")
        empty = aggregate_period((), pricing, 0, 1)
        anchor = LimitWindow(85, 1000, 10080)
        state, _ = update_window_calibration(
            "secondary", None, anchor, empty, None, "test"
        )
        paid = self.priced_period(pricing, 1, input_tokens=2_000_000)
        state, estimate = update_window_calibration(
            "secondary", anchor, LimitWindow(87, 1000, 10080), paid, state, "test"
        )
        jump = estimate["rolling_estimates"].get("last_jump")
        self.assertIsNotNone(jump)
        self.assertEqual(jump["observed_range"], "85% -> 87%")
        self.assertEqual(jump["completed_percentage_points"], 2)
        self.assertEqual(Decimal(jump["estimate_100_usd"]), Decimal("100"))
        self.assert_displayed_estimates_reconcile(estimate)

    def test_weekly_unobserved_rise_excludes_points_from_projection_without_imputing(self):
        pricing = self.simple_pricing()
        empty = aggregate_period((), pricing, 0, 1)
        previous = LimitWindow(10, 1000, 10080)
        state, _ = update_window_calibration("secondary", None, previous, empty, None, "test")
        paid = self.priced_period(pricing, 1)
        state, _ = update_window_calibration(
            "secondary", previous, LimitWindow(11, 1000, 10080), paid, state, "test"
        )
        zero_token_request = self.priced_period(pricing, 2, input_tokens=0)
        self.assertEqual(zero_token_request["total"]["requests"], 1)
        self.assertEqual(zero_token_request["total"]["input_tokens"], 0)
        state, gap = update_window_calibration(
            "secondary", LimitWindow(11, 1000, 10080),
            LimitWindow(15, 1000, 10080), zero_token_request, state, "test",
            conservative_unobserved=True,
        )
        self.assertEqual(state["known_cost"], "1")
        self.assertEqual(state["anchor"]["used_percent"], 10)
        self.assertFalse(state["has_imputed"])
        self.assertEqual(state["unobserved_percentage_ranges"][0]["from_percent"], 11)
        self.assertEqual(state["unobserved_percentage_ranges"][0]["to_percent"], 15)
        self.assertFalse(state["crossings"][-1]["reliable"])
        self.assertEqual(gap["status"], "unobserved_percentage_change")
        self.assertEqual(gap["counted_percentage_points"], 1)
        self.assertEqual(gap["excluded_no_token_percentage_points"], 4)
        self.assertEqual(Decimal(gap["full_window_usd"]), Decimal("100"))
        state, continued = update_window_calibration(
            "secondary", LimitWindow(15, 1000, 10080),
            LimitWindow(16, 1000, 10080), paid, state, "test",
            conservative_unobserved=True,
        )
        self.assertEqual(state["known_cost"], "2")
        self.assertEqual(Decimal(continued["full_window_usd"]), Decimal("100"))
        self.assertEqual(continued["counted_percentage_points"], 2)
        self.assertEqual(continued["excluded_no_token_percentage_points"], 4)
        closed, _ = update_window_calibration(
            "secondary", LimitWindow(16, 1000, 10080),
            LimitWindow(0, 2000, 10080), empty, state, "test",
            conservative_unobserved=True,
        )
        self.assertEqual(closed["cycle_history"][0]["known_cost_usd"], "2")
        self.assertEqual(closed["cycle_history"][0]["observed_percent"], "10% -> 16%")
        self.assertEqual(len(closed["cycle_history"][0]["unobserved_percentage_ranges"]), 1)
        self.assertEqual(
            closed["cycle_history"][0]["model_attribution"]["test-model"]["cost_usd"], "2"
        )

    def test_multi_point_jump_with_local_calls_is_not_verified_per_point(self):
        pricing = self.simple_pricing()
        empty = aggregate_period((), pricing, 0, 1)
        previous = LimitWindow(54, 1000, 10080)
        state, _ = update_window_calibration("secondary", None, previous, empty, None, "test")
        tiny = self.priced_period(pricing, 1, input_tokens=1_000)
        state, estimate = update_window_calibration(
            "secondary", previous, LimitWindow(61, 1000, 10080), tiny, state, "test"
        )
        self.assertEqual(state["crossings"][-1]["skipped_points"], list(range(55, 61)))
        self.assertEqual(state["crossings"][-1]["reason"], "multi_point_interval_unresolved")
        self.assertEqual(estimate["exact_cost_usd"], "0.001")
        self.assertEqual(estimate["counted_percentage_points"], 7)
        self.assertEqual(estimate["excluded_no_token_percentage_points"], 0)
        self.assertEqual(Decimal(estimate["full_window_usd"]), Decimal("0.001") * 100 / 7)
        self.assertTrue(state["has_uncertain_attribution"])
        self.assertFalse(state["has_imputed"])

    def test_external_usage_imputed_from_window_mean_mix(self):
        # Outside-VM spend: the account percentage rises with no local
        # Headroom rows.  With >= 2 completed points of priced history the
        # missing points are imputed from the window's own mean instead of
        # restarting the cycle.
        pricing = self.simple_pricing(input_rate="1")
        empty = aggregate_period((), pricing, 0, 1)
        anchor = LimitWindow(10, 1000, 10080)
        state, _ = update_window_calibration(
            "secondary", None, anchor, empty, None, "test"
        )
        previous = anchor
        for percent in (11, 12):
            paid = self.priced_period(pricing, percent, input_tokens=1_000_000)
            state, _ = update_window_calibration(
                "secondary", previous, LimitWindow(percent, 1000, 10080),
                paid, state, "test",
            )
            previous = LimitWindow(percent, 1000, 10080)
        state, estimate = update_window_calibration(
            "secondary", previous, LimitWindow(14, 1000, 10080), empty, state, "test"
        )
        # No restart: one continuous 10% -> 14% cycle.
        self.assertEqual(state["anchor"]["used_percent"], 10)
        self.assertEqual(len(state["cycle_history"]), 0)
        # Mean is $1.00/pt over 2 completed points; the 2 unattributed points
        # impute $2.00 on top of $2.00 of exact local cost.
        self.assertEqual(Decimal(state["known_cost"]), Decimal("4"))
        self.assertEqual(Decimal(estimate["known_cost_usd"]), Decimal("4"))
        self.assertEqual(Decimal(estimate["exact_cost_usd"]), Decimal("2"))
        self.assertEqual(Decimal(estimate["imputed_cost_usd"]), Decimal("2"))
        self.assertTrue(state["has_imputed"])
        self.assertTrue(state["has_uncertain_attribution"])
        self.assertEqual(estimate["status"], "imputed_external_usage")
        self.assertIn("imputed_external_usage", estimate["confidence_reasons"])
        # Imputed amount lives in its own bucket, never in a real model.
        self.assertEqual(
            Decimal(state["costs_by_model"]["external-imputed"]), Decimal("2")
        )
        self.assertEqual(
            Decimal(state["costs_by_model"]["test-model"]), Decimal("2")
        )
        imputed_usage = state["usage_by_model"]["external-imputed"]
        self.assertEqual(imputed_usage["input_tokens"], 2_000_000)
        self.assertEqual(imputed_usage["requests"], 2)
        # Crossing recorded with the skip and the imputation flag.
        self.assertEqual(
            [(row["percent"], bool(row.get("imputed"))) for row in state["crossings"]],
            [(10, False), (11, False), (12, False), (14, True)],
        )
        self.assertEqual(state["crossings"][-1]["skipped_points"], [13])
        # Full estimate uses the whole delta: 4 / 4 * 100 = 100.
        self.assertEqual(Decimal(estimate["full_window_usd"]), Decimal("100"))
        last_jump = estimate["rolling_estimates"].get("last_jump")
        self.assertIsNotNone(last_jump)
        self.assertEqual(last_jump["observed_range"], "12% -> 14%")
        self.assertTrue(last_jump["has_imputed"])
        self.assert_displayed_estimates_reconcile(estimate)

    def test_imputed_tokens_preserve_cached_and_output_mix(self):
        # The mean in/cached/out split comes from local history: the Codex
        # rate-limit call reports percentages only, never token mix.
        pricing = self.simple_pricing(
            input_rate="1", output_rate="2", cached_rate="0.5"
        )
        empty = aggregate_period((), pricing, 0, 1)
        anchor = LimitWindow(10, 1000, 10080)
        state, _ = update_window_calibration(
            "secondary", None, anchor, empty, None, "test"
        )
        previous = anchor
        for percent in (11, 12):
            paid = self.priced_period(
                pricing, percent,
                input_tokens=1_000_000, cached=400_000, output=50_000,
            )
            state, _ = update_window_calibration(
                "secondary", previous, LimitWindow(percent, 1000, 10080),
                paid, state, "test",
            )
            previous = LimitWindow(percent, 1000, 10080)
        # Per point: (600000*1 + 400000*0.5 + 50000*2)/1M = $0.90.
        self.assertEqual(Decimal(state["known_cost"]), Decimal("1.80"))
        state, estimate = update_window_calibration(
            "secondary", previous, LimitWindow(13, 1000, 10080), empty, state, "test"
        )
        self.assertEqual(Decimal(state["known_cost"]), Decimal("2.70"))
        self.assertEqual(Decimal(estimate["imputed_cost_usd"]), Decimal("0.90"))
        imputed_usage = state["usage_by_model"]["external-imputed"]
        self.assertEqual(imputed_usage["input_tokens"], 1_000_000)
        self.assertEqual(imputed_usage["cached_input_tokens"], 400_000)
        self.assertEqual(imputed_usage["uncached_input_tokens"], 600_000)
        self.assertEqual(imputed_usage["output_tokens"], 50_000)
        self.assertEqual(Decimal(estimate["full_window_usd"]), Decimal("90"))
        strict_last_1 = estimate["rolling_estimates"].get("1")
        self.assertIsNotNone(strict_last_1)
        self.assertTrue(strict_last_1["has_imputed"])
        self.assert_displayed_estimates_reconcile(estimate)

    def test_external_jump_without_history_still_restarts(self):
        # One completed point is not enough basis: no numbers are invented.
        pricing = self.simple_pricing(input_rate="1")
        empty = aggregate_period((), pricing, 0, 1)
        anchor = LimitWindow(10, 1000, 10080)
        state, _ = update_window_calibration(
            "secondary", None, anchor, empty, None, "test"
        )
        paid = self.priced_period(pricing, 1, input_tokens=1_000_000)
        state, _ = update_window_calibration(
            "secondary", anchor, LimitWindow(11, 1000, 10080), paid, state, "test"
        )
        state, restarted = update_window_calibration(
            "secondary", LimitWindow(11, 1000, 10080), LimitWindow(13, 1000, 10080),
            empty, state, "test",
        )
        self.assertEqual(restarted["status"], "unattributed_usage")
        self.assertIsNone(restarted["full_window_usd"])
        self.assertEqual(state["anchor"]["used_percent"], 13)
        self.assertEqual(state["known_cost"], "0")
        self.assertFalse(state.get("has_imputed", False))

    def test_external_imputation_can_be_disabled(self):
        pricing = self.simple_pricing(input_rate="1")
        empty = aggregate_period((), pricing, 0, 1)
        anchor = LimitWindow(10, 1000, 10080)
        state, _ = update_window_calibration(
            "secondary", None, anchor, empty, None, "test"
        )
        previous = anchor
        for percent in (11, 12):
            paid = self.priced_period(pricing, percent, input_tokens=1_000_000)
            state, _ = update_window_calibration(
                "secondary", previous, LimitWindow(percent, 1000, 10080),
                paid, state, "test",
            )
            previous = LimitWindow(percent, 1000, 10080)
        previous_env = os.environ.get("EXTERNAL_IMPUTATION")
        os.environ["EXTERNAL_IMPUTATION"] = "0"
        try:
            state, restarted = update_window_calibration(
                "secondary", previous, LimitWindow(14, 1000, 10080),
                empty, state, "test",
            )
        finally:
            if previous_env is None:
                os.environ.pop("EXTERNAL_IMPUTATION", None)
            else:
                os.environ["EXTERNAL_IMPUTATION"] = previous_env
        self.assertEqual(restarted["status"], "unattributed_usage")
        self.assertEqual(state["anchor"]["used_percent"], 14)

    def test_usage_v2_is_appended_after_legacy_prefix(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "usage.log"
            log_usage(
                path,
                snap(p_used=42),
                {
                    "schema_version": 2,
                    "period_id": "period-1",
                    "total": {"estimated_cost": "1.25"},
                    "windows": {
                        "primary": {"full_window_usd": "5.00"},
                        "secondary": {"full_window_usd": "12.50"},
                    },
                    "weekly_estimate": {
                        "full_window_usd": "12.50",
                        "counted_percentage_points": 8,
                        "excluded_no_token_percentage_points": 4,
                        "full_observed_estimate": {
                            "calculation_basis": "priced_cost_over_token_backed_percentage_points"
                        },
                    },
                },
            )
            line = path.read_text(encoding="utf-8").strip()
            self.assertTrue(line.startswith("1970-01-01"))
            self.assertIn("weekly_100pct_usd=12.50", line)
            self.assertIn("weekly_100pct_counted_points=8", line)
            self.assertIn("weekly_100pct_excluded_no_token_points=4", line)
            self.assertNotIn("primary_100pct_usd=", line)
            payload = json.loads(line.split(" usage_v2=", 1)[1])
            self.assertEqual(payload["period_id"], "period-1")
            self.assertEqual(payload["weekly_estimate"]["full_window_usd"], "12.50")


class Gpt6FamilyPricingTests(unittest.TestCase):
    """Official GPT-6 Standard-processing rates for the shipped example file.

    Sources (verified 2026-09-23):
      https://developers.openai.com/api/docs/models/gpt-6-astra
      https://developers.openai.com/api/docs/models/gpt-6-sol
      https://developers.openai.com/api/docs/models/gpt-6-luna
    """

    @classmethod
    def setUpClass(cls):
        example = Path(__file__).resolve().parents[1] / "model-pricing.json.example"
        cls.pricing = load_model_pricing(example)

    def test_gpt6_rates_match_official_standard_processing_prices(self):
        expected = {
            # model: (input, cached input, output) per 1M tokens, Standard tier
            "gpt-6-astra": ("10.00", "1.00", "50.00"),
            "gpt-6-sol": ("2.00", "0.20", "10.00"),
            "gpt-6-luna": ("0.10", "0.01", "0.50"),
        }
        for model, (input_rate, cached_rate, output_rate) in expected.items():
            row = self.pricing.resolve(model)
            self.assertIsNotNone(row, f"{model} is missing from the shipped pricing example")
            self.assertEqual(row.input_rate, Decimal(input_rate))
            self.assertEqual(row.cached_input_rate, Decimal(cached_rate))
            self.assertEqual(row.output_rate, Decimal(output_rate))
            self.assertEqual(row.long_context_threshold_tokens, 272_000)
            self.assertEqual(row.long_context_input_multiplier, Decimal("2"))
            self.assertEqual(row.long_context_output_multiplier, Decimal("1.5"))
            self.assertEqual(row.processing_multiplier("standard"), Decimal("1"))
            self.assertEqual(row.processing_multiplier("fast"), Decimal("2"))
            self.assertEqual(row.processing_multiplier("priority"), Decimal("2"))

    def test_gpt6_luna_request_is_priced_instead_of_unpriced(self):
        record = HeadroomUsageRecord(0.0, "gpt-6-luna", 21008, 210, 10752, 10246, "r1")
        breakdown = price_usage_record(record, self.pricing)
        self.assertTrue(breakdown.complete, breakdown.reason)
        # 10256 uncached * $0.10/M + 10752 cached * $0.01/M + 210 output * $0.50/M
        self.assertEqual(breakdown.total_cost, Decimal("0.00123812"))
        period = aggregate_period((record,), self.pricing, 0, 1)
        self.assertTrue(period["total"]["complete"])
        self.assertIsNone(period["total"]["reason"])
        self.assertEqual(period["models"]["gpt-6-luna"]["reasons"], [])

    def test_gpt6_sol_request_is_priced_instead_of_unpriced(self):
        record = HeadroomUsageRecord(0.0, "gpt-6-sol", 21008, 210, 10752, 10246, "r1")
        breakdown = price_usage_record(record, self.pricing)
        self.assertTrue(breakdown.complete, breakdown.reason)
        # 10256 uncached * $2.00/M + 10752 cached * $0.20/M + 210 output * $10.00/M
        self.assertEqual(breakdown.total_cost, Decimal("0.02476240"))

    def test_gpt6_long_context_threshold_is_applied_per_request(self):
        short = HeadroomUsageRecord(0.0, "gpt-6-sol", 272_000, 1_000, 0, 0, "short")
        long = HeadroomUsageRecord(0.0, "gpt-6-sol", 272_001, 1_000, 0, 0, "long")
        short_breakdown = price_usage_record(short, self.pricing)
        long_breakdown = price_usage_record(long, self.pricing)
        self.assertFalse(short_breakdown.long_context)
        self.assertTrue(long_breakdown.long_context)
        self.assertEqual(short_breakdown.total_cost, Decimal("0.554"))
        # 272001 * ($2.00 * 2) / 1M + 1000 * ($10.00 * 1.5) / 1M
        self.assertEqual(long_breakdown.total_cost, Decimal("1.103004"))

    def test_gpt6_fast_mode_is_twice_standard(self):
        record = HeadroomUsageRecord(0.0, "gpt-6-luna", 21008, 210, 10752, 10246, "r1")
        standard = price_usage_record(record, self.pricing)
        fast = price_usage_record(record, self.pricing, processing_tier="fast")
        self.assertEqual(fast.total_cost, standard.total_cost * 2)


class ExternalEvidenceTests(unittest.TestCase):
    def setUp(self):
        self._previous_env = os.environ.get("EXTERNAL_EVIDENCE")
        os.environ["EXTERNAL_EVIDENCE"] = "1"

    def tearDown(self):
        if self._previous_env is None:
            os.environ.pop("EXTERNAL_EVIDENCE", None)
        else:
            os.environ["EXTERNAL_EVIDENCE"] = self._previous_env

    @staticmethod
    def usage_payload(lifetime):
        return {
            "summary": {"lifetimeTokens": lifetime},
            "dailyUsageBuckets": [{"startDate": "2026-09-13", "tokens": lifetime}],
            "threadUsage": None,
        }

    @staticmethod
    def empty_period():
        return {
            "total": {"requests": 0, "input_tokens": 0, "output_tokens": 0},
            "diagnostics": {},
        }

    def test_external_tokens_detected_from_lifetime_delta(self):
        previous = {"account_lifetime_tokens": 1_000}
        evidence = update_external_evidence(
            previous, self.usage_payload(1_500), self.empty_period(), 0, 600
        )
        self.assertTrue(evidence["available"])
        self.assertEqual(evidence["account_lifetime_tokens"], 1_500)
        self.assertEqual(evidence["account_lifetime_delta"], 500)
        self.assertEqual(evidence["external_tokens_estimate"], 500)
        self.assertTrue(evidence["external_detected"])
        self.assertEqual(
            evidence["account_daily_buckets"],
            [{"startDate": "2026-09-13", "tokens": 1_500}],
        )
        self.assertFalse(evidence["account_calls_available"])
        self.assertEqual(evidence["reason"], "ok")

    def test_local_tokens_offset_account_delta(self):
        previous = {"account_lifetime_tokens": 1_000}
        period = {
            "total": {"requests": 3, "input_tokens": 1_000, "output_tokens": 200},
            "diagnostics": {},
        }
        evidence = update_external_evidence(
            previous, self.usage_payload(2_500), period, 0, 600
        )
        self.assertEqual(evidence["local_period_tokens"], 1_200)
        self.assertEqual(evidence["local_period_requests"], 3)
        self.assertEqual(evidence["external_tokens_estimate"], 300)
        self.assertTrue(evidence["external_detected"])

    def test_no_external_when_delta_matches_local(self):
        previous = {"account_lifetime_tokens": 1_000}
        period = {
            "total": {"requests": 1, "input_tokens": 400, "output_tokens": 100},
            "diagnostics": {},
        }
        evidence = update_external_evidence(
            previous, self.usage_payload(1_500), period, 0, 600
        )
        self.assertEqual(evidence["external_tokens_estimate"], 0)
        self.assertFalse(evidence["external_detected"])

    def test_zero_delta_with_zero_local_is_clean(self):
        evidence = update_external_evidence(
            {"account_lifetime_tokens": 1_000},
            self.usage_payload(1_000),
            self.empty_period(),
            0,
            600,
        )
        self.assertTrue(evidence["available"])
        self.assertEqual(evidence["external_tokens_estimate"], 0)
        self.assertFalse(evidence["external_detected"])
        self.assertEqual(evidence["reason"], "ok")

    def test_delta_below_local_spend_is_stale_not_zero(self):
        previous = {"account_lifetime_tokens": 1_000}
        period = {
            "total": {"requests": 3, "input_tokens": 1_000, "output_tokens": 200},
            "diagnostics": {},
        }
        evidence = update_external_evidence(
            previous, self.usage_payload(1_000), period, 0, 600
        )
        self.assertTrue(evidence["available"])
        self.assertEqual(evidence["account_lifetime_delta"], 0)
        self.assertIsNone(evidence["external_tokens_estimate"])
        self.assertFalse(evidence["external_detected"])
        self.assertEqual(evidence["reason"], "stale_counter")

    def test_partial_delta_below_local_spend_is_stale(self):
        previous = {"account_lifetime_tokens": 1_000}
        period = {
            "total": {"requests": 3, "input_tokens": 1_000, "output_tokens": 200},
            "diagnostics": {},
        }
        evidence = update_external_evidence(
            previous, self.usage_payload(1_500), period, 0, 600
        )
        self.assertEqual(evidence["account_lifetime_delta"], 500)
        self.assertIsNone(evidence["external_tokens_estimate"])
        self.assertEqual(evidence["reason"], "stale_counter")

    def test_supra_physical_delta_is_catchup_not_spike(self):
        evidence = update_external_evidence(
            {"account_lifetime_tokens": 0},
            self.usage_payload(200_000_000),
            self.empty_period(),
            0,
            600,
        )
        self.assertTrue(evidence["available"])
        self.assertEqual(evidence["account_lifetime_delta"], 200_000_000)
        self.assertIsNone(evidence["external_tokens_estimate"])
        self.assertFalse(evidence["external_detected"])
        self.assertEqual(evidence["reason"], "counter_catchup")

    def test_catchup_boundary_is_inclusive(self):
        evidence = update_external_evidence(
            {"account_lifetime_tokens": 0},
            self.usage_payload(1_000_000),
            self.empty_period(),
            0,
            10,
        )
        self.assertEqual(evidence["reason"], "ok")
        self.assertEqual(evidence["external_tokens_estimate"], 1_000_000)

    def test_first_sample_stores_counter_without_delta(self):
        evidence = update_external_evidence(
            None, self.usage_payload(1_500), self.empty_period(), 0, 600
        )
        self.assertTrue(evidence["available"])
        self.assertIsNone(evidence["account_lifetime_delta"])
        self.assertIsNone(evidence["external_tokens_estimate"])
        self.assertFalse(evidence["external_detected"])
        self.assertEqual(evidence["reason"], "awaiting_next_sample")

    def test_missing_usage_payload_is_unavailable(self):
        evidence = update_external_evidence(
            {"account_lifetime_tokens": 1_000}, None, self.empty_period(), 0, 600
        )
        self.assertFalse(evidence["available"])
        self.assertEqual(evidence["reason"], "account_usage_unavailable")

    def test_negative_delta_is_unavailable(self):
        evidence = update_external_evidence(
            {"account_lifetime_tokens": 2_000},
            self.usage_payload(1_500),
            self.empty_period(),
            0,
            600,
        )
        self.assertFalse(evidence["available"])
        self.assertEqual(evidence["reason"], "lifetime_counter_decreased")

    def test_disabled_evidence_records_reason(self):
        os.environ["EXTERNAL_EVIDENCE"] = "0"
        evidence = update_external_evidence(
            {"account_lifetime_tokens": 1_000},
            self.usage_payload(1_500),
            self.empty_period(),
            0,
            600,
        )
        self.assertFalse(evidence["available"])
        self.assertEqual(evidence["reason"], "disabled")

    def test_coverage_gap_is_flagged(self):
        previous = {"account_lifetime_tokens": 1_000}
        period = {
            "total": {"requests": 0, "input_tokens": 0, "output_tokens": 0},
            "diagnostics": {"coverage_gap": True},
        }
        evidence = update_external_evidence(
            previous, self.usage_payload(1_500), period, 0, 600
        )
        self.assertTrue(evidence["coverage_gap"])
        self.assertEqual(evidence["reason"], "coverage_gap_local_sum_incomplete")

    def test_evidence_key_never_changes_window_estimates(self):
        monitor_module = __import__("monitor")
        pricing = monitor_module.load_model_pricing(
            Path(__file__).resolve().parents[1] / "model-pricing.json.example"
        )
        previous = snap(p_used=10, s_used=20)
        current = snap(p_used=10, s_used=20)
        period = aggregate_period((), pricing, 900, 1500, {})
        plain = monitor_module.empty_accounting_state()
        seeded = monitor_module.empty_accounting_state()
        seeded["external_evidence"] = {"account_lifetime_tokens": 5_000}
        plain_state, plain_estimates = update_accounting_state(
            plain, previous, current, period, pricing, monitor_module._empty_cursor()
        )
        seeded_state, seeded_estimates = update_accounting_state(
            seeded, previous, current, period, pricing, monitor_module._empty_cursor()
        )
        self.assertEqual(seeded_estimates, plain_estimates)
        self.assertEqual(
            seeded_state["external_evidence"], {"account_lifetime_tokens": 5_000}
        )
        self.assertNotIn("external_evidence", plain_state["windows"])


class NonCodexModelFilterTests(unittest.TestCase):
    """Rows from other providers share the proxy but must not reach accounting."""

    def setUp(self):
        self._previous = os.environ.get("HEADROOM_EXCLUDED_MODELS")

    def tearDown(self):
        if self._previous is None:
            os.environ.pop("HEADROOM_EXCLUDED_MODELS", None)
        else:
            os.environ["HEADROOM_EXCLUDED_MODELS"] = self._previous

    @staticmethod
    def _line(request_id: str, model: str) -> str:
        return (
            "2026-09-08 12:00:01,000 - headroom.proxy - INFO - [%s] PERF "
            "model=%s tok_after=100 tok_out=10 cache_read=60 cache_write=40\n"
            % (request_id, model)
        )

    def _collect(self, log_dir, **kwargs):
        return collect_headroom_usage(
            log_dir, 0, 2_000_000_000, {}, client="codex", timezone_name="UTC", **kwargs
        )

    def test_default_exclusion_list_covers_known_third_party_models(self):
        os.environ.pop("HEADROOM_EXCLUDED_MODELS", None)
        self.assertEqual(
            headroom_excluded_models(),
            frozenset(
                {
                    "deepseek-v4.1-flash",
                    "deepseek-v4-flash",
                    "kimi-k2.6",
                    "muse-spark-1.3-contributor",
                    "gemini-3.8-flash-high",
                }
            ),
        )

    def test_empty_setting_disables_exclusion(self):
        os.environ["HEADROOM_EXCLUDED_MODELS"] = ""
        self.assertEqual(headroom_excluded_models(), frozenset())

    def test_custom_setting_is_trimmed_and_case_insensitive(self):
        os.environ["HEADROOM_EXCLUDED_MODELS"] = " Foo , BAR ,, baz "
        self.assertEqual(headroom_excluded_models(), frozenset({"foo", "bar", "baz"}))

    def test_non_codex_rows_are_dropped_and_counted_separately(self):
        os.environ.pop("HEADROOM_EXCLUDED_MODELS", None)
        with tempfile.TemporaryDirectory() as directory:
            log_dir = Path(directory)
            (log_dir / "proxy-8787.log").write_text(
                self._line("hr_codex", "gpt-6-luna")
                + self._line("hr_deepseek", "deepseek-v4.1-flash")
                + self._line("hr_kimi", "kimi-k2.6")
                + self._line("hr_sol", "gpt-6-sol"),
                encoding="utf-8",
            )
            records, _, diagnostics = self._collect(log_dir)
            self.assertEqual(
                [record.request_id for record in records], ["hr_codex", "hr_sol"]
            )
            self.assertEqual(diagnostics["records_read"], 4)
            self.assertEqual(diagnostics["accepted"], 2)
            self.assertEqual(diagnostics["excluded_models"], 2)
            self.assertEqual(diagnostics["excluded"], 2)
            self.assertEqual(
                diagnostics["excluded_model_names"],
                sorted(headroom_excluded_models()),
            )

    def test_unknown_model_still_reaches_aggregation_as_unpriced(self):
        # A brand-new Codex model must keep the ``unpriced_model`` safety net:
        # an allowlist would silently drop its spend instead of flagging it.
        os.environ["HEADROOM_EXCLUDED_MODELS"] = "deepseek-v4.1-flash"
        with tempfile.TemporaryDirectory() as directory:
            log_dir = Path(directory)
            (log_dir / "proxy.log").write_text(
                self._line("hr_new", "gpt-7-vega"), encoding="utf-8"
            )
            records, _, diagnostics = self._collect(log_dir)
            self.assertEqual([record.model for record in records], ["gpt-7-vega"])
            self.assertEqual(diagnostics["excluded_models"], 0)

    def test_excluded_models_do_not_inflate_outside_vm_evidence(self):
        # ``update_external_evidence`` compares the local token sum with the
        # Codex account counter, so third-party rows must not be added to it.
        os.environ.pop("HEADROOM_EXCLUDED_MODELS", None)
        pricing = PricingTable(
            revision="test",
            currency="USD",
            unit="per_1m_tokens",
            models={
                "gpt-6-luna": ModelPricing(
                    model_id="gpt-6-luna",
                    input_rate=Decimal("0.10"),
                    output_rate=Decimal("0.50"),
                    cached_input_rate=Decimal("0.01"),
                    cache_write_rate=Decimal("0"),
                )
            },
            aliases={},
        )
        with tempfile.TemporaryDirectory() as directory:
            log_dir = Path(directory)
            (log_dir / "proxy.log").write_text(
                self._line("hr_codex", "gpt-6-luna")
                + self._line("hr_deepseek", "deepseek-v4.1-flash"),
                encoding="utf-8",
            )
            records, _, diagnostics = self._collect(log_dir)
            period = aggregate_period(records, pricing, 0, 2_000_000_000, diagnostics)
            self.assertTrue(period["total"]["complete"])
            self.assertEqual(period["total"]["requests"], 1)
            evidence = update_external_evidence(
                {"account_lifetime_tokens": 1_000},
                {"summary": {"lifetimeTokens": 1_200}},
                period,
                0,
                1,
            )
            # Only the Codex row's 100 input + 10 output tokens are local.
            self.assertEqual(evidence["local_period_tokens"], 110)
            self.assertEqual(evidence["local_period_requests"], 1)
            self.assertEqual(evidence["reason"], "ok")
            # 200 account tokens - 110 local tokens = 90 genuinely off-VM.
            self.assertEqual(evidence["external_tokens_estimate"], 90)

    def test_excluded_models_do_not_flip_evidence_to_stale_counter(self):
        # With the third-party rows counted, the local sum (220) would exceed
        # the account delta (200) and the sample would be discarded as
        # ``stale_counter`` instead of reporting real off-VM spend.
        os.environ.pop("HEADROOM_EXCLUDED_MODELS", None)
        pricing = PricingTable(
            revision="test",
            currency="USD",
            unit="per_1m_tokens",
            models={
                "gpt-6-luna": ModelPricing(
                    model_id="gpt-6-luna",
                    input_rate=Decimal("0.10"),
                    output_rate=Decimal("0.50"),
                    cached_input_rate=Decimal("0.01"),
                    cache_write_rate=Decimal("0"),
                )
            },
            aliases={},
        )
        with tempfile.TemporaryDirectory() as directory:
            log_dir = Path(directory)
            (log_dir / "proxy.log").write_text(
                self._line("hr_codex", "gpt-6-luna")
                + self._line("hr_deepseek", "deepseek-v4.1-flash"),
                encoding="utf-8",
            )
            account = {"summary": {"lifetimeTokens": 1_200}}
            previous = {"account_lifetime_tokens": 1_000}
            filtered, _, diagnostics = self._collect(log_dir)
            kept = update_external_evidence(
                previous, account, aggregate_period(filtered, pricing, 0, 1, diagnostics), 0, 1
            )
            self.assertEqual(kept["reason"], "ok")

            os.environ["HEADROOM_EXCLUDED_MODELS"] = ""
            unfiltered, _, diagnostics = self._collect(log_dir)
            self.assertEqual(len(unfiltered), 2)
            dropped = update_external_evidence(
                previous, account, aggregate_period(unfiltered, pricing, 0, 1, diagnostics), 0, 1
            )
            self.assertEqual(dropped["reason"], "stale_counter")

    def test_filter_applies_when_client_tag_is_absent(self):
        os.environ.pop("HEADROOM_EXCLUDED_MODELS", None)
        with tempfile.TemporaryDirectory() as directory:
            log_dir = Path(directory)
            (log_dir / "proxy.log").write_text(
                self._line("hr_pi", "deepseek-v4-flash"), encoding="utf-8"
            )
            records, _, diagnostics = self._collect(log_dir)
            self.assertEqual(records, [])
            self.assertEqual(diagnostics["missing_client"], 1)
            self.assertEqual(diagnostics["excluded_models"], 1)


if __name__ == "__main__":
    unittest.main()
