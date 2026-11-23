"""Reporter: the only component that sends messages to Raja.

The scan, applier and sender call enqueue(); this module delivers each event by
email (always) and WhatsApp (when the 24h window is open).

    python -m agent.reporter          # deliver everything pending, then the keepalive check
"""
import logging
import time
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from . import chatlog, gmail_client, whatsapp
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
HOLD_SECONDS = 72 * 3600     # a WhatsApp-only message the closed window could not carry is kept this long


def ny_now() -> str:
    return datetime.now(NY).strftime("%Y-%m-%d %I:%M %p %Z")


def enqueue(kind: str, subject: str, body: str, needs_action: bool = False, buttons: list = None,
            wa_text: str = None, wa_prompt: str = None, email: bool = False, ref: str = None) -> str:
    """Queue a report. `buttons` [(id, title)] make the WhatsApp copy tappable; the email is unchanged.
    `wa_text` replaces the WhatsApp text (it must carry everything Raja is asked to approve, word for word);
    `wa_prompt` is the short line shown with the buttons if that text is too long for one message."""
    if kind not in REPORTABLE:
        raise ValueError(f"'{kind}' is not a reportable event")
    if needs_action or kind in {"question", "draft", "needs_you"}:
        subject = f"{subject} — updates needed"
    event = {"id": uuid.uuid4().hex[:8], "kind": kind, "subject": subject,
             "body": f"{body}\n\nNew York time: {ny_now()}",
             "email_sent": not email, "whatsapp": "pending", "attempts": 0,
             "wa_only": not email, "created": time.time()}   # email=False: WhatsApp only
    if ref:                                   # "question <id>" / "draft <id>": a WhatsApp Reply to it is matched to that item
        event["ref"] = ref
    if buttons:
        whatsapp.check_buttons(wa_prompt or "x", [tuple(b) for b in buttons])   # refuse impossible buttons now
        event.update(buttons=[list(b) for b in buttons], wa_text=wa_text, wa_prompt=wa_prompt)
    with locked("outbox"):
        items = read_json(OUTBOX, [])
        items.append(event)
        write_json(OUTBOX, items)
    return event["id"]


def process(send_email=gmail_client.send, send_whatsapp=whatsapp.send_text, send_buttons=whatsapp.send_buttons) -> dict:
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
                    # every message is remembered by its WhatsApp id: a question or draft by its id, any other by its text,
                    # so a Reply to it tells the reader exactly which message he means
                    shown = e.get("wa_text") or e["subject"] + "\n\n" + e["body"]
                    about = e.get("ref") or "message " + shown[:1200]
                    with whatsapp.sending_for(about):
                        delivered = _deliver_whatsapp(e, send_whatsapp, send_buttons)
                    if delivered:
                        e["whatsapp"] = "sent"
                        stats["whatsapp"] += 1
                        chatlog.add("agent", e["subject"], e["body"])
                    else:
                        stats["whatsapp_closed"] += 1
                        if e.get("wa_only"):
                            # WhatsApp-only and the window is closed: email it now so it is not missed, and keep it
                            # pending so it is sent on WhatsApp (buttons included) once he next messages the agent.
                            if not e.get("fallback_emailed"):
                                try:
                                    send_email(e["subject"], e["body"])
                                    e["fallback_emailed"] = True
                                    stats["emailed"] += 1
                                except Exception as ex:
                                    logging.error(f"Fallback email for event {e['id']} failed: {ex}")
                            if time.time() - e.get("created", 0) >= HOLD_SECONDS:
                                e["whatsapp"] = "window_closed"
                        else:
                            e["whatsapp"] = "window_closed"  # email carries it
                except Exception as ex:
                    e["whatsapp"] = "failed"
                    logging.error(f"WhatsApp for event {e['id']} failed: {ex}")
        # keep recent history, drop what is fully delivered and old
        write_json(OUTBOX, [e for e in items if not e["email_sent"] or e["whatsapp"] == "pending"] +
                   [e for e in items if e["email_sent"] and e["whatsapp"] != "pending"][-100:])
    return stats


def _deliver_whatsapp(e: dict, send_whatsapp, send_buttons) -> bool:
    """Send one event to WhatsApp. With buttons: one tappable message if it fits, else the full text and then a
    short message carrying the buttons. If the buttons can not be sent for any other reason, the plain text goes
    out instead, so the report always arrives (Raja can still type the command)."""
    text = e.get("wa_text") or f"{e['subject']}\n\n{e['body']}"
    buttons = [tuple(b) for b in e.get("buttons") or []]
    if not buttons:
        return send_whatsapp(text)
    try:
        if len(text) <= whatsapp.BUTTON_BODY_MAX:
            return send_buttons(text, buttons)
        if not send_whatsapp(text):
            return False                       # window closed: nothing was sent, email carries it
        return send_buttons(e.get("wa_prompt") or "Tap to answer:", buttons)
    except Exception as ex:
        logging.warning(f"Buttons not sent for event {e['id']} ({ex}); sending plain text")
        return send_whatsapp(text)


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
