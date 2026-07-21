#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
=============================================================================
JobMonitor — Authentication
=============================================================================

User accounts for the web UI, stored in users.yaml.

Each user is an email, a password hash, and a role:

    users:
      - email: raja@onlinegbc.com
        password_hash: "scrypt:32768:8:1$..."
        role: admin
      - email: roopa@example.com
        password_hash: "scrypt:32768:8:1$..."
        role: user

An `admin` sees and edits everything. A `user` sees only the monitors whose
`owner` matches their email, and cannot reach settings, logs, or the scheduler
controls.

Passwords are never stored - only Werkzeug hashes. Manage accounts with:

    python manage_users.py add roopa@example.com --role user

FILES CREATED:
    - users.yaml: Account list (gitignored - contains password hashes)
"""

import logging
import os
import secrets
from pathlib import Path

import yaml
from werkzeug.security import check_password_hash, generate_password_hash

USERS_YAML = Path("users.yaml")

ROLE_ADMIN = "admin"
ROLE_USER = "user"
VALID_ROLES = (ROLE_ADMIN, ROLE_USER)


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


def verify_credentials(email: str, password: str):
    """
    Check an email/password pair.

    Returns the user dict on success, None on failure. Deliberately does not
    distinguish "no such user" from "wrong password" to the caller.
    """
    user = get_user(email)
    if not user:
        return None
    stored = user.get("password_hash") or ""
    if not stored:
        return None
    try:
        if check_password_hash(stored, password or ""):
            return user
    except Exception as e:
        logging.error(f"Malformed password hash for {user.get('email')}: {e}")
    return None


def add_user(email: str, password: str, role: str = ROLE_USER) -> dict:
    """Create a user. Raises ValueError if the email already exists."""
    email = normalize_email(email)
    if not email:
        raise ValueError("Email is required")
    if role not in VALID_ROLES:
        raise ValueError(f"Role must be one of: {', '.join(VALID_ROLES)}")
    if not password:
        raise ValueError("Password is required")
    if get_user(email):
        raise ValueError(f"User '{email}' already exists")

    users = load_users()
    user = {
        "email": email,
        "password_hash": generate_password_hash(password),
        "role": role,
    }
    users.append(user)
    save_users(users)
    return user


def set_password(email: str, password: str) -> None:
    """Replace a user's password. Raises ValueError if they do not exist."""
    if not password:
        raise ValueError("Password is required")
    email = normalize_email(email)
    users = load_users()
    for u in users:
        if normalize_email(u.get("email")) == email:
            u["password_hash"] = generate_password_hash(password)
            save_users(users)
            return
    raise ValueError(f"User '{email}' not found")


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
