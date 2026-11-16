"""The guarded Submit step and the Continue/Easy Apply opener, against local pages that mirror LinkedIn."""
import os
import tempfile
import unittest
from types import SimpleNamespace

os.environ.setdefault("AGENT_DATA_DIR", tempfile.mkdtemp())

from agent import easy_apply  # noqa: E402
from agent.store import write_json  # noqa: E402

STUB = SimpleNamespace(wait_for_timeout=lambda ms: None)   # a page that only needs to wait

DIALOG = """<!doctype html><html><body><div id="bg">posting</div>
<script>
window.LOG = [];
function L(x){ window.LOG.push(x) }
function show(html){ let d = document.querySelector('dialog'); if (!d) { d = document.createElement('dialog'); document.body.appendChild(d); } d.open = true; d.innerHTML = html; }
function closeDlg(){ document.querySelectorAll('dialog').forEach(d => d.remove()) }
__SCRIPT__
</script></body></html>"""

REVIEW = ("show('<h2>Apply to __COMPANY__</h2><p>4/4 pages</p><p>Review your application</p>' +"
          " '<button onclick=\"L(\\'DISMISS CLICKED\\')\">Dismiss</button><button>Back</button>__EXTRA__' +"
          " '<button onclick=\"doSubmit()\">Submit application</button>');"
          "function doSubmit(){ L('SUBMITTED'); __AFTER__ }"
          "function doDone(){ L('DONE'); closeDlg() }")
SENT = "show('<h2>Application sent</h2><p>Your application was sent to __COMPANY__</p><button onclick=\"doDone()\">Done</button>');"


def page_html(company="RSP Group", dialog_company=None, extra="", after=None, two_dialogs=False):
    script = REVIEW + "\n" + ("(function(){ const d = document.createElement('dialog'); d.open = true; d.textContent = 'cookie banner'; document.body.appendChild(d); })();" if two_dialogs else "")
    if not two_dialogs:
        script += "\n"
    html = (DIALOG.replace("__SCRIPT__", script)
            .replace("__COMPANY__", dialog_company or company).replace("__EXTRA__", extra)
            .replace("__AFTER__", after if after is not None else SENT.replace("__COMPANY__", dialog_company or company)))
    return html.replace("</script></body>", "</script><script>" + ("" if two_dialogs else "") + "</script></body>")


class SubmitStep(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise unittest.SkipTest("playwright not installed")
        write_json("translations.json", {t: t for t in ("Dismiss", "Back", "Submit application", "Done")})
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def run_case(self, company="RSP Group", **opts):
        page = self.browser.new_page(viewport={"width": 1400, "height": 900})
        page.set_content(page_html(**opts))
        page.evaluate("() => { window.__boot = true }")
        if not opts.get("two_dialogs"):
            page.evaluate("() => { show('<h2>Apply to ' + %r + '</h2><p>4/4 pages</p><p>Review your application</p>' + '<button onclick=\"L(\\'DISMISS CLICKED\\')\">Dismiss</button><button>Back</button>' + %r + '<button onclick=\"doSubmit()\">Submit application</button>') }"
                          % (opts.get("dialog_company") or company, opts.get("extra", "")))
        try:
            outcome = easy_apply.submit_application(page, company, "Submit application")
            error = None
        except easy_apply.SubmitRefused as e:
            outcome, error = None, str(e)
        log, dialogs = page.evaluate("window.LOG"), page.locator("dialog[open]").count()
        page.close()
        return outcome, error, log, dialogs

    def test_the_happy_path_clicks_submit_once_confirms_and_closes_the_success_dialog(self):
        outcome, error, log, dialogs = self.run_case()
        self.assertIsNone(error)
        self.assertEqual((outcome["clicked"], outcome["confirmed"]), (True, True))
        self.assertEqual(log, ["SUBMITTED", "DONE"])          # one submit, then the Done button, nothing else
        self.assertNotIn("DISMISS CLICKED", log)               # the Dismiss trap before Submit is never touched
        self.assertEqual(dialogs, 0)

    def test_a_dialog_for_another_company_is_refused_before_any_click(self):
        outcome, error, log, dialogs = self.run_case(dialog_company="Some Other Company")
        self.assertIsNone(outcome)
        self.assertIn("does not name", error)
        self.assertEqual(log, [])

    def test_a_page_that_is_not_the_final_review_is_refused(self):
        for extra in ("<button>Next</button>", "<button>Review</button>"):
            outcome, error, log, _ = self.run_case(extra=extra)
            self.assertIsNone(outcome)
            self.assertIn("not the final review page", error)
            self.assertEqual(log, [], extra)

    def test_two_open_dialogs_are_refused(self):
        page = self.browser.new_page(viewport={"width": 1400, "height": 900})
        page.set_content(page_html())
        page.evaluate("() => { show('<h2>Apply to RSP Group</h2><button onclick=\"doSubmit()\">Submit application</button>');"
                      " const d = document.createElement('dialog'); d.open = true; d.textContent = 'cookie banner'; document.body.appendChild(d); }")
        with self.assertRaises(easy_apply.SubmitRefused):
            easy_apply.submit_application(page, "RSP Group", "Submit application")
        self.assertEqual(page.evaluate("window.LOG"), [])
        page.close()

    def test_no_company_means_no_submit(self):
        page = self.browser.new_page()
        page.set_content(page_html())
        for company in (None, "", "   "):
            with self.assertRaises(easy_apply.SubmitRefused):
                easy_apply.submit_application(page, company, "Submit application")
        page.close()

    def test_a_confirmation_toast_on_the_page_counts_when_the_dialog_just_closes(self):
        outcome, error, log, _ = self.run_case(
            after="closeDlg(); document.body.insertAdjacentHTML('beforeend', '<div>Application sent</div>');")
        self.assertEqual((outcome["clicked"], outcome["confirmed"]), (True, True))
        self.assertEqual(log, ["SUBMITTED"])

    def test_a_click_with_no_confirmation_text_is_reported_unconfirmed_not_confirmed(self):
        outcome, error, log, _ = self.run_case(after="closeDlg();")      # dialog simply vanishes, no 'sent' text
        self.assertEqual((outcome["clicked"], outcome["confirmed"]), (True, False))
        self.assertEqual(log, ["SUBMITTED"])


class RunFormSubmitGate(unittest.TestCase):
    """run_form only reaches Submit when asked to, and passes the company through."""

    def run_form(self, **kwargs):
        step = {"page": [4, 4], "errors": [], "fields": [],
                "buttons_info": [{"text": "Submit application", "en": "Submit application", "role": "submit"}],
                "text": "Review your application"}
        calls = []
        real_read, real_submit = easy_apply.read_step, easy_apply.submit_application
        easy_apply.read_step = lambda page: step
        easy_apply.submit_application = lambda page, company, label: calls.append((company, label)) or \
            {"clicked": True, "confirmed": True, "evidence": "Your application was sent"}
        try:
            result = easy_apply.run_form(STUB, "VP of AI", **kwargs)
        finally:
            easy_apply.read_step, easy_apply.submit_application = real_read, real_submit
        return result, calls

    def test_the_default_stops_at_the_review_page_and_never_submits(self):
        result, calls = self.run_form()
        self.assertEqual((result["status"], calls), ("ready", []))

    def test_allow_submit_passes_the_company_and_reports_submitted(self):
        result, calls = self.run_form(allow_submit=True, company="Acme")
        self.assertEqual(result["status"], "submitted")
        self.assertEqual(calls, [("Acme", "Submit application")])

    def test_allow_submit_without_a_company_is_refused_not_submitted(self):
        real = easy_apply.submit_application      # use the REAL guard, which refuses an empty company
        easy_apply.submit_application = real
        step = {"page": [4, 4], "errors": [], "fields": [], "text": "x",
                "buttons_info": [{"text": "Submit application", "en": "Submit application", "role": "submit"}]}
        real_read = easy_apply.read_step
        easy_apply.read_step = lambda page: step
        try:
            result = easy_apply.run_form(STUB, "VP of AI", allow_submit=True)
        finally:
            easy_apply.read_step = real_read
        self.assertEqual(result["status"], "paused")
        self.assertIn("refused to submit", result["reason"])


OPEN_PAGE = """<!doctype html><html><body>
<aside style="position:absolute;left:1020px;top:80px"><h2>More jobs</h2>
 <a href="/jobs/view/999/" onclick="window.LOG.push('SIDEBAR LINK CLICKED');return false">Easy Apply</a></div></aside>
<main style="width:900px"><h1>Chief AI Officer</h1><div>RSP Group</div>
 __CONTROL__
</main>
<script>window.LOG = []; function openDlg(){ window.LOG.push('OPENED'); const d = document.createElement('dialog'); d.open = true; d.textContent = 'Apply to RSP Group'; document.body.appendChild(d); }</script>
</body></html>"""


class OpenControl(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise unittest.SkipTest("playwright not installed")
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def controls(self, control_html):
        page = self.browser.new_page(viewport={"width": 1500, "height": 900})
        page.set_content(OPEN_PAGE.replace("__CONTROL__", control_html))
        found = easy_apply._top_card_apply_controls(page)
        texts = [c.inner_text().strip() for c in found]
        log = page.evaluate("window.LOG")
        page.close()
        return texts, log

    def test_finds_the_easy_apply_button_and_the_continue_link_in_the_top_card_only(self):
        self.assertEqual(self.controls('<button onclick="openDlg()">Easy Apply</button>')[0], ["Easy Apply"])
        self.assertEqual(self.controls('<a href="#" onclick="openDlg();return false">Continue</a>')[0], ["Continue"])

    def test_a_sidebar_easy_apply_link_is_never_a_candidate(self):
        texts, log = self.controls("")                         # the page has only the sidebar link
        self.assertEqual((texts, log), ([], []))               # not found, and never clicked

    def test_two_controls_in_the_top_card_is_ambiguous(self):
        texts, _ = self.controls('<button>Easy Apply</button><a href="#">Continue</a>')
        self.assertEqual(len(texts), 2)                        # open_easy_apply raises on != 1


if __name__ == "__main__":
    unittest.main()
