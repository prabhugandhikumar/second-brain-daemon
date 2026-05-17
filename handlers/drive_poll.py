"""
Drive Brain Inbox poller — the 15-minute sweep job.

Triggered by Cloud Scheduler hitting POST /cron/poll-drive.

Algorithm:
1. List files in Drive `Brain Inbox` folder (NOT in `Brain Inbox/Processed`).
2. For each new image:
   a. Download bytes
   b. Send to Gemini for classification
   c. If actionable + high confidence:
        - Add each commitment to Notion (idempotent)
        - Move file to Processed/<today>/
   d. If actionable + low/medium confidence (needs_user_input):
        - Send Telegram message to Prabhu's chat with the screenshot + classification
          summary + inline buttons for the questions_for_user options
        - Do NOT move the file yet (only move after Prabhu answers)
   e. If not actionable (noise):
        - Per Prabhu's rule, still ASK rather than archive silently
        - Send Telegram with quick category buttons
3. Log everything.
"""

import io
import logging
import os
import re
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
import google.auth

from lib import classify as classifier
from lib import notion_writer
from lib import telegram_client as tg

log = logging.getLogger(__name__)

DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive"]

# Only consider files whose effective screenshot date is within the last N
# days. "Effective screenshot date" = date parsed from the filename if
# possible (Android/iOS/WhatsApp all encode the date in the name), else
# Drive's createdTime as fallback. This is the right signal because if
# Prabhu batch-uploads old phone screenshots, Drive's createdTime is fresh
# even though the screenshots themselves are years old.
# Set DRIVE_LOOKBACK_DAYS env var to override (default 7).
DEFAULT_LOOKBACK_DAYS = 7

# Hard cap on how many files to process per poll. Cheap protection against
# runaway processing (every classified file = one Gemini call = real cost).
MAX_FILES_PER_POLL = 10

# Date prefixes in screenshot filenames. Order matters — the most specific
# patterns come first. We try each pattern against the filename in turn.
_FILENAME_DATE_PATTERNS = [
    # Android: Screenshot_2024-09-04-01-53-25-68_<pkg>.jpg
    re.compile(r"Screenshot[_\- ](\d{4})-(\d{2})-(\d{2})"),
    # WhatsApp: IMG-20240904-WA0042.jpg
    re.compile(r"IMG[_\-](\d{4})(\d{2})(\d{2})[_\-]WA"),
    # iOS / generic: IMG_20240904_152233.jpg
    re.compile(r"IMG[_\-](\d{4})(\d{2})(\d{2})[_\-]"),
    # Generic 2024-09-04 anywhere in the name
    re.compile(r"(\d{4})-(\d{2})-(\d{2})"),
    # Generic 20240904 anywhere in the name (8 consecutive digits)
    re.compile(r"(?:^|[^\d])(\d{4})(\d{2})(\d{2})(?:[^\d]|$)"),
]


def _date_from_filename(name: str) -> Optional[date]:
    """Parse a screenshot date from the filename, if possible."""
    for pat in _FILENAME_DATE_PATTERNS:
        m = pat.search(name)
        if m:
            try:
                y, mo, d = (int(x) for x in m.groups())
                # Sanity check: reject obvious nonsense like year 9999 or month 13
                if 2000 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31:
                    return date(y, mo, d)
            except ValueError:
                continue
    return None


def _effective_date(file: dict) -> date:
    """Best-guess date for a Drive file: filename first, then Drive createdTime."""
    fname_date = _date_from_filename(file.get("name", ""))
    if fname_date:
        return fname_date
    # Fall back to Drive's createdTime — better than nothing
    created_iso = file.get("createdTime", "")
    if created_iso:
        try:
            return datetime.fromisoformat(created_iso.replace("Z", "+00:00")).date()
        except ValueError:
            pass
    # If we genuinely can't date it, return epoch so it's filtered out (too old)
    return date(1970, 1, 1)


def _drive_service():
    """
    Build the Drive API client. In Cloud Run, uses the service account's
    Application Default Credentials with Drive scope (the service account
    must be granted access to Prabhu's Drive via a shared 'Brain Inbox'
    folder OR via a domain-wide delegation if his Drive is on Workspace).
    Locally, falls back to user OAuth.
    """
    creds, _ = google.auth.default(scopes=DRIVE_SCOPES)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


async def run_drive_poll() -> dict:
    """Main entry point. Called by /cron/poll-drive."""
    brain_inbox_id = os.environ["DRIVE_BRAIN_INBOX_FOLDER_ID"]
    processed_id = await _ensure_processed_subfolder(brain_inbox_id)

    files = _list_unprocessed_files(brain_inbox_id, processed_id)
    log.info("drive poll: %d unprocessed files in Brain Inbox", len(files))

    processed_count = 0
    asked_count = 0
    error_count = 0
    captured_titles: list[str] = []

    for f in files:
        try:
            result = await _process_one_file(f, processed_id)
            if isinstance(result, tuple):
                # ("processed", [titles...]) shape — backwards compatible with old str return
                kind, titles = result
                if kind == "processed":
                    processed_count += 1
                    captured_titles.extend(titles)
                elif kind == "asked":
                    asked_count += 1
            elif result == "processed":
                processed_count += 1
            elif result == "asked":
                asked_count += 1
        except Exception as e:
            log.exception("failed to process %s: %s", f.get("name"), e)
            error_count += 1

    summary = {
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "files_seen": len(files),
        "processed": processed_count,
        "asked_user": asked_count,
        "errors": error_count,
    }
    log.info("drive poll done: %s", summary)

    # Send a summary DM if anything happened. Silent success is no
    # success — Prabhu needs to see what the daemon's been doing.
    await _send_poll_summary_dm(summary, captured_titles)

    return summary


async def _send_poll_summary_dm(summary: dict, captured_titles: list[str]) -> None:
    """Tell Prabhu what just happened. Skip if nothing did."""
    if summary["files_seen"] == 0:
        return
    owner_chat = os.environ.get("TELEGRAM_OWNER_CHAT_ID")
    if not owner_chat:
        log.warning("TELEGRAM_OWNER_CHAT_ID not set — skipping summary DM")
        return

    lines = [f"📥 *Drive sweep* — {summary['files_seen']} screenshot(s)"]
    if summary["processed"]:
        lines.append(f"✅ Auto-captured: *{summary['processed']}*")
    if summary["asked_user"]:
        lines.append(f"❓ Asked for input on: *{summary['asked_user']}* (see messages above)")
    if summary["errors"]:
        lines.append(f"⚠️ Errors: *{summary['errors']}* — check logs")

    if captured_titles:
        lines.append("")
        lines.append("_Captured:_")
        for t in captured_titles[:8]:
            lines.append(f"• {t}")
        if len(captured_titles) > 8:
            lines.append(f"…and {len(captured_titles) - 8} more")

    try:
        await tg.send_message(int(owner_chat), "\n".join(lines))
    except Exception:
        log.exception("failed to send drive-poll summary DM")


def _list_unprocessed_files(parent_folder_id: str, processed_folder_id: str) -> list[dict]:
    """List recent images directly under Brain Inbox (NOT under Processed/).

    "Recent" = effective screenshot date within DRIVE_LOOKBACK_DAYS days.
    Effective date is parsed from the filename (Android/iOS/WhatsApp patterns)
    and falls back to Drive createdTime. This is robust against batch-uploads
    of old phone screenshots, where Drive createdTime is fresh but the
    screenshots are years old.

    Caps the returned list at MAX_FILES_PER_POLL as runaway protection.
    """
    svc = _drive_service()

    lookback_days = int(os.environ.get("DRIVE_LOOKBACK_DAYS", DEFAULT_LOOKBACK_DAYS))
    cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).date()

    # List up to 200 most-recently-uploaded images from Brain Inbox. We
    # sort by createdTime DESC so that recent uploads (most likely to
    # contain recent screenshots) bubble to the top, then filter in
    # Python by effective date.
    q = (
        f"'{parent_folder_id}' in parents "
        f"and mimeType contains 'image/' "
        f"and trashed = false"
    )
    resp = svc.files().list(
        q=q,
        fields="files(id, name, mimeType, modifiedTime, createdTime, size)",
        orderBy="createdTime desc",
        pageSize=200,
    ).execute()
    all_files = resp.get("files", [])

    # Filter to effective-date within window
    recent: list[dict] = []
    for f in all_files:
        eff = _effective_date(f)
        if eff >= cutoff:
            recent.append(f)
            if len(recent) >= MAX_FILES_PER_POLL:
                break

    log.info(
        "drive list: lookback_days=%d cutoff=%s scanned=%d recent=%d (cap=%d)",
        lookback_days, cutoff.isoformat(), len(all_files), len(recent),
        MAX_FILES_PER_POLL,
    )
    # Process oldest-first within the recent window so older screenshots
    # don't get starved by a fresh batch.
    recent.sort(key=_effective_date)
    return recent


async def ensure_processed_subfolder(parent_id: str) -> str:
    """Get or create a 'Processed' subfolder under Brain Inbox.

    Public — also used by the triage callback in handlers/telegram.py.
    """
    svc = _drive_service()
    q = (
        f"'{parent_id}' in parents "
        f"and name = 'Processed' "
        f"and mimeType = 'application/vnd.google-apps.folder' "
        f"and trashed = false"
    )
    found = svc.files().list(q=q, fields="files(id, name)").execute().get("files", [])
    if found:
        return found[0]["id"]
    meta = {
        "name": "Processed",
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id],
    }
    new = svc.files().create(body=meta, fields="id").execute()
    return new["id"]


# Backwards-compat alias for older internal callers (none expected at runtime).
_ensure_processed_subfolder = ensure_processed_subfolder


def get_drive_file_meta(file_id: str) -> dict:
    """Look up name + mimeType for a Drive file. Used by the triage callback."""
    svc = _drive_service()
    return svc.files().get(fileId=file_id, fields="id, name, mimeType").execute()


async def _process_one_file(file: dict, processed_folder_id: str):
    """
    Returns either a plain str ("skipped") or a tuple ("processed" | "asked", titles).
    The titles list is the human-readable commitments captured from this file,
    used by run_drive_poll for the summary DM.
    """
    fid = file["id"]
    fname = file["name"]
    log.info("processing file %s (%s)", fname, fid)

    # 1. Download bytes
    image_bytes = _download(fid)

    # 2. Classify
    classification = await classifier.classify_screenshot(
        image_bytes,
        mime_type=file["mimeType"],
        source_hint=f"Drive Brain Inbox · file '{fname}'",
    )

    # 3. Decide routing
    if classification.get("actionable") and not classification.get("needs_user_input"):
        # Auto-capture: write commitments to Notion, move file to Processed
        captured: list[str] = []
        for commitment in classification.get("commitments", []):
            try:
                await notion_writer.add_commitment(commitment, source_file_id=fid)
                captured.append(commitment.get("what", "(no title)"))
            except Exception as e:
                log.exception("failed to add commitment from %s: %s", fname, e)
        _move_to_processed(fid, processed_folder_id)
        return ("processed", captured)

    elif classification.get("needs_user_input") or not classification.get("actionable"):
        # Per Prabhu's rule: always ask if unclear or non-actionable
        await _ask_user_about(file, image_bytes, classification)
        # We do NOT move to Processed yet — that happens after the user answers.
        return ("asked", [])

    return "skipped"


def download_drive_file(file_id: str) -> bytes:
    """Download a Drive file as bytes. Public — used by triage callback."""
    svc = _drive_service()
    req = svc.files().get_media(fileId=file_id)
    buf = io.BytesIO()
    dl = MediaIoBaseDownload(buf, req)
    done = False
    while not done:
        _, done = dl.next_chunk()
    return buf.getvalue()


def move_to_processed(file_id: str, processed_folder_id: str) -> None:
    """Move a file from Brain Inbox to Brain Inbox/Processed/.

    Public — also used by the triage callback in handlers/telegram.py.
    """
    svc = _drive_service()
    # Get the file's current parents
    current = svc.files().get(fileId=file_id, fields="parents").execute()
    previous_parents = ",".join(current.get("parents", []))
    svc.files().update(
        fileId=file_id,
        addParents=processed_folder_id,
        removeParents=previous_parents,
        fields="id, parents",
    ).execute()
    log.info("moved file %s to Processed", file_id)


# Backwards-compat aliases.
_download = download_drive_file
_move_to_processed = move_to_processed


async def _ask_user_about(file: dict, image_bytes: bytes, classification: dict) -> None:
    """
    Send the screenshot to Prabhu on Telegram with an inline-keyboard question
    so he can disambiguate. The callback_data uses prefix "triage:" which is
    already handled in handlers/telegram.py:_handle_callback.
    """
    owner_chat = os.environ.get("TELEGRAM_OWNER_CHAT_ID")
    if not owner_chat:
        log.error("TELEGRAM_OWNER_CHAT_ID not set — cannot ask user about %s", file.get("name"))
        return

    questions = classification.get("questions_for_user") or []
    summary = classification.get("summary", "(no summary)")
    bucket_hint = classification.get("bucket", "")
    screen_type = classification.get("screen_type", "")

    # Build the question: prefer the LLM's own question_for_user; fall back
    # to a generic bucket-picker so we always ask SOMETHING.
    if questions:
        q = questions[0]
        question_text = q.get("question", "What is this for?")
        options = q.get("options") or ["TABP", "TABPS Pets", "Personal", "Skip"]
    else:
        question_text = "What is this for?"
        options = ["TABP", "TABPS Pets", "Other Businesses", "Skip"]

    # Keep options to 4 max so they fit one row of inline buttons
    options = options[:4]

    # callback_data is capped at 64 bytes per Telegram. We pack:
    # "triage:<choice>:<drive_file_id>" — the file_id is needed so the
    # callback handler can move the file to Processed after the answer.
    # Drive file IDs are ~33 chars which leaves plenty of room.
    fid = file["id"]
    kb = tg.inline_keyboard([
        [tg.button(opt, f"triage:{opt[:20]}:{fid}") for opt in options]
    ])

    # Plain text caption — no Markdown. Telegram rejects messages with
    # unbalanced *…* / _…_ entities (LLM output occasionally has them),
    # and the readability cost of plain text is negligible.
    caption_lines = [f"🤔 {question_text}", "", summary]
    if screen_type:
        hint = f"(type: {screen_type}"
        if bucket_hint:
            hint += f", guess: {bucket_hint}"
        hint += ")"
        caption_lines.append(hint)
    caption = "\n".join(caption_lines)

    try:
        await tg.send_photo(
            int(owner_chat),
            image_bytes,
            caption=caption,
            reply_markup=kb,
            filename=file.get("name", "screenshot.jpg"),
            # parse_mode intentionally omitted → plain text, no parse errors
        )
        log.info("asked user about file %s (%s)", file.get("name"), fid)
    except Exception:
        log.exception("failed to send triage photo for %s", fid)
