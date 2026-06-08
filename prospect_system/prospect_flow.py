from __future__ import annotations
import re
import asyncio
import logging
from collections.abc import Callable
from typing import Any


from prospect_system.dashboard_state import ProspectAnalysisState, ScrapedPage
from prospect_system.google_sheets_client import GoogleSheetsClient
from prospect_system.logging_utils import StateJSONLLogger
from prospect_system.prospect_ai_analyzer import ProspectAIAnalyzer
from prospect_system.prospect_config import ProspectSettings
from prospect_system.prospect_scraper import ProspectScraper

ProgressCallback = Callable[[ProspectAnalysisState], None]

BLOCKED_ERROR_TYPE = "Blocked / Forbidden"


def _safe_structural_score(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


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
        timeout_seconds: float | None = None,
    ) -> ProspectAnalysisState:
        coro = self._run_async(
            input_urls=input_urls,
            target_criteria=target_criteria,
            outreach_goal=outreach_goal,
            progress_callback=progress_callback,
        )

        if timeout_seconds and timeout_seconds > 0:
            coro = asyncio.wait_for(coro, timeout=timeout_seconds)
        return asyncio.run(coro)

    def run_cached_demo(
        self,
        *,
        input_urls: list[str],
        target_criteria: str,
        outreach_goal: str,
        cached_text: str,
        progress_callback: ProgressCallback | None = None,
        timeout_seconds: float | None = None,
    ) -> ProspectAnalysisState:
        coro = self._run_cached_demo_async(
            input_urls=input_urls,
            target_criteria=target_criteria,
            outreach_goal=outreach_goal,
            cached_text=cached_text,
            progress_callback=progress_callback,
        )
        if timeout_seconds and timeout_seconds > 0:
            coro = asyncio.wait_for(coro, timeout=timeout_seconds)
        return asyncio.run(coro)

    def reanalyze(
        self,
        *,
        state: ProspectAnalysisState,
        website: str,
        reanalysis_instruction: str = "",
        progress_callback: ProgressCallback | None = None,
        timeout_seconds: float | None = None,
    ) -> ProspectAnalysisState:
        coro = self._reanalyze_async(
            state=state,
            website=website,
            reanalysis_instruction=reanalysis_instruction,
            progress_callback=progress_callback,
        )
        if timeout_seconds and timeout_seconds > 0:
            coro = asyncio.wait_for(coro, timeout=timeout_seconds)
        return asyncio.run(coro)

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
        self._log(
            state, "Input received", stage="input", progress_callback=progress_callback
        )
        for missing in self.settings.missing_env_values:
            self._error(
                state,
                stage="config",
                message=f"Missing .env value: {missing}",
                progress_callback=progress_callback,
            )

        for website in input_urls:
            state.current_url = website
            self._log(
                state,
                "Starting scrape",
                stage="scraping",
                url=website,
                progress_callback=progress_callback,
            )
            await self._analyze_one_url(
                state, website=website, progress_callback=progress_callback
            )

        self.state_logger.write_state_snapshot(state)
        return state

    async def _run_cached_demo_async(
        self,
        *,
        input_urls: list[str],
        target_criteria: str,
        outreach_goal: str,
        cached_text: str,
        progress_callback: ProgressCallback | None,
    ) -> ProspectAnalysisState:
        website = (
            input_urls[0]
            if input_urls
            else self.settings.demo_website_url or "https://www.launchgood.com/"
        )
        state = ProspectAnalysisState.create(
            input_urls=[website],
            target_criteria=target_criteria,
            outreach_goal=outreach_goal,
        )
        state.current_url = website
        self._log(
            state, "Input received", stage="input", progress_callback=progress_callback
        )
        for missing in self.settings.missing_env_values:
            self._error(
                state,
                stage="config",
                message=f"Missing .env value: {missing}",
                progress_callback=progress_callback,
            )
        self._log(
            state,
            "Using cached demo content instead of live scraping",
            stage="scraping",
            url=website,
            progress_callback=progress_callback,
        )
        page = ScrapedPage(
            url=website,
            final_url=website,
            title="Cached Demo Prospect Page",
            text=str(cached_text or "").strip(),
            metadata={"source": "cached_demo"},
            links=[],
            status=200,
            strategy="cached_demo",
        )
        state.add_scraped_page(page)
        self.state_logger.write_state_snapshot(state)
        self._emit(progress_callback, state)
        self._log(
            state,
            "AI is analyzing cached demo content",
            stage="ai",
            url=website,
            progress_callback=progress_callback,
        )
        card = self._create_card_with_evidence(
            state, website=website, progress_callback=progress_callback
        )
        state.set_card(card)
        self._log(
            state,
            "Prospect card created",
            stage="output",
            url=website,
            progress_callback=progress_callback,
        )
        self._log(
            state,
            "Waiting for human decision",
            stage="decision",
            url=website,
            progress_callback=progress_callback,
        )
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
        self._log(
            state,
            "AI is updating analysis",
            stage="ai",
            url=website,
            progress_callback=progress_callback,
        )
        card = self._create_card_with_evidence(
            state,
            website=website,
            reanalysis_instruction=reanalysis_instruction,
            progress_callback=progress_callback,
        )
        state.set_card(card)
        self._log(
            state,
            "Prospect card created",
            stage="output",
            url=website,
            progress_callback=progress_callback,
        )
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
        candidate_links = self._candidate_links_for_site(state, website)

        self._log(
            state,
            f"Discovered {len(candidate_links)} internal link(s) from scraped page metadata",
            stage="scraping",
            url=website,
            progress_callback=progress_callback,
        )
        if not candidate_links:
            self._log(
                state,
                "No internal links were discovered; continuing with homepage evidence and uncertainty",
                stage="scraping",
                url=website,
                progress_callback=progress_callback,
            )
        ranking = self._rank_links(
            state,
            website=website,
            candidate_links=candidate_links,
            progress_callback=progress_callback,
        )
        if ranking.homepage_enough:
            self._log(
                state,
                f"AI says enough information is available: {ranking.reason_homepage_enough}",
                stage="ai",
                url=website,
                progress_callback=progress_callback,
            )
        else:
            self._log_selected_links(
                state,
                ranking.ranked_links,
                url=website,
                progress_callback=progress_callback,
            )

        attempted_urls: set[str] = set()
        while (
            not ranking.homepage_enough
            and len(self._pages_for_site(state, website))
            < self.settings.max_pages_to_scrape
        ):
            selected_links = self._unseen_ranked_links(
                state, website, ranking.ranked_links, attempted_urls
            )
            if not selected_links:
                candidate_links = self._candidate_links_for_site(state, website)
                if not candidate_links:
                    self._log(
                        state,
                        "No more useful discovered internal links remain; creating output from available evidence",
                        stage="scraping",
                        url=website,
                        progress_callback=progress_callback,
                    )
                    break
                ranking = self._rank_links(
                    state,
                    website=website,
                    candidate_links=candidate_links,
                    progress_callback=progress_callback,
                )
                if ranking.homepage_enough:
                    self._log(
                        state,
                        f"AI says enough information is available: {ranking.reason_homepage_enough}",
                        stage="ai",
                        url=website,
                        progress_callback=progress_callback,
                    )
                    break
                selected_links = self._unseen_ranked_links(
                    state, website, ranking.ranked_links, attempted_urls
                )
            if not selected_links:
                self._log(
                    state,
                    "AI did not select additional useful pages; creating output from available evidence",
                    stage="scraping",
                    url=website,
                    progress_callback=progress_callback,
                )
                break
            for ranked_link in selected_links:
                if (
                    len(self._pages_for_site(state, website))
                    >= self.settings.max_pages_to_scrape
                ):
                    break
                next_url = ranked_link.url
                attempted_urls.add(next_url.lower())
                next_page_number = len(self._pages_for_site(state, website)) + 1
                self._log(
                    state,
                    f"AI selected page {next_page_number} of {self.settings.max_pages_to_scrape}: {next_url}",
                    stage="ai",
                    url=next_url,
                    progress_callback=progress_callback,
                )
                self._log(
                    state,
                    f"Selection reason: {ranked_link.reason}",
                    stage="ai",
                    url=next_url,
                    progress_callback=progress_callback,
                )
                added = await self._scrape_and_record(
                    state,
                    url=next_url,
                    root_url=website,
                    log_message=f"Scraping page {next_page_number} of {self.settings.max_pages_to_scrape}",
                    selected_for_reason=ranked_link.reason,
                    progress_callback=progress_callback,
                )
                if not added:
                    continue
                self._log(
                    state,
                    f"Discovered {len(self._candidate_links_for_site(state, website))} remaining internal link candidate(s)",
                    stage="scraping",
                    url=website,
                    progress_callback=progress_callback,
                )
                ranking = self._rank_links(
                    state,
                    website=website,
                    candidate_links=self._candidate_links_for_site(state, website),
                    progress_callback=progress_callback,
                )
                if ranking.homepage_enough:
                    self._log(
                        state,
                        f"AI says enough information is available: {ranking.reason_homepage_enough}",
                        stage="ai",
                        url=website,
                        progress_callback=progress_callback,
                    )
                    break
                self._log(
                    state,
                    "AI says more evidence may help; continuing with selected internal pages",
                    stage="ai",
                    url=website,
                    progress_callback=progress_callback,
                )
                self._log_selected_links(
                    state,
                    ranking.ranked_links,
                    url=website,
                    progress_callback=progress_callback,
                )
                break

        card = self._create_card_with_evidence(
            state, website=website, progress_callback=progress_callback
        )

        state.set_card(card)
        self._log(
            state,
            "Prospect card created",
            stage="output",
            url=website,
            progress_callback=progress_callback,
        )
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
        selected_for_reason: str = "",
        progress_callback: ProgressCallback | None,
    ) -> bool:
        self._log(
            state,
            log_message,
            stage="scraping",
            url=url,
            progress_callback=progress_callback,
        )
        outcome = await self.scraper.scrape_page(url, root_url=root_url)
        if outcome.blocked:
            self._record_skipped_page(
                state,
                url=url,
                final_url=outcome.final_url or url,
                status_code=outcome.status_code,
                blocked=True,
                error=outcome.error,
                selected_for_reason=selected_for_reason,
            )
            message = self.settings.captcha_end_message or outcome.error
            error = self._error(
                state,
                stage="Scraping",
                message=message,
                url=url,
                details={
                    "error_type": BLOCKED_ERROR_TYPE,
                    "error_message": _blocked_error_message(outcome.error),
                    "resolved": "No",
                    "raw_error": outcome.error,
                },
                progress_callback=progress_callback,
            )
            self._append_error_to_sheet(error)
            return False
        if outcome.error:
            self._record_skipped_page(
                state,
                url=url,
                final_url=outcome.final_url or url,
                status_code=outcome.status_code,
                blocked=False,
                error=outcome.error,
                selected_for_reason=selected_for_reason,
            )
            self._error(
                state,
                stage="scraping",
                message=outcome.error,
                url=url,
                progress_callback=progress_callback,
            )
            return False
        if outcome.page is None:
            self._record_skipped_page(
                state,
                url=url,
                final_url=outcome.final_url or url,
                status_code=outcome.status_code,
                blocked=False,
                error="Website returned no readable page.",
                selected_for_reason=selected_for_reason,
            )
            self._error(
                state,
                stage="scraping",
                message="Website returned no readable page.",
                url=url,
                progress_callback=progress_callback,
            )
            return False
        if not outcome.page.text.strip():
            self._record_skipped_page(
                state,
                url=outcome.page.url,
                final_url=outcome.page.final_url,
                status_code=outcome.page.status_code or outcome.page.status,
                blocked=False,
                error="Empty scraped content.",
                selected_for_reason=selected_for_reason,
            )
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

    def _rank_links(
        self,
        state: ProspectAnalysisState,
        *,
        website: str,
        candidate_links: list[dict[str, Any]],
        progress_callback: ProgressCallback | None,
    ) -> Any:
        self._log(
            state,
            f"AI is ranking {len(candidate_links)} discovered internal link candidate(s)",
            stage="ai",
            url=website,
            progress_callback=progress_callback,
        )
        ranking = self.ai.rank_internal_links(
            state,
            website=website,
            candidate_links=candidate_links,
        )
        if ranking.homepage_enough:
            return ranking
        self._log(
            state,
            f"AI selected {len(ranking.ranked_links)} useful page(s) for possible scraping",
            stage="ai",
            url=website,
            progress_callback=progress_callback,
        )
        return ranking

    def _create_card_with_evidence(
        self,
        state: ProspectAnalysisState,
        *,
        website: str,
        reanalysis_instruction: str = "",
        progress_callback: ProgressCallback | None,
    ) -> Any:
        try:
            chunks = self.ai.build_evidence_chunks(state, website=website)
            state.set_evidence_chunks(chunks)
            self._log(
                state,
                f"Created {len(chunks)} evidence chunk(s)",
                stage="evidence",
                url=website,
                progress_callback=progress_callback,
            )
            if not chunks:
                self._log(
                    state,
                    "Evidence extraction failed or returned weak support.",
                    stage="evidence",
                    url=website,
                    progress_callback=progress_callback,
                )
        except Exception as exc:  # noqa: BLE001
            chunks = []
            self._error(
                state,
                stage="evidence",
                message=f"Evidence extraction failed or returned weak support: {exc}",
                url=website,
                progress_callback=progress_callback,
            )
        card = self.ai.create_prospect_card(
            state,
            website=website,
            reanalysis_instruction=reanalysis_instruction,
            evidence_chunks=chunks,
        )
        summary = card.evidence_validation_summary or {}
        returned_count = int(summary.get("returned_count") or 0)
        valid_count = int(summary.get("valid_count") or len(card.evidence_snippets))
        weak_fields = list(
            summary.get("weak_evidence_fields") or card.weak_evidence_fields or []
        )
        self._log(
            state,
            f"AI returned {returned_count} evidence snippet(s)",
            stage="evidence",
            url=website,
            progress_callback=progress_callback,
        )
        self._log(
            state,
            f"Validated {valid_count} of {returned_count} evidence snippet(s)",
            stage="evidence",
            url=website,
            progress_callback=progress_callback,
        )
        if weak_fields:
            self._log(
                state,
                f"{len(weak_fields)} field(s) need human review due to weak evidence",
                stage="evidence",
                url=website,
                progress_callback=progress_callback,
            )
        return card

    def _log_selected_links(
        self,
        state: ProspectAnalysisState,
        ranked_links: list[Any],
        *,
        url: str,
        progress_callback: ProgressCallback | None,
    ) -> None:
        for ranked_link in ranked_links:
            expected = (
                f" Expected: {ranked_link.expected_information}"
                if ranked_link.expected_information
                else ""
            )
            self._log(
                state,
                f"Rank {ranked_link.priority}: {ranked_link.url} | {ranked_link.reason}{expected}",
                stage="ai",
                url=url,
                progress_callback=progress_callback,
            )

    def _record_skipped_page(
        self,
        state: ProspectAnalysisState,
        *,
        url: str,
        final_url: str,
        status_code: int,
        blocked: bool,
        error: str,
        selected_for_reason: str,
    ) -> None:
        state.add_skipped_page(
            ScrapedPage(
                url=url,
                final_url=final_url or url,
                title="",
                text="",
                metadata={},
                links=[],
                discovered_links=[],
                selected_for_reason=selected_for_reason,
                blocked=blocked,
                error=error,
                status_code=status_code,
                status=status_code,
                strategy="blocked" if blocked else "skipped",
            )
        )
        self.state_logger.write_state_snapshot(state)

    def _candidate_links_for_site(
        self, state: ProspectAnalysisState, website: str
    ) -> list[dict[str, Any]]:
        scraped = {
            str(value or "").strip().lower()
            for page in self._pages_for_site(state, website)
            for value in (page.url, page.final_url)
            if str(value or "").strip()
        }
        skipped = {
            str(value or "").strip().lower()
            for page in self._skipped_pages_for_site(state, website)
            for value in (page.url, page.final_url)
            if str(value or "").strip()
        }
        found: dict[str, dict[str, Any]] = {}
        for page in self._pages_for_site(state, website):
            for link in page.discovered_links or page.links:
                url = str(link.get("url") or "").strip()
                if not url or url.lower() in scraped or url.lower() in skipped:
                    continue
                existing = found.get(url)
                if existing is None or _safe_structural_score(
                    link.get("structural_score")
                ) > _safe_structural_score(existing.get("structural_score")):
                    found[url] = dict(link)
        return sorted(
            found.values(),
            key=lambda item: _safe_structural_score(item.get("structural_score")),
            reverse=True,
        )

    def _unseen_ranked_links(
        self,
        state: ProspectAnalysisState,
        website: str,
        ranked_links: list[Any],
        attempted_urls: set[str],
    ) -> list[Any]:
        unavailable = {
            str(value or "").strip().lower()
            for page in [
                *self._pages_for_site(state, website),
                *self._skipped_pages_for_site(state, website),
            ]
            for value in (page.url, page.final_url)
            if str(value or "").strip()
        } | {url.lower() for url in attempted_urls}
        selected: list[Any] = []
        seen: set[str] = set()
        for ranked_link in ranked_links:
            url = str(getattr(ranked_link, "url", "") or "").strip()

            key = url.lower()
            if not url or key in unavailable or key in seen:
                continue
            selected.append(ranked_link)
            seen.add(key)
        return selected

    @staticmethod
    def _pages_for_site(state: ProspectAnalysisState, website: str) -> list[Any]:
        from prospect_system.prospect_ai_analyzer import _same_site

        pages = [
            page
            for page in state.scraped_pages
            if _same_site(page.final_url or page.url, website)
        ]
        return pages or []

    @staticmethod
    def _skipped_pages_for_site(
        state: ProspectAnalysisState, website: str
    ) -> list[Any]:
        from prospect_system.prospect_ai_analyzer import _same_site

        return [
            page
            for page in state.skipped_pages
            if _same_site(page.final_url or page.url, website)
        ]

    def _log(
        self,
        state: ProspectAnalysisState,
        message: str,
        *,
        stage: str,
        url: str = "",
        progress_callback: ProgressCallback | None,
    ) -> None:
        eta_seconds = state.estimate_remaining_seconds(
            max_pages_to_scrape=self.settings.max_pages_to_scrape
        )
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
    ) -> Any:
        error = state.add_error(stage=stage, message=message, url=url, details=details)
        self.state_logger.write_error(error)
        if state.ai_step_logs:
            self.state_logger.write_step(state, state.ai_step_logs[-1])
        self.state_logger.write_state_snapshot(state)
        self._emit(progress_callback, state)
        return error

    def _append_error_to_sheet(self, error: Any) -> None:
        try:
            result = GoogleSheetsClient(self.settings).append_error(error)
        except Exception as exc:  # noqa: BLE001
            self.logger.warning(
                "[prospect-sheets] failed to save scraping error: %s", str(exc)
            )
            return
        if not result.success:
            self.logger.warning("[prospect-sheets] %s", result.message)

    @staticmethod
    def _emit(
        progress_callback: ProgressCallback | None, state: ProspectAnalysisState
    ) -> None:
        if progress_callback is None:
            return
        progress_callback(state)


def _blocked_error_message(raw_error: str) -> str:
    match = re.search(r"\bHTTP\s+(\d{3})\b", str(raw_error or ""), flags=re.IGNORECASE)
    if match:
        return f"HTTP {match.group(1)}"
    return str(raw_error or "Blocked or forbidden response").strip()
