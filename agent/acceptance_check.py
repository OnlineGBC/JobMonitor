"""Acceptance check (daily): has anyone accepted an invitation? If so, draft a follow-up for approval.

    python -m agent.acceptance_check

For each invitation on the watch list it opens the person's profile (read-only) and looks at their top card:
  * 1st-degree connection  -> accepted: a follow-up MESSAGE draft is created and shown to Raja word for word.
                              Nothing is sent until he approves that exact text.
  * invitation still Pending -> nothing to do.
  * neither                 -> counted as a miss; after 3 daily misses in a row the watch is closed
                              (the invitation was declined, withdrawn or expired). No follow-up is drafted.
Exit codes follow the scheduler's convention: 0 = ok, 10 = LinkedIn sign-in needed.
"""
import logging
import sys

from . import discovery, invite, outreach


class LoginNeeded(Exception):
    pass


def process(ops) -> list:
    """Check every open watch entry. `ops.state(url, name)` returns {'wrong_person', 'degree', 'pending'}."""
    results = []
    for w in outreach.watch_list():
        if w.get("accepted") or w.get("closed"):
            continue
        who = f"{w['poster']} at {w['company']}"
        try:
            s = ops.state(w["poster_url"], w["poster"])
        except LoginNeeded:
            raise
        except Exception as e:
            logging.error(f"{who}: could not check: {e}")
            results.append((who, "error"))
            continue
        if s["wrong_person"]:
            logging.warning(f"{who}: the profile page is not {w['poster']}; leaving the watch unchanged")
            results.append((who, "wrong page"))
        elif s["degree"] == "1st":
            outreach.mark_accepted(w["poster_url"], w["job_id"])
            text = outreach.followup_text(w["poster"], w["title"], w["company"], applied=w.get("applied", True))
            draft = outreach.create("message", w["job_id"], w["poster"], w["poster_url"], w["company"],
                                    w["title"], text=text, requires_applied=False)
            outreach.announce(draft)
            results.append((who, "accepted: follow-up drafted"))
        elif s["pending"]:
            outreach.clear_misses(w["poster_url"], w["job_id"])
            results.append((who, "still pending"))
        else:
            closed = outreach.note_miss(w["poster_url"], w["job_id"])
            results.append((who, "no longer pending: watch closed" if closed else "no longer pending: will check again"))
    return results


class LinkedInOps:
    def __init__(self, page):
        self.page = page

    def state(self, url: str, name: str) -> dict:
        self.page.goto(url, wait_until="domcontentloaded", timeout=45000)
        self.page.wait_for_timeout(4000)
        if "/login" in self.page.url or "authwall" in self.page.url:
            raise LoginNeeded()
        s = invite.card_state(self.page)
        return {"wrong_person": not invite.name_matches(s["title"], name), "degree": s["degree"],
                "pending": s["pending"]}


def main() -> int:
    from playwright.sync_api import sync_playwright
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    if not any(not (w.get("accepted") or w.get("closed")) for w in outreach.watch_list()):
        print("No invitations to check.")
        return 0
    if not discovery.STATE.exists():
        print("No agent session. Run: python manual_login.py agent")
        return 10
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            ctx = browser.new_context(storage_state=str(discovery.STATE), viewport={"width": 1500, "height": 1000})
            results = process(LinkedInOps(ctx.new_page()))
        except LoginNeeded:
            print("LinkedIn session expired. Run: python manual_login.py agent")
            return 10
        finally:
            browser.close()
    for who, outcome in results:
        print(f"{who}: {outcome}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
