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

    def test_phd_context(self):
        amgen = ("Basic Qualifications:\nDoctorate degree and 4 years of relevant experience\nOr\n"
                 "Master's degree and 6 years of relevant experience\nOr\nBachelor's degree and 8 years\n"
                 "Preferred Qualifications:\nPhD in a relevant computational discipline.\n")
        self.assertTrue(self.ok(description=amgen))
        self.assertFalse(self.ok(description="Qualifications:\n- PhD in Computer Science"))
        self.assertFalse(self.ok(description="Basic Qualifications:\nPhD in a quantitative field\n"
                                             "Preferred Qualifications:\nCloud experience"))
        self.assertTrue(self.ok(description="Nice to Have\nPhD in machine learning"))
        # a hard heading after a soft one switches back to required
        self.assertFalse(self.ok(description="Preferred Qualifications:\nMBA\nRequirements:\nPhD in AI"))

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
        for part in ("/jobs/search-results/", "f_TPR=r86400", "f_EA=true", "geoId=103644278"):
            self.assertIn(part, u)
        # the old path redirects into a hidden salary filter; f_WT/sortBy are dropped by LinkedIn
        for part in ("/jobs/search/?", "f_SAL", "f_WT", "sortBy"):
            self.assertNotIn(part, u)
        self.assertIn("start=25", self.d.build_search_url(2))
        k = self.d.search_keywords()
        self.assertIn('title:"CAIO"', k)
        self.assertIn('title:"Artificial Intelligence"', k)

    def test_title_rule(self):
        m = self.d.title_matches_search
        for t in ("VP of AI", "Director, Artificial Intelligence", "Head of AI & Data", "Chief AI Officer",
                  "CTO", "Fractional CIO", "Chief Technology Officer", "SVP, Enterprise AI Platform"):
            self.assertTrue(m(t), t)
        for t in ("Director, Core Infrastructure Engineering", "Chief Data Strategy Officer",
                  "Senior AI Engineer", "VP, Technology and Strategy",
                  "Sr Dir Product - AI & Data Management"):
            self.assertFalse(m(t), t)

    def test_parse_card(self):
        c = self.d.parse_card("4490254044", [
            "Sr Dir Product - AI & Data Management (Verified job)", "Sr Dir Product - AI & Data Management",
            "ACI Worldwide", "United States (Remote)", "3 school alumni work here", "Viewed", "\\ufffd",
            "Be an early applicant"])
        self.assertEqual((c["title"], c["company"], c["workplace"]),
                         ("Sr Dir Product - AI & Data Management", "ACI Worldwide", "Remote"))
        self.assertEqual(c["url"], "https://www.linkedin.com/jobs/view/4490254044/")
        h = self.d.parse_card("1", ["Director, AI", "Director, AI", "Deloitte", "Greater Cleveland (Hybrid)"])
        self.assertEqual((h["company"], h["workplace"]), ("Deloitte", "Hybrid"))

    def test_parse_posting_from_real_page_layout(self):
        lines = ["Skip to search", "OctaPulse-shi", "Chief Data Strategy Officer",
                 "United States \\u00b7 54 minutes ago \\u00b7 4 applicants", "No response insights available yet",
                 "Remote", "Full-time", "Easy Apply", "Save", "About the job", "Role Description ..."]
        d = self.d.parse_posting("Chief Data Strategy Officer | OctaPulse-shi | LinkedIn", lines, "Role Description ...")
        self.assertTrue(d["loaded"])
        self.assertEqual((d["title"], d["company"], d["workplace"], d["apply_type"]),
                         ("Chief Data Strategy Officer", "OctaPulse-shi", "Remote", "Easy Apply"))
        self.assertIn("4 applicants", d["applicants"])
        self.assertFalse(d["not_accepting"])
        # a title containing ' | ' stays intact; a page that is not a posting is not 'loaded'
        d2 = self.d.parse_posting("VP | AI | Acme | LinkedIn", ["VP | AI"], "")
        self.assertEqual((d2["title"], d2["company"]), ("VP | AI", "Acme"))
        self.assertFalse(self.d.parse_posting("LinkedIn Login", [], "")["loaded"])
        closed = self.d.parse_posting("X | Y | LinkedIn", ["Y", "X", "No longer accepting applications"], "")
        self.assertTrue(closed["not_accepting"])

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


class Scheduler(unittest.TestCase):
    def setUp(self):
        from agent import scheduler, reporter
        self.s, self.rep = scheduler, reporter
        write_json("control.json", {})
        write_json("outbox.json", [])
        self.calls = []
        self.t = 1_000_000.0
        # no real sends in tests
        self._p, self._k = reporter.process, reporter.keepalive
        reporter.process = lambda *a, **k: {}
        reporter.keepalive = lambda *a, **k: False

    def tearDown(self):
        self.rep.process, self.rep.keepalive = self._p, self._k

    def sched(self, result=(0, False)):
        def launcher(module, cap, log):
            self.calls.append(module)
            return result
        return self.s.AgentScheduler(clock=lambda: self.t, launcher=launcher)

    def test_everything_paused_by_default(self):
        sch = self.sched()
        self.assertEqual(sch.tick(sync=True), [])
        self.assertTrue(all(not v["enabled"] for v in self.s.load_state().values()))

    def test_unbuilt_process_cannot_be_enabled(self):
        with self.assertRaises(ValueError):
            self.s.set_enabled("sender", True)
        with self.assertRaises(ValueError):
            self.s.set_enabled("nope", True)

    def test_runs_when_due_then_waits_for_interval(self):
        self.s.set_enabled("discovery", True)
        sch = self.sched()
        self.assertEqual(sch.tick(sync=True), ["discovery"])
        self.t += 300
        self.assertEqual(sch.tick(sync=True), [])
        self.t += 301
        self.assertEqual(sch.tick(sync=True), ["discovery"])
        self.assertEqual(self.calls, ["agent.discovery"] * 2)

    def test_disable_stops_runs(self):
        self.s.set_enabled("discovery", True)
        self.s.set_enabled("discovery", False)
        self.assertEqual(self.sched().tick(sync=True), [])

    def test_cap_is_reported(self):
        self.s.set_enabled("discovery", True)
        self.sched(result=(None, True)).tick(sync=True)
        ev = read_json("outbox.json", [])
        self.assertEqual(ev[0]["kind"], "cap")
        self.assertIn("60-minute cap", ev[0]["subject"])
        self.assertIn("Started:", ev[0]["body"])

    def test_login_alert_once_until_success(self):
        self.s.set_enabled("discovery", True)
        sch = self.sched(result=(10, False))
        sch.tick(sync=True)
        self.t += 601
        sch.tick(sync=True)
        self.assertEqual(len(read_json("outbox.json", [])), 1)
        sch.launcher = lambda m, c, l: (0, False)
        self.t += 601
        sch.tick(sync=True)
        sch.launcher = lambda m, c, l: (10, False)
        self.t += 601
        sch.tick(sync=True)
        self.assertEqual(len(read_json("outbox.json", [])), 2)

    def test_cli(self):
        self.assertEqual(self.s.main(["enable", "discovery"]), 0)
        self.assertTrue(self.s.load_state()["discovery"]["enabled"])
        self.assertEqual(self.s.main(["enable", "sender"]), 1)
        self.assertEqual(self.s.main(["status"]), 0)


class Applier(unittest.TestCase):
    def fake(self, reply):
        create = lambda **kw: SimpleNamespace(content=[SimpleNamespace(text=reply)])
        return SimpleNamespace(messages=SimpleNamespace(create=create))

    def ans(self, label, reply='{"action": "answer", "value": "Yes", "reason": "bank"}', **kw):
        from agent import answers
        q = {"label": label, "type": "text", "required": True, **kw}
        return answers.decide_answer(q, bank="Always Yes", client=self.fake(reply))

    def test_resume_mapping(self):
        from agent import resumes
        n = resumes.resume_name
        self.assertEqual(n("VP of AI"), resumes.VP)
        self.assertEqual(n("Associate Vice President, AI"), resumes.VP)
        self.assertEqual(n("Head of AI"), resumes.VP)
        self.assertEqual(n("Director, Machine Learning"), resumes.DIRECTOR)
        self.assertEqual(n("Chief AI Officer"), resumes.CHIEF)
        self.assertEqual(n("Fractional CTO"), resumes.CHIEF)
        self.assertEqual(n("CEO"), resumes.CHIEF)
        self.assertEqual(n("VP / Director of AI"), resumes.VP)
        self.assertIsNone(n("Senior AI Engineer"))
        # VP and Chief resumes exist on disk (any extension); the Director resume lives on LinkedIn only
        self.assertTrue(resumes.local_path("VP of AI").exists())
        self.assertTrue(resumes.local_path("Chief AI Officer").exists())
        self.assertIsNone(resumes.local_path("Director of AI"))
        self.assertIn("DataScience", resumes.DIRECTOR)
        self.assertNotIn("SAP", resumes.DIRECTOR)

    def test_hard_blockers(self):
        self.assertEqual(self.ans("What is your risk appetite?", required=False)["action"], "skip_application")
        self.assertEqual(self.ans("Describe your financial capacity")["action"], "skip_application")
        self.assertEqual(self.ans("School attendance dates")["action"], "skip_application")
        self.assertEqual(self.ans("School attendance dates", required=False)["action"], "blank")

    def test_optional_stays_blank_without_calling_the_model(self):
        from agent import answers
        boom = SimpleNamespace(messages=SimpleNamespace(create=lambda **k: 1 / 0))
        r = answers.decide_answer({"label": "Pronouns", "type": "text", "required": False}, bank="x", client=boom)
        self.assertEqual(r["action"], "blank")

    def test_salary_rules(self):
        a = self.ans("Desired salary", options=["$150,000 - $199,999", "$200,000 - $250,000", "$250,000+"])
        self.assertEqual(a["value"], "$200,000 - $250,000")
        self.assertEqual(self.ans("Salary expectation", options=["$100k-$150k"])["action"], "pause")
        self.assertEqual(self.ans("Desired salary", type="number")["action"], "pause")
        self.assertEqual(self.ans("Expected compensation", options=["Under $100k", "$200k+"])["value"], "$200k+")

    def test_references(self):
        self.assertEqual(self.ans("Please provide references")["value"], "Will be provided once interview is confirmed")
        self.assertEqual(self.ans("May we contact your references?", options=["Yes", "No"])["value"], "No")

    def test_model_answers_are_validated(self):
        self.assertEqual(self.ans("Authorized to work?", options=["Yes", "No"])["value"], "Yes")
        # not one of the options -> pause, never guess
        r = self.ans("Authorized?", '{"action": "answer", "value": "Maybe", "reason": "x"}', options=["Yes", "No"])
        self.assertEqual(r["action"], "pause")
        # never 0 years
        r = self.ans("Years of experience with Kubernetes?", '{"action": "answer", "value": "0", "reason": "x"}')
        self.assertEqual(r["action"], "pause")
        self.assertEqual(self.ans("Years of Python experience?", '{"action": "answer", "value": "5", "reason": "x"}')["value"], "5")
        # banned school never typed
        r = self.ans("Where did you study?", "{\"action\": \"answer\", \"value\": \"St. Stephen's College\", \"reason\": \"x\"}")
        self.assertEqual(r["action"], "pause")
        # model says no basis / garbage / error -> pause
        self.assertEqual(self.ans("Odd question", '{"action": "pause", "value": null, "reason": "no basis"}')["action"], "pause")
        self.assertEqual(self.ans("Odd question", "not json")["action"], "pause")

    def test_tracker_numbers(self):
        from agent import tracker
        write_json("tracker.json", [])
        tracker.book(1, "Acme", "VP AI", "9", "SUBMITTED")
        with self.assertRaises(ValueError):
            tracker.book(1, "Other", "VP AI", "10", "SUBMITTED")
        with self.assertRaises(ValueError):
            tracker.book(2, "Other", "VP AI", "10", "MAYBE")
        self.assertTrue(dup_gate.is_duplicate("acme", "VP AI")[0])


if __name__ == "__main__":
    unittest.main()
