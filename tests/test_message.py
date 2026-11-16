"""The direct-message driver, against a local page that mirrors the real one:

  * a profile whose Message control is a LINK to /messaging/compose/ in the top card;
  * the same kind of link for OTHER people in a sidebar (listed first, so '.first' would hit them) and in the
    main column (traps: any click on them is logged);
  * an overlay div[role=dialog] with a recipient chip, a contenteditable box, and "Press Enter to Send"
    (no Send button) as on Raja's account; variants for a Send button and for Enter not sending.
"""
import os
import tempfile
import unittest

os.environ.setdefault("AGENT_DATA_DIR", tempfile.mkdtemp())

from agent import message  # noqa: E402
from agent.store import write_json  # noqa: E402

TEXT = "Hello Cody, thank you for connecting. I came across the role at Acme. I would love to discuss next steps. Thank you, Raja"
URL = "https://www.linkedin.com/in/codyarice/"

PAGE = """<!doctype html><html><head><title>__TITLE__</title><style>
 body{margin:0;font-family:sans-serif} main{width:900px;padding:20px}
 aside{position:absolute;left:1020px;top:80px;width:300px}
 #ov{position:absolute;left:600px;top:300px;width:420px;border:1px solid #888;background:#fff}
 #ed{min-height:60px;border:1px solid #ccc}
</style></head><body>
<aside><h2>More profiles for you</h2>
 <div>Maxx Wright · 2nd <a href="/messaging/compose/?profileUrn=MAXX" onclick="L('SIDEBAR MESSAGE CLICKED');return false">Message</a></div>
 <div>Ally Bowers · 2nd <a href="/messaging/compose/?profileUrn=ALLY" onclick="L('SIDEBAR MESSAGE CLICKED');return false">Message</a></div>
</aside>
<main>
 <h1>__NAME__</h1><span>· __DEGREE__</span>
 <div><a href="/messaging/compose/?profileUrn=CODY" onclick="openOv();return false">Message</a>
      <button aria-label="More">...</button></div>
 __CARD_TRAP__
 <div style="height:700px"></div>
 <section>Say hello <a href="/messaging/compose/?profileUrn=OTHER" onclick="L('MAIN MESSAGE LINK CLICKED');return false">Message</a></section>
</main>
<script>
window.LOG = [];
function L(x){ window.LOG.push(x) }
function openOv(){
  if (__UNHYDRATED__ && !sessionStorage.getItem('alive')) { sessionStorage.setItem('alive', '1'); return; }
  if (__DEAD__) return;
  const d = document.createElement('div'); d.id = 'ov'; d.setAttribute('role', 'dialog');
  d.innerHTML = '<div>New message</div><button aria-label="Close your draft conversation">x</button>' +
    '<div class="chip">__DIALOGNAME__ x</div><div>1st degree connection</div><div id="thread"></div>' +
    '<div id="ed" contenteditable="true" aria-label="Write a message\\u2026"></div>' +
    (__SENDBTN__ ? '<button id="sendbtn">Send</button>' : '<span>Press Enter to Send</span>');
  document.body.appendChild(d);
  const ed = d.querySelector('#ed');
  const send = () => { if (!__SENDS__) return; const t = ed.innerText; L('MSG SENT:' + t);
      const p = document.createElement('p'); p.textContent = t; d.querySelector('#thread').appendChild(p); ed.innerText = ''; };
  ed.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); if (!__SENDBTN__) send(); } });
  if (__SENDBTN__) d.querySelector('#sendbtn').onclick = send;
  d.querySelector('[aria-label^="Close your"]').onclick = () => d.remove();
}
</script></body></html>"""


CARD_TRAP = ("<p>Intro <a href=\"/messaging/compose/?profileUrn=EXTRA\" "
             "onclick=\"L('EXTRA CARD LINK CLICKED');return false\">Message</a></p>")


def page_html(name="Cody Rice", degree="1st", dialog_name=None, send_button=False, sends=True, title=None,
              second_link_in_card=False, unhydrated=False, dead=False):
    return (PAGE.replace("__TITLE__", title or f"{name} | LinkedIn").replace("__NAME__", name)
            .replace("__CARD_TRAP__", CARD_TRAP if second_link_in_card else "")
            .replace("__UNHYDRATED__", "true" if unhydrated else "false").replace("__DEAD__", "true" if dead else "false")
            .replace("__DEGREE__", degree).replace("__DIALOGNAME__", dialog_name or name)
            .replace("__SENDBTN__", "true" if send_button else "false").replace("__SENDS__", "true" if sends else "false"))


class MessageDriver(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise unittest.SkipTest("playwright not installed")
        write_json("translations.json", {t: t for t in ("Send", "x", "Close your draft conversation")})
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def run_case(self, text=TEXT, confirm=None, name="Cody Rice", **opts):
        page = self.browser.new_page(viewport={"width": 1500, "height": 900})
        page.set_content(page_html(name=opts.pop("page_name", name), **opts))
        result = message.send_message(page, URL, name, text, confirm=confirm, navigate=False)
        log, overlays = page.evaluate("window.LOG"), message._overlay(page).count()
        page.close()
        return result, log, overlays

    def test_press_enter_to_send_happy_path_sends_the_exact_text_once(self):
        result, log, _ = self.run_case()
        self.assertEqual(result["result"], "sent")
        self.assertEqual(log, [f"MSG SENT:{TEXT}"])

    def test_a_send_button_is_used_when_there_is_one(self):
        result, log, _ = self.run_case(send_button=True)
        self.assertEqual((result["result"], log), ("sent", [f"MSG SENT:{TEXT}"]))

    def test_other_peoples_message_links_are_never_clicked(self):
        for opts in ({}, {"send_button": True}):
            _, log, _ = self.run_case(**opts)
            self.assertNotIn("SIDEBAR MESSAGE CLICKED", log)
            self.assertNotIn("MAIN MESSAGE LINK CLICKED", log)

    def test_text_with_a_line_break_is_refused_before_anything_is_touched(self):
        result, log, overlays = self.run_case(text="Hello Cody,\nI would love to discuss next steps. Thank you, Raja")
        self.assertEqual((result["result"], log, overlays), ("aborted", [], 0))
        self.assertIn("line break", result["reason"])

    def test_wrong_person_or_not_connected_aborts_before_any_click(self):
        for opts in ({"page_name": "Someone Else"}, {"degree": "2nd"}, {"degree": "3rd"}):
            result, log, overlays = self.run_case(**opts)
            self.assertEqual((result["result"], log, overlays), ("aborted", [], 0), opts)

    def test_an_overlay_addressed_to_someone_else_aborts_and_closes(self):
        result, log, overlays = self.run_case(dialog_name="Brooke Schneider")
        self.assertEqual(result["result"], "aborted")
        self.assertIn("not addressed", result["reason"])
        self.assertEqual((log, overlays), ([], 0))

    def test_declining_sends_nothing_and_clears_and_closes(self):
        seen = []
        result, log, overlays = self.run_case(confirm=lambda t: seen.append(t) or False)
        self.assertEqual(seen, [TEXT])
        self.assertEqual((result["result"], log, overlays), ("aborted", [], 0))

    def test_two_message_links_in_the_top_card_is_ambiguous_so_nothing_is_clicked(self):
        result, log, overlays = self.run_case(second_link_in_card=True)
        self.assertEqual((result["result"], log, overlays), ("aborted", [], 0))
        self.assertIn("found 2", result["reason"])

    def run_routed(self, **opts):
        """Serve the page from the profile URL so a reload really reloads it."""
        page = self.browser.new_page(viewport={"width": 1500, "height": 900})
        html = page_html(**opts)
        page.route(URL, lambda route: route.fulfill(body=html, content_type="text/html"))
        old = message.OVERLAY_POLLS
        message.OVERLAY_POLLS = 6          # keep the test quick: 1.5 s per attempt
        try:
            result = message.send_message(page, URL, "Cody Rice", TEXT)
        finally:
            message.OVERLAY_POLLS = old
        out = (result, page.evaluate("window.LOG"), message._overlay(page).count())
        page.close()
        return out

    def test_a_first_click_that_does_nothing_is_retried_after_a_reload(self):
        result, log, _ = self.run_routed(unhydrated=True)
        self.assertEqual(result["result"], "sent")
        self.assertEqual(log, [f"MSG SENT:{TEXT}"])       # sent exactly once, not twice

    def test_a_link_that_never_opens_a_box_aborts_without_typing_or_sending(self):
        result, log, overlays = self.run_routed(dead=True)
        self.assertEqual((result["result"], log, overlays), ("aborted", [], 0))
        self.assertIn("no message box", result["reason"])

    def test_enter_that_does_not_send_is_unsettled_not_sent(self):
        result, log, _ = self.run_case(sends=False)
        self.assertEqual(result["result"], "unsettled")
        self.assertNotEqual(result["result"], "sent")


if __name__ == "__main__":
    unittest.main()
