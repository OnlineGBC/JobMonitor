#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Screenshot-based Web Change Monitor (Windows-friendly)
- Captures screenshots of LinkedIn job search pages using Playwright
- Uses Claude AI vision to compare screenshots for meaningful changes
- Sends email with screenshot attachment when changes are detected

Run manually. On first run, captures initial screenshot and emails it.
On subsequent runs, compares with previous screenshot and only alerts on changes.
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

# Optional imports
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


# --------- Configuration ---------

SCREENSHOT1_PATH = Path("snapshots/screenshot1.png")
SCREENSHOT2_PATH = Path("snapshots/screenshot2.png")


# --------- Utilities ---------

def ensure_dirs():
    Path("snapshots").mkdir(exist_ok=True)
    Path("logs").mkdir(exist_ok=True)


def configure_logging():
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
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# --------- LinkedIn Login ---------

def login_to_linkedin(page, username: str, password: str) -> bool:
    """
    Login to LinkedIn with provided credentials.
    Returns True if login successful, False otherwise.
    """
    try:
        logging.info("Logging in to LinkedIn...")
        page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded", timeout=30000)

        page.wait_for_selector('input[name="session_key"]', timeout=10000)
        page.wait_for_selector('input[name="session_password"]', timeout=10000)

        page.fill('input[name="session_key"]', username)
        page.fill('input[name="session_password"]', password)
        page.click('button[type="submit"]')

        # Wait for page to load after login (don't use networkidle - LinkedIn never stops)
        page.wait_for_load_state("load", timeout=30000)
        page.wait_for_timeout(3000)  # Give time for redirect

        current_url = page.url
        logging.info(f"Post-login URL: {current_url}")

        # Check for error messages first
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

        # Check if we hit a challenge/checkpoint
        if "/challenge" in current_url or "checkpoint" in current_url:
            logging.warning("LinkedIn login requires verification/challenge (2FA, captcha, etc.)")
            return False

        # Check if we're still on login page (login failed)
        if "/login" in current_url or "/uas/login" in current_url:
            logging.warning("Still on login page - login may have failed")
            return False

        # If we're on feed, home, or jobs page, login succeeded
        if any(x in current_url for x in ["/feed", "/mynetwork", "/jobs", "linkedin.com/$"]):
            logging.info("LinkedIn login successful")
            return True

        # Default: assume success if we got past login page
        logging.info("LinkedIn login appears successful (redirected away from login)")
        return True

    except Exception as e:
        logging.error(f"LinkedIn login failed: {e}")
        logging.debug(traceback.format_exc())
        return False


# --------- Screenshot Capture ---------

def capture_screenshot(
    url: str,
    output_path: Path,
    linkedin_username: Optional[str] = None,
    linkedin_password: Optional[str] = None,
    headless: bool = False,
    timeout: int = 180
) -> bool:
    """
    Capture a screenshot of the given URL using Playwright.
    Returns True if successful, False otherwise.
    """
    if not HAS_PLAYWRIGHT:
        raise RuntimeError("Playwright not installed. Run: pip install playwright && playwright install chromium")

    storage_state_path = "linkedin_state.json"
    storage_state = None
    has_saved_session = False

    if Path(storage_state_path).exists():
        storage_state = storage_state_path
        has_saved_session = True
        logging.info(f"Loading saved LinkedIn session from {storage_state_path}")

    needs_login = "linkedin.com" in url and linkedin_username and linkedin_password
    effective_headless = headless

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

            # Validate existing session
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

            # Login if needed
            if needs_login and not session_valid:
                login_success = login_to_linkedin(page, linkedin_username, linkedin_password)
                if login_success:
                    is_authenticated = True
                    try:
                        context.storage_state(path=storage_state_path)
                        logging.info(f"Saved LinkedIn session to {storage_state_path}")
                    except Exception as e:
                        logging.warning(f"Could not save storage state: {e}")
                else:
                    logging.warning("LinkedIn login failed, continuing with unauthenticated access")

            # Navigate to target URL
            logging.info(f"Navigating to {url}")
            page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)

            # Wait for job results to load
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
                        logging.debug(f"LinkedIn results container found: {selector}")
                        break
                    except Exception:
                        continue

                # Wait for content to fully render
                page.wait_for_timeout(3000)

            # Capture screenshot
            logging.info(f"Capturing screenshot to {output_path}")
            page.screenshot(path=str(output_path), full_page=True)

            # Refresh session state
            if needs_login and is_authenticated:
                try:
                    context.storage_state(path=storage_state_path)
                except Exception:
                    pass

            logging.info("Screenshot captured successfully")
            return True

        except Exception as e:
            logging.error(f"Screenshot capture failed: {e}")
            logging.debug(traceback.format_exc())
            return False
        finally:
            browser.close()


# --------- AI Comparison ---------

def compare_screenshots_with_ai(img1_path: Path, img2_path: Path) -> bool:
    """
    Use Claude AI vision to compare two screenshots.
    Returns True if they appear to show the same content (no meaningful change).
    Returns False if there are meaningful differences (new/removed jobs).
    """
    if not HAS_ANTHROPIC:
        raise RuntimeError("Anthropic package not installed. Run: pip install anthropic")

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY environment variable not set")

    logging.info("Comparing screenshots using Claude AI vision...")

    # Read and encode both images
    with open(img1_path, "rb") as f:
        img1_data = base64.standard_b64encode(f.read()).decode("utf-8")

    with open(img2_path, "rb") as f:
        img2_data = base64.standard_b64encode(f.read()).decode("utf-8")

    client = anthropic.Anthropic(api_key=api_key)

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

    # Return True if SAME (no change), False if DIFFERENT (change detected)
    return "SAME" in response_text


# --------- Email ---------

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
    """Send an email with optional image attachment."""
    msg = MIMEMultipart()
    msg["From"] = from_addr
    msg["To"] = ", ".join(to_addrs)
    msg["Subject"] = subject

    msg.attach(MIMEText(body_text, "plain", "utf-8"))

    # Attach image if provided
    if image_path and image_path.exists():
        with open(image_path, "rb") as f:
            img_data = f.read()

        img_attachment = MIMEImage(img_data, name=image_path.name)
        img_attachment.add_header("Content-Disposition", "attachment", filename=image_path.name)
        msg.attach(img_attachment)

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
    """Send email alert with optional image. Returns True if successful."""
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


# --------- Main ---------

def main():
    configure_logging()
    load_dotenv()
    ensure_dirs()

    logging.info("=" * 60)
    logging.info("Screen Compare Monitor Started")
    logging.info("=" * 60)

    # Load config
    config_path = os.getenv("CONFIG_PATH", "monitors.yaml")
    try:
        cfg = load_yaml(config_path)
    except Exception as e:
        logging.error(f"Cannot read {config_path}: {e}")
        sys.exit(1)

    # Get first monitor's URL (or could be configured differently)
    monitors = cfg.get("monitors", [])
    if not monitors:
        logging.error("No monitors defined in monitors.yaml")
        sys.exit(1)

    monitor = monitors[0]  # Use first monitor
    url = monitor["url"]
    name = monitor.get("name", "ScreenCompare")
    headless = bool(monitor.get("headless", cfg.get("defaults", {}).get("headless", False)))

    # Email configuration
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

    # LinkedIn credentials
    linkedin_username = os.getenv("LINKEDIN_USERNAME")
    linkedin_password = os.getenv("LINKEDIN_PASSWORD")

    # Check for Anthropic API key (needed for comparison)
    if SCREENSHOT1_PATH.exists() and not os.getenv("ANTHROPIC_API_KEY"):
        logging.error("ANTHROPIC_API_KEY environment variable not set (required for screenshot comparison)")
        sys.exit(3)

    # Main logic
    if not SCREENSHOT1_PATH.exists():
        # First run - capture initial screenshot
        logging.info("No existing screenshot found. Capturing initial screenshot...")

        success = capture_screenshot(
            url=url,
            output_path=SCREENSHOT1_PATH,
            linkedin_username=linkedin_username,
            linkedin_password=linkedin_password,
            headless=headless
        )

        if not success:
            logging.error("Failed to capture initial screenshot")
            sys.exit(4)

        # Email the initial screenshot
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
        # Subsequent run - capture and compare
        logging.info("Existing screenshot found. Capturing new screenshot for comparison...")

        success = capture_screenshot(
            url=url,
            output_path=SCREENSHOT2_PATH,
            linkedin_username=linkedin_username,
            linkedin_password=linkedin_password,
            headless=headless
        )

        if not success:
            logging.error("Failed to capture comparison screenshot")
            sys.exit(4)

        # Compare screenshots using AI
        try:
            is_same = compare_screenshots_with_ai(SCREENSHOT1_PATH, SCREENSHOT2_PATH)
        except Exception as e:
            logging.error(f"AI comparison failed: {e}")
            logging.debug(traceback.format_exc())
            # Clean up and exit
            if SCREENSHOT2_PATH.exists():
                SCREENSHOT2_PATH.unlink()
            sys.exit(5)

        if is_same:
            # No change - discard the new screenshot
            logging.info("No meaningful change detected. Discarding new screenshot.")
            SCREENSHOT2_PATH.unlink()
        else:
            # Change detected - send alert and rotate screenshots
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

            # Rotate screenshots: delete old, rename new to old
            logging.info("Rotating screenshots...")
            SCREENSHOT1_PATH.unlink()
            SCREENSHOT2_PATH.rename(SCREENSHOT1_PATH)
            logging.info("Screenshot rotation complete")

    logging.info("Screen Compare Monitor Finished")
    logging.info("=" * 60)


if __name__ == "__main__":
    main()
