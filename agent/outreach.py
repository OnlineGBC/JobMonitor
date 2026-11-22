"""Outreach drafts: wording, voice rules, and the approval queue (outreach.json).

Nothing here sends anything. A draft is created for the job poster of an application, shown to Raja
word for word, and only sent after he approves THAT text (agent/approvals.py, agent/outreach_sender.py).

Voice (from the spec): short plain sentences, no em dashes, never "thanks" (always "thank you"),
sign-off "Thank you, Raja", invite notes within 300 characters, and the meeting line exactly
"I would love to discuss next steps."
"""
import re
import uuid
from datetime import datetime, timezone

from .store import locked, read_json, write_json

FILE = "outreach.json"
WATCH = "invite_watch.json"
MEETING_LINE = "I would love to discuss next steps."
SIGNOFF = "Thank you, Raja"
INVITE_LIMIT = 300
MESSAGE_LIMIT = 1500
STATUSES = {"DRAFT", "APPROVED", "SENT", "REJECTED", "FAILED", "UNSETTLED"}  # UNSETTLED: maybe sent, never retried

_DASHES = re.compile(r"[—–]")
_THANKS = re.compile(r"\bthanks\b", re.I)  # not 'thank': "thank you" is required
_HONORIFIC = re.compile(r"^(dr|mr|mrs|ms|prof)\.?$", re.I)


def _plain(s: str) -> str:
    """Inputs from LinkedIn may contain dashes the voice rules forbid; use a plain hyphen."""
    return re.sub(r"\s+", " ", _DASHES.sub("-", s or "")).strip()


def first_name(full: str) -> str:
    parts = [p for p in re.split(r"[\s,]+", _plain(full)) if p and not _HONORIFIC.match(p)]
    return parts[0] if parts else ""


def problems(text: str, limit: int = None) -> list:
    """Every way the text breaks the voice rules. Empty list means it is acceptable."""
    out = []
    if _DASHES.search(text):
        out.append("contains a dash (em or en dash)")
    if _THANKS.search(text):
        out.append("uses 'thanks' (always 'thank you')")
    if not re.search(r"thank you", text, re.I):
        out.append("does not say 'thank you'")
    if MEETING_LINE not in text:
        out.append(f"missing the exact line: {MEETING_LINE}")
    if not text.rstrip().endswith(SIGNOFF):
        out.append(f"does not end with the sign-off '{SIGNOFF}'")
    if limit and len(text) > limit:
        out.append(f"is {len(text)} characters (limit {limit})")
    return out


def _fit(template_lines: list, title: str, company: str, limit: int) -> str:
    """Choose the fullest wording that fits; as a last resort shorten the role title at a word boundary."""
    for body in template_lines:
        text = body.format(title=title, company=company)
        if len(text) <= limit:
            return text
    shortest = template_lines[-1]
    overflow = len(shortest.format(title=title, company=company)) - limit
    words = title.split()
    while overflow > 0 and len(words) > 1:
        overflow -= len(words.pop()) + 1
    return shortest.format(title=" ".join(words), company=company)


def invite_text(poster: str, title: str, company: str) -> str:
    first, title, company = first_name(poster), _plain(title), _plain(company)
    hello = f"Hello {first}, " if first else "Hello, "
    return _fit([
        hello + "I applied for the {title} role at {company}. "
        "My background is leading enterprise AI and data science programs. " + MEETING_LINE + " " + SIGNOFF,
        hello + "I applied for the {title} role at {company}. " + MEETING_LINE + " " + SIGNOFF,
    ], title, company, INVITE_LIMIT)


def followup_text(poster: str, title: str, company: str, applied: bool = True) -> str:
    """Message sent once an invitation is accepted. It says 'I applied' only if an application exists."""
    first, title, company = first_name(poster), _plain(title), _plain(company)
    hello = f"Hello {first}, " if first else "Hello, "
    context = "I applied for the {title} role at {company}. " if applied else "I came across the {title} role at {company}. "
    return _fit([hello + "thank you for connecting. " + context + MEETING_LINE + " " + SIGNOFF],
                title, company, MESSAGE_LIMIT)


# ---- the approval queue ---------------------------------------------------

def _now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")  # fine enough to order same-second approvals


def create(kind: str, job_id: str, poster: str, poster_url: str, company: str, title: str, text: str = None,
           requires_applied: bool = True) -> dict:
    """Create a DRAFT (kind 'invite' or 'message'). One draft per (kind, poster, job): a repeat returns the old one."""
    if kind not in ("invite", "message"):
        raise ValueError("kind must be 'invite' or 'message'")
    if text is None:
        text = (invite_text if kind == "invite" else followup_text)(poster, title, company)
    bad = problems(text, INVITE_LIMIT if kind == "invite" else MESSAGE_LIMIT)
    if bad:
        raise ValueError("draft breaks the voice rules: " + "; ".join(bad))
    with locked("outreach"):
        items = read_json(FILE, [])
        same = next((i for i in items if (i["kind"], i["poster_url"], i["job_id"]) == (kind, poster_url, str(job_id))), None)
        if same:
            return same
        item = {"id": uuid.uuid4().hex[:8], "kind": kind, "job_id": str(job_id), "poster": poster,
                "poster_url": poster_url, "company": company, "title": title, "text": text,
                "status": "DRAFT", "created": _now(), "approved": None, "sent": None, "attempts": 0, "error": None,
                # True: the sender sends only once LinkedIn shows the job as Applied (the note says "I applied")
                "requires_applied": requires_applied}
        items.append(item)
        write_json(FILE, items)
        return item


def announce(d: dict) -> None:
    """Show Raja a draft word for word and tell him how to answer. Nothing is sent by this."""
    from . import reporter
    gate = ("Approve only after you have submitted the application: the sender checks that LinkedIn shows "
            "you applied and waits if it does not." if d.get("requires_applied", True) else
            "This draft is not tied to an application, so it will be sent as soon as you approve it.")
    reporter.enqueue(
        "draft", f"Outreach draft {d['id']}: {d['kind']} to {d['poster']} at {d['company']}",
        f"{d['poster']} ({d['title']} at {d['company']}):\n{d['poster_url']}\n\n"
        f"{d['kind'].title()} ({len(d['text'])} characters), exactly as it would be sent:\n\n{d['text']}\n\n"
        f"Reply APPROVE {d['id']} to send it as shown, APPROVE {d['id']} followed by your own text to send "
        f"that instead, or REJECT {d['id']}.\n{gate}",
        # WhatsApp: the exact text, then three tappable buttons. A tap sends the same command as typing it.
        buttons=[(f"APPROVE {d['id']}", "Approve"), (f"REJECT {d['id']}", "Reject"), ("PENDING", "Pending list")],
        wa_text=(f"Outreach draft {d['id']}\n{d['kind'].title()} to {d['poster']} ({d['title']} at {d['company']}), "
                 f"exactly as it would be sent:\n\n{d['text']}\n\n{gate}\n\n"
                 "Tap Approve to send it as shown, or use WhatsApp's Reply on this message and type the exact "
                 "wording you want sent instead."),
        wa_prompt=f"Draft {d['id']} ({d['kind']} to {d['poster']}): tap to answer.", ref=f"draft {d['id']}")


def find(kind: str, poster_url: str, job_id: str):
    return next((i for i in all_items() if (i["kind"], i["poster_url"], i["job_id"]) == (kind, poster_url, str(job_id))), None)


def all_items() -> list:
    return read_json(FILE, [])


def get(item_id: str):
    return next((i for i in all_items() if i["id"] == item_id.lower()), None)


def _update(item_id: str, **fields) -> bool:
    with locked("outreach"):
        items = read_json(FILE, [])
        for i in items:
            if i["id"] == item_id.lower():
                i.update(fields)
                write_json(FILE, items)
                return True
    return False


def approve(item_id: str, text: str = None):
    """Approve a draft, optionally with Raja's own edited text. Returns (ok, message)."""
    item = get(item_id)
    if item is None:
        return False, f"No outreach draft with id {item_id}."
    if item["status"] != "DRAFT":
        return False, f"Draft {item_id} is already {item['status']}."
    final = (text if text is not None else item["text"]).strip()
    bad = problems(final, INVITE_LIMIT if item["kind"] == "invite" else MESSAGE_LIMIT)
    if bad:
        return False, f"Not approved. That text {'; '.join(bad)}. Reply again with APPROVE {item['id']} and corrected text."
    _update(item["id"], status="APPROVED", text=final, approved=_now())
    return True, f"Approved draft {item['id']}. It will be sent exactly as:\n{final}"


def reject(item_id: str):
    item = get(item_id)
    if item is None:
        return False, f"No outreach draft with id {item_id}."
    if item["status"] not in ("DRAFT", "APPROVED"):
        return False, f"Draft {item_id} is already {item['status']}."
    _update(item["id"], status="REJECTED")
    return True, f"Rejected draft {item['id']}. Nothing will be sent."


def approved() -> list:
    """Every approved draft, oldest approval first."""
    return sorted((i for i in all_items() if i["status"] == "APPROVED"), key=lambda x: x["approved"] or "")


def next_approved():
    return next((i for i in sorted(all_items(), key=lambda x: x["approved"] or "") if i["status"] == "APPROVED"), None)


def mark(item_id: str, status: str, error: str = None) -> None:
    if status not in STATUSES:
        raise ValueError(status)
    fields = {"status": status, "error": error}
    if status == "SENT":
        fields["sent"] = _now()
    _update(item_id, **fields)


def reopen(item_id: str):
    """Make a FAILED draft sendable again after the cause was fixed. Only for drafts that never went out.

    The text Raja approved is unchanged. SENT and UNSETTLED drafts are never reopened (something may
    have been sent), and a draft that was never approved cannot be reopened into approval.
    """
    item = get(item_id)
    if item is None:
        return False, f"No outreach draft with id {item_id}."
    if item["status"] != "FAILED" or item["sent"] or not item["approved"]:
        return False, f"Draft {item_id} is {item['status']} and cannot be reopened."
    _update(item["id"], status="APPROVED", error=None)
    return True, f"Draft {item['id']} is approved again with the same text."


def bump_attempts(item_id: str) -> int:
    with locked("outreach"):
        items = read_json(FILE, [])
        for i in items:
            if i["id"] == item_id.lower():
                i["attempts"] = i.get("attempts", 0) + 1
                write_json(FILE, items)
                return i["attempts"]
    return 0


# ---- accepted-invite watch list ------------------------------------------

def watch(poster_url: str, poster: str, job_id: str, company: str, title: str, applied: bool = True) -> None:
    """Add a sent invitation to the acceptance watch list. `applied`: the note said 'I applied'."""
    with locked("invite_watch"):
        w = read_json(WATCH, [])
        if not any(x["poster_url"] == poster_url and x["job_id"] == str(job_id) for x in w):
            w.append({"poster_url": poster_url, "poster": poster, "job_id": str(job_id), "company": company,
                      "title": title, "since": _now(), "accepted": False, "closed": False, "misses": 0,
                      "applied": applied})
            write_json(WATCH, w)


def watch_list() -> list:
    return read_json(WATCH, [])


def _watch_update(poster_url: str, job_id: str, **fields) -> None:
    with locked("invite_watch"):
        w = read_json(WATCH, [])
        for x in w:
            if x["poster_url"] == poster_url and x["job_id"] == str(job_id):
                x.update(fields)
        write_json(WATCH, w)


def mark_accepted(poster_url: str, job_id: str) -> None:
    _watch_update(poster_url, job_id, accepted=True)


def note_miss(poster_url: str, job_id: str, limit: int = 3) -> bool:
    """The invitation looked neither pending nor accepted. Close it only after `limit` such checks in a row
    (one odd page load must not end the watch). Returns True when it was closed."""
    with locked("invite_watch"):
        w = read_json(WATCH, [])
        closed = False
        for x in w:
            if x["poster_url"] == poster_url and x["job_id"] == str(job_id):
                x["misses"] = x.get("misses", 0) + 1
                x["closed"] = closed = x["misses"] >= limit
        write_json(WATCH, w)
        return closed


def clear_misses(poster_url: str, job_id: str) -> None:
    _watch_update(poster_url, job_id, misses=0)


def main(argv=None) -> int:
    """Create a draft by hand, e.g. for a first test. Nothing is sent; Raja approves it like any other.

        python -m agent.outreach draft --url URL --name "Full Name" --company Acme --title "VP of AI"
               [--text "exact note"] [--no-applied-check]
    """
    import argparse
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("draft")
    d.add_argument("--url", required=True)
    d.add_argument("--name", required=True)
    d.add_argument("--company", required=True)
    d.add_argument("--title", required=True)
    d.add_argument("--text")
    d.add_argument("--no-applied-check", action="store_true", help="send once approved, without waiting for 'Applied'")
    sub.add_parser("list")
    r = sub.add_parser("retry")
    r.add_argument("id")
    a = ap.parse_args(argv)
    if a.cmd == "retry":
        print(reopen(a.id)[1])
        return 0
    if a.cmd == "list":
        for i in all_items():
            print(f"{i['id']}  {i['status']:<9} {i['kind']:<7} {i['poster']} ({i['company']})")
        return 0
    item = create("invite", "manual-" + uuid.uuid4().hex[:6], a.name, a.url, a.company, a.title, a.text,
                  requires_applied=not a.no_applied_check)
    announce(item)
    print(f"Draft {item['id']} created and queued for your approval. Nothing is sent until you approve it.")
    print("Text:\n" + item["text"])
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
