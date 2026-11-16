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

    def fill(self, title, company=None, submit=False):
        self.calls.append("fill")
        self.fill_args = {"company": company, "submit": submit}
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

    def test_company_site_director_report_names_the_sap_resume_on_disk(self):
        write_json("pending_survivors.json", [])
        write_json("tracker.json", [])
        queue_tool.add({"job_id": "77", "title": "Director of AI", "company": "Globex", "workplace": "Remote"})
        item = queue_tool.claim()
        self.assertEqual(applier.process_item(item, FakeOps(detail={"apply_type": "Apply"})), "paused: company site")
        body = self.outbox()[-1]["body"]
        self.assertIn("RajaGopalanDir-SystemArchitectEnterpriseAI_SAP.docx", body)
        self.assertNotIn("DataScience", body)  # the LinkedIn-only Director resume cannot be uploaded elsewhere
        self.assertEqual(tracker.rows()[-1]["resume"], "RajaGopalanDir-SystemArchitectEnterpriseAI_SAP.docx")

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
            def fill(self, title, company=None, submit=False):
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


class AutoSubmit(unittest.TestCase):
    """The applier's automatic-submission mode. The default (nothing saved) is the cautious draft-and-stop."""

    def setUp(self):
        for f in ("pending_survivors.json", "tracker.json", "outbox.json", "outreach.json"):
            write_json(f, [])
        write_json("applier_mode.json", {})
        next_number.set_next(1)
        queue_tool.add({"job_id": "42", "title": "VP of AI", "company": "Acme", "workplace": "Remote"})
        self.item = queue_tool.claim()
        self.addCleanup(lambda: write_json("applier_mode.json", {}))

    def outbox(self):
        return read_json("outbox.json", [])

    def test_the_default_is_draft_and_stop_and_the_mode_is_saved_between_runs(self):
        self.assertFalse(applier.submit_mode())
        applier.set_submit_mode(True)
        self.assertTrue(applier.submit_mode())
        applier.set_submit_mode(False)
        self.assertFalse(applier.submit_mode())
        self.assertEqual(applier.main(["--set-submit", "on"]), 0)
        self.assertTrue(applier.submit_mode())
        applier.main(["--set-submit", "off"])
        self.assertFalse(applier.submit_mode())

    def test_with_the_mode_off_the_form_is_filled_with_submit_disabled(self):
        ops = FakeOps()
        applier.process_item(self.item, ops)
        self.assertEqual(ops.fill_args, {"company": "Acme", "submit": False})
        self.assertEqual(self.outbox()[0]["kind"], "paused")               # the old 'Ready to submit' report

    def test_with_the_mode_on_a_submitted_application_waits_for_linkedin_to_settle_it(self):
        applier.set_submit_mode(True)
        ops = FakeOps(fill={"status": "submitted", "reason": "sent", "confirmed": False, "steps": [], "learned": []},
                      detail={"poster": "Cody Rice", "poster_url": "https://www.linkedin.com/in/codyarice/"})
        out = applier.process_item(self.item, ops)
        self.assertEqual(ops.fill_args, {"company": "Acme", "submit": True})
        self.assertEqual(out, "submitted: waiting for LinkedIn's confirmation")
        row = tracker.rows()[0]
        self.assertEqual((row["status"], row["agent_submitted"]), ("AWAITING RAJA", True))   # not SUBMITTED until LinkedIn says so
        self.assertIn("waiting for LinkedIn", row["note"])
        self.assertNotIn("close(save=True)", ops.calls)                    # the dialog is already closed; nothing to save
        self.assertNotIn("close(save=False)", ops.calls)
        self.assertEqual([e["kind"] for e in self.outbox()], ["draft"])    # no 'applied' report from the applier itself;
        self.assertEqual(outreach_item_kinds(), ["invite"])                # only the outreach draft for the job poster

    def test_a_refused_submit_is_reported_as_could_not_submit_and_the_draft_is_kept(self):
        applier.set_submit_mode(True)
        ops = FakeOps(fill={"status": "paused", "reason": "refused to submit: the dialog does not name Acme",
                            "question": None, "steps": ["page 1/2: nothing to fill"], "learned": []})
        self.assertEqual(applier.process_item(self.item, ops), "paused: submit refused")
        (ev,) = self.outbox()
        self.assertEqual(ev["kind"], "needs_you")
        self.assertIn("Could not submit", ev["subject"])
        self.assertIn("nothing was sent", ev["body"])
        self.assertIn("close(save=True)", ops.calls)                       # the draft is kept
        self.assertEqual(tracker.rows()[0]["status"], "AWAITING RAJA")

    def test_questions_and_hard_blockers_still_stop_it_in_auto_mode(self):
        applier.set_submit_mode(True)
        ops = FakeOps(fill={"status": "paused", "reason": "no basis", "question": "Do you hold a CISSP?", "steps": [], "learned": []})
        self.assertEqual(applier.process_item(self.item, ops), "paused: question")
        self.assertEqual(self.outbox()[0]["kind"], "question")
        write_json("pending_survivors.json", [])
        queue_tool.add({"job_id": "43", "title": "VP of AI", "company": "Beta", "workplace": "Remote"})
        item2 = queue_tool.claim()
        ops2 = FakeOps(fill={"status": "skipped", "reason": "risk appetite", "question": "q", "steps": [], "learned": []})
        self.assertTrue(applier.process_item(item2, ops2).startswith("skipped"))
        self.assertEqual(len(tracker.rows()), 1)                           # a skip books nothing


def outreach_item_kinds():
    from agent import outreach
    return [i["kind"] for i in outreach.all_items()]


class AgentSubmittedReminder(unittest.TestCase):
    def setUp(self):
        write_json("tracker.json", [])
        write_json("outbox.json", [])

    def test_an_unconfirmed_agent_submission_gets_its_own_one_time_message(self):
        from datetime import datetime, timedelta
        from agent import reconcile, reporter
        tracker.book(1, "Acme", "VP of AI", "9", "AWAITING RAJA", url="u", extra={"agent_submitted": True})
        later = datetime.now(reporter.NY) + timedelta(minutes=61)
        reconcile.process(set(), lambda j: True, now=later)
        reconcile.process(set(), lambda j: True, now=later + timedelta(hours=2))
        (ev,) = read_json("outbox.json", [])
        self.assertIn("could not be confirmed on LinkedIn", ev["subject"])
        self.assertIn("may not have gone through", ev["body"])
        self.assertNotIn("prepared", ev["body"])                           # not the 'waiting for you' wording

    def test_a_confirmed_agent_submission_is_settled_with_the_applied_report(self):
        from agent import reconcile
        tracker.book(1, "Acme", "VP of AI", "9", "AWAITING RAJA", url="u", extra={"agent_submitted": True})
        out = reconcile.process({"9"}, lambda j: True)
        self.assertEqual(out[0][1], "SUBMITTED")
        self.assertEqual([e["kind"] for e in read_json("outbox.json", [])], ["applied"])


class TrackerSettle(unittest.TestCase):
    def setUp(self):
        write_json("tracker.json", [])

    def test_an_awaiting_row_can_be_settled_once_with_a_reason(self):
        tracker.book(1, "Acme", "VP of AI", "9", "AWAITING RAJA", note="filled to the review page")
        row = tracker.settle(1, "SUBMITTED", "LinkedIn Applied list shows it")
        self.assertEqual((row["number"], row["status"], row["note"]), (1, "SUBMITTED", "LinkedIn Applied list shows it"))
        self.assertEqual(row["previous_note"], "filled to the review page")
        self.assertIn("settled", row)
        with self.assertRaises(ValueError):                 # a settled row is never moved again
            tracker.settle(1, "DO NOT ATTEMPT", "x")

    def test_unknown_number_or_status_is_refused(self):
        tracker.book(1, "Acme", "VP of AI", "9", "AWAITING RAJA")
        with self.assertRaises(ValueError):
            tracker.settle(2, "SUBMITTED", "x")
        with self.assertRaises(ValueError):
            tracker.settle(1, "MAYBE", "x")
        self.assertEqual(tracker.rows()[0]["status"], "AWAITING RAJA")


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
