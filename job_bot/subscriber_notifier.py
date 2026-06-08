from __future__ import annotations

import html
import json
import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

try:
    from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
except ImportError:
    from telegram._bot import Bot
    from telegram._inline.inlinekeyboardbutton import InlineKeyboardButton
from telegram._inline.inlinekeyboardmarkup import InlineKeyboardMarkup

from job_bot.filter_ai import FilterAI, WorkArrangementAssessment
from job_bot.language_utils import detect_language, localized_text, normalize_match_text, tokenize_with_morphology
from job_bot.location_product_rules import REMOTE_GLOBAL_RULE, REMOTE_WITHIN_COUNTRY_RULE
from job_bot.models import JobCard
from job_bot.opportunity_fields import OPPORTUNITY_NOTIFICATION_FIELD_ORDER, opportunity_card_value
from job_bot.telegram_localization import message as localized_message
from job_bot.telegram_localization import normalize_message_language
from job_bot.telegram_subscription_store import (
    SubscriptionStore,
    format_utc_iso,
    normalize_project_preferences,
    strip_www_prefix,
    utc_now_dt,
    utc_now_iso,
)
from job_bot.user_message_logger import UserMessageLogger


@dataclass(slots=True)
class DispatchOutcome:
    active_subscribers: int = 0
    filter_matched_subscribers: int = 0
    sent_count: int = 0
    queued_count: int = 0
    human_review_count: int = 0


@dataclass(slots=True)
class MatchEvaluation:
    matched: bool
    reason_summary: str = ""
    admin_reason_summary: str = ""
    needs_human_review: bool = False
    review_kind: str = ""
    review_summary: str = ""
    review_context: dict[str, Any] = field(default_factory=dict)
    decision_stage: str = ""
    decision_reason_code: str = ""


@dataclass(slots=True, frozen=True)
class LocationFilterSpec:
    label: str
    rule: str
    country: str = ""
    location: str = ""


class SubscriberNotifier:
    REMOTE_GLOBAL_PREFERENCE_LABEL = "Remote Global"
    REMOTE_WITHIN_COUNTRY_PREFIX = "remote within "
    ONSITE_HYBRID_WITHIN_COUNTRY_PREFIXES = (
        "on-site/hybrid within ",
        "onsite/hybrid within ",
        "on site/hybrid within ",
        "on-site ",
        "onsite ",
        "on site ",
    )
    FEEDBACK_UP_PREFIX = "match_up_"
    FEEDBACK_DOWN_PREFIX = "match_dn_"

    def __init__(
        self,
        bot_token: str,
        store: SubscriptionStore,
        logger: logging.Logger,
        user_message_logger: UserMessageLogger | None = None,
        openai_api_key: str = "",
        openai_model: str = "gpt-4o-mini",
        keyword_model: str = "",
        final_match_model: str = "",
        request_timeout_seconds: float = 45.0,
        retry_budget: int = 2,
        backoff_base_seconds: float = 1.5,
        delivery_job_retry_budget: int = 3,
        delivery_job_backoff_seconds: float = 20.0,
    ) -> None:
        self.bot = Bot(token=bot_token)
        self.store = store
        self.logger = logger
        self.user_message_logger = user_message_logger
        self.delivery_job_retry_budget = max(0, int(delivery_job_retry_budget))
        self.delivery_job_backoff_seconds = max(1.0, float(delivery_job_backoff_seconds))
        self.filter_ai = FilterAI(
            openai_api_key=openai_api_key,
            openai_model=openai_model,
            logger=logger,
            keyword_model=keyword_model,
            final_match_model=final_match_model,
            request_timeout_seconds=request_timeout_seconds,
            retry_budget=retry_budget,
            backoff_base_seconds=backoff_base_seconds,
        )

    @staticmethod
    def format_job_message(card: JobCard, why_matched: str = "", ui_language: str = "") -> str:
        language = normalize_message_language(ui_language or getattr(card, "language", "en") or "en")
        unknown = localized_message(language, "card_unknown")
        cleaned_values = {
            field_key: SubscriberNotifier._clean_message_text(opportunity_card_value(card, field_key), default=unknown)
            for field_key in OPPORTUNITY_NOTIFICATION_FIELD_ORDER
        }
        title = cleaned_values["title"]
        counterparty = cleaned_values["counterparty"]
        description = cleaned_values["scope_summary"]
        location = cleaned_values["opportunity_location"]
        payment_terms = cleaned_values["payment_terms"]
        engagement = cleaned_values["engagement_summary"]
        timeline = cleaned_values["timeline_summary"]
        skills = cleaned_values["skills_required"]
        source_label = SubscriberNotifier._source_label(card.website)
        localized_reason = SubscriberNotifier._clean_message_text(
            why_matched,
            default=localized_message(language, "card_default_match_reason"),
        )

        message_text = (
            f"{localized_message(language, 'card_new_project_match')}\n"
            f"{localized_message(language, 'card_project')}: {title}\n"
            f"{localized_message(language, 'card_client')}: {counterparty or unknown}\n"
            f"{localized_message(language, 'card_budget_rate')}: {payment_terms}\n"
            f"{localized_message(language, 'card_engagement_type')}: {engagement}\n"
            f"{localized_message(language, 'card_timeline_duration')}: {timeline}\n"
            f"{localized_message(language, 'card_skills_requested')}: {skills}\n"
            f"{localized_message(language, 'card_remote_location')}: {location}\n"
            f"{localized_message(language, 'card_source')}: {source_label}\n"
            f"{localized_message(language, 'card_scope_summary')}\n"
            f"{description}"
        )
        return (
            f"{message_text}\n\n"
            f"{localized_message(language, 'card_why_matched')}\n"
            f"{localized_reason}"
        )

    @staticmethod
    def _clean_message_text(value: str, default: str = "Not specified") -> str:
        text = html.unescape(str(value or ""))
        text = re.sub(r"<\s*br\s*/?\s*>", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"</\s*p\s*>", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"[ \t\r\f\v]+", " ", text)
        text = re.sub(r"\s+([,.;:!?])", r"\1", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip() or str(default or "Not specified")

    @staticmethod
    def _source_label(value: str) -> str:
        parsed = urlparse(str(value or "").strip())
        domain = parsed.netloc or str(value or "").strip()
        return strip_www_prefix(domain) or "Unknown"

    @staticmethod
    def _normalize_preference_list(value: object) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            items = [value]
        elif isinstance(value, (list, tuple, set)):
            items = [str(item) for item in value]
        else:
            items = [str(value)]
        normalized: list[str] = []
        seen: set[str] = set()
        for item in items:
            cleaned = str(item or "").strip()
            lowered = cleaned.lower()
            if not cleaned or lowered in seen:
                continue
            seen.add(lowered)
            normalized.append(cleaned)
        return normalized

    def _project_preferences_for_user(self, user_id: int) -> dict[str, object]:
        getter = getattr(self.store, "get_project_preferences", None)
        if not callable(getter):
            return normalize_project_preferences({})
        try:
            raw_preferences = getter(user_id) or {}
        except Exception as exc:  # noqa: BLE001
            self.logger.warning(
                "[subscriber-notifier] could not load project preferences user=%s: %s",
                user_id,
                str(exc),
            )
            raw_preferences = {}
        return normalize_project_preferences(raw_preferences)

    @staticmethod
    def _has_active_project_preferences(project_preferences: dict[str, object]) -> bool:
        for key, value in project_preferences.items():
            if isinstance(value, list) and value:
                return True
            if value is not None and value != "":
                return True
        return False

    @staticmethod
    def _project_filter_blob(*parts: str) -> str:
        return normalize_match_text(" ".join(str(part or "").strip() for part in parts if str(part or "").strip()))

    @classmethod
    def _expanded_preference_terms(cls, field_name: str, value: str) -> list[str]:
        del field_name
        normalized = cls._project_filter_blob(value)
        if not normalized:
            return []
        ordered_terms = [normalized]
        seen = {normalized}
        for token in tokenize_with_morphology(value, min_length=2):
            if token in seen:
                continue
            seen.add(token)
            ordered_terms.append(token)
        return ordered_terms

    @classmethod
    def _preference_terms_match(
        cls,
        preferences: object,
        text: str,
        *,
        field_name: str,
        label: str,
    ) -> tuple[bool, str]:
        cleaned_preferences = cls._normalize_preference_list(preferences)
        if not cleaned_preferences:
            return (True, f"No {label} filter.")
        blob = cls._project_filter_blob(text)
        blob_tokens = set(tokenize_with_morphology(text, min_length=2))
        if not blob:
            return (False, f"Opportunity does not show the requested {label} signals.")
        for preference in cleaned_preferences:
            expanded_terms = [term for term in cls._expanded_preference_terms(field_name, preference) if term]
            if any(term in blob for term in expanded_terms):
                return (True, f"Opportunity matches the requested {label}: {preference}.")
            preference_tokens = set(tokenize_with_morphology(preference, min_length=2))
            if preference_tokens and len(preference_tokens & blob_tokens) >= max(1, len(preference_tokens) // 2):
                return (True, f"Opportunity matches the requested {label}: {preference}.")
        return (False, f"Opportunity does not show the requested {label} preferences.")

    @classmethod
    def _payment_model_tags(cls, card: JobCard) -> set[str]:
        payment_text = str(card.payment_terms or card.salary or "").strip()
        if not payment_text or payment_text.lower() in {"unknown", "not specified", "n/a", "na", "none"}:
            return {"undisclosed"}
        blob = cls._project_filter_blob(
            payment_text,
            getattr(card, "engagement_type", ""),
            getattr(card, "duration", ""),
            getattr(card, "scope_summary", ""),
            getattr(card, "description", ""),
            getattr(card, "notes", ""),
            getattr(card, "title", ""),
        )
        tags: set[str] = set()
        if any(term in blob for term in ("fixed price", "fixed", "flat fee", "project fee", "per project", "фикс", "фиксирован")):
            tags.add("fixed")
        if any(term in blob for term in ("hourly", "per hour", "/hr", "hour rate", "почас", "в час")):
            tags.add("hourly")
        if any(term in blob for term in ("retainer", "ретейнер")):
            tags.add("retainer")
        if any(term in blob for term in ("milestone", "этап")):
            tags.add("milestone based")
        if any(term in blob for term in ("commission", "commission only", "revenue share", "rev share", "комис", "процент")):
            tags.add("commission based")
        if any(term in blob for term in ("negotiable", "tbd", "to be discussed", "doe", "договорная", "обсуждаемо")):
            tags.add("negotiable")
        if not tags:
            tags.add("disclosed")
        return tags

    @staticmethod
    def _payment_lower_bound_usd(payment_text: str) -> float | None:
        raw = str(payment_text or "").strip()
        if not raw or raw.lower() in {"unknown", "not specified", "n/a", "na", "none"}:
            return None
        lowered = raw.lower()
        other_currency_markers = ("eur", "gbp", "rub", "egp", "aed", "cad", "aud")
        if any(marker in lowered for marker in other_currency_markers) and "usd" not in lowered and "$" not in raw:
            return None
        matches = re.findall(r"\d+(?:,\d{3})*(?:\.\d+)?", raw)
        if not matches:
            return None
        try:
            values = [float(match.replace(",", "")) for match in matches]
        except ValueError:
            return None
        return min(values) if values else None

    @classmethod
    def _payment_preferences_match(
        cls,
        card: JobCard,
        project_preferences: dict[str, object],
    ) -> tuple[bool, str, str]:
        minimum_fixed = project_preferences.get("minimum_fixed_budget_usd")
        minimum_hourly = project_preferences.get("minimum_hourly_rate_usd")
        if minimum_fixed is None and minimum_hourly is None:
            return (True, "No budget/rate filter.", "")

        tags = cls._payment_model_tags(card)
        lower_bound = cls._payment_lower_bound_usd(card.payment_terms or card.salary)
        if minimum_fixed is not None and "fixed" in tags and lower_bound is not None and lower_bound < float(minimum_fixed):
            return (False, "Opportunity fixed budget is below the requested minimum.", "budget_minimum_mismatch")
        if minimum_hourly is not None and "hourly" in tags and lower_bound is not None and lower_bound < float(minimum_hourly):
            return (False, "Opportunity hourly rate is below the requested minimum.", "hourly_rate_mismatch")

        if minimum_fixed is not None and "fixed" in tags and lower_bound is None:
            return (True, "Fixed budget could not be normalized, so the AI should decide.", "")
        if minimum_hourly is not None and "hourly" in tags and lower_bound is None:
            return (True, "Hourly rate could not be normalized, so the AI should decide.", "")
        return (True, "Opportunity budget/rate fits the requested budget filters.", "")

    async def _match_specific_locations(self, requested_locations: list[str], post_location: str) -> tuple[bool, str]:
        if not requested_locations:
            return (True, "No specific location filter.")
        failure_reasons: list[str] = []
        for requested_location in requested_locations:
            matched, reason = await self.filter_ai.check_location_match(requested_location, post_location)
            if matched:
                if len(requested_locations) == 1:
                    return (True, reason)
                prefix = f"Matched one of your saved locations ({requested_location})."
                suffix = reason.strip()
                return (True, f"{prefix} {suffix}".strip() if suffix else prefix)
            if len(failure_reasons) < 3:
                failure_reasons.append(f"{requested_location}: {reason.strip() or 'no match'}")
        if len(requested_locations) == 1:
            return (False, failure_reasons[0] if failure_reasons else "Specific location did not match.")
        summary = f"No match for any of your saved locations ({'; '.join(requested_locations)})."
        if failure_reasons:
            summary = f"{summary} {'; '.join(failure_reasons)}"
        return (False, summary)

    @classmethod
    def _delivery_markup(cls, job_url: str, delivery_event_id: int | None, language: str = "en") -> InlineKeyboardMarkup:
        localized_rows = [
            [InlineKeyboardButton(localized_message(language, "open_project"), url=job_url)]
        ]
        if delivery_event_id is not None:
            localized_rows.append(
                [
                    InlineKeyboardButton(
                        localized_message(language, "relevant"),
                        callback_data=f"{cls.FEEDBACK_UP_PREFIX}{delivery_event_id}",
                    ),
                    InlineKeyboardButton(
                        localized_message(language, "not_relevant"),
                        callback_data=f"{cls.FEEDBACK_DOWN_PREFIX}{delivery_event_id}",
                    ),
                ]
            )
        return InlineKeyboardMarkup(localized_rows)

    async def dispatch_new_job(self, card: JobCard, *, allow_manual_review: bool = True) -> DispatchOutcome:        
        now_utc = utc_now_dt()
        subscribers = self.store.get_active_subscribers()
        outcome = DispatchOutcome()
        if float(getattr(card, "confidence", 0.0) or 0.0) <= 0.0:
            self.logger.info(
                "[subscriber-notifier] suppressed delivery for zero-confidence post=%s",
                card.url,
            )
            return outcome
        for user_id, username in subscribers:
            if not self.store.is_user_subscribed(user_id):
                self.logger.info(
                    "[subscriber-notifier] skip dispatch for inactive subscription user=%s",
                    user_id,
                )
                continue
            if not self._has_alert_configuration(user_id):
                self.logger.info(
                    "[subscriber-notifier] skip dispatch for missing alert user=%s",
                    user_id,
                )
                continue

            outcome.active_subscribers += 1
            evaluation = await self._evaluate_user_filters(user_id, card)
            if evaluation.needs_human_review and allow_manual_review:
                if self._queue_manual_review_case(
                    user_id=user_id,
                    username=username,
                    card=card,
                    evaluation=evaluation,
                ):
                    outcome.human_review_count += 1
                    self._log_internal_event(
                        user_id=user_id,
                        username=username,
                        event_type="job_flash_human_review_queued",
                        text=f"Queued for human review: {card.url} | reason={evaluation.review_summary}",
                    )
                    continue
                self.logger.warning(
                    "[subscriber-notifier] manual review queue failed, falling back to normal delivery user=%s post=%s",
                    user_id,
                    card.url,
                )
            if not evaluation.matched:
                continue
            outcome.filter_matched_subscribers += 1

            ui_language = self._ui_language_for_user(user_id, getattr(card, "language", "en"))
            message = self.format_job_message(card, why_matched=evaluation.reason_summary, ui_language=ui_language)
            delivery_event_id = self._record_delivery_event(
                user_id=user_id,
                username=username,
                card=card,
                match_reason=evaluation.reason_summary,
            )
            delivery_mode = self._delivery_mode(user_id)

            if delivery_mode == "digest":
                self._queue_notification(
                    user_id=user_id,
                    username=username,
                    card_url=card.url,
                    message_text=message,
                    available_after_utc=self._next_digest_delivery_time(user_id, now_utc),
                    delivery_event_id=delivery_event_id,
                )
                self._log_internal_event(
                    user_id=user_id,
                    username=username,
                    event_type="job_flash_queued",
                    text=f"Queued for digest delivery: {card.url}",
                )
                outcome.queued_count += 1
                continue

            if not self._should_deliver_now(user_id, now_utc):
                self._queue_notification(
                    user_id=user_id,
                    username=username,
                    card_url=card.url,
                    message_text=message,
                    available_after_utc=self._next_allowed_delivery_time(user_id, now_utc),
                    delivery_event_id=delivery_event_id,
                )
                self._log_internal_event(
                    user_id=user_id,
                    username=username,
                    event_type="job_flash_queued",
                    text=f"Queued until delivery window opens: {card.url}",
                )
                outcome.queued_count += 1
                continue

            try:
                if not self.store.is_user_subscribed(user_id):
                    self.logger.info(
                        "[subscriber-notifier] subscription ended before send user=%s post=%s",
                        user_id,
                        card.url,
                    )
                    continue
                telegram_message = await self.bot.send_message(
                    chat_id=user_id,
                    text=message,
                    disable_web_page_preview=False,
                    reply_markup=self._delivery_markup(card.proposal_or_contact_url, delivery_event_id, ui_language),
                )
                self._mark_delivery_event_sent(
                    delivery_event_id=delivery_event_id,
                    chat_id=user_id,
                    message=telegram_message,
                )
                outcome.sent_count += 1
                self._log_outbound(user_id=user_id, username=username, text=message, event_type="job_flash_sent")
            except Exception as exc:  # noqa: BLE001
                if self.store.is_user_subscribed(user_id):
                    self._queue_notification(
                        user_id=user_id,
                        username=username,
                        card_url=card.url,
                        message_text=message,
                        available_after_utc=self._next_allowed_delivery_time(user_id, now_utc),
                        delivery_event_id=delivery_event_id,
                    )
                    self._log_internal_event(
                        user_id=user_id,
                        username=username,
                        event_type="job_flash_send_failed",
                        text=f"Immediate delivery failed and was queued: {card.url} | error={str(exc)}",
                    )
                    outcome.queued_count += 1
                else:
                    self.logger.info(
                        "[subscriber-notifier] not queueing post for inactive user=%s post=%s",
                        user_id,
                        card.url,
                    )
                self.logger.warning("[subscriber-notifier] immediate send failed for %s: %s", user_id, str(exc))
        return outcome

    def queue_job_for_delivery(self, card: JobCard) -> int | None:
        queue_method = getattr(self.store, "queue_delivery_job", None)
        if not callable(queue_method):
            return None
        payload = {
            "website": card.website,
            "url": card.url,
            "title": card.title,
            "description": card.description,
            "salary": card.payment_terms or card.salary,
            "location": card.location,
            "is_job_post": card.is_job_post,
            "is_relevant_opportunity": getattr(card, "is_relevant_opportunity", card.is_job_post),
            "confidence": card.confidence,
            "extraction_method": card.extraction_method,
            "extracted_at_utc": card.extracted_at_utc,
            "posted_at_utc": card.posted_at_utc,
            "notes": card.notes,
            "company": getattr(card, "company", ""),
            "language": getattr(card, "language", "en"),
            "opportunity_kind": getattr(card, "opportunity_kind", ""),
            "client": getattr(card, "client", ""),
            "requester": getattr(card, "requester", ""),
            "scope_summary": getattr(card, "scope_summary", ""),
            "budget": getattr(card, "budget", ""),
            "duration": getattr(card, "duration", ""),
            "commitment_level": getattr(card, "commitment_level", ""),
            "skills_required": getattr(card, "skills_required", ""),
            "proposal_deadline": getattr(card, "proposal_deadline", ""),
            "start_timeline": getattr(card, "start_timeline", ""),
            "industry": getattr(card, "industry", ""),
            "engagement_type": getattr(card, "engagement_type", ""),
            "remote_location_constraint": getattr(card, "remote_location_constraint", ""),
            "contact_url": getattr(card, "contact_url", ""),
        }
        try:
            queue_id = int(
                queue_method(
                    website=card.website,
                    card_url=card.url,
                    card_json=json.dumps(payload, ensure_ascii=False),
                )
            )
            self.logger.info("[subscriber-notifier] queued delivery job queue_id=%s url=%s", queue_id, card.url)
            return queue_id
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("[subscriber-notifier] could not queue delivery job %s: %s", card.url, str(exc))
            return None

    async def process_one_delivery_job(self) -> bool:
        claim_method = getattr(self.store, "claim_due_delivery_jobs", None)
        if not callable(claim_method):
            return False
        jobs = claim_method(limit=1)
        if not jobs:
            return False
        job = jobs[0]
        try:
            payload = json.loads(str(job.card_json or "{}"))
            card = JobCard(
                website=str(payload.get("website") or job.website or ""),
                url=str(payload.get("url") or job.card_url or ""),
                title=str(payload.get("title") or "Unknown"),
                description=str(payload.get("description") or "Unknown"),
                salary=str(payload.get("salary") or "Unknown"),
                location=str(payload.get("location") or "Unknown"),
                is_job_post=bool(payload.get("is_job_post", True)),
                confidence=float(payload.get("confidence") or 0.0),
                extraction_method=str(payload.get("extraction_method") or "queued_delivery"),
                extracted_at_utc=str(payload.get("extracted_at_utc") or utc_now_iso()),
                posted_at_utc=str(payload.get("posted_at_utc") or ""),
                notes=str(payload.get("notes") or ""),
                company=str(payload.get("company") or ""),
                language=str(payload.get("language") or "en"),
                is_relevant_opportunity=bool(payload.get("is_relevant_opportunity", payload.get("is_job_post", True))),
                opportunity_kind=str(payload.get("opportunity_kind") or ""),
                client=str(payload.get("client") or ""),
                requester=str(payload.get("requester") or ""),
                scope_summary=str(payload.get("scope_summary") or ""),
                budget=str(payload.get("budget") or ""),
                duration=str(payload.get("duration") or ""),
                commitment_level=str(payload.get("commitment_level") or ""),
                skills_required=str(payload.get("skills_required") or ""),
                proposal_deadline=str(payload.get("proposal_deadline") or ""),
                start_timeline=str(payload.get("start_timeline") or ""),
                industry=str(payload.get("industry") or ""),
                engagement_type=str(payload.get("engagement_type") or ""),
                remote_location_constraint=str(payload.get("remote_location_constraint") or ""),
                contact_url=str(payload.get("contact_url") or payload.get("url") or job.card_url or ""),
            )
        except Exception as exc:  # noqa: BLE001
            fail_method = getattr(self.store, "fail_delivery_job", None)
            if callable(fail_method):
                fail_method(job.queue_id, last_error=f"invalid_card_json:{exc}")
            self.logger.warning("[subscriber-notifier] invalid delivery job payload queue_id=%s: %s", job.queue_id, str(exc))
            return True

        try:
            await self.dispatch_new_job(card)
        except Exception as exc:  # noqa: BLE001
            attempt_number = int(getattr(job, "attempt_count", 0)) + 1
            if attempt_number > self.delivery_job_retry_budget:
                fail_method = getattr(self.store, "fail_delivery_job", None)
                if callable(fail_method):
                    fail_method(job.queue_id, last_error=str(exc))
                self.logger.warning(
                    "[subscriber-notifier] delivery job permanently failed queue_id=%s url=%s: %s",
                    job.queue_id,
                    card.url,
                    str(exc),
                )
                return True
            retry_delay_seconds = self.delivery_job_backoff_seconds * max(1, 2 ** (attempt_number - 1))
            next_attempt_utc = format_utc_iso(utc_now_dt() + timedelta(seconds=retry_delay_seconds))
            reschedule_method = getattr(self.store, "reschedule_delivery_job", None)
            if callable(reschedule_method):
                reschedule_method(job.queue_id, available_after_utc=next_attempt_utc, last_error=str(exc))
            self.logger.warning(
                "[subscriber-notifier] delivery job retry queue_id=%s attempt=%s/%s next=%s error=%s",
                job.queue_id,
                attempt_number,
                self.delivery_job_retry_budget,
                next_attempt_utc,
                str(exc),
            )
            return True

        complete_method = getattr(self.store, "mark_delivery_job_completed", None)
        if callable(complete_method):
            complete_method(job.queue_id)
        self.logger.info("[subscriber-notifier] delivery job completed queue_id=%s url=%s", job.queue_id, card.url)
        return True

    async def flush_due_notifications(self, limit: int = 100) -> None:
        due_items = self.store.get_due_notifications(limit=limit)
        if not due_items:
            return

        grouped_items: dict[int, list[Any]] = defaultdict(list)
        for item in due_items:
            grouped_items[item.user_id].append(item)

        for user_id, items in grouped_items.items():
            if not self.store.is_user_subscribed(user_id):
                for item in items:
                    self.store.cancel_notification(item.queue_id, reason="subscription_inactive")
                continue
            if not self._has_alert_configuration(user_id):
                for item in items:
                    self.store.cancel_notification(item.queue_id, reason="alert_not_configured")
                continue

            deliverable_items: list[Any] = []
            for item in items:
                if not self._queued_item_matches_current_website_selection(item):
                    self.store.cancel_notification(item.queue_id, reason="website_filter_changed")
                    continue
                deliverable_items.append(item)
            if not deliverable_items:
                continue

            now_utc = utc_now_dt()
            if not self._should_deliver_now(user_id, now_utc):
                retry_time = self._next_allowed_delivery_time(user_id, now_utc)
                for item in deliverable_items:
                    self.store.reschedule_notification(
                        item.queue_id,
                        available_after_utc=retry_time,
                        last_error="delivery_deferred_quiet_hours",
                    )
                continue

            delivery_mode = self._delivery_mode(user_id)
            if delivery_mode == "digest":
                try:
                    await self.bot.send_message(
                        chat_id=user_id,
                        text=localized_message(
                            self._ui_language_for_user(user_id),
                            "digest_header",
                            count=len(deliverable_items),
                        ),
                        disable_web_page_preview=True,
                    )
                except Exception as exc:  # noqa: BLE001
                    retry_time = self._next_digest_delivery_time(user_id, now_utc)
                    for item in deliverable_items:
                        self.store.reschedule_notification(
                            item.queue_id,
                            available_after_utc=retry_time,
                            last_error=str(exc),
                        )
                    self.logger.warning(
                        "[subscriber-notifier] digest header failed user=%s count=%s: %s",
                        user_id,
                        len(deliverable_items),
                        str(exc),
                    )
                    continue

            for item in deliverable_items:
                try:
                    telegram_message = await self.bot.send_message(
                        chat_id=item.user_id,
                        text=item.message_text,
                        disable_web_page_preview=False,
                        reply_markup=self._delivery_markup(
                            item.card_url,
                            item.delivery_event_id,
                            self._ui_language_for_user(item.user_id, detect_language(text=item.message_text)),
                        ),
                    )
                    self._mark_delivery_event_sent(
                        delivery_event_id=item.delivery_event_id,
                        chat_id=item.user_id,
                        message=telegram_message,
                    )
                    self.store.mark_notification_sent(item.queue_id)
                    self._log_outbound(
                        user_id=item.user_id,
                        username=item.username,
                        text=item.message_text,
                        event_type="job_flash_queued_sent",
                    )
                except Exception as exc:  # noqa: BLE001
                    retry_time = (
                        self._next_digest_delivery_time(item.user_id, utc_now_dt())
                        if delivery_mode == "digest"
                        else self._next_allowed_delivery_time(item.user_id, utc_now_dt())
                    )
                    self.store.reschedule_notification(
                        item.queue_id,
                        available_after_utc=retry_time,
                        last_error=str(exc),
                    )
                    self.logger.warning(
                        "[subscriber-notifier] queued send failed queue_id=%s user=%s: %s",
                        item.queue_id,
                        item.user_id,
                        str(exc),
                    )

    async def _card_matches_user_filters(self, user_id: int, card: JobCard) -> bool:
        return (await self._evaluate_user_filters(user_id, card)).matched

    async def _evaluate_user_filters(self, user_id: int, card: JobCard) -> MatchEvaluation:
        if not self.store.is_user_subscribed(user_id):
            return MatchEvaluation(matched=False)
        if not self._has_alert_configuration(user_id):
            return MatchEvaluation(matched=False)
        if not self.store.get_notification_preference(user_id):
            return MatchEvaluation(matched=False)
        ui_language = self._ui_language_for_user(user_id, getattr(card, "language", "en"))

        match_blob = " ".join(
            part
            for part in (
                card.title,
                card.scope_summary or card.description,
                card.opportunity_location or card.location,
                card.payment_terms or card.salary,
                card.counterparty,
                getattr(card, "skills_required", ""),
                getattr(card, "engagement_type", ""),
                getattr(card, "duration", ""),
                getattr(card, "start_timeline", ""),
                getattr(card, "industry", ""),
            )
            if part
        ).lower()
        work_mode = await self._assess_work_mode(card)
        role_title = self.store.get_role_preference(user_id).strip()
        spheres = [] if role_title else self.store.get_user_spheres(user_id)
        location_pref = self.store.get_location_preference(user_id).strip()
        location_filters = self._location_filter_specs(location_pref)
        include_no_salary = self.store.get_include_no_salary(user_id)
        salary_pref = ""
        structured_salary_pref: dict[str, object] = {}
        get_salary_pref = getattr(self.store, "get_salary_range_preference", None)
        if callable(get_salary_pref):
            salary_pref = str(get_salary_pref(user_id) or "").strip()
        get_structured_salary_pref = getattr(self.store, "get_salary_range_structured", None)
        if callable(get_structured_salary_pref):
            raw_structured = get_structured_salary_pref(user_id) or {}
            if isinstance(raw_structured, dict):
                structured_salary_pref = dict(raw_structured)
        keywords = self.store.get_user_keywords(user_id)
        project_preferences = self._project_preferences_for_user(user_id)
        project_scope_blob = " ".join(
            part
            for part in (
                card.title,
                getattr(card, "scope_summary", ""),
                card.description,
                getattr(card, "skills_required", ""),
                getattr(card, "engagement_type", ""),
                getattr(card, "duration", ""),
                getattr(card, "start_timeline", ""),
                getattr(card, "industry", ""),
                getattr(card, "notes", ""),
                card.counterparty,
            )
            if part
        )

        classification_blob = " ".join(
            part
            for part in (
                card.title,
                card.scope_summary or card.description,
                card.opportunity_location or card.location,
                card.payment_terms or card.salary,
                card.counterparty,
                getattr(card, "engagement_type", ""),
                getattr(card, "duration", ""),
                getattr(card, "start_timeline", ""),
                getattr(card, "industry", ""),
                getattr(card, "notes", ""),
                work_mode.reason,
            )
            if part
        ).lower()
        is_remote = work_mode.is_remote
        is_hybrid = work_mode.is_hybrid
        location_context = " ".join(
            part
            for part in (
                card.opportunity_location or card.location,
                card.scope_summary or card.description,
                card.title,
                card.counterparty,
                getattr(card, "notes", ""),
                work_mode.reason,
                ", ".join(work_mode.scope_locations),
            )
            if part
        ).strip()

        if not self.store.user_selected_website(user_id, card.website):
            source_reason = "Post source was not selected in the user's website preferences."
            self.logger.info(
                "[subscriber-notifier] source reject user=%s post=%s reason=%s",
                user_id,
                card.url,
                source_reason,
            )
            self._record_filter_mismatch_event(
                user_id=user_id,
                card=card,
                stage="source",
                reason=source_reason,
                reason_code="source_mismatch",
            )
            return self._rejected_match_evaluation(
                card=card,
                reason_summary=source_reason,
                decision_stage="source",
                decision_reason_code="source_mismatch",
                precheck_results={"source": {"matched": False, "reason": source_reason, "reason_code": "source_mismatch"}},
                role_title=role_title,
                location_pref=location_pref,
                salary_pref=salary_pref,
                structured_salary_pref=structured_salary_pref,
                include_no_salary=include_no_salary,
                keywords=keywords,
                project_preferences=project_preferences,
                location_filters=location_filters,
                location_context=location_context,
                work_mode=work_mode,
            )

        card_type = self._infer_card_type(classification_blob)
        role_match = True
        role_reason = "No role filter."
        if role_title:
            role_alignment_checker = getattr(self.filter_ai, "assess_role_alignment", None)
            if callable(role_alignment_checker):
                role_match, role_reason = await role_alignment_checker(
                    requested_role=role_title,
                    post_title=card.title,
                    post_description=card.description,
                    post_location=card.location,
                )
            else:
                role_match, role_reason = await self.filter_ai.semantic_match_any(
                    [role_title],
                    card.title,
                    threshold=0.62,
                    label="role",
                )

        sphere_match = True
        sphere_reason = "No sphere filter."
        if spheres:
            sphere_match, sphere_reason = await self.filter_ai.semantic_match_any(
                spheres,
                match_blob,
                threshold=0.54,
                label="sphere",
            )

        specific_locations = [item.location for item in location_filters if item.rule == "specific_location" and item.location]
        remote_country = (
            location_filters[0].country
            if len(location_filters) == 1 and location_filters[0].rule == "remote_within_country"
            else ""
        )
        onsite_hybrid_country = (
            location_filters[0].country
            if len(location_filters) == 1 and location_filters[0].rule == "onsite_hybrid_within_country"
            else ""
        )
        location_match, location_reason, location_reason_code = await self._evaluate_location_filters(
            location_filters=location_filters,
            card=card,
            location_context=location_context,
            work_mode=work_mode,
        )
        serialized_location_filters = self._serialize_location_filters(location_filters)
        location_rule = (
            "none"
            if not location_filters
            else location_filters[0].rule
            if len(location_filters) == 1
            else "any_of_location_filters"
        )
        requires_remote = any(item.rule in {"remote_global_only", "remote_within_country"} for item in location_filters)
        requires_global_remote = any(item.rule == "remote_global_only" for item in location_filters)
        requires_onsite_or_hybrid = any(item.rule == "onsite_hybrid_within_country" for item in location_filters)

        structured_has_bounds = any(
            structured_salary_pref.get(key) is not None for key in ("min_usd", "max_usd")
        )
        if structured_has_bounds and hasattr(self.filter_ai, "salary_matches_structured"):
            salary_match, salary_reason = await self.filter_ai.salary_matches_structured(
                structured_salary_pref,
                card.payment_terms or card.salary,
                include_no_salary,
            )
        elif salary_pref:
            salary_match, salary_reason = await self.filter_ai.salary_matches(
                salary_pref,
                card.payment_terms or card.salary,
                include_no_salary,
            )
        else:
            salary_text = (card.payment_terms or card.salary).strip().lower()
            salary_specified = bool(salary_text) and salary_text not in {"unknown", "not specified", "n/a", "na", "none"}
            salary_match = salary_specified or include_no_salary
            salary_reason = (
                "Opportunity shows a visible budget or rate."
                if salary_specified
                else "Opportunity has no visible budget or rate, but optional-budget mode is enabled."
                if include_no_salary
                else "Opportunity has no visible budget or rate and optional-budget mode is disabled."
            )
        salary_reason_code = "" if salary_match else "salary_policy"

        keyword_blob = f"{match_blob} remote" if is_remote else match_blob
        keyword_match = True
        keyword_reason = "No keyword filter."
        if keywords:
            keyword_match, keyword_reason = await self.filter_ai.semantic_match_any(
                keywords,
                keyword_blob,
                threshold=0.58,
                label="keyword",
            )

        deliverable_match, deliverable_reason = self._preference_terms_match(
            project_preferences.get("deliverables"),
            project_scope_blob,
            field_name="deliverables",
            label="deliverable",
        )
        payment_model_match, payment_model_reason, payment_model_reason_code = self._payment_preferences_match(
            card,
            project_preferences,
        )

        precheck_results: dict[str, dict[str, Any]] = {
            "role": {"matched": role_match, "reason": role_reason, "reason_code": "" if role_match else "role_mismatch"},
            "spheres": {"matched": sphere_match, "reason": sphere_reason, "reason_code": "" if sphere_match else "sphere_mismatch"},
            "location": {"matched": location_match, "reason": location_reason, "reason_code": location_reason_code},
            "salary_visibility": {"matched": salary_match, "reason": salary_reason, "reason_code": salary_reason_code},
            "keywords": {"matched": keyword_match, "reason": keyword_reason, "reason_code": "" if keyword_match else "keyword_mismatch"},
            "deliverables": {
                "matched": deliverable_match,
                "reason": deliverable_reason,
                "reason_code": "" if deliverable_match else "deliverable_mismatch",
            },
            "payment_model": {
                "matched": payment_model_match,
                "reason": payment_model_reason,
                "reason_code": payment_model_reason_code,
            },
        }
        terminal_failures = [
            (stage_name, details)
            for stage_name, details in precheck_results.items()
            if isinstance(details, dict)
            and not bool(details.get("matched", False))
            and self._is_terminal_precheck_failure(stage_name, str(details.get("reason_code") or ""))
        ]
        if terminal_failures:
            primary_stage, primary_payload = terminal_failures[0]
            primary_reason = str(primary_payload.get("reason") or "Filter mismatch").strip() or "Filter mismatch"
            primary_reason_code = str(primary_payload.get("reason_code") or "").strip()
            self.logger.info(
                "[subscriber-notifier] terminal precheck reject user=%s post=%s stage=%s reason_code=%s reason=%s",
                user_id,
                card.url,
                primary_stage,
                primary_reason_code or "none",
                primary_reason,
            )
            self._record_filter_mismatch_event(
                user_id=user_id,
                card=card,
                stage=primary_stage,
                reason=primary_reason,
                reason_code=primary_reason_code,
            )
            return self._rejected_match_evaluation(
                card=card,
                reason_summary=primary_reason,
                decision_stage=primary_stage,
                decision_reason_code=primary_reason_code,
                precheck_results=precheck_results,
                role_title=role_title,
                location_pref=location_pref,
                salary_pref=salary_pref,
                structured_salary_pref=structured_salary_pref,
                include_no_salary=include_no_salary,
                keywords=keywords,
                project_preferences=project_preferences,
                location_filters=location_filters,
                location_context=location_context,
                work_mode=work_mode,
            )

        all_prechecks_pass = all(bool(item["matched"]) for item in precheck_results.values())
        has_active_filters = bool(
            role_title
            or spheres
            or location_pref
            or keywords
            or salary_pref
            or structured_has_bounds
            or not include_no_salary
            or self._has_active_project_preferences(project_preferences)
        )
        if not has_active_filters:
            return MatchEvaluation(
                matched=all_prechecks_pass,
                reason_summary=localized_text(
                    ui_language,
                    en="Matched your saved alert settings.",
                    ru="Проект подходит под ваши фильтры поиска.",
                ),
                admin_reason_summary="Matched the saved alert settings.",
            )

        feedback_guidance = ""
        feedback_builder = getattr(self.store, "build_match_feedback_guidance", None)
        if callable(feedback_builder):
            try:
                feedback_guidance = str(feedback_builder(user_id, limit=8) or "").strip()
            except Exception as exc:  # noqa: BLE001
                self.logger.warning(
                    "[subscriber-notifier] could not load match feedback guidance user=%s: %s",
                    user_id,
                    str(exc),
                )

        ai_match, ai_reason = await self.filter_ai.confirm_post_matches_filters(
            post_x={
                "url": card.url,
                "website": card.website,
                "title": card.title,
                "description": card.description,
                "scope_summary": getattr(card, "scope_summary", ""),
                "location": card.location,
                "location_context": location_context,
                "salary": card.payment_terms or card.salary,
                "budget_or_rate": card.payment_terms or card.salary,
                "engagement_type": getattr(card, "engagement_type", ""),
                "duration": getattr(card, "duration", ""),
                "start_timeline": getattr(card, "start_timeline", ""),
                "skills_requested": getattr(card, "skills_required", ""),
                "opportunity_kind": getattr(card, "opportunity_kind", ""),
                "is_remote": is_remote,
                "is_hybrid": is_hybrid,
                "is_onsite": work_mode.is_onsite,
                "remote_scope": work_mode.remote_scope,
                "scope_locations": list(work_mode.scope_locations),
                "work_mode_confidence": work_mode.confidence,
                "work_mode_reason": work_mode.reason,
                "has_scope_restriction": work_mode.has_scope_restriction,
                "card_type_guess": card_type,
                "language": getattr(card, "language", "en"),
            },
            user_filters={
                "content_mode": "freelance_opportunities",
                "role_title": role_title,
                "spheres": spheres,
                "location_preference": location_pref,
                "location_preferences": specific_locations,
                "location_filters": serialized_location_filters,
                "remote_country": remote_country,
                "onsite_hybrid_country": onsite_hybrid_country,
                "location_rule": location_rule,
                "requires_remote": requires_remote,
                "requires_global_remote": requires_global_remote,
                "requires_onsite_or_hybrid": requires_onsite_or_hybrid,
                "salary_range_preference": salary_pref,
                "budget_rate_preference": salary_pref,
                "include_no_salary": include_no_salary,
                "keywords": keywords,
                "project_preferences": project_preferences,
                "deliverables": project_preferences.get("deliverables", []),
                "minimum_fixed_budget_usd": project_preferences.get("minimum_fixed_budget_usd"),
                "minimum_hourly_rate_usd": project_preferences.get("minimum_hourly_rate_usd"),
                "feedback_guidance": feedback_guidance,
                "output_language": ui_language,
            },
            precheck_results=precheck_results,
        )
        if not ai_match:
            failed_prechecks = [
                (stage_name, details)
                for stage_name, details in precheck_results.items()
                if isinstance(details, dict) and not bool(details.get("matched", False))
            ]
            primary_stage = "final_ai"
            primary_reason = ai_reason
            primary_reason_code = ""
            if failed_prechecks:
                primary_stage, primary_payload = failed_prechecks[0]
                primary_reason = str(primary_payload.get("reason") or ai_reason or "").strip() or ai_reason
                primary_reason_code = str(primary_payload.get("reason_code") or "").strip()
            self.logger.info(
                "[subscriber-notifier] filter reject user=%s post=%s reason=%s",
                user_id,
                card.url,
                primary_reason or ai_reason,
            )
            self._record_filter_mismatch_event(
                user_id=user_id,
                card=card,
                stage=primary_stage,
                reason=primary_reason or ai_reason,
                reason_code=primary_reason_code,
            )
            return self._rejected_match_evaluation(
                card=card,
                reason_summary=primary_reason or ai_reason,
                admin_reason_summary=(
                    primary_reason
                    if primary_stage != "final_ai" or primary_reason_code
                    else "Final AI rejected the opportunity for the saved alert."
                ),
                decision_stage=primary_stage,
                decision_reason_code=primary_reason_code,
                precheck_results={**precheck_results, "final_ai": {"matched": False, "reason": ai_reason, "reason_code": primary_reason_code}},
                role_title=role_title,
                location_pref=location_pref,
                salary_pref=salary_pref,
                structured_salary_pref=structured_salary_pref,
                include_no_salary=include_no_salary,
                keywords=keywords,
                project_preferences=project_preferences,
                location_filters=location_filters,
                location_context=location_context,
                work_mode=work_mode,
                final_ai_reason=ai_reason,
            )

        if not all_prechecks_pass:
            self.logger.info(
                "[subscriber-notifier] AI accepted post despite failed prechecks user=%s post=%s reason=%s",
                user_id,
                card.url,
                ai_reason,
            )

        reason_parts: list[str] = []
        active_reasons = [
            (bool(role_title) and role_match, role_reason),
            (bool(spheres) and sphere_match, sphere_reason),
            (bool(location_pref) and location_match, location_reason),
            (bool(salary_pref or not include_no_salary) and salary_match, salary_reason),
            (bool(keywords) and keyword_match, keyword_reason),
            (bool(self._normalize_preference_list(project_preferences.get("deliverables"))) and deliverable_match, deliverable_reason),
            (
                bool(
                    project_preferences.get("minimum_fixed_budget_usd") is not None
                    or project_preferences.get("minimum_hourly_rate_usd") is not None
                )
                and payment_model_match,
                payment_model_reason,
            ),
        ]
        for is_active, reason in active_reasons:
            cleaned_reason = self._clean_message_text(reason)
            if is_active and cleaned_reason and cleaned_reason.lower() != "not specified":
                reason_parts.append(cleaned_reason)
        cleaned_ai_reason = self._clean_message_text(ai_reason)
        admin_ai_reason = (
            cleaned_ai_reason
            if ui_language == "en" and cleaned_ai_reason and cleaned_ai_reason.lower() != "not specified"
            else "Final AI accepted the opportunity for the saved alert."
        )
        if cleaned_ai_reason:
            reason_parts.append(f"AI check: {cleaned_ai_reason}")
        admin_reason_summary = "; ".join(reason_parts[:4]) or "Matched the saved alert settings."
        if ui_language == "ru":
            reason_summary = cleaned_ai_reason or "Проект подходит под ваши фильтры поиска."
        else:
            reason_summary = "; ".join(reason_parts[:5]) or "Matched your saved alert settings."
        review_kind, review_reasons = self._human_review_reasons(
            user_id=user_id,
            card=card,
            role_title=role_title,
            location_pref=location_pref,
            salary_pref=salary_pref,
            include_no_salary=include_no_salary,
            keywords=keywords,
            ai_reason=ai_reason,
            precheck_results=precheck_results,
            location_filters=location_filters,
            location_context=location_context,
            is_remote=is_remote,
        )
        if review_reasons:
            return MatchEvaluation(
                matched=True,
                reason_summary=reason_summary,
                admin_reason_summary=admin_reason_summary,
                needs_human_review=True,
                review_kind=review_kind,
                review_summary="; ".join(review_reasons[:4]),
                review_context=self._build_review_context(
                    card=card,
                    precheck_results={**precheck_results, "final_ai": {"matched": True, "reason": ai_reason, "reason_code": ""}},
                    role_title=role_title,
                    location_pref=location_pref,
                    salary_pref=salary_pref,
                    structured_salary_pref=structured_salary_pref,
                    include_no_salary=include_no_salary,
                    keywords=keywords,
                    project_preferences=project_preferences,
                    location_filters=location_filters,
                    location_context=location_context,
                    work_mode=work_mode,
                    review_reasons=review_reasons,
                    decision_stage="matched_review",
                    decision_reason_code="",
                    decision_reason=admin_reason_summary,
                    final_ai_reason=admin_ai_reason,
                ),
            )
        return MatchEvaluation(
            matched=True,
            reason_summary=reason_summary,
            admin_reason_summary=admin_reason_summary,
        )

    def _human_review_reasons(
        self,
        *,
        user_id: int,
        card: JobCard,
        role_title: str,
        location_pref: str,
        salary_pref: str,
        include_no_salary: bool,
        keywords: list[str],
        ai_reason: str,
        precheck_results: dict[str, dict[str, Any]],
        location_filters: list[LocationFilterSpec],
        location_context: str,
        is_remote: bool,
    ) -> tuple[str, list[str]]:
        del user_id
        reasons: list[str] = []
        card_blob = " ".join(
            part
            for part in (
                card.title,
                getattr(card, "scope_summary", ""),
                card.description,
                getattr(card, "notes", ""),
                getattr(card, "engagement_type", ""),
            )
            if part
        ).lower()
        ai_blob = str(ai_reason or "").lower()
        confidence = float(getattr(card, "confidence", 0.0) or 0.0)
        card_type = self._infer_card_type(card_blob)

        if 0.55 <= confidence < 0.78:
            reasons.append(
                f"Extraction confidence is only {confidence:.2f}, so this opportunity looks plausible but not reliable enough to auto-send."
            )

        design_tokens = {"ux", "ui", "design", "designer", "wireframe", "prototype", "figma", "дизайн", "дизайнер", "интерфейс"}
        engineering_tokens = {
            "engineer",
            "developer",
            "frontend",
            "backend",
            "software",
            "react",
            "javascript",
            "typescript",
            "инженер",
            "разработчик",
            "фронтенд",
            "бэкенд",
        }
        design_phrases = ("product design",)
        engineering_phrases = ("front-end", "full stack")

        def _token_set(value: str) -> set[str]:
            return set(re.findall(r"[a-zA-Zа-яА-ЯёЁ0-9]+", value.lower()))

        def _has_markers(value: str, *, tokens: set[str], phrases: tuple[str, ...]) -> bool:
            lowered = value.lower()
            source_tokens = _token_set(lowered)
            return any(token in source_tokens for token in tokens) or any(phrase in lowered for phrase in phrases)

        role_blob = role_title.lower()
        user_is_design = _has_markers(role_blob, tokens=design_tokens, phrases=design_phrases)
        user_is_engineering = _has_markers(role_blob, tokens=engineering_tokens, phrases=engineering_phrases)
        card_has_design = _has_markers(card_blob, tokens=design_tokens, phrases=design_phrases)
        card_has_engineering = _has_markers(card_blob, tokens=engineering_tokens, phrases=engineering_phrases)
        if (user_is_design and card_has_engineering) or (user_is_engineering and card_has_design) or (
            card_has_design and card_has_engineering
        ):
            reasons.append(
                "The opportunity mixes design and engineering signals, so this could be a true freelance match or a misleading repost."
            )

        article_markers = ("article", "blog", "newsletter", "guide", "case study", "how to", "tips", "resource")
        opportunity_markers = ("project", "contract", "freelance", "brief", "proposal", "rfp", "scope", "client", "budget")
        consulting_markers = ("consult", "consulting", "advisor", "advisory", "audit")
        if any(marker in card_blob for marker in article_markers) and any(marker in card_blob for marker in opportunity_markers):
            reasons.append(
                "This looks partly like editorial or resource content and partly like a real opportunity, so it needs an opportunity review."
            )

        card_tokens = _token_set(card_blob)
        if card_type in {"project", "consulting", "request"} and (
            any(marker in card_blob for marker in ("full-time", "permanent", "employment"))
            or any(token in card_tokens for token in ("position", "vacancy", "job"))
        ):
            reasons.append(
                "This may be a freelance project or a traditional job post, and the system should not guess without review."
            )

        if any(marker in card_blob for marker in consulting_markers) and not any(
            marker in card_blob for marker in ("deliverable", "scope", "brief", "proposal", "budget", "rate", "timeline")
        ):
            reasons.append(
                "This could be a broad consulting-services page or a real consulting brief, so it needs a project review."
            )

        sparse_details = sum(
            1
            for value in (
                getattr(card, "counterparty", ""),
                getattr(card, "payment_terms", ""),
                getattr(card, "duration", ""),
                getattr(card, "skills_required", ""),
                getattr(card, "proposal_or_contact_url", ""),
            )
            if str(value or "").strip()
        )
        if any(marker in card_blob for marker in opportunity_markers) and sparse_details <= 1:
            reasons.append(
                "The post looks like a real opportunity, but it is under-specified enough that a human should verify it before delivery."
            )

        country_sensitive = any(
            item.rule in {"remote_within_country", "onsite_hybrid_within_country", "specific_location"}
            for item in location_filters
        )
        location_blob = location_context.lower()
        vague_location = not location_blob or location_blob in {
            "remote",
            "worldwide",
            "global",
            "anywhere",
            "unknown",
            "not specified",
        }
        if country_sensitive and (vague_location or (is_remote and not card.location.strip())):
            reasons.append(
                "The location filter is strict, but the opportunity location is too vague to be fully trusted without a human check."
            )

        uncertainty_markers = ("unclear", "ambiguous", "borderline", "partial", "possibly", "maybe", "likely")
        if any(marker in ai_blob for marker in uncertainty_markers):
            reasons.append(
                "The AI match explanation itself sounds uncertain, so this is better handled with an opportunity review."
            )

        if not reasons:
            return ("", [])

        joined = " ".join(reasons).lower()
        if "project review" in joined or "opportunity review" in joined:
            review_kind = "project_review"
        elif "design and engineering" in joined:
            review_kind = "role_ambiguity"
        elif "location filter" in joined:
            review_kind = "location_ambiguity"
        else:
            review_kind = "opportunity_review"

        if not salary_pref and include_no_salary and not keywords and precheck_results.get("salary_visibility", {}).get("matched"):
            reasons = reasons[:2]
        return (review_kind, reasons[:3])

    @staticmethod
    def _is_terminal_precheck_failure(stage_name: str, reason_code: str) -> bool:
        normalized_stage = str(stage_name or "").strip().lower()
        normalized_code = str(reason_code or "").strip().lower()
        if normalized_stage in {"source", "content_type"}:
            return True
        return normalized_code in {
            "source_mismatch",
            "content_type_project",
            "role_mismatch",
            "country_restriction",
            "nationality_or_citizenship_restriction",
            "residency_restriction",
            "work_authorization_country_restriction",
            "non_remote",
            "not_country_limited",
            "specific_location_mismatch",
            "salary_policy",
            "payment_type_mismatch",
            "budget_minimum_mismatch",
            "hourly_rate_mismatch",
        }

    @staticmethod
    def _should_queue_decision_review(card: JobCard) -> bool:
        return bool(getattr(card, "is_relevant_opportunity", getattr(card, "is_job_post", False))) and float(
            getattr(card, "confidence", 0.0) or 0.0
        ) > 0.0

    @staticmethod
    def _post_age_text(card: JobCard) -> str:
        raw_value = str(getattr(card, "posted_at_utc", "") or "").strip()
        if not raw_value:
            return "Unknown"
        try:
            parsed = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
        except ValueError:
            return raw_value
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        age = max(timedelta(0), datetime.now(timezone.utc) - parsed.astimezone(timezone.utc))
        total_hours = int(age.total_seconds() // 3600)
        if total_hours < 24:
            return f"{total_hours}h old ({parsed.astimezone(timezone.utc).date().isoformat()})"
        days = total_hours // 24
        return f"{days}d old ({parsed.astimezone(timezone.utc).date().isoformat()})"

    @staticmethod
    def _active_precheck_details(precheck_results: dict[str, dict[str, Any]]) -> tuple[list[str], list[str]]:
        label_map = {
            "source": "Source",
            "content_type": "Content type",
            "role": "Role",
            "spheres": "Sphere",
            "location": "Location",
            "salary_visibility": "Budget / Payment",
            "keywords": "Keywords",
            "deliverables": "Deliverables",
            "payment_model": "Budget / Rate",
            "final_ai": "Final AI",
        }
        matches: list[str] = []
        mismatches: list[str] = []
        for key in (
            "source",
            "content_type",
            "role",
            "spheres",
            "location",
            "salary_visibility",
            "keywords",
            "deliverables",
            "payment_model",
            "final_ai",
        ):
            payload = precheck_results.get(key)
            if not isinstance(payload, dict):
                continue
            matched = bool(payload.get("matched", False))
            reason = str(payload.get("reason", "") or "").strip()
            if not reason or reason.lower().startswith("no ") or reason.lower() == "not specified":
                continue
            target = matches if matched else mismatches
            target.append(f"{label_map.get(key, key.title())}: {reason}")
        return (matches, mismatches)

    def _build_review_context(
        self,
        *,
        card: JobCard,
        precheck_results: dict[str, dict[str, Any]],
        role_title: str,
        location_pref: str,
        salary_pref: str,
        structured_salary_pref: dict[str, object],
        include_no_salary: bool,
        keywords: list[str],
        project_preferences: dict[str, object] | None,
        location_filters: list[LocationFilterSpec],
        location_context: str,
        work_mode: WorkArrangementAssessment,
        review_reasons: list[str] | None = None,
        decision_stage: str = "",
        decision_reason_code: str = "",
        decision_reason: str = "",
        final_ai_reason: str = "",
    ) -> dict[str, Any]:
        matches, mismatches = self._active_precheck_details(precheck_results)
        cleaned_final_ai_reason = self._clean_message_text(final_ai_reason)
        if cleaned_final_ai_reason and cleaned_final_ai_reason.lower() != "not specified":
            if decision_reason:
                mismatches.append(f"Final AI: {cleaned_final_ai_reason}")
            else:
                matches.append(f"Final AI: {cleaned_final_ai_reason}")
        review_lines = [str(line).strip() for line in (review_reasons or []) if str(line).strip()]
        if decision_reason and all(decision_reason not in line for line in mismatches):
            mismatches.insert(0, decision_reason)
        return {
            "ambiguity_reasons": review_lines,
            "matches": matches[:6],
            "mismatches": mismatches[:6],
            "decision_stage": decision_stage,
            "decision_reason_code": decision_reason_code,
            "decision_reason": decision_reason,
            "ai_reason": cleaned_final_ai_reason,
            "precheck_results": precheck_results,
            "role_title": role_title,
            "location_preference": location_pref,
            "salary_preference": salary_pref,
            "salary_preference_structured": structured_salary_pref,
            "include_no_salary": include_no_salary,
            "keywords": keywords,
            "project_preferences": normalize_project_preferences(project_preferences or {}),
            "location_filters": self._serialize_location_filters(location_filters),
            "location_context": location_context,
            "is_remote": work_mode.is_remote,
            "is_hybrid": work_mode.is_hybrid,
            "is_onsite": work_mode.is_onsite,
            "remote_scope": work_mode.remote_scope,
            "scope_locations": list(work_mode.scope_locations),
            "work_mode_confidence": work_mode.confidence,
            "work_mode_reason": work_mode.reason,
            "work_mode_restriction_reason_code": getattr(work_mode, "restriction_reason_code", ""),
            "card_language": getattr(card, "language", "en"),
            "card_confidence": float(getattr(card, "confidence", 0.0) or 0.0),
            "post_age": self._post_age_text(card),
        }

    def _rejected_match_evaluation(
        self,
        *,
        card: JobCard,
        reason_summary: str,
        decision_stage: str,
        decision_reason_code: str,
        precheck_results: dict[str, dict[str, Any]],
        role_title: str,
        location_pref: str,
        salary_pref: str,
        structured_salary_pref: dict[str, object],
        include_no_salary: bool,
        keywords: list[str],
        project_preferences: dict[str, object] | None,
        location_filters: list[LocationFilterSpec],
        location_context: str,
        work_mode: WorkArrangementAssessment,
        admin_reason_summary: str = "",
        final_ai_reason: str = "",
    ) -> MatchEvaluation:
        review_context = self._build_review_context(
            card=card,
            precheck_results=precheck_results,
            role_title=role_title,
            location_pref=location_pref,
            salary_pref=salary_pref,
            structured_salary_pref=structured_salary_pref,
            include_no_salary=include_no_salary,
            keywords=keywords,
            project_preferences=project_preferences,
            location_filters=location_filters,
            location_context=location_context,
            work_mode=work_mode,
            review_reasons=[],
            decision_stage=decision_stage,
            decision_reason_code=decision_reason_code,
            decision_reason=admin_reason_summary or reason_summary,
            final_ai_reason=final_ai_reason,
        )
        return MatchEvaluation(
            matched=False,
            reason_summary=reason_summary,
            admin_reason_summary=admin_reason_summary or reason_summary,
            needs_human_review=self._should_queue_decision_review(card),
            review_kind="rejected_match",
            review_summary=admin_reason_summary or reason_summary,
            review_context=review_context,
            decision_stage=decision_stage,
            decision_reason_code=decision_reason_code,
        )

    def _queue_manual_review_case(
        self,
        *,
        user_id: int,
        username: str,
        card: JobCard,
        evaluation: MatchEvaluation,
    ) -> bool:
        queue_case = getattr(self.store, "queue_manual_review_case", None)
        if not callable(queue_case):
            return False
        role_title = str(evaluation.review_context.get("role_title") or "")
        location_pref = str(evaluation.review_context.get("location_preference") or "")
        salary_pref = str(evaluation.review_context.get("salary_preference") or "")
        include_no_salary = bool(evaluation.review_context.get("include_no_salary", True))
        keywords = [
            str(keyword).strip()
            for keyword in (evaluation.review_context.get("keywords") or [])
            if str(keyword).strip()
        ]
        card_payload = {
            "website": card.website,
            "url": card.url,
            "title": card.title,
            "description": card.description,
            "salary": card.payment_terms or card.salary,
            "location": card.location,
            "is_job_post": card.is_job_post,
            "is_relevant_opportunity": getattr(card, "is_relevant_opportunity", card.is_job_post),
            "confidence": float(card.confidence),
            "extraction_method": card.extraction_method,
            "extracted_at_utc": card.extracted_at_utc,
            "posted_at_utc": getattr(card, "posted_at_utc", ""),
            "notes": getattr(card, "notes", ""),
            "company": getattr(card, "company", ""),
            "language": getattr(card, "language", "en"),
            "opportunity_kind": getattr(card, "opportunity_kind", ""),
            "client": getattr(card, "client", ""),
            "requester": getattr(card, "requester", ""),
            "scope_summary": getattr(card, "scope_summary", ""),
            "budget": getattr(card, "budget", ""),
            "duration": getattr(card, "duration", ""),
            "commitment_level": getattr(card, "commitment_level", ""),
            "skills_required": getattr(card, "skills_required", ""),
            "proposal_deadline": getattr(card, "proposal_deadline", ""),
            "start_timeline": getattr(card, "start_timeline", ""),
            "industry": getattr(card, "industry", ""),
            "engagement_type": getattr(card, "engagement_type", ""),
            "remote_location_constraint": getattr(card, "remote_location_constraint", ""),
            "contact_url": getattr(card, "contact_url", ""),
        }
        try:
            queue_case(
                user_id=user_id,
                username=username,
                card_url=card.url,
                card_website=card.website,
                card_title=card.title,
                card_company=getattr(card, "company", "") or card.counterparty,
                card_location=card.opportunity_location or card.location,
                card_salary=card.payment_terms or card.salary,
                card_language=getattr(card, "language", "en"),
                card_payload=card_payload,
                review_kind=evaluation.review_kind or "opportunity_review",
                ambiguity_summary=evaluation.review_summary,
                match_reason=evaluation.reason_summary,
                role_title=role_title,
                location_preference=location_pref,
                salary_preference=salary_pref,
                include_no_salary=include_no_salary,
                keywords=keywords,
                context_payload=evaluation.review_context,
            )
        except Exception as exc:  # noqa: BLE001
            self.logger.warning(
                "[subscriber-notifier] could not queue manual review user=%s post=%s: %s",
                user_id,
                card.url,
                str(exc),
            )
            return False
        return True

    async def deliver_manual_review_case(
        self,
        *,
        user_id: int,
        username: str,
        card: JobCard,
        match_reason: str,
        force_send: bool = False,
    ) -> tuple[str, int]:
        if not self.store.is_user_subscribed(user_id):
            return ("inactive_subscription", 0)
        if not self._has_alert_configuration(user_id):
            return ("missing_alert", 0)
        if not self.store.get_notification_preference(user_id):
            return ("notifications_paused", 0)
        if not force_send and not self.store.user_selected_website(user_id, card.website):
            return ("website_unselected", 0)

        ui_language = self._ui_language_for_user(user_id, getattr(card, "language", "en"))
        message = self.format_job_message(card, why_matched=match_reason, ui_language=ui_language)
        delivery_event_id = self._record_delivery_event(
            user_id=user_id,
            username=username,
            card=card,
            match_reason=match_reason,
        )
        now_utc = utc_now_dt()
        delivery_mode = self._delivery_mode(user_id)

        if not force_send and delivery_mode == "digest":
            self._queue_notification(
                user_id=user_id,
                username=username,
                card_url=card.url,
                message_text=message,
                available_after_utc=self._next_digest_delivery_time(user_id, now_utc),
                delivery_event_id=delivery_event_id,
            )
            self._log_internal_event(
                user_id=user_id,
                username=username,
                event_type="job_flash_human_review_approved_digest",
                text=f"Approved by human review and queued for digest: {card.url}",
            )
            return ("queued_digest", 1)

        if not force_send and not self._should_deliver_now(user_id, now_utc):
            self._queue_notification(
                user_id=user_id,
                username=username,
                card_url=card.url,
                message_text=message,
                available_after_utc=self._next_allowed_delivery_time(user_id, now_utc),
                delivery_event_id=delivery_event_id,
            )
            self._log_internal_event(
                user_id=user_id,
                username=username,
                event_type="job_flash_human_review_approved_queued",
                text=f"Approved by human review and queued until delivery window opens: {card.url}",
            )
            return ("queued_window", 1)

        try:
            telegram_message = await self.bot.send_message(
                chat_id=user_id,
                text=message,
                disable_web_page_preview=False,
                reply_markup=self._delivery_markup(card.proposal_or_contact_url, delivery_event_id, ui_language),
            )
            self._mark_delivery_event_sent(
                delivery_event_id=delivery_event_id,
                chat_id=user_id,
                message=telegram_message,
            )
            self._log_outbound(
                user_id=user_id,
                username=username,
                text=message,
                event_type="job_flash_human_review_sent",
            )
            return ("sent", 1)
        except Exception as exc:  # noqa: BLE001
            if self.store.is_user_subscribed(user_id):
                self._queue_notification(
                    user_id=user_id,
                    username=username,
                    card_url=card.url,
                    message_text=message,
                    available_after_utc=(
                        format_utc_iso(utc_now_dt() + timedelta(seconds=30))
                        if force_send
                        else self._next_allowed_delivery_time(user_id, utc_now_dt())
                    ),
                    delivery_event_id=delivery_event_id,
                )
                self._log_internal_event(
                    user_id=user_id,
                    username=username,
                    event_type="job_flash_human_review_send_failed",
                    text=f"Approved by human review but immediate send failed and was queued: {card.url} | error={str(exc)}",
                )
                self.logger.warning(
                    "[subscriber-notifier] human-review delivery failed for %s and was queued: %s",
                    user_id,
                    str(exc),
                )
                return ("queued_after_send_failure", 1)
            return ("send_failed", 0)

    async def reevaluate_manual_review_case(self, *, user_id: int, card: JobCard) -> MatchEvaluation:
        evaluation = await self._evaluate_user_filters(user_id, card)
        if evaluation.review_context:
            return evaluation

        role_title = self.store.get_role_preference(user_id).strip()
        location_pref = self.store.get_location_preference(user_id).strip()
        salary_pref = ""
        structured_salary_pref: dict[str, object] = {}
        get_salary_pref = getattr(self.store, "get_salary_range_preference", None)
        if callable(get_salary_pref):
            salary_pref = str(get_salary_pref(user_id) or "").strip()
        get_structured_salary_pref = getattr(self.store, "get_salary_range_structured", None)
        if callable(get_structured_salary_pref):
            raw_structured = get_structured_salary_pref(user_id) or {}
            if isinstance(raw_structured, dict):
                structured_salary_pref = dict(raw_structured)
        include_no_salary = self.store.get_include_no_salary(user_id)
        keywords = self.store.get_user_keywords(user_id)
        project_preferences = self._project_preferences_for_user(user_id)
        location_filters = self._location_filter_specs(location_pref)
        work_mode = await self._assess_work_mode(card)
        location_context = " ".join(
            part
            for part in (
                card.location,
                card.description,
                card.title,
                getattr(card, "notes", ""),
                work_mode.reason,
                ", ".join(work_mode.scope_locations),
            )
            if part
        ).strip()
        review_context = self._build_review_context(
            card=card,
            precheck_results={
                "final_ai": {
                    "matched": bool(evaluation.matched),
                    "reason": evaluation.reason_summary,
                    "reason_code": evaluation.decision_reason_code,
                }
            },
            role_title=role_title,
            location_pref=location_pref,
            salary_pref=salary_pref,
            structured_salary_pref=structured_salary_pref,
            include_no_salary=include_no_salary,
            keywords=keywords,
            project_preferences=project_preferences,
            location_filters=location_filters,
            location_context=location_context,
            work_mode=work_mode,
            review_reasons=[],
            decision_stage=evaluation.decision_stage,
            decision_reason_code=evaluation.decision_reason_code,
            decision_reason="" if evaluation.matched else (evaluation.admin_reason_summary or evaluation.reason_summary),
            final_ai_reason=evaluation.admin_reason_summary if evaluation.matched else "",
        )
        return MatchEvaluation(
            matched=evaluation.matched,
            reason_summary=evaluation.reason_summary,
            admin_reason_summary=evaluation.admin_reason_summary or evaluation.reason_summary,
            needs_human_review=True,
            review_kind=evaluation.review_kind or ("rechecked_match" if evaluation.matched else "rejected_match"),
            review_summary=evaluation.review_summary or evaluation.admin_reason_summary or evaluation.reason_summary,
            review_context=review_context,
            decision_stage=evaluation.decision_stage,
            decision_reason_code=evaluation.decision_reason_code,
        )

    @staticmethod
    def _infer_card_type(card_blob: str) -> str:
        project_hints = {
            "project",
            "freelance",
            "gig",
            "contract",
            "proposal",
            "brief",
            "rfp",
            "scope",
            "statement of work",
            "проект",
            "фриланс",
            "контракт",
        }
        request_hints = {"request", "request for proposal", "brief", "rfp", "quote request", "client request"}
        consulting_hints = {"consult", "consulting", "advisor", "advisory", "audit"}
        article_hints = {"article", "blog", "newsletter", "guide", "case study", "tutorial", "tips"}
        job_hints = {"job", "position", "full-time", "part-time", "employment", "role", "вакансия", "должность", "работа"}
        if any(hint in card_blob for hint in project_hints):
            return "project"
        if any(hint in card_blob for hint in request_hints):
            return "request"
        if any(hint in card_blob for hint in consulting_hints):
            return "consulting"
        if any(hint in card_blob for hint in article_hints):
            return "article"
        if any(hint in card_blob for hint in job_hints):
            return "job"
        return "unknown"

    async def _assess_work_mode(self, card: JobCard) -> WorkArrangementAssessment:
        assessor = getattr(self.filter_ai, "assess_work_arrangement", None)
        if callable(assessor):
            try:
                return await assessor(
                    post_title=card.title,
                    post_description=card.description,
                    post_location=card.location,
                    post_notes=getattr(card, "notes", ""),
                )
            except Exception as exc:  # noqa: BLE001
                self.logger.warning("[subscriber-notifier] work-arrangement AI failed for %s: %s", card.url, str(exc))
        return self._fallback_assess_work_mode(card)

    @classmethod
    def _fallback_assess_work_mode(cls, card: JobCard) -> WorkArrangementAssessment:
        return FilterAI._fallback_work_arrangement_assessment(
            card.title,
            card.description,
            card.location,
            getattr(card, "notes", ""),
        )

    async def _evaluate_location_filters(
        self,
        *,
        location_filters: list[LocationFilterSpec],
        card: JobCard,
        location_context: str,
        work_mode: WorkArrangementAssessment,
    ) -> tuple[bool, str, str]:
        if not location_filters:
            return (True, "Location filter bypassed because no location preference is set.", "")
        failure_reasons: list[str] = []
        failure_codes: list[str] = []
        for location_filter in location_filters:
            matched, reason, reason_code = await self._evaluate_single_location_filter(
                location_filter=location_filter,
                card=card,
                location_context=location_context,
                work_mode=work_mode,
            )
            if matched:
                if len(location_filters) == 1:
                    return (True, reason, "")
                prefix = f"Matched one of your saved location filters ({location_filter.label})."
                return (True, f"{prefix} {reason}".strip(), "")
            if len(failure_reasons) < 3:
                failure_reasons.append(f"{location_filter.label}: {reason}")
            if reason_code and len(failure_codes) < 3:
                failure_codes.append(reason_code)
        if len(location_filters) == 1:
            return (
                False,
                failure_reasons[0] if failure_reasons else "Location filter did not match.",
                failure_codes[0] if failure_codes else "",
            )
        summary = f"No match for any of your saved location filters ({'; '.join(item.label for item in location_filters)})."
        if failure_reasons:
            summary = f"{summary} {'; '.join(failure_reasons)}"
        return (False, summary, failure_codes[0] if failure_codes else "")

    async def _evaluate_single_location_filter(
        self,
        *,
        location_filter: LocationFilterSpec,
        card: JobCard,
        location_context: str,
        work_mode: WorkArrangementAssessment,
    ) -> tuple[bool, str, str]:
        if location_filter.rule == "all_locations":
            return (True, "World Wide preference accepts all locations.", "")
        if location_filter.rule == "remote_global_only":
            if not work_mode.is_remote:
                return (False, "Remote Global alert rejected a non-remote opportunity.", "non_remote")
            remote_match_checker = getattr(self.filter_ai, "check_remote_global_match", None)
            if callable(remote_match_checker):
                location_match, remote_reason = await remote_match_checker(
                    post_title=card.title,
                    post_description=card.description,
                    post_location=card.location,
                )
            else:
                location_match = work_mode.remote_scope == "global" or (
                    not work_mode.has_scope_restriction and work_mode.remote_scope in {"unknown", "global"}
                )
                remote_reason = work_mode.reason or "Remote scope assessment is unavailable."
            reason = (
                f"Remote Global alert matched this opportunity: {remote_reason}"
                if location_match
                else f"Remote Global alert rejected this opportunity: {remote_reason}"
            )
            return (
                location_match,
                reason,
                "" if location_match else (getattr(work_mode, "restriction_reason_code", "") or "country_restriction"),
            )
        if location_filter.rule == "remote_within_country":
            if not work_mode.is_remote:
                return (False, "Remote-within-country alert rejected a non-remote opportunity.", "non_remote")
            if work_mode.remote_scope == "global" and not work_mode.has_scope_restriction:
                return (
                    False,
                    f"Remote-within-country alert rejected this opportunity for {location_filter.country}: globally remote opportunities do not count unless the scope is explicitly country-limited.",
                    "not_country_limited",
                )
            if work_mode.remote_scope != "country_limited" and not work_mode.has_scope_restriction:
                return (
                    False,
                    f"Remote-within-country alert rejected this opportunity for {location_filter.country}: the scope does not show an explicit country-limited remote setup.",
                    "not_country_limited",
                )
            remote_country_checker = getattr(self.filter_ai, "check_remote_country_eligibility", None)
            if callable(remote_country_checker):
                location_match, country_reason = await remote_country_checker(
                    location_filter.country,
                    card.title,
                    card.description,
                    card.location,
                    getattr(card, "notes", ""),
                )
            else:
                scope_text = " ".join(
                    part
                    for part in (
                        ", ".join(work_mode.scope_locations),
                        card.location,
                        location_context,
                    )
                    if part
                ).strip()
                location_match, country_reason = await self.filter_ai.check_location_match(
                    location_filter.country,
                    scope_text,
                )
            reason = (
                f"Remote-within-country alert matched a remote opportunity explicitly limited to {location_filter.country}."
                if location_match
                else f"Remote-within-country alert rejected this opportunity for {location_filter.country}: {country_reason}"
            )
            return (location_match, reason, "" if location_match else "country_restriction")
        if location_filter.rule == "onsite_hybrid_within_country":
            if work_mode.is_remote and not work_mode.is_hybrid:
                return (False, "On-site/hybrid-within-country alert rejected a fully remote opportunity.", "non_remote")
            location_match, country_reason = await self.filter_ai.check_location_match(
                location_filter.country,
                location_context,
            )
            reason = (
                f"On-site/hybrid-within-country alert matched an on-site or hybrid opportunity in {location_filter.country}."
                if location_match
                else (
                    "On-site/hybrid-within-country alert rejected this opportunity "
                    f"for {location_filter.country}: {country_reason}"
                )
            )
            return (location_match, reason, "" if location_match else "country_restriction")
        specific_location = location_filter.location or location_filter.label
        location_match, location_reason = await self.filter_ai.check_location_match(specific_location, card.location)
        if location_match:
            return (True, location_reason.strip() or f"Matched your saved location ({specific_location}).", "")
        return (
            False,
            location_reason.strip() or f"Specific location did not match ({specific_location}).",
            "specific_location_mismatch",
        )

    @classmethod
    def _location_filter_specs(cls, location_pref: str) -> list[LocationFilterSpec]:
        cleaned_pref = str(location_pref or "").strip()
        if not cleaned_pref:
            return []
        raw_items = FilterAI.split_location_preferences(cleaned_pref)
        if not raw_items:
            raw_items = [cleaned_pref]
        specs: list[LocationFilterSpec] = []
        seen: set[tuple[str, str, str]] = set()
        for raw_item in raw_items:
            item = " ".join(str(raw_item or "").split()).strip()
            if not item:
                continue
            if cls._is_worldwide_preference(item):
                spec = LocationFilterSpec(label=item, rule="all_locations")
            elif cls._is_remote_global_preference(item) or cls._is_broad_remote_preference(item):
                spec = LocationFilterSpec(label=cls.REMOTE_GLOBAL_PREFERENCE_LABEL, rule="remote_global_only")
            else:
                remote_country = cls._remote_within_country_preference(item)
                onsite_hybrid_country = cls._onsite_hybrid_within_country_preference(item)
                if remote_country:
                    spec = LocationFilterSpec(
                        label=f"Remote within {remote_country}",
                        rule="remote_within_country",
                        country=remote_country,
                    )
                elif onsite_hybrid_country:
                    spec = LocationFilterSpec(
                        label=f"On-site/hybrid within {onsite_hybrid_country}",
                        rule="onsite_hybrid_within_country",
                        country=onsite_hybrid_country,
                    )
                else:
                    spec = LocationFilterSpec(label=item, rule="specific_location", location=item)
            dedupe_key = (spec.rule, spec.country.strip().lower(), (spec.location or spec.label).strip().lower())
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            specs.append(spec)
        return specs

    @staticmethod
    def _serialize_location_filters(location_filters: list[LocationFilterSpec]) -> list[dict[str, str]]:
        serialized: list[dict[str, str]] = []
        for location_filter in location_filters:
            payload: dict[str, str] = {
                "label": location_filter.label,
                "rule": location_filter.rule,
            }
            if location_filter.country:
                payload["country"] = location_filter.country
            if location_filter.location:
                payload["location"] = location_filter.location
            serialized.append(payload)
        return serialized

    @staticmethod
    def _normalize_location_preference(location_pref: str) -> str:
        normalized = location_pref.strip().lower().replace("-", " ")
        return re.sub(r"\s+", " ", normalized).strip()

    @classmethod
    def _is_broad_remote_preference(cls, location_pref: str) -> bool:
        normalized = re.sub(r"\([^)]*\)", "", cls._normalize_location_preference(location_pref)).strip()
        if not normalized.startswith("remote"):
            return False
        if normalized in {"remote global"}:
            return False
        return not normalized.startswith(cls.REMOTE_WITHIN_COUNTRY_PREFIX)

    @classmethod
    def _is_remote_global_preference(cls, location_pref: str) -> bool:
        normalized = re.sub(r"\([^)]*\)", "", cls._normalize_location_preference(location_pref)).strip()
        return normalized in {"remote global"}

    @staticmethod
    def _is_worldwide_preference(location_pref: str) -> bool:
        normalized = location_pref.strip().lower().replace(" ", "")
        return normalized in {"worldwide", "worldwide(anylocation)", "anylocation", "global", "alllocations"}

    @classmethod
    def _remote_within_country_preference(cls, location_pref: str) -> str:
        normalized = location_pref.strip()
        if not normalized:
            return ""
        lowered = normalized.lower()
        if not lowered.startswith(cls.REMOTE_WITHIN_COUNTRY_PREFIX):
            return ""
        return normalized[len(cls.REMOTE_WITHIN_COUNTRY_PREFIX) :].strip(" -,:")

    @classmethod
    def _onsite_hybrid_within_country_preference(cls, location_pref: str) -> str:
        normalized = location_pref.strip()
        if not normalized:
            return ""
        lowered = normalized.lower()
        for prefix in cls.ONSITE_HYBRID_WITHIN_COUNTRY_PREFIXES:
            if lowered.startswith(prefix):
                return normalized[len(prefix) :].strip(" -,:")
        return ""

    def _has_alert_configuration(self, user_id: int) -> bool:
        checker = getattr(self.store, "has_alert_configuration", None)
        if callable(checker):
            return bool(checker(user_id))
        return bool(self.store.get_role_preference(user_id).strip())

    def _record_delivery_event(self, *, user_id: int, username: str, card: JobCard, match_reason: str) -> int | None:
        recorder = getattr(self.store, "record_delivery_event", None)
        if not callable(recorder):
            return None
        try:
            return int(
                recorder(
                    user_id=user_id,
                    username=username,
                    card_url=card.url,
                    card_title=card.title,
                    card_company=getattr(card, "company", "") or card.counterparty,
                    card_location=card.opportunity_location or card.location,
                    card_website=card.website,
                    match_reason=match_reason,
                )
            )
        except Exception as exc:  # noqa: BLE001
            self.logger.warning(
                "[subscriber-notifier] could not record delivery event user=%s post=%s: %s",
                user_id,
                card.url,
                str(exc),
            )
            return None

    def _mark_delivery_event_sent(self, *, delivery_event_id: int | None, chat_id: int, message: object) -> None:
        if delivery_event_id is None:
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
                telegram_chat_id=int(chat_id),
                telegram_message_id=message_id,
            )
        except Exception as exc:  # noqa: BLE001
            self.logger.warning(
                "[subscriber-notifier] could not persist sent message metadata event_id=%s chat_id=%s: %s",
                delivery_event_id,
                chat_id,
                str(exc),
            )

    def _record_filter_mismatch_event(
        self,
        *,
        user_id: int,
        card: JobCard,
        stage: str,
        reason: str,
        reason_code: str = "",
    ) -> None:
        recorder = getattr(self.store, "record_filter_mismatch_event", None)
        if not callable(recorder):
            return
        username = ""
        latest_sub_getter = getattr(self.store, "get_latest_subscription", None)
        if callable(latest_sub_getter):
            try:
                latest_sub = latest_sub_getter(user_id)
                username = str(getattr(latest_sub, "username", "") or "")
            except Exception:  # noqa: BLE001
                username = ""
        try:
            recorder(
                user_id=user_id,
                username=username,
                card_url=card.url,
                card_website=card.website,
                mismatch_stage=stage,
                reason_code=reason_code,
                reason=reason,
            )
        except Exception as exc:  # noqa: BLE001
            self.logger.warning(
                "[subscriber-notifier] could not record filter mismatch user=%s post=%s: %s",
                user_id,
                card.url,
                str(exc),
            )

    def _queued_item_matches_current_website_selection(self, item: Any) -> bool:
        delivery_event_id = getattr(item, "delivery_event_id", None)
        if delivery_event_id is None:
            return True
        getter = getattr(self.store, "get_delivery_event", None)
        if not callable(getter):
            return True
        try:
            event = getter(delivery_event_id)
        except Exception as exc:  # noqa: BLE001
            self.logger.warning(
                "[subscriber-notifier] could not load delivery event queue_id=%s event_id=%s: %s",
                getattr(item, "queue_id", "unknown"),
                delivery_event_id,
                str(exc),
            )
            return True
        if event is None:
            return True
        if int(getattr(event, "user_id", getattr(item, "user_id", 0))) != int(getattr(item, "user_id", 0)):
            self.logger.warning(
                "[subscriber-notifier] delivery event user mismatch queue_id=%s event_id=%s queued_user=%s event_user=%s",
                getattr(item, "queue_id", "unknown"),
                delivery_event_id,
                getattr(item, "user_id", "unknown"),
                getattr(event, "user_id", "unknown"),
            )
            return False
        website = str(getattr(event, "card_website", "") or "").strip()
        if not website:
            return True
        return bool(self.store.user_selected_website(item.user_id, website))

    def _queue_notification(
        self,
        *,
        user_id: int,
        username: str,
        card_url: str,
        message_text: str,
        available_after_utc: str,
        delivery_event_id: int | None,
    ) -> None:
        try:
            self.store.queue_notification(
                user_id=user_id,
                username=username,
                card_url=card_url,
                message_text=message_text,
                available_after_utc=available_after_utc,
                delivery_event_id=delivery_event_id,
            )
        except TypeError:
            self.store.queue_notification(
                user_id=user_id,
                username=username,
                card_url=card_url,
                message_text=message_text,
                available_after_utc=available_after_utc,
            )

    def _ui_language_for_user(self, user_id: int, fallback: str = "en") -> str:
        getter = getattr(self.store, "get_ui_language", None)
        if callable(getter):
            value = str(getter(user_id) or "").strip().lower()
            if value in {"en", "ru"}:
                return value
        normalized_fallback = normalize_message_language(fallback)
        return normalized_fallback if normalized_fallback in {"en", "ru", "ar"} else "en"

    def _delivery_mode(self, user_id: int) -> str:
        getter = getattr(self.store, "get_delivery_mode", None)
        if callable(getter):
            value = str(getter(user_id) or "").strip().lower()
            if value == "digest":
                return "digest"
        return "instant"

    def _should_deliver_now(self, user_id: int, at_utc) -> bool:
        return bool(self.store.get_notification_preference(user_id))

    def _next_allowed_delivery_time(self, user_id: int, from_utc) -> str:
        getter = getattr(self.store, "next_allowed_delivery_time", None)
        if callable(getter):
            return str(getter(user_id, from_utc=from_utc))
        return format_utc_iso(from_utc + timedelta(seconds=30))

    def _next_digest_delivery_time(self, user_id: int, from_utc) -> str:
        return self._next_allowed_delivery_time(user_id, from_utc)

    def _log_outbound(self, user_id: int, username: str, text: str, event_type: str) -> None:
        if self.user_message_logger is None:
            return
        self.user_message_logger.append_event(
            user_id=user_id,
            username=username,
            direction="out",
            text=text,
            event_type=event_type,
            created_at_utc=utc_now_iso(),
        )

    def _log_internal_event(self, user_id: int, username: str, event_type: str, text: str) -> None:
        if self.user_message_logger is None:
            return
        self.user_message_logger.append_event(
            user_id=user_id,
            username=username,
            direction="system",
            text=text,
            event_type=event_type,
            created_at_utc=utc_now_iso(),
        )

    def _website_currency_for_user(self, user_id: int, website_url: str) -> str:
        getter = getattr(self.store, "get_user_website_currency", None)
        if callable(getter):
            currency = str(getter(user_id, website_url) or "").strip().upper()
            if currency in {"USD", "RUB"}:
                return currency
        lowered = str(website_url or "").lower()
        if ".ru" in lowered:
            return "RUB"
        return "USD"

    @staticmethod
    def _salary_bounds_for_currency(normalized_salary, raw_text: str, currency: str) -> tuple[float | None, float | None]:
        lowered = str(raw_text or "").lower()
        low: float | None
        high: float | None
        if currency == "RUB":
            if str(getattr(normalized_salary, "currency", "") or "").upper() in {"RUB", "RUR", "UNKNOWN"} or any(
                marker in lowered for marker in ("руб", "₽", "rub", "rur")
            ):
                low = getattr(normalized_salary, "min_value", None)
                high = getattr(normalized_salary, "max_value", None)
            else:
                low = None
                high = None
        else:
            low = getattr(normalized_salary, "min_usd", None)
            high = getattr(normalized_salary, "max_usd", None)
            normalized_currency = str(getattr(normalized_salary, "currency", "") or "").upper()
            if low is None and normalized_currency in {"USD", "UNKNOWN"}:
                low = getattr(normalized_salary, "min_value", None)
            if high is None and normalized_currency in {"USD", "UNKNOWN"}:
                high = getattr(normalized_salary, "max_value", None)
        if low is not None and high is not None and low > high:
            low, high = high, low
        return (low, high)

    async def _payment_preferences_match_for_user(
        self,
        user_id: int,
        card: JobCard,
        project_preferences: dict[str, object],
    ) -> tuple[bool, str, str]:
        source_currency = self._website_currency_for_user(user_id, card.website)
        if source_currency == "RUB":
            minimum_payment = project_preferences.get("minimum_payment_rub")
            maximum_payment = project_preferences.get("maximum_payment_rub")
        else:
            minimum_payment = project_preferences.get("minimum_payment_usd")
            maximum_payment = project_preferences.get("maximum_payment_usd")
        if minimum_payment is None and maximum_payment is None:
            return (True, "No payment range filter.", "")

        payment_text = str(card.payment_terms or card.salary or "").strip()
        if not payment_text or payment_text.lower() in {"unknown", "not specified", "n/a", "na", "none"}:
            return (False, "Opportunity does not show a visible payment amount.", "payment_missing")

        normalized_salary = await self.filter_ai.normalize_salary(payment_text)
        if not normalized_salary.is_specified:
            return (False, "Opportunity does not show a visible payment amount.", "payment_missing")

        candidate_low, candidate_high = self._salary_bounds_for_currency(normalized_salary, payment_text, source_currency)
        if candidate_low is None and candidate_high is None:
            return (True, "Payment could not be normalized cleanly, so the AI should decide.", "")
        if candidate_low is None:
            candidate_low = candidate_high
        if candidate_high is None:
            candidate_high = candidate_low
        if candidate_low is None or candidate_high is None:
            return (True, "Payment details are incomplete, so the AI should decide.", "")

        if minimum_payment is not None and candidate_high < float(minimum_payment):
            return (False, "Opportunity payment is below the requested minimum.", "payment_minimum_mismatch")
        if maximum_payment is not None and candidate_low > float(maximum_payment):
            return (False, "Opportunity payment is above the requested maximum.", "payment_maximum_mismatch")
        return (True, "Opportunity payment fits the requested payment range.", "")

    @staticmethod
    def _active_precheck_details(precheck_results: dict[str, dict[str, Any]]) -> tuple[list[str], list[str]]:
        label_map = {
            "source": "Source",
            "content_type": "Content type",
            "role": "Role",
            "spheres": "Sphere",
            "keywords": "Keywords",
            "deliverables": "Deliverables",
            "payment_model": "Payment",
            "final_ai": "Final AI",
        }
        matches: list[str] = []
        mismatches: list[str] = []
        for key in ("source", "content_type", "role", "spheres", "keywords", "deliverables", "payment_model", "final_ai"):
            payload = precheck_results.get(key)
            if not isinstance(payload, dict):
                continue
            matched = bool(payload.get("matched", False))
            reason = str(payload.get("reason", "") or "").strip()
            if not reason or reason.lower().startswith("no ") or reason.lower() == "not specified":
                continue
            target = matches if matched else mismatches
            target.append(f"{label_map.get(key, key.title())}: {reason}")
        return (matches, mismatches)

    @staticmethod
    def _is_terminal_precheck_failure(stage_name: str, reason_code: str) -> bool:
        normalized_stage = str(stage_name or "").strip().lower()
        normalized_code = str(reason_code or "").strip().lower()
        if normalized_stage in {"source", "content_type"}:
            return True
        return normalized_code in {
            "source_mismatch",
            "content_type_project",
            "role_mismatch",
            "payment_missing",
            "payment_minimum_mismatch",
            "payment_maximum_mismatch",
        }

    async def _evaluate_user_filters(self, user_id: int, card: JobCard) -> MatchEvaluation:
        if not self.store.is_user_subscribed(user_id):
            return MatchEvaluation(matched=False)
        if not self._has_alert_configuration(user_id):
            return MatchEvaluation(matched=False)
        if not self.store.get_notification_preference(user_id):
            return MatchEvaluation(matched=False)
        ui_language = self._ui_language_for_user(user_id, getattr(card, "language", "en"))

        match_blob = " ".join(
            part
            for part in (
                card.title,
                card.scope_summary or card.description,
                card.payment_terms or card.salary,
                card.counterparty,
                getattr(card, "skills_required", ""),
                getattr(card, "engagement_type", ""),
                getattr(card, "duration", ""),
                getattr(card, "start_timeline", ""),
                getattr(card, "industry", ""),
            )
            if part
        ).lower()
        project_scope_blob = " ".join(
            part
            for part in (
                card.title,
                getattr(card, "scope_summary", ""),
                card.description,
                getattr(card, "skills_required", ""),
                getattr(card, "engagement_type", ""),
                getattr(card, "duration", ""),
                getattr(card, "start_timeline", ""),
                getattr(card, "industry", ""),
                getattr(card, "notes", ""),
                card.counterparty,
            )
            if part
        )
        work_mode = await self._assess_work_mode(card)
        role_title = self.store.get_role_preference(user_id).strip()
        spheres = [] if role_title else self.store.get_user_spheres(user_id)
        keywords = self.store.get_user_keywords(user_id)
        project_preferences = self._project_preferences_for_user(user_id)

        if not self.store.user_selected_website(user_id, card.website):
            source_reason = "Post source was not selected in the user's website preferences."
            self.logger.info(
                "[subscriber-notifier] source reject user=%s post=%s reason=%s",
                user_id,
                card.url,
                source_reason,
            )
            self._record_filter_mismatch_event(
                user_id=user_id,
                card=card,
                stage="source",
                reason=source_reason,
                reason_code="source_mismatch",
            )
            return self._rejected_match_evaluation(
                card=card,
                reason_summary=source_reason,
                decision_stage="source",
                decision_reason_code="source_mismatch",
                precheck_results={"source": {"matched": False, "reason": source_reason, "reason_code": "source_mismatch"}},
                role_title=role_title,
                location_pref="",
                salary_pref="",
                structured_salary_pref={},
                include_no_salary=True,
                keywords=keywords,
                project_preferences=project_preferences,
                location_filters=[],
                location_context="",
                work_mode=work_mode,
            )

        classification_blob = " ".join(
            part
            for part in (
                card.title,
                card.scope_summary or card.description,
                card.payment_terms or card.salary,
                card.counterparty,
                getattr(card, "engagement_type", ""),
                getattr(card, "duration", ""),
                getattr(card, "start_timeline", ""),
                getattr(card, "industry", ""),
                getattr(card, "notes", ""),
                work_mode.reason,
            )
            if part
        ).lower()
        card_type = self._infer_card_type(classification_blob)
        role_match = True
        role_reason = "No role filter."
        if role_title:
            role_alignment_checker = getattr(self.filter_ai, "assess_role_alignment", None)
            if callable(role_alignment_checker):
                role_match, role_reason = await role_alignment_checker(
                    requested_role=role_title,
                    post_title=card.title,
                    post_description=card.description,
                    post_location=card.location,
                )
            else:
                role_match, role_reason = await self.filter_ai.semantic_match_any(
                    [role_title],
                    card.title,
                    threshold=0.62,
                    label="role",
                )

        sphere_match = True
        sphere_reason = "No sphere filter."
        if spheres:
            sphere_match, sphere_reason = await self.filter_ai.semantic_match_any(
                spheres,
                match_blob,
                threshold=0.54,
                label="sphere",
            )

        keyword_match = True
        keyword_reason = "No keyword filter."
        if keywords:
            keyword_match, keyword_reason = await self.filter_ai.semantic_match_any(
                keywords,
                match_blob,
                threshold=0.58,
                label="keyword",
            )

        deliverable_match, deliverable_reason = self._preference_terms_match(
            project_preferences.get("deliverables"),
            project_scope_blob,
            field_name="deliverables",
            label="deliverable",
        )
        payment_model_match, payment_model_reason, payment_model_reason_code = await self._payment_preferences_match_for_user(
            user_id,
            card,
            project_preferences,
        )

        precheck_results: dict[str, dict[str, Any]] = {
            "source": {"matched": True, "reason": "Source was selected in the alert.", "reason_code": ""},
            "role": {"matched": role_match, "reason": role_reason, "reason_code": "" if role_match else "role_mismatch"},
            "spheres": {"matched": sphere_match, "reason": sphere_reason, "reason_code": "" if sphere_match else "sphere_mismatch"},
            "keywords": {"matched": keyword_match, "reason": keyword_reason, "reason_code": "" if keyword_match else "keyword_mismatch"},
            "deliverables": {
                "matched": deliverable_match,
                "reason": deliverable_reason,
                "reason_code": "" if deliverable_match else "deliverable_mismatch",
            },
            "payment_model": {
                "matched": payment_model_match,
                "reason": payment_model_reason,
                "reason_code": payment_model_reason_code,
            },
        }

        terminal_failures = [
            (stage_name, details)
            for stage_name, details in precheck_results.items()
            if isinstance(details, dict)
            and not bool(details.get("matched", False))
            and self._is_terminal_precheck_failure(stage_name, str(details.get("reason_code") or ""))
        ]
        if terminal_failures:
            primary_stage, primary_payload = terminal_failures[0]
            primary_reason = str(primary_payload.get("reason") or "Filter mismatch").strip() or "Filter mismatch"
            primary_reason_code = str(primary_payload.get("reason_code") or "").strip()
            self.logger.info(
                "[subscriber-notifier] terminal precheck reject user=%s post=%s stage=%s reason_code=%s reason=%s",
                user_id,
                card.url,
                primary_stage,
                primary_reason_code or "none",
                primary_reason,
            )
            self._record_filter_mismatch_event(
                user_id=user_id,
                card=card,
                stage=primary_stage,
                reason=primary_reason,
                reason_code=primary_reason_code,
            )
            return self._rejected_match_evaluation(
                card=card,
                reason_summary=primary_reason,
                decision_stage=primary_stage,
                decision_reason_code=primary_reason_code,
                precheck_results=precheck_results,
                role_title=role_title,
                location_pref="",
                salary_pref="",
                structured_salary_pref={},
                include_no_salary=True,
                keywords=keywords,
                project_preferences=project_preferences,
                location_filters=[],
                location_context="",
                work_mode=work_mode,
            )

        all_prechecks_pass = all(bool(item["matched"]) for item in precheck_results.values())
        has_active_filters = bool(
            role_title
            or spheres
            or keywords
            or self._has_active_project_preferences(project_preferences)
        )
        if not has_active_filters:
            return MatchEvaluation(
                matched=all_prechecks_pass,
                reason_summary=localized_text(
                    ui_language,
                    en="Matched your saved alert settings.",
                    ru="Проект подходит под ваши фильтры поиска.",
                ),
                admin_reason_summary="Matched the saved alert settings.",
            )

        feedback_guidance = ""
        feedback_builder = getattr(self.store, "build_match_feedback_guidance", None)
        if callable(feedback_builder):
            try:
                feedback_guidance = str(feedback_builder(user_id, limit=8) or "").strip()
            except Exception as exc:  # noqa: BLE001
                self.logger.warning(
                    "[subscriber-notifier] could not load match feedback guidance user=%s: %s",
                    user_id,
                    str(exc),
                )

        ai_match, ai_reason = await self.filter_ai.confirm_post_matches_filters(
            post_x={
                "url": card.url,
                "website": card.website,
                "title": card.title,
                "description": card.description,
                "scope_summary": getattr(card, "scope_summary", ""),
                "location": card.location,
                "salary": card.payment_terms or card.salary,
                "budget_or_rate": card.payment_terms or card.salary,
                "engagement_type": getattr(card, "engagement_type", ""),
                "duration": getattr(card, "duration", ""),
                "start_timeline": getattr(card, "start_timeline", ""),
                "skills_requested": getattr(card, "skills_required", ""),
                "opportunity_kind": getattr(card, "opportunity_kind", ""),
                "is_remote": work_mode.is_remote,
                "is_hybrid": work_mode.is_hybrid,
                "is_onsite": work_mode.is_onsite,
                "remote_scope": work_mode.remote_scope,
                "scope_locations": list(work_mode.scope_locations),
                "work_mode_confidence": work_mode.confidence,
                "work_mode_reason": work_mode.reason,
                "has_scope_restriction": work_mode.has_scope_restriction,
                "card_type_guess": card_type,
                "language": getattr(card, "language", "en"),
            },
            user_filters={
                "content_mode": "freelance_opportunities",
                "role_title": role_title,
                "spheres": spheres,
                "keywords": keywords,
                "project_preferences": project_preferences,
                "deliverables": project_preferences.get("deliverables", []),
                "minimum_payment_usd": project_preferences.get("minimum_payment_usd"),
                "maximum_payment_usd": project_preferences.get("maximum_payment_usd"),
                "minimum_payment_rub": project_preferences.get("minimum_payment_rub"),
                "maximum_payment_rub": project_preferences.get("maximum_payment_rub"),
                "source_currency": self._website_currency_for_user(user_id, card.website),
                "feedback_guidance": feedback_guidance,
                "output_language": ui_language,
            },
            precheck_results=precheck_results,
        )
        if not ai_match:
            self.logger.info(
                "[subscriber-notifier] filter reject user=%s post=%s reason=%s",
                user_id,
                card.url,
                ai_reason,
            )
            self._record_filter_mismatch_event(
                user_id=user_id,
                card=card,
                stage="final_ai",
                reason=ai_reason,
                reason_code="",
            )
            return self._rejected_match_evaluation(
                card=card,
                reason_summary=ai_reason,
                admin_reason_summary="Final AI rejected the opportunity for the saved alert.",
                decision_stage="final_ai",
                decision_reason_code="",
                precheck_results={**precheck_results, "final_ai": {"matched": False, "reason": ai_reason, "reason_code": ""}},
                role_title=role_title,
                location_pref="",
                salary_pref="",
                structured_salary_pref={},
                include_no_salary=True,
                keywords=keywords,
                project_preferences=project_preferences,
                location_filters=[],
                location_context="",
                work_mode=work_mode,
                final_ai_reason=ai_reason,
            )

        reason_parts: list[str] = []
        active_reasons = [
            (bool(role_title) and role_match, role_reason),
            (bool(spheres) and sphere_match, sphere_reason),
            (bool(keywords) and keyword_match, keyword_reason),
            (bool(self._normalize_preference_list(project_preferences.get("deliverables"))) and deliverable_match, deliverable_reason),
            (
                bool(
                    project_preferences.get("minimum_payment_usd") is not None
                    or project_preferences.get("maximum_payment_usd") is not None
                    or project_preferences.get("minimum_payment_rub") is not None
                    or project_preferences.get("maximum_payment_rub") is not None
                )
                and payment_model_match,
                payment_model_reason,
            ),
        ]
        for is_active, reason in active_reasons:
            cleaned_reason = self._clean_message_text(reason)
            if is_active and cleaned_reason and cleaned_reason.lower() != "not specified":
                reason_parts.append(cleaned_reason)
        cleaned_ai_reason = self._clean_message_text(ai_reason)
        admin_ai_reason = (
            cleaned_ai_reason
            if ui_language == "en" and cleaned_ai_reason and cleaned_ai_reason.lower() != "not specified"
            else "Final AI accepted the opportunity for the saved alert."
        )
        if cleaned_ai_reason:
            reason_parts.append(f"AI check: {cleaned_ai_reason}")
        admin_reason_summary = "; ".join(reason_parts[:4]) or "Matched the saved alert settings."
        if ui_language == "ru":
            reason_summary = cleaned_ai_reason or "Проект подходит под ваши фильтры поиска."
        else:
            reason_summary = "; ".join(reason_parts[:5]) or "Matched your saved alert settings."

        review_kind, review_reasons = self._human_review_reasons(
            user_id=user_id,
            card=card,
            role_title=role_title,
            location_pref="",
            salary_pref="",
            include_no_salary=True,
            keywords=keywords,
            ai_reason=ai_reason,
            precheck_results=precheck_results,
            location_filters=[],
            location_context="",
            is_remote=work_mode.is_remote,
        )
        if review_reasons:
            return MatchEvaluation(
                matched=True,
                reason_summary=reason_summary,
                admin_reason_summary=admin_reason_summary,
                needs_human_review=True,
                review_kind=review_kind,
                review_summary="; ".join(review_reasons[:4]),
                review_context=self._build_review_context(
                    card=card,
                    precheck_results={**precheck_results, "final_ai": {"matched": True, "reason": ai_reason, "reason_code": ""}},
                    role_title=role_title,
                    location_pref="",
                    salary_pref="",
                    structured_salary_pref={},
                    include_no_salary=True,
                    keywords=keywords,
                    project_preferences=project_preferences,
                    location_filters=[],
                    location_context="",
                    work_mode=work_mode,
                    review_reasons=review_reasons,
                    decision_stage="matched_review",
                    decision_reason_code="",
                    decision_reason=admin_reason_summary,
                    final_ai_reason=admin_ai_reason,
                ),
            )
        return MatchEvaluation(
            matched=True,
            reason_summary=reason_summary,
            admin_reason_summary=admin_reason_summary,
        )
