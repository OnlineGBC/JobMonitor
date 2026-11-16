import os
import tempfile
import unittest

os.environ.setdefault("AGENT_DATA_DIR", tempfile.mkdtemp())

from agent import applier, discovery, next_number, queue_tool, tracker  # noqa: E402
from agent.store import read_json, write_json  # noqa: E402

LINK = "https://www.linkedin.com/jobs/view/42/"


class FakeOps:
    """Stands in for the browser. It has no submit method at all: the applier cannot submit."""

    def __init__(self, detail=None, fill=None, save_result="save", open_error=None):
        self._detail = {"loaded": True, "not_accepting": False, "already_applied": False,
                        "apply_type": "Easy Apply", **(detail or {})}
        self._fill = fill or {"status": "ready", "reason": "reached the final review page",
                              "steps": ["page 1/2: nothing to fill"], "learned": []}
        self.save_result, self.open_error, self.calls = save_result, open_error, []

    def detail(self, url):
        self.calls.append("detail")
        return self._detail

    def open_form(self, url):
        self.calls.append("open_form")
        if self.open_error:
            raise self.open_error

    def fill(self, title):
        self.calls.append("fill")
        return self._fill

    def close(self, save):
        self.calls.append(f"close(save={save})")
        return self.save_result if save else "discard"


class ApplierTests(unittest.TestCase):
    def setUp(self):
        for f in ("pending_survivors.json", "tracker.json", "outbox.json"):
            write_json(f, [])
        next_number.set_next(1)
        queue_tool.add({"job_id": "42", "title": "VP of AI", "company": "Acme", "workplace": "Remote"})
        self.item = queue_tool.claim()

    def status(self):
        return next((i["status"] for i in queue_tool.all_items() if i["job_id"] == "42"), None)

    def outbox(self):
        return read_json("outbox.json", [])

    def test_ready_form_is_saved_booked_and_reported_but_never_submitted(self):
        ops = FakeOps()
        self.assertEqual(applier.process_item(self.item, ops), "ready: draft saved")
        self.assertEqual(ops.calls, ["detail", "open_form", "fill", "close(save=True)"])
        row = tracker.rows()[0]
        self.assertEqual((row["number"], row["status"], row["company"]), (1, "AWAITING RAJA", "Acme"))
        self.assertEqual(self.status(), "AWAITING RAJA")
        ev = self.outbox()[0]
        self.assertEqual(ev["kind"], "paused")
        self.assertTrue(ev["subject"].startswith("#1 Ready to submit: VP of AI at Acme"))
        self.assertTrue(ev["subject"].endswith("updates needed"))
        self.assertIn(LINK, ev["body"])
        self.assertIn("click Submit yourself", ev["body"])

    def test_question_is_reported_word_for_word(self):
        q = "Do you hold an active AWS Solutions Architect Professional certification?"
        ops = FakeOps(fill={"status": "paused", "reason": "credential not in your answers", "question": q,
                            "steps": ["page 1/3: nothing to fill"], "learned": []})
        self.assertEqual(applier.process_item(self.item, ops), "paused: question")
        ev = self.outbox()[0]
        self.assertEqual(ev["kind"], "question")
        self.assertIn(q, ev["body"])
        self.assertEqual(tracker.rows()[0]["status"], "AWAITING RAJA")
        self.assertIn("close(save=True)", ops.calls)  # the partial draft is kept

    def test_hard_blocker_is_skipped_silently(self):
        ops = FakeOps(fill={"status": "skipped", "reason": "form asks about risk appetite", "question": "q",
                            "steps": [], "learned": []})
        self.assertTrue(applier.process_item(self.item, ops).startswith("skipped"))
        self.assertEqual(self.status(), "DO NOT ATTEMPT")
        self.assertEqual(self.outbox(), [])           # skips are never reported
        self.assertEqual(tracker.rows(), [])          # and use no application number
        self.assertIn("close(save=False)", ops.calls)
        self.assertEqual(next_number.reserve(), 1)

    def test_duplicate_closed_and_already_applied_leave_the_queue_quietly(self):
        tracker.book(7, "Acme", "VP of AI", "1", "SUBMITTED")
        ops = FakeOps()
        self.assertTrue(applier.process_item(self.item, ops).startswith("duplicate"))
        self.assertEqual(ops.calls, [])               # not even opened
        self.assertIsNone(self.status())
        for detail, expected in (({"not_accepting": True}, "closed"), ({"already_applied": True}, "already applied")):
            write_json("tracker.json", [])
            queue_tool.add({"job_id": "42", "title": "VP of AI", "company": "Acme", "workplace": "Remote"})
            item = queue_tool.claim()
            ops = FakeOps(detail=detail)
            self.assertEqual(applier.process_item(item, ops), expected)
            self.assertNotIn("open_form", ops.calls)
            self.assertIsNone(self.status())
        self.assertEqual(self.outbox(), [])

    def test_company_site_applications_pause_with_a_link(self):
        ops = FakeOps(detail={"apply_type": "Apply"})
        self.assertEqual(applier.process_item(self.item, ops), "paused: company site")
        self.assertNotIn("open_form", ops.calls)
        self.assertIn(LINK, self.outbox()[0]["body"])
        self.assertEqual(tracker.rows()[0]["note"], "company-site application (not automated)")

    def test_failures_retry_then_ask_for_help_without_using_a_number(self):
        for attempt in (1, 2):
            ops = FakeOps(open_error=RuntimeError("button missing"))
            self.assertIn(f"attempt {attempt}", applier.process_item(self.item, ops))
            self.assertEqual(self.status(), "QUEUED")
            self.assertNotIn("close(save=False)", ops.calls)  # the form never opened: nothing to close
            self.item = queue_tool.claim()
        self.assertEqual(applier.process_item(self.item, FakeOps(open_error=RuntimeError("button missing"))),
                         "failed three times: reported")
        self.assertEqual(self.status(), "AWAITING RAJA")
        self.assertEqual(self.outbox()[0]["kind"], "needs_you")
        self.assertEqual(tracker.rows(), [])
        self.assertEqual(next_number.reserve(), 1)

    def test_a_crash_while_filling_discards_the_open_form(self):
        class Crashing(FakeOps):
            def fill(self, title):
                self.calls.append("fill")
                raise RuntimeError("selector vanished")
        ops = Crashing()
        self.assertIn("failed", applier.process_item(self.item, ops))
        self.assertEqual(ops.calls[-1], "close(save=False)")  # never leaves a half-filled form behind
        self.assertEqual(self.outbox(), [])

    def test_unconfirmed_save_is_a_failure_not_a_success(self):
        self.assertIn("failed", applier.process_item(self.item, FakeOps(save_result="none")))
        self.assertEqual(self.outbox(), [])
        self.assertEqual(tracker.rows(), [])

    def test_login_needed_releases_the_claim_and_propagates(self):
        class Expired(FakeOps):
            def detail(self, url):
                raise applier.LoginNeeded()
        with self.assertRaises(applier.LoginNeeded):
            applier.process_item(self.item, Expired())
        self.assertEqual(self.status(), "QUEUED")


class PostingFlags(unittest.TestCase):
    def test_already_applied_flag(self):
        lines = ["Acme", "VP of AI", "United States · 1 day ago", "Applied 2 hours ago", "Remote", "About the job"]
        d = discovery.parse_posting("VP of AI | Acme | LinkedIn", lines, "")
        self.assertTrue(d["already_applied"])
        lines = ["Acme", "VP of AI", "United States · 1 day ago", "Remote", "Easy Apply", "About the job",
                 "Applied machine learning experience is a plus"]  # the word in the description does not count
        self.assertFalse(discovery.parse_posting("VP of AI | Acme | LinkedIn", lines, "")["already_applied"])


if __name__ == "__main__":
    unittest.main()
