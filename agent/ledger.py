"""seen_jobs.json: every processed job ID. A ledgered ID is never reopened or re-judged."""
from datetime import datetime, timezone

from .store import locked, read_json, write_json

FILE = "seen_jobs.json"


def seen_ids() -> set:
    return set(read_json(FILE, {}))


def new_ids(ids) -> list:
    seen = seen_ids()
    return [str(i) for i in ids if str(i) not in seen]


def add(job_id, outcome: str) -> None:
    """Record an ID at once. The first outcome recorded for an ID stands."""
    with locked("seen_jobs"):
        d = read_json(FILE, {})
        d.setdefault(str(job_id), {
            "first_seen": datetime.now(timezone.utc).isoformat(),
            "outcome": outcome,
        })
        write_json(FILE, d)
