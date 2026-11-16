"""Reporter: the only component that sends messages to Raja.

The scan, applier and sender call enqueue(); this module delivers each event by
email (always) and WhatsApp (when the 24h window is open).

    python -m agent.reporter          # deliver everything pending, then the keepalive check
"""
import logging
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from . import gmail_client, whatsapp
from .store import locked, read_json, write_json

OUTBOX = "outbox.json"
NY = ZoneInfo("America/New_York")

# Only these are ever reported. Skipped matches and employer rejections are not on the
# list on purpose: enqueue() refuses anything else.
REPORTABLE = {
    "applied",    # a submitted application, settled on an independent record
    "paused",     # a settled pause (AWAITING RAJA)
    "question",   # a question only Raja can answer
    "draft",      # outreach text awaiting his approval
    "needs_you",  # sign-in, code, approval: he must step in
    "cap",        # a run hit the 60-minute cap
    "digest",     # pending list
    "notice",     # acknowledgement of a command Raja sent (approved, rejected, not valid, pending list)
}
MAX_ATTEMPTS = 10


def ny_now() -> str:
    return datetime.now(NY).strftime("%Y-%m-%d %I:%M %p %Z")


def enqueue(kind: str, subject: str, body: str, needs_action: bool = False) -> str:
    if kind not in REPORTABLE:
        raise ValueError(f"'{kind}' is not a reportable event")
    if needs_action or kind in {"question", "draft", "needs_you"}:
        subject = f"{subject} — updates needed"
    event = {"id": uuid.uuid4().hex[:8], "kind": kind, "subject": subject,
             "body": f"{body}\n\nNew York time: {ny_now()}",
             "email_sent": False, "whatsapp": "pending", "attempts": 0}
    with locked("outbox"):
        items = read_json(OUTBOX, [])
        items.append(event)
        write_json(OUTBOX, items)
    return event["id"]


def process(send_email=gmail_client.send, send_whatsapp=whatsapp.send_text) -> dict:
    """Deliver pending events. Email failures retry next run; WhatsApp never blocks email."""
    stats = {"emailed": 0, "whatsapp": 0, "whatsapp_closed": 0, "failed": 0}
    with locked("outbox"):
        items = read_json(OUTBOX, [])
        for e in items:
            if not e["email_sent"] and e["attempts"] < MAX_ATTEMPTS:
                try:
                    send_email(e["subject"], e["body"])
                    e["email_sent"] = True
                    stats["emailed"] += 1
                except Exception as ex:
                    e["attempts"] += 1
                    stats["failed"] += 1
                    logging.error(f"Email for event {e['id']} failed: {ex}")
            if e["whatsapp"] == "pending":
                try:
                    if send_whatsapp(f"{e['subject']}\n\n{e['body']}"):
                        e["whatsapp"] = "sent"
                        stats["whatsapp"] += 1
                    else:
                        e["whatsapp"] = "window_closed"  # email carries it
                        stats["whatsapp_closed"] += 1
                except Exception as ex:
                    e["whatsapp"] = "failed"
                    logging.error(f"WhatsApp for event {e['id']} failed: {ex}")
        # keep recent history, drop what is fully delivered and old
        write_json(OUTBOX, [e for e in items if not e["email_sent"]] +
                   [e for e in items if e["email_sent"]][-100:])
    return stats


def keepalive(send=whatsapp.send_keepalive) -> bool:
    """Send the 12-hourly check-in template. Never raises: it is a convenience."""
    try:
        return send()
    except Exception as ex:
        logging.warning(f"Keepalive not sent: {ex}")
        return False


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    print(process(), "keepalive sent:", keepalive())
