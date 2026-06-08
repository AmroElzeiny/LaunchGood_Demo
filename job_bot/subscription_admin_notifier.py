from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from datetime import datetime, timezone

try:
    from telegram import Bot
except ImportError:
    from telegram._bot import Bot

from job_bot.telegram_subscription_store import ActiveSubscription


DEFAULT_SUBSCRIPTION_ADMIN_CHAT_ID = 100000001


class SubscriptionAdminNotifier:
    def __init__(
        self,
        bot: Bot,
        logger: logging.Logger,
        admin_chat_id: int | None = DEFAULT_SUBSCRIPTION_ADMIN_CHAT_ID,
        admin_chat_ids: Iterable[int] | None = None,
    ) -> None:
        self.bot = bot
        self.logger = logger
        self.admin_chat_ids = self._normalize_admin_chat_ids(admin_chat_ids, admin_chat_id)
        self.admin_chat_id = self.admin_chat_ids[0]
        self._sent_event_keys: dict[str, set[int]] = {}
        self._lock = asyncio.Lock()

    async def notify_subscription_event(self, active_sub: ActiveSubscription) -> None:
        event_key = self._event_key(active_sub)
        message = self.format_message(active_sub)
        await self.notify_admin_message(event_key=event_key, message=message)

    async def notify_first_start(self, *, user_id: int, username: str) -> bool:
        return await self.notify_admin_message(
            event_key=self._first_start_event_key(user_id),
            message=self.format_first_start_message(user_id=user_id, username=username),
        )

    async def notify_admin_message(self, *, event_key: str, message: str) -> bool:
        async with self._lock:
            sent_chat_ids = self._sent_event_keys.setdefault(event_key, set())
            pending_chat_ids = [chat_id for chat_id in self.admin_chat_ids if chat_id not in sent_chat_ids]
            if not pending_chat_ids:
                return True
            successful_chat_ids: list[int] = []
            for chat_id in pending_chat_ids:
                try:
                    await self.bot.send_message(chat_id=chat_id, text=message)
                except Exception as exc:  # noqa: BLE001
                    self.logger.warning(
                        "[subscription-admin] failed to send notification key=%s chat_id=%s: %s",
                        event_key,
                        chat_id,
                        str(exc),
                    )
                    continue
                sent_chat_ids.add(chat_id)
                successful_chat_ids.append(chat_id)
            if not sent_chat_ids:
                self._sent_event_keys.pop(event_key, None)
            notification_complete = len(sent_chat_ids) == len(self.admin_chat_ids)
        for chat_id in successful_chat_ids:
            self.logger.info(
                "[subscription-admin] sent admin notification key=%s chat_id=%s",
                event_key,
                chat_id,
            )
        return notification_complete

    @classmethod
    def format_message(cls, active_sub: ActiveSubscription) -> str:
        return (
            "Subscription event\n"
            f"Type: {cls._event_type(active_sub)}\n"
            f"User ID: {active_sub.user_id}\n"
            f"Username: {cls._format_username(active_sub.username)}\n"
            f"Plan: {cls._plan_display_name(active_sub.plan)}\n"
            f"Source: {active_sub.source}\n"
            f"Started (UTC): {active_sub.started_at_utc}\n"
            f"Ends (UTC): {active_sub.ends_at_utc}\n"
            f"Updated (UTC): {active_sub.updated_at_utc}"
        )

    @classmethod
    def format_first_start_message(cls, *, user_id: int, username: str) -> str:
        started_at_utc = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        return (
            "First bot start\n"
            "Type: User started the bot for the first time\n"
            f"User ID: {user_id}\n"
            f"Username: {cls._format_username(username)}\n"
            f"Started (UTC): {started_at_utc}"
        )

    @staticmethod
    def _event_key(active_sub: ActiveSubscription) -> str:
        return (
            f"{active_sub.user_id}|{active_sub.source}|{active_sub.plan}|"
            f"{active_sub.started_at_utc}|{active_sub.ends_at_utc}"
        )

    @staticmethod
    def _first_start_event_key(user_id: int) -> str:
        return f"first_start|{user_id}"

    @staticmethod
    def _event_type(active_sub: ActiveSubscription) -> str:
        if str(active_sub.source).strip().lower() == "trial":
            return "Free trial claimed"
        return "Paid subscription activated"

    @staticmethod
    def _format_username(username: str) -> str:
        value = str(username or "").strip()
        if not value:
            return "(none)"
        if value.startswith("@"):
            return value
        return f"@{value}"

    @staticmethod
    def _plan_display_name(plan_key: str) -> str:
        normalized = str(plan_key or "").strip().lower()
        if normalized == "trial_48h":
            return "48-hour free trial"
        return str(plan_key or "").replace("_", " ").title() or "Unknown"

    @staticmethod
    def _normalize_admin_chat_ids(
        admin_chat_ids: Iterable[int] | None,
        admin_chat_id: int | None,
    ) -> tuple[int, ...]:
        candidates = list(admin_chat_ids or [])
        if not candidates and admin_chat_id is not None:
            candidates = [admin_chat_id]
        ordered: list[int] = []
        seen: set[int] = set()
        for candidate in candidates:
            chat_id = int(candidate)
            if chat_id in seen:
                continue
            seen.add(chat_id)
            ordered.append(chat_id)
        if not ordered:
            ordered.append(DEFAULT_SUBSCRIPTION_ADMIN_CHAT_ID)
        return tuple(ordered)
