#!/usr/bin/env python3
"""Export raw Headroom token/model/usage data for chosen Codex cycles.

Reads the retained Headroom PERF logs plus the monitor state and writes one
self-describing JSON document containing, per cycle:

* the cycle summary as the monitor recorded it,
* per-model aggregates (requests, tokens, cost),
* every percentage crossing with its cumulative cost and token mix,
* the external-imputation events and the outside-VM evidence samples,
* the raw per-request rows (timestamp, model, token counters, priced cost).

This is a read-only reporting tool: it never writes monitor state or logs.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import datetime as dt
import json
import os
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

import monitor


def _iso(timestamp: float) -> str:
    return dt.datetime.fromtimestamp(timestamp, dt.timezone.utc).isoformat()


def _dec(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal("0")


def _state_default() -> Path:
    return Path(
        os.getenv("XDG_STATE_HOME", str(Path.home() / ".local" / "state"))
    ) / "codex-reset-monitor" / "state.json"


def load_requests(log_dir: Path, pricing: monitor.PricingTable) -> list[dict[str, Any]]:
    """Parse every retained PERF row into a priced, self-describing record."""

    rows: list[dict[str, Any]] = []
    for path in monitor.headroom_log_paths(log_dir):
        with path.open("r", errors="ignore") as handle:
            offset = 0
            for line in handle:
                start = offset
                offset += len(line)
                if "PERF model=" not in line:
                    continue
                record = monitor.parse_headroom_perf_line(
                    line, source_file=str(path), source_offset=start, timezone_name="UTC"
                )
                if record is None:
                    continue
                breakdown = monitor.price_usage_record(record, pricing)
                rows.append(
                    {
                        "timestamp": record.timestamp,
                        "timestamp_utc": _iso(record.timestamp),
                        "request_id": record.request_id,
                        "model": record.model,
                        "client": record.client,
                        "input_tokens": record.input_tokens,
                        "output_tokens": record.output_tokens,
                        "cache_read_tokens": record.cache_read_tokens,
                        "raw_cache_write_tokens": record.raw_cache_write_tokens,
                        "uncached_input_tokens": breakdown.uncached_input_tokens,
                        "cached_input_tokens": breakdown.cached_input_tokens,
                        "local_cache_hit": record.local_cache_hit,
                        "processing_tier": breakdown.processing_tier,
                        "long_context": breakdown.long_context,
                        "priced": breakdown.complete,
                        "unpriced_reason": None if breakdown.complete else breakdown.reason,
                        "cost_usd": (
                            str(breakdown.total_cost) if breakdown.complete else None
                        ),
                        "cost_uncached_input_usd": (
                            str(breakdown.uncached_input_cost) if breakdown.complete else None
                        ),
                        "cost_cached_input_usd": (
                            str(breakdown.cached_input_cost) if breakdown.complete else None
                        ),
                        "cost_output_usd": (
                            str(breakdown.output_cost) if breakdown.complete else None
                        ),
                        "source_file": Path(record.source_file).name,
                        "source_offset": record.source_offset,
                    }
                )
    rows.sort(key=lambda row: (row["timestamp"], row["source_file"], row["source_offset"]))
    return rows


def _percent_at(
    crossings: list[dict[str, Any]], timestamps: list[float]
) -> list[int | None]:
    """Cycle percentage in force at each instant, from the crossing timeline.

    A crossing records the tick at which the cycle percentage was first
    observed, so the band for an instant is the newest crossing at or before
    it.  This is what makes "the calls before 24%" directly answerable.
    """

    marks: list[tuple[float, int]] = []
    for crossing in crossings:
        try:
            marks.append((float(crossing["captured_at"]), int(crossing["percent"])))
        except (KeyError, TypeError, ValueError):
            continue
    marks.sort()
    if not marks:
        return [None] * len(timestamps)
    band_times = [mark[0] for mark in marks]
    band_percents = [mark[1] for mark in marks]
    resolved: list[int | None] = []
    for timestamp in timestamps:
        index = bisect.bisect_right(band_times, timestamp) - 1
        resolved.append(band_percents[index] if index >= 0 else None)
    return resolved


def _band_summary(
    rows: list[dict[str, Any]], excluded: frozenset[str]
) -> list[dict[str, Any]]:
    """Per-cycle-percentage totals, so a gap at one percentage is obvious."""

    bands: dict[int | None, dict[str, Any]] = {}
    for row in rows:
        band = row.get("cycle_percent_at_request")
        entry = bands.setdefault(
            band,
            {
                "cycle_percent": band,
                "calls": 0,
                "codex_calls": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cost_usd": "0",
                "models": [],
            },
        )
        is_codex = row["model"].strip().lower() not in excluded
        entry["calls"] += 1
        if is_codex:
            entry["codex_calls"] += 1
            entry["input_tokens"] += int(row["input_tokens"])
            entry["output_tokens"] += int(row["output_tokens"])
            entry["cost_usd"] = str(_dec(entry["cost_usd"]) + _dec(row["cost_usd"]))
            if row["model"] not in entry["models"]:
                entry["models"].append(row["model"])
    ordered = sorted(
        bands.values(), key=lambda item: (-1 if item["cycle_percent"] is None else item["cycle_percent"])
    )
    for entry in ordered:
        entry["cost_usd"] = str(_dec(entry["cost_usd"]))
        entry["models"].sort()
    return ordered


def summarise(rows: Iterable[dict[str, Any]], excluded: frozenset[str]) -> dict[str, Any]:
    """Per-model token/cost totals plus the Codex vs non-Codex split."""

    by_model: dict[str, dict[str, Any]] = {}
    for row in rows:
        name = row["model"]
        entry = by_model.setdefault(
            name,
            {
                "model": name,
                "requests": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_tokens": 0,
                "raw_cache_write_tokens": 0,
                "uncached_input_tokens": 0,
                "cached_input_tokens": 0,
                "cost_usd": "0",
                "priced": True,
                "consumes_codex_quota": name.strip().lower() not in excluded,
            },
        )
        entry["requests"] += 1
        for field in (
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "raw_cache_write_tokens",
            "uncached_input_tokens",
            "cached_input_tokens",
        ):
            entry[field] += int(row[field])
        entry["cost_usd"] = str(_dec(entry["cost_usd"]) + _dec(row["cost_usd"]))
        entry["priced"] = entry["priced"] and bool(row["priced"])
    for entry in by_model.values():
        entry["cost_usd"] = str(_dec(entry["cost_usd"]))
        entry["total_tokens"] = entry["input_tokens"] + entry["output_tokens"]
    return by_model


def build_cycle(
    name: str,
    window: str,
    start: float,
    end: float | None,
    rows: list[dict[str, Any]],
    excluded: frozenset[str],
    state: dict[str, Any],
    evidence: list[dict[str, Any]],
    history: dict[tuple[str, float], list[dict[str, Any]]] | None = None,
    ticks: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    account = state["accounting"]
    window_state = account["windows"][window]
    selected = [row for row in rows if row["timestamp"] >= start and (end is None or row["timestamp"] < end)]
    crossings = (
        (history or {}).get((window, float(start))) or window_state.get("crossings") or []
    )
    bands = _percent_at(crossings, [row["timestamp"] for row in selected])
    selected = [dict(row, cycle_percent_at_request=band) for row, band in zip(selected, bands)]
    codex_rows = [row for row in selected if row["model"].strip().lower() not in excluded]
    other_rows = [row for row in selected if row["model"].strip().lower() in excluded]
    series = [
        tick
        for tick in (ticks or [])
        if tick["timestamp"] >= start and (end is None or tick["timestamp"] <= end)
    ]

    anchor_percent = window_state["anchor"].get("used_percent")
    current = state[window]["used_percent"]
    local_cost = sum((_dec(row["cost_usd"]) for row in codex_rows), Decimal("0"))

    summary: dict[str, Any] = {}
    if end is not None:
        for cycle in window_state.get("cycle_history") or []:
            if cycle["anchor"]["captured_at"] == start and cycle["closed_at"] == end:
                summary = cycle
                break
    else:
        summary = {
            "observed_percent": f"{anchor_percent}% -> {current}%",
            "completed_percentage_points": (
                None if anchor_percent is None else float(current) - float(anchor_percent)
            ),
            "known_cost_usd": str(local_cost),
        }

    # The monitor accumulates live and can drop rows written to the log after
    # the tick that covered their timestamp.  This export re-reads whatever the
    # logs still retain, so it can hold slightly more rows than the monitor
    # recorded.  Publish both so the difference is never silent.
    recorded = (summary.get("cumulative_usage") or {}).get("models") or {}
    coverage: dict[str, Any] = {}
    for model_name, entry in sorted(
        summarise(selected, excluded).items(), key=lambda kv: -kv[1]["requests"]
    ):
        if model_name == monitor._IMPUTED_MODEL_NAME:
            continue
        recorded_requests = int((recorded.get(model_name) or {}).get("requests", 0))
        coverage[model_name] = {
            "rows_in_export": entry["requests"],
            "requests_recorded_by_monitor": recorded_requests,
            "difference": entry["requests"] - recorded_requests,
        }

    return {
        "cycle": name,
        "window": window,
        "excluded_models": sorted(excluded),
        "data_sources": {
            "raw_calls_available": bool(selected),
            "raw_calls": len(selected),
            "monitor_ticks_in_cycle": len(series),
            "note": (
                "raw per-call rows from the retained Headroom logs"
                if selected
                else "no raw calls: the Headroom log rotations covering this "
                "period are gone. 'monitor_ticks' holds the per-tick "
                "cumulative token/model counters the monitor recorded live."
            ),
        },
        "started_at": _iso(start),
        "ended_at": _iso(end) if end is not None else None,
        "duration_hours": round(((end or dt.datetime.now(dt.timezone.utc).timestamp()) - start) / 3600, 3),
        "anchor_used_percent": anchor_percent,
        "end_used_percent": current if end is None else summary.get("observed_percent"),
        "monitor_summary": summary,
        "coverage": coverage,
        "requests": {
            "codex_quota_consuming": len(codex_rows),
            "non_codex_excluded": len(other_rows),
            "total_seen": len(selected),
        },
        "tokens": {
            "codex_quota_consuming": {
                "input": sum(row["input_tokens"] for row in codex_rows),
                "output": sum(row["output_tokens"] for row in codex_rows),
                "cache_read": sum(row["cache_read_tokens"] for row in codex_rows),
                "uncached_input": sum(row["uncached_input_tokens"] for row in codex_rows),
                "cached_input": sum(row["cached_input_tokens"] for row in codex_rows),
                "raw_cache_write": sum(row["raw_cache_write_tokens"] for row in codex_rows),
            },
            "non_codex_excluded": {
                "input": sum(row["input_tokens"] for row in other_rows),
                "output": sum(row["output_tokens"] for row in other_rows),
            },
        },
        "cost_usd": {
            "local_priced_total": str(local_cost),
            "non_codex_excluded_total": str(
                sum((_dec(row["cost_usd"]) for row in other_rows), Decimal("0"))
            ),
        },
        "models": summarise(selected, excluded),
        "crossings": crossings,
        "percent_bands": _band_summary(selected, excluded),
        "outside_vm_evidence": [
            item
            for item in evidence
            if (item.get("timestamp_epoch") or 0) >= start
            and (end is None or (item.get("timestamp_epoch") or 0) < end)
        ],
        "requests_detail": selected,
        "monitor_ticks": series,
    }


def load_window_history(
    log_path: Path,
) -> dict[tuple[str, float], list[dict[str, Any]]]:
    """Latest crossing list per ``(window, anchor)`` seen in the usage log.

    A closed cycle's crossings are dropped from monitor state when the window
    restarts, so the only surviving copy is the sequence of ``usage_v2``
    snapshots.  Keep the last one per cycle rather than the whole history.
    """

    latest: dict[tuple[str, float], tuple[float, list[dict[str, Any]]]] = {}
    if not log_path.exists():
        return {}
    with log_path.open("r", errors="ignore") as handle:
        for line in handle:
            marker = line.find(" usage_v2=")
            if marker < 0:
                continue
            try:
                payload = json.loads(line[marker + len(" usage_v2=") :])
            except json.JSONDecodeError:
                continue
            tick = _parse_log_timestamp(line[:19])
            if tick is None:
                continue
            windows = payload.get("windows")
            if not isinstance(windows, dict):
                continue
            for name, window in windows.items():
                if not isinstance(window, dict):
                    continue
                anchor = window.get("anchor_captured_at")
                crossings = window.get("crossings")
                if anchor is None or not isinstance(crossings, list) or not crossings:
                    continue
                key = (str(name), float(anchor))
                previous = latest.get(key)
                if previous is None or tick >= previous[0]:
                    latest[key] = (tick, crossings)
    return {key: value[1] for key, value in latest.items()}


def load_evidence(log_path: Path) -> list[dict[str, Any]]:
    """Outside-VM evidence samples recorded on each usage_v2 log line."""

    samples: list[dict[str, Any]] = []
    if not log_path.exists():
        return samples
    with log_path.open("r", errors="ignore") as handle:
        for line in handle:
            marker = line.find(" usage_v2=")
            if marker < 0:
                continue
            try:
                payload = json.loads(line[marker + len(" usage_v2=") :])
            except json.JSONDecodeError:
                continue
            evidence = payload.get("external_evidence")
            if not isinstance(evidence, dict):
                continue
            weekly = payload.get("weekly_estimate")
            windows = payload.get("windows") if isinstance(payload.get("windows"), dict) else {}
            weekly = weekly if isinstance(weekly, dict) else windows.get("secondary", {})
            weekly = weekly if isinstance(weekly, dict) else {}
            samples.append(
                {
                    "timestamp": line[:19],
                    "timestamp_epoch": _parse_log_timestamp(line[:19]),
                    "period_id": payload.get("period_id"),
                    "weekly_used_percent": weekly.get("current_used_percent"),
                    "weekly_estimate_status": weekly.get("status"),
                    "weekly_account_total_100_usd": weekly.get("account_total_100_usd"),
                    **evidence,
                }
            )
    return samples


def _parse_log_timestamp(text: str) -> float | None:
    """Epoch seconds for a ``YYYY-MM-DD HH:MM:SS`` usage-log prefix."""

    try:
        return dt.datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=dt.timezone.utc
        ).timestamp()
    except ValueError:
        return None


def load_local_period_daily_totals(log_path: Path) -> dict[str, dict[str, Any]]:
    """Sum local usage periods by checkpoint UTC date (periods may straddle dates)."""
    daily: dict[str, dict[str, Any]] = {}
    if not log_path.exists():
        return daily
    with log_path.open("r", errors="ignore") as handle:
        for line in handle:
            marker = line.find(" usage_v2=")
            if marker < 0 or len(line) < 10:
                continue
            try:
                payload = json.loads(line[marker + len(" usage_v2=") :])
            except json.JSONDecodeError:
                continue
            total = payload.get("total")
            if not isinstance(total, dict):
                continue
            day = line[:10]
            entry = daily.setdefault(day, {
                "checkpoint_rows": 0, "requests": 0, "input_tokens": 0,
                "output_tokens": 0, "known_cost_usd": Decimal("0"),
            })
            entry["checkpoint_rows"] += 1
            for key in ("requests", "input_tokens", "output_tokens"):
                try:
                    entry[key] += max(0, int(total.get(key, 0) or 0))
                except (TypeError, ValueError):
                    pass
            entry["known_cost_usd"] += _dec(total.get("known_cost"))
    for entry in daily.values():
        entry["local_tokens"] = entry["input_tokens"] + entry["output_tokens"]
        entry["known_cost_usd"] = str(entry["known_cost_usd"])
    return daily


def account_daily_usage_snapshot(
    evidence: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    excluded: frozenset[str],
    log_path: Path,
) -> dict[str, Any] | None:
    """Pair account daily buckets with local evidence without claiming attribution."""
    candidates = [
        item for item in evidence
        if isinstance(item.get("account_daily_buckets"), list)
        and item.get("account_daily_buckets")
        and item.get("timestamp_epoch") is not None
    ]
    if not candidates:
        return None
    sample = max(candidates, key=lambda item: float(item["timestamp_epoch"]))
    account_days = {
        str(item.get("startDate")): max(0, int(item.get("tokens", 0) or 0))
        for item in sample.get("account_daily_buckets", [])
        if isinstance(item, dict) and item.get("startDate")
    }
    raw_local: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row["model"].strip().lower() in excluded:
            continue
        day = str(row.get("timestamp_utc", ""))[:10]
        if len(day) != 10:
            continue
        entry = raw_local.setdefault(day, {
            "requests": 0, "input_tokens": 0, "output_tokens": 0,
            "priced_cost_usd": Decimal("0"),
        })
        entry["requests"] += 1
        entry["input_tokens"] += int(row.get("input_tokens", 0) or 0)
        entry["output_tokens"] += int(row.get("output_tokens", 0) or 0)
        if row.get("cost_usd") is not None:
            entry["priced_cost_usd"] += _dec(row["cost_usd"])
    checkpoint_local = load_local_period_daily_totals(log_path)
    recent_counter_flags = []
    try:
        sample_time = float(sample["timestamp_epoch"])
    except (KeyError, TypeError, ValueError):
        sample_time = 0.0
    for item in evidence:
        reason = str(item.get("reason", ""))
        try:
            timestamp = float(item.get("timestamp_epoch"))
        except (TypeError, ValueError):
            continue
        try:
            external_tokens = int(item.get("external_tokens_estimate") or 0)
        except (TypeError, ValueError):
            external_tokens = 0
        if timestamp >= sample_time - 7 * 86400 and (
            reason == "counter_catchup" or external_tokens > 0
        ):
            recent_counter_flags.append({
                "observed_at_utc": item.get("timestamp"),
                "reason": reason,
                "account_lifetime_delta": item.get("account_lifetime_delta"),
                "local_period_tokens": item.get("local_period_tokens"),
                "external_tokens_estimate": item.get("external_tokens_estimate"),
                "weekly_used_percent": item.get("weekly_used_percent"),
                "weekly_estimate_status": item.get("weekly_estimate_status"),
                "weekly_account_total_100_usd": item.get("weekly_account_total_100_usd"),
            })
    days = []
    for day, account_tokens in sorted(account_days.items()):
        raw = raw_local.get(day, {
            "requests": 0, "input_tokens": 0, "output_tokens": 0,
            "priced_cost_usd": Decimal("0"),
        })
        checkpoint = checkpoint_local.get(day, {})
        raw_tokens = raw["input_tokens"] + raw["output_tokens"]
        checkpoint_tokens = checkpoint.get("local_tokens")
        days.append({
            "date": day,
            "account_reported_tokens": account_tokens,
            "retained_headroom_requests": raw["requests"],
            "retained_headroom_tokens": raw_tokens,
            "retained_headroom_priced_cost_usd": str(raw["priced_cost_usd"]),
            "monitor_checkpoint_end_date_requests": checkpoint.get("requests"),
            "monitor_checkpoint_end_date_tokens": checkpoint_tokens,
            "account_minus_monitor_checkpoint_tokens": (
                account_tokens - checkpoint_tokens if checkpoint_tokens is not None else None
            ),
            "differences_are_external_usage_estimates": False,
        })
    return {
        "source": "Codex app-server account/usage/read dailyUsageBuckets",
        "observed_at_utc": sample.get("timestamp"),
        "account_lifetime_tokens": sample.get("account_lifetime_tokens"),
        "daily_buckets": days,
        "recent_counter_flags": recent_counter_flags,
        "interpretation": (
            "Account-reported daily totals are preserved as reported. They are not quota-cycle or per-model "
            "attribution. Local raw rows may be incomplete after proxy rotation; checkpoint totals are grouped "
            "by checkpoint end date and can span midnight. Counter differences are diagnostic only, not an "
            "external-token estimate."
        ),
    }


def load_latest_usage_checkpoint(log_path: Path) -> dict[str, Any] | None:
    """Latest parsed full-accounting log line, including estimate and evidence."""
    if not log_path.exists():
        return None
    for line in reversed(log_path.read_text(encoding="utf-8", errors="ignore").splitlines()):
        marker = line.find(" usage_v2=")
        if marker < 0:
            continue
        try:
            payload = json.loads(line[marker + len(" usage_v2=") :])
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return {"timestamp": line[:19], "payload": payload}
    return None


def load_tick_series(log_path: Path) -> list[dict[str, Any]]:
    """Per-tick cumulative record the monitor wrote, for every window.

    Headroom rotates its proxy logs at ~10 MB, so anything older than the
    retained rotations has no raw per-call rows left.  The monitor's own
    ``usage_v2`` line is not rotated, so it is the durable record: it carries
    the cumulative token and model counters for each window at each check.
    """

    series: list[dict[str, Any]] = []
    if not log_path.exists():
        return series
    with log_path.open("r", errors="ignore") as handle:
        for line in handle:
            marker = line.find(" usage_v2=")
            if marker < 0:
                continue
            try:
                payload = json.loads(line[marker + len(" usage_v2=") :])
            except json.JSONDecodeError:
                continue
            tick = _parse_log_timestamp(line[:19])
            if tick is None:
                continue
            windows = payload.get("windows")
            if not isinstance(windows, dict):
                continue
            for name, window in windows.items():
                if not isinstance(window, dict):
                    continue
                models = window.get("cumulative_usage", {}).get("models") or {}
                series.append(
                    {
                        "timestamp": tick,
                        "timestamp_utc": _iso(tick),
                        "window": str(name),
                        "period_id": payload.get("period_id"),
                        "anchor_used_percent": window.get("anchor_used_percent"),
                        "used_percent": window.get("current_used_percent"),
                        "known_cost_usd": window.get("known_cost_usd"),
                        "imputed_cost_usd": window.get("imputed_cost_usd"),
                        "full_window_usd": window.get("full_window_usd"),
                        "confidence": window.get("confidence"),
                        "models": {
                            str(m): {
                                "requests": row.get("requests"),
                                "input_tokens": row.get("input_tokens"),
                                "output_tokens": row.get("output_tokens"),
                                "cache_read_tokens": row.get("cache_read_tokens"),
                                "raw_cache_write_tokens": row.get("raw_cache_write_tokens"),
                                "known_cost": row.get("known_cost"),
                            }
                            for m, row in models.items()
                            if isinstance(row, dict)
                        },
                    }
                )
    series.sort(key=lambda item: item["timestamp"])
    return series


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-path", default=str(_state_default()))
    parser.add_argument("--usage-log", default=None)
    parser.add_argument(
        "--headroom-log-dir",
        default=os.getenv("HEADROOM_LOG_DIR", str(Path.home() / ".headroom" / "logs")),
    )
    parser.add_argument(
        "--pricing-file",
        default=os.getenv("MODEL_PRICING_FILE", "")
        or str(monitor.default_pricing_path()),
    )
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)

    state_path = Path(args.state_path).expanduser()
    state = json.loads(state_path.read_text(encoding="utf-8"))
    account = state["accounting"]
    log_path = Path(args.usage_log).expanduser() if args.usage_log else state_path.parent / "usage.log"

    pricing = monitor.load_model_pricing(Path(args.pricing_file).expanduser())
    excluded = monitor.headroom_excluded_models()
    log_dir = Path(args.headroom_log_dir).expanduser()

    rows = load_requests(log_dir, pricing)
    evidence = load_evidence(log_path)
    history = load_window_history(log_path)
    ticks = load_tick_series(log_path)
    daily_usage_snapshot = account_daily_usage_snapshot(evidence, rows, excluded, log_path)
    latest_checkpoint = load_latest_usage_checkpoint(log_path)
    latest_weekly: dict[str, Any] | None = None
    if latest_checkpoint:
        latest_payload = latest_checkpoint["payload"]
        weekly = ((latest_payload.get("windows") or {}).get("secondary") or {})
        latest_external = latest_payload.get("external_evidence") or {}
        external_unquantified = bool(
            weekly.get("has_external_usage_unquantified")
            or weekly.get("status") == "external_usage_unquantified"
            or weekly.get("external_usage_status") == "unquantified"
        )
        latest_weekly = {
            "as_of_utc": latest_checkpoint["timestamp"] + " UTC",
            "used_percent": weekly.get("current_used_percent"),
            "status": weekly.get("status"),
            "external_usage_status": weekly.get("external_usage_status"),
            "account_total_100_usd": weekly.get("account_total_100_usd"),
            "headroom_only_100_usd": (
                None if external_unquantified else weekly.get("local_token_backed_100_usd")
            ),
            "local_known_cost_usd": weekly.get("known_cost_usd"),
            "counted_percentage_points": weekly.get("counted_percentage_points"),
            "locally_attributed_percentage_points": (
                None if external_unquantified
                else weekly.get("locally_attributed_percentage_points")
            ),
            "excluded_no_token_percentage_points": weekly.get("excluded_no_token_percentage_points"),
            "counter_reason": latest_external.get("reason"),
            "account_lifetime_delta": latest_external.get("account_lifetime_delta"),
            "local_period_tokens": latest_external.get("local_period_tokens"),
            "has_external_usage_unquantified": weekly.get("has_external_usage_unquantified"),
        }

    cycles: list[dict[str, Any]] = []

    # Every completed cycle the monitor still retains, oldest first, both
    # windows.  Cycles whose raw proxy logs have rotated away keep their
    # per-tick series so the period is still represented.
    for window in ("secondary", "primary"):
        window_state = account["windows"][window]
        for cycle in window_state.get("cycle_history") or []:
            start = cycle["anchor"]["captured_at"]
            end = cycle["closed_at"]
            points = cycle.get("completed_percentage_points") or 0
            if points < 1:
                continue
            cycles.append(
                build_cycle(
                    f"{window}_{int(points)}pt_{_iso(start)[:10]}",
                    window,
                    start,
                    end,
                    rows,
                    excluded,
                    state,
                    evidence,
                    history,
                    ticks,
                )
            )
    cycles.sort(key=lambda item: item["started_at"])

    # The cycles still in flight.
    for window in ("primary", "secondary"):
        window_state = account["windows"][window]
        cycles.append(
            build_cycle(
                f"current_{window}",
                window,
                window_state["anchor"]["captured_at"],
                None,
                rows,
                excluded,
                state,
                evidence,
                history,
                ticks,
            )
        )

    document = {
        "schema_version": 1,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "source": {
            "state_path": str(state_path),
            "usage_log": str(log_path),
            "headroom_log_dir": str(log_dir),
            "pricing_file": str(Path(args.pricing_file).expanduser()),
            "pricing_revision": pricing.revision,
            "pricing_currency": pricing.currency,
            "pricing_unit": pricing.unit,
            "pricing_mode": pricing.pricing_mode,
            "excluded_models": sorted(excluded),
            "live_snapshot": {
                "primary_used_percent": state["primary"]["used_percent"],
                "primary_resets_at": _iso(state["primary"]["resets_at"]),
                "secondary_used_percent": state["secondary"]["used_percent"],
                "secondary_resets_at": _iso(state["secondary"]["resets_at"]),
            },
        },
        "account_daily_usage_snapshot": daily_usage_snapshot,
        "live_weekly_checkpoint": latest_weekly,
        "notes": [
            "cost_usd is a current-price-equivalent figure, not an invoice.",
            "account_daily_usage_snapshot preserves account-wide daily token buckets beside retained local usage; differences are not attributed automatically.",
            "Cache writes are never a separately billed category: uncached input is",
            "priced at the model's ordinary input rate.",
            "Requests from models outside the Codex quota (excluded_models) are kept",
            "in requests_detail and flagged consumes_codex_quota=false so the raw",
            "evidence stays complete, but they never contribute to cost_usd.",
            "monitor_summary may include external-imputed cost for percentage points",
            "that had no local Headroom rows; requests_detail has no such rows.",
        ],
        "cycles": cycles,
    }

    out = Path(args.output).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(document, indent=1, sort_keys=False, default=str) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {out} ({out.stat().st_size:,} bytes)")

    # Per-cycle files: a 14 MB blob is unusable when you want "the calls before
    # 24%", so each cycle also gets its own JSON, a CSV of every call, and a
    # compact markdown table of the per-percentage breakdown.
    bundle = out.parent / "cycles"
    bundle.mkdir(parents=True, exist_ok=True)
    for cycle in cycles:
        name = cycle["cycle"]
        (bundle / f"{name}.json").write_text(
            json.dumps(cycle, indent=1, sort_keys=False, default=str) + "\n",
            encoding="utf-8",
        )
        _write_cycle_csv(bundle / f"{name}.calls.csv", cycle)
        _write_ticks_csv(bundle / f"{name}.ticks.csv", cycle)
        (bundle / f"{name}.md").write_text(_cycle_markdown(cycle), encoding="utf-8")

    print(f"wrote {bundle}/ ({len(cycles)} cycles)")
    for cycle in cycles:
        detail = cycle["requests_detail"]
        print(
            f"  {cycle['cycle']:<28} rows={len(detail):>6} "
            f"codex={cycle['requests']['codex_quota_consuming']:>6} "
            f"excluded={cycle['requests']['non_codex_excluded']:>5} "
            f"local=${_dec(cycle['cost_usd']['local_priced_total']):.6f}"
        )
    return 0


_CSV_FIELDS = (
    "timestamp_utc",
    "cycle_percent_at_request",
    "model",
    "consumes_codex_quota",
    "request_id",
    "client",
    "input_tokens",
    "cache_read_tokens",
    "uncached_input_tokens",
    "raw_cache_write_tokens",
    "output_tokens",
    "cost_usd",
    "cost_uncached_input_usd",
    "cost_cached_input_usd",
    "cost_output_usd",
    "priced",
    "unpriced_reason",
    "long_context",
    "local_cache_hit",
    "processing_tier",
    "source_file",
    "source_offset",
)


def _write_cycle_csv(path: Path, cycle: dict[str, Any]) -> None:
    excluded = set(cycle.get("excluded_models") or ())
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=_CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in cycle["requests_detail"]:
            record = dict(row)
            record["consumes_codex_quota"] = row["model"].strip().lower() not in excluded
            # A non-Codex call has no Codex price at all, so leave its cost
            # columns blank rather than writing a misleading 0.
            if not record["consumes_codex_quota"]:
                for field in (
                    "cost_usd",
                    "cost_uncached_input_usd",
                    "cost_cached_input_usd",
                    "cost_output_usd",
                ):
                    record[field] = ""
            writer.writerow(record)


def _write_ticks_csv(path: Path, cycle: dict[str, Any]) -> None:
    """One row per monitor check: the durable record for rotated-away periods."""

    fields = (
        "timestamp_utc",
        "used_percent",
        "known_cost_usd",
        "imputed_cost_usd",
        "full_window_usd",
        "confidence",
        "model",
        "requests",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "known_cost",
    )
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for tick in cycle.get("monitor_ticks") or []:
            models = tick.get("models") or {}
            if not models:
                writer.writerow({**{k: tick.get(k) for k in fields}, "model": ""})
                continue
            for model_name, row in sorted(models.items()):
                writer.writerow(
                    {
                        "timestamp_utc": tick.get("timestamp_utc"),
                        "used_percent": tick.get("used_percent"),
                        "known_cost_usd": tick.get("known_cost_usd"),
                        "imputed_cost_usd": tick.get("imputed_cost_usd"),
                        "full_window_usd": tick.get("full_window_usd"),
                        "confidence": tick.get("confidence"),
                        "model": model_name,
                        "requests": row.get("requests"),
                        "input_tokens": row.get("input_tokens"),
                        "output_tokens": row.get("output_tokens"),
                        "cache_read_tokens": row.get("cache_read_tokens"),
                        "known_cost": row.get("known_cost"),
                    }
                )


def _cycle_markdown(cycle: dict[str, Any]) -> str:
    summary = cycle["monitor_summary"]
    lines = [
        f"# {cycle['cycle']}",
        "",
        f"- window: `{cycle['window']}`",
        f"- started: {cycle['started_at']}",
        f"- ended: {cycle['ended_at'] or '(in flight)'}",
        f"- observed: {summary.get('observed_percent')}",
        f"- monitor recorded: ${summary.get('cumulative_usage', {}).get('known_cost_usd')}",
        f"- local re-priced from logs: ${cycle['cost_usd']['local_priced_total']}",
        f"- calls: {cycle['requests']['codex_quota_consuming']} Codex, "
        f"{cycle['requests']['non_codex_excluded']} non-Codex",
        "",
        "## Cost per cycle percentage",
        "",
        "| cycle % | codex calls | input | output | cost | models |",
        "|---:|---:|---:|---:|---:|---|",
    ]
    for band in cycle["percent_bands"]:
        pct = band["cycle_percent"]
        lines.append(
            f"| {pct if pct is not None else '-'} | {band['codex_calls']} | "
            f"{band['input_tokens']:,} | {band['output_tokens']:,} | "
            f"${_dec(band['cost_usd']):.8f} | {', '.join(band['models']) or '-'} |"
        )
    lines.append("")
    lines.append("## Crossings")
    lines.append("")
    lines.append("| % | cumulative $ | added $ | status |")
    lines.append("|---:|---:|---:|---|")
    previous = Decimal("0")
    for crossing in cycle["crossings"]:
        value = _dec(crossing.get("cumulative_cost_usd"))
        status = "imputed" if crossing.get("imputed") else "measured"
        lines.append(
            f"| {crossing.get('percent')} | {value:.8f} | {value - previous:.8f} | {status} |"
        )
        previous = value
    lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
