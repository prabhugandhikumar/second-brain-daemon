"""
Form login + bcrypt password verification + signed session cookies.

Users are stored in Secret Manager (key=DASHBOARD_USERS_YAML) as YAML:
    prabhu: $2b$12$...     # bcrypt hash
    ea: $2b$12$...

To add a user, run locally:
    python -c "import bcrypt; print(bcrypt.hashpw(b'newpassword', bcrypt.gensalt()).decode())"
Append `username: <hash>` to the YAML in Secret Manager.
"""

import logging
import os
import time
from typing import Optional

import bcrypt
import yaml
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

log = logging.getLogger(__name__)

SESSION_COOKIE_NAME = "secondbrain_session"
SESSION_MAX_AGE_SEC = 7 * 24 * 60 * 60  # 7 days


def _serializer() -> URLSafeTimedSerializer:
    secret = os.environ.get("SESSION_SECRET")
    if not secret:
        raise RuntimeError("SESSION_SECRET env var missing")
    return URLSafeTimedSerializer(secret, salt="secondbrain-auth")


def _load_users() -> dict[str, str]:
    """Return dict of username → bcrypt hash."""
    raw = os.environ.get("DASHBOARD_USERS_YAML", "")
    if not raw:
        log.warning("DASHBOARD_USERS_YAML is empty — no logins will work")
        return {}
    try:
        parsed = yaml.safe_load(raw) or {}
        return {str(k): str(v) for k, v in parsed.items()}
    except Exception as e:
        log.error("DASHBOARD_USERS_YAML parse error: %s", e)
        return {}


def check_password(username: str, password: str) -> bool:
    users = _load_users()
    hashed = users.get(username)
    if not hashed:
        return False
    try:
        return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False


def make_session_cookie(username: str) -> str:
    return _serializer().dumps({"u": username, "t": int(time.time())})


def read_session_cookie(cookie_value: Optional[str]) -> Optional[str]:
    """Return username if cookie is valid + not expired, else None."""
    if not cookie_value:
        return None
    try:
        data = _serializer().loads(cookie_value, max_age=SESSION_MAX_AGE_SEC)
        return data.get("u")
    except (BadSignature, SignatureExpired):
        return None
    except Exception:
        return None
