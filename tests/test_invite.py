"""The invite driver, tested against a local page that reproduces what LinkedIn really does:

  * the invitation dialog is a div[role=dialog] inside the SHADOW ROOT of #interop-outlet (not a native
    <dialog>), has an icon-only close button, and names the recipient in its text;
  * the page also has TRAPS: other people's Connect buttons in a sidebar (listed BEFORE the real one in
    the page, so a '.first' click hits them) and a stray 'Connect' text in the main column. Any trap click
    is logged. (A first version of this page used a native <dialog>, so the tests passed while the real
    page failed; the page now mirrors the real structure.)
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
 aside{position:absolute;left:1020px;top:80px;width:300px} .dd{display:none;position:absolute} .dd.open{display:block}
</style></head><body>
<aside><h2>More profiles for you</h2>
 <div>Maxx Wright · 2nd <button onclick="L('SIDEBAR CONNECT CLICKED')"><span>Connect</span></button></div>
 <div>Ally Bowers · 2nd <button onclick="L('SIDEBAR CONNECT CLICKED')"><span>Connect</span></button></div>
 <div><a href="/preload/custom-invite/?vanityName=someone-else" onclick="L('SIDEBAR CONNECT CLICKED');return false">Connect</a></div>
</aside>
<main>
 <h1>__NAME__</h1><span>· __DEGREE__</span>
 <div id="actions">__TOPCARD__<button>Message</button><button>Follow</button>__PENDING__
  <button aria-label="More" onclick="document.getElementById('dd').classList.toggle('open')">...</button>
  <div id="dd" class="dd artdeco-dropdown__content"><ul>
   <li><p>Send profile in a message</p></li>__MENUCONNECT____SECOND__
   <li><p>Report</p></li></ul></div></div>
 <section>Grow your network <span onclick="L('MAIN CONNECT TEXT CLICKED')">Connect</span></section>
</main>
<div id="interop-outlet"></div>
<script>
window.LOG = []; function L(x){ window.LOG.push(x) }
const shadow = document.getElementById('interop-outlet').attachShadow({mode: 'open'});
function pend(){ const b = document.createElement('button'); b.textContent = 'Pending'; document.getElementById('actions').appendChild(b) }
function dlg(){ return shadow.getElementById('dlg') }
function closeDlg(){ const d = dlg(); if (d) d.remove() }
function startConnect(){
  document.getElementById('dd').classList.remove('open');
  if (__INSTANT__) { L('INSTANT SEND'); pend(); return; }
  const d = document.createElement('div'); d.id = 'dlg'; d.setAttribute('role', 'dialog'); d.setAttribute('aria-labelledby', 'h');
  d.innerHTML = '<button aria-label="Dismiss" onclick="closeDlg()">\\u00d7</button><h2 id="h">Add a note to your invitation?</h2>' +
    '<p>Personalize your invitation to <b>__DIALOGNAME__</b> by adding a note.</p>' +
    (__NOTEBTN__ ? '<button id="addnote">Add a note</button>' : '') + '<button id="nonote">Send without a note</button>';
  shadow.appendChild(d);
  if (__NOTEBTN__) shadow.getElementById('addnote').onclick = addNote;
  shadow.getElementById('nonote').onclick = () => { L('SENT WITHOUT NOTE'); closeDlg(); pend() };
  shadow.querySelector('[aria-label=Dismiss]').onclick = closeDlg;
}
function addNote(){
  const d = dlg();
  d.innerHTML = '<button aria-label="Dismiss">\\u00d7</button><h2 id="h">Add a note</h2><textarea maxlength="__MAXLEN__"></textarea><button id="send">Send</button>';
  d.querySelector('[aria-label=Dismiss]').onclick = closeDlg;
  d.querySelector('#send').onclick = () => { L('SENT:' + d.querySelector('textarea').value); closeDlg(); pend() };
}
</script></body></html>"""


def page_html(name="Cody Rice", title=None, degree="3rd", pending=False, instant=False, maxlength=300,
              note_button=True, second_connect=False, dialog_name=None, top_card=False):
    link = ('<a href="/preload/custom-invite/?vanityName=codyarice" aria-label="Invite Cody Rice to connect" '
            'onclick="startConnect();return false">Connect</a>')
    return (PAGE.replace("__TOPCARD__", link if top_card else "")
            .replace("__MENUCONNECT__", "" if top_card else '<li><p onclick="startConnect()">Connect</p></li>').replace("__TITLE__", title or f"{name} | LinkedIn").replace("__NAME__", name)
            .replace("__DEGREE__", degree).replace("__PENDING__", "<button>Pending</button>" if pending else "")
            .replace("__INSTANT__", "true" if instant else "false").replace("__NOTEBTN__", "true" if note_button else "false")
            .replace("__MAXLEN__", str(maxlength)).replace("__DIALOGNAME__", dialog_name or name)
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
        log, dialogs = page.evaluate("window.LOG"), invite._dlg(page).count()
        page.close()
        return result, log, dialogs

    def test_the_dialog_is_really_in_a_shadow_root_and_invisible_to_plain_selectors(self):
        """Guards the test page itself: it must keep the structure that broke the first real run."""
        page = self.browser.new_page(viewport={"width": 1500, "height": 900})
        page.set_content(page_html())
        page.locator("main").get_by_role("button", name="More", exact=True).click()
        page.locator(".artdeco-dropdown__content").get_by_text("Connect", exact=True).click()
        page.wait_for_timeout(300)
        self.assertEqual(page.evaluate("document.querySelectorAll('dialog[open], [role=dialog]').length"), 0)
        self.assertEqual(page.locator("dialog[open]").count(), 0)   # what the broken driver looked for
        self.assertEqual(invite._dlg(page).count(), 1)              # what the fixed driver uses
        page.close()

    def test_the_happy_path_sends_the_exact_text_once_and_touches_nothing_else(self):
        result, log, dialogs = self.run_case()
        self.assertEqual(result["result"], "sent")
        self.assertEqual(log, [f"SENT:{TEXT}"])   # exactly one send, exact text, no trap clicked
        self.assertEqual(dialogs, 0)

    def test_the_top_card_connect_link_is_used_when_the_menu_has_no_connect(self):
        result, log, dialogs = self.run_case(top_card=True)
        self.assertEqual(result["result"], "sent")
        self.assertEqual(log, [f"SENT:{TEXT}"])       # the sidebar's other Connect links were not touched
        self.assertEqual(dialogs, 0)

    def test_without_a_top_card_link_or_a_menu_connect_nothing_is_clicked(self):
        page = self.browser.new_page(viewport={"width": 1500, "height": 900})
        page.set_content(page_html().replace('<li><p onclick="startConnect()">Connect</p></li>', ""))
        result = invite.send_invite(page, URL, "Cody Rice", TEXT, navigate=False)
        log = page.evaluate("window.LOG")
        page.close()
        self.assertEqual(result["result"], "aborted")
        self.assertEqual(log, [])

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

    def test_a_dialog_that_names_someone_else_aborts_and_closes(self):
        result, log, dialogs = self.run_case(dialog_name="Brooke Schneider")
        self.assertEqual(result["result"], "aborted")
        self.assertIn("does not name", result["reason"])
        self.assertEqual((log, dialogs), ([], 0))

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
