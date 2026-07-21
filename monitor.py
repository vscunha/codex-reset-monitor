#!/usr/bin/env python3
"""Monitor Codex subscription usage resets through `codex app-server`.

No third-party Python dependencies are required.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import queue
import shlex
import smtplib
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Iterable

LOG = logging.getLogger("codex-reset-monitor")
_MISSING = object()


class MonitorError(RuntimeError):
    """Expected runtime/configuration error."""


@dataclass(frozen=True)
class LimitWindow:
    used_percent: float | None = None
    resets_at: int | None = None
    window_duration_mins: int | None = None


@dataclass(frozen=True)
class Snapshot:
    captured_at: int
    primary: LimitWindow | None
    secondary: LimitWindow | None
    credit_balance: str | None
    reset_credit_available_count: int | None
    reset_credit_ids: tuple[str, ...]


@dataclass(frozen=True)
class Event:
    kind: str
    title: str
    body: str


class CodexAppServerClient:
    """Small JSONL client for the official local `codex app-server`."""

    def __init__(self, command: str, timeout_seconds: float = 25.0) -> None:
        self.command = shlex.split(command)
        if not self.command:
            raise MonitorError("CODEX_COMMAND is empty")
        self.timeout_seconds = timeout_seconds
        self.process: subprocess.Popen[str] | None = None
        self.stdout_queue: queue.Queue[str | None] = queue.Queue()
        self.stderr_tail: deque[str] = deque(maxlen=50)
        self._stdout_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._next_id = 1

    def __enter__(self) -> "CodexAppServerClient":
        try:
            self.process = subprocess.Popen(
                self.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                bufsize=1,
            )
        except FileNotFoundError as exc:
            raise MonitorError(
                f"Could not start {self.command[0]!r}. Install Codex CLI or set CODEX_COMMAND."
            ) from exc
        except OSError as exc:
            raise MonitorError(f"Could not start Codex app-server: {exc}") from exc

        assert self.process.stdout is not None
        assert self.process.stderr is not None

        def read_stdout() -> None:
            assert self.process is not None and self.process.stdout is not None
            for line in self.process.stdout:
                self.stdout_queue.put(line)
            self.stdout_queue.put(None)

        def read_stderr() -> None:
            assert self.process is not None and self.process.stderr is not None
            for line in self.process.stderr:
                self.stderr_tail.append(line.rstrip())

        self._stdout_thread = threading.Thread(target=read_stdout, daemon=True)
        self._stderr_thread = threading.Thread(target=read_stderr, daemon=True)
        self._stdout_thread.start()
        self._stderr_thread.start()

        self.call(
            "initialize",
            {
                "clientInfo": {
                    "name": "codex_reset_monitor",
                    "title": "Codex Reset Monitor",
                    "version": "1.0.0",
                }
            },
        )
        self.notify("initialized")
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self.process is None:
            return
        try:
            if self.process.stdin is not None:
                self.process.stdin.close()
        except OSError:
            pass
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)

    def _write(self, payload: dict[str, Any]) -> None:
        assert self.process is not None and self.process.stdin is not None
        if self.process.poll() is not None:
            raise MonitorError(self._exited_message())
        try:
            self.process.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
            self.process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise MonitorError(self._exited_message()) from exc

    def _exited_message(self) -> str:
        details = "\n".join(self.stderr_tail)
        suffix = f"\nCodex stderr:\n{details}" if details else ""
        return f"Codex app-server exited unexpectedly.{suffix}"

    def notify(self, method: str, params: Any = _MISSING) -> None:
        message: dict[str, Any] = {"method": method}
        if params is not _MISSING:
            message["params"] = params
        self._write(message)

    def call(self, method: str, params: Any = _MISSING) -> Any:
        request_id = self._next_id
        self._next_id += 1
        message: dict[str, Any] = {"method": method, "id": request_id}
        if params is not _MISSING:
            message["params"] = params
        self._write(message)

        deadline = time.monotonic() + self.timeout_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MonitorError(f"Timed out waiting for Codex response to {method!r}")
            try:
                line = self.stdout_queue.get(timeout=remaining)
            except queue.Empty as exc:
                raise MonitorError(f"Timed out waiting for Codex response to {method!r}") from exc
            if line is None:
                raise MonitorError(self._exited_message())
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                LOG.debug("Ignoring non-JSON app-server output: %s", line.rstrip())
                continue
            if payload.get("id") != request_id:
                LOG.debug("Ignoring unrelated app-server message: %s", payload)
                continue
            if "error" in payload:
                error = payload["error"]
                message_text = error.get("message", str(error)) if isinstance(error, dict) else str(error)
                if "authentication required" in message_text.lower():
                    raise MonitorError(
                        f"{message_text}. Open Codex once and sign in with your ChatGPT account."
                    )
                raise MonitorError(f"Codex app-server error: {message_text}")
            if "result" not in payload:
                raise MonitorError(f"Malformed Codex response: {payload}")
            return payload["result"]

    def read_rate_limits(self) -> dict[str, Any]:
        result = self.call("account/rateLimits/read")
        if not isinstance(result, dict):
            raise MonitorError("Codex returned an unexpected rate-limit payload")
        return result


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise MonitorError(f"Invalid line {number} in {path}: expected KEY=VALUE")
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _integer(value: Any) -> int | None:
    number = _number(value)
    return int(number) if number is not None else None


def _decimal_string(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return str(Decimal(str(value)))
    except (InvalidOperation, ValueError):
        return None


def _window(value: Any) -> LimitWindow | None:
    if not isinstance(value, dict):
        return None
    return LimitWindow(
        used_percent=_number(value.get("usedPercent")),
        resets_at=_integer(value.get("resetsAt")),
        window_duration_mins=_integer(value.get("windowDurationMins")),
    )


def snapshot_from_payload(payload: dict[str, Any], captured_at: int | None = None) -> Snapshot:
    rate_limits: Any = None
    by_id = payload.get("rateLimitsByLimitId")
    if isinstance(by_id, dict):
        rate_limits = by_id.get("codex")
        if not isinstance(rate_limits, dict) and by_id:
            rate_limits = next((v for v in by_id.values() if isinstance(v, dict)), None)
    if not isinstance(rate_limits, dict):
        rate_limits = payload.get("rateLimits")
    if not isinstance(rate_limits, dict):
        raise MonitorError("Rate-limit response did not contain rateLimits")

    credits = rate_limits.get("credits")
    credit_balance: str | None = None
    if isinstance(credits, dict):
        credit_balance = _decimal_string(credits.get("balance"))

    reset_summary = payload.get("rateLimitResetCredits")
    available_count: int | None = None
    reset_ids: list[str] = []
    if isinstance(reset_summary, dict):
        available_count = _integer(reset_summary.get("availableCount"))
        rows = reset_summary.get("credits")
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, dict) and row.get("status") in {None, "available"}:
                    identifier = row.get("id")
                    if isinstance(identifier, str):
                        reset_ids.append(identifier)

    return Snapshot(
        captured_at=captured_at or int(time.time()),
        primary=_window(rate_limits.get("primary")),
        secondary=_window(rate_limits.get("secondary")),
        credit_balance=credit_balance,
        reset_credit_available_count=available_count,
        reset_credit_ids=tuple(sorted(set(reset_ids))),
    )


def snapshot_to_dict(snapshot: Snapshot) -> dict[str, Any]:
    return asdict(snapshot)


def snapshot_from_dict(data: dict[str, Any]) -> Snapshot:
    def load_window(value: Any) -> LimitWindow | None:
        if not isinstance(value, dict):
            return None
        return LimitWindow(
            used_percent=_number(value.get("used_percent")),
            resets_at=_integer(value.get("resets_at")),
            window_duration_mins=_integer(value.get("window_duration_mins")),
        )

    ids = data.get("reset_credit_ids")
    return Snapshot(
        captured_at=int(data.get("captured_at", 0)),
        primary=load_window(data.get("primary")),
        secondary=load_window(data.get("secondary")),
        credit_balance=_decimal_string(data.get("credit_balance")),
        reset_credit_available_count=_integer(data.get("reset_credit_available_count")),
        reset_credit_ids=tuple(str(v) for v in ids) if isinstance(ids, list) else (),
    )


def format_timestamp(timestamp: int | None) -> str:
    if timestamp is None:
        return "unknown"
    return time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(timestamp))


def detect_events(previous: Snapshot, current: Snapshot) -> list[Event]:
    events: list[Event] = []
    min_drop = float(os.getenv("RESET_MIN_PERCENT_DROP", "1"))

    labels = {
        "primary": "short Codex usage window",
        "secondary": "long/weekly Codex usage window",
    }
    for field_name, label in labels.items():
        old = getattr(previous, field_name)
        new = getattr(current, field_name)
        if old is None or new is None:
            continue
        if old.used_percent is None or new.used_percent is None:
            continue
        reset_timestamp_advanced = (
            old.resets_at is not None
            and new.resets_at is not None
            and new.resets_at > old.resets_at + 60
        )
        usage_dropped = new.used_percent <= old.used_percent - min_drop
        if reset_timestamp_advanced and usage_dropped:
            events.append(
                Event(
                    kind=f"{field_name}_window_reset",
                    title=f"Codex {label} reset",
                    body=(
                        f"Usage changed from {old.used_percent:g}% used to "
                        f"{new.used_percent:g}% used. The next reset is "
                        f"{format_timestamp(new.resets_at)}."
                    ),
                )
            )

    old_count = previous.reset_credit_available_count
    new_count = current.reset_credit_available_count
    new_ids = set(current.reset_credit_ids) - set(previous.reset_credit_ids)
    if old_count is not None and new_count is not None and new_count > old_count:
        events.append(
            Event(
                kind="reset_credit_granted",
                title="New Codex reset credit available",
                body=f"Available reset credits increased from {old_count} to {new_count}.",
            )
        )
    elif new_ids:
        events.append(
            Event(
                kind="reset_credit_granted",
                title="New Codex reset credit available",
                body=f"Codex reported {len(new_ids)} newly available reset credit(s).",
            )
        )

    if previous.credit_balance is not None and current.credit_balance is not None:
        old_balance = Decimal(previous.credit_balance)
        new_balance = Decimal(current.credit_balance)
        if new_balance > old_balance:
            events.append(
                Event(
                    kind="credit_balance_increased",
                    title="Codex credit balance increased",
                    body=f"Credit balance changed from {old_balance} to {new_balance}.",
                )
            )

    return events


def load_state(path: Path) -> Snapshot | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("state is not an object")
        return snapshot_from_dict(data)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise MonitorError(f"Could not read state file {path}: {exc}") from exc


def save_state(path: Path, snapshot: Snapshot) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(snapshot_to_dict(snapshot), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def events_message(events: Iterable[Event]) -> tuple[str, str]:
    event_list = list(events)
    if len(event_list) == 1:
        return event_list[0].title, event_list[0].body
    subject = f"{len(event_list)} Codex usage changes detected"
    body = "\n\n".join(f"{event.title}\n{event.body}" for event in event_list)
    return subject, body


class EmailNotifier:
    def send(self, subject: str, body: str) -> None:
        host = os.getenv("SMTP_HOST", "smtp.gmail.com")
        port = int(os.getenv("SMTP_PORT", "465"))
        username = os.getenv("SMTP_USERNAME", "")
        password = os.getenv("SMTP_PASSWORD", "")
        sender = os.getenv("EMAIL_FROM", username)
        recipients = [v.strip() for v in os.getenv("EMAIL_TO", "").split(",") if v.strip()]
        if not username or not password or not sender or not recipients:
            raise MonitorError(
                "Email is enabled but SMTP_USERNAME, SMTP_PASSWORD, EMAIL_FROM/SMTP_USERNAME, "
                "or EMAIL_TO is missing"
            )

        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = sender
        message["To"] = ", ".join(recipients)
        message.set_content(body)

        context = ssl.create_default_context()
        if env_bool("SMTP_SSL", True):
            with smtplib.SMTP_SSL(host, port, timeout=30, context=context) as server:
                server.login(username, password)
                server.send_message(message)
        else:
            with smtplib.SMTP(host, port, timeout=30) as server:
                server.ehlo()
                if env_bool("SMTP_STARTTLS", True):
                    server.starttls(context=context)
                    server.ehlo()
                server.login(username, password)
                server.send_message(message)


class WhatsAppNotifier:
    """Send through Meta WhatsApp Cloud API.

    For proactive alerts, use an approved template with two body variables: title and body.
    """

    def send(self, subject: str, body: str) -> None:
        api_url = os.getenv("WHATSAPP_API_URL", "")
        token = os.getenv("WHATSAPP_ACCESS_TOKEN", "")
        recipient = os.getenv("WHATSAPP_TO", "")
        if not api_url or not token or not recipient:
            raise MonitorError(
                "WhatsApp is enabled but WHATSAPP_API_URL, WHATSAPP_ACCESS_TOKEN, or WHATSAPP_TO is missing"
            )

        template_name = os.getenv("WHATSAPP_TEMPLATE_NAME", "codex_reset_alert").strip()
        language = os.getenv("WHATSAPP_TEMPLATE_LANGUAGE", "en_US")
        if template_name:
            payload: dict[str, Any] = {
                "messaging_product": "whatsapp",
                "to": recipient,
                "type": "template",
                "template": {
                    "name": template_name,
                    "language": {"code": language},
                    "components": [
                        {
                            "type": "body",
                            "parameters": [
                                {"type": "text", "text": subject[:1024]},
                                {"type": "text", "text": body[:1024]},
                            ],
                        }
                    ],
                },
            }
        else:
            payload = {
                "messaging_product": "whatsapp",
                "to": recipient,
                "type": "text",
                "text": {"body": f"{subject}\n\n{body}"[:4096]},
            }

        request = urllib.request.Request(
            api_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                if response.status >= 300:
                    raise MonitorError(f"WhatsApp API returned HTTP {response.status}")
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            raise MonitorError(f"WhatsApp API returned HTTP {exc.code}: {error_body}") from exc
        except urllib.error.URLError as exc:
            raise MonitorError(f"Could not contact WhatsApp API: {exc.reason}") from exc


def configured_notifiers() -> list[tuple[str, Any]]:
    channels = [v.strip().lower() for v in os.getenv("NOTIFY_CHANNELS", "email").split(",") if v.strip()]
    result: list[tuple[str, Any]] = []
    for channel in channels:
        if channel == "email":
            result.append((channel, EmailNotifier()))
        elif channel == "whatsapp":
            result.append((channel, WhatsAppNotifier()))
        elif channel in {"stdout", "console"}:
            result.append((channel, None))
        else:
            raise MonitorError(f"Unknown notification channel: {channel}")
    return result


def send_notifications(subject: str, body: str) -> bool:
    successes = 0
    failures: list[str] = []
    notifiers = configured_notifiers()
    if not notifiers:
        raise MonitorError("No notification channel configured")
    for name, notifier in notifiers:
        try:
            if notifier is None:
                print(f"{subject}\n\n{body}")
            else:
                notifier.send(subject, body)
            successes += 1
            LOG.info("Notification sent through %s", name)
        except Exception as exc:  # Keep other configured channels working.
            failures.append(f"{name}: {exc}")
            LOG.error("Notification through %s failed: %s", name, exc)
    if successes == 0:
        raise MonitorError("All notification channels failed: " + "; ".join(failures))
    return True


def query_snapshot() -> Snapshot:
    command = os.getenv("CODEX_COMMAND", "codex app-server")
    timeout_seconds = float(os.getenv("CODEX_TIMEOUT_SECONDS", "25"))
    with CodexAppServerClient(command, timeout_seconds) as client:
        payload = client.read_rate_limits()
    return snapshot_from_payload(payload)


def run_once(state_path: Path) -> int:
    current = query_snapshot()
    previous = load_state(state_path)
    if previous is None:
        save_state(state_path, current)
        LOG.info("Baseline saved; no notification is sent on the first run")
        return 0

    events = detect_events(previous, current)
    if events:
        subject, body = events_message(events)
        send_notifications(subject, body)
        LOG.info("Detected %d event(s)", len(events))
    else:
        LOG.info("No Codex reset or credit increase detected")
    save_state(state_path, current)
    return 0


def default_state_path() -> Path:
    state_home = Path(os.getenv("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return state_home / "codex-reset-monitor" / "state.json"


def default_env_path() -> Path:
    config_home = Path(os.getenv("XDG_CONFIG_HOME", Path.home() / ".config"))
    return config_home / "codex-reset-monitor" / "config.env"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=default_env_path())
    parser.add_argument("--state-path", type=Path, default=default_state_path())
    parser.add_argument("--once", action="store_true", help="Check once (default)")
    parser.add_argument("--loop", action="store_true", help="Run continuously")
    parser.add_argument("--print-status", action="store_true", help="Print current normalized status")
    parser.add_argument("--test-notification", action="store_true", help="Send a test notification")
    parser.add_argument("--verbose", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        load_env_file(args.env_file)
        if args.test_notification:
            send_notifications(
                "Codex reset monitor test",
                "Your Codex reset monitor notification is configured correctly.",
            )
            return 0
        if args.print_status:
            print(json.dumps(snapshot_to_dict(query_snapshot()), indent=2, sort_keys=True))
            return 0
        if args.loop:
            interval = max(60, int(os.getenv("CHECK_INTERVAL_SECONDS", "300")))
            while True:
                try:
                    run_once(args.state_path)
                except Exception as exc:
                    LOG.error("Check failed: %s", exc)
                time.sleep(interval)
        return run_once(args.state_path)
    except KeyboardInterrupt:
        return 130
    except MonitorError as exc:
        LOG.error("%s", exc)
        return 2
    except Exception:
        LOG.exception("Unexpected failure")
        return 3


if __name__ == "__main__":
    sys.exit(main())
