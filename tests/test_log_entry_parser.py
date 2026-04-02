import unittest
from datetime import datetime
from itertools import product

from time_tracker_pro.services.parser import TimeLogParser


class LogEntryParserRulesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = TimeLogParser()
        self.now = datetime(2026, 1, 21, 10, 0)
        self.now_str = "2026-01-21 10:00"

    def parse(self, entry: str, previous_end: datetime | None = None) -> dict:
        return self.parser.parse_row(entry, self.now_str, previous_end)

    def parse_with_now(self, entry: str, now_str: str, previous_end: datetime | None = None) -> dict:
        return self.parser.parse_row(entry, now_str, previous_end)

    def test_zero_elements_uses_previous_end(self) -> None:
        previous_end = datetime(2026, 1, 21, 8, 0)
        parsed = self.parse("Task only", previous_end)
        self.assertEqual(parsed["start_dt"], previous_end)
        self.assertEqual(parsed["end_dt"], self.now)

    def test_one_time_with_dot(self) -> None:
        parsed = self.parse("9. Task")
        self.assertEqual(parsed["start_dt"], datetime(2026, 1, 21, 9, 0))
        self.assertEqual(parsed["end_dt"], self.now)

    def test_one_time_with_separate_pm_token(self) -> None:
        parsed = self.parse_with_now("7:50 pm sleep", "2026-01-20 19:58")
        self.assertEqual(parsed["start_dt"], datetime(2026, 1, 20, 19, 50))
        self.assertEqual(parsed["end_dt"], datetime(2026, 1, 20, 19, 50))

    def test_one_time_without_dot_uses_previous_end(self) -> None:
        previous_end = datetime(2026, 1, 21, 8, 0)
        parsed = self.parse("9 Task", previous_end)
        self.assertEqual(parsed["start_dt"], previous_end)
        self.assertEqual(parsed["end_dt"], datetime(2026, 1, 21, 9, 0))

    def test_first_row_without_previous_end_sets_start_date(self) -> None:
        parsed = self.parse("9 Task")
        self.assertEqual(parsed["start_dt"], datetime(2026, 1, 21, 9, 0))
        self.assertEqual(parsed["end_dt"], datetime(2026, 1, 21, 9, 0))

    def test_two_elements_time_date_with_dot(self) -> None:
        previous_end = datetime(2026, 1, 20, 8, 0)
        parsed = self.parse("9 20/01. Task", previous_end)
        self.assertEqual(parsed["start_dt"], datetime(2026, 1, 20, 9, 0))
        self.assertEqual(parsed["end_dt"], self.now)

    def test_two_elements_time_date_without_dot(self) -> None:
        previous_end = datetime(2026, 1, 20, 7, 0)
        parsed = self.parse("9 20/01 Task", previous_end)
        self.assertEqual(parsed["start_dt"], previous_end)
        self.assertEqual(parsed["end_dt"], datetime(2026, 1, 20, 9, 0))

    def test_two_elements_two_times_wraps_day(self) -> None:
        parsed = self.parse("23 1 Task")
        self.assertEqual(parsed["start_dt"], datetime(2026, 1, 20, 23, 0))
        self.assertEqual(parsed["end_dt"], datetime(2026, 1, 21, 1, 0))

    def test_two_elements_two_times_same_day(self) -> None:
        parsed = self.parse("9 11 Task")
        self.assertEqual(parsed["start_dt"], datetime(2026, 1, 21, 9, 0))
        self.assertEqual(parsed["end_dt"], datetime(2026, 1, 21, 11, 0))

    def test_one_time_midnight_crossing_uses_previous_day(self) -> None:
        previous_end = datetime(2026, 1, 13, 21, 51)
        parsed = self.parse_with_now(
            "11:30 pm friends time",
            "2026-01-14 01:02",
            previous_end=previous_end,
        )
        self.assertEqual(parsed["start_dt"], previous_end)
        self.assertEqual(parsed["end_dt"], datetime(2026, 1, 13, 23, 30))

    def test_three_elements_date_times_with_dot_after_date(self) -> None:
        parsed = self.parse("20/01. 9 11 Task")
        self.assertEqual(parsed["start_dt"], datetime(2026, 1, 20, 9, 0))
        self.assertEqual(parsed["end_dt"], datetime(2026, 1, 21, 11, 0))

    def test_three_elements_date_times_wraps_day(self) -> None:
        parsed = self.parse("20/01 23 1 Task")
        self.assertEqual(parsed["start_dt"], datetime(2026, 1, 20, 23, 0))
        self.assertEqual(parsed["end_dt"], datetime(2026, 1, 21, 1, 0))

    def test_four_elements_dates_and_times(self) -> None:
        parsed = self.parse("20/01 9 21/01 10 Task")
        self.assertEqual(parsed["start_dt"], datetime(2026, 1, 20, 9, 0))
        self.assertEqual(parsed["end_dt"], datetime(2026, 1, 21, 10, 0))

    def test_screenshot_scenario_sleep_then_instagram(self) -> None:
        first = self.parse_with_now(
            "7:50 pm sleep . Necessity Urgent",
            "2026-01-20 19:58",
            previous_end=datetime(2026, 1, 20, 19, 0),
        )
        self.assertEqual(first["start_dt"], datetime(2026, 1, 20, 19, 0))
        self.assertEqual(first["end_dt"], datetime(2026, 1, 20, 19, 50))

        second = self.parse_with_now(
            "Instagram .",
            "2026-01-20 20:27",
            previous_end=first["end_dt"],
        )
        self.assertEqual(second["start_dt"], datetime(2026, 1, 20, 19, 50))
        self.assertEqual(second["end_dt"], datetime(2026, 1, 20, 20, 27))

    def test_generated_permutations_do_not_crash(self) -> None:
        time_seqs = [
            ("9",),
            ("09",),
            ("9:00",),
            ("11:15",),
            ("9am",),
            ("9", "am"),
            ("7:50", "pm"),
            ("12", "am"),
            ("12", "pm"),
            ("23",),
        ]
        date_tokens = [
            "20/01",
            "20-01",
            "20.01",
            "20/01/26",
            "2026-01-20",
            "Jan",
        ]

        def as_text(tokens: tuple[str, ...]) -> str:
            return " ".join(tokens)

        def with_trailing_dot(tokens: tuple[str, ...]) -> tuple[str, ...]:
            if not tokens:
                return tokens
            return (*tokens[:-1], f"{tokens[-1]}.")

        def with_trailing_comma(tokens: tuple[str, ...]) -> tuple[str, ...]:
            if not tokens:
                return tokens
            return (*tokens[:-1], f"{tokens[-1]},")

        base_text = ("Task", ".", "Work", "Urgent", "Important")

        cases: list[str] = []

        cases.append(as_text(base_text))

        for t in time_seqs:
            cases.append(as_text((*t, *base_text)))
            cases.append(as_text((*with_trailing_dot(t), *base_text)))
            cases.append(as_text((*t, ".", *base_text)))
            cases.append(as_text((*with_trailing_comma(t), *base_text)))

        for t, d in product(time_seqs, date_tokens):
            td = (*t, d)
            cases.append(as_text((*td, *base_text)))
            cases.append(as_text((*with_trailing_dot(td), *base_text)))
            cases.append(as_text((*td, ".", *base_text)))

        for t1, t2 in product(time_seqs, time_seqs):
            cases.append(as_text((*t1, *t2, *base_text)))

        for d, t1, t2 in product(date_tokens, time_seqs, time_seqs):
            cases.append(as_text((d + ".", *t1, *t2, *base_text)))
            cases.append(as_text((d, ".", *t1, *t2, *base_text)))
            cases.append(as_text((d, *t1, *t2, *base_text)))

        for d1, t1, d2, t2 in product(date_tokens, time_seqs, date_tokens, time_seqs):
            cases.append(as_text((d1, *t1, d2, *t2, *base_text)))

        max_cases = 5000
        tested = 0
        for entry in cases:
            if tested >= max_cases:
                break
            with self.subTest(entry=entry):
                parsed = self.parse(entry)
                self.assertIn("start_dt", parsed)
                self.assertIn("end_dt", parsed)
                self.assertIsInstance(parsed["start_dt"], datetime)
                self.assertIsInstance(parsed["end_dt"], datetime)
            tested += 1


if __name__ == "__main__":
    unittest.main()
