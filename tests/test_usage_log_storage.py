"""Memory-safe usage.log storage: tail dedup, streaming backfill, rotation."""

import gzip
import json
import os
import sys
import tempfile
import tracemalloc
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from monitor import (
    UsageLogPolicy,
    _append_usage_line_once,
    _iter_usage_log_payloads,
    archive_oversized_usage_log,
    backfill_accounting_usage_from_log,
    prepare_usage_log,
    rotate_usage_log,
    usage_log_policy,
)


def usage_line(
    period_id: str, end_epoch: float = 100.0, tokens: int = 10, pad: int = 0
) -> str:
    payload = {
        "period_id": period_id,
        "period": {"end": end_epoch},
        "total": {
            "requests": 1,
            "input_tokens": tokens,
            "output_tokens": 1,
        },
        "models": {"test-model": {"requests": 1, "input_tokens": tokens, "output_tokens": 1}},
    }
    if pad:
        payload["diagnostics"] = {"padding": "z" * pad}
    return "1970-01-01T00:00:00Z primary=1% secondary=1% balance=- usage_v2=" + json.dumps(
        payload, separators=(",", ":"), sort_keys=True
    )


class NoFullFileReads:
    """Fail loudly if the code under test pulls the whole file into memory."""

    def __enter__(self):
        self._patch = patch.object(
            Path,
            "read_text",
            side_effect=AssertionError("usage.log must never be read with read_text()"),
        )
        self._patch.start()
        return self

    def __exit__(self, *exc_info):
        self._patch.stop()
        return False


class TailDedupTests(unittest.TestCase):
    def test_duplicate_period_id_in_tail_is_not_appended_twice(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "usage.log"
            _append_usage_line_once(path, usage_line("p1"), "p1")
            _append_usage_line_once(path, usage_line("p1"), "p1")
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)

    def test_distinct_period_ids_are_both_appended(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "usage.log"
            _append_usage_line_once(path, usage_line("p1"), "p1")
            _append_usage_line_once(path, usage_line("p2"), "p2")
            self.assertEqual(len(path.read_text(encoding="utf-8").splitlines()), 2)

    def test_dedup_never_reads_the_whole_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "usage.log"
            path.write_text(usage_line("old") + "\n" * 5000, encoding="utf-8")
            with NoFullFileReads():
                _append_usage_line_once(path, usage_line("new"), "new")
            self.assertIn("new", path.read_text(encoding="utf-8").splitlines()[-1])

    def test_dedup_is_bounded_by_the_tail_window(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "usage.log"
            # The period id lives far outside a tiny tail window, so it is not
            # found; this is the documented trade-off that keeps memory flat.
            path.write_text(usage_line("ancient") + "\n" + "x" * 20000 + "\n", encoding="utf-8")
            policy = UsageLogPolicy(tail_bytes=4096)
            _append_usage_line_once(path, usage_line("ancient"), "ancient", policy)
            self.assertEqual(len(path.read_text(encoding="utf-8").splitlines()), 3)

    def test_dedup_finds_a_period_across_the_rotation_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "usage.log"
            _append_usage_line_once(path, usage_line("p1"), "p1")
            self.assertTrue(rotate_usage_log(path, UsageLogPolicy(max_bytes=1, max_backups=3)))
            self.assertFalse(path.exists() and path.stat().st_size)
            _append_usage_line_once(path, usage_line("p1"), "p1")
            self.assertFalse(path.exists() and path.stat().st_size)


class StreamingBackfillTests(unittest.TestCase):
    def test_backfill_streams_without_read_text(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "usage.log"
            path.write_text(
                "\n".join(
                    [
                        usage_line("a", end_epoch=200.0, tokens=100),
                        usage_line("b", end_epoch=300.0, tokens=250),
                        "no payload here",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            accounting = {
                "last_sample_at": 999,
                "windows": {"secondary": {"anchor": {"captured_at": 100}}},
            }
            with NoFullFileReads():
                changed = backfill_accounting_usage_from_log(accounting, path)
            self.assertTrue(changed)
            total = accounting["windows"]["secondary"]["usage_total"]
            self.assertEqual(total["input_tokens"], 350)
            self.assertEqual(total["requests"], 2)
            model = accounting["windows"]["secondary"]["usage_by_model"]["test-model"]
            self.assertEqual(model["input_tokens"], 350)

    def test_backfill_respects_anchor_and_last_sample(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "usage.log"
            path.write_text(
                "\n".join(
                    [
                        usage_line("before-anchor", end_epoch=50.0, tokens=1000),
                        usage_line("inside", end_epoch=200.0, tokens=7),
                        usage_line("future", end_epoch=500.0, tokens=999),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            accounting = {
                "last_sample_at": 400,
                "windows": {"secondary": {"anchor": {"captured_at": 100}}},
            }
            backfill_accounting_usage_from_log(accounting, path)
            self.assertEqual(
                accounting["windows"]["secondary"]["usage_total"]["input_tokens"], 7
            )

    def test_backfill_deduplicates_repeated_period_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "usage.log"
            path.write_text(
                "\n".join(
                    [
                        usage_line("same", end_epoch=200.0, tokens=100),
                        usage_line("same", end_epoch=200.0, tokens=100),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            accounting = {
                "last_sample_at": 999,
                "windows": {"secondary": {"anchor": {"captured_at": 100}}},
            }
            backfill_accounting_usage_from_log(accounting, path)
            self.assertEqual(
                accounting["windows"]["secondary"]["usage_total"]["input_tokens"], 100
            )

    def test_backfill_peak_memory_is_flat_for_a_large_log(self):
        # ~24 MB of ~120 KB lines: the old read_text()+splitlines() path needed
        # several times this in RAM, the streaming path needs one line.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "usage.log"
            padding = 120_000
            with path.open("w", encoding="utf-8") as handle:
                for index in range(200):
                    handle.write(
                        usage_line(f"p{index}", end_epoch=200.0 + index, tokens=1, pad=padding)
                        + "\n"
                    )
            accounting = {
                "last_sample_at": 10_000,
                "windows": {"secondary": {"anchor": {"captured_at": 100}}},
            }
            tracemalloc.start()
            try:
                backfill_accounting_usage_from_log(accounting, path)
                _, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
            self.assertLess(peak, 32 * 1024 * 1024, f"peak={peak / 1024 / 1024:.1f} MiB")
            self.assertEqual(
                accounting["windows"]["secondary"]["usage_total"]["requests"], 200
            )


class RotationTests(unittest.TestCase):
    def test_rotation_shifts_and_compresses_older_archives(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "usage.log"
            policy = UsageLogPolicy(max_bytes=1024, max_backups=3, archive_compress_level=1)
            for index in range(6):
                _append_usage_line_once(
                    path, usage_line(f"p{index}") + " pad=" + "y" * 2000, f"p{index}", policy
                )
            self.assertTrue(path.exists())
            self.assertTrue((Path(str(path) + ".1")).exists())
            self.assertTrue((Path(str(path) + ".2.gz")).exists())
            self.assertTrue((Path(str(path) + ".3.gz")).exists())
            self.assertFalse((Path(str(path) + ".4.gz")).exists())
            # Retention is bounded even after many rotations.
            self.assertEqual(
                sorted(p.name for p in Path(directory).glob("usage.log.*") if p.name != "usage.log.lock"),
                ["usage.log.1", "usage.log.2.gz", "usage.log.3.gz"],
            )

    def test_rotated_archive_round_trips_its_content(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "usage.log"
            path.write_text(usage_line("keep-me") + "\n", encoding="utf-8")
            self.assertTrue(rotate_usage_log(path, UsageLogPolicy(max_bytes=1, max_backups=2)))
            archived = path.with_name(path.name + ".1")
            self.assertIn("keep-me", archived.read_text(encoding="utf-8"))
            # The active file is created by the next append, not by rotation.
            self.assertFalse(path.exists() or (path.exists() and path.stat().st_size))

    def test_age_based_rotation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "usage.log"
            path.write_text(usage_line("old") + "\n", encoding="utf-8")
            os.utime(path, (1_000_000, 1_000_000))
            policy = UsageLogPolicy(max_bytes=0, max_age_seconds=3600, max_backups=2)
            self.assertTrue(rotate_usage_log(path, policy))
            self.assertTrue((path.parent / (path.name + ".1")).exists())

    def test_rotation_keeps_only_newest_archives(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "usage.log"
            policy = UsageLogPolicy(max_bytes=1, max_backups=2, archive_compress_level=1)
            for index in range(5):
                _append_usage_line_once(path, usage_line(f"p{index}"), f"p{index}", policy)
            names = sorted(p.name for p in Path(directory).glob("usage.log.[0-9]*"))
            self.assertEqual(names, ["usage.log.1", "usage.log.2.gz"])


class OversizedMigrationTests(unittest.TestCase):
    def test_oversized_log_is_archived_and_replaced_with_an_empty_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "usage.log"
            body = "".join(usage_line(f"p{i}") + "\n" for i in range(50))
            path.write_text(body, encoding="utf-8")
            archive = archive_oversized_usage_log(path, threshold_bytes=1)
            self.assertIsNotNone(archive)
            self.assertEqual(path.stat().st_size, 0)
            self.assertTrue(str(archive).endswith(".gz"))
            with gzip.open(archive, "rt", encoding="utf-8") as handle:
                self.assertIn("p0", handle.read())

    def test_small_log_is_left_alone(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "usage.log"
            path.write_text(usage_line("p") + "\n", encoding="utf-8")
            self.assertIsNone(archive_oversized_usage_log(path, threshold_bytes=1024 * 1024))
            self.assertGreater(path.stat().st_size, 0)

    def test_migration_is_skipped_until_counters_are_persisted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "usage.log"
            path.write_text(usage_line("p") + "\n", encoding="utf-8")
            state = Path(directory) / "state.json"
            state.write_text(
                json.dumps(
                    {
                        "accounting": {
                            "windows": {"secondary": {"anchor": {"captured_at": 1}}}
                        }
                    }
                ),
                encoding="utf-8",
            )
            with patch("monitor.usage_log_policy", return_value=UsageLogPolicy()):
                prepare_usage_log(path, state)
            self.assertGreater(path.stat().st_size, 0)

    def test_migration_runs_when_no_window_needs_backfill(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "usage.log"
            path.write_text(usage_line("p") + "\n", encoding="utf-8")
            state = Path(directory) / "state.json"
            state.write_text(
                json.dumps(
                    {
                        "accounting": {
                            "windows": {
                                "secondary": {
                                    "anchor": {},
                                    "usage_total": {},
                                    "usage_by_model": {},
                                }
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            with patch(
                "monitor.usage_log_policy",
                return_value=UsageLogPolicy(archive_threshold_bytes=1),
            ):
                prepare_usage_log(path, state)
            self.assertEqual(path.stat().st_size, 0)
            self.assertTrue(list(Path(directory).glob("usage.log.bak-*.gz")))

    def test_migration_runs_when_there_are_no_windows_at_all(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "usage.log"
            path.write_text(usage_line("p") + "\n", encoding="utf-8")
            state = Path(directory) / "state.json"
            state.write_text(json.dumps({"accounting": {"windows": None}}), encoding="utf-8")
            with patch(
                "monitor.usage_log_policy",
                return_value=UsageLogPolicy(archive_threshold_bytes=1),
            ):
                prepare_usage_log(path, state)
            self.assertEqual(path.stat().st_size, 0)


class ArchiveAwareBackfillTests(unittest.TestCase):
    def test_backfill_reads_rotated_and_gzipped_segments(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "usage.log"
            archived = path.with_name(path.name + ".1")
            archived.write_text(usage_line("old", end_epoch=200.0, tokens=11) + "\n", encoding="utf-8")
            compressed = path.with_name(path.name + ".2.gz")
            with gzip.open(compressed, "wt", encoding="utf-8") as handle:
                handle.write(usage_line("older", end_epoch=190.0, tokens=13) + "\n")
            path.write_text(usage_line("new", end_epoch=300.0, tokens=17) + "\n", encoding="utf-8")
            accounting = {
                "last_sample_at": 999,
                "windows": {"secondary": {"anchor": {"captured_at": 100}}},
            }
            backfill_accounting_usage_from_log(accounting, path)
            self.assertEqual(
                accounting["windows"]["secondary"]["usage_total"]["input_tokens"], 41
            )

    def test_backfill_skips_archives_when_disabled(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "usage.log"
            path.with_name(path.name + ".1").write_text(
                usage_line("old", end_epoch=200.0, tokens=11) + "\n", encoding="utf-8"
            )
            path.write_text(usage_line("new", end_epoch=300.0, tokens=17) + "\n", encoding="utf-8")
            accounting = {
                "last_sample_at": 999,
                "windows": {"secondary": {"anchor": {"captured_at": 100}}},
            }
            with patch.dict(os.environ, {"USAGE_LOG_BACKFILL_ARCHIVES": "0"}):
                backfill_accounting_usage_from_log(accounting, path)
            self.assertEqual(
                accounting["windows"]["secondary"]["usage_total"]["input_tokens"], 17
            )

    def test_migration_archive_is_still_backfillable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "usage.log"
            path.write_text(usage_line("p", end_epoch=200.0, tokens=23) + "\n", encoding="utf-8")
            archive_oversized_usage_log(path, threshold_bytes=1)
            self.assertEqual(path.stat().st_size, 0)
            accounting = {
                "last_sample_at": 999,
                "windows": {"secondary": {"anchor": {"captured_at": 100}}},
            }
            backfill_accounting_usage_from_log(accounting, path)
            self.assertEqual(
                accounting["windows"]["secondary"]["usage_total"]["input_tokens"], 23
            )


    def test_backfill_works_when_only_archives_remain(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "usage.log"
            path.write_text(usage_line("p", end_epoch=200.0, tokens=29) + "\n", encoding="utf-8")
            # Rotation renames the active file away and leaves no new one yet.
            self.assertTrue(rotate_usage_log(path, UsageLogPolicy(max_bytes=1, max_backups=2)))
            self.assertFalse(path.exists())
            accounting = {
                "last_sample_at": 999,
                "windows": {"secondary": {"anchor": {"captured_at": 100}}},
            }
            backfill_accounting_usage_from_log(accounting, path)
            self.assertEqual(
                accounting["windows"]["secondary"]["usage_total"]["input_tokens"], 29
            )


class PayloadStreamTests(unittest.TestCase):
    def test_stream_skips_malformed_and_irrelevant_lines(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "usage.log"
            path.write_text(
                "legacy prefix only\n"
                "primary=1% usage_v2={not json\n"
                'primary=1% usage_v2={"ok": true}\n',
                encoding="utf-8",
            )
            payloads = list(_iter_usage_log_payloads(path))
            self.assertEqual(payloads, [{"ok": True}])


class PolicyTests(unittest.TestCase):
    def test_defaults_are_bounded(self):
        policy = usage_log_policy()
        self.assertGreater(policy.max_bytes, 0)
        self.assertLessEqual(policy.max_bytes, 100 * 1024 * 1024)
        self.assertGreater(policy.max_backups, 0)
        self.assertGreater(policy.tail_bytes, 0)
        self.assertGreater(policy.archive_threshold_bytes, policy.max_bytes)

    def test_environment_overrides(self):
        with patch.dict(
            os.environ,
            {
                "USAGE_LOG_MAX_BYTES": "1048576",
                "USAGE_LOG_MAX_BACKUPS": "2",
                "USAGE_LOG_TAIL_BYTES": "65536",
                "USAGE_LOG_ARCHIVE_THRESHOLD_BYTES": "2097152",
                "USAGE_LOG_COMPRESS_BACKUPS": "false",
            },
        ):
            policy = usage_log_policy()
        self.assertEqual(policy.max_bytes, 1048576)
        self.assertEqual(policy.max_backups, 2)
        self.assertEqual(policy.tail_bytes, 65536)
        self.assertEqual(policy.archive_threshold_bytes, 2097152)
        self.assertFalse(policy.compress_backups)

    def test_invalid_environment_values_fall_back_to_defaults(self):
        with patch.dict(os.environ, {"USAGE_LOG_MAX_BYTES": "not-a-number"}, clear=False):
            self.assertEqual(usage_log_policy().max_bytes, UsageLogPolicy().max_bytes)


if __name__ == "__main__":
    unittest.main()
