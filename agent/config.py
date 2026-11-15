"""Agent settings. Override any model with an environment variable."""
import os

CORE_CHECK_MODEL = os.environ.get("AGENT_CORE_CHECK_MODEL", "claude-sonnet-5-5")
FORM_ANSWER_MODEL = os.environ.get("AGENT_FORM_ANSWER_MODEL", "claude-sonnet-5-5")

# A claimed posting is treated as abandoned after this long.
CLAIM_STALE_MINUTES = 70
