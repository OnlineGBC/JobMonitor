import os
import tempfile
import unittest

os.environ.setdefault("AGENT_DATA_DIR", tempfile.mkdtemp())

from agent import acceptance_check, outreach  # noqa: E402
from agent.store import read_json, write_json  # noqa: E402

URL = "https://www.linkedin.com/in/codyarice/"


class FakeOps:
    def __init__(self, **state):
        self.s = {"wrong_person": False, "degree": "3rd", "pending": True, **state}
        self.calls = []

    def state(self, url, name):
        self.calls.append((url, name))
        return self.s


class Acceptance(unittest.TestCase):
    def setUp(self):
        for f in ("outreach.json", "invite_watch.json", "outbox.json"):
            write_json(f, [])

    def watch(self, applied=True, url=URL, job="1"):
        outreach.watch(url, "Cody Rice", job, "Acme", "VP of AI", applied=applied)

    def test_an_accepted_invitation_drafts_a_follow_up_and_tells_raja(self):
        self.watch()
        out = acceptance_check.process(FakeOps(degree="1st", pending=False))
        self.assertEqual(out[0][1], "accepted: follow-up drafted")
        self.assertTrue(outreach.watch_list()[0]["accepted"])
        d = outreach.all_items()[0]
        self.assertEqual((d["kind"], d["status"]), ("message", "DRAFT"))   # nothing is sent: it waits for approval
        self.assertEqual(outreach.problems(d["text"], outreach.MESSAGE_LIMIT), [])
        ev = read_json("outbox.json", [])[0]
        self.assertEqual(ev["kind"], "draft")
        self.assertIn(d["text"], ev["body"])
        self.assertTrue(ev["subject"].endswith("updates needed"))

    def test_the_follow_up_only_says_applied_when_there_was_an_application(self):
        self.watch(applied=True)
        acceptance_check.process(FakeOps(degree="1st", pending=False))
        self.assertIn("I applied for the VP of AI role", outreach.all_items()[0]["text"])
        for f in ("outreach.json", "invite_watch.json"):
            write_json(f, [])
        self.watch(applied=False)
        acceptance_check.process(FakeOps(degree="1st", pending=False))
        text = outreach.all_items()[0]["text"]
        self.assertNotIn("applied", text)
        self.assertIn("I came across the VP of AI role at Acme", text)
        self.assertEqual(outreach.problems(text, outreach.MESSAGE_LIMIT), [])

    def test_an_accepted_invitation_is_handled_once(self):
        self.watch()
        ops = FakeOps(degree="1st", pending=False)
        acceptance_check.process(ops)
        acceptance_check.process(ops)
        self.assertEqual(len(ops.calls), 1)                 # no longer watched
        self.assertEqual(len(outreach.all_items()), 1)      # one draft, one report
        self.assertEqual(len(read_json("outbox.json", [])), 1)

    def test_a_pending_invitation_changes_nothing(self):
        self.watch()
        self.assertEqual(acceptance_check.process(FakeOps())[0][1], "still pending")
        self.assertEqual(outreach.all_items(), [])
        self.assertEqual(read_json("outbox.json", []), [])

    def test_a_declined_invitation_is_closed_only_after_three_misses_in_a_row(self):
        self.watch()
        gone = FakeOps(pending=False)
        self.assertIn("will check again", acceptance_check.process(gone)[0][1])
        self.assertIn("will check again", acceptance_check.process(gone)[0][1])
        self.assertFalse(outreach.watch_list()[0]["closed"])
        self.assertIn("watch closed", acceptance_check.process(gone)[0][1])
        self.assertTrue(outreach.watch_list()[0]["closed"])
        self.assertEqual((outreach.all_items(), read_json("outbox.json", [])), ([], []))  # no follow-up for a decline
        self.assertEqual(acceptance_check.process(gone), [])                              # and it is not checked again

    def test_one_odd_page_load_does_not_end_the_watch(self):
        self.watch()
        acceptance_check.process(FakeOps(pending=False))
        acceptance_check.process(FakeOps(pending=False))
        acceptance_check.process(FakeOps())                  # pending again: the misses reset
        acceptance_check.process(FakeOps(pending=False))
        self.assertFalse(outreach.watch_list()[0]["closed"])
        self.assertEqual(outreach.watch_list()[0]["misses"], 1)

    def test_the_wrong_page_or_an_error_never_changes_the_watch(self):
        self.watch()
        self.assertEqual(acceptance_check.process(FakeOps(wrong_person=True, degree="1st"))[0][1], "wrong page")
        w = outreach.watch_list()[0]
        self.assertEqual((w["accepted"], w["closed"], w["misses"]), (False, False, 0))
        self.assertEqual(outreach.all_items(), [])

        class Boom(FakeOps):
            def state(self, url, name):
                raise RuntimeError("page did not load")
        self.assertEqual(acceptance_check.process(Boom())[0][1], "error")
        self.assertEqual(outreach.watch_list()[0]["misses"], 0)

    def test_login_expiry_propagates(self):
        self.watch()

        class Expired(FakeOps):
            def state(self, url, name):
                raise acceptance_check.LoginNeeded()
        with self.assertRaises(acceptance_check.LoginNeeded):
            acceptance_check.process(Expired())

    def test_two_watched_people_are_handled_independently(self):
        self.watch()
        outreach.watch("https://www.linkedin.com/in/other/", "Pat Lee", "2", "Beta", "Director of AI", applied=True)

        class Mixed(FakeOps):
            def state(self, url, name):
                return {"wrong_person": False, "degree": "1st" if "other" in url else "3rd",
                        "pending": "other" not in url}
        out = dict(acceptance_check.process(Mixed()))
        self.assertEqual(out["Cody Rice at Acme"], "still pending")
        self.assertEqual(out["Pat Lee at Beta"], "accepted: follow-up drafted")


if __name__ == "__main__":
    unittest.main()
