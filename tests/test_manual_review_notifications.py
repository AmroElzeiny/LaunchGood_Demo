from __future__ import annotations

import logging
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from job_bot.models import JobCard
from job_bot.storage import StateStore
from job_bot.subscriber_notifier import DispatchOutcome
from job_bot.telegram_subscription_store import SubscriptionStore
from job_bot.zapcareers_telegram_bot import TelegramMenuBot


class _FakeBot:
    def __init__(self) -> None:
        self.sent_messages: list[dict[str, object]] = []

    async def send_message(self, **kwargs):
        self.sent_messages.append(dict(kwargs))
        return SimpleNamespace(message_id=len(self.sent_messages))


class _LengthLimitedFakeBot(_FakeBot):
    async def send_message(self, **kwargs):
        if len(str(kwargs.get("text", "") or "")) > 4096:
            raise RuntimeError("Message is too long")
        return await super().send_message(**kwargs)


class _FakeSubscriberNotifier:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.dispatch_calls: list[str] = []

    async def deliver_manual_review_case(
        self,
        *,
        user_id: int,
        username: str,
        card,
        match_reason: str,
        force_send: bool = False,
    ) -> tuple[str, int]:
        self.calls.append(
            {
                "user_id": user_id,
                "username": username,
                "card_url": getattr(card, "url", ""),
                "match_reason": match_reason,
                "force_send": force_send,
            }
        )
        return ("sent", 1)

    async def dispatch_new_job(self, card: JobCard) -> DispatchOutcome:
        self.dispatch_calls.append(getattr(card, "url", ""))
        return DispatchOutcome(active_subscribers=1, filter_matched_subscribers=1, sent_count=1, queued_count=0)

    async def _evaluate_user_filters(self, user_id: int, card: JobCard):
        if user_id == 77 and getattr(card, "url", "") == "https://example.com/jobs/product-designer-frontend":
            return SimpleNamespace(matched=True, reason_summary="Matched role, remote, and salary filters.")
        return SimpleNamespace(matched=False, reason_summary="")


class _FakeQuery:
    def __init__(self) -> None:
        self.answers: list[tuple[str, bool]] = []
        self.reply_markup_cleared = False

    async def answer(self, text: str, show_alert: bool = False) -> None:
        self.answers.append((text, show_alert))

    async def edit_message_reply_markup(self, reply_markup=None) -> None:
        self.reply_markup_cleared = reply_markup is None


class _FakeExpiredQuery(_FakeQuery):
    async def answer(self, text: str | None = None, show_alert: bool = False) -> None:
        from telegram.error import BadRequest

        raise BadRequest("Query is too old and response timeout expired or query id is invalid")


class ManualReviewNotificationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = SubscriptionStore(Path(self.temp_dir.name) / "telegram_subscriptions.db")
        self.state_store = StateStore(Path(self.temp_dir.name) / "job_bot_state.db")
        self.fake_bot = _FakeBot()
        self.fake_notifier = _FakeSubscriberNotifier()
        self.bot = TelegramMenuBot.__new__(TelegramMenuBot)
        self.bot.store = self.store
        self.bot.state_store = self.state_store
        self.bot.application = SimpleNamespace(bot=self.fake_bot)
        self.bot.subscriber_notifier = self.fake_notifier
        self.bot.subscription_admin_notifier = SimpleNamespace(admin_chat_id=100000001)
        self.bot.settings = SimpleNamespace(
            telegram_bot_token="token",
            openai_api_key="test-key",
            openai_model="gpt-4o-mini",
            openai_model_filter_match="gpt-4o-mini",
            openai_model_keyword_expansion="gpt-4o-mini",
        )
        self.bot.logger = logging.getLogger("test-manual-review-notifications")
        self.store.upsert_trial_subscription(
            user_id=77,
            username="alice",
            started_at_utc="2026-03-01T00:00:00Z",
            ends_at_utc="2099-03-01T00:00:00Z",
        )
        self.store.set_role_preference(77, "Product Designer")
        self.store.set_location_preference(77, "Remote Global")
        self.store.set_salary_range_preference(77, "USD 3000-5000")
        self.store.add_keywords(77, ["figma", "saas"])
        self.store.replace_user_spheres(77, ["design"])
        self.store.add_user_website(77, "https://example.com/jobs")

    async def asyncTearDown(self) -> None:
        self.store.close()
        self.state_store.close()
        self.temp_dir.cleanup()

    def _queue_case(self) -> int:
        return self.store.queue_manual_review_case(
            user_id=77,
            username="alice",
            card_url="https://example.com/jobs/product-designer-frontend",
            card_website="https://example.com/jobs",
            card_title="Product Designer / Frontend",
            card_company="Acme",
            card_location="Remote",
            card_salary="USD 4000 per month",
            card_language="en",
            card_payload={
                "website": "https://example.com/jobs",
                "url": "https://example.com/jobs/product-designer-frontend",
                "title": "Product Designer / Frontend",
                "description": "Own design and React work.",
                "salary": "USD 4000 per month",
                "budget": "USD 4000 per month",
                "location": "Remote",
                "is_job_post": False,
                "is_relevant_opportunity": True,
                "confidence": 0.71,
                "extraction_method": "test",
                "company": "Acme",
                "client": "Acme",
                "language": "en",
                "scope_summary": "Landing page redesign and lightweight frontend implementation.",
                "engagement_type": "project",
                "duration": "2-4 weeks",
                "skills_required": "Figma, React, design systems",
                "contact_url": "https://example.com/jobs/product-designer-frontend",
            },
            review_kind="role_ambiguity",
            ambiguity_summary="The role mixes design and engineering signals.",
            match_reason="Likely relevant, but the role mixes product design and frontend ownership.",
            role_title="UX/UI Designer",
            location_preference="Remote Global",
            salary_preference="",
            include_no_salary=True,
            keywords=["figma", "saas"],
            context_payload={
                "card_confidence": 0.71,
                "ambiguity_reasons": ["The role mixes design and engineering signals."],
                "precheck_results": {
                    "role": {"matched": True},
                    "location": {"matched": True},
                    "salary_visibility": {"matched": True},
                    "keywords": {"matched": True},
                },
                "project_preferences": {
                    "deliverables": ["landing pages", "design systems"],
                    "minimum_fixed_budget_usd": 1500,
                },
            },
        )

    def _queue_human_review_item(self) -> int:
        return self.state_store.queue_human_review_item(
            JobCard(
                website="https://example.com/jobs",
                url="https://example.com/jobs/product-designer-frontend",
                title="Product Designer",
                description="Own UX flows and collaborate with product.",
                salary="USD 4000 per month",
                location="Remote",
                is_job_post=True,
                confidence=0.41,
                extraction_method="ai-pass-3",
                company="Acme",
                language="en",
                notes="Low confidence because details were sparse.",
            ),
            reason="low_confidence_extraction:0.410<0.620",
            source="ai-pass-3",
        )

    def _queue_non_matching_human_review_item(self) -> int:
        return self.state_store.queue_human_review_item(
            JobCard(
                website="https://example.com/jobs",
                url="https://example.com/jobs/marketing-manager",
                title="Marketing Manager",
                description="Own demand generation and campaign planning.",
                salary="USD 2600 per month",
                location="Cairo",
                is_job_post=True,
                confidence=0.44,
                extraction_method="ai-pass-3",
                company="Acme",
                language="en",
                notes="Likely outside the subscriber role filters.",
            ),
            reason="low_confidence_extraction:0.440<0.620",
            source="ai-pass-3",
        )

    async def test_send_pending_manual_reviews_sends_admin_card_with_buttons(self) -> None:
        review_id = self._queue_case()

        await self.bot._send_pending_manual_reviews(limit=10)

        self.assertEqual(len(self.fake_bot.sent_messages), 1)
        message = self.fake_bot.sent_messages[0]
        self.assertEqual(message["chat_id"], 100000001)
        self.assertIn("Project Review Needed", str(message["text"]))
        self.assertIn("Alert preferences:", str(message["text"]))
        self.assertIn("Deliverables: landing pages, design systems", str(message["text"]))
        self.assertIn("Matches:", str(message["text"]))
        self.assertIn("Mismatches:", str(message["text"]))
        self.assertIn("Product Designer / Frontend", str(message["text"]))
        self.assertIn("Engagement type: project", str(message["text"]))
        self.assertIn("Skills requested: Figma, React, design systems", str(message["text"]))
        markup = message["reply_markup"]
        self.assertIsNotNone(markup)
        labels = [button.text for row in markup.inline_keyboard for button in row]
        self.assertEqual(labels, ["Force Send", "Keep Rejecting", "Resend to AI to Double Check"])
        case = self.store.get_manual_review_case(review_id)
        self.assertIsNotNone(case)
        assert case is not None
        self.assertEqual(case.admin_chat_id, 100000001)
        self.assertEqual(case.admin_message_id, 1)

    async def test_send_pending_manual_reviews_strips_html_tags_from_admin_post(self) -> None:
        self.store.queue_manual_review_case(
            user_id=77,
            username="alice",
            card_url="https://example.com/jobs/html-product-designer",
            card_website="https://example.com/jobs",
            card_title="<b>Product Designer</b>",
            card_company="<i>Acme</i>",
            card_location="Remote",
            card_salary="<strong>USD 4000 per month</strong>",
            card_language="en",
            card_payload={
                "website": "https://example.com/jobs",
                "url": "https://example.com/jobs/html-product-designer",
                "title": "<b>Product Designer</b>",
                "description": "<p>Build UX flows and deliver wireframes.</p>",
                "salary": "<strong>USD 4000 per month</strong>",
                "location": "Remote",
                "is_job_post": False,
                "is_relevant_opportunity": True,
                "confidence": 0.74,
                "extraction_method": "test",
                "company": "<i>Acme</i>",
                "client": "<i>Acme</i>",
                "language": "en",
                "scope_summary": "<div>Build UX flows and deliver wireframes.</div>",
                "engagement_type": "project",
                "duration": "2 weeks",
                "skills_required": "<b>Figma</b>, UX research",
                "contact_url": "https://example.com/jobs/html-product-designer",
            },
            review_kind="role_ambiguity",
            ambiguity_summary="<p>Design and frontend signals are mixed.</p>",
            match_reason="<strong>Strong role match for UX/UI filters.</strong>",
            role_title="UX/UI Designer",
            location_preference="Remote Global",
            salary_preference="",
            include_no_salary=True,
            keywords=["figma"],
            context_payload={
                "card_confidence": 0.74,
                "matches": ["<b>Role aligned</b>"],
                "mismatches": ["<i>Frontend overlap</i>"],
                "decision_reason": "<p>Strong role match for UX/UI filters.</p>",
            },
        )

        await self.bot._send_pending_manual_reviews(limit=10)

        text = str(self.fake_bot.sent_messages[0]["text"])
        self.assertNotIn("<b>", text)
        self.assertNotIn("<p>", text)
        self.assertNotIn("<div>", text)
        self.assertIn("Project: Product Designer", text)
        self.assertIn("Client: Acme", text)
        self.assertIn("Scope: Build UX flows and deliver wireframes.", text)
        self.assertIn("Admin rationale: Strong role match for UX/UI filters.", text)

    async def test_send_pending_manual_reviews_shortens_oversized_admin_post_for_telegram(self) -> None:
        huge_text = "Very detailed project note. " * 500
        limited_bot = _LengthLimitedFakeBot()
        self.bot.application = SimpleNamespace(bot=limited_bot)
        review_id = self.store.queue_manual_review_case(
            user_id=77,
            username="alice",
            card_url="https://example.com/jobs/very-long-review",
            card_website="https://example.com/jobs",
            card_title="Senior Product Designer",
            card_company="Acme",
            card_location="Remote",
            card_salary="USD 6000 fixed price",
            card_language="en",
            card_payload={
                "website": "https://example.com/jobs",
                "url": "https://example.com/jobs/very-long-review",
                "title": "Senior Product Designer",
                "description": huge_text,
                "salary": "USD 6000 fixed price",
                "location": "Remote",
                "is_job_post": False,
                "is_relevant_opportunity": True,
                "confidence": 0.83,
                "extraction_method": "test",
                "company": "Acme",
                "client": "Acme",
                "language": "en",
                "scope_summary": huge_text,
                "engagement_type": "project",
                "duration": "8 weeks",
                "skills_required": huge_text,
                "contact_url": "https://example.com/jobs/very-long-review",
            },
            review_kind="project_review",
            ambiguity_summary=huge_text,
            match_reason=huge_text,
            role_title="UX/UI Designer",
            location_preference="Remote Global",
            salary_preference="",
            include_no_salary=True,
            keywords=["figma", "saas"],
            context_payload={
                "card_confidence": 0.83,
                "matches": [huge_text, huge_text, huge_text],
                "mismatches": [huge_text, huge_text, huge_text],
                "decision_reason": huge_text,
                "project_preferences": {
                    "deliverables": [huge_text, huge_text],
                    "minimum_payment_usd": 1500,
                    "maximum_payment_usd": 8000,
                },
            },
        )

        await self.bot._send_pending_manual_reviews(limit=10)

        self.assertEqual(len(limited_bot.sent_messages), 1)
        text = str(limited_bot.sent_messages[0]["text"])
        self.assertLessEqual(len(text), 3900)
        self.assertIn("[Shortened for Telegram]", text)
        case = self.store.get_manual_review_case(review_id)
        self.assertIsNotNone(case)
        assert case is not None
        self.assertEqual(case.admin_message_id, 1)

    async def test_send_pending_manual_reviews_keeps_admin_copy_in_english_for_russian_cases(self) -> None:
        self.store.queue_manual_review_case(
            user_id=77,
            username="alice",
            card_url="https://example.com/projects/russian-brief",
            card_website="https://example.com/projects",
            card_title="Редизайн SaaS интерфейса",
            card_company="Acme",
            card_location="Удаленно",
            card_salary="Бюджет скрыт",
            card_language="ru",
            card_payload={
                "website": "https://example.com/projects",
                "url": "https://example.com/projects/russian-brief",
                "title": "Редизайн SaaS интерфейса",
                "description": "Нужен UX/UI дизайнер для аудита и прототипов.",
                "salary": "Бюджет скрыт",
                "location": "Удаленно",
                "is_job_post": False,
                "is_relevant_opportunity": True,
                "confidence": 0.77,
                "extraction_method": "test",
                "company": "Acme",
                "client": "Acme",
                "language": "ru",
                "scope_summary": "UX аудит, прототипы и переработка ключевых экранов.",
                "engagement_type": "project",
                "duration": "2 недели",
                "skills_required": "Figma, UX research",
                "contact_url": "https://example.com/projects/russian-brief",
            },
            review_kind="project_review",
            ambiguity_summary="Russian project needs admin verification.",
            match_reason="Подходит по фильтрам пользователя.",
            role_title="UX/UI Designer",
            location_preference="Remote Global",
            salary_preference="",
            include_no_salary=True,
            keywords=["figma"],
            context_payload={
                "card_confidence": 0.77,
                "decision_reason": "Matched the saved alert settings.",
                "project_preferences": {
                    "allow_hidden_budget": True,
                    "team_or_agency_ok": False,
                },
            },
        )

        await self.bot._send_pending_manual_reviews(limit=10)

        message = self.fake_bot.sent_messages[0]
        self.assertIn("Project Review Needed", str(message["text"]))
        self.assertIn("Allow Hidden Budget: Yes", str(message["text"]))
        self.assertIn("Team / Agency OK: No", str(message["text"]))
        self.assertIn("Admin rationale: Matched the saved alert settings.", str(message["text"]))

    async def test_accept_manual_review_sends_through_delivery_path_and_marks_case(self) -> None:
        review_id = self._queue_case()
        query = _FakeQuery()

        await self.bot._handle_manual_review_decision(
            context=SimpleNamespace(bot=self.fake_bot),
            chat_id=100000001,
            reviewer_id=100000001,
            reviewer_username="admin",
            callback_data=f"manual_review_force_send_{review_id}",
            action="force_send",
            query=query,
        )

        self.assertEqual(len(self.fake_notifier.calls), 1)
        self.assertTrue(self.fake_notifier.calls[0]["force_send"])
        self.assertTrue(query.reply_markup_cleared)
        self.assertTrue(query.answers)
        case = self.store.get_manual_review_case(review_id)
        self.assertIsNotNone(case)
        assert case is not None
        self.assertEqual(case.status, "approved_sent")
        self.assertEqual(case.sent_count, 1)

    async def test_send_pending_human_reviews_sends_admin_card_with_target_user_and_three_buttons(self) -> None:
        item_id = self._queue_human_review_item()

        await self.bot._send_pending_human_reviews(limit=10)

        self.assertEqual(len(self.fake_bot.sent_messages), 1)
        message = self.fake_bot.sent_messages[0]
        self.assertEqual(message["chat_id"], 100000001)
        self.assertIn("Project Review Needed", str(message["text"]))
        self.assertIn("Product Designer", str(message["text"]))
        self.assertIn("Target subscriber:", str(message["text"]))
        self.assertIn("Username: @alice", str(message["text"]))
        markup = message["reply_markup"]
        self.assertIsNotNone(markup)
        labels = [button.text for row in markup.inline_keyboard for button in row]
        self.assertEqual(labels, ["Open Project", "Approve & Send", "Neglect"])
        item = self.state_store.get_human_review_item(item_id)
        self.assertIsNotNone(item)
        assert item is not None
        self.assertEqual(item.admin_chat_id, 100000001)
        self.assertEqual(item.admin_message_id, 1)
        self.assertGreater(item.confidence, 0.0)

    async def test_send_pending_human_reviews_strips_html_tags_from_admin_post(self) -> None:
        self.state_store.queue_human_review_item(
            JobCard(
                website="https://example.com/jobs",
                url="https://example.com/jobs/product-designer-frontend",
                title="<b>Product Designer</b>",
                description="<p>Own UX flows and collaborate with product.</p>",
                salary="<strong>USD 4000 per month</strong>",
                location="Remote",
                is_job_post=True,
                confidence=0.41,
                extraction_method="ai-pass-3",
                company="<i>Acme</i>",
                language="en",
                notes="<div>Low <strong>confidence</strong> because details were sparse.</div>",
                skills_required="<b>Figma</b>, research",
            ),
            reason="low_confidence_extraction:<b>0.410</b><0.620",
            source="ai-pass-3",
        )

        await self.bot._send_pending_human_reviews(limit=10)

        text = str(self.fake_bot.sent_messages[0]["text"])
        self.assertNotIn("<b>", text)
        self.assertNotIn("<p>", text)
        self.assertNotIn("<div>", text)
        self.assertIn("Project: Product Designer", text)
        self.assertIn("Client: Acme", text)
        self.assertIn("Skills requested: Figma, research", text)
        self.assertIn("Extractor notes: Low confidence because details were sparse.", text)

    async def test_send_pending_human_reviews_still_sends_admin_card_without_matching_subscriber(self) -> None:
        item_id = self._queue_non_matching_human_review_item()

        await self.bot._send_pending_human_reviews(limit=10)

        self.assertEqual(len(self.fake_bot.sent_messages), 1)
        message = self.fake_bot.sent_messages[0]
        self.assertIn("No matching subscriber is currently eligible for auto-send.", str(message["text"]))
        item = self.state_store.get_human_review_item(item_id)
        self.assertIsNotNone(item)
        assert item is not None
        self.assertEqual(item.admin_message_id, 1)
        self.assertEqual(item.last_dispatch_error, "")
        self.assertEqual(self.state_store.list_human_review_items(status="pending", only_undispatched=True, limit=10), [])

    async def test_approve_send_human_review_alerts_when_target_subscriber_is_missing(self) -> None:
        item_id = self._queue_non_matching_human_review_item()
        query = _FakeQuery()

        await self.bot._handle_human_review_decision(
            context=SimpleNamespace(bot=self.fake_bot),
            chat_id=100000001,
            reviewer_id=100000001,
            reviewer_username="admin",
            callback_data=f"human_review_send_{item_id}",
            action="approve_send",
            query=query,
        )

        self.assertFalse(query.reply_markup_cleared)
        self.assertEqual(
            query.answers,
            [("No matching active subscriber is eligible for this post right now.", True)],
        )
        self.assertEqual(self.fake_notifier.dispatch_calls, [])

    async def test_safe_query_answer_ignores_expired_callback_errors(self) -> None:
        await self.bot._safe_query_answer(_FakeExpiredQuery(), "Approved")


if __name__ == "__main__":
    unittest.main()
