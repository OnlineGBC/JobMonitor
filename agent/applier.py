"""Applier: works ONE queued posting per run, in supervised mode.

    python -m agent.applier

Claims the oldest eligible posting, checks it is still open and not already applied to, fills the Easy
Apply form to its final review page and SAVES it as a draft on LinkedIn. It never clicks Submit: the
application is booked in the tracker as AWAITING RAJA and reported, and Raja submits it himself.

Exit codes follow the scheduler's convention: 0 = ok (including 'nothing to do'), 10 = LinkedIn sign-in needed.
"""
import logging
import sys

from . import dup_gate, next_number, queue_tool, reporter, resumes, tracker

MAX_ATTEMPTS = 3
AWAITING, DO_NOT_ATTEMPT = "AWAITING RAJA", "DO NOT ATTEMPT"


class LoginNeeded(Exception):
    pass


def _book_and_report(item, number, kind, subject, body, note, resume):
    """Book the paused application, then report it. Every paused application needs Raja to step in."""
    tracker.book(number, item["company"], item["title"], item["job_id"], AWAITING,
                 url=item.get("url") or f"https://www.linkedin.com/jobs/view/{item['job_id']}/",
                 resume=resume, note=note)
    queue_tool.set_status(item["job_id"], AWAITING)
    reporter.enqueue(kind, subject, body, needs_action=True)


def _link(item) -> str:
    return item.get("url") or f"https://www.linkedin.com/jobs/view/{item['job_id']}/"


def process_item(item: dict, ops) -> str:
    """Handle one claimed queue item. `ops` supplies the browser side. Returns what happened."""
    jid, company, title = item["job_id"], item["company"], item["title"]
    try:
        dup, why = dup_gate.is_duplicate(company, title)
        if dup:
            queue_tool.remove(jid)
            return f"duplicate ({why})"

        detail = ops.detail(_link(item))
        if not detail.get("loaded"):
            raise RuntimeError("posting page did not load")
        if detail.get("not_accepting"):
            queue_tool.remove(jid)
            return "closed"
        if detail.get("already_applied"):
            queue_tool.remove(jid)
            return "already applied"

        resume = resumes.resume_name(title)
        number = None

        if detail.get("apply_type") != "Easy Apply":
            number = next_number.reserve()
            _book_and_report(
                item, number, "paused", f"#{number} Company-site application: {title} at {company}",
                f"This posting applies on the company's own site, which the agent cannot fill yet.\n"
                f"Apply here: {_link(item)}\nSuggested resume: {resume or 'none (no rule for this title)'}",
                "company-site application (not automated)", resume or "")
            return "paused: company site"

        ops.open_form(_link(item))
        try:
            result = ops.fill(title)
        except Exception:
            ops.close(save=False)
            raise

        if result["status"] == "skipped":
            ops.close(save=False)
            queue_tool.set_status(jid, DO_NOT_ATTEMPT)
            return f"skipped: {result['reason']}"  # skips are never reported

        saved = ops.close(save=True)
        if saved != "save":
            raise RuntimeError(f"could not confirm the draft was saved (close returned '{saved}')")

        number = next_number.reserve()
        steps = "\n".join(f"  - {s}" for s in result["steps"])
        learned = "".join(f"\n  - {q} → {v}" for q, v in result["learned"])
        tail = (f"\n\nAnswered from your earlier applications (now saved as standing answers):{learned}"
                if learned else "")
        if result["status"] == "ready":
            _book_and_report(
                item, number, "paused", f"#{number} Ready to submit: {title} at {company}",
                f"The form is filled to the review page and saved as a draft on LinkedIn.\n"
                f"Open it, check it, and click Submit yourself:\n{_link(item)}\n\nWhat was filled:\n{steps}{tail}",
                "filled to the review page; draft saved on LinkedIn", resume or "")
            return "ready: draft saved"
        _book_and_report(
            item, number, "question", f"#{number} Needs your answer: {title} at {company}",
            f"The application stopped at a question it must not guess.\n\nQuestion (word for word):\n"
            f"  {result.get('question') or '(none)'}\n\nWhy it stopped: {result['reason']}\n"
            f"A partial draft is saved on LinkedIn:\n{_link(item)}\n\nWhat was filled so far:\n{steps}{tail}",
            f"paused: {result['reason']}", resume or "")
        return "paused: question"

    except LoginNeeded:
        queue_tool.release(jid)
        raise
    except Exception as e:
        attempts = queue_tool.bump_attempts(jid)
        logging.error(f"[{jid}] attempt {attempts} failed: {e}")
        if attempts >= MAX_ATTEMPTS:
            queue_tool.set_status(jid, AWAITING)
            reporter.enqueue("needs_you", f"Applier could not process: {title} at {company}",
                             f"Three attempts failed. Last error:\n{e}\n\nPosting: {_link(item)}")
            return "failed three times: reported"
        queue_tool.release(jid)
        return f"failed (attempt {attempts}), will retry"


class LinkedInOps:
    """The browser side, over one Playwright page."""

    def __init__(self, page):
        self.page = page

    def detail(self, url):
        from . import discovery
        d = discovery.read_detail(self.page, url)
        if "/login" in self.page.url or "authwall" in self.page.url:
            raise LoginNeeded()
        return d

    def open_form(self, url):
        from . import easy_apply
        easy_apply.open_easy_apply(self.page, url)

    def fill(self, title):
        from . import easy_apply
        return easy_apply.run_form(self.page, title)

    def close(self, save):
        from . import easy_apply
        return easy_apply.close_form(self.page, save=save)


def main() -> int:
    from playwright.sync_api import sync_playwright
    from . import discovery

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    item = queue_tool.claim()
    if item is None:
        print("Nothing queued.")
        return 0
    print(f"Claimed: {item['company']} | {item['title']}")
    if not discovery.STATE.exists():
        queue_tool.release(item["job_id"])
        print("No agent session. Run: python manual_login.py agent")
        return 10
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            ctx = browser.new_context(storage_state=str(discovery.STATE), viewport={"width": 1500, "height": 1000})
            outcome = process_item(item, LinkedInOps(ctx.new_page()))
        except LoginNeeded:
            print("LinkedIn session expired. Run: python manual_login.py agent")
            return 10
        finally:
            browser.close()
    print("Outcome:", outcome)
    return 0


if __name__ == "__main__":
    sys.exit(main())
