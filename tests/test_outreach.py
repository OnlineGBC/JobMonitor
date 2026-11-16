import os
import tempfile
import unittest

os.environ.setdefault("AGENT_DATA_DIR", tempfile.mkdtemp())

from agent import applier, approvals, discovery, outreach, queue_tool, tracker  # noqa: E402
from agent.store import read_json, write_json  # noqa: E402

DOT = "·"
URL = "https://www.linkedin.com/in/codyarice/"


def reset():
    for f in ("outreach.json", "invite_watch.json", "outbox.json", "whatsapp_inbox.json", "approvals_seen.json",
              "pending_survivors.json", "tracker.json"):
        write_json(f, [])


class Wording(unittest.TestCase):
    def test_invite_follows_every_voice_rule(self):
        for poster, title, company in [
            ("Cody Rice", "Director of Software Engineering", "Kimball Midwest"),
            ("Dr. Jane Smith", "Vice President — Enterprise AI – Platform", "Acme — Global"),
            ("Madonna", "Chief AI Officer", "Thanksgiving Holdings"),
            ("Cody Rice", "Senior Vice President of Global Enterprise Artificial Intelligence and Data Science "
                          "Transformation and Strategic Partnerships Across Many Regions", "A Very Long Company Name Inc"),
        ]:
            text = outreach.invite_text(poster, title, company)
            self.assertEqual(outreach.problems(text, outreach.INVITE_LIMIT), [], text)
            self.assertLessEqual(len(text), 300)
            self.assertIn("I would love to discuss next steps.", text)
            self.assertTrue(text.endswith("Thank you, Raja"))
            self.assertNotRegex(text, "[—–]")

    def test_first_name_handles_honorifics_and_empty(self):
        self.assertEqual(outreach.first_name("Dr. Jane Smith"), "Jane")
        self.assertEqual(outreach.first_name("Cody Rice"), "Cody")
        self.assertEqual(outreach.first_name(""), "")
        self.assertTrue(outreach.invite_text("", "VP AI", "Acme").startswith("Hello, "))

    def test_the_checker_catches_each_violation(self):
        good = outreach.invite_text("Cody Rice", "VP of AI", "Acme")
        self.assertEqual(outreach.problems(good, 300), [])
        self.assertTrue(any("dash" in p for p in outreach.problems(good.replace("role", "role —"), 300)))
        self.assertTrue(any("thanks" in p for p in outreach.problems(good.replace("I applied", "Thanks, I applied"), 300)))
        self.assertTrue(any("exact line" in p for p in outreach.problems(good.replace("I would love", "I'd love"), 300)))
        self.assertTrue(any("sign-off" in p for p in outreach.problems(good + " Raja", 300)))
        self.assertTrue(any("characters" in p for p in outreach.problems(good + "x" * 300, 300)))

    def test_followup_wording(self):
        text = outreach.followup_text("Cody Rice", "VP of AI", "Acme")
        self.assertEqual(outreach.problems(text, outreach.MESSAGE_LIMIT), [])
        self.assertIn("thank you for connecting", text)


class Queue(unittest.TestCase):
    def setUp(self):
        reset()

    def make(self, **kw):
        a = dict(kind="invite", job_id="1", poster="Cody Rice", poster_url=URL, company="Acme", title="VP of AI")
        a.update(kw)
        return outreach.create(**a)

    def test_one_draft_per_poster_and_job(self):
        a, b = self.make(), self.make()
        self.assertEqual(a["id"], b["id"])
        self.assertEqual(len(outreach.all_items()), 1)
        self.assertNotEqual(self.make(job_id="2")["id"], a["id"])

    def test_a_draft_that_breaks_the_rules_is_never_created(self):
        with self.assertRaises(ValueError):
            self.make(text="Thanks for your time. I would love to discuss next steps. Thank you, Raja")
        self.assertEqual(outreach.all_items(), [])

    def test_approve_exact_text_edit_and_wrong_state(self):
        d = self.make()
        ok, msg = outreach.approve(d["id"])
        self.assertTrue(ok)
        self.assertEqual(outreach.get(d["id"])["text"], d["text"])
        self.assertFalse(outreach.approve(d["id"])[0])  # cannot approve twice
        e = self.make(job_id="5")
        edited = "Hello Cody, I applied last night. I would love to discuss next steps. Thank you, Raja"
        self.assertTrue(outreach.approve(e["id"], edited)[0])
        self.assertEqual(outreach.get(e["id"])["text"], edited)
        f = self.make(job_id="6")
        ok, msg = outreach.approve(f["id"], "Thanks, call me.")
        self.assertFalse(ok)
        self.assertEqual(outreach.get(f["id"])["status"], "DRAFT")  # a bad edit approves nothing
        self.assertFalse(outreach.approve("zzzzzzzz")[0])

    def test_reject_and_next_approved_order(self):
        a, b = self.make(job_id="1"), self.make(job_id="2")
        outreach.approve(b["id"])
        outreach.approve(a["id"])
        self.assertEqual(outreach.next_approved()["id"], b["id"])  # oldest approval first
        outreach.mark(b["id"], "SENT")
        self.assertEqual(outreach.next_approved()["id"], a["id"])
        self.assertTrue(outreach.reject(a["id"])[0])
        self.assertIsNone(outreach.next_approved())

    def test_watch_list(self):
        outreach.watch(URL, "Cody Rice", "1", "Acme", "VP of AI")
        outreach.watch(URL, "Cody Rice", "1", "Acme", "VP of AI")
        self.assertEqual(len(outreach.watch_list()), 1)
        outreach.mark_accepted(URL, "1")
        self.assertTrue(outreach.watch_list()[0]["accepted"])


class Commands(unittest.TestCase):
    def test_parse(self):
        p = approvals.parse
        self.assertEqual(p("APPROVE abcd1234"), {"action": "APPROVE", "id": "abcd1234", "text": None})
        self.assertEqual(p("approve ABCD1234 ")["id"], "abcd1234")
        self.assertEqual(p("Reject abcd1234")["action"], "REJECT")
        self.assertEqual(p("PENDING")["action"], "PENDING")
        self.assertEqual(p("approve abcd1234 Hello Cody. Thank you, Raja")["text"], "Hello Cody. Thank you, Raja")
        for junk in ("", "ok", "approved", "I approve", "APPROVE", "approve 12", "approve zzzzzzzz", "yes please"):
            self.assertIsNone(p(junk), junk)

    def test_only_the_new_part_of_a_reply_counts(self):
        quoted = "APPROVE abcd1234\n\nOn Sun, Nov 15, 2026 at 10:31 PM Job Agent <x@gmail.com> wrote:\n> REJECT abcd1234"
        self.assertEqual(approvals.parse(quoted)["action"], "APPROVE")
        # a command that exists only inside the quoted history does nothing
        self.assertIsNone(approvals.parse("Looks good, thanks.\n> APPROVE abcd1234"))
        self.assertIsNone(approvals.parse("On Sun, Nov 15 at 10:31 PM Job Agent wrote:\nAPPROVE abcd1234"))
        # mobile signatures are not part of an edited text
        r = approvals.parse("approve abcd1234 Hello Cody. Thank you, Raja\nSent from my iPhone")
        self.assertEqual(r["text"], "Hello Cody. Thank you, Raja")


class Inbound(unittest.TestCase):
    def setUp(self):
        reset()
        self.d = outreach.create("invite", "1", "Cody Rice", URL, "Acme", "VP of AI")

    def test_whatsapp_commands_are_applied_and_marked_handled(self):
        write_json("whatsapp_inbox.json", [
            {"ts": 1, "text": f"APPROVE {self.d['id']}", "handled": False},
            {"ts": 2, "text": "OK", "handled": False},
            {"ts": 3, "text": "what is the weather", "handled": False}])
        self.assertEqual(approvals.process_whatsapp(), 1)
        self.assertEqual(outreach.get(self.d["id"])["status"], "APPROVED")
        self.assertTrue(all(m["handled"] for m in read_json("whatsapp_inbox.json", [])))
        self.assertEqual(approvals.process_whatsapp(), 0)  # nothing is processed twice
        self.assertEqual(read_json("outbox.json", [])[0]["kind"], "notice")

    def gmail(self, messages):
        search = lambda q, max_results=10: [{"id": m["id"], "from": m["from"]} for m in messages]
        text = lambda mid: next(m["text"] for m in messages if m["id"] == mid)
        return search, text

    def test_email_only_from_raja_and_each_message_once(self):
        msgs = [{"id": "m1", "from": "Raja Gopalan <raja@onlinegbc.com>", "text": f"APPROVE {self.d['id']}"},
                {"id": "m2", "from": "Evil <evil@example.com>", "text": f"REJECT {self.d['id']}"}]
        s, t = self.gmail(msgs)
        self.assertEqual(approvals.process_gmail(search=s, get_text=t), 1)
        self.assertEqual(outreach.get(self.d["id"])["status"], "APPROVED")  # the spoofed REJECT did nothing
        self.assertEqual(approvals.process_gmail(search=s, get_text=t), 0)  # same ids are not replayed

    def test_a_forged_display_name_does_not_pass(self):
        msgs = [{"id": "m3", "from": "\"raja@onlinegbc.com\" <evil@example.com>", "text": f"REJECT {self.d['id']}"}]
        s, t = self.gmail(msgs)
        self.assertEqual(approvals.process_gmail(search=s, get_text=t), 0)
        self.assertEqual(outreach.get(self.d["id"])["status"], "DRAFT")

    def test_invalid_edit_is_explained_and_changes_nothing(self):
        write_json("whatsapp_inbox.json", [{"ts": 1, "text": f"APPROVE {self.d['id']} Thanks, call me.", "handled": False}])
        approvals.process_whatsapp()
        self.assertEqual(outreach.get(self.d["id"])["status"], "DRAFT")
        self.assertIn("Not approved", read_json("outbox.json", [])[0]["body"])

    def test_pending_lists_what_is_waiting(self):
        tracker.book(1, "Acme", "VP of AI", "1", "AWAITING RAJA", url="u")
        text = approvals.pending_digest()
        self.assertIn(self.d["id"], text)
        self.assertIn("Application #1", text)


class PosterCapture(unittest.TestCase):
    def test_parse_person_from_real_hiring_team_text(self):
        p = discovery.parse_person(f"Cody Rice {DOT} 3rd Talent Acquisition Professional Job poster", URL)
        self.assertEqual((p["name"], p["degree"], p["job_poster"]), ("Cody Rice", "3rd", True))
        q = discovery.parse_person(f"Christopher Gianni Ottolini {DOT} 3rd Building @BlueJai | FGCU Alumni Job poster", "u")
        self.assertEqual(q["name"], "Christopher Gianni Ottolini")

    def test_only_the_tagged_job_poster_is_used(self):
        hiring = [{"href": "https://www.linkedin.com/in/other/", "text": f"Pat Lee {DOT} 2nd Engineering Manager"},
                  {"href": URL, "text": f"Cody Rice {DOT} 3rd Talent Acquisition Job poster"}]
        self.assertEqual(discovery.job_poster(hiring)["url"], URL)
        self.assertIsNone(discovery.job_poster([hiring[0]]))   # an adjacent contact never gets a draft
        self.assertIsNone(discovery.job_poster(None))

    def test_posting_exposes_the_poster(self):
        d = discovery.parse_posting("VP of AI | Acme | LinkedIn", ["VP of AI", "Remote", "About the job"], "",
                                    [{"href": URL, "text": f"Cody Rice {DOT} 3rd Recruiter Job poster"}])
        self.assertEqual((d["poster"], d["poster_url"], d["poster_degree"]), ("Cody Rice", URL, "3rd"))
        none = discovery.parse_posting("VP of AI | Acme | LinkedIn", ["VP of AI", "Remote", "About the job"], "")
        self.assertIsNone(none["poster_url"])


class ApplierOffersOutreach(unittest.TestCase):
    def setUp(self):
        reset()
        write_json("pending_survivors.json", [])
        next_n = __import__("agent.next_number", fromlist=["x"])
        next_n.set_next(1)
        queue_tool.add({"job_id": "9", "title": "VP of AI", "company": "Acme", "workplace": "Remote"})
        self.item = queue_tool.claim()

    def ops(self, **detail):
        from tests.test_applier import FakeOps
        return FakeOps(detail=detail)

    def test_a_job_poster_gets_a_draft_for_approval_and_nothing_is_sent(self):
        applier.process_item(self.item, self.ops(poster="Cody Rice", poster_url=URL))
        items = outreach.all_items()
        self.assertEqual((len(items), items[0]["status"], items[0]["poster_url"]), (1, "DRAFT", URL))
        drafts = [e for e in read_json("outbox.json", []) if e["kind"] == "draft"]
        self.assertEqual(len(drafts), 1)
        self.assertIn(items[0]["text"], drafts[0]["body"])           # shown word for word
        self.assertIn(f"APPROVE {items[0]['id']}", drafts[0]["body"])
        self.assertTrue(drafts[0]["subject"].endswith("updates needed"))

    def test_no_poster_no_draft(self):
        applier.process_item(self.item, self.ops())
        self.assertEqual(outreach.all_items(), [])
        self.assertEqual([e for e in read_json("outbox.json", []) if e["kind"] == "draft"], [])


if __name__ == "__main__":
    unittest.main()
