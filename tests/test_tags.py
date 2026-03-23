import unittest
from app import normalize_tag

class TagNormalizationTests(unittest.TestCase):
    def test_trailing_punctuation_removed(self):
        self.assertEqual(normalize_tag("Work."), "Work")
        self.assertEqual(normalize_tag("General,"), "General")
        self.assertEqual(normalize_tag("Necessity!"), "Necessity")

    def test_whitespace_collapsed_and_titlecased(self):
        self.assertEqual(normalize_tag("  work   "), "Work")
        self.assertEqual(normalize_tag("wOrKoUt"), "Workout")

    def test_empty_defaults_to_empty_string(self):
        self.assertEqual(normalize_tag(""), "")
        self.assertEqual(normalize_tag(None), "")

if __name__ == "__main__":
    unittest.main()
