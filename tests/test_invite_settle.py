import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("AGENT_DATA_DIR", tempfile.mkdtemp())

from agent import invite  # noqa: E402


class Dlg:
    def count(self):
        return 0


class Settle(unittest.TestCase):
    """The tail of send_invite: LinkedIn can take a while to show Pending, so it keeps looking."""

    def settle(self, pending_after):
        calls = {"n": 0}

        def state(page):
            calls["n"] += 1
            return {"pending": calls["n"] > pending_after, "degree": "3rd", "title": "x", "more_buttons": 1}
        page = mock.MagicMock()
        page.evaluate.return_value = {"controls": ["More"]}
        with mock.patch.object(invite, "card_state", side_effect=state), mock.patch.object(invite, "_dlg", return_value=Dlg()):
            return invite._settle(page, navigate=True)

    def test_pending_that_shows_up_late_is_still_confirmed(self):
        self.assertEqual(self.settle(pending_after=3)["result"], "sent")

    def test_never_pending_is_unsettled_and_says_what_the_page_showed(self):
        r = self.settle(pending_after=999)
        self.assertEqual(r["result"], "unsettled")
        self.assertIn("The page showed: no dialog", r["reason"])


if __name__ == "__main__":
    unittest.main()
