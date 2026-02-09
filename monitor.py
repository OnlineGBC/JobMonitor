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

CLI OPTIONS:
    --dry-run       Run without sending notifications (emails/webhooks)
    --force-refresh Clear baseline screenshots and start fresh
    --monitor NAME  Only process the specified monitor (by name)

EXIT CODES:
    0  = Success
    1  = Cannot read config file
    2  = Missing email environment variables
    3  = Missing ANTHROPIC_API_KEY
    4  = Screenshot capture failed
    5  = AI comparison call to LLM failed
    10 = LinkedIn login failed (triggers job stop in run_monitor_job.ps1)

FILES CREATED:
    - snapshots/<name>_screenshot1.png: The baseline screenshot for comparison
    - snapshots/<name>_screenshot1.txt: Page text for baseline (for deterministic checks)
    - snapshots/<name>_screenshot2.png: Temporary screenshot (deleted after comparison)
    - snapshots/<name>_screenshot2.txt: Temporary page text (deleted after comparison)
    - logs/screen_compare.log: Log file (with rotation, max 5MB x 3 backups)
    - snapshots/<name>_linkedin_state.json: Saved LinkedIn session cookies
"""

import os
import sys
import json
import time
import base64
import smtplib
import logging
import argparse
import traceback
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.image import MIMEImage
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Dict, Any, Optional, Callable
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

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

try:
    from PIL import Image
    import imagehash
    HAS_IMAGEHASH = True
except Exception:
    HAS_IMAGEHASH = False


# =============================================================================
# Configuration
# =============================================================================

NO_MATCHING_JOBS_TEXT = "No matching jobs found"

# Retry configuration
MAX_RETRIES = 3
RETRY_BASE_DELAY = 2  # seconds, doubles each retry (exponential backoff)

# Perceptual hash threshold - images with hash difference <= this are considered identical
PHASH_THRESHOLD = 10


def get_snapshot_paths(monitor_name: str) -> tuple:
    """
    Get snapshot paths for a specific monitor.
    Returns (screenshot1_path, screenshot2_path, text1_path, text2_path, state_path).
    """
    # Sanitize monitor name for use in filenames
    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in monitor_name)
    return (
        Path(f"snapshots/{safe_name}_screenshot1.png"),
        Path(f"snapshots/{safe_name}_screenshot2.png"),
        Path(f"snapshots/{safe_name}_screenshot1.txt"),
        Path(f"snapshots/{safe_name}_screenshot2.txt"),
        Path(f"snapshots/{safe_name}_linkedin_state.json"),
    )


# =============================================================================
# Utility Functions
# =============================================================================

def ensure_dirs():
    """Create required directories if they don't exist."""
    Path("snapshots").mkdir(exist_ok=True)
    Path("logs").mkdir(exist_ok=True)


def configure_logging():
    """Set up logging to both file and console with log rotation."""
    ensure_dirs()
    log_path = Path("logs/screen_compare.log")

    # Create logger
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)

    # Clear any existing handlers
    logger.handlers.clear()

    # File handler with rotation (5MB max, keep 3 backups)
    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=5 * 1024 * 1024,  # 5 MB
        backupCount=3,
        encoding="utf-8"
    )
    file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(file_handler)

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(console_handler)


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


def retry_with_backoff(
    func: Callable,
    max_retries: int = MAX_RETRIES,
    base_delay: float = RETRY_BASE_DELAY,
    operation_name: str = "operation"
):
    """
    Execute a function with exponential backoff retry logic.

    Args:
        func: Function to execute (should raise exception on failure)
        max_retries: Maximum number of retry attempts
        base_delay: Initial delay between retries (doubles each attempt)
        operation_name: Name for logging purposes

    Returns:
        The return value of func() if successful

    Raises:
        The last exception if all retries fail
    """
    last_exception = None
    for attempt in range(max_retries + 1):
        try:
            return func()
        except Exception as e:
            last_exception = e
            if attempt < max_retries:
                delay = base_delay * (2 ** attempt)
                logging.warning(
                    f"{operation_name} failed (attempt {attempt + 1}/{max_retries + 1}): {e}. "
                    f"Retrying in {delay}s..."
                )
                time.sleep(delay)
            else:
                logging.error(f"{operation_name} failed after {max_retries + 1} attempts: {e}")
    raise last_exception


def images_are_identical_phash(img1_path: Path, img2_path: Path, threshold: int = PHASH_THRESHOLD) -> Optional[bool]:
    """
    Compare two images using perceptual hashing.

    This is a fast pre-check before calling the expensive AI comparison.
    Returns True if images are perceptually identical (hash difference <= threshold).
    Returns False if images are clearly different.
    Returns None if imagehash is not available or comparison fails.
    """
    if not HAS_IMAGEHASH:
        return None

    try:
        hash1 = imagehash.phash(Image.open(img1_path))
        hash2 = imagehash.phash(Image.open(img2_path))
        difference = hash1 - hash2
        logging.info(f"Perceptual hash difference: {difference} (threshold: {threshold})")

        if difference <= threshold:
            logging.info("Images are perceptually identical (skipping AI comparison)")
            return True
        return False
    except Exception as e:
        logging.warning(f"Perceptual hash comparison failed: {e}")
        return None


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
    timeout: int = 180,
    storage_state_path: Optional[Path] = None
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

    if storage_state_path is None:
        storage_state_path = Path("linkedin_state.json")
    storage_state = None
    has_saved_session = False

    # Check if we have saved LinkedIn cookies from a previous session
    if storage_state_path.exists():
        storage_state = str(storage_state_path)
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
                # Clear old cookies before re-login to avoid LinkedIn showing a different page
                if has_saved_session:
                    logging.info("Clearing expired cookies before re-login")
                    context.clear_cookies()
                login_success = login_to_linkedin(page, linkedin_username, linkedin_password)
                if login_success:
                    is_authenticated = True
                    # Save the session cookies for next time
                    try:
                        context.storage_state(path=str(storage_state_path))
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
                    context.storage_state(path=str(storage_state_path))
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

def resize_image_for_api(img_path: Path, max_dimension: int = 7500) -> bytes:
    """
    Resize an image if it exceeds max dimensions (Claude API limit is 8000px).
    Returns the image data as bytes (PNG format).
    """
    try:
        from PIL import Image
        import io

        with Image.open(img_path) as img:
            width, height = img.size

            # Check if resizing is needed
            if width <= max_dimension and height <= max_dimension:
                # No resize needed, return original
                with open(img_path, "rb") as f:
                    return f.read()

            # Calculate new dimensions maintaining aspect ratio
            if width > height:
                new_width = max_dimension
                new_height = int(height * (max_dimension / width))
            else:
                new_height = max_dimension
                new_width = int(width * (max_dimension / height))

            logging.info(f"Resizing image from {width}x{height} to {new_width}x{new_height}")

            # Resize using high-quality resampling
            resized = img.resize((new_width, new_height), Image.Resampling.LANCZOS)

            # Save to bytes
            buffer = io.BytesIO()
            resized.save(buffer, format="PNG", optimize=True)
            return buffer.getvalue()

    except ImportError:
        logging.warning("PIL not available, returning original image (may fail API size limit)")
        with open(img_path, "rb") as f:
            return f.read()


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

    # Read and resize images if needed (Claude API has 8000px limit)
    img1_bytes = resize_image_for_api(img1_path)
    img2_bytes = resize_image_for_api(img2_path)

    img1_data = base64.standard_b64encode(img1_bytes).decode("utf-8")
    img2_data = base64.standard_b64encode(img2_bytes).decode("utf-8")

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

    # Use exact match to avoid false positives like "NOT THE SAME"
    return response_text == "SAME"


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
    Send an email alert with retry logic. Returns True if successful, False otherwise.
    """
    def _send():
        # Deduplicate email addresses while preserving order
        to_addrs_list = [addr.strip() for addr in email_cfg["to_addrs"].split(",") if addr.strip()]
        to_addrs_unique = list(dict.fromkeys(to_addrs_list))
        send_email_with_image(
            smtp_host=email_cfg["smtp_host"],
            smtp_port=int(email_cfg["smtp_port"]),
            smtp_username=email_cfg["smtp_username"],
            smtp_password=email_cfg["smtp_password"],
            # use_tls=True means STARTTLS (explicit TLS), False means SMTP_SSL (implicit TLS)
            use_tls=bool(int(email_cfg.get("smtp_use_tls", "1"))),
            from_addr=email_cfg["from_addr"],
            to_addrs=to_addrs_unique,
            subject=subject,
            body_text=body,
            image_path=image_path
        )

    try:
        retry_with_backoff(_send, operation_name="Email send")
        return True
    except Exception as e:
        logging.error(f"Error sending email after retries: {e}")
        logging.debug(traceback.format_exc())
        return False


# =============================================================================
# Webhook Notifications (Slack/Discord)
# =============================================================================

def send_slack_webhook(webhook_url: str, subject: str, body: str, image_path: Optional[Path] = None) -> bool:
    """
    Send a notification to Slack via webhook.

    Args:
        webhook_url: Slack incoming webhook URL
        subject: Message title
        body: Message body text
        image_path: Optional path to image (not directly supported, will add as note)

    Returns:
        True if successful, False otherwise
    """
    def _send():
        # Build Slack message payload
        blocks = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": subject[:150], "emoji": True}
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": body[:3000]}
            }
        ]

        if image_path and image_path.exists():
            blocks.append({
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": f"📎 Screenshot attached: `{image_path.name}`"}]
            })

        payload = {"blocks": blocks, "text": subject}
        data = json.dumps(payload).encode("utf-8")

        req = Request(webhook_url, data=data, headers={"Content-Type": "application/json"})
        with urlopen(req, timeout=30) as response:
            if response.status != 200:
                raise RuntimeError(f"Slack webhook returned status {response.status}")

    try:
        retry_with_backoff(_send, operation_name="Slack webhook")
        logging.info("Slack notification sent successfully")
        return True
    except Exception as e:
        logging.error(f"Error sending Slack notification: {e}")
        logging.debug(traceback.format_exc())
        return False


def send_discord_webhook(webhook_url: str, subject: str, body: str, image_path: Optional[Path] = None) -> bool:
    """
    Send a notification to Discord via webhook.

    Args:
        webhook_url: Discord webhook URL
        subject: Message title (used as embed title)
        body: Message body text
        image_path: Optional path to image (not directly supported, will add as note)

    Returns:
        True if successful, False otherwise
    """
    def _send():
        # Build Discord embed payload
        embed = {
            "title": subject[:256],
            "description": body[:4096],
            "color": 0x0077B5,  # LinkedIn blue
            "timestamp": datetime.utcnow().isoformat() + "Z"
        }

        if image_path and image_path.exists():
            embed["footer"] = {"text": f"Screenshot: {image_path.name}"}

        payload = {"embeds": [embed]}
        data = json.dumps(payload).encode("utf-8")

        req = Request(webhook_url, data=data, headers={"Content-Type": "application/json"})
        with urlopen(req, timeout=30) as response:
            if response.status not in (200, 204):
                raise RuntimeError(f"Discord webhook returned status {response.status}")

    try:
        retry_with_backoff(_send, operation_name="Discord webhook")
        logging.info("Discord notification sent successfully")
        return True
    except Exception as e:
        logging.error(f"Error sending Discord notification: {e}")
        logging.debug(traceback.format_exc())
        return False


def send_notifications(
    email_cfg: Dict[str, Any],
    subject: str,
    body: str,
    image_path: Optional[Path] = None,
    dry_run: bool = False
) -> bool:
    """
    Send notifications via all configured channels (email, Slack, Discord).

    Returns True if at least one notification was sent successfully.
    """
    if dry_run:
        logging.info(f"[DRY RUN] Would send notification: {subject}")
        logging.info(f"[DRY RUN] Body: {body[:200]}...")
        return True

    success = False

    # Send email
    if email_cfg.get("to_addrs"):
        if send_alert(email_cfg, subject, body, image_path):
            success = True

    # Send Slack webhook
    slack_url = os.getenv("SLACK_WEBHOOK_URL")
    if slack_url:
        if send_slack_webhook(slack_url, subject, body, image_path):
            success = True

    # Send Discord webhook
    discord_url = os.getenv("DISCORD_WEBHOOK_URL")
    if discord_url:
        if send_discord_webhook(discord_url, subject, body, image_path):
            success = True

    return success


# =============================================================================
# Main Program
# =============================================================================

def process_monitor(
    monitor: Dict[str, Any],
    defaults: Dict[str, Any],
    email_cfg: Dict[str, Any],
    subject_prefix: str,
    linkedin_username: Optional[str],
    linkedin_password: Optional[str],
    dry_run: bool = False
) -> int:
    """
    Process a single monitor. Returns exit code (0 for success, non-zero for failure).

    Args:
        monitor: Monitor configuration dict
        defaults: Default settings from config
        email_cfg: Email configuration
        subject_prefix: Prefix for email subjects
        linkedin_username: LinkedIn username
        linkedin_password: LinkedIn password
        dry_run: If True, skip sending notifications

    Exit codes:
        0  = Success
        1  = Missing required config
        3  = Missing ANTHROPIC_API_KEY
        4  = Screenshot capture failed
        5  = AI comparison failed
        10 = LinkedIn login failed
    """
    name = monitor.get("name", "Monitor")
    if "url" not in monitor:
        logging.error(f"[{name}] Monitor is missing required 'url' field")
        return 1
    url = monitor["url"]
    headless = bool(monitor.get("headless", defaults.get("headless", False)))

    if dry_run:
        logging.info(f"[{name}] DRY RUN MODE - notifications will be skipped")

    # Get monitor-specific snapshot paths
    screenshot1_path, screenshot2_path, text1_path, text2_path, state_path = get_snapshot_paths(name)

    logging.info("-" * 60)
    logging.info(f"Processing monitor: {name}")
    logging.info(f"URL: {url[:80]}{'...' if len(url) > 80 else ''}")

    # Check for Anthropic API key (only needed if baseline exists)
    if screenshot1_path.exists() and not os.getenv("ANTHROPIC_API_KEY"):
        logging.error(f"[{name}] ANTHROPIC_API_KEY not set (required for screenshot comparison)")
        return 3

    # -------------------------------------------------------------------------
    # Monitor Logic
    # -------------------------------------------------------------------------

    if not screenshot1_path.exists():
        # FIRST RUN: No previous screenshot exists
        logging.info(f"[{name}] No existing screenshot found. Capturing initial screenshot...")

        success, login_failed = capture_screenshot(
            url=url,
            output_path=screenshot1_path,
            linkedin_username=linkedin_username,
            linkedin_password=linkedin_password,
            headless=headless,
            storage_state_path=state_path
        )

        if login_failed:
            logging.error(f"[{name}] LinkedIn login failed - sending alert")
            body = (
                f"LinkedIn login failed for '{name}'!\n\n"
                f"URL: {url}\n"
                f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                f"The monitor job has been stopped.\n"
                f"Please check your LinkedIn credentials or session."
            )
            send_notifications(email_cfg, f"{subject_prefix} {name}: LOGIN FAILED - JOB STOPPED", body, dry_run=dry_run)
            return 10

        if not success:
            logging.error(f"[{name}] Failed to capture initial screenshot")
            return 4

        logging.info(f"[{name}] Initial screenshot captured (baseline). No notification on first run.")

    else:
        # SUBSEQUENT RUN: A previous screenshot exists
        logging.info(f"[{name}] Existing screenshot found. Capturing new screenshot for comparison...")

        success, login_failed = capture_screenshot(
            url=url,
            output_path=screenshot2_path,
            linkedin_username=linkedin_username,
            linkedin_password=linkedin_password,
            headless=headless,
            storage_state_path=state_path
        )

        if login_failed:
            logging.error(f"[{name}] LinkedIn login failed - sending alert")
            body = (
                f"LinkedIn login failed for '{name}'!\n\n"
                f"URL: {url}\n"
                f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                f"The monitor job has been stopped.\n"
                f"Please check your LinkedIn credentials or session."
            )
            send_notifications(email_cfg, f"{subject_prefix} {name}: LOGIN FAILED - JOB STOPPED", body, dry_run=dry_run)
            return 10

        if not success:
            logging.error(f"[{name}] Failed to capture comparison screenshot")
            return 4

        # Check for "No matching jobs found" in screenshot2 (deterministic check)
        if has_no_matching_jobs(text2_path):
            logging.info(f"[{name}] No matching jobs found. Skipping email notification.")
            # Clean up screenshot2 files
            if screenshot2_path.exists():
                screenshot2_path.unlink()
            if text2_path.exists():
                text2_path.unlink()
            # Note if screenshot1 also had no matching jobs
            if has_no_matching_jobs(text1_path):
                logging.info(f"[{name}] Baseline also had no matching jobs.")
        else:
            # Check if screenshot1 had "No matching jobs found" but now there are jobs
            baseline_had_no_jobs = has_no_matching_jobs(text1_path)
            if baseline_had_no_jobs:
                logging.info(f"[{name}] New jobs appeared! Baseline had no matching jobs - skipping AI comparison.")
                is_same = False  # Definitely different - jobs appeared
            else:
                # Try perceptual hash comparison first (fast pre-check)
                phash_result = images_are_identical_phash(screenshot1_path, screenshot2_path)
                if phash_result is True:
                    # Images are perceptually identical, skip expensive AI call
                    is_same = True
                else:
                    # Use AI to compare the two screenshots (with retry logic)
                    try:
                        def _ai_compare():
                            return compare_screenshots_with_ai(screenshot1_path, screenshot2_path)
                        is_same = retry_with_backoff(_ai_compare, operation_name="AI comparison")
                    except Exception as e:
                        logging.error(f"[{name}] AI comparison call to LLM failed after retries: {e}")
                        logging.debug(traceback.format_exc())
                        if screenshot2_path.exists():
                            screenshot2_path.unlink()
                        if text2_path.exists():
                            text2_path.unlink()
                        return 5

            if is_same:
                # No change detected - discard the new screenshot
                logging.info(f"[{name}] No meaningful change detected. Discarding new screenshot.")
                screenshot2_path.unlink()
                if text2_path.exists():
                    text2_path.unlink()
            else:
                # Change detected - send alert and update the baseline
                logging.info(f"[{name}] CHANGE DETECTED! Sending notification...")

                body = (
                    f"Change detected for '{name}'!\n\n"
                    f"URL: {url}\n"
                    f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                    f"The job listings have changed since the last check.\n"
                    f"See attached screenshot for current state."
                )

                notification_sent = send_notifications(
                    email_cfg, f"{subject_prefix} {name}: CHANGE DETECTED", body, screenshot2_path, dry_run=dry_run
                )
                if notification_sent:
                    logging.info(f"[{name}] Change alert notification sent successfully")
                else:
                    logging.warning(f"[{name}] Failed to send change alert notification")

                # Only rotate screenshots if notification was sent successfully (or in dry-run mode)
                # Otherwise keep the old baseline so we can retry notification next time
                if notification_sent:
                    # Replace the old screenshot with the new one (atomic-safe approach)
                    logging.info(f"[{name}] Rotating screenshots...")
                    temp_path = screenshot1_path.with_suffix('.old.png')
                    screenshot1_path.rename(temp_path)
                    screenshot2_path.rename(screenshot1_path)
                    temp_path.unlink()
                    # Also rotate the text files
                    if text1_path.exists():
                        temp_text = text1_path.with_suffix('.old.txt')
                        text1_path.rename(temp_text)
                        if text2_path.exists():
                            text2_path.rename(text1_path)
                        temp_text.unlink()
                    elif text2_path.exists():
                        text2_path.rename(text1_path)
                    logging.info(f"[{name}] Screenshot rotation complete")
                else:
                    # Clean up screenshot2 but keep screenshot1 as baseline
                    logging.info(f"[{name}] Keeping old baseline for retry (notification failed)")
                    if screenshot2_path.exists():
                        screenshot2_path.unlink()
                    if text2_path.exists():
                        text2_path.unlink()

    logging.info(f"[{name}] Monitor processing complete")
    return 0


def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="LinkedIn Job Monitor - Screenshot-based change detection",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python monitor.py                    # Normal run
  python monitor.py --dry-run          # Test without sending notifications
  python monitor.py --force-refresh    # Clear baselines and start fresh
  python monitor.py --monitor "MyJob"  # Only process monitor named "MyJob"
        """
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run without sending notifications (emails/webhooks)"
    )
    parser.add_argument(
        "--force-refresh",
        action="store_true",
        help="Clear baseline screenshots and start fresh"
    )
    parser.add_argument(
        "--monitor",
        type=str,
        metavar="NAME",
        help="Only process the specified monitor (by name)"
    )
    return parser.parse_args()


def clear_baseline(monitor_name: str) -> None:
    """Clear baseline files for a specific monitor."""
    screenshot1, screenshot2, text1, text2, state = get_snapshot_paths(monitor_name)
    for path in [screenshot1, screenshot2, text1, text2]:
        if path.exists():
            path.unlink()
            logging.info(f"Deleted: {path}")


def main():
    # Parse command line arguments first
    args = parse_args()

    configure_logging()
    load_dotenv()
    ensure_dirs()

    logging.info("=" * 60)
    logging.info("Screen Compare Monitor Started")
    if args.dry_run:
        logging.info("*** DRY RUN MODE - No notifications will be sent ***")
    if args.force_refresh:
        logging.info("*** FORCE REFRESH - Baselines will be cleared ***")
    logging.info("=" * 60)

    # Load the monitor configuration
    config_path = os.getenv("CONFIG_PATH", "monitors.yaml")
    try:
        cfg = load_yaml(config_path) or {}
    except Exception as e:
        logging.error(f"Cannot read {config_path}: {e}")
        sys.exit(1)

    monitors = cfg.get("monitors", [])
    if not monitors:
        logging.error("No monitors defined in monitors.yaml")
        sys.exit(1)

    # Filter monitors if --monitor flag is specified
    if args.monitor:
        monitors = [m for m in monitors if m.get("name") == args.monitor]
        if not monitors:
            logging.error(f"No monitor found with name: {args.monitor}")
            sys.exit(1)

    defaults = cfg.get("defaults", {})
    logging.info(f"Found {len(monitors)} monitor(s) to process")

    # Check for notification configuration
    # Email is optional if webhooks are configured
    has_email = all(os.getenv(k) for k in ["SMTP_HOST", "SMTP_PORT", "SMTP_USERNAME", "SMTP_PASSWORD", "FROM_ADDR", "TO_ADDRS"])
    has_slack = bool(os.getenv("SLACK_WEBHOOK_URL"))
    has_discord = bool(os.getenv("DISCORD_WEBHOOK_URL"))

    if not has_email and not has_slack and not has_discord and not args.dry_run:
        logging.error("No notification channels configured. Set email env vars or SLACK_WEBHOOK_URL or DISCORD_WEBHOOK_URL")
        sys.exit(2)

    email_cfg = {
        "smtp_host": os.getenv("SMTP_HOST"),
        "smtp_port": os.getenv("SMTP_PORT"),
        "smtp_username": os.getenv("SMTP_USERNAME"),
        "smtp_password": os.getenv("SMTP_PASSWORD"),
        "smtp_use_tls": os.getenv("SMTP_USE_TLS", "1"),
        "from_addr": os.getenv("FROM_ADDR"),
        "to_addrs": os.getenv("TO_ADDRS", ""),
    }

    subject_prefix = os.getenv("SUBJECT_PREFIX", "[ScreenCompare]")

    # Load LinkedIn credentials
    linkedin_username = os.getenv("LINKEDIN_USERNAME")
    linkedin_password = os.getenv("LINKEDIN_PASSWORD")

    # Log configured notification channels
    channels = []
    if has_email:
        channels.append("Email")
    if has_slack:
        channels.append("Slack")
    if has_discord:
        channels.append("Discord")
    if channels:
        logging.info(f"Notification channels: {', '.join(channels)}")

    # -------------------------------------------------------------------------
    # Handle force refresh
    # -------------------------------------------------------------------------

    if args.force_refresh:
        for monitor in monitors:
            name = monitor.get("name", "Monitor")
            logging.info(f"Clearing baseline for: {name}")
            clear_baseline(name)

    # -------------------------------------------------------------------------
    # Process all monitors
    # -------------------------------------------------------------------------

    worst_exit_code = 0

    for monitor in monitors:
        exit_code = process_monitor(
            monitor=monitor,
            defaults=defaults,
            email_cfg=email_cfg,
            subject_prefix=subject_prefix,
            linkedin_username=linkedin_username,
            linkedin_password=linkedin_password,
            dry_run=args.dry_run
        )

        # Track the worst exit code encountered
        if exit_code != 0:
            worst_exit_code = exit_code

        # Stop immediately on login failure (affects all monitors)
        if exit_code == 10:
            logging.error("LinkedIn login failed - stopping all monitors")
            break

    logging.info("=" * 60)
    logging.info("Screen Compare Monitor Finished")
    logging.info("=" * 60)

    if worst_exit_code != 0:
        sys.exit(worst_exit_code)


if __name__ == "__main__":
    main()
