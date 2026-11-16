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


def _abort(reason: str, close=None) -> dict:
    if close:
        close()
    return {"result": "aborted", "reason": reason}


def _dialog_roles(page) -> list:
    """[(text, role)] for the buttons of the open dialog, roles from the translation cache."""
    names = [t.strip() for t in page.locator("dialog[open] button").all_inner_texts() if t.strip()]
    tr = translate.translate(names)
    return [(n, translate.button_role(tr.get(n, n))) for n in names]


def close_dialog(page) -> None:
    """Close whatever dialog is open WITHOUT sending: its close button, Escape, then Discard if asked."""
    for _ in range(3):
        if not page.locator("dialog[open]").count():
            return
        roles = _dialog_roles(page)
        discard = next((n for n, r in roles if r == "discard"), None)
        close = next((n for n, r in roles if r == "close"), None)
        if discard:
            page.get_by_role("button", name=discard).first.click()
        elif close:
            page.locator("dialog[open]").last.get_by_role("button", name=close).first.click()
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

    # 2. The ONE Connect that may be clicked: inside the profile's own More menu
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

    # 3. The invitation dialog must appear. If it does not, we cannot tell what happened.
    try:
        page.wait_for_selector("dialog[open]", timeout=7000)
    except Exception:
        page.wait_for_timeout(1500)
        if card_state(page)["pending"]:
            return {"result": "sent_without_note",
                    "reason": "Connect sent an invitation immediately with no dialog; the note was NOT delivered"}
        return _abort("no invitation dialog appeared (nothing seems to have been sent)")
    page.wait_for_timeout(800)

    roles = _dialog_roles(page)
    add = next((n for n, r in roles if r == "add_note"), None)
    if add:
        page.locator("dialog[open]").last.get_by_role("button", name=add).first.click()
        page.wait_for_timeout(1000)
    area = page.locator("dialog[open] textarea")
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
    page.locator("dialog[open]").last.get_by_role("button", name=sends[0], exact=True).click()

    # 7. Settle on LinkedIn's own state: the dialog closes and the profile shows Pending
    for _ in range(16):
        if not page.locator("dialog[open]").count():
            break
        page.wait_for_timeout(500)
    if navigate:
        page.reload(wait_until="domcontentloaded")
        page.wait_for_timeout(3500)
    if not page.locator("dialog[open]").count() and card_state(page)["pending"]:
        return {"result": "sent", "reason": "LinkedIn shows the invitation as Pending"}
    return {"result": "unsettled", "reason": "Send was clicked but Pending was not confirmed"}
