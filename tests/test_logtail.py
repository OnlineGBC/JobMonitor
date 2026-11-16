import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("AGENT_DATA_DIR", tempfile.mkdtemp())

from agent import chatlog, decisions, logtail, queue_tool, reporter, router, tracker  # noqa: E402
from agent.store import data_dir, write_json  # noqa: E402

APPLIER = """Claimed: Lantern | Director, Application & AI Security
Outcome: submitted
Claimed: NET-Group Ltd | Chief Technology Officer
2026-11-16 15:44:45,784 [ERROR] [4490945016] attempt 1 failed: Locator.evaluate: Timeout 30000ms exceeded.
Call log:
  - waiting for locator("dialog[open]").first
Outcome: failed (attempt 1), will retry
Claimed: The Judge Group | Director, AI and Data
Outcome: closed
"""
WEB = """2026-11-16 15:43:21,793 [INFO] whatsapp: DECIDE 9a6c6b9e -> Queued Chief Technology Officer at NET-Group Ltd.
2026-11-16 15:43:22,000 [INFO] 2a03::1 - - "POST /whatsapp/webhook HTTP/1.1" 200 - NET-Group
2026-11-16 15:44:00,000 [INFO] HTTP Request: POST https://api.anthropic.com/v1/messages NET-Group
"""


class Base(unittest.TestCase):
    def setUp(self):
        logs = data_dir() / "logs"
        logs.mkdir(exist_ok=True)
        (logs / "applier.log").write_text(APPLIER, encoding="utf-8")
        for n in ("discovery.log", "acceptance.log", "sender.log"):
            (logs / n).write_text("", encoding="utf-8")
        web = Path(tempfile.mkdtemp()) / "screen_compare.log"
        web.write_text(WEB, encoding="utf-8")
        p = mock.patch.object(logtail, "WEB_LOG", web)
        p.start()
        self.addCleanup(p.stop)
        write_json(queue_tool.FILE, [{"job_id": "4490945016", "title": "Chief Technology Officer", "company": "NET-Group Ltd",
                                      "status": "AWAITING RAJA", "queued_at": "x"}])
        write_json(tracker.FILE, [{"number": 7, "company": "The Judge Group", "title": "Director, AI and Data",
                                   "job_id": "4490687654", "status": "SUBMITTED", "url": "u"}])
        write_json(decisions.FILE, [])
        self.addCleanup(lambda: [write_json(queue_tool.FILE, []), write_json(tracker.FILE, [])])
        p2 = os.path.join(os.environ["AGENT_DATA_DIR"], chatlog.FILE)
        if os.path.exists(p2):
            os.remove(p2)
        self.addCleanup(lambda: os.path.exists(p2) and os.remove(p2))


class Excerpts(Base):
    def test_the_named_company_matches_and_other_companies_do_not(self):
        out = logtail.excerpts("What went wrong with NET-group? Can you check logs?")
        self.assertIn("attempt 1 failed: Locator.evaluate: Timeout 30000ms exceeded", out)
        self.assertIn('waiting for locator("dialog[open]")', out)
        self.assertIn("== applier.log ==", out)
        self.assertNotIn("Claimed: The Judge Group", out)          # another company's run is not included

    def test_a_company_suffix_word_alone_does_not_match_other_companies(self):
        self.assertEqual(logtail.terms_for("what about the group thing"), set())
        self.assertIn("the judge group", logtail.terms_for("any news on Judge?"))

    def test_noise_lines_are_left_out(self):
        out = logtail.excerpts("net-group")
        self.assertIn("DECIDE 9a6c6b9e", out)
        self.assertNotIn("POST /whatsapp/webhook", out)
        self.assertNotIn("HTTP Request", out)

    def test_asking_for_logs_with_no_role_gives_the_latest_lines(self):
        out = logtail.excerpts("check the logs please")
        self.assertIn("== applier.log ==", out)
        self.assertIn("Claimed: The Judge Group", out)

    def test_nothing_relevant_gives_none(self):
        self.assertEqual(logtail.excerpts("hello there my friend"), "(none)")


class Links(Base):
    def test_a_link_with_its_last_digits_cut_off_is_repaired(self):
        fixed = logtail.fix_links("check https://www.linkedin.com/jobs/view/4490945")
        self.assertEqual(fixed, "check https://www.linkedin.com/jobs/view/4490945016/")

    def test_a_correct_link_is_left_alone_and_an_unknown_one_is_removed(self):
        text = "a https://www.linkedin.com/jobs/view/4490945016/ b https://www.linkedin.com/jobs/view/1234567890"
        self.assertEqual(logtail.fix_links(text), "a https://www.linkedin.com/jobs/view/4490945016/ b (link not available)")

    def test_links_the_system_sent_count_as_known_but_model_written_replies_do_not(self):
        chatlog.add("agent", "New Easy Apply role", "see https://www.linkedin.com/jobs/view/5550001111")
        chatlog.add("agent", "Reply", "see https://www.linkedin.com/jobs/view/5550002222")
        self.assertIn("5550001111", logtail.known_ids())
        self.assertNotIn("5550002222", logtail.known_ids())


class InTheRouter(Base):
    def test_logs_reach_the_model_and_the_reply_link_is_repaired(self):
        reply = {"actions": [], "comment": False, "ask": None,
                 "reply": "The window never opened. https://www.linkedin.com/jobs/view/4490945"}
        r = SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(reply))], stop_reason="end_turn")
        c = SimpleNamespace(messages=SimpleNamespace(create=mock.Mock(return_value=r)))
        out = router.route("What went wrong with NET-group? Can you check logs?", client=c)
        prompt = c.messages.create.call_args.kwargs["messages"][0]["content"]
        self.assertIn('waiting for locator("dialog[open]")', prompt)
        self.assertTrue(out["reply"].endswith("https://www.linkedin.com/jobs/view/4490945016/"))


if __name__ == "__main__":
    unittest.main()
