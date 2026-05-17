"""
Weekly Gemini model-chain refresh.

What this does, in plain English:
    1. Calls Gemini's list_models() to discover what Flash models are
       currently available to our API key.
    2. Picks the newest 2 specific-version Flash models and slots them
       into the fallback chain between gemini-flash-latest (always slot 0)
       and a stable last-resort model (always last).
    3. Compares to the current chain stored in Secret Manager.
    4. If unchanged: logs and exits.
    5. If changed: writes the new chain to Secret Manager, DMs Prabhu a
       one-liner with what changed.

Why this design:
    - gemini-flash-latest is always slot 0 so we get Google's newest
      whenever the auto-rotating alias has free-tier quota.
    - Specific-version slots 1-2 catch the case where -latest has tight
      quota (as happened with gemini-3-flash's 20/day cap on 2026-05-17)
      and we need a fallback that's stable and older but still capable.
    - Last slot is a known-good older model so we degrade gracefully even
      if the discovered list is wrong.

Triggered by Cloud Scheduler job `refresh-models` (weekly, Sunday 03:00 IST).
"""

import json
import logging
import os
import re
from typing import Optional

log = logging.getLogger(__name__)

# Default chain used at startup if no secret is set yet.
DEFAULT_CHAIN = ["gemini-flash-latest", "gemini-2.5-flash", "gemini-2.0-flash"]

# Slot 0 — always the auto-rotating alias. We don't replace this slot.
PRIMARY_MODEL = "gemini-flash-latest"

# Always-last fallback. Known stable, generous free tier historically.
LAST_RESORT_MODEL = "gemini-2.0-flash"

# How many specific-version models to keep between PRIMARY and LAST_RESORT.
SPECIFIC_VERSION_SLOTS = 2


def _flash_version_key(model_name: str) -> tuple[int, int]:
    """Sort key for Flash model names, newest first.

    e.g. 'models/gemini-3.1-flash' → (3, 1)
         'models/gemini-3-flash'   → (3, 0)
         'models/gemini-2.5-flash' → (2, 5)
    Non-matching names sort last.
    """
    name = model_name.split("/")[-1]
    m = re.match(r"gemini-(\d+)(?:\.(\d+))?-flash$", name)
    if not m:
        return (-1, -1)
    return (int(m.group(1)), int(m.group(2) or 0))


def build_chain(available_model_names: list[str]) -> list[str]:
    """Given the list of available model names, assemble the fallback chain."""
    # Keep only specific-version Flash models (skip -latest aliases, skip Pro)
    specific = [
        n.split("/")[-1] for n in available_model_names
        if "flash" in n.lower() and "-latest" not in n and "-exp" not in n
    ]
    # Newest first
    specific.sort(key=_flash_version_key, reverse=True)

    chain = [PRIMARY_MODEL]
    for v in specific:
        if v == LAST_RESORT_MODEL or v in chain:
            continue
        chain.append(v)
        if len(chain) - 1 >= SPECIFIC_VERSION_SLOTS:
            break
    if LAST_RESORT_MODEL not in chain:
        chain.append(LAST_RESORT_MODEL)
    return chain


def _current_chain_from_env() -> list[str]:
    raw = os.environ.get("GEMINI_MODEL_CHAIN", "")
    if not raw:
        return DEFAULT_CHAIN
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list) and all(isinstance(x, str) for x in parsed):
            return parsed
    except json.JSONDecodeError:
        pass
    return DEFAULT_CHAIN


def _write_secret(project_id: str, secret_id: str, value_json: str) -> None:
    """Add a new version to a Secret Manager secret. Create the secret if missing."""
    from google.cloud import secretmanager

    client = secretmanager.SecretManagerServiceClient()
    parent_secret = f"projects/{project_id}/secrets/{secret_id}"
    payload = {"data": value_json.encode("utf-8")}
    try:
        client.add_secret_version(parent=parent_secret, payload=payload)
        return
    except Exception as e:
        log.info("secret %s doesn't exist yet, creating: %s", secret_id, e)
    # Create then add the first version
    client.create_secret(
        parent=f"projects/{project_id}",
        secret_id=secret_id,
        secret={"replication": {"automatic": {}}},
    )
    client.add_secret_version(parent=parent_secret, payload=payload)


async def refresh_model_chain() -> dict:
    """Top-level entry point called by the /cron/refresh-models endpoint."""
    import google.generativeai as genai

    from lib import secrets
    from lib import telegram_client as tg

    genai.configure(api_key=secrets.get("GEMINI_API_KEY"))

    # 1. Discover
    available: list[str] = []
    try:
        for m in genai.list_models():
            methods = getattr(m, "supported_generation_methods", []) or []
            if "generateContent" in methods:
                available.append(m.name)
    except Exception as e:
        log.exception("list_models failed")
        return {"changed": False, "error": f"list_models: {e}"}

    log.info("discovered %d generateContent-capable models", len(available))

    # 2. Build chain
    proposed = build_chain(available)
    current = _current_chain_from_env()

    if proposed == current:
        log.info("model chain unchanged: %s", current)
        return {"changed": False, "chain": current, "discovered_count": len(available)}

    # 3. Write to Secret Manager
    project_id = os.environ.get("GCP_PROJECT_ID")
    if not project_id:
        log.error("GCP_PROJECT_ID not set — cannot update gemini-model-chain secret")
        return {
            "changed": False, "chain_current": current, "chain_proposed": proposed,
            "error": "GCP_PROJECT_ID not set",
        }

    try:
        _write_secret(project_id, "gemini-model-chain", json.dumps(proposed))
    except Exception as e:
        log.exception("failed to update gemini-model-chain secret")
        return {
            "changed": False, "chain_current": current, "chain_proposed": proposed,
            "error": f"secret write: {e}",
        }

    log.info("model chain updated: %s → %s", current, proposed)

    # 4. DM Prabhu
    owner_chat = os.environ.get("TELEGRAM_OWNER_CHAT_ID")
    if owner_chat:
        try:
            msg = (
                "🔁 *Gemini model chain updated*\n\n"
                f"*Was:* `{' → '.join(current)}`\n"
                f"*Now:* `{' → '.join(proposed)}`\n\n"
                "_Daemon picks up the change on next cold start. "
                "Trigger one by deploying or by being idle for ~15 min._"
            )
            await tg.send_message(int(owner_chat), msg)
        except Exception:
            log.exception("failed to send model-chain DM (non-fatal)")

    return {"changed": True, "chain_was": current, "chain_now": proposed}
