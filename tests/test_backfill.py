import json
import os
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import backfill
from monitor import HeadroomUsageRecord, ModelPricing, PricingTable


def pricing() -> PricingTable:
    return PricingTable(
        revision="test",
        currency="USD",
        unit="per_1m_tokens",
        models={
            "test-model": ModelPricing(
                model_id="test-model",
                input_rate=Decimal("1"),
                output_rate=Decimal("2"),
                cached_input_rate=Decimal("0.1"),
                cache_write_rate=Decimal("0"),
                long_context_threshold_tokens=272000,
                long_context_input_multiplier=Decimal("2"),
                long_context_output_multiplier=Decimal("1.5"),
            )
        },
        aliases={},
    )


def record(timestamp: float, tokens: int, request_id: str) -> HeadroomUsageRecord:
    return HeadroomUsageRecord(
        timestamp=timestamp,
        model="test-model",
        input_tokens=tokens,
        output_tokens=0,
        cache_read_tokens=0,
        raw_cache_write_tokens=0,
        request_id=request_id,
    )


def checkpoint(line_number: int, timestamp: str, secondary: float) -> backfill.Checkpoint:
    line = (
        f"{timestamp} UTC primary=0.0% secondary={secondary}% balance=0 "
        "weekly_100pct_usd=-"
    )
    return backfill.parse_checkpoint_line(line, line_number)


class BackfillTests(unittest.TestCase):
    def setUp(self):
        self._env = os.environ.copy()

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)

    def test_scoped_replay_preserves_pre_restart_and_accumulates_bucket_cost(self):
        start = backfill.parse_timestamp("2026-09-09T17:41:42Z")
        checkpoints = [
            checkpoint(0, "2026-09-09 17:30:42", 8),
            checkpoint(1, "2026-09-09 17:41:42", 9),
            checkpoint(2, "2026-09-09 17:51:42", 10),
            checkpoint(3, "2026-09-09 18:01:42", 10),
        ]
        records = [record(start + 60, 1_000_000, "one"), record(start + 700, 500_000, "two")]

        result = backfill.replay_history(
            checkpoints,
            records,
            pricing(),
            raw_diagnostics={"coverage_gap": False},
            scope_start=start,
        )

        self.assertEqual(set(result["payloads"]), {1, 2, 3})
        self.assertEqual(result["first_exact_checkpoint"], start)
        final = result["payloads"][3]["windows"]["secondary"]
        self.assertEqual(final["known_cost_usd"], "3.0")
        self.assertEqual(final["full_window_usd"], "300.0")
        self.assertEqual(final["cumulative_usage"]["requests"], 2)
        self.assertEqual(result["states"]["secondary"]["anchor"]["used_percent"], 9)

    def test_reset_closes_post_restart_cycle_without_merging_costs(self):
        start = backfill.parse_timestamp("2026-09-09T17:41:42Z")
        checkpoints = [
            checkpoint(0, "2026-09-09 17:41:42", 9),
            checkpoint(1, "2026-09-09 17:51:42", 10),
            checkpoint(2, "2026-09-09 18:01:42", 1),
            checkpoint(3, "2026-09-09 18:11:42", 2),
        ]
        records = [record(start + 60, 1_000_000, "one"), record(start + 1_210, 2_000_000, "two")]

        result = backfill.replay_history(
            checkpoints,
            records,
            pricing(),
            raw_diagnostics={"coverage_gap": False},
            scope_start=start,
        )
        secondary = [row for row in result["cycles"] if row["window"] == "secondary"]
        self.assertEqual(len(secondary), 2)
        self.assertEqual(secondary[0]["observed_range"], "9.0% -> 10.0%")
        self.assertEqual(secondary[0]["corrected_cost_usd"], "2.0")
        self.assertEqual(secondary[1]["observed_range"], "1.0% -> 2.0%")
        self.assertEqual(secondary[1]["corrected_cost_usd"], "4.0")

    def test_no_post_restart_requests_are_unavailable(self):
        start = backfill.parse_timestamp("2026-09-09T17:41:42Z")
        checkpoints = [
            checkpoint(0, "2026-09-09 17:30:42", 8),
            checkpoint(1, "2026-09-09 17:41:42", 9),
            checkpoint(2, "2026-09-09 17:51:42", 10),
        ]
        result = backfill.replay_history(
            checkpoints,
            [],
            pricing(),
            raw_diagnostics={"coverage_gap": True},
            scope_start=start,
        )
        self.assertEqual(set(result["payloads"]), {1, 2})
        self.assertEqual(
            result["payloads"][2]["windows"]["secondary"]["status"], "unavailable"
        )
        self.assertEqual(result["payloads"][2]["windows"]["secondary"]["full_window_usd"], None)

    def test_rewrite_keeps_pre_scope_bytes_exactly(self):
        start = backfill.parse_timestamp("2026-09-09T17:41:42Z")
        before = (
            "2026-09-09 17:30:42 UTC primary=0.0% secondary=8.0% balance=0 "
            "old_payload=untouched\n"
        )
        after = (
            "2026-09-09 17:41:42 UTC primary=0.0% secondary=9.0% balance=0\n"
        )
        after_checkpoint = backfill.parse_checkpoint_line(after.rstrip("\n"), 1)
        payload = {"weekly_estimate": {"full_window_usd": "10", "confidence": "Low"}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "usage.log"
            path.write_text(before + after, encoding="utf-8")
            backup, rewritten, first_line = backfill.rewrite_usage_log(
                path, [after_checkpoint], {1: payload}
            )
            self.assertEqual(rewritten, 1)
            self.assertEqual(first_line, 1)
            self.assertEqual(path.read_bytes().splitlines(keepends=True)[0], before.encode())
            self.assertIn(b"weekly_100pct_usd=10", path.read_bytes())
            self.assertTrue(backup and backup.exists())

    def test_long_context_count_uses_individual_request_boundary(self):
        counts = backfill._long_context_counts(
            [
                record(1, 271_999, "a"),
                record(2, 272_000, "b"),
                record(3, 272_001, "c"),
            ],
            pricing(),
        )
        self.assertEqual(counts, {"test-model": 1})


class PreScopeHistoryMergeTests(unittest.TestCase):
    """A cycle closing exactly at the scope start must survive the merge."""

    @staticmethod
    def _cycle(anchor_at: float, closed_at: float, cost: str) -> dict:
        return {
            "anchor": {"captured_at": anchor_at, "used_percent": 0.0},
            "closed_at": closed_at,
            "completed_percentage_points": 100,
            "cumulative_usage": {"known_cost_usd": cost},
        }

    def test_cycle_closing_exactly_at_scope_start_is_preserved(self):
        scope_start = 1790167183.0
        completed = self._cycle(1789817204.0, scope_start, "54.61")
        older = self._cycle(1789000000.0, 1789100000.0, "17.91")
        later = self._cycle(scope_start + 600, scope_start + 1200, "2.87")
        merged = backfill._merge_pre_scope_history(
            {"cycle_history": [later]},
            {"cycle_history": [older, completed]},
            scope_start,
        )
        costs = [
            row["cumulative_usage"]["known_cost_usd"] for row in merged["cycle_history"]
        ]
        self.assertEqual(costs, ["17.91", "54.61", "2.87"])

    def test_replayed_cycle_is_not_listed_twice(self):
        scope_start = 1790167183.0
        completed = self._cycle(1789817204.0, scope_start, "54.61")
        replayed = self._cycle(1789817204.0, scope_start, "54.61")
        merged = backfill._merge_pre_scope_history(
            {"cycle_history": [replayed]},
            {"cycle_history": [completed]},
            scope_start,
        )
        self.assertEqual(len(merged["cycle_history"]), 1)

    def test_cycle_closing_after_scope_start_is_still_dropped(self):
        scope_start = 1790167183.0
        in_scope = self._cycle(scope_start - 60, scope_start + 60, "9.99")
        merged = backfill._merge_pre_scope_history(
            {"cycle_history": []},
            {"cycle_history": [in_scope]},
            scope_start,
        )
        self.assertEqual(merged["cycle_history"], [])

    def test_legacy_cycles_use_the_same_inclusive_boundary(self):
        scope_start = 1790167183.0
        merged = backfill._merge_pre_scope_history(
            {"legacy_cycles": []},
            {"legacy_cycles": [self._cycle(1.0, scope_start, "3.00")]},
            scope_start,
        )
        self.assertEqual(len(merged["legacy_cycles"]), 1)


class EvidencePreservationTests(unittest.TestCase):
    """The replay must not erase the parallel outside-VM evidence stream."""

    @staticmethod
    def _checkpoint(payload):
        return backfill.Checkpoint(
            line_number=1,
            line="",
            prefix="2026-09-23 14:51:44 UTC primary=44.0% secondary=7.0% balance=0",
            captured_at=1.0,
            primary_percent=44.0,
            secondary_percent=7.0,
            balance="0",
            payload=payload,
        )

    def test_evidence_is_carried_across_verbatim(self):
        evidence = {
            "available": True,
            "account_lifetime_tokens": 20_773_097_370,
            "account_lifetime_delta": 0,
            "local_period_tokens": 7_553_629,
            "reason": "stale_counter",
        }
        payload: dict = {"period_id": "rebuilt"}
        backfill._carry_external_evidence(payload, self._checkpoint({"external_evidence": evidence}))
        self.assertEqual(payload["external_evidence"], evidence)
        # Deep copy, so a later mutation cannot alias the parsed original.
        payload["external_evidence"]["reason"] = "mutated"
        self.assertEqual(evidence["reason"], "stale_counter")

    def test_missing_evidence_leaves_the_key_absent(self):
        payload: dict = {"period_id": "rebuilt"}
        backfill._carry_external_evidence(payload, self._checkpoint({}))
        self.assertNotIn("external_evidence", payload)

    def test_non_dict_evidence_is_ignored(self):
        payload: dict = {"period_id": "rebuilt"}
        backfill._carry_external_evidence(payload, self._checkpoint({"external_evidence": "n/a"}))
        self.assertNotIn("external_evidence", payload)

    def test_line_without_a_parsed_payload_is_tolerated(self):
        payload: dict = {"period_id": "rebuilt"}
        backfill._carry_external_evidence(payload, self._checkpoint(None))
        self.assertNotIn("external_evidence", payload)


if __name__ == "__main__":
    unittest.main()
