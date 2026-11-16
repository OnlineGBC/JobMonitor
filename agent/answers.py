"""Answering application questions. The rules live in code; the model only fills the gaps.

decide_answer() returns {"action", "value", "reason"} where action is one of:
  answer            - fill `value`
  blank             - leave empty (optional question)
  pause             - stop this application, mark AWAITING RAJA, report the question word for word
  skip_application  - a hard blocker: abandon the application entirely
"""
import re
from pathlib import Path

from . import config, llm

ANSWER_BANK = Path(__file__).resolve().parent.parent / "resumes" / "StandingAnswers.txt"
SALARY_TARGET = 206000
REFERENCES_TEXT = "Will be provided once interview is confirmed"

ATTENDANCE_DATES = re.compile(
    r"attendance dates?|dates? attended|"
    r"(school|college|university|education|degree).{0,40}(start|end|from|to)\s*(date|year|month)|"
    r"(start|end)\s*(date|year|month).{0,40}(school|college|university|education|degree)", re.I)
RISK_OR_FINANCIAL = re.compile(
    r"risk (appetite|tolerance)|financial capacity|net worth|investable assets|liquid assets", re.I)
BANNED_SCHOOLS = re.compile(r"st\.?\s*stephen|st\.?\s*xavier", re.I)
SALARY_Q = re.compile(r"salary|compensation|pay (expectation|range)|expected pay|desired pay", re.I)
YEARS_Q = re.compile(r"years?\b.{0,40}(experience|of)|how many years", re.I)
REFERENCES_Q = re.compile(r"reference", re.I)
CONTACT_REFS_Q = re.compile(r"(may|can|could|permission).{0,30}contact|contact.{0,30}reference", re.I)


def load_bank() -> str:
    return ANSWER_BANK.read_text(encoding="utf-8")


LEARNED_HEADING = "Learned from earlier applications (added automatically; edit or delete freely)"


def learn(label: str, value: str, path: Path = None) -> bool:
    """Record an answer you gave in an earlier application as a standing answer.

    Appended under a marked section of StandingAnswers.txt. Returns False if the question is already
    recorded there. The explicit rules and the answers above that section always take priority.
    """
    path = path or ANSWER_BANK
    key = re.sub(r"\W+", " ", label.lower()).strip()
    with open(path, encoding="utf-8", newline="") as f:  # newline="": keep the file's own line endings
        text = f.read()
    if any(re.sub(r"\W+", " ", line.lower()).strip().startswith(key)
           for line in text.splitlines() if "→" in line):
        return False
    _append_learned(path, text, label, value)
    return True


def _append_learned(path: Path, text: str, label: str, value: str) -> None:
    nl = "\r\n" if "\r\n" in text else "\n"
    if LEARNED_HEADING not in text:
        text = text.rstrip() + nl + nl + LEARNED_HEADING + nl
    elif not text.endswith(nl):
        text += nl
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text + f"- {label.strip()} → {value.strip()}" + nl)


def set_answer(label: str, value: str, path: Path = None) -> str:
    """Save Raja's answer to this EXACT question, replacing an older saved answer for it.

    Returns 'replaced' or 'added'. Unlike learn(), a newer answer wins: the answer he gives now is the one
    that must be used. Only the whole question text is matched, and the file's line endings are kept.
    """
    path = path or ANSWER_BANK
    key = _q_key(label)
    with open(path, encoding="utf-8", newline="") as f:
        text = f.read()
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        m = re.match(r"^(\s*-\s*)(.+?)(\s*→\s*)(.+?)(\r?\n?)$", line)
        if m and _q_key(m.group(2)) == key:
            lines[i] = f"{m.group(1)}{m.group(2)}{m.group(3)}{value.strip()}{m.group(5)}"
            with open(path, "w", encoding="utf-8", newline="") as f:
                f.write("".join(lines))
            return "replaced"
    _append_learned(path, text, label, value)
    return "added"


def _q_key(text: str) -> str:
    return re.sub(r"\W+", " ", text.lower()).strip()


def exact_answer(label: str, bank: str):
    """The answer Raja saved for exactly this question ('- <question> → <answer>'), or None.

    Matching is on the whole question text (case and punctuation ignored), never on part of it, so an answer
    to one question can not leak onto a similar-looking one.
    """
    want = _q_key(label)
    if not want:
        return None
    for line in bank.splitlines():
        m = re.match(r"^\s*-\s*(.+?)\s*→\s*(.+?)\s*$", line)
        if m and _q_key(m.group(1)) == want:
            return m.group(2)
    return None


def acceptable_earlier_answer(label: str, value: str) -> bool:
    """An earlier answer may fill a gap only if it passes the same safety rules as a model answer."""
    if not value or BANNED_SCHOOLS.search(value):
        return False
    if YEARS_Q.search(label) and re.fullmatch(r"0+(\.0+)?(\s*years?)?", value.strip().lower()):
        return False
    return not (RISK_OR_FINANCIAL.search(label) or SALARY_Q.search(label))


def _pause(reason: str) -> dict:
    return {"action": "pause", "value": None, "reason": reason}


def _match_option(value: str, options: list):
    v = (value or "").strip().lower()
    return next((o for o in options if o.strip().lower() == v), None)


def _salary_range_option(options: list):
    """The option whose range contains SALARY_TARGET, or None."""
    def num(m):
        n = float(m.group(1).replace(",", ""))
        return n * 1000 if m.group(2) else n

    for opt in options:
        nums = [num(m) for m in re.finditer(r"(\d[\d,]*(?:\.\d+)?)\s*([kK])?", opt.replace("$", ""))]
        nums = [n for n in nums if n >= 1000]
        if not nums:
            continue
        low = opt.lower()
        if len(nums) >= 2 and min(nums) <= SALARY_TARGET <= max(nums):
            return opt
        if len(nums) == 1:
            n = nums[0]
            if re.search(r"\+|over|above|more than|at least", low) and SALARY_TARGET >= n:
                return opt
            if re.search(r"under|below|less than|up to", low) and SALARY_TARGET <= n:
                return opt
    return None


PROMPT = """You fill in a job application for Raja Gopalan using ONLY his standing answers below.

STANDING ANSWERS:
{bank}

RULES
- Use only facts and answers in the standing answers. Never invent. If they give no clear basis, pause.
- Never answer 0 years of experience.
- A question naming a credential, certification or licence he does not literally hold: pause.
- For choice questions the value must be exactly one of the options.
- Applications list only Columbia, Harvard and Manhattanville as schools.

QUESTION: {label}
TYPE: {qtype}
OPTIONS: {options}

Reply with JSON only: {{"action": "answer" or "pause", "value": "<answer or null>", "reason": "<one sentence>"}}"""


def decide_answer(question: dict, bank: str = None, client=None, model: str = None) -> dict:
    label = (question.get("label") or "").strip()
    qtype = question.get("type", "text")
    options = question.get("options") or []
    required = bool(question.get("required"))

    # Hard blockers
    if RISK_OR_FINANCIAL.search(label):
        return {"action": "skip_application", "value": None,
                "reason": "form asks about risk appetite or financial capacity"}
    if required and ATTENDANCE_DATES.search(label):
        return {"action": "skip_application", "value": None,
                "reason": "form requires education attendance dates"}

    # Optional questions stay blank
    if not required:
        return {"action": "blank", "value": None, "reason": "optional"}

    # An answer Raja gave to THIS EXACT question (a "question -> answer" line in the standing answers) is used
    # as given, with no model involved. It still has to pass the same safety rules as any other answer.
    known = exact_answer(label, bank if bank is not None else load_bank())
    if known is not None:
        if not acceptable_earlier_answer(label, known):
            return _pause("the saved answer to this question breaks a safety rule")
        if options:
            known = _match_option(known, options)
            if known is None:
                return _pause("the saved answer is not one of the form's options")
        return {"action": "answer", "value": known, "reason": "your saved answer to this exact question"}

    # Salary: Open/blank where allowed (not reachable here, it is required); a forced range uses 206000
    if SALARY_Q.search(label):
        if options:
            pick = _salary_range_option(options)
            if pick:
                return {"action": "answer", "value": pick, "reason": f"range containing {SALARY_TARGET}"}
            return _pause("forced salary range has no option containing 206000")
        return _pause("forced numeric salary field")

    # References
    if REFERENCES_Q.search(label):
        if CONTACT_REFS_Q.search(label):
            no = _match_option("No", options) if options else "No"
            return {"action": "answer", "value": no or "No", "reason": "may-contact-references is No"} \
                if (no or not options) else _pause("no 'No' option for contacting references")
        return {"action": "answer", "value": REFERENCES_TEXT, "reason": "references standing answer"}

    # Everything else: the model, constrained by the standing answers, then validated in code
    try:
        if client is None:
            import anthropic
            client = anthropic.Anthropic()
        resp = client.messages.create(
            model=model or config.FORM_ANSWER_MODEL, max_tokens=2000,
            messages=[{"role": "user", "content": PROMPT.format(
                bank=bank if bank is not None else load_bank(), label=label, qtype=qtype,
                options=options or "(free entry)")}])
        data = llm.json_object(resp)
        action, value = data.get("action"), data.get("value")
        reason = str(data.get("reason", ""))
    except Exception as e:
        return _pause(f"could not get an answer: {e}")

    if action != "answer" or value in (None, ""):
        return _pause(reason or "no basis in the standing answers")
    value = str(value).strip()
    if BANNED_SCHOOLS.search(value):
        return _pause("answer would list a school that must not appear")
    if options:
        value = _match_option(value, options)
        if value is None:
            return _pause("answer is not one of the form's options")
    if YEARS_Q.search(label) and re.fullmatch(r"0+(\.0+)?(\s*years?)?", value.lower()):
        return _pause("would answer 0 years of experience")
    return {"action": "answer", "value": value, "reason": reason}
