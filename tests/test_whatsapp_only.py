import os
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

os.environ.setdefault("AGENT_DATA_DIR", tempfile.mkdtemp())

from agent import applier, reconcile, reporter, tracker  # noqa: E402


class WhatsAppOnly(unittest.TestCase):
    def sent(self, fn):
        with mock.patch.object(reporter, "enqueue") as enq:
            fn()
        return enq.call_args_list

    def row(self, **extra):
        return {"number": 7, "job_id": "123", "company": "Acme", "title": "CTO", "status": "AWAITING RAJA",
                "booked": (datetime.now().astimezone() - timedelta(minutes=90)).isoformat(), "note": "n", **extra}

    def run_reconcile(self, row, applied):
        with mock.patch.object(tracker, "rows", return_value=[row]), mock.patch.object(tracker, "settle"), \
                mock.patch.object(tracker, "mark_reminded"), mock.patch("agent.queue_tool.remove"):
            return self.sent(lambda: reconcile.process(applied, lambda j: True))

    def test_applied_confirmation_is_whatsapp_only(self):
        calls = self.run_reconcile(self.row(), {"123"})
        self.assertEqual(len(calls), 1)
        self.assertIs(calls[0].kwargs["email"], False)

    def test_still_waiting_reminder_is_whatsapp_only(self):
        calls = self.run_reconcile(self.row(), set())
        self.assertIs(calls[0].kwargs["email"], False)

    def test_could_not_be_confirmed_reminder_is_whatsapp_only(self):
        calls = self.run_reconcile(self.row(agent_submitted=True), set())
        self.assertIs(calls[0].kwargs["email"], False)

    def test_needs_your_answer_is_whatsapp_only_but_other_pauses_still_email(self):
        item = {"job_id": "1", "company": "A", "title": "T"}
        with mock.patch.object(tracker, "book"), mock.patch("agent.queue_tool.set_status"):
            q = self.sent(lambda: applier._book_and_report(item, 1, "question", "s", "b", "n", ""))
            p = self.sent(lambda: applier._book_and_report(item, 1, "paused", "s", "b", "n", ""))
        self.assertIs(q[0].kwargs["email"], False)
        self.assertIs(p[0].kwargs["email"], True)


if __name__ == "__main__":
    unittest.main()
