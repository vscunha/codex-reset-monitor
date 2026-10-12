"""An interval audit must never turn skipped percentages into zero-cost points."""

import unittest
from decimal import Decimal

from weekly_cycle_audit import (
    analyze_cycle,
    annotate_export,
    build_historical_adjustment_ledger,
    historical_adjustment_markdown,
    markdown,
)
from pathlib import Path


class WeeklyCycleAuditTests(unittest.TestCase):
    def test_replaces_not_adds_assumed_cost_for_weak_jumps(self):
        levels = [(0, "0"), (54, "1"), (61, "1.1"), (69, "1.2"),
                  (93, "2.2"), (96, "2.3"), (100, "3")]
        cycle = {
            "cycle": "secondary_100pt_2026-09-19",
            "window": "secondary",
            "crossings": [
                {"percent": pct, "captured_at": i * 60, "cumulative_cost_usd": cost,
                 "costs_by_model": {"gpt-test": cost}}
                for i, (pct, cost) in enumerate(levels)
            ],
            "excluded_models": [], "requests_detail": [], "data_sources": {},
            "cost_usd": {"local_priced_total": "3"},
            "monitor_summary": {
                "known_cost_usd": "3",
                "model_attribution": {"external-imputed": {"cost_usd": "0"}},
            },
        }
        result = analyze_cycle(cycle, {180: {"reason": "stale_counter", "account_lifetime_delta": 0}})
        self.assertEqual(len(result["intervals"]), 6)
        self.assertEqual(result["intervals"][1]["points"], 7)
        self.assertEqual(result["intervals"][1]["tick_account_lifetime_delta"], None)
        self.assertEqual(result["intervals"][2]["tick_evidence_reason"], "stale_counter")
        scenario = result["conditional_scenario"]
        self.assertEqual(scenario["reference_points"], 82)
        self.assertEqual(scenario["replaced_points"], 18)
        self.assertEqual(Decimal(scenario["recorded_cost_replaced_usd"]), Decimal("0.3"))
        self.assertEqual(result["local_token_backed_estimate"]["counted_token_backed_points"], 100)
        self.assertEqual(
            Decimal(result["local_token_backed_estimate"]["estimate_100_usd"]), Decimal("3")
        )
        self.assertEqual(
            Decimal(scenario["hypothetical_close_total_usd"]),
            Decimal("3") - Decimal("0.3") + Decimal("2.7") / Decimal(82) * Decimal(18),
        )
        adjustment = result["selected_historical_adjustment"]
        self.assertEqual(adjustment["adjusted_value_at_first_100_usd"], scenario["hypothetical_first_100_usd"])
        self.assertEqual(adjustment["adjusted_value_through_close_usd"], scenario["hypothetical_close_total_usd"])
        older = {
            "cycle": "secondary_13pt_2026-09-11", "window": "secondary",
            "started_at": "2026-09-11T19:04:43+00:00", "ended_at": "2026-09-11T20:43:43+00:00",
            "monitor_summary": {"known_cost_usd": "13.98"}, "outside_vm_evidence": [],
        }
        current = {
            "cycle": "current_secondary", "window": "secondary", "started_at": "2026-09-23T12:39:43+00:00",
            "ended_at": None, "monitor_summary": {"known_cost_usd": "11"},
            "outside_vm_evidence": [{"reason": "stale_counter"}],
        }
        export = {
            "cycles": [cycle, older, current], "notes": [],
            "live_weekly_checkpoint": {"local_known_cost_usd": "10.5"},
        }
        annotate_export(export, result, Path("original-usage.log.bak"))
        self.assertEqual(len(cycle["observed_intervals"]), 6)
        self.assertEqual(cycle["outside_vm_evidence"][0]["account_lifetime_delta"], 0)
        self.assertEqual(cycle["cost_usd"]["local_priced_total"], "3")
        self.assertEqual(cycle["monitor_summary"]["known_cost_usd"], "3")
        self.assertEqual(cycle["local_token_backed_estimate"]["estimate_100_usd"], "3")
        self.assertEqual(cycle["historical_adjustment"]["adjusted_value_through_close_usd"], scenario["hypothetical_close_total_usd"])
        self.assertIsNone(older["historical_adjustment"]["adjusted_value_through_close_usd"])
        self.assertFalse(older["historical_adjustment"]["include_in_historical_comparison"])
        self.assertIn("stale/catching up", current["historical_adjustment"]["reason"])
        ledger = build_historical_adjustment_ledger(export, result)
        self.assertEqual(len(ledger["cycles"]), 3)
        self.assertEqual(
            next(row for row in ledger["cycles"] if row["cycle"] == "current_secondary")["raw_monitor_total_usd"],
            "10.5",
        )
        self.assertIn("conditional", historical_adjustment_markdown(ledger))
        text = markdown(result)
        self.assertIn("54→61% | +7", text)
        self.assertNotIn("55→56%", text)
        self.assertIn("Alternate local-only diagnostic", text)


if __name__ == "__main__":
    unittest.main()
