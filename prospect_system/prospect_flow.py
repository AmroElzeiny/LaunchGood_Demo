from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

from prospect_system.dashboard_state import ProspectAnalysisState
from prospect_system.logging_utils import StateJSONLLogger
from prospect_system.prospect_ai_analyzer import ProspectAIAnalyzer
from prospect_system.prospect_config import ProspectSettings
from prospect_system.prospect_scraper import ProspectScraper


ProgressCallback = Callable[[ProspectAnalysisState], None]

_COMMON_PROSPECT_PATHS = (
    "/about",
    "/about-us",
    "/contact",
    "/contact-us",
    "/services",
    "/solutions",
    "/programs",
    "/our-work",
    "/mission",
    "/team",
    "/partners",
    "/faq",
    "/support",
    "/donate",
    "/get-involved",
)


class ProspectFlow:
    def __init__(self, settings: ProspectSettings, logger: logging.Logger) -> None:
        self.settings = settings
        self.logger = logger
        self.state_logger = StateJSONLLogger(settings.prospect_log_dir)
        self.scraper = ProspectScraper(settings, logger)
        self.ai = ProspectAIAnalyzer(settings, logger)

    def run(
        self,
        *,
        input_urls: list[str],
        target_criteria: str,
        outreach_goal: str,
        progress_callback: ProgressCallback | None = None,
    ) -> ProspectAnalysisState:
        return asyncio.run(
            self._run_async(
                input_urls=input_urls,
                target_criteria=target_criteria,
                outreach_goal=outreach_goal,
                progress_callback=progress_callback,
            )
        )

    def reanalyze(
        self,
        *,
        state: ProspectAnalysisState,
        website: str,
        reanalysis_instruction: str = "",
        progress_callback: ProgressCallback | None = None,
    ) -> ProspectAnalysisState:
        return asyncio.run(
            self._reanalyze_async(
                state=state,
                website=website,
                reanalysis_instruction=reanalysis_instruction,
                progress_callback=progress_callback,
            )
        )

    async def _run_async(
        self,
        *,
        input_urls: list[str],
        target_criteria: str,
        outreach_goal: str,
        progress_callback: ProgressCallback | None,
    ) -> ProspectAnalysisState:
        state = ProspectAnalysisState.create(
            input_urls=input_urls,
            target_criteria=target_criteria,
            outreach_goal=outreach_goal,
        )
        self._log(state, "Input received", stage="input", progress_callback=progress_callback)
        for missing in self.settings.missing_env_values:
            self._error(
                state,
                stage="config",
                message=f"Missing .env value: {missing}",
                progress_callback=progress_callback,
            )

        for website in input_urls:
            state.current_url = website
            self._log(state, "Starting scrape", stage="scraping", url=website, progress_callback=progress_callback)
            await self._analyze_one_url(state, website=website, progress_callback=progress_callback)

        self.state_logger.write_state_snapshot(state)
        return state

    async def _reanalyze_async(
        self,
        *,
        state: ProspectAnalysisState,
        website: str,
        reanalysis_instruction: str,
        progress_callback: ProgressCallback | None,
    ) -> ProspectAnalysisState:
        state.current_url = website
        state.reanalysis_count += 1
        self._log(
            state,
            "Re-analysis attempt started",
            stage="reanalysis",
            url=website,
            progress_callback=progress_callback,
        )
        if not self._pages_for_site(state, website):
            await self._scrape_and_record(
                state,
                url=website,
                root_url=website,
                log_message="Scraping homepage",
                progress_callback=progress_callback,
            )
        self._log(state, "AI is updating analysis", stage="ai", url=website, progress_callback=progress_callback)
        card = self.ai.create_prospect_card(
            state,
            website=website,
            reanalysis_instruction=reanalysis_instruction,
        )
        state.set_card(card)
        self._log(state, "Prospect card created", stage="output", url=website, progress_callback=progress_callback)
        self._log(
            state,
            "Waiting for human decision",
            stage="decision",
            url=website,
            progress_callback=progress_callback,
        )
        self.state_logger.write_state_snapshot(state)
        return state

    async def _analyze_one_url(
        self,
        state: ProspectAnalysisState,
        *,
        website: str,
        progress_callback: ProgressCallback | None,
    ) -> None:
        self._log(
            state,
            "Checking website accessibility",
            stage="scraping",
            url=website,
            progress_callback=progress_callback,
        )
        self._log(
            state,
            f"Scraping target is {self.settings.max_pages_to_scrape} page(s) for this website",
            stage="scraping",
            url=website,
            progress_callback=progress_callback,
        )
        homepage_added = await self._scrape_and_record(
            state,
            url=website,
            root_url=website,
            log_message=f"Scraping page 1 of {self.settings.max_pages_to_scrape}: homepage",
            progress_callback=progress_callback,
        )
        if not homepage_added:
            return

        self._log(state, "AI is analyzing homepage", stage="ai", url=website, progress_callback=progress_callback)
        decision = self.ai.assess_information_need(
            state,
            website=website,
            candidate_links=self._candidate_links_for_site(state, website),
        )
        self._log(
            state,
            decision.reason or "AI information decision completed",
            stage="ai",
            url=website,
            progress_callback=progress_callback,
        )
        scraped_count = len(self._pages_for_site(state, website))
        while scraped_count < self.settings.max_pages_to_scrape:
            candidate_links = self._candidate_links_for_site(state, website)
            selected_urls = self._unseen_urls_for_site(state, website, decision.selected_extra_urls)
            if not selected_urls:
                selected_urls = self._unseen_urls_for_site(
                    state,
                    website,
                    [str(link.get("url") or "").strip() for link in candidate_links],
                )
            if not selected_urls:
                self._log(
                    state,
                    "No more internal pages were found to scrape before reaching the configured page target",
                    stage="scraping",
                    url=website,
                    progress_callback=progress_callback,
                )
                break
            message = (
                "Enough evidence found, scraping additional relevant pages up to the configured limit"
                if decision.enough_information
                else "Decision not enough yet, searching for extra relevant pages"
            )
            self._log(
                state,
                message,
                stage="ai",
                url=website,
                progress_callback=progress_callback,
            )
            for next_url in selected_urls:
                if len(self._pages_for_site(state, website)) >= self.settings.max_pages_to_scrape:
                    break
                next_page_number = len(self._pages_for_site(state, website)) + 1
                added = await self._scrape_and_record(
                    state,
                    url=next_url,
                    root_url=website,
                    log_message=f"Scraping extra page {next_page_number} of {self.settings.max_pages_to_scrape}",
                    progress_callback=progress_callback,
                )
                if not added:
                    continue
                self._log(
                    state,
                    "AI is updating analysis",
                    stage="ai",
                    url=website,
                    progress_callback=progress_callback,
                )
                decision = self.ai.assess_information_need(
                    state,
                    website=website,
                    candidate_links=self._candidate_links_for_site(state, website),
                )
                self._log(
                    state,
                    decision.reason or "AI information decision completed",
                    stage="ai",
                    url=website,
                    progress_callback=progress_callback,
                )
            new_scraped_count = len(self._pages_for_site(state, website))
            if new_scraped_count <= scraped_count:
                break
            scraped_count = new_scraped_count

        card = self.ai.create_prospect_card(state, website=website)
        state.set_card(card)
        self._log(state, "Prospect card created", stage="output", url=website, progress_callback=progress_callback)
        self._log(
            state,
            "Waiting for human decision",
            stage="decision",
            url=website,
            progress_callback=progress_callback,
        )

    async def _scrape_and_record(
        self,
        state: ProspectAnalysisState,
        *,
        url: str,
        root_url: str,
        log_message: str,
        progress_callback: ProgressCallback | None,
    ) -> bool:
        self._log(state, log_message, stage="scraping", url=url, progress_callback=progress_callback)
        outcome = await self.scraper.scrape_page(url, root_url=root_url)
        if outcome.blocked:
            message = self.settings.captcha_end_message or outcome.error
            self._error(
                state,
                stage="blocked_page",
                message=message,
                url=url,
                details={"raw_error": outcome.error},
                progress_callback=progress_callback,
            )
            return False
        if outcome.error:
            self._error(
                state,
                stage="scraping",
                message=outcome.error,
                url=url,
                progress_callback=progress_callback,
            )
            return False
        if outcome.page is None:
            self._error(
                state,
                stage="scraping",
                message="Website returned no readable page.",
                url=url,
                progress_callback=progress_callback,
            )
            return False
        if not outcome.page.text.strip():
            self._error(
                state,
                stage="scraping",
                message="Empty scraped content.",
                url=url,
                progress_callback=progress_callback,
            )
            return False
        existing_urls = {
            str(value or "").strip().lower()
            for page in self._pages_for_site(state, root_url)
            for value in (page.url, page.final_url)
            if str(value or "").strip()
        }
        page_key = str(outcome.page.final_url or outcome.page.url or "").strip().lower()
        if page_key in existing_urls:
            self._log(
                state,
                "Skipped duplicate page after redirect",
                stage="scraping",
                url=outcome.page.final_url or outcome.page.url,
                progress_callback=progress_callback,
            )
            return False
        state.add_scraped_page(outcome.page)
        self.state_logger.write_state_snapshot(state)
        self._emit(progress_callback, state)
        page_count = len(self._pages_for_site(state, root_url))
        self._log(
            state,
            f"Scraped page {page_count} of {self.settings.max_pages_to_scrape}",
            stage="scraping",
            url=outcome.page.final_url or outcome.page.url,
            progress_callback=progress_callback,
        )
        return True

    def _candidate_links_for_site(self, state: ProspectAnalysisState, website: str) -> list[dict[str, Any]]:
        scraped = {
            str(value or "").strip()
            for page in self._pages_for_site(state, website)
            for value in (page.url, page.final_url)
            if str(value or "").strip()
        }
        found: dict[str, dict[str, Any]] = {}
        for page in self._pages_for_site(state, website):
            for link in page.links:
                url = str(link.get("url") or "").strip()
                if not url or url in scraped:
                    continue
                existing = found.get(url)
                if existing is None or float(link.get("structural_score", 0)) > float(existing.get("structural_score", 0)):
                    found[url] = dict(link)
        for link in self._fallback_probe_links(website, scraped | set(found)):
            found.setdefault(str(link["url"]), link)
        return sorted(found.values(), key=lambda item: float(item.get("structural_score", 0)), reverse=True)

    @staticmethod
    def _fallback_probe_links(website: str, excluded_urls: set[str]) -> list[dict[str, Any]]:
        parsed = urlsplit(website)
        if not parsed.scheme or not parsed.netloc:
            return []
        root = urlunsplit((parsed.scheme, parsed.netloc, "/", "", ""))
        excluded = {url.strip().lower() for url in excluded_urls if url.strip()}
        links: list[dict[str, Any]] = []
        for index, path in enumerate(_COMMON_PROSPECT_PATHS):
            url = urljoin(root, path.lstrip("/"))
            if url.lower() in excluded:
                continue
            links.append(
                {
                    "url": url,
                    "anchor_text": path.strip("/").replace("-", " ").title(),
                    "source_page": root,
                    "structural_score": 1.5 - (index * 0.02),
                }
            )
        return links

    def _unseen_urls_for_site(
        self,
        state: ProspectAnalysisState,
        website: str,
        urls: list[str],
    ) -> list[str]:
        scraped = {
            str(value or "").strip().lower()
            for page in self._pages_for_site(state, website)
            for value in (page.url, page.final_url)
            if str(value or "").strip()
        }
        selected: list[str] = []
        seen: set[str] = set()
        for raw_url in urls:
            url = str(raw_url or "").strip()
            key = url.lower()
            if not url or key in scraped or key in seen:
                continue
            selected.append(url)
            seen.add(key)
        return selected

    @staticmethod
    def _pages_for_site(state: ProspectAnalysisState, website: str) -> list[Any]:
        from prospect_system.prospect_ai_analyzer import _same_site

        pages = [page for page in state.scraped_pages if _same_site(page.final_url or page.url, website)]
        return pages or []

    def _log(
        self,
        state: ProspectAnalysisState,
        message: str,
        *,
        stage: str,
        url: str = "",
        progress_callback: ProgressCallback | None,
    ) -> None:
        eta_seconds = state.estimate_remaining_seconds(max_pages_to_scrape=self.settings.max_pages_to_scrape)
        step = state.log_step(message, stage=stage, url=url, eta_seconds=eta_seconds)
        self.state_logger.write_step(state, step)
        self.state_logger.write_state_snapshot(state)
        self._emit(progress_callback, state)

    def _error(
        self,
        state: ProspectAnalysisState,
        *,
        stage: str,
        message: str,
        url: str = "",
        details: dict[str, Any] | None = None,
        progress_callback: ProgressCallback | None,
    ) -> None:
        error = state.add_error(stage=stage, message=message, url=url, details=details)
        self.state_logger.write_error(error)
        if state.ai_step_logs:
            self.state_logger.write_step(state, state.ai_step_logs[-1])
        self.state_logger.write_state_snapshot(state)
        self._emit(progress_callback, state)

    @staticmethod
    def _emit(progress_callback: ProgressCallback | None, state: ProspectAnalysisState) -> None:
        if progress_callback is None:
            return
        progress_callback(state)
