import os
import tempfile
import unittest
from datetime import datetime, timedelta

os.environ.setdefault("AGENT_DATA_DIR", tempfile.mkdtemp())

import monitor  # noqa: E402
from agent import discovery, ledger, queue_tool, tracker  # noqa: E402
from agent.store import read_json, write_json  # noqa: E402


class FakePage:
    def __init__(self, cards):
        self.cards = cards

    def evaluate(self, js):
        return self.cards


def card(jid, *lines):
    return {"id": jid, "lines": list(lines), "text": "\n".join(lines)}


class Extract(unittest.TestCase):
    def test_easy_apply_and_applied_come_from_the_card_footer(self):
        page = FakePage([
            card("1", "Chief AI Officer", "Chief AI Officer", "RSP Group", "United States (Remote)",
                 "Actively reviewing applicants", "Applied · Easy Apply"),
            card("2", "VP AI", "VP AI", "Acme", "Texas (Remote)", "Easy Apply"),
            card("3", "Head of AI", "Head of AI", "Beta", "Ohio (Remote)", "2 school alumni work here"),
        ])
        j = {x["job_id"]: x for x in monitor.extract_job_listings_new_layout(page)}
        self.assertTrue(j["1"]["easy_apply"] and j["1"]["applied"])
        self.assertTrue(j["2"]["easy_apply"] and not j["2"]["applied"])
        self.assertFalse(j["3"]["easy_apply"] or j["3"]["applied"])

    def test_the_word_applied_in_a_title_is_not_an_application(self):
        page = FakePage([card("9", "Head of Applied AI", "Head of Applied AI", "Acme", "Texas (Remote)")])
        self.assertFalse(monitor.extract_job_listings_new_layout(page)[0]["applied"])


class Collapse(unittest.TestCase):
    def test_one_role_posted_per_state_becomes_one_entry(self):
        jobs = [{"job_id": str(i), "title": "CTO - FuzeRx", "company": "Fuze Health",
                 "location": f"{s}, United States (Remote)", "url": f"u{i}"}
                for i, s in enumerate(["Texas", "Ohio", "Utah", "Iowa", "Maine", "Idaho"])]
        out = monitor.collapse_jobs(jobs)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["count"], 6)
        text = monitor.format_jobs_for_email(jobs)
        self.assertIn("[6 postings]", text)
        self.assertIn("+2 more", text)
        self.assertEqual(text.count("https://") + text.count("u0"), 1)   # one link, not six

    def test_any_easy_apply_posting_makes_the_entry_easy_apply(self):
        jobs = [{"job_id": "1", "title": "T", "company": "C", "location": "A (Remote)", "url": "u1"},
                {"job_id": "2", "title": "T", "company": "C", "location": "B (Remote)", "url": "u2", "easy_apply": True}]
        self.assertTrue(monitor.collapse_jobs(jobs)[0]["easy_apply"])


class Sections(unittest.TestCase):
    def setUp(self):
        for f in (tracker.FILE, queue_tool.FILE, ledger.FILE):
            write_json(f, [] if f != ledger.FILE else {})
        self.jobs = [
            {"job_id": "10", "title": "Done Role", "company": "A", "location": "X (Remote)", "url": "u10", "easy_apply": True},
            {"job_id": "11", "title": "Queued Role", "company": "B", "location": "X (Remote)", "url": "u11", "easy_apply": True},
            {"job_id": "12", "title": "Rejected Role", "company": "C", "location": "X (Remote)", "url": "u12", "easy_apply": True},
            {"job_id": "13", "title": "Site Role", "company": "D", "location": "X (Remote)", "url": "u13", "easy_apply": False},
            {"job_id": "14", "title": "LinkedIn Says Applied", "company": "E", "location": "X (Remote)", "url": "u14",
             "easy_apply": True, "applied": True},
        ]
        tracker.book(5, "A", "Done Role", "10", "SUBMITTED", url="u10")
        write_json(queue_tool.FILE, [{"job_id": "11", "company": "B", "title": "Queued Role", "status": "QUEUED"}])
        ledger.add("12", "skip: title does not match the search rule")

    def test_three_sections_with_the_right_roles_in_each(self):
        text = monitor.format_jobs_for_email(self.jobs)
        applied = text.split("EASY APPLY, NOT YET APPLIED")[0]
        pending = text.split("EASY APPLY, NOT YET APPLIED")[1].split("OTHER NEW JOBS")[0]
        other = text.split("OTHER NEW JOBS")[1]
        self.assertIn("EASY APPLY, ALREADY APPLIED (2)", applied)
        self.assertIn("Done Role", applied)
        self.assertIn("application #5", applied)
        self.assertIn("LinkedIn Says Applied", applied)
        self.assertIn("Queued Role", pending)
        self.assertIn("queued", pending)
        self.assertIn("Rejected Role", pending)
        self.assertIn("rules would skip it (title does not match", pending)
        self.assertIn("Site Role", other)
        self.assertNotIn("Site Role", applied + pending)

    def test_a_job_is_never_called_applied_without_evidence(self):
        text = monitor.format_jobs_for_email([self.jobs[1]])
        self.assertNotIn("ALREADY APPLIED", text)


class HandOff(unittest.TestCase):
    def setUp(self):
        write_json(discovery.HINTS_FILE, {})
        write_json(ledger.FILE, {})

    def test_only_unapplied_easy_apply_jobs_are_handed_over(self):
        jobs = [{"job_id": "1", "title": "A", "company": "x", "location": "T (Remote)", "url": "u", "easy_apply": True},
                {"job_id": "2", "title": "B", "company": "y", "location": "T (Remote)", "url": "u", "easy_apply": True, "applied": True},
                {"job_id": "3", "title": "C", "company": "z", "location": "T (Remote)", "url": "u", "easy_apply": False}]
        self.assertEqual(monitor.hand_easy_apply_jobs_to_agent(jobs), 1)
        self.assertEqual(list(read_json(discovery.HINTS_FILE, {})), ["1"])

    def test_discovery_turns_hints_into_cards_but_skips_judged_and_stale_ones(self):
        now = datetime(2026, 11, 16, 12, 0)
        entry = lambda hours: {"title": "T", "company": "C", "location": "Texas (Remote)", "url": "u",
                               "added": (now - timedelta(hours=hours)).isoformat()}
        write_json(discovery.HINTS_FILE, {"1": entry(1), "2": entry(80), "3": entry(1), "4": entry(1)})
        ledger.add("3", "queued")
        cards = discovery.monitor_hint_cards(exclude={"4"}, now=now)
        self.assertEqual([c["job_id"] for c in cards], ["1"])
        self.assertEqual(cards[0]["workplace"], "Remote")
        self.assertEqual(cards[0]["url"], "https://www.linkedin.com/jobs/view/1/")


if __name__ == "__main__":
    unittest.main()
