"""Log in to LinkedIn by hand and save the session for a monitor.

Use when the automatic login is blocked (e.g. a Cloudflare bot check) and a
monitor reports "Saved LinkedIn session appears expired".

    python manual_login.py "RemoteUSA"

Opens a visible Chrome window. Solve any check, log in, and wait for the feed.
The session is then saved to snapshots/<name>_linkedin_state.json.
"""
import re
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

if len(sys.argv) != 2:
    sys.exit('Usage: python manual_login.py "<monitor name>"')

name = sys.argv[1]
out = Path(__file__).parent / "snapshots" / f"{re.sub(r'[^A-Za-z0-9_-]+', '_', name)}_linkedin_state.json"

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    page = ctx.new_page()
    page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded")
    print("Window open - log in manually (waiting up to 8 min)...", flush=True)
    try:
        page.wait_for_url(lambda u: any(x in u for x in ("/feed", "/mynetwork", "/jobs")), timeout=480000)
        page.wait_for_timeout(3000)
        ctx.storage_state(path=str(out))
        print(f"Saved session to {out}")
    except Exception as e:
        print(f"Not saved: {e}")
    browser.close()
