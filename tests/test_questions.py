import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("AGENT_DATA_DIR", tempfile.mkdtemp())

from agent import answers, approvals, questions, queue_tool, submit_drafts, tracker  # noqa: E402
from agent.store import write_json  # noqa: E402

YN = {"question": "Have you led an AI program?", "options": ["Yes", "No"], "reason": "no basis"}
ROW = {"number": 41, "job_id": "900", "company": "Acme", "title": "CAIO"}


class BankCase(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self.bank = Path(d) / "bank.txt"
        self.bank.write_text("Standing answers\r\nLearned\r\n- How many years? → 15\r\n", encoding="utf-8", newline="")
        write_json(questions.FILE, [])
        write_json(tracker.FILE, [])
        self.addCleanup(lambda: [write_json(f, []) for f in (questions.FILE, tracker.FILE, "outbox.json")])
        self.patch = mock.patch.object(answers, "ANSWER_BANK", self.bank)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.sched = mock.patch("agent.scheduler.run_soon", return_value=True)
        self.sched.start()
        self.addCleanup(self.sched.stop)


class SetAnswer(BankCase):
    def test_replaces_the_same_question_and_keeps_crlf(self):
        self.assertEqual(answers.set_answer("How many years?", "20"), "replaced")
        raw = self.bank.read_bytes().decode()
        self.assertIn("- How many years? → 20\r\n", raw)
        self.assertNotIn("→ 15", raw)

    def test_a_different_question_is_added_not_merged(self):
        self.assertEqual(answers.set_answer("How many years with Python?", "9"), "added")
        self.assertEqual(answers.exact_answer("How many years?", self.bank.read_text(encoding="utf-8")), "15")


class Asking(BankCase):
    def test_buttons_for_yes_no_and_skip(self):
        rec, created = questions.ask(ROW, YN, "http://x")
        self.assertTrue(created)
        self.assertEqual([t for _, t in questions.buttons(rec)], ["Yes", "No", "Skip"])
        self.assertEqual(questions.buttons(rec)[0][0], f"ANSWER {rec['id']} YES")

    def test_same_open_question_is_not_asked_twice(self):
        questions.ask(ROW, YN, "http://x")
        self.assertFalse(questions.ask(ROW, YN, "http://x")[1])

    def test_free_text_gets_only_skip_button(self):
        rec, _ = questions.ask(ROW, {"question": "Years?", "options": []}, "u")
        self.assertEqual([t for _, t in questions.buttons(rec)], ["Skip"])


class Answering(BankCase):
    def test_yes_is_saved_and_the_question_waits_to_be_finished(self):
        rec, _ = questions.ask(ROW, YN, "u")
        ok, _ = questions.answer(rec["id"], "yes")
        self.assertTrue(ok)
        self.assertEqual(answers.exact_answer(YN["question"], self.bank.read_text(encoding="utf-8")), "Yes")
        self.assertEqual([q["id"] for q in questions.answered_waiting()], [rec["id"]])

    def test_a_value_that_is_not_an_option_is_refused(self):
        rec, _ = questions.ask(ROW, YN, "u")
        ok, msg = questions.answer(rec["id"], "maybe")
        self.assertFalse(ok)
        self.assertIn("not one of", msg)
        self.assertEqual(questions.get(rec["id"])["status"], "OPEN")

    def test_zero_years_is_refused_by_the_safety_rule(self):
        rec, _ = questions.ask(ROW, {"question": "How many years of experience do you have?"}, "u")
        self.assertFalse(questions.answer(rec["id"], "0")[0])

    def test_answering_twice_is_refused(self):
        rec, _ = questions.ask(ROW, YN, "u")
        questions.answer(rec["id"], "No")
        self.assertFalse(questions.answer(rec["id"], "Yes")[0])

    def test_skip_settles_the_row_and_holds_the_queue_item(self):
        tracker.book(41, "Acme", "CAIO", "900", "AWAITING RAJA", url="u")
        rec, _ = questions.ask(ROW, YN, "u")
        self.assertTrue(questions.answer(rec["id"], "SKIP")[0])
        row = next(r for r in tracker.rows() if r["number"] == 41)
        self.assertEqual(row["status"], "DO NOT ATTEMPT")
        self.assertEqual(questions.get(rec["id"])["status"], "SKIPPED")


class Commands(BankCase):
    def test_a_tapped_button_and_a_typed_reply_both_parse(self):
        self.assertEqual(approvals.parse("ANSWER 1a2b3c4d YES"), {"action": "ANSWER", "id": "1a2b3c4d", "text": "YES"})
        self.assertEqual(approvals.parse("answer 1a2b3c4d 20 years")["text"], "20 years")

    def test_apply_answers_the_question(self):
        rec, _ = questions.ask(ROW, YN, "u")
        msg = approvals.apply(approvals.parse(f"ANSWER {rec['id']} NO"))
        self.assertIn("Saved 'No'", msg)


class Finishing(BankCase):
    class Ops:
        def __init__(self, status="submitted"):
            self.status = status
            self.submits = []

        def detail(self, url):
            return {"loaded": True}

        def open(self, url):
            pass

        def fill(self, title, company, submit):
            self.submits.append(submit)
            return {"status": self.status, "confirmed": True, "reason": "", "steps": [], "learned": []}

        def close(self, save):
            return "save"

    def test_an_answered_question_is_finished_and_marked_done(self):
        tracker.book(41, "Acme", "CAIO", "900", "AWAITING RAJA", url="u")
        rec, _ = questions.ask(ROW, YN, "u")
        questions.answer(rec["id"], "Yes")
        ops = self.Ops()
        with mock.patch.object(submit_drafts.reconcile, "run", return_value=0):
            submit_drafts.finish_answered(submit=True, ops=ops)
        self.assertEqual(ops.submits, [True])
        self.assertTrue(questions.get(rec["id"])["done"])
        self.assertEqual(questions.answered_waiting(), [])

    def test_with_automatic_submission_off_it_stops_at_review(self):
        tracker.book(42, "Beta", "VP AI", "901", "AWAITING RAJA", url="u")
        rec, _ = questions.ask({**ROW, "number": 42, "job_id": "901"}, YN, "u")
        questions.answer(rec["id"], "Yes")
        ops = self.Ops(status="ready")
        submit_drafts.finish_answered(submit=False, ops=ops)
        self.assertEqual(ops.submits, [False])

    def test_a_row_already_settled_by_hand_is_just_closed_out(self):
        tracker.book(43, "Gamma", "CTO", "902", "SUBMITTED", url="u")
        rec, _ = questions.ask({**ROW, "number": 43, "job_id": "902"}, YN, "u")
        questions.answer(rec["id"], "No")
        ops = self.Ops()
        submit_drafts.finish_answered(submit=True, ops=ops)
        self.assertEqual(ops.submits, [])
        self.assertTrue(questions.get(rec["id"])["done"])


if __name__ == "__main__":
    unittest.main()
