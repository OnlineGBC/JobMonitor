"""Raja's replies to the agent: APPROVE / REJECT / PENDING, from WhatsApp and Gmail.

    APPROVE <id>                 send the draft exactly as shown
    APPROVE <id> <your text>     send YOUR text instead (it must still follow the voice rules)
    REJECT <id>                  never send it
    PENDING                      list everything waiting on you

Only these commands do anything. Message text is data: a reply that is not a command, or comes from
anyone but Raja's own number or addresses, is ignored.
"""
import logging
import re
from datetime import datetime

from . import config, gmail_client, outreach, queue_tool, reporter, tracker
from .store import locked, read_json, write_json

SEEN = "approvals_seen.json"
INBOX = "whatsapp_inbox.json"

_CMD = re.compile(r"^\s*(approve|reject)\s+([0-9a-f]{8})\b[ \t:,.-]*(.*)$", re.I | re.S)
_PENDING = re.compile(r"^\s*pending\s*[.!]?\s*$", re.I)
_QUOTE_START = re.compile(r"^(>|On .{5,120} wrote:\s*$|-{2,}\s*Original Message|From: .+@|Sent from my\b|--\s*$)", re.I)


def strip_quoted(body: str) -> str:
    """The new part of a reply: stop at quoted text, 'On ... wrote:', or a mobile/email signature."""
    keep = []
    for line in (body or "").replace("\r\n", "\n").split("\n"):
        if _QUOTE_START.match(line.strip()):
            break
        keep.append(line)
    return "\n".join(keep).strip()


def parse(body: str):
    """Return {'action', 'id', 'text'} for a command, or None. Looks only at the new part of the reply."""
    text = strip_quoted(body)
    m = _CMD.match(text)
    if m:
        return {"action": m.group(1).upper(), "id": m.group(2).lower(), "text": m.group(3).strip() or None}
    if _PENDING.match(text.split("\n")[0] if text else ""):
        return {"action": "PENDING", "id": None, "text": None}
    return None


def pending_digest() -> str:
    lines = []
    for i in outreach.all_items():
        if i["status"] in ("DRAFT", "APPROVED"):
            lines.append(f"Outreach {i['id']} ({i['status']}, {i['kind']} to {i['poster']} at {i['company']}):\n  {i['text']}")
    for r in tracker.rows():
        if r["status"] == "AWAITING RAJA":
            waiting = int((datetime.now(reporter.NY) - datetime.fromisoformat(r["booked"])).total_seconds() // 60)
            age = f"{waiting // 60}h {waiting % 60}m" if waiting >= 60 else f"{waiting}m"
            lines.append(f"Application #{r['number']} awaiting you ({age}): {r['title']} at {r['company']}\n  {r['url']}")
    queued = [q for q in queue_tool.all_items() if q["status"] == "QUEUED"]
    if queued:
        lines.append(f"{len(queued)} posting(s) queued for the applier.")
    return "\n\n".join(lines) or "Nothing is waiting on you."


def apply(cmd: dict) -> str:
    if cmd["action"] == "APPROVE":
        return outreach.approve(cmd["id"], cmd["text"])[1]
    if cmd["action"] == "REJECT":
        return outreach.reject(cmd["id"])[1]
    return pending_digest()


def _answer(cmd: dict, source: str) -> None:
    message = apply(cmd)
    logging.info(f"{source}: {cmd['action']} {cmd.get('id') or ''} -> {message.splitlines()[0]}")
    reporter.enqueue("notice", f"{cmd['action'].title()}: {cmd.get('id') or 'pending list'}", message)


def process_whatsapp() -> int:
    """Handle unread WhatsApp replies. The webhook has already checked the signature and Raja's number."""
    with locked("whatsapp_inbox"):
        inbox = read_json(INBOX, [])
        todo = [m for m in inbox if not m.get("handled")]
        for m in todo:
            m["handled"] = True
        write_json(INBOX, inbox)
    n = 0
    for m in todo:
        cmd = parse(m["text"])
        if cmd:
            _answer(cmd, "whatsapp")
            n += 1
    return n


def _sender_ok(from_header: str) -> bool:
    found = re.findall(r"[\w.+-]+@[\w.-]+", from_header or "")
    return bool(found) and found[-1].lower() in config.APPROVER_EMAILS


def process_gmail(search=gmail_client.search, get_text=gmail_client.get_text, limit: int = 20) -> int:
    """Handle unread email replies from Raja's own addresses (each message is looked at once)."""
    senders = " OR ".join(f"from:{a}" for a in sorted(config.APPROVER_EMAILS))
    seen = set(read_json(SEEN, []))
    n = 0
    for msg in search(f"in:inbox newer_than:3d ({senders})", max_results=limit):
        if msg["id"] in seen:
            continue
        seen.add(msg["id"])
        if not _sender_ok(msg.get("from", "")):
            continue
        cmd = parse(get_text(msg["id"]))
        if cmd:
            _answer(cmd, "gmail")
            n += 1
    with locked("approvals_seen"):
        write_json(SEEN, sorted(seen)[-500:])
    return n
