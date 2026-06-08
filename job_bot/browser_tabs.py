from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass

from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright
from scrapling import Selector

from job_bot.config import Settings
from job_bot.fetching import FetchResult, ResilientFetcher
from job_bot.site_profiles import get_site_profile


@dataclass(slots=True)
class TabResult:
    success: bool
    fetch_result: FetchResult | None
    error: str = ""


class BrowserTabManager:
    def __init__(self, settings: Settings, logger: logging.Logger) -> None:
        self.settings = settings
        self.logger = logger
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._main_page: Page | None = None
        self._disabled_reason: str | None = None
        self._disabled_until = 0.0
        self._driver_failure_count = 0
        self._main_url = ""
        self._lock = asyncio.Lock()

    async def start(self, main_url: str) -> None:
        async with self._lock:
            self._main_url = main_url
            if self._browser is not None:
                return
            self._restore_browser_after_cooldown_locked()
            if self._disabled_reason is not None:
                return
            try:
                await self._launch_browser_locked(main_url, headless=self.settings.headless_browser)
            except Exception as exc:  # noqa: BLE001
                await self._disable_locked(
                    str(exc),
                    source_url=main_url,
                    prefix="[tabs] browser startup failed",
                )
                return

            try:
                await self._main_page.goto(
                    main_url,
                    wait_until="domcontentloaded",
                    timeout=self.settings.request_timeout_ms,
                )
                await self._wait_for_full_load(self._main_page)
                await self._dismiss_interstitials(self._main_page)
                await self._scroll_down_up(self._main_page)
                await self._dismiss_interstitials(self._main_page)
                self._reset_browser_health_locked()
                self.logger.info("[tabs] main tab ready at %s", self._main_page.url)
            except Exception as exc:  # noqa: BLE001
                self.logger.warning(
                    "[tabs] main tab navigation failed for %s: %s; continuing with fallback-only mode",
                    main_url,
                    str(exc),
                )
                try:
                    await self._main_page.goto(
                        "about:blank",
                        wait_until="domcontentloaded",
                        timeout=min(5000, self.settings.request_timeout_ms),
                    )
                except Exception:  # noqa: BLE001
                    pass

    async def resolve_challenge_manually(self, url: str) -> bool:
        if not bool(getattr(self.settings, "enable_manual_challenge_flow", False)):
            return False

        async with self._lock:
            self._main_url = url
            self._disabled_reason = None
            self._disabled_until = 0.0
            self.logger.warning(
                "[tabs] manual challenge flow opened for %s; waiting for a human solve for up to %ss",
                url,
                int(getattr(self.settings, "manual_challenge_timeout_seconds", 900)),
            )
            try:
                await self._close_locked()
                await self._launch_browser_locked(url, headless=False)
                assert self._main_page is not None
                await self._main_page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=self.settings.request_timeout_ms,
                )
                await self._wait_for_full_load(self._main_page)
                with contextlib.suppress(Exception):
                    await self._main_page.bring_to_front()
                deadline = time.monotonic() + max(
                    30,
                    int(getattr(self.settings, "manual_challenge_timeout_seconds", 900)),
                )
                poll_seconds = max(
                    0.25,
                    float(getattr(self.settings, "manual_challenge_poll_seconds", 2.0)),
                )
                while time.monotonic() < deadline:
                    html = str(await self._main_page.content() or "")
                    if not self._looks_like_challenge_page(self._main_page.url, html):
                        self._reset_browser_health_locked()
                        self.logger.info(
                            "[tabs] manual challenge solved for %s; resuming crawl",
                            self._main_page.url,
                        )
                        return True
                    await asyncio.sleep(poll_seconds)
                self.logger.warning("[tabs] manual challenge flow timed out for %s", url)
            except Exception as exc:  # noqa: BLE001
                self.logger.warning("[tabs] manual challenge flow failed for %s: %s", url, str(exc))
            await self._close_locked()
            return False

    async def close(self) -> None:
        async with self._lock:
            await self._close_locked()

    async def fetch_in_new_tab(self, url: str) -> TabResult:
        async with self._lock:
            self._restore_browser_after_cooldown_locked()
            if self._context is None or self._main_page is None:
                return TabResult(
                    success=False,
                    fetch_result=None,
                    error=self._disabled_error("Browser tab manager not started"),
                )

            tab: Page | None = None
            status = 0
            final_url = url
            try:
                tab = await self._context.new_page()
                response = await tab.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=self.settings.request_timeout_ms,
                )
                if response is not None:
                    status = response.status
                final_url = tab.url
                await self._wait_for_full_load(tab)
                await self._dismiss_interstitials(tab)
                await self._scroll_down_up(tab)
                await self._dismiss_interstitials(tab)
                html = await tab.content()
                if self._looks_like_challenge_page(final_url, html):
                    self.logger.warning("[tabs] challenge/interstitial detected in new tab for %s", final_url)
                    return TabResult(
                        success=False,
                        fetch_result=self._challenge_fetch_result(
                            url=final_url,
                            status=status,
                            html=html,
                            strategy="main-tab-new-tab",
                        ),
                        error="challenge_or_interstitial",
                    )
                selector_page = Selector(content=html, url=final_url)
                self._reset_browser_health_locked()
                self.logger.info("[tabs] opened new tab status=%s url=%s", status, final_url)
                return TabResult(
                    success=True,
                    fetch_result=FetchResult(
                        url=final_url,
                        status=status,
                        html=html,
                        page=selector_page,
                        strategy="main-tab-new-tab",
                    ),
                )
            except Exception as exc:  # noqa: BLE001
                if self._looks_like_browser_driver_failure(exc):
                    await self._disable_locked(
                        str(exc),
                        source_url=url,
                        prefix="[tabs] browser driver failed while opening new tab",
                    )
                else:
                    self.logger.warning("[tabs] new tab failed for %s: %s", url, str(exc))
                return TabResult(success=False, fetch_result=None, error=str(exc))
            finally:
                if tab is not None:
                    with contextlib.suppress(Exception):
                        await tab.close()
                if self._main_page is not None:
                    with contextlib.suppress(Exception):
                        await self._main_page.bring_to_front()
                self.logger.info("[tabs] closed temporary tab and returned to main tab")

    async def fetch_card_detail_via_click(self, source_page_url: str, candidate_url: str) -> TabResult:
        async with self._lock:
            self._restore_browser_after_cooldown_locked()
            if self._context is None or self._main_page is None:
                return TabResult(
                    success=False,
                    fetch_result=None,
                    error=self._disabled_error("Browser tab manager not started"),
                )

            page: Page | None = None
            popup_task: asyncio.Task[Page] | None = None
            popup_page: Page | None = None
            try:
                page = await self._context.new_page()
                await page.goto(
                    source_page_url,
                    wait_until="domcontentloaded",
                    timeout=self.settings.request_timeout_ms,
                )
                await self._wait_for_full_load(page)
                await self._dismiss_interstitials(page)
                await self._scroll_down_up(page)
                await self._dismiss_interstitials(page)
                popup_task = asyncio.create_task(
                    self._context.wait_for_event(
                        "page",
                        timeout=min(8000, self.settings.request_timeout_ms),
                    )
                )
                clicked = await self._click_matching_card(page, candidate_url)
                if not clicked:
                    return TabResult(
                        success=False,
                        fetch_result=None,
                        error="No clickable card matched the candidate URL on the source page.",
                    )
                with contextlib.suppress(Exception):
                    popup_page = await popup_task
                target_page = popup_page or page
                await asyncio.sleep(0.75)
                await self._wait_for_full_load(target_page)
                await self._dismiss_interstitials(target_page)
                await self._scroll_down_up(target_page)
                await self._dismiss_interstitials(target_page)
                detail_html = await target_page.content()
                if self._looks_like_challenge_page(target_page.url, detail_html):
                    self.logger.warning(
                        "[tabs] challenge/interstitial detected after card click source=%s target=%s final=%s",
                        source_page_url,
                        candidate_url,
                        target_page.url,
                    )
                    return TabResult(
                        success=False,
                        fetch_result=self._challenge_fetch_result(
                            url=target_page.url,
                            status=200,
                            html=detail_html,
                            strategy="source-page-card-click",
                        ),
                        error="challenge_or_interstitial",
                    )
                fetch_result = await self._page_to_fetch_result(target_page, strategy="source-page-card-click")
                self._reset_browser_health_locked()
                self.logger.info(
                    "[tabs] card click detail fetch success source=%s target=%s final=%s",
                    source_page_url,
                    candidate_url,
                    fetch_result.url,
                )
                return TabResult(success=True, fetch_result=fetch_result)
            except Exception as exc:  # noqa: BLE001
                if self._looks_like_browser_driver_failure(exc):
                    await self._disable_locked(
                        str(exc),
                        source_url=candidate_url,
                        prefix="[tabs] browser driver failed during card-click detail fetch",
                    )
                else:
                    self.logger.warning(
                        "[tabs] card click detail fetch failed source=%s target=%s: %s",
                        source_page_url,
                        candidate_url,
                        str(exc),
                    )
                return TabResult(success=False, fetch_result=None, error=str(exc))
            finally:
                if popup_task is not None and not popup_task.done():
                    popup_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await popup_task
                if popup_page is not None:
                    with contextlib.suppress(Exception):
                        await popup_page.close()
                if page is not None:
                    with contextlib.suppress(Exception):
                        await page.close()
                if self._main_page is not None:
                    with contextlib.suppress(Exception):
                        await self._main_page.bring_to_front()

    async def _page_to_fetch_result(self, page: Page, *, strategy: str) -> FetchResult:
        html = await page.content()
        selector_page = Selector(content=html, url=page.url)
        return FetchResult(
            url=page.url,
            status=200,
            html=html,
            page=selector_page,
            strategy=strategy,
        )

    async def _launch_browser_locked(self, main_url: str, *, headless: bool) -> None:
        locale = self._site_locale(main_url)
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=headless)
        self._context = await self._browser.new_context(
            ignore_https_errors=not self.settings.verify_ssl,
            locale=locale,
        )
        self._main_page = await self._context.new_page()

    @staticmethod
    def _site_locale(url: str) -> str:
        profile = get_site_profile(url)
        return str(getattr(profile, "browser_locale", "") or "").strip() or "en-US"

    @staticmethod
    def _looks_like_challenge_page(url: str, html: str) -> bool:
        return ResilientFetcher._looks_like_cloudflare_challenge(url, html)

    @staticmethod
    def _challenge_fetch_result(*, url: str, status: int, html: str, strategy: str) -> FetchResult:
        return FetchResult(
            url=url,
            status=status,
            html=html,
            page=None,
            strategy=strategy,
        )

    async def _click_matching_card(self, page: Page, candidate_url: str) -> bool:
        click_script = """
            (targetUrl) => {
                const normalize = (value) => {
                    const raw = (value || "").toString().trim();
                    if (!raw) return "";
                    try {
                        const absolute = new URL(raw, window.location.href);
                        absolute.hash = "";
                        if (absolute.pathname.length > 1 && absolute.pathname.endsWith("/")) {
                            absolute.pathname = absolute.pathname.slice(0, -1);
                        }
                        return absolute.toString();
                    } catch (error) {
                        return raw.replace(/#.*$/, "").replace(/\\/$/, "");
                    }
                };
                const target = normalize(targetUrl);
                if (!target) return false;
                const clickable = Array.from(
                    document.querySelectorAll("a[href], [data-url], [data-href], [onclick], button, [role='button']")
                );
                for (const el of clickable) {
                    if (!(el instanceof HTMLElement)) continue;
                    const candidates = [
                        el.getAttribute("href"),
                        el.getAttribute("data-url"),
                        el.getAttribute("data-href"),
                        el.getAttribute("onclick"),
                    ].filter(Boolean).map(normalize);
                    const isMatch = candidates.some((value) => value && (value === target || target.includes(value) || value.includes(target)));
                    if (!isMatch) continue;
                    el.scrollIntoView({ block: "center", inline: "center" });
                    el.click();
                    return true;
                }
                return false;
            }
        """
        try:
            return bool(await page.evaluate(click_script, candidate_url))
        except Exception:  # noqa: BLE001
            return False

    async def _wait_for_full_load(self, page: Page) -> None:
        try:
            await page.wait_for_load_state("networkidle", timeout=self.settings.request_timeout_ms)
        except Exception:  # noqa: BLE001
            # Some pages keep background network requests forever.
            pass

    async def _scroll_down_up(self, page: Page) -> None:
        script = """
            async () => {
                await new Promise((r) => setTimeout(r, 400));
                const step = Math.max(300, Math.floor(window.innerHeight * 0.8));
                let y = 0;
                const maxY = Math.max(document.body.scrollHeight, document.documentElement.scrollHeight);
                while (y < maxY) {
                    window.scrollTo(0, y);
                    await new Promise((r) => setTimeout(r, 120));
                    y += step;
                }
                await new Promise((r) => setTimeout(r, 250));
                while (y > 0) {
                    y -= step;
                    window.scrollTo(0, Math.max(0, y));
                    await new Promise((r) => setTimeout(r, 100));
                }
                window.scrollTo(0, 0);
            }
        """
        try:
            await page.evaluate(script)
        except Exception:  # noqa: BLE001
            pass

    async def _dismiss_interstitials(self, page: Page) -> None:
        click_script = """
            () => {
                const closeExact = new Set(["x", "×", "✕", "✖", "close", "dismiss"]);
                const hintTokens = [
                    "cookie", "consent", "privacy", "location", "notification", "popup",
                    "accept", "allow", "agree", "got it", "continue", "not now", "no thanks",
                    "use current location", "enable location"
                ];
                const clickable = "button, [role='button'], a[href], input[type='button'], input[type='submit'], [aria-label], [title]";
                const normalize = (value) => (value || "").toLowerCase().replace(/\\s+/g, " ").trim();
                const getBlob = (el) => normalize(
                    [
                        el.innerText,
                        el.textContent,
                        el.getAttribute("aria-label"),
                        el.getAttribute("title"),
                        el.getAttribute("value"),
                    ]
                    .filter(Boolean)
                    .join(" ")
                );
                const isVisible = (el) => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    if (!style) return false;
                    return (
                        style.visibility !== "hidden" &&
                        style.display !== "none" &&
                        style.pointerEvents !== "none" &&
                        rect.width > 6 &&
                        rect.height > 6 &&
                        rect.bottom >= 0 &&
                        rect.right >= 0 &&
                        rect.top <= window.innerHeight &&
                        rect.left <= window.innerWidth
                    );
                };
                const hasOverlayAncestor = (el) => {
                    let current = el;
                    for (let i = 0; i < 6 && current; i += 1) {
                        const role = normalize(current.getAttribute && current.getAttribute("role"));
                        const id = normalize(current.id);
                        const className = normalize((current.className || "").toString());
                        if (
                            role === "dialog" ||
                            role === "alertdialog" ||
                            id.includes("cookie") ||
                            id.includes("consent") ||
                            id.includes("modal") ||
                            className.includes("cookie") ||
                            className.includes("consent") ||
                            className.includes("modal") ||
                            className.includes("popup")
                        ) {
                            return true;
                        }
                        current = current.parentElement;
                    }
                    return false;
                };
                const candidates = Array.from(document.querySelectorAll(clickable));
                let clicks = 0;
                const used = new Set();
                for (const el of candidates) {
                    if (clicks >= 10) break;
                    if (!(el instanceof HTMLElement)) continue;
                    if (!isVisible(el)) continue;
                    const blob = getBlob(el);
                    if (!blob) continue;
                    const isExactClose = closeExact.has(blob);
                    const hasHint = hintTokens.some((token) => blob.includes(token));
                    if (!isExactClose && !hasHint) continue;
                    if (!isExactClose && !hasOverlayAncestor(el) && !blob.includes("cookie") && !blob.includes("location")) {
                        continue;
                    }
                    const rect = el.getBoundingClientRect();
                    const key = `${blob}|${Math.round(rect.left)}|${Math.round(rect.top)}`;
                    if (used.has(key)) continue;
                    used.add(key);
                    try {
                        el.click();
                        clicks += 1;
                    } catch (error) {
                        // no-op
                    }
                }
                return clicks;
            }
        """
        total_clicks = 0
        for _ in range(3):
            clicked = 0
            try:
                clicked = int(await page.evaluate(click_script))
            except Exception:  # noqa: BLE001
                clicked = 0
            total_clicks += max(0, clicked)
            try:
                await page.keyboard.press("Escape")
            except Exception:  # noqa: BLE001
                pass
            if clicked <= 0:
                break
            await asyncio.sleep(0.25)
        if total_clicks > 0:
            self.logger.info("[tabs] dismissed %s interstitial controls on %s", total_clicks, page.url)

    async def _disable_locked(self, reason: str, *, source_url: str, prefix: str) -> None:
        self._driver_failure_count += 1
        cooldown_base = max(30, int(getattr(self.settings, "browser_reprobe_cooldown_seconds", 600)))
        cooldown_seconds = min(cooldown_base * max(1, 2 ** (self._driver_failure_count - 1)), 3600)
        self._disabled_reason = reason
        self._disabled_until = time.monotonic() + cooldown_seconds
        self.logger.warning(
            "%s for %s: %s; continuing with fallback-only mode for %ss before re-probe",
            prefix,
            source_url,
            reason,
            cooldown_seconds,
        )
        await self._close_locked()

    async def _close_locked(self) -> None:
        if self._context is not None:
            with contextlib.suppress(Exception):
                await self._context.close()
        if self._browser is not None:
            with contextlib.suppress(Exception):
                await self._browser.close()
        if self._playwright is not None:
            with contextlib.suppress(Exception):
                await self._playwright.stop()
        self._playwright = None
        self._browser = None
        self._context = None
        self._main_page = None

    def _disabled_error(self, default_message: str) -> str:
        if self._disabled_reason:
            remaining_seconds = max(0, int(round(self._disabled_until - time.monotonic())))
            if remaining_seconds > 0:
                return f"Browser automation disabled for {remaining_seconds}s cooldown: {self._disabled_reason}"
            return f"Browser automation disabled: {self._disabled_reason}"
        return default_message

    def _restore_browser_after_cooldown_locked(self) -> None:
        if self._disabled_reason is None:
            return
        if time.monotonic() < self._disabled_until:
            return
        self.logger.info("[tabs] browser cooldown elapsed; re-probing browser automation")
        self._disabled_reason = None
        self._disabled_until = 0.0

    def _reset_browser_health_locked(self) -> None:
        self._driver_failure_count = 0
        self._disabled_reason = None
        self._disabled_until = 0.0

    @staticmethod
    def _looks_like_browser_driver_failure(exc: Exception) -> bool:
        text = str(exc).strip().lower()
        markers = (
            "broken pipe",
            "epipe",
            "pipe closed",
            "channel closed",
            "connection closed",
            "connection disposed",
            "target page, context or browser has been closed",
            "target browser, context or page has been closed",
            "browser has been closed",
            "browser closed",
            "playwright connection closed",
        )
        return any(marker in text for marker in markers)
