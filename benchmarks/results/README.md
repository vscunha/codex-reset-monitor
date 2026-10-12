# pi harness benchmark runs — 2026-09-25 (secondary window)

Harness: `--harness pi --token-source auto --thinking off`, tool-free JSON-mode pi
requests. Crossing percentage comes from the Codex account snapshot; tokens come
from pi's own usage events (monitor pricing and pi's catalog pricing agreed
exactly for every model measured). All requests were uncached input.

## Session 1: gpt-6-luna

| Run | File | Range | Tokens | Cost | Tokens/point | End bracket |
|---|---|---|---:|---:|---:|---:|
| A primer | `2026-09-25_gpt-6-luna_secondary_53-54.json` | 53% → 54% | 3,778,794 | $0.37805 | 3,778,794 | ±71,298 |
| B (stopped at attempt limit) | `2026-09-25_gpt-6-luna_secondary_54-55_incomplete.json` | 54% → 54% | 3,671,848 | $0.36750 | — | — |
| C primer | `2026-09-25_gpt-6-luna_secondary_54-55.json` | 54% → 55% | 285,192 | $0.02853 | — | ±71,298 |
| D clean | `2026-09-25_gpt-6-luna_secondary_55-57.json` | 55% → 57% | 7,896,756 | $0.79028 | 3,948,378 | ±7,260 |

## Session 2: multi-model sweep (blocked at primary 100%)

| Run | File | Model | Range | Tokens | Cost | Tokens/point | Bracket |
|---|---|---|---|---:|---:|---:|---:|
| 1 | `2026-09-25_gpt-6-sol_secondary_57-58.json` | gpt-6-sol | 57% → 58% | 212,481 | $0.42579 | 212,481 | ±5,835 |
| 2 | `2026-09-25_gpt-5.6-luna_secondary_58-59.json` | gpt-5.6-luna | 58% → 59% | 2,709,400 | $0.54218 | 2,709,400 | ±71,300 |
| 3 | `2026-09-25_gpt-5.6-luna_secondary_59-61.json` | gpt-5.6-luna | 59% → 61% | 5,327,034 | $1.06605 | 2,663,517 | ±39,281 |
| 4 | `2026-09-25_gpt-6-astra_secondary_61-63.json` | gpt-6-astra | 61% → 63% | 125,141 | $1.25749 | 62,570.5 | ±2,991 |
| 5 | `2026-09-25_gpt-6-luna_secondary_63-64_incomplete.json` | gpt-6-luna | 63% → 63% | 2,728,713 | $0.27300 | — | — |

Run 5 stopped when the **primary 5-hour window reached 100%** (resets
2026-09-25T04:37:40Z): requests were rejected with no usage events and the
fallback found no matching Headroom records.

## Session 3 (after the primary reset): finishing the sweep

| Run | File | Model | Range | Tokens | Cost | Tokens/point | Bracket |
|---|---|---|---|---:|---:|---:|---:|
| 5b | `2026-09-25_gpt-6-luna_secondary_63-64.json` | gpt-6-luna | 63% → 64% | 900,465 | $0.09013 | — | ±7,261 |
| 6 | `2026-09-25_gpt-5.6-luna_secondary_64-66.json` | gpt-5.6-luna | 64% → 66% | 5,281,590 | $1.05720 | 2,640,795 | ±7,263 |
| 7 | `2026-09-25_gpt-6-sol_secondary_66-68.json` | gpt-6-sol | 66% → 68% | 577,713 | $1.15984 | 288,856.5 | ±4,413 |

Run 5 + Run 5b together filled the 63% bucket (≈3.63M tokens). Run 6
crossings: 65% at 2,669,847 and 66% at 5,281,590. Run 7 crossings: 67% at
295,476 and 68% at 577,713.

## Per-bucket values with start-offset bounds

| Model | Bucket | Tokens/point | $/point | Start offset bound |
|---|---|---:|---:|---:|
| gpt-6-sol | 57 → 58 | 212,481 | $0.4258 | ≤ 7,260 |
| gpt-6-sol | 66 → 67 | 295,476 | $0.5933 | ≤ 7,263 |
| gpt-6-sol | 67 → 68 | 282,237 | $0.5665 | ≤ 4,413 |
| gpt-5.6-luna | 58 → 59 | 2,709,400 | $0.5422 | ≤ 5,835 |
| gpt-5.6-luna | 59 → 60 | 2,638,100 | $0.5276 | ≤ 5,835 |
| gpt-5.6-luna | 60 → 61 | 2,688,934 | $0.5378 | ≤ 71,300 |
| gpt-5.6-luna | 64 → 65 | 2,669,847 | $0.5344 | ≤ 7,261 |
| gpt-5.6-luna | 65 → 66 | 2,611,743 | $0.5229 | ≤ 7,263 |
| gpt-6-astra | 61 → 63 | 62,570.5 | $0.6287 | ≤ 7,262 |
| gpt-6-luna | 53 → 54 | 3,778,794 | $0.3779 | unknown |
| gpt-6-luna | 55 → 57 | 3,948,378 | $0.3948 | ≤ 71,298 |

## Session 4: gpt-5.6-sol attempt — contaminated

| Run | File | Model | Range | Tokens (mine) | Cost | Tokens/point | Status |
|---|---|---|---|---:|---:|---:|---|
| 8 | `2026-09-25_gpt-5.6-luna_secondary_73-74_contaminated.json` | gpt-5.6-luna | 73% → 74% | 1,711,224 | $0.34244 | — | primer, contaminated |
| 9 | (failed) | gpt-5.6-sol | 74% → 74% | 0 | $0 | — | Headroom saw no rows: direct pi traffic does not route through Headroom |
| 9b | `2026-09-25_gpt-5.6-sol_secondary_74-76_contaminated.json` | gpt-5.6-sol | 74% → 76% | 416,832 | $1.67347 | 208,416 (understated) | **contaminated** |

A concurrent gpt-6-luna session resumed at 11:00:22, mid-run, and kept
burning through and after the run:

| Interval | Concurrent gpt-6-luna | Tokens |
|---|---:|---:|
| 74% → 75% (ends 11:00:30) | 2 requests | 31,315 |
| 75% → 76% (ends 11:04:13) | 20 requests | 1,318,256 |
| after the run (11:04:13 → 11:05:39) | 10 requests | 1,602,646 |

### gpt-5.6-sol bounds (not a clean value)

- **Lower bound 208,416 tokens/point** ($0.834): attributes all bucket
  movement to this run's tokens; contamination can only make the true value
  larger.
- **Exchange-rate corrected ≈ 314,000 tokens/point** ($1.26): converts the
  concurrent 2,627,347 gpt-6-luna tokens at its measured ≈3.9M/point rate
  (0.674 points), leaving 1.326 of the 2 points to this run.
- The nearly clean first interval alone (204,001 of my tokens + 31,315
  concurrent + an unknown 9k–80k baseline offset) suggests ≈235k–333k.

A clean value needs a re-run with no concurrent Codex-quota traffic. Session
`13063753` (resumed 08:02, still active) and session `8fd17fb` (started
11:00:02) were the two gpt-6-luna burners observed.

## Session 5: gpt-5.6-sol clean bucket

| Run | File | Model | Range | Tokens (mine) | Cost | Tokens/point | Bracket |
|---|---|---|---|---:|---:|---:|---:|
| 10 | `2026-09-25_gpt-5.6-sol_secondary_76-78.json` | gpt-5.6-sol | 76% → 78% | 451,311 | $1.80793 | 225,655.5 avg | ±21,491 |

Verified clean: no Codex-quota rows appeared in Headroom during the run
(11:09:15–11:11:50); the only other traffic was one deepseek request.

**Only the 77→78 bucket is valid for gpt-5.6-sol.** The 76% bucket already held
≥1.6M gpt-6-luna tokens from the scheduled task's post-run traffic (11:04–11:07)
plus run 9b's overflow, so its 76→77 crossing needed only 214,910 of this run's
tokens. The 77→78 crossing needed 236,401 of this run's tokens with an overflow
of at most 21,491:

- **gpt-5.6-sol ≈ 236k–258k tokens/point (central ≈247k), ≈ $0.99/point**
- The reported 2-point average (225,656) is biased low by the loaded first
  bucket and should not be quoted as the model value.
- Contaminated-run estimates ranged 208k (lower bound) to ~364k (gpt-6-luna
  exchange-rate correction), consistent with the ±30% bucket variance already
  measured for gpt-6-sol.

## Central values

| Model | Median tokens/point | Median $/point | Bucket spread |
|---|---:|---:|---:|
| gpt-6-luna | ≈ 3.86M | $0.386 | 3.78M – 3.97M (~5%) |
| gpt-6-sol | ≈ 289k | $0.580 | 212k – 295k (~33%) |
| gpt-5.6-luna | ≈ 2.67M | $0.534 | 2.61M – 2.71M (~4%) |
| gpt-6-astra | ≈ 62.6k | $0.629 | single bucket |
| gpt-5.6-sol | ≈ 247k (clean 77→78 bucket) | ≈ $0.99 | clean bucket 236k–258k; other estimates 208k–364k |

## Caveats

- **Bucket size varies more than quantization.** gpt-6-sol's 57→58 bucket
  needed 212k tokens while 66→67 and 67→68 needed 295k and 282k (~33% spread),
  at the same uncached input rate ($2.00/M). gpt-5.6-luna's buckets spread
  ~4%. A single clean bucket is a bucket-specific value, not a universal
  tokens-per-point constant; the median across buckets is the better estimate.
- Cost per point is not constant across models: gpt-6-luna $0.39, gpt-6-sol
  $0.58, gpt-5.6-luna $0.53, gpt-6-astra $0.63. Heavier models consume quota
  faster per dollar, so the weekly counter is not purely price-weighted.
- Reported bounds include the previous run's end bracket as the start offset.
- These are local price-equivalent estimates (`current_price_equivalent`), not
  invoices; the Codex subscription is not billed per token.
- Fine chunks need a high `--max-attempts`; a fine window smaller than the
  coarse chunk can still produce a coarse bracket (Run 3's first point).
- The primary 5-hour window exhausts quickly during sweeps and blocks all
  requests until it resets; budget roughly one primary point per secondary
  point for large models.
- **Contamination cannot be detected from pi's own events alone.** Direct pi
  invocations do not route through Headroom (only Omnigent-launched pi
  sessions do), so `--token-source headroom` sees zero rows for benchmark
  traffic; and with `--token-source pi` the only models visible are the
  benchmark's own. Verify no concurrent Codex-quota session is running before
  and after a run, and cross-check the Headroom log for other-model rows
  during the run window.
- **Scheduled tasks are the main contamination source.** The "Manager - codex"
  scheduled task fires hourly at :00 (`America/Sao_Paulo`) on gpt-6-luna and
  runs for several minutes. Pause it or measure inside the quiet window, and
  remember that its traffic can pre-load the bucket a run starts in.

## Session 4: gpt-6.1-sol (new model, fresh weekly window)

Window reset at 22:41:32 UTC; both runs started from a verified-empty window
(no Codex-quota traffic in Headroom before or during either run).

| Run | File | Range | Tokens | Cost | Bucket | Bracket |
|---|---|---|---:|---:|---:|---:|
| 1 | `2026-09-29_gpt-6.1-sol_secondary_0-2.json` | 0% → 2% | 360,060 | $0.72262 | 0→1: 128,952 | ±21,492 |
| 1 | (same) | | | | 1→2: 231,108 | ±21,492 |
| 2 | `2026-09-29_gpt-6.1-sol_secondary_2-4.json` | 2% → 4% | 482,188 | $0.96617 | 2→3: 257,880 | ±12,954 |
| 2 | (same) | | | | 3→4: 224,308 | ±12,954 |

| Bucket | Tokens/point | $/point |
|---|---:|---:|
| 0 → 1 (immediately post-reset) | 128,952 | $0.2579 |
| 1 → 2 | 231,108 | $0.4622 |
| 2 → 3 | 257,880 | $0.5158 |
| 3 → 4 | 224,308 | $0.4486 |

**gpt-6.1-sol ≈ 231k–238k tokens/point ≈ $0.46–0.48/point** (median of the three
steady-state buckets). The post-reset 0→1 bucket is roughly half the others and
should be treated as an outlier: it is the only bucket whose start was the window
boundary itself, so any account-wide or delayed movement right after the reset
lands in it.

### How gpt-6.1-sol compares

| Model | Tokens/point | $/point | cacheRead rate |
|---|---:|---:|---:|
| gpt-6-sol | ≈ 289k (212k–295k) | $0.58 | $0.20/M |
| **gpt-6.1-sol** | **≈ 231k (224k–258k)** | **$0.46** | **$0.10/M** |

Same input/output list price ($2/$10) but half the cached-read price, and it
consumes ~20% fewer tokens per point. Both runs were all-uncached input, so the
cacheRead difference did not affect these numbers.

### Pricing table note

`gpt-6.1-sol` was absent from `model-pricing.json` before this session; it was
added (input $2.00, output $10.00, cached_input $0.10, cache_write $2.50, 272k
long-context tier at 2x/1.5x) in both the live config and
`model-pricing.json.example`. The pricing `revision` was deliberately left
unchanged so the running accounting span was not reset.

### Contamination control

The hourly `Manager - codex` scheduled task (model `openai-codex/gpt-6-luna`)
was paused for the duration of both runs and re-activated afterwards. It had
corrupted run 9 on 2026-09-25 by resuming mid-run.
