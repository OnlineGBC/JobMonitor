"""The agent's own log lines about a role, for answering "what went wrong with X?" / "check the logs".

excerpts(text) finds the roles named in Raja's message (company or title words, or a LinkedIn job id), then collects
the log lines that mention them (with a little context) from the agent's log files and the web UI log. If he simply
asks for the logs with no role, it returns the latest lines of the applier and discovery logs.

fix_links(reply) repairs LinkedIn links in a model-written reply: a job id that is not one the agent really knows
(for example a link with its last digits cut off) is replaced by the known id it was cut from, or the link is dropped.
"""
import re
from pathlib import Path

from . import chatlog, decisions, queue_tool, tracker
from .store import data_dir

ROOT = Path(__file__).resolve().parent.parent
WEB_LOG = ROOT / "logs" / "screen_compare.log"
AGENT_LOGS = ("applier.log", "discovery.log", "acceptance.log", "sender.log")
NOISE = ("POST /whatsapp/webhook", "HTTP Request:", "Keepalive not sent", "GET /api/scheduler")
GENERIC = {"chief", "officer", "director", "senior", "head", "vice", "president", "manager", "the", "and", "for", "ltd",
           "inc", "llc", "corp", "company", "technology", "remote", "united", "states",
           "was", "what", "can", "you", "with", "went", "wrong", "check", "log", "logs", "why", "did", "not", "how", "are",
           "has", "have", "this", "that", "from", "about", "role", "job", "apply", "applied", "please", "tell", "show",
           "more", "details", "error", "happened", "happen", "does", "fail", "failed", "it", "any", "all", "get", "got"}
SUFFIX = {"ltd", "inc", "llc", "corp", "corporation", "group", "company", "the", "and", "co"}   # not part of a name
MAX_CHARS = 8000
CONTEXT = 2
READ_LINES = 4000


def _roles() -> list:
    out = []
    for r in tracker.rows():
        out.append((str(r.get("job_id")), r.get("company", ""), r.get("title", "")))
    for q in queue_tool.all_items():
        out.append((str(q.get("job_id")), q.get("company", ""), q.get("title", "")))
    for d in decisions.all_decisions():
        for jid in d.get("job_ids") or []:
            out.append((str(jid), d.get("company", ""), d.get("title", "")))
    return out


def _words(text: str) -> set:
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(w) >= 3 and w not in GENERIC}


def _name_words(text: str) -> set:
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(w) >= 3}


def terms_for(text: str) -> set:
    """What to search the logs for: job ids named or implied by the message, and the matching company names."""
    wanted = set(re.findall(r"\b\d{7,}\b", text or ""))
    words = _words(text)
    terms = set(wanted)
    for jid, company, _title in _roles():
        distinct = _name_words(company) - SUFFIX
        if jid in wanted or (distinct and distinct <= words):
            terms.add(jid)
            if company:
                terms.add(company.lower())
    return {t for t in terms if t}


def _read_tail(path: Path) -> list:
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()[-READ_LINES:]
    except OSError:
        return []


def _matching(lines: list, terms: set) -> list:
    keep = set()
    for i, line in enumerate(lines):
        low = line.lower()
        if any(t in low for t in terms):
            keep.update(range(max(0, i - CONTEXT), min(len(lines), i + CONTEXT + 1)))
    return [lines[i] for i in sorted(keep) if not any(n in lines[i] for n in NOISE)]


def excerpts(text: str, max_chars: int = MAX_CHARS) -> str:
    """Log lines relevant to this message, labelled by file; '(none)' if there are none."""
    files = [data_dir() / "logs" / n for n in AGENT_LOGS] + [WEB_LOG]
    terms = terms_for(text)
    asked_for_logs = bool(re.search(r"\blogs?\b", text or "", re.I))
    parts = []
    for f in files:
        lines = _read_tail(f)
        if terms:
            chosen = _matching(lines, terms)
        elif asked_for_logs and f.name in ("applier.log", "discovery.log", "screen_compare.log"):
            chosen = [l for l in lines if not any(n in l for n in NOISE)][-25:]
        else:
            chosen = []
        if chosen:
            parts.append(f"== {f.name} ==\n" + "\n".join(l[:300] for l in chosen[-60:]))
    out = "\n\n".join(parts)
    return out[-max_chars:] if out else "(none)"


# ---- links ---------------------------------------------------------------

_LINK = re.compile(r"https?://(?:www\.)?linkedin\.com/jobs/view/(\d+)/?")


def known_ids() -> set:
    ids = {r[0] for r in _roles()}
    for m in chatlog.last(200):
        if m.get("who") == "agent" and m.get("subject") not in ("Reply", "Not understood"):   # not model-written text
            ids.update(_LINK.findall(m.get("text") or ""))
    return {i for i in ids if i}


def fix_links(reply: str, known: set = None) -> str:
    known = known_ids() if known is None else known

    def repair(m):
        jid = m.group(1)
        if jid in known:
            return m.group(0)
        cut_from = [k for k in known if k.startswith(jid) and len(jid) >= 6]
        if len(cut_from) == 1:
            return f"https://www.linkedin.com/jobs/view/{cut_from[0]}/"
        return "(link not available)"

    return _LINK.sub(repair, reply or "")
