"""Reserve the next application number for the tracker.

    python -m agent.next_number          # reserve and print the next number
    python -m agent.next_number --set N  # set the next number to issue (e.g. to match an existing tracker)
"""
import sys

from .store import locked, read_json, write_json

FILE = "next_number.json"


def reserve() -> int:
    with locked("next_number"):
        n = read_json(FILE, {"next": 1})["next"]
        write_json(FILE, {"next": n + 1})
        return n


def set_next(n: int) -> None:
    with locked("next_number"):
        write_json(FILE, {"next": int(n)})


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--set":
        set_next(sys.argv[2])
    else:
        print(reserve())
