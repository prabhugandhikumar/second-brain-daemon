"""
Morning briefing — runs at 5 AM IST via Cloud Scheduler.

Schedule-first design (per Prabhu's preference, 2026-05-17): the day is a
series of calendar slots and everything else slots around them. Order:

  1. Today's Outlook calendar (the spine, with prep notes per meeting)
  2. Unscheduled meetings he agreed to (from Commitments DB, action=Meet)
  3. Email digest — past 24h, split into todo / fyi / skip_count
  4. Other urgent commitments (action items not covered by schedule)
  5. Decisions needed
  6. Good news
  7. Closing nudge

Pairs with the evening briefing (handlers/evening_briefing.py) which fires
at 9 PM IST and is forward-looking for tomorrow.
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

    # 1. Open commitments (full set; LLM uses them for prep notes + urgent)
    raw_rows = await notion_writer.get_open_commitments(limit=100)
    commitments = _summarize_rows(raw_rows, today)

    # 2. Outlook calendar for today
    meetings = await _fetch_today_meetings()

    # 3. Meetings Prabhu agreed to but hasn't put on the calendar.
    #    Pulled from Commitments DB (action_type=Meet, status=Open).
    unscheduled_meetings = _filter_unscheduled_meetings(
        commitments, window_start=today, window_days=3
    )

    # 4. Past-24-hour email — every message, the LLM will categorize.
    emails = await _fetch_recent_emails(hours_back=24)

    # WhatsApp captures still TODO — leave empty.
    whatsapp_caps: list[dict] = []

    # 5. Synthesize via LLM
    prompt = MORNING_BRIEFING_PROMPT.format(
        date=today_iso,
        commitments_json=json.dumps(commitments[:50], default=str),
        meetings_json=json.dumps(meetings, default=str),
        unscheduled_meetings_json=json.dumps(unscheduled_meetings, default=str),
        emails_json=json.dumps(emails, default=str),
        whatsapp_json=json.dumps(whatsapp_caps, default=str),
    )
    briefing = await llm.reason_about(prompt, system=MORNING_BRIEFING_SYSTEM)

    # 6. Persist as a Daily Briefing row in Notion
    try:
        await notion_writer.add_briefing_row(briefing)
    except Exception as e:
        log.warning("briefing row write failed: %s", e)

    # 7. Deliver to Telegram
    await _deliver_telegram(briefing, commitments, today)

    log.info(
        "morning briefing delivered: schedule=%d unscheduled=%d email_todo=%d urgent=%d",
        len(briefing.get("schedule", [])),
        len(briefing.get("unscheduled_meetings", [])),
        len((briefing.get("email_digest") or {}).get("todo", [])),
        len(briefing.get("urgent_today", [])),
    )
    return {"ok": True, "urgent_count": len(briefing.get("urgent_today", []))}


# ─────────────────────────────────────────────────────────────────────
# Data fetch helpers (also used by handlers/evening_briefing.py)
# ─────────────────────────────────────────────────────────────────────
async def _fetch_today_meetings() -> list[dict]:
    """Today's Outlook calendar, soft-fail to []."""
    now_ist = datetime.now(IST)
    start_ist = now_ist.replace(hour=0, minute=0, second=0, microsecond=0)
    end_ist = now_ist.replace(hour=23, minute=59, second=59, microsecond=0)
    return await _fetch_calendar_window(start_ist, end_ist, label="today")


async def fetch_meetings_for(target_date) -> list[dict]:
    """Public helper used by evening briefing — calendar for a given date."""
    start_ist = IST.localize(datetime.combine(target_date, datetime.min.time()))
    end_ist = IST.localize(
        datetime.combine(target_date, datetime.min.time()) + timedelta(days=1)
        - timedelta(seconds=1)
    )
    return await _fetch_calendar_window(start_ist, end_ist, label=str(target_date))


async def _fetch_calendar_window(start_ist, end_ist, label: str) -> list[dict]:
    try:
        raw = await outlook.list_calendar_events(start_ist, end_ist, timezone_name="Asia/Kolkata")
    except Exception as e:
        log.warning("briefing: calendar fetch failed for %s (non-fatal): %s", label, e)
        return []

    log.info("briefing: fetched %d events for %s", len(raw), label)
    out: list[dict] = []
    for ev in raw:
        if ev.get("isCancelled"):
            continue
        start = (ev.get("start") or {}).get("dateTime", "")
        end = (ev.get("end") or {}).get("dateTime", "")
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


async def _fetch_recent_emails(hours_back: int = 24) -> list[dict]:
    """Outlook messages received in the last N hours. Soft-fail."""
    since_utc = datetime.now(timezone.utc) - timedelta(hours=hours_back)
    try:
        raw = await outlook.list_recent_messages(since_utc, limit=100)
    except Exception as e:
        log.warning("briefing: recent email fetch failed (non-fatal): %s", e)
        return []

    log.info("briefing: fetched %d recent emails (last %dh)", len(raw), hours_back)
    out: list[dict] = []
    for m in raw:
        f = (m.get("from") or {}).get("emailAddress") or {}
        out.append({
            "subject": (m.get("subject") or "")[:200],
            "from_name": f.get("name") or "",
            "from_email": f.get("address") or "",
            "preview": (m.get("bodyPreview") or "")[:400],
            "received": m.get("receivedDateTime", ""),
            "importance": m.get("importance", "normal"),
            "is_read": m.get("isRead", False),
            "has_attachments": m.get("hasAttachments", False),
        })
    return out


def _filter_unscheduled_meetings(
    commitments: list[dict], window_start, window_days: int = 3
) -> list[dict]:
    """Return commitments with action_type=Meet, status=Open, within window.

    These are meetings Prabhu agreed to (in WhatsApp, email, calls) that
    haven't landed on the Outlook calendar yet — by definition NOT in the
    meetings list. The morning briefing surfaces them so he can put them
    on the calendar.
    """
    window_end_iso = (window_start + timedelta(days=window_days)).isoformat()
    out: list[dict] = []
    for c in commitments:
        if c.get("action_type") != "Meet":
            continue
        if c.get("status") and c["status"] != "Open":
            continue
        due = c.get("due_by") or ""
        promised = c.get("promised_on") or ""
        # Include if due_by is within window, or if no due_by but promised within last 14d
        in_window = False
        if due and due <= window_end_iso:
            in_window = True
        elif not due and promised:
            try:
                p_date = datetime.fromisoformat(promised).date()
                if (window_start - p_date).days <= 14:
                    in_window = True
            except Exception:
                pass
        if in_window:
            out.append({
                "with": c.get("counterparty", ""),
                "what": c.get("what", ""),
                "company": c.get("company", ""),
                "promised_on": promised,
                "due_by": due,
                "aging_days": c.get("aging_days", 0),
                "page_id": c.get("id"),
            })
    return out


def _summarize_rows(rows: list, today) -> list[dict]:
    """Flatten Notion commitment rows into something the LLM can read."""
    out = []
    for r in rows:
        props = r.get("properties", {})
        title = " ".join(
            b.get("plain_text", "")
            for b in props.get("Commitment", {}).get("title", [])
        )
        counterparty = " ".join(
            b.get("plain_text", "")
            for b in props.get("Counterparty", {}).get("rich_text", [])
        )
        company = (props.get("Company", {}).get("select") or {}).get("name", "")
        action_type = (props.get("Action Type", {}).get("select") or {}).get("name", "")
        status = (props.get("Status", {}).get("select") or {}).get("name", "")
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
            "action_type": action_type,
            "status": status,
            "due_by": due_raw,
            "promised_on": promised_raw,
            "aging_days": aging_days,
            "is_overdue": is_overdue,
        })
    return out


# ─────────────────────────────────────────────────────────────────────
# Telegram delivery + rendering
# ─────────────────────────────────────────────────────────────────────
async def _deliver_telegram(briefing: dict, commitments: list, today) -> None:
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
    """Render the briefing JSON into a Telegram-friendly Markdown message."""
    lines = [
        f"*🧠 Today — {today.strftime('%a %d %b')}*",
        "",
        briefing.get("greeting", ""),
        "",
    ]
    lead = briefing.get("lead")
    if lead:
        lines += [f"*Lead:* {lead}", ""]

    # 1) Schedule — the spine of the day
    schedule = briefing.get("schedule", [])
    if schedule:
        lines.append("*📅 Schedule*")
        for s in schedule:
            time = s.get("time", "")
            subject = s.get("subject", "")
            with_who = s.get("with", "")
            company = s.get("company", "")
            tag = f" · _{company}_" if company and company != "Personal" else ""
            who_tag = f" — {with_who}" if with_who else ""
            lines.append(f"• *{time}* {subject}{who_tag}{tag}")
            prep = s.get("prep_notes", "")
            if prep:
                lines.append(f"  _{prep}_")
        lines.append("")

    # 2) Unscheduled meetings — things to put on the calendar
    unsched = briefing.get("unscheduled_meetings", [])
    if unsched:
        lines.append("*🤝 Agreed meetings* (not on calendar yet)")
        for m in unsched[:8]:
            who = m.get("with", "")
            what = m.get("what", "")
            promised = m.get("promised", "")
            lines.append(f"• *{who}* — {what}" + (f" _(promised {promised})_" if promised else ""))
        lines.append("")

    # 3) Email digest — past 24h
    email_digest = briefing.get("email_digest") or {}
    todo = email_digest.get("todo", [])
    fyi = email_digest.get("fyi", [])
    skip = email_digest.get("skip_count", 0)
    if todo or fyi or skip:
        total = len(todo) + len(fyi) + skip
        lines.append(f"*📨 Email — past 24h* ({total} total)")
        if todo:
            lines.append("")
            lines.append("_To-do — reply / decide / act:_")
            for e in todo[:15]:
                sender = e.get("from", "")
                subject = e.get("subject", "")
                action = e.get("action", "")
                lines.append(f"• *{sender}* — {subject}")
                if action:
                    lines.append(f"  → {action}")
        if fyi:
            lines.append("")
            lines.append("_FYI:_")
            for e in fyi[:5]:
                sender = e.get("from", "")
                subject = e.get("subject", "")
                summary = e.get("summary", "")
                lines.append(f"• *{sender}* — {subject}" + (f": {summary}" if summary else ""))
        if skip:
            lines.append("")
            lines.append(f"_Skipped {skip} marketing/transactional/noise._")
        lines.append("")

    # 4) Urgent items — action commitments not covered by schedule
    urgent = briefing.get("urgent_today", [])
    if urgent:
        lines.append("*🔴 Urgent today*")
        for u in urgent[:8]:
            who = u.get("who", "")
            what = u.get("what", "")
            why = u.get("why_urgent", "")
            lines.append(f"• *{who}*: {what}" + (f" — _{why}_" if why else ""))
        lines.append("")

    # 5) Decisions
    decisions = briefing.get("decisions_needed", [])
    if decisions:
        lines.append("*🎯 Decisions needed*")
        for d in decisions[:5]:
            lines.append(f"• {d}")
        lines.append("")

    # 6) Good news
    good = briefing.get("good_news", [])
    if good:
        lines.append("*✨ Good news*")
        for g in good[:3]:
            lines.append(f"• {g}")
        lines.append("")

    # 7) Closing nudge
    nudge = briefing.get("closing_nudge")
    if nudge:
        lines += ["—", f"_{nudge}_"]

    # Footer stats
    lines += [
        "",
        f"📊 Open: {len(commitments)} · Overdue: {sum(1 for c in commitments if c['is_overdue'])}",
        "Dashboard: https://briefing.tabp.co.in",
    ]
    return "\n".join(lines)


def _briefing_buttons(briefing: dict, commitments: list) -> dict:
    """Inline ✓ Done buttons for the urgent items (max 3 to keep the message clean)."""
    rows = []
    urgent_ids = []
    today_iso = datetime.now(IST).date().isoformat()
    for c in commitments:
        if c["is_overdue"] or (c.get("due_by") == today_iso):
            urgent_ids.append((c["counterparty"] or "?", c["id"]))
        if len(urgent_ids) >= 3:
            break
    for who, pid in urgent_ids:
        rows.append([
            tg.button(f"✓ Done — {who}", f"done:{pid}"),
            tg.button("⏰ Snooze 3d", f"snooze:{pid}:3"),
        ])
    rows.append([tg.button("📊 Open dashboard", "dashboard:open")])
    return tg.inline_keyboard(rows)
