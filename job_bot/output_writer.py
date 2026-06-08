from __future__ import annotations

from pathlib import Path
from threading import Lock

from job_bot.language_utils import localized_text
from job_bot.models import JobCard


class CardWriter:
    def __init__(self, output_file: Path) -> None:
        self.output_file = output_file
        self.output_file.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()

    def write_card(self, cycle_utc: str, agent_name: str, card: JobCard) -> None:
        language = getattr(card, "language", "en")
        title = self._limit_words(card.title, 5)
        description = self._limit_words(card.scope_summary or card.description, 30)
        counterparty = card.counterparty or localized_text(language, en="Unknown", ru="Не указано")
        payment_terms = card.payment_terms or localized_text(language, en="Unknown", ru="Не указано")
        location = card.opportunity_location or localized_text(language, en="Unknown", ru="Не указано")
        engagement = " / ".join(part for part in (card.engagement_type, card.commitment_level) if part) or localized_text(language, en="Unknown", ru="Не указано")
        timeline = " / ".join(part for part in (card.start_timeline, card.duration) if part) or localized_text(language, en="Unknown", ru="Не указано")
        skills = card.skills_required or localized_text(language, en="Unknown", ru="Не указано")
        contact_url = card.proposal_or_contact_url or card.url
        localized_block = [
            "===== OPPORTUNITY CARD START =====",
            f"cycle_utc: {cycle_utc}",
            f"agent: {agent_name}",
            f"website: {card.website}",
            localized_text(language, en="New Project Match", ru="Новое совпадение по проекту"),
            "--------------",
            f"{localized_text(language, en='Project', ru='Проект')}: {title}",
            f"{localized_text(language, en='Scope Summary', ru='Краткое описание')}: {description}",
            f"{localized_text(language, en='Client', ru='Заказчик')}: {counterparty}",
            f"{localized_text(language, en='Budget / Rate', ru='Бюджет / ставка')}: {payment_terms}",
            f"{localized_text(language, en='Engagement Type', ru='Формат работы')}: {engagement}",
            f"{localized_text(language, en='Timeline / Duration', ru='Сроки / длительность')}: {timeline}",
            f"{localized_text(language, en='Skills Requested', ru='Нужные навыки')}: {skills}",
            f"{localized_text(language, en='Remote / Location', ru='Формат / локация')}: {location}",
            (
                f"{localized_text(language, en='Posted At (UTC)', ru='Опубликовано (UTC)')}: "
                f"{card.posted_at_utc or localized_text(language, en='Unknown', ru='Не указано')}"
            ),
            f"URL: {contact_url}",
            "===== OPPORTUNITY CARD END =====",
            "",
        ]
        content = "\n".join(localized_block)
        with self._lock:
            with self.output_file.open("a", encoding="utf-8") as handle:
                handle.write(content)

    @staticmethod
    def _limit_words(text: str, max_words: int) -> str:
        words = text.split()
        if len(words) <= max_words:
            return text
        return " ".join(words[:max_words])
