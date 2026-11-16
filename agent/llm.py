"""Small helpers for reading model responses."""
import json
import re


def text_of(response) -> str:
    """All text from a response. Skips non-text blocks (e.g. thinking) that may come first."""
    parts = [b.text for b in response.content if getattr(b, "type", "text") == "text" and hasattr(b, "text")]
    if not parts:
        raise ValueError("the response contained no text")
    return "".join(parts)


def json_object(response) -> dict:
    """The first JSON object in the response's text."""
    return json.loads(re.search(r"\{.*\}", text_of(response), re.S).group(0))
