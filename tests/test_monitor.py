import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from monitor import LimitWindow, Snapshot, detect_events, snapshot_from_payload


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


if __name__ == "__main__":
    unittest.main()
