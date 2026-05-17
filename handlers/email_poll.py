"""
Email poller — fetch new mails from Outlook, classify them with Gemini,
write actionable ones to Notion as commitments, DM Prabhu a batch summary.

Triggered by Cloud Scheduler job `poll-email` every 30 min.

Algorithm:
  1. Compute `since` = last successful poll's high-water timestamp.
  2. Graph: list messages received after `since`, newest first, cap 50.
  3. For each: send subject + preview to Gemini for triage.
  4. action_needed → write commitment(s) to Notion (dedup'd).
  5. Record the newest receivedDateTime as the new high-water mark.
  6. DM a one-message summary: "📨 Email sweep — 12 new, 3 action, 9 fyi".

High-water mark is stored as a Secret Manager secret `email-poll-high-water`
(ISO 8601 UTC). On first run it falls back to 12 hours ago to avoid
classifying the entire inbox.
"""

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from lib import email_outlook as outlook
from lib import llm
from lib import notion_writer
from lib import telegram_client as tg
from lib.prompts import EMAIL_TRIAGE_PROMPT, EMAIL_TRIAGE_SYSTEM

log = logging.getLogger(__name__)

# Cap on how many emails to classify per poll. Each classification = one
# Gemini call. With our 30-min cadence, 30 covers normal volume cleanly.
MAX_EMAILS_PER_POLL = 30

# How far back to look on the very first run (no high-water mark yet).
FIRST_RUN_LOOKBACK_HOURS = 12

HIGH_WATER_SECRET = "email-poll-high-water"


def _read_high_water() -> datetime:
    """Last poll's high-water mark, or first-run default."""
    raw = os.environ.get("EMAIL_POLL_HIGH_WATER", "")
    if raw:
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            log.warning("malformed EMAIL_POLL_HIGH_WATER=%r, using default", raw)
    return datetime.now(timezone.utc) - timedelta(hours=FIRST_RUN_LOOKBACK_HOURS)


def _write_high_water(ts: datetime) -> None:
    """Persist the new high-water mark to Secret Manager."""
    project_id = os.environ.get("GCP_PROJECT_ID")
    if not project_id:
        log.warning("GCP_PROJECT_ID missing, skipping high-water write")
        return
    iso = ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        from google.cloud import secretmanager
        client = secretmanager.SecretManagerServiceClient()
        parent = f"projects/{project_id}/secrets/{HIGH_WATER_SECRET}"
        payload = {"data": iso.encode("utf-8")}
        try:
            client.add_secret_version(parent=parent, payload=payload)
        except Exception:
            # Create then add
            client.create_secret(
                parent=f"projects/{project_id}",
                secret_id=HIGH_WATER_SECRET,
                secret={"replication": {"automatic": {}}},
            )
            client.add_secret_version(parent=parent, payload=payload)
        # Also update process env so subsequent polls in the same warm
        # instance see the new value without restarting.
        os.environ["EMAIL_POLL_HIGH_WATER"] = iso
        log.info("email high-water mark advanced to %s", iso)
    except Exception:
        log.exception("failed to persist email high-water mark (non-fatal)")


def _sender_str(message: dict) -> str:
    f = (message.get("from") or {}).get("emailAddress") or {}
    name = f.get("name") or ""
    addr = f.get("address") or ""
    return f"{name} <{addr}>" if name else addr or "(unknown)"


async def _triage_one(message: dict, today_iso: str) -> dict:
    """Classify a single message via Gemini. Returns parsed JSON."""
    subject = message.get("subject", "(no subject)")
    body = message.get("bodyPreview", "")[:1500]  # truncate long previews
    received = message.get("receivedDateTime", "")
    importance = message.get("importance", "normal")
    sender = _sender_str(message)

    prompt = EMAIL_TRIAGE_PROMPT.format(
        today=today_iso,
        sender=sender,
        subject=subject,
        subject_quoted=subject.replace('"', "'"),
        received=received,
        importance=importance,
        body=body or "(no preview)",
    )
    return await llm.reason_about(prompt, system=EMAIL_TRIAGE_SYSTEM)


async def run_email_poll() -> dict:
    """Top-level entry point — called by /cron/poll-email."""
    since = _read_high_water()
    log.info("email poll: fetching messages since %s", since.isoformat())

    try:
        messages = await outlook.list_recent_messages(since, limit=MAX_EMAILS_PER_POLL)
    except Exception as e:
        log.exception("failed to list Outlook messages")
        return {"error": f"list failed: {e}", "processed": 0}

    if not messages:
        log.info("email poll: no new messages")
        return {"new": 0, "action_needed": 0, "fyi": 0, "noise": 0}

    log.info("email poll: fetched %d new messages", len(messages))

    today_iso = datetime.now(timezone.utc).date().isoformat()
    action_count = 0
    fyi_count = 0
    noise_count = 0
    captured_titles: list[str] = []
    newest_received: Optional[datetime] = None

    for msg in messages:
        try:
            # Track the newest receivedDateTime so we can advance the
            # high-water mark even if classification fails for some.
            recv_iso = msg.get("receivedDateTime", "")
            if recv_iso:
                recv_dt = datetime.fromisoformat(recv_iso.replace("Z", "+00:00"))
                if not newest_received or recv_dt > newest_received:
                    newest_received = recv_dt

            triage = await _triage_one(msg, today_iso)
        except Exception:
            log.exception("triage failed for message %s", msg.get("id"))
            continue

        category = triage.get("category", "noise")
        if category == "action_needed":
            action_count += 1
            # Write each extracted commitment
            for commitment in triage.get("commitments", []):
                commitment.setdefault("counterparty", _sender_str(msg))
                commitment.setdefault("channel", "Email")
                commitment.setdefault("source_thread", msg.get("subject", ""))
                commitment.setdefault("bucket", triage.get("bucket", "TABP"))
                # Notion's add_commitment expects these keys; defaults are safe.
                try:
                    await notion_writer.add_commitment(
                        commitment,
                        source_file_id=None,
                    )
                    captured_titles.append(commitment.get("what", "(no title)"))
                except Exception:
                    log.exception("notion write failed for email-derived commitment")
        elif category == "fyi":
            fyi_count += 1
        else:
            noise_count += 1

    # Advance high-water mark to the newest message we saw (regardless
    # of classification outcome — the message exists, we don't want to
    # re-fetch it next poll).
    if newest_received:
        _write_high_water(newest_received)

    summary = {
        "new": len(messages),
        "action_needed": action_count,
        "fyi": fyi_count,
        "noise": noise_count,
    }
    log.info("email poll done: %s", summary)

    await _send_email_summary_dm(summary, captured_titles)

    return summary


async def _send_email_summary_dm(summary: dict, captured_titles: list[str]) -> None:
    """One-line summary to Prabhu's chat. Skip if quiet poll."""
    if summary.get("new", 0) == 0:
        return
    owner_chat = os.environ.get("TELEGRAM_OWNER_CHAT_ID")
    if not owner_chat:
        return

    lines = [
        f"📨 Email sweep — {summary['new']} new",
        f"⚡ Action needed: {summary['action_needed']}"
        f"   📰 FYI: {summary['fyi']}"
        f"   🔇 Noise: {summary['noise']}",
    ]
    if captured_titles:
        lines.append("")
        lines.append("Captured commitments:")
        for t in captured_titles[:8]:
            lines.append(f"• {t}")
        if len(captured_titles) > 8:
            lines.append(f"…and {len(captured_titles) - 8} more")
    try:
        await tg.send_message(int(owner_chat), "\n".join(lines), parse_mode=None)
    except Exception:
        log.exception("failed to send email-poll summary DM (non-fatal)")
