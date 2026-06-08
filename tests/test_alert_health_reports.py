from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from job_bot.storage import AlertHealthSnapshot
from job_bot.zapcareers_telegram_bot import TelegramMenuBot


class _FakeAlertHealthStore:
    def has_alert_configuration(self, user_id: int) -> bool:
        return user_id == 1

    def get_user_websites(self, user_id: int) -> list[str]:
        assert user_id == 1
        return [
            "https://example.com/jobs",
            "https://blocked.example/jobs",
        ]

    def get_last_inactivity_report_anchor_utc(self, user_id: int) -> str:
        assert user_id == 1
        return ""

    def get_last_inactivity_report_sent_at_utc(self, user_id: int) -> str:
        assert user_id == 1
        return ""

    def count_filter_mismatch_events(self, user_id: int, *, since_utc: str | None = None) -> int:
        assert user_id == 1
        return 4 if since_utc else 10

    def get_filter_mismatch_stage_counts(self, user_id: int, *, since_utc: str | None = None) -> list[tuple[str, int]]:
        assert user_id == 1
        return [("location", 3), ("role", 1)]

    def count_delivery_events(self, user_id: int, *, since_utc: str | None = None) -> int:
        assert user_id == 1
        return 1 if since_utc else 5

    def get_role_preference(self, user_id: int) -> str:
        assert user_id == 1
        return "UX/UI Designer"

    def get_location_preference(self, user_id: int) -> str:
        assert user_id == 1
        return "Remote within Egypt"

    def get_include_no_salary(self, user_id: int) -> bool:
        assert user_id == 1
        return True

    def get_user_keywords(self, user_id: int) -> list[str]:
        assert user_id == 1
        return ["figma", "saas"]

    def get_latest_delivery_event_at_utc(self, user_id: int) -> str:
        assert user_id == 1
        return "2026-04-04T08:00:00Z"

    def get_alert_updated_at_utc(self, user_id: int) -> str:
        assert user_id == 1
        return "2026-04-04T07:55:00Z"

    def get_latest_filter_change_at_utc(self, user_id: int) -> str:
        assert user_id == 1
        return "2026-04-04T07:58:00Z"

    def get_active_subscription(self, user_id: int):
        assert user_id == 1
        return SimpleNamespace(started_at_utc="2026-04-04T07:00:00Z")

    def get_notification_preference(self, user_id: int) -> bool:
        assert user_id == 1
        return True

    def get_ui_language(self, user_id: int) -> str:
        assert user_id == 1
        return "en"


class _FakeAlertHealthStateStore:
    def summarize_alert_health(self, websites: list[str], *, since_utc: str) -> AlertHealthSnapshot:
        assert websites == [
            "https://example.com/jobs",
            "https://blocked.example/jobs",
        ]
        assert since_utc == "2026-04-04T08:00:00Z"
        return AlertHealthSnapshot(
            last_scan_utc="2026-04-04T10:20:00Z",
            discovered_posts=12,
            blocked_sources=1,
            blocked_source_urls=("https://blocked.example/jobs",),
            duplicate_prevented=3,
            freshness_rejects=5,
            rejected_posts=8,
        )


class _FakeFilterAI:
    async def explain_alert_health(self, *, payload: dict[str, object]) -> str:
        assert payload["discovered_posts"] == 12
        assert payload["blocked_sources"] == 1
        return "The bot is scanning normally, but your location filter is rejecting many of the opportunities it finds."


class AlertHealthReportTests(unittest.IsolatedAsyncioTestCase):
    async def test_build_alert_health_report_includes_counts_links_and_explanation(self) -> None:
        bot = TelegramMenuBot.__new__(TelegramMenuBot)
        bot.store = _FakeAlertHealthStore()
        bot.state_store = _FakeAlertHealthStateStore()
        bot.filter_ai = _FakeFilterAI()
        bot.settings = SimpleNamespace(no_posts_report_minutes=60)
        bot.logger = SimpleNamespace(warning=lambda *args, **kwargs: None)

        report = await bot._build_alert_health_report(
            1,
            now_utc=TelegramMenuBot._parse_utc_iso("2026-04-04T10:30:00Z"),
            enforce_threshold=True,
        )

        self.assertIsNotNone(report)
        assert report is not None
        self.assertEqual(report.anchor_utc, "2026-04-04T08:00:00Z")
        self.assertIn("The last scan was 10 minutes ago.", report.text)
        self.assertIn("• Matches discovered: 12", report.text)
        self.assertIn("• Blocked sources: 1", report.text)
        self.assertIn("• Stale or closed matches: 5", report.text)
        self.assertIn("• Project filter mismatches for your alert: 4", report.text)
        self.assertIn("1. https://example.com/jobs", report.text)
        self.assertIn("2. https://blocked.example/jobs", report.text)
        self.assertIn("location 3, project scope 1", report.text)
        self.assertIn("Duplicate prevented: 3", report.text)
        self.assertIn("AI Analysis:", report.text)
        self.assertIn("your location filter is rejecting many of the opportunities it finds", report.text)

    async def test_build_alert_health_report_repeats_after_interval_in_same_quiet_period(self) -> None:
        class _RepeatingStore(_FakeAlertHealthStore):
            def get_last_inactivity_report_anchor_utc(self, user_id: int) -> str:
                assert user_id == 1
                return "2026-04-04T08:00:00Z"

            def get_last_inactivity_report_sent_at_utc(self, user_id: int) -> str:
                assert user_id == 1
                return "2026-04-04T09:00:00Z"

        bot = TelegramMenuBot.__new__(TelegramMenuBot)
        bot.store = _RepeatingStore()
        bot.state_store = _FakeAlertHealthStateStore()
        bot.filter_ai = _FakeFilterAI()
        bot.settings = SimpleNamespace(no_posts_report_minutes=60)
        bot.logger = SimpleNamespace(warning=lambda *args, **kwargs: None)

        report = await bot._build_alert_health_report(
            1,
            now_utc=TelegramMenuBot._parse_utc_iso("2026-04-04T10:30:00Z"),
            enforce_threshold=True,
        )

        self.assertIsNotNone(report)

    async def test_build_alert_health_report_localizes_wrapper_text_for_russian_ui(self) -> None:
        class _RussianStore(_FakeAlertHealthStore):
            def get_ui_language(self, user_id: int) -> str:
                assert user_id == 1
                return "ru"

        class _RussianFilterAI:
            async def explain_alert_health(self, *, payload: dict[str, object]) -> str:
                assert payload["ui_language"] == "ru"
                return "Система работает нормально, но фильтр по месту работы сейчас отсекает многие проекты."

        bot = TelegramMenuBot.__new__(TelegramMenuBot)
        bot.store = _RussianStore()
        bot.state_store = _FakeAlertHealthStateStore()
        bot.filter_ai = _RussianFilterAI()
        bot.settings = SimpleNamespace(no_posts_report_minutes=60)
        bot.logger = SimpleNamespace(warning=lambda *args, **kwargs: None)

        report = await bot._build_alert_health_report(
            1,
            now_utc=TelegramMenuBot._parse_utc_iso("2026-04-04T10:30:00Z"),
            enforce_threshold=True,
        )

        self.assertIsNotNone(report)
        assert report is not None
        self.assertIn("Почему пока нет подходящих проектов", report.text)
        self.assertIn("Что происходит сейчас:", report.text)
        self.assertIn("Краткое пояснение:", report.text)
        self.assertIn("Система работает нормально", report.text)

    async def test_build_alert_health_report_skips_repeat_before_interval(self) -> None:
        class _RecentlySentStore(_FakeAlertHealthStore):
            def get_last_inactivity_report_anchor_utc(self, user_id: int) -> str:
                assert user_id == 1
                return "2026-04-04T08:00:00Z"

            def get_last_inactivity_report_sent_at_utc(self, user_id: int) -> str:
                assert user_id == 1
                return "2026-04-04T10:00:00Z"

        bot = TelegramMenuBot.__new__(TelegramMenuBot)
        bot.store = _RecentlySentStore()
        bot.state_store = _FakeAlertHealthStateStore()
        bot.filter_ai = _FakeFilterAI()
        bot.settings = SimpleNamespace(no_posts_report_minutes=60)
        bot.logger = SimpleNamespace(warning=lambda *args, **kwargs: None)

        report = await bot._build_alert_health_report(
            1,
            now_utc=TelegramMenuBot._parse_utc_iso("2026-04-04T10:30:00Z"),
            enforce_threshold=True,
        )

        self.assertIsNone(report)

    def test_my_alert_markup_includes_manual_alert_health_button(self) -> None:
        bot = TelegramMenuBot.__new__(TelegramMenuBot)
        bot.store = _FakeAlertHealthStore()

        markup = bot._my_alert_markup(1)
        labels = [row[0].text for row in markup.inline_keyboard]

        self.assertIn("Why am I not receiving matches?", labels)

    async def test_manual_alert_health_report_disables_web_preview(self) -> None:
        bot = TelegramMenuBot.__new__(TelegramMenuBot)
        bot.store = _FakeAlertHealthStore()
        bot.state_store = _FakeAlertHealthStateStore()
        bot.filter_ai = _FakeFilterAI()
        bot.settings = SimpleNamespace(no_posts_report_minutes=60)
        bot.logger = SimpleNamespace(warning=lambda *args, **kwargs: None)

        captured: list[bool] = []

        async def fake_send_and_log(*, context, chat_id, user_id, username, text, reply_markup=None, disable_web_page_preview=False) -> None:
            del context, chat_id, user_id, username, text, reply_markup
            captured.append(disable_web_page_preview)

        bot._send_and_log = fake_send_and_log

        await bot._show_alert_health_report(
            context=SimpleNamespace(bot=SimpleNamespace()),
            chat_id=10,
            user_id=1,
            username="alice",
            mark_as_sent=False,
        )

        self.assertEqual(captured, [True])

    async def test_background_alert_health_report_disables_web_preview(self) -> None:
        class _BackgroundStore(_FakeAlertHealthStore):
            def get_active_subscribers(self) -> list[tuple[int, str]]:
                return [(1, "alice")]

            def mark_inactivity_report_sent(self, user_id: int, *, anchor_utc: str) -> None:
                self.marked = (user_id, anchor_utc)

        bot = TelegramMenuBot.__new__(TelegramMenuBot)
        bot.store = _BackgroundStore()
        bot.state_store = _FakeAlertHealthStateStore()
        bot.filter_ai = _FakeFilterAI()
        bot.settings = SimpleNamespace(no_posts_report_minutes=60)
        bot.logger = SimpleNamespace(warning=lambda *args, **kwargs: None)

        captured: list[bool] = []

        async def fake_send_to_user_and_log(*, user_id, username, text, reply_markup=None, disable_web_page_preview=False) -> None:
            del user_id, username, text, reply_markup
            captured.append(disable_web_page_preview)

        bot._send_to_user_and_log = fake_send_to_user_and_log

        with patch(
            "job_bot.zapcareers_telegram_bot.utc_now_dt",
            return_value=TelegramMenuBot._parse_utc_iso("2026-04-04T10:30:00Z"),
        ):
            await bot._send_no_posts_reports()

        self.assertEqual(captured, [True])


if __name__ == "__main__":
    unittest.main()
