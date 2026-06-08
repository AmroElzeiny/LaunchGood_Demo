from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


def _clean_text(value: Any, default: str = "") -> str:
    cleaned = " ".join(str(value or "").split()).strip()
    return cleaned or default


def _clean_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [_clean_text(item) for item in value if _clean_text(item)]
    if isinstance(value, str):
        return [_clean_text(part) for part in value.split(";") if _clean_text(part)]
    return []


@dataclass(slots=True)
class ProspectCard:
    company: str
    website: str
    category: str
    fit_score: int
    fit_status: str
    reason: str
    pain_point: str
    suggested_offer: str
    recommended_contact_type: str
    confidence: str
    next_step: str
    country_region: str = ""
    target_audience: str = ""
    what_they_do: str = ""
    services_products: str = ""
    signals_of_fit: list[str] = field(default_factory=list)
    missing_information: list[str] = field(default_factory=list)
    possible_collaboration_angle: str = ""
    risk_uncertainty_flags: list[str] = field(default_factory=list)
    recommended_action: str = ""
    outreach_angle: str = ""
    contact_email: str = ""
    contact_url: str = ""
    ai_summary: str = ""
    why_relevant: str = ""
    ai_reasoning_summary: str = ""
    pages_scraped: list[str] = field(default_factory=list)
    source_session_id: str = ""

    @classmethod
    def from_ai_json(
        cls,
        data: dict[str, Any],
        *,
        website: str,
        fit_status: str,
        pages_scraped: list[str],
        session_id: str,
    ) -> "ProspectCard":
        raw_score = data.get("fit_score", data.get("score", 0))
        try:
            fit_score = int(float(raw_score))
        except (TypeError, ValueError):
            fit_score = 0
        fit_score = max(0, min(fit_score, 100))

        company = _clean_text(
            data.get("company") or data.get("organization_name") or data.get("organization") or data.get("name"),
            default=_clean_text(website, "Unknown"),
        )
        ai_summary = _clean_text(data.get("ai_summary") or data.get("summary") or data.get("reasoning_summary"))
        reason = _clean_text(data.get("reason") or data.get("why_relevant") or ai_summary, default="No reason provided.")
        outreach_angle = _clean_text(data.get("outreach_angle") or data.get("possible_collaboration_angle"))
        recommended_action = _clean_text(data.get("recommended_action") or data.get("recommended_next_action"))
        return cls(
            company=company,
            website=_clean_text(data.get("website") or website),
            category=_clean_text(data.get("category"), "Unknown"),
            fit_score=fit_score,
            fit_status=fit_status,
            reason=reason,
            pain_point=_clean_text(data.get("pain_point"), "Unknown"),
            suggested_offer=_clean_text(data.get("suggested_offer"), "Unknown"),
            recommended_contact_type=_clean_text(data.get("recommended_contact_type"), "Unknown"),
            confidence=_clean_text(data.get("confidence"), "Unknown"),
            next_step=_clean_text(data.get("next_step") or recommended_action, "Needs human decision"),
            country_region=_clean_text(data.get("country_region") or data.get("country") or data.get("region")),
            target_audience=_clean_text(data.get("target_audience") or data.get("audience")),
            what_they_do=_clean_text(data.get("what_they_do")),
            services_products=_clean_text(data.get("services_products") or data.get("services") or data.get("products")),
            signals_of_fit=_clean_list(data.get("signals_of_fit") or data.get("fit_signals")),
            missing_information=_clean_list(data.get("missing_information") or data.get("missing_info")),
            possible_collaboration_angle=_clean_text(data.get("possible_collaboration_angle") or outreach_angle),
            risk_uncertainty_flags=_clean_list(
                data.get("risk_uncertainty_flags") or data.get("uncertainty_flags") or data.get("risk_flags")
            ),
            recommended_action=recommended_action,
            outreach_angle=outreach_angle,
            contact_email=_clean_text(data.get("contact_email") or data.get("email")),
            contact_url=_clean_text(data.get("contact_url") or data.get("linkedin_or_contact_url") or data.get("linkedin_url")),
            ai_summary=ai_summary,
            why_relevant=_clean_text(data.get("why_relevant") or reason),
            ai_reasoning_summary=_clean_text(data.get("ai_reasoning_summary") or data.get("reasoning") or ai_summary),
            pages_scraped=list(pages_scraped),
            source_session_id=session_id,
        )

    @property
    def uncertainty_flags(self) -> list[str]:
        return list(self.risk_uncertainty_flags)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_sheet_row(
        self,
        *,
        human_decision: str,
        crm_stage: str,
        outreach_goal: str,
        target_criteria: str,
        created_at: str = "",
        telegram_or_slack_alert_sent: str = "",
        notes: str = "",
        outreach_subject: str = "",
        outreach_body: str = "",
        recipient_email: str = "",
    ) -> list[Any]:
        return [
            created_at,
            self.website,
            self.company,
            self.category,
            self.country_region,
            self.target_audience,
            self.fit_score,
            self.fit_status,
            crm_stage,
            self.ai_summary,
            self.why_relevant,
            "; ".join(self.risk_uncertainty_flags),
            self.recommended_action or self.next_step,
            outreach_goal,
            self.outreach_angle or self.possible_collaboration_angle,
            self.contact_email,
            self.contact_url,
            human_decision,
            telegram_or_slack_alert_sent,
            notes,
            outreach_subject,
            outreach_body,
            recipient_email,
        ]
