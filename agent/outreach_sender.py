"""Outreach sender: applies Raja's replies, then sends ONE approved invitation per run.

    python -m agent.outreach_sender                         # normal run (headless), as the scheduler runs it
    python -m agent.outreach_sender --test --id <draft id>  # visible browser; fills the note, then WAITS for you
                                                            # to type SEND in this terminal before clicking Send

Exit codes follow the scheduler's convention: 0 = ok (including nothing to do), 10 = LinkedIn sign-in needed.
Only invitations Raja approved are sent, with exactly the text he approved. Follow-up messages to
accepted invitations come with the acceptance check, which is not built yet.
"""
import argparse
import logging
import sys

from . import approvals, discovery, invite, message, outreach, reporter


class _LoginNeeded(Exception):
    pass


def pick_ready(candidates: list, applied) -> dict:
    """The first approved item that may be sent now, or None.

    Candidates arrive oldest approval first. An invitation whose note says "I applied" is only ready once
    `applied(job_id)` is true (LinkedIn shows the job as Applied); until then it is skipped, so it cannot
    hold up newer approved items behind it. Messages and invitations not tied to an application are always ready.
    """
    for cand in candidates:
        if cand["kind"] == "invite" and cand.get("requires_applied", True) and not applied(cand["job_id"]):
            print(f"Waiting: LinkedIn does not show {cand['title']} at {cand['company']} as Applied yet "
                  "(the note says 'I applied').")
            continue
        return cand
    return None


def _confirm_in_terminal(text: str) -> bool:
    print("\nThe note box now holds exactly this text:\n")
    print(text)
    print(f"\n({len(text)} characters). Look at the browser window: is this the right person?")
    return input("Type SEND to send it, anything else to cancel: ").strip() == "SEND"


def _record(item: dict, result: dict) -> str:
    """Update the draft and alert Raja when he must know. Returns a one-line outcome."""
    r, why = result["result"], result["reason"]
    who = f"{item['poster']} at {item['company']}"
    if r == "sent":
        outreach.mark(item["id"], "SENT")
        if item["kind"] == "invite":   # a delivered message needs no further watching
            outreach.watch(item["poster_url"], item["poster"], item["job_id"], item["company"], item["title"],
                           applied=item.get("requires_applied", True))
        return f"sent to {who}"
    if r == "already_pending":
        outreach.mark(item["id"], "SENT", error="an invitation was already pending")
        outreach.watch(item["poster_url"], item["poster"], item["job_id"], item["company"], item["title"],
                        applied=item.get("requires_applied", True))
        return f"already pending for {who}"
    if r == "already_connected":
        outreach.mark(item["id"], "FAILED", error="already connected; no invitation needed")
        return f"already connected to {who}"
    if r == "aborted":
        outreach.mark(item["id"], "FAILED", error=why)
        reporter.enqueue("needs_you", f"Invite to {who} was NOT sent",
                         f"Nothing was sent. Reason: {why}\nDraft {item['id']}:\n{item['poster_url']}\n\n"
                         "Reply to this message with \"try again\" to send it once more.")
        return f"aborted: {why}"
    # sent_without_note / unsettled: something may have gone out, so never retry
    outreach.mark(item["id"], "UNSETTLED", error=why)
    reporter.enqueue("needs_you", f"Check LinkedIn: invite to {who} is unsettled",
                     f"{why}\nPlease look at your sent invitations and at {item['poster_url']}\n"
                     f"Draft {item['id']} will not be retried.")
    return f"unsettled: {why}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true", help="visible browser and a terminal confirmation before Send")
    ap.add_argument("--id", help="send this approved draft instead of the oldest approved one")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    approvals.process_whatsapp()
    try:
        approvals.process_gmail()
    except Exception as e:  # Gmail being unavailable must not stop WhatsApp-approved sends
        logging.warning(f"Could not read email replies: {e}")

    if a.id:
        wanted = outreach.get(a.id)
        if wanted is None or wanted["status"] != "APPROVED":
            print(f"Draft {a.id} is not approved (status: {wanted['status'] if wanted else 'not found'}). Approve it first.")
            return 0
        candidates = [wanted]
    else:
        candidates = outreach.approved()
        if not candidates:
            print("No approved invitation or message to send.")
            return 0
    if not discovery.STATE.exists():
        print("No agent session. Run: python manual_login.py agent")
        return 10

    from playwright.sync_api import sync_playwright
    item = result = None
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not a.test)
        try:
            ctx = browser.new_context(storage_state=str(discovery.STATE), viewport={"width": 1500, "height": 1000})
            page = ctx.new_page()
            def applied(job_id: str) -> bool:
                posting = discovery.read_detail(page, f"https://www.linkedin.com/jobs/view/{job_id}/")
                if "/login" in page.url or "authwall" in page.url:
                    raise _LoginNeeded()
                return bool(posting.get("already_applied"))

            try:
                item = pick_ready(candidates, applied)
            except _LoginNeeded:
                print("LinkedIn session expired. Run: python manual_login.py agent")
                return 10
            if item is None:
                return 0
            send = invite.send_invite if item["kind"] == "invite" else message.send_message
            print(f"Sending the approved {item['kind']} to {item['poster']}...")
            result = send(page, item["poster_url"], item["poster"], item["text"],
                          confirm=_confirm_in_terminal if a.test else None)
            if "/login" in page.url or "authwall" in page.url:
                print("LinkedIn session expired. Run: python manual_login.py agent")
                return 10
        finally:
            browser.close()
    print("Result:", result["result"], "|", result["reason"])
    print("Recorded:", _record(item, result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
