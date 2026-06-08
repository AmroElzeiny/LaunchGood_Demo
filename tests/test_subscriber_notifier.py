from __future__ import annotations

import logging
import re
import unittest
from dataclasses import dataclass
from types import SimpleNamespace

from job_bot.filter_ai import FilterAI, SalaryNormalization
from job_bot.models import JobCard
from job_bot.subscriber_notifier import SubscriberNotifier


@dataclass
class _QueuedItem:
    queue_id: int
    user_id: int
    username: str
    card_url: str
    message_text: str
    delivery_event_id: int | None
    available_after_utc: str


class _FakeStore:
    def __init__(self) -> None:
        self.active_subscribers: list[tuple[int, str]] = []
        self.subscribed_users: set[int] = set()
        self.selected_domains: dict[int, set[str]] = {}
        self.search_method: dict[int, str] = {}
        self.role_titles: dict[int, str] = {}
        self.spheres: dict[int, list[str]] = {}
        self.location_pref: dict[int, str] = {}
        self.include_no_salary: dict[int, bool] = {}
        self.notification_preferences: dict[int, bool] = {}
        self.salary_pref: dict[int, str] = {}
        self.salary_pref_structured: dict[int, dict[str, object]] = {}
        self.keywords: dict[int, list[str]] = {}
        self.ui_languages: dict[int, str] = {}
        self.project_preferences: dict[int, dict[str, object]] = {}
        self.website_currencies: dict[tuple[int, str], str] = {}
        self.queued_notifications: list[_QueuedItem] = []
        self.delivery_mode: dict[int, str] = {}
        self.should_deliver: dict[int, bool] = {}
        self.next_allowed: dict[int, str] = {}
        self.next_digest: dict[int, str] = {}
        self.feedback_guidance: dict[int, str] = {}
        self.delivery_events: dict[int, dict[str, object]] = {}
        self.next_event_id = 1
        self.next_queue_id = 1
        self.next_review_id = 1
        self.cancelled_notifications: list[tuple[int, str]] = []
        self.sent_queue_ids: list[int] = []
        self.rescheduled_notifications: list[tuple[int, str, str | None]] = []
        self.manual_review_cases: list[dict[str, object]] = []
        self.delivery_jobs: list[SimpleNamespace] = []
        self.completed_delivery_jobs: list[int] = []
        self.failed_delivery_jobs: list[tuple[int, str]] = []
        self.rescheduled_delivery_jobs: list[tuple[int, str, str]] = []
        self.next_delivery_job_id = 1

    def get_active_subscribers(self) -> list[tuple[int, str]]:
        return list(self.active_subscribers)

    def is_user_subscribed(self, user_id: int) -> bool:
        return user_id in self.subscribed_users

    def user_selected_website(self, user_id: int, candidate_website: str) -> bool:
        selected = self.selected_domains.get(user_id, set())
        return candidate_website in selected

    def get_search_method(self, user_id: int) -> str:
        return self.search_method.get(user_id, "both")

    def get_role_preference(self, user_id: int) -> str:
        return self.role_titles.get(user_id, "")

    def get_user_spheres(self, user_id: int) -> list[str]:
        return list(self.spheres.get(user_id, []))

    def get_location_preference(self, user_id: int) -> str:
        return self.location_pref.get(user_id, "")

    def get_include_no_salary(self, user_id: int) -> bool:
        return bool(self.include_no_salary.get(user_id, True))

    def get_notification_preference(self, user_id: int) -> bool:
        return bool(self.notification_preferences.get(user_id, True))

    def get_salary_range_preference(self, user_id: int) -> str:
        return self.salary_pref.get(user_id, "")

    def get_salary_range_structured(self, user_id: int) -> dict[str, object]:
        return dict(self.salary_pref_structured.get(user_id, {}))

    def get_user_keywords(self, user_id: int) -> list[str]:
        return list(self.keywords.get(user_id, []))

    def get_project_preferences(self, user_id: int) -> dict[str, object]:
        return dict(self.project_preferences.get(user_id, {}))

    def get_user_website_currency(self, user_id: int, url: str) -> str:
        return self.website_currencies.get((user_id, url), "")

    def get_ui_language(self, user_id: int) -> str:
        return self.ui_languages.get(user_id, "")

    def has_alert_configuration(self, user_id: int) -> bool:
        return bool(self.role_titles.get(user_id, "").strip())

    def queue_notification(
        self,
        user_id: int,
        username: str,
        card_url: str,
        message_text: str,
        available_after_utc: str,
        delivery_event_id: int | None = None,
    ) -> None:
        self.queued_notifications.append(
            _QueuedItem(
                queue_id=self.next_queue_id,
                user_id=user_id,
                username=username,
                card_url=card_url,
                message_text=message_text,
                delivery_event_id=delivery_event_id,
                available_after_utc=available_after_utc,
            )
        )
        self.next_queue_id += 1

    def get_due_notifications(self, limit: int = 100) -> list[_QueuedItem]:
        return list(self.queued_notifications[:limit])

    def get_delivery_mode(self, user_id: int) -> str:
        return self.delivery_mode.get(user_id, "instant")

    def should_deliver_now(self, user_id: int, at_utc=None) -> bool:
        del at_utc
        return self.should_deliver.get(user_id, True)

    def next_allowed_delivery_time(self, user_id: int, from_utc=None) -> str:
        del from_utc
        return self.next_allowed.get(user_id, "2030-01-01T00:00:00Z")

    def next_digest_delivery_time(self, user_id: int, from_utc=None) -> str:
        del from_utc
        return self.next_digest.get(user_id, "2030-01-01T06:00:00Z")

    def record_delivery_event(
        self,
        *,
        user_id: int,
        username: str,
        card_url: str,
        card_title: str,
        card_company: str,
        card_location: str,
        card_website: str,
        match_reason: str,
    ) -> int:
        event_id = self.next_event_id
        self.next_event_id += 1
        self.delivery_events[event_id] = {
            "user_id": user_id,
            "username": username,
            "card_url": card_url,
            "card_title": card_title,
            "card_company": card_company,
            "card_location": card_location,
            "card_website": card_website,
            "match_reason": match_reason,
        }
        return event_id

    def mark_delivery_event_sent(
        self,
        event_id: int,
        *,
        telegram_chat_id: int,
        telegram_message_id: int,
    ) -> None:
        payload = self.delivery_events.get(event_id)
        if payload is None:
            return
        payload["telegram_chat_id"] = telegram_chat_id
        payload["telegram_message_id"] = telegram_message_id
        payload["sent_at_utc"] = "2030-01-01T00:00:00Z"

    def get_delivery_event(self, event_id: int):
        payload = self.delivery_events.get(event_id)
        if payload is None:
            return None
        return SimpleNamespace(event_id=event_id, **payload)

    def mark_notification_sent(self, queue_id: int) -> None:
        self.sent_queue_ids.append(queue_id)
        self.queued_notifications = [item for item in self.queued_notifications if item.queue_id != queue_id]

    def cancel_notification(self, queue_id: int, reason: str) -> None:
        self.cancelled_notifications.append((queue_id, reason))
        self.queued_notifications = [item for item in self.queued_notifications if item.queue_id != queue_id]

    def reschedule_notification(self, queue_id: int, available_after_utc: str, last_error: str | None = None) -> None:
        self.rescheduled_notifications.append((queue_id, available_after_utc, last_error))
        for item in self.queued_notifications:
            if item.queue_id == queue_id:
                item.available_after_utc = available_after_utc
                break

    def build_match_feedback_guidance(self, user_id: int, limit: int = 8) -> str:
        del limit
        return self.feedback_guidance.get(user_id, "")

    def queue_manual_review_case(self, **payload: object) -> int:
        payload = dict(payload)
        payload["review_id"] = self.next_review_id
        self.next_review_id += 1
        self.manual_review_cases.append(payload)
        return int(payload["review_id"])

    def queue_delivery_job(
        self,
        *,
        website: str,
        card_url: str,
        card_json: str,
        available_after_utc: str | None = None,
    ) -> int:
        queue_id = self.next_delivery_job_id
        self.next_delivery_job_id += 1
        self.delivery_jobs.append(
            SimpleNamespace(
                queue_id=queue_id,
                website=website,
                card_url=card_url,
                card_json=card_json,
                status="queued",
                attempt_count=0,
                available_after_utc=available_after_utc or "2030-01-01T00:00:00Z",
                created_at_utc="2030-01-01T00:00:00Z",
                updated_at_utc="2030-01-01T00:00:00Z",
                last_error="",
            )
        )
        return queue_id

    def claim_due_delivery_jobs(self, limit: int = 1):
        claimed = []
        for job in self.delivery_jobs:
            if job.status != "queued":
                continue
            job.status = "processing"
            claimed.append(job)
            if len(claimed) >= limit:
                break
        return claimed

    def mark_delivery_job_completed(self, queue_id: int) -> None:
        self.completed_delivery_jobs.append(queue_id)
        for job in self.delivery_jobs:
            if job.queue_id == queue_id:
                job.status = "completed"
                break

    def reschedule_delivery_job(self, queue_id: int, *, available_after_utc: str, last_error: str = "") -> None:
        self.rescheduled_delivery_jobs.append((queue_id, available_after_utc, last_error))
        for job in self.delivery_jobs:
            if job.queue_id == queue_id:
                job.status = "queued"
                job.attempt_count += 1
                job.available_after_utc = available_after_utc
                job.last_error = last_error
                break

    def fail_delivery_job(self, queue_id: int, *, last_error: str = "") -> None:
        self.failed_delivery_jobs.append((queue_id, last_error))
        for job in self.delivery_jobs:
            if job.queue_id == queue_id:
                job.status = "failed"
                job.last_error = last_error
                break


class _FakeBot:
    def __init__(self) -> None:
        self.sent_messages: list[tuple[int, str, object | None]] = []

    async def send_message(
        self,
        chat_id: int,
        text: str,
        disable_web_page_preview: bool = False,
        reply_markup: object | None = None,
    ):
        del disable_web_page_preview
        self.sent_messages.append((chat_id, text, reply_markup))
        return SimpleNamespace(message_id=len(self.sent_messages))


class _FakeFilterAI:
    def __init__(
        self,
        final_match: bool,
        final_reason: str = "final decision",
        *,
        role_match: bool = True,
        role_reason: str = "role alignment pass",
        sphere_match: bool = True,
        sphere_reason: str = "sphere semantic match",
        keyword_match: bool = True,
        keyword_reason: str = "keyword semantic match",
        location_match: bool = True,
        location_reason: str = "location precheck pass",
        location_results: dict[str, tuple[bool, str]] | None = None,
        remote_global_match: bool = True,
        remote_global_reason: str = "global remote pass",
        remote_country_match: bool | None = None,
        remote_country_reason: str = "remote-country pass",
        salary_match: bool = True,
        salary_reason: str = "salary precheck pass",
    ) -> None:
        self.final_match = final_match
        self.final_reason = final_reason
        self.role_match = role_match
        self.role_reason = role_reason
        self.sphere_match = sphere_match
        self.sphere_reason = sphere_reason
        self.keyword_match = keyword_match
        self.keyword_reason = keyword_reason
        self.location_match = location_match
        self.location_reason = location_reason
        self.location_results = dict(location_results or {})
        self.remote_global_match = remote_global_match
        self.remote_global_reason = remote_global_reason
        self.remote_country_match = location_match if remote_country_match is None else remote_country_match
        self.remote_country_reason = remote_country_reason
        self.salary_match = salary_match
        self.salary_reason = salary_reason
        self.confirm_calls: list[tuple[dict[str, object], dict[str, object], dict[str, object]]] = []
        self.location_calls: list[tuple[str, str]] = []
        self.remote_global_calls: list[tuple[str, str, str]] = []
        self.remote_country_calls: list[tuple[str, str, str, str]] = []
        self.salary_calls: list[tuple[str, str, bool]] = []
        self.salary_structured_calls: list[tuple[dict[str, object], str, bool]] = []

    async def check_location_match(self, user_location: str, post_location: str) -> tuple[bool, str]:
        self.location_calls.append((user_location, post_location))
        custom_result = self.location_results.get(user_location)
        if custom_result is not None:
            return custom_result
        return (self.location_match, self.location_reason)

    async def check_remote_global_match(
        self,
        *,
        post_title: str,
        post_description: str,
        post_location: str = "",
    ) -> tuple[bool, str]:
        self.remote_global_calls.append((post_title, post_description, post_location))
        return (self.remote_global_match, self.remote_global_reason)

    async def check_remote_country_eligibility(
        self,
        target_country: str,
        post_title: str,
        post_description: str,
        post_location: str = "",
        post_notes: str = "",
    ) -> tuple[bool, str]:
        del post_notes
        self.remote_country_calls.append((target_country, post_title, post_description, post_location))
        return (self.remote_country_match, self.remote_country_reason)

    async def assess_role_alignment(
        self,
        *,
        requested_role: str,
        post_title: str,
        post_description: str,
        post_location: str = "",
    ) -> tuple[bool, str]:
        del requested_role, post_title, post_description, post_location
        return (self.role_match, self.role_reason)

    async def salary_matches(
        self,
        user_salary_pref: str,
        post_salary: str,
        include_no_salary: bool,
    ) -> tuple[bool, str]:
        self.salary_calls.append((user_salary_pref, post_salary, include_no_salary))
        return (self.salary_match, self.salary_reason)

    async def salary_matches_structured(
        self,
        user_salary_pref: dict[str, object],
        post_salary: str,
        include_no_salary: bool,
    ) -> tuple[bool, str]:
        self.salary_structured_calls.append((dict(user_salary_pref), post_salary, include_no_salary))
        return (self.salary_match, self.salary_reason)

    async def normalize_salary(self, text: str) -> SalaryNormalization:
        raw = text.strip()
        cleaned = raw.replace(",", "")
        values = [float(item) for item in re.findall(r"\d+(?:\.\d+)?", cleaned)]
        if not values:
            return SalaryNormalization(
                raw_input=raw,
                is_specified=False,
                currency="USD",
                period="unknown",
                min_value=None,
                max_value=None,
                min_usd=None,
                max_usd=None,
                canonical_text=raw or "Not specified",
                confidence=0.0,
            )
        low = min(values)
        high = max(values)
        upper = raw.upper()
        currency = "RUB" if any(token in upper for token in ("RUB", "RUR", "РУБ", "₽")) else "USD"
        period = "hour" if any(token in upper for token in ("/HR", "PER HOUR", "HOUR", "ЧАС")) else "project"
        return SalaryNormalization(
            raw_input=raw,
            is_specified=True,
            currency=currency,
            period=period,
            min_value=low,
            max_value=high,
            min_usd=low if currency == "USD" else None,
            max_usd=high if currency == "USD" else None,
            canonical_text=raw,
            confidence=0.95,
        )

    async def semantic_match_any(
        self,
        terms: list[str],
        text: str,
        *,
        threshold: float,
        label: str,
    ) -> tuple[bool, str]:
        del terms, text, threshold
        if label == "role":
            return (self.role_match, self.role_reason)
        if label == "sphere":
            return (self.sphere_match, self.sphere_reason)
        if label == "keyword":
            return (self.keyword_match, self.keyword_reason)
        return (self.final_match, f"{label} semantic match")

    async def confirm_post_matches_filters(
        self,
        *,
        post_x: dict[str, object],
        user_filters: dict[str, object],
        precheck_results: dict[str, dict[str, object]],
    ) -> tuple[bool, str]:
        self.confirm_calls.append((post_x, user_filters, precheck_results))
        return (self.final_match, self.final_reason)


def _sample_card() -> JobCard:
    return JobCard(
        website="https://example.com",
        url="https://example.com/careers/backend-engineer",
        title="Backend Engineer",
        description="Build APIs for a product team.",
        salary="USD 3000-5000 per month",
        location="Cairo, Egypt",
        is_job_post=True,
        confidence=0.95,
        extraction_method="test",
    )


def _russian_card() -> JobCard:
    return JobCard(
        website="https://workspace.ru/web-design/",
        url="https://workspace.ru/tenders/redizayn-sayta-123456",
        title="Продуктовый дизайнер",
        description="Проектировать интерфейсы и пользовательские сценарии.",
        salary="200000 RUB",
        location="Москва, Россия",
        is_job_post=True,
        confidence=0.95,
        extraction_method="test",
        company="Яндекс",
        language="ru",
    )


def _project_card() -> JobCard:
    return JobCard(
        website="https://example.com",
        url="https://example.com/project/ui-redesign",
        title="Freelance UI Project",
        description="<p>Need a UI/UX designer for a short contract.</p>",
        salary="USD 2500 fixed price",
        location="Remote",
        is_job_post=True,
        confidence=0.92,
        extraction_method="test",
        client="Acme Startup",
        scope_summary="Redesign a SaaS landing page and dashboard in Figma.",
        duration="3 weeks",
        skills_required="Figma, UX research, UI design",
        engagement_type="fixed-price project",
    )


class SubscriberNotifierTests(unittest.IsolatedAsyncioTestCase):
    def _build_notifier(self, store: _FakeStore, ai: _FakeFilterAI) -> tuple[SubscriberNotifier, _FakeBot]:
        notifier = SubscriberNotifier(
            bot_token="123:ABC",
            store=store,  # type: ignore[arg-type]
            logger=logging.getLogger("test-subscriber-notifier"),
            openai_api_key="",
            openai_model="gpt-4o-mini",
        )
        fake_bot = _FakeBot()
        notifier.bot = fake_bot  # type: ignore[assignment]
        notifier.filter_ai = ai  # type: ignore[assignment]
        return notifier, fake_bot

    async def test_dispatch_skips_send_when_subscription_inactive(self) -> None:
        store = _FakeStore()
        store.active_subscribers = [(1, "alice")]
        store.selected_domains[1] = {"https://example.com"}
        ai = _FakeFilterAI(final_match=True)
        notifier, fake_bot = self._build_notifier(store, ai)

        outcome = await notifier.dispatch_new_job(_sample_card())

        self.assertEqual(fake_bot.sent_messages, [])
        self.assertEqual(ai.confirm_calls, [])
        self.assertEqual(outcome.active_subscribers, 0)
        self.assertEqual(outcome.filter_matched_subscribers, 0)

    async def test_dispatch_uses_ai_final_confirmation_to_allow_send(self) -> None:
        store = _FakeStore()
        store.active_subscribers = [(1, "alice")]
        store.subscribed_users = {1}
        store.selected_domains[1] = {"https://example.com"}
        store.role_titles[1] = "Backend Engineer"
        store.spheres[1] = ["devops"]
        ai = _FakeFilterAI(final_match=True, final_reason="semantic sphere match")
        notifier, fake_bot = self._build_notifier(store, ai)

        outcome = await notifier.dispatch_new_job(_sample_card())

        self.assertEqual(len(fake_bot.sent_messages), 1)
        self.assertEqual(len(ai.confirm_calls), 1)
        self.assertEqual(outcome.active_subscribers, 1)
        self.assertEqual(outcome.filter_matched_subscribers, 1)
        self.assertEqual(outcome.sent_count, 1)
        post_x, user_filters, _ = ai.confirm_calls[0]
        self.assertEqual(post_x["url"], "https://example.com/careers/backend-engineer")
        self.assertEqual(user_filters["role_title"], "Backend Engineer")
        self.assertEqual(user_filters["spheres"], [])

    async def test_delivery_worker_processes_queued_job(self) -> None:
        store = _FakeStore()
        store.active_subscribers = [(1, "alice")]
        store.subscribed_users = {1}
        store.selected_domains[1] = {"https://example.com"}
        store.role_titles[1] = "Backend Engineer"
        ai = _FakeFilterAI(final_match=True, final_reason="semantic sphere match")
        notifier, fake_bot = self._build_notifier(store, ai)

        queue_id = notifier.queue_job_for_delivery(_sample_card())
        self.assertIsNotNone(queue_id)

        processed = await notifier.process_one_delivery_job()

        self.assertTrue(processed)
        self.assertEqual(len(fake_bot.sent_messages), 1)
        self.assertIn(int(queue_id), store.completed_delivery_jobs)

    async def test_dispatch_queues_human_review_for_ambiguous_match_instead_of_sending(self) -> None:
        store = _FakeStore()
        store.active_subscribers = [(23, "alice")]
        store.subscribed_users = {23}
        store.selected_domains[23] = {"https://example.com"}
        store.role_titles[23] = "UX/UI Designer"
        ai = _FakeFilterAI(final_match=True, final_reason="Likely relevant, but the role mixes product design and frontend ownership.")
        notifier, fake_bot = self._build_notifier(store, ai)

        ambiguous_card = JobCard(
            website="https://example.com",
            url="https://example.com/jobs/product-designer-frontend",
            title="Product Designer / Frontend",
            description="Own product design, Figma flows, and React implementation for the marketing site.",
            salary="USD 4000 per month",
            location="Remote",
            is_job_post=True,
            confidence=0.71,
            extraction_method="test",
            company="Acme",
        )

        outcome = await notifier.dispatch_new_job(ambiguous_card)

        self.assertEqual(fake_bot.sent_messages, [])
        self.assertEqual(outcome.sent_count, 0)
        self.assertEqual(outcome.human_review_count, 1)
        self.assertEqual(len(store.manual_review_cases), 1)
        queued_case = store.manual_review_cases[0]
        self.assertEqual(queued_case["user_id"], 23)
        self.assertEqual(queued_case["card_url"], "https://example.com/jobs/product-designer-frontend")
        self.assertIn("design and engineering signals", str(queued_case["ambiguity_summary"]))
        self.assertIn("UX/UI Designer", str(queued_case["role_title"]))

    async def test_dispatch_uses_role_filter_when_present(self) -> None:
        store = _FakeStore()
        store.active_subscribers = [(4, "dina")]
        store.subscribed_users = {4}
        store.selected_domains[4] = {"https://example.com"}
        store.role_titles[4] = "Backend Engineer"
        ai = _FakeFilterAI(final_match=True, final_reason="role match")
        notifier, fake_bot = self._build_notifier(store, ai)

        outcome = await notifier.dispatch_new_job(_sample_card())

        self.assertEqual(len(fake_bot.sent_messages), 1)
        self.assertEqual(outcome.sent_count, 1)
        self.assertEqual(store.delivery_events[1]["telegram_chat_id"], 4)
        self.assertEqual(store.delivery_events[1]["telegram_message_id"], 1)
        _, user_filters, prechecks = ai.confirm_calls[0]
        self.assertEqual(user_filters["role_title"], "Backend Engineer")
        self.assertTrue(prechecks["role"]["matched"])

    async def test_dispatch_rejects_keyword_overlap_when_role_alignment_fails(self) -> None:
        store = _FakeStore()
        store.active_subscribers = [(18, "zoe")]
        store.subscribed_users = {18}
        store.selected_domains[18] = {"https://example.com"}
        store.role_titles[18] = "UX/UI Designer"
        store.keywords[18] = ["figma", "design system"]
        ai = _FakeFilterAI(
            final_match=True,
            role_match=False,
            role_reason="Primary role is Frontend Engineer. Employer wants React UI implementation.",
            keyword_match=True,
        )
        notifier, fake_bot = self._build_notifier(store, ai)

        front_end_card = JobCard(
            website="https://example.com",
            url="https://example.com/jobs/frontend-engineer",
            title="Frontend Engineer",
            description="Build React UI screens, maintain the design system, and collaborate with designers in Figma.",
            salary="USD 5000 per month",
            location="Remote",
            is_job_post=True,
            confidence=0.95,
            extraction_method="test",
        )

        outcome = await notifier.dispatch_new_job(front_end_card)

        self.assertEqual(fake_bot.sent_messages, [])
        self.assertEqual(outcome.sent_count, 0)
        self.assertEqual(ai.confirm_calls, [])

    async def test_dispatch_blocks_send_when_ai_rejects(self) -> None:
        store = _FakeStore()
        store.active_subscribers = [(2, "bob")]
        store.subscribed_users = {2}
        store.selected_domains[2] = {"https://example.com"}
        store.role_titles[2] = "Backend Engineer"
        store.keywords[2] = ["python"]
        ai = _FakeFilterAI(final_match=False, final_reason="keywords mismatch")
        notifier, fake_bot = self._build_notifier(store, ai)

        outcome = await notifier.dispatch_new_job(_sample_card())

        self.assertEqual(fake_bot.sent_messages, [])
        self.assertEqual(len(ai.confirm_calls), 1)
        self.assertEqual(outcome.active_subscribers, 1)
        self.assertEqual(outcome.filter_matched_subscribers, 0)

    async def test_dispatch_allows_project_like_cards_as_matching_opportunities(self) -> None:
        store = _FakeStore()
        store.active_subscribers = [(3, "charlie")]
        store.subscribed_users = {3}
        store.selected_domains[3] = {"https://example.com"}
        store.role_titles[3] = "UX/UI Designer"
        ai = _FakeFilterAI(final_match=True)
        notifier, fake_bot = self._build_notifier(store, ai)

        outcome = await notifier.dispatch_new_job(_project_card())

        self.assertEqual(len(fake_bot.sent_messages), 1)
        self.assertEqual(outcome.sent_count, 1)
        self.assertEqual(len(ai.confirm_calls), 1)
        _, user_filters, _ = ai.confirm_calls[0]
        self.assertEqual(user_filters["content_mode"], "freelance_opportunities")

    async def test_dispatch_skips_paused_alerts(self) -> None:
        store = _FakeStore()
        store.active_subscribers = [(5, "eve")]
        store.subscribed_users = {5}
        store.selected_domains[5] = {"https://example.com"}
        store.role_titles[5] = "Backend Engineer"
        store.notification_preferences[5] = False
        ai = _FakeFilterAI(final_match=True)
        notifier, fake_bot = self._build_notifier(store, ai)

        outcome = await notifier.dispatch_new_job(_sample_card())

        self.assertEqual(fake_bot.sent_messages, [])
        self.assertEqual(outcome.filter_matched_subscribers, 0)
        self.assertEqual(ai.confirm_calls, [])

    def test_message_format_strips_html_tags(self) -> None:
        message = SubscriberNotifier.format_job_message(_project_card())
        self.assertNotIn("<p>", message)
        self.assertIn("Redesign a SaaS landing page and dashboard in Figma.", message)
        self.assertIn("✨ New Project Match", message)
        self.assertIn("💼 Project: Freelance UI Project", message)

    def test_message_format_includes_why_this_matched(self) -> None:
        message = SubscriberNotifier.format_job_message(
            _sample_card(),
            why_matched="Role semantic match; AI check: strong fit",
        )
        self.assertIn("🎯 Matched because:", message)
        self.assertIn("strong fit", message)

    def test_source_label_preserves_domains_starting_with_w(self) -> None:
        self.assertEqual(SubscriberNotifier._source_label("https://wellfound.com/role/ui-ux-designer"), "wellfound.com")
        self.assertEqual(SubscriberNotifier._source_label("https://web3.career/jobs"), "web3.career")

    def test_message_format_localizes_russian_cards(self) -> None:
        message = SubscriberNotifier.format_job_message(
            _russian_card(),
            why_matched="Подходит по роли и месту работы.",
        )
        self.assertIn("✨ Новый подходящий проект", message)
        self.assertIn("💼 Проект: Продуктовый дизайнер", message)
        self.assertIn("🎯 Почему проект вам подходит:", message)

    def test_message_format_localizes_arabic_match_explainer(self) -> None:
        message = SubscriberNotifier.format_job_message(
            _sample_card(),
            why_matched="فيجما + عن بُعد + ميزانية أعلى من 1500 دولار",
            ui_language="ar",
        )
        self.assertIn("مشروع مطابق جديد", message)
        self.assertIn("طابق لأن:", message)
        self.assertIn("فيجما", message)

    async def test_dispatch_ignores_legacy_location_preferences_entirely(self) -> None:
        store = _FakeStore()
        store.active_subscribers = [(6, "fay")]
        store.subscribed_users = {6}
        store.selected_domains[6] = {"https://example.com"}
        store.role_titles[6] = "Backend Engineer"
        store.location_pref[6] = "Remote Global"
        ai = _FakeFilterAI(final_match=True)
        notifier, fake_bot = self._build_notifier(store, ai)

        outcome = await notifier.dispatch_new_job(_sample_card())

        self.assertEqual(len(fake_bot.sent_messages), 1)
        self.assertEqual(outcome.sent_count, 1)
        _, user_filters, prechecks = ai.confirm_calls[0]
        self.assertNotIn("location_rule", user_filters)
        self.assertNotIn("location", prechecks)

    async def test_dispatch_location_preference_updates_do_not_affect_matching(self) -> None:
        store = _FakeStore()
        store.active_subscribers = [(22, "dina")]
        store.subscribed_users = {22}
        store.selected_domains[22] = {"https://example.com"}
        store.role_titles[22] = "Product Designer"
        store.location_pref[22] = "Remote Global"
        ai = _FakeFilterAI(final_match=True)
        notifier, fake_bot = self._build_notifier(store, ai)

        product_card = JobCard(
            website="https://example.com",
            url="https://example.com/jobs/product-designer-cairo-live",
            title="Product Designer",
            description="On-site role in Cairo, Egypt.",
            salary="USD 4000 per month",
            location="Cairo, Egypt",
            is_job_post=True,
            confidence=0.9,
            extraction_method="test",
        )

        first_outcome = await notifier.dispatch_new_job(product_card)
        store.location_pref[22] = "On-site/hybrid within Egypt"
        second_outcome = await notifier.dispatch_new_job(product_card)

        self.assertEqual(first_outcome.sent_count, 1)
        self.assertEqual(second_outcome.sent_count, 1)

    async def test_dispatch_rejects_when_en_source_payment_is_below_minimum(self) -> None:
        store = _FakeStore()
        store.active_subscribers = [(9, "iris")]
        store.subscribed_users = {9}
        store.selected_domains[9] = {"https://example.com"}
        store.role_titles[9] = "Backend Engineer"
        store.project_preferences[9] = {"minimum_payment_usd": 4000}
        ai = _FakeFilterAI(final_match=True)
        notifier, fake_bot = self._build_notifier(store, ai)
        low_budget_card = JobCard(
            website="https://example.com",
            url="https://example.com/project/backend-maintenance",
            title="Backend Maintenance",
            description="Need a backend engineer for small API fixes.",
            salary="USD 2500 fixed price",
            location="Remote",
            is_job_post=True,
            confidence=0.94,
            extraction_method="test",
        )

        outcome = await notifier.dispatch_new_job(low_budget_card)

        self.assertEqual(fake_bot.sent_messages, [])
        self.assertEqual(ai.confirm_calls, [])
        self.assertEqual(outcome.sent_count, 0)

    async def test_dispatch_rejects_when_en_source_payment_is_above_maximum(self) -> None:
        store = _FakeStore()
        store.active_subscribers = [(90, "iris")]
        store.subscribed_users = {90}
        store.selected_domains[90] = {"https://example.com"}
        store.role_titles[90] = "Backend Engineer"
        store.project_preferences[90] = {"maximum_payment_usd": 2000}
        ai = _FakeFilterAI(final_match=True)
        notifier, fake_bot = self._build_notifier(store, ai)

        outcome = await notifier.dispatch_new_job(_sample_card())

        self.assertEqual(fake_bot.sent_messages, [])
        self.assertEqual(ai.confirm_calls, [])
        self.assertEqual(outcome.sent_count, 0)

    async def test_dispatch_rejects_when_payment_is_missing_for_active_range(self) -> None:
        store = _FakeStore()
        store.active_subscribers = [(91, "iris")]
        store.subscribed_users = {91}
        store.selected_domains[91] = {"https://example.com"}
        store.role_titles[91] = "UX/UI Design"
        store.project_preferences[91] = {"minimum_payment_usd": 1000}
        ai = _FakeFilterAI(final_match=True)
        notifier, fake_bot = self._build_notifier(store, ai)
        no_budget_card = JobCard(
            website="https://example.com",
            url="https://example.com/opportunity/hourly-ui",
            title="Hourly UI Designer Support",
            description="Need hourly support for product screens and quick iterations.",
            salary="Unknown",
            location="Remote",
            is_job_post=True,
            confidence=0.94,
            extraction_method="test",
            engagement_type="Hourly contract",
        )

        outcome = await notifier.dispatch_new_job(no_budget_card)

        self.assertEqual(fake_bot.sent_messages, [])
        self.assertEqual(ai.confirm_calls, [])
        self.assertEqual(outcome.sent_count, 0)

    async def test_dispatch_passes_project_preferences_to_ai_confirmation(self) -> None:
        store = _FakeStore()
        store.active_subscribers = [(92, "mira")]
        store.subscribed_users = {92}
        store.selected_domains[92] = {"https://example.com"}
        store.role_titles[92] = "UX/UI Design"
        store.project_preferences[92] = {
            "deliverables": ["landing page redesign", "dashboard redesign"],
            "minimum_payment_usd": 1200,
            "maximum_payment_usd": 3000,
        }
        ai = _FakeFilterAI(final_match=True, final_reason="Strong project fit")
        notifier, fake_bot = self._build_notifier(store, ai)
        project_card = JobCard(
            website="https://example.com",
            url="https://example.com/opportunity/landing-page-redesign",
            title="Landing Page Redesign for SaaS Startup",
            description="Looking for a designer to refresh our landing page and improve the dashboard onboarding flow.",
            salary="USD 1800 fixed price",
            location="Remote",
            is_job_post=True,
            confidence=0.97,
            extraction_method="test",
            engagement_type="Project",
            duration="3 weeks",
            industry="SaaS",
        )

        outcome = await notifier.dispatch_new_job(project_card)

        self.assertEqual(len(fake_bot.sent_messages), 1)
        self.assertEqual(outcome.sent_count, 1)
        _, user_filters, prechecks = ai.confirm_calls[0]
        self.assertEqual(user_filters["deliverables"], ["landing page redesign", "dashboard redesign"])
        self.assertEqual(user_filters["minimum_payment_usd"], 1200.0)
        self.assertEqual(user_filters["maximum_payment_usd"], 3000.0)
        self.assertEqual(user_filters["source_currency"], "USD")
        self.assertTrue(prechecks["payment_model"]["matched"])

    async def test_dispatch_uses_rub_payment_range_for_ru_sources(self) -> None:
        store = _FakeStore()
        store.active_subscribers = [(94, "lina")]
        store.subscribed_users = {94}
        store.selected_domains[94] = {"https://workspace.ru/web-design/"}
        store.role_titles[94] = "UX/UI Designer"
        store.project_preferences[94] = {
            "minimum_payment_rub": 180000,
            "maximum_payment_rub": 250000,
        }
        ai = _FakeFilterAI(final_match=True)
        notifier, fake_bot = self._build_notifier(store, ai)

        outcome = await notifier.dispatch_new_job(_russian_card())

        self.assertEqual(len(fake_bot.sent_messages), 1)
        self.assertEqual(outcome.sent_count, 1)
        _, user_filters, prechecks = ai.confirm_calls[0]
        self.assertEqual(user_filters["source_currency"], "RUB")
        self.assertEqual(user_filters["minimum_payment_rub"], 180000.0)
        self.assertEqual(user_filters["maximum_payment_rub"], 250000.0)
        self.assertTrue(prechecks["payment_model"]["matched"])

    async def test_dispatch_rejects_when_ru_source_payment_is_above_maximum(self) -> None:
        store = _FakeStore()
        store.active_subscribers = [(95, "lina")]
        store.subscribed_users = {95}
        store.selected_domains[95] = {"https://workspace.ru/web-design/"}
        store.role_titles[95] = "UX/UI Designer"
        store.project_preferences[95] = {"maximum_payment_rub": 180000}
        ai = _FakeFilterAI(final_match=True)
        notifier, fake_bot = self._build_notifier(store, ai)
        expensive_ru_card = JobCard(
            website="https://workspace.ru/web-design/",
            url="https://workspace.ru/tenders/krupnyy-dizayn-proekt-987654",
            title="Старший продуктовый дизайнер",
            description="Нужен дизайнер для большого продукта.",
            salary="250000 RUB",
            location="Москва, Россия",
            is_job_post=True,
            confidence=0.95,
            extraction_method="test",
            company="Яндекс",
            language="ru",
        )

        outcome = await notifier.dispatch_new_job(expensive_ru_card)

        self.assertEqual(fake_bot.sent_messages, [])
        self.assertEqual(ai.confirm_calls, [])
        self.assertEqual(outcome.sent_count, 0)

    async def test_dispatch_uses_saved_currency_for_custom_sources(self) -> None:
        store = _FakeStore()
        store.active_subscribers = [(96, "mira")]
        store.subscribed_users = {96}
        custom_url = "https://custom.example/briefs"
        store.selected_domains[96] = {custom_url}
        store.website_currencies[(96, custom_url)] = "RUB"
        store.role_titles[96] = "UX/UI Designer"
        store.project_preferences[96] = {"minimum_payment_rub": 120000}
        ai = _FakeFilterAI(final_match=True)
        notifier, fake_bot = self._build_notifier(store, ai)
        custom_card = JobCard(
            website=custom_url,
            url="https://custom.example/briefs/landing-redesign",
            title="Landing page redesign",
            description="Нужен UX/UI дизайнер для обновления лендинга.",
            salary="110000 RUB",
            location="Удалённо",
            is_job_post=True,
            confidence=0.93,
            extraction_method="test",
            language="ru",
        )

        outcome = await notifier.dispatch_new_job(custom_card)

        self.assertEqual(fake_bot.sent_messages, [])
        self.assertEqual(ai.confirm_calls, [])
        self.assertEqual(outcome.sent_count, 0)

    async def test_dispatch_uses_ui_language_for_user_message_wrapper_and_ai_output_language(self) -> None:
        store = _FakeStore()
        store.active_subscribers = [(93, "lina")]
        store.subscribed_users = {93}
        store.selected_domains[93] = {"https://example.com"}
        store.role_titles[93] = "UX/UI Designer"
        store.ui_languages[93] = "ru"
        ai = _FakeFilterAI(final_match=True, final_reason="Проект подходит под ваши фильтры поиска.")
        notifier, fake_bot = self._build_notifier(store, ai)

        outcome = await notifier.dispatch_new_job(_project_card())

        self.assertEqual(outcome.sent_count, 1)
        self.assertEqual(len(fake_bot.sent_messages), 1)
        self.assertIn("Новый подходящий проект", fake_bot.sent_messages[0][1])
        self.assertIn("Почему проект вам подходит:", fake_bot.sent_messages[0][1])
        _, user_filters, _ = ai.confirm_calls[0]
        self.assertEqual(user_filters["output_language"], "ru")

    async def test_dispatch_skips_send_when_role_is_missing(self) -> None:
        store = _FakeStore()
        store.active_subscribers = [(10, "jade")]
        store.subscribed_users = {10}
        store.selected_domains[10] = {"https://example.com"}
        store.keywords[10] = ["python"]
        ai = _FakeFilterAI(final_match=True)
        notifier, fake_bot = self._build_notifier(store, ai)

        outcome = await notifier.dispatch_new_job(_sample_card())

        self.assertEqual(fake_bot.sent_messages, [])
        self.assertEqual(outcome.active_subscribers, 0)
        self.assertEqual(ai.confirm_calls, [])

    async def test_dispatch_queues_when_quiet_hours_block_send(self) -> None:
        store = _FakeStore()
        store.active_subscribers = [(10, "jade")]
        store.subscribed_users = {10}
        store.selected_domains[10] = {"https://example.com"}
        store.role_titles[10] = "Backend Engineer"
        store.should_deliver[10] = False
        store.next_allowed[10] = "2030-01-01T08:00:00Z"
        ai = _FakeFilterAI(final_match=True, final_reason="role match")
        notifier, fake_bot = self._build_notifier(store, ai)

        outcome = await notifier.dispatch_new_job(_sample_card())

        self.assertEqual(len(fake_bot.sent_messages), 1)
        self.assertEqual(outcome.sent_count, 1)
        self.assertEqual(outcome.queued_count, 0)
        self.assertEqual(store.queued_notifications, [])

    async def test_dispatch_digest_mode_queues_for_digest_window(self) -> None:
        store = _FakeStore()
        store.active_subscribers = [(14, "nora")]
        store.subscribed_users = {14}
        store.selected_domains[14] = {"https://example.com"}
        store.role_titles[14] = "Backend Engineer"
        store.delivery_mode[14] = "digest"
        store.next_digest[14] = "2030-01-01T06:00:00Z"
        ai = _FakeFilterAI(final_match=True, final_reason="role match")
        notifier, fake_bot = self._build_notifier(store, ai)

        outcome = await notifier.dispatch_new_job(_sample_card())

        self.assertEqual(fake_bot.sent_messages, [])
        self.assertEqual(outcome.sent_count, 0)
        self.assertEqual(outcome.queued_count, 1)
        self.assertEqual(len(store.queued_notifications), 1)

    async def test_dispatch_passes_feedback_guidance_to_ai_confirmation(self) -> None:
        store = _FakeStore()
        store.active_subscribers = [(15, "omar")]
        store.subscribed_users = {15}
        store.selected_domains[15] = {"https://example.com"}
        store.role_titles[15] = "Backend Engineer"
        store.feedback_guidance[15] = "LIKED | title=Backend Engineer | why=Role match"
        ai = _FakeFilterAI(final_match=True, final_reason="semantic sphere match")
        notifier, fake_bot = self._build_notifier(store, ai)

        outcome = await notifier.dispatch_new_job(_sample_card())

        self.assertEqual(len(fake_bot.sent_messages), 1)
        self.assertEqual(outcome.sent_count, 1)
        _, user_filters, _ = ai.confirm_calls[0]
        self.assertIn("LIKED | title=Backend Engineer", user_filters["feedback_guidance"])

    async def test_flush_due_notifications_cancels_stale_website_queue_items(self) -> None:
        store = _FakeStore()
        store.subscribed_users = {20}
        store.role_titles[20] = "Backend Engineer"
        store.selected_domains[20] = {"https://custom.example/jobs"}
        event_id = store.record_delivery_event(
            user_id=20,
            username="raya",
            card_url="https://example.com/jobs/backend-engineer",
            card_title="Backend Engineer",
            card_company="Example",
            card_location="Remote",
            card_website="https://example.com",
            match_reason="role match",
        )
        store.queue_notification(
            user_id=20,
            username="raya",
            card_url="https://example.com/jobs/backend-engineer",
            message_text="queued message",
            available_after_utc="2000-01-01T00:00:00Z",
            delivery_event_id=event_id,
        )
        notifier, fake_bot = self._build_notifier(store, _FakeFilterAI(final_match=True))

        await notifier.flush_due_notifications(limit=10)

        self.assertEqual(fake_bot.sent_messages, [])
        self.assertEqual(store.cancelled_notifications, [(1, "website_filter_changed")])
        self.assertEqual(store.sent_queue_ids, [])

    async def test_flush_due_notifications_persists_sent_message_metadata(self) -> None:
        store = _FakeStore()
        store.subscribed_users = {20}
        store.selected_domains[20] = {"https://example.com/projects"}
        store.role_titles[20] = "UX/UI Designer"
        store.notification_preferences[20] = True
        store.should_deliver[20] = True
        store.delivery_mode[20] = "instant"
        event_id = store.record_delivery_event(
            user_id=20,
            username="raya",
            card_url="https://example.com/projects/dashboard-redesign",
            card_title="Dashboard Redesign",
            card_company="Acme",
            card_location="Remote",
            card_website="https://example.com/projects",
            match_reason="deliverable match",
        )
        store.queue_notification(
            user_id=20,
            username="raya",
            card_url="https://example.com/projects/dashboard-redesign",
            message_text="queued project message",
            available_after_utc="2000-01-01T00:00:00Z",
            delivery_event_id=event_id,
        )
        notifier, fake_bot = self._build_notifier(store, _FakeFilterAI(final_match=True))

        await notifier.flush_due_notifications(limit=10)

        self.assertEqual(len(fake_bot.sent_messages), 1)
        self.assertEqual(store.sent_queue_ids, [1])
        self.assertEqual(store.delivery_events[event_id]["telegram_chat_id"], 20)
        self.assertEqual(store.delivery_events[event_id]["telegram_message_id"], 1)

    async def test_flush_due_notifications_localizes_digest_header_for_russian_ui(self) -> None:
        store = _FakeStore()
        store.subscribed_users = {30}
        store.selected_domains[30] = {"https://example.com/projects"}
        store.role_titles[30] = "UX/UI Designer"
        store.notification_preferences[30] = True
        store.delivery_mode[30] = "digest"
        store.ui_languages[30] = "ru"
        event_id = store.record_delivery_event(
            user_id=30,
            username="nina",
            card_url="https://example.com/projects/mobile-app-redesign",
            card_title="Mobile App Redesign",
            card_company="Acme",
            card_location="Remote",
            card_website="https://example.com/projects",
            match_reason="role match",
        )
        store.queue_notification(
            user_id=30,
            username="nina",
            card_url="https://example.com/projects/mobile-app-redesign",
            message_text="queued project message",
            available_after_utc="2000-01-01T00:00:00Z",
            delivery_event_id=event_id,
        )
        notifier, fake_bot = self._build_notifier(store, _FakeFilterAI(final_match=True))

        await notifier.flush_due_notifications(limit=10)

        self.assertEqual(len(fake_bot.sent_messages), 2)
        self.assertIn("Дайджест ZapLance", fake_bot.sent_messages[0][1])
        self.assertIn("новых подходящих проектов", fake_bot.sent_messages[0][1])

    async def test_dispatch_role_match_ignores_topic_words_in_source_urls(self) -> None:
        store = _FakeStore()
        store.active_subscribers = [(21, "sara")]
        store.subscribed_users = {21}
        store.selected_domains[21] = {"https://uiuxjobsboard.com/design-jobs"}
        store.role_titles[21] = "UX/UI Designer"
        notifier, fake_bot = self._build_notifier(store, _FakeFilterAI(final_match=True))
        notifier.filter_ai = FilterAI(
            openai_api_key="",
            openai_model="gpt-4o-mini",
            logger=logging.getLogger("test-subscriber-notifier-real-filter-ai"),
        )

        front_end_card = JobCard(
            website="https://uiuxjobsboard.com/design-jobs",
            url="https://uiuxjobsboard.com/jobs/frontend-react-engineer",
            title="Front-End Developer",
            description="Build React applications and shared components for a web app.",
            salary="Unknown",
            location="Remote",
            is_job_post=True,
            confidence=0.94,
            extraction_method="test",
        )

        outcome = await notifier.dispatch_new_job(front_end_card)

        self.assertEqual(fake_bot.sent_messages, [])
        self.assertEqual(outcome.filter_matched_subscribers, 0)
        self.assertEqual(outcome.sent_count, 0)


if __name__ == "__main__":
    unittest.main()
