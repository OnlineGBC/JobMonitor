"""Confirm step for replies that were typed, not tapped.

A tapped button is deliberate, so it acts at once. A typed or free-text reply ("apply acme", "skip", "20 years")
is first interpreted (agent.interpret) and echoed back exactly as understood, with Confirm / Cancel buttons.
Nothing happens until you confirm. A confirmation that is not answered within 24 hours is dropped.

    CONFIRM <id>   /   CANCEL <id>     (or tap the buttons; a plain "yes" / "no" also works when only one is waiting)
"""
from datetime import datetime, timedelta

from . import reporter
from .store import locked, read_json, write_json

FILE = "pending_confirmations.json"
EXPIRE = timedelta(hours=24)


def _now() -> datetime:
    return datetime.now(reporter.NY)


def all_confirmations() -> list:
    return read_json(FILE, [])


def get(cid: str):
    return next((c for c in all_confirmations() if c["id"] == (cid or "").strip().lower()), None)


def open_ones(now: datetime = None) -> list:
    now = now or _now()
    return [c for c in all_confirmations()
            if c["status"] == "PENDING" and now - datetime.fromisoformat(c["created"]) <= EXPIRE]


def _set(cid: str, status: str) -> None:
    with locked("confirmations"):
        items = read_json(FILE, [])
        for c in items:
            if c["id"] == cid:
                c["status"], c["resolved"] = status, _now().isoformat(timespec="seconds")
        write_json(FILE, items)


def resolve(cid: str, accept: bool, run) -> str:
    """Carry out (accept) or drop (cancel) a pending confirmation. `run(command)` executes one command."""
    rec = get(cid)
    if rec is None:
        return f"No pending confirmation {cid}."
    if rec["status"] != "PENDING":
        return f"That was already {rec['status'].lower()}."
    if _now() - datetime.fromisoformat(rec["created"]) > EXPIRE:
        _set(rec["id"], "EXPIRED")
        return "That confirmation expired (over 24 hours old). Send your reply again."
    if not accept:
        _set(rec["id"], "CANCELLED")
        return f"Cancelled. Nothing was done: {rec['description']}"
    _set(rec["id"], "CONFIRMED")
    return "\n".join(run(c) for c in rec["commands"])
