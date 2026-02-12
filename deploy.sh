#!/bin/bash
# Pull latest from GitHub and deploy changed files.
# Run via cron every minute — exits immediately if nothing changed.

set -euo pipefail

REPO_DIR="$HOME/home-server"
cd "$REPO_DIR"

# ntfy topics for deploy notifications (main topics, not command topics)
FRIGATE_NTFY_TOPIC="frigate-manse"
EMAIL_NTFY_TOPIC="email-backup"

notify_deploy() {
    local topic="$1"
    local title="$2"
    local message="$3"
    curl -s -o /dev/null \
        -H "Title: $title" \
        -H "Tags: package" \
        -d "$message" \
        "https://ntfy.sh/$topic" || true
}

BEFORE=$(git rev-parse HEAD)
git pull --quiet origin master
AFTER=$(git rev-parse HEAD)

# Nothing new
[ "$BEFORE" = "$AFTER" ] && exit 0

CHANGED=$(git diff --name-only "$BEFORE" "$AFTER")
echo "[deploy] Changes detected: $CHANGED"

# --- frigate-alerts (just copy and restart, no rebuild) ---
if echo "$CHANGED" | grep -qE "^(frigate/frigate-alerts/alerts.py|version.sh|VERSION)$"; then
    cp "$REPO_DIR/frigate/frigate-alerts/alerts.py" "$HOME/frigate/frigate-alerts/alerts.py"
    cp "$REPO_DIR/version.sh" "$HOME/frigate/frigate-alerts/version.sh"
    cp "$REPO_DIR/VERSION" "$HOME/frigate/frigate-alerts/VERSION"
    docker restart frigate-alerts
    echo "[deploy] Restarted frigate-alerts"
    notify_deploy "$FRIGATE_NTFY_TOPIC" "Frigate Alerts Deployed" "Codebase updated and service restarted"
fi

# --- frigate config or docker-compose ---
if echo "$CHANGED" | grep -qE "^frigate/(config/config\.yml|docker-compose\.yml|mosquitto/)"; then
    cp "$REPO_DIR/frigate/config/config.yml"          "$HOME/frigate/config/config.yml"
    cp "$REPO_DIR/frigate/docker-compose.yml"         "$HOME/frigate/docker-compose.yml"
    cp "$REPO_DIR/frigate/mosquitto/config/mosquitto.conf" "$HOME/frigate/mosquitto/config/mosquitto.conf"
    cd "$HOME/frigate"
    docker compose up -d
    echo "[deploy] Restarted frigate stack"
    notify_deploy "$FRIGATE_NTFY_TOPIC" "Frigate Stack Deployed" "Config updated and stack restarted"
fi

# --- email-backup (rebuild and restart) ---
if echo "$CHANGED" | grep -q "^email-backup/"; then
    cp "$REPO_DIR/email-backup/email_common.py"     "$HOME/email-backup/email_common.py"
    cp "$REPO_DIR/email-backup/backup.py"           "$HOME/email-backup/backup.py"
    cp "$REPO_DIR/email-backup/cleanup.py"          "$HOME/email-backup/cleanup.py"
    cp "$REPO_DIR/email-backup/docker-compose.yml"  "$HOME/email-backup/docker-compose.yml"
    cp "$REPO_DIR/email-backup/Dockerfile"          "$HOME/email-backup/Dockerfile"
    cd "$HOME/email-backup"
    docker compose build --quiet
    echo "[deploy] Rebuilt email-backup"
    notify_deploy "$EMAIL_NTFY_TOPIC" "Email Backup Deployed" "Codebase updated and service rebuilt"
fi

# --- immich docker-compose ---
if echo "$CHANGED" | grep -q "^immich/docker-compose.yml$"; then
    cp "$REPO_DIR/immich/docker-compose.yml" "$HOME/immich/docker-compose.yml"
    cd "$HOME/immich"
    docker compose up -d
    echo "[deploy] Restarted immich stack"
    # Add immich ntfy topic here when available
fi
