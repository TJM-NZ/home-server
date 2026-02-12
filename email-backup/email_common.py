"""Shared utilities for email backup and cleanup.

Provides Gmail API authentication, SQLite database initialization,
label lookups, email parsing helpers, ntfy notifications, and
configuration from environment.
"""

import email
import hashlib
import logging
import os
import sqlite3
import sys
from pathlib import Path

import requests as http_requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("email-backup")

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]

# --- Configuration from environment ---

STORAGE_PATH = Path(os.environ.get("BACKUP_STORAGE_PATH", "/data"))
CREDENTIALS_FILE = Path(os.environ.get("GOOGLE_CREDENTIALS_FILE", "/config/credentials.json"))
TOKEN_FILE = Path(os.environ.get("GOOGLE_TOKEN_FILE", "/config/token.json"))
BACKUP_LABEL = os.environ.get("EMAIL_BACKUP_LABEL", "Backup")
KEEP_LABEL = os.environ.get("EMAIL_KEEP_LABEL", "Keep")
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "730"))
PAPERLESS_CONSUME_DIR = os.environ.get("PAPERLESS_CONSUME_DIR", "")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")

DB_PATH = STORAGE_PATH / "db" / "emails.db"
RAW_PATH = STORAGE_PATH / "raw"
ATTACHMENTS_PATH = STORAGE_PATH / "attachments"


def send_ntfy(title, message, priority="default", tags=""):
    """Send a notification via ntfy. Silently skips if NTFY_TOPIC is not set."""
    if not NTFY_TOPIC:
        return
    headers = {"Title": title, "Priority": priority}
    if tags:
        headers["Tags"] = tags
    try:
        http_requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message,
            headers=headers,
            timeout=10,
        )
    except Exception as e:
        log.warning("Failed to send ntfy notification: %s", e)


def get_gmail_service():
    """Authenticate and return a Gmail API service instance."""
    creds = None

    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            log.info("Refreshing expired token")
            creds.refresh(Request())
        else:
            if not CREDENTIALS_FILE.exists():
                log.error("No credentials.json found at %s", CREDENTIALS_FILE)
                log.error("See CLAUDE.md for setup instructions")
                sys.exit(1)
            flow = InstalledAppFlow.from_client_secrets_file(
                str(CREDENTIALS_FILE),
                SCOPES,
                redirect_uri='urn:ietf:wg:oauth:2.0:oob'
            )
            auth_url, _ = flow.authorization_url(prompt='consent')
            log.info("\n" + "="*80)
            log.info("Please visit this URL to authorize the application:")
            log.info(auth_url)
            log.info("="*80 + "\n")
            code = input("Enter the authorization code: ")
            flow.fetch_token(code=code)
            creds = flow.credentials

        TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_FILE.write_text(creds.to_json())
        log.info("Token saved to %s", TOKEN_FILE)

    return build("gmail", "v1", credentials=creds)


def init_db():
    """Initialize SQLite database with FTS5 full-text search."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS emails (
            message_id TEXT PRIMARY KEY,
            gmail_id TEXT UNIQUE,
            thread_id TEXT,
            subject TEXT,
            sender TEXT,
            recipients TEXT,
            date TEXT,
            date_epoch INTEGER,
            labels TEXT,
            snippet TEXT,
            body_text TEXT,
            has_attachments INTEGER DEFAULT 0,
            attachment_count INTEGER DEFAULT 0,
            raw_path TEXT,
            backed_up_at TEXT,
            deleted_from_gmail INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS attachments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id TEXT,
            filename TEXT,
            content_type TEXT,
            size_bytes INTEGER,
            sha256 TEXT,
            local_path TEXT,
            paperless_exported INTEGER DEFAULT 0,
            FOREIGN KEY (message_id) REFERENCES emails(message_id)
        );

        CREATE TABLE IF NOT EXISTS cleanup_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_at TEXT,
            cutoff_date TEXT,
            retention_days INTEGER,
            emails_deleted INTEGER,
            emails_kept INTEGER
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS emails_fts USING fts5(
            subject,
            sender,
            recipients,
            body_text,
            content=emails,
            content_rowid=rowid
        );

        CREATE TRIGGER IF NOT EXISTS emails_ai AFTER INSERT ON emails BEGIN
            INSERT INTO emails_fts(rowid, subject, sender, recipients, body_text)
            VALUES (new.rowid, new.subject, new.sender, new.recipients, new.body_text);
        END;

        CREATE TRIGGER IF NOT EXISTS emails_ad AFTER DELETE ON emails BEGIN
            INSERT INTO emails_fts(emails_fts, rowid, subject, sender, recipients, body_text)
            VALUES ('delete', old.rowid, old.subject, old.sender, old.recipients, old.body_text);
        END;

        CREATE TRIGGER IF NOT EXISTS emails_au AFTER UPDATE ON emails BEGIN
            INSERT INTO emails_fts(emails_fts, rowid, subject, sender, recipients, body_text)
            VALUES ('delete', old.rowid, old.subject, old.sender, old.recipients, old.body_text);
            INSERT INTO emails_fts(rowid, subject, sender, recipients, body_text)
            VALUES (new.rowid, new.subject, new.sender, new.recipients, new.body_text);
        END;
    """)

    conn.commit()
    return conn


def get_label_id(service, label_name):
    """Get the Gmail label ID for a given label name."""
    results = service.users().labels().list(userId="me").execute()
    for label in results.get("labels", []):
        if label["name"] == label_name:
            return label["id"]
    return None


def fetch_message(service, msg_id):
    """Fetch a full message by ID in raw format."""
    return service.users().messages().get(
        userId="me", id=msg_id, format="raw"
    ).execute()


def extract_body_text(msg):
    """Extract plain text body from an email.Message object."""
    if msg.is_multipart():
        parts = []
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and not part.get_filename():
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    try:
                        parts.append(payload.decode(charset, errors="replace"))
                    except LookupError:
                        log.warning("Unknown encoding '%s', falling back to UTF-8", charset)
                        parts.append(payload.decode("utf-8", errors="replace"))
        return "\n".join(parts)
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            try:
                return payload.decode(charset, errors="replace")
            except LookupError:
                log.warning("Unknown encoding '%s', falling back to UTF-8", charset)
                return payload.decode("utf-8", errors="replace")
    return ""


def extract_attachments(msg, message_id):
    """Extract attachments from an email, save to disk, return metadata list."""
    attachments = []

    for part in msg.walk():
        filename = part.get_filename()
        if not filename:
            continue

        content_type = part.get_content_type()
        payload = part.get_payload(decode=True)
        if not payload:
            continue

        sha256 = hashlib.sha256(payload).hexdigest()

        att_dir = ATTACHMENTS_PATH / message_id[:2] / message_id
        att_dir.mkdir(parents=True, exist_ok=True)

        safe_filename = "".join(
            c if c.isalnum() or c in ".-_ " else "_" for c in filename
        )
        local_path = att_dir / safe_filename
        local_path.write_bytes(payload)

        paperless_exported = False
        if PAPERLESS_CONSUME_DIR:
            consume_dir = Path(PAPERLESS_CONSUME_DIR)
            if consume_dir.exists():
                paperless_path = consume_dir / f"{message_id}_{safe_filename}"
                paperless_path.write_bytes(payload)
                paperless_exported = True

        attachments.append({
            "filename": filename,
            "content_type": content_type,
            "size_bytes": len(payload),
            "sha256": sha256,
            "local_path": str(local_path),
            "paperless_exported": paperless_exported,
        })

    return attachments


def parse_date_epoch(date_str):
    """Parse an email date string into a Unix epoch timestamp."""
    if not date_str:
        return 0
    try:
        parsed = email.utils.parsedate_to_datetime(date_str)
        return int(parsed.timestamp())
    except Exception:
        return 0


def print_stats(conn):
    """Print backup statistics."""
    stats = conn.execute("""
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN deleted_from_gmail = 1 THEN 1 ELSE 0 END) as deleted,
            SUM(has_attachments) as with_attachments,
            SUM(attachment_count) as total_attachments,
            MIN(date) as oldest,
            MAX(date) as newest
        FROM emails
    """).fetchone()

    total, deleted, with_att, total_att, oldest, newest = stats
    print(f"\nEmail Backup Statistics:")
    print(f"  Total emails backed up:  {total}")
    print(f"  Deleted from Gmail:      {deleted}")
    print(f"  Emails with attachments: {with_att}")
    print(f"  Total attachments:       {total_att}")
    print(f"  Date range:              {oldest} → {newest}")
    print()

    cleanup_history = conn.execute("""
        SELECT run_at, cutoff_date, retention_days, emails_deleted, emails_kept
        FROM cleanup_runs
        ORDER BY run_at DESC
        LIMIT 10
    """).fetchall()

    if cleanup_history:
        print(f"Recent Cleanup Runs:")
        for run_at, cutoff_date, retention_days, emails_deleted, emails_kept in cleanup_history:
            run_date = run_at[:10] if run_at else "Unknown"
            print(f"  {run_date} | Cutoff: {cutoff_date} ({retention_days}d) | Deleted: {emails_deleted} | Kept: {emails_kept}")
        print()
