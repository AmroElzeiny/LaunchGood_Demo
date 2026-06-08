from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any
from urllib.parse import urlsplit

from openai import OpenAI

from job_bot.openai_compat import create_chat_completion_with_fallback

try:
    from prospect_system.dashboard_state import (
        EvidenceChunk,
        ProspectAnalysisState,
        ScrapedPage,
    )

    from prospect_system.errors import ProspectAIError
    from prospect_system.prospect_card import EvidenceSnippet, ProspectCard
    from prospect_system.prospect_config import ProspectSettings
    from prospect_system.prospect_scraper import extract_contact_email
except ImportError:  # pragma: no cover - direct script execution fallback
    from dashboard_state import EvidenceChunk, ProspectAnalysisState, ScrapedPage

    from errors import ProspectAIError
    from prospect_card import EvidenceSnippet, ProspectCard

    from prospect_config import ProspectSettings
    from prospect_scraper import extract_contact_email


def _safe_json_loads(text: str) -> dict[str, Any]:
    cleaned = str(text or "").strip()
    if not cleaned:
        return {}
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError:
                return {}
    return {}


def _clean_text(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _clean_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [_clean_text(item) for item in value if _clean_text(item)]
    if isinstance(value, str):
        return [_clean_text(part) for part in value.split(";") if _clean_text(part)]
    return []


def _remove_dash_punctuation(value: str) -> str:
    text = str(value or "")
    text = re.sub(r"(?m)^\s*[-\u2010-\u2015\u2212]\s+", "", text)
    text = re.sub(r"\s+[-\u2010-\u2015\u2212]\s+", ", ", text)
    text = re.sub(r"[\u2010-\u2015\u2212]", ", ", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\s+,", ",", text)
    return text.strip()


def _format_email_text(value: Any) -> str:
    return _remove_dash_punctuation(_clean_text(value))


def _format_email_body(value: Any) -> str:
    body = str(value or "").strip()
    if not body:
        return ""
    body = _remove_dash_punctuation(body)
    body = body.replace("\\n", "\n").replace("\r\n", "\n").replace("\r", "\n")
    body = re.sub(r"<br\s*/?>", "\n", body, flags=re.IGNORECASE)
    body = re.sub(r"</p>\s*<p[^>]*>", "\n\n", body, flags=re.IGNORECASE)
    body = re.sub(r"</?p[^>]*>", "", body, flags=re.IGNORECASE)
    if "\n" not in body:
        sentences = [
            part.strip() for part in re.split(r"(?<=[.!?])\s+", body) if part.strip()
        ]
        if len(sentences) >= 3:
            paragraphs: list[str] = []
            first = sentences[0]
            if re.match(r"^(hi|hello|dear)\b", first, flags=re.IGNORECASE):
                paragraphs.append(first)
                sentences = sentences[1:]
            while sentences:
                paragraphs.append(" ".join(sentences[:2]))
                sentences = sentences[2:]
            body = "\n\n".join(paragraphs)
    lines = [" ".join(line.split()).strip() for line in body.split("\n")]
    normalized = "\n".join(lines)
    normalized = re.sub(r"\n{3,}", "\n\n", normalized)
    return _remove_dash_punctuation(normalized)


def _host(url: str) -> str:
    return urlsplit(str(url or "")).netloc.lower().strip(".")


def _same_site(url_a: str, url_b: str) -> bool:
    host_a = _host(url_a)
    host_b = _host(url_b)
    if not host_a or not host_b:
        return False
    return (
        host_a == host_b
        or host_a.endswith("." + host_b)
        or host_b.endswith("." + host_a)
    )


def _normalize_for_evidence_match(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().lower()


def _very_close_quote_match(quote: str, text: str) -> bool:
    quote_norm = _normalize_for_evidence_match(quote)
    text_norm = _normalize_for_evidence_match(text)
    if not quote_norm or not text_norm:
        return False
    if quote_norm in text_norm:
        return True
    quote_words = quote_norm.split()
    text_words = text_norm.split()
    if len(quote_words) < 5 or len(text_words) < len(quote_words):
        return False
    window_size = min(len(text_words), max(len(quote_words) + 4, len(quote_words)))
    step = max(1, len(quote_words) // 3)
    for start in range(0, min(len(text_words), 2500), step):
        window = " ".join(text_words[start : start + window_size])
        if not window:
            continue
        if SequenceMatcher(None, quote_norm, window).ratio() >= 0.86:
            return True
        if start + window_size >= len(text_words):
            break
    return False


def _normalize_supports_field(value: Any) -> str:
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


def _safe_structural_score(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


@dataclass(slots=True)
class InformationDecision:
    enough_information: bool
    selected_extra_urls: list[str] = field(default_factory=list)
    reason: str = ""
    missing_information: list[str] = field(default_factory=list)
    confidence: float = 0.0


@dataclass(slots=True)
class RankedInternalLink:
    url: str
    priority: int
    reason: str
    expected_information: str
    confidence: float


@dataclass(slots=True)
class LinkRankingDecision:
    ranked_links: list[RankedInternalLink] = field(default_factory=list)
    homepage_enough: bool = False
    reason_homepage_enough: str = ""
    recommended_next_action: str = "scrape_more_pages"


class ProspectAIAnalyzer:
    def __init__(self, settings: ProspectSettings, logger: logging.Logger) -> None:
        self.settings = settings
        self.logger = logger
        self.client = (
            OpenAI(api_key=settings.openai_api_key) if settings.openai_api_key else None
        )

    def build_evidence_chunks(
        self,
        state: ProspectAnalysisState,
        *,
        website: str,
        chunk_size: int = 850,
        overlap: int = 80,
    ) -> list[EvidenceChunk]:
        chunks: list[EvidenceChunk] = []
        chunk_index = 1
        for page in self._pages_for_site(state, website):
            text = str(page.text or "").strip()
            if not text:
                continue
            text = re.sub(r"\n{3,}", "\n\n", text)
            cursor = 0
            while cursor < len(text):
                end = min(len(text), cursor + max(300, chunk_size))
                if end < len(text):
                    boundary_candidates = [
                        text.rfind("\n\n", cursor + 450, end),
                        text.rfind(". ", cursor + 450, end),
                        text.rfind("! ", cursor + 450, end),
                        text.rfind("? ", cursor + 450, end),
                    ]
                    boundary = max(boundary_candidates)
                    if boundary > cursor:
                        end = min(len(text), boundary + 1)
                chunk_text = _clean_text(text[cursor:end])
                if len(chunk_text) >= 80:
                    chunks.append(
                        EvidenceChunk(
                            chunk_id=f"chunk_{chunk_index:03d}",
                            page_url=page.final_url or page.url,
                            page_title=page.title,
                            text=chunk_text,
                            start_index=cursor,
                            end_index=end,
                        )
                    )
                    chunk_index += 1
                if end >= len(text):
                    break
                cursor = max(end - max(0, overlap), cursor + 1)
                if len(chunks) >= 90:
                    return chunks
        return chunks

    def assess_information_need(
        self,
        state: ProspectAnalysisState,
        *,
        website: str,
        candidate_links: list[dict[str, Any]],
    ) -> InformationDecision:
        ranking = self.rank_internal_links(
            state, website=website, candidate_links=candidate_links
        )
        return InformationDecision(
            enough_information=ranking.homepage_enough,
            selected_extra_urls=[link.url for link in ranking.ranked_links],
            reason=ranking.reason_homepage_enough,
            missing_information=[],
            confidence=max(
                [link.confidence for link in ranking.ranked_links], default=0.0
            ),
        )

    def rank_internal_links(
        self,
        state: ProspectAnalysisState,
        *,
        website: str,
        candidate_links: list[dict[str, Any]],
    ) -> LinkRankingDecision:
        if self.client is None or not self.settings.ai_model:
            return self._fallback_link_ranking(
                state, website=website, candidate_links=candidate_links
            )
        remaining_page_budget = max(
            0,
            self.settings.max_pages_to_scrape
            - len(self._pages_for_site(state, website)),
        )
        visible_candidate_links = candidate_links[:80]
        payload = {
            "website": website,
            "target_criteria": state.target_criteria,
            "outreach_goal": state.outreach_goal,
            "scraped_pages": self._pages_payload(
                state, website=website, text_limit=3500
            ),
            "candidate_internal_links": visible_candidate_links,
            "max_pages_to_scrape": self.settings.max_pages_to_scrape,
            "scraped_page_count": len(self._pages_for_site(state, website)),
            "remaining_page_budget": remaining_page_budget,
            "already_scraped_urls": [
                page.final_url
                for page in state.scraped_pages
                if _same_site(page.final_url, website)
            ],
            "return_format": {
                "ranked_links": [
                    {
                        "url": "exact_absolute_url_from_candidate_internal_links",
                        "priority": "integer_1_is_highest",
                        "reason": "short_string",
                        "expected_information": "short_string",
                        "confidence": "float_0_to_1",
                    }
                ],
                "homepage_enough": "bool",
                "reason_homepage_enough": "short_string",
                "recommended_next_action": "scrape_more_pages|create_card",
            },
        }
        system_prompt = (
            "You are a strict, evidence-based AI prospect discovery analyst ranking discovered internal links for the next "
            "scraping step. Review scraped_pages, target_criteria, outreach_goal, candidate_internal_links, already_scraped_urls, "
            "and the remaining page budget. Return JSON only. Do not include markdown, comments, explanations outside JSON, "
            "extra keys, trailing commas, or null values. Output must be valid JSON parseable by Python json.loads(). "
            "Use only the discovered link objects provided in candidate_internal_links. Do not browse, invent URLs, rewrite URLs, "
            "normalize URLs, use outside knowledge, or select URLs not copied exactly from candidate_internal_links. "
            "Your semantic goal is to choose pages most likely to improve a prospect analysis by revealing what the organization "
            "or company does, who it serves, its services, products, programs, positioning, audience, beneficiaries, customers, "
            "partnership or contact information, credibility or trust signals, and uncertainty or risk signals. These are semantic "
            "evidence goals, not path-name rules. Do not rely on hardcoded page names or path patterns. Base ranking on the full "
            "link metadata: anchor_text, title, aria_label, surrounding_text, dom_context, source_page, path, and the text already "
            "scraped. Prefer links whose metadata suggests useful evidence; ignore links that look duplicated, thin, navigational "
            "only, transactional, account-related, blocked, file/media-oriented, or irrelevant. "
            "Set homepage_enough to true only when the currently scraped pages already contain enough reliable evidence to create "
            "a confident prospect card for human review. If homepage_enough is true, ranked_links must be an empty array and "
            "recommended_next_action must be create_card. If homepage_enough is false, recommended_next_action must be "
            "scrape_more_pages and ranked_links should contain only the highest-value links, ordered by priority, with no more "
            "items than remaining_page_budget. If no useful links are available, ranked_links must be an empty array and the "
            "reason_homepage_enough should explain that analysis will continue with available evidence and uncertainty. "
            "Each ranked_links item must include url, priority, reason, expected_information, and confidence. priority 1 is the "
            "highest priority. confidence must be a number from 0.0 to 1.0."
        )
        try:
            data = self._chat_json(system_prompt, payload, stage="link_ranking")
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("[prospect-ai] link ranking failed: %s", str(exc))
            return self._fallback_link_ranking(
                state, website=website, candidate_links=candidate_links
            )
        allowed_urls = {
            str(item.get("url") or "").strip()
            for item in visible_candidate_links
            if str(item.get("url") or "").strip()
        }
        ranked_links: list[RankedInternalLink] = []
        seen: set[str] = set()
        for item in data.get("ranked_links", []) or []:
            if not isinstance(item, dict):
                continue
            url = _clean_text(item.get("url"))
            if not url or url not in allowed_urls or url in seen:
                continue
            seen.add(url)
            try:
                priority = max(
                    1, int(float(item.get("priority", len(ranked_links) + 1)))
                )
            except (TypeError, ValueError):
                priority = len(ranked_links) + 1
            ranked_links.append(
                RankedInternalLink(
                    url=url,
                    priority=priority,
                    reason=_clean_text(item.get("reason"))
                    or "AI selected this page as potentially useful evidence.",
                    expected_information=_clean_text(item.get("expected_information")),
                    confidence=self._as_float(item.get("confidence")),
                )
            )
        ranked_links = sorted(
            ranked_links, key=lambda link: (link.priority, -link.confidence)
        )[:remaining_page_budget]
        homepage_enough = self._as_bool(data.get("homepage_enough"))
        recommended_next_action = _clean_text(data.get("recommended_next_action"))
        if recommended_next_action == "create_card":
            homepage_enough = True
        if homepage_enough:
            recommended_next_action = "create_card"
            ranked_links = []
        elif recommended_next_action not in {"scrape_more_pages", "create_card"}:
            recommended_next_action = "scrape_more_pages"
        return LinkRankingDecision(
            ranked_links=ranked_links,
            homepage_enough=homepage_enough,
            reason_homepage_enough=_clean_text(data.get("reason_homepage_enough"))
            or "AI link ranking completed.",
            recommended_next_action=recommended_next_action,
        )

    def create_prospect_card(
        self,
        state: ProspectAnalysisState,
        *,
        website: str,
        reanalysis_instruction: str = "",
        evidence_chunks: list[EvidenceChunk] | None = None,
    ) -> ProspectCard:
        pages = self._pages_for_site(state, website)
        evidence_chunks = (
            evidence_chunks
            if evidence_chunks is not None
            else self.build_evidence_chunks(state, website=website)
        )

        if self.client is None or not self.settings.ai_model:
            return self._fallback_card(
                state,
                website=website,
                reason="AI credentials are missing.",
                evidence_chunks=evidence_chunks,
            )
        payload = {
            "website": website,
            "target_criteria": state.target_criteria,
            "outreach_goal": state.outreach_goal,
            "reanalysis_instruction": reanalysis_instruction,
            "scraped_pages": self._pages_payload(
                state, website=website, text_limit=700
            ),
            "evidence_chunks": [
                {
                    "chunk_id": chunk.chunk_id,
                    "page_url": chunk.page_url,
                    "page_title": chunk.page_title,
                    "text": chunk.text,
                    "start_index": chunk.start_index,
                    "end_index": chunk.end_index,
                }
                for chunk in evidence_chunks[:70]
            ],
            "return_format": {
                "company": "string",
                "website": "absolute_url",
                "category": "string",
                "country_region": "string_or_unknown",
                "target_audience": "string_or_unknown",
                "what_they_do": "string",
                "services_products": "string",
                "fit_score": "integer_0_to_100",
                "reason": "string",
                "why_relevant": "string",
                "pain_point": "string_or_unknown",
                "suggested_offer": "string_or_unknown",
                "recommended_contact_type": "string_or_unknown",
                "confidence": "High|Medium|Low plus short reason",
                "signals_of_fit": ["string"],
                "missing_information": ["string"],
                "possible_collaboration_angle": "string",
                "risk_uncertainty_flags": ["string"],
                "recommended_action": "string",
                "outreach_angle": "string",
                "contact_email": "email_or_empty",
                "contact_url": "absolute_url_or_empty",
                "next_step": "string",
                "ai_summary": "short_summary",
                "ai_reasoning_summary": "short_reasoning_summary",
                "evidence_snippets": [
                    {
                        "evidence_id": "ev_001",
                        "claim": "string",
                        "quote": "exact_short_quote_from_evidence_chunks",
                        "source_url": "absolute_url_from_chunk",
                        "source_title": "string",
                        "chunk_id": "chunk_001",
                        "supports_field": "field_name",
                        "confidence": "float_0_to_1",
                    }
                ],
                "field_evidence_map": {
                    "category": ["ev_001"],
                    "audience": ["ev_002"],
                    "signals_of_fit": ["ev_003"],
                    "risk_or_uncertainty_flags": ["ev_004"],
                    "suggested_offer": ["ev_005"],
                },
            },
        }
        system_prompt = (
            "You are a strict, evidence-based AI prospect analyst creating CRM-ready prospect cards for human review. "
            "Your task is to evaluate the scraped website content against the provided target_criteria and outreach_goal, "
            "then produce one complete prospect card. Return JSON only. Do not include markdown, comments, explanations "
            "outside JSON, or extra text. The output must be valid JSON parseable by Python json.loads(). Do not include "
            "trailing commas or null values. Use strings, integers, and arrays exactly as requested by return_format. "
            "Use only the scraped website evidence provided in scraped_pages. Do not use outside knowledge, assumptions, "
            "inferred facts, or web browsing. Do not fabricate company details, contact details, emails, names, services, "
            "locations, partnerships, funding, intent, pain points, buying readiness, or approvals. Prefer 'Unknown' or "
            "an empty string over guessing. If information is missing, unclear, contradictory, weakly supported, blocked, "
            "generic, or mostly unreadable, state that clearly in missing_information and risk_uncertainty_flags, lower "
            "fit_score, and explain why. Do not mark a prospect as approved; final approval must remain a human decision. "
            "Be conservative with scoring: high scores require multiple direct pieces of scraped evidence, not vague keyword "
            "matches alone. Every major conclusion must be supported by scraped website evidence. "
            "Evaluation method: compare the organization's apparent business, audience, services, mission, industry, and "
            "needs against target_criteria; compare the likely outreach angle against outreach_goal; identify whether the "
            "organization has a supported pain point, relevant need, and plausible reason to engage; separate confirmed "
            "evidence from uncertainty. "
            "Fit score guidance: 90-100 means very strong fit with multiple direct evidence points, clear pain point, and "
            "strong outreach relevance. 75-89 means strong fit with good evidence but some missing or less specific details. "
            "55-74 means medium fit with partial match or plausible relevance but incomplete important evidence. 35-54 means "
            "weak fit with limited overlap or weak evidence. 0-34 means poor fit or not enough reliable evidence to justify "
            "outreach. The application derives fit_status from fit_score, so do not rely on a separate fit_status field. "
            "Confidence guidance: use 'High - ...' when evidence is clear, specific, and consistent; 'Medium - ...' when "
            "evidence supports the conclusion but has gaps or ambiguity; 'Low - ...' when content is thin, generic, unclear, "
            "blocked, or only indirectly relevant. "
            "Field requirements: company must be the organization name only if clearly found, otherwise 'Unknown'. website "
            "must be the analyzed website URL. category, country_region, target_audience, what_they_do, and services_products "
            "must be based only on evidence. reason explains fit or non-fit. why_relevant explains the strongest relevance "
            "to target_criteria. pain_point must be evidence-supported or 'Unknown'. suggested_offer and outreach_angle must "
            "align with outreach_goal and evidence without inventing needs. recommended_contact_type should be a role type, "
            "not a specific person unless clearly shown. contact_email and contact_url must only contain values directly "
            "found in scraped evidence, otherwise use an empty string. signals_of_fit should list concise evidence-backed "
            "signals. missing_information should list important absent facts that would improve the decision. "
            "risk_uncertainty_flags should list specific uncertainty reasons. recommended_action and next_step should be "
            "human-review actions such as review, gather more information, approve for CRM consideration, or reject; they "
            "must not claim final approval. ai_summary should summarize the organization and fit in 1-2 sentences. "
            "ai_reasoning_summary should summarize the evidence-based reasoning in 2-4 concise sentences. "
            "Evidence requirements: You must use evidence_chunks as the source for evidence_snippets. Each quote must be an "
            "exact short quote copied from an evidence chunk, not a paraphrase. Do not invent quotes. Each evidence snippet "
            "must connect one claim to one source chunk and one supports_field. Use supports_field values matching the fields "
            "in the prospect card, such as category, target_audience, what_they_do, services_products, signals_of_fit, "
            "risk_uncertainty_flags, suggested_offer, outreach_angle, pain_point, recommended_contact_type, or why_relevant. "
            "If direct evidence is missing for a field, do not fabricate evidence; leave that field out of field_evidence_map "
            "and include the gap in missing_information or risk_uncertainty_flags. field_evidence_map must reference only "
            "evidence IDs that appear in evidence_snippets."
        )
        try:
            data = self._chat_json(system_prompt, payload, stage="prospect_card")
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("[prospect-ai] prospect card failed: %s", str(exc))
            return self._fallback_card(
                state, website=website, reason=str(exc), evidence_chunks=evidence_chunks
            )

        try:
            score = int(float(data.get("fit_score", 0)))
        except (TypeError, ValueError):
            score = 0
        score = max(0, min(score, 100))
        fit_status = self.settings.fit_status_for_score(score)
        if not data.get("contact_email"):
            data["contact_email"] = extract_contact_email(
                "\n".join(page.text for page in pages),
                "\n".join(page.metadata.get("html_excerpt", "") for page in pages),
            )
        data = self._attach_validated_evidence(
            data, evidence_chunks=evidence_chunks, state=state, website=website
        )

        return ProspectCard.from_ai_json(
            data,
            website=website,
            fit_status=fit_status,
            pages_scraped=[page.final_url for page in pages],
            session_id=state.session_id,
        )

    def _attach_validated_evidence(
        self,
        data: dict[str, Any],
        *,
        evidence_chunks: list[EvidenceChunk],
        state: ProspectAnalysisState,
        website: str,
    ) -> dict[str, Any]:
        returned_items = (
            data.get("evidence_snippets", [])
            if isinstance(data.get("evidence_snippets"), list)
            else []
        )
        chunk_lookup = {chunk.chunk_id: chunk for chunk in evidence_chunks}
        valid_snippets: list[dict[str, Any]] = []
        invalid_count = 0
        for index, raw_item in enumerate(returned_items, start=1):
            if not isinstance(raw_item, dict):
                invalid_count += 1
                continue
            quote = _clean_text(raw_item.get("quote"))[:500]
            if not quote:
                invalid_count += 1
                continue
            chunk_id = _clean_text(raw_item.get("chunk_id"))
            source_chunk = chunk_lookup.get(chunk_id)
            search_chunks = (
                [source_chunk] if source_chunk is not None else evidence_chunks
            )
            matching_chunk = next(
                (
                    chunk
                    for chunk in search_chunks
                    if chunk is not None and _very_close_quote_match(quote, chunk.text)
                ),
                None,
            )
            if matching_chunk is None:
                invalid_count += 1
                continue
            evidence_id = (
                _clean_text(raw_item.get("evidence_id"))
                or f"ev_{len(valid_snippets) + 1:03d}"
            )
            valid_snippets.append(
                {
                    "evidence_id": evidence_id,
                    "claim": _clean_text(raw_item.get("claim")),
                    "quote": quote,
                    "source_url": _clean_text(raw_item.get("source_url"))
                    or matching_chunk.page_url,
                    "source_title": _clean_text(raw_item.get("source_title"))
                    or matching_chunk.page_title,
                    "chunk_id": matching_chunk.chunk_id,
                    "supports_field": _normalize_supports_field(
                        raw_item.get("supports_field")
                    ),
                    "confidence": self._as_float(raw_item.get("confidence")),
                    "evidence_type": "direct",
                    "valid": True,
                }
            )
        valid_ids = {item["evidence_id"] for item in valid_snippets}
        field_map = self._validated_field_evidence_map(
            data.get("field_evidence_map"), valid_ids, valid_snippets
        )
        weak_fields = self._weak_evidence_fields(
            data, field_map, state=state, website=website
        )
        data = dict(data)
        data["evidence_snippets"] = valid_snippets
        data["field_evidence_map"] = field_map
        data["weak_evidence_fields"] = weak_fields
        data["evidence_validation_summary"] = {
            "returned_count": len(returned_items),
            "valid_count": len(valid_snippets),
            "invalid_count": invalid_count,
            "chunk_count": len(evidence_chunks),
            "weak_evidence_fields": weak_fields,
        }
        return data

    def _validated_field_evidence_map(
        self,
        raw_map: Any,
        valid_ids: set[str],
        valid_snippets: list[dict[str, Any]],
    ) -> dict[str, list[str]]:
        mapped: dict[str, list[str]] = {}
        if isinstance(raw_map, dict):
            for raw_field, raw_ids in raw_map.items():
                field_name = _normalize_supports_field(raw_field)
                if isinstance(raw_ids, str):
                    raw_ids = [raw_ids]
                if not isinstance(raw_ids, list):
                    continue
                for raw_id in raw_ids:
                    evidence_id = _clean_text(raw_id)
                    if evidence_id in valid_ids:
                        mapped.setdefault(field_name, [])
                        if evidence_id not in mapped[field_name]:
                            mapped[field_name].append(evidence_id)
        for snippet in valid_snippets:
            field_name = _normalize_supports_field(snippet.get("supports_field"))
            evidence_id = _clean_text(snippet.get("evidence_id"))
            if field_name and evidence_id in valid_ids:
                mapped.setdefault(field_name, [])
                if evidence_id not in mapped[field_name]:
                    mapped[field_name].append(evidence_id)
        return mapped

    def _weak_evidence_fields(
        self,
        data: dict[str, Any],
        field_map: dict[str, list[str]],
        *,
        state: ProspectAnalysisState,
        website: str,
    ) -> list[str]:
        important_fields = (
            "category",
            "target_audience",
            "what_they_do",
            "services_products",
            "signals_of_fit",
            "risk_uncertainty_flags",
            "suggested_offer",
            "outreach_angle",
        )
        weak: list[str] = []
        for field_name in important_fields:
            value = data.get(field_name)
            if field_name == "target_audience":
                value = value or data.get("audience")
            if field_name == "what_they_do":
                value = value or data.get("what_the_organization_does")
            if field_name == "services_products":
                value = value or data.get("services_or_products")
            if field_name == "risk_uncertainty_flags":
                value = value or data.get("risk_or_uncertainty_flags")
            has_value = bool(
                _clean_list(value) if isinstance(value, list) else _clean_text(value)
            )
            if has_value and not field_map.get(field_name):
                weak.append(field_name)
        if not _clean_text(data.get("contact_email")) and not extract_contact_email(
            state.extracted_text
        ):
            weak.append("contact_email")
        pages = self._pages_for_site(state, website)
        if len(pages) <= 1:
            weak.append("only_homepage_available")
        if any(page.blocked for page in getattr(state, "skipped_pages", [])):
            weak.append("some_pages_blocked")
        deduped: list[str] = []
        for field_name in weak:
            if field_name not in deduped:
                deduped.append(field_name)
        return deduped

    def generate_outreach_draft(
        self,
        state: ProspectAnalysisState,
        *,
        card: ProspectCard,
        regenerate_instruction: str = "",
    ) -> dict[str, str]:
        if self.client is None or not self.settings.ai_model:
            return self._fallback_outreach_draft(card, state)
        payload = {
            "target_criteria": state.target_criteria,
            "outreach_goal": state.outreach_goal,
            "prospect_card": card.to_dict(),
            "regenerate_instruction": regenerate_instruction,
            "scraped_page_context": self._pages_payload(
                state, website=card.website, text_limit=3500
            ),
            "return_format": {
                "subject": "string",
                "body": "string",
                "cta": "string",
            },
        }
        system_prompt = (
            "You are a strict, evidence-based outreach copywriter and prospect analyst creating a warm, professional, "
            "human-sounding email draft for CRM review. Your task is to write one natural outreach email based only on "
            "the provided prospect_card, scraped_page_context, target_criteria, outreach_goal, fit_score, confidence, "
            "and optional regenerate_instruction. Return JSON only. Do not include markdown, comments, explanations "
            "outside JSON, or extra text. The output must be valid JSON parseable by Python json.loads(). Do not include "
            "trailing commas or null values. Use only the keys in return_format: subject, body, and cta. "
            "Core evidence rules: use only information provided in scraped_page_context, prospect_card, target_criteria, "
            "outreach_goal, and regenerate_instruction. Do not use outside knowledge, assumptions, web browsing, inferred "
            "company facts, or invented context. Do not fabricate names, roles, contact details, partnerships, funding, "
            "company size, locations, intent, urgency, pain points, buying readiness, prior contact, referrals, meetings, "
            "or approval status. If a detail is not clearly supported, do not mention it. If the evidence is weak, generic, "
            "blocked, contradictory, or incomplete, make the email more cautious and exploratory. Do not claim the recipient "
            "has a confirmed problem unless the evidence directly supports it. Prefer verified evidence_snippets from "
            "prospect_card when personalizing the email. Ignore invalid or missing evidence and use safe inferences only when "
            "the card clearly marks them as uncertain. One specific, evidence-backed observation is better than several weak "
            "assumptions. The email must be suitable for human review before sending. "
            "Fit-score logic: for fit_score 90-100, write a confident, specific email with a clear value connection and "
            "direct but low-pressure CTA. For 75-89, write a confident but not exaggerated email using one strong "
            "evidence-backed reason for relevance. For 55-74, write an exploratory email and frame the offer as potentially "
            "relevant. For 35-54, write a cautious, low-pressure email focused on learning whether there is a fit. For "
            "0-34, do not write a persuasive sales email; keep it conservative and review-oriented, or politely indicate "
            "that outreach may not be recommended inside the body. "
            "Confidence logic: if confidence is High, the email may be specific and direct. If confidence is Medium, use "
            "softer wording such as 'may be relevant', 'could be useful', or 'wanted to ask'. If confidence is Low, avoid "
            "strong claims and keep the CTA focused on confirming relevance. "
            "Human-sounding style requirements: write like a real person sending a thoughtful note after reviewing the "
            "organization's website. Be friendly, professional, restrained, and specific. Keep the body ideally 80-150 "
            "words. Use short paragraphs, natural transitions, and simple language. Make the email unlikely to be perceived "
            "as AI-written by avoiding formulaic structure, generic compliments, inflated praise, sales hype, excessive "
            "adjectives, and overly polished marketing language. Do not use phrases such as 'I hope this email finds you "
            "well', 'I was impressed by', 'game-changing', 'revolutionize', 'unlock', 'seamless', 'cutting-edge', 'leverage', "
            "'synergy', 'tailored solutions', 'at your earliest convenience', or 'I wanted to reach out because'. Do not "
            "mention AI, scraped data, fit score, CRM, prospect card, target criteria, or internal analysis. Do not use fake "
            "familiarity, false urgency, manipulative language, unsupported numbers, testimonials, case studies, or promised "
            "outcomes. Do not ask multiple questions in the CTA. "
            "Punctuation and formatting requirements: do not use dash punctuation, em dashes, en dashes, hyphen bullets, "
            "or hyphenated compounds when a simple alternative exists. Format body as a real email with paragraph breaks "
            "using newline characters. Do not return the body as one long line. Do not use bullet lists. "
            "Email construction: subject should be short, natural, and not clickbait; avoid title case unless it reads "
            "naturally. Opening should mention one specific evidence-backed observation about the organization. Relevance "
            "bridge should connect outreach_goal to the organization's apparent work, audience, services, or mission without "
            "overclaiming. Offer should state the suggested value in plain language. CTA should be a simple, low-friction, "
            "specific question such as asking whether it makes sense to share a short idea, send more details, or connect "
            "with the right person. Include a signature only if sender information is provided; never invent sender name, "
            "company, phone number, or title. "
            "Personalization rules: mention the company or organization name only if clearly provided. Mention a service, "
            "program, audience, mission, or activity only if clearly supported by evidence. Mention a pain point only if "
            "supported; otherwise frame the offer around possible relevance rather than a confirmed problem. If "
            "recommended_contact_type is available, write the email so it is appropriate for that role type, but do not "
            "address a specific person unless the name is provided. Follow regenerate_instruction only when it does not "
            "conflict with evidence rules, JSON requirements, or professional tone. "
            "Field requirements: subject is a short natural email subject. body is the complete email body, ready for human "
            "review, with paragraph breaks. cta is the one core call to action from the email. If evidence is insufficient, "
            "prioritize accuracy over persuasion and make the uncertainty clear in natural language."
        )
        try:
            data = self._chat_json(system_prompt, payload, stage="outreach_draft")
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("[prospect-ai] outreach draft failed: %s", str(exc))
            return self._fallback_outreach_draft(card, state)
        fallback_subject = _format_email_text(
            f"Potential collaboration with {card.company}"
        )
        return {
            "subject": _format_email_text(data.get("subject")) or fallback_subject,
            "body": _format_email_body(data.get("body")),
            "cta": _format_email_text(data.get("cta"))
            or "Would you be open to a quick reply if this is relevant?",
        }

    def _chat_json(
        self, system_prompt: str, payload: dict[str, Any], *, stage: str
    ) -> dict[str, Any]:
        if self.client is None:
            raise ProspectAIError("OPENAI_API_KEY is missing.")
        response = create_chat_completion_with_fallback(
            self.client.chat.completions.create,
            model=self.settings.ai_model,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            logger=self.logger,
            log_label="prospect-ai",
            timeout_seconds=self.settings.openai_request_timeout_seconds,
            retry_budget=self.settings.ai_retry_budget,
            backoff_base_seconds=self.settings.ai_backoff_base_seconds,
        )
        content = response.choices[0].message.content or "{}"
        data = _safe_json_loads(content)
        if not data:
            raise ProspectAIError(f"AI returned invalid JSON for {stage}.")
        return data

    @staticmethod
    def _as_bool(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value != 0
        if isinstance(value, str):
            return value.strip().lower() in {"true", "yes", "1", "enough"}
        return False

    @staticmethod
    def _as_float(value: Any) -> float:
        try:
            return max(0.0, min(float(value), 1.0))
        except (TypeError, ValueError):
            return 0.0

    def _pages_for_site(
        self, state: ProspectAnalysisState, website: str
    ) -> list[ScrapedPage]:
        pages = [
            page
            for page in state.scraped_pages
            if _same_site(page.final_url or page.url, website)
        ]
        return pages or list(state.scraped_pages)

    def _pages_payload(
        self, state: ProspectAnalysisState, *, website: str, text_limit: int
    ) -> list[dict[str, Any]]:
        payload: list[dict[str, Any]] = []
        for page in self._pages_for_site(state, website):
            payload.append(
                {
                    "url": page.final_url or page.url,
                    "title": page.title,
                    "text_excerpt": page.text_excerpt(text_limit),
                    "metadata": page.metadata,
                    "links": page.links[:30],
                    "discovered_links_count": len(page.discovered_links or page.links),
                    "selected_for_reason": page.selected_for_reason,
                    "blocked": page.blocked,
                    "error": page.error,
                    "status": page.status,
                    "status_code": page.status_code,
                    "strategy": page.strategy,
                }
            )
        return payload

    def _fallback_information_decision(
        self,
        state: ProspectAnalysisState,
        *,
        website: str,
        candidate_links: list[dict[str, Any]],
    ) -> InformationDecision:
        ranking = self._fallback_link_ranking(
            state, website=website, candidate_links=candidate_links
        )

        return InformationDecision(
            enough_information=ranking.homepage_enough,
            selected_extra_urls=[link.url for link in ranking.ranked_links],
            reason=ranking.reason_homepage_enough,
            missing_information=["AI information sufficiency check did not run."],
            confidence=max(
                [link.confidence for link in ranking.ranked_links], default=0.25
            ),
        )

    def _fallback_link_ranking(
        self,
        state: ProspectAnalysisState,
        *,
        website: str,
        candidate_links: list[dict[str, Any]],
    ) -> LinkRankingDecision:
        pages = self._pages_for_site(state, website)
        text_length = sum(len(page.text or "") for page in pages)
        remaining_page_budget = max(0, self.settings.max_pages_to_scrape - len(pages))
        homepage_enough = (
            text_length >= 1800 or len(pages) >= self.settings.max_pages_to_scrape
        )
        if homepage_enough or remaining_page_budget <= 0:
            return LinkRankingDecision(
                ranked_links=[],
                homepage_enough=True,
                reason_homepage_enough="Local fallback found enough readable evidence or no remaining page budget.",
                recommended_next_action="create_card",
            )
        ranked_links: list[RankedInternalLink] = []
        ordered_candidates = sorted(
            candidate_links,
            key=lambda value: _safe_structural_score(value.get("structural_score")),
            reverse=True,
        )
        for item in ordered_candidates[:remaining_page_budget]:
            url = _clean_text(item.get("url"))
            if not url:
                continue
            evidence_hint = _clean_text(
                item.get("surrounding_text")
                or item.get("anchor_text")
                or item.get("title")
                or item.get("aria_label")
            )
            ranked_links.append(
                RankedInternalLink(
                    url=url,
                    priority=len(ranked_links) + 1,
                    reason="Local fallback ranked this discovered link by available text, DOM context, and structural signal.",
                    expected_information=evidence_hint[:180],
                    confidence=0.25,
                )
            )
        return LinkRankingDecision(
            ranked_links=ranked_links,
            homepage_enough=False,
            reason_homepage_enough=(
                "Local fallback needs more evidence from discovered internal links."
                if ranked_links
                else "No internal links were available; analysis will continue with homepage evidence and uncertainty."
            ),
            recommended_next_action="scrape_more_pages",
        )

    def _fallback_card(
        self,
        state: ProspectAnalysisState,
        *,
        website: str,
        reason: str,
        evidence_chunks: list[EvidenceChunk] | None = None,
    ) -> ProspectCard:
        pages = self._pages_for_site(state, website)
        combined_text = "\n".join(page.text for page in pages)
        first_page = pages[0] if pages else None
        title = first_page.title if first_page else ""
        company = self._company_from_title_or_url(title, website)
        contact_email = extract_contact_email(combined_text)
        contact_url = self._fallback_contact_url(pages)
        criteria_terms = {
            token
            for token in re.findall(
                r"[A-Za-z][A-Za-z0-9-]{3,}", state.target_criteria.lower()
            )
            if len(token) > 3
        }
        matched_terms = [
            token for token in criteria_terms if token in combined_text.lower()
        ]
        score = int(
            min(65, 25 + (len(matched_terms) * 8) + min(len(combined_text) // 700, 20))
        )
        fit_status = self.settings.fit_status_for_score(score)
        evidence_chunks = evidence_chunks or self.build_evidence_chunks(
            state, website=website
        )
        first_chunk = evidence_chunks[0] if evidence_chunks else None
        fallback_snippets = []
        field_evidence_map: dict[str, list[str]] = {}
        if first_chunk is not None:
            quote = first_chunk.text[:260].strip()
            fallback_snippets = [
                EvidenceSnippet(
                    evidence_id="ev_001",
                    claim="Readable website evidence was available for manual review.",
                    quote=quote,
                    source_url=first_chunk.page_url,
                    source_title=first_chunk.page_title,
                    chunk_id=first_chunk.chunk_id,
                    supports_field="ai_summary",
                    confidence=0.25,
                    evidence_type="direct",
                    valid=True,
                )
            ]
            field_evidence_map = {"ai_summary": ["ev_001"]}
        weak_evidence_fields = [
            "category",
            "target_audience",
            "services_products",
            "signals_of_fit",
            "suggested_offer",
        ]
        if not contact_email:
            weak_evidence_fields.append("contact_email")
        if len(pages) <= 1:
            weak_evidence_fields.append("only_homepage_available")

        return ProspectCard(
            company=company,
            website=website,
            category="Unknown",
            fit_score=score,
            fit_status=fit_status,
            reason=f"AI card generation was unavailable: {reason}",
            pain_point="Unknown",
            suggested_offer=state.outreach_goal or "Unknown",
            recommended_contact_type="Human review",
            confidence="Low - local fallback",
            next_step="Review manually before saving or sending outreach.",
            contact_email=contact_email,
            contact_url=contact_url,
            ai_summary=(
                (combined_text[:280] + "...")
                if len(combined_text) > 280
                else combined_text
            ),
            why_relevant="Local fallback found limited evidence; AI review is recommended.",
            ai_reasoning_summary="Generated without AI due to missing or failed AI analysis.",
            missing_information=[
                "AI analysis did not complete.",
                "Manual validation is required.",
            ],
            risk_uncertainty_flags=["Low-confidence fallback card."],
            evidence_snippets=fallback_snippets,
            field_evidence_map=field_evidence_map,
            weak_evidence_fields=weak_evidence_fields,
            evidence_validation_summary={
                "returned_count": len(fallback_snippets),
                "valid_count": len(fallback_snippets),
                "invalid_count": 0,
                "chunk_count": len(evidence_chunks),
                "weak_evidence_fields": weak_evidence_fields,
            },
            pages_scraped=[page.final_url for page in pages],
            source_session_id=state.session_id,
        )

    @staticmethod
    def _company_from_title_or_url(title: str, url: str) -> str:
        cleaned_title = _clean_text(title)
        if cleaned_title:
            return (
                re.split(r"\s[-|:]\s", cleaned_title)[0][:80].strip()
                or cleaned_title[:80]
            )
        host = _host(url).removeprefix("www.")
        return host or "Unknown"

    @staticmethod
    def _fallback_contact_url(pages: list[ScrapedPage]) -> str:
        for page in pages:
            for link in page.links:
                url = str(link.get("url") or "")
                text = str(link.get("anchor_text") or "").lower()
                if (
                    "linkedin.com" in url.lower()
                    or "contact" in text
                    or "contact" in url.lower()
                ):
                    return url
        return pages[0].final_url if pages else ""

    @staticmethod
    def _fallback_outreach_draft(
        card: ProspectCard, state: ProspectAnalysisState
    ) -> dict[str, str]:
        subject = f"Potential collaboration with {card.company}"
        body = (
            f"Hi {card.company} team,\n\n"
            f"I reviewed {card.website} and noticed a possible fit around {card.outreach_angle or state.outreach_goal}.\n\n"
            f"If this is relevant, I would be happy to share a short idea tailored to your current priorities.\n\n"
            "Best,\n"
        )
        return {
            "subject": subject,
            "body": _format_email_body(body),
            "cta": "Would a brief reply be the best next step?",
        }
