"""
Microsoft Graph email client for md@tabp.co.in (TABP M365 tenant).

Public-client OAuth flow:
  - Azure app registered with Mail.Read + offline_access delegated permissions
  - "Allow public client flows" = Yes (no client secret needed)
  - One-time device-code flow on Prabhu's Mac mints a long-lived refresh token
  - Daemon stores: ms-client-id, ms-tenant-id, ms-refresh-token in Secret Manager
  - Each call: refresh-token → access-token → Graph API

Public API:
    await list_recent_messages(since: datetime, limit: int = 50) -> list[dict]
    await get_message(message_id: str) -> dict
"""

import logging
from datetime import datetime, timezone
from typing import Optional

import httpx

from lib import secrets

log = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
SCOPES = ["https://graph.microsoft.com/Mail.Read"]


def _msal_app():
    """Build (and lazily cache) the MSAL PublicClientApplication."""
    global _APP
    if _APP is not None:
        return _APP
    from msal import PublicClientApplication
    client_id = secrets.get("MS_CLIENT_ID")
    tenant_id = secrets.get("MS_TENANT_ID")
    authority = f"https://login.microsoftonline.com/{tenant_id}"
    _APP = PublicClientApplication(client_id, authority=authority)
    return _APP


_APP = None
_CACHED_TOKEN: Optional[dict] = None  # {"token": str, "expires_at": datetime}


def _get_access_token() -> str:
    """Mint an access token from the stored refresh token.

    Caches in-process for the lifetime of the access token (~1 hour).
    """
    global _CACHED_TOKEN
    now = datetime.now(timezone.utc)
    if _CACHED_TOKEN and _CACHED_TOKEN["expires_at"] > now:
        return _CACHED_TOKEN["token"]

    app = _msal_app()
    refresh_token = secrets.get("MS_REFRESH_TOKEN")
    result = app.acquire_token_by_refresh_token(refresh_token, scopes=SCOPES)
    if "access_token" not in result:
        log.error("MSAL refresh failed: %s", result)
        raise RuntimeError(
            f"Could not refresh MS token: {result.get('error_description', result)}"
        )

    # Cache for slightly less than expires_in to give a safety margin
    expires_in = int(result.get("expires_in", 3600))
    from datetime import timedelta
    _CACHED_TOKEN = {
        "token": result["access_token"],
        "expires_at": now + timedelta(seconds=max(60, expires_in - 60)),
    }
    return result["access_token"]


async def list_recent_messages(since: datetime, limit: int = 50) -> list[dict]:
    """Return Outlook messages received after `since`, newest first.

    Each item is a dict with at least: id, subject, from, receivedDateTime,
    bodyPreview, importance, isRead, webLink.
    """
    access_token = _get_access_token()
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    since_iso = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    params = {
        "$filter": f"receivedDateTime ge {since_iso}",
        "$orderby": "receivedDateTime desc",
        "$top": min(limit, 100),
        "$select": (
            "id,subject,from,receivedDateTime,bodyPreview,"
            "importance,isRead,webLink,hasAttachments"
        ),
    }
    headers = {"Authorization": f"Bearer {access_token}"}
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(f"{GRAPH_BASE}/me/messages", params=params, headers=headers)
        if r.status_code >= 400:
            log.error("Graph list_messages failed: %s", r.text)
            r.raise_for_status()
        return r.json().get("value", [])


async def get_message(message_id: str) -> dict:
    """Fetch full body for a message id (used when bodyPreview isn't enough)."""
    access_token = _get_access_token()
    headers = {"Authorization": f"Bearer {access_token}"}
    params = {
        "$select": (
            "id,subject,from,toRecipients,ccRecipients,receivedDateTime,"
            "body,bodyPreview,importance,isRead,webLink,hasAttachments"
        ),
    }
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(
            f"{GRAPH_BASE}/me/messages/{message_id}",
            params=params, headers=headers,
        )
        r.raise_for_status()
        return r.json()
