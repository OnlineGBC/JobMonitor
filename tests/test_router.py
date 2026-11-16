import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("AGENT_DATA_DIR", tempfile.mkdtemp())

from agent import answers, approvals, decisions, feedback, ledger, queue_tool, questions, reporter, router, tracker  # noqa: E402
from agent.store import read_json, write_json  # noqa: E402

FILES = ((decisions.FILE, []), (questions.FILE, []), ("pending_confirmations.json", []), (reporter.OUTBOX, []),
         (queue_tool.FILE, []), (tracker.FILE, []), (ledger.FILE, {}), ("whatsapp_inbox.json", []),
         (feedback.LOG, []), (feedback.RULES, []))


def job(jid, title, company):
    return {"job_id": jid, "job_ids": [jid], "title": title, "company": company, "location": "Texas (Remote)",
            "url": f"https://www.linkedin.com/jobs/view/{jid}", "summary": "fit"}


def client(**reply):
    r = SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(reply))], stop_reason="end_turn")
    return SimpleNamespace(messages=SimpleNamespace(create=mock.Mock(return_value=r)))


class Base(unittest.TestCase):
    def setUp(self):
        for f, empty in FILES:
            write_json(f, empty)
        self.addCleanup(lambda: [write_json(f, e) for f, e in FILES])
        bank = Path(tempfile.mkdtemp()) / "bank.txt"
        bank.write_text("Standing answers\n", encoding="utf-8")
        for p in (mock.patch.object(answers, "ANSWER_BANK", bank), mock.patch("agent.scheduler.run_soon", return_value=True)):
            p.start()
            self.addCleanup(p.stop)
        self.d1 = decisions.offer(job("1", "Director of AI", "Acme"))
        self.d2 = decisions.offer(job("2", "VP Data", "Beta Corp"))
        row = {"number": 9, "job_id": "50", "company": "Gamma", "title": "CAIO"}
        self.q_yn, _ = questions.ask(row, {"question": "Led an AI program?", "options": ["Yes", "No"]}, "link")
        self.q_pick, _ = questions.ask(row, {"question": "Work authorization?", "options": ["Citizen", "Visa"]}, "link")
        self.q_text, _ = questions.ask(row, {"question": "Years of AI experience?", "options": []}, "link")


class Route(Base):
    def test_decisions_and_answers_come_back_as_commands(self):
        c = client(actions=[{"type": "decide", "id": self.d1["id"], "choice": "apply"},
                            {"type": "decide", "id": self.d2["id"], "choice": "SKIP"},
                            {"type": "answer", "id": self.q_yn["id"], "value": "no"},
                            {"type": "answer", "id": self.q_text["id"], "value": "20 years"},
                            {"type": "skip_application", "id": self.q_pick["id"]}], comment=False, ask=None)
        out = router.route("whatever", client=c)
        self.assertEqual(out["commands"], [f"DECIDE {self.d1['id']} APPLY", f"DECIDE {self.d2['id']} SKIP",
                                           f"ANSWER {self.q_yn['id']} No", f"ANSWER {self.q_text['id']} 20 years",
                                           f"ANSWER {self.q_pick['id']} SKIP"])
        self.assertIsNone(out["ask"])

    def test_an_invented_or_closed_id_is_dropped_and_turns_into_a_question(self):
        out = router.route("x", client=client(actions=[{"type": "decide", "id": "deadbeef", "choice": "APPLY"}],
                                              comment=False, ask=None))
        self.assertEqual(out["commands"], [])
        self.assertIn("could not match", out["ask"])
        self.assertIn("https://www.linkedin.com/jobs/view/2", out["ask"])      # the list carries location and link

    def test_an_answer_that_is_not_one_of_the_choices_is_asked_about(self):
        out = router.route("x", client=client(actions=[{"type": "answer", "id": self.q_pick["id"], "value": "Green card"}],
                                              comment=False, ask=None))
        self.assertEqual(out["commands"], [])
        self.assertIn("Pick one of: Citizen, Visa", out["ask"])

    def test_the_model_asking_which_one_is_passed_on_with_the_open_list(self):
        out = router.route("apply", client=client(actions=[], comment=False, ask="Which role?"))
        self.assertTrue(out["ask"].startswith("Which role?"))
        self.assertIn("Beta Corp", out["ask"])

    def test_chat_does_nothing(self):
        out = router.route("how are you", client=client(actions=[], comment=False, ask=None))
        self.assertEqual(out, {"commands": [], "comment": False, "ask": None, "reply": None})

    def test_the_message_and_open_items_reach_the_model_as_data(self):
        c = client(actions=[], comment=False, ask=None)
        router.route("skip beta, too junior", client=c)
        prompt = c.messages.create.call_args.kwargs["messages"][0]["content"]
        self.assertIn("skip beta, too junior", prompt)
        self.assertIn(self.d2["id"], prompt)
        self.assertIn("Options: Yes, No", prompt)


class Replies(Base):
    def test_a_question_to_the_agent_gets_a_reply(self):
        out = router.route("will you use my messages?", client=client(
            actions=[], comment=False, ask=None, reply="Yes: what you type is acted on at once."))
        self.assertEqual(out["reply"], "Yes: what you type is acted on at once.")

    def test_the_prompt_tells_the_model_to_map_to_an_option(self):
        c = client(actions=[], comment=False, ask=None)
        router.route("10 years", client=c)
        prompt = c.messages.create.call_args.kwargs["messages"][0]["content"]
        self.assertIn('"5+"', prompt)


class EndToEnd(Base):
    def say(self, text, c):
        write_json("whatsapp_inbox.json", [{"ts": 1, "text": text, "via": "text", "handled": False}])
        with mock.patch("anthropic.Anthropic", return_value=c):
            approvals.process_whatsapp()

    def test_skip_one_role_and_state_a_preference_in_one_message(self):
        replies = [SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(
            {"actions": [{"type": "decide", "id": self.d2["id"], "choice": "SKIP"}], "comment": True, "ask": None}))],
            stop_reason="end_turn"),
            SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(
                {"rules": [{"type": "exclude_company", "value": "Beta Corp", "reason": "he said so"}]}))],
                stop_reason="end_turn")]
        c = SimpleNamespace(messages=SimpleNamespace(create=mock.Mock(side_effect=replies)))
        self.say("skip beta, never show me Beta Corp again", c)
        self.assertIn("skipped", ledger.seen_outcomes()["2"]["outcome"])
        self.assertEqual(feedback.active_rules()[0]["value"], "Beta Corp")
        bodies = " ".join(m["body"] for m in read_json(reporter.OUTBOX, []))
        self.assertIn('You said: "skip beta, never show me Beta Corp again"', bodies)

    def test_a_question_is_answered_on_whatsapp(self):
        c = client(actions=[], comment=False, ask=None, reply="Yes, I act on what you type.")
        self.say("you will take my messages right?", c)
        self.assertIn("Yes, I act on what you type.", read_json(reporter.OUTBOX, [])[-1]["body"])

    def test_nothing_is_acted_on_when_the_model_returns_nonsense(self):
        r = SimpleNamespace(content=[SimpleNamespace(type="text", text="I think he wants to apply")], stop_reason="end_turn")
        c = SimpleNamespace(messages=SimpleNamespace(create=mock.Mock(return_value=r)))
        self.say("apply", c)                            # unparseable -> plain rules, and "apply" alone is ambiguous: it asks
        self.assertEqual(queue_tool.all_items(), [])


if __name__ == "__main__":
    unittest.main()
