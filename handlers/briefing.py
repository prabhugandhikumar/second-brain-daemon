"""
Morning briefing — runs at 5 AM IST via Cloud Scheduler.

Pulls open commitments + today's calendar + recent activity, asks Gemini
to synthesize a coherent briefing, and delivers via Telegram (and email
later when Gmail OAuth is set up).
"""

import json
import logging
import os
from datetime import datetime, timedelta, timezone

import pytz

from lib import email_outlook as outlook
from lib import llm
from lib import notion_writer
from lib import telegram_client as tg
from lib.prompts import MORNING_BRIEFING_PROMPT, MORNING_BRIEFING_SYSTEM

log = logging.getLogger(__name__)

IST = pytz.timezone("Asia/Kolkata")


async def run_morning_briefing() -> dict:
    """Main entry. Called by /cron/morning-briefing."""
    today = datetime.now(IST).date()
    today_iso = today.isoformat()
    log.info("morning briefing for %s starting", today_iso)

    # 1. Gather commitments
    raw_rows = await notion_writer.get_open_commitments(limit=50)
    commitments = _summarize_rows(raw_rows, today)

    # Today's calendar events from Outlook (md@tabp.co.in)
    meetings = await _fetch_today_meetings()

    # Overnight email from md@tabp.co.in — fetch via Microsoft Graph.
    # Window: yesterday 18:00 IST → now, which covers anything that
    # arrived after Prabhu logged off the previous evening. Capped at
    # 50 messages so the LLM context stays manageable; Gemini picks the
    # top 5 by importance per the briefing prompt.
    emails = await _fetch_overnight_emails()

    # TODO: pull WhatsApp captures from last 24h (from Notion via new-rows filter)
    whatsapp_caps = []

    # 2. Synthesize via LLM
    prompt = MORNING_BRIEFING_PROMPT.format(
        date=today_iso,
        commitments_json=json.dumps(commitments[:30], default=str),
        meetings_json=json.dumps(meetings, default=str),
        emails_json=json.dumps(emails, default=str),
        whatsapp_json=json.dumps(whatsapp_caps, default=str),
    )
    briefing = await llm.reason_about(prompt, system=MORNING_BRIEFING_SYSTEM)

    # 3. Persist as a Daily Briefing row in Notion
    try:
        await notion_writer.add_briefing_row(briefing)
    except Exception as e:
        log.warning("briefing row write failed: %s", e)

    # 4. Deliver to Telegram (and group)
    await _deliver_telegram(briefing, commitments, today)

    log.info("morning briefing delivered: urgent=%d", len(briefing.get("urgent", [])))
    return {"ok": True, "urgent_count": len(briefing.get("urgent", []))}


async def _fetch_today_meetings() -> list[dict]:
    """Pull today's Outlook calendar events. Soft-fail.

    Window: today 00:00 IST → today 23:59 IST. The Graph response includes
    recurring meeting instances expanded for the day (calendarView endpoint).
    Returns a list of flattened event dicts. Empty list on Graph error.
    """
    now_ist = datetime.now(IST)
    start_ist = now_ist.replace(hour=0, minute=0, second=0, microsecond=0)
    end_ist = now_ist.replace(hour=23, minute=59, second=59, microsecond=0)

    try:
        raw = await outlook.list_calendar_events(start_ist, end_ist, timezone_name="Asia/Kolkata")
    except Exception as e:
        log.warning("briefing: today's calendar fetch failed (non-fatal): %s", e)
        return []

    log.info("briefing: fetched %d events for today", len(raw))

    out: list[dict] = []
    for ev in raw:
        if ev.get("isCancelled"):
            continue
        start = (ev.get("start") or {}).get("dateTime", "")
        end = (ev.get("end") or {}).get("dateTime", "")
        # Format start time as "10:00 AM IST" — Graph returns "2026-05-18T10:00:00.0000000"
        time_label = ""
        if start:
            try:
                dt = datetime.fromisoformat(start.split(".")[0])
                time_label = dt.strftime("%I:%M %p").lstrip("0")
            except Exception:
                time_label = start
        out.append({
            "subject": ev.get("subject", "(no subject)"),
            "time": time_label,
            "start": start,
            "end": end,
            "location": ((ev.get("location") or {}).get("displayName") or ""),
            "organizer": ((ev.get("organizer") or {}).get("emailAddress") or {}).get("name") or "",
            "is_online": bool((ev.get("onlineMeeting") or {}).get("joinUrl")),
            "is_all_day": ev.get("isAllDay", False),
            "preview": (ev.get("bodyPreview") or "")[:200],
        })
    return out


async def _fetch_overnight_emails() -> list[dict]:
    """Pull recent Outlook messages for the briefing. Soft-fail.

    Returns a list of flattened message dicts (subject, from, preview, …)
    suitable for embedding in the morning-briefing prompt. If Graph is
    unreachable or the token is bad, returns an empty list and logs —
    the rest of the briefing still works.
    """
    # Yesterday 18:00 IST in UTC for the Graph query
    now_ist = datetime.now(IST)
    yesterday_evening_ist = (now_ist - timedelta(days=1)).replace(
        hour=18, minute=0, second=0, microsecond=0
    )
    since_utc = yesterday_evening_ist.astimezone(timezone.utc)

    try:
        raw = await outlook.list_recent_messages(since_utc, limit=50)
    except Exception as e:
        log.warning("briefing: overnight email fetch failed (non-fatal): %s", e)
        return []

    log.info("briefing: fetched %d overnight emails since %s", len(raw), since_utc.isoformat())

    out: list[dict] = []
    for m in raw:
        f = (m.get("from") or {}).get("emailAddress") or {}
        out.append({
            "subject": (m.get("subject") or "")[:200],
            "from_name": f.get("name") or "",
            "from_email": f.get("address") or "",
            "preview": (m.get("bodyPreview") or "")[:300],
            "received": m.get("receivedDateTime", ""),
            "importance": m.get("importance", "normal"),
            "is_read": m.get("isRead", False),
            "has_attachments": m.get("hasAttachments", False),
        })
    return out


def _summarize_rows(rows: list, today) -> list[dict]:
    """Flatten Notion row payloads into something the LLM can read."""
    out = []
    for r in rows:
        props = r.get("properties", {})
        title = " ".join(b.get("plain_text", "") for b in props.get("Commitment", {}).get("title", []))
        counterparty = " ".join(b.get("plain_text", "") for b in props.get("Counterparty", {}).get("rich_text", []))
        company = (props.get("Company", {}).get("select") or {}).get("name", "")
        due_raw = (props.get("Due By", {}).get("date") or {}).get("start", "")
        promised_raw = (props.get("Promised On", {}).get("date") or {}).get("start", "")
        aging_days = 0
        if promised_raw:
            try:
                promised_d = datetime.fromisoformat(promised_raw).date()
                aging_days = (today - promised_d).days
            except Exception:
                pass
        is_overdue = bool(due_raw and due_raw < today.isoformat())
        out.append({
            "id": r.get("id"),
            "what": title,
            "counterparty": counterparty,
            "company": company,
            "due_by": due_raw,
            "promised_on": promised_raw,
            "aging_days": aging_days,
            "is_overdue": is_overdue,
        })
    return out


async def _deliver_telegram(briefing: dict, commitments: list, today) -> None:
    """Send the briefing to Prabhu's chat + the TABP Briefing group."""
    owner_id = int(os.environ.get("TELEGRAM_OWNER_CHAT_ID", "0"))
    group_id_str = os.environ.get("TELEGRAM_GROUP_CHAT_ID", "")
    targets = [owner_id]
    if group_id_str:
        try:
            targets.append(int(group_id_str))
        except ValueError:
            pass

    msg = _render_briefing(briefing, commitments, today)
    keyboard = _briefing_buttons(briefing, commitments)

    for chat_id in targets:
        if chat_id == 0:
            continue
        try:
            await tg.send_message(chat_id, msg, reply_markup=keyboard)
        except Exception as e:
            log.warning("briefing send to %s failed: %s", chat_id, e)


def _render_briefing(briefing: dict, commitments: list, today) -> str:
    lines = [
        f"*🧠 Morning briefing — {today.strftime('%a %d %b')}*",
        "",
        briefing.get("greeting", ""),
        "",
    ]
    lead = briefing.get("lead")
    if lead:
        lines += [f"*Lead:* {lead}", ""]

    urgent = briefing.get("urgent", [])
    if urgent:
        lines.append("*🔴 Urgent today:*")
        for u in urgent[:5]:
            who = u.get("who", "")
            what = u.get("what", "")
            why = u.get("why_urgent", "")
            lines.append(f"• *{who}*: {what}" + (f" — _{why}_" if why else ""))
        lines.append("")

    meetings = briefing.get("today_meetings", [])
    if meetings:
        lines.append("*📅 Today's meetings:*")
        for m in meetings[:5]:
            lines.append(f"• {m.get('time','')} {m.get('subject','')}")
        lines.append("")

    top_emails = briefing.get("top_emails", [])
    if top_emails:
        lines.append("*📨 Overnight email:*")
        for e in top_emails[:5]:
            sender = e.get("from", "")
            subject = e.get("subject", "")
            why = e.get("why_it_matters", "")
            lines.append(f"• *{sender}* — {subject}" + (f"\n  _{why}_" if why else ""))
        lines.append("")

    decisions = briefing.get("decisions_needed", [])
    if decisions:
        lines.append("*🎯 Decisions needed:*")
        for d in decisions[:3]:
            lines.append(f"• {d}")
        lines.append("")

    good = briefing.get("good_news", [])
    if good:
        lines.append("*✨ Good news:*")
        for g in good[:3]:
            lines.append(f"• {g}")
        lines.append("")

    nudge = briefing.get("closing_nudge")
    if nudge:
        lines += ["—", f"_{nudge}_"]

    lines += [
        "",
        f"📊 Open: {len(commitments)} · Overdue: {sum(1 for c in commitments if c['is_overdue'])}",
        "Open the full dashboard: https://briefing.tabp.co.in",
    ]
    return "\n".join(lines)


def _briefing_buttons(briefing: dict, commitments: list) -> dict:
    """Inline ✓ Done buttons for the urgent items (max 3 to keep the message clean)."""
    rows = []
    urgent_ids = []
    for c in commitments:
        if c["is_overdue"] or (c.get("due_by") == datetime.now(IST).date().isoformat()):
            urgent_ids.append((c["counterparty"] or "?", c["id"]))
        if len(urgent_ids) >= 3:
            break
    for who, pid in urgent_ids:
        rows.append([tg.button(f"✓ Done — {who}", f"done:{pid}"), tg.button("⏰ Snooze 3d", f"snooze:{pid}:3")])
    rows.append([tg.button("📊 Open dashboard", "dashboard:open")])
    return tg.inline_keyboard(rows)
