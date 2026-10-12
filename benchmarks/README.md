# Percentage benchmark

`percentage_benchmark.py` is an isolated experiment harness. It does not
modify `monitor.py`, the systemd timer, `state.json`, or `usage.log`.

The benchmark burns tokens with a real agent CLI until the account's
Primary/Secondary percentage moves, then reports tokens and cost per observed
point. Two evidence sources are available:

- `PERF` records from Headroom give per-request input, cached input, output,
  model, and timing data.
- pi's own `--mode json` events give per-message `input`, `cacheRead`,
  `cacheWrite`, `output`, `reasoning`, `totalTokens`, and `cost`.

The account percentage itself only moves in whole integer points, so a
crossing is always bracketed by the request that contained it. The prompt size
is only a starting approximation; the reported counters are authoritative.
The crossing percentage always comes from the Codex account snapshot
(`account/rateLimits/read`), because both harnesses spend the same signed-in
allowance.

## Dry run

The default is safe and sends no requests:

```bash
python3 benchmarks/percentage_benchmark.py \
  --window secondary \
  --target-points 0.1
```

It uses recent completed crossings in the installed monitor state, separately
for each model. Use `--model-tokens-per-point MODEL=N` for an explicit model
calibration, or `--target-tokens N` for a fixed raw-token target.

## Harnesses

`--harness codex` (default) runs the Codex CLI and reads Headroom `PERF` rows.
`--harness pi` runs pi headlessly:

```bash
python3 benchmarks/percentage_benchmark.py \
  --execute --confirm-spend \
  --harness pi --token-source auto \
  --pi-provider openai-codex \
  --window secondary \
  --models gpt-6-luna,gpt-6-sol \
  --mode crossing \
  --chunk-tokens 50000 \
  --accumulate-points 2 \
  --fine-chunk-tokens 5000 \
  --output /tmp/pi-percentage-benchmark.json
```

pi runs as `pi --mode json --no-session --no-tools --no-context-files
--no-skills --no-prompt-templates --no-extensions --provider <provider>
--model <provider>/<id>`; the payload is piped through stdin. Use
`--pi-keep-extensions` if a provider is supplied by a pi extension,
`--pi-extra-args` for anything else, and `--thinking <level>` to pin a
thinking level (omitted preserves the pi configuration).

`--token-source` chooses where counters come from:

- `pi` reads pi's own usage events. It needs no Headroom routing and also
  reports pi's catalog-based `cost.total` as `pi_reported_cost_usd`.
- `headroom` reads Headroom `PERF` rows, which requires the harness traffic to
  reach Headroom.
- `codex` reads codex's `turn.completed` usage events. **This count is always
  low.** A codex `exec` issues two requests — a smaller preliminary one and the
  main turn — but the JSON stream reports only the main turn, so roughly 5% of
  the tokens are missing. Results carry
  `codex_usage_reported_turns_only: true` and a `usage_note` when this source is
  used.
- `auto` (default) resolves per attempt in this order: pi usage events, then
  Headroom rows, then codex usage. Headroom outranks codex because it logs
  every request, including the preliminary one. When `--codex-base-url` is set
  the proxy is bypassed, so `auto` skips the Headroom wait and uses the
  codex-reported count directly.

Both paths are priced through the monitor's `model-pricing.json` so
`cost_usd` is comparable with the monitor. pi's own cost is reported
separately and may use different catalog rates; for a subscription both are
API-equivalent estimates, not invoices.

## Live run

Only run this with other Codex agents stopped. Codex, Work, and pi usage can
share the same allowance, and the requests below consume the signed-in
account's quota. The command requires two explicit flags:

```bash
python3 benchmarks/percentage_benchmark.py \
  --execute --confirm-spend \
  --window secondary \
  --target-points 0.1 \
  --models luna,terra,sol,astra \
  --mode crossing \
  --max-total-tokens 25000000 \
  --output /tmp/codex-percentage-benchmark.json
```

Each model is run in its own ephemeral invocation with a unique, tool-free
prompt. The harness captures the new counters, checks the model identity, and
takes a new usage snapshot after each request. If another model appears during
a run, it stops and marks the result contaminated.

`--mode crossing` is the default and works even when a model has no historical
exclusive calibration. It sends chunks until a percentage increase is
observed, subject to `--max-total-tokens` and the attempt limit. It then
reports the actual tokens per observed percentage point and the implied
tokens for `0.1%`.

`--mode midpoint` uses a model-specific calibration and stops after that
model's estimated tenth-point token budget. It refuses to run a model without
an exclusive calibration unless `--model-tokens-per-point MODEL=TOKENS` or
`--target-tokens` is supplied. A crossing is not automatically a complete
one-percent measurement: the starting position inside the rounded bucket is
unknown.

The benchmark never copies a Luna calibration to Sol, Terra, or Astra. Mixed
historical intervals are rejected for model calibration. The first crossing
run is therefore the bootstrap measurement for models without exclusive
history.

## Granularity

**Absolute tokens-per-point from a single crossing is not trustworthy.** Measured
on the same model and account, single-point benchmark runs gave 3.4–4.6M
tokens/point while the real workload gave ~29.9M — an 8.7× gap. A crossing is
observed by polling an integer percentage, so it can be caused partly by
concurrent traffic from other sessions, and any such traffic makes the measured
figure too low. Treat benchmark results as **relative** comparisons between arms
run under identical conditions, not as absolute rates, and prefer several points
per arm over one.

The account percentage is an integer, so finer measurement comes from two
levers:

- `--chunk-tokens N` bounds each crossing bracket. The crossing happened
  somewhere inside the last request, so a smaller chunk narrows it.
- `--accumulate-points K` keeps burning until `K` integer points have crossed.
  The summed tokens are divided by the observed points, so the per-point
  bracket shrinks roughly by `K`.

`--fine-chunk-tokens N` plus `--fine-window-fraction F` shrink only the final
part of a point: once the model has a point-size estimate (from
`--model-tokens-per-point`, historical crossings, or the current run's first
crossing), requests within `F` of the expected boundary use the smaller size.
A good pattern for a very fine estimate is a calibrated model with
`--chunk-tokens 50000 --fine-chunk-tokens 2000 --fine-window-fraction 0.05
--accumulate-points 3`, which typically lands the per-point bracket within a
few thousand tokens. Fine chunks need many more requests for the same tokens,
so raise `--max-attempts` (for example `400`) whenever fine chunks are enabled;
otherwise a run can stop short of the boundary with the bucket only partly
filled.

Each result reports:

- `observed_tokens_per_point` / `observed_cost_per_point_usd` — totals divided
  by the observed points.
- `crossings[]` — per-crossing percent, cumulative tokens/cost, and the
  `bracket_tokens` of the request that contained it.
- `quantization_uncertainty_tokens_per_point` — the summed brackets divided by
  the observed points; a bound on the integer-percentage quantization error.

`--poll-seconds` (default 0.25) controls how often the account snapshot and
Headroom capture are polled; `--max-seconds` is a wall-clock safety limit.

For a fixed experiment set `--reasoning-effort` (Codex) or `--thinking` (pi)
explicitly; otherwise the harness configuration is preserved. The benchmark
does not retain model responses.
