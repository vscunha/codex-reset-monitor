#!/usr/bin/env python3
"""Monitor Codex subscription usage resets through `codex app-server`.

No third-party Python dependencies are required.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import json
import logging
import math
import os
import queue
import re
import shlex
import smtplib
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Collection, Iterable

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows does not provide fcntl.
    fcntl = None  # type: ignore[assignment]

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python installations without zoneinfo.
    ZoneInfo = None  # type: ignore[assignment,misc]

LOG = logging.getLogger("codex-reset-monitor")
_MISSING = object()
_USAGE_COUNTER_FIELDS = (
    "requests",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "raw_cache_write_tokens",
    # Legacy payload alias; never a billing category.
    "cache_write_tokens",
    "cached_input_tokens",
    "uncached_input_tokens",
    # Kept in persisted payloads for readers of schema v2.  They are aliases,
    # not separately billed categories.
    "ordinary_input_tokens",
    "priced_cache_write_tokens",
)
_ACCOUNTING_SCHEMA_VERSION = 3
_CROSSING_ROLLING_POINTS = (1, 3, 5, 10)
_SPIKE_SHARE_THRESHOLD = Decimal("0.25")
_SPIKE_MIN_COST = Decimal("0.10")
# Bucket for usage the Codex account consumed outside this VM.  The
# app-server rate-limit call reports account-wide percentages only, so a
# percentage increase with no local Headroom rows means some other client
# spent quota.  Imputed cost/tokens below are attributed here, never to a
# real model, and always flagged as approximate.
_IMPUTED_MODEL_NAME = "external-imputed"
_EXTERNAL_IMPUTATION_MIN_POINTS_DEFAULT = 2
# Models that share the Headroom proxy but never consume Codex quota (other
# providers reached through pi/OpenCode).  Counting them would price them as
# ``unpriced_model``, mark every period incomplete, and inflate the local token
# sum the outside-VM evidence compares against the Codex account counter.
# This is an exclusion list, never an allowlist: a model that is merely
# *unknown* (a newly released Codex model) must keep flagging ``unpriced_model``
# so a missing rate stays visible instead of silently dropping spend.
_NON_CODEX_MODELS_DEFAULT = (
    "deepseek-v4.1-flash",
    "deepseek-v4-flash",
    "kimi-k2.6",
    "muse-spark-1.3-contributor",
    "gemini-3.8-flash-high",
)


def _usage_counters(value: Any = None) -> dict[str, int]:
    source = value if isinstance(value, dict) else {}
    counters: dict[str, int] = {}

    def integer(field: str, fallback: int = 0) -> int:
        raw = source.get(field, fallback)
        try:
            number = int(raw)
        except (TypeError, ValueError, OverflowError):
            number = 0
        return max(0, number)

    counters["requests"] = integer("requests")
    counters["input_tokens"] = integer("input_tokens")
    counters["output_tokens"] = integer("output_tokens")
    counters["cache_read_tokens"] = integer(
        "cache_read_tokens", integer("cached_input_tokens")
    )
    raw_cache_write_tokens = integer(
        "raw_cache_write_tokens", integer("cache_write_tokens")
    )
    counters["raw_cache_write_tokens"] = raw_cache_write_tokens
    counters["cache_write_tokens"] = raw_cache_write_tokens
    counters["cached_input_tokens"] = integer(
        "cached_input_tokens", counters["cache_read_tokens"]
    )
    # Headroom's cache_write counter is not a separately billed Codex category.
    # For old state/log payloads, derive the canonical uncached count from the
    # total and cached input counters instead of trusting legacy aliases.
    counters["uncached_input_tokens"] = max(
        0,
        integer(
            "uncached_input_tokens",
            counters["input_tokens"] - counters["cached_input_tokens"],
        ),
    )
    counters["ordinary_input_tokens"] = counters["uncached_input_tokens"]
    counters["priced_cache_write_tokens"] = 0
    return counters


def _add_usage_counters(target: dict[str, int], value: Any = None) -> None:
    source = _usage_counters(value)
    for field in _USAGE_COUNTER_FIELDS:
        target[field] = max(0, int(target.get(field, 0))) + source[field]


def _cumulative_usage_payload(
    usage_total: dict[str, int] | None,
    usage_by_model: dict[str, dict[str, int]] | None,
    costs_by_model: dict[str, Any] | None,
    known_total: Decimal | None,
) -> dict[str, Any]:
    payload = _usage_counters(usage_total)
    payload["pricing_basis"] = "uncached_input_plus_cached_input_plus_output"
    payload["cache_write_billing"] = "ignored"
    payload["cost_basis"] = "current_price_equivalent"
    payload["known_cost_usd"] = str(known_total if known_total is not None else Decimal("0"))
    payload["models"] = {}
    model_rows = usage_by_model if isinstance(usage_by_model, dict) else {}
    model_costs = costs_by_model if isinstance(costs_by_model, dict) else {}
    for model_name in sorted(set(model_rows) | set(model_costs)):
        row = _usage_counters(model_rows.get(model_name))
        row["known_cost_usd"] = str(_decimal_value(model_costs.get(model_name)))
        payload["models"][str(model_name)] = row
    return payload


class MonitorError(RuntimeError):
    """Expected runtime/configuration error."""


@dataclass(frozen=True)
class LimitWindow:
    used_percent: float | None = None
    resets_at: int | None = None
    window_duration_mins: int | None = None


@dataclass(frozen=True)
class Snapshot:
    captured_at: int
    primary: LimitWindow | None
    secondary: LimitWindow | None
    credit_balance: str | None
    reset_credit_available_count: int | None
    reset_credit_ids: tuple[str, ...]


@dataclass(frozen=True)
class Event:
    kind: str
    title: str
    body: str


@dataclass(frozen=True)
class ModelPricing:
    """Rates for one model, expressed in currency units per pricing unit."""

    model_id: str
    input_rate: Decimal
    output_rate: Decimal
    cached_input_rate: Decimal
    cache_write_rate: Decimal
    cache_write_billed_as: str = "write"
    long_context_threshold_tokens: int | None = None
    long_context_input_multiplier: Decimal = Decimal("1")
    long_context_output_multiplier: Decimal = Decimal("1")
    processing_multipliers: tuple[tuple[str, Decimal], ...] = ()

    @property
    def cached_in_rate(self) -> Decimal:
        return self.cached_input_rate

    def processing_multiplier(self, tier: str) -> Decimal:
        values = dict(self.processing_multipliers)
        return values.get(tier.lower(), Decimal("1"))


@dataclass(frozen=True)
class PricingTable:
    """Validated, immutable pricing settings loaded for one monitor sample."""

    revision: str
    currency: str
    unit: str
    models: dict[str, ModelPricing]
    aliases: dict[str, str]
    pricing_mode: str = "codex"
    processing_tier: str = "standard"
    source_path: str | None = None

    def resolve(self, model: str) -> ModelPricing | None:
        key = model.strip().lower()
        canonical = self.aliases.get(key, key)
        return self.models.get(canonical)

    def canonical_name(self, model: str) -> str:
        key = model.strip().lower()
        return self.aliases.get(key, key)


@dataclass(frozen=True)
class HeadroomUsageRecord:
    """Normalized completed request/turn emitted by Headroom's PERF logger."""

    timestamp: float
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    raw_cache_write_tokens: int
    request_id: str
    client: str | None = None
    source_file: str = ""
    source_offset: int = 0
    local_cache_hit: bool = False
    cache_write_inferred: bool = False
    quality_flags: tuple[str, ...] = ()
    tok_before: int | None = None
    tok_saved: int | None = None
    processing_tier: str | None = None

    @property
    def cache_write_tokens(self) -> int:
        """Legacy alias for the raw Headroom diagnostic counter."""

        return self.raw_cache_write_tokens


@dataclass(frozen=True)
class CostBreakdown:
    """Codex-equivalent cost and token categories for one request."""

    uncached_input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    uncached_input_cost: Decimal | None
    cached_input_cost: Decimal | None
    output_cost: Decimal | None
    total_cost: Decimal | None
    complete: bool
    reason: str | None = None
    inferred_cache_write: bool = False
    local_cache_hit: bool = False
    raw_cache_write_tokens: int = 0
    processing_tier: str = "standard"
    long_context: bool = False

    @property
    def ordinary_input_tokens(self) -> int:
        """Schema-v2 compatibility alias for uncached input."""

        return self.uncached_input_tokens

    @property
    def ordinary_input_cost(self) -> Decimal | None:
        """Schema-v2 compatibility alias for uncached input cost."""

        return self.uncached_input_cost

    @property
    def cache_write_tokens(self) -> int:
        """Raw Headroom diagnostic; never used as a billable category."""

        return self.raw_cache_write_tokens

    @property
    def cache_write_cost(self) -> Decimal:
        """Cache writes are not separately billed by this Codex calculation."""

        return Decimal("0")


@dataclass(frozen=True)
class PeriodUsage:
    """Aggregated usage and pricing diagnostics for one successful sample span."""

    period_start: float
    period_end: float
    records: tuple[HeadroomUsageRecord, ...]
    by_model: dict[str, dict[str, Any]]
    total: dict[str, Any]
    diagnostics: dict[str, Any]


class CodexAppServerClient:
    """Small JSONL client for the official local `codex app-server`."""

    def __init__(self, command: str, timeout_seconds: float = 25.0) -> None:
        self.command = shlex.split(command)
        if not self.command:
            raise MonitorError("CODEX_COMMAND is empty")
        self.timeout_seconds = timeout_seconds
        self.process: subprocess.Popen[str] | None = None
        self.stdout_queue: queue.Queue[str | None] = queue.Queue()
        self.stderr_tail: deque[str] = deque(maxlen=50)
        self._stdout_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._next_id = 1

    def __enter__(self) -> "CodexAppServerClient":
        try:
            self.process = subprocess.Popen(
                self.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                bufsize=1,
            )
        except FileNotFoundError as exc:
            raise MonitorError(
                f"Could not start {self.command[0]!r}. Install Codex CLI or set CODEX_COMMAND."
            ) from exc
        except OSError as exc:
            raise MonitorError(f"Could not start Codex app-server: {exc}") from exc

        assert self.process.stdout is not None
        assert self.process.stderr is not None

        def read_stdout() -> None:
            assert self.process is not None and self.process.stdout is not None
            for line in self.process.stdout:
                self.stdout_queue.put(line)
            self.stdout_queue.put(None)

        def read_stderr() -> None:
            assert self.process is not None and self.process.stderr is not None
            for line in self.process.stderr:
                self.stderr_tail.append(line.rstrip())

        self._stdout_thread = threading.Thread(target=read_stdout, daemon=True)
        self._stderr_thread = threading.Thread(target=read_stderr, daemon=True)
        self._stdout_thread.start()
        self._stderr_thread.start()

        self.call(
            "initialize",
            {
                "clientInfo": {
                    "name": "codex_reset_monitor",
                    "title": "Codex Reset Monitor",
                    "version": "1.0.0",
                }
            },
        )
        self.notify("initialized")
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self.process is None:
            return
        try:
            if self.process.stdin is not None:
                self.process.stdin.close()
        except OSError:
            pass
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)

    def _write(self, payload: dict[str, Any]) -> None:
        assert self.process is not None and self.process.stdin is not None
        if self.process.poll() is not None:
            raise MonitorError(self._exited_message())
        try:
            self.process.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
            self.process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise MonitorError(self._exited_message()) from exc

    def _exited_message(self) -> str:
        details = "\n".join(self.stderr_tail)
        suffix = f"\nCodex stderr:\n{details}" if details else ""
        return f"Codex app-server exited unexpectedly.{suffix}"

    def notify(self, method: str, params: Any = _MISSING) -> None:
        message: dict[str, Any] = {"method": method}
        if params is not _MISSING:
            message["params"] = params
        self._write(message)

    def call(self, method: str, params: Any = _MISSING) -> Any:
        request_id = self._next_id
        self._next_id += 1
        message: dict[str, Any] = {"method": method, "id": request_id}
        if params is not _MISSING:
            message["params"] = params
        self._write(message)

        deadline = time.monotonic() + self.timeout_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MonitorError(f"Timed out waiting for Codex response to {method!r}")
            try:
                line = self.stdout_queue.get(timeout=remaining)
            except queue.Empty as exc:
                raise MonitorError(f"Timed out waiting for Codex response to {method!r}") from exc
            if line is None:
                raise MonitorError(self._exited_message())
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                LOG.debug("Ignoring non-JSON app-server output: %s", line.rstrip())
                continue
            if payload.get("id") != request_id:
                LOG.debug("Ignoring unrelated app-server message: %s", payload)
                continue
            if "error" in payload:
                error = payload["error"]
                message_text = error.get("message", str(error)) if isinstance(error, dict) else str(error)
                if "authentication required" in message_text.lower():
                    raise MonitorError(
                        f"{message_text}. Open Codex once and sign in with your ChatGPT account."
                    )
                raise MonitorError(f"Codex app-server error: {message_text}")
            if "result" not in payload:
                raise MonitorError(f"Malformed Codex response: {payload}")
            return payload["result"]

    def read_rate_limits(self) -> dict[str, Any]:
        result = self.call("account/rateLimits/read")
        if not isinstance(result, dict):
            raise MonitorError("Codex returned an unexpected rate-limit payload")
        return result

    def read_account_usage(self) -> dict[str, Any]:
        result = self.call("account/usage/read", {})
        if not isinstance(result, dict):
            raise MonitorError("Codex returned an unexpected account-usage payload")
        return result


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise MonitorError(f"Invalid line {number} in {path}: expected KEY=VALUE")
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def default_pricing_path() -> Path:
    config_home = Path(os.getenv("XDG_CONFIG_HOME", Path.home() / ".config"))
    return config_home / "codex-reset-monitor" / "model-pricing.json"


def _pricing_decimal(value: Any, field: str, *, allow_zero: bool = True) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise MonitorError(f"Pricing field {field!r} is missing")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise MonitorError(f"Pricing field {field!r} is not a decimal") from exc
    if not result.is_finite() or (not allow_zero and result <= 0) or (allow_zero and result < 0):
        comparison = "positive" if not allow_zero else "non-negative"
        raise MonitorError(f"Pricing field {field!r} must be a finite {comparison} decimal")
    return result


def _pricing_int(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise MonitorError(f"Pricing field {field!r} must be an integer")
    try:
        decimal_value = Decimal(str(value))
        result = int(decimal_value)
    except (InvalidOperation, OverflowError, TypeError, ValueError) as exc:
        raise MonitorError(f"Pricing field {field!r} must be an integer") from exc
    if not decimal_value.is_finite() or decimal_value != Decimal(result):
        raise MonitorError(f"Pricing field {field!r} must be an integer")
    if result <= 0:
        raise MonitorError(f"Pricing field {field!r} must be greater than zero")
    return result


def load_model_pricing(path: Path | str | None = None) -> PricingTable:
    """Read and validate editable model pricing settings.

    Rates are intentionally loaded for every check so an operator can change the
    file without restarting the monitor.  Decimal strings avoid binary rounding
    in the accounting record.
    """

    pricing_path = Path(path or os.getenv("MODEL_PRICING_FILE", "").strip() or default_pricing_path()).expanduser()
    try:
        raw = json.loads(pricing_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise MonitorError(
            f"Pricing file {pricing_path} does not exist; copy model-pricing.json.example to it"
        ) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise MonitorError(f"Could not read pricing file {pricing_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise MonitorError(f"Pricing file {pricing_path} must contain a JSON object")

    models_raw = raw.get("models")
    if not isinstance(models_raw, dict) or not models_raw:
        raise MonitorError(f"Pricing file {pricing_path} must contain a non-empty models object")

    parsed_models: dict[str, ModelPricing] = {}
    for model_name, row in models_raw.items():
        if not isinstance(model_name, str) or not model_name.strip():
            raise MonitorError("Every pricing model key must be a non-empty string")
        if not isinstance(row, dict):
            raise MonitorError(f"Pricing model {model_name!r} must be an object")
        model_key = model_name.strip().lower()
        input_rate = _pricing_decimal(row.get("input", row.get("input_rate")), f"{model_name}.input")
        output_rate = _pricing_decimal(row.get("output", row.get("output_rate")), f"{model_name}.output")
        cached_rate = row.get("cached_input", row.get("cache_read", row.get("cached_in")))
        cached_rate = _pricing_decimal(cached_rate, f"{model_name}.cached_input")
        # Accepted for old configuration files and diagnostics only.  The
        # Codex-equivalent calculator never bills this legacy cache-write rate.
        write_rate = row.get(
            "cache_write", row.get("cached_output", row.get("cached_out", "0"))
        )
        write_rate = _pricing_decimal(write_rate, f"{model_name}.legacy_cache_write")
        billed_as = str(row.get("cache_write_billed_as", "write")).strip().lower()
        if billed_as not in {"write", "input"}:
            raise MonitorError(
                f"{model_name}.cache_write_billed_as must be 'write' or 'input'"
            )

        threshold = row.get("long_context_threshold_tokens")
        threshold_int = None if threshold is None or threshold == "" else _pricing_int(
            threshold, f"{model_name}.long_context_threshold_tokens"
        )
        input_multiplier = _pricing_decimal(
            row.get("long_context_input_multiplier", "1"),
            f"{model_name}.long_context_input_multiplier",
            allow_zero=False,
        )
        output_multiplier = _pricing_decimal(
            row.get("long_context_output_multiplier", "1"),
            f"{model_name}.long_context_output_multiplier",
            allow_zero=False,
        )
        tier_rows = row.get("processing_multipliers", {"standard": "1"})
        if not isinstance(tier_rows, dict):
            raise MonitorError(f"{model_name}.processing_multipliers must be an object")
        tiers: list[tuple[str, Decimal]] = []
        for tier_name, tier_rate in tier_rows.items():
            if not isinstance(tier_name, str) or not tier_name.strip():
                raise MonitorError(f"{model_name} contains an invalid processing tier")
            tiers.append(
                (
                    tier_name.strip().lower(),
                    _pricing_decimal(tier_rate, f"{model_name}.processing_multipliers.{tier_name}", allow_zero=False),
                )
            )
        if not any(name == "standard" for name, _ in tiers):
            tiers.append(("standard", Decimal("1")))
        parsed_models[model_key] = ModelPricing(
            model_id=model_key,
            input_rate=input_rate,
            output_rate=output_rate,
            cached_input_rate=cached_rate,
            cache_write_rate=write_rate,
            cache_write_billed_as=billed_as,
            long_context_threshold_tokens=threshold_int,
            long_context_input_multiplier=input_multiplier,
            long_context_output_multiplier=output_multiplier,
            processing_multipliers=tuple(tiers),
        )

    aliases_raw = raw.get("aliases", {})
    if not isinstance(aliases_raw, dict):
        raise MonitorError(f"Pricing file {pricing_path} aliases must be an object")
    aliases: dict[str, str] = {}
    for alias, target in aliases_raw.items():
        if not isinstance(alias, str) or not isinstance(target, str):
            raise MonitorError("Pricing aliases must map strings to strings")
        alias_key = alias.strip().lower()
        target_key = target.strip().lower()
        if not alias_key or target_key not in parsed_models:
            raise MonitorError(f"Pricing alias {alias!r} points to unknown model {target!r}")
        aliases[alias_key] = target_key

    revision = str(raw.get("revision", raw.get("verified_on", "unversioned"))).strip()
    if not revision:
        raise MonitorError("Pricing file revision must be a non-empty string")
    currency = str(raw.get("currency", "USD")).strip().upper()
    unit = str(raw.get("unit", "per_1m_tokens")).strip()
    if not currency or not unit:
        raise MonitorError("Pricing currency and unit must be non-empty")
    # Validate the unit while the pricing file is being loaded.  This keeps an
    # invalid settings file on the same unavailable-cost path as malformed
    # rates, so reset detection can still commit its snapshot.
    _pricing_denominator(unit)
    processing_tier = str(
        raw.get("processing_tier", os.getenv("PRICING_PROCESSING_TIER", "standard"))
    ).strip().lower() or "standard"
    pricing_mode = str(
        raw.get("pricing_mode", os.getenv("PRICING_MODE", "codex"))
    ).strip().lower() or "codex"
    if pricing_mode != "codex":
        raise MonitorError(
            f"Unsupported pricing mode {pricing_mode!r}; this monitor currently supports 'codex'"
        )
    return PricingTable(
        revision=revision,
        currency=currency,
        unit=unit,
        models=parsed_models,
        aliases=aliases,
        pricing_mode=pricing_mode,
        processing_tier=processing_tier,
        source_path=str(pricing_path),
    )


def unavailable_pricing(reason: str, path: Path | None = None) -> PricingTable:
    """Return an empty table used to keep reset detection running on pricing errors."""

    return PricingTable(
        revision=f"unavailable:{reason}",
        currency="USD",
        unit="per_1m_tokens",
        models={},
        aliases={},
        pricing_mode="codex",
        source_path=str(path) if path else None,
    )


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _integer(value: Any) -> int | None:
    number = _number(value)
    return int(number) if number is not None else None


def _decimal_string(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return str(result) if result.is_finite() else None


def _window(value: Any) -> LimitWindow | None:
    if not isinstance(value, dict):
        return None
    return LimitWindow(
        used_percent=_number(value.get("usedPercent")),
        resets_at=_integer(value.get("resetsAt")),
        window_duration_mins=_integer(value.get("windowDurationMins")),
    )


def snapshot_from_payload(
    payload: dict[str, Any], captured_at: int | None = None, limit_id: str | None = None
) -> Snapshot:
    rate_limits: Any = None
    by_id = payload.get("rateLimitsByLimitId")
    if isinstance(by_id, dict):
        requested_limit = limit_id or os.getenv("CODEX_LIMIT_ID", "codex")
        rate_limits = by_id.get(requested_limit)
        if not isinstance(rate_limits, dict) and by_id:
            rate_limits = next((v for v in by_id.values() if isinstance(v, dict)), None)
    if not isinstance(rate_limits, dict):
        rate_limits = payload.get("rateLimits")
    if not isinstance(rate_limits, dict):
        raise MonitorError("Rate-limit response did not contain rateLimits")

    credits = rate_limits.get("credits")
    credit_balance: str | None = None
    if isinstance(credits, dict):
        credit_balance = _decimal_string(credits.get("balance"))

    reset_summary = payload.get("rateLimitResetCredits")
    available_count: int | None = None
    reset_ids: list[str] = []
    if isinstance(reset_summary, dict):
        available_count = _integer(reset_summary.get("availableCount"))
        rows = reset_summary.get("credits")
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, dict) and row.get("status") in {None, "available"}:
                    identifier = row.get("id")
                    if isinstance(identifier, str):
                        reset_ids.append(identifier)

    return Snapshot(
        captured_at=captured_at if captured_at is not None else int(time.time()),
        primary=_window(rate_limits.get("primary")),
        secondary=_window(rate_limits.get("secondary")),
        credit_balance=credit_balance,
        reset_credit_available_count=available_count,
        reset_credit_ids=tuple(sorted(set(reset_ids))),
    )


def snapshot_to_dict(snapshot: Snapshot) -> dict[str, Any]:
    return asdict(snapshot)


def snapshot_from_dict(data: dict[str, Any]) -> Snapshot:
    def load_window(value: Any) -> LimitWindow | None:
        if not isinstance(value, dict):
            return None
        return LimitWindow(
            used_percent=_number(value.get("used_percent")),
            resets_at=_integer(value.get("resets_at")),
            window_duration_mins=_integer(value.get("window_duration_mins")),
        )

    ids = data.get("reset_credit_ids")
    return Snapshot(
        captured_at=int(data.get("captured_at", 0)),
        primary=load_window(data.get("primary")),
        secondary=load_window(data.get("secondary")),
        credit_balance=_decimal_string(data.get("credit_balance")),
        reset_credit_available_count=_integer(data.get("reset_credit_available_count")),
        reset_credit_ids=tuple(str(v) for v in ids) if isinstance(ids, list) else (),
    )


_PERF_MARKER = re.compile(r"\bPERF\b")
_PERF_TIMESTAMP = re.compile(
    r"(?P<timestamp>\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}(?:,\d{1,6})?)"
)
_PERF_REQUEST_ID = re.compile(r"\[(?P<id>[^\]]+)\]\s+PERF\b")
_PERF_FIELD = re.compile(r"(?P<key>[A-Za-z][A-Za-z0-9_]*)=(?P<value>[^\s]+)")


def _parse_perf_integer(fields: dict[str, str], names: tuple[str, ...], label: str) -> int:
    value: str | None = next((fields[name] for name in names if name in fields), None)
    if value is None:
        raise MonitorError(f"PERF record is missing {label}")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise MonitorError(f"PERF {label} is not an integer: {value!r}") from exc
    if result < 0:
        raise MonitorError(f"PERF {label} cannot be negative")
    return result


def _parse_optional_perf_integer(fields: dict[str, str], names: tuple[str, ...]) -> int | None:
    value = next((fields[name] for name in names if name in fields), None)
    if value is None:
        return None
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result >= 0 else None


def _parse_perf_bool(value: str | None) -> bool:
    return value is not None and value.strip().lower() in {"1", "true", "yes", "on"}


def _parse_perf_timestamp(value: str, timezone_name: str | None = None) -> float:
    try:
        if "," in value:
            parsed = dt.datetime.strptime(value, "%Y-%m-%d %H:%M:%S,%f")
        else:
            parsed = dt.datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except ValueError as exc:
        raise MonitorError(f"PERF timestamp is invalid: {value!r}") from exc
    if timezone_name:
        if ZoneInfo is None:
            raise MonitorError("Timezone support is unavailable in this Python build")
        try:
            parsed = parsed.replace(tzinfo=ZoneInfo(timezone_name))
        except Exception as exc:
            raise MonitorError(f"Unknown Headroom log timezone {timezone_name!r}") from exc
    else:
        parsed = parsed.astimezone()
    return parsed.astimezone(dt.timezone.utc).timestamp()


def parse_headroom_perf_line(
    line: str,
    *,
    source_file: str = "",
    source_offset: int = 0,
    timezone_name: str | None = None,
) -> HeadroomUsageRecord | None:
    """Parse one Headroom PERF line without importing the Headroom package."""

    if not _PERF_MARKER.search(line):
        return None
    timestamp_match = _PERF_TIMESTAMP.search(line)
    if timestamp_match is None:
        raise MonitorError("PERF record is missing its timestamp")
    fields = {match.group("key"): match.group("value") for match in _PERF_FIELD.finditer(line)}
    model = fields.get("model", "").strip()
    if not model:
        raise MonitorError("PERF record is missing model")
    input_tokens = _parse_perf_integer(fields, ("tok_after", "input_tokens", "tok_in"), "input tokens")
    output_tokens = _parse_perf_integer(fields, ("tok_out", "output_tokens"), "output tokens")
    cache_read_tokens = _parse_perf_integer(
        fields, ("cache_read", "cached_input", "cache_read_tokens"), "cache_read"
    )
    cache_write_tokens = _parse_perf_integer(
        fields, ("cache_write", "cached_output", "cache_write_tokens"), "cache_write"
    )
    request_match = _PERF_REQUEST_ID.search(line)
    request_id = request_match.group("id") if request_match else ""
    if not request_id:
        request_id = hashlib.sha256(f"{source_file}:{source_offset}:{line}".encode()).hexdigest()[:20]
    client = fields.get("client")
    quality: list[str] = []
    if "tok_after" in fields:
        quality.append("input_from_tok_after")
    if "tok_out" in fields:
        quality.append("output_from_tok_out")
    if client is None:
        quality.append("missing_client")

    # The PERF logger always includes cache_write, but current WebSocket metrics
    # derive it from input minus cache reads.  Only explicit source markers make
    # that counter authoritative.
    explicit_write = (
        _parse_perf_bool(fields.get("cache_write_explicit"))
        or fields.get("cache_write_source", "").strip().lower() in {"upstream", "reported", "explicit"}
        or fields.get("cache_inferred", "").strip().lower() in {"0", "false", "no"}
    )
    inferred = not explicit_write
    if inferred:
        quality.append("cache_write_inferred")
    local_hit = _parse_perf_bool(fields.get("cached")) or _parse_perf_bool(fields.get("local_cache_hit"))
    if local_hit:
        quality.append("local_cache_hit")
    processing_tier = (
        fields.get("processing_tier")
        or fields.get("service_tier")
        or fields.get("tier")
        or ""
    ).strip().lower() or None
    return HeadroomUsageRecord(
        timestamp=_parse_perf_timestamp(timestamp_match.group("timestamp"), timezone_name),
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        raw_cache_write_tokens=cache_write_tokens,
        request_id=request_id,
        client=client,
        source_file=source_file,
        source_offset=source_offset,
        local_cache_hit=local_hit,
        cache_write_inferred=inferred,
        quality_flags=tuple(quality),
        tok_before=_parse_optional_perf_integer(fields, ("tok_before",)),
        tok_saved=_parse_optional_perf_integer(fields, ("tok_saved",)),
        processing_tier=processing_tier,
    )


def _file_digest(path: Path, *, from_end: bool = False, size: int = 4096) -> str:
    try:
        with path.open("rb") as handle:
            if from_end:
                handle.seek(0, os.SEEK_END)
                length = handle.tell()
                handle.seek(max(0, length - size))
            data = handle.read(size)
    except OSError:
        return ""
    return hashlib.sha256(data).hexdigest()


def _empty_cursor() -> dict[str, Any]:
    return {"version": 1, "files": {}, "coverage_gap": False, "last_scan_at": None}


def _cursor_file_key(stat_result: os.stat_result) -> str:
    return f"{stat_result.st_dev}:{stat_result.st_ino}"


# Headroom proxy builds have used both ``proxy.log`` and port-suffixed names
# such as ``proxy-8787.log`` for their active file; numbered rotations append
# another ``.N`` suffix to either base name.
_HEADROOM_LOG_PATTERNS = ("proxy.log*", "proxy-*.log*")
_PROXY_LOG_BASE_NAME = re.compile(r"^proxy(?:-\d+)?\.log$")


def headroom_log_paths(log_dir: Path | str) -> list[Path]:
    """Discover proxy log files, including port-suffixed names and rotations."""

    directory = Path(log_dir).expanduser()
    found: set[Path] = set()
    for pattern in _HEADROOM_LOG_PATTERNS:
        found.update(directory.glob(pattern))
    return sorted(found)


def prime_headroom_cursor(
    log_dir: Path | str, cursor: dict[str, Any] | None = None
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Set a first-run cursor at EOF without attributing retained history to a period."""

    log_dir = Path(log_dir).expanduser()
    current = json.loads(json.dumps(cursor)) if isinstance(cursor, dict) else _empty_cursor()
    if not isinstance(current.get("files"), dict):
        current["files"] = {}
    diagnostics: dict[str, Any] = {
        "coverage_gap": False,
        "files_seen": 0,
        "records_read": 0,
        "accepted": 0,
        "excluded": 0,
        "invalid": 0,
        "baseline": True,
    }
    try:
        paths = headroom_log_paths(log_dir)
    except OSError as exc:
        current["coverage_gap"] = True
        diagnostics.update({"coverage_gap": True, "error": str(exc)})
        return current, diagnostics
    if not paths:
        current["coverage_gap"] = True
        diagnostics.update({"coverage_gap": True, "error": f"No Headroom logs found in {log_dir}"})
        current["last_scan_at"] = time.time()
        return current, diagnostics
    for path in paths:
        try:
            stat_result = path.stat()
        except OSError:
            diagnostics["coverage_gap"] = True
            continue
        if not path.is_file():
            continue
        key = _cursor_file_key(stat_result)
        try:
            with path.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                end_offset = handle.tell()
        except OSError as exc:
            current["coverage_gap"] = True
            diagnostics["coverage_gap"] = True
            diagnostics.setdefault("errors", []).append(f"{path}: {exc}")
            continue
        current["files"][key] = {
            "path": str(path),
            "offset": end_offset,
            "size": end_offset,
            "head_hash": _file_digest(path),
            "tail_hash": _file_digest(path, from_end=True),
        }
        diagnostics["files_seen"] += 1
    current["last_scan_at"] = time.time()
    return current, diagnostics


def headroom_excluded_models() -> frozenset[str]:
    """Model names excluded from Codex accounting, read from the environment.

    ``HEADROOM_EXCLUDED_MODELS`` holds a comma-separated list.  Unset uses the
    built-in third-party defaults; setting it to an empty string disables
    exclusion entirely, so every parsed row is accounted for again.
    """

    raw = os.getenv("HEADROOM_EXCLUDED_MODELS")
    if raw is None:
        candidates: tuple[str, ...] = _NON_CODEX_MODELS_DEFAULT
    else:
        candidates = tuple(raw.split(","))
    return frozenset(
        name.strip().lower() for name in candidates if name and name.strip()
    )


def collect_headroom_usage(
    log_dir: Path | str,
    start_at: float | None,
    end_at: float,
    cursor: dict[str, Any] | None = None,
    *,
    client: str | None = None,
    timezone_name: str | None = None,
    excluded_models: Collection[str] | None = None,
) -> tuple[list[HeadroomUsageRecord], dict[str, Any], dict[str, Any]]:
    """Read appended PERF records and update an inode/offset cursor.

    A copied rotation is recognized by its content fingerprint and resumes from
    the old offset.  Rows are never deduplicated by the bracketed connection ID;
    each completed line is a distinct usage observation.
    """

    log_dir = Path(log_dir).expanduser()
    excluded = (
        headroom_excluded_models()
        if excluded_models is None
        else frozenset(str(name).strip().lower() for name in excluded_models if str(name).strip())
    )
    current = json.loads(json.dumps(cursor)) if isinstance(cursor, dict) else _empty_cursor()
    current.setdefault("version", 1)
    if not isinstance(current.get("files"), dict):
        current["files"] = {}
    records: list[HeadroomUsageRecord] = []
    diagnostics: dict[str, Any] = {
        # A previous scan may have seen no files.  Once logs are available again
        # that old condition must not poison every later period; current-scan
        # gaps are computed below.
        "coverage_gap": False,
        "previous_coverage_gap": bool(current.get("coverage_gap", False)),
        "files_seen": 0,
        "records_read": 0,
        "accepted": 0,
        "excluded": 0,
        "invalid": 0,
        "late": 0,
        "missing_client": 0,
        "excluded_models": 0,
        "excluded_model_names": sorted(excluded),
        "source_assumption": (
            "Headroom records are filtered by client and by the non-Codex model "
            "exclusion list; account identity is unavailable in PERF lines"
        ),
    }
    try:
        paths = headroom_log_paths(log_dir)
    except OSError as exc:
        diagnostics.update({"coverage_gap": True, "error": str(exc)})
        current["coverage_gap"] = True
        return records, current, diagnostics
    if not paths:
        diagnostics.update({"coverage_gap": True, "error": f"No Headroom logs found in {log_dir}"})
        current["coverage_gap"] = True
        return records, current, diagnostics

    # Keep the previous cursor immutable while matching rotations.  Updating
    # ``current["files"]`` as each path is scanned must not change the
    # fingerprints used to recognize a copied file later in this same pass.
    old_files = current.get("files", {})
    old_files_snapshot = (
        json.loads(json.dumps(old_files)) if isinstance(old_files, dict) else {}
    )
    seen_keys: set[str] = set()
    for path in paths:
        try:
            stat_result = path.stat()
        except OSError as exc:
            diagnostics["coverage_gap"] = True
            diagnostics.setdefault("errors", []).append(f"{path}: {exc}")
            continue
        if not path.is_file():
            continue
        diagnostics["files_seen"] += 1
        key = _cursor_file_key(stat_result)
        seen_keys.add(key)
        head_hash = _file_digest(path)
        old = old_files_snapshot.get(key)
        copied_from: dict[str, Any] | None = None
        copied_from_key: str | None = None
        if not isinstance(old, dict):
            # Some rotation tools copy/truncate rather than rename.  Matching a
            # small prefix lets us continue at the prior byte offset without
            # replaying the copied rows.  The filename fallback covers a short
            # file whose hash changes when a new line is appended before the
            # copy is made.
            for candidate_key, candidate in old_files_snapshot.items():
                if not isinstance(candidate, dict):
                    continue
                candidate_path = Path(str(candidate.get("path", "")))
                same_prefix = candidate.get("head_hash") == head_hash
                conventional_rotation = (
                    candidate_path.parent == path.parent
                    and _PROXY_LOG_BASE_NAME.match(candidate_path.name) is not None
                    and path.name.startswith(candidate_path.name + ".")
                )
                if same_prefix or conventional_rotation:
                    copied_from = candidate
                    copied_from_key = candidate_key
                    break
            old = copied_from
        start_offset = int(old.get("offset", 0)) if isinstance(old, dict) else 0
        if stat_result.st_size < start_offset:
            diagnostics["coverage_gap"] = True
            start_offset = 0
        try:
            with path.open("rb") as handle:
                handle.seek(start_offset)
                chunk = handle.read()
        except OSError as exc:
            diagnostics["coverage_gap"] = True
            diagnostics.setdefault("errors", []).append(f"{path}: {exc}")
            continue
        newline = chunk.rfind(b"\n")
        complete_chunk = chunk[: newline + 1] if newline >= 0 else b""
        offset = start_offset
        copied_rows_already_read = False
        if copied_from is not None and complete_chunk and copied_from_key is not None:
            # A copy may retain the same newly appended suffix as the active
            # file.  Compare bytes at the old cursor boundary (or at zero when
            # the active copy/truncated source wrapped) and skip only an exact
            # copied suffix.  Rows repeated legitimately within one source file
            # remain untouched because this check is limited to copied files.
            source_path = Path(str(copied_from.get("path", "")))
            try:
                source_stat = source_path.stat()
                source_is_same_generation = _cursor_file_key(source_stat) == copied_from_key
                source_offset = int(copied_from.get("offset", 0))
                if source_stat.st_size < source_offset:
                    source_offset = 0
                if source_is_same_generation and source_path != path:
                    with source_path.open("rb") as source_handle:
                        source_handle.seek(source_offset)
                        source_chunk = source_handle.read()
                    source_newline = source_chunk.rfind(b"\n")
                    source_complete = source_chunk[: source_newline + 1] if source_newline >= 0 else b""
                    copied_rows_already_read = source_complete.startswith(complete_chunk)
            except OSError:
                copied_rows_already_read = False
        if copied_rows_already_read:
            diagnostics["duplicate_rows_skipped"] = int(
                diagnostics.get("duplicate_rows_skipped", 0)
            ) + complete_chunk.count(b"\n")
            offset += len(complete_chunk)
        lines_to_parse = () if copied_rows_already_read else complete_chunk.splitlines(keepends=True)
        for raw_line in lines_to_parse:
            line_start = offset
            offset += len(raw_line)
            try:
                line = raw_line.decode("utf-8")
                record = parse_headroom_perf_line(
                    line,
                    source_file=str(path),
                    source_offset=line_start,
                    timezone_name=timezone_name,
                )
            except (UnicodeDecodeError, MonitorError) as exc:
                if _PERF_MARKER.search(raw_line.decode("utf-8", errors="replace")):
                    diagnostics["invalid"] += 1
                    diagnostics.setdefault("errors", []).append(f"{path}:{line_start}: {exc}")
                continue
            if record is None:
                continue
            diagnostics["records_read"] += 1
            if record.client is None:
                diagnostics["missing_client"] += 1
            if excluded and record.model.strip().lower() in excluded:
                # Another provider reached through the same proxy.  It does not
                # consume Codex quota, so it must not reach aggregation: doing
                # so would price it ``unpriced_model``, keep every period
                # incomplete, and inflate the local token sum the outside-VM
                # evidence compares against the Codex account counter.
                diagnostics["excluded_models"] += 1
                diagnostics["excluded"] += 1
                continue
            if client and record.client and record.client.lower() != client.lower():
                diagnostics["excluded"] += 1
                continue
            if start_at is not None and record.timestamp <= start_at:
                diagnostics["excluded"] += 1
                diagnostics["late"] += 1
                continue
            if record.timestamp > end_at:
                diagnostics["excluded"] += 1
                diagnostics["late"] += 1
                continue
            records.append(record)
            diagnostics["accepted"] += 1
        # Keep an incomplete final line for the next read by advancing only to
        # the last complete newline.
        current["files"][key] = {
            "path": str(path),
            "offset": offset,
            "size": stat_result.st_size,
            "head_hash": head_hash,
            "tail_hash": _file_digest(path, from_end=True),
        }

    # A cursor entry disappearing while the file is no longer retained means a
    # period may contain usage that cannot be recovered from local logs.
    for old_key, old_entry in old_files_snapshot.items():
        if old_key not in seen_keys and isinstance(old_entry, dict):
            old_path = old_entry.get("path")
            if old_path and not Path(str(old_path)).exists():
                diagnostics["coverage_gap"] = True
    current["coverage_gap"] = bool(diagnostics["coverage_gap"])
    current["last_scan_at"] = time.time()
    records.sort(key=lambda item: (item.timestamp, item.source_file, item.source_offset))
    return records, current, diagnostics


def _pricing_denominator(unit: str) -> Decimal:
    normalized = unit.lower().replace(" ", "_")
    if "1k" in normalized or "1000" in normalized:
        return Decimal("1000")
    if "1m" in normalized or "1000000" in normalized or "million" in normalized:
        return Decimal("1000000")
    raise MonitorError(f"Unsupported pricing unit {unit!r}; use per_1m_tokens")


def _decimal_cost(tokens: int, rate: Decimal, denominator: Decimal) -> Decimal:
    return (Decimal(tokens) * rate) / denominator


def price_usage_record(
    record: HeadroomUsageRecord,
    pricing: PricingTable,
    *,
    inferred_cache_write_mode: str | None = None,
    processing_tier: str | None = None,
) -> CostBreakdown:
    """Price one request using uncached input, cached input, and output only.

    Headroom exposes a cache_write-like counter for its internal accounting,
    but Codex-equivalent pricing does not bill that counter as a third input
    category.  The canonical uncached input count is therefore always
    ``input_tokens - cache_read_tokens``.
    """

    if pricing.pricing_mode != "codex":
        return CostBreakdown(
            uncached_input_tokens=max(record.input_tokens - record.cache_read_tokens, 0),
            cached_input_tokens=record.cache_read_tokens,
            output_tokens=record.output_tokens,
            uncached_input_cost=None,
            cached_input_cost=None,
            output_cost=None,
            total_cost=None,
            complete=False,
            reason="unsupported_pricing_mode",
            inferred_cache_write=record.cache_write_inferred,
            raw_cache_write_tokens=record.raw_cache_write_tokens,
        )
    model = pricing.resolve(record.model)
    raw_read = record.cache_read_tokens
    raw_write = record.raw_cache_write_tokens
    if (
        record.input_tokens < 0
        or record.output_tokens < 0
        or raw_read < 0
        or raw_write < 0
        or raw_read > record.input_tokens
    ):
        return CostBreakdown(
            uncached_input_tokens=max(record.input_tokens - raw_read, 0),
            cached_input_tokens=raw_read,
            output_tokens=record.output_tokens,
            uncached_input_cost=None,
            cached_input_cost=None,
            output_cost=None,
            total_cost=None,
            complete=False,
            reason="invalid_counters",
            inferred_cache_write=record.cache_write_inferred,
            raw_cache_write_tokens=raw_write,
        )
    uncached_tokens = record.input_tokens - raw_read
    if model is None:
        return CostBreakdown(
            uncached_input_tokens=uncached_tokens,
            cached_input_tokens=raw_read,
            output_tokens=record.output_tokens,
            uncached_input_cost=None,
            cached_input_cost=None,
            output_cost=None,
            total_cost=None,
            complete=False,
            reason="unpriced_model",
            inferred_cache_write=record.cache_write_inferred,
            raw_cache_write_tokens=raw_write,
        )
    if record.local_cache_hit:
        # Keep observed counters for diagnostics, but a response served from
        # Headroom's local cache did not create an upstream billable request.
        return CostBreakdown(
            uncached_input_tokens=uncached_tokens,
            cached_input_tokens=raw_read,
            output_tokens=record.output_tokens,
            uncached_input_cost=Decimal("0"),
            cached_input_cost=Decimal("0"),
            output_cost=Decimal("0"),
            total_cost=Decimal("0"),
            complete=True,
            reason="local_cache_hit",
            inferred_cache_write=record.cache_write_inferred,
            local_cache_hit=True,
            raw_cache_write_tokens=raw_write,
        )
    tier = (
        record.processing_tier
        or processing_tier
        or os.getenv("PRICING_PROCESSING_TIER")
        or pricing.processing_tier
        or "standard"
    ).strip().lower()
    processing_multiplier = model.processing_multiplier(tier)
    long_context = (
        model.long_context_threshold_tokens is not None
        and record.input_tokens > model.long_context_threshold_tokens
    )
    input_multiplier = model.long_context_input_multiplier if long_context else Decimal("1")
    output_multiplier = model.long_context_output_multiplier if long_context else Decimal("1")
    denominator = _pricing_denominator(pricing.unit)
    common_input_multiplier = processing_multiplier * input_multiplier
    output_rate = model.output_rate * processing_multiplier * output_multiplier
    input_rate = model.input_rate * common_input_multiplier
    cached_rate = model.cached_input_rate * common_input_multiplier
    uncached_cost = _decimal_cost(uncached_tokens, input_rate, denominator)
    cached_cost = _decimal_cost(raw_read, cached_rate, denominator)
    output_cost = _decimal_cost(record.output_tokens, output_rate, denominator)
    return CostBreakdown(
        uncached_input_tokens=uncached_tokens,
        cached_input_tokens=raw_read,
        output_tokens=record.output_tokens,
        uncached_input_cost=uncached_cost,
        cached_input_cost=cached_cost,
        output_cost=output_cost,
        total_cost=uncached_cost + cached_cost + output_cost,
        complete=True,
        inferred_cache_write=record.cache_write_inferred,
        raw_cache_write_tokens=raw_write,
        processing_tier=tier,
        long_context=long_context,
    )


def _sum_decimal(values: Iterable[Decimal]) -> Decimal:
    result = Decimal("0")
    for value in values:
        result += value
    return result


def aggregate_period(
    records: Iterable[HeadroomUsageRecord],
    pricing: PricingTable,
    period_start: float,
    period_end: float,
    diagnostics: dict[str, Any] | None = None,
    *,
    inferred_cache_write_mode: str | None = None,
    processing_tier: str | None = None,
) -> dict[str, Any]:
    """Aggregate token totals, component costs, and pricing quality by model."""

    rows: dict[str, dict[str, Any]] = {}
    invalid = 0
    for record in records:
        canonical = pricing.canonical_name(record.model)
        row = rows.setdefault(
            canonical,
            {
                "model": canonical,
                "requests": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_tokens": 0,
                "raw_cache_write_tokens": 0,
                # Legacy alias retained in logged payloads.
                "cache_write_tokens": 0,
                "cached_input_tokens": 0,
                "uncached_input_tokens": 0,
                # Schema-v2 compatibility aliases.  They are never priced as
                # a separate cache-write category.
                "ordinary_input_tokens": 0,
                "priced_cache_write_tokens": 0,
                "uncached_input_cost": Decimal("0"),
                "cached_input_cost": Decimal("0"),
                "output_cost": Decimal("0"),
                "estimated_cost": Decimal("0"),
                "known_cost": Decimal("0"),
                "complete": True,
                "reasons": [],
                "inferred_cache_write_requests": 0,
                "local_cache_hits": 0,
                "quality_flags": [],
                "pricing_revision": pricing.revision,
                "processing_tiers": [],
                "long_context_requests": 0,
                "pricing_basis": "uncached_input_plus_cached_input_plus_output",
                "cache_write_billing": "ignored",
                "cost_basis": "current_price_equivalent",
            },
        )
        row["requests"] += 1
        row["input_tokens"] += record.input_tokens
        row["output_tokens"] += record.output_tokens
        row["cache_read_tokens"] += record.cache_read_tokens
        row["raw_cache_write_tokens"] += record.raw_cache_write_tokens
        row["cache_write_tokens"] += record.raw_cache_write_tokens
        if record.cache_write_inferred:
            row["inferred_cache_write_requests"] += 1
        if record.local_cache_hit:
            row["local_cache_hits"] += 1
        for flag in record.quality_flags:
            if flag not in row["quality_flags"]:
                row["quality_flags"].append(flag)
        breakdown = price_usage_record(
            record,
            pricing,
            inferred_cache_write_mode=inferred_cache_write_mode,
            processing_tier=processing_tier,
        )
        row["uncached_input_tokens"] += breakdown.uncached_input_tokens
        row["ordinary_input_tokens"] += breakdown.uncached_input_tokens
        row["cached_input_tokens"] += breakdown.cached_input_tokens
        # ``priced_cache_write_tokens`` is intentionally zero in schema v3.
        # Keep the raw source counter above for diagnostics only.
        if breakdown.processing_tier not in row["processing_tiers"]:
            row["processing_tiers"].append(breakdown.processing_tier)
        if breakdown.long_context:
            row["long_context_requests"] += 1
        if breakdown.complete:
            row["uncached_input_cost"] += breakdown.uncached_input_cost or Decimal("0")
            row["cached_input_cost"] += breakdown.cached_input_cost or Decimal("0")
            row["output_cost"] += breakdown.output_cost or Decimal("0")
            if row["estimated_cost"] is not None:
                row["estimated_cost"] += breakdown.total_cost or Decimal("0")
            row["known_cost"] += breakdown.total_cost or Decimal("0")
        else:
            row["complete"] = False
            row["estimated_cost"] = None
            reason = breakdown.reason or "unavailable_cost"
            if reason not in row["reasons"]:
                row["reasons"].append(reason)
            if reason == "invalid_counters":
                invalid += 1
    supplied_diagnostics = dict(diagnostics or {})
    supplied_diagnostics["invalid"] = int(supplied_diagnostics.get("invalid", 0)) + invalid
    supplied_diagnostics.setdefault("accepted", sum(row["requests"] for row in rows.values()))
    supplied_diagnostics.setdefault("coverage_gap", False)
    known_total = _sum_decimal(row["known_cost"] for row in rows.values())
    complete = bool(rows) and all(row["complete"] for row in rows.values()) and not supplied_diagnostics.get(
        "coverage_gap"
    ) and supplied_diagnostics.get("invalid", 0) == 0
    total = {
        "requests": sum(row["requests"] for row in rows.values()),
        "input_tokens": sum(row["input_tokens"] for row in rows.values()),
        "output_tokens": sum(row["output_tokens"] for row in rows.values()),
        "cache_read_tokens": sum(row["cache_read_tokens"] for row in rows.values()),
        "raw_cache_write_tokens": sum(row["raw_cache_write_tokens"] for row in rows.values()),
        "cache_write_tokens": sum(row["cache_write_tokens"] for row in rows.values()),
        "uncached_input_tokens": sum(row["uncached_input_tokens"] for row in rows.values()),
        "ordinary_input_tokens": sum(row["ordinary_input_tokens"] for row in rows.values()),
        "cached_input_tokens": sum(row["cached_input_tokens"] for row in rows.values()),
        "priced_cache_write_tokens": 0,
        "pricing_basis": "uncached_input_plus_cached_input_plus_output",
        "cache_write_billing": "ignored",
        "cost_basis": "current_price_equivalent",
        "known_cost": known_total,
        "estimated_cost": known_total if complete else None,
        "complete": complete,
        "reason": None if complete else (
            "partial_coverage" if supplied_diagnostics.get("coverage_gap") else
            "invalid_counters" if supplied_diagnostics.get("invalid") else
            "unpriced_model" if any(not row["complete"] for row in rows.values()) else "no_usage"
        ),
    }
    return {
        "period_start": period_start,
        "period_end": period_end,
        "elapsed_seconds": max(0, period_end - period_start),
        "models": rows,
        "total": total,
        "diagnostics": supplied_diagnostics,
    }


def format_timestamp(timestamp: int | None) -> str:
    if timestamp is None:
        return "unknown"
    return time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(timestamp))


def detect_events(previous: Snapshot, current: Snapshot) -> list[Event]:
    events: list[Event] = []
    min_drop = float(os.getenv("RESET_MIN_PERCENT_DROP", "1"))

    labels = {
        "primary": "short Codex usage window",
        "secondary": "long/weekly Codex usage window",
    }
    for field_name, label in labels.items():
        old = getattr(previous, field_name)
        new = getattr(current, field_name)
        if old is None or new is None:
            continue
        if old.used_percent is None or new.used_percent is None:
            continue
        reset_timestamp_advanced = (
            old.resets_at is not None
            and new.resets_at is not None
            and new.resets_at > old.resets_at + 60
        )
        usage_dropped = new.used_percent <= old.used_percent - min_drop
        if reset_timestamp_advanced and usage_dropped:
            events.append(
                Event(
                    kind=f"{field_name}_window_reset",
                    title=f"Codex {label} reset",
                    body=(
                        f"Usage changed from {old.used_percent:g}% used to "
                        f"{new.used_percent:g}% used. The next reset is "
                        f"{format_timestamp(new.resets_at)}."
                    ),
                )
            )

    old_count = previous.reset_credit_available_count
    new_count = current.reset_credit_available_count
    new_ids = set(current.reset_credit_ids) - set(previous.reset_credit_ids)
    if old_count is not None and new_count is not None and new_count > old_count:
        events.append(
            Event(
                kind="reset_credit_granted",
                title="New Codex reset credit available",
                body=f"Available reset credits increased from {old_count} to {new_count}.",
            )
        )
    elif new_ids:
        events.append(
            Event(
                kind="reset_credit_granted",
                title="New Codex reset credit available",
                body=f"Codex reported {len(new_ids)} newly available reset credit(s).",
            )
        )

    if previous.credit_balance is not None and current.credit_balance is not None:
        old_balance = Decimal(previous.credit_balance)
        new_balance = Decimal(current.credit_balance)
        if new_balance > old_balance:
            events.append(
                Event(
                    kind="credit_balance_increased",
                    title="Codex credit balance increased",
                    body=f"Credit balance changed from {old_balance} to {new_balance}.",
                )
            )

    return events


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    return value


def _decimal_value(value: Any, default: Decimal = Decimal("0")) -> Decimal:
    if value is None:
        return default
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return default
    return result if result.is_finite() else default


def _decimal_mapping(value: Any) -> dict[str, Decimal]:
    if not isinstance(value, dict):
        return {}
    return {str(name): _decimal_value(amount) for name, amount in value.items()}


def _usage_difference(later: Any, earlier: Any) -> dict[str, int]:
    later_counters = _usage_counters(later)
    earlier_counters = _usage_counters(earlier)
    return {
        field: max(0, later_counters[field] - earlier_counters[field])
        for field in _USAGE_COUNTER_FIELDS
    }


def _confidence_reasons(window_state: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    if window_state.get("has_uncertain_attribution"):
        reasons.append("uncertain_attribution")
    if window_state.get("has_imputed"):
        reasons.append("imputed_external_usage")
    if window_state.get("has_unobserved_percentage"):
        reasons.append("unobserved_percentage_change")
    if window_state.get("has_external_usage_unquantified"):
        reasons.append("external_usage_unquantified")
    return reasons


def _confidence_label(points: int, *, reduced: bool = False) -> str:
    if points < 2:
        label = "Very low"
    elif points <= 4:
        label = "Low"
    elif points <= 9:
        label = "Medium"
    elif points <= 19:
        label = "High"
    else:
        label = "Very high"
    if not reduced:
        return label
    order = ["Very low", "Low", "Medium", "High", "Very high"]
    return order[max(0, order.index(label) - 1)]


def _crossing_payload(
    percent: int,
    captured_at: float,
    costs_by_model: dict[str, Decimal],
    usage_total: dict[str, int],
    usage_by_model: dict[str, dict[str, int]],
    *,
    reliable: bool = True,
    reason: str | None = None,
    skipped_from: int | None = None,
    imputed: bool = False,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "percent": int(percent),
        "captured_at": float(captured_at),
        "cumulative_cost_usd": str(_sum_decimal(costs_by_model.values())),
        "costs_by_model": {
            name: str(value) for name, value in sorted(costs_by_model.items())
        },
        "cumulative_usage": _cumulative_usage_payload(
            usage_total, usage_by_model, costs_by_model, _sum_decimal(costs_by_model.values())
        ),
        "reliable": bool(reliable),
    }
    if reason:
        payload["reason"] = reason
    if imputed:
        payload["imputed"] = True
        payload["has_imputed"] = True
    if skipped_from is not None and int(percent) - int(skipped_from) > 1:
        payload["skipped_from"] = int(skipped_from)
        payload["skipped_points"] = list(range(int(skipped_from) + 1, int(percent)))
    return payload


def _model_interval_rows(
    start: dict[str, Any],
    end: dict[str, Any],
    interval_cost: Decimal,
) -> dict[str, dict[str, Any]]:
    start_costs = _decimal_mapping(start.get("costs_by_model"))
    end_costs = _decimal_mapping(end.get("costs_by_model"))
    start_models = start.get("cumulative_usage", {}).get("models", {})
    end_models = end.get("cumulative_usage", {}).get("models", {})
    names = sorted(set(start_costs) | set(end_costs) | set(start_models) | set(end_models))
    rows: dict[str, dict[str, Any]] = {}
    for name in names:
        cost = max(Decimal("0"), end_costs.get(name, Decimal("0")) - start_costs.get(name, Decimal("0")))
        usage = _usage_difference(end_models.get(name), start_models.get(name))
        rows[name] = {
            "model": name,
            "requests": usage["requests"],
            "input_tokens": usage["input_tokens"],
            "cached_input_tokens": usage["cached_input_tokens"],
            "uncached_input_tokens": usage["uncached_input_tokens"],
            "output_tokens": usage["output_tokens"],
            "cache_read_tokens": usage["cache_read_tokens"],
            "raw_cache_write_tokens": usage["raw_cache_write_tokens"],
            # Legacy alias for consumers of schema v2 logs.
            "cache_write_tokens": usage["cache_write_tokens"],
            "cost_usd": str(cost),
            "interval_cost_percent": str(
                (cost * Decimal("100") / interval_cost) if interval_cost > 0 else Decimal("0")
            ),
        }
    return rows


def _model_cumulative_rows(
    usage_by_model: dict[str, dict[str, Any]],
    costs_by_model: dict[str, Decimal],
    total_cost: Decimal,
) -> dict[str, dict[str, Any]]:
    """Build attribution rows for all usage accumulated after the anchor."""

    names = sorted(set(costs_by_model) | set(usage_by_model))
    rows: dict[str, dict[str, Any]] = {}
    for name in names:
        usage = _usage_counters(usage_by_model.get(name))
        cost = max(Decimal("0"), costs_by_model.get(name, Decimal("0")))
        rows[name] = {
            "model": name,
            "requests": usage["requests"],
            "input_tokens": usage["input_tokens"],
            "cached_input_tokens": usage["cached_input_tokens"],
            "uncached_input_tokens": usage["uncached_input_tokens"],
            "output_tokens": usage["output_tokens"],
            "cache_read_tokens": usage["cache_read_tokens"],
            "raw_cache_write_tokens": usage["raw_cache_write_tokens"],
            # Legacy alias for consumers of schema v2 logs.
            "cache_write_tokens": usage["cache_write_tokens"],
            "cost_usd": str(cost),
            "interval_cost_percent": str(
                (cost * Decimal("100") / total_cost)
                if total_cost > 0
                else Decimal("0")
            ),
        }
    return rows


def _find_crossing_index(crossings: list[dict[str, Any]], percent: int, before: int | None = None) -> int | None:
    end = len(crossings) - 1 if before is None else min(before - 1, len(crossings) - 1)
    for index in range(end, -1, -1):
        try:
            if int(crossings[index].get("percent")) == int(percent):
                return index
        except (TypeError, ValueError):
            continue
    return None


def _interval_estimate(
    crossings: list[dict[str, Any]],
    start_percent: int,
    end_percent: int,
    *,
    label: str,
    reduced_confidence: bool = False,
) -> dict[str, Any] | None:
    if end_percent <= start_percent or not crossings:
        return None
    end_index = _find_crossing_index(crossings, end_percent)
    if end_index is None:
        return None
    start_index = _find_crossing_index(crossings, start_percent, end_index + 1)
    if start_index is None or start_index >= end_index:
        return None
    interval_crossings = crossings[start_index : end_index + 1]
    if any(not bool(row.get("reliable", True)) for row in interval_crossings):
        return None
    start = interval_crossings[0]
    end = interval_crossings[-1]
    start_cost = _decimal_value(start.get("cumulative_cost_usd"))
    end_cost = _decimal_value(end.get("cumulative_cost_usd"))
    interval_cost = max(Decimal("0"), end_cost - start_cost)
    points = int(end_percent) - int(start_percent)
    if points <= 0 or interval_cost <= 0:
        return None
    estimate = interval_cost * Decimal("100") / Decimal(points)
    interval_has_imputed = any(bool(row.get("imputed")) for row in interval_crossings)
    return {
        "label": label,
        "start_percent": int(start_percent),
        "end_percent": int(end_percent),
        "observed_range": f"{int(start_percent)}% -> {int(end_percent)}%",
        "completed_percentage_points": points,
        "start_captured_at": start.get("captured_at"),
        "end_captured_at": end.get("captured_at"),
        "cost_usd": str(interval_cost),
        "estimate_100_usd": str(estimate),
        "confidence": _confidence_label(points, reduced=reduced_confidence),
        "partial_buckets_excluded": True,
        "calculation_basis": "completed_percentage_crossings",
        "has_imputed": interval_has_imputed,
        "model_attribution": _model_interval_rows(start, end, interval_cost),
    }


def _anchor_accumulation_estimate(
    window_state: dict[str, Any],
    anchor: dict[str, Any],
    anchor_percent: int,
    current_percent: int,
    known_cost: Decimal,
) -> dict[str, Any] | None:
    """Estimate the full window from all cost accumulated since its anchor."""

    points = current_percent - anchor_percent
    if points <= 0 or known_cost <= 0:
        return None
    unobserved_points = 0
    for gap in window_state.get("unobserved_percentage_ranges", []):
        if not isinstance(gap, dict):
            continue
        try:
            gap_start = int(gap["from_percent"])
            gap_end = int(gap["to_percent"])
        except (KeyError, TypeError, ValueError):
            continue
        unobserved_points += max(
            0, min(current_percent, gap_end) - max(anchor_percent, gap_start)
        )
    unobserved_points = min(points, unobserved_points)
    counted_points = points - unobserved_points
    if counted_points <= 0:
        return None
    estimate = known_cost * Decimal("100") / Decimal(counted_points)
    costs_by_model = {
        str(name): _decimal_value(value)
        for name, value in (window_state.get("costs_by_model", {}) or {}).items()
    }
    usage_by_model = {
        str(name): value
        for name, value in (window_state.get("usage_by_model", {}) or {}).items()
        if isinstance(value, dict)
    }
    return {
        "label": "full_observed",
        "start_percent": anchor_percent,
        "end_percent": current_percent,
        "observed_range": f"{anchor_percent}% -> {current_percent}%",
        "completed_percentage_points": points,
        "counted_percentage_points": counted_points,
        "excluded_no_token_percentage_points": unobserved_points,
        "start_captured_at": anchor.get("captured_at"),
        "end_captured_at": window_state.get("last_sample_at"),
        "cost_usd": str(known_cost),
        "estimate_100_usd": str(estimate),
        "confidence": _confidence_label(
            counted_points, reduced=bool(window_state.get("has_uncertain_attribution"))
        ),
        "partial_buckets_excluded": False,
        "calculation_basis": "priced_cost_over_token_backed_percentage_points",
        "has_imputed": bool(window_state.get("has_imputed", False)),
        "imputed_cost_usd": str(_decimal_value(window_state.get("imputed_cost_usd"))),
        "model_attribution": _model_cumulative_rows(
            usage_by_model, costs_by_model, known_cost
        ),
    }


def _estimate_window(
    window_name: str,
    window_state: dict[str, Any],
    current_window: LimitWindow,
    *,
    status: str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    anchor = window_state.get("anchor", {})
    crossings = [row for row in window_state.get("crossings", []) if isinstance(row, dict)]
    current_percent = int(Decimal(str(current_window.used_percent or 0)))
    anchor_percent = int(Decimal(str(anchor.get("used_percent", current_percent))))
    known_cost = _decimal_value(window_state.get("known_cost"))
    full = None
    if crossings:
        full = _anchor_accumulation_estimate(
            window_state,
            anchor,
            anchor_percent,
            current_percent,
            known_cost,
        )
    rolling: dict[str, Any] = {}
    if crossings:
        try:
            latest_percent = int(crossings[-1].get("percent"))
        except (TypeError, ValueError):
            latest_percent = anchor_percent
        for points in _CROSSING_ROLLING_POINTS:
            interval = _interval_estimate(
                crossings,
                latest_percent - points,
                latest_percent,
                label=f"last_{points}_percentage_points",
                reduced_confidence=bool(window_state.get("has_uncertain_attribution")),
            )
            if interval is not None:
                rolling[str(points)] = interval
        # Jumps larger than 1 point (e.g. 85% -> 87%, 98% -> 100%) never
        # match a strict last_1 window, so without this nothing rolling is
        # calculable right after them. Report the most recent
        # crossing-to-crossing jump as-is whenever it spans >1 point.
        if len(crossings) >= 2:
            try:
                prev_percent = int(crossings[-2].get("percent"))
            except (TypeError, ValueError):
                prev_percent = latest_percent
            if latest_percent - prev_percent > 1:
                jump = _interval_estimate(
                    crossings,
                    prev_percent,
                    latest_percent,
                    label="last_jump",
                    reduced_confidence=bool(window_state.get("has_uncertain_attribution")),
                )
                if jump is not None:
                    rolling["last_jump"] = jump

    external_usage_unquantified = bool(window_state.get("has_external_usage_unquantified", False))
    headroom_rolling = {} if external_usage_unquantified else rolling
    account_rolling = {} if external_usage_unquantified else rolling
    raw_projection = None
    raw_delta = Decimal(str(current_window.used_percent)) - Decimal(str(anchor.get("used_percent", 0)))
    if raw_delta > 0 and known_cost > 0 and not external_usage_unquantified:
        raw_projection = known_cost * Decimal("100") / raw_delta
    # A local-cost/all-points ratio is not a local-only estimate when some
    # percentage points may have come from outside Headroom. We cannot map
    # unquantified account usage to exact points, so withhold every normalized
    # projection rather than leave external points in the denominator.
    headroom_projection = (
        _decimal_value(full.get("estimate_100_usd"))
        if full and not external_usage_unquantified
        else None
    )
    full_value = None if external_usage_unquantified else headroom_projection
    if full is not None:
        full["headroom_only_100_usd"] = str(headroom_projection) if headroom_projection is not None else None
        full["external_usage_status"] = (
            "unquantified" if external_usage_unquantified else "no_external_detected_in_reliable_samples"
        )
        if external_usage_unquantified:
            full["estimate_100_usd"] = None
            full["label"] = "headroom_only_projection"
    model_shares = {}
    if full:
        for model, row in full.get("model_attribution", {}).items():
            model_shares[model] = str(
                _decimal_value(row.get("cost_usd"))
                * Decimal("100")
                / Decimal(full.get("counted_percentage_points", full["completed_percentage_points"]))
            )
    spike_rows: list[dict[str, Any]] = []
    for spike in window_state.get("spikes", []):
        if not isinstance(spike, dict):
            continue
        current_spike = dict(spike)
        spike_cost = _decimal_value(spike.get("cost_usd"))
        current_spike["share_of_observed_cost"] = str(
            spike_cost * Decimal("100") / known_cost if known_cost > 0 else Decimal("0")
        )
        spike_rows.append(current_spike)
    if status is None:
        status = "estimated" if full else "insufficient_percentage_crossings"
    estimate: dict[str, Any] = {
        "window": window_name,
        "status": status,
        "reason": reason if reason is not None else ("mixed_models" if full else "no_completed_interval"),
        "estimate_basis": "cumulative_since_anchor",
        "anchor_captured_at": anchor.get("captured_at"),
        "span_end": window_state.get("last_sample_at"),
        "anchor_used_percent": anchor.get("used_percent"),
        "current_used_percent": current_window.used_percent,
        "delta_percent_points": str(
            Decimal(str(current_window.used_percent)) - Decimal(str(anchor.get("used_percent", 0)))
        ),
        "reset_at": current_window.resets_at,
        "window_duration_mins": current_window.window_duration_mins,
        "pricing_revision": anchor.get("pricing_revision"),
        "known_cost_usd": str(known_cost),
        # Backward-compatible field name; the authoritative full estimate uses
        # all known cost accumulated after the anchor through this sample.
        "full_window_usd": str(full_value) if full_value is not None else None,
        "account_total_100_usd": str(full_value) if full_value is not None else None,
        "local_token_backed_100_usd": (
            str(headroom_projection) if headroom_projection is not None else None
        ),
        "external_usage_status": (
            "unquantified" if external_usage_unquantified else "no_external_detected_in_reliable_samples"
        ),
        "full_observed_estimate": full,
        "rolling_estimates": account_rolling,
        "headroom_only_rolling_estimates": headroom_rolling,
        "raw_current_bucket_projection_usd": str(raw_projection) if raw_projection is not None else None,
        "completed_percentage_points": full.get("completed_percentage_points", 0) if full else 0,
        "locally_attributed_percentage_points": (
            full.get("counted_percentage_points", 0)
            if full and not external_usage_unquantified
            else None
        ),
        "counted_percentage_points": full.get("counted_percentage_points", 0) if full else 0,
        "excluded_no_token_percentage_points": (
            full.get("excluded_no_token_percentage_points", 0) if full else max(
                0,
                int(Decimal(str(current_window.used_percent or 0)))
                - int(Decimal(str(anchor.get("used_percent", 0))))
            ) if window_state.get("has_unobserved_percentage") else 0
        ),
        "confidence": full.get("confidence", "Very low") if full else "Very low",
        "model_shares_at_100_usd": {} if external_usage_unquantified else model_shares,
        "headroom_model_shares_at_100_usd": (
            {} if external_usage_unquantified else model_shares
        ),
        "exclusive_model": None,
        "exclusive_use_usd": None,
        "cumulative_usage": _cumulative_usage_payload(
            window_state.get("usage_total"),
            window_state.get("usage_by_model"),
            window_state.get("costs_by_model"),
            known_cost,
        ),
        "crossings": crossings,
        "cycle_history": list(window_state.get("cycle_history", [])),
        "spikes": spike_rows,
        "has_imputed": bool(window_state.get("has_imputed", False)),
        "imputed_cost_usd": str(_decimal_value(window_state.get("imputed_cost_usd"))),
        "has_unobserved_percentage": bool(window_state.get("has_unobserved_percentage")),
        "unobserved_percentage_ranges": list(window_state.get("unobserved_percentage_ranges", [])),
        "has_external_usage_unquantified": external_usage_unquantified,
        "exact_cost_usd": str(known_cost - _decimal_value(window_state.get("imputed_cost_usd"))),
        "confidence_reasons": _confidence_reasons(window_state),
    }
    return estimate


def _close_cycle_summary(
    window_name: str,
    window_state: dict[str, Any],
    previous_window: LimitWindow | None,
    closed_at: float,
    reason: str,
) -> dict[str, Any]:
    if previous_window is None or previous_window.used_percent is None:
        current = LimitWindow(
            used_percent=window_state.get("anchor", {}).get("used_percent"),
            resets_at=window_state.get("anchor", {}).get("resets_at"),
            window_duration_mins=window_state.get("anchor", {}).get("window_duration_mins"),
        )
    else:
        current = previous_window
    estimate = _estimate_window(window_name, window_state, current, status="closed", reason=reason)
    return {
        "window": window_name,
        "closed_at": closed_at,
        "close_reason": reason,
        "anchor": window_state.get("anchor"),
        "observed_percent": (
            f"{estimate.get('anchor_used_percent')}% -> {current.used_percent}%"
            if estimate.get("full_observed_estimate") or window_state.get("has_unobserved_percentage")
            else None
        ),
        "known_cost_usd": estimate.get("known_cost_usd", "0"),
        "full_observed_estimate": estimate.get("full_observed_estimate"),
        "local_token_backed_100_usd": estimate.get("local_token_backed_100_usd"),
        "external_usage_status": estimate.get("external_usage_status"),
        "has_external_usage_unquantified": bool(window_state.get("has_external_usage_unquantified", False)),
        "completed_percentage_points": (
            int(Decimal(str(current.used_percent or 0))) -
            int(Decimal(str(window_state.get("anchor", {}).get("used_percent", 0))))
            if window_state.get("has_unobserved_percentage") else estimate.get("completed_percentage_points", 0)
        ),
        "confidence": estimate.get("confidence", "Very low"),
        "unobserved_percentage_ranges": list(window_state.get("unobserved_percentage_ranges", [])),
        "has_external_usage_unquantified": bool(
            window_state.get("has_external_usage_unquantified", False)
        ),
        "cumulative_usage": estimate.get("cumulative_usage"),
        "model_attribution": (
            (estimate.get("full_observed_estimate") or {}).get("model_attribution")
            or _model_cumulative_rows(
                window_state.get("usage_by_model", {}),
                {name: _decimal_value(value) for name, value in
                 (window_state.get("costs_by_model", {}) or {}).items()},
                _decimal_value(window_state.get("known_cost")),
            )
        ),
        "spikes": estimate.get("spikes", []),
    }


def empty_accounting_state(pricing_revision: str | None = None) -> dict[str, Any]:
    return {
        "schema_version": _ACCOUNTING_SCHEMA_VERSION,
        "pricing_revision": pricing_revision,
        "limit_id": os.getenv("CODEX_LIMIT_ID", "codex"),
        "last_sample_at": None,
        "last_period_id": None,
        "cursor": _empty_cursor(),
        "windows": {"primary": None, "secondary": None},
        "recovery": {"pending": False, "coverage_gap": False},
        # Parallel outside-VM evidence (account lifetimeTokens deltas).
        # Never read by window calibration or estimates.
        "external_evidence": None,
    }


def _window_state_anchor(window: LimitWindow, captured_at: float, pricing_revision: str) -> dict[str, Any]:
    return {
        "captured_at": captured_at,
        "used_percent": window.used_percent,
        "resets_at": window.resets_at,
        "window_duration_mins": window.window_duration_mins,
        "pricing_revision": pricing_revision,
    }


def _baseline_window_state(
    window: LimitWindow | None,
    captured_at: float,
    pricing_revision: str,
    *,
    cycle_history: list[dict[str, Any]] | None = None,
    legacy_cycles: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    if window is None or window.used_percent is None:
        return None
    return {
        "schema_version": _ACCOUNTING_SCHEMA_VERSION,
        "anchor": _window_state_anchor(window, captured_at, pricing_revision),
        "costs_by_model": {},
        "usage_total": _usage_counters(),
        "usage_by_model": {},
        "models_seen": [],
        "known_cost": "0",
        "has_unpriced": False,
        "has_invalid": False,
        "has_coverage_gap": False,
        "has_uncertain_attribution": False,
        "has_imputed": False,
        "imputed_cost_usd": "0",
        "has_unobserved_percentage": False,
        "unobserved_percentage_ranges": [],
        "has_external_usage_unquantified": False,
        "crossings": [
            _crossing_payload(
                int(Decimal(str(window.used_percent))),
                captured_at,
                {},
                _usage_counters(),
                {},
            )
        ],
        "cycle_history": list(cycle_history or []),
        "legacy_cycles": list(legacy_cycles or []),
        "spikes": [],
    }


def update_window_calibration(
    window_name: str,
    previous_window: LimitWindow | None,
    current_window: LimitWindow | None,
    period: dict[str, Any],
    window_state: dict[str, Any] | None,
    pricing_revision: str,
    *,
    min_delta_points: float | None = None,
    conservative_unobserved: bool = False,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Accumulate one independent window and estimate from its anchor.

    Ten-minute samples update cumulative usage, but only an observed increase
    into a new integer percentage bucket creates a crossing.  The full-window
    estimate uses all cost accumulated after the anchor through the current
    sample.  Crossing-to-crossing intervals remain available for rolling
    estimates, so spending inside the current bucket is not discarded from the
    authoritative cumulative estimate.
    """

    return _update_window_calibration_inner(
        window_name,
        previous_window,
        current_window,
        period,
        window_state,
        pricing_revision,
        conservative_unobserved=conservative_unobserved,
    )


def _external_imputation_enabled() -> bool:
    raw = str(os.getenv("EXTERNAL_IMPUTATION", "1")).strip().lower()
    return raw not in {"0", "false", "no", "off", "disabled"}


def _external_imputation_min_points() -> int:
    try:
        value = int(
            os.getenv(
                "EXTERNAL_IMPUTATION_MIN_POINTS",
                str(_EXTERNAL_IMPUTATION_MIN_POINTS_DEFAULT),
            )
        )
    except (TypeError, ValueError):
        return _EXTERNAL_IMPUTATION_MIN_POINTS_DEFAULT
    return max(1, value)


def _imputation_basis(
    window_state: dict[str, Any], anchor_percent: int
) -> dict[str, Any] | None:
    """Mean priced cost and token mix per completed point, from crossings.

    The app-server rate-limit call reports account-wide percentages only, so
    the in/cached/out split for outside-VM spend cannot come from it.  It is
    derived from this window's own completed crossings instead.  Returns None
    when there is not enough priced history to impute from.
    """

    crossings = [row for row in window_state.get("crossings", []) if isinstance(row, dict)]
    if len(crossings) < 2:
        return None
    latest = crossings[-1]
    try:
        latest_percent = int(latest.get("percent"))
    except (TypeError, ValueError):
        return None
    points = latest_percent - int(anchor_percent)
    if points < _external_imputation_min_points():
        return None
    basis_cost = _decimal_value(latest.get("cumulative_cost_usd"))
    if basis_cost <= 0:
        return None
    cumulative = latest.get("cumulative_usage", {})
    if not isinstance(cumulative, dict):
        return None

    def _count(key: str) -> int:
        try:
            return max(0, int(cumulative.get(key, 0)))
        except (TypeError, ValueError):
            return 0

    basis = {
        "points": points,
        "cost": basis_cost,
        "input_tokens": _count("input_tokens"),
        "cached_input_tokens": _count("cached_input_tokens"),
        "output_tokens": _count("output_tokens"),
        "requests": _count("requests"),
    }
    if basis["input_tokens"] <= 0 and basis["output_tokens"] <= 0:
        return None
    return basis


def _scale_imputed_counters(
    basis: dict[str, Any], delta_points: int
) -> tuple[dict[str, int], Decimal]:
    """Scale the window's mean mix to an unattributed point delta."""

    points = int(basis["points"])

    def _scale(value: int) -> int:
        return int(round(value * delta_points / points))

    counters = _usage_counters()
    counters["input_tokens"] = _scale(int(basis["input_tokens"]))
    counters["cached_input_tokens"] = min(
        _scale(int(basis["cached_input_tokens"])), counters["input_tokens"]
    )
    counters["cache_read_tokens"] = counters["cached_input_tokens"]
    counters["uncached_input_tokens"] = max(
        0, counters["input_tokens"] - counters["cached_input_tokens"]
    )
    counters["ordinary_input_tokens"] = counters["uncached_input_tokens"]
    counters["output_tokens"] = _scale(int(basis["output_tokens"]))
    if counters["input_tokens"] or counters["output_tokens"]:
        counters["requests"] = max(1, _scale(int(basis["requests"])))
    else:
        counters["requests"] = max(1, delta_points)
    cost = basis["cost"] * Decimal(delta_points) / Decimal(points)
    return counters, cost


def _update_window_calibration_inner(
    window_name: str,
    previous_window: LimitWindow | None,
    current_window: LimitWindow | None,
    period: dict[str, Any],
    window_state: dict[str, Any] | None,
    pricing_revision: str,
    *,
    conservative_unobserved: bool = False,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    if (
        current_window is None
        or current_window.used_percent is None
        or not math.isfinite(float(current_window.used_percent))
        or current_window.used_percent < 0
        or current_window.used_percent > 100
    ):
        return None, {
            "window": window_name,
            "status": "missing_window",
            "reason": "missing_percentage",
            "estimate_basis": "cumulative_since_anchor",
            "full_window_usd": None,
            "full_observed_estimate": None,
            "rolling_estimates": {},
            "raw_current_bucket_projection_usd": None,
            "completed_percentage_points": 0,
            "confidence": "Very low",
            "model_shares_at_100_usd": {},
            "exclusive_use_usd": None,
            "cumulative_usage": _cumulative_usage_payload({}, {}, {}, Decimal("0")),
            "crossings": [],
            "cycle_history": [],
            "spikes": [],
            "has_imputed": False,
            "imputed_cost_usd": "0",
            "exact_cost_usd": "0",
            "confidence_reasons": [],
        }
    if window_state is None or not isinstance(window_state.get("anchor"), dict):
        new_state = _baseline_window_state(current_window, period["period_end"], pricing_revision)
        assert new_state is not None
        return new_state, _estimate_window(
            window_name, new_state, current_window, status="baseline", reason="first_sample"
        )

    anchor = window_state["anchor"]
    anchor_revision = str(anchor.get("pricing_revision", pricing_revision))
    anchor_percent = _decimal_value(anchor.get("used_percent"), Decimal("0"))
    current_percent = Decimal(str(current_window.used_percent))
    previous_percent = current_percent
    if (
        previous_window is not None
        and previous_window.used_percent is not None
        and math.isfinite(float(previous_window.used_percent))
    ):
        previous_percent = Decimal(str(previous_window.used_percent))
    reset_changed = (
        anchor.get("window_duration_mins") != current_window.window_duration_mins
        or current_percent < previous_percent
        or current_percent < anchor_percent
    )

    def restart(reason: str, status: str) -> tuple[dict[str, Any], dict[str, Any]]:
        history = list(window_state.get("cycle_history", []))
        history.append(
            _close_cycle_summary(window_name, window_state, previous_window, period["period_end"], reason)
        )
        new_state = _baseline_window_state(
            current_window,
            period["period_end"],
            pricing_revision,
            cycle_history=history,
            legacy_cycles=list(window_state.get("legacy_cycles", [])),
        )
        assert new_state is not None
        return new_state, _estimate_window(window_name, new_state, current_window, status=status, reason=reason)

    if anchor_revision != pricing_revision:
        return restart("pricing_revision", "pricing_revision")
    if reset_changed:
        return restart("reset" if current_percent < previous_percent else "usage_decreased", "reset")

    diagnostics = period.get("diagnostics", {})
    # A coverage gap means some log bytes may be unrecoverable, but restarting
    # here split one weekly cycle (9% -> 100%) every time logs rotated.
    # Accumulate what was observed, keep the gap bytes out of the cost, and
    # flag uncertainty (reduced confidence) instead of discarding history.
    period_has_gap = bool(diagnostics.get("coverage_gap", False))
    if diagnostics.get("invalid", 0):
        return restart("invalid_counters", "invalid_counters")
    incomplete_rows = [
        row for row in period.get("models", {}).values() if not bool(row.get("complete", False))
    ]
    invalid_rows = [
        row
        for row in incomplete_rows
        if "invalid_counters" in set(row.get("reasons", []))
    ]
    if invalid_rows:
        return restart("invalid_counters", "invalid_counters")
    # Unpriced models (e.g. gpt-reserve) must not restart the window.
    # A 2+ point jump with priced usage plus one unpriced row previously
    # discarded the whole period's priced cost and split one weekly cycle
    # (9% -> 85% / 87% -> 100%). Accumulate priced rows, keep unpriced
    # tokens for diagnostics, and flag uncertainty instead.
    # Any incomplete row remaining here is unpriced-only (invalid rows
    # already restarted above).
    period_has_unpriced = bool(incomplete_rows)
    period_models = period.get("models", {})
    period_delta = current_percent - previous_percent
    # Outside-VM spend: the account percentage rose with no local Headroom
    # rows.  When this window already has enough priced history, impute the
    # missing points from its own mean cost and token mix instead of
    # restarting the cycle.  The imputed amount is attributed to
    # ``external-imputed`` and flagged approximate; without enough history
    # the restart below still applies so no numbers are invented.
    imputed_counters: dict[str, int] | None = None
    imputed_cost = Decimal("0")
    local_total = period.get("total", {})
    period_has_local_tokens = (
        int(local_total.get("input_tokens", 0) or 0)
        + int(local_total.get("output_tokens", 0) or 0)
    ) > 0
    unobserved_rise = conservative_unobserved and period_delta > 0 and not period_has_local_tokens
    if period_delta > 0 and not period_models and not conservative_unobserved and _external_imputation_enabled():
        delta_points = int(current_percent) - int(previous_percent)
        basis = _imputation_basis(window_state, int(anchor_percent))
        if delta_points > 0 and basis is not None:
            imputed_counters, imputed_cost = _scale_imputed_counters(basis, delta_points)
    if period_delta > 0 and not period_models and imputed_counters is None and not conservative_unobserved:
        return restart("percentage_increased_without_local_usage", "unattributed_usage")
    period_has_imputed = imputed_counters is not None
    costs_by_model: dict[str, Decimal] = {
        str(name): _decimal_value(value)
        for name, value in (window_state.get("costs_by_model", {}) or {}).items()
    }
    usage_total = _usage_counters(window_state.get("usage_total"))
    usage_by_model = {
        str(name): _usage_counters(value)
        for name, value in (window_state.get("usage_by_model", {}) or {}).items()
        if isinstance(value, dict)
    }
    models_seen = set(str(item) for item in (window_state.get("models_seen", []) or []))
    has_unpriced = bool(window_state.get("has_unpriced", False))
    has_invalid = bool(window_state.get("has_invalid", False)) or bool(diagnostics.get("invalid", 0))
    has_coverage_gap = bool(window_state.get("has_coverage_gap", False))
    has_uncertain_attribution = bool(window_state.get("has_uncertain_attribution", False))
    has_imputed = bool(window_state.get("has_imputed", False))
    imputed_total = _decimal_value(window_state.get("imputed_cost_usd"))
    unobserved_ranges = list(window_state.get("unobserved_percentage_ranges", []))
    has_unobserved_percentage = bool(window_state.get("has_unobserved_percentage", False))
    has_external_usage_unquantified = bool(
        window_state.get("has_external_usage_unquantified", False)
    )
    crossings = [row for row in window_state.get("crossings", []) if isinstance(row, dict)]
    if not crossings:
        crossings = [
            _crossing_payload(
                int(anchor_percent),
                float(anchor.get("captured_at", period["period_start"])),
                costs_by_model,
                usage_total,
                usage_by_model,
            )
        ]
    _add_usage_counters(usage_total, period.get("total"))
    for model_name, row in period_models.items():
        models_seen.add(str(model_name))
        model_usage = usage_by_model.setdefault(str(model_name), _usage_counters())
        _add_usage_counters(model_usage, row)
        row_complete = bool(row.get("complete", False))
        if row_complete:
            costs_by_model[str(model_name)] = costs_by_model.get(str(model_name), Decimal("0")) + _decimal_value(
                row.get("known_cost", row.get("estimated_cost"))
            )
        else:
            reasons = set(row.get("reasons", []))
            has_unpriced = has_unpriced or "unpriced_model" in reasons
            has_invalid = has_invalid or "invalid_counters" in reasons
    has_coverage_gap = has_coverage_gap or period_has_gap
    if period_has_gap:
        has_uncertain_attribution = True
    if period_has_unpriced:
        has_unpriced = True
        has_uncertain_attribution = True
    if period_has_imputed:
        assert imputed_counters is not None
        _add_usage_counters(usage_total, imputed_counters)
        imputed_usage = usage_by_model.setdefault(_IMPUTED_MODEL_NAME, _usage_counters())
        _add_usage_counters(imputed_usage, imputed_counters)
        models_seen.add(_IMPUTED_MODEL_NAME)
        costs_by_model[_IMPUTED_MODEL_NAME] = (
            costs_by_model.get(_IMPUTED_MODEL_NAME, Decimal("0")) + imputed_cost
        )
        imputed_total += imputed_cost
        has_imputed = True
        has_uncertain_attribution = True
    if unobserved_rise:
        has_unobserved_percentage = True
        has_uncertain_attribution = True
        unobserved_ranges.append({
            "from_percent": int(previous_percent),
            "to_percent": int(current_percent),
            "period_start": period.get("period_start"),
            "period_end": period.get("period_end"),
            "reason": "percentage_rose_without_local_rows",
        })
    known_total = _sum_decimal(costs_by_model.values())
    period_cost = _decimal_value(period.get("total", {}).get("known_cost"))
    previous_total = max(Decimal("0"), known_total - period_cost)
    spikes = [row for row in window_state.get("spikes", []) if isinstance(row, dict)]
    try:
        spike_share_threshold = Decimal(os.getenv("SPIKE_COST_SHARE_THRESHOLD", str(_SPIKE_SHARE_THRESHOLD)))
        spike_min_cost = Decimal(os.getenv("SPIKE_MIN_COST_USD", str(_SPIKE_MIN_COST)))
    except InvalidOperation:
        spike_share_threshold = _SPIKE_SHARE_THRESHOLD
        spike_min_cost = _SPIKE_MIN_COST
    if (
        period_cost >= spike_min_cost
        and previous_total >= spike_min_cost
        and known_total > 0
        and period_cost / known_total >= spike_share_threshold
    ):
        spike_key = f"{period.get('period_end')}:{period_cost}"
        if not any(str(row.get("key")) == spike_key for row in spikes):
            spikes.append(
                {
                    "key": spike_key,
                    "period_start": period.get("period_start"),
                    "period_end": period.get("period_end"),
                    "cost_usd": str(period_cost),
                    "share_of_observed_cost": str(period_cost * Decimal("100") / known_total),
                    "requests": period.get("total", {}).get("requests", 0),
                    "models": {
                        str(name): {
                            "requests": row.get("requests", 0),
                            "input_tokens": row.get("input_tokens", 0),
                            "cached_input_tokens": row.get("cached_input_tokens", 0),
                            "uncached_input_tokens": row.get("uncached_input_tokens", 0),
                            "output_tokens": row.get("output_tokens", 0),
                            "cost_usd": str(_decimal_value(row.get("known_cost", row.get("estimated_cost")))),
                        }
                        for name, row in period_models.items()
                    },
                }
            )
    current_bucket = int(current_percent)
    last_bucket = int(crossings[-1].get("percent", current_bucket))
    # A +N snapshot is one measured interval, never N independently
    # observed one-point bins, even when some local calls are present.
    unresolved_local_jump = current_bucket - last_bucket > 1 and not period_has_imputed
    if unresolved_local_jump:
        has_uncertain_attribution = True
    if current_bucket > last_bucket:
        crossings.append(
            _crossing_payload(
                current_bucket,
                period["period_end"],
                costs_by_model,
                usage_total,
                usage_by_model,
                reliable=not unobserved_rise,
                skipped_from=last_bucket,
                reason=(
                    "unobserved_percentage_change" if unobserved_rise
                    else "external_usage_imputed"
                    if period_has_imputed
                    else ("unpriced_model_included" if period_has_unpriced
                          else ("multi_point_interval_unresolved" if unresolved_local_jump else None))
                ),
                imputed=period_has_imputed,
            )
        )
        if unresolved_local_jump:
            crossings[-1]["interval_aggregate_only"] = True
        if period_has_unpriced:
            crossings[-1]["has_unpriced"] = True
    new_state = {
        "schema_version": _ACCOUNTING_SCHEMA_VERSION,
        "anchor": anchor,
        "costs_by_model": {name: str(value) for name, value in sorted(costs_by_model.items())},
        "usage_total": usage_total,
        "usage_by_model": {name: usage_by_model[name] for name in sorted(usage_by_model)},
        "models_seen": sorted(models_seen),
        "known_cost": str(known_total),
        "has_unpriced": has_unpriced,
        "has_invalid": has_invalid,
        "has_coverage_gap": has_coverage_gap,
        "has_uncertain_attribution": has_uncertain_attribution,
        "has_imputed": has_imputed,
        "imputed_cost_usd": str(imputed_total),
        "has_unobserved_percentage": has_unobserved_percentage,
        "unobserved_percentage_ranges": unobserved_ranges,
        "has_external_usage_unquantified": has_external_usage_unquantified,
        "crossings": crossings,
        "cycle_history": list(window_state.get("cycle_history", [])),
        "legacy_cycles": list(window_state.get("legacy_cycles", [])),
        "spikes": spikes,
        "last_sample_at": period["period_end"],
    }
    status = None
    reason = None
    if window_state.get("has_external_usage_unquantified"):
        status, reason = "external_usage_unquantified", "outside_headroom_usage_or_account_counter_not_trustworthy"
    elif has_unobserved_percentage:
        status, reason = "unobserved_percentage_change", "percentage_rose_without_local_rows"
    elif has_coverage_gap:
        status, reason = "partial_coverage", "coverage_gap"
    elif has_invalid:
        status, reason = "invalid_counters", "invalid_counters"
    elif has_imputed:
        status, reason = "imputed_external_usage", "external_usage_imputed"
    elif has_unpriced:
        status, reason = "unpriced_model", "unpriced_model"
    estimate = _estimate_window(window_name, new_state, current_window, status=status, reason=reason)
    return new_state, estimate


def update_accounting_state(
    accounting: dict[str, Any] | None,
    previous: Snapshot,
    current: Snapshot,
    period: dict[str, Any],
    pricing: PricingTable,
    cursor: dict[str, Any],
    *,
    weekly_only: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    state = json.loads(json.dumps(accounting)) if isinstance(accounting, dict) else empty_accounting_state()
    # Clear the one-shot migration marker after the baseline has been written.
    # Otherwise every subsequent check would discard the period accounting.
    state.pop("migration_required", None)
    windows = state.get("windows") if isinstance(state.get("windows"), dict) else {}
    if weekly_only:
        # Preserve historic primary data; only the weekly window gets new usage.
        primary_state = windows.get("primary")
        _, primary_estimate = update_window_calibration(
            "primary", None, None, period, primary_state, pricing.revision
        )
    else:
        primary_state, primary_estimate = update_window_calibration(
            "primary", previous.primary, current.primary, period, windows.get("primary"), pricing.revision
        )
    secondary_state, secondary_estimate = update_window_calibration(
        "secondary", previous.secondary, current.secondary, period, windows.get("secondary"), pricing.revision,
        conservative_unobserved=weekly_only,
    )
    legacy_by_window = state.get("legacy_cycles_by_window", {})
    if isinstance(legacy_by_window, dict):
        for name, window_state in (("primary", primary_state), ("secondary", secondary_state)):
            if isinstance(window_state, dict):
                legacy = legacy_by_window.get(name, [])
                if isinstance(legacy, list) and legacy:
                    window_state["legacy_cycles"] = legacy
                    window_state["cycle_history"] = list(window_state.get("cycle_history", [])) + legacy
                    estimate = primary_estimate if name == "primary" else secondary_estimate
                    estimate["cycle_history"] = list(window_state["cycle_history"])
    state.pop("legacy_cycles_by_window", None)
    state.update(
        {
            "schema_version": _ACCOUNTING_SCHEMA_VERSION,
            "pricing_revision": pricing.revision,
            "limit_id": os.getenv("CODEX_LIMIT_ID", "codex"),
            "last_sample_at": current.captured_at,
            "cursor": cursor,
            "windows": {"primary": primary_state, "secondary": secondary_state},
            "recovery": {
                "pending": False,
                "coverage_gap": bool(period.get("diagnostics", {}).get("coverage_gap", False)),
            },
        }
    )
    return state, {"primary": primary_estimate, "secondary": secondary_estimate}


def build_period_id(previous: Snapshot, current: Snapshot, records: Iterable[HeadroomUsageRecord]) -> str:
    identities = [
        f"{record.source_file}:{record.source_offset}:{record.request_id}:{record.timestamp:.6f}"
        for record in records
    ]
    digest = hashlib.sha256("\n".join(identities).encode("utf-8")).hexdigest()[:16]
    return f"{previous.captured_at}-{current.captured_at}-{digest}"


def _attach_applied_rates(
    period: dict[str, Any], pricing: PricingTable, processing_tier: str | None = None
) -> None:
    tier = (
        processing_tier
        or os.getenv("PRICING_PROCESSING_TIER")
        or pricing.processing_tier
        or "standard"
    ).strip().lower()
    for model_name, row in period.get("models", {}).items():
        model = pricing.resolve(model_name)
        if model is None:
            row["applied_rates"] = None
            continue
        row["applied_rates"] = {
            "model": model.model_id,
            "input": str(model.input_rate),
            "output": str(model.output_rate),
            "cached_input": str(model.cached_input_rate),
            "uncached_input": str(model.input_rate),
            # Retained for configuration compatibility; never included in
            # Codex-equivalent cost calculation.
            "legacy_cache_write": str(model.cache_write_rate),
            "legacy_cache_write_billing": "ignored",
            "unit": pricing.unit,
            "currency": pricing.currency,
            "pricing_mode": pricing.pricing_mode,
            "cost_basis": "current_price_equivalent",
            "default_processing_tier": tier,
            "processing_tiers_observed": row.get("processing_tiers", []),
            "long_context_requests": row.get("long_context_requests", 0),
            "long_context_threshold_tokens": model.long_context_threshold_tokens,
            "long_context_input_multiplier": str(model.long_context_input_multiplier),
            "long_context_output_multiplier": str(model.long_context_output_multiplier),
        }


def build_usage_v2(
    previous: Snapshot,
    current: Snapshot,
    period: dict[str, Any],
    estimates: dict[str, Any],
    pricing: PricingTable,
    records: Iterable[HeadroomUsageRecord],
) -> dict[str, Any]:
    return {
        "schema_version": _ACCOUNTING_SCHEMA_VERSION,
        "period_id": build_period_id(previous, current, records),
        "period": {
            "start": dt.datetime.fromtimestamp(previous.captured_at, dt.timezone.utc).isoformat(),
            "end": dt.datetime.fromtimestamp(current.captured_at, dt.timezone.utc).isoformat(),
            "elapsed_seconds": max(0, current.captured_at - previous.captured_at),
        },
        "source": {
            "headroom_log_dir": os.getenv("HEADROOM_LOG_DIR", str(Path.home() / ".headroom" / "logs")),
            "client": os.getenv("HEADROOM_CLIENT", "codex"),
            "timezone": os.getenv("HEADROOM_LOG_TIMEZONE", ""),
            "excluded_models": sorted(headroom_excluded_models()),
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
            "processing_tier": os.getenv("PRICING_PROCESSING_TIER", pricing.processing_tier),
        },
        "diagnostics": period.get("diagnostics", {}),
        "models": period.get("models", {}),
        "total": period.get("total", {}),
        "windows": estimates,
        # The user-facing dollar estimate is the long/weekly window.  Keep
        # both window diagnostics for troubleshooting, but make the intended
        # scope explicit for log consumers.
        "weekly_estimate": estimates.get("secondary"),
    }


def _migrate_accounting_state(accounting: dict[str, Any]) -> dict[str, Any]:
    """Start schema v3 without mixing legacy cache-write estimates into it."""

    migrated = empty_accounting_state()
    legacy_by_window: dict[str, list[dict[str, Any]]] = {}
    old_windows = accounting.get("windows") if isinstance(accounting, dict) else {}
    if isinstance(old_windows, dict):
        for name, old_window in old_windows.items():
            if not isinstance(old_window, dict):
                continue
            anchor = old_window.get("anchor")
            if not isinstance(anchor, dict):
                continue
            legacy_by_window[str(name)] = [
                {
                    "status": "legacy_unrecalculated",
                    "schema_version": accounting.get("schema_version", 2),
                    "reason": "pre_crossing_or_cache_write_pricing",
                    "anchor": anchor,
                    "known_cost_usd": str(_decimal_value(old_window.get("known_cost"))),
                    "cumulative_usage": _cumulative_usage_payload(
                        old_window.get("usage_total"),
                        old_window.get("usage_by_model"),
                        old_window.get("costs_by_model"),
                        _decimal_value(old_window.get("known_cost")),
                    ),
                }
            ]
    migrated["migration_required"] = True
    migrated["migration_from_schema"] = accounting.get("schema_version", 1)
    migrated["legacy_cycles_by_window"] = legacy_by_window
    return migrated


def load_state_bundle(path: Path) -> tuple[Snapshot | None, dict[str, Any]]:
    if not path.exists():
        return None, empty_accounting_state()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("state is not an object")
        snapshot = snapshot_from_dict(data)
        accounting = data.get("accounting")
        if not isinstance(accounting, dict):
            # Existing installations have only snapshot fields.  Treat the
            # first post-upgrade run as a new accounting baseline.
            accounting = empty_accounting_state()
            accounting["migration_required"] = True
        else:
            schema_version = int(accounting.get("schema_version", 2) or 2)
            has_crossings = all(
                isinstance(window, dict) and "crossings" in window
                for window in (accounting.get("windows") or {}).values()
                if window is not None
            )
            if schema_version < _ACCOUNTING_SCHEMA_VERSION or not has_crossings:
                accounting = _migrate_accounting_state(accounting)
            accounting.setdefault("schema_version", _ACCOUNTING_SCHEMA_VERSION)
            accounting.setdefault("cursor", _empty_cursor())
            accounting.setdefault("windows", {"primary": None, "secondary": None})
        return snapshot, accounting
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise MonitorError(f"Could not read state file {path}: {exc}") from exc


def load_state(path: Path) -> Snapshot | None:
    """Backward-compatible snapshot-only state reader."""

    snapshot, _ = load_state_bundle(path)
    return snapshot


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(_json_safe(value), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def save_state(path: Path, snapshot: Snapshot, accounting: dict[str, Any] | None = None) -> None:
    state = snapshot_to_dict(snapshot)
    if accounting is not None:
        state["state_schema_version"] = _ACCOUNTING_SCHEMA_VERSION
        state["accounting"] = accounting
    _atomic_write_json(path, state)


@contextlib.contextmanager
def state_lock(path: Path):
    """Serialize checks and make a manual invocation safe beside systemd."""

    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _pending_path(state_path: Path) -> Path:
    return state_path.with_suffix(state_path.suffix + ".pending")


def _append_usage_line_once(path: Path, line: str, period_id: str | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if period_id and path.exists():
        try:
            existing = path.read_text(encoding="utf-8")
            if f'"period_id":"{period_id}"' in existing or f'"period_id": "{period_id}"' in existing:
                return
        except OSError as exc:
            raise MonitorError(f"Could not inspect usage log {path}: {exc}") from exc
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line.rstrip("\n") + "\n")


def backfill_accounting_usage_from_log(
    accounting: dict[str, Any], usage_log_path: Path | None
) -> bool:
    """Populate canonical cumulative counters for older persisted state."""

    if usage_log_path is None or not usage_log_path.exists():
        return False
    windows = accounting.get("windows")
    if not isinstance(windows, dict):
        return False
    targets = {
        name: window
        for name, window in windows.items()
        if isinstance(window, dict)
        and isinstance(window.get("anchor"), dict)
        and ("usage_total" not in window or "usage_by_model" not in window)
    }
    if not targets:
        return False

    try:
        lines = usage_log_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        LOG.warning("Could not backfill cumulative usage from %s: %s", usage_log_path, exc)
        return False

    last_sample = _number(accounting.get("last_sample_at"))
    parsed_rows: list[dict[str, Any]] = []
    seen_period_ids: set[str] = set()
    for line in lines:
        if "usage_v2=" not in line:
            continue
        try:
            payload = json.loads(line.split("usage_v2=", 1)[1])
        except (IndexError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        period_id = payload.get("period_id")
        if period_id is not None:
            period_key = str(period_id)
            if period_key in seen_period_ids:
                continue
            seen_period_ids.add(period_key)
        period = payload.get("period")
        if not isinstance(period, dict):
            continue
        end_value = period.get("end")
        try:
            period_end = float(end_value)
        except (TypeError, ValueError):
            try:
                period_end = dt.datetime.fromisoformat(str(end_value)).timestamp()
            except (TypeError, ValueError, OverflowError):
                continue
        if last_sample is not None and period_end > last_sample:
            continue
        payload["_period_end_epoch"] = period_end
        parsed_rows.append(payload)

    changed = False
    for window in targets.values():
        anchor = window["anchor"]
        anchor_at = _number(anchor.get("captured_at"))
        usage_total = _usage_counters()
        usage_by_model: dict[str, dict[str, int]] = {}
        for payload in parsed_rows:
            period_end = payload["_period_end_epoch"]
            if anchor_at is None or period_end <= anchor_at:
                continue
            _add_usage_counters(usage_total, payload.get("total"))
            for model_name, row in (payload.get("models") or {}).items():
                model_usage = usage_by_model.setdefault(str(model_name), _usage_counters())
                _add_usage_counters(model_usage, row)
        window["usage_total"] = usage_total
        window["usage_by_model"] = {
            name: usage_by_model[name] for name in sorted(usage_by_model)
        }
        changed = True
    return changed


def recover_pending(state_path: Path, usage_log_path: Path | None) -> None:
    pending_path = _pending_path(state_path)
    if not pending_path.exists():
        return
    try:
        pending = json.loads(pending_path.read_text(encoding="utf-8"))
        if not isinstance(pending, dict):
            raise ValueError("pending commit is not an object")
        line = pending.get("line")
        period_id = pending.get("period_id")
        state = pending.get("state")
        if usage_log_path is not None and isinstance(line, str):
            _append_usage_line_once(usage_log_path, line, str(period_id) if period_id else None)
        if isinstance(state, dict):
            snapshot = snapshot_from_dict(state)
            save_state(state_path, snapshot, state.get("accounting"))
        pending_path.unlink()
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise MonitorError(f"Could not recover pending accounting commit {pending_path}: {exc}") from exc


def events_message(events: Iterable[Event]) -> tuple[str, str]:
    event_list = list(events)
    if len(event_list) == 1:
        return event_list[0].title, event_list[0].body
    subject = f"{len(event_list)} Codex usage changes detected"
    body = "\n\n".join(f"{event.title}\n{event.body}" for event in event_list)
    return subject, body


class EmailNotifier:
    def send(self, subject: str, body: str) -> None:
        host = os.getenv("SMTP_HOST", "smtp.gmail.com")
        port = int(os.getenv("SMTP_PORT", "465"))
        username = os.getenv("SMTP_USERNAME", "")
        password = os.getenv("SMTP_PASSWORD", "")
        sender = os.getenv("EMAIL_FROM", username)
        recipients = [v.strip() for v in os.getenv("EMAIL_TO", "").split(",") if v.strip()]
        if not username or not password or not sender or not recipients:
            raise MonitorError(
                "Email is enabled but SMTP_USERNAME, SMTP_PASSWORD, EMAIL_FROM/SMTP_USERNAME, "
                "or EMAIL_TO is missing"
            )

        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = sender
        message["To"] = ", ".join(recipients)
        message.set_content(body)

        context = ssl.create_default_context()
        if env_bool("SMTP_SSL", True):
            with smtplib.SMTP_SSL(host, port, timeout=30, context=context) as server:
                server.login(username, password)
                server.send_message(message)
        else:
            with smtplib.SMTP(host, port, timeout=30) as server:
                server.ehlo()
                if env_bool("SMTP_STARTTLS", True):
                    server.starttls(context=context)
                    server.ehlo()
                server.login(username, password)
                server.send_message(message)


class NtfyNotifier:
    """Send through an ntfy server (public ntfy.sh or self-hosted).

    Uses standard urllib so the project stays dependency-free.
    """

    def send(self, subject: str, body: str) -> None:
        base = os.getenv("NTFY_URL", "https://ntfy.sh").rstrip("/")
        topic = os.getenv("NTFY_TOPIC", "")
        token = os.getenv("NTFY_TOKEN", "")
        if not topic:
            raise MonitorError(
                "ntfy is enabled but NTFY_TOPIC is missing"
            )

        url = f"{base}/{topic}"
        headers: dict[str, str] = {
            "Title": subject,
            "Priority": os.getenv("NTFY_PRIORITY", "default"),
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if tags := os.getenv("NTFY_TAGS"):
            headers["Tags"] = tags

        request = urllib.request.Request(
            url,
            data=body.encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                if response.status >= 300:
                    raise MonitorError(f"ntfy returned HTTP {response.status}")
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            raise MonitorError(f"ntfy returned HTTP {exc.code}: {error_body}") from exc
        except urllib.error.URLError as exc:
            raise MonitorError(f"Could not contact ntfy: {exc.reason}") from exc


class WhatsAppNotifier:
    """Send through Meta WhatsApp Cloud API.

    For proactive alerts, use an approved template with two body variables: title and body.
    """

    def send(self, subject: str, body: str) -> None:
        api_url = os.getenv("WHATSAPP_API_URL", "")
        token = os.getenv("WHATSAPP_ACCESS_TOKEN", "")
        recipient = os.getenv("WHATSAPP_TO", "")
        if not api_url or not token or not recipient:
            raise MonitorError(
                "WhatsApp is enabled but WHATSAPP_API_URL, WHATSAPP_ACCESS_TOKEN, or WHATSAPP_TO is missing"
            )

        template_name = os.getenv("WHATSAPP_TEMPLATE_NAME", "codex_reset_alert").strip()
        language = os.getenv("WHATSAPP_TEMPLATE_LANGUAGE", "en_US")
        if template_name:
            payload: dict[str, Any] = {
                "messaging_product": "whatsapp",
                "to": recipient,
                "type": "template",
                "template": {
                    "name": template_name,
                    "language": {"code": language},
                    "components": [
                        {
                            "type": "body",
                            "parameters": [
                                {"type": "text", "text": subject[:1024]},
                                {"type": "text", "text": body[:1024]},
                            ],
                        }
                    ],
                },
            }
        else:
            payload = {
                "messaging_product": "whatsapp",
                "to": recipient,
                "type": "text",
                "text": {"body": f"{subject}\n\n{body}"[:4096]},
            }

        request = urllib.request.Request(
            api_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                if response.status >= 300:
                    raise MonitorError(f"WhatsApp API returned HTTP {response.status}")
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            raise MonitorError(f"WhatsApp API returned HTTP {exc.code}: {error_body}") from exc
        except urllib.error.URLError as exc:
            raise MonitorError(f"Could not contact WhatsApp API: {exc.reason}") from exc


def configured_notifiers() -> list[tuple[str, Any]]:
    channels = [v.strip().lower() for v in os.getenv("NOTIFY_CHANNELS", "email").split(",") if v.strip()]
    result: list[tuple[str, Any]] = []
    for channel in channels:
        if channel == "email":
            result.append((channel, EmailNotifier()))
        elif channel == "whatsapp":
            result.append((channel, WhatsAppNotifier()))
        elif channel == "ntfy":
            result.append((channel, NtfyNotifier()))
        elif channel in {"stdout", "console"}:
            result.append((channel, None))
        else:
            raise MonitorError(f"Unknown notification channel: {channel}")
    return result


def send_notifications(subject: str, body: str) -> bool:
    successes = 0
    failures: list[str] = []
    notifiers = configured_notifiers()
    if not notifiers:
        raise MonitorError("No notification channel configured")
    for name, notifier in notifiers:
        try:
            if notifier is None:
                print(f"{subject}\n\n{body}")
            else:
                notifier.send(subject, body)
            successes += 1
            LOG.info("Notification sent through %s", name)
        except Exception as exc:  # Keep other configured channels working.
            failures.append(f"{name}: {exc}")
            LOG.error("Notification through %s failed: %s", name, exc)
    if successes == 0:
        raise MonitorError("All notification channels failed: " + "; ".join(failures))
    return True


def _external_evidence_enabled() -> bool:
    raw = str(os.getenv("EXTERNAL_EVIDENCE", "1")).strip().lower()
    return raw not in {"0", "false", "no", "off", "disabled"}


# The account lifetime counter batch-updates (observed frozen for hours while
# local usage burned millions of tokens), so per-sample deltas need guards:
# a delta smaller than this VM's own spend proves staleness, and a delta far
# beyond any plausible sustained rate proves a batch catch-up covering a much
# longer span than the current period.  Both cases must refuse attribution
# instead of reporting a false zero or a false spike.
_EXTERNAL_EVIDENCE_MAX_TOKENS_PER_SEC = 100_000.0


def _account_lifetime_tokens(payload: dict[str, Any] | None) -> int | None:
    """Account-wide lifetime token counter from ``account/usage/read``.

    This counter covers every surface and machine on the account.  The
    endpoint exposes no per-model split and no call/request counts, so
    call-count comparison against local Headroom requests is impossible
    from CLI data alone.
    """
    if not isinstance(payload, dict):
        return None
    summary = payload.get("summary")
    if not isinstance(summary, dict):
        return None
    try:
        value = int(summary.get("lifetimeTokens"))
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _account_daily_buckets(payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    buckets = payload.get("dailyUsageBuckets")
    if not isinstance(buckets, list):
        return []
    cleaned: list[dict[str, Any]] = []
    for row in buckets:
        if not isinstance(row, dict):
            continue
        try:
            tokens = int(row.get("tokens", 0))
        except (TypeError, ValueError):
            continue
        cleaned.append({"startDate": str(row.get("startDate", "")), "tokens": max(0, tokens)})
    return cleaned


def update_external_evidence(
    previous_evidence: dict[str, Any] | None,
    account_usage: dict[str, Any] | None,
    period: dict[str, Any],
    previous_captured_at: float,
    current_captured_at: float,
) -> dict[str, Any]:
    """Parallel outside-VM evidence; gates weekly projections but not cost math.

    Compares the account-wide ``lifetimeTokens`` delta between two samples
    with the local Headroom token sum for the same wall-clock window.  Any
    positive remainder is spend from another machine, the web app, or the
    phone.  Raw observations are preserved; interpretation stays with the
    reader because the exact token accounting of ``lifetimeTokens``
    (cached/reasoning inclusion) is undocumented, so small diffs are noise.
    The counter batch-updates (observed frozen for hours): a delta below the
    local spend marks the sample ``stale_counter`` and a supra-physical delta
    marks it ``counter_catchup``; both refuse attribution instead of logging
    a false zero or a false spike.
    """
    total = period.get("total", {}) if isinstance(period, dict) else {}
    try:
        local_requests = max(0, int(total.get("requests", 0)))
    except (TypeError, ValueError):
        local_requests = 0
    try:
        local_tokens = max(0, int(total.get("input_tokens", 0))) + max(
            0, int(total.get("output_tokens", 0))
        )
    except (TypeError, ValueError):
        local_tokens = 0
    diagnostics = period.get("diagnostics", {}) if isinstance(period, dict) else {}
    coverage_gap = bool(diagnostics.get("coverage_gap", False)) if isinstance(diagnostics, dict) else False

    base: dict[str, Any] = {
        "available": False,
        "account_calls_available": False,
        "account_lifetime_tokens": None,
        "account_lifetime_delta": None,
        "local_period_tokens": local_tokens,
        "local_period_requests": local_requests,
        "external_tokens_estimate": None,
        "external_detected": False,
        "account_daily_buckets": _account_daily_buckets(account_usage),
        "coverage_gap": coverage_gap,
        "period_start": previous_captured_at,
        "period_end": current_captured_at,
        "reason": "",
    }
    if not _external_evidence_enabled():
        base["reason"] = "disabled"
        return base
    lifetime = _account_lifetime_tokens(account_usage)
    if account_usage is None:
        base["reason"] = "account_usage_unavailable"
        return base
    if lifetime is None:
        base["reason"] = "lifetime_counter_missing"
        return base
    base["account_lifetime_tokens"] = lifetime
    previous_lifetime: int | None = None
    if isinstance(previous_evidence, dict):
        try:
            previous_lifetime = int(previous_evidence.get("account_lifetime_tokens"))
        except (TypeError, ValueError):
            previous_lifetime = None
    if previous_lifetime is None:
        base["available"] = True
        base["reason"] = "awaiting_next_sample"
        return base
    delta = lifetime - previous_lifetime
    if delta < 0:
        base["reason"] = "lifetime_counter_decreased"
        return base
    base["available"] = True
    base["account_lifetime_delta"] = delta
    elapsed = max(0.0, float(current_captured_at) - float(previous_captured_at))
    if delta < local_tokens:
        # The account-wide counter cannot grow by less than this VM alone
        # spent (local usage is a subset of account usage).  The counter is
        # stale; reporting external=0 here would falsely claim no off-VM
        # spend, so attribution is refused while raw numbers are preserved.
        base["external_tokens_estimate"] = None
        base["external_detected"] = False
        base["reason"] = "stale_counter"
        return base
    if elapsed > 0 and delta > elapsed * _EXTERNAL_EVIDENCE_MAX_TOKENS_PER_SEC:
        # Batch catch-up: the counter absorbed a much longer span than this
        # period (e.g. a whole day landing at once).  Attributing it to this
        # 10-minute window would fabricate a giant external spike.
        base["external_tokens_estimate"] = None
        base["external_detected"] = False
        base["reason"] = "counter_catchup"
        return base
    external = max(0, delta - local_tokens)
    base["external_tokens_estimate"] = external
    base["external_detected"] = external > 0
    if coverage_gap:
        base["reason"] = "coverage_gap_local_sum_incomplete"
    else:
        base["reason"] = "ok"
    return base


def query_snapshot() -> Snapshot:
    command = os.getenv("CODEX_COMMAND", "codex app-server")
    timeout_seconds = float(os.getenv("CODEX_TIMEOUT_SECONDS", "25"))
    with CodexAppServerClient(command, timeout_seconds) as client:
        payload = client.read_rate_limits()
    return snapshot_from_payload(
        payload, limit_id=os.getenv("CODEX_LIMIT_ID", "codex").strip() or "codex"
    )


def query_snapshot_and_account_usage() -> tuple[Snapshot, dict[str, Any] | None]:
    """Rate-limit snapshot plus best-effort account-wide usage counters.

    The usage read shares one app-server session with the rate-limit read.
    A usage failure never fails the run; the parallel evidence simply
    records itself unavailable while the main accounting cycle proceeds.
    """
    command = os.getenv("CODEX_COMMAND", "codex app-server")
    timeout_seconds = float(os.getenv("CODEX_TIMEOUT_SECONDS", "25"))
    limit_id = os.getenv("CODEX_LIMIT_ID", "codex").strip() or "codex"
    account_usage: dict[str, Any] | None = None
    with CodexAppServerClient(command, timeout_seconds) as client:
        rate_payload = client.read_rate_limits()
        if _external_evidence_enabled():
            try:
                maybe_usage = client.read_account_usage()
            except MonitorError as exc:
                LOG.warning("account usage read failed; external evidence unavailable: %s", exc)
                maybe_usage = None
            account_usage = maybe_usage if isinstance(maybe_usage, dict) else None
    return snapshot_from_payload(rate_payload, limit_id=limit_id), account_usage


def weekly_checkpoint_due(
    previous: Snapshot | None,
    current: Snapshot,
    accounting: dict[str, Any],
    max_seconds: int = 600,
) -> bool:
    """Checkpoint weekly changes and bound loss of calls in unchanged buckets."""
    if previous is None or accounting.get("migration_required"):
        return True
    old, new = previous.secondary, current.secondary
    if old is None or new is None or old.used_percent is None or new.used_percent is None:
        return True
    # The API may move resets_at without a quota reset. The percentage drop
    # detects an actual observed reset; the periodic checkpoint bounds the rest.
    if (old.used_percent != new.used_percent
            or old.window_duration_mins != new.window_duration_mins):
        return True
    last = accounting.get("last_sample_at")
    try:
        return current.captured_at - float(last) >= max_seconds
    except (ValueError, TypeError):
        return True


def query_weekly_probe(
    previous: Snapshot | None, accounting: dict[str, Any]
) -> tuple[Snapshot, dict[str, Any] | None, bool]:
    """Use one app-server session, reading account usage only at checkpoints."""
    command = os.getenv("CODEX_COMMAND", "codex app-server")
    timeout_seconds = float(os.getenv("CODEX_TIMEOUT_SECONDS", "25"))
    limit_id = os.getenv("CODEX_LIMIT_ID", "codex").strip() or "codex"
    try:
        max_seconds = max(60, int(os.getenv("WEEKLY_MAX_CHECKPOINT_SECONDS", "600")))
    except ValueError:
        max_seconds = 600
    account_usage: dict[str, Any] | None = None
    with CodexAppServerClient(command, timeout_seconds) as client:
        current = snapshot_from_payload(client.read_rate_limits(), limit_id=limit_id)
        due = weekly_checkpoint_due(previous, current, accounting, max_seconds)
        if due and _external_evidence_enabled():
            try:
                maybe_usage = client.read_account_usage()
            except MonitorError as exc:
                LOG.warning("account usage read failed; external evidence unavailable: %s", exc)
                maybe_usage = None
            account_usage = maybe_usage if isinstance(maybe_usage, dict) else None
    return current, account_usage, due


def _external_usage_evidence_is_unquantified(evidence: Any) -> bool:
    """True when a sample cannot separate account usage from local Headroom."""
    if not isinstance(evidence, dict):
        return True
    reason = str(evidence.get("reason", ""))
    if reason == "awaiting_next_sample":
        return False  # Baseline only; it describes no elapsed usage interval.
    if reason != "ok":
        return True
    try:
        external_tokens = int(evidence.get("external_tokens_estimate"))
    except (TypeError, ValueError):
        return True
    return external_tokens > 0 or bool(evidence.get("external_detected", False))


def _mark_weekly_external_usage_unquantified(
    accounting_new: dict[str, Any],
    estimates: dict[str, Any],
    usage_v2: dict[str, Any],
    current_window: LimitWindow | None,
    evidence_samples: list[Any],
) -> None:
    """Withhold normalized $/100 estimates when local points are unmeasured."""
    if current_window is None or not any(
        _external_usage_evidence_is_unquantified(item) for item in evidence_samples
    ):
        return
    windows = accounting_new.get("windows", {})
    window_state = windows.get("secondary") if isinstance(windows, dict) else None
    if not isinstance(window_state, dict):
        return
    window_state["has_external_usage_unquantified"] = True
    window_state["has_uncertain_attribution"] = True
    estimate = _estimate_window(
        "secondary",
        window_state,
        current_window,
        status="external_usage_unquantified",
        reason="outside_headroom_usage_or_account_counter_not_trustworthy",
    )
    estimates["secondary"] = estimate
    usage_v2.setdefault("windows", {})["secondary"] = estimate
    usage_v2["weekly_estimate"] = estimate


def _attach_external_evidence(
    accounting: dict[str, Any],
    accounting_new: dict[str, Any],
    usage_v2: dict[str, Any],
    account_usage: dict[str, Any] | None,
    period: dict[str, Any],
    previous_captured_at: float,
    current_captured_at: float,
) -> None:
    """Attach the parallel outside-VM evidence stream.

    Only the ``external_evidence`` accounting key and the additive
    ``usage_v2["external_evidence"]`` section are written.  Window states,
    anchors, crossings, and estimates are never touched here.
    """
    previous_evidence = accounting.get("external_evidence") if isinstance(accounting, dict) else None
    accounting_new["external_evidence"] = update_external_evidence(
        previous_evidence if isinstance(previous_evidence, dict) else None,
        account_usage,
        period,
        previous_captured_at,
        current_captured_at,
    )
    usage_v2["external_evidence"] = accounting_new["external_evidence"]


def run_once(
    state_path: Path, usage_log_path: Path | None = None, *, weekly_probe: bool = False
) -> int:
    with state_lock(state_path):
        recover_pending(state_path, usage_log_path)
        previous, accounting = load_state_bundle(state_path)
        backfill_accounting_usage_from_log(accounting, usage_log_path)
        if weekly_probe:
            current, account_usage, due = query_weekly_probe(previous, accounting)
            if not due:
                return 0
        else:
            current, account_usage = query_snapshot_and_account_usage()
        pricing_path = Path(
            os.getenv("MODEL_PRICING_FILE", "").strip() or default_pricing_path()
        ).expanduser()
        try:
            pricing = load_model_pricing(pricing_path)
            pricing_error: str | None = None
        except MonitorError as exc:
            pricing = unavailable_pricing(str(exc), pricing_path)
            pricing_error = str(exc)
            LOG.error("Pricing unavailable; token totals will be retained: %s", exc)

        headroom_dir = Path(
            os.getenv("HEADROOM_LOG_DIR", str(Path.home() / ".headroom" / "logs"))
        ).expanduser()
        headroom_client = os.getenv("HEADROOM_CLIENT", "codex").strip() or None
        timezone_name = os.getenv("HEADROOM_LOG_TIMEZONE", "").strip() or None
        limit_id = os.getenv("CODEX_LIMIT_ID", "codex").strip() or "codex"

        # A missing accounting section is an upgrade baseline.  Keep reset
        # notifications based on the old snapshot, while avoiding attribution of
        # retained Headroom history to a period whose start is unknown.
        needs_baseline = (
            previous is None
            or accounting.get("migration_required")
            or accounting.get("last_sample_at") is None
            or (accounting.get("limit_id") or "codex") != limit_id
        )
        if needs_baseline:
            accounting["limit_id"] = limit_id
            cursor, diagnostics = prime_headroom_cursor(
                headroom_dir, accounting.get("cursor") if isinstance(accounting, dict) else None
            )
            diagnostics["baseline"] = True
            baseline_period = aggregate_period(
                (), pricing, current.captured_at, current.captured_at, diagnostics
            )
            if pricing_error:
                baseline_period["diagnostics"]["pricing_error"] = pricing_error
            accounting_new, estimates = update_accounting_state(
                accounting, current, current, baseline_period, pricing, cursor,
                weekly_only=weekly_probe,
            )
            usage_v2 = build_usage_v2(
                current, current, baseline_period, estimates, pricing, ()
            )
            usage_v2["diagnostics"]["baseline"] = True
            accounting_new["last_period_id"] = usage_v2["period_id"]
            _attach_external_evidence(
                accounting,
                accounting_new,
                usage_v2,
                account_usage,
                baseline_period,
                float(current.captured_at),
                float(current.captured_at),
            )
            state = snapshot_to_dict(current)
            state.update({"state_schema_version": _ACCOUNTING_SCHEMA_VERSION, "accounting": accounting_new})
            line = format_usage_line(current, usage_v2)
            pending = {
                "period_id": usage_v2["period_id"],
                "line": line,
                "state": state,
            }
            _atomic_write_json(_pending_path(state_path), pending)
            if usage_log_path is not None:
                _append_usage_line_once(usage_log_path, line, usage_v2["period_id"])
            save_state(state_path, current, accounting_new)
            _pending_path(state_path).unlink(missing_ok=True)
            if previous is None:
                LOG.info("Baseline saved; no notification is sent on the first run")
            else:
                LOG.info("Accounting baseline saved after state migration")
                events = detect_events(previous, current)
                if events:
                    subject, body = events_message(events)
                    try:
                        send_notifications(subject, body)
                    except Exception as exc:
                        LOG.error("Notification delivery failed after commit: %s", exc)
            return 0

        period_start = float(previous.captured_at)
        period_end = float(current.captured_at)
        records, cursor, diagnostics = collect_headroom_usage(
            headroom_dir,
            period_start,
            period_end,
            accounting.get("cursor"),
            client=headroom_client,
            timezone_name=timezone_name,
        )
        period = aggregate_period(
            records,
            pricing,
            period_start,
            period_end,
            diagnostics,
            inferred_cache_write_mode=os.getenv("INFERRED_CACHE_WRITE_MODE", "estimate"),
            processing_tier=os.getenv("PRICING_PROCESSING_TIER", pricing.processing_tier),
        )
        if pricing_error:
            period["diagnostics"]["pricing_error"] = pricing_error
        _attach_applied_rates(
            period,
            pricing,
            os.getenv("PRICING_PROCESSING_TIER", pricing.processing_tier),
        )
        accounting_new, estimates = update_accounting_state(
            accounting, previous, current, period, pricing, cursor,
            weekly_only=weekly_probe,
        )
        usage_v2 = build_usage_v2(previous, current, period, estimates, pricing, records)
        accounting_new["last_period_id"] = usage_v2["period_id"]
        _attach_external_evidence(
            accounting,
            accounting_new,
            usage_v2,
            account_usage,
            period,
            float(previous.captured_at),
            float(current.captured_at),
        )
        if weekly_probe:
            _mark_weekly_external_usage_unquantified(
                accounting_new,
                estimates,
                usage_v2,
                current.secondary,
                [
                    accounting.get("external_evidence"),
                    accounting_new.get("external_evidence"),
                ],
            )
        state = snapshot_to_dict(current)
        state.update({"state_schema_version": _ACCOUNTING_SCHEMA_VERSION, "accounting": accounting_new})
        line = format_usage_line(current, usage_v2)

        # The pending record is durable before either side effect.  If a process
        # dies after the append, the next run sees the same period ID and avoids
        # writing a duplicate line; if append fails, the old cursor remains in
        # state and recovery retries the exact line.
        pending = {"period_id": usage_v2["period_id"], "line": line, "state": state}
        _atomic_write_json(_pending_path(state_path), pending)
        if usage_log_path is not None:
            _append_usage_line_once(usage_log_path, line, usage_v2["period_id"])
        save_state(state_path, current, accounting_new)
        _pending_path(state_path).unlink(missing_ok=True)

        events = detect_events(previous, current)
        if events:
            subject, body = events_message(events)
            try:
                send_notifications(subject, body)
            except Exception as exc:
                LOG.error("Notification delivery failed after accounting commit: %s", exc)
            LOG.info("Detected %d event(s)", len(events))
        else:
            LOG.info(
                "No Codex reset or credit increase detected; period requests=%d cost=%s",
                period["total"]["requests"],
                period["total"].get("estimated_cost"),
            )
        return 0


def default_state_path() -> Path:
    state_home = Path(os.getenv("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return state_home / "codex-reset-monitor" / "state.json"


def default_usage_log_path() -> Path:
    state_home = Path(os.getenv("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return state_home / "codex-reset-monitor" / "usage.log"


def default_env_path() -> Path:
    config_home = Path(os.getenv("XDG_CONFIG_HOME", Path.home() / ".config"))
    return config_home / "codex-reset-monitor" / "config.env"


def format_usage_line(snapshot: Snapshot, usage_v2: dict[str, Any] | None = None) -> str:
    p = snapshot.primary.used_percent if snapshot.primary else None
    s = snapshot.secondary.used_percent if snapshot.secondary else None
    line = (
        f"{format_timestamp(snapshot.captured_at)} "
        f"primary={p if p is not None else '-'}% "
        f"secondary={s if s is not None else '-'}% "
        f"balance={snapshot.credit_balance or '-'}"
    )
    if usage_v2 is not None:
        weekly = usage_v2.get("weekly_estimate") or {}
        weekly_estimate = weekly.get("account_total_100_usd", weekly.get("full_window_usd", "-"))
        weekly_local_estimate = weekly.get("local_token_backed_100_usd", weekly.get("full_window_usd", "-"))
        weekly_external_status = weekly.get("external_usage_status", "not_assessed")
        weekly_confidence = weekly.get("confidence", "Very low")
        full = weekly.get("full_observed_estimate") or {}
        basis = full.get("calculation_basis", "unavailable")
        line += (
            f" weekly_100pct_usd={weekly_estimate or '-'}"
            f" weekly_local_100pct_usd={weekly_local_estimate or '-'}"
            f" weekly_external_usage_status={weekly_external_status}"
            f" weekly_100pct_basis={basis}"
            f" weekly_100pct_counted_points={weekly.get('counted_percentage_points', 0)}"
            f" weekly_100pct_locally_attributed_points={weekly.get('locally_attributed_percentage_points') if weekly.get('locally_attributed_percentage_points') is not None else '-'}"
            f" weekly_100pct_excluded_no_token_points={weekly.get('excluded_no_token_percentage_points', 0)}"
            f" weekly_100pct_confidence={weekly_confidence.replace(' ', '_')}"
        )
        compact = json.dumps(_json_safe(usage_v2), separators=(",", ":"), sort_keys=True)
        line += f" usage_v2={compact}"
    return line


def log_usage(path: Path, snapshot: Snapshot, usage_v2: dict[str, Any] | None = None) -> None:
    """Append the legacy snapshot prefix and, when supplied, the v2 period JSON."""

    line = format_usage_line(snapshot, usage_v2)
    period_id = usage_v2.get("period_id") if isinstance(usage_v2, dict) else None
    _append_usage_line_once(path, line, str(period_id) if period_id else None)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=default_env_path())
    parser.add_argument("--state-path", type=Path, default=default_state_path())
    parser.add_argument("--usage-log", type=Path, default=default_usage_log_path())
    parser.add_argument("--once", action="store_true", help="Check once (default)")
    parser.add_argument("--weekly-probe", action="store_true", help="Poll weekly only; account on changes or a bounded checkpoint")
    parser.add_argument("--loop", action="store_true", help="Run continuously")
    parser.add_argument("--print-status", action="store_true", help="Print current normalized status")
    parser.add_argument("--test-notification", action="store_true", help="Send a test notification")
    parser.add_argument("--verbose", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        load_env_file(args.env_file)
        if args.test_notification:
            send_notifications(
                "Codex reset monitor test",
                "Your Codex reset monitor notification is configured correctly.",
            )
            return 0
        if args.print_status:
            print(json.dumps(snapshot_to_dict(query_snapshot()), indent=2, sort_keys=True))
            return 0
        if args.loop:
            interval = max(60, int(os.getenv("CHECK_INTERVAL_SECONDS", "600")))
            while True:
                try:
                    run_once(args.state_path, args.usage_log, weekly_probe=args.weekly_probe)
                except Exception as exc:
                    LOG.error("Check failed: %s", exc)
                time.sleep(interval)
        return run_once(args.state_path, args.usage_log, weekly_probe=args.weekly_probe)
    except KeyboardInterrupt:
        return 130
    except MonitorError as exc:
        LOG.error("%s", exc)
        return 2
    except Exception:
        LOG.exception("Unexpected failure")
        return 3


if __name__ == "__main__":
    sys.exit(main())
