# Task: estimate Codex window value from Headroom usage

Status: implemented. Pricing checked on 2026-09-08; verification cadence is ten minutes.

At each monitor check, collect Headroom usage since the last successful check,
calculate token costs for each model, and estimate the dollar value represented
by a full Codex allowance window. Append the token counts and estimates to the
existing application usage log.

## Scope and interpretation

- A **period** is the elapsed time between successful monitor samples, normally
  ten minutes. It can be longer after downtime.
- A **window** is one account allowance: primary/short or secondary/weekly.
  Calculate estimates for both independently.
- “100%” means the estimated API-equivalent dollar value of an entire allowance,
  including the part already consumed. It is not the subscription bill or a
  forecast based on time remaining.
- Treat “cached in” as prompt-cache reads and “cached out” as prompt-cache writes.
  A cached response served locally by Headroom is a different event and has no
  new upstream token cost.
- The application currently uses `config.env` for settings and has no settings
  UI. Add a pricing JSON file next to that configuration, with its path exposed
  through `config.env`.

## Findings that determine the implementation

The current integration points are `run_once`, `query_snapshot`, `log_usage`,
`load_state`, and `save_state` in `monitor.py`. `usage.log` already contains one
text line per check with primary/secondary percentages and credit balance.
The state currently stores only the last account snapshot. `install.sh` installs
a single Python file and preserves existing configuration files.

Read-only inspection of `codex-logs-bundle.zip` and the installed Headroom source
established the following:

- Live files are `~/.headroom/logs/proxy.log` and numbered rotations. The export
  contains 19,671 `PERF` records, including 19,020 marked `client=codex`, from
  September 1–8. Other clients and providers share these files.
- `PERF` exposes `model`, `tok_before`, `tok_after`, `cache_read`, `cache_write`,
  `tok_out`, and usually `client`; local response hits have `cached=1`.
- `savings_events.jsonl` records compression savings, without the required
  output/cache breakdown. `proxy_savings.json` contains cumulative statistics.
  Neither is an appropriate request-by-request billing source.
- Headroom's current WebSocket handler derives its per-turn input/output counts
  from upstream `response.completed` usage. It infers cache writes as
  `max(input_tokens - cached_tokens, 0)` and does not consume the newer explicit
  write counter in that handler.
- `tok_after` can be a local estimate in other handlers. Preserve the source and
  quality of each count; do not call every `PERF` input count authoritative.
- A bracketed `hr_...` identifier can identify a whole WebSocket connection.
  The export has 599 identifiers with multiple `PERF` rows. Deduplicating solely
  by this ID would discard real usage.

Source locations inspected: `/mnt/storage/headroom/headroom/proxy/outcome.py`
(`RequestOutcome`, `emit_request_outcome`) and
`/mnt/storage/headroom/headroom/proxy/handlers/openai.py`
(`_extract_responses_usage`, `_record_ws_response_metrics`). The installed
Headroom package points to this checkout. Recheck these contracts when coding;
the monitor should parse logs without importing Headroom.

## 1. Add editable model pricing

Ship `model-pricing.json.example`, install it as
`~/.config/codex-reset-monitor/model-pricing.json` only when absent, and expose
`MODEL_PRICING_FILE` with the corresponding XDG-aware default. Keep the example
file as the single shipped source of defaults; a missing or invalid installed
file must produce a clear pricing status rather than silently use zero rates.

Default prices in USD per 1,000,000 tokens, using Standard processing and
requests within the short-context threshold:

| Model | Input | Output | Cached input/read | Cache write |
| --- | ---: | ---: | ---: | ---: |
| [GPT-5.5](https://developers.openai.com/api/docs/models/gpt-5.5) | 5.00 | 30.00 | 0.50 | 5.00* |
| [GPT-5.6 Luna](https://developers.openai.com/api/docs/models/gpt-5.6-luna) | 0.20 | 1.20 | 0.02 | 0.25 |
| [GPT-5.6 Terra](https://developers.openai.com/api/docs/models/gpt-5.6-terra) | 2.00 | 12.00 | 0.20 | 2.50 |
| [GPT-5.6 Sol](https://developers.openai.com/api/docs/models/gpt-5.6-sol) | 4.00 | 20.00 | 0.40 | 5.00 |
| [GPT-6 Astra](https://developers.openai.com/api/docs/models/gpt-6-astra) | 10.00 | 50.00 | 1.00 | 12.50 |
| [GPT-6 Sol](https://developers.openai.com/api/docs/models/gpt-6-sol) | 2.00 | 10.00 | 0.20 | 2.50 |
| [GPT-6 Luna](https://developers.openai.com/api/docs/models/gpt-6-luna) | 0.10 | 0.50 | 0.01 | 0.125 |

*GPT-5.5 has no additional cache-write charge: an inferred write is priced as
ordinary input, once. GPT-5.6 and later charge writes at 1.25 times ordinary
input. These are total category rates, not surcharges to add to the input rate.
See [OpenAI prompt caching](https://developers.openai.com/api/docs/guides/prompt-caching).

Store decimal strings, currency, units, verification date, source URLs, model
IDs, and an explicit alias map. Include the short names `luna`, `terra`, `sol`,
and `astra`, plus the documented `gpt-5.6` alias for Sol. Allow additional exact
aliases, such as `gpt-5-6-terra` and `gpt-6.0-luna`, through settings; do not use
fuzzy matching.

Read and validate the pricing file at every iteration, including `--loop`.
Reject negative, non-finite, malformed, and missing required rates. Unknown
models retain token counts and have an unavailable cost with a reason.
Record the applied rates/revision in each log entry; updates take effect on the
next check without rewriting history. Reset estimation anchors when the pricing
revision changes so accumulated samples use a consistent price table.

Include per-model long-context rules and a configurable processing multiplier.
For GPT-5.6 and Astra, requests above 272,000 input tokens use 2x input/read/write
rates and 1.5x output rates; apply this per request before aggregation. Standard
is the default; Fast/priority is 2x for these models. Record a configured tier as
an assumption when the log does not expose the actual tier. The GPT-5.5 page
describes session-wide long-context pricing; preserve a session marker where
available and label an estimate incomplete when that scope cannot be recovered.
Sol's current promotional rate lasts at least through November 21, 2026; store
that date as a reminder to recheck, not an invented future replacement price.
See [OpenAI pricing](https://developers.openai.com/api/docs/pricing).

## 2. Read the period's Headroom usage

Add settings for `HEADROOM_LOG_DIR` (default `~/.headroom/logs`),
`HEADROOM_LOG_TIMEZONE` (default the proxy host's local timezone),
`HEADROOM_CLIENT` (default `codex`), and the monitored Codex limit group.
Normalize timestamps to UTC and use `(previous_sample, current_sample]`.
Select completed usage records, not request-start or compression-only messages.

Normalize each record into timestamp, model, source identity, client, input
total, output total, cache reads, inferred/reported cache writes, and quality
flags. Preserve token totals after compression. `tok_before`, `tok_saved`, and
savings dollar fields are diagnostics, not extra consumed tokens. Treat
`tok_out` as total output; do not add reasoning tokens again if included.

Filter traffic to the monitored account/limit scope as far as the source permits.
The `codex` client flag alone does not prove which account or authentication mode
paid for a request. Use available connection metadata for OAuth/API-key filtering;
if account identity is absent, document the single-account source assumption in
the estimate. Exclude other clients/providers from allowance calibration. Keep
unsupported Codex models visible as unpriced or unmapped usage, which invalidates
a complete window estimate rather than making that usage disappear.

Persist file identity/generation, byte offsets, and small boundary metadata.
Read only newly appended data in normal operation; handle renamed rotations,
copy/truncate, and incomplete final lines. Count each distinct completed row,
including repeated WebSocket IDs. Use file-position identity for normal reads
and overlap reconciliation for copied rotations, preserving repeated legitimate
rows rather than deduplicating an entire connection or identical token totals.

First enablement establishes the sampling baseline and cursor without attributing
old history to the first ten-minute period. Later restarts catch up from the
saved cursor while retained files permit. Report a coverage gap if history has
rotated away. Late rows must remain visible as late usage and must not be silently
paired with an unrelated percentage delta. Retain boundary rows for reconciliation
and invalidate affected calibration spans when precise alignment is unavailable.

## 3. Price tokens without counting cache categories twice

For a record with inclusive input `I`, cached reads `R`, writes `W`, and total
output `O`, derive mutually exclusive ordinary input `U = I - R - W`.
The cost is:

```text
cost_usd = (U * input_rate
          + R * cached_input_rate
          + W * cache_write_rate
          + O * output_rate) / 1_000_000
```

Validate `0 <= R + W <= I`. Do not conceal inconsistent counters by silently
clamping them into an apparently complete estimate. Preserve known counts and
mark the affected cost incomplete. Missing counters differ from a genuine zero.

Support a setting `INFERRED_CACHE_WRITE_MODE` with two explicit choices:

- `estimate` (default): use Headroom's inferred `W = I - R`, price it once at
  the configured write rate, and label the write count/cost as inferred. This
  assumes all non-cached input is written; the current logs cannot prove that.
- `ordinary_input`: keep the inferred write count as a diagnostic and price
  the non-cached tokens at ordinary input rates. Label the missing write premium
  as unmeasured for models that charge one.

Prefer explicit upstream write counts when the source actually reports them.
Do not infer reporting quality from the mere presence of `cache_write=` in a
`PERF` line. Count Headroom response-cache hits separately and assign zero new
upstream cost. Record raw input/output totals and the disjoint priced categories
per model, plus period totals. Use `Decimal` and round only for display.

## 4. Estimate each allowance window's 100% value

Maintain a separate calibration anchor and accumulated cost per model for each
limit group and each primary/secondary window. Anchors contain sample time,
starting used percentage, reset timestamp, duration, and pricing revision.

For a valid observed span:

```text
delta_pp = current_used_percent - anchor_used_percent
full_window_usd = 100 * sum(model_costs_since_anchor) / delta_pp
model_share_at_100_usd[m] = 100 * model_cost_since_anchor[m] / delta_pp
```

Example: $0.50 of observed token cost accompanying a rise from 20% to 22%
implies a $25 full-window value. If Luna contributed $0.10 and Sol $0.40,
their projected shares are $5 and $20 at that same usage mix.

These model shares add to the full-window estimate. They are not independent
full allowances for exclusive use of each model. Account percentages alone do
not identify individual model consumption when models share a limit. If a span
contains only one eligible model, the full value can also be reported as that
model's observed exclusive-use estimate. Otherwise set that separate field to
`null` with `mixed_models`; still emit every model's tokens, cost, and projected
share. If actual separate model limit groups exist, map and estimate them
independently. Do not invent per-model quota percentages.

Use the whole valid span since the anchor to reduce rounding noise, while still
logging each check's period tokens/cost. In particular, accumulate costs across
checks where reported usage does not change. Require a positive percentage delta
meeting `ESTIMATE_MIN_PERCENT_POINTS` (default 1); below that threshold emit
`insufficient_usage_delta`. Record the observation span and delta so small samples
remain recognizable as approximate.

Invalidate/rebaseline the affected window on a reset timestamp or duration change,
a usage decrease, an observed reset/refill, an account/limit change, a pricing
revision, missing percentage data, or incomplete usage coverage. Preserve period
tokens/cost even when a reset spans the check, but do not extrapolate across it.
Primary and secondary rebaseline independently. Never divide by zero or turn a
negative delta into a positive one. An idle period has zero observed tokens/cost;
it must not imply that a full allowance costs zero. Usage at a saturated 100%
cannot calibrate further consumption until a new usable anchor exists.

Use statuses such as `baseline`, `insufficient_usage_delta`, `reset`,
`partial_coverage`, `unpriced_model`, `invalid_counters`, and `estimated`.
Report known subtotal costs separately from a complete total. Local observation
cannot establish all usage from other machines; log the source scope assumption.
The [Codex app-server interface](https://learn.chatgpt.com/docs/app-server)
reports allowance percentages by limit group; converting them to dollars is this
application's empirical estimate.

## 5. Persist results in the existing application log

Keep the existing timestamp, `primary=`, `secondary=`, and `balance=` prefix.
Append a compact `usage_v2=<JSON>` object on the same physical line, with:

- Schema version, stable period ID, UTC period bounds, and actual elapsed time.
- Source coverage, accepted/excluded/invalid record counts, and quality flags.
- Per-model requests, input/output/cache counts, priced categories, component
  costs, applied rates, and estimated period cost; also aggregate period totals.
- For each primary/secondary limit window: reset identity, observed percentage
  delta, calibration bounds, full-window value, per-model projected shares,
  exclusive-use value when identifiable, and status/reason for missing values.

Dollar values are decimal strings; unavailable values are JSON `null` with a
reason. An empty period is recorded. Human-facing diagnostic summaries go to the
existing logger/journal. Persist only usage metadata, not prompts or responses.

Extend `state.json` with a versioned accounting section containing cursors,
calibration state, and recovery metadata. Keep current snapshot fields readable
and migrate old state on first use. Read the previous state before assembling
the new log entry so the period boundary and accounting cursor are explicit.

Serialize checks using a local lock, including manual checks concurrent with
systemd. Make log append and cursor advancement recoverable with a stable period
ID and pending commit record: save the pending result atomically, append/recover
the complete log line once, then commit the new cursor/anchors. On restart,
reconcile the final log record and any partial tail before reading another
period. A failed append must never advance past unlogged usage. Keep accounting
recovery independent of notification delivery.

Headroom or pricing errors should yield an explicit unavailable-cost entry while
the existing reset/credit detection continues. A failed account query must not
advance the successful-sample boundary; the next successful check catches up
and reports any resulting gap. Never treat unreadable logs as zero usage.

## 6. Implementation sequence and acceptance checks

1. **Settings and pricing:** add/validate the example file and settings loader;
   update installer and configuration documentation, preserving user overrides.
2. **Headroom reader:** implement normalization and incremental rotation-aware
   collection with small sanitized fixtures based on the inspected formats.
3. **Cost and window math:** implement Decimal pricing, inferred-write policy,
   context/tier rules, and independent primary/secondary calibration.
4. **Monitor integration:** extend state/logging and recovery, then connect the
   collector to `run_once` and ensure pricing reloads on every loop iteration.
5. **Documentation and validation:** document units, formulas, source assumptions,
   configuration edits, log schema, and diagnostics. Update README privacy/state
   descriptions to reflect token counts and dollar estimates.

Expected file changes: `monitor.py`, `config.env.example`,
`model-pricing.json.example` (new), `install.sh`, `README.md`, and
`tests/test_monitor.py`, with sanitized fixtures if useful. Keep the service
dependency-free; do not require the monitor to install/import Headroom.

Acceptance tests must demonstrate:

- Two consecutive checks attribute each completed row to one period, with all
  five requested models supported by defaults and configurable aliases/rates.
- Same WebSocket ID on multiple turns counts all turns; restarts, rotations,
  copied overlap, partial lines, and crash/retry do not silently lose or replay
  usage. Gaps produce a visible status.
- A synthetic Sol record with `I=1000`, `R=600`, `W=200`, `O=100` costs exactly
  `$0.00404` at the default short-context rates. Inferred writes follow the
  selected policy; GPT-5.5 incurs no extra write premium; local cache hits cost
  zero upstream. Invalid/missing counters do not become fabricated zeros.
- Changing settings changes the next iteration's cost and records a new pricing
  revision. Long-context and configured tier multipliers apply per request.
- `$0.50 / 2 percentage points * 100` yields `$25`; mixed-model shares sum to
  that value without claiming two exclusive model allowances.
- Costs from zero-delta periods are retained. First run, percentage rounding,
  reset/refill, saturated usage, missing windows, unpriced models, and only one
  window resetting produce correct independent statuses/anchors.
- Existing state migrates; existing log prefix expectations and reset/credit
  notifier tests still pass. Source/pricing failures preserve reset detection.
- Installer retains existing settings and installs the pricing template only
  when absent. Both `--once` and `--loop` use the same accounting flow.

Run `rtk proxy python3 -m unittest discover -s tests -v` after implementation.
Then replay sanitized multi-period fixtures through the complete check flow and
inspect the resulting `usage.log` and state. Use mocked account queries and
notifiers for verification so validation does not send messages.
