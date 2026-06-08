from __future__ import annotations

import html
import logging
import re
from urllib.parse import urlparse

try:
    from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
except ImportError:
    from telegram._bot import Bot
    from telegram._inline.inlinekeyboardbutton import InlineKeyboardButton
    from telegram._inline.inlinekeyboardmarkup import InlineKeyboardMarkup

from job_bot.language_utils import detect_language, localized_text
from job_bot.models import JobCard
from job_bot.opportunity_fields import OPPORTUNITY_NOTIFICATION_FIELD_ORDER, opportunity_card_value
from job_bot.telegram_subscription_store import SubscriptionStore, strip_www_prefix


class TelegramNotifier:
    FEEDBACK_UP_PREFIX = "match_up_"
    FEEDBACK_DOWN_PREFIX = "match_dn_"

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        logger: logging.Logger,
        store: SubscriptionStore | None = None,
    ) -> None:
        self.bot = Bot(token=bot_token)
        self.chat_id = chat_id
        self.logger = logger
        self.store = store
        self.chat_user_id = self._parse_numeric_chat_id(chat_id)

    @staticmethod
    def format_message(card: JobCard) -> str:
        language = getattr(card, "language", "en")
        cleaned_values = {
            field_key: TelegramNotifier._clean_message_text(opportunity_card_value(card, field_key))
            for field_key in OPPORTUNITY_NOTIFICATION_FIELD_ORDER
        }
        website = TelegramNotifier._source_label(card.website)
        return (
            f"{localized_text(language, en='✨ New Project Match', ru='✨ Новое совпадение по проекту')}\n"
            f"{localized_text(language, en='💼 Project', ru='💼 Проект')}: {cleaned_values['title']}\n"
            f"{localized_text(language, en='👤 Client', ru='👤 Заказчик')}: {cleaned_values['counterparty'] or localized_text(language, en='Unknown', ru='Не указано')}\n"
            f"{localized_text(language, en='💰 Budget / Rate', ru='💰 Бюджет / ставка')}: {cleaned_values['payment_terms']}\n"
            f"{localized_text(language, en='🧩 Engagement Type', ru='🧩 Формат работы')}: {cleaned_values['engagement_summary']}\n"
            f"{localized_text(language, en='⏳ Timeline / Duration', ru='⏳ Сроки / длительность')}: {cleaned_values['timeline_summary']}\n"
            f"{localized_text(language, en='🛠️ Skills Requested', ru='🛠️ Нужные навыки')}: {cleaned_values['skills_required']}\n"
            f"{localized_text(language, en='📍 Remote / Location', ru='📍 Формат / локация')}: {cleaned_values['opportunity_location']}\n"
            f"{localized_text(language, en='🌐 Source', ru='🌐 Источник')}: {website}\n"
            f"{localized_text(language, en='📝 Scope Summary:', ru='📝 Краткое описание:')}\n"
            f"{cleaned_values['scope_summary']}"
        )

    @staticmethod
    def _clean_message_text(value: str) -> str:
        text = html.unescape(str(value or ""))
        text = re.sub(r"<\s*br\s*/?\s*>", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"</\s*p\s*>", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"[ \t\r\f\v]+", " ", text)
        text = re.sub(r"\s+([,.;:!?])", r"\1", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip() or "Not specified"

    @staticmethod
    def _parse_numeric_chat_id(value: str) -> int | None:
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _source_label(value: str) -> str:
        parsed = urlparse(str(value or "").strip())
        domain = parsed.netloc or str(value or "").strip()
        return strip_www_prefix(domain) or "Unknown"

    @classmethod
    def _delivery_markup(cls, job_url: str, delivery_event_id: int | None, language: str = "en") -> InlineKeyboardMarkup:
        rows = [[InlineKeyboardButton(localized_text(language, en="Open Project", ru="Открыть проект"), url=job_url)]]
        if delivery_event_id is not None:
            rows.append(
                [
                    InlineKeyboardButton(
                        localized_text(language, en="Relevant", ru="Подходит"),
                        callback_data=f"{cls.FEEDBACK_UP_PREFIX}{delivery_event_id}",
                    ),
                    InlineKeyboardButton(
                        localized_text(language, en="Not Relevant", ru="Не подходит"),
                        callback_data=f"{cls.FEEDBACK_DOWN_PREFIX}{delivery_event_id}",
                    ),
                ]
            )
        return InlineKeyboardMarkup(rows)

    def _is_subscriber_chat(self) -> bool:
        if self.store is None or self.chat_user_id is None:
            return False
        try:
            return bool(self.store.is_user_subscribed(self.chat_user_id))
        except Exception as exc:  # noqa: BLE001
            self.logger.warning(
                "[telegram] failed to detect subscriber chat_id=%s: %s",
                self.chat_id,
                str(exc),
            )
            return False

    def _record_delivery_event(self, card: JobCard) -> int | None:
        if self.store is None or self.chat_user_id is None or self.chat_user_id <= 0:
            return None
        recorder = getattr(self.store, "record_delivery_event", None)
        if not callable(recorder):
            return None
        try:
            return int(
                recorder(
                    user_id=self.chat_user_id,
                    username="",
                    card_url=card.url,
                    card_title=card.title,
                    card_company=getattr(card, "company", "") or card.counterparty,
                    card_location=card.opportunity_location or card.location,
                    card_website=card.website,
                    match_reason="legacy_telegram_chat_notification",
                )
            )
        except Exception as exc:  # noqa: BLE001
            self.logger.warning(
                "[telegram] failed to record legacy delivery event chat_id=%s url=%s: %s",
                self.chat_id,
                card.url,
                str(exc),
            )
            return None

    def _mark_delivery_event_sent(self, delivery_event_id: int | None, message: object) -> None:
        if self.store is None or self.chat_user_id is None or delivery_event_id is None:
            return
        marker = getattr(self.store, "mark_delivery_event_sent", None)
        if not callable(marker):
            return
        message_id = int(getattr(message, "message_id", 0) or 0)
        if message_id <= 0:
            return
        try:
            marker(
                int(delivery_event_id),
                telegram_chat_id=int(self.chat_user_id),
                telegram_message_id=message_id,
            )
        except Exception as exc:  # noqa: BLE001
            self.logger.warning(
                "[telegram] failed to persist sent message metadata chat_id=%s event_id=%s: %s",
                self.chat_id,
                delivery_event_id,
                str(exc),
            )

    @classmethod
    def format_human_review_message(
        cls,
        card: JobCard,
        *,
        reason: str = "",
        review_id: int | None = None,
    ) -> str:
        language = getattr(card, "language", "en")
        company = cls._clean_message_text(card.counterparty or getattr(card, "company", ""))
        title = cls._clean_message_text(card.title)
        description = cls._clean_message_text(card.scope_summary or card.description)
        location = cls._clean_message_text(card.opportunity_location or card.location)
        salary = cls._clean_message_text(card.payment_terms or card.salary)
        engagement = cls._clean_message_text(" / ".join(part for part in (card.engagement_type, card.commitment_level) if part))
        timeline = cls._clean_message_text(" / ".join(part for part in (card.start_timeline, card.duration) if part))
        skills = cls._clean_message_text(getattr(card, "skills_required", ""))
        website = cls._source_label(card.website)
        reason_text = cls._clean_message_text(reason)
        review_label = f"\nReview ID: {review_id}" if review_id is not None else ""
        return (
            f"{localized_text(language, en='Project Review Needed', ru='Нужна проверка проекта')}\n"
            f"Project: {title}\n"
            f"Client: {company}\n"
            f"Budget / Rate: {salary}\n"
            f"Engagement Type: {engagement}\n"
            f"Timeline / Duration: {timeline}\n"
            f"Skills Requested: {skills}\n"
            f"Remote / Location: {location}\n"
            f"Source: {website}\n"
            f"Confidence: {getattr(card, 'confidence', 0.0):.3f}{review_label}\n"
            f"Reason: {reason_text}\n"
            "Scope Summary:\n"
            f"{description}"
        )

    async def send_card(self, card: JobCard) -> None:
        if self._is_subscriber_chat():
            self.logger.info(
                "[telegram] skipped legacy direct-chat alert because subscriber delivery is active chat_id=%s url=%s",
                self.chat_id,
                card.url,
            )
            return
        message = self.format_message(card)
        delivery_event_id = self._record_delivery_event(card)
        telegram_message = await self.bot.send_message(
            chat_id=self.chat_id,
            text=message,
            disable_web_page_preview=False,
            reply_markup=self._delivery_markup(card.proposal_or_contact_url, delivery_event_id, detect_language(text=message)),
        )
        self._mark_delivery_event_sent(delivery_event_id, telegram_message)
        self.logger.info("[telegram] sent card to chat_id=%s url=%s", self.chat_id, card.url)

    async def send_human_review_request(
        self,
        card: JobCard,
        *,
        reason: str = "",
        review_id: int | None = None,
    ) -> None:
        message = self.format_human_review_message(card, reason=reason, review_id=review_id)
        await self.bot.send_message(
            chat_id=self.chat_id,
            text=message,
            disable_web_page_preview=False,
            reply_markup=self._delivery_markup(card.proposal_or_contact_url, None, detect_language(text=message)),
        )
        self.logger.info("[telegram] sent human-review alert to chat_id=%s url=%s", self.chat_id, card.url)
