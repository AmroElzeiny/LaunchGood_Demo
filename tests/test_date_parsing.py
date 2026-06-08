from __future__ import annotations

import unittest
from datetime import datetime, timezone

from job_bot.date_parsing import find_date_in_text_to_iso, parse_date_text_to_iso


class RussianDateParsingTests(unittest.TestCase):
    def test_parse_russian_month_name_date(self) -> None:
        parsed = parse_date_text_to_iso("8 апреля 2026", now_utc=datetime(2026, 4, 1, tzinfo=timezone.utc))
        self.assertEqual(parsed, "2026-04-08T00:00:00Z")

    def test_parse_russian_relative_date_phrase(self) -> None:
        parsed = parse_date_text_to_iso(
            "3 дня назад",
            now_utc=datetime(2026, 4, 8, 12, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(parsed, "2026-04-05T12:00:00Z")

    def test_find_russian_date_inside_post_text(self) -> None:
        parsed = find_date_in_text_to_iso(
            "Срок подачи предложений до 12 апреля 2026. Бюджет обсуждается.",
            now_utc=datetime(2026, 4, 1, tzinfo=timezone.utc),
            prefer_future=True,
        )
        self.assertEqual(parsed, "2026-04-12T00:00:00Z")


if __name__ == "__main__":
    unittest.main()
