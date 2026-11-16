import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("AGENT_DATA_DIR", tempfile.mkdtemp())

import monitor  # noqa: E402
from agent import approvals, decisions, ledger, queue_tool, reporter, tracker  # noqa: E402
from agent.store import read_json, write_json  # noqa: E402

JOB = {"job_id": "700", "title": "Director of AI", "company": "Acme", "location": "Texas (Remote)",
       "url": "https://www.linkedin.com/jobs/view/700", "easy_apply": True, "summary": "Strong fit."}


class Base(unittest.TestCase):
    def setUp(self):
        for f, empty in ((decisions.FILE, []), (reporter.OUTBOX, []), (queue_tool.FILE, []),
                         (tracker.FILE, []), (ledger.FILE, {})):
            write_json(f, empty)
        sched = mock.patch("agent.scheduler.run_soon", return_value=True)
        self.run_soon = sched.start()
        self.addCleanup(sched.stop)


class Offer(Base):
    def test_one_whatsapp_only_message_with_apply_and_skip_buttons(self):
        rec = decisions.offer(JOB)
        events = read_json(reporter.OUTBOX, [])
        self.assertEqual(len(events), 1)
        e = events[0]
        self.assertTrue(e["email_sent"], "no email: the monitor email already lists the role")
        self.assertEqual(e["whatsapp"], "pending")
        self.assertEqual([t for _, t in e["buttons"]], ["Apply", "Skip"])
        self.assertEqual(e["buttons"][0][0], f"DECIDE {rec['id']} APPLY")
        self.assertIn("Director of AI", e["wa_text"])
        self.assertIn("Strong fit.", e["wa_text"])
        self.assertIn("https://www.linkedin.com/jobs/view/700", e["wa_text"])

    def test_a_role_is_offered_only_once(self):
        self.assertIsNotNone(decisions.offer({**JOB, "job_ids": ["700"]}))
        self.assertIsNone(decisions.offer({**JOB, "job_ids": ["700"]}))

    def test_a_role_already_applied_for_or_queued_is_not_offered(self):
        tracker.book(1, "Acme", "Director of AI", "700", "SUBMITTED", url="u")
        self.assertIsNone(decisions.offer({**JOB, "job_ids": ["700"]}))


class Deciding(Base):
    def offered(self):
        return decisions.offer({**JOB, "job_ids": ["700", "701"]})

    def test_apply_queues_it_even_when_the_card_did_not_say_remote(self):
        rec = self.offered()
        ok, msg = decisions.decide(rec["id"], "APPLY")
        self.assertTrue(ok, msg)
        item = queue_tool.all_items()[0]
        self.assertEqual((item["company"], item["title"], item["status"]), ("Acme", "Director of AI", "QUEUED"))
        self.assertEqual(item["apply_type"], "Easy Apply")
        self.assertEqual(decisions.get(rec["id"])["status"], "APPLY")
        self.run_soon.assert_called_with("applier")

    def test_normal_queue_still_refuses_an_unknown_workplace(self):
        ok, why = queue_tool.add({"job_id": "1", "title": "T", "company": "C", "workplace": ""})
        self.assertFalse(ok)
        self.assertIn("dropped", why)

    def test_skip_marks_every_posting_and_removes_a_waiting_queue_item(self):
        rec = self.offered()
        queue_tool.add({"job_id": "700", "title": "Director of AI", "company": "Acme", "workplace": "Remote"})
        ok, _ = decisions.decide(rec["id"], "skip")
        self.assertTrue(ok)
        self.assertEqual(queue_tool.all_items(), [])
        self.assertEqual(set(ledger.seen_outcomes()), {"700", "701"})
        self.assertEqual(decisions.get(rec["id"])["status"], "SKIPPED")

    def test_a_second_tap_is_refused_and_a_bad_word_is_refused(self):
        rec = self.offered()
        self.assertFalse(decisions.decide(rec["id"], "maybe")[0])
        self.assertTrue(decisions.decide(rec["id"], "apply")[0])
        self.assertFalse(decisions.decide(rec["id"], "skip")[0])
        self.assertEqual(len(queue_tool.all_items()), 1)

    def test_a_tapped_button_is_understood_as_a_command(self):
        rec = self.offered()
        cmd = approvals.parse(f"DECIDE {rec['id']} APPLY")
        self.assertEqual((cmd["action"], cmd["id"], cmd["text"]), ("DECIDE", rec["id"], "APPLY"))
        self.assertIn("Queued", approvals.apply(cmd))


class FromTheMonitor(Base):
    def test_offers_easy_apply_roles_but_not_applied_or_dropped_or_site_roles(self):
        jobs = [
            {**JOB, "job_id": "1", "title": "A", "company": "a"},
            {**JOB, "job_id": "2", "title": "B", "company": "b", "applied": True},
            {**JOB, "job_id": "3", "title": "C", "company": "c", "easy_apply": False},
            {**JOB, "job_id": "4", "title": "D", "company": "d"},
            {**JOB, "job_id": "5", "title": "E", "company": "e"},
        ]
        ledger.add("4", "dropped: workplace is Hybrid")
        ledger.add("5", "skip: title does not match the search rule")
        self.assertEqual(monitor.offer_easy_apply_decisions(jobs), 2)
        texts = [e["wa_text"] for e in read_json(reporter.OUTBOX, [])]
        self.assertTrue(any("— a" in t for t in texts))
        skipped = [t for t in texts if "— e" in t][0]
        self.assertIn("would skip this: title does not match", skipped)

    def test_no_more_than_eight_per_email(self):
        jobs = [{**JOB, "job_id": str(i), "title": f"Role {i}", "company": f"Co {i}"} for i in range(12)]
        self.assertEqual(monitor.offer_easy_apply_decisions(jobs), decisions.MAX_OFFERS_PER_EMAIL)


class ReporterWhatsAppOnly(Base):
    def test_a_whatsapp_only_event_sends_no_email(self):
        reporter.enqueue("question", "s", "body", buttons=[("A", "Yes"), ("B", "No")], wa_text="t", wa_prompt="p", email=False)
        mails, chats = [], []
        stats = reporter.process(send_email=lambda s, b: mails.append(s), send_whatsapp=lambda t: chats.append(t) or True,
                                 send_buttons=lambda t, b: chats.append(t) or True)
        self.assertEqual(mails, [])
        self.assertEqual(chats, ["t"])
        self.assertEqual(stats["whatsapp"], 1)


if __name__ == "__main__":
    unittest.main()
