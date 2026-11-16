import contextlib
import io
import os
import tempfile
import unittest

os.environ.setdefault("AGENT_DATA_DIR", tempfile.mkdtemp())

from agent import outreach, outreach_sender  # noqa: E402
from agent.store import read_json, write_json  # noqa: E402

URL = "https://www.linkedin.com/in/codyarice/"


def reset():
    for f in ("outreach.json", "invite_watch.json", "outbox.json", "whatsapp_inbox.json", "approvals_seen.json"):
        write_json(f, [])


class Recording(unittest.TestCase):
    def setUp(self):
        reset()
        self.d = outreach.create("invite", "1", "Cody Rice", URL, "Acme", "VP of AI")
        outreach.approve(self.d["id"])
        self.item = outreach.get(self.d["id"])

    def status(self):
        return outreach.get(self.d["id"])["status"]

    def alerts(self):
        return [e for e in read_json("outbox.json", []) if e["kind"] == "needs_you"]

    def test_sent_is_recorded_and_watched_without_bothering_raja(self):
        outreach_sender._record(self.item, {"result": "sent", "reason": "pending"})
        self.assertEqual(self.status(), "SENT")
        self.assertEqual(len(outreach.watch_list()), 1)
        self.assertEqual(self.alerts(), [])      # sends are not reported (only applications and pauses are)

    def test_already_pending_counts_as_done_and_is_watched(self):
        outreach_sender._record(self.item, {"result": "already_pending", "reason": "x"})
        self.assertEqual((self.status(), len(outreach.watch_list())), ("SENT", 1))

    def test_already_connected_needs_no_invitation_and_no_alert(self):
        outreach_sender._record(self.item, {"result": "already_connected", "reason": "x"})
        self.assertEqual((self.status(), self.alerts()), ("FAILED", []))

    def test_an_abort_tells_raja_that_nothing_was_sent(self):
        outreach_sender._record(self.item, {"result": "aborted", "reason": "the page is 'X', not Cody Rice"})
        self.assertEqual(self.status(), "FAILED")
        a = self.alerts()
        self.assertEqual(len(a), 1)
        self.assertIn("NOT sent", a[0]["subject"])
        self.assertTrue(a[0]["subject"].endswith("updates needed"))

    def test_unsettled_and_note_less_sends_are_alerted_and_never_retried(self):
        for result in ("unsettled", "sent_without_note"):
            reset()
            d = outreach.create("invite", "1", "Cody Rice", URL, "Acme", "VP of AI")
            outreach.approve(d["id"])
            outreach_sender._record(outreach.get(d["id"]), {"result": result, "reason": "check it"})
            self.assertEqual(outreach.get(d["id"])["status"], "UNSETTLED")
            self.assertIsNone(outreach.next_approved())   # not eligible again
            self.assertEqual(len(self.alerts()), 1)


class BeforeTheBrowser(unittest.TestCase):
    """Cases that must be decided without ever opening LinkedIn."""

    def run_main(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = outreach_sender.main(list(args))
        return code, out.getvalue()

    def setUp(self):
        reset()

    def test_nothing_approved_means_nothing_happens(self):
        code, out = self.run_main()
        self.assertEqual(code, 0)
        self.assertIn("No approved invitation", out)

    def test_an_unapproved_draft_is_never_sent(self):
        d = outreach.create("invite", "1", "Cody Rice", URL, "Acme", "VP of AI")
        code, out = self.run_main("--id", d["id"])
        self.assertEqual(code, 0)
        self.assertIn("not approved", out)
        self.assertEqual(outreach.get(d["id"])["status"], "DRAFT")

    def test_rejected_and_sent_drafts_are_not_resent(self):
        d = outreach.create("invite", "1", "Cody Rice", URL, "Acme", "VP of AI")
        outreach.reject(d["id"])
        self.assertIn("not approved", self.run_main("--id", d["id"])[1])
        e = outreach.create("invite", "2", "Cody Rice", URL, "Acme", "VP of AI")
        outreach.approve(e["id"])
        outreach.mark(e["id"], "SENT")
        self.assertIn("not approved", self.run_main("--id", e["id"])[1])

    def test_whatsapp_approval_is_applied_at_the_start_of_a_run(self):
        d = outreach.create("invite", "1", "Cody Rice", URL, "Acme", "VP of AI")
        write_json("whatsapp_inbox.json", [{"ts": 1, "text": f"REJECT {d['id']}", "handled": False}])
        self.run_main()
        self.assertEqual(outreach.get(d["id"])["status"], "REJECTED")


class ManualDraft(unittest.TestCase):
    def test_creates_a_draft_that_waits_for_approval(self):
        reset()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = outreach.main(["draft", "--url", URL, "--name", "Cody Rice", "--company", "Acme",
                                  "--title", "VP of AI", "--no-applied-check"])
        self.assertEqual(code, 0)
        d = outreach.all_items()[0]
        self.assertEqual((d["status"], d["requires_applied"], d["poster_url"]), ("DRAFT", False, URL))
        ev = read_json("outbox.json", [])[0]
        self.assertEqual(ev["kind"], "draft")
        self.assertIn(d["text"], ev["body"])
        self.assertIn(f"APPROVE {d['id']}", ev["body"])
        self.assertIn("as soon as you approve", ev["body"])

    def test_a_custom_text_must_still_follow_the_voice_rules(self):
        reset()
        with self.assertRaises(ValueError):
            outreach.main(["draft", "--url", URL, "--name", "Cody Rice", "--company", "Acme", "--title", "VP",
                           "--text", "Thanks for your time."])
        self.assertEqual(outreach.all_items(), [])


class Reopen(unittest.TestCase):
    def setUp(self):
        reset()
        self.d = outreach.create("invite", "1", "Cody Rice", URL, "Acme", "VP of AI")

    def test_a_failed_approved_draft_can_be_retried_with_the_same_text(self):
        outreach.approve(self.d["id"])
        outreach.mark(self.d["id"], "FAILED", error="no dialog")
        ok, _ = outreach.reopen(self.d["id"])
        got = outreach.get(self.d["id"])
        self.assertEqual((ok, got["status"], got["error"], got["text"]), (True, "APPROVED", None, self.d["text"]))

    def test_sent_unsettled_rejected_and_unapproved_drafts_are_never_reopened(self):
        self.assertFalse(outreach.reopen(self.d["id"])[0])          # never approved
        outreach.approve(self.d["id"])
        for status in ("SENT", "UNSETTLED"):
            outreach.mark(self.d["id"], status)
            self.assertFalse(outreach.reopen(self.d["id"])[0], status)
            self.assertEqual(outreach.get(self.d["id"])["status"], status)
        outreach.mark(self.d["id"], "FAILED")
        outreach._update(self.d["id"], sent="2026-11-16T00:00:00")   # something did go out
        self.assertFalse(outreach.reopen(self.d["id"])[0])
        self.assertFalse(outreach.reopen("zzzzzzzz")[0])


if __name__ == "__main__":
    unittest.main()
