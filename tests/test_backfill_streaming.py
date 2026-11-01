"""Bounded-memory reading and rewriting of a large usage log."""

import sys
import tempfile
import tracemalloc
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from decimal import Decimal

import backfill
from monitor import ModelPricing, PricingTable


def pricing() -> PricingTable:
    return PricingTable(
        revision="test",
        currency="USD",
        unit="per_1m_tokens",
        models={
            "test-model": ModelPricing(
                model_id="test-model",
                input_rate=Decimal(1),
                output_rate=Decimal(2),
                cached_input_rate=Decimal("0.1"),
                cache_write_rate=Decimal(0),
            )
        },
        aliases={},
    )


def usage_line(offset: int, payload: dict | None = None, *, ending: str = "\n") -> str:
    stamp = f"2026-09-23 14:{offset % 60:02d}:44"
    line = f"{stamp} UTC primary=1.0% secondary=2.0% balance=0"
    if payload is not None:
        line += " usage_v2=" + __import__("json").dumps(payload, separators=(",", ":"))
    return line + ending


class StreamingReadTests(unittest.TestCase):
    def test_scan_keeps_no_payload_and_no_source_line_in_memory(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "usage.log"
            path.write_text(
                "".join(
                    usage_line(i, {"period_id": f"p{i}", "windows": {"secondary": {}}})
                    for i in range(5)
                ),
                encoding="utf-8",
            )
            checkpoints = backfill.read_checkpoints(path)
            self.assertEqual(len(checkpoints), 5)
            for checkpoint in checkpoints:
                self.assertEqual(checkpoint.line, "")
                self.assertIsNone(checkpoint.payload)
                self.assertIsNotNone(checkpoint.payload_offset)
                self.assertGreater(checkpoint.payload_length, 0)
                self.assertIsInstance(checkpoint.load_payload(), dict)
                self.assertEqual(checkpoint.load_payload()["period_id"], f"p{checkpoint.line_number}")

    def test_scan_never_calls_read_text(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "usage.log"
            path.write_text(usage_line(1, {"period_id": "p1"}), encoding="utf-8")
            with patch.object(
                Path, "read_text", side_effect=AssertionError("must stream the log")
            ):
                checkpoints = backfill.read_checkpoints(path)
            self.assertEqual(len(checkpoints), 1)

    def test_scan_handles_crlf_and_missing_final_newline(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "usage.log"
            path.write_bytes(
                usage_line(1, {"period_id": "p1"}, ending="\r\n").encode()
                + usage_line(2, {"period_id": "p2"}, ending="").encode()
            )
            checkpoints = backfill.read_checkpoints(path)
            self.assertEqual([c.line_number for c in checkpoints], [0, 1])
            self.assertEqual(checkpoints[0].load_payload()["period_id"], "p1")
            self.assertEqual(checkpoints[1].load_payload()["period_id"], "p2")
            # No stray \r leaks into the prefix used to rebuild the line.
            self.assertFalse(checkpoints[0].prefix.endswith("\r"))

    def test_scan_peak_memory_is_flat(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "usage.log"
            padding = "z" * 2000
            with path.open("w", encoding="utf-8") as handle:
                for index in range(1000):
                    handle.write(
                        usage_line(index, {"period_id": f"p{index}", "pad": padding})
                    )
            size = path.stat().st_size
            tracemalloc.start()
            try:
                checkpoints = backfill.read_checkpoints(path)
                _, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
            self.assertEqual(len(checkpoints), 1000)
            # Metadata only: far below the ~2 MB of text on disk.
            self.assertLess(peak, 2 * 1024 * 1024, f"peak={peak / 1024:.0f} KiB of {size} B")

    def test_missing_payload_range_degrades_quietly(self):
        checkpoint = backfill.Checkpoint(
            line_number=0,
            line="",
            prefix="2026-09-23 14:00:44 UTC primary=1.0% secondary=2.0% balance=0",
            captured_at=1.0,
            primary_percent=1.0,
            secondary_percent=2.0,
            balance="0",
            payload=None,
            payload_path="/nonexistent/usage.log",
            payload_offset=0,
            payload_length=10,
        )
        self.assertIsNone(checkpoint.load_payload())


class StreamingRewriteTests(unittest.TestCase):
    def _write(self, directory: str, body: bytes) -> Path:
        path = Path(directory) / "usage.log"
        path.write_bytes(body)
        return path

    def test_rewrite_preserves_crlf_line_endings(self):
        with tempfile.TemporaryDirectory() as directory:
            body = (
                usage_line(1, {"period_id": "a"}, ending="\r\n")
                + usage_line(2, {"period_id": "b"}, ending="\r\n")
            ).encode()
            path = self._write(directory, body)
            checkpoints = backfill.read_checkpoints(path)
            backup, count, first = backfill.rewrite_usage_log(
                path, checkpoints, {1: {"weekly_estimate": {"full_window_usd": "9"}}}
            )
            self.assertEqual((count, first), (1, 1))
            data = path.read_bytes()
            self.assertEqual(data.count(b"\r\n"), 2)
            self.assertIn(b"weekly_100pct_usd=9", data)
            self.assertEqual(data.split(b"\r\n")[0], body.split(b"\r\n")[0])
            self.assertTrue(backup and backup.exists())

    def test_rewrite_preserves_missing_final_newline(self):
        with tempfile.TemporaryDirectory() as directory:
            body = usage_line(1, {"period_id": "a"}, ending="").encode()
            path = self._write(directory, body)
            checkpoints = backfill.read_checkpoints(path)
            backfill.rewrite_usage_log(path, checkpoints, {0: {"period_id": "new"}})
            self.assertFalse(path.read_bytes().endswith(b"\n"))
            self.assertIn(b'"period_id":"new"', path.read_bytes())

    def test_untouched_lines_are_copied_verbatim(self):
        with tempfile.TemporaryDirectory() as directory:
            body = (
                b"legacy line without payload\n"
                + usage_line(2, {"period_id": "b"}).encode()
            )
            path = self._write(directory, body)
            checkpoints = backfill.read_checkpoints(path)
            backfill.rewrite_usage_log(path, checkpoints, {1: {"period_id": "new"}})
            self.assertEqual(path.read_bytes().split(b"\n")[0], b"legacy line without payload")

    def test_rewrite_rejects_a_line_beyond_the_end(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._write(directory, usage_line(1, {"period_id": "a"}).encode())
            checkpoints = backfill.read_checkpoints(path)
            ghost = backfill.Checkpoint(
                line_number=99,
                line="",
                prefix=checkpoints[0].prefix,
                captured_at=2.0,
                primary_percent=1.0,
                secondary_percent=2.0,
                balance="0",
                payload=None,
            )
            with self.assertRaises(backfill.monitor.MonitorError):
                backfill.rewrite_usage_log(path, [ghost], {99: {"period_id": "x"}})
            # The original file is untouched after a failed rewrite.
            self.assertIn(b'"period_id":"a"', path.read_bytes())

    def test_abort_leaves_the_original_and_no_temporary(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._write(directory, usage_line(1, {"period_id": "a"}).encode())
            checkpoints = backfill.read_checkpoints(path)
            rewriter = backfill._StreamingLogRewrite(path).start()
            rewriter.emit(checkpoints[0], {"period_id": "b"})
            rewriter.abort()
            self.assertIn(b'"period_id":"a"', path.read_bytes())
            self.assertEqual(list(Path(directory).glob(".usage.log.*.tmp")), [])

    def test_out_of_order_emission_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            body = (
                usage_line(1, {"period_id": "a"})
                + usage_line(2, {"period_id": "b"})
            ).encode()
            path = self._write(directory, body)
            checkpoints = backfill.read_checkpoints(path)
            rewriter = backfill._StreamingLogRewrite(path).start()
            try:
                rewriter.emit(checkpoints[1], {"period_id": "b2"})
                with self.assertRaises(backfill.monitor.MonitorError):
                    rewriter.emit(checkpoints[0], {"period_id": "a2"})
            finally:
                rewriter.abort()


class CliGuardTests(unittest.TestCase):
    def test_gzipped_archive_is_rejected_with_a_clear_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "usage.log.bak-20260101T000000Z.gz"
            path.write_bytes(b"")
            with self.assertRaises(backfill.monitor.MonitorError) as caught:
                backfill.run_backfill(
                    backfill.build_parser().parse_args(
                        ["--usage-log", str(path), "--state-path", str(Path(directory) / "s.json")]
                    )
                )
            self.assertIn("gzip", str(caught.exception))


class PayloadSinkTests(unittest.TestCase):
    def test_sink_receives_payloads_without_buffering_them(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "usage.log"
            path.write_text(
                "".join(
                    usage_line(i, {"period_id": f"p{i}", "windows": {"secondary": {}}})
                    for i in range(3)
                ),
                encoding="utf-8",
            )
            checkpoints = backfill.read_checkpoints(path)
            seen = []
            result = backfill.replay_history(
                checkpoints,
                [],
                pricing(),
                raw_diagnostics={"coverage_gap": False},
                scope_start=0.0,
                payload_sink=lambda checkpoint, payload: seen.append(
                    (checkpoint.line_number, payload)
                ),
            )
            self.assertEqual([line for line, _ in seen], [0, 1, 2])
            self.assertEqual(result["payloads"], {})
            self.assertIsInstance(result["last_payload"], dict)


if __name__ == "__main__":
    unittest.main()
