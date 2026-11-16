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
        for p in (mock.patch.object(answers, "ANSWER_BANK", bank), mock.patch("agent.scheduler.run_soon", return_value=True)):
            p.start()
            self.addCleanup(p.stop)

    @staticmethod
    def wipe():
        """Leave nothing open for other test modules (they share one data directory)."""
        for f, empty in ((decisions.FILE, []), (questions.FILE, []), (confirmations.FILE, []), (reporter.OUTBOX, []),
                         (queue_tool.FILE, []), (tracker.FILE, []), (ledger.FILE, {}), ("whatsapp_inbox.json", [])):
            write_json(f, empty)

    def say(self, text, via="text"):
        """Raja sends one WhatsApp message (typed, or a tapped button when via='button')."""
        write_json("whatsapp_inbox.json", [{"ts": 1, "text": text, "via": via, "handled": False}])
        approvals.process_whatsapp()

    def outbox(self):
        return read_json(reporter.OUTBOX, [])

    def last_buttons(self):
        return [t for _, t in self.outbox()[-1].get("buttons", [])]

    def confirm_id(self):
        return confirmations.open_ones()[-1]["id"]


class Decisions(Base):
    def test_a_tapped_button_acts_at_once_with_no_confirmation(self):
        rec = decisions.offer(job("1", "Director of AI", "Acme"))
        self.say(f"DECIDE {rec['id']} APPLY", via="button")
        self.assertEqual(len(queue_tool.all_items()), 1)
        self.assertEqual(confirmations.open_ones(), [])

    def test_typed_apply_is_echoed_back_and_does_nothing_until_confirmed(self):
        decisions.offer(job("1", "Director of AI", "Acme"))
        self.say("apply")
        self.assertEqual(queue_tool.all_items(), [])
        self.assertEqual(self.last_buttons(), ["Confirm", "Cancel"])
        self.assertIn("Apply: Director of AI at Acme", self.outbox()[-1]["wa_text"])
        self.say(f"CONFIRM {self.confirm_id()}", via="button")
        self.assertEqual(len(queue_tool.all_items()), 1)

    def test_typed_yes_confirms_and_typed_no_cancels(self):
        decisions.offer(job("1", "Director of AI", "Acme"))
        self.say("apply")
        self.say("yes")
        self.assertEqual(len(queue_tool.all_items()), 1)
        decisions.offer(job("2", "VP AI", "Beta"))
        self.say("skip")
        self.say("no")
        self.assertEqual(ledger.seen_outcomes().get("2"), None)
        self.assertEqual(confirmations.open_ones(), [])

    def test_cancel_button_does_nothing(self):
        decisions.offer(job("1", "Director of AI", "Acme"))
        self.say("apply")
        self.say(f"CANCEL {self.confirm_id()}", via="button")
        self.assertEqual(queue_tool.all_items(), [])
        self.assertEqual(decisions.all_decisions()[0]["status"], "OPEN")

    def test_several_open_roles_need_the_company_name(self):
        decisions.offer(job("1", "Director of AI", "Acme"))
        decisions.offer(job("2", "VP AI", "Beta Corp"))
        self.say("apply")
        self.assertIn("Which one", self.outbox()[-1]["body"] + self.outbox()[-1]["subject"])
        self.assertEqual(confirmations.open_ones(), [])
        self.say("apply beta")
        self.assertIn("Apply: VP AI at Beta Corp", confirmations.open_ones()[-1]["description"])
        self.assertEqual(confirmations.open_ones()[-1]["commands"][0].endswith("APPLY"), True)

    def test_skip_all_lists_every_role_for_confirmation(self):
        decisions.offer(job("1", "Director of AI", "Acme"))
        decisions.offer(job("2", "VP AI", "Beta"))
        self.say("skip all")
        c = confirmations.open_ones()[-1]
        self.assertEqual(len(c["commands"]), 2)
        self.assertIn("Acme", c["description"])
        self.assertIn("Beta", c["description"])
        self.assertEqual(ledger.seen_outcomes(), {})
        self.say("confirm")
        self.assertEqual(set(ledger.seen_outcomes()), {"1", "2"})

    def test_ok_and_thanks_are_never_turned_into_an_answer(self):
        decisions.offer(job("1", "Director of AI", "Acme"))
        for word in ("ok", "OK", "thanks", "thank you", "hello", "Ok again"):
            self.say(word)
        self.assertEqual(confirmations.open_ones(), [])
        self.assertEqual(queue_tool.all_items(), [])
        self.assertEqual(len(self.outbox()), 1)               # only the original offer

    def test_ok_does_confirm_when_a_confirmation_is_waiting(self):
        decisions.offer(job("1", "Director of AI", "Acme"))
        self.say("apply")
        self.say("ok")
        self.assertEqual(len(queue_tool.all_items()), 1)

    def test_a_role_that_is_not_remote_is_never_offered(self):
        onsite = dict(job("7", "CTO", "Onsite Co"), location="New York, NY")
        self.assertIsNone(decisions.offer(onsite))
        self.assertEqual(self.outbox(), [])
        self.assertIsNotNone(decisions.offer(job("8", "CTO", "Remote Co")))

    def test_which_one_list_shows_remote_and_the_link(self):
        decisions.offer(job("1", "Director of AI", "Acme"))
        decisions.offer(job("2", "VP AI", "Beta Corp"))
        self.say("apply")
        body = self.outbox()[-1]["body"]
        self.assertIn("Remote: Texas (Remote)", body)
        self.assertIn("https://www.linkedin.com/jobs/view/1", body)

    def test_chatter_when_nothing_is_waiting_is_ignored(self):
        self.say("hello there")
        self.assertEqual(self.outbox(), [])


class Questions(Base):
    def ask(self, options=None, question="How many years of AI experience?"):
        row = {"number": 9, "job_id": "50", "company": "Gamma", "title": "CAIO"}
        rec, _ = questions.ask(row, {"question": question, "options": options or []}, "link")
        return rec

    def test_typed_free_text_answer_is_confirmed_before_it_is_saved(self):
        rec = self.ask()
        self.say("20 years")
        self.assertIsNone(answers.exact_answer(rec["question"], answers.load_bank()))
        self.assertIn("Answer '20 years' to: How many years of AI experience?", confirmations.open_ones()[-1]["description"])
        self.say("yes")
        self.assertEqual(answers.exact_answer(rec["question"], answers.load_bank()), "20 years")

    def test_a_typed_command_with_free_text_is_also_confirmed(self):
        rec = self.ask()
        self.say(f"ANSWER {rec['id']} 20")
        self.assertIsNone(answers.exact_answer(rec["question"], answers.load_bank()))
        self.assertEqual(len(confirmations.open_ones()), 1)

    def test_typed_yes_no_command_and_taps_need_no_confirmation(self):
        rec = self.ask(["Yes", "No"], "Led an AI program?")
        self.say(f"ANSWER {rec['id']} YES")
        self.assertEqual(answers.exact_answer(rec["question"], answers.load_bank()), "Yes")
        self.assertEqual(confirmations.open_ones(), [])
        rec2 = self.ask(["Yes", "No"], "Another question?")
        self.say(f"ANSWER {rec2['id']} NO", via="button")
        self.assertEqual(answers.exact_answer(rec2["question"], answers.load_bank()), "No")

    def test_plain_no_on_a_yes_no_question_is_an_answer_after_confirmation(self):
        rec = self.ask(["Yes", "No"], "Led an AI program?")
        self.say("no")
        self.assertIn("Answer NO", confirmations.open_ones()[-1]["description"])
        self.assertIsNone(answers.exact_answer(rec["question"], answers.load_bank()))
        self.say("yes")                      # this "yes" answers the CONFIRMATION, not the question
        self.assertEqual(answers.exact_answer(rec["question"], answers.load_bank()), "No")

    def test_skip_word_skips_the_application_after_confirmation(self):
        rec = self.ask(["Yes", "No"], "Led an AI program?")
        self.say("skip")
        self.assertEqual(questions.get(rec["id"])["status"], "OPEN")
        self.say("confirm")
        self.assertEqual(questions.get(rec["id"])["status"], "SKIPPED")


class Expiry(Base):
    def test_an_old_confirmation_is_dropped_not_run(self):
        decisions.offer(job("1", "Director of AI", "Acme"))
        self.say("apply")
        items = read_json(confirmations.FILE, [])
        items[0]["created"] = (datetime.now(reporter.NY) - timedelta(hours=30)).isoformat(timespec="seconds")
        write_json(confirmations.FILE, items)
        self.assertEqual(confirmations.open_ones(), [])
        self.say(f"CONFIRM {items[0]['id']}", via="button")
        self.assertEqual(queue_tool.all_items(), [])


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

    def test_a_whatsapp_confirmation_sends_no_email_but_an_email_reply_does(self):
        decisions.offer(job("1", "Director of AI", "Acme"))
        self.say("apply")
        self.assertTrue(self.outbox()[-1]["email_sent"])          # already marked: WhatsApp only
        confirmations.propose(["DECIDE x APPLY"], "d", "gmail")
        self.assertFalse(self.outbox()[-1]["email_sent"])         # by email the prompt must be emailed


if __name__ == "__main__":
    unittest.main()
