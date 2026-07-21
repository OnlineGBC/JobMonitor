#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
=============================================================================
JobMonitor Web UI — Flask Application
=============================================================================

Browser-based management interface for the LinkedIn Job Monitor.
Provides dashboard, monitor CRUD, settings, logs, screenshots, and scheduler.

Usage:
    python web_monitor_menu.py                  # Start on default port 5000
    python web_monitor_menu.py --port 8080      # Start on custom port

Then open http://localhost:5000 in your browser.
"""

import argparse
import json
import logging
import os
import secrets
import shutil
import time
from datetime import datetime
from functools import wraps
from pathlib import Path

from dotenv import load_dotenv
from flask import (
    Flask,
    flash,
    has_request_context,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)

import auth

# ruamel.yaml preserves comments when editing monitors.yaml
from ruamel.yaml import YAML

from monitor import clear_baseline, configure_logging, get_snapshot_paths
from run_monitor import get_scheduler_ranges
from background_scheduler import MonitorScheduler, get_run_history, get_total_run_count

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------
load_dotenv()

app = Flask(__name__)
# Stable across restarts, so a restart does not log everyone out
app.secret_key = auth.get_or_create_secret_key()
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

# Set PUBLIC_URL in .env when the app is served through a tunnel or reverse
# proxy (see README). Its presence means "we are behind a proxy speaking HTTPS".
PUBLIC_URL = os.getenv("PUBLIC_URL", "").strip()

if PUBLIC_URL:
    # The proxy terminates TLS and talks plain HTTP to 127.0.0.1, so Flask sees
    # an insecure request and would otherwise refuse to mark cookies Secure and
    # would log every visitor's IP as 127.0.0.1. ProxyFix reads the X-Forwarded-*
    # headers to recover the real scheme and client address.
    #
    # Those headers are attacker-controlled on a directly reachable port. This is
    # only safe because the app stays bound to 127.0.0.1, so the proxy is the
    # only thing that can reach it. Do not bind to 0.0.0.0 with this enabled.
    from werkzeug.middleware.proxy_fix import ProxyFix

    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    app.config["SESSION_COOKIE_SECURE"] = True
    app.config["PREFERRED_URL_SCHEME"] = "https"
    # Announced from main() instead of here - this runs at import, before
    # configure_logging(), so anything logged now never reaches the log file.

# Single scheduler instance shared across requests
scheduler = MonitorScheduler()

# ruamel.yaml instance for reading/writing monitors.yaml with comments
ryaml = YAML()
ryaml.preserve_quotes = True
ryaml.allow_duplicate_keys = True

MONITORS_YAML = Path("monitors.yaml")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_monitors_yaml():
    """Load monitors.yaml with ruamel.yaml (preserves comments)."""
    if not MONITORS_YAML.exists():
        return {}
    with open(MONITORS_YAML, "r", encoding="utf-8") as f:
        return ryaml.load(f) or {}


def _save_monitors_yaml(data):
    """Save monitors.yaml with ruamel.yaml (preserves comments)."""
    with open(MONITORS_YAML, "w", encoding="utf-8") as f:
        ryaml.dump(data, f)


def _get_monitor_info(monitor_cfg):
    """Enrich a monitor config dict with filesystem info."""
    name = monitor_cfg.get("name", "Unknown")
    s1, s2, t1, t2, state = get_snapshot_paths(name)
    info = dict(monitor_cfg)
    info["has_baseline"] = s1.exists()
    info["thumbnail"] = s1.name if s1.exists() else None
    info["last_modified"] = None
    if s1.exists():
        mtime = s1.stat().st_mtime
        info["last_modified"] = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")
    return info


def _read_env():
    """Read .env file and return list of (key, value) tuples."""
    env_path = Path(".env")
    if not env_path.exists():
        return []
    lines = []
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                lines.append(("_comment", line))
                continue
            if "=" in line:
                key, _, value = line.partition("=")
                lines.append((key.strip(), value.strip()))
    return lines


def _write_env(env_vars):
    """Write environment variables to .env file, preserving comments."""
    env_path = Path(".env")
    existing_lines = []
    if env_path.exists():
        with open(env_path, "r", encoding="utf-8") as f:
            existing_lines = f.readlines()

    # Build a map of new values
    new_vals = dict(env_vars)
    written_keys = set()
    output = []

    for line in existing_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            output.append(line)
            continue
        if "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in new_vals:
                output.append(f"{key}={new_vals[key]}\n")
                written_keys.add(key)
            else:
                output.append(line)
        else:
            output.append(line)

    # Append any new keys not in the original file
    for key, value in env_vars:
        if key not in written_keys:
            output.append(f"{key}={value}\n")

    with open(env_path, "w", encoding="utf-8") as f:
        f.writelines(output)

    # Reload into process
    load_dotenv(override=True)


# Settings grouping for the UI
SECRET_KEYS = {"SMTP_PASSWORD", "LINKEDIN_PASSWORD", "ANTHROPIC_API_KEY"}

SETTINGS_GROUPS = {
    "SMTP / Email": [
        ("SMTP_HOST", "SMTP server hostname", False),
        ("SMTP_PORT", "SMTP port (587 for STARTTLS, 465 for SSL)", False),
        ("SMTP_USERNAME", "SMTP login username", False),
        ("SMTP_PASSWORD", "SMTP login password", True),
        ("SMTP_USE_TLS", "1 = STARTTLS, 0 = SSL", False),
        ("FROM_ADDR", "Sender email address", False),
        ("TO_ADDRS", "Recipient email addresses (comma-separated)", False),
        ("SUBJECT_PREFIX", "Email subject prefix", False),
    ],
    "LinkedIn": [
        ("LINKEDIN_USERNAME", "LinkedIn login email", False),
        ("LINKEDIN_PASSWORD", "LinkedIn login password", True),
    ],
    "API Keys": [
        ("ANTHROPIC_API_KEY", "Anthropic API key for Claude AI", True),
    ],
    "Webhooks": [
        ("SLACK_WEBHOOK_URL", "Slack incoming webhook URL", False),
        ("DISCORD_WEBHOOK_URL", "Discord webhook URL", False),
    ],
    "Scheduler (intervals in minutes)": [
        ("SCHED_BUSINESS_MIN", "Business-hours minimum interval (default 10)", False),
        ("SCHED_BUSINESS_MAX", "Business-hours maximum interval (default 15)", False),
        ("SCHED_OFFHOURS_MIN", "Off-hours minimum interval (default 115)", False),
        ("SCHED_OFFHOURS_MAX", "Off-hours maximum interval (default 125)", False),
    ],
    "Other": [
        ("CONFIG_PATH", "Path to monitors.yaml config file", False),
        ("PUBLIC_URL", "Public address if served through a tunnel/proxy - restart required", False),
    ],
}


def _get_settings_groups():
    """Build settings groups with current values from .env."""
    env_pairs = _read_env()
    env_dict = {k: v for k, v in env_pairs if k != "_comment"}
    groups = {}
    for group_name, fields in SETTINGS_GROUPS.items():
        vars_list = []
        for key, description, is_secret in fields:
            vars_list.append({
                "key": key,
                "value": env_dict.get(key, ""),
                "description": description,
                "is_secret": is_secret,
            })
        groups[group_name] = vars_list
    return groups


# ---------------------------------------------------------------------------
# CSRF protection
# ---------------------------------------------------------------------------

# Without this, any page on the internet could make your logged-in browser POST
# here - deleting monitors, rewriting settings - simply because the session
# cookie rides along automatically. The token lives in the session, which an
# attacker's page cannot read, so it cannot forge a valid request.

CSRF_SESSION_KEY = "csrf_token"
CSRF_FORM_FIELD = "csrf_token"
CSRF_HEADER = "X-CSRFToken"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}


def csrf_token():
    """The session's CSRF token, minting one on first use."""
    token = session.get(CSRF_SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        session[CSRF_SESSION_KEY] = token
    return token


@app.before_request
def verify_csrf():
    """
    Reject any state-changing request without a valid token.

    Registered before the login check so it applies to every route including
    the login form itself, and denies by default - a new POST route is
    protected without anyone remembering to opt in.
    """
    if request.method in SAFE_METHODS:
        return None
    if request.endpoint == "static":
        return None

    expected = session.get(CSRF_SESSION_KEY)
    supplied = request.form.get(CSRF_FORM_FIELD) or request.headers.get(CSRF_HEADER, "")

    if expected and supplied and secrets.compare_digest(str(expected), str(supplied)):
        return None

    logging.warning(
        f"CSRF check failed for {request.method} {request.path} from {request.remote_addr}"
    )
    if _wants_json():
        return jsonify({"status": "error", "message": "Security token expired. Reload the page."}), 400
    flash("Your session expired. Please try again.", "error")
    return redirect(request.referrer or url_for("dashboard"))


@app.context_processor
def inject_csrf_token():
    """Make csrf_token() callable from every template."""
    return {"csrf_token": csrf_token}


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

# Endpoints reachable without logging in. Everything else is denied by default,
# so adding a route cannot accidentally expose it.
PUBLIC_ENDPOINTS = {"login", "login_verify", "static"}

# Routes that live under /api/ but are posted by an HTML <form> and redirect
# afterwards. Denying one must flash and redirect, not return JSON the browser
# would render as raw text.
FORM_ENDPOINTS = {"api_monitor_create", "api_monitor_update"}


def _wants_json():
    """Whether this request should be answered with JSON rather than a redirect."""
    return request.path.startswith("/api/") and request.endpoint not in FORM_ENDPOINTS

# Code requests per email - stops the form being used to flood an inbox
_code_requests = {}
MAX_CODE_REQUESTS = 5

# Code requests per source address. The per-email cap alone is useless against
# someone cycling through addresses, which matters once the UI is public.
_code_requests_by_ip = {}
MAX_CODE_REQUESTS_PER_IP = 20

LOCKOUT_SECONDS = 900


def _client_ip():
    """The caller's address, or a placeholder outside a request context."""
    return request.remote_addr if has_request_context() else "-"


def _prune(store, key):
    now = time.time()
    recent = [t for t in store.get(key, []) if now - t < LOCKOUT_SECONDS]
    store[key] = recent
    return recent


def _record_request(email):
    _prune(_code_requests, email).append(time.time())
    _prune(_code_requests_by_ip, _client_ip()).append(time.time())


def _seconds_locked_out(email):
    """Seconds remaining before another code may be requested, 0 if free."""
    now = time.time()
    for store, key, cap in (
        (_code_requests, email, MAX_CODE_REQUESTS),
        (_code_requests_by_ip, _client_ip(), MAX_CODE_REQUESTS_PER_IP),
    ):
        recent = _prune(store, key)
        if len(recent) >= cap:
            return int(LOCKOUT_SECONDS - (now - min(recent)))
    return 0


def current_user():
    """The logged-in user dict, or None.

    Re-read from users.yaml on each request rather than trusted from the cookie,
    so a deleted account or a changed role takes effect immediately.
    """
    email = session.get("user_email")
    if not email:
        return None
    return auth.get_user(email)


@app.before_request
def require_login():
    """Deny every request that is not from a logged-in user."""
    if request.endpoint in PUBLIC_ENDPOINTS:
        return None

    if current_user():
        return None

    # Session refers to an account that no longer exists
    session.pop("user_email", None)

    if _wants_json():
        return jsonify({"status": "error", "message": "Not logged in."}), 401
    return redirect(url_for("login", next=request.path))


def admin_required(view):
    """Restrict a route to admins."""
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not auth.is_admin(current_user()):
            if _wants_json():
                return jsonify({"status": "error", "message": "Admins only."}), 403
            flash("That page is for administrators only.", "error")
            return redirect(url_for("dashboard"))
        return view(*args, **kwargs)
    return wrapper


def _find_monitor(cfg, name):
    """Return the monitor dict with this name, or None."""
    for m in cfg.get("monitors", []):
        if m.get("name") == name:
            return m
    return None


def _require_owned(cfg, name):
    """
    Fetch a monitor the current user is allowed to touch.

    Returns (monitor, error_response). A monitor that exists but belongs to
    someone else gives the same answer as one that does not exist, so the UI
    cannot be used to enumerate other people's monitor names.
    """
    monitor = _find_monitor(cfg, name)
    if monitor is not None and auth.owns_monitor(current_user(), monitor):
        return monitor, None

    if _wants_json():
        return None, (jsonify({"status": "error", "message": f"Monitor '{name}' not found."}), 404)
    flash(f"Monitor '{name}' not found.", "error")
    return None, redirect(url_for("monitors"))


@app.context_processor
def inject_user():
    """Make the current user available to every template."""
    user = current_user()
    return {"current_user": user, "is_admin": auth.is_admin(user)}


def _safe_next():
    """The post-login destination, restricted to same-site relative paths."""
    nxt = request.args.get("next") or request.form.get("next") or ""
    if nxt.startswith("/") and not nxt.startswith("//"):
        return nxt
    return url_for("dashboard")


@app.route("/login", methods=["GET", "POST"])
def login():
    """Step one: ask for an email address and send it a code."""
    if current_user():
        return redirect(url_for("dashboard"))

    nxt = _safe_next()

    if request.method == "POST":
        email = auth.normalize_email(request.form.get("email"))
        if not email:
            flash("Enter your email address.", "error")
            return render_template("login.html", email="", next=nxt)

        locked = _seconds_locked_out(email)
        if locked:
            flash(f"Too many code requests. Try again in {locked // 60 + 1} minute(s).", "error")
            return render_template("login.html", email=email, next=nxt)

        wait = auth.seconds_until_resend(email)
        if wait:
            flash(f"A code was just sent. Wait {wait} second(s) before asking for another.", "warning")
            session["pending_email"] = email
            return render_template("login_code.html", email=email, next=nxt)

        _record_request(email)
        user = auth.get_user(email)

        if user:
            code = auth.issue_login_code(email)
            sent, error = auth.send_login_code(email, code)
            if not sent:
                # A server-side mail failure is worth showing plainly - the user
                # would otherwise wait for a code that is never coming.
                flash(error, "error")
                return render_template("login.html", email=email, next=nxt)
            logging.info(f"Login code sent to {email}")
        else:
            # Same response either way, so the form cannot be used to discover
            # which addresses have accounts.
            logging.warning(f"Login code requested for unknown address '{email}' from {request.remote_addr}")

        session["pending_email"] = email
        flash("If that address has an account, a sign-in code is on its way.", "success")
        return render_template("login_code.html", email=email, next=nxt)

    if not auth.load_users():
        flash("No accounts exist yet. Run: python manage_users.py add you@example.com --role admin", "warning")
    return render_template("login.html", email="", next=nxt)


@app.route("/login/verify", methods=["POST"])
def login_verify():
    """Step two: check the code and start the session."""
    if current_user():
        return redirect(url_for("dashboard"))

    nxt = _safe_next()
    # Trust the session for the address, not the form, so the submitted code
    # can only ever be checked against the address that requested it.
    email = auth.normalize_email(session.get("pending_email"))
    code = request.form.get("code", "")

    if not email:
        flash("Start again - your sign-in attempt timed out.", "error")
        return redirect(url_for("login"))

    user, error = auth.verify_login_code(email, code)
    if not user:
        flash(error, "error")
        return render_template("login_code.html", email=email, next=nxt)

    session.clear()
    session["user_email"] = auth.normalize_email(user["email"])
    session.permanent = False
    _code_requests.pop(email, None)
    logging.info(f"Login: {user['email']} ({user.get('role')}) from {request.remote_addr}")
    return redirect(nxt)


@app.route("/logout")
def logout():
    user = current_user()
    if user:
        logging.info(f"Logout: {user['email']}")
    session.clear()
    flash("Signed out.", "success")
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# Page routes
# ---------------------------------------------------------------------------

def _visible_status(user, status, visible_names):
    """Blank out a running monitor's name if it is not one the user may see."""
    if auth.is_admin(user):
        return status
    status = dict(status)
    if status.get("current_monitor") not in visible_names:
        status["current_monitor"] = None
    return status


@app.route("/")
def dashboard():
    user = current_user()
    cfg = _load_monitors_yaml()
    monitors_list = auth.visible_monitors(user, cfg.get("monitors", []))
    monitors = [_get_monitor_info(m) for m in monitors_list]
    visible_names = {m.get("name") for m in monitors_list}

    # Run history covers every monitor, so a non-admin would otherwise read
    # other people's monitor names straight off the dashboard.
    if auth.is_admin(user):
        history = get_run_history(get_total_run_count())
    else:
        history = [r for r in get_run_history(get_total_run_count())
                   if r.get("monitor") in visible_names]

    total_runs = len(history)
    show_runs = request.args.get("runs", 20, type=int)
    show_runs = max(1, min(show_runs, total_runs)) if total_runs > 0 else 0
    return render_template(
        "dashboard.html",
        monitors=monitors,
        scheduler=_visible_status(user, scheduler.get_status(), visible_names),
        run_history=history[:show_runs],
        total_runs=total_runs,
        show_runs=show_runs,
        sched_ranges=get_scheduler_ranges(),
    )


@app.route("/monitors")
def monitors():
    cfg = _load_monitors_yaml()
    monitors_list = auth.visible_monitors(current_user(), cfg.get("monitors", []))
    monitors_info = [_get_monitor_info(m) for m in monitors_list]
    return render_template("monitors.html", monitors=monitors_info)


@app.route("/monitors/new")
def monitor_new():
    return render_template("monitor_edit.html", monitor=None)


@app.route("/monitors/<name>/edit")
def monitor_edit(name):
    cfg = _load_monitors_yaml()
    monitor, error = _require_owned(cfg, name)
    if error:
        return error
    return render_template("monitor_edit.html", monitor=dict(monitor))


@app.route("/monitors/<name>/screenshots")
def screenshots(name):
    cfg = _load_monitors_yaml()
    _, error = _require_owned(cfg, name)
    if error:
        return error
    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)
    snapshots_dir = Path("snapshots")
    screenshot_files = []
    if snapshots_dir.exists():
        for f in sorted(snapshots_dir.glob(f"{safe_name}_screenshot*.png")):
            stat = f.stat()
            screenshot_files.append({
                "filename": f.name,
                "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                "size": f"{stat.st_size / 1024:.1f} KB",
            })
    return render_template("screenshots.html", name=name, screenshots=screenshot_files)


@app.route("/settings", methods=["GET", "POST"])
@admin_required
def settings():
    if request.method == "POST":
        # Collect all form fields
        env_vars = []
        for group_fields in SETTINGS_GROUPS.values():
            for key, _, _ in group_fields:
                value = request.form.get(key, "")
                env_vars.append((key, value))
        _write_env(env_vars)
        flash("Settings saved successfully.", "success")
        return redirect(url_for("settings"))
    return render_template("settings.html", groups=_get_settings_groups())


@app.route("/logs")
@admin_required
def logs():
    return render_template("logs.html")


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------

def _resolve_owner(user, existing_owner=None):
    """
    Decide the owner to store for a monitor being created or updated.

    Admins may hand a monitor to anyone via the Owner field; the field is not
    rendered for regular users, and is ignored if one posts it anyway, so a user
    cannot assign a monitor away from themselves or claim someone else's.

    An admin editing someone else's monitor keeps that owner unless they
    deliberately type a different one - a blank field must not quietly transfer
    the monitor to the admin.
    """
    if auth.is_admin(user):
        requested = auth.normalize_email(request.form.get("owner"))
        if requested:
            return requested
        if existing_owner:
            return auth.normalize_email(existing_owner)
    return auth.normalize_email(user.get("email"))


@app.route("/api/monitors", methods=["POST"])
def api_monitor_create():
    name = request.form.get("name", "").strip()
    url_val = request.form.get("url", "").strip()
    if not name or not url_val:
        flash("Name and URL are required.", "error")
        return redirect(url_for("monitor_new"))

    cfg = _load_monitors_yaml()
    monitors_list = cfg.get("monitors", [])

    # Check for duplicate name
    for m in monitors_list:
        if m.get("name") == name:
            flash(f"Monitor '{name}' already exists.", "error")
            return redirect(url_for("monitor_new"))

    user = current_user()
    new_monitor = {
        "name": name,
        "url": url_val,
        "headless": "headless" in request.form,
        "enabled": "enabled" in request.form,
        "owner": _resolve_owner(user),
    }
    to_addrs = request.form.get("to_addrs", "").strip()
    if to_addrs:
        new_monitor["to_addrs"] = to_addrs
    monitors_list.append(new_monitor)
    cfg["monitors"] = monitors_list
    _save_monitors_yaml(cfg)
    flash(f"Monitor '{name}' created.", "success")
    return redirect(url_for("monitors"))


@app.route("/api/monitors/<name>", methods=["POST"])
def api_monitor_update(name):
    cfg = _load_monitors_yaml()
    m, error = _require_owned(cfg, name)
    if error:
        return error

    m["url"] = request.form.get("url", "").strip()
    m["headless"] = "headless" in request.form
    m["enabled"] = "enabled" in request.form
    m["owner"] = _resolve_owner(current_user(), m.get("owner"))
    # Blank means "use the global TO_ADDRS" - drop the key entirely so
    # the YAML does not carry an empty field that looks configured.
    to_addrs = request.form.get("to_addrs", "").strip()
    if to_addrs:
        m["to_addrs"] = to_addrs
    else:
        m.pop("to_addrs", None)

    _save_monitors_yaml(cfg)
    flash(f"Monitor '{name}' updated.", "success")
    return redirect(url_for("monitors"))


@app.route("/api/monitors/<name>/delete", methods=["POST"])
def api_monitor_delete(name):
    cfg = _load_monitors_yaml()
    _, error = _require_owned(cfg, name)
    if error:
        return error

    monitors_list = [m for m in cfg.get("monitors", []) if m.get("name") != name]
    cfg["monitors"] = monitors_list
    _save_monitors_yaml(cfg)

    # Clean up snapshot files
    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)
    snapshots_dir = Path("snapshots")
    if snapshots_dir.exists():
        for f in snapshots_dir.glob(f"{safe_name}_*"):
            f.unlink()

    return jsonify({"status": "ok", "message": f"Monitor '{name}' deleted."})


@app.route("/api/monitors/<name>/run", methods=["POST"])
def api_monitor_run(name):
    if name == "all":
        # "Run All" walks every monitor in the config, including other people's
        if not auth.is_admin(current_user()):
            return jsonify({"status": "error", "message": "Admins only."}), 403
        started = scheduler.run_on_demand(None)
    else:
        cfg = _load_monitors_yaml()
        _, error = _require_owned(cfg, name)
        if error:
            return error
        started = scheduler.run_on_demand(name)
    if started:
        return jsonify({"status": "ok", "message": f"Running {'all monitors' if name == 'all' else name}..."})
    return jsonify({"status": "warning", "message": "A run is already in progress."})


@app.route("/api/monitors/all/run", methods=["POST"])
@admin_required
def api_monitor_run_all():
    started = scheduler.run_on_demand(None)
    if started:
        return jsonify({"status": "ok", "message": "Running all monitors..."})
    return jsonify({"status": "warning", "message": "A run is already in progress."})


@app.route("/api/monitors/<name>/clear", methods=["POST"])
def api_monitor_clear(name):
    cfg = _load_monitors_yaml()
    _, error = _require_owned(cfg, name)
    if error:
        return error
    try:
        clear_baseline(name)
        return jsonify({"status": "ok", "message": f"Baseline cleared for '{name}'."})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})


@app.route("/api/scheduler/start", methods=["POST"])
@admin_required
def api_scheduler_start():
    custom_interval = None
    if request.is_json:
        custom_interval = request.json.get("custom_interval_minutes")
        if custom_interval is not None:
            custom_interval = int(custom_interval)
            if not (5 <= custom_interval <= 120):
                return jsonify({"status": "error", "message": "Interval must be between 5 and 120 minutes."})
    if scheduler.start(custom_interval_minutes=custom_interval):
        msg = "Scheduler started."
        if custom_interval:
            msg = f"Scheduler started with custom interval: every {custom_interval} minutes."
        return jsonify({"status": "ok", "message": msg})
    return jsonify({"status": "warning", "message": "Scheduler is already running."})


@app.route("/api/scheduler/stop", methods=["POST"])
@admin_required
def api_scheduler_stop():
    if scheduler.stop():
        return jsonify({"status": "ok", "message": "Scheduler stopped."})
    return jsonify({"status": "warning", "message": "Scheduler is not running."})


@app.route("/api/scheduler/status")
def api_scheduler_status():
    # Polled continuously by the dashboard - same name-leak guard as the page
    user = current_user()
    cfg = _load_monitors_yaml()
    visible_names = {m.get("name") for m in auth.visible_monitors(user, cfg.get("monitors", []))}
    return jsonify(_visible_status(user, scheduler.get_status(), visible_names))


@app.route("/api/scheduler/intervals", methods=["POST"])
@admin_required
def api_scheduler_intervals():
    """Save the four randomized-interval ranges (in minutes) to .env."""
    if not request.is_json:
        return jsonify({"status": "error", "message": "Expected JSON body."})
    payload = request.json or {}
    keys = ("business_min", "business_max", "offhours_min", "offhours_max")
    try:
        vals = {k: int(payload.get(k)) for k in keys}
    except (TypeError, ValueError):
        return jsonify({"status": "error", "message": "All four intervals must be integers."})

    for k, v in vals.items():
        if v < 1 or v > 1440:
            return jsonify({"status": "error", "message": f"{k} must be between 1 and 1440 minutes."})
    if vals["business_max"] < vals["business_min"]:
        return jsonify({"status": "error", "message": "Business-hours max must be ≥ min."})
    if vals["offhours_max"] < vals["offhours_min"]:
        return jsonify({"status": "error", "message": "Off-hours max must be ≥ min."})

    _write_env([
        ("SCHED_BUSINESS_MIN", str(vals["business_min"])),
        ("SCHED_BUSINESS_MAX", str(vals["business_max"])),
        ("SCHED_OFFHOURS_MIN", str(vals["offhours_min"])),
        ("SCHED_OFFHOURS_MAX", str(vals["offhours_max"])),
    ])
    return jsonify({"status": "ok", "message": "Interval ranges saved. Next cycle will use the new values."})


@app.route("/api/logs")
@admin_required
def api_logs():
    lines = request.args.get("lines", 300, type=int)
    log_path = Path("logs/screen_compare.log")
    if not log_path.exists():
        return jsonify({"content": "(No log file found)"})
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
        content = "".join(all_lines[-lines:])
        return jsonify({"content": content})
    except Exception as e:
        return jsonify({"content": f"Error reading log: {e}"})


@app.route("/api/logs/download")
@admin_required
def api_log_download():
    log_path = Path("logs/screen_compare.log")
    if not log_path.exists():
        flash("No log file found.", "warning")
        return redirect(url_for("logs"))
    return send_from_directory("logs", "screen_compare.log", as_attachment=True)


@app.route("/api/screenshots/<name>/<filename>")
def api_screenshot(name, filename):
    """Serve a screenshot image file."""
    # Ownership first: without this the URL is a way to read other people's
    # screenshots by guessing monitor names, bypassing the dashboard filter.
    cfg = _load_monitors_yaml()
    monitor = _find_monitor(cfg, name)
    if monitor is None or not auth.owns_monitor(current_user(), monitor):
        return "Not found", 404

    # Security: only allow png files from the snapshots directory
    if not filename.endswith(".png"):
        return "Not found", 404
    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)
    if not filename.startswith(safe_name):
        return "Not found", 404
    snapshots_dir = Path("snapshots")
    if not (snapshots_dir / filename).exists():
        return "Not found", 404
    return send_from_directory("snapshots", filename)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="JobMonitor Web UI")
    parser.add_argument("--port", type=int, default=5000, help="Port to listen on (default: 5000)")
    args = parser.parse_args()

    configure_logging()

    # Suppress repetitive /api/scheduler/status request logs
    class _QuietSchedulerPoll(logging.Filter):
        def filter(self, record):
            msg = record.getMessage()
            return "/api/scheduler/status" not in msg

    logging.getLogger("werkzeug").addFilter(_QuietSchedulerPoll())

    # Bind to 0.0.0.0 in container environments (Cloud Run), localhost otherwise
    import os as _os
    host = "0.0.0.0" if _os.getenv("K_SERVICE") else "127.0.0.1"

    logging.info(f"Starting JobMonitor Web UI on http://{host}:{args.port}")
    if PUBLIC_URL:
        logging.info(f"Public mode: behind a proxy at {PUBLIC_URL} - cookies marked Secure")
    else:
        logging.info("Local mode: no PUBLIC_URL set - cookies are not marked Secure")

    # Ensure data directory exists
    Path("data").mkdir(exist_ok=True)

    app.run(host=host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
