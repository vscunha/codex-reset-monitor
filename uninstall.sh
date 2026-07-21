#!/usr/bin/env bash
set -euo pipefail
systemctl --user disable --now codex-reset-monitor.timer 2>/dev/null || true
rm -f "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/codex-reset-monitor.service"
rm -f "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/codex-reset-monitor.timer"
rm -rf "$HOME/.local/lib/codex-reset-monitor"
systemctl --user daemon-reload
printf 'Program removed. Configuration and state were kept.\n'
