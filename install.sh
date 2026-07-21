#!/usr/bin/env bash
set -euo pipefail

SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/codex-reset-monitor"
LIB_DIR="$HOME/.local/lib/codex-reset-monitor"
SYSTEMD_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

mkdir -p "$CONFIG_DIR" "$LIB_DIR" "$SYSTEMD_DIR"
install -m 0755 "$SOURCE_DIR/monitor.py" "$LIB_DIR/monitor.py"
install -m 0644 "$SOURCE_DIR/codex-reset-monitor.service" "$SYSTEMD_DIR/codex-reset-monitor.service"
install -m 0644 "$SOURCE_DIR/codex-reset-monitor.timer" "$SYSTEMD_DIR/codex-reset-monitor.timer"

if [[ ! -f "$CONFIG_DIR/config.env" ]]; then
  install -m 0600 "$SOURCE_DIR/config.env.example" "$CONFIG_DIR/config.env"
  echo "Created $CONFIG_DIR/config.env"
else
  echo "Kept existing $CONFIG_DIR/config.env"
fi

systemctl --user daemon-reload
systemctl --user enable --now codex-reset-monitor.timer

echo
echo "Installed. Edit: $CONFIG_DIR/config.env"
echo "Test notification: python3 $LIB_DIR/monitor.py --test-notification --env-file $CONFIG_DIR/config.env"
echo "Test Codex query: python3 $LIB_DIR/monitor.py --print-status --env-file $CONFIG_DIR/config.env"
echo "Logs: journalctl --user -u codex-reset-monitor.service -f"
