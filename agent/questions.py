"""Questions the agent stopped on, and Raja's answers to them.

When an application stops on a question his standing answers do not cover, the question is recorded here and
he is offered a way to answer it: tappable Yes / No / Skip buttons on WhatsApp, a Questions page in the web
UI, an `ANSWER <id> ...` reply by email or WhatsApp, or the command line:

    python -m agent.questions list
    python -m agent.questions answer <id> <value>      (value: YES, NO, a choice, a number or text; SKIP skips)
    python -m agent.questions skip <id>

An answer is saved as a standing answer for EXACTLY that question (so the same question is never asked again),
and the paused application is then finished by the applier on its next pass: it reopens the saved draft, fills
the answer in, and submits it (or stops at the review page if automatic submission is off). SKIP settles the
application as DO NOT ATTEMPT and leaves the draft on LinkedIn untouched.
"""
import re
import sys
import uuid
from datetime import datetime

from . import answers, queue_tool, reporter, tracker
from .store import locked, read_json, write_json

FILE = "pending_questions.json"
MAX_ANSWER_LENGTH = 300
DO_NOT_ATTEMPT = "DO NOT ATTEMPT"


def _now() -> str:
    return datetime.now(reporter.NY).isoformat(timespec="seconds")


def _key(text: str) -> str:
    return re.sub(r"\W+", " ", (text or "").lower()).strip()


def yes_no(options: list) -> bool:
    return {"yes", "no"} <= {o.strip().lower() for o in options or []}


def all_questions() -> list:
    return read_json(FILE, [])


def get(qid: str):
    return next((q for q in all_questions() if q["id"] == (qid or "").strip().lower()), None)


def open_questions() -> list:
    return [q for q in all_questions() if q["status"] == "OPEN"]


def answered_waiting() -> list:
    """Answered questions whose application has not been finished yet."""
    return [q for q in all_questions() if q["status"] == "ANSWERED" and not q.get("done")]


def _update(qid: str, **fields) -> None:
    with locked("questions"):
        items = read_json(FILE, [])
        for q in items:
            if q["id"] == qid:
                q.update(fields)
        write_json(FILE, items)


def mark_done(qid: str) -> None:
    _update(qid, done=True)


# ---- asking ---------------------------------------------------------------

def ask(row: dict, result: dict, link: str):
    """Record the question a paused application stopped on. Returns (record, created).

    One OPEN record per (job, question): if it is already open it is returned, not asked again.
    """
    question = (result.get("question") or "").strip()
    if not question:
        return None, False
    options = [str(o) for o in result.get("options") or []]
    with locked("questions"):
        items = read_json(FILE, [])
        same = next((q for q in items if q["job_id"] == str(row["job_id"]) and _key(q["question"]) == _key(question)
                     and q["status"] == "OPEN"), None)
        if same:
            return same, False
        rec = {"id": uuid.uuid4().hex[:8], "number": row["number"], "job_id": str(row["job_id"]),
               "company": row["company"], "title": row["title"], "link": link, "question": question,
               "options": options, "type": "choice" if options else "text", "status": "OPEN",
               "asked": _now(), "answer": None, "answered": None, "done": False}
        items.append(rec)
        write_json(FILE, items)
    return rec, True


def buttons(rec: dict) -> list:
    """WhatsApp reply buttons [(id, title)] for a question (at most 3). A tap sends its id as a command."""
    qid, opts = rec["id"], rec.get("options") or []
    skip = (f"ANSWER {qid} SKIP", "Skip")
    if yes_no(opts):
        return [(f"ANSWER {qid} YES", "Yes"), (f"ANSWER {qid} NO", "No"), skip]
    if 1 <= len(opts) <= 2:
        return [(f"ANSWER {qid} {o}", o[:20]) for o in opts] + [skip]
    return [skip]


def how_to_answer(rec: dict) -> str:
    qid, opts = rec["id"], rec.get("options") or []
    if yes_no(opts):
        return f"Tap Yes, No or Skip on WhatsApp, or reply  ANSWER {qid} YES  /  ANSWER {qid} NO  /  ANSWER {qid} SKIP"
    if opts:
        return (f"Reply  ANSWER {qid} <one of: {', '.join(opts)}>  or  ANSWER {qid} SKIP  "
                f"(on WhatsApp, Skip is a button)")
    return f"Reply  ANSWER {qid} <your answer>  (for example ANSWER {qid} 20)  or  ANSWER {qid} SKIP"


def message(rec: dict, reason: str, steps: str = "", tail: str = "") -> dict:
    """The report for a question: the email, and the WhatsApp text that carries the buttons."""
    n, title, company = rec["number"], rec["title"], rec["company"]
    email = (f"The application stopped at a question it must not guess.\n\nQuestion (word for word):\n  {rec['question']}\n\n"
             f"Why it stopped: {reason}\nA partial draft is saved on LinkedIn:\n{rec['link']}\n\n"
             f"What was filled so far:\n{steps}{tail}\n\n"
             f"HOW TO ANSWER: {how_to_answer(rec)}\nOr use the Questions page in JobMonitor. Your answer is saved as a "
             f"standing answer for exactly this question, then the application is finished.")
    whatsapp = (f"#{n} needs your answer\n{title} at {company}\n\nQuestion:\n{rec['question']}\n\n"
                + ("Tap an answer below." if rec.get("options") else
                   f"Reply  ANSWER {rec['id']} <your answer>  (for example ANSWER {rec['id']} 20), or tap Skip."))
    return {"subject": f"#{n} Needs your answer: {title} at {company}", "body": email, "wa_text": whatsapp,
            "wa_prompt": f"Question for #{n} at {company}: tap an answer."}


def ask_and_report(row: dict, result: dict, link: str, steps: str = "", tail: str = "") -> bool:
    """Record the question and tell Raja how to answer it. Returns False if there was no question text to record
    (the caller then sends its ordinary report)."""
    rec, created = ask(row, result, link)
    if rec is None:
        return False
    if created:
        m = message(rec, result.get("reason", ""), steps, tail)
        reporter.enqueue("question", m["subject"].split(" Needs your answer")[0] + " Needs your answer: "
                         + f"{rec['title']} at {rec['company']}", m["body"], buttons=buttons(rec),
                         wa_text=m["wa_text"], wa_prompt=m["wa_prompt"], email=False)
    return True


# ---- answering ------------------------------------------------------------

def _canonical(rec: dict, value: str):
    """The text to save for `value`, or None if it is not acceptable for this question."""
    v = value.strip()
    opts = rec.get("options") or []
    if not opts:
        return v
    lowered = v.lower()
    if yes_no(opts) and lowered in ("yes", "y", "true", "no", "n", "false"):
        want = "yes" if lowered in ("yes", "y", "true") else "no"
        return next(o for o in opts if o.strip().lower() == want)
    return next((o for o in opts if o.strip().lower() == lowered), None)


def answer(qid: str, value: str):
    """Save Raja's answer and queue the application to be finished. Returns (ok, message)."""
    rec = get(qid)
    if rec is None:
        return False, f"No question with id {qid}."
    if rec["status"] != "OPEN":
        return False, f"Question {rec['id']} was already {rec['status'].lower()}."
    v = (value or "").strip()
    if not v:
        return False, f"No answer was given for question {rec['id']}."
    if v.upper() == "SKIP":
        return skip(rec["id"])
    canonical = _canonical(rec, v)
    if canonical is None:
        return False, (f"'{v}' is not one of this question's choices ({', '.join(rec['options'])}). "
                       f"Reply again with ANSWER {rec['id']} and one of them.")
    if len(canonical) > MAX_ANSWER_LENGTH:
        return False, f"That answer is {len(canonical)} characters; the limit is {MAX_ANSWER_LENGTH}."
    if not answers.acceptable_earlier_answer(rec["question"], canonical):
        return False, ("Not saved: that answer breaks a safety rule (0 years of experience, a school that must "
                       "not appear, or a risk-appetite or salary question).")
    how = answers.set_answer(rec["question"], canonical)
    _update(rec["id"], status="ANSWERED", answer=canonical, answered=_now())
    try:
        from . import scheduler
        scheduler.run_soon("applier")          # finish it at the next scheduler pass, not in 10-15 minutes
    except Exception:
        pass
    return True, (f"Saved '{canonical}' as your answer ({how}) to: {rec['question'][:120]}\n"
                  f"The application ({rec['title']} at {rec['company']}) will be finished on the next applier run, "
                  f"usually within a minute or two. If another question comes up you will be asked again.")


def skip(qid: str):
    """Do not pursue this application. The draft stays on LinkedIn untouched; nothing is submitted."""
    rec = get(qid)
    if rec is None:
        return False, f"No question with id {qid}."
    if rec["status"] != "OPEN":
        return False, f"Question {rec['id']} was already {rec['status'].lower()}."
    _update(rec["id"], status="SKIPPED", answered=_now(), done=True)
    row = next((r for r in tracker.rows() if r["number"] == rec["number"]), None)
    if row and row["status"] == "AWAITING RAJA":
        tracker.settle(rec["number"], DO_NOT_ATTEMPT, f"Raja chose to skip this application at the question: {rec['question']}")
    queue_tool.set_status(rec["job_id"], DO_NOT_ATTEMPT)
    return True, (f"Skipped {rec['title']} at {rec['company']}. Nothing will be submitted; the saved draft is left "
                  f"on LinkedIn as it is.")


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    cmd = args[0] if args else "list"
    if cmd == "list":
        items = open_questions()
        for q in items:
            print(f"{q['id']}  #{q['number']} {q['company']} | {q['title'][:40]}\n    {q['question']}\n    options: {q['options'] or 'free text'}")
        print(f"{len(items)} open question(s)." if items else "No open questions.")
        return 0
    if cmd == "answer" and len(args) >= 3:
        ok, msg = answer(args[1], " ".join(args[2:]))
    elif cmd == "skip" and len(args) == 2:
        ok, msg = skip(args[1])
    else:
        print(__doc__)
        return 1
    print(msg)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
