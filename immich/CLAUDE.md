# Immich Photo NAS Setup

## Project Goal
Set up Immich as a NAS solution for backing up photos from Android phones.

## Current Status

### Completed
- Docker Compose configuration files created (docker-compose.yml and .env)
- Timezone configured: Pacific/Auckland
- Database password generated
- All Docker images downloaded
- Storage drive mounted: `/mnt/immich-storage` (1.8TB)
- Immich containers started and running

### Current Configuration
- Upload location: `/mnt/immich-storage` (1.7TB available)
- Database location: `./postgres` (relative to immich directory)
- Immich version: v2
- Port: 2283
- Server URL: `http://localhost:2283`

### Storage Layout
- `/dev/nvme0n1` (238GB): System disk (NVMe) mounted at `/` — Immich DB stored here
- `/dev/sda1` (1.8TB): Mounted at `/mnt/immich-storage` for Immich photos
- `/dev/sdb1` (1.8TB): Mounted at `/mnt/frigate-storage` for Frigate NVR
- `/dev/sdc` (300GB): Old system disk (kept for reference)

### Running Containers
- immich_server (port 2283)
- immich_postgres (database)
- immich_redis (cache)
- immich_machine_learning (ML features)

## Next Steps

### 1. Initial Setup (Do This First)
- Open browser to `http://localhost:2283`
- Complete initial setup wizard
- Create admin account

### 2. Configure Android Phones
- Install Immich mobile app from Google Play Store
- Point to server IP address (port 2283)
- Login with admin account
- Enable automatic backup

## Useful Commands

```bash
# Check status
docker compose -f ~/immich/docker-compose.yml ps

# View logs
docker compose -f ~/immich/docker-compose.yml logs -f immich-server

# Stop / Start
docker compose -f ~/immich/docker-compose.yml down
docker compose -f ~/immich/docker-compose.yml up -d

# Update
docker compose -f ~/immich/docker-compose.yml pull
docker compose -f ~/immich/docker-compose.yml down
docker compose -f ~/immich/docker-compose.yml up -d

# Check disk usage
df -h /mnt/immich-storage
```

## Notes
- Database stored locally on system disk (`./postgres` directory)
- Photos stored on dedicated 1.8TB drive at `/mnt/immich-storage`
- All containers configured to restart automatically
- Credentials are in `.env` — do not commit to git
