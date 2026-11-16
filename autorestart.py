"""Restart the web UI by itself when its code changes, and on demand (the "Restart web UI" button).

Why: the scheduler, the WhatsApp reply handling and the webhook all run inside the web UI process, which loads
the code once. A code change only takes effect after a restart.

How, in two parts:
  * supervise()  - `python web_monitor_menu.py` first starts itself again as a child process and watches it. When
                   the child exits with RESTART_EXIT it is started again. Any other exit (Ctrl+C, a crash) ends
                   everything, exactly as before. The command to start the web UI is unchanged.
  * Restarter    - runs inside the child. It watches the .py files and templates. After a change it waits for
                   the files to stop changing, checks that every .py file still compiles (a half-saved or broken
                   file never takes the web UI down), waits until nothing is running (no monitor, no agent run),
                   then exits with RESTART_EXIT. The button does the same on request; `force` skips the wait.

Set AUTO_RESTART=0 to turn all of this off.
"""
import logging
import os
import py_compile
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

RESTART_EXIT = 75
CHILD_ENV = "JOBMONITOR_CHILD"
ROOT = Path(__file__).resolve().parent
WATCHED = ("*.py", "agent/*.py", "templates/*.html")


def enabled() -> bool:
    return os.getenv("AUTO_RESTART", "1").strip().lower() not in ("0", "false", "")


def files(root: Path = ROOT) -> list:
    out = []
    for pattern in WATCHED:
        out += sorted(root.glob(pattern))
    return out


def signature(root: Path = ROOT) -> dict:
    """{path: modified time} for every file whose change needs a restart."""
    sig = {}
    for p in files(root):
        try:
            sig[str(p)] = p.stat().st_mtime_ns
        except OSError:
            pass
    return sig


def broken(paths: list) -> list:
    """The .py files in `paths` that do not compile, as 'name: reason'."""
    bad = []
    for p in paths:
        if str(p).endswith(".py"):
            try:
                py_compile.compile(str(p), cfile=os.path.join(tempfile.gettempdir(), "jm_check.pyc"), doraise=True)
            except py_compile.PyCompileError as e:
                bad.append(f"{Path(p).name}: {str(e).strip().splitlines()[-1]}")
            except OSError:
                pass
    return bad


def _exit_process(code: int = RESTART_EXIT) -> None:
    logging.shutdown()
    os._exit(code)


class Restarter:
    def __init__(self, is_busy, exit_fn=_exit_process, sig_fn=signature, poll: float = 10.0):
        self.is_busy, self.exit_fn, self.sig_fn, self.poll = is_busy, exit_fn, sig_fn, poll
        self.baseline = sig_fn()
        self._last = self.baseline
        self._reported = None
        self._stop = threading.Event()

    def changed(self, sig: dict = None) -> list:
        sig = self.sig_fn() if sig is None else sig
        return sorted(p for p in set(sig) | set(self.baseline) if sig.get(p) != self.baseline.get(p))

    def check(self) -> str:
        """One look. Returns what happened: idle / settling / broken / busy / restarting."""
        sig = self.sig_fn()
        moved, self._last = sig != self._last, sig
        changed = self.changed(sig)
        if not changed:
            return "idle"
        if moved:
            return "settling"                       # still being saved: look again next time
        bad = broken(changed)
        if bad:
            if self._reported != tuple(bad):
                logging.error("Code changed but NOT restarting, it does not compile: " + "; ".join(bad))
                self._reported = tuple(bad)
            return "broken"
        if self.is_busy():
            return "busy"
        logging.info("Code changed (" + ", ".join(Path(p).name for p in changed[:5]) + "): restarting the web UI")
        self.exit_fn(RESTART_EXIT)
        return "restarting"

    def request(self, force: bool = False) -> tuple:
        """The button. Returns (status, message). 'ok' means the restart has been started."""
        bad = broken(files())
        if bad:
            return "error", "Not restarted, a code file has an error: " + "; ".join(bad)
        if self.is_busy() and not force:
            return "busy", "A monitor or agent run is in progress. Restarting now would cut it off."
        logging.info("Restart requested from the web UI" + (" (forced)" if force else ""))
        threading.Timer(1.0, self.exit_fn, args=(RESTART_EXIT,)).start()    # after the reply has been sent
        return "ok", "Restarting. This page reloads when the web UI is back."

    def start(self) -> None:
        def loop():
            while not self._stop.wait(self.poll):
                try:
                    self.check()
                except Exception as e:
                    logging.error(f"Auto-restart check failed: {e}")
        threading.Thread(target=loop, daemon=True, name="auto-restart").start()

    def stop(self) -> None:
        self._stop.set()


def supervise(argv: list = None, run=subprocess.run) -> int:
    """Run the web UI as a child and start it again whenever it asks for a restart."""
    argv = argv if argv is not None else [sys.executable] + sys.argv
    env = {**os.environ, CHILD_ENV: "1"}
    while True:
        try:
            code = run(argv, env=env).returncode
        except KeyboardInterrupt:
            return 0
        if code != RESTART_EXIT:
            return code
        logging.info("Web UI restarting")
        time.sleep(1)
