#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Background scheduler for running monitors on a schedule.
Runs in a daemon thread so it dies when the Flask app exits.
"""

import json
import logging
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from run_monitor import get_eastern_time, get_sleep_interval, is_business_hours, EXIT_CODE_DESCRIPTIONS

# Cap run history at this many entries
MAX_HISTORY = 500
HISTORY_PATH = Path("data/run_history.json")


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

    @property
    def running(self):
        return self._running and self._thread is not None and self._thread.is_alive()

    def get_status(self):
        """Return scheduler status as a dict."""
        now = time.time()
        next_run_seconds = 0
        if self._next_run_time and self._running:
            next_run_seconds = max(0, int(self._next_run_time - now))
        return {
            "running": self.running,
            "current_monitor": self._current_monitor,
            "last_run": self._last_run,
            "next_run_seconds": next_run_seconds,
            "run_in_progress": self._run_in_progress or self._current_monitor is not None,
        }

    def start(self):
        """Start the scheduler loop."""
        if self.running:
            return False
        self._stop_event.clear()
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="monitor-scheduler")
        self._thread.start()
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
        """Sleep in 1-second increments so we can respond to stop signals."""
        end_time = time.time() + seconds
        self._next_run_time = end_time
        while time.time() < end_time:
            if self._stop_event.is_set():
                return False
            time.sleep(1)
        return True

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

    def _loop(self):
        """Main scheduler loop - runs all monitors, then sleeps."""
        logging.info("Scheduler loop started")

        # Load monitor names from config
        import yaml
        config_path = Path("monitors.yaml")

        while not self._stop_event.is_set():
            # Reload monitors each cycle
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    cfg = yaml.safe_load(f) or {}
                monitor_names = [m["name"] for m in cfg.get("monitors", []) if "name" in m]
            except Exception as e:
                logging.error(f"Scheduler: cannot read monitors.yaml: {e}")
                self._sleep_interruptible(60)
                continue

            if not monitor_names:
                logging.warning("Scheduler: no monitors configured")
                self._sleep_interruptible(60)
                continue

            self._run_in_progress = True

            # Run each monitor
            for name in monitor_names:
                if self._stop_event.is_set():
                    break

                start_time = time.time()
                exit_code = self._run_single_monitor(name)
                duration = time.time() - start_time
                self._record_run(name, exit_code, duration)

                et_now = get_eastern_time()
                self._last_run = et_now.strftime("%Y-%m-%d %H:%M:%S ET")

                # Handle login failure with retry
                if exit_code == 10:
                    logging.warning(f"Login failed for {name} - retrying once after delay")
                    if not self._sleep_interruptible(600):  # 10 min
                        break
                    start_time = time.time()
                    exit_code = self._run_single_monitor(name)
                    duration = time.time() - start_time
                    self._record_run(name, exit_code, duration)
                    if exit_code != 0:
                        logging.error(f"Retry also failed for {name} (exit {exit_code})")

            self._run_in_progress = False

            if self._stop_event.is_set():
                break

            # Sleep until next cycle
            et_now = get_eastern_time()
            sleep_seconds = get_sleep_interval(et_now)
            logging.info(
                f"Scheduler sleeping for {sleep_seconds}s "
                f"({sleep_seconds / 60:.1f} min) "
                f"[{'business hours' if is_business_hours(et_now) else 'off-hours'}]"
            )
            if not self._sleep_interruptible(sleep_seconds):
                break

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
                    names = [m["name"] for m in cfg.get("monitors", []) if "name" in m]

                for name in names:
                    start_time = time.time()
                    exit_code = self._run_single_monitor(name)
                    duration = time.time() - start_time
                    self._record_run(name, exit_code, duration)
                    et_now = get_eastern_time()
                    self._last_run = et_now.strftime("%Y-%m-%d %H:%M:%S ET")
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
