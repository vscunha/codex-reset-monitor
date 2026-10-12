# Historical weekly quota values

Export snapshot: 2026-09-24T15:56:26.126090+00:00.
Raw cycle exports and monitor totals remain unchanged; this ledger controls historical comparisons.

| Cycle | Raw monitor total | Adjusted at first 100% | Through close | Status | Count in comparison |
| --- | ---: | ---: | ---: | --- | --- |
| secondary_76pt_2026-09-09 | $76.96127796 | $— | $— | external_usage_unquantified | no |

  - secondary_76pt_2026-09-09: No reliable per-cycle account-counter evidence is retained; do not count the raw monitor estimate as adjusted.
| secondary_13pt_2026-09-11 | $13.98198184 | $— | $— | external_usage_unquantified | no |

  - secondary_13pt_2026-09-11: No reliable per-cycle account-counter evidence is retained; do not count the raw monitor estimate as adjusted.
| secondary_20pt_2026-09-12 | $17.91728632 | $— | $— | external_usage_unquantified | no |

  - secondary_20pt_2026-09-12: No reliable per-cycle account-counter evidence is retained; do not count the raw monitor estimate as adjusted.
| secondary_100pt_2026-09-19 | $54.61359971361058601134215501 | $66.65047747887374390607899711 | $66.84827485887374390607899711 | conditional_selected_not_measured | yes (conditional) |
| current_secondary | $19.83151416 | $— | $— | external_usage_unquantified | no |

  - current_secondary: The live account counter is stale/catching up; wait for a reliable sample.

## Account-wide daily token buckets

Read at 2026-09-24 15:49:43 from `Codex app-server account/usage/read dailyUsageBuckets`. These totals are account-wide, not quota-cycle assignments.
| Date | Account tokens | Retained local Headroom tokens | Monitor-period local tokens | Account − monitor difference |
| --- | ---: | ---: | ---: | ---: |
| 2026-09-14 | 261,466,553 | 242,663,116 | 242,432,384 | +19,034,169 |
| 2026-09-19 | 19,143,861 | 19,281,635 | 19,281,635 | -137,774 |
| 2026-09-20 | 516,644,846 | 514,654,932 | 514,661,619 | +1,983,227 |
| 2026-09-21 | 490,172,587 | 488,287,449 | 529,792,761 | -39,620,174 |
| 2026-09-22 | 58,723,377 | 47,668,487 | 192,365,554 | -133,642,177 |
| 2026-09-23 | 719,765,470 | 705,806,751 | 713,847,934 | +5,917,536 |
| 2026-09-24 | 246,091,960 | 441,500,070 | 438,520,441 | -192,428,481 |

Showing the latest seven reported buckets; all daily rows remain in the JSON export.

Account-reported daily totals are preserved as reported. They are not quota-cycle or per-model attribution. Local raw rows may be incomplete after proxy rotation; checkpoint totals are grouped by checkpoint end date and can span midnight. Counter differences are diagnostic only, not an external-token estimate.

Recent account-counter flags (not cycle/model attribution):
- 2026-09-23 21:05:43: `counter_catchup`, account delta 297763471, local 6543934 tokens, external estimate None; weekly 22.0%, status `external_usage_unquantified`, account $/100 withheld.
- 2026-09-23 23:53:43: `counter_catchup`, account delta 144804249, local 12599817 tokens, external estimate None; weekly 25.0%, status `external_usage_unquantified`, account $/100 withheld.
- 2026-09-24 03:23:42: `counter_catchup`, account delta 109857608, local 4356766 tokens, external estimate None; weekly 29.0%, status `external_usage_unquantified`, account $/100 withheld.
- 2026-09-24 06:48:43: `counter_catchup`, account delta 153188558, local 4695734 tokens, external estimate None; weekly 32.0%, status `external_usage_unquantified`, account $/100 withheld.
- 2026-09-24 09:44:43: `counter_catchup`, account delta 78635608, local 6311524 tokens, external estimate None; weekly 34.0%, status `external_usage_unquantified`, account $/100 withheld.
- 2026-09-24 12:18:43: `ok`, account delta 41146396, local 3079733 tokens, external estimate 38066663; weekly 35.0%, status `external_usage_unquantified`, account $/100 withheld.
- 2026-09-24 14:57:52: `ok`, account delta 59907811, local 10685372 tokens, external estimate 49222439; weekly 40.0%, status `external_usage_unquantified`, account $/100 withheld.

## Latest live weekly accounting checkpoint

As of 2026-09-24 15:49:43 UTC, usage was 41.0%; priced local cost $19.83151416; local-only projection unavailable (local/external percentage points cannot be separated); account-wide projection unavailable.
Locally attributable percentage points: unavailable (candidate 41, no-local points excluded 0).
External evidence: `stale_counter`; counter delta 0, local period tokens 2207667. The cycle remains excluded from adjusted historical comparisons until account-wide attribution is reliable.

The Sep 19–23 values are selected conditional working values based on repricing three weakly recorded jumps; they are not measured invoices. Other weekly spans are excluded from adjusted comparisons because their external-use attribution cannot be reconstructed. Their raw monitor data remains available for audit.
