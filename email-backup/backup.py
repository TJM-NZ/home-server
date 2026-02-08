#!/usr/bin/env python3
"""Email Backup Service

Backs up Gmail emails with a specified label, stores them in SQLite with
full-text search, extracts attachments for document management, and
performs monthly cleanup of old emails from Gmail.

Usage:
    python backup.py backup     # Run backup of labelled emails
    python backup.py cleanup    # Delete old emails from Gmail (keeps local backup)
    python backup.py search <query>  # Search backed-up emails
"""

import base64
import email
import hashlib
import json
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from email import policy
from pathlib import Path

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

DB_PATH = STORAGE_PATH / "db" / "emails.db"
RAW_PATH = STORAGE_PATH / "raw"
ATTACHMENTS_PATH = STORAGE_PATH / "attachments"


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
                        # Unknown/invalid encoding, fall back to UTF-8
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
                # Unknown/invalid encoding, fall back to UTF-8
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

        # Organize by message_id to avoid collisions
        att_dir = ATTACHMENTS_PATH / message_id[:2] / message_id
        att_dir.mkdir(parents=True, exist_ok=True)

        # Sanitize filename
        safe_filename = "".join(
            c if c.isalnum() or c in ".-_ " else "_" for c in filename
        )
        local_path = att_dir / safe_filename
        local_path.write_bytes(payload)

        # Export to Paperless consume dir if configured
        paperless_exported = False
        if PAPERLESS_CONSUME_DIR:
            consume_dir = Path(PAPERLESS_CONSUME_DIR)
            if consume_dir.exists():
                # Prefix with date and sender for Paperless matching
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


def backup_emails(service, conn):
    """Back up all emails with the configured label."""
    label_id = get_label_id(service, BACKUP_LABEL)
    if not label_id:
        log.error("Label '%s' not found in Gmail", BACKUP_LABEL)
        sys.exit(1)

    log.info("Backing up emails with label '%s' (id: %s)", BACKUP_LABEL, label_id)

    RAW_PATH.mkdir(parents=True, exist_ok=True)
    ATTACHMENTS_PATH.mkdir(parents=True, exist_ok=True)

    page_token = None
    total_backed_up = 0
    total_skipped = 0

    while True:
        results = service.users().messages().list(
            userId="me", labelIds=[label_id], pageToken=page_token, maxResults=100
        ).execute()

        messages = results.get("messages", [])
        if not messages:
            break

        for msg_summary in messages:
            gmail_id = msg_summary["id"]

            # Skip if already backed up
            row = conn.execute(
                "SELECT 1 FROM emails WHERE gmail_id = ?", (gmail_id,)
            ).fetchone()
            if row:
                total_skipped += 1
                continue

            try:
                raw_msg = fetch_message(service, gmail_id)
                # Gmail API returns the raw email in the 'raw' field
                raw_bytes = base64.urlsafe_b64decode(raw_msg["raw"])

                # Parse email
                msg = email.message_from_bytes(raw_bytes, policy=policy.default)

                message_id = msg.get("Message-ID", gmail_id)
                # Clean message_id for filesystem use
                fs_message_id = hashlib.sha256(message_id.encode()).hexdigest()[:16]

                subject = msg.get("Subject", "(no subject)")
                sender = msg.get("From", "")
                recipients = msg.get("To", "")
                date_str = msg.get("Date", "")
                date_epoch = parse_date_epoch(date_str)
                snippet = raw_msg.get("snippet", "")
                labels = json.dumps(raw_msg.get("labelIds", []))
                body_text = extract_body_text(msg)
                thread_id = raw_msg.get("threadId", "")

                # Save raw .eml
                if date_epoch:
                    dt = datetime.fromtimestamp(date_epoch, tz=timezone.utc)
                    year_month = dt.strftime("%Y/%m")
                else:
                    year_month = "unknown"

                eml_dir = RAW_PATH / year_month
                eml_dir.mkdir(parents=True, exist_ok=True)
                eml_path = eml_dir / f"{fs_message_id}.eml"
                eml_path.write_bytes(raw_bytes)

                # Extract attachments
                attachments = extract_attachments(msg, fs_message_id)

                # Insert into DB
                conn.execute("""
                    INSERT INTO emails
                        (message_id, gmail_id, thread_id, subject, sender,
                         recipients, date, date_epoch, labels, snippet,
                         body_text, has_attachments, attachment_count,
                         raw_path, backed_up_at, deleted_from_gmail)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                """, (
                    message_id, gmail_id, thread_id, subject, sender,
                    recipients, date_str, date_epoch, labels, snippet,
                    body_text, 1 if attachments else 0, len(attachments),
                    str(eml_path), datetime.now(timezone.utc).isoformat(),
                ))

                for att in attachments:
                    conn.execute("""
                        INSERT INTO attachments
                            (message_id, filename, content_type, size_bytes,
                             sha256, local_path, paperless_exported)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, (
                        message_id, att["filename"], att["content_type"],
                        att["size_bytes"], att["sha256"], att["local_path"],
                        1 if att["paperless_exported"] else 0,
                    ))

                conn.commit()
                total_backed_up += 1
                log.info("Backed up: %s — %s", sender, subject)

            except Exception:
                log.exception("Failed to back up message %s", gmail_id)
                continue

        page_token = results.get("nextPageToken")
        if not page_token:
            break

    log.info(
        "Backup complete: %d new, %d already existed",
        total_backed_up, total_skipped,
    )


def cleanup_old_emails(service, conn):
    """Delete ALL emails from Gmail that are older than RETENTION_DAYS and not labelled Keep."""
    keep_label_id = get_label_id(service, KEEP_LABEL)

    cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
    cutoff_epoch = int(cutoff.timestamp())
    # Format for Gmail search query (YYYY/MM/DD)
    cutoff_date_str = cutoff.strftime("%Y/%m/%d")

    log.info(
        "Cleaning up ALL emails from Gmail older than %s (%d days)",
        cutoff.strftime("%Y-%m-%d"), RETENTION_DAYS,
    )

    deleted = 0
    kept = 0
    page_token = None

    # Query Gmail directly for all emails before the cutoff date
    while True:
        try:
            results = service.users().messages().list(
                userId="me",
                q=f"before:{cutoff_date_str}",
                pageToken=page_token,
                maxResults=100
            ).execute()

            messages = results.get("messages", [])
            if not messages:
                break

            for msg_summary in messages:
                gmail_id = msg_summary["id"]

                try:
                    # Get message metadata to check labels
                    msg = service.users().messages().get(
                        userId="me", id=gmail_id, format="metadata",
                        metadataHeaders=["Subject", "From", "Date"],
                    ).execute()

                    current_labels = msg.get("labelIds", [])

                    # Skip if email has the Keep label
                    if keep_label_id and keep_label_id in current_labels:
                        kept += 1
                        continue

                    # Extract metadata for logging
                    headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
                    subject = headers.get("Subject", "(no subject)")
                    sender = headers.get("From", "(unknown)")
                    date_str = headers.get("Date", "(no date)")

                    # Delete from Gmail (move to trash)
                    service.users().messages().trash(userId="me", id=gmail_id).execute()

                    # Update database if this email was backed up
                    conn.execute(
                        "UPDATE emails SET deleted_from_gmail = 1 WHERE gmail_id = ?",
                        (gmail_id,),
                    )
                    conn.commit()

                    deleted += 1
                    log.info("Deleted from Gmail: %s — %s (%s)", sender, subject, date_str)

                except Exception:
                    log.exception("Failed to process message %s for cleanup", gmail_id)
                    continue

            page_token = results.get("nextPageToken")
            if not page_token:
                break

        except Exception:
            log.exception("Failed to list messages for cleanup")
            break

    # Log this cleanup run
    conn.execute("""
        INSERT INTO cleanup_runs (run_at, cutoff_date, retention_days, emails_deleted, emails_kept)
        VALUES (?, ?, ?, ?, ?)
    """, (
        datetime.now(timezone.utc).isoformat(),
        cutoff.strftime("%Y-%m-%d"),
        RETENTION_DAYS,
        deleted,
        kept,
    ))
    conn.commit()

    log.info("Cleanup complete: %d deleted from Gmail, %d kept", deleted, kept)


def search_emails(conn, query):
    """Search backed-up emails using full-text search."""
    results = conn.execute("""
        SELECT e.subject, e.sender, e.date, e.snippet, e.has_attachments
        FROM emails_fts fts
        JOIN emails e ON e.rowid = fts.rowid
        WHERE emails_fts MATCH ?
        ORDER BY e.date_epoch DESC
        LIMIT 50
    """, (query,)).fetchall()

    if not results:
        print(f"No results for: {query}")
        return

    print(f"\n{'='*80}")
    print(f"Search results for: {query} ({len(results)} found)")
    print(f"{'='*80}\n")

    for subject, sender, date_str, snippet, has_att in results:
        att_marker = " [+att]" if has_att else ""
        print(f"  Date:    {date_str}")
        print(f"  From:    {sender}")
        print(f"  Subject: {subject}{att_marker}")
        print(f"  Preview: {snippet[:120]}")
        print()


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

    # Show recent cleanup runs
    cleanup_history = conn.execute("""
        SELECT run_at, cutoff_date, retention_days, emails_deleted, emails_kept
        FROM cleanup_runs
        ORDER BY run_at DESC
        LIMIT 10
    """).fetchall()

    if cleanup_history:
        print(f"Recent Cleanup Runs:")
        for run_at, cutoff_date, retention_days, emails_deleted, emails_kept in cleanup_history:
            # Parse ISO timestamp and format nicely
            run_date = run_at[:10] if run_at else "Unknown"
            print(f"  {run_date} | Cutoff: {cutoff_date} ({retention_days}d) | Deleted: {emails_deleted} | Kept: {emails_kept}")
        print()


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    command = sys.argv[1]

    conn = init_db()

    if command == "backup":
        service = get_gmail_service()
        backup_emails(service, conn)
        print_stats(conn)

    elif command == "cleanup":
        service = get_gmail_service()
        cleanup_old_emails(service, conn)
        print_stats(conn)

    elif command == "search":
        if len(sys.argv) < 3:
            print("Usage: python backup.py search <query>")
            sys.exit(1)
        query = " ".join(sys.argv[2:])
        search_emails(conn, query)

    elif command == "stats":
        print_stats(conn)

    else:
        print(f"Unknown command: {command}")
        print(__doc__)
        sys.exit(1)

    conn.close()


if __name__ == "__main__":
    main()
