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
      groups[key].options.push({id: el.id, text: lab || t(el.parentElement), aria, checked: el.checked});
    } else {
      const label = (lab || aria).replace(/\*$/, '').trim();
      fields.push({kind: el.tagName === 'SELECT' ? 'select' : (el.tagName === 'TEXTAREA' ? 'textarea' : 'text'),
        input_type: el.type, id: el.id, label, value: el.value,
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


def stem(name: str) -> str:
    return EXTENSIONS.sub("", name or "").strip().lower()


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


def choose_resume(page, group: dict, path: Path) -> str:
    """Select the resume by name (extension ignored); upload the local file if LinkedIn lacks it."""
    target = stem(path.name)
    match = next((o for o in group["options"] if stem(o["aria"]) == target), None)
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


def run_form(page, title: str, allow_submit: bool = False, decide=answers.decide_answer) -> dict:
    """Fill the open Easy Apply form. Returns {'status': 'ready'|'paused'|'skipped', ...}."""
    steps, learned = [], []
    resume = resumes.pick_resume(title)
    if resume is None:
        return _paused(f"no resume rule for the title '{title}'")
    bank = answers.load_bank()
    for _ in range(MAX_STEPS):
        step = read_step(page)
        label = f"page {step['page'][0]}/{step['page'][1]}" if step["page"] else "page ?"
        notes = []
        for f in step["fields"]:
            if f.get("resume"):
                notes.append(f"resume: {choose_resume(page, f, resume)}")
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
    page.keyboard.press("Escape")
    page.wait_for_timeout(1500)
    names = [b.strip() for b in page.locator("dialog[open] button").all_inner_texts() if b.strip()]
    tr = translate.translate(names)
    want = "save" if save else "discard"
    for n in names:
        if translate.button_role(tr.get(n, n)) == want:
            page.get_by_role("button", name=n).first.click()
            page.wait_for_timeout(1500)
            return want
    return "none"


def open_easy_apply(page, url: str) -> None:
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(3500)
    btn = page.get_by_role("button", name=re.compile(r"^(Easy Apply|Einfach bewerben)", re.I))
    if not btn.count():
        raise RuntimeError("no Easy Apply button on this posting")
    btn.first.click()
    page.wait_for_selector("dialog[open]", timeout=15000)
    page.wait_for_timeout(2000)


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
