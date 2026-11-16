import os
import tempfile
import unittest
from types import SimpleNamespace

os.environ.setdefault("AGENT_DATA_DIR", tempfile.mkdtemp())

from agent import answers  # noqa: E402

Q1 = "Have you led an enterprise-wide data and AI strategy for a company with annual revenues exceeding $5bn?"
Q3 = "Have you taken a look through our website/profile, and are you particularly interested in joining us to bring our vision to life?"
BANK = (
    "Always Yes\nAlways No\n\n" + answers.LEARNED_HEADING + "\n"
    f"- {Q1} → Yes\n"
    f"- {Q3} → Yes\n"
    "- How many years of Python experience? → 0\n"
    "- Where did you study? → St. Xavier's School\n"
)
BOOM = SimpleNamespace(messages=SimpleNamespace(create=lambda **k: 1 / 0))   # any model call fails the test


class ExactAnswers(unittest.TestCase):
    def test_matches_the_whole_question_ignoring_case_and_punctuation(self):
        self.assertEqual(answers.exact_answer(Q1, BANK), "Yes")
        self.assertEqual(answers.exact_answer(Q1.upper().rstrip("?"), BANK), "Yes")
        self.assertEqual(answers.exact_answer("  " + Q3 + " ", BANK), "Yes")

    def test_never_matches_part_of_a_question_or_a_similar_one(self):
        self.assertIsNone(answers.exact_answer("Have you led an enterprise-wide data and AI strategy?", BANK))
        self.assertIsNone(answers.exact_answer(Q1.replace("$5bn", "$50bn"), BANK))
        self.assertIsNone(answers.exact_answer(Q1 + " Please explain.", BANK))
        self.assertIsNone(answers.exact_answer("", BANK))
        self.assertIsNone(answers.exact_answer("Always Yes", BANK))     # only 'question -> answer' lines count

    def decide(self, label, options=None, bank=BANK):
        return answers.decide_answer({"label": label, "type": "radio", "required": True, "options": options or []},
                                     bank=bank, client=BOOM)

    def test_a_saved_answer_is_used_without_asking_the_model(self):
        r = self.decide(Q1, ["Yes", "No"])
        self.assertEqual((r["action"], r["value"]), ("answer", "Yes"))
        self.assertIn("exact question", r["reason"])

    def test_the_saved_answer_must_be_one_of_the_forms_options(self):
        r = self.decide(Q1, ["Maybe", "Never"])
        self.assertEqual(r["action"], "pause")
        self.assertIn("not one of the form's options", r["reason"])

    def test_a_saved_answer_still_has_to_pass_the_safety_rules(self):
        self.assertEqual(self.decide("How many years of Python experience?")["action"], "pause")   # never 0 years
        self.assertEqual(self.decide("Where did you study?")["action"], "pause")                   # never that school

    def test_hard_blockers_and_optional_questions_still_come_first(self):
        bank = BANK + "- What is your risk appetite? → High\n"
        r = answers.decide_answer({"label": "What is your risk appetite?", "type": "text", "required": True},
                                  bank=bank, client=BOOM)
        self.assertEqual(r["action"], "skip_application")
        r = answers.decide_answer({"label": Q1, "type": "radio", "required": False}, bank=BANK, client=BOOM)
        self.assertEqual(r["action"], "blank")

    def test_a_different_question_still_goes_to_the_model_path(self):
        other = SimpleNamespace(messages=SimpleNamespace(create=lambda **k: SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text='{"action": "pause", "value": null, "reason": "no basis"}')])))
        r = answers.decide_answer({"label": "Do you hold an AWS certification?", "type": "radio", "required": True,
                                   "options": ["Yes", "No"]}, bank=BANK, client=other)
        self.assertEqual(r["action"], "pause")

    def test_the_three_answers_are_really_saved_in_the_standing_answers(self):
        real = answers.load_bank()
        for q in (Q1, "Have you led enterprise healthcare data integration involving HL7, FHIR, EHR, or EMR platforms?", Q3):
            self.assertEqual(answers.exact_answer(q, real), "Yes", q)


if __name__ == "__main__":
    unittest.main()
