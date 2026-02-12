#!/usr/bin/env python3
"""Email Backup — downloads emails and extracts attachments.

Usage:
    python backup.py             # Run backup of labelled emails
    python backup.py search <q>  # Search backed-up emails
    python backup.py stats       # Show backup statistics
"""

import base64
import email
import hashlib
import json
import sys
from datetime import datetime, timezone
from email import policy

from email_common import (
    ATTACHMENTS_PATH,
    BACKUP_LABEL,
    RAW_PATH,
    extract_attachments,
    extract_body_text,
    fetch_message,
    get_gmail_service,
    get_label_id,
    init_db,
    log,
    parse_date_epoch,
    print_stats,
    send_ntfy,
)


def backup_emails(service, conn):
    """Back up all emails with the configured label."""
    label_id = get_label_id(service, BACKUP_LABEL)
    if not label_id:
        log.error("Label '%s' not found in Gmail", BACKUP_LABEL)
        send_ntfy(
            "Email Backup Failed",
            f"Label '{BACKUP_LABEL}' not found in Gmail",
            priority="high",
            tags="x",
        )
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

                msg = email.message_from_bytes(raw_bytes, policy=policy.default)

                message_id = msg.get("Message-ID", gmail_id)
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

                if date_epoch:
                    dt = datetime.fromtimestamp(date_epoch, tz=timezone.utc)
                    year_month = dt.strftime("%Y/%m")
                else:
                    year_month = "unknown"

                eml_dir = RAW_PATH / year_month
                eml_dir.mkdir(parents=True, exist_ok=True)
                eml_path = eml_dir / f"{fs_message_id}.eml"
                eml_path.write_bytes(raw_bytes)

                attachments = extract_attachments(msg, fs_message_id)

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

            except Exception as exc:
                log.exception("Failed to back up message %s", gmail_id)
                send_ntfy(
                    "Email Backup Error",
                    f"Failed to back up message {gmail_id}: {exc}",
                    priority="high",
                    tags="warning",
                )
                continue

        page_token = results.get("nextPageToken")
        if not page_token:
            break

    log.info(
        "Backup complete: %d new, %d already existed",
        total_backed_up, total_skipped,
    )
    send_ntfy(
        "Email Backup Complete",
        f"Backed up {total_backed_up} new emails ({total_skipped} already existed)",
        tags="white_check_mark",
    )


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


def main():
    if len(sys.argv) < 2:
        command = "backup"
    else:
        command = sys.argv[1]

    conn = init_db()

    if command == "backup":
        service = get_gmail_service()
        backup_emails(service, conn)
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
