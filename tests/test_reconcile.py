import os
import tempfile
import unittest
from datetime import datetime, timedelta

os.environ.setdefault("AGENT_DATA_DIR", tempfile.mkdtemp())

from agent import approvals, next_number, queue_tool, reconcile, reporter, tracker  # noqa: E402
from agent.store import read_json, write_json  # noqa: E402


def reset():
    for f in ("tracker.json", "outbox.json", "pending_survivors.json", "outreach.json"):
        write_json(f, [])
    next_number.set_next(1)


def book(number=1, job="4490239950", status="AWAITING RAJA", company="Acme", title="VP of AI"):
    tracker.book(number, company, title, job, status, url=f"https://www.linkedin.com/jobs/view/{job}/",
                 resume="RajaGopalanVPEnterpriseAI_DataScience.pdf", note="filled to the review page")


class Reconcile(unittest.TestCase):
    def setUp(self):
        reset()

    def reports(self):
        return read_json("outbox.json", [])

    def test_both_records_agree_so_the_row_is_settled_removed_and_reported(self):
        book()
        queue_tool.add({"job_id": "4490239950", "title": "VP of AI", "company": "Acme", "workplace": "Remote"})
        out = reconcile.process({"4490239950"}, lambda j: True)
        self.assertEqual(out[0][1], "SUBMITTED")
        row = tracker.rows()[0]
        self.assertEqual(row["status"], "SUBMITTED")
        self.assertIn("does not say", row["note"])            # never claims who submitted
        self.assertIn("Applied tab", row["note"])
        self.assertEqual(queue_tool.all_items(), [])
        (ev,) = self.reports()
        self.assertEqual(ev["kind"], "applied")
        self.assertIn("https://www.linkedin.com/jobs/view/4490239950/", ev["body"])
        self.assertNotIn("does not say who submitted", ev["body"])
        self.assertNotIn("you submitted", ev["body"].lower())
        self.assertIn("Job poster: not named", ev["body"])
        self.assertIn("New York time", ev["body"])

    def test_on_the_applied_list_but_the_posting_page_disagrees_changes_nothing(self):
        book()
        out = reconcile.process({"4490239950"}, lambda j: False)
        self.assertIn("does not confirm", out[0][1])
        self.assertEqual((tracker.rows()[0]["status"], self.reports()), ("AWAITING RAJA", []))

    def test_matching_is_on_the_exact_job_number_never_company_or_title(self):
        book(job="4490239950", company="Acme", title="VP of AI")
        # the same company and title reposted under another job number is a different job
        reconcile.process({"4999999999"}, lambda j: True)
        self.assertEqual((tracker.rows()[0]["status"], self.reports()), ("AWAITING RAJA", []))

    def test_an_unreadable_applied_tab_changes_nothing_and_sends_no_reminder(self):
        book()
        late = datetime.now(reporter.NY) + timedelta(hours=5)
        out = reconcile.process(None, lambda j: True, now=late)
        self.assertIn("not readable", out[0][1])
        self.assertEqual((tracker.rows()[0]["status"], self.reports()), ("AWAITING RAJA", []))

    def test_one_reminder_after_60_minutes_and_never_a_second(self):
        book()
        soon = datetime.now(reporter.NY) + timedelta(minutes=59)
        self.assertEqual(reconcile.process(set(), lambda j: True, now=soon)[0][1], "still waiting")
        self.assertEqual(self.reports(), [])
        later = datetime.now(reporter.NY) + timedelta(minutes=61)
        out = reconcile.process(set(), lambda j: True, now=later)
        self.assertIn("reminder sent", out[0][1])
        (ev,) = self.reports()
        self.assertEqual(ev["kind"], "needs_you")
        self.assertTrue(ev["subject"].endswith("updates needed"))
        self.assertIn("sent once", ev["body"])
        self.assertTrue(tracker.rows()[0]["reminded"])
        reconcile.process(set(), lambda j: True, now=later + timedelta(hours=3))   # an hour, then hours more
        self.assertEqual(len(self.reports()), 1)

    def test_a_reminder_is_not_sent_for_something_already_submitted(self):
        book()
        later = datetime.now(reporter.NY) + timedelta(hours=2)
        reconcile.process({"4490239950"}, lambda j: True, now=later)
        kinds = [e["kind"] for e in self.reports()]
        self.assertEqual(kinds, ["applied"])                     # the applied report, no reminder

    def test_only_awaiting_rows_are_touched_and_a_settled_row_is_never_reopened(self):
        book(1, "111", status="SUBMITTED")
        book(2, "222", status="DO NOT ATTEMPT")
        out = reconcile.process({"111", "222"}, lambda j: True)
        self.assertEqual(out, [])
        self.assertEqual([r["status"] for r in tracker.rows()], ["SUBMITTED", "DO NOT ATTEMPT"])
        self.assertEqual(self.reports(), [])

    def test_several_rows_are_handled_independently(self):
        book(1, "111", title="A")
        book(2, "222", title="B")
        book(3, "333", title="C")
        out = dict(reconcile.process({"111", "333"}, lambda j: j == "111"))
        self.assertEqual(out["#1 A at Acme"], "SUBMITTED")
        self.assertIn("does not confirm", out["#3 C at Acme"])
        self.assertEqual(out["#2 B at Acme"], "still waiting")
        self.assertEqual([r["status"] for r in tracker.rows()], ["SUBMITTED", "AWAITING RAJA", "AWAITING RAJA"])

    def test_the_pending_digest_shows_how_long_an_application_has_waited(self):
        book()
        self.assertIn("awaiting you (0m)", approvals.pending_digest())
        rows = tracker.rows()
        rows[0]["booked"] = (datetime.now(reporter.NY) - timedelta(minutes=135)).isoformat(timespec="seconds")
        write_json("tracker.json", rows)
        self.assertIn("awaiting you (2h 15m)", approvals.pending_digest())


APPLIED_PAGE = """<!doctype html><html><body><nav><a href="/feed/">Home</a><a href="/jobs/">Jobs</a></nav>
<main><h1>Job tracker</h1>
<ul>
 <li><a href="https://www.linkedin.com/jobs/view/4490239950/?trk=x">Chief Innovation Officer</a>
     <div>Bridgepoint Group · United States (Remote)</div><div>Applied 1h ago</div>
     <a href="https://www.linkedin.com/jobs/view/4490239950/">View job</a></li>
 <li><a href="https://www.linkedin.com/jobs/view/4488695719/">VP Client Partner - FinTech</a>
     <div>EXL · San Francisco Bay Area (Remote)</div><div>Applied 3d ago</div></li>
 <li><a href="https://www.linkedin.com/company/acme/">Acme</a><div>a company link, not a job</div></li>
</ul></main></body></html>"""


class ReadApplied(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise unittest.SkipTest("playwright not installed")
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def read(self, html):
        page = self.browser.new_page()
        page.set_content(html)
        out = reconcile.read_applied(page, navigate=False)
        page.close()
        return out

    def test_reads_exact_job_numbers_newest_first_once_each(self):
        entries = self.read(APPLIED_PAGE)
        self.assertEqual([e["job_id"] for e in entries], ["4490239950", "4488695719"])   # no duplicates, no company link
        self.assertIn("Bridgepoint Group", entries[0]["text"])
        self.assertIn("Applied 1h ago", entries[0]["text"])

    def test_a_page_with_no_entries_reads_as_empty_which_run_treats_as_unreadable(self):
        self.assertEqual(self.read("<html><body><main><h1>Job tracker</h1></main></body></html>"), [])


class PosterLine(unittest.TestCase):
    def test_names_the_poster_and_the_invite_state(self):
        from agent import outreach
        d = outreach.create("invite", "777", "Jane Roe", "https://www.linkedin.com/in/jane/", "Acme", "Director of AI")
        line = reconcile._poster_line("777")
        self.assertIn("Jane Roe", line)
        self.assertIn(f"Invite {d['id']}: waiting for your approval", line)
        self.assertIn("message follows once they accept", line)


if __name__ == "__main__":
    unittest.main()
