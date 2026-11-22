import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("AGENT_DATA_DIR", tempfile.mkdtemp())

from agent import approvals, outreach, questions, reporter, router, whatsapp  # noqa: E402
from agent.store import write_json  # noqa: E402
from tests.test_router import Base, client  # noqa: E402


class Session:
    def __init__(self):
        self.n = 0

    def post(self, url, **kw):
        self.n += 1
        return mock.Mock(status_code=200, json=lambda: {"messages": [{"id": f"wamid.{self.n}"}]}, text="")


class SentIdsRemembered(unittest.TestCase):
    def setUp(self):
        write_json(whatsapp.REFS, {})
        write_json(whatsapp.STATE, {"last_inbound": 1000})
        p = mock.patch.object(whatsapp.vault, "require", return_value="x")
        p.start()
        self.addCleanup(p.stop)

    def test_a_message_sent_for_an_item_is_remembered_against_it(self):
        s = Session()
        with whatsapp.sending_for("question abcd1234"):
            whatsapp.send_text("hi", s, now=1100)
        whatsapp.send_text("other", s, now=1100)
        self.assertEqual(whatsapp.ref_for("wamid.1"), "question abcd1234")
        self.assertIsNone(whatsapp.ref_for("wamid.2"))
        self.assertIsNone(whatsapp.ref_for(None))

    def test_the_reply_target_is_read_from_the_webhook(self):
        with mock.patch.object(whatsapp.vault, "get", return_value="12125551234"):
            payload = {"entry": [{"changes": [{"value": {"messages": [
                {"from": "12125551234", "type": "text", "text": {"body": "20"}, "context": {"id": "wamid.1"}},
                {"from": "12125551234", "type": "text", "text": {"body": "hi"}}]}}]}]}
            ev = whatsapp.parse_inbound_events(payload)
        self.assertEqual([e[3] for e in ev], ["wamid.1", None])

    def test_the_outbox_event_carries_the_ref_and_delivery_remembers_it(self):
        write_json(reporter.OUTBOX, [])
        reporter.enqueue("question", "s", "b", ref="question abcd1234")
        seen = []
        reporter.process(send_email=lambda *a: None,
                         send_whatsapp=lambda t: seen.append(whatsapp._sending_for) or True,
                         send_buttons=lambda *a: True)
        self.assertEqual(seen, ["question abcd1234"])
        self.assertIsNone(whatsapp._sending_for)


class RepliesAreMatched(Base):
    def reply(self, text, about, routed=None, down=False):
        route = mock.patch("agent.router.route", side_effect=RuntimeError("down")) if down else \
            mock.patch("agent.router.route", return_value=routed)
        with route as r:
            approvals.handle_reply(text, "text", "whatsapp", about=about)
        return r

    def test_the_model_is_told_which_message_he_replied_to(self):
        r = self.reply("20", f"question {self.q_text['id']}", {"commands": [], "comment": False, "ask": None})
        self.assertEqual(r.call_args.kwargs["about"], f"question {self.q_text['id']}")

    def test_a_reply_to_a_question_is_its_answer_when_the_model_is_down(self):
        self.reply("20", f"question {self.q_text['id']}", down=True)
        self.assertEqual(questions.get(self.q_text["id"])["answer"], "20")

    def test_ok_alone_is_still_ignored_without_a_reply_target_but_read_with_one(self):
        r = self.reply("ok", None, {"commands": [], "comment": False, "ask": None})
        r.assert_not_called()
        r = self.reply("ok", "draft abcd1234", {"commands": [], "comment": False, "ask": None})
        r.assert_called_once()

    def test_process_whatsapp_passes_the_target_of_a_reply(self):
        write_json("whatsapp_inbox.json", [{"ts": 1, "text": "20", "via": "text", "reply_to": "wamid.9", "handled": False}])
        with mock.patch.object(whatsapp, "ref_for", return_value="question x"), \
                mock.patch.object(approvals, "handle_reply", return_value=True) as h:
            approvals.process_whatsapp()
        self.assertEqual(h.call_args.kwargs["about"], "question x")


class DraftReplies(Base):
    def setUp(self):
        super().setUp()
        write_json(outreach.FILE, [])
        self.d = outreach.create("invite", "9", "Jane Roe", "https://www.linkedin.com/in/jane/", "Acme", "Director of AI")
        self.addCleanup(lambda: write_json(outreach.FILE, []))

    def test_ok_approves_the_draft_as_shown(self):
        c = client(actions=[{"type": "approve_draft", "id": self.d["id"], "text": None}], comment=False, ask=None)
        out = router.route("looks good", client=c, about=f"draft {self.d['id']}")
        self.assertEqual(out["commands"], [f"APPROVE {self.d['id']}"])

    def test_his_own_wording_is_sent_instead(self):
        c = client(actions=[{"type": "approve_draft", "id": self.d["id"], "text": "Hi Jane, I applied."}],
                   comment=False, ask=None)
        out = router.route("Hi Jane, I applied.", client=c, about=f"draft {self.d['id']}")
        self.assertEqual(out["commands"], [f"APPROVE {self.d['id']} Hi Jane, I applied."])

    def test_reject(self):
        c = client(actions=[{"type": "reject_draft", "id": self.d["id"]}], comment=False, ask=None)
        out = router.route("no", client=c, about=f"draft {self.d['id']}")
        self.assertEqual(out["commands"], [f"REJECT {self.d['id']}"])

    def test_a_draft_cannot_be_approved_unless_he_replied_to_that_draft(self):
        c = client(actions=[{"type": "approve_draft", "id": self.d["id"], "text": None}], comment=False, ask=None)
        self.assertEqual(router.route("ok", client=c)["commands"], [])
        self.assertEqual(router.route("ok", client=c, about="draft ffffffff")["commands"], [])


if __name__ == "__main__":
    unittest.main()
