"""Agent settings. Override any model with an environment variable."""
import os

# The web app loads .env for its subprocesses; do the same when a module is run directly,
# so ANTHROPIC_API_KEY is found either way. Existing environment variables win.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:  # python-dotenv is in requirements.txt; tolerate its absence
    pass

CORE_CHECK_MODEL = os.environ.get("AGENT_CORE_CHECK_MODEL", "claude-sonnet-5-5")
FORM_ANSWER_MODEL = os.environ.get("AGENT_FORM_ANSWER_MODEL", "claude-sonnet-5-5")
FEEDBACK_MODEL = os.environ.get("AGENT_FEEDBACK_MODEL", "claude-sonnet-5-5")
# UI strings are short and translated once, then cached, so the small model is enough.
TRANSLATE_MODEL = os.environ.get("AGENT_TRANSLATE_MODEL", "claude-haiku-5-5")

# A claimed posting is treated as abandoned after this long.
CLAIM_STALE_MINUTES = 70

# Replies from these addresses may approve outreach. Anything else is ignored.
APPROVER_EMAILS = {"raja@onlinegbc.com", "rgopalan@onlinegbc.com"}
