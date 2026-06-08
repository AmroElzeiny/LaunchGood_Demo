from __future__ import annotations

import unittest

from job_bot.telegram_menu_bot import (
    CALLBACK_MONTHLY_PLAN,
    CALLBACK_QUARTERLY_PLAN,
    CALLBACK_WEEKLY_PLAN,
    CALLBACK_YEARLY_PLAN,
    PLAN_DEFINITIONS,
)


class LegacyTelegramMenuBotPlanTests(unittest.TestCase):
    def test_paid_plan_descriptions_match_plan_metadata(self) -> None:
        for callback_data in (
            CALLBACK_WEEKLY_PLAN,
            CALLBACK_MONTHLY_PLAN,
            CALLBACK_QUARTERLY_PLAN,
        ):
            plan = PLAN_DEFINITIONS[callback_data]
            self.assertIn(f"${plan.amount_usd:.2f}", plan.description)
            self.assertIn(f"{plan.duration_days} days", plan.description)

    def test_monthly_and_quarterly_copy_no_longer_reuses_14_day_text(self) -> None:
        self.assertNotIn("14 days", PLAN_DEFINITIONS[CALLBACK_MONTHLY_PLAN].description)
        self.assertNotIn("14 days", PLAN_DEFINITIONS[CALLBACK_QUARTERLY_PLAN].description)

    def test_legacy_yearly_plan_is_not_offered_as_paid_plan(self) -> None:
        self.assertNotIn(CALLBACK_YEARLY_PLAN, PLAN_DEFINITIONS)


if __name__ == "__main__":
    unittest.main()
