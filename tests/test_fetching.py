from __future__ import annotations

import logging
import sys
import time
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

_scrapling_stub = types.ModuleType("scrapling")
_scrapling_stub.DynamicFetcher = object
_scrapling_stub.Fetcher = object
_scrapling_stub.Selector = lambda content="", url="": SimpleNamespace(content=content, url=url)
_scrapling_stub.StealthyFetcher = object
sys.modules.setdefault("scrapling", _scrapling_stub)

_curl_cffi_requests_stub = types.ModuleType("curl_cffi.requests")
_curl_cffi_requests_stub.get = lambda *args, **kwargs: None
_curl_cffi_stub = types.ModuleType("curl_cffi")
_curl_cffi_stub.requests = _curl_cffi_requests_stub
sys.modules.setdefault("curl_cffi", _curl_cffi_stub)
sys.modules.setdefault("curl_cffi.requests", _curl_cffi_requests_stub)

from job_bot.config import Settings
from job_bot.fetching import ResilientFetcher
from job_bot.site_profiles import get_site_profile
from job_bot.zapcareers_telegram_bot import UX_UI_WEBSITE_OPTIONS


def _settings() -> Settings:
    return Settings(
        openai_api_key="test-key",
        openai_model="gpt-5-mini",
        websites=["https://example.com/jobs"],
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
        telegram_subs_db_path=Path("state/subs.db"),
        telegram_user_log_dir=Path("state/user_logs"),
        websites_file_path=Path("sites.txt"),
        neglect_post_if_filters_miss=False,
        enable_human_review_queue=True,
        human_review_confidence_threshold=0.62,
    )


class ResilientFetcherTests(unittest.TestCase):
    def test_falls_back_when_stealthy_returns_cloudflare_challenge(self) -> None:
        settings = _settings()
        fetcher = ResilientFetcher(settings, logging.getLogger("test-fetcher"))

        challenge_response = SimpleNamespace(
            status=403,
            url="https://example.com/cdn-cgi/challenge-platform/h/b",
            html_content="<title>Just a moment...</title><div>Verify you are human</div>",
        )
        good_response = SimpleNamespace(
            status=200,
            url="https://example.com/jobs/backend-engineer",
            html_content="<html><body><h1>Backend Engineer</h1></body></html>",
        )

        class _FakeStealthyFetcher:
            call_kwargs: dict[str, object] | None = None

            @staticmethod
            def fetch(url: str, **kwargs):
                del url
                _FakeStealthyFetcher.call_kwargs = kwargs
                return challenge_response

        class _FakeDynamicFetcher:
            @staticmethod
            def fetch(url: str, **kwargs):
                del url, kwargs
                return good_response

        with (
            patch("job_bot.fetching.StealthyFetcher", _FakeStealthyFetcher),
            patch("job_bot.fetching.DynamicFetcher", _FakeDynamicFetcher),
        ):
            result = fetcher._fetch_sync("https://example.com/jobs")

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.strategy, "dynamic")
        assert _FakeStealthyFetcher.call_kwargs is not None
        self.assertFalse(_FakeStealthyFetcher.call_kwargs["solve_cloudflare"])

    def test_detects_cloudflare_challenge_html(self) -> None:
        self.assertTrue(
            ResilientFetcher._looks_like_cloudflare_challenge(
                "https://example.com/jobs",
                "<html><title>Just a moment...</title><script src='https://challenges.cloudflare.com'></script></html>",
            )
        )
        self.assertFalse(
            ResilientFetcher._looks_like_cloudflare_challenge(
                "https://example.com/jobs/backend-engineer",
                "<html><body>Backend Engineer</body></html>",
            )
        )

    def test_detects_generic_challenge_html(self) -> None:
        self.assertTrue(
            ResilientFetcher._looks_like_cloudflare_challenge(
                "https://jobs.example/search?__cf_chl_rt_tk=test-token",
                (
                    "<html><head><title>Challenge - Example</title></head>"
                    "<body><script>window._cf_chl_opt={};</script>"
                    "<p>Enable JavaScript and cookies to continue</p></body></html>"
                ),
            )
        )

    def test_fetch_sync_remembers_domain_failure_reason_for_challenge_pages(self) -> None:
        settings = _settings()
        settings.enable_browser_tabs = False
        fetcher = ResilientFetcher(settings, logging.getLogger("test-fetcher"))
        blocked_response = SimpleNamespace(
            status=403,
            url="https://jobs.example/search?__cf_chl_rt_tk=test-token",
            html_content=(
                "<html><head><title>Challenge - Example</title></head>"
                "<body><script>window._cf_chl_opt={};</script>"
                "<p>Enable JavaScript and cookies to continue</p></body></html>"
            ),
        )

        class _FakeFetcher:
            @staticmethod
            def get(url: str, **kwargs):
                del url, kwargs
                return blocked_response

        with patch("job_bot.fetching.Fetcher", _FakeFetcher):
            result = fetcher._fetch_sync("https://jobs.example/search")

        self.assertIsNone(result)
        self.assertEqual(
            fetcher.last_failure_reason("https://jobs.example/search"),
            "challenge_or_interstitial",
        )

    def test_detects_generic_protected_interstitial_html(self) -> None:
        self.assertTrue(
            ResilientFetcher._looks_like_cloudflare_challenge(
                "https://www.flexjobs.com/remote-jobs/ux-designer",
                (
                    "<html><body>Powered and protected by Privacy"
                    "<script>window.XMLHttpRequest.prototype.send=function(){location.reload(true)}</script>"
                    "<div id='sec-if-cpt-container'></div></body></html>"
                ),
            )
        )

    def test_uses_curl_cffi_when_other_strategies_only_return_blocking_pages(self) -> None:
        settings = _settings()
        fetcher = ResilientFetcher(settings, logging.getLogger("test-fetcher"))

        blocked_response = SimpleNamespace(
            status=403,
            url="https://example.com/cdn-cgi/challenge-platform/h/b",
            html_content="<title>Just a moment...</title><div>Verify you are human</div>",
        )
        curl_response = SimpleNamespace(
            status_code=200,
            url="https://example.com/jobs/backend-engineer",
            text="<html><body><h1>Backend Engineer</h1></body></html>",
        )

        class _FakeStealthyFetcher:
            @staticmethod
            def fetch(url: str, **kwargs):
                del url, kwargs
                return blocked_response

        class _FakeDynamicFetcher:
            @staticmethod
            def fetch(url: str, **kwargs):
                del url, kwargs
                return blocked_response

        class _FakeFetcher:
            @staticmethod
            def get(url: str, **kwargs):
                del url, kwargs
                return blocked_response

        with (
            patch("job_bot.fetching.StealthyFetcher", _FakeStealthyFetcher),
            patch("job_bot.fetching.DynamicFetcher", _FakeDynamicFetcher),
            patch("job_bot.fetching.Fetcher", _FakeFetcher),
            patch("curl_cffi.requests.get", return_value=curl_response),
        ):
            result = fetcher._fetch_sync("https://example.com/jobs")

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.strategy, "curl_cffi")
        self.assertEqual(result.status, 200)
        self.assertEqual(result.url, "https://example.com/jobs/backend-engineer")

    def test_curl_cffi_uses_lightweight_selector_when_scrapling_selector_is_unavailable(self) -> None:
        settings = _settings()
        fetcher = ResilientFetcher(settings, logging.getLogger("test-fetcher"))

        blocked_response = SimpleNamespace(
            status=403,
            url="https://example.com/cdn-cgi/challenge-platform/h/b",
            html_content="<title>Just a moment...</title><div>Verify you are human</div>",
        )
        curl_response = SimpleNamespace(
            status_code=200,
            url="https://example.com/jobs/backend-engineer",
            text=(
                "<html><body>"
                "<a href='/jobs/backend-engineer'>Apply now</a>"
                "<script type='application/ld+json'>{\"@type\": \"JobPosting\", \"title\": \"Backend Engineer\"}</script>"
                "</body></html>"
            ),
        )
        broken_scrapling = types.ModuleType("scrapling")
        broken_scrapling.Selector = object

        class _FakeStealthyFetcher:
            @staticmethod
            def fetch(url: str, **kwargs):
                del url, kwargs
                return blocked_response

        class _FakeDynamicFetcher:
            @staticmethod
            def fetch(url: str, **kwargs):
                del url, kwargs
                return blocked_response

        class _FakeFetcher:
            @staticmethod
            def get(url: str, **kwargs):
                del url, kwargs
                return blocked_response

        with (
            patch("job_bot.fetching.StealthyFetcher", _FakeStealthyFetcher),
            patch("job_bot.fetching.DynamicFetcher", _FakeDynamicFetcher),
            patch("job_bot.fetching.Fetcher", _FakeFetcher),
            patch("curl_cffi.requests.get", return_value=curl_response),
            patch.dict(sys.modules, {"scrapling": broken_scrapling}),
        ):
            result = fetcher._fetch_sync("https://example.com/jobs")

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.strategy, "curl_cffi")
        self.assertEqual(result.page.css("body ::text").getall(), ["Apply now", '{"@type": "JobPosting", "title": "Backend Engineer"}'])
        self.assertEqual(result.page.css("a")[0].attrib["href"], "/jobs/backend-engineer")
        self.assertEqual(result.page.css("a")[0].css("::text").get(), "Apply now")
        self.assertEqual(
            result.page.css('script[type="application/ld+json"]::text').getall(),
            ['{"@type": "JobPosting", "title": "Backend Engineer"}'],
        )

    def test_disables_browser_backed_strategies_after_broken_pipe(self) -> None:
        settings = _settings()
        fetcher = ResilientFetcher(settings, logging.getLogger("test-fetcher"))
        good_response = SimpleNamespace(
            status=200,
            url="https://example.com/jobs/backend-engineer",
            html_content="<html><body>Backend Engineer</body></html>",
        )

        class _FakeStealthyFetcher:
            call_count = 0

            @staticmethod
            def fetch(url: str, **kwargs):
                del url, kwargs
                _FakeStealthyFetcher.call_count += 1
                raise Exception("Error: EPIPE: broken pipe, write")

        class _FakeDynamicFetcher:
            call_count = 0

            @staticmethod
            def fetch(url: str, **kwargs):
                del url, kwargs
                _FakeDynamicFetcher.call_count += 1
                return good_response

        class _FakeFetcher:
            call_count = 0

            @staticmethod
            def get(url: str, **kwargs):
                del url, kwargs
                _FakeFetcher.call_count += 1
                return good_response

        with (
            patch("job_bot.fetching.StealthyFetcher", _FakeStealthyFetcher),
            patch("job_bot.fetching.DynamicFetcher", _FakeDynamicFetcher),
            patch("job_bot.fetching.Fetcher", _FakeFetcher),
            patch("curl_cffi.requests.get", side_effect=Exception("curl disabled in test")),
        ):
            first_result = fetcher._fetch_sync("https://example.com/jobs")
            second_result = fetcher._fetch_sync("https://example.com/jobs")

        self.assertIsNotNone(first_result)
        self.assertIsNotNone(second_result)
        assert first_result is not None
        assert second_result is not None
        self.assertEqual(first_result.strategy, "static_verified")
        self.assertEqual(second_result.strategy, "static_verified")
        self.assertEqual(_FakeStealthyFetcher.call_count, 1)
        self.assertEqual(_FakeDynamicFetcher.call_count, 0)
        self.assertEqual(_FakeFetcher.call_count, 2)

    def test_retries_rate_limited_strategy_with_backoff(self) -> None:
        settings = _settings()
        settings.enable_browser_tabs = False
        settings.network_retry_budget = 1
        fetcher = ResilientFetcher(settings, logging.getLogger("test-fetcher"))
        responses = [
            SimpleNamespace(status_code=429, url="https://example.com/jobs/backend-engineer", text="Too many requests"),
            SimpleNamespace(
                status_code=200,
                url="https://example.com/jobs/backend-engineer",
                text="<html><body><h1>Backend Engineer</h1></body></html>",
            ),
        ]

        with (
            patch("curl_cffi.requests.get", side_effect=responses),
            patch("job_bot.fetching.time.sleep") as mocked_sleep,
        ):
            result = fetcher._fetch_sync("https://example.com/jobs/backend-engineer")

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.strategy, "curl_cffi")
        mocked_sleep.assert_called_once()

    def test_site_profile_reorders_fetch_strategies_for_fl_ru_projects_feed(self) -> None:
        settings = _settings()
        settings.enable_site_profiles = True
        fetcher = ResilientFetcher(settings, logging.getLogger("test-fetcher"))

        plan = fetcher._strategy_plan(
            "https://www.fl.ru/projects/?kind=1",
            settings.request_timeout_ms,
        )

        self.assertGreaterEqual(len(plan), 3)
        self.assertEqual(plan[0][0], "curl_cffi")

    def test_site_profiles_cover_all_supported_telegram_sources(self) -> None:
        uncovered = [url for _, url in UX_UI_WEBSITE_OPTIONS if get_site_profile(url) is None]
        self.assertEqual(uncovered, [])

    def test_site_profile_reorders_fetch_strategies_for_workatastartup(self) -> None:
        settings = _settings()
        settings.enable_site_profiles = True
        fetcher = ResilientFetcher(settings, logging.getLogger("test-fetcher"))

        plan = fetcher._strategy_plan("https://www.workatastartup.com/jobs", settings.request_timeout_ms)

        self.assertGreaterEqual(len(plan), 3)
        self.assertEqual(plan[0][0], "dynamic")

    def test_fetch_strategy_hard_timeout_aborts_slow_attempt(self) -> None:
        settings = _settings()
        fetcher = ResilientFetcher(settings, logging.getLogger("test-fetcher"))

        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            fetcher._call_strategy_with_timeout(
                lambda: time.sleep(0.2),
                timeout_seconds=0.05,
            )
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.15)


if __name__ == "__main__":
    unittest.main()
