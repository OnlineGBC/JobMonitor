"""One-time setup: store the WhatsApp credentials in the encrypted vault.

    python -m agent.setup_whatsapp          # prompts (secrets are hidden as you type)
    python -m agent.setup_whatsapp --test   # sends a test text if the 24h window is open
"""
import getpass
import secrets
import sys

from . import vault, whatsapp

FIELDS = [
    ("wa_phone_number_id", "Phone Number ID", False),
    ("wa_waba_id", "Messaging account (WhatsApp Business Account) ID", False),
    ("wa_recipient", "Your WhatsApp number, digits only with country code (e.g. 12125551234)", False),
    ("wa_access_token", "Permanent System User access token", True),
    ("wa_app_secret", "App secret (App settings > Basic > App secret)", True),
]


def main():
    if "--test" in sys.argv:
        print("sent" if whatsapp.send_text("Job agent test message.") else
              "window closed: send any message to the test number from your phone first")
        return
    for name, label, secret in FIELDS:
        have = vault.get(name)
        prompt = f"{label}{' [keep existing]' if have else ''}: "
        val = (getpass.getpass(prompt) if secret else input(prompt)).strip()
        if val:
            vault.put(name, val.lstrip("+") if name == "wa_recipient" else val)
        elif not have:
            sys.exit(f"{name} is required")
    if not vault.get("wa_verify_token"):
        vault.put("wa_verify_token", secrets.token_urlsafe(24))
    print("Saved to the Windows credential vault.")
    print("Webhook verify token (paste into Meta's webhook settings):", vault.get("wa_verify_token"))


if __name__ == "__main__":
    main()
