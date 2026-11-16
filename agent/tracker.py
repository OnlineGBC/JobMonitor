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


def settle(number: int, status: str, note: str) -> dict:
    """Move an existing row to a new status, recording WHY and WHEN. The row keeps its number and history of
    where it came from; only an AWAITING RAJA row may be settled (a SUBMITTED row never goes backwards)."""
    if status not in STATUSES:
        raise ValueError(f"unknown status '{status}'")
    with locked("tracker"):
        rows_ = read_json(FILE, [])
        row = next((r for r in rows_ if r["number"] == number), None)
        if row is None:
            raise ValueError(f"no application number {number}")
        if row["status"] != "AWAITING RAJA":
            raise ValueError(f"application {number} is {row['status']}, so it cannot be settled again")
        row.update(status=status, note=note, settled=datetime.now(NY).isoformat(timespec="seconds"),
                   previous_note=row.get("note", ""))
        write_json(FILE, rows_)
        return row


def mark_reminded(number: int) -> None:
    """Remember that the one reminder for this row was sent."""
    with locked("tracker"):
        rows_ = read_json(FILE, [])
        for r in rows_:
            if r["number"] == number:
                r["reminded"] = datetime.now(NY).isoformat(timespec="seconds")
        write_json(FILE, rows_)


def rows() -> list:
    return read_json(FILE, [])
