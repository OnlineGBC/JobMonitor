"""Easy Apply form driver (supervised: fills to the Review page, never submits unless allow_submit).

    python -m agent.easy_apply --job 4489709428            # fill, then DISCARD (leaves nothing behind)
    python -m agent.easy_apply --job 4489709428 --save     # fill, then save the draft on LinkedIn

A form is read through Playwright locators. LinkedIn renders it in a native <dialog>, its UI text can be
in several languages (translated once by Haiku and cached), and answers are decided by answers.py.
"""
import argparse
import logging
import re
import sys
from pathlib import Path

from . import answers, discovery, resumes, translate

MAX_STEPS = 8
EXTENSIONS = re.compile(r"\.(docx?|pdf)$", re.I)

READ_JS = r"""(d) => {
  const t = e => (e ? (e.innerText || e.textContent || '').trim().replace(/\s+/g, ' ') : '');
  const flat = t(d);
  const starred = q => !!q && flat.includes(q + '*');
  // Visible text of a radio/checkbox option: the nearest enclosing block that holds only this input.
  const optText = el => {
    for (let n = el.parentElement, k = 0; n && k < 5; n = n.parentElement, k++) {
      if (n.querySelectorAll('input').length !== 1) break;
      const x = t(n);
      if (x) return x;
    }
    return '';
  };
  const fields = [], groups = {};
  for (const el of d.querySelectorAll('input,select,textarea')) {
    if (el.type === 'hidden' || el.type === 'file') continue;
    const aria = el.getAttribute('aria-label') || '';
    const lab = el.labels && el.labels[0] ? t(el.labels[0]) : '';
    if (el.type === 'radio' || el.type === 'checkbox') {
      const key = el.type + ':' + (el.name || el.id);
      if (!groups[key]) {
        const fs = el.closest('fieldset');
        groups[key] = {kind: el.type, prev: fs && fs.previousElementSibling ? t(fs.previousElementSibling) : '', options: []};
        fields.push(groups[key]);
      }
      groups[key].options.push({id: el.id, text: lab || optText(el), aria, checked: el.checked});
    } else {
      const label = (lab || aria).replace(/\*$/, '').trim();
      fields.push({kind: el.tagName === 'SELECT' ? 'select' : (el.tagName === 'TEXTAREA' ? 'textarea' : 'text'),
        input_type: el.type, id: el.id, label, value: el.value,
        // visible text of the chosen option; index 0 is the placeholder ("Select an option"), i.e. no answer
        selected: el.tagName === 'SELECT' && el.selectedIndex > 0 ? el.options[el.selectedIndex].text.trim() : '',
        required: el.required || el.getAttribute('aria-required') === 'true' || /\*$/.test(lab) || starred(aria),
        options: el.tagName === 'SELECT' ? [...el.options].map(o => o.text.trim()) : []});
    }
  }
  const pm = flat.match(/(\d+)\s*\/\s*(\d+)\s*pages?/i);
  return {
    page: pm ? [+pm[1], +pm[2]] : null, text: flat.slice(0, 1200), fields,
    errors: [...d.querySelectorAll('[role=alert], .artdeco-inline-feedback--error')].map(t).filter(Boolean),
    buttons: [...d.querySelectorAll('button')].filter(b => b.offsetParent).map(t).filter(Boolean),
    flat,
  };
}"""


stem = resumes.stem  # one definition of "same resume": the name without docx/doc/pdf


def _dlg(page):
    return page.locator("dialog[open]").first


def _is_resume_group(group: dict) -> bool:
    return group["kind"] == "radio" and bool(group["options"]) and all(
        EXTENSIONS.search(o["aria"] or "") for o in group["options"])


def read_step(page) -> dict:
    """Read the open form page and normalise it. Group questions end '*' when required."""
    raw = _dlg(page).evaluate(READ_JS)
    ui = list(raw["buttons"])
    for f in raw["fields"]:
        if f["kind"] in ("radio", "checkbox") and not _is_resume_group(f):
            ui += [o["text"] for o in f["options"]]
    tr = translate.translate(ui)
    raw["buttons_info"] = [{"text": b, "en": tr.get(b, b), "role": translate.button_role(tr.get(b, b))}
                           for b in raw["buttons"]]
    fields = []
    for f in raw["fields"]:
        if f["kind"] in ("radio", "checkbox"):
            q = f["prev"] or (f["options"][0]["aria"] if f["options"] else "")
            fields.append({
                "kind": f["kind"], "resume": _is_resume_group(f),
                "label": q.rstrip("*").strip(), "required": q.endswith("*"),
                "options": [{"id": o["id"], "text": o["text"], "en": tr.get(o["text"], o["text"]),
                             "aria": o["aria"], "checked": o["checked"]} for o in f["options"]]})
        else:
            fields.append({**f, "resume": False})
    raw["fields"] = fields
    return raw


def _click_option(page, option_id: str) -> None:
    _dlg(page).locator(f'[id="{option_id}"]').evaluate("e => e.click()")


def _contact_field(f: dict) -> bool:
    return bool(re.search(r"e-?mail|phone|mobile|country code", f.get("label", ""), re.I))


class ResumeUnavailable(Exception):
    """The wanted resume is not on LinkedIn and there is no local file to upload."""


def choose_resume(page, group: dict, name: str, path: Path = None) -> str:
    """Select the resume `name` already on LinkedIn (extension ignored).

    Uploads `path` only when LinkedIn lacks the resume AND a local file exists; otherwise raises
    ResumeUnavailable so the application pauses instead of using a different resume.
    """
    target = stem(name)
    match = next((o for o in group["options"] if stem(o["aria"]) == target), None)
    if match is None and path is None:
        raise ResumeUnavailable(f"resume '{name}' is not on LinkedIn and not in the resumes folder")
    if match is None:
        info = read_step(page)["buttons_info"]
        up = next((b for b in info if b["role"] == "upload"), None)
        if up is None:
            raise RuntimeError("resume not on LinkedIn and no upload button found")
        with page.expect_file_chooser() as fc:
            _dlg(page).get_by_role("button", name=up["text"]).first.click()
        fc.value.set_files(str(path))
        page.wait_for_timeout(5000)
        group = next(f for f in read_step(page)["fields"] if f.get("resume"))
        match = next((o for o in group["options"] if stem(o["aria"]) == target), None)
        if match is None:
            raise RuntimeError("uploaded resume did not appear in the list")
    if not match["checked"]:
        _click_option(page, match["id"])
        page.wait_for_timeout(600)
    now = next(f for f in read_step(page)["fields"] if f.get("resume"))
    if not any(o["checked"] and stem(o["aria"]) == target for o in now["options"]):
        raise RuntimeError("resume selection did not hold")
    return match["aria"]


def _current(f: dict) -> str:
    if f["kind"] in ("radio", "checkbox"):
        return next((o["en"] for o in f["options"] if o["checked"]), "")
    if f["kind"] == "select":
        return f.get("selected", "")  # the option's visible text, never its internal code
    return (f.get("value") or "").strip()


def _apply(page, f: dict, value: str) -> None:
    if f["kind"] in ("radio", "checkbox"):
        opt = next(o for o in f["options"] if o["en"].lower() == value.lower())
        _click_option(page, opt["id"])
    elif f["kind"] == "select":
        _dlg(page).locator(f'[id="{f["id"]}"]').select_option(label=value)
    else:
        loc = _dlg(page).locator(f'[id="{f["id"]}"]')
        loc.fill("")
        loc.fill(value)


def _paused(reason, question=None, steps=None, learned=None):
    return {"status": "paused", "reason": reason, "question": question,
            "steps": steps or [], "learned": learned or []}


def run_form(page, title: str, allow_submit: bool = False, decide=answers.decide_answer, company: str = None) -> dict:
    """Fill the open Easy Apply form. Returns {'status': 'ready'|'submitted'|'paused'|'skipped', ...}.

    With allow_submit=False (the default, and what the applier uses) it stops at the review page. With
    allow_submit=True it also clicks Submit there, after submit_application's checks, and `company` is required.
    """
    steps, learned = [], []
    resume = resumes.resume_name(title)
    if resume is None:
        return _paused(f"no resume rule for the title '{title}'")
    resume_file = resumes.local_path(title)  # may be None: the resume can live on LinkedIn only
    bank = answers.load_bank()
    for _ in range(MAX_STEPS):
        step = read_step(page)
        label = f"page {step['page'][0]}/{step['page'][1]}" if step["page"] else "page ?"
        notes = []
        for f in step["fields"]:
            if f.get("resume"):
                try:
                    notes.append(f"resume: {choose_resume(page, f, resume, resume_file)}")
                except ResumeUnavailable as e:
                    return _paused(str(e), None, steps, learned)
                continue
            if f["kind"] in ("radio", "checkbox") and not f["options"]:
                continue
            cur = _current(f)
            if _contact_field(f):
                if f["required"] and not cur:
                    return _paused(f"contact field '{f['label']}' is empty", f["label"], steps, learned)
                continue
            q = {"label": f["label"], "type": f["kind"], "required": f.get("required", False),
                 "options": ([o["en"] for o in f["options"]] if f["kind"] in ("radio", "checkbox")
                             else f.get("options", [])[1:] if f["kind"] == "select" else [])}
            d = decide(q, bank=bank)
            if d["action"] == "skip_application":
                return {"status": "skipped", "reason": d["reason"], "question": f["label"],
                        "steps": steps, "learned": learned}
            if d["action"] == "blank":
                continue
            if d["action"] == "pause":
                if cur and answers.acceptable_earlier_answer(f["label"], cur):
                    if answers.learn(f["label"], cur):
                        learned.append((f["label"], cur))
                    notes.append(f"kept your earlier answer '{cur}' for: {f['label']}")
                    continue
                return _paused(d["reason"], f["label"], steps, learned)
            if d["value"].strip().lower() != cur.lower():
                _apply(page, f, d["value"])
                notes.append(f"set '{d['value']}' for: {f['label']}")
            else:
                notes.append(f"already '{cur}' for: {f['label']}")
        steps.append(f"{label}: " + ("; ".join(notes) if notes else "nothing to fill"))
        page.wait_for_timeout(500)

        after = read_step(page)
        btns = {b["role"]: b for b in after["buttons_info"]}
        advance = btns.get("next") or btns.get("review")
        if advance is None:
            if "submit" in btns:
                if allow_submit:
                    try:
                        sent = submit_application(page, company, btns["submit"]["text"])
                    except SubmitRefused as e:
                        return _paused(f"refused to submit: {e}", None, steps, learned)
                    return {"status": "submitted", "reason": sent["evidence"] or "Submit was clicked",
                            "confirmed": sent["confirmed"], "steps": steps, "learned": learned}
                return {"status": "ready", "reason": "reached the final review page",
                        "steps": steps, "learned": learned, "review": after["text"]}
            return _paused("no Next/Review button on this page", None, steps, learned)
        _dlg(page).get_by_role("button", name=advance["text"]).first.click()
        page.wait_for_timeout(2500)
        nxt = read_step(page)
        if nxt["errors"] or (nxt["page"] and step["page"] and nxt["page"][0] == step["page"][0]):
            return _paused(f"the form rejected the page: {'; '.join(nxt['errors']) or 'it did not advance'}",
                           None, steps, learned)
    return _paused("too many pages", None, steps, learned)


def close_form(page, save: bool) -> str:
    """Close the dialog, choosing Save or Discard on LinkedIn's confirmation. Returns what was done."""
    want = "save" if save else "discard"
    for _ in range(3):
        names = [b.strip() for b in page.locator("dialog[open] button").all_inner_texts() if b.strip()]
        tr = translate.translate(names)
        choice = next((n for n in names if translate.button_role(tr.get(n, n)) == want), None)
        if choice:  # the Save/Discard confirmation is showing
            page.get_by_role("button", name=choice).first.click()
            page.wait_for_timeout(1500)
            if not page.locator("dialog[open]").count():
                return want
        else:
            page.keyboard.press("Escape")  # ask LinkedIn to close the form; it shows the confirmation
            page.wait_for_timeout(1500)
        if not page.locator("dialog[open]").count():
            return "closed"
    return "none"


# The accessible name is longer than the visible text ("Easy Apply to this job"), so match the START of it.
_APPLY_CONTROL = re.compile(r"^(Easy Apply|Continue|Einfach bewerben|Fortfahren)\b", re.I)


def _top_card_apply_controls(page) -> list:
    """The Easy Apply / Continue controls in the posting's OWN top card (never a sidebar or another job's card).

    'Easy Apply' is a button on a fresh posting; once a draft is saved the same place shows 'Continue', a link.
    """
    found = []
    for role in ("button", "link"):
        loc = page.get_by_role(role, name=_APPLY_CONTROL)
        for i in range(loc.count()):
            box = loc.nth(i).bounding_box()
            if box and box["y"] < 600 and box["x"] < 1000:
                found.append(loc.nth(i))
    return found


def open_easy_apply(page, url: str) -> None:
    """Open the application dialog: a fresh one via 'Easy Apply', or a saved draft via 'Continue'. A saved draft
    reopens at its first page, so run_form walks forward through the pages again."""
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(3500)
    controls = _top_card_apply_controls(page)
    if not controls:
        raise RuntimeError("no Easy Apply or Continue control in this posting's top card")
    if len(controls) != 1:
        raise RuntimeError(f"expected one Easy Apply/Continue control in the top card, found {len(controls)}")
    controls[0].click()
    page.wait_for_selector("dialog[open]", timeout=15000)
    page.wait_for_timeout(2000)


class SubmitRefused(Exception):
    """A safety check failed, so Submit was not clicked."""


SUCCESS = re.compile(r"application (was )?sent|application submitted|was sent to|successfully submitted|gesendet", re.I)


def submit_application(page, company: str, submit_label: str) -> dict:
    """Click Submit on the review page, but only after the checks. Raises SubmitRefused if any fails.

    Checks: exactly one dialog is open; it names this company; it has exactly one Submit button and no
    Next/Review. Afterwards it waits for LinkedIn's confirmation and closes the success dialog. Settling
    ("Applied" on the job tracker AND the posting page) is done by agent.reconcile, not here.
    """
    if not company or not company.strip():
        raise SubmitRefused("no company was given to check the dialog against")
    if page.locator("dialog[open]").count() != 1:
        raise SubmitRefused(f"expected one open dialog, found {page.locator('dialog[open]').count()}")
    words = set(re.findall(r"[^\W\d_]+", _dlg(page).inner_text().lower()))
    if not set(re.findall(r"[^\W\d_]+", company.lower())) <= words:
        raise SubmitRefused(f"the dialog does not name {company}")
    roles = [b["role"] for b in read_step(page)["buttons_info"]]
    if roles.count("submit") != 1 or "next" in roles or "review" in roles:
        raise SubmitRefused(f"this is not the final review page (buttons: {roles})")
    _dlg(page).get_by_role("button", name=submit_label, exact=True).click()
    evidence = ""
    for _ in range(48):                                   # up to 12 s
        page.wait_for_timeout(250)
        evidence = _dlg(page).inner_text() if page.locator("dialog[open]").count() else ""
        if not evidence:                                   # the dialog closed: the confirmation may be a page toast
            toast = SUCCESS.search(page.inner_text("body"))
            evidence = toast.group(0) if toast else ""
            break
        if SUCCESS.search(evidence):
            break
    confirmed = bool(SUCCESS.search(evidence))
    for _ in range(3):                                    # dismiss the success dialog if there is one
        if not page.locator("dialog[open]").count():
            break
        labels = _dlg(page).evaluate(
            "d => [...d.querySelectorAll('button')].map(b => (b.getAttribute('aria-label') || b.innerText || '').trim()).filter(Boolean)")
        tr = translate.translate(labels)
        close = next((l for l in labels if translate.button_role(tr.get(l, l)) in ("close", "discard")), None)
        if close and "submit" not in translate.button_role(tr.get(close, close)):
            _dlg(page).get_by_role("button", name=close, exact=True).first.click()
        else:
            page.keyboard.press("Escape")
        page.wait_for_timeout(600)
    return {"clicked": True, "confirmed": confirmed, "evidence": " ".join(evidence.split())[:200]}


def main():
    from playwright.sync_api import sync_playwright
    ap = argparse.ArgumentParser()
    ap.add_argument("--job", required=True)
    ap.add_argument("--save", action="store_true", help="save the draft instead of discarding")
    ap.add_argument("--headed", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    url = f"https://www.linkedin.com/jobs/view/{a.job}/"
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not a.headed)
        try:
            ctx = browser.new_context(storage_state=str(discovery.STATE), viewport={"width": 1500, "height": 1000})
            page = ctx.new_page()
            detail = discovery.read_detail(page, url)
            print("Posting:", detail["company"], "|", detail["title"])
            if detail["not_accepting"]:
                sys.exit("This posting is no longer accepting applications. Nothing was opened.")
            open_easy_apply(page, url)
            try:
                result = run_form(page, detail["title"])
            finally:
                closed = close_form(page, save=a.save)
            print("STATUS:", result["status"], "|", result["reason"])
            if result.get("question"):
                print("QUESTION:", result["question"])
            for s in result["steps"]:
                print("  ", s)
            for q, v in result["learned"]:
                print("   LEARNED:", q, "->", v)
            print("closed with:", closed, "| dialog still open:", page.locator("dialog[open]").count() > 0)
        finally:
            browser.close()


if __name__ == "__main__":
    main()
