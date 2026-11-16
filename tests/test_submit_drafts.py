import os
import tempfile
import unittest

os.environ.setdefault("AGENT_DATA_DIR", tempfile.mkdtemp())

from agent import submit_drafts, tracker  # noqa: E402
from agent.store import read_json, write_json  # noqa: E402


class FakeOps:
    def __init__(self, detail=None, result=None, open_error=None, fill_error=None):
        self._detail = {"loaded": True, "already_applied": False, "not_accepting": False, **(detail or {})}
        self._result = result or {"status": "submitted", "confirmed": True, "reason": "sent", "steps": [], "learned": []}
        self.open_error, self.fill_error, self.calls = open_error, fill_error, []

    def detail(self, url):
        self.calls.append("detail")
        return self._detail

    def open(self, url):
        self.calls.append("open")
        if self.open_error:
            raise self.open_error

    def fill(self, title, company, submit):
        self.calls.append(f"fill(submit={submit})")
        if self.fill_error:
            raise self.fill_error
        return self._result

    def close(self, save):
        self.calls.append(f"close(save={save})")
        return "save" if save else "discard"


def book(number=3, status="AWAITING RAJA", company="RSP Group", title="Chief AI Officer", job="4490720859"):
    tracker.book(number, company, title, job, status, url=f"https://www.linkedin.com/jobs/view/{job}/")


class ProcessRow(unittest.TestCase):
    def setUp(self):
        write_json("tracker.json", [])
        write_json("outbox.json", [])
        book()
        self.row = tracker.rows()[0]

    def test_a_submitted_draft_is_reported_as_submitted_and_left_for_reconcile_to_settle(self):
        ops = FakeOps()
        out = submit_drafts.process_row(self.row, ops, submit=True)
        self.assertIn("SUBMITTED", out)
        self.assertEqual(ops.calls, ["detail", "open", "fill(submit=True)"])
        self.assertEqual(tracker.rows()[0]["status"], "AWAITING RAJA")      # only the reconcile step settles it
        self.assertEqual(read_json("outbox.json", []), [])                  # and only it sends the 'applied' report

    def test_an_unconfirmed_click_says_so_plainly(self):
        out = submit_drafts.process_row(self.row, FakeOps(result={"status": "submitted", "confirmed": False,
                                                                  "reason": "", "steps": [], "learned": []}), submit=True)
        self.assertIn("did not show a confirmation", out)

    def test_a_dry_run_never_submits_and_keeps_the_draft(self):
        ops = FakeOps(result={"status": "ready", "reason": "x", "steps": [], "learned": []})
        out = submit_drafts.process_row(self.row, ops, submit=False)
        self.assertIn("nothing submitted", out)
        self.assertEqual(ops.calls, ["detail", "open", "fill(submit=False)", "close(save=True)"])

    def test_a_new_question_stops_it_keeps_the_draft_and_asks_raja(self):
        res = {"status": "paused", "reason": "no basis", "question": "Do you hold a CISSP?", "steps": [], "learned": []}
        ops = FakeOps(result=res)
        out = submit_drafts.process_row(self.row, ops, submit=True)
        self.assertIn("stopped again", out)
        self.assertEqual(ops.calls[-1], "close(save=True)")
        (ev,) = read_json("outbox.json", [])
        self.assertEqual(ev["kind"], "question")
        self.assertIn("Do you hold a CISSP?", ev["body"])

    def test_a_reopened_draft_is_never_discarded_whatever_goes_wrong(self):
        for kwargs in ({"fill_error": RuntimeError("selector vanished")},
                       {"result": {"status": "skipped", "reason": "risk appetite", "steps": [], "learned": []}},
                       {"result": {"status": "paused", "reason": "r", "question": "q", "steps": [], "learned": []}},
                       {"result": {"status": "ready", "reason": "r", "steps": [], "learned": []}}):
            ops = FakeOps(**kwargs)
            submit_drafts.process_row(self.row, ops, submit=True)
            self.assertNotIn("close(save=False)", ops.calls, kwargs)           # discarding would delete saved progress
            self.assertIn("close(save=True)", ops.calls, kwargs)

    def test_nothing_is_opened_when_it_is_already_applied_closed_or_unloaded(self):
        for detail, text in (({"already_applied": True}, "already shows it as applied"),
                             ({"not_accepting": True}, "no longer accepts"),
                             ({"loaded": False}, "did not load")):
            ops = FakeOps(detail=detail)
            out = submit_drafts.process_row(self.row, ops, submit=True)
            self.assertIn(text, out)
            self.assertEqual(ops.calls, ["detail"])

    def test_a_draft_that_cannot_be_reopened_is_left_alone(self):
        ops = FakeOps(open_error=RuntimeError("no Easy Apply or Continue control"))
        out = submit_drafts.process_row(self.row, ops, submit=True)
        self.assertIn("could not reopen", out)
        self.assertEqual(ops.calls, ["detail", "open"])                          # never reached fill, nothing clicked


class SelectRows(unittest.TestCase):
    def setUp(self):
        write_json("tracker.json", [])
        book(1, "SUBMITTED", "A", "VP", "111")
        book(3, "AWAITING RAJA", "B", "CAIO", "333")
        book(5, "AWAITING RAJA", "C", "Head", "555")
        book(6, "DO NOT ATTEMPT", "D", "CTO", "666")

    def test_only_awaiting_rows_that_were_named_are_selected(self):
        rows, skipped = submit_drafts.select_rows([3, 5])
        self.assertEqual([r["number"] for r in rows], [3, 5])
        self.assertEqual(skipped, [])

    def test_unnamed_submitted_unknown_and_do_not_attempt_rows_are_never_touched(self):
        rows, skipped = submit_drafts.select_rows([1, 6, 99, 3])
        self.assertEqual([r["number"] for r in rows], [3])                       # #5 was not named, so it is not here
        self.assertEqual(len(skipped), 3)
        self.assertTrue(any("is SUBMITTED" in s for s in skipped))
        self.assertTrue(any("is DO NOT ATTEMPT" in s for s in skipped))
        self.assertTrue(any("no such application" in s for s in skipped))


if __name__ == "__main__":
    unittest.main()
