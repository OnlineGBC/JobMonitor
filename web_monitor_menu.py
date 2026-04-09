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
import base64
import itertools
import json
import logging
import os
import queue
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from flask import (
    Flask,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    url_for,
)

# ruamel.yaml preserves comments when editing monitors.yaml
from ruamel.yaml import YAML

from monitor import clear_baseline, configure_logging, get_snapshot_paths
from web_run_monitor import MonitorScheduler, get_run_history, get_total_run_count

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------
load_dotenv()

app = Flask(__name__)
app.secret_key = os.urandom(24)

# Single scheduler instance shared across requests
scheduler = MonitorScheduler()

# ruamel.yaml instance for reading/writing monitors.yaml with comments
ryaml = YAML()
ryaml.preserve_quotes = True
ryaml.allow_duplicate_keys = True

MONITORS_YAML = Path("monitors.yaml")

# ---------------------------------------------------------------------------
# LinkedIn login relay state
# ---------------------------------------------------------------------------
_display_counter = itertools.count(99)   # Xvfb display numbers (:99, :100, …)
_login_sessions: dict = {}               # monitor_name -> LinkedInLoginSession
_login_sessions_lock = threading.Lock()

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
    "Other": [
        ("CONFIG_PATH", "Path to monitors.yaml config file", False),
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
# Secret Manager REST helpers (no extra packages — uses urllib + metadata server)
# ---------------------------------------------------------------------------

def _sm_get_access_token() -> str:
    req = urllib.request.Request(
        "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token",
        headers={"Metadata-Flavor": "Google"},
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read())["access_token"]


def _sm_get_project_id() -> str:
    req = urllib.request.Request(
        "http://metadata.google.internal/computeMetadata/v1/project/project-id",
        headers={"Metadata-Flavor": "Google"},
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.read().decode()


def _sm_read_secret(secret_name: str):
    """Return latest version of a Secret Manager secret, or None on failure."""
    try:
        project_id = _sm_get_project_id()
        token = _sm_get_access_token()
        url = (
            f"https://secretmanager.googleapis.com/v1/projects/{project_id}"
            f"/secrets/{secret_name}/versions/latest:access"
        )
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            return base64.b64decode(data["payload"]["data"]).decode()
    except Exception as e:
        logging.warning(f"Secret Manager read failed for {secret_name}: {e}")
        return None


def _sm_write_secret(secret_name: str, value: str) -> bool:
    """Write a new version of a secret (creates the secret if it doesn't exist)."""
    try:
        project_id = _sm_get_project_id()
        token = _sm_get_access_token()
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        encoded = base64.b64encode(value.encode()).decode()
        version_payload = json.dumps({"payload": {"data": encoded}}).encode()
        add_url = (
            f"https://secretmanager.googleapis.com/v1/projects/{project_id}"
            f"/secrets/{secret_name}:addVersion"
        )

        # Try add-version first (secret already exists)
        req = urllib.request.Request(add_url, data=version_payload, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                resp.read()
                return True
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise

        # Secret doesn't exist — create it, then add version
        create_payload = json.dumps({"replication": {"automatic": {}}}).encode()
        create_url = (
            f"https://secretmanager.googleapis.com/v1/projects/{project_id}"
            f"/secrets?secretId={secret_name}"
        )
        req2 = urllib.request.Request(create_url, data=create_payload, headers=headers, method="POST")
        with urllib.request.urlopen(req2, timeout=10) as resp:
            resp.read()

        req3 = urllib.request.Request(add_url, data=version_payload, headers=headers, method="POST")
        with urllib.request.urlopen(req3, timeout=10) as resp:
            resp.read()
        return True
    except Exception as e:
        logging.warning(f"Secret Manager write failed for {secret_name}: {e}")
        return False


def _sm_save_linkedin_session(monitor_name: str, state_path: Path) -> bool:
    """Save LinkedIn storage state JSON to Secret Manager."""
    try:
        with open(state_path, "r", encoding="utf-8") as f:
            state_json = f.read()
        secret_name = f"LINKEDIN_STATE_{monitor_name.upper().replace('-', '_').replace(' ', '_')}"
        ok = _sm_write_secret(secret_name, state_json)
        if ok:
            logging.info(f"LinkedIn session for {monitor_name} saved to Secret Manager as {secret_name}")
        return ok
    except Exception as e:
        logging.warning(f"Failed to save LinkedIn session for {monitor_name}: {e}")
        return False


def _load_linkedin_sessions_from_secrets():
    """At startup on Cloud Run, restore LinkedIn state files from Secret Manager."""
    if not os.getenv("K_SERVICE"):
        return
    cfg = _load_monitors_yaml()
    Path("snapshots").mkdir(exist_ok=True)
    for m in cfg.get("monitors", []):
        name = m.get("name", "")
        if not name:
            continue
        secret_name = f"LINKEDIN_STATE_{name.upper().replace('-', '_').replace(' ', '_')}"
        state_json = _sm_read_secret(secret_name)
        if state_json:
            state_path = Path("snapshots") / f"{name}_linkedin_state.json"
            with open(state_path, "w", encoding="utf-8") as f:
                f.write(state_json)
            logging.info(f"Loaded LinkedIn session for {name} from Secret Manager → {state_path}")


# ---------------------------------------------------------------------------
# LinkedIn browser login relay
# ---------------------------------------------------------------------------

_BROWSER_WIDTH = 1366
_BROWSER_HEIGHT = 768


class LinkedInLoginSession:
    """Non-headless Playwright session for interactive LinkedIn login."""

    def __init__(self, monitor_name: str, monitor_url: str, username: str, password: str):
        self.monitor_name = monitor_name
        self.monitor_url = monitor_url
        self.username = username
        self.password = password
        self._cmd_queue: queue.Queue = queue.Queue()
        self._screenshot_lock = threading.Lock()
        self._screenshot_b64: str | None = None
        self._status_lock = threading.Lock()
        self._status = {"state": "starting", "message": "Initializing…"}
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._xvfb_proc: subprocess.Popen | None = None

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"linkedin-login-{self.monitor_name}")
        self._thread.start()

    def _set_status(self, state: str, message: str):
        with self._status_lock:
            self._status = {"state": state, "message": message}

    def get_status(self) -> dict:
        with self._status_lock:
            return dict(self._status)

    def get_screenshot_b64(self) -> str | None:
        with self._screenshot_lock:
            return self._screenshot_b64

    def _capture_screenshot(self, page):
        try:
            data = page.screenshot(type="jpeg", quality=70)
            encoded = base64.b64encode(data).decode()
            with self._screenshot_lock:
                self._screenshot_b64 = encoded
        except Exception:
            pass

    def send_command(self, cmd: dict):
        self._cmd_queue.put(cmd)

    def cancel(self):
        self._stop_event.set()
        self._cmd_queue.put({"type": "cancel"})

    def _run(self):
        in_container = bool(os.getenv("K_SERVICE"))
        display = None

        # Start a private Xvfb display in container environments
        if in_container:
            display_num = next(_display_counter)
            display = f":{display_num}"
            self._xvfb_proc = subprocess.Popen(
                ["Xvfb", display, "-screen", "0", f"{_BROWSER_WIDTH}x{_BROWSER_HEIGHT}x24"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            os.environ["DISPLAY"] = display
            time.sleep(1.5)  # Give Xvfb time to start

        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as pw:
                launch_args = []
                if in_container:
                    launch_args = [
                        "--no-sandbox",
                        "--disable-setuid-sandbox",
                        "--disable-dev-shm-usage",
                    ]
                browser = pw.chromium.launch(headless=False, args=launch_args)
                context = browser.new_context(viewport={"width": _BROWSER_WIDTH, "height": _BROWSER_HEIGHT})
                page = context.new_page()

                self._set_status("navigating", "Opening LinkedIn login page…")
                page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded", timeout=30000)
                self._capture_screenshot(page)

                # Auto-fill credentials
                if self.username:
                    try:
                        page.fill('input[name="session_key"]', self.username)
                        page.fill('input[name="session_password"]', self.password)
                        self._capture_screenshot(page)
                        self._set_status("ready", "Credentials filled. Click Sign in or solve any CAPTCHA, then wait.")
                    except Exception:
                        self._set_status("ready", "Ready — please log in to LinkedIn.")
                else:
                    self._set_status("ready", "Ready — please log in to LinkedIn.")

                # Interaction loop
                while not self._stop_event.is_set():
                    # Poll for login success
                    try:
                        current_url = page.url
                    except Exception:
                        break
                    if any(x in current_url for x in ["/feed", "/mynetwork", "/jobs", "/in/"]):
                        self._set_status("saving", "Login detected! Saving session…")
                        self._capture_screenshot(page)
                        state_path = Path("snapshots") / f"{self.monitor_name}_linkedin_state.json"
                        Path("snapshots").mkdir(exist_ok=True)
                        context.storage_state(path=str(state_path))
                        _sm_save_linkedin_session(self.monitor_name, state_path)
                        self._set_status("done", "Session saved. You can close this dialog.")
                        break

                    # Process one pending command (0.5 s timeout keeps the loop responsive)
                    try:
                        cmd = self._cmd_queue.get(timeout=0.5)
                    except queue.Empty:
                        self._capture_screenshot(page)
                        continue

                    if cmd["type"] == "cancel":
                        break
                    elif cmd["type"] == "click":
                        try:
                            page.mouse.click(cmd["x"], cmd["y"])
                        except Exception as e:
                            logging.warning(f"LinkedIn relay click failed: {e}")
                    elif cmd["type"] == "type":
                        try:
                            text = cmd["text"]
                            # Directly set the focused input's value via JS (more
                            # reliable than keyboard.type across React/SPA pages)
                            filled = page.evaluate("""(text) => {
                                const el = document.activeElement;
                                if (el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA')) {
                                    el.value = text;
                                    el.dispatchEvent(new Event('input', {bubbles: true}));
                                    el.dispatchEvent(new Event('change', {bubbles: true}));
                                    return true;
                                }
                                return false;
                            }""", text)
                            if not filled:
                                page.keyboard.type(text, delay=30)
                        except Exception as e:
                            logging.warning(f"LinkedIn relay type failed: {e}")
                    elif cmd["type"] == "key":
                        try:
                            page.keyboard.press(cmd["key"])
                        except Exception as e:
                            logging.warning(f"LinkedIn relay key failed: {e}")

                    self._capture_screenshot(page)

                context.close()
                browser.close()

        except Exception as e:
            logging.error(f"LinkedIn login session '{self.monitor_name}' error: {e}")
            self._set_status("error", f"Error: {e}")
        finally:
            if self._xvfb_proc:
                try:
                    self._xvfb_proc.terminate()
                except Exception:
                    pass
            with _login_sessions_lock:
                _login_sessions.pop(self.monitor_name, None)


# ---------------------------------------------------------------------------
# Page routes
# ---------------------------------------------------------------------------

@app.route("/")
def dashboard():
    cfg = _load_monitors_yaml()
    monitors_list = cfg.get("monitors", [])
    monitors = [_get_monitor_info(m) for m in monitors_list]
    total_runs = get_total_run_count()
    show_runs = request.args.get("runs", 20, type=int)
    show_runs = max(1, min(show_runs, total_runs)) if total_runs > 0 else 0
    return render_template(
        "dashboard.html",
        monitors=monitors,
        scheduler=scheduler.get_status(),
        run_history=get_run_history(show_runs),
        total_runs=total_runs,
        show_runs=show_runs,
    )


@app.route("/monitors")
def monitors():
    cfg = _load_monitors_yaml()
    monitors_list = cfg.get("monitors", [])
    monitors_info = [_get_monitor_info(m) for m in monitors_list]
    return render_template("monitors.html", monitors=monitors_info)


@app.route("/monitors/new")
def monitor_new():
    return render_template("monitor_edit.html", monitor=None)


@app.route("/monitors/<name>/edit")
def monitor_edit(name):
    cfg = _load_monitors_yaml()
    monitor = None
    for m in cfg.get("monitors", []):
        if m.get("name") == name:
            monitor = dict(m)
            break
    if not monitor:
        flash(f"Monitor '{name}' not found.", "error")
        return redirect(url_for("monitors"))
    return render_template("monitor_edit.html", monitor=monitor)


@app.route("/monitors/<name>/screenshots")
def screenshots(name):
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
def logs():
    return render_template("logs.html")


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------

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

    new_monitor = {
        "name": name,
        "url": url_val,
        "headless": "headless" in request.form,
        "wait_selector": request.form.get("wait_selector", "").strip() or "ul.jobs-search__results-list",
        "css_selector": request.form.get("css_selector", "").strip() or "ul.jobs-search__results-list",
    }
    monitors_list.append(new_monitor)
    cfg["monitors"] = monitors_list
    _save_monitors_yaml(cfg)
    flash(f"Monitor '{name}' created.", "success")
    return redirect(url_for("monitors"))


@app.route("/api/monitors/<name>", methods=["POST"])
def api_monitor_update(name):
    cfg = _load_monitors_yaml()
    monitors_list = cfg.get("monitors", [])

    found = False
    for m in monitors_list:
        if m.get("name") == name:
            m["url"] = request.form.get("url", "").strip()
            m["headless"] = "headless" in request.form
            m["wait_selector"] = request.form.get("wait_selector", "").strip() or "ul.jobs-search__results-list"
            m["css_selector"] = request.form.get("css_selector", "").strip() or "ul.jobs-search__results-list"
            found = True
            break

    if not found:
        flash(f"Monitor '{name}' not found.", "error")
        return redirect(url_for("monitors"))

    _save_monitors_yaml(cfg)
    flash(f"Monitor '{name}' updated.", "success")
    return redirect(url_for("monitors"))


@app.route("/api/monitors/<name>/delete", methods=["POST"])
def api_monitor_delete(name):
    cfg = _load_monitors_yaml()
    monitors_list = cfg.get("monitors", [])
    original_len = len(monitors_list)
    monitors_list = [m for m in monitors_list if m.get("name") != name]

    if len(monitors_list) == original_len:
        return jsonify({"status": "error", "message": f"Monitor '{name}' not found."})

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
        started = scheduler.run_on_demand(None)
    else:
        started = scheduler.run_on_demand(name)
    if started:
        return jsonify({"status": "ok", "message": f"Running {'all monitors' if name == 'all' else name}..."})
    return jsonify({"status": "warning", "message": "A run is already in progress."})


@app.route("/api/monitors/all/run", methods=["POST"])
def api_monitor_run_all():
    started = scheduler.run_on_demand(None)
    if started:
        return jsonify({"status": "ok", "message": "Running all monitors..."})
    return jsonify({"status": "warning", "message": "A run is already in progress."})


@app.route("/api/monitors/<name>/clear", methods=["POST"])
def api_monitor_clear(name):
    try:
        clear_baseline(name)
        return jsonify({"status": "ok", "message": f"Baseline cleared for '{name}'."})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})


@app.route("/api/scheduler/start", methods=["POST"])
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
def api_scheduler_stop():
    if scheduler.stop():
        return jsonify({"status": "ok", "message": "Scheduler stopped."})
    return jsonify({"status": "warning", "message": "Scheduler is not running."})


@app.route("/api/scheduler/status")
def api_scheduler_status():
    return jsonify(scheduler.get_status())


@app.route("/api/logs")
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
def api_log_download():
    log_path = Path("logs/screen_compare.log")
    if not log_path.exists():
        flash("No log file found.", "warning")
        return redirect(url_for("logs"))
    return send_from_directory("logs", "screen_compare.log", as_attachment=True)


@app.route("/api/screenshots/<name>/<filename>")
def api_screenshot(name, filename):
    """Serve a screenshot image file."""
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
# LinkedIn login relay API routes
# ---------------------------------------------------------------------------

@app.route("/api/linkedin/<name>/start", methods=["POST"])
def api_linkedin_start(name):
    with _login_sessions_lock:
        if name in _login_sessions:
            existing = _login_sessions[name]
            status = existing.get_status()
            if status["state"] not in ("done", "error"):
                return jsonify({"status": "warning", "message": "A login session is already active."})
            # Stale session — clean it up
            existing.cancel()
            del _login_sessions[name]

    cfg = _load_monitors_yaml()
    monitor_cfg = next((m for m in cfg.get("monitors", []) if m.get("name") == name), None)
    if not monitor_cfg:
        return jsonify({"status": "error", "message": f"Monitor '{name}' not found."})

    username = os.getenv("LINKEDIN_USERNAME", "")
    password = os.getenv("LINKEDIN_PASSWORD", "")

    session = LinkedInLoginSession(
        monitor_name=name,
        monitor_url=monitor_cfg.get("url", ""),
        username=username,
        password=password,
    )
    with _login_sessions_lock:
        _login_sessions[name] = session
    session.start()
    return jsonify({"status": "ok", "message": "LinkedIn browser session started."})


@app.route("/api/linkedin/<name>/screenshot")
def api_linkedin_screenshot(name):
    with _login_sessions_lock:
        session = _login_sessions.get(name)
    if not session:
        return jsonify({"status": "error", "message": "No active session."})
    img_b64 = session.get_screenshot_b64()
    if img_b64 is None:
        return jsonify({"status": "waiting", "message": "Screenshot not yet available."})
    return jsonify({"status": "ok", "image": img_b64})


@app.route("/api/linkedin/<name>/click", methods=["POST"])
def api_linkedin_click(name):
    with _login_sessions_lock:
        session = _login_sessions.get(name)
    if not session:
        return jsonify({"status": "error", "message": "No active session."})
    data = request.get_json(force=True, silent=True) or {}
    x = int(data.get("x", 0))
    y = int(data.get("y", 0))
    session.send_command({"type": "click", "x": x, "y": y})
    return jsonify({"status": "ok"})


@app.route("/api/linkedin/<name>/type", methods=["POST"])
def api_linkedin_type(name):
    with _login_sessions_lock:
        session = _login_sessions.get(name)
    if not session:
        return jsonify({"status": "error", "message": "No active session."})
    data = request.get_json(force=True, silent=True) or {}
    text = str(data.get("text", ""))
    session.send_command({"type": "type", "text": text})
    return jsonify({"status": "ok"})


@app.route("/api/linkedin/<name>/key", methods=["POST"])
def api_linkedin_key(name):
    with _login_sessions_lock:
        session = _login_sessions.get(name)
    if not session:
        return jsonify({"status": "error", "message": "No active session."})
    data = request.get_json(force=True, silent=True) or {}
    key = str(data.get("key", ""))
    session.send_command({"type": "key", "key": key})
    return jsonify({"status": "ok"})


@app.route("/api/linkedin/<name>/status")
def api_linkedin_status(name):
    with _login_sessions_lock:
        session = _login_sessions.get(name)
    if not session:
        return jsonify({"state": "none", "message": "No active session."})
    return jsonify(session.get_status())


@app.route("/api/linkedin/<name>/cancel", methods=["POST"])
def api_linkedin_cancel(name):
    with _login_sessions_lock:
        session = _login_sessions.get(name)
        if session:
            session.cancel()
            _login_sessions.pop(name, None)
    return jsonify({"status": "ok", "message": "Session cancelled."})


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

    # Ensure data directory exists
    Path("data").mkdir(exist_ok=True)

    # Restore LinkedIn sessions from Secret Manager (Cloud Run only)
    _load_linkedin_sessions_from_secrets()

    app.run(host=host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
