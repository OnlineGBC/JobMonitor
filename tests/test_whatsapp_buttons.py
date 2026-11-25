import hashlib
import hmac
import json
import os
import tempfile
import unittest
from types import SimpleNamespace

os.environ.setdefault("AGENT_DATA_DIR", tempfile.mkdtemp())

from agent import approvals, outreach, reporter, vault, whatsapp, webhook  # noqa: E402
from agent.store import read_json, write_json  # noqa: E402

FAKE = {"wa_phone_number_id": "111", "wa_access_token": "tok", "wa_recipient": "12125551234",
        "wa_app_secret": "shh", "wa_verify_token": "vt"}
URL = "https://www.linkedin.com/in/codyarice/"
BUTTONS = [("APPROVE abc12345", "Approve"), ("REJECT abc12345", "Reject"), ("PENDING", "Pending list")]


class Base(unittest.TestCase):
    def setUp(self):
        self._g, self._r = vault.get, vault.require
        vault.get, vault.require = FAKE.get, (lambda n: FAKE[n])
        for f in ("whatsapp_state.json", "whatsapp_inbox.json"):
            write_json(f, {} if f.endswith("state.json") else [])
        for f in ("outbox.json", "outreach.json", "invite_watch.json", "approvals_seen.json", "tracker.json",
                  "pending_survivors.json"):
            write_json(f, [])
        self.sent = []
        outer = self

        class Session:
            def post(self, url, headers=None, json=None, timeout=None):
                outer.sent.append(json)
                return SimpleNamespace(status_code=200, json=lambda: {"ok": 1}, text="")
        self.session = Session()

    def tearDown(self):
        vault.get, vault.require = self._g, self._r

    def open_window(self):
        whatsapp.note_inbound(now=1_000_000)
        return 1_000_000 + 60


class SendButtons(Base):
    def test_the_message_has_the_shape_whatsapp_requires(self):
        now = self.open_window()
        self.assertTrue(whatsapp.send_buttons("Approve this?", BUTTONS, self.session, now=now))
        (msg,) = self.sent
        self.assertEqual((msg["messaging_product"], msg["to"], msg["type"]), ("whatsapp", "12125551234", "interactive"))
        i = msg["interactive"]
        self.assertEqual((i["type"], i["body"]["text"]), ("button", "Approve this?"))
        self.assertEqual([(b["reply"]["id"], b["reply"]["title"]) for b in i["action"]["buttons"]], BUTTONS)
        self.assertTrue(all(b["type"] == "reply" for b in i["action"]["buttons"]))

    def test_whatsapps_limits_are_enforced_before_anything_is_sent(self):
        now = self.open_window()
        bad = [
            ("x", [("a", "1"), ("b", "2"), ("c", "3"), ("d", "4")]),          # more than 3 buttons
            ("x", []),                                                        # none
            ("x", [("a", "t" * 21)]),                                         # title over 20
            ("x", [("a" * 257, "ok")]),                                       # id over 256
            ("x", [("a", "same"), ("a", "same2")]),                           # duplicate ids
            ("y" * 1025, [("a", "ok")]),                                      # body over 1024
            ("", [("a", "ok")]),                                              # empty body
        ]
        for body, buttons in bad:
            with self.assertRaises(ValueError):
                whatsapp.send_buttons(body, buttons, self.session, now=now)
        self.assertEqual(self.sent, [])

    def test_a_closed_window_sends_nothing(self):
        self.assertFalse(whatsapp.send_buttons("hi", BUTTONS, self.session, now=5_000_000))
        self.assertEqual(self.sent, [])


def pairs(payload):
    return [e[:2] for e in whatsapp.parse_inbound_events(payload)]


class Taps(Base):
    def payload(self, sender="12125551234", **msg):
        return {"entry": [{"changes": [{"value": {"messages": [{"from": sender, **msg}]}}]}]}

    def test_a_tapped_button_becomes_its_command(self):
        tap = self.payload(type="interactive",
                           interactive={"type": "button_reply", "button_reply": {"id": "APPROVE abc12345", "title": "Approve"}})
        self.assertEqual(pairs(tap), [("12125551234", "APPROVE abc12345")])
        lst = self.payload(type="interactive", interactive={"type": "list_reply", "list_reply": {"id": "PENDING", "title": "x"}})
        self.assertEqual(pairs(lst), [("12125551234", "PENDING")])

    def test_typed_replies_still_work_and_strangers_are_still_ignored(self):
        typed = self.payload(type="text", text={"body": "APPROVE abc12345"})
        self.assertEqual(pairs(typed), [("12125551234", "APPROVE abc12345")])
        stranger = self.payload(sender="19998887777", type="interactive",
                                interactive={"type": "button_reply", "button_reply": {"id": "APPROVE abc12345", "title": "A"}})
        self.assertEqual(pairs(stranger), [])

    def test_end_to_end_a_signed_tap_on_approve_approves_the_draft(self):
        from flask import Flask
        d = outreach.create("invite", "1", "Cody Rice", URL, "Acme", "VP of AI")
        app = Flask(__name__)
        webhook.register(app)
        body = json.dumps(self.payload(type="interactive", interactive={
            "type": "button_reply", "button_reply": {"id": f"APPROVE {d['id']}", "title": "Approve"}})).encode()
        sig = "sha256=" + hmac.new(b"shh", body, hashlib.sha256).hexdigest()
        r = app.test_client().post("/whatsapp/webhook", data=body, content_type="application/json",
                                   headers={"X-Hub-Signature-256": sig})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(approvals.process_whatsapp(), 1)
        self.assertEqual(outreach.get(d["id"])["status"], "APPROVED")
        self.assertEqual(outreach.get(d["id"])["text"], d["text"])        # approved exactly as shown

    def test_a_tap_on_a_stale_button_changes_nothing(self):
        d = outreach.create("invite", "1", "Cody Rice", URL, "Acme", "VP of AI")
        outreach.reject(d["id"])
        write_json("whatsapp_inbox.json", [{"ts": 1, "text": f"APPROVE {d['id']}", "handled": False}])
        approvals.process_whatsapp()
        self.assertEqual(outreach.get(d["id"])["status"], "REJECTED")      # a rejected draft is never approved by a late tap

    def test_the_pending_button_lists_what_is_waiting(self):
        write_json("whatsapp_inbox.json", [{"ts": 1, "text": "PENDING", "handled": False}])
        self.assertEqual(approvals.process_whatsapp(), 1)
        self.assertEqual(read_json("outbox.json", [])[0]["kind"], "notice")


class ReporterDelivery(Base):
    def deliver(self, **event):
        """Run Reporter.process with fakes that record what goes to WhatsApp."""
        write_json("outbox.json", [{"id": "e1", "kind": "draft", "subject": "S", "body": "B", "email_sent": False,
                                    "whatsapp": "pending", "attempts": 0, **event}])
        calls = []
        stats = reporter.process(
            send_email=lambda s, b: calls.append(("email", s)),
            send_whatsapp=lambda t: calls.append(("text", t)) or True,
            send_buttons=lambda t, b: calls.append(("buttons", t, b)) or True)
        return stats, calls

    def test_an_event_with_buttons_goes_as_one_tappable_message(self):
        stats, calls = self.deliver(buttons=[list(b) for b in BUTTONS], wa_text="the exact text")
        self.assertEqual([c[0] for c in calls], ["email", "buttons"])
        self.assertEqual(calls[1][1:], ("the exact text", BUTTONS))
        self.assertEqual(stats["whatsapp"], 1)

    def test_a_long_text_is_sent_in_full_first_then_the_buttons_follow(self):
        long_text = "word " * 400                                           # over WhatsApp's 1024 for button messages
        stats, calls = self.deliver(buttons=[list(b) for b in BUTTONS], wa_text=long_text, wa_prompt="Tap to answer")
        self.assertEqual([c[0] for c in calls], ["email", "text", "buttons"])
        self.assertEqual(calls[1][1], long_text)                            # never truncated
        self.assertEqual(calls[2][1], "Tap to answer")

    def test_if_buttons_cannot_be_sent_the_plain_text_still_arrives(self):
        write_json("outbox.json", [{"id": "e1", "kind": "draft", "subject": "S", "body": "B", "email_sent": False,
                                    "whatsapp": "pending", "attempts": 0, "buttons": [list(b) for b in BUTTONS],
                                    "wa_text": "the exact text"}])
        calls = []

        def boom(t, b):
            raise RuntimeError("meta rejected the interactive message")
        stats = reporter.process(send_email=lambda s, b: None, send_whatsapp=lambda t: calls.append(t) or True, send_buttons=boom)
        self.assertEqual((calls, stats["whatsapp"]), (["the exact text"], 1))

    def test_a_closed_window_leaves_it_to_email_and_events_without_buttons_are_unchanged(self):
        write_json("outbox.json", [{"id": "e1", "kind": "draft", "subject": "S", "body": "B", "email_sent": False,
                                    "whatsapp": "pending", "attempts": 0, "buttons": [list(b) for b in BUTTONS],
                                    "wa_text": "t"}])
        stats = reporter.process(send_email=lambda s, b: None, send_whatsapp=lambda t: False, send_buttons=lambda t, b: False)
        self.assertEqual((stats["whatsapp_closed"], stats["emailed"]), (1, 1))
        _, calls = self.deliver()                                           # a plain event: no buttons key
        self.assertEqual([c[0] for c in calls], ["email", "text"])
        self.assertEqual(calls[1][1], "S\n\nB")

    def test_impossible_buttons_are_refused_when_the_report_is_queued(self):
        with self.assertRaises(ValueError):
            reporter.enqueue("draft", "S", "B", buttons=[("a", "t" * 30)], wa_prompt="x")
        self.assertEqual(read_json("outbox.json", []), [])


class DraftAnnouncement(Base):
    def test_a_draft_is_announced_with_the_exact_text_and_three_buttons(self):
        d = outreach.create("invite", "1", "Cody Rice", URL, "Acme", "VP of AI")
        outreach.announce(d)
        (ev,) = read_json("outbox.json", [])
        self.assertEqual([tuple(b) for b in ev["buttons"]],
                         [(f"APPROVE {d['id']}", "Approve"), (f"REJECT {d['id']}", "Reject"), ("PENDING", "Pending list")])
        self.assertIn(d["text"], ev["wa_text"])                              # word for word, never summarised
        self.assertLessEqual(len(ev["wa_text"]), whatsapp.BUTTON_BODY_MAX)
        self.assertIn(d["text"], ev["body"])                                 # the email is unchanged
        self.assertIn(f"APPROVE {d['id']}", ev["body"])
        self.assertTrue(ev["subject"].endswith("updates needed"))

    def test_every_button_id_is_a_command_the_approval_code_understands(self):
        d = outreach.create("invite", "1", "Cody Rice", URL, "Acme", "VP of AI")
        outreach.announce(d)
        for button_id, _ in read_json("outbox.json", [])[0]["buttons"]:
            self.assertIsNotNone(approvals.parse(button_id), button_id)

    def test_a_long_follow_up_message_still_shows_its_full_text(self):
        text = outreach.followup_text("Cody Rice", "VP of AI", "Acme") + " " + "x" * 1000   # over the button limit
        d = {"id": "abc12345", "kind": "message", "poster": "Cody Rice", "poster_url": URL, "company": "Acme",
             "title": "VP of AI", "text": text, "requires_applied": False}
        outreach.announce(d)
        (ev,) = read_json("outbox.json", [])
        stats, calls = None, []
        reporter.process(send_email=lambda s, b: None, send_whatsapp=lambda t: calls.append(("text", t)) or True,
                         send_buttons=lambda t, b: calls.append(("buttons", t)) or True)
        self.assertEqual([c[0] for c in calls], ["text", "buttons"])
        self.assertIn(text, calls[0][1])                                      # the full text, then the buttons


if __name__ == "__main__":
    unittest.main()
