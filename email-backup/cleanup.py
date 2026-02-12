#!/usr/bin/env python3
"""Email Cleanup — deletes old emails from Gmail while keeping local backups.

Finds ALL emails in Gmail older than RETENTION_DAYS (default 2 years),
checks for the "Keep" label, and trashes unprotected emails.
Local backups are never deleted.

Usage:
    python cleanup.py            # Run cleanup
    python cleanup.py --dry-run  # Preview what would be deleted
"""

import sys
from datetime import datetime, timedelta, timezone

from email_common import (
    KEEP_LABEL,
    RETENTION_DAYS,
    get_gmail_service,
    get_label_id,
    init_db,
    log,
    print_stats,
    send_ntfy,
)


def cleanup_old_emails(service, conn, dry_run=False):
    """Delete ALL emails from Gmail that are older than RETENTION_DAYS and not labelled Keep."""
    keep_label_id = get_label_id(service, KEEP_LABEL)

    cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
    cutoff_epoch = int(cutoff.timestamp())
    cutoff_date_str = cutoff.strftime("%Y/%m/%d")

    log.info(
        "Cleaning up ALL emails from Gmail older than %s (%d days)%s",
        cutoff.strftime("%Y-%m-%d"), RETENTION_DAYS,
        " [DRY RUN]" if dry_run else "",
    )

    deleted = 0
    kept = 0
    page_token = None

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
                    msg = service.users().messages().get(
                        userId="me", id=gmail_id, format="metadata",
                        metadataHeaders=["Subject", "From", "Date"],
                    ).execute()

                    current_labels = msg.get("labelIds", [])

                    if keep_label_id and keep_label_id in current_labels:
                        kept += 1
                        continue

                    headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
                    subject = headers.get("Subject", "(no subject)")
                    sender = headers.get("From", "(unknown)")
                    date_str = headers.get("Date", "(no date)")

                    if dry_run:
                        log.info("[DRY RUN] Would delete: %s — %s (%s)", sender, subject, date_str)
                        deleted += 1
                        continue

                    service.users().messages().trash(userId="me", id=gmail_id).execute()

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
    if not dry_run:
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

    action = "Would delete" if dry_run else "Deleted"
    log.info("Cleanup complete: %d %s from Gmail, %d kept", deleted, action.lower(), kept)

    if not dry_run:
        send_ntfy(
            "Email Cleanup Complete",
            f"Deleted {deleted} emails from Gmail ({kept} kept)",
            tags="wastebasket",
        )


def main():
    dry_run = "--dry-run" in sys.argv

    conn = init_db()
    service = get_gmail_service()

    cleanup_old_emails(service, conn, dry_run=dry_run)
    print_stats(conn)

    conn.close()


if __name__ == "__main__":
    main()
