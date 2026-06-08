from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(slots=True)
class LinkCandidate:
    url: str
    anchor_text: str
    source_page: str
    structural_score: float


@dataclass(slots=True)
class JobCard:
    """Canonical opportunity card with legacy job-field aliases preserved."""

    website: str
    url: str
    title: str
    description: str
    salary: str
    location: str
    is_job_post: bool
    confidence: float
    extraction_method: str
    extracted_at_utc: str = field(default_factory=utc_now_iso)
    posted_at_utc: str = ""
    notes: str = ""
    company: str = ""
    language: str = "en"
    is_relevant_opportunity: bool = False
    opportunity_kind: str = ""
    client: str = ""
    requester: str = ""
    scope_summary: str = ""
    budget: str = ""
    duration: str = ""
    commitment_level: str = ""
    skills_required: str = ""
    proposal_deadline: str = ""
    start_timeline: str = ""
    industry: str = ""
    engagement_type: str = ""
    remote_location_constraint: str = ""
    contact_url: str = ""

    def __post_init__(self) -> None:
        self.is_job_post = bool(self.is_job_post)
        self.is_relevant_opportunity = bool(self.is_relevant_opportunity or self.is_job_post)
        if not self.opportunity_kind:
            if self.is_job_post:
                self.opportunity_kind = "job_post"
            elif self.is_relevant_opportunity:
                self.opportunity_kind = "project_post"
            else:
                self.opportunity_kind = ""

        company = str(self.company or "").strip()
        client = str(self.client or "").strip()
        requester = str(self.requester or "").strip()
        salary = str(self.salary or "").strip()
        budget = str(self.budget or "").strip()
        location = str(self.location or "").strip()
        remote_constraint = str(self.remote_location_constraint or "").strip()

        if not client and company and company.lower() != "unknown":
            self.client = company
        if not requester and self.client and self.client.lower() != "unknown":
            self.requester = self.client
        if (not company or company.lower() == "unknown") and self.client:
            self.company = self.client

        if not self.scope_summary and str(self.description or "").strip():
            self.scope_summary = str(self.description).strip()

        if not budget and salary and salary.lower() not in {"unknown", "not specified"}:
            self.budget = salary
        if (not salary or salary.lower() in {"unknown", "not specified"}) and self.budget:
            self.salary = self.budget

        if not remote_constraint and location and location.lower() not in {"unknown", "not specified"}:
            self.remote_location_constraint = location
        if (not location or location.lower() in {"unknown", "not specified"}) and self.remote_location_constraint:
            self.location = self.remote_location_constraint

        if not self.contact_url:
            self.contact_url = self.url

    @property
    def counterparty(self) -> str:
        for value in (self.client, self.requester, self.company):
            cleaned = str(value or "").strip()
            if cleaned and cleaned.lower() not in {"unknown", "not specified"}:
                return cleaned
        return ""

    @property
    def payment_terms(self) -> str:
        for value in (self.budget, self.salary):
            cleaned = str(value or "").strip()
            if cleaned and cleaned.lower() not in {"unknown", "not specified"}:
                return cleaned
        return ""

    @property
    def opportunity_location(self) -> str:
        for value in (self.remote_location_constraint, self.location):
            cleaned = str(value or "").strip()
            if cleaned and cleaned.lower() not in {"unknown", "not specified"}:
                return cleaned
        return ""

    @property
    def proposal_or_contact_url(self) -> str:
        return str(self.contact_url or self.url or "").strip()

    @property
    def is_opportunity_post(self) -> bool:
        return bool(self.is_relevant_opportunity)

    @property
    def payment(self) -> str:
        return self.payment_terms

    @property
    def location_requirement(self) -> str:
        return self.opportunity_location


OpportunityCard = JobCard


@dataclass(slots=True)
class CycleStats:
    website: str
    fetched_pages: int = 0
    discovered_links: int = 0
    checked_links: int = 0
    seen_links_in_row: int = 0
    new_cards: int = 0
    neglected_posts: int = 0
    neglect_reasons: dict[str, int] = field(default_factory=dict)
    errors: int = 0
    last_error: Optional[str] = None
