"""Record a role Raja applied for himself, so the agent never tries it again and the tracker is right.

    APPLIED <linkedin job id>        (or just tell the agent on WhatsApp: "I applied to the NET-Group one")

The role is booked in the tracker as SUBMITTED, marked as applied by Raja himself (LinkedIn has not been asked), taken
off the queue, and noted in the ledger. If the agent already holds a waiting tracker row for it, that row is settled.
"""
import sys

from . import decisions, ledger, next_number, queue_tool, tracker


def find(job_id: str):
    """What the agent knows about this job id: {company, title, url}, or None."""
    jid = str(job_id).strip()
    for r in tracker.rows():
        if str(r.get("job_id")) == jid:
            return {"company": r["company"], "title": r["title"], "url": r.get("url", ""), "row": r}
    for q in queue_tool.all_items():
        if str(q.get("job_id")) == jid:
            return {"company": q["company"], "title": q["title"], "url": f"https://www.linkedin.com/jobs/view/{jid}/"}
    for d in decisions.all_decisions():
        if jid in [str(i) for i in d.get("job_ids") or []]:
            return {"company": d["company"], "title": d["title"], "url": d.get("link", "")}
    return None


def mark_applied(job_id: str):
    """Returns (ok, message)."""
    jid = str(job_id).strip()
    info = find(jid)
    if info is None:
        return False, f"I have no record of job {jid}, so I cannot book it."
    who = f"{info['title']} at {info['company']}"
    note = "Applied by Raja himself (from his WhatsApp message). LinkedIn was not asked."
    row = info.get("row")
    if row and row["status"] == "SUBMITTED":
        return True, f"{who} is already recorded as applied (#{row['number']})."
    if row and row["status"] == "AWAITING RAJA":
        tracker.settle(row["number"], "SUBMITTED", note)
        number = row["number"]
    else:
        number = next_number.reserve()
        tracker.book(number, info["company"], info["title"], jid, "SUBMITTED", url=info["url"], note=note,
                     extra={"manual": True})
    queue_tool.remove(jid)
    ledger.add(jid, "applied: you applied yourself")
    return True, f"Recorded #{number}: {who} as applied. It will not be attempted again."


if __name__ == "__main__":
    if len(sys.argv) == 2:
        print(mark_applied(sys.argv[1])[1])
    else:
        print(__doc__)
