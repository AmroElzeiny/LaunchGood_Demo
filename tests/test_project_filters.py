from __future__ import annotations

import unittest

from job_bot.project_filters import (
    format_project_filter_value,
    parse_project_preferences_text,
    project_filter_example,
    project_filter_label,
)


class ProjectFilterParsingTests(unittest.TestCase):
    def test_parse_russian_free_text_project_filters(self) -> None:
        parsed = parse_project_preferences_text(
            "\n".join(
                [
                    "скрытый бюджет: да",
                    "минимальная оплата usd: 1200",
                    "максимальная оплата usd: 5000",
                    "минимальная оплата руб: 120000",
                    "максимальная оплата руб: 350000",
                    "срок: до 2 недель",
                    "срочно: да",
                    "ретейнер: нет",
                    "консалтинг: да",
                    "команда / агентство: нет",
                ]
            )
        )

        self.assertTrue(parsed["allow_hidden_budget"])
        self.assertEqual(parsed["minimum_payment_usd"], 1200.0)
        self.assertEqual(parsed["maximum_payment_usd"], 5000.0)
        self.assertEqual(parsed["minimum_payment_rub"], 120000.0)
        self.assertEqual(parsed["maximum_payment_rub"], 350000.0)
        self.assertEqual(parsed["duration_preferences"], ["до 2 недель"])
        self.assertTrue(parsed["urgent_only"])
        self.assertFalse(parsed["retainer_ok"])
        self.assertTrue(parsed["consulting_ok"])
        self.assertFalse(parsed["team_or_agency_ok"])

    def test_future_filter_labels_and_examples_are_localized_for_russian(self) -> None:
        fields = (
            "engagement_types",
            "payment_types",
            "client_types",
            "proposal_style",
            "timezone_preferences",
            "language_requirements",
            "recurring_preferences",
        )

        for field_name in fields:
            self.assertTrue(project_filter_label("ru", field_name))
            self.assertTrue(project_filter_example("ru", field_name))
            self.assertNotEqual(project_filter_label("ru", field_name), project_filter_label("en", field_name))

    def test_numeric_filter_formatting_uses_valid_grouped_currency_output(self) -> None:
        self.assertEqual(format_project_filter_value("en", "minimum_payment_usd", 1500), "$1,500")
        self.assertEqual(format_project_filter_value("en", "maximum_payment_usd", 3500), "$3,500")
        self.assertEqual(format_project_filter_value("ru", "minimum_payment_rub", 150000), "₽150,000")
        self.assertEqual(format_project_filter_value("ru", "maximum_payment_rub", 350000), "₽350,000")


if __name__ == "__main__":
    unittest.main()
