from __future__ import annotations

import asyncio
import logging
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

_scrapling_stub = types.ModuleType("scrapling")
_scrapling_stub.Selector = object
_scrapling_stub.DynamicFetcher = object
_scrapling_stub.Fetcher = object
_scrapling_stub.StealthyFetcher = object
sys.modules.setdefault("scrapling", _scrapling_stub)

from job_bot.config import Settings
from job_bot.coordinator import JobBotCoordinator
from job_bot.models import CycleStats
from job_bot.websites_refresh import request_websites_refresh


class _FakeStateStore:
    def __init__(self) -> None:
        self.runtime_metrics: dict[str, float] = {}
        self.health_payload: dict[str, object] | None = None
        self.blocked_sites: dict[str, object] = {}

    def increment_runtime_metric(self, metric_key: str, amount: float = 1.0) -> None:
        self.runtime_metrics[metric_key] = self.runtime_metrics.get(metric_key, 0.0) + float(amount)

    def set_runtime_metric(self, metric_key: str, value: float) -> None:
        self.runtime_metrics[metric_key] = float(value)

    def get_runtime_metric(self, metric_key: str, default: float = 0.0) -> float:
        return float(self.runtime_metrics.get(metric_key, default))

    def set_crawler_health(self, *, severity: str, state: str, reason: str, details=None) -> None:
        self.health_payload = {
            "severity": severity,
            "state": state,
            "reason": reason,
            "details": dict(details or {}),
        }

    def summarize_recent_cycle_window(self, *, since_utc: str) -> dict[str, float]:
        del since_utc
        return {"cycles": 1.0, "discovered_links": 1.0, "new_cards": 1.0, "errors": 0.0}

    def list_temporarily_blocked_sites(
        self,
        websites: list[str],
        *,
        cooldown_seconds: int = 1800,
        challenge_threshold: int = 2,
    ) -> dict[str, object]:
        del cooldown_seconds, challenge_threshold
        return {website: self.blocked_sites[website] for website in websites if website in self.blocked_sites}

    def should_send_admin_alert(self, alert_key: str, *, suppress_for_seconds: int = 900) -> bool:
        del alert_key, suppress_for_seconds
        return False

    def mark_admin_alert_sent(self, alert_key: str, *, alert_kind: str, alert_message: str) -> None:
        del alert_key, alert_kind, alert_message
        return

    def close(self) -> None:
        return


class _FakeWriter:
    pass


class _FakeFetcher:
    pass


class _FakeAIClient:
    pass


class _FakeSubscriberNotifier:
    pass


class _FakeAgent:
    def __init__(self, **kwargs) -> None:
        self.website = kwargs["website"]


class _SlowAgent:
    def __init__(self, website: str) -> None:
        self.website = website
        self.cancelled = False

    async def run_cycle(self, cycle_utc: str, budget) -> None:
        try:
            await asyncio.sleep(60)
        finally:
            self.cancelled = True


class _ProgressAgent:
    def __init__(self, website: str) -> None:
        self.website = website

    async def run_cycle(self, cycle_utc: str, budget) -> str:
        import time

        for _ in range(4):
            self._cycle_progress_at = time.monotonic()
            self._cycle_progress_note = f"heartbeat {self.website}"
            await asyncio.sleep(0.05)
        return self.website


class _HardTimeoutAgent:
    def __init__(self, website: str) -> None:
        self.website = website

    async def run_cycle(self, cycle_utc: str, budget) -> None:
        import time

        started = time.monotonic()
        while time.monotonic() - started < 3.0:
            self._cycle_progress_at = time.monotonic()
            self._cycle_progress_note = f"still active {self.website}"
            self._cycle_busy_until = time.monotonic() + 0.2
            await asyncio.sleep(0.05)


class _ConcurrencyTrackingAgent:
    def __init__(self, website: str, tracker: dict[str, int]) -> None:
        self.website = website
        self._tracker = tracker

    async def run_cycle(self, cycle_utc: str, budget) -> str:
        self._tracker["current"] += 1
        self._tracker["max"] = max(self._tracker["max"], self._tracker["current"])
        try:
            await asyncio.sleep(0.02)
            return self.website
        finally:
            self._tracker["current"] -= 1


class CoordinatorRefreshTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self.temp_path = Path(self._temp_dir.name)
        self.websites_file_path = self.temp_path / "websites.txt"
        self.websites_file_path.write_text(
            "https://builtin.example/jobs\nhttps://ignored.example/jobs\n",
            encoding="utf-8",
        )
        self.settings = Settings(
            openai_api_key="test-key",
            openai_model="gpt-5-mini",
            websites=["https://builtin.example/jobs", "https://ignored.example/jobs"],
            agent_count=1,
            cycle_seconds=60,
            max_cards_per_cycle=3,
            seen_streak_stop=3,
            max_candidates_per_site=10,
            max_seed_pages_per_site=2,
            state_db_path=self.temp_path / "job_bot_state.db",
            output_file_path=self.temp_path / "job_cards.txt",
            headless_browser=True,
            log_level="INFO",
            request_timeout_ms=1000,
            verify_ssl=True,
            telegram_bot_token="token",
            telegram_chat_id="",
            telegram_subs_db_path=self.temp_path / "telegram_subscriptions.db",
            telegram_user_log_dir=self.temp_path / "user_logs",
            websites_file_path=self.websites_file_path,
            neglect_post_if_filters_miss=False,
            enable_human_review_queue=True,
            human_review_confidence_threshold=0.62,
        )

    def tearDown(self) -> None:
        self._temp_dir.cleanup()

    def _build_coordinator(self) -> JobBotCoordinator:
        with (
            patch("job_bot.coordinator.StateStore", return_value=_FakeStateStore()),
            patch("job_bot.coordinator.CardWriter", return_value=_FakeWriter()),
            patch("job_bot.coordinator.ResilientFetcher", return_value=_FakeFetcher()),
            patch("job_bot.coordinator.AIClient", return_value=_FakeAIClient()),
            patch("job_bot.coordinator.SubscriberNotifier", return_value=_FakeSubscriberNotifier()),
            patch("job_bot.coordinator.SiteAgent", side_effect=lambda **kwargs: _FakeAgent(**kwargs)),
        ):
            coordinator = JobBotCoordinator(self.settings, logging.getLogger("test-coordinator"))
        return coordinator

    def test_refresh_agents_ignores_configured_sites_until_user_selects_them(self) -> None:
        coordinator = self._build_coordinator()
        assert coordinator.subscription_store is not None

        coordinator.subscription_store.upsert_trial_subscription(
            user_id=7000,
            username="active-user",
            started_at_utc="2026-03-01T00:00:00Z",
            ends_at_utc="2099-03-01T00:00:00Z",
        )
        coordinator.subscription_store.add_user_website(7000, "https://builtin.example/jobs")
        coordinator._refresh_agents(force=True)

        self.assertEqual(coordinator.active_websites, ["https://builtin.example/jobs"])
        self.assertEqual([agent.website for agent in coordinator.agents], ["https://builtin.example/jobs"])

        coordinator.subscription_store.close()

    def test_refresh_agents_activates_custom_sites_when_selected_by_an_active_user(self) -> None:
        coordinator = self._build_coordinator()
        assert coordinator.subscription_store is not None

        coordinator.subscription_store.upsert_trial_subscription(
            user_id=7001,
            username="active-user",
            started_at_utc="2026-03-01T00:00:00Z",
            ends_at_utc="2099-03-01T00:00:00Z",
        )
        coordinator.subscription_store.add_user_website(7001, "https://custom.example/jobs")
        coordinator._refresh_agents(force=True)

        self.assertEqual(
            coordinator.active_websites,
            ["https://custom.example/jobs"],
        )
        self.assertEqual(
            [agent.website for agent in coordinator.agents],
            ["https://custom.example/jobs"],
        )

        coordinator.subscription_store.close()

    def test_refresh_agents_prioritizes_websites_selected_by_more_active_users(self) -> None:
        coordinator = self._build_coordinator()
        assert coordinator.subscription_store is not None

        coordinator.subscription_store.upsert_trial_subscription(
            user_id=7010,
            username="user-a",
            started_at_utc="2026-03-01T00:00:00Z",
            ends_at_utc="2099-03-01T00:00:00Z",
        )
        coordinator.subscription_store.upsert_trial_subscription(
            user_id=7011,
            username="user-b",
            started_at_utc="2026-03-01T00:00:00Z",
            ends_at_utc="2099-03-01T00:00:00Z",
        )
        coordinator.subscription_store.add_user_website(7010, "https://custom.example/jobs")
        coordinator.subscription_store.add_user_website(7010, "https://builtin.example/jobs")
        coordinator.subscription_store.add_user_website(7011, "https://builtin.example/jobs")

        coordinator._refresh_agents(force=True)

        self.assertEqual(
            coordinator.active_websites,
            ["https://builtin.example/jobs", "https://custom.example/jobs"],
        )
        coordinator.subscription_store.close()

    def test_refresh_agents_ignores_inactive_user_selected_sites(self) -> None:
        coordinator = self._build_coordinator()
        assert coordinator.subscription_store is not None

        coordinator.subscription_store.upsert_trial_subscription(
            user_id=7002,
            username="active-user",
            started_at_utc="2026-03-01T00:00:00Z",
            ends_at_utc="2099-03-01T00:00:00Z",
        )
        coordinator.subscription_store.upsert_trial_subscription(
            user_id=7003,
            username="expired-user",
            started_at_utc="2020-01-01T00:00:00Z",
            ends_at_utc="2020-01-03T00:00:00Z",
        )
        coordinator.subscription_store.add_user_website(7002, "https://active.example/jobs")
        coordinator.subscription_store.add_user_website(7003, "https://inactive.example/jobs")

        coordinator._refresh_agents(force=True)

        self.assertEqual(
            coordinator.active_websites,
            ["https://active.example/jobs"],
        )
        self.assertEqual(
            [agent.website for agent in coordinator.agents],
            ["https://active.example/jobs"],
        )

        coordinator.subscription_store.close()

    def test_refresh_agents_returns_no_sites_when_no_user_selected_anything(self) -> None:
        coordinator = self._build_coordinator()
        assert coordinator.subscription_store is not None

        coordinator.subscription_store.upsert_trial_subscription(
            user_id=7004,
            username="active-user",
            started_at_utc="2026-03-01T00:00:00Z",
            ends_at_utc="2099-03-01T00:00:00Z",
        )
        coordinator._refresh_agents(force=True)

        self.assertEqual(coordinator.active_websites, [])
        self.assertEqual([agent.website for agent in coordinator.agents], [])

        coordinator.subscription_store.close()

    def test_refresh_agents_temporarily_pauses_repeatedly_blocked_websites(self) -> None:
        coordinator = self._build_coordinator()
        assert coordinator.subscription_store is not None
        coordinator.subscription_store.upsert_trial_subscription(
            user_id=7006,
            username="active-user",
            started_at_utc="2026-03-01T00:00:00Z",
            ends_at_utc="2099-03-01T00:00:00Z",
        )
        coordinator.subscription_store.add_user_website(7006, "https://blocked.example/jobs")
        coordinator.subscription_store.add_user_website(7006, "https://builtin.example/jobs")
        assert isinstance(coordinator.store, _FakeStateStore)
        coordinator.store.blocked_sites["https://blocked.example/jobs"] = type(
            "Cooldown",
            (),
            {"blocked_until_utc": "2099-03-01T01:00:00Z"},
        )()

        coordinator._refresh_agents(force=True)

        self.assertEqual(coordinator.active_websites, ["https://builtin.example/jobs"])
        self.assertIn("temporarily paused", coordinator._website_resolution_reason)
        coordinator.subscription_store.close()

    def test_refresh_agents_normalizes_tracking_params_in_runtime_websites(self) -> None:
        noisy_url = "https://www.indeed.com/q-ux-designer-jobs.html?utm_source=chatgpt.com&sort=date&vjk=abc123"
        self.websites_file_path.write_text(f"{noisy_url}\n", encoding="utf-8")
        self.settings.websites = [noisy_url]

        coordinator = self._build_coordinator()
        assert coordinator.subscription_store is not None

        coordinator.subscription_store.upsert_trial_subscription(
            user_id=7005,
            username="active-user",
            started_at_utc="2026-03-01T00:00:00Z",
            ends_at_utc="2099-03-01T00:00:00Z",
        )
        coordinator.subscription_store.add_user_website(7005, noisy_url)
        coordinator._refresh_agents(force=True)

        self.assertEqual(
            coordinator.active_websites,
            ["https://www.indeed.com/q-ux-designer-jobs.html?sort=date"],
        )
        coordinator.subscription_store.close()

    def test_refresh_agents_returns_no_sites_when_subscriber_store_is_disabled(self) -> None:
        settings = self.settings
        settings.telegram_bot_token = ""

        coordinator = self._build_coordinator()

        self.assertIsNone(coordinator.subscription_store)
        self.assertEqual(coordinator.active_websites, [])
        self.assertEqual([agent.website for agent in coordinator.agents], [])

    def test_coordinator_does_not_enable_legacy_chat_notifier(self) -> None:
        settings = self.settings
        settings.telegram_chat_id = "100000001"

        coordinator = self._build_coordinator()

        self.assertIsNone(coordinator.notifier)
        if coordinator.subscription_store is not None:
            coordinator.subscription_store.close()


class CoordinatorSleepSignalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self.temp_path = Path(self._temp_dir.name)
        websites_file_path = self.temp_path / "websites.txt"
        websites_file_path.write_text("https://builtin.example/jobs\n", encoding="utf-8")
        self.settings = Settings(
            openai_api_key="test-key",
            openai_model="gpt-5-mini",
            websites=["https://builtin.example/jobs"],
            agent_count=1,
            cycle_seconds=60,
            max_cards_per_cycle=3,
            seen_streak_stop=3,
            max_candidates_per_site=10,
            max_seed_pages_per_site=2,
            state_db_path=self.temp_path / "job_bot_state.db",
            output_file_path=self.temp_path / "job_cards.txt",
            headless_browser=True,
            log_level="INFO",
            request_timeout_ms=1000,
            verify_ssl=True,
            telegram_bot_token="token",
            telegram_chat_id="",
            telegram_subs_db_path=self.temp_path / "telegram_subscriptions.db",
            telegram_user_log_dir=self.temp_path / "user_logs",
            websites_file_path=websites_file_path,
            neglect_post_if_filters_miss=False,
            enable_human_review_queue=True,
            human_review_confidence_threshold=0.62,
        )
        with (
            patch("job_bot.coordinator.StateStore", return_value=_FakeStateStore()),
            patch("job_bot.coordinator.CardWriter", return_value=_FakeWriter()),
            patch("job_bot.coordinator.ResilientFetcher", return_value=_FakeFetcher()),
            patch("job_bot.coordinator.AIClient", return_value=_FakeAIClient()),
            patch("job_bot.coordinator.SubscriberNotifier", return_value=_FakeSubscriberNotifier()),
            patch("job_bot.coordinator.SiteAgent", side_effect=lambda **kwargs: _FakeAgent(**kwargs)),
        ):
            self.coordinator = JobBotCoordinator(self.settings, logging.getLogger("test-coordinator"))
        self.coordinator.websites_refresh_signal_path = self.temp_path / "websites.refresh.signal"

    async def asyncTearDown(self) -> None:
        if self.coordinator.subscription_store is not None:
            self.coordinator.subscription_store.close()
        self._temp_dir.cleanup()

    async def test_sleep_until_next_cycle_returns_early_when_refresh_requested(self) -> None:
        request_websites_refresh(self.coordinator.websites_refresh_signal_path)

        started = asyncio.get_running_loop().time()
        refreshed = await self.coordinator._sleep_until_next_cycle(5.0)
        elapsed = asyncio.get_running_loop().time() - started

        self.assertTrue(refreshed)
        self.assertLess(elapsed, 1.0)

    async def test_run_agent_cycle_with_watchdog_cancels_stuck_agent(self) -> None:
        slow_agent = _SlowAgent("https://builtin.example/jobs")
        self.coordinator.settings.site_cycle_idle_timeout_seconds = 1
        self.coordinator.settings.site_cycle_hard_timeout_seconds = 30

        with self.assertRaises(TimeoutError) as exc_info:
            await self.coordinator._run_agent_cycle_with_watchdog(
                slow_agent,
                cycle_utc="2026-04-05T08:00:00+00:00",
                budget=object(),
            )

        self.assertIn("site cycle stage timeout", str(exc_info.exception))
        self.assertIn("https://builtin.example/jobs", str(exc_info.exception))
        self.assertTrue(slow_agent.cancelled)

    async def test_run_agent_cycle_with_watchdog_allows_progressing_agent_to_finish(self) -> None:
        progress_agent = _ProgressAgent("https://builtin.example/jobs")
        self.coordinator.settings.site_cycle_idle_timeout_seconds = 1
        self.coordinator.settings.site_cycle_hard_timeout_seconds = 30

        result = await self.coordinator._run_agent_cycle_with_watchdog(
            progress_agent,
            cycle_utc="2026-04-05T08:00:00+00:00",
            budget=object(),
        )

        self.assertEqual(result, "https://builtin.example/jobs")

    async def test_run_agent_cycle_with_watchdog_enforces_hard_timeout_even_with_progress(self) -> None:
        hard_timeout_agent = _HardTimeoutAgent("https://builtin.example/jobs")
        self.coordinator.settings.site_cycle_idle_timeout_seconds = 1
        self.coordinator.settings.site_cycle_hard_timeout_seconds = 2

        with self.assertRaises(TimeoutError) as exc_info:
            await self.coordinator._run_agent_cycle_with_watchdog(
                hard_timeout_agent,
                cycle_utc="2026-04-05T08:00:00+00:00",
                budget=object(),
            )

        self.assertIn("site cycle hard timeout", str(exc_info.exception))
        self.assertIn("https://builtin.example/jobs", str(exc_info.exception))

    async def test_run_agents_for_cycle_respects_agent_count_concurrency_limit(self) -> None:
        tracker = {"current": 0, "max": 0}
        self.coordinator.settings.agent_count = 1
        self.coordinator.agents = [
            _ConcurrencyTrackingAgent(f"https://example.com/jobs/{index}", tracker)
            for index in range(3)
        ]
        self.coordinator.active_websites = [agent.website for agent in self.coordinator.agents]

        results = await self.coordinator._run_agents_for_cycle(
            cycle_utc="2026-04-05T08:00:00+00:00",
            budget=object(),
        )

        self.assertEqual(results, self.coordinator.active_websites)
        self.assertEqual(tracker["max"], 1)

    async def test_zero_active_websites_sets_critical_health_state(self) -> None:
        assert isinstance(self.coordinator.store, _FakeStateStore)
        self.coordinator.active_websites = []
        self.coordinator.agents = []
        self.coordinator._website_resolution_reason = "No active subscribers selected any websites."

        await self.coordinator._handle_zero_active_sites()

        self.assertIsNotNone(self.coordinator.store.health_payload)
        assert self.coordinator.store.health_payload is not None
        self.assertEqual(self.coordinator.store.health_payload["severity"], "critical")
        self.assertEqual(self.coordinator.store.health_payload["state"], "crawler_inactive")
        self.assertGreater(self.coordinator.store.runtime_metrics.get("zero_site_cycles", 0.0), 0.0)
        self.assertGreater(self.coordinator.store.runtime_metrics.get("zero_site_cycles_consecutive", 0.0), 0.0)


class CoordinatorCycleSummaryTests(unittest.TestCase):
    def test_format_neglect_breakdown_uses_readable_reason_names(self) -> None:
        breakdown = JobBotCoordinator._format_neglect_breakdown(
            {"not_job": 2, "old_post": 1, "filter_mismatch": 3}
        )

        self.assertEqual(breakdown, "old post:1, not job:2, filter mismatch:3")

    def test_aggregate_cycle_totals_includes_discovered_and_neglected_posts(self) -> None:
        totals = JobBotCoordinator._aggregate_cycle_totals(
            [
                CycleStats(
                    website="https://a.example/jobs",
                    discovered_links=12,
                    new_cards=2,
                    neglected_posts=3,
                    neglect_reasons={"old_post": 1, "not_job": 2},
                ),
                RuntimeError("boom"),
                CycleStats(
                    website="https://b.example/jobs",
                    discovered_links=5,
                    new_cards=1,
                    neglected_posts=2,
                    neglect_reasons={"filter_mismatch": 1, "duplicate_cluster": 1},
                ),
            ]
        )

        self.assertEqual(totals["discovered_posts"], 17)
        self.assertEqual(totals["new_cards"], 3)
        self.assertEqual(totals["neglected_posts"], 5)
        self.assertEqual(
            totals["neglect_reasons"],
            {"old_post": 1, "not_job": 2, "filter_mismatch": 1, "duplicate_cluster": 1},
        )


if __name__ == "__main__":
    unittest.main()
