from __future__ import annotations

import json
import logging
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

_scrapling_stub = types.ModuleType("scrapling")
_scrapling_stub.Selector = object
_scrapling_stub.DynamicFetcher = object
_scrapling_stub.Fetcher = object
_scrapling_stub.StealthyFetcher = object
sys.modules.setdefault("scrapling", _scrapling_stub)

from job_bot.ai_client import PostAgeAssessment
from job_bot.agent import SiteAgent
from job_bot.config import Settings
from job_bot.discovery import DiscoveryResult
from job_bot.models import JobCard, LinkCandidate
from job_bot.storage import LinkState
from job_bot.subscriber_notifier import DispatchOutcome


class SiteAgentErrorClassificationTests(unittest.TestCase):
    def test_expected_navigation_error_is_detected(self) -> None:
        exc = Exception("Page.goto: net::ERR_NAME_NOT_RESOLVED at https://placeholder-s2-b.example/")
        self.assertTrue(SiteAgent._is_expected_navigation_error(exc))

    def test_non_navigation_error_is_not_detected(self) -> None:
        exc = Exception("unexpected parsing error while reading response payload")
        self.assertFalse(SiteAgent._is_expected_navigation_error(exc))

    def test_queued_human_review_is_terminal(self) -> None:
        state = LinkState(
            website="https://example.com",
            url="https://example.com/jobs/backend-engineer",
            status="queued_human_review",
            not_job_flags=0,
            neglected=False,
            telegram_sent=False,
        )
        self.assertTrue(SiteAgent._is_terminal_state(state))


class _FakeStore:
    def __init__(self) -> None:
        self.status_updates: list[tuple[str, str, str, bool | None]] = []
        self.saved_urls: list[tuple[str, str]] = []
        self.sent_urls: list[tuple[str, str]] = []
        self.queued_review_items: list[tuple[JobCard, str, str]] = []

    def get_pending_telegram_links(self, website: str, limit: int = 10) -> list[str]:
        return []

    def get_link_state(self, website: str, url: str):
        return None

    def mark_status(self, website: str, url: str, status: str, neglected: bool | None = None) -> None:
        self.status_updates.append((website, url, status, neglected))

    def mark_job_saved(self, website: str, url: str) -> None:
        self.saved_urls.append((website, url))

    def mark_telegram_sent(self, website: str, url: str) -> None:
        self.sent_urls.append((website, url))

    def queue_human_review_item(self, card: JobCard, *, reason: str, source: str) -> int:
        self.queued_review_items.append((card, reason, source))
        return len(self.queued_review_items)


class _FakeWriter:
    def __init__(self) -> None:
        self.write_count = 0

    def write_card(self, cycle_utc: str, agent_name: str, card: JobCard) -> None:
        self.write_count += 1


class _FakeTabManager:
    def __init__(self, *, fetch_results: list[object] | None = None, resolve_result: bool = False) -> None:
        self.fetch_results = list(fetch_results or [])
        self.fetch_calls: list[str] = []
        self.resolve_calls: list[str] = []
        self.resolve_result = resolve_result

    async def start(self, website: str) -> None:
        return

    async def close(self) -> None:
        return

    async def fetch_in_new_tab(self, url: str):
        self.fetch_calls.append(url)
        if self.fetch_results:
            return self.fetch_results.pop(0)
        return type("TabResult", (), {"success": False, "fetch_result": None, "error": "not configured"})()

    async def fetch_card_detail_via_click(self, source_page_url: str, candidate_url: str):
        del source_page_url, candidate_url
        return type("TabResult", (), {"success": False, "fetch_result": None, "error": "not configured"})()

    async def resolve_challenge_manually(self, url: str) -> bool:
        self.resolve_calls.append(url)
        return self.resolve_result


class _FakeFetcher:
    def __init__(self, *, results: list[object | None] | None = None, failure_reason: str = "") -> None:
        self.results = list(results or [])
        self.failure_reason = failure_reason
        self.fetch_calls: list[str] = []

    async def fetch(self, url: str):
        self.fetch_calls.append(url)
        if self.results:
            return self.results.pop(0)
        return None

    def last_failure_reason(self, url: str) -> str:
        del url
        return self.failure_reason


class _FakeAdminNotifier:
    def __init__(self) -> None:
        self.notifications: list[tuple[str, str]] = []

    async def notify_admin_message(self, *, event_key: str, message: str) -> bool:
        self.notifications.append((event_key, message))
        return True


class _FakeBudget:
    def __init__(self, limit: int = 1) -> None:
        self.limit = limit
        self.used = 0

    async def is_exhausted(self) -> bool:
        return self.used >= self.limit

    async def try_take(self) -> bool:
        if self.used >= self.limit:
            return False
        self.used += 1
        return True

    async def release(self, amount: int = 1) -> None:
        self.used = max(0, self.used - max(0, amount))


class _FakeSubscriberNotifier:
    def __init__(self, outcome: DispatchOutcome) -> None:
        self.outcome = outcome

    async def flush_due_notifications(self, limit: int = 100) -> None:
        return

    async def dispatch_new_job(self, card: JobCard) -> DispatchOutcome:
        return self.outcome


class _AgeAwareAIClient:
    async def assess_post_age(
        self,
        *,
        website: str,
        url: str,
        page_text: str,
        html: str,
        max_age_days: int,
        now_utc_iso: str,
    ) -> PostAgeAssessment:
        return PostAgeAssessment(
            older_than_limit=True,
            confidence=0.95,
            detected_posted_at_utc="2020-01-01T00:00:00Z",
            reason="Detected date older than threshold",
        )


class _StructuredFirstAIClient:
    def __init__(self) -> None:
        self.extract_job_card = AsyncMock(return_value=None)


def _tab_result(*, success: bool, fetch_result: object | None = None, error: str = ""):
    return type("TabResult", (), {"success": success, "fetch_result": fetch_result, "error": error})()


def _fetch_result(url: str, *, status: int = 200):
    return type(
        "FakeFetchResult",
        (),
        {
            "url": url,
            "status": status,
            "html": "<html><body>ok</body></html>",
            "page": object(),
            "strategy": "test",
        },
    )()


class SiteAgentExtractorOrderTests(unittest.IsolatedAsyncioTestCase):
    async def test_structured_extractor_runs_before_ai_fallback(self) -> None:
        class _FakeCssResult:
            def __init__(self, values: list[str]) -> None:
                self._values = list(values)

            def getall(self) -> list[str]:
                return list(self._values)

            def get(self) -> str | None:
                return self._values[0] if self._values else None

        class _FakePage:
            def __init__(self, *, scripts: list[str], body_text: list[str]) -> None:
                self._scripts = list(scripts)
                self._body_text = list(body_text)

            def css(self, selector: str) -> _FakeCssResult:
                if selector == 'script[type="application/ld+json"]::text':
                    return _FakeCssResult(self._scripts)
                if selector == "body ::text":
                    return _FakeCssResult(self._body_text)
                if selector == "h1":
                    return _FakeCssResult(["Landing Page Redesign Project"])
                return _FakeCssResult([])

        ai_client = _StructuredFirstAIClient()
        agent = SiteAgent(
            name="agent-structured",
            website="https://example.com",
            settings=SiteAgentFilterNeglectOptionTests._settings(neglect_post_if_filters_miss=False),
            logger=logging.getLogger("test-agent-structured"),
            fetcher=object(),  # type: ignore[arg-type]
            ai_client=ai_client,  # type: ignore[arg-type]
            store=_FakeStore(),  # type: ignore[arg-type]
            writer=_FakeWriter(),  # type: ignore[arg-type]
            notifier=None,
            subscriber_notifier=None,
        )
        payload = {
            "@type": "CreativeWork",
            "name": "Landing Page Redesign Project",
            "description": "Client needs a freelance landing page redesign with a fixed budget and proposal deadline.",
            "provider": {"name": "Acme SaaS"},
            "budget": {"price": "2500", "priceCurrency": "USD"},
            "location": "Remote",
            "serviceType": "project brief",
            "skills": ["Figma", "Landing pages"],
            "url": "https://example.com/projects/landing-page-redesign",
            "datePublished": "2026-04-05",
            "deadline": "2026-04-10",
        }
        post_page = type(
            "FakeFetchResult",
            (),
            {
                "url": "https://example.com/projects/landing-page-redesign",
                "html": '<script type="application/ld+json">' + json.dumps(payload) + "</script>",
                "page": _FakePage(
                    scripts=[json.dumps(payload)],
                    body_text=["Landing page redesign", "Freelance UX/UI designer", "Fixed price budget"],
                ),
            },
        )()

        card = await agent._extract_job_card_from_page(
            "https://example.com/projects/landing-page-redesign",
            post_page,
            page_kind="project_post",
        )

        self.assertIsNotNone(card)
        assert card is not None
        self.assertEqual(card.extraction_method, "local-jsonld-fallback")
        ai_client.extract_job_card.assert_not_awaited()


class SiteAgentFilterNeglectOptionTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _settings(neglect_post_if_filters_miss: bool) -> Settings:
        return Settings(
            openai_api_key="test-key",
            openai_model="gpt-4o-mini",
            websites=["https://example.com"],
            agent_count=1,
            cycle_seconds=60,
            max_cards_per_cycle=3,
            seen_streak_stop=3,
            max_candidates_per_site=10,
            max_seed_pages_per_site=2,
            state_db_path=Path("state/test.db"),
            output_file_path=Path("output/test.txt"),
            headless_browser=True,
            log_level="INFO",
            request_timeout_ms=1000,
            verify_ssl=True,
            telegram_bot_token="",
            telegram_chat_id="",
            telegram_subs_db_path=Path("state/subs_test.db"),
            telegram_user_log_dir=Path("state/user_logs"),
            websites_file_path=Path("websites.txt"),
            neglect_post_if_filters_miss=neglect_post_if_filters_miss,
            enable_human_review_queue=True,
            human_review_confidence_threshold=0.62,
        )

    @staticmethod
    def _job_card() -> JobCard:
        return JobCard(
            website="https://example.com",
            url="https://example.com/jobs/backend-engineer",
            title="Backend Engineer",
            description="Build APIs",
            salary="USD 2000-4000 per month",
            location="Remote",
            is_job_post=True,
            confidence=0.9,
            extraction_method="test",
        )

    @staticmethod
    def _low_confidence_card(confidence: float) -> JobCard:
        return JobCard(
            website="https://example.com",
            url=f"https://example.com/jobs/ux-role-{int(confidence * 1000)}",
            title="UX/UI Designer",
            description="Own wireframes, prototypes, and user flows.",
            salary="USD 2000-4000 per month",
            location="Remote",
            is_job_post=True,
            confidence=confidence,
            extraction_method="test",
        )

    async def test_option_enabled_marks_filter_miss_post_as_neglected(self) -> None:
        store = _FakeStore()
        writer = _FakeWriter()
        subscriber = _FakeSubscriberNotifier(
            DispatchOutcome(active_subscribers=3, filter_matched_subscribers=0, sent_count=0, queued_count=0)
        )
        agent = SiteAgent(
            name="agent-1",
            website="https://example.com",
            settings=self._settings(neglect_post_if_filters_miss=True),
            logger=logging.getLogger("test-agent"),
            fetcher=object(),  # type: ignore[arg-type]
            ai_client=object(),  # type: ignore[arg-type]
            store=store,  # type: ignore[arg-type]
            writer=writer,  # type: ignore[arg-type]
            notifier=None,
            subscriber_notifier=subscriber,  # type: ignore[arg-type]
        )
        agent.tab_manager = _FakeTabManager()  # type: ignore[assignment]
        agent._fetch_post_page = AsyncMock(return_value=object())  # type: ignore[assignment]
        agent._extract_job_card_from_page = AsyncMock(return_value=self._job_card())  # type: ignore[assignment]
        agent._send_pending_notifications = AsyncMock()  # type: ignore[assignment]

        candidate = LinkCandidate(
            url="https://example.com/jobs/backend-engineer",
            anchor_text="Backend Engineer",
            source_page="https://example.com",
            structural_score=10.0,
        )
        budget = _FakeBudget(limit=1)
        with patch(
            "job_bot.agent.discover_candidates",
            AsyncMock(return_value=DiscoveryResult(candidates=[candidate], fetched_pages=1)),
        ):
            stats = await agent.run_cycle(cycle_utc="2026-03-11T00:00:00Z", budget=budget)

        self.assertEqual(stats.new_cards, 0)
        self.assertEqual(writer.write_count, 0)
        self.assertEqual(store.saved_urls, [])
        self.assertEqual(
            store.status_updates[-1],
            ("https://example.com", "https://example.com/jobs/backend-engineer", "neglected_filter_mismatch", True),
        )
        self.assertEqual(stats.neglected_posts, 1)
        self.assertEqual(stats.neglect_reasons, {"filter_mismatch": 1})
        self.assertEqual(budget.used, 0)

    async def test_option_disabled_keeps_saving_post_even_if_filter_miss(self) -> None:
        store = _FakeStore()
        writer = _FakeWriter()
        subscriber = _FakeSubscriberNotifier(
            DispatchOutcome(active_subscribers=3, filter_matched_subscribers=0, sent_count=0, queued_count=0)
        )
        agent = SiteAgent(
            name="agent-1",
            website="https://example.com",
            settings=self._settings(neglect_post_if_filters_miss=False),
            logger=logging.getLogger("test-agent"),
            fetcher=object(),  # type: ignore[arg-type]
            ai_client=object(),  # type: ignore[arg-type]
            store=store,  # type: ignore[arg-type]
            writer=writer,  # type: ignore[arg-type]
            notifier=None,
            subscriber_notifier=subscriber,  # type: ignore[arg-type]
        )
        agent.tab_manager = _FakeTabManager()  # type: ignore[assignment]
        agent._fetch_post_page = AsyncMock(return_value=object())  # type: ignore[assignment]
        agent._extract_job_card_from_page = AsyncMock(return_value=self._job_card())  # type: ignore[assignment]
        agent._send_pending_notifications = AsyncMock()  # type: ignore[assignment]

        candidate = LinkCandidate(
            url="https://example.com/jobs/backend-engineer",
            anchor_text="Backend Engineer",
            source_page="https://example.com",
            structural_score=10.0,
        )
        budget = _FakeBudget(limit=1)
        with patch(
            "job_bot.agent.discover_candidates",
            AsyncMock(return_value=DiscoveryResult(candidates=[candidate], fetched_pages=1)),
        ):
            stats = await agent.run_cycle(cycle_utc="2026-03-11T00:00:00Z", budget=budget)

        self.assertEqual(stats.new_cards, 1)
        self.assertEqual(writer.write_count, 1)
        self.assertEqual(
            store.saved_urls,
            [("https://example.com", "https://example.com/jobs/backend-engineer")],
        )
        self.assertNotIn(
            ("https://example.com", "https://example.com/jobs/backend-engineer", "neglected_filter_mismatch", True),
            store.status_updates,
        )
        self.assertEqual(budget.used, 1)

    async def test_old_post_is_neglected_by_age_filter(self) -> None:
        store = _FakeStore()
        writer = _FakeWriter()
        subscriber = _FakeSubscriberNotifier(
            DispatchOutcome(active_subscribers=1, filter_matched_subscribers=1, sent_count=1, queued_count=0)
        )
        agent = SiteAgent(
            name="agent-1",
            website="https://example.com",
            settings=self._settings(neglect_post_if_filters_miss=False),
            logger=logging.getLogger("test-agent"),
            fetcher=object(),  # type: ignore[arg-type]
            ai_client=_AgeAwareAIClient(),  # type: ignore[arg-type]
            store=store,  # type: ignore[arg-type]
            writer=writer,  # type: ignore[arg-type]
            notifier=None,
            subscriber_notifier=subscriber,  # type: ignore[arg-type]
        )
        agent.tab_manager = _FakeTabManager()  # type: ignore[assignment]
        fake_post_page = type("FakePage", (), {"page": object(), "html": "<html></html>"})()
        agent._fetch_post_page = AsyncMock(return_value=fake_post_page)  # type: ignore[assignment]
        agent._extract_job_card_from_page = AsyncMock(return_value=self._job_card())  # type: ignore[assignment]
        agent._send_pending_notifications = AsyncMock()  # type: ignore[assignment]

        candidate = LinkCandidate(
            url="https://example.com/jobs/backend-engineer",
            anchor_text="Backend Engineer",
            source_page="https://example.com",
            structural_score=10.0,
        )
        budget = _FakeBudget(limit=1)
        with patch(
            "job_bot.agent.discover_candidates",
            AsyncMock(return_value=DiscoveryResult(candidates=[candidate], fetched_pages=1)),
        ):
            stats = await agent.run_cycle(cycle_utc="2026-03-11T00:00:00Z", budget=budget)

        self.assertEqual(stats.new_cards, 0)
        self.assertEqual(writer.write_count, 0)
        self.assertEqual(store.saved_urls, [])
        self.assertIn(
            ("https://example.com", "https://example.com/jobs/backend-engineer", "neglected_old_post", True),
            store.status_updates,
        )
        self.assertEqual(stats.neglected_posts, 1)
        self.assertEqual(stats.neglect_reasons, {"old_post": 1})

    async def test_saved_status_is_recorded_before_writer_output(self) -> None:
        events: list[str] = []

        class _OrderedStore(_FakeStore):
            def mark_job_saved(self, website: str, url: str) -> None:
                events.append("mark_job_saved")
                super().mark_job_saved(website, url)

        class _OrderedWriter(_FakeWriter):
            def write_card(self, cycle_utc: str, agent_name: str, card: JobCard) -> None:
                del cycle_utc, agent_name, card
                events.append("write_card")
                self.write_count += 1

        self_card = self._job_card()
        store = _OrderedStore()
        writer = _OrderedWriter()
        subscriber = _FakeSubscriberNotifier(
            DispatchOutcome(active_subscribers=1, filter_matched_subscribers=1, sent_count=1, queued_count=0)
        )
        agent = SiteAgent(
            name="agent-1",
            website="https://example.com",
            settings=self._settings(neglect_post_if_filters_miss=False),
            logger=logging.getLogger("test-agent"),
            fetcher=object(),  # type: ignore[arg-type]
            ai_client=object(),  # type: ignore[arg-type]
            store=store,  # type: ignore[arg-type]
            writer=writer,  # type: ignore[arg-type]
            notifier=None,
            subscriber_notifier=subscriber,  # type: ignore[arg-type]
        )
        agent.tab_manager = _FakeTabManager()  # type: ignore[assignment]
        fake_post_page = type("FakePage", (), {"page": object(), "html": "<html></html>"})()
        agent._fetch_post_page = AsyncMock(return_value=fake_post_page)  # type: ignore[assignment]
        agent._extract_job_card_from_page = AsyncMock(return_value=self_card)  # type: ignore[assignment]
        agent._send_pending_notifications = AsyncMock()  # type: ignore[assignment]

        candidate = LinkCandidate(
            url="https://example.com/jobs/backend-engineer",
            anchor_text="Backend Engineer",
            source_page="https://example.com",
            structural_score=10.0,
        )
        budget = _FakeBudget(limit=1)
        with patch(
            "job_bot.agent.discover_candidates",
            AsyncMock(return_value=DiscoveryResult(candidates=[candidate], fetched_pages=1)),
        ):
            stats = await agent.run_cycle(cycle_utc="2026-03-11T00:00:00Z", budget=budget)

        self.assertEqual(stats.new_cards, 1)
        self.assertEqual(events[:2], ["mark_job_saved", "write_card"])

    async def test_low_confidence_card_in_review_band_is_queued_for_human_review(self) -> None:
        store = _FakeStore()
        writer = _FakeWriter()
        subscriber = _FakeSubscriberNotifier(
            DispatchOutcome(active_subscribers=1, filter_matched_subscribers=1, sent_count=1, queued_count=0)
        )
        agent = SiteAgent(
            name="agent-1",
            website="https://example.com",
            settings=self._settings(neglect_post_if_filters_miss=False),
            logger=logging.getLogger("test-agent"),
            fetcher=object(),  # type: ignore[arg-type]
            ai_client=object(),  # type: ignore[arg-type]
            store=store,  # type: ignore[arg-type]
            writer=writer,  # type: ignore[arg-type]
            notifier=None,
            subscriber_notifier=subscriber,  # type: ignore[arg-type]
        )
        agent.tab_manager = _FakeTabManager()  # type: ignore[assignment]
        fake_post_page = type("FakePage", (), {"page": object(), "html": "<html></html>"})()
        agent._fetch_post_page = AsyncMock(return_value=fake_post_page)  # type: ignore[assignment]
        agent._extract_job_card_from_page = AsyncMock(return_value=self._low_confidence_card(0.41))  # type: ignore[assignment]
        agent._send_pending_notifications = AsyncMock()  # type: ignore[assignment]

        candidate = LinkCandidate(
            url="https://example.com/jobs/ux-role-410",
            anchor_text="UX/UI Designer",
            source_page="https://example.com",
            structural_score=10.0,
        )
        budget = _FakeBudget(limit=1)
        with patch(
            "job_bot.agent.discover_candidates",
            AsyncMock(return_value=DiscoveryResult(candidates=[candidate], fetched_pages=1)),
        ):
            stats = await agent.run_cycle(cycle_utc="2026-03-11T00:00:00Z", budget=budget)

        self.assertEqual(stats.new_cards, 0)
        self.assertEqual(len(store.queued_review_items), 1)
        self.assertIn(
            ("https://example.com", "https://example.com/jobs/ux-role-410", "queued_human_review", None),
            store.status_updates,
        )
        self.assertEqual(writer.write_count, 0)

    async def test_very_low_confidence_card_is_neglected_instead_of_queued_for_review(self) -> None:
        store = _FakeStore()
        writer = _FakeWriter()
        subscriber = _FakeSubscriberNotifier(
            DispatchOutcome(active_subscribers=1, filter_matched_subscribers=1, sent_count=1, queued_count=0)
        )
        agent = SiteAgent(
            name="agent-1",
            website="https://example.com",
            settings=self._settings(neglect_post_if_filters_miss=False),
            logger=logging.getLogger("test-agent"),
            fetcher=object(),  # type: ignore[arg-type]
            ai_client=object(),  # type: ignore[arg-type]
            store=store,  # type: ignore[arg-type]
            writer=writer,  # type: ignore[arg-type]
            notifier=None,
            subscriber_notifier=subscriber,  # type: ignore[arg-type]
        )
        agent.tab_manager = _FakeTabManager()  # type: ignore[assignment]
        fake_post_page = type("FakePage", (), {"page": object(), "html": "<html></html>"})()
        agent._fetch_post_page = AsyncMock(return_value=fake_post_page)  # type: ignore[assignment]
        agent._extract_job_card_from_page = AsyncMock(return_value=self._low_confidence_card(0.29))  # type: ignore[assignment]
        agent._send_pending_notifications = AsyncMock()  # type: ignore[assignment]

        candidate = LinkCandidate(
            url="https://example.com/jobs/ux-role-290",
            anchor_text="UX/UI Designer",
            source_page="https://example.com",
            structural_score=10.0,
        )
        budget = _FakeBudget(limit=1)
        with patch(
            "job_bot.agent.discover_candidates",
            AsyncMock(return_value=DiscoveryResult(candidates=[candidate], fetched_pages=1)),
        ):
            stats = await agent.run_cycle(cycle_utc="2026-03-11T00:00:00Z", budget=budget)

        self.assertEqual(stats.new_cards, 0)
        self.assertEqual(store.queued_review_items, [])
        self.assertIn(
            ("https://example.com", "https://example.com/jobs/ux-role-290", "neglected_low_confidence", True),
            store.status_updates,
        )
        self.assertEqual(stats.neglected_posts, 1)
        self.assertEqual(stats.neglect_reasons, {"low_confidence": 1})
        self.assertEqual(writer.write_count, 0)

    async def test_search_results_candidate_uses_clicked_card_detail_page(self) -> None:
        store = _FakeStore()
        writer = _FakeWriter()
        subscriber = _FakeSubscriberNotifier(
            DispatchOutcome(active_subscribers=1, filter_matched_subscribers=1, sent_count=1, queued_count=0)
        )
        agent = SiteAgent(
            name="agent-1",
            website="https://example.com",
            settings=self._settings(neglect_post_if_filters_miss=False),
            logger=logging.getLogger("test-agent"),
            fetcher=object(),  # type: ignore[arg-type]
            ai_client=object(),  # type: ignore[arg-type]
            store=store,  # type: ignore[arg-type]
            writer=writer,  # type: ignore[arg-type]
            notifier=None,
            subscriber_notifier=subscriber,  # type: ignore[arg-type]
        )
        tab_manager = _FakeTabManager()
        detail_page = type(
            "FakeFetchResult",
            (),
            {
                "url": "https://example.com/jobs/backend-engineer-123",
                "html": '<script type="application/ld+json">{"@type":"JobPosting"}</script>',
                "page": type("FakeSelector", (), {"css": lambda self, selector: type("FakeCss", (), {"getall": lambda self: ["Apply now", "Job description", "Requirements"]})()})(),
            },
        )()
        tab_manager.fetch_card_detail_via_click = AsyncMock(
            return_value=type("TabResult", (), {"success": True, "fetch_result": detail_page, "error": ""})()
        )
        agent.tab_manager = tab_manager  # type: ignore[assignment]
        search_page = type(
            "FakeFetchResult",
            (),
            {
                "url": "https://example.com/jobs/search?q=backend",
                "html": "<html><body>25 jobs found Sort by newest Filter by location</body></html>",
                "page": type("FakeSelector", (), {"css": lambda self, selector: type("FakeCss", (), {"getall": lambda self: ["25 jobs found", "Sort by newest", "Filter by location"]})()})(),
            },
        )()
        agent._fetch_post_page = AsyncMock(return_value=search_page)  # type: ignore[assignment]
        agent._extract_job_card_from_page = AsyncMock(return_value=SiteAgentFilterNeglectOptionTests._job_card())  # type: ignore[assignment]
        agent._send_pending_notifications = AsyncMock()  # type: ignore[assignment]

        candidate = LinkCandidate(
            url="https://example.com/jobs/backend-engineer-123",
            anchor_text="Backend Engineer",
            source_page="https://example.com/jobs/search?q=backend",
            structural_score=10.0,
        )
        budget = _FakeBudget(limit=1)
        with patch(
            "job_bot.agent.discover_candidates",
            AsyncMock(return_value=DiscoveryResult(candidates=[candidate], fetched_pages=1)),
        ):
            stats = await agent.run_cycle(cycle_utc="2026-03-11T00:00:00Z", budget=budget)

        self.assertEqual(stats.new_cards, 1)
        self.assertEqual(writer.write_count, 1)
        tab_manager.fetch_card_detail_via_click.assert_awaited_once()

    async def test_cycle_checks_unseen_candidate_before_terminal_links(self) -> None:
        class _TerminalAwareStore(_FakeStore):
            def get_link_state(self, website: str, url: str):
                if url in {
                    "https://example.com/jobs/already-saved-1",
                    "https://example.com/jobs/already-saved-2",
                    "https://example.com/jobs/already-saved-3",
                }:
                    return LinkState(
                        website=website,
                        url=url,
                        status="job_saved",
                        not_job_flags=0,
                        neglected=False,
                        telegram_sent=True,
                    )
                return None

        store = _TerminalAwareStore()
        writer = _FakeWriter()
        subscriber = _FakeSubscriberNotifier(
            DispatchOutcome(active_subscribers=1, filter_matched_subscribers=1, sent_count=1, queued_count=0)
        )
        agent = SiteAgent(
            name="agent-1",
            website="https://example.com",
            settings=self._settings(neglect_post_if_filters_miss=False),
            logger=logging.getLogger("test-agent"),
            fetcher=object(),  # type: ignore[arg-type]
            ai_client=object(),  # type: ignore[arg-type]
            store=store,  # type: ignore[arg-type]
            writer=writer,  # type: ignore[arg-type]
            notifier=None,
            subscriber_notifier=subscriber,  # type: ignore[arg-type]
        )
        agent.tab_manager = _FakeTabManager()  # type: ignore[assignment]
        fake_post_page = type("FakePage", (), {"page": object(), "html": "<html></html>"})()
        agent._fetch_post_page = AsyncMock(return_value=fake_post_page)  # type: ignore[assignment]
        agent._extract_job_card_from_page = AsyncMock(return_value=self._job_card())  # type: ignore[assignment]
        agent._send_pending_notifications = AsyncMock()  # type: ignore[assignment]

        candidates = [
            LinkCandidate(
                url="https://example.com/jobs/already-saved-1",
                anchor_text="Saved One",
                source_page="https://example.com",
                structural_score=10.0,
            ),
            LinkCandidate(
                url="https://example.com/jobs/already-saved-2",
                anchor_text="Saved Two",
                source_page="https://example.com",
                structural_score=9.0,
            ),
            LinkCandidate(
                url="https://example.com/jobs/already-saved-3",
                anchor_text="Saved Three",
                source_page="https://example.com",
                structural_score=8.0,
            ),
            LinkCandidate(
                url="https://example.com/jobs/fresh-role",
                anchor_text="Fresh Role",
                source_page="https://example.com",
                structural_score=1.0,
            ),
        ]
        budget = _FakeBudget(limit=1)
        with patch(
            "job_bot.agent.discover_candidates",
            AsyncMock(return_value=DiscoveryResult(candidates=candidates, fetched_pages=1)),
        ):
            stats = await agent.run_cycle(cycle_utc="2026-03-11T00:00:00Z", budget=budget)

        self.assertEqual(stats.checked_links, 1)
        self.assertEqual(stats.new_cards, 1)
        self.assertEqual(writer.write_count, 1)
        self.assertIn(
            ("https://example.com", "https://example.com/jobs/fresh-role", "queued", None),
            store.status_updates,
        )


class SiteAgentManualChallengeTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _settings(*, enable_browser_tabs: bool = True) -> Settings:
        settings = SiteAgentFilterNeglectOptionTests._settings(neglect_post_if_filters_miss=False)
        settings.enable_manual_challenge_flow = True
        settings.manual_challenge_timeout_seconds = 30
        settings.manual_challenge_poll_seconds = 0.01
        settings.enable_browser_tabs = enable_browser_tabs
        return settings

    async def test_discovery_fetch_enters_manual_challenge_flow_and_retries_tab_fetch(self) -> None:
        fetcher = _FakeFetcher()
        admin_notifier = _FakeAdminNotifier()
        tab_manager = _FakeTabManager(
            fetch_results=[
                _tab_result(
                    success=False,
                    fetch_result=_fetch_result("https://example.com/jobs", status=403),
                    error="challenge_or_interstitial",
                ),
                _tab_result(success=True, fetch_result=_fetch_result("https://example.com/jobs")),
            ],
            resolve_result=True,
        )
        agent = SiteAgent(
            name="agent-manual-discovery",
            website="https://example.com",
            settings=self._settings(enable_browser_tabs=True),
            logger=logging.getLogger("test-agent-manual-discovery"),
            fetcher=fetcher,  # type: ignore[arg-type]
            ai_client=object(),  # type: ignore[arg-type]
            store=_FakeStore(),  # type: ignore[arg-type]
            writer=_FakeWriter(),  # type: ignore[arg-type]
            notifier=None,
            subscriber_notifier=None,
            admin_notifier=admin_notifier,  # type: ignore[arg-type]
        )
        agent.tab_manager = tab_manager  # type: ignore[assignment]

        result = await agent._fetch_discovery_page("https://example.com/jobs")

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.url, "https://example.com/jobs")
        self.assertEqual(tab_manager.resolve_calls, ["https://example.com/jobs"])
        self.assertEqual(len(admin_notifier.notifications), 1)
        self.assertEqual(fetcher.fetch_calls, [])

    async def test_fetcher_challenge_enters_manual_flow_even_when_browser_tabs_are_disabled(self) -> None:
        fetcher = _FakeFetcher(results=[None], failure_reason="challenge_or_interstitial")
        admin_notifier = _FakeAdminNotifier()
        tab_manager = _FakeTabManager(
            fetch_results=[_tab_result(success=True, fetch_result=_fetch_result("https://example.com/jobs/detail"))],
            resolve_result=True,
        )
        agent = SiteAgent(
            name="agent-manual-fetcher",
            website="https://example.com",
            settings=self._settings(enable_browser_tabs=False),
            logger=logging.getLogger("test-agent-manual-fetcher"),
            fetcher=fetcher,  # type: ignore[arg-type]
            ai_client=object(),  # type: ignore[arg-type]
            store=_FakeStore(),  # type: ignore[arg-type]
            writer=_FakeWriter(),  # type: ignore[arg-type]
            notifier=None,
            subscriber_notifier=None,
            admin_notifier=admin_notifier,  # type: ignore[arg-type]
        )
        agent.tab_manager = tab_manager  # type: ignore[assignment]

        result = await agent._fetch_post_page("https://example.com/jobs/detail")

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.url, "https://example.com/jobs/detail")
        self.assertEqual(fetcher.fetch_calls, ["https://example.com/jobs/detail"])
        self.assertEqual(tab_manager.resolve_calls, ["https://example.com/jobs/detail"])
        self.assertEqual(tab_manager.fetch_calls, ["https://example.com/jobs/detail"])
        self.assertEqual(len(admin_notifier.notifications), 1)


if __name__ == "__main__":
    unittest.main()
