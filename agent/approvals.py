"""Raja's replies to the agent: APPROVE / REJECT / PENDING, from WhatsApp and Gmail.

    APPROVE <id>                 send the draft exactly as shown
    APPROVE <id> <your text>     send YOUR text instead (it must still follow the voice rules)
    REJECT <id>                  never send it
    PENDING                      list everything waiting on you

Only these commands do anything. Message text is data: a reply that is not a command, or comes from
anyone but Raja's own number or addresses, is ignored.
"""
import logging
import re
from datetime import datetime

from . import chatlog, config, gmail_client, confirmations, decisions, feedback, interpret, manual, outreach, queue_tool, questions, reporter, router, tracker
from .store import locked, read_json, write_json

SEEN = "approvals_seen.json"
INBOX = "whatsapp_inbox.json"

_CMD = re.compile(r"^\s*(approve|reject|answer|decide|confirm|cancel|rule)\s+([0-9a-f]{8})\b[ \t:,.-]*(.*)$", re.I | re.S)
_PENDING = re.compile(r"^\s*pending\s*[.!]?\s*$", re.I)
_APPLIED = re.compile(r"^\s*applied\s+(\d{6,})\s*[.!]?\s*$", re.I)
_RULES = re.compile(r"^\s*rules\s*[.!]?\s*$", re.I)
_QUOTE_START = re.compile(r"^(>|On .{5,120} wrote:\s*$|-{2,}\s*Original Message|From: .+@|Sent from my\b|--\s*$)", re.I)


def strip_quoted(body: str) -> str:
    """The new part of a reply: stop at quoted text, 'On ... wrote:', or a mobile/email signature."""
    keep = []
    for line in (body or "").replace("\r\n", "\n").split("\n"):
        if _QUOTE_START.match(line.strip()):
            break
        keep.append(line)
    return "\n".join(keep).strip()


def parse(body: str):
    """Return {'action', 'id', 'text'} for a command, or None. Looks only at the new part of the reply."""
    text = strip_quoted(body)
    m = _CMD.match(text)
    if m:
        return {"action": m.group(1).upper(), "id": m.group(2).lower(), "text": m.group(3).strip() or None}
    if _PENDING.match(text.split("\n")[0] if text else ""):
        return {"action": "PENDING", "id": None, "text": None}
    if _RULES.match(text.split("\n")[0] if text else ""):
        return {"action": "RULES", "id": None, "text": None}
    m = _APPLIED.match(text.split("\n")[0] if text else "")
    if m:
        return {"action": "APPLIED", "id": m.group(1), "text": None}
    return None


def pending_digest() -> str:
    lines = []
    for i in outreach.all_items():
        if i["status"] in ("DRAFT", "APPROVED"):
            lines.append(f"Outreach {i['id']} ({i['status']}, {i['kind']} to {i['poster']} at {i['company']}):\n  {i['text']}")
    for r in tracker.rows():
        if r["status"] == "AWAITING RAJA":
            waiting = int((datetime.now(reporter.NY) - datetime.fromisoformat(r["booked"])).total_seconds() // 60)
            age = f"{waiting // 60}h {waiting % 60}m" if waiting >= 60 else f"{waiting}m"
            lines.append(f"Application #{r['number']} awaiting you ({age}): {r['title']} at {r['company']}\n  {r['url']}")
    queued = [q for q in queue_tool.all_items() if q["status"] == "QUEUED"]
    if queued:
        lines.append(f"{len(queued)} posting(s) queued for the applier.")
    return "\n\n".join(lines) or "Nothing is waiting on you."


def apply(cmd: dict) -> str:
    if cmd["action"] == "APPROVE":
        return outreach.approve(cmd["id"], cmd["text"])[1]
    if cmd["action"] == "REJECT":
        return outreach.reject(cmd["id"])[1]
    if cmd["action"] in ("CONFIRM", "CANCEL"):
        return confirmations.resolve(cmd["id"], cmd["action"] == "CONFIRM", lambda c: apply(parse(c)))
    if cmd["action"] == "DECIDE":
        return decisions.decide(cmd["id"], cmd["text"] or "")[1]
    if cmd["action"] == "ANSWER":
        return questions.answer(cmd["id"], cmd["text"] or "")[1]
    if cmd["action"] == "APPLIED":
        return manual.mark_applied(cmd["id"])[1]
    if cmd["action"] == "RULES":
        return feedback.digest()
    if cmd["action"] == "RULE":
        return feedback.remove(cmd["id"]) if (cmd["text"] or "").upper() == "REMOVE" else feedback.activate(cmd["id"])
    return pending_digest()


def _answer(cmd: dict, source: str, said: str = None) -> None:
    message = apply(cmd)
    logging.info(f"{source}: {cmd['action']} {cmd.get('id') or ''} -> {message.splitlines()[0]}")
    if said:                                   # a typed reply was interpreted: show Raja exactly what he said and what was done
        message = f"You said: \"{said.strip()[:200]}\"\n\n{message}"
    # An acknowledgement goes back on the channel the reply came from: a WhatsApp reply gets a WhatsApp
    # acknowledgement only (no duplicate email), an emailed reply gets email and WhatsApp as before.
    reporter.enqueue("notice", f"{cmd['action'].title()}: {cmd.get('id') or 'pending list'}", message,
                     email=(source != "whatsapp"))


_CASUAL = {"ok", "okay", "thanks", "thank", "you", "great", "cool", "hi", "hello", "hey", "got", "it", "nice", "good",
           "noted", "again", "please"}


def handle_reply(text: str, via: str, source: str, about: str = None) -> bool:
    """One reply from Raja. What he types or taps IS the confirmation: nothing is echoed back for approval first.

    A button or an exact command acts at once. Any other typed text is read by the model (agent.router), checked in
    code, acted on, and acknowledged with what he said and what was done. If the model cannot be reached the plain
    rules in agent.interpret are used instead. True if anything was done."""
    cmd = parse(text)
    if cmd:
        _answer(cmd, source)
        return True
    words = interpret._words(text)
    if not words or (set(words) <= _CASUAL and not about):
        return False
    if confirmations.open_ones():              # a confirmation sent before this change: a plain yes / no still settles it
        old = interpret.interpret(text)
        if old and old["kind"] == "resolve":
            _answer({"action": "CONFIRM" if old["accept"] else "CANCEL", "id": old["id"], "text": None}, source)
            return True
    try:
        result = router.route(text, about=about)
    except Exception as e:
        logging.warning(f"Could not read the reply with the model ({e}); using the plain rules")
        if about and about.startswith("question "):       # he replied to that question: his text is the answer
            _answer({"action": "ANSWER", "id": about.split()[1], "text": text.strip()}, source, said=text)
            return True
        return _by_rules(text, source)
    for c in result["commands"]:
        _answer(parse(c), source, said=text)
    if result["ask"]:
        reporter.enqueue("notice", "Which one?", result["ask"], email=(source == "gmail"))
    if result.get("reply"):                    # he asked the agent a question: answer it, never leave him without a reply
        reporter.enqueue("notice", "Reply", result["reply"], email=(source == "gmail"))
    if result["comment"]:
        feedback.handle(text, source)
    if not (result["commands"] or result["ask"] or result["comment"] or result.get("reply")):
        # He wrote something real and nothing matched: say so rather than stay silent.
        reporter.enqueue("notice", "Not understood", f"You said: \"{text.strip()[:200]}\"\n\nI could not tell what to do "
                         "with that, so I did nothing. Reply with a company name and apply or skip, answer an open "
                         "question, state a preference (for example \"no part-time roles\"), or send RULES or PENDING.",
                         email=(source == "gmail"))
    return True


def _by_rules(text: str, source: str) -> bool:
    """Fallback when the model is unavailable: the keyword rules. Still no extra confirmation step."""
    if feedback.is_comment(text) and not _text_question_open():
        return feedback.handle(text, source)
    result = interpret.interpret(text)
    if result is None or (result["kind"] == "clarify" and feedback.worth_keeping(text)):
        return feedback.handle(text, source) if feedback.worth_keeping(text) else False
    if result["kind"] == "propose":
        for c in result["commands"]:
            _answer(parse(c), source, said=text)
    elif result["kind"] == "resolve":
        _answer({"action": "CONFIRM" if result["accept"] else "CANCEL", "id": result["id"], "text": None}, source)
    else:
        reporter.enqueue("notice", "Which one?", result["message"], email=(source == "gmail"))
    return True


def _text_question_open() -> bool:
    """A free-text application question is waiting: a typed sentence is probably its answer, not a comment."""
    return any(q.get("type") == "text" for q in questions.open_questions())


def whatsapp_ref(message_id):
    from . import whatsapp
    return whatsapp.ref_for(message_id)


def process_whatsapp() -> int:
    """Handle unread WhatsApp replies. The webhook has already checked the signature and Raja's number."""
    with locked("whatsapp_inbox"):
        inbox = read_json(INBOX, [])
        todo = [m for m in inbox if not m.get("handled")]
        for m in todo:
            m["handled"] = True
        write_json(INBOX, inbox)
    n = 0
    for m in todo:
        if m["text"]:
            about = whatsapp_ref(m.get("reply_to")) if m.get("via", "text") == "text" else None
            if handle_reply(m["text"], m.get("via", "text"), "whatsapp", about=about):
                n += 1
            chatlog.add("raja", "", m["text"])
    return n


def _sender_ok(from_header: str) -> bool:
    found = re.findall(r"[\w.+-]+@[\w.-]+", from_header or "")
    return bool(found) and found[-1].lower() in config.APPROVER_EMAILS


def process_gmail(search=gmail_client.search, get_text=gmail_client.get_text, limit: int = 20) -> int:
    """Handle unread email replies from Raja's own addresses (each message is looked at once)."""
    senders = " OR ".join(f"from:{a}" for a in sorted(config.APPROVER_EMAILS))
    seen = set(read_json(SEEN, []))
    n = 0
    for msg in search(f"in:inbox newer_than:3d ({senders})", max_results=limit):
        if msg["id"] in seen:
            continue
        seen.add(msg["id"])
        if not _sender_ok(msg.get("from", "")):
            continue
        if handle_reply(get_text(msg["id"]), "text", "gmail"):
            n += 1
    with locked("approvals_seen"):
        write_json(SEEN, sorted(seen)[-500:])
    return n
