import json
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import monitor
from benchmarks.percentage_benchmark import (
    bracket_uncertainty_tokens_per_point,
    build_pi_command,
    build_prompt,
    build_calibrations,
    build_parser,
    canonical_model,
    choose_chunk_tokens,
    codex_usage_records,
    codex_usage_totals,
    command_summary,
    estimate_tokens_per_point,
    parse_codex_events,
    parse_model_overrides,
    parse_models,
    parse_pi_events,
    pi_usage_records,
    pi_usage_totals,
    reset_advanced,
)


class PercentageBenchmarkTests(unittest.TestCase):
    def test_default_target_is_one_tenth_of_a_percentage_point(self):
        args = build_parser().parse_args([])
        self.assertEqual(args.target_points, Decimal("0.1"))
        self.assertEqual(args.chunk_tokens, 50_000)

    def test_model_aliases_and_model_list(self):
        self.assertEqual(canonical_model("Luna"), "gpt-5.6-luna")
        self.assertEqual(canonical_model("astra"), "gpt-6-astra")
        self.assertEqual(parse_models("luna, terra, sol"), (
            "gpt-5.6-luna",
            "gpt-5.6-terra",
            "gpt-5.6-sol",
        ))

    def test_prompt_has_requested_approximate_payload_and_unique_nonce(self):
        prompt = build_prompt(100, "run-a")
        self.assertIn("Benchmark nonce: run-a", prompt)
        self.assertIn("PAYLOAD START", prompt)
        self.assertIn("PAYLOAD END", prompt)
        self.assertGreater(len(prompt.split()), 100)

    def test_calibration_uses_completed_crossing_deltas(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            state = {
                "accounting": {
                    "windows": {
                        "secondary": {
                            "crossings": [
                                {"percent": 10, "cumulative_usage": {"input_tokens": 0, "output_tokens": 0}},
                                {"percent": 11, "cumulative_usage": {"input_tokens": 100, "output_tokens": 10}},
                                {"percent": 13, "cumulative_usage": {"input_tokens": 300, "output_tokens": 20}},
                            ]
                        }
                    }
                }
            }
            path.write_text(json.dumps(state), encoding="utf-8")
            calibration = estimate_tokens_per_point(path, "secondary")
        self.assertEqual(calibration.source, "state.crossings")
        self.assertEqual(calibration.intervals, 2)
        self.assertEqual(calibration.tokens_per_point, Decimal("107.5"))

    def test_model_calibration_uses_only_exclusive_intervals(self):
        def crossing(percent, luna, sol):
            return {
                "percent": percent,
                "reliable": True,
                "cumulative_usage": {
                    "models": {
                        "gpt-5.6-luna": {"input_tokens": luna, "output_tokens": 0},
                        "gpt-5.6-sol": {"input_tokens": sol, "output_tokens": 0},
                    }
                },
            }

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text(json.dumps({
                "accounting": {"windows": {"secondary": {"crossings": [
                    crossing(10, 0, 0),
                    crossing(11, 100, 0),       # Luna only: 100/point
                    crossing(12, 200, 900),     # Mixed: excluded for both
                    crossing(13, 200, 1100),   # Sol only: 200/point
                ]}}}
            }), encoding="utf-8")
            luna = estimate_tokens_per_point(path, "secondary", model="luna")
            sol = estimate_tokens_per_point(path, "secondary", model="sol")

        self.assertEqual(luna.tokens_per_point, Decimal("100"))
        self.assertEqual(luna.source, "state.crossings:model_exclusive")
        self.assertEqual(luna.rejected_mixed_intervals, 2)
        self.assertEqual(sol.tokens_per_point, Decimal("200"))
        self.assertEqual(sol.rejected_mixed_intervals, 2)

    def test_model_without_exclusive_interval_is_not_given_shared_average(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text(json.dumps({
                "accounting": {"windows": {"secondary": {"crossings": [
                    {"percent": 10, "cumulative_usage": {"models": {
                        "gpt-5.6-luna": {"input_tokens": 0},
                        "gpt-5.6-sol": {"input_tokens": 0},
                    }}},
                    {"percent": 11, "cumulative_usage": {"models": {
                        "gpt-5.6-luna": {"input_tokens": 100},
                        "gpt-5.6-sol": {"input_tokens": 100},
                    }}},
                ]}}}
            }), encoding="utf-8")
            sol = estimate_tokens_per_point(path, "secondary", model="sol")

        self.assertIsNone(sol.tokens_per_point)
        self.assertEqual(sol.source, "unavailable_model_mixture")
        self.assertEqual(sol.reason, "no_exclusive_model_intervals")

    def test_build_calibrations_keeps_model_targets_independent(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text(json.dumps({"accounting": {"windows": {"secondary": {"crossings": []}}}}), encoding="utf-8")
            values = build_calibrations(
                path,
                "secondary",
                ("luna", "sol"),
                lookback=12,
                model_overrides=parse_model_overrides(["luna=100", "sol=200"]),
            )
        self.assertEqual(values["gpt-5.6-luna"].tokens_per_point, Decimal("100"))
        self.assertEqual(values["gpt-5.6-sol"].tokens_per_point, Decimal("200"))
        self.assertNotEqual(
            values["gpt-5.6-luna"].tokens_per_point,
            values["gpt-5.6-sol"].tokens_per_point,
        )

    def test_missing_calibration_is_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            calibration = estimate_tokens_per_point(Path(directory) / "missing.json", "primary")
        self.assertIsNone(calibration.tokens_per_point)
        self.assertEqual(calibration.source, "unavailable")

class PiHarnessTests(unittest.TestCase):
    def test_pi_command_disables_tools_and_uses_json_mode(self):
        command = build_pi_command(
            "gpt-5.6-luna",
            pi_command="pi",
            provider="openai-codex",
            thinking="low",
            system_prompt="reply BENCHMARK_OK",
            keep_extensions=False,
            extra_args=None,
        )
        self.assertIn("--mode", command)
        self.assertIn("json", command)
        self.assertIn("--no-session", command)
        self.assertIn("--no-tools", command)
        self.assertIn("--no-context-files", command)
        self.assertIn("--no-extensions", command)
        self.assertIn("openai-codex/gpt-5.6-luna", command)
        self.assertIn("--thinking", command)
        self.assertIn("reply BENCHMARK_OK", command)

    def test_pi_command_respects_keep_extensions_and_explicit_provider(self):
        command = build_pi_command(
            "openai-codex/gpt-6-luna",
            pi_command="pi",
            provider="openai-codex",
            thinking=None,
            system_prompt=None,
            keep_extensions=True,
            extra_args="--verbose",
        )
        self.assertNotIn("--no-extensions", command)
        self.assertNotIn("--thinking", command)
        self.assertNotIn("--system-prompt", command)
        self.assertEqual(command.count("openai-codex/gpt-6-luna"), 1)
        self.assertIn("--verbose", command)

    def test_pi_usage_parsing_sums_assistant_messages_and_dedupes(self):
        events = "\n".join(
            json.dumps(event)
            for event in (
                {"type": "message_start", "message": {"role": "user", "content": "payload"}},
                {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "model": "gpt-6-luna",
                        "timestamp": 1789099924000,
                        "usage": {
                            "input": 1000,
                            "output": 10,
                            "cacheRead": 250,
                            "cacheWrite": 0,
                            "totalTokens": 1260,
                            "cost": {"total": 0.000105},
                        },
                    },
                },
                {
                    "type": "agent_end",
                    "messages": [
                        {
                            "role": "assistant",
                            "model": "gpt-6-luna",
                            "timestamp": 1789099924000,
                            "usage": {
                                "input": 1000,
                                "output": 10,
                                "cacheRead": 250,
                                "totalTokens": 1260,
                                "cost": {"total": 0.000105},
                            },
                        }
                    ],
                },
            )
        )
        usages = parse_pi_events(events)
        self.assertEqual(len(usages), 1)
        self.assertEqual(usages[0].model, "gpt-6-luna")
        self.assertEqual(usages[0].input_tokens, 1000)
        self.assertEqual(usages[0].cache_read_tokens, 250)
        self.assertEqual(usages[0].total_tokens, 1260)
        totals = pi_usage_totals(usages)
        self.assertEqual(totals.input_tokens, 1250)
        self.assertEqual(totals.cached_input_tokens, 250)
        self.assertEqual(totals.uncached_input_tokens, 1000)
        self.assertEqual(totals.total_tokens, 1260)

    def test_pi_usage_records_match_monitor_pricing_semantics(self):
        events = json.dumps(
            {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "model": "gpt-5.6-luna",
                    "timestamp": 1789099924000,
                    "usage": {
                        "input": 600,
                        "output": 10,
                        "cacheRead": 400,
                        "cacheWrite": 600,
                        "totalTokens": 1010,
                        "cost": {"total": 0.00014},
                    },
                },
            }
        )
        usage = parse_pi_events(events)[0]
        record = pi_usage_records([usage])[0]
        self.assertEqual(record.input_tokens, 1000)
        self.assertEqual(record.cache_read_tokens, 400)
        self.assertEqual(record.raw_cache_write_tokens, 600)
        pricing = monitor.load_model_pricing(
            Path(__file__).resolve().parents[1] / "model-pricing.json.example"
        )
        breakdown = monitor.price_usage_record(record, pricing)
        self.assertEqual(breakdown.uncached_input_tokens, 600)
        self.assertEqual(breakdown.cached_input_tokens, 400)
        self.assertEqual(breakdown.total_cost, Decimal("0.000140"))

    def test_choose_chunk_tokens_switches_to_fine_near_boundary(self):
        self.assertEqual(
            choose_chunk_tokens(
                base_chunk=50_000,
                fine_chunk=5_000,
                fine_window_fraction=Decimal("0.15"),
                expected_tokens_per_point=Decimal("100000"),
                consumed_since_last_crossing=Decimal("10000"),
            ),
            50_000,
        )
        self.assertEqual(
            choose_chunk_tokens(
                base_chunk=50_000,
                fine_chunk=5_000,
                fine_window_fraction=Decimal("0.15"),
                expected_tokens_per_point=Decimal("100000"),
                consumed_since_last_crossing=Decimal("95000"),
            ),
            5_000,
        )
        self.assertEqual(
            choose_chunk_tokens(
                base_chunk=50_000,
                fine_chunk=None,
                fine_window_fraction=Decimal("0.15"),
                expected_tokens_per_point=Decimal("100000"),
                consumed_since_last_crossing=Decimal("95000"),
            ),
            50_000,
        )

    def test_bracket_uncertainty_divides_by_observed_points(self):
        self.assertEqual(
            bracket_uncertainty_tokens_per_point([5_000, 5_000], Decimal("2")),
            Decimal("5000"),
        )
        self.assertIsNone(bracket_uncertainty_tokens_per_point([5_000], Decimal("0")))

    def test_reset_detection_ignores_second_level_drift(self):
        self.assertFalse(reset_advanced(1_000, 1_001))
        self.assertFalse(reset_advanced(1_000, 1_060))
        self.assertTrue(reset_advanced(1_000, 1_061))
        self.assertTrue(reset_advanced(1_000, 1_000 + 5 * 3600))
        self.assertFalse(reset_advanced(None, 1_000))
        self.assertFalse(reset_advanced(1_000, None))

    def test_command_summary_stays_json_serializable(self):
        usages = parse_pi_events(
            json.dumps(
                {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "model": "gpt-6-luna",
                        "timestamp": 1789099924000,
                        "usage": {"input": 10, "output": 1, "cacheRead": 0, "totalTokens": 11},
                    },
                }
            )
        )
        summary = command_summary(
            {
                "returncode": 0,
                "timed_out": False,
                "started_at": 1.0,
                "finished_at": 2.0,
                "stdout_bytes": 10,
                "stderr_tail": "",
                "pi_usages": usages,
                "pi_models": ["gpt-6-luna"],
                "parse_error": None,
            }
        )
        self.assertNotIn("pi_usages", summary)
        json.dumps(summary)

    def test_parser_accepts_pi_harness_and_granularity_options(self):
        args = build_parser().parse_args(
            [
                "--harness",
                "pi",
                "--token-source",
                "pi",
                "--accumulate-points",
                "3",
                "--fine-chunk-tokens",
                "2000",
                "--fine-window-fraction",
                "0.1",
                "--poll-seconds",
                "0.1",
                "--thinking",
                "low",
                "--pi-provider",
                "openai-codex",
            ]
        )
        self.assertEqual(args.harness, "pi")
        self.assertEqual(args.token_source, "pi")
        self.assertEqual(args.accumulate_points, 3)
        self.assertEqual(args.fine_chunk_tokens, 2_000)
        self.assertEqual(args.fine_window_fraction, Decimal("0.1"))
        self.assertEqual(args.poll_seconds, 0.1)
        self.assertEqual(args.thinking, "low")
        self.assertEqual(args.pi_provider, "openai-codex")


class CodexUsageTests(unittest.TestCase):
    def test_parse_codex_turn_completed_usage(self):
        stdout = "\n".join(
            [
                json.dumps({"type": "thread.started", "thread_id": "t1"}),
                json.dumps({"type": "turn.started"}),
                json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "OK"}}),
                json.dumps(
                    {
                        "type": "turn.completed",
                        "usage": {
                            "input_tokens": 15315,
                            "cached_input_tokens": 11008,
                            "cache_write_input_tokens": 0,
                            "output_tokens": 30,
                            "reasoning_output_tokens": 23,
                        },
                    }
                ),
            ]
        )
        usages = parse_codex_events(stdout)
        self.assertEqual(len(usages), 1)
        self.assertEqual(usages[0]["input_tokens"], 15315)
        self.assertEqual(usages[0]["cached_input_tokens"], 11008)
        self.assertEqual(usages[0]["output_tokens"], 30)
        self.assertEqual(usages[0]["reasoning_output_tokens"], 23)

    def test_codex_usage_totals_treats_input_as_cached_inclusive(self):
        usages = [
            {"input_tokens": 1000, "cached_input_tokens": 900, "cache_write_input_tokens": 0, "output_tokens": 10, "reasoning_output_tokens": 0},
            {"input_tokens": 500, "cached_input_tokens": 0, "cache_write_input_tokens": 500, "output_tokens": 5, "reasoning_output_tokens": 0},
        ]
        totals = codex_usage_totals(usages)
        self.assertEqual(totals.requests, 2)
        self.assertEqual(totals.input_tokens, 1500)
        self.assertEqual(totals.cached_input_tokens, 900)
        self.assertEqual(totals.uncached_input_tokens, 600)
        self.assertEqual(totals.total_tokens, 1515)

    def test_codex_usage_records_match_monitor_pricing_semantics(self):
        usages = [
            {"input_tokens": 1000, "cached_input_tokens": 400, "cache_write_input_tokens": 600, "output_tokens": 10, "reasoning_output_tokens": 0}
        ]
        records = codex_usage_records(usages, "gpt-5.6-luna", started_at=1.0, finished_at=2.0)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].input_tokens, 1000)
        self.assertEqual(records[0].cache_read_tokens, 400)
        self.assertEqual(records[0].raw_cache_write_tokens, 600)
        pricing = monitor.load_model_pricing(
            Path(__file__).resolve().parents[1] / "model-pricing.json.example"
        )
        breakdown = monitor.price_usage_record(records[0], pricing)
        self.assertEqual(breakdown.uncached_input_tokens, 600)
        self.assertEqual(breakdown.cached_input_tokens, 400)
        self.assertEqual(breakdown.total_cost, Decimal("0.000140"))

    def test_parse_codex_events_ignores_malformed_and_empty_usage(self):
        stdout = "\n".join(
            [
                "not json",
                json.dumps({"type": "turn.completed"}),
                json.dumps({"type": "turn.completed", "usage": {"input_tokens": 0, "output_tokens": 0}}),
                json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 1}}),
            ]
        )
        usages = parse_codex_events(stdout)
        self.assertEqual(len(usages), 1)
        self.assertEqual(usages[0]["input_tokens"], 10)

    def test_parser_accepts_codex_token_source_and_base_url(self):
        args = build_parser().parse_args(
            [
                "--harness", "codex",
                "--token-source", "codex",
                "--codex-base-url", "https://chatgpt.com/backend-api/codex",
            ]
        )
        self.assertEqual(args.token_source, "codex")
        self.assertEqual(args.codex_base_url, "https://chatgpt.com/backend-api/codex")

    def test_auto_prefers_headroom_over_codex_reported_usage(self):
        """codex turn.completed omits the preliminary request, so auto must
        prefer Headroom rows whenever the proxy produced them."""
        import benchmarks.percentage_benchmark as pb
        source = Path(pb.__file__).read_text(encoding="utf-8")
        self.assertIn("collect_headroom", source)
        # auto must not treat codex usage as the first choice
        self.assertNotIn('use_codex_usage = (', source)
        self.assertIn('args.token_source == "auto" and bypassing_proxy', source)
        self.assertIn('fallback_from', source)

    def test_result_flags_codex_undercount(self):
        import benchmarks.percentage_benchmark as pb
        source = Path(pb.__file__).read_text(encoding="utf-8")
        self.assertIn("codex_usage_reported_turns_only", source)
        self.assertIn("usage_note", source)
