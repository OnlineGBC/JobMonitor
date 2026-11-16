"""One-time Gmail setup.

    python -m agent.setup_gmail --credentials "C:\\path\\to\\credentials.json"
        Stores the OAuth client in the vault, opens a browser for you to approve access,
        and stores the resulting token in the vault. Delete credentials.json afterwards.
    python -m agent.setup_gmail --test
        Sends a test email to the report address.
"""
import argparse
import json
import sys

from . import gmail_client, vault


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--credentials", help="path to the OAuth client JSON downloaded from Google Cloud")
    ap.add_argument("--to", help=f"report address (default {gmail_client.DEFAULT_TO})")
    ap.add_argument("--test", action="store_true")
    a = ap.parse_args()

    if a.test:
        mid = gmail_client.send("Job agent test email", "If you can read this, the Gmail channel works.")
        print("sent, message id", mid)
        return
    if not a.credentials:
        sys.exit(__doc__)

    from google_auth_oauthlib.flow import InstalledAppFlow
    with open(a.credentials, encoding="utf-8") as f:
        client = json.load(f)
    if "installed" not in client:
        sys.exit("This is not a Desktop-app OAuth client. Create one of type 'Desktop app'.")
    vault.put("gmail_client", json.dumps(client))
    flow = InstalledAppFlow.from_client_config(client, gmail_client.SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent")
    vault.put("gmail_token", creds.to_json())
    vault.put("gmail_to", a.to or gmail_client.DEFAULT_TO)
    print("Saved to the Windows credential vault. You can delete the credentials.json file now.")
    print("Reports go to:", vault.get("gmail_to"))


if __name__ == "__main__":
    main()
