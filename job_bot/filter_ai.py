from __future__ import annotations

import asyncio
import json
import logging
import math
import re
from dataclasses import dataclass
from typing import Any

from openai import OpenAI

from job_bot.language_utils import detect_language, localized_text, normalize_match_text, tokenize_with_morphology
from job_bot.location_product_rules import (
    REMOTE_GLOBAL_RULE,
    REMOTE_SCOPE_PROMPT_BLOCK,
    REMOTE_WITHIN_COUNTRY_RULE,
)
from job_bot.openai_compat import create_chat_completion_with_fallback

_GENERIC_REMOTE_LOCATION_MARKERS = {
    normalize_match_text(value)
    for value in (
        "remote",
        "remote global",
        "work from home",
        "work from anywhere",
        "worldwide",
        "global",
        "anywhere",
        "distributed",
        "not specified",
        "unknown",
        "n/a",
        "none",
        "удаленно",
        "удалённо",
        "удаленка",
        "удалёнка",
        "по всему миру",
        "из любой точки мира",
    )
}

_COUNTRY_SCOPE_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\b(?:us|usa|u\.s\.a?|uk|u\.k\.|europe|eu|emea|apac|latam|mena|india|egypt|canada|australia|germany|france|poland|russia|uae|saudi arabia)\s+only\b", "country_restriction"),
    (r"\bonly\s+(?:for|in|within)\s+[a-z]", "country_restriction"),
    (r"\bmust\s+(?:be|live|reside|remain|work)\b.{0,40}\b(?:in|within|from)\b", "country_restriction"),
    (r"\bbased\s+in\b", "country_restriction"),
    (r"\blocated\s+in\b", "country_restriction"),
    (r"\bcandidates?\s+(?:in|from|based in|located in)\b", "country_restriction"),
    (r"\bremote\s*(?:-|,|/)?\s*(?:within|in|from)?\s*(?:us|usa|uk|europe|eu|emea|apac|latam|mena|india|egypt|canada|australia|germany|france|poland|russia|uae)\b", "country_restriction"),
    (r"\bтолько\s+(?:для|из|в)\b", "country_restriction"),
    (r"\bкандидат(?:ы|ам)?\s+(?:из|в|на территории)\b", "country_restriction"),
    (r"\bтрудоустройство\s+только\s+в\b", "country_restriction"),
)

_NATIONALITY_PATTERNS: tuple[str, ...] = (
    r"\bcitizen(?:ship)?\b",
    r"\bnationality\b",
    r"\bus citizens?\b",
    r"\bгражданств\w*\b",
    r"\bнациональност\w*\b",
)

_RESIDENCY_PATTERNS: tuple[str, ...] = (
    r"\bresiden(?:ce|cy|t)\b",
    r"\bresident of\b",
    r"\bpermanent resident\b",
    r"\bresiding in\b",
    r"\bпроживани\w+\s+в\b",
    r"\bрезидент\w*\b",
)

_WORK_AUTH_PATTERNS: tuple[str, ...] = (
    r"\bwork authorization\b.{0,40}\b(?:in|for)\b",
    r"\bright to work in\b",
    r"\beligible to work in\b",
    r"\bauthori[sz]ed to work in\b",
    r"\bразрешени\w+\s+на\s+работу\b",
    r"\bправо\s+на\s+работу\s+в\b",
)

_GLOBAL_REMOTE_MARKERS = {
    normalize_match_text(value)
    for value in (
        "work from anywhere",
        "anywhere in the world",
        "globally remote",
        "global remote",
        "worldwide",
        "open worldwide",
        "open globally",
        "from anywhere",
        "distributed team across the world",
        "можно работать из любой точки мира",
        "по всему миру",
        "по всему свету",
        "из любой точки мира",
    )
}


def _normalized_scope_text(*parts: str) -> str:
    return normalize_match_text(" ".join(part for part in parts if str(part or "").strip()))


def _remote_scope_reason_for_code(reason_code: str, *, scoped_value: str = "") -> str:
    normalized_code = str(reason_code or "").strip().lower()
    if normalized_code == "nationality_or_citizenship_restriction":
        return "Remote role requires a specific nationality or citizenship."
    if normalized_code == "residency_restriction":
        return "Remote role requires residence in a specific country."
    if normalized_code == "work_authorization_country_restriction":
        return "Remote role requires work authorization tied to a specific country."
    if normalized_code == "country_restriction":
        if scoped_value:
            return f"Remote role is limited by country or country-specific scope: {scoped_value}."
        return "Remote role is limited by country or country-specific scope."
    return "Remote role has no clear country or eligibility restriction."


def _detect_remote_restriction_reason_code(text: str) -> str:
    normalized = _normalized_scope_text(text)
    if not normalized:
        return ""
    if any(re.search(pattern, normalized, flags=re.IGNORECASE | re.DOTALL) for pattern in _NATIONALITY_PATTERNS):
        return "nationality_or_citizenship_restriction"
    if any(re.search(pattern, normalized, flags=re.IGNORECASE | re.DOTALL) for pattern in _RESIDENCY_PATTERNS):
        return "residency_restriction"
    if any(re.search(pattern, normalized, flags=re.IGNORECASE | re.DOTALL) for pattern in _WORK_AUTH_PATTERNS):
        return "work_authorization_country_restriction"
    for pattern, reason_code in _COUNTRY_SCOPE_PATTERNS:
        if re.search(pattern, normalized, flags=re.IGNORECASE | re.DOTALL):
            return reason_code
    return ""


def _extract_country_scope_hint(post_location: str) -> str:
    cleaned = " ".join(str(post_location or "").split()).strip()
    if not cleaned:
        return ""
    normalized = normalize_match_text(cleaned)
    if not normalized or normalized in _GENERIC_REMOTE_LOCATION_MARKERS:
        return ""
    if "remote" in normalized or "удален" in normalized or "удалён" in normalized:
        return cleaned
    return ""


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


def _to_float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            return float(stripped)
        except ValueError:
            return None
    return None


@dataclass(slots=True)
class LocationNormalization:
    raw_input: str
    country: str
    state: str
    city: str
    canonical: str
    confidence: float


@dataclass(slots=True)
class SalaryNormalization:
    raw_input: str
    is_specified: bool
    currency: str
    period: str
    min_value: float | None
    max_value: float | None
    min_usd: float | None
    max_usd: float | None
    canonical_text: str
    confidence: float


@dataclass(slots=True)
class KeywordExpansionItem:
    input_keyword: str
    understood_as: str
    similar_keywords: list[str]


@dataclass(slots=True)
class KeywordInterpretation:
    input_keywords: list[str]
    understood_keywords: list[str]
    expanded_keywords: list[str]
    per_keyword: list[KeywordExpansionItem]


@dataclass(slots=True, frozen=True)
class WorkArrangementAssessment:
    work_mode: str
    remote_scope: str
    scope_locations: tuple[str, ...]
    has_scope_restriction: bool
    confidence: float
    reason: str
    restriction_reason_code: str = ""

    @property
    def is_remote(self) -> bool:
        return self.work_mode == "remote"

    @property
    def is_hybrid(self) -> bool:
        return self.work_mode == "hybrid"

    @property
    def is_onsite(self) -> bool:
        return self.work_mode == "onsite"


class FilterAI:
    UX_UI_TOPIC_MARKERS = (
        "ux",
        "ui",
        "user experience",
        "user interface",
        "product designer",
        "interaction designer",
        "visual designer",
        "web designer",
        "ux дизайнер",
        "ui дизайнер",
        "дизайнер ux",
        "дизайнер ui",
        "дизайнер ux ui",
        "ux ui дизайнер",
        "продуктовый дизайнер",
        "продукт дизайнер",
        "веб дизайнер",
        "веб-дизайнер",
        "дизайнер интерфейсов",
    )
    UX_UI_ROLE_MARKERS = (
        "ux designer",
        "ui designer",
        "ux ui designer",
        "ui ux designer",
        "user experience designer",
        "user interface designer",
        "product designer",
        "interaction designer",
        "visual designer",
        "web designer",
        "ux дизайнер",
        "ui дизайнер",
        "дизайнер ux",
        "дизайнер ui",
        "ux ui дизайнер",
        "дизайнер ux ui",
        "продуктовый дизайнер",
        "продукт дизайнер",
        "дизайнер интерфейсов",
        "веб дизайнер",
        "веб-дизайнер",
    )
    UX_UI_WORK_MARKERS = (
        "wireframe",
        "wireframes",
        "prototype",
        "prototypes",
        "user flow",
        "user flows",
        "user research",
        "usability",
        "information architecture",
        "figma",
        "mockup",
        "mockups",
        "вайрфрейм",
        "вайрфреймы",
        "прототип",
        "прототипы",
        "пользовательский сценарий",
        "пользовательские сценарии",
        "исследование пользователей",
        "юзабилити",
        "информационная архитектура",
        "макет",
        "макеты",
        "дизайн система",
        "дизайн системы",
    )
    FRONTEND_ROLE_MARKERS = (
        "frontend developer",
        "front end developer",
        "frontend engineer",
        "front end engineer",
        "software engineer",
        "software developer",
        "web developer",
        "full stack developer",
        "fullstack developer",
        "full stack engineer",
        "fullstack engineer",
        "react developer",
        "javascript developer",
        "typescript developer",
        "ui engineer",
        "ui developer",
        "frontend разработчик",
        "front end разработчик",
        "фронтенд разработчик",
        "front-end разработчик",
        "frontend инженер",
        "фронтенд инженер",
        "разработчик интерфейсов",
        "web разработчик",
        "веб разработчик",
        "react разработчик",
        "javascript разработчик",
        "typescript разработчик",
    )

    def __init__(
        self,
        openai_api_key: str,
        openai_model: str,
        logger: logging.Logger,
        *,
        keyword_model: str = "",
        final_match_model: str = "",
        request_timeout_seconds: float = 45.0,
        retry_budget: int = 2,
        backoff_base_seconds: float = 1.5,
    ) -> None:
        self.logger = logger
        self.openai_model = openai_model.strip() or "gpt-4o-mini"
        self.keyword_model = keyword_model.strip() or self.openai_model
        self.final_match_model = final_match_model.strip() or self.openai_model
        self.request_timeout_seconds = max(5.0, float(request_timeout_seconds))
        self.retry_budget = max(0, int(retry_budget))
        self.backoff_base_seconds = max(0.1, float(backoff_base_seconds))
        api_key = openai_api_key.strip()
        self.client = OpenAI(api_key=api_key) if api_key else None
        self._location_cache: dict[str, LocationNormalization] = {}
        self._salary_cache: dict[str, SalaryNormalization] = {}
        self._keywords_cache: dict[str, list[str]] = {}
        self._keyword_interpretation_cache: dict[str, KeywordInterpretation] = {}
        self._location_match_cache: dict[tuple[str, str], tuple[bool, str]] = {}
        self._post_filter_match_cache: dict[str, tuple[bool, str]] = {}
        self._role_alignment_cache: dict[str, tuple[bool, str]] = {}
        self._remote_scope_cache: dict[str, tuple[bool, str]] = {}
        self._work_arrangement_cache: dict[str, WorkArrangementAssessment] = {}
        self._remote_country_cache: dict[str, tuple[bool, str]] = {}
        self._feedback_analysis_cache: dict[str, dict[str, str]] = {}
        self._embedding_cache: dict[str, list[float]] = {}
        self.embedding_model = "text-embedding-3-small"

    @staticmethod
    def split_location_preferences(text: str) -> list[str]:
        raw = str(text or "").strip()
        if not raw:
            return []
        ordered: list[str] = []
        seen: set[str] = set()
        for item in re.split(r"(?:[;|\n]+|\s&\s)", raw):
            cleaned = re.sub(r"\s+", " ", str(item or "").strip(" ,;|\n\t"))
            if not cleaned:
                continue
            lowered = cleaned.lower()
            if lowered in seen:
                continue
            seen.add(lowered)
            ordered.append(cleaned)
            if len(ordered) >= 10:
                break
        return ordered

    @classmethod
    def build_multi_location_candidate(cls, text: str) -> LocationNormalization:
        cleaned = str(text or "").strip()
        locations = cls.split_location_preferences(cleaned)
        canonical = "; ".join(locations) or cleaned
        return LocationNormalization(
            raw_input=cleaned,
            country="",
            state="",
            city="",
            canonical=canonical,
            confidence=1.0 if locations else 0.0,
        )

    async def normalize_location(self, text: str) -> LocationNormalization:
        return await asyncio.to_thread(self._normalize_location_sync, text)

    async def check_location_match(self, user_location: str, post_location: str) -> tuple[bool, str]:
        return await asyncio.to_thread(self._check_location_match_sync, user_location, post_location)

    async def normalize_salary(self, text: str) -> SalaryNormalization:
        return await asyncio.to_thread(self._normalize_salary_sync, text)

    async def salary_matches(
        self,
        user_salary_pref: str,
        post_salary: str,
        include_no_salary: bool,
    ) -> tuple[bool, str]:
        return await asyncio.to_thread(
            self._salary_matches_sync,
            user_salary_pref,
            post_salary,
            include_no_salary,
        )

    async def salary_matches_structured(
        self,
        user_salary_pref: dict[str, object],
        post_salary: str,
        include_no_salary: bool,
    ) -> tuple[bool, str]:
        return await asyncio.to_thread(
            self._salary_matches_structured_sync,
            user_salary_pref,
            post_salary,
            include_no_salary,
        )

    async def assess_role_alignment(
        self,
        *,
        requested_role: str,
        post_title: str,
        post_description: str,
        post_location: str = "",
    ) -> tuple[bool, str]:
        return await asyncio.to_thread(
            self._assess_role_alignment_sync,
            requested_role,
            post_title,
            post_description,
            post_location,
        )

    async def check_remote_global_match(
        self,
        *,
        post_title: str,
        post_description: str,
        post_location: str = "",
    ) -> tuple[bool, str]:
        return await asyncio.to_thread(
            self._check_remote_global_match_sync,
            post_title,
            post_description,
            post_location,
        )

    async def assess_work_arrangement(
        self,
        *,
        post_title: str,
        post_description: str,
        post_location: str = "",
        post_notes: str = "",
    ) -> WorkArrangementAssessment:
        return await asyncio.to_thread(
            self._assess_work_arrangement_sync,
            post_title,
            post_description,
            post_location,
            post_notes,
        )

    async def check_remote_country_eligibility(
        self,
        *,
        target_country: str,
        post_title: str,
        post_description: str,
        post_location: str = "",
        post_notes: str = "",
    ) -> tuple[bool, str]:
        return await asyncio.to_thread(
            self._check_remote_country_eligibility_sync,
            target_country,
            post_title,
            post_description,
            post_location,
            post_notes,
        )

    async def analyze_negative_match_feedback(
        self,
        *,
        job_title: str,
        job_description: str,
        job_location: str,
        job_salary: str,
        match_reason: str,
        user_feedback: str,
    ) -> dict[str, str]:
        return await asyncio.to_thread(
            self._analyze_negative_match_feedback_sync,
            job_title,
            job_description,
            job_location,
            job_salary,
            match_reason,
            user_feedback,
        )

    async def expand_keywords(self, keywords: list[str]) -> list[str]:
        return await asyncio.to_thread(self._expand_keywords_sync, keywords)

    async def interpret_keywords(self, keywords: list[str]) -> KeywordInterpretation:
        return await asyncio.to_thread(self._interpret_keywords_sync, keywords)

    async def confirm_post_matches_filters(
        self,
        *,
        post_x: dict[str, Any],
        user_filters: dict[str, Any],
        precheck_results: dict[str, dict[str, str | bool]],
    ) -> tuple[bool, str]:
        return await asyncio.to_thread(
            self._confirm_post_matches_filters_sync,
            post_x,
            user_filters,
            precheck_results,
        )

    async def explain_alert_health(self, *, payload: dict[str, Any]) -> str:
        return await asyncio.to_thread(self._explain_alert_health_sync, payload)

    async def semantic_match_any(
        self,
        terms: list[str],
        text: str,
        *,
        threshold: float,
        label: str,
    ) -> tuple[bool, str]:
        return await asyncio.to_thread(
            self._semantic_match_any_sync,
            terms,
            text,
            threshold,
            label,
        )

    def _chat_json(self, system_prompt: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._chat_json_impl(system_prompt, payload, self.openai_model)

    def _chat_json_for_model(self, system_prompt: str, payload: dict[str, Any], model: str) -> dict[str, Any]:
        if model == self.openai_model:
            return self._chat_json(system_prompt, payload)
        return self._chat_json_impl(system_prompt, payload, model)

    def _chat_json_impl(self, system_prompt: str, payload: dict[str, Any], model: str) -> dict[str, Any]:
        if self.client is None:
            return {}

        user_prompt = json.dumps(payload, ensure_ascii=False)
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
            log_label="filter-ai",
            timeout_seconds=self.request_timeout_seconds,
            retry_budget=self.retry_budget,
            backoff_base_seconds=self.backoff_base_seconds,
        )
        content = response.choices[0].message.content or "{}"
        return _safe_json_loads(content)

    @staticmethod
    def _limit_sentence_count(text: str, max_sentences: int = 3) -> str:
        cleaned = " ".join(str(text or "").split()).strip()
        if not cleaned:
            return ""
        parts = re.split(r"(?<=[.!?])\s+", cleaned)
        limited = " ".join(part.strip() for part in parts[:max_sentences] if part.strip()).strip()
        return limited or cleaned

    def _explain_alert_health_sync(self, payload: dict[str, Any]) -> str:
        fallback = self._fallback_alert_health_explanation(payload)
        if self.client is None:
            return fallback

        response = self._chat_json_for_model(
            (
                "You explain why a Telegram freelance alert user has not received matches. "
                "Return JSON with an 'explanation' field only. "
                "Keep it to 1-3 short sentences. "
                "Be honest about whether the issue looks system-side, filter-side, or both. "
                "Give one concrete suggestion. Avoid bullets. "
                "If payload.ui_language is 'ru', respond in Russian. Otherwise respond in English."
            ),
            payload,
            self.final_match_model,
        )
        explanation = self._limit_sentence_count(str(response.get("explanation") or ""), max_sentences=3)
        return explanation or fallback

    @staticmethod
    def _fallback_alert_health_explanation(payload: dict[str, Any]) -> str:
        discovered_posts = int(payload.get("discovered_posts") or 0)
        blocked_sources = int(payload.get("blocked_sources") or 0)
        duplicate_prevented = int(payload.get("duplicate_prevented") or 0)
        freshness_rejects = int(payload.get("freshness_rejects") or 0)
        user_filter_mismatches = int(payload.get("user_filter_mismatches") or 0)
        selected_websites = payload.get("selected_websites") or []
        ui_language = str(payload.get("ui_language") or "en").strip().lower()
        location = str(payload.get("location") or "").strip()
        keywords = payload.get("keywords") or []
        salary_required = not bool(payload.get("include_no_salary", True))

        if blocked_sources > 0 and blocked_sources >= max(1, len(selected_websites) // 2):
            explanation = (
                "The biggest issue looks system-side right now because some of your selected sources appear blocked or unstable. "
                "Keeping the alert active is fine, but adding one or two extra sources will reduce those gaps."
            )
        elif discovered_posts == 0:
            explanation = (
                "The bot is not seeing many fresh opportunities from your selected links in this period. "
                "Keeping the same alert is fine, but adding broader freelance or project sources will usually improve coverage."
            )
        elif duplicate_prevented >= max(2, discovered_posts // 3):
            explanation = (
                "The bot is finding opportunities, but many of them look like reposts or duplicates and are being filtered out on purpose. "
                "Keeping the same alert is reasonable, and adding one or two extra sources can help surface more unique opportunities."
            )
        elif freshness_rejects >= max(2, user_filter_mismatches) and freshness_rejects > 0:
            explanation = (
                "Most of what the bot found looked stale or already closed for your freshness rules, so the alert is filtering those opportunities out before delivery. "
                "If this keeps happening, widening the freshness window is the strongest fix."
            )
        elif user_filter_mismatches > 0:
            suggestion = "broadening your filters a little"
            if location:
                suggestion = "loosening the location filter slightly"
            elif keywords:
                suggestion = "reducing or broadening the keywords"
            elif salary_required:
                suggestion = "allowing opportunities without visible budget or rate"
            explanation = (
                "The bot is finding opportunities, but your alert filters are rejecting many of them before they can be sent. "
                f"If you want more volume, try {suggestion}."
            )
        else:
            explanation = (
                "The system is scanning normally, but there have not been enough fresh opportunities that clearly match your alert yet. "
                "Keeping the current setup is reasonable if quality matters more than volume."
            )

        if ui_language == "ru":
            translations = {
                "The biggest issue looks system-side right now because some of your selected sources appear blocked or unstable. Keeping the alert active is fine, but adding one or two extra sources will reduce those gaps.": "Похоже, сейчас главная проблема на стороне системы: часть выбранных источников заблокирована или работает нестабильно. Поиск можно оставить активным, но лучше добавить ещё один-два источника, чтобы сократить такие провалы.",
                "The bot is not seeing many fresh opportunities from your selected links in this period. Keeping the same alert is fine, but adding broader freelance or project sources will usually improve coverage.": "За этот период бот почти не видит свежих проектов в выбранных источниках. Текущий поиск можно оставить, но более широкие фриланс- и проектные источники обычно улучшают охват.",
                "The bot is finding opportunities, but many of them look like reposts or duplicates and are being filtered out on purpose. Keeping the same alert is reasonable, and adding one or two extra sources can help surface more unique opportunities.": "Бот находит проекты, но многие из них похожи на репосты или дубликаты, поэтому система специально их отсеивает. Текущий поиск можно оставить, а один-два дополнительных источника помогут находить больше уникальных проектов.",
                "Most of what the bot found looked stale or already closed for your freshness rules, so the alert is filtering those opportunities out before delivery. If this keeps happening, widening the freshness window is the strongest fix.": "Большая часть найденного выглядит устаревшей или уже закрытой по вашим правилам свежести, поэтому поиск отсеивает такие проекты ещё до отправки. Если это повторяется, лучше всего расширить окно свежести.",
                "The system is scanning normally, but there have not been enough fresh opportunities that clearly match your alert yet. Keeping the current setup is reasonable if quality matters more than volume.": "Система работает нормально, но пока не нашлось достаточно свежих проектов, которые явно подходят под ваши фильтры. Текущую настройку разумно оставить, если качество важнее количества.",
            }
            return translations.get(explanation, explanation)
        return explanation

    def _normalize_location_sync(self, text: str) -> LocationNormalization:
        raw = text.strip()
        if not raw:
            return LocationNormalization(
                raw_input=text,
                country="",
                state="",
                city="",
                canonical="",
                confidence=0.0,
            )
        cached = self._location_cache.get(raw.lower())
        if cached is not None:
            return cached

        if self.client is None:
            fallback = self._fallback_location(raw)
            self._location_cache[raw.lower()] = fallback
            return fallback

        try:
            data = self._chat_json_for_model(
                system_prompt=(
                    "You are a professional geographic normalizer for hiring filters. Return JSON only. "
                    "Infer country/state/city from noisy text in any language, including misspellings "
                    "and abbreviations. Be precise and do not ask follow-up questions."
                ),
                payload={
                    "input_location_text": raw,
                    "task": (
                        "Identify country, state_or_province, city when possible. "
                        "Differentiate country and state clearly. Use best-guess canonical English names."
                    ),
                    "return_format": {
                        "country": "string_or_empty",
                        "state": "string_or_empty",
                        "city": "string_or_empty",
                        "canonical": "string_or_empty",
                        "confidence": "float_0_to_1",
                    },
                },
                model=self.openai_model,
            )
            normalized = LocationNormalization(
                raw_input=raw,
                country=str(data.get("country", "")).strip(),
                state=str(data.get("state", "")).strip(),
                city=str(data.get("city", "")).strip(),
                canonical=str(data.get("canonical", "")).strip() or raw,
                confidence=max(0.0, min(_to_float_or_none(data.get("confidence")) or 0.0, 1.0)),
            )
            self._location_cache[raw.lower()] = normalized
            return normalized
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("[filter-ai] location normalization failed: %s", str(exc))
            fallback = self._fallback_location(raw)
            self._location_cache[raw.lower()] = fallback
            return fallback

    def _check_location_match_sync(self, user_location: str, post_location: str) -> tuple[bool, str]:
        user_clean = user_location.strip()
        post_clean = post_location.strip()
        if not user_clean:
            return (True, "No user location filter.")

        cache_key = (user_clean.lower(), post_clean.lower())
        cached = self._location_match_cache.get(cache_key)
        if cached is not None:
            return cached

        if self.client is None:
            result = self._fallback_location_match(user_clean, post_clean)
            self._location_match_cache[cache_key] = result
            return result

        try:
            data = self._chat_json_for_model(
                system_prompt=(
                    "You are a strict and professional location matcher for freelance opportunities, projects, and contracts. Return JSON only. "
                    "Interpret misspellings and short forms. Differentiate country and state. "
                    "If user specifies a state/province, match only that state in the correct country."
                ),
                payload={
                    "user_location_preference": user_clean,
                    "post_location_text": post_clean,
                    "task": (
                        "Determine if the post location satisfies the user preference. "
                        "If user has only country, state can be anything inside that country. "
                        "If user includes state/province, both country and state must align."
                    ),
                    "return_format": {
                        "match": "bool",
                        "reason": "short_string",
                    },
                },
                model=self.openai_model,
            )
            result = (bool(data.get("match", False)), str(data.get("reason", "")).strip() or "AI decision")
            self._location_match_cache[cache_key] = result
            return result
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("[filter-ai] location match failed: %s", str(exc))
            result = self._fallback_location_match(user_clean, post_clean)
            self._location_match_cache[cache_key] = result
            return result

    def _normalize_salary_sync(self, text: str) -> SalaryNormalization:
        raw = text.strip()
        if not raw:
            return SalaryNormalization(
                raw_input=text,
                is_specified=False,
                currency="USD",
                period="unknown",
                min_value=None,
                max_value=None,
                min_usd=None,
                max_usd=None,
                canonical_text="Not specified",
                confidence=0.0,
            )

        cached = self._salary_cache.get(raw.lower())
        if cached is not None:
            return cached

        if self.client is None:
            fallback = self._fallback_salary(raw)
            self._salary_cache[raw.lower()] = fallback
            return fallback

        try:
            data = self._chat_json_for_model(
                system_prompt=(
                    "You are a professional payment parser for freelance opportunities. Return JSON only. "
                    "Parse messy budget, rate, retainer, milestone, commission, or salary strings in any language and normalize to numeric range. "
                    "Also estimate USD equivalents when possible."
                ),
                payload={
                    "salary_text": raw,
                    "return_format": {
                        "is_specified": "bool",
                        "currency": "string_currency_or_unknown",
                        "period": "hour|day|week|month|year|project|unknown",
                        "min_value": "number_or_null",
                        "max_value": "number_or_null",
                        "min_usd": "number_or_null",
                        "max_usd": "number_or_null",
                        "canonical_text": "string",
                        "confidence": "float_0_to_1",
                    },
                },
                model=self.openai_model,
            )
            min_value = _to_float_or_none(data.get("min_value"))
            max_value = _to_float_or_none(data.get("max_value"))
            if min_value is not None and max_value is not None and min_value > max_value:
                min_value, max_value = max_value, min_value
            min_usd = _to_float_or_none(data.get("min_usd"))
            max_usd = _to_float_or_none(data.get("max_usd"))
            if min_usd is not None and max_usd is not None and min_usd > max_usd:
                min_usd, max_usd = max_usd, min_usd

            normalized = SalaryNormalization(
                raw_input=raw,
                is_specified=bool(data.get("is_specified", bool(min_value is not None or max_value is not None))),
                currency=str(data.get("currency", "USD")).strip().upper() or "USD",
                period=str(data.get("period", "unknown")).strip().lower() or "unknown",
                min_value=min_value,
                max_value=max_value,
                min_usd=min_usd if min_usd is not None else min_value,
                max_usd=max_usd if max_usd is not None else max_value,
                canonical_text=str(data.get("canonical_text", "")).strip() or raw,
                confidence=max(0.0, min(_to_float_or_none(data.get("confidence")) or 0.0, 1.0)),
            )
            self._salary_cache[raw.lower()] = normalized
            return normalized
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("[filter-ai] salary normalization failed: %s", str(exc))
            fallback = self._fallback_salary(raw)
            self._salary_cache[raw.lower()] = fallback
            return fallback

    def _salary_matches_sync(
        self,
        user_salary_pref: str,
        post_salary: str,
        include_no_salary: bool,
    ) -> tuple[bool, str]:
        user_pref = user_salary_pref.strip()
        if not user_pref:
            if post_salary.strip() or include_no_salary:
                return (True, "No budget or rate filter.")
            return (False, "Opportunity budget or rate is missing and visible-payment-only mode is enabled.")

        normalized_pref = self._normalize_salary_sync(user_pref)
        post_norm = self._normalize_salary_sync(post_salary)
        if not post_norm.is_specified:
            return (
                include_no_salary,
                (
                    "Opportunity has no visible budget or rate."
                    if include_no_salary
                    else "Opportunity has no visible budget or rate and visible-payment-only mode is enabled."
                ),
            )

        pref_low, pref_high = self._normalized_usd_bounds(normalized_pref)
        post_low, post_high = self._normalized_usd_bounds(post_norm)
        if pref_low is None and pref_high is None:
            return (True, "User payment preference could not be normalized; pass.")
        if post_low is None and post_high is None:
            return (
                include_no_salary,
                "Opportunity payment details could not be normalized."
                if include_no_salary
                else "Opportunity payment details could not be normalized and visible-payment-only mode is enabled.",
            )

        # Compare range overlap.
        candidate_low = post_low if post_low is not None else post_high
        candidate_high = post_high if post_high is not None else post_low
        if candidate_low is None or candidate_high is None:
            return (True, "Payment details are incomplete but still acceptable.")

        if pref_low is not None and candidate_high < pref_low:
            return (False, "Opportunity budget or rate is below the preferred minimum.")
        if pref_high is not None and candidate_low > pref_high:
            return (False, "Opportunity budget or rate is above the preferred maximum.")
        return (True, "Opportunity payment matches the preferred range.")

    def _salary_matches_structured_sync(
        self,
        user_salary_pref: dict[str, object],
        post_salary: str,
        include_no_salary: bool,
    ) -> tuple[bool, str]:
        pref_low = _to_float_or_none(user_salary_pref.get("min_usd"))
        pref_high = _to_float_or_none(user_salary_pref.get("max_usd"))
        raw_pref = str(user_salary_pref.get("salary_range_usd") or "").strip()
        if pref_low is None and pref_high is None:
            return self._salary_matches_sync(raw_pref, post_salary, include_no_salary)

        post_norm = self._normalize_salary_sync(post_salary)
        if not post_norm.is_specified:
            return (
                include_no_salary,
                (
                    "Opportunity has no visible budget or rate."
                    if include_no_salary
                    else "Opportunity has no visible budget or rate and visible-payment-only mode is enabled."
                ),
            )

        post_low, post_high = self._normalized_usd_bounds(post_norm)
        if post_low is None and post_high is None:
            return (
                include_no_salary,
                "Opportunity payment details could not be normalized."
                if include_no_salary
                else "Opportunity payment details could not be normalized and visible-payment-only mode is enabled.",
            )

        candidate_low = post_low if post_low is not None else post_high
        candidate_high = post_high if post_high is not None else post_low
        if candidate_low is None or candidate_high is None:
            return (True, "Payment details are incomplete but still acceptable.")

        if pref_low is not None and candidate_high < pref_low:
            return (False, "Opportunity budget or rate is below the structured preferred minimum.")
        if pref_high is not None and candidate_low > pref_high:
            return (False, "Opportunity budget or rate is above the structured preferred maximum.")
        return (True, "Opportunity payment matches the structured preferred range.")

    def _expand_keywords_sync(self, keywords: list[str]) -> list[str]:
        return list(self._interpret_keywords_sync(keywords).expanded_keywords)

    def _interpret_keywords_sync(self, keywords: list[str]) -> KeywordInterpretation:
        cleaned: list[str] = []
        seen_cleaned: set[str] = set()
        for raw_item in keywords:
            item = raw_item.strip().lower()
            if not item or item in seen_cleaned:
                continue
            seen_cleaned.add(item)
            cleaned.append(item)
        if not cleaned:
            return KeywordInterpretation(input_keywords=[], understood_keywords=[], expanded_keywords=[], per_keyword=[])
        cache_key = "|".join(cleaned)
        cached_interpretation = self._keyword_interpretation_cache.get(cache_key)
        if cached_interpretation is not None:
            return KeywordInterpretation(
                input_keywords=list(cached_interpretation.input_keywords),
                understood_keywords=list(cached_interpretation.understood_keywords),
                expanded_keywords=list(cached_interpretation.expanded_keywords),
                per_keyword=[
                    KeywordExpansionItem(
                        input_keyword=item.input_keyword,
                        understood_as=item.understood_as,
                        similar_keywords=list(item.similar_keywords),
                    )
                    for item in cached_interpretation.per_keyword
                ],
            )

        remote_tokens = {
            "remote",
            "wfh",
            "work from home",
            "anywhere",
            "worldwide",
            "distributed",
            "удаленно",
            "удалённо",
            "удаленка",
            "удалёнка",
            "на дому",
            "из дома",
        }
        if self.client is None:
            fallback_items: list[KeywordExpansionItem] = []
            combined_keywords: list[str] = []
            for keyword in cleaned:
                variations = self._fallback_keyword_variations(keyword, remote_tokens)
                fallback_items.append(
                    KeywordExpansionItem(
                        input_keyword=keyword,
                        understood_as=keyword,
                        similar_keywords=variations,
                    )
                )
                combined_keywords.extend([keyword, *variations])
            interpretation = self._build_keyword_interpretation(cleaned, fallback_items, combined_keywords)
            self._keyword_interpretation_cache[cache_key] = interpretation
            self._keywords_cache[cache_key] = list(interpretation.expanded_keywords)
            return interpretation

        try:
            data = self._chat_json_for_model(
                system_prompt=(
                    "You are a professional keyword interpretation engine for freelance project alerts. Return JSON only. "
                    "For each input keyword, infer the intended hiring meaning and generate up to 10 close, relevant, "
                    "professionally accurate similar terms. Do not broaden the meaning. "
                    "Inputs may be English, Russian, or mixed-language."
                ),
                payload={
                    "keywords": cleaned,
                    "task": (
                        "Interpret each keyword exactly as a recruiter or job seeker would use it, "
                        "then expand it with up to 10 similar hiring terms. "
                        "Preserve the original language when possible, and include a common English equivalent only when it materially helps matching."
                    ),
                    "constraints": {
                        "max_similar_per_keyword": 10,
                        "max_total_terms": 100,
                        "lowercase": True,
                        "single_or_short_phrase": True,
                        "preserve_original_keywords": True,
                        "avoid_unrelated_broad_terms": True,
                    },
                    "return_format": {
                        "items": [
                            {
                                "input_keyword": "string",
                                "understood_as": "string",
                                "similar_keywords": ["term1", "term2"],
                            }
                        ],
                        "expanded_keywords": ["term1", "term2"],
                    },
                },
                model=self.keyword_model,
            )
            raw_items = data.get("items", [])
            structured_items: list[KeywordExpansionItem] = []
            combined_keywords: list[str] = []
            if isinstance(raw_items, list):
                for raw_item in raw_items:
                    if not isinstance(raw_item, dict):
                        continue
                    input_keyword = str(raw_item.get("input_keyword", "")).strip().lower()
                    understood_as = str(raw_item.get("understood_as", input_keyword)).strip().lower() or input_keyword
                    raw_similar = raw_item.get("similar_keywords", [])
                    similar_keywords = []
                    if isinstance(raw_similar, list):
                        seen_similar: set[str] = set()
                        for raw_value in raw_similar:
                            cleaned_value = str(raw_value).strip().lower()
                            if not cleaned_value or cleaned_value == input_keyword or cleaned_value in seen_similar:
                                continue
                            seen_similar.add(cleaned_value)
                            similar_keywords.append(cleaned_value)
                            if len(similar_keywords) >= 10:
                                break
                    if input_keyword:
                        structured_items.append(
                            KeywordExpansionItem(
                                input_keyword=input_keyword,
                                understood_as=understood_as,
                                similar_keywords=similar_keywords,
                            )
                        )
                        combined_keywords.extend([input_keyword, understood_as, *similar_keywords])
            raw_expanded = data.get("expanded_keywords", [])
            if isinstance(raw_expanded, list):
                combined_keywords.extend(str(item).strip().lower() for item in raw_expanded if str(item).strip())
            if not structured_items:
                for keyword in cleaned:
                    structured_items.append(
                        KeywordExpansionItem(
                            input_keyword=keyword,
                            understood_as=keyword,
                            similar_keywords=[],
                        )
                    )
                    combined_keywords.append(keyword)
            interpretation = self._build_keyword_interpretation(cleaned, structured_items, combined_keywords)
            self._keyword_interpretation_cache[cache_key] = interpretation
            self._keywords_cache[cache_key] = list(interpretation.expanded_keywords)
            return interpretation
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("[filter-ai] keyword expansion failed: %s", str(exc))
            fallback_items = [
                KeywordExpansionItem(
                    input_keyword=keyword,
                    understood_as=keyword,
                    similar_keywords=self._fallback_keyword_variations(keyword, remote_tokens),
                )
                for keyword in cleaned
            ]
            combined_keywords = [value for item in fallback_items for value in [item.input_keyword, item.understood_as, *item.similar_keywords]]
            interpretation = self._build_keyword_interpretation(cleaned, fallback_items, combined_keywords)
            self._keyword_interpretation_cache[cache_key] = interpretation
            self._keywords_cache[cache_key] = list(interpretation.expanded_keywords)
            return interpretation

    def _confirm_post_matches_filters_sync(
        self,
        post_x: dict[str, Any],
        user_filters: dict[str, Any],
        precheck_results: dict[str, dict[str, str | bool]],
    ) -> tuple[bool, str]:
        output_language = self._detect_output_language(post_x=post_x, user_filters=user_filters)
        payload = {
            "post_x": post_x,
            "user_filters": user_filters,
            "precheck_results": precheck_results,
            "output_language": output_language,
        }
        cache_key = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        cached = self._post_filter_match_cache.get(cache_key)
        if cached is not None:
            return cached

        failed_prechecks = self._failed_precheck_names(precheck_results)
        all_prechecks_pass = not failed_prechecks

        if self.client is None:
            result = self._precheck_fallback_decision(
                all_prechecks_pass,
                failed_prechecks,
                "AI unavailable",
                output_language=output_language,
            )
            self._post_filter_match_cache[cache_key] = result
            return result

        try:
            data = self._chat_json_for_model(
                system_prompt=(
                    "You are a careful but reasonably tolerant subscriber filter validator for freelance project alerts. Return JSON only. "
                    "Answer the question: Is this a relevant freelance opportunity for this user? "
                    "Evaluate active filters carefully, but do not require a perfect textbook match when the saved filters "
                    "are broad."
                ),
                payload={
                    "post_x": post_x,
                    "user_filters": user_filters,
                    "precheck_results": precheck_results,
                    "task": (
                        "Confirm whether post X should be delivered to this subscriber as a relevant freelance opportunity. "
                        "Treat freelance projects, contracts, consulting work, retainers, gigs, briefs, request-for-proposal posts, "
                        "and short engagements as first-class matches, not tolerated exceptions. "
                        "Use semantic matching, not only exact substrings, and respect location, budget/rate, role, keyword, "
                        "deliverables, and website constraints. Ignore inactive or empty filters. "
                        "If feedback_guidance is present, use it to personalize matching toward opportunities the user previously "
                        "liked and away from opportunities the user previously disliked. "
                        "For role_title, identify the primary scope, deliverables, and what the client/company/requester actually wants delivered. "
                        "Match on the real client need, not only the formal title. "
                        "Reject the post if role_title is only a side skill, collaborator mention, adjacent team, or a "
                        "keyword overlap while the primary opportunity is clearly something else. "
                        "If role_title is a broad UX/UI request such as UX/UI Designer, UX Designer, UI Designer, Product "
                        "Designer, Visual Designer, or Web Designer, be tolerant of adjacent design roles and mixed "
                        "design-build roles when the post shows real design ownership, UX/UI deliverables, landing page redesign, dashboard redesign, "
                        "app wireframes, product audit work, Figma cleanup, design systems, prototypes, user flows, usability work, "
                        "client project help, product/visual/web design responsibility, or interaction-design ownership. Reject only when the role is clearly "
                        "engineering-first and the design signals are incidental. "
                        "Be similarly tolerant for other freelance domains when the deliverables clearly align, including frontend implementation, copywriting, marketing, and video editing work. "
                        "Respect these location rules exactly: "
                        "'all_locations' accepts every location; "
                        f"'remote_global_only' follows this rule exactly: {REMOTE_GLOBAL_RULE} In plain words, the post must be globally remote. "
                        f"'remote_within_country' follows this rule exactly: {REMOTE_WITHIN_COUNTRY_RULE} If user_filters.remote_country is present, treat remote_country as the country attached to this rule. "
                        "'onsite_hybrid_within_country' accepts only non-remote or hybrid posts and only when the "
                        "opportunity scope, location, or description semantically matches the specified onsite_hybrid_country; "
                        "reject fully remote posts; "
                        "'specific_location' must match the requested location explicitly. "
                        "If user_filters.location_filters contains entries, treat them as an OR list of saved location "
                        "filters. Each entry contains a rule and may include label, country, or location. Apply the same "
                        "location semantics to each entry and accept the post when any one entry matches; reject only if "
                        "all entries fail. "
                        "If user_filters.location_preferences contains multiple explicit locations, treat them as OR and "
                        "accept the post when any one of those locations matches. "
                        "If user_filters.project_preferences is present, treat it as a real set of active freelance filters, not as optional notes. "
                        "Interpret deliverables and budget/rate minimums in a freelance-project way. "
                        "If the post looks like the right freelance scope but is under-specified, prefer a tolerant match unless a strict filter clearly conflicts. "
                        "Timezone preferences or collaboration windows do not block a match by themselves unless the post clearly restricts who can take the opportunity by country, citizenship, residency, or work authorization. "
                        "Write the final reason in the same language as output_language."
                    ),
                    "return_format": {
                        "match": "bool",
                        "reason": "short_string",
                        "failed_filters": ["filter_name_1", "filter_name_2"],
                    },
                },
                model=self.final_match_model,
            )
            match = bool(data.get("match", False))
            reason = str(data.get("reason", "")).strip()
            raw_failed = data.get("failed_filters", [])
            failed_filters = []
            if isinstance(raw_failed, list):
                failed_filters = [str(item).strip() for item in raw_failed if str(item).strip()]
            if not reason:
                if failed_filters:
                    reason = localized_text(
                        output_language,
                        en=f"AI rejected filters: {', '.join(failed_filters)}",
                        ru=f"Проверка ИИ отклонила фильтры: {', '.join(failed_filters)}",
                    )
                else:
                    reason = localized_text(
                        output_language,
                        en="AI final filter decision.",
                        ru="Итоговая проверка ИИ по фильтрам.",
                    )
            result = (match, reason)
            self._post_filter_match_cache[cache_key] = result
            return result
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("[filter-ai] post filter confirmation failed: %s", str(exc))
            result = self._precheck_fallback_decision(
                all_prechecks_pass,
                failed_prechecks,
                "AI confirmation failed",
                output_language=output_language,
            )
            self._post_filter_match_cache[cache_key] = result
            return result

    def _assess_role_alignment_sync(
        self,
        requested_role: str,
        post_title: str,
        post_description: str,
        post_location: str = "",
    ) -> tuple[bool, str]:
        requested_clean = requested_role.strip()
        if not requested_clean:
            return (True, "No role filter.")

        payload = {
            "requested_role": requested_clean,
            "post_title": str(post_title or "").strip(),
            "post_description": str(post_description or "").strip(),
            "post_location": str(post_location or "").strip(),
        }
        cache_key = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        cached = self._role_alignment_cache.get(cache_key)
        if cached is not None:
            return cached

        if self.client is None:
            result = self._fallback_role_alignment(
                requested_clean,
                payload["post_title"],
                payload["post_description"],
            )
            self._role_alignment_cache[cache_key] = result
            return result

        try:
            data = self._chat_json_for_model(
                system_prompt=(
                    "You are a careful but reasonably tolerant freelance opportunity matcher. Return JSON only. "
                    "Decide whether the post is genuinely relevant for the requested role or freelance scope. "
                    "Identify the primary role, deliverables, and the core need the client, company, or requester describes. "
                    "Reject when the requested role is only a side skill, collaborator mention, secondary task, "
                    "or keyword overlap while the primary opportunity is clearly something else. "
                    "When the requested role is a broad UX/UI request such as UX/UI Designer, UX Designer, UI Designer, "
                    "Product Designer, Web Designer, or a similarly broad design request, be tolerant of adjacent design "
                    "titles and mixed design-build roles when the post shows real design ownership, UX/UI deliverables, "
                    "wireframes, prototypes, user flows, usability work, Figma, design systems, landing page redesign, dashboard redesign, "
                    "app wireframes, product audits, Figma cleanup, product design, visual design, web design, or interaction-design responsibility. "
                    "For other domains, match on what the client wants delivered, not only on a formal title. "
                    "Accept mixed freelance scopes when the requested role is a clear part of the deliverables. "
                    "Do not reject solely because the post is a project, contract, freelance, consultant, temporary, "
                    "or short engagement. Treat those as valid opportunities. "
                    "Reject only when the role is clearly engineering-first and the design signals are incidental."
                ),
                payload={
                    **payload,
                    "task": (
                        "Determine whether the requested_role is the true primary target or a clearly relevant adjacent freelance scope. "
                        "Summarize what the client/company/requester actually wants delivered in the reason. "
                        "For broad freelance requests, prefer tolerance rather than perfection when the deliverables align."
                    ),
                    "return_format": {
                        "match": "bool",
                        "reason": "short_string",
                        "primary_role": "short_string",
                        "customer_need": "short_string",
                    },
                },
                model=self.final_match_model,
            )
            match = bool(data.get("match", False))
            reason = str(data.get("reason", "")).strip()
            primary_role = str(data.get("primary_role", "")).strip()
            customer_need = str(data.get("customer_need", "")).strip()
            if not reason:
                reason_parts: list[str] = []
                if primary_role:
                    reason_parts.append(f"Primary role: {primary_role}.")
                if customer_need:
                    reason_parts.append(f"Client/requester wants: {customer_need}.")
                reason = " ".join(reason_parts).strip() or "AI role-alignment decision."
            result = (match, reason)
            self._role_alignment_cache[cache_key] = result
            return result
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("[filter-ai] role alignment failed: %s", str(exc))
            result = self._fallback_role_alignment(
                requested_clean,
                payload["post_title"],
                payload["post_description"],
            )
            self._role_alignment_cache[cache_key] = result
            return result

    def _assess_work_arrangement_sync(
        self,
        post_title: str,
        post_description: str,
        post_location: str = "",
        post_notes: str = "",
    ) -> WorkArrangementAssessment:
        payload = {
            "post_title": str(post_title or "").strip(),
            "post_description": str(post_description or "").strip(),
            "post_location": str(post_location or "").strip(),
            "post_notes": str(post_notes or "").strip(),
        }
        cache_key = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        cached = self._work_arrangement_cache.get(cache_key)
        if cached is not None:
            return cached

        if self.client is None:
            result = self._fallback_work_arrangement_assessment(
                payload["post_title"],
                payload["post_description"],
                payload["post_location"],
                payload["post_notes"],
            )
            self._work_arrangement_cache[cache_key] = result
            return result

        try:
            data = self._chat_json_for_model(
                system_prompt=(
                    "You are a strict work-arrangement classifier for freelance project alerts. Return JSON only. "
                    "Classify whether the role is fully remote, hybrid, on-site, or unclear. "
                    "For remote roles, classify the remote scope precisely using the product rules below. "
                    f"{REMOTE_GLOBAL_RULE} "
                    f"{REMOTE_WITHIN_COUNTRY_RULE} "
                    "Do not treat timezone preferences, payroll wording, team distribution wording, or optional office hubs "
                    "as country restrictions by themselves. "
                    "Hybrid means office attendance is required for part of the schedule. "
                    "On-site means office or physical presence is required."
                ),
                payload={
                    **payload,
                    "task": (
                        "Classify the work arrangement and remote scope from the title, description, location, and notes. "
                        "Extract only the restrictions that matter for country eligibility: country limitation, "
                        "nationality or citizenship limitation, residency limitation, or work authorization tied to a country. "
                        "If the job is fully remote and has none of those restrictions, mark remote_scope as global. "
                        "If the role is remote and clearly country-limited, mark remote_scope as country_limited. "
                        "If the role is hybrid or on-site, remote_scope must be not_remote. "
                        f"{REMOTE_SCOPE_PROMPT_BLOCK}"
                    ),
                    "return_format": {
                        "work_mode": "remote|hybrid|onsite|unknown",
                        "remote_scope": "global|country_limited|not_remote|unknown",
                        "scope_locations": ["country_or_country_specific_scope"],
                        "has_scope_restriction": "bool",
                        "restriction_reason_code": "country_restriction|nationality_or_citizenship_restriction|residency_restriction|work_authorization_country_restriction|none",
                        "confidence": "float_0_to_1",
                        "reason": "short_string",
                    },
                },
                model=self.final_match_model,
            )
            work_mode = str(data.get("work_mode", "unknown")).strip().lower() or "unknown"
            if work_mode not in {"remote", "hybrid", "onsite", "unknown"}:
                work_mode = "unknown"
            remote_scope = str(data.get("remote_scope", "unknown")).strip().lower() or "unknown"
            if remote_scope not in {
                "global",
                "country_limited",
                "not_remote",
                "unknown",
            }:
                remote_scope = "unknown"
            raw_scope_locations = data.get("scope_locations", [])
            scope_locations = (
                tuple(str(item).strip() for item in raw_scope_locations if str(item).strip())[:6]
                if isinstance(raw_scope_locations, list)
                else ()
            )
            restriction_reason_code = str(data.get("restriction_reason_code", "")).strip().lower()
            if restriction_reason_code in {"", "none", "unknown"}:
                restriction_reason_code = ""
            if restriction_reason_code not in {
                "",
                "country_restriction",
                "nationality_or_citizenship_restriction",
                "residency_restriction",
                "work_authorization_country_restriction",
            }:
                restriction_reason_code = ""
            has_scope_restriction = bool(
                data.get("has_scope_restriction", False)
                or remote_scope == "country_limited"
                or bool(restriction_reason_code)
                or bool(scope_locations)
            )
            confidence = max(0.0, min(_to_float_or_none(data.get("confidence")) or 0.0, 1.0))
            if work_mode != "remote" and remote_scope == "global":
                remote_scope = "not_remote"
            reason = str(data.get("reason", "")).strip()
            if not reason:
                scoped_value = ", ".join(scope_locations[:3]).strip()
                if work_mode == "remote" and not has_scope_restriction:
                    reason = "Remote role shows no country or eligibility restriction."
                elif work_mode == "remote":
                    reason = _remote_scope_reason_for_code(restriction_reason_code or "country_restriction", scoped_value=scoped_value)
                elif remote_scope and remote_scope != "unknown":
                    reason = f"{work_mode.title()} role with remote scope {remote_scope}."
                else:
                    reason = "AI work-arrangement decision."
            result = WorkArrangementAssessment(
                work_mode=work_mode,
                remote_scope=remote_scope,
                scope_locations=scope_locations,
                has_scope_restriction=has_scope_restriction,
                confidence=confidence,
                reason=reason,
                restriction_reason_code=restriction_reason_code,
            )
            self._work_arrangement_cache[cache_key] = result
            return result
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("[filter-ai] work-arrangement assessment failed: %s", str(exc))
            result = self._fallback_work_arrangement_assessment(
                payload["post_title"],
                payload["post_description"],
                payload["post_location"],
                payload["post_notes"],
            )
            self._work_arrangement_cache[cache_key] = result
            return result

    def _check_remote_global_match_sync(
        self,
        post_title: str,
        post_description: str,
        post_location: str = "",
    ) -> tuple[bool, str]:
        payload = {
            "post_title": str(post_title or "").strip(),
            "post_description": str(post_description or "").strip(),
            "post_location": str(post_location or "").strip(),
        }
        cache_key = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        cached = self._remote_scope_cache.get(cache_key)
        if cached is not None:
            return cached

        assessment = self._assess_work_arrangement_sync(
            payload["post_title"],
            payload["post_description"],
            payload["post_location"],
        )
        if assessment.is_hybrid:
            result = (False, assessment.reason or "Hybrid roles are not treated as globally remote.")
        elif not assessment.is_remote:
            result = (False, assessment.reason or "Post is not a fully remote role.")
        elif assessment.remote_scope == "global" or (
            not assessment.has_scope_restriction and assessment.remote_scope in {"unknown", "global"}
        ):
            result = (True, assessment.reason or "Remote role is open from anywhere with no country restriction.")
        else:
            result = (
                False,
                assessment.reason
                or _remote_scope_reason_for_code(
                    assessment.restriction_reason_code or "country_restriction",
                    scoped_value=", ".join(assessment.scope_locations[:3]),
                ),
            )
        self._remote_scope_cache[cache_key] = result
        return result

    def _check_remote_country_eligibility_sync(
        self,
        target_country: str,
        post_title: str,
        post_description: str,
        post_location: str = "",
        post_notes: str = "",
    ) -> tuple[bool, str]:
        normalized_country = str(target_country or "").strip()
        if not normalized_country:
            return (False, "Target country is missing.")
        payload = {
            "target_country": normalized_country,
            "post_title": str(post_title or "").strip(),
            "post_description": str(post_description or "").strip(),
            "post_location": str(post_location or "").strip(),
            "post_notes": str(post_notes or "").strip(),
        }
        cache_key = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        cached = self._remote_country_cache.get(cache_key)
        if cached is not None:
            return cached

        assessment = self._assess_work_arrangement_sync(
            payload["post_title"],
            payload["post_description"],
            payload["post_location"],
            payload["post_notes"],
        )
        if assessment.is_hybrid:
            result = (False, assessment.reason or "Hybrid roles are not treated as fully remote-within-country.")
            self._remote_country_cache[cache_key] = result
            return result
        if not assessment.is_remote:
            result = (False, assessment.reason or "Post is not a fully remote role.")
            self._remote_country_cache[cache_key] = result
            return result
        if assessment.remote_scope == "global" and not assessment.has_scope_restriction:
            result = (
                False,
                f"Remote Global does not count for Remote within {normalized_country}; this mode only accepts explicit country-limited remote roles.",
            )
            self._remote_country_cache[cache_key] = result
            return result
        if assessment.remote_scope != "country_limited" and not assessment.has_scope_restriction:
            result = (
                False,
                f"Remote within {normalized_country} needs an explicit country-limited remote role.",
            )
            self._remote_country_cache[cache_key] = result
            return result

        scope_text = " ".join(
            part
            for part in (
                ", ".join(assessment.scope_locations),
                payload["post_location"],
                payload["post_description"],
                payload["post_notes"],
            )
            if part
        ).strip()
        location_match, location_reason = self._check_location_match_sync(normalized_country, scope_text)
        if location_match:
            result = (
                True,
                f"Remote role is explicitly country-limited and matches {normalized_country}.",
            )
        else:
            result = (
                False,
                location_reason.strip()
                or f"Remote role is country-limited, but not to {normalized_country}.",
            )
        self._remote_country_cache[cache_key] = result
        return result

    def _analyze_negative_match_feedback_sync(
        self,
        job_title: str,
        job_description: str,
        job_location: str,
        job_salary: str,
        match_reason: str,
        user_feedback: str,
    ) -> dict[str, str]:
        payload = {
            "job_title": str(job_title or "").strip(),
            "job_description": str(job_description or "").strip(),
            "job_location": str(job_location or "").strip(),
            "job_salary": str(job_salary or "").strip(),
            "match_reason": str(match_reason or "").strip(),
            "user_feedback": str(user_feedback or "").strip(),
        }
        cache_key = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        cached = self._feedback_analysis_cache.get(cache_key)
        if cached is not None:
            return dict(cached)

        if self.client is None:
            result = self._fallback_negative_feedback_analysis(payload["user_feedback"])
            self._feedback_analysis_cache[cache_key] = dict(result)
            return result

        try:
            data = self._chat_json_for_model(
                system_prompt=(
                    "You are a strict learning assistant for freelance opportunity relevance feedback. Return JSON only. "
                    "Read the delivered opportunity and the subscriber's complaint, then extract future-matching guidance. "
                    "Identify whether the mismatch was about role/title, location, budget or rate, remote scope, deliverables, "
                    "or something else."
                ),
                payload={
                    **payload,
                    "task": (
                        "Summarize the mistake so future matching can avoid similar opportunities. "
                        "Keep each field concise and factual."
                    ),
                    "return_format": {
                        "summary": "short_string",
                        "job_title_issue": "short_string_or_empty",
                        "location_issue": "short_string_or_empty",
                        "salary_issue": "short_string_or_empty",
                        "other_issue": "short_string_or_empty",
                    },
                },
                model=self.final_match_model,
            )
            result = {
                "summary": str(data.get("summary", "")).strip(),
                "job_title_issue": str(data.get("job_title_issue", "")).strip(),
                "location_issue": str(data.get("location_issue", "")).strip(),
                "salary_issue": str(data.get("salary_issue", "")).strip(),
                "other_issue": str(data.get("other_issue", "")).strip(),
            }
            if not any(result.values()):
                result = self._fallback_negative_feedback_analysis(payload["user_feedback"])
            self._feedback_analysis_cache[cache_key] = dict(result)
            return result
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("[filter-ai] negative feedback analysis failed: %s", str(exc))
            result = self._fallback_negative_feedback_analysis(payload["user_feedback"])
            self._feedback_analysis_cache[cache_key] = dict(result)
            return result

    @staticmethod
    def _fallback_keyword_variations(keyword: str, remote_tokens: set[str]) -> list[str]:
        candidates: list[str] = []
        normalized = keyword.strip().lower().replace("ё", "е")
        if "/" in normalized:
            candidates.append(normalized.replace("/", " "))
        if "-" in normalized:
            candidates.append(normalized.replace("-", " "))
        if "&" in normalized:
            candidates.append(normalized.replace("&", "and"))
        compact = normalize_match_text(normalized)
        if compact and compact != normalized:
            candidates.append(compact)
        if any(token in normalized for token in remote_tokens):
            candidates.append("remote")
            candidates.extend(["удаленно", "удалённо", "удаленка", "удалёнка"])
        if "ux/ui" in normalized or "ux ui" in compact:
            candidates.extend(["ux ui", "ui ux", "дизайнер ux ui", "ux ui дизайнер"])
        if "web designer" in compact or "веб дизайнер" in compact:
            candidates.extend(["веб дизайнер", "web designer"])
        seen: set[str] = set()
        filtered: list[str] = []
        for value in candidates:
            cleaned = value.strip().lower()
            if not cleaned or cleaned == normalized or cleaned in seen:
                continue
            seen.add(cleaned)
            filtered.append(cleaned)
            if len(filtered) >= 10:
                break
        return filtered

    def _build_keyword_interpretation(
        self,
        cleaned_keywords: list[str],
        items: list[KeywordExpansionItem],
        combined_keywords: list[str],
    ) -> KeywordInterpretation:
        remote_tokens = {"remote", "wfh", "work from home", "anywhere", "worldwide", "distributed"}
        seen: set[str] = set()
        ordered_terms: list[str] = []
        for value in combined_keywords:
            raw_value = str(value).strip().lower()
            candidates = [raw_value, normalize_match_text(value), *tokenize_with_morphology(raw_value, min_length=2)]
            for candidate in candidates:
                cleaned_value = str(candidate).strip().lower()
                dedupe_key = normalize_match_text(cleaned_value) or cleaned_value
                stored_value = cleaned_value or dedupe_key
                if not dedupe_key or dedupe_key in seen or not stored_value:
                    continue
                seen.add(dedupe_key)
                ordered_terms.append(stored_value)
        if any(any(token in term for token in remote_tokens) for term in ordered_terms) and "remote" not in seen:
            ordered_terms.append("remote")
        ordered_terms = ordered_terms[:100]
        understood_keywords = []
        for item in items:
            understood = item.understood_as.strip().lower() or item.input_keyword.strip().lower()
            if understood and understood not in understood_keywords:
                understood_keywords.append(understood)
        return KeywordInterpretation(
            input_keywords=list(cleaned_keywords),
            understood_keywords=understood_keywords,
            expanded_keywords=ordered_terms,
            per_keyword=[
                KeywordExpansionItem(
                    input_keyword=item.input_keyword.strip().lower(),
                    understood_as=item.understood_as.strip().lower() or item.input_keyword.strip().lower(),
                    similar_keywords=[
                        value.strip().lower()
                        for value in item.similar_keywords[:10]
                        if value.strip() and value.strip().lower() != item.input_keyword.strip().lower()
                    ],
                )
                for item in items
            ],
        )

    @staticmethod
    def _failed_precheck_names(precheck_results: dict[str, dict[str, str | bool]]) -> list[str]:
        failed: list[str] = []
        for filter_name, details in precheck_results.items():
            matched = False
            if isinstance(details, dict):
                matched = bool(details.get("matched", False))
            if not matched:
                failed.append(filter_name)
        return failed

    @staticmethod
    def _precheck_fallback_decision(
        all_prechecks_pass: bool,
        failed_prechecks: list[str],
        prefix: str,
        *,
        output_language: str = "en",
    ) -> tuple[bool, str]:
        if all_prechecks_pass:
            return (
                True,
                localized_text(
                    output_language,
                    en=f"{prefix}; all preliminary filter checks passed.",
                    ru=f"{prefix}; базовые фильтры пройдены.",
                ),
            )
        failed = ", ".join(failed_prechecks) or "unknown"
        return (
            False,
            localized_text(
                output_language,
                en=f"{prefix}; preliminary checks failed: {failed}.",
                ru=f"{prefix}; базовые фильтры не пройдены: {failed}.",
            ),
        )

    @staticmethod
    def _normalize_match_text(value: str) -> str:
        return normalize_match_text(value)

    @staticmethod
    def _detect_output_language(*, post_x: dict[str, Any], user_filters: dict[str, Any]) -> str:
        explicit = str(user_filters.get("output_language", "")).strip().lower()
        if explicit in {"ru", "en"}:
            return explicit
        post_language = str(post_x.get("language", "")).strip().lower()
        if post_language in {"ru", "en"}:
            return post_language
        combined = " ".join(
            str(value).strip()
            for value in (
                post_x.get("title", ""),
                post_x.get("description", ""),
                post_x.get("location", ""),
                user_filters.get("role_title", ""),
                user_filters.get("location_preference", ""),
            )
            if str(value).strip()
        )
        return detect_language(text=combined)

    @staticmethod
    def _role_head_token(normalized_role: str) -> str:
        ignored = {
            "senior",
            "junior",
            "lead",
            "principal",
            "staff",
            "intern",
            "remote",
            "global",
            "contract",
            "full",
            "time",
            "part",
            "старший",
            "младший",
            "ведущий",
            "удаленно",
            "удалённо",
            "удаленка",
            "удалёнка",
            "полная",
            "частичная",
        }
        tokens = [token for token in normalized_role.split() if token and token not in ignored]
        return tokens[-1] if tokens else ""

    @classmethod
    def _looks_like_ux_ui_topic(cls, normalized_terms: list[str]) -> bool:
        return any(marker in term for term in normalized_terms for marker in cls.UX_UI_TOPIC_MARKERS)

    @classmethod
    def _ux_ui_signal_summary(cls, normalized_content: str) -> dict[str, object]:
        normalized = str(normalized_content or "").strip()
        has_ux_ui_role = any(marker in normalized for marker in cls.UX_UI_ROLE_MARKERS)
        ux_ui_work_hits = sum(1 for marker in cls.UX_UI_WORK_MARKERS if marker in normalized)
        has_frontend_role = any(marker in normalized for marker in cls.FRONTEND_ROLE_MARKERS)
        has_designer_word = "designer" in normalized or "РґРёР·Р°Р№РЅРµСЂ" in normalized
        has_design_word = "design" in normalized or "РґРёР·Р°Р№РЅ" in normalized
        mixed_design_build_match = bool(
            has_frontend_role and (ux_ui_work_hits >= 2 or (ux_ui_work_hits >= 1 and has_designer_word))
        )
        adjacent_design_match = bool(
            has_ux_ui_role
            or (has_designer_word and ux_ui_work_hits >= 1)
            or (has_design_word and ux_ui_work_hits >= 2)
        )
        pure_frontend_role = bool(
            has_frontend_role and not has_ux_ui_role and ux_ui_work_hits == 0 and not has_designer_word
        )
        return {
            "has_ux_ui_role": has_ux_ui_role,
            "ux_ui_work_hits": ux_ui_work_hits,
            "has_frontend_role": has_frontend_role,
            "has_designer_word": has_designer_word,
            "has_design_word": has_design_word,
            "mixed_design_build_match": mixed_design_build_match,
            "adjacent_design_match": adjacent_design_match,
            "pure_frontend_role": pure_frontend_role,
        }

    @classmethod
    def _topic_guard_match(
        cls,
        normalized_terms: list[str],
        normalized_content: str,
        label: str,
    ) -> tuple[bool, str] | None:
        if label not in {"role", "sphere"}:
            return None
        if not cls._looks_like_ux_ui_topic(normalized_terms):
            return None

        broad_ux_ui_requests = {
            "ux designer",
            "ui designer",
            "ux ui designer",
            "ui ux designer",
            "user experience designer",
            "user interface designer",
            "product designer",
            "visual designer",
            "web designer",
            "designer",
            "ux дизайнер",
            "ui дизайнер",
            "дизайнер ux",
            "дизайнер ui",
            "дизайнер ux ui",
            "продуктовый дизайнер",
            "веб дизайнер",
            "дизайнер интерфейсов",
            "дизайнер",
        }
        adjacent_design_role_markers = (
            "product designer",
            "visual designer",
            "web designer",
            "digital designer",
            "interaction designer",
            "service designer",
            "design technologist",
            "design engineer",
            "ux researcher",
            "ui designer",
            "ux designer",
            "продуктовый дизайнер",
            "визуальный дизайнер",
            "веб дизайнер",
            "дизайнер интерфейсов",
            "ux исследователь",
        )

        has_ux_ui_role = any(marker in normalized_content for marker in cls.UX_UI_ROLE_MARKERS)
        has_ux_ui_work = any(marker in normalized_content for marker in cls.UX_UI_WORK_MARKERS)
        has_frontend_role = any(marker in normalized_content for marker in cls.FRONTEND_ROLE_MARKERS)
        has_adjacent_design_role = any(marker in normalized_content for marker in adjacent_design_role_markers)
        broad_request = any(term in broad_ux_ui_requests for term in normalized_terms)
        project_or_contract_context = any(
            token in normalized_content
            for token in (
                "project",
                "contract",
                "freelance",
                "gig",
                "consultant",
                "consulting",
                "short term",
                "temporary",
                "retainer",
                "проект",
                "контракт",
                "фриланс",
                "консультант",
            )
        )

        if has_frontend_role and not has_ux_ui_role and not has_ux_ui_work:
            return (False, f"Rejected {label} match: front-end/developer role is not UX/UI and has no real UX/UI ownership.")

        if has_ux_ui_role:
            return (True, f"Heuristic {label} match: UX/UI role signal found.")

        if has_ux_ui_work and any(token in normalized_content for token in ("design", "designer", "дизайн", "дизайнер", "figma")):
            return (True, f"Heuristic {label} match: UX/UI design-work signal found.")

        if broad_request and has_adjacent_design_role:
            return (True, f"Heuristic {label} match: adjacent design role accepted for a broad UX/UI request.")

        if broad_request and project_or_contract_context and (has_adjacent_design_role or has_ux_ui_work):
            return (True, f"Heuristic {label} match: project/contract design opportunity accepted for a broad UX/UI request.")

        return None
    @classmethod
    def _fallback_role_alignment(
        cls,
        requested_role: str,
        post_title: str,
        post_description: str,
    ) -> tuple[bool, str]:
        requested_norm = cls._normalize_match_text(requested_role)
        title_norm = cls._normalize_match_text(post_title)
        description_norm = cls._normalize_match_text(post_description)
        combined_norm = " ".join(part for part in (title_norm, description_norm) if part).strip()
        if not requested_norm:
            return (True, "No role filter.")
        if not combined_norm:
            return (False, "Missing opportunity content for role matching.")

        topic_guard = cls._topic_guard_match([requested_norm], combined_norm, "role")
        if topic_guard is not None and not topic_guard[0]:
            return topic_guard
        if topic_guard is not None and topic_guard[0]:
            return topic_guard

        if requested_norm and requested_norm in title_norm:
            return (True, f"Title directly matches requested role: {requested_role}.")

        broad_ux_ui_requests = {
            "ux designer",
            "ui designer",
            "ux ui designer",
            "ui ux designer",
            "user experience designer",
            "user interface designer",
            "product designer",
            "visual designer",
            "web designer",
            "designer",
            "ux дизайнер",
            "ui дизайнер",
            "дизайнер ux",
            "дизайнер ui",
            "дизайнер ux ui",
            "продуктовый дизайнер",
            "веб дизайнер",
            "дизайнер интерфейсов",
            "дизайнер",
        }
        adjacent_design_title_markers = (
            "product designer",
            "visual designer",
            "web designer",
            "digital designer",
            "interaction designer",
            "service designer",
            "design technologist",
            "design engineer",
            "ux researcher",
            "ui designer",
            "ux designer",
            "продуктовый дизайнер",
            "визуальный дизайнер",
            "веб дизайнер",
            "дизайнер интерфейсов",
            "ux исследователь",
        )
        project_or_contract_markers = (
            "project",
            "contract",
            "freelance",
            "gig",
            "consultant",
            "consulting",
            "short term",
            "temporary",
            "retainer",
            "проект",
            "контракт",
            "фриланс",
            "консультант",
        )

        if cls._looks_like_ux_ui_topic([requested_norm]):
            if any(marker in title_norm for marker in cls.UX_UI_ROLE_MARKERS):
                return (True, "Title shows a UX/UI-adjacent design role.")

            if requested_norm in broad_ux_ui_requests and any(marker in title_norm for marker in adjacent_design_title_markers):
                return (True, "Broad UX/UI request matched an adjacent design title.")

            ux_ui_work_hits = sum(1 for marker in cls.UX_UI_WORK_MARKERS if marker in combined_norm)
            design_context = any(
                token in combined_norm
                for token in ("design", "designer", "дизайн", "дизайнер", "figma", "prototype", "wireframe", "user flow")
            )

            if design_context and ux_ui_work_hits >= 2:
                return (True, "Post shows strong UX/UI design responsibility.")

            if any(token in combined_norm for token in project_or_contract_markers) and design_context and ux_ui_work_hits >= 2:
                return (True, "Project/contract post shows strong UX/UI design responsibility.")

            frontend_heavy = any(marker in title_norm for marker in cls.FRONTEND_ROLE_MARKERS)
            if frontend_heavy and ux_ui_work_hits < 2:
                return (False, f"Primary role does not look like {requested_role}; it appears engineering-first.")

            return (False, f"Primary role does not look like {requested_role}.")

        requested_tokens = {token for token in requested_norm.split() if token}
        title_tokens = {token for token in title_norm.split() if token}
        overlap = len(requested_tokens & title_tokens)
        head_token = cls._role_head_token(requested_norm)
        if head_token and head_token in title_tokens and overlap >= max(1, len(requested_tokens) - 1):
            return (True, f"Title aligns closely with requested role: {requested_role}.")
        if head_token and head_token in title_tokens and requested_norm in description_norm:
            return (True, f"Description confirms the requested role: {requested_role}.")
        return (False, f"Primary role appears different from {requested_role}.")
    @classmethod
    def _fallback_work_arrangement_assessment(
        cls,
        post_title: str,
        post_description: str,
        post_location: str,
        post_notes: str = "",
    ) -> WorkArrangementAssessment:
        raw_content = " ".join(part for part in (post_title, post_description, post_location, post_notes) if part).strip()
        content = normalize_match_text(raw_content)
        if not content:
            return WorkArrangementAssessment(
                work_mode="unknown",
                remote_scope="unknown",
                scope_locations=(),
                has_scope_restriction=False,
                confidence=0.0,
                reason="Missing opportunity content for work-arrangement check.",
            )

        remote_hints = {
            "remote",
            "work from home",
            "wfh",
            "anywhere",
            "worldwide",
            "global",
            "distributed",
            "home office",
            "fully remote",
            "100 remote",
            "удаленно",
            "удалённо",
            "удаленная",
            "удалённая",
            "удаленный",
            "удалённый",
            "удаленная работа",
            "удалённая работа",
            "удаленка",
            "удалёнка",
            "из дома",
            "на дому",
            "работа из дома",
        }
        hybrid_hints = {
            "hybrid",
            "remote office",
            "office and home",
            "split between home and office",
            "days in office",
            "day in office",
            "office days",
            "2 days in office",
            "3 days in office",
            "2x office",
            "3x office",
            "гибрид",
            "гибридный",
            "гибридный формат",
        }
        onsite_hints = {
            "on site",
            "onsite",
            "in office",
            "office based",
            "office attendance",
            "work from office",
            "relocation",
            "офис",
            "в офисе",
            "офисный",
        }
        restriction_patterns = (
            r"\b(?:us|usa|u\.s\.|uk|u\.k\.|europe|eu|emea|apac|latam|mena|india|egypt|canada|australia)\s+only\b",
            r"\bonly\s+(?:for|in|within)\s+[a-z]",
            r"\bremote\s*[-,/]?\s*(?:us|usa|uk|u\.k\.|europe|eu|emea|apac|latam|mena|india|egypt|canada|australia)\b",
            r"\bmust\s+(?:be|live|reside|work|remain)\b.{0,40}\b(?:in|within|from)\b",
            r"\bbased\s+in\b",
            r"\blocated\s+in\b",
            r"\bcandidates?\s+(?:in|from|based in|located in)\b",
            r"\bwork authorization\b",
            r"\bvisa\b",
            r"\bcitizen(?:ship)?\b",
            r"\beligible to work in\b",
            r"\bright to work in\b",
            r"\bтолько\s+(?:для|из|в)\b",
            r"\bкандидат(?:ы|ам)?\s+(?:из|в|на территории)\b",
            r"\bчасов(?:ой|ому)\s+пояс\b",
            r"\bпо\s+московскому\s+времени\b",
            r"\bгражданств\w*\b",
            r"\bразрешени\w+\s+на\s+работу\b",
            r"\bпроживани\w+\s+в\b",
            r"\bтрудоустройство\s+только\s+в\b",
        )
        global_markers = (
            "work from anywhere",
            "anywhere in the world",
            "globally remote",
            "global remote",
            "worldwide",
            "open worldwide",
            "open globally",
            "from anywhere",
            "можно работать из любой точки мира",
            "по всему миру",
            "по всему свету",
            "из любой точки мира",
        )

        has_remote = any(normalize_match_text(marker) in content for marker in remote_hints)
        has_hybrid = any(normalize_match_text(marker) in content for marker in hybrid_hints)
        has_onsite = any(normalize_match_text(marker) in content for marker in onsite_hints)
        restriction_reason_code = _detect_remote_restriction_reason_code(content)
        scope_hint = _extract_country_scope_hint(post_location)
        has_scope_restriction = bool(restriction_reason_code or scope_hint)
        has_global_scope = any(marker in content for marker in _GLOBAL_REMOTE_MARKERS)
        scope_locations = (scope_hint,) if scope_hint else ()

        if has_hybrid:
            return WorkArrangementAssessment(
                work_mode="hybrid",
                remote_scope="not_remote",
                scope_locations=scope_locations,
                has_scope_restriction=has_scope_restriction,
                confidence=0.72,
                reason="Hybrid work pattern detected from office cadence or mixed remote and office wording.",
                restriction_reason_code=restriction_reason_code,
            )
        if has_remote and not has_onsite:
            if has_global_scope and not has_scope_restriction:
                remote_scope = "global"
                reason = "Remote role shows no country or eligibility restriction."
            elif has_scope_restriction:
                remote_scope = "country_limited"
                reason = _remote_scope_reason_for_code(
                    restriction_reason_code or "country_restriction",
                    scoped_value=scope_hint,
                )
            else:
                remote_scope = "unknown"
                reason = "Remote role is fully remote, but the country scope is not explicit."
            return WorkArrangementAssessment(
                work_mode="remote",
                remote_scope=remote_scope,
                scope_locations=scope_locations,
                has_scope_restriction=has_scope_restriction,
                confidence=0.78 if has_global_scope else 0.74 if has_scope_restriction else 0.58,
                reason=reason,
                restriction_reason_code=restriction_reason_code,
            )
        if has_onsite:
            return WorkArrangementAssessment(
                work_mode="onsite",
                remote_scope="not_remote",
                scope_locations=scope_locations,
                has_scope_restriction=has_scope_restriction,
                confidence=0.74,
                reason="On-site or office-based wording detected.",
                restriction_reason_code=restriction_reason_code,
            )
        return WorkArrangementAssessment(
            work_mode="unknown",
            remote_scope="unknown",
            scope_locations=scope_locations,
            has_scope_restriction=has_scope_restriction,
            confidence=0.35,
            reason="No reliable remote, hybrid, or on-site signal detected.",
            restriction_reason_code=restriction_reason_code,
        )

    @classmethod
    def _fallback_remote_global_match(
        cls,
        post_title: str,
        post_description: str,
        post_location: str,
    ) -> tuple[bool, str]:
        assessment = cls._fallback_work_arrangement_assessment(post_title, post_description, post_location)
        if assessment.is_hybrid:
            return (False, assessment.reason)
        if not assessment.is_remote:
            return (False, assessment.reason or "Post is not clearly remote.")
        if assessment.remote_scope == "global" or (
            not assessment.has_scope_restriction and assessment.remote_scope in {"unknown", "global"}
        ):
            return (True, assessment.reason or "Remote role is open from anywhere with no country restriction.")
        return (
            False,
            assessment.reason
            or _remote_scope_reason_for_code(
                assessment.restriction_reason_code or "country_restriction",
                scoped_value=", ".join(assessment.scope_locations[:3]),
            ),
        )

    def _fallback_remote_country_eligibility(
        self,
        target_country: str,
        post_title: str,
        post_description: str,
        post_location: str,
        post_notes: str = "",
        *,
        assessment: WorkArrangementAssessment | None = None,
    ) -> tuple[bool, str]:
        normalized_country = str(target_country or "").strip()
        if not normalized_country:
            return (False, "Target country is missing.")
        resolved_assessment = assessment or self._fallback_work_arrangement_assessment(
            post_title,
            post_description,
            post_location,
            post_notes,
        )
        if resolved_assessment.is_hybrid:
            return (False, resolved_assessment.reason or "Hybrid roles are not treated as fully remote-within-country.")
        if not resolved_assessment.is_remote:
            return (False, resolved_assessment.reason or "Post is not clearly remote.")
        if resolved_assessment.remote_scope == "global" and not resolved_assessment.has_scope_restriction:
            return (
                False,
                f"Remote Global does not count for Remote within {normalized_country}; this mode only accepts explicit country-limited remote roles.",
            )
        if resolved_assessment.remote_scope != "country_limited" and not resolved_assessment.has_scope_restriction:
            return (False, f"Remote within {normalized_country} needs an explicit country-limited remote role.")
        combined = " ".join(
            part
            for part in (
                ", ".join(resolved_assessment.scope_locations),
                post_location,
                post_description,
                post_title,
                post_notes,
            )
            if part
        ).strip()
        matched, reason = self._check_location_match_sync(normalized_country, combined)
        if matched:
            return (True, f"Remote role is explicitly country-limited and matches {normalized_country}.")
        return (False, reason or f"Remote role is country-limited, but not to {normalized_country}.")

    @staticmethod
    def _fallback_negative_feedback_analysis(user_feedback: str) -> dict[str, str]:
        lowered = str(user_feedback or "").strip().lower()
        summary = lowered[:240]
        result = {
            "summary": summary or "User marked this as not relevant.",
            "job_title_issue": "",
            "location_issue": "",
            "salary_issue": "",
            "other_issue": "",
        }
        if any(
            token in lowered
            for token in {
                "title",
                "role",
                "position",
                "frontend",
                "backend",
                "designer",
                "developer",
                "должност",
                "роль",
                "позици",
                "фронтенд",
                "дизайнер",
                "разработчик",
            }
        ):
            result["job_title_issue"] = user_feedback.strip()
        if any(
            token in lowered
            for token in {
                "location",
                "country",
                "city",
                "remote",
                "onsite",
                "hybrid",
                "timezone",
                "локац",
                "страна",
                "город",
                "удален",
                "удалён",
                "офис",
                "гибрид",
                "часов",
            }
        ):
            result["location_issue"] = user_feedback.strip()
        if any(
            token in lowered
            for token in {"salary", "pay", "compensation", "budget", "rate", "зарплат", "оплат", "доход", "оклад"}
        ):
            result["salary_issue"] = user_feedback.strip()
        if not any(result[field] for field in ("job_title_issue", "location_issue", "salary_issue")):
            result["other_issue"] = user_feedback.strip() or "No specific reason provided."
        return result

    def _semantic_match_any_sync(
        self,
        terms: list[str],
        text: str,
        threshold: float,
        label: str,
    ) -> tuple[bool, str]:
        cleaned_terms = sorted({term.strip().lower() for term in terms if term.strip()})
        content = text.strip().lower()
        if not cleaned_terms:
            return (True, f"No {label} filters.")
        if not content:
            return (False, f"Empty content for {label} matching.")

        normalized_terms = [self._normalize_match_text(term) or term for term in cleaned_terms]
        term_token_sets: list[set[str]] = []
        for term, normalized_term in zip(cleaned_terms, normalized_terms):
            tokens = set(tokenize_with_morphology(term, min_length=2))
            if not tokens:
                tokens = {token for token in normalized_term.split() if token}
            term_token_sets.append(tokens)
        normalized_content = self._normalize_match_text(content)
        topic_guard = self._topic_guard_match(normalized_terms, normalized_content, label)
        if topic_guard is not None:
            return topic_guard

        # Fast lexical pass first to avoid unnecessary embedding calls.
        for term in cleaned_terms:
            if term in content:
                return (True, f"Lexical {label} match: {term}")
        for normalized_term in normalized_terms:
            if normalized_term and normalized_term in normalized_content:
                return (True, f"Normalized lexical {label} match: {normalized_term}")

        content_tokens = {token for token in re.split(r"[\s,;/|]+", content) if token}
        normalized_content_tokens = {token for token in normalized_content.split() if token}
        morphology_content_tokens = set(tokenize_with_morphology(content, min_length=2))
        for term, normalized_term, morphology_term_tokens in zip(cleaned_terms, normalized_terms, term_token_sets):
            term_tokens = {token for token in re.split(r"[\s,;/|]+", term) if token}
            if not term_tokens:
                term_tokens = {token for token in normalized_term.split() if token}
            normalized_term_tokens = {token for token in normalized_term.split() if token}
            overlap = len(term_tokens & content_tokens)
            normalized_overlap = len(normalized_term_tokens & normalized_content_tokens)
            morphology_overlap = len(morphology_term_tokens & morphology_content_tokens)
            if overlap >= max(1, len(term_tokens) // 2):
                return (True, f"Token-overlap {label} match: {term}")
            if normalized_overlap >= max(1, len(normalized_term_tokens) // 2):
                return (True, f"Normalized token-overlap {label} match: {term}")
            if morphology_overlap >= max(1, len(morphology_term_tokens) // 2):
                return (True, f"Morphology-aware {label} match: {term}")

        if self.client is None:
            return (False, f"No lexical {label} match and embeddings unavailable.")

        try:
            text_embedding = self._embedding_for_text(content)
            if not text_embedding:
                return (False, f"Could not compute embedding for {label} content.")

            best_term = ""
            best_score = -1.0
            for term in cleaned_terms:
                term_embedding = self._embedding_for_text(term)
                if not term_embedding:
                    continue
                score = self._cosine_similarity(text_embedding, term_embedding)
                if score > best_score:
                    best_score = score
                    best_term = term

            if best_score >= threshold:
                return (True, f"Semantic {label} match: {best_term} ({best_score:.3f})")
            return (False, f"Semantic {label} mismatch. Best={best_term} ({best_score:.3f})")
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("[filter-ai] semantic %s matching failed: %s", label, str(exc))
            return (False, f"Semantic {label} matching failed.")

    def _embedding_for_text(self, text: str) -> list[float]:
        normalized = text.strip().lower()
        if not normalized:
            return []
        cached = self._embedding_cache.get(normalized)
        if cached is not None:
            return cached
        if self.client is None:
            return []
        response = self.client.embeddings.create(
            model=self.embedding_model,
            input=normalized,
        )
        vector = response.data[0].embedding
        self._embedding_cache[normalized] = vector
        return vector

    @staticmethod
    def _cosine_similarity(vec_a: list[float], vec_b: list[float]) -> float:
        if not vec_a or not vec_b or len(vec_a) != len(vec_b):
            return -1.0
        dot = sum(a * b for a, b in zip(vec_a, vec_b))
        norm_a = math.sqrt(sum(a * a for a in vec_a))
        norm_b = math.sqrt(sum(b * b for b in vec_b))
        if norm_a == 0.0 or norm_b == 0.0:
            return -1.0
        return dot / (norm_a * norm_b)

    @staticmethod
    def _normalized_usd_bounds(salary: SalaryNormalization) -> tuple[float | None, float | None]:
        low = salary.min_usd if salary.min_usd is not None else salary.min_value
        high = salary.max_usd if salary.max_usd is not None else salary.max_value
        if low is not None and high is not None and low > high:
            low, high = high, low
        return (low, high)

    @staticmethod
    def _fallback_location(raw: str) -> LocationNormalization:
        pieces = [item.strip() for item in re.split(r"[,|/]", raw) if item.strip()]
        country = pieces[-1] if pieces else raw
        state = pieces[-2] if len(pieces) >= 2 else ""
        city = pieces[0] if len(pieces) >= 3 else ""
        canonical = ", ".join([piece for piece in [city, state, country] if piece]) or raw
        return LocationNormalization(
            raw_input=raw,
            country=country,
            state=state,
            city=city,
            canonical=canonical,
            confidence=0.35,
        )

    @staticmethod
    def _fallback_location_match(user_location: str, post_location: str) -> tuple[bool, str]:
        if not post_location.strip():
            return (False, "Post location is empty.")
        user_blob = user_location.lower()
        post_blob = post_location.lower()
        if user_blob in post_blob:
            return (True, "Substring location match.")
        user_tokens = {token for token in re.split(r"[\s,;/|]+", user_blob) if token}
        post_tokens = {token for token in re.split(r"[\s,;/|]+", post_blob) if token}
        if user_tokens and user_tokens.issubset(post_tokens):
            return (True, "Token-level location match.")
        overlap = len(user_tokens & post_tokens)
        return (overlap >= 2, "Fallback token overlap check.")

    @staticmethod
    def _fallback_salary(raw: str) -> SalaryNormalization:
        lower = raw.lower()
        normalized = lower.replace("k", "000")
        numbers = [float(item.replace(",", "")) for item in re.findall(r"\d[\d,]*", normalized)]
        min_value: float | None = numbers[0] if numbers else None
        max_value: float | None = numbers[1] if len(numbers) >= 2 else numbers[0] if numbers else None
        if any(token in lower for token in {"₽", "руб", "rur", "rub", "ruble"}):
            currency = "RUB"
        elif any(token in lower for token in {"$", "usd", "dollar"}):
            currency = "USD"
        else:
            currency = "UNKNOWN"
        if "hour" in lower or "/h" in lower or "hr" in lower:
            period = "hour"
        elif "час" in lower:
            period = "hour"
        elif "day" in lower:
            period = "day"
        elif "день" in lower or "дня" in lower or "дней" in lower:
            period = "day"
        elif "week" in lower:
            period = "week"
        elif "нед" in lower:
            period = "week"
        elif "month" in lower:
            period = "month"
        elif "месяц" in lower or "месяца" in lower or "месяцев" in lower:
            period = "month"
        elif "year" in lower or "annual" in lower:
            period = "year"
        elif "год" in lower or "лет" in lower:
            period = "year"
        elif "project" in lower or "fixed" in lower or "проект" in lower or "фикс" in lower:
            period = "project"
        else:
            period = "unknown"

        if min_value is None and max_value is None:
            canonical = "Not specified"
        elif min_value == max_value:
            canonical = f"{currency} {int(min_value)} ({period})"
        else:
            canonical = f"{currency} {int(min_value or 0)}-{int(max_value or 0)} ({period})"
        return SalaryNormalization(
            raw_input=raw,
            is_specified=bool(numbers),
            currency=currency,
            period=period,
            min_value=min_value,
            max_value=max_value,
            min_usd=min_value if currency in {"USD", "UNKNOWN"} else None,
            max_usd=max_value if currency in {"USD", "UNKNOWN"} else None,
            canonical_text=canonical,
            confidence=0.35,
        )


_FILTER_AI_UX_UI_TOPIC_MARKERS = (
    "ux",
    "ui",
    "user experience",
    "user interface",
    "product designer",
    "interaction designer",
    "visual designer",
    "web designer",
    "ux \u0434\u0438\u0437\u0430\u0439\u043d\u0435\u0440",
    "ui \u0434\u0438\u0437\u0430\u0439\u043d\u0435\u0440",
    "\u0434\u0438\u0437\u0430\u0439\u043d\u0435\u0440 ux",
    "\u0434\u0438\u0437\u0430\u0439\u043d\u0435\u0440 ui",
    "\u0434\u0438\u0437\u0430\u0439\u043d\u0435\u0440 ux ui",
    "ux ui \u0434\u0438\u0437\u0430\u0439\u043d\u0435\u0440",
    "\u043f\u0440\u043e\u0434\u0443\u043a\u0442\u043e\u0432\u044b\u0439 \u0434\u0438\u0437\u0430\u0439\u043d\u0435\u0440",
    "\u043f\u0440\u043e\u0434\u0443\u043a\u0442 \u0434\u0438\u0437\u0430\u0439\u043d\u0435\u0440",
    "\u0432\u0435\u0431 \u0434\u0438\u0437\u0430\u0439\u043d\u0435\u0440",
    "\u0432\u0435\u0431 \u0434\u0438\u0437\u0430\u0439\u043d",
    "\u0434\u0438\u0437\u0430\u0439\u043d\u0435\u0440 \u0438\u043d\u0442\u0435\u0440\u0444\u0435\u0439\u0441\u043e\u0432",
)
_FILTER_AI_UX_UI_ROLE_MARKERS = (
    "ux designer",
    "ui designer",
    "ux ui designer",
    "ui ux designer",
    "user experience designer",
    "user interface designer",
    "product designer",
    "interaction designer",
    "visual designer",
    "web designer",
    "ux \u0434\u0438\u0437\u0430\u0439\u043d\u0435\u0440",
    "ui \u0434\u0438\u0437\u0430\u0439\u043d\u0435\u0440",
    "\u0434\u0438\u0437\u0430\u0439\u043d\u0435\u0440 ux",
    "\u0434\u0438\u0437\u0430\u0439\u043d\u0435\u0440 ui",
    "ux ui \u0434\u0438\u0437\u0430\u0439\u043d\u0435\u0440",
    "\u0434\u0438\u0437\u0430\u0439\u043d\u0435\u0440 ux ui",
    "\u043f\u0440\u043e\u0434\u0443\u043a\u0442\u043e\u0432\u044b\u0439 \u0434\u0438\u0437\u0430\u0439\u043d\u0435\u0440",
    "\u043f\u0440\u043e\u0434\u0443\u043a\u0442 \u0434\u0438\u0437\u0430\u0439\u043d\u0435\u0440",
    "\u0434\u0438\u0437\u0430\u0439\u043d\u0435\u0440 \u0438\u043d\u0442\u0435\u0440\u0444\u0435\u0439\u0441\u043e\u0432",
    "\u0432\u0435\u0431 \u0434\u0438\u0437\u0430\u0439\u043d\u0435\u0440",
)
_FILTER_AI_UX_UI_WORK_MARKERS = (
    "wireframe",
    "wireframes",
    "prototype",
    "prototypes",
    "user flow",
    "user flows",
    "user research",
    "usability",
    "information architecture",
    "figma",
    "mockup",
    "mockups",
    "\u0432\u0430\u0439\u0440\u0444\u0440\u0435\u0439\u043c",
    "\u0432\u0430\u0439\u0440\u0444\u0440\u0435\u0439\u043c\u044b",
    "\u043f\u0440\u043e\u0442\u043e\u0442\u0438\u043f",
    "\u043f\u0440\u043e\u0442\u043e\u0442\u0438\u043f\u044b",
    "\u043f\u043e\u043b\u044c\u0437\u043e\u0432\u0430\u0442\u0435\u043b\u044c\u0441\u043a\u0438\u0439 \u0441\u0446\u0435\u043d\u0430\u0440\u0438\u0439",
    "\u043f\u043e\u043b\u044c\u0437\u043e\u0432\u0430\u0442\u0435\u043b\u044c\u0441\u043a\u0438\u0435 \u0441\u0446\u0435\u043d\u0430\u0440\u0438\u0438",
    "\u0438\u0441\u0441\u043b\u0435\u0434\u043e\u0432\u0430\u043d\u0438\u0435 \u043f\u043e\u043b\u044c\u0437\u043e\u0432\u0430\u0442\u0435\u043b\u0435\u0439",
    "\u044e\u0437\u0430\u0431\u0438\u043b\u0438\u0442\u0438",
    "\u0438\u043d\u0444\u043e\u0440\u043c\u0430\u0446\u0438\u043e\u043d\u043d\u0430\u044f \u0430\u0440\u0445\u0438\u0442\u0435\u043a\u0442\u0443\u0440\u0430",
    "\u043c\u0430\u043a\u0435\u0442",
    "\u043c\u0430\u043a\u0435\u0442\u044b",
    "\u0434\u0438\u0437\u0430\u0439\u043d \u0441\u0438\u0441\u0442\u0435\u043c\u0430",
    "\u0434\u0438\u0437\u0430\u0439\u043d \u0441\u0438\u0441\u0442\u0435\u043c\u044b",
)
_FILTER_AI_FRONTEND_ROLE_MARKERS = (
    "frontend developer",
    "front end developer",
    "frontend engineer",
    "front end engineer",
    "software engineer",
    "software developer",
    "web developer",
    "full stack developer",
    "fullstack developer",
    "full stack engineer",
    "fullstack engineer",
    "react developer",
    "javascript developer",
    "typescript developer",
    "ui engineer",
    "ui developer",
    "frontend \u0440\u0430\u0437\u0440\u0430\u0431\u043e\u0442\u0447\u0438\u043a",
    "front end \u0440\u0430\u0437\u0440\u0430\u0431\u043e\u0442\u0447\u0438\u043a",
    "\u0444\u0440\u043e\u043d\u0442\u0435\u043d\u0434 \u0440\u0430\u0437\u0440\u0430\u0431\u043e\u0442\u0447\u0438\u043a",
    "frontend \u0438\u043d\u0436\u0435\u043d\u0435\u0440",
    "\u0444\u0440\u043e\u043d\u0442\u0435\u043d\u0434 \u0438\u043d\u0436\u0435\u043d\u0435\u0440",
    "\u0440\u0430\u0437\u0440\u0430\u0431\u043e\u0442\u0447\u0438\u043a \u0438\u043d\u0442\u0435\u0440\u0444\u0435\u0439\u0441\u043e\u0432",
    "web \u0440\u0430\u0437\u0440\u0430\u0431\u043e\u0442\u0447\u0438\u043a",
    "\u0432\u0435\u0431 \u0440\u0430\u0437\u0440\u0430\u0431\u043e\u0442\u0447\u0438\u043a",
    "react \u0440\u0430\u0437\u0440\u0430\u0431\u043e\u0442\u0447\u0438\u043a",
    "javascript \u0440\u0430\u0437\u0440\u0430\u0431\u043e\u0442\u0447\u0438\u043a",
    "typescript \u0440\u0430\u0437\u0440\u0430\u0431\u043e\u0442\u0447\u0438\u043a",
)
_FILTER_AI_REMOTE_TERMS = {
    "remote",
    "wfh",
    "work from home",
    "anywhere",
    "worldwide",
    "distributed",
    "global remote",
    "\u0443\u0434\u0430\u043b\u0435\u043d\u043d\u043e",
    "\u0443\u0434\u0430\u043b\u0435\u043d\u043a\u0430",
    "\u0443\u0434\u0430\u043b\u0435\u043d\u043d\u0430\u044f",
    "\u0443\u0434\u0430\u043b\u0435\u043d\u043d\u044b\u0439",
    "\u0443\u0434\u0430\u043b\u0435\u043d\u043d\u0430\u044f \u0440\u0430\u0431\u043e\u0442\u0430",
    "\u0438\u0437 \u0434\u043e\u043c\u0430",
    "\u043d\u0430 \u0434\u043e\u043c\u0443",
    "\u0440\u0430\u0431\u043e\u0442\u0430 \u0438\u0437 \u0434\u043e\u043c\u0430",
}
_FILTER_AI_IGNORED_ROLE_TOKENS = {
    "senior",
    "junior",
    "lead",
    "principal",
    "staff",
    "intern",
    "remote",
    "global",
    "contract",
    "full",
    "time",
    "part",
    "\u0441\u0442\u0430\u0440\u0448\u0438\u0439",
    "\u043c\u043b\u0430\u0434\u0448\u0438\u0439",
    "\u0432\u0435\u0434\u0443\u0449\u0438\u0439",
    "\u0443\u0434\u0430\u043b\u0435\u043d\u043d\u043e",
    "\u0443\u0434\u0430\u043b\u0435\u043d\u043a\u0430",
    "\u043f\u043e\u043b\u043d\u0430\u044f",
    "\u0447\u0430\u0441\u0442\u0438\u0447\u043d\u0430\u044f",
}


def _filter_ai_fallback_keyword_variations(keyword: str, remote_tokens: set[str]) -> list[str]:
    candidates: list[str] = []
    normalized = keyword.strip().lower().replace("\u0451", "\u0435")
    if "/" in normalized:
        candidates.append(normalized.replace("/", " "))
    if "-" in normalized:
        candidates.append(normalized.replace("-", " "))
    if "&" in normalized:
        candidates.append(normalized.replace("&", "and"))
    compact = normalize_match_text(normalized)
    if compact and compact != normalized:
        candidates.append(compact)

    remote_variants = {normalize_match_text(token) for token in remote_tokens if token}
    remote_variants.update(normalize_match_text(token) for token in _FILTER_AI_REMOTE_TERMS)
    if any(token and token in compact for token in remote_variants):
        candidates.append("remote")
        candidates.extend(
            [
                "\u0443\u0434\u0430\u043b\u0435\u043d\u043d\u043e",
                "\u0443\u0434\u0430\u043b\u0435\u043d\u043a\u0430",
                "\u0440\u0430\u0431\u043e\u0442\u0430 \u0438\u0437 \u0434\u043e\u043c\u0430",
            ]
        )
    if "ux/ui" in normalized or "ux ui" in compact:
        candidates.extend(
            [
                "ux ui",
                "ui ux",
                "\u0434\u0438\u0437\u0430\u0439\u043d\u0435\u0440 ux ui",
                "ux ui \u0434\u0438\u0437\u0430\u0439\u043d\u0435\u0440",
            ]
        )
    if "web designer" in compact or "\u0432\u0435\u0431 \u0434\u0438\u0437\u0430\u0439\u043d\u0435\u0440" in compact:
        candidates.extend(["\u0432\u0435\u0431 \u0434\u0438\u0437\u0430\u0439\u043d\u0435\u0440", "web designer"])

    seen: set[str] = set()
    filtered: list[str] = []
    for value in candidates:
        cleaned = value.strip().lower()
        if not cleaned or cleaned == normalized or cleaned in seen:
            continue
        seen.add(cleaned)
        filtered.append(cleaned)
        if len(filtered) >= 10:
            break
    return filtered


def _filter_ai_build_keyword_interpretation(
    self,
    cleaned_keywords: list[str],
    items: list[KeywordExpansionItem],
    combined_keywords: list[str],
) -> KeywordInterpretation:
    remote_tokens = {normalize_match_text(token) for token in _FILTER_AI_REMOTE_TERMS}
    seen: set[str] = set()
    ordered_terms: list[str] = []
    for value in combined_keywords:
        raw_value = str(value).strip().lower()
        candidates = [raw_value, normalize_match_text(value), *tokenize_with_morphology(raw_value, min_length=2)]
        for candidate in candidates:
            cleaned_value = str(candidate).strip().lower()
            dedupe_key = normalize_match_text(cleaned_value) or cleaned_value
            stored_value = cleaned_value or dedupe_key
            if not dedupe_key or dedupe_key in seen or not stored_value:
                continue
            seen.add(dedupe_key)
            ordered_terms.append(stored_value)
    if any(any(token in normalize_match_text(term) for token in remote_tokens) for term in ordered_terms) and "remote" not in seen:
        ordered_terms.append("remote")
    ordered_terms = ordered_terms[:100]
    understood_keywords = []
    for item in items:
        understood = normalize_match_text(item.understood_as) or normalize_match_text(item.input_keyword)
        if understood and understood not in understood_keywords:
            understood_keywords.append(understood)
    return KeywordInterpretation(
        input_keywords=list(cleaned_keywords),
        understood_keywords=understood_keywords,
        expanded_keywords=ordered_terms,
        per_keyword=[
            KeywordExpansionItem(
                input_keyword=normalize_match_text(item.input_keyword),
                understood_as=normalize_match_text(item.understood_as) or normalize_match_text(item.input_keyword),
                similar_keywords=[
                    normalize_match_text(value)
                    for value in item.similar_keywords[:10]
                    if normalize_match_text(value)
                    and normalize_match_text(value) != normalize_match_text(item.input_keyword)
                ],
            )
            for item in items
        ],
    )


def _filter_ai_precheck_fallback_decision(
    all_prechecks_pass: bool,
    failed_prechecks: list[str],
    prefix: str,
    *,
    output_language: str = "en",
) -> tuple[bool, str]:
    if all_prechecks_pass:
        return (
            True,
            localized_text(
                output_language,
                en=f"{prefix}; all preliminary filter checks passed.",
                ru=f"{prefix}; \u0432\u0441\u0435 \u043f\u0440\u0435\u0434\u0432\u0430\u0440\u0438\u0442\u0435\u043b\u044c\u043d\u044b\u0435 \u043f\u0440\u043e\u0432\u0435\u0440\u043a\u0438 \u0444\u0438\u043b\u044c\u0442\u0440\u043e\u0432 \u043f\u0440\u043e\u0439\u0434\u0435\u043d\u044b.",
            ),
        )
    failed = ", ".join(failed_prechecks) or "unknown"
    return (
        False,
        localized_text(
            output_language,
            en=f"{prefix}; preliminary checks failed: {failed}.",
            ru=f"{prefix}; \u043f\u0440\u0435\u0434\u0432\u0430\u0440\u0438\u0442\u0435\u043b\u044c\u043d\u044b\u0435 \u043f\u0440\u043e\u0432\u0435\u0440\u043a\u0438 \u043d\u0435 \u043f\u0440\u043e\u0439\u0434\u0435\u043d\u044b: {failed}.",
        ),
    )


def _filter_ai_role_head_token(normalized_role: str) -> str:
    tokens = [token for token in normalized_role.split() if token and token not in _FILTER_AI_IGNORED_ROLE_TOKENS]
    return tokens[-1] if tokens else ""


def _filter_ai_topic_guard_match(
    cls,
    normalized_terms: list[str],
    normalized_content: str,
    label: str,
) -> tuple[bool, str] | None:
    if label not in {"role", "sphere"}:
        return None
    if not cls._looks_like_ux_ui_topic(normalized_terms):
        return None

    signals = cls._ux_ui_signal_summary(normalized_content)

    if bool(signals["pure_frontend_role"]):
        return (False, f"Rejected {label} match: front-end/developer role is not UX/UI and has no real UX/UI ownership.")
    if bool(signals["has_ux_ui_role"]):
        return (True, f"Heuristic {label} match: UX/UI role signal found.")
    if bool(signals["mixed_design_build_match"]):
        return (True, f"Heuristic {label} match: mixed design/build role shows real UX/UI ownership.")
    if bool(signals["adjacent_design_match"]):
        return (True, f"Heuristic {label} match: UX/UI design-work signal found.")
    if bool(signals["has_frontend_role"]):
        return (False, f"Rejected {label} match: engineering role lacks enough UX/UI ownership signals.")
    return None


def _filter_ai_fallback_role_alignment(
    cls,
    requested_role: str,
    post_title: str,
    post_description: str,
) -> tuple[bool, str]:
    requested_norm = cls._normalize_match_text(requested_role)
    title_norm = cls._normalize_match_text(post_title)
    description_norm = cls._normalize_match_text(post_description)
    combined_norm = " ".join(part for part in (title_norm, description_norm) if part).strip()
    if not requested_norm:
        return (True, "No role filter.")
    if not combined_norm:
        return (False, "Missing opportunity content for role matching.")

    topic_guard_success: tuple[bool, str] | None = None
    topic_guard = cls._topic_guard_match([requested_norm], combined_norm, "role")
    if topic_guard is not None:
        if not topic_guard[0]:
            return topic_guard
        topic_guard_success = topic_guard

    if requested_norm and requested_norm in title_norm:
        return (True, f"Title directly matches requested role: {requested_role}.")

    if cls._looks_like_ux_ui_topic([requested_norm]):
        signals = cls._ux_ui_signal_summary(combined_norm)
        title_has_frontend_role = any(marker in title_norm for marker in cls.FRONTEND_ROLE_MARKERS)
        title_has_ux_ui_role = any(marker in title_norm for marker in cls.UX_UI_ROLE_MARKERS)
        adjacent_with_real_design_work = bool(signals["adjacent_design_match"]) and int(signals["ux_ui_work_hits"]) >= 2
        if title_has_frontend_role and not title_has_ux_ui_role:
            if bool(signals["mixed_design_build_match"]) or adjacent_with_real_design_work:
                pass
            else:
                return (False, "Primary role looks engineering-first without enough UX/UI ownership signals.")
        if any(marker in title_norm for marker in cls.UX_UI_ROLE_MARKERS):
            return (True, "Title shows a UX/UI-adjacent design role.")
        if bool(signals["mixed_design_build_match"]):
            return (True, "Broad UX/UI request matched a mixed design and implementation role with real design ownership.")
        if bool(signals["adjacent_design_match"]):
            return (True, "Broad UX/UI request matched an adjacent design role with UX/UI deliverables.")
        if topic_guard_success is not None:
            return topic_guard_success
        if bool(signals["has_frontend_role"]):
            return (False, "Primary role looks engineering-first without enough UX/UI ownership signals.")
        return (False, f"Primary role does not look like {requested_role}.")

    project_context_markers = (
        "project",
        "freelance",
        "contract",
        "consult",
        "brief",
        "scope",
        "deliverable",
        "proposal",
        "client",
        "request",
        "retainer",
        "gig",
    )
    role_deliverable_profiles: tuple[tuple[tuple[str, ...], tuple[str, ...], str], ...] = (
        (
            ("frontend", "front end", "frontend developer", "react developer", "web developer", "javascript", "typescript"),
            ("landing page", "dashboard", "web app", "react", "next js", "nextjs", "responsive", "figma", "component", "ui"),
            "frontend delivery scope",
        ),
        (
            ("copywriter", "copywriting", "content writer", "seo writer", "email copy", "sales copy"),
            ("landing page copy", "website copy", "sales page", "email", "ad copy", "blog", "messaging", "headline", "copy polish"),
            "copywriting deliverables",
        ),
        (
            ("marketing", "marketer", "growth", "performance marketer", "digital marketer", "social media"),
            ("campaign", "lead gen", "lead generation", "ads", "seo", "social media", "content calendar", "funnel", "newsletter"),
            "marketing deliverables",
        ),
        (
            ("video editor", "video editing", "editor", "motion designer", "motion graphics"),
            ("video", "edit", "reels", "shorts", "youtube", "podcast", "motion", "caption", "color grade"),
            "video editing deliverables",
        ),
    )
    for role_markers, deliverable_markers, label in role_deliverable_profiles:
        if not any(marker in requested_norm for marker in role_markers):
            continue
        deliverable_hits = sum(1 for marker in deliverable_markers if marker in combined_norm)
        if deliverable_hits >= 2:
            return (True, f"Post deliverables align with the requested {label}.")
        if deliverable_hits >= 1 and any(marker in combined_norm for marker in project_context_markers):
            return (True, f"Project scope aligns with the requested {label}.")

    requested_tokens = {token for token in requested_norm.split() if token}
    title_tokens = {token for token in title_norm.split() if token}
    overlap = len(requested_tokens & title_tokens)
    head_token = cls._role_head_token(requested_norm)
    if head_token and head_token in title_tokens and overlap >= max(1, len(requested_tokens) - 1):
        return (True, f"Title aligns closely with requested role: {requested_role}.")
    if head_token and head_token in title_tokens and requested_norm in description_norm:
        return (True, f"Description confirms the requested role: {requested_role}.")
    return (False, f"Primary role appears different from {requested_role}.")


def _filter_ai_fallback_remote_global_match(
    cls,
    post_title: str,
    post_description: str,
    post_location: str,
) -> tuple[bool, str]:
    assessment = cls._fallback_work_arrangement_assessment(post_title, post_description, post_location)
    if assessment.is_hybrid:
        return (False, assessment.reason)
    if not assessment.is_remote:
        return (False, assessment.reason or "Post is not clearly remote.")
    if assessment.remote_scope == "global" or (
        not assessment.has_scope_restriction and assessment.remote_scope in {"unknown", "global"}
    ):
        return (True, assessment.reason or "Remote role is open from anywhere with no country restriction.")
    return (
        False,
        assessment.reason
        or _remote_scope_reason_for_code(
            assessment.restriction_reason_code or "country_restriction",
            scoped_value=", ".join(assessment.scope_locations[:3]),
        ),
    )


def _filter_ai_fallback_negative_feedback_analysis(user_feedback: str) -> dict[str, str]:
    raw_feedback = str(user_feedback or "").strip()
    lowered = normalize_match_text(raw_feedback)
    summary = raw_feedback[:240]
    result = {
        "summary": summary or "User marked this opportunity as not relevant.",
        "job_title_issue": "",
        "location_issue": "",
        "salary_issue": "",
        "other_issue": "",
    }
    if any(
        token in lowered
        for token in {
            "title",
            "role",
            "position",
            "frontend",
            "backend",
            "designer",
            "developer",
            "\u0434\u043e\u043b\u0436\u043d\u043e\u0441\u0442",
            "\u0440\u043e\u043b\u044c",
            "\u043f\u043e\u0437\u0438\u0446\u0438",
            "\u0444\u0440\u043e\u043d\u0442\u0435\u043d\u0434",
            "\u0434\u0438\u0437\u0430\u0439\u043d\u0435\u0440",
            "\u0440\u0430\u0437\u0440\u0430\u0431\u043e\u0442\u0447\u0438\u043a",
        }
    ):
        result["job_title_issue"] = raw_feedback
    if any(
        token in lowered
        for token in {
            "location",
            "country",
            "city",
            "remote",
            "onsite",
            "hybrid",
            "timezone",
            "\u043b\u043e\u043a\u0430\u0446",
            "\u0441\u0442\u0440\u0430\u043d\u0430",
            "\u0433\u043e\u0440\u043e\u0434",
            "\u0443\u0434\u0430\u043b\u0435\u043d",
            "\u043e\u0444\u0438\u0441",
            "\u0433\u0438\u0431\u0440\u0438\u0434",
            "\u0447\u0430\u0441\u043e\u0432",
        }
    ):
        result["location_issue"] = raw_feedback
    if any(
        token in lowered
        for token in {
            "salary",
            "pay",
            "compensation",
            "budget",
            "rate",
            "\u0437\u0430\u0440\u043f\u043b\u0430\u0442",
            "\u043e\u043f\u043b\u0430\u0442",
            "\u0434\u043e\u0445\u043e\u0434",
            "\u043e\u043a\u043b\u0430\u0434",
        }
    ):
        result["salary_issue"] = raw_feedback
    if not any(result[field] for field in ("job_title_issue", "location_issue", "salary_issue")):
        result["other_issue"] = raw_feedback or "No specific reason provided."
    return result


FilterAI.UX_UI_TOPIC_MARKERS = _FILTER_AI_UX_UI_TOPIC_MARKERS
FilterAI.UX_UI_ROLE_MARKERS = _FILTER_AI_UX_UI_ROLE_MARKERS
FilterAI.UX_UI_WORK_MARKERS = _FILTER_AI_UX_UI_WORK_MARKERS
FilterAI.FRONTEND_ROLE_MARKERS = _FILTER_AI_FRONTEND_ROLE_MARKERS
FilterAI._fallback_keyword_variations = staticmethod(_filter_ai_fallback_keyword_variations)
FilterAI._build_keyword_interpretation = _filter_ai_build_keyword_interpretation
FilterAI._precheck_fallback_decision = staticmethod(_filter_ai_precheck_fallback_decision)
FilterAI._role_head_token = staticmethod(_filter_ai_role_head_token)
FilterAI._topic_guard_match = classmethod(_filter_ai_topic_guard_match)
FilterAI._fallback_role_alignment = classmethod(_filter_ai_fallback_role_alignment)
FilterAI._fallback_remote_global_match = classmethod(_filter_ai_fallback_remote_global_match)
FilterAI._fallback_negative_feedback_analysis = staticmethod(_filter_ai_fallback_negative_feedback_analysis)
