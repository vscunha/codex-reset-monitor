"""Read-only, evidence-labeled audit of a completed weekly quota cycle.

No per-point requests are fabricated for skipped percentages. Reconstructs
original account-counter observations from an explicitly supplied pre-backfill
log backup, while retaining the export's raw proxy calls and monitor summary.
"""

from __future__ import annotations

import argparse
import bisect
import datetime as dt
import json
import os
import tempfile
from decimal import Decimal
from pathlib import Path
from typing import Any


WEAK_JUMPS = {(54, 61), (61, 69), (93, 96)}  # hypothesis, not measured missing spend


def dec(value: Any) -> Decimal:
    return Decimal(str(value or "0"))


def original_evidence(path: Path, start: float, end: float) -> dict[int, dict[str, Any]]:
    """Recover only observations inside the requested cycle; never modify logs."""
    found: dict[int, dict[str, Any]] = {}
    with path.open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            if " usage_v2=" not in line:
                continue
            try:
                when = dt.datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S").replace(
                    tzinfo=dt.timezone.utc
                ).timestamp()
            except ValueError:
                continue
            if not start <= when <= end:
                continue
            payload = json.loads(line.split(" usage_v2=", 1)[1])
            evidence = payload.get("external_evidence")
            if isinstance(evidence, dict):
                found[int(when)] = evidence
    return found


def analyze_cycle(
    cycle: dict[str, Any], evidence: dict[int, dict[str, Any]]
) -> dict[str, Any]:
    xs = cycle["crossings"]
    excluded = {name.lower() for name in cycle["excluded_models"]}
    calls = sorted(cycle["requests_detail"], key=lambda r: r["timestamp"])
    times = [r["timestamp"] for r in calls]
    intervals: list[dict[str, Any]] = []
    for a, b in zip(xs, xs[1:]):
        first, last = a["percent"], b["percent"]
        left = bisect.bisect_right(times, float(a["captured_at"]))
        right = bisect.bisect_right(times, float(b["captured_at"]))
        local = [r for r in calls[left:right] if r["model"].lower() not in excluded]
        modeled = dec(b.get("costs_by_model", {}).get("external-imputed")) - dec(
            a.get("costs_by_model", {}).get("external-imputed")
        )
        # Model deltas preserve their original precision. Subtracting rounded
        # cumulative totals can yield a spurious -3e-27 priced remainder.
        priced = sum((
            dec(b.get("costs_by_model", {}).get(name)) - dec(a.get("costs_by_model", {}).get(name))
            for name in set(a.get("costs_by_model", {})) | set(b.get("costs_by_model", {}))
            if name != "external-imputed"
        ), Decimal(0))
        assert last > first and priced >= 0 and modeled >= 0
        tick_evidence = evidence.get(int(b["captured_at"]))
        intervals.append({
            "from_percent": first, "to_percent": last, "points": last - first,
            "start_at": a["captured_at"], "end_at": b["captured_at"],
            "raw_codex_calls_in_interval": len(local),
            "monitor_priced_usd": str(priced),
            "monitor_imputed_usd": str(modeled),
            "tick_account_lifetime_delta": (
                tick_evidence.get("account_lifetime_delta") if tick_evidence else None
            ),
            "tick_evidence_reason": tick_evidence.get("reason") if tick_evidence else None,
            "status": ("synthetic_mean_imputation" if modeled else
                       "multi_point_interval_not_per_point_measured" if last - first > 1
                       else "single_point_observed_interval"),
            "weak_jump_scenario": (first, last) in WEAK_JUMPS,
        })
    summary = cycle["monitor_summary"]
    total = dec(summary["known_cost_usd"])
    modeled_total = dec(summary["model_attribution"]["external-imputed"]["cost_usd"])
    first_100 = dec(xs[-1]["cumulative_cost_usd"])
    assert abs(sum((dec(r["monitor_priced_usd"]) + dec(r["monitor_imputed_usd"]) for r in intervals), Decimal(0)) - first_100) < Decimal("1e-20")
    assert dec(intervals[-1]["monitor_imputed_usd"]) <= modeled_total
    weak = [r for r in intervals if r["weak_jump_scenario"]]
    assert {(r["from_percent"], r["to_percent"]) for r in weak} == WEAK_JUMPS
    points = sum(r["points"] for r in intervals if not r["weak_jump_scenario"] and not dec(r["monitor_imputed_usd"]))
    cost = sum((dec(r["monitor_priced_usd"]) for r in intervals if not r["weak_jump_scenario"] and not dec(r["monitor_imputed_usd"])), Decimal(0))
    weak_paid = sum((dec(r["monitor_priced_usd"]) for r in weak), Decimal(0))
    weak_points = sum(r["points"] for r in weak)
    rate = cost / points
    scenario = total - weak_paid + rate * weak_points
    raw_cost = dec(cycle["cost_usd"]["local_priced_total"])
    synthetic_intervals = [r for r in intervals if dec(r["monitor_imputed_usd"]) > 0]
    evidence_groups = {
        "counter_zero": [r for r in synthetic_intervals if r["tick_account_lifetime_delta"] == 0],
        "counter_positive": [r for r in synthetic_intervals if (r["tick_account_lifetime_delta"] or 0) > 0],
        "counter_unavailable": [r for r in synthetic_intervals if r["tick_account_lifetime_delta"] is None],
    }
    synthetic_evidence = {name: {
        "points": sum(r["points"] for r in group),
        "synthetic_usd": str(sum((dec(r["monitor_imputed_usd"]) for r in group), Decimal(0))),
    } for name, group in evidence_groups.items()}
    no_token_points = sum(r["points"] for r in synthetic_intervals)
    token_backed_points = max(0, int(xs[-1]["percent"]) - int(xs[0]["percent"]) - no_token_points)
    priced_at_first_100 = first_100 - sum(
        (dec(r["monitor_imputed_usd"]) for r in intervals if r["to_percent"] <= 100), Decimal(0)
    )
    local_token_backed_estimate = (
        priced_at_first_100 * Decimal(100) / Decimal(token_backed_points)
        if token_backed_points > 0 else None
    )
    selected_historical_adjustment = {
        "status": "conditional_selected_not_measured",
        "include_in_historical_comparison": True,
        "adjusted_value_at_first_100_usd": str(scenario - (total - first_100)),
        "adjusted_value_through_close_usd": str(scenario),
        "selected_value_usd": str(scenario),
        "replaced_intervals": ["54->61", "61->69", "93->96"],
        "replaced_points": weak_points,
        "recorded_weak_interval_cost_usd": str(weak_paid),
        "replacement_cost_usd": str(rate * weak_points),
        "reference_points": points,
        "reference_priced_usd": str(cost),
        "assumption": (
            "The three selected jumps were under-recorded locally; replace their "
            "recorded cost at the observed reference cost per point."
        ),
        "raw_monitor_values_preserved": True,
    }
    return {
        "cycle": cycle["cycle"], "intervals": intervals,
        "reconciliation": {
            "monitor_priced_usd": str(total - modeled_total),
            "monitor_imputed_usd": str(modeled_total),
            "monitor_total_usd": str(total),
            "first_observed_100_usd": str(first_100),
            "priced_after_first_100_usd": str(total - first_100),
            "raw_priced_through_reset_boundary_usd": str(raw_cost),
            "raw_minus_monitor_priced_usd": str(raw_cost - (total - modeled_total)),
        },
        "selected_historical_adjustment": selected_historical_adjustment,
        "conditional_scenario": {
            "not_a_measured_total": True,
            "selection_rule": "Exclude historical synthetic intervals and 54->61, 61->69, 93->96; use crossing-to-crossing costs before first 100% observation.",
            "reference_points": points, "reference_priced_usd": str(cost),
            "reference_rate_usd_per_point": str(rate),
            "replaced_points": weak_points, "recorded_cost_replaced_usd": str(weak_paid),
            "replacement_usd": str(rate * weak_points),
            "uniform_100_points_usd": str(rate * 100),
            "hypothetical_close_total_usd": str(scenario),
            "hypothetical_first_100_usd": str(scenario - (total - first_100)),
        },
        "original_evidence_samples": evidence,
        "synthetic_evidence_summary": synthetic_evidence,
        "local_token_backed_estimate": {
            "basis": "priced local cost at first observed 100% / points excluding historical imputed no-local-token rises",
            "priced_local_cost_at_first_100_usd": str(priced_at_first_100),
            "quota_points_total": int(xs[-1]["percent"]) - int(xs[0]["percent"]),
            "excluded_no_local_token_points": no_token_points,
            "counted_token_backed_points": token_backed_points,
            "estimate_100_usd": str(local_token_backed_estimate) if local_token_backed_estimate is not None else None,
            "not_total_account_cost": True,
        },
    }


def markdown(audit: dict[str, Any]) -> str:
    r = audit["reconciliation"]
    s = audit["conditional_scenario"]
    e = audit["synthetic_evidence_summary"]
    local = audit["local_token_backed_estimate"]
    lines = [
        "# Weekly observed intervals — NOT independent 1% measurements", "",
        "The percentage is sampled at checkpoints. A +N jump is **one interval**, not N measured points.",
        "Request counts are raw proxy rows between crossing timestamps, not proof they caused the rise.",
        "Local prices and synthetic amounts are the monitor's preserved values; account-counter evidence is restored from the pre-backfill backup.", "",
        "| UTC end | Weekly range | Points | Raw Codex calls | Monitor priced $ | Synthetic $ | Account lifetime tick Δ | Evidence |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for row in audit["intervals"]:
        stamp = dt.datetime.fromtimestamp(row["end_at"], dt.timezone.utc).strftime("%m-%d %H:%M:%S")
        delta = row["tick_account_lifetime_delta"]
        reason = row["tick_evidence_reason"] or "unavailable"
        if row["weak_jump_scenario"]:
            reason += "; scenario excludes this interval"
        lines.append(
            f"| {stamp} | {row['from_percent']}→{row['to_percent']}% | +{row['points']} | "
            f"{row['raw_codex_calls_in_interval']} | ${dec(row['monitor_priced_usd']):.8f} | "
            f"${dec(row['monitor_imputed_usd']):.8f} | "
            f"{delta if delta is not None else '—'} | {reason} |"
        )
    lines += ["", "## Reconciliation and uncertainty", "",
        f"Monitor through close: **${dec(r['monitor_total_usd']):.8f}** = "
        f"${dec(r['monitor_priced_usd']):.8f} priced + ${dec(r['monitor_imputed_usd']):.8f} synthetic.",
        f"First observed 100%: ${dec(r['first_observed_100_usd']):.8f}; "
        f"later recorded while capped at 100%: ${dec(r['priced_after_first_100_usd']):.8f}.",
        f"Raw re-read through reset is ${dec(r['raw_priced_through_reset_boundary_usd']):.8f}; "
        f"the ${dec(r['raw_minus_monitor_priced_usd']):.8f} difference includes raw calls not accepted "
        "into the closing monitor summary. In this cycle, 94 GPT-6 calls ($0.16781282) "
        "landed between the last 100% tick and the observed reset; their quota-cycle "
        "ownership cannot be determined from snapshots.", "",
        f"Historical synthetic intervals: {e['counter_zero']['points']} points / "
        f"${dec(e['counter_zero']['synthetic_usd']):.8f} with a zero account-counter tick delta; "
        f"{e['counter_positive']['points']} points with a positive delta; "
        f"{e['counter_unavailable']['points']} point(s) without an original observation. "
        "Account lifetime counters can lag; these classifications do not prove where usage occurred.", "",
        "### Selected historical comparison value — conditional, not measured", "",
        f"Use **${dec(audit['selected_historical_adjustment']['adjusted_value_at_first_100_usd']):.8f} at first 100%** "
        f"and **${dec(audit['selected_historical_adjustment']['adjusted_value_through_close_usd']):.8f} through close** "
        "as the selected working values for this cycle's historical comparison. This replaces recorded costs "
        "for 54→61, 61→69, and 93→96 with the observed reference rate; it is an assumption, not an invoice.", "",
        "### Alternate local-only diagnostic — not the selected cycle value", "",
        f"At first 100%, preserved priced local cost was ${dec(local['priced_local_cost_at_first_100_usd']):.8f}. "
        f"Exclude {local['excluded_no_local_token_points']} points whose rise had no local-token interval; "
        f"{local['counted_token_backed_points']} points remain. "
        f"Projection: **${dec(local['estimate_100_usd']):.8f}** per 100 token-backed quota points. "
        "This is a local-only price-equivalent projection, not total account cost; token presence alone does not prove the proxy captured all traffic in multi-point jumps.", "",
        "### Calculation details for the selected conditional value", "", 
        f"Reference selection: {s['reference_points']} observed crossing points, "
        f"${dec(s['reference_priced_usd']):.8f} recorded = ${dec(s['reference_rate_usd_per_point']):.8f}/point. "
        "This selection is a hypothesis, NOT proof that the other intervals lost proxy traffic.",
        f"Replace the ${dec(s['recorded_cost_replaced_usd']):.8f} already recorded for "
        f"{s['replaced_points']} selected jump points with "
        f"${dec(s['replacement_usd']):.8f} at that assumed rate: "
        f"**${dec(s['hypothetical_close_total_usd']):.8f}** through close, "
        f"or ${dec(s['hypothetical_first_100_usd']):.8f} at the first observed 100%. "
        "Neither is a verified dollar cost of quota. The historical synthetic costs remain modeled.",
        f"A *different* uniform-rate assumption across all 100 points gives "
        f"${dec(s['uniform_100_points_usd']):.8f}; do not mistake that extrapolation for another observation.",
        ""]
    return "\n".join(lines)


def _unquantified_adjustment(cycle: dict[str, Any]) -> dict[str, Any]:
    evidence = cycle.get("outside_vm_evidence") or []
    reasons = sorted({str(row.get("reason", "unknown")) for row in evidence if isinstance(row, dict)})
    is_current = cycle.get("ended_at") is None
    stale = any(reason in {"stale_counter", "counter_catchup"} for reason in reasons)
    return {
        "status": "external_usage_unquantified",
        "include_in_historical_comparison": False,
        "adjusted_value_at_first_100_usd": None,
        "adjusted_value_through_close_usd": None,
        "reason": (
            "The live account counter is stale/catching up; wait for a reliable sample."
            if is_current and stale else
            "No reliable per-cycle account-counter evidence is retained; do not count the raw monitor estimate as adjusted."
        ),
        "evidence_reasons": reasons,
        "raw_monitor_values_preserved": True,
    }


def build_historical_adjustment_ledger(
    export: dict[str, Any], audit: dict[str, Any]
) -> dict[str, Any]:
    selected = audit["selected_historical_adjustment"]
    live_checkpoint = export.get("live_weekly_checkpoint") or {}
    cycles = []
    for cycle in export.get("cycles", []):
        if cycle.get("window") != "secondary":
            continue
        adjustment = selected if cycle.get("cycle") == audit["cycle"] else _unquantified_adjustment(cycle)
        summary = cycle.get("monitor_summary") or {}
        cumulative = summary.get("cumulative_usage") or {}
        raw_monitor_total = summary.get("known_cost_usd") or cumulative.get("known_cost_usd")
        if cycle.get("ended_at") is None and cycle.get("cycle") == "current_secondary":
            raw_monitor_total = live_checkpoint.get("local_known_cost_usd") or raw_monitor_total
        cycles.append({
            "cycle": cycle.get("cycle"),
            "started_at": cycle.get("started_at"),
            "ended_at": cycle.get("ended_at"),
            "raw_monitor_total_usd": raw_monitor_total,
            **adjustment,
        })
    return {
        "schema_version": 1,
        "as_of": export.get("generated_at"),
        "account_daily_usage_snapshot": export.get("account_daily_usage_snapshot"),
        "live_cycle_snapshot": export.get("live_weekly_checkpoint"),
        "policy": (
            "Use only selected_historical_adjustment values for comparison. Preserve raw monitor data; "
            "exclude cycles with unknown external attribution rather than invent a subtraction."
        ),
        "cycles": cycles,
    }


def historical_adjustment_markdown(ledger: dict[str, Any]) -> str:
    lines = [
        "# Historical weekly quota values", "",
        f"Export snapshot: {ledger.get('as_of') or 'not recorded'}.",
        "Raw cycle exports and monitor totals remain unchanged; this ledger controls historical comparisons.", "",
        "| Cycle | Raw monitor total | Adjusted at first 100% | Through close | Status | Count in comparison |",
        "| --- | ---: | ---: | ---: | --- | --- |",
    ]
    for cycle in ledger["cycles"]:
        first = cycle.get("adjusted_value_at_first_100_usd")
        close = cycle.get("adjusted_value_through_close_usd")
        lines.append(
            f"| {cycle['cycle']} | ${cycle['raw_monitor_total_usd'] or '—'} | "
            f"${first or '—'} | ${close or '—'} | {cycle['status']} | "
            f"{'yes (conditional)' if cycle.get('include_in_historical_comparison') else 'no'} |"
        )
        if cycle.get("reason"):
            lines.append(f"\n  - {cycle['cycle']}: {cycle['reason']}")
    daily = ledger.get("account_daily_usage_snapshot")
    if isinstance(daily, dict) and daily.get("daily_buckets"):
        lines += [
            "", "## Account-wide daily token buckets", "",
            f"Read at {daily.get('observed_at_utc')} from `{daily.get('source')}`. "
            "These totals are account-wide, not quota-cycle assignments.",
            "| Date | Account tokens | Retained local Headroom tokens | Monitor-period local tokens | Account − monitor difference |",
            "| --- | ---: | ---: | ---: | ---: |",
        ]
        for row in daily["daily_buckets"][-7:]:
            local_raw = row.get("retained_headroom_tokens")
            local_monitor = row.get("monitor_checkpoint_end_date_tokens")
            difference = row.get("account_minus_monitor_checkpoint_tokens")
            lines.append(
                f"| {row['date']} | {row['account_reported_tokens']:,} | "
                f"{local_raw:,} | {local_monitor:,} | {difference:+,} |"
                if local_raw is not None and local_monitor is not None and difference is not None
                else f"| {row['date']} | {row['account_reported_tokens']:,} | "
                     f"{local_raw if local_raw is not None else '—'} | "
                     f"{local_monitor if local_monitor is not None else '—'} | — |"
            )
        lines += ["", "Showing the latest seven reported buckets; all daily rows remain in the JSON export.", "", daily.get("interpretation", "")]
        flags = daily.get("recent_counter_flags") or []
        if flags:
            lines += ["", "Recent account-counter flags (not cycle/model attribution):"]
            for item in flags:
                lines.append(
                    f"- {item.get('observed_at_utc')}: `{item.get('reason')}`, "
                    f"account delta {item.get('account_lifetime_delta')}, "
                    f"local {item.get('local_period_tokens')} tokens, "
                    f"external estimate {item.get('external_tokens_estimate')}; weekly "
                    f"{item.get('weekly_used_percent')}%, status `{item.get('weekly_estimate_status')}`, "
                    f"account $/100 {item.get('weekly_account_total_100_usd') or 'withheld'}."
                )
    live = ledger.get("live_cycle_snapshot")
    if isinstance(live, dict):
        local_projection = live.get("headroom_only_100_usd")
        local_projection_text = (
            f"${local_projection} per 100 points"
            if local_projection is not None
            else "unavailable (local/external percentage points cannot be separated)"
        )
        account_projection = live.get("account_total_100_usd")
        locally_attributed = live.get("locally_attributed_percentage_points")
        lines += [
            "", "## Latest live weekly accounting checkpoint", "",
            f"As of {live.get('as_of_utc')}, usage was {live.get('used_percent')}%; "
            f"priced local cost ${live.get('local_known_cost_usd')}; "
            f"local-only projection {local_projection_text}; "
            f"account-wide projection {account_projection or 'unavailable'}.",
            f"Locally attributable percentage points: {locally_attributed if locally_attributed is not None else 'unavailable'} "
            f"(candidate {live.get('counted_percentage_points')}, no-local points excluded {live.get('excluded_no_token_percentage_points')}).",
            f"External evidence: `{live.get('counter_reason')}`; counter delta "
            f"{live.get('account_lifetime_delta')}, local period tokens {live.get('local_period_tokens')}. "
            "The cycle remains excluded from adjusted historical comparisons until account-wide attribution is reliable.",
        ]
    lines += [
        "", "The Sep 19–23 values are selected conditional working values based on repricing three weakly recorded jumps; "
        "they are not measured invoices. Other weekly spans are excluded from adjusted comparisons because their external-use "
        "attribution cannot be reconstructed. Their raw monitor data remains available for audit.", "",
    ]
    return "\n".join(lines)


def annotate_export(export: dict[str, Any], audit: dict[str, Any], evidence_source: Path) -> None:
    """Attach adjustments while preserving raw rows and monitor totals."""
    cycle = next(c for c in export["cycles"] if c["cycle"] == audit["cycle"])
    cycle["outside_vm_evidence"] = [
        {"timestamp_epoch": int(stamp),
         "timestamp": dt.datetime.fromtimestamp(int(stamp), dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
         **value}
        for stamp, value in sorted(audit["original_evidence_samples"].items())
    ]
    cycle["data_sources"]["outside_vm_evidence_restored_from"] = evidence_source.name
    cycle["percent_bands_semantics"] = (
        "Legacy labels are last observed percentage at request time, NOT "
        "per-point usage or the percentage each call caused. Use observed_intervals."
    )
    cycle["observed_intervals"] = audit["intervals"]
    cycle["reconciliation"] = audit["reconciliation"]
    cycle["synthetic_evidence_summary"] = audit["synthetic_evidence_summary"]
    cycle["local_token_backed_estimate"] = audit["local_token_backed_estimate"]
    cycle["conditional_scenario"] = audit["conditional_scenario"]
    cycle["historical_adjustment"] = audit["selected_historical_adjustment"]
    for other in export.get("cycles", []):
        if other.get("window") == "secondary" and other.get("cycle") != audit["cycle"]:
            other["historical_adjustment"] = _unquantified_adjustment(other)
    ledger = build_historical_adjustment_ledger(export, audit)
    export["historical_adjustments"] = ledger
    export["notes"] = [
        note for note in export.get("notes", [])
        if not note.startswith("Weekly 2026-09-19: observed_intervals supersede percent_bands")
    ]
    export["notes"].append(
        "Weekly historical comparison: selected working value for 2026-09-19 is "
        "$66.65 at first 100% / $66.85 through close, conditional on repricing "
        "three weakly recorded jumps. Other weekly spans without reliable "
        "external-usage attribution are marked unquantified and excluded from "
        "adjusted comparisons; raw rows and monitor totals are preserved."
    )


def write_json_atomically(path: Path, data: dict[str, Any]) -> None:
    fd, tmp = tempfile.mkstemp(prefix="weekly-audit-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(data, out, indent=2, sort_keys=True)
            out.write("\n")
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--export", type=Path, default=Path("codex-cycle-export-2026-09-23.json"))
    parser.add_argument("--original-log", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("weekly-1pct-breakdown.md"))
    parser.add_argument("--json-output", type=Path, default=Path("cycles/secondary_100pt_2026-09-19.audit.json"))
    parser.add_argument("--adjustment-json-output", type=Path, default=Path("cycles/weekly-historical-adjustments.json"))
    parser.add_argument("--adjustment-markdown-output", type=Path, default=Path("cycles/weekly-historical-adjustments.md"))
    parser.add_argument("--annotate-export", action="store_true", help="Add evidence and reconciliation to JSON export without changing raw observations")
    args = parser.parse_args()
    export = json.loads(args.export.read_text())
    cycle = next(c for c in export["cycles"] if c["cycle"] == "secondary_100pt_2026-09-19")
    start = dt.datetime.fromisoformat(cycle["started_at"]).timestamp()
    end = dt.datetime.fromisoformat(cycle["ended_at"]).timestamp()
    audit = analyze_cycle(cycle, original_evidence(args.original_log, start, end))
    args.output.write_text(markdown(audit), encoding="utf-8")
    args.json_output.write_text(json.dumps(audit, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.annotate_export:
        annotate_export(export, audit, args.original_log)
        write_json_atomically(args.export, export)
        for cycle_record in export.get("cycles", []):
            if cycle_record.get("window") != "secondary":
                continue
            cycle_file = args.json_output.parent / (cycle_record["cycle"] + ".json")
            if cycle_file.exists():
                write_json_atomically(cycle_file, cycle_record)
    adjustment_ledger = build_historical_adjustment_ledger(export, audit)
    args.adjustment_json_output.parent.mkdir(parents=True, exist_ok=True)
    args.adjustment_json_output.write_text(
        json.dumps(adjustment_ledger, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    args.adjustment_markdown_output.write_text(
        historical_adjustment_markdown(adjustment_ledger), encoding="utf-8"
    )
    print(
        f"Wrote {args.output}, {args.json_output}, {args.adjustment_json_output}, "
        f"and {args.adjustment_markdown_output}; selected value is conditional"
    )


if __name__ == "__main__":
    main()
