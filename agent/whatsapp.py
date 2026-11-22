"""WhatsApp via the Meta Cloud API (no Twilio).

Free-form text is deliverable only within 24 hours of Raja's last inbound message.
Outside that window only approved templates can be sent, so send_text() refuses
and returns False; the caller falls back to email.
"""
import contextlib
import hashlib
import hmac
import time

import requests

from . import vault
from .store import locked, read_json, write_json

API = "https://graph.facebook.com/v25.0"
WINDOW_SECONDS = 24 * 3600
STATE = "whatsapp_state.json"
KEEPALIVE_TEMPLATE = "job_agent_checkin"


REFS = "whatsapp_refs.json"      # {WhatsApp message id: what it was about, e.g. "question 3f9a21"}
MAX_REFS = 300
_sending_for = None


@contextlib.contextmanager
def sending_for(ref):
    """While active, every message sent is remembered as being about `ref`, so that a WhatsApp Reply to it can be
    matched to that question or draft."""
    global _sending_for
    before, _sending_for = _sending_for, ref
    try:
        yield
    finally:
        _sending_for = before


def remember_sent(message_id: str, ref: str) -> None:
    with locked("whatsapp_refs"):
        refs = read_json(REFS, {})
        refs[message_id] = ref
        write_json(REFS, dict(list(refs.items())[-MAX_REFS:]))


def ref_for(message_id):
    """What the message with this id was about ('question <id>' or 'draft <id>'), or None."""
    return read_json(REFS, {}).get(message_id) if message_id else None


def _post(payload: dict, session=requests):
    r = session.post(
        f"{API}/{vault.require('wa_phone_number_id')}/messages",
        headers={"Authorization": f"Bearer {vault.require('wa_access_token')}"},
        json={"messaging_product": "whatsapp", "to": vault.require("wa_recipient"), **payload},
        timeout=20,
    )
    if r.status_code >= 300:
        raise RuntimeError(f"WhatsApp send failed {r.status_code}: {r.text[:300]}")
    data = r.json()
    if _sending_for:
        try:
            for m in data.get("messages") or []:
                if m.get("id"):
                    remember_sent(m["id"], _sending_for)
        except Exception:                     # remembering must never stop a message from being sent
            pass
    return data


def window_open(now: float = None) -> bool:
    last = read_json(STATE, {}).get("last_inbound")
    return last is not None and (now or time.time()) - last < WINDOW_SECONDS


def note_inbound(now: float = None) -> None:
    with locked("whatsapp_state"):
        s = read_json(STATE, {})
        s["last_inbound"] = now or time.time()
        write_json(STATE, s)


def send_text(text: str, session=requests, now: float = None) -> bool:
    """Send free-form text. Returns False (and sends nothing) when the 24h window is closed."""
    if not window_open(now):
        return False
    _post({"type": "text", "text": {"body": text[:4000]}}, session)
    return True


MAX_BUTTONS, BUTTON_TITLE_MAX, BUTTON_ID_MAX, BUTTON_BODY_MAX = 3, 20, 256, 1024


def check_buttons(body: str, buttons: list) -> None:
    """WhatsApp's limits for reply buttons: 1-3 buttons, title <= 20, id <= 256, body <= 1024 characters."""
    if not 1 <= len(buttons) <= MAX_BUTTONS:
        raise ValueError(f"a message can have 1 to {MAX_BUTTONS} buttons, not {len(buttons)}")
    if not body or len(body) > BUTTON_BODY_MAX:
        raise ValueError(f"the text of a message with buttons must be 1 to {BUTTON_BODY_MAX} characters, not {len(body or '')}")
    for button_id, title in buttons:
        if not title or len(title) > BUTTON_TITLE_MAX:
            raise ValueError(f"button title '{title}' must be 1 to {BUTTON_TITLE_MAX} characters")
        if not button_id or len(button_id) > BUTTON_ID_MAX:
            raise ValueError("a button id must be 1 to 256 characters")
    if len({b[0] for b in buttons}) != len(buttons):
        raise ValueError("button ids must be different")


def send_buttons(body: str, buttons: list, session=requests, now: float = None) -> bool:
    """Send a message with tappable reply buttons: buttons = [(id, title), ...].

    Tapping one sends its id back through the webhook, where parse_inbound() turns it into the same text
    command as typing it (e.g. id 'APPROVE 9e6ae9d2'). Like any free-form message it needs the 24-hour window
    to be open; if it is closed this sends nothing and returns False.
    """
    check_buttons(body, buttons)
    if not window_open(now):
        return False
    _post({"type": "interactive", "interactive": {
        "type": "button", "body": {"text": body},
        "action": {"buttons": [{"type": "reply", "reply": {"id": i, "title": t}} for i, t in buttons]}}}, session)
    return True


KEEPALIVE_LEAD_SECONDS = 3 * 3600     # send it when the 24-hour window has this little time left (or is closed)
KEEPALIVE_RETRY_SECONDS = 6 * 3600   # wait this long after a FAILED keepalive (e.g. template still in review)


def send_keepalive(session=requests, now: float = None) -> bool:
    """Send the approved check-in template, at most once per 12 hours, and only when the 24-hour window has less
    than 3 hours left or is already closed (while he is chatting, it is not needed)."""
    with locked("whatsapp_state"):
        s = read_json(STATE, {})
        t = now or time.time()
        inbound = s.get("last_inbound")
        if inbound is not None and WINDOW_SECONDS - (t - inbound) > KEEPALIVE_LEAD_SECONDS:
            return False
        last = s.get("last_keepalive")
        if last is not None and t - last < 12 * 3600:
            return False
        tried = s.get("last_keepalive_attempt")
        if tried is not None and t - tried < KEEPALIVE_RETRY_SECONDS:
            return False                     # a failed try waits 6 hours, not one 20-second scheduler pass
        s["last_keepalive_attempt"] = t
        write_json(STATE, s)                 # recorded BEFORE sending, so a failure is also remembered
        _post({"type": "template", "template": {
            "name": KEEPALIVE_TEMPLATE, "language": {"code": "en_US"}}}, session)
        s = read_json(STATE, {})
        s["last_keepalive"] = t
        write_json(STATE, s)
    return True


def valid_signature(body: bytes, header: str) -> bool:
    """Check Meta's X-Hub-Signature-256 header against the app secret."""
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(vault.require("wa_app_secret").encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header[7:])


def parse_inbound(payload: dict) -> list:
    """Return [(sender_number, text)] from a webhook payload; only Raja's number is accepted."""
    return [(sender, text) for sender, text, _via, _to in parse_inbound_events(payload)]


def parse_inbound_events(payload: dict) -> list:
    """Like parse_inbound, but [(sender_number, text, via, reply_to)] where via is 'button' for a tapped button or
    list row and 'text' for a typed message, and reply_to is the id of the message he used WhatsApp's Reply on
    (None if he just typed)."""
    mine = (vault.get("wa_recipient") or "").lstrip("+")
    out = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            for m in change.get("value", {}).get("messages", []):
                if m.get("from") != mine:
                    continue
                if m.get("type") == "interactive":   # a tapped button: its id IS the command we attached to it
                    tap = (m.get("interactive") or {})
                    reply = tap.get("button_reply") or tap.get("list_reply") or {}
                    out.append((m["from"], str(reply.get("id", "")).strip(), "button", None))
                else:
                    out.append((m["from"], (m.get("text", {}) or {}).get("body", "").strip(), "text",
                                (m.get("context") or {}).get("id")))
    return out
