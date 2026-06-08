from __future__ import annotations

import logging
import unittest
from unittest.mock import patch

from job_bot.models import JobCard
from job_bot.notifier import TelegramNotifier


class _FakeStore:
    def __init__(self) -> None:
        self.subscribed_users: set[int] = set()
        self.delivery_events: list[dict[str, object]] = []

    def is_user_subscribed(self, user_id: int) -> bool:
        return user_id in self.subscribed_users

    def record_delivery_event(
        self,
        *,
        user_id: int,
        username: str,
        card_url: str,
        card_title: str,
        card_company: str,
        card_location: str,
        card_website: str,
        match_reason: str,
    ) -> int:
        self.delivery_events.append(
            {
                "user_id": user_id,
                "username": username,
                "card_url": card_url,
                "card_title": card_title,
                "card_company": card_company,
                "card_location": card_location,
                "card_website": card_website,
                "match_reason": match_reason,
            }
        )
        return len(self.delivery_events)

    def mark_delivery_event_sent(
        self,
        event_id: int,
        *,
        telegram_chat_id: int,
        telegram_message_id: int,
    ) -> None:
        self.delivery_events[event_id - 1]["telegram_chat_id"] = telegram_chat_id
        self.delivery_events[event_id - 1]["telegram_message_id"] = telegram_message_id


class _FakeBot:
    def __init__(self) -> None:
        self.sent_messages: list[tuple[str, str, object | None]] = []

    async def send_message(
        self,
        chat_id: str,
        text: str,
        disable_web_page_preview: bool = False,
        reply_markup: object | None = None,
    ):
        del disable_web_page_preview
        self.sent_messages.append((chat_id, text, reply_markup))
        return type("FakeMessage", (), {"message_id": len(self.sent_messages)})()


def _sample_card() -> JobCard:
    return JobCard(
        website="https://www.bayt.com/en/international/jobs",
        url="https://www.bayt.com/en/egypt/jobs/product-designer-1234567",
        title="Product Designer",
        description="Design flows and improve the UX for a hiring product.",
        salary="USD 2500-3500 per month",
        location="Cairo, Egypt",
        is_job_post=True,
        confidence=0.96,
        extraction_method="test",
        company="Bayt",
    )


def _russian_card() -> JobCard:
    return JobCard(
        website="https://workspace.ru/web-design/",
        url="https://workspace.ru/tenders/redizayn-sayta-123456",
        title="Продуктовый дизайнер",
        description="Проектировать интерфейсы и пользовательские сценарии.",
        salary="200000 RUB",
        location="Москва, Россия",
        is_job_post=True,
        confidence=0.94,
        extraction_method="test",
        company="Яндекс",
        language="ru",
    )


class TelegramNotifierTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_card_uses_buttons_and_keeps_url_out_of_message_text(self) -> None:
        fake_bot = _FakeBot()
        store = _FakeStore()
        with patch("job_bot.notifier.Bot", return_value=fake_bot):
            notifier = TelegramNotifier(
                bot_token="token",
                chat_id="100000001",
                logger=logging.getLogger("test-notifier"),
                store=store,  # type: ignore[arg-type]
            )

        await notifier.send_card(_sample_card())

        self.assertEqual(len(fake_bot.sent_messages), 1)
        chat_id, text, reply_markup = fake_bot.sent_messages[0]
        self.assertEqual(chat_id, "100000001")
        self.assertNotIn("URL:", text)
        self.assertIn("✨ New Project Match", text)
        self.assertIn("💼 Project: Product Designer", text)
        self.assertIn("🌐 Source: bayt.com", text)
        self.assertEqual(len(store.delivery_events), 1)
        self.assertEqual(store.delivery_events[0]["telegram_chat_id"], 100000001)
        self.assertEqual(store.delivery_events[0]["telegram_message_id"], 1)
        self.assertIsNotNone(reply_markup)
        assert reply_markup is not None
        self.assertEqual(reply_markup.inline_keyboard[0][0].text, "Open Project")
        self.assertEqual(
            reply_markup.inline_keyboard[0][0].url,
            "https://www.bayt.com/en/egypt/jobs/product-designer-1234567",
        )
        self.assertEqual(reply_markup.inline_keyboard[1][0].text, "Relevant")
        self.assertEqual(reply_markup.inline_keyboard[1][0].callback_data, "match_up_1")
        self.assertEqual(reply_markup.inline_keyboard[1][1].text, "Not Relevant")
        self.assertEqual(reply_markup.inline_keyboard[1][1].callback_data, "match_dn_1")

    async def test_send_card_skips_legacy_delivery_for_active_subscriber_chat(self) -> None:
        fake_bot = _FakeBot()
        store = _FakeStore()
        store.subscribed_users.add(100000001)
        with patch("job_bot.notifier.Bot", return_value=fake_bot):
            notifier = TelegramNotifier(
                bot_token="token",
                chat_id="100000001",
                logger=logging.getLogger("test-notifier"),
                store=store,  # type: ignore[arg-type]
            )

        await notifier.send_card(_sample_card())

        self.assertEqual(fake_bot.sent_messages, [])
        self.assertEqual(store.delivery_events, [])

    async def test_send_human_review_request_bypasses_subscriber_skip(self) -> None:
        fake_bot = _FakeBot()
        store = _FakeStore()
        store.subscribed_users.add(100000001)
        with patch("job_bot.notifier.Bot", return_value=fake_bot):
            notifier = TelegramNotifier(
                bot_token="token",
                chat_id="100000001",
                logger=logging.getLogger("test-notifier-review"),
                store=store,  # type: ignore[arg-type]
            )

        await notifier.send_human_review_request(
            _sample_card(),
            reason="low_confidence_extraction:0.440<0.620",
            review_id=12,
        )

        self.assertEqual(len(fake_bot.sent_messages), 1)
        chat_id, text, reply_markup = fake_bot.sent_messages[0]
        self.assertEqual(chat_id, "100000001")
        self.assertIn("Project Review Needed", text)
        self.assertIn("Review ID: 12", text)
        self.assertIn("low_confidence_extraction:0.440<0.620", text)
        self.assertIsNotNone(reply_markup)
        self.assertEqual(store.delivery_events, [])

    async def test_send_card_localizes_russian_cards(self) -> None:
        fake_bot = _FakeBot()
        store = _FakeStore()
        with patch("job_bot.notifier.Bot", return_value=fake_bot):
            notifier = TelegramNotifier(
                bot_token="token",
                chat_id="100000001",
                logger=logging.getLogger("test-notifier-ru"),
                store=store,  # type: ignore[arg-type]
            )

        await notifier.send_card(_russian_card())

        _, text, reply_markup = fake_bot.sent_messages[0]
        self.assertIn("✨ Новое совпадение по проекту", text)
        self.assertIn("💼 Проект: Продуктовый дизайнер", text)
        self.assertIn("🌐 Источник: workspace.ru", text)
        assert reply_markup is not None
        self.assertEqual(reply_markup.inline_keyboard[0][0].text, "Открыть проект")
        self.assertEqual(reply_markup.inline_keyboard[1][0].text, "Подходит")
        self.assertEqual(reply_markup.inline_keyboard[1][1].text, "Не подходит")
