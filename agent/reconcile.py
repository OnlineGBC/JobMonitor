"""Reconcile: notice when an application the agent prepared has been submitted, and keep the tracker honest.

    python -m agent.reconcile          (also runs at the start of every applier run)

The agent saves a draft and books the application as AWAITING RAJA; Raja submits it on LinkedIn. Without this
step nothing would notice. For each AWAITING RAJA row it checks LinkedIn's own records:

  1. the job's EXACT number appears on the Applied tab of the job tracker, AND
  2. the posting page itself shows the application as submitted.

Only when both agree is the row settled as SUBMITTED, the queue item removed, and an "applied" report sent
(the independent-record rule). It never marks anything submitted on a guess: if the Applied tab cannot be
read, or the two records disagree, nothing changes and nothing is reported. LinkedIn does not say WHO
submitted, so the wording never claims it was Raja.

A row still unsubmitted after REMIND_AFTER_MINUTES gets ONE reminder (never repeated). If the Applied tab could
not be read that run, no reminder is sent, because the application might in fact have been submitted.
"""
import logging
import sys
from datetime import datetime

from . import discovery, queue_tool, reporter, tracker

APPLIED_URL = "https://www.linkedin.com/jobs-tracker/?stage=applied"
REMIND_AFTER_MINUTES = 60

APPLIED_JS = r"""() => {
  const out = [], seen = new Set();
  for (const a of document.querySelectorAll('main a[href*="/jobs/view/"]')) {
    const m = a.href.match(/\/jobs\/view\/(\d+)/);
    if (!m || seen.has(m[1])) continue;
    seen.add(m[1]);
    let n = a, text = '';
    for (let k = 0; k < 8 && n.parentElement; k++) {
      n = n.parentElement;
      const t = (n.innerText || '').trim().replace(/\s+/g, ' ');
      if (t.length > 20 && t.length < 500) { text = t; break; }
    }
    out.push({job_id: m[1], text: text.slice(0, 200)});
  }
  return out;
}"""


class LoginNeeded(Exception):
    pass


def read_applied(page, navigate: bool = True) -> list:
    """The job numbers on LinkedIn's Applied tab, newest first: [{'job_id', 'text'}]. [] if it cannot be read."""
    if navigate:
        page.goto(APPLIED_URL, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(5500)
        if "/login" in page.url or "authwall" in page.url:
            raise LoginNeeded()
    return page.evaluate(APPLIED_JS)


def _minutes_since(iso: str, now: datetime) -> float:
    return (now - datetime.fromisoformat(iso)).total_seconds() / 60


def process(applied_ids, confirm_posting, now: datetime = None) -> list:
    """Reconcile every AWAITING RAJA row. `applied_ids` is the set of job numbers on the Applied tab, or None if
    that tab could not be read. `confirm_posting(job_id)` says whether the posting page shows it as applied."""
    now = now or datetime.now(reporter.NY)
    results = []
    for row in tracker.rows():
        if row["status"] != "AWAITING RAJA":
            continue
        who = f"#{row['number']} {row['title']} at {row['company']}"
        jid, link = str(row["job_id"]), row.get("url") or f"https://www.linkedin.com/jobs/view/{row['job_id']}/"
        if applied_ids is None:
            results.append((who, "Applied tab not readable: left unchanged"))
            continue
        if jid in applied_ids:
            if not confirm_posting(jid):
                results.append((who, "on the Applied list but the posting page does not confirm: left unchanged"))
                continue
            tracker.settle(row["number"], "SUBMITTED",
                           f"LinkedIn shows this application as submitted: it is on the Applied tab and the posting "
                           f"page confirms it (checked {now.strftime('%Y-%m-%d %I:%M %p %Z')}). LinkedIn does not say "
                           f"who submitted it. Previously: {row.get('note', '')}")
            queue_tool.remove(jid)
            reporter.enqueue("applied", f"#{row['number']} Applied: {row['title']} at {row['company']}",
                             f"LinkedIn shows this application as submitted (the job is on your Applied list and the "
                             f"posting page confirms it).\n{link}\n\nResume prepared: {row.get('resume') or 'n/a'}",
                             email=False)
            results.append((who, "SUBMITTED"))
        elif not row.get("reminded") and _minutes_since(row["booked"], now) >= REMIND_AFTER_MINUTES:
            tracker.mark_reminded(row["number"])
            minutes = int(_minutes_since(row["booked"], now))
            if row.get("agent_submitted"):
                reporter.enqueue("needs_you", f"#{row['number']} could not be confirmed on LinkedIn: {row['title']} at {row['company']}",
                                 f"The agent clicked Submit {minutes} minutes ago, but LinkedIn does not show this "
                                 f"application as applied. It may not have gone through.\n{link}\n\n"
                                 f"Please check it. (This message is sent once.)", email=False)
            else:
                reporter.enqueue("needs_you", f"#{row['number']} still waiting for you: {row['title']} at {row['company']}",
                                 f"This application was prepared {minutes} minutes ago and LinkedIn does not show it as "
                                 f"submitted yet.\n{link}\n\nNote: {row.get('note', '')}\n(This reminder is sent once.)", email=False)
            results.append((who, f"reminder sent ({minutes} minutes)"))
        else:
            results.append((who, "still waiting"))
    return results


def run() -> int:
    """Read LinkedIn once and reconcile. Exit codes: 0 ok, 10 LinkedIn sign-in needed."""
    from playwright.sync_api import sync_playwright
    if not any(r["status"] == "AWAITING RAJA" for r in tracker.rows()):
        return 0
    if not discovery.STATE.exists():
        print("No agent session. Run: python manual_login.py agent")
        return 10
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            ctx = browser.new_context(storage_state=str(discovery.STATE), viewport={"width": 1500, "height": 1400})
            page = ctx.new_page()
            try:
                entries = read_applied(page)
            except LoginNeeded:
                print("LinkedIn session expired. Run: python manual_login.py agent")
                return 10
            if entries:
                ids = {e["job_id"] for e in entries}
            else:
                logging.warning("The Applied tab could not be read (no entries); reconcile changed nothing")
                ids = None

            def confirm(job_id: str) -> bool:
                posting = discovery.read_detail(page, f"https://www.linkedin.com/jobs/view/{job_id}/")
                return bool(posting.get("already_applied"))

            for who, outcome in process(ids, confirm):
                print(f"{who}: {outcome}")
        finally:
            browser.close()
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    sys.exit(run())
