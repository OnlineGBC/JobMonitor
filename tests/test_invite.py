"""The invite driver, tested against a local page that contains the TRAPS found on LinkedIn:
other people's Connect buttons in a sidebar (listed BEFORE the real one in the page, so a '.first'
click would hit them) and a stray 'Connect' text in the main column. Any trap click is logged.
"""
import os
import tempfile
import unittest

os.environ.setdefault("AGENT_DATA_DIR", tempfile.mkdtemp())

from agent import invite  # noqa: E402
from agent.store import write_json  # noqa: E402

TEXT = "Hello Cody, I applied for the VP of AI role at Acme. I would love to discuss next steps. Thank you, Raja"
URL = "https://www.linkedin.com/in/codyarice/"

PAGE = """<!doctype html><html><head><title>__TITLE__</title><style>
 body{margin:0;font-family:sans-serif} main{width:900px;padding:20px}
 aside{position:absolute;left:1020px;top:80px;width:300px} .dd{display:none} .dd.open{display:block}
</style></head><body>
<aside><h2>More profiles for you</h2>
 <div>Maxx Wright · 2nd <button onclick="L('SIDEBAR CONNECT CLICKED')"><span>Connect</span></button></div>
 <div>Ally Bowers · 2nd <button onclick="L('SIDEBAR CONNECT CLICKED')"><span>Connect</span></button></div>
</aside>
<main>
 <h1>__NAME__</h1><span>· __DEGREE__</span>
 <div id="actions"><button>Message</button><button>Follow</button>__PENDING__
  <button aria-label="More" onclick="document.getElementById('dd').classList.toggle('open')">...</button>
  <div id="dd" class="dd artdeco-dropdown__content"><ul>
   <li><p>Send profile in a message</p></li><li><p onclick="startConnect()">Connect</p></li>__SECOND__
   <li><p>Report</p></li></ul></div></div>
 <section>Grow your network <span onclick="L('MAIN CONNECT TEXT CLICKED')">Connect</span></section>
</main>
<script>
window.LOG = []; function L(x){ window.LOG.push(x) }
function pend(){ const b = document.createElement('button'); b.textContent = 'Pending'; document.getElementById('actions').appendChild(b) }
function startConnect(){
  document.getElementById('dd').classList.remove('open');
  if (__INSTANT__) { L('INSTANT SEND'); pend(); return; }
  const d = document.createElement('dialog'); d.open = true; d.id = 'dlg';
  d.innerHTML = '<h2>Add a note to your invitation?</h2>' + (__NOTEBTN__ ? '<button onclick="addNote()">Add a note</button>' : '') +
    '<button onclick="sendNoNote()">Send without a note</button><button aria-label="Dismiss" onclick="closeDlg()">Dismiss</button>';
  document.body.appendChild(d);
}
function closeDlg(){ const d = document.getElementById('dlg'); if (d) d.remove() }
function sendNoNote(){ L('SENT WITHOUT NOTE'); closeDlg(); pend() }
function addNote(){ document.getElementById('dlg').innerHTML = '<h2>Add a note</h2><textarea maxlength="__MAXLEN__"></textarea>' +
  '<button onclick="doSend()">Send</button><button onclick="closeDlg()">Dismiss</button>' }
function doSend(){ L('SENT:' + document.querySelector('#dlg textarea').value); closeDlg(); pend() }
</script></body></html>"""


def page_html(name="Cody Rice", title=None, degree="3rd", pending=False, instant=False, maxlength=300,
              note_button=True, second_connect=False):
    return (PAGE.replace("__TITLE__", title or f"{name} | LinkedIn").replace("__NAME__", name)
            .replace("__DEGREE__", degree).replace("__PENDING__", "<button>Pending</button>" if pending else "")
            .replace("__INSTANT__", "true" if instant else "false").replace("__NOTEBTN__", "true" if note_button else "false")
            .replace("__MAXLEN__", str(maxlength))
            .replace("__SECOND__", "<li><p onclick=\"startConnect()\">Connect</p></li>" if second_connect else ""))


class InviteDriver(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise unittest.SkipTest("playwright not installed")
        write_json("translations.json", {t: t for t in ("Add a note", "Send without a note", "Dismiss", "Send")})
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def run_case(self, text=TEXT, confirm=None, name="Cody Rice", **opts):
        page = self.browser.new_page(viewport={"width": 1500, "height": 900})
        page.set_content(page_html(name=opts.pop("page_name", name), **opts))
        result = invite.send_invite(page, URL, name, text, confirm=confirm, navigate=False)
        log, dialogs = page.evaluate("window.LOG"), page.locator("dialog[open]").count()
        page.close()
        return result, log, dialogs

    def test_the_happy_path_sends_the_exact_text_once_and_touches_nothing_else(self):
        result, log, dialogs = self.run_case()
        self.assertEqual(result["result"], "sent")
        self.assertEqual(log, [f"SENT:{TEXT}"])   # exactly one send, exact text, no trap clicked
        self.assertEqual(dialogs, 0)

    def test_other_peoples_connect_buttons_are_never_clicked(self):
        _, log, _ = self.run_case()
        self.assertNotIn("SIDEBAR CONNECT CLICKED", log)
        self.assertNotIn("MAIN CONNECT TEXT CLICKED", log)

    def test_wrong_person_aborts_before_any_click(self):
        result, log, _ = self.run_case(page_name="Someone Else")
        self.assertEqual(result["result"], "aborted")
        self.assertEqual(log, [])
        result, log, _ = self.run_case(page_name="Cody Rice-Jones")  # a near miss is not a match
        self.assertEqual((result["result"], log), ("aborted", []))

    def test_already_connected_or_pending_clicks_nothing(self):
        result, log, _ = self.run_case(degree="1st")
        self.assertEqual((result["result"], log), ("already_connected", []))
        result, log, _ = self.run_case(pending=True)
        self.assertEqual((result["result"], log), ("already_pending", []))

    def test_an_instant_send_is_reported_as_sent_without_the_note(self):
        result, log, _ = self.run_case(instant=True)
        self.assertEqual(result["result"], "sent_without_note")
        self.assertNotEqual(result["result"], "sent")
        self.assertEqual(log, ["INSTANT SEND"])

    def test_a_note_longer_than_the_box_allows_sends_nothing(self):
        result, log, dialogs = self.run_case(maxlength=50)
        self.assertEqual(result["result"], "aborted")
        self.assertEqual((log, dialogs), ([], 0))

    def test_declining_the_confirmation_sends_nothing_and_closes_the_dialog(self):
        seen = []
        result, log, dialogs = self.run_case(confirm=lambda t: seen.append(t) or False)
        self.assertEqual(seen, [TEXT])               # shown the exact text first
        self.assertEqual((result["result"], log, dialogs), ("aborted", [], 0))

    def test_never_sends_without_a_note(self):
        result, log, dialogs = self.run_case(note_button=False)   # the dialog offers only 'Send without a note'
        self.assertEqual(result["result"], "aborted")
        self.assertNotIn("SENT WITHOUT NOTE", log)
        self.assertEqual((log, dialogs), ([], 0))

    def test_an_ambiguous_menu_aborts(self):
        result, log, _ = self.run_case(second_connect=True)
        self.assertEqual((result["result"], log), ("aborted", []))


class NameMatching(unittest.TestCase):
    def test_exact_names_only(self):
        m = invite.name_matches
        self.assertTrue(m("Cody Rice | LinkedIn", "Cody Rice"))
        self.assertTrue(m("(2) Cody Rice | LinkedIn", "Cody Rice"))
        self.assertTrue(m("Ally Bowers, SHRM-CP | LinkedIn", "Ally Bowers"))
        self.assertTrue(m("Ally Bowers | LinkedIn", "Ally Bowers, SHRM-CP"))
        self.assertFalse(m("Cody Rice-Jones | LinkedIn", "Cody Rice"))
        self.assertFalse(m("Cody | LinkedIn", "Cody Rice"))
        self.assertFalse(m("LinkedIn Login | LinkedIn", "Cody Rice"))
        self.assertFalse(m("", "Cody Rice"))
        self.assertFalse(m("Cody Rice | LinkedIn", ""))


class ButtonRoles(unittest.TestCase):
    def test_send_without_a_note_is_never_the_send_role(self):
        from agent import translate
        r = translate.button_role
        self.assertEqual(r("Send"), "send")
        self.assertEqual(r("Send invitation"), "send")
        self.assertEqual(r("Send without a note"), "send_without_note")
        self.assertEqual(r("Add a note"), "add_note")
        self.assertEqual(r("Send profile in a message"), "other")  # a menu item, not Send
        self.assertEqual(r("Submit application"), "submit")


if __name__ == "__main__":
    unittest.main()
