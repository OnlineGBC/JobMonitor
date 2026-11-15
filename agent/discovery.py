"""Discovery scan: search, open new postings, run the gates, ledger and queue. Never applies.

    python -m agent.discovery --dry-run [--headed]   # decide and print; writes nothing
    python -m agent.discovery                        # live: ledger + queue

Uses its own LinkedIn session file (snapshots/agent_linkedin_state.json). Create it with:
    python manual_login.py agent
"""
import argparse
import logging
import re
import sys
from pathlib import Path
from urllib.parse import quote

from . import core_check, dup_gate, ledger, queue_tool
from .criteria_gate import gate1

STATE = Path("snapshots/agent_linkedin_state.json")
US_GEO_ID = "103644278"

LEADERSHIP = ['VP', 'Vice President', 'Vice-President', 'SVP', 'EVP', 'Director', 'Head', 'Leader', 'Chief']
AI_WORDS = ['AI', 'Artificial Intelligence']
TOP_OFFICER = ['CAIO', 'CTO', 'Chief Technology Officer', 'CIO', 'Chief Information Officer',
               'Fractional CTO', 'Fractional CIO']


def search_keywords() -> str:
    def anyof(words):
        return "(" + " OR ".join(f'title:"{w}"' for w in words) + ")"
    return f"({anyof(LEADERSHIP)} AND {anyof(AI_WORDS)}) OR {anyof(TOP_OFFICER)}"


def build_search_url(page: int = 1) -> str:
    """US, Past 24 hours, Remote, Most recent. (Under-10-applicants is applied on screen.)"""
    url = ("https://www.linkedin.com/jobs/search/?"
           f"keywords={quote(search_keywords())}&geoId={US_GEO_ID}"
           "&f_TPR=r86400&f_WT=2&sortBy=DD")
    return url + (f"&start={25 * (page - 1)}" if page > 1 else "")


def _norm(s: str) -> str:
    return dup_gate.norm(s)


def same_posting(card: dict, detail: dict) -> bool:
    """Load check: the opened page must match the card's title and company."""
    t1, t2 = _norm(card.get("title")), _norm(detail.get("title"))
    c1, c2 = _norm(card.get("company")), _norm(detail.get("company"))
    return bool(t2 and c2 and (t1 in t2 or t2 in t1) and (c1 in c2 or c2 in c1))


def decide(card: dict, detail: dict, judge=core_check.judge, tracker=None):
    """Run one opened posting through the pipeline.

    Returns (action, reason) where action is one of:
      error   - extraction/judgment failure: do NOT ledger, do NOT queue
      closed  - not accepting applications: ledger
      skip    - failed a gate: ledger
      held    - passed but not Remote: ledger, not queued
      queue   - passed everything and is Remote: ledger + queue
    """
    if not detail.get("loaded") or not same_posting(card, detail):
        return "error", "page did not load live with matching title and company"
    if detail.get("not_accepting"):
        return "closed", "not currently accepting applications"
    posting = {**card, **detail}
    ok, why = gate1(posting)
    if not ok:
        return "skip", f"gate 1: {why}"
    try:
        core, why = judge(posting)
    except core_check.CoreCheckError as e:
        return "error", f"core check unavailable: {e}"
    if not core:
        return "skip", f"core check: {why}"
    dup, why = dup_gate.is_duplicate(posting["company"], posting["title"], tracker)
    if dup:
        return "skip", f"gate 2: {why}"
    if (posting.get("workplace") or "").strip().lower() != "remote":
        return "held", f"workplace is {posting.get('workplace') or 'unknown'}"
    return "queue", "passed all gates"


# ---------------------------------------------------------------------------
# Browser side. LinkedIn's markup changes often; selectors are best-effort and
# anything that cannot be verified makes the run stop rather than guess.
# ---------------------------------------------------------------------------

def job_id_from_url(url: str):
    m = re.search(r"/jobs/view/(\d+)", url or "")
    return m.group(1) if m else None


def verify_filters(page) -> dict:
    """The URL carries Past 24h / Remote / sort; the applicants filter must show as active."""
    url = page.url
    badges = {
        "past_24h": "f_TPR=r86400" in url,
        "remote": "f_WT=2" in url,
        "sort_recent": "sortBy=DD" in url,
        "under_10_applicants": False,
    }
    try:
        badges["under_10_applicants"] = page.locator(
            "button[aria-pressed='true'], button[aria-checked='true'], .artdeco-pill--selected"
        ).filter(has_text=re.compile(r"under 10 applicants", re.I)).count() > 0
    except Exception:
        pass
    return badges


def apply_applicant_filter(page) -> None:
    try:
        btn = page.get_by_role("button", name=re.compile(r"under 10 applicants", re.I)).first
        if btn.count():
            btn.click()
            page.wait_for_timeout(2500)
    except Exception as e:
        logging.warning(f"Could not click the applicants filter: {e}")


def read_detail(page, url: str) -> dict:
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(2500)
    d = page.evaluate("""() => {
        const t = s => { const e = document.querySelector(s); return e ? e.innerText.trim() : ''; };
        const body = document.body.innerText;
        return {
          title: t('h1') || t('.job-details-jobs-unified-top-card__job-title'),
          company: t('.job-details-jobs-unified-top-card__company-name') || t('.topcard__org-name-link'),
          description: t('#job-details') || t('.jobs-description__content') || t('.description__text'),
          body: body.slice(0, 6000),
        };
    }""")
    body = d.pop("body", "")
    low = body.lower()
    d["loaded"] = bool(d["title"] and d["company"])
    d["not_accepting"] = "no longer accepting applications" in low or "not currently accepting" in low
    d["workplace"] = next((w for w in ("Remote", "Hybrid", "On-site") if re.search(rf"\b{w}\b", body)), "")
    m = re.search(r"(over\s+\d+|\d+)\s+(applicants|people clicked apply)", body, re.I)
    d["applicants"] = m.group(0) if m else None
    d["apply_type"] = "Easy Apply" if "easy apply" in low else "Apply"
    m = re.search(r"meet the hiring team\s*\n+\s*([^\n]+)", body, re.I)
    d["poster"] = m.group(1).strip() if m else None
    return d


def run(dry_run: bool = True, headed: bool = False, max_pages: int = 2) -> list:
    from playwright.sync_api import sync_playwright
    from monitor import extract_job_listings

    if not STATE.exists():
        sys.exit("No agent session. Run:  python manual_login.py agent")
    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        try:
            ctx = browser.new_context(storage_state=str(STATE), viewport={"width": 1920, "height": 1080})
            page = ctx.new_page()
            cards, page_no = [], 1
            while page_no <= max_pages:
                page.goto(build_search_url(page_no), wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(3000)
                if "/login" in page.url or "authwall" in page.url:
                    sys.exit("LinkedIn session expired. Run:  python manual_login.py agent")
                if page_no == 1:
                    apply_applicant_filter(page)
                badges = verify_filters(page)
                if not all(badges.values()):
                    logging.warning(f"Filter badges not all active: {badges}")
                    if not badges["sort_recent"]:
                        logging.warning("Sort will not hold; continuing with page 1 only")
                    missing = [k for k, v in badges.items() if not v and k != "sort_recent"]
                    if missing:
                        sys.exit(f"Aborting: cannot verify filters {missing}. Nothing was read.")
                page_cards = [{**c, "job_id": job_id_from_url(c["url"])} for c in extract_job_listings(page)]
                fresh = ledger.new_ids(c["job_id"] for c in page_cards if c["job_id"])
                cards += [c for c in page_cards if c["job_id"] in fresh]
                if fresh or not badges["sort_recent"]:
                    break  # page 2 only when page 1 had nothing new
                page_no += 1

            for card in cards:
                try:
                    detail = read_detail(page, card["url"])
                except Exception as e:
                    results.append((card, "error", f"could not open: {e}"))
                    continue
                action, why = decide(card, detail)
                results.append(({**card, **detail}, action, why))
                logging.info(f"[{card['job_id']}] {card['company']} | {card['title']} -> {action}: {why}")
                if dry_run or action == "error":
                    continue
                if action == "queue":
                    ok, qwhy = queue_tool.add({**card, **detail})
                    ledger.add(card["job_id"], "queued" if ok else f"skipped: {qwhy}")
                else:
                    ledger.add(card["job_id"], f"{action}: {why}")
        finally:
            browser.close()
    return results


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="decide and print; write nothing")
    ap.add_argument("--headed", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    out = run(dry_run=a.dry_run, headed=a.headed)
    print(f"\n{len(out)} posting(s) processed" + (" (dry run, nothing written)" if a.dry_run else ""))
