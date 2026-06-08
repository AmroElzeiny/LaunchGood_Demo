from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from openai import OpenAI

from job_bot.config import Settings
from job_bot.date_parsing import find_date_in_text_to_iso
from job_bot.human_review_confidence import stabilize_job_confidence
from job_bot.language_utils import detect_language
from job_bot.models import JobCard, LinkCandidate
from job_bot.opportunity_fields import prompt_field_dictionary
from job_bot.openai_compat import create_chat_completion_with_fallback

if TYPE_CHECKING:
    from job_bot.storage import StateStore


def _safe_json_loads(text: str) -> dict[str, Any]:
    text = text.strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                return {}
    return {}


class AIClient:
    def __init__(self, settings: Settings, logger: logging.Logger, state_store: "StateStore | None" = None) -> None:
        self.settings = settings
        self.logger = logger
        self.state_store = state_store
        self.client = OpenAI(api_key=settings.openai_api_key)
        self.link_ranking_model = settings.openai_model_link_ranking.strip() or settings.openai_model
        self.extraction_model = settings.openai_model_extraction.strip() or settings.openai_model
        self.post_age_model = settings.openai_model_post_age.strip() or settings.openai_model

    async def rank_links(self, website: str, candidates: list[LinkCandidate]) -> list[str]:
        if not candidates:
            return []
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._rank_links_sync, website, candidates),
                timeout=max(5, int(self.settings.link_ranking_timeout_seconds)),
            )
        except TimeoutError:
            self._record_ai_stage_metric("link_ranking", "timeouts")
            self.logger.warning("[ai-client] link-ranking timed out for %s", website)
            return []

    def _chat_json(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        model: str,
        stage: str,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        response = create_chat_completion_with_fallback(
            self.client.chat.completions.create,
            model=model,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            logger=self.logger,
            log_label="ai-client",
            timeout_seconds=timeout_seconds or float(self.settings.openai_request_timeout_seconds),
            retry_budget=int(self.settings.ai_retry_budget),
            backoff_base_seconds=float(self.settings.ai_backoff_base_seconds),
            event_callback=lambda event_type, payload: self._record_ai_event(stage, event_type, payload),
        )
        content = response.choices[0].message.content or "{}"
        self._record_ai_stage_metric(stage, "success")
        return _safe_json_loads(content)

    def _record_ai_stage_metric(self, stage: str, suffix: str, amount: float = 1.0) -> None:
        if self.state_store is None:
            return
        self.state_store.increment_runtime_metric(f"ai.{stage}.{suffix}", amount)

    def _record_ai_event(self, stage: str, event_type: str, payload: dict[str, Any]) -> None:
        if self.state_store is None:
            return
        self.state_store.increment_runtime_metric(f"ai.{stage}.calls", 1.0 if event_type == "call_failed" else 0.0)
        if event_type == "retry_scheduled":
            self.state_store.increment_runtime_metric(f"ai.{stage}.retries", 1.0)
            if bool(payload.get("rate_limited")):
                self.state_store.increment_runtime_metric(f"ai.{stage}.rate_limits", 1.0)
            if bool(payload.get("timed_out")):
                self.state_store.increment_runtime_metric(f"ai.{stage}.timeouts", 1.0)
            return
        if event_type == "call_failed":
            if bool(payload.get("rate_limited")):
                self.state_store.increment_runtime_metric(f"ai.{stage}.rate_limits", 1.0)
            if bool(payload.get("timed_out")):
                self.state_store.increment_runtime_metric(f"ai.{stage}.timeouts", 1.0)
            self.state_store.increment_runtime_metric(f"ai.{stage}.failures", 1.0)

    @staticmethod
    def _remaining_budget_seconds(deadline: float, *, floor_seconds: float = 1.0) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("stage budget exceeded")
        return max(float(floor_seconds), remaining)

    def _rank_links_sync(self, website: str, candidates: list[LinkCandidate]) -> list[str]:
        deadline = time.monotonic() + max(5.0, float(self.settings.link_ranking_timeout_seconds))
        compact_candidates = [
            {
                "url": candidate.url,
                "anchor_text": candidate.anchor_text[:200],
                "source_page": candidate.source_page,
                "structural_score": round(candidate.structural_score, 3),
            }
            for candidate in candidates[:200]
        ]

        system_prompt = (
            "You are a strict opportunity-link ranking analyst. "
            "Return strictly JSON. "
            "Rank URLs by likelihood that they are unique opportunity detail pages for jobs, projects, contracts, "
            "freelance work, consulting briefs, or RFPs. "
            "Strongly prefer detail pages with stable identifiers or role-specific or project-specific slugs. "
            "Strongly avoid login/signup/legal/privacy/help/about/community/company-profile/category/search-result "
            "and employer-marketing pages. Use structural_score as a prior, but override it when the URL pattern is obvious noise."
        )
        user_prompt = json.dumps(
            {
                "website": website,
                "candidates": compact_candidates,
                "return_format": {"ordered_urls": ["url1", "url2"]},
            },
            ensure_ascii=False,
        )
        data = self._chat_json(
            system_prompt,
            user_prompt,
            model=self.link_ranking_model,
            stage="link_ranking",
            timeout_seconds=min(
                float(self.settings.openai_request_timeout_seconds),
                self._remaining_budget_seconds(deadline),
            ),
        )
        ordered_urls = data.get("ordered_urls", [])
        if not isinstance(ordered_urls, list):
            return []
        normalized = [str(item).strip() for item in ordered_urls if str(item).strip()]
        return normalized

    async def extract_job_card(
        self,
        website: str,
        url: str,
        page_text: str,
        html: str,
        review_guidance: str = "",
    ) -> JobCard | None:
        if not bool(getattr(self.settings, "enable_ai_extraction", True)):
            return None
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._extract_job_card_sync, website, url, page_text, html, review_guidance),
                timeout=max(10, int(self.settings.extraction_timeout_seconds)),
            )
        except TimeoutError:
            self._record_ai_stage_metric("extraction", "timeouts")
            self.logger.warning("[ai-client] extraction stage timed out for %s", url)
            return None

    async def assess_post_age(
        self,
        *,
        website: str,
        url: str,
        page_text: str,
        html: str,
        max_age_days: int,
        now_utc_iso: str,
    ) -> "PostAgeAssessment":
        if not bool(getattr(self.settings, "enable_ai_age_check", True)):
            return self._deterministic_post_age_assessment(
                page_text=page_text,
                html=html,
                max_age_days=max_age_days,
                now_utc_iso=now_utc_iso,
                default_reason="Age-check AI disabled; deterministic date parsing was used.",
            )
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(
                    self._assess_post_age_sync,
                    website,
                    url,
                    page_text,
                    html,
                    max_age_days,
                    now_utc_iso,
                ),
                timeout=max(5, int(self.settings.age_check_timeout_seconds)),
            )
        except TimeoutError:
            self._record_ai_stage_metric("age_check", "timeouts")
            self.logger.warning("[ai-client] age-check stage timed out for %s", url)
            return self._deterministic_post_age_assessment(
                page_text=page_text,
                html=html,
                max_age_days=max_age_days,
                now_utc_iso=now_utc_iso,
                default_reason="Age check timed out; deterministic date parsing kept the post.",
            )

    @staticmethod
    def _parse_utc_iso(value: str) -> datetime:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)

    def _deterministic_post_age_assessment(
        self,
        *,
        page_text: str,
        html: str,
        max_age_days: int,
        now_utc_iso: str,
        default_reason: str,
    ) -> "PostAgeAssessment":
        now_utc = self._parse_utc_iso(now_utc_iso)
        hints = (
            "posted",
            "published",
            "updated",
            "date posted",
            "опублик",
            "размещ",
            "дата",
            "сегодня",
            "вчера",
            "назад",
        )
        candidates: list[str] = []
        for source in (page_text, html[:12000]):
            for line in str(source or "").splitlines():
                cleaned = " ".join(line.split()).strip()
                if not cleaned:
                    continue
                lowered = cleaned.lower().replace("ё", "е")
                if any(hint in lowered for hint in hints):
                    candidates.append(cleaned)
            if not candidates:
                candidates.append(str(source or ""))
        detected_posted_at_utc = ""
        for candidate in candidates:
            detected_posted_at_utc = find_date_in_text_to_iso(candidate, now_utc=now_utc, prefer_future=False)
            if detected_posted_at_utc:
                break
        older_than_limit = False
        if detected_posted_at_utc:
            parsed_detected = self._parse_utc_iso(detected_posted_at_utc)
            older_than_limit = parsed_detected < (now_utc - timedelta(days=max_age_days))
        return PostAgeAssessment(
            older_than_limit=older_than_limit,
            confidence=0.34 if detected_posted_at_utc else 0.0,
            detected_posted_at_utc=detected_posted_at_utc,
            reason=default_reason if detected_posted_at_utc else "Deterministic date parsing did not find a reliable posting date.",
        )

    def _extract_job_card_sync(
        self,
        website: str,
        url: str,
        page_text: str,
        html: str,
        review_guidance: str,
    ) -> JobCard | None:
        deadline = time.monotonic() + max(10.0, float(self.settings.extraction_timeout_seconds))
        detected_language = detect_language(text=page_text, html=html)
        low_confidence_threshold = max(
            0.0,
            min(
                float(
                    getattr(
                        self.settings,
                        "extraction_low_confidence_threshold",
                        max(self.settings.human_review_confidence_threshold + 0.08, 0.72),
                    )
                ),
                1.0,
            ),
        )
        base_payload = {
            "website": website,
            "url": url,
            "page_text": page_text[:14000],
            "page_html_excerpt": html[:12000],
            "review_guidance": review_guidance[:4000],
            "preferred_output_language": detected_language,
            "freelance_field_dictionary": prompt_field_dictionary(),
            "return_format": {
                "is_relevant_opportunity": "bool",
                "is_job_post": "bool_compatibility_alias",
                "confidence": "float_0_to_1",
                "title": "string_max_5_words",
                "description": "string_max_30_words_professional_factual_concise",
                "opportunity_kind": "job_post|project_post|request_for_proposal|consulting_brief|mixed_opportunity_feed|unknown",
                "client": "string_or_unknown",
                "requester": "string_or_unknown",
                "scope_summary": "string_or_unknown",
                "budget": "string_or_unknown",
                "salary": "string_or_unknown_compatibility_alias",
                "duration": "string_or_unknown",
                "commitment_level": "string_or_unknown",
                "skills_required": "string_or_unknown",
                "proposal_deadline": "string_or_unknown",
                "start_timeline": "string_or_unknown",
                "industry": "string_or_unknown",
                "engagement_type": "string_or_unknown",
                "remote_location_constraint": "string_or_unknown",
                "location": "string_or_unknown_compatibility_alias",
                "contact_url": "absolute_url_or_empty",
                "posted_at_utc": "ISO8601_or_empty",
                "company": "string_or_unknown_compatibility_alias",
                "language": "en|ru|other",
                "notes": "string",
            },
        }

        system_prompt_1 = (
            "You are a strict and professional information extraction engine for freelance and project opportunities. "
            "Return JSON only. Do not fabricate fields; use 'Unknown' when missing. "
            "Classify whether the page is a single relevant opportunity and extract fields. "
            "Treat projects, contracts, retainers, gigs, consulting work, short-term engagements, freelance assignments, "
            "and traditional job posts as first-class valid opportunity matches. "
            "Review guidance contains recent human approvals/rejections; align with it. "
            "Title max 5 words. Description max 30 words, professional, factual, and concise. "
            "Preserve the primary language of the source page in title, description, budget, location, client, and notes. "
            "If preferred_output_language is 'ru', keep the extracted wording in Russian."
        )
        first_payload = dict(base_payload)
        first_payload["decision_pass"] = 1
        first_payload["instruction"] = "Initial decision and extraction."
        first_data = self._chat_json(
            system_prompt_1,
            json.dumps(first_payload, ensure_ascii=False),
            model=self.extraction_model,
            stage="extraction",
            timeout_seconds=min(
                float(self.settings.openai_request_timeout_seconds),
                self._remaining_budget_seconds(deadline),
            ),
        )
        first_card = self._job_card_from_json(
            first_data,
            website,
            url,
            method="ai-pass-1",
            fallback_language=detected_language,
        )
        if first_card is None:
            return None
        if not self._needs_extraction_escalation(first_card, low_confidence_threshold):
            return first_card
        try:
            second_timeout_seconds = min(
                float(self.settings.openai_request_timeout_seconds),
                self._remaining_budget_seconds(deadline),
            )
        except TimeoutError:
            first_card.notes = f"{first_card.notes} | timed_out_after=pass1".strip(" |")
            return first_card

        system_prompt_2 = (
            "You are a second independent professional verifier. "
            "Return JSON only. Re-evaluate this same page from scratch and confirm or reject that it is a relevant single opportunity page. "
            "Review guidance contains recent human approvals/rejections; align with it. "
            "Title max 5 words. Description max 30 words, professional, factual, and concise. "
            "Preserve the primary language of the source page in extracted fields."
        )
        second_payload = dict(base_payload)
        second_payload["decision_pass"] = 2
        second_payload["instruction"] = "Independent confirmation with a different reasoning style."
        second_data = self._chat_json(
            system_prompt_2,
            json.dumps(second_payload, ensure_ascii=False),
            model=self.extraction_model,
            stage="extraction",
            timeout_seconds=second_timeout_seconds,
        )
        second_card = self._job_card_from_json(
            second_data,
            website,
            url,
            method="ai-pass-2",
            fallback_language=detected_language,
        )
        if second_card is None:
            return first_card
        if not self._needs_final_extraction_pass(first_card, second_card, low_confidence_threshold):
            second_card.notes = f"{second_card.notes} | final=pass2_used".strip(" |")
            return second_card
        try:
            third_timeout_seconds = min(
                float(self.settings.openai_request_timeout_seconds),
                self._remaining_budget_seconds(deadline),
            )
        except TimeoutError:
            second_card.notes = f"{second_card.notes} | timed_out_after=pass2".strip(" |")
            return second_card

        system_prompt_3 = (
            "You are the final professional arbiter for this page classification. "
            "Return JSON only. Make the final yes/no decision on whether this is a relevant single opportunity page. "
            "Review guidance contains recent human approvals/rejections; align with it. "
            "Title max 5 words. Description max 30 words, professional, factual, and concise. "
            "Preserve the primary language of the source page in extracted fields."
        )
        third_payload = dict(base_payload)
        third_payload["decision_pass"] = 3
        third_payload["instruction"] = "Final decision because earlier passes were low-confidence or disagreed."
        third_data = self._chat_json(
            system_prompt_3,
            json.dumps(third_payload, ensure_ascii=False),
            model=self.extraction_model,
            stage="extraction",
            timeout_seconds=third_timeout_seconds,
        )
        third_card = self._job_card_from_json(
            third_data,
            website,
            url,
            method="ai-pass-3",
            fallback_language=detected_language,
        )
        if third_card is None:
            second_card.notes = f"{second_card.notes} | final=pass2_used".strip(" |")
            return second_card
        return third_card

    @staticmethod
    def _needs_extraction_escalation(card: JobCard, low_confidence_threshold: float) -> bool:
        title_unknown = str(card.title or "").strip().lower() == "unknown"
        description_unknown = str(card.description or "").strip().lower() == "unknown"
        missing_context = title_unknown or description_unknown
        return missing_context or float(card.confidence) < low_confidence_threshold

    @staticmethod
    def _needs_final_extraction_pass(
        first_card: JobCard,
        second_card: JobCard,
        low_confidence_threshold: float,
    ) -> bool:
        if first_card.is_job_post != second_card.is_job_post:
            return True
        if float(second_card.confidence) < low_confidence_threshold:
            return True
        critical_fields = (str(second_card.title or "").strip().lower(), str(second_card.description or "").strip().lower())
        return any(value == "unknown" for value in critical_fields)

    def _assess_post_age_sync(
        self,
        website: str,
        url: str,
        page_text: str,
        html: str,
        max_age_days: int,
        now_utc_iso: str,
    ) -> "PostAgeAssessment":
        deadline = time.monotonic() + max(5.0, float(self.settings.age_check_timeout_seconds))
        deterministic = self._deterministic_post_age_assessment(
            page_text=page_text,
            html=html,
            max_age_days=max_age_days,
            now_utc_iso=now_utc_iso,
            default_reason="Deterministic date parsing from page text was used.",
        )
        system_prompt = (
            "You are a strict opportunity freshness validator. Return JSON only. "
            "Determine whether the opportunity date is older than the provided max_age_days. "
            "Use any evidence in the text/HTML (absolute dates, relative dates like '3 days ago', JSON-LD). "
            "If the posting date is unclear, do not mark it as old."
        )
        payload = {
            "website": website,
            "url": url,
            "max_age_days": max_age_days,
            "now_utc_iso": now_utc_iso,
            "page_text": page_text[:14000],
            "page_html_excerpt": html[:12000],
            "return_format": {
                "older_than_limit": "bool",
                "confidence": "float_0_to_1",
                "detected_posted_at_utc": "ISO8601_or_empty",
                "reason": "short_string",
            },
        }
        data = self._chat_json(
            system_prompt,
            json.dumps(payload, ensure_ascii=False),
            model=self.post_age_model,
            stage="age_check",
            timeout_seconds=min(
                float(self.settings.openai_request_timeout_seconds),
                self._remaining_budget_seconds(deadline),
            ),
        )
        older_than_limit_raw = data.get("older_than_limit", False)
        if isinstance(older_than_limit_raw, bool):
            older_than_limit = older_than_limit_raw
        elif isinstance(older_than_limit_raw, (int, float)):
            older_than_limit = older_than_limit_raw != 0
        elif isinstance(older_than_limit_raw, str):
            older_than_limit = older_than_limit_raw.strip().lower() in {"true", "yes", "1"}
        else:
            older_than_limit = False
        try:
            confidence = float(data.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        detected_posted_at_utc = str(data.get("detected_posted_at_utc", "")).strip()
        reason = str(data.get("reason", "")).strip() or "No age reason provided."
        if deterministic.detected_posted_at_utc and not detected_posted_at_utc:
            detected_posted_at_utc = deterministic.detected_posted_at_utc
            if not reason or reason == "No age reason provided.":
                reason = deterministic.reason
        if deterministic.older_than_limit and not older_than_limit and confidence <= 0.35:
            older_than_limit = True
            detected_posted_at_utc = deterministic.detected_posted_at_utc or detected_posted_at_utc
            reason = deterministic.reason
        return PostAgeAssessment(
            older_than_limit=older_than_limit,
            confidence=max(0.0, min(confidence, 1.0)),
            detected_posted_at_utc=detected_posted_at_utc,
            reason=reason,
        )

    @staticmethod
    def _job_card_from_json(
        data: dict[str, Any],
        website: str,
        url: str,
        method: str,
        *,
        fallback_language: str,
    ) -> JobCard | None:
        if not isinstance(data, dict):
            return None

        raw_is_opportunity = data.get("is_relevant_opportunity", data.get("is_job_post", False))
        raw_is_job_post = data.get("is_job_post", False)
        if isinstance(raw_is_opportunity, bool):
            is_opportunity = raw_is_opportunity
        elif isinstance(raw_is_opportunity, (int, float)):
            is_opportunity = raw_is_opportunity != 0
        elif isinstance(raw_is_opportunity, str):
            is_opportunity = raw_is_opportunity.strip().lower() in {"true", "yes", "1"}
        else:
            is_opportunity = False
        if isinstance(raw_is_job_post, bool):
            is_job_post = raw_is_job_post
        elif isinstance(raw_is_job_post, (int, float)):
            is_job_post = raw_is_job_post != 0
        elif isinstance(raw_is_job_post, str):
            is_job_post = raw_is_job_post.strip().lower() in {"true", "yes", "1"}
        else:
            is_job_post = False
        try:
            confidence = float(data.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0

        title_raw = data.get("title", data.get("headline", ""))
        title = AIClient._limit_words(str(title_raw).strip(), 5) or "Unknown"
        description = AIClient._limit_words(str(data.get("description", "")).strip(), 30) or "Unknown"
        budget = str(data.get("budget", data.get("salary", ""))).strip() or "Unknown"
        salary = str(data.get("salary", data.get("budget", ""))).strip() or "Unknown"
        location = str(data.get("location", data.get("remote_location_constraint", ""))).strip() or "Unknown"
        company = str(data.get("company", data.get("client", data.get("hiring_organization", "")))).strip()
        client = str(data.get("client", data.get("company", ""))).strip()
        requester = str(data.get("requester", data.get("client", data.get("company", "")))).strip()
        scope_summary = AIClient._limit_words(
            str(data.get("scope_summary", data.get("description", ""))).strip(),
            30,
        ) or description
        opportunity_kind = str(data.get("opportunity_kind", "")).strip().lower()
        duration = str(data.get("duration", "")).strip()
        commitment_level = str(data.get("commitment_level", "")).strip()
        skills_required = str(data.get("skills_required", "")).strip()
        proposal_deadline = str(data.get("proposal_deadline", "")).strip()
        start_timeline = str(data.get("start_timeline", "")).strip()
        industry = str(data.get("industry", "")).strip()
        engagement_type = str(data.get("engagement_type", "")).strip()
        remote_location_constraint = str(data.get("remote_location_constraint", data.get("location", ""))).strip()
        contact_url = str(data.get("contact_url", data.get("proposal_url", ""))).strip() or url
        posted_at_utc = str(data.get("posted_at_utc", "")).strip()
        notes = str(data.get("notes", "")).strip()
        language_raw = str(data.get("language", "")).strip().lower()
        language = language_raw if language_raw in {"ru", "en"} else detect_language(
            text=" ".join(
                part
                for part in (title, description, location, budget, company, client, requester, notes)
                if part
            )
        )
        if language not in {"ru", "en"}:
            language = fallback_language if fallback_language in {"ru", "en"} else "en"
        if is_opportunity and AIClient._looks_like_multi_job_feed(title=title, description=description, notes=notes):
            is_opportunity = False
            is_job_post = False
            opportunity_kind = "mixed_opportunity_feed"
            notes = "Rejected multi-opportunity feed/listing page masquerading as a single detail page."
        if not opportunity_kind:
            opportunity_kind = AIClient._infer_opportunity_kind(title=title, description=description, notes=notes, url=url)
        is_job_post = bool(is_job_post or opportunity_kind == "job_post")
        confidence = stabilize_job_confidence(
            confidence,
            is_job_post=is_opportunity,
            title=title,
            description=scope_summary or description,
            location=remote_location_constraint or location,
            salary=budget or salary,
            company=client or requester or company,
            posted_at_utc=posted_at_utc,
            notes=notes,
        )

        return JobCard(
            website=website,
            url=url,
            title=title,
            description=description,
            salary=salary,
            location=location,
            is_job_post=is_job_post,
            confidence=max(0.0, min(confidence, 1.0)),
            extraction_method=method,
            posted_at_utc=posted_at_utc,
            notes=notes,
            company=company,
            language=language,
            is_relevant_opportunity=is_opportunity,
            opportunity_kind=opportunity_kind,
            client=client,
            requester=requester,
            scope_summary=scope_summary,
            budget=budget,
            duration=duration,
            commitment_level=commitment_level,
            skills_required=skills_required,
            proposal_deadline=proposal_deadline,
            start_timeline=start_timeline,
            industry=industry,
            engagement_type=engagement_type,
            remote_location_constraint=remote_location_constraint,
            contact_url=contact_url,
        )

    @staticmethod
    def _limit_words(text: str, max_words: int) -> str:
        words = text.split()
        if len(words) <= max_words:
            return text
        return " ".join(words[:max_words])

    @staticmethod
    def _looks_like_multi_job_feed(*, title: str, description: str, notes: str) -> bool:
        content = " ".join(part for part in (title, description, notes) if part).strip().lower()
        if not content:
            return False
        feed_hints = (
            "jobs feed",
            "job feed",
            "rss feed",
            "vacancy feed",
            "latest jobs",
            "job roundup",
            "job digest",
            "list of jobs",
            "multiple job listings",
            "project board",
            "project feed",
            "latest projects",
            "latest opportunities",
            "open requests",
            "request board",
            "multiple projects",
            "multiple opportunities",
            "\u043b\u0435\u043d\u0442\u0430 \u0432\u0430\u043a\u0430\u043d\u0441\u0438\u0439",
            "\u043f\u043e\u0434\u0431\u043e\u0440\u043a\u0430 \u0432\u0430\u043a\u0430\u043d\u0441\u0438\u0439",
            "\u0441\u043f\u0438\u0441\u043e\u043a \u0432\u0430\u043a\u0430\u043d\u0441\u0438\u0439",
        )
        role_separator_count = content.count("product designer") + content.count("ux designer") + content.count("ui designer")
        return any(hint in content for hint in feed_hints) or role_separator_count >= 2

    @staticmethod
    def _infer_opportunity_kind(*, title: str, description: str, notes: str, url: str) -> str:
        blob = " ".join(part for part in (title, description, notes, url) if part).lower()
        if any(token in blob for token in ("request for proposal", "rfp", "/rfp/", "proposal deadline")):
            return "request_for_proposal"
        if any(token in blob for token in ("consulting", "retainer", "statement of work", "scope of work")):
            return "consulting_brief"
        if any(token in blob for token in ("project", "freelance", "contract", "gig", "brief", "proposal", "client")):
            return "project_post"
        return "job_post"


@dataclass(slots=True)
class PostAgeAssessment:
    older_than_limit: bool
    confidence: float
    detected_posted_at_utc: str
    reason: str
