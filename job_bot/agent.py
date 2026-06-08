from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
import time
from typing import Optional

from job_bot.ai_client import AIClient
from job_bot.browser_tabs import BrowserTabManager
from job_bot.config import Settings
from job_bot.discovery import discover_candidates
from job_bot.extractors import (
    extract_domain_opportunity_card,
    extract_structured_opportunity_card,
    local_fallback_card,
    page_to_text,
)
from job_bot.fetching import ResilientFetcher
from job_bot.models import CycleStats
from job_bot.notifier import TelegramNotifier
from job_bot.output_writer import CardWriter
from job_bot.page_classifier import classify_page_kind, is_opportunity_detail_kind
from job_bot.site_profiles import get_site_profile, url_matches_profile_pattern
from job_bot.storage import LinkState, StateStore
from job_bot.subscriber_notifier import SubscriberNotifier
from job_bot.subscription_admin_notifier import SubscriptionAdminNotifier


class SiteAgent:
    def __init__(
        self,
        name: str,
        website: str,
        settings: Settings,
        logger: logging.Logger,
        fetcher: ResilientFetcher,
        ai_client: AIClient,
        store: StateStore,
        writer: CardWriter,
        notifier: Optional[TelegramNotifier] = None,
        review_notifier: Optional[TelegramNotifier] = None,
        subscriber_notifier: Optional[SubscriberNotifier] = None,
        admin_notifier: Optional[SubscriptionAdminNotifier] = None,
    ) -> None:
        self.name = name
        self.website = website
        self.settings = settings
        self.logger = logger
        self.fetcher = fetcher
        self.ai_client = ai_client
        self.store = store
        self.writer = writer
        self.notifier = notifier
        self.review_notifier = review_notifier
        self.subscriber_notifier = subscriber_notifier
        self.admin_notifier = admin_notifier
        self.tab_manager = BrowserTabManager(settings=settings, logger=logger)
        self._cycle_progress_at = 0.0
        self._cycle_progress_note = ""
        self._cycle_stage = "idle"
        self._cycle_stage_detail = ""
        self._cycle_stage_started_at = 0.0
        self._cycle_stage_progress_at = 0.0
        self._cycle_stage_budget_seconds = 0.0

    def _enter_stage(self, stage: str, detail: str, *, budget_seconds: float) -> None:
        now = time.monotonic()
        self._cycle_stage = stage.strip() or "unknown"
        self._cycle_stage_detail = detail.strip()
        self._cycle_stage_started_at = now
        self._cycle_stage_progress_at = now
        self._cycle_stage_budget_seconds = max(1.0, float(budget_seconds))
        self._cycle_progress_at = now
        self._cycle_progress_note = f"{self._cycle_stage}: {self._cycle_stage_detail}".strip()

    def _mark_cycle_progress(self, note: str, *, busy_for_seconds: float = 0.0) -> None:
        del busy_for_seconds
        now = time.monotonic()
        cleaned_note = note.strip()
        self._cycle_progress_at = now
        self._cycle_progress_note = cleaned_note
        self._cycle_stage_progress_at = now
        if cleaned_note:
            self._cycle_stage_detail = cleaned_note

    def _mark_cycle_busy(self, note: str, *, busy_for_seconds: float) -> None:
        self._mark_cycle_progress(note)
        self._cycle_stage_budget_seconds = max(
            self._cycle_stage_budget_seconds,
            max(1.0, float(busy_for_seconds)),
        )

    def _expected_fetch_wait_seconds(self, multiplier: float = 1.0, extra_seconds: float = 20.0) -> float:
        request_seconds = max(1.0, float(self.settings.request_timeout_ms) / 1000.0)
        return (request_seconds * max(1.0, multiplier)) + max(0.0, extra_seconds)

    def _expected_model_wait_seconds(self, base_seconds: float = 120.0) -> float:
        return max(30.0, base_seconds)

    def _candidate_fetch_budget_seconds(self) -> float:
        fetch_timeout_seconds = max(1.0, float(getattr(self.settings, "fetch_strategy_timeout_ms", self.settings.request_timeout_ms)) / 1000.0)
        retry_budget = max(0, int(getattr(self.settings, "network_retry_budget", 0)))
        strategy_budget = fetch_timeout_seconds * max(1, retry_budget + 1)
        return max(15.0, strategy_budget + 10.0)

    def _adaptive_seen_streak_stop(self, prioritized_candidates) -> int:
        base_stop = max(1, int(self.settings.seen_streak_stop))
        total_candidates = len(prioritized_candidates)
        terminal_candidates = sum(1 for _, state in prioritized_candidates if self._is_terminal_state(state))
        unchecked_candidates = max(0, total_candidates - terminal_candidates)
        adaptive_stop = base_stop
        adaptive_stop += min(6, total_candidates // 20)
        if terminal_candidates >= max(10, unchecked_candidates * 2):
            adaptive_stop += 4
        elif terminal_candidates > unchecked_candidates:
            adaptive_stop += 2
        if unchecked_candidates <= 5 and terminal_candidates >= 10:
            adaptive_stop += 2
        return min(18, max(base_stop, adaptive_stop))

    @staticmethod
    def _record_neglect(stats: CycleStats, reason_key: str) -> None:
        normalized_reason = str(reason_key or "").strip().lower() or "other"
        stats.neglected_posts += 1
        stats.neglect_reasons[normalized_reason] = stats.neglect_reasons.get(normalized_reason, 0) + 1

    def _handle_low_confidence_opportunity(self, *, candidate_url: str, chosen_card, stats: CycleStats) -> bool:
        if not self.settings.enable_human_review_queue:
            return False

        confidence_score = float(getattr(chosen_card, "confidence", 0.0) or 0.0)
        min_review_confidence = float(
            getattr(self.settings, "human_review_min_confidence_threshold", 0.30) or 0.30
        )
        max_review_confidence = float(self.settings.human_review_confidence_threshold)

        if confidence_score < min_review_confidence:
            self.store.mark_status(
                self.website,
                candidate_url,
                status="neglected_low_confidence",
                neglected=True,
            )
            self._record_neglect(stats, "low_confidence")
            self.logger.info(
                "[%s] neglected low-confidence opportunity url=%s confidence=%.3f threshold=%.3f",
                self.name,
                candidate_url,
                confidence_score,
                min_review_confidence,
            )
            self._mark_cycle_progress(f"neglected low confidence {candidate_url}")
            return True

        if confidence_score >= max_review_confidence:
            return False

        try:
            review_id = self.store.queue_human_review_item(
                chosen_card,
                reason=(
                    "low_confidence_extraction:"
                    f"{confidence_score:.3f} in "
                    f"[{min_review_confidence:.3f}, {max_review_confidence:.3f})"
                ),
                source=chosen_card.extraction_method,
            )
            self.store.mark_status(self.website, candidate_url, status="queued_human_review")
            self.logger.info(
                "[%s] queued human review id=%s url=%s confidence=%.3f",
                self.name,
                review_id,
                candidate_url,
                confidence_score,
            )
            self._mark_cycle_progress(f"queued human review {candidate_url}")
            return True
        except Exception as exc:  # noqa: BLE001
            self.logger.warning(
                "[%s] failed to queue human review for %s: %s",
                self.name,
                candidate_url,
                str(exc),
            )
            return False

    @staticmethod
    def _is_terminal_state(state: LinkState | None) -> bool:
        if state is None:
            return False
        return state.neglected or state.status in {"job_saved", "queued_human_review"}

    def _prioritize_candidates(self, candidates):
        prioritized: list[tuple[object, LinkState | None]] = []
        terminal: list[tuple[object, LinkState | None]] = []
        for candidate in candidates:
            state = self.store.get_link_state(self.website, candidate.url)
            item = (candidate, state)
            if self._is_terminal_state(state):
                terminal.append(item)
            else:
                prioritized.append(item)
        if terminal and prioritized:
            self.logger.info(
                "[%s] deprioritized %s terminal candidates so %s unchecked candidates are evaluated first",
                self.name,
                len(terminal),
                len(prioritized),
            )
        return prioritized + terminal

    @staticmethod
    def _is_expected_navigation_error(exc: Exception) -> bool:
        # Playwright navigation/network failures are expected in normal crawling
        # (down hosts, DNS failures, temporary blocks). Keep logs concise.
        text = str(exc).lower()
        if "page.goto" not in text:
            return False
        markers = (
            "net::err_",
            "name_not_resolved",
            "timeout",
            "timed out",
            "connection",
            "ssl",
        )
        return any(marker in text for marker in markers)

    def _manual_challenge_enabled(self) -> bool:
        return bool(getattr(self.settings, "enable_manual_challenge_flow", False))

    @staticmethod
    def _is_challenge_error(error: str) -> bool:
        return str(error or "").strip().lower() == "challenge_or_interstitial"

    @staticmethod
    def _is_usable_tab_result(tab_result) -> bool:
        fetch_result = getattr(tab_result, "fetch_result", None)
        if not bool(getattr(tab_result, "success", False)) or fetch_result is None:
            return False
        return 200 <= int(getattr(fetch_result, "status", 0)) < 400

    async def _notify_manual_challenge(self, *, url: str, source: str) -> None:
        if self.admin_notifier is None:
            return
        event_key = f"manual_challenge|{self.website}|{source}|{url}"
        detected_at_utc = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        message = (
            "Manual challenge required\n"
            f"Website: {self.website}\n"
            f"URL: {url}\n"
            f"Detected during: {source}\n"
            "Detected: Cloudflare / Turnstile challenge page\n"
            "Action: a headful browser was opened for manual solve; complete the challenge there and crawling will resume automatically\n"
            f"Detected (UTC): {detected_at_utc}"
        )
        try:
            await self.admin_notifier.notify_admin_message(event_key=event_key, message=message)
        except Exception as exc:  # noqa: BLE001
            self.logger.warning(
                "[%s] failed to send manual challenge notification for %s: %s",
                self.name,
                url,
                str(exc),
            )

    async def _maybe_resolve_challenge_manually(self, url: str, *, source: str) -> bool:
        if not self._manual_challenge_enabled():
            return False
        self.logger.warning(
            "[%s] challenge detected via %s for %s; switching to headful manual mode",
            self.name,
            source,
            url,
        )
        await self._notify_manual_challenge(url=url, source=source)
        self._mark_cycle_busy(
            f"waiting for manual challenge solve {url}",
            busy_for_seconds=max(
                30.0,
                float(getattr(self.settings, "manual_challenge_timeout_seconds", 900)) + 10.0,
            ),
        )
        solved = await self.tab_manager.resolve_challenge_manually(url)
        if solved:
            self.logger.info("[%s] manual challenge solved for %s", self.name, url)
            self._mark_cycle_progress(f"manual challenge solved {url}")
            return True
        self.logger.warning("[%s] manual challenge was not solved for %s", self.name, url)
        self._mark_cycle_progress(f"manual challenge unresolved {url}")
        return False

    async def _retry_after_manual_challenge(self, url: str, *, source: str):
        if not await self._maybe_resolve_challenge_manually(url, source=source):
            return None
        retry_result = await self.tab_manager.fetch_in_new_tab(url)
        retry_page = getattr(retry_result, "fetch_result", None)
        if self._is_usable_tab_result(retry_result):
            return retry_page
        self.logger.warning(
            "[%s] manual challenge solve completed but retry fetch stayed unusable (status=%s error=%s) for %s",
            self.name,
            getattr(retry_page, "status", "n/a") if retry_page is not None else "n/a",
            getattr(retry_result, "error", ""),
            url,
        )
        return None

    async def _fetch_post_page(self, url: str):
        self._enter_stage("fetching", f"candidate fetch {url}", budget_seconds=self._candidate_fetch_budget_seconds())
        manual_attempted = False
        if bool(getattr(self.settings, "enable_browser_tabs", True)):
            tab_result = await self.tab_manager.fetch_in_new_tab(url)
            post_page = tab_result.fetch_result
            if self._is_usable_tab_result(tab_result):
                self.logger.info(
                    "[%s] new-tab fetch success status=%s url=%s",
                    self.name,
                    post_page.status,
                    post_page.url,
                )
                return post_page
            if self._is_challenge_error(getattr(tab_result, "error", "")) and self._manual_challenge_enabled():
                manual_attempted = True
                retried_post_page = await self._retry_after_manual_challenge(
                    url,
                    source="candidate new-tab fetch",
                )
                if retried_post_page is not None:
                    return retried_post_page

            self.logger.warning(
                "[%s] new-tab fetch unusable (status=%s) for %s, fallback to resilient fetcher",
                self.name,
                post_page.status if post_page is not None else "n/a",
                url,
            )
        self._mark_cycle_progress(f"fallback fetch {url}")
        post_page = await self.fetcher.fetch(url)
        if post_page is not None:
            return post_page
        if (
            not manual_attempted
            and self._manual_challenge_enabled()
            and getattr(self.fetcher, "last_failure_reason", lambda _: "")(url) == "challenge_or_interstitial"
        ):
            manual_attempted = True
            retried_post_page = await self._retry_after_manual_challenge(
                url,
                source="candidate resilient fetch",
            )
            if retried_post_page is not None:
                return retried_post_page
        return post_page

    async def _fetch_discovery_page(self, url: str):
        self._enter_stage("discovering", f"discovery fetch {url}", budget_seconds=self._candidate_fetch_budget_seconds())
        manual_attempted = False
        if bool(getattr(self.settings, "enable_browser_tabs", True)):
            tab_result = await self.tab_manager.fetch_in_new_tab(url)
            post_page = tab_result.fetch_result
            if self._is_usable_tab_result(tab_result):
                self.logger.info(
                    "[%s] discovery fetch via tab status=%s url=%s",
                    self.name,
                    post_page.status,
                    post_page.url,
                )
                return post_page
            if self._is_challenge_error(getattr(tab_result, "error", "")) and self._manual_challenge_enabled():
                manual_attempted = True
                retried_post_page = await self._retry_after_manual_challenge(
                    url,
                    source="discovery new-tab fetch",
                )
                if retried_post_page is not None:
                    return retried_post_page
        self._mark_cycle_progress(f"discovery fallback fetch {url}")
        post_page = await self.fetcher.fetch(url)
        if post_page is not None:
            return post_page
        if (
            not manual_attempted
            and self._manual_challenge_enabled()
            and getattr(self.fetcher, "last_failure_reason", lambda _: "")(url) == "challenge_or_interstitial"
        ):
            retried_post_page = await self._retry_after_manual_challenge(
                url,
                source="discovery resilient fetch",
            )
            if retried_post_page is not None:
                return retried_post_page
        return post_page

    async def _extract_job_card_from_page(
        self,
        url: str,
        post_page,
        *,
        page_text: str | None = None,
        page_kind: str = "",
    ):
        page_text = page_text if page_text is not None else page_to_text(post_page.page)
        html = str(getattr(post_page, "html", "") or "")

        self._enter_stage("extracting", f"structured extract {url}", budget_seconds=10.0)
        structured_card = extract_structured_opportunity_card(
            website=self.website,
            url=url,
            page=post_page.page,
            page_kind=page_kind,
            page_text=page_text,
            html=html,
        )
        if structured_card is not None and getattr(structured_card, "is_relevant_opportunity", structured_card.is_job_post):
            return structured_card

        self._enter_stage("extracting", f"dom extract {url}", budget_seconds=10.0)
        dom_card = extract_domain_opportunity_card(
            website=self.website,
            url=url,
            page=post_page.page,
            page_kind=page_kind,
            page_text=page_text,
            html=html,
        )
        if dom_card is not None and getattr(dom_card, "is_relevant_opportunity", dom_card.is_job_post):
            return dom_card

        ai_card = None
        review_guidance = ""
        if self.settings.enable_human_review_queue:
            try:
                review_guidance = self.store.build_review_guidance(limit=12)
            except Exception as exc:  # noqa: BLE001
                self.logger.warning("[%s] could not load review guidance: %s", self.name, str(exc))
        try:
            self._enter_stage(
                "extracting",
                f"ai extract {url}",
                budget_seconds=max(10.0, float(self.settings.extraction_timeout_seconds)),
            )
            ai_card = await self.ai_client.extract_job_card(
                website=self.website,
                url=url,
                page_text=page_text,
                html=html,
                review_guidance=review_guidance,
            )
        except Exception as exc:  # noqa: BLE001
            self.logger.warning(
                "[%s] AI extraction failed for %s: %s",
                self.name,
                url,
                str(exc),
            )

        if ai_card is not None and getattr(ai_card, "is_relevant_opportunity", ai_card.is_job_post):
            return ai_card
        if ai_card is None:
            if not bool(getattr(self.settings, "reduced_quality_fallback_mode", True)):
                return None
            return local_fallback_card(
                website=self.website,
                url=url,
                page=post_page.page,
                page_kind=page_kind,
                page_text=page_text,
                html=html,
            )
        return None

    async def _fetch_clicked_card_detail(self, candidate) -> object | None:
        source_page = str(getattr(candidate, "source_page", "") or "").strip()
        if not source_page or source_page == candidate.url:
            return None
        profile = get_site_profile(candidate.url) if bool(getattr(self.settings, "enable_site_profiles", True)) else None
        if profile is not None and not profile.allow_click_detail:
            self.logger.info(
                "[%s] skipping click-detail for %s because site profile=%s disables generic click navigation",
                self.name,
                candidate.url,
                profile.key,
            )
            return None
        if profile is not None and url_matches_profile_pattern(candidate.url, profile.blocked_detail_url_patterns):
            self.logger.info(
                "[%s] skipping click-detail for %s because site profile=%s marks the target as blocked detail",
                self.name,
                candidate.url,
                profile.key,
            )
            return None
        if not bool(getattr(self.settings, "enable_browser_tabs", True)):
            return None
        self._enter_stage(
            "fetching",
            f"card click detail {candidate.url}",
            budget_seconds=self._candidate_fetch_budget_seconds(),
        )
        tab_result = await self.tab_manager.fetch_card_detail_via_click(source_page, candidate.url)
        detail_page = tab_result.fetch_result
        if tab_result.success and detail_page is not None:
            return detail_page
        if self._is_challenge_error(getattr(tab_result, "error", "")) and self._manual_challenge_enabled():
            retried_detail_page = await self._retry_after_manual_challenge(
                candidate.url,
                source="card click detail fetch",
            )
            if retried_detail_page is not None:
                return retried_detail_page
        if tab_result.error:
            self.logger.info(
                "[%s] card-click detail fallback unavailable for %s: %s",
                self.name,
                candidate.url,
                tab_result.error,
            )
        return None

    async def _extract_job_card_from_candidate(self, candidate, post_page):
        if not hasattr(post_page, "page") or not hasattr(post_page, "html") or not hasattr(post_page, "url"):
            return await self._extract_job_card_from_page(candidate.url, post_page)

        page_text = page_to_text(post_page.page)
        page_kind = classify_page_kind(post_page.url, html=post_page.html, text=page_text)

        if is_opportunity_detail_kind(page_kind):
            direct_card = await self._extract_job_card_from_page(
                candidate.url,
                post_page,
                page_text=page_text,
                page_kind=page_kind,
            )
            if direct_card is not None and getattr(direct_card, "is_relevant_opportunity", direct_card.is_job_post):
                return direct_card
        else:
            self.logger.info(
                "[%s] direct candidate page classified as %s for %s",
                self.name,
                page_kind,
                candidate.url,
            )

        detail_page = await self._fetch_clicked_card_detail(candidate)
        if detail_page is None:
            return None

        detail_text = page_to_text(detail_page.page)
        detail_kind = classify_page_kind(detail_page.url, html=detail_page.html, text=detail_text)
        if not is_opportunity_detail_kind(detail_kind):
            self.logger.info(
                "[%s] clicked card detail classified as %s for %s",
                self.name,
                detail_kind,
                candidate.url,
            )
            return None

        return await self._extract_job_card_from_page(
            candidate.url,
            detail_page,
            page_text=detail_text,
            page_kind=detail_kind,
        )

    @staticmethod
    def _parse_iso_datetime(value: str) -> datetime | None:
        raw = value.strip()
        if not raw:
            return None
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            try:
                parsed = datetime.strptime(raw, "%Y-%m-%d")
                parsed = parsed.replace(tzinfo=timezone.utc)
            except ValueError:
                return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    async def _should_neglect_old_post(self, cycle_utc: str, post_url: str, post_page, chosen_card) -> tuple[bool, str]:
        max_age_days = int(self.settings.max_job_post_age_days)
        if max_age_days < 1:
            return (False, "Age filter disabled.")
        if not bool(getattr(self.settings, "enable_ai_age_check", True)):
            return (False, "Age check skipped: AI age-check feature flag is disabled.")
        if not hasattr(self.ai_client, "assess_post_age"):
            return (False, "Age check skipped: AI client does not support post-age assessment.")
        try:
            self._enter_stage(
                "age-checking",
                f"age check {post_url}",
                budget_seconds=max(5.0, float(self.settings.age_check_timeout_seconds)),
            )
            age_assessment = await self.ai_client.assess_post_age(
                website=self.website,
                url=post_url,
                page_text=page_to_text(post_page.page),
                html=post_page.html,
                max_age_days=max_age_days,
                now_utc_iso=cycle_utc,
            )
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("[%s] post-age AI check failed for %s: %s", self.name, post_url, str(exc))
            return (False, "Age check failed; post kept.")

        if age_assessment.detected_posted_at_utc and not chosen_card.posted_at_utc:
            chosen_card.posted_at_utc = age_assessment.detected_posted_at_utc
        if age_assessment.reason:
            chosen_card.notes = (
                f"{chosen_card.notes} | age_check={age_assessment.reason}".strip(" |")
                if chosen_card.notes
                else f"age_check={age_assessment.reason}"
            )
        if age_assessment.older_than_limit:
            return (True, f"AI marked post older than {max_age_days} days: {age_assessment.reason}")

        # Deterministic backstop if AI extracted an absolute posted date.
        posted_dt = self._parse_iso_datetime(chosen_card.posted_at_utc)
        cycle_dt = self._parse_iso_datetime(cycle_utc)
        if posted_dt is not None and cycle_dt is not None:
            if posted_dt < (cycle_dt - timedelta(days=max_age_days)):
                return (
                    True,
                    (
                        f"Posted at {posted_dt.isoformat()} which is older than "
                        f"{max_age_days} days from cycle time {cycle_dt.isoformat()}."
                    ),
                )
        return (False, age_assessment.reason or "Age check passed.")

    async def _send_pending_notifications(self) -> None:
        if self.notifier is None:
            return

        pending_urls = self.store.get_pending_telegram_links(self.website, limit=10)
        if not pending_urls:
            return
        self.logger.info("[%s] pending telegram notifications: %s", self.name, len(pending_urls))

        for pending_url in pending_urls:
            self._enter_stage("delivering", f"pending notification {pending_url}", budget_seconds=60.0)
            self._mark_cycle_progress(f"pending notification fetch {pending_url}")
            post_page = await self._fetch_post_page(pending_url)
            if post_page is None:
                continue
            self._mark_cycle_progress(f"pending notification extract {pending_url}")
            pending_text = page_to_text(post_page.page)
            pending_kind = classify_page_kind(post_page.url, html=post_page.html, text=pending_text)
            card = await self._extract_job_card_from_page(
                pending_url,
                post_page,
                page_text=pending_text,
                page_kind=pending_kind,
            )
            if card is None or not getattr(card, "is_relevant_opportunity", card.is_job_post):
                continue
            try:
                await self.notifier.send_card(card)
                self.store.mark_telegram_sent(self.website, pending_url)
                self._mark_cycle_progress(f"pending notification sent {pending_url}")
                self.logger.info("[%s] backfill telegram sent for %s", self.name, pending_url)
            except Exception as exc:  # noqa: BLE001
                self.logger.warning(
                    "[%s] backfill telegram send failed for %s: %s",
                    self.name,
                    pending_url,
                    str(exc),
                )

    async def run_cycle(self, cycle_utc: str, budget: "CycleBudget") -> CycleStats:
        stats = CycleStats(website=self.website)
        seen_streak = 0

        try:
            self._enter_stage("discovering", f"cycle started on {self.website}", budget_seconds=60.0)
            self.logger.info("[%s] starting cycle on %s", self.name, self.website)
            if bool(getattr(self.settings, "enable_browser_tabs", True)):
                self._enter_stage(
                    "fetching",
                    f"starting browser tab manager for {self.website}",
                    budget_seconds=self._candidate_fetch_budget_seconds(),
                )
                await self.tab_manager.start(self.website)
                self._mark_cycle_progress("browser tab manager started")
            await self._send_pending_notifications()

            discovery_feedback = None
            get_discovery_feedback = getattr(self.store, "get_discovery_feedback", None)
            if callable(get_discovery_feedback):
                discovery_feedback = get_discovery_feedback(self.website)

            self._enter_stage(
                "discovering",
                f"discovering candidates on {self.website}",
                budget_seconds=max(
                    30.0,
                    self._candidate_fetch_budget_seconds() * max(1, int(self.settings.max_seed_pages_per_site)),
                ),
            )
            discovery = await discover_candidates(
                website=self.website,
                fetcher=self.fetcher,
                ai_client=self.ai_client,
                settings=self.settings,
                logger=self.logger,
                interactive_fetch=self._fetch_discovery_page,
                discovery_feedback=discovery_feedback,
                discovery_event_logger=getattr(self.store, "record_discovery_page_outcome", None),
            )
            self._mark_cycle_progress(
                f"discovery finished pages={discovery.fetched_pages} candidates={len(discovery.candidates)}"
            )
            set_runtime_metric = getattr(self.store, "set_runtime_metric", None)
            if callable(set_runtime_metric):
                set_runtime_metric("crawler.last_discovered_candidates", float(len(discovery.candidates)))
            stats.fetched_pages += discovery.fetched_pages
            stats.discovered_links = len(discovery.candidates)
            self.logger.info(
                "[%s] discovered %s candidate links (from %s pages)",
                self.name,
                len(discovery.candidates),
                discovery.fetched_pages,
            )

            prioritized_candidates = self._prioritize_candidates(discovery.candidates)
            adaptive_seen_streak_stop = self._adaptive_seen_streak_stop(prioritized_candidates)
            self.logger.info(
                "[%s] adaptive terminal streak stop=%s (configured=%s candidates=%s)",
                self.name,
                adaptive_seen_streak_stop,
                self.settings.seen_streak_stop,
                len(prioritized_candidates),
            )

            for candidate, state in prioritized_candidates:
                self._enter_stage("discovering", f"candidate loop {candidate.url}", budget_seconds=60.0)
                if await budget.is_exhausted():
                    self.logger.info("[%s] global cycle card budget exhausted", self.name)
                    break

                if self._is_terminal_state(state):
                    seen_streak += 1
                    stats.seen_links_in_row = seen_streak
                    observed_status = state.status if state else "unknown"
                    if hasattr(self.store, "record_link_observation"):
                        self.store.record_link_observation(
                            self.website,
                            candidate.url,
                            status="already_scraped_seen",
                            detail=observed_status,
                        )
                    self.logger.info(
                        "[%s] terminal link streak=%s status=%s url=%s",
                        self.name,
                        seen_streak,
                        observed_status,
                        candidate.url,
                    )
                    if seen_streak >= adaptive_seen_streak_stop:
                        self.logger.info(
                            "[%s] stopping scan after %s terminal links in a row (adaptive stop=%s)",
                            self.name,
                            seen_streak,
                            adaptive_seen_streak_stop,
                        )
                        break
                    continue

                seen_streak = 0
                stats.checked_links += 1
                self.store.mark_status(self.website, candidate.url, status="queued")
                self._mark_cycle_progress(f"checking candidate {candidate.url}")
                self.logger.info("[%s] checking candidate %s", self.name, candidate.url)

                post_page = await self._fetch_post_page(candidate.url)

                if post_page is None:
                    self.store.mark_status(self.website, candidate.url, status="fetch_failed")
                    self._mark_cycle_progress(f"fetch failed {candidate.url}")
                    stats.errors += 1
                    continue

                self._mark_cycle_progress(f"fetched candidate {candidate.url}")
                stats.fetched_pages += 1
                chosen_card = await self._extract_job_card_from_candidate(candidate, post_page)
                self._mark_cycle_progress(f"extracted candidate {candidate.url}")

                if chosen_card and getattr(chosen_card, "is_relevant_opportunity", chosen_card.is_job_post):
                    too_old, old_reason = await self._should_neglect_old_post(
                        cycle_utc=cycle_utc,
                        post_url=candidate.url,
                        post_page=post_page,
                        chosen_card=chosen_card,
                    )
                    if too_old:
                        self.store.mark_status(
                            self.website,
                            candidate.url,
                            status="neglected_old_post",
                            neglected=True,
                        )
                        self._record_neglect(stats, "old_post")
                        self.logger.info(
                            "[%s] neglected old post url=%s reason=%s",
                            self.name,
                            candidate.url,
                            old_reason,
                        )
                        continue

                    if self._handle_low_confidence_opportunity(
                        candidate_url=candidate.url,
                        chosen_card=chosen_card,
                        stats=stats,
                    ):
                        continue

                    if await budget.try_take():
                        cluster_registration = None
                        register_cluster = getattr(self.store, "register_job_cluster", None)
                        if callable(register_cluster):
                            try:
                                cluster_registration = register_cluster(chosen_card)
                            except Exception as exc:  # noqa: BLE001
                                self.logger.warning(
                                    "[%s] cluster registration failed for %s: %s",
                                    self.name,
                                    candidate.url,
                                    str(exc),
                                )
                        if cluster_registration is not None and getattr(cluster_registration, "is_duplicate", False):
                            await budget.release()
                            self.store.mark_status(
                                self.website,
                                candidate.url,
                                status="neglected_duplicate_cluster",
                                neglected=True,
                            )
                            self._record_neglect(stats, "duplicate_cluster")
                            self.logger.info(
                                "[%s] duplicate cluster skipped url=%s canonical=%s",
                                self.name,
                                candidate.url,
                                getattr(cluster_registration, "canonical_url", candidate.url),
                            )
                            self._mark_cycle_progress(f"skipped duplicate cluster {candidate.url}")
                            continue

                        if self.subscriber_notifier is not None:
                            queue_method = getattr(self.subscriber_notifier, "queue_job_for_delivery", None)
                            if callable(queue_method):
                                self._enter_stage("queueing", f"queue subscriber delivery {candidate.url}", budget_seconds=30.0)
                                queue_id = queue_method(chosen_card)
                                if queue_id is not None:
                                    self.logger.info(
                                        "[%s] queued subscriber delivery job queue_id=%s url=%s",
                                        self.name,
                                        queue_id,
                                        candidate.url,
                                    )
                                self._mark_cycle_progress(f"queued subscriber delivery {candidate.url}")
                            else:
                                dispatch_outcome = None
                                try:
                                    self._enter_stage(
                                        "delivering",
                                        f"subscriber dispatch {candidate.url}",
                                        budget_seconds=180.0,
                                    )
                                    dispatch_outcome = await self.subscriber_notifier.dispatch_new_job(chosen_card)
                                    self._mark_cycle_progress(f"subscriber dispatch evaluated {candidate.url}")
                                except Exception as exc:  # noqa: BLE001
                                    self.logger.warning(
                                        "[%s] subscriber dispatch failed for %s: %s",
                                        self.name,
                                        candidate.url,
                                        str(exc),
                                    )
                                if (
                                    self.settings.neglect_post_if_filters_miss
                                    and dispatch_outcome is not None
                                    and dispatch_outcome.active_subscribers > 0
                                    and dispatch_outcome.filter_matched_subscribers == 0
                                    and getattr(dispatch_outcome, "human_review_count", 0) == 0
                                ):
                                    await budget.release()
                                    self.store.mark_status(
                                        self.website,
                                        candidate.url,
                                        status="neglected_filter_mismatch",
                                        neglected=True,
                                    )
                                    self._record_neglect(stats, "filter_mismatch")
                                    self.logger.info(
                                        "[%s] neglected by filter mismatch (no subscriber match) url=%s",
                                        self.name,
                                        candidate.url,
                                    )
                                    self._mark_cycle_progress(f"neglected filter mismatch {candidate.url}")
                                    continue

                        self.store.mark_job_saved(self.website, candidate.url)
                        self._enter_stage("queueing", f"saving job card {candidate.url}", budget_seconds=30.0)
                        self.writer.write_card(cycle_utc=cycle_utc, agent_name=self.name, card=chosen_card)
                        self._mark_cycle_progress(f"saved job card {candidate.url}")
                        if self.notifier is not None:
                            try:
                                self._enter_stage("delivering", f"legacy telegram send {candidate.url}", budget_seconds=60.0)
                                await self.notifier.send_card(chosen_card)
                                self.store.mark_telegram_sent(self.website, candidate.url)
                            except Exception as exc:  # noqa: BLE001
                                self.logger.warning(
                                    "[%s] telegram send failed for %s: %s",
                                    self.name,
                                    candidate.url,
                                    str(exc),
                                )
                        stats.new_cards += 1
                        self.logger.info(
                            "[%s] saved job card #%s this cycle: %s",
                            self.name,
                            stats.new_cards,
                            candidate.url,
                        )
                    else:
                        self.store.mark_status(self.website, candidate.url, status="job_found_budget_exhausted")
                        self._mark_cycle_progress(f"budget exhausted at {candidate.url}")
                        self.logger.info("[%s] card budget hit while processing %s", self.name, candidate.url)
                        break
                else:
                    flagged = self.store.increment_not_job_flag(self.website, candidate.url, max_flags=3)
                    self._mark_cycle_progress(f"not-job classified {candidate.url}")
                    self.logger.info(
                        "[%s] not-job flag=%s neglected=%s url=%s",
                        self.name,
                        flagged.not_job_flags,
                        flagged.neglected,
                        candidate.url,
                    )
                    if flagged.neglected and flagged.status == "neglected_not_job":
                        self._record_neglect(stats, "not_job")

        except Exception as exc:  # noqa: BLE001
            stats.errors += 1
            stats.last_error = str(exc)
            if self._is_expected_navigation_error(exc):
                self.logger.error("[%s] cycle failed on %s: %s", self.name, self.website, str(exc))
            else:
                self.logger.exception("[%s] cycle failed on %s: %s", self.name, self.website, str(exc))
        finally:
            self._mark_cycle_progress("closing tab manager")
            if bool(getattr(self.settings, "enable_browser_tabs", True)):
                await self.tab_manager.close()

        return stats


# Used only for type hints in this module.
class CycleBudget:
    async def is_exhausted(self) -> bool:  # pragma: no cover - protocol-only
        raise NotImplementedError

    async def try_take(self) -> bool:  # pragma: no cover - protocol-only
        raise NotImplementedError

    async def release(self, amount: int = 1) -> None:  # pragma: no cover - protocol-only
        raise NotImplementedError
