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
from collections import Counter
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
    """Direct search-results URL in LinkedIn's current layout.

    Only f_TPR (past 24 hours) and f_EA (under 10 applicants) survive in the URL. Going
    straight to /jobs/search-results/ avoids the hidden salary filter (f_SAL) that the old
    /jobs/search/ path redirects into. Remote and the title rule are enforced from each
    card, and the sort option no longer exists in this layout.
    """
    url = ("https://www.linkedin.com/jobs/search-results/?"
           f"keywords={quote(search_keywords())}&geoId={US_GEO_ID}"
           "&f_TPR=r86400&f_EA=true")
    return url + (f"&start={25 * (page - 1)}" if page > 1 else "")


def _norm(s: str) -> str:
    return dup_gate.norm(s)


def _word_re(words):
    alt = "|".join(re.escape(w) for w in words)
    return re.compile(rf"(?<![\w-])({alt})(?![\w-])", re.I)


_LEAD, _AI, _TOP = _word_re(LEADERSHIP), _word_re(AI_WORDS), _word_re(TOP_OFFICER)


def title_matches_search(title: str) -> bool:
    """The spec's deterministic title rule. LinkedIn's search is now semantic, so enforce it here:
    (leadership word AND AI word) OR a top-officer title on its own."""
    return bool((_LEAD.search(title) and _AI.search(title)) or _TOP.search(title))


_CARD_SUFFIX = re.compile(r"\s*\((Verified job|Promoted)\)$", re.I)


def parse_card(job_id: str, lines: list) -> dict:
    """Parse a result card's text lines: title (maybe repeated), company, 'Place (Workplace)'."""
    lines = [l.strip() for l in lines if l.strip() and l.strip() != "�"]
    title = _CARD_SUFFIX.sub("", lines[0]) if lines else ""
    rest = [l for l in lines[1:] if _norm(_CARD_SUFFIX.sub("", l)) != _norm(title)]
    location = rest[1] if len(rest) > 1 else ""
    m = re.search(r"\((Remote|Hybrid|On-site)\)", location, re.I)
    return {"job_id": str(job_id), "title": title, "company": rest[0] if rest else "",
            "location": location, "workplace": m.group(1).title() if m else "",
            "url": f"https://www.linkedin.com/jobs/view/{job_id}/"}


def parse_posting(tab_title: str, lines: list, description: str) -> dict:
    """Parse an opened posting page. tab_title is 'Title | Company | LinkedIn'."""
    parts = [p.strip() for p in tab_title.split(" | ")]
    ok = len(parts) >= 3 and parts[-1].lower() == "linkedin"
    title, company = (" | ".join(parts[:-2]), parts[-2]) if ok else ("", "")
    lines = [l.strip() for l in lines if l.strip()]
    i = next((k for k, l in enumerate(lines) if l == title), 0)
    j = next((k for k, l in enumerate(lines) if k > i and l.lower() == "about the job"),
             min(len(lines), i + 12))
    header = lines[i:j]
    workplace = next((l for l in header if l in ("Remote", "Hybrid", "On-site")), "")
    m = re.search(r"(over\s+[\d,]+|[\d,]+)\s+(applicants|people clicked apply)", " ".join(header), re.I)
    full = " ".join(lines).lower()
    poster = next((lines[k + 1] for k, l in enumerate(lines[:-1]) if l.lower() == "meet the hiring team"), None)
    return {
        "loaded": bool(ok and title and company), "title": title, "company": company,
        "description": description,
        "not_accepting": "no longer accepting applications" in full or "not currently accepting" in full,
        "already_applied": any(re.match(r"applied\b", l, re.I) or "application submitted" in l.lower() for l in header),
        "workplace": workplace, "applicants": m.group(0) if m else None,
        "apply_type": "Easy Apply" if "Easy Apply" in header else "Apply", "poster": poster,
    }


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
# Browser side. LinkedIn's markup changes often; anything that cannot be verified
# makes the run stop rather than guess.
# ---------------------------------------------------------------------------

CARDS_JS = r"""() => {
  const seen = new Set(), out = [];
  for (const c of document.querySelectorAll('[componentkey^="job-card-component-ref-"]')) {
    const id = c.getAttribute('componentkey').split('-').pop();
    if (seen.has(id)) continue;
    seen.add(id);
    out.push({id, lines: c.innerText.split('\n')});
  }
  return out;
}"""

FILTER_STATE_JS = r"""() => {
  const read = name => {
    for (const l of document.querySelectorAll('label,div,span,p')) {
      if (!l.offsetParent || l.getBoundingClientRect().left < 1000 || (l.innerText || '').trim() !== name) continue;
      let n = l;
      for (let k = 0; k < 4 && n; k++, n = n.parentElement) {
        const i = n.querySelector('input');
        if (i) return i.checked;
      }
    }
    return null;
  };
  return {past_24h: read('Past 24 hours'), under_10_applicants: read('Under 10 applicants')};
}"""

POSTING_JS = r"""() => {
  const about = [...document.querySelectorAll('*')].find(
      e => e.children.length === 0 && /^About the job$/i.test((e.innerText || '').trim()));
  let desc = '';
  if (about && about.parentElement && about.parentElement.parentElement) {
    desc = about.parentElement.parentElement.innerText;
    const cut = desc.search(/\n(About the company|Set alert for similar jobs|More jobs)\b/);
    if (cut > 0) desc = desc.slice(0, cut);
  }
  return {tab: document.title, lines: document.body.innerText.split('\n'), desc};
}"""


def verify_filters(page) -> dict:
    """Open 'All filters' and read the real controls. Remote is enforced per card (see run)."""
    page.get_by_text("All filters", exact=True).first.click()
    page.wait_for_timeout(2000)
    state = page.evaluate(FILTER_STATE_JS)
    page.keyboard.press("Escape")
    page.wait_for_timeout(500)
    return {k: bool(v) for k, v in state.items()}


def read_detail(page, url: str) -> dict:
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(3000)
    raw = page.evaluate(POSTING_JS)
    return parse_posting(raw["tab"], raw["lines"], raw["desc"])


def run(dry_run: bool = True, headed: bool = False, max_pages: int = 2) -> list:
    from playwright.sync_api import sync_playwright

    if not STATE.exists():
        sys.exit("No agent session. Run:  python manual_login.py agent")
    results = []

    def record(card, action, why, detail=None):
        merged = {**card, **(detail or {})}
        results.append((merged, action, why))
        logging.info(f"[{card['job_id']}] {card['company']} | {card['title']} -> {action}: {why}")
        if dry_run or action == "error":
            return
        if action == "queue":
            ok, qwhy = queue_tool.add(merged)
            ledger.add(card["job_id"], "queued" if ok else f"skipped: {qwhy}")
        else:
            ledger.add(card["job_id"], f"{action}: {why}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        try:
            ctx = browser.new_context(storage_state=str(STATE), viewport={"width": 1920, "height": 1080})
            page = ctx.new_page()
            cards = []
            for page_no in range(1, max_pages + 1):
                page.goto(build_search_url(page_no), wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(4000)
                if "/login" in page.url or "authwall" in page.url:
                    sys.exit("LinkedIn session expired. Run:  python manual_login.py agent")
                if page_no == 1:
                    badges = verify_filters(page)
                    logging.info(f"Filters: {badges}. Remote is enforced per card. Sort is not "
                                 "available in LinkedIn's current layout; reading in its default order.")
                    if not all(badges.values()):
                        sys.exit(f"Aborting: filters not active {badges}. Nothing was read.")
                raw = page.evaluate(CARDS_JS)
                if not raw:
                    sys.exit("Aborting: no job cards could be read from the results page.")
                page_cards = [parse_card(c["id"], c["lines"]) for c in raw]
                new = set(ledger.new_ids(c["job_id"] for c in page_cards))
                cards += [c for c in page_cards if c["job_id"] in new]
                if new:
                    break  # page 2 only when page 1 had nothing new

            for card in cards:
                if not title_matches_search(card["title"]):
                    record(card, "skip", "title does not match the search rule")
                    continue
                if card["workplace"].lower() in ("hybrid", "on-site"):
                    record(card, "held", f"workplace is {card['workplace']}")
                    continue
                # Remote, or the card is silent: open the posting. decide() makes the final call.
                try:
                    detail = read_detail(page, card["url"])
                except Exception as e:
                    results.append((card, "error", f"could not open: {e}"))
                    continue
                action, why = decide(card, detail)
                record(card, action, why, detail)
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
    print(f"\n{len(out)} posting(s) processed" + (" (dry run, nothing written)" if a.dry_run else ""),
          dict(Counter(a for _, a, _ in out)))
