# Retro review: GPT-6 pricing gap (2026-09-23)

## What happened

The GPT-6 family shipped and Codex began routing traffic to `gpt-6-luna` on
2026-09-23 01:33 UTC. The shipped pricing table still listed only `gpt-6-astra`,
so every `gpt-6-luna` request was priced `unpriced_model`: its tokens were
counted for diagnostics, but it contributed `$0.00` to the window cost.

Unpriced rows deliberately do **not** restart a window (that fix landed
earlier), so the effect was silent under-counting rather than lost history.

## Prices added (Standard processing, USD per 1M tokens)

Verified 2026-09-23 against the official model pages:

| Model | Input | Cached input | Cache write | Output | Long context |
| --- | ---: | ---: | ---: | ---: | --- |
| [GPT-6 Astra](https://developers.openai.com/api/docs/models/gpt-6-astra) | 10.00 | 1.00 | 12.50 | 50.00 | in x2, out x1.5 |
| [GPT-6 Sol](https://developers.openai.com/api/docs/models/gpt-6-sol) | 2.00 | 0.20 | 2.50 | 10.00 | in x2, out x1.5 |
| [GPT-6 Luna](https://developers.openai.com/api/docs/models/gpt-6-luna) | 0.10 | 0.01 | 0.125 | 0.50 | in x2, out x1.5 |

`gpt-6-astra` was already present and its rates already matched the published
table, so only Sol and Luna were new. The whole family uses a 272,000-token
short/long-context boundary and Fast mode at 2x Standard. GPT-6 has no
promotional-pricing note (unlike GPT-5.6 Sol, which carries
`promotion_review_after: 2026-11-21`).

## Unpriced requests found in the retained Headroom logs

`gpt-6-luna` only; `gpt-6-sol` has no requests yet.

| Window | Requests | Missed cost |
| --- | ---: | ---: |
| Primary cycle 01:30:43 -> 05:20:11 | 609 | $0.868244 |
| Primary cycle 05:20:11 -> 12:39:43 | 584 | $0.907176 |
| Weekly cycle 09-19 11:26 -> 09-23 12:39 | 1,193 | $1.775420 |
| Current cycle (since 12:39:43) | 816 | $1.380810 |
| **Total across all retained logs** | **2,009** | **$3.156230** |

Token totals for the same requests: 252,888,190 input (248,947,584 of it
cache reads) and 545,387 output.

## Recovery

Changing the pricing `revision` correctly forced both windows to restart on the
next tick, so the already-accumulated unpriced tokens were dropped. The
retained raw logs still held every request, so a scoped replay rebuilt the
accounting with the new table:

```
backfill.py --start '2026-09-19T11:26:44+00:00'
```

13,399 requests replayed, 544 checkpoints corrected, 19 cycles corrected.
Rehearsed on copies first; the replay was verified to lose no cycle cost.

| Result | Before | After |
| --- | ---: | ---: |
| Closed weekly cycle 09-19 -> 09-23 | $52.97618625 | **$54.61359971** |
| Primary cycle 50% -> 05:20:11 | $0.00 | $0.86824404 |
| Primary cycle 0% -> 12:39:43 | $0.00 | $0.73936322 |
| Earlier primary cycles (3) | — | +$0.0250 combined |
| Current cycle cost basis | $0.00 | $1.37308088 (gpt-6-luna) |
| Weekly 100% estimate | unavailable (`-`) | restored |

Because the replay stamped the new `pricing_revision` into the anchors, the
next scheduled tick continues the existing cycle instead of restarting again.

## Verification

- `gpt-6-luna` now aggregates as `complete: true` with an empty `reasons` list.
- Short-context, long-context (2x input / 1.5x output), and Fast-mode (2x)
  arithmetic asserted in `tests/test_monitor.py::Gpt6FamilyPricingTests`.
- Full suite: 82 passed (pytest and unittest).
- Backups: `state.json.before-backfill-20260923T141425Z.bak`,
  `usage.log.before-backfill-20260923T141409Z.bak`; report:
  `backfill-report-gpt6-pricing.json`.

## Remaining known gap

`deepseek-v4.1-flash` (and `muse-spark-1.3-contributor`, `kimi-k2.6`) arrive
through the same Headroom proxy from pi/OpenCode traffic and stay unpriced by
design: they do not consume Codex quota. They still mark a period
`complete: false` and set `has_unpriced` / `uncertain_attribution`, which
lowers the *reported* confidence reason list but does not change any dollar
estimate.

## Follow-up: non-Codex model filter (2026-09-23)

The gap above was closed with `HEADROOM_EXCLUDED_MODELS`. Non-Codex rows are
now dropped in `collect_headroom_usage` before aggregation, so they never reach
`aggregate_period` or `update_external_evidence`.

Why a denylist and not an allowlist: an allowlist keyed on the pricing table
would have silently dropped `gpt-6-luna` during the window above instead of
flagging it. A newly released Codex model must keep reporting `unpriced_model`.

Measured effect on the retained logs (262 rows, 36,135,014 tokens of
`deepseek-v4.1-flash` removed from the recent windows):

| | Before | After |
| --- | --- | --- |
| Period `total.complete` | `false` (`unpriced_model`) | `true` |
| `external_evidence.local_period_tokens` | 17,005,917 | 14,316,954 |
| `known_cost` for the same range | $2.64031846 | $2.64031846 (identical) |
| `full_window_usd` / `model_shares_at_100_usd` | — | identical (asserted) |

So the filter corrects the outside-VM comparison and the confidence flags
without moving any dollar figure. `diagnostics.excluded_models` counts dropped
rows and `diagnostics.excluded_model_names` plus `source.excluded_models`
record the active list on every `usage_v2` line.

The `has_unpriced` / `has_uncertain_attribution` flags already latched into a
live window state are sticky until that window's next restart, so the weekly
estimate keeps one step of reduced confidence until the next reset unless a
scoped `backfill.py` replay recomputes the cycle.
