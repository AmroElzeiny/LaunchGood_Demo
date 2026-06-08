from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from dataclasses import dataclass

from job_bot.human_review_confidence import display_value, stabilize_job_confidence
from job_bot.models import JobCard
from job_bot.project_filters import (
    FULL_PROJECT_FILTER_FIELDS,
    normalize_project_preferences,
    project_preferences_display_lines,
)
from job_bot.storage import HumanReviewItem, StateStore
from job_bot.subscriber_notifier import SubscriberNotifier
from job_bot.telegram_subscription_store import SubscriptionStore


@dataclass(slots=True)
class HumanReviewTargetRecipient:
    user_id: int
    username: str
    role: str
    keywords: tuple[str, ...]
    specialties: tuple[str, ...]
    project_preferences: tuple[str, ...]
    delivery_mode: str
    match_reason: str


@dataclass(slots=True)
class HumanReviewPresentation:
    card: JobCard
    confidence: float
    target_recipient: HumanReviewTargetRecipient | None


@dataclass(slots=True)
class HumanReviewActionResult:
    success: bool
    decision: str
    message: str
    sent_count: int = 0


def _plain_review_text(value: object, *, fallback: str = "Unknown") -> str:
    return SubscriberNotifier._clean_message_text(
        display_value(value, fallback=fallback),
        default=fallback,
    )


def _username_label(username: str, user_id: int) -> str:
    cleaned = str(username or "").strip()
    if cleaned:
        return cleaned if cleaned.startswith("@") else f"@{cleaned}"
    return f"user {user_id}"

def _reason_label(raw_reason: str) -> str:
    cleaned = str(raw_reason or "").strip()
    if not cleaned:
        return "No queue reason recorded."
    if cleaned.startswith("low_confidence_extraction:"):
        return cleaned.replace("low_confidence_extraction:", "Low-confidence extraction: ", 1)
    return cleaned


def _project_preferences_display_lines(project_preferences: dict[str, object], *, limit: int = 4) -> tuple[str, ...]:
    lines = project_preferences_display_lines(
        project_preferences,
        language="en",
        fields=FULL_PROJECT_FILTER_FIELDS,
        limit=limit,
    )
    return tuple(lines[:limit])


def job_card_from_review_item(item: HumanReviewItem) -> JobCard:
    payload: dict[str, object] = {}
    with contextlib.suppress(json.JSONDecodeError, TypeError, ValueError):
        payload = json.loads(str(getattr(item, "card_json", "") or "{}"))

    title = str(payload.get("title") or item.title or "")
    description = str(payload.get("description") or item.description or "")
    salary = str(payload.get("salary") or item.salary or "")
    location = str(payload.get("location") or item.location or "")
    company = str(payload.get("company") or "")
    client = str(payload.get("client") or company or "")
    requester = str(payload.get("requester") or client or company or "")
    posted_at_utc = str(payload.get("posted_at_utc") or "")
    notes = str(payload.get("notes") or "")
    is_job_post = bool(payload.get("is_job_post", False))
    is_relevant_opportunity = bool(payload.get("is_relevant_opportunity", is_job_post))
    confidence = stabilize_job_confidence(
        float(payload.get("confidence") or item.confidence or 0.0),
        is_job_post=is_relevant_opportunity or is_job_post,
        title=title,
        description=description,
        location=location,
        salary=str(payload.get("budget") or salary or ""),
        company=client or requester or company,
        posted_at_utc=posted_at_utc,
        notes=notes,
    )

    return JobCard(
        website=str(payload.get("website") or item.website or ""),
        url=str(payload.get("url") or item.url or ""),
        title=title,
        description=description,
        salary=salary,
        location=location,
        is_job_post=is_job_post,
        confidence=confidence,
        extraction_method=str(payload.get("extraction_method") or item.source or "human-review"),
        extracted_at_utc=str(payload.get("extracted_at_utc") or item.created_at_utc or ""),
        posted_at_utc=posted_at_utc,
        notes=notes,
        company=company,
        language=str(payload.get("language") or "en" or "en"),
        is_relevant_opportunity=is_relevant_opportunity,
        opportunity_kind=str(payload.get("opportunity_kind") or ""),
        client=client,
        requester=requester,
        scope_summary=str(payload.get("scope_summary") or description or ""),
        budget=str(payload.get("budget") or salary or ""),
        duration=str(payload.get("duration") or ""),
        commitment_level=str(payload.get("commitment_level") or ""),
        skills_required=str(payload.get("skills_required") or ""),
        proposal_deadline=str(payload.get("proposal_deadline") or ""),
        start_timeline=str(payload.get("start_timeline") or ""),
        industry=str(payload.get("industry") or ""),
        engagement_type=str(payload.get("engagement_type") or ""),
        remote_location_constraint=str(payload.get("remote_location_constraint") or location or ""),
        contact_url=str(payload.get("contact_url") or payload.get("url") or item.url or ""),
    )


def _recipient_details(
    user_id: int,
    username: str,
    subs_store: SubscriptionStore,
    *,
    match_reason: str,
) -> HumanReviewTargetRecipient:
    keywords = tuple(subs_store.get_user_keywords(user_id)[:5])
    specialties = tuple(subs_store.get_user_spheres(user_id)[:3])
    project_preferences_getter = getattr(subs_store, "get_project_preferences", None)
    project_preferences = (
        normalize_project_preferences(project_preferences_getter(user_id))
        if callable(project_preferences_getter)
        else normalize_project_preferences({})
    )
    delivery_mode = "digest" if subs_store.get_delivery_mode(user_id) == "digest" else "instant"
    return HumanReviewTargetRecipient(
        user_id=user_id,
        username=_username_label(username, user_id),
        role=display_value(subs_store.get_role_preference(user_id), fallback="not set"),
        keywords=keywords,
        specialties=specialties,
        project_preferences=_project_preferences_display_lines(project_preferences),
        delivery_mode=delivery_mode,
        match_reason=display_value(match_reason, fallback="Matched subscriber alert filters."),
    )


async def build_human_review_presentation_async(
    item: HumanReviewItem,
    subs_store: SubscriptionStore,
    *,
    subscriber_notifier: SubscriberNotifier | None = None,
) -> HumanReviewPresentation:
    card = job_card_from_review_item(item)
    for user_id, username in subs_store.get_active_subscribers():
        if not subs_store.user_selected_website(user_id, item.website):
            continue
        if subscriber_notifier is None or not hasattr(subscriber_notifier, "_evaluate_user_filters"):
            return HumanReviewPresentation(
                card=card,
                confidence=card.confidence,
                target_recipient=_recipient_details(
                    user_id,
                    username,
                    subs_store,
                    match_reason="Selected this website; exact auto-send match was not evaluated here.",
                ),
            )
        try:
            evaluation = await subscriber_notifier._evaluate_user_filters(user_id, card)
        except Exception:  # noqa: BLE001
            continue
        if getattr(evaluation, "matched", False):
            return HumanReviewPresentation(
                card=card,
                confidence=card.confidence,
                target_recipient=_recipient_details(
                    user_id,
                    username,
                    subs_store,
                    match_reason=(
                        str(getattr(evaluation, "admin_reason_summary", "") or "").strip()
                        or str(getattr(evaluation, "reason_summary", "") or "")
                    ),
                ),
            )
    return HumanReviewPresentation(
        card=card,
        confidence=card.confidence,
        target_recipient=None,
    )


def build_human_review_presentation(
    item: HumanReviewItem,
    subs_store: SubscriptionStore,
    *,
    subscriber_notifier: SubscriberNotifier | None = None,
) -> HumanReviewPresentation:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(
            build_human_review_presentation_async(
                item,
                subs_store,
                subscriber_notifier=subscriber_notifier,
            )
        )
    finally:
        loop.close()


def format_human_review_text(item: HumanReviewItem, presentation: HumanReviewPresentation) -> str:
    card = presentation.card
    posted_at = _plain_review_text(card.posted_at_utc)
    if presentation.target_recipient is None:
        target_text = "No matching subscriber is currently eligible for auto-send."
    else:
        target = presentation.target_recipient
        keywords = _plain_review_text(", ".join(target.keywords), fallback="none")
        specialties = _plain_review_text(", ".join(target.specialties), fallback="none")
        project_preference_lines = [
            f"Project preferences: {_plain_review_text(line, fallback='not set')}"
            for line in target.project_preferences
            if _plain_review_text(line, fallback="").strip()
        ]
        project_preferences = "\n".join(project_preference_lines) or "Project preferences: none"
        target_text = (
            f"User ID: {target.user_id}\n"
            f"Username: {_plain_review_text(target.username)}\n"
            f"Role filter: {_plain_review_text(target.role, fallback='not set')}\n"
            f"Delivery mode: {_plain_review_text(target.delivery_mode, fallback='instant')}\n"
            f"Keywords: {keywords}\n"
            f"Specialties: {specialties}\n"
            f"{project_preferences}\n"
            f"Why it matches: {_plain_review_text(target.match_reason, fallback='Matched subscriber alert filters.')}"
        )
    return (
        "Project Review Needed\n"
        f"Review ID: {item.id}\n"
        f"Project: {_plain_review_text(card.title)}\n"
        f"Client: {_plain_review_text(card.counterparty)}\n"
        f"Budget / Rate: {_plain_review_text(card.payment_terms)}\n"
        f"Engagement type: {_plain_review_text(card.engagement_type)}\n"
        f"Timeline / Duration: {_plain_review_text(' / '.join(part for part in (card.start_timeline, card.duration) if part))}\n"
        f"Skills requested: {_plain_review_text(card.skills_required)}\n"
        f"Remote / Location: {_plain_review_text(card.opportunity_location)}\n"
        f"Website: {_plain_review_text(card.website)}\n"
        f"URL: {_plain_review_text(card.url)}\n"
        f"Language: {_plain_review_text(card.language, fallback='en')}\n"
        f"Confidence: {presentation.confidence:.3f}\n"
        f"Extraction source: {_plain_review_text(card.extraction_method)}\n"
        f"Posted at: {posted_at}\n"
        f"Why queued for project review: {_plain_review_text(_reason_label(item.reason), fallback='No queue reason recorded.')}\n"
        f"Extractor notes: {_plain_review_text(card.notes, fallback='No extractor notes.')}\n"
        "Target subscriber:\n"
        f"{target_text}\n\n"
        "Approve & Send: mark the project as valid and dispatch it to matching active subscribers.\n"
        "Neglect: reject the project and remove it from the pending review queue."
    )


async def approve_review_item(
    item: HumanReviewItem,
    *,
    state_store: StateStore,
    subs_store: SubscriptionStore,
    bot_token: str,
    reviewer: str,
    notes: str,
    logger: logging.Logger,
    send_to_subscribers: bool,
    subscriber_notifier: SubscriberNotifier | None = None,
    openai_api_key: str = "",
    openai_model: str = "gpt-4o-mini",
    keyword_model: str = "",
    final_match_model: str = "",
) -> HumanReviewActionResult:
    card = job_card_from_review_item(item)
    state_store.update_human_review_confidence(item.id, card.confidence)
    cluster_registration = state_store.register_job_cluster(card)
    if cluster_registration.is_duplicate:
        duplicate_note = (
            f"{notes}\n\nSkipped duplicate cluster. Canonical source: {cluster_registration.canonical_url}"
            if notes.strip()
            else f"Skipped duplicate cluster. Canonical source: {cluster_registration.canonical_url}"
        )
        state_store.set_human_review_decision(
            item.id,
            decision="approved_no_send",
            reviewer=reviewer,
            notes=duplicate_note,
            sent_count=0,
        )
        state_store.mark_status(item.website, item.url, status="neglected_duplicate_cluster", neglected=True)
        return HumanReviewActionResult(
            success=True,
            decision="approved_no_send",
            message="Approved, but skipped because this project already exists in a canonical cluster.",
            sent_count=0,
        )

    if not send_to_subscribers:
        state_store.set_human_review_decision(
            item.id,
            decision="approved_no_send",
            reviewer=reviewer,
            notes=notes,
            sent_count=0,
        )
        state_store.mark_job_saved(item.website, item.url)
        return HumanReviewActionResult(
            success=True,
            decision="approved_no_send",
            message="Approved without dispatch. The post is saved as a valid project, but no subscribers were notified.",
            sent_count=0,
        )

    if not bot_token.strip():
        return HumanReviewActionResult(
            success=False,
            decision="pending",
            message="Missing TELEGRAM_BOT_TOKEN. Cannot send approved review to subscribers.",
            sent_count=0,
        )

    notifier = subscriber_notifier or SubscriberNotifier(
        bot_token=bot_token,
        store=subs_store,
        logger=logger,
        openai_api_key=openai_api_key,
        openai_model=openai_model,
        keyword_model=keyword_model,
        final_match_model=final_match_model,
    )
    try:
        outcome = await notifier.dispatch_new_job(card, allow_manual_review=False)
    except Exception as exc:  # noqa: BLE001
        return HumanReviewActionResult(
            success=False,
            decision="pending",
            message=f"Subscriber dispatch failed: {str(exc)}",
            sent_count=0,
        )

    dispatched_any = outcome.sent_count > 0 or outcome.queued_count > 0
    decision = "approved_sent" if dispatched_any else "approved_no_send"
    state_store.set_human_review_decision(
        item.id,
        decision=decision,
        reviewer=reviewer,
        notes=notes,
        sent_count=outcome.sent_count,
    )
    state_store.mark_job_saved(item.website, item.url)
    if outcome.sent_count > 0:
        return HumanReviewActionResult(
            success=True,
            decision=decision,
            message=f"Approved and sent to {outcome.sent_count} users.",
            sent_count=outcome.sent_count,
        )
    if outcome.queued_count > 0:
        return HumanReviewActionResult(
            success=True,
            decision=decision,
            message=f"Approved and queued for delivery to {outcome.queued_count} users.",
            sent_count=outcome.sent_count,
        )
    return HumanReviewActionResult(
        success=True,
        decision=decision,
        message="Approved, but no matching active subscribers were found.",
        sent_count=outcome.sent_count,
    )


def neglect_review_item(
    item: HumanReviewItem,
    *,
    state_store: StateStore,
    reviewer: str,
    notes: str,
) -> HumanReviewActionResult:
    state_store.set_human_review_decision(
        item.id,
        decision="neglected",
        reviewer=reviewer,
        notes=notes,
        sent_count=0,
    )
    state_store.mark_status(item.website, item.url, status="neglected_human_review", neglected=True)
    return HumanReviewActionResult(
        success=True,
        decision="neglected",
        message="Post neglected.",
        sent_count=0,
    )
