from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from job_bot.models import JobCard


OPPORTUNITY_FIELD_DICTIONARY: dict[str, dict[str, object]] = {
    "title": {
        "prompt_label": "project title",
        "card_fields": ("title",),
        "markers": ("project title", "title", "opportunity title", "project name", "brief title"),
    },
    "counterparty": {
        "prompt_label": "client / requester",
        "card_fields": ("client", "requester", "company"),
        "markers": ("client", "requester", "company", "organization", "agency", "founder", "customer"),
    },
    "scope_summary": {
        "prompt_label": "scope summary",
        "card_fields": ("scope_summary", "description"),
        "markers": (
            "scope",
            "scope summary",
            "project overview",
            "brief",
            "summary",
            "deliverables",
            "what we need",
            "work required",
            "project details",
        ),
    },
    "payment_terms": {
        "prompt_label": "budget / hourly rate / fixed price",
        "card_fields": ("budget", "salary"),
        "markers": (
            "budget",
            "rate",
            "payment",
            "fixed price",
            "fixed-price",
            "hourly",
            "hourly rate",
            "compensation",
            "pricing",
        ),
    },
    "engagement_summary": {
        "prompt_label": "engagement type",
        "card_fields": ("engagement_type", "commitment_level"),
        "markers": ("engagement type", "contract type", "project type", "work type", "commitment", "retainer"),
    },
    "duration": {
        "prompt_label": "duration",
        "card_fields": ("duration",),
        "markers": ("duration", "timeline", "project length", "contract length", "term", "length"),
    },
    "start_timeline": {
        "prompt_label": "start timeline",
        "card_fields": ("start_timeline",),
        "markers": ("start", "start date", "kickoff", "start timeline", "begin", "timeline"),
    },
    "proposal_deadline": {
        "prompt_label": "proposal deadline",
        "card_fields": ("proposal_deadline",),
        "markers": ("proposal deadline", "apply by", "deadline", "proposal due", "submission deadline", "due date"),
    },
    "opportunity_location": {
        "prompt_label": "remote / location constraint",
        "card_fields": ("remote_location_constraint", "location"),
        "markers": ("location", "remote", "timezone", "region", "country", "hybrid", "on-site", "onsite"),
    },
    "skills_required": {
        "prompt_label": "skills required",
        "card_fields": ("skills_required",),
        "markers": ("skills", "requirements", "tools", "tech stack", "must have", "experience with"),
    },
    "industry": {
        "prompt_label": "industry / niche",
        "card_fields": ("industry",),
        "markers": ("industry", "niche", "sector", "vertical", "domain"),
    },
    "contact_url": {
        "prompt_label": "contact / proposal url",
        "card_fields": ("contact_url",),
        "markers": ("contact", "submit proposal", "proposal link", "brief link", "reach out", "apply"),
    },
}

OPPORTUNITY_NOTIFICATION_FIELD_ORDER: tuple[str, ...] = (
    "title",
    "counterparty",
    "payment_terms",
    "engagement_summary",
    "timeline_summary",
    "skills_required",
    "opportunity_location",
    "scope_summary",
)


def prompt_field_dictionary() -> dict[str, dict[str, object]]:
    return {
        field_key: {
            "prompt_label": str(spec.get("prompt_label") or field_key),
            "markers": tuple(str(marker) for marker in spec.get("markers", ()) if str(marker).strip()),
        }
        for field_key, spec in OPPORTUNITY_FIELD_DICTIONARY.items()
    }


def opportunity_card_value(card: "JobCard", field_key: str) -> str:
    if field_key == "counterparty":
        return str(card.counterparty or getattr(card, "company", "") or "").strip()
    if field_key == "payment_terms":
        return str(card.payment_terms or getattr(card, "salary", "") or "").strip()
    if field_key == "engagement_summary":
        return " / ".join(part for part in (card.engagement_type, card.commitment_level) if str(part or "").strip()).strip()
    if field_key == "timeline_summary":
        return " / ".join(part for part in (card.start_timeline, card.duration) if str(part or "").strip()).strip()
    if field_key == "opportunity_location":
        return str(card.opportunity_location or getattr(card, "location", "") or "").strip()

    spec = OPPORTUNITY_FIELD_DICTIONARY.get(field_key, {})
    for attribute in spec.get("card_fields", (field_key,)):
        raw = getattr(card, str(attribute), "")
        cleaned = str(raw or "").strip()
        if cleaned and cleaned.lower() not in {"unknown", "not specified"}:
            return cleaned
    return ""
