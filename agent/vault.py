"""Secrets live in the OS credential vault (Windows Credential Manager, DPAPI-encrypted).

Never in the repo, .env, or data/agent.
"""
import keyring

SERVICE = "jobmonitor-agent"


def get(name: str):
    return keyring.get_password(SERVICE, name)


def put(name: str, value: str) -> None:
    keyring.set_password(SERVICE, name, value)


def require(name: str) -> str:
    v = get(name)
    if not v:
        raise RuntimeError(f"'{name}' is not in the vault. Run: python -m agent.setup_whatsapp")
    return v
