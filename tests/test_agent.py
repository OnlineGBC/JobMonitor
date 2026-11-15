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


if __name__ == "__main__":
    unittest.main()
