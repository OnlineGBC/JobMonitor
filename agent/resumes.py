"""Choose the resume for a job title.

Spec: VP titles -> VP resume; Director family -> Director resume; Chief -> Chief; Head -> VP; CEO -> Chief.

Two places a resume is used, with two rules:
  * LinkedIn Easy Apply: select a resume ALREADY UPLOADED there, matched by name (extension ignored: docx
    and pdf hold the same content). For Director that is '...Dir-SystemArchitectEnterpriseAI_DataScience',
    which is not on disk, so it is never uploaded. A resume missing from LinkedIn is uploaded only when a
    file with that name exists in resumes/ (local_path).
  * Company sites: upload a file from resumes/ (company_site_path). For Director that is the SAP resume.
BDO and FDE are reserved for later.
"""
import re
from pathlib import Path

RESUME_DIR = Path(__file__).resolve().parent.parent / "resumes"
VP = "RajaGopalanVPEnterpriseAI_DataScience.pdf"
DIRECTOR = "RajaGopalanDir-SystemArchitectEnterpriseAI_DataScience.docx"  # lives on LinkedIn only
DIRECTOR_COMPANY_SITE = "RajaGopalanDir-SystemArchitectEnterpriseAI_SAP.docx"  # the Director file in resumes/
CHIEF = "RajaGopalanChiefAI_DataOfficer.pdf"

_CHIEF = re.compile(r"(?<![\w-])(chief|ceo|cto|cio|caio|cdo)(?![\w-])", re.I)
_VP = re.compile(r"(?<![\w-])(vp|svp|evp|vice[- ]president)(?![\w-])", re.I)
_HEAD = re.compile(r"(?<![\w-])head(?![\w-])", re.I)
_DIRECTOR = re.compile(r"(?<![\w-])director(?![\w-])", re.I)


def resume_name(title: str):
    """Return the resume name for a title, or None when no rule applies (pause and ask)."""
    # Most senior wins when a title carries several words, e.g. 'VP / Director' -> VP.
    if _CHIEF.search(title):
        return CHIEF
    if _VP.search(title) or _HEAD.search(title):
        return VP
    if _DIRECTOR.search(title):
        return DIRECTOR
    return None


def stem(name: str) -> str:
    """Name without its docx/doc/pdf extension, lower-cased: how resumes are matched."""
    return re.sub(r"\.(docx?|pdf)$", "", name or "", flags=re.I).strip().lower()


def local_path(title: str):
    """The resume file on disk for a title (any extension), or None if there is none."""
    name = resume_name(title)
    if name is None:
        return None
    for p in sorted(RESUME_DIR.glob("*")):
        if stem(p.name) == stem(name):
            return p
    return None


def company_site_path(title: str):
    """The file in resumes/ to upload on a company's own site, or None if no rule applies.

    Same choice as local_path, except Director uses the SAP Director resume that is on disk.
    """
    name = resume_name(title)
    if name is None:
        return None
    wanted = DIRECTOR_COMPANY_SITE if name == DIRECTOR else name
    return next((p for p in sorted(RESUME_DIR.glob("*")) if stem(p.name) == stem(wanted)), None)
