"""Scheduler for the agent's processes. Everything starts PAUSED.

    python -m agent.scheduler status
    python -m agent.scheduler enable discovery
    python -m agent.scheduler disable discovery

Each enabled process runs as its own subprocess, then waits a RANDOM 10 to 15 minutes before its next
run (a steady 10-minute beat looks mechanical), with a 60-minute cap per run. The loop thread is started by the web UI; this CLI only edits the
saved state, which the running loop picks up on its next tick (within ~20 s).
"""
import importlib.util
import logging
import os
import random
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from . import reporter
from .store import data_dir, locked, read_json, write_json

CONTROL = "control.json"
ROOT = Path(__file__).resolve().parent.parent
CAP_SECONDS = 60 * 60
TICK_SECONDS = 20
EXIT_LOGIN_NEEDED = 10  # same convention as monitor.py

PROCESSES = {
    # "wait": (min, max) seconds between the START of one run and the next, drawn at random each time
    "discovery": {"module": "agent.discovery", "wait": (600, 900)},
    "applier": {"module": "agent.applier", "wait": (600, 900)},
    "sender": {"module": "agent.outreach_sender", "wait": (600, 900)},
    "acceptance": {"module": "agent.acceptance_check", "wait": (84600, 88200)},  # about once a day
}


def built(name: str) -> bool:
    try:
        return importlib.util.find_spec(PROCESSES[name]["module"]) is not None
    except ModuleNotFoundError:
        return False


def load_state() -> dict:
    saved = read_json(CONTROL, {})
    return {n: {"enabled": False, **saved.get(n, {})} for n in PROCESSES}


def set_enabled(name: str, on: bool) -> None:
    if name not in PROCESSES:
        raise ValueError(f"unknown process '{name}' (choose from {', '.join(PROCESSES)})")
    if on and not built(name):
        raise ValueError(f"'{name}' is not built yet, so it cannot be enabled")
    with locked("control"):
        st = load_state()
        st[name]["enabled"] = on
        write_json(CONTROL, st)


def _update(name: str, **fields) -> None:
    with locked("control"):
        st = load_state()
        st[name].update(fields)
        write_json(CONTROL, st)


def run_soon(name: str) -> bool:
    """Ask the running scheduler to start `name` at its next pass (about 20 seconds) instead of waiting out its
    random 10-15 minute gap. Does nothing, and returns False, if that process is not enabled."""
    if name not in PROCESSES or not load_state()[name]["enabled"]:
        return False
    _update(name, next_due=0)
    return True


def launch(module: str, cap: int, log_path: Path):
    """Run `python -m module`. Returns (exit_code, timed_out). Kills the whole tree on timeout."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "ab") as log:
        p = subprocess.Popen([sys.executable, "-m", module], cwd=ROOT,
                             stdout=log, stderr=subprocess.STDOUT)
        try:
            return p.wait(timeout=cap), False
        except subprocess.TimeoutExpired:
            if os.name == "nt":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True)
            else:
                p.kill()
            p.wait()
            return None, True


def _tail(path: Path, lines: int = 12) -> str:
    try:
        return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
    except OSError:
        return "(no log)"


class AgentScheduler:
    def __init__(self, clock=time.time, launcher=launch, cap=CAP_SECONDS, rng=random.uniform):
        self.clock, self.launcher, self.cap, self.rng = clock, launcher, cap, rng
        self._running = set()
        self._stop = threading.Event()
        self._thread = None

    # -- loop ---------------------------------------------------------------
    def start(self) -> bool:
        if self._thread and self._thread.is_alive():
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="agent-scheduler")
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()

    def _loop(self):
        logging.info("Agent scheduler started (all processes paused unless enabled)")
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as e:
                logging.error(f"Agent scheduler tick failed: {e}")
            self._stop.wait(TICK_SECONDS)

    # -- one pass -----------------------------------------------------------
    def tick(self, sync: bool = False) -> list:
        """Start whatever is due. With sync=True, run it inline (for tests). Returns names started."""
        now = self.clock()
        started = []
        state = load_state()
        for name, cfg in PROCESSES.items():
            s = state[name]
            if not s["enabled"] or name in self._running or not built(name):
                continue
            due = s.get("next_due")
            if due is not None and now < due:
                continue
            _update(name, last_start=now, last_status="running", next_due=now + self.rng(*cfg["wait"]))
            self._running.add(name)
            started.append(name)
            if sync:
                self._run(name, now)
            else:
                threading.Thread(target=self._run, args=(name, now), daemon=True,
                                 name=f"agent-{name}").start()
        self._deliver(any(s["enabled"] for s in state.values()))
        return started

    def _run(self, name: str, started_at: float) -> None:
        log = data_dir() / "logs" / f"{name}.log"
        try:
            code, timed_out = self.launcher(PROCESSES[name]["module"], self.cap, log)
            status = "timed out (60-minute cap)" if timed_out else f"exit {code}"
            _update(name, last_end=self.clock(), last_status=status)
            began = datetime.fromtimestamp(started_at, reporter.NY).strftime("%Y-%m-%d %I:%M %p %Z")
            if timed_out:
                reporter.enqueue("cap", f"{name} run hit the 60-minute cap",
                                 f"Run: {name}\nStarted: {began}\nIt was stopped at the cap. "
                                 f"Last log lines:\n{_tail(log)}")
            elif code == EXIT_LOGIN_NEEDED:
                if not load_state()[name].get("login_alerted"):
                    _update(name, login_alerted=True)
                    reporter.enqueue("needs_you", f"{name}: LinkedIn sign-in needed",
                                     f"The {name} process could not sign in to LinkedIn.\n"
                                     "Run: python manual_login.py agent")
            elif code == 0:
                _update(name, login_alerted=False)
        except Exception as e:
            logging.error(f"Agent process {name} crashed in the scheduler: {e}")
            _update(name, last_end=self.clock(), last_status=f"scheduler error: {e}")
        finally:
            self._running.discard(name)

    def _deliver(self, any_enabled: bool) -> None:
        """Send pending reports; send the 12-hourly keepalive only while something is enabled."""
        try:
            reporter.process()
            if any_enabled:
                reporter.keepalive()
        except Exception as e:
            logging.error(f"Reporter pass failed: {e}")
        try:                                   # taps and replies are handled within one pass, not at the next sender run
            from . import approvals
            approvals.process_whatsapp()
        except Exception as e:
            logging.error(f"WhatsApp reply pass failed: {e}")


def _fmt_time(t):
    return datetime.fromtimestamp(t, reporter.NY).strftime("%m-%d %I:%M %p") if t else "-"


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    if not argv or argv[0] == "status":
        st = load_state()
        print(f"{'process':<11}{'state':<9}{'built':<7}{'last start (NY)':<17}last status")
        for n in PROCESSES:
            s = st[n]
            print(f"{n:<11}{'ENABLED' if s['enabled'] else 'paused':<9}{'yes' if built(n) else 'no':<7}"
                  f"{_fmt_time(s.get('last_start')):<17}{s.get('last_status', '-')}")
        return 0
    if argv[0] in ("enable", "disable") and len(argv) == 2:
        try:
            set_enabled(argv[1], argv[0] == "enable")
        except ValueError as e:
            print(f"Error: {e}")
            return 1
        print(f"{argv[1]}: {'ENABLED' if argv[0] == 'enable' else 'paused'}")
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
