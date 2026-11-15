"""Small JSON-file store with cross-process locking and atomic writes.

Discovery, applier and sender run as separate processes, so every
read-modify-write of a shared file must happen inside locked(name).
"""
import contextlib
import json
import os
import time
from pathlib import Path


def data_dir() -> Path:
    d = Path(os.environ.get("AGENT_DATA_DIR")
             or Path(__file__).resolve().parent.parent / "data" / "agent")
    d.mkdir(parents=True, exist_ok=True)
    return d


@contextlib.contextmanager
def locked(name: str, timeout: float = 30, stale: float = 60):
    lock = data_dir() / f"{name}.lock"
    start = time.time()
    while True:
        try:
            os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            break
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > stale:
                    lock.unlink(missing_ok=True)  # holder crashed
                    continue
            except FileNotFoundError:
                continue
            if time.time() - start > timeout:
                raise TimeoutError(f"Could not lock {name}")
            time.sleep(0.05)
    try:
        yield
    finally:
        lock.unlink(missing_ok=True)


def read_json(name: str, default):
    p = data_dir() / name
    if not p.exists():
        return default
    return json.loads(p.read_text(encoding="utf-8"))


def write_json(name: str, obj) -> None:
    p = data_dir() / name
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2), encoding="utf-8")
    os.replace(tmp, p)
