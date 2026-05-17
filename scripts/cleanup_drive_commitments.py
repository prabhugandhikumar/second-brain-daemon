#!/usr/bin/env python3
"""
One-off cleanup: archive Notion commitments that were auto-captured
from the Drive Brain Inbox sweep during the runaway poll on 2026-05-16.

Filter:
  - Notes field contains "[source: Drive file"  (signature of Drive captures)
  - Captured date is on_or_after (today - 2 days IST)

Usage:
  python3 scripts/cleanup_drive_commitments.py           # dry-run: lists matches
  python3 scripts/cleanup_drive_commitments.py --confirm # actually archive

Reads NOTION_API_TOKEN from `gcloud secrets versions access` so you don't
need to paste the token. Requires gcloud + httpx (already in requirements.txt).
"""

import argparse
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import httpx

NOTION_VERSION = "2025-09-03"
NOTION_API = "https://api.notion.com/v1"
COMMITMENTS_DS = "d42a2708-214e-4f31-8933-422dee8d0a09"  # from notion_structure.md
GCP_PROJECT = "tabp-second-brain"

LOOKBACK_DAYS = 2  # catch anything captured today or yesterday
DRIVE_NOTE_MARKER = "[source: Drive file"


def get_notion_token() -> str:
    """Pull the Notion API token from gcloud Secret Manager."""
    try:
        result = subprocess.run(
            [
                "gcloud", "secrets", "versions", "access", "latest",
                "--secret=notion-api-token",
                f"--project={GCP_PROJECT}",
            ],
            capture_output=True, text=True, check=True,
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError as e:
        sys.stderr.write(f"Failed to read notion-api-token from Secret Manager:\n{e.stderr}\n")
        sys.exit(1)


def query_matches(token: str) -> list[dict]:
    """Find commitments captured recently with the Drive-file marker in Notes."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).date().isoformat()
    body = {
        "filter": {
            "and": [
                {"property": "Captured", "date": {"on_or_after": cutoff}},
                {"property": "Notes", "rich_text": {"contains": DRIVE_NOTE_MARKER}},
            ]
        },
        "page_size": 100,
    }

    headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }

    matches: list[dict] = []
    cursor = None
    while True:
        if cursor:
            body["start_cursor"] = cursor
        r = httpx.post(
            f"{NOTION_API}/data_sources/{COMMITMENTS_DS}/query",
            json=body, headers=headers, timeout=30.0,
        )
        if r.status_code >= 400:
            sys.stderr.write(f"Notion query failed {r.status_code}:\n{r.text}\n")
            sys.exit(1)
        data = r.json()
        matches.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")

    return matches


def title_of(page: dict) -> str:
    props = page.get("properties", {})
    title_blocks = props.get("Commitment", {}).get("title", [])
    return " ".join(b.get("plain_text", "") for b in title_blocks)


def captured_at(page: dict) -> str:
    props = page.get("properties", {})
    cap = props.get("Captured", {})
    if "created_time" in cap:
        return cap["created_time"][:19]
    if "date" in cap:
        return (cap["date"] or {}).get("start", "")
    return ""


def archive_page(token: str, page_id: str) -> None:
    headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }
    r = httpx.patch(
        f"{NOTION_API}/pages/{page_id}",
        json={"archived": True}, headers=headers, timeout=30.0,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"archive failed {r.status_code}: {r.text}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--confirm", action="store_true",
                        help="Actually archive matched commitments. Default is dry-run.")
    args = parser.parse_args()

    print(f"Reading Notion token from Secret Manager (project={GCP_PROJECT})...")
    token = get_notion_token()

    print(f"Querying commitments captured in last {LOOKBACK_DAYS} day(s) "
          f"with Notes containing {DRIVE_NOTE_MARKER!r}...")
    matches = query_matches(token)

    if not matches:
        print("No matches found. Nothing to do.")
        return

    print(f"\nFound {len(matches)} commitment(s):\n")
    for i, page in enumerate(matches, 1):
        print(f"  {i:3d}. {captured_at(page)}  |  {title_of(page)[:80]}")

    if not args.confirm:
        print(f"\n(dry-run) Re-run with --confirm to archive these {len(matches)} commitments.")
        return

    print(f"\nArchiving {len(matches)} commitments...")
    success = 0
    failed = 0
    for page in matches:
        try:
            archive_page(token, page["id"])
            success += 1
        except Exception as e:
            failed += 1
            sys.stderr.write(f"  failed to archive {page['id']}: {e}\n")

    print(f"\nDone: archived={success} failed={failed}")
    if success:
        print("Note: archived pages are recoverable from Notion's trash for 30 days.")


if __name__ == "__main__":
    main()
