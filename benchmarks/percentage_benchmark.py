#!/usr/bin/env python3
"""Benchmark Codex token usage against Headroom percentage buckets.

This is intentionally separate from ``monitor.py``.  It never writes the
monitor state or usage log.  A live run requires both ``--execute`` and
``--confirm-spend`` because it consumes the signed-in Codex allowance.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import monitor


DEFAULT_MODELS = (
    "gpt-5.6-luna",
    "gpt-5.6-terra",
    "gpt-5.6-sol",
    "gpt-6-astra",
)
DEFAULT_TARGET_POINTS = Decimal("0.1")
DEFAULT_CHUNK_TOKENS = 50_000
DEFAULT_MAX_ATTEMPTS = 100
DEFAULT_MAX_TOTAL_TOKENS = 25_000_000
DEFAULT_HEADROOM_WAIT = 30.0
DEFAULT_SETTLE_SECONDS = 1.0
DEFAULT_POLL_SECONDS = 0.25
DEFAULT_MAX_SECONDS = 3600.0
DEFAULT_FINE_WINDOW_FRACTION = Decimal("0.15")
RESET_TIMESTAMP_TOLERANCE_SECONDS = 60
DEFAULT_PI_COMMAND = "pi"
DEFAULT_PI_PROVIDER = "openai-codex"
DEFAULT_PI_SYSTEM_PROMPT = (
    "You are participating in a token-usage benchmark. Do not use tools. "
    "Read the delimited payload and respond with exactly BENCHMARK_OK."
)

FALLBACK_ALIASES = {
    "luna": "gpt-5.6-luna",
    "terra": "gpt-5.6-terra",
    "sol": "gpt-5.6-sol",
    "astra": "gpt-6-astra",
    "gpt-5-6-luna": "gpt-5.6-luna",
    "gpt-5-6-terra": "gpt-5.6-terra",
    "gpt-5-6-sol": "gpt-5.6-sol",
    "gpt-6": "gpt-6-astra",
}

PAYLOAD_WORDS = (
    "alpha bravo charlie delta echo foxtrot golf hotel india juliett kilo lima "
    "mike november oscar papa quebec romeo sierra tango uniform victor whiskey "
    "xray yankee zulu"
).split()


class BenchmarkError(RuntimeError):
    """Expected benchmark configuration or runtime error."""


@dataclass(frozen=True)
class Calibration:
    window: str
    source: str
    intervals: int
    tokens_per_point: Decimal | None
    model: str | None = None
    rejected_mixed_intervals: int = 0
    reason: str | None = None


@dataclass(frozen=True)
class PerfTotals:
    requests: int
    input_tokens: int
    cached_input_tokens: int
    uncached_input_tokens: int
    output_tokens: int

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

@dataclass(frozen=True)
class PiUsage:
    """Token and cost counters reported by one pi assistant message.

    pi reports ``input`` as uncached input and ``cacheRead`` separately; the
    monitor's ``HeadroomUsageRecord`` instead stores total input and cached
    input.  ``reasoning`` is informational because pi's ``output`` already
    includes it.
    """

    timestamp: float
    model: str
    input_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    output_tokens: int
    reasoning_tokens: int
    total_tokens: int
    cost_usd: Decimal



def decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def canonical_model(name: str, aliases: dict[str, str] | None = None) -> str:
    key = name.strip().lower()
    values = dict(FALLBACK_ALIASES)
    if aliases:
        values.update({str(k).lower(): str(v).lower() for k, v in aliases.items()})
    return values.get(key, key)


def parse_models(value: str) -> tuple[str, ...]:
    models = tuple(canonical_model(item) for item in value.split(",") if item.strip())
    if not models:
        raise argparse.ArgumentTypeError("at least one model is required")
    return models


def load_aliases() -> dict[str, str]:
    """Load aliases when the installed pricing file is available."""

    pricing_path = Path(
        os.getenv("MODEL_PRICING_FILE", "").strip()
        or monitor.default_pricing_path()
    ).expanduser()
    try:
        pricing = monitor.load_model_pricing(pricing_path)
    except Exception:
        return dict(FALLBACK_ALIASES)
    return {str(key): str(value) for key, value in pricing.aliases.items()}


def build_prompt(approx_tokens: int, nonce: str) -> str:
    """Build a tool-free, unique prompt with an approximate input size.

    The estimate is deliberately only a sizing hint.  The Headroom PERF record
    is the authoritative token count because the Codex system context and
    tokenizer overhead are not known to this script.
    """

    count = max(1, int(approx_tokens))
    repetitions = (count + len(PAYLOAD_WORDS) - 1) // len(PAYLOAD_WORDS)
    words = (PAYLOAD_WORDS * repetitions)[:count]
    return (
        "You are participating in a token-usage benchmark.\n"
        f"Benchmark nonce: {nonce}\n"
        "Do not use tools. Do not inspect, create, or modify files.\n"
        "Read the delimited payload, then respond with exactly BENCHMARK_OK.\n"
        "Do not explain your answer.\n\n"
        "--- PAYLOAD START ---\n"
        + " ".join(words)
        + "\n--- PAYLOAD END ---\n"
    )


def _counter(usage: dict[str, Any], key: str) -> int:
    try:
        return max(0, int(usage.get(key, 0)))
    except (AttributeError, TypeError, ValueError):
        return 0


def _crossing_token_total(row: dict[str, Any]) -> int:
    usage = row.get("cumulative_usage") if isinstance(row, dict) else None
    if not isinstance(usage, dict):
        return 0
    return _counter(usage, "input_tokens") + _counter(usage, "output_tokens")


def _crossing_model_totals(row: dict[str, Any]) -> dict[str, int]:
    usage = row.get("cumulative_usage") if isinstance(row, dict) else None
    models = usage.get("models") if isinstance(usage, dict) else None
    if not isinstance(models, dict):
        return {}
    return {
        str(model): _counter(values, "input_tokens") + _counter(values, "output_tokens")
        for model, values in models.items()
        if isinstance(values, dict)
    }


def estimate_tokens_per_point(
    state_path: Path,
    window: str,
    lookback: int = 12,
    *,
    model: str | None = None,
    aliases: dict[str, str] | None = None,
) -> Calibration:
    """Estimate tokens per point, using only exclusive model intervals when requested."""

    canonical = canonical_model(model, aliases) if model else None

    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return Calibration(window, "unavailable", 0, None, canonical, reason="state_unavailable")
    window_state = (
        state.get("accounting", {}).get("windows", {}).get(window, {})
        if isinstance(state, dict)
        else {}
    )
    crossings = window_state.get("crossings", []) if isinstance(window_state, dict) else []
    if not isinstance(crossings, list):
        return Calibration(window, "unavailable", 0, None, canonical, reason="crossings_unavailable")
    intervals: list[Decimal] = []
    rejected_mixed = 0
    rows = [row for row in crossings if isinstance(row, dict)]
    for earlier, later in zip(rows, rows[1:]):
        if not bool(earlier.get("reliable", True)) or not bool(later.get("reliable", True)):
            continue
        points = decimal(later.get("percent")) - decimal(earlier.get("percent"))
        if points <= 0:
            continue
        if canonical is None:
            tokens = _crossing_token_total(later) - _crossing_token_total(earlier)
        else:
            earlier_models = _crossing_model_totals(earlier)
            later_models = _crossing_model_totals(later)
            deltas = {
                name: later_models.get(name, 0) - earlier_models.get(name, 0)
                for name in set(earlier_models) | set(later_models)
            }
            active_models = {
                canonical_model(name, aliases): value
                for name, value in deltas.items()
                if value > 0
            }
            if set(active_models) != {canonical}:
                rejected_mixed += 1
                continue
            tokens = active_models[canonical]
        if tokens > 0:
            intervals.append(Decimal(tokens) / points)
    if lookback > 0:
        intervals = intervals[-lookback:]
    if not intervals:
        reason = "no_exclusive_model_intervals" if canonical else "no_completed_intervals"
        source = "unavailable_model_mixture" if canonical else "unavailable"
        return Calibration(
            window,
            source,
            0,
            None,
            canonical,
            rejected_mixed_intervals=rejected_mixed,
            reason=reason,
        )
    ordered = sorted(intervals)
    midpoint = len(ordered) // 2
    if len(ordered) % 2:
        median = ordered[midpoint]
    else:
        median = (ordered[midpoint - 1] + ordered[midpoint]) / Decimal("2")
    source = "state.crossings:model_exclusive" if canonical else "state.crossings"
    return Calibration(
        window,
        source,
        len(intervals),
        median,
        canonical,
        rejected_mixed_intervals=rejected_mixed,
    )


def parse_model_overrides(values: Iterable[str]) -> dict[str, Decimal]:
    overrides: dict[str, Decimal] = {}
    for value in values:
        if "=" not in value:
            raise BenchmarkError(f"invalid --model-tokens-per-point value: {value!r}")
        name, raw_tokens = value.split("=", 1)
        model = canonical_model(name)
        try:
            tokens = Decimal(raw_tokens)
        except InvalidOperation as exc:
            raise BenchmarkError(f"invalid token calibration for {name!r}: {raw_tokens!r}") from exc
        if tokens <= 0:
            raise BenchmarkError(f"token calibration for {name!r} must be greater than zero")
        overrides[model] = tokens
    return overrides


def build_calibrations(
    state_path: Path,
    window: str,
    models: Iterable[str],
    *,
    lookback: int,
    shared_override: Decimal | None = None,
    model_overrides: dict[str, Decimal] | None = None,
    aliases: dict[str, str] | None = None,
) -> dict[str, Calibration]:
    overrides = model_overrides or {}
    calibrations: dict[str, Calibration] = {}
    for raw_model in models:
        model = canonical_model(raw_model, aliases)
        if model in overrides:
            calibrations[model] = Calibration(
                window,
                "command_line:model_override",
                0,
                overrides[model],
                model,
            )
        elif shared_override is not None:
            calibrations[model] = Calibration(
                window,
                "command_line:shared_override",
                0,
                shared_override,
                model,
            )
        else:
            calibrations[model] = estimate_tokens_per_point(
                state_path,
                window,
                lookback,
                model=model,
                aliases=aliases,
            )
    return calibrations


def _snapshot_value(snapshot: monitor.Snapshot, window: str) -> Decimal:
    value = getattr(snapshot, window)
    if value is None or value.used_percent is None:
        raise BenchmarkError(f"Headroom did not return a {window} percentage")
    return Decimal(str(value.used_percent))


def _snapshot_reset(snapshot: monitor.Snapshot, window: str) -> int | None:
    value = getattr(snapshot, window)
    return value.resets_at if value is not None else None

def reset_advanced(
    baseline_reset: int | None,
    current_reset: int | None,
    *,
    tolerance: int = RESET_TIMESTAMP_TOLERANCE_SECONDS,
) -> bool:
    """Report a real window reset, ignoring the app-server's second-level drift.

    The reset timestamp is periodically re-rounded, so it can move by a few
    seconds without the window resetting.  A real reset advances it by the
    window duration (hours or days).
    """

    if baseline_reset is None or current_reset is None:
        return False
    return current_reset - baseline_reset > tolerance


def _perf_totals(records: Iterable[monitor.HeadroomUsageRecord]) -> PerfTotals:
    rows = list(records)
    input_tokens = sum(max(0, row.input_tokens) for row in rows)
    cached = sum(max(0, row.cache_read_tokens) for row in rows)
    output = sum(max(0, row.output_tokens) for row in rows)
    return PerfTotals(
        requests=len(rows),
        input_tokens=input_tokens,
        cached_input_tokens=cached,
        uncached_input_tokens=max(0, input_tokens - cached),
        output_tokens=output,
    )


def _json_totals(totals: PerfTotals) -> dict[str, int]:
    result = asdict(totals)
    result["total_tokens"] = totals.total_tokens
    return result


def _json_model_rows(period: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Keep benchmark attribution JSON-safe and aligned with monitor fields."""

    rows: dict[str, dict[str, Any]] = {}
    for model, raw in (period.get("models") or {}).items():
        if not isinstance(raw, dict):
            continue
        rows[str(model)] = {
            "requests": int(raw.get("requests", 0)),
            "input_tokens": int(raw.get("input_tokens", 0)),
            "cached_input_tokens": int(raw.get("cached_input_tokens", 0)),
            "uncached_input_tokens": int(raw.get("uncached_input_tokens", 0)),
            "output_tokens": int(raw.get("output_tokens", 0)),
            "known_cost_usd": str(raw.get("known_cost", "0")),
            "complete": bool(raw.get("complete", False)),
            "long_context_requests": int(raw.get("long_context_requests", 0)),
        }
    return rows


def choose_chunk_tokens(
    *,
    base_chunk: int,
    fine_chunk: int | None,
    fine_window_fraction: Decimal | None,
    expected_tokens_per_point: Decimal | None,
    consumed_since_last_crossing: Decimal,
) -> int:
    """Pick the next request size, switching to fine chunks near a boundary.

    The account percentage only moves in whole integer points, so the crossing
    bracket is bounded by the request that contained it.  Once a model has a
    point-size estimate, shrinking the last few requests narrows that bracket
    without paying for small chunks over the whole point.
    """

    if fine_chunk is None or fine_window_fraction is None or expected_tokens_per_point is None:
        return base_chunk
    if expected_tokens_per_point <= 0:
        return base_chunk
    remaining = expected_tokens_per_point - consumed_since_last_crossing
    threshold = fine_window_fraction * expected_tokens_per_point
    if remaining <= threshold:
        return max(1, min(base_chunk, fine_chunk))
    return base_chunk

def bracket_uncertainty_tokens_per_point(
    brackets: Iterable[int], observed_delta_points: Decimal
) -> Decimal | None:
    """Quantization uncertainty from the requests that contained crossings.

    Each crossing happened somewhere inside one request, so that request's
    token count bounds the error for that point.  Dividing the summed brackets
    by the number of observed points keeps the figure per-point comparable.
    """

    if observed_delta_points <= 0:
        return None
    total = sum(max(0, int(item)) for item in brackets)
    return Decimal(total) / observed_delta_points

def build_pi_command(
    model: str,
    *,
    pi_command: str,
    provider: str | None,
    thinking: str | None,
    system_prompt: str | None,
    keep_extensions: bool,
    extra_args: str | None,
) -> list[str]:
    """Build a one-shot, tool-free pi invocation in JSON event mode."""

    command = shlex.split(pi_command)
    if not command:
        raise BenchmarkError("--pi-command is empty")
    command.extend(
        [
            "--mode",
            "json",
            "--no-session",
            "--no-tools",
            "--no-context-files",
            "--no-skills",
            "--no-prompt-templates",
        ]
    )
    if not keep_extensions:
        command.append("--no-extensions")
    if provider:
        command.extend(["--provider", provider])
    qualified = model if "/" in model or not provider else f"{provider}/{model}"
    command.extend(["--model", qualified])
    if thinking:
        command.extend(["--thinking", thinking])
    if system_prompt:
        command.extend(["--system-prompt", system_prompt])
    if extra_args:
        command.extend(shlex.split(extra_args))
    return command

def _pi_int(raw: Any) -> int:
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 0

def _pi_cost(usage: dict[str, Any]) -> Decimal:
    cost = usage.get("cost")
    if isinstance(cost, dict):
        return decimal(cost.get("total", 0))
    return decimal(cost or 0)

def parse_pi_events(stdout: str, aliases: dict[str, str] | None = None) -> list[PiUsage]:
    """Extract assistant usage records from pi ``--mode json`` stdout.

    ``message_end`` is the authoritative final message event.  Duplicate
    records are dropped because a single assistant response can surface through
    more than one event type.
    """

    usages: list[PiUsage] = []
    seen: set[tuple[Any, ...]] = set()
    fallback_messages: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if kind == "message_end":
            message = event.get("message")
            if isinstance(message, dict) and message.get("role") == "assistant":
                usage = _pi_usage_from_message(message, aliases)
                if usage is not None and _pi_usage_key(usage) not in seen:
                    seen.add(_pi_usage_key(usage))
                    usages.append(usage)
        elif kind == "agent_end":
            for message in event.get("messages") or []:
                if isinstance(message, dict) and message.get("role") == "assistant":
                    fallback_messages.append(message)
    if not usages:
        for message in fallback_messages:
            usage = _pi_usage_from_message(message, aliases)
            if usage is not None and _pi_usage_key(usage) not in seen:
                seen.add(_pi_usage_key(usage))
                usages.append(usage)
    return usages

def _pi_usage_key(usage: PiUsage) -> tuple[Any, ...]:
    return (
        usage.timestamp,
        usage.model,
        usage.input_tokens,
        usage.cache_read_tokens,
        usage.output_tokens,
        usage.total_tokens,
    )

def _pi_usage_from_message(
    message: dict[str, Any], aliases: dict[str, str] | None
) -> PiUsage | None:
    usage = message.get("usage")
    if not isinstance(usage, dict):
        return None
    input_tokens = _pi_int(usage.get("input"))
    cache_read = _pi_int(usage.get("cacheRead"))
    output = _pi_int(usage.get("output"))
    total = _pi_int(usage.get("totalTokens")) or (input_tokens + cache_read + output)
    if total <= 0 and input_tokens <= 0 and output <= 0:
        return None
    model = str(message.get("model") or "")
    timestamp = message.get("timestamp")
    if isinstance(timestamp, (int, float)) and timestamp > 1_000_000_000_000:
        timestamp = float(timestamp) / 1000.0
    try:
        timestamp_value = float(timestamp)
    except (TypeError, ValueError):
        timestamp_value = time.time()
    return PiUsage(
        timestamp=timestamp_value,
        model=canonical_model(model, aliases) if model else model,
        input_tokens=input_tokens,
        cache_read_tokens=cache_read,
        cache_write_tokens=_pi_int(usage.get("cacheWrite")),
        output_tokens=output,
        reasoning_tokens=_pi_int(usage.get("reasoning")),
        total_tokens=total,
        cost_usd=_pi_cost(usage),
    )

def pi_usage_records(usages: Iterable[PiUsage]) -> list[monitor.HeadroomUsageRecord]:
    """Convert pi-reported counters into monitor records for pricing.

    ``cacheWrite`` is kept as the raw diagnostic counter and never added to
    billable input, matching how the monitor treats Headroom's inferred writes.
    """

    records: list[monitor.HeadroomUsageRecord] = []
    for usage in usages:
        records.append(
            monitor.HeadroomUsageRecord(
                timestamp=usage.timestamp,
                model=usage.model,
                input_tokens=usage.input_tokens + usage.cache_read_tokens,
                output_tokens=usage.output_tokens,
                cache_read_tokens=usage.cache_read_tokens,
                raw_cache_write_tokens=usage.cache_write_tokens,
                request_id=f"pi-{usage.timestamp}-{len(records)}",
                client="pi",
                cache_write_inferred=False,
                quality_flags=("pi_reported",),
            )
        )
    return records

def pi_usage_totals(usages: Iterable[PiUsage]) -> PerfTotals:
    rows = list(usages)
    uncached = sum(row.input_tokens for row in rows)
    cached = sum(row.cache_read_tokens for row in rows)
    output = sum(row.output_tokens for row in rows)
    return PerfTotals(
        requests=len(rows),
        input_tokens=uncached + cached,
        cached_input_tokens=cached,
        uncached_input_tokens=uncached,
        output_tokens=output,
    )

def pi_usage_cost(usages: Iterable[PiUsage]) -> Decimal:
    """Sum pi's own catalog-based cost for the reported messages."""

    return sum((row.cost_usd for row in usages), Decimal("0"))

def command_summary(command_result: dict[str, Any]) -> dict[str, Any]:
    """Drop parsed usage objects so a command result stays JSON-serializable."""

    return {key: value for key, value in command_result.items() if key != "pi_usages"}

def _collect_after_command(
    log_dir: Path,
    start_at: float,
    cursor: dict[str, Any],
    *,
    client: str | None,
    timezone_name: str | None,
    wait_seconds: float,
    settle_seconds: float,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
) -> tuple[list[monitor.HeadroomUsageRecord], dict[str, Any], dict[str, Any]]:
    deadline = time.monotonic() + wait_seconds
    quiet_deadline: float | None = None
    records: list[monitor.HeadroomUsageRecord] = []
    diagnostics: dict[str, Any] = {}
    while time.monotonic() < deadline:
        batch, cursor, diagnostics = monitor.collect_headroom_usage(
            log_dir,
            start_at,
            time.time(),
            cursor,
            client=client,
            timezone_name=timezone_name,
            # The benchmark measures whichever model it was pointed at, so the
            # Codex quota exclusion list must not apply here: otherwise a
            # deliberately benchmarked third-party model would silently
            # collect zero records.
            excluded_models=(),
        )
        if batch:
            records.extend(batch)
            quiet_deadline = time.monotonic() + settle_seconds
        elif records and quiet_deadline is not None and time.monotonic() >= quiet_deadline:
            break
        time.sleep(max(0.05, poll_seconds))
    return records, cursor, diagnostics


def parse_codex_events(stdout: str) -> list[dict[str, Any]]:
    """Extract token usage from codex ``exec --json`` ``turn.completed`` events.

    codex reports ``input_tokens`` as the total input (cached inclusive),
    ``cached_input_tokens`` as the cached subset, and ``output_tokens`` as the
    final output.  Verified against a matching Headroom PERF row: codex's
    ``input_tokens`` equals Headroom's ``tok_after`` and ``cached_input_tokens``
    equals ``cache_read``.
    """

    usages: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("type") != "turn.completed":
            continue
        usage = event.get("usage")
        if not isinstance(usage, dict):
            continue
        input_tokens = _pi_int(usage.get("input_tokens"))
        cached = _pi_int(usage.get("cached_input_tokens"))
        output = _pi_int(usage.get("output_tokens"))
        if input_tokens <= 0 and output <= 0:
            continue
        usages.append(
            {
                "input_tokens": input_tokens,
                "cached_input_tokens": cached,
                "cache_write_input_tokens": _pi_int(usage.get("cache_write_input_tokens")),
                "output_tokens": output,
                "reasoning_output_tokens": _pi_int(usage.get("reasoning_output_tokens")),
            }
        )
    return usages

def codex_usage_records(
    usages: Iterable[dict[str, Any]],
    model: str,
    *,
    started_at: float,
    finished_at: float,
) -> list[monitor.HeadroomUsageRecord]:
    """Convert codex-reported usage into monitor records for pricing."""

    rows = list(usages)
    records: list[monitor.HeadroomUsageRecord] = []
    for index, usage in enumerate(rows):
        records.append(
            monitor.HeadroomUsageRecord(
                timestamp=finished_at,
                model=model,
                input_tokens=int(usage["input_tokens"]),
                output_tokens=int(usage["output_tokens"]),
                cache_read_tokens=int(usage["cached_input_tokens"]),
                raw_cache_write_tokens=int(usage["cache_write_input_tokens"]),
                request_id=f"codex-{started_at}-{index}",
                client="codex",
                cache_write_inferred=False,
                quality_flags=("codex_reported",),
            )
        )
    return records

def codex_usage_totals(usages: Iterable[dict[str, Any]]) -> PerfTotals:
    rows = list(usages)
    total_input = sum(int(row["input_tokens"]) for row in rows)
    cached = sum(int(row["cached_input_tokens"]) for row in rows)
    output = sum(int(row["output_tokens"]) for row in rows)
    return PerfTotals(
        requests=len(rows),
        input_tokens=total_input,
        cached_input_tokens=cached,
        uncached_input_tokens=max(0, total_input - cached),
        output_tokens=output,
    )

def _run_codex(
    model: str,
    prompt: str,
    workdir: Path,
    *,
    codex_command: str,
    timeout_seconds: float,
    reasoning_effort: str | None,
    base_url: str | None = None,
) -> dict[str, Any]:
    command = shlex.split(codex_command)
    if not command:
        raise BenchmarkError("--codex-command is empty")
    command.extend(
        [
            "exec",
            "--json",
            "--ephemeral",
            "--model",
            model,
            "--sandbox",
            "read-only",
            "--skip-git-repo-check",
            "--color",
            "never",
            "--cd",
            str(workdir),
        ]
    )
    if reasoning_effort:
        command.extend(["-c", f'model_reasoning_effort="{reasoning_effort}"'])
    if base_url:
        # Bypass the configured Headroom proxy so the request goes straight to
        # the upstream endpoint.
        command.extend(["-c", f'openai_base_url="{base_url}"'])
    started = time.time()
    try:
        completed = subprocess.run(
            command,
            input=prompt,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "returncode": None,
            "timed_out": True,
            "started_at": started,
            "finished_at": time.time(),
            "stdout_bytes": len((exc.stdout or "").encode("utf-8")) if exc.stdout else 0,
            "stderr_tail": str(exc.stderr or "")[-1000:],
            "codex_usages": [],
            "base_url": base_url,
        }
    return {
        "returncode": completed.returncode,
        "timed_out": False,
        "started_at": started,
        "finished_at": time.time(),
        "stdout_bytes": len(completed.stdout.encode("utf-8")),
        "stderr_tail": completed.stderr[-1000:],
        "codex_usages": parse_codex_events(completed.stdout),
        "base_url": base_url,
    }

def _run_pi(
    model: str,
    prompt: str,
    workdir: Path,
    *,
    pi_command: str,
    provider: str | None,
    timeout_seconds: float,
    thinking: str | None,
    system_prompt: str | None,
    keep_extensions: bool,
    extra_args: str | None,
    aliases: dict[str, str],
) -> dict[str, Any]:
    """Run one tool-free pi turn and return its reported token usage.

    The payload is piped through stdin; pi prepends piped stdin to the first
    prompt, which keeps very large payloads out of the process arguments.
    """

    command = build_pi_command(
        model,
        pi_command=pi_command,
        provider=provider,
        thinking=thinking,
        system_prompt=system_prompt,
        keep_extensions=keep_extensions,
        extra_args=extra_args,
    )
    started = time.time()
    try:
        completed = subprocess.run(
            command,
            input=prompt,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            cwd=str(workdir),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "returncode": None,
            "timed_out": True,
            "started_at": started,
            "finished_at": time.time(),
            "stdout_bytes": len((exc.stdout or "").encode("utf-8")) if exc.stdout else 0,
            "stderr_tail": str(exc.stderr or "")[-1000:],
            "pi_usages": [],
            "pi_models": [],
            "parse_error": None,
        }
    usages = parse_pi_events(completed.stdout, aliases)
    return {
        "returncode": completed.returncode,
        "timed_out": False,
        "started_at": started,
        "finished_at": time.time(),
        "stdout_bytes": len(completed.stdout.encode("utf-8")),
        "stderr_tail": completed.stderr[-1000:],
        "pi_usages": usages,
        "pi_models": sorted({usage.model for usage in usages}),
        "parse_error": None if usages or completed.returncode == 0 else "no_usage_events",
    }


def _decimal_arg(value: str) -> Decimal:
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError(f"invalid decimal: {value}") from exc
    if result <= 0:
        raise argparse.ArgumentTypeError("value must be greater than zero")
    return result


def _positive_int(value: str) -> int:
    try:
        result = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid integer: {value}") from exc
    if result <= 0:
        raise argparse.ArgumentTypeError("value must be greater than zero")
    return result


def _calibration_payload(
    calibration: Calibration,
    target_points: Decimal,
    target_tokens_override: int | None = None,
) -> dict[str, Any]:
    target_tokens = (
        Decimal(target_tokens_override)
        if target_tokens_override is not None
        else (
            calibration.tokens_per_point * target_points
            if calibration.tokens_per_point is not None
            else None
        )
    )
    return {
        "source": calibration.source,
        "intervals": calibration.intervals,
        "tokens_per_point": (
            str(calibration.tokens_per_point)
            if calibration.tokens_per_point is not None
            else None
        ),
        "target_tokens": str(target_tokens) if target_tokens is not None else None,
        "rejected_mixed_intervals": calibration.rejected_mixed_intervals,
        "reason": calibration.reason,
    }


def _plan(
    args: argparse.Namespace,
    calibrations: dict[str, Calibration],
    models: tuple[str, ...],
) -> dict[str, Any]:
    return {
        "window": args.window,
        "target_percentage_points": str(args.target_points),
        "mode": args.mode,
        "harness": args.harness,
        "token_source": args.token_source,
        "token_source_resolution": (
            "pi usage events, else Headroom rows, else codex turn.completed"
            if args.token_source == "auto"
            else args.token_source
        ),
        "models": list(models),
        "calibration_by_model": {
            model: _calibration_payload(
                calibration,
                args.target_points,
                args.target_tokens,
            )
            for model, calibration in calibrations.items()
        },
        "granularity": {
            "account_percentage_unit": "integer_percentage_point",
            "accumulate_points": args.accumulate_points,
            "chunk_tokens": args.chunk_tokens,
            "fine_chunk_tokens": args.fine_chunk_tokens,
            "fine_window_fraction": (
                str(args.fine_window_fraction)
                if args.fine_chunk_tokens is not None
                else None
            ),
            "snapshot_poll_seconds": args.poll_seconds,
            "note": (
                "Each crossing is bracketed by the request that contained it; "
                "accumulating N points divides that bracket by N."
            ),
        },
        "headroom_percentage_granularity": "integer_buckets",
        "target_point_is_estimated": True,
        "monitor_state_modified": False,
        "monitor_usage_log_modified": False,
    }


def run_model(
    model: str,
    args: argparse.Namespace,
    calibration: Calibration,
    aliases: dict[str, str],
    pricing: monitor.PricingTable,
    cursor: dict[str, Any],
    workdir: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    model = canonical_model(model, aliases)
    if (
        calibration.tokens_per_point is None
        and args.target_tokens is None
        and args.mode != "crossing"
    ):
        raise BenchmarkError(
            f"no exclusive crossing calibration found for {model}; "
            "pass --model-tokens-per-point or --target-tokens"
        )
    target_tokens = args.target_tokens or (
        calibration.tokens_per_point * args.target_points
        if calibration.tokens_per_point is not None
        else None
    )
    before = monitor.query_snapshot()
    baseline_percent = _snapshot_value(before, args.window)
    baseline_reset = _snapshot_reset(before, args.window)
    started_at = time.time()
    consumed = 0
    attempts: list[dict[str, Any]] = []
    all_records: list[monitor.HeadroomUsageRecord] = []
    current_snapshot = before
    contamination: list[str] = []
    status = "max_attempts_reached"
    crossings: list[dict[str, Any]] = []
    previous_percent = baseline_percent
    consumed_at_last_crossing = Decimal("0")
    expected_tokens_per_point = calibration.tokens_per_point
    pi_cost_total = Decimal("0")
    monitor_cost_total = Decimal("0")
    last_token_source = args.token_source

    for attempt_number in range(1, args.max_attempts + 1):
        chunk_tokens = choose_chunk_tokens(
            base_chunk=args.chunk_tokens,
            fine_chunk=args.fine_chunk_tokens,
            fine_window_fraction=args.fine_window_fraction,
            expected_tokens_per_point=expected_tokens_per_point,
            consumed_since_last_crossing=Decimal(consumed) - consumed_at_last_crossing,
        )
        if args.mode == "midpoint":
            assert target_tokens is not None
            remaining = max(Decimal("1"), target_tokens - Decimal(consumed))
            approx_prompt_tokens = min(chunk_tokens, max(1, int(remaining)))
        else:
            # Crossing mode bootstraps models without an exclusive historical
            # interval. The observed Headroom increase, not this estimate, is
            # the stop condition.
            approx_prompt_tokens = chunk_tokens
        prompt = build_prompt(approx_prompt_tokens, f"{model}-{attempt_number}-{time.time_ns()}")
        if args.harness == "pi":
            command_result = _run_pi(
                model,
                prompt,
                workdir,
                pi_command=args.pi_command,
                provider=args.pi_provider,
                timeout_seconds=args.request_timeout,
                thinking=args.thinking,
                system_prompt=args.pi_system_prompt,
                keep_extensions=args.pi_keep_extensions,
                extra_args=args.pi_extra_args,
                aliases=aliases,
            )
        else:
            command_result = _run_codex(
                model,
                prompt,
                workdir,
                codex_command=args.codex_command,
                timeout_seconds=args.request_timeout,
                reasoning_effort=args.reasoning_effort,
                base_url=args.codex_base_url,
            )
        codex_usages = command_result.get("codex_usages") or []
        pi_usages = [
            usage
            for usage in (command_result.get("pi_usages") or [])
            if usage.model == model
        ]
        use_pi_usage = (
            args.token_source in ("pi", "auto")
            and bool(pi_usages)
        )
        # codex ``turn.completed`` reports only the main turn.  A codex ``exec``
        # issues a second, smaller preliminary request that never appears in the
        # JSON stream, so a codex-reported count is always low.  Headroom logs
        # every request, so ``auto`` prefers it whenever it has records.
        bypassing_proxy = bool(args.codex_base_url)
        collect_headroom = (
            not use_pi_usage
            and args.token_source != "codex"
            and args.token_source != "pi"
            and not (args.token_source == "auto" and bypassing_proxy)
        )
        pi_usage_missing = args.token_source == "pi" and not pi_usages
        codex_usage_missing = args.token_source == "codex" and not codex_usages
        if use_pi_usage:
            last_token_source = "pi"
            records: list[monitor.HeadroomUsageRecord] = []
            canonical_records = pi_usage_records(pi_usages)
            other_models = sorted(
                {
                    usage.model
                    for usage in (command_result.get("pi_usages") or [])
                    if usage.model != model
                }
            )
            diagnostics = {
                "source": "pi_usage_events",
                "messages": len(pi_usages),
                "models": sorted({usage.model for usage in pi_usages}),
            }
            attempt_pi_cost = pi_usage_cost(pi_usages)
        elif pi_usage_missing:
            last_token_source = "pi"
            records = []
            canonical_records = []
            other_models = []
            diagnostics = {
                "source": "pi_usage_events",
                "messages": 0,
                "error": command_result.get("parse_error") or "no_pi_usage_events",
            }
            attempt_pi_cost = Decimal("0")
        elif codex_usage_missing:
            last_token_source = "codex"
            records = []
            canonical_records = []
            other_models = []
            diagnostics = {
                "source": "codex_usage_events",
                "turns": 0,
                "error": "no_codex_usage_events",
            }
            attempt_pi_cost = Decimal("0")
        elif collect_headroom:
            last_token_source = "headroom"
            records, cursor, diagnostics = _collect_after_command(
                args.headroom_log_dir,
                command_result["started_at"],
                cursor,
                client=args.headroom_client,
                timezone_name=args.headroom_timezone,
                wait_seconds=args.headroom_wait,
                settle_seconds=args.settle_seconds,
                poll_seconds=args.poll_seconds,
            )
            canonical_records = [
                record for record in records if canonical_model(record.model, aliases) == model
            ]
            other_models = sorted(
                {canonical_model(record.model, aliases) for record in records if canonical_model(record.model, aliases) != model}
            )
            attempt_pi_cost = Decimal("0")
            if not canonical_records and codex_usages:
                # No proxied rows (proxy bypassed, down, or not logging): fall
                # back to the codex-reported count and record the shortfall.
                last_token_source = "codex"
                canonical_records = codex_usage_records(
                    codex_usages,
                    model,
                    started_at=command_result["started_at"],
                    finished_at=command_result["finished_at"],
                )
                diagnostics = {
                    "source": "codex_usage_events",
                    "turns": len(codex_usages),
                    "base_url": command_result.get("base_url"),
                    "fallback_from": "headroom",
                }
        elif codex_usages:
            last_token_source = "codex"
            records = []
            canonical_records = codex_usage_records(
                codex_usages,
                model,
                started_at=command_result["started_at"],
                finished_at=command_result["finished_at"],
            )
            other_models = []
            diagnostics = {
                "source": "codex_usage_events",
                "turns": len(codex_usages),
                "base_url": command_result.get("base_url"),
            }
            attempt_pi_cost = Decimal("0")
        else:
            last_token_source = "codex"
            records = []
            canonical_records = []
            other_models = []
            diagnostics = {
                "source": "codex_usage_events",
                "turns": 0,
                "error": "no_codex_usage_events",
            }
            attempt_pi_cost = Decimal("0")
        all_records.extend(canonical_records)
        contamination.extend(item for item in other_models if item not in contamination)
        totals = (
            pi_usage_totals(pi_usages)
            if use_pi_usage
            else codex_usage_totals(codex_usages)
            if last_token_source == "codex"
            else _perf_totals(canonical_records)
        )
        consumed += totals.total_tokens
        pi_cost_total += attempt_pi_cost
        attempt_period = monitor.aggregate_period(
            canonical_records,
            pricing,
            command_result["started_at"],
            time.time(),
            diagnostics,
        )
        attempt_cost = decimal(attempt_period["total"].get("known_cost", Decimal("0")))
        monitor_cost_total += attempt_cost
        try:
            current_snapshot = monitor.query_snapshot()
            current_percent = _snapshot_value(current_snapshot, args.window)
            current_reset = _snapshot_reset(current_snapshot, args.window)
        except Exception as exc:
            current_percent = None
            current_reset = None
            snapshot_error = str(exc)
        else:
            snapshot_error = None
        command_summary_dict = command_summary(command_result)
        attempt = {
            "attempt": attempt_number,
            "approx_prompt_tokens": approx_prompt_tokens,
            "chunk_tokens": chunk_tokens,
            "token_source": last_token_source,
            "command": command_summary_dict,
            "headroom": {
                "records": len(records),
                "matching_records": len(canonical_records),
                "other_models": other_models,
                "diagnostics": diagnostics,
            },
            "tokens": _json_totals(totals),
            "cost_usd": str(attempt_cost),
            "pi_cost_usd": str(attempt_pi_cost),
            "long_context_requests": sum(
                int(row.get("long_context_requests", 0))
                for row in attempt_period.get("models", {}).values()
            ),
            "after_percent": str(current_percent) if current_percent is not None else None,
            "estimated_progress_points": str(
                Decimal(consumed) / calibration.tokens_per_point
                if calibration.tokens_per_point
                else Decimal("0")
            ),
            "snapshot_error": snapshot_error,
        }
        attempts.append(attempt)
        if command_result["timed_out"] or command_result["returncode"] not in (0, None):
            status = f"{args.harness}_request_failed"
            break
        if not canonical_records:
            status = (
                "no_pi_usage_recorded"
                if last_token_source == "pi"
                else "no_matching_headroom_record"
            )
            break
        if current_percent is None:
            status = "headroom_snapshot_failed"
            break
        if reset_advanced(baseline_reset, current_reset):
            status = "window_reset_detected"
            break
        if current_percent < baseline_percent:
            status = "usage_decreased_during_run"
            break
        if contamination:
            status = "other_model_usage_detected"
            break
        if current_percent > previous_percent:
            delta_points = current_percent - previous_percent
            crossings.append(
                {
                    "percent": str(current_percent),
                    "delta_points": str(delta_points),
                    "cumulative_tokens": consumed,
                    "cumulative_cost_usd": str(monitor_cost_total),
                    "bracket_tokens": totals.total_tokens,
                    "bracket_cost_usd": str(attempt_cost),
                }
            )
            segment_tokens = Decimal(consumed) - consumed_at_last_crossing
            if delta_points > 0 and expected_tokens_per_point is None:
                expected_tokens_per_point = segment_tokens / delta_points
            consumed_at_last_crossing = Decimal(consumed)
            previous_percent = current_percent
        if current_percent - baseline_percent >= args.accumulate_points:
            status = "percentage_crossing_observed"
            break
        if args.mode == "midpoint" and Decimal(consumed) >= target_tokens:
            status = "estimated_midpoint_reached"
            break
        if consumed >= args.max_total_tokens:
            status = "max_total_tokens_reached"
            break
        if time.time() - started_at >= args.max_seconds:
            status = "max_seconds_reached"
            break

    final_percent = _snapshot_value(current_snapshot, args.window)
    final_reset = _snapshot_reset(current_snapshot, args.window)
    total = PerfTotals(
        requests=sum(int(item["tokens"]["requests"]) for item in attempts),
        input_tokens=sum(int(item["tokens"]["input_tokens"]) for item in attempts),
        cached_input_tokens=sum(int(item["tokens"]["cached_input_tokens"]) for item in attempts),
        uncached_input_tokens=sum(int(item["tokens"]["uncached_input_tokens"]) for item in attempts),
        output_tokens=sum(int(item["tokens"]["output_tokens"]) for item in attempts),
    )
    final_period = monitor.aggregate_period(
        all_records,
        pricing,
        started_at,
        time.time(),
    )
    observed_delta = final_percent - baseline_percent
    observed_tokens_per_point = (
        Decimal(total.total_tokens) / observed_delta
        if observed_delta > 0
        else None
    )
    observed_cost = decimal(final_period["total"].get("known_cost", "0"))
    observed_cost_per_point = (
        observed_cost / observed_delta
        if observed_delta > 0
        else None
    )
    uncertainty_per_point = bracket_uncertainty_tokens_per_point(
        (int(item["bracket_tokens"]) for item in crossings),
        observed_delta,
    )
    result = {
        "model": model,
        "window": args.window,
        "harness": args.harness,
        "token_source": last_token_source,
        "codex_usage_reported_turns_only": last_token_source == "codex",
        "usage_note": (
            "codex turn.completed omits the preliminary request each exec issues, "
            "so this count is low; use --token-source headroom for the full count"
            if last_token_source == "codex"
            else None
        ),
        "started_at": dt.datetime.fromtimestamp(started_at, dt.timezone.utc).isoformat(),
        "finished_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "baseline_percent": str(baseline_percent),
        "final_percent": str(final_percent),
        "observed_delta_points": str(final_percent - baseline_percent),
        "baseline_reset_at": baseline_reset,
        "final_reset_at": final_reset,
        "target_percentage_points": str(args.target_points),
        "target_tokens": str(target_tokens) if target_tokens is not None else None,
        "tokens": _json_totals(total),
        "cost_usd": str(final_period["total"].get("known_cost", Decimal("0"))),
        "pi_reported_cost_usd": str(pi_cost_total),
        "cost_complete": bool(final_period["total"].get("complete", False)),
        "models": _json_model_rows(final_period),
        "pricing_revision": pricing.revision,
        "crossings": crossings,
        "quantization_uncertainty_tokens_per_point": (
            str(uncertainty_per_point) if uncertainty_per_point is not None else None
        ),
        "expected_tokens_per_point": (
            str(expected_tokens_per_point) if expected_tokens_per_point is not None else None
        ),
        "long_context_requests": sum(
            int(row.get("long_context_requests", 0))
            for row in final_period.get("models", {}).values()
        ),
        "estimated_progress_points": (
            str(Decimal(total.total_tokens) / calibration.tokens_per_point)
            if calibration.tokens_per_point
            else None
        ),
        "observed_tokens_per_point": (
            str(observed_tokens_per_point)
            if observed_tokens_per_point is not None
            else None
        ),
        "observed_target_tokens": (
            str(observed_tokens_per_point * args.target_points)
            if observed_tokens_per_point is not None
            else None
        ),
        "observed_cost_per_point_usd": (
            str(observed_cost_per_point)
            if observed_cost_per_point is not None
            else None
        ),
        "observed_target_cost_usd": (
            str(observed_cost_per_point * args.target_points)
            if observed_cost_per_point is not None
            else None
        ),
        "calibration": _calibration_payload(
            calibration,
            args.target_points,
            args.target_tokens,
        ),
        "status": status,
        "contamination_models": contamination,
        "attempts": attempts,
    }
    return result, cursor


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", type=parse_models, default=DEFAULT_MODELS, help="comma-separated model IDs or aliases")
    parser.add_argument("--window", choices=("primary", "secondary"), default="secondary")
    parser.add_argument("--target-points", type=_decimal_arg, default=DEFAULT_TARGET_POINTS)
    parser.add_argument("--target-tokens", type=_positive_int, help="override token target; skips calibration")
    parser.add_argument(
        "--tokens-per-point",
        type=_decimal_arg,
        help="explicit shared override for every model (not inferred per model)",
    )
    parser.add_argument(
        "--model-tokens-per-point",
        action="append",
        default=[],
        metavar="MODEL=TOKENS",
        help="explicit per-model token calibration; may be repeated",
    )
    parser.add_argument("--calibration-lookback", type=_positive_int, default=12)
    parser.add_argument("--chunk-tokens", type=_positive_int, default=DEFAULT_CHUNK_TOKENS)
    parser.add_argument("--max-attempts", type=_positive_int, default=DEFAULT_MAX_ATTEMPTS)
    parser.add_argument(
        "--max-total-tokens",
        type=_positive_int,
        default=DEFAULT_MAX_TOTAL_TOKENS,
        help="safety budget for one model run in crossing mode",
    )
    parser.add_argument(
        "--mode",
        choices=("midpoint", "crossing"),
        default="crossing",
        help="crossing bootstraps uncalibrated models and stops at a Headroom increase",
    )
    parser.add_argument(
        "--harness",
        choices=("codex", "pi"),
        default="codex",
        help="which agent CLI sends the benchmark requests",
    )
    parser.add_argument(
        "--token-source",
        choices=("auto", "headroom", "pi", "codex"),
        default="auto",
        help=(
            "where token counts come from: Headroom PERF rows, pi's own "
            "session usage events, or pi usage with a Headroom fallback"
        ),
    )
    parser.add_argument(
        "--accumulate-points",
        type=_positive_int,
        default=1,
        help=(
            "keep burning until this many integer percentage points have "
            "crossed; the summed tokens are divided by the observed points"
        ),
    )
    parser.add_argument(
        "--fine-chunk-tokens",
        type=_positive_int,
        default=None,
        help="smaller request size used near an expected percentage boundary",
    )
    parser.add_argument(
        "--fine-window-fraction",
        type=_decimal_arg,
        default=DEFAULT_FINE_WINDOW_FRACTION,
        help="fraction of the expected point size that triggers fine chunks",
    )
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=DEFAULT_POLL_SECONDS,
        help="Headroom/pi capture polling interval in seconds",
    )
    parser.add_argument(
        "--max-seconds",
        type=float,
        default=DEFAULT_MAX_SECONDS,
        help="wall-clock safety limit for one model run",
    )
    parser.add_argument("--execute", action="store_true", help="send real Codex requests")
    parser.add_argument("--confirm-spend", action="store_true", help="confirm that this run may consume allowance")
    parser.add_argument("--reasoning-effort", help="optional fixed Codex reasoning effort")
    parser.add_argument(
        "--pi-command",
        default=None,
        help="pi executable; defaults to the executable in PI_COMMAND",
    )
    parser.add_argument(
        "--pi-provider",
        default=DEFAULT_PI_PROVIDER,
        help="provider used to qualify bare model ids for the pi harness",
    )
    parser.add_argument(
        "--thinking",
        choices=("off", "minimal", "low", "medium", "high", "xhigh", "max"),
        default=None,
        help="optional fixed pi thinking level; omitted preserves the pi configuration",
    )
    parser.add_argument(
        "--pi-system-prompt",
        default=DEFAULT_PI_SYSTEM_PROMPT,
        help="system prompt for pi benchmark requests; an empty value disables the override",
    )
    parser.add_argument(
        "--pi-keep-extensions",
        action="store_true",
        help="load discovered pi extensions instead of passing --no-extensions",
    )
    parser.add_argument(
        "--pi-extra-args",
        default=None,
        help="extra shell-split arguments appended to the pi command",
    )
    parser.add_argument("--request-timeout", type=float, default=900.0)
    parser.add_argument("--headroom-wait", type=float, default=DEFAULT_HEADROOM_WAIT)
    parser.add_argument("--settle-seconds", type=float, default=DEFAULT_SETTLE_SECONDS)
    parser.add_argument("--headroom-log-dir", type=Path, default=None)
    parser.add_argument("--headroom-client", default=None)
    parser.add_argument("--headroom-timezone", default=None)
    parser.add_argument("--state-path", type=Path, default=monitor.default_state_path())
    parser.add_argument("--env-file", type=Path, default=monitor.default_env_path())
    parser.add_argument(
        "--codex-command",
        default=None,
        help="Codex executable; defaults to the executable in CODEX_COMMAND",
    )
    parser.add_argument(
        "--codex-base-url",
        default=None,
        help=(
            "override openai_base_url for codex, bypassing the configured "
            "Headroom proxy; e.g. https://chatgpt.com/backend-api/codex"
        ),
    )
    parser.add_argument("--request-cd", type=Path, default=None)
    parser.add_argument("--output", type=Path, help="optional JSON report path")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.execute and not args.confirm_spend:
        parser.error("live execution requires both --execute and --confirm-spend")
    if not args.execute and args.confirm_spend:
        parser.error("--confirm-spend requires --execute")
    monitor.load_env_file(args.env_file)
    if not args.codex_command:
        configured_command = os.getenv("CODEX_COMMAND", "codex")
        configured_parts = shlex.split(configured_command)
        args.codex_command = configured_parts[0] if configured_parts else "codex"
    if not args.pi_command:
        configured_pi = os.getenv("PI_COMMAND", DEFAULT_PI_COMMAND)
        configured_pi_parts = shlex.split(configured_pi)
        args.pi_command = configured_pi_parts[0] if configured_pi_parts else DEFAULT_PI_COMMAND
    if args.token_source == "pi" and args.harness != "pi":
        raise BenchmarkError("--token-source pi requires --harness pi")
    if args.token_source == "codex" and args.harness != "codex":
        raise BenchmarkError("--token-source codex requires --harness codex")
    if args.poll_seconds <= 0:
        raise BenchmarkError("--poll-seconds must be greater than zero")
    if args.max_seconds <= 0:
        raise BenchmarkError("--max-seconds must be greater than zero")
    if args.fine_chunk_tokens is not None and args.fine_chunk_tokens >= args.chunk_tokens:
        raise BenchmarkError(
            "--fine-chunk-tokens must be smaller than --chunk-tokens"
        )
    args.headroom_log_dir = args.headroom_log_dir or Path(
        os.getenv("HEADROOM_LOG_DIR", "~/.headroom/logs")
    ).expanduser()
    args.headroom_client = args.headroom_client or os.getenv("HEADROOM_CLIENT", "codex")
    args.headroom_timezone = args.headroom_timezone or os.getenv("HEADROOM_LOG_TIMEZONE") or None
    aliases = load_aliases()
    models = tuple(canonical_model(item, aliases) for item in args.models)
    model_overrides = parse_model_overrides(args.model_tokens_per_point)
    model_overrides = {
        canonical_model(model, aliases): value
        for model, value in model_overrides.items()
    }
    calibrations = build_calibrations(
        args.state_path,
        args.window,
        models,
        lookback=args.calibration_lookback,
        shared_override=args.tokens_per_point,
        model_overrides=model_overrides,
        aliases=aliases,
    )
    plan = _plan(args, calibrations, models)
    if not args.execute:
        payload = {"schema_version": 1, "dry_run": True, "plan": plan}
    else:
        missing = [
            model
            for model in models
            if args.target_tokens is None and calibrations[model].tokens_per_point is None
        ]
        if missing and args.mode != "crossing":
            raise BenchmarkError(
                "no exclusive per-model crossing calibration for: "
                + ", ".join(missing)
                + "; pass --model-tokens-per-point MODEL=TOKENS or --target-tokens"
            )
        pricing_path = Path(
            os.getenv("MODEL_PRICING_FILE", "").strip()
            or monitor.default_pricing_path()
        ).expanduser()
        try:
            pricing = monitor.load_model_pricing(pricing_path)
        except monitor.MonitorError as exc:
            raise BenchmarkError(f"cannot load monitor pricing: {exc}") from exc
        try:
            cursor, cursor_diagnostics = monitor.prime_headroom_cursor(args.headroom_log_dir)
        except Exception as exc:
            raise BenchmarkError(f"cannot initialize Headroom capture: {exc}") from exc
        results: list[dict[str, Any]] = []
        with tempfile.TemporaryDirectory(prefix="codex-percentage-benchmark-") as temporary:
            workdir = args.request_cd or Path(temporary)
            workdir.mkdir(parents=True, exist_ok=True)
            for model in models:
                result, cursor = run_model(
                    model,
                    args,
                    calibrations[model],
                    aliases,
                    pricing,
                    cursor,
                    workdir,
                )
                results.append(result)
                if result["status"] == "other_model_usage_detected":
                    break
        payload = {
            "schema_version": 1,
            "dry_run": False,
            "plan": plan,
            "capture_baseline": cursor_diagnostics,
            "results": results,
        }
    rendered = json.dumps(payload, indent=2, sort_keys=True)
    print(rendered)
    if args.output:
        args.output.expanduser().parent.mkdir(parents=True, exist_ok=True)
        args.output.expanduser().write_text(rendered + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BenchmarkError as exc:
        print(f"percentage benchmark: {exc}", file=sys.stderr)
        raise SystemExit(2)
