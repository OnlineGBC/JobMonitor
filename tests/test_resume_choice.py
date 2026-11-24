import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("AGENT_DATA_DIR", tempfile.mkdtemp())

from agent import approvals, easy_apply, questions, reporter, resumes, router, submit_drafts, tracker, whatsapp  # noqa: E402
from agent.store import write_json  # noqa: E402
from tests.test_router import Base, client  # noqa: E402
from tests.test_reply_to import Session  # noqa: E402


class ResumeRules(unittest.TestCase):
    def setUp(self):
        write_json(resumes.RULES_FILE, {})
        self.addCleanup(lambda: write_json(resumes.RULES_FILE, {}))

    def test_four_resumes_are_offered(self):
        self.assertEqual(list(resumes.CHOICES), ["Director", "Executive Director", "VP", "Chief"])
        self.assertIn("ExecDir", resumes.EXEC_DIR)

    def test_an_executive_director_title_uses_the_executive_director_resume(self):
        self.assertEqual(resumes.resume_name("Executive Director, AI"), resumes.EXEC_DIR)
        self.assertEqual(resumes.resume_name("Director of AI"), resumes.DIRECTOR)
        self.assertEqual(resumes.resume_name("Chief AI Officer"), resumes.CHIEF)

    def test_a_remembered_choice_applies_to_that_exact_title_only(self):
        self.assertIsNone(resumes.resume_name("Sr. Manager of RPA"))
        resumes.set_title_rule("Sr. Manager of RPA", resumes.DIRECTOR)
        self.assertEqual(resumes.resume_name("Sr. Manager of RPA"), resumes.DIRECTOR)
        self.assertEqual(resumes.resume_name("sr manager of rpa!"), resumes.DIRECTOR)   # same title, different spelling
        self.assertIsNone(resumes.resume_name("Sr. Manager of AI"))

    def test_the_form_asks_which_resume_when_there_is_no_rule(self):
        r = easy_apply._run_form(None, "Sr. Manager of RPA", False, None, None, {"steps": [], "stage": ""})
        self.assertEqual(r["status"], "paused")
        self.assertEqual(r["qtype"], "resume")
        self.assertEqual(r["options"], list(resumes.CHOICES))
        self.assertIn("Sr. Manager of RPA", r["question"])


class ResumeQuestion(Base):
    def setUp(self):
        super().setUp()
        write_json(resumes.RULES_FILE, {})
        self.addCleanup(lambda: write_json(resumes.RULES_FILE, {}))
        row = {"number": 102, "job_id": "4494375735", "company": "Insight Global", "title": "Sr. Manager of RPA"}
        r = easy_apply._run_form(None, row["title"], False, None, None, {"steps": [], "stage": ""})
        self.q, _ = questions.ask(row, r, "link")

    def test_it_is_a_resume_question_with_a_four_row_pick_list(self):
        self.assertEqual(self.q["type"], "resume")
        rows = questions.pick_list(self.q)
        self.assertEqual([t for _, t in rows], ["Director", "Executive Director", "VP", "Chief", "Skip this job"])
        self.assertEqual(rows[1][0], f"ANSWER {self.q['id']} Executive Director")
        self.assertTrue(all(len(t) <= whatsapp.LIST_ROW_TITLE_MAX for _, t in rows))
        self.assertIsNone(questions.pick_list(self.q_yn))

    def test_choosing_remembers_the_resume_for_that_title_and_queues_the_application(self):
        with mock.patch("agent.scheduler.run_soon", return_value=True) as soon:
            ok, msg = questions.answer(self.q["id"], "Executive Director")
        self.assertTrue(ok)
        self.assertEqual(resumes.resume_name("Sr. Manager of RPA"), resumes.EXEC_DIR)
        self.assertEqual(questions.get(self.q["id"])["status"], "ANSWERED")
        self.assertIn(self.q["id"], [q["id"] for q in questions.answered_waiting()])
        soon.assert_called_once_with("applier")

    def test_a_name_that_is_not_one_of_the_four_is_refused(self):
        ok, _ = questions.answer(self.q["id"], "Intern")
        self.assertFalse(ok)
        self.assertIsNone(resumes.resume_name("Sr. Manager of RPA"))

    def test_the_model_maps_use_director_resume_to_the_option(self):
        c = client(actions=[{"type": "answer", "id": self.q["id"], "value": "director"}], comment=False, ask=None)
        out = router.route("Use director resume", client=c, about=f"question {self.q['id']}")
        self.assertEqual(out["commands"], [f"ANSWER {self.q['id']} Director"])


class PickListSending(unittest.TestCase):
    def setUp(self):
        write_json(whatsapp.REFS, {})
        write_json(whatsapp.STATE, {"last_inbound": 1000})
        p = mock.patch.object(whatsapp.vault, "require", return_value="x")
        p.start()
        self.addCleanup(p.stop)

    def test_a_list_message_has_a_button_and_rows(self):
        posted = []

        class S(Session):
            def post(self, url, **kw):
                posted.append(kw["json"])
                return super().post(url, **kw)
        rows = [("ANSWER q Director", "Director"), ("ANSWER q VP", "VP")]
        self.assertTrue(whatsapp.send_list("Which resume?", "Choose resume", rows, S(), now=1100))
        i = posted[0]["interactive"]
        self.assertEqual((i["type"], i["action"]["button"]), ("list", "Choose resume"))
        self.assertEqual(i["action"]["sections"][0]["rows"][0], {"id": "ANSWER q Director", "title": "Director"})

    def test_limits_and_a_closed_window(self):
        with self.assertRaises(ValueError):
            whatsapp.send_list("x", "Choose resume", [("a", "t" * 30)], Session(), now=1100)
        with self.assertRaises(ValueError):
            whatsapp.send_list("x", "Choose resume", [], Session(), now=1100)
        self.assertFalse(whatsapp.send_list("x", "Choose resume", [("a", "A")], Session(), now=10_000_000))

    def test_the_outbox_sends_a_pick_list_event_as_a_list(self):
        write_json(reporter.OUTBOX, [])
        reporter.enqueue("question", "s", "b", wa_text="Which resume?", pick_list=[("ANSWER q Director", "Director")])
        calls = []
        reporter.process(send_email=lambda *a: None, send_whatsapp=lambda t: calls.append(("text", t)) or True,
                         send_buttons=lambda *a: calls.append(("buttons",)) or True,
                         send_list=lambda t, label, rows: calls.append(("list", t, label, rows)) or True)
        self.assertEqual(calls, [("list", "Which resume?", "Choose resume", [("ANSWER q Director", "Director")])])


class FinishFromChat(Base):
    def setUp(self):
        super().setUp()
        write_json(submit_drafts.REQUESTS, [])
        self.addCleanup(lambda: write_json(submit_drafts.REQUESTS, []))
        tracker.book(102, "Insight Global", "Sr. Manager of RPA", "4494375735", "AWAITING RAJA", url="u", resume="",
                     note="paused")

    def test_the_command_forms(self):
        self.assertEqual(approvals.parse("FINISH 102"), {"action": "FINISH", "id": "102", "text": None})
        self.assertEqual(approvals.parse("finish 102 submit"), {"action": "FINISH", "id": "102", "text": "SUBMIT"})

    def test_a_finish_request_is_recorded_and_the_applier_is_woken(self):
        with mock.patch("agent.scheduler.run_soon", return_value=True) as soon:
            msg = approvals.apply(approvals.parse("FINISH 102 SUBMIT"))
        self.assertEqual(submit_drafts.requested(), [{"number": 102, "submit": True}])
        self.assertIn("submitted", msg)
        soon.assert_called_once_with("applier")

    def test_only_a_waiting_application_can_be_finished(self):
        msg = approvals.apply(approvals.parse("FINISH 999"))
        self.assertIn("no such application", msg)
        self.assertEqual(submit_drafts.requested(), [])

    def test_the_model_can_finish_an_application_and_submit_only_when_told_to(self):
        c = client(actions=[{"type": "finish_application", "number": 102, "submit": False}], comment=False, ask=None)
        self.assertEqual(router.route("go ahead", client=c)["commands"], ["FINISH 102"])
        c = client(actions=[{"type": "finish_application", "number": 102, "submit": True}], comment=False, ask=None)
        self.assertEqual(router.route("submit it", client=c)["commands"], ["FINISH 102 SUBMIT"])
        c = client(actions=[{"type": "finish_application", "number": 555, "submit": True}], comment=False, ask=None)
        self.assertEqual(router.route("submit it", client=c)["commands"], [])

    def test_finish_requested_runs_each_group_with_its_own_submit_setting(self):
        submit_drafts.request_finish(102, False)
        with mock.patch.object(submit_drafts, "_finish_rows", return_value=0) as run:
            self.assertEqual(submit_drafts.finish_requested(default_submit=False, ops=object()), 0)
        self.assertEqual([(c.args[0][0]["number"], c.args[3]) for c in run.call_args_list], [(102, False)])
        self.assertEqual(submit_drafts.requested(), [])           # taken, never repeated
        submit_drafts.request_finish(102, True)
        with mock.patch.object(submit_drafts, "_finish_rows", return_value=0) as run:
            submit_drafts.finish_requested(default_submit=False, ops=object())
        self.assertEqual([c.args[3] for c in run.call_args_list], [True])


class ModelIsToldWhatItCanDo(unittest.TestCase):
    def test_the_prompt_lists_every_action_and_says_when_not_to_file_a_comment(self):
        for needle in ("WHAT THE AGENT CAN DO FROM THIS CHAT", "finish_application", "retry_invite",
                       "one-off instruction", "cannot be done from the chat"):
            self.assertIn(needle.lower(), router.PROMPT.lower(), needle)


if __name__ == "__main__":
    unittest.main()
