"""WhatsApp via the Meta Cloud API (no Twilio).

Free-form text is deliverable only within 24 hours of Raja's last inbound message.
Outside that window only approved templates can be sent, so send_text() refuses
and returns False; the caller falls back to email.
"""
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


def _post(payload: dict, session=requests):
    r = session.post(
        f"{API}/{vault.require('wa_phone_number_id')}/messages",
        headers={"Authorization": f"Bearer {vault.require('wa_access_token')}"},
        json={"messaging_product": "whatsapp", "to": vault.require("wa_recipient"), **payload},
        timeout=20,
    )
    if r.status_code >= 300:
        raise RuntimeError(f"WhatsApp send failed {r.status_code}: {r.text[:300]}")
    return r.json()


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


def send_keepalive(session=requests, now: float = None) -> bool:
    """Send the approved check-in template, at most once per 12 hours."""
    with locked("whatsapp_state"):
        s = read_json(STATE, {})
        last = s.get("last_keepalive")
        if last is not None and (now or time.time()) - last < 12 * 3600:
            return False
        _post({"type": "template", "template": {
            "name": KEEPALIVE_TEMPLATE, "language": {"code": "en_US"}}}, session)
        s["last_keepalive"] = now or time.time()
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
    mine = (vault.get("wa_recipient") or "").lstrip("+")
    out = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            for m in change.get("value", {}).get("messages", []):
                if m.get("from") == mine:
                    out.append((m["from"], (m.get("text", {}) or {}).get("body", "").strip()))
    return out
