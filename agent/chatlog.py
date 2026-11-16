"""The WhatsApp conversation, kept on disk (whatsapp_log.json), so a question like "more details on that error" can be
answered from what was really said, not from whatever the outbox still holds.

Every message the agent delivers to WhatsApp is added here (full text), and every message Raja sends. The most
recent 2000 are kept. The reply reader (agent.router) is given the last 50.
"""
from datetime import datetime

from .store import locked, read_json, write_json

FILE = "whatsapp_log.json"
CAP = 2000


def _now() -> str:
    from .reporter import NY
    return datetime.now(NY).isoformat(timespec="seconds")


def add(who: str, subject: str, text: str) -> None:
    """who is 'agent' or 'raja'. Never raises: the log must not be able to break a send."""
    try:
        with locked("whatsapp_log"):
            items = read_json(FILE, None)
            if items is None:
                items = _seed()
            items.append({"ts": _now(), "who": who, "subject": subject or "", "text": text or ""})
            write_json(FILE, items[-CAP:])
    except Exception:
        pass


def _seed() -> list:
    """First use: start from what the outbox still holds, so the log is not empty."""
    try:
        out = read_json("outbox.json", [])
    except Exception:
        return []
    return [{"ts": None, "who": "agent", "subject": e.get("subject", ""), "text": e.get("body", "")}
            for e in out if e.get("whatsapp") == "sent"]


def last(n: int = 50) -> list:
    items = read_json(FILE, None)
    if items is None:
        items = _seed()
    return items[-n:]
