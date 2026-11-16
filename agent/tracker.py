"""Application tracker (tracker.json). Gate 2 reads it to catch duplicates.

An application number is reserved with next_number.reserve() first, then the row is booked here
once the application is settled on LinkedIn's or the employer's own record.
"""
from datetime import datetime

from .reporter import NY
from .store import locked, read_json, write_json

FILE = "tracker.json"
STATUSES = {"SUBMITTED", "AWAITING RAJA", "DO NOT ATTEMPT"}


def book(number: int, company: str, title: str, job_id: str, status: str, url: str = "",
         resume: str = "", note: str = "") -> dict:
    if status not in STATUSES:
        raise ValueError(f"unknown status '{status}'")
    row = {"number": number, "company": company, "title": title, "job_id": str(job_id),
           "status": status, "url": url, "resume": resume, "note": note,
           "booked": datetime.now(NY).isoformat(timespec="seconds")}
    with locked("tracker"):
        rows = read_json(FILE, [])
        if any(r["number"] == number for r in rows):
            raise ValueError(f"application number {number} is already booked")
        rows.append(row)
        write_json(FILE, rows)
    return row


def rows() -> list:
    return read_json(FILE, [])
