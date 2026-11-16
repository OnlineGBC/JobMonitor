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
    """The first JSON object in the response's text. Raises ValueError with the real reason on failure."""
    if getattr(response, "stop_reason", None) == "max_tokens":
        raise ValueError("the reply was cut off (max_tokens reached); raise the limit")
    text = text_of(response)
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError(f"the reply contained no JSON object: {text[:120]!r}")
    return json.loads(m.group(0))
