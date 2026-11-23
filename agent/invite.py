"""Send ONE LinkedIn connection invitation with Raja's approved note.

Built around one rule: scope every click. A profile page also shows OTHER people's Connect buttons
(the "More profiles for you" sidebar sends an invitation instantly, with no dialog), so this module
never clicks a page-wide text match. The only Connect it may click is the single one inside the
profile's own "More" menu, and only after the page has been verified to be the right person's.

Results of send_invite():
  sent               the note was typed, read back exactly, sent, and LinkedIn shows the invite as Pending
  already_connected  the person is already a 1st-degree connection; nothing clicked
  already_pending    an invitation is already pending; nothing clicked
  aborted            a safety check failed or Raja declined; NOTHING was sent
  sent_without_note  Connect sent immediately with no dialog: an invite went out WITHOUT the note (alert)
  unsettled          a Send click happened but LinkedIn's Pending state was not confirmed (alert, never retried)
"""
import re

from . import translate

CARD_JS = r"""() => {
  // The profile's top card: visible things in the main column near the top. The sidebar sits to the right.
  const inCard = e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0 && r.top < 560 && r.left < 1000; };
  const leaves = [...document.querySelectorAll('main *')].filter(e => e.children.length === 0 && inCard(e));
  return {
    title: document.title,
    texts: leaves.map(e => (e.innerText || '').trim()).filter(Boolean),
    controls: [...document.querySelectorAll('main button, main [role=button]')].filter(inCard)
              .map(b => (b.getAttribute('aria-label') || b.innerText || '').trim()),
  };
}"""

_CONNECT = re.compile(r"^(Connect|Vernetzen)$", re.I)


def _tokens(name: str) -> list:
    base = re.split(r",", name or "")[0]
    return [t for t in re.findall(r"[^\W\d_]+", base.lower()) if len(t) > 1]


def name_matches(page_title: str, expected: str) -> bool:
    """The page title ('(2) Cody Rice | LinkedIn') must carry the expected first and last name."""
    shown = re.sub(r"^\(\d+\)\s*", "", page_title or "").split("|")[0]
    want, have = _tokens(expected), _tokens(shown)
    return bool(want) and want == have  # exact: a near-miss aborts, which is the safe side


def card_state(page) -> dict:
    s = page.evaluate(CARD_JS)
    flat = " ".join(s["texts"])
    m = re.search(r"\b(1st|2nd|3rd)\b", flat)
    labels = s["texts"] + s["controls"]
    return {"title": s["title"], "degree": m.group(1) if m else "",
            "pending": any(re.fullmatch(r"(pending|ausstehend)", t.strip(), re.I) for t in labels),
            "more_buttons": sum(1 for t in s["controls"] if t.strip().lower() == "more")}


def _dlg(page):
    """The visible dialog. LinkedIn's invitation modal is a div[role=dialog] inside the shadow root of
    #interop-outlet, not a native <dialog>; Playwright's role selector sees through the shadow root."""
    return page.get_by_role("dialog").filter(visible=True)


def _wait_for_dialog(page, ms: int = 7000) -> bool:
    for _ in range(ms // 250):
        if _dlg(page).count():
            return True
        page.wait_for_timeout(250)
    return False


def _button_labels(page) -> list:
    """Visible text or aria-label of each button in the dialog (the close X has only an aria-label)."""
    return _dlg(page).first.evaluate(
        "d => [...d.querySelectorAll('button')]"
        ".map(b => (b.getAttribute('aria-label') || b.innerText || '').trim()).filter(Boolean)")


def _abort(reason: str, close=None) -> dict:
    if close:
        close()
    return {"result": "aborted", "reason": reason}


def _dialog_roles(page) -> list:
    """[(label, role)] for the buttons of the open dialog, roles from the translation cache."""
    names = _button_labels(page)
    tr = translate.translate(names)
    return [(n, translate.button_role(tr.get(n, n))) for n in names]


def close_dialog(page) -> None:
    """Close whatever dialog is open WITHOUT sending: Discard if asked, else its Dismiss/close button,
    else Escape. Never touches a Send button."""
    for _ in range(4):
        if not _dlg(page).count():
            return
        roles = _dialog_roles(page)
        button = next((n for n, r in roles if r == "discard"), None) or next((n for n, r in roles if r == "close"), None)
        if button:
            _dlg(page).first.get_by_role("button", name=button, exact=True).first.click()
        else:
            page.keyboard.press("Escape")
        page.wait_for_timeout(700)


def send_invite(page, url: str, name: str, text: str, confirm=None, navigate: bool = True) -> dict:
    """Send `text` as the note of an invitation to the person at `url`. See the module docstring."""
    if navigate:
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(4000)

    # 1. Is this the right person, and is there anything to do?
    state = card_state(page)
    if not name_matches(state["title"], name):
        return _abort(f"the page is '{state['title']}', not {name}")
    if state["degree"] == "1st":
        return {"result": "already_connected", "reason": "already a 1st-degree connection"}
    if state["pending"]:
        return {"result": "already_pending", "reason": "an invitation is already pending"}
    if state["more_buttons"] != 1:
        return _abort(f"expected one 'More' button in the profile's top card, found {state['more_buttons']}")

    # 2. The ONE Connect that may be clicked: this profile's own Connect link in the top card (its address names this
    #    person), or else the one inside the profile's own More menu
    direct = _top_card_connect(page, url)
    if direct is not None:
        direct.click()
        return _after_connect(page, name, text, confirm, navigate)
    box = None
    for i in range(page.locator("main").get_by_role("button", name="More", exact=True).count()):
        b = page.locator("main").get_by_role("button", name="More", exact=True).nth(i).bounding_box()
        if b and b["y"] < 560 and b["x"] < 1000:
            page.locator("main").get_by_role("button", name="More", exact=True).nth(i).click()
            box = True
            break
    if not box:
        return _abort("the profile's More button was not found")
    page.wait_for_timeout(1200)
    menu = page.locator(".artdeco-dropdown__content:visible, [role=menu]:visible")
    if menu.count() != 1:
        return _abort(f"expected one open menu, found {menu.count()}", lambda: page.keyboard.press("Escape"))
    connect = menu.get_by_text(_CONNECT)
    if connect.count() != 1:
        page.keyboard.press("Escape")
        return _abort(f"expected exactly one Connect in the profile menu, found {connect.count()}")
    connect.first.click()
    return _after_connect(page, name, text, confirm, navigate)


def _top_card_connect(page, url: str):
    """The Connect link in this profile's own top card, or None. Its address carries this person's profile name
    (/preload/custom-invite/?vanityName=<name in the profile address>), and it must be the only such visible link in
    the top card, so no other person's Connect (the sidebar's) can match."""
    m = re.search(r"/in/([^/?#]+)", url or "")
    if not m:
        return None
    links = page.locator(f'main a[href*="custom-invite"][href*="vanityName={m.group(1)}" i]')
    found = []
    for i in range(links.count()):
        b = links.nth(i).bounding_box()
        if b and b["width"] > 0 and b["y"] < 560 and b["x"] < 1000:
            found.append(links.nth(i))
    return found[0] if len(found) == 1 else None


def _after_connect(page, name: str, text: str, confirm, navigate: bool) -> dict:
    # 3. The invitation dialog must appear. If it does not, we cannot tell what happened.
    if not _wait_for_dialog(page):
        page.wait_for_timeout(1500)
        if card_state(page)["pending"]:
            return {"result": "sent_without_note",
                    "reason": "Connect sent an invitation immediately with no dialog; the note was NOT delivered"}
        return _abort("no invitation dialog appeared (nothing seems to have been sent)")
    if _dlg(page).count() != 1:
        return _abort(f"expected one open dialog, found {_dlg(page).count()}", lambda: close_dialog(page))
    # The dialog itself names who the invitation is for ("Personalize your invitation to Cody Rice...")
    words = set(re.findall(r"[^\W\d_]+", _dlg(page).first.inner_text().lower()))
    if not set(_tokens(name)) <= words:
        return _abort(f"the invitation dialog does not name {name}", lambda: close_dialog(page))
    page.wait_for_timeout(500)

    add = next((n for n, r in _dialog_roles(page) if r == "add_note"), None)
    if add:
        _dlg(page).first.get_by_role("button", name=add, exact=True).click()
        page.wait_for_timeout(1000)
    area = _dlg(page).first.locator("textarea")
    if area.count() != 1:
        return _abort("could not find the note box; never sending without the note", lambda: close_dialog(page))

    # 4. Type the exact approved text and read it back, character for character
    limit = area.first.get_attribute("maxlength")
    if limit and len(text) > int(limit):
        return _abort(f"the note box allows {limit} characters; the approved text has {len(text)}",
                      lambda: close_dialog(page))
    area.first.fill(text)
    page.wait_for_timeout(400)
    if area.first.input_value() != text:
        return _abort("the note box did not hold the exact approved text", lambda: close_dialog(page))

    # 5. A last human check (visible-browser test runs)
    if confirm is not None and not confirm(text):
        return _abort("declined at the confirmation", lambda: close_dialog(page))

    # 6. Send, using only the plain Send button (never 'Send without a note')
    sends = [n for n, r in _dialog_roles(page) if r == "send"]
    if len(sends) != 1:
        return _abort(f"expected exactly one Send button, found {len(sends)}", lambda: close_dialog(page))
    _dlg(page).first.get_by_role("button", name=sends[0], exact=True).click()

    # 7. Settle on LinkedIn's own state: the dialog closes and the profile shows Pending
    for _ in range(16):
        if not _dlg(page).count():
            break
        page.wait_for_timeout(500)
    return _settle(page, navigate)


def _settle(page, navigate: bool) -> dict:
    """LinkedIn can take several seconds to show Pending, so look for up to about 25 seconds (reloading the
    profile at the start and again partway through) instead of checking once."""
    for attempt in range(6):
        if navigate and attempt in (0, 3):
            page.reload(wait_until="domcontentloaded")
        page.wait_for_timeout(3500 if attempt == 0 else 4000)
        if not _dlg(page).count() and card_state(page)["pending"]:
            return {"result": "sent", "reason": "LinkedIn shows the invitation as Pending"}
    return {"result": "unsettled",
            "reason": "Send was clicked but Pending was not confirmed. " + _observed(page)}


def _observed(page) -> str:
    """What the page showed when the check gave up, for the message to Raja."""
    try:
        dialogs = [" ".join(_dlg(page).nth(i).inner_text().split())[:120] for i in range(_dlg(page).count())]
        s = card_state(page)
        buttons = [t for t in page.evaluate(CARD_JS)["controls"] if t][:6]
        return (f"The page showed: {'a dialog: ' + dialogs[0] if dialogs else 'no dialog'}; "
                f"buttons {buttons}; degree {s['degree'] or 'unknown'}.")
    except Exception as e:
        return f"(could not read the page: {e})"
