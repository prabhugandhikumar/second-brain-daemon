"""
Telegram webhook handler — routes every incoming update to the right action.

Handles:
- Text messages: parsed as commands ("/open", "/done sukkrish") or natural
  language ("what's open for TABP?", "mark Edwin call done", "remind me to call SBI tomorrow")
- Voice notes: audio → Gemini Flash (single call: transcribe + classify intent)
- Photos: classified (same path as Drive polling)
- Callback queries (inline button taps): handled per the callback_data prefix
"""

import json
import logging
import os
import re
from datetime import datetime
from typing import Optional

import pytz

from lib import classify as classifier
from lib import llm
from lib import notion_writer
from lib import telegram_client as tg
from lib.prompts import (
    VOICE_NOTE_PARSE_SYSTEM,
    VOICE_NOTE_PARSE_PROMPT,
    VOICE_AUDIO_PARSE_PROMPT,
)

log = logging.getLogger(__name__)

IST = pytz.timezone("Asia/Kolkata")


# Allowed chat IDs — only Prabhu's personal + the TABP Briefing group can use the bot.
# Strangers who find @PrabhuBrainBot get a polite "not authorized" reply.
def _is_authorized(chat_id: int) -> bool:
    owner = int(os.environ.get("TELEGRAM_OWNER_CHAT_ID", "0"))
    group = os.environ.get("TELEGRAM_GROUP_CHAT_ID", "")
    group_id = int(group) if group else None
    return chat_id == owner or (group_id is not None and chat_id == group_id)


async def handle_telegram_update(payload: dict) -> None:
    """Top-level webhook dispatch."""
    if "callback_query" in payload:
        await _handle_callback(payload["callback_query"])
        return

    msg = payload.get("message") or payload.get("edited_message")
    if not msg:
        return

    chat_id = msg["chat"]["id"]
    if not _is_authorized(chat_id):
        log.warning("unauthorized chat %s sent: %s", chat_id, msg.get("text", "")[:50])
        try:
            await tg.send_message(chat_id, "🔒 This bot is private. Reach out to Prabhu if you should have access.")
        except Exception:
            pass
        return

    # Voice / audio
    if "voice" in msg or "audio" in msg:
        await _handle_voice(msg)
        return

    # Photo
    if "photo" in msg:
        await _handle_photo(msg)
        return

    # Document (someone might forward an image as a file)
    if "document" in msg and msg["document"].get("mime_type", "").startswith("image/"):
        await _handle_photo_as_document(msg)
        return

    # Text
    if "text" in msg:
        await _handle_text(msg)
        return

    log.info("unhandled telegram message type: keys=%s", list(msg.keys()))


# ─────────────────────────────────────────────────────────────────────
# Text messages — commands and natural language
# ─────────────────────────────────────────────────────────────────────
async def _handle_text(msg: dict) -> None:
    chat_id = msg["chat"]["id"]
    text = msg["text"].strip()

    # Command: /start
    if text.startswith("/start"):
        await tg.send_message(
            chat_id,
            "*🧠 Second Brain*\n\n"
            "I'm your conversational interface to the Second Brain system.\n\n"
            "*What I do:*\n"
            "• Send me a *screenshot* → I classify and capture commitments\n"
            "• Send me a *voice note* → I transcribe + create tasks\n"
            "• Send me *text* → I answer questions or take actions\n\n"
            "*Commands:*\n"
            "`/open` — list your open commitments\n"
            "`/today` — what's due today\n"
            "`/overdue` — what's overdue\n"
            "`/help` — show this menu",
        )
        return

    if text.startswith("/help"):
        await _handle_text({**msg, "text": "/start"})
        return

    if text in ("/open", "/commitments"):
        await _send_commitment_list(chat_id, filter_status="Open")
        return

    if text == "/today":
        await _send_commitment_list(chat_id, filter_status="Open", due_filter="today")
        return

    if text == "/overdue":
        await _send_commitment_list(chat_id, filter_status="Open", due_filter="overdue")
        return

    # Natural language — pass to LLM to parse intent and act
    await _handle_natural_language(chat_id, text)


async def _handle_natural_language(chat_id: int, text: str) -> None:
    today = datetime.now(IST).date().isoformat()
    prompt = VOICE_NOTE_PARSE_PROMPT.format(transcript=text, today=today)
    try:
        parsed = await llm.reason_about(prompt, system=VOICE_NOTE_PARSE_SYSTEM)
    except Exception as e:
        log.exception("LLM intent parse failed for text: %r", text[:200])
        await tg.send_message(chat_id, f"❌ Could not parse: {e}")
        return

    log.info("LLM parsed intent for %r → %r", text[:120], parsed)

    # Any error inside intent execution should surface to the user,
    # not die silently. Without this the bot just stops mid-conversation.
    try:
        await _execute_parsed_intent(chat_id, parsed, original_text=text)
    except Exception as e:
        log.exception("intent execution failed; parsed=%r", parsed)
        await tg.send_message(
            chat_id,
            f"❌ Something went wrong handling that: `{e}`\n\nTry rephrasing, "
            f"or use `/open`, `/today`, `/overdue` for direct lookups."
        )


async def _execute_parsed_intent(chat_id: int, parsed: dict, original_text: str) -> None:
    """Take the LLM's structured parse and act on it."""
    intent = parsed.get("intent", "unclear")

    # Ambiguous — ask user to disambiguate
    if intent == "unclear" or parsed.get("clarify"):
        clarify = parsed.get("clarify") or {"question": "I'm not sure what to do.", "options": ["Add as note", "Skip"]}
        kb = tg.inline_keyboard([
            [tg.button(opt, f"clarify:{opt[:50]}:{original_text[:30]}") for opt in clarify["options"]]
        ])
        await tg.send_message(chat_id, f"🤔 {clarify['question']}", reply_markup=kb)
        return

    # Query — fetch from Notion and reply
    if intent == "query":
        await _answer_query(chat_id, original_text)
        return

    # Mark done — fuzzy-match user's phrase against open commitments,
    # then send a confirm-tap UI. The actual Notion update happens in
    # the `done:<page_id>` callback handler (already wired below).
    if intent == "mark_done":
        action = (parsed.get("actions") or [{}])[0]
        query_text = action.get("what") or original_text
        await _ask_to_mark_done(chat_id, query_text)
        return

    # Add commitment / task / note — write to Notion
    if intent in ("add_commitment", "add_task", "assign_task", "remind_me", "add_note"):
        created = []
        for action in parsed.get("actions", []):
            commitment = {
                "what": action.get("what", original_text[:100]),
                "counterparty": action.get("who", "Prabhu"),
                "channel": "Other",
                "source_thread": "Telegram bot",
                "promised_on": datetime.now(IST).date().isoformat(),
                "due_by": action.get("due_by"),
                "action_type": "Other",
                "bucket": action.get("bucket", "TABP"),
                "notes": action.get("notes", "Captured via Telegram voice/text"),
            }
            try:
                page = await notion_writer.add_commitment(commitment)
                created.append((commitment["what"], page.get("url", "")))
            except Exception as e:
                log.exception("notion write failed")
                await tg.send_message(chat_id, f"❌ Notion write failed: {e}")
                return

        if not created:
            await tg.send_message(chat_id, "🤔 I didn't extract any actions. Try rephrasing?")
            return

        lines = ["✅ *Captured:*"]
        for what, url in created:
            lines.append(f"• {what}" + (f"\n  → {url}" if url else ""))
        await tg.send_message(chat_id, "\n".join(lines))
        return

    # Fallback — any intent we don't recognise (e.g. the LLM returned
    # "search", "find", "lookup", "summarize", or anything off-script).
    # Treat the message as a question rather than dropping it silently.
    log.warning("unhandled intent %r — falling through to _answer_query", intent)
    await _answer_query(chat_id, original_text)


async def _answer_query(chat_id: int, question: str) -> None:
    """User asked a question — fetch context from Notion + compose an answer."""
    rows = await notion_writer.get_open_commitments(limit=50)
    # Hand to LLM with the rows as context
    rows_brief = []
    for r in rows:
        props = r.get("properties", {})
        title = " ".join(
            b.get("plain_text", "") for b in props.get("Commitment", {}).get("title", [])
        )
        counterparty = " ".join(
            b.get("plain_text", "") for b in props.get("Counterparty", {}).get("rich_text", [])
        )
        company = (props.get("Company", {}).get("select") or {}).get("name", "")
        due = (props.get("Due By", {}).get("date") or {}).get("start", "")
        rows_brief.append(
            {"what": title, "counterparty": counterparty, "company": company, "due_by": due}
        )

    prompt = (
        f"Prabhu asked: {question}\n\n"
        f"Open commitments in Notion:\n{json.dumps(rows_brief, indent=2)}\n\n"
        "Answer briefly. Mention specific items with their counterparty if relevant. "
        "Use markdown. Max 6 lines."
    )
    answer = await llm.synthesize_text(prompt)
    await tg.send_message(chat_id, answer)


async def _send_commitment_list(chat_id: int, filter_status: str = "Open",
                                  due_filter: Optional[str] = None) -> None:
    rows = await notion_writer.get_open_commitments(limit=50)
    if not rows:
        await tg.send_message(chat_id, "✨ Nothing open. Inbox zero.")
        return

    today = datetime.now(IST).date()
    today_iso = today.isoformat()

    lines = []
    for r in rows[:20]:
        props = r.get("properties", {})
        title = " ".join(b.get("plain_text", "") for b in props.get("Commitment", {}).get("title", []))
        counterparty = " ".join(b.get("plain_text", "") for b in props.get("Counterparty", {}).get("rich_text", []))
        due = (props.get("Due By", {}).get("date") or {}).get("start", "")

        if due_filter == "today" and due != today_iso:
            continue
        if due_filter == "overdue" and (not due or due >= today_iso):
            continue

        aging = ""
        if due:
            d = datetime.fromisoformat(due).date()
            days = (d - today).days
            if days < 0:
                aging = f" ⚠️ {abs(days)}d overdue"
            elif days == 0:
                aging = " 🟡 today"
            elif days == 1:
                aging = " 📅 tomorrow"
        lines.append(f"• *{counterparty or 'Unknown'}*: {title}{aging}")

    if not lines:
        await tg.send_message(chat_id, f"✨ Nothing {due_filter or 'open'}.")
        return

    await tg.send_message(chat_id, "*Open commitments*\n\n" + "\n".join(lines))


# ─────────────────────────────────────────────────────────────────────
# Mark-done by natural language
# ─────────────────────────────────────────────────────────────────────
# Cheap keyword-overlap matching against open commitments. We strip
# common stop-words from the user's phrase, then score each commitment
# by how many of the remaining keywords appear in (title + counterparty).
# No LLM call needed for matching itself — predictable, fast, free.
# If matching ever turns out too dumb in practice, layer an LLM
# semantic-match call on top of these candidates.
_DONE_STOP_WORDS = {
    "mark", "done", "complete", "completed", "finish", "finished",
    "close", "closed", "kill", "delete", "remove", "tick", "tickoff",
    "the", "is", "as", "a", "an", "and", "to", "for", "off", "of",
    "that", "this", "it", "with", "from", "on", "in",
}


def _query_keywords(text: str) -> list[str]:
    """Extract probable content keywords from a mark-done phrase."""
    words = re.findall(r"\w+", text.lower())
    return [w for w in words if w not in _DONE_STOP_WORDS and len(w) > 2]


def _commitment_text(row: dict) -> tuple[str, str]:
    """Pull (title, counterparty) plain text from a Notion commitment row."""
    props = row.get("properties", {})
    title = " ".join(
        b.get("plain_text", "")
        for b in props.get("Commitment", {}).get("title", [])
    )
    counterparty = " ".join(
        b.get("plain_text", "")
        for b in props.get("Counterparty", {}).get("rich_text", [])
    )
    return title, counterparty


def _score_commitment(keywords: list[str], row: dict) -> int:
    """Number of keywords present in the commitment's title+counterparty."""
    title, counterparty = _commitment_text(row)
    haystack = (title + " " + counterparty).lower()
    return sum(1 for kw in keywords if kw in haystack)


async def _find_done_candidates(query_text: str, limit: int = 3) -> list[dict]:
    """Return top-scoring open commitments matching the query."""
    keywords = _query_keywords(query_text)
    if not keywords:
        return []
    rows = await notion_writer.get_open_commitments(limit=100)
    scored: list[dict] = []
    for r in rows:
        score = _score_commitment(keywords, r)
        if score == 0:
            continue
        title, counterparty = _commitment_text(r)
        scored.append({
            "page_id": r["id"],
            "title": title,
            "counterparty": counterparty,
            "score": score,
        })
    scored.sort(key=lambda x: (-x["score"], x["title"]))
    return scored[:limit]


async def _ask_to_mark_done(chat_id: int, query_text: str) -> None:
    """Send a confirm-tap UI listing candidate commitments to mark done."""
    candidates = await _find_done_candidates(query_text)

    if not candidates:
        await tg.send_message(
            chat_id,
            f"🤔 Couldn't find any open commitment matching '{query_text}'. "
            f"Try /open to see what's tracked.",
            parse_mode=None,
        )
        return

    if len(candidates) == 1:
        c = candidates[0]
        kb = tg.inline_keyboard([[
            tg.button("✓ Mark done", f"done:{c['page_id']}"),
            tg.button("✗ Cancel", "noop:cancel"),
        ]])
        body = (
            "Mark this done?\n\n"
            f"• {c['title']}"
            + (f"\n  ({c['counterparty']})" if c['counterparty'] else "")
        )
        await tg.send_message(chat_id, body, reply_markup=kb, parse_mode=None)
        return

    # Multiple matches — show numbered list with per-candidate buttons
    lines = [f"Which one to mark done? (matched on '{query_text}')\n"]
    button_row: list[dict] = []
    for i, c in enumerate(candidates, 1):
        suffix = f" — {c['counterparty']}" if c['counterparty'] else ""
        lines.append(f"{i}. {c['title']}{suffix}")
        button_row.append(tg.button(f"#{i}", f"done:{c['page_id']}"))
    button_row.append(tg.button("✗ Cancel", "noop:cancel"))
    kb = tg.inline_keyboard([button_row])
    await tg.send_message(chat_id, "\n".join(lines), reply_markup=kb, parse_mode=None)


# ─────────────────────────────────────────────────────────────────────
# Voice notes — one-shot: audio → Gemini Flash → {transcript + intent}
# ─────────────────────────────────────────────────────────────────────
# Note: replaces the old Whisper + Gemini two-step. Gemini Flash accepts
# audio directly, so we do transcription and intent classification in a
# single call. Stays inside the Gemini free tier, no OpenAI vendor.
async def _handle_voice(msg: dict) -> None:
    chat_id = msg["chat"]["id"]
    voice = msg.get("voice") or msg.get("audio")
    file_id = voice["file_id"]
    # Telegram .ogg/opus is the default for voice notes; "audio" may differ
    mime_type = voice.get("mime_type", "audio/ogg")

    # Acknowledge while we work
    await tg.send_message(chat_id, "🎙 Listening...")

    today = datetime.now(IST).date().isoformat()
    prompt = VOICE_AUDIO_PARSE_PROMPT.format(today=today)

    try:
        audio_bytes = await tg.get_file_bytes(file_id)
        parsed = await llm.parse_audio(
            audio_bytes, prompt,
            mime_type=mime_type,
            system=VOICE_NOTE_PARSE_SYSTEM,
        )
    except Exception as e:
        log.exception("voice parse failed (file_id=%s)", file_id)
        await tg.send_message(chat_id, f"❌ Voice processing failed: {e}")
        return

    transcript = (parsed.get("transcript") or "").strip()
    log.info("voice parsed: transcript=%r intent=%r", transcript[:120], parsed.get("intent"))

    # Echo back what we heard — preserves the user-facing trust signal
    if transcript:
        await tg.send_message(chat_id, f"📝 _Heard:_ \"{transcript}\"")
    else:
        await tg.send_message(chat_id, "🤷 Couldn't make out anything in the audio.")
        return

    # Execute exactly like the text path — same intent contract
    try:
        await _execute_parsed_intent(chat_id, parsed, original_text=transcript)
    except Exception as e:
        log.exception("voice intent execution failed; parsed=%r", parsed)
        await tg.send_message(
            chat_id,
            f"❌ Something went wrong handling that: `{e}`\n\nTry rephrasing, "
            f"or use `/open`, `/today`, `/overdue` for direct lookups."
        )


# ─────────────────────────────────────────────────────────────────────
# Photos / screenshots
# ─────────────────────────────────────────────────────────────────────
async def _handle_photo(msg: dict) -> None:
    chat_id = msg["chat"]["id"]
    # Telegram sends photos in multiple sizes; take the largest
    photo = msg["photo"][-1]
    file_id = photo["file_id"]
    caption = msg.get("caption", "")

    await _process_image_from_telegram(chat_id, file_id, source_hint=f"Telegram (caption: {caption!r})" if caption else "Telegram")


async def _handle_photo_as_document(msg: dict) -> None:
    chat_id = msg["chat"]["id"]
    file_id = msg["document"]["file_id"]
    await _process_image_from_telegram(chat_id, file_id, source_hint="Telegram document")


async def _process_image_from_telegram(chat_id: int, file_id: str, source_hint: str) -> None:
    try:
        image_bytes = await tg.get_file_bytes(file_id)
    except Exception as e:
        await tg.send_message(chat_id, f"❌ Could not download image: {e}")
        return

    classification = await classifier.classify_screenshot(
        image_bytes, source_hint=source_hint
    )

    summary = classification.get("summary", "(no summary)")
    if classification.get("actionable") and not classification.get("needs_user_input"):
        # Auto-write commitments
        written = []
        for c in classification.get("commitments", []):
            try:
                page = await notion_writer.add_commitment(c)
                written.append(c["what"])
            except Exception as e:
                log.exception("notion write from telegram failed")
        if written:
            await tg.send_message(
                chat_id,
                f"✅ *Captured {len(written)}:*\n" + "\n".join(f"• {w}" for w in written)
                + f"\n\n_{summary}_"
            )
        else:
            await tg.send_message(chat_id, f"📋 Saved. _{summary}_")
        return

    # Need to ask
    questions = classification.get("questions_for_user", [])
    q = (questions[0] if questions else {"question": "What is this for?", "options": ["TABP", "TABPS Pets", "Personal", "Skip"]})
    kb = tg.inline_keyboard([
        [tg.button(opt, f"triage:{opt[:30]}:{file_id[:30]}") for opt in q["options"][:4]]
    ])
    await tg.send_message(
        chat_id,
        f"🤔 {q['question']}\n\n_{summary}_",
        reply_markup=kb,
    )


# ─────────────────────────────────────────────────────────────────────
# Callback queries — inline button taps
# ─────────────────────────────────────────────────────────────────────
async def _handle_callback(cb: dict) -> None:
    chat_id = cb["message"]["chat"]["id"]
    message_id = cb["message"]["message_id"]
    callback_id = cb["id"]
    data = cb.get("data", "")

    try:
        parts = data.split(":", 2)
        prefix = parts[0]

        if prefix == "done":
            page_id = parts[1] if len(parts) > 1 else None
            if page_id:
                await notion_writer.mark_commitment_done(page_id)
                await tg.answer_callback_query(callback_id, "✓ Marked done")
                await tg.edit_message_reply_markup(chat_id, message_id, None)
            return

        if prefix == "snooze":
            page_id = parts[1] if len(parts) > 1 else None
            days = int(parts[2]) if len(parts) > 2 else 3
            if page_id:
                await notion_writer.snooze_commitment(page_id, days=days)
                await tg.answer_callback_query(callback_id, f"⏰ Snoozed {days}d")
                await tg.edit_message_reply_markup(chat_id, message_id, None)
            return

        if prefix == "triage":
            choice = parts[1] if len(parts) > 1 else "Skip"
            file_id = parts[2] if len(parts) > 2 else ""
            if not file_id:
                await tg.answer_callback_query(callback_id, "Missing file id")
                return

            # Ack immediately so the button stops showing a loading spinner.
            # The actual work below can take several seconds.
            await tg.answer_callback_query(callback_id, f"Routing to {choice}…")
            await tg.edit_message_reply_markup(chat_id, message_id, None)

            try:
                summary = await _route_triaged_screenshot(file_id, choice)
            except Exception as e:
                log.exception("triage routing failed for file %s → %s", file_id, choice)
                await tg.send_message(
                    chat_id,
                    f"❌ Failed to route to {choice}: {e}",
                    parse_mode=None,
                )
                return

            await tg.send_message(chat_id, summary, parse_mode=None)
            return

        if prefix == "clarify":
            choice = parts[1] if len(parts) > 1 else ""
            await tg.answer_callback_query(callback_id, f"Got it: {choice}")
            await tg.edit_message_reply_markup(chat_id, message_id, None)
            return

        if prefix == "noop":
            # Used by the Cancel button on mark-done confirmation flows.
            await tg.answer_callback_query(callback_id, "Cancelled")
            await tg.edit_message_reply_markup(chat_id, message_id, None)
            return

        await tg.answer_callback_query(callback_id, "Unknown action")
    except Exception as e:
        log.exception("callback handler failed")
        await tg.answer_callback_query(callback_id, f"Error: {e}")


# ─────────────────────────────────────────────────────────────────────
# Triage routing — invoked when user taps a bucket button on the
# "🤔 What is this for?" message that handlers.drive_poll._ask_user_about
# sent after an ambiguous screenshot classification.
# ─────────────────────────────────────────────────────────────────────
async def _route_triaged_screenshot(file_id: str, choice: str) -> str:
    """
    Act on the user's bucket choice for a previously-ambiguous screenshot.

    - "Skip"  → move file to Processed/, no Notion write.
    - bucket → re-classify with the bucket as an override hint, write any
               commitments to Notion under Company = <bucket>, move file
               to Processed/. Idempotent (add_commitment de-dups).

    Returns a plain-text summary suitable for sending back to Prabhu.
    """
    # Lazy import — keeps the module-load graph simple (drive_poll imports
    # this file's send_message wrapper indirectly).
    from handlers import drive_poll as dp

    brain_inbox_id = os.environ["DRIVE_BRAIN_INBOX_FOLDER_ID"]
    processed_id = await dp.ensure_processed_subfolder(brain_inbox_id)

    if choice.lower() in ("skip", "skip "):
        try:
            dp.move_to_processed(file_id, processed_id)
        except Exception:
            log.exception("move to processed failed during skip; file %s", file_id)
            return "❌ Couldn't move file to Processed (will retry on next poll)."
        return "📂 Skipped — file moved to Processed."

    # Fetch metadata for the right mimeType, then download bytes
    try:
        meta = dp.get_drive_file_meta(file_id)
        mime_type = meta.get("mimeType", "image/jpeg")
        fname = meta.get("name", "screenshot.jpg")
    except Exception:
        log.exception("could not fetch Drive metadata for %s; using defaults", file_id)
        mime_type = "image/jpeg"
        fname = "screenshot.jpg"

    image_bytes = dp.download_drive_file(file_id)

    # Re-classify with the bucket choice as a strong hint
    classification = await classifier.classify_screenshot(
        image_bytes,
        mime_type=mime_type,
        source_hint=(
            f"Drive Brain Inbox · file '{fname}' · "
            f"Prabhu manually selected bucket={choice} — extract any commitments "
            f"and assign them to this bucket."
        ),
    )

    # Write commitments with the user's bucket choice OVERRIDING whatever
    # the LLM guessed.
    written: list[str] = []
    write_errors: list[str] = []
    for commitment in classification.get("commitments", []):
        commitment["bucket"] = choice
        try:
            await notion_writer.add_commitment(commitment, source_file_id=file_id)
            written.append(commitment.get("what", "(no title)"))
        except Exception as e:
            log.exception("notion write failed during triage routing")
            write_errors.append(str(e))

    # Move regardless of Notion outcome — dedup catches future retries,
    # but leaving the file in inbox would trigger triage again.
    try:
        dp.move_to_processed(file_id, processed_id)
    except Exception:
        log.exception("move to processed failed after triage; file %s", file_id)

    # Build a plain-text summary
    if not written and not write_errors:
        return (
            f"📂 Filed to {choice}. The LLM didn't extract any commitments "
            f"from this one — the screenshot is in Processed/ for reference."
        )
    if write_errors and not written:
        return (
            f"⚠️ Filed to {choice}, but Notion writes all failed:\n"
            + "\n".join(f"• {e[:120]}" for e in write_errors[:3])
        )

    lines = [f"📂 Filed to {choice} — captured {len(written)}:"]
    for t in written[:8]:
        lines.append(f"• {t}")
    if len(written) > 8:
        lines.append(f"…and {len(written) - 8} more")
    if write_errors:
        lines.append("")
        lines.append(f"⚠️ {len(write_errors)} write(s) also failed; check logs.")
    return "\n".join(lines)
