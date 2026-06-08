from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

try:
    from telegram import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Update, User
except ImportError:
    from telegram._callbackquery import CallbackQuery
    from telegram._inline.inlinekeyboardbutton import InlineKeyboardButton
    from telegram._inline.inlinekeyboardmarkup import InlineKeyboardMarkup
    from telegram._update import Update
    from telegram._user import User
from telegram.error import BadRequest, Conflict, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from job_bot.filter_ai import FilterAI, KeywordInterpretation, LocationNormalization
from job_bot.human_review_queue import (
    approve_review_item,
    build_human_review_presentation_async,
    format_human_review_text,
    job_card_from_review_item,
    neglect_review_item,
)
from job_bot.language_utils import contains_cyrillic, detect_language, normalize_match_text
from job_bot.models import JobCard
from job_bot.nowpayments_client import NowPaymentsClient
from job_bot.project_filters import (
    ACTIVE_PROJECT_FILTER_FIELDS,
    FULL_PROJECT_FILTER_FIELDS,
    format_project_filter_value as shared_format_project_filter_value,
    normalize_project_filter_values as shared_normalize_project_filter_values,
    parse_project_filter_bool as shared_parse_project_filter_bool,
    parse_project_filter_number as shared_parse_project_filter_number,
    parse_project_preferences_text as shared_parse_project_preferences_text,
    project_filter_clear_tokens as shared_project_filter_clear_tokens,
    project_filter_value_labels as shared_project_filter_value_labels,
    project_preferences_display_lines as shared_project_preferences_display_lines,
)
from job_bot.subscription_admin_notifier import SubscriptionAdminNotifier
from job_bot.subscriber_notifier import SubscriberNotifier
from job_bot.telegram_config import TelegramBotSettings
from job_bot.telegram_subscription_store import (
    PAID_COMPLETED_STATUSES,
    PAYMENT_FINAL_STATUSES,
    ActiveSubscription,
    SubscriptionStore,
    format_utc_iso,
    infer_website_currency,
    is_disabled_website,
    normalize_project_preferences,
    normalize_website_currency,
    parse_utc_iso,
    strip_www_prefix,
    utc_now_dt,
    utc_now_iso,
)
from job_bot.telegram_localization import (
    BRAND_NAME,
    CANONICAL_ROLE_TITLE,
    button as localized_button,
    normalize_ui_language,
    payment_status_label,
    project_filter_example,
    project_filter_label,
    text as localized_text,
)
from job_bot.storage import AlertHealthSnapshot, StateStore
from job_bot.user_message_logger import UserMessageLogger
from job_bot.website_guard import WebsiteGuard
from job_bot.websites_refresh import request_websites_refresh

CALLBACK_LANGUAGE_EN = "ui_lang_en"
CALLBACK_LANGUAGE_RU = "ui_lang_ru"
CALLBACK_CREATE_ALERT = "alert_create"
CALLBACK_TRIAL_SCREEN = "trial_screen"
CALLBACK_TRIAL_START = "trial_start"
CALLBACK_HOW_IT_WORKS = "how_it_works"
CALLBACK_MY_ALERT = "my_alert"
CALLBACK_SUBSCRIPTION_MENU = "subscription_menu"
CALLBACK_SUPPORT_MENU = "support_menu"
CALLBACK_ABOUT_MENU = "about_menu"
CALLBACK_BACK_TO_MAIN = "back_main"
CALLBACK_BACK_TO_MY_ALERT = "back_my_alert"
CALLBACK_BACK_TO_EDIT_ALERT = "back_edit_alert"
CALLBACK_BACK_TO_SUBSCRIPTION = "back_subscription"
CALLBACK_BACK_TO_ROLE_STEP = "back_role_step"
CALLBACK_BACK_TO_LOCATION_STEP = "back_location_step"
CALLBACK_BACK_TO_PROJECT_FILTERS_STEP = "back_project_filters_step"
CALLBACK_BACK_TO_SALARY_STEP = "back_salary_step"
CALLBACK_BACK_TO_WEBSITES_STEP = "back_websites_step"
CALLBACK_VIEW_MY_ALERT = "view_my_alert"
CALLBACK_VIEW_SUBSCRIPTION = "view_subscription"
CALLBACK_VIEW_PLANS = "view_plans"
CALLBACK_WRITE_SUPPORT = "support_write"
CALLBACK_ALERT_HEALTH = "alert_health"
CALLBACK_ALERT_HEALTH_CONTINUE = "alert_health_continue"
CALLBACK_MANUAL_REVIEW_FORCE_SEND_PREFIX = "manual_review_force_send_"
CALLBACK_MANUAL_REVIEW_KEEP_REJECTING_PREFIX = "manual_review_keep_rejecting_"
CALLBACK_MANUAL_REVIEW_RECHECK_PREFIX = "manual_review_recheck_"
CALLBACK_HUMAN_REVIEW_SEND_PREFIX = "human_review_send_"
CALLBACK_HUMAN_REVIEW_SAVE_PREFIX = "human_review_save_"
CALLBACK_HUMAN_REVIEW_NEGLECT_PREFIX = "human_review_neglect_"
HUMAN_REVIEW_SUPPRESSED_NO_MATCH = "suppressed:no_matching_subscriber"

CALLBACK_EDIT_ALERT = "alert_edit"
CALLBACK_EDIT_ROLE = "edit_role"
CALLBACK_EDIT_LOCATION = "edit_location"
CALLBACK_EDIT_PROJECT_FILTERS = "edit_project_filters"
CALLBACK_EDIT_SALARY = "edit_salary"
CALLBACK_EDIT_SOURCES = "edit_sources"
CALLBACK_EDIT_KEYWORDS = "edit_keywords"
CALLBACK_EDIT_DELIVERY = "edit_delivery_legacy"
CALLBACK_EDIT_LANGUAGE = "edit_ui_language"
CALLBACK_ALERT_EDIT_SETTINGS = "alert_edit_settings"
CALLBACK_ACTIVATE_ALERT = "alert_activate"
CALLBACK_TOGGLE_ALERT = "alert_toggle"
CALLBACK_DELETE_ALERT = "alert_delete"
CALLBACK_UNDO_DELETE_ALERT = "alert_undo_delete"

CALLBACK_ROLE_PRESET_PREFIX = "role_preset_"
CALLBACK_ROLE_CUSTOM = "role_custom"

CALLBACK_LOCATION_REMOTE_GLOBAL = "location_remote"
LEGACY_LOCATION_REMOTE_CALLBACK = f"{CALLBACK_LOCATION_REMOTE_GLOBAL}_legacy"
CALLBACK_LOCATION_REMOTE_COUNTRY = "location_worldwide"
CALLBACK_LOCATION_ONSITE_COUNTRY = "location_onsite_country"
CALLBACK_LOCATION_CUSTOM = "location_custom"
CALLBACK_LOCATION_SAVE = "location_save"
CALLBACK_LOCATION_CLEAR = "location_clear"
CALLBACK_LOCATION_CONFIRM = "location_confirm"
CALLBACK_LOCATION_REENTER = "location_reenter"

CALLBACK_SALARY_REQUIRED = "salary_required"
CALLBACK_SALARY_OPTIONAL = "salary_optional"

CALLBACK_PROJECT_FILTERS_ADD = "project_filters_add_legacy"
CALLBACK_PROJECT_FILTERS_FIELD_PREFIX = "project_filter_field_"
CALLBACK_PROJECT_FILTERS_SKIP = "project_filters_skip"

CALLBACK_WEBSITE_PICK_PREFIX = "site_pick_"
CALLBACK_WEBSITE_SELECT_ALL = "site_select_all"
CALLBACK_WEBSITE_CUSTOM = "site_custom"
CALLBACK_WEBSITE_CURRENCY_PREFIX = "site_currency_"
CALLBACK_WEBSITE_DONE = "site_done"
CALLBACK_WEBSITE_BACK = "site_back"

CALLBACK_KEYWORDS_ADD = "keywords_add"
CALLBACK_KEYWORDS_SKIP = "keywords_skip"
CALLBACK_KEYWORDS_CONFIRM = "keywords_confirm"
CALLBACK_KEYWORDS_REENTER = "keywords_reenter"

CALLBACK_DELIVERY_INSTANT = "delivery_instant"
CALLBACK_DELIVERY_DIGEST = "delivery_digest"
CALLBACK_DELIVERY_SET_TIMEZONE = "delivery_set_timezone"
CALLBACK_DELIVERY_SET_QUIET = "delivery_set_quiet"
CALLBACK_DELIVERY_CLEAR_QUIET = "delivery_clear_quiet"
CALLBACK_BACK_TO_DELIVERY = "back_delivery"

CALLBACK_WEEKLY_PLAN = "plan_weekly"
CALLBACK_MONTHLY_PLAN = "plan_monthly"
CALLBACK_QUARTERLY_PLAN = "plan_quarterly"
CALLBACK_YEARLY_PLAN = "plan_yearly"
CALLBACK_PAYMENT_UPDATE_PREFIX = "pay_upd_"
CALLBACK_PAYMENT_CANCEL_PREFIX = "pay_can_"
CALLBACK_PAYMENT_CONTACT_PREFIX = "pay_sup_"

WAITING_SUPPORT_MESSAGE = "waiting_support_message"
WAITING_ROLE_INPUT = "waiting_role_input"
WAITING_LOCATION_INPUT = "waiting_location_input"
WAITING_WEBSITE_INPUT = "waiting_website_input"
WAITING_PROJECT_FILTERS_INPUT = "waiting_project_filters_input"
WAITING_KEYWORDS_INPUT = "waiting_keywords_input"
WAITING_TIMEZONE_INPUT = "waiting_timezone_input"
WAITING_QUIET_HOURS_INPUT = "waiting_quiet_hours_input"
WAITING_MATCH_FEEDBACK_INPUT = "waiting_match_feedback_input"

FLOW_CREATE = "create"
FLOW_EDIT = "edit"

STEP_ROLE = "role"
STEP_LOCATION = "location"
STEP_PROJECT_FILTERS = "project_filters"
STEP_WEBSITES = "websites"
STEP_KEYWORDS = "keywords"
STEP_SUMMARY = "summary"

MAX_KEYWORDS = 100
QUEUE_INVALIDATING_FILTERS = {
    "ui_language",
    "role",
    "project_preferences",
    "keywords",
    "websites",
    "website_currency",
    "chosen_websites",
    "spheres",
}
DEFAULT_ROLE_PRESETS = (
    CANONICAL_ROLE_TITLE,
)
REMOTE_GLOBAL_LABEL = "Remote Global"
REMOTE_WITHIN_COUNTRY_LABEL = "Remote within"
REMOTE_WITHIN_COUNTRY_BUTTON_LABEL = "Remote within a country"
ONSITE_HYBRID_WITHIN_COUNTRY_LABEL = "On-site/hybrid within"
ONSITE_HYBRID_WITHIN_COUNTRY_BUTTON_LABEL = "On-site/hybrid within a country"
ALL_LOCATIONS_LABEL = "All locations"
AI_PROCESSING_NOTICE = localized_text("en", "ai_processing_notice")
CUSTOM_WEBSITE_GUIDE_IMAGE = "Gemini_Generated_Image.png"
LOCATION_MODE_CUSTOM = "custom"
LOCATION_MODE_REMOTE_COUNTRY = "remote_country"
LOCATION_MODE_ONSITE_HYBRID_COUNTRY = "onsite_hybrid_country"
LOCATION_OPTION_LABELS: dict[str, str] = {
    CALLBACK_LOCATION_REMOTE_GLOBAL: REMOTE_GLOBAL_LABEL,
}
SITE_DOMAIN_LABELS: dict[str, str] = {
    "contra.com": "Contra [en]",
    "dribbble.com": "Dribbble Jobs [en]",
    "weworkremotely.com": "We Work Remotely [en]",
    "wellfound.com": "Wellfound [en]",
    "builtin.com": "Built In [en]",
    "flexjobs.com": "FlexJobs [en]",
    "workspace.ru": "🇷🇺 Workspace [ru]",
    "fl.ru": "🇷🇺 FL.ru [ru]",
}


@dataclass(frozen=True, slots=True)
class PlanDefinition:
    callback_data: str
    plan_key: str
    title: str
    amount_usd: float
    duration_days: int
    description: str = ""


@dataclass(frozen=True, slots=True)
class DeletedAlertSnapshot:
    role_title: str
    project_preferences: dict[str, object]
    websites: tuple[tuple[str, str], ...]
    keywords: tuple[str, ...]
    spheres: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AlertHealthReport:
    anchor_utc: str
    text: str


PLAN_DEFINITIONS: dict[str, PlanDefinition] = {
    CALLBACK_WEEKLY_PLAN: PlanDefinition(
        CALLBACK_WEEKLY_PLAN,
        "14-day",
        "14-Day",
        4.99,
        14,
        "Fast coverage for the next two weeks. $4.99 for 14 days with fast project matches and personalized filters.",
    ),
    CALLBACK_MONTHLY_PLAN: PlanDefinition(
        CALLBACK_MONTHLY_PLAN,
        "monthly",
        "Monthly",
        7.49,
        30,
        "Best for ongoing freelance outreach. $7.49 for 30 days with fast project matches and personalized filters.",
    ),
    CALLBACK_QUARTERLY_PLAN: PlanDefinition(
        CALLBACK_QUARTERLY_PLAN,
        "quarterly",
        "Quarterly",
        14.49,
        90,
        "Better value for a steady client pipeline. $14.49 for 90 days with fast project matches and personalized filters.",
    ),
}

UX_UI_WEBSITE_OPTIONS: tuple[tuple[str, str], ...] = (
    ("Contra [en]", "https://contra.com/opportunities?search=ui%2Fux%20designer"),
    ("Dribbble Jobs [en]", "https://dribbble.com/jobs?location=Anywhere&remote=true"),
    ("We Work Remotely [en]", "https://weworkremotely.com/categories/remote-design-jobs"),
    ("Wellfound [en]", "https://wellfound.com/role/ui-ux-designer"),
    ("Built In [en]", "https://builtin.com/jobs/remote/hybrid/office/designer?search=Web+Designer&allLocations=true"),
    ("FlexJobs [en]", "https://www.flexjobs.com/remote-jobs/ux-designer?sortbyposteddate=true&page=1"),
    ("🇷🇺 FL.ru Projects [ru]", "https://www.fl.ru/projects/?kind=1"),
    ("🇷🇺 Workspace [ru]", "https://workspace.ru/web-design/"),
)


def format_utc_readable(value: str) -> str:
    return parse_utc_iso(value).strftime("%Y-%m-%d %H:%M UTC")


def truncate_callback_id(value: str) -> str:
    return value[:16]


class TelegramMenuBot:
    def __init__(self, settings: TelegramBotSettings, logger: logging.Logger) -> None:
        self.settings = settings
        self.logger = logger
        self.store = SubscriptionStore(settings.telegram_subs_db_path)
        self.state_store = StateStore(settings.state_db_path)
        self.user_message_logger = UserMessageLogger(settings.telegram_user_log_dir)
        self.nowpayments = NowPaymentsClient(
            api_key=settings.nowpayments_api_key,
            logger=logger,
            base_url=settings.nowpayments_base_url,
            email=settings.nowpayments_email,
            password=settings.nowpayments_password,
        )
        self.website_guard = WebsiteGuard(
            openai_api_key=settings.openai_api_key,
            openai_model=settings.openai_model_website_guard or settings.openai_model,
            logger=logger,
        )
        self.filter_ai = FilterAI(
            openai_api_key=settings.openai_api_key,
            openai_model=settings.openai_model_filter_match or settings.openai_model,
            logger=logger,
            keyword_model=settings.openai_model_keyword_expansion,
            final_match_model=settings.openai_model_filter_match,
        )
        self.subscriber_notifier = SubscriberNotifier(
            bot_token=settings.telegram_bot_token,
            store=self.store,
            logger=logger,
            user_message_logger=self.user_message_logger,
            openai_api_key=settings.openai_api_key,
            openai_model=settings.openai_model_filter_match or settings.openai_model,
            keyword_model=settings.openai_model_keyword_expansion,
            final_match_model=settings.openai_model_filter_match,
        )
        self.scrape_sites_file_path = settings.scrape_sites_file_path
        self.pending_inputs: dict[int, str] = {}
        self.pending_location_candidates: dict[int, LocationNormalization] = {}
        self.pending_location_modes: dict[int, str] = {}
        self.pending_location_selections: dict[int, list[str]] = {}
        self.pending_keyword_reviews: dict[int, KeywordInterpretation] = {}
        self.pending_project_filter_fields: dict[int, str] = {}
        self.pending_website_choices: dict[int, set[str]] = {}
        self.pending_custom_website_urls: dict[int, str] = {}
        self.pending_custom_website_currency: dict[int, str] = {}
        self.pending_match_feedback_event_ids: dict[int, int] = {}
        self.website_picker_cache: dict[int, list[str]] = {}
        self.deleted_alert_snapshots: dict[int, DeletedAlertSnapshot] = {}
        self.flow_origin: dict[int, str] = {}
        self.flow_step: dict[int, str] = {}
        self.custom_website_guide_image_path = Path(__file__).resolve().parents[1] / CUSTOM_WEBSITE_GUIDE_IMAGE
        self._polling_lock_path: Path = (
            Path(__file__).resolve().parents[1] / "state" / "telegram_menu_bot.polling.lock"
        )
        self._polling_lock_fd: int | None = None

        self.application: Application = ApplicationBuilder().token(settings.telegram_bot_token).build()
        self.subscription_admin_notifier = SubscriptionAdminNotifier(
            bot=self.application.bot,
            logger=logger,
            admin_chat_ids=settings.subscription_admin_chat_ids,
        )
        self._register_handlers()

    def _register_handlers(self) -> None:
        self.application.add_handler(CommandHandler("start", self.handle_start))
        self.application.add_handler(CallbackQueryHandler(self.handle_callback))
        self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_text_message))
        self.application.add_error_handler(self.handle_error)

    def _ui_language(self, user_id: int) -> str:
        store = getattr(self, "store", None)
        if store is None:
            return "en"
        getter = getattr(store, "get_ui_language", None)
        if callable(getter):
            return normalize_ui_language(getter(user_id))
        return ""

    def _ui_language_or_default(self, user_id: int) -> str:
        return self._ui_language(user_id) or "en"

    def _t(self, user_id: int, key: str, **kwargs: object) -> str:
        return localized_text(self._ui_language_or_default(user_id), key, **kwargs)

    def _btn(self, user_id: int, key: str, **kwargs: object) -> str:
        return localized_button(self._ui_language_or_default(user_id), key, **kwargs)

    def _working_input_language(self, user_id: int, text: str) -> str:
        saved = self._ui_language(user_id)
        if saved in {"en", "ru"}:
            return saved
        if contains_cyrillic(text):
            return "ru"
        detected = detect_language(text=text)
        return detected if detected in {"en", "ru"} else "en"

    @staticmethod
    def _language_callback_to_code(callback_data: str) -> str:
        if callback_data == CALLBACK_LANGUAGE_RU:
            return "ru"
        if callback_data == CALLBACK_LANGUAGE_EN:
            return "en"
        return ""

    def _language_selector_markup(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton(localized_button("en", "english"), callback_data=CALLBACK_LANGUAGE_EN)],
                [InlineKeyboardButton(localized_button("ru", "russian"), callback_data=CALLBACK_LANGUAGE_RU)],
            ]
        )

    async def _show_language_gate(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        user_id: int,
        username: str,
    ) -> None:
        await self._send_and_log(
            context=context,
            chat_id=chat_id,
            user_id=user_id,
            username=username,
            text=(
                f"{localized_text('en', 'language_gate_title')}\n"
                f"{localized_text('en', 'language_gate_text')}"
            ),
            reply_markup=self._language_selector_markup(),
        )

    async def _maybe_show_language_gate(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        user_id: int,
        username: str,
    ) -> bool:
        if self._ui_language(user_id):
            return False
        await self._show_language_gate(context, chat_id, user_id, username)
        return True

    @staticmethod
    def _is_pid_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except PermissionError:
            return True
        except OSError:
            return False
        return True

    def _acquire_polling_lock(self) -> bool:
        self._polling_lock_path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(self._polling_lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode("ascii", errors="ignore"))
                self._polling_lock_fd = fd
                return True
            except FileExistsError:
                existing_pid: int | None = None
                with contextlib.suppress(OSError, ValueError):
                    existing_pid = int(self._polling_lock_path.read_text(encoding="utf-8").strip())
                if existing_pid is not None and self._is_pid_alive(existing_pid):
                    return False
                with contextlib.suppress(FileNotFoundError, PermissionError, OSError):
                    self._polling_lock_path.unlink()
        return False

    def _release_polling_lock(self) -> None:
        if self._polling_lock_fd is not None:
            with contextlib.suppress(OSError):
                os.close(self._polling_lock_fd)
            self._polling_lock_fd = None
        with contextlib.suppress(FileNotFoundError, PermissionError, OSError):
            self._polling_lock_path.unlink()

    async def run_long_polling(self) -> None:
        if not self._acquire_polling_lock():
            self.logger.error(
                "[telegram-menu] polling lock is already held. Another local instance is running: %s",
                self._polling_lock_path,
            )
            return

        updater = self.application.updater
        if updater is None:
            self._release_polling_lock()
            raise RuntimeError("Polling updater is not available.")

        initialized = False
        started = False
        polling_started = False
        background_task: asyncio.Task[None] | None = None
        polling_conflict = asyncio.Event()
        loop = asyncio.get_running_loop()

        def _polling_error_callback(exc: TelegramError) -> None:
            if isinstance(exc, Conflict):
                self.logger.error(
                    "[telegram-menu] polling conflict: %s. Another getUpdates consumer is active for this bot token.",
                    str(exc),
                )
                loop.call_soon_threadsafe(polling_conflict.set)
                return
            self.logger.warning("[telegram-menu] polling error: %s", str(exc))

        try:
            await self.application.initialize()
            initialized = True
            await self.application.start()
            started = True
            await updater.start_polling(allowed_updates=Update.ALL_TYPES, error_callback=_polling_error_callback)
            polling_started = True
            background_task = asyncio.create_task(self._background_loop())
            self.logger.info("[telegram-menu] long polling started")

            while True:
                if polling_conflict.is_set():
                    break
                if polling_started and not updater.running:
                    break
                await asyncio.sleep(1)
        finally:
            if background_task is not None:
                background_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await background_task
            if polling_started and updater.running:
                await updater.stop()
            if started and self.application.running:
                await self.application.stop()
            if initialized:
                await self.application.shutdown()
            try:
                try:
                    self.store.close()
                finally:
                    self.state_store.close()
            finally:
                self._release_polling_lock()

    async def _background_loop(self) -> None:
        while True:
            try:
                await self._refresh_pending_payments(limit=100)
                await self._send_pending_system_notices(limit=50)
                await self.subscriber_notifier.flush_due_notifications(limit=200)
                await self._send_pending_manual_reviews(limit=20)
                await self._send_pending_human_reviews(limit=20)
                await self._send_no_posts_reports()
                await self._send_expiry_reminders()
                await self._send_expiration_notices()
            except Exception as exc:  # noqa: BLE001
                self.logger.warning("[telegram-menu] background loop error: %s", str(exc))
            await asyncio.sleep(20)

    async def handle_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        chat = update.effective_chat
        if user is None or chat is None:
            return

        username = self._resolve_username(user)
        self._log_inbound(user_id=user.id, username=username, text="/start", event_type="command")
        self._clear_user_flow_state(user.id)
        await self._maybe_notify_admin_first_start(user.id, username)

        if not self._ui_language(user.id):
            await self._show_language_gate(context, chat.id, user.id, username)
            return

        has_profile = bool(
            self.store.has_alert_configuration(user.id)
            or self.store.get_latest_subscription(user.id) is not None
            or self.store.has_claimed_trial(user.id)
        )
        is_first_time = not self.store.has_seen_landing(user.id) and not has_profile
        self.store.mark_seen_landing(user.id)
        if is_first_time:
            await self._send_and_log(
                context=context,
                chat_id=chat.id,
                user_id=user.id,
                username=username,
                text=self._welcome_text(user.id),
                reply_markup=self._welcome_markup(user.id),
            )
            return

        await self._show_main_menu(context, chat.id, user.id, username)

    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        user = update.effective_user
        chat = update.effective_chat
        if query is None or user is None or chat is None:
            return

        callback_data = query.data or ""
        username = self._resolve_username(user)
        self._log_inbound(user_id=user.id, username=username, text=callback_data, event_type="callback")

        if callback_data in {CALLBACK_LANGUAGE_EN, CALLBACK_LANGUAGE_RU}:
            chosen_language = self._language_callback_to_code(callback_data)
            if chosen_language:
                self.store.set_ui_language(user.id, chosen_language)
                self._log_filter_update(user.id, username, "ui_language", chosen_language)
                self.store.mark_seen_landing(user.id)
                await self._safe_query_answer(
                    query,
                    self._t(user.id, "language_saved_ru" if chosen_language == "ru" else "language_saved_en"),
                )
                has_profile = bool(
                    self.store.has_alert_configuration(user.id)
                    or self.store.get_latest_subscription(user.id) is not None
                    or self.store.has_claimed_trial(user.id)
                )
                if has_profile:
                    await self._show_main_menu(context, chat.id, user.id, username)
                else:
                    await self._send_and_log(
                        context=context,
                        chat_id=chat.id,
                        user_id=user.id,
                        username=username,
                        text=self._welcome_text(user.id),
                        reply_markup=self._welcome_markup(user.id),
                    )
            return

        if not self._ui_language(user.id):
            await self._safe_query_answer(query)
            await self._show_language_gate(context, chat.id, user.id, username)
            return

        if callback_data.startswith(SubscriberNotifier.FEEDBACK_UP_PREFIX):
            await self._handle_match_feedback(context, chat.id, user.id, username, callback_data, "up", query=query)
            return
        if callback_data.startswith(SubscriberNotifier.FEEDBACK_DOWN_PREFIX):
            await self._handle_match_feedback(context, chat.id, user.id, username, callback_data, "down", query=query)
            return
        if callback_data.startswith(CALLBACK_MANUAL_REVIEW_FORCE_SEND_PREFIX):
            await self._handle_manual_review_decision(
                context=context,
                chat_id=chat.id,
                reviewer_id=user.id,
                reviewer_username=username,
                callback_data=callback_data,
                action="force_send",
                query=query,
            )
            return
        if callback_data.startswith(CALLBACK_MANUAL_REVIEW_KEEP_REJECTING_PREFIX):
            await self._handle_manual_review_decision(
                context=context,
                chat_id=chat.id,
                reviewer_id=user.id,
                reviewer_username=username,
                callback_data=callback_data,
                action="keep_rejecting",
                query=query,
            )
            return
        if callback_data.startswith(CALLBACK_MANUAL_REVIEW_RECHECK_PREFIX):
            await self._handle_manual_review_decision(
                context=context,
                chat_id=chat.id,
                reviewer_id=user.id,
                reviewer_username=username,
                callback_data=callback_data,
                action="recheck",
                query=query,
            )
            return
        if callback_data.startswith(CALLBACK_HUMAN_REVIEW_SEND_PREFIX):
            await self._handle_human_review_decision(
                context=context,
                chat_id=chat.id,
                reviewer_id=user.id,
                reviewer_username=username,
                callback_data=callback_data,
                action="approve_send",
                query=query,
            )
            return
        if callback_data.startswith(CALLBACK_HUMAN_REVIEW_SAVE_PREFIX):
            await self._handle_human_review_decision(
                context=context,
                chat_id=chat.id,
                reviewer_id=user.id,
                reviewer_username=username,
                callback_data=callback_data,
                action="approve_no_send",
                query=query,
            )
            return
        if callback_data.startswith(CALLBACK_HUMAN_REVIEW_NEGLECT_PREFIX):
            await self._handle_human_review_decision(
                context=context,
                chat_id=chat.id,
                reviewer_id=user.id,
                reviewer_username=username,
                callback_data=callback_data,
                action="neglect",
                query=query,
            )
            return
        if callback_data.startswith("human_review_"):
            await self._safe_query_answer(
                query,
                "This human-review button is outdated. Please use the latest review card.",
                show_alert=True,
            )
            return

        await self._safe_query_answer(query)

        if callback_data == CALLBACK_CREATE_ALERT:
            await self._start_alert_creation_flow(context, chat.id, user.id, username)
            return
        if callback_data == CALLBACK_HOW_IT_WORKS:
            await self._show_about(context, chat.id, user.id, username)
            return
        if callback_data == CALLBACK_TRIAL_SCREEN:
            await self._show_trial_screen(context, chat.id, user.id, username)
            return
        if callback_data == CALLBACK_TRIAL_START:
            await self._activate_trial(context, chat.id, user.id, username)
            return
        if callback_data == CALLBACK_VIEW_PLANS:
            await self._show_subscription_menu(context, chat.id, user.id, username)
            return
        if callback_data == CALLBACK_YEARLY_PLAN:
            await self._send_and_log(
                context=context,
                chat_id=chat.id,
                user_id=user.id,
                username=username,
                text=self._localized_inline(
                    self._ui_language_or_default(user.id),
                    "That old plan is no longer available.\nHere are the current plans.",
                    "Этот старый план больше недоступен.\nВот актуальные планы.",
                ),
                reply_markup=self._subscription_menu_markup(user.id),
            )
            return
        if callback_data in {CALLBACK_MY_ALERT, CALLBACK_VIEW_MY_ALERT}:
            await self._show_my_alert(context, chat.id, user.id, username)
            return
        if callback_data in {CALLBACK_SUBSCRIPTION_MENU, CALLBACK_VIEW_SUBSCRIPTION}:
            await self._show_subscription_menu(context, chat.id, user.id, username)
            return
        if callback_data == CALLBACK_SUPPORT_MENU:
            await self._show_support_menu(context, chat.id, user.id, username)
            return
        if callback_data == CALLBACK_ABOUT_MENU:
            await self._show_about(context, chat.id, user.id, username)
            return
        if callback_data == CALLBACK_EDIT_LANGUAGE:
            self._clear_user_flow_state(user.id)
            await self._show_language_gate(context, chat.id, user.id, username)
            return
        if callback_data == CALLBACK_BACK_TO_MAIN:
            self._clear_user_flow_state(user.id)
            await self._show_main_menu(context, chat.id, user.id, username)
            return
        if callback_data == CALLBACK_BACK_TO_MY_ALERT:
            self._clear_user_flow_state(user.id)
            await self._show_my_alert(context, chat.id, user.id, username)
            return
        if callback_data == CALLBACK_BACK_TO_EDIT_ALERT:
            self._clear_user_flow_state(user.id)
            await self._show_edit_alert_menu(context, chat.id, user.id, username)
            return
        if callback_data == CALLBACK_BACK_TO_SUBSCRIPTION:
            self._clear_user_flow_state(user.id)
            await self._show_subscription_menu(context, chat.id, user.id, username)
            return
        if callback_data == CALLBACK_BACK_TO_ROLE_STEP:
            await self._show_role_prompt(context, chat.id, user.id, username)
            return
        if callback_data == CALLBACK_BACK_TO_LOCATION_STEP:
            await self._show_role_prompt(context, chat.id, user.id, username)
            return
        if callback_data == CALLBACK_BACK_TO_PROJECT_FILTERS_STEP:
            await self._show_project_preferences_prompt(context, chat.id, user.id, username)
            return
        if callback_data == CALLBACK_BACK_TO_SALARY_STEP:
            await self._show_project_preferences_prompt(context, chat.id, user.id, username)
            return
        if callback_data == CALLBACK_BACK_TO_WEBSITES_STEP:
            await self._show_website_picker(context, chat.id, user.id, username)
            return
        if callback_data == CALLBACK_WRITE_SUPPORT:
            await self._show_support_menu(context, chat.id, user.id, username)
            return
        if callback_data == CALLBACK_ALERT_HEALTH:
            await self._show_alert_health_report(context, chat.id, user.id, username, mark_as_sent=True)
            return
        if callback_data == CALLBACK_ALERT_HEALTH_CONTINUE:
            self.store.mark_inactivity_report_sent(
                user.id,
                anchor_utc=self._current_alert_activity_anchor_utc(user.id),
            )
            await self._send_and_log(
                context=context,
                chat_id=chat.id,
                user_id=user.id,
                username=username,
                text=self._localized_inline(
                    self._ui_language_or_default(user.id),
                    "ZapLance will keep your current search settings and selected sources.",
                    "Я оставлю текущие настройки поиска и выбранные источники без изменений.",
                ),
                reply_markup=self._my_alert_markup(user.id),
            )
            return
        if callback_data in {CALLBACK_EDIT_ALERT, CALLBACK_ALERT_EDIT_SETTINGS}:
            await self._show_edit_alert_menu(context, chat.id, user.id, username)
            return
        if callback_data == CALLBACK_EDIT_ROLE:
            self.flow_origin[user.id] = FLOW_EDIT
            self.flow_step[user.id] = STEP_ROLE
            await self._show_role_prompt(context, chat.id, user.id, username)
            return
        if callback_data == CALLBACK_EDIT_LOCATION:
            self._clear_user_flow_state(user.id)
            await self._show_edit_alert_menu(context, chat.id, user.id, username)
            return
        if callback_data in {
            CALLBACK_LOCATION_REMOTE_GLOBAL,
            CALLBACK_LOCATION_REMOTE_COUNTRY,
            CALLBACK_LOCATION_ONSITE_COUNTRY,
            CALLBACK_LOCATION_CUSTOM,
            CALLBACK_LOCATION_SAVE,
            CALLBACK_LOCATION_CLEAR,
            CALLBACK_LOCATION_CONFIRM,
            CALLBACK_LOCATION_REENTER,
        }:
            self.pending_inputs.pop(user.id, None)
            self.pending_location_candidates.pop(user.id, None)
            self.pending_location_modes.pop(user.id, None)
            self.pending_location_selections.pop(user.id, None)
            await self._send_and_log(
                context=context,
                chat_id=chat.id,
                user_id=user.id,
                username=username,
                text=self._localized_inline(
                    self._ui_language_or_default(user.id),
                    "Location filters were removed. Please continue with project filters.",
                    "Фильтры по местоположению удалены. Пожалуйста, перейдите к фильтрам проектов.",
                ),
                reply_markup=self._project_preferences_prompt_markup(user.id),
            )
            return
        if callback_data in {CALLBACK_EDIT_PROJECT_FILTERS, CALLBACK_EDIT_SALARY}:
            self.flow_origin[user.id] = FLOW_EDIT
            self.flow_step[user.id] = STEP_PROJECT_FILTERS
            await self._show_project_preferences_prompt(context, chat.id, user.id, username)
            return
        if callback_data == CALLBACK_EDIT_SOURCES:
            self.flow_origin[user.id] = FLOW_EDIT
            await self._show_website_picker(context, chat.id, user.id, username)
            return
        if callback_data == CALLBACK_EDIT_KEYWORDS:
            self.flow_origin[user.id] = FLOW_EDIT
            self.flow_step[user.id] = STEP_KEYWORDS
            await self._show_keywords_prompt(context, chat.id, user.id, username)
            return
        if callback_data == CALLBACK_ACTIVATE_ALERT:
            await self._activate_alert(context, chat.id, user.id, username)
            return
        if callback_data == CALLBACK_TOGGLE_ALERT:
            await self._toggle_alert(context, chat.id, user.id, username)
            return
        if callback_data == CALLBACK_DELETE_ALERT:
            snapshot = self._capture_deleted_alert_snapshot(user.id)
            if snapshot is not None:
                self.deleted_alert_snapshots[user.id] = snapshot
            else:
                self.deleted_alert_snapshots.pop(user.id, None)
            self.store.clear_user_alert(user.id)
            self._log_filter_update(user.id, username, "alert_deleted", "true")
            self._clear_user_flow_state(user.id)
            await self._send_and_log(
                context=context,
                chat_id=chat.id,
                user_id=user.id,
                username=username,
                text=self._t(user.id, "alert_deleted"),
                reply_markup=self._deleted_alert_markup(user.id),
            )
            return
        if callback_data == CALLBACK_UNDO_DELETE_ALERT:
            await self._undo_deleted_alert(context, chat.id, user.id, username)
            return

        if callback_data == CALLBACK_ROLE_CUSTOM:
            await self._save_role_and_continue(context, chat.id, user.id, username, CANONICAL_ROLE_TITLE)
            return
        if callback_data == CALLBACK_LOCATION_REMOTE_COUNTRY:
            self.pending_location_candidates.pop(user.id, None)
            self.pending_location_modes[user.id] = LOCATION_MODE_REMOTE_COUNTRY
            self.pending_inputs[user.id] = WAITING_LOCATION_INPUT
            self.flow_step[user.id] = STEP_LOCATION
            await self._send_and_log(
                context=context,
                chat_id=chat.id,
                user_id=user.id,
                username=username,
                text=self._t(user.id, "location_remote_country_prompt"),
                reply_markup=self._location_text_input_markup(user.id),
            )
            return
        if callback_data == CALLBACK_LOCATION_ONSITE_COUNTRY:
            self.pending_location_candidates.pop(user.id, None)
            self.pending_location_modes[user.id] = LOCATION_MODE_ONSITE_HYBRID_COUNTRY
            self.pending_inputs[user.id] = WAITING_LOCATION_INPUT
            self.flow_step[user.id] = STEP_LOCATION
            await self._send_and_log(
                context=context,
                chat_id=chat.id,
                user_id=user.id,
                username=username,
                text=self._t(user.id, "location_onsite_country_prompt"),
                reply_markup=self._location_text_input_markup(user.id),
            )
            return
        if callback_data == CALLBACK_LOCATION_CUSTOM:
            self.pending_location_candidates.pop(user.id, None)
            self.pending_location_modes[user.id] = LOCATION_MODE_CUSTOM
            self.pending_inputs[user.id] = WAITING_LOCATION_INPUT
            self.flow_step[user.id] = STEP_LOCATION
            await self._send_and_log(
                context=context,
                chat_id=chat.id,
                user_id=user.id,
                username=username,
                text=self._t(user.id, "location_custom_prompt"),
                reply_markup=self._location_text_input_markup(user.id),
            )
            return
        if callback_data == CALLBACK_LOCATION_REENTER:
            self.pending_location_candidates.pop(user.id, None)
            self.pending_inputs[user.id] = WAITING_LOCATION_INPUT
            await self._send_and_log(
                context=context,
                chat_id=chat.id,
                user_id=user.id,
                username=username,
                text=self._t(user.id, "location_custom_prompt"),
                reply_markup=self._location_text_input_markup(user.id),
            )
            return
        if callback_data.startswith(CALLBACK_PROJECT_FILTERS_FIELD_PREFIX):
            field_name = callback_data[len(CALLBACK_PROJECT_FILTERS_FIELD_PREFIX) :].strip()
            if field_name:
                self.pending_project_filter_fields[user.id] = field_name
                self.pending_inputs[user.id] = WAITING_PROJECT_FILTERS_INPUT
                self.flow_step[user.id] = STEP_PROJECT_FILTERS
                await self._send_and_log(
                    context=context,
                    chat_id=chat.id,
                    user_id=user.id,
                    username=username,
                    text=self._project_filter_input_text(user.id, field_name),
                    reply_markup=self._project_preferences_input_markup(user.id),
                )
            return
        if callback_data == CALLBACK_KEYWORDS_ADD:
            self.pending_inputs[user.id] = WAITING_KEYWORDS_INPUT
            self.flow_step[user.id] = STEP_KEYWORDS
            await self._send_and_log(
                context=context,
                chat_id=chat.id,
                user_id=user.id,
                username=username,
                text=self._t(user.id, "keywords_input_prompt"),
                reply_markup=self._keywords_input_markup(user.id),
            )
            return
        if callback_data == CALLBACK_KEYWORDS_REENTER:
            self.pending_keyword_reviews.pop(user.id, None)
            self.pending_inputs[user.id] = WAITING_KEYWORDS_INPUT
            await self._send_and_log(
                context=context,
                chat_id=chat.id,
                user_id=user.id,
                username=username,
                text=self._t(user.id, "keywords_reenter_prompt"),
                reply_markup=self._keywords_input_markup(user.id),
            )
            return
        if callback_data in {
            CALLBACK_EDIT_DELIVERY,
            CALLBACK_DELIVERY_INSTANT,
            CALLBACK_DELIVERY_DIGEST,
            CALLBACK_DELIVERY_SET_TIMEZONE,
            CALLBACK_DELIVERY_SET_QUIET,
            CALLBACK_DELIVERY_CLEAR_QUIET,
            CALLBACK_BACK_TO_DELIVERY,
        }:
            self.pending_inputs.pop(user.id, None)
            await self._send_edit_saved(context, chat.id, user.id, username)
            return

        if callback_data.startswith(CALLBACK_ROLE_PRESET_PREFIX):
            role_title = callback_data[len(CALLBACK_ROLE_PRESET_PREFIX) :].replace("_", " ")
            await self._save_role_and_continue(context, chat.id, user.id, username, role_title)
            return
        if callback_data == CALLBACK_ROLE_CUSTOM:
            self.pending_inputs[user.id] = WAITING_ROLE_INPUT
            await self._send_and_log(
                context=context,
                chat_id=chat.id,
                user_id=user.id,
                username=username,
                text=(
                    "🧩 Send your primary specialty.\n"
                    "Examples: UX/UI Design, Frontend Development, Webflow, Copywriting, Marketing, Automation."
                ),
                reply_markup=self._role_text_input_markup(user.id),
            )
            return

        if callback_data == CALLBACK_LOCATION_REMOTE_GLOBAL or callback_data.startswith(f"{CALLBACK_LOCATION_REMOTE_GLOBAL}_"):
            await self._save_location_choice(context, chat.id, user.id, username, CALLBACK_LOCATION_REMOTE_GLOBAL)
            return
        if callback_data in LOCATION_OPTION_LABELS:
            await self._save_location_choice(context, chat.id, user.id, username, callback_data)
            return
        if callback_data == CALLBACK_LOCATION_SAVE:
            await self._persist_location_selections(context, chat.id, user.id, username)
            return
        if callback_data == CALLBACK_LOCATION_CLEAR:
            self.pending_location_candidates.pop(user.id, None)
            self.pending_location_modes.pop(user.id, None)
            self.pending_inputs.pop(user.id, None)
            self.pending_location_selections[user.id] = []
            await self._show_location_prompt(context, chat.id, user.id, username)
            return
        if callback_data == CALLBACK_LOCATION_REMOTE_COUNTRY:
            self.pending_location_candidates.pop(user.id, None)
            self.pending_location_modes[user.id] = LOCATION_MODE_REMOTE_COUNTRY
            self.pending_inputs[user.id] = WAITING_LOCATION_INPUT
            self.flow_step[user.id] = STEP_LOCATION
            await self._send_and_log(
                context=context,
                chat_id=chat.id,
                user_id=user.id,
                username=username,
                text=(
                    "🌍 Enter the country where you want remote projects or contracts.\n"
                    "Example: Egypt, Germany, or United States."
                ),
                reply_markup=self._location_text_input_markup(user.id),
            )
            return
        if callback_data == CALLBACK_LOCATION_ONSITE_COUNTRY:
            self.pending_location_candidates.pop(user.id, None)
            self.pending_location_modes[user.id] = LOCATION_MODE_ONSITE_HYBRID_COUNTRY
            self.pending_inputs[user.id] = WAITING_LOCATION_INPUT
            self.flow_step[user.id] = STEP_LOCATION
            await self._send_and_log(
                context=context,
                chat_id=chat.id,
                user_id=user.id,
                username=username,
                text=(
                    "🏢 Enter the country where you want on-site or hybrid contracts.\n"
                    "Example: Egypt, Germany, or United States."
                ),
                reply_markup=self._location_text_input_markup(user.id),
            )
            return
        if callback_data == CALLBACK_LOCATION_CUSTOM:
            self.pending_location_candidates.pop(user.id, None)
            self.pending_location_modes[user.id] = LOCATION_MODE_CUSTOM
            self.pending_inputs[user.id] = WAITING_LOCATION_INPUT
            self.flow_step[user.id] = STEP_LOCATION
            await self._send_and_log(
                context=context,
                chat_id=chat.id,
                user_id=user.id,
                username=username,
                text=(
                    "📍 Enter the location you want us to consider.\n"
                    "Please include the country and the state/province too if you know them.\n"
                    "Example: USA, California or Egypt, Cairo."
                ),
                reply_markup=self._location_text_input_markup(user.id),
            )
            return
        if callback_data == CALLBACK_LOCATION_REENTER:
            self.pending_location_candidates.pop(user.id, None)
            self.pending_inputs[user.id] = WAITING_LOCATION_INPUT
            await self._send_and_log(
                context=context,
                chat_id=chat.id,
                user_id=user.id,
                username=username,
                text="✏️ Send the location again.",
                reply_markup=self._location_text_input_markup(user.id),
            )
            return
        if callback_data == CALLBACK_LOCATION_CONFIRM:
            await self._confirm_custom_location(context, chat.id, user.id, username)
            return

        if callback_data == CALLBACK_SALARY_REQUIRED:
            await self._show_project_preferences_prompt(context, chat.id, user.id, username)
            return
        if callback_data == CALLBACK_SALARY_OPTIONAL:
            await self._show_project_preferences_prompt(context, chat.id, user.id, username)
            return
        if callback_data == CALLBACK_PROJECT_FILTERS_ADD:
            self.pending_inputs[user.id] = WAITING_PROJECT_FILTERS_INPUT
            self.flow_step[user.id] = STEP_PROJECT_FILTERS
            await self._send_and_log(
                context=context,
                chat_id=chat.id,
                user_id=user.id,
                username=username,
                text=self._project_preferences_input_text(user.id),
                reply_markup=self._project_preferences_input_markup(user.id),
            )
            return
        if callback_data == CALLBACK_PROJECT_FILTERS_SKIP:
            self.pending_inputs.pop(user.id, None)
            if self.flow_origin.get(user.id) == FLOW_CREATE:
                await self._show_website_picker(context, chat.id, user.id, username)
            else:
                await self._send_edit_saved(context, chat.id, user.id, username)
            return

        if callback_data.startswith(CALLBACK_WEBSITE_PICK_PREFIX):
            await self._toggle_website_choice(context, chat.id, user.id, username, callback_data, edit_query=query)
            return
        if callback_data == CALLBACK_WEBSITE_SELECT_ALL:
            await self._select_all_website_choices(context, chat.id, user.id, username, edit_query=query)
            return
        if callback_data == CALLBACK_WEBSITE_CUSTOM:
            self.pending_inputs[user.id] = WAITING_WEBSITE_INPUT
            self.flow_step[user.id] = STEP_WEBSITES
            await self._send_custom_website_guide_image(
                context=context,
                chat_id=chat.id,
                user_id=user.id,
                username=username,
                text=self._t(user.id, "website_custom_prompt"),
                reply_markup=self._website_text_input_markup(user.id),
            )
            return
        if callback_data.startswith(CALLBACK_WEBSITE_CURRENCY_PREFIX):
            currency_code = normalize_website_currency(callback_data[len(CALLBACK_WEBSITE_CURRENCY_PREFIX) :])
            pending_url = self.pending_custom_website_urls.get(user.id, "")
            if pending_url and currency_code:
                self.pending_custom_website_currency[user.id] = currency_code
                selected = self.pending_website_choices.setdefault(user.id, set(self.store.get_user_websites(user.id)))
                selected.add(pending_url)
                await self._send_and_log(
                    context=context,
                    chat_id=chat.id,
                    user_id=user.id,
                    username=username,
                    text=self._t(user.id, "website_added"),
                    reply_markup=self._website_added_markup(user.id),
                )
            else:
                await self._show_website_picker(context, chat.id, user.id, username, edit_query=query)
            return
        if callback_data == CALLBACK_WEBSITE_DONE:
            await self._save_websites_and_continue(context, chat.id, user.id, username)
            return
        if callback_data == CALLBACK_WEBSITE_BACK:
            if self.flow_origin.get(user.id) == FLOW_CREATE:
                await self._show_project_preferences_prompt(context, chat.id, user.id, username)
            else:
                await self._show_edit_alert_menu(context, chat.id, user.id, username)
            return

        if callback_data == CALLBACK_KEYWORDS_ADD:
            self.pending_inputs[user.id] = WAITING_KEYWORDS_INPUT
            self.flow_step[user.id] = STEP_KEYWORDS
            await self._send_and_log(
                context=context,
                chat_id=chat.id,
                user_id=user.id,
                username=username,
                text=(
                    "🏷️ Send project keywords separated by commas.\n"
                    "Example: landing page, dashboard, mobile app redesign, SaaS audit, Webflow, Figma, startup"
                ),
                reply_markup=self._keywords_input_markup(user.id),
            )
            return
        if callback_data == CALLBACK_KEYWORDS_SKIP:
            self.pending_keyword_reviews.pop(user.id, None)
            self.store.clear_keywords(user.id)
            self._log_filter_update(user.id, username, "keywords", "")
            if self.flow_origin.get(user.id) == FLOW_CREATE:
                await self._show_alert_summary(context, chat.id, user.id, username)
            else:
                await self._send_edit_saved(context, chat.id, user.id, username)
            return
        if callback_data == CALLBACK_KEYWORDS_CONFIRM:
            await self._confirm_keywords_and_continue(context, chat.id, user.id, username)
            return
        if callback_data == CALLBACK_KEYWORDS_REENTER:
            self.pending_keyword_reviews.pop(user.id, None)
            self.pending_inputs[user.id] = WAITING_KEYWORDS_INPUT
            await self._send_and_log(
                context=context,
                chat_id=chat.id,
                user_id=user.id,
                username=username,
                text=(
                    "🏷️ Send your keywords again.\n"
                    "Include the exact skills, deliverables, industries, tools, or project types you want us to track."
                ),
                reply_markup=self._keywords_input_markup(user.id),
            )
            return

        if callback_data == CALLBACK_DELIVERY_INSTANT:
            self.store.set_delivery_mode(user.id, "instant")
            self._log_filter_update(user.id, username, "delivery_mode", "instant")
            await self._show_delivery_settings(context, chat.id, user.id, username, edit_query=query)
            return
        if callback_data == CALLBACK_DELIVERY_DIGEST:
            self.store.set_delivery_mode(user.id, "digest")
            self._log_filter_update(user.id, username, "delivery_mode", "digest")
            await self._show_delivery_settings(context, chat.id, user.id, username, edit_query=query)
            return
        if callback_data == CALLBACK_DELIVERY_SET_TIMEZONE:
            self.pending_inputs[user.id] = WAITING_TIMEZONE_INPUT
            await self._send_and_log(
                context=context,
                chat_id=chat.id,
                user_id=user.id,
                username=username,
                text=self._localized_inline(
                    self._ui_language_or_default(user.id),
                    "🌍 Send your timezone as a UTC offset.\nExample: +02:00, -05:00, or +3",
                    "🌍 Отправьте ваш часовой пояс в формате UTC.\nНапример: +02:00, -05:00 или +3",
                ),
                reply_markup=self._delivery_text_input_markup(),
            )
            return
        if callback_data == CALLBACK_DELIVERY_SET_QUIET:
            self.pending_inputs[user.id] = WAITING_QUIET_HOURS_INPUT
            await self._send_and_log(
                context=context,
                chat_id=chat.id,
                user_id=user.id,
                username=username,
                text=self._localized_inline(
                    self._ui_language_or_default(user.id),
                    "🌙 Send your quiet hours in local time.\nExample: 22-8 or 23:00-07:00",
                    "🌙 Отправьте тихие часы по вашему местному времени.\nНапример: 22-8 или 23:00-07:00",
                ),
                reply_markup=self._delivery_text_input_markup(),
            )
            return
        if callback_data == CALLBACK_DELIVERY_CLEAR_QUIET:
            self.store.clear_quiet_hours(user.id)
            self._log_filter_update(user.id, username, "quiet_hours", "off")
            await self._show_delivery_settings(context, chat.id, user.id, username, edit_query=query)
            return
        if callback_data == CALLBACK_BACK_TO_DELIVERY:
            self.pending_inputs.pop(user.id, None)
            await self._show_delivery_settings(context, chat.id, user.id, username, edit_query=query)
            return

        if callback_data in PLAN_DEFINITIONS:
            await self._start_paid_plan_payment(
                context=context,
                chat_id=chat.id,
                user_id=user.id,
                username=username,
                plan=PLAN_DEFINITIONS[callback_data],
            )
            return
        if callback_data.startswith(CALLBACK_PAYMENT_UPDATE_PREFIX):
            local_payment_id = callback_data[len(CALLBACK_PAYMENT_UPDATE_PREFIX) :]
            await self._handle_payment_update(
                context=context,
                chat_id=chat.id,
                user_id=user.id,
                username=username,
                local_payment_id=local_payment_id,
                is_manual=True,
            )
            return
        if callback_data.startswith(CALLBACK_PAYMENT_CANCEL_PREFIX):
            local_payment_id = callback_data[len(CALLBACK_PAYMENT_CANCEL_PREFIX) :]
            await self._handle_payment_cancel(context, chat.id, user.id, username, local_payment_id)
            return
        if callback_data.startswith(CALLBACK_PAYMENT_CONTACT_PREFIX):
            await self._show_support_menu(context, chat.id, user.id, username)
            return

        await self._send_and_log(
            context=context,
            chat_id=chat.id,
            user_id=user.id,
            username=username,
            text=self._t(user.id, "use_menu_buttons"),
            reply_markup=self._main_menu_markup(user.id),
        )

    async def handle_text_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        chat = update.effective_chat
        message = update.effective_message
        if user is None or chat is None or message is None or message.text is None:
            return

        text = message.text.strip()
        username = self._resolve_username(user)
        self._log_inbound(user_id=user.id, username=username, text=text, event_type="message")
        pending_state = self.pending_inputs.get(user.id)

        if await self._maybe_show_language_gate(context, chat.id, user.id, username):
            return

        if pending_state == WAITING_ROLE_INPUT:
            await self._save_role_and_continue(context, chat.id, user.id, username, CANONICAL_ROLE_TITLE)
            return

        if pending_state == WAITING_TIMEZONE_INPUT:
            self.pending_inputs.pop(user.id, None)
            await self._send_edit_saved(context, chat.id, user.id, username)
            return

        if pending_state == WAITING_QUIET_HOURS_INPUT:
            self.pending_inputs.pop(user.id, None)
            await self._send_edit_saved(context, chat.id, user.id, username)
            return

        if pending_state == WAITING_SUPPORT_MESSAGE:
            self.pending_inputs.pop(user.id, None)
            await self._send_and_log(
                context=context,
                chat_id=chat.id,
                user_id=user.id,
                username=username,
                text=self._t(user.id, "support_contact_message"),
                reply_markup=self._main_menu_markup(user.id),
            )
            return

        if pending_state == WAITING_MATCH_FEEDBACK_INPUT:
            await self._handle_match_feedback_text(context, chat.id, user.id, username, text)
            return

        if pending_state == WAITING_SUPPORT_MESSAGE:
            self.pending_inputs.pop(user.id, None)
            await self._send_and_log(
                context=context,
                chat_id=chat.id,
                user_id=user.id,
                username=username,
                text="🛟 Please contact @zapcareers for support.",
                reply_markup=self._main_menu_markup(),
            )
            return

        if pending_state == WAITING_ROLE_INPUT:
            role_title = " ".join(text.split())
            if not role_title:
                await self._send_and_log(
                    context=context,
                    chat_id=chat.id,
                    user_id=user.id,
                    username=username,
                    text="🧩 Send your primary specialty so I can save it.",
                    reply_markup=self._role_text_input_markup(user.id),
                )
                return
            await self._save_role_and_continue(context, chat.id, user.id, username, role_title)
            return

        if pending_state == WAITING_LOCATION_INPUT:
            self.pending_inputs.pop(user.id, None)
            await self._send_and_log(
                context=context,
                chat_id=chat.id,
                user_id=user.id,
                username=username,
                text=self._localized_inline(
                    self._ui_language_or_default(user.id),
                    "Location filters were removed. Please continue with project filters.",
                    "Фильтры по местоположению удалены. Пожалуйста, перейдите к фильтрам проектов.",
                ),
                reply_markup=self._project_preferences_prompt_markup(user.id),
            )
            return

        if pending_state == WAITING_WEBSITE_INPUT:
            await self._handle_website_text(context, chat.id, user.id, username, text)
            return

        if pending_state == WAITING_PROJECT_FILTERS_INPUT:
            await self._handle_project_preferences_text(context, chat.id, user.id, username, text)
            return

        if pending_state == WAITING_KEYWORDS_INPUT:
            await self._handle_keywords_text(context, chat.id, user.id, username, text)
            return

        if pending_state == WAITING_TIMEZONE_INPUT:
            offset_minutes = self._parse_timezone_offset_minutes(text)
            if offset_minutes is None:
                await self._send_and_log(
                    context=context,
                    chat_id=chat.id,
                    user_id=user.id,
                    username=username,
                    text="🌍 Send a valid UTC offset like +02:00, -05:00, or +3.",
                    reply_markup=self._delivery_text_input_markup(),
                )
                return
            self.pending_inputs.pop(user.id, None)
            self.store.set_timezone_offset_minutes(user.id, offset_minutes)
            self._log_filter_update(user.id, username, "timezone_offset", self._timezone_offset_label(offset_minutes))
            await self._show_delivery_settings(context, chat.id, user.id, username)
            return

        if pending_state == WAITING_QUIET_HOURS_INPUT:
            quiet_range = self._parse_quiet_hours_input(text)
            if quiet_range is None:
                await self._send_and_log(
                    context=context,
                    chat_id=chat.id,
                    user_id=user.id,
                    username=username,
                    text="🌙 Send quiet hours like 22-8 or 23:00-07:00.",
                    reply_markup=self._delivery_text_input_markup(),
                )
                return
            self.pending_inputs.pop(user.id, None)
            start_hour, end_hour = quiet_range
            self.store.replace_quiet_hours_range(user.id, start_hour, end_hour)
            self._log_filter_update(user.id, username, "quiet_hours", f"{start_hour:02d}:00-{end_hour:02d}:00")
            await self._show_delivery_settings(context, chat.id, user.id, username)
            return

        await self._send_and_log(
            context=context,
            chat_id=chat.id,
            user_id=user.id,
            username=username,
            text=self._t(user.id, "use_menu_buttons"),
            reply_markup=self._main_menu_markup(user.id),
        )

    async def handle_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        self.logger.exception("[telegram-menu] unhandled error: %s", context.error)

    async def _start_alert_creation_flow(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        user_id: int,
        username: str,
    ) -> None:
        self._clear_user_flow_state(user_id)
        self.flow_origin[user_id] = FLOW_CREATE
        self.flow_step[user_id] = STEP_ROLE
        await self._show_role_prompt(context, chat_id, user_id, username)

    async def _start_alert_creation_flow_in_dm(self, user_id: int, username: str) -> None:
        self._clear_user_flow_state(user_id)
        self.flow_origin[user_id] = FLOW_CREATE
        self.flow_step[user_id] = STEP_ROLE
        await self._send_to_user_and_log(
            user_id=user_id,
            username=username,
            text=self._role_prompt_text(user_id),
            reply_markup=self._role_prompt_markup(user_id),
        )

    async def _show_main_menu(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        user_id: int,
        username: str,
    ) -> None:
        await self._send_and_log(
            context=context,
            chat_id=chat_id,
            user_id=user_id,
            username=username,
            text=self._main_menu_text(user_id),
            reply_markup=self._main_menu_markup(),
        )

    async def _show_how_it_works(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        user_id: int,
        username: str,
    ) -> None:
        await self._send_and_log(
            context=context,
            chat_id=chat_id,
            user_id=user_id,
            username=username,
            text=self._about_text(user_id),
            reply_markup=self._about_markup(),
        )

    async def _show_trial_screen(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        user_id: int,
        username: str,
    ) -> None:
        await self._send_and_log(
            context=context,
            chat_id=chat_id,
            user_id=user_id,
            username=username,
            text=self._trial_screen_text(user_id),
            reply_markup=self._trial_screen_markup(),
        )

    async def _show_subscription_menu(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        user_id: int,
        username: str,
    ) -> None:
        latest_sub = self.store.get_latest_subscription(user_id)
        await self._send_and_log(
            context=context,
            chat_id=chat_id,
            user_id=user_id,
            username=username,
            text=self._subscription_text(latest_sub, user_id),
            reply_markup=self._subscription_menu_markup(),
        )

    async def _show_support_menu(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        user_id: int,
        username: str,
    ) -> None:
        await self._send_and_log(
            context=context,
            chat_id=chat_id,
            user_id=user_id,
            username=username,
            text=self._support_text(user_id),
            reply_markup=self._support_menu_markup(),
        )

    async def _show_about(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        user_id: int,
        username: str,
    ) -> None:
        await self._send_and_log(
            context=context,
            chat_id=chat_id,
            user_id=user_id,
            username=username,
            text=self._about_text(user_id),
            reply_markup=self._about_markup(),
        )

    async def _show_role_prompt(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        user_id: int,
        username: str,
    ) -> None:
        self.flow_step[user_id] = STEP_ROLE
        await self._send_and_log(
            context=context,
            chat_id=chat_id,
            user_id=user_id,
            username=username,
            text=self._role_prompt_text(user_id),
            reply_markup=self._role_prompt_markup(user_id),
        )

    async def _save_role_and_continue(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        user_id: int,
        username: str,
        role_title: str,
    ) -> None:
        normalized_role = " ".join(role_title.split())
        self.pending_inputs.pop(user_id, None)
        self.store.set_role_preference(user_id, normalized_role)
        self._log_filter_update(user_id, username, "role", normalized_role)
        self._sync_saved_default_websites_for_role(user_id, username)
        if self.flow_origin.get(user_id) == FLOW_CREATE:
            await self._show_location_prompt(context, chat_id, user_id, username)
        else:
            await self._send_edit_saved(context, chat_id, user_id, username)

    def _pending_location_selection_values(self, user_id: int) -> list[str]:
        existing = self.pending_location_selections.get(user_id)
        if existing is not None:
            return list(existing)
        stored_value = self.store.get_location_preference(user_id)
        normalized = self._normalize_location_selection_entries(stored_value)
        self.pending_location_selections[user_id] = normalized
        return list(normalized)

    @classmethod
    def _normalize_location_selection_entries(cls, raw_value: str | list[str]) -> list[str]:
        raw_items = raw_value if isinstance(raw_value, list) else FilterAI.split_location_preferences(raw_value)
        if not raw_items and isinstance(raw_value, str) and raw_value.strip():
            raw_items = [raw_value.strip()]
        normalized: list[str] = []
        seen: set[str] = set()
        for raw_item in raw_items:
            display_value = cls._display_location(str(raw_item or "").strip())
            if not display_value or display_value == ALL_LOCATIONS_LABEL:
                continue
            key = display_value.lower()
            if key in seen:
                continue
            seen.add(key)
            normalized.append(display_value)
        return normalized

    def _location_selection_summary(self, user_id: int) -> str:
        selections = self._pending_location_selection_values(user_id)
        if not selections:
            return "• Any location"
        return "\n".join(f"• {item}" for item in selections)

    def _add_pending_location_selection(self, user_id: int, raw_value: str) -> None:
        selections = self._pending_location_selection_values(user_id)
        additions = self._normalize_location_selection_entries(raw_value)
        if not additions:
            self.pending_location_selections[user_id] = selections
            return
        seen = {item.lower() for item in selections}
        for item in additions:
            key = item.lower()
            if key in seen:
                continue
            seen.add(key)
            selections.append(item)
        self.pending_location_selections[user_id] = selections

    @staticmethod
    def _location_button_marker(is_selected: bool) -> str:
        return "✅" if is_selected else "▫️"

    @classmethod
    def _location_selection_kind(cls, raw_value: str) -> str:
        display_value = cls._display_location(raw_value)
        lowered = display_value.lower()
        remote_within_prefix = f"{REMOTE_WITHIN_COUNTRY_LABEL.lower()} "
        onsite_within_prefix = f"{ONSITE_HYBRID_WITHIN_COUNTRY_LABEL.lower()} "
        if lowered == REMOTE_GLOBAL_LABEL.lower():
            return CALLBACK_LOCATION_REMOTE_GLOBAL
        if lowered.startswith(remote_within_prefix):
            return CALLBACK_LOCATION_REMOTE_COUNTRY
        if lowered.startswith(onsite_within_prefix):
            return CALLBACK_LOCATION_ONSITE_COUNTRY
        return CALLBACK_LOCATION_CUSTOM

    def _has_pending_location_kind(self, user_id: int, callback_data: str) -> bool:
        selections = self._pending_location_selection_values(user_id)
        if callback_data == CALLBACK_LOCATION_REMOTE_GLOBAL:
            return any(item.lower() == REMOTE_GLOBAL_LABEL.lower() for item in selections)
        return any(self._location_selection_kind(item) == callback_data for item in selections)

    async def _send_ai_processing_notice(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        user_id: int,
        username: str,
    ) -> None:
        await self._send_and_log(
            context=context,
            chat_id=chat_id,
            user_id=user_id,
            username=username,
            text=AI_PROCESSING_NOTICE,
        )

    async def _persist_location_selections(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        user_id: int,
        username: str,
    ) -> None:
        selections = self._pending_location_selection_values(user_id)
        saved_location = "; ".join(selections)
        country = ""
        if len(selections) == 1:
            remote_country = self._remote_within_country_location_country(selections[0])
            onsite_country = self._onsite_hybrid_within_country_location_country(selections[0])
            country = remote_country or onsite_country
        self.store.set_location_preference(
            user_id,
            saved_location,
            country=country,
            state="",
            city="",
            confidence=1.0 if selections else 0.0,
        )
        self._log_filter_update(user_id, username, "location", saved_location)
        self.pending_location_candidates.pop(user_id, None)
        self.pending_location_modes.pop(user_id, None)
        self.pending_inputs.pop(user_id, None)
        self.pending_location_selections.pop(user_id, None)
        if self.flow_origin.get(user_id) == FLOW_CREATE:
            await self._show_project_preferences_prompt(context, chat_id, user_id, username)
        else:
            await self._send_edit_saved(context, chat_id, user_id, username)

    async def _show_location_prompt(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        user_id: int,
        username: str,
    ) -> None:
        self.flow_step[user_id] = STEP_LOCATION
        current_location = self._location_selection_summary(user_id)
        heading = "📍 Step 2 of 6" if self.flow_origin.get(user_id) == FLOW_CREATE else "📍 Edit Location"
        await self._send_and_log(
            context=context,
            chat_id=chat_id,
            user_id=user_id,
            username=username,
            text=(
                f"{heading}\n"
                "Choose one or more location filters.\n"
                f"🌐 {REMOTE_GLOBAL_UI_HELP}\n"
                f"🌍 {REMOTE_WITHIN_COUNTRY_UI_HELP}\n"
                "🏢 Pick On-site/hybrid within a country to receive non-remote or hybrid contracts limited to one country you specify.\n"
                "✍️ Type Custom Location if you want a specific city, state, country, or multiple locations separated with ; or &.\n"
                "Tap Save Selections when you're done.\n"
                f"Current selections:\n{current_location}"
            ),
            reply_markup=self._location_prompt_markup(user_id),
        )

    async def _save_location_choice(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        user_id: int,
        username: str,
        callback_data: str,
    ) -> None:
        label = LOCATION_OPTION_LABELS[callback_data]
        self._add_pending_location_selection(user_id, label)
        self.pending_location_candidates.pop(user_id, None)
        self.pending_location_modes.pop(user_id, None)
        self.pending_inputs.pop(user_id, None)
        await self._show_location_prompt(context, chat_id, user_id, username)

    async def _confirm_custom_location(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        user_id: int,
        username: str,
    ) -> None:
        candidate = self.pending_location_candidates.pop(user_id, None)
        location_mode = self.pending_location_modes.pop(user_id, LOCATION_MODE_CUSTOM)
        if candidate is None:
            self.pending_location_modes[user_id] = location_mode
            self.pending_inputs[user_id] = WAITING_LOCATION_INPUT
            await self._send_and_log(
                context=context,
                chat_id=chat_id,
                user_id=user_id,
                username=username,
                text=(
                    "🌍 Send the country again so I can save your remote-country filter."
                    if location_mode == LOCATION_MODE_REMOTE_COUNTRY
                    else "🏢 Send the country again so I can save your on-site/hybrid country filter."
                    if location_mode == LOCATION_MODE_ONSITE_HYBRID_COUNTRY
                    else "📍 Send your custom location so I can save it.\nInclude the country and state if you know them."
                ),
                reply_markup=self._location_text_input_markup(user_id),
            )
            return
        if location_mode == LOCATION_MODE_REMOTE_COUNTRY:
            country = (candidate.country or candidate.canonical or candidate.raw_input).strip()
            saved_location = self._remote_within_country_location(country)
            self._add_pending_location_selection(user_id, saved_location)
        elif location_mode == LOCATION_MODE_ONSITE_HYBRID_COUNTRY:
            country = (candidate.country or candidate.canonical or candidate.raw_input).strip()
            saved_location = self._onsite_hybrid_within_country_location(country)
            self._add_pending_location_selection(user_id, saved_location)
        else:
            canonical = candidate.canonical or candidate.raw_input
            self._add_pending_location_selection(user_id, canonical)
        self.pending_inputs.pop(user_id, None)
        await self._show_location_prompt(context, chat_id, user_id, username)

    async def _show_project_preferences_prompt(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        user_id: int,
        username: str,
    ) -> None:
        self.flow_step[user_id] = STEP_PROJECT_FILTERS
        heading = (
            "🧩 Step 3 of 6"
            if self.flow_origin.get(user_id) == FLOW_CREATE
            else "🧩 Edit Project Preferences"
        )
        await self._send_and_log(
            context=context,
            chat_id=chat_id,
            user_id=user_id,
            username=username,
            text=(
                f"{heading}\n"
                "Tell me what kind of freelance opportunities you want.\n"
                "Keep this focused on the 3 critical project filters: deliverables, minimum fixed budget, and minimum hourly rate.\n\n"
                f"{self._project_preferences_summary_text(user_id)}"
            ),
            reply_markup=self._project_preferences_prompt_markup(user_id),
        )

    async def _save_project_budget_visibility_choice(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        user_id: int,
        username: str,
    ) -> None:
        await self._show_project_preferences_prompt(context, chat_id, user_id, username)

    async def _show_salary_prompt(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        user_id: int,
        username: str,
    ) -> None:
        await self._show_project_preferences_prompt(context, chat_id, user_id, username)

    async def _save_salary_choice(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        user_id: int,
        username: str,
        *,
        include_no_salary: bool,
    ) -> None:
        del include_no_salary
        await self._show_project_preferences_prompt(context, chat_id, user_id, username)

    async def _handle_project_preferences_text(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        user_id: int,
        username: str,
        text: str,
    ) -> None:
        parsed_preferences = self._parse_project_preferences_text(text, user_id)
        self.store.set_project_preferences(user_id, parsed_preferences)
        self.pending_inputs.pop(user_id, None)
        self._log_filter_update(
            user_id,
            username,
            "project_preferences",
            self._project_preferences_log_text(user_id),
        )
        if self.flow_origin.get(user_id) == FLOW_CREATE:
            await self._show_website_picker(context, chat_id, user_id, username)
        else:
            await self._send_edit_saved(context, chat_id, user_id, username)

    async def _show_website_picker(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        user_id: int,
        username: str,
        edit_query: CallbackQuery | None = None,
    ) -> None:
        self.flow_step[user_id] = STEP_WEBSITES
        role_title = self.store.get_role_preference(user_id)
        selected = self.pending_website_choices.get(user_id, set(self.store.get_user_websites(user_id)))
        remapped = set(self._remap_default_source_urls(selected, role_title))
        self.pending_website_choices[user_id] = remapped
        options = self._available_website_choices(user_id)
        self.website_picker_cache[user_id] = options
        heading = "🌐 Step 4 of 6" if self.flow_origin.get(user_id) == FLOW_CREATE else "🌐 Edit Sources"
        await self._send_or_edit_and_log(
            context=context,
            chat_id=chat_id,
            user_id=user_id,
            username=username,
            text=(
                f"{heading}\n"
                "Choose the opportunity sources you want us to monitor.\n"
                "Freelance marketplaces and project boards are the main feed. Mixed boards are optional extras.\n"
                f"Selected sources:\n{self._selected_sources_text(remapped)}"
            ),
            reply_markup=self._website_picker_markup(user_id),
            edit_query=edit_query,
        )

    async def _toggle_website_choice(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        user_id: int,
        username: str,
        callback_data: str,
        *,
        edit_query: CallbackQuery | None,
    ) -> None:
        website_url = self._extract_website_from_picker_callback(user_id, callback_data)
        if website_url is not None:
            selected = self.pending_website_choices.setdefault(user_id, set(self.store.get_user_websites(user_id)))
            if website_url in selected:
                selected.remove(website_url)
            else:
                selected.add(website_url)
        await self._show_website_picker(context, chat_id, user_id, username, edit_query=edit_query)

    async def _select_all_website_choices(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        user_id: int,
        username: str,
        *,
        edit_query: CallbackQuery | None,
    ) -> None:
        options = self.website_picker_cache.get(user_id, self._available_website_choices(user_id))
        selected = self.pending_website_choices.setdefault(user_id, set(self.store.get_user_websites(user_id)))
        option_set = set(options)
        if option_set and option_set.issubset(selected):
            selected.difference_update(option_set)
        else:
            selected.update(option_set)
        await self._show_website_picker(context, chat_id, user_id, username, edit_query=edit_query)

    async def _save_websites_and_continue(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        user_id: int,
        username: str,
    ) -> None:
        selected = {
            url
            for url in self.pending_website_choices.get(user_id, set())
            if self._normalize_website_url(url) is not None
        }
        if not selected:
            await self._send_and_log(
                context=context,
                chat_id=chat_id,
                user_id=user_id,
                username=username,
                text="🌐 Choose at least one source before continuing.",
                reply_markup=self._website_picker_markup(user_id),
            )
            return
        self._persist_selected_websites(user_id, username, selected)
        if self.flow_origin.get(user_id) == FLOW_CREATE:
            await self._show_keywords_prompt(context, chat_id, user_id, username)
        else:
            await self._send_edit_saved(context, chat_id, user_id, username)

    async def _show_keywords_prompt(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        user_id: int,
        username: str,
    ) -> None:
        self.flow_step[user_id] = STEP_KEYWORDS
        current_keywords = self.store.get_user_keywords(user_id)
        current_text = self._keywords_preview(current_keywords)
        heading = "🏷️ Step 5 of 6" if self.flow_origin.get(user_id) == FLOW_CREATE else "🏷️ Edit Project Keywords"
        await self._send_and_log(
            context=context,
            chat_id=chat_id,
            user_id=user_id,
            username=username,
            text=(
                f"{heading}\n"
                "Add project-scope keywords to improve relevance.\n"
                "Use skills, deliverables, industries, tools, or project types you want tracked.\n"
                "Examples: landing page, dashboard, mobile app redesign, SaaS audit, Webflow build, Figma cleanup, ad creatives\n"
                f"Current: {current_text}"
            ),
            reply_markup=self._keywords_prompt_markup(user_id),
        )

    async def _show_alert_summary(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        user_id: int,
        username: str,
    ) -> None:
        self.flow_step[user_id] = STEP_SUMMARY
        await self._send_and_log(
            context=context,
            chat_id=chat_id,
            user_id=user_id,
            username=username,
            text=self._alert_summary_text(user_id),
            reply_markup=self._alert_summary_markup(),
        )

    async def _show_my_alert(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        user_id: int,
        username: str,
    ) -> None:
        self._clear_user_flow_state(user_id)
        if not self.store.has_alert_configuration(user_id):
            await self._send_and_log(
                context=context,
                chat_id=chat_id,
                user_id=user_id,
                username=username,
                text="✨ You do not have an alert yet.",
                reply_markup=self._empty_alert_markup(),
            )
            return
        await self._send_and_log(
            context=context,
            chat_id=chat_id,
            user_id=user_id,
            username=username,
            text=self._my_alert_text(user_id),
            reply_markup=self._my_alert_markup(user_id),
        )

    async def _show_edit_alert_menu(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        user_id: int,
        username: str,
    ) -> None:
        await self._send_and_log(
                context=context,
                chat_id=chat_id,
                user_id=user_id,
                username=username,
                text="✏️ Choose what you want to edit.",
                reply_markup=self._edit_alert_menu_markup(),
            )

    def _manual_review_admin_chat_id(self) -> int:
        return 100000001

    def _is_expired_callback_error(self, exc: BadRequest) -> bool:
        message = str(exc).strip().lower()
        return "query is too old" in message or "query id is invalid" in message

    async def _safe_query_answer(
        self,
        query: CallbackQuery,
        text: str | None = None,
        *,
        show_alert: bool = False,
    ) -> None:
        try:
            if text is None:
                await query.answer()
            else:
                await query.answer(text, show_alert=show_alert)
        except BadRequest as exc:
            if not self._is_expired_callback_error(exc):
                raise
            self.logger.info("[telegram-menu] skipped callback answer because the query had already expired")

    @staticmethod
    def _manual_review_markup(review_id: int) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "Force Send",
                        callback_data=f"{CALLBACK_MANUAL_REVIEW_FORCE_SEND_PREFIX}{review_id}",
                    ),
                    InlineKeyboardButton(
                        "Keep Rejecting",
                        callback_data=f"{CALLBACK_MANUAL_REVIEW_KEEP_REJECTING_PREFIX}{review_id}",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "Resend to AI to Double Check",
                        callback_data=f"{CALLBACK_MANUAL_REVIEW_RECHECK_PREFIX}{review_id}",
                    ),
                ]
            ]
        )

    @staticmethod
    def _card_from_manual_review_case(case) -> JobCard:
        payload: dict[str, object] = {}
        with contextlib.suppress(json.JSONDecodeError, TypeError, ValueError):
            payload = json.loads(str(getattr(case, "card_json", "") or "{}"))
        opportunity_kind = str(payload.get("opportunity_kind") or "").strip()
        is_job_post = bool(payload.get("is_job_post", opportunity_kind == "job_post"))
        return JobCard(
            website=str(payload.get("website") or getattr(case, "card_website", "")),
            url=str(payload.get("url") or getattr(case, "card_url", "")),
            title=str(payload.get("title") or getattr(case, "card_title", "")),
            description=str(payload.get("description") or ""),
            salary=str(payload.get("salary") or getattr(case, "card_salary", "")),
            location=str(payload.get("location") or getattr(case, "card_location", "")),
            is_job_post=is_job_post,
            confidence=float(payload.get("confidence") or 0.0),
            extraction_method=str(payload.get("extraction_method") or "manual-review"),
            extracted_at_utc=str(payload.get("extracted_at_utc") or utc_now_iso()),
            posted_at_utc=str(payload.get("posted_at_utc") or ""),
            notes=str(payload.get("notes") or ""),
            company=str(payload.get("company") or getattr(case, "card_company", "")),
            language=str(payload.get("language") or getattr(case, "card_language", "en") or "en"),
            is_relevant_opportunity=bool(payload.get("is_relevant_opportunity", is_job_post)),
            opportunity_kind=opportunity_kind,
            client=str(payload.get("client") or payload.get("company") or getattr(case, "card_company", "")),
            requester=str(payload.get("requester") or payload.get("client") or payload.get("company") or getattr(case, "card_company", "")),
            scope_summary=str(payload.get("scope_summary") or payload.get("description") or ""),
            budget=str(payload.get("budget") or payload.get("salary") or getattr(case, "card_salary", "")),
            duration=str(payload.get("duration") or ""),
            commitment_level=str(payload.get("commitment_level") or ""),
            skills_required=str(payload.get("skills_required") or ""),
            proposal_deadline=str(payload.get("proposal_deadline") or ""),
            start_timeline=str(payload.get("start_timeline") or ""),
            industry=str(payload.get("industry") or ""),
            engagement_type=str(payload.get("engagement_type") or ""),
            remote_location_constraint=str(payload.get("remote_location_constraint") or payload.get("location") or getattr(case, "card_location", "")),
            contact_url=str(payload.get("contact_url") or payload.get("url") or getattr(case, "card_url", "")),
        )

    @staticmethod
    def _manual_review_context(case) -> dict[str, object]:
        context_payload: dict[str, object] = {}
        with contextlib.suppress(json.JSONDecodeError, TypeError, ValueError):
            context_payload = json.loads(str(getattr(case, "context_json", "") or "{}"))
        return context_payload

    @staticmethod
    def _review_plain_text(value: object, *, fallback: str = "Unknown") -> str:
        return SubscriberNotifier._clean_message_text(str(value or ""), default=fallback)

    @staticmethod
    def _manual_review_list_block(items: list[str], *, fallback: str) -> str:
        cleaned = [
            TelegramMenuBot._review_plain_text(item, fallback="")
            for item in items
            if TelegramMenuBot._review_plain_text(item, fallback="").strip()
        ]
        if not cleaned:
            return f"- {TelegramMenuBot._review_plain_text(fallback)}"
        return "\n".join(f"- {item}" for item in cleaned[:6])

    @staticmethod
    def _clip_review_segment(value: object, *, max_chars: int) -> str:
        text = str(value or "").strip()
        if max_chars <= 0 or len(text) <= max_chars:
            return text
        return f"{text[: max_chars - 1].rstrip()}…"

    @classmethod
    def _fit_review_message_for_telegram(
        cls,
        text: str,
        *,
        max_chars: int = 3900,
        max_line_chars: int = 520,
    ) -> str:
        lines = [
            cls._clip_review_segment(line.rstrip(), max_chars=max_line_chars)
            for line in str(text or "").splitlines()
        ]
        candidate = "\n".join(lines).strip()
        if len(candidate) <= max_chars:
            return candidate
        suffix = "\n\n[Shortened for Telegram]"
        available = max(1, max_chars - len(suffix))
        kept: list[str] = []
        current_length = 0
        for line in lines:
            addition_length = len(line) if not kept else len(line) + 1
            if current_length + addition_length > available:
                break
            kept.append(line)
            current_length += addition_length
        shortened = "\n".join(kept).rstrip() if kept else cls._clip_review_segment(candidate, max_chars=available)
        return f"{shortened}{suffix}"

    @staticmethod
    def _manual_review_project_preferences(context_payload: dict[str, object]) -> list[str]:
        return shared_project_preferences_display_lines(
            context_payload.get("project_preferences") or {},
            language="en",
            fields=FULL_PROJECT_FILTER_FIELDS,
            limit=6,
        )

    @staticmethod
    def _manual_review_text(case) -> str:
        context_payload = TelegramMenuBot._manual_review_context(case)
        card = TelegramMenuBot._card_from_manual_review_case(case)
        username = TelegramMenuBot._review_plain_text(getattr(case, "username", ""), fallback="").strip()
        username_text = f"@{username}" if username and not username.startswith("@") else (username or "(none)")
        keywords_text = TelegramMenuBot._review_plain_text(getattr(case, "keywords_text", ""), fallback="").strip()
        salary_pref = TelegramMenuBot._review_plain_text(getattr(case, "salary_preference", ""), fallback="").strip()
        if not salary_pref:
            salary_pref = (
                "budget / payment optional"
                if bool(getattr(case, "include_no_salary", True))
                else "budget / payment required"
            )
        confidence_value = 0.0
        with contextlib.suppress(TypeError, ValueError):
            confidence_value = float(context_payload.get("card_confidence") or 0.0)
        confidence_text = f"{confidence_value:.2f}"
        matches = [
            TelegramMenuBot._review_plain_text(item, fallback="")
            for item in (context_payload.get("matches") or [])
            if TelegramMenuBot._review_plain_text(item, fallback="").strip()
        ]
        mismatches = [
            TelegramMenuBot._review_plain_text(item, fallback="")
            for item in (context_payload.get("mismatches") or [])
            if TelegramMenuBot._review_plain_text(item, fallback="").strip()
        ]
        ambiguity_reasons = [
            TelegramMenuBot._review_plain_text(reason, fallback="")
            for reason in (context_payload.get("ambiguity_reasons") or [])
            if TelegramMenuBot._review_plain_text(reason, fallback="").strip()
        ]
        if ambiguity_reasons:
            mismatches = ambiguity_reasons + mismatches
        project_preferences = TelegramMenuBot._manual_review_project_preferences(context_payload)
        preferences_lines = [
            f"Primary specialty: {TelegramMenuBot._review_plain_text(getattr(case, 'role_title', ''), fallback='not set')}",
            f"Location: {TelegramMenuBot._review_plain_text(getattr(case, 'location_preference', ''), fallback='not set')}",
            f"Budget / payment: {TelegramMenuBot._review_plain_text(salary_pref, fallback='not set')}",
            f"Keywords: {TelegramMenuBot._review_plain_text(keywords_text or 'none', fallback='none')}",
        ]
        preferences_lines.extend(project_preferences[:4])
        source_reason = TelegramMenuBot._review_plain_text(getattr(case, "ambiguity_summary", ""), fallback="").strip()
        if source_reason and all(source_reason != item for item in mismatches):
            mismatches.insert(0, source_reason)
        admin_reason = (
            TelegramMenuBot._review_plain_text(context_payload.get("decision_reason") or "", fallback="").strip()
            or TelegramMenuBot._review_plain_text(context_payload.get("admin_reason_summary") or "", fallback="").strip()
            or TelegramMenuBot._review_plain_text(getattr(case, "match_reason", "") or "", fallback="").strip()
            or "not available"
        )
        return (
            "Project Review Needed\n"
            f"Link: {TelegramMenuBot._review_plain_text(getattr(case, 'card_url', ''), fallback='Unknown')}\n"
            f"User: {getattr(case, 'user_id', 0)} | {username_text}\n"
            f"Post age: {TelegramMenuBot._review_plain_text(context_payload.get('post_age') or 'Unknown')}\n"
            f"Confidence score: {confidence_text}\n\n"
            "Alert preferences:\n"
            f"{TelegramMenuBot._manual_review_list_block(preferences_lines, fallback='No saved preferences found.')}\n\n"
            "Project:\n"
            f"- Project: {TelegramMenuBot._review_plain_text(card.title, fallback='Unknown')}\n"
            f"- Client: {TelegramMenuBot._review_plain_text(card.counterparty, fallback='Unknown')}\n"
            f"- Scope: {TelegramMenuBot._review_plain_text(card.scope_summary or card.description, fallback='Unknown')}\n"
            f"- Budget / rate: {TelegramMenuBot._review_plain_text(card.payment_terms, fallback='Unknown')}\n"
            f"- Engagement type: {TelegramMenuBot._review_plain_text(card.engagement_type, fallback='Unknown')}\n"
            f"- Timeline / duration: {TelegramMenuBot._review_plain_text(' / '.join(part for part in (card.start_timeline, card.duration) if part), fallback='Unknown')}\n"
            f"- Skills requested: {TelegramMenuBot._review_plain_text(card.skills_required, fallback='Unknown')}\n"
            f"- Remote / location: {TelegramMenuBot._review_plain_text(card.opportunity_location, fallback='Unknown')}\n"
            f"- Proposal / contact URL: {TelegramMenuBot._review_plain_text(card.proposal_or_contact_url, fallback='Unknown')}\n"
            f"- Source: {TelegramMenuBot._review_plain_text(card.website, fallback='Unknown')}\n"
            f"- Language: {TelegramMenuBot._review_plain_text(card.language or 'en', fallback='en')}\n\n"
            "Matches:\n"
            f"{TelegramMenuBot._manual_review_list_block(matches, fallback='No strong match signals were recorded.')}\n\n"
            "Mismatches:\n"
            f"{TelegramMenuBot._manual_review_list_block(mismatches, fallback='No explicit mismatch reason was recorded.')}\n\n"
            f"Admin rationale: {TelegramMenuBot._review_plain_text(admin_reason, fallback='not available')}"
        )

    @staticmethod
    def _human_review_markup(item_id: int, job_url: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("Open Project", url=job_url)],
                [
                    InlineKeyboardButton(
                        "Approve & Send",
                        callback_data=f"{CALLBACK_HUMAN_REVIEW_SEND_PREFIX}{item_id}",
                    ),
                    InlineKeyboardButton(
                        "Neglect",
                        callback_data=f"{CALLBACK_HUMAN_REVIEW_NEGLECT_PREFIX}{item_id}",
                    )
                ],
            ]
        )

    async def _send_pending_manual_reviews(self, limit: int = 20) -> None:
        loader = getattr(self.store, "list_manual_review_cases", None)
        marker = getattr(self.store, "mark_manual_review_dispatched", None)
        error_marker = getattr(self.store, "mark_manual_review_dispatch_error", None)
        if not callable(loader) or not callable(marker):
            return
        pending_cases = loader(status="pending", only_undispatched=True, limit=limit)
        if not pending_cases:
            return
        admin_chat_id = self._manual_review_admin_chat_id()
        for case in pending_cases:
            card = self._card_from_manual_review_case(case)
            if float(getattr(card, "confidence", 0.0) or 0.0) <= 0.0:
                if callable(error_marker):
                    error_marker(int(getattr(case, "review_id", 0)), error="Skipped decision review send because confidence is 0.")
                self.logger.info(
                    "[telegram-menu] suppressed manual review Telegram send review_id=%s because confidence is 0",
                    getattr(case, "review_id", "unknown"),
                )
                continue
            try:
                review_text = self._fit_review_message_for_telegram(self._manual_review_text(case))
                message = await self.application.bot.send_message(
                    chat_id=admin_chat_id,
                    text=review_text,
                    reply_markup=self._manual_review_markup(int(getattr(case, "review_id", 0))),
                    disable_web_page_preview=True,
                )
                marker(
                    int(getattr(case, "review_id", 0)),
                    admin_chat_id=admin_chat_id,
                    admin_message_id=int(getattr(message, "message_id", 0) or 0),
                )
            except Exception as exc:  # noqa: BLE001
                if callable(error_marker):
                    error_marker(int(getattr(case, "review_id", 0)), error=str(exc))
                self.logger.warning(
                    "[telegram-menu] failed to send manual review review_id=%s: %s",
                    getattr(case, "review_id", "unknown"),
                    str(exc),
                )

    async def _send_pending_human_reviews(self, limit: int = 20) -> None:
        loader = getattr(self.state_store, "list_human_review_items", None)
        marker = getattr(self.state_store, "mark_human_review_dispatched", None)
        error_marker = getattr(self.state_store, "mark_human_review_dispatch_error", None)
        confidence_updater = getattr(self.state_store, "update_human_review_confidence", None)
        getter = getattr(self.state_store, "get_human_review_item", None)
        if not callable(loader) or not callable(marker):
            return
        pending_items = loader(status="pending", only_undispatched=True, limit=limit)
        if not pending_items:
            return
        admin_chat_id = self._manual_review_admin_chat_id()
        for item in pending_items:
            card = job_card_from_review_item(item)
            if callable(confidence_updater) and abs(card.confidence - float(getattr(item, "confidence", 0.0) or 0.0)) > 0.0005:
                confidence_updater(int(getattr(item, "id", 0)), card.confidence)
                if callable(getter):
                    refreshed = getter(int(getattr(item, "id", 0)))
                    if refreshed is not None:
                        item = refreshed
                        card = job_card_from_review_item(item)
            if card.confidence <= 0.0:
                if callable(error_marker):
                    error_marker(int(getattr(item, "id", 0)), error="Skipped Telegram review send because confidence remained 0.")
                continue
            try:
                presentation = await build_human_review_presentation_async(
                    item,
                    self.store,
                    subscriber_notifier=self.subscriber_notifier,
                )
                review_text = self._fit_review_message_for_telegram(format_human_review_text(item, presentation))
                message = await self.application.bot.send_message(
                    chat_id=admin_chat_id,
                    text=review_text,
                    reply_markup=self._human_review_markup(int(getattr(item, "id", 0)), card.url),
                    disable_web_page_preview=True,
                )
                marker(
                    int(getattr(item, "id", 0)),
                    admin_chat_id=admin_chat_id,
                    admin_message_id=int(getattr(message, "message_id", 0) or 0),
                )
            except Exception as exc:  # noqa: BLE001
                if callable(error_marker):
                    error_marker(int(getattr(item, "id", 0)), error=str(exc))
                self.logger.warning(
                    "[telegram-menu] failed to send human review item_id=%s: %s",
                    getattr(item, "id", "unknown"),
                    str(exc),
                )

    async def _handle_manual_review_decision(
        self,
        *,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        reviewer_id: int,
        reviewer_username: str,
        callback_data: str,
        action: str,
        query: CallbackQuery,
    ) -> None:
        if int(reviewer_id) != int(self._manual_review_admin_chat_id()):
            await self._safe_query_answer(
                query,
                "This review action is only available in the admin review chat.",
                show_alert=True,
            )
            return
        if action == "force_send":
            prefix = CALLBACK_MANUAL_REVIEW_FORCE_SEND_PREFIX
        elif action == "keep_rejecting":
            prefix = CALLBACK_MANUAL_REVIEW_KEEP_REJECTING_PREFIX
        else:
            prefix = CALLBACK_MANUAL_REVIEW_RECHECK_PREFIX
        raw_id = callback_data[len(prefix) :].strip()
        if not raw_id.isdigit():
            await self._safe_query_answer(query, "Invalid review action.", show_alert=True)
            return
        review_id = int(raw_id)
        getter = getattr(self.store, "get_manual_review_case", None)
        setter = getattr(self.store, "set_manual_review_decision", None)
        if not callable(getter) or not callable(setter):
            await self._safe_query_answer(query, "Manual review storage is not available.", show_alert=True)
            return
        case = getter(review_id)
        if case is None:
            await self._safe_query_answer(query, "This review item no longer exists.", show_alert=True)
            return
        if str(getattr(case, "status", "")).strip().lower() != "pending":
            await self._safe_query_answer(query, "This review item has already been handled.", show_alert=True)
            with contextlib.suppress(Exception):
                await query.edit_message_reply_markup(reply_markup=None)
            return

        if action == "force_send":
            card = self._card_from_manual_review_case(case)
            delivery_result, sent_count = await self.subscriber_notifier.deliver_manual_review_case(
                user_id=int(getattr(case, "user_id", 0)),
                username=str(getattr(case, "username", "") or ""),
                card=card,
                match_reason=str(getattr(case, "match_reason", "") or ""),
                force_send=True,
            )
            approved_sent = delivery_result in {"sent", "queued_digest", "queued_window", "queued_after_send_failure"}
            setter(
                review_id,
                decision="approved_sent" if approved_sent else "approved_no_send",
                reviewer=reviewer_username,
                notes=f"force_send:{delivery_result}: {str(getattr(case, 'ambiguity_summary', '') or '').strip()}",
                sent_count=sent_count,
            )
            confirmation = (
                f"Force send finished: {delivery_result.replace('_', ' ')}."
                if approved_sent
                else f"Force send could not complete delivery because of: {delivery_result.replace('_', ' ')}."
            )
            with contextlib.suppress(Exception):
                await query.edit_message_reply_markup(reply_markup=None)
            await self._safe_query_answer(query, confirmation)
            with contextlib.suppress(Exception):
                await context.bot.send_message(chat_id=chat_id, text=confirmation, disable_web_page_preview=True)
            return

        if action == "keep_rejecting":
            setter(
                review_id,
                decision="neglected",
                reviewer=reviewer_username,
                notes=f"kept_rejecting: {str(getattr(case, 'ambiguity_summary', '') or '').strip()}",
                sent_count=0,
            )
            confirmation = "Rejected and kept out of delivery."
            with contextlib.suppress(Exception):
                await query.edit_message_reply_markup(reply_markup=None)
            await self._safe_query_answer(query, confirmation)
            with contextlib.suppress(Exception):
                await context.bot.send_message(chat_id=chat_id, text=confirmation, disable_web_page_preview=True)
            return

        card = self._card_from_manual_review_case(case)
        refreshed = await self.subscriber_notifier.reevaluate_manual_review_case(
            user_id=int(getattr(case, "user_id", 0)),
            card=card,
        )
        queued = self.subscriber_notifier._queue_manual_review_case(  # noqa: SLF001
            user_id=int(getattr(case, "user_id", 0)),
            username=str(getattr(case, "username", "") or ""),
            card=card,
            evaluation=refreshed,
        )
        updated_case = getter(review_id) if queued else case
        confirmation = (
            "AI double-check completed. The review card was refreshed."
            if queued
            else "AI double-check ran, but the stored review card could not be refreshed."
        )
        with contextlib.suppress(Exception):
            await query.edit_message_text(
                text=self._manual_review_text(updated_case),
                reply_markup=self._manual_review_markup(review_id),
                disable_web_page_preview=True,
            )
        await self._safe_query_answer(query, confirmation)

    async def _handle_human_review_decision(
        self,
        *,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        reviewer_id: int,
        reviewer_username: str,
        callback_data: str,
        action: str,
        query: CallbackQuery,
    ) -> None:
        if int(reviewer_id) != int(self._manual_review_admin_chat_id()):
            await self._safe_query_answer(
                query,
                "This review action is only available in the admin review chat.",
                show_alert=True,
            )
            return
        if action == "approve_send":
            prefix = CALLBACK_HUMAN_REVIEW_SEND_PREFIX
        elif action == "approve_no_send":
            prefix = CALLBACK_HUMAN_REVIEW_SAVE_PREFIX
        else:
            prefix = CALLBACK_HUMAN_REVIEW_NEGLECT_PREFIX
        raw_id = callback_data[len(prefix) :].strip()
        if not raw_id.isdigit():
            await self._safe_query_answer(query, "Invalid review action.", show_alert=True)
            return
        item_id = int(raw_id)
        getter = getattr(self.state_store, "get_human_review_item", None)
        error_marker = getattr(self.state_store, "mark_human_review_dispatch_error", None)
        if not callable(getter):
            await self._safe_query_answer(query, "Human review storage is not available.", show_alert=True)
            return
        item = getter(item_id)
        if item is None:
            await self._safe_query_answer(query, "This review item no longer exists.", show_alert=True)
            return
        if str(getattr(item, "status", "")).strip().lower() != "pending":
            await self._safe_query_answer(query, "This review item has already been handled.", show_alert=True)
            with contextlib.suppress(Exception):
                await query.edit_message_reply_markup(reply_markup=None)
            return

        reviewer_label = reviewer_username or str(reviewer_id)
        if action == "approve_send":
            presentation = await build_human_review_presentation_async(
                item,
                self.store,
                subscriber_notifier=self.subscriber_notifier,
            )
            if presentation.target_recipient is None:
                if callable(error_marker):
                    error_marker(item_id, error=HUMAN_REVIEW_SUPPRESSED_NO_MATCH)
                await self._safe_query_answer(
                    query,
                    "No matching active subscriber is eligible for this post right now.",
                    show_alert=True,
                )
                return
            result = await approve_review_item(
                item,
                state_store=self.state_store,
                subs_store=self.store,
                bot_token=self.settings.telegram_bot_token,
                reviewer=reviewer_label,
                notes="Approved from Telegram admin review card.",
                logger=self.logger,
                send_to_subscribers=True,
                subscriber_notifier=self.subscriber_notifier,
                openai_api_key=self.settings.openai_api_key,
                openai_model=self.settings.openai_model_filter_match or self.settings.openai_model,
                keyword_model=self.settings.openai_model_keyword_expansion,
                final_match_model=self.settings.openai_model_filter_match,
            )
        elif action == "approve_no_send":
            result = await approve_review_item(
                item,
                state_store=self.state_store,
                subs_store=self.store,
                bot_token=self.settings.telegram_bot_token,
                reviewer=reviewer_label,
                notes="Approved without subscriber send from Telegram admin review card.",
                logger=self.logger,
                send_to_subscribers=False,
                subscriber_notifier=self.subscriber_notifier,
                openai_api_key=self.settings.openai_api_key,
                openai_model=self.settings.openai_model_filter_match or self.settings.openai_model,
                keyword_model=self.settings.openai_model_keyword_expansion,
                final_match_model=self.settings.openai_model_filter_match,
            )
        else:
            result = neglect_review_item(
                item,
                state_store=self.state_store,
                reviewer=reviewer_label,
                notes="Neglected from Telegram admin review card.",
            )

        if not result.success:
            await self._safe_query_answer(query, result.message, show_alert=True)
            return

        with contextlib.suppress(Exception):
            await query.edit_message_reply_markup(reply_markup=None)
        await self._safe_query_answer(query, result.message)
        with contextlib.suppress(Exception):
            await context.bot.send_message(chat_id=chat_id, text=result.message, disable_web_page_preview=True)

    async def _send_no_posts_reports(self) -> None:
        report_minutes = max(0, int(self.settings.no_posts_report_minutes))
        if report_minutes < 1:
            return

        now_utc = utc_now_dt()
        for user_id, username in self.store.get_active_subscribers():
            if not self.store.has_alert_configuration(user_id):
                continue
            if not self.store.get_notification_preference(user_id):
                continue
            report = await self._build_alert_health_report(
                user_id,
                now_utc=now_utc,
                enforce_threshold=True,
            )
            if report is None:
                continue
            try:
                await self._send_to_user_and_log(
                    user_id=user_id,
                    username=username,
                    text=report.text,
                    reply_markup=self._alert_health_markup(user_id),
                    disable_web_page_preview=True,
                )
                self.store.mark_inactivity_report_sent(user_id, anchor_utc=report.anchor_utc)
            except Exception as exc:  # noqa: BLE001
                self.logger.warning(
                    "[telegram-menu] failed to send no-posts report user=%s: %s",
                    user_id,
                    str(exc),
                )

    async def _show_alert_health_report(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        user_id: int,
        username: str,
        *,
        mark_as_sent: bool,
    ) -> None:
        report = await self._build_alert_health_report(
            user_id,
            now_utc=utc_now_dt(),
            enforce_threshold=False,
        )
        if report is None:
            await self._send_and_log(
                context=context,
                chat_id=chat_id,
                user_id=user_id,
                username=username,
                text=self._t(user_id, "alert_health_missing"),
                reply_markup=self._my_alert_markup(user_id),
            )
            return
        await self._send_and_log(
            context=context,
            chat_id=chat_id,
            user_id=user_id,
            username=username,
            text=report.text,
            reply_markup=self._alert_health_markup(user_id),
            disable_web_page_preview=True,
        )
        if mark_as_sent:
            self.store.mark_inactivity_report_sent(user_id, anchor_utc=report.anchor_utc)

    async def _build_alert_health_report(
        self,
        user_id: int,
        *,
        now_utc: datetime,
        enforce_threshold: bool,
    ) -> AlertHealthReport | None:
        if not self.store.has_alert_configuration(user_id):
            return None

        selected_websites = self.store.get_user_websites(user_id)
        anchor_utc = self._current_alert_activity_anchor_utc(user_id)
        anchor_dt = self._parse_utc_iso(anchor_utc)
        if anchor_dt is None:
            anchor_dt = now_utc
            anchor_utc = format_utc_iso(anchor_dt)

        inactivity_minutes = max(1, int((now_utc - anchor_dt).total_seconds() // 60))
        report_minutes = max(0, int(self.settings.no_posts_report_minutes))
        if enforce_threshold and inactivity_minutes < max(1, report_minutes):
            return None
        if enforce_threshold and self.store.get_last_inactivity_report_anchor_utc(user_id) == anchor_utc:
            last_report_sent_dt = self._parse_utc_iso(self.store.get_last_inactivity_report_sent_at_utc(user_id))
            if last_report_sent_dt is not None:
                minutes_since_last_report = max(0, int((now_utc - last_report_sent_dt).total_seconds() // 60))
                if minutes_since_last_report < max(1, report_minutes):
                    return None

        health = self.state_store.summarize_alert_health(selected_websites, since_utc=anchor_utc)
        mismatch_count = self.store.count_filter_mismatch_events(user_id, since_utc=anchor_utc)
        mismatch_stages = self.store.get_filter_mismatch_stage_counts(user_id, since_utc=anchor_utc)
        sent_since_anchor = self.store.count_delivery_events(user_id, since_utc=anchor_utc)
        sent_total = self.store.count_delivery_events(user_id)
        mismatch_total = self.store.count_filter_mismatch_events(user_id)

        last_scan_dt = self._parse_utc_iso(health.last_scan_utc)
        last_scan_minutes = (
            max(0, int((now_utc - last_scan_dt).total_seconds() // 60))
            if last_scan_dt is not None
            else None
        )

        explanation = await self._build_alert_health_explanation(
            user_id=user_id,
            inactivity_minutes=inactivity_minutes,
            last_scan_minutes=last_scan_minutes,
            selected_websites=selected_websites,
            health=health,
            mismatch_count=mismatch_count,
            mismatch_total=mismatch_total,
            sent_since_anchor=sent_since_anchor,
            sent_total=sent_total,
        )

        language = self._ui_language_or_default(user_id)
        last_scan_text = (
            self._localized_inline(
                language,
                f"The last scan was {last_scan_minutes} minutes ago.",
                f"Последняя проверка была {last_scan_minutes} мин. назад.",
            )
            if last_scan_minutes is not None
            else self._localized_inline(
                language,
                "No scan has been recorded yet for your selected links.",
                "Выбранные источники ещё ни разу не удалось проверить.",
            )
        )
        if selected_websites:
            visible_links = selected_websites[:10]
            links_text = "\n".join(f"{index}. {url}" for index, url in enumerate(visible_links, start=1))
            if len(selected_websites) > len(visible_links):
                links_text = (
                    f"{links_text}\n"
                    f"{self._localized_inline(language, f'... and {len(selected_websites) - len(visible_links)} more link(s).', f'... и ещё {len(selected_websites) - len(visible_links)} ссылок.')}"
                )
        else:
            links_text = self._localized_inline(language, "No selected source links yet.", "Пока нет выбранных источников.")
        mismatch_breakdown = self._filter_mismatch_breakdown_text(mismatch_stages, language=language)
        inactivity_text = self._localized_inline(
            language,
            f"It's been {inactivity_minutes} minutes since your last delivered match or alert update.",
            f"С последнего подходящего проекта или изменения поиска прошло {inactivity_minutes} мин.",
        )

        text = (
            f"{self._localized_inline(language, 'Why You Are Not Receiving Matches', 'Почему пока нет подходящих проектов')}\n"
            f"{inactivity_text}\n"
            f"{last_scan_text}\n\n"
            f"{self._localized_inline(language, 'Current report:', 'Что происходит сейчас:')}\n"
            f"• {self._localized_inline(language, 'Matches discovered', 'Найдено проектов')}: {health.discovered_posts}\n"
            f"• {self._localized_inline(language, 'Blocked sources', 'Заблокированные источники')}: {health.blocked_sources}\n"
            f"• {self._localized_inline(language, 'Rejected matches overall', 'Отклонено всего')}: {health.rejected_posts}\n"
            f"• {self._localized_inline(language, 'Duplicate prevented', 'Отсеяно как дубликаты')}: {health.duplicate_prevented}\n"
            f"• {self._localized_inline(language, 'Stale or closed matches', 'Слишком старые или уже закрытые')}: {health.freshness_rejects}\n"
            f"• {self._localized_inline(language, 'Project filter mismatches for your alert', 'Не подошло по вашим фильтрам')}: {mismatch_count}\n"
            f"• {self._localized_inline(language, 'Matches already sent to you in this gap', 'Уже отправлено за этот период')}: {sent_since_anchor}\n"
            f"• {self._localized_inline(language, 'Matches sent to you overall', 'Отправлено всего')}: {sent_total}\n"
            f"• {self._localized_inline(language, 'Top mismatch reasons', 'Основные причины отказа')}: {mismatch_breakdown}\n\n"
            f"{self._localized_inline(language, 'Sources being searched:', 'Какие источники сейчас проверяются:')}\n"
            f"{links_text}\n\n"
            f"{self._localized_inline(language, 'AI Analysis:', 'Краткое пояснение:')}\n"
            f"{explanation}"
        )
        return AlertHealthReport(anchor_utc=anchor_utc, text=text)

    async def _build_alert_health_explanation(
        self,
        *,
        user_id: int,
        inactivity_minutes: int,
        last_scan_minutes: int | None,
        selected_websites: list[str],
        health: AlertHealthSnapshot,
        mismatch_count: int,
        mismatch_total: int,
        sent_since_anchor: int,
        sent_total: int,
    ) -> str:
        payload = {
            "ui_language": self._ui_language_or_default(user_id),
            "inactivity_minutes": inactivity_minutes,
            "last_scan_minutes_ago": last_scan_minutes,
            "selected_websites": selected_websites,
            "role_title": self.store.get_role_preference(user_id),
            "location": self.store.get_location_preference(user_id),
            "include_no_salary": self.store.get_include_no_salary(user_id),
            "keywords": self.store.get_user_keywords(user_id),
            "discovered_posts": health.discovered_posts,
            "blocked_sources": health.blocked_sources,
            "blocked_source_urls": list(health.blocked_source_urls),
            "rejected_posts": health.rejected_posts,
            "duplicate_prevented": health.duplicate_prevented,
            "freshness_rejects": health.freshness_rejects,
            "user_filter_mismatches": mismatch_count,
            "user_filter_mismatches_total": mismatch_total,
            "sent_posts_in_gap": sent_since_anchor,
            "sent_posts_total": sent_total,
        }
        try:
            explanation = await self.filter_ai.explain_alert_health(payload=payload)
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("[telegram-menu] alert-health AI explanation failed user=%s: %s", user_id, str(exc))
            explanation = ""
        cleaned = " ".join(str(explanation or "").split()).strip()
        return cleaned or self._localized_inline(
            self._ui_language_or_default(user_id),
            "The alert is still active, but there have not been enough fresh matching opportunities to send yet.",
            "Поиск всё ещё активен, но пока не было достаточно свежих проектов, которые подошли бы по вашим фильтрам.",
        )

    def _current_alert_activity_anchor_utc(self, user_id: int) -> str:
        candidate_datetimes: list[datetime] = []
        for raw_value in (
            self.store.get_latest_delivery_event_at_utc(user_id),
            self.store.get_alert_updated_at_utc(user_id),
            self.store.get_latest_filter_change_at_utc(user_id),
        ):
            parsed = self._parse_utc_iso(raw_value)
            if parsed is not None:
                candidate_datetimes.append(parsed)

        active_sub = self.store.get_active_subscription(user_id)
        if active_sub is not None:
            parsed_started = self._parse_utc_iso(active_sub.started_at_utc)
            if parsed_started is not None:
                candidate_datetimes.append(parsed_started)

        if not candidate_datetimes:
            return utc_now_iso()
        return format_utc_iso(max(candidate_datetimes))

    @staticmethod
    def _parse_utc_iso(value: str) -> datetime | None:
        raw = str(value or "").strip()
        if not raw:
            return None
        try:
            return parse_utc_iso(raw)
        except ValueError:
            return None

    @staticmethod
    def _filter_mismatch_label(stage: str, language: str = "en") -> str:
        mapping = {
            "source": {"en": "source", "ru": "источник"},
            "content_type": {"en": "content type", "ru": "тип страницы"},
            "final_ai": {"en": "AI final check", "ru": "итоговая проверка ИИ"},
            "location": {"en": "location", "ru": "местоположение"},
            "role": {"en": "project scope", "ru": "суть проекта"},
            "salary": {"en": "budget visibility", "ru": "наличие бюджета"},
            "salary_visibility": {"en": "budget visibility", "ru": "наличие бюджета"},
            "keywords": {"en": "keywords", "ru": "ключевые слова"},
            "deliverables": {"en": "deliverables", "ru": "что нужно сделать"},
            "payment_model": {"en": "budget / rate", "ru": "бюджет / ставка"},
        }
        normalized = str(stage or "").strip().lower()
        labels = mapping.get(normalized, {})
        if isinstance(labels, dict):
            return str(labels.get(language) or labels.get("en") or normalized.replace("_", " "))
        fallback = normalized.replace("_", " ") or "not specified"
        return fallback if language == "en" else ("не указано" if fallback == "not specified" else fallback)

    def _filter_mismatch_breakdown_text(self, stage_counts: list[tuple[str, int]], *, language: str = "en") -> str:
        if not stage_counts:
            return "none recorded" if language == "en" else "не зафиксировано"
        parts = [
            f"{self._filter_mismatch_label(stage, language)} {count}"
            for stage, count in stage_counts[:3]
            if count > 0
        ]
        return ", ".join(parts) if parts else ("none recorded" if language == "en" else "не зафиксировано")

    async def _show_delivery_settings(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        user_id: int,
        username: str,
        edit_query: CallbackQuery | None = None,
    ) -> None:
        self.flow_origin[user_id] = FLOW_EDIT
        await self._send_or_edit_and_log(
            context=context,
            chat_id=chat_id,
            user_id=user_id,
            username=username,
            text=self._delivery_settings_text(user_id),
            reply_markup=self._delivery_settings_markup(user_id),
            edit_query=edit_query,
        )

    async def _send_edit_saved(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        user_id: int,
        username: str,
    ) -> None:
        self._clear_user_flow_state(user_id)
        await self._send_and_log(
                context=context,
                chat_id=chat_id,
                user_id=user_id,
                username=username,
                text="✅ Your alert settings have been updated.",
                reply_markup=self._my_alert_markup(user_id),
            )
        await self._show_my_alert(context, chat_id, user_id, username)

    async def _handle_match_feedback(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        user_id: int,
        username: str,
        callback_data: str,
        vote: str,
        *,
        query: CallbackQuery,
    ) -> None:
        prefix = (
            SubscriberNotifier.FEEDBACK_UP_PREFIX
            if vote == "up"
            else SubscriberNotifier.FEEDBACK_DOWN_PREFIX
        )
        event_id_text = callback_data[len(prefix) :]
        if not event_id_text.isdigit():
            await query.answer("This feedback button is no longer valid.")
            return

        event = self.store.get_delivery_event(int(event_id_text))
        if event is None or event.user_id != user_id:
            await query.answer(
                self._localized_inline(
                    self._ui_language_or_default(user_id),
                    "This match is no longer available.",
                    "Этот проект больше недоступен.",
                )
            )
            return

        self.store.save_match_feedback(event.event_id, user_id, vote)
        self.user_message_logger.append_event(
            user_id=user_id,
            username=username,
            direction="system",
            text=f"match_feedback={event.event_id}:{vote}",
            event_type="match_feedback",
            created_at_utc=utc_now_iso(),
        )
        with contextlib.suppress(BadRequest, TelegramError):
            await query.edit_message_reply_markup(
                reply_markup=SubscriberNotifier._delivery_markup(event.card_url, None)
            )
        if vote == "up":
            await query.answer(
                self._localized_inline(
                    self._ui_language_or_default(user_id),
                    "Thanks. I'll use this to improve future matches.",
                    "Спасибо, учту это в следующих рекомендациях.",
                )
            )
            return

        self.pending_match_feedback_event_ids[user_id] = event.event_id
        self.pending_inputs[user_id] = WAITING_MATCH_FEEDBACK_INPUT
        await query.answer(
            self._localized_inline(
                self._ui_language_or_default(user_id),
                "Tell me what was wrong with this match so I can avoid similar mistakes.",
                "Напишите, что именно было не так, чтобы я реже присылал похожие проекты.",
            )
        )
        await self._send_and_log(
            context=context,
            chat_id=chat_id,
            user_id=user_id,
            username=username,
                text=self._t(user_id, "feedback_prompt"),
                reply_markup=self._feedback_reason_prompt_markup(user_id),
        )

    async def _handle_match_feedback_text(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        user_id: int,
        username: str,
        text: str,
    ) -> None:
        feedback_text = " ".join(text.split())
        if not feedback_text:
            await self._send_and_log(
                context=context,
                chat_id=chat_id,
                user_id=user_id,
                username=username,
                text=self._localized_inline(
                    self._ui_language_or_default(user_id),
                    "👎 Send a short reason so I can learn from this mismatch.",
                    "👎 Напишите коротко, что было не так с этим проектом.",
                ),
                reply_markup=self._feedback_reason_prompt_markup(user_id),
            )
            return

        event_id = self.pending_match_feedback_event_ids.pop(user_id, None)
        self.pending_inputs.pop(user_id, None)
        if event_id is None:
            await self._send_and_log(
                context=context,
                chat_id=chat_id,
                user_id=user_id,
                username=username,
                text=self._localized_inline(
                    self._ui_language_or_default(user_id),
                    "I couldn't find the match linked to this feedback anymore.",
                    "Я больше не могу найти проект, к которому относился этот отзыв.",
                ),
                reply_markup=self._main_menu_markup(user_id),
            )
            return

        event = self.store.get_delivery_event(event_id)
        if event is None or event.user_id != user_id:
            await self._send_and_log(
                context=context,
                chat_id=chat_id,
                user_id=user_id,
                username=username,
                text=self._localized_inline(
                    self._ui_language_or_default(user_id),
                    "That match is no longer available, so I couldn't save the feedback details.",
                    "Этот проект больше недоступен, поэтому я не смог сохранить детали отзыва.",
                ),
                reply_markup=self._main_menu_markup(user_id),
            )
            return

        feedback_analysis = await self.filter_ai.analyze_negative_match_feedback(
            job_title=event.card_title,
            job_description="",
            job_location=event.card_location,
            job_salary="",
            match_reason=event.match_reason,
            user_feedback=feedback_text,
        )
        self.store.save_match_feedback_details(
            event_id,
            user_id,
            raw_feedback=feedback_text,
            feedback_summary=feedback_analysis.get("summary", ""),
            job_title_issue=feedback_analysis.get("job_title_issue", ""),
            location_issue=feedback_analysis.get("location_issue", ""),
            salary_issue=feedback_analysis.get("salary_issue", ""),
            other_issue=feedback_analysis.get("other_issue", ""),
        )
        self.user_message_logger.append_event(
            user_id=user_id,
            username=username,
            direction="system",
            text=f"match_feedback_detail={event_id}:{feedback_text}",
            event_type="match_feedback_detail",
            created_at_utc=utc_now_iso(),
        )
        await self._send_and_log(
            context=context,
            chat_id=chat_id,
            user_id=user_id,
            username=username,
            text=self._t(user_id, "feedback_saved"),
            reply_markup=self._main_menu_markup(user_id),
        )

    async def _handle_website_text(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        user_id: int,
        username: str,
        text: str,
    ) -> None:
        normalized_url = self._normalize_website_url(text)
        if normalized_url is None:
            await self._send_and_log(
                context=context,
                chat_id=chat_id,
                user_id=user_id,
                username=username,
                text=self._t(user_id, "website_invalid"),
                reply_markup=self._website_text_input_markup(user_id),
            )
            return
        if is_disabled_website(normalized_url):
            await self._send_and_log(
                context=context,
                chat_id=chat_id,
                user_id=user_id,
                username=username,
                text=self._t(
                    user_id,
                    "website_unmonitorable",
                    reason=self._localized_inline(
                        self._ui_language_or_default(user_id),
                        "This website is no longer supported by ZapLance.",
                        "Этот сайт больше не поддерживается в ZapLance.",
                    ),
                ),
                reply_markup=self._website_text_input_markup(user_id),
            )
            return
        review = await self.website_guard.review(normalized_url)
        if not review.accepted:
            await self._send_and_log(
                context=context,
                chat_id=chat_id,
                user_id=user_id,
                username=username,
                text=self._t(user_id, "website_unmonitorable", reason=review.reason),
                reply_markup=self._website_text_input_markup(user_id),
            )
            return
        self.pending_inputs.pop(user_id, None)
        self.pending_custom_website_urls[user_id] = normalized_url
        await self._send_and_log(
                context=context,
                chat_id=chat_id,
                user_id=user_id,
                username=username,
                text=self._t(user_id, "website_currency_prompt", website_url=normalized_url),
                reply_markup=self._website_currency_markup(user_id),
            )

    async def _handle_keywords_text(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        user_id: int,
        username: str,
        text: str,
    ) -> None:
        keywords = self._parse_keywords(text)
        if not keywords:
            await self._send_and_log(
                context=context,
                chat_id=chat_id,
                user_id=user_id,
                username=username,
                text=self._t(user_id, "keywords_invalid"),
                reply_markup=self._keywords_input_markup(user_id),
            )
            return
        await self._send_ai_processing_notice(context, chat_id, user_id, username)
        interpretation = await self.filter_ai.interpret_keywords(keywords)
        self.pending_inputs.pop(user_id, None)
        self.pending_keyword_reviews[user_id] = interpretation
        await self._send_and_log(
            context=context,
            chat_id=chat_id,
            user_id=user_id,
            username=username,
            text=self._keywords_confirmation_text(user_id, interpretation),
            reply_markup=self._keywords_confirm_markup(user_id),
        )

    async def _confirm_keywords_and_continue(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        user_id: int,
        username: str,
    ) -> None:
        interpretation = self.pending_keyword_reviews.pop(user_id, None)
        if interpretation is None:
            self.pending_inputs[user_id] = WAITING_KEYWORDS_INPUT
            await self._send_and_log(
                context=context,
                chat_id=chat_id,
                user_id=user_id,
                username=username,
                text=self._t(user_id, "keywords_input_prompt"),
                reply_markup=self._keywords_input_markup(user_id),
            )
            return
        expanded_keywords = interpretation.expanded_keywords[:MAX_KEYWORDS]
        self.store.replace_keywords(user_id, expanded_keywords)
        self._log_filter_update(user_id, username, "keywords", ",".join(expanded_keywords))
        if self.flow_origin.get(user_id) == FLOW_CREATE:
            await self._show_alert_summary(context, chat_id, user_id, username)
        else:
            await self._send_edit_saved(context, chat_id, user_id, username)

    async def _activate_alert(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        user_id: int,
        username: str,
    ) -> None:
        if not self.store.has_alert_configuration(user_id):
            await self._start_alert_creation_flow(context, chat_id, user_id, username)
            return
        active_sub = self.store.get_active_subscription(user_id)
        if active_sub is None:
            self._clear_user_flow_state(user_id)
            await self._send_and_log(
                context=context,
                chat_id=chat_id,
                user_id=user_id,
                username=username,
                text=self._t(user_id, "activate_trial_prompt"),
                reply_markup=self._trial_screen_markup(user_id),
            )
            return
        self.store.set_notification_preference(user_id, True)
        await self._notify_subscription_admin(self.store.get_active_subscription(user_id))
        self._clear_user_flow_state(user_id)
        await self._send_and_log(
            context=context,
            chat_id=chat_id,
            user_id=user_id,
            username=username,
            text=self._t(user_id, "alert_ready_active"),
            reply_markup=self._trial_success_markup(user_id),
        )

    async def _toggle_alert(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        user_id: int,
        username: str,
    ) -> None:
        if not self.store.has_alert_configuration(user_id):
            await self._start_alert_creation_flow(context, chat_id, user_id, username)
            return
        is_enabled = self.store.get_notification_preference(user_id)
        active_sub = self.store.get_active_subscription(user_id)
        if is_enabled:
            self.store.set_notification_preference(user_id, False)
            await self._send_and_log(
                context=context,
                chat_id=chat_id,
                user_id=user_id,
                username=username,
                text=self._t(user_id, "alert_paused_message"),
                reply_markup=self._my_alert_markup(user_id),
            )
            return
        if active_sub is None:
            await self._send_and_log(
                context=context,
                chat_id=chat_id,
                user_id=user_id,
                username=username,
                text=self._t(user_id, "activate_trial_prompt"),
                reply_markup=self._trial_screen_markup(user_id),
            )
            return
        self.store.set_notification_preference(user_id, True)
        await self._send_and_log(
            context=context,
            chat_id=chat_id,
            user_id=user_id,
            username=username,
            text=self._t(user_id, "alert_resumed_message"),
            reply_markup=self._my_alert_markup(user_id),
        )

    async def _activate_trial(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        user_id: int,
        username: str,
    ) -> None:
        active_sub = self.store.get_active_subscription(user_id)
        if active_sub is not None:
            has_alert = self.store.has_alert_configuration(user_id)
            await self._send_and_log(
                context=context,
                chat_id=chat_id,
                user_id=user_id,
                username=username,
                text=self._t(user_id, "access_active_with_alert") if has_alert else self._t(user_id, "access_active_without_alert"),
                reply_markup=self._trial_success_markup(user_id) if has_alert else self._empty_alert_markup(user_id),
            )
            return
        if self.store.has_claimed_trial(user_id):
            await self._send_and_log(
                context=context,
                chat_id=chat_id,
                user_id=user_id,
                username=username,
                text=self._t(user_id, "trial_used"),
                reply_markup=self._subscription_menu_markup(user_id),
            )
            return
        started_at_dt = utc_now_dt()
        ends_at_dt = started_at_dt + timedelta(days=7)
        created = self.store.upsert_trial_subscription(
            user_id=user_id,
            username=username,
            started_at_utc=format_utc_iso(started_at_dt),
            ends_at_utc=format_utc_iso(ends_at_dt),
        )
        if not created:
            await self._send_and_log(
                context=context,
                chat_id=chat_id,
                user_id=user_id,
                username=username,
                text=self._t(user_id, "trial_used"),
                reply_markup=self._subscription_menu_markup(user_id),
            )
            return
        self.store.set_notification_preference(user_id, True)
        await self._notify_subscription_admin(self.store.get_active_subscription(user_id))
        self._clear_user_flow_state(user_id)
        has_alert = self.store.has_alert_configuration(user_id)
        if has_alert:
            text = self._t(user_id, "trial_active_with_alert")
        else:
            text = self._t(user_id, "trial_active_without_alert")
        await self._send_and_log(
            context=context,
            chat_id=chat_id,
            user_id=user_id,
            username=username,
            text=text,
            reply_markup=self._trial_success_markup(user_id),
        )
        if not has_alert:
            await self._start_alert_creation_flow_in_dm(user_id, username)

    async def _start_paid_plan_payment(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        user_id: int,
        username: str,
        plan: PlanDefinition,
    ) -> None:
        order_id = f"{plan.plan_key}-{user_id}-{int(utc_now_dt().timestamp())}"
        try:
            invoice = await self.nowpayments.create_invoice(
                amount_usd=plan.amount_usd,
                order_id=order_id,
                order_description=f"{plan.plan_key} subscription",
            )
        except Exception as exc:  # noqa: BLE001
            await self._send_and_log(
                context=context,
                chat_id=chat_id,
                user_id=user_id,
                username=username,
                text=self._t(user_id, "payment_link_error", error=str(exc)),
                reply_markup=self._subscription_menu_markup(user_id),
            )
            return
        local_payment_id = self.store.create_payment_session(
            user_id=user_id,
            username=username,
            plan=plan.plan_key,
            duration_days=plan.duration_days,
            amount_usd=plan.amount_usd,
            provider_payment_id=invoice.provider_payment_id,
            payment_url=invoice.payment_url,
            status=invoice.status,
        )
        await self._send_and_log(
            context=context,
            chat_id=chat_id,
            user_id=user_id,
            username=username,
            text=self._payment_message_text(user_id, plan, invoice.payment_url, invoice.status),
            reply_markup=self._payment_actions_markup(user_id, local_payment_id),
        )

    async def _handle_payment_update(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        user_id: int,
        username: str,
        local_payment_id: str,
        is_manual: bool,
    ) -> None:
        session = self.store.get_payment_session(local_payment_id)
        if session is None or session.user_id != user_id:
            await self._send_and_log(
                context=context,
                chat_id=chat_id,
                user_id=user_id,
                username=username,
                text=self._t(user_id, "payment_session_not_found"),
                reply_markup=self._subscription_menu_markup(user_id),
            )
            return
        try:
            provider_status = await self.nowpayments.get_payment_status(session.provider_payment_id)
        except Exception as exc:  # noqa: BLE001
            if is_manual:
                await self._send_and_log(
                    context=context,
                    chat_id=chat_id,
                    user_id=user_id,
                    username=username,
                    text=self._t(user_id, "payment_refresh_error", error=str(exc)),
                    reply_markup=self._payment_actions_markup(session.user_id, session.local_payment_id),
                )
            return
        previous_status = session.status
        current_status = provider_status.lower()
        if current_status != previous_status:
            self.store.update_payment_status(session.local_payment_id, current_status)
            session = self.store.get_payment_session(session.local_payment_id) or session
        if current_status in PAID_COMPLETED_STATUSES:
            active_sub = self.store.activate_paid_subscription(
                user_id=session.user_id,
                username=session.username,
                plan=session.plan,
                duration_days=session.duration_days,
            )
            self.store.set_notification_preference(session.user_id, True)
            await self._send_payment_success(session.user_id, session.username, active_sub)
            await self.subscriber_notifier.flush_due_notifications(limit=200)
            return
        if current_status in PAYMENT_FINAL_STATUSES:
            if is_manual or current_status != previous_status:
                await self._send_to_user_and_log(
                    user_id=session.user_id,
                    username=session.username,
                    text=self._t(
                        session.user_id,
                        "payment_status_final",
                        status=payment_status_label(self._ui_language_or_default(session.user_id), current_status),
                    ),
                    reply_markup=self._subscription_menu_markup(session.user_id),
                )
            return
        if is_manual or current_status != previous_status:
            await self._send_and_log(
                context=context,
                chat_id=chat_id,
                user_id=user_id,
                username=username,
                text=self._t(
                    user_id,
                    "payment_status_update",
                    status=payment_status_label(self._ui_language_or_default(user_id), current_status),
                    payment_url=session.payment_url,
                ),
                reply_markup=self._payment_actions_markup(session.user_id, session.local_payment_id),
            )

    async def _handle_payment_cancel(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        user_id: int,
        username: str,
        local_payment_id: str,
    ) -> None:
        session = self.store.get_payment_session(local_payment_id)
        if session is None or session.user_id != user_id:
            await self._send_and_log(
                context=context,
                chat_id=chat_id,
                user_id=user_id,
                username=username,
                text=self._t(user_id, "payment_session_not_found"),
                reply_markup=self._subscription_menu_markup(user_id),
            )
            return
        self.store.cancel_payment(local_payment_id)
        await self._send_and_log(
            context=context,
            chat_id=chat_id,
            user_id=user_id,
            username=username,
            text=self._t(user_id, "payment_cancelled"),
            reply_markup=self._subscription_menu_markup(user_id),
        )

    async def _refresh_pending_payments(self, limit: int = 100) -> None:
        sessions = self.store.list_pending_payment_sessions(limit=limit)
        for session in sessions:
            try:
                status = (await self.nowpayments.get_payment_status(session.provider_payment_id)).lower()
            except Exception as exc:  # noqa: BLE001
                self.logger.warning(
                    "[telegram-menu] payment refresh failed for %s: %s",
                    session.local_payment_id,
                    str(exc),
                )
                continue
            if status == session.status:
                continue
            self.store.update_payment_status(session.local_payment_id, status)
            if status in PAID_COMPLETED_STATUSES:
                active_sub = self.store.activate_paid_subscription(
                    user_id=session.user_id,
                    username=session.username,
                    plan=session.plan,
                    duration_days=session.duration_days,
                )
                self.store.set_notification_preference(session.user_id, True)
                await self._send_payment_success(session.user_id, session.username, active_sub)
                await self.subscriber_notifier.flush_due_notifications(limit=200)
                continue
            if status in PAYMENT_FINAL_STATUSES:
                await self._send_to_user_and_log(
                    user_id=session.user_id,
                    username=session.username,
                    text=self._t(
                        session.user_id,
                        "payment_status_changed",
                        status=payment_status_label(self._ui_language_or_default(session.user_id), status),
                    ),
                    reply_markup=self._subscription_menu_markup(session.user_id),
                )
                continue
            await self._send_to_user_and_log(
                user_id=session.user_id,
                username=session.username,
                text=self._t(
                    session.user_id,
                    "payment_status_background_update",
                    status=payment_status_label(self._ui_language_or_default(session.user_id), status),
                    payment_url=session.payment_url,
                ),
                reply_markup=self._payment_actions_markup(session.user_id, session.local_payment_id),
            )

    async def _send_payment_success(
        self,
        user_id: int,
        username: str,
        active_sub: ActiveSubscription,
    ) -> None:
        await self._notify_subscription_admin(active_sub)
        has_alert = self.store.has_alert_configuration(user_id)
        await self._send_to_user_and_log(
            user_id=user_id,
            username=username,
            text=self._t(
                user_id,
                "payment_success_message",
                plan_name=self._plan_display_name(active_sub.plan),
                expires_at=format_utc_readable(active_sub.ends_at_utc),
                next_step=self._t(user_id, "payment_success_next_step") if not has_alert else "",
            ),
            reply_markup=self._payment_success_markup(user_id),
        )
        if not has_alert:
            await self._start_alert_creation_flow_in_dm(user_id, username)

    async def _notify_subscription_admin(self, active_sub: ActiveSubscription | None) -> None:
        notifier = getattr(self, "subscription_admin_notifier", None)
        if notifier is None or active_sub is None:
            return
        await notifier.notify_subscription_event(active_sub)

    async def _maybe_notify_admin_first_start(self, user_id: int, username: str) -> None:
        store = getattr(self, "store", None)
        notifier = getattr(self, "subscription_admin_notifier", None)
        if store is None or notifier is None:
            return
        has_notified = getattr(store, "has_first_start_admin_notified", None)
        mark_notified = getattr(store, "mark_first_start_admin_notified", None)
        if not callable(has_notified) or not callable(mark_notified):
            return
        if has_notified(user_id):
            return
        try:
            notification_complete = await notifier.notify_first_start(user_id=user_id, username=username)
        except Exception as exc:  # noqa: BLE001
            self.logger.warning(
                "[telegram-menu] failed to send first-start admin notification user_id=%s: %s",
                user_id,
                str(exc),
            )
            return
        if notification_complete:
            mark_notified(user_id)

    async def _send_expiry_reminders(self) -> None:
        for sub in self.store.list_subscriptions_for_expiry_reminder():
            text = self._t(sub.user_id, "expiry_trial" if sub.source == "trial" else "expiry_subscription")
            await self._send_to_user_and_log(
                user_id=sub.user_id,
                username=sub.username,
                text=text,
                reply_markup=self._subscription_menu_markup(sub.user_id),
            )
            self.store.mark_expiry_reminder_sent(sub.user_id)

    async def _send_expiration_notices(self) -> None:
        for sub in self.store.list_recently_expired_subscriptions():
            text = self._t(sub.user_id, "expired_trial" if sub.source == "trial" else "expired_subscription")
            await self._send_to_user_and_log(
                user_id=sub.user_id,
                username=sub.username,
                text=text,
                reply_markup=self._subscription_menu_markup(sub.user_id),
            )
            self.store.mark_expiration_notice_sent(sub.user_id)

    @staticmethod
    def _removed_source_display_name(source: str) -> str:
        normalized = str(source or "").strip().lower().rstrip("/")
        if "fl.ru/projects/category/dizajn/web-dizajner-verstalschik-dizajn" in normalized:
            return "FL.ru Web Design category"
        normalized = strip_www_prefix(normalized)
        if normalized == "hh.ru":
            return "HeadHunter"
        if normalized == "rabota.ru":
            return "Rabota.ru"
        if normalized.endswith(".com") and normalized.count(".") == 1:
            return normalized.split(".", 1)[0].capitalize()
        return normalized or "This source"

    async def _send_removed_source_notice(self, user_id: int, username: str, source: str) -> None:
        selected_sources = set(self.store.get_user_websites(user_id))
        source_name = self._removed_source_display_name(source)
        if selected_sources:
            text = self._t(
                user_id,
                "source_removed_notice",
                source_name=source_name,
                current_sources=self._selected_sources_text(selected_sources, user_id),
            )
        else:
            text = self._t(
                user_id,
                "source_removed_notice_empty",
                source_name=source_name,
            )
        await self._send_to_user_and_log(
            user_id=user_id,
            username=username,
            text=text,
            reply_markup=self._website_removed_notice_markup(user_id),
            disable_web_page_preview=True,
        )

    async def _send_pending_system_notices(self, limit: int = 50) -> None:
        notices_getter = getattr(self.store, "list_due_system_notices", None)
        mark_sent = getattr(self.store, "mark_system_notice_sent", None)
        reschedule = getattr(self.store, "reschedule_system_notice", None)
        if not callable(notices_getter) or not callable(mark_sent) or not callable(reschedule):
            return
        for notice in notices_getter(limit=limit):
            notice_key = str(getattr(notice, "notice_key", "") or "").strip().lower()
            user_id = int(getattr(notice, "user_id", 0) or 0)
            if user_id <= 0 or not notice_key:
                continue
            username = str(getattr(notice, "username", "") or user_id)
            try:
                if notice_key.startswith("website_removed:"):
                    removed_source = notice_key.split(":", 1)[1].strip()
                    await self._send_removed_source_notice(user_id, username, removed_source)
                    mark_sent(int(getattr(notice, "notice_id", 0) or 0))
                    continue
                mark_sent(int(getattr(notice, "notice_id", 0) or 0))
            except Exception as exc:  # noqa: BLE001
                self.logger.warning(
                    "[telegram-menu] failed to send system notice user_id=%s key=%s: %s",
                    user_id,
                    notice_key,
                    str(exc),
                )
                reschedule(
                    int(getattr(notice, "notice_id", 0) or 0),
                    attempt_count=max(0, int(getattr(notice, "attempt_count", 0) or 0)),
                    last_error=str(exc),
                )

    async def _send_or_edit_and_log(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        user_id: int,
        username: str,
        text: str,
        reply_markup: InlineKeyboardMarkup | None = None,
        edit_query: CallbackQuery | None = None,
    ) -> None:
        if edit_query is not None:
            try:
                await edit_query.edit_message_text(text=text, reply_markup=reply_markup)
            except BadRequest as exc:
                if "message is not modified" not in str(exc).lower():
                    await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)
        else:
            await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)
        self.user_message_logger.append_event(
            user_id=user_id,
            username=username,
            direction="out",
            text=text,
            event_type="bot_message",
            created_at_utc=utc_now_iso(),
        )

    async def _send_and_log(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        user_id: int,
        username: str,
        text: str,
        reply_markup: InlineKeyboardMarkup | None = None,
        disable_web_page_preview: bool = False,
    ) -> None:
        send_kwargs: dict[str, object] = {
            "chat_id": chat_id,
            "text": text,
            "reply_markup": reply_markup,
        }
        if disable_web_page_preview:
            send_kwargs["disable_web_page_preview"] = True
        await context.bot.send_message(
            **send_kwargs,
        )
        self.user_message_logger.append_event(
            user_id=user_id,
            username=username,
            direction="out",
            text=text,
            event_type="bot_message",
            created_at_utc=utc_now_iso(),
        )

    async def _send_custom_website_guide_image(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        user_id: int,
        username: str,
        text: str,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> None:
        path = self.custom_website_guide_image_path
        if not path.exists():
            self.logger.warning("[telegram-menu] custom website guide image not found: %s", path)
            await self._send_and_log(
                context=context,
                chat_id=chat_id,
                user_id=user_id,
                username=username,
                text=text,
                reply_markup=reply_markup,
            )
            return
        try:
            with path.open("rb") as image_file:
                await context.bot.send_photo(
                    chat_id=chat_id,
                    photo=image_file,
                    caption=text,
                    reply_markup=reply_markup,
                    write_timeout=60,
                    read_timeout=60,
                    connect_timeout=20,
                    pool_timeout=20,
                )
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("[telegram-menu] failed to send custom website guide image %s: %s", path, str(exc))
            return
        self.user_message_logger.append_event(
            user_id=user_id,
            username=username,
            direction="out",
            text=f"[image] {path.name}\n{text}",
            event_type="bot_message",
            created_at_utc=utc_now_iso(),
        )

    async def _send_to_user_and_log(
        self,
        user_id: int,
        username: str,
        text: str,
        reply_markup: InlineKeyboardMarkup | None = None,
        disable_web_page_preview: bool = False,
    ) -> None:
        send_kwargs: dict[str, object] = {
            "chat_id": user_id,
            "text": text,
            "reply_markup": reply_markup,
        }
        if disable_web_page_preview:
            send_kwargs["disable_web_page_preview"] = True
        await self.application.bot.send_message(**send_kwargs)
        self.user_message_logger.append_event(
            user_id=user_id,
            username=username,
            direction="out",
            text=text,
            event_type="bot_background_message",
            created_at_utc=utc_now_iso(),
        )

    def _log_inbound(self, user_id: int, username: str, text: str, event_type: str) -> None:
        self.user_message_logger.append_event(
            user_id=user_id,
            username=username,
            direction="in",
            text=text,
            event_type=event_type,
            created_at_utc=utc_now_iso(),
        )

    def _log_filter_update(self, user_id: int, username: str, filter_name: str, filter_value: str) -> None:
        self.store.log_filter_change(user_id=user_id, filter_name=filter_name, filter_value=filter_value)
        normalized_filter_name = filter_name.strip().lower()
        if normalized_filter_name in QUEUE_INVALIDATING_FILTERS:
            cancelled_count = self.store.cancel_queued_notifications_for_user(
                user_id,
                reason=f"filter_changed:{normalized_filter_name}",
            )
            if cancelled_count:
                self.logger.info(
                    "[telegram-menu] cancelled stale queued notifications user=%s filter=%s count=%s",
                    user_id,
                    normalized_filter_name,
                    cancelled_count,
                )
        self.user_message_logger.append_event(
            user_id=user_id,
            username=username,
            direction="system",
            text=f"{filter_name}={filter_value}",
            event_type="filter_update",
            created_at_utc=utc_now_iso(),
        )

    def _clear_user_flow_state(self, user_id: int) -> None:
        for attr_name in (
            "pending_inputs",
            "pending_match_feedback_event_ids",
            "pending_location_candidates",
            "pending_location_modes",
            "pending_location_selections",
            "pending_keyword_reviews",
            "pending_project_filter_fields",
            "pending_website_choices",
            "pending_custom_website_urls",
            "pending_custom_website_currency",
            "website_picker_cache",
            "flow_origin",
            "flow_step",
        ):
            store = getattr(self, attr_name, None)
            if hasattr(store, "pop"):
                store.pop(user_id, None)

    def _capture_deleted_alert_snapshot(self, user_id: int) -> DeletedAlertSnapshot | None:
        if not self.store.has_alert_configuration(user_id):
            return None
        return DeletedAlertSnapshot(
            role_title=self.store.get_role_preference(user_id),
            project_preferences=self.store.get_project_preferences(user_id),
            websites=tuple(self.store.get_user_websites_with_currency(user_id)),
            keywords=tuple(self.store.get_user_keywords(user_id)),
            spheres=tuple(self.store.get_user_spheres(user_id)),
        )

    async def _undo_deleted_alert(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        user_id: int,
        username: str,
    ) -> None:
        snapshot = self.deleted_alert_snapshots.pop(user_id, None)
        if snapshot is None:
            await self._send_and_log(
                context=context,
                chat_id=chat_id,
                user_id=user_id,
                username=username,
                text=self._localized_inline(
                    self._ui_language_or_default(user_id),
                    "There is no recently deleted alert to restore.",
                    "Нет недавно удалённого поиска для восстановления.",
                ),
                reply_markup=self._empty_alert_markup(user_id),
            )
            return
        self.store.set_role_preference(user_id, snapshot.role_title)
        self.store.set_project_preferences(user_id, snapshot.project_preferences)
        self.store.replace_user_spheres(user_id, list(snapshot.spheres))
        self.store.replace_keywords(user_id, list(snapshot.keywords))
        self._persist_selected_websites(
            user_id,
            username,
            {website_url for website_url, _currency in snapshot.websites},
            website_currency_overrides=dict(snapshot.websites),
        )
        self.store.set_notification_preference(user_id, True)
        self._log_filter_update(user_id, username, "alert_restored", "true")
        self._clear_user_flow_state(user_id)
        active_sub = self.store.get_active_subscription(user_id)
        await self._send_and_log(
            context=context,
            chat_id=chat_id,
            user_id=user_id,
            username=username,
            text=(
                "✅ Your deleted alert has been restored and is active again."
                if active_sub is not None
                else "✅ Your deleted alert has been restored and turned back on."
            ),
            reply_markup=self._my_alert_markup(user_id),
        )

    @staticmethod
    def _remote_within_country_location(country: str) -> str:
        normalized_country = " ".join(str(country or "").split()).strip()
        if not normalized_country:
            return REMOTE_WITHIN_COUNTRY_BUTTON_LABEL
        return f"{REMOTE_WITHIN_COUNTRY_LABEL} {normalized_country}"

    @staticmethod
    def _onsite_hybrid_within_country_location(country: str) -> str:
        normalized_country = " ".join(str(country or "").split()).strip()
        if not normalized_country:
            return ONSITE_HYBRID_WITHIN_COUNTRY_BUTTON_LABEL
        return f"{ONSITE_HYBRID_WITHIN_COUNTRY_LABEL} {normalized_country}"

    @staticmethod
    def _remote_within_country_location_country(raw_value: str) -> str:
        cleaned = " ".join(str(raw_value or "").split()).strip()
        if not cleaned:
            return ""
        lowered = cleaned.lower()
        prefix = f"{REMOTE_WITHIN_COUNTRY_LABEL.lower()} "
        if lowered.startswith(prefix):
            return cleaned[len(REMOTE_WITHIN_COUNTRY_LABEL) :].strip(" -,:")
        return ""

    @staticmethod
    def _onsite_hybrid_within_country_location_country(raw_value: str) -> str:
        cleaned = " ".join(str(raw_value or "").split()).strip()
        if not cleaned:
            return ""
        lowered = cleaned.lower()
        prefixes = (
            "on-site/hybrid within ",
            "onsite/hybrid within ",
            "on site/hybrid within ",
            "on-site ",
            "onsite ",
            "on site ",
        )
        for prefix in prefixes:
            if lowered.startswith(prefix):
                return cleaned[len(prefix) :].strip(" -,:")
        return ""

    def _available_website_choices(self, user_id: int) -> list[str]:
        default_urls = [url for _, url in self._default_site_choices(self.store.get_role_preference(user_id))]
        urls: list[str] = []
        seen: set[str] = set()
        for raw_url in default_urls:
            normalized = self._normalize_website_url(raw_url)
            if normalized is None or normalized in seen:
                continue
            urls.append(normalized)
            seen.add(normalized)
        return urls

    def _default_site_choices(self, role_title: str) -> list[tuple[str, str]]:
        del role_title
        label_map = self._website_label_map()
        if self.scrape_sites_file_path.exists():
            raw_lines = self.scrape_sites_file_path.read_text(encoding="utf-8").splitlines()
            normalized_from_file: list[str] = []
            seen: set[str] = set()
            for raw_line in raw_lines:
                normalized = self._normalize_website_url(raw_line)
                if normalized is None or normalized in seen:
                    continue
                normalized_from_file.append(normalized)
                seen.add(normalized)
            if normalized_from_file:
                return [(label_map.get(url, self._site_label_from_url(url) or urlparse(url).netloc or url), url) for url in normalized_from_file]
        return list(UX_UI_WEBSITE_OPTIONS)

    def _remap_default_source_urls(self, urls: set[str] | list[str], role_title: str) -> list[str]:
        default_map = dict(self._default_site_choices(role_title))
        allowed_urls = {
            normalized
            for normalized in (
                self._normalize_website_url(default_url)
                for default_url in default_map.values()
            )
            if normalized is not None
        }
        remapped: list[str] = []
        seen: set[str] = set()
        for raw_url in urls:
            label = self._site_label_from_url(raw_url)
            candidate = default_map.get(label, raw_url)
            normalized = self._normalize_website_url(candidate)
            if normalized is None or normalized in seen:
                continue
            if normalized not in allowed_urls and candidate == raw_url:
                remapped.append(normalized)
                seen.add(normalized)
                continue
            if normalized not in allowed_urls:
                continue
            remapped.append(normalized)
            seen.add(normalized)
        return remapped

    def _sync_saved_default_websites_for_role(self, user_id: int, username: str) -> None:
        current = self.store.get_user_websites(user_id)
        if not current:
            return
        remapped = self._remap_default_source_urls(set(current), self.store.get_role_preference(user_id))
        normalized_current = {self._normalize_website_url(url) for url in current}
        if normalized_current == set(remapped):
            return
        self._persist_selected_websites(
            user_id,
            username,
            set(remapped),
            website_currency_overrides=dict(self.store.get_user_websites_with_currency(user_id)),
        )

    def _website_currency_from_url(self, website_url: str, user_id: int | None = None) -> str:
        normalized_url = self._normalize_website_url(website_url) or str(website_url).strip()
        if user_id is not None:
            pending_url = getattr(self, "pending_custom_website_urls", {}).get(user_id, "")
            pending_currency = normalize_website_currency(getattr(self, "pending_custom_website_currency", {}).get(user_id))
            if pending_url and normalized_url == pending_url and pending_currency:
                return pending_currency
            getter = getattr(self.store, "get_user_website_currency", None)
            if callable(getter):
                stored_currency = normalize_website_currency(getter(user_id, normalized_url))
                if stored_currency:
                    return stored_currency
        label = self._website_label_map().get(normalized_url) or self._site_label_from_url(normalized_url)
        normalized_label = str(label or "").lower()
        if "[ru]" in normalized_label:
            return "RUB"
        if "[en]" in normalized_label:
            return "USD"
        return infer_website_currency(normalized_url)

    def _persist_selected_websites(
        self,
        user_id: int,
        username: str,
        selected: set[str],
        *,
        website_currency_overrides: dict[str, str] | None = None,
    ) -> None:
        normalized_urls = self._remap_default_source_urls(selected, self.store.get_role_preference(user_id))
        overrides = {
            self._normalize_website_url(url) or str(url).strip(): normalize_website_currency(currency)
            for url, currency in (website_currency_overrides or {}).items()
            if (self._normalize_website_url(url) or str(url).strip())
        }
        self.store.clear_user_websites(user_id)
        for website_url in normalized_urls:
            normalized_url = self._normalize_website_url(website_url) or website_url
            website_currency = overrides.get(normalized_url) or self._website_currency_from_url(normalized_url, user_id)
            self.store.add_user_website(user_id, normalized_url, currency=website_currency)
            self._append_scrape_website_if_missing(website_url)
        self._log_filter_update(user_id, username, "websites", ",".join(normalized_urls))
        self.pending_website_choices[user_id] = set(normalized_urls)
        request_websites_refresh()

    def _website_display_name(self, website_url: str) -> str:
        label = self._website_label_map().get(self._normalize_website_url(website_url) or website_url)
        if label:
            return label
        label = self._site_label_from_url(website_url)
        if label:
            return label
        parsed = urlparse(website_url)
        return parsed.netloc or website_url

    def _website_label_map(self) -> dict[str, str]:
        label_map: dict[str, str] = {}
        for label, raw_url in UX_UI_WEBSITE_OPTIONS:
            normalized = self._normalize_website_url(raw_url)
            if normalized is not None:
                label_map[normalized] = label
        return label_map

    def _site_label_from_url(self, website_url: str) -> str:
        normalized = self._normalize_website_url(website_url) or str(website_url).strip()
        exact_label = self._website_label_map().get(normalized)
        if exact_label:
            return exact_label
        parsed = urlparse(str(website_url).strip())
        domain = strip_www_prefix(parsed.netloc or str(website_url).strip())
        for candidate_domain, label in SITE_DOMAIN_LABELS.items():
            if domain == candidate_domain or domain.endswith(f".{candidate_domain}"):
                return label
        return ""

    def _extract_website_from_picker_callback(self, user_id: int, callback_data: str) -> str | None:
        index_text = callback_data[len(CALLBACK_WEBSITE_PICK_PREFIX) :]
        if not index_text.isdigit():
            return None
        options = self.website_picker_cache.get(user_id, self._available_website_choices(user_id))
        index = int(index_text)
        if 0 <= index < len(options):
            return options[index]
        return None

    def _append_scrape_website_if_missing(self, website_url: str) -> bool:
        normalized = self._normalize_website_url(website_url)
        if normalized is None:
            return False
        self.scrape_sites_file_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.scrape_sites_file_path.exists():
            self.scrape_sites_file_path.write_text("", encoding="utf-8")
        existing = {
            self._normalize_website_url(line)
            for line in self.scrape_sites_file_path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        }
        if normalized in existing:
            return False
        with self.scrape_sites_file_path.open("a", encoding="utf-8") as file:
            if self.scrape_sites_file_path.stat().st_size > 0:
                file.write("\n")
            file.write(normalized)
        return True

    @staticmethod
    def _parse_keywords(text: str) -> list[str]:
        ordered: list[str] = []
        seen: set[str] = set()
        for raw_token in re.split(r"[,;|\n]", text):
            token = raw_token.strip().lower()
            if not token or len(token) > 50 or token in seen:
                continue
            seen.add(token)
            ordered.append(token)
            if len(ordered) >= MAX_KEYWORDS:
                break
        return ordered

    @staticmethod
    def _resolve_username(user: User) -> str:
        return user.username or user.full_name or str(user.id)

    @staticmethod
    def _normalize_website_url(value: str) -> str | None:
        cleaned = value.strip().lstrip("\ufeff")
        parsed = urlparse(cleaned)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return None
        normalized = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{parsed.path.rstrip('/')}"
        if parsed.query:
            normalized = f"{normalized}?{parsed.query}"
        return normalized

    @staticmethod
    def _slugify(value: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
        return slug or "ux-ui-designer"

    @staticmethod
    def _salary_preference_label(include_no_salary: bool) -> str:
        return "Payment is optional" if include_no_salary else "Visible payment required"

    @staticmethod
    def _project_filter_alias_map() -> dict[str, tuple[str, ...]]:
        return {
            "deliverables": ("deliverables", "deliverable", "scope", "project scope", "sub-specialty", "sub specialty"),
            "minimum_payment_usd": ("min payment usd", "minimum payment usd", "min usd", "minimum usd"),
            "maximum_payment_usd": ("max payment usd", "maximum payment usd", "max usd", "maximum usd"),
            "minimum_payment_rub": ("min payment rub", "minimum payment rub", "min rub", "minimum rub"),
            "maximum_payment_rub": ("max payment rub", "maximum payment rub", "max rub", "maximum rub"),
        }

    @staticmethod
    def _project_filter_display_labels() -> dict[str, str]:
        return {
            "deliverables": "Deliverables",
            "minimum_payment_usd": "Min Payment USD",
            "maximum_payment_usd": "Max Payment USD",
            "minimum_payment_rub": "Min Payment RUB",
            "maximum_payment_rub": "Max Payment RUB",
        }

    @staticmethod
    def _parse_project_filter_values(value: str) -> list[str]:
        ordered: list[str] = []
        seen: set[str] = set()
        for raw_token in re.split(r"[,;|\n]", value):
            cleaned = re.sub(r"\s+", " ", raw_token.strip()).strip(" ,;|")
            lowered = cleaned.lower()
            if not cleaned or lowered in seen:
                continue
            if lowered == "either":
                for alias in ("fixed", "hourly"):
                    if alias not in seen:
                        seen.add(alias)
                        ordered.append(alias)
                continue
            seen.add(lowered)
            ordered.append(cleaned)
        return ordered

    @staticmethod
    def _parse_project_filter_number(value: str) -> float | None:
        return shared_parse_project_filter_number(value)

    @staticmethod
    def _parse_project_filter_bool(value: str) -> bool | None:
        return shared_parse_project_filter_bool(value)

    def _parse_project_preferences_text(self, text: str, user_id: int) -> dict[str, object]:
        return shared_parse_project_preferences_text(text, self.store.get_project_preferences(user_id))

    def _project_preferences_summary_lines(self, user_id: int) -> list[str]:
        language = self._ui_language_or_default(user_id)
        preferences = normalize_project_preferences(self.store.get_project_preferences(user_id))
        lines: list[str] = []

        deliverables = preferences.get("deliverables") or []
        if deliverables:
            lines.append(
                f"• {project_filter_label(language, 'deliverables')}: "
                f"{', '.join(str(item) for item in deliverables)}"
            )

        minimum_fixed = preferences.get("minimum_fixed_budget_usd")
        minimum_hourly = preferences.get("minimum_hourly_rate_usd")
        if minimum_fixed is not None:
            lines.append(
                f"• {project_filter_label(language, 'minimum_fixed_budget_usd')}: "
                f"{shared_format_project_filter_value(language, 'minimum_fixed_budget_usd', minimum_fixed)}"
            )
        if minimum_hourly is not None:
            lines.append(
                f"• {project_filter_label(language, 'minimum_hourly_rate_usd')}: "
                f"{shared_format_project_filter_value(language, 'minimum_hourly_rate_usd', minimum_hourly)}"
            )
        return lines

    def _project_preferences_summary_text(self, user_id: int) -> str:
        lines = self._project_preferences_summary_lines(user_id)
        if not lines:
            return "Current project filters: none yet. You can skip this step or send only the fields that matter to you."
        return "Current project filters:\n" + "\n".join(lines[:10])

    def _project_preferences_log_text(self, user_id: int) -> str:
        lines = self._project_preferences_summary_lines(user_id)
        if not lines:
            return "none"
        return " | ".join(line.replace("• ", "", 1) for line in lines[:8])

    def _project_preferences_input_text(self, user_id: int) -> str:
        return (
            "🧩 Send the project filters you want to save.\n"
            "Keep it to the 3 critical filters only.\n\n"
            "Example:\n"
            "Deliverables: landing pages, dashboards, mobile app redesign\n"
            "Min Fixed USD: 800\n"
            "Min Hourly USD: 35\n"
            "\n"
            f"{self._project_preferences_summary_text(user_id)}"
        )

    @staticmethod
    def _plan_display_name(plan_key: str) -> str:
        normalized = str(plan_key or "").strip().lower()
        if normalized == "trial_48h":
            return "Trial 48H"
        if normalized == "14-day":
            return "14-Day"
        return str(plan_key or "").replace("_", " ").title() or "Unknown"

    @staticmethod
    def _localized_plan_display_name(language: str, plan_key: str) -> str:
        normalized = str(plan_key or "").strip().lower()
        normalized_language = str(language or "en").strip().lower()
        if normalized_language == "ru":
            if normalized == "trial_48h":
                return "пробный период 48 часов"
            if normalized == "14-day":
                return "14 дней"
            if normalized == "monthly":
                return "1 месяц"
            if normalized == "quarterly":
                return "3 месяца"
        return TelegramMenuBot._plan_display_name(plan_key)

    @staticmethod
    def _display_location(raw_value: str) -> str:
        cleaned = raw_value.strip()
        split_values = FilterAI.split_location_preferences(cleaned)
        if len(split_values) > 1:
            return "; ".join(TelegramMenuBot._display_location(item) for item in split_values)
        normalized = cleaned.lower().replace("-", " ")
        normalized_no_hint = re.sub(r"\([^)]*\)", "", normalized).strip()
        if normalized_no_hint == "remote global":
            return REMOTE_GLOBAL_LABEL
        remote_within_prefix = f"{REMOTE_WITHIN_COUNTRY_LABEL.lower().replace('-', ' ')} "
        if normalized_no_hint.startswith("remote") and not normalized_no_hint.startswith(remote_within_prefix):
            return REMOTE_GLOBAL_LABEL
        if normalized.startswith(remote_within_prefix):
            country = cleaned[len(REMOTE_WITHIN_COUNTRY_LABEL) :].strip(" -,:")
            return TelegramMenuBot._remote_within_country_location(country)
        onsite_prefixes = (
            "on-site/hybrid within ",
            "onsite/hybrid within ",
            "on site/hybrid within ",
            "on-site ",
            "onsite ",
            "on site ",
        )
        lowered = cleaned.lower()
        for prefix in onsite_prefixes:
            if lowered.startswith(prefix):
                country = cleaned[len(prefix) :].strip(" -,:")
                return TelegramMenuBot._onsite_hybrid_within_country_location(country)
        return cleaned or ALL_LOCATIONS_LABEL

    def _delivery_mode_label(self, user_id: int) -> str:
        return "Digest every 6 hours" if self.store.get_delivery_mode(user_id) == "digest" else "Instant alerts"

    @staticmethod
    def _timezone_offset_label(offset_minutes: int) -> str:
        sign = "+" if offset_minutes >= 0 else "-"
        total_minutes = abs(int(offset_minutes))
        hours, minutes = divmod(total_minutes, 60)
        return f"UTC{sign}{hours:02d}:{minutes:02d}"

    def _quiet_hours_label(self, user_id: int) -> str:
        quiet_range = self.store.get_quiet_hours_range(user_id)
        if quiet_range is None:
            return "Off"
        start_hour, end_hour = quiet_range
        return f"{start_hour:02d}:00-{end_hour:02d}:00 local time"

    def _delivery_summary_text(self, user_id: int) -> str:
        return (
            f"• Mode: {self._delivery_mode_label(user_id)}\n"
            f"• Timezone: {self._timezone_offset_label(self.store.get_timezone_offset_minutes(user_id))}\n"
            f"• Quiet hours: {self._quiet_hours_label(user_id)}"
        )

    def _selected_sources_text(self, selected: set[str]) -> str:
        if not selected:
            return "None selected"
        return "\n".join(f"• {self._website_display_name(url)}" for url in sorted(selected))

    @staticmethod
    def _parse_timezone_offset_minutes(text: str) -> int | None:
        normalized = text.strip().upper().replace("UTC", "").replace(" ", "")
        match = re.fullmatch(r"([+-]?)(\d{1,2})(?::?(\d{2}))?", normalized)
        if match is None:
            return None
        sign_text, hours_text, minutes_text = match.groups()
        hours = int(hours_text)
        minutes = int(minutes_text or "0")
        if hours > 14 or minutes >= 60:
            return None
        sign = -1 if sign_text == "-" else 1
        return sign * ((hours * 60) + minutes)

    @staticmethod
    def _parse_quiet_hours_input(text: str) -> tuple[int, int] | None:
        normalized = text.strip().lower().replace("to", "-").replace("–", "-").replace("—", "-")
        parts = [part.strip() for part in normalized.split("-") if part.strip()]
        if len(parts) != 2:
            return None

        def _parse_local_time(value: str, *, round_end_up: bool) -> int | None:
            match = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?", value)
            if match is None:
                return None
            hour = int(match.group(1))
            minute = int(match.group(2) or "0")
            if hour > 23 or minute > 59:
                return None
            if round_end_up and minute > 0:
                return (hour + 1) % 24
            return hour

        start_hour = _parse_local_time(parts[0], round_end_up=False)
        end_hour = _parse_local_time(parts[1], round_end_up=True)
        if start_hour is None or end_hour is None:
            return None
        return (start_hour, end_hour)

    @staticmethod
    def _keywords_preview(keywords: list[str], limit: int = 10) -> str:
        if not keywords:
            return "None"
        if len(keywords) <= limit:
            return ", ".join(keywords)
        remaining = len(keywords) - limit
        return f"{', '.join(keywords[:limit])}, ... (+{remaining} more)"

    def _role_prompt_text(self, user_id: int) -> str:
        current_role = self.store.get_role_preference(user_id) or DEFAULT_ROLE_PRESETS[0]
        heading = "🧩 Step 1 of 6" if self.flow_origin.get(user_id) == FLOW_CREATE else "🧩 Edit Primary Specialty"
        current_line = f"\nCurrent: {current_role}" if current_role else ""
        return (
            f"{heading}\n"
            "What kind of projects do you want?\n"
            "Choose your primary specialty or type your own.\n"
            "In the next step, you can add deliverables and budget floors."
            f"{current_line}"
        )

    def _keywords_confirmation_text(self, interpretation: KeywordInterpretation) -> str:
        lines = [
            "🧠 Here is what the AI understood from your keywords:",
            f"🎯 Main focus: {', '.join(interpretation.understood_keywords) or 'None'}",
        ]
        for item in interpretation.per_keyword:
            similar = ", ".join(item.similar_keywords[:10]) if item.similar_keywords else "No extra variations"
            lines.append(f"• {item.input_keyword} -> {item.understood_as}")
            lines.append(f"🔎 Similar terms: {similar}")
        lines.extend(
            [
                "",
                f"🏷️ Total tracked keywords: {len(interpretation.expanded_keywords)}",
                "Tap Confirm to save them or Re-enter to adjust them.",
            ]
        )
        return "\n".join(lines)

    def _alert_summary_text(self, user_id: int) -> str:
        role = self.store.get_role_preference(user_id) or "Not set"
        location = self._display_location(self.store.get_location_preference(user_id))
        websites = self._selected_sources_text(set(self.store.get_user_websites(user_id)))
        keywords = self.store.get_user_keywords(user_id)
        keywords_text = self._keywords_preview(keywords)
        project_filters = self._project_preferences_summary_text(user_id).replace("Current project filters:\n", "")
        return (
            "✅ Your alert is ready\n"
            f"🧩 Primary specialty: {role}\n"
            f"📍 Location: {location}\n"
            f"🧩 Project filters:\n{project_filters}\n"
            f"🌐 Sources:\n{websites}\n"
            f"🏷️ Project keywords: {keywords_text}\n"
            f"⏰ Delivery:\n{self._delivery_summary_text(user_id)}"
        )

    def _my_alert_text(self, user_id: int) -> str:
        role = self.store.get_role_preference(user_id) or "Not set"
        location = self._display_location(self.store.get_location_preference(user_id))
        websites = self._selected_sources_text(set(self.store.get_user_websites(user_id)))
        keywords = self.store.get_user_keywords(user_id)
        keywords_text = self._keywords_preview(keywords)
        project_filters = self._project_preferences_summary_text(user_id).replace("Current project filters:\n", "")
        latest_sub = self.store.get_latest_subscription(user_id)
        if latest_sub is None or not latest_sub.is_active:
            status = "🔴 Inactive"
        elif not self.store.get_notification_preference(user_id):
            status = "⏸️ Paused"
        else:
            status = "🟢 Active"
        return (
            "🎯 My Alert\n"
            f"🔔 Status: {status}\n"
            f"🧩 Primary specialty: {role}\n"
            f"📍 Location: {location}\n"
            f"🧩 Project filters:\n{project_filters}\n"
            f"🌐 Sources:\n{websites}\n"
            f"🏷️ Project keywords: {keywords_text}\n"
            f"⏰ Delivery:\n{self._delivery_summary_text(user_id)}"
        )

    def _main_menu_text(self, user_id: int) -> str:
        latest_sub = self.store.get_latest_subscription(user_id)
        if not self.store.has_alert_configuration(user_id):
            alert_line = "🔔 Alert: not set up yet"
        elif latest_sub is None or not latest_sub.is_active:
            alert_line = "🔔 Alert: ready to activate"
        elif not self.store.get_notification_preference(user_id):
            alert_line = "🔔 Alert: paused"
        else:
            alert_line = "🔔 Alert: active"
        if latest_sub is None or not latest_sub.is_active:
            sub_line = "💳 Subscription: inactive"
        else:
            sub_line = (
                f"💳 Subscription: {self._plan_display_name(latest_sub.plan)} "
                f"until {format_utc_readable(latest_sub.ends_at_utc)}"
            )
        return (
            "🏠 Main Menu\n"
            "⚡ Welcome to ZapLance ⚡\n"
            "🚀 We deliver relevant freelance opportunities, projects, and contracts from multiple sources directly to your Telegram in under 1 minute from posting time.\n\n"
            "🎯 Set your specialty, project filters, sources, and keywords once, and we’ll monitor the market for you.\n\n"
            "⚡️ Why this matters\n"
            "Most freelancers discover new briefs and contracts hours later.\n"
            "With Zap Careers, you can see relevant opportunities within seconds after they are published.\n\n"
            "🛠️ Use the menu below to manage your alerts, update your filters, or upgrade your plan.\n\n"
            f"{alert_line}\n"
            f"{sub_line}"
        )

    @staticmethod
    def _welcome_text() -> str:
        return (
            "👋 Welcome to ZapLance\n"
            "🔥 WELCOME TO THE FASTEST FREELANCE PROJECT MATCHES IN THE WORLD! 🔥\n"
            "🎯 Set your specialty, project filters, and sources once and we'll monitor freelance marketplaces, project boards, and client request sources for you.\n"
            "🤖 You'll receive fast Telegram alerts, AI summaries, and proposal or contact links."
        )

    @staticmethod
    def _how_it_works_text() -> str:
        return (
            "ℹ️ About ZapLance\n"
            "⚡ ZapLance helps you discover new freelance opportunities, project briefs, and contract posts in under 1 minute from the time they go live.\n\n"
            "🎯 Set your primary specialty, project filters, sources, and project keywords once, and we’ll continuously monitor matching opportunities for you.\n\n"
            "⚡️ Why this matters\n"
            "Most freelancers discover new briefs and contracts hours later.\n"
            "With Zap Careers, you can see relevant opportunities within seconds after they are published.\n\n"
            "📲 As soon as a relevant opportunity is posted, ZapLance sends it directly to your Telegram with:\n\n"
            "💼 Project title\n"
            "👥 Client / company / requester\n"
            "📍 Location\n"
            "💰 Budget / rate / payment when available\n"
            "🤖 AI summary\n"
            "🔗 Proposal / contact link\n\n"
            "✨ Why users choose ZapLance\n"
            "⚡ Alerts in under 1 minute\n"
            "🚀 Faster access to fresh opportunities\n"
            "🔎 Less manual searching\n"
            "🎯 Better focus with custom filters\n"
            "🌐 Multiple sources in one stream\n\n"
            "🚀 Reach out earlier. Send proposals faster. Win projects faster."
        )

    @staticmethod
    def _trial_screen_text() -> str:
        return (
            "🆓 Start your 48-hour free trial\n"
            "During your trial you get:\n"
            "• Instant Telegram project matches\n"
            "• AI project summaries\n"
            "• Budget or payment details when available\n"
            "• Direct proposal or contact links"
        )

    def _subscription_text(self, latest_sub: ActiveSubscription | None) -> str:
        lines = [
            "💳 Subscription Plans",
            "Choose the plan that fits your freelance search:",
            "• 14-Day Plan - $4.99",
            "• Monthly - $7.49",
            "• Quarterly - $14.49",
            "• Claim free trial - 48 hours",
            "✅ Every plan is renewed manually.",
        ]
        if latest_sub is not None and latest_sub.is_active:
            lines.extend(
                [
                    "",
                    f"📦 Current plan: {self._plan_display_name(latest_sub.plan)}",
                    f"📅 Expires: {format_utc_readable(latest_sub.ends_at_utc)}",
                ]
            )
        return "\n".join(lines)

    @staticmethod
    def _support_text() -> str:
        return (
            "🛟 Support\n"
            "Need help, have feedback, or want us to check something?\n"
            "📩 Contact @zapcareers and we'll get back to you as soon as possible."
        )

    @staticmethod
    def _about_text() -> str:
        return TelegramMenuBot._how_it_works_text()

    def _delivery_settings_text(self, user_id: int) -> str:
        return (
            "⏰ Delivery Settings\n"
            "Choose how and when you want to receive project matches.\n\n"
            f"⚡ Mode: {self._delivery_mode_label(user_id)}\n"
            f"🌍 Timezone: {self._timezone_offset_label(self.store.get_timezone_offset_minutes(user_id))}\n"
            f"🌙 Quiet hours: {self._quiet_hours_label(user_id)}\n\n"
            "Instant alerts send every matching project as soon as it is available.\n"
            "Digest mode groups matches and sends them every 6 hours in your local time."
        )

    def _payment_message_text(self, user_id: int, plan: PlanDefinition, payment_url: str, status: str) -> str:
        return self._t(
            user_id,
            "payment_checkout_message",
            plan_title=plan.title,
            duration_days=plan.duration_days,
            amount_usd=plan.amount_usd,
            payment_url=payment_url,
            status=payment_status_label(self._ui_language_or_default(user_id), status),
        )

    def _welcome_markup(self, user_id: int) -> InlineKeyboardMarkup:
        rows = [[InlineKeyboardButton("✨ Create Alert", callback_data=CALLBACK_CREATE_ALERT)]]
        if not self.store.has_claimed_trial(user_id):
            rows.append([InlineKeyboardButton("🆓 Activate Trial", callback_data=CALLBACK_TRIAL_START)])
        rows.extend(
            [
                [InlineKeyboardButton("💳 Subscribe", callback_data=CALLBACK_SUBSCRIPTION_MENU)],
                [InlineKeyboardButton("ℹ️ About ZapLance", callback_data=CALLBACK_ABOUT_MENU)],
                [InlineKeyboardButton("🛟 Support", callback_data=CALLBACK_SUPPORT_MENU)],
            ]
        )
        return InlineKeyboardMarkup(rows)

    @staticmethod
    def _main_menu_markup() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🎯 My Alert", callback_data=CALLBACK_MY_ALERT)],
                [InlineKeyboardButton("💳 Subscription", callback_data=CALLBACK_SUBSCRIPTION_MENU)],
                [InlineKeyboardButton("ℹ️ About ZapLance", callback_data=CALLBACK_ABOUT_MENU)],
                [InlineKeyboardButton("🛟 Support", callback_data=CALLBACK_SUPPORT_MENU)],
            ]
        )

    @staticmethod
    def _how_it_works_markup() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("💳 Subscribe", callback_data=CALLBACK_SUBSCRIPTION_MENU)],
                [InlineKeyboardButton("🛟 Support", callback_data=CALLBACK_SUPPORT_MENU)],
                [InlineKeyboardButton("🏠 Main Menu", callback_data=CALLBACK_BACK_TO_MAIN)],
            ]
        )

    @staticmethod
    def _trial_screen_markup() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🆓 Activate Free Trial", callback_data=CALLBACK_TRIAL_START)],
                [InlineKeyboardButton("💳 View Subscription Plans", callback_data=CALLBACK_VIEW_PLANS)],
                [InlineKeyboardButton("🏠 Main Menu", callback_data=CALLBACK_BACK_TO_MAIN)],
            ]
        )

    @staticmethod
    def _subscription_menu_markup(user_id: int | None = None) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🆓 Claim Free Trial", callback_data=CALLBACK_TRIAL_START)],
                [InlineKeyboardButton("⏳ 14-Day Plan - $4.99", callback_data=CALLBACK_WEEKLY_PLAN)],
                [InlineKeyboardButton("📅 Monthly - $7.49", callback_data=CALLBACK_MONTHLY_PLAN)],
                [InlineKeyboardButton("🚀 Quarterly - $14.49", callback_data=CALLBACK_QUARTERLY_PLAN)],
                [InlineKeyboardButton("🏠 Main Menu", callback_data=CALLBACK_BACK_TO_MAIN)],
            ]
        )

    @staticmethod
    def _support_menu_markup() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("💬 Contact @zapcareers", url="https://t.me/zapcareers")],
                [InlineKeyboardButton("🏠 Main Menu", callback_data=CALLBACK_BACK_TO_MAIN)],
            ]
        )

    @staticmethod
    def _support_write_markup() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Main Menu", callback_data=CALLBACK_BACK_TO_MAIN)]])

    @staticmethod
    def _feedback_reason_prompt_markup() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Main Menu", callback_data=CALLBACK_BACK_TO_MAIN)]])

    @staticmethod
    def _about_markup() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("💳 Subscribe", callback_data=CALLBACK_SUBSCRIPTION_MENU)],
                [InlineKeyboardButton("🛟 Support", callback_data=CALLBACK_SUPPORT_MENU)],
                [InlineKeyboardButton("🏠 Main Menu", callback_data=CALLBACK_BACK_TO_MAIN)],
            ]
        )

    def _role_prompt_markup(self, user_id: int) -> InlineKeyboardMarkup:
        rows = [
            [InlineKeyboardButton(f"🎨 {preset}", callback_data=f"{CALLBACK_ROLE_PRESET_PREFIX}{preset.replace(' ', '_')}")]
            for preset in DEFAULT_ROLE_PRESETS
        ]
        rows.append([InlineKeyboardButton("✍️ Type My Own Specialty", callback_data=CALLBACK_ROLE_CUSTOM)])
        rows.append(
            [
                InlineKeyboardButton(
                    "⬅️ Back",
                    callback_data=CALLBACK_BACK_TO_MAIN
                    if self.flow_origin.get(user_id) == FLOW_CREATE
                    else CALLBACK_BACK_TO_EDIT_ALERT,
                )
            ]
        )
        return InlineKeyboardMarkup(rows)

    def _role_text_input_markup(self, user_id: int) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "⬅️ Back",
                        callback_data=CALLBACK_BACK_TO_MAIN
                        if self.flow_origin.get(user_id) == FLOW_CREATE
                        else CALLBACK_BACK_TO_EDIT_ALERT,
                    )
                ]
            ]
        )

    def _location_prompt_markup(self, user_id: int) -> InlineKeyboardMarkup:
        back_target = CALLBACK_BACK_TO_EDIT_ALERT if self.flow_origin.get(user_id) == FLOW_EDIT else CALLBACK_BACK_TO_ROLE_STEP
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        f"{self._location_button_marker(self._has_pending_location_kind(user_id, CALLBACK_LOCATION_REMOTE_GLOBAL))} {REMOTE_GLOBAL_LABEL}",
                        callback_data=CALLBACK_LOCATION_REMOTE_GLOBAL,
                    )
                ],
                [
                    InlineKeyboardButton(
                        f"{self._location_button_marker(self._has_pending_location_kind(user_id, CALLBACK_LOCATION_REMOTE_COUNTRY))} {REMOTE_WITHIN_COUNTRY_BUTTON_LABEL}",
                        callback_data=CALLBACK_LOCATION_REMOTE_COUNTRY,
                    )
                ],
                [
                    InlineKeyboardButton(
                        f"{self._location_button_marker(self._has_pending_location_kind(user_id, CALLBACK_LOCATION_ONSITE_COUNTRY))} {ONSITE_HYBRID_WITHIN_COUNTRY_BUTTON_LABEL}",
                        callback_data=CALLBACK_LOCATION_ONSITE_COUNTRY,
                    )
                ],
                [
                    InlineKeyboardButton(
                        f"{self._location_button_marker(self._has_pending_location_kind(user_id, CALLBACK_LOCATION_CUSTOM))} Type Custom Location",
                        callback_data=CALLBACK_LOCATION_CUSTOM,
                    )
                ],
                [InlineKeyboardButton("✅ Save Selections", callback_data=CALLBACK_LOCATION_SAVE)],
                [InlineKeyboardButton("🗑️ Clear Selections", callback_data=CALLBACK_LOCATION_CLEAR)],
                [InlineKeyboardButton("⬅️ Back", callback_data=back_target)],
            ]
        )

    def _location_text_input_markup(self, user_id: int) -> InlineKeyboardMarkup:
        back_target = CALLBACK_BACK_TO_EDIT_ALERT if self.flow_origin.get(user_id) == FLOW_EDIT else CALLBACK_BACK_TO_LOCATION_STEP
        return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data=back_target)]])

    @staticmethod
    def _location_confirm_markup() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("✅ Confirm", callback_data=CALLBACK_LOCATION_CONFIRM)],
                [InlineKeyboardButton("✏️ Re-enter", callback_data=CALLBACK_LOCATION_REENTER)],
                [InlineKeyboardButton("⬅️ Back", callback_data=CALLBACK_BACK_TO_LOCATION_STEP)],
            ]
        )

    def _project_preferences_prompt_markup(self, user_id: int) -> InlineKeyboardMarkup:
        back_target = CALLBACK_BACK_TO_EDIT_ALERT if self.flow_origin.get(user_id) == FLOW_EDIT else CALLBACK_BACK_TO_LOCATION_STEP
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("✍️ Add / Update Project Filters", callback_data=CALLBACK_PROJECT_FILTERS_ADD)],
                [InlineKeyboardButton("💵 Visible Budget Only", callback_data=CALLBACK_SALARY_REQUIRED)],
                [InlineKeyboardButton("🎯 Allow Undisclosed Budget", callback_data=CALLBACK_SALARY_OPTIONAL)],
                [InlineKeyboardButton("⏭️ Continue", callback_data=CALLBACK_PROJECT_FILTERS_SKIP)],
                [InlineKeyboardButton("⬅️ Back", callback_data=back_target)],
            ]
        )

    def _project_preferences_input_markup(self, user_id: int) -> InlineKeyboardMarkup:
        back_target = CALLBACK_BACK_TO_EDIT_ALERT if self.flow_origin.get(user_id) == FLOW_EDIT else CALLBACK_BACK_TO_PROJECT_FILTERS_STEP
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("⏭️ Continue Without Editing", callback_data=CALLBACK_PROJECT_FILTERS_SKIP)],
                [InlineKeyboardButton("⬅️ Back", callback_data=back_target)],
            ]
        )

    def _salary_prompt_markup(self, user_id: int) -> InlineKeyboardMarkup:
        return self._project_preferences_prompt_markup(user_id)

    @staticmethod
    def _website_text_input_markup() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data=CALLBACK_BACK_TO_WEBSITES_STEP)]])

    @staticmethod
    def _website_added_markup() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("✅ Done", callback_data=CALLBACK_WEBSITE_DONE)],
            ]
        )

    def _website_picker_markup(self, user_id: int) -> InlineKeyboardMarkup:
        options = self.website_picker_cache.get(user_id, self._available_website_choices(user_id))
        selected = self.pending_website_choices.get(user_id, set())
        all_selected = bool(options) and set(options).issubset(selected)
        rows: list[list[InlineKeyboardButton]] = []
        for index, website_url in enumerate(options):
            marker = "✅" if website_url in selected else "▫️"
            rows.append(
                [
                    InlineKeyboardButton(
                        f"{marker} {self._website_display_name(website_url)}",
                        callback_data=f"{CALLBACK_WEBSITE_PICK_PREFIX}{index}",
                    )
                ]
            )
        rows.extend(
            [
                [
                    InlineKeyboardButton(
                        "✅ Select/Unselect All" if all_selected else "Select/Unselect All",
                        callback_data=CALLBACK_WEBSITE_SELECT_ALL,
                    )
                ],
                [InlineKeyboardButton("✅ Done", callback_data=CALLBACK_WEBSITE_DONE)],
                [InlineKeyboardButton("⬅️ Back", callback_data=CALLBACK_WEBSITE_BACK)],
            ]
        )
        return InlineKeyboardMarkup(rows)

    @staticmethod
    def _delivery_text_input_markup() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data=CALLBACK_BACK_TO_EDIT_ALERT)]])

    def _delivery_settings_markup(self, user_id: int) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([[InlineKeyboardButton(self._btn(user_id, "back"), callback_data=CALLBACK_BACK_TO_EDIT_ALERT)]])

    def _keywords_prompt_markup(self, user_id: int) -> InlineKeyboardMarkup:
        back_target = CALLBACK_BACK_TO_EDIT_ALERT if self.flow_origin.get(user_id) == FLOW_EDIT else CALLBACK_BACK_TO_WEBSITES_STEP
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🏷️ Add Keywords", callback_data=CALLBACK_KEYWORDS_ADD)],
                [InlineKeyboardButton("⏭️ Skip", callback_data=CALLBACK_KEYWORDS_SKIP)],
                [InlineKeyboardButton("⬅️ Back", callback_data=back_target)],
            ]
        )

    def _keywords_input_markup(self, user_id: int) -> InlineKeyboardMarkup:
        back_target = CALLBACK_BACK_TO_EDIT_ALERT if self.flow_origin.get(user_id) == FLOW_EDIT else CALLBACK_BACK_TO_WEBSITES_STEP
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("⏭️ Skip", callback_data=CALLBACK_KEYWORDS_SKIP)],
                [InlineKeyboardButton("⬅️ Back", callback_data=back_target)],
            ]
        )

    def _keywords_confirm_markup(self, user_id: int) -> InlineKeyboardMarkup:
        back_target = CALLBACK_BACK_TO_EDIT_ALERT if self.flow_origin.get(user_id) == FLOW_EDIT else CALLBACK_BACK_TO_WEBSITES_STEP
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("✅ Confirm Keywords", callback_data=CALLBACK_KEYWORDS_CONFIRM)],
                [InlineKeyboardButton("✏️ Re-enter Keywords", callback_data=CALLBACK_KEYWORDS_REENTER)],
                [InlineKeyboardButton("⬅️ Back", callback_data=back_target)],
            ]
        )

    @staticmethod
    def _alert_summary_markup() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🚀 Activate Alert", callback_data=CALLBACK_ACTIVATE_ALERT)],
                [InlineKeyboardButton("✏️ Edit Settings", callback_data=CALLBACK_ALERT_EDIT_SETTINGS)],
                [InlineKeyboardButton("🏠 Main Menu", callback_data=CALLBACK_BACK_TO_MAIN)],
            ]
        )

    def _my_alert_markup(self, user_id: int) -> InlineKeyboardMarkup:
        is_paused = not self.store.get_notification_preference(user_id)
        toggle_label = self._btn(user_id, "resume_alert") if is_paused else self._btn(user_id, "pause_alert")
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton(self._btn(user_id, "edit_alert"), callback_data=CALLBACK_EDIT_ALERT)],
                [InlineKeyboardButton(self._btn(user_id, "why_no_matches"), callback_data=CALLBACK_ALERT_HEALTH)],
                [InlineKeyboardButton(toggle_label, callback_data=CALLBACK_TOGGLE_ALERT)],
                [InlineKeyboardButton(self._btn(user_id, "delete_alert"), callback_data=CALLBACK_DELETE_ALERT)],
                [InlineKeyboardButton(self._btn(user_id, "back_main"), callback_data=CALLBACK_BACK_TO_MAIN)],
            ]
        )

    @staticmethod
    def _alert_health_markup() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("✅ Just Continue the Same Way", callback_data=CALLBACK_ALERT_HEALTH_CONTINUE)],
                [InlineKeyboardButton("✏️ Edit Alert", callback_data=CALLBACK_EDIT_ALERT)],
                [InlineKeyboardButton("🏠 Main Menu", callback_data=CALLBACK_BACK_TO_MAIN)],
            ]
        )

    @staticmethod
    def _empty_alert_markup() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("✨ Create Alert", callback_data=CALLBACK_CREATE_ALERT)],
                [InlineKeyboardButton("🏠 Main Menu", callback_data=CALLBACK_BACK_TO_MAIN)],
            ]
        )

    @staticmethod
    def _deleted_alert_markup() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("✨ Create Alert", callback_data=CALLBACK_CREATE_ALERT)],
                [InlineKeyboardButton("🏠 Main Menu", callback_data=CALLBACK_BACK_TO_MAIN)],
                [InlineKeyboardButton("↩️ Undo", callback_data=CALLBACK_UNDO_DELETE_ALERT)],
            ]
        )

    @staticmethod
    def _edit_alert_menu_markup() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🧩 Primary Specialty", callback_data=CALLBACK_EDIT_ROLE)],
                [InlineKeyboardButton("📍 Location", callback_data=CALLBACK_EDIT_LOCATION)],
                [InlineKeyboardButton("🧩 Project Preferences", callback_data=CALLBACK_EDIT_PROJECT_FILTERS)],
                [InlineKeyboardButton("🌐 Sources", callback_data=CALLBACK_EDIT_SOURCES)],
                [InlineKeyboardButton("🏷️ Project Keywords", callback_data=CALLBACK_EDIT_KEYWORDS)],
                [InlineKeyboardButton("⏰ Delivery", callback_data=CALLBACK_EDIT_DELIVERY)],
                [InlineKeyboardButton("⬅️ Back", callback_data=CALLBACK_BACK_TO_MY_ALERT)],
            ]
        )

    @staticmethod
    def _trial_success_markup() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🎯 View My Alert", callback_data=CALLBACK_VIEW_MY_ALERT)],
                [InlineKeyboardButton("💳 Subscription", callback_data=CALLBACK_VIEW_SUBSCRIPTION)],
                [InlineKeyboardButton("🏠 Main Menu", callback_data=CALLBACK_BACK_TO_MAIN)],
            ]
        )

    @staticmethod
    def _payment_success_markup(user_id: int | None = None) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🎯 My Alert", callback_data=CALLBACK_MY_ALERT)],
                [InlineKeyboardButton("🛟 Support", callback_data=CALLBACK_SUPPORT_MENU)],
                [InlineKeyboardButton("🏠 Main Menu", callback_data=CALLBACK_BACK_TO_MAIN)],
            ]
        )

    @staticmethod
    def _localized_inline(language: str, en_text: str, ru_text: str) -> str:
        return ru_text if str(language or "").strip().lower() == "ru" else en_text

    def _display_location_for_user(self, user_id: int, raw_value: str) -> str:
        language = self._ui_language_or_default(user_id)
        display_value = self._display_location(raw_value)
        if language != "ru":
            return display_value
        if display_value == REMOTE_GLOBAL_LABEL:
            return "Удалённо из любой страны"
        remote_prefix = f"{REMOTE_WITHIN_COUNTRY_LABEL} "
        onsite_prefix = f"{ONSITE_HYBRID_WITHIN_COUNTRY_LABEL} "
        if display_value.startswith(remote_prefix):
            return f"Удалённо внутри выбранной страны: {display_value[len(remote_prefix):].strip()}"
        if display_value.startswith(onsite_prefix):
            return f"Офис / гибрид внутри выбранной страны: {display_value[len(onsite_prefix):].strip()}"
        return display_value

    def _selected_sources_text(self, selected: set[str], user_id: int | None = None) -> str:
        if not selected:
            language = self._ui_language_or_default(user_id) if user_id is not None else "en"
            return self._localized_inline(language, "None selected", "Ничего не выбрано")
        return "\n".join(f"- {self._website_display_name(url)}" for url in sorted(selected))

    def _location_selection_summary(self, user_id: int) -> str:
        selections = self._pending_location_selection_values(user_id)
        if not selections:
            return self._localized_inline(self._ui_language_or_default(user_id), "- Any location", "- Любое местоположение")
        return "\n".join(f"- {self._display_location_for_user(user_id, item)}" for item in selections)

    def _project_filter_field_names(self) -> tuple[str, ...]:
        return ACTIVE_PROJECT_FILTER_FIELDS

    def _project_filter_value_labels(self, language: str, field_name: str) -> dict[str, str]:
        return shared_project_filter_value_labels(language, field_name)

    def _project_filter_display_value(self, language: str, field_name: str, value: object) -> str:
        raw_value = shared_format_project_filter_value(language, field_name, value)
        return raw_value or self._none_label(language)

    @staticmethod
    def _project_filter_clear_tokens() -> set[str]:
        return shared_project_filter_clear_tokens()

    def _project_filter_aliases(self) -> dict[str, dict[str, tuple[str, ...]]]:
        return {}

    def _normalize_project_filter_values(self, field_name: str, raw_value: object) -> list[str]:
        return shared_normalize_project_filter_values(field_name, raw_value)

    def _project_filter_has_value(self, user_id: int, field_name: str) -> bool:
        store = getattr(self, "store", None)
        raw_preferences = store.get_project_preferences(user_id) if store is not None else {}
        preferences = normalize_project_preferences(raw_preferences)
        value = preferences.get(field_name)
        return bool(value)

    def _project_filter_current_value(self, user_id: int, field_name: str) -> str:
        language = self._ui_language_or_default(user_id)
        store = getattr(self, "store", None)
        raw_preferences = store.get_project_preferences(user_id) if store is not None else {}
        preferences = normalize_project_preferences(raw_preferences)
        if field_name in {"minimum_fixed_budget_usd", "minimum_hourly_rate_usd"}:
            value = preferences.get(field_name)
            if value is None:
                return self._none_label(language)
            if field_name == "minimum_fixed_budget_usd":
                return self._localized_inline(
                    language,
                    f"${float(value):,.0f} minimum",
                    f"от ${float(value):,.0f}",
                )
            return self._localized_inline(
                language,
                f"${float(value):,.0f}/hr minimum",
                f"от ${float(value):,.0f}/час",
            )
        value = preferences.get(field_name)
        if not value:
            return self._none_label(language)
        if isinstance(value, list):
            return ", ".join(self._project_filter_display_value(language, field_name, item) for item in value)
        return self._project_filter_display_value(language, field_name, value)

    def _project_filter_input_text(self, user_id: int, field_name: str) -> str:
        return self._project_preferences_input_text(user_id, field_name)

    def _project_preferences_summary_lines(self, user_id: int) -> list[str]:
        language = self._ui_language_or_default(user_id)
        lines: list[str] = []
        for field_name in self._project_filter_field_names():
            if not self._project_filter_has_value(user_id, field_name):
                continue
            lines.append(
                f"- {project_filter_label(language, field_name)}: {self._project_filter_current_value(user_id, field_name)}"
            )
        return lines

    def _project_preferences_summary_text(self, user_id: int) -> str:
        language = self._ui_language_or_default(user_id)
        lines = self._project_preferences_summary_lines(user_id)
        if not lines:
            return self._t(user_id, "project_filters_empty")
        heading = self._localized_inline(language, "Current project filters:", "Текущие фильтры проектов:")
        return f"{heading}\n" + "\n".join(lines[:12])

    def _project_preferences_log_text(self, user_id: int) -> str:
        lines = self._project_preferences_summary_lines(user_id)
        if not lines:
            return "none"
        return " | ".join(line.replace("- ", "", 1) for line in lines[:10])

    def _project_preferences_input_text(self, user_id: int, field_name: str | None = None) -> str:
        language = self._ui_language_or_default(user_id)
        if not field_name:
            return self._t(user_id, "project_filters_prompt_body", summary=self._project_preferences_summary_text(user_id))
        return self._t(
            user_id,
            "project_filter_value_prompt",
            field_label=project_filter_label(language, field_name),
            example=project_filter_example(language, field_name) or self._none_label(language),
            current_value=self._project_filter_current_value(user_id, field_name),
        )

    def _role_prompt_text(self, user_id: int) -> str:
        key = "role_prompt_edit" if self.flow_origin.get(user_id) == FLOW_EDIT else "role_prompt_create"
        return self._t(user_id, key)

    def _keywords_confirmation_text(self, user_id: int, interpretation: KeywordInterpretation) -> str:
        language = self._ui_language_or_default(user_id)
        lines = [
            self._t(user_id, "keywords_confirmation_intro"),
            self._localized_inline(
                language,
                f"Main focus: {', '.join(interpretation.understood_keywords) or 'None'}",
                f"Что ищем: {', '.join(interpretation.understood_keywords) or 'Ничего не выбрано'}",
            ),
        ]
        for item in interpretation.per_keyword:
            similar = ", ".join(item.similar_keywords[:10]) if item.similar_keywords else self._localized_inline(language, "No extra variations", "Без дополнительных вариантов")
            lines.append(f"- {item.input_keyword} -> {item.understood_as}")
            lines.append(self._localized_inline(language, f"Similar terms: {similar}", f"Похожие формулировки: {similar}"))
        lines.extend(
            [
                "",
                self._localized_inline(language, f"Total tracked keywords: {len(interpretation.expanded_keywords)}", f"Всего ключевых слов в поиске: {len(interpretation.expanded_keywords)}"),
                self._localized_inline(language, "Tap Confirm to save them or Re-enter to adjust them.", "Нажмите «Подтвердить», чтобы сохранить, или «Ввести заново», чтобы исправить."),
            ]
        )
        return "\n".join(lines)

    def _alert_summary_text(self, user_id: int) -> str:
        language = self._ui_language_or_default(user_id)
        role = self.store.get_role_preference(user_id) or CANONICAL_ROLE_TITLE
        location = self._display_location_for_user(user_id, self.store.get_location_preference(user_id)) or self._none_label(language)
        websites = self._selected_sources_text(set(self.store.get_user_websites(user_id)), user_id)
        keywords = self.store.get_user_keywords(user_id)
        keywords_text = ", ".join(keywords) if keywords else self._none_label(language)
        return "\n".join(
            [
                self._t(user_id, "alert_summary_title"),
                f"{self._btn(user_id, 'primary_specialty')}: {role}",
                f"{self._btn(user_id, 'location')}: {location}",
                f"{self._btn(user_id, 'project_filters')}:\n{self._project_preferences_summary_text(user_id)}",
                f"{self._btn(user_id, 'sources')}:\n{websites}",
                f"{self._btn(user_id, 'keywords')}: {keywords_text}",
            ]
        )

    def _my_alert_text(self, user_id: int) -> str:
        language = self._ui_language_or_default(user_id)
        role = self.store.get_role_preference(user_id) or CANONICAL_ROLE_TITLE
        location = self._display_location_for_user(user_id, self.store.get_location_preference(user_id)) or self._none_label(language)
        websites = self._selected_sources_text(set(self.store.get_user_websites(user_id)), user_id)
        keywords = self.store.get_user_keywords(user_id)
        keywords_text = ", ".join(keywords) if keywords else self._none_label(language)
        latest_sub = self.store.get_latest_subscription(user_id)
        if latest_sub is None or not latest_sub.is_active:
            status = self._localized_inline(language, "Inactive", "Неактивен")
        elif not self.store.get_notification_preference(user_id):
            status = self._localized_inline(language, "Paused", "На паузе")
        else:
            status = self._localized_inline(language, "Active", "Активен")
        return "\n".join(
            [
                self._t(user_id, "my_alert_title"),
                f"{self._localized_inline(language, 'Status', 'Статус')}: {status}",
                f"{self._btn(user_id, 'primary_specialty')}: {role}",
                f"{self._btn(user_id, 'location')}: {location}",
                f"{self._btn(user_id, 'project_filters')}:\n{self._project_preferences_summary_text(user_id)}",
                f"{self._btn(user_id, 'sources')}:\n{websites}",
                f"{self._btn(user_id, 'keywords')}: {keywords_text}",
            ]
        )

    def _main_menu_text(self, user_id: int) -> str:
        latest_sub = self.store.get_latest_subscription(user_id)
        language = self._ui_language_or_default(user_id)
        if not self.store.has_alert_configuration(user_id):
            alert_line = self._t(user_id, "alert_not_set")
        elif latest_sub is None or not latest_sub.is_active:
            alert_line = self._t(user_id, "alert_ready")
        elif not self.store.get_notification_preference(user_id):
            alert_line = self._t(user_id, "alert_paused")
        else:
            alert_line = self._t(user_id, "alert_active")
        if latest_sub is None or not latest_sub.is_active:
            subscription_line = self._t(user_id, "subscription_inactive")
        else:
            plan_name = self._localized_plan_display_name(language, latest_sub.plan)
            subscription_line = self._localized_inline(
                language,
                f"💳 Subscription: {plan_name} until {format_utc_readable(latest_sub.ends_at_utc)}",
                f"💳 Подписка: {plan_name} до {format_utc_readable(latest_sub.ends_at_utc)}",
            )
        return self._t(user_id, "main_menu_text", alert_line=alert_line, subscription_line=subscription_line)

    def _welcome_text(self, user_id: int) -> str:
        return self._t(user_id, "welcome_text")

    def _how_it_works_text(self, user_id: int) -> str:
        return self._t(user_id, "about_text")

    def _trial_screen_text(self, user_id: int | None = None) -> str:
        language = self._ui_language_or_default(user_id) if user_id is not None else "en"
        return self._localized_inline(
            language,
            "Start your 48-hour free trial\nYou will get instant project matches, AI summaries, payment details when available, and direct proposal or contact links.",
            "Запустите бесплатный пробный период на 48 часов\nВы будете получать подходящие проекты сразу, краткие пояснения от ИИ, данные об оплате, если они есть, и прямые ссылки для отклика или контакта.",
        )

    def _subscription_text(self, latest_sub: ActiveSubscription | None, user_id: int | None = None) -> str:
        language = self._ui_language_or_default(user_id) if user_id is not None else "en"
        lines = [self._t(user_id, "subscription_text") if user_id is not None else localized_text(language, "subscription_text")]
        if latest_sub is not None and latest_sub.is_active:
            plan_name = self._localized_plan_display_name(language, latest_sub.plan)
            lines.extend(
                [
                    "",
                    self._localized_inline(language, f"Current plan: {plan_name}", f"Текущий план: {plan_name}"),
                    self._localized_inline(language, f"Expires: {format_utc_readable(latest_sub.ends_at_utc)}", f"Истекает: {format_utc_readable(latest_sub.ends_at_utc)}"),
                ]
            )
        return "\n".join(lines)

    def _support_text(self, user_id: int | None = None) -> str:
        return self._t(user_id, "support_text") if user_id is not None else localized_text("en", "support_text")

    def _about_text(self, user_id: int | None = None) -> str:
        return self._how_it_works_text(user_id or 0)

    def _delivery_settings_text(self, user_id: int) -> str:
        language = self._ui_language_or_default(user_id)
        return self._localized_inline(
            language,
            "Delivery settings were removed. Active alerts now send matches instantly.",
            "Настроек доставки больше нет. Как только появляется подходящий проект, я сразу его отправляю.",
        )

    def _location_option_label(self, user_id: int, callback_data: str) -> str:
        if callback_data == CALLBACK_LOCATION_REMOTE_GLOBAL:
            return self._btn(user_id, "remote_global")
        if callback_data == CALLBACK_LOCATION_REMOTE_COUNTRY:
            return self._btn(user_id, "remote_country")
        if callback_data == CALLBACK_LOCATION_ONSITE_COUNTRY:
            return self._btn(user_id, "onsite_country")
        return self._btn(user_id, "type_custom_location")

    def _welcome_markup(self, user_id: int) -> InlineKeyboardMarkup:
        rows = [[InlineKeyboardButton(self._btn(user_id, "create_alert"), callback_data=CALLBACK_CREATE_ALERT)]]
        if not self.store.has_claimed_trial(user_id):
            rows.append([InlineKeyboardButton(self._btn(user_id, "activate_trial"), callback_data=CALLBACK_TRIAL_START)])
        rows.extend(
            [
                [InlineKeyboardButton(self._btn(user_id, "subscription"), callback_data=CALLBACK_SUBSCRIPTION_MENU)],
                [InlineKeyboardButton(self._btn(user_id, "about"), callback_data=CALLBACK_ABOUT_MENU)],
                [InlineKeyboardButton(self._btn(user_id, "support"), callback_data=CALLBACK_SUPPORT_MENU)],
                [InlineKeyboardButton(self._btn(user_id, "language"), callback_data=CALLBACK_EDIT_LANGUAGE)],
            ]
        )
        return InlineKeyboardMarkup(rows)

    def _main_menu_markup(self, user_id: int | None = None) -> InlineKeyboardMarkup:
        language = self._ui_language_or_default(user_id) if user_id is not None else "en"
        button_text = (lambda key: self._btn(user_id, key)) if user_id is not None else (lambda key: localized_button(language, key))
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton(button_text("my_alert"), callback_data=CALLBACK_MY_ALERT)],
                [InlineKeyboardButton(button_text("subscription"), callback_data=CALLBACK_SUBSCRIPTION_MENU)],
                [InlineKeyboardButton(button_text("about"), callback_data=CALLBACK_ABOUT_MENU)],
                [InlineKeyboardButton(button_text("support"), callback_data=CALLBACK_SUPPORT_MENU)],
                [InlineKeyboardButton(button_text("language"), callback_data=CALLBACK_EDIT_LANGUAGE)],
            ]
        )

    def _how_it_works_markup(self, user_id: int | None = None) -> InlineKeyboardMarkup:
        language = self._ui_language_or_default(user_id) if user_id is not None else "en"
        button_text = (lambda key: self._btn(user_id, key)) if user_id is not None else (lambda key: localized_button(language, key))
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton(button_text("subscription"), callback_data=CALLBACK_SUBSCRIPTION_MENU)],
                [InlineKeyboardButton(button_text("support"), callback_data=CALLBACK_SUPPORT_MENU)],
                [InlineKeyboardButton(button_text("back_main"), callback_data=CALLBACK_BACK_TO_MAIN)],
            ]
        )

    def _trial_screen_markup(self, user_id: int | None = None) -> InlineKeyboardMarkup:
        language = self._ui_language_or_default(user_id) if user_id is not None else "en"
        button_text = (lambda key: self._btn(user_id, key)) if user_id is not None else (lambda key: localized_button(language, key))
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton(button_text("activate_trial"), callback_data=CALLBACK_TRIAL_START)],
                [InlineKeyboardButton(button_text("subscription"), callback_data=CALLBACK_VIEW_PLANS)],
                [InlineKeyboardButton(button_text("back_main"), callback_data=CALLBACK_BACK_TO_MAIN)],
            ]
        )

    def _subscription_menu_markup(self, user_id: int | None = None) -> InlineKeyboardMarkup:
        language = self._ui_language_or_default(user_id) if user_id is not None else "en"
        button_text = (lambda key: self._btn(user_id, key)) if user_id is not None else (lambda key: localized_button(language, key))
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton(button_text("claim_trial"), callback_data=CALLBACK_TRIAL_START)],
                [InlineKeyboardButton(button_text("plan_14_day"), callback_data=CALLBACK_WEEKLY_PLAN)],
                [InlineKeyboardButton(button_text("plan_monthly"), callback_data=CALLBACK_MONTHLY_PLAN)],
                [InlineKeyboardButton(button_text("plan_quarterly"), callback_data=CALLBACK_QUARTERLY_PLAN)],
                [InlineKeyboardButton(button_text("back_main"), callback_data=CALLBACK_BACK_TO_MAIN)],
            ]
        )

    def _support_menu_markup(self, user_id: int | None = None) -> InlineKeyboardMarkup:
        language = self._ui_language_or_default(user_id) if user_id is not None else "en"
        button_text = (lambda key: self._btn(user_id, key)) if user_id is not None else (lambda key: localized_button(language, key))
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton(button_text("contact_support"), url="https://t.me/zapcareers")],
                [InlineKeyboardButton(button_text("back_main"), callback_data=CALLBACK_BACK_TO_MAIN)],
            ]
        )

    def _support_write_markup(self, user_id: int | None = None) -> InlineKeyboardMarkup:
        label = self._btn(user_id, "back_main") if user_id is not None else localized_button("en", "back_main")
        return InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=CALLBACK_BACK_TO_MAIN)]])

    def _feedback_reason_prompt_markup(self, user_id: int | None = None) -> InlineKeyboardMarkup:
        label = self._btn(user_id, "back_main") if user_id is not None else localized_button("en", "back_main")
        return InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=CALLBACK_BACK_TO_MAIN)]])

    def _about_markup(self, user_id: int | None = None) -> InlineKeyboardMarkup:
        return self._how_it_works_markup(user_id)

    def _role_prompt_markup(self, user_id: int) -> InlineKeyboardMarkup:
        back_target = CALLBACK_BACK_TO_MAIN if self.flow_origin.get(user_id) == FLOW_CREATE else CALLBACK_BACK_TO_EDIT_ALERT
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton(CANONICAL_ROLE_TITLE, callback_data=CALLBACK_ROLE_CUSTOM)],
                [InlineKeyboardButton(self._btn(user_id, "back"), callback_data=back_target)],
            ]
        )

    def _role_text_input_markup(self, user_id: int) -> InlineKeyboardMarkup:
        back_target = CALLBACK_BACK_TO_MAIN if self.flow_origin.get(user_id) == FLOW_CREATE else CALLBACK_BACK_TO_EDIT_ALERT
        return InlineKeyboardMarkup([[InlineKeyboardButton(self._btn(user_id, "back"), callback_data=back_target)]])

    def _location_prompt_markup(self, user_id: int) -> InlineKeyboardMarkup:
        back_target = CALLBACK_BACK_TO_EDIT_ALERT if self.flow_origin.get(user_id) == FLOW_EDIT else CALLBACK_BACK_TO_ROLE_STEP
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton(f"{self._location_button_marker(self._has_pending_location_kind(user_id, CALLBACK_LOCATION_REMOTE_GLOBAL))} {self._location_option_label(user_id, CALLBACK_LOCATION_REMOTE_GLOBAL)}", callback_data=CALLBACK_LOCATION_REMOTE_GLOBAL)],
                [InlineKeyboardButton(f"{self._location_button_marker(self._has_pending_location_kind(user_id, CALLBACK_LOCATION_REMOTE_COUNTRY))} {self._location_option_label(user_id, CALLBACK_LOCATION_REMOTE_COUNTRY)}", callback_data=CALLBACK_LOCATION_REMOTE_COUNTRY)],
                [InlineKeyboardButton(f"{self._location_button_marker(self._has_pending_location_kind(user_id, CALLBACK_LOCATION_ONSITE_COUNTRY))} {self._location_option_label(user_id, CALLBACK_LOCATION_ONSITE_COUNTRY)}", callback_data=CALLBACK_LOCATION_ONSITE_COUNTRY)],
                [InlineKeyboardButton(f"{self._location_button_marker(self._has_pending_location_kind(user_id, CALLBACK_LOCATION_CUSTOM))} {self._location_option_label(user_id, CALLBACK_LOCATION_CUSTOM)}", callback_data=CALLBACK_LOCATION_CUSTOM)],
                [InlineKeyboardButton(self._btn(user_id, "save_selections"), callback_data=CALLBACK_LOCATION_SAVE)],
                [InlineKeyboardButton(self._btn(user_id, "clear_selections"), callback_data=CALLBACK_LOCATION_CLEAR)],
                [InlineKeyboardButton(self._btn(user_id, "back"), callback_data=back_target)],
            ]
        )

    def _location_text_input_markup(self, user_id: int) -> InlineKeyboardMarkup:
        back_target = CALLBACK_BACK_TO_EDIT_ALERT if self.flow_origin.get(user_id) == FLOW_EDIT else CALLBACK_BACK_TO_LOCATION_STEP
        return InlineKeyboardMarkup([[InlineKeyboardButton(self._btn(user_id, "back"), callback_data=back_target)]])

    def _location_confirm_markup(self, user_id: int | None = None) -> InlineKeyboardMarkup:
        language = self._ui_language_or_default(user_id) if user_id is not None else "en"
        button_text = (lambda key: self._btn(user_id, key)) if user_id is not None else (lambda key: localized_button(language, key))
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton(button_text("confirm"), callback_data=CALLBACK_LOCATION_CONFIRM)],
                [InlineKeyboardButton(button_text("reenter"), callback_data=CALLBACK_LOCATION_REENTER)],
                [InlineKeyboardButton(button_text("back"), callback_data=CALLBACK_BACK_TO_LOCATION_STEP)],
            ]
        )

    def _project_preferences_prompt_markup(self, user_id: int) -> InlineKeyboardMarkup:
        back_target = CALLBACK_BACK_TO_EDIT_ALERT if self.flow_origin.get(user_id) == FLOW_EDIT else CALLBACK_BACK_TO_LOCATION_STEP
        rows: list[list[InlineKeyboardButton]] = []
        for field_name in self._project_filter_field_names():
            marker = "[x]" if self._project_filter_has_value(user_id, field_name) else "[ ]"
            rows.append(
                [
                    InlineKeyboardButton(
                        f"{marker} {project_filter_label(self._ui_language_or_default(user_id), field_name)}",
                        callback_data=f"{CALLBACK_PROJECT_FILTERS_FIELD_PREFIX}{field_name}",
                    )
                ]
            )
        rows.extend(
            [
                [InlineKeyboardButton(self._btn(user_id, "continue"), callback_data=CALLBACK_PROJECT_FILTERS_SKIP)],
                [InlineKeyboardButton(self._btn(user_id, "back"), callback_data=back_target)],
            ]
        )
        return InlineKeyboardMarkup(rows)

    def _project_preferences_input_markup(self, user_id: int) -> InlineKeyboardMarkup:
        back_target = CALLBACK_BACK_TO_EDIT_ALERT if self.flow_origin.get(user_id) == FLOW_EDIT else CALLBACK_BACK_TO_PROJECT_FILTERS_STEP
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton(self._btn(user_id, "continue"), callback_data=CALLBACK_PROJECT_FILTERS_SKIP)],
                [InlineKeyboardButton(self._btn(user_id, "back"), callback_data=back_target)],
            ]
        )

    def _website_text_input_markup(self, user_id: int | None = None) -> InlineKeyboardMarkup:
        label = self._btn(user_id, "back") if user_id is not None else localized_button("en", "back")
        return InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=CALLBACK_BACK_TO_WEBSITES_STEP)]])

    def _website_added_markup(self, user_id: int | None = None) -> InlineKeyboardMarkup:
        language = self._ui_language_or_default(user_id) if user_id is not None else "en"
        button_text = (lambda key: self._btn(user_id, key)) if user_id is not None else (lambda key: localized_button(language, key))
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton(button_text("done"), callback_data=CALLBACK_WEBSITE_DONE)],
            ]
        )

    def _website_picker_markup(self, user_id: int) -> InlineKeyboardMarkup:
        options = self.website_picker_cache.get(user_id, self._available_website_choices(user_id))
        selected = self.pending_website_choices.get(user_id, set())
        rows: list[list[InlineKeyboardButton]] = []
        for index, website_url in enumerate(options):
            marker = "[x]" if website_url in selected else "[ ]"
            rows.append([InlineKeyboardButton(f"{marker} {self._website_display_name(website_url)}", callback_data=f"{CALLBACK_WEBSITE_PICK_PREFIX}{index}")])
        rows.extend(
            [
                [InlineKeyboardButton(self._btn(user_id, "select_all"), callback_data=CALLBACK_WEBSITE_SELECT_ALL)],
                [InlineKeyboardButton(self._btn(user_id, "done"), callback_data=CALLBACK_WEBSITE_DONE)],
                [InlineKeyboardButton(self._btn(user_id, "back"), callback_data=CALLBACK_WEBSITE_BACK)],
            ]
        )
        return InlineKeyboardMarkup(rows)

    def _keywords_prompt_markup(self, user_id: int) -> InlineKeyboardMarkup:
        back_target = CALLBACK_BACK_TO_EDIT_ALERT if self.flow_origin.get(user_id) == FLOW_EDIT else CALLBACK_BACK_TO_WEBSITES_STEP
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton(self._btn(user_id, "add_keywords"), callback_data=CALLBACK_KEYWORDS_ADD)],
                [InlineKeyboardButton(self._btn(user_id, "skip"), callback_data=CALLBACK_KEYWORDS_SKIP)],
                [InlineKeyboardButton(self._btn(user_id, "back"), callback_data=back_target)],
            ]
        )

    def _keywords_input_markup(self, user_id: int) -> InlineKeyboardMarkup:
        back_target = CALLBACK_BACK_TO_EDIT_ALERT if self.flow_origin.get(user_id) == FLOW_EDIT else CALLBACK_BACK_TO_WEBSITES_STEP
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton(self._btn(user_id, "skip"), callback_data=CALLBACK_KEYWORDS_SKIP)],
                [InlineKeyboardButton(self._btn(user_id, "back"), callback_data=back_target)],
            ]
        )

    def _keywords_confirm_markup(self, user_id: int) -> InlineKeyboardMarkup:
        back_target = CALLBACK_BACK_TO_EDIT_ALERT if self.flow_origin.get(user_id) == FLOW_EDIT else CALLBACK_BACK_TO_WEBSITES_STEP
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton(self._btn(user_id, "confirm_keywords"), callback_data=CALLBACK_KEYWORDS_CONFIRM)],
                [InlineKeyboardButton(self._btn(user_id, "reenter_keywords"), callback_data=CALLBACK_KEYWORDS_REENTER)],
                [InlineKeyboardButton(self._btn(user_id, "back"), callback_data=back_target)],
            ]
        )

    def _alert_summary_markup(self, user_id: int | None = None) -> InlineKeyboardMarkup:
        language = self._ui_language_or_default(user_id) if user_id is not None else "en"
        button_text = (lambda key: self._btn(user_id, key)) if user_id is not None else (lambda key: localized_button(language, key))
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton(button_text("activate_alert"), callback_data=CALLBACK_ACTIVATE_ALERT)],
                [InlineKeyboardButton(button_text("edit_alert"), callback_data=CALLBACK_ALERT_EDIT_SETTINGS)],
                [InlineKeyboardButton(button_text("back_main"), callback_data=CALLBACK_BACK_TO_MAIN)],
            ]
        )

    def _my_alert_markup(self, user_id: int) -> InlineKeyboardMarkup:
        toggle_label = self._btn(user_id, "resume_alert") if not self.store.get_notification_preference(user_id) else self._btn(user_id, "pause_alert")
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton(self._btn(user_id, "edit_alert"), callback_data=CALLBACK_EDIT_ALERT)],
                [InlineKeyboardButton(self._btn(user_id, "why_no_matches"), callback_data=CALLBACK_ALERT_HEALTH)],
                [InlineKeyboardButton(toggle_label, callback_data=CALLBACK_TOGGLE_ALERT)],
                [InlineKeyboardButton(self._btn(user_id, "delete_alert"), callback_data=CALLBACK_DELETE_ALERT)],
                [InlineKeyboardButton(self._btn(user_id, "back_main"), callback_data=CALLBACK_BACK_TO_MAIN)],
            ]
        )

    def _alert_health_markup(self, user_id: int | None = None) -> InlineKeyboardMarkup:
        language = self._ui_language_or_default(user_id) if user_id is not None else "en"
        button_text = (lambda key: self._btn(user_id, key)) if user_id is not None else (lambda key: localized_button(language, key))
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton(button_text("just_continue"), callback_data=CALLBACK_ALERT_HEALTH_CONTINUE)],
                [InlineKeyboardButton(button_text("edit_alert"), callback_data=CALLBACK_EDIT_ALERT)],
                [InlineKeyboardButton(button_text("back_main"), callback_data=CALLBACK_BACK_TO_MAIN)],
            ]
        )

    def _empty_alert_markup(self, user_id: int | None = None) -> InlineKeyboardMarkup:
        language = self._ui_language_or_default(user_id) if user_id is not None else "en"
        button_text = (lambda key: self._btn(user_id, key)) if user_id is not None else (lambda key: localized_button(language, key))
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton(button_text("create_alert"), callback_data=CALLBACK_CREATE_ALERT)],
                [InlineKeyboardButton(button_text("back_main"), callback_data=CALLBACK_BACK_TO_MAIN)],
            ]
        )

    def _deleted_alert_markup(self, user_id: int | None = None) -> InlineKeyboardMarkup:
        language = self._ui_language_or_default(user_id) if user_id is not None else "en"
        button_text = (lambda key: self._btn(user_id, key)) if user_id is not None else (lambda key: localized_button(language, key))
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton(button_text("create_alert"), callback_data=CALLBACK_CREATE_ALERT)],
                [InlineKeyboardButton(button_text("back_main"), callback_data=CALLBACK_BACK_TO_MAIN)],
                [InlineKeyboardButton(button_text("undo"), callback_data=CALLBACK_UNDO_DELETE_ALERT)],
            ]
        )

    def _edit_alert_menu_markup(self, user_id: int | None = None) -> InlineKeyboardMarkup:
        language = self._ui_language_or_default(user_id) if user_id is not None else "en"
        button_text = (lambda key: self._btn(user_id, key)) if user_id is not None else (lambda key: localized_button(language, key))
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton(button_text("primary_specialty"), callback_data=CALLBACK_EDIT_ROLE)],
                [InlineKeyboardButton(button_text("location"), callback_data=CALLBACK_EDIT_LOCATION)],
                [InlineKeyboardButton(button_text("project_filters"), callback_data=CALLBACK_EDIT_PROJECT_FILTERS)],
                [InlineKeyboardButton(button_text("sources"), callback_data=CALLBACK_EDIT_SOURCES)],
                [InlineKeyboardButton(button_text("keywords"), callback_data=CALLBACK_EDIT_KEYWORDS)],
                [InlineKeyboardButton(button_text("language"), callback_data=CALLBACK_EDIT_LANGUAGE)],
                [InlineKeyboardButton(button_text("back"), callback_data=CALLBACK_BACK_TO_MY_ALERT)],
            ]
        )

    def _trial_success_markup(self, user_id: int | None = None) -> InlineKeyboardMarkup:
        language = self._ui_language_or_default(user_id) if user_id is not None else "en"
        button_text = (lambda key: self._btn(user_id, key)) if user_id is not None else (lambda key: localized_button(language, key))
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton(button_text("view_my_alert"), callback_data=CALLBACK_VIEW_MY_ALERT)],
                [InlineKeyboardButton(button_text("subscription"), callback_data=CALLBACK_VIEW_SUBSCRIPTION)],
                [InlineKeyboardButton(button_text("back_main"), callback_data=CALLBACK_BACK_TO_MAIN)],
            ]
        )

    def _payment_success_markup(self, user_id: int | None = None) -> InlineKeyboardMarkup:
        language = self._ui_language_or_default(user_id) if user_id is not None else "en"
        button_text = (lambda key: self._btn(user_id, key)) if user_id is not None else (lambda key: localized_button(language, key))
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton(button_text("my_alert"), callback_data=CALLBACK_MY_ALERT)],
                [InlineKeyboardButton(button_text("support"), callback_data=CALLBACK_SUPPORT_MENU)],
                [InlineKeyboardButton(button_text("back_main"), callback_data=CALLBACK_BACK_TO_MAIN)],
            ]
        )

    async def _send_ai_processing_notice(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        user_id: int,
        username: str,
    ) -> None:
        await self._send_and_log(
            context=context,
            chat_id=chat_id,
            user_id=user_id,
            username=username,
            text=self._t(user_id, "ai_processing_notice"),
        )

    async def _show_main_menu(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        user_id: int,
        username: str,
    ) -> None:
        await self._send_and_log(
            context=context,
            chat_id=chat_id,
            user_id=user_id,
            username=username,
            text=self._main_menu_text(user_id),
            reply_markup=self._main_menu_markup(user_id),
        )

    async def _show_trial_screen(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        user_id: int,
        username: str,
    ) -> None:
        await self._send_and_log(
            context=context,
            chat_id=chat_id,
            user_id=user_id,
            username=username,
            text=self._trial_screen_text(user_id),
            reply_markup=self._trial_screen_markup(user_id),
        )

    async def _show_subscription_menu(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        user_id: int,
        username: str,
    ) -> None:
        latest_sub = self.store.get_latest_subscription(user_id)
        await self._send_and_log(
            context=context,
            chat_id=chat_id,
            user_id=user_id,
            username=username,
            text=self._subscription_text(latest_sub, user_id),
            reply_markup=self._subscription_menu_markup(user_id),
        )

    async def _show_support_menu(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        user_id: int,
        username: str,
    ) -> None:
        await self._send_and_log(
            context=context,
            chat_id=chat_id,
            user_id=user_id,
            username=username,
            text=self._support_text(user_id),
            reply_markup=self._support_menu_markup(user_id),
        )

    async def _show_about(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        user_id: int,
        username: str,
    ) -> None:
        await self._send_and_log(
            context=context,
            chat_id=chat_id,
            user_id=user_id,
            username=username,
            text=self._about_text(user_id),
            reply_markup=self._about_markup(user_id),
        )

    async def _show_role_prompt(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        user_id: int,
        username: str,
    ) -> None:
        self.flow_step[user_id] = STEP_ROLE
        await self._send_and_log(
            context=context,
            chat_id=chat_id,
            user_id=user_id,
            username=username,
            text=self._role_prompt_text(user_id),
            reply_markup=self._role_prompt_markup(user_id),
        )

    async def _save_role_and_continue(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        user_id: int,
        username: str,
        role_title: str,
    ) -> None:
        self.pending_inputs.pop(user_id, None)
        self.store.set_role_preference(user_id, CANONICAL_ROLE_TITLE)
        self._log_filter_update(user_id, username, "role", CANONICAL_ROLE_TITLE)
        self._sync_saved_default_websites_for_role(user_id, username)
        if self.flow_origin.get(user_id) == FLOW_CREATE:
            await self._show_location_prompt(context, chat_id, user_id, username)
        else:
            await self._send_edit_saved(context, chat_id, user_id, username)

    async def _show_location_prompt(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        user_id: int,
        username: str,
    ) -> None:
        self.flow_step[user_id] = STEP_LOCATION
        heading_key = "location_prompt_edit_heading" if self.flow_origin.get(user_id) == FLOW_EDIT else "location_prompt_create_heading"
        text = "\n".join(
            [
                self._t(user_id, heading_key),
                self._t(user_id, "location_prompt_body", current_location=self._location_selection_summary(user_id)),
            ]
        )
        await self._send_and_log(
            context=context,
            chat_id=chat_id,
            user_id=user_id,
            username=username,
            text=text,
            reply_markup=self._location_prompt_markup(user_id),
        )

    async def _confirm_custom_location(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        user_id: int,
        username: str,
    ) -> None:
        candidate = self.pending_location_candidates.pop(user_id, None)
        location_mode = self.pending_location_modes.pop(user_id, LOCATION_MODE_CUSTOM)
        if candidate is None:
            self.pending_location_modes[user_id] = location_mode
            self.pending_inputs[user_id] = WAITING_LOCATION_INPUT
            prompt_key = (
                "location_remote_country_prompt"
                if location_mode == LOCATION_MODE_REMOTE_COUNTRY
                else "location_onsite_country_prompt"
                if location_mode == LOCATION_MODE_ONSITE_HYBRID_COUNTRY
                else "location_custom_prompt"
            )
            await self._send_and_log(
                context=context,
                chat_id=chat_id,
                user_id=user_id,
                username=username,
                text=self._t(user_id, prompt_key),
                reply_markup=self._location_text_input_markup(user_id),
            )
            return
        if location_mode == LOCATION_MODE_REMOTE_COUNTRY:
            country = (candidate.country or candidate.canonical or candidate.raw_input).strip()
            self._add_pending_location_selection(user_id, self._remote_within_country_location(country))
        elif location_mode == LOCATION_MODE_ONSITE_HYBRID_COUNTRY:
            country = (candidate.country or candidate.canonical or candidate.raw_input).strip()
            self._add_pending_location_selection(user_id, self._onsite_hybrid_within_country_location(country))
        else:
            self._add_pending_location_selection(user_id, candidate.canonical or candidate.raw_input)
        self.pending_inputs.pop(user_id, None)
        await self._show_location_prompt(context, chat_id, user_id, username)

    async def _show_project_preferences_prompt(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        user_id: int,
        username: str,
    ) -> None:
        self.flow_step[user_id] = STEP_PROJECT_FILTERS
        heading_key = "project_filters_prompt_edit_heading" if self.flow_origin.get(user_id) == FLOW_EDIT else "project_filters_prompt_create_heading"
        await self._send_and_log(
            context=context,
            chat_id=chat_id,
            user_id=user_id,
            username=username,
            text="\n".join(
                [
                    self._t(user_id, heading_key),
                    self._t(user_id, "project_filters_prompt_body", summary=self._project_preferences_summary_text(user_id)),
                ]
            ),
            reply_markup=self._project_preferences_prompt_markup(user_id),
        )

    async def _handle_project_preferences_text(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        user_id: int,
        username: str,
        text: str,
    ) -> None:
        field_name = self.pending_project_filter_fields.get(user_id, "")
        if not field_name:
            self.pending_inputs.pop(user_id, None)
            await self._show_project_preferences_prompt(context, chat_id, user_id, username)
            return
        raw_text = str(text or "").strip()
        if not raw_text:
            await self._send_and_log(
                context=context,
                chat_id=chat_id,
                user_id=user_id,
                username=username,
                text=self._project_filter_input_text(user_id, field_name),
                reply_markup=self._project_preferences_input_markup(user_id),
            )
            return
        preferences = normalize_project_preferences(self.store.get_project_preferences(user_id))
        if normalize_match_text(raw_text) in self._project_filter_clear_tokens():
            if field_name in {"minimum_fixed_budget_usd", "minimum_hourly_rate_usd"}:
                preferences[field_name] = None
            else:
                preferences[field_name] = []
        elif field_name in {"minimum_fixed_budget_usd", "minimum_hourly_rate_usd"}:
            parsed_number = self._parse_project_filter_number(raw_text)
            if parsed_number is None:
                language = self._ui_language_or_default(user_id)
                invalid_text = self._localized_inline(
                    language,
                    "Send a valid number like `1500` or `35`.",
                    "Отправьте корректное число, например `1500` или `35`.",
                )
                await self._send_and_log(
                    context=context,
                    chat_id=chat_id,
                    user_id=user_id,
                    username=username,
                    text=invalid_text,
                    reply_markup=self._project_preferences_input_markup(user_id),
                )
                return
            preferences[field_name] = parsed_number
        else:
            preferences[field_name] = self._normalize_project_filter_values(field_name, raw_text)
        self.store.set_project_preferences(user_id, preferences)
        self.pending_inputs.pop(user_id, None)
        self.pending_project_filter_fields.pop(user_id, None)
        self._log_filter_update(user_id, username, "project_preferences", self._project_preferences_log_text(user_id))
        await self._show_project_preferences_prompt(context, chat_id, user_id, username)

    async def _show_website_picker(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        user_id: int,
        username: str,
        edit_query: CallbackQuery | None = None,
    ) -> None:
        self.flow_step[user_id] = STEP_WEBSITES
        role_title = self.store.get_role_preference(user_id)
        selected = self.pending_website_choices.get(user_id, set(self.store.get_user_websites(user_id)))
        remapped = set(self._remap_default_source_urls(selected, role_title))
        self.pending_website_choices[user_id] = remapped
        self.website_picker_cache[user_id] = self._available_website_choices(user_id)
        heading_key = "website_prompt_edit_heading" if self.flow_origin.get(user_id) == FLOW_EDIT else "website_prompt_create_heading"
        await self._send_or_edit_and_log(
            context=context,
            chat_id=chat_id,
            user_id=user_id,
            username=username,
            text="\n".join(
                [
                    self._t(user_id, heading_key),
                    self._t(user_id, "website_prompt_body", selected_sources=self._selected_sources_text(remapped, user_id)),
                ]
            ),
            reply_markup=self._website_picker_markup(user_id),
            edit_query=edit_query,
        )

    async def _show_keywords_prompt(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        user_id: int,
        username: str,
    ) -> None:
        self.flow_step[user_id] = STEP_KEYWORDS
        current_keywords = self.store.get_user_keywords(user_id)
        current_text = ", ".join(current_keywords) if current_keywords else self._none_label(self._ui_language_or_default(user_id))
        heading_key = "keywords_prompt_edit_heading" if self.flow_origin.get(user_id) == FLOW_EDIT else "keywords_prompt_create_heading"
        await self._send_and_log(
            context=context,
            chat_id=chat_id,
            user_id=user_id,
            username=username,
            text="\n".join(
                [
                    self._t(user_id, heading_key),
                    self._t(user_id, "keywords_prompt_body", current_keywords=current_text),
                ]
            ),
            reply_markup=self._keywords_prompt_markup(user_id),
        )

    async def _show_alert_summary(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        user_id: int,
        username: str,
    ) -> None:
        self.flow_step[user_id] = STEP_SUMMARY
        await self._send_and_log(
            context=context,
            chat_id=chat_id,
            user_id=user_id,
            username=username,
            text=self._alert_summary_text(user_id),
            reply_markup=self._alert_summary_markup(user_id),
        )

    async def _show_my_alert(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        user_id: int,
        username: str,
    ) -> None:
        self._clear_user_flow_state(user_id)
        if not self.store.has_alert_configuration(user_id):
            await self._send_and_log(
                context=context,
                chat_id=chat_id,
                user_id=user_id,
                username=username,
                text=self._t(user_id, "empty_alert"),
                reply_markup=self._empty_alert_markup(user_id),
            )
            return
        await self._send_and_log(
            context=context,
            chat_id=chat_id,
            user_id=user_id,
            username=username,
            text=self._my_alert_text(user_id),
            reply_markup=self._my_alert_markup(user_id),
        )

    async def _show_edit_alert_menu(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        user_id: int,
        username: str,
    ) -> None:
        await self._send_and_log(
            context=context,
            chat_id=chat_id,
            user_id=user_id,
            username=username,
            text=self._t(user_id, "edit_alert_text"),
            reply_markup=self._edit_alert_menu_markup(user_id),
        )

    async def _send_edit_saved(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        user_id: int,
        username: str,
    ) -> None:
        self._clear_user_flow_state(user_id)
        await self._send_and_log(
            context=context,
            chat_id=chat_id,
            user_id=user_id,
            username=username,
            text=self._t(user_id, "edit_saved"),
            reply_markup=self._my_alert_markup(user_id),
        )
        await self._show_my_alert(context, chat_id, user_id, username)

    def _none_label(self, language: str) -> str:
        return "Не задано" if str(language or "").strip().lower() == "ru" else "Not set"

    def _payment_actions_markup(self, user_id: int, local_payment_id: str) -> InlineKeyboardMarkup:
        payment_id = truncate_callback_id(local_payment_id)
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(self._btn(user_id, "payment_update"), callback_data=f"{CALLBACK_PAYMENT_UPDATE_PREFIX}{payment_id}"),
                    InlineKeyboardButton(self._btn(user_id, "payment_cancel"), callback_data=f"{CALLBACK_PAYMENT_CANCEL_PREFIX}{payment_id}"),
                ],
                [InlineKeyboardButton(self._btn(user_id, "support"), callback_data=f"{CALLBACK_PAYMENT_CONTACT_PREFIX}{payment_id}")],
                [InlineKeyboardButton(self._btn(user_id, "back"), callback_data=CALLBACK_BACK_TO_SUBSCRIPTION)],
            ]
        )

    @staticmethod
    def _plan_display_name(plan_key: str) -> str:
        normalized = str(plan_key or "").strip().lower()
        if normalized in {"trial_7d", "trial_48h"}:
            return "Trial 7D"
        if normalized == "14-day":
            return "14-Day"
        return str(plan_key or "").replace("_", " ").title() or "Unknown"

    @staticmethod
    def _localized_plan_display_name(language: str, plan_key: str) -> str:
        normalized = str(plan_key or "").strip().lower()
        normalized_language = str(language or "en").strip().lower()
        if normalized_language == "ru":
            if normalized in {"trial_7d", "trial_48h"}:
                return "пробный период 7 дней"
            if normalized == "14-day":
                return "14 дней"
            if normalized == "monthly":
                return "1 месяц"
            if normalized == "quarterly":
                return "3 месяца"
        return TelegramMenuBot._plan_display_name(plan_key)

    def _trial_screen_text(self, user_id: int | None = None) -> str:
        language = self._ui_language_or_default(user_id) if user_id is not None else "en"
        return self._localized_inline(
            language,
            "Start your 7-day free trial\nYou will get instant project matches, AI summaries, payment details when available, and direct proposal or contact links.",
            "Запустите бесплатный пробный период на 7 дней\nВы будете получать подходящие проекты сразу, краткие пояснения от ИИ, данные об оплате, если они есть, и прямые ссылки для отклика или контакта.",
        )

    async def _save_role_and_continue(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        user_id: int,
        username: str,
        role_title: str,
    ) -> None:
        self.pending_inputs.pop(user_id, None)
        self.store.set_role_preference(user_id, CANONICAL_ROLE_TITLE)
        self._log_filter_update(user_id, username, "role", CANONICAL_ROLE_TITLE)
        self._sync_saved_default_websites_for_role(user_id, username)
        if self.flow_origin.get(user_id) == FLOW_CREATE:
            await self._show_project_preferences_prompt(context, chat_id, user_id, username)
        else:
            await self._send_edit_saved(context, chat_id, user_id, username)

    async def _show_location_prompt(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        user_id: int,
        username: str,
    ) -> None:
        await self._send_and_log(
            context=context,
            chat_id=chat_id,
            user_id=user_id,
            username=username,
            text=self._localized_inline(
                self._ui_language_or_default(user_id),
                "Location filters were removed. Please continue with project filters.",
                "Фильтры по местоположению удалены. Пожалуйста, перейдите к фильтрам проектов.",
            ),
            reply_markup=self._project_preferences_prompt_markup(user_id),
        )

    def _project_filter_current_value(self, user_id: int, field_name: str) -> str:
        language = self._ui_language_or_default(user_id)
        store = getattr(self, "store", None)
        raw_preferences = store.get_project_preferences(user_id) if store is not None else {}
        preferences = normalize_project_preferences(raw_preferences)
        if field_name in {"minimum_fixed_budget_usd", "minimum_hourly_rate_usd"}:
            field_name = "minimum_payment_usd"
        value = preferences.get(field_name)
        if value in (None, "", []):
            return self._none_label(language)
        if field_name in {"minimum_payment_usd", "maximum_payment_usd", "minimum_payment_rub", "maximum_payment_rub"}:
            formatted = shared_format_project_filter_value(language, field_name, value)
            if field_name.startswith("minimum_"):
                return self._localized_inline(language, f"{formatted} minimum", f"от {formatted}")
            return self._localized_inline(language, f"{formatted} maximum", f"до {formatted}")
        if isinstance(value, list):
            return ", ".join(self._project_filter_display_value(language, field_name, item) for item in value)
        return self._project_filter_display_value(language, field_name, value)

    def _project_preferences_summary_lines(self, user_id: int) -> list[str]:
        language = self._ui_language_or_default(user_id)
        lines: list[str] = []
        for field_name in self._project_filter_field_names():
            if not self._project_filter_has_value(user_id, field_name):
                continue
            lines.append(
                f"- {project_filter_label(language, field_name)}: {self._project_filter_current_value(user_id, field_name)}"
            )
        return lines

    def _project_preferences_summary_text(self, user_id: int) -> str:
        language = self._ui_language_or_default(user_id)
        lines = self._project_preferences_summary_lines(user_id)
        if not lines:
            return self._t(user_id, "project_filters_empty")
        heading = self._localized_inline(language, "Current project filters:", "Текущие фильтры проектов:")
        return f"{heading}\n" + "\n".join(lines[:12])

    def _project_preferences_log_text(self, user_id: int) -> str:
        lines = self._project_preferences_summary_lines(user_id)
        if not lines:
            return "none"
        return " | ".join(line.replace("- ", "", 1) for line in lines[:10])

    def _alert_summary_text(self, user_id: int) -> str:
        language = self._ui_language_or_default(user_id)
        role = self.store.get_role_preference(user_id) or CANONICAL_ROLE_TITLE
        websites = self._selected_sources_text(set(self.store.get_user_websites(user_id)), user_id)
        keywords = self.store.get_user_keywords(user_id)
        keywords_text = ", ".join(keywords) if keywords else self._none_label(language)
        return "\n".join(
            [
                self._t(user_id, "alert_summary_title"),
                f"{self._btn(user_id, 'primary_specialty')}: {role}",
                f"{self._btn(user_id, 'project_filters')}:\n{self._project_preferences_summary_text(user_id)}",
                f"{self._btn(user_id, 'sources')}:\n{websites}",
                f"{self._btn(user_id, 'keywords')}: {keywords_text}",
            ]
        )

    def _my_alert_text(self, user_id: int) -> str:
        language = self._ui_language_or_default(user_id)
        role = self.store.get_role_preference(user_id) or CANONICAL_ROLE_TITLE
        websites = self._selected_sources_text(set(self.store.get_user_websites(user_id)), user_id)
        keywords = self.store.get_user_keywords(user_id)
        keywords_text = ", ".join(keywords) if keywords else self._none_label(language)
        latest_sub = self.store.get_latest_subscription(user_id)
        if latest_sub is None or not latest_sub.is_active:
            status = self._localized_inline(language, "Inactive", "Неактивен")
        elif not self.store.get_notification_preference(user_id):
            status = self._localized_inline(language, "Paused", "На паузе")
        else:
            status = self._localized_inline(language, "Active", "Активен")
        return "\n".join(
            [
                self._t(user_id, "my_alert_title"),
                f"{self._localized_inline(language, 'Status', 'Статус')}: {status}",
                f"{self._btn(user_id, 'primary_specialty')}: {role}",
                f"{self._btn(user_id, 'project_filters')}:\n{self._project_preferences_summary_text(user_id)}",
                f"{self._btn(user_id, 'sources')}:\n{websites}",
                f"{self._btn(user_id, 'keywords')}: {keywords_text}",
            ]
        )

    def _project_preferences_prompt_markup(self, user_id: int) -> InlineKeyboardMarkup:
        flow_origin = getattr(self, "flow_origin", {})
        back_target = CALLBACK_BACK_TO_EDIT_ALERT if flow_origin.get(user_id) == FLOW_EDIT else CALLBACK_BACK_TO_ROLE_STEP
        rows: list[list[InlineKeyboardButton]] = []
        for field_name in self._project_filter_field_names():
            marker = "[x]" if self._project_filter_has_value(user_id, field_name) else "[ ]"
            rows.append(
                [
                    InlineKeyboardButton(
                        f"{marker} {project_filter_label(self._ui_language_or_default(user_id), field_name)}",
                        callback_data=f"{CALLBACK_PROJECT_FILTERS_FIELD_PREFIX}{field_name}",
                    )
                ]
            )
        rows.extend(
            [
                [InlineKeyboardButton(self._btn(user_id, "continue"), callback_data=CALLBACK_PROJECT_FILTERS_SKIP)],
                [InlineKeyboardButton(self._btn(user_id, "back"), callback_data=back_target)],
            ]
        )
        return InlineKeyboardMarkup(rows)

    def _project_preferences_input_markup(self, user_id: int) -> InlineKeyboardMarkup:
        flow_origin = getattr(self, "flow_origin", {})
        back_target = CALLBACK_BACK_TO_EDIT_ALERT if flow_origin.get(user_id) == FLOW_EDIT else CALLBACK_BACK_TO_PROJECT_FILTERS_STEP
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton(self._btn(user_id, "continue"), callback_data=CALLBACK_PROJECT_FILTERS_SKIP)],
                [InlineKeyboardButton(self._btn(user_id, "back"), callback_data=back_target)],
            ]
        )

    def _website_currency_markup(self, user_id: int) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton(self._btn(user_id, "website_currency_usd"), callback_data=f"{CALLBACK_WEBSITE_CURRENCY_PREFIX}USD")],
                [InlineKeyboardButton(self._btn(user_id, "website_currency_rub"), callback_data=f"{CALLBACK_WEBSITE_CURRENCY_PREFIX}RUB")],
                [InlineKeyboardButton(self._btn(user_id, "back"), callback_data=CALLBACK_BACK_TO_WEBSITES_STEP)],
            ]
        )

    def _website_picker_markup(self, user_id: int) -> InlineKeyboardMarkup:
        options = getattr(self, "website_picker_cache", {}).get(user_id, self._available_website_choices(user_id))
        selected = getattr(self, "pending_website_choices", {}).get(user_id, set())
        rows: list[list[InlineKeyboardButton]] = []
        for index, website_url in enumerate(options):
            marker = "[x]" if website_url in selected else "[ ]"
            rows.append([InlineKeyboardButton(f"{marker} {self._website_display_name(website_url)}", callback_data=f"{CALLBACK_WEBSITE_PICK_PREFIX}{index}")])
        rows.extend(
            [
                [InlineKeyboardButton(self._btn(user_id, "add_custom_website"), callback_data=CALLBACK_WEBSITE_CUSTOM)],
                [InlineKeyboardButton(self._btn(user_id, "select_all"), callback_data=CALLBACK_WEBSITE_SELECT_ALL)],
                [InlineKeyboardButton(self._btn(user_id, "done"), callback_data=CALLBACK_WEBSITE_DONE)],
                [InlineKeyboardButton(self._btn(user_id, "back"), callback_data=CALLBACK_WEBSITE_BACK)],
            ]
        )
        return InlineKeyboardMarkup(rows)

    def _website_removed_notice_markup(self, user_id: int) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton(self._btn(user_id, "sources"), callback_data=CALLBACK_EDIT_SOURCES)],
                [InlineKeyboardButton(self._btn(user_id, "add_custom_website"), callback_data=CALLBACK_WEBSITE_CUSTOM)],
                [InlineKeyboardButton(self._btn(user_id, "back_main"), callback_data=CALLBACK_BACK_TO_MAIN)],
            ]
        )

    async def _save_websites_and_continue(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        user_id: int,
        username: str,
    ) -> None:
        selected = {
            url
            for url in self.pending_website_choices.get(user_id, set())
            if self._normalize_website_url(url) is not None
        }
        if not selected:
            await self._send_and_log(
                context=context,
                chat_id=chat_id,
                user_id=user_id,
                username=username,
                text=self._t(user_id, "website_choose_one"),
                reply_markup=self._website_picker_markup(user_id),
            )
            return
        website_currency_overrides = dict(self.store.get_user_websites_with_currency(user_id))
        if user_id in getattr(self, "pending_custom_website_urls", {}) and user_id in getattr(self, "pending_custom_website_currency", {}):
            website_currency_overrides[self.pending_custom_website_urls[user_id]] = self.pending_custom_website_currency[user_id]
        self._persist_selected_websites(
            user_id,
            username,
            selected,
            website_currency_overrides=website_currency_overrides,
        )
        self.pending_custom_website_urls.pop(user_id, None)
        self.pending_custom_website_currency.pop(user_id, None)
        if self.flow_origin.get(user_id) == FLOW_CREATE:
            await self._show_keywords_prompt(context, chat_id, user_id, username)
        else:
            await self._send_edit_saved(context, chat_id, user_id, username)

    def _edit_alert_menu_markup(self, user_id: int | None = None) -> InlineKeyboardMarkup:
        language = self._ui_language_or_default(user_id) if user_id is not None else "en"
        button_text = (lambda key: self._btn(user_id, key)) if user_id is not None else (lambda key: localized_button(language, key))
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton(button_text("primary_specialty"), callback_data=CALLBACK_EDIT_ROLE)],
                [InlineKeyboardButton(button_text("project_filters"), callback_data=CALLBACK_EDIT_PROJECT_FILTERS)],
                [InlineKeyboardButton(button_text("sources"), callback_data=CALLBACK_EDIT_SOURCES)],
                [InlineKeyboardButton(button_text("keywords"), callback_data=CALLBACK_EDIT_KEYWORDS)],
                [InlineKeyboardButton(button_text("language"), callback_data=CALLBACK_EDIT_LANGUAGE)],
                [InlineKeyboardButton(button_text("back"), callback_data=CALLBACK_BACK_TO_MY_ALERT)],
            ]
        )
