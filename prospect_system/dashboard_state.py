from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from prospect_system.errors import ErrorRecord
from prospect_system.prospect_card import ProspectCard


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


@dataclass(slots=True)
class StepLog:
    message: str
    stage: str = ""
    url: str = ""
    created_at: str = field(default_factory=utc_now_iso)
    eta_seconds: float | None = None

    def to_row(self, session_id: str) -> list[Any]:
        return [self.created_at, session_id, self.stage, self.url, self.message, self.eta_seconds or ""]


@dataclass(slots=True)
class ScrapedPage:
    url: str
    final_url: str
    title: str
    text: str
    metadata: dict[str, str] = field(default_factory=dict)
    links: list[dict[str, Any]] = field(default_factory=list)
    status: int = 0
    strategy: str = ""
    scraped_at: str = field(default_factory=utc_now_iso)

    def text_excerpt(self, limit: int = 5000) -> str:
        return self.text[:limit]


@dataclass(slots=True)
class ProspectAnalysisState:
    session_id: str
    input_urls: list[str]
    target_criteria: str
    outreach_goal: str
    current_url: str = ""
    scraped_pages: list[ScrapedPage] = field(default_factory=list)
    extracted_text: str = ""
    ai_step_logs: list[StepLog] = field(default_factory=list)
    prospect_card: ProspectCard | None = None
    prospect_cards: list[ProspectCard] = field(default_factory=list)
    outreach_draft: dict[str, str] = field(default_factory=dict)
    human_decision: str = ""
    crm_stage: str = ""
    google_sheet_status: str = ""
    telegram_or_slack_alert_sent: str = ""
    errors: list[ErrorRecord] = field(default_factory=list)
    started_at: str = field(default_factory=utc_now_iso)
    completed_at: str = ""
    processing_time_seconds: float = 0.0
    reanalysis_count: int = 0

    @classmethod
    def create(cls, *, input_urls: list[str], target_criteria: str, outreach_goal: str) -> "ProspectAnalysisState":
        return cls(
            session_id=f"prospect-{uuid4().hex[:12]}",
            input_urls=list(input_urls),
            target_criteria=target_criteria,
            outreach_goal=outreach_goal,
        )

    def log_step(self, message: str, *, stage: str = "", url: str = "", eta_seconds: float | None = None) -> StepLog:
        entry = StepLog(
            message=message,
            stage=stage,
            url=url or self.current_url,
            eta_seconds=eta_seconds,
        )
        self.ai_step_logs.append(entry)
        self._refresh_processing_time()
        return entry

    def add_error(
        self,
        *,
        stage: str,
        message: str,
        url: str = "",
        details: dict[str, Any] | None = None,
    ) -> ErrorRecord:
        record = ErrorRecord(
            session_id=self.session_id,
            stage=stage,
            message=message,
            url=url or self.current_url,
            details=details or {},
        )
        self.errors.append(record)
        self.log_step(message, stage=stage, url=url)
        return record

    def add_scraped_page(self, page: ScrapedPage) -> None:
        self.scraped_pages.append(page)
        self.extracted_text = "\n\n".join(item.text for item in self.scraped_pages if item.text).strip()
        self._refresh_processing_time()

    def set_card(self, card: ProspectCard) -> None:
        self.prospect_card = card
        existing_index = next((idx for idx, item in enumerate(self.prospect_cards) if item.website == card.website), None)
        if existing_index is None:
            self.prospect_cards.append(card)
        else:
            self.prospect_cards[existing_index] = card

    def mark_completed(self) -> None:
        self.completed_at = utc_now_iso()
        self._refresh_processing_time()

    def _refresh_processing_time(self) -> None:
        started = _parse_iso(self.started_at)
        if started is None:
            return
        end = _parse_iso(self.completed_at) or datetime.now(timezone.utc)
        self.processing_time_seconds = max(0.0, (end - started).total_seconds())

    def pages_for_url(self, url: str) -> list[ScrapedPage]:
        normalized = url.strip().lower()
        return [
            page
            for page in self.scraped_pages
            if page.url.strip().lower() == normalized or page.final_url.strip().lower() == normalized
        ]

    def estimate_remaining_seconds(self, *, max_pages_to_scrape: int) -> float:
        elapsed = max(self.processing_time_seconds, 0.01)
        pages_done = max(len(self.scraped_pages), 1)
        average_per_page = elapsed / pages_done
        total_possible_pages = max(1, len(self.input_urls) * max(1, max_pages_to_scrape))
        pages_remaining = max(0, total_possible_pages - len(self.scraped_pages))
        return pages_remaining * average_per_page

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["prospect_card"] = self.prospect_card.to_dict() if self.prospect_card else None
        data["prospect_cards"] = [card.to_dict() for card in self.prospect_cards]
        return data
