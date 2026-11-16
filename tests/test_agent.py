import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

os.environ["AGENT_DATA_DIR"] = tempfile.mkdtemp()

from agent import core_check, criteria_gate, dup_gate, ledger, next_number, queue_tool  # noqa: E402
from agent.store import read_json, write_json  # noqa: E402


def post(**kw):
    base = dict(job_id="1", title="VP of AI", company="Acme", description="Lead our AI platform.",
                workplace="Remote", location="US")
    base.update(kw)
    return base


class Gate1(unittest.TestCase):
    def ok(self, **kw):
        return criteria_gate.gate1(post(**kw))[0]

    def test_excluded_companies(self):
        for c in ["Jobgether", "Archer", "hackajob", "Axial Search", "Crossover", "Get IT", "Ethos",
                  "Talentify", "DataAnnotation", "Toptal", "Stealth Stanford AI Startup", "Lensa",
                  "Tempus", "Harnham", "Swooped", "RemoteJobsOne"]:
            self.assertFalse(self.ok(company=c), c)

    def test_ladders_eligible_and_exact_match_only(self):
        self.assertTrue(self.ok(company="Ladders"))
        self.assertTrue(self.ok(company="Ethos Group Holdings"))

    def test_title_rules(self):
        self.assertFalse(self.ok(title="Director, Partnerships AI"))
        self.assertFalse(self.ok(title="VP Business Development"))
        self.assertFalse(self.ok(title="Head of Marketing"))
        self.assertFalse(self.ok(title="Head of Revenue Operations"))
        self.assertTrue(self.ok(title="VP Marketing AI"))
        self.assertFalse(self.ok(title="VP Process Mining AI"))
        self.assertFalse(self.ok(title="Director, Process Intelligence"))

    def test_equity_only(self):
        self.assertFalse(self.ok(description="This is equity only."))
        self.assertFalse(self.ok(description="No cash compensation."))
        self.assertTrue(self.ok(description="Compensation not listed."))

    def test_phd(self):
        self.assertFalse(self.ok(description="PhD required."))
        self.assertFalse(self.ok(description="PhD or equivalent experience."))
        self.assertFalse(self.ok(description="Qualifications:\n- PhD in Computer Science"))
        self.assertTrue(self.ok(description="PhD preferred."))
        self.assertTrue(self.ok(description="A doctorate is a plus."))
        self.assertTrue(self.ok(description="Master's or PhD in a related field."))

    def test_no_location_or_leadership_check(self):
        self.assertTrue(self.ok(title="Senior AI Engineer", workplace="On-site"))


class Core(unittest.TestCase):
    def fake(self, reply):
        create = lambda **kw: SimpleNamespace(content=[SimpleNamespace(text=reply)])
        return SimpleNamespace(messages=SimpleNamespace(create=create))

    def test_core_word_title_unjudged(self):
        self.assertTrue(core_check.judge(post(title="VP, Machine Learning"), client=object())[0])
        self.assertTrue(core_check.judge(post(title="Head of Analytics"), client=object())[0])

    def test_judged(self):
        p = post(title="Chief Technology Officer")
        self.assertTrue(core_check.judge(p, self.fake('{"core": true, "reason": "x"}'))[0])
        self.assertFalse(core_check.judge(p, self.fake('{"core": false, "reason": "x"}'))[0])

    def test_failure_raises(self):
        p = post(title="Chief Technology Officer")
        with self.assertRaises(core_check.CoreCheckError):
            core_check.judge(p, self.fake("not json"))
        with self.assertRaises(core_check.CoreCheckError):
            core_check.judge(post(title="CTO", description=""), client=object())


class Gate2(unittest.TestCase):
    def test_dup(self):
        t = [{"company": "Acme, Inc.", "title": "VP of AI", "number": 7}]
        self.assertTrue(dup_gate.is_duplicate("acme inc", "VP of AI!", t)[0])
        self.assertFalse(dup_gate.is_duplicate("Other Co", "VP of AI", t)[0])
        self.assertFalse(dup_gate.is_duplicate("Acme, Inc.", "Director of AI", t)[0])


class Ledger(unittest.TestCase):
    def test_first_outcome_stands(self):
        ledger.add("L1", "queued")
        ledger.add("L1", "skipped")
        self.assertEqual(read_json("seen_jobs.json", {})["L1"]["outcome"], "queued")
        self.assertEqual(ledger.new_ids(["L1", "L2"]), ["L2"])


class Queue(unittest.TestCase):
    def setUp(self):
        write_json("pending_survivors.json", [])

    def test_hybrid_held_out_unless_forced(self):
        self.assertFalse(queue_tool.add(post(job_id="h", workplace="Hybrid"))[0])
        self.assertTrue(queue_tool.add(post(job_id="h", workplace="Hybrid"), force=True)[0])

    def test_twin_dropped(self):
        self.assertTrue(queue_tool.add(post(job_id="a"))[0])
        self.assertEqual(queue_tool.add(post(job_id="b"))[1], "queue twin")

    def test_claim_oldest_and_company_block(self):
        queue_tool.add(post(job_id="a", company="A", title="VP AI"))
        queue_tool.add(post(job_id="b", company="A", title="Director AI"))
        queue_tool.add(post(job_id="c", company="C", title="VP AI"))
        self.assertEqual(queue_tool.claim()["job_id"], "a")
        self.assertEqual(queue_tool.claim()["job_id"], "c")  # b blocked: same company as live claim
        self.assertIsNone(queue_tool.claim())

    def test_stale_claim_reclaimable_and_holds_skipped(self):
        queue_tool.add(post(job_id="a", company="A"))
        queue_tool.add(post(job_id="b", company="B", title="Head of AI"))
        queue_tool.claim()
        items = read_json("pending_survivors.json", [])
        items[0]["claimed_at"] = (datetime.now(timezone.utc) - timedelta(minutes=71)).isoformat()
        write_json("pending_survivors.json", items)
        queue_tool.set_status("b", "AWAITING RAJA")
        self.assertEqual(queue_tool.claim()["job_id"], "a")
        self.assertIsNone(queue_tool.claim())


class Numbers(unittest.TestCase):
    def test_sequence(self):
        next_number.set_next(100)
        self.assertEqual((next_number.reserve(), next_number.reserve()), (100, 101))


class Discovery(unittest.TestCase):
    def setUp(self):
        write_json("pending_survivors.json", [])
        from agent import discovery
        self.d = discovery
        self.card = dict(job_id="9", title="VP of AI", company="Acme", url="u")

    def detail(self, **kw):
        base = dict(loaded=True, title="VP of AI", company="Acme", description="Lead AI.",
                    workplace="Remote", not_accepting=False)
        base.update(kw)
        return base

    def test_url_and_keywords(self):
        u = self.d.build_search_url()
        for part in ("f_TPR=r86400", "f_WT=2", "sortBy=DD", "geoId=103644278"):
            self.assertIn(part, u)
        self.assertIn("start=25", self.d.build_search_url(2))
        k = self.d.search_keywords()
        self.assertIn('title:"CAIO"', k)
        self.assertIn('title:"Artificial Intelligence"', k)

    def test_pipeline(self):
        never = core_check.judge  # AI-word titles pass unjudged, so no API client is needed
        self.assertEqual(self.d.decide(self.card, self.detail(), never, [])[0], "queue")
        self.assertEqual(self.d.decide(self.card, self.detail(workplace="Hybrid"), never, [])[0], "held")
        self.assertEqual(self.d.decide(self.card, self.detail(not_accepting=True), never, [])[0], "closed")
        self.assertEqual(self.d.decide(self.card, self.detail(company="Jobgether"), never, [])[0], "error")
        self.assertEqual(self.d.decide({**self.card, "company": "Jobgether"},
                                       self.detail(company="Jobgether"), never, [])[0], "skip")
        self.assertEqual(self.d.decide(self.card, self.detail(loaded=False), never, [])[0], "error")
        tracker = [{"company": "Acme", "title": "VP of AI", "number": 1}]
        self.assertEqual(self.d.decide(self.card, self.detail(), never, tracker)[0], "skip")

    def test_judgment_failure_is_error_not_skip(self):
        def boom(p):
            raise core_check.CoreCheckError("api down")
        card = {**self.card, "title": "Chief Technology Officer"}
        det = self.detail(title="Chief Technology Officer")
        self.assertEqual(self.d.decide(card, det, boom, [])[0], "error")
        self.assertEqual(self.d.decide(card, det, lambda p: (False, "tool only"), [])[0], "skip")


class WhatsApp(unittest.TestCase):
    FAKE = {"wa_phone_number_id": "111", "wa_access_token": "tok", "wa_recipient": "12125551234",
            "wa_app_secret": "shh"}

    def setUp(self):
        from agent import vault, whatsapp
        self.w = whatsapp
        self._g, self._r = vault.get, vault.require
        vault.get = self.FAKE.get
        vault.require = lambda n: self.FAKE[n]
        write_json("whatsapp_state.json", {})
        self.sent = []
        outer = self

        class S:
            def post(self, url, headers=None, json=None, timeout=None):
                outer.sent.append((url, headers, json))
                return SimpleNamespace(status_code=200, json=lambda: {"ok": 1}, text="")
        self.session = S()

    def tearDown(self):
        from agent import vault
        vault.get, vault.require = self._g, self._r

    def test_text_blocked_until_inbound_then_sent(self):
        self.assertFalse(self.w.send_text("hi", self.session, now=1000))
        self.assertEqual(self.sent, [])
        self.w.note_inbound(now=1000)
        self.assertTrue(self.w.send_text("hi", self.session, now=1000 + 23 * 3600))
        self.assertEqual(self.sent[0][2]["to"], "12125551234")
        self.assertEqual(self.sent[0][1]["Authorization"], "Bearer tok")
        self.assertFalse(self.w.send_text("late", self.session, now=1000 + 25 * 3600))

    def test_keepalive_once_per_12h(self):
        self.assertTrue(self.w.send_keepalive(self.session, now=5000))
        self.assertFalse(self.w.send_keepalive(self.session, now=5000 + 11 * 3600))
        self.assertTrue(self.w.send_keepalive(self.session, now=5000 + 12 * 3600))
        self.assertEqual(self.sent[0][2]["type"], "template")

    def test_signature_and_sender_filter(self):
        import hashlib, hmac
        body = b'{"x":1}'
        good = "sha256=" + hmac.new(b"shh", body, hashlib.sha256).hexdigest()
        self.assertTrue(self.w.valid_signature(body, good))
        self.assertFalse(self.w.valid_signature(body, "sha256=bad"))
        self.assertFalse(self.w.valid_signature(body, ""))
        payload = {"entry": [{"changes": [{"value": {"messages": [
            {"from": "12125551234", "text": {"body": "APPROVE 7"}},
            {"from": "19998887777", "text": {"body": "APPROVE 8"}}]}}]}]}
        self.assertEqual(self.w.parse_inbound(payload), [("12125551234", "APPROVE 7")])


class Webhook(unittest.TestCase):
    def setUp(self):
        from flask import Flask
        from agent import vault, webhook
        self.fake = {"wa_verify_token": "vt", "wa_recipient": "12125551234", "wa_app_secret": "shh"}
        self._g, self._r = vault.get, vault.require
        vault.get = self.fake.get
        vault.require = lambda n: self.fake[n]
        write_json("whatsapp_state.json", {})
        write_json("whatsapp_inbox.json", [])
        app = Flask(__name__)
        webhook.register(app)
        self.c = app.test_client()

    def tearDown(self):
        from agent import vault
        vault.get, vault.require = self._g, self._r

    def sign(self, body):
        import hashlib, hmac
        return "sha256=" + hmac.new(b"shh", body, hashlib.sha256).hexdigest()

    def test_verification(self):
        ok = self.c.get("/whatsapp/webhook?hub.mode=subscribe&hub.verify_token=vt&hub.challenge=abc")
        self.assertEqual((ok.status_code, ok.get_data(as_text=True)), (200, "abc"))
        self.assertEqual(self.c.get("/whatsapp/webhook?hub.mode=subscribe&hub.verify_token=no&hub.challenge=abc").status_code, 403)
        self.assertEqual(self.c.get("/whatsapp/webhook").status_code, 403)

    def test_post_requires_signature_and_opens_window(self):
        from agent import whatsapp
        body = (b'{"entry":[{"changes":[{"value":{"messages":[{"from":"12125551234",'
                b'"text":{"body":"APPROVE 7"}}]}}]}]}')
        self.assertEqual(self.c.post("/whatsapp/webhook", data=body, content_type="application/json").status_code, 403)
        self.assertFalse(whatsapp.window_open())
        r = self.c.post("/whatsapp/webhook", data=body, content_type="application/json",
                        headers={"X-Hub-Signature-256": self.sign(body)})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(whatsapp.window_open())
        self.assertEqual(read_json("whatsapp_inbox.json", [])[0]["text"], "APPROVE 7")

    def test_stranger_is_ignored(self):
        from agent import whatsapp
        body = (b'{"entry":[{"changes":[{"value":{"messages":[{"from":"19998887777",'
                b'"text":{"body":"APPROVE 7"}}]}}]}]}')
        r = self.c.post("/whatsapp/webhook", data=body, content_type="application/json",
                        headers={"X-Hub-Signature-256": self.sign(body)})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(whatsapp.window_open())
        self.assertEqual(read_json("whatsapp_inbox.json", []), [])


class Gmail(unittest.TestCase):
    def fake_service(self, sent):
        import base64
        outer = self

        class Exec:
            def __init__(self, v): self.v = v
            def execute(self): return self.v

        class Messages:
            def send(self, userId, body):
                sent.append(body)
                return Exec({"id": "m1"})

            def list(self, userId, q, maxResults):
                outer.q = q
                return Exec({"messages": [{"id": "a"}]})

            def get(self, userId, id, format, metadataHeaders=None):
                if format == "metadata":
                    return Exec({"threadId": "t", "snippet": "hi", "payload": {"headers": [
                        {"name": "Subject", "value": "APPROVE 7"}, {"name": "From", "value": "r@x.com"},
                        {"name": "Date", "value": "today"}]}})
                enc = base64.urlsafe_b64encode(b"line one").decode()
                return Exec({"payload": {"mimeType": "multipart/alternative", "parts": [
                    {"mimeType": "text/html", "body": {"data": base64.urlsafe_b64encode(b"<p>x</p>").decode()}},
                    {"mimeType": "text/plain", "body": {"data": enc}}]}})

        class Users:
            def messages(self): return Messages()

        class Svc:
            def users(self): return Users()
        return Svc()

    def test_send_builds_message(self):
        import base64
        from email import message_from_bytes
        from agent import gmail_client
        sent = []
        mid = gmail_client.send("Applied: VP AI", "Body text", to="raja@onlinegbc.com",
                                service=self.fake_service(sent))
        self.assertEqual(mid, "m1")
        m = message_from_bytes(base64.urlsafe_b64decode(sent[0]["raw"]))
        self.assertEqual((m["To"], m["Subject"]), ("raja@onlinegbc.com", "Applied: VP AI"))
        self.assertIn("Body text", m.get_payload())

    def test_search_and_text(self):
        from agent import gmail_client
        svc = self.fake_service([])
        r = gmail_client.search("subject:APPROVE", service=svc)
        self.assertEqual(self.q, "subject:APPROVE")
        self.assertEqual((r[0]["subject"], r[0]["from"], r[0]["thread_id"]), ("APPROVE 7", "r@x.com", "t"))
        self.assertEqual(gmail_client.get_text("a", service=svc), "line one")


class Reporter(unittest.TestCase):
    def setUp(self):
        from agent import reporter
        self.r = reporter
        write_json("outbox.json", [])

    def test_only_reportable_kinds(self):
        for bad in ("skipped", "rejection", "match"):
            with self.assertRaises(ValueError):
                self.r.enqueue(bad, "x", "y")
        self.r.enqueue("applied", "Applied: VP AI at Acme", "https://www.linkedin.com/jobs/view/1")

    def test_subject_suffix_and_new_york_time(self):
        self.r.enqueue("applied", "Applied: VP AI", "body")
        self.r.enqueue("question", "Need an answer", "body")
        a, q = read_json("outbox.json", [])
        self.assertEqual(a["subject"], "Applied: VP AI")
        self.assertTrue(q["subject"].endswith("— updates needed"))
        self.assertIn("New York time:", a["body"])

    def test_email_always_whatsapp_when_open(self):
        self.r.enqueue("applied", "A", "b")
        mail, wa = [], []
        st = self.r.process(lambda s, b: mail.append(s), lambda t: wa.append(t) or True)
        self.assertEqual((len(mail), len(wa), st["whatsapp"]), (1, 1, 1))

    def test_window_closed_still_emails(self):
        self.r.enqueue("applied", "A", "b")
        mail = []
        st = self.r.process(lambda s, b: mail.append(s), lambda t: False)
        self.assertEqual((len(mail), st["whatsapp_closed"]), (1, 1))
        self.assertEqual(read_json("outbox.json", [])[0]["whatsapp"], "window_closed")

    def test_email_failure_retries_and_whatsapp_failure_does_not_block(self):
        self.r.enqueue("applied", "A", "b")

        def boom(s, b): raise RuntimeError("gmail down")
        def wa_boom(t): raise RuntimeError("meta down")
        st = self.r.process(boom, lambda t: True)
        self.assertEqual(st["failed"], 1)
        item = read_json("outbox.json", [])[0]
        self.assertFalse(item["email_sent"])
        mail = []
        self.r.process(lambda s, b: mail.append(s), wa_boom)  # retry; whatsapp already sent
        self.assertEqual(len(mail), 1)
        self.r.enqueue("applied", "B", "b")
        mail = []
        st = self.r.process(lambda s, b: mail.append(s), wa_boom)
        self.assertEqual(len(mail), 1)  # email delivered despite WhatsApp error

    def test_keepalive_never_raises(self):
        def boom(): raise RuntimeError("template not approved")
        self.assertFalse(self.r.keepalive(boom))
        self.assertTrue(self.r.keepalive(lambda: True))


if __name__ == "__main__":
    unittest.main()
