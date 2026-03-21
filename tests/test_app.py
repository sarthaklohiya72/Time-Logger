import unittest
from datetime import date

import pandas as pd

from app import get_matrix_stats, get_period_range, parse_date_param, parse_period_param


class MatrixStatsTests(unittest.TestCase):
    def test_empty_dataframe_returns_zero_stats(self):
        df = pd.DataFrame()
        stats = get_matrix_stats(df)
        self.assertEqual(stats["total_hours"], 0.0)
        self.assertEqual(stats["q1"]["hours"], 0.0)
        self.assertEqual(stats["q2"]["pct"], 0.0)

    def test_non_empty_dataframe_computes_quadrants(self):
        df = pd.DataFrame(
            [
                {"duration": 60, "urgent": True, "important": True},
                {"duration": 30, "urgent": False, "important": True},
                {"duration": 30, "urgent": True, "important": False},
                {"duration": 60, "urgent": False, "important": False},
            ]
        )
        stats = get_matrix_stats(df)
        self.assertAlmostEqual(stats["total_hours"], 3.0)
        self.assertAlmostEqual(stats["q1"]["hours"], 1.0)
        self.assertAlmostEqual(stats["q2"]["hours"], 0.5)
        self.assertAlmostEqual(stats["q3"]["hours"], 0.5)
        self.assertAlmostEqual(stats["q4"]["hours"], 1.0)


class PeriodHelpersTests(unittest.TestCase):
    def test_parse_period_param_defaults_invalid_to_day(self):
        self.assertEqual(parse_period_param("invalid"), "day")
        self.assertEqual(parse_period_param(None), "day")

    def test_parse_period_param_valid_values(self):
        self.assertEqual(parse_period_param("day"), "day")
        self.assertEqual(parse_period_param("week"), "week")
        self.assertEqual(parse_period_param("month"), "month")

    def test_parse_date_param_valid_and_invalid(self):
        d = parse_date_param("2024-01-01")
        self.assertEqual(d, date(2024, 1, 1))
        today = parse_date_param("invalid-date")
        self.assertIsInstance(today, date)

    def test_get_period_range_day(self):
        d = date(2024, 1, 10)
        start, end = get_period_range(d, "day")
        self.assertEqual(start, d)
        self.assertEqual(end, d)

    def test_get_period_range_week(self):
        d = date(2024, 1, 10)
        start, end = get_period_range(d, "week")
        self.assertLessEqual(start, d)
        self.assertGreaterEqual(end, d)
        self.assertEqual((end - start).days, 6)

    def test_get_period_range_month(self):
        d = date(2024, 1, 10)
        start, end = get_period_range(d, "month")
        self.assertEqual(start.day, 1)
        self.assertGreaterEqual(end.day, 28)
        self.assertEqual(start.month, end.month)


if __name__ == "__main__":
    unittest.main()

