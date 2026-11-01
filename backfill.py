#!/usr/bin/env python3
"""Rebuild historical Codex usage estimates from retained Headroom records."""

from __future__ import annotations

import argparse
import bisect
import copy
import datetime as dt
import json
import os
import re
import shutil
import sys
import tempfile
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, replace as dataclass_replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import monitor


_CHECKPOINT_RE = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) UTC "
    r"primary=(?P<primary>-|[-+]?\d+(?:\.\d+)?)% "
    r"secondary=(?P<secondary>-|[-+]?\d+(?:\.\d+)?)% "
    r"balance=(?P<balance>\S+)"
)
_COST_FIELDS = (
    "known_cost",
    "known_cost_usd",
    "estimated_cost",
    "uncached_input_cost",
    "cached_input_cost",
    "output_cost",
    "cache_write_cost",
)

# The current installation was restarted at this checkpoint.  Earlier usage
# lines predate the retained, request-level pricing run and are intentionally
# outside this backfill's scope.  The CLI exposes --start so a future restart
# can use a different boundary without changing the replay algorithm.
DEFAULT_SCOPE_START = "2026-09-09T17:41:42+00:00"

# A few recently used payloads are cached so the replay can ask a checkpoint
# for its window details several times without re-reading the log each time.
_PAYLOAD_CACHE: OrderedDict[tuple[str, int, int], dict[str, Any] | None] = OrderedDict()
_PAYLOAD_CACHE_LIMIT = 4


def _read_payload_range(path: str, offset: int, length: int) -> dict[str, Any] | None:
    """Re-read and decode one embedded usage_v2 payload by byte range."""

    key = (path, offset, length)
    if key in _PAYLOAD_CACHE:
        _PAYLOAD_CACHE.move_to_end(key)
        return _PAYLOAD_CACHE[key]
    try:
        with open(path, "rb") as handle:
            handle.seek(offset)
            raw = handle.read(length)
    except OSError:
        return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        parsed = None
    payload = parsed if isinstance(parsed, dict) else None
    _PAYLOAD_CACHE[key] = payload
    while len(_PAYLOAD_CACHE) > _PAYLOAD_CACHE_LIMIT:
        _PAYLOAD_CACHE.popitem(last=False)
    return payload


@dataclass(frozen=True)
class Checkpoint:
    line_number: int
    line: str
    prefix: str
    captured_at: float
    primary_percent: float | None
    secondary_percent: float | None
    balance: str | None
    payload: dict[str, Any] | None
    # Byte range of the embedded usage_v2 JSON.  ``payload`` is left as None
    # while scanning and re-parsed on demand so that holding every checkpoint
    # of a multi-gigabyte log costs bytes, not gigabytes.
    payload_path: str | None = None
    payload_offset: int | None = None
    payload_length: int = 0

    def load_payload(self) -> dict[str, Any] | None:
        """Return the parsed usage_v2 payload, reading it back if needed."""

        if isinstance(self.payload, dict):
            return self.payload
        if self.payload_path is None or self.payload_offset is None or self.payload_length <= 0:
            return None
        return _read_payload_range(self.payload_path, self.payload_offset, self.payload_length)

    def window(self, name: str) -> monitor.LimitWindow | None:
        percent = self.primary_percent if name == "primary" else self.secondary_percent
        if percent is None:
            return None
        payload = self.load_payload() or {}
        source: Any = None
        windows = payload.get("windows")
        if isinstance(windows, dict):
            source = windows.get(name)
        if not isinstance(source, dict) and name == "secondary":
            source = payload.get("weekly_estimate")
        source = source if isinstance(source, dict) else {}
        duration = _integer(source.get("window_duration_mins"))
        if duration is None:
            duration = 300 if name == "primary" else 10080
        return monitor.LimitWindow(
            used_percent=percent,
            resets_at=_integer(source.get("reset_at")),
            window_duration_mins=duration,
        )

    def snapshot(self, fallback: monitor.Snapshot | None = None) -> monitor.Snapshot:
        fallback = fallback or monitor.Snapshot(
            captured_at=int(self.captured_at),
            primary=None,
            secondary=None,
            credit_balance=None,
            reset_credit_available_count=None,
            reset_credit_ids=(),
        )
        return monitor.Snapshot(
            captured_at=int(self.captured_at),
            primary=self.window("primary"),
            secondary=self.window("secondary"),
            credit_balance=(
                fallback.credit_balance
                if self.balance in {None, "-"}
                else self.balance
            ),
            reset_credit_available_count=fallback.reset_credit_available_count,
            reset_credit_ids=fallback.reset_credit_ids,
        )


def _integer(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _iso(timestamp: float | None) -> str | None:
    if timestamp is None:
        return None
    return dt.datetime.fromtimestamp(timestamp, dt.timezone.utc).isoformat()


def _parse_percent(value: str) -> float | None:
    if value == "-":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def parse_checkpoint_line(
    line: str, line_number: int, *, parse_payload: bool = True
) -> Checkpoint | None:
    """Parse the preserved human-readable prefix and optional old JSON."""

    content = line.rstrip("\n")
    prefix, separator, encoded = content.partition(" usage_v2=")
    match = _CHECKPOINT_RE.match(prefix)
    if match is None:
        return None
    captured_at = dt.datetime.strptime(
        match.group("timestamp"), "%Y-%m-%d %H:%M:%S"
    ).replace(tzinfo=dt.timezone.utc)
    payload: dict[str, Any] | None = None
    if separator and parse_payload:
        try:
            decoded = json.loads(encoded)
        except json.JSONDecodeError:
            decoded = None
        if isinstance(decoded, dict):
            payload = decoded
    return Checkpoint(
        line_number=line_number,
        line=content,
        prefix=prefix,
        captured_at=captured_at.timestamp(),
        primary_percent=_parse_percent(match.group("primary")),
        secondary_percent=_parse_percent(match.group("secondary")),
        balance=None if match.group("balance") == "-" else match.group("balance"),
        payload=payload,
    )


def read_checkpoints(path: Path) -> list[Checkpoint]:
    """Stream the usage log and return lightweight checkpoint records.

    Payloads are not decoded here: each record stores the byte range of its
    embedded JSON and re-reads it on demand (see ``Checkpoint.load_payload``),
    so a multi-gigabyte log no longer materialises every payload at once.
    """

    checkpoints: list[Checkpoint] = []
    offset = 0
    with path.open("rb") as handle:
        for number, raw in enumerate(handle):
            line_start = offset
            offset += len(raw)
            text = raw.decode("utf-8", errors="replace")
            if text.endswith("\r\n"):
                text = text[:-2]
            elif text.endswith(("\n", "\r")):
                text = text[:-1]
            checkpoint = parse_checkpoint_line(text, number, parse_payload=False)
            if checkpoint is None:
                continue
            head, separator, encoded = raw.partition(b" usage_v2=")
            if separator:
                checkpoint = dataclass_replace(
                    checkpoint,
                    line="",
                    payload_path=str(path),
                    payload_offset=line_start + len(head) + len(separator),
                    payload_length=len(encoded),
                )
            checkpoints.append(checkpoint)
    return checkpoints


def _source_payload(pricing: monitor.PricingTable) -> dict[str, Any]:
    return {
        "headroom_log_dir": os.getenv(
            "HEADROOM_LOG_DIR", str(Path.home() / ".headroom" / "logs")
        ),
        "client": os.getenv("HEADROOM_CLIENT", "codex"),
        "excluded_models": sorted(monitor.headroom_excluded_models()),
        "timezone": os.getenv("HEADROOM_LOG_TIMEZONE", ""),
        "pricing_file": pricing.source_path,
        "pricing_revision": pricing.revision,
        "currency": pricing.currency,
        "unit": pricing.unit,
        "pricing_mode": pricing.pricing_mode,
        "cost_basis": "current_price_equivalent",
        "pricing_basis": "uncached_input_plus_cached_input_plus_output",
        "legacy_cache_write_billing": "ignored",
        "legacy_inferred_cache_write_mode": os.getenv(
            "INFERRED_CACHE_WRITE_MODE", "estimate"
        ),
        "processing_tier": os.getenv(
            "PRICING_PROCESSING_TIER", pricing.processing_tier
        ),
    }


def _period_diagnostics(
    records: list[monitor.HeadroomUsageRecord],
    *,
    coverage_gap: bool,
    reason: str | None = None,
) -> dict[str, Any]:
    diagnostics: dict[str, Any] = {
        "accepted": len(records),
        "excluded": 0,
        "invalid": 0,
        "late": 0,
        "coverage_gap": coverage_gap,
        "previous_coverage_gap": False,
        "backfill": True,
        "source_assumption": (
            "Headroom records are filtered by client; account identity is unavailable "
            "in PERF lines"
        ),
    }
    if reason:
        diagnostics["coverage_gap_reason"] = reason
    return diagnostics


def _records_in_period(
    records: list[monitor.HeadroomUsageRecord],
    timestamps: list[float],
    start: float,
    end: float,
) -> list[monitor.HeadroomUsageRecord]:
    left = bisect.bisect_right(timestamps, start)
    right = bisect.bisect_right(timestamps, end)
    return records[left:right]


def _reset_observed(
    previous: monitor.LimitWindow | None,
    current: monitor.LimitWindow | None,
) -> bool:
    if previous is None or current is None:
        return False
    if previous.used_percent is None or current.used_percent is None:
        return False
    if current.used_percent < previous.used_percent:
        return True
    return current.window_duration_mins != previous.window_duration_mins


def _carry_external_evidence(payload: dict[str, Any], checkpoint: "Checkpoint") -> None:
    """Copy the outside-VM evidence section from the line being replaced.

    ``external_evidence`` records what the account-wide lifetime token counter
    did during the period.  It is a raw observation rather than a derived
    estimate, and by design it never feeds anchors, crossings, or window
    numbers.  Rebuilding the payload without it would silently delete the only
    local record of off-VM spend, so it is carried across verbatim.
    """

    previous = checkpoint.load_payload() or {}
    evidence = previous.get("external_evidence")
    if isinstance(evidence, dict):
        payload["external_evidence"] = copy.deepcopy(evidence)


def _old_estimate(checkpoint: Checkpoint, name: str) -> str | None:
    payload = checkpoint.load_payload()
    if not isinstance(payload, dict):
        return None
    source: Any = (payload.get("windows") or {}).get(name)
    if not isinstance(source, dict) and name == "secondary":
        source = payload.get("weekly_estimate")
    if not isinstance(source, dict):
        return None
    value = source.get("full_window_usd")
    return str(value) if value not in {None, ""} else None


def _mark_partial_cycle(estimate: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(estimate)
    result["status"] = "partial_coverage"
    result["reason"] = "cycle_anchor_before_raw_coverage"
    result["backfill_status"] = "partial_cycle"
    result["cost_scope"] = "since_replay_anchor"
    result["full_window_usd"] = None
    result["full_observed_estimate"] = None
    result["model_shares_at_100_usd"] = {}
    result["completed_percentage_points"] = 0
    result["confidence"] = "Very low"
    reasons = list(result.get("confidence_reasons") or [])
    if "cycle_anchor_before_raw_coverage" not in reasons:
        reasons.append("cycle_anchor_before_raw_coverage")
    result["confidence_reasons"] = reasons
    return result


def _strip_costs(value: Any) -> Any:
    if isinstance(value, dict):
        result = {key: _strip_costs(item) for key, item in value.items()}
        for field in _COST_FIELDS:
            if field in result:
                result[field] = None
        return result
    if isinstance(value, list):
        return [_strip_costs(item) for item in value]
    return value


def _unavailable_payload(
    checkpoint: Checkpoint,
    previous: Checkpoint | None,
    pricing: monitor.PricingTable,
    *,
    reason: str,
    raw_earliest: float | None,
) -> dict[str, Any]:
    previous_at = previous.captured_at if previous is not None else checkpoint.captured_at
    original = _strip_costs(copy.deepcopy(checkpoint.load_payload() or {}))
    payload: dict[str, Any] = original if isinstance(original, dict) else {}
    payload.update(
        {
            "schema_version": monitor._ACCOUNTING_SCHEMA_VERSION,
            "period_id": f"backfill-unavailable-{checkpoint.line_number}",
            "period": {
                "start": _iso(previous_at),
                "end": _iso(checkpoint.captured_at),
                "elapsed_seconds": max(0, checkpoint.captured_at - previous_at),
            },
            "source": _source_payload(pricing),
            "diagnostics": {
                "accepted": 0,
                "coverage_gap": True,
                "backfill": True,
                "coverage_gap_reason": reason,
                "raw_coverage_start": _iso(raw_earliest),
            },
            "models": payload.get("models", {}),
            "total": payload.get(
                "total",
                {
                    "requests": 0,
                    "input_tokens": 0,
                    "cached_input_tokens": 0,
                    "uncached_input_tokens": 0,
                    "output_tokens": 0,
                    "complete": False,
                    "known_cost": None,
                    "estimated_cost": None,
                    "reason": reason,
                },
            ),
        }
    )
    for name in ("primary", "secondary"):
        window = checkpoint.window(name)
        estimate = {
            "window": name,
            "status": "unavailable",
            "reason": reason,
            "estimate_basis": "cumulative_since_anchor",
            "anchor_captured_at": None,
            "anchor_used_percent": None,
            "current_used_percent": (
                window.used_percent if window is not None else None
            ),
            "delta_percent_points": None,
            "reset_at": window.resets_at if window is not None else None,
            "window_duration_mins": (
                window.window_duration_mins if window is not None else None
            ),
            "known_cost_usd": None,
            "full_window_usd": None,
            "full_observed_estimate": None,
            "rolling_estimates": {},
            "completed_percentage_points": 0,
            "confidence": "Very low",
            "model_shares_at_100_usd": {},
            "crossings": [],
            "cycle_history": [],
            "spikes": [],
        }
        payload.setdefault("windows", {})[name] = estimate
    payload["weekly_estimate"] = payload["windows"]["secondary"]
    payload["backfill"] = {
        "version": 1,
        "status": "unavailable",
        "reason": reason,
        "raw_observations_preserved": True,
        "raw_coverage_start": _iso(raw_earliest),
    }
    return payload


def _cycle_row(
    name: str,
    state: dict[str, Any],
    estimate: dict[str, Any],
    checkpoint: Checkpoint,
    *,
    exact: bool,
    old_estimate: str | None,
) -> dict[str, Any]:
    anchor = state.get("anchor") or {}
    full = estimate.get("full_observed_estimate")
    rolling = estimate.get("rolling_estimates") or {}
    return {
        "window": name,
        "anchor_captured_at": anchor.get("captured_at"),
        "anchor_used_percent": anchor.get("used_percent"),
        "end_captured_at": checkpoint.captured_at,
        "end_used_percent": estimate.get("current_used_percent"),
        "observed_range": (
            f"{anchor.get('used_percent')}% -> "
            f"{estimate.get('current_used_percent')}%"
        ),
        "corrected_cost_usd": (
            estimate.get("known_cost_usd") if exact else None
        ),
        "observed_cost_since_replay_anchor_usd": estimate.get("known_cost_usd"),
        "full_100_usd": estimate.get("full_window_usd") if exact else None,
        "last_10_estimate_100_usd": (
            (rolling.get("10") or {}).get("estimate_100_usd") if exact else None
        ),
        "models": sorted(
            (full or {}).get("model_attribution", {}).keys()
            if exact
            else (estimate.get("cumulative_usage", {}).get("models", {}) or {}).keys()
        ),
        "model_attribution": (
            copy.deepcopy((full or {}).get("model_attribution", {})) if exact else {}
        ),
        "confidence": estimate.get("confidence", "Very low") if exact else "Very low",
        "exact": exact,
        "old_estimate_100_usd": old_estimate,
        "status": "exact" if exact else "partial_cycle",
    }


def _close_cycle(
    current: dict[str, Any] | None,
    checkpoint: Checkpoint,
    estimate: dict[str, Any] | None,
    *,
    old_estimate: str | None,
) -> None:
    if current is None:
        return
    if estimate is not None:
        current["end_captured_at"] = checkpoint.captured_at
        current["end_used_percent"] = estimate.get("current_used_percent")
        current["observed_range"] = (
            f"{current.get('anchor_used_percent')}% -> "
            f"{estimate.get('current_used_percent')}%"
        )
        if current.get("exact"):
            current["corrected_cost_usd"] = estimate.get("known_cost_usd")
            current["full_100_usd"] = estimate.get("full_window_usd")
            current["last_10_estimate_100_usd"] = (
                (estimate.get("rolling_estimates") or {}).get("10") or {}
            ).get("estimate_100_usd")
            current["confidence"] = estimate.get("confidence", "Very low")
            current["models"] = sorted(
                (estimate.get("full_observed_estimate") or {})
                .get("model_attribution", {})
                .keys()
            )
            current["model_attribution"] = copy.deepcopy(
                (estimate.get("full_observed_estimate") or {}).get(
                    "model_attribution", {}
                )
            )
    if old_estimate is not None:
        current["old_estimate_100_usd"] = old_estimate


def replay_history(
    checkpoints: list[Checkpoint],
    records: list[monitor.HeadroomUsageRecord],
    pricing: monitor.PricingTable,
    *,
    raw_diagnostics: dict[str, Any],
    scope_start: float | None = None,
    payload_sink: Callable[[Checkpoint, dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Replay retained requests checkpoint by checkpoint.

    ``payload_sink`` receives each rebuilt payload as it is produced.  Passing
    one keeps the rebuilt payloads out of memory entirely, which is what makes
    a multi-gigabyte usage log tractable; without it the payloads are returned
    in ``result["payloads"]`` for callers that want them.
    """
    if not checkpoints:
        raise monitor.MonitorError("Usage log contains no parseable checkpoints")

    all_ordered = sorted(checkpoints, key=lambda checkpoint: checkpoint.captured_at)
    if scope_start is not None:
        # The first checkpoint at or after the explicit restart is the exact
        # accounting baseline.  No pre-restart checkpoint is ever replayed or
        # rewritten by this mode.
        ordered = [
            checkpoint
            for checkpoint in all_ordered
            if checkpoint.captured_at >= scope_start
        ]
        records = [record for record in records if record.timestamp > scope_start]
    else:
        ordered = all_ordered

    if not ordered:
        return {
            "payloads": {},
            "states": {"primary": None, "secondary": None},
            "cycles": [],
            "raw_earliest": None,
            "raw_latest": None,
            "first_exact_checkpoint": None,
            "unavailable_intervals": [
                {
                    "start": scope_start,
                    "end": None,
                    "reason": "no_checkpoint_at_or_after_scope_start",
                }
            ],
            "requests_replayed": 0,
            "scope_start": scope_start,
        }
    payloads: dict[int, dict[str, Any]] = {}
    last_payload: dict[str, Any] | None = None

    def emit(checkpoint: Checkpoint, payload: dict[str, Any]) -> None:
        if payload_sink is not None:
            payload_sink(checkpoint, payload)
        else:
            payloads[checkpoint.line_number] = payload

    if not records:
        for index, checkpoint in enumerate(ordered):
            last_payload = _unavailable_payload(
                checkpoint,
                ordered[index - 1] if index else None,
                pricing,
                reason="no_retained_headroom_requests_after_scope_start",
                raw_earliest=None,
            )
            emit(checkpoint, last_payload)
        return {
            "payloads": payloads,
            "last_payload": last_payload,
            "states": {"primary": None, "secondary": None},
            "cycles": [],
            "raw_earliest": None,
            "raw_latest": None,
            "first_exact_checkpoint": None,
            "unavailable_intervals": [
                {
                    "start": ordered[0].captured_at,
                    "end": ordered[-1].captured_at,
                    "reason": "no_retained_headroom_requests_after_scope_start",
                }
            ],
            "requests_replayed": 0,
            "scope_start": scope_start,
            "last_checkpoint": ordered[-1],
        }

    records = sorted(records, key=lambda record: record.timestamp)
    timestamps = [record.timestamp for record in records]
    raw_earliest = records[0].timestamp
    raw_latest = records[-1].timestamp
    exact_index = 0 if scope_start is not None else next(
        (
            index
            for index, checkpoint in enumerate(ordered)
            if checkpoint.captured_at > raw_earliest
        ),
        None,
    )
    if exact_index is None:
        return {
            "payloads": payloads,
            "states": {"primary": None, "secondary": None},
            "cycles": [],
            "raw_earliest": raw_earliest,
            "raw_latest": raw_latest,
            "first_exact_checkpoint": None,
            "unavailable_intervals": [
                {
                    "start": ordered[0].captured_at,
                    "end": ordered[-1].captured_at,
                    "reason": "all_checkpoints_precede_retained_headroom",
                }
            ],
            "requests_replayed": len(records),
        }

    previous_checkpoint = ordered[exact_index - 1] if exact_index else None
    baseline_checkpoint = previous_checkpoint or ordered[exact_index]
    fallback_snapshot = monitor.Snapshot(
        captured_at=int(baseline_checkpoint.captured_at),
        primary=None,
        secondary=None,
        credit_balance=baseline_checkpoint.balance,
        reset_credit_available_count=None,
        reset_credit_ids=(),
    )
    previous_snapshot = baseline_checkpoint.snapshot(fallback_snapshot)
    states: dict[str, dict[str, Any] | None] = {
        name: (
            monitor._baseline_window_state(
                getattr(previous_snapshot, name),
                previous_snapshot.captured_at,
                pricing.revision,
            )
            if getattr(previous_snapshot, name) is not None
            else None
        )
        for name in ("primary", "secondary")
    }
    # In scoped mode the restart checkpoint is a known baseline, so the cycle
    # is exact from its first post-restart request.  The legacy full-history
    # mode still marks a cycle partial when its anchor predates raw retention.
    cycle_exact = {
        "primary": scope_start is not None,
        "secondary": scope_start is not None,
    }
    current_cycles: dict[str, dict[str, Any] | None] = {
        "primary": None,
        "secondary": None,
    }
    cycles: list[dict[str, Any]] = []
    previous_at = previous_snapshot.captured_at

    for index in range(exact_index, len(ordered)):
        checkpoint = ordered[index]
        current_snapshot = checkpoint.snapshot(previous_snapshot)
        in_period = _records_in_period(records, timestamps, previous_at, checkpoint.captured_at)
        partial_period = (
            scope_start is None and previous_at < raw_earliest
        ) or bool(raw_diagnostics.get("coverage_gap"))
        period = monitor.aggregate_period(
            in_period,
            pricing,
            previous_at,
            checkpoint.captured_at,
            _period_diagnostics(
                in_period,
                coverage_gap=partial_period,
                reason="period_starts_before_first_retained_request"
                if partial_period
                else None,
            ),
            processing_tier=os.getenv("PRICING_PROCESSING_TIER", pricing.processing_tier),
        )
        monitor._attach_applied_rates(
            period,
            pricing,
            os.getenv("PRICING_PROCESSING_TIER", pricing.processing_tier),
        )
        estimates: dict[str, dict[str, Any]] = {}
        new_states: dict[str, dict[str, Any] | None] = {}
        for name in ("primary", "secondary"):
            previous_window = getattr(previous_snapshot, name)
            current_window = getattr(current_snapshot, name)
            reset = index > exact_index and _reset_observed(previous_window, current_window)
            state, estimate = monitor.update_window_calibration(
                name,
                previous_window,
                current_window,
                period,
                states[name],
                pricing.revision,
            )
            if reset:
                cycle_exact[name] = True
            if estimate.get("status") == "missing_window":
                cycle_exact[name] = False
            if estimate.get("status") in {
                "partial_coverage",
                "unattributed_usage",
                "invalid_counters",
                "unpriced_model",
                "imputed_external_usage",
            }:
                cycle_exact[name] = False
            if state is not None and not cycle_exact[name]:
                estimate = _mark_partial_cycle(estimate)
            new_states[name] = state
            estimates[name] = estimate

            old_value = _old_estimate(checkpoint, name)
            if reset and current_cycles[name] is not None:
                previous_cycle_checkpoint = ordered[index - 1] if index else checkpoint
                _close_cycle(
                    current_cycles[name],
                    previous_cycle_checkpoint,
                    None,
                    old_estimate=_old_estimate(previous_cycle_checkpoint, name),
                )
                cycles.append(current_cycles[name])
                current_cycles[name] = None
            if state is not None and current_cycles[name] is None:
                current_cycles[name] = _cycle_row(
                    name,
                    state,
                    estimate,
                    checkpoint,
                    exact=cycle_exact[name],
                    old_estimate=old_value,
                )
            elif current_cycles[name] is not None:
                _close_cycle(
                    current_cycles[name],
                    checkpoint, estimate, old_estimate=old_value
                )

        payload = monitor.build_usage_v2(
            previous_snapshot,
            current_snapshot,
            period,
            estimates,
            pricing,
            in_period,
        )
        # The parallel outside-VM evidence stream is a raw observation of the
        # account-wide counter, not a derived estimate, and it never feeds the
        # window numbers.  Carry it across the replay unchanged so the rewrite
        # does not erase the only record of what the account counter did.
        _carry_external_evidence(payload, checkpoint)
        payload["backfill"] = {
            "version": 1,
            "status": "partial_period" if partial_period else "replayed",
            "raw_observations_preserved": True,
            "raw_coverage_start": _iso(raw_earliest),
            "raw_coverage_end": _iso(raw_latest),
            "request_count_replayed": len(in_period),
            "cycle_exact": {
                name: bool(cycle_exact[name]) for name in ("primary", "secondary")
            },
        }
        last_payload = payload
        emit(checkpoint, payload)
        states = new_states
        previous_snapshot = current_snapshot
        previous_at = checkpoint.captured_at

    for name in ("primary", "secondary"):
        if current_cycles[name] is not None:
            _close_cycle(
                current_cycles[name],
                ordered[-1],
                None,
                old_estimate=_old_estimate(ordered[-1], name),
            )
            cycles.append(current_cycles[name])

    unavailable_intervals: list[dict[str, Any]] = []
    if exact_index:
        unavailable_intervals.append(
            {
                "start": ordered[0].captured_at,
                "end": ordered[exact_index - 1].captured_at,
                "reason": "raw_headroom_logs_rotated_before_first_retained_request",
            }
        )
    if raw_diagnostics.get("coverage_gap"):
        unavailable_intervals.append(
            {
                "start": raw_earliest,
                "end": raw_latest,
                "reason": "headroom_collector_reported_coverage_gap",
            }
        )
    return {
        "payloads": payloads,
        "last_payload": last_payload,
        "states": states,
        "cycles": cycles,
        "raw_earliest": raw_earliest,
        "raw_latest": raw_latest,
        "first_exact_checkpoint": ordered[exact_index].captured_at,
        "unavailable_intervals": unavailable_intervals,
        "requests_replayed": len(records),
        "last_checkpoint": ordered[-1],
        "scope_start": scope_start,
    }


def parse_timestamp(value: str) -> float:
    """Parse an ISO-8601 UTC timestamp used as the replay boundary."""

    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"invalid timestamp {value!r}; use ISO-8601, for example {DEFAULT_SCOPE_START}"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc).timestamp()


def _backup_path(path: Path, label: str) -> Path:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    candidate = path.with_name(f"{path.name}.{label}-{stamp}.bak")
    suffix = 1
    while candidate.exists():
        candidate = path.with_name(f"{path.name}.{label}-{stamp}-{suffix}.bak")
        suffix += 1
    return candidate


def _base_checkpoint_prefix(checkpoint: Checkpoint) -> str:
    match = _CHECKPOINT_RE.match(checkpoint.prefix)
    if match is None:
        raise monitor.MonitorError(
            f"Could not reconstruct checkpoint prefix on usage-log line {checkpoint.line_number + 1}"
        )
    # Preserve the exact recorded timestamp, percentage text, and balance
    # spelling.  Only the derived projection and usage_v2 payload are rebuilt.
    return (
        f"{match.group('timestamp')} UTC "
        f"primary={match.group('primary')}% "
        f"secondary={match.group('secondary')}% "
        f"balance={match.group('balance')}"
    )


def render_checkpoint(checkpoint: Checkpoint, payload: dict[str, Any]) -> str:
    """Render corrected derived data while retaining raw checkpoint fields."""

    weekly = payload.get("weekly_estimate") or {}
    estimate = weekly.get("full_window_usd") or "-"
    confidence = str(weekly.get("confidence", "Very low")).replace(" ", "_")
    compact = json.dumps(
        monitor._json_safe(payload), separators=(",", ":"), sort_keys=True
    )
    return (
        f"{_base_checkpoint_prefix(checkpoint)} "
        f"weekly_100pct_usd={estimate} "
        f"weekly_100pct_basis=cumulative_since_anchor "
        f"weekly_100pct_confidence={confidence} "
        f"usage_v2={compact}"
    )


def _line_ending(raw: bytes) -> bytes:
    for ending in (b"\r\n", b"\n", b"\r"):
        if raw.endswith(ending):
            return ending
    return b""


class _StreamingLogRewrite:
    """Rewrite selected usage-log lines through a temporary file.

    The log is read and written one line at a time, so peak memory is bounded
    by the pending replacements rather than by the size of the log.  The
    original file is replaced atomically only in ``finish()``; ``abort()``
    leaves it untouched.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.count = 0
        self.first_line: int | None = None
        self._source = None
        self._target = None
        self._temporary: Path | None = None
        self._pending: dict[int, tuple[Checkpoint, dict[str, Any]]] = {}
        self._next_line = 0
        self._started = False

    def start(self) -> _StreamingLogRewrite:
        # Both handles are long-lived by design: the rewrite streams from them
        # until finish() or abort() closes them.
        self._source = self.path.open("rb")
        handle = tempfile.NamedTemporaryFile(  # noqa: SIM115
            mode="wb",
            prefix=f".{self.path.name}.",
            suffix=".tmp",
            dir=self.path.parent,
            delete=False,
        )
        self._temporary = Path(handle.name)
        self._target = handle
        self._started = True
        return self

    # -- emission ---------------------------------------------------------
    def emit(self, checkpoint: Checkpoint, payload: dict[str, Any]) -> None:
        if not isinstance(payload, dict):
            return
        line_number = checkpoint.line_number
        if line_number < self._next_line:
            raise monitor.MonitorError(
                "Backfill produced an out-of-order replacement for "
                f"usage-log line {line_number + 1}"
            )
        self._pending[line_number] = (checkpoint, payload)
        self._flush_until(line_number)

    def _write_replacement(
        self, entry: tuple[Checkpoint, dict[str, Any]], raw: bytes
    ) -> None:
        checkpoint, payload = entry
        rendered = render_checkpoint(checkpoint, payload).encode("utf-8")
        self._target.write(rendered + _line_ending(raw))
        self.count += 1
        if self.first_line is None:
            self.first_line = checkpoint.line_number

    def _write_line(self, raw: bytes) -> None:
        entry = self._pending.pop(self._next_line, None)
        if entry is None:
            # Untouched lines are copied byte for byte, which is what keeps
            # everything before the scope boundary identical.
            self._target.write(raw)
        else:
            self._write_replacement(entry, raw)
        self._next_line += 1

    def _flush_until(self, limit: int) -> None:
        while self._next_line < limit:
            raw = self._source.readline()
            if not raw:
                raise monitor.MonitorError(
                    "Usage log changed while preparing the backfill"
                )
            self._write_line(raw)

    # -- lifecycle --------------------------------------------------------
    def finish(self) -> tuple[Path | None, int, int]:
        if not self._started:
            return None, 0, 0
        while True:
            raw = self._source.readline()
            if not raw:
                break
            self._write_line(raw)
        if self._pending:
            raise monitor.MonitorError("Usage log changed while preparing the backfill")
        if not self.count:
            return None, 0, 0
        self._source.close()
        self._target.flush()
        os.fsync(self._target.fileno())
        self._target.close()
        mode = self.path.stat().st_mode & 0o777
        os.chmod(self._temporary, mode)
        backup = _backup_path(self.path, "before-backfill")
        shutil.copy2(self.path, backup)
        self._temporary.replace(self.path)
        self._started = False
        return backup, self.count, int(self.first_line or 0)

    def abort(self) -> None:
        for handle in (self._source, self._target):
            try:
                if handle is not None:
                    handle.close()
            except OSError:
                pass
        self._source = None
        self._target = None
        if self._temporary is not None:
            try:
                self._temporary.unlink()
            except OSError:
                pass
            self._temporary = None
        self._started = False


def rewrite_usage_log(
    path: Path,
    checkpoints: list[Checkpoint],
    payloads: dict[int, dict[str, Any]],
) -> tuple[Path | None, int, int]:
    """Atomically replace only post-scope derived checkpoint payloads.

    The human-readable prefix is reconstructed from the parsed checkpoint, so
    timestamps and percentages remain the original observations.  Lines not
    selected by ``payloads`` are copied as bytes, including all pre-restart
    lines and their old payloads.  Both directions stream, so a multi-gigabyte
    log is never held in memory.
    """

    if not payloads:
        return None, 0, 0
    checkpoint_by_line = {checkpoint.line_number: checkpoint for checkpoint in checkpoints}
    selected = [
        (checkpoint_by_line[line_number], payloads[line_number])
        for line_number in sorted(payloads)
        if line_number in checkpoint_by_line
    ]
    if not selected:
        return None, 0, 0

    rewriter = _StreamingLogRewrite(path).start()
    try:
        for checkpoint, payload in selected:
            rewriter.emit(checkpoint, payload)
        return rewriter.finish()
    except BaseException:
        rewriter.abort()
        raise


def _timestamped(value: Any) -> str | None:
    try:
        return _iso(float(value))
    except (TypeError, ValueError):
        return None


def _historical_cycle_rows(
    cycles: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for cycle in cycles:
        row = copy.deepcopy(cycle)
        row["anchor_captured_at"] = _timestamped(row.get("anchor_captured_at"))
        row["end_captured_at"] = _timestamped(row.get("end_captured_at"))
        rows.append(row)
    return rows


def _before_after_rows(cycles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for cycle in cycles:
        if not cycle.get("exact"):
            continue
        result.append(
            {
                "window": cycle.get("window"),
                "observed_range": cycle.get("observed_range"),
                "old_estimate_100_usd": cycle.get("old_estimate_100_usd"),
                "corrected_estimate_100_usd": cycle.get("full_100_usd"),
                "old_to_corrected": (
                    f"{cycle.get('old_estimate_100_usd')} -> {cycle.get('full_100_usd')}"
                    if cycle.get("old_estimate_100_usd") is not None
                    else None
                ),
            }
        )
    return result


def _long_context_counts(
    records: list[monitor.HeadroomUsageRecord], pricing: monitor.PricingTable
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records:
        breakdown = monitor.price_usage_record(record, pricing)
        if breakdown.long_context:
            model = pricing.canonical_name(record.model)
            counts[model] = counts.get(model, 0) + 1
    return dict(sorted(counts.items()))


def build_report(
    result: dict[str, Any],
    checkpoints: list[Checkpoint],
    records: list[monitor.HeadroomUsageRecord],
    pricing: monitor.PricingTable,
    *,
    scope_start: float,
    raw_diagnostics: dict[str, Any],
    usage_log: Path,
    state_path: Path,
) -> dict[str, Any]:
    in_scope = [
        checkpoint for checkpoint in checkpoints if checkpoint.captured_at >= scope_start
    ]
    latest = in_scope[-1] if in_scope else None
    cycles = result.get("cycles", [])
    exact_cycles = [cycle for cycle in cycles if cycle.get("exact")]
    return {
        "schema_version": 1,
        "scope": {
            "start": _iso(scope_start),
            "description": "post-restart only; pre-restart derived log lines were not rewritten",
            "pre_scope_checkpoints_untouched": sum(
                checkpoint.captured_at < scope_start for checkpoint in checkpoints
            ),
        },
        "source": _source_payload(pricing),
        "usage_log": str(usage_log),
        "state_path": str(state_path),
        "earliest_timestamp_exactly_replayed": _timestamped(
            result.get("first_exact_checkpoint")
        ),
        "earliest_raw_request_replayed": _timestamped(result.get("raw_earliest")),
        "latest_timestamp": _timestamped(
            latest.captured_at if latest is not None else None
        ),
        "requests_replayed": int(result.get("requests_replayed", 0)),
        "checkpoints_in_scope": len(in_scope),
        "checkpoints_corrected": len(result.get("payloads", {})),
        "cycles_corrected": len(exact_cycles),
        "long_context_requests_by_model": _long_context_counts(records, pricing),
        "raw_diagnostics": raw_diagnostics,
        "unavailable_intervals": [
            {
                **interval,
                "start": _timestamped(interval.get("start")),
                "end": _timestamped(interval.get("end")),
            }
            for interval in result.get("unavailable_intervals", [])
        ],
        "historical_cycles": _historical_cycle_rows(cycles),
        "before_after": _before_after_rows(cycles),
        "raw_observations_preserved": True,
        "raw_headroom_logs_modified": False,
        "backfill_status": "exact" if exact_cycles else "unavailable_or_partial",
    }


def _history_timestamp(row: Any) -> float | None:
    if not isinstance(row, dict):
        return None
    for key in ("closed_at", "end_captured_at"):
        try:
            return float(row[key])
        except (KeyError, TypeError, ValueError):
            continue
    anchor = row.get("anchor")
    if isinstance(anchor, dict):
        try:
            return float(anchor["captured_at"])
        except (KeyError, TypeError, ValueError):
            pass
    return None


def _history_identity(row: Any) -> tuple[Any, Any] | None:
    """Identify one closed cycle by its anchor instant and close instant."""

    if not isinstance(row, dict):
        return None
    anchor = row.get("anchor")
    if not isinstance(anchor, dict):
        return None
    try:
        return (float(anchor["captured_at"]), float(row["closed_at"]))
    except (KeyError, TypeError, ValueError):
        return None


def _merge_pre_scope_history(
    rebuilt: dict[str, Any] | None,
    previous: dict[str, Any] | None,
    scope_start: float,
) -> dict[str, Any] | None:
    """Keep the closed cycles that the replay cannot reproduce.

    A cycle that closed exactly at ``scope_start`` is pre-scope history: the
    replay begins at that instant and therefore never observes its close.  The
    boundary comparison must be inclusive, otherwise a scope anchored on a
    window restart silently discards the cycle that just completed.
    """
    if rebuilt is None:
        return copy.deepcopy(previous) if isinstance(previous, dict) else None
    old = previous if isinstance(previous, dict) else {}
    for field in ("cycle_history", "legacy_cycles"):
        new_rows = list(rebuilt.get(field) or [])
        rebuilt_keys = {_history_identity(row) for row in new_rows}
        old_rows = [
            row
            for row in (old.get(field) or [])
            if _history_timestamp(row) is not None
            and _history_timestamp(row) <= scope_start
            # A replay can also emit a row for the same cycle; keep its version
            # rather than listing the cycle twice.
            and _history_identity(row) not in rebuilt_keys
        ]
        rebuilt[field] = old_rows + new_rows
    return rebuilt


def build_final_state(
    previous_snapshot: monitor.Snapshot | None,
    previous_accounting: dict[str, Any],
    result: dict[str, Any],
    pricing: monitor.PricingTable,
    checkpoints: list[Checkpoint],
    scope_start: float,
) -> tuple[monitor.Snapshot, dict[str, Any]]:
    latest = result.get("last_checkpoint")
    if not isinstance(latest, Checkpoint):
        raise monitor.MonitorError("Backfill produced no final checkpoint")
    final_snapshot = latest.snapshot(previous_snapshot)
    accounting = monitor.empty_accounting_state(pricing.revision)
    accounting["limit_id"] = previous_accounting.get(
        "limit_id", os.getenv("CODEX_LIMIT_ID", "codex")
    )
    accounting["last_sample_at"] = latest.captured_at
    latest_payload = (
        result.get("last_payload")
        or result.get("payloads", {}).get(latest.line_number)
        or {}
    )
    accounting["last_period_id"] = latest_payload.get("period_id")
    accounting["cursor"] = copy.deepcopy(
        previous_accounting.get("cursor") or monitor._empty_cursor()
    )
    old_windows = previous_accounting.get("windows")
    old_windows = old_windows if isinstance(old_windows, dict) else {}
    rebuilt_windows = result.get("states") or {}
    accounting["windows"] = {
        name: _merge_pre_scope_history(
            rebuilt_windows.get(name), old_windows.get(name), scope_start
        )
        for name in ("primary", "secondary")
    }
    accounting["recovery"] = {
        "pending": False,
        "coverage_gap": bool(
            result.get("unavailable_intervals")
            or result.get("raw_diagnostics", {}).get("coverage_gap")
        ),
    }
    accounting["backfill"] = {
        "scope_start": _iso(scope_start),
        "raw_observations_preserved": True,
        "pre_scope_state_preserved": True,
        "requests_replayed": result.get("requests_replayed", 0),
    }
    return final_snapshot, accounting


def print_report(report: dict[str, Any]) -> None:
    print(f"Backfill scope: {report['scope']['start']} onward")
    print(
        "Exact replay: "
        f"{report.get('earliest_timestamp_exactly_replayed') or '-'} -> "
        f"{report.get('latest_timestamp') or '-'}; "
        f"requests={report['requests_replayed']}; "
        f"checkpoints={report['checkpoints_corrected']}"
    )
    print("Cycle | Observed range | Corrected cost | Full 100% | Last 10% | Models | Confidence")
    for cycle in report.get("historical_cycles", []):
        print(
            " | ".join(
                str(cycle.get(field) if cycle.get(field) is not None else "-")
                for field in (
                    "window",
                    "observed_range",
                    "corrected_cost_usd",
                    "full_100_usd",
                    "last_10_estimate_100_usd",
                    "models",
                    "confidence",
                )
            )
        )
    print("Before -> corrected:")
    for row in report.get("before_after", []):
        print(
            f"{row.get('window')} {row.get('observed_range')}: "
            f"{row.get('old_to_corrected') or 'old unavailable'}"
        )
    print(
        "Long-context requests: "
        f"{report.get('long_context_requests_by_model') or 'none'}"
    )
    print(
        "Unreconstructable intervals: "
        f"{report.get('unavailable_intervals') or 'none'}"
    )
    print("Raw Headroom observations modified: no")


def run_backfill(args: argparse.Namespace) -> dict[str, Any]:
    monitor.load_env_file(args.env_file)
    usage_log = args.usage_log.expanduser()
    if usage_log.suffix == ".gz":
        raise monitor.MonitorError(
            f"{usage_log} is a gzip archive; decompress it first "
            f"(gunzip -k {usage_log}) and point --usage-log at the result"
        )
    state_path = args.state_path.expanduser()
    pricing_path = Path(
        os.getenv("MODEL_PRICING_FILE", "").strip() or monitor.default_pricing_path()
    ).expanduser()
    pricing = monitor.load_model_pricing(pricing_path)
    scope_start = parse_timestamp(args.start)

    with monitor.state_lock(state_path):
        checkpoints = read_checkpoints(usage_log)
        in_scope = [
            checkpoint
            for checkpoint in checkpoints
            if checkpoint.captured_at >= scope_start
        ]
        if not in_scope:
            raise monitor.MonitorError(
                f"No usage checkpoints found at or after {_iso(scope_start)}"
            )
        end_at = in_scope[-1].captured_at
        headroom_dir = Path(
            os.getenv("HEADROOM_LOG_DIR", str(Path.home() / ".headroom" / "logs"))
        ).expanduser()
        client = os.getenv("HEADROOM_CLIENT", "codex").strip() or None
        timezone_name = os.getenv("HEADROOM_LOG_TIMEZONE", "").strip() or None
        records, scan_cursor, diagnostics = monitor.collect_headroom_usage(
            headroom_dir,
            scope_start,
            end_at,
            {},
            client=client,
            timezone_name=timezone_name,
        )
        # The rewrite consumes rebuilt payloads as the replay produces them,
        # so neither the payloads nor the source lines are ever accumulated.
        rewriter: _StreamingLogRewrite | None = None
        counted = {"payloads": 0}

        def _count_sink(checkpoint: Checkpoint, payload: dict[str, Any]) -> None:
            counted["payloads"] += 1

        if args.dry_run:
            sink: Callable[[Checkpoint, dict[str, Any]], None] = _count_sink
        else:
            rewriter = _StreamingLogRewrite(usage_log).start()
            sink = rewriter.emit
        try:
            result = replay_history(
                checkpoints,
                records,
                pricing,
                raw_diagnostics=diagnostics,
                scope_start=scope_start,
                payload_sink=sink,
            )
        except BaseException:
            if rewriter is not None:
                rewriter.abort()
            raise
        try:
            result["raw_diagnostics"] = diagnostics
            report = build_report(
                result,
                checkpoints,
                records,
                pricing,
                scope_start=scope_start,
                raw_diagnostics=diagnostics,
                usage_log=usage_log,
                state_path=state_path,
            )
            previous_snapshot, previous_accounting = monitor.load_state_bundle(state_path)
        except BaseException:
            # The temporary rewrite file must never be left behind.
            if rewriter is not None:
                rewriter.abort()
            raise
        if not args.dry_run:
            assert rewriter is not None
            try:
                usage_backup, rewritten, first_line = rewriter.finish()
            except BaseException:
                rewriter.abort()
                raise
            rewriter = None  # committed; nothing left to abort
            final_snapshot, final_accounting = build_final_state(
                previous_snapshot,
                previous_accounting,
                result,
                pricing,
                checkpoints,
                scope_start,
            )
            state_backup = None
            if state_path.exists():
                state_backup = _backup_path(state_path, "before-backfill")
                shutil.copy2(state_path, state_backup)
            monitor.save_state(state_path, final_snapshot, final_accounting)
            report["usage_log_backup"] = str(usage_backup) if usage_backup else None
            report["state_backup"] = str(state_backup) if state_backup else None
            report["checkpoints_corrected"] = rewritten
            report["first_rewritten_line_zero_based"] = first_line
        else:
            report["dry_run"] = True
            report["checkpoints_corrected"] = counted["payloads"]
            report["first_rewritten_line_zero_based"] = 0

        report_path = args.report_path
        if report_path is None:
            stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            report_path = state_path.with_name(f"backfill-report-{stamp}.json")
        monitor._atomic_write_json(report_path.expanduser(), report)
        print_report(report)
        print(f"Report: {report_path.expanduser()}")
        return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=monitor.default_env_path())
    parser.add_argument("--state-path", type=Path, default=monitor.default_state_path())
    parser.add_argument("--usage-log", type=Path, default=monitor.default_usage_log_path())
    parser.add_argument(
        "--start",
        default=DEFAULT_SCOPE_START,
        help=f"UTC replay boundary (default: {DEFAULT_SCOPE_START})",
    )
    parser.add_argument("--report-path", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        run_backfill(args)
        return 0
    except monitor.MonitorError as exc:
        print(f"backfill: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
