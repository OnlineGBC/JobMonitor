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


def get_sleep_interval(et_now: datetime) -> int:
    """
    Get the sleep interval in seconds based on time of day.

    - Business hours: 10-15 minutes (600-900 seconds)
    - Off-hours: 115-125 minutes (6900-7500 seconds)
    """
    if is_business_hours(et_now):
        return random.randint(600, 900)
    else:
        return random.randint(6900, 7500)


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

                # Wait using the same randomized schedule
                et_now = get_eastern_time()
                if custom_interval_minutes:
                    sleep_seconds = custom_interval_minutes * 60
                else:
                    sleep_seconds = get_sleep_interval(et_now)
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
