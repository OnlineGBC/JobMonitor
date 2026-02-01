#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
=============================================================================
LinkedIn Job Monitor - Screenshot-based Change Detection
=============================================================================

WHAT THIS SCRIPT DOES:
    Monitors a LinkedIn job search page for new job postings by taking
    screenshots and using Claude AI to detect meaningful changes.

HOW IT WORKS:
    1. First run: Takes a screenshot, saves it, and emails it to you
    2. Subsequent runs: Takes a new screenshot and compares it to the previous one
       - If the job listings are the same: discards the new screenshot, does nothing
       - If there are new/removed jobs: emails you the new screenshot and saves it

CONFIGURATION:
    - monitors.yaml: Contains the LinkedIn search URL to monitor
    - .env file: Contains credentials (LinkedIn, SMTP, Anthropic API key)

EXIT CODES:
    0  = Success
    1  = Cannot read config file
    2  = Missing email environment variables
    3  = Missing ANTHROPIC_API_KEY
    4  = Screenshot capture failed
    5  = AI comparison call to LLM failed
    10 = LinkedIn login failed (triggers job stop in run_monitor_job.ps1)

FILES CREATED:
    - snapshots/screenshot1.png: The baseline screenshot for comparison
    - snapshots/screenshot1.txt: Page text for baseline (for deterministic checks)
    - snapshots/screenshot2.png: Temporary screenshot (deleted after comparison)
    - snapshots/screenshot2.txt: Temporary page text (deleted after comparison)
    - logs/screen_compare.log: Log file
    - linkedin_state.json: Saved LinkedIn session cookies (for faster login)
"""

import os
import sys
import base64
import smtplib
import logging
import traceback
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.image import MIMEImage
from pathlib import Path
from typing import Dict, Any, Optional

import yaml
from dotenv import load_dotenv

try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except Exception:
    HAS_PLAYWRIGHT = False

try:
    import anthropic
    HAS_ANTHROPIC = True
except Exception:
    HAS_ANTHROPIC = False


# =============================================================================
# Configuration
# =============================================================================

SCREENSHOT1_PATH = Path("snapshots/screenshot1.png")  # Baseline screenshot
SCREENSHOT2_PATH = Path("snapshots/screenshot2.png")  # Temporary screenshot for comparison
SCREENSHOT1_TEXT_PATH = Path("snapshots/screenshot1.txt")  # Page text for baseline
SCREENSHOT2_TEXT_PATH = Path("snapshots/screenshot2.txt")  # Page text for comparison

NO_MATCHING_JOBS_TEXT = "No matching jobs found"


# =============================================================================
# Utility Functions
# =============================================================================

def ensure_dirs():
    """Create required directories if they don't exist."""
    Path("snapshots").mkdir(exist_ok=True)
    Path("logs").mkdir(exist_ok=True)


def configure_logging():
    """Set up logging to both file and console."""
    ensure_dirs()
    log_path = Path("logs/screen_compare.log")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def load_yaml(path: str) -> Dict[str, Any]:
    """Load and parse a YAML configuration file."""
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def has_no_matching_jobs(text_path: Path) -> bool:
    """Check if the page text file contains 'No matching jobs found'."""
    if not text_path.exists():
        return False
    try:
        with open(text_path, "r", encoding="utf-8") as f:
            content = f.read()
        return NO_MATCHING_JOBS_TEXT in content
    except Exception as e:
        logging.warning(f"Could not read {text_path}: {e}")
        return False


# =============================================================================
# LinkedIn Login
# =============================================================================

def login_to_linkedin(page, username: str, password: str) -> bool:
    """
    Log in to LinkedIn using the provided credentials.

    Returns True if login succeeded, False if it failed.
    """
    try:
        logging.info("Logging in to LinkedIn...")
        page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded", timeout=30000)

        # Wait for and fill in the login form
        page.wait_for_selector('input[name="session_key"]', timeout=10000)
        page.wait_for_selector('input[name="session_password"]', timeout=10000)
        page.fill('input[name="session_key"]', username)
        page.fill('input[name="session_password"]', password)
        page.click('button[type="submit"]')

        # Wait for the page to load after submitting
        # Note: Don't use "networkidle" - LinkedIn keeps making requests forever
        page.wait_for_load_state("load", timeout=30000)
        page.wait_for_timeout(3000)

        current_url = page.url
        logging.info(f"Post-login URL: {current_url}")

        # Check for login error messages
        error_selectors = [
            'div[role="alert"]',
            '.error-for-password',
            '#error-for-password',
            'div[data-test-id="login-error"]'
        ]
        for selector in error_selectors:
            try:
                error_el = page.query_selector(selector)
                if error_el and error_el.is_visible():
                    error_text = error_el.inner_text()
                    logging.warning(f"LinkedIn login error detected: {error_text}")
                    return False
            except Exception:
                pass

        # Check if LinkedIn is asking for verification (2FA, captcha, etc.)
        if "/challenge" in current_url or "checkpoint" in current_url:
            logging.warning("LinkedIn login requires verification/challenge (2FA, captcha, etc.)")
            return False

        # Check if we're still stuck on the login page
        if "/login" in current_url or "/uas/login" in current_url:
            logging.warning("Still on login page - login may have failed")
            return False

        # If we reached the feed or jobs page, login worked
        if any(x in current_url for x in ["/feed", "/mynetwork", "/jobs"]):
            logging.info("LinkedIn login successful")
            return True

        # If we got past the login page, assume success
        logging.info("LinkedIn login appears successful (redirected away from login)")
        return True

    except Exception as e:
        logging.error(f"LinkedIn login failed: {e}")
        logging.debug(traceback.format_exc())
        return False


# =============================================================================
# Screenshot Capture
# =============================================================================

def capture_screenshot(
    url: str,
    output_path: Path,
    linkedin_username: Optional[str] = None,
    linkedin_password: Optional[str] = None,
    headless: bool = False,
    timeout: int = 180
) -> tuple:
    """
    Open a browser, navigate to the URL, and take a screenshot.

    Returns a tuple: (success, login_failed)
        - (True, False) = Screenshot captured successfully
        - (False, True) = LinkedIn login failed
        - (False, False) = Some other error occurred
    """
    if not HAS_PLAYWRIGHT:
        raise RuntimeError("Playwright not installed. Run: pip install playwright && playwright install chromium")

    storage_state_path = "linkedin_state.json"
    storage_state = None
    has_saved_session = False

    # Check if we have saved LinkedIn cookies from a previous session
    if Path(storage_state_path).exists():
        storage_state = storage_state_path
        has_saved_session = True
        logging.info(f"Loading saved LinkedIn session from {storage_state_path}")

    needs_login = "linkedin.com" in url and linkedin_username and linkedin_password
    effective_headless = headless

    # Decide whether to show the browser window
    if needs_login:
        if has_saved_session and headless:
            effective_headless = True
            logging.info("Valid cookies detected - using headless mode")
        elif not has_saved_session:
            effective_headless = False
            logging.info("No cookies available - using visible browser for login")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=effective_headless)
        try:
            context_options = {
                "viewport": {'width': 1920, 'height': 1080},
                "ignore_https_errors": True
            }

            if storage_state:
                context_options["storage_state"] = storage_state

            context = browser.new_context(**context_options)
            page = context.new_page()
            page.set_default_timeout(timeout * 1000)
            page.set_default_navigation_timeout(timeout * 1000)

            session_valid = False
            is_authenticated = False

            # If we have saved cookies, check if they're still valid
            if has_saved_session and needs_login:
                try:
                    page.goto("https://www.linkedin.com/feed", wait_until="domcontentloaded", timeout=10000)
                    page.wait_for_timeout(2000)
                    current_url = page.url
                    if "/login" not in current_url and ("/feed" in current_url or current_url == "https://www.linkedin.com/" or "/mynetwork" in current_url):
                        session_valid = True
                        is_authenticated = True
                        logging.info("Saved LinkedIn session is still valid")
                    else:
                        logging.info("Saved LinkedIn session appears expired, will re-login")
                except Exception as e:
                    logging.warning(f"Could not validate session, will re-login: {e}")

            # Log in if we don't have a valid session
            if needs_login and not session_valid:
                login_success = login_to_linkedin(page, linkedin_username, linkedin_password)
                if login_success:
                    is_authenticated = True
                    # Save the session cookies for next time
                    try:
                        context.storage_state(path=storage_state_path)
                        logging.info(f"Saved LinkedIn session to {storage_state_path}")
                    except Exception as e:
                        logging.warning(f"Could not save storage state: {e}")
                else:
                    logging.error("LinkedIn login failed!")
                    return (False, True)

            # Navigate to the target URL
            logging.info(f"Navigating to {url}")
            page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)

            # For LinkedIn job search pages, wait for the results to load
            if "linkedin.com/jobs/search" in url:
                container_selectors = [
                    "[data-search-results-container='true']",
                    "div.jobs-search__results-list",
                    "ul.jobs-search__results-list",
                    "main.jobs-search-results",
                ]

                for selector in container_selectors:
                    try:
                        page.wait_for_selector(selector, timeout=15000)
                        break
                    except Exception:
                        continue

                # Give the page a moment to finish rendering
                page.wait_for_timeout(3000)

            # Take the screenshot
            logging.info(f"Capturing screenshot to {output_path}")
            page.screenshot(path=str(output_path), full_page=True)

            # Extract and save page text for deterministic checks
            text_output_path = output_path.with_suffix(".txt")
            try:
                page_text = page.inner_text("body")
                with open(text_output_path, "w", encoding="utf-8") as f:
                    f.write(page_text)
                logging.info(f"Page text saved to {text_output_path}")
            except Exception as e:
                logging.warning(f"Could not extract page text: {e}")

            # Update the saved session cookies
            if needs_login and is_authenticated:
                try:
                    context.storage_state(path=storage_state_path)
                except Exception:
                    pass

            logging.info("Screenshot captured successfully")
            return (True, False)

        except Exception as e:
            logging.error(f"Screenshot capture failed: {e}")
            logging.debug(traceback.format_exc())
            return (False, False)
        finally:
            browser.close()


# =============================================================================
# AI-Powered Screenshot Comparison
# =============================================================================

def compare_screenshots_with_ai(img1_path: Path, img2_path: Path) -> bool:
    """
    Use Claude AI to compare two screenshots and detect meaningful changes.

    Returns True if the screenshots show the same job listings (no change).
    Returns False if there are new or removed jobs (change detected).

    The AI ignores minor differences like timestamps, applicant counts, and
    visual styling - it only looks for actual changes to job listings.
    """
    if not HAS_ANTHROPIC:
        raise RuntimeError("Anthropic package not installed. Run: pip install anthropic")

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY environment variable not set")

    logging.info("Comparing screenshots using Claude AI vision...")

    # Read and encode both images as base64
    with open(img1_path, "rb") as f:
        img1_data = base64.standard_b64encode(f.read()).decode("utf-8")

    with open(img2_path, "rb") as f:
        img2_data = base64.standard_b64encode(f.read()).decode("utf-8")

    client = anthropic.Anthropic(api_key=api_key)

    # Ask Claude to compare the screenshots
    message = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1024,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": """Compare these two screenshots of a LinkedIn job search results page.

I need to know if there are any MEANINGFUL differences between them, specifically:
- Are there any NEW job listings in the second image that weren't in the first?
- Are there any job listings that were REMOVED from the first image?
- Has the content of any job listing changed significantly?

Ignore these types of changes (they are NOT meaningful):
- Minor visual differences (colors, fonts, spacing)
- Time stamps ("posted 2 hours ago" vs "posted 3 hours ago")
- Number of applicants or views
- Order/position of the same jobs
- UI elements like buttons, menus, ads

Answer with ONLY one of these two words:
- "SAME" if the job listings appear to be essentially the same (no new or removed jobs)
- "DIFFERENT" if there are new jobs, removed jobs, or significant content changes

First image (previous screenshot):"""
                    },
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": img1_data
                        }
                    },
                    {
                        "type": "text",
                        "text": "Second image (current screenshot):"
                    },
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": img2_data
                        }
                    }
                ]
            }
        ]
    )

    response_text = message.content[0].text.strip().upper()
    logging.info(f"AI comparison result: {response_text}")

    return "SAME" in response_text


# =============================================================================
# Email Functions
# =============================================================================

def send_email_with_image(
    smtp_host: str,
    smtp_port: int,
    smtp_username: str,
    smtp_password: str,
    use_tls: bool,
    from_addr: str,
    to_addrs: list,
    subject: str,
    body_text: str,
    image_path: Optional[Path] = None
):
    """Send an email, optionally with an image attachment."""
    msg = MIMEMultipart()
    msg["From"] = from_addr
    msg["To"] = ", ".join(to_addrs)
    msg["Subject"] = subject
    msg.attach(MIMEText(body_text, "plain", "utf-8"))

    # Attach the image if provided
    if image_path and image_path.exists():
        with open(image_path, "rb") as f:
            img_data = f.read()
        img_attachment = MIMEImage(img_data, name=image_path.name)
        img_attachment.add_header("Content-Disposition", "attachment", filename=image_path.name)
        msg.attach(img_attachment)

    # Connect and send
    if use_tls:
        server = smtplib.SMTP(smtp_host, smtp_port, timeout=30)
        try:
            server.starttls()
            server.login(smtp_username, smtp_password)
            server.sendmail(from_addr, to_addrs, msg.as_string())
        finally:
            server.quit()
    else:
        server = smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=30)
        try:
            server.login(smtp_username, smtp_password)
            server.sendmail(from_addr, to_addrs, msg.as_string())
        finally:
            server.quit()


def send_alert(email_cfg: Dict[str, Any], subject: str, body: str, image_path: Optional[Path] = None) -> bool:
    """
    Send an email alert. Returns True if successful, False otherwise.
    """
    try:
        send_email_with_image(
            smtp_host=email_cfg["smtp_host"],
            smtp_port=int(email_cfg["smtp_port"]),
            smtp_username=email_cfg["smtp_username"],
            smtp_password=email_cfg["smtp_password"],
            use_tls=bool(int(email_cfg.get("smtp_use_tls", "1"))),
            from_addr=email_cfg["from_addr"],
            to_addrs=[addr.strip() for addr in email_cfg["to_addrs"].split(",") if addr.strip()],
            subject=subject,
            body_text=body,
            image_path=image_path
        )
        return True
    except Exception as e:
        logging.error(f"Error sending email: {e}")
        logging.debug(traceback.format_exc())
        return False


# =============================================================================
# Main Program
# =============================================================================

def main():
    configure_logging()
    load_dotenv()
    ensure_dirs()

    logging.info("=" * 60)
    logging.info("Screen Compare Monitor Started")
    logging.info("=" * 60)

    # Load the monitor configuration
    config_path = os.getenv("CONFIG_PATH", "monitors.yaml")
    try:
        cfg = load_yaml(config_path)
    except Exception as e:
        logging.error(f"Cannot read {config_path}: {e}")
        sys.exit(1)

    monitors = cfg.get("monitors", [])
    if not monitors:
        logging.error("No monitors defined in monitors.yaml")
        sys.exit(1)

    # Use the first monitor defined in the config
    monitor = monitors[0]
    url = monitor["url"]
    name = monitor.get("name", "ScreenCompare")
    headless = bool(monitor.get("headless", cfg.get("defaults", {}).get("headless", False)))

    # Load email settings from environment variables
    required_env = ["SMTP_HOST", "SMTP_PORT", "SMTP_USERNAME", "SMTP_PASSWORD", "FROM_ADDR", "TO_ADDRS"]
    missing = [k for k in required_env if not os.getenv(k)]
    if missing:
        logging.error(f"Missing required email environment variables: {missing}")
        sys.exit(2)

    email_cfg = {
        "smtp_host": os.getenv("SMTP_HOST"),
        "smtp_port": os.getenv("SMTP_PORT"),
        "smtp_username": os.getenv("SMTP_USERNAME"),
        "smtp_password": os.getenv("SMTP_PASSWORD"),
        "smtp_use_tls": os.getenv("SMTP_USE_TLS", "1"),
        "from_addr": os.getenv("FROM_ADDR"),
        "to_addrs": os.getenv("TO_ADDRS"),
    }

    subject_prefix = os.getenv("SUBJECT_PREFIX", "[ScreenCompare]")

    # Load LinkedIn credentials
    linkedin_username = os.getenv("LINKEDIN_USERNAME")
    linkedin_password = os.getenv("LINKEDIN_PASSWORD")

    # Check for Anthropic API key (only needed after first run)
    if SCREENSHOT1_PATH.exists() and not os.getenv("ANTHROPIC_API_KEY"):
        logging.error("ANTHROPIC_API_KEY environment variable not set (required for screenshot comparison)")
        sys.exit(3)

    # -------------------------------------------------------------------------
    # Main Logic
    # -------------------------------------------------------------------------

    if not SCREENSHOT1_PATH.exists():
        # FIRST RUN: No previous screenshot exists
        # Take an initial screenshot to use as the baseline
        logging.info("No existing screenshot found. Capturing initial screenshot...")

        success, login_failed = capture_screenshot(
            url=url,
            output_path=SCREENSHOT1_PATH,
            linkedin_username=linkedin_username,
            linkedin_password=linkedin_password,
            headless=headless
        )

        if login_failed:
            logging.error("LinkedIn login failed - sending alert and stopping")
            body = (
                f"LinkedIn login failed for '{name}'!\n\n"
                f"URL: {url}\n"
                f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                f"The monitor job has been stopped.\n"
                f"Please check your LinkedIn credentials or session."
            )
            send_alert(email_cfg, f"{subject_prefix} {name}: LOGIN FAILED - JOB STOPPED", body)
            sys.exit(10)

        if not success:
            logging.error("Failed to capture initial screenshot")
            sys.exit(4)

        # Send email with the initial screenshot
        body = (
            f"Initial screenshot captured for '{name}'.\n\n"
            f"URL: {url}\n"
            f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            f"This is the baseline. Future runs will compare against this screenshot."
        )

        if send_alert(email_cfg, f"{subject_prefix} {name}: Initial Screenshot", body, SCREENSHOT1_PATH):
            logging.info("Initial screenshot email sent successfully")
        else:
            logging.warning("Failed to send initial screenshot email")

    else:
        # SUBSEQUENT RUN: A previous screenshot exists
        # Take a new screenshot and compare it to the previous one
        logging.info("Existing screenshot found. Capturing new screenshot for comparison...")

        success, login_failed = capture_screenshot(
            url=url,
            output_path=SCREENSHOT2_PATH,
            linkedin_username=linkedin_username,
            linkedin_password=linkedin_password,
            headless=headless
        )

        if login_failed:
            logging.error("LinkedIn login failed - sending alert and stopping")
            body = (
                f"LinkedIn login failed for '{name}'!\n\n"
                f"URL: {url}\n"
                f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                f"The monitor job has been stopped.\n"
                f"Please check your LinkedIn credentials or session."
            )
            send_alert(email_cfg, f"{subject_prefix} {name}: LOGIN FAILED - JOB STOPPED", body)
            sys.exit(10)

        if not success:
            logging.error("Failed to capture comparison screenshot")
            sys.exit(4)

        # Check for "No matching jobs found" in screenshot2 (deterministic check)
        if has_no_matching_jobs(SCREENSHOT2_TEXT_PATH):
            logging.info("No matching jobs found. Skipping email notification.")
            # Clean up screenshot2 files
            if SCREENSHOT2_PATH.exists():
                SCREENSHOT2_PATH.unlink()
            if SCREENSHOT2_TEXT_PATH.exists():
                SCREENSHOT2_TEXT_PATH.unlink()
            # Note if screenshot1 also had no matching jobs
            if has_no_matching_jobs(SCREENSHOT1_TEXT_PATH):
                logging.info("Baseline also had no matching jobs.")
        else:
            # Check if screenshot1 had "No matching jobs found" but now there are jobs
            if has_no_matching_jobs(SCREENSHOT1_TEXT_PATH):
                logging.info("New jobs appeared! Baseline had no matching jobs.")

            # Use AI to compare the two screenshots
            try:
                is_same = compare_screenshots_with_ai(SCREENSHOT1_PATH, SCREENSHOT2_PATH)
            except Exception as e:
                logging.error(f"AI comparison call to LLM failed: {e}")
                logging.debug(traceback.format_exc())
                if SCREENSHOT2_PATH.exists():
                    SCREENSHOT2_PATH.unlink()
                if SCREENSHOT2_TEXT_PATH.exists():
                    SCREENSHOT2_TEXT_PATH.unlink()
                sys.exit(5)

            if is_same:
                # No change detected - discard the new screenshot
                logging.info("No meaningful change detected. Discarding new screenshot.")
                SCREENSHOT2_PATH.unlink()
                if SCREENSHOT2_TEXT_PATH.exists():
                    SCREENSHOT2_TEXT_PATH.unlink()
            else:
                # Change detected - send alert and update the baseline
                logging.info("CHANGE DETECTED! Sending alert email...")

                body = (
                    f"Change detected for '{name}'!\n\n"
                    f"URL: {url}\n"
                    f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                    f"The job listings have changed since the last check.\n"
                    f"See attached screenshot for current state."
                )

                if send_alert(email_cfg, f"{subject_prefix} {name}: CHANGE DETECTED", body, SCREENSHOT2_PATH):
                    logging.info("Change alert email sent successfully")
                else:
                    logging.warning("Failed to send change alert email")

                # Replace the old screenshot with the new one
                logging.info("Rotating screenshots...")
                SCREENSHOT1_PATH.unlink()
                SCREENSHOT2_PATH.rename(SCREENSHOT1_PATH)
                # Also rotate the text files
                if SCREENSHOT1_TEXT_PATH.exists():
                    SCREENSHOT1_TEXT_PATH.unlink()
                if SCREENSHOT2_TEXT_PATH.exists():
                    SCREENSHOT2_TEXT_PATH.rename(SCREENSHOT1_TEXT_PATH)
                logging.info("Screenshot rotation complete")

    logging.info("Screen Compare Monitor Finished")
    logging.info("=" * 60)


if __name__ == "__main__":
    main()
