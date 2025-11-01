#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Web Change Monitor (Windows-friendly)
- Supports requests-based fetch (fast) and optional Playwright (JS-rendered) fetch
- Monitors full page or a CSS-selected element
- Strips ignorable CSS selectors and regex patterns before diffing
- Stores snapshots under ./snapshots/<monitor_name>.txt
- Sends email (SMTP) when a change is detected, with a short textual diff

Run once and exit. Use Windows Task Scheduler to run every X minutes.

Author: ChatGPT (GPT-5 Thinking)
"""

import os
import re
import sys
import time
import smtplib
import hashlib
import logging
import difflib
import traceback
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path
from typing import Dict, Any, Optional, List

import yaml
from dotenv import load_dotenv

# Optional imports (only needed if you enable JS rendering)
try:
    from playwright.sync_api import sync_playwright  # type: ignore
    HAS_PLAYWRIGHT = True
except Exception:
    HAS_PLAYWRIGHT = False

try:
    from bs4 import BeautifulSoup  # type: ignore
    HAS_BS4 = True
except Exception:
    HAS_BS4 = False

import requests


# --------- Utilities ---------

def ensure_dirs():
    Path("snapshots").mkdir(exist_ok=True)
    Path("logs").mkdir(exist_ok=True)


def configure_logging():
    ensure_dirs()
    log_path = Path("logs/monitor.log")
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


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()


def strip_ignorable_selectors(html: str, css_selectors: List[str]) -> str:
    if not HAS_BS4:
        return html  # graceful fallback
    soup = BeautifulSoup(html, "html.parser")
    for selector in css_selectors:
        try:
            for tag in soup.select(selector):
                tag.decompose()
        except Exception:
            # Ignore bad selectors
            continue
    return str(soup)


def select_element(html: str, selector: Optional[str]) -> str:
    """Return inner text/HTML of the selected element; fallback to full doc if selector is None or bs4 missing."""
    if not selector or not HAS_BS4:
        return html
    soup = BeautifulSoup(html, "html.parser")
    node = soup.select_one(selector)
    return str(node) if node else ""


def apply_regex_ignores(text: str, patterns: List[str]) -> str:
    result = text
    for pat in patterns:
        try:
            result = re.sub(pat, "", result, flags=re.MULTILINE)
        except re.error:
            # Ignore invalid regex
            continue
    return result


def normalize_whitespace(text: str) -> str:
    # collapse multiple spaces/newlines to reduce noise
    return re.sub(r"\s+\n", "\n", re.sub(r"[ \t]+", " ", text)).strip()


def filter_dynamic_content(content: str) -> str:
    """Filter out dynamic content that changes frequently but isn't meaningful."""
    if not content:
        return content
    
    lines = content.split('\n')
    filtered_lines = []
    
    for line in lines:
        line = line.strip()
        
        # Skip empty lines
        if not line:
            filtered_lines.append('')
            continue
        
        # Filter out dynamic metadata patterns
        skip_patterns = [
            r'^\+\d+\s+benefits?$',           # "+3 benefits", "+4 benefits"
            r'^\d+\s+benefits?$',              # "3 benefits", "4 benefits" 
            r'^\d+\s+views?$',                 # "23 views", "120 views"
            r'^\d+\s+applicants?$',            # "23 applicants", "120 applicants"
            r'^Just now$',                     # "Just now"
            r'^\d+\s+minutes?\s+ago$',         # "5 minutes ago"
            r'^\d+\s+hours?\s+ago$',          # "2 hours ago"
            r'^\d+\s+days?\s+ago$',           # "3 days ago"
            r'^Updated on \d{4}-\d{2}-\d{2}$', # "Updated on 2024-10-18"
            r'^\d{1,2}:\d{2}(?:am|pm)$',       # "14:30", "2:30pm"
            r'^\d{4}-\d{2}-\d{2}$',           # "2024-10-18"
            r'^Reposted$',                     # "Reposted"
            r'^Promoted$',                     # "Promoted"
        ]
        
        # Check if line matches any skip pattern
        should_skip = False
        for pattern in skip_patterns:
            if re.match(pattern, line, re.IGNORECASE):
                should_skip = True
                break
        
        if not should_skip:
            filtered_lines.append(line)
    
    return '\n'.join(filtered_lines)


def normalize_job_content(content: str) -> str:
    """Normalize job content to eliminate false positives from dynamic ordering."""
    if not content:
        return content
    
    # First filter out dynamic content
    content = filter_dynamic_content(content)
    
    lines = content.split('\n')
    normalized_lines = []
    
    # Group lines into job blocks
    current_job = []
    
    for line in lines:
        line = line.strip()
        
        if not line:
            # Empty line - end of job block
            if current_job:
                # Sort the job block lines and add to normalized
                current_job.sort()
                normalized_lines.extend(current_job)
                normalized_lines.append('')  # Add empty line separator
                current_job = []
        else:
            # Non-empty line - add to current job block
            current_job.append(line)
    
    # Handle last job block
    if current_job:
        current_job.sort()
        normalized_lines.extend(current_job)
    
    return '\n'.join(normalized_lines)


def text_diff(old: str, new: str, context: int = 3, max_lines: int = 200) -> str:
    diff_lines = list(difflib.unified_diff(
        old.splitlines(), new.splitlines(),
        lineterm="", n=context, fromfile="previous", tofile="current"
    ))
    # Limit excessively long diffs in email
    if len(diff_lines) > max_lines:
        diff_lines = diff_lines[:max_lines] + ["... (diff truncated)"]
    return "\n".join(diff_lines)

def extract_job_key_list_from_html(html: str) -> Optional[str]:
    """
    Return a stable JSON array of (id, title, url) tuples for *real* LinkedIn job items.
    Real items have a data job-id OR a link to /jobs/view/.
    """
    if not HAS_BS4:
        return None
    try:
        soup = BeautifulSoup(html, "html.parser")
        # Tolerate DOM variants
        ul = (
            soup.select_one("ul.jobs-search__results-list")
            or soup.select_one("div.jobs-search__results-list")
            or soup.select_one("[data-search-results-container='true']")
        )
        if not ul:
            return "[]"
        items = []
        for li in ul.find_all("li", recursive=False):
            job_id = (li.get("data-occludable-job-id") or li.get("data-job-id") or "").strip()
            title_el = li.select_one("a[href*='/jobs/view/'], a.job-card-list__title, a.job-card-container__link")
            title = title_el.get_text(strip=True) if title_el else ""
            # Extract job URL
            job_url = ""
            if title_el and title_el.get("href"):
                href = title_el.get("href", "").strip()
                # Make absolute URL if relative
                if href.startswith("/"):
                    job_url = f"https://www.linkedin.com{href}"
                elif href.startswith("http"):
                    job_url = href
                else:
                    job_url = href
            # Only keep entries that look like real jobs
            if job_id or (title_el and title):
                items.append((job_id, title, job_url))
        items = sorted(set(items))
        import json
        return json.dumps(items, ensure_ascii=False, sort_keys=True, indent=2)
    except Exception:
        return None

# --------- Fetchers ---------

def fetch_via_requests(url: str, timeout: int, headers: Dict[str, str]) -> str:
    resp = requests.get(url, timeout=timeout, headers=headers)
    resp.raise_for_status()
    # Best-effort to get proper encoding
    resp.encoding = resp.apparent_encoding or resp.encoding
    return resp.text


def login_to_linkedin(page, username: str, password: str) -> bool:
    """
    Login to LinkedIn with provided credentials.
    Returns True if login successful, False otherwise.
    """
    try:
        logging.info("Logging in to LinkedIn...")
        # Navigate to LinkedIn login page
        page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded", timeout=30000)
        
        # Wait for login form
        page.wait_for_selector('input[name="session_key"]', timeout=10000)
        page.wait_for_selector('input[name="session_password"]', timeout=10000)
        
        # Fill in credentials
        page.fill('input[name="session_key"]', username)
        page.fill('input[name="session_password"]', password)
        
        # Submit form
        page.click('button[type="submit"]')
        
        # Wait for navigation after login (check for feed or profile)
        try:
            # Wait for either successful login indicators or error message
            page.wait_for_load_state("networkidle", timeout=15000)
            page.wait_for_timeout(2000)  # Additional wait for redirect
            
            # Check if we're logged in by looking for common logged-in elements
            current_url = page.url
            if "/feed" in current_url or "/mynetwork" in current_url or current_url == "https://www.linkedin.com/":
                logging.info("LinkedIn login successful")
                return True
            
            # Check for error messages
            error_selectors = [
                'div[role="alert"]',
                '.error-for-password',
                '#error-for-password',
                'div[data-test-id="login-error"]'
            ]
            for selector in error_selectors:
                error_el = page.query_selector(selector)
                if error_el and error_el.is_visible():
                    error_text = error_el.inner_text()
                    logging.warning(f"LinkedIn login error detected: {error_text}")
                    return False
            
            # If we're not redirected but no error, might need verification
            if "/challenge" in current_url or "checkpoint" in current_url:
                logging.warning("LinkedIn login requires verification/challenge (2FA, captcha, etc.)")
                return False
            
            # Default: assume success if we got past login page
            logging.info("LinkedIn login appears successful")
            return True
            
        except Exception as e:
            logging.warning(f"Could not verify LinkedIn login status: {e}")
            # Assume failure if we can't verify
            return False
            
    except Exception as e:
        logging.error(f"LinkedIn login failed: {e}")
        logging.debug(traceback.format_exc())
        return False


def fetch_via_playwright(url: str, timeout: int, wait_until: str, wait_selector: Optional[str], user_agent: Optional[str], linkedin_username: Optional[str] = None, linkedin_password: Optional[str] = None, headless: bool = False, storage_state_path: Optional[str] = None) -> str:
    if not HAS_PLAYWRIGHT:
        raise RuntimeError("Playwright not installed. Install and run: pip install playwright && playwright install chromium")
    
    # Determine if we need to login (LinkedIn URLs)
    needs_login = "linkedin.com" in url and linkedin_username and linkedin_password
    
    # Storage state path for cookie persistence (LinkedIn only)
    if needs_login and not storage_state_path:
        storage_state_path = "linkedin_state.json"
    
    # Check if storage state exists
    storage_state = None
    has_saved_session = False
    if storage_state_path and Path(storage_state_path).exists():
        try:
            storage_state = storage_state_path
            has_saved_session = True
            logging.info(f"Loading saved LinkedIn session from {storage_state_path}")
        except Exception as e:
            logging.warning(f"Could not load storage state: {e}")
            storage_state = None
    
    # For LinkedIn: Use visible browser only when cookies are unavailable or invalid
    # If valid cookies exist, use headless mode to avoid showing browser unnecessarily
    effective_headless = headless
    if needs_login:
        # If we have a saved session, we'll validate it and use headless if valid
        # For now, assume we'll use headless if session exists (will be validated below)
        if has_saved_session:
            effective_headless = True  # Use headless when cookies are available
            logging.info("Valid cookies detected - using headless mode")
        else:
            effective_headless = False  # Use visible browser when no cookies (for login/challenges)
            logging.info("No cookies available - using visible browser for login")
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=effective_headless)
        try:
            context_options = {
                "user_agent": user_agent,
                "viewport": {'width': 1920, 'height': 1080},
                "ignore_https_errors": True
            }
            
            # Load saved session state if available
            if storage_state:
                context_options["storage_state"] = storage_state
            
            context = browser.new_context(**context_options)
            page = context.new_page()
            page.set_default_timeout(timeout * 1000)
            page.set_default_navigation_timeout(timeout * 1000)
            
            # Track session state
            session_valid = False
            login_success = False
            is_authenticated = False
            
            # If we have a saved session, validate it before using
            if has_saved_session and needs_login:
                try:
                    # Try to access LinkedIn to check if session is valid
                    page.goto("https://www.linkedin.com/feed", wait_until="domcontentloaded", timeout=10000)
                    page.wait_for_timeout(2000)
                    current_url = page.url
                    # If we're redirected to login, session is invalid
                    if "/login" not in current_url and ("/feed" in current_url or current_url == "https://www.linkedin.com/" or "/mynetwork" in current_url):
                        session_valid = True
                        is_authenticated = True
                        logging.info("Saved LinkedIn session is still valid")
                    else:
                        logging.info("Saved LinkedIn session appears expired, will re-login")
                        # Note: Browser is already launched in headless mode since cookies existed
                        # If re-login requires interaction (challenge/captcha), user should delete linkedin_state.json and rerun
                except Exception as e:
                    logging.warning(f"Could not validate session, will re-login: {e}")
            
            # Login to LinkedIn if credentials provided and (no saved session or session expired)
            if needs_login and not session_valid:
                login_success = login_to_linkedin(page, linkedin_username, linkedin_password)
                if login_success:
                    is_authenticated = True
                    # Save session state after successful login
                    if storage_state_path:
                        try:
                            context.storage_state(path=storage_state_path)
                            logging.info(f"Saved LinkedIn session to {storage_state_path}")
                        except Exception as e:
                            logging.warning(f"Could not save storage state: {e}")
                else:
                    logging.warning("LinkedIn login failed, continuing with unauthenticated access")
            
            # Navigate with more lenient settings
            page.goto(url, wait_until=wait_until, timeout=timeout * 1000)
            
            # For LinkedIn: Use Option 1B - wait for parent container that always exists
            if "linkedin.com/jobs/search" in url:
                # Wait for a container that exists whether jobs are present or not
                container_selectors = [
                    "[data-search-results-container='true']",
                    "div.jobs-search__results-list",
                    "main.jobs-search-results",
                    ".jobs-search-results-container"
                ]
                
                container_found = False
                wait_timeout = 15000  # 15 seconds instead of 90
                
                for selector in container_selectors:
                    try:
                        page.wait_for_selector(selector, timeout=wait_timeout)
                        container_found = True
                        logging.debug(f"LinkedIn results container found: {selector}")
                        break
                    except Exception:
                        continue
                
                if not container_found:
                    logging.warning("LinkedIn results container not found, continuing anyway")
                
                # Additional wait for dynamic content to fully render
                page.wait_for_timeout(2000)  # Wait 2 seconds for any remaining JS
            elif wait_selector:
                # For non-LinkedIn URLs, use the original wait selector logic
                try:
                    page.wait_for_selector(wait_selector, timeout=timeout * 1000)
                except Exception as e:
                    logging.warning(f"Wait selector '{wait_selector}' not found, continuing anyway: {e}")
                
                # Additional wait for dynamic content
                page.wait_for_timeout(2000)  # Wait 2 seconds for any remaining JS
            
            html = page.content()
            
            # Refresh session state after successful fetch (keeps session alive)
            if needs_login and storage_state_path and is_authenticated:
                try:
                    context.storage_state(path=storage_state_path)
                    logging.debug(f"Refreshed LinkedIn session state")
                except Exception as e:
                    logging.debug(f"Could not update storage state: {e}")
        finally:
            browser.close()
    return html


# --------- Email ---------

def send_email(
    smtp_host: str,
    smtp_port: int,
    smtp_username: str,
    smtp_password: str,
    use_tls: bool,
    from_addr: str,
    to_addrs: List[str],
    subject: str,
    body_text: str
):
    msg = MIMEMultipart()
    msg["From"] = from_addr
    msg["To"] = ", ".join(to_addrs)
    msg["Subject"] = subject

    msg.attach(MIMEText(body_text, "plain", "utf-8"))

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


# --------- Core monitoring ---------

def check_one_monitor(m: Dict[str, Any], defaults: Dict[str, Any], email_cfg: Dict[str, Any]) -> None:
    name = m["name"]
    url = m["url"]

    # Fetch mode
    render_js = bool(m.get("render_js", defaults.get("render_js", False)))
    wait_until = m.get("wait_until", defaults.get("wait_until", "load"))  # "domcontentloaded" | "load" | "networkidle"
    wait_selector = m.get("wait_selector", defaults.get("wait_selector"))
    headless = bool(m.get("headless", defaults.get("headless", False)))  # False = visible browser (default)

    # Extraction / filtering
    css_selector = m.get("css_selector")
    remove_selectors = m.get("remove_selectors", [])
    ignore_regexes = m.get("ignore_regexes", [])
    compare_mode = m.get("compare_mode", defaults.get("compare_mode", "text"))  # "text" or "html"
    normalize = bool(m.get("normalize_whitespace", defaults.get("normalize_whitespace", True)))

    # Network
    timeout = int(m.get("timeout_seconds", defaults.get("timeout_seconds", 30)))
    headers = defaults.get("headers", {})
    user_agent = m.get("user_agent", headers.get("User-Agent"))

    # LinkedIn authentication (optional, from environment variables)
    linkedin_username = os.getenv("LINKEDIN_USERNAME")
    linkedin_password = os.getenv("LINKEDIN_PASSWORD")

    # Email behavior
    email_on_first_snapshot = bool(m.get("email_on_first_snapshot", defaults.get("email_on_first_snapshot", False)))
    subject_prefix = email_cfg.get("subject_prefix", "[WebChange]")

    # Fetch content
    logging.info(f"[{name}] Fetching {url} (render_js={render_js}, headless={headless})")
    try:
        if render_js:
            html = fetch_via_playwright(url, timeout=timeout, wait_until=wait_until, wait_selector=wait_selector, user_agent=user_agent, linkedin_username=linkedin_username, linkedin_password=linkedin_password, headless=headless)
        else:
            html = fetch_via_requests(url, timeout=timeout, headers=headers)
    except Exception as e:
        logging.error(f"[{name}] Fetch error: {e}")
        logging.debug(traceback.format_exc())
        return

    # ----- Early exit if the page clearly shows "no results" -----
    try:
        if HAS_BS4:
            soup = BeautifulSoup(html, "html.parser")
            page_text = soup.get_text(separator=" ", strip=True)
            page_text_lc = page_text.lower()

            # Collect skip patterns from defaults + monitor
            skip_patterns = (defaults.get("skip_if_page_text_matches", []) or []) + \
                            (m.get("skip_if_page_text_matches", []) or [])

            # 1) Regex-based guard
            matched_pat = None
            for pat in skip_patterns:
                try:
                    if re.search(pat, page_text, flags=re.IGNORECASE):
                        matched_pat = pat
                        break
                except re.error:
                    logging.warning(f"[{name}] Bad regex in skip_if_page_text_matches: {pat!r}")

            # 2) Plain-substring guard (conservative; no suggestions-only phrase)
            plain_triggers = [
                "no matching jobs found",
                "0 results",
                "try adjusting your search",
            ]
            matched_plain = next((t for t in plain_triggers if t in page_text_lc), None)

            if matched_pat or matched_plain:
                logging.info(f"[{name}] Early skip (no email): "
                             f"{'regex '+repr(matched_pat) if matched_pat else 'plain '+repr(matched_plain)}")
                return

            # 3) Structural guard — count *real* job items only
            results_ul = (
                soup.select_one("ul.jobs-search__results-list")
                or soup.select_one("div.jobs-search__results-list")
                or soup.select_one("[data-search-results-container='true']")
            )
            real_count = 0
            if results_ul:
                for li in results_ul.find_all("li", recursive=False):
                    job_id = (li.get("data-occludable-job-id") or li.get("data-job-id") or "").strip()
                    title_a = li.select_one("a[href*='/jobs/view/']")
                    if job_id or title_a:
                        real_count += 1
            logging.info(f"[{name}] Structural check: real_count={real_count}")
            if real_count == 0:
                logging.info(f"[{name}] Structural empty results (0 real job cards) — skipping without email.")
                return
        else:
            # Fallback: raw-HTML plain check if bs4 is unavailable
            page_text_lc = html.lower()
            if ("no matching jobs found" in page_text_lc) or ("0 results" in page_text_lc):
                logging.info(f"[{name}] Early skip (no email): plain-fallback on raw HTML.")
                return
    except Exception as _e:
        logging.warning(f"[{name}] Skip-check failed, continuing: {_e}")

    # Strip ignorable selectors (noise)
    if remove_selectors:
        html = strip_ignorable_selectors(html, remove_selectors)

    # Select element or full page
    extracted = select_element(html, css_selector)

    # LinkedIn: prefer structured comparison (stable JSON of job keys)
    content = None
    structured = None
    if "linkedin.com/jobs/search" in url:
        structured = extract_job_key_list_from_html(extracted if extracted else html)
        if structured is not None:
            content = structured

    # Legacy text pipeline (if not structured)
    if content is None:
        if compare_mode == "text" and HAS_BS4:
            soup = BeautifulSoup(extracted if extracted else html, "html.parser")
            content = soup.get_text(separator="\n")
        else:
            content = extracted if extracted else html
        if ignore_regexes:
            content = apply_regex_ignores(content, ignore_regexes)
        if normalize:
            content = normalize_whitespace(content)
        # Last-mile normalization to reduce job reordering noise (legacy)
        content = normalize_job_content(content)
    # Snapshot paths
    snapshot_path = Path("snapshots") / f"{name}.txt"
    hash_path = Path("snapshots") / f"{name}.sha256"

    new_hash = sha256_text(content)

    if not snapshot_path.exists():
        # First snapshot
        snapshot_path.write_text(content, encoding="utf-8")
        hash_path.write_text(new_hash, encoding="utf-8")
        logging.info(f"[{name}] Created initial snapshot.")
        if email_on_first_snapshot:
            body = (
                f"Initial snapshot created for '{name}'.\n\n"
                f"URL: {url}\n"
                f"(No previous content to diff.)"
            )
            email_sent = _send_alert(email_cfg, subject=f"{subject_prefix} {name}: initial snapshot", body=body)
            if email_sent:
                logging.info(f"[{name}] Initial snapshot, Email sent.")
            else:
                logging.warning(f"[{name}] Initial snapshot, Email send failed.")
        else:
            logging.info(f"[{name}] Initial snapshot, No email sent (email_on_first_snapshot disabled).")
        return

    # Compare hash
    prev_hash = hash_path.read_text(encoding="utf-8").strip()
    if new_hash == prev_hash:
        logging.info(f"[{name}] No change detected, No email sent.")
        return

    # Load old content
    try:
        old_content = snapshot_path.read_text(encoding="utf-8")
    except Exception:
        old_content = ""

    # For LinkedIn structured data: only detect NEW jobs (additions only)
    if "linkedin.com/jobs/search" in url and structured is not None and content and content.strip().startswith('['):
        try:
            import json
            old_jobs = json.loads(old_content) if old_content else []
            new_jobs = json.loads(content) if content else []
            
            # Convert to sets of tuples for comparison
            # Handle both old format (2-tuple) and new format (3-tuple with URL)
            def normalize_job(job):
                """Normalize job tuple to handle migration from 2-tuple to 3-tuple format"""
                if isinstance(job, list) and len(job) >= 2:
                    # Pad with empty URL if old format (2-tuple)
                    if len(job) == 2:
                        return (job[0], job[1], "")
                    elif len(job) >= 3:
                        return (job[0], job[1], job[2] if len(job) > 2 else "")
                return None
            
            old_jobs_set = {normalize_job(job) for job in old_jobs if normalize_job(job)}
            new_jobs_set = {normalize_job(job) for job in new_jobs if normalize_job(job)}
            
            # Find only new jobs (additions) - compare by (job_id, title) ignoring URL for uniqueness
            # But preserve URL in the stored data
            old_jobs_keys = {(job[0], job[1]) for job in old_jobs_set}
            new_jobs_keys = {(job[0], job[1]) for job in new_jobs_set}
            new_additions_keys = new_jobs_keys - old_jobs_keys
            
            if not new_additions_keys:
                logging.info(f"[{name}] No new jobs detected (only removals/reordering), No email sent.")
                # Update snapshot with current jobs (to track removals for future comparison)
                snapshot_path.write_text(content, encoding="utf-8")
                hash_path.write_text(new_hash, encoding="utf-8")
                return
            
            # Get full tuples for new additions (including URL)
            new_additions = [job for job in new_jobs_set if (job[0], job[1]) in new_additions_keys]
            
            # Sort additions for display
            new_additions_list = sorted(new_additions)
            logging.info(f"[{name}] {len(new_additions)} new job(s) detected!")
            
            # Format new jobs for email with URLs
            new_jobs_text = "\n".join([
                f"  • {title}\n    {job_url if job_url else f'https://www.linkedin.com/jobs/view/{job_id}' if job_id else 'URL not available'}"
                for job_id, title, job_url in new_additions_list
            ])
            
            body = (
                f"New jobs detected for '{name}'!\n\n"
                f"URL: {url}\n\n"
                f"New jobs ({len(new_additions)}):\n"
                f"{'-'*70}\n"
                f"{new_jobs_text}\n"
                f"{'-'*70}\n\n"
                f"(A full snapshot is stored at: {snapshot_path.resolve()})"
            )
            email_sent = _send_alert(email_cfg, subject=f"{subject_prefix} {name}: {len(new_additions)} NEW JOB(S) DETECTED", body=body)
            if email_sent:
                logging.info(f"[{name}] Change detected, Email sent.")
            else:
                logging.warning(f"[{name}] Change detected, Email send failed.")
            
            # Update snapshot with merged jobs (keep old + new)
            # Deduplicate by (job_id, title), preferring URLs from new jobs
            merged_dict = {}
            # First add old jobs
            for job in old_jobs_set:
                key = (job[0], job[1])
                if key not in merged_dict:
                    merged_dict[key] = job
            # Then add/update with new jobs (this will update URLs if available)
            for job in new_jobs_set:
                key = (job[0], job[1])
                # Prefer new job if it has a URL and old doesn't, or if both have URLs prefer new
                if key not in merged_dict or (job[2] and not merged_dict[key][2]):
                    merged_dict[key] = job
            merged_jobs = sorted(list(merged_dict.values()))
            merged_content = json.dumps(merged_jobs, ensure_ascii=False, sort_keys=True, indent=2)
            snapshot_path.write_text(merged_content, encoding="utf-8")
            hash_path.write_text(sha256_text(merged_content), encoding="utf-8")
            return
        except Exception as e:
            logging.warning(f"[{name}] Structured comparison failed, falling back to text diff: {e}")
            # Fall through to text diff

    # Legacy text diff for non-structured content
    diff = text_diff(old_content, content, context=3, max_lines=200)
    snapshot_path.write_text(content, encoding="utf-8")
    hash_path.write_text(new_hash, encoding="utf-8")

    logging.info(f"[{name}] Change detected!")
    body = (
        f"Change detected for '{name}'.\n\n"
        f"URL: {url}\n\n"
        f"Unified diff (first 200 lines):\n"
        f"{'-'*70}\n"
        f"{diff}\n"
        f"{'-'*70}\n"
        f"(A full snapshot is stored at: {snapshot_path.resolve()})"
    )
    email_sent = _send_alert(email_cfg, subject=f"{subject_prefix} {name}: CHANGE DETECTED", body=body)
    if email_sent:
        logging.info(f"[{name}] Change detected, Email sent.")
    else:
        logging.warning(f"[{name}] Change detected, Email send failed.")


def _send_alert(email_cfg: Dict[str, Any], subject: str, body: str) -> bool:
    """
    Send email alert. Returns True if successful, False otherwise.
    """
    try:
        send_email(
            smtp_host=email_cfg["smtp_host"],
            smtp_port=int(email_cfg["smtp_port"]),
            smtp_username=email_cfg["smtp_username"],
            smtp_password=email_cfg["smtp_password"],
            use_tls=bool(int(email_cfg.get("smtp_use_tls", "1"))),
            from_addr=email_cfg["from_addr"],
            to_addrs=[addr.strip() for addr in email_cfg["to_addrs"].split(",") if addr.strip()],
            subject=subject,
            body_text=body
        )
        return True
    except Exception as e:
        logging.error(f"Error sending email: {e}")
        logging.debug(traceback.format_exc())
        return False


def main():
    configure_logging()
    load_dotenv()  # load .env
    ensure_dirs()

    # Load config files
    config_path = os.getenv("CONFIG_PATH", "monitors.yaml")
    try:
        cfg = load_yaml(config_path)
    except Exception as e:
        logging.error(f"Cannot read {config_path}: {e}")
        sys.exit(1)

    # Email configuration (required)
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
        "subject_prefix": os.getenv("SUBJECT_PREFIX", "[WebChange]"),
    }

    defaults = cfg.get("defaults", {})
    monitors = cfg.get("monitors", [])

    if not monitors:
        logging.error("No monitors defined in monitors.yaml")
        sys.exit(3)

    # Run each monitor once
    for m in monitors:
        try:
            check_one_monitor(m, defaults, email_cfg)
        except Exception as e:
            logging.error(f"[{m.get('name','<unnamed>')}] Unexpected error: {e}")
            logging.debug(traceback.format_exc())


if __name__ == "__main__":
    main()
