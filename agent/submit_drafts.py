"""Finish, and optionally submit, SPECIFIC saved Easy Apply drafts, on Raja's explicit instruction.

    python -m agent.submit_drafts 3 5 6              # answer, review and SUBMIT application numbers 3, 5 and 6
    python -m agent.submit_drafts 3 5 6 --dry-run    # same, but stop at the review page and keep the draft

Only tracker rows that are AWAITING RAJA and named on the command line are touched; nothing else is ever
submitted. For each one it reopens the saved draft ("Continue"), answers from the standing answers (including
answers Raja saved for exact questions), and stops without submitting if anything is unanswered. At the review
page it checks the dialog is for the right company and that this is really the final page, then clicks Submit.

Settling is NOT done here: afterwards agent.reconcile confirms on LinkedIn's own records (the Applied tab AND the
posting page), marks the row SUBMITTED, and sends the "applied" report. A draft is always saved, never discarded,
if anything goes wrong, because discarding would delete the progress that was saved.
"""
import argparse
import logging
import sys

from . import discovery, questions, reconcile, reporter, tracker


def process_row(row: dict, ops, submit: bool) -> str:
    """Handle one AWAITING RAJA row. `ops` is the browser side. Returns a one-line outcome."""
    link = row.get("url") or f"https://www.linkedin.com/jobs/view/{row['job_id']}/"
    who = f"#{row['number']} {row['title']} at {row['company']}"
    try:
        detail = ops.detail(link)
        if not detail.get("loaded"):
            return f"{who}: the posting page did not load; left as it is"
        if detail.get("already_applied"):
            return f"{who}: LinkedIn already shows it as applied (the reconcile step will settle it)"
        if detail.get("not_accepting"):
            return f"{who}: the posting no longer accepts applications; nothing to submit"
        ops.open(link)
    except Exception as e:
        return f"{who}: could not reopen the draft ({e}); left as it is"

    try:
        result = ops.fill(row["title"], row["company"], submit)
    except Exception as e:
        ops.close(save=True)                    # never discard a saved draft because of an error
        return f"{who}: failed while filling ({e}); the draft was kept"

    status = result["status"]
    if status == "submitted":
        note = "LinkedIn confirmed it" if result.get("confirmed") else "LinkedIn did not show a confirmation yet"
        return f"{who}: SUBMITTED ({note}); the reconcile step will verify it on your Applied list"
    ops.close(save=True)
    if status == "ready":
        return f"{who}: dry run reached the review page with every answer filled; draft kept, nothing submitted"
    if status == "paused":
        if not (result.get("question") and questions.ask_and_report(row, result, link)):
            reporter.enqueue("question", f"#{row['number']} Still needs your answer: {row['title']} at {row['company']}",
                             f"Reopening the draft stopped again.\n\nQuestion (word for word):\n  {result.get('question') or '(none)'}"
                             f"\n\nWhy: {result['reason']}\n{link}", email=False)
        return f"{who}: stopped again, {result['reason']} (question: {result.get('question')}); draft kept"
    return f"{who}: {status}: {result.get('reason')}; draft kept, nothing submitted"


class LinkedInOps:
    def __init__(self, page):
        self.page = page

    def detail(self, url):
        d = discovery.read_detail(self.page, url)
        if "/login" in self.page.url or "authwall" in self.page.url:
            raise RuntimeError("LinkedIn session expired; run: python manual_login.py agent")
        return d

    def open(self, url):
        from . import easy_apply
        easy_apply.open_easy_apply(self.page, url)

    def fill(self, title, company, submit):
        from . import easy_apply
        return easy_apply.run_form(self.page, title, allow_submit=submit, company=company)

    def close(self, save):
        from . import easy_apply
        return easy_apply.close_form(self.page, save=save)


def select_rows(numbers: list) -> tuple:
    """(rows to process, messages about numbers that were skipped). Only AWAITING RAJA rows qualify."""
    by_number = {r["number"]: r for r in tracker.rows()}
    rows, skipped = [], []
    for n in numbers:
        r = by_number.get(n)
        if r is None:
            skipped.append(f"#{n}: no such application")
        elif r["status"] != "AWAITING RAJA":
            skipped.append(f"#{n}: is {r['status']}, not AWAITING RAJA, so it is not touched")
        else:
            rows.append(r)
    return rows, skipped


def finish_answered(submit: bool, ops=None) -> int:
    """Finish the applications whose question Raja has answered (see agent.questions).

    Each one is reopened from its saved draft and filled from the standing answers (which now hold his answer).
    With `submit` False it stops at the review page and keeps the draft. A question is marked done once it has
    been attempted: if the form stops on ANOTHER question, that one is asked about separately. Returns 10 if
    LinkedIn needs a new sign-in, else 0.
    """
    waiting = questions.answered_waiting()
    rows, _ = select_rows(sorted({q["number"] for q in waiting}))
    live = {r["number"] for r in rows}
    for q in waiting:
        if q["number"] not in live:          # already settled some other way (e.g. submitted by hand)
            questions.mark_done(q["id"])
    if not rows:
        return 0
    if ops is None:
        if not discovery.STATE.exists():
            return 10
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                ctx = browser.new_context(storage_state=str(discovery.STATE), viewport={"width": 1500, "height": 1000})
                return _finish_rows(rows, waiting, LinkedInOps(ctx.new_page()), submit)
            finally:
                browser.close()
    return _finish_rows(rows, waiting, ops, submit)


def _finish_rows(rows, waiting, ops, submit) -> int:
    for row in rows:
        outcome = process_row(row, ops, submit=submit)
        logging.info(outcome)
        print(outcome)
        for q in waiting:
            if q["number"] == row["number"]:
                questions.mark_done(q["id"])
        if "SUBMITTED" in outcome:
            reporter.enqueue("notice", f"#{row['number']} Submitted after your answer: {row['title']} at {row['company']}",
                             f"Your answer was used and the application was submitted. The reconcile step will confirm it "
                             f"on LinkedIn's own records and send the 'applied' report.")
    if submit:
        reconcile.run()
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("numbers", nargs="+", type=int, help="application numbers (the # in the reports) to finish")
    ap.add_argument("--dry-run", action="store_true", help="stop at the review page; submit nothing")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    rows, skipped = select_rows(a.numbers)
    for s in skipped:
        print(s)
    if not rows:
        return 0
    if not discovery.STATE.exists():
        print("No agent session. Run: python manual_login.py agent")
        return 10
    from playwright.sync_api import sync_playwright
    print(("DRY RUN: nothing will be submitted. " if a.dry_run else "SUBMITTING: ") + ", ".join(f"#{r['number']}" for r in rows))
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            ctx = browser.new_context(storage_state=str(discovery.STATE), viewport={"width": 1500, "height": 1000})
            ops = LinkedInOps(ctx.new_page())
            for row in rows:
                print(process_row(row, ops, submit=not a.dry_run))
        finally:
            browser.close()
    if not a.dry_run:
        print("--- checking LinkedIn's own records ---")
        reconcile.run()
        for r in tracker.rows():
            if r["number"] in [x["number"] for x in rows]:
                print(f"#{r['number']} {r['company']}: {r['status']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
