"""Send ONE LinkedIn direct message to a 1st-degree connection, with Raja's approved text.

Same discipline as invite.py: scope every click, verify who it is for, read the text back, confirm, settle.

What is specific to messages (from the real page):
  * The Message control on a profile is a LINK to /messaging/compose/ inside the top card. Other people's
    Message links appear in sidebars, so only the one in the profile's own top card may be used.
  * It opens an overlay (div[role=dialog]) with the recipient shown as a chip and a contenteditable box.
  * Raja's account is set to "Press Enter to send": there is NO Send button and Enter sends. So the text
    must be a single line (a line break would send early), and Enter is pressed once, only after the
    confirmation, with the box focused and holding the exact text. If the setting is ever "Click Send",
    the Send button is used instead.
  * The text is typed character by character, as the spec requires.

Results of send_message(): sent | aborted (NOTHING sent) | unsettled (a send was attempted but not confirmed).
"""
import re

from . import invite, translate

TYPE_DELAY_MS = 20


def _overlay(page):
    return page.get_by_role("dialog").filter(visible=True)


def _top_card_message_links(page):
    """Visible /messaging/compose links in the profile's top card (not sidebars), language independent."""
    links = page.locator("main a[href*='/messaging/compose']")
    found = []
    for i in range(links.count()):
        box = links.nth(i).bounding_box()
        if box and box["y"] < 560 and box["x"] < 1000:
            found.append(links.nth(i))
    return found


OVERLAY_POLLS = 32  # x 250 ms = 8 s to wait for the message box after a click


def _open_overlay(page, name: str):
    """Click the profile's own Message link and wait for the box. Returns None on success, else a reason.

    The first click can land before the page's scripts are attached and do nothing. If no box appears, the
    profile is reloaded, the person re-verified, and the click tried once more. Nothing is typed until a
    box exists, so a failure here changes nothing.
    """
    for attempt in (1, 2):
        links = _top_card_message_links(page)
        if len(links) != 1:
            return f"expected one Message link in the profile's top card, found {len(links)}"
        links[0].click()
        for _ in range(OVERLAY_POLLS):
            if _overlay(page).count():
                return None
            page.wait_for_timeout(250)
        if attempt == 1:
            page.reload(wait_until="domcontentloaded")
            for _ in range(10):                  # the page can take a while to draw its top card again
                page.wait_for_timeout(2000)
                state = invite.card_state(page)
                if invite.name_matches(state["title"], name) and state["degree"] == "1st":
                    break
            else:
                return (f"the profile did not show {name} as a 1st-degree connection after a reload "
                        f"(page title '{state['title']}', degree '{state['degree']}'); stopping")
    return "no message box appeared (nothing was sent)"


def _clear_and_close(page) -> None:
    """Remove any typed text, then close the overlay. Never sends."""
    try:
        editor = _overlay(page).first.locator("[contenteditable=true]")
        if editor.count():
            editor.first.click()
            page.keyboard.press("Control+A")
            page.keyboard.press("Delete")
        for _ in range(3):
            if not _overlay(page).count():
                return
            labels = _overlay(page).first.evaluate(
                "d => [...d.querySelectorAll('button')].map(b => (b.getAttribute('aria-label') || b.innerText || '').trim())")
            tr = translate.translate([l for l in labels if l])
            pick = next((l for l in labels if translate.button_role(tr.get(l, l)) in ("discard", "close")
                         or re.match(r"^close your( draft)? conversation", tr.get(l, l), re.I)), None)
            if pick:
                _overlay(page).first.get_by_role("button", name=pick, exact=True).first.click()
            else:
                page.keyboard.press("Escape")
            page.wait_for_timeout(700)
    except Exception:
        pass


def _abort(reason: str, page=None) -> dict:
    if page is not None:
        _clear_and_close(page)
    return {"result": "aborted", "reason": reason}


def send_message(page, url: str, name: str, text: str, confirm=None, navigate: bool = True) -> dict:
    """Send `text` as a direct message to the 1st-degree connection at `url`. See the module docstring."""
    if re.search(r"[\r\n]", text):
        return _abort("the text has a line break, which could send the message early")
    if navigate:
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(4000)

    # 1. Right person, and actually connected
    state = invite.card_state(page)
    if not invite.name_matches(state["title"], name):
        return _abort(f"the page is '{state['title']}', not {name}")
    if state["degree"] != "1st":
        return _abort("not a 1st-degree connection, so a direct message cannot be sent")
    reason = _open_overlay(page, name)
    if reason:
        return _abort(reason, page)
    if _overlay(page).count() != 1:
        return _abort(f"expected one open overlay, found {_overlay(page).count()}", page)
    words = set(re.findall(r"[^\W\d_]+", _overlay(page).first.inner_text().lower()))
    if not set(invite._tokens(name)) <= words:
        return _abort(f"the message overlay is not addressed to {name}", page)
    editor = _overlay(page).first.locator("[contenteditable=true]")
    if editor.count() != 1:
        return _abort(f"expected one message box, found {editor.count()}", page)

    # 3. Type the exact text, character by character, and read it back
    editor.first.click()
    page.keyboard.type(text, delay=TYPE_DELAY_MS)
    page.wait_for_timeout(400)
    if editor.first.inner_text().strip() != text:
        return _abort("the message box did not hold the exact approved text", page)

    # 4. A last human check (visible-browser test runs)
    if confirm is not None and not confirm(text):
        return _abort("declined at the confirmation", page)

    # 5. Send: the Send button if there is exactly one, otherwise the single Enter key ("Press Enter to send")
    labels = _overlay(page).first.evaluate(
        "d => [...d.querySelectorAll('button')].map(b => (b.getAttribute('aria-label') || b.innerText || '').trim()).filter(Boolean)")
    tr = translate.translate(labels)
    sends = [l for l in labels if translate.button_role(tr.get(l, l)) == "send"]
    if len(sends) > 1:
        return _abort(f"expected at most one Send button, found {len(sends)}", page)
    editor.first.focus()
    if sends:
        _overlay(page).first.get_by_role("button", name=sends[0], exact=True).click()
    else:
        editor.first.press("Enter")

    # 6. Settle: the box empties and the thread now shows our text
    probe = " ".join(text[:40].split())  # compared with whitespace normalised, like the overlay text below
    for _ in range(40):
        page.wait_for_timeout(250)
        if not _overlay(page).count():
            break
        box_empty = editor.count() == 0 or editor.first.inner_text().strip() == ""
        if box_empty and probe in " ".join(_overlay(page).first.inner_text().split()):
            return {"result": "sent", "reason": "the message shows in the conversation"}
    return {"result": "unsettled", "reason": "a send was attempted but the message was not seen in the conversation"}
