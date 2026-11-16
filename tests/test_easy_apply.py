import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("AGENT_DATA_DIR", tempfile.mkdtemp())

from agent import answers, easy_apply, translate  # noqa: E402
from agent.store import read_json, write_json  # noqa: E402


class FakeClient:
    """Counts calls and answers with a canned JSON object."""
    def __init__(self, reply):
        self.reply, self.calls = reply, 0
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kw):
        self.calls += 1
        self.model = kw["model"]
        return SimpleNamespace(content=[SimpleNamespace(text=self.reply)])


class Translation(unittest.TestCase):
    def setUp(self):
        write_json("translations.json", {})

    def test_translated_once_then_cached(self):
        c = FakeClient('{"Ja": "Yes", "Nein": "No"}')
        self.assertEqual(translate.translate(["Ja", "Nein"], client=c), {"Ja": "Yes", "Nein": "No"})
        self.assertEqual(c.calls, 1)
        self.assertEqual(c.model, "claude-haiku-5-5")
        # second time: served from the saved file, no API call at all
        boom = FakeClient("not used")
        self.assertEqual(translate.translate(["Ja"], client=boom)["Ja"], "Yes")
        self.assertEqual(boom.calls, 0)
        self.assertEqual(read_json("translations.json", {})["Nein"], "No")

    def test_failure_returns_original_and_caches_nothing(self):
        r = translate.translate(["Weiter"], client=FakeClient("garbage"))
        self.assertEqual(r, {"Weiter": "Weiter"})
        self.assertEqual(read_json("translations.json", {}), {})

    def test_button_roles(self):
        role = translate.button_role
        self.assertEqual(role("Next"), "next")
        self.assertEqual(role("Review"), "review")
        self.assertEqual(role("Review your application"), "review")
        self.assertEqual(role("Submit application"), "submit")
        self.assertEqual(role("Upload resume"), "upload")
        self.assertEqual(role("Discard"), "discard")
        self.assertEqual(role("Save"), "save")
        self.assertEqual(role("Dismiss"), "other")  # unknown buttons are never clicked
        self.assertEqual(role("Weiter"), "other")   # untranslated text is never mistaken for Next


class ResumeMatching(unittest.TestCase):
    def test_extension_is_ignored(self):
        self.assertEqual(easy_apply.stem("RajaGopalanChiefAI_DataOfficer.pdf"),
                         easy_apply.stem("RajaGopalanChiefAI_DataOfficer.docx"))
        self.assertNotEqual(easy_apply.stem("RajaGopalanDir-SystemArchitectEnterpriseAI_SAP.docx"),
                            easy_apply.stem("RajaGopalanDir-SystemArchitectEnterpriseAI_DataScience.docx"))

    def test_resume_group_detection(self):
        g = {"kind": "radio", "options": [{"aria": "A.docx"}, {"aria": "B.pdf"}]}
        self.assertTrue(easy_apply._is_resume_group(g))
        self.assertFalse(easy_apply._is_resume_group({"kind": "radio", "options": [{"aria": "Is it?"}]}))
        self.assertFalse(easy_apply._is_resume_group({"kind": "radio", "options": []}))


class Learning(unittest.TestCase):
    def test_learn_appends_once_under_a_marked_section(self):
        p = Path(tempfile.mkdtemp()) / "bank.txt"
        p.write_text("Always Yes\r\nAlways No\r\n", encoding="utf-8", newline="")
        self.assertTrue(answers.learn("How many years of Management experience?", "15", path=p))
        self.assertFalse(answers.learn("how many years of management experience", "20", path=p))
        text = p.read_bytes().decode("utf-8")
        self.assertEqual(text.count(answers.LEARNED_HEADING), 1)
        self.assertIn("How many years of Management experience? → 15", text)
        self.assertTrue(text.startswith("Always Yes\r\nAlways No\r\n"))  # original untouched
        self.assertEqual(text.count("\n"), text.count("\r\n"))  # every line ending is still CRLF

    def test_earlier_answers_pass_the_same_safety_rules(self):
        ok = answers.acceptable_earlier_answer
        self.assertTrue(ok("Years of Management experience?", "15"))
        self.assertFalse(ok("Years of Python experience?", "0"))
        self.assertFalse(ok("Where did you study?", "St. Xavier's School"))
        self.assertFalse(ok("What is your risk appetite?", "High"))
        self.assertFalse(ok("Desired salary", "150000"))
        self.assertFalse(ok("Anything", ""))


class ThinkingBlocks(unittest.TestCase):
    """The model may return a thinking block before its text; every caller must cope."""

    def reply(self, text):
        think = SimpleNamespace(type="thinking", thinking="hmm")  # no .text attribute at all
        out = SimpleNamespace(type="text", text=text)
        create = lambda **kw: SimpleNamespace(content=[think, out])
        return SimpleNamespace(messages=SimpleNamespace(create=create))

    def test_llm_helper(self):
        from agent import llm
        r = SimpleNamespace(content=[SimpleNamespace(type="thinking"), SimpleNamespace(type="text", text='x {"a": 1} y')])
        self.assertEqual(llm.json_object(r), {"a": 1})
        with self.assertRaises(ValueError):
            llm.text_of(SimpleNamespace(content=[SimpleNamespace(type="thinking")]))
        # a reply cut off by the token limit, or with no JSON, says so plainly
        cut = SimpleNamespace(stop_reason="max_tokens", content=[SimpleNamespace(type="text", text='{"a": ')])
        with self.assertRaisesRegex(ValueError, "cut off"):
            llm.json_object(cut)
        plain = SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text="Yes.")])
        with self.assertRaisesRegex(ValueError, "no JSON"):
            llm.json_object(plain)

    def test_all_three_callers(self):
        from agent import core_check
        write_json("translations.json", {})
        c = self.reply('{"Ja": "Yes"}')
        self.assertEqual(translate.translate(["Ja"], client=c), {"Ja": "Yes"})
        r = answers.decide_answer({"label": "Authorized to work?", "type": "text", "required": True,
                                   "options": ["Yes", "No"]}, bank="Always Yes",
                                  client=self.reply('{"action": "answer", "value": "Yes", "reason": "bank"}'))
        self.assertEqual((r["action"], r["value"]), ("answer", "Yes"))
        ok, why = core_check.judge({"title": "Chief Technology Officer", "company": "X", "description": "Lead ML platform"},
                                   client=self.reply('{"core": true, "reason": "ml"}'))
        self.assertTrue(ok)


class ReadFormMarkup(unittest.TestCase):
    """Run the form reader against the exact markup seen on LinkedIn, in a local page (no network)."""
    LINKEDIN_RADIOS = (
        '<dialog open><h2>Apply to X</h2>1/5 pages'
        '<div>Have you completed the following level of education: Bachelor Degree?*</div>'
        '<fieldset role="radiogroup"><div><div><div><input id="r1" aria-label="Have you completed the following '
        'level of education: Bachelor Degree?" type="radio" checked name="g"><label for="r1"></label></div>'
        '<div><p>Ja</p></div></div></div><div><div><div><input id="r2" aria-label="Have you completed the '
        'following level of education: Bachelor Degree?" type="radio" name="g"><label for="r2"></label></div>'
        '<div><p>Nein</p></div></div></div></fieldset>'
        '<input id="t1" type="text" aria-label="How many years of Management experience do you currently have?" '
        'value="15"><button>Back</button><button>Review</button></dialog>')

    def test_reads_option_text_questions_and_required(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.skipTest("playwright not installed")
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True)
            page = b.new_page()
            page.set_content(self.LINKEDIN_RADIOS)
            raw = page.locator("dialog[open]").first.evaluate(easy_apply.READ_JS)
            b.close()
        group = next(f for f in raw["fields"] if f["kind"] == "radio")
        self.assertEqual([o["text"] for o in group["options"]], ["Ja", "Nein"])
        self.assertEqual([o["checked"] for o in group["options"]], [True, False])
        self.assertTrue(group["prev"].endswith("*"))
        text = next(f for f in raw["fields"] if f["kind"] == "text")
        self.assertEqual((text["label"], text["value"]), ("How many years of Management experience do you currently have?", "15"))
        self.assertEqual(raw["page"], [1, 5])
        self.assertEqual(raw["buttons"], ["Back", "Review"])


class CloseForm(unittest.TestCase):
    """close_form() against a local page that behaves like LinkedIn's Save/Discard confirmation."""
    PAGE = (
        "<body><dialog open id=form><button>Back</button><button>Next</button></dialog>"
        "<script>window.choice = null;"
        "function show(){ if(!document.getElementById('c')){ const d=document.createElement('dialog'); d.id='c'; d.open=true;"
        " d.innerHTML='<button onclick=\"pick(this)\">Discard</button><button onclick=\"pick(this)\">Save</button>';"
        " document.body.appendChild(d);} else { document.getElementById('c').remove(); } }"
        "function pick(b){ window.choice=b.innerText; document.querySelectorAll('dialog').forEach(x=>x.remove()); }"
        "document.addEventListener('keydown', e => { if(e.key==='Escape') show(); });</script></body>")

    def run_close(self, save, confirmation_already_open=False):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.skipTest("playwright not installed")
        write_json("translations.json", {"Discard": "Discard", "Save": "Save", "Back": "Back", "Next": "Next"})
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True)
            page = b.new_page()
            page.set_content(self.PAGE)
            if confirmation_already_open:
                page.keyboard.press("Escape")
            done = easy_apply.close_form(page, save=save)
            out = (done, page.evaluate("window.choice"), page.locator("dialog[open]").count())
            b.close()
        return out

    def test_discard_and_save(self):
        self.assertEqual(self.run_close(save=False), ("discard", "Discard", 0))
        self.assertEqual(self.run_close(save=True), ("save", "Save", 0))

    def test_copes_when_the_confirmation_is_already_showing(self):
        self.assertEqual(self.run_close(save=False, confirmation_already_open=True), ("discard", "Discard", 0))


if __name__ == "__main__":
    unittest.main()
