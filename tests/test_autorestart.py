import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import autorestart


class Watch(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        (self.dir / "agent").mkdir()
        (self.dir / "templates").mkdir()
        self.mod = self.dir / "a.py"
        self.mod.write_text("x = 1\n", encoding="utf-8")
        self.busy = False
        self.exits = []
        self.r = autorestart.Restarter(lambda: self.busy, exit_fn=self.exits.append,
                                       sig_fn=lambda: autorestart.signature(self.dir))

    def edit(self, text="x = 2\n"):
        self.mod.write_text(text, encoding="utf-8")
        os.utime(self.mod, ns=(time.time_ns(), time.time_ns() + 5_000_000_000))   # surely a new mtime

    def test_nothing_changed_does_nothing(self):
        self.assertEqual(self.r.check(), "idle")
        self.assertEqual(self.exits, [])

    def test_a_change_waits_until_the_file_stops_changing_then_restarts(self):
        self.edit()
        self.assertEqual(self.r.check(), "settling")
        self.assertEqual(self.r.check(), "restarting")
        self.assertEqual(self.exits, [autorestart.RESTART_EXIT])

    def test_templates_and_agent_files_count_too(self):
        (self.dir / "templates" / "t.html").write_text("<p>hi</p>", encoding="utf-8")
        self.assertEqual(self.r.check(), "settling")
        self.assertEqual(self.r.check(), "restarting")

    def test_it_waits_while_a_run_is_in_progress(self):
        self.busy = True
        self.edit()
        self.r.check()
        self.assertEqual(self.r.check(), "busy")
        self.assertEqual(self.exits, [])
        self.busy = False
        self.assertEqual(self.r.check(), "restarting")

    def test_a_file_that_does_not_compile_never_causes_a_restart(self):
        self.edit("def broken(:\n")
        self.r.check()
        self.assertEqual(self.r.check(), "broken")
        self.assertEqual(self.exits, [])
        self.edit("x = 3\n")                                   # fixed: now it restarts
        self.r.check()
        self.assertEqual(self.r.check(), "restarting")


class Button(Watch):
    def request(self, force=False):
        with mock.patch("autorestart.files", lambda root=None: [self.mod]):
            with mock.patch("autorestart.threading.Timer") as timer:
                out = self.r.request(force)
                return out, timer

    def test_restarts_when_idle(self):
        (status, _), timer = self.request()
        self.assertEqual(status, "ok")
        timer.assert_called_once()

    def test_refuses_while_busy_unless_forced(self):
        self.busy = True
        (status, message), timer = self.request()
        self.assertEqual(status, "busy")
        self.assertIn("in progress", message)
        timer.assert_not_called()
        (status, _), timer = self.request(force=True)
        self.assertEqual(status, "ok")
        timer.assert_called_once()

    def test_refuses_when_the_code_does_not_compile(self):
        self.edit("def broken(:\n")
        (status, message), timer = self.request(force=True)
        self.assertEqual(status, "error")
        self.assertIn("a.py", message)
        timer.assert_not_called()


class Supervise(unittest.TestCase):
    def test_restart_exit_starts_it_again_and_any_other_exit_ends_it(self):
        codes = iter([autorestart.RESTART_EXIT, autorestart.RESTART_EXIT, 0])
        run = mock.Mock(side_effect=lambda *a, **k: subprocess.CompletedProcess(a, next(codes)))
        with mock.patch("autorestart.time.sleep"):
            self.assertEqual(autorestart.supervise(["x"], run=run), 0)
        self.assertEqual(run.call_count, 3)
        self.assertEqual(run.call_args.kwargs["env"][autorestart.CHILD_ENV], "1")

    def test_a_crash_is_not_restarted(self):
        run = mock.Mock(return_value=subprocess.CompletedProcess([], 1))
        self.assertEqual(autorestart.supervise(["x"], run=run), 1)
        self.assertEqual(run.call_count, 1)

    def test_ctrl_c_ends_it(self):
        run = mock.Mock(side_effect=KeyboardInterrupt)
        self.assertEqual(autorestart.supervise(["x"], run=run), 0)

    def test_the_real_web_ui_files_all_compile(self):
        self.assertEqual(autorestart.broken(autorestart.files()), [])


if __name__ == "__main__":
    unittest.main()
