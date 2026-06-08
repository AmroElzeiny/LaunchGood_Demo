from __future__ import annotations

import logging
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from job_bot.filter_ai import KeywordExpansionItem, KeywordInterpretation
from job_bot.telegram_menu_bot import TelegramMenuBot as LegacyTelegramMenuBot
from job_bot.zapcareers_telegram_bot import (
    AI_PROCESSING_NOTICE,
    CALLBACK_WEBSITE_CUSTOM,
    STEP_WEBSITES,
    WAITING_KEYWORDS_INPUT,
    WAITING_LOCATION_INPUT,
    WAITING_WEBSITE_INPUT,
    TelegramMenuBot as ZapCareersTelegramMenuBot,
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
        self.sent_photos: list[tuple[int, str, str | None, object | None]] = []
        self.sent_messages: list[tuple[int, str, object | None]] = []
        self.sent_photo_kwargs: list[dict[str, object]] = []

    async def send_photo(self, *, chat_id: int, photo, caption: str | None = None, reply_markup=None, **kwargs) -> None:
        self.sent_photos.append((chat_id, getattr(photo, "name", ""), caption, reply_markup))
        self.sent_photo_kwargs.append(kwargs)

    async def send_message(self, *, chat_id: int, text: str, reply_markup=None, **kwargs) -> None:
        del kwargs
        self.sent_messages.append((chat_id, text, reply_markup))


class _FailingPhotoBot(_FakeBot):
    async def send_photo(self, *, chat_id: int, photo, caption: str | None = None, reply_markup=None, **kwargs) -> None:
        raise RuntimeError("upload timed out")


class _FakeKeywordFilterAI:
    async def interpret_keywords(self, keywords: list[str]) -> KeywordInterpretation:
        return KeywordInterpretation(
            input_keywords=list(keywords),
            understood_keywords=list(keywords),
            expanded_keywords=[*keywords, "artificial intelligence"],
            per_keyword=[
                KeywordExpansionItem(
                    input_keyword=keyword,
                    understood_as=keyword,
                    similar_keywords=["artificial intelligence"] if keyword == "ai" else [],
                )
                for keyword in keywords
            ],
        )


class _FakeCallbackQuery:
    def __init__(self, data: str) -> None:
        self.data = data
        self.answered = False

    async def answer(self) -> None:
        self.answered = True


class TelegramGuideImageTests(unittest.IsolatedAsyncioTestCase):
    def test_zapcareers_language_selector_offers_english_and_russian_only(self) -> None:
        bot = ZapCareersTelegramMenuBot.__new__(ZapCareersTelegramMenuBot)

        markup = bot._language_selector_markup()
        labels = [row[0].text for row in markup.inline_keyboard]

        self.assertEqual(len(labels), 2)
        self.assertIn("English", labels[0])
        self.assertIn("Русский", labels[1])

    def test_zapcareers_about_text_is_freelance_focused_in_both_languages(self) -> None:
        bot = ZapCareersTelegramMenuBot.__new__(ZapCareersTelegramMenuBot)
        bot.store = SimpleNamespace(get_ui_language=lambda user_id: "en" if user_id == 1 else "ru")

        english_text = bot._about_text(1)
        russian_text = bot._about_text(2)

        self.assertIn("ℹ️ About ZapLance", english_text)
        self.assertIn("freelance projects", english_text)
        self.assertIn("AI summary", english_text)
        self.assertIn("ℹ️ О ZapLance", russian_text)
        self.assertIn("контрактные проекты", russian_text)
        self.assertIn("Краткое резюме от ИИ", russian_text)

    def test_zapcareers_custom_website_prompt_is_localized_in_english_and_russian(self) -> None:
        bot = ZapCareersTelegramMenuBot.__new__(ZapCareersTelegramMenuBot)
        bot.store = SimpleNamespace(get_ui_language=lambda user_id: "en" if user_id == 1 else "ru")

        english_text = bot._t(1, "website_custom_prompt")
        russian_text = bot._t(2, "website_custom_prompt")

        self.assertIn("freelance projects search-results page", english_text)
        self.assertIn("Please avoid homepages and single-job pages.", english_text)
        self.assertIn("страницу результатов поиска фриланс-проектов", russian_text)
        self.assertIn("не отправляйте главные страницы сайтов", russian_text)

    def test_zapcareers_main_menu_text_uses_new_marketing_copy_and_localized_plan_names(self) -> None:
        bot = ZapCareersTelegramMenuBot.__new__(ZapCareersTelegramMenuBot)

        def _latest_subscription(user_id: int):
            return SimpleNamespace(
                is_active=True,
                plan="trial_48h",
                ends_at_utc="2027-02-28T22:00:00Z",
            )

        bot.store = SimpleNamespace(
            get_ui_language=lambda user_id: "en" if user_id == 1 else "ru",
            get_latest_subscription=_latest_subscription,
            has_alert_configuration=lambda user_id: True,
            get_notification_preference=lambda user_id: True,
        )

        english_text = bot._main_menu_text(1)
        russian_text = bot._main_menu_text(2)

        self.assertIn("🏠 Main Menu", english_text)
        self.assertIn("Welcome to ZapLance", english_text)
        self.assertIn("under 1 minute from posting time", english_text)
        self.assertIn("🔔 Project alerts: active", english_text)
        self.assertIn("💳 Subscription: Trial 7D until 2027-02-28 22:00 UTC", english_text)

        self.assertIn("🏠 Главное меню", russian_text)
        self.assertIn("Добро пожаловать в ZapLance", russian_text)
        self.assertIn("менее чем за 1 минуту после публикации", russian_text)
        self.assertIn("🔔 Уведомления о проектах: активны", russian_text)
        self.assertIn("💳 Подписка: пробный период 7 дней до 2027-02-28 22:00 UTC", russian_text)

    def test_zapcareers_working_input_language_prefers_cyrillic_when_ui_language_not_saved(self) -> None:
        bot = ZapCareersTelegramMenuBot.__new__(ZapCareersTelegramMenuBot)
        bot.store = SimpleNamespace(get_ui_language=lambda user_id: "")

        self.assertEqual(bot._working_input_language(1, "Нужен UX/UI дизайнер"), "ru")

    def test_zapcareers_project_filter_prompt_uses_payment_fields_and_no_location(self) -> None:
        bot = ZapCareersTelegramMenuBot.__new__(ZapCareersTelegramMenuBot)
        bot.flow_origin = {}
        bot.store = SimpleNamespace(
            get_ui_language=lambda user_id: "en",
            get_project_preferences=lambda user_id: {},
        )

        markup = bot._project_preferences_prompt_markup(123)
        labels = [row[0].text for row in markup.inline_keyboard]
        joined = "\n".join(labels)

        self.assertIn("Deliverables", joined)
        self.assertIn("Min Payment USD", joined)
        self.assertIn("Max Payment USD", joined)
        self.assertIn("Min Payment RUB", joined)
        self.assertIn("Max Payment RUB", joined)
        self.assertNotIn("Location", joined)

    def test_zapcareers_project_filter_current_value_formats_numeric_filters(self) -> None:
        bot = ZapCareersTelegramMenuBot.__new__(ZapCareersTelegramMenuBot)
        bot.store = SimpleNamespace(
            get_ui_language=lambda user_id: "en" if user_id == 1 else "ru",
            get_project_preferences=lambda user_id: {
                "minimum_payment_usd": 1500,
                "maximum_payment_usd": 5000,
                "minimum_payment_rub": 120000,
                "maximum_payment_rub": 350000,
            },
        )

        self.assertEqual(bot._project_filter_current_value(1, "minimum_payment_usd"), "$1,500 minimum")
        self.assertEqual(bot._project_filter_current_value(1, "maximum_payment_usd"), "$5,000 maximum")
        self.assertEqual(bot._project_filter_current_value(2, "minimum_payment_rub"), "от ₽120,000")
        self.assertEqual(bot._project_filter_current_value(2, "maximum_payment_rub"), "до ₽350,000")

    def test_zapcareers_website_labels_include_russian_sites(self) -> None:
        bot = ZapCareersTelegramMenuBot.__new__(ZapCareersTelegramMenuBot)
        self.assertIn("🇷🇺 Workspace [ru]", bot._website_display_name("https://workspace.ru/web-design/"))
        self.assertIn(
            "🇷🇺 FL.ru Projects [ru]",
            bot._website_display_name("https://www.fl.ru/projects/?kind=1"),
        )

    def test_legacy_website_choices_include_russian_sites(self) -> None:
        bot = LegacyTelegramMenuBot.__new__(LegacyTelegramMenuBot)
        self.assertIn("Workspace", bot._website_display_name("https://workspace.ru/web-design/"))

    def test_zapcareers_default_site_choices_follow_curated_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            curated = Path(temp_dir) / "sites.txt"
            curated.write_text(
                "\n".join(
                    [
                        "https://contra.com/opportunities?search=ui%2Fux%20designer",
                        "https://dribbble.com/jobs?location=Anywhere&remote=true",
                        "https://weworkremotely.com/categories/remote-design-jobs",
                        "https://wellfound.com/role/ui-ux-designer",
                        "https://builtin.com/jobs/remote/hybrid/office/designer?search=Web+Designer&allLocations=true",
                        "https://www.flexjobs.com/remote-jobs/ux-designer?sortbyposteddate=true&page=1",
                        "https://www.fl.ru/projects/?kind=1",
                        "https://workspace.ru/web-design/",
                    ]
                ),
                encoding="utf-8",
            )
            bot = ZapCareersTelegramMenuBot.__new__(ZapCareersTelegramMenuBot)
            bot.scrape_sites_file_path = curated

            choices = bot._default_site_choices("UX/UI Designer")

            self.assertEqual(len(choices), 8)
            urls = [url for _, url in choices]
            self.assertNotIn("https://www.upwork.com/freelance-jobs/uiux", urls)
            self.assertNotIn("https://www.workatastartup.com/jobs", urls)
            self.assertNotIn("https://www.ycombinator.com/jobs", urls)
            self.assertNotIn("https://hh.ru/search/vacancy?area=113&text=UX%2FUI+designer", urls)
            self.assertNotIn(
                "https://www.rabota.ru/vacancy/?query=%D0%B4%D0%B8%D0%B7%D0%B0%D0%B9%D0%BD%D0%B5%D1%80%20ux&sort=publication_time",
                urls,
            )
            self.assertIn("https://workspace.ru/web-design", urls)
            self.assertIn("https://www.fl.ru/projects?kind=1", urls)

    async def test_zapcareers_keywords_flow_sends_ai_processing_notice_before_confirmation(self) -> None:
        bot = ZapCareersTelegramMenuBot.__new__(ZapCareersTelegramMenuBot)
        bot.filter_ai = _FakeKeywordFilterAI()
        bot.flow_origin = {}
        bot.pending_inputs = {456: WAITING_KEYWORDS_INPUT}
        bot.pending_keyword_reviews = {}
        sent_texts: list[str] = []

        async def fake_send_and_log(*, context, chat_id, user_id, username, text, reply_markup=None) -> None:
            sent_texts.append(text)

        bot._send_and_log = fake_send_and_log

        await bot._handle_keywords_text(
            context=SimpleNamespace(bot=_FakeBot()),
            chat_id=123,
            user_id=456,
            username="alice",
            text="Figma, AI",
        )

        self.assertEqual(sent_texts[0], AI_PROCESSING_NOTICE)
        self.assertIn("Here is what the AI understood", sent_texts[1])
        self.assertNotIn(456, bot.pending_inputs)
        self.assertIn(456, bot.pending_keyword_reviews)

    async def test_zapcareers_location_pending_input_redirects_to_project_filters(self) -> None:
        bot = ZapCareersTelegramMenuBot.__new__(ZapCareersTelegramMenuBot)
        bot.pending_inputs = {456: WAITING_LOCATION_INPUT}
        bot.flow_origin = {}
        bot.store = SimpleNamespace(
            get_ui_language=lambda user_id: "en",
            get_project_preferences=lambda user_id: {},
        )
        bot._log_inbound = lambda **kwargs: None
        sent_messages: list[tuple[str, object | None]] = []

        async def fake_send_and_log(*, context, chat_id, user_id, username, text, reply_markup=None) -> None:
            sent_messages.append((text, reply_markup))

        bot._send_and_log = fake_send_and_log
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=456, username="alice", first_name="Alice"),
            effective_chat=SimpleNamespace(id=123),
            effective_message=SimpleNamespace(text="USA"),
        )

        await bot.handle_text_message(update, SimpleNamespace(bot=_FakeBot()))

        self.assertEqual(
            sent_messages[0][0],
            "Location filters were removed. Please continue with project filters.",
        )
        labels = [row[0].text for row in sent_messages[0][1].inline_keyboard]
        self.assertIn("Min Payment USD", "\n".join(labels))
        self.assertNotIn(456, bot.pending_inputs)

    async def test_zapcareers_bot_sends_custom_website_guide_image(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            image_path = Path(temp_dir) / "Gemini_Generated_Image.png"
            image_path.write_bytes(b"fake-image-bytes")
            bot = ZapCareersTelegramMenuBot.__new__(ZapCareersTelegramMenuBot)
            bot.custom_website_guide_image_path = image_path
            bot.user_message_logger = _FakeUserMessageLogger()
            bot.logger = logging.getLogger("test-zapcareers-guide-image")
            context = SimpleNamespace(bot=_FakeBot())
            prompt_text = "Send the link of a project board."
            reply_markup = object()

            await bot._send_custom_website_guide_image(
                context,
                123,
                456,
                "alice",
                prompt_text,
                reply_markup,
            )

            self.assertEqual(context.bot.sent_photos, [(123, str(image_path), prompt_text, reply_markup)])
            self.assertEqual(context.bot.sent_messages, [])
            self.assertEqual(
                context.bot.sent_photo_kwargs,
                [
                    {
                        "write_timeout": 60,
                        "read_timeout": 60,
                        "connect_timeout": 20,
                        "pool_timeout": 20,
                    }
                ],
            )
            self.assertEqual(bot.user_message_logger.events[0]["text"], f"[image] Gemini_Generated_Image.png\n{prompt_text}")
            self.assertEqual(bot.user_message_logger.events[0]["event_type"], "bot_message")

    async def test_zapcareers_custom_website_callback_sends_single_photo_message(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            image_path = Path(temp_dir) / "Gemini_Generated_Image.png"
            image_path.write_bytes(b"fake-image-bytes")
            bot = ZapCareersTelegramMenuBot.__new__(ZapCareersTelegramMenuBot)
            bot.custom_website_guide_image_path = image_path
            bot.user_message_logger = _FakeUserMessageLogger()
            bot.logger = logging.getLogger("test-zapcareers-custom-site-callback")
            bot.pending_inputs = {}
            bot.flow_step = {}
            bot.flow_origin = {}
            bot.pending_location_candidates = {}
            bot.pending_location_modes = {}
            bot.pending_location_selections = {}
            bot.pending_keyword_reviews = {}
            bot.pending_project_filter_fields = {}
            bot.pending_website_choices = {}
            bot.pending_custom_website_urls = {}
            bot.pending_custom_website_currency = {}
            bot.website_picker_cache = {}
            bot.deleted_alert_snapshots = {}
            bot.store = SimpleNamespace(
                get_ui_language=lambda user_id: "en",
                get_user_websites=lambda user_id: [],
                get_role_preference=lambda user_id: "UX/UI Designer",
            )
            context = SimpleNamespace(bot=_FakeBot())
            query = _FakeCallbackQuery(CALLBACK_WEBSITE_CUSTOM)
            update = SimpleNamespace(
                effective_user=SimpleNamespace(id=456, username="alice", full_name="Alice"),
                effective_chat=SimpleNamespace(id=123),
                callback_query=query,
            )

            await bot.handle_callback(update, context)

            self.assertTrue(query.answered)
            self.assertEqual(bot.pending_inputs[456], WAITING_WEBSITE_INPUT)
            self.assertEqual(bot.flow_step[456], STEP_WEBSITES)
            self.assertEqual(len(context.bot.sent_photos), 1)
            self.assertEqual(context.bot.sent_messages, [])
            self.assertIn(
                "freelance projects search-results page",
                context.bot.sent_photos[0][2] or "",
            )

    async def test_legacy_bot_sends_custom_website_guide_image(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            image_path = Path(temp_dir) / "Gemini_Generated_Image.png"
            image_path.write_bytes(b"fake-image-bytes")
            bot = LegacyTelegramMenuBot.__new__(LegacyTelegramMenuBot)
            bot.custom_website_guide_image_path = image_path
            bot.user_message_logger = _FakeUserMessageLogger()
            bot.logger = logging.getLogger("test-legacy-guide-image")
            context = SimpleNamespace(bot=_FakeBot())
            prompt_text = "Send the link of a project board."
            reply_markup = object()

            await bot._send_custom_website_guide_image(
                context,
                321,
                654,
                "bob",
                prompt_text,
                reply_markup,
            )

            self.assertEqual(context.bot.sent_photos, [(321, str(image_path), prompt_text, reply_markup)])
            self.assertEqual(context.bot.sent_messages, [])
            self.assertEqual(
                context.bot.sent_photo_kwargs,
                [
                    {
                        "write_timeout": 60,
                        "read_timeout": 60,
                        "connect_timeout": 20,
                        "pool_timeout": 20,
                    }
                ],
            )
            self.assertEqual(bot.user_message_logger.events[0]["text"], f"[image] Gemini_Generated_Image.png\n{prompt_text}")
            self.assertEqual(bot.user_message_logger.events[0]["event_type"], "bot_message")

    async def test_zapcareers_bot_does_not_fallback_to_text_when_photo_upload_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            image_path = Path(temp_dir) / "Gemini_Generated_Image.png"
            image_path.write_bytes(b"fake-image-bytes")
            bot = ZapCareersTelegramMenuBot.__new__(ZapCareersTelegramMenuBot)
            bot.custom_website_guide_image_path = image_path
            bot.user_message_logger = _FakeUserMessageLogger()
            bot.logger = logging.getLogger("test-zapcareers-guide-image-failure")
            context = SimpleNamespace(bot=_FailingPhotoBot())

            await bot._send_custom_website_guide_image(
                context,
                123,
                456,
                "alice",
                "Send the link of a project board.",
                object(),
            )

            self.assertEqual(context.bot.sent_messages, [])
            self.assertEqual(bot.user_message_logger.events, [])


if __name__ == "__main__":
    unittest.main()

