import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("AGENT_DATA_DIR", tempfile.mkdtemp())

from agent import applier, easy_apply  # noqa: E402


class Loc:
    def __init__(self, page):
        self.page = page
        self.first = self

    def count(self):
        return len(self.page.dialogs)

    def inner_text(self, timeout=None):
        return self.page.dialogs[0]


class Page:
    def __init__(self, first, after):
        self.dialogs = list(first)
        self.after = list(after)

    def goto(self, *a, **k):
        pass

    def wait_for_selector(self, *a, **k):
        pass

    def wait_for_timeout(self, ms):
        if ms == 2000:
            self.dialogs = self.after

    def locator(self, sel):
        return Loc(self)

    def screenshot(self, path):
        Path(path).write_bytes(b"png")


class DialogGone(unittest.TestCase):
    def open(self, page):
        with mock.patch.object(easy_apply, "_top_card_apply_controls", return_value=[mock.Mock()]):
            easy_apply.open_easy_apply(page, "https://x")

    def test_a_dialog_that_closes_at_once_is_reported_with_its_text(self):
        with self.assertRaises(RuntimeError) as cm:
            self.open(Page(["Job search\n  alert  created"], []))
        self.assertIn("closed again within 2 seconds", str(cm.exception))
        self.assertIn("Job search alert created", str(cm.exception))

    def test_a_dialog_that_stays_is_fine(self):
        self.open(Page(["Apply to NET-Group"], ["Apply to NET-Group"]))


class SafetyReminder(unittest.TestCase):
    def page(self):
        page = Page(["Job search safety reminder Research the company Review job post Continue applying"],
                    ["Job search safety reminder Research the company Review job post Continue applying"])
        page.clicked = []

        class Link:
            def click(inner):
                page.clicked.append("Continue applying")
                page.dialogs = ["Apply to Acme Contact info"]
                page.after = page.dialogs
        loc = Loc(page)
        loc.get_by_role = lambda role, name=None, exact=False: Link() if (role, name) == ("link", "Continue applying") else None
        page.locator = lambda sel: loc
        return page

    def test_the_safety_reminder_is_passed_with_continue_applying_only(self):
        page = self.page()
        with mock.patch.object(easy_apply, "_top_card_apply_controls", return_value=[mock.Mock()]):
            easy_apply.open_easy_apply(page, "https://x")
        self.assertEqual(page.clicked, ["Continue applying"])

    def test_review_job_post_is_never_the_review_step(self):
        from agent import translate
        self.assertNotEqual(translate.button_role("Review job post"), "review")
        self.assertEqual(translate.button_role("Review"), "review")
        self.assertEqual(translate.button_role("Review your application"), "review")


class Snapshot(unittest.TestCase):
    def test_saves_a_screenshot_and_the_dialog_text(self):
        folder = Path(tempfile.mkdtemp())
        out = easy_apply.snapshot(Page(["Contact info"], ["Contact info"]), "123_1", folder)
        self.assertTrue((folder / "123_1.png").exists())
        self.assertIn("Contact info", out)

    def test_no_dialog_is_said_plainly(self):
        out = easy_apply.snapshot(Page([], []), "123_2", Path(tempfile.mkdtemp()))
        self.assertIn("no dialog open", out)

    def test_log_screen_logs_and_never_raises(self):
        ops = mock.Mock()
        ops.snapshot.return_value = "screenshot a.png; no dialog open"
        with self.assertLogs(level=logging.ERROR) as cm:
            applier._log_screen(ops, "42")
        self.assertIn("screen at failure: screenshot a.png", cm.output[0])
        ops.snapshot.side_effect = RuntimeError("boom")
        with self.assertLogs(level=logging.ERROR):
            applier._log_screen(ops, "42")


class FormStoppedMessage(unittest.TestCase):
    def test_a_form_that_vanishes_names_the_page_and_step(self):
        step = {"page": (2, 4), "fields": [], "buttons_info": [{"text": "Next", "en": "Next", "role": "next"}],
                "errors": [], "text": "t"}
        calls = {"n": 0}

        def read(page):
            calls["n"] += 1
            if calls["n"] >= 3:                       # the third read: the form has gone
                raise RuntimeError("Locator.evaluate: Timeout 30000ms exceeded.\nCall log:\n  - waiting for locator")
            return step
        page = mock.MagicMock()
        with mock.patch.object(easy_apply, "read_step", side_effect=read), \
                mock.patch.object(easy_apply.resumes, "resume_name", return_value="r.docx"), \
                mock.patch.object(easy_apply.resumes, "local_path", return_value=None), \
                mock.patch.object(easy_apply.answers, "load_bank", return_value=""):
            with self.assertRaises(easy_apply.FormStopped) as cm:
                easy_apply.run_form(page, "Director of AI")
        msg = str(cm.exception)
        self.assertIn("closed or stopped responding after clicking 'Next' on page 2/4", msg)
        self.assertIn("page 2/4: nothing to fill", msg)
        self.assertIn("Timeout 30000ms exceeded", msg)


if __name__ == "__main__":
    unittest.main()
