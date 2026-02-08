# Email Backup Service

Backs up Gmail emails labelled "Backup", stores them locally with full-text search,
extracts attachments for future Paperless-ngx integration, and periodically cleans
up old emails from Gmail.

## Google Cloud Setup (one-time)

1. Go to https://console.cloud.google.com/
2. Create a new project (e.g. "Home Server Email Backup")
3. Enable the **Gmail API**:
   - APIs & Services → Library → search "Gmail API" → Enable
4. Create OAuth 2.0 credentials:
   - APIs & Services → Credentials → Create Credentials → OAuth client ID
   - Application type: **Desktop app**
   - Download the JSON file
5. Configure OAuth consent screen:
   - User type: **External** (or Internal if using Workspace)
   - Add your Gmail address as a test user
   - Add scope: `https://www.googleapis.com/auth/gmail.modify`

## Initial Setup

```bash
cd ~/home-server/email-backup

# Copy env template
cp .env.example .env
# Edit .env with your storage path and settings

# Create config directory and add credentials
mkdir -p config
cp ~/Downloads/client_secret_*.json config/credentials.json

# Build the container
docker compose build

# First run — will open a browser for OAuth consent
# (run interactively for the initial auth flow)
docker compose run --rm email-backup backup
```

After the first run, `config/token.json` is created and subsequent runs
authenticate automatically.

## Gmail Labels

Create these labels in Gmail:
- **Backup** — emails you want backed up (customizable via `EMAIL_BACKUP_LABEL`)
- **Keep** — emails that should never be deleted from Gmail (customizable via `EMAIL_KEEP_LABEL`)

## Usage

```bash
# Run a backup (download new Backup emails)
docker compose run --rm email-backup backup

# Run cleanup (delete emails >2 years old from Gmail, unless labelled Keep)
docker compose run --rm email-backup cleanup

# Search backed-up emails
docker compose run --rm email-backup search "invoice"

# View statistics
docker compose run --rm email-backup stats
```

## Scheduling with Systemd Timers

The repo includes systemd service and timer files for automated backups:
- **Weekly backup** - Mondays at 2:00 AM
- **Monthly cleanup** - 1st of month at 3:00 AM

### Installation

```bash
# Create user systemd directory if it doesn't exist
mkdir -p ~/.config/systemd/user

# Symlink the service and timer files
ln -sf ~/home-server/email-backup/email-backup.service ~/.config/systemd/user/
ln -sf ~/home-server/email-backup/email-backup.timer ~/.config/systemd/user/
ln -sf ~/home-server/email-backup/email-backup-cleanup.service ~/.config/systemd/user/
ln -sf ~/home-server/email-backup/email-backup-cleanup.timer ~/.config/systemd/user/

# Reload systemd and enable timers
systemctl --user daemon-reload
systemctl --user enable --now email-backup.timer
systemctl --user enable --now email-backup-cleanup.timer
```

### Check Status

```bash
# View timer status
systemctl --user list-timers

# Check service logs
journalctl --user -u email-backup.service
journalctl --user -u email-backup-cleanup.service

# Manually trigger a backup (for testing)
systemctl --user start email-backup.service
```

## Storage Layout

```
$BACKUP_STORAGE_PATH/
├── db/
│   └── emails.db             # SQLite with FTS5 full-text search index
├── raw/
│   └── 2026/01/
│       └── <hash>.eml        # Raw email files organized by year/month
└── attachments/
    └── <hash>/
        └── document.pdf      # Extracted attachments
```

## Future: Paperless-ngx Integration

When Paperless is set up:
1. Set `PAPERLESS_CONSUME_DIR` in `.env` to the Paperless consume directory
2. Uncomment the volume mount in `docker-compose.yml`
3. Attachments will automatically be exported to Paperless on backup

## Useful Commands

```bash
# Check backup database directly
docker compose run --rm email-backup python -c "
import sqlite3
conn = sqlite3.connect('/data/db/emails.db')
for row in conn.execute('SELECT COUNT(*), SUM(attachment_count) FROM emails'):
    print(f'Emails: {row[0]}, Attachments: {row[1]}')
"

# Rebuild container after code changes
docker compose build --no-cache
```
