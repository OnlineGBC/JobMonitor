"""WhatsApp webhook route (Meta Cloud API).

GET  /whatsapp/webhook  - Meta's one-time verification (verify token).
POST /whatsapp/webhook  - inbound messages; accepted only with a valid
                          X-Hub-Signature-256 signature, and only from Raja's number.

The web app is login-gated and CSRF-protected by default, so this endpoint is
exempted there by name (WEBHOOK_ENDPOINT). Its protection is the signature check.
"""
import hmac
import time

from flask import request

from . import vault, whatsapp
from .store import locked, read_json, write_json

WEBHOOK_ENDPOINT = "whatsapp_webhook"
INBOX = "whatsapp_inbox.json"


def whatsapp_webhook():
    if request.method == "GET":
        token = vault.get("wa_verify_token") or ""
        supplied = request.args.get("hub.verify_token", "")
        if request.args.get("hub.mode") == "subscribe" and token and hmac.compare_digest(token, supplied):
            return request.args.get("hub.challenge", ""), 200
        return "forbidden", 403

    body = request.get_data()
    if not whatsapp.valid_signature(body, request.headers.get("X-Hub-Signature-256", "")):
        return "forbidden", 403
    for _sender, text in whatsapp.parse_inbound(request.get_json(silent=True) or {}):
        whatsapp.note_inbound()  # any message from Raja reopens the 24h window
        with locked("whatsapp_inbox"):
            inbox = read_json(INBOX, [])
            inbox.append({"ts": time.time(), "text": text, "handled": False})
            write_json(INBOX, inbox[-200:])
    return "ok", 200


def register(app) -> None:
    app.add_url_rule("/whatsapp/webhook", endpoint=WEBHOOK_ENDPOINT,
                     view_func=whatsapp_webhook, methods=["GET", "POST"])
