# Codex Reset Monitor

Get an email the moment your Codex usage resets — including when OpenAI quietly grants you a free reset credit or tops up your credit balance on a random day, not just on the scheduled weekly reset.

A small, dependency-free Python service that reads your authenticated Codex account limits through the local official `codex app-server` interface, snapshots them, and notifies you when something you care about changes. No third-party packages, no database, no ChatGPT tokens copied anywhere — authentication stays entirely with your installed Codex CLI.

## What triggers an alert

Checks run every five minutes. Each check compares the fresh snapshot against the saved state and sends one email for everything that changed:

| Alert | Trigger |
| --- | --- |
| **New Codex reset credit available** | A new redeemable reset credit appears — the available count goes up, or a new credit ID is reported. This is the "OpenAI gave you a free reset out of the blue" case. |
| **Codex credit balance increased** | Your reported credit balance goes up (for example `0` → `10.00`). |
| **Codex short usage window reset** | The short window's reset timestamp advances *and* its used percentage drops. |
| **Codex long/weekly usage window reset** | Same logic for the long/weekly window — the classic "my limit is back" email. |

A few deliberate design choices:

- **First run only records a baseline**, so you never get a false alert from the initial reading.
- **Increases only.** You are notified when credits appear or windows reset — never when *you* spend them. No spam from your own usage.
- **Five-minute granularity.** Worst case, the email arrives five minutes after the change.

## How it works

1. A systemd **user** timer runs a one-shot check every five minutes.
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
- a systemd user timer that checks every five minutes (enabled immediately)

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

Done. From now on the timer checks every five minutes and emails you when something changes.

## Running when logged out / after reboot

User services normally run only while you are logged in. To keep the monitor alive across reboots and logouts, enable *linger* once:

```bash
loginctl enable-linger "$USER"
```

Your user manager then starts at boot, and the timer fires two minutes after boot and every five minutes after that — no login required. Check the setting with `loginctl show-user "$USER" -p Linger`.

## Checking operation

```bash
systemctl --user list-timers codex-reset-monitor.timer   # next run
journalctl --user -u codex-reset-monitor.service -f      # live logs
```

## Privacy

The persisted state contains usage percentages and reset timestamps only:

```text
~/.local/state/codex-reset-monitor/state.json
```

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

## Manual and continuous modes

One check:

```bash
python3 monitor.py --once --env-file ./config.env
```

Long-running foreground mode (handy for trying things out before installing):

```bash
python3 monitor.py --loop --env-file ./config.env
```

The systemd installation uses one-shot checks every five minutes, which is easier to supervise and restart than a permanent Python process.

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
