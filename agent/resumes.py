"""Choose the resume for a job title.

Spec: VP titles -> VP resume; Director family -> Director (SAP) resume; Chief -> Chief;
Head -> VP; CEO -> Chief. BDO and FDE resumes are reserved for later.
"""
import re
from pathlib import Path

RESUME_DIR = Path(__file__).resolve().parent.parent / "resumes"
VP = "RajaGopalanVPEnterpriseAI_DataScience.pdf"
DIRECTOR = "RajaGopalanDir-SystemArchitectEnterpriseAI_SAP.docx"
CHIEF = "RajaGopalanChiefAI_DataOfficer.pdf"

_CHIEF = re.compile(r"(?<![\w-])(chief|ceo|cto|cio|caio|cdo)(?![\w-])", re.I)
_VP = re.compile(r"(?<![\w-])(vp|svp|evp|vice[- ]president)(?![\w-])", re.I)
_HEAD = re.compile(r"(?<![\w-])head(?![\w-])", re.I)
_DIRECTOR = re.compile(r"(?<![\w-])director(?![\w-])", re.I)


def resume_name(title: str):
    """Return the resume file name for a title, or None when no rule applies (pause and ask)."""
    # Most senior wins when a title carries several words, e.g. 'VP / Director' -> VP.
    if _CHIEF.search(title):
        return CHIEF
    if _VP.search(title) or _HEAD.search(title):
        return VP
    if _DIRECTOR.search(title):
        return DIRECTOR
    return None


def pick_resume(title: str):
    """Return the resume Path, or None if no rule applies. Raises if the chosen file is missing."""
    name = resume_name(title)
    if name is None:
        return None
    path = RESUME_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"resume file missing: {path}")
    return path
