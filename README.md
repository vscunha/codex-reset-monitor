# Codex Reset Monitor

A small dependency-free Python service that reads your authenticated Codex account limits through the local official `codex app-server` interface. It stores the last snapshot and notifies you when it detects:

- the short usage window resetting;
- the long/weekly usage window resetting;
- a new redeemable Codex reset credit;
- an increase in the reported credit balance.

The first run only records a baseline, so it does not produce a false alert.

## Recommended setup: email

Email is the simplest free option. WhatsApp Cloud API support is included, but it requires a Meta developer/business setup, a sender number, an access token, and usually an approved message template. Meta pricing or template rules may apply.

### 1. Prerequisites

- Linux with Python 3.10 or newer and systemd user services.
- Codex CLI installed and signed in with the ChatGPT account whose limits you want to monitor.
- Run `codex` once and confirm that `/status` can show your usage.

### 2. Install

```bash
chmod +x install.sh
./install.sh
```

The installer creates:

- `~/.config/codex-reset-monitor/config.env`
- `~/.local/lib/codex-reset-monitor/monitor.py`
- a systemd user timer that checks every five minutes.

### 3. Configure Gmail

Edit:

```bash
nano ~/.config/codex-reset-monitor/config.env
```

Set at least:

```dotenv
NOTIFY_CHANNELS=email
SMTP_USERNAME=your-address@gmail.com
SMTP_PASSWORD=your-16-character-google-app-password
EMAIL_FROM=your-address@gmail.com
EMAIL_TO=your-address@gmail.com
```

For Gmail, enable two-step verification and create a Google App Password. Do not put your normal Google password in this file. The file is installed with mode `0600`.

Test the notification:

```bash
python3 ~/.local/lib/codex-reset-monitor/monitor.py \
  --test-notification \
  --env-file ~/.config/codex-reset-monitor/config.env
```

Test reading Codex limits:

```bash
python3 ~/.local/lib/codex-reset-monitor/monitor.py \
  --print-status \
  --env-file ~/.config/codex-reset-monitor/config.env
```

The first normal check creates the baseline:

```bash
systemctl --user start codex-reset-monitor.service
```

### 4. Check operation

```bash
systemctl --user status codex-reset-monitor.timer
journalctl --user -u codex-reset-monitor.service -f
```

The persisted state contains usage percentages and reset timestamps only:

```text
~/.local/state/codex-reset-monitor/state.json
```

No ChatGPT access token is copied into the monitor state or configuration. Authentication remains managed by the installed Codex CLI.

## Optional WhatsApp Cloud API

Create an approved WhatsApp template named `codex_reset_alert` whose body contains two variables, such as:

```text
{{1}}
{{2}}
```

Then configure:

```dotenv
NOTIFY_CHANNELS=whatsapp
WHATSAPP_API_URL=https://graph.facebook.com/YOUR_GRAPH_VERSION/YOUR_PHONE_NUMBER_ID/messages
WHATSAPP_ACCESS_TOKEN=your-permanent-access-token
WHATSAPP_TO=55DDDNUMBER
WHATSAPP_TEMPLATE_NAME=codex_reset_alert
WHATSAPP_TEMPLATE_LANGUAGE=en_US
```

You can use both channels:

```dotenv
NOTIFY_CHANNELS=email,whatsapp
```

Leaving `WHATSAPP_TEMPLATE_NAME` empty makes the monitor send a free-form text message. That normally works only while an eligible customer-service conversation window is open, so a template is preferable for unattended alerts.

## Manual and continuous modes

One check:

```bash
python3 monitor.py --once --env-file ./config.env
```

Long-running foreground mode:

```bash
python3 monitor.py --loop --env-file ./config.env
```

The systemd installation uses one-shot checks every five minutes, which is easier to supervise and restart than a permanent Python process.

## Detection logic

For each Codex usage window, a reset is detected only when both conditions occur:

1. Codex advances the window's `resetsAt` timestamp; and
2. the used percentage falls by at least `RESET_MIN_PERCENT_DROP`.

This avoids confusing normal consumption changes or stale readings with a reset. The monitor also compares reset-credit IDs/counts and the optional monetary/flexible credit balance.

## Troubleshooting

**Authentication required:** open `codex`, sign in with ChatGPT, and run the check again.

**`codex` not found under systemd:** set `CODEX_COMMAND` to the complete executable path, for example:

```dotenv
CODEX_COMMAND="/home/your-user/.local/bin/codex app-server"
```

Find it with `command -v codex` in your terminal.

**Gmail rejects the password:** use a Google App Password with two-step verification enabled. App passwords may be unavailable on some managed or Advanced Protection accounts.

**No alert on initial execution:** this is intentional. The first execution establishes the previous-value baseline.

## Tests

```bash
python3 -m unittest discover -s tests -v
```
