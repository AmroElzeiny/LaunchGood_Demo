from __future__ import annotations

import json
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


def _clean_float(value: Any, default: float = 0.0) -> float:
    try:
        return max(0.0, min(float(value), 1.0))
    except (TypeError, ValueError):
        return default


def _clean_bool(value: Any, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "y", "valid"}:
            return True
        if normalized in {"false", "0", "no", "n", "invalid"}:
            return False
    return default


def _normalize_field_name(value: Any) -> str:
    field_name = _clean_text(value).lower().replace(" ", "_")
    aliases = {
        "audience": "target_audience",
        "what_the_organization_does": "what_they_do",
        "services_or_products": "services_products",
        "risk_or_uncertainty_flags": "risk_uncertainty_flags",
        "recommended_next_action": "recommended_action",
        "possible_collaboration_angle": "outreach_angle",
        "fit_signals": "signals_of_fit",
    }
    return aliases.get(field_name, field_name)


@dataclass(slots=True)
class EvidenceSnippet:
    evidence_id: str
    claim: str
    quote: str
    source_url: str
    source_title: str
    chunk_id: str
    supports_field: str
    confidence: float = 0.0
    evidence_type: str = "direct"
    valid: bool = True

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EvidenceSnippet":
        return cls(
            evidence_id=_clean_text(data.get("evidence_id")),
            claim=_clean_text(data.get("claim")),
            quote=_clean_text(data.get("quote")),
            source_url=_clean_text(data.get("source_url")),
            source_title=_clean_text(data.get("source_title")),
            chunk_id=_clean_text(data.get("chunk_id")),
            supports_field=_normalize_field_name(data.get("supports_field")),
            confidence=_clean_float(data.get("confidence")),
            evidence_type=_clean_text(data.get("evidence_type"), "direct"),
            valid=_clean_bool(data.get("valid", True)),
        )


def _clean_evidence_snippets(value: Any) -> list[EvidenceSnippet]:
    if not isinstance(value, list):
        return []
    snippets: list[EvidenceSnippet] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        snippet = EvidenceSnippet.from_dict(item)
        if snippet.evidence_id and snippet.quote and snippet.valid:
            snippets.append(snippet)
    return snippets


def _clean_field_evidence_map(value: Any, valid_ids: set[str]) -> dict[str, list[str]]:
    if not isinstance(value, dict):
        return {}
    cleaned: dict[str, list[str]] = {}
    for raw_key, raw_ids in value.items():
        key = _normalize_field_name(raw_key)
        if not key:
            continue
        if isinstance(raw_ids, str):
            ids = [raw_ids]
        elif isinstance(raw_ids, list):
            ids = raw_ids
        else:
            ids = []
        cleaned_ids = []
        for raw_id in ids:
            evidence_id = _clean_text(raw_id)
            if (
                evidence_id
                and evidence_id in valid_ids
                and evidence_id not in cleaned_ids
            ):
                cleaned_ids.append(evidence_id)
        if cleaned_ids:
            cleaned[key] = cleaned_ids
    return cleaned


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
    evidence_snippets: list[EvidenceSnippet] = field(default_factory=list)
    field_evidence_map: dict[str, list[str]] = field(default_factory=dict)
    weak_evidence_fields: list[str] = field(default_factory=list)
    evidence_validation_summary: dict[str, Any] = field(default_factory=dict)
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
            data.get("company")
            or data.get("organization_name")
            or data.get("organization")
            or data.get("name"),
            default=_clean_text(website, "Unknown"),
        )
        ai_summary = _clean_text(
            data.get("ai_summary")
            or data.get("summary")
            or data.get("reasoning_summary")
        )
        reason = _clean_text(
            data.get("reason") or data.get("why_relevant") or ai_summary,
            default="No reason provided.",
        )
        outreach_angle = _clean_text(
            data.get("outreach_angle") or data.get("possible_collaboration_angle")
        )
        recommended_action = _clean_text(
            data.get("recommended_action") or data.get("recommended_next_action")
        )
        evidence_snippets = _clean_evidence_snippets(data.get("evidence_snippets"))
        valid_evidence_ids = {snippet.evidence_id for snippet in evidence_snippets}
        field_evidence_map = _clean_field_evidence_map(
            data.get("field_evidence_map"), valid_evidence_ids
        )
        return cls(
            company=company,
            website=_clean_text(data.get("website") or website),
            category=_clean_text(data.get("category"), "Unknown"),
            fit_score=fit_score,
            fit_status=fit_status,
            reason=reason,
            pain_point=_clean_text(data.get("pain_point"), "Unknown"),
            suggested_offer=_clean_text(data.get("suggested_offer"), "Unknown"),
            recommended_contact_type=_clean_text(
                data.get("recommended_contact_type"), "Unknown"
            ),
            confidence=_clean_text(data.get("confidence"), "Unknown"),
            next_step=_clean_text(
                data.get("next_step") or recommended_action, "Needs human decision"
            ),
            country_region=_clean_text(
                data.get("country_region") or data.get("country") or data.get("region")
            ),
            target_audience=_clean_text(
                data.get("target_audience") or data.get("audience")
            ),
            what_they_do=_clean_text(
                data.get("what_they_do") or data.get("what_the_organization_does")
            ),
            services_products=_clean_text(
                data.get("services_products")
                or data.get("services_or_products")
                or data.get("services")
                or data.get("products")
            ),
            signals_of_fit=_clean_list(
                data.get("signals_of_fit") or data.get("fit_signals")
            ),
            missing_information=_clean_list(
                data.get("missing_information") or data.get("missing_info")
            ),
            possible_collaboration_angle=_clean_text(
                data.get("possible_collaboration_angle") or outreach_angle
            ),
            risk_uncertainty_flags=_clean_list(
                data.get("risk_uncertainty_flags")
                or data.get("risk_or_uncertainty_flags")
                or data.get("uncertainty_flags")
                or data.get("risk_flags")
            ),
            recommended_action=recommended_action,
            outreach_angle=outreach_angle,
            contact_email=_clean_text(data.get("contact_email") or data.get("email")),
            contact_url=_clean_text(
                data.get("contact_url")
                or data.get("linkedin_or_contact_url")
                or data.get("linkedin_url")
            ),
            ai_summary=ai_summary,
            why_relevant=_clean_text(data.get("why_relevant") or reason),
            ai_reasoning_summary=_clean_text(
                data.get("ai_reasoning_summary") or data.get("reasoning") or ai_summary
            ),
            evidence_snippets=evidence_snippets,
            field_evidence_map=field_evidence_map,
            weak_evidence_fields=_clean_list(data.get("weak_evidence_fields")),
            evidence_validation_summary=dict(
                data.get("evidence_validation_summary") or {}
            ),
            pages_scraped=list(pages_scraped),
            source_session_id=session_id,
        )

    @property
    def uncertainty_flags(self) -> list[str]:
        return list(self.risk_uncertainty_flags)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def compact_evidence_summary(self, *, limit: int = 2400) -> str:
        payload = {
            "evidence_snippets_json": [
                {
                    "evidence_id": snippet.evidence_id,
                    "quote": snippet.quote,
                    "source_url": snippet.source_url,
                    "supports_field": snippet.supports_field,
                    "confidence": snippet.confidence,
                }
                for snippet in self.evidence_snippets[:8]
            ],
            "pages_scraped": len(self.pages_scraped),
            "scraped_page_urls": self.pages_scraped,
            "weak_evidence_fields": self.weak_evidence_fields[:12],
        }
        text = json.dumps(payload, ensure_ascii=False)
        if len(text) <= limit:
            return text
        return text[: max(0, limit - 3)] + "..."

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
        evidence_summary = self.compact_evidence_summary()
        ai_summary = self.ai_summary
        if evidence_summary:
            ai_summary = f"{ai_summary}\nEvidence: {evidence_summary}".strip()

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
            ai_summary,
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
