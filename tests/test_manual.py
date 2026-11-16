import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("AGENT_DATA_DIR", tempfile.mkdtemp())

from agent import approvals, chatlog, decisions, ledger, manual, next_number, queue_tool, reporter, tracker  # noqa: E402
from agent.store import read_json, write_json  # noqa: E402

FILES = ((queue_tool.FILE, []), (tracker.FILE, []), (ledger.FILE, {}), (reporter.OUTBOX, []), (decisions.FILE, []),
         ("whatsapp_inbox.json", []), (chatlog.FILE, None))


def reset():
    for f, empty in FILES:
        if empty is None:
            path = os.path.join(os.environ["AGENT_DATA_DIR"], f)
            if os.path.exists(path):
                os.remove(path)
        else:
            write_json(f, empty)
    next_number.set_next(30)


class Manual(unittest.TestCase):
    def setUp(self):
        reset()
        self.addCleanup(reset)
        write_json(queue_tool.FILE, [{"job_id": "4490945016", "title": "Chief Technology Officer", "company": "NET-Group Ltd",
                                      "status": "AWAITING RAJA", "queued_at": "2026-11-16T20:43:21+00:00", "attempts": 3}])

    def test_a_failed_queued_role_is_booked_as_applied_by_raja_and_leaves_the_queue(self):
        ok, msg = manual.mark_applied("4490945016")
        self.assertTrue(ok)
        self.assertIn("Recorded #30: Chief Technology Officer at NET-Group Ltd as applied", msg)
        row = tracker.rows()[0]
        self.assertEqual((row["status"], row["manual"], row["job_id"]), ("SUBMITTED", True, "4490945016"))
        self.assertIn("Applied by Raja himself", row["note"])
        self.assertEqual(queue_tool.all_items(), [])
        self.assertIn("applied", ledger.seen_outcomes()["4490945016"]["outcome"])

    def test_it_is_idempotent(self):
        manual.mark_applied("4490945016")
        ok, msg = manual.mark_applied("4490945016")
        self.assertIn("already recorded as applied", msg)
        self.assertEqual(len(tracker.rows()), 1)

    def test_an_awaiting_row_is_settled_not_duplicated(self):
        tracker.book(5, "Acme", "VP AI", "777777", "AWAITING RAJA", url="u")
        write_json(queue_tool.FILE, [])
        manual.mark_applied("777777")
        self.assertEqual([(r["number"], r["status"]) for r in tracker.rows()], [(5, "SUBMITTED")])

    def test_an_unknown_job_is_refused(self):
        ok, msg = manual.mark_applied("123456789")
        self.assertFalse(ok)
        self.assertEqual(tracker.rows(), [])

    def test_the_typed_command_works(self):
        cmd = approvals.parse("APPLIED 4490945016")
        self.assertEqual((cmd["action"], cmd["id"]), ("APPLIED", "4490945016"))
        approvals._answer(cmd, "whatsapp")
        self.assertEqual(tracker.rows()[0]["status"], "SUBMITTED")
        self.assertTrue(reporter.read_json(reporter.OUTBOX, [])[-1]["email_sent"])      # WhatsApp only


class Chatlog(unittest.TestCase):
    def setUp(self):
        reset()
        self.addCleanup(reset)

    def test_messages_in_both_directions_are_kept_in_full_and_the_last_n_returned(self):
        for i in range(60):
            chatlog.add("agent", f"subject {i}", "x" * 3000 + str(i))
        chatlog.add("raja", "", "hello")
        last = chatlog.last(50)
        self.assertEqual(len(last), 50)
        self.assertEqual(last[-1]["who"], "raja")
        self.assertEqual(len(last[-2]["text"]), 3000 + 2)          # nothing is cut when stored
        self.assertEqual(len(chatlog.last(1000)), 61)

    def test_delivered_whatsapp_messages_are_logged_by_the_reporter(self):
        reporter.enqueue("notice", "Hello", "Full body here", email=False)
        reporter.process(send_email=lambda *a: None, send_whatsapp=lambda t: True, send_buttons=lambda *a: True)
        self.assertEqual([m["subject"] for m in chatlog.last()], ["Hello"])
        self.assertIn("Full body here", chatlog.last()[0]["text"])

    def test_a_message_that_was_not_delivered_is_not_logged(self):
        reporter.enqueue("notice", "Hello", "body", email=False)
        reporter.process(send_email=lambda *a: None, send_whatsapp=lambda t: False, send_buttons=lambda *a: False)
        self.assertEqual(chatlog.last(), [])

    def test_the_first_use_starts_from_what_the_outbox_still_holds(self):
        write_json(reporter.OUTBOX, [{"subject": "Old", "body": "old body", "whatsapp": "sent", "email_sent": True}])
        self.assertEqual([m["subject"] for m in chatlog.last()], ["Old"])

    def test_what_raja_types_is_logged_after_it_is_handled(self):
        write_json("whatsapp_inbox.json", [{"ts": 1, "text": f"APPLIED 4490945016", "via": "text", "handled": False}])
        write_json(queue_tool.FILE, [{"job_id": "4490945016", "title": "CTO", "company": "NET", "status": "AWAITING RAJA",
                                      "queued_at": "2026-11-16T20:43:21+00:00"}])
        approvals.process_whatsapp()
        self.assertEqual(chatlog.last()[-1]["who"], "raja")
        self.assertEqual(chatlog.last()[-1]["text"], "APPLIED 4490945016")


if __name__ == "__main__":
    unittest.main()
