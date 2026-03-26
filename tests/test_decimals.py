import unittest
from time_tracker_pro.core.time_utils import human_hours

class DecimalHandlingTests(unittest.TestCase):
    def test_human_hours_simple(self):
        self.assertEqual(human_hours(0.5), "30 minutes")
        self.assertEqual(human_hours(1.0), "1 hours")
        self.assertEqual(human_hours(1.25), "1 hours 15 minutes")

    def test_human_hours_rounding(self):
        # Avoid float drift like 31.5099999999 by rounding minutes first
        self.assertEqual(human_hours(31.51), "31 hours 31 minutes")
        self.assertEqual(human_hours(5.26), "5 hours 16 minutes")

if __name__ == "__main__":
    unittest.main()
