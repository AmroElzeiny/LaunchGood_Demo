from __future__ import annotations

import logging
import sys
import time
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

from job_bot.browser_tabs import BrowserTabManager
from job_bot.config import Settings


class _FakeKeyboard:
    async def press(self, key: str) -> None:
        del key


class _FakePage:
    def __init__(
        self,
        *,
        main_url: str,
        fail_main_navigation: bool,
        html_sequence: list[str] | None = None,
    ) -> None:
        self.main_url = main_url
        self.fail_main_navigation = fail_main_navigation
        self.url = "about:blank"
        self.keyboard = _FakeKeyboard()
        self.goto_calls: list[str] = []
        self._html_sequence = list(html_sequence or ["<html><body>ok</body></html>"])
        self._content_calls = 0

    async def goto(self, url: str, wait_until: str, timeout: int):
        del wait_until, timeout
        self.goto_calls.append(url)
        self.url = url
        if self.fail_main_navigation and url == self.main_url:
            raise Exception(f"Page.goto: Timeout 45000ms exceeded at {url}")
        return None

    async def wait_for_load_state(self, state: str, timeout: int) -> None:
        del state, timeout

    async def evaluate(self, script: str, *args):
        del script, args
        return 0

    async def content(self) -> str:
        index = min(self._content_calls, len(self._html_sequence) - 1)
        self._content_calls += 1
        return self._html_sequence[index]

    async def close(self) -> None:
        return

    async def bring_to_front(self) -> None:
        return


class _FakeContext:
    def __init__(
        self,
        page: _FakePage,
        *,
        new_page_error: Exception | None = None,
        close_error: Exception | None = None,
    ) -> None:
        self.page = page
        self.new_page_error = new_page_error
        self.close_error = close_error
        self.closed = False

    async def new_page(self) -> _FakePage:
        if self.new_page_error is not None:
            raise self.new_page_error
        return self.page

    async def close(self) -> None:
        self.closed = True
        if self.close_error is not None:
            raise self.close_error


class _FakeBrowser:
    def __init__(self, context: _FakeContext, *, close_error: Exception | None = None) -> None:
        self.context = context
        self.close_error = close_error
        self.closed = False
        self.context_kwargs: dict[str, object] = {}

    async def new_context(self, **kwargs) -> _FakeContext:
        self.context_kwargs = dict(kwargs)
        return self.context

    async def close(self) -> None:
        self.closed = True
        if self.close_error is not None:
            raise self.close_error


class _FakeChromium:
    def __init__(self, browser: _FakeBrowser, *, launch_error: Exception | None = None) -> None:
        self.browser = browser
        self.launch_error = launch_error
        self.launch_headless_values: list[bool] = []

    async def launch(self, *, headless: bool) -> _FakeBrowser:
        self.launch_headless_values.append(headless)
        if self.launch_error is not None:
            raise self.launch_error
        return self.browser


class _FakePlaywright:
    def __init__(
        self,
        browser: _FakeBrowser,
        *,
        launch_error: Exception | None = None,
        stop_error: Exception | None = None,
    ) -> None:
        self.chromium = _FakeChromium(browser, launch_error=launch_error)
        self.stop_error = stop_error
        self.stopped = False

    async def stop(self) -> None:
        self.stopped = True
        if self.stop_error is not None:
            raise self.stop_error


class _FakeAsyncPlaywrightFactory:
    def __init__(self, playwright: _FakePlaywright) -> None:
        self.playwright = playwright

    async def start(self) -> _FakePlaywright:
        return self.playwright


class BrowserTabManagerTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _settings() -> Settings:
        return Settings(
            openai_api_key="test-key",
            openai_model="gpt-4o-mini",
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
            request_timeout_ms=45000,
            verify_ssl=True,
            telegram_bot_token="",
            telegram_chat_id="",
            telegram_subs_db_path=Path("state/subs_test.db"),
            telegram_user_log_dir=Path("state/user_logs"),
            websites_file_path=Path("websites.txt"),
            neglect_post_if_filters_miss=False,
            enable_human_review_queue=True,
            human_review_confidence_threshold=0.62,
        )

    async def test_start_keeps_manager_alive_when_main_navigation_times_out(self) -> None:
        main_url = "https://example.com/jobs"
        page = _FakePage(main_url=main_url, fail_main_navigation=True)
        context = _FakeContext(page)
        browser = _FakeBrowser(context)
        playwright = _FakePlaywright(browser)
        manager = BrowserTabManager(settings=self._settings(), logger=logging.getLogger("test-browser-tabs"))

        with patch(
            "job_bot.browser_tabs.async_playwright",
            return_value=_FakeAsyncPlaywrightFactory(playwright),
        ):
            await manager.start(main_url)

        self.assertEqual(page.goto_calls, [main_url, "about:blank"])
        self.assertIs(manager._context, context)
        self.assertIs(manager._main_page, page)

        await manager.close()
        self.assertTrue(context.closed)
        self.assertTrue(browser.closed)
        self.assertTrue(playwright.stopped)

    async def test_start_disables_browser_automation_when_launch_fails(self) -> None:
        page = _FakePage(main_url="https://example.com/jobs", fail_main_navigation=False)
        context = _FakeContext(page)
        browser = _FakeBrowser(context)
        playwright = _FakePlaywright(
            browser,
            launch_error=Exception("Error: EPIPE: broken pipe, write"),
        )
        manager = BrowserTabManager(settings=self._settings(), logger=logging.getLogger("test-browser-tabs"))

        with patch(
            "job_bot.browser_tabs.async_playwright",
            return_value=_FakeAsyncPlaywrightFactory(playwright),
        ):
            await manager.start("https://example.com/jobs")

        self.assertIsNone(manager._context)
        self.assertIsNone(manager._browser)
        self.assertIn("broken pipe", manager._disabled_reason or "")

        result = await manager.fetch_in_new_tab("https://example.com/jobs/backend")
        self.assertFalse(result.success)
        self.assertIn("disabled", result.error.lower())

    async def test_fetch_in_new_tab_disables_manager_when_new_page_crashes(self) -> None:
        page = _FakePage(main_url="https://example.com/jobs", fail_main_navigation=False)
        context = _FakeContext(page, new_page_error=Exception("Target page, context or browser has been closed"))
        browser = _FakeBrowser(context)
        manager = BrowserTabManager(settings=self._settings(), logger=logging.getLogger("test-browser-tabs"))
        manager._context = context
        manager._browser = browser
        manager._main_page = page

        result = await manager.fetch_in_new_tab("https://example.com/jobs/backend")

        self.assertFalse(result.success)
        self.assertIn("closed", result.error.lower())
        self.assertIsNone(manager._context)
        self.assertIn("closed", manager._disabled_reason or "")

    async def test_start_reprobes_browser_after_cooldown_expires(self) -> None:
        main_url = "https://example.com/jobs"
        page = _FakePage(main_url=main_url, fail_main_navigation=False)
        context = _FakeContext(page)
        browser = _FakeBrowser(context)
        playwright = _FakePlaywright(browser)
        manager = BrowserTabManager(settings=self._settings(), logger=logging.getLogger("test-browser-tabs"))
        manager._disabled_reason = "broken pipe"
        manager._disabled_until = time.monotonic() - 1

        with patch(
            "job_bot.browser_tabs.async_playwright",
            return_value=_FakeAsyncPlaywrightFactory(playwright),
        ):
            await manager.start(main_url)

        self.assertIs(manager._context, context)
        self.assertIs(manager._main_page, page)
        self.assertIsNone(manager._disabled_reason)

    async def test_close_ignores_browser_shutdown_errors(self) -> None:
        page = _FakePage(main_url="https://example.com/jobs", fail_main_navigation=False)
        context = _FakeContext(page, close_error=Exception("context close failed"))
        browser = _FakeBrowser(context, close_error=Exception("browser close failed"))
        playwright = _FakePlaywright(browser, stop_error=Exception("playwright stop failed"))
        manager = BrowserTabManager(settings=self._settings(), logger=logging.getLogger("test-browser-tabs"))
        manager._context = context
        manager._browser = browser
        manager._playwright = playwright
        manager._main_page = page

        await manager.close()

        self.assertTrue(context.closed)
        self.assertTrue(browser.closed)
        self.assertTrue(playwright.stopped)
        self.assertIsNone(manager._context)
        self.assertIsNone(manager._browser)
        self.assertIsNone(manager._playwright)

    async def test_start_uses_russian_browser_locale_for_russian_sources(self) -> None:
        main_url = "https://www.fl.ru/projects/"
        page = _FakePage(main_url=main_url, fail_main_navigation=False)
        context = _FakeContext(page)
        browser = _FakeBrowser(context)
        playwright = _FakePlaywright(browser)
        manager = BrowserTabManager(settings=self._settings(), logger=logging.getLogger("test-browser-tabs"))

        with patch(
            "job_bot.browser_tabs.async_playwright",
            return_value=_FakeAsyncPlaywrightFactory(playwright),
        ):
            await manager.start(main_url)

        self.assertEqual(browser.context_kwargs.get("locale"), "ru-RU")

    async def test_fetch_in_new_tab_returns_challenge_error_for_challenge_page(self) -> None:
        page = _FakePage(
            main_url="https://example.com/jobs",
            fail_main_navigation=False,
            html_sequence=["<html><body>Just a moment... cf-turnstile</body></html>"],
        )
        context = _FakeContext(page)
        browser = _FakeBrowser(context)
        manager = BrowserTabManager(settings=self._settings(), logger=logging.getLogger("test-browser-tabs"))
        manager._context = context
        manager._browser = browser
        manager._main_page = page

        result = await manager.fetch_in_new_tab("https://example.com/jobs/backend")

        self.assertFalse(result.success)
        self.assertEqual(result.error, "challenge_or_interstitial")
        self.assertIsNotNone(result.fetch_result)

    async def test_resolve_challenge_manually_relaunches_headful_and_waits_until_page_clears(self) -> None:
        settings = self._settings()
        settings.enable_manual_challenge_flow = True
        settings.manual_challenge_timeout_seconds = 30
        settings.manual_challenge_poll_seconds = 0.01
        page = _FakePage(
            main_url="https://example.com/jobs",
            fail_main_navigation=False,
            html_sequence=[
                "<html><body>Checking your browser before accessing cf-turnstile</body></html>",
                "<html><body>Checking your browser before accessing cf-turnstile</body></html>",
                "<html><body>real content</body></html>",
            ],
        )
        context = _FakeContext(page)
        browser = _FakeBrowser(context)
        playwright = _FakePlaywright(browser)
        manager = BrowserTabManager(settings=settings, logger=logging.getLogger("test-browser-tabs"))

        with patch(
            "job_bot.browser_tabs.async_playwright",
            return_value=_FakeAsyncPlaywrightFactory(playwright),
        ):
            solved = await manager.resolve_challenge_manually("https://example.com/jobs")

        self.assertTrue(solved)
        self.assertEqual(playwright.chromium.launch_headless_values, [False])
        self.assertEqual(page.goto_calls, ["https://example.com/jobs"])


if __name__ == "__main__":
    unittest.main()
