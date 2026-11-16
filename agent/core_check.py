"""Core check: does the role exist to build/lead/own AI, GenAI, ML, LLM, data science or analytics?

Runs only when the title carries none of the core words; such a title passes unjudged.
"""
import re

from . import config, llm

CORE_TITLE = re.compile(
    r"\b(ai|artificial intelligence|genai|generative ai|machine learning|ml|llm|"
    r"data science|data analytics|analytics)\b", re.I)

PROMPT = """You screen job postings for an AI-leadership job search.

Decide whether this role EXISTS to build, lead, or own products, platforms, models, or
programs in AI, GenAI, machine learning, LLMs, data science, or data analytics.
Data science or analytics leadership counts as core even if it never says "AI".

Answer false if the field is only a tool the role uses, a passing mention, or company
background, or if the description mentions none of these fields.

Title: {title}
Company: {company}

Description:
{description}

Reply with JSON only: {{"core": true or false, "reason": "<one sentence>"}}"""


class CoreCheckError(Exception):
    """The judgment could not be obtained. The caller must NOT ledger or queue the posting."""


def title_passes_unjudged(title: str) -> bool:
    return bool(CORE_TITLE.search(title or ""))


def judge(posting: dict, client=None, model: str = None):
    """Return (is_core, reason). Raises CoreCheckError on any API or parse failure."""
    if title_passes_unjudged(posting.get("title", "")):
        return True, "title carries a core word"
    desc = (posting.get("description") or "").strip()
    if not desc:
        raise CoreCheckError("no description to judge")
    try:
        if client is None:
            import anthropic
            client = anthropic.Anthropic()
        resp = client.messages.create(
            model=model or config.CORE_CHECK_MODEL,
            max_tokens=2000,
            messages=[{"role": "user", "content": PROMPT.format(
                title=posting.get("title", ""), company=posting.get("company", ""),
                description=desc[:12000])}],
        )
        data = llm.json_object(resp)
        return bool(data["core"]), str(data.get("reason", ""))
    except Exception as e:
        raise CoreCheckError(str(e)) from e
