from __future__ import annotations

import logging
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from job_bot.subscription_admin_notifier import (
    DEFAULT_SUBSCRIPTION_ADMIN_CHAT_ID,
    SubscriptionAdminNotifier,
)
from job_bot.telegram_subscription_store import ActiveSubscription, SubscriptionStore
from job_bot.zapcareers_telegram_bot import (
    CALLBACK_BACK_TO_MAIN,
    CALLBACK_EDIT_SOURCES,
    CALLBACK_MONTHLY_PLAN,
    CALLBACK_WEBSITE_CUSTOM,
    PLAN_DEFINITIONS,
    TelegramMenuBot,
)


class _FakeUserMessageLogger:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    def append_event(
        self,
        *,
        user_id: int,
        username: str,
        direction: str,
        text: str,
        event_type: str,
        created_at_utc: str,
    ) -> None:
        self.events.append(
            {
                "user_id": user_id,
                "username": username,
                "direction": direction,
                "text": text,
                "event_type": event_type,
                "created_at_utc": created_at_utc,
            }
        )


class _FakeBot:
    def __init__(self) -> None:
        self.sent_messages: list[tuple[int, str, object | None]] = []

    async def send_message(self, *, chat_id: int, text: str, reply_markup=None) -> None:
        self.sent_messages.append((chat_id, text, reply_markup))


class _FlakyBot(_FakeBot):
    def __init__(self, fail_once_chat_ids: set[int]) -> None:
        super().__init__()
        self.fail_once_chat_ids = set(fail_once_chat_ids)

    async def send_message(self, *, chat_id: int, text: str, reply_markup=None) -> None:
        if chat_id in self.fail_once_chat_ids:
            self.fail_once_chat_ids.remove(chat_id)
            raise RuntimeError(f"temporary send failure for {chat_id}")
        await super().send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)


class _FakeNowPayments:
    async def get_payment_status(self, provider_payment_id: str) -> str:
        return "finished"


class _FakeSubscriberNotifier:
    def __init__(self) -> None:
        self.flush_calls: list[int] = []

    async def flush_due_notifications(self, limit: int = 100) -> None:
        self.flush_calls.append(limit)


class SubscriptionAdminNotificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_handle_start_sends_first_start_admin_notification_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SubscriptionStore(Path(temp_dir) / "telegram_subscriptions.db")
            try:
                user_bot = _FakeBot()
                admin_bot = _FakeBot()
                bot = TelegramMenuBot.__new__(TelegramMenuBot)
                bot.store = store
                bot.user_message_logger = _FakeUserMessageLogger()
                bot.logger = logging.getLogger("test-first-start-admin-notification")
                bot.subscription_admin_notifier = SubscriptionAdminNotifier(
                    bot=admin_bot,
                    logger=bot.logger,
                    admin_chat_ids=[100000001, 100000002],
                )
                bot.pending_inputs = {}
                bot.pending_location_candidates = {}
                bot.pending_location_modes = {}
                bot.pending_location_selections = {}
                bot.pending_keyword_reviews = {}
                bot.pending_project_filter_fields = {}
                bot.pending_website_choices = {}
                bot.pending_custom_website_urls = {}
                bot.pending_custom_website_currency = {}
                bot.pending_match_feedback_event_ids = {}
                bot.website_picker_cache = {}
                bot.flow_origin = {}
                bot.flow_step = {}
                update = SimpleNamespace(
                    effective_user=SimpleNamespace(
                        id=701,
                        username="alice",
                        full_name="Alice Example",
                    ),
                    effective_chat=SimpleNamespace(id=701),
                )
                context = SimpleNamespace(bot=user_bot)

                await bot.handle_start(update, context)
                await bot.handle_start(update, context)

                self.assertEqual([chat_id for chat_id, _, _ in admin_bot.sent_messages], [100000001, 100000002])
                self.assertTrue(store.has_first_start_admin_notified(701))
                self.assertIn("User started the bot for the first time", admin_bot.sent_messages[0][1])
                self.assertIn("User ID: 701", admin_bot.sent_messages[0][1])
                self.assertIn("Username: @alice", admin_bot.sent_messages[0][1])
            finally:
                store.close()

    async def test_notifier_deduplicates_the_same_activation_window(self) -> None:
        admin_bot = _FakeBot()
        notifier = SubscriptionAdminNotifier(bot=admin_bot, logger=logging.getLogger("test-admin-dedupe"))
        active_sub = ActiveSubscription(
            user_id=77,
            username="alice",
            plan="monthly",
            started_at_utc="2026-03-27T12:00:00Z",
            ends_at_utc="2026-04-26T12:00:00Z",
            is_active=True,
            source="payment",
            updated_at_utc="2026-03-27T12:00:00Z",
        )

        await notifier.notify_subscription_event(active_sub)
        await notifier.notify_subscription_event(active_sub)

        self.assertEqual(len(admin_bot.sent_messages), 1)
        self.assertEqual(admin_bot.sent_messages[0][0], DEFAULT_SUBSCRIPTION_ADMIN_CHAT_ID)

    async def test_notifier_sends_to_each_configured_admin_chat_once(self) -> None:
        admin_bot = _FakeBot()
        notifier = SubscriptionAdminNotifier(
            bot=admin_bot,
            logger=logging.getLogger("test-admin-multi"),
            admin_chat_ids=[100000001, 100000002],
        )
        active_sub = ActiveSubscription(
            user_id=91,
            username="rima",
            plan="monthly",
            started_at_utc="2026-03-27T12:00:00Z",
            ends_at_utc="2026-04-26T12:00:00Z",
            is_active=True,
            source="payment",
            updated_at_utc="2026-03-27T12:00:00Z",
        )

        await notifier.notify_subscription_event(active_sub)
        await notifier.notify_subscription_event(active_sub)

        self.assertEqual([chat_id for chat_id, _, _ in admin_bot.sent_messages], [100000001, 100000002])

    async def test_notifier_retries_only_failed_admin_recipients(self) -> None:
        admin_bot = _FlakyBot({100000002})
        notifier = SubscriptionAdminNotifier(
            bot=admin_bot,
            logger=logging.getLogger("test-admin-partial-retry"),
            admin_chat_ids=[100000001, 100000002],
        )
        active_sub = ActiveSubscription(
            user_id=92,
            username="laila",
            plan="monthly",
            started_at_utc="2026-03-27T12:00:00Z",
            ends_at_utc="2026-04-26T12:00:00Z",
            is_active=True,
            source="payment",
            updated_at_utc="2026-03-27T12:00:00Z",
        )

        await notifier.notify_subscription_event(active_sub)
        await notifier.notify_subscription_event(active_sub)

        self.assertEqual([chat_id for chat_id, _, _ in admin_bot.sent_messages], [100000001, 100000002])

    async def test_activate_trial_sends_admin_message_immediately(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SubscriptionStore(Path(temp_dir) / "telegram_subscriptions.db")
            try:
                user_bot = _FakeBot()
                admin_bot = _FakeBot()
                bot = TelegramMenuBot.__new__(TelegramMenuBot)
                bot.store = store
                bot.user_message_logger = _FakeUserMessageLogger()
                bot.logger = logging.getLogger("test-trial-admin-notification")
                bot.subscription_admin_notifier = SubscriptionAdminNotifier(bot=admin_bot, logger=bot.logger)
                bot.pending_inputs = {}
                bot.pending_location_candidates = {}
                bot.pending_location_modes = {}
                bot.pending_keyword_reviews = {}
                bot.pending_website_choices = {}
                bot.website_picker_cache = {}
                bot.deleted_alert_snapshots = {}
                bot.flow_origin = {}
                bot.flow_step = {}
                store.set_role_preference(77, "UX/UI Designer")

                async def _unexpected_start_alert_creation_flow_in_dm(user_id: int, username: str) -> None:
                    raise AssertionError("alert creation flow should not start when an alert already exists")

                bot._start_alert_creation_flow_in_dm = _unexpected_start_alert_creation_flow_in_dm

                await bot._activate_trial(
                    context=SimpleNamespace(bot=user_bot),
                    chat_id=77,
                    user_id=77,
                    username="alice",
                )

                active_sub = store.get_active_subscription(77)
                self.assertIsNotNone(active_sub)
                self.assertEqual(len(admin_bot.sent_messages), 1)
                self.assertEqual(admin_bot.sent_messages[0][0], DEFAULT_SUBSCRIPTION_ADMIN_CHAT_ID)
                self.assertIn("Type: Free trial claimed", admin_bot.sent_messages[0][1])
                self.assertIn("User ID: 77", admin_bot.sent_messages[0][1])
                self.assertIn("Username: @alice", admin_bot.sent_messages[0][1])
                self.assertEqual(len(user_bot.sent_messages), 1)
                self.assertEqual(user_bot.sent_messages[0][0], 77)
            finally:
                store.close()

    async def test_completed_payment_sends_admin_message_immediately(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SubscriptionStore(Path(temp_dir) / "telegram_subscriptions.db")
            try:
                user_bot = _FakeBot()
                admin_bot = _FakeBot()
                bot = TelegramMenuBot.__new__(TelegramMenuBot)
                bot.store = store
                bot.user_message_logger = _FakeUserMessageLogger()
                bot.logger = logging.getLogger("test-payment-admin-notification")
                bot.subscription_admin_notifier = SubscriptionAdminNotifier(bot=admin_bot, logger=bot.logger)
                bot.application = SimpleNamespace(bot=user_bot)
                bot.nowpayments = _FakeNowPayments()
                bot.subscriber_notifier = _FakeSubscriberNotifier()
                store.set_role_preference(88, "Product Designer")
                local_payment_id = store.create_payment_session(
                    user_id=88,
                    username="bob",
                    plan="monthly",
                    duration_days=30,
                    amount_usd=7.49,
                    provider_payment_id="invoice-88",
                    payment_url="https://example.com/pay/88",
                    status="waiting",
                )

                await bot._handle_payment_update(
                    context=SimpleNamespace(bot=user_bot),
                    chat_id=88,
                    user_id=88,
                    username="bob",
                    local_payment_id=local_payment_id,
                    is_manual=True,
                )

                active_sub = store.get_active_subscription(88)
                self.assertIsNotNone(active_sub)
                assert active_sub is not None
                self.assertEqual(active_sub.plan, "monthly")
                self.assertEqual(len(admin_bot.sent_messages), 1)
                self.assertEqual(admin_bot.sent_messages[0][0], DEFAULT_SUBSCRIPTION_ADMIN_CHAT_ID)
                self.assertIn("Type: Paid subscription activated", admin_bot.sent_messages[0][1])
                self.assertIn("Plan: Monthly", admin_bot.sent_messages[0][1])
                self.assertIn("User ID: 88", admin_bot.sent_messages[0][1])
                self.assertEqual(len(user_bot.sent_messages), 1)
                self.assertEqual(user_bot.sent_messages[0][0], 88)
                self.assertEqual(bot.subscriber_notifier.flush_calls, [200])
            finally:
                store.close()

    async def test_send_expiration_notices_uses_subscription_user_id(self) -> None:
        class _ExpiredStore:
            def __init__(self) -> None:
                self.marked: list[int] = []

            def list_recently_expired_subscriptions(self):
                return [SimpleNamespace(user_id=501, username="rima", source="payment")]

            def mark_expiration_notice_sent(self, user_id: int) -> None:
                self.marked.append(user_id)

            def get_ui_language(self, user_id: int) -> str:
                assert user_id == 501
                return "ru"

        user_bot = _FakeBot()
        bot = TelegramMenuBot.__new__(TelegramMenuBot)
        bot.store = _ExpiredStore()
        bot.application = SimpleNamespace(bot=user_bot)
        bot.user_message_logger = _FakeUserMessageLogger()

        await bot._send_expiration_notices()

        self.assertEqual(bot.store.marked, [501])
        self.assertEqual(len(user_bot.sent_messages), 1)
        self.assertEqual(user_bot.sent_messages[0][0], 501)
        self.assertIn("Поиск неактивен", user_bot.sent_messages[0][1])

    def test_payment_message_text_localizes_russian_checkout_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SubscriptionStore(Path(temp_dir) / "telegram_subscriptions.db")
            try:
                store.set_ui_language(701, "ru")
                bot = TelegramMenuBot.__new__(TelegramMenuBot)
                bot.store = store

                text = bot._payment_message_text(
                    701,
                    PLAN_DEFINITIONS[CALLBACK_MONTHLY_PLAN],
                    "https://example.com/pay/701",
                    "finished",
                )

                self.assertIn("Оплата плана Monthly", text)
                self.assertIn("Статус: оплачен", text)
            finally:
                store.close()


    async def test_send_pending_system_notices_notifies_users_about_removed_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "telegram_subscriptions.db"
            store = SubscriptionStore(db_path)
            try:
                store.upsert_trial_subscription(
                    user_id=703,
                    username="nora",
                    started_at_utc="2026-03-01T00:00:00Z",
                    ends_at_utc="2099-03-01T00:00:00Z",
                )
                store.set_ui_language(703, "en")
                store.add_user_website(703, "https://workspace.ru/web-design/")
                store._conn.execute(
                    """
                    INSERT INTO user_websites (user_id, url, currency, created_at_utc)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        703,
                        "https://www.rabota.ru/vacancy/%D0%B4%D0%B8%D0%B7%D0%B0%D0%B9%D0%BD%D0%B5%D1%80%20ux",
                        "RUB",
                        "2026-04-13T10:00:00Z",
                    ),
                )
                store._conn.commit()
                store.close()
                store = SubscriptionStore(db_path)

                bot = TelegramMenuBot.__new__(TelegramMenuBot)
                bot.store = store
                bot.logger = logging.getLogger("test-removed-source-notice")
                bot.user_message_logger = _FakeUserMessageLogger()

                captured: list[dict[str, object]] = []

                async def fake_send_to_user_and_log(*, user_id, username, text, reply_markup=None, disable_web_page_preview=False) -> None:
                    captured.append(
                        {
                            "user_id": user_id,
                            "username": username,
                            "text": text,
                            "reply_markup": reply_markup,
                            "disable_web_page_preview": disable_web_page_preview,
                        }
                    )

                bot._send_to_user_and_log = fake_send_to_user_and_log

                await bot._send_pending_system_notices(limit=10)

                self.assertEqual(len(captured), 1)
                self.assertEqual(captured[0]["user_id"], 703)
                self.assertIn("Rabota.ru was removed from ZapLance", str(captured[0]["text"]))
                self.assertIn("Workspace [ru]", str(captured[0]["text"]))
                self.assertTrue(bool(captured[0]["disable_web_page_preview"]))
                markup = captured[0]["reply_markup"]
                self.assertIsNotNone(markup)
                assert markup is not None
                callbacks = [button.callback_data for row in markup.inline_keyboard for button in row]
                self.assertEqual(callbacks, [CALLBACK_EDIT_SOURCES, CALLBACK_WEBSITE_CUSTOM, CALLBACK_BACK_TO_MAIN])
                self.assertEqual(store.list_due_system_notices(limit=10), [])
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
