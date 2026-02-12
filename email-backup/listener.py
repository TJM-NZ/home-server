#!/usr/bin/env python3
"""Email Backup Listener — listens for commands via ntfy.

Subscribes to a ntfy commands topic and triggers backup or cleanup
when a matching message is received.

Commands:
    backup emails   — Run email backup
    cleanup emails  — Run email cleanup
"""

import json
import os
import subprocess
import sys
import time

import requests as http_requests

from email_common import NTFY_TOPIC, log, send_ntfy

NTFY_COMMANDS_TOPIC = os.environ.get("NTFY_COMMANDS_TOPIC", f"{NTFY_TOPIC}-cmd")
NTFY_COMMAND_SECRET = os.environ.get("NTFY_COMMAND_SECRET", "")

COMMANDS = {
    "backup emails": {"script": "backup.py", "title": "Email Backup"},
    "cleanup emails": {"script": "cleanup.py", "title": "Email Cleanup"},
}

# Track whether a command is currently running to prevent overlap
_running = False


def run_command(script, title):
    """Run a script and send a notification with the result."""
    global _running

    if _running:
        log.warning("Ignoring '%s' — a command is already running", title)
        send_ntfy(title, "Ignored — a command is already running", tags="hourglass")
        return

    _running = True
    log.info("Running %s", script)
    send_ntfy(title, f"Starting {title.lower()}...", tags="arrow_forward")

    try:
        result = subprocess.run(
            [sys.executable, "-u", script],
            capture_output=True,
            text=True,
            timeout=3600,  # 1 hour max
        )

        if result.returncode == 0:
            log.info("%s completed successfully", title)
        else:
            log.error("%s failed (exit %d): %s", title, result.returncode, result.stderr[-500:] if result.stderr else "")
            send_ntfy(
                f"{title} Failed",
                f"Exited with code {result.returncode}",
                priority="high",
                tags="x",
            )

    except subprocess.TimeoutExpired:
        log.error("%s timed out after 1 hour", title)
        send_ntfy(f"{title} Failed", "Timed out after 1 hour", priority="high", tags="x")
    except Exception as e:
        log.exception("Error running %s", script)
        send_ntfy(f"{title} Failed", str(e), priority="high", tags="x")
    finally:
        _running = False


def validate_command(raw_message):
    """Validate command secret and return the command, or None if invalid."""
    if not NTFY_COMMAND_SECRET:
        log.warning("NTFY_COMMAND_SECRET not set, rejecting all commands")
        return None

    if ":" not in raw_message:
        log.warning("Rejected command (no secret): %s", raw_message)
        return None

    secret, _, command = raw_message.partition(":")
    if secret != NTFY_COMMAND_SECRET:
        log.warning("Rejected command (bad secret): %s", raw_message)
        return None

    return command.strip().lower()


def listen():
    """Subscribe to the ntfy commands topic and handle incoming messages."""
    log.info("Listening for commands on topic: %s", NTFY_COMMANDS_TOPIC)
    for cmd, info in COMMANDS.items():
        log.info("  '%s' -> %s", cmd, info["script"])

    while True:
        try:
            response = http_requests.get(
                f"https://ntfy.sh/{NTFY_COMMANDS_TOPIC}/json",
                stream=True,
                timeout=65,  # ntfy keepalive is 60s
            )

            for line in response.iter_lines():
                if not line:
                    continue

                try:
                    msg = json.loads(line.decode("utf-8"))
                except json.JSONDecodeError:
                    continue

                if msg.get("event") != "message":
                    continue

                raw = msg.get("message", "").strip()
                text = validate_command(raw)
                if text is None:
                    continue
                log.info("Received command: %s", text)

                if text in COMMANDS:
                    cmd = COMMANDS[text]
                    run_command(cmd["script"], cmd["title"])
                else:
                    log.info("Unknown command: %s", text)

        except Exception as e:
            log.warning("Listener error: %s, reconnecting in 10s...", e)

        time.sleep(10)


if __name__ == "__main__":
    listen()
