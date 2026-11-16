"""Gate 1: deterministic skip rules. Returns (passed, reason).

By design there is no leadership-title check, no applicant-count check and no
workplace/location check here; the search and the queue handle those.
"""
import re

# Exact company names (case-insensitive). Ladders is deliberately NOT here.
EXCLUDED_COMPANIES = {
    "jobgether", "archer", "hackajob", "axial search", "crossover", "get it",
    "ethos", "talentify", "dataannotation", "toptal",
    "stealth stanford ai startup", "lensa", "tempus", "harnham", "swooped",
    "remotejobsone",  # standing ban: ruled a fraud
}

AI_TYPE = re.compile(
    r"\b(ai|artificial intelligence|genai|generative ai|machine learning|ml|llm|data science)\b",
    re.I)
NOT_AI_CORE = re.compile(r"\b(marketing|revenue|go[- ]to[- ]market|gtm)\b", re.I)
PARTNER_BD = re.compile(r"partnerships|business development", re.I)
PROCESS_INTEL = re.compile(r"process intelligence|process mining", re.I)
EQUITY_ONLY = re.compile(r"equity[- ]only|no cash compensation|no salary", re.I)

PHD = re.compile(r"ph\.?\s?d|doctorate|doctoral", re.I)
SOFTENERS = re.compile(
    r"prefer|a plus|nice to have|bonus|desired|ideal|an advantage|beneficial|"
    r"master'?s?\b[^.\n]{0,20}\bor\b[^.\n]{0,12}(ph\.?\s?d|doctorate)|"
    r"(ph\.?\s?d|doctorate)[^.\n]{0,12}\bor\b[^.\n]{0,20}master",
    re.I)


SOFT_HEADING = re.compile(
    r"^(preferred|nice[- ]to[- ]have|bonus|desired|ideal|plus)\b|"
    r"\b(preferred|desired|nice[- ]to[- ]have)\s+(qualifications|skills|experience|requirements)\b|"
    r"^what (will )?(sets?|set) you apart|^additional (qualifications|skills)", re.I)
HARD_HEADING = re.compile(
    r"^((basic|minimum|required|key|core)\s+)?(qualifications|requirements)\b|"
    r"^what you.?ll (need|bring)|^responsibilities|^about\b|^the role\b|^who you are", re.I)
OR_LINE = re.compile(r"^[-*•\s]*or\b[:\s]*$|^[-*•\s]*or\s", re.I)


def _is_heading(line: str, pattern) -> bool:
    return len(line) < 80 and bool(pattern.search(line.strip(" :")))


def _in_or_ladder(lines: list, i: int) -> bool:
    """A doctorate listed as one tier of 'Doctorate OR Master's OR Bachelor's' is not required."""
    near = [lines[k] for k in (i - 1, i + 1) if 0 <= k < len(lines)]
    return any(OR_LINE.search(l) for l in near) or bool(re.search(r"[,;]\s*or\s*$", lines[i], re.I))


def _phd_required(description: str) -> bool:
    """True if a PhD/doctorate is a requirement.

    Blocks: 'PhD required', 'PhD or equivalent', a PhD listed under qualifications.
    Does not block: 'PhD preferred', a PhD under a Preferred/Nice-to-have heading, 'Master's or
    PhD', or a doctorate that is one tier of an OR ladder with lower degrees.
    """
    lines = [l.strip() for l in re.split(r"[\n\r]+", description) if l.strip()]
    soft = False
    for i, line in enumerate(lines):
        if _is_heading(line, SOFT_HEADING):
            soft = True
            continue
        if _is_heading(line, HARD_HEADING):
            soft = False
            continue
        if soft or _in_or_ladder(lines, i):
            continue
        for sentence in re.split(r"(?<=[.;])\s", line):
            if PHD.search(sentence) and not SOFTENERS.search(sentence):
                return True
    return False


def gate1(posting: dict):
    title = posting.get("title", "")
    company = posting.get("company", "").strip().lower()
    desc = posting.get("description", "") or ""

    if company in EXCLUDED_COMPANIES:
        return False, f"excluded company: {posting.get('company')}"
    if PARTNER_BD.search(title):
        return False, "title is partnerships / business development"
    if NOT_AI_CORE.search(title) and not AI_TYPE.search(title):
        return False, "marketing/revenue/GTM title with no AI-type word"
    if PROCESS_INTEL.search(title):
        return False, "process intelligence / process mining title"
    if EQUITY_ONLY.search(desc):
        return False, "equity-only / no cash compensation"
    if _phd_required(desc):
        return False, "PhD/doctorate required"
    return True, "ok"
