# Codex Reset Monitor

Get an email the moment your Codex usage resets — including when OpenAI quietly grants you a free reset credit or tops up your credit balance on a random day, not just on the scheduled weekly reset.

A small, dependency-free Python service that reads your authenticated Codex account limits through the local official `codex app-server` interface, snapshots them, and notifies you when something you care about changes. No third-party packages, no database, and no prompts or responses copied anywhere — authentication stays entirely with your installed Codex CLI.

## What triggers an alert

The weekly quota is probed every minute. Full accounting and notifications run when the weekly value changes, or at a bounded ten-minute checkpoint so requests within a displayed percentage are retained. Primary/credit changes alone wait until that checkpoint:

| Alert | Trigger |
| --- | --- |
| **New Codex reset credit available** | A new redeemable reset credit appears — the available count goes up, or a new credit ID is reported. This is the "OpenAI gave you a free reset out of the blue" case. |
| **Codex credit balance increased** | Your reported credit balance goes up (for example `0` → `10.00`). |
| **Codex short usage window reset** | The short window's reset timestamp advances *and* its used percentage drops. |
| **Codex long/weekly usage window reset** | Same logic for the long/weekly window — the classic "my limit is back" email. |

A few deliberate design choices:

- **First run only records a baseline**, so you never get a false alert from the initial reading.
- **Increases only.** You are notified when credits appear or windows reset — never when *you* spend them. No spam from your own usage.
- **One-minute weekly detection; up to ten minutes for other alerts.** A quota change triggers an immediate full check; unchanged weekly probes are lightweight.

## How it works

1. A systemd **user** timer runs a one-shot weekly probe every minute; full token accounting happens on a weekly change or a ten-minute safety checkpoint.
2. Each check talks to your local `codex app-server` over JSON-RPC (`account/rateLimits/read`) — the same data `/status` shows in the Codex TUI.
3. The snapshot (usage percentages, reset timestamps, credit counts, balance) is stored in `~/.local/state/codex-reset-monitor/state.json`.
4. Differences against the previous snapshot become an email via SMTP (or WhatsApp, optionally).

## Quick start

### 1. Prerequisites

- Linux with Python 3.10+ and systemd user services.
- [Codex CLI](https://github.com/openai/codex) installed and signed in with the ChatGPT account you want to monitor.
- Run `codex` once and confirm `/status` shows your usage.

### 2. Install

```bash
git clone https://github.com/vscunha/codex-reset-monitor.git
cd codex-reset-monitor
./install.sh
```

The installer creates:

- `~/.config/codex-reset-monitor/config.env` (mode `0600`)
- `~/.local/lib/codex-reset-monitor/monitor.py`
- `~/.config/codex-reset-monitor/model-pricing.json` (mode `0600`, created only when absent)
- a systemd user timer that probes weekly every minute (enabled immediately)

The optional `CODEX_LIMIT_ID` setting selects a limit group from the app-server response (default `codex`).

### 3. Set up Gmail (about 5 minutes)

Google does not let scripts sign in with your normal password — you need an **App Password**:

1. Go to [myaccount.google.com](https://myaccount.google.com) → **Security** → enable **2-Step Verification** if it is not already on (App Passwords require it).
2. Open [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords) (or search "App passwords" in the account search bar).
3. Create one named e.g. `codex-monitor`. Google shows a **16-character password** — copy it; it is shown only once.
4. Edit the config:

```bash
nano ~/.config/codex-reset-monitor/config.env
```

```dotenv
NOTIFY_CHANNELS=email
SMTP_USERNAME=your-address@gmail.com
SMTP_PASSWORD=the-16-character-app-password
EMAIL_FROM=your-address@gmail.com
EMAIL_TO=your-address@gmail.com
```

`EMAIL_TO` accepts a comma-separated list if you want several recipients. Never put your real Google password in this file.

### 4. Test it

Send yourself a test email:

```bash
python3 ~/.local/lib/codex-reset-monitor/monitor.py \
  --test-notification \
  --env-file ~/.config/codex-reset-monitor/config.env
```

Check that Codex limits can be read:

```bash
python3 ~/.local/lib/codex-reset-monitor/monitor.py \
  --print-status \
  --env-file ~/.config/codex-reset-monitor/config.env
```

Then record the baseline (the timer also does this on its first run):

```bash
systemctl --user start codex-reset-monitor.service
```

Done. From now on the timer probes weekly every minute; it accounts and notifies on weekly changes or a ten-minute checkpoint.

## Running when logged out / after reboot

User services normally run only while you are logged in. To keep the monitor alive across reboots and logouts, enable *linger* once:

```bash
loginctl enable-linger "$USER"
```

Your user manager then starts at boot, and the timer fires one minute after boot and every minute after that — no login required. Check the setting with `loginctl show-user "$USER" -p Linger`.

## Checking operation

```bash
systemctl --user list-timers codex-reset-monitor.timer   # next run
journalctl --user -u codex-reset-monitor.service -f      # live logs
```

## Privacy

The persisted state contains usage percentages, reset timestamps, accounting cursors, and calibration metadata:

```text
~/.local/state/codex-reset-monitor/state.json
~/.local/state/codex-reset-monitor/usage.log
```

`usage.log` is an append-only text file with one entry per full accounting checkpoint (not per lightweight one-minute probe). Every line keeps the readable timestamp, primary/secondary percentages, and balance prefix, then adds a compact `usage_v3`-compatible `usage_v2` JSON object with the period's Headroom token counts, model costs, source diagnostics, pricing revision, raw checkpoint projection, and anchor-based 100% estimates. Token accounting uses total input, cached input, uncached input (`input - cached_input`), and output. Headroom's raw `cache_write` counter is retained for diagnostics only and is never billed as a separate category. Each window also contains `cumulative_usage`, observed percentage `crossings`, rolling estimates, confidence, model attribution, spike records, and `cycle_history`. In the default legacy mode Primary and Secondary have independent anchors and histories; `--weekly-probe` preserves existing Primary history but updates estimates only for Secondary. Multi-point percentage changes are interval observations, never independent one-point measurements. Prompts and responses are never copied.

The installer copies editable defaults to `model-pricing.json`. Rates are USD per million tokens for GPT-5.5, GPT-5.6 Luna/Terra/Sol, and the GPT-6 family (Astra/Sol/Luna), with exact aliases and per-request long-context/tier multipliers. Edit that file or set `MODEL_PRICING_FILE`; it is reloaded on every check. Increase its `revision` when changing rates so an existing calibration starts a fresh price-consistent span. `HEADROOM_LOG_DIR` defaults to `~/.headroom/logs`; both `proxy.log*` and port-suffixed `proxy-<port>.log*` files (plus their numbered rotations) are read, so a proxy restart that renames the active log does not stop accounting. `HEADROOM_EXCLUDED_MODELS` is a comma-separated list of models that share the proxy but never consume Codex quota (other providers reached through the same proxy). Those rows are dropped before aggregation, because counting them would price them as `unpriced_model`, keep every period incomplete, and inflate the local token sum the outside-VM evidence compares against the Codex account counter. Unset uses the built-in third-party defaults; set it to an empty value to disable exclusion. It is deliberately an exclusion list and never an allowlist, so a newly released Codex model still reports `unpriced_model` instead of silently dropping spend. The legacy `INFERRED_CACHE_WRITE_MODE` setting is accepted for compatibility but ignored: uncached input is always priced at the ordinary input rate. `SPIKE_COST_SHARE_THRESHOLD` and `SPIKE_MIN_COST_USD` control raw-period spike reporting. A missing or invalid pricing file keeps token totals and reset detection visible while marking dollar estimates unavailable.

### Cost estimates

The pricing file uses one central Codex pricing mode. Normalized billing fields are total input, cached input, uncached input, and output. Raw Headroom cache-write data is retained only for diagnostics. Dollar values use the current-price-equivalent basis; historical rates are not silently mixed into that metric.

The weekly probe records at least one full checkpoint every ten minutes for token-rate and debugging analysis, plus checkpoints on weekly changes. When attribution is clean, its 100% estimate uses priced local cost divided by percentage points that had local usage; percentage rises with zero local tokens are excluded:

```text
known priced local cost since anchor
  / (weekly percentage-point delta - no-local-token rise points) * 100
```

A +N jump with local tokens is one aggregate interval, not proof that every point was local. If account evidence detects outside usage or is stale/catching up, the monitor cannot identify which percentage points were local, so it **withholds every normalized $/100 projection**, including the local-only ratio and rolling/model projections. It still preserves measured local cost, raw crossings, and the count of points excluded for zero local tokens; it does not guess a subtraction. `weekly_100pct_usd` and `weekly_local_100pct_usd` are therefore both unavailable while local/external points cannot be separated. Raw checkpoint projections are non-authoritative.

When a Secondary reset is detected, the previous cycle is closed in `cycle_history` and a new independent anchor/crossing series starts. A migration from the older cache-write billing schema preserves a legacy summary but does not reuse its dollar totals in the new calculation.

### Outside-VM spend

The Codex `app-server` call reports account-wide percentages only — never token counts. Local Headroom logs capture only this machine's requests. The installed `--weekly-probe` mode **does not impute** a percentage rise with no local tokens: it retains the anchor and excludes those points from the denominator. A +N jump with local calls is one aggregate interval; it is marked unresolved, not deemed fully local. When outside use is detected or the account counter is stale/catching up, the monitor cannot distinguish mixed-source percentage points and withholds all normalized $/100 estimates, not just the account-wide one. Historical/legacy `--once` behavior is preserved for backfill compatibility: when the percentage rises with no local rows (which might reflect unproxied usage, delayed quota reporting, or lost proxy coverage), it imputes the missing points from the window's own mean cost-per-point and mean input/cached/output mix, attributes them to an `external-imputed` bucket, and flags the window approximate (`imputed_external_usage`, reduced confidence, `has_imputed`, `imputed_cost_usd`/`exact_cost_usd` in every estimate). Imputation requires at least `EXTERNAL_IMPUTATION_MIN_POINTS` (default 2) completed points of priced history; without that, the window restarts as `unattributed_usage` rather than inventing numbers. Set `EXTERNAL_IMPUTATION=0` to disable it.

At full checkpoints, the monitor also reads the account-wide `account/usage/read` lifetime token counter and daily usage buckets, storing the lifetime delta next to the local Headroom token sum plus the account-reported per-day totals as `external_evidence` (in state and in each `usage_v2` log line). Daily buckets are account-wide date totals, not per-cycle attribution; the bucket date/timezone and token-accounting semantics may differ from local Headroom sums. This stream never changes anchors, crossings, or measured local costs. Outside usage or an unavailable/stale/catching-up counter marks the cycle `external_usage_unquantified` and suppresses both account-wide and local-only normalized $/100 projections, because the external percentage points cannot be apportioned. Two caveats learned from live data: the lifetime counter batch-updates (observed frozen for hours while local usage burned millions of tokens), so a delta below this VM's own spend is recorded as `stale_counter` and a supra-physical delta as `counter_catchup` — both refuse attribution instead of logging a false zero or a false spike. Only a fresh delta at or above the local sum yields an `external_tokens_estimate`. Early lines logged before these guards (reason `ok` with a frozen counter) are inconclusive, not proof of zero external spend. The CLI exposes no account-wide call/request counts, so call-count comparison is impossible from CLI data — `account_calls_available` is always false; only local `local_period_requests` is recorded. Daily bucket totals now persist on every full checkpoint so yesterday's account-wide token count remains available for audit. Set `EXTERNAL_EVIDENCE=0` to skip the extra usage read.

### Audited weekly cycle (2026-09-19 → 2026-09-23)

`weekly-1pct-breakdown.md` lists 65 **observed crossing intervals**, not fabricated 1% bins. `cycles/secondary_100pt_2026-09-19.audit.json` keeps the complete interval reconciliation and 323 original account-counter samples recovered from `usage.log.before-backfill-20260923T141409Z.bak`. The historical comparison ledger is `cycles/weekly-historical-adjustments.md` (also included in the JSON export). For the Sep 19–23 cycle, it selects **$66.65047748 at first 100% / $66.84827486 through close** as conditional working values, replacing the recorded cost for three weakly recorded jumps at the observed reference rate. This is the selected historical comparison, not a measured invoice. Other weekly spans without reliable external-usage attribution are marked unquantified and excluded from adjusted comparisons; their raw monitor values and requests remain preserved. The $53.43 Headroom-only ratio is an alternate diagnostic, not the selected cycle value. The raw re-read includes 94 reset-boundary calls of uncertain quota-cycle ownership. The export's legacy `percent_bands` field describes the last displayed percentage at request time, not the percentage each call caused; `observed_intervals` supersedes it for quota analysis. Recreate the report (do not replay live accounting):

```bash
python3 weekly_cycle_audit.py \
  --original-log ~/.local/state/codex-reset-monitor/usage.log.before-backfill-20260923T141409Z.bak \
  --annotate-export
```

### Historical backfill

`backfill.py` rebuilds derived usage payloads from retained raw Headroom requests. By default it starts at the current post-restart baseline (`2026-09-09T17:41:42+00:00` in this installation); lines before that boundary and all raw Headroom logs are left unchanged. It saves a usage-log/state backup and a JSON report:

```bash
python3 ~/.local/lib/codex-reset-monitor/backfill.py \
  --env-file ~/.config/codex-reset-monitor/config.env
```

Use `--dry-run` to inspect the replay without changing the derived log or state, or `--start ISO_TIMESTAMP` for a later restart boundary. Intervals without retained request-level data are marked unavailable rather than estimated from aggregate tokens.

No ChatGPT access token is copied into the monitor state or configuration. Authentication remains managed by the installed Codex CLI.

## Optional: WhatsApp Cloud API

Email is the simplest free option. WhatsApp support is included, but it requires a Meta developer/business setup, a sender number, an access token, and usually an approved message template; Meta pricing or template rules may apply.

Create an approved template named `codex_reset_alert` whose body contains two variables (`{{1}}` then `{{2}}`), then configure:

```dotenv
NOTIFY_CHANNELS=whatsapp
WHATSAPP_API_URL=https://graph.facebook.com/YOUR_GRAPH_VERSION/YOUR_PHONE_NUMBER_ID/messages
WHATSAPP_ACCESS_TOKEN=your-permanent-access-token
WHATSAPP_TO=55DDDNUMBER
WHATSAPP_TEMPLATE_NAME=codex_reset_alert
WHATSAPP_TEMPLATE_LANGUAGE=en_US
```

Both channels can be used together: `NOTIFY_CHANNELS=email,whatsapp`.

Leaving `WHATSAPP_TEMPLATE_NAME` empty sends a free-form text message, which normally works only while an eligible customer-service conversation window is open — a template is preferable for unattended alerts.

## Optional: ntfy push notifications

For Android (and iOS) push without setting up email or Meta, use [ntfy.sh](https://ntfy.sh):

1. Install the ntfy app and tap **Subscribe to topic**.
2. Pick a long, random topic name — it works like a shared password on the free ntfy.sh server.
3. Edit `config.env`:

```dotenv
NOTIFY_CHANNELS=ntfy
NTFY_TOPIC=your-random-codex-topic-name
```

Send a test notification:

```bash
python3 ~/.local/lib/codex-reset-monitor/monitor.py \
  --test-notification \
  --env-file ~/.config/codex-reset-monitor/config.env
```

You should see the message in the app immediately.

Optional variables:

| Variable | Default | Notes |
| --- | --- | --- |
| `NTFY_URL` | `https://ntfy.sh` | Set to your own server for self-hosting. |
| `NTFY_TOKEN` | empty | Access token when your topic/server requires auth. |
| `NTFY_PRIORITY` | `default` | `min`, `low`, `default`, `high`, `max`. |
| `NTFY_TAGS` | empty | Comma-separated emoji/keywords shown in the notification. |

Combine channels: `NOTIFY_CHANNELS=email,ntfy`.

> **Note:** free `ntfy.sh` topics are open by default; anyone who guesses your topic can send or read messages. Use a long random topic name, and treat `NTFY_TOKEN` as required if you later switch to a private server.

## Manual and continuous modes

One check:

```bash
python3 monitor.py --once --env-file ./config.env
```

Long-running foreground mode (handy for trying things out before installing):

```bash
python3 monitor.py --loop --env-file ./config.env
```

The systemd installation uses one-shot weekly probes every minute (full accounting on change or at least every ten minutes), which is easier to supervise than a permanent Python process.

## Detection logic (the fine print)

For each usage window, a reset is reported only when **both** conditions occur:

1. the window's `resetsAt` timestamp advances; and
2. the used percentage falls by at least `RESET_MIN_PERCENT_DROP` (default `1`).

This avoids confusing normal consumption changes or stale readings with a reset. Independently of the windows, the monitor also compares reset-credit counts/IDs and the credit balance (exact decimal comparison, increases only). If several things change between two checks, they are combined into a single email.

## Troubleshooting

- **401 / "token invalidated":** your Codex login expired. Open `codex`, sign in again with your ChatGPT account, and the next check succeeds.
- **`codex` not found under systemd:** set `CODEX_COMMAND` to the full executable path in `config.env`, e.g. `CODEX_COMMAND="/home/you/.local/bin/codex app-server"`. Find it with `command -v codex`.
- **Gmail rejects the password:** use an App Password with 2-Step Verification enabled — never your normal password. App Passwords may be unavailable on some managed or Advanced Protection accounts.
- **Test email missing:** check the Spam folder, then re-run `--test-notification` and read the error output.
- **No alert on the very first run:** intentional — the first run establishes the baseline.

## Uninstall

```bash
./uninstall.sh
```

Removes the program and the systemd units; keeps your `config.env` and state.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

## License

MIT — see [LICENSE](LICENSE).
