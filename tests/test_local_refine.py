import unittest

from chatterbox.local_refine import refine_text


class LocalRefineTests(unittest.TestCase):
    def test_removes_standalone_fillers(self):
        self.assertEqual(refine_text("um so i think, uh, this works"), "So i think, this works.")

    def test_collapses_repeated_words(self):
        self.assertEqual(refine_text("the the quick fox"), "The quick fox.")

    def test_fixes_space_before_punctuation_and_adds_terminal_period(self):
        self.assertEqual(refine_text("hello there ,how are you"), "Hello there, how are you.")

    def test_keeps_existing_terminal_punctuation(self):
        self.assertEqual(refine_text("is this working"), "Is this working.")
        self.assertEqual(refine_text("is this working?"), "Is this working?")

    def test_does_not_strip_a_real_word_containing_filler_letters(self):
        self.assertIn("umbrella", refine_text("bring an umbrella today"))

    def test_rejects_empty_text(self):
        with self.assertRaises(ValueError):
            refine_text("   ")


if __name__ == "__main__":
    unittest.main()
