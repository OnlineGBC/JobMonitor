import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("AGENT_DATA_DIR", tempfile.mkdtemp())

from agent import approvals, confirmations, decisions, feedback, ledger, queue_tool, questions, reporter, tracker  # noqa: E402
from agent.store import read_json, write_json  # noqa: E402

FILES = ((decisions.FILE, []), (questions.FILE, []), (confirmations.FILE, []), (reporter.OUTBOX, []),
         (queue_tool.FILE, []), (tracker.FILE, []), (ledger.FILE, {}), ("whatsapp_inbox.json", []),
         (feedback.LOG, []), (feedback.RULES, []))


def job(jid, title, company, location="Texas (Remote)"):
    return {"job_id": jid, "job_ids": [jid], "title": title, "company": company, "location": location,
            "url": f"https://www.linkedin.com/jobs/view/{jid}", "summary": "fit"}


def model(*rules):
    """A fake Claude client that answers with these rules."""
    reply = SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps({"rules": list(rules)}))],
                            stop_reason="end_turn")
    return SimpleNamespace(messages=SimpleNamespace(create=mock.Mock(return_value=reply)))


COMMENT = {"commands": [], "comment": True, "ask": None}       # what the router says about a plain comment


class Base(unittest.TestCase):
    def setUp(self):
        for f, empty in FILES:
            write_json(f, empty)
        self.addCleanup(lambda: [write_json(f, e) for f, e in FILES])
        p = mock.patch("agent.scheduler.run_soon", return_value=True)
        p.start()
        self.addCleanup(p.stop)

    def say(self, text, client=None, routed=COMMENT, via="text"):
        write_json("whatsapp_inbox.json", [{"ts": 1, "text": text, "via": via, "handled": False}])
        with mock.patch("anthropic.Anthropic", return_value=client or model()), \
                mock.patch("agent.router.route", return_value=routed):
            approvals.process_whatsapp()

    def outbox(self):
        return read_json(reporter.OUTBOX, [])


class Learning(Base):
    def test_a_comment_takes_effect_at_once_with_your_exact_words_and_an_undo(self):
        self.say("I do not want part-time work",
                 model({"type": "exclude_title", "value": "Part-Time", "reason": "he does not want part-time"}))
        self.assertEqual(read_json(feedback.LOG, [])[0]["text"], "I do not want part-time work")
        self.assertEqual(feedback.active_rules()[0]["value"], "Part-Time")
        ack = self.outbox()[-1]
        self.assertIn('You said: "I do not want part-time work"', ack["body"])
        self.assertIn('Never offer roles with "Part-Time" in the title', ack["body"])
        self.assertEqual(ack["buttons"][0][0], f"RULE {feedback.active_rules()[0]['id']} REMOVE")
        self.assertTrue(ack["email_sent"])                        # WhatsApp only

    def test_undo_takes_the_rule_back(self):
        self.say("I do not want part-time work", model({"type": "exclude_title", "value": "Part-Time", "reason": "x"}))
        rid = feedback.active_rules()[0]["id"]
        self.say(f"RULE {rid} REMOVE", via="button", routed={"commands": [], "comment": False, "ask": None})
        self.assertEqual(feedback.active_rules(), [])
        self.assertIsNotNone(decisions.offer(job("6", "Part-Time CTO", "Zed")))

    def test_a_rule_stops_the_offer_and_the_agents_own_queue_but_not_your_apply(self):
        self.say("stop sending me contract roles", model({"type": "exclude_title", "value": "Contract", "reason": "x"}))
        self.assertIsNone(decisions.offer(job("1", "Contract CTO", "Acme")))
        self.assertIn("your rule", ledger.seen_outcomes()["1"]["outcome"])
        ok, why = queue_tool.add({"job_id": "2", "title": "Contract CTO", "company": "B", "workplace": "Remote"})
        self.assertFalse(ok)
        self.assertIn("your rule", why)
        self.assertTrue(queue_tool.add({"job_id": "3", "title": "Contract CTO", "company": "C", "workplace": "Remote"},
                                       forced=True)[0])
        self.assertIsNotNone(decisions.offer(job("4", "Chief AI Officer", "Gamma")))

    def test_a_company_rule(self):
        self.say("never show me anything from Acme Staffing",
                 model({"type": "exclude_company", "value": "Acme Staffing", "reason": "x"}))
        self.assertIsNone(decisions.offer(job("5", "CTO", "Acme Staffing Inc")))

    def test_a_preference_that_cannot_be_enforced_is_kept_as_a_note(self):
        self.say("I prefer AI strategy roles over engineering", model({"type": "note", "value": "prefers AI strategy", "reason": "x"}))
        self.assertIn("Noted: prefers AI strategy", self.outbox()[-1]["body"])
        self.assertEqual(feedback.active_rules()[0]["type"], "note")

    def test_core_words_are_never_excluded(self):
        self.say("I never want to see AI roles",
                 model({"type": "exclude_title", "value": "AI", "reason": "x"}, {"type": "exclude_title", "value": "Chief", "reason": "x"}))
        self.assertEqual(feedback.active_rules(), [])

    def test_model_failure_still_saves_the_comment(self):
        bad = SimpleNamespace(messages=SimpleNamespace(create=mock.Mock(side_effect=RuntimeError("api down"))))
        self.say("never show me part-time roles", bad)
        self.assertEqual(len(read_json(feedback.LOG, [])), 1)
        self.assertEqual(feedback.active_rules(), [])
        self.assertIn("could not interpret", self.outbox()[-1]["body"])

    def test_rules_lists_the_rules(self):
        self.say("stop sending contract roles", model({"type": "exclude_title", "value": "Contract", "reason": "x"}))
        rid = feedback.active_rules()[0]["id"]
        self.say("RULES")
        self.assertIn(rid, self.outbox()[-1]["body"])


class Fallback(Base):
    """When the model cannot be reached, the keyword rules still keep comments and never ask for a confirm."""
    def say_offline(self, text, client):
        write_json("whatsapp_inbox.json", [{"ts": 1, "text": text, "via": "text", "handled": False}])
        with mock.patch("anthropic.Anthropic", return_value=client), \
                mock.patch("agent.router.route", side_effect=RuntimeError("down")):
            approvals.process_whatsapp()

    def test_a_marker_comment_is_learned_without_a_confirm(self):
        self.say_offline("never show me contract roles", model({"type": "exclude_title", "value": "Contract", "reason": "x"}))
        self.assertEqual(feedback.active_rules()[0]["value"], "Contract")
        self.assertEqual(confirmations.open_ones(), [])

    def test_unrelated_chat_is_ignored_but_a_reaction_to_a_shown_role_is_kept(self):
        client = model()
        self.say_offline("what is the weather today", client)
        client.messages.create.assert_not_called()
        decisions.offer(job("1", "Director of AI", "Acme"))
        self.say_offline("what is the weather today", client)
        client.messages.create.assert_called_once()
        self.assertEqual(len(read_json(feedback.LOG, [])), 1)

    def test_a_typed_sentence_answering_a_free_text_question_stays_an_answer(self):
        row = {"number": 9, "job_id": "50", "company": "Gamma", "title": "CAIO"}
        questions.ask(row, {"question": "Describe your AI experience", "options": []}, "link")
        client = model()
        self.say_offline("I have never worked without AI in my stack", client)
        client.messages.create.assert_not_called()
        self.assertEqual(read_json(feedback.LOG, []), [])


if __name__ == "__main__":
    unittest.main()
