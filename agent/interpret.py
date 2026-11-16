"""Understand a free-text reply ("apply acme", "skip", "no", "20 years") without guessing.

Pure rules, no model: the reply is matched against what is actually waiting for Raja (open Easy Apply decisions,
open application questions, pending confirmations). The result is ALWAYS echoed back for confirmation by the
caller, and anything ambiguous is turned into a question instead of an action.

interpret(text) returns one of
    {"kind": "resolve", "id": <confirmation id>, "accept": bool}      "yes" / "no" to a pending confirmation
    {"kind": "propose", "commands": [...], "description": str}        what to confirm
    {"kind": "clarify", "message": str}                                  what to ask instead
    None                                                                 nothing is waiting, so ignore it
"""
import re

from . import confirmations, decisions, questions

YES = {"yes", "y", "yep", "yeah", "sure", "apply", "go", "submit", "ok", "okay", "confirm", "confirmed", "proceed"}
NO = {"no", "n", "nope", "nah", "skip", "pass", "ignore", "cancel", "stop", "dont", "don't"}
FILLER = {"it", "that", "please", "ahead", "do", "the", "this", "one", "to", "for", "role", "job", "and", "thanks", "thank", "you"}
GENERIC = {"chief", "officer", "director", "senior", "head", "vice", "president", "manager", "of", "the", "and", "for"}


def _words(text: str) -> list:
    return re.findall(r"[a-z0-9']+", (text or "").lower())


def _tokens(*parts) -> set:
    return {w for p in parts for w in _words(p) if len(w) >= 3 and w not in GENERIC}


def _open_items() -> list:
    items = [("decision", d) for d in decisions.all_decisions() if d["status"] == "OPEN"]
    items += [("question", q) for q in questions.open_questions()]
    return items


def _label(kind: str, rec: dict) -> str:
    if kind == "decision":
        return f"{rec['title']} at {rec['company']} (apply or skip)"
    return f"{rec['title']} at {rec['company']}: {rec['question'][:80]}"


def _list(items: list) -> str:
    return "\n".join(f"{i}. {_label(k, r)}" for i, (k, r) in enumerate(items, 1))


def _target(words: set, items: list):
    """The one open item the reply is about, or None. Matches on company name, then on title words."""
    for pick in (lambda r: _tokens(r["company"]), lambda r: _tokens(r["title"])):
        hits = [it for it in items if pick(it[1]) & words]
        if len(hits) == 1:
            return hits[0]
    return items[0] if len(items) == 1 else None


def interpret(text: str):
    text = (text or "").strip()
    words = _words(text)
    if not words:
        return None
    wset = set(words)
    meaningful = [w for w in words if w not in FILLER]

    # 1. An answer to a pending confirmation ("yes" / "no")
    pending = confirmations.open_ones()
    if pending and meaningful and all(w in YES | NO for w in meaningful) and not (set(meaningful) & YES and set(meaningful) & NO):
        accept = bool(set(meaningful) & YES)
        if len(pending) == 1:
            return {"kind": "resolve", "id": pending[0]["id"], "accept": accept}
        return {"kind": "clarify", "message": "More than one confirmation is waiting. Tap Confirm or Cancel on the one you mean."}

    items = _open_items()
    if not items:
        return None
    yes, no = bool(wset & YES), bool(wset & NO)

    # 2. "apply all" / "skip all": every open Easy Apply decision, listed back for confirmation
    if "all" in wset and (yes or no):
        decs = [r for k, r in items if k == "decision"]
        if decs and not (yes and no):
            verb = "APPLY" if yes else "SKIP"
            return {"kind": "propose", "commands": [f"DECIDE {d['id']} {verb}" for d in decs],
                    "description": f"{verb.title()} all {len(decs)} open roles:\n"
                                   + "\n".join(f"- {d['title']} at {d['company']}" for d in decs)}

    # 3. One specific item
    target = _target(wset, items)
    if target is None:
        return {"kind": "clarify", "message": "Which one do you mean? Reply with the company name, for example "
                                               "\"apply acme\".\n" + _list(items)}
    kind, rec = target
    who = f"{rec['title']} at {rec['company']}"

    if kind == "decision":
        if yes == no:
            return {"kind": "clarify", "message": f"Apply or skip {who}? Reply \"apply\" or \"skip\"."}
        verb = "APPLY" if yes else "SKIP"
        return {"kind": "propose", "commands": [f"DECIDE {rec['id']} {verb}"],
                "description": f"{verb.title()}: {who}"}

    q = rec["question"]
    if "skip" in wset:
        return {"kind": "propose", "commands": [f"ANSWER {rec['id']} SKIP"],
                "description": f"Skip this application ({who}). Nothing will be submitted."}
    opts = rec.get("options") or []
    if questions.yes_no(opts):
        if yes == no:
            return {"kind": "clarify", "message": f"Yes or no? {q}\n({who})"}
        value = "YES" if yes else "NO"
        return {"kind": "propose", "commands": [f"ANSWER {rec['id']} {value}"],
                "description": f"Answer {value} to: {q}\n({who}), then finish the application."}
    if opts:
        chosen = [o for o in opts if o.lower() in text.lower()]
        if len(chosen) != 1:
            return {"kind": "clarify", "message": f"Pick one of: {', '.join(opts)}\n{q}\n({who})"}
        return {"kind": "propose", "commands": [f"ANSWER {rec['id']} {chosen[0]}"],
                "description": f"Answer '{chosen[0]}' to: {q}\n({who}), then finish the application."}
    value = re.sub(r"\s+", " ", text).strip()
    return {"kind": "propose", "commands": [f"ANSWER {rec['id']} {value}"],
            "description": f"Answer '{value}' to: {q}\n({who}), then finish the application."}
