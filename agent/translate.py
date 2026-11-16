"""Translate LinkedIn UI strings to English once, and remember the result.

LinkedIn here shows a mix of languages ("Kontaktinfo", "Ja"/"Nein", "Lebenslauf"). The first time a
string is seen, Haiku translates it; the answer is saved in data/agent/translations.json and reused
forever, so each string costs one API call at most.
"""
import json

from . import config, llm
from .store import locked, read_json, write_json

FILE = "translations.json"

PROMPT = """Translate each of these job-application website UI strings to English.
If a string is already English, return it unchanged. Keep it short and literal (a button label stays a
button label). Reply with ONLY a JSON object mapping each input string to its English translation.

Strings:
{strings}"""


def translate(strings, client=None, model: str = None) -> dict:
    """Return {original: english} for every non-empty string. Unseen strings are translated and cached.

    If the model cannot be reached, unseen strings map to themselves and are NOT cached.
    """
    wanted = list(dict.fromkeys(s.strip() for s in strings if s and s.strip()))
    cache = read_json(FILE, {})
    missing = [s for s in wanted if s not in cache]
    if missing:
        try:
            if client is None:
                import anthropic
                client = anthropic.Anthropic()
            resp = client.messages.create(
                model=model or config.TRANSLATE_MODEL, max_tokens=1500,
                messages=[{"role": "user", "content": PROMPT.format(strings=json.dumps(missing, ensure_ascii=False))}])
            data = llm.json_object(resp)
            fresh = {s: str(data[s]).strip() for s in missing if isinstance(data.get(s), str) and data[s].strip()}
            if fresh:
                with locked("translations"):
                    cache = read_json(FILE, {})
                    cache.update(fresh)
                    write_json(FILE, cache)
        except Exception:
            pass  # fall through: unseen strings stay untranslated
    return {s: cache.get(s, s) for s in wanted}


def english(text: str, **kw) -> str:
    return translate([text], **kw).get(text.strip(), text) if text and text.strip() else ""


def button_role(english_text: str) -> str:
    """Classify a button from its ENGLISH text. Anything unrecognised is 'other' and is never clicked."""
    t = english_text.strip().lower()
    if "submit" in t:
        return "submit"
    if t.startswith("review"):
        return "review"
    if t in ("next", "continue", "next step", "continue to next step"):
        return "next"
    if t == "back":
        return "back"
    if t in ("save", "save draft", "save application"):
        return "save"
    if t in ("discard", "delete"):
        return "discard"
    if "upload" in t:
        return "upload"
    return "other"
