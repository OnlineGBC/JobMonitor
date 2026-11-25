"""Typed replies act at once (no Confirm step). The fallback keyword rules and the old confirmations are covered here;
the model-based reading is in test_router.py."""
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

os.environ.setdefault("AGENT_DATA_DIR", tempfile.mkdtemp())

from agent import answers, approvals, confirmations, decisions, interpret, ledger, queue_tool, questions, reporter, tracker  # noqa: E402
from agent.store import read_json, write_json  # noqa: E402


def job(jid, title, company):
    return {"job_id": jid, "job_ids": [jid], "title": title, "company": company, "location": "Texas (Remote)",
            "url": f"https://www.linkedin.com/jobs/view/{jid}", "summary": "fit"}


class Base(unittest.TestCase):
    def setUp(self):
        for f, empty in ((decisions.FILE, []), (questions.FILE, []), (confirmations.FILE, []), (reporter.OUTBOX, []),
                         (queue_tool.FILE, []), (tracker.FILE, []), (ledger.FILE, {}), ("whatsapp_inbox.json", [])):
            write_json(f, empty)
        self.addCleanup(self.wipe)
        bank = Path(tempfile.mkdtemp()) / "bank.txt"
        bank.write_text("Standing answers\n", encoding="utf-8")
        # The model is unavailable in these tests, so the plain keyword rules read the replies.
        for p in (mock.patch.object(answers, "ANSWER_BANK", bank), mock.patch("agent.scheduler.run_soon", return_value=True),
                  mock.patch("agent.router.route", side_effect=RuntimeError("model unavailable"))):
            p.start()
            self.addCleanup(p.stop)

    @staticmethod
    def wipe():
        """Leave nothing open for other test modules (they share one data directory)."""
        for f, empty in ((decisions.FILE, []), (questions.FILE, []), (confirmations.FILE, []), (reporter.OUTBOX, []),
                         (queue_tool.FILE, []), (tracker.FILE, []), (ledger.FILE, {}), ("whatsapp_inbox.json", [])):
            write_json(f, empty)

    def say(self, text, via="text"):
        write_json("whatsapp_inbox.json", [{"ts": 1, "text": text, "via": via, "handled": False}])
        approvals.process_whatsapp()

    def outbox(self):
        return read_json(reporter.OUTBOX, [])


class Decisions(Base):
    def test_a_tapped_button_acts_at_once(self):
        rec = decisions.offer(job("1", "Director of AI", "Acme"))
        self.say(f"DECIDE {rec['id']} APPLY", via="button")
        self.assertEqual(len(queue_tool.all_items()), 1)

    def test_typing_apply_acts_at_once_and_says_what_was_done(self):
        decisions.offer(job("1", "Director of AI", "Acme"))
        self.say("apply")
        self.assertEqual(len(queue_tool.all_items()), 1)
        self.assertEqual(confirmations.open_ones(), [])
        self.assertIn('You said: "apply"', self.outbox()[-1]["body"])
        self.assertIn("Queued Director of AI at Acme", self.outbox()[-1]["body"])

    def test_typing_skip_acts_at_once(self):
        decisions.offer(job("2", "VP AI", "Beta"))
        self.say("skip")
        self.assertIn("skipped", ledger.seen_outcomes()["2"]["outcome"])

    def test_several_open_roles_need_the_company_name(self):
        decisions.offer(job("1", "Director of AI", "Acme"))
        decisions.offer(job("2", "VP AI", "Beta Corp"))
        self.say("apply")
        self.assertEqual(queue_tool.all_items(), [])
        text = self.outbox()[-1]["body"]
        self.assertIn("Which one", text + self.outbox()[-1]["subject"])
        self.assertIn("Texas (Remote)", text)                       # enough detail to decide: location and link
        self.assertIn("https://www.linkedin.com/jobs/view/2", text)
        self.say("apply beta")
        self.assertEqual([q["company"] for q in queue_tool.all_items()], ["Beta Corp"])

    def test_skip_all_skips_every_open_role(self):
        decisions.offer(job("1", "Director of AI", "Acme"))
        decisions.offer(job("2", "VP AI", "Beta"))
        self.say("skip all")
        self.assertEqual(set(ledger.seen_outcomes()), {"1", "2"})

    def test_ok_and_thanks_are_never_turned_into_an_answer(self):
        decisions.offer(job("1", "Director of AI", "Acme"))
        for word in ("ok", "OK", "thanks", "thank you", "hello", "Ok again"):
            self.say(word)
        self.assertEqual(queue_tool.all_items(), [])
        self.assertEqual(len(self.outbox()), 1)               # only the original offer

    def test_chatter_when_nothing_is_waiting_is_ignored(self):
        self.say("hello there")
        self.assertEqual(self.outbox(), [])


class Questions(Base):
    def ask(self, options=None, question="How many years of AI experience?"):
        row = {"number": 9, "job_id": "50", "company": "Gamma", "title": "CAIO"}
        rec, _ = questions.ask(row, {"question": question, "options": options or []}, "link")
        return rec

    def test_typed_free_text_answer_is_saved_at_once(self):
        rec = self.ask()
        self.say("20 years")
        self.assertEqual(answers.exact_answer(rec["question"], answers.load_bank()), "20 years")
        self.assertEqual(confirmations.open_ones(), [])

    def test_a_typed_command_with_free_text_is_saved_at_once(self):
        rec = self.ask()
        self.say(f"ANSWER {rec['id']} 20")
        self.assertEqual(answers.exact_answer(rec["question"], answers.load_bank()), "20")

    def test_plain_no_on_a_yes_no_question_is_an_answer(self):
        rec = self.ask(["Yes", "No"], "Led an AI program?")
        self.say("no")
        self.assertEqual(answers.exact_answer(rec["question"], answers.load_bank()), "No")

    def test_skip_word_skips_the_application(self):
        rec = self.ask(["Yes", "No"], "Led an AI program?")
        self.say("skip")
        self.assertEqual(questions.get(rec["id"])["status"], "SKIPPED")


class OldConfirmations(Base):
    """Confirm prompts sent before this change still work; none are created any more."""
    def old_confirmation(self, cid, created=None):
        rec = {"id": cid, "commands": [f"DECIDE {decisions.all_decisions()[0]['id']} APPLY"], "description": "Apply: Acme",
               "source": "whatsapp", "status": "PENDING",
               "created": (created or datetime.now(reporter.NY)).isoformat(timespec="seconds")}
        write_json(confirmations.FILE, [rec])
        return rec

    def test_a_plain_yes_still_settles_an_old_confirmation(self):
        decisions.offer(job("1", "Director of AI", "Acme"))
        rec = self.old_confirmation("abc12345")
        self.say("yes")
        self.assertEqual(len(queue_tool.all_items()), 1)
        self.assertEqual(confirmations.get(rec["id"])["status"], "CONFIRMED")

    def test_an_old_confirmation_is_dropped_after_24_hours(self):
        decisions.offer(job("1", "Director of AI", "Acme"))
        self.old_confirmation("abc12345", datetime.now(reporter.NY) - timedelta(hours=30))
        self.assertEqual(confirmations.open_ones(), [])


class Channels(Base):
    def test_acknowledging_a_whatsapp_reply_sends_no_email(self):
        rec = decisions.offer(job("1", "Director of AI", "Acme"))
        before = len(self.outbox())
        self.say(f"DECIDE {rec['id']} SKIP", via="button")
        acks = self.outbox()[before:]
        self.assertEqual(len(acks), 1)
        self.assertTrue(acks[0]["email_sent"], "already marked sent, so the mailer never sends it")
        self.assertEqual(acks[0]["whatsapp"], "pending")

    def test_an_emailed_reply_is_still_acknowledged_by_email(self):
        rec = decisions.offer(job("2", "VP AI", "Beta"))
        before = len(self.outbox())
        approvals._answer(approvals.parse(f"DECIDE {rec['id']} SKIP"), "gmail")
        self.assertFalse(self.outbox()[before]["email_sent"])

    def test_a_role_that_is_not_remote_is_never_offered(self):
        onsite = dict(job("7", "CTO", "Onsite Co"), location="New York, NY")
        self.assertIsNone(decisions.offer(onsite))
        self.assertEqual(self.outbox(), [])
        self.assertIsNotNone(decisions.offer(job("8", "CTO", "Remote Co")))


if __name__ == "__main__":
    unittest.main()
