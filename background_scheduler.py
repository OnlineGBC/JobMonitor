#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Background scheduler for running monitors on a schedule.
Runs in a daemon thread so it dies when the Flask app exits.
"""

import json
import logging
import random
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from run_monitor import (
    EXIT_CODE_DESCRIPTIONS,
    get_eastern_time,
    get_min_interval_minutes,
    get_sleep_interval,
    is_business_hours,
)

# Cap run history at this many entries
MAX_HISTORY = 500
HISTORY_PATH = Path("data/run_history.json")

# How often the loop wakes to see whether anything is due
TICK_SECONDS = 20

# Minimum spacing between any two monitor runs. Runs stay serialized and spaced
# no matter what intervals users pick, so LinkedIn sees a steady trickle rather
# than bursts however many monitors come due together.
MIN_GAP_SECONDS = 120

# Longest interval a monitor may ask for
MAX_INTERVAL_MINUTES = 1440


def now_seconds():
    """Wall clock, wrapped so tests can stub the module's time source."""
    return time.time()


def _load_history():
    """Load run history from disk."""
    if HISTORY_PATH.exists():
        try:
            with open(HISTORY_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return []
    return []


def _save_history(history):
    """Save run history to disk (capped at MAX_HISTORY)."""
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    history = history[-MAX_HISTORY:]
    with open(HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2)


class MonitorScheduler:
    """Runs monitors on a schedule in a background thread."""

    def __init__(self):
        self._thread = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._running = False
        self._current_monitor = None
        self._last_run = None
        self._next_run_time = None
        self._run_in_progress = False
        self._on_demand_threads = []
        self._custom_interval_minutes = None
        # name -> epoch seconds when that monitor is next due
        self._next_due = {}
        # monitors whose next run is a login-failure retry
        self._retry_pending = set()

    @property
    def running(self):
        return self._running and self._thread is not None and self._thread.is_alive()

    def next_due_for(self, names=None):
        """
        Seconds until the next run among `names`, or None if nothing is scheduled.

        Passing a name set answers "when does something of *mine* run next",
        which is what a user should see - the global next-due time belongs to
        whichever monitor happens to be first, quite possibly someone else's.
        """
        if not self._running:
            return None
        due = self._next_due
        if names is not None:
            due = {k: v for k, v in due.items() if k in names}
        if not due:
            return None
        return max(0, int(min(due.values()) - time.time()))

    def get_status(self, names=None):
        """
        Return scheduler status as a dict.

        With `names`, the countdown and the currently-running monitor are limited
        to that set, so one user's panel never reports another user's activity.
        """
        if names is None:
            next_run_seconds = 0
            if self._next_run_time and self._running:
                next_run_seconds = max(0, int(self._next_run_time - now_seconds()))
        else:
            next_run_seconds = self.next_due_for(names)

        current = self._current_monitor
        if names is not None and current not in names:
            current = None

        return {
            "running": self.running,
            "current_monitor": current,
            "last_run": self._last_run,
            "next_run_seconds": next_run_seconds,
            "run_in_progress": self._run_in_progress or self._current_monitor is not None,
            "custom_interval_minutes": self._custom_interval_minutes,
        }

    def start(self, custom_interval_minutes=None):
        """Start the scheduler loop. Optionally pass a custom interval in minutes."""
        if self.running:
            return False
        self._stop_event.clear()
        self._running = True
        self._custom_interval_minutes = custom_interval_minutes
        # Everything is due immediately on a fresh start
        self._next_due = {}
        self._retry_pending = set()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="monitor-scheduler")
        self._thread.start()
        if custom_interval_minutes:
            logging.info(f"Scheduler started with custom interval: {custom_interval_minutes} min")
        else:
            logging.info("Scheduler started")
        return True

    def stop(self):
        """Stop the scheduler loop."""
        if not self.running:
            return False
        self._stop_event.set()
        self._running = False
        logging.info("Scheduler stop requested")
        return True

    def _sleep_interruptible(self, seconds):
        """
        Sleep in 1-second increments so we can respond to stop signals.

        Deliberately does not touch _next_run_time: with per-monitor schedules
        the next run is the earliest due time, not the end of this nap.
        """
        end_time = time.time() + seconds
        while time.time() < end_time:
            if self._stop_event.is_set():
                return False
            time.sleep(1)
        return True

    def _is_monitor_enabled(self, monitor_name):
        """
        Re-read monitors.yaml to check whether a monitor is still enabled.

        A cycle takes minutes to walk (2-min gaps between monitors, plus login
        retries), so the list captured at the top of the cycle goes stale. Pausing
        a monitor from the web UI has to take effect on this cycle, not the next.

        Fails open: if the config can't be read, run the monitor as already
        decided rather than silently skipping everything.
        """
        import yaml
        try:
            with open("monitors.yaml", "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
        except Exception as e:
            logging.warning(f"Could not re-check enabled flag for {monitor_name}: {e}")
            return True

        for m in cfg.get("monitors", []):
            if m.get("name") == monitor_name:
                return bool(m.get("enabled", True))
        # Monitor was deleted mid-cycle
        return False

    def _run_single_monitor(self, monitor_name):
        """Run monitor.py for a single monitor. Returns exit code."""
        script_dir = Path(__file__).parent
        monitor_script = script_dir / "monitor.py"
        python_exe = sys.executable

        self._current_monitor = monitor_name
        try:
            result = subprocess.run(
                [python_exe, str(monitor_script), "--monitor", monitor_name],
                cwd=str(script_dir),
                timeout=600,  # 10 minute timeout
            )
            return result.returncode
        except subprocess.TimeoutExpired:
            logging.error(f"Monitor {monitor_name} timed out after 600s")
            return -1
        except Exception as e:
            logging.error(f"Error running monitor {monitor_name}: {e}")
            return -1
        finally:
            self._current_monitor = None

    def _record_run(self, monitor_name, exit_code, duration=None):
        """Record a run result to history."""
        et_now = get_eastern_time()
        entry = {
            "monitor": monitor_name,
            "timestamp": et_now.strftime("%Y-%m-%d %H:%M:%S ET"),
            "exit_code": exit_code,
            "description": EXIT_CODE_DESCRIPTIONS.get(exit_code, "OK" if exit_code == 0 else f"Unknown error ({exit_code})"),
        }
        if duration is not None:
            entry["duration_seconds"] = round(duration, 1)

        history = _load_history()
        history.append(entry)
        _save_history(history)

    def interval_seconds_for(self, monitor):
        """
        How long until this monitor should run again.

        A monitor may set its own `interval_minutes`, clamped to the admin's
        floor so nobody can schedule themselves into a rate limit. Without one
        it keeps the shared business-hours / off-hours behaviour. An admin's
        custom interval, chosen when starting the scheduler, overrides both.

        The result is jittered so repeated runs do not land on a machine-perfect
        cadence.
        """
        if self._custom_interval_minutes:
            return self._custom_interval_minutes * 60

        raw = monitor.get("interval_minutes")
        minutes = 0
        if raw not in (None, ""):
            try:
                minutes = int(raw)
            except (TypeError, ValueError):
                logging.warning(
                    f"Scheduler: {monitor.get('name')} has an unreadable "
                    f"interval_minutes ({raw!r}) - using the default schedule"
                )
                minutes = 0

        if minutes <= 0:
            return get_sleep_interval(get_eastern_time())

        floor = get_min_interval_minutes()
        clamped = max(floor, min(minutes, MAX_INTERVAL_MINUTES))
        if clamped != minutes:
            logging.info(
                f"Scheduler: {monitor.get('name')} asked for {minutes} min, "
                f"using {clamped} min (allowed range {floor}-{MAX_INTERVAL_MINUTES})"
            )

        base = clamped * 60
        jitter = int(base * 0.1)
        return random.randint(base - jitter, base + jitter)

    def _due_monitors(self, enabled, now):
        """Enabled monitors whose turn has come, soonest first."""
        due = [m for m in enabled if self._next_due.get(m["name"], 0) <= now]
        due.sort(key=lambda m: self._next_due.get(m["name"], 0))
        return due

    def _loop(self):
        """
        Main scheduler loop.

        Each monitor carries its own next-due time, so one person's cadence does
        not dictate anyone else's. Runs stay serialized and spaced by
        MIN_GAP_SECONDS: users pick how often their monitor runs, not how hard
        LinkedIn gets hit.
        """
        logging.info("Scheduler loop started")

        import yaml
        config_path = Path("monitors.yaml")
        last_finished = 0.0
        announced_skips = set()

        while not self._stop_event.is_set():
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    cfg = yaml.safe_load(f) or {}
                all_monitors = [m for m in cfg.get("monitors", []) if m.get("name")]
            except Exception as e:
                logging.error(f"Scheduler: cannot read monitors.yaml: {e}")
                self._sleep_interruptible(60)
                continue

            enabled = [m for m in all_monitors if m.get("enabled", True)]
            enabled_names = {m["name"] for m in enabled}

            skipped = {m["name"] for m in all_monitors} - enabled_names
            if skipped and skipped != announced_skips:
                logging.info(f"Scheduler: skipping disabled monitor(s): {', '.join(sorted(skipped))}")
            announced_skips = skipped

            # Forget monitors that were paused or deleted, so re-enabling one
            # makes it due immediately rather than resuming an old countdown
            for gone in set(self._next_due) - enabled_names:
                self._next_due.pop(gone, None)
                self._retry_pending.discard(gone)

            if not enabled:
                self._next_run_time = None
                self._sleep_interruptible(60)
                continue

            now = time.time()
            for m in enabled:
                self._next_due.setdefault(m["name"], now)

            self._next_run_time = min(self._next_due.values())

            # Keep runs spaced apart no matter how many came due at once
            since_last = now - last_finished
            if since_last < MIN_GAP_SECONDS:
                self._sleep_interruptible(min(TICK_SECONDS, MIN_GAP_SECONDS - since_last))
                continue

            due = self._due_monitors(enabled, now)
            if not due:
                self._sleep_interruptible(TICK_SECONDS)
                continue

            monitor = due[0]
            name = monitor["name"]

            # Config was read a moment ago; confirm before spending a run on it
            if not self._is_monitor_enabled(name):
                logging.info(f"Scheduler: {name} was paused - skipping")
                self._next_due.pop(name, None)
                continue

            was_retry = name in self._retry_pending
            self._retry_pending.discard(name)

            self._run_in_progress = True
            start_time = time.time()
            exit_code = self._run_single_monitor(name)
            duration = time.time() - start_time
            self._run_in_progress = False
            last_finished = time.time()

            self._record_run(name, exit_code, duration)
            self._last_run = get_eastern_time().strftime("%Y-%m-%d %H:%M:%S ET")

            if exit_code == 11:
                # Nothing was reachable. Not a fault to retry aggressively or
                # alert about - just wait for this monitor's next turn.
                interval = self.interval_seconds_for(monitor)
                self._next_due[name] = last_finished + interval
                logging.info(
                    f"Scheduler: {name} skipped (network unavailable), "
                    f"next attempt in {interval / 60:.1f} min"
                )
            elif exit_code == 10 and not was_retry:
                # Retry once, but by scheduling it rather than blocking here -
                # other people's monitors should not wait out someone's retry
                logging.warning(f"Login failed for {name} - retrying in 10 minutes")
                self._retry_pending.add(name)
                self._next_due[name] = last_finished + 600
            else:
                if was_retry and exit_code != 0:
                    logging.error(f"Retry also failed for {name} (exit {exit_code})")
                interval = self.interval_seconds_for(monitor)
                self._next_due[name] = last_finished + interval
                logging.info(f"Scheduler: {name} next run in {interval / 60:.1f} min")

            self._next_run_time = min(self._next_due.values())

        self._running = False
        self._next_run_time = None
        logging.info("Scheduler loop ended")

    def run_on_demand(self, monitor_name=None):
        """
        Run monitor(s) on demand in a background thread.
        If monitor_name is None, runs all monitors.
        Returns True if started, False if already running.
        """
        if self._run_in_progress:
            return False

        def _run():
            self._run_in_progress = True
            try:
                import yaml
                if monitor_name:
                    names = [monitor_name]
                else:
                    with open("monitors.yaml", "r", encoding="utf-8") as f:
                        cfg = yaml.safe_load(f) or {}
                    all_monitors = cfg.get("monitors", [])
                    names = [m["name"] for m in all_monitors if "name" in m and m.get("enabled", True)]
                    skipped = [m["name"] for m in all_monitors if "name" in m and not m.get("enabled", True)]
                    if skipped:
                        logging.info(f"Run All: skipping disabled monitor(s): {', '.join(skipped)}")

                for idx, name in enumerate(names):
                    # Skip a monitor paused since the list was built, but only for
                    # "Run All" - an explicit single-monitor Run is a deliberate click
                    # and runs even when paused.
                    if not monitor_name and not self._is_monitor_enabled(name):
                        logging.info(f"Run All: {name} was paused mid-run - skipping")
                        continue
                    start_time = time.time()
                    exit_code = self._run_single_monitor(name)
                    duration = time.time() - start_time
                    self._record_run(name, exit_code, duration)
                    et_now = get_eastern_time()
                    self._last_run = et_now.strftime("%Y-%m-%d %H:%M:%S ET")
                    # 2-minute delay between monitors (skip after last one)
                    if idx < len(names) - 1:
                        logging.info(f"Waiting 2 minutes before next monitor...")
                        time.sleep(120)
            finally:
                self._run_in_progress = False

        t = threading.Thread(target=_run, daemon=True, name=f"on-demand-{monitor_name or 'all'}")
        t.start()
        self._on_demand_threads.append(t)
        return True


def get_run_history(limit=50):
    """Get recent run history entries."""
    history = _load_history()
    return list(reversed(history[-limit:]))


def get_total_run_count():
    """Get total number of run history entries."""
    history = _load_history()
    return len(history)
