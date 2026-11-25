"""Applier: works ONE queued posting per run.

    python -m agent.applier

Claims the oldest eligible posting, checks it is still open and not already applied to, and fills the Easy
Apply form to its final review page. In supervised mode (the default) it SAVES the form as a draft on
LinkedIn and never clicks Submit: the application is booked in the tracker as AWAITING RAJA and reported, and
Raja submits it himself. With submit mode on (set_submit_mode) it clicks Submit itself, after the safety checks.

Exit codes follow the scheduler's convention: 0 = ok (including 'nothing to do'), 10 = LinkedIn sign-in needed.
"""
import logging
import sys
import time

from . import dup_gate, next_number, outreach, queue_tool, questions, reconcile, reporter, resumes, tracker
from .store import read_json, write_json

MODE_FILE = "applier_mode.json"


def submit_mode() -> bool:
    """True when the applier submits applications itself (set by Raja on 2026-11-16). The default, with no
    setting saved, is the cautious one: fill the form, save a draft, and stop for him."""
    return bool(read_json(MODE_FILE, {}).get("submit", False))


def set_submit_mode(on: bool) -> None:
    write_json(MODE_FILE, {"submit": bool(on)})

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
    reporter.enqueue(kind, subject, body, needs_action=True, email=(kind != "question"))   # questions: WhatsApp only


def _offer_outreach(item: dict, detail: dict) -> None:
    """If the posting names a job poster, draft an invite and ask Raja to approve the exact text.

    Nothing is sent here. Only the person tagged 'Job poster' gets a draft, never other team members.
    """
    poster, url = detail.get("poster"), detail.get("poster_url")
    if not (poster and url) or outreach.find("invite", url, item["job_id"]):
        return
    d = outreach.create("invite", item["job_id"], poster, url, item["company"], item["title"])
    outreach.announce(d)


def _link(item) -> str:
    return item.get("url") or f"https://www.linkedin.com/jobs/view/{item['job_id']}/"


def _log_screen(ops, jid) -> None:
    try:
        snap = getattr(ops, "snapshot", None)
        if snap:
            logging.error(f"[{jid}] screen at failure: {snap(f'{jid}_{int(time.time())}')}")
    except Exception as e:
        logging.error(f"[{jid}] could not capture the screen: {e}")


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
            site_file = resumes.company_site_path(title)  # a file in resumes/, e.g. the SAP Director resume
            site_resume = site_file.name if site_file else ""
            _book_and_report(
                item, number, "paused", f"#{number} Company-site application: {title} at {company}",
                f"This posting applies on the company's own site, which the agent cannot fill yet.\n"
                f"Apply here: {_link(item)}\n"
                f"Resume to upload (from the resumes folder): {site_resume or 'none (no rule for this title)'}",
                "company-site application (not automated)", site_resume)
            return "paused: company site"

        ops.open_form(_link(item))
        auto = submit_mode()
        try:
            result = ops.fill(title, company=company, submit=auto)
        except Exception:
            _log_screen(ops, jid)                  # before closing: the screen as it was when it failed
            ops.close(save=False)
            raise

        if result["status"] == "skipped":
            ops.close(save=False)
            queue_tool.set_status(jid, DO_NOT_ATTEMPT)
            return f"skipped: {result['reason']}"  # skips are never reported

        if result["status"] == "submitted":
            # Submit was clicked. It is NOT reported as applied yet: only LinkedIn's own records (the Applied tab
            # AND the posting page) settle it, which agent.reconcile does and then sends the report.
            number = next_number.reserve()
            tracker.book(number, company, title, jid, AWAITING, url=_link(item), resume=resume or "",
                         note="submitted by the agent; waiting for LinkedIn's confirmation (the reconcile step settles it)",
                         extra={"agent_submitted": True})
            queue_tool.set_status(jid, AWAITING)
            _offer_outreach(item, detail)
            return "submitted: waiting for LinkedIn's confirmation"

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
            _offer_outreach(item, detail)
            return "ready: draft saved"
        if not result.get("question") and "refused to submit" in result["reason"]:
            _book_and_report(
                item, number, "needs_you", f"#{number} Could not submit: {title} at {company}",
                f"The form was filled, but a safety check stopped the agent from clicking Submit, so nothing was "
                f"sent.\n\nWhy: {result['reason']}\nThe draft is saved on LinkedIn; you can submit it yourself:\n"
                f"{_link(item)}\n\nWhat was filled:\n{steps}{tail}",
                f"paused: {result['reason']}", resume or "")
            return "paused: submit refused"
        if result.get("question"):
            row = tracker.book(number, company, title, jid, AWAITING, url=_link(item), resume=resume or "",
                               note=f"paused: {result['reason']}")
            queue_tool.set_status(jid, AWAITING)
            if not questions.ask_and_report(row, result, _link(item), steps, tail):
                reporter.enqueue("question", f"#{number} Needs your answer: {title} at {company}",
                                 f"The application stopped.\n\nWhy: {result['reason']}\n{_link(item)}",
                                 needs_action=True, email=False)
            return "paused: question"
        _book_and_report(
            item, number, "question", f"#{number} Needs your answer: {title} at {company}",
            f"The application stopped.\n\nWhy it stopped: {result['reason']}\n"
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
                             f"Three attempts failed. What happened (last attempt):\n{e}\n\nPosting: {_link(item)}\n\nReply to this message with \"try again\" to queue it once more.", email=False)
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

    def fill(self, title, company=None, submit=False):
        from . import easy_apply
        return easy_apply.run_form(self.page, title, allow_submit=submit, company=company)

    def snapshot(self, name):
        from . import easy_apply
        from .store import data_dir
        return easy_apply.snapshot(self.page, name, data_dir() / "failures")

    def close(self, save):
        from . import easy_apply
        return easy_apply.close_form(self.page, save=save)


def main(argv=None) -> int:
    import argparse
    import time
    from playwright.sync_api import sync_playwright
    from . import discovery

    ap = argparse.ArgumentParser()
    ap.add_argument("--set-submit", choices=("on", "off"),
                    help="on: the applier submits applications itself. off: it saves a draft and stops for Raja.")
    a = ap.parse_args(argv)
    if a.set_submit:
        set_submit_mode(a.set_submit == "on")
        print(f"Automatic submission is now {a.set_submit.upper()}.")
        return 0
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    try:
        if reconcile.run() == 10:        # notice applications that were submitted since the last run
            return 10
    except Exception as e:               # reconciling must never stop the applier from working
        logging.error(f"Reconcile failed: {e}")
    try:                                 # first finish applications whose question Raja has just answered
        if questions.answered_waiting():
            from . import submit_drafts
            if submit_drafts.finish_answered(submit=submit_mode()) == 10:
                return 10
    except Exception as e:
        logging.error(f"Finishing answered applications failed (the next run will retry): {e}")
    try:                                 # then the ones he asked, in chat, to be finished
        from . import submit_drafts
        if submit_drafts.requested():
            if submit_drafts.finish_requested(default_submit=submit_mode()) == 10:
                return 10
    except Exception as e:
        logging.error(f"Finishing the applications you asked for failed: {e}")
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
    if outcome.startswith("submitted"):
        time.sleep(10)                    # give LinkedIn a moment, then confirm on its own records right away
        try:
            if reconcile.run() == 10:
                return 10
        except Exception as e:
            logging.error(f"Reconcile after submit failed (the next run will retry): {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
