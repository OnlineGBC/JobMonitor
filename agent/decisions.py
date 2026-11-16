"""Easy Apply roles the monitor emails found, offered to Raja on WhatsApp with Apply / Skip buttons.

The monitor change alerts list new roles. For each Easy Apply role that has not been applied for, one WhatsApp
message is sent (WhatsApp only; the email already carries the list) with two tappable buttons:

    Apply  -> the role is queued for the applier, which fills it in and submits it (when automatic submission is on)
    Skip   -> the role is never applied for, and is taken off the queue if it was waiting there

Typed replies work too:  DECIDE <id> APPLY   /   DECIDE <id> SKIP
Your choice overrides the agent's own title and criteria rules. It does not override the safety rules in the
applier (duplicates, a closed posting, an application already on LinkedIn, hard answer rules).

    python -m agent.decisions list
    python -m agent.decisions decide <id> apply|skip
"""
import re
import sys
import uuid
from datetime import datetime

from . import ledger, queue_tool, reporter, tracker
from .dup_gate import key
from .store import locked, read_json, write_json

FILE = "pending_decisions.json"
MAX_OFFERS_PER_EMAIL = 8


def _now() -> str:
    return datetime.now(reporter.NY).isoformat(timespec="seconds")


def all_decisions() -> list:
    return read_json(FILE, [])


def get(did: str):
    return next((d for d in all_decisions() if d["id"] == (did or "").strip().lower()), None)


def _update(did: str, **fields) -> None:
    with locked("decisions"):
        items = read_json(FILE, [])
        for d in items:
            if d["id"] == did:
                d.update(fields)
        write_json(FILE, items)


def buttons(rec: dict) -> list:
    return [(f"DECIDE {rec['id']} APPLY", "Apply"), (f"DECIDE {rec['id']} SKIP", "Skip")]


def _text(rec: dict) -> str:
    place = f" ({rec['location']})" if rec.get("location") else ""
    parts = [f"New Easy Apply role\n{rec['title']} — {rec['company']}{place}"]
    if rec.get("summary"):
        parts.append(f"Fit: {rec['summary']}")
    if rec.get("note"):
        parts.append(rec["note"])
    parts.append(rec["link"])
    parts.append("Apply it, or skip it?")
    return "\n\n".join(parts)[:1000]


def already_handled(job: dict) -> bool:
    """True if this role was already offered, queued, started or applied for."""
    k = key(job.get("company", ""), job.get("title", ""))
    ids = {str(i) for i in job.get("job_ids") or []}
    if any(key(d["company"], d["title"]) == k for d in all_decisions()):
        return True
    if any(str(r.get("job_id")) in ids or key(r["company"], r["title"]) == k for r in tracker.rows()):
        return True
    return any(str(q.get("job_id")) in ids or key(q["company"], q["title"]) == k for q in queue_tool.all_items())


def offer(job: dict, note: str = "") -> dict:
    """Record one role and send it to WhatsApp with Apply / Skip buttons. Returns the record, or None if the
    role was already offered, queued, started or applied for."""
    if already_handled(job):
        return None
    ids = [str(i) for i in job.get("job_ids") or ([job["job_id"]] if job.get("job_id") else [])]
    if not ids:
        return None
    places = job.get("places") or []
    location = ", ".join(places[:3]) + (f" +{len(places) - 3} more" if len(places) > 3 else "") if places \
        else job.get("location", "")
    rec = {"id": uuid.uuid4().hex[:8], "job_ids": ids, "title": job["title"], "company": job.get("company", ""),
           "location": ("Remote: " if "(remote)" in (job.get("location") or "").lower() else "") + location,
           "link": (job.get("urls") or [job.get("url", "")])[0], "summary": job.get("summary", ""), "note": note,
           "status": "OPEN", "created": _now()}
    with locked("decisions"):
        items = read_json(FILE, [])
        items.append(rec)
        write_json(FILE, items)
    reporter.enqueue("question", f"Easy Apply role: {rec['title']} at {rec['company']}", _text(rec), buttons=buttons(rec),
                     wa_text=_text(rec), wa_prompt=f"{rec['title']} at {rec['company']}: apply or skip?", email=False)
    return rec


def decide(did: str, choice: str):
    """Apply Raja's choice. Returns (ok, message)."""
    rec = get(did)
    if rec is None:
        return False, f"No role with id {did}."
    if rec["status"] != "OPEN":
        return False, f"{rec['title']} at {rec['company']} was already decided ({rec['status'].lower()})."
    c = (choice or "").strip().lower()
    who = f"{rec['title']} at {rec['company']}"
    if c in ("apply", "yes", "y"):
        posting = {"job_id": rec["job_ids"][0], "title": rec["title"], "company": rec["company"],
                   "location": rec["location"], "workplace": "", "apply_type": "Easy Apply"}
        queued, why = queue_tool.add(posting, forced=True)
        if not queued and why != "queue twin":
            return False, f"Could not queue {who}: {why}"
        for jid in rec["job_ids"]:
            ledger.add(jid, "queued: you chose to apply")
        _update(rec["id"], status="APPLY", decided=_now())
        try:
            from . import scheduler
            scheduler.run_soon("applier")
        except Exception:
            pass
        return True, (f"Queued {who}. The applier starts within about a minute, fills it in and submits it if "
                      f"automatic submission is on. You will get the usual report.")
    if c in ("skip", "no", "n"):
        for jid in rec["job_ids"]:
            ledger.add(jid, "skipped: you chose to skip")
            item = next((q for q in queue_tool.all_items() if q["job_id"] == jid and q["status"] == queue_tool.QUEUED), None)
            if item:
                queue_tool.remove(jid)
        _update(rec["id"], status="SKIPPED", decided=_now())
        return True, f"Skipped {who}. It will not be applied for."
    return False, f"'{choice}' is not APPLY or SKIP. Reply DECIDE {rec['id']} APPLY or DECIDE {rec['id']} SKIP."


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] == "list":
        open_ = [d for d in all_decisions() if d["status"] == "OPEN"]
        for d in open_:
            print(f"{d['id']}  {d['title']} — {d['company']}  {d['link']}")
        print(f"{len(open_)} role(s) waiting for your decision." if open_ else "Nothing waiting for your decision.")
        return 0
    if args[0] == "decide" and len(args) == 3:
        ok, msg = decide(args[1], args[2])
        print(msg)
        return 0 if ok else 1
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
