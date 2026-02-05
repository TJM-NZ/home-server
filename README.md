# Home Server

Docker-based home server configuration for media and security cameras.

## Services

| Service | Port | Description |
|---------|------|-------------|
| Immich | 2283 | Photo backup & management |
| Frigate | 5000 | NVR / security camera system |
| Mosquitto | 1883 | MQTT broker (used by Frigate) |

## Setup

### Frigate

1. Copy the env template and fill in your camera password:
   ```bash
   cp frigate/.env.example frigate/.env
   ```
2. Start the stack:
   ```bash
   cd frigate
   docker compose up -d
   ```

The `FRIGATE_CAMERA_PASSWORD` variable in `.env` is substituted into `config/config.yml` for the go2rtc RTSP streams. Frigate uses `{FRIGATE_VAR}` syntax (no `$`) and all env vars must be prefixed with `FRIGATE_`. The password must be URL-encoded manually for go2rtc streams (e.g. `!` becomes `%21`, `&` becomes `%26`).

### Immich

1. Copy the env template and set your database password:
   ```bash
   cp immich/.env.example immich/.env
   ```
2. Start the stack:
   ```bash
   cd immich
   docker compose up -d
   ```

See `immich/CLAUDE.md` for a full runbook.

### Frigate Alerts (ntfy)

The `frigate-alerts` sidecar (`frigate/frigate-alerts/`) sends push notifications via [ntfy](https://ntfy.sh) when Frigate detects a person or car. It also monitors Frigate's cache disk usage and accepts remote commands (restart, clear-cache, status) via a separate ntfy topic.

It is included in the Frigate docker-compose stack — no separate setup needed.

## Storage

Both services expect dedicated mount points for media storage:

| Mount Point | Used By |
|-------------|---------|
| `/mnt/frigate-storage` | Frigate recordings & snapshots |
| `/mnt/immich-storage` | Immich photo uploads |
