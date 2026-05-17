"""
Secret loading — pulls from GCP Secret Manager in production,
falls back to env vars (loaded from .env) in local development.

Call `load_runtime_secrets()` once at app startup. After that, use
`get(name)` to read individual values from process memory.
"""

import logging
import os
from functools import lru_cache
from typing import Optional

log = logging.getLogger(__name__)

# Secret names — these are the canonical names in GCP Secret Manager.
# The daemon reads them at boot and caches in os.environ for downstream code.
SECRET_NAMES = [
    "telegram-bot-token",
    "gemini-api-key",
    "notion-api-token",
    "openai-api-key",         # deprecated 2026-05-16; safe to delete the secret
    "gmail-refresh-token",
    "gmail-client-id",
    "gmail-client-secret",
    "ms-client-id",
    "ms-tenant-id",
    "ms-refresh-token",       # public-client flow; no client secret needed
    "dashboard-users-yaml",
    "session-secret",
    "gemini-model-chain",     # JSON list, written by weekly refresh-models cron
    "email-poll-high-water",  # ISO timestamp, written by email_poll after each run
]

# Map Secret Manager names → env var names the rest of the app reads.
ENV_VAR_MAP = {
    "telegram-bot-token":   "TELEGRAM_BOT_TOKEN",
    "gemini-api-key":       "GEMINI_API_KEY",
    "notion-api-token":     "NOTION_API_TOKEN",
    "openai-api-key":       "OPENAI_API_KEY",
    "gmail-refresh-token":  "GMAIL_REFRESH_TOKEN",
    "gmail-client-id":      "GMAIL_CLIENT_ID",
    "gmail-client-secret":  "GMAIL_CLIENT_SECRET",
    "ms-client-id":         "MS_CLIENT_ID",
    "ms-tenant-id":         "MS_TENANT_ID",
    "ms-refresh-token":     "MS_REFRESH_TOKEN",
    "dashboard-users-yaml": "DASHBOARD_USERS_YAML",
    "session-secret":       "SESSION_SECRET",
    "gemini-model-chain":   "GEMINI_MODEL_CHAIN",
    "email-poll-high-water":"EMAIL_POLL_HIGH_WATER",
}


def _is_running_in_cloud_run() -> bool:
    """Cloud Run sets K_SERVICE; local dev doesn't."""
    return "K_SERVICE" in os.environ


def _gcp_project_id() -> Optional[str]:
    """Get the project ID — explicit env var wins, else metadata server."""
    pid = os.environ.get("GCP_PROJECT_ID") or os.environ.get("GOOGLE_CLOUD_PROJECT")
    if pid:
        return pid
    if _is_running_in_cloud_run():
        # In Cloud Run the metadata server has the project ID
        try:
            import requests
            r = requests.get(
                "http://metadata.google.internal/computeMetadata/v1/project/project-id",
                headers={"Metadata-Flavor": "Google"},
                timeout=2,
            )
            return r.text.strip()
        except Exception as e:
            log.warning("metadata project-id lookup failed: %s", e)
    return None


def _fetch_secret_from_manager(project_id: str, secret_name: str) -> Optional[str]:
    """Read the latest version of a secret from Secret Manager."""
    try:
        from google.cloud import secretmanager
        client = secretmanager.SecretManagerServiceClient()
        path = f"projects/{project_id}/secrets/{secret_name}/versions/latest"
        resp = client.access_secret_version(name=path)
        return resp.payload.data.decode("utf-8")
    except Exception as e:
        log.error("failed to fetch secret %s: %s", secret_name, e)
        return None


async def load_runtime_secrets() -> None:
    """
    Called once at app startup. In Cloud Run, pulls every named secret from
    Secret Manager and stuffs it into os.environ under the mapped env var.
    Locally, no-op (relies on .env or shell env).
    """
    if not _is_running_in_cloud_run():
        log.info("local dev — skipping Secret Manager, using env / .env")
        return

    project_id = _gcp_project_id()
    if not project_id:
        raise RuntimeError(
            "Running in Cloud Run but no project ID. "
            "Set GCP_PROJECT_ID or ensure metadata server is reachable."
        )

    log.info("loading %d secrets from Secret Manager for project=%s",
             len(SECRET_NAMES), project_id)

    loaded = 0
    for secret_name in SECRET_NAMES:
        value = _fetch_secret_from_manager(project_id, secret_name)
        if value is not None:
            env_var = ENV_VAR_MAP[secret_name]
            os.environ[env_var] = value
            loaded += 1
        else:
            log.warning("secret %s not loaded — downstream code may fail", secret_name)

    log.info("loaded %d/%d secrets", loaded, len(SECRET_NAMES))


@lru_cache(maxsize=None)
def get(name: str) -> str:
    """
    Convenience getter. Returns the value of an env var (which may have been
    loaded from Secret Manager at startup) and raises if missing.
    """
    value = os.environ.get(name)
    if value is None:
        raise RuntimeError(f"required secret/env var missing: {name}")
    return value


def get_optional(name: str, default: str = "") -> str:
    """Like get(), but returns default instead of raising."""
    return os.environ.get(name, default)
