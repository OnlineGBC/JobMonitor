"""Gate 2: duplicate check against the application tracker.

Same company + same role title already tracked = duplicate (including a repost
under a new job ID). The same client role listed by a different company is not.
"""
import re

from .store import read_json

TRACKER_FILE = "tracker.json"


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", (s or "").lower())).strip()


def key(company: str, title: str):
    return (norm(company), norm(title))


def is_duplicate(company: str, title: str, tracker=None):
    """Return (is_dup, reason)."""
    rows = tracker if tracker is not None else read_json(TRACKER_FILE, [])
    k = key(company, title)
    for r in rows:
        if key(r.get("company"), r.get("title")) == k:
            return True, f"already tracked (#{r.get('number', '?')})"
    return False, "ok"
