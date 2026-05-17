"""
Idempotent writes to Notion's Commitments / Tasks / Daily Briefings DBs.

Hits the Notion REST API directly (no SDK dep). Authentication via
NOTION_API_TOKEN — an internal integration token created at
https://www.notion.so/profile/integrations and shared with the
Second Brain workspace.

Public API:
    await add_commitment(commitment_dict, source_file_id, dedup_window_days=14)
    await add_task(task_dict, source_file_id=None)
    await mark_commitment_done(page_id, completion_note=None)
    await snooze_commitment(page_id, days=3)
    await get_open_commitments() -> list[dict]
"""

import logging
import os
from datetime import datetime, timedelta
from typing import Optional

import httpx
import pytz

from lib import secrets

log = logging.getLogger(__name__)

NOTION_VERSION = "2025-09-03"   # match the v2 schema (data sources)
NOTION_API = "https://api.notion.com/v1"
IST = pytz.timezone("Asia/Kolkata")


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={
            "Authorization": f"Bearer {secrets.get('NOTION_API_TOKEN')}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        },
        timeout=30.0,
    )


def _ds(name: str) -> str:
    """Look up a Notion data source ID by env var name (without the 'collection://' prefix)."""
    ds = os.environ.get(f"NOTION_{name}_DS")
    if not ds:
        raise RuntimeError(f"NOTION_{name}_DS env var missing")
    return ds


# ─────────────────────────────────────────────────────────────────────
# Commitment write — idempotent
# ─────────────────────────────────────────────────────────────────────
async def add_commitment(
    commitment: dict,
    source_file_id: Optional[str] = None,
    dedup_window_days: int = 14,
) -> dict:
    """
    Add a row to the Commitments DB. Before inserting, check if an open
    commitment with very similar `what` + same counterparty already exists
    within the last `dedup_window_days`. If so, return the existing row.

    Args:
        commitment: dict from prompts.py classify schema (keys: what, counterparty,
                    channel, source_thread, promised_on, due_by, action_type,
                    bucket, notes, urgency)
        source_file_id: optional Drive file ID for this commitment's source
                        screenshot — stored in Notes for backtracing
        dedup_window_days: skip insert if a similar row exists from the
                           last N days (default 14)

    Returns: the Notion page dict (newly created OR existing match).
    """
    existing = await _find_recent_match(commitment, days=dedup_window_days)
    if existing:
        log.info("dedup: matching commitment exists id=%s — skipping insert",
                 existing.get("id"))
        return existing

    notes = commitment.get("notes", "")
    if source_file_id:
        notes += f"\n\n[source: Drive file {source_file_id}]"

    properties = {
        "Commitment": {"title": [{"text": {"content": commitment["what"]}}]},
        "Counterparty": {"rich_text": [{"text": {"content": commitment.get("counterparty", "")}}]},
        "Counterparty Channel": {"select": {"name": commitment.get("channel", "Other")}},
        "Company": {"select": {"name": _normalize_bucket(commitment.get("bucket"))}},
        "Status": {"select": {"name": "Open"}},
        "Source Thread": {"rich_text": [{"text": {"content": commitment.get("source_thread", "")}}]},
        "Action Type": {"select": {"name": commitment.get("action_type", "Other")}},
        "Notes": {"rich_text": [{"text": {"content": notes[:1900]}}]},  # 2000 char cap
    }
    if commitment.get("promised_on"):
        properties["Promised On"] = {"date": {"start": commitment["promised_on"]}}
    if commitment.get("due_by"):
        properties["Due By"] = {"date": {"start": commitment["due_by"]}}

    body = {
        "parent": {"type": "data_source_id", "data_source_id": _ds("COMMITMENTS")},
        "properties": properties,
    }

    async with _client() as c:
        resp = await c.post(f"{NOTION_API}/pages", json=body)
        if resp.status_code >= 400:
            log.error("notion create failed %s: %s", resp.status_code, resp.text)
            resp.raise_for_status()
        page = resp.json()
        log.info("commitment added id=%s what=%r", page.get("id"), commitment["what"][:50])
        return page


async def _find_recent_match(commitment: dict, days: int = 14) -> Optional[dict]:
    """
    Look for an existing Open commitment with similar counterparty + similar
    title text in the last N days. Uses Notion's data source query with filters.
    """
    counterparty = (commitment.get("counterparty") or "").strip()
    if not counterparty:
        return None
    cutoff = (datetime.now(IST) - timedelta(days=days)).date().isoformat()

    body = {
        "filter": {
            "and": [
                {"property": "Status", "select": {"equals": "Open"}},
                {"property": "Counterparty", "rich_text": {"contains": counterparty[:30]}},
                {"timestamp": "created_time", "created_time": {"on_or_after": cutoff}},
            ]
        },
        "page_size": 10,
    }

    async with _client() as c:
        url = f"{NOTION_API}/data_sources/{_ds('COMMITMENTS')}/query"
        resp = await c.post(url, json=body)
        if resp.status_code >= 400:
            log.warning("notion dedup query failed %s: %s", resp.status_code, resp.text)
            return None
        results = resp.json().get("results", [])

    # Simple title overlap check
    new_title = (commitment.get("what") or "").lower()
    for row in results:
        title_blocks = row.get("properties", {}).get("Commitment", {}).get("title", [])
        existing_title = " ".join(b.get("plain_text", "") for b in title_blocks).lower()
        if _titles_overlap(new_title, existing_title):
            return row
    return None


def _titles_overlap(a: str, b: str, min_words: int = 4) -> bool:
    """Simple bag-of-words overlap. Considered a match if N+ significant words in common."""
    stop = {"the", "and", "to", "of", "for", "a", "with", "in", "on", "by", "at",
            "is", "be", "will", "from", "this", "that", "his", "her", "its"}
    a_words = {w for w in a.split() if len(w) > 3 and w not in stop}
    b_words = {w for w in b.split() if len(w) > 3 and w not in stop}
    return len(a_words & b_words) >= min_words


def _normalize_bucket(b: Optional[str]) -> str:
    """Map any free-form bucket to the canonical Notion select values."""
    if not b:
        return "Other Businesses"
    b = b.strip()
    canonical = {
        "tabp": "TABP",
        "beverages": "TABP",
        "tabps pets": "TABPS Pets",
        "tabps": "TABPS Pets",
        "pets": "TABPS Pets",
        "personal": "Personal",
        "other businesses": "Other Businesses",
        "other": "Other Businesses",
        "unknown": "Other Businesses",
    }
    return canonical.get(b.lower(), "Other Businesses")


# ─────────────────────────────────────────────────────────────────────
# Status updates
# ─────────────────────────────────────────────────────────────────────
async def mark_commitment_done(page_id: str, completion_note: Optional[str] = None) -> None:
    """Set Status=Done on a commitment row."""
    body = {
        "properties": {
            "Status": {"select": {"name": "Done"}},
        }
    }
    if completion_note:
        # Append to existing Notes (we don't fetch first; we just overwrite — caller can be careful)
        body["properties"]["Notes"] = {
            "rich_text": [{"text": {"content": f"DONE: {completion_note}"[:1900]}}]
        }

    async with _client() as c:
        resp = await c.patch(f"{NOTION_API}/pages/{page_id}", json=body)
        resp.raise_for_status()
        log.info("commitment marked done id=%s", page_id)


async def snooze_commitment(page_id: str, days: int = 3) -> None:
    """Push the Due By field out by N days."""
    new_due = (datetime.now(IST) + timedelta(days=days)).date().isoformat()
    body = {"properties": {"Due By": {"date": {"start": new_due}}}}
    async with _client() as c:
        resp = await c.patch(f"{NOTION_API}/pages/{page_id}", json=body)
        resp.raise_for_status()
        log.info("commitment snoozed id=%s new_due=%s", page_id, new_due)


# ─────────────────────────────────────────────────────────────────────
# Queries
# ─────────────────────────────────────────────────────────────────────
async def get_open_commitments(limit: int = 50) -> list[dict]:
    """Return all open commitments, oldest first by due date."""
    body = {
        "filter": {"property": "Status", "select": {"equals": "Open"}},
        "sorts": [{"property": "Due By", "direction": "ascending"}],
        "page_size": min(limit, 100),
    }
    async with _client() as c:
        resp = await c.post(f"{NOTION_API}/data_sources/{_ds('COMMITMENTS')}/query", json=body)
        resp.raise_for_status()
        return resp.json().get("results", [])


# ─────────────────────────────────────────────────────────────────────
# Daily briefing row
# ─────────────────────────────────────────────────────────────────────
async def add_briefing_row(briefing: dict) -> dict:
    """Write the morning briefing as a row in the Daily Briefings DB."""
    today = datetime.now(IST).date().isoformat()
    body = {
        "parent": {"type": "data_source_id", "data_source_id": _ds("BRIEFINGS")},
        "properties": {
            "Date": {"title": [{"text": {"content": today}}]},
            "Highlights": {"rich_text": [{"text": {"content": briefing.get("lead", "")[:1900]}}]},
            "Decisions Needed": {"rich_text": [{"text": {"content": "\n".join(briefing.get("decisions_needed", []))[:1900]}}]},
            "Reports Overdue": {"number": len(briefing.get("urgent", []))},
        },
    }
    async with _client() as c:
        resp = await c.post(f"{NOTION_API}/pages", json=body)
        resp.raise_for_status()
        return resp.json()
