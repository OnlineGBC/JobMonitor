#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
=============================================================================
LinkedIn Job Monitor - Background Loop Runner
=============================================================================

WHAT THIS SCRIPT DOES:
    Runs monitor.py in a loop to continuously check for new LinkedIn job
    postings. It handles timing, error detection, and email alerts.

HOW IT RUNS:
    - Runs monitor.py repeatedly
    - Waits a random amount of time between runs to avoid detection
    - Checks more frequently during business hours, less frequently at night

TIMING:
    - Weekdays (Mon-Fri) 8 AM to 8 PM Eastern: runs every 10-15 minutes
    - All other times (nights and weekends): runs every 115-125 minutes

STOPPING CONDITIONS:
    - If monitor.py exits with any non-zero exit code, the loop stops
    - An email alert is sent with a description of the error

EXIT CODES (from monitor.py):
    0  - Success
    1  - Configuration error (cannot read monitors.yaml or no monitors defined)
    2  - Missing required email environment variables
    3  - Missing ANTHROPIC_API_KEY (required for screenshot comparison)
    4  - Screenshot capture failed
    5  - AI comparison call to LLM failed
    10 - LinkedIn login failed

HOW TO USE:
    python run_monitor.py
"""

import os
import sys
import subprocess
import random
import logging
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path

from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Import send_alert from monitor.py
from monitor import send_alert, configure_logging

# Exit code descriptions
EXIT_CODE_DESCRIPTIONS = {
    1: "Configuration error (monitors.yaml)",
    2: "Missing email environment variables",
    3: "Missing ANTHROPIC_API_KEY",
    4: "Screenshot capture failed",
    5: "AI comparison call to LLM failed",
    10: "LinkedIn login failed",
    11: "Network unavailable - run skipped",
    12: "Personal LinkedIn cookie expired - re-paste needed",
}


def get_eastern_time():
    """Get the current time in US Eastern timezone."""
    return datetime.now(ZoneInfo("America/New_York"))


def is_business_hours(et_now: datetime) -> bool:
    """
    Check if we're in business hours (Mon-Fri, 8 AM to 8 PM Eastern).
    """
    is_weekday = et_now.weekday() < 5  # Monday=0, Friday=4
    is_daytime = 8 <= et_now.hour < 20
    return is_weekday and is_daytime


SCHED_DEFAULTS = {
    "SCHED_BUSINESS_MIN": 10,
    "SCHED_BUSINESS_MAX": 15,
    "SCHED_OFFHOURS_MIN": 115,
    "SCHED_OFFHOURS_MAX": 125,
    # Floor for a per-monitor interval. Users choose their own cadence, but not
    # one fast enough to get the LinkedIn account rate limited.
    "SCHED_MIN_INTERVAL": 30,
}


def _read_sched_minutes(key: str) -> int:
    """Read a scheduler interval env var (in minutes) with default fallback."""
    raw = os.getenv(key)
    if raw is None:
        return SCHED_DEFAULTS[key]
    try:
        value = int(raw)
        if value < 1:
            return SCHED_DEFAULTS[key]
        return value
    except (ValueError, TypeError):
        return SCHED_DEFAULTS[key]


def get_scheduler_ranges() -> dict:
    """Return the current business-hours and off-hours interval ranges in minutes."""
    b_min = _read_sched_minutes("SCHED_BUSINESS_MIN")
    b_max = _read_sched_minutes("SCHED_BUSINESS_MAX")
    o_min = _read_sched_minutes("SCHED_OFFHOURS_MIN")
    o_max = _read_sched_minutes("SCHED_OFFHOURS_MAX")
    if b_max < b_min:
        b_max = b_min
    if o_max < o_min:
        o_max = o_min
    return {
        "business_min": b_min,
        "business_max": b_max,
        "offhours_min": o_min,
        "offhours_max": o_max,
    }


def get_min_interval_minutes() -> int:
    """
    The smallest per-monitor interval a user is allowed to choose.

    Never above the default schedule's own fastest setting. SCHED_MIN_INTERVAL
    and SCHED_BUSINESS_MIN both describe how often monitors may run, but nothing
    ties them together, so they can be set to contradict each other - and did:
    a 30 minute floor against a default that runs every 10. That made the app
    advertise "default: every 10-15 min" and then refuse a request for 10.

    Whichever is smaller wins, so the app can never refuse a cadence it is
    already using itself, whatever the two are set to.
    """
    floor = _read_sched_minutes("SCHED_MIN_INTERVAL")
    business_min = _read_sched_minutes("SCHED_BUSINESS_MIN")
    return min(floor, business_min)


def get_sleep_interval(et_now: datetime) -> int:
    """
    Get the sleep interval in seconds based on time of day.

    Ranges come from env vars (SCHED_BUSINESS_MIN/MAX, SCHED_OFFHOURS_MIN/MAX,
    all in minutes) with defaults of 10-15 and 115-125.
    """
    ranges = get_scheduler_ranges()
    if is_business_hours(et_now):
        return random.randint(ranges["business_min"] * 60, ranges["business_max"] * 60)
    else:
        return random.randint(ranges["offhours_min"] * 60, ranges["offhours_max"] * 60)


def build_email_config() -> dict:
    """Build email configuration from environment variables."""
    return {
        "smtp_host": os.getenv("SMTP_HOST"),
        "smtp_port": os.getenv("SMTP_PORT"),
        "smtp_username": os.getenv("SMTP_USERNAME"),
        "smtp_password": os.getenv("SMTP_PASSWORD"),
        "smtp_use_tls": os.getenv("SMTP_USE_TLS", "1"),
        "from_addr": os.getenv("FROM_ADDR"),
        "to_addrs": os.getenv("TO_ADDRS"),
    }


def send_error_alert(exit_code: int, description: str):
    """Send an email alert when monitor.py fails."""
    email_cfg = build_email_config()

    # Check if email config is available
    if not email_cfg["smtp_host"] or not email_cfg["from_addr"]:
        logging.warning("Cannot send email alert - missing SMTP environment variables")
        return

    et_now = get_eastern_time()
    subject_prefix = os.getenv("SUBJECT_PREFIX", "[JobMonitor]")

    subject = f"{subject_prefix} STOPPED: {description} (exit code {exit_code})"
    body = f"""The LinkedIn Job Monitor has stopped due to an error.

Exit Code: {exit_code}
Error: {description}
Timestamp: {et_now.strftime('%Y-%m-%d %H:%M:%S')} ET

To restart the monitor:
    python run_monitor.py
"""

    send_alert(email_cfg, subject, body)


def run_monitor_loop(custom_interval_minutes=None):
    """
    Main loop that runs monitor.py repeatedly.

    Args:
        custom_interval_minutes: If provided, use this fixed interval (in minutes)
                                 instead of the business hours schedule.
    """
    import time

    logging.info("=" * 60)
    logging.info("Job Monitor Loop Started")
    if custom_interval_minutes:
        logging.info(f"Custom schedule: every {custom_interval_minutes} minutes")
    logging.info("=" * 60)

    # Get the path to monitor.py (same directory as this script)
    script_dir = Path(__file__).parent
    monitor_script = script_dir / "monitor.py"
    python_exe = sys.executable

    while True:
        # Run monitor.py
        logging.info("Running monitor.py...")
        result = subprocess.run(
            [python_exe, str(monitor_script)],
            cwd=str(script_dir)
        )
        exit_code = result.returncode

        # Check for errors
        if exit_code != 0:
            description = EXIT_CODE_DESCRIPTIONS.get(exit_code, "Unknown error")

            # Special handling for login failures (exit code 10) - retry once
            if exit_code == 10:
                logging.warning(f"Login failed (exit code 10) - will retry once after delay")

                # Always wait 10-15 minutes for login retry (regardless of business hours)
                # This is shorter than off-hours polling because LinkedIn issues are usually transient
                et_now = get_eastern_time()
                sleep_seconds = random.randint(600, 900)  # 10-15 minutes
                sleep_minutes = sleep_seconds / 60

                logging.info(
                    f"[{et_now.strftime('%Y-%m-%d %H:%M:%S')} ET] "
                    f"Waiting {sleep_seconds} seconds ({sleep_minutes:.1f} minutes) before retry..."
                )

                try:
                    time.sleep(sleep_seconds)
                except KeyboardInterrupt:
                    logging.info("Received keyboard interrupt - stopping loop")
                    break

                # Retry once
                logging.info("Retrying monitor.py after login failure...")
                result = subprocess.run(
                    [python_exe, str(monitor_script)],
                    cwd=str(script_dir)
                )
                exit_code = result.returncode

                if exit_code == 0:
                    logging.info("Retry successful - continuing normal operation")
                    # Continue to normal sleep and next iteration
                else:
                    # Retry also failed - now stop
                    description = EXIT_CODE_DESCRIPTIONS.get(exit_code, "Unknown error")
                    logging.error(f"Retry also failed with exit code {exit_code} ({description}) - stopping loop")
                    send_error_alert(exit_code, f"{description} (after retry)")
                    break
            else:
                # Non-login errors stop immediately
                logging.error(f"Monitor failed with exit code {exit_code} ({description}) - stopping loop")
                send_error_alert(exit_code, description)
                break

        # Calculate sleep interval
        et_now = get_eastern_time()
        if custom_interval_minutes:
            sleep_seconds = custom_interval_minutes * 60
        else:
            sleep_seconds = get_sleep_interval(et_now)
        sleep_minutes = sleep_seconds / 60

        logging.info(
            f"[{et_now.strftime('%Y-%m-%d %H:%M:%S')} ET] "
            f"Sleeping for {sleep_seconds} seconds ({sleep_minutes:.1f} minutes)"
        )

        try:
            time.sleep(sleep_seconds)
        except KeyboardInterrupt:
            logging.info("Received keyboard interrupt - stopping loop")
            break

    logging.info("Job Monitor Loop Finished")
    logging.info("=" * 60)


def main():
    configure_logging()
    run_monitor_loop()


if __name__ == "__main__":
    main()
