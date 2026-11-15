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


def _phd_required(description: str) -> bool:
    """True if a line mentions a PhD without softening it to preferred/optional.

    'PhD or equivalent' blocks. 'Master's or PhD' and 'PhD preferred' do not.
    """
    for line in re.split(r"[\n\r]+|(?<=[.;])\s", description):
        if PHD.search(line) and not SOFTENERS.search(line):
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
