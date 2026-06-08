from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

try:
    from telegram import Bot
except ImportError:
    from telegram._bot import Bot

from job_bot.agent import SiteAgent
from job_bot.ai_client import AIClient
from job_bot.config import Settings, load_websites_file
from job_bot.fetching import ResilientFetcher
from job_bot.output_writer import CardWriter
from job_bot.storage import StateStore
from job_bot.subscriber_notifier import SubscriberNotifier
from job_bot.subscription_admin_notifier import SubscriptionAdminNotifier
from job_bot.telegram_subscription_store import SubscriptionStore, is_disabled_website, normalize_website
from job_bot.user_message_logger import UserMessageLogger
from job_bot.websites_refresh import consume_websites_refresh, default_websites_refresh_signal_path


@dataclass(slots=True)
class CycleBudget:
    limit: int
    used: int = 0
    _lock: asyncio.Lock = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._lock = asyncio.Lock()

    async def try_take(self) -> bool:
        async with self._lock:
            if self.used >= self.limit:
                return False
            self.used += 1
            return True

    async def is_exhausted(self) -> bool:
        async with self._lock:
            return self.used >= self.limit

    async def remaining(self) -> int:
        async with self._lock:
            return max(0, self.limit - self.used)

    async def release(self, amount: int = 1) -> None:
        if amount <= 0:
            return
        async with self._lock:
            self.used = max(0, self.used - amount)


class JobBotCoordinator:
    _NEGLECT_REASON_LABELS = {
        "old_post": "old post",
        "not_job": "not job",
        "filter_mismatch": "filter mismatch",
        "duplicate_cluster": "duplicate cluster",
    }
    _NEGLECT_REASON_ORDER = ("old_post", "not_job", "filter_mismatch", "duplicate_cluster")

    def __init__(self, settings: Settings, logger: logging.Logger) -> None:
        self.settings = settings
        self.logger = logger
        self.store = StateStore(settings.state_db_path)
        self.subscription_store: SubscriptionStore | None = None
        self.writer = CardWriter(settings.output_file_path)
        self.ai_client = AIClient(settings=settings, logger=logger, state_store=self.store)
        self.fetcher = ResilientFetcher(settings=settings, logger=logger, state_store=self.store)
        self.notifier = None
        self.review_notifier = None
        self.subscriber_notifier = None
        self.admin_notifier: SubscriptionAdminNotifier | None = None
        self._website_resolution_reason = "startup"
        self._delivery_stop_event = asyncio.Event()
        self._delivery_worker_tasks: list[asyncio.Task] = []
        self._notification_flush_task: asyncio.Task | None = None
        if settings.telegram_bot_token:
            self.subscription_store = SubscriptionStore(settings.telegram_subs_db_path)
            user_message_logger = UserMessageLogger(settings.telegram_user_log_dir)
            self.subscriber_notifier = SubscriberNotifier(
                bot_token=settings.telegram_bot_token,
                store=self.subscription_store,
                logger=logger,
                user_message_logger=user_message_logger,
                openai_api_key=settings.openai_api_key,
                openai_model=settings.openai_model_filter_match or settings.openai_model,
                keyword_model=settings.openai_model_keyword_expansion,
                final_match_model=settings.openai_model_filter_match,
                request_timeout_seconds=settings.openai_request_timeout_seconds,
                retry_budget=settings.ai_retry_budget,
                backoff_base_seconds=settings.ai_backoff_base_seconds,
                delivery_job_retry_budget=settings.delivery_job_retry_budget,
                delivery_job_backoff_seconds=settings.delivery_job_backoff_seconds,
            )
            if settings.subscription_admin_chat_ids:
                self.admin_notifier = SubscriptionAdminNotifier(
                    bot=Bot(token=settings.telegram_bot_token),
                    logger=logger,
                    admin_chat_ids=settings.subscription_admin_chat_ids,
                )
            self.logger.info("[telegram] subscriber delivery enabled")
        else:
            self.logger.info("[telegram] subscriber delivery disabled (missing TELEGRAM_BOT_TOKEN)")
        if settings.telegram_chat_id:
            self.logger.info("[telegram] legacy TELEGRAM_CHAT_ID delivery is disabled; subscriber delivery only")
        else:
            self.logger.info("[telegram] legacy direct-chat delivery disabled")
        self.logger.info("[telegram] human-review queue delivery is handled by the Telegram bot review loop")
        self.active_websites: list[str] = []
        self.agents: list[SiteAgent] = []
        self.websites_refresh_signal_path = default_websites_refresh_signal_path()
        self._refresh_agents(force=True)

    @classmethod
    def _format_neglect_breakdown(cls, reasons: dict[str, int] | None) -> str:
        if not reasons:
            return "none"
        normalized_reasons = {
            str(key).strip().lower(): int(value)
            for key, value in reasons.items()
            if str(key).strip() and int(value) > 0
        }
        if not normalized_reasons:
            return "none"
        ordered_keys = [key for key in cls._NEGLECT_REASON_ORDER if normalized_reasons.get(key, 0) > 0]
        ordered_keys.extend(
            sorted(key for key in normalized_reasons if key not in cls._NEGLECT_REASON_LABELS)
        )
        return ", ".join(
            f"{cls._NEGLECT_REASON_LABELS.get(key, key.replace('_', ' '))}:{normalized_reasons[key]}"
            for key in ordered_keys
        )

    @classmethod
    def _aggregate_cycle_totals(cls, stats_results) -> dict[str, object]:
        totals = {
            "new_cards": 0,
            "discovered_posts": 0,
            "neglected_posts": 0,
            "neglect_reasons": {},
        }
        for result in stats_results:
            if isinstance(result, Exception) or not hasattr(result, "new_cards"):
                continue
            totals["new_cards"] += max(0, int(getattr(result, "new_cards", 0)))
            totals["discovered_posts"] += max(0, int(getattr(result, "discovered_links", 0)))
            totals["neglected_posts"] += max(0, int(getattr(result, "neglected_posts", 0)))
            for reason_key, count in dict(getattr(result, "neglect_reasons", {}) or {}).items():
                normalized_reason = str(reason_key or "").strip().lower()
                if not normalized_reason:
                    continue
                normalized_count = max(0, int(count))
                if normalized_count <= 0:
                    continue
                reasons = totals["neglect_reasons"]
                assert isinstance(reasons, dict)
                reasons[normalized_reason] = int(reasons.get(normalized_reason, 0)) + normalized_count
        return totals

    def _load_candidate_websites(self) -> list[str]:
        runtime_websites = load_websites_file(self.settings.websites_file_path)
        if not runtime_websites:
            runtime_websites = self.settings.websites
        return [website for website in runtime_websites if website.strip() and not is_disabled_website(website)]

    def _resolve_runtime_websites(self) -> list[str]:
        if bool(getattr(self.settings, "scraper_paused", False)):
            self._website_resolution_reason = "Scraper is intentionally paused by configuration."
            return []
        candidate_websites = self._load_candidate_websites()
        if self.subscription_store is None:
            self._website_resolution_reason = (
                "No subscriber store is available; waiting for at least one active customer website selection."
            )
            return []

        active_user_websites = self.subscription_store.get_all_user_websites(active_only=True)
        if not active_user_websites:
            self._website_resolution_reason = "No active subscribers selected any websites."
            return []

        website_counts = self.subscription_store.get_active_website_selection_counts()
        curated_order: dict[str, int] = {}
        for index, website in enumerate(candidate_websites):
            raw_website = str(website).strip()
            if not raw_website:
                continue
            normalized = normalize_website(raw_website)
            if normalized and normalized not in curated_order:
                curated_order[normalized] = index

        runtime_websites: list[str] = []
        seen_runtime: set[str] = set()
        for website in active_user_websites:
            raw_website = str(website).strip()
            if not raw_website or is_disabled_website(raw_website):
                continue
            normalized = normalize_website(raw_website)
            if not normalized or normalized in seen_runtime:
                continue
            runtime_websites.append(normalized)
            seen_runtime.add(normalized)

        original_order = {website: index for index, website in enumerate(runtime_websites)}
        runtime_websites.sort(
            key=lambda website: (
                -website_counts.get(normalize_website(website), 0),
                curated_order.get(normalize_website(website), len(curated_order) + original_order.get(website, 0)),
                original_order.get(website, 0),
                website,
            )
        )
        blocked_runtime_sites: dict[str, object] = {}
        blocked_sites_getter = getattr(self.store, "list_temporarily_blocked_sites", None)
        if callable(blocked_sites_getter) and runtime_websites:
            blocked_runtime_sites = blocked_sites_getter(
                runtime_websites,
                cooldown_seconds=max(1800, int(getattr(self.settings, "browser_reprobe_cooldown_seconds", 600)) * 3),
                challenge_threshold=2,
            )
        if blocked_runtime_sites:
            visible_blocked = [
                f"{website} (until {getattr(cooldown, 'blocked_until_utc', '') or 'cooldown'})"
                for website, cooldown in blocked_runtime_sites.items()
            ]
            self.logger.warning(
                "[coordinator] temporarily pausing %s blocked website(s): %s",
                len(blocked_runtime_sites),
                "; ".join(visible_blocked[:5]),
            )
            self.store.set_runtime_metric("crawler.temporarily_blocked_sites", float(len(blocked_runtime_sites)))
            runtime_websites = [website for website in runtime_websites if website not in blocked_runtime_sites]
        else:
            self.store.set_runtime_metric("crawler.temporarily_blocked_sites", 0.0)

        curated_count = sum(1 for website in runtime_websites if website in curated_order)
        custom_count = max(0, len(runtime_websites) - curated_count)
        if custom_count:
            self.logger.info(
                "[coordinator] runtime websites resolved from active subscriber sources | active=%s curated=%s custom=%s",
                len(runtime_websites),
                curated_count,
                custom_count,
            )
        if runtime_websites:
            blocked_note = ""
            if blocked_runtime_sites:
                blocked_note = f" ({len(blocked_runtime_sites)} temporarily paused after repeated challenge blocks)."
            self._website_resolution_reason = (
                f"Active subscriber selections resolved to {len(runtime_websites)} runtime websites.{blocked_note}"
            )
            return runtime_websites
        if blocked_runtime_sites:
            self._website_resolution_reason = (
                f"All selected websites are temporarily paused after repeated challenge blocks ({len(blocked_runtime_sites)} site(s))."
            )
            return []
        self._website_resolution_reason = "Active subscriber website selections did not resolve to any valid runtime websites."
        return runtime_websites

    def _set_crawler_health(self, *, severity: str, state: str, reason: str, details: dict[str, object] | None = None) -> None:
        details = dict(details or {})
        details.setdefault("active_sites", len(self.active_websites))
        details.setdefault("concurrent_agents", min(len(self.active_websites), max(1, int(self.settings.agent_count))))
        details.setdefault("resolution_reason", self._website_resolution_reason)
        self.store.set_crawler_health(
            severity=severity,
            state=state,
            reason=reason,
            details=details,
        )

    async def _send_admin_alert(self, *, alert_key: str, alert_kind: str, message: str) -> None:
        if self.admin_notifier is None:
            return
        if not self.store.should_send_admin_alert(alert_key):
            return
        try:
            await self.admin_notifier.notify_admin_message(event_key=alert_key, message=message)
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("[coordinator] failed to send admin alert %s: %s", alert_key, str(exc))
            return
        self.store.mark_admin_alert_sent(
            alert_key,
            alert_kind=alert_kind,
            alert_message=message,
        )

    async def _handle_zero_active_sites(self) -> None:
        reason = self._website_resolution_reason or "No active websites resolved for scraping."
        self.logger.error("[coordinator] scraping inactive because no websites are active: %s", reason)
        self.store.increment_runtime_metric("zero_site_cycles", 1.0)
        self.store.increment_runtime_metric("zero_site_cycles_consecutive", 1.0)
        consecutive_zero_cycles = int(self.store.get_runtime_metric("zero_site_cycles_consecutive", 0.0))
        self._set_crawler_health(
            severity="critical",
            state="crawler_inactive",
            reason=reason,
            details={
                "reason_code": "zero_active_sites",
                "consecutive_zero_site_cycles": consecutive_zero_cycles,
            },
        )
        await self._send_admin_alert(
            alert_key="crawler:zero_active_sites",
            alert_kind="zero_active_sites",
            message=(
                "Crawler inactive\n"
                f"Reason: {reason}\n"
                f"Consecutive zero-site cycles: {consecutive_zero_cycles}\n"
                f"Updated (UTC): {datetime.now(timezone.utc).isoformat()}"
            ),
        )
        if consecutive_zero_cycles > 1:
            await self._send_admin_alert(
                alert_key="crawler:zero_active_sites_multi_cycle",
                alert_kind="zero_active_sites_multi_cycle",
                message=(
                    "Crawler still inactive after more than one cycle\n"
                    f"Reason: {reason}\n"
                    f"Consecutive zero-site cycles: {consecutive_zero_cycles}\n"
                    f"Updated (UTC): {datetime.now(timezone.utc).isoformat()}"
                ),
            )

    @staticmethod
    def _window_anchor_utc(minutes: int) -> str:
        return (
            (datetime.now(timezone.utc) - timedelta(minutes=max(1, int(minutes))))
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )

    async def _update_inactivity_health(self) -> None:
        if not self.agents:
            return
        candidate_window_since = self._window_anchor_utc(
            int(getattr(self.settings, "no_candidates_alert_window_minutes", 120))
        )
        delivery_window_since = self._window_anchor_utc(
            int(getattr(self.settings, "no_delivery_alert_window_minutes", 180))
        )
        recent_cycles = self.store.summarize_recent_cycle_window(since_utc=candidate_window_since)
        recent_discoveries = int(recent_cycles.get("discovered_links", 0))
        recent_cycle_count = int(recent_cycles.get("cycles", 0))
        queue_backlog = 0
        sent_recent = 0
        if self.subscription_store is not None:
            queue_backlog = self.subscription_store.get_delivery_backlog()
            sent_recent = self.subscription_store.count_sent_notifications(since_utc=delivery_window_since)
        if recent_cycle_count > 0 and recent_discoveries <= 0:
            reason = (
                "No new candidates were discovered in the recent crawl window. "
                f"Window start (UTC): {candidate_window_since}"
            )
            self.logger.warning("[coordinator] crawler inactive: %s", reason)
            self._set_crawler_health(
                severity="critical",
                state="crawler_inactive",
                reason=reason,
                details={
                    "reason_code": "no_recent_candidates",
                    "window_start_utc": candidate_window_since,
                    "recent_cycles": recent_cycle_count,
                    "queue_backlog": queue_backlog,
                },
            )
            await self._send_admin_alert(
                alert_key="crawler:no_recent_candidates",
                alert_kind="crawler_inactive",
                message=(
                    "Crawler inactive\n"
                    f"Reason: {reason}\n"
                    f"Queue backlog: {queue_backlog}\n"
                    f"Updated (UTC): {datetime.now(timezone.utc).isoformat()}"
                ),
            )
            return
        if self.subscription_store is not None and sent_recent <= 0:
            reason = (
                "No jobs were delivered in the recent delivery window. "
                f"Window start (UTC): {delivery_window_since}"
            )
            self.logger.warning("[coordinator] delivery inactivity: %s", reason)
            self._set_crawler_health(
                severity="warning",
                state="delivery_inactive",
                reason=reason,
                details={
                    "reason_code": "no_recent_deliveries",
                    "window_start_utc": delivery_window_since,
                    "queue_backlog": queue_backlog,
                    "recent_discoveries": recent_discoveries,
                },
            )
            await self._send_admin_alert(
                alert_key="crawler:no_recent_deliveries",
                alert_kind="delivery_inactive",
                message=(
                    "Delivery inactive\n"
                    f"Reason: {reason}\n"
                    f"Queue backlog: {queue_backlog}\n"
                    f"Updated (UTC): {datetime.now(timezone.utc).isoformat()}"
                ),
            )
            return
        if queue_backlog:
            self.store.set_runtime_metric("delivery.queue_backlog", float(queue_backlog))
        self._set_crawler_health(
            severity="info",
            state="healthy",
            reason=self._website_resolution_reason,
            details={
                "reason_code": "healthy",
                "queue_backlog": queue_backlog,
                "recent_discoveries": recent_discoveries,
                "recent_sent_notifications": sent_recent,
            },
        )

    def _refresh_agents(self, force: bool = False) -> None:
        runtime_websites = self._resolve_runtime_websites()
        if not runtime_websites:
            if force or self.active_websites or self.agents:
                self.active_websites = []
                self.agents = []
                self.logger.info("[coordinator] no active websites to monitor right now | reason=%s", self._website_resolution_reason)
            return

        if not force and runtime_websites == self.active_websites:
            return

        self.active_websites = runtime_websites
        self.store.set_runtime_metric("zero_site_cycles_consecutive", 0.0)
        self.agents = [
            SiteAgent(
                name=f"agent-{index + 1}",
                website=website,
                settings=self.settings,
                logger=self.logger,
                fetcher=self.fetcher,
                ai_client=self.ai_client,
                store=self.store,
                writer=self.writer,
                notifier=self.notifier,
                review_notifier=self.review_notifier,
                subscriber_notifier=self.subscriber_notifier,
                admin_notifier=self.admin_notifier,
            )
            for index, website in enumerate(self.active_websites)
        ]
        self.logger.info(
            "[coordinator] refreshed websites list. active_sites=%s concurrent_agents=%s reason=%s",
            len(self.active_websites),
            min(len(self.active_websites), max(1, int(self.settings.agent_count))),
            self._website_resolution_reason,
        )
        self._set_crawler_health(
            severity="info",
            state="healthy",
            reason=self._website_resolution_reason,
            details={"website_count": len(self.active_websites)},
        )

    async def _run_agent_cycle_with_watchdog(self, agent: SiteAgent, *, cycle_utc: str, budget: CycleBudget):
        idle_timeout_seconds = max(
            1.0,
            float(
                getattr(
                    self.settings,
                    "site_cycle_idle_timeout_seconds",
                    getattr(self.settings, "site_cycle_timeout_seconds", 900),
                )
            ),
        )
        hard_timeout_seconds = max(
            idle_timeout_seconds + 1.0,
            float(
                getattr(
                    self.settings,
                    "site_cycle_hard_timeout_seconds",
                    max(idle_timeout_seconds * 6.0, 14400.0),
                )
            ),
        )
        poll_interval_seconds = min(10.0, max(0.5, idle_timeout_seconds / 8.0))
        heartbeat_interval_seconds = min(60.0, max(15.0, idle_timeout_seconds / 4.0))
        started_at = time.monotonic()
        next_heartbeat_at = started_at + heartbeat_interval_seconds
        setattr(agent, "_cycle_progress_at", started_at)
        setattr(agent, "_cycle_progress_note", f"cycle scheduled for {getattr(agent, 'website', 'unknown')}")
        setattr(agent, "_cycle_stage", "scheduled")
        setattr(agent, "_cycle_stage_detail", f"cycle scheduled for {getattr(agent, 'website', 'unknown')}")
        setattr(agent, "_cycle_stage_started_at", started_at)
        setattr(agent, "_cycle_stage_progress_at", started_at)
        setattr(agent, "_cycle_stage_budget_seconds", idle_timeout_seconds)
        task = asyncio.create_task(agent.run_cycle(cycle_utc=cycle_utc, budget=budget))
        try:
            while True:
                try:
                    return await asyncio.wait_for(asyncio.shield(task), timeout=poll_interval_seconds)
                except asyncio.TimeoutError:
                    now = time.monotonic()
                    stage_name = str(getattr(agent, "_cycle_stage", "unknown") or "unknown")
                    stage_detail = str(getattr(agent, "_cycle_stage_detail", "") or "")
                    stage_started_at = float(getattr(agent, "_cycle_stage_started_at", started_at) or started_at)
                    cycle_progress_at = float(getattr(agent, "_cycle_progress_at", stage_started_at) or stage_started_at)
                    stage_progress_at = max(
                        float(getattr(agent, "_cycle_stage_progress_at", stage_started_at) or stage_started_at),
                        cycle_progress_at,
                    )
                    stage_budget_seconds = max(
                        1.0,
                        float(getattr(agent, "_cycle_stage_budget_seconds", idle_timeout_seconds) or idle_timeout_seconds),
                    )
                    idle_for = max(0.0, now - stage_progress_at)
                    stage_runtime_for = max(0.0, now - stage_started_at)
                    runtime_for = max(0.0, now - started_at)
                    last_progress_note = str(getattr(agent, "_cycle_progress_note", "") or stage_detail or "unknown")
                    if now >= next_heartbeat_at:
                        level = logging.WARNING if idle_for >= min(stage_budget_seconds, heartbeat_interval_seconds * 2) else logging.INFO
                        self.logger.log(
                            level,
                            "[watchdog] %s still running | runtime=%.1fs stage=%s stage_runtime=%.1fs stage_idle=%.1fs stage_budget=%.1fs | detail=%s",
                            getattr(agent, "website", "unknown"),
                            runtime_for,
                            stage_name,
                            stage_runtime_for,
                            idle_for,
                            stage_budget_seconds,
                            last_progress_note,
                        )
                        next_heartbeat_at = now + heartbeat_interval_seconds
                    if runtime_for >= hard_timeout_seconds:
                        self.store.increment_runtime_metric("watchdog.hard_timeouts", 1.0)
                        task.cancel()
                        with contextlib.suppress(asyncio.CancelledError, Exception):
                            await task
                        raise TimeoutError(
                            "site cycle hard timeout after "
                            f"{runtime_for:.1f}s on {getattr(agent, 'website', 'unknown')} "
                            f"(stage: {stage_name}; last progress: {last_progress_note})"
                        )
                    if idle_for < min(stage_budget_seconds, idle_timeout_seconds):
                        continue
                    if stage_runtime_for < stage_budget_seconds:
                        continue
                    self.store.increment_runtime_metric("watchdog.stage_timeouts", 1.0)
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await task
                    raise TimeoutError(
                        "site cycle stage timeout after "
                        f"{idle_for:.1f}s without progress on {getattr(agent, 'website', 'unknown')} "
                        f"(stage: {stage_name}; last progress: {last_progress_note})"
                    )
        finally:
            if task.done():
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    task.exception()

    async def _run_agents_for_cycle(self, *, cycle_utc: str, budget: CycleBudget):
        if not self.agents:
            return []

        concurrency_limit = max(1, min(int(self.settings.agent_count), len(self.agents)))
        semaphore = asyncio.Semaphore(concurrency_limit)

        async def _run_limited(agent: SiteAgent):
            async with semaphore:
                return await self._run_agent_cycle_with_watchdog(agent, cycle_utc=cycle_utc, budget=budget)

        return await asyncio.gather(*(_run_limited(agent) for agent in self.agents), return_exceptions=True)

    async def _delivery_worker_loop(self, worker_index: int) -> None:
        if self.subscriber_notifier is None:
            return
        poll_seconds = max(1.0, float(getattr(self.settings, "delivery_worker_poll_seconds", 5.0)))
        while not self._delivery_stop_event.is_set():
            try:
                processed = await self.subscriber_notifier.process_one_delivery_job()
            except Exception as exc:  # noqa: BLE001
                self.logger.warning("[delivery-worker-%s] job processing failed: %s", worker_index, str(exc))
                processed = False
            if processed:
                continue
            await asyncio.sleep(poll_seconds)

    async def _notification_flush_loop(self) -> None:
        if self.subscriber_notifier is None:
            return
        poll_seconds = max(1.0, float(getattr(self.settings, "delivery_worker_poll_seconds", 5.0)))
        while not self._delivery_stop_event.is_set():
            try:
                await self.subscriber_notifier.flush_due_notifications(limit=200)
            except Exception as exc:  # noqa: BLE001
                self.logger.warning("[delivery-flush] queued notification flush failed: %s", str(exc))
            await asyncio.sleep(poll_seconds)

    def _start_delivery_workers(self) -> None:
        if self.subscriber_notifier is None or self._delivery_worker_tasks:
            return
        self._delivery_stop_event = asyncio.Event()
        worker_count = max(1, int(getattr(self.settings, "delivery_worker_count", 2)))
        self._delivery_worker_tasks = [
            asyncio.create_task(self._delivery_worker_loop(worker_index + 1))
            for worker_index in range(worker_count)
        ]
        self._notification_flush_task = asyncio.create_task(self._notification_flush_loop())
        self.logger.info("[coordinator] started %s delivery workers", worker_count)

    async def _stop_delivery_workers(self) -> None:
        self._delivery_stop_event.set()
        tasks = list(self._delivery_worker_tasks)
        if self._notification_flush_task is not None:
            tasks.append(self._notification_flush_task)
        self._delivery_worker_tasks = []
        self._notification_flush_task = None
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def run_forever(self) -> None:
        self._start_delivery_workers()
        self.logger.info(
            "Starting job bot loop. Active sites: %s | concurrent agents: %s | cycle interval: %ss | site idle timeout: %ss | site hard timeout: %ss",
            len(self.agents),
            min(len(self.agents), max(1, int(self.settings.agent_count))),
            self.settings.cycle_seconds,
            getattr(self.settings, "site_cycle_idle_timeout_seconds", self.settings.site_cycle_timeout_seconds),
            getattr(self.settings, "site_cycle_hard_timeout_seconds", max(self.settings.site_cycle_timeout_seconds * 6, 14400)),
        )
        try:
            while True:
                self._refresh_agents()
                cycle_started = datetime.now(timezone.utc)
                cycle_utc = cycle_started.isoformat()
                budget = CycleBudget(limit=self.settings.max_cards_per_cycle)
                if not self.agents:
                    await self._handle_zero_active_sites()
                    sleep_for = max(5.0, float(self.settings.cycle_seconds))
                    self.logger.info(
                        "=== cycle inactive %s | active_sites=0 | next_in=%.1fs | reason=%s ===",
                        cycle_utc,
                        sleep_for,
                        self._website_resolution_reason,
                    )
                    if await self._sleep_until_next_cycle(sleep_for):
                        self.logger.info("[coordinator] website refresh requested while inactive; re-checking now")
                    continue
                self.logger.info(
                    "=== cycle start %s | max_cards=%s | active_sites=%s | concurrent_agents=%s | idle_timeout=%ss | hard_timeout=%ss ===",
                    cycle_utc,
                    self.settings.max_cards_per_cycle,
                    len(self.agents),
                    min(len(self.agents), max(1, int(self.settings.agent_count))),
                    getattr(self.settings, "site_cycle_idle_timeout_seconds", self.settings.site_cycle_timeout_seconds),
                    getattr(self.settings, "site_cycle_hard_timeout_seconds", max(self.settings.site_cycle_timeout_seconds * 6, 14400)),
                )

                stats = await self._run_agents_for_cycle(cycle_utc=cycle_utc, budget=budget)
                cycle_totals = self._aggregate_cycle_totals(stats)

                for agent_index, result in enumerate(stats):
                    website = self.active_websites[agent_index]
                    if isinstance(result, Exception):
                        self.logger.error("[agent-%s] unhandled error: %s", agent_index + 1, str(result))
                        self.store.save_cycle_stats(
                            cycle_utc=cycle_utc,
                            website=website,
                            fetched_pages=0,
                            discovered_links=0,
                            checked_links=0,
                            seen_links_in_row=0,
                            new_cards=0,
                            errors=1,
                            last_error=str(result),
                        )
                        continue

                    self.store.save_cycle_stats(
                        cycle_utc=cycle_utc,
                        website=result.website,
                        fetched_pages=result.fetched_pages,
                        discovered_links=result.discovered_links,
                        checked_links=result.checked_links,
                        seen_links_in_row=result.seen_links_in_row,
                        new_cards=result.new_cards,
                        errors=result.errors,
                        last_error=result.last_error,
                    )
                    self.logger.info(
                        "[summary] %s | fetched=%s discovered=%s checked=%s new_cards=%s neglected=%s neglect_reasons=%s errors=%s",
                        result.website,
                        result.fetched_pages,
                        result.discovered_links,
                        result.checked_links,
                        result.new_cards,
                        result.neglected_posts,
                        self._format_neglect_breakdown(result.neglect_reasons),
                        result.errors,
                    )

                elapsed = (datetime.now(timezone.utc) - cycle_started).total_seconds()
                sleep_for = max(0.0, float(self.settings.cycle_seconds) - elapsed)
                self.logger.info(
                    "=== cycle end %s | cards=%s | discovered_posts=%s | neglected_posts=%s | neglected_reasons=%s | elapsed=%.1fs | next_in=%.1fs ===",
                    cycle_utc,
                    cycle_totals["new_cards"],
                    cycle_totals["discovered_posts"],
                    cycle_totals["neglected_posts"],
                    self._format_neglect_breakdown(cycle_totals["neglect_reasons"]),
                    elapsed,
                    sleep_for,
                )
                await self._update_inactivity_health()
                if await self._sleep_until_next_cycle(sleep_for):
                    self.logger.info("[coordinator] website refresh requested; starting next cycle early")
        finally:
            await self._stop_delivery_workers()
            self.store.close()
            if self.subscription_store is not None:
                self.subscription_store.close()

    async def run_once(self) -> None:
        self._start_delivery_workers()
        self._refresh_agents(force=True)
        cycle_utc = datetime.now(timezone.utc).isoformat()
        budget = CycleBudget(limit=self.settings.max_cards_per_cycle)
        if not self.agents:
            await self._handle_zero_active_sites()
            await self._stop_delivery_workers()
            return
        self.logger.info(
            "Running one cycle at %s | active_sites=%s | concurrent_agents=%s",
            cycle_utc,
            len(self.agents),
            min(len(self.agents), max(1, int(self.settings.agent_count))),
        )
        stats = await self._run_agents_for_cycle(cycle_utc=cycle_utc, budget=budget)
        await asyncio.sleep(max(0.1, float(getattr(self.settings, "delivery_worker_poll_seconds", 5.0))))
        await self._update_inactivity_health()
        await self._stop_delivery_workers()
        cycle_totals = self._aggregate_cycle_totals(stats)
        for agent_index, result in enumerate(stats):
            website = self.active_websites[agent_index]
            if isinstance(result, Exception):
                self.logger.error("[once][agent-%s] error on %s: %s", agent_index + 1, website, str(result))
                continue
            self.logger.info(
                "[once] %s | fetched=%s discovered=%s checked=%s new_cards=%s neglected=%s neglect_reasons=%s errors=%s",
                result.website,
                result.fetched_pages,
                result.discovered_links,
                result.checked_links,
                result.new_cards,
                result.neglected_posts,
                self._format_neglect_breakdown(result.neglect_reasons),
                result.errors,
            )
        self.logger.info(
            "[once] cycle totals | cards=%s discovered_posts=%s neglected_posts=%s neglected_reasons=%s",
            cycle_totals["new_cards"],
            cycle_totals["discovered_posts"],
            cycle_totals["neglected_posts"],
            self._format_neglect_breakdown(cycle_totals["neglect_reasons"]),
        )
        self.store.close()
        if self.subscription_store is not None:
            self.subscription_store.close()

    async def _sleep_until_next_cycle(self, sleep_for: float) -> bool:
        remaining = max(0.0, float(sleep_for))
        while remaining > 0:
            if consume_websites_refresh(self.websites_refresh_signal_path):
                return True
            sleep_chunk = min(1.0, remaining)
            await asyncio.sleep(sleep_chunk)
            remaining -= sleep_chunk
        return consume_websites_refresh(self.websites_refresh_signal_path)
