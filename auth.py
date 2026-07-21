#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
=============================================================================
JobMonitor — Authentication
=============================================================================

User accounts for the web UI, stored in users.yaml.

There are no passwords. Signing in means asking for a one-time code, which is
emailed to the address over the SMTP settings already configured in .env. Proving
you can read that inbox is the whole login. Nothing to choose, store, or forget.

Each user is just an email and a role:

    users:
      - email: raja@onlinegbc.com
        role: admin
      - email: roopa@example.com
        role: user

An `admin` sees and edits everything. A `user` sees only the monitors whose
`owner` matches their email, and cannot reach settings, logs, or the scheduler
controls.

    python manage_users.py add roopa@example.com --role user

FILES CREATED:
    - users.yaml: Account list (gitignored)
"""

import logging
import os
import secrets
import time
from pathlib import Path

import yaml
from werkzeug.security import check_password_hash, generate_password_hash

USERS_YAML = Path("users.yaml")

ROLE_ADMIN = "admin"
ROLE_USER = "user"
VALID_ROLES = (ROLE_ADMIN, ROLE_USER)

# One-time login codes
CODE_LENGTH = 6
CODE_TTL_SECONDS = 600          # A code is good for 10 minutes
CODE_MAX_ATTEMPTS = 5           # Wrong guesses before a code is burned
CODE_RESEND_SECONDS = 60        # Minimum gap between sending codes to an address

# Pending codes, keyed by email. In memory only: codes are short-lived, and
# keeping them off disk means a restart cannot leave a valid code lying around.
_login_codes = {}


# =============================================================================
# Storage
# =============================================================================

def load_users() -> list:
    """Load all users from users.yaml. Returns [] if the file does not exist."""
    if not USERS_YAML.exists():
        return []
    try:
        with open(USERS_YAML, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception as e:
        logging.error(f"Could not read {USERS_YAML}: {e}")
        return []
    users = data.get("users") or []
    return [u for u in users if isinstance(u, dict) and u.get("email")]


def save_users(users: list) -> None:
    """Write the full user list back to users.yaml."""
    with open(USERS_YAML, "w", encoding="utf-8") as f:
        yaml.safe_dump({"users": users}, f, default_flow_style=False, sort_keys=False)


def normalize_email(email: str) -> str:
    """Emails are matched case-insensitively and without surrounding space."""
    return (email or "").strip().lower()


def get_user(email: str):
    """Find a user by email (case-insensitive). Returns the dict or None."""
    target = normalize_email(email)
    if not target:
        return None
    for u in load_users():
        if normalize_email(u.get("email")) == target:
            return u
    return None


def add_user(email: str, role: str = ROLE_USER) -> dict:
    """Create a user. Raises ValueError if the email already exists."""
    email = normalize_email(email)
    if not email:
        raise ValueError("Email is required")
    if "@" not in email:
        raise ValueError(f"'{email}' is not an email address")
    if role not in VALID_ROLES:
        raise ValueError(f"Role must be one of: {', '.join(VALID_ROLES)}")
    if get_user(email):
        raise ValueError(f"User '{email}' already exists")

    users = load_users()
    user = {"email": email, "role": role}
    users.append(user)
    save_users(users)
    return user


def set_role(email: str, role: str) -> None:
    """Change a user's role. Raises ValueError if they do not exist."""
    if role not in VALID_ROLES:
        raise ValueError(f"Role must be one of: {', '.join(VALID_ROLES)}")
    email = normalize_email(email)
    users = load_users()

    for u in users:
        if normalize_email(u.get("email")) == email:
            # Refuse to remove the last admin - that would lock everyone out of
            # settings, logs and the scheduler with no way back in via the UI.
            if u.get("role") == ROLE_ADMIN and role != ROLE_ADMIN:
                admins = [x for x in users if x.get("role") == ROLE_ADMIN]
                if len(admins) <= 1:
                    raise ValueError("Cannot demote the only admin")
            u["role"] = role
            save_users(users)
            return
    raise ValueError(f"User '{email}' not found")


def delete_user(email: str) -> None:
    """Remove a user. Refuses to delete the last admin."""
    email = normalize_email(email)
    users = load_users()
    target = None
    for u in users:
        if normalize_email(u.get("email")) == email:
            target = u
            break
    if not target:
        raise ValueError(f"User '{email}' not found")

    if target.get("role") == ROLE_ADMIN:
        admins = [x for x in users if x.get("role") == ROLE_ADMIN]
        if len(admins) <= 1:
            raise ValueError("Cannot delete the only admin")

    save_users([u for u in users if u is not target])


# =============================================================================
# One-time login codes
# =============================================================================

def _generate_code() -> str:
    """A numeric code, zero-padded, from a cryptographically secure source."""
    return f"{secrets.randbelow(10 ** CODE_LENGTH):0{CODE_LENGTH}d}"


def seconds_until_resend(email: str) -> int:
    """
    How long the caller must wait before another code may be sent.

    Stops the login form being used to flood someone's inbox.
    """
    entry = _login_codes.get(normalize_email(email))
    if not entry:
        return 0
    elapsed = time.time() - entry["created"]
    return max(0, int(CODE_RESEND_SECONDS - elapsed))


def issue_login_code(email: str) -> str:
    """
    Create and store a login code for this address, returning the plaintext.

    The caller emails it. Only a hash is retained, so the code cannot be read
    back out of the process afterwards.
    """
    email = normalize_email(email)
    code = _generate_code()
    _login_codes[email] = {
        "hash": generate_password_hash(code),
        "expires": time.time() + CODE_TTL_SECONDS,
        "created": time.time(),
        "attempts": 0,
    }
    return code


def verify_login_code(email: str, code: str):
    """
    Check a submitted code.

    Returns (user, error_message). On success error_message is None. A correct
    code is consumed, so it cannot be replayed.
    """
    email = normalize_email(email)
    entry = _login_codes.get(email)
    if not entry:
        return None, "That code has expired. Request a new one."

    if time.time() > entry["expires"]:
        _login_codes.pop(email, None)
        return None, "That code has expired. Request a new one."

    entry["attempts"] += 1
    if entry["attempts"] > CODE_MAX_ATTEMPTS:
        _login_codes.pop(email, None)
        return None, "Too many incorrect attempts. Request a new code."

    if not check_password_hash(entry["hash"], (code or "").strip()):
        return None, "That code is not correct."

    # Correct: burn the code, then confirm the account still exists
    _login_codes.pop(email, None)
    user = get_user(email)
    if not user:
        return None, "That account no longer exists."
    return user, None


def send_login_code(email: str, code: str) -> tuple:
    """
    Email a login code using the SMTP settings already configured in .env.

    Returns (sent, error_message).
    """
    # Imported lazily: monitor.py pulls in Playwright and friends, which are not
    # needed just to render a login page.
    from monitor import send_alert

    email_cfg = {
        "smtp_host": os.getenv("SMTP_HOST"),
        "smtp_port": os.getenv("SMTP_PORT", "587"),
        "smtp_username": os.getenv("SMTP_USERNAME"),
        "smtp_password": os.getenv("SMTP_PASSWORD"),
        "smtp_use_tls": os.getenv("SMTP_USE_TLS", "1"),
        "from_addr": os.getenv("FROM_ADDR"),
        "to_addrs": email,
    }
    missing = [k for k in ("smtp_host", "smtp_username", "smtp_password", "from_addr")
               if not email_cfg.get(k)]
    if missing:
        logging.error(f"Cannot send login code - SMTP not configured: {', '.join(missing)}")
        return False, "Email is not configured on the server, so codes cannot be sent."

    minutes = CODE_TTL_SECONDS // 60
    body = (
        f"Your JobMonitor sign-in code is:\n\n"
        f"    {code}\n\n"
        f"It expires in {minutes} minutes and can be used once.\n\n"
        f"If you did not try to sign in, you can ignore this email - "
        f"the code is useless without access to the sign-in page."
    )
    sent, error = send_alert(email_cfg, f"JobMonitor sign-in code: {code}", body)
    if sent:
        return True, None
    logging.error(f"Could not send login code to {email}: {error}")
    return False, "Could not send the code. Check the mail settings or try again."


# =============================================================================
# Authorization helpers
# =============================================================================

def is_admin(user) -> bool:
    """True if the given user dict is an admin."""
    return bool(user) and user.get("role") == ROLE_ADMIN


def owns_monitor(user, monitor) -> bool:
    """
    Whether `user` may see and edit `monitor`.

    Admins may touch anything. A regular user may touch a monitor only when its
    `owner` matches their email. A monitor with no owner is admin-only, so
    monitors that predate accounts are never exposed to a new user by default.
    """
    if not user:
        return False
    if is_admin(user):
        return True
    owner = normalize_email((monitor or {}).get("owner"))
    return bool(owner) and owner == normalize_email(user.get("email"))


def visible_monitors(user, monitors: list) -> list:
    """Filter a monitor list down to what `user` is allowed to see."""
    return [m for m in (monitors or []) if owns_monitor(user, m)]


# =============================================================================
# Flask secret key
# =============================================================================

def get_or_create_secret_key(env_path: Path = Path(".env")) -> str:
    """
    Return a stable Flask secret key, generating one into .env if absent.

    A random key per process would invalidate every session on restart, logging
    everyone out whenever the app is restarted.
    """
    key = os.getenv("FLASK_SECRET_KEY", "").strip()
    if key:
        return key

    key = secrets.token_hex(32)
    try:
        with open(env_path, "a", encoding="utf-8") as f:
            f.write(f"\n# Session signing key - generated automatically, keep secret\nFLASK_SECRET_KEY={key}\n")
        logging.info(f"Generated a new FLASK_SECRET_KEY into {env_path}")
    except Exception as e:
        logging.warning(f"Could not persist FLASK_SECRET_KEY ({e}) - sessions will reset on restart")
    os.environ["FLASK_SECRET_KEY"] = key
    return key
