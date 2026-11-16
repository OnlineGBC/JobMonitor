"""pending_survivors.json access. Always go through this module, never edit the file by hand.

    python -m agent.queue_tool list
    python -m agent.queue_tool claim
    python -m agent.queue_tool release <job_id>
    python -m agent.queue_tool status <job_id> "AWAITING RAJA"
"""
import sys
import uuid
from datetime import datetime, timedelta, timezone

from . import config
from .dup_gate import key
from .store import locked, read_json, write_json

FILE = "pending_survivors.json"
NOTE = "QUEUED — awaiting the application process."
QUEUED, CLAIMED = "QUEUED", "PICKUP IN PROGRESS"
HOLD = {"AWAITING RAJA", "DO NOT ATTEMPT"}


def _now():
    return datetime.now(timezone.utc)


def _live_claim(item) -> bool:
    if item["status"] != CLAIMED or not item.get("claimed_at"):
        return False
    age = _now() - datetime.fromisoformat(item["claimed_at"])
    return age < timedelta(minutes=config.CLAIM_STALE_MINUTES)


def add(posting: dict, force: bool = False):
    """Queue a passing posting. Returns (queued, reason).

    Hybrid/On-site postings are held out unless force=True (Raja's word).
    A queue twin (same company + title) is dropped at once.
    """
    workplace = (posting.get("workplace") or "").strip().lower()
    if workplace != "remote" and not force:
        return False, f"held out: workplace is {posting.get('workplace') or 'unknown'}"
    with locked("queue"):
        items = read_json(FILE, [])
        k = key(posting["company"], posting["title"])
        if any(key(i["company"], i["title"]) == k for i in items):
            return False, "queue twin"
        items.append({
            "job_id": str(posting["job_id"]), "title": posting["title"],
            "company": posting["company"], "location": posting.get("location", ""),
            "workplace": posting.get("workplace", ""),
            "applicants": posting.get("applicants"),
            "apply_type": posting.get("apply_type", ""),
            "poster": posting.get("poster"), "poster_url": posting.get("poster_url"),
            "note": NOTE, "status": QUEUED,
            "queued_at": _now().isoformat(), "claimed_at": None, "claim_id": None,
        })
        write_json(FILE, items)
    return True, "queued"


def claim():
    """Atomically claim the oldest eligible posting, or return None."""
    with locked("queue"):
        items = read_json(FILE, [])
        busy = {key(i["company"], "")[0] for i in items if _live_claim(i)}
        for i in sorted(items, key=lambda x: x["queued_at"]):
            eligible = i["status"] == QUEUED or (i["status"] == CLAIMED and not _live_claim(i))
            if not eligible or key(i["company"], "")[0] in busy:
                continue
            i.update(status=CLAIMED, claimed_at=_now().isoformat(), claim_id=uuid.uuid4().hex)
            # Drop queue twins of the claimed posting at once.
            k = key(i["company"], i["title"])
            items = [x for x in items if x is i or key(x["company"], x["title"]) != k]
            write_json(FILE, items)
            return i
    return None


def release(job_id: str) -> bool:
    return set_status(job_id, QUEUED)


def set_status(job_id: str, status: str) -> bool:
    with locked("queue"):
        items = read_json(FILE, [])
        for i in items:
            if i["job_id"] == str(job_id):
                i["status"] = status
                if status != CLAIMED:
                    i["claimed_at"] = i["claim_id"] = None
                write_json(FILE, items)
                return True
    return False


def bump_attempts(job_id: str) -> int:
    """Count a failed attempt on a posting and return the new total."""
    with locked("queue"):
        items = read_json(FILE, [])
        for i in items:
            if i["job_id"] == str(job_id):
                i["attempts"] = i.get("attempts", 0) + 1
                write_json(FILE, items)
                return i["attempts"]
    return 0


def remove(job_id: str) -> None:
    with locked("queue"):
        write_json(FILE, [i for i in read_json(FILE, []) if i["job_id"] != str(job_id)])


def all_items():
    return read_json(FILE, [])


if __name__ == "__main__":
    import json
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"
    if cmd == "list":
        print(json.dumps(all_items(), indent=2))
    elif cmd == "claim":
        print(json.dumps(claim(), indent=2))
    elif cmd == "release":
        print(release(sys.argv[2]))
    elif cmd == "status":
        print(set_status(sys.argv[2], sys.argv[3]))
    else:
        sys.exit(__doc__)
